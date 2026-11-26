# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents.entity_render`` (S5.3 Wave 1, ADR 0021).

All tests are pure: the graph-read seam (``query=...``) and the SQL-exec seam
(``query_fn=...``) are injected as fakes. No AWS / Neptune / pg8000 / LLM is touched.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
import threading
import time
from unittest.mock import Mock

import pytest

import agents.entity_render as er
from agents.entity_render import (RenderHint, SectionSpec, build_athena_section_sql,
                                  build_section_sql)


@pytest.fixture(autouse=True)
def _clear_hot_read_memo():
    """C2b memo is process-global with a 15s TTL — isolate every test."""
    er._hot_read_memo.clear()
    yield
    er._hot_read_memo.clear()


# --------------------------------------------------------------------------
# Fake graph seam — returns canned RETURN rows for the one sections cypher.
# --------------------------------------------------------------------------

def make_query(rows):
    """Build a fake ``neptune_query`` returning ``rows`` for the sections read."""
    def _q(cypher, parameters=None):
        assert "MAPS_TO]->(ec:Concept)" in cypher
        return [dict(r) for r in rows]
    return _q


def _row(table, entity, column, field_concept=None, role=None):
    return {"table": table, "entity": entity, "column": column,
            "field_concept": field_concept, "role": role}


# --------------------------------------------------------------------------
# read_sections — ordering + labels (3 mapped + 2 unmapped)
# --------------------------------------------------------------------------

def test_column_ordering_and_labels():
    # mapped: order-status (role none), order-id (identifier role), customer (none);
    # unmapped: status, internal_ref (discovered order preserved).
    rows = [
        _row("customer_orders", "sales-order", "status", None, None),          # unmapped
        _row("customer_orders", "sales-order", "order_number", "sales-order", "identifier"),
        _row("customer_orders", "sales-order", "internal_ref", None, None),    # unmapped
        _row("customer_orders", "sales-order", "order_state", "order-status", None),
        _row("customer_orders", "sales-order", "cust", "customer", None),
    ]
    specs = er.read_sections("erp", query=make_query(rows))
    assert len(specs) == 1
    spec = specs[0]
    assert spec.entity == "sales-order"
    assert spec.entity_label == "Sales Order"

    cols = spec.columns
    # identifier-role mapped column first, then other mapped by field_concept_id
    # (customer < order-status), then unmapped in discovered order (status, internal_ref).
    assert [c["column"] for c in cols] == [
        "order_number", "cust", "order_state", "status", "internal_ref"]
    assert [c["field_concept"] for c in cols] == [
        "sales-order", "customer", "order-status", None, None]
    # labels: mapped use CONCEPT_NAME, unmapped humanized
    by_col = {c["column"]: c["label"] for c in cols}
    assert by_col["order_number"] == "Sales Order"
    assert by_col["cust"] == "Customer"
    assert by_col["order_state"] == "Order Status"
    assert by_col["status"] == "Status"
    assert by_col["internal_ref"] == "Internal Ref"


def test_null_field_concept_column_humanized():
    rows = [_row("widgets", "product", "widget_count", None, None)]
    specs = er.read_sections("plm", query=make_query(rows))
    assert specs[0].columns == [
        {"column": "widget_count", "label": "Widget Count", "field_concept": None}]


# --------------------------------------------------------------------------
# read_sections — dedupe / collapse / skip rules
# --------------------------------------------------------------------------

def test_raw_table_deduped_when_view_present():
    rows = [
        _row("work_orders", "work-order", "wo_number", "work-order", "identifier"),
        _row("work_orders_raw", "work-order", "wo_number", "work-order", "identifier"),
    ]
    specs = er.read_sections("cmms", query=make_query(rows))
    assert [s.table for s in specs] == ["work_orders"]  # _raw suppressed


def test_raw_table_kept_when_no_view():
    rows = [_row("events_raw", "downtime", "dt_id", "downtime", "identifier")]
    specs = er.read_sections("cmms", query=make_query(rows))
    assert [s.table for s in specs] == ["events_raw"]  # no non-raw base → kept


