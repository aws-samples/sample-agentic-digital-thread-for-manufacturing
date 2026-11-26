# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables read that returns a TIMESTAMP WITH TIME ZONE value succeeds.

DuckDB converts a TIMESTAMPTZ result to a Python datetime through ``pytz``; with
``pytz`` missing it raises ``Invalid Input Error: Required module 'pytz' failed
to import``, which the connector reports as an opaque "invalid query input".
Measured on a live deployment: every query that selected MAX/MIN of a
timestamptz column failed this way, while the same query without the time
column ran. ``pytz`` is therefore a declared runtime dependency.
"""
import json

import duckdb

from agents.duckdb_tools import make_duckdb_tools

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo"


def _factory():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.flow")
    con.execute("CREATE TABLE s3t.flow.t(id VARCHAR, done_at TIMESTAMPTZ)")
    con.execute("INSERT INTO s3t.flow.t VALUES ('a', TIMESTAMPTZ '2026-11-05 10:30:00+00')")
    return con


def test_pytz_is_importable_for_duckdb_timestamptz_conversion():
    import pytz  # noqa: F401  (declared in pyproject.toml for this conversion)


def test_a_timestamptz_result_is_returned_not_refused():
    tools = make_duckdb_tools({"table_bucket_arn": ARN}, connection_factory=_factory)
    out = json.loads(tools[2].__wrapped__(
        "SELECT MAX(done_at) AS newest, COUNT(*) AS n FROM s3t.flow.t"))
    assert "error" not in out, out
    assert out["rows"][0][1] == 1
    assert out["rows"][0][0].startswith("2026-11-05")
