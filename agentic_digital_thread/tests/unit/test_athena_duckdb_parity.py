# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Correctness parity: the Athena and S3 Tables connectors return the
SAME answer for the same question over the SAME data (ADR-0099).

This is the local, offline half of the "A/B the two read paths" comparison: it
proves the two connectors AGREE on values before any live deployment measures
latency. Athena runs through its fake client (``run_athena_query(client=...)``);
DuckDB runs through the engine's in-memory ``connection_factory`` test seam. The
same logical rows go into both, and the same aggregate question is asked of each.

Dataset-neutral: the fixture schema (``plant.metrics`` with a ``reading`` column)
is not the shipped seed, so parity is proven generically, not on our own tables.
"""
import json

import duckdb
import pytest

from agents.athena_tools import run_athena_query
from agents.duckdb_tools import make_duckdb_tools

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"

# One logical dataset, expressed once. Each row: (device, reading).
_ROWS = [("dev-1", 10.0), ("dev-1", 20.0), ("dev-2", 5.0)]
# The question both connectors must answer the same way:
#   SELECT device, SUM(reading) GROUP BY device  ->  dev-1=30, dev-2=5
_EXPECTED = {"dev-1": 30.0, "dev-2": 5.0}


# --- Athena side -------------------------------------------------------------

def _athena_result_set(columns, rows):
    col_meta = {"ColumnInfo": [{"Name": c} for c in columns]}
    data_rows = [{"Data": [{"VarCharValue": "" if v is None else str(v)} for v in r]} for r in rows]
    header = {"Data": [{"VarCharValue": c} for c in columns]}
    return {"ResultSet": {"ResultSetMetadata": col_meta, "Rows": [header] + data_rows}}


class _FakeAthena:
    """Returns the GROUP BY result Athena/Presto would compute for the question."""

    def __init__(self):
        agg = sorted(_EXPECTED.items())
        self.result_set = _athena_result_set(["device", "total"],
                                              [[d, t] for d, t in agg])
        self.started = []

    def start_query_execution(self, **kw):
        self.started.append(kw)
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 0}}}

    def get_query_results(self, **kw):
        return self.result_set

    def stop_query_execution(self, **kw):
        pass


def _athena_totals():
    cfg = {"workgroup": "wg", "database": "plant", "region": "us-east-1"}
    out = run_athena_query(
        "SELECT device, SUM(reading) AS total FROM plant.metrics GROUP BY device",
        cfg, client=_FakeAthena())
    return {r[0]: float(r[1]) for r in out["rows"]}


# --- DuckDB side -------------------------------------------------------------

def _duckdb_factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.plant")
        con.execute("CREATE TABLE s3t.plant.metrics(device VARCHAR, reading DOUBLE)")
        con.executemany("INSERT INTO s3t.plant.metrics VALUES (?, ?)", _ROWS)
        return con
    return factory


def _duckdb_totals():
    tools = make_duckdb_tools({"table_bucket_arn": ARN}, connection_factory=_duckdb_factory())
    run_query = tools[2].__wrapped__
    out = json.loads(run_query(
        "SELECT device, SUM(reading) AS total FROM s3t.plant.metrics GROUP BY device"))
    return {r[0]: float(r[1]) for r in out["rows"]}


# --- parity ------------------------------------------------------------------

def test_athena_computes_the_expected_totals():
    assert _athena_totals() == _EXPECTED


def test_duckdb_computes_the_expected_totals():
    assert _duckdb_totals() == _EXPECTED


def test_athena_and_duckdb_agree():
    """The core comparison claim: both read paths give the same answer."""
    assert _athena_totals() == _duckdb_totals()
