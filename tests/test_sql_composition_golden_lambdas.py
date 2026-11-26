# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SQL the Lambda handlers send, pinned statement by statement.

Each handler builds DDL and counts with identifiers that cannot be bound as
parameters. These tests run the real functions against a recording cursor and
pin the exact text and parameters of every statement, so changing how a
statement is composed (f-string or ``pgdriver.sql``) cannot change what the
database receives.

A ``pgdriver.sql`` object is rendered without a connection: ``SQL`` pieces as
written, ``Identifier`` pieces double-quoted (a quote doubled), ``Literal``
pieces as the quoted literal pgdriver writes. Pieces are matched by class name, so the copy of
pgdriver a handler imports (its own, its layer's or the ERP simulator's) does
not matter.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _load(relative: str):
    """The module at *relative*; boto3 stands in only when not installed."""
    stand_in = False
    try:
        __import__("boto3")
    except ImportError:
        sys.modules["boto3"] = types.ModuleType("boto3")
        stand_in = True
    sys.path.insert(0, str(_REPO))
    try:
        spec = importlib.util.spec_from_file_location(f"golden_{uuid.uuid4().hex[:8]}", _REPO / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_REPO))
        if stand_in:
            del sys.modules["boto3"]


def render(query) -> str:
    """The statement text a connection would receive."""
    if isinstance(query, str):
        return query
    kind = type(query).__name__
    if kind == "Composed":
        return "".join(render(part) for part in query.seq)
    if kind == "SQL":
        return query.string
    if kind == "Identifier":
        return ".".join('"' + part.replace('"', '""') + '"' for part in query.strings)
    if kind == "Literal":
        return query.as_string(None)               # quoted, as pgdriver writes it
    raise AssertionError(f"unrenderable query part: {query!r}")


class RecordingCursor:
    """Records ``(text, params)``; ``fetchone`` answers come from ``answers``
    keyed by a statement prefix (default: one row of zero)."""

    def __init__(self, answers=None):
        self.executed: list[tuple[str, object]] = []
        self.answers = answers or {}
        self._last = ""
        self.rowcount = 0

    def execute(self, query, params=None):
        self._last = render(query)
        self.executed.append((self._last, params))

    def fetchone(self):
        for prefix, row in self.answers.items():
            if self._last.startswith(prefix):
                return row
        return (0,)

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    closed = False


def ddl_of(cursor):
    """The statements that change the database (not the catalog reads)."""
    return [item for item in cursor.executed
            if item[0].startswith(("CREATE", "ALTER", "GRANT", "DROP", "REVOKE"))]


def texts(cursor, prefix=""):
    return [(text, params) for text, params in cursor.executed if text.startswith(prefix)]


# --- lambda-db-reader ----------------------------------------------------------

_SCRAM = "SCRAM-SHA-256$4096:salt$stored:server"


@pytest.fixture(scope="module")
def reader():
    from _pg_sql import pgdriver

    pgdriver()                                     # the layer's module, importable as pgdriver
    return _load("lambda-db-reader/handler.py")


def _setup(reader, monkeypatch, *, role_exists, schema_present):
    monkeypatch.setattr(reader, "_password_verifier", lambda conn, role, password: _SCRAM)
    monkeypatch.setattr(reader, "_verify", lambda *args: ["t1"])
    cursor = RecordingCursor({
        "SELECT current_database()": ("adt_db", "owner_login"),
        "SELECT rolsuper": (False, False, False) if role_exists else None,
        "SELECT 1 FROM pg_namespace": (1,) if schema_present else None,
    })
    return cursor, RecordingConn(cursor)


def test_reader_setup_ddl_for_a_new_role_and_present_schema(reader, monkeypatch):
    cursor, conn = _setup(reader, monkeypatch, role_exists=False, schema_present=True)
    reader.ensure_schema_reader(conn, "adt_reader_a", "pw", "erp_data")
    ddl = ddl_of(cursor)
    assert ddl == [
        # A role statement cannot bind: the verifier is a quoted literal.
        (f"CREATE ROLE \"adt_reader_a\" WITH {reader._CREATE_ATTRIBUTES} PASSWORD '{_SCRAM}'", None),
        ('GRANT CONNECT ON DATABASE "adt_db" TO "adt_reader_a"', None),
        ('GRANT USAGE ON SCHEMA "erp_data" TO "adt_reader_a"', None),
        ('GRANT SELECT ON ALL TABLES IN SCHEMA "erp_data" TO "adt_reader_a"', None),
        ('ALTER DEFAULT PRIVILEGES IN SCHEMA "erp_data" GRANT SELECT ON TABLES TO "adt_reader_a"', None),
        ('ALTER ROLE "adt_reader_a" SET default_transaction_read_only = on', None),
        (f'ALTER ROLE "adt_reader_a" SET statement_timeout = {reader.READER_STATEMENT_TIMEOUT_MS}', None),
    ]


