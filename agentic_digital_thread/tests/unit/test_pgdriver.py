# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``agents.pgdriver`` without a server: placeholder scanning, literal and
identifier quoting, connection strings, parameter types, row converters,
error text and SCRAM verifiers. The behaviour against PostgreSQL itself is in
``tests/integration/test_pgdriver_contract_pg.py``."""
import base64
import datetime as dt
import decimal
import hashlib
import hmac
import os
import ssl
import types

import pytest

from agents import pgdriver
from agents.pgdriver import _scan, quote_literal, sql

# A throwaway self-signed CA certificate (no key kept), only to build
# verifying TLS contexts without a server.
_TEST_CA = """-----BEGIN CERTIFICATE-----
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


# ------------------------------------------------------------------ _scan ---

@pytest.mark.parametrize("query,args,text,values", [
    ("SELECT %s, %s", ("a", 2), "SELECT $1, $2", ["a", 2]),
    ("SELECT %(x)s, %(y)s, %(x)s", {"x": 1, "y": 2}, "SELECT $1, $2, $1", [1, 2]),
    ("SELECT 'a%%b' LIKE %s", ("a%",), "SELECT 'a%b' LIKE $1", ["a%"]),
    ("SELECT %s, %s", (None, 1), "SELECT NULL, $1", [1]),
    ("SELECT %(o)s IS NULL OR o = %(o)s", {"o": None}, "SELECT NULL IS NULL OR o = NULL", []),
    ("SELECT 1", (), "SELECT 1", []),
    ("SELECT $$%%$$, %s", (1,), "SELECT $$%$$, $1", [1]),
    ("SELECT E'\\'%%', %s", (1,), "SELECT E'\\'%', $1", [1]),
    ('SELECT "a""%%b", %s', (1,), 'SELECT "a""%b", $1', [1]),
    ("SELECT 1 /* a /* b */ %% */, %s", (1,), "SELECT 1 /* a /* b */ % */, $1", [1]),
    ("SELECT a$b, %s", (1,), "SELECT a$b, $1", [1]),
])
def test_scan_rewrites_placeholders(query, args, text, values):
    assert _scan(query, args) == (text, values)


@pytest.mark.parametrize("query", [
    "SELECT INTERVAL '%s days'",
    "SELECT \"col%s\"",
    "SELECT $q$ %s $q$",
    "SELECT 1 -- %s\n",
    "SELECT 1 /* %s */",
    "SELECT E'\\' %s'",
])
def test_scan_refuses_a_placeholder_inside_quotes_or_comments(query):
    with pytest.raises(pgdriver.ProgrammingError, match="bind the whole value"):
        _scan(query, (1,))


def test_scan_reads_backslash_quotes_as_the_session_does():
    """With standard_conforming_strings off, ``\\'`` does not close a literal,
    so a placeholder after it is still inside the literal (and a ``$1`` there
    is literal text, not a parameter)."""
    query = "SELECT 'lit\\' %s', $1"
    with pytest.raises(pgdriver.ProgrammingError, match="bind the whole value"):
        _scan(query, (1,), backslash_quotes=True)
    # Standard strings: 'lit\' is closed, %s is a parameter, and the rest is an
    # unterminated literal the server refuses.
    assert _scan(query, (1,)) == ("SELECT 'lit\\' $1', $1", [1])
    assert _scan("SELECT 'it\\'s', %s", ("x",), backslash_quotes=True) == (
        "SELECT 'it\\'s', $1", ["x"])
    assert _scan("SELECT 'a\\', %s", ("x",)) == ("SELECT 'a\\', $1", ["x"])


class _FakeWire:
    """Just enough of pg8000's connection for Cursor.execute without a server."""

    def __init__(self):
        self.parameter_statuses = {"standard_conforming_strings": "on"}
        self._transaction_status = b"I"
        self.sent = []

    def register_in_adapter(self, oid, func):
        pass

    def _context(self):
        return types.SimpleNamespace(columns=None, rows=None, row_count=-1)

    def execute_simple(self, text):
        self.sent.append((text, None))
        return self._context()

    def execute_unnamed(self, text, vals=(), oids=()):
        self.sent.append((text, vals))
        return self._context()


