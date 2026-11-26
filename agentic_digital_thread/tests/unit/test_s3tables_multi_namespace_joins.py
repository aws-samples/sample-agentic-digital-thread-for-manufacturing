# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Joined grouped metrics and heatmaps resolve each table's OWN namespace.

The config scopes ``facts.measurements`` and ``reference.assets``; a join
between them must read ``s3t.facts.measurements`` and ``s3t.reference.assets``,
never ``s3t.facts.assets``. Executed on an in-memory DuckDB stand-in.
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
SYSTEM = "s3t-multi"
CONFIG = {"table_bucket_arn": ARN, "namespaces": ["facts", "reference"],
          "tables": ["facts.measurements", "reference.assets"]}
JOIN = md.JoinPathProvenance(None, "measurements", "asset_id", "assets", "asset_id")


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return SYSTEM


def _bucket():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.facts")
    con.execute("CREATE SCHEMA s3t.reference")
    con.execute("CREATE TABLE s3t.facts.measurements(asset_id INTEGER, qty INTEGER, "
                "line VARCHAR)")
    con.execute("CREATE TABLE s3t.reference.assets(asset_id INTEGER, name VARCHAR, "
                "usl DOUBLE)")
    con.execute("INSERT INTO s3t.facts.measurements VALUES (1, 10, 'L1'), (1, 5, 'L1'), "
                "(2, 7, 'L1')")
    con.execute("INSERT INTO s3t.reference.assets VALUES (1, 'press', 20), (2, 'saw', 9)")
    return con


@pytest.fixture
def wired(monkeypatch):
    con = _bucket()
    seen = {"sql": []}
    real = dt.run_select

    def run_select(cfg, sql, source_system="", connection_factory=None):
        seen["sql"].append(sql)
        return real(cfg, sql, source_system, connection_factory=lambda: con)

    monkeypatch.setattr(mr.duckdb_tools, "run_select", run_select)
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.S3TABLES_DUCKDB})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(CONFIG))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    yield seen
    con.close()


def _plan(**over):
    base = dict(system_id=SYSTEM, schema="facts", dimension_schema="reference",
                measure_table="measurements", measure_column="qty",
                aggregation=Aggregation.SUM, dimension_table="assets",
                dimension_column="name", join=JOIN)
    base.update(over)
    return md.GroupedQueryPlan(**base)


def test_the_joined_grouped_sql_names_each_tables_namespace(wired):
    sql = md._grouped_sql(_plan())
    assert 'FROM "s3t"."facts"."measurements" m JOIN "s3t"."reference"."assets" d' in sql
    assert md._execute_grouped(_plan(), sql) == [("press", 15), ("saw", 7)]


def test_the_joined_latest_shape_names_each_namespace(wired):
    plan = _plan(aggregation=Aggregation.LATEST, order_column="qty")
    sql = md._grouped_sql(plan)
    assert '"s3t"."facts"."measurements" m JOIN "s3t"."reference"."assets" d' in sql
    assert dict(md._execute_grouped(plan, sql)) == {"press": 10, "saw": 7}


def test_the_plan_builder_resolves_each_tables_namespace(monkeypatch, wired):
    metric = DirectMetric(id="m", concept="c", aggregation=Aggregation.SUM,
                          label="L", unit="u")
    monkeypatch.setattr(md, "_resolve_order_column", lambda *a, **k: None)
    auth = mr.AuthoritySource(system_id=SYSTEM, source="authority_rule")
    measure = mr.MappingProvenance(table="measurements", column="qty", role=None,
                                   confidence=None, source="x")
    dimension = mr.MappingProvenance(table="assets", column="name", role=None,
                                     confidence=None, source="x")
    plan = md._build_grouped_plan(metric, "m", auth, measure, dimension, JOIN, None)
    assert (plan.schema, plan.dimension_schema) == ("facts", "reference")


def test_a_colocated_plan_has_no_separate_dimension_namespace(monkeypatch, wired):
    metric = DirectMetric(id="m", concept="c", aggregation=Aggregation.SUM,
                          label="L", unit="u")
    monkeypatch.setattr(md, "_resolve_order_column", lambda *a, **k: None)
    auth = mr.AuthoritySource(system_id=SYSTEM, source="authority_rule")
    measure = mr.MappingProvenance(table="measurements", column="qty", role=None,
                                   confidence=None, source="x")
    plan = md._build_grouped_plan(metric, "m", auth, measure, measure, None, None)
    assert plan.dimension_schema is None


def test_the_dimension_namespace_stays_out_of_postgres_lineage():
    plan = md.GroupedQueryPlan(
        system_id="sys-pg", schema="plant", measure_table="m", measure_column="c",
        aggregation=Aggregation.SUM, dimension_table="d", dimension_column="x")
    assert "dimension_schema" not in md._row_lineage_to_dict(
        _lineage_for(plan))["query_plan"]


def _lineage_for(plan):
    fields = md.RowLineage.__dataclass_fields__
    kwargs = {name: None for name in fields}
    kwargs["query_plan"] = plan
    return md.RowLineage(**kwargs)


def test_a_postgres_joined_shape_keeps_one_schema(monkeypatch):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.POSTGRESQL})
    plan = _plan(system_id="sys-pg", schema="plant", dimension_schema=None)
    sql = md._compose_grouped_sql(plan)
    assert 'FROM "plant"."measurements" m JOIN "plant"."assets" d' in sql


def test_the_athena_joined_shape_keeps_one_schema(monkeypatch):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.ATHENA})
    plan = _plan(system_id="sys-lake", schema="lake", dimension_schema=None)
    sql = md._compose_grouped_sql(plan)
    assert 'FROM "lake"."measurements" m JOIN "lake"."assets" d' in sql


# --- heatmap ------------------------------------------------------------------

def _hm_plan(**over):
    base = dict(system_id=SYSTEM, schema="facts", threshold_schema="reference",
                measure_table="measurements", measure_column="qty",
                aggregation=Aggregation.SUM, dim_x_column="asset_id",
                dim_y_column="line", threshold_table="assets", threshold_column="usl",
                threshold_join=JOIN)
    base.update(over)
    return hm.HeatmapPlan(**base)


def test_the_joined_heatmap_names_each_tables_namespace(wired):
    plan = _hm_plan()
    sql = hm._compose_heatmap_sql(plan)
    assert ('FROM "s3t"."facts"."measurements" m '
            'JOIN "s3t"."reference"."assets" t') in sql
    rows = hm._execute_heatmap(plan, sql, has_threshold=True)
    assert sorted((r[0], r[2], r[3]) for r in rows) == [(1, 15, 20.0), (2, 7, 9.0)]


def test_the_joined_latest_heatmap_names_each_namespace(wired):
    plan = _hm_plan(aggregation=Aggregation.LATEST, order_column="qty")
    sql = hm._compose_heatmap_sql(plan)
    assert '"s3t"."reference"."assets" t' in sql


def test_the_heatmap_threshold_namespace_is_resolved_from_the_scope(wired):
    assert hm._threshold_schema(SYSTEM, "assets", "measurements") == "reference"
    assert hm._threshold_schema(SYSTEM, "measurements", "measurements") is None
    assert hm._threshold_schema(SYSTEM, None, "measurements") is None


def test_a_postgres_heatmap_keeps_one_schema(monkeypatch):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.POSTGRESQL})
    sql = hm._compose_heatmap_sql(_hm_plan(system_id="sys-pg", schema="plant",
                                           threshold_schema=None))
    assert 'FROM "plant"."measurements" m JOIN "plant"."assets" t' in sql