def test_reader_setup_ddl_for_an_existing_role_and_missing_schema(reader, monkeypatch):
    cursor, conn = _setup(reader, monkeypatch, role_exists=True, schema_present=False)
    reader.ensure_schema_reader(conn, "adt_reader_a", "pw", "erp_data")
    ddl = ddl_of(cursor)
    assert ddl == [
        (f'ALTER ROLE "adt_reader_a" WITH {reader._RESET_ATTRIBUTES} PASSWORD \'{_SCRAM}\'', None),
        ('GRANT CONNECT ON DATABASE "adt_db" TO "adt_reader_a"', None),
        ('ALTER ROLE "adt_reader_a" SET default_transaction_read_only = on', None),
        (f'ALTER ROLE "adt_reader_a" SET statement_timeout = {reader.READER_STATEMENT_TIMEOUT_MS}', None),
    ]


def test_reader_setup_quotes_the_database_name_read_from_the_catalog(reader, monkeypatch):
    cursor, conn = _setup(reader, monkeypatch, role_exists=True, schema_present=False)
    cursor.answers["SELECT current_database()"] = ('we"ird', "owner_login")
    reader.ensure_schema_reader(conn, "adt_reader_a", "pw", "erp_data")
    assert ('GRANT CONNECT ON DATABASE "we""ird" TO "adt_reader_a"', None) in cursor.executed


@pytest.mark.parametrize("role, schema", [
    ('adt"; DROP ROLE x; --', "erp_data"), ("adt_reader_a", "Erp Data"), ("", "erp_data"),
    ("adt_reader_a", "erp_data; select 1"), ("UPPER", "erp_data"),
])
def test_reader_setup_refuses_a_name_that_is_not_a_plain_identifier(reader, monkeypatch, role, schema):
    cursor, conn = _setup(reader, monkeypatch, role_exists=False, schema_present=True)
    with pytest.raises(ValueError, match="not a plain lower-case identifier"):
        reader.ensure_schema_reader(conn, role, "pw", schema)
    assert cursor.executed == []


def test_reader_drop_runs_nologin_then_drop_role(reader):
    cursor = RecordingCursor({"SELECT rolsuper": (False, False, False)})
    assert reader.drop_reader(RecordingConn(cursor), "adt_reader_a") is True
    assert ddl_of(cursor) == [
        ('ALTER ROLE "adt_reader_a" NOLOGIN', None),
        ('DROP ROLE "adt_reader_a"', None),
    ]


@pytest.mark.parametrize("role", ['adt"; DROP ROLE x; --', "UPPER", ""])
def test_reader_drop_refuses_a_name_that_is_not_a_plain_identifier(reader, role):
    cursor = RecordingCursor()
    with pytest.raises(ValueError, match="not a plain lower-case identifier"):
        reader.drop_reader(RecordingConn(cursor), role)
    assert cursor.executed == []


# --- lambda-db-init ------------------------------------------------------------


@pytest.fixture(scope="module")
def db_init():
    return _load("lambda-db-init/handler.py")


def test_db_init_sets_each_database_timezone(db_init):
    cursor = RecordingCursor({"SELECT 1 FROM pg_database": (1,)})
    assert db_init._alter_databases_to_utc(cursor, "erp_db", "app_db") == ["erp_db", "app_db"]
    assert cursor.executed == [
        ("ALTER DATABASE \"erp_db\" SET timezone = 'UTC'", None),
        ("SELECT 1 FROM pg_database WHERE datname = %s", ("app_db",)),
        ("ALTER DATABASE \"app_db\" SET timezone = 'UTC'", None),
    ]


