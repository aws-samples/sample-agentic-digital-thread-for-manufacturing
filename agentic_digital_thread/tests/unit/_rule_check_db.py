# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``onboard.pg`` over in-memory SQLite, for the provision's identity-rule checks.

One SQLite database per schema, with PostgreSQL's case-sensitive LIKE.
``= ANY(%s)`` (PostgreSQL's array membership, which the Resolve probe uses) is
read as an IN list. ``information_schema.columns`` is answered from the
fixture's own tables: the
selected columns (``column_name``, ``table_name``, ``data_type``) for the
``<column> = %s`` conditions in the WHERE clause, in order. Every call is
recorded (``calls``: schema, sql, params, keyword arguments), and ``fail``
(a predicate on schema and sql) makes a read come back the way ``pg`` reports
an outage: no rows, ``unavailable`` and an ``error_category``.

A table is ``(columns, rows)``; a column is a name or ``(name, type)`` with
type ``varchar`` (the default), ``text`` or ``integer``. Neutral fixtures only.
"""
from __future__ import annotations

import re
import sqlite3

_DATA_TYPES = {"varchar": "character varying", "text": "text", "integer": "integer"}
_WHERE_PARAM = re.compile(r"(\w+)\s*=\s*%s")


def _column(spec) -> tuple[str, str]:
    return (spec, "varchar") if isinstance(spec, str) else (spec[0], spec[1])


class RuleCheckDb:
    def __init__(self, schemas: dict, fail=None, error_category: str = "QueryCanceled"):
        self.schemas = {s: {t: ([_column(c) for c in cols], rows) for t, (cols, rows) in tables.items()}
                        for s, tables in schemas.items()}
        self.fail = fail
        self.error_category = error_category
        self.calls: list = []
        self.conns = {}
        for schema, tables in self.schemas.items():
            conn = sqlite3.connect(":memory:")
            conn.execute("PRAGMA case_sensitive_like = ON")
            for table, (columns, rows) in tables.items():
                ddl = ", ".join(f"{name} {'INTEGER' if kind == 'integer' else 'TEXT'}"
                                for name, kind in columns)
                conn.execute(f"CREATE TABLE {table} ({ddl})")
                conn.executemany(
                    f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", rows)
            self.conns[schema] = conn

    def _information_schema(self, sql: str, params) -> dict:
        selected = [c.strip() for c in
                    re.search(r"SELECT\s+(.+?)\s+FROM\s+information_schema", sql, re.S).group(1).split(",")]
        where = dict(zip(_WHERE_PARAM.findall(sql), params or ()))
        rows = []
        for table, (columns, _rows) in self.schemas.get(where.get("table_schema"), {}).items():
            if "table_name" in where and where["table_name"] != table:
                continue
            for name, kind in columns:
                if "column_name" in where and where["column_name"] != name:
                    continue
                info = {"column_name": name, "table_name": table,
                        "data_type": _DATA_TYPES.get(kind, kind)}
                rows.append([info[c] for c in selected])
        if re.search(r"LIMIT\s+1\b", sql):
            rows = rows[:1]
        return {"columns": selected, "rows": rows}

    def __call__(self, schema, sql, params=None, **kw):
        self.calls.append((schema, sql, params, kw))
        if self.fail and self.fail(schema, sql):
            return {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                    "error_category": self.error_category}
        if "information_schema.columns" in sql:
            return self._information_schema(sql, params)
        if "= ANY(%s)" in sql:  # PostgreSQL's array membership, as an IN list
            (values,) = params
            sql = sql.replace("= ANY(%s)", "IN (" + ", ".join(["%s"] * len(values)) + ")")
            params = tuple(values)
        try:
            cur = self.conns[schema].execute(sql.replace("%s", "?"), tuple(params or ()))
        except (KeyError, sqlite3.Error) as exc:
            return {"columns": [], "rows": [], "unavailable": True,
                    "error_category": type(exc).__name__}
        return {"columns": [d[0] for d in cur.description],
                "rows": [list(r) for r in cur.fetchall()]}

    def reads_of(self, table: str) -> list:
        """The recorded calls that read ``table`` itself (not the catalog)."""
        return [c for c in self.calls if re.search(rf"\bFROM\s+{table}\b", c[1])
                and "information_schema" not in c[1]]


class FakeCatalog:
    def __init__(self, entries: dict):
        self.entries = entries

    def get(self, system_id):
        return self.entries.get(system_id)

    def get_onboarded_ids(self):
        return set(self.entries)


def measured_as_sent(monkeypatch) -> None:
    """For a test about what the provision WRITER does with a measured rule
    (``value_exact_match`` / ``value_core_match``), not about earning it:
    provision's re-measurement (``onboard._remeasured_rule``) answers that
    the rule as sent is what it measured. The re-measurement itself is pinned
    in ``test_identity_rule_provision_r4.py``."""
    import agents.onboard as onboard
    monkeypatch.setattr(onboard, "_remeasured_rule", lambda _ctx, rule, _table: (dict(rule), None))
