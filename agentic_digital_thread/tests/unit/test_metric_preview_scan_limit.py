# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The metric preview on an Athena scan-limit refusal, and the default window
column of a duration (measure) metric.

Two defects, both reproduced from a live rehearsal account (2026-10-19):

1. A duration-percentile metric is NOT decomposable, so its Athena query is
   narrowed up front and, when the engine still refuses it for scan size, the
   resolver re-raises the raw ``AthenaEngineScanLimitError``. ``metric_preview.
   _resolve_entry`` caught only ``MetricResolverError`` / ``NotImplementedError``
   / ``KeyError``, so the preview route answered HTTP 500. The fix turns a typed
   connector refusal into the existing ``unavailable`` card with a plain reason.

2. The default (no-window) narrowing bounded the table's generic event-time
   column (e.g. ``not_started_date``), not the column the duration measures to
   (``complete_date``). The fix bounds a measure metric's END column; a
   non-measure metric keeps today's column choice.

Offline: every graph/DB/connector seam is patched.
"""
from __future__ import annotations

import json

import pytest

import agents.athena_tools as at
import agents.dashboard as dashboard
import agents.metric_athena_scope as mas
import agents.metric_dimension as md
import agents.metric_preview as mp
import agents.metric_resolver as mr
from agents import connection_types as ct
from agents.catalog import catalog
from agents.metric_definition import canonical_json
from agents.metric_layer import CodeMetricLayer

_CONCEPTS = frozenset({"cycle-time", "event-time", "widget-event"})

# A duration-percentile (measure) metric: TP90/TP99 of complete - start.
_MEASURE = {
    "id": "cycle-tp", "kind": "direct", "label": "Cycle TP", "synonyms": [],
    "unit": "hours", "concept": "cycle-time", "aggregation": "count",
    "max_exclusive": False,
    "measure": {"kind": "duration",
                "start": {"concept": "event-time", "column": "start_date"},
                "end": {"concept": "event-time", "column": "complete_date"},
                "unit": "hours", "clamp_negative": True},
    "percentiles": [0.9, 0.99],
}
# A plain non-measure AVG on the same Athena table. AVG is NOT decomposable,
# so (like a measure) it is narrowed up front: the branch whose column choice
# this test pins. A COUNT would run as defined (no up-front narrowing at all).
_AVG = {"id": "ev-avg", "kind": "direct", "label": "Avg Qty", "synonyms": [],
        "unit": "qty", "concept": "widget-event", "aggregation": "avg",
        "max_exclusive": False}

_TABLE = "work_orders"
#: The table's GENERIC event-time column: what the old narrowing bounded on.
_GENERIC_TIME_COL = "not_started_date"
#: The measure's END column: what a measure metric's default window must bound.
_END_COL = "complete_date"


def _context():
    code_ids = frozenset(m.id for m in CodeMetricLayer().list_metrics())
    return _CONCEPTS, code_ids, code_ids, {}


def _raw(doc):
    return canonical_json(doc)


class _Connector:
    """Stub for the Athena exact-runner: records SQL and either returns a row
    result or raises a scan-limit refusal on EVERY call."""

    def __init__(self, *, refuse=False, result=None):
        self.refuse = refuse
        self.result = result or {"columns": ["p", "p", "c"],
                                 "rows": [["61.8", "121.4", "1000"]],
                                 "row_count": 1}
        self.calls: list[str] = []

    def __call__(self, sql, connection_config, **_kwargs):
        self.calls.append(sql)
        if self.refuse:
            error = at.AthenaEngineScanLimitError(
                "over the per-query scan limit", limit_source="engine")
            error.exact_split = {"applied": False, "reason": "not_decomposable"}
            raise error
        return dict(self.result)


@pytest.fixture
def seams(monkeypatch):
    """Athena seams for a measure metric on ``_TABLE``: the measure columns are
    temporal; the generic event-time column resolves to ``_GENERIC_TIME_COL``;
    no partition pruning (so the fallback time-window path is exercised)."""
    state = {"connector": _Connector()}

    monkeypatch.setattr(catalog, "get_onboarded", lambda: [{"id": "sys-a"}])
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer())
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: mr.AuthoritySource(
        system_id="sys-a", source="authority_rule"))
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mr.MappingProvenance(
        table=_TABLE, column="wo_id", role=None, confidence=None, source="onboarding"))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.ATHENA)
    monkeypatch.setattr(mr.catalog, "get", lambda sid: {
        "id": sid, "connection_type": ct.ATHENA})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {
        "workgroup": "wg", "database": "lake_db"})
    # Measure columns are timestamps; the event-time concept maps to the two
    # measure columns; the AVG concept maps to a numeric column; no recent
    # partition prune (so the fallback time-window path is exercised).
    def _neptune(cypher, params=None):
        if cypher == mr._FIELD_TYPE_CYPHER:
            return [{"data_type": "timestamp(6) with time zone"}]
        concept = (params or {}).get("concept")
        if cypher == mr._PREDICATE_BINDING_CYPHER and concept == "event-time":
            return [{"table": _TABLE, "column": "start_date"},
                    {"table": _TABLE, "column": _END_COL}]
        if cypher == mr._PREDICATE_BINDING_CYPHER and concept == "widget-event":
            return [{"table": _TABLE, "column": "qty"}]
        return []
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    # The generic event-time fallback resolves to the table's general column.
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, **k: _GENERIC_TIME_COL)

    def _runner(source_system=""):
        return state["connector"]
    monkeypatch.setattr(mas, "exact_runner", _runner)
    # metric_resolver reads the connector through run_athena_query_exact too.
    monkeypatch.setattr(at, "run_athena_query_exact",
                        lambda sql, cfg, **k: state["connector"](sql, cfg, **k),
                        raising=False)
    monkeypatch.setattr(at, "run_athena_query",
                        lambda sql, cfg, **k: state["connector"](sql, cfg, **k),
                        raising=False)
    return state


def _dry(doc):
    return mp.dry_run(_raw(doc), {}, _context())


def test_scan_limit_refusal_is_an_unavailable_card_not_a_500(seams):
    """A duration-percentile preview the engine refuses for scan size returns
    the preview's unavailable card with a plain reason, never a raised
    exception (which the route answered 500)."""
    seams["connector"] = _Connector(refuse=True)
    out = _dry(_MEASURE)
    assert out["preview"] is True
    assert out["metric_id"] == "cycle-tp"
    assert out["status"] == "unavailable"
    assert out["value"] is None
    assert "scan limit" in out["error"].lower()


def test_measure_default_window_bounds_the_end_column(seams):
    """With no window, a measure metric's default preview window bounds the
    measure's END column (``complete_date``), not the table's generic
    event-time column (``not_started_date``)."""
    out = _dry(_MEASURE)
    assert out["status"] == "ok"
    sql = " ".join(seams["connector"].calls)
    assert _END_COL in sql
    assert _GENERIC_TIME_COL not in sql


def test_non_measure_default_window_keeps_the_generic_column(seams):
    """A metric with no measure keeps today's column choice: the generic
    event-time fallback (``not_started_date``), byte-compatible."""
    out = _dry(_AVG)
    assert out["status"] == "ok"
    sql = " ".join(seams["connector"].calls)
    assert _GENERIC_TIME_COL in sql
    assert _END_COL not in sql


_PARTITION = "\"event_date\" IN (DATE '2026-10-19', DATE '2026-10-18')"


def test_measure_window_is_added_to_a_recent_partition_bound(seams, monkeypatch):
    """A table with a recent-partition bound keeps it (partition pruning) and
    the measure's END-column window is ANDed onto it, not swapped in."""
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: _PARTITION)
    out = _dry(_MEASURE)
    assert out["status"] == "ok", out
    sql = " ".join(seams["connector"].calls)
    assert _PARTITION in sql and _END_COL in sql, sql
    assert _GENERIC_TIME_COL not in sql


def test_non_measure_keeps_the_partition_bound_alone(seams, monkeypatch):
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: _PARTITION)
    out = _dry(_AVG)
    assert out["status"] == "ok", out
    sql = " ".join(seams["connector"].calls)
    assert _PARTITION in sql and _END_COL not in sql, sql
