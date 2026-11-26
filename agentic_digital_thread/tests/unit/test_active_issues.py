# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the S5.3 Wave 2 active-issues panel (Item 1, §6.2).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the default
(unmarked) pytest lane, mirroring ``test_metric_resolver.py`` / ``test_dashboard_cards.py``.

Coverage:
  * ``build_filtered_section_sql`` composes a validated, parameter-free ``WHERE`` from the
    CLOSED predicate vocabulary (exact SQL strings) and REJECTS injection on the column,
    table, operator, and value surfaces (``ValueError``).
  * Server-side deterministic severity (the three ``classify`` mappers).
  * ``compose_active_issues`` unions the three sources with a fake ``query_fn`` (correct
    items, severity, source_system, entity, lineage.sql) and fails LOUD on a DB error.
  * The ``/api/dashboard/active-issues`` route returns a friendly HTTP-200 envelope on a
    simulated composer/DB error (mirrors ``metric_resolver._grouped_error``) and inherits
    the app-wide ``AuthMiddleware``.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.active_issues as ai
import agents.dashboard as dashboard
from agents.active_issues import (
    ISSUE_SOURCES,
    MAX_ISSUES_PER_SOURCE,
    _classify_backordered,
    _classify_quality,
    _classify_work_order,
    _spec_for,
    compose_active_issues,
)
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.dashboard import dashboard_active_issues, router as dashboard_router
from agents.entity_render import (
    Predicate,
    PredicateOp,
    SectionSpec,
    _predicate_sql,
    build_filtered_section_sql,
)


# --- build_filtered_section_sql: exact composed SQL (closed vocabulary) ------

def _spec(table, *cols):
    return SectionSpec(table=table, entity="e", entity_label="E",
                       columns=[{"column": c, "label": c, "field_concept": None} for c in cols])


def test_filtered_sql_numeric_lt_exact():
    spec = _spec("quality_checks", "check_type", "pass_rate")
    sql = build_filtered_section_sql(
        "mes_milwaukee", spec, [Predicate("pass_rate", PredicateOp.LT, 90)])
    assert sql == (
        'SELECT "check_type","pass_rate" FROM "mes_milwaukee"."quality_checks" '
        'WHERE "pass_rate" < 90 LIMIT 200'
    )


def test_filtered_sql_in_enum_list_exact():
    spec = _spec("work_orders", "work_order_number", "status")
    sql = build_filtered_section_sql(
        "mes_detroit", spec,
        [Predicate("status", PredicateOp.IN, ("CANCELLED", "PAUSED"))], max_rows=50)
    assert sql == (
        'SELECT "work_order_number","status" FROM "mes_detroit"."work_orders" '
        'WHERE "status" IN (\'CANCELLED\',\'PAUSED\') LIMIT 50'
    )


def test_filtered_sql_eq_enum_string_exact():
    spec = _spec("customer_orders", "order_number", "status")
    sql = build_filtered_section_sql(
        "erp_data", spec, [Predicate("status", PredicateOp.EQ, "BACKORDERED")])
    assert sql == (
        'SELECT "order_number","status" FROM "erp_data"."customer_orders" '
        'WHERE "status" = \'BACKORDERED\' LIMIT 200'
    )


def test_filtered_sql_multiple_predicates_anded():
    spec = _spec("work_orders", "status", "good_quantity")
    sql = build_filtered_section_sql(
        "mes_milwaukee", spec,
        [Predicate("status", PredicateOp.EQ, "COMPLETED"),
         Predicate("good_quantity", PredicateOp.GE, 0)])
    assert ' WHERE "status" = \'COMPLETED\' AND "good_quantity" >= 0 LIMIT 200' in sql


def test_filtered_sql_no_predicates_is_plain_select():
    spec = _spec("customer_orders", "order_number")
    sql = build_filtered_section_sql("erp_data", spec, [])
    assert sql == 'SELECT "order_number" FROM "erp_data"."customer_orders" LIMIT 200'


# --- injection rejection (every identifier/value surface) --------------------

def test_filtered_sql_rejects_injection_column():
    spec = _spec("quality_checks", "pass_rate")
    with pytest.raises(ValueError):
        build_filtered_section_sql(
            "mes_milwaukee", spec,
            [Predicate("pass_rate; DROP TABLE x", PredicateOp.LT, 90)])


