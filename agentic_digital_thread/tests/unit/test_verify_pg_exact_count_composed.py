# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The verify route's exact-count fallback names the table through pgdriver.sql
composition (``db.table_sql``), not an f-string: the driver quotes the schema
and table, as the f-string's double quotes did for a validated identifier."""

from __future__ import annotations

import types

from agents import db, onboard
from agents.pgdriver import sql as psql


class _Cursor:
    def __init__(self):
        self.executed = []
        self._last = None

    def execute(self, query, params=None):
        self.executed.append((query, params))
        self._last = query

    def fetchall(self):
        return [("alpha",), ("beta",)]

    def fetchone(self):
        return (5,)

    def close(self):
        pass


def test_the_exact_count_is_a_composed_statement(monkeypatch):
    cur = _Cursor()
    conn = types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)
    monkeypatch.setattr(onboard.pgdriver, "connect", lambda dsn: conn)
    monkeypatch.setattr(db, "pg_row_estimates", lambda c, schema: {"alpha": 7})

    tables, rows, err = onboard._verify_pg_reachability("postgresql://example.com/db", "sch_a")

    assert (tables, rows, err) == (["alpha", "beta"], 12, None)
    counts = [q for q, _ in cur.executed if not isinstance(q, str)]
    assert counts == [db.table_sql("SELECT COUNT(*) FROM {}.{}", "sch_a", "beta")]
    assert counts[0] == psql.SQL("SELECT COUNT(*) FROM {}.{}").format(
        psql.Identifier("sch_a"), psql.Identifier("beta"))
