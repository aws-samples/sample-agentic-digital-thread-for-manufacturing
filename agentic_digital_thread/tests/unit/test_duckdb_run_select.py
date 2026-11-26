# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``duckdb_tools.run_select``: the public read helper the metric layer uses.

It runs ONE read-only SELECT through the same guards the chat tool applies
(authorization, select-only, row cap, scope) and returns the parsed result, so
a caller outside the tool wrapper never touches engine internals. Offline:
the engine's in-memory ``connection_factory`` seam stands in for the bucket.
"""
from __future__ import annotations

import duckdb
import pytest

from agents import duckdb_tools as dt

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.plant")
        con.execute("CREATE TABLE s3t.plant.metrics(device VARCHAR, reading DOUBLE)")
        con.execute("INSERT INTO s3t.plant.metrics VALUES "
                    "('a', 10.0), ('a', 20.0), ('b', 5.0)")
        con.execute("CREATE TABLE s3t.plant.other(x INTEGER)")
        return con
    return factory


def test_returns_rows_and_columns():
    out = dt.run_select({"table_bucket_arn": ARN},
                        "SELECT SUM(reading), COUNT(reading) FROM s3t.plant.metrics",
                        connection_factory=_factory())
    assert [float(v) for v in out["rows"][0]] == [35.0, 3.0]


def test_refuses_a_non_select():
    with pytest.raises(dt.QueryError):
        dt.run_select({"table_bucket_arn": ARN},
                      "DROP TABLE s3t.plant.metrics",
                      connection_factory=_factory())


def test_refuses_a_table_outside_the_configured_scope():
    cfg = {"table_bucket_arn": ARN, "tables": ["plant.metrics"]}
    with pytest.raises(dt.QueryError):
        dt.run_select(cfg, "SELECT COUNT(*) FROM s3t.plant.other",
                      connection_factory=_factory())


def test_refuses_a_table_function():
    with pytest.raises(dt.QueryError):
        dt.run_select({"table_bucket_arn": ARN},
                      "SELECT * FROM read_csv('/etc/passwd')",
                      connection_factory=_factory())


def test_a_bad_config_raises_the_config_error():
    with pytest.raises(dt.DuckDBConfigError):
        dt.run_select({}, "SELECT 1", connection_factory=_factory())


def test_a_failing_query_raises_a_safe_error():
    with pytest.raises(dt.QueryError) as exc:
        dt.run_select({"table_bucket_arn": ARN},
                      "SELECT nope FROM s3t.plant.metrics",
                      connection_factory=_factory())
    msg = str(exc.value)
    # The allowlisted sanitizer returns a safe client reason (here the fixed
    # column-not-found category), wrapped as "rejected the query", or the opaque
    # fallback. Either way the message must stay a safe client error with no
    # path, ARN or URL.
    assert ("S3 Tables rejected the query:" in msg
            or msg == "Query against S3 Tables failed")
    assert "arn:" not in msg and "://" not in msg and "/" not in msg


def test_one_engine_is_reused_for_the_same_config(monkeypatch):
    built = []
    real = dt._EngineHandle

    class _Counting(real):
        def __init__(self, cfg, connection_factory=None):
            built.append(cfg)
            super().__init__(cfg, connection_factory=connection_factory)

    monkeypatch.setattr(dt, "_EngineHandle", _Counting)
    monkeypatch.setattr(dt, "_SHARED_HANDLES", dt.OrderedDict())
    factory = _factory()
    cfg = {"table_bucket_arn": ARN}
    for _ in range(3):
        dt.run_select(cfg, "SELECT COUNT(*) FROM s3t.plant.metrics",
                      connection_factory=factory)
    dt.run_select({**cfg, "role_arn": "arn:aws:iam::444455556666:role/r"},
                  "SELECT COUNT(*) FROM s3t.plant.metrics",
                  connection_factory=factory)
    assert len(built) == 2  # one per distinct config, not one per call
