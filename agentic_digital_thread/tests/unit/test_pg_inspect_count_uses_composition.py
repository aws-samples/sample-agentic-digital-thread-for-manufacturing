# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""pg_inspect_tables builds its exact COUNT(*) with pgdriver.sql composition.

The row-count fallback used to interpolate the schema and table into an
f-string. The identifiers are already validated by validate_identifier(), but
the file's other identifier SQL (db.py:344/392) uses psql.SQL/Identifier
composition. This guard asserts the COUNT(*) statement is a composed object,
not a plain string, so the hardening cannot silently revert to an f-string.
"""
from __future__ import annotations

from agents.db import pg_inspect_tables
from agents.pgdriver import sql as psql


class _FakeCursor:
    """Records every execute; returns no columns and a fixed count."""

    def __init__(self):
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append(query)

    def fetchall(self):
        # No columns for the inspected table.
        return []

    def fetchone(self):
        return {"cnt": 0}


def test_count_query_is_a_sql_composition(monkeypatch):
    # No estimate for the table, so the exact COUNT(*) path runs.
    monkeypatch.setattr("agents.db.pg_row_estimates", lambda cur, schema: {})
    cur = _FakeCursor()

    pg_inspect_tables(cur, "public", ["orders"])

    composed = [q for q in cur.executed if isinstance(q, psql.Composed)]
    assert composed, "the COUNT(*) query must be a pgdriver sql.Composed, not an f-string"
    # No raw-string COUNT(*) statement (the old f-string path) was executed.
    assert not [q for q in cur.executed if isinstance(q, str) and "COUNT(*)" in q]
    # The composed statement carries the fixed COUNT(*) text and two Identifiers.
    parts = composed[0].seq
    assert any(isinstance(p, psql.SQL) and "COUNT(*)" in p.string for p in parts)
    assert sum(isinstance(p, psql.Identifier) for p in parts) == 2
