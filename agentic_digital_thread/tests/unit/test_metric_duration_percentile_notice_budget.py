# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The duration-PERCENTILE notice wording, overall read budget and null keys.

One or more test per behaviour:

* the closing scope sentence of an Athena/grouped notice must not say "exactly"
  and must not say per-dimension breakdowns are unavailable (it just supplied
  them); it must keep the "approximate" instruction for Athena;
* the extra overall read runs inside the turn budget and trace, and the
  delivered query_count counts both the grouped and overall reads; when the
  budget is spent after the grouped read the overall read is skipped and the
  notice says so;
* a SQL NULL grouping key renders with a fixed label, not blank.

Offline. Neutral names throughout.
"""
from __future__ import annotations

import json

import pytest

import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_resolver as mr
import agents.metric_turn_notice as mtn
import agents.metric_turn_render as mtr_r
from agents.metric_layer import Aggregation, DirectMetric
from agents.metric_measure_rule import DurationMeasure, FieldRef

CONCEPT = "cycle-time"
TIME = "event-time"
DIM = "widget-group"
TABLE = "segments"

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, "start_ts"),
    end=FieldRef(TIME, "complete_ts"), unit="hours", clamp_negative=True)
_BOUND = {
    CONCEPT: [{"table": TABLE, "column": "segment_id"}],
    TIME: [{"table": TABLE, "column": "start_ts"},
           {"table": TABLE, "column": "complete_ts"}],
    DIM: [{"table": TABLE, "column": "group_code"}],
}
_TYPES = {"start_ts": "timestamp(6) with time zone",
          "complete_ts": "timestamp(6) with time zone"}


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-a"


def _metric(**over) -> DirectMetric:
    base = dict(id="cycle-tp", concept=CONCEPT, aggregation=Aggregation.COUNT,
                label="Cycle Time", unit="hours",
                measure=_MEASURE, percentiles=(0.9, 0.99),
                group_by=FieldRef(DIM, "group_code"))
    base.update(over)
    return DirectMetric(**base)


def _wire(monkeypatch, *, metric=None, conn_type="PostgreSQL",
          grouped_result=None, scalar_result=None):
    metric = metric or _metric()
    registry = {metric.id: metric}
    captured = {"sql": []}
    grouped_result = grouped_result if grouped_result is not None else json.dumps({
        "columns": ["g", "p", "p", "c"],
        "rows": [["alpha", 10.0, 20.0, 100], ["beta", 30.0, 40.0, 50]],
        "row_count": 2})
    scalar_result = scalar_result if scalar_result is not None else json.dumps({
        "columns": ["p", "p", "c"], "rows": [[18.0, 36.0, 150]], "row_count": 1})

    def _neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._FIELD_TYPE_CYPHER:
            return [{"data_type": "timestamp(6) with time zone"}]
        if cypher == md._DIM_MAPS_TO_CYPHER:
            table = (params or {}).get("table")
            rows = _BOUND.get(concept, [])
            pairs = sorted({(r["table"], r["column"]) for r in rows
                            if table is None or r["table"] == table})
            return [{"table": t, "column": c} for t, c in pairs]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER):
            table = (params or {}).get("table")
            pairs = sorted({(r["table"], r["column"]) for r in _BOUND.get(concept, [])
                            if table is None or r["table"] == table})
            return [{"table": t, "column": c} for t, c in pairs][
                :mb.BINDING_CANDIDATE_READ_LIMIT]
        return []

    def _pg(schema, sql, params=None):
        captured["sql"].append(sql)
        return grouped_result if " GROUP BY " in sql else scalar_result

    monkeypatch.setattr(mr, "get_metric_layer", lambda: type("L", (), {
        "get_metric": staticmethod(lambda mid: registry[mid])})())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(mr, "_athena_time_window_where", lambda *a, **k: None)
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, t, query=None: "start_ts")
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth())
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    return captured


def _win():
    return mr.AbsoluteWindow(start_utc="2026-08-20T00:00:00+00:00",
                             end_utc="2026-08-21T00:00:00+00:00")


def _match():
    return type("M", (), {"ambiguous": False, "metric_ids": ["cycle-tp"],
                          "term": "cycle time", "label": "Cycle Time"})()


def _notice_for(monkeypatch):
    mtn.invalidate()  # drop any cross-test cached turn result
    monkeypatch.setattr(mtn, "_turn_window", lambda q: _win())
    monkeypatch.setattr(mtn, "_matches_for_turn", lambda q: ([_match()], None))
    results = mtn.results_for_turn("TP90 cycle time by group on 8/20")
    return mtr_r.render_metric_layer_notice(results), results


# --------------------------------------------------------------------------- #
# the closing scope sentence must not contradict an approximate grouped block
# --------------------------------------------------------------------------- #
def test_athena_grouped_notice_never_says_exactly_or_unavailable(monkeypatch):
    captured = _wire(monkeypatch, conn_type="Athena")

    def _athena(plan, sql):
        captured["sql"].append(sql)
        return (json.loads('{"columns":["g","p","p","c"],'
                           '"rows":[["alpha",10.0,20.0,100],["beta",30.0,40.0,50]],'
                           '"row_count":2}') if " GROUP BY " in sql
                else json.loads('{"columns":["p","p","c"],'
                                '"rows":[[18.0,36.0,150]],"row_count":1}'))

    monkeypatch.setattr(mr, "_execute_athena", _athena)
    monkeypatch.setattr(md, "_athena_grouped_result",
                        lambda plan, sql: _athena(plan, sql))
    notice, _ = _notice_for(monkeypatch)
    low = notice.lower()
    assert "approximate" in low
    assert "exactly" not in low
    assert "not available from it this turn" not in low
    assert "breakdowns are not available" not in low


def test_athena_scalar_notice_never_says_exactly(monkeypatch):
    _wire(monkeypatch, conn_type="Athena", metric=_metric(group_by=None))
    monkeypatch.setattr(mr, "_execute_athena", lambda plan, sql: json.loads(
        '{"columns":["p","p","c"],"rows":[[18.0,36.0,150]],"row_count":1}'))
    notice, _ = _notice_for(monkeypatch)
    low = notice.lower()
    assert "approximate" in low and "exactly" not in low


def test_postgres_grouped_notice_has_no_approximate_and_no_unavailable(monkeypatch):
    _wire(monkeypatch)  # PostgreSQL (exact)
    notice, _ = _notice_for(monkeypatch)
    low = notice.lower()
    # the per-group line is exact (no approximate label) on PostgreSQL
    assert "approximate: athena" not in low
    # the closing scope sentence neither denies the breakdown nor the window
    assert "breakdowns are not available" not in low
    assert "not available from it this turn" not in notice.split("COMPUTED.")[-1].lower()


# --------------------------------------------------------------------------- #
# the overall read is budgeted and both reads are counted
# --------------------------------------------------------------------------- #
def test_grouped_turn_counts_both_reads(monkeypatch):
    _wire(monkeypatch)
    result = mtn._resolve_one("cycle-tp", turn_window=_win())
    assert result["outcome"] == "computed"
    assert result["query_count"] == 2  # grouped + overall both recorded


def test_overall_read_skipped_when_budget_spent(monkeypatch):
    captured = _wire(monkeypatch)
    import time
    past = time.monotonic() - 1.0  # a deadline already in the past
    result = mtn._resolve_one("cycle-tp", deadline=past, turn_window=_win())
    assert result["outcome"] == "computed"
    # the grouped read ran; the overall read was skipped (budget spent)
    assert sum(1 for s in captured["sql"] if " GROUP BY " in s) == 1
    assert sum(1 for s in captured["sql"] if " GROUP BY " not in s) == 0
    assert result["overall"] == {"not_computed": True}


# --------------------------------------------------------------------------- #
# a SQL NULL grouping key renders with a fixed label
# --------------------------------------------------------------------------- #
def test_null_group_key_is_labelled(monkeypatch):
    _wire(monkeypatch, grouped_result=json.dumps({
        "columns": ["g", "p", "p", "c"],
        "rows": [[None, 2.0, 3.0, 3], ["alpha", 10.0, 20.0, 100]],
        "row_count": 2}))
    notice, _ = _notice_for(monkeypatch)
    assert "(no value):" in notice
    # the blank-label bug rendered "percentiles by group: : p90=..."
    assert "group: : " not in notice
