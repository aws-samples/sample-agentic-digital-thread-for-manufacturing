# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0075 — the SCALAR resolver for filtered DIRECT metrics.

Pins: predicate columns bind through the system's MAPS_TO on the MEASURE
table; the WHERE composes prune → window → predicate through the one joiner;
an unbound concept WITHHOLDS as ``unresolved_predicate`` (value None, concept
NAMED, NOTHING executed); 2+ bindings select through the steward's
``AuthorityRule.field`` or WITHHOLD as ``ambiguous_binding`` (ADR-0076); a
dotted physical column renders as ONE quoted identifier; a non-SQL backend is structural (never the
unfiltered count); a hostile hand-built literal fails before execution; a
denied predicate column is never pruned-and-retried; filter-less metrics stay
byte-identical. Offline — graph/DB seams monkeypatched on ``metric_resolver``.
"""
from __future__ import annotations

import json

import pytest

import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricRef,
    Predicate,
    PredicateOp as Op,
    Scalar,
)
from agents.metric_resolver import (
    AuthorityResolver,
    MetricResolverError,
    AMBIGUOUS_BINDING_STATUS,
    StructuralMetricError,
    UNRESOLVED_PREDICATE_STATUS,
    _lineage_to_dict,
    _resolve_metric_payload,
    resolve_metric,
)
from agents.models import ResolveMetricRequest

STATE = "widget-color"
TYPE = "widget-size"
_FILTER = (
    Predicate(STATE, Op.EQ, ("BLUE",)),
    Predicate(TYPE, Op.IN, ("LARGE",)),
)
_BOUND = {
    STATE: [{"table": "widget_events", "column": "color_code"}],
    TYPE: [{"table": "widget_events", "column": "size_code"}],
}
_PG_RESULT = json.dumps({"columns": ["count", "count"], "rows": [[5, 5]], "row_count": 1})
_BASE = "SELECT COUNT(widget_id), COUNT(widget_id) FROM plant.widget_events"
_PRED = "\"color_code\" = 'BLUE' AND \"size_code\" IN ('LARGE')"


def _graph(rows, cypher, params):
    """Emulate the two binding reads over a synthetic ``(table, column)`` list:
    exact SchemaEntry name, DISTINCT before ORDER BY / LIMIT (Cypher order)."""
    if cypher == mr._PREDICATE_BINDING_CYPHER:
        pairs = sorted({(r["table"], r["column"])
                        for r in rows if r["table"] == params["table"]})
        return [{"table": t, "column": c} for t, c in pairs][
            :mb.BINDING_CANDIDATE_READ_LIMIT]
    assert cypher == mr._PREDICATE_ELSEWHERE_CYPHER
    tables = sorted({r["table"] for r in rows if r["table"] != params["table"]})
    return [{"table": t} for t in tables][:1]


class _Auth(AuthorityResolver):
    def authoritative_system(self, concept: str) -> str | None:  # noqa: ARG002
        return "sys-a"


def _wire(monkeypatch, *, filter=_FILTER, bindings=None, window=None,
          conn_type="PostgreSQL", extra_metrics=(), rules=None,
          aggregation=Aggregation.COUNT):
    metric = DirectMetric(
        id="m-pred", concept="widget-event", aggregation=aggregation,
        label="Blue Large Widgets", unit="widgets", filter=filter,
        window=window,
    )
    registry = {m.id: m for m in (metric, *extra_metrics)}

    class _Layer:
        def get_metric(self, mid):
            return registry[mid]

    bound = _BOUND if bindings is None else bindings
    captured: dict = {"sql": [], "predicate_reads": [], "binding_params": [],
                      "rule_reads": []}

    def _neptune(cypher, params=None):
        if cypher == mr._BINDING_RULE_CYPHER:
            captured["rule_reads"].append(params["concept"])
            return (rules or {}).get(params["concept"], [])
        if cypher == mr._PREDICATE_BINDING_CYPHER:
            captured["predicate_reads"].append(params["concept"])
            captured["binding_params"].append(params)
        if cypher in (mr._PREDICATE_BINDING_CYPHER, mr._PREDICATE_ELSEWHERE_CYPHER):
            return _graph(bound.get(params["concept"], []), cypher, params)
        return [{"table": "widget_events", "column": "widget_id"}]

    def _pg(schema, sql, params=None):
        captured["sql"].append(sql)
        return _PG_RESULT

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *_a, **_k: None)
    monkeypatch.setattr(mr, "_athena_time_window_where", lambda *_a, **_k: None)
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: "event_ts")
    return captured


def _resolve(mid="m-pred"):
    return resolve_metric(mid, authority=_Auth())


# --- Bound: exact SQL + lineage --------------------------------------------- #

def test_bound_predicates_compose_exact_postgres_sql(monkeypatch):
    captured = _wire(monkeypatch)
    rm = _resolve()
    assert captured["sql"] == [f"{_BASE} WHERE {_PRED}"]
    assert rm.value == 5 and rm.status is None
    assert rm.lineage.sql == captured["sql"][0]
    assert captured["predicate_reads"] == [STATE, TYPE]


def test_bound_predicates_follow_the_window(monkeypatch):
    captured = _wire(monkeypatch, window="24h")
    _resolve()
    assert captured["sql"] == [
        f"{_BASE} WHERE \"event_ts\" >= (now() - INTERVAL '24 hours') AND {_PRED}"
    ]


def test_athena_order_is_prune_then_window_then_predicate(monkeypatch):
    # The up-front prune remains only for an aggregate the connector cannot
    # compute exactly from pieces (here AVG); COUNT/SUM/MIN/MAX run as defined.
    captured = _wire(monkeypatch, window="24h", conn_type="Athena",
                     aggregation=Aggregation.AVG)
    monkeypatch.setattr(mr, "_athena_recent_partition_where",
                        lambda sid, table, qualifier="": "\"dt\" >= '2026-07'")
    _resolve()
    base = _BASE.replace("SELECT COUNT(", "SELECT AVG(", 1)
    assert captured["sql"] == [
        f"{base} WHERE \"dt\" >= '2026-07' "
        "AND \"event_ts\" >= (current_timestamp - INTERVAL '24' HOUR) "
        f"AND {_PRED}"
    ]


def test_lineage_carries_bound_predicates(monkeypatch):
    _wire(monkeypatch)
    rm = _resolve()
    assert [(b.concept, b.column, b.op, b.values) for b in rm.lineage.predicates] == [
        (STATE, "color_code", Op.EQ, ("BLUE",)),
        (TYPE, "size_code", Op.IN, ("LARGE",)),
    ]
    payload = _lineage_to_dict(rm.lineage)
    assert payload["predicates"][0] == {
        "concept": STATE, "column": "color_code", "op": "eq", "values": ["BLUE"]}
    assert set(payload["query_plan"]) == {
        "system_id", "schema", "table", "column", "aggregation"}
    json.dumps(payload)


# --- Unbound: withhold, NAME the concept, execute nothing ------------------- #

def test_unbound_predicate_withholds_and_names_the_concept(monkeypatch):
    captured = _wire(monkeypatch, bindings={STATE: _BOUND[STATE]})

    def _no_prune(*_a, **_k):
        raise AssertionError("no prune discovery for an unbound predicate")

    monkeypatch.setattr(mr, "_athena_recent_partition_where", _no_prune)
    rm = _resolve()
    assert rm.status == UNRESOLVED_PREDICATE_STATUS == "unresolved_predicate"
    assert rm.value is None and rm.sample_count is None
    assert TYPE in rm.detail and "sys-a" in rm.detail and "widget_events" in rm.detail
    assert captured["sql"] == []
    assert rm.lineage.sql is None and rm.lineage.predicates is None
    assert "predicates" not in _lineage_to_dict(rm.lineage)


def test_bound_on_another_table_is_named_distinctly(monkeypatch):
    captured = _wire(monkeypatch, bindings={
        STATE: _BOUND[STATE], TYPE: [{"table": "widget_log", "column": "size"}]})
    rm = _resolve()
    assert rm.status == UNRESOLVED_PREDICATE_STATUS
    assert "'widget_log'" in rm.detail and "not the measure table" in rm.detail
    assert captured["sql"] == []


def test_duplicate_rows_for_one_column_collapse_and_compute(monkeypatch):
    dup = [{"table": "widget_events", "column": "color_code"},
           {"table": "widget_events", "column": "color_code"}]
    captured = _wire(monkeypatch, bindings={**_BOUND, STATE: dup})
    rm = _resolve()
    assert rm.status is None and rm.value == 5
    assert captured["sql"] == [f"{_BASE} WHERE {_PRED}"]


@pytest.mark.parametrize("reverse", [False, True])
def test_two_distinct_same_table_columns_withhold_regardless_of_order(
        monkeypatch, reverse):
    rows = [{"table": "widget_events", "column": "size_code"},
            {"table": "widget_events", "column": "alt_size_code"}]
    if reverse:
        rows = list(reversed(rows))
    captured = _wire(monkeypatch, bindings={**_BOUND, TYPE: rows})
    rm = _resolve()
    assert rm.status == AMBIGUOUS_BINDING_STATUS == "ambiguous_binding"
    assert rm.value is None and rm.sample_count is None
    assert rm.detail == (
        f"predicate concept {TYPE!r} is bound to more than one column on system "
        "'sys-a' (widget_events: 'alt_size_code', 'size_code'); a steward must "
        "choose one via POST /api/metrics/set-authority {field}")
    assert captured["sql"] == [] and captured["rule_reads"] == [TYPE]
    # the measure plan exists and rides in lineage with sql null (ADR-0076 D3)
    assert rm.query_plan is not None and rm.lineage.sql is None
    assert rm.lineage.mapping.column == "widget_id"


def test_steward_rule_field_selects_the_predicate_column(monkeypatch):
    rows = [{"table": "widget_events", "column": "size_code"},
            {"table": "widget_events", "column": "alt_size_code"}]
    captured = _wire(monkeypatch, bindings={**_BOUND, TYPE: rows}, rules={
        TYPE: [{"table": "widget_events", "field": "size_code"}]})
    rm = _resolve()
    assert rm.status is None and rm.value == 5
    assert captured["sql"] == [f"{_BASE} WHERE {_PRED}"]


def test_stale_rule_field_withholds_naming_it(monkeypatch):
    rows = [{"table": "widget_events", "column": "size_code"},
            {"table": "widget_events", "column": "alt_size_code"}]
    captured = _wire(monkeypatch, bindings={**_BOUND, TYPE: rows}, rules={
        TYPE: [{"table": "widget_events", "field": "gone_code"}]})
    rm = _resolve()
    assert rm.status == AMBIGUOUS_BINDING_STATUS
    assert "field 'gone_code'" in rm.detail and "names none" in rm.detail
    assert captured["sql"] == []


def test_single_binding_never_reads_rules(monkeypatch):
    captured = _wire(monkeypatch)
    _resolve()
    assert captured["rule_reads"] == []


def test_binding_read_targets_the_exact_measure_entry(monkeypatch):
    """The read is scoped to the measure SchemaEntry and names candidates
    BEFORE the cap (DISTINCT → ORDER BY → LIMIT, ADR-0076 D1)."""
    captured = _wire(monkeypatch)
    _resolve()
    assert mr._PREDICATE_BINDING_CYPHER is mb.PREDICATE_BINDING_CYPHER
    assert "(se:SchemaEntry {name: $table})" in mr._PREDICATE_BINDING_CYPHER
    assert "RETURN se.name AS table, f.name AS column, collect({" in (
        mr._PREDICATE_BINDING_CYPHER)
    assert mr._PREDICATE_BINDING_CYPHER.endswith(
        f"LIMIT {mb.BINDING_CANDIDATE_READ_LIMIT}")
    assert [p["table"] for p in captured["binding_params"]] == [
        "widget_events", "widget_events"]


def test_second_measure_binding_beyond_other_table_noise_withholds(monkeypatch):
    """49 other-table bindings + 2 measure-table bindings: the old unscoped
    ``LIMIT 50`` page could hold only one measure column and compute."""
    noise = [{"table": f"other_{i:02d}", "column": "size_code"} for i in range(49)]
    rows = noise + [{"table": "widget_events", "column": "size_code"},
                    {"table": "widget_events", "column": "zz_size_code"}]
    captured = _wire(monkeypatch, bindings={**_BOUND, TYPE: rows})
    rm = _resolve()
    assert rm.status == AMBIGUOUS_BINDING_STATUS and rm.value is None
    assert "bound to more than one column" in rm.detail
    assert "'size_code'" in rm.detail and "'zz_size_code'" in rm.detail
    assert "other_" not in rm.detail
    assert captured["sql"] == []


def test_same_tail_different_entry_is_not_the_measure_table(monkeypatch):
    captured = _wire(monkeypatch, bindings={
        **_BOUND, TYPE: [{"table": "archive.widget_events", "column": "size_code"}]})
    rm = _resolve()
    assert rm.status == UNRESOLVED_PREDICATE_STATUS
    assert "'archive.widget_events'" in rm.detail
    assert "not the measure table" in rm.detail
    assert captured["sql"] == []


def test_dotted_bound_field_renders_as_one_identifier(monkeypatch):
    """ADR-0076 D4 (inverts the ADR-0075 fail-closed pin): a dotted physical
    column is ONE quoted identifier — never tail-normalised, never split."""
    captured = _wire(monkeypatch, filter=(Predicate(STATE, Op.IS_NULL),), bindings={
        STATE: [{"table": "widget_events", "column": "payload.color_code"}]})
    rm = _resolve()
    assert captured["sql"] == [f'{_BASE} WHERE "payload.color_code" IS NULL']
    assert rm.lineage.predicates[0].column == "payload.color_code"
    assert '"payload"."color_code"' not in captured["sql"][0]


def test_payload_route_surfaces_status_and_detail(monkeypatch):
    _wire(monkeypatch, bindings={})
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth())
    payload = _resolve_metric_payload(ResolveMetricRequest(metric_id="m-pred"))
    assert payload["status"] == "unresolved_predicate"
    assert payload["value"] is None and STATE in payload["detail"]


def test_derived_over_a_withheld_input_names_the_concept(monkeypatch):
    derived = DerivedMetric(
        id="d-pred", label="Scaled", unit="widgets",
        formula=Formula(FormulaOp.SCALE, (MetricRef("m-pred"), Scalar(2.0))),
    )
    _wire(monkeypatch, bindings={}, extra_metrics=(derived,))
    with pytest.raises(MetricResolverError) as e:
        _resolve("d-pred")
    assert "unresolved_predicate" in str(e.value) and STATE in str(e.value)


# --- Never a proxy ------------------------------------------------------------- #

@pytest.mark.parametrize("conn_type", ["SiteWise Direct", "MCP Server"])
def test_non_sql_backend_is_structural(monkeypatch, conn_type):
    captured = _wire(monkeypatch, conn_type=conn_type)
    with pytest.raises(StructuralMetricError, match="SQL backends only"):
        _resolve()
    assert captured["sql"] == [] and captured["predicate_reads"] == []


def test_hostile_hand_built_literal_fails_before_execution(monkeypatch):
    captured = _wire(monkeypatch, filter=(Predicate(STATE, Op.EQ, ("bad'lit",)),))
    with pytest.raises(MetricResolverError, match="invalid metric predicate"):
        _resolve()
    assert captured["sql"] == []


def test_invalid_bound_column_identifier_fails_before_execution(monkeypatch):
    captured = _wire(monkeypatch, filter=(Predicate(STATE, Op.IS_NULL),),
                     bindings={STATE: [{"table": "widget_events", "column": "bad-col"}]})
    with pytest.raises(MetricResolverError, match="invalid metric predicate"):
        _resolve()
    assert captured["sql"] == []


def test_denied_predicate_column_is_never_pruned_and_retried(monkeypatch):
    _wire(monkeypatch, conn_type="Athena")
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid, *_a: {"connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {})
    calls: list[str] = []

    def _run(sql, config):
        calls.append(sql)
        raise RuntimeError(
            "COLUMN_NOT_FOUND: line 1:1: Column 'color_code' cannot be resolved")

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", _run)
    with pytest.raises(RuntimeError, match="color_code"):
        _resolve()
    assert len(calls) == 1 and _PRED in calls[0]


# --- Filter-less regression ------------------------------------------------------ #

def test_filter_less_metric_is_byte_identical(monkeypatch):
    captured = _wire(monkeypatch, filter=None)
    rm = _resolve()
    assert captured["sql"] == [_BASE]
    assert captured["predicate_reads"] == []
    assert rm.lineage.predicates is None and rm.status is None
    assert set(_lineage_to_dict(rm.lineage)) == {
        "metric_id", "kind", "concept", "authority", "mapping", "query_plan",
        "sql", "executed_at", "op", "inputs", "prefer_stored_hit",
    }
