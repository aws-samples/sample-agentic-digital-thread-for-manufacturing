# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The PostgreSQL client contract the sample's database code relies on,
checked against a real server.

``agents.pgdriver`` (over pg8000) is the client under test. The same checks
run against any module with its names (``connect``, ``sql``, the DB-API
errors, ``RealDictCursor``, ``parse_dsn``, ``make_dsn``, ``encrypt_password``,
``SimpleConnectionPool``, ``TRANSACTION_STATUS_*``): ``PG_CONTRACT_DRIVER``
names it, so the contract can be compared with the driver the sample used
before.

Covers bound parameters and their typing, ``%%``, values read back, errors and
SQLSTATEs, transactions and ``with`` blocks, read-only sessions,
identifier and literal composition (with ``standard_conforming_strings`` on
and off), connection strings, TLS modes, SCRAM verifiers, the pool and a
connection shared by threads.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server it may
``CREATE``/``DROP DATABASE`` on. Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_pgdriver_contract_pg.py -q
"""
from __future__ import annotations

import datetime as dt
import decimal
import importlib
import os
import threading
import uuid

import pytest

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture(scope="module")
def drv():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    return importlib.import_module(os.environ.get("PG_CONTRACT_DRIVER", "agents.pgdriver"))


@pytest.fixture(scope="module")
def db(drv):
    """``(params, connect)`` for a fresh database dropped afterwards."""
    name = "contract_" + uuid.uuid4().hex[:8]
    admin = drv.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(drv.sql.SQL("CREATE DATABASE {}").format(drv.sql.Identifier(name)))
    params = {**drv.parse_dsn(_DSN), "dbname": name}

    def connect(**extra):
        return drv.connect(drv.make_dsn(**{**params, **extra}))

    try:
        yield params, connect
    finally:
        with admin.cursor() as cur:
            cur.execute(drv.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                drv.sql.Identifier(name)))
        admin.close()


@pytest.fixture
def conn(db):
    c = db[1]()
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


def one(conn, query, args=None):
    with conn.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchone()


# ------------------------------------------------------------- parameters ---

def test_positional_and_named_parameters(conn):
    assert one(conn, "SELECT %s, %s", ("a", "b")) == ("a", "b")
    assert one(conn, "SELECT %(x)s, %(y)s, %(x)s", {"x": "1", "y": "2"}) == ("1", "2", "1")


def test_percent_escapes_only_with_parameters(conn):
    assert one(conn, "SELECT 'a%%b', %s", ("c",)) == ("a%b", "c")
    assert one(conn, "SELECT 'a%%b', 'x%'") == ("a%%b", "x%")
    assert one(conn, "SELECT 'abc' LIKE 'a%%'", ()) == (True,)


def test_values_round_trip_unchanged(conn):
    for value in ["it's", "back\\slash", "tab\tnew\nline", "ünïcødé ✓", "", "'; DROP TABLE x; --",
                  "%s", "%(x)s", "$1", "E'\\''"]:
        assert one(conn, "SELECT %s", (value,)) == (value,), value


def test_null_and_none(conn):
    assert one(conn, "SELECT %s IS NULL", (None,)) == (True,)
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE owners (owner text)")
        cur.execute("INSERT INTO owners VALUES ('a'), ('b'), (NULL)")
        q = "SELECT count(*) FROM owners WHERE (%(o)s::text IS NULL OR owner = %(o)s)"
        cur.execute(q, {"o": None})
        assert cur.fetchone() == (3,)
        cur.execute(q, {"o": "a"})
        assert cur.fetchone() == (1,)


def test_list_parameters_for_any(conn):
    assert one(conn, "SELECT 2 = ANY(%s)", ([1, 2, 3],)) == (True,)
    assert one(conn, "SELECT 'c,d' = ANY(%s)", (["a", 'b"q', "c,d"],)) == (True,)
    assert one(conn, "SELECT 'x' = ANY(%s::text[])", ([],)) == (False,)
    assert one(conn, "SELECT %s::text[]", (["a b", 'q"', "c,d", None, "\\"],)) == (
        ["a b", 'q"', "c,d", None, "\\"],)


def test_typed_parameters(conn):
    assert one(conn, "SELECT %s + 1", (41,)) == (42,)
    assert one(conn, "SELECT round(%s, 2)", (1.2345,)) == (decimal.Decimal("1.23"),)
    assert one(conn, "SELECT %s * 2", (decimal.Decimal("1.5"),)) == (decimal.Decimal("3.0"),)
    assert one(conn, "SELECT NOT %s", (True,)) == (False,)
    stamp = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
    got = one(conn, "SELECT %s - interval '1 hour'", (stamp,))[0]
    assert got == stamp - dt.timedelta(hours=1)
    assert one(conn, "SELECT %s + 1", (dt.date(2026, 11, 21),)) == (dt.date(2026, 11, 22),)
    assert one(conn, "SELECT %s::int + 1", ("5",)) == (6,)
    assert one(conn, "SELECT make_interval(days => %s)", (3,)) == (dt.timedelta(days=3),)
    assert one(conn, "SELECT jsonb_build_object('k', %s)", (7,)) == ({"k": 7},)


def test_string_parameters_in_untyped_positions(conn, drv):
    """A string is typed by where it is used. Where nothing gives it a type
    (``IS NULL``, a ``VARIADIC "any"`` function) the statement casts it."""
    assert one(conn, "SELECT jsonb_build_object('k', %s::text)", ("v",)) == ({"k": "v"},)
    assert one(conn, "SELECT concat(%s::text, 'x'), coalesce(%s, 'd')", ("a", None)) == ("ax", "d")
    assert one(conn, "SELECT format('<%%s>', %s::text)", ("y",)) == ("<y>",)
    assert one(conn, "SELECT %s::text IS NOT NULL, %s", ("a", None)) == (True, None)
    if drv.__name__ == "agents.pgdriver":
        with conn.cursor() as cur, pytest.raises(drv.ProgrammingError) as caught:
            cur.execute("SELECT %s IS NULL", ("a",))
        assert caught.value.pgcode == "42P18"


def test_string_parameter_against_integer_column(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE nums (n int)")
        cur.execute("INSERT INTO nums VALUES (%s), (%s)", (5, 6))
        cur.execute("SELECT count(*) FROM nums WHERE n = %s", ("5",))
        assert cur.fetchone() == (1,)
        cur.execute("SELECT count(*) FROM nums WHERE n > %s LIMIT %s", (4, 10))
        assert cur.fetchone() == (2,)


# -------------------------------------------------------------- row values ---

def test_row_value_types(conn):
    row = one(conn, """
        SELECT 1::int, 2::bigint, 1.5::numeric, 2.5::float8, 'x'::text, true,
               DATE '2026-11-21', TIMESTAMP '2026-11-21 01:02:03',
               TIMESTAMPTZ '2026-11-21 01:02:03+00', interval '1 day 02:00:00',
               interval '1 mon 2 days', '00000000-0000-0000-0000-000000000001'::uuid,
               '10.0.0.1'::inet, '{"a": [1]}'::jsonb, '{"b": 2}'::json,
               ARRAY['p', 'q'], ARRAY[1, 2], NULL::text, '\\x0102'::bytea""")
    assert row[:7] == (1, 2, decimal.Decimal("1.5"), 2.5, "x", True, dt.date(2026, 11, 21))
    assert row[7] == dt.datetime(2026, 11, 21, 1, 2, 3)
    assert row[8] == dt.datetime(2026, 11, 21, 1, 2, 3, tzinfo=dt.timezone.utc)
    assert row[9] == dt.timedelta(days=1, hours=2)
    assert row[10] == dt.timedelta(days=32)
    assert row[11] == "00000000-0000-0000-0000-000000000001"
    assert row[12] == "10.0.0.1"
    assert row[13:17] == ({"a": [1]}, {"b": 2}, ["p", "q"], [1, 2])
    assert row[17] is None
    assert bytes(row[18]) == b"\x01\x02"
    assert isinstance(row, tuple)


def test_description_rowcount_and_fetch(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS a, 'z' AS b UNION ALL SELECT 2, 'y'")
        assert [d[0] for d in cur.description] == ["a", "b"]
        assert [d.name for d in cur.description] == ["a", "b"]
        assert cur.rowcount == 2
        assert cur.fetchmany(1) == [(1, "z")]
        assert cur.fetchall() == [(2, "y")]
        assert cur.fetchone() is None
        cur.execute("CREATE TEMP TABLE t (n int)")
        assert cur.description is None
        cur.execute("INSERT INTO t SELECT generate_series(1, 3)")
        assert cur.rowcount == 3
        cur.execute("UPDATE t SET n = n + 1 WHERE n > %s", (1,))
        assert cur.rowcount == 2
        cur.execute("SELECT n FROM t ORDER BY n")
        assert list(cur) == [(1,), (3,), (4,)]


def test_real_dict_cursor(conn, drv):
    with conn.cursor(cursor_factory=drv.RealDictCursor) as cur:
        cur.execute("SELECT 1 AS a, 'z' AS b")
        assert cur.fetchall() == [{"a": 1, "b": "z"}]


def test_multi_statement_without_parameters_returns_the_last_result(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1; SELECT 2")
        assert cur.fetchall() == [(2,)]


# ------------------------------------------------------------------ errors ---

def test_server_errors_by_sqlstate(conn, drv):
    with conn.cursor() as cur:
        with pytest.raises(drv.ProgrammingError) as caught:
            cur.execute("SELECT * FROM no_such_table")
        assert caught.value.pgcode == "42P01"
        text = str(caught.value)
        assert text.splitlines()[0] == 'relation "no_such_table" does not exist'
        assert "LINE 1: SELECT * FROM no_such_table" in text
        assert caught.value.pgerror.startswith("ERROR:  relation")
        with pytest.raises(drv.DataError) as caught:
            cur.execute("SELECT 1 / 0")
        assert caught.value.pgcode == "22012"
        cur.execute("CREATE TEMP TABLE u (k int PRIMARY KEY)")
        cur.execute("INSERT INTO u VALUES (1)")
        with pytest.raises(drv.IntegrityError) as caught:
            cur.execute("INSERT INTO u VALUES (%s)", (1,))
        assert caught.value.pgcode == "23505"
        cur.execute("SET statement_timeout = 50")
        with pytest.raises(drv.OperationalError) as caught:
            cur.execute("SELECT pg_sleep(1)")
        assert caught.value.pgcode == "57014"
        assert isinstance(caught.value, drv.DatabaseError) and isinstance(caught.value, drv.Error)
        assert type(caught.value).__name__ == "QueryCanceled"


@pytest.mark.parametrize("query,name", [
    ("SELECT * FROM no_such_table", "UndefinedTable"),
    ("SELECT no_such_column FROM pg_class", "UndefinedColumn"),
    ("SELECT 1 / 0", "DivisionByZero"),
    ("SELECT 'x'::int", "InvalidTextRepresentation"),
    ("SELEC 1", "SyntaxError"),
    ("SELECT now() = 1", "UndefinedFunction"),
])
def test_error_classes_are_named_by_condition(conn, query, name):
    """Callers report and branch on ``type(exc).__name__``."""
    with conn.cursor() as cur, pytest.raises(Exception) as caught:
        cur.execute(query)
    assert type(caught.value).__name__ == name


def test_connect_failures_are_operational(db, drv):
    params, connect = db
    with pytest.raises(drv.OperationalError) as caught:
        connect(password="wrong-" + uuid.uuid4().hex)
    assert "authentication failed" in str(caught.value)
    with pytest.raises(drv.OperationalError):
        drv.connect(drv.make_dsn(**{**params, "host": "127.0.0.1", "port": "1"}),
                    connect_timeout=2)
    with pytest.raises(drv.OperationalError):
        connect(dbname="no_such_db_" + uuid.uuid4().hex[:6])


def test_closed_connection_and_cursor(db, drv):
    c = db[1]()
    cur = c.cursor()
    c.close()
    assert c.closed
    with pytest.raises(drv.InterfaceError):
        cur.execute("SELECT 1")
    with pytest.raises(drv.InterfaceError):
        c.cursor()


# ------------------------------------------------------------ transactions ---

def test_implicit_transaction_commit_and_rollback(db, drv):
    _, connect = db
    a, b = connect(), connect()
    b.autocommit = True
    try:
        with a.cursor() as cur:
            cur.execute("CREATE TABLE tx (n int)")
        assert a.get_transaction_status() == drv.TRANSACTION_STATUS_INTRANS
        a.commit()
        assert a.get_transaction_status() == drv.TRANSACTION_STATUS_IDLE
        with a.cursor() as cur:
            cur.execute("INSERT INTO tx VALUES (1)")
        assert one(b, "SELECT count(*) FROM tx") == (0,)
        a.rollback()
        with a:
            with a.cursor() as cur:
                cur.execute("INSERT INTO tx VALUES (2)")
        assert not a.closed
        assert one(b, "SELECT array_agg(n) FROM tx") == ([2],)
        with pytest.raises(RuntimeError):
            with a:
                with a.cursor() as cur:
                    cur.execute("INSERT INTO tx VALUES (3)")
                raise RuntimeError("boom")
        assert one(b, "SELECT array_agg(n) FROM tx") == ([2],)
        with a.cursor() as cur:
            with pytest.raises(drv.Error):
                cur.execute("SELECT 1 / 0")
            assert a.get_transaction_status() == drv.TRANSACTION_STATUS_INERROR
            with pytest.raises(drv.InternalError) as caught:
                cur.execute("SELECT 1")
            assert caught.value.pgcode == "25P02"
        a.rollback()
        assert one(a, "SELECT 1") == (1,)
        with pytest.raises(drv.ProgrammingError):
            a.autocommit = True  # inside the transaction the SELECT opened
        a.rollback()
        a.autocommit = True
        with a.cursor() as cur:
            cur.execute("INSERT INTO tx VALUES (4)")
        assert a.get_transaction_status() == drv.TRANSACTION_STATUS_IDLE
        assert one(b, "SELECT count(*) FROM tx WHERE n = 4") == (1,)
    finally:
        a.close()
        b.close()


def _backend_xmin(observer, pid):
    return one(observer, "SELECT backend_xmin IS NULL FROM pg_stat_activity WHERE pid = %s", (pid,))[0]


def test_an_idle_transaction_holds_no_snapshot_after_a_bound_statement(db, conn):
    """READ COMMITTED: once a statement finishes, an idle transaction keeps no
    snapshot (or CREATE INDEX CONCURRENTLY would wait on it)."""
    c = db[1]()
    try:
        pid = one(c, "SELECT pg_backend_pid()")[0]
        c.commit()
        one(c, "SELECT pg_try_advisory_xact_lock(%s)", (4242,))
        assert _backend_xmin(conn, pid) is True
        one(c, "SELECT count(*) FROM pg_class WHERE relname = %s", ("pg_class",))
        assert _backend_xmin(conn, pid) is True
    finally:
        c.close()


def test_cancel_from_another_thread(db, drv):
    c = db[1]()
    c.autocommit = True
    timer = threading.Timer(0.5, c.cancel)
    timer.start()
    try:
        with c.cursor() as cur, pytest.raises(drv.OperationalError) as caught:
            cur.execute("SELECT pg_sleep(10)")
        assert caught.value.pgcode == "57014"
        assert one(c, "SELECT 1") == (1,)
        c.cancel()  # nothing running: no effect
    finally:
        timer.cancel()
        c.close()


def test_readonly_autocommit_session(db, drv):
    c = db[1]()
    try:
        c.set_session(readonly=True, autocommit=True)
        assert one(c, "SHOW default_transaction_read_only") == ("on",)
        with c.cursor() as cur:
            with pytest.raises(drv.InternalError) as caught:
                cur.execute("CREATE TABLE ro_refused (n int)")
            assert caught.value.pgcode == "25006"
            cur.execute("SELECT set_config('statement_timeout', %s, false)", ("1234",))
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone() == ("1234ms",)
    finally:
        c.close()


# ------------------------------------------------------------- composition ---

def test_identifiers_are_quoted(conn, drv):
    sql = drv.sql
    weird = 'we"ird name; DROP'
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE TEMP TABLE {} ({} int)").format(
            sql.Identifier(weird), sql.Identifier("Col")))
        cur.execute(sql.SQL("INSERT INTO {t} ({c}) VALUES (%s)").format(
            t=sql.Identifier(weird), c=sql.Identifier("Col")), (7,))
        cur.execute(sql.SQL("SELECT {} FROM {}").format(sql.Identifier("Col"), sql.Identifier(weird)))
        assert cur.fetchall() == [(7,)]
        cur.execute(sql.SQL("SELECT {}").format(sql.Identifier("pg_catalog", "now")) + sql.SQL("()"))
        assert cur.fetchone()[0] is not None


@pytest.mark.parametrize("scs", ["on", "off"])
def test_literals_read_back_unchanged(conn, drv, scs):
    sql = drv.sql
    with conn.cursor() as cur:
        cur.execute(f"SET standard_conforming_strings = {scs}")
        for value in ["plain", "it's", "back\\slash", "\\'; SELECT 1; --", "end\\", "ünï"]:
            cur.execute(sql.SQL("SELECT {}").format(sql.Literal(value)))
            assert cur.fetchone() == (value,), (scs, value)
        cur.execute(sql.SQL("SELECT {} + 1").format(sql.Literal(5)))
        assert cur.fetchone() == (6,)


@pytest.mark.parametrize("scs", ["on", "off"])
def test_parameters_after_a_backslash_in_a_literal(conn, scs):
    """The placeholder after a literal holding a backslash binds where the
    server reads it, whatever standard_conforming_strings is."""
    with conn.cursor() as cur:
        cur.execute(f"SET standard_conforming_strings = {scs}")
        if scs == "off":
            cur.execute("SELECT 'it\\'s', %s", ("x",))
            assert cur.fetchone() == ("it's", "x")
        else:
            cur.execute("SELECT 'a\\', %s", ("x",))
            assert cur.fetchone() == ("a\\", "x")
        cur.execute("SET standard_conforming_strings = on")


def test_literal_in_ddl(db, drv):
    sql = drv.sql
    c = db[1]()
    c.autocommit = True
    role = "contract_role_" + uuid.uuid4().hex[:6]
    try:
        with c.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} CONNECTION LIMIT {}").format(
                sql.Identifier(role), sql.Literal(5)))
            cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                sql.Identifier(role), sql.Literal("30s")))
            cur.execute("SELECT rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s", (role,))
            assert cur.fetchone() == (5, ["statement_timeout=30s"])
            cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    finally:
        c.close()


# --------------------------------------------------------- connection setup ---

def test_parse_and_make_dsn(drv):
    uri = "postgresql://u%40x:p%3Ass@db.example.com:5433/my%20db?options=-csearch_path%3Derp&sslmode=require"
    assert drv.parse_dsn(uri) == {"user": "u@x", "password": "p:ss", "host": "db.example.com",
                                  "port": "5433", "dbname": "my db",
                                  "options": "-csearch_path=erp", "sslmode": "require"}
    kv = "host=h port=5 dbname='a b' password='x\\'y' user=u"
    assert drv.parse_dsn(kv) == {"host": "h", "port": "5", "dbname": "a b", "password": "x'y", "user": "u"}
    assert drv.parse_dsn(drv.make_dsn(kv, dbname="c d")) == {**drv.parse_dsn(kv), "dbname": "c d"}


def test_connect_keywords(db, drv):
    params, connect = db
    c = drv.connect(host="localhost", hostaddr="127.0.0.1", port=params["port"],
                    database=params["dbname"], user=params["user"], password=params["password"],
                    connect_timeout=5, application_name="contract-app",
                    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
                    options="-c statement_timeout=4321 -c search_path=contract_path")
    try:
        assert one(c, "SHOW statement_timeout") == ("4321ms",)
        assert one(c, "SHOW search_path") == ("contract_path",)
        assert one(c, "SELECT current_setting('application_name'), current_database()") == (
            "contract-app", params["dbname"])
        dsn = c.get_dsn_parameters()
        assert dsn["dbname"] == params["dbname"] and dsn["user"] == params["user"]
        assert "password" not in dsn
        assert {"host", "port"} <= set(dsn)
    finally:
        c.close()


def test_uri_with_search_path_option(db, drv):
    params, _ = db
    uri = (f"postgresql://{params['user']}:{params['password']}@{params['host']}:{params['port']}"
           f"/{params['dbname']}?options=-csearch_path%3Dsome_schema")
    c = drv.connect(uri)
    try:
        assert one(c, "SHOW search_path") == ("some_schema",)
    finally:
        c.close()


def _ssl_in_use(c):
    return one(c, "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")[0]


def test_ssl_modes(db, drv):
    params, connect = db
    probe = connect()
    try:
        server_ssl = one(probe, "SHOW ssl")[0] == "on"
        default_tls = _ssl_in_use(probe)
    finally:
        probe.close()
    assert default_tls is server_ssl  # sslmode=prefer by default
    c = connect(sslmode="disable")
    try:
        assert _ssl_in_use(c) is False
    finally:
        c.close()
    if server_ssl:
        c = connect(sslmode="require")
        try:
            assert _ssl_in_use(c) is True
        finally:
            c.close()


def test_allow_starts_in_plain_text(db, drv):
    c = db[1](sslmode="allow")
    try:
        assert _ssl_in_use(c) is False
    finally:
        c.close()


# Certificate checks need a server whose certificate a test CA signed for
# DNS:localhost only: host=localhost verifies, host=127.0.0.1 is a mismatch.
_CA = os.environ.get("ERP_SIM_TEST_PG_CA")
# A throwaway self-signed CA certificate (no key kept) that did not sign the
# test server's certificate.
_OTHER_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBlzCCAT2gAwIBAgIUHBBHxV91eNyaV5X38S9TjD2G2oEwCgYIKoZIzj0EAwIw
IDEeMBwGA1UEAwwVcGdkcml2ZXIgdW5pdCB0ZXN0IENBMCAXDTI2MTEyMjA1MzYw
M1oYDzIxMjYxMDI5MDUzNjAzWjAgMR4wHAYDVQQDDBVwZ2RyaXZlciB1bml0IHRl
c3QgQ0EwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATw89/6aB2fBcUiFjlbUp0b
vtN02eBvbOj6KtPdzIEN1c4trlhOtHyvqWR8J1v6JJ4MGUuFbX1GxCe4r3cTQMf7
o1MwUTAdBgNVHQ4EFgQUq+smv8ZrubJNR1ps0z7fNcGYTHkwHwYDVR0jBBgwFoAU
q+smv8ZrubJNR1ps0z7fNcGYTHkwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQD
AgNIADBFAiEAgiTx1zj9qoR9/ENvr0Csv7z2e4uAR55yx4bYFb1M3p4CIHvCwm27
86DMgSKchtI17OYSZvv8ijHz75o3zeXo3OtS
-----END CERTIFICATE-----
"""