def test_filtered_sql_rejects_injection_select_column():
    spec = _spec("quality_checks", 'pass_rate" --')
    with pytest.raises(ValueError):
        build_filtered_section_sql(
            "mes_milwaukee", spec, [Predicate("pass_rate", PredicateOp.LT, 90)])


def test_filtered_sql_rejects_injection_table():
    spec = _spec("quality_checks); DROP", "pass_rate")
    with pytest.raises(ValueError):
        build_filtered_section_sql(
            "mes_milwaukee", spec, [Predicate("pass_rate", PredicateOp.LT, 90)])


def test_filtered_sql_rejects_injection_schema():
    spec = _spec("quality_checks", "pass_rate")
    with pytest.raises(ValueError):
        build_filtered_section_sql(
            "mes; DROP TABLE x", spec, [Predicate("pass_rate", PredicateOp.LT, 90)])


def test_predicate_rejects_non_allowlisted_string_value():
    # A would-be SQL-injection string value is NOT an uppercase enum token → rejected.
    with pytest.raises(ValueError):
        _predicate_sql(Predicate("status", PredicateOp.EQ, "BACK'; DROP TABLE x; --"))


def test_predicate_rejects_lowercase_string_value():
    # Closed allowlist is strict uppercase-enum; lowercase is rejected (no silent coercion).
    with pytest.raises(ValueError):
        _predicate_sql(Predicate("status", PredicateOp.EQ, "backordered"))


def test_predicate_rejects_boolean_value():
    with pytest.raises(ValueError):
        _predicate_sql(Predicate("flag", PredicateOp.EQ, True))


def test_predicate_in_rejects_injection_inside_list():
    with pytest.raises(ValueError):
        _predicate_sql(Predicate("status", PredicateOp.IN, ("CANCELLED", "x'; DROP")))


def test_predicate_in_empty_list_rejected():
    with pytest.raises(ValueError):
        _predicate_sql(Predicate("status", PredicateOp.IN, ()))


# --- server-side deterministic severity (the three classify mappers) ---------

def test_classify_quality_severity_thresholds():
    crit = _classify_quality({"check_type": "FILL_LEVEL", "pass_rate": 65.0,
                              "fail_count": 7, "sample_size": 20})
    assert crit["severity"] == "critical"
    assert "FILL_LEVEL" in crit["title"]
    assert "65.0%" in crit["detail"] and "7 of 20 failed" in crit["detail"]

    warn = _classify_quality({"check_type": "VISUAL", "pass_rate": 85.0})
    assert warn["severity"] == "warning"

    # None pass_rate is NOT < threshold → warning (no crash, no false-critical).
    unknown = _classify_quality({"pass_rate": None})
    assert unknown["severity"] == "warning"


def test_classify_work_order_severity_by_status():
    crit = _classify_work_order({"work_order_number": "WO-9", "status": "CANCELLED",
                                 "good_quantity": 100, "planned_quantity": 500, "line": "line1"})
    assert crit["severity"] == "critical"
    assert "WO-9" in crit["title"] and "cancelled" in crit["title"]
    assert "100/500 good units" in crit["detail"]

    warn = _classify_work_order({"work_order_number": "WO-10", "status": "PAUSED"})
    assert warn["severity"] == "warning"


def test_classify_backordered_is_warning():
    out = _classify_backordered({"order_number": "SO-1", "total_amount": "1250.00"})
    assert out["severity"] == "warning"
    assert "SO-1" in out["title"]
    assert "1250.00" in out["detail"]


# --- compose_active_issues: cross-entity union with a fake query_fn ----------

_SYSTEMS = [
    {"id": "mes-mil", "name": "Milwaukee MES", "system_type": "MES", "schema": "mes_milwaukee"},
    {"id": "erp", "name": "ERP", "system_type": "ERP", "schema": "erp_data"},
]


def _fake_query(schema, sql):
    """Dispatch canned rows by the table named in the composed SQL (a JSON string, exactly
    like ``db.query_pg``). Asserts the closed-vocabulary WHERE made it into the SQL."""
    assert "WHERE" in sql and "LIMIT" in sql
    if "quality_checks" in sql:
        return json.dumps({
            "columns": ["check_type", "pass_rate", "fail_count", "sample_size", "work_order_id"],
            "rows": [["FILL_LEVEL", 65.0, 7, 20, "wo-1"], ["VISUAL", 85.0, 3, 20, "wo-2"]],
            "row_count": 2})
    if "work_orders" in sql:
        return json.dumps({
            "columns": ["work_order_number", "status", "good_quantity", "planned_quantity", "line"],
            "rows": [["WO-9", "CANCELLED", 100, 500, "line1"], ["WO-10", "PAUSED", 400, 500, "line2"]],
            "row_count": 2})
    if "customer_orders" in sql:
        return json.dumps({
            "columns": ["order_number", "status", "total_amount"],
            "rows": [["SO-1", "BACKORDERED", "1250.00"]],
            "row_count": 1})
    return json.dumps({"columns": [], "rows": [], "row_count": 0})