def test_db_init_skips_a_missing_app_database(db_init):
    cursor = RecordingCursor({"SELECT 1 FROM pg_database": None})
    assert db_init._alter_databases_to_utc(cursor, "erp_db", "app_db") == ["erp_db"]
    assert [text for text, _ in cursor.executed if text.startswith("ALTER")] == [
        "ALTER DATABASE \"erp_db\" SET timezone = 'UTC'"]


def test_db_init_drops_the_four_schemas_in_order(db_init, monkeypatch):
    monkeypatch.setenv("DB_NAME", "erp_db")
    cursor = RecordingCursor()
    db_init.drop_all_tables(RecordingConn(cursor))
    assert texts(cursor, "DROP SCHEMA") == [
        (f'DROP SCHEMA IF EXISTS "{name}" CASCADE', None)
        for name in ("erp_data", "mes_milwaukee", "mes_detroit", "mes_cedarrapids")
    ]


def test_db_init_creates_the_schema_from_one_script(db_init):
    cursor = RecordingCursor()
    db_init.create_schema(RecordingConn(cursor))
    assert cursor.executed == [(
        db_init.get_schema_sql() + db_init.get_schema_sql_part2() + db_init.get_schema_sql_part3()
        + db_init.get_schema_sql_part4() + db_init.get_schema_sql_part5() + db_init.HOT_PATH_INDEXES_SQL,
        None)]


def test_db_init_adds_each_site_event_column(db_init):
    cursor = RecordingCursor()
    db_init._ensure_site_event_columns(cursor)
    assert cursor.executed == [
        (f'ALTER TABLE simulation_events ADD COLUMN IF NOT EXISTS "{column}" INTEGER DEFAULT 0', None)
        for column in db_init.SITE_EVENT_COLUMNS
    ]


# --- lambda_/lambda_cleanup_old_data.py ----------------------------------------


@pytest.fixture(scope="module")
def cleanup():
    return _load("lambda_/lambda_cleanup_old_data.py")


def test_cleanup_counts_every_listed_table(cleanup):
    cursor = RecordingCursor()
    counts = cleanup._table_counts(cursor)
    assert list(counts) == list(cleanup._TABLES_TO_CHECK)
    assert cursor.executed == [(f'SELECT COUNT(*) FROM "{table}"', None) for table in cleanup._TABLES_TO_CHECK]


def test_cleanup_verify_counts_the_key_tables(cleanup, monkeypatch):
    cursor = RecordingCursor()
    monkeypatch.setattr(cleanup, "get_db_credentials", lambda: {
        "host": "h", "port": 5432, "database": "d", "user": "u", "password": "p"})
    monkeypatch.setattr(cleanup.pgdriver, "connect", lambda **kwargs: RecordingConn(cursor))
    cleanup.verify_data()
    assert [text for text, _ in texts(cursor, "SELECT COUNT(*) FROM ")][:11] == [
        f'SELECT COUNT(*) FROM "{table}"' for table in (
            "customers", "suppliers", "products", "inventory_items", "inventory_locations",
            "bill_of_materials", "inventory_balances", "customer_orders", "production_runs",
            "shipments", "invoices")]


# --- lambda_/lambda_seed_data.py -----------------------------------------------


@pytest.fixture(scope="module")
def seed():
    return _load("lambda_/lambda_seed_data.py")


def test_seed_sets_each_database_timezone(seed, monkeypatch):
    from erp_simulation import pgdriver

    cursor = RecordingCursor({"SELECT 1 FROM pg_database": (1,)})
    monkeypatch.setattr(pgdriver, "connect", lambda **kwargs: RecordingConn(cursor))
    creds = {"host": "h", "username": "u", "password": "p"}
    assert seed._set_databases_utc(creds, "erp_db", "app_db") == ["erp_db", "app_db"]
    assert cursor.executed == [
        ("ALTER DATABASE \"erp_db\" SET timezone = 'UTC'", None),
        ("SELECT 1 FROM pg_database WHERE datname = %s", ("app_db",)),
        ("ALTER DATABASE \"app_db\" SET timezone = 'UTC'", None),
    ]


def test_seed_skips_a_missing_app_database(seed):
    cursor = RecordingCursor({"SELECT 1 FROM pg_database": None})
    done = ["erp_db"]
    seed._set_app_db_utc(cursor, "app_db", done)
    assert done == ["erp_db"]
    assert [text for text, _ in cursor.executed if text.startswith("ALTER")] == []
