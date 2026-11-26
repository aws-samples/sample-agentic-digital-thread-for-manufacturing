# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Namespace and table scope are enforced independently.

``namespaces`` alone allows only those namespaces; ``tables`` alone allows only
those tables; both together allow the intersection; neither is the documented
whole-bucket grant. The same rule holds for run_select, the chat tools,
describe and discovery.
"""
from __future__ import annotations

import json

import duckdb
import pytest

from agents import duckdb_tools as dt

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        for ns in ("allowed", "other"):
            con.execute(f"CREATE SCHEMA s3t.{ns}")
        con.execute("CREATE TABLE s3t.allowed.a(x INTEGER)")
        con.execute("CREATE TABLE s3t.allowed.b(x INTEGER)")
        con.execute("CREATE TABLE s3t.other.secret(x INTEGER)")
        con.execute("INSERT INTO s3t.allowed.a VALUES (1)")
        con.execute("INSERT INTO s3t.allowed.b VALUES (2)")
        con.execute("INSERT INTO s3t.other.secret VALUES (42)")
        return con
    return factory


def _cfg(**extra):
    return {"table_bucket_arn": ARN, **extra}


def _select(cfg, sql):
    return dt.run_select(cfg, sql, connection_factory=_factory())


def test_namespace_only_refuses_another_namespace():
    with pytest.raises(dt.QueryError, match="outside this system's configured scope"):
        _select(_cfg(namespaces=["allowed"]), "SELECT x FROM s3t.other.secret")


def test_namespace_only_allows_every_table_in_the_namespace():
    out = _select(_cfg(namespaces=["allowed"]),
                  "SELECT (SELECT SUM(x) FROM s3t.allowed.a) + (SELECT SUM(x) FROM s3t.allowed.b)")
    assert int(out["rows"][0][0]) == 3


def test_namespace_match_is_case_insensitive():
    out = _select(_cfg(namespaces=["ALLOWED"]), "SELECT COUNT(*) FROM s3t.allowed.a")
    assert int(out["rows"][0][0]) == 1


def test_tables_only_still_refuses_siblings():
    with pytest.raises(dt.QueryError):
        _select(_cfg(tables=["allowed.a"]), "SELECT x FROM s3t.allowed.b")


def test_both_set_requires_the_intersection():
    cfg = _cfg(namespaces=["allowed"], tables=["allowed.a", "other.secret"])
    assert int(_select(cfg, "SELECT COUNT(*) FROM s3t.allowed.a")["rows"][0][0]) == 1
    # other.secret is in the tables list but its namespace is not allowed
    with pytest.raises(dt.QueryError):
        _select(cfg, "SELECT x FROM s3t.other.secret")
    with pytest.raises(dt.QueryError):
        _select(cfg, "SELECT x FROM s3t.allowed.b")


def test_neither_set_is_the_whole_bucket():
    out = _select(_cfg(), "SELECT x FROM s3t.other.secret")
    assert int(out["rows"][0][0]) == 42


def test_scope_holds_through_a_cte_and_a_subquery():
    cfg = _cfg(namespaces=["allowed"])
    with pytest.raises(dt.QueryError):
        _select(cfg, "WITH t AS (SELECT x FROM s3t.other.secret) SELECT * FROM t")
    with pytest.raises(dt.QueryError):
        _select(cfg, "SELECT * FROM (SELECT x FROM s3t.other.secret) q")


def test_list_tables_tool_honors_namespace_only_scope():
    tools = dt.make_duckdb_tools(_cfg(namespaces=["allowed"]),
                                 connection_factory=_factory())
    listed = json.loads(tools[0].__wrapped__())["tables"]
    assert sorted(listed) == ["allowed.a", "allowed.b"]


def test_describe_tool_refuses_another_namespace():
    tools = dt.make_duckdb_tools(_cfg(namespaces=["allowed"]),
                                 connection_factory=_factory())
    out = json.loads(tools[1].__wrapped__("other.secret"))
    assert "outside this system's configured scope" in out["error"]


def test_discovery_lists_only_the_configured_namespaces():
    out = dt.discover_schema(_cfg(namespaces=["allowed"]),
                             connection_factory=_factory())
    assert sorted(t["name"] for t in out["tables"]) == ["allowed.a", "allowed.b"]


def test_discovery_applies_the_intersection():
    out = dt.discover_schema(
        _cfg(namespaces=["allowed"], tables=["allowed.a", "other.secret"]),
        connection_factory=_factory())
    assert [t["name"] for t in out["tables"]] == ["allowed.a"]


def test_a_malformed_tables_entry_fails_closed_not_open():
    # a config that bypassed validation: every entry malformed must not
    # collapse to the whole-bucket grant
    scope = dt._scope_pairs({"tables": ["nodot"]})
    assert not dt._scope_allows(scope, "allowed", "a")
    assert not dt._scope_allows(scope, "other", "secret")


def test_malformed_entries_are_dropped_but_valid_ones_still_scope():
    scope = dt._scope_pairs({"tables": ["nodot", "allowed.a"]})
    assert dt._scope_allows(scope, "allowed", "a")
    assert not dt._scope_allows(scope, "allowed", "b")


def test_a_legacy_pairs_tuple_scope_still_works():
    dt.authorize_duckdb_sql("SELECT x FROM s3t.allowed.a", (("allowed", "a"),))
    with pytest.raises(dt.DuckDBAuthorizationError):
        dt.authorize_duckdb_sql("SELECT x FROM s3t.allowed.b", (("allowed", "a"),))
