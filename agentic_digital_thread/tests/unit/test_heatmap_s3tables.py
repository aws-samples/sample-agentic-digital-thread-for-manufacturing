# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The heatmap resolver on an S3 Tables system.

The grid SQL names the table ``"s3t"."<namespace>"."<table>"`` and runs through
the S3 Tables connector; both the LATEST (DISTINCT ON) and the GROUP BY shapes
are executed on an in-memory DuckDB standing in for the bucket.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.connection_types as ct
import agents.duckdb_tools as dt
import agents.heatmap_resolver as hm
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYSTEM = "s3t-sys"
CONFIG = {"table_bucket_arn": ARN, "namespaces": ["plant"]}
CONCEPT = "sensor-reading"


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return SYSTEM


def _bucket():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.plant")
    con.execute("CREATE TABLE s3t.plant.readings(asset_id VARCHAR, line VARCHAR, "
                "value DOUBLE, reading_time TIMESTAMP, threshold DOUBLE)")
    con.execute(
        "INSERT INTO s3t.plant.readings VALUES "
        "('A1', 'L1', 90, '2026-01-01 10:00:00', 100), "
        "('A1', 'L1', 105, '2026-01-01 11:00:00', 100), "
        "('A2', 'L1', 50, '2026-01-01 10:00:00', 100)")
    return con


@pytest.fixture
def wired(monkeypatch):
    con = _bucket()
    seen = {"sql": []}
    real = dt.run_select

    def run_select(cfg, sql, source_system="", connection_factory=None):
        seen["sql"].append(sql)
        return real(cfg, sql, source_system, connection_factory=lambda: con)

    def neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._MEASURE_BINDING_CYPHER:
            return [{"table": "readings", "column": "value", "role": "measure",
                     "confidence": 0.9, "source": "fk"}]
        if concept in ("asset", "line", "limit"):
            col = {"asset": "asset_id", "line": "line", "limit": "threshold"}[concept]
            return [{"table": "readings", "column": col, "role": "dimension",
                     "confidence": 1.0, "source": "user_confirmed"}]
        if concept == "event-time":
            return [{"table": "readings", "column": "reading_time",
                     "role": "dimension", "confidence": 1.0, "source": "x"}]
        return []

    monkeypatch.setattr(mr.duckdb_tools, "run_select", run_select)
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.S3TABLES_DUCKDB})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(CONFIG))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    monkeypatch.setattr(mr, "neptune_query", neptune)
    monkeypatch.setattr(hm, "covered_concepts", lambda query=None: {CONCEPT})
    yield seen
    con.close()


def _metric(monkeypatch, aggregation):
    metric = DirectMetric(id="hm", concept=CONCEPT, aggregation=aggregation,
                          label="Reading", unit="units")

    class _L:
        def get_metric(self, mid):
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _L())


def test_a_latest_heatmap_resolves_on_s3_tables(monkeypatch, wired):
    _metric(monkeypatch, Aggregation.LATEST)
    env = hm.resolve_heatmap("hm", "asset", "line", "limit", authority=_Auth())
    assert env["status"] == "ok"
    assert env["rows"] == [["A1", "L1", 105, 100, "critical"],
                           ["A2", "L1", 50, 100, "ok"]]
    assert '"s3t"."plant"."readings"' in wired["sql"][0]
    assert "DISTINCT ON" in wired["sql"][0]


def test_an_aggregate_heatmap_resolves_on_s3_tables(monkeypatch, wired):
    _metric(monkeypatch, Aggregation.AVG)
    env = hm.resolve_heatmap("hm", "asset", "line", authority=_Auth())
    assert env["status"] == "ok"
    assert [r[:3] for r in env["rows"]] == [["A1", "L1", 97.5], ["A2", "L1", 50.0]]


def test_a_joined_threshold_shape_names_both_tables_in_the_catalog(wired):
    plan = hm.HeatmapPlan(
        system_id=SYSTEM, schema="plant", measure_table="readings",
        measure_column="value", aggregation=Aggregation.AVG,
        dim_x_column="asset_id", dim_y_column="line",
        threshold_table="limits", threshold_column="usl",
        threshold_join=md.JoinPathProvenance(None, "readings", "asset_id",
                                             "limits", "asset_id"))
    sql = hm._compose_heatmap_sql(plan)
    assert 'FROM "s3t"."plant"."readings" m JOIN "s3t"."plant"."limits" t' in sql


def test_a_postgres_heatmap_keeps_its_two_part_name(monkeypatch):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.POSTGRESQL})
    plan = hm.HeatmapPlan(
        system_id="sys-pg", schema="plant", measure_table="readings",
        measure_column="value", aggregation=Aggregation.AVG,
        dim_x_column="asset_id", dim_y_column="line")
    assert 'FROM "plant"."readings"' in hm._compose_heatmap_sql(plan)