def test_the_string_mode_is_read_under_the_connection_lock(monkeypatch):
    """Another thread cannot change standard_conforming_strings between the
    placeholder scan and the execution: both happen under the lock."""
    conn = pgdriver.Connection(_FakeWire(), {"host": "h", "port": 5432})
    conn.autocommit = True
    held = []
    real = pgdriver.Connection._backslash_quotes
    monkeypatch.setattr(pgdriver.Connection, "_backslash_quotes",
                        lambda self: held.append(self._lock._is_owned()) or real(self))
    conn.cursor().execute("SELECT %s", ("x",))
    assert held == [True]
    assert conn._raw.sent == [("SELECT $1", ("x",))]


def test_scan_argument_errors():
    with pytest.raises(IndexError):
        _scan("SELECT %s, %s", (1,))
    with pytest.raises(TypeError, match="not all arguments converted"):
        _scan("SELECT %s", (1, 2))
    with pytest.raises(ValueError, match="unsupported format character"):
        _scan("SELECT 50% off", ())
    with pytest.raises(pgdriver.ProgrammingError, match="mixed"):
        _scan("SELECT %s", {"a": 1})
    with pytest.raises(TypeError, match="mapping"):
        _scan("SELECT %(a)s", (1,))
    with pytest.raises(KeyError):
        _scan("SELECT %(a)s", {"b": 1})
    with pytest.raises(pgdriver.ProgrammingError, match="not \\$n"):
        _scan("SELECT $1", ())


# --------------------------------------------------------------- literals ---

@pytest.mark.parametrize("value,text", [
    (None, "NULL"), (True, "true"), (False, "false"), (5, "5"), (-7, "-7"),
    (1.5, "1.5"), (decimal.Decimal("2.50"), "2.50"),
    ("plain", "'plain'"), ("it's", "'it''s'"),
    ("a\\b", "E'a\\\\b'"), ("\\'; DROP", "E'\\\\''; DROP'"), ("end\\", "E'end\\\\'"),
])
def test_quote_literal_matches_postgres_quote_literal(value, text):
    assert quote_literal(value) == text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), decimal.Decimal("NaN"), b"x", object()])
def test_quote_literal_refuses_what_it_cannot_write(value):
    with pytest.raises(pgdriver.ProgrammingError):
        quote_literal(value)


def test_quote_literal_refuses_nul():
    with pytest.raises(ValueError):
        quote_literal("a\x00b")


def test_identifiers_and_composition():
    assert sql.Identifier('we"ird').as_string() == '"we""ird"'
    assert sql.Identifier("s", "t").as_string() == '"s"."t"'
    for bad in ("", "a\x00b"):
        with pytest.raises(ValueError):
            sql.Identifier(bad)
    with pytest.raises(TypeError):
        sql.Identifier(5)
    stmt = sql.SQL("SELECT {} FROM {t} WHERE x = {}").format(
        sql.Identifier("c"), sql.Literal("v"), t=sql.Identifier("tbl"))
    assert stmt.as_string(None) == "SELECT \"c\" FROM \"tbl\" WHERE x = 'v'"
    assert sql.SQL(", ").join([sql.Identifier("a"), sql.Identifier("b")]).as_string() == '"a", "b"'
    assert (sql.SQL("a") + sql.SQL("b")).as_string() == "ab"
    assert sql.SQL("{{}} {}").format(sql.SQL("x")).as_string() == "{} x"
    with pytest.raises(TypeError):
        sql.SQL("{}").format("not composable")
    with pytest.raises(ValueError):
        sql.SQL("{!r}").format(sql.SQL("x"))
    assert sql.Identifier("a") == sql.Identifier("a") and sql.Literal(1) != sql.Literal("1")


# ---------------------------------------------------------------- dsn ---

