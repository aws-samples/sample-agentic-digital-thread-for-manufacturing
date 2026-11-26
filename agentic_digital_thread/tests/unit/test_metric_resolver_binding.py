# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0076 — the metric's OWN concept column through the ONE binding rule,
dotted physical columns on every rendering path, and the steward door.

Pins: a multi-bound measure concept WITHHOLDS as ``ambiguous_binding``
regardless of graph row order (value None, candidates NAMED, nothing
executed, ``query_plan`` None); a steward ``AuthorityRule.field`` selects a
candidate (basis ``authority_rule`` in ``lineage.mapping``); a stale field or
two field-carrying rules withhold; a dotted measure column is ONE quoted
identifier (never its tail); the grouped path surfaces the gap as the
existing ``uncovered`` envelope naming the candidates; derived metrics name
the withheld input; ``POST /api/metrics/set-authority`` is steward-gated,
offloaded, validates ``field`` against the candidates and reads back.
Offline; placeholder names only (``widget_*``).
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.metric_authority as ma
import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation, DerivedMetric, DirectMetric, Formula, FormulaOp, MetricRef, Scalar,
)
from agents.models import ResolveMetricByDimensionRequest, ResolveMetricRequest

CONCEPT = "widget-kind"
_THREE = [
    {"table": "widget_events", "column": "parent.kind_code", "role": "measure"},
    {"table": "widget_events", "column": "kind_code", "role": "identifier"},
    {"table": "widget_events", "column": "detail.trigger.kind_code"},
]
_PG = json.dumps({"columns": ["count", "count"], "rows": [[9, 9]], "row_count": 1})
_AMBIGUOUS_DETAIL = (
    "concept 'widget-kind' is bound to more than one column on system 'sys-a' "
    "(widget_events: 'detail.trigger.kind_code', 'kind_code', "
    "'parent.kind_code'); a steward must choose one via POST "
    "/api/metrics/set-authority {field}")


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-a"


def _wire(monkeypatch, rows, rules=None, extra_metrics=()):
    metric = DirectMetric(id="m-kind", concept=CONCEPT, aggregation=Aggregation.COUNT,
                          label="Widget Kinds", unit="widgets")
    registry = {m.id: m for m in (metric, *extra_metrics)}

    class _Layer:
        def get_metric(self, mid):
            return registry[mid]

    captured: dict = {"sql": [], "rule_reads": 0}

    def _neptune(cypher, params=None):
        if cypher == mr._BINDING_RULE_CYPHER:
            captured["rule_reads"] += 1
            return list(rules or [])
        assert cypher == mr._MEASURE_BINDING_CYPHER, cypher
        return list(rows)

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: "PostgreSQL")
    monkeypatch.setattr(mr, "query_pg",
                        lambda schema, sql, params=None: captured["sql"].append(sql) or _PG)
    return captured


def _resolve(mid="m-kind"):
    return mr.resolve_metric(mid, authority=_Auth())


# --- measure column: ONE rule ------------------------------------------------ #

@pytest.mark.parametrize("reverse", [False, True])
def test_multi_bound_measure_withholds_regardless_of_row_order(monkeypatch, reverse):
    rows = list(reversed(_THREE)) if reverse else _THREE
    captured = _wire(monkeypatch, rows)
    rm = _resolve()
    assert rm.status == mr.AMBIGUOUS_BINDING_STATUS == "ambiguous_binding"
    assert rm.value is None and rm.sample_count is None
    assert rm.detail == _AMBIGUOUS_DETAIL
    assert rm.query_plan is None and rm.system_id == "sys-a"
    assert rm.lineage.mapping is None and rm.lineage.sql is None
    assert rm.lineage.authority.system_id == "sys-a"
    assert captured["sql"] == [] and captured["rule_reads"] == 1
    json.dumps(mr._lineage_to_dict(rm.lineage))


def test_rule_picked_dotted_column_is_one_quoted_identifier(monkeypatch):
    captured = _wire(monkeypatch, _THREE,
                     rules=[{"table": "widget_events", "field": "parent.kind_code"}])
    rm = _resolve()
    assert captured["sql"] == [
        'SELECT COUNT("parent.kind_code"), COUNT("parent.kind_code") '
        "FROM plant.widget_events"]
    assert rm.status is None and rm.value == 9
    mapping = mr._lineage_to_dict(rm.lineage)["mapping"]
    assert mapping["column"] == "parent.kind_code"
    assert mapping["binding_basis"] == "authority_rule"
    assert mapping["role"] == "measure"


