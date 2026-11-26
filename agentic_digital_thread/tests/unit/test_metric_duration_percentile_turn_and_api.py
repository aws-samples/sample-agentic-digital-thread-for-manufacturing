# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The steward-defined duration-PERCENTILE metric on the turn and API paths.

One or more test per behaviour below; each test's docstring names what goes
wrong without it:

* the chat/turn path applies the turn's absolute window and the definition's
      ``group_by`` (today it resolves a lifetime scalar only);
* the public result boundaries (grouped API ``_grouped_ok`` /
      ``_grouped_sites_envelope``, scalar ``/resolve`` payload, dashboard card)
      carry the percentile values + sample count instead of dropping them;
* a narrowed Athena grouped-percentile query AND-extends its existing WHERE
      instead of emitting a second ``WHERE``;
* Athena ``approx_percentile`` is labelled approximate (PostgreSQL is not);
* the grouped window targets the measure's END column, not an arbitrary
      co-located event-time mapping;
* a duration-percentile plan on an unsupported backend fails closed (never
      runs the SiteWise count/aggregate path);
* filters and ``group_by`` accept the exact ``{concept, column}`` pin;
* start/end physical types are validated and ``clamp_negative`` must be true.

Offline -- graph/DB seams monkeypatched. Neutral names throughout (no domain
literals: the group dimension is a generic id, values are generic tokens).
"""
from __future__ import annotations

import json

import pytest

import agents.metric_binding as mb
import agents.metric_definition as mdf
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation, DirectMetric, MetricLayerError, Predicate, PredicateOp as Op,
)
from agents.metric_measure_rule import DurationMeasure, FieldRef

CONCEPT = "cycle-time"
TIME = "event-time"
STATE = "widget-state"
DIM = "widget-group"
TABLE = "segments"

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, "start_ts"),
    end=FieldRef(TIME, "complete_ts"), unit="hours", clamp_negative=True)

_BOUND = {
    CONCEPT: [{"table": TABLE, "column": "segment_id"}],
    TIME: [{"table": TABLE, "column": "start_ts"},
           {"table": TABLE, "column": "complete_ts"}],
    STATE: [{"table": TABLE, "column": "state_code"}],
    DIM: [{"table": TABLE, "column": "group_code"}],
}

_PG_EXPR = ('greatest(extract(epoch from ("complete_ts" - "start_ts")), 0) '
            '/ 3600.0')
_NOTNULL = '"start_ts" IS NOT NULL AND "complete_ts" IS NOT NULL'


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-a"


def _metric(**over) -> DirectMetric:
    base = dict(id="cycle-tp", concept=CONCEPT, aggregation=Aggregation.COUNT,
                label="Cycle Time", unit="hours",
                measure=_MEASURE, percentiles=(0.9, 0.99))
    base.update(over)
    return DirectMetric(**base)


def _wire(monkeypatch, *, metric=None, bound=None, conn_type="PostgreSQL",
          pg_result=None, columns="timestamp"):
    """Wire the offline resolver seams. ``columns`` sets the physical type the
    schema reports for the measure timestamp columns."""
    metric = metric or _metric()
    registry = {metric.id: metric}
    bound = _BOUND if bound is None else bound
    captured = {"sql": []}
    result = pg_result if pg_result is not None else json.dumps(
        {"columns": ["p", "p", "c"], "rows": [[61.8, 121.4, 7]], "row_count": 1})

    def _neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._FIELD_TYPE_CYPHER:  # measure columns are temporal
            return [{"data_type": "timestamp(6) with time zone"}]
        if cypher == md._DIM_MAPS_TO_CYPHER:
            table = (params or {}).get("table")
            rows = bound.get(concept, [])
            pairs = sorted({(r["table"], r["column"]) for r in rows
                            if table is None or r["table"] == table})
            return [{"table": t, "column": c} for t, c in pairs]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER):
            table = (params or {}).get("table")
            pairs = sorted({(r["table"], r["column"]) for r in bound.get(concept, [])
                            if table is None or r["table"] == table})
            return [{"table": t, "column": c} for t, c in pairs][
                :mb.BINDING_CANDIDATE_READ_LIMIT]
        return []

    def _pg(schema, sql, params=None):
        captured["sql"].append(sql)
        return result

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


# --------------------------------------------------------------------------- #
# the turn path applies the absolute window and the definition's group_by
# --------------------------------------------------------------------------- #
def test_f1_absolute_window_bounds_the_end_column(monkeypatch):
    """An absolute ``[start, end)`` turn window must bound the measure's END
    column half-open (without it: resolve_metric has no absolute-window
    parameter)."""
    captured = _wire(monkeypatch)
    win = mr.AbsoluteWindow(start_utc="2026-08-20T00:00:00Z",
                            end_utc="2026-08-21T00:00:00Z")
    mr.resolve_metric("cycle-tp", authority=_Auth(), turn_window=win)
    sql = captured["sql"][0]
    assert '"complete_ts" >= ' in sql and '"complete_ts" < ' in sql
    assert "2026-08-20" in sql and "2026-08-21" in sql


def test_f1_group_by_definition_yields_grouped_result(monkeypatch):
    """A definition with ``group_by`` resolved through the turn path yields one
    row per group with percentiles (without it: no group_by field, and the
    turn path only ever calls the scalar resolver)."""
    metric = _metric(group_by=FieldRef(DIM, "group_code"))
    _wire(monkeypatch, metric=metric, pg_result=json.dumps({
        "columns": ["g", "p", "p", "c"],
        "rows": [["alpha", 10.0, 20.0, 100], ["beta", 30.0, 40.0, 50]],
        "row_count": 2}))
    results = mr.resolve_turn_metric("cycle-tp", authority=_Auth())
    # the turn resolver returns the grouped shape for a group_by metric
    assert results.rows and len(results.rows) == 2
    by_group = {r.dimension_value: r for r in results.rows}
    assert by_group["alpha"].percentiles == ((0.9, 10.0), (0.99, 20.0))


# --------------------------------------------------------------------------- #
# the narrowed Athena grouped-percentile WHERE must AND-extend, not double
# --------------------------------------------------------------------------- #
def test_f3_athena_grouped_prune_and_extends_existing_where(monkeypatch):
    """The grouped percentile SQL already has ``WHERE <null guard>``; the prune
    must AND onto it, never emit a second ``WHERE`` (without it: the double
    WHERE is syntactically invalid)."""
    plan = md.GroupedQueryPlan(
        system_id="sys-a", schema="plant",
        measure_table=TABLE, measure_column="segment_id",
        aggregation=Aggregation.COUNT,
        dimension_table=TABLE, dimension_column="group_code",
        measure=_MEASURE, percentiles=(0.9, 0.99))
    base_sql = md._compose_measure_grouped_sql(
        plan, "plant", TABLE, TABLE, '"group_code"')
    monkeypatch.setattr(
        mr, "_athena_recent_partition_where",
        lambda sid, table, qualifier="": f'{qualifier}"event_day" >= DATE \'2020-01-01\'')
    pruned = md._prune_athena_grouped_sql(plan, base_sql)
    assert pruned.count(" WHERE ") == 1
    assert " AND " in pruned.split(" GROUP BY ")[0]
    # the null guard and the prune predicate are both present, AND-joined
    assert _NOTNULL in pruned
    assert '"event_day" >= DATE \'2020-01-01\'' in pruned


# --------------------------------------------------------------------------- #
# public boundaries carry the percentile values + sample count
# --------------------------------------------------------------------------- #
def test_f2_grouped_ok_envelope_carries_percentiles_and_count(monkeypatch):
    """``_grouped_ok`` must emit each group's percentile pairs + sample count,
    not ``[dimension_value, None]`` (without it: percentile data dropped)."""
    _wire(monkeypatch, pg_result=json.dumps({
        "columns": ["g", "p", "p", "c"],
        "rows": [["alpha", 10.0, 20.0, 100], ["beta", 30.0, 40.0, 50]],
        "row_count": 2}))
    grm = md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth())
    env = mr._grouped_ok(DIM, grm)
    rows = {r[0]: r for r in env["rows"]}
    # the row must carry the (p, value) pairs, not a null measure cell
    assert rows["alpha"][1] is not None
    payload = json.dumps(env)  # must be serialisable
    assert "10.0" in payload and "20.0" in payload
    # the sample count survives to the envelope
    assert "100" in payload


def test_f2_scalar_resolve_payload_carries_percentiles(monkeypatch):
    """``_resolve_metric_payload`` must carry the (p, value) pairs + sample
    count (without it: the scalar payload omits them)."""
    _wire(monkeypatch)
    rm = mr._resolve_direct("cycle-tp", _metric(), _Auth())
    body = type("B", (), {"metric_id": "cycle-tp"})()
    monkeypatch.setattr(mr, "resolve_metric", lambda mid: rm)
    payload = mr._resolve_metric_payload(body)
    assert payload.get("percentiles"), "scalar payload must carry percentiles"
    text = json.dumps(payload)
    assert "61.8" in text and "121.4" in text


def test_f2_dashboard_card_is_not_no_data_for_a_percentile_result(monkeypatch):
    """A successful percentile result (``rm.value is None``) must NOT be
    classified ``no-data`` by the dashboard card."""
    from agents import dashboard
    _wire(monkeypatch)
    rm = mr._resolve_direct("cycle-tp", _metric(), _Auth())
    entry = dashboard.card_entry_from_resolved(rm)
    assert entry["status"] != "no-data"
    assert entry.get("percentiles")


# --------------------------------------------------------------------------- #
# Athena approx_percentile is labelled approximate; PostgreSQL is not
# --------------------------------------------------------------------------- #
def test_f4_athena_result_is_marked_approximate(monkeypatch):
    _wire(monkeypatch, conn_type="Athena")
    monkeypatch.setattr(mr, "_execute_athena", lambda plan, sql: {
        "columns": ["p", "p", "c"], "rows": [[61.8, 121.4, 7]], "row_count": 1})
    rm = mr._resolve_direct("cycle-tp", _metric(), _Auth())
    payload = mr._lineage_to_dict(rm.lineage)
    assert payload.get("approximate") is True


def test_f4_postgres_result_is_not_marked_approximate(monkeypatch):
    _wire(monkeypatch)
    rm = mr._resolve_direct("cycle-tp", _metric(), _Auth())
    payload = mr._lineage_to_dict(rm.lineage)
    assert payload.get("approximate") is not True


def test_f4_turn_block_says_approximate_only_for_athena(monkeypatch):
    """The computed percentiles line must say 'approximate' for an Athena
    percentile result (without it: no dialect approximation reaches the
    renderer; 'approximate' appears only in unrelated boilerplate)."""
    from agents import metric_turn_render as mtr_r
    pct = [{"p": 0.9, "value": 61.8}, {"p": 0.99, "value": 121.4}]
    athena = {"outcome": "computed", "value": 121.4, "unit": "hours",
              "system_id": "sys-a", "percentiles": pct, "approximate": True,
              "kind": "direct", "shape": "direct", "lineage": "x",
              "term": "cycle time", "metric_id": "cycle-tp", "label": "Cycle"}
    pg = {**athena, "approximate": False}
    athena_line = mtr_r._render_computed(athena, "- head")
    pg_line = mtr_r._render_computed(pg, "- head")
    # the percentiles line itself must carry the approximate label on Athena
    assert "approximate" in athena_line.split("percentiles:")[1].split(" | ")[0].lower()
    assert "approximate" not in pg_line.split("percentiles:")[1].split(" | ")[0].lower()


# --------------------------------------------------------------------------- #
# the grouped window targets the measure's END column
# --------------------------------------------------------------------------- #
def test_f5_grouped_window_uses_measure_end_column(monkeypatch):
    """With a fixed metric window, the grouped percentile SQL must bound on the
    measure's END column (``complete_ts``), not ``_resolve_order_column``'s
    first event-time mapping (``start_ts``)."""
    metric = _metric(window="24h")
    captured = _wire(monkeypatch, metric=metric, pg_result=json.dumps({
        "columns": ["g", "p", "p", "c"],
        "rows": [["alpha", 10.0, 20.0, 100]], "row_count": 1}))
    # _resolve_order_column returns start_ts (adversarial order); the window
    # must still land on the measure's end column complete_ts.
    md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth())
    sql = captured["sql"][0]
    assert "\"complete_ts\" >= (now() - INTERVAL '24 hours')" in sql
    assert "\"start_ts\" >= (now()" not in sql


# --------------------------------------------------------------------------- #
# a duration-percentile plan on an unsupported backend fails closed
# --------------------------------------------------------------------------- #
def test_f6_sitewise_scalar_withholds_structural(monkeypatch):
    """A duration-percentile metric on SiteWise (Direct) must be a structural
    withhold, never the SiteWise count/aggregate (without it: the SiteWise
    branch runs before the measure check and returns 999.0)."""
    _wire(monkeypatch, conn_type="SiteWise (Direct)")
    monkeypatch.setattr(mr, "_execute_sitewise", lambda plan: (999.0, 123))
    auth = _Auth()
    _mapping, plan = mr._direct_plan(
        _metric(), mr._resolve_authority(CONCEPT, auth))
    with pytest.raises(mr.StructuralMetricError):
        mr._execute_direct_plan(plan, None, _metric().filter)


def test_f6_sitewise_grouped_withholds_structural(monkeypatch):
    metric = _metric()
    _wire(monkeypatch, metric=metric, conn_type="SiteWise (Direct)")
    monkeypatch.setattr(md, "_execute_sitewise_grouped",
                        lambda plan: [("alpha", 1.0), ("beta", 2.0)])
    with pytest.raises(mr.StructuralMetricError):
        md.resolve_metric_by_dimension("cycle-tp", DIM, authority=_Auth())


# --------------------------------------------------------------------------- #
# filters and group_by accept the {concept, column} pin
# --------------------------------------------------------------------------- #
def test_f7_filter_column_pin_parses_and_binds(monkeypatch):
    """A filter predicate may pin ``{concept, column}``; the pinned column is
    used verbatim (without it: the predicate schema rejects ``column``)."""
    doc = {
        "id": "cycle-tp", "kind": "direct", "label": "Cycle", "unit": "hours",
        "concept": CONCEPT, "aggregation": "count",
        "measure": {"kind": "duration",
                    "start": {"concept": TIME, "column": "start_ts"},
                    "end": {"concept": TIME, "column": "complete_ts"},
                    "unit": "hours", "clamp_negative": True},
        "percentiles": [0.9, 0.99],
        "filter": [{"concept": STATE, "column": "state_code",
                    "op": "eq", "value": "DONE"}],
    }
    # must normalize without raising (column pin is accepted)
    norm = mdf._normalize(doc)
    pinned = norm["filter"][0].get("column")
    assert pinned == "state_code"


def test_f7_group_by_field_ref_on_definition(monkeypatch):
    """The definition may carry ``group_by: {concept, column}`` (without it:
    the field is unknown and rejected)."""
    doc = {
        "id": "cycle-tp", "kind": "direct", "label": "Cycle", "unit": "hours",
        "concept": CONCEPT, "aggregation": "count",
        "measure": {"kind": "duration",
                    "start": {"concept": TIME, "column": "start_ts"},
                    "end": {"concept": TIME, "column": "complete_ts"},
                    "unit": "hours", "clamp_negative": True},
        "percentiles": [0.9, 0.99],
        "group_by": {"concept": DIM, "column": "group_code"},
    }
    metric = mdf.to_metric(doc)
    assert metric.group_by is not None
    assert metric.group_by.column == "group_code"


# --------------------------------------------------------------------------- #
# timestamp type validation + mandatory clamp
# --------------------------------------------------------------------------- #
def test_f8_clamp_negative_false_is_rejected(monkeypatch):
    """``clamp_negative: false`` is rejected at definition time for this measure
    kind (without it: false is accepted)."""
    doc = {
        "id": "cycle-tp", "kind": "direct", "label": "Cycle", "unit": "hours",
        "concept": CONCEPT, "aggregation": "count",
        "measure": {"kind": "duration",
                    "start": {"concept": TIME, "column": "start_ts"},
                    "end": {"concept": TIME, "column": "complete_ts"},
                    "unit": "hours", "clamp_negative": False},
        "percentiles": [0.9, 0.99],
    }
    with pytest.raises(mdf.MetricDefinitionError):
        mdf.validate_definition(
            doc, known_concepts=frozenset({CONCEPT, TIME}),
            known_metric_ids=frozenset(), code_metric_ids=frozenset())


def test_f8_string_timestamp_column_withholds(monkeypatch):
    """A measure column whose physical type is a string (not a timestamp/date)
    withholds at resolve time (without it: no type check -- fails only at
    execution)."""
    captured = _wire(monkeypatch)

    def _type(system_id, table, column):
        return "varchar" if column == "start_ts" else "timestamp"

    monkeypatch.setattr(mr, "_measure_column_type", _type, raising=False)
    rm = mr._resolve_direct("cycle-tp", _metric(), _Auth())
    assert rm.value is None and rm.percentiles is None
    assert rm.status == mr.UNRESOLVED_PREDICATE_STATUS
    assert "start_ts" in (rm.detail or "")
    assert captured["sql"] == []
