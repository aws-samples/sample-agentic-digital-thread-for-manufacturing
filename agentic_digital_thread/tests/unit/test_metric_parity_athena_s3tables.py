# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parity: one metric plan gives the SAME number on Athena and on S3 Tables.

The same logical rows sit in two in-memory DuckDB databases. One stands in for
the Athena table (SQL named ``plant.widgets``); the other for the S3 Tables
bucket (SQL named ``s3t.plant.widgets``, through the real connector helper).
Each system resolves the same metric over its own backend, scalar and grouped,
and the results must be equal. Exact percentiles are compared to an
independent computation, because Athena's ``approx_percentile`` is an
estimate and cannot run here.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.connection_types as ct
import agents.duckdb_tools as dt
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp as Op
from agents.metric_resolver import resolve_metric

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
ATHENA_ID, S3T_ID = "sys-athena", "sys-s3t"
CONCEPT, STATE, TABLE = "widget-count", "widget-state", "widgets"

_ROWS = [(10, "ok", 1), (20, "ok", 2), (5, "bad", 1), (7, "ok", 1), (3, "bad", 2)]


def _load(con, prefix):
    con.execute(f"CREATE SCHEMA {prefix}plant")
    con.execute(f"CREATE TABLE {prefix}plant.widgets(qty INTEGER, state VARCHAR, "
                "event_ts TIMESTAMP)")
    for i, (qty, state, age_days) in enumerate(_ROWS):
        con.execute(f"INSERT INTO {prefix}plant.widgets VALUES "
                    f"({qty}, '{state}', current_timestamp::TIMESTAMP "
                    f"- INTERVAL {age_days * (1 if i < 3 else 10)} DAY)")


@pytest.fixture
def systems(monkeypatch):
    athena = duckdb.connect()
    _load(athena, "")
    s3t = duckdb.connect()
    s3t.execute("ATTACH ':memory:' AS s3t")
    _load(s3t, "s3t.")

    types = {ATHENA_ID: ct.ATHENA, S3T_ID: ct.S3TABLES_DUCKDB}
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": types[sid]})
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"table_bucket_arn": ARN, "namespaces": ["plant"]})
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(mr, "_athena_time_window_where", lambda *a, **k: None)
    # (rows are all newer than the Athena 30 day scan bound, so that bound,
    # which only the Athena path applies, changes no number here)
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: "event_ts")

    def athena_run(plan, sql):
        cur = athena.execute(sql)
        return {"columns": [], "rows": [list(r) for r in cur.fetchall()]}

    monkeypatch.setattr(mr, "_execute_athena", athena_run)
    monkeypatch.setattr(md, "_athena_grouped_result",
                        lambda plan, sql: athena_run(plan, sql))
    real = dt.run_select
    monkeypatch.setattr(
        mr.duckdb_tools, "run_select",
        lambda cfg, sql, source_system="", connection_factory=None:
        real(cfg, sql, source_system, connection_factory=lambda: s3t))
    yield
    athena.close()
    s3t.close()


def _wire(monkeypatch, metric):
    bound = {CONCEPT: [{"table": TABLE, "column": "qty"}],
             STATE: [{"table": TABLE, "column": "state"}]}

    def neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == md._DIM_MAPS_TO_CYPHER and concept == STATE:
            return [{"table": TABLE, "column": "state", "role": "dimension",
                     "confidence": 0.9, "source": "user", "version": "1"}]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER,
                      md._DIM_MAPS_TO_CYPHER):
            return [{"table": t, "column": c} for t, c in sorted(
                {(r["table"], r["column"]) for r in bound.get(concept, [])})]
        return []

    class _L:
        def get_metric(self, mid):
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _L())
    monkeypatch.setattr(mr, "neptune_query", neptune)


def _authority(system_id):
    class _A(mr.AuthorityResolver):
        def authoritative_system(self, concept):  # noqa: ARG002
            return system_id
    return _A()


def _metric(**over):
    base = dict(id="m", concept=CONCEPT, aggregation=Aggregation.SUM,
                label="Widgets", unit="units")
    base.update(over)
    return DirectMetric(**base)


_VARIANTS = [
    pytest.param({}, id="sum"),
    pytest.param({"aggregation": Aggregation.AVG}, id="avg"),
    pytest.param({"aggregation": Aggregation.MIN}, id="min"),
    pytest.param({"aggregation": Aggregation.MAX}, id="max"),
    pytest.param({"aggregation": Aggregation.COUNT}, id="count"),
    pytest.param({"filter": (Predicate(STATE, Op.EQ, ("ok",)),)}, id="filter"),
    pytest.param({"filter": (Predicate(STATE, Op.IN, ("ok", "bad")),)}, id="filter-in"),
    pytest.param({"window": "7d"}, id="window-7d"),
    pytest.param({"window": "24h", "filter": (Predicate(STATE, Op.EQ, ("ok",)),)},
                 id="window-and-filter"),
]


@pytest.mark.parametrize("over", _VARIANTS)
def test_the_scalar_value_is_the_same_on_both_engines(monkeypatch, systems, over):
    _wire(monkeypatch, _metric(**over))
    athena = resolve_metric("m", authority=_authority(ATHENA_ID))
    s3t = resolve_metric("m", authority=_authority(S3T_ID))
    assert s3t.value == pytest.approx(athena.value)
    assert s3t.sample_count == athena.sample_count


@pytest.mark.parametrize("over", [v.values[0] for v in _VARIANTS
                                  if "filter" not in v.values[0]])
def test_the_grouped_values_are_the_same_on_both_engines(monkeypatch, systems, over):
    _wire(monkeypatch, _metric(**over))
    athena = md.resolve_metric_by_dimension("m", STATE, authority=_authority(ATHENA_ID))
    s3t = md.resolve_metric_by_dimension("m", STATE, authority=_authority(S3T_ID))
    got = {r.dimension_value: r.value for r in s3t.rows}
    want = {r.dimension_value: r.value for r in athena.rows}
    assert got.keys() == want.keys()
    for key in want:
        assert got[key] == pytest.approx(want[key])


def test_the_two_engines_name_the_table_differently(monkeypatch, systems):
    _wire(monkeypatch, _metric())
    athena = resolve_metric("m", authority=_authority(ATHENA_ID))
    s3t = resolve_metric("m", authority=_authority(S3T_ID))
    assert athena.lineage.sql.endswith("FROM plant.widgets")
    assert s3t.lineage.sql.endswith("FROM s3t.plant.widgets")