def test_single_dotted_binding_is_never_tail_split(monkeypatch):
    captured = _wire(monkeypatch, _THREE[2:])
    rm = _resolve()
    assert captured["sql"] == [
        'SELECT COUNT("detail.trigger.kind_code"), COUNT("detail.trigger.kind_code") '
        "FROM plant.widget_events"]
    assert rm.lineage.mapping.binding_basis == "single"
    assert captured["rule_reads"] == 0


def test_single_flat_binding_sql_is_byte_identical(monkeypatch):
    captured = _wire(monkeypatch, _THREE[1:2])
    _resolve()
    assert captured["sql"] == [
        "SELECT COUNT(kind_code), COUNT(kind_code) FROM plant.widget_events"]


def test_sentinel_page_withholds_even_with_a_matching_rule(monkeypatch):
    """Regression: 25 visible pairs + the sentinel — a tableless rule
    for a visible field (duplicated on a hidden table) never selects."""
    rows = [{"table": "widget_events", "column": f"c{i:02d}"}
            for i in range(mb.BINDING_CANDIDATE_CAP)]
    rows.append({"table": "widget_zz", "column": "c00"})
    captured = _wire(monkeypatch, rows, rules=[{"table": None, "field": "c00"}])
    rm = _resolve()
    assert rm.status == mr.AMBIGUOUS_BINDING_STATUS and rm.value is None
    assert "not exhaustive" in rm.detail and "widget_zz" not in rm.detail
    assert captured["sql"] == [] and captured["rule_reads"] == 0


def test_stale_rule_field_withholds_naming_it(monkeypatch):
    _wire(monkeypatch, _THREE, rules=[{"table": "widget_events", "field": "kind_cod"}])
    rm = _resolve()
    assert rm.status == mr.AMBIGUOUS_BINDING_STATUS
    assert "AuthorityRule field 'kind_cod'" in rm.detail


def test_two_field_rules_withhold(monkeypatch):
    _wire(monkeypatch, _THREE, rules=[
        {"table": "widget_events", "field": "kind_code"},
        {"table": "widget_events", "field": "parent.kind_code"}])
    rm = _resolve()
    assert rm.status == mr.AMBIGUOUS_BINDING_STATUS
    assert "more than one active AuthorityRule" in rm.detail


def test_hostile_bound_column_fails_closed_before_execution(monkeypatch):
    captured = _wire(monkeypatch, [{"table": "widget_events", "column": 'a."b'}])
    with pytest.raises(ValueError, match="Invalid identifier"):
        _resolve()
    assert captured["sql"] == []


def test_unbound_measure_is_still_a_coverage_gap(monkeypatch):
    _wire(monkeypatch, [])
    with pytest.raises(mr.CoverageGapError) as e:
        _resolve()
    assert not isinstance(e.value, mr.AmbiguousBindingError)


def test_derived_over_ambiguous_input_names_the_status(monkeypatch):
    derived = DerivedMetric(id="d-kind", label="Scaled", unit="widgets", formula=Formula(
        FormulaOp.SCALE, (MetricRef("m-kind"), Scalar(2.0))))
    _wire(monkeypatch, _THREE, extra_metrics=(derived,))
    with pytest.raises(mr.MetricResolverError) as e:
        _resolve("d-kind")
    assert "ambiguous_binding" in str(e.value) and CONCEPT in str(e.value)


def test_payload_route_surfaces_status_with_no_plan(monkeypatch):
    _wire(monkeypatch, _THREE)
    payload = mr._resolve_metric_payload(ResolveMetricRequest(metric_id="m-kind"))
    assert payload["status"] == "ambiguous_binding"
    assert payload["value"] is None and payload["query_plan"] is None
    assert "'parent.kind_code'" in payload["detail"]


# --- grouped path + dotted rendering ---------------------------------------- #

def test_grouped_path_surfaces_uncovered_naming_candidates(monkeypatch):
    _wire(monkeypatch, _THREE)
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: mr.AuthoritySource(
        system_id="sys-a", source="authority_rule"))
    with pytest.raises(mr.AmbiguousBindingError) as e:
        md.resolve_metric_by_dimension("m-kind", "widget-line")
    assert e.value.concept == CONCEPT and len(e.value.candidates) == 3
    out = mr._resolve_by_dimension_payload(ResolveMetricByDimensionRequest(
        metric_id="m-kind", dimension_concept="widget-line"))
    assert out["status"] == "uncovered"
    assert out["missing_concepts"] == [CONCEPT]
    assert "'parent.kind_code'" in json.dumps(out)


def _plan(**over):
    base = dict(system_id="sys-a", schema="plant", measure_table="widget_events",
                measure_column="parent.kind_code", dimension_table="widget_events",
                dimension_column="line.code", aggregation=Aggregation.COUNT,
                join=None, order_column=None)
    base.update(over)
    return md.GroupedQueryPlan(**base)


