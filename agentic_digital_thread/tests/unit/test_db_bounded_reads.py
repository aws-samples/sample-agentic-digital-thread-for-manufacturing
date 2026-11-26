# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounded PostgreSQL reads.

``db.query_pg`` used to open a connection, ``fetchall()`` with no deadline,
and leak the connection on any exception; ``db.pg`` hid an outage as an
empty result. Now:

* every ``query_pg`` sets a ``statement_timeout`` and ``search_path``, reads at
  most ``max_rows`` (+1 to detect overflow) and marks ``truncated`` /
  ``partial_reason: "row_limit"``; cursor and connection close on every path;
* ``pg()`` still returns an empty shape on failure but MARKS it
  (``unavailable: True`` + ``error_category``) so callers can tell an outage
  from no data (the honest-data rule);
* onboarding discovery/verification read row counts from ``pg_class.reltuples``
  in ONE catalog query and fall back to an exact ``COUNT(*)`` only for a
  relation without statistics.

Offline: ``pgdriver.connect`` is stubbed with a recording fake.
"""
from __future__ import annotations

import json

import pytest

import agents.db as db
from agents import pgdriver


class _FakeCursor:
    def __init__(self, conn, rows, description=(("a",), ("b",))):
        self.conn = conn
        self.executed: list[tuple] = []
        self._rows = list(rows)
        self.description = description
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql if isinstance(sql, str) else str(sql), params))
        if "boom" in str(sql):
            raise RuntimeError("query exploded")

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.session = None
        self.closed = False
        self.cursors: list[_FakeCursor] = []

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self, *a, **k):
        cur = _FakeCursor(self, self.rows)
        self.cursors.append(cur)
        return cur

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pg(monkeypatch):
    state = {"conn": None, "rows": []}

    def _connect(**_params):
        state["conn"] = _FakeConn(state["rows"])
        return state["conn"]

    monkeypatch.setattr(db.pgdriver, "connect", _connect)
    monkeypatch.setattr(db, "_get_conn_params_for_schema", lambda schema: {"dbname": schema})
    return state


def _timeout(ms: int) -> tuple:
    """The session timeout statement as executed: a utility ``SET`` cannot bind
    a server-side parameter, so it is ``set_config`` with the value as a str."""
    return ("SELECT set_config('statement_timeout', %s, false)", (str(ms),))


def test_query_pg_sets_timeout_search_path_and_closes(fake_pg):
    fake_pg["rows"] = [(1, "x"), (2, "y")]
    out = json.loads(db.query_pg("erp", "SELECT a, b FROM t"))
    assert out == {"columns": ["a", "b"], "rows": [[1, "x"], [2, "y"]], "row_count": 2}
    conn = fake_pg["conn"]
    cur = conn.cursors[0]
    assert cur.executed[0] == _timeout(db._PG_STATEMENT_TIMEOUT_MS)
    assert "search_path" in cur.executed[1][0]
    assert cur.executed[2] == ("SELECT a, b FROM t", None)
    assert conn.session == {"readonly": True, "autocommit": True}
    assert cur.closed and conn.closed


def test_query_pg_caps_rows_and_marks_truncation(fake_pg):
    fake_pg["rows"] = [(i, "r") for i in range(10)]
    out = json.loads(db.query_pg("erp", "SELECT a, b FROM big", max_rows=3))
    assert out["row_count"] == 3 and len(out["rows"]) == 3
    assert out["truncated"] is True and out["partial_reason"] == "row_limit"
    # exactly max_rows + 1 were requested — no full materialisation on our side
    assert fake_pg["conn"].cursors[0]._rows == [(i, "r") for i in range(4, 10)]


def test_query_pg_under_cap_is_not_marked(fake_pg):
    fake_pg["rows"] = [(1, "r")]
    out = json.loads(db.query_pg("erp", "SELECT a, b FROM small", max_rows=3))
    assert "truncated" not in out and out["row_count"] == 1


def test_query_pg_default_cap_is_generous_for_internal_reads():
    assert db._PG_INTERNAL_MAX_ROWS == 5_000


def test_query_pg_closes_connection_when_the_query_raises(fake_pg):
    with pytest.raises(RuntimeError, match="query exploded"):
        db.query_pg("erp", "SELECT boom")
    conn = fake_pg["conn"]
    assert conn.closed and conn.cursors[0].closed


def test_pg_marks_an_outage_instead_of_hiding_it(fake_pg, caplog):
    out = db.pg("erp", "SELECT boom")
    assert out["rows"] == [] and out["row_count"] == 0
    assert out["unavailable"] is True
    assert out["error_category"] == "RuntimeError"
    assert "SELECT boom" not in caplog.text  # category only, never the SQL


@pytest.mark.parametrize("sqlstate, category", [
    ("57014", "QueryCanceled"),
    ("42883", "UndefinedFunction"),
    ("42804", "DatatypeMismatch"),
    ("42P01", "UndefinedTable"),
    ("ZZ999", "DatabaseError"),
])
def test_pg_names_the_postgres_condition_callers_branch_on(monkeypatch, sqlstate, category):
    """The error pgdriver raises for a server error is named by its condition
    (callers branch on a statement timeout and on two columns that cannot be
    compared); an unlisted SQLSTATE is its DB-API class."""
    exc = pgdriver._server_error({"C": sqlstate, "M": "server message"}, None)

    def _raise(*_a, **_k):
        raise exc
    monkeypatch.setattr(db, "query_pg", _raise)
    assert db.pg("erp", "SELECT 1")["error_category"] == category


def test_pg_success_shape_is_unchanged_for_row_readers(fake_pg):
    fake_pg["rows"] = [(7, "z")]
    out = db.pg("erp", "SELECT a, b FROM t")
    assert out == {"columns": ["a", "b"], "rows": [[7, "z"]], "row_count": 1}
    assert "unavailable" not in out


def test_query_pg_bounded_shares_the_timeout(fake_pg):
    fake_pg["rows"] = [(1, "r")]
    db.query_pg_bounded("erp", "SELECT a, b FROM t")
    cur = fake_pg["conn"].cursors[0]
    assert cur.executed[0] == _timeout(db._PG_STATEMENT_TIMEOUT_MS)


def test_pg_passes_a_shorter_deadline_for_an_optional_read(fake_pg):
    """An optional onboarding read (the distinct-identifier measurement) sets
    its own short deadline; every other read keeps the default."""
    fake_pg["rows"] = [(1, "r")]
    db.pg("erp", "SELECT a, b FROM t", statement_timeout_ms=1500)
    assert fake_pg["conn"].cursors[0].executed[0] == _timeout(1500)
    db.pg("erp", "SELECT a, b FROM t")
    assert fake_pg["conn"].cursors[0].executed[0] == _timeout(db._PG_STATEMENT_TIMEOUT_MS)


def test_pg_calls_query_pg_unchanged_without_a_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "query_pg", lambda schema, sql, params=None, **kw: (
        calls.append(kw) or json.dumps({"rows": []})))
    db.pg("erp", "SELECT 1")
    db.pg("erp", "SELECT 1", statement_timeout_ms=900)
    assert calls == [{}, {"statement_timeout_ms": 900}]


@pytest.mark.parametrize("asked, applied", [
    (0, 1),            # 0 would switch PostgreSQL's deadline off entirely
    (-5, 1),
    (1, 1),
    (db._PG_STATEMENT_TIMEOUT_MS, db._PG_STATEMENT_TIMEOUT_MS),
    (db._PG_STATEMENT_TIMEOUT_MS + 1, db._PG_STATEMENT_TIMEOUT_MS),
    (10 * db._PG_STATEMENT_TIMEOUT_MS, db._PG_STATEMENT_TIMEOUT_MS),
])
def test_an_explicit_deadline_can_only_shorten_the_default(fake_pg, asked, applied):
    """``statement_timeout_ms`` is a shorter deadline for an optional read: it
    never disables the timeout and never extends it past the default."""
    fake_pg["rows"] = [(1, "r")]
    db.query_pg("erp", "SELECT a, b FROM t", statement_timeout_ms=asked)
    assert fake_pg["conn"].cursors[0].executed[0] == _timeout(applied)


# ── planner-statistics row estimates ────────────────────────────────────────

def test_pg_row_estimates_reads_pg_class_once_and_drops_unanalyzed():
    cur = _FakeCursor(None, [
        {"table_name": "orders", "estimate": 1200},
        {"table_name": "fresh", "estimate": -1},     # never analyzed (PG14+)
        {"table_name": "empty", "estimate": 0},
        {"table_name": None, "estimate": 5},
    ])
    out = db.pg_row_estimates(cur, "erp")
    assert out == {"orders": 1200, "empty": 0}
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert "pg_class" in sql and "reltuples" in sql and params == ("erp",)
    assert "COUNT(*)" not in sql.upper()


def test_pg_row_estimates_accepts_tuple_rows():
    cur = _FakeCursor(None, [("orders", 42), ("v", None)])
    assert db.pg_row_estimates(cur, "erp") == {"orders": 42}


def test_discovery_uses_estimates_and_counts_only_without_statistics(monkeypatch):
    """``onboard_agent._discover_postgres``'s loop: a table with statistics is
    reported from the estimate (flagged); one without gets ONE exact count."""
    import agents.onboard_agent as oa
    from agents.pgdriver import sql as psql

    def _render(sql):
        # Connection-free text of a str or a psql.Composed, rendered piece by
        # piece. Identifiers render double-quoted, matching PostgreSQL.
        if isinstance(sql, str):
            return sql
        parts = []
        for p in getattr(sql, "seq", []):
            if isinstance(p, psql.SQL):
                parts.append(p.string)
            elif isinstance(p, psql.Identifier):
                parts.append(".".join(f'"{s}"' for s in p.strings))
            else:
                parts.append(str(p))
        return "".join(parts)

    class _Cur:
        def __init__(self):
            self.executed: list[str] = []
            self._next = None

        def execute(self, sql, params=None):
            sql = _render(sql)
            self.executed.append(sql)
            if "information_schema.tables" in sql:
                self._next = [{"table_name": "orders"}, {"table_name": "fresh"}]
            elif "pg_class" in sql:
                self._next = [{"table_name": "orders", "estimate": 1200}]
            elif "information_schema.columns" in sql:
                self._next = [{"column_name": "id", "data_type": "integer"}]
            elif "COUNT(*)" in sql:
                self._next = [{"cnt": 3}]

        def fetchall(self):
            out, self._next = self._next or [], None
            return out

        def fetchone(self):
            out = (self._next or [None])[0]
            self._next = None
            return out

        def close(self):
            pass

    class _Conn:
        def __init__(self):
            self.cur = _Cur()

        def cursor(self, *a, **k):
            return self.cur

        def close(self):
            pass

    conn = _Conn()
    # pgdriver is imported lazily INSIDE _discover_postgres — patch its
    # connect (the test_onboard_agent_discover_resolve idiom).
    from agents import pgdriver
    monkeypatch.setattr(pgdriver, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(oa, "_fetch_credentials", lambda *_a, **_k: {}, raising=False)
    result = oa._discover_postgres(
        {"id": "erp", "schema": "erp_data"},
        {"connection_string": "postgresql://u:p@h/db", "schema": "erp_data"},
    )
    tables = {t["name"]: t for t in result["tables"]}
    assert tables["orders"]["row_count"] == 1200 and tables["orders"]["row_count_estimated"] is True
    assert tables["fresh"]["row_count"] == 3 and "row_count_estimated" not in tables["fresh"]
    counts = [s for s in conn.cur.executed if "COUNT(*)" in s]
    assert len(counts) == 1 and '"fresh"' in counts[0]
