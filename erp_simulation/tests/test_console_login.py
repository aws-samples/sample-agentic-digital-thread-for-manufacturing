# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SQL console runs as its own login, never as the admin (console_login.py).

Offline tests cover the credential sources, the fail-closed paths, the
re-sync-and-retry rules, connection closing and that the password never
reaches a log, an error text or a statement. The ``pg_*`` tests need
ERP_SIM_TEST_PG_DSN (a throwaway PostgreSQL whose user can create roles) and
prove, as the console role, that side-effecting functions are refused:

    python -m pytest erp_simulation/tests/test_console_login.py -q
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import types
import uuid

import pytest

from erp_simulation import console_login
from erp_simulation.console_login import ConsoleCredential, ConsoleUnavailable
from erp_simulation.database import DatabaseManager
from erp_simulation import pgdriver

_PASSWORD = "Pw" + "q7" * 12  # a stand-in console password for the offline tests  # nosec B105
_LOCAL_ENV = {console_login.LOCAL_PASSWORD_ENV: _PASSWORD}


@pytest.fixture(autouse=True)
def _fresh_cache():
    console_login.forget_sync()
    yield
    console_login.forget_sync()


# ------------------------------------------------------------ credential ---

def test_no_credential_is_unavailable():
    with pytest.raises(ConsoleUnavailable):
        console_login.load_credential({})


def test_local_password_gives_the_fixed_role():
    assert console_login.load_credential(_LOCAL_ENV) == ConsoleCredential("erp_console", _PASSWORD)


def test_local_password_is_ignored_on_a_secrets_manager_deployment():
    with pytest.raises(ConsoleUnavailable):
        console_login.load_credential({**_LOCAL_ENV, "USE_SECRETS_MANAGER": "true"})


def test_secret_wins_over_the_local_password():
    seen = []

    def fetch(secret_id, env):
        seen.append(secret_id)
        return {"username": "erp_console", "password": "from-secret"}

    env = {**_LOCAL_ENV, console_login.SECRET_ENV: "arn:aws:secretsmanager:us-east-1:111122223333:secret:x"}
    assert console_login.load_credential(env, fetch).password == "from-secret"  # nosec B105 - a fake test password
    assert seen == [env[console_login.SECRET_ENV]]


@pytest.mark.parametrize("secret", [
    {"username": "erp_admin", "password": "x"},   # never the admin, or any other role
    {"username": "erp_console", "password": ""},
    {"username": "erp_console"},
    {"password": "x"},
])
def test_secret_with_an_unexpected_shape_is_unavailable(secret):
    env = {console_login.SECRET_ENV: "s"}
    with pytest.raises(ConsoleUnavailable):
        console_login.load_credential(env, lambda *_: secret)


def test_unreadable_secret_is_unavailable_and_logs_no_detail(caplog):
    def fetch(*_):
        raise RuntimeError(f"denied while reading {_PASSWORD}")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConsoleUnavailable) as caught:
        console_login.load_credential({console_login.SECRET_ENV: "s"}, fetch)
    assert _PASSWORD not in caplog.text and _PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_credential_repr_hides_the_password():
    assert _PASSWORD not in repr(ConsoleCredential("erp_console", _PASSWORD))


# --------------------------------------------------------------- fakes ---

class _Cursor:
    def __init__(self, conn):
        self._conn, self.description = conn, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.log.append(sql)
        failure = self._conn.fail_with.get(sql) if isinstance(sql, str) else None
        if failure is not None:
            raise failure
        if sql == "QUERY":
            self.description = [("n",)]

    def fetchall(self):
        return self._conn.fetchall_rows

    def fetchone(self):
        return self._conn.fetchone_rows.pop(0) if self._conn.fetchone_rows else None