def test_grouped_sql_renders_dotted_columns_as_one_identifier():
    sql = md._compose_grouped_sql(_plan())
    assert sql == ('SELECT "line.code", COUNT("parent.kind_code") '
                   'FROM "plant"."widget_events" GROUP BY "line.code" '
                   'ORDER BY "line.code" LIMIT 200')
    latest = md._compose_grouped_sql(_plan(aggregation=Aggregation.LATEST,
                                           order_column="evt.ts"))
    assert 'ORDER BY "line.code", "evt.ts" DESC' in latest
    for text in (sql, latest):
        assert '"."' not in text.replace('"plant"."widget_events"', "")


def test_heatmap_sql_renders_dotted_columns_as_one_identifier():
    from agents.heatmap_resolver import HeatmapPlan, _compose_heatmap_sql
    sql = _compose_heatmap_sql(HeatmapPlan(
        system_id="sys-a", schema="plant", measure_table="widget_events",
        measure_column="parent.kind_code", aggregation=Aggregation.COUNT,
        dim_x_column="line.code", dim_y_column="shift"))
    assert sql == ('SELECT "line.code", "shift", COUNT("parent.kind_code") '
                   'FROM "plant"."widget_events" GROUP BY "line.code", "shift" '
                   'ORDER BY "line.code", "shift" LIMIT 200')


def test_window_predicates_render_dotted_columns_as_one_identifier():
    assert md._pg_window_predicate("24h", "evt.ts", qualifier="m.") == (
        "m.\"evt.ts\" >= (now() - INTERVAL '24 hours')")
    assert md._athena_window_predicate("evt.ts", ("24", "HOUR")) == (
        "\"evt.ts\" >= (current_timestamp - INTERVAL '24' HOUR)")
    with pytest.raises(ValueError):
        md._pg_window_predicate("24h", "evt..ts")


# --- the steward door: POST /api/metrics/set-authority ----------------------- #

class _Graph:
    """In-memory AuthorityRule store behind mr's graph seams."""

    def __init__(self, candidates, *, drop_readback=False):
        self.candidates, self.drop = candidates, drop_readback
        self.writes: list[tuple[str, dict]] = []

    def write(self, cypher, params=None):
        self.writes.append((cypher, params or {}))
        return True

    def query(self, cypher, params=None):
        if cypher == mr._MEASURE_BINDING_CYPHER:
            return list(self.candidates)
        assert cypher == ma._FIELD_READBACK
        fields = [p for c, p in self.writes if c == ma._FIELD_WRITE]
        if not fields or self.drop:
            return [{"field": None, "table": None}]
        return [{"field": fields[-1]["field"], "table": fields[-1]["table"]}]


def _client(monkeypatch, graph, is_admin=True):
    monkeypatch.setattr(mr, "neptune_write", graph.write)
    monkeypatch.setattr(mr, "neptune_query", graph.query)
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.include_router(mr.router)
    return TestClient(app)


def _post(client, **body):
    return client.post("/api/metrics/set-authority",
                       json={"concept": CONCEPT, "system_id": "sys-a", **body})


@pytest.mark.parametrize("is_admin", [False, None])
def test_non_steward_is_denied_before_any_graph_io(monkeypatch, is_admin):
    graph = _Graph(_THREE)
    client = _client(monkeypatch, graph, is_admin=is_admin)
    monkeypatch.setattr(mr, "neptune_query", lambda *a, **k: pytest.fail("read"))
    resp = _post(client, field="kind_code")
    assert resp.status_code == 403 and graph.writes == []


def test_without_field_the_response_and_writes_are_historical(monkeypatch):
    graph = _Graph(_THREE)
    resp = _post(_client(monkeypatch, graph))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "concept": CONCEPT, "system_id": "sys-a"}
    assert len(graph.writes) == 2 and "APPLIES_TO" in graph.writes[1][0]


def test_field_naming_a_candidate_is_written_and_read_back(monkeypatch):
    graph = _Graph(_THREE)
    resp = _post(_client(monkeypatch, graph), field="parent.kind_code")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "ok" and body["readback"] is True
    assert (body["field"], body["table"]) == ("parent.kind_code", "widget_events")
    assert [c["column"] for c in body["candidates"]] == [
        "detail.trigger.kind_code", "kind_code", "parent.kind_code"]
    cypher, params = graph.writes[2]
    assert cypher == ma._FIELD_WRITE and params["arid"] == f"ar-sys-a-{CONCEPT}"
    assert params["actor"] == "unknown"


