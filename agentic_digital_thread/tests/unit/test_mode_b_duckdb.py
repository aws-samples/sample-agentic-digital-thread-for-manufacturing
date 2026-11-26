# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DDB-10: an S3 Tables system under SYSTEM_QUERY_MODE=orchestrator
(Mode B) must NOT fall through to the PostgreSQL path (which would try a
pgdriver connection and fail confusingly). It is handled like the other
non-SQL-via-Mode-B types: Mode B points the caller at the system's own
structured tools. Fully offline — the non-SQL branch opens no connection.
"""
import agents.system_agent as sa
from agents.connection_types import S3TABLES_DUCKDB


def test_mode_b_describe_routes_duckdb_to_structured_tools():
    # The describe tool short-circuits DuckDB to the non-SQL note before the
    # PostgreSQL-defaulting _mode_b_describe_schema (DDB-10).
    out = sa._mode_b_non_sql_note(S3TABLES_DUCKDB)
    assert out["connection_type"] == S3TABLES_DUCKDB
    assert "structured" in out.get("note", "").lower()
    assert "tables" not in out


def test_mode_b_run_query_refuses_duckdb_as_sql_only():
    out = sa._mode_b_run_query(S3TABLES_DUCKDB, query="SELECT 1", schema="",
                               conn_params=None, connection_config={})
    assert out["connection_type"] == S3TABLES_DUCKDB
    assert "error" in out and "sql-only" in out["error"].lower()


def test_mode_b_connect_duckdb_defers_to_structured_tools():
    out = sa._mode_b_connect(S3TABLES_DUCKDB, schema="", conn_params=None,
                             connection_config={})
    assert out["connection_type"] == S3TABLES_DUCKDB
    assert out.get("connected") is None  # not a PG connect attempt