def test_parse_dsn_forms():
    assert pgdriver.parse_dsn("postgresql://u:p%2Fw@[::1]:6543/d?sslmode=disable") == {
        "user": "u", "password": "p/w", "host": "::1", "port": "6543", "dbname": "d",
        "sslmode": "disable"}
    assert pgdriver.parse_dsn("postgres://h") == {"host": "h"}
    assert pgdriver.parse_dsn(" host = h  dbname='x y' password=a\\ b ") == {
        "host": "h", "dbname": "x y", "password": "a b"}
    for bad in ("host", "host=h =x", "dbname='open"):
        with pytest.raises(pgdriver.ProgrammingError):
            pgdriver.parse_dsn(bad)
    with pytest.raises(pgdriver.ProgrammingError):
        pgdriver.parse_dsn("postgresql://a,b/d")


def test_make_dsn_quotes_and_round_trips():
    dsn = pgdriver.make_dsn("host=h", dbname="a b", password="x'y\\z", database=None, port=5)
    assert pgdriver.parse_dsn(dsn) == {"host": "h", "dbname": "a b", "password": "x'y\\z", "port": "5"}
    assert pgdriver.parse_dsn(pgdriver.make_dsn(database="d")) == {"dbname": "d"}


def test_connect_refuses_bad_options_before_any_network():
    with pytest.raises(pgdriver.ProgrammingError, match="invalid connection option"):
        pgdriver.connect(host="h", user="u", sslcompression="1")
    with pytest.raises(pgdriver.ProgrammingError, match="sslmode"):
        pgdriver.connect(host="h", user="u", sslmode="bogus")
    with pytest.raises(pgdriver.ProgrammingError, match="user"):
        pgdriver.connect(host="h")
    with pytest.raises(pgdriver.NotSupportedError):
        pgdriver.connect(host="/var/run/postgresql", user="u")


# ---------------------------------------------------------------- tls ---

