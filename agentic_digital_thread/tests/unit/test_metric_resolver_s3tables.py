# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SCALAR metric resolver on an S3 Tables system.

A metric bound to an S3 Tables system resolves through the S3 Tables
connector (``duckdb_tools.run_select``) with the system's FULL connection
config. The composed SQL names the table ``s3t.<namespace>.<table>``. Every
test runs the composed SQL on an in-memory DuckDB standing in for the bucket,
so SQL that is not valid DuckDB fails here. Offline: graph, catalog and
connector seams are monkeypatched on ``agents.metric_resolver``.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.connection_types as ct
import agents.duckdb_tools as dt
import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp as Op
from agents.metric_measure_rule import DurationMeasure, FieldRef
from agents.metric_resolver import (
    AuthorityResolver,
    MetricResolverError,
    StructuralMetricError,
    resolve_metric,
)

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
ROLE = "arn:aws:iam::444455556666:role/data-reader"
SYSTEM = "s3t-sys"
CONCEPT = "widget-count"
TIME = "event-time"
STATE = "widget-state"
TABLE = "widgets"
FULL_CONFIG = {"table_bucket_arn": ARN, "catalog_endpoint": "glue",
               "role_arn": ROLE, "external_id": "ext-1",
               "namespaces": ["plant"]}


class _Auth(AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return SYSTEM


def _bucket():
    """An in-memory DuckDB with ``s3t.plant.widgets`` (the bucket stand-in)."""
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.plant")
    con.execute(
        "CREATE TABLE s3t.plant.widgets(qty INTEGER, state VARCHAR, "
        "start_ts TIMESTAMP, end_ts TIMESTAMP, event_ts TIMESTAMP)")
    con.execute(
        "INSERT INTO s3t.plant.widgets VALUES "
        "(10, 'ok',  '2026-01-01 10:00:00', '2026-01-01 10:00:30', "
        " current_timestamp::TIMESTAMP - INTERVAL 1 HOUR), "
        "(20, 'ok',  '2026-01-01 10:00:00', '2026-01-01 10:01:00', "
        " current_timestamp::TIMESTAMP - INTERVAL 2 DAY), "
        "(5,  'bad', '2026-01-01 10:00:00', '2026-01-01 10:02:00', "
        " current_timestamp::TIMESTAMP - INTERVAL 40 DAY)")
    return con


@pytest.fixture
def wired(monkeypatch):
    """Wire the resolver to the in-memory bucket; capture config + SQL."""
    con = _bucket()
    seen = {"sql": [], "cfg": []}
    real = dt.run_select

    def run_select(cfg, sql, source_system="", connection_factory=None):
        seen["sql"].append(sql)
        seen["cfg"].append(cfg)
        return real(cfg, sql, source_system, connection_factory=lambda: con)

    monkeypatch.setattr(mr.duckdb_tools, "run_select", run_select)
    _on(monkeypatch, ct.S3TABLES_DUCKDB)
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(FULL_CONFIG))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    monkeypatch.setattr(mr, "_athena_recent_partition_where", _boom("athena prune"))
    monkeypatch.setattr(mr, "_athena_time_window_where", _boom("athena window"))
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: "event_ts")
    yield seen
    con.close()


def _on(monkeypatch, connection_type):
    """Make ``connection_type`` the system's type for every resolver read."""
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": connection_type})


def _boom(what):
    def fail(*_a, **_k):
        raise AssertionError(f"the Athena-only {what} path ran for an S3 Tables system")
    return fail


def _layer(monkeypatch, metric, bound):
    registry = {metric.id: metric}

    def neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._FIELD_TYPE_CYPHER:
            return [{"data_type": "timestamp(6)"}]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER):
            table = (params or {}).get("table")
            return [{"table": t, "column": c} for t, c in sorted(
                {(r["table"], r["column"]) for r in bound.get(concept, [])
                 if table is None or r["table"] == table})][
                :mb.BINDING_CANDIDATE_READ_LIMIT]
        return []

    class _L:
        def get_metric(self, mid):
            return registry[mid]

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _L())
    monkeypatch.setattr(mr, "neptune_query", neptune)


_BOUND = {CONCEPT: [{"table": TABLE, "column": "qty"}],
          STATE: [{"table": TABLE, "column": "state"}],
          TIME: [{"table": TABLE, "column": "start_ts"},
                 {"table": TABLE, "column": "end_ts"}]}


def _sum(**over):
    base = dict(id="m-sum", concept=CONCEPT, aggregation=Aggregation.SUM,
                label="Widgets", unit="units")
    base.update(over)
    return DirectMetric(**base)


# --- execution ---------------------------------------------------------------

def test_metric_on_an_s3_tables_system_resolves(monkeypatch, wired):
    _layer(monkeypatch, _sum(), _BOUND)
    rm = resolve_metric("m-sum", authority=_Auth())
    assert rm.value == 35 and rm.sample_count == 3
    assert wired["sql"] == ["SELECT SUM(qty), COUNT(qty) FROM s3t.plant.widgets"]
    assert rm.lineage.sql == wired["sql"][0]


def test_the_full_connection_config_reaches_the_connector(monkeypatch, wired):
    _layer(monkeypatch, _sum(), _BOUND)
    resolve_metric("m-sum", authority=_Auth())
    cfg = wired["cfg"][0]
    assert cfg["role_arn"] == ROLE and cfg["external_id"] == "ext-1"
    assert cfg["catalog_endpoint"] == "glue" and cfg["table_bucket_arn"] == ARN