@pytest.fixture(scope="module")
def other_ca(tmp_path_factory):
    path = tmp_path_factory.mktemp("ca") / "other-ca.crt"
    path.write_text(_OTHER_CA_PEM)
    return str(path)


@pytest.fixture
def tls(db):
    if not _CA:
        pytest.skip("set ERP_SIM_TEST_PG_CA to the CA that signed the server certificate")
    return db[1]


def _verified(connect, **kwargs):
    c = connect(**kwargs)
    try:
        assert _ssl_in_use(c) is True
    finally:
        c.close()


def test_verify_full_refuses_a_host_name_mismatch(tls, drv):
    _verified(tls, host="localhost", sslmode="verify-full", sslrootcert=_CA)
    with pytest.raises(drv.OperationalError, match="does not match host name"):
        tls(host="127.0.0.1", sslmode="verify-full", sslrootcert=_CA)


def test_verify_ca_checks_the_issuer_not_the_name(tls, drv, other_ca):
    _verified(tls, host="127.0.0.1", sslmode="verify-ca", sslrootcert=_CA)
    with pytest.raises(drv.OperationalError, match="certificate verify failed"):
        tls(host="localhost", sslmode="verify-ca", sslrootcert=other_ca)


def test_require_verifies_the_issuer_when_a_root_certificate_exists(tls, drv, other_ca, tmp_path):
    _verified(tls, sslmode="require", sslrootcert=_CA)
    with pytest.raises(drv.OperationalError, match="certificate verify failed"):
        tls(sslmode="require", sslrootcert=other_ca)
    _verified(tls, sslmode="require", sslrootcert=str(tmp_path / "none.crt"))