class _Conn:
    """A pgdriver-shaped connection that records everything done to it."""

    def __init__(self, fail_with=None, close_raises=False, fetchone_rows=None, fetchall_rows=None):
        self.log, self.fail_with, self.autocommit = [], dict(fail_with or {}), False
        self.closed, self._close_raises = False, close_raises
        self.fetchone_rows = list(fetchone_rows or [])
        self.fetchall_rows = fetchall_rows if fetchall_rows is not None else [(1,)]

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.log.append("rollback")

    def commit(self):
        self.log.append("commit")

    def close(self):
        self.closed = True
        if self._close_raises:
            raise RuntimeError("connection already broken")

    def get_dsn_parameters(self):
        return {"host": "db.example.com", "port": "5432", "dbname": "erp", "user": "erp_admin",
                "sslmode": "prefer", "options": "", "target_session_attrs": "any"}


class _Pool:
    def __init__(self, conn):
        self.conn, self.handed_out = conn, 0

    def getconn(self):
        self.handed_out += 1
        return self.conn

    def putconn(self, conn):
        pass


def _db(admin=None):
    """The REAL DatabaseManager.connection wrapper over a fake pool."""
    manager = DatabaseManager()
    manager._pool = _Pool(admin or _Conn())
    return manager


class _PgError(Exception):
    def __init__(self, pgcode, text="error"):
        super().__init__(text)
        self.pgcode = pgcode


class _Harness:
    """Counts syncs and connects; ``plan`` is what each connect yields."""

    def __init__(self, monkeypatch, plan, sync_error=None):
        self.syncs, self.connects, self.conns = 0, [], []
        self._plan, self._sync_error = list(plan), sync_error
        monkeypatch.setattr(console_login, "sync_role", self._sync)

    def _sync(self, admin, credential):
        self.syncs += 1
        if self._sync_error is not None:
            raise self._sync_error
        return ["public"]

    def connect(self, params, credential):
        self.connects.append((dict(params), credential))
        step = self._plan.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.conns.append(step)
        return step

    def run(self, db, sql="QUERY"):
        return console_login.run_console_query(db, sql, connect=self.connect, environ=_LOCAL_ENV)


# ------------------------------------------------------- run, offline ---

def test_no_credential_runs_nothing(monkeypatch):
    harness, db = _Harness(monkeypatch, []), _db()
    with pytest.raises(ConsoleUnavailable):
        console_login.run_console_query(db, "QUERY", connect=harness.connect, environ={})
    assert db._pool.handed_out == 0 and harness.syncs == 0 and harness.connects == []


def test_query_runs_on_its_own_connection_which_is_closed(monkeypatch):
    admin, console = _Conn(), _Conn()
    harness = _Harness(monkeypatch, [console])
    assert harness.run(_db(admin)) == (["n"], [(1,)])
    assert "QUERY" in console.log and "QUERY" not in admin.log
    assert console.closed
    params, credential = harness.connects[0]
    assert params == {"host": "db.example.com", "port": "5432", "dbname": "erp", "sslmode": "prefer"}
    assert credential.username == "erp_console"  # never the admin user from the DSN


def test_sync_is_cached_per_process(monkeypatch):
    harness, db = _Harness(monkeypatch, [_Conn(), _Conn()]), _db()
    harness.run(db)
    harness.run(db)
    assert harness.syncs == 1 and len(harness.connects) == 2


@pytest.mark.parametrize("error", [_PgError("42601"), KeyboardInterrupt()])
def test_failed_query_still_closes_the_connection(monkeypatch, error):
    console = _Conn(fail_with={"QUERY": error})
    harness = _Harness(monkeypatch, [console])
    with pytest.raises(type(error)):
        harness.run(_db())
    assert console.closed


def test_close_failure_does_not_mask_the_query_error(monkeypatch):
    console = _Conn(fail_with={"QUERY": _PgError("57014")}, close_raises=True)
    harness = _Harness(monkeypatch, [console])
    with pytest.raises(_PgError) as caught:
        harness.run(_db())
    assert caught.value.pgcode == "57014"


def test_auth_failure_resyncs_once_and_retries(monkeypatch):
    harness = _Harness(monkeypatch, [_Conn(), console_login._AuthFailed(), _Conn()])
    db = _db()
    harness.run(db)
    assert harness.run(db) == (["n"], [(1,)])
    assert harness.syncs == 2 and len(harness.connects) == 3