@pytest.fixture
def no_pg_env(monkeypatch):
    for name in ("PGSSLMODE", "PGSSLROOTCERT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _tls(**kwargs):
    params, _ = pgdriver._connect_params(None, {"user": "u", **kwargs})
    return params["sslmode"], params.get("sslrootcert")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", None, "db.example.com", "10.0.0.5"])
def test_default_sslmode_is_libpq_prefer_for_every_host(no_pg_env, host):
    assert _tls(host=host)[0] == "prefer"
    # libpq: sslrootcert=system makes verify-full the default.
    assert _tls(host=host, sslrootcert="system") == ("verify-full", "system")


def test_ssl_environment_variables_and_params_win(no_pg_env):
    no_pg_env.setenv("PGSSLMODE", "require")
    no_pg_env.setenv("PGSSLROOTCERT", "/env/root.crt")
    assert _tls(host="localhost") == ("require", "/env/root.crt")
    assert _tls(host="db.example.com", sslmode="verify-ca", sslrootcert="/p.crt") == (
        "verify-ca", "/p.crt")
    assert pgdriver.parse_dsn(pgdriver.make_dsn(host="h", sslmode="disable"))["sslmode"] == "disable"
    no_pg_env.setenv("PGSSLMODE", "bogus")
    with pytest.raises(pgdriver.ProgrammingError, match="sslmode"):
        _tls(host="h")


def test_root_certificate_defaults_to_libpq_location(no_pg_env):
    assert _tls(host="db.example.com")[1] == os.path.expanduser("~/.postgresql/root.crt")


def test_ssl_contexts_follow_libpq(tmp_path):
    """REPRODUCTION: require with no root certificate built its own context
    with check_hostname=False / CERT_NONE, and verify modes with a missing
    root file failed with a non-libpq message."""
    import ssl
    missing = str(tmp_path / "none.crt")
    ca = tmp_path / "ca.crt"
    ca.write_text(_TEST_CA)
    assert pgdriver._ssl_context("disable", missing) is False
    # allow, prefer and require with no root certificate file: the TLS
    # attempt encrypts without checking (libpq).
    for mode in ("allow", "require"):
        assert pgdriver._ssl_context(mode, missing) is True
    # prefer with no root file: pg8000's opportunistic TLS, which goes on in
    # plain text on the same socket when the server has no TLS (libpq).
    assert pgdriver._ssl_context("prefer", missing) is None
    for mode, hostname in (("allow", False), ("prefer", False), ("require", False),
                           ("verify-ca", False), ("verify-full", True)):
        ctx = pgdriver._ssl_context(mode, str(ca))
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is hostname
    for mode in ("verify-ca", "verify-full"):
        with pytest.raises(pgdriver.OperationalError, match="root certificate file .* does not exist"):
            pgdriver._ssl_context(mode, missing)
    system = pgdriver._ssl_context("verify-full", "system")
    assert system.verify_mode == ssl.CERT_REQUIRED and system.check_hostname
    for mode in ("disable", "allow", "prefer", "require", "verify-ca"):
        with pytest.raises(pgdriver.OperationalError, match="weak sslmode"):
            pgdriver._connect_params(None, {"host": "h", "user": "u", "sslmode": mode,
                                            "sslrootcert": "system"})


def _fake_login(monkeypatch, fail):
    """Record each attempt's ssl_context; ``fail(ssl_context)`` returns the
    error that attempt raises, or ``None`` to connect."""
    tried = []

    def login(params, timeout, ssl_context, cursor_factory):
        tried.append(ssl_context)
        err = fail(ssl_context)
        if err is not None:
            raise err
        return "conn"

    monkeypatch.setattr(pgdriver, "_login", login)
    return tried


def test_allow_retries_with_tls_after_any_server_error(no_pg_env):
    """REPRODUCTION: allow retried with TLS only on 28000; libpq moves on to
    TLS after any server error during startup (a wrong password too)."""
    for code in ("28000", "28P01"):
        tried = _fake_login(no_pg_env, lambda ctx: None if ctx is not False else
                            pgdriver.OperationalError("refused", pgcode=code))
        assert pgdriver.connect(host="h", user="u", sslmode="allow") == "conn"
        assert tried == [False, True]
    # No server answer (a socket error): no second attempt.
    tried = _fake_login(no_pg_env, lambda ctx: pgdriver.OperationalError("connection refused"))
    with pytest.raises(pgdriver.OperationalError):
        pgdriver.connect(host="h", user="u", sslmode="allow")
    assert tried == [False]


def test_prefer_falls_back_to_plain_text_as_libpq_does(no_pg_env, tmp_path):
    """REPRODUCTION: prefer passed pg8000's opportunistic context, which
    ignores the root certificate file. libpq checks the issuer when the file
    exists and, as prefer allows, retries in plain text when TLS fails or the
    server answers the TLS attempt with an error."""
    ca = tmp_path / "ca.crt"
    ca.write_text(_TEST_CA)
    tls_failures = (
        pgdriver._TLSError("certificate verify failed"),
        pgdriver._TLSError("server does not support SSL"),
        pgdriver.OperationalError("password authentication failed", pgcode="28P01"),
    )
    for err in tls_failures:
        tried = _fake_login(no_pg_env, lambda ctx, err=err: None if ctx is False else err)
        assert pgdriver.connect(host="h", user="u", sslrootcert=str(ca)) == "conn"
        assert len(tried) == 2 and tried[1] is False
        assert tried[0].verify_mode == ssl.CERT_REQUIRED and not tried[0].check_hostname
    tried = _fake_login(no_pg_env, lambda ctx: None)
    assert pgdriver.connect(host="h", user="u", sslrootcert=str(tmp_path / "none.crt")) == "conn"
    assert tried == [None]
    tried = _fake_login(no_pg_env, lambda ctx: pgdriver.OperationalError("connection refused"))
    with pytest.raises(pgdriver.OperationalError):
        pgdriver.connect(host="h", user="u")
    assert len(tried) == 1


def test_explicit_empty_tls_keywords_are_not_replaced_by_the_environment(no_pg_env):
    """REPRODUCTION: an empty keyword fell back to PGSSLMODE / PGSSLROOTCERT;
    libpq keeps the keyword (an empty sslmode is invalid, an empty
    sslrootcert means the default root file)."""
    no_pg_env.setenv("PGSSLMODE", "require")
    no_pg_env.setenv("PGSSLROOTCERT", "/env/should-not-win.crt")
    with pytest.raises(pgdriver.Error, match='invalid sslmode value: ""'):
        _tls(host="h", sslmode="")
    assert _tls(host="h", sslrootcert="")[1] == os.path.expanduser("~/.postgresql/root.crt")


def test_an_unreadable_root_certificate_is_an_operational_error(tmp_path):
    """REPRODUCTION: a root file that is not a certificate leaked ssl.SSLError."""
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n")
    for mode in ("prefer", "require", "verify-ca", "verify-full"):
        with pytest.raises(pgdriver.OperationalError, match="could not read root certificate file"):
            pgdriver._ssl_context(mode, str(bad))


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_tls_failures_escape_as_plain_operational_errors(no_pg_env, tmp_path, mode):
    """REPRODUCTION: the internal prefer-fallback signal escaped as its own
    class; psycopg2 raises OperationalError itself."""
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n")
    with pytest.raises(pgdriver.OperationalError, match="could not read root certificate") as info:
        pgdriver.connect(host="h", user="u", sslmode=mode, sslrootcert=str(bad))
    assert type(info.value) is pgdriver.OperationalError
    _fake_login(no_pg_env, lambda ctx: pgdriver._TLSError("certificate verify failed"))
    with pytest.raises(pgdriver.OperationalError) as info:
        pgdriver.connect(host="h", user="u", sslmode=mode, sslrootcert="system"
                         if mode == "verify-full" else _write(tmp_path, _TEST_CA))
    assert type(info.value) is pgdriver.OperationalError


def _write(tmp_path, text):
    path = tmp_path / "ca.crt"
    path.write_text(text)
    return str(path)


def test_prefer_does_not_reconnect_when_the_server_has_no_tls(monkeypatch, tmp_path):
    """REPRODUCTION: prefer without a root file passed ssl_context=True; a
    server without TLS then cost a second TCP connection. libpq goes on in
    plain text on the same socket, as pg8000's ssl_context=None does."""
    opened = []

    class _Sock:
        def setsockopt(self, *a):
            pass

        def close(self):
            pass

        def settimeout(self, value):
            pass

    def create_connection(address, timeout):
        opened.append(address)
        return _Sock()

    def wire(user, **kw):
        if kw["ssl_context"] is not None and kw["ssl_context"] is not False:
            raise pgdriver._pgexc.InterfaceError("Server refuses SSL")
        return types.SimpleNamespace(_usock=kw["sock"])

    monkeypatch.delenv("PGSSLMODE", raising=False)
    monkeypatch.setenv("PGSSLROOTCERT", str(tmp_path / "none.crt"))
    monkeypatch.setattr(pgdriver.socket, "create_connection", create_connection)
    monkeypatch.setattr(pgdriver, "_Wire", wire)
    monkeypatch.setattr(pgdriver, "Connection", lambda raw, params, cursor_factory=None: "conn")
    assert pgdriver.connect(host="h", user="u") == "conn"
    assert len(opened) == 1


@pytest.mark.parametrize("mode", ["allow", "prefer"])
def test_a_server_that_cannot_accept_connections_now_is_not_retried(no_pg_env, mode):
    """REPRODUCTION: 57P03 (the server is starting or stopping) made a second
    attempt with the other encryption; libpq does not retry it."""
    tried = _fake_login(no_pg_env, lambda ctx: pgdriver.OperationalError(
        "the database system is starting up", pgcode="57P03"))
    with pytest.raises(pgdriver.OperationalError, match="starting up"):
        pgdriver.connect(host="h", user="u", sslmode=mode)
    assert len(tried) == 1


class _RecordingSocket:
    def __init__(self):
        self.options = {}

    def setsockopt(self, level, option, value):
        self.options[(level, option)] = value


@pytest.mark.parametrize("params", [{}, {"keepalives": "0"}], ids=["keepalives", "no-keepalives"])
def test_the_socket_turns_nagle_off_as_libpq_does(monkeypatch, params):
    """REPRODUCTION: without TCP_NODELAY the TLS login's small writes wait
    for the server's delayed ACK, about 50 ms per connection (libpq always
    sets it; the app opens one connection per query)."""
    import socket

    made = []

    def create_connection(address, timeout):
        made.append((address, timeout))
        return _RecordingSocket()

    monkeypatch.setattr(pgdriver.socket, "create_connection", create_connection)
    sock = pgdriver._open_socket("db.example.com", 5432, 5.0, params)
    assert made == [(("db.example.com", 5432), 5.0)]
    assert sock.options[(socket.IPPROTO_TCP, socket.TCP_NODELAY)] == 1


# ---------------------------------------------------------- parameter types ---

@pytest.mark.parametrize("value,oid", [
    ("s", 0), ([1], 0), (True, 16), (1, 23), (2 ** 40, 20), (2 ** 70, 1700),
    (1.5, 1700), (float("nan"), 701), (decimal.Decimal("1"), 1700),
    (dt.datetime(2026, 1, 1), 1114), (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 1184),
    (dt.date(2026, 1, 1), 1082), (dt.time(1, 2), 1083), (dt.timedelta(1), 1186), (b"x", 17),
])
def test_parameter_types_follow_sql_text_literals(value, oid):
    assert pgdriver._oid(value) == oid


@pytest.mark.parametrize("value", [{"a": 1}, (1, 2), object()])
def test_unadaptable_parameters_are_refused(value):
    with pytest.raises(pgdriver.ProgrammingError, match="can't adapt"):
        pgdriver._oid(value)


@pytest.mark.parametrize("text,expected", [
    ("1 day 02:00:00", dt.timedelta(days=1, hours=2)),
    ("1 year 2 mons 3 days", dt.timedelta(days=365 + 60 + 3)),
    ("-1 days +02:00:00.5", dt.timedelta(days=-1, hours=2, milliseconds=500)),
    ("-00:00:01", dt.timedelta(seconds=-1)),
    ("00:00:00", dt.timedelta(0)),
])
def test_interval_values(text, expected):
    assert pgdriver._interval_in(text) == expected


# ------------------------------------------------------------------ errors ---

def test_server_error_text_and_class():
    err = pgdriver._server_error({"S": "ERROR", "C": "42P01", "M": 'relation "x" does not exist',
                                  "P": "15", "H": "check it"}, "SELECT * FROM x")
    assert isinstance(err, pgdriver.ProgrammingError) and err.pgcode == "42P01"
    assert str(err) == ('relation "x" does not exist\nLINE 1: SELECT * FROM x\n'
                        "                      ^\nHINT:  check it\n")
    assert err.pgerror.startswith('ERROR:  relation "x"')
    assert isinstance(pgdriver._server_error({"C": "57014", "M": "t"}, None), pgdriver.QueryCanceledError)
    assert isinstance(pgdriver._server_error({"C": "23505", "M": "u"}, None), pgdriver.IntegrityError)
    assert isinstance(pgdriver._server_error({"C": "40P01", "M": "d"}, None),
                      pgdriver.TransactionRollbackError)
    assert type(pgdriver._server_error({"C": "ZZ000", "M": "?"}, None)) is pgdriver.DatabaseError


def test_error_line_points_at_the_right_line():
    assert pgdriver._error_line("SELECT 1\nFROM nope", "15") == "LINE 2: FROM nope\n             ^\n"
    assert pgdriver._error_line("SELECT 1", None) == ""


# ---------------------------------------------------------------- passwords ---

def test_scram_verifier_is_postgres_format():
    verifier = pgdriver.encrypt_password("pässword", "role", None, "scram-sha-256")
    head, keys = verifier.split("$", 1)[1].split("$")
    iterations, salt = head.split(":")
    stored, server = (base64.b64decode(k) for k in keys.split(":"))
    assert verifier.startswith("SCRAM-SHA-256$4096:") and iterations == "4096"
    salted = hashlib.pbkdf2_hmac("sha256", "pässword".encode(), base64.b64decode(salt), 4096)
    assert stored == hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    assert server == hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    assert verifier != pgdriver.encrypt_password("pässword", "role")  # fresh salt
    with pytest.raises(pgdriver.NotSupportedError):
        pgdriver.encrypt_password("p", "r", None, "md5")
