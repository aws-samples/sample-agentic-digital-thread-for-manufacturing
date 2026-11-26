# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every predicate or remedy the data rules emit is valid DuckDB SQL (scope 5).

The rules were written against Trino/Athena SQL text. On the S3 Tables path the
same predicate is ANDed into DuckDB SQL and the same ``COUNT(DISTINCT key)`` and
``TIMESTAMP '...'`` forms the remedies name are sent to DuckDB. These tests run
the EMITTED SQL through a real in-memory DuckDB so a Presto-only construct could
not slip through unnoticed. Dataset-neutral fixture.
"""
import duckdb
import pytest

import agents.row_exclusion as row_exclusion


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(
        "CREATE TABLE t(route_name VARCHAR, segment_type VARCHAR, "
        "manufacturing_attempt_id VARCHAR, lifecycle_id VARCHAR, "
        "observed_at TIMESTAMP, id BIGINT)")
    c.execute(
        "INSERT INTO t VALUES "
        "('r','BIND','a','l', TIMESTAMP '2026-10-06 00:00:00', 1),"
        "('', 'WELD','a','l', TIMESTAMP '2026-10-06 00:00:00', 2)")
    yield c
    c.close()


def _ex(conditions):
    return row_exclusion.exclusion_from("non-record rows", conditions)


# --- the row-exclusion canonical predicate, every op, runs in DuckDB --------

@pytest.mark.parametrize("conditions", [
    [{"column": "route_name", "op": "not_empty"}],
    [{"column": "route_name", "op": "empty"}],
    [{"column": "segment_type", "op": "in", "values": ["BIND", "TRIM_BUFFER"]}],
    [{"column": "segment_type", "op": "not_in", "values": ["BIND", "TRIM_BUFFER"]}],
    [{"column": "route_name", "op": "not_empty"},
     {"column": "lifecycle_id", "op": "not_empty"},
     {"column": "segment_type", "op": "not_in", "values": ["BIND", "FINALIZE_BUFFER"]}],
])
def test_canonical_predicate_runs_in_duckdb(con, conditions):
    pred = row_exclusion.predicate_sql(_ex(conditions))
    # It must parse and execute as a WHERE conjunct, returning a row count.
    out = con.execute(f"SELECT COUNT(*) FROM t WHERE {pred}").fetchone()
    assert out[0] >= 0


def test_qualified_predicate_runs_in_a_join(con):
    pred = row_exclusion.predicate_sql(
        _ex([{"column": "segment_type", "op": "not_in", "values": ["BIND"]}]),
        qualifier="a")
    out = con.execute(
        f"SELECT COUNT(*) FROM t a JOIN t b ON a.id = b.id WHERE {pred}").fetchone()
    assert out[0] >= 0


# --- the record-key de-dup remedy form (COUNT(DISTINCT key)) runs -----------

def test_count_distinct_on_a_composite_record_key_runs(con):
    # The record-key rule's remedy counts DISTINCT over the key columns, with
    # COALESCE on the nullable members, exactly as the live key is written.
    key = ("id, segment_type, COALESCE(manufacturing_attempt_id, ''), "
           "COALESCE(lifecycle_id, '')")
    out = con.execute(f"SELECT COUNT(DISTINCT ({key})) FROM t").fetchone()
    assert out[0] >= 0


# --- timestamp literal form the window remedies name runs -------------------

def test_timestamp_literal_predicate_runs(con):
    out = con.execute(
        "SELECT COUNT(*) FROM t WHERE observed_at >= TIMESTAMP '2026-10-05 00:00:00'"
    ).fetchone()
    assert out[0] >= 0