def test_auth_failure_after_the_resync_is_unavailable(monkeypatch):
    harness = _Harness(monkeypatch, [console_login._AuthFailed(), console_login._AuthFailed()])
    with pytest.raises(ConsoleUnavailable):
        harness.run(_db())
    assert harness.syncs == 2 and len(harness.connects) == 2


def test_missing_privilege_on_a_cached_sync_resyncs_and_retries(monkeypatch):
    """A data reset recreates tables, which drops their grants."""
    late = _Conn(fail_with={"QUERY": _PgError("42501")})
    harness = _Harness(monkeypatch, [_Conn(), late, _Conn()])
    db = _db()
    harness.run(db)
    assert harness.run(db) == (["n"], [(1,)])
    assert harness.syncs == 2 and late.closed


def test_missing_privilege_right_after_a_sync_is_the_answer(monkeypatch):
    console = _Conn(fail_with={"QUERY": _PgError("42501")})
    harness = _Harness(monkeypatch, [console])
    with pytest.raises(_PgError) as caught:
        harness.run(_db())
    assert caught.value.pgcode == "42501" and harness.syncs == 1


def test_other_connect_errors_propagate_without_a_resync(monkeypatch):
    harness = _Harness(monkeypatch, [RuntimeError("too many connections for role")])
    with pytest.raises(RuntimeError):
        harness.run(_db())
    assert harness.syncs == 1


def test_sync_failure_is_unavailable_logs_no_secret_and_is_not_cached(monkeypatch, caplog):
    """The sync's error text may quote the statement; it must reach neither
    a log (the pool wrapper's included) nor the error shown to the user."""
    leak = _PgError("42501", f"ERROR at ALTER ROLE erp_console PASSWORD '{_PASSWORD}'")
    admin = _Conn()
    harness = _Harness(monkeypatch, [], sync_error=leak)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConsoleUnavailable) as caught:
        harness.run(_db(admin))
    assert _PASSWORD not in caplog.text and _PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "rollback" in admin.log and harness.connects == []
    with pytest.raises(ConsoleUnavailable):
        harness.run(_db(admin))
    assert harness.syncs == 2  # a failed sync is retried on the next query


def test_sync_refusal_is_unavailable(monkeypatch):
    harness = _Harness(monkeypatch, [], sync_error=ConsoleUnavailable("console login is the admin login"))
    with pytest.raises(ConsoleUnavailable):
        harness.run(_db())
    assert harness.connects == []


def test_connect_translates_only_authentication_failures(monkeypatch):
    from erp_simulation import pgdriver
    errors = iter([pgdriver.OperationalError('password authentication failed for user "erp_console"'),
                   pgdriver.OperationalError("too many connections for role")])

    def fake_connect(**kwargs):
        raise next(errors)

    monkeypatch.setattr(pgdriver, "connect", fake_connect)
    credential = ConsoleCredential("erp_console", _PASSWORD)
    with pytest.raises(console_login._AuthFailed):
        console_login._connect({"host": "db.example.com"}, credential)
    with pytest.raises(pgdriver.OperationalError):
        console_login._connect({"host": "db.example.com"}, credential)


# ------------------------------------------------------ sync, offline ---

def _sync_offline(monkeypatch, fetchone_rows):
    monkeypatch.setattr(console_login, "_password_verifier", lambda conn, cred: "SCRAM-SHA-256$fake")
    return _Conn(fetchone_rows=fetchone_rows, fetchall_rows=[("public",), ("erp_data",)])


def test_sync_refuses_to_alter_the_admin_login(monkeypatch):
    admin = _sync_offline(monkeypatch, [("erp_console",)])
    with pytest.raises(ConsoleUnavailable):
        console_login.sync_role(admin, ConsoleCredential("erp_console", _PASSWORD))
    assert admin.log == ["SELECT current_user"]


def test_sync_refuses_a_role_that_is_not_minimal(monkeypatch):
    elevated = (True, True, False, False, False, False, 0)  # rolsuper
    admin = _sync_offline(monkeypatch, [("erp_admin",), (1,), elevated])
    with pytest.raises(ConsoleUnavailable):
        console_login.sync_role(admin, ConsoleCredential("erp_console", _PASSWORD))
    assert "commit" not in admin.log