def test_multi_concept_per_field_collapses_to_one_column():
    # One column mapped to TWO concepts → one row; identifier role wins.
    rows = [
        _row("assets", "asset", "asset_tag", "asset-class", None),
        _row("assets", "asset", "asset_tag", "equipment-id", "identifier"),
    ]
    specs = er.read_sections("cmms", query=make_query(rows))
    cols = specs[0].columns
    assert len(cols) == 1
    assert cols[0]["column"] == "asset_tag"
    assert cols[0]["field_concept"] == "equipment-id"  # identifier role wins


def test_zero_field_table_skipped():
    # SchemaEntry mapped to entity but NO Field nodes (OPTIONAL MATCH → column None).
    rows = [_row("empty_tbl", "asset", None, None, None)]
    specs = er.read_sections("cmms", query=make_query(rows))
    assert specs == []  # no spec → caller builds no SQL


def test_no_graph_safe_empty_read():
    assert er.read_sections("erp", query=lambda *_a, **_k: []) == []


# --------------------------------------------------------------------------
# build_section_sql — exact string, validation, no SELECT *
# --------------------------------------------------------------------------

def test_build_section_sql_exact_string():
    spec = SectionSpec(table="customer_orders", entity="sales-order",
                       entity_label="Sales Order",
                       columns=[{"column": "a", "label": "A", "field_concept": None},
                                {"column": "b", "label": "B", "field_concept": None}])
    sql = build_section_sql("erp_data", spec)
    assert sql == 'SELECT "a","b" FROM "erp_data"."customer_orders" LIMIT 200'
    assert "*" not in sql  # never SELECT *


def test_build_section_sql_caps_columns():
    cols = [{"column": f"c{i}", "label": f"C{i}", "field_concept": None} for i in range(20)]
    spec = SectionSpec(table="t", entity="asset", entity_label="Asset", columns=cols)
    sql = build_section_sql("cmms_maintenance", spec, max_cols=3)
    assert sql == 'SELECT "c0","c1","c2" FROM "cmms_maintenance"."t" LIMIT 200'


def test_build_section_sql_rejects_bad_identifier():
    bad_table = SectionSpec(table="orders; DROP TABLE x", entity="e", entity_label="E",
                            columns=[{"column": "a", "label": "A", "field_concept": None}])
    with pytest.raises(ValueError):
        build_section_sql("erp_data", bad_table)

    bad_col = SectionSpec(table="orders", entity="e", entity_label="E",
                          columns=[{"column": "a; DROP", "label": "A", "field_concept": None}])
    with pytest.raises(ValueError):
        build_section_sql("erp_data", bad_col)

    with pytest.raises(ValueError):
        build_section_sql("erp data", SectionSpec(
            table="orders", entity="e", entity_label="E",
            columns=[{"column": "a", "label": "A", "field_concept": None}]))


def test_build_section_sql_empty_columns_raises():
    with pytest.raises(ValueError):
        build_section_sql("erp_data", SectionSpec(
            table="t", entity="asset", entity_label="Asset", columns=[]))


def test_build_section_sql_postgres_path_rejects_dotted_paths():
    # The PG path stays FLAT — a dotted struct path is a hard ValueError there
    # (only the Athena builder is struct-aware).
    dotted = SectionSpec(table="orders", entity="e", entity_label="E",
                         columns=[{"column": "entity_type_details.request.cell",
                                   "label": "Cell", "field_concept": None}])
    with pytest.raises(ValueError, match="Invalid identifier"):
        build_section_sql("erp_data", dotted)


# --------------------------------------------------------------------------
# build_athena_section_sql — struct-aware Athena variant
# --------------------------------------------------------------------------

def _lake_spec(columns):
    return SectionSpec(table="events", entity="work-order", entity_label="Work Order",
                       columns=[{"column": c, "label": c, "field_concept": None}
                                for c in columns])


