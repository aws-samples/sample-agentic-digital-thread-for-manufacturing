# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The GROUPED (metric by dimension) resolver on an S3 Tables system.

The composed grouped SQL names the table ``"s3t"."<namespace>"."<table>"`` and
runs through the S3 Tables connector. Every shape is executed on an in-memory
DuckDB standing in for the bucket. The Athena-only scan-cap machinery
(recent-partition prune, time-window fallback, exact split) must not run.
Offline: graph, catalog and connector seams are monkeypatched.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.connection_types as ct
import agents.duckdb_tools as dt
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp as Op
from agents.metric_measure_rule import DurationMeasure, FieldRef
from agents.metric_resolver import MetricResolverError, StructuralMetricError

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYSTEM = "s3t-sys"
CONCEPT = "widget-count"
STATE = "widget-state"
TIME = "event-time"
TABLE = "widgets"
CONFIG = {"table_bucket_arn": ARN, "namespaces": ["plant"],
          "role_arn": "arn:aws:iam::444455556666:role/data-reader",
          "external_id": "ext-1"}


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return SYSTEM


def _bucket():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.plant")
    con.execute(
        "CREATE TABLE s3t.plant.widgets(qty INTEGER, state VARCHAR, line_id INTEGER, "
        "start_ts TIMESTAMP, end_ts TIMESTAMP, event_ts TIMESTAMP)")
    con.execute(
        "INSERT INTO s3t.plant.widgets VALUES "
        "(10, 'ok',  1, '2026-01-01 10:00:00', '2026-01-01 10:00:30', "
        " current_timestamp::TIMESTAMP - INTERVAL 1 HOUR), "
        "(20, 'ok',  2, '2026-01-01 10:00:00', '2026-01-01 10:01:00', "
        " current_timestamp::TIMESTAMP - INTERVAL 2 DAY), "
        "(5,  'bad', 1, '2026-01-01 10:00:00', '2026-01-01 10:02:00', "
        " current_timestamp::TIMESTAMP - INTERVAL 40 DAY)")
    con.execute("CREATE TABLE s3t.plant.lines(line_id INTEGER, line_name VARCHAR)")
    con.execute("INSERT INTO s3t.plant.lines VALUES (1, 'L1'), (2, 'L2')")
    return con


@pytest.fixture
def wired(monkeypatch):
    con = _bucket()
    seen = {"sql": [], "cfg": []}
    real = dt.run_select

    def run_select(cfg, sql, source_system="", connection_factory=None):
        seen["sql"].append(sql)
        seen["cfg"].append(cfg)
        return real(cfg, sql, source_system, connection_factory=lambda: con)

    monkeypatch.setattr(mr.duckdb_tools, "run_select", run_select)
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.S3TABLES_DUCKDB})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(CONFIG))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "s3tables")
    for name in ("_athena_recent_partition_where", "_athena_time_window_where"):
        monkeypatch.setattr(mr, name, _boom(name))
    monkeypatch.setattr(md, "_athena_time_window_where", _boom("md window"))
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: "event_ts")
    yield seen
    con.close()


def _boom(what):
    def fail(*_a, **_k):
        raise AssertionError(f"Athena-only {what} ran for an S3 Tables system")
    return fail


_BOUND = {CONCEPT: [{"table": TABLE, "column": "qty"}],
          STATE: [{"table": TABLE, "column": "state"}],
          TIME: [{"table": TABLE, "column": "start_ts"},
                 {"table": TABLE, "column": "end_ts"}]}


def _layer(monkeypatch, metric):
    def neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._FIELD_TYPE_CYPHER:
            return [{"data_type": "timestamp(6)"}]
        if cypher == md._DIM_MAPS_TO_CYPHER and concept == STATE:
            return [{"table": TABLE, "column": "state", "role": "dimension",
                     "confidence": 0.9, "source": "user", "version": "1"}]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER,
                      md._DIM_MAPS_TO_CYPHER):
            table = (params or {}).get("table")
            return [{"table": t, "column": c} for t, c in sorted(
                {(r["table"], r["column"]) for r in _BOUND.get(concept, [])
                 if table is None or r["table"] == table})]
        return []

    class _L:
        def get_metric(self, mid):
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _L())
    monkeypatch.setattr(mr, "neptune_query", neptune)


def _metric(**over):
    base = dict(id="m", concept=CONCEPT, aggregation=Aggregation.SUM,
                label="Widgets", unit="units")
    base.update(over)
    return DirectMetric(**base)


def _by_group(grm):
    return {r.dimension_value: r.value for r in grm.rows}


# --- resolve_metric_by_dimension ----------------------------------------------

def test_grouped_metric_resolves_on_s3_tables(monkeypatch, wired):
    _layer(monkeypatch, _metric())
    grm = md.resolve_metric_by_dimension("m", STATE, authority=_Auth())
    assert _by_group(grm) == {"bad": 5, "ok": 30}
    assert wired["sql"] == [
        'SELECT "state", SUM("qty") FROM "s3t"."plant"."widgets" '
        f'GROUP BY "state" ORDER BY "state" LIMIT {md.MAX_GROUPS}']
    assert grm.query_plan.schema == "plant"
    assert wired["cfg"][0]["role_arn"] == CONFIG["role_arn"]
    assert all(r.lineage.scan is None for r in grm.rows)


