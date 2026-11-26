# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ERP dashboard's SQL console stays read-only (erp_simulation/sql_console.py).

Offline tests cover the size cap, the single-statement scanner, the
leading-keyword check, the error-text mapping, the transaction sequence and
best-effort cleanup on a fake connection, and the dashboard method with
streamlit stubbed. The ``pg_*`` tests need ERP_SIM_TEST_PG_DSN (a throwaway
PostgreSQL whose user can create roles) and drive the dashboard end to end:
the query runs as the console's own login (console_login.py), and writes,
escapes and long queries are refused:

    python -m pytest erp_simulation/tests/test_sql_console.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import secrets
import sys
import types
import uuid
from pathlib import Path

import pytest

from erp_simulation import console_login, sql_console
from erp_simulation.sql_console import (
    CHECK_SQL_MESSAGE,
    GENERIC_MESSAGE,
    MAX_QUERY_BYTES,
    PERMISSION_MESSAGE,
    READ_ONLY_MESSAGE,
    TIMEOUT_MESSAGE,
    TOO_LONG_MESSAGE,
    exceeds_size_limit,
    is_single_statement,
    leading_keyword,
)
from erp_simulation.tests._pg_sql import stmt

_DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "streamlit_dashboard.py"
_ONE_STATEMENT = "❌ Run one statement at a time"
_NOT_SELECT = "❌ Only SELECT queries are allowed"
_WRITE_KEYWORD = "❌ Only read-only SELECT queries are allowed"
_UNAVAILABLE = f"❌ {console_login.UNAVAILABLE_MESSAGE}"


@pytest.fixture(autouse=True)
def _fresh_console_cache():
    console_login.forget_sync()
    yield
    console_login.forget_sync()


# ------------------------------------------------------------ size cap ---

def test_size_cap_is_64_kib_of_utf8():
    assert MAX_QUERY_BYTES == 65536
    assert TOO_LONG_MESSAGE == "The query is longer than 64 KiB."


