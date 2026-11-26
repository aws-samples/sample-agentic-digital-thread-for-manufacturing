# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A duration metric asked about an explicit past day is bounded by that day,
never by the recent-days scan narrowing.

Before this change every measure metric on Athena was narrowed up front (the
table's recent partitions, else the last 30 days of event time), on the scalar
AND the grouped path, and that bound was ANDed onto the turn's absolute
window. A window older than the narrowing then read no rows and the notice
reported ``no-data`` (which forbids substitution) for a day that has data.

Pinned here, one test per behaviour:

* with an explicit window the scalar and grouped SQL carry the window and no
  partition bound and no ``current_timestamp - INTERVAL`` clause, and the
  lineage carries no narrowing disclosure;
* with no window the SQL is byte-identical to before (still narrowed);
* a scan refusal with an explicit window is never retried narrowed: in a chat
  turn it is ``not_computed_in_time`` (the system agent can compute it); with
  no turn (a card) the refusal is raised as before.

Offline: the graph, catalog and connector seams are stubbed. The schema is
invented (``jobs``, ``began_at`` / ``finished_at``, ``shade_code``).
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.metric_turn_notice as mtn
from agents import connection_types as ct
from agents.metric_layer import Aggregation, DirectMetric
from agents.metric_measure_rule import DurationMeasure, FieldRef

CONCEPT = "job-duration"
TIME = "job-time"
SHADE = "job-shade"
TABLE = "jobs"
_CFG = {"workgroup": "wg", "database": "lake_db"}
_PART = "\"day_part\" >= DATE '2026-10-10'"
_INTERVAL = "current_timestamp - INTERVAL"

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, "began_at"),
    end=FieldRef(TIME, "finished_at"), unit="hours", clamp_negative=True)
_BOUND = {
    CONCEPT: [{"table": TABLE, "column": "job_id"}],
    TIME: [{"table": TABLE, "column": "began_at"},
           {"table": TABLE, "column": "finished_at"}],
    SHADE: [{"table": TABLE, "column": "shade_code"}],
}
_SCALAR = {"columns": ["p", "p", "c"], "rows": [["5.0", "9.0", "30"]], "row_count": 1}
_GROUPED = {"columns": ["g", "p", "p", "c"],
            "rows": [["light", "5.0", "9.0", "20"], ["dark", "6.0", "10.0", "10"]],
            "row_count": 2}


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-lake"


class _Connector:
    """Stub for the Athena connector: records SQL; ``refuse`` refuses every
    query for scan size, the way the engine does."""

    def __init__(self, *, refuse=False):
        self.refuse, self.calls = refuse, []

    def __call__(self, sql, connection_config, **_kwargs):
        self.calls.append(sql)
        if self.refuse:
            error = at.AthenaEngineScanLimitError("over", limit_source="engine")
            error.exact_split = {"applied": False, "reason": "shape"}
            raise error
        return dict(_GROUPED if " GROUP BY " in sql else _SCALAR)


def _metric(grouped: bool) -> DirectMetric:
    return DirectMetric(
        id="job-tp", concept=CONCEPT, aggregation=Aggregation.COUNT,
        label="Job Time", unit="hours", measure=_MEASURE, percentiles=(0.9, 0.99),
        group_by=FieldRef(SHADE, "shade_code") if grouped else None)


def _neptune(cypher, params=None):
    params = params or {}
    if cypher == mr._FIELD_TYPE_CYPHER:
        return [{"data_type": "timestamp(6) with time zone"}]
    if cypher in (md._DIM_MAPS_TO_CYPHER, mr._MEASURE_BINDING_CYPHER,
                  mr._PREDICATE_BINDING_CYPHER):
        table = params.get("table")
        pairs = sorted({(r["table"], r["column"]) for r in _BOUND.get(params.get("concept"), [])
                        if table is None or r["table"] == table})
        return [{"table": t, "column": c} for t, c in pairs][:mb.BINDING_CANDIDATE_READ_LIMIT]
    return []


def _wire(monkeypatch, *, grouped=False, refuse=False, partition=True):
    metric = _metric(grouped)
    connector = _Connector(refuse=refuse)
    layer = type("L", (), {"get_metric": staticmethod(lambda mid: {metric.id: metric}[mid])})()
    monkeypatch.setattr(mr, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(ml, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.ATHENA)
    monkeypatch.setattr(mr.catalog, "get", lambda sid: {"id": sid, "connection_type": ct.ATHENA})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: (
        f"{k.get('qualifier', '')}{_PART}" if partition else None))
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, t, query=None: "began_at")
    monkeypatch.setattr(at, "run_athena_query_exact", connector, raising=False)
    monkeypatch.setattr(at, "run_athena_query", connector)
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    mtn.invalidate()
    return connector