def test_compose_active_issues_union_shape_and_severity():
    issues = compose_active_issues(_SYSTEMS, query_fn=_fake_query)

    # 2 quality + 2 work-order (one MES system) + 1 backordered (ERP) = 5.
    assert len(issues) == 5

    # Every item carries the documented closed shape.
    for it in issues:
        assert set(it) == {"severity", "title", "detail", "source_system", "entity", "lineage"}
        assert it["severity"] in ("warning", "critical")
        assert isinstance(it["lineage"], dict)
        assert "WHERE" in it["lineage"]["sql"]  # filtered, lineage-backed SQL

    by_entity = {}
    for it in issues:
        by_entity.setdefault(it["entity"], []).append(it)

    # quality-check: a critical (65%) + a warning (85%), MES source.
    q = by_entity["quality-check"]
    assert {i["severity"] for i in q} == {"critical", "warning"}
    assert all(i["source_system"] == "Milwaukee MES" for i in q)

    # work-order: CANCELLED critical + PAUSED warning.
    w = by_entity["work-order"]
    assert {i["severity"] for i in w} == {"critical", "warning"}

    # sales-order: backordered warning from ERP.
    o = by_entity["sales-order"]
    assert len(o) == 1 and o[0]["severity"] == "warning"
    assert o[0]["source_system"] == "ERP"

    # JSON-serializable (no enum/dataclass leftovers).
    json.dumps(issues)


def test_compose_active_issues_only_matching_system_types():
    # Only an ERP system onboarded → only the backordered source runs (MES sources skipped).
    issues = compose_active_issues(
        [{"id": "erp", "name": "ERP", "system_type": "ERP", "schema": "erp_data"}],
        query_fn=_fake_query)
    assert {i["entity"] for i in issues} == {"sales-order"}


def test_compose_active_issues_empty_when_no_systems():
    assert compose_active_issues([], query_fn=_fake_query) == []


def test_compose_skips_non_sql_connection_types_instead_of_raising():
    """ISSUE_SOURCES execute through the PostgreSQL query path by contract, so
    a right-TYPED system on a non-SQL connection (observed live: an
    Athena-backed MES on a base install with no PostgreSQL) must be SKIPPED as
    not-applicable — never handed to query_pg to blow up into a false
    'temporarily unavailable' (a structural mismatch a retry can never fix)."""
    def _pg_must_not_run(_schema, _sql):
        raise AssertionError("non-SQL system must never reach the PG query path")

    non_sql = [
        {"id": "sys-a", "name": "Sys A", "system_type": "MES",
         "connection_type": "Athena", "schema": ""},
        {"id": "sys-b", "name": "Sys B", "system_type": "Historian",
         "connection_type": "SiteWise (Direct)", "schema": ""},
    ]
    assert compose_active_issues(non_sql, query_fn=_pg_must_not_run) == []

    # A SQL-backed system alongside them still reports (skip is per-system,
    # not a global bail-out). Catalog records without a connection_type key
    # (legacy PG-era rows) keep flowing to the SQL path — absence ≠ non-SQL.
    mixed = non_sql + [
        {"id": "erp", "name": "ERP", "system_type": "ERP", "schema": "erp_data"},
    ]
    issues = compose_active_issues(mixed, query_fn=_fake_query)
    assert {i["entity"] for i in issues} == {"sales-order"}


def test_compose_active_issues_fails_loud_on_db_error():
    def _boom(_schema, _sql):
        raise RuntimeError("relation \"quality_checks\" does not exist")

    with pytest.raises(RuntimeError):
        compose_active_issues(_SYSTEMS, query_fn=_boom)

class _SchemaShapeError(Exception):
    """Duck-typed pgdriver error: ``pgcode`` carries the SQLSTATE."""
    def __init__(self, pgcode: str, msg: str):
        super().__init__(msg)
        self.pgcode = pgcode