def test_build_athena_section_sql_nested_path_composes_struct_access():
    sql = build_athena_section_sql(
        "lake_db", _lake_spec(["entity_type_details.request.cell", "status"]))
    assert sql == ('SELECT "entity_type_details"."request"."cell" '
                   'AS "entity_type_details.request.cell",'
                   '"status" FROM "lake_db"."events" LIMIT 200')
    assert "*" not in sql  # never SELECT *


def test_build_athena_section_sql_flat_columns_byte_identical_to_pg_builder():
    spec = _lake_spec(["order_number", "status"])
    assert build_athena_section_sql("lake_db", spec) == build_section_sql("lake_db", spec)


@pytest.mark.parametrize("bad", [
    'evil"; DROP TABLE x--.request.cell',   # injection in FIRST part
    'entity.re"quest.cell',                 # injection in MIDDLE part
    "entity.request.cell; DROP",            # injection in LEAF part
    "entity..cell",                         # empty part
    ".entity.cell",                         # leading dot → empty first part
    "entity.request.",                      # trailing dot → empty leaf
    "entity.1bad.cell",                     # part not an identifier
])
def test_build_athena_section_sql_injection_in_any_part_raises(bad):
    with pytest.raises(ValueError):
        build_athena_section_sql("lake_db", _lake_spec([bad]))


def test_build_athena_section_sql_depth_bounded():
    ok = ".".join(f"p{i}" for i in range(5))       # 5 parts — at the bound
    sql = build_athena_section_sql("lake_db", _lake_spec([ok]))
    assert f'AS "{ok}"' in sql
    too_deep = ".".join(f"p{i}" for i in range(6))  # 6 parts — over
    with pytest.raises(ValueError, match="struct path too deep"):
        build_athena_section_sql("lake_db", _lake_spec([too_deep]))


def test_build_athena_section_sql_empty_columns_and_bad_table_raise():
    with pytest.raises(ValueError):
        build_athena_section_sql("lake_db", _lake_spec([]))
    with pytest.raises(ValueError):
        build_athena_section_sql("lake db", _lake_spec(["a"]))
    bad_table = SectionSpec(table="t; DROP", entity="e", entity_label="E",
                            columns=[{"column": "a", "label": "A", "field_concept": None}])
    with pytest.raises(ValueError):
        build_athena_section_sql("lake_db", bad_table)


def test_run_athena_section_nested_logs_info_once_per_section(monkeypatch, caplog):
    # Dotted graph column names ⇒ ONE honest INFO line per section (physical
    # discovery was skipped upstream) — never one per column.
    captured = {}

    def fake_retry(compose_sql, columns, config, **kwargs):
        captured["sql"] = compose_sql(columns)
        return {"result": {"rows": [], "row_count": 0}, "columns": columns, "denied": []}

    monkeypatch.setattr("agents.athena_tools.run_athena_select_with_column_retry",
                        fake_retry)
    monkeypatch.setattr("agents.metric_resolver._full_connection_config", lambda sid: {})
    spec = _lake_spec(["entity_type_details.request.cell",
                       "entity_type_details.request.line", "status"])
    with caplog.at_level(logging.INFO, logger="agents.entity_render"):
        er._run_athena_section("sys-lake", "lake_db", spec)
    hits = [r for r in caplog.records if "nested struct path" in r.getMessage()]
    assert len(hits) == 1                       # once per SECTION, not per column
    assert "2 nested struct path(s)" in hits[0].getMessage()
    assert '"entity_type_details"."request"."cell"' in captured["sql"]


def test_run_athena_section_flat_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(
        "agents.athena_tools.run_athena_select_with_column_retry",
        lambda compose_sql, columns, config, **kw: {
            "result": {"rows": [], "row_count": 0}, "columns": columns, "denied": []})
    monkeypatch.setattr("agents.metric_resolver._full_connection_config", lambda sid: {})
    with caplog.at_level(logging.INFO, logger="agents.entity_render"):
        er._run_athena_section("sys-lake", "lake_db", _lake_spec(["a", "b"]))
    assert not [r for r in caplog.records if "nested struct path" in r.getMessage()]


