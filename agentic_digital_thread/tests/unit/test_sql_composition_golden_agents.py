# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SQL the agents send for a schema or table name, pinned statement by statement.

A schema or table name cannot be a bound parameter, so each of these reads
builds the name into the statement. The tests run the real functions against a
recording cursor and pin the exact text and parameters the database receives,
so changing how a statement is composed cannot change what is sent.

A ``pgdriver.sql`` object is rendered without a connection: ``SQL`` pieces as
written, ``Identifier`` pieces double-quoted (a quote doubled).

pgdriver binds every parameter on the server, and a utility ``SET`` cannot take
a bound parameter, so the session's statement timeout is set with
``set_config`` and a str value.
"""
from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import db, form_pairing, onboard_preflight, pgdriver, system_agent  # noqa: E402
from agents.pgdriver import sql as psql  # noqa: E402

_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, false)"


def render(query) -> str:
    """The statement text a connection would receive."""
    if isinstance(query, str):
        return query
    if isinstance(query, psql.Composed):
        return "".join(render(part) for part in query.seq)
    if isinstance(query, psql.SQL):
        return query.string
    if isinstance(query, psql.Identifier):
        return ".".join('"' + part.replace('"', '""') + '"' for part in query.strings)
    raise AssertionError(f"unrenderable query part: {query!r}")


class Cursor:
    """Records ``(text, params)``. ``rows`` answers ``fetchall``; ``one`` answers ``fetchone``."""

    description = None

    def __init__(self, rows=None, one=None):
        self.executed: list[tuple[str, object]] = []
        self._rows = rows if rows is not None else []
        self._one = one

    def execute(self, query, params=None):
        self.executed.append((render(query), params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one

    def fetchmany(self, n):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Conn:
    autocommit = False

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *args, **kwargs):
        return self._cursor

    def set_session(self, **kwargs):
        pass

    def close(self):
        pass


_SEARCH_PATH = ("SET search_path TO \"erp_data\"", None)


def test_query_pg_sets_the_search_path_then_runs_the_query(monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(db, "_get_conn_params_for_schema", lambda schema: {})
    monkeypatch.setattr(db.pgdriver, "connect", lambda **kwargs: Conn(cursor))
    db.query_pg("erp_data", "SELECT 1", (5,))
    assert cursor.executed == [
        (_TIMEOUT_SQL, (str(db._PG_STATEMENT_TIMEOUT_MS),)),
        _SEARCH_PATH,
        ("SELECT 1", (5,)),
    ]


def test_query_pg_quotes_a_schema_name_with_a_quote(monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(db, "_get_conn_params_for_schema", lambda schema: {})
    monkeypatch.setattr(db.pgdriver, "connect", lambda **kwargs: Conn(cursor))
    db.query_pg('we"ird', "SELECT 1")
    assert cursor.executed[1] == ('SET search_path TO "we""ird"', None)


def test_query_pg_bounded_sets_the_search_path_then_runs_the_query(monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(db, "_get_conn_params_for_schema", lambda schema: {})
    monkeypatch.setattr(db.pgdriver, "connect", lambda **kwargs: Conn(cursor))
    with contextlib.suppress(Exception):
        db.query_pg_bounded("erp_data", "SELECT 1", (5,))
    assert cursor.executed[:3] == [
        (_TIMEOUT_SQL, (str(db._PG_STATEMENT_TIMEOUT_MS),)),
        _SEARCH_PATH,
        ("SELECT 1", (5,)),
    ]


def test_pg_inspect_counts_a_table_without_statistics_by_name(monkeypatch):
    cursor = Cursor(rows=[], one={"cnt": 7})
    monkeypatch.setattr(db, "pg_row_estimates", lambda cur, schema: {})
    tables = db.pg_inspect_tables(cursor, "erp_data", ["orders"])
    assert tables == [{"name": "orders", "columns": [], "row_count": 7}]
    assert cursor.executed[-1] == ('SELECT COUNT(*) AS cnt FROM "erp_data"."orders"', None)


def test_pg_inspect_refuses_a_table_name_that_is_not_an_identifier(monkeypatch):
    cursor = Cursor(rows=[])
    monkeypatch.setattr(db, "pg_row_estimates", lambda cur, schema: {})
    with pytest.raises(Exception):
        db.pg_inspect_tables(cursor, "erp_data", ['orders"; DROP TABLE x; --'])
    assert not [text for text, _ in cursor.executed if text.startswith("SELECT COUNT")]


def test_form_pairing_sets_the_search_path_before_its_read(monkeypatch):
    cursor = Cursor(rows=[("a", "b")])
    monkeypatch.setattr(form_pairing, "connect", lambda params: Conn(cursor))
    rows = form_pairing._execute("erp_data", {}, "SELECT 1", ["x"], ["y"])
    assert rows == [("a", "b")]
    assert cursor.executed == [
        (_TIMEOUT_SQL, (str(form_pairing.STATEMENT_TIMEOUT_MS),)),
        _SEARCH_PATH,
        ("SELECT 1", (["x"], ["y"], form_pairing.MAX_PAIRS + 1)),
    ]


def test_preflight_reads_one_row_of_each_table_by_quoted_name(monkeypatch):
    cursor = Cursor(rows=[("quality_checks",), ("work_orders",)], one=(2,))
    monkeypatch.setattr(pgdriver, "connect", lambda conn_str, **kw: Conn(cursor))
    monkeypatch.setattr("agents.connection_security.validate_connection_target", lambda *a, **k: "")
    onboard_preflight._pf_postgres({"connection_string": "postgresql://x", "schema": "erp_data"},
                                   "erp", time.monotonic() + 300)
    assert [item for item in cursor.executed if item[0].startswith("SELECT 1 FROM")] == [
        ('SELECT 1 FROM "erp_data"."quality_checks" LIMIT 1', None),
        ('SELECT 1 FROM "erp_data"."work_orders" LIMIT 1', None),
    ]


def test_system_agent_mode_a_sets_the_search_path_before_the_query(monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(system_agent.pgdriver, "connect", lambda **kwargs: Conn(cursor))
    with contextlib.suppress(Exception):
        system_agent._execute_direct_query("erp_data", {}, "SELECT 1")
    texts = [text for text, _ in cursor.executed]
    assert texts[:2] == [_TIMEOUT_SQL, "SET standard_conforming_strings = on"]
    assert cursor.executed[0][1] == (str(system_agent._PG_STATEMENT_TIMEOUT_MS),)
    assert cursor.executed[2] == _SEARCH_PATH
    assert texts[3].startswith("SELECT 1")


@pytest.mark.parametrize("schema, expected", [("erp_data", [_SEARCH_PATH]), ("", [])])
def test_system_agent_mode_b_sets_the_search_path_only_for_a_schema(monkeypatch, schema, expected):
    cursor = Cursor()
    monkeypatch.setattr(system_agent, "_resolve_pg_conn_params", lambda *a: {})
    monkeypatch.setattr(system_agent, "_pg_readonly_conn", lambda params: Conn(cursor))
    with contextlib.suppress(Exception):
        system_agent._mode_b_postgres_query("SELECT 1", schema, None, None)
    assert [item for item in cursor.executed if item[0].startswith("SET search_path")] == expected
    assert cursor.executed[0] == (_TIMEOUT_SQL, (str(system_agent._PG_STATEMENT_TIMEOUT_MS),))
