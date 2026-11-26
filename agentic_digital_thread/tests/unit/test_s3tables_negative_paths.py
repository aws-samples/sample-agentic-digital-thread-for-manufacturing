# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refusals on the S3 Tables metric path: the specific error, and no connector call.

A refused statement or an invalid identifier must stop BEFORE anything reaches
the S3 Tables connector. Offline: the connector helper is replaced by a spy.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.connection_types as ct
import agents.duckdb_tools as dt
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation
from agents.metric_resolver import MetricResolverError

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYSTEM = "s3t-neg"


@pytest.fixture
def spy(monkeypatch):
    calls = []
    monkeypatch.setattr(mr.duckdb_tools, "run_select",
                        lambda *a, **k: calls.append(a) or {"rows": [[1, 1]]})
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.S3TABLES_DUCKDB})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    return calls


def _config(monkeypatch, **cfg):
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"table_bucket_arn": ARN, **cfg})


def test_a_hostile_namespace_is_refused_before_any_connector_call(monkeypatch, spy):
    _config(monkeypatch, namespaces=['x"; DROP TABLE t; --'])
    with pytest.raises(ValueError, match="Invalid identifier"):
        mr.plan_schema(SYSTEM, "t")
    assert spy == []


def test_a_hostile_table_name_never_reaches_the_connector(monkeypatch, spy):
    plan = mr.QueryPlan(system_id=SYSTEM, schema="plant", table='t"; DROP',
                        column="qty", aggregation=Aggregation.SUM)
    with pytest.raises(ValueError, match="Invalid identifier"):
        mr._compose_sql(plan)
    assert spy == []


def test_a_hostile_grouped_namespace_never_reaches_the_connector(monkeypatch, spy):
    plan = md.GroupedQueryPlan(
        system_id=SYSTEM, schema='p"; DROP', measure_table="m", measure_column="c",
        aggregation=Aggregation.SUM, dimension_table="d", dimension_column="x")
    with pytest.raises(ValueError, match="Invalid identifier"):
        md._compose_grouped_sql(plan)
    assert spy == []


def test_a_hostile_dimension_namespace_never_reaches_the_connector(monkeypatch, spy):
    plan = md.GroupedQueryPlan(
        system_id=SYSTEM, schema="plant", dimension_schema='r"; DROP',
        measure_table="m", measure_column="c", aggregation=Aggregation.SUM,
        dimension_table="d", dimension_column="x",
        join=md.JoinPathProvenance(None, "m", "k", "d", "k"))
    with pytest.raises(ValueError, match="Invalid identifier"):
        md._compose_grouped_sql(plan)
    assert spy == []


def test_two_candidate_namespaces_for_one_table_fail_loud(monkeypatch, spy):
    _config(monkeypatch, tables=["a.one", "b.one"])
    with pytest.raises(MetricResolverError, match="no single S3 Tables namespace"):
        mr.plan_schema(SYSTEM, "one")


def test_a_table_outside_the_scope_has_no_namespace(monkeypatch, spy):
    _config(monkeypatch, namespaces=["a"], tables=["a.one"])
    with pytest.raises(MetricResolverError, match="no single S3 Tables namespace"):
        mr.plan_schema(SYSTEM, "unlisted")


# --- the connector's own refusals carry the specific message -------------------

def _bucket():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.plant")
    con.execute("CREATE TABLE s3t.plant.t(x INTEGER)")
    return con


def _select(cfg, sql):
    return dt.run_select({"table_bucket_arn": ARN, **cfg}, sql,
                         connection_factory=_bucket)


@pytest.mark.parametrize("sql,reason", [
    ("DROP TABLE s3t.plant.t", "read-only"),
    ("SELECT * FROM read_csv('/etc/passwd')", "table function"),
    ("SELECT x FROM main.other", "outside the s3t catalog"),
    ("SELECT x FROM s3t.plant.t; SELECT 1", "single"),
])
def test_refusals_name_the_reason(sql, reason):
    with pytest.raises(dt.QueryError) as exc:
        _select({}, sql)
    assert reason in str(exc.value).lower() or "not allowed" in str(exc.value)


def test_a_namespace_outside_the_scope_is_named_as_out_of_scope():
    with pytest.raises(dt.QueryError, match="outside this system's configured scope"):
        _select({"namespaces": ["only"]}, "SELECT x FROM s3t.plant.t")