# --------------------------------------------------------------------------
# render_section — contract shape, ok path, error path (never raises)
# --------------------------------------------------------------------------

_OK_SYSTEM = {"id": "erp", "name": "ERP", "schema": "erp_data",
              "connection_type": "PostgreSQL", "system_type": "ERP"}


def _spec():
    return SectionSpec(table="customer_orders", entity="sales-order",
                       entity_label="Sales Order",
                       columns=[{"column": "order_number", "label": "Sales Order",
                                 "field_concept": "sales-order"},
                                {"column": "status", "label": "Status",
                                 "field_concept": None}])


def test_render_section_ok_contract():
    payload = json.dumps({"columns": ["order_number", "status"],
                          "rows": [["SO-1001", "SHIPPED"], ["SO-1002", "PREPARING"]],
                          "row_count": 2})
    captured = {}

    def fake_qfn(schema, sql):
        captured["schema"], captured["sql"] = schema, sql
        return payload

    sec = er.render_section(_OK_SYSTEM, _spec(), query_fn=fake_qfn)
    assert sec["status"] == "ok"
    assert sec["kind"] == "table" == RenderHint.TABLE.value
    assert sec["group_by"] is None and sec["filter"] is None
    assert sec["system_id"] == "erp" and sec["system_name"] == "ERP"
    assert sec["entity"] == "sales-order" and sec["entity_label"] == "Sales Order"
    assert sec["table"] == "customer_orders"
    assert sec["rows"] == [["SO-1001", "SHIPPED"], ["SO-1002", "PREPARING"]]
    assert sec["row_count"] == 2
    assert [c["column"] for c in sec["columns"]] == ["order_number", "status"]
    # lineage present, per-column concept→system→table.column chain
    assert sec["lineage"] == [
        {"concept": "sales-order", "system_id": "erp",
         "table": "customer_orders", "column": "order_number"},
        {"concept": None, "system_id": "erp",
         "table": "customer_orders", "column": "status"}]
    # SQL was built deterministically and executed against the system's schema
    assert captured["schema"] == "erp_data"
    assert captured["sql"] == 'SELECT "order_number","status" FROM "erp_data"."customer_orders" LIMIT 200'


def test_render_section_error_never_raises_no_fabricated_rows():
    def boom(schema, sql):
        raise RuntimeError("connection refused")

    sec = er.render_section(_OK_SYSTEM, _spec(), query_fn=boom)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0  # never fabricated
    assert "Sales Order" in sec["error"] and "stack" not in sec["error"].lower()
    # contract seams still present on the error envelope
    assert sec["kind"] == "table"
    assert sec["group_by"] is None and sec["filter"] is None
    assert [c["column"] for c in sec["columns"]] == ["order_number", "status"]


def test_render_section_accepts_dict_payload():
    sec = er.render_section(_OK_SYSTEM, _spec(),
                            query_fn=lambda s, q: {"rows": [["x", "y"]], "row_count": 1})
    assert sec["status"] == "ok" and sec["row_count"] == 1


# --------------------------------------------------------------------------
# per-system routing — schema passed to query_fn picks Aurora vs demo
# --------------------------------------------------------------------------

@pytest.mark.parametrize("system,schema", [
    ({"id": "erp", "name": "ERP", "schema": "erp_data"}, "erp_data"),
    ({"id": "mes-mil", "name": "Milwaukee MES", "schema": "mes_milwaukee"}, "mes_milwaukee"),
    ({"id": "cmms", "name": "CMMS", "schema": "cmms_maintenance"}, "cmms_maintenance"),
    ({"id": "plm", "name": "PLM", "schema": "plm_engineering"}, "plm_engineering"),
])
def test_per_system_routing_schema(system, schema):
    # query_pg routes erp_*/mes_* → Aurora and cmms/plm → demo internally by this schema;
    # assert entity_render hands it the system's real schema unchanged.
    captured = {}

    def fake_qfn(s, q):
        captured["schema"] = s
        return {"rows": [], "row_count": 0}

    er.render_section(system, _spec(), query_fn=fake_qfn)
    assert captured["schema"] == schema