def test_prefer_checks_an_existing_root_certificate_and_falls_back(tls, drv, other_ca, tmp_path):
    # The server accepts plain text, so a failed check ends in plain text.
    _verified(tls, host="localhost", sslmode="prefer", sslrootcert=_CA)
    c = tls(host="localhost", sslmode="prefer", sslrootcert=other_ca)
    try:
        assert _ssl_in_use(c) is False
    finally:
        c.close()
    _verified(tls, host="localhost", sslmode="prefer", sslrootcert=str(tmp_path / "none.crt"))


def test_an_unreadable_root_certificate_is_refused(tls, drv, tmp_path):
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n")
    for mode in ("require", "verify-ca", "verify-full"):
        with pytest.raises(drv.OperationalError, match="could not read root certificate file"):
            tls(host="localhost", sslmode=mode, sslrootcert=str(bad))
    # prefer gives up on TLS and connects in plain text, as libpq does.
    c = tls(host="localhost", sslmode="prefer", sslrootcert=str(bad))
    try:
        assert _ssl_in_use(c) is False
    finally:
        c.close()


def test_an_empty_sslmode_keyword_is_not_replaced_by_the_environment(tls, drv, monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "require")
    with pytest.raises(drv.Error, match='invalid sslmode value: ""'):
        tls(sslmode="")