@pytest.mark.parametrize("agg,expected", [
    (Aggregation.AVG, 35 / 3), (Aggregation.MIN, 5), (Aggregation.MAX, 20),
    (Aggregation.COUNT, 3)])
def test_every_aggregation_runs(monkeypatch, wired, agg, expected):
    _layer(monkeypatch, _sum(aggregation=agg), _BOUND)
    assert resolve_metric("m-sum", authority=_Auth()).value == pytest.approx(expected)


def test_a_connector_refusal_becomes_a_resolver_error(monkeypatch, wired):
    _layer(monkeypatch, _sum(), {CONCEPT: [{"table": TABLE, "column": "missing"}]})
    with pytest.raises(MetricResolverError):
        resolve_metric("m-sum", authority=_Auth())


# --- namespace / table reference ---------------------------------------------

def test_namespace_comes_from_the_single_configured_namespace(monkeypatch):
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.S3TABLES_DUCKDB)
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"namespaces": ["plant"]})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    assert mr.plan_schema(SYSTEM) == "plant"


def test_namespace_comes_from_the_scoped_table_entry(monkeypatch):
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.S3TABLES_DUCKDB)
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"namespaces": ["a", "b"],
                                     "tables": ["a.one", "b.two"]})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    assert mr.plan_schema(SYSTEM, "two") == "b"
    assert mr.plan_schema(SYSTEM, "ONE") == "a"


def test_an_ambiguous_namespace_fails_loud(monkeypatch):
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.S3TABLES_DUCKDB)
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"namespaces": ["a", "b"]})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    with pytest.raises(MetricResolverError, match="namespace"):
        mr.plan_schema(SYSTEM, "one")


def test_no_scope_and_no_catalog_namespace_fails_loud(monkeypatch):
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.S3TABLES_DUCKDB)
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    with pytest.raises(MetricResolverError, match="namespace"):
        mr.plan_schema(SYSTEM, "one")


def test_a_hostile_namespace_is_refused(monkeypatch):
    monkeypatch.setattr(mr, "_connection_type", lambda sid: ct.S3TABLES_DUCKDB)
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"namespaces": ['x"; DROP']})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    with pytest.raises(ValueError, match="Invalid identifier"):
        mr.plan_schema(SYSTEM, "one")


# --- predicates --------------------------------------------------------------

def test_a_predicate_binds_and_runs_on_s3_tables(monkeypatch, wired):
    metric = _sum(filter=(Predicate(STATE, Op.EQ, ("ok",)),))
    _layer(monkeypatch, metric, _BOUND)
    rm = resolve_metric("m-sum", authority=_Auth())
    assert rm.value == 30
    assert wired["sql"] == [
        "SELECT SUM(qty), COUNT(qty) FROM s3t.plant.widgets "
        "WHERE \"state\" = 'ok'"]


# --- window ------------------------------------------------------------------

def test_a_windowed_metric_applies_the_event_time_bound(monkeypatch, wired):
    _layer(monkeypatch, _sum(window="7d"), _BOUND)
    rm = resolve_metric("m-sum", authority=_Auth())
    assert rm.value == 30  # the 40 day old row is outside 7d
    assert rm.lineage.window == "7d" and rm.lineage.window_column == "event_ts"
    assert "INTERVAL '7' DAY" in wired["sql"][0]


# --- duration percentile -----------------------------------------------------

_MEASURE = DurationMeasure(kind="duration", start=FieldRef(TIME, "start_ts"),
                           end=FieldRef(TIME, "end_ts"), unit="seconds",
                           clamp_negative=True)


def test_a_duration_percentile_runs_exact_on_s3_tables(monkeypatch, wired):
    metric = _sum(id="m-p", aggregation=Aggregation.COUNT, measure=_MEASURE,
                  percentiles=(0.5, 0.9))
    _layer(monkeypatch, metric, _BOUND)
    rm = resolve_metric("m-p", authority=_Auth())
    assert [v for _p, v in rm.percentiles] == [60, 120]
    assert rm.sample_count == 3
    assert "quantile_disc" in wired["sql"][0]
    assert rm.lineage.approximate is False
    assert wired["sql"][0].endswith(
        'FROM s3t.plant.widgets WHERE "start_ts" IS NOT NULL '
        'AND "end_ts" IS NOT NULL')


def test_a_windowed_duration_percentile_bounds_the_end_column(monkeypatch, wired):
    metric = _sum(id="m-p", aggregation=Aggregation.COUNT, measure=_MEASURE,
                  percentiles=(0.5,), window="30d")
    _layer(monkeypatch, metric, _BOUND)
    rm = resolve_metric("m-p", authority=_Auth())
    assert rm.lineage.window_column == "end_ts"
    assert ('"end_ts" >= (current_timestamp - INTERVAL \'30\' DAY)'
            in wired["sql"][0])


def test_a_non_sql_backend_still_rejects_a_measure(monkeypatch, wired):
    metric = _sum(id="m-p", aggregation=Aggregation.COUNT, measure=_MEASURE,
                  percentiles=(0.5,))
    _layer(monkeypatch, metric, _BOUND)
    _on(monkeypatch, ct.MCP_SERVER)
    with pytest.raises(StructuralMetricError):
        resolve_metric("m-p", authority=_Auth())
