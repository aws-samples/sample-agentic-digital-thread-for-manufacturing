# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the grouped (metric-by-dimension) Athena routing.

``metric_dimension._execute_grouped`` mirrors ``metric_resolver._execute``:
PostgreSQL → ``query_pg``, Athena → ``athena_tools.run_athena_query`` with the
cross-account-safe ``_full_connection_config``, anything else → ``query_system``.
All connectors are mocked; synthetic names + AWS documentation placeholder
accounts (111122223333) only.
"""
from __future__ import annotations

import json

import pytest

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation
from agents.metric_resolver import MetricResolverError


def _plan(system_id="sys-lake"):
    return md.GroupedQueryPlan(
        system_id=system_id, schema="lake_db",
        measure_table="orders", measure_column="qty",
        aggregation=Aggregation.SUM,
        dimension_table="orders", dimension_column="status",
    )


_SQL = ('SELECT "status", SUM("qty") FROM "lake_db"."orders" '
        'GROUP BY "status" ORDER BY "status" LIMIT 200')


def _patch_catalog(monkeypatch, conn_type):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": conn_type})


def test_grouped_athena_routes_through_run_athena_query(monkeypatch):
    _patch_catalog(monkeypatch, "Athena")
    captured = {}
    full_config = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
                   "role_arn": "arn:aws:iam::111122223333:role/ReadOnly",
                   "external_id": "example-external-id"}

    def fake_cfg(sid):
        captured["config_system"] = sid
        return full_config

    monkeypatch.setattr(mr, "_full_connection_config", fake_cfg)

    def fake_run(sql, connection_config):
        captured["sql"], captured["config"] = sql, connection_config
        return {"columns": ["status", "sum"],
                "rows": [["CLOSED", 5], ["OPEN", 7]], "row_count": 2}

    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)
    monkeypatch.setattr(mr, "query_pg",
                        lambda *a, **k: pytest.fail("query_pg must NOT be called for Athena"))
    monkeypatch.setattr(mr, "query_system",
                        lambda *a, **k: pytest.fail("query_system must NOT be called for Athena"))

    out = md._execute_grouped(_plan(), _SQL)
    assert out == [("CLOSED", 5), ("OPEN", 7)]
    assert captured["sql"] == _SQL
    assert captured["config"] is full_config
    assert captured["config_system"] == "sys-lake"


def test_grouped_postgres_path_unchanged(monkeypatch):
    _patch_catalog(monkeypatch, "PostgreSQL")
    called = {}

    def fake_pg(schema, sql):
        called["schema"], called["sql"] = schema, sql
        return json.dumps({"rows": [["OPEN", 3]], "row_count": 1})

    monkeypatch.setattr(mr, "query_pg", fake_pg)
    monkeypatch.setattr(
        "agents.athena_tools.run_athena_query",
        lambda *a, **k: pytest.fail("Athena connector must NOT be called for PostgreSQL"))

    out = md._execute_grouped(_plan("sys-pg"), _SQL)
    assert out == [("OPEN", 3)]
    assert called["schema"] == "lake_db" and called["sql"] == _SQL


def test_grouped_other_types_still_route_query_system(monkeypatch):
    _patch_catalog(monkeypatch, "MCP Server")
    monkeypatch.setattr(mr, "query_system",
                        lambda sid, sql: {"rows": [["A", 1]], "row_count": 1})
    monkeypatch.setattr(
        "agents.athena_tools.run_athena_query",
        lambda *a, **k: pytest.fail("Athena connector must NOT be called for MCP Server"))
    out = md._execute_grouped(_plan("sys-mcp"), _SQL)
    assert out == [("A", 1)]


def test_grouped_athena_failure_wrapped_as_resolver_error(monkeypatch):
    _patch_catalog(monkeypatch, "Athena")
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {})

    def boom(sql, connection_config):
        raise RuntimeError("COLUMN_NOT_FOUND: qty")

    monkeypatch.setattr("agents.athena_tools.run_athena_query", boom)
    with pytest.raises(MetricResolverError) as exc:
        md._execute_grouped(_plan(), _SQL)
    # Generic client-facing message; raw DB detail stays server-side (chained).
    assert "grouped query failed" in str(exc.value)


# --- schema resolution in resolve_metric_by_dimension (empty catalog schema) ---
# Live-confirmed 500: an Athena system with an EMPTY catalog schema reached
# _compose_grouped_sql, where validate_identifier('') escaped as an opaque
# HTTP 500. The plan schema now resolves from the connection config's database
# (same helper as entity_render's Athena branch) and fails loud when neither
# source provides one.

def _wire_by_dimension(monkeypatch, *, conn_type, catalog_schema, config_db):
    """Wire resolve_metric_by_dimension up to plan construction with mocks."""
    from agents.metric_layer import DirectMetric, MetricKind

    metric = DirectMetric(id="m-widgets", label="Widgets", unit="count",
                          concept="widget-count", aggregation=Aggregation.SUM)

    class _Layer:
        def get_metric(self, mid):
            if mid != "m-widgets":
                raise KeyError(mid)
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())

    auth = type("A", (), {"system_id": "sys-lake", "concept": "widget-count",
                          "source": None})()
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    mapping = type("M", (), {"table": "orders", "column": "qty",
                             "role": "measure", "confidence": 0.9})()
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mapping)
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "get_schema", lambda sid: catalog_schema)
    monkeypatch.setattr(
        "agents.entity_render._athena_schema_from_config",
        lambda sid: config_db,
    )
    dim = type("D", (), {"table": "orders", "column": "status",
                         "role": "identifier", "confidence": 0.9})()
    monkeypatch.setattr(md, "_resolve_dimension_mappings", lambda *a, **k: [dim])
    monkeypatch.setattr(md, "_select_dimension_mapping", lambda *a, **k: (dim, None))
    # Keep the lane hermetic: the Athena partition-prune seam would otherwise
    # attempt connection-config resolution (Secrets Manager) — stub it to
    # "no pruning" (covered by test_athena_partition_pruning.py). Ditto the
    # second-tier time-window fallback (a graph read via _resolve_order_column).
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(md, "_athena_time_window_where", lambda *a, **k: None)
    executed = {}

    def fake_exec(plan, sql):
        executed["schema"] = plan.schema
        return [("OPEN", 7)]

    monkeypatch.setattr(md, "_execute_grouped", fake_exec)
    return executed


def test_by_dimension_empty_catalog_schema_resolves_from_athena_config(monkeypatch):
    executed = _wire_by_dimension(monkeypatch, conn_type="Athena",
                                  catalog_schema="", config_db="lake_db")
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    assert executed["schema"] == "lake_db"
    assert out.rows[0].dimension_value == "OPEN"


def test_by_dimension_both_schema_sources_missing_fails_loud_not_500(monkeypatch):
    _wire_by_dimension(monkeypatch, conn_type="Athena",
                       catalog_schema="", config_db="")
    with pytest.raises(MetricResolverError, match="no schema/database resolved"):
        md.resolve_metric_by_dimension("m-widgets", "widget-state")


def test_by_dimension_truthy_catalog_schema_never_consults_config(monkeypatch):
    executed = _wire_by_dimension(monkeypatch, conn_type="Athena",
                                  catalog_schema="warehouse", config_db="lake_db")
    monkeypatch.setattr(
        "agents.entity_render._athena_schema_from_config",
        lambda sid: pytest.fail("config must NOT be consulted when catalog schema set"),
    )
    md.resolve_metric_by_dimension("m-widgets", "widget-state")
    assert executed["schema"] == "warehouse"