def test_sync_statements_never_carry_the_password(monkeypatch):
    admin = _sync_offline(monkeypatch, [("erp_admin",), None, console_login._MINIMAL_ROLE])
    granted = console_login.sync_role(admin, ConsoleCredential("erp_console", _PASSWORD))
    assert granted == ["public", "erp_data"] and admin.log[-1] == "commit"
    assert all(_PASSWORD not in repr(stmt) for stmt in admin.log)
    assert any("SCRAM-SHA-256$fake" in repr(stmt) for stmt in admin.log)


# ------------------------------------------- real PostgreSQL (ERP_SIM) ---

from erp_simulation.tests._pg_sql import stmt  # noqa: E402

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture(scope="module")
def pg_cluster():
    """A throwaway database with ``orders`` and a sequence, and a unique
    console role name (roles are cluster-wide)."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn

    suffix = uuid.uuid4().hex[:8]
    db_name, role = f"csl_{suffix}", f"erp_console_{suffix}"
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))
    dsn = make_dsn(**{**parse_dsn(_DSN), "dbname": db_name})
    setup = pgdriver.connect(dsn)
    setup.autocommit = True
    with setup.cursor() as cur:
        cur.execute("CREATE TABLE orders (id int, status text)")
        cur.execute("INSERT INTO orders VALUES (1, 'open'), (2, 'shipped')")
        cur.execute("CREATE SEQUENCE console_seq")
        cur.execute("SELECT current_user, pg_backend_pid()")
        admin_user, setup_pid = cur.fetchone()
    manager = DatabaseManager()
    manager._pool = pgdriver.SimpleConnectionPool(1, 1, dsn=dsn)
    try:
        yield types.SimpleNamespace(db=manager, setup=setup, role=role, admin_user=admin_user,
                                    setup_pid=setup_pid, connect=lambda: pgdriver.connect(dsn))
    finally:
        manager._pool.closeall()
        setup.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db} WITH (FORCE)", db=db_name))
            cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=role))
        admin.close()


@pytest.fixture
def pg(pg_cluster, monkeypatch):
    monkeypatch.setattr(console_login, "CONSOLE_ROLE", pg_cluster.role)
    password = secrets.token_hex(16)
    monkeypatch.setenv(console_login.LOCAL_PASSWORD_ENV, password)
    monkeypatch.delenv(console_login.SECRET_ENV, raising=False)
    monkeypatch.delenv("USE_SECRETS_MANAGER", raising=False)
    pg_cluster.password = password
    return pg_cluster


def _run(pg, sql):
    return console_login.run_console_query(pg.db, sql)


def _scalar(pg, sql, params=None):
    with pg.setup.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _refused(pg, sql, *codes):
    from erp_simulation import pgdriver
    with pytest.raises(pgdriver.Error) as caught:
        _run(pg, sql)
    assert caught.value.pgcode in codes, (caught.value.pgcode, str(caught.value))
    return caught.value


def test_pg_query_runs_as_the_console_role(pg):
    assert _run(pg, "SELECT current_user, session_user") == (
        ["current_user", "session_user"], [(pg.role, pg.role)])


def test_pg_select_and_with_select_return_rows(pg):
    assert _run(pg, "SELECT id, status FROM orders ORDER BY id")[1] == [(1, "open"), (2, "shipped")]
    sql = "WITH o AS (SELECT * FROM orders WHERE status = 'open') SELECT id FROM o"
    assert _run(pg, sql) == (["id"], [(1,)])


@pytest.mark.parametrize("function", ["pg_terminate_backend", "pg_cancel_backend"])
def test_pg_cannot_signal_another_backend(pg, function):
    _refused(pg, f"SELECT {function}({pg.setup_pid})", "42501")
    assert _scalar(pg, "SELECT pg_backend_pid()") == pg.setup_pid  # still alive


@pytest.mark.parametrize("setting", ["role", "session_authorization"])
def test_pg_cannot_become_the_admin(pg, setting):
    _refused(pg, f"SELECT set_config('{setting}', '{pg.admin_user}', true)", "42501")


def test_pg_nextval_is_refused(pg):
    _refused(pg, "SELECT nextval('console_seq')", "42501", "25006")
    assert _scalar(pg, "SELECT is_called FROM console_seq") is False


def test_pg_advisory_lock_dies_with_the_console_connection(pg):
    _run(pg, "SELECT pg_advisory_lock(4242), pg_advisory_lock_shared(4343)")
    other = pg.connect()
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(4242), pg_try_advisory_lock(4343)")
            assert cur.fetchone() == (True, True)
    finally:
        other.close()


def test_pg_console_connection_is_closed_after_each_query(pg):
    _run(pg, "SELECT 1")
    deadline = time.monotonic() + 5
    while _scalar(pg, "SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (pg.role,)):
        assert time.monotonic() < deadline, "the console connection stayed open"
        time.sleep(0.05)


def test_pg_role_is_minimal_and_read_only(pg):
    _run(pg, "SELECT 1")
    with pg.setup.cursor() as cur:
        cur.execute("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
                    " rolbypassrls, rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s", (pg.role,))
        *flags, limit, config = cur.fetchone()
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (pg.role,))
        stored = cur.fetchone()[0]
    assert flags == [True, False, False, False, False, False] and limit == 5
    assert {"default_transaction_read_only=on", "statement_timeout=15s"} <= set(config)
    assert stored.startswith("SCRAM-SHA-256$") and pg.password not in stored


def test_pg_table_created_after_the_sync_is_readable_after_the_next(pg, monkeypatch):
    syncs = []
    real_sync = console_login.sync_role
    monkeypatch.setattr(console_login, "sync_role", lambda *a: syncs.append(1) or real_sync(*a))
    _run(pg, "SELECT 1")
    with pg.setup.cursor() as cur:
        cur.execute("CREATE TABLE late_orders AS SELECT * FROM orders")
    try:
        assert _run(pg, "SELECT count(*) FROM late_orders") == (["count"], [(2,)])
        assert len(syncs) == 2  # the missing grant triggered one re-sync
    finally:
        with pg.setup.cursor() as cur:
            cur.execute("DROP TABLE late_orders")


def test_pg_changed_password_is_repaired_by_one_resync(pg):
    _run(pg, "SELECT 1")
    with pg.setup.cursor() as cur:
        cur.execute(stmt("ALTER ROLE {role} PASSWORD {pw}", role=pg.role,  # utility: cannot bind
                         pw=pgdriver.sql.Literal(secrets.token_hex(16))))
    assert _run(pg, "SELECT 1") == (["?column?"], [(1,)])


def test_pg_drifted_createdb_is_reset_by_the_sync(pg):
    _run(pg, "SELECT 1")
    with pg.setup.cursor() as cur:
        cur.execute(stmt("ALTER ROLE {role} CREATEDB", role=pg.role))
    console_login.forget_sync()
    _run(pg, "SELECT 1")
    assert _scalar(pg, "SELECT rolcreatedb FROM pg_roles WHERE rolname = %s", (pg.role,)) is False


@pytest.mark.parametrize("drift, undo", [
    ("GRANT pg_signal_backend TO {role}", "REVOKE pg_signal_backend FROM {role}"),
    ("ALTER ROLE {role} SUPERUSER", "ALTER ROLE {role} NOSUPERUSER"),
])
def test_pg_role_that_cannot_be_made_minimal_fails_closed(pg, drift, undo):
    _run(pg, "SELECT 1")
    with pg.setup.cursor() as cur:
        cur.execute(stmt(drift, role=pg.role))
    console_login.forget_sync()
    try:
        with pytest.raises(ConsoleUnavailable):
            _run(pg, "SELECT 1")
    finally:
        with pg.setup.cursor() as cur:
            cur.execute(stmt(undo, role=pg.role))


def test_pg_sync_logs_no_password(pg, caplog):
    caplog.set_level(logging.DEBUG)
    _run(pg, "SELECT 1")
    assert pg.password not in caplog.text


# A managed cluster's admin (the Aurora administrator user) is NOT a superuser: it has
# CREATEROLE and CREATEDB and owns the ERP tables. The sync must work from
# such a login, and the console must not reach that admin's own sessions.

@pytest.fixture(scope="module")
def pg_managed():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import sql as psql
    from erp_simulation.pgdriver import make_dsn, parse_dsn

    suffix = uuid.uuid4().hex[:8]
    db_name, mgr, console = f"csm_{suffix}", f"mgr_{suffix}", f"erp_console_m{suffix}"
    mgr_password = secrets.token_hex(16)
    root = pgdriver.connect(_DSN)
    root.autocommit = True
    with root.cursor() as cur:
        cur.execute(stmt("CREATE ROLE {mgr} LOGIN NOSUPERUSER CREATEROLE CREATEDB PASSWORD {password}",
                         mgr=mgr, password=psql.Literal(mgr_password)))
        cur.execute(stmt("CREATE DATABASE {db} OWNER {mgr}", db=db_name, mgr=mgr))
    dsn = make_dsn(**{**parse_dsn(_DSN), "dbname": db_name, "user": mgr, "password": mgr_password})
    owner = pgdriver.connect(dsn)
    owner.autocommit = True
    with owner.cursor() as cur:
        cur.execute("CREATE SCHEMA erp_data")
        cur.execute("CREATE TABLE orders (id int, status text)")
        cur.execute("INSERT INTO orders VALUES (1, 'open'), (2, 'shipped')")
        cur.execute("CREATE VIEW erp_data.open_orders AS SELECT id FROM orders WHERE status = 'open'")
        cur.execute("SELECT pg_backend_pid()")
        owner_pid = cur.fetchone()[0]
    manager = DatabaseManager()
    manager._pool = pgdriver.SimpleConnectionPool(1, 1, dsn=dsn)
    try:
        yield types.SimpleNamespace(db=manager, owner=owner, owner_pid=owner_pid, mgr=mgr, role=console)
    finally:
        manager._pool.closeall()
        owner.close()
        with root.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db} WITH (FORCE)", db=db_name))
            cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=console))
            cur.execute(stmt("DROP ROLE {mgr}", mgr=mgr))
        root.close()


@pytest.fixture
def managed(pg_managed, monkeypatch):
    monkeypatch.setattr(console_login, "CONSOLE_ROLE", pg_managed.role)
    monkeypatch.setenv(console_login.LOCAL_PASSWORD_ENV, secrets.token_hex(16))
    monkeypatch.delenv(console_login.SECRET_ENV, raising=False)
    monkeypatch.delenv("USE_SECRETS_MANAGER", raising=False)
    return pg_managed


def test_pg_managed_admin_can_sync_and_the_console_reads(managed):
    assert console_login.run_console_query(managed.db, "SELECT current_user") == (
        ["current_user"], [(managed.role,)])
    assert console_login.run_console_query(managed.db, "SELECT id FROM erp_data.open_orders") == (
        ["id"], [(1,)])


@pytest.mark.parametrize("sql", [
    "SELECT pg_terminate_backend({pid})",
    "SELECT pg_cancel_backend({pid})",
    "SELECT set_config('role', '{mgr}', true)",
    "SELECT set_config('session_authorization', '{mgr}', true)",
])
def test_pg_console_cannot_reach_the_managed_admin(managed, sql):
    from erp_simulation import pgdriver
    with pytest.raises(pgdriver.Error) as caught:
        console_login.run_console_query(managed.db, sql.format(pid=managed.owner_pid, mgr=managed.mgr))
    assert caught.value.pgcode == "42501", str(caught.value)
    with managed.owner.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        assert cur.fetchone()[0] == managed.owner_pid  # the admin session survived


def test_pg_console_never_touches_the_admin_session(pg):
    """The pooled admin connection keeps its session state: the console's
    set_config and advisory locks happen on the console's own connection."""
    _run(pg, "SELECT set_config('application_name', 'console-was-here', false), pg_advisory_lock(77)")
    conn = pg.db.pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('application_name'), (SELECT count(*) FROM pg_locks"
                        " WHERE locktype = 'advisory' AND pid = pg_backend_pid())")
            application_name, advisory_locks = cur.fetchone()
        conn.rollback()
    finally:
        pg.db.pool.putconn(conn)
    assert application_name != "console-was-here" and advisory_locks == 0
