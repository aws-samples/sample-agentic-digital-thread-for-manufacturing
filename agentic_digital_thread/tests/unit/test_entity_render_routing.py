# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents.entity_render``'s connection-type routing.

``render_section`` routes by the system's ``connection_type`` (mirroring
``metric_resolver._execute``): PostgreSQL → the injected ``query_fn`` (the
``db.query_pg`` schema-routing seam), Athena → ``athena_tools.run_athena_query``
with the cross-account-safe ``metric_resolver._full_connection_config``, and
SiteWise (Direct) → a native latest-value-per-asset table via ``sitewise_read``.

All connectors are mocked (synthetic names only; AWS documentation placeholder
account ids like 111122223333 where an ARN shape is needed). No AWS / Neptune /
pg8000 / LLM is touched.
"""
from __future__ import annotations

import pytest

import agents.entity_render as er
from agents.entity_render import SectionSpec


def _spec(table="orders", columns=None):
    cols = columns if columns is not None else [
        {"column": "order_number", "label": "Order Number", "field_concept": "work-order"},
        {"column": "status", "label": "Status", "field_concept": None},
    ]
    return SectionSpec(table=table, entity="work-order", entity_label="Work Order",
                       columns=cols)


_PG_SYSTEM = {"id": "sys-pg", "name": "PG System", "schema": "erp_data",
              "connection_type": "PostgreSQL", "system_type": "ERP"}
_ATHENA_SYSTEM = {"id": "sys-lake", "name": "Lake System", "schema": "lake_db",
                  "connection_type": "Athena", "system_type": "MES"}
_SW_SYSTEM = {"id": "sys-ot", "name": "OT System", "schema": "sys-ot",
              "connection_type": "SiteWise (Direct)", "system_type": "Historian"}


# ---------------------------------------------------------------------------
# PostgreSQL branch — unchanged legacy path
# ---------------------------------------------------------------------------

def test_postgres_routes_through_query_fn_only(monkeypatch):
    called = {}

    def fake_qfn(schema, sql):
        called["schema"], called["sql"] = schema, sql
        return {"rows": [["WO-1", "OPEN"]], "row_count": 1}

    def athena_boom(*a, **k):
        raise AssertionError("Athena connector must NOT be called for PostgreSQL")

    monkeypatch.setattr("agents.athena_tools.run_athena_query", athena_boom)
    sec = er.render_section(_PG_SYSTEM, _spec(), query_fn=fake_qfn)
    assert sec["status"] == "ok"
    assert sec["rows"] == [["WO-1", "OPEN"]]
    assert called["schema"] == "erp_data"
    assert called["sql"] == 'SELECT "order_number","status" FROM "erp_data"."orders" LIMIT 200'


def test_missing_connection_type_defaults_to_postgres():
    system = {"id": "sys-x", "name": "X", "schema": "erp_data"}
    called = {}

    def fake_qfn(schema, sql):
        called["schema"] = schema
        return {"rows": [], "row_count": 0}

    sec = er.render_section(system, _spec(), query_fn=fake_qfn)
    assert sec["status"] == "ok" and called["schema"] == "erp_data"


# ---------------------------------------------------------------------------
# Athena branch — run_athena_query + _full_connection_config, query_fn untouched
# ---------------------------------------------------------------------------

def test_athena_routes_through_run_athena_query(monkeypatch):
    captured = {}
    full_config = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
                   "role_arn": "arn:aws:iam::111122223333:role/ReadOnly",
                   "external_id": "example-external-id"}

    def fake_full_config(system_id):
        captured["config_system"] = system_id
        return full_config

    def fake_run(sql, connection_config):
        captured["sql"], captured["config"] = sql, connection_config
        return {"columns": ["order_number", "status"],
                "rows": [["WO-9", "CLOSED"]], "row_count": 1}

    def pg_boom(schema, sql):
        raise AssertionError("query_fn must NOT be called for Athena")

    monkeypatch.setattr("agents.metric_resolver._full_connection_config", fake_full_config)
    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)

    sec = er.render_section(_ATHENA_SYSTEM, _spec(), query_fn=pg_boom)
    assert sec["status"] == "ok"
    assert sec["rows"] == [["WO-9", "CLOSED"]] and sec["row_count"] == 1
    # The composed section SQL (double-quoted identifiers, LIMIT) went to Athena.
    assert captured["sql"] == 'SELECT "order_number","status" FROM "lake_db"."orders" LIMIT 200'
    # Cross-account-safe config resolution was REUSED (not the raw catalog snapshot).
    assert captured["config_system"] == "sys-lake"
    assert captured["config"] is full_config
    # Envelope contract intact.
    assert sec["kind"] == "table"
    assert sec["group_by"] is None and sec["filter"] is None
    assert sec["system_id"] == "sys-lake"
    assert [c["column"] for c in sec["columns"]] == ["order_number", "status"]


def test_athena_error_names_connection_type_no_fabricated_rows(monkeypatch):
    monkeypatch.setattr("agents.metric_resolver._full_connection_config", lambda sid: {})

    def boom(sql, connection_config):
        raise RuntimeError("workgroup missing")

    monkeypatch.setattr("agents.athena_tools.run_athena_query", boom)
    sec = er.render_section(_ATHENA_SYSTEM, _spec(),
                            query_fn=lambda s, q: {"rows": [["fake"]], "row_count": 1})
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0    # never fabricated
    assert "Athena" in sec["error"]                       # names the connection type
    assert "Work Order" in sec["error"]


# ---------------------------------------------------------------------------
# Athena branch — EMPTY catalog schema resolves from the connection config's
# 'database' key (the same key run_athena_query's QueryExecutionContext uses)
# ---------------------------------------------------------------------------

_ATHENA_NO_SCHEMA_SYSTEM = {"id": "sys-lake2", "name": "Lake NoSchema", "schema": "",
                            "connection_type": "Athena", "system_type": "MES"}


def test_athena_empty_catalog_schema_resolves_database_from_config(monkeypatch):
    captured = {}
    full_config = {"workgroup": "wg", "database": "config_lake_db",
                   "region": "us-east-1"}

    monkeypatch.setattr("agents.entity_render.get_schema", lambda sid: "")
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: full_config)

    def fake_run(sql, connection_config):
        captured["sql"] = sql
        return {"columns": ["order_number", "status"],
                "rows": [["WO-1", "OPEN"]], "row_count": 1}

    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)
    sec = er.render_section(_ATHENA_NO_SCHEMA_SYSTEM, _spec(),
                            query_fn=lambda s, q: pytest.fail("query_fn must NOT be called"))
    assert sec["status"] == "ok"
    # The SQL was composed against the config's database — NOT an empty schema.
    assert captured["sql"] == \
        'SELECT "order_number","status" FROM "config_lake_db"."orders" LIMIT 200'
    assert sec["rows"] == [["WO-1", "OPEN"]]


def test_athena_empty_catalog_schema_falls_back_to_config_schema_key(monkeypatch):
    # Legacy configs may carry 'schema' instead of 'database' — same precedence
    # athena_tools.run_athena_query applies (database first, then schema).
    captured = {}
    monkeypatch.setattr("agents.entity_render.get_schema", lambda sid: "")
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: {"workgroup": "wg", "schema": "legacy_lake_db"})

    def fake_run(sql, connection_config):
        captured["sql"] = sql
        return {"rows": [], "row_count": 0}

    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)
    sec = er.render_section(_ATHENA_NO_SCHEMA_SYSTEM, _spec(), query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert '"legacy_lake_db"."orders"' in captured["sql"]


def test_athena_no_schema_anywhere_is_clear_error_not_invalid_identifier(monkeypatch):
    # BOTH the catalog schema and the config database are missing → a CLEAR
    # status='error' naming the problem, never a cryptic "Invalid identifier: ''".
    monkeypatch.setattr("agents.entity_render.get_schema", lambda sid: "")
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: {"workgroup": "wg"})
    monkeypatch.setattr(
        "agents.athena_tools.run_athena_query",
        lambda *a, **k: pytest.fail("must NOT execute with no database"))
    sec = er.render_section(_ATHENA_NO_SCHEMA_SYSTEM, _spec(), query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0
    assert "Athena" in sec["error"]
    assert "database" in sec["error"]                 # names the actual problem
    assert "Invalid identifier" not in sec["error"]   # never the cryptic ValueError


def test_athena_config_resolution_failure_is_clear_error(monkeypatch):
    # A config-resolution failure (secret fetch blowing up, etc.) degrades to the
    # same clear no-database error — the boundary never raises.
    def cfg_boom(sid):
        raise RuntimeError("secrets manager unavailable")

    monkeypatch.setattr("agents.entity_render.get_schema", lambda sid: "")
    monkeypatch.setattr("agents.metric_resolver._full_connection_config", cfg_boom)
    sec = er.render_section(_ATHENA_NO_SCHEMA_SYSTEM, _spec(), query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert "database" in sec["error"]


def test_athena_catalog_schema_still_wins_over_config(monkeypatch):
    # A TRUTHY catalog schema is used as-is — the config database never overrides it.
    captured = {}
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: {"workgroup": "wg", "database": "other_db"})

    def fake_run(sql, connection_config):
        captured["sql"] = sql
        return {"rows": [], "row_count": 0}

    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)
    sec = er.render_section(_ATHENA_SYSTEM, _spec(), query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert '"lake_db"."orders"' in captured["sql"]     # catalog schema, not other_db


def test_postgres_empty_schema_does_not_consult_connection_config(monkeypatch):
    # The Postgres path is byte-identical: an empty schema still hits
    # build_section_sql's validation → the generic error boundary; the Athena
    # config resolution is NEVER consulted for PostgreSQL.
    def cfg_boom(sid):
        raise AssertionError("_full_connection_config must NOT be called for PostgreSQL")

    monkeypatch.setattr("agents.metric_resolver._full_connection_config", cfg_boom)
    monkeypatch.setattr("agents.entity_render.get_schema", lambda sid: "")
    system = {"id": "sys-pg2", "name": "PG NoSchema", "schema": "",
              "connection_type": "PostgreSQL", "system_type": "ERP"}
    sec = er.render_section(system, _spec(), query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0


# ---------------------------------------------------------------------------
# SiteWise (Direct) branch — native latest-value table via sitewise_read
# ---------------------------------------------------------------------------

def _patch_sitewise(monkeypatch, *, models, assets, values, config=None):
    """Wire the sitewise_read + config seams with canned data.

    ``values`` maps ``(asset_id, property_id)`` → the latest-value dict
    (served through the batch_get_property_values seam the renderer uses).
    """
    calls = {"models": [], "assets": [], "reads": []}

    def fake_full_config(system_id):
        return dict(config or {"region": "us-east-1"})

    def fake_models(cfg, region):
        calls["models"].append((cfg, region))
        return models

    def fake_assets(cfg, region, asset_model_id=""):
        calls["assets"].append(asset_model_id)
        return assets

    def fake_batch_values(cfg, region, entries):
        out = {}
        for e in entries:
            key = (e["assetId"], e["propertyId"])
            calls["reads"].append(key)
            out[f"{e['assetId']}_{e['propertyId']}"] = values.get(
                key, {"value": None, "timestamp": None})
        return out

    monkeypatch.setattr("agents.metric_resolver._full_connection_config", fake_full_config)
    monkeypatch.setattr("agents.sitewise_read.list_asset_models_with_properties", fake_models)
    monkeypatch.setattr("agents.sitewise_read.list_assets", fake_assets)
    monkeypatch.setattr("agents.sitewise_read.batch_get_property_values", fake_batch_values)
    return calls


_SW_SPEC = SectionSpec(
    table="Machine", entity="asset", entity_label="Asset",
    columns=[
        {"column": "speed", "label": "Machine Speed", "field_concept": "machine-speed"},
        {"column": "state", "label": "Machine State", "field_concept": "machine-state"},
    ])

_SW_MODELS = [{"id": "model-1", "name": "Machine", "properties": [
    {"id": "prop-speed", "name": "speed", "dataType": "DOUBLE"},
    {"id": "prop-state", "name": "state", "dataType": "STRING"},
    {"id": "prop-extra", "name": "unmapped_extra", "dataType": "DOUBLE"},
]}]


def test_sitewise_native_table_one_row_per_asset(monkeypatch):
    assets = [{"id": "asset-1", "name": "Press A"}, {"id": "asset-2", "name": "Press B"}]
    values = {
        ("asset-1", "prop-speed"): {"value": 42.5, "timestamp": 1700000000, "quality": "GOOD"},
        ("asset-1", "prop-state"): {"value": "RUNNING", "timestamp": 1700000100, "quality": "GOOD"},
        ("asset-2", "prop-speed"): {"value": 10, "timestamp": 1700000200, "quality": "GOOD"},
        # asset-2 state: no data → empty cell, not an error
        ("asset-2", "prop-state"): {"value": None, "timestamp": None},
    }
    calls = _patch_sitewise(monkeypatch, models=_SW_MODELS, assets=assets, values=values)

    def pg_boom(schema, sql):
        raise AssertionError("query_fn must NOT be called for SiteWise (Direct)")

    sec = er.render_section(_SW_SYSTEM, _SW_SPEC, query_fn=pg_boom)
    assert sec["status"] == "ok"
    # Columns: asset name + the mapped properties + the timestamp column.
    assert [c["column"] for c in sec["columns"]] == ["asset", "speed", "state", "timestamp"]
    assert sec["columns"][0]["label"] == "Asset"
    assert sec["columns"][-1]["field_concept"] is None
    # One row per asset: name, latest values, newest ISO timestamp.
    assert sec["row_count"] == 2 and len(sec["rows"]) == 2
    row1, row2 = sec["rows"]
    assert row1[0] == "Press A" and row1[1] == 42.5 and row1[2] == "RUNNING"
    assert row1[3].startswith("2023-")          # ISO-rendered newest timestamp
    assert row2[0] == "Press B" and row2[1] == 10
    assert row2[2] is None                       # no-data property → empty cell
    # Assets listed for the matched model; per-property reads bounded (2 assets × 2 props).
    assert calls["assets"] == ["model-1"]
    assert len(calls["reads"]) == 4
    # Envelope contract intact (kind/group_by/filter/lineage present).
    assert sec["kind"] == "table"
    assert sec["group_by"] is None and sec["filter"] is None
    assert len(sec["lineage"]) == len(sec["columns"])


def test_sitewise_asset_cap(monkeypatch):
    assets = [{"id": f"asset-{i}", "name": f"A{i}"} for i in range(80)]
    values = {}
    _patch_sitewise(monkeypatch, models=_SW_MODELS, assets=assets, values=values)
    sec = er.render_section(_SW_SYSTEM, _SW_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert sec["row_count"] == er.MAX_SITEWISE_ASSETS == 50


def test_sitewise_property_read_error_is_empty_cell(monkeypatch):
    assets = [{"id": "asset-1", "name": "Press A"}]
    values = {
        ("asset-1", "prop-speed"): {"error": "ThrottlingException"},
        ("asset-1", "prop-state"): {"value": "IDLE", "timestamp": 1700000100},
    }
    _patch_sitewise(monkeypatch, models=_SW_MODELS, assets=assets, values=values)
    sec = er.render_section(_SW_SYSTEM, _SW_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert sec["rows"][0][1] is None            # errored property → empty cell
    assert sec["rows"][0][2] == "IDLE"


def test_sitewise_no_matching_model_is_error_naming_connection_type(monkeypatch):
    _patch_sitewise(monkeypatch, models=[{"id": "m", "name": "OtherModel", "properties": []}],
                    assets=[], values={})
    sec = er.render_section(_SW_SYSTEM, _SW_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0
    assert "SiteWise (Direct)" in sec["error"]


def test_sitewise_boundary_error_never_raises(monkeypatch):
    monkeypatch.setattr("agents.metric_resolver._full_connection_config", lambda sid: {})

    def boom(cfg, region):
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr("agents.sitewise_read.list_asset_models_with_properties", boom)
    sec = er.render_section(_SW_SYSTEM, _SW_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert sec["rows"] == []
    assert "SiteWise (Direct)" in sec["error"]


# ---------------------------------------------------------------------------
# _format_epoch_seconds — pure helper
# ---------------------------------------------------------------------------

def test_format_epoch_seconds():
    assert er._format_epoch_seconds(None) == ""
    assert er._format_epoch_seconds("not-a-number") == ""
    out = er._format_epoch_seconds(1700000000)
    assert out.startswith("2023-11-14T") and out.endswith("+00:00")