@pytest.mark.parametrize("sql, over", [
    ("x" * MAX_QUERY_BYTES, False),
    ("x" * (MAX_QUERY_BYTES + 1), True),
    ("é" * (MAX_QUERY_BYTES // 2), False),        # 2 bytes each: exactly at the cap
    ("é" * (MAX_QUERY_BYTES // 2) + "x", True),   # fewer characters than the cap, more bytes
    ("€" * (MAX_QUERY_BYTES // 3) + "x", False),  # 3 bytes each: 65535 + 1
    ("€" * (MAX_QUERY_BYTES // 3) + "xx", True),
])
def test_size_cap_boundaries(sql, over):
    assert exceeds_size_limit(sql) is over


def test_oversized_text_is_never_a_single_statement():
    assert not is_single_statement("SELECT 1 -- " + "x" * MAX_QUERY_BYTES)
    assert is_single_statement("SELECT 1 -- " + "x" * (MAX_QUERY_BYTES - 12))


# ---------------------------------------------------- single statement ---

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT 1;",
    "SELECT 1 ;  ;\n\t",
    "SELECT 1; -- trailing comment",
    "SELECT 1; /* trailing */ ;",
    "SELECT ';' AS semi",
    "SELECT 'it''s; fine'",
    'SELECT 1 AS "a;b"',
    'SELECT 1 AS "a"";b"',
    "SELECT $$; still a string$$",
    "SELECT $tag$ $$ ; nested-looking $$ $tag$",
    "SELECT $a$ $b$ ; $b$ $a$",
    "SELECT E'\\'; still inside'",
    "SELECT B'0101', X'1F'",
    "SELECT U&'d\\0061t; a'",
    'SELECT 1 AS U&"x;y"',
    "SELECT 1 -- ; comment\n",
    "SELECT 1 /* ; */",
    "SELECT 1 /* outer /* inner ; */ still ; comment */",
    "SELECT 'a'\n';b'",                      # continued string, one literal
    "SELECT * FROM t WHERE note LIKE '%;%'",
    "SELECT a$b$c FROM t",                   # $ inside an identifier
    "SELECT $1",                              # a parameter, not a dollar quote
    "WITH x AS (SELECT 1) SELECT * FROM x;",
    "SELECT 'C:\\dir'",                       # same under both backslash readings
])
def test_single_statement_accepted(sql):
    assert is_single_statement(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2",
    "SELECT 1; GRANT SELECT ON orders TO someone; SELECT 1",
    "SELECT 1; COMMIT; GRANT SELECT ON orders TO someone",
    "SELECT 1; COMMIT; SET default_transaction_read_only = off; DELETE FROM orders",
    "SELECT 1; DO $$ BEGIN EXECUTE 'DR' || 'OP TABLE orders'; END $$",
    "SELECT 1;SELECT 2",
    "SELECT 1 /* c */ ; COMMIT",
    "SELECT 'x'; COMMIT",
    "SELECT $$x$$; COMMIT",
    'SELECT "x"; COMMIT',
    "SELECT 1 -- c\r; COMMIT",               # \r ends a -- comment
    "SELECT 1; 'x'",
    # Escape payloads that fool a scanner reading only '' escapes:
    "SELECT E'\\''; COMMIT; GRANT SELECT ON orders TO someone; SELECT '",
    "SELECT e'\\''; COMMIT; SELECT '",
    # A backslash before the quote is literal when standard_conforming_strings
    # is on, an escape when it is off. Each text is one statement under one
    # reading and two under the other, so both are rejected:
    "SELECT '\\'; COMMIT; SELECT '",
    "SELECT '\\''; COMMIT; SELECT '''",
    # A continued E-string keeps backslash escapes on its second segment:
    "SELECT E'a'\n'\\''; COMMIT; SELECT '''",
    # A $tag$ inside an identifier is not a dollar quote:
    "SELECT 1 AS a$b$; COMMIT; SELECT $b$",
    # Unterminated constructs fail closed:
    "SELECT 'unterminated; COMMIT",
    'SELECT "unterminated; COMMIT',
    "SELECT $$unterminated; COMMIT",
    "SELECT 1 /* unterminated ; COMMIT",
    "SELECT 1 /* /* nested */ ; COMMIT",
    "SELECT E'ends with a backslash\\",
])
def test_multi_statement_or_unterminated_rejected(sql):
    assert not is_single_statement(sql)


def test_dashboard_templates_are_accepted(dash):
    for name, sql in dash.SQL_QUERY_TEMPLATES.items():
        assert leading_keyword(sql) == "SELECT", name
        assert is_single_statement(sql), name


@pytest.mark.parametrize("sql, keyword", [
    ("SELECT 1", "SELECT"),
    ("  select * from t", "SELECT"),
    ("-- note\nSELECT 1", "SELECT"),
    ("/* a /* b */ */ WITH x AS (SELECT 1) SELECT 1", "WITH"),
    ("SELECT*FROM t", "SELECT"),
    ("SELECTX 1", "SELECTX"),
    ("SELECT_1", ""),
    ("SELECT1", ""),
    ("(SELECT 1)", ""),
    ("/* never closed SELECT", ""),
    ("", ""),
    ("GRANT SELECT ON t TO r", "GRANT"),
])
def test_leading_keyword(sql, keyword):
    assert leading_keyword(sql) == keyword


# ------------------------------------------------------- error mapping ---

class _PgError(Exception):
    def __init__(self, pgcode, text="relation \"secret_table\" does not exist"):
        super().__init__(text)
        self.pgcode = pgcode


@pytest.mark.parametrize("pgcode, message", [
    ("25006", READ_ONLY_MESSAGE),
    ("42501", PERMISSION_MESSAGE),
    ("57014", TIMEOUT_MESSAGE),
    ("42601", CHECK_SQL_MESSAGE),
    ("42P01", CHECK_SQL_MESSAGE),
    ("42703", CHECK_SQL_MESSAGE),
    ("22012", GENERIC_MESSAGE),
    (None, GENERIC_MESSAGE),
])
def test_console_error_message(pgcode, message):
    assert sql_console.console_error_message(_PgError(pgcode)) == message


def test_messages_match_the_timeout_constant():
    assert sql_console.STATEMENT_TIMEOUT_SECONDS == 15
    assert TIMEOUT_MESSAGE == "The query took longer than 15 seconds and was stopped."
    assert sql_console.console_error_message(RuntimeError("host db.example.com refused")) == "Query failed."


# ------------------------------------------------ fake-connection order ---

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.log.append(("execute", sql, params))
        if sql == self._conn.fail_on:
            raise self._conn.fail_with
        if sql in self._conn.cleanup_fails:
            raise RuntimeError(f"cleanup step {sql} failed")
        if sql == "USER":
            self.description = [("n",)]

    def fetchall(self):
        return [(1,)]


class _FakeConn:
    def __init__(self, autocommit=False, fail_on=None, fail_with=None, cleanup_fails=(),
                 rollback_fails_from=None):
        self.log, self._autocommit, self.fail_on = [], autocommit, fail_on
        self.fail_with = fail_with if fail_with is not None else _PgError("25006")
        self.cleanup_fails = set(cleanup_fails)
        # rollback number (1-based) from which every rollback raises
        self._rollback_fails_from, self._rollbacks = rollback_fails_from, 0
        self.closed = False

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        self.log.append(("autocommit", value))
        if value and "autocommit" in self.cleanup_fails:
            raise RuntimeError("connection already closed")
        self._autocommit = value

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self._rollbacks += 1
        self.log.append(("rollback",))
        if self._rollback_fails_from and self._rollbacks >= self._rollback_fails_from:
            raise RuntimeError("server closed the connection unexpectedly")

    def commit(self):
        self.log.append(("commit",))

    def close(self):
        self.log.append(("close",))
        self.closed = True


_EXPECTED_SEQUENCE = [
    ("rollback",),
    ("execute", "SET TRANSACTION READ ONLY", None),
    ("execute", "SELECT set_config('statement_timeout', %s, true)", ("15s",)),
    ("execute", "USER", None),
    ("rollback",),
    ("execute", "SELECT pg_advisory_unlock_all()", None),
    ("rollback",),
]


def test_run_readonly_query_sequence_on_success():
    conn = _FakeConn()
    assert sql_console.run_readonly_query(conn, "USER") == (["n"], [(1,)])
    assert conn.log == _EXPECTED_SEQUENCE
    assert ("commit",) not in conn.log


def test_run_readonly_query_rolls_back_on_failure():
    conn = _FakeConn(fail_on="USER")
    with pytest.raises(_PgError):
        sql_console.run_readonly_query(conn, "USER")
    assert conn.log == _EXPECTED_SEQUENCE


def test_run_readonly_query_restores_autocommit():
    conn = _FakeConn(autocommit=True)
    sql_console.run_readonly_query(conn, "USER")
    assert conn.log[1] == ("autocommit", False)
    assert conn.log[-1] == ("autocommit", True)
    assert conn.autocommit is True
    assert not conn.closed


_UNLOCK = "SELECT pg_advisory_unlock_all()"


@pytest.mark.parametrize("make_conn", [
    lambda: _FakeConn(fail_on="USER", rollback_fails_from=2),   # first cleanup rollback
    lambda: _FakeConn(fail_on="USER", rollback_fails_from=3),   # final rollback
    lambda: _FakeConn(fail_on="USER", cleanup_fails={_UNLOCK}),
    lambda: _FakeConn(fail_on="USER", autocommit=True, cleanup_fails={"autocommit"}),
], ids=["first-rollback", "final-rollback", "unlock", "autocommit"])
def test_cleanup_failure_keeps_the_query_error_and_closes(make_conn):
    conn = make_conn()
    with pytest.raises(_PgError) as caught:
        sql_console.run_readonly_query(conn, "USER")
    assert caught.value.pgcode == "25006"   # the query's error, not the cleanup's
    assert conn.closed and conn.log[-1] == ("close",)


def test_keyboard_interrupt_survives_cleanup():
    conn = _FakeConn(fail_on="USER", fail_with=KeyboardInterrupt(), cleanup_fails={_UNLOCK})
    with pytest.raises(KeyboardInterrupt):
        sql_console.run_readonly_query(conn, "USER")
    assert conn.closed


def test_keyboard_interrupt_with_clean_cleanup_leaves_the_connection_open():
    conn = _FakeConn(fail_on="USER", fail_with=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        sql_console.run_readonly_query(conn, "USER")
    assert conn.log == _EXPECTED_SEQUENCE and not conn.closed


def test_cleanup_failure_after_success_returns_rows_and_closes():
    conn = _FakeConn(cleanup_fails={_UNLOCK})
    assert sql_console.run_readonly_query(conn, "USER") == (["n"], [(1,)])
    assert conn.closed


def test_close_quietly_swallows_a_failing_close():
    class _Broken:
        def close(self):
            raise RuntimeError("already gone")

    sql_console.close_quietly(_Broken())


# ------------------------------------------------- dashboard, stubbed ---

class _State(dict):
    """``st.session_state``: a dict with attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _FakeSt:
    """Records st.error/st.success; every other streamlit call is a no-op."""

    def __init__(self):
        self.messages = []
        self.session_state = _State()

    def error(self, message):
        self.messages.append(("error", message))

    def success(self, message):
        self.messages.append(("success", message))

    def spinner(self, *_args, **_kwargs):
        return contextlib.nullcontext()

    def dialog(self, *_args, **_kwargs):
        return lambda fn: fn

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture(scope="module")
def dash():
    patch = pytest.MonkeyPatch()
    for name in ("pandas", "plotly", "plotly.graph_objects", "plotly.express"):
        patch.setitem(sys.modules, name, types.ModuleType(name))
    patch.setitem(sys.modules, "streamlit", _FakeSt())
    spec = importlib.util.spec_from_file_location("_sqlconsole_streamlit_dashboard", _DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patch.undo()
    return module


def _board(dash, db, shown):
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = db
    board._show_query_rows = lambda columns, rows, max_rows: shown.append((columns, list(rows)))
    return board


def _ask(dash, monkeypatch, db, sql, console=None):
    """Run the console method once; return (st messages, rows shown).
    ``console(sql)`` stands in for the console login when given."""
    st, shown = _FakeSt(), []
    monkeypatch.setattr(dash, "st", st)
    if console is not None:
        monkeypatch.setattr(console_login, "run_console_query", lambda _db, text: console(text))
    _board(dash, db, shown)._execute_readonly_query(sql, 100)
    return st.messages, shown


class _NoDb:
    def connection(self):
        raise AssertionError("the guard must refuse before touching the database")


@pytest.mark.parametrize("sql, message", [
    ("GRANT SELECT ON orders TO someone", _NOT_SELECT),
    ("(SELECT 1)", _NOT_SELECT),
    ("SELECT 1; DELETE FROM orders; SELECT 1", _WRITE_KEYWORD),
    ("SELECT 1; GRANT SELECT ON orders TO someone; SELECT 1", _ONE_STATEMENT),
    ("SELECT 1; COMMIT; GRANT SELECT ON orders TO someone", _ONE_STATEMENT),
    ("SELECT 1 -- " + "x" * MAX_QUERY_BYTES, f"❌ {TOO_LONG_MESSAGE}"),
])
def test_dashboard_guard_refuses_before_the_database(dash, monkeypatch, sql, message):
    monkeypatch.setenv(console_login.LOCAL_PASSWORD_ENV, "unused")
    messages, shown = _ask(dash, monkeypatch, _NoDb(), sql)
    assert messages == [("error", message)] and shown == []


def test_dashboard_refuses_oversized_text_before_any_scan(dash, monkeypatch):
    def scan(*_args):
        raise AssertionError("oversized text must not be scanned")

    for name in ("leading_keyword", "is_single_statement"):
        monkeypatch.setattr(sql_console, name, scan)
    messages, shown = _ask(dash, monkeypatch, _NoDb(), "SELECT " + "é" * (MAX_QUERY_BYTES // 2))
    assert messages == [("error", f"❌ {TOO_LONG_MESSAGE}")] and shown == []


def test_dashboard_without_a_console_login_runs_nothing(dash, monkeypatch):
    for name in (console_login.SECRET_ENV, console_login.LOCAL_PASSWORD_ENV):
        monkeypatch.delenv(name, raising=False)
    messages, shown = _ask(dash, monkeypatch, _NoDb(), "SELECT 1")
    assert messages == [("error", _UNAVAILABLE)] and shown == []


def test_dashboard_reports_a_failed_sync_as_unavailable(dash, monkeypatch):
    monkeypatch.setenv(console_login.LOCAL_PASSWORD_ENV, "pw-for-this-test")
    monkeypatch.delenv(console_login.SECRET_ENV, raising=False)

    def refuse(*_args):
        raise _PgError("42501", "permission denied to create role (pw-for-this-test)")

    monkeypatch.setattr(console_login, "sync_role", refuse)
    db = types.SimpleNamespace(connection=lambda: contextlib.nullcontext(_SyncAdmin()))
    messages, shown = _ask(dash, monkeypatch, db, "SELECT 1")
    assert messages == [("error", _UNAVAILABLE)] and shown == []
    assert "pw-for-this-test" not in repr(messages)


class _SyncAdmin:
    def get_dsn_parameters(self):
        return {"host": "db.example.com", "dbname": "erp"}

    def rollback(self):
        pass


@pytest.mark.parametrize("pgcode, message", [
    ("25006", READ_ONLY_MESSAGE),
    ("42501", PERMISSION_MESSAGE),
    ("42P01", CHECK_SQL_MESSAGE),
    (None, GENERIC_MESSAGE),
])
def test_dashboard_never_shows_the_raw_error(dash, monkeypatch, pgcode, message):
    def raising_console(sql):
        raise _PgError(pgcode)

    messages, _ = _ask(dash, monkeypatch, _NoDb(), "SELECT * FROM secret_table", console=raising_console)
    assert messages == [("error", f"❌ {message}")]
    assert "secret_table" not in repr(messages)


# ------------------------------------------- real PostgreSQL (ERP_SIM) ---

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture(scope="module")
def pg_cluster():
    """A throwaway database with an ``orders`` table, a sequence and a probe
    role, behind the REAL DatabaseManager.connection wrapper over a
    one-connection pool, plus a unique console role name (roles are
    cluster-wide)."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn

    from erp_simulation.database import DatabaseManager

    suffix = uuid.uuid4().hex[:8]
    db_name, role, console = f"csq_{suffix}", f"cs_probe_{suffix}", f"erp_console_{suffix}"
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))
        cur.execute(stmt("CREATE ROLE {role}", role=role))
    dsn = make_dsn(**{**parse_dsn(_DSN), "dbname": db_name})
    setup = pgdriver.connect(dsn)
    setup.autocommit = True
    with setup.cursor() as cur:
        cur.execute("CREATE TABLE orders (id int, status text)")
        cur.execute("INSERT INTO orders VALUES (1, 'open'), (2, 'shipped')")
        cur.execute("CREATE SEQUENCE console_seq")
    manager = DatabaseManager()
    manager._pool = pgdriver.SimpleConnectionPool(1, 1, dsn=dsn)
    try:
        yield types.SimpleNamespace(db=manager, check=setup, role=role, console_role=console,
                                    connect=lambda: pgdriver.connect(dsn))
    finally:
        manager._pool.closeall()
        setup.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db} WITH (FORCE)", db=db_name))
            cur.execute(stmt("DROP ROLE {role}", role=role))
            cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=console))
        admin.close()


@pytest.fixture
def pg(pg_cluster, monkeypatch):
    """The console logs in as ``pg_cluster.console_role`` (local password)."""
    monkeypatch.setattr(console_login, "CONSOLE_ROLE", pg_cluster.console_role)
    monkeypatch.setenv(console_login.LOCAL_PASSWORD_ENV, secrets.token_hex(16))
    monkeypatch.delenv(console_login.SECRET_ENV, raising=False)
    monkeypatch.delenv("USE_SECRETS_MANAGER", raising=False)
    return pg_cluster


def _scalar(pg, sql):
    with pg.check.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _unchanged(pg):
    """Nothing the probes try may have reached the database."""
    assert _scalar(pg, f"SELECT has_table_privilege('{pg.role}', 'orders', 'SELECT')") is False
    assert _scalar(pg, "SELECT to_regclass('orders_copy') IS NOT NULL") is False
    assert _scalar(pg, "SELECT obj_description('orders'::regclass)") is None
    assert _scalar(pg, "SELECT count(*) FROM orders") == 2
    assert _scalar(pg, "SELECT last_value FROM console_seq") == 1
    assert _scalar(pg, "SELECT is_called FROM console_seq") is False


def test_pg_plain_select_returns_rows(dash, monkeypatch, pg):
    messages, shown = _ask(dash, monkeypatch, pg.db, "SELECT id, status FROM orders ORDER BY id;")
    assert messages == [] and shown == [(["id", "status"], [(1, "open"), (2, "shipped")])]


def test_pg_with_select_returns_rows(dash, monkeypatch, pg):
    sql = "-- open orders\nWITH o AS (SELECT * FROM orders WHERE status = 'open') SELECT id FROM o"
    messages, shown = _ask(dash, monkeypatch, pg.db, sql)
    assert messages == [] and shown == [(["id"], [(1,)])]


@pytest.mark.parametrize("sql, message", [
    # The four probes of the original reproduction:
    ("SELECT 1; DELETE FROM orders; SELECT 1", _WRITE_KEYWORD),
    ("SELECT 1; GRANT SELECT ON orders TO {role}; SELECT 1", _ONE_STATEMENT),
    ("SELECT 1; SELECT * INTO orders_copy FROM orders; SELECT 1", _ONE_STATEMENT),
    ("SELECT 1; COMMENT ON TABLE orders IS 'console wrote this'; SELECT 1", _ONE_STATEMENT),
    # Ending the transaction from inside the text:
    ("SELECT 1; COMMIT; GRANT SELECT ON orders TO {role}", _ONE_STATEMENT),
    ("SELECT 1; COMMIT; SET default_transaction_read_only = off; GRANT SELECT ON orders TO {role}",
     _ONE_STATEMENT),
    ("SELECT 1; DO $$ BEGIN EXECUTE 'GR' || 'ANT SELECT ON orders TO {role}'; END $$", _ONE_STATEMENT),
    ("SELECT E'\\''; COMMIT; GRANT SELECT ON orders TO {role}; SELECT '", _ONE_STATEMENT),
    # One statement, refused by the READ ONLY transaction or the console role:
    ("SELECT * INTO orders_copy FROM orders", f"❌ {READ_ONLY_MESSAGE}"),
    ("SELECT nextval('console_seq')", f"❌ {PERMISSION_MESSAGE}"),
    ("SELECT set_config('transaction_read_only', 'off', true)", f"❌ {GENERIC_MESSAGE}"),
    ("SELECT pg_terminate_backend({admin_pid})", f"❌ {PERMISSION_MESSAGE}"),
    ("SELECT set_config('role', '{admin_user}', true)", f"❌ {PERMISSION_MESSAGE}"),
])
def test_pg_writes_are_refused(dash, monkeypatch, pg, sql, message):
    admin_pid, admin_user = _scalar(pg, "SELECT pg_backend_pid()"), _scalar(pg, "SELECT current_user")
    messages, shown = _ask(dash, monkeypatch, pg.db,
                           sql.format(role=pg.role, admin_pid=admin_pid, admin_user=admin_user))
    assert messages == [("error", message)] and shown == []
    _unchanged(pg)
    assert _scalar(pg, "SELECT pg_backend_pid()") == admin_pid  # not terminated


def test_pg_data_modifying_cte_is_refused_by_the_transaction(pg):
    """WITH is allowed as a leading keyword because the READ ONLY transaction
    refuses a data-modifying CTE; the keyword check is not what stops it."""
    from erp_simulation import pgdriver
    with pg.db.connection() as conn:
        with pytest.raises(pgdriver.Error) as caught:
            sql_console.run_readonly_query(conn, "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d")
    assert caught.value.pgcode == "25006"
    _unchanged(pg)


@pytest.mark.parametrize("sql, message", [
    ("SELEC 1", None),  # not a SELECT: refused before the database
    ("SELECT FROM WHERE", CHECK_SQL_MESSAGE),
    ("SELECT * FROM no_such_table", CHECK_SQL_MESSAGE),
    ("SELECT no_such_column FROM orders", CHECK_SQL_MESSAGE),
    ("SELECT 1 / 0", GENERIC_MESSAGE),
])
def test_pg_errors_show_generic_text(dash, monkeypatch, pg, sql, message):
    messages, _ = _ask(dash, monkeypatch, pg.db, sql)
    expected = _NOT_SELECT if message is None else f"❌ {message}"
    assert messages == [("error", expected)]
    for raw in ("no_such_table", "no_such_column", "division", "relation", "LINE"):
        assert raw not in messages[0][1]


def test_pg_long_query_hits_the_timeout(dash, monkeypatch, pg):
    messages, shown = _ask(dash, monkeypatch, pg.db, "SELECT pg_sleep(20)")
    assert messages == [("error", f"❌ {TIMEOUT_MESSAGE}")] and shown == []


def test_pg_query_cannot_lift_its_own_timeout(monkeypatch, pg):
    """set_config inside the statement does not disarm the running timeout
    (shortened to 1 s here so the test stays quick)."""
    from erp_simulation import pgdriver
    monkeypatch.setattr(sql_console, "STATEMENT_TIMEOUT_SECONDS", 1)
    with pg.db.connection() as conn:
        with pytest.raises(pgdriver.Error) as caught:
            sql_console.run_readonly_query(conn, "SELECT set_config('statement_timeout', '0', true), pg_sleep(3)")
    assert caught.value.pgcode == "57014"


def _pooled_state(pg):
    """Session state of the pool's only connection, read through the pool."""
    conn = pg.db.pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('application_name'), current_setting('transaction_read_only'), "
                        "current_setting('statement_timeout'), "
                        "(SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()), "
                        "pg_backend_pid()")
            state = cur.fetchone()
        conn.rollback()
        return state
    finally:
        pg.db.pool.putconn(conn)


def test_pg_console_leaves_the_admin_pool_clean(dash, monkeypatch, pg):
    """Console queries run on their own login and connection; the pooled
    admin connection (used only to sync the role) keeps its session state."""
    before = _pooled_state(pg)
    for sql in ("SELECT set_config('application_name', 'console-was-here', false)",
                "SELECT set_config('default_transaction_read_only', 'on', false)",
                "SELECT pg_advisory_lock(4242), pg_advisory_lock_shared(4343)"):
        messages, shown = _ask(dash, monkeypatch, pg.db, sql)
        assert messages == [] and len(shown) == 1
    after = _pooled_state(pg)
    assert after[4] == before[4]  # the pool's only connection, still the same one
    assert after[0] == before[0] != "console-was-here"
    assert after[1] == "off"      # not left read-only, not left in a transaction
    assert after[2] == before[2]  # statement_timeout back to the session default
    assert after[3] == 0          # session-level advisory locks released
    other = pg.connect()
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(4242)")
            assert cur.fetchone()[0] is True
    finally:
        other.close()
