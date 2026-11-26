# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query-shape fingerprint observability contracts.

The helper is deliberately one-way: callers receive a digest and version,
never normalized query text. Connector execution remains byte-for-byte
independent from this post-execution observability path.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import agents.athena_scope as athena_scope
import agents.athena_tools as athena_tools
import agents.sql_safety as sql_safety
import agents.system_agent as system_agent
from agents.query_shape import (
    MAX_QUERY_SHAPE_BYTES,
    QUERY_SHAPE_VERSION,
    fingerprint_query_shape,
)


def _fingerprint(query: str) -> str:
    result = fingerprint_query_shape(query)
    assert set(result) == {"query_fingerprint", "query_shape_version"}
    assert result["query_shape_version"] == QUERY_SHAPE_VERSION
    assert re.fullmatch(r"[0-9a-f]{64}", result["query_fingerprint"])
    return result["query_fingerprint"]


def test_literal_comment_whitespace_and_case_only_differences_share_shape():
    first = """
        SELECT Part_No, Qty FROM Orders
        WHERE Qty > 5 AND status = 'OPEN' AND active = TRUE
    """
    second = """
        select part_no,qty /* ticket 8675309 must never affect the shape */
        from orders where qty>987.25 and status='CLOSED' and active=false
        -- another comment with a sensitive-looking value 424242
    """
    assert _fingerprint(first) == _fingerprint(second)


def test_comment_markers_inside_literals_are_masked_as_literals():
    assert _fingerprint("SELECT part_no FROM orders WHERE note = '-- not a comment'") == _fingerprint(
        "select part_no from orders where note='/* also not a comment */'"
    )


def test_relation_projection_and_operation_changes_have_distinct_shapes():
    baseline = _fingerprint("SELECT part_no FROM orders WHERE qty = 1")
    changed = {
        _fingerprint("SELECT qty FROM orders WHERE qty = 1"),
        _fingerprint("SELECT part_no FROM archived_orders WHERE qty = 1"),
        _fingerprint("DELETE FROM orders WHERE qty = 1"),
    }
    assert baseline not in changed
    assert len(changed) == 3


def test_helper_returns_no_normalized_text_or_literal_material():
    marker = "customer-secret-8675309"
    result = fingerprint_query_shape(
        f"SELECT part_no FROM orders WHERE customer_name = '{marker}'"  # nosec B608 - test fixture SQL
    )
    assert set(result) == {"query_fingerprint", "query_shape_version"}
    assert marker not in json.dumps(result)
    assert "select" not in json.dumps(result).lower()
    assert "orders" not in json.dumps(result).lower()


def test_empty_malformed_and_over_cap_queries_have_honest_absence():
    for query in (
        None,
        "",
        "   -- comment only\n /* and another */ ",
        "SELECT * FROM orders WHERE note = 'unterminated",
        "SELECT * FROM orders /* unterminated",
        "SELECT $$unterminated",
        "SELECT 'x' " + (" " * MAX_QUERY_SHAPE_BYTES),
    ):
        assert fingerprint_query_shape(query) == {}


def test_exact_original_sql_reaches_connector_without_shape_rewrite(monkeypatch):
    original = (
        "  SeLeCt part_no FROM orders /* connector receives this */ "
        "WHERE status = 'OPEN-8675309' LIMIT 7  "
    )
    seen: list[str] = []

    def _execute(schema, conn_params, sql, row_ids=None):
        seen.append(sql)
        return {"columns": [], "rows": [], "row_count": 0}, sql

    monkeypatch.setattr(system_agent, "_execute_direct_query", _execute)
    query_tool = system_agent._make_query_tool(
        "public", {"host": "example", "dbname": "db"}, system_id="system-sql"
    )
    result = json.loads(query_tool._tool_func(sql=original))
    assert seen == [original]
    assert result["_adt_executed_query"] == original


def test_fingerprint_helper_is_absent_from_execution_and_rewrite_call_graph():
    for function in (
        system_agent._execute_direct_query,
        system_agent._make_direct_query_tool,
        athena_tools.run_athena_query,
    ):
        source = inspect.getsource(function)
        assert "query_shape" not in source
        assert "fingerprint_query_shape" not in source

    for module in (sql_safety, athena_scope, athena_tools):
        module_file = module.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        assert "query_shape" not in source
        assert "fingerprint_query_shape" not in source
