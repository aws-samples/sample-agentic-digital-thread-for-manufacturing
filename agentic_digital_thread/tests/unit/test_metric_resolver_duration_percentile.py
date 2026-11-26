# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

""": the SCALAR resolver for a steward-defined duration-PERCENTILE metric.

Pins: the two field-ref columns bind through the system's MAPS_TO on the
MEASURE table (fail-closed when a column does not map its concept, the field
NAMED); the exact Athena and PostgreSQL SQL (percentile of a clamped,
unit-scaled date_diff, paired with COUNT of the same expr, NULLs excluded, the
filter WHERE on pinned columns, the window on the measure's END column); a
percentile metric is narrowed up front on Athena (not decomposable); the
result carries every percentile and the sample count. Offline, graph/DB seams
monkeypatched on ``metric_resolver``."""
from __future__ import annotations

import json

import pytest

import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp as Op
from agents.metric_measure_rule import DurationMeasure, FieldRef
from agents.metric_resolver import (
    AuthorityResolver,
    UNRESOLVED_PREDICATE_STATUS,
    resolve_metric,
)

CONCEPT = "cycle-time"
TIME = "event-time"
STATE = "widget-color"
TABLE = "segments"

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, "start_ts"),
    end=FieldRef(TIME, "complete_ts"), unit="hours", clamp_negative=True)

# MAPS_TO graph for the measure concept, its two timestamp field refs, and the
# filter concept, all on the measure table.
_BOUND = {
    CONCEPT: [{"table": TABLE, "column": "segment_id"}],
    TIME: [{"table": TABLE, "column": "start_ts"},
           {"table": TABLE, "column": "complete_ts"}],
    STATE: [{"table": TABLE, "column": "state_code"}],
}

_ATHENA_EXPR = ('greatest(date_diff(\'second\', "start_ts", "complete_ts"), 0) '
                '/ 3600.0')
_PG_EXPR = ('greatest(extract(epoch from ("complete_ts" - "start_ts")), 0) '
            '/ 3600.0')
_NOTNULL = '"start_ts" IS NOT NULL AND "complete_ts" IS NOT NULL'

_PG_RESULT = json.dumps(
    {"columns": ["p", "p", "c"], "rows": [[61.8, 121.4, 2251968]], "row_count": 1})
_ATHENA_RESULT = {"columns": ["p", "p", "c"],
                  "rows": [[61.8, 121.4, 2251968]], "row_count": 1}


class _Auth(AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-a"


def _metric(**over) -> DirectMetric:
    base = dict(id="cycle-tp", concept=CONCEPT, aggregation=Aggregation.COUNT,
                label="Cycle Time", unit="hours",
                measure=_MEASURE, percentiles=(0.9, 0.99))
    base.update(over)
    return DirectMetric(**base)


def _wire(monkeypatch, *, metric=None, bound=None, conn_type="PostgreSQL"):
    metric = metric or _metric()
    registry = {metric.id: metric}
    bound = _BOUND if bound is None else bound
    captured = {"sql": []}

    def _neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._FIELD_TYPE_CYPHER:  # measure columns are temporal
            return [{"data_type": "timestamp(6) with time zone"}]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER):
            table = (params or {}).get("table")
            pairs = sorted({(r["table"], r["column"]) for r in bound.get(concept, [])
                            if table is None or r["table"] == table})
            return [{"table": t, "column": c} for t, c in pairs][
                :mb.BINDING_CANDIDATE_READ_LIMIT]
        if cypher == mr._PREDICATE_ELSEWHERE_CYPHER:
            return []
        return []

    def _pg(schema, sql, params=None):
        captured["sql"].append(sql)
        return _PG_RESULT

    class _Layer:
        def get_metric(self, mid):
            return registry[mid]

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(mr, "_athena_time_window_where", lambda *a, **k: None)
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, t, query=None: "complete_ts")
    return captured


def _resolve(mid="cycle-tp"):
    return resolve_metric(mid, authority=_Auth())


# --- SQL byte-pins ---------------------------------------------------------- #

def test_postgres_percentile_sql(monkeypatch):
    captured = _wire(monkeypatch)
    rm = _resolve()
    assert captured["sql"] == [
        f"SELECT percentile_disc(0.9) WITHIN GROUP (ORDER BY {_PG_EXPR}), "
        f"percentile_disc(0.99) WITHIN GROUP (ORDER BY {_PG_EXPR}), "
        f"COUNT({_PG_EXPR}) FROM plant.{TABLE} WHERE {_NOTNULL}"
    ]
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))
    assert rm.sample_count == 2251968
    assert rm.value is None and rm.status is None


def test_athena_percentile_sql(monkeypatch):
    captured = _wire(monkeypatch, conn_type="Athena")
    monkeypatch.setattr(mr, "_execute_athena",
                        lambda plan, sql: (captured["sql"].append(sql) or _ATHENA_RESULT))
    rm = _resolve()
    assert captured["sql"] == [
        f"SELECT approx_percentile({_ATHENA_EXPR}, 0.9), "
        f"approx_percentile({_ATHENA_EXPR}, 0.99), "
        f"COUNT({_ATHENA_EXPR}) FROM plant.{TABLE} WHERE {_NOTNULL}"
    ]
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))