def test_a_windowed_grouped_metric_applies_the_bound(monkeypatch, wired):
    _layer(monkeypatch, _metric(window="7d"))
    grm = md.resolve_metric_by_dimension("m", STATE, authority=_Auth())
    assert _by_group(grm) == {"ok": 30}  # the 40 day old 'bad' row is outside
    assert grm.rows[0].lineage.query_plan.window == "7d"


def test_a_grouped_predicate_runs_on_s3_tables(monkeypatch, wired):
    _layer(monkeypatch, _metric(filter=(Predicate(STATE, Op.EQ, ("ok",)),)))
    grm = md.resolve_metric_by_dimension("m", STATE, authority=_Auth())
    assert _by_group(grm) == {"ok": 30}


def test_a_latest_grouped_metric_runs_on_s3_tables(monkeypatch, wired):
    _layer(monkeypatch, _metric(aggregation=Aggregation.LATEST))
    grm = md.resolve_metric_by_dimension("m", STATE, authority=_Auth())
    assert _by_group(grm) == {"ok": 10, "bad": 5}


_MEASURE = DurationMeasure(kind="duration", start=FieldRef(TIME, "start_ts"),
                           end=FieldRef(TIME, "end_ts"), unit="seconds",
                           clamp_negative=True)


def test_a_grouped_duration_percentile_is_exact_on_s3_tables(monkeypatch, wired):
    _layer(monkeypatch, _metric(aggregation=Aggregation.COUNT, measure=_MEASURE,
                                percentiles=(0.5,)))
    grm = md.resolve_metric_by_dimension("m", STATE, authority=_Auth())
    by = {r.dimension_value: (r.percentiles, r.sample_count) for r in grm.rows}
    assert by == {"ok": (((0.5, 30),), 2), "bad": (((0.5, 120),), 1)}
    assert "quantile_disc" in wired["sql"][0]
    assert grm.query_plan.approximate is False


def test_a_grouped_measure_on_a_non_sql_backend_is_rejected(monkeypatch, wired):
    _layer(monkeypatch, _metric(aggregation=Aggregation.COUNT, measure=_MEASURE,
                                percentiles=(0.5,)))
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": ct.MCP_SERVER})
    with pytest.raises(StructuralMetricError):
        md.resolve_metric_by_dimension("m", STATE, authority=_Auth())


# --- composed shapes ----------------------------------------------------------

def _plan(**over):
    base = dict(system_id=SYSTEM, schema="plant", measure_table=TABLE,
                measure_column="qty", aggregation=Aggregation.SUM,
                dimension_table="lines", dimension_column="line_name",
                join=md.JoinPathProvenance(None, TABLE, "line_id", "lines", "line_id"))
    base.update(over)
    return md.GroupedQueryPlan(**base)


def test_a_joined_grouped_shape_runs_on_s3_tables(wired):
    plan = _plan()
    sql = md._grouped_sql(plan)
    assert 'FROM "s3t"."plant"."widgets" m JOIN "s3t"."plant"."lines" d' in sql
    assert md._execute_grouped(plan, sql) == [("L1", 15), ("L2", 20)]


def test_a_joined_latest_shape_runs_on_s3_tables(wired):
    plan = _plan(aggregation=Aggregation.LATEST, order_column="event_ts")
    rows = md._execute_grouped(plan, md._grouped_sql(plan))
    assert dict(rows) == {"L1": 10, "L2": 20}


def test_a_joined_measure_shape_runs_on_s3_tables(wired):
    plan = _plan(aggregation=Aggregation.COUNT, measure=_MEASURE,
                 percentiles=(0.5,), measure_casts={"start": "unzoned", "end": "unzoned"})
    rows = md._execute_grouped(plan, md._grouped_sql(plan))
    assert {r[0]: r[1] for r in rows} == {"L1": ((0.5, 30),), "L2": ((0.5, 60),)}


def test_a_grouped_failure_is_wrapped(wired, monkeypatch):
    plan = _plan(measure_column="nope", dimension_column="line_name", join=None,
                 dimension_table=TABLE)
    # The allowlisted sanitizer now wraps the engine failure as a safe client
    # reason ("rejected the query: ..."), which is the intended behaviour; it
    # must stay a safe client error with no path, ARN or URL.
    with pytest.raises(MetricResolverError,
                       match="S3 Tables rejected the query:") as exc:
        md._execute_grouped(plan, md._grouped_sql(plan))
    msg = str(exc.value)
    assert "arn:" not in msg and "://" not in msg and "/" not in msg


def test_a_scoped_grouped_read_never_narrows_like_athena(wired):
    values, sql, scan = md._execute_grouped_scoped(_plan())
    assert scan is None and dict(values) == {"L1": 15, "L2": 20}