# --------------------------------------------------------------------------
# fan-out helpers
# --------------------------------------------------------------------------

def test_render_system_and_all_sections():
    rows = [_row("customer_orders", "sales-order", "order_number", "sales-order", "identifier")]
    q = make_query(rows)
    qfn = lambda s, sql: {"rows": [["SO-1"]], "row_count": 1}

    secs = er.render_system_sections(_OK_SYSTEM, query=q, query_fn=qfn)
    assert len(secs) == 1 and secs[0]["status"] == "ok"

    allsecs = er.render_all_sections([_OK_SYSTEM], query=q, query_fn=qfn)
    assert len(allsecs) == 1 and allsecs[0]["table"] == "customer_orders"


def test_render_system_sections_filters_exact_table_before_render():
    rows = [
        _row("customer_orders", "sales-order", "order_number", "sales-order", "identifier"),
        _row("customer_orders_archive", "sales-order", "order_number", "sales-order", "identifier"),
    ]
    executed = []

    def qfn(schema, sql):
        executed.append(sql)
        return {"rows": [["SO-1"]], "row_count": 1}

    sections = er.render_system_sections(
        _OK_SYSTEM, table="customer_orders", query=make_query(rows), query_fn=qfn)

    assert [section["table"] for section in sections] == ["customer_orders"]
    assert len(executed) == 1
    assert '"customer_orders_archive"' not in executed[0]


def test_render_system_sections_returns_requested_error_envelope():
    rows = [_row("customer_orders", "sales-order", "order_number", "sales-order", "identifier")]

    sections = er.render_system_sections(
        _OK_SYSTEM, table="customer_orders", query=make_query(rows),
        query_fn=lambda *_args: (_ for _ in ()).throw(RuntimeError("source down")))

    assert len(sections) == 1
    assert sections[0]["table"] == "customer_orders"
    assert sections[0]["status"] == "error"
    assert sections[0]["rows"] == []



def test_safe_system_render_forwards_exact_table_and_injected_seams(monkeypatch):
    query = object()
    query_fn = object()
    render = Mock(return_value=[{"status": "ok"}])
    monkeypatch.setattr(er, "render_system_sections", render)

    assert er._render_system_sections_safe(
        _OK_SYSTEM, table="customer_orders", query=query, query_fn=query_fn,
    ) == [{"status": "ok"}]
    render.assert_called_once_with(
        _OK_SYSTEM, table="customer_orders", query=query, query_fn=query_fn,
    )