_MIXED_VOCAB_SYSTEMS = _SYSTEMS + [
    # A plant MES whose schema has production_runs (no work_orders) and a
    # quality_checks table WITHOUT a pass_rate column; a plant ERP with
    # ``orders`` (no customer_orders). Placeholder ids (ADR-0044).
    {"id": "mes-plant", "name": "Plant MES", "system_type": "MES", "schema": "mes_plant"},
    {"id": "erp-plant", "name": "Plant ERP", "system_type": "ERP", "schema": "erp_plant"},
]


def _vocab_mismatch_query(schema, sql):
    if schema == "mes_plant":
        if "work_orders" in sql:
            raise _SchemaShapeError("42P01", 'relation "work_orders" does not exist')
        raise _SchemaShapeError("42703", 'column "pass_rate" does not exist')
    if schema == "erp_plant":
        raise _SchemaShapeError("42P01", 'relation "customer_orders" does not exist')
    return _fake_query(schema, sql)


def test_vocabulary_mismatch_skips_that_system_and_the_rest_still_report():
    """Test event 18 (2026-09-18): the fixed sources name tables/columns of ONE
    seeded vocabulary; a plant system with a different vocabulary made the
    whole panel 'unavailable' although the other systems could report."""
    issues = compose_active_issues(_MIXED_VOCAB_SYSTEMS, query_fn=_vocab_mismatch_query)
    assert len(issues) == 5
    assert {i["source_system"] for i in issues} == {s["name"] for s in _SYSTEMS}


def test_vocabulary_mismatch_alone_yields_empty_not_error():
    plant_only = _MIXED_VOCAB_SYSTEMS[2:]
    assert compose_active_issues(plant_only, query_fn=_vocab_mismatch_query) == []


def test_non_shape_pg_errors_still_fail_loud():
    def _outage(_schema, _sql):
        raise _SchemaShapeError("08006", "connection failure")

    with pytest.raises(_SchemaShapeError):
        compose_active_issues(_SYSTEMS, query_fn=_outage)



def test_issue_sources_use_max_per_source_cap():
    # Each composed source SQL is capped at MAX_ISSUES_PER_SOURCE.
    captured = {}

    def _capture(schema, sql):
        captured[sql] = True
        return json.dumps({"columns": [], "rows": [], "row_count": 0})

    compose_active_issues(_SYSTEMS, query_fn=_capture)
    assert captured  # at least one source queried
    assert all(f"LIMIT {MAX_ISSUES_PER_SOURCE}" in sql for sql in captured)


def test_spec_for_builds_section_columns():
    spec = _spec_for(ISSUE_SOURCES[0])
    assert isinstance(spec, SectionSpec)
    assert spec.table == "quality_checks"
    assert [c["column"] for c in spec.columns] == ISSUE_SOURCES[0].columns


# --- route: friendly HTTP-200 envelope (success + simulated error) -----------

def test_route_success_envelope(monkeypatch):
    canned = [{"severity": "critical", "title": "t", "detail": "d",
               "source_system": "Milwaukee MES", "entity": "quality-check",
               "lineage": {"sql": "SELECT ..."}}]
    monkeypatch.setattr(dashboard.active_issues, "compose_active_issues",
                        lambda systems: canned)
    # Avoid touching the real catalog/onboarded path's downstream — _onboarded_for_plant
    # returns the (empty by default) onboarded list; compose is mocked regardless.
    result = asyncio.run(dashboard_active_issues(plant="all"))
    assert result == {"kind": "active-issues", "issues": canned, "status": "ok"}
    json.dumps(result)


def test_route_degrades_to_friendly_error_envelope(monkeypatch):
    def _boom(_systems):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(dashboard.active_issues, "compose_active_issues", _boom)
    result = asyncio.run(dashboard_active_issues(plant="all"))

    assert result["kind"] == "active-issues"
    assert result["status"] == "error"
    assert result["issues"] == []
    assert result["error"]  # friendly message present (never a raw 500/stack trace)
    json.dumps(result)


# --- AuthMiddleware coverage via TestClient ----------------------------------

def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(dashboard_router)
    return app


def test_route_requires_auth(monkeypatch):
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.get("/api/dashboard/active-issues")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_route_passes_under_dev_no_auth(monkeypatch, local_auth_env):
    get_config.cache_clear()
    monkeypatch.setattr(dashboard.active_issues, "compose_active_issues", lambda systems: [])
    client = TestClient(_build_app())

    resp = client.get("/api/dashboard/active-issues")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"kind": "active-issues", "issues": [], "status": "ok"}