def _win():
    return mr.AbsoluteWindow(start_utc="2026-08-20T00:00:00+00:00",
                             end_utc="2026-08-21T00:00:00+00:00")


def _resolve(grouped: bool, window):
    if grouped:
        return md.resolve_metric_by_dimension("job-tp", SHADE, authority=_Auth(),
                                              dimension_column="shade_code",
                                              turn_window=window)
    return mr.resolve_metric("job-tp", authority=_Auth(), turn_window=window)


def _assert_window_only(sql: str) -> None:
    assert "2026-08-20" in sql and "2026-08-21" in sql
    assert _INTERVAL not in sql
    assert "day_part" not in sql


# --------------------------------------------------------------------------- #
# an explicit window is the bound: no partition bound, no recent-days clause
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("partition", [True, False], ids=["partition", "time-window"])
def test_scalar_with_an_explicit_window_is_not_narrowed(monkeypatch, partition):
    """Without it: the scalar SQL also carries the recent-days bound, so a day
    older than it reads no rows and the notice says no-data."""
    connector = _wire(monkeypatch, partition=partition)
    resolved = _resolve(False, _win())
    assert len(connector.calls) == 1
    _assert_window_only(connector.calls[0])
    assert resolved.lineage.scan is None
    assert resolved.percentiles == ((0.9, 5.0), (0.99, 9.0))


@pytest.mark.parametrize("partition", [True, False], ids=["partition", "time-window"])
def test_grouped_with_an_explicit_window_is_not_narrowed(monkeypatch, partition):
    """Without it: the grouped SQL carries the partition bound (else the 30-day
    event-time bound) next to the window."""
    connector = _wire(monkeypatch, grouped=True, partition=partition)
    out = _resolve(True, _win())
    assert len(connector.calls) == 1
    _assert_window_only(connector.calls[0])
    assert {r.dimension_value for r in out.rows} == {"light", "dark"}
    assert all(r.lineage.scan is None for r in out.rows)


# --------------------------------------------------------------------------- #
# no window: byte-identical to before
# --------------------------------------------------------------------------- #
_NOTNULL = '"began_at" IS NOT NULL AND "finished_at" IS NOT NULL'
_EXPR = 'greatest(date_diff(\'second\', "began_at", "finished_at"), 0) / 3600.0'


def test_scalar_without_a_window_is_unchanged(monkeypatch):
    connector = _wire(monkeypatch)
    resolved = _resolve(False, None)
    assert connector.calls == [_scalar_lifetime_sql()]
    assert resolved.lineage.scan["mode"] == "narrowed"


def test_grouped_without_a_window_is_unchanged(monkeypatch):
    connector = _wire(monkeypatch, grouped=True)
    _resolve(True, None)
    assert connector.calls == [_grouped_lifetime_sql()]


def _scalar_lifetime_sql() -> str:
    return (f"SELECT approx_percentile({_EXPR}, 0.9), approx_percentile({_EXPR}, 0.99), "
            f"COUNT({_EXPR}) FROM lake_db.jobs WHERE {_NOTNULL} AND ({_PART}) AND "
            f"(\"finished_at\" >= ({_INTERVAL} '30' DAY))")


def _grouped_lifetime_sql() -> str:
    return (f'SELECT "shade_code", approx_percentile({_EXPR}, 0.9), '
            f"approx_percentile({_EXPR}, 0.99), COUNT({_EXPR}) "
            f'FROM "lake_db"."jobs" WHERE {_NOTNULL} AND {_PART} '
            'GROUP BY "shade_code" ORDER BY "shade_code" LIMIT 200')


# --------------------------------------------------------------------------- #
# a scan refusal with an explicit window: never narrowed, never no-data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_turn_refusal_with_a_window_is_not_computed_in_time(monkeypatch, grouped):
    """Without it: the scalar refusal is ``result_unavailable`` and the grouped
    one is retried narrowed (outside the window), both forbidding a number."""
    connector = _wire(monkeypatch, grouped=grouped, refuse=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert result["outcome"] == mtn.NOT_IN_TIME_OUTCOME
    assert result["system_id"] == "sys-lake"
    assert "jobs" in result["lineage"]
    assert len(connector.calls) == 1
    for sql in connector.calls:
        _assert_window_only(sql)


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_card_refusal_with_a_window_is_raised_not_narrowed(monkeypatch, grouped):
    """Outside a chat turn the refusal stands (the unavailable card); a
    narrowed retry outside the window never runs."""
    connector = _wire(monkeypatch, grouped=grouped, refuse=True)
    with pytest.raises((at.AthenaEngineScanLimitError, mr.MetricResolverError)) as caught:
        _resolve(grouped, _win())
    assert not isinstance(caught.value, mr.ExactValueNotInTime)
    assert len(connector.calls) == 1
    _assert_window_only(connector.calls[0])