def test_safe_targeted_graph_read_failure_warns_once_and_later_recovers(caplog):
    calls = {"n": 0}

    def graph_read(_cypher, _parameters=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("graph unavailable")
        return [_row("customer_orders", "sales-order", "order_number",
                     "sales-order", "identifier")]

    def query_fn(*_args):
        return {"rows": [["SO-1"]], "row_count": 1}

    with caplog.at_level(logging.WARNING, logger="agents.entity_render"):
        first = er._render_system_sections_safe(
            _OK_SYSTEM, table="customer_orders", query=graph_read, query_fn=query_fn,
        )
        second = er._render_system_sections_safe(
            _OK_SYSTEM, table="customer_orders", query=graph_read, query_fn=query_fn,
        )

    assert first == []
    assert [section["table"] for section in second] == ["customer_orders"]
    warnings = [record for record in caplog.records
                if "system render failed" in record.getMessage()]
    assert len(warnings) == 1

# --------------------------------------------------------------------------
# C2: parallel across-systems fan-out (serial-telemetry invariant preserved)
# --------------------------------------------------------------------------

def _fake_systems(n, tables_per_system=1):
    """N generic PG fake systems, each with ``tables_per_system`` graph tables."""
    systems, graph_rows = [], {}
    for i in range(n):
        sid = f"sys-{i:02d}"
        systems.append({"id": sid, "name": f"System {i}", "schema": f"schema_{i}",
                        "connection_type": "PostgreSQL"})
        graph_rows[sid] = [
            _row(f"table_{i}_{t}", "asset", "asset_tag", "equipment-id", "identifier")
            for t in range(tables_per_system)]
    def q(cypher, parameters=None):
        return [dict(r) for r in graph_rows.get((parameters or {}).get("sid"), [])]
    return systems, q


class _ConcurrencyGauge:
    """Instrumented ``query_fn``: tracks global + per-system concurrent entries."""

    def __init__(self, delay=0.1):
        self.delay = delay
        self._lock = threading.Lock()
        self._active_global = 0
        self._active_by_key = {}
        self.max_global = 0
        self.max_by_key = {}

    def __call__(self, schema, sql):
        with self._lock:
            self._active_global += 1
            self._active_by_key[schema] = self._active_by_key.get(schema, 0) + 1
            self.max_global = max(self.max_global, self._active_global)
            self.max_by_key[schema] = max(
                self.max_by_key.get(schema, 0), self._active_by_key[schema])
        try:
            time.sleep(self.delay)
            return {"rows": [["A-1"]], "row_count": 1}
        finally:
            with self._lock:
                self._active_global -= 1
                self._active_by_key[schema] -= 1


def test_fanout_bounded_pool_max_8_min_2():
    # 12 systems, pool bound min(8, 12) == 8: concurrency must reach ≥2 (it IS
    # parallel) and never exceed 8 (it IS bounded).
    systems, q = _fake_systems(12)
    gauge = _ConcurrencyGauge(delay=0.1)
    er.render_all_sections(systems, query=q, query_fn=gauge)
    assert gauge.max_global <= 8
    assert gauge.max_global >= 2


def test_fanout_order_byte_identical_to_serial():
    # The flattened output must equal the serial implementation's output —
    # same systems in, same section ORDER out (list equality, not membership).
    systems, q = _fake_systems(6, tables_per_system=3)
    qfn = lambda s, sql: {"rows": [["A-1"]], "row_count": 1}  # deterministic
    expected = []
    for system in systems:  # the pre-C2 serial implementation, inlined
        expected.extend(er.render_system_sections(system, query=q, query_fn=qfn))
    result = er.render_all_sections(systems, query=q, query_fn=qfn)
    assert result == expected  # byte-identical sections, byte-identical order
    assert [(s["system_id"], s["table"]) for s in result] == [
        (f"sys-{i:02d}", f"table_{i}_{t}") for i in range(6) for t in range(3)]


def test_fanout_wall_clock_collapses():
    # 6 systems × 1 section, each query sleeping ~0.1 s: serial would take
    # ~0.6 s; parallel must finish in < serial/2 (generous, no flake).
    systems, q = _fake_systems(6)
    delay = 0.1
    gauge = _ConcurrencyGauge(delay=delay)
    start = time.monotonic()
    secs = er.render_all_sections(systems, query=q, query_fn=gauge)
    elapsed = time.monotonic() - start
    assert len(secs) == 6
    assert elapsed < (len(systems) * delay) / 2


def test_serial_within_one_system_five_sections():
    # SERIAL-TELEMETRY INVARIANT: one system's sections must never render
    # concurrently — a single system with 5 sections observes max concurrency 1.
    systems, q = _fake_systems(1, tables_per_system=5)
    gauge = _ConcurrencyGauge(delay=0.02)
    secs = er.render_all_sections(systems, query=q, query_fn=gauge)
    assert len(secs) == 5
    assert gauge.max_by_key["schema_0"] == 1


def test_parallel_across_but_serial_within_each_system():
    # The strong combined form: multiple systems overlap globally, yet NO
    # system ever observes two of its own sections in flight at once.
    systems, q = _fake_systems(6, tables_per_system=3)
    gauge = _ConcurrencyGauge(delay=0.03)
    er.render_all_sections(systems, query=q, query_fn=gauge)
    assert gauge.max_global >= 2  # across systems: parallel
    assert all(v == 1 for v in gauge.max_by_key.values())  # within: serial


def test_fanout_empty_systems_guard():
    assert er.render_all_sections([], query=lambda *a, **k: [],
                                  query_fn=lambda s, q: {}) == []


def test_fanout_worker_exception_contained(caplog):
    # A system whose GRAPH read raises (render_section can't contain that seam)
    # yields [] + one WARNING; the sibling system still renders — the endpoint
    # never dies wholesale.
    good = {"id": "sys-ok", "name": "OK", "schema": "s_ok",
            "connection_type": "PostgreSQL"}
    bad = {"id": "sys-bad", "name": "Bad", "schema": "s_bad",
           "connection_type": "PostgreSQL"}

    def q(cypher, parameters=None):
        if (parameters or {}).get("sid") == "sys-bad":
            raise RuntimeError("graph exploded")
        return [_row("t_ok", "asset", "asset_tag", "equipment-id", "identifier")]

    qfn = lambda s, sql: {"rows": [["A-1"]], "row_count": 1}
    with caplog.at_level(logging.WARNING, logger="agents.entity_render"):
        secs = er.render_all_sections([bad, good], query=q, query_fn=qfn)
    assert [(s["system_id"], s["status"]) for s in secs] == [("sys-ok", "ok")]
    warnings = [r for r in caplog.records
                if r.name == "agents.entity_render" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "sys-bad" in warnings[0].getMessage()


# --------------------------------------------------------------------------
# C2: SITEWISE_I3X branch — graph-asset render, honest fallback, zero WARNINGs
# --------------------------------------------------------------------------

_I3X_SYSTEM = {"id": "hist-ot", "name": "OT Historian",
               "connection_type": "SiteWise (i3X)"}


def _i3x_spec():
    return SectionSpec(table="asset_hierarchy", entity="asset", entity_label="Asset",
                       columns=[{"column": "name", "label": "Asset",
                                 "field_concept": None}])


def _forbidden_qfn(schema, sql):
    raise AssertionError("i3X section must never compose/execute SQL")


def test_i3x_with_graph_assets_renders_graph_section(monkeypatch, caplog):
    asset_rows = [
        {"name": "Line 1", "asset_type": "line", "path": "/plant/line-1", "level": 2},
        {"name": "Pump 7", "asset_type": "pump", "path": "/plant/line-1/pump-7",
         "level": 3},
    ]

    def fake_nq(cypher, parameters=None):
        assert "Asset" in cypher and parameters == {"sid": "hist-ot"}
        return [dict(r) for r in asset_rows]

    monkeypatch.setattr(er, "neptune_query", fake_nq)
    with caplog.at_level(logging.DEBUG, logger="agents.entity_render"):
        sec = er.render_section(_I3X_SYSTEM, _i3x_spec(), query_fn=_forbidden_qfn)
    assert sec["status"] == "ok"
    assert sec["rows"] == [["Line 1", "line", "/plant/line-1", 2],
                           ["Pump 7", "pump", "/plant/line-1/pump-7", 3]]
    assert sec["row_count"] == 2  # rows come ONLY from the graph read
    assert [c["column"] for c in sec["columns"]] == [
        "name", "asset_type", "path", "level"]
    entity_records = [r for r in caplog.records if r.name == "agents.entity_render"]
    assert [r for r in entity_records if r.levelno >= logging.WARNING] == []
    assert len([r for r in entity_records if r.levelno == logging.DEBUG]) <= 1


def test_i3x_without_graph_assets_honest_error_no_warning(monkeypatch, caplog):
    monkeypatch.setattr(er, "neptune_query", lambda *a, **k: [])
    with caplog.at_level(logging.DEBUG, logger="agents.entity_render"):
        sec = er.render_section(_I3X_SYSTEM, _i3x_spec(), query_fn=_forbidden_qfn)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0  # ADR-0045: never fabricate
    assert "SiteWise (i3X)" in sec["error"]  # friendly, names the connection type
    assert "Asset" in sec["error"] or "asset" in sec["error"]
    entity_records = [r for r in caplog.records if r.name == "agents.entity_render"]
    assert [r for r in entity_records if r.levelno >= logging.WARNING] == []
    debugs = [r for r in entity_records if r.levelno == logging.DEBUG]
    assert len(debugs) == 1 and debugs[0].exc_info is None


def test_i3x_graph_read_failure_honest_error_no_warning(monkeypatch, caplog):
    def broken_nq(*a, **k):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(er, "neptune_query", broken_nq)
    with caplog.at_level(logging.DEBUG, logger="agents.entity_render"):
        sec = er.render_section(_I3X_SYSTEM, _i3x_spec(), query_fn=_forbidden_qfn)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0
    assert "SiteWise (i3X)" in sec["error"]
    assert [r for r in caplog.records
            if r.name == "agents.entity_render"
            and r.levelno >= logging.WARNING] == []


# --------------------------------------------------------------------------
# Import purity (ADR 0001/0020): imports with pg8000/agents.pgdriver/boto3 absent.
# --------------------------------------------------------------------------

def test_module_is_import_pure_symbols():
    assert not hasattr(er, "boto3")
    assert not hasattr(er, "pgdriver")
    assert callable(er.neptune_query)
    assert callable(er.query_pg)


def test_imports_with_no_pg8000_or_aws():
    code = (
        "import sys\n"
        "for _m in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[_m] = None\n"
        "import agents.entity_render as er\n"
        "assert callable(er.neptune_query)\n"
        "assert er.read_sections('erp', query=lambda *a, **k: []) == []\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


class TestHotReadMemo:
    """C2b — per-system hot-read memo (model enumeration / graph asset rows)."""

    def test_second_call_within_ttl_skips_the_read(self):
        calls = []
        out1 = er._memoized(("k", "s1"), lambda: calls.append(1) or "v")
        out2 = er._memoized(("k", "s1"), lambda: calls.append(1) or "v")
        assert (out1, out2) == ("v", "v") and len(calls) == 1

    def test_keys_are_system_scoped(self):
        assert er._memoized(("k", "s1"), lambda: "a") == "a"
        assert er._memoized(("k", "s2"), lambda: "b") == "b"

    def test_expired_entry_recomputes(self):
        er._memoized(("k", "s1"), lambda: "old")
        ts, v = er._hot_read_memo[("k", "s1")]
        er._hot_read_memo[("k", "s1")] = (ts - er._HOT_READ_TTL_S - 1, v)
        assert er._memoized(("k", "s1"), lambda: "new") == "new"

    def test_failure_is_not_cached(self):
        with pytest.raises(RuntimeError):
            er._memoized(("k", "s1"), lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert ("k", "s1") not in er._hot_read_memo
        assert er._memoized(("k", "s1"), lambda: "ok") == "ok"

    def test_i3x_sections_share_one_graph_read(self, monkeypatch):
        calls = []

        def fake_query(cypher, params=None):
            calls.append(1)
            return [{"name": "Line1", "asset_type": "Area", "path": "/p", "level": 1}]

        monkeypatch.setattr(er, "neptune_query", fake_query)
        system = {"id": "sw1", "name": "SW", "connection_type": "SiteWise (i3X)"}
        specs = [SectionSpec(table=f"T{i}", entity=f"t{i}", entity_label=f"T{i}")
                 for i in range(3)]
        out = [er.render_section(system, s, query_fn=lambda *a: '{"rows": []}')
               for s in specs]
        assert len(calls) == 1, f"expected ONE graph read across 3 i3X sections, saw {len(calls)}"
        assert all(o["status"] == "ok" and o["rows"] for o in out)