def test_filter_where_on_pinned_columns(monkeypatch):
    metric = _metric(filter=(Predicate(STATE, Op.EQ, ("DONE",)),))
    captured = _wire(monkeypatch, metric=metric)
    _resolve()
    assert captured["sql"] == [
        f"SELECT percentile_disc(0.9) WITHIN GROUP (ORDER BY {_PG_EXPR}), "
        f"percentile_disc(0.99) WITHIN GROUP (ORDER BY {_PG_EXPR}), "
        f"COUNT({_PG_EXPR}) FROM plant.{TABLE} "
        f"WHERE {_NOTNULL} AND \"state_code\" = 'DONE'"
    ]


def test_window_applies_to_the_end_column(monkeypatch):
    metric = _metric(window="24h")
    captured = _wire(monkeypatch, metric=metric)
    _resolve()
    sql = captured["sql"][0]
    # the window predicate targets the END column of the measure, not a
    # generic event-time mapping
    assert "\"complete_ts\" >= (now() - INTERVAL '24 hours')" in sql
    assert sql.count("complete_ts >= ") == 0  # quoted form only


# --- field-ref binding fail-closed ------------------------------------------ #

def test_unmapped_start_column_withholds_naming_the_field(monkeypatch):
    # start_ts does not MAPS_TO event-time on the measure table
    bound = {**_BOUND, TIME: [{"table": TABLE, "column": "complete_ts"}]}
    captured = _wire(monkeypatch, bound=bound)
    rm = _resolve()
    assert rm.value is None and rm.percentiles is None
    assert rm.status == UNRESOLVED_PREDICATE_STATUS
    assert "start_ts" in rm.detail
    assert captured["sql"] == []


def test_column_not_on_measure_table_withholds(monkeypatch):
    bound = {**_BOUND, TIME: [{"table": TABLE, "column": "start_ts"},
                              {"table": "other", "column": "complete_ts"}]}
    captured = _wire(monkeypatch, bound=bound)
    rm = _resolve()
    assert rm.status == UNRESOLVED_PREDICATE_STATUS
    assert "complete_ts" in rm.detail
    assert captured["sql"] == []


# --- lineage ---------------------------------------------------------------- #

def test_lineage_states_null_exclusion_and_percentiles(monkeypatch):
    _wire(monkeypatch)
    rm = _resolve()
    payload = mr._lineage_to_dict(rm.lineage)
    assert payload["measure"]["unit"] == "hours"
    assert payload["measure"]["clamp_negative"] is True
    assert payload["measure"]["excludes_null"] is True
    assert payload["percentiles"] == [0.9, 0.99]
    json.dumps(payload)


# --- a non-measure metric stays byte-identical ------------------------------ #

def test_plain_metric_lineage_has_no_measure_keys(monkeypatch):
    plain = DirectMetric(id="plain", concept=CONCEPT, aggregation=Aggregation.SUM,
                         label="Plain", unit="u")
    _wire(monkeypatch, metric=plain, bound={CONCEPT: _BOUND[CONCEPT]})
    rm = _resolve("plain")
    payload = mr._lineage_to_dict(rm.lineage)
    assert "measure" not in payload and "percentiles" not in payload


# --- offline end-to-end fixture (approx-free percentile_disc) --------------- #

def _percentile_disc(sorted_values, p):
    """PostgreSQL ``percentile_disc`` (nearest-rank, discrete): the first
    value whose cumulative share >= p, over the ascending values."""
    n = len(sorted_values)
    for i, v in enumerate(sorted_values):
        if (i + 1) / n >= p:
            return v
    return sorted_values[-1]


def test_end_to_end_percentile_disc_matches_hand_computed(monkeypatch):
    # Neutral rows: (start_ts epoch, end_ts epoch) seconds; durations in hours
    # clamp at 0. One NULL-end row is excluded.
    rows = [
        (0, 3600),            # 1 h
        (0, 7200),            # 2 h
        (0, 10800),           # 3 h
        (0, 18000),           # 5 h
        (0, 36000),           # 10 h
        (100, 50),            # negative -> clamped to 0 h
    ]
    durations = sorted(max(end - start, 0) / 3600.0 for start, end in rows)
    want = ((0.9, _percentile_disc(durations, 0.9)),
            (0.99, _percentile_disc(durations, 0.99)))

    def _pg(schema, sql, params=None):
        # Emulate percentile_disc + COUNT of the valid rows from the fixture.
        return json.dumps({
            "columns": ["p", "p", "c"],
            "rows": [[want[0][1], want[1][1], len(durations)]],
            "row_count": 1})

    _wire(monkeypatch)
    monkeypatch.setattr(mr, "query_pg", _pg)
    rm = _resolve()
    assert rm.percentiles == want
    assert rm.sample_count == len(durations)