def test_verify_modes_need_the_root_certificate_file(tls, drv, tmp_path):
    for mode in ("verify-ca", "verify-full"):
        with pytest.raises(drv.OperationalError, match="root certificate file .* does not exist"):
            tls(host="localhost", sslmode=mode, sslrootcert=str(tmp_path / "none.crt"))


def test_system_roots_need_verify_full(tls, drv):
    with pytest.raises(drv.OperationalError, match="certificate verify failed"):
        tls(host="localhost", sslmode="verify-full", sslrootcert="system")
    with pytest.raises(drv.OperationalError, match="weak sslmode"):
        tls(sslmode="require", sslrootcert="system")


def test_ssl_environment_variables_and_params_win(tls, drv, other_ca, monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    monkeypatch.setenv("PGSSLROOTCERT", _CA)
    _verified(tls, host="localhost")
    with pytest.raises(drv.OperationalError, match="does not match host name"):
        tls(host="127.0.0.1")
    _verified(tls, host="127.0.0.1", sslmode="verify-ca")
    monkeypatch.setenv("PGSSLROOTCERT", other_ca)
    _verified(tls, host="localhost", sslrootcert=_CA)


def test_scram_verifier_logs_in(db, drv):
    params, connect = db
    admin = connect()
    admin.autocommit = True
    role = "contract_login_" + uuid.uuid4().hex[:6]
    password = "pw-" + uuid.uuid4().hex + "\\'q"
    try:
        verifier = drv.encrypt_password(password, role, admin, "scram-sha-256")
        assert verifier.startswith("SCRAM-SHA-256$4096:") and password not in verifier
        with admin.cursor() as cur:
            cur.execute(drv.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                drv.sql.Identifier(role), drv.sql.Literal(verifier)))
        c = connect(user=role, password=password)
        assert one(c, "SELECT current_user") == (role,)
        c.close()
        with pytest.raises(drv.OperationalError):
            connect(user=role, password=password + "x")
    finally:
        with admin.cursor() as cur:
            cur.execute(drv.sql.SQL("DROP ROLE IF EXISTS {}").format(drv.sql.Identifier(role)))
        admin.close()


# ------------------------------------------------------------ pool, threads ---

def test_simple_pool(db, drv):
    params, _ = db
    pool = drv.SimpleConnectionPool(1, 2, drv.make_dsn(**params))
    try:
        a = pool.getconn()
        b = pool.getconn()
        with pytest.raises(drv.PoolError if hasattr(drv, "PoolError") else Exception):
            pool.getconn()
        with a.cursor() as cur:
            cur.execute("SELECT 1")
        pool.putconn(a)
        pool.putconn(b)
        again = pool.getconn()
        assert again.get_transaction_status() == drv.TRANSACTION_STATUS_IDLE
        pool.putconn(again)
    finally:
        pool.closeall()


def test_connection_shared_by_threads(conn):
    errors, results = [], []

    def work(n):
        try:
            for _ in range(20):
                with conn.cursor() as cur:
                    cur.execute("SELECT %s::int, pg_sleep(0)", (n,))
                    results.append((n, cur.fetchone()[0]))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 80 and all(n == got for n, got in results)
