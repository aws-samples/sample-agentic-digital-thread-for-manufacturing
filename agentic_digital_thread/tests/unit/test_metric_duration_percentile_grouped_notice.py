# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Grouped results of the duration-PERCENTILE metric, through to the notice.

One or more test per behaviour:

* the grouped, day-windowed turn result reaches the rendered notice with
      EVERY group, each group's sample count, an OVERALL row, the exact applied
      ``[start, end)`` day boundary, and a grouped (not "deployment-wide
      scalar"/"lifetime") scope;
* an Athena grouped result is labelled approximate end to end (real
      resolver -> render); PostgreSQL is not;
* grouped DATE operands are cast and absolute-window literals match the
      bound column's physical type (zoned/unzoned) on both dialects;
* an unrecorded (unprovable) measure type withholds, not proceeds.

Offline. Neutral names throughout.
"""
from __future__ import annotations

import json

import pytest

import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_measure_rule as mmr
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
# Both measure columns are timestamp(6) with time zone (the live deployment shape).
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


def _wire(monkeypatch, *, metric=None, conn_type="PostgreSQL", grouped_result=None,
          scalar_result=None, types=None):
    metric = metric or _metric()
    registry = {metric.id: metric}
    types = _TYPES if types is None else types
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
            col = (params or {}).get("column")
            dt = types.get(col)
            return [{"data_type": dt}] if dt is not None else []
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
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, t, query=None: "start_ts")
    return captured


_WIN = None  # set per test


def _win():
    return mr.AbsoluteWindow(start_utc="2026-08-20T00:00:00+00:00",
                             end_utc="2026-08-21T00:00:00+00:00")


# --------------------------------------------------------------------------- #
# grouped day result reaches the notice: all groups, overall, boundary
# --------------------------------------------------------------------------- #
def test_h1_classify_grouped_carries_all_groups_and_window(monkeypatch):
    _wire(monkeypatch)
    grm = md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth(),
                                         dimension_column="group_code",
                                         turn_window=_win())
    result = mtn._classify_grouped(grm)
    assert result["outcome"] == "computed"
    # every group survives classification, not just the first
    groups = {g["group"]: g for g in result["grouped"]}
    assert set(groups) == {"alpha", "beta"}
    assert groups["beta"]["sample_count"] == 50
    # the applied absolute window travels through classification
    rng = result.get("window_applied_range")
    assert rng and rng["start"].startswith("2026-08-20")
    assert rng["end"].startswith("2026-08-21")


def test_h1_end_to_end_notice_has_all_groups_overall_and_boundary(monkeypatch):
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")  # opt in over the lane default
    _wire(monkeypatch)
    # the turn path uses the DEFAULT graph authority; point it at the fake.
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth())
    # Force _turn_window to the named-site day window (one window).
    monkeypatch.setattr(mtn, "_turn_window", lambda q: _win())
    # One defined-metric match for the question.
    monkeypatch.setattr(
        mtn, "_matches_for_turn",
        lambda q: ([type("M", (), {"ambiguous": False, "metric_ids": ["cycle-tp"],
                                   "term": "cycle time", "label": "Cycle Time"})()],
                   None))
    results = mtn.results_for_turn("TP90 cycle time by group on 8/20")
    notice = mtr_r.render_metric_layer_notice(results)
    assert "alpha" in notice and "beta" in notice          # every group named
    assert "100" in notice and "50" in notice              # per-group samples
    assert "overall" in notice.lower()                     # the overall row
    assert "2026-08-20" in notice and "2026-08-21" in notice  # exact boundary
    assert "lifetime (no window declared)" not in notice
    assert "over all recorded data" not in notice  # not the lifetime closing sentence
    assert "deployment-wide scalar" not in notice


# --------------------------------------------------------------------------- #
# Athena grouped approximation end to end (real resolver -> render)
# --------------------------------------------------------------------------- #
def test_h2_athena_grouped_result_renders_approximate(monkeypatch):
    captured = _wire(monkeypatch, conn_type="Athena")
    monkeypatch.setattr(mr, "_execute_athena", lambda plan, sql: (
        captured["sql"].append(sql) or (json.loads(
            '{"columns":["g","p","p","c"],"rows":[["alpha",10.0,20.0,100]],'
            '"row_count":1}') if " GROUP BY " in sql else json.loads(
            '{"columns":["p","p","c"],"rows":[[18.0,36.0,150]],"row_count":1}'))))
    grm = md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth(),
                                         dimension_column="group_code")
    result = mtn._classify_grouped(grm)
    assert result.get("approximate") is True
    clean = mtr_r.sanitize_result({**result, "term": "cycle time",
                                   "metric_id": "cycle-tp", "label": "Cycle"})
    assert clean.get("approximate") is True
    notice = mtr_r.render_metric_layer_notice([clean])
    assert "approximate" in notice.lower()


def test_h2_postgres_grouped_result_is_not_approximate(monkeypatch):
    _wire(monkeypatch)
    grm = md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth(),
                                         dimension_column="group_code")
    result = mtn._classify_grouped(grm)
    assert result.get("approximate") is not True


# --------------------------------------------------------------------------- #
# grouped DATE cast + type-aware window literals
# --------------------------------------------------------------------------- #
def test_h3_grouped_date_columns_are_cast(monkeypatch):
    captured = _wire(monkeypatch, types={"start_ts": "date", "complete_ts": "date"})
    md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth(),
                                   dimension_column="group_code")
    sql = captured["sql"][0]
    # a DATE operand is cast to timestamp in the grouped duration expr
    assert "::timestamp" in sql


def test_h3_pg_zoned_window_literal_is_timestamptz(monkeypatch):
    # zoned measure end column ⇒ PostgreSQL TIMESTAMPTZ literal (offset honoured)
    where = mmr.absolute_window_where(
        "complete_ts", "2026-08-20T00:00:00+00:00", "2026-08-21T00:00:00+00:00",
        dialect="postgres", col_type="zoned")
    assert "TIMESTAMPTZ" in where


def test_h3_pg_unzoned_window_literal_is_plain_timestamp(monkeypatch):
    where = mmr.absolute_window_where(
        "complete_ts", "2026-08-20T00:00:00+00:00", "2026-08-21T00:00:00+00:00",
        dialect="postgres", col_type="unzoned")
    assert "TIMESTAMPTZ" not in where and "TIMESTAMP '" in where
    # the offset is dropped for a timestamp-without-time-zone literal
    assert "+00:00" not in where


def test_h3_athena_unzoned_window_literal_is_plain_timestamp(monkeypatch):
    where = mmr.absolute_window_where(
        "complete_ts", "2026-08-20T00:00:00+00:00", "2026-08-21T00:00:00+00:00",
        dialect="athena", col_type="unzoned")
    assert "from_iso8601_timestamp" not in where and "TIMESTAMP '" in where


def test_h3_athena_zoned_window_literal_uses_from_iso8601(monkeypatch):
    where = mmr.absolute_window_where(
        "complete_ts", "2026-08-20T00:00:00+00:00", "2026-08-21T00:00:00+00:00",
        dialect="athena", col_type="zoned")
    assert "from_iso8601_timestamp" in where


# --------------------------------------------------------------------------- #
# an unrecorded physical type withholds (fail-closed)
# --------------------------------------------------------------------------- #
def test_h4_unrecorded_type_withholds(monkeypatch):
    captured = _wire(monkeypatch, metric=_metric(group_by=None),
                     types={})  # no data_type recorded for either column
    rm = mr._resolve_direct("cycle-tp", _metric(group_by=None), _Auth())
    assert rm.value is None and rm.percentiles is None
    assert rm.status == mr.UNRESOLVED_PREDICATE_STATUS
    assert "start_ts" in (rm.detail or "") or "complete_ts" in (rm.detail or "")
    assert captured["sql"] == []