@pytest.mark.parametrize("candidates, body, needle", [
    (_THREE, {"field": "kind_cod"}, "is not a binding of"),
    ([], {"field": "kind_code"}, "no MAPS_TO field"),
    (_THREE, {"field": "kind_code", "table": "widget_log"}, "on table 'widget_log'"),
    ([{"table": "a", "column": "kind_code"}, {"table": "b", "column": "kind_code"}],
     {"field": "kind_code"}, "pass 'table'"),
])
def test_invalid_field_is_400_and_writes_nothing(monkeypatch, candidates, body, needle):
    graph = _Graph(candidates)
    resp = _post(_client(monkeypatch, graph), **body)
    assert resp.status_code == 400 and needle in resp.json()["error"]
    assert len(resp.json()["candidates"]) == len(candidates)
    assert graph.writes == []


def test_sentinel_page_rejects_a_field_write(monkeypatch):
    """Regression: the visible page has ONE ``c00`` but the sentinel
    (a hidden same-field candidate on another table) makes it non-exhaustive."""
    cands = [{"table": "widget_events", "column": f"c{i:02d}"}
             for i in range(mb.BINDING_CANDIDATE_CAP)]
    cands.append({"table": "widget_zz", "column": "c00"})
    graph = _Graph(cands)
    resp = _post(_client(monkeypatch, graph), field="c00")
    assert resp.status_code == 400 and "not exhaustive" in resp.json()["error"]
    assert len(resp.json()["candidates"]) == mb.BINDING_CANDIDATE_CAP
    assert graph.writes == []


def test_table_disambiguates_a_field_on_two_tables(monkeypatch):
    graph = _Graph([{"table": "a", "column": "kind_code"},
                    {"table": "b", "column": "kind_code"}])
    resp = _post(_client(monkeypatch, graph), field="kind_code", table="b")
    assert resp.status_code == 200 and resp.json()["table"] == "b"


def test_readback_mismatch_reports_failed(monkeypatch):
    graph = _Graph(_THREE, drop_readback=True)
    body = _post(_client(monkeypatch, graph), field="kind_code").json()
    assert body["status"] == "failed" and body["readback"] is False


def test_route_is_steward_gated_and_offload_only():
    route = next(r for r in mr.router.routes if r.path.endswith("/set-authority"))
    assert any(d.call is mr.check_steward for d in route.dependant.dependencies)
    fn = ast.parse(inspect.getsource(mr.set_authority_route)).body[0]
    assert len(fn.body) == 2  # docstring + the ONE ``return await offload(...)``
    ret = fn.body[1]
    assert isinstance(ret, ast.Return) and isinstance(ret.value, ast.Await)
    assert ret.value.value.func.id == "offload"


def test_machine_paths_never_write_field():
    """Only ``metric_authority`` writes ``ar.field`` (ADR-0076 D2)."""
    import pathlib
    agents = pathlib.Path(mr.__file__).parent
    writers = sorted(p.name for p in agents.rglob("*.py")
                     if "ar.field = " in p.read_text(encoding="utf-8"))
    assert writers == ["metric_authority.py"]


# --- ADR-0075/0076 live finding: Athena systems may have an EMPTY catalog schema ---

def test_plan_schema_resolves_athena_database_from_connection_config(monkeypatch):
    """A direct metric on an Athena system whose catalog ``schema`` is '' must
    take the database from the connection config (the grouped path's rule),
    never hand '' to validate_identifier (escaped as a 500 on preview)."""
    import agents.metric_resolver as mr
    from agents import connection_types, entity_render
    monkeypatch.setattr(mr, "get_schema", lambda sid: "")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: connection_types.ATHENA)
    monkeypatch.setattr(entity_render, "_athena_schema_from_config", lambda sid: "widget_db")
    assert mr.plan_schema("widget-athena") == "widget_db"


def test_plan_schema_fails_loud_when_no_source_has_a_database(monkeypatch):
    import agents.metric_resolver as mr
    from agents import connection_types, entity_render
    monkeypatch.setattr(mr, "get_schema", lambda sid: "")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: connection_types.ATHENA)
    monkeypatch.setattr(entity_render, "_athena_schema_from_config", lambda sid: "")
    import pytest
    with pytest.raises(mr.MetricResolverError, match="no schema/database"):
        mr.plan_schema("widget-athena")


def test_plan_schema_keeps_catalog_schema_for_postgres(monkeypatch):
    import agents.metric_resolver as mr
    monkeypatch.setattr(mr, "get_schema", lambda sid: "widget_schema")
    assert mr.plan_schema("widget-pg") == "widget_schema"
