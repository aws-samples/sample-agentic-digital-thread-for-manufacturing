# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.onboard_agent.discover`` connection acquisition.

Proves the ``discover`` Strands ``@tool`` (and its ``_discover_postgres``
backend) acquires its CONNECTION via ``connection_resolver.resolve_connection``
— the System node when present, else the live-env/catalog-snapshot path the
resolver fails-soft to — and NOT by reading a persisted catalog connection
snapshot (``entry.get('connection_string')``) directly. No live AWS, Postgres,
HTTP, or Neptune is touched: ``resolve_connection`` is mocked and
``agents.pgdriver.connect`` is replaced with an in-memory fake.

Runnable two ways (matches ``test_onboard_agent_test_connection.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_agent_discover_resolve.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_agent_discover_resolve.py
"""
import sys
from pathlib import Path

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.onboarding_session import SessionNotFoundError, SessionExistsError  # noqa: E402


class _FakeStore:
    """In-memory stand-in for OnboardingSessionStore (no AWS).

    Faithfully mirrors the store's create->advance state machine so discover's
    new persistence path (create_session 'discovering' -> advance 'discovered')
    runs offline. ``discover`` is the only path exercised here, so only the
    create/advance/get methods are needed."""

    def __init__(self):
        self.items = {}

    def get_session(self, system_id):
        if system_id not in self.items:
            raise SessionNotFoundError(system_id)
        return dict(self.items[system_id])

    def create_session(self, system_id, initiated_by, **fields):
        if system_id in self.items:
            raise SessionExistsError(system_id)
        item = {"system_id": system_id, "status": "discovering", "initiated_by": initiated_by}
        item.update(fields)
        self.items[system_id] = item
        return dict(item)

    def advance_status(self, system_id, from_status, to_status, **fields):
        item = self.items[system_id]
        assert item["status"] == from_status, (item["status"], from_status)
        item["status"] = to_status
        item.update(fields)


def _install_fake_store():
    """Point oa._store at a fresh _FakeStore; return a restore callable."""
    orig = oa._store
    fake = _FakeStore()
    oa._store = lambda: fake
    def _restore():
        oa._store = orig
    return fake, _restore


# A DSN that ONLY the resolver returns — if discover read the catalog snapshot
# instead, this value would never reach pgdriver.connect.
_RESOLVER_DSN = "postgresql://from-resolver/db?options=-csearch_path%3Derp_data"
# A DSN deliberately planted on the catalog entry; discover must NOT use it.
_CATALOG_SNAPSHOT_DSN = "postgresql://STALE-catalog-snapshot/db?options=-csearch_path%3Derp_data"


class _FakeCursor:
    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return []  # no tables — keeps the discover loop trivial

    def fetchone(self):
        return {"cnt": 0}

    def close(self):
        return None


class _FakeConn:
    def cursor(self, *a, **k):
        return _FakeCursor()

    def close(self):
        return None


def _install_fake_pgdriver_connect(captured: dict):
    """Replace ``agents.pgdriver.connect`` with an in-memory fake.

    Returns a restore callable. The fake ``connect`` records the DSN it is given
    so the test can assert WHICH connection string discover used.
    """
    from agents import pgdriver

    saved = pgdriver.connect

    def _connect(conn_str, *a, **k):
        captured["conn_str"] = conn_str
        return _FakeConn()

    pgdriver.connect = _connect

    def _restore():
        pgdriver.connect = saved

    return _restore


def test_postgres_discover_connects_with_resolver_dsn_not_catalog_snapshot():
    """discover must connect with the resolver's DSN, never the catalog snapshot."""
    captured = {}
    calls = []

    def _resolve(system_id):
        calls.append(system_id)
        return {"system_id": system_id, "connection_type": "PostgreSQL",
                "connection_string": _RESOLVER_DSN, "secret_arn": ""}

    restore_pgdriver = _install_fake_pgdriver_connect(captured)
    fake_store, restore_store = _install_fake_store()
    orig_get, orig_resolve = oa.catalog.get, cr.resolve_connection
    # The catalog entry carries menu identity AND a STALE connection snapshot that
    # discover must ignore in favor of resolve_connection's value.
    oa.catalog.get = lambda sid: {
        "id": sid, "name": "ERP", "connection_type": "PostgreSQL",
        "schema": "erp_data", "connection_string": _CATALOG_SNAPSHOT_DSN,
    }
    cr.resolve_connection = _resolve
    try:
        res = oa.discover("erp")
        # Connection was acquired via resolve_connection (called with the id)...
        assert calls == ["erp"], calls
        # ...and the DSN actually handed to pgdriver came from the resolver.
        assert captured.get("conn_str") == _RESOLVER_DSN, captured
        assert captured.get("conn_str") != _CATALOG_SNAPSHOT_DSN, captured
        # And discover completed (no error path).
        assert res.get("system_id") == "erp", res
        assert res.get("connection_type") == "PostgreSQL", res
        # Discover persisted to the store and advanced discovering->discovered.
        assert fake_store.items["erp"]["status"] == "discovered", fake_store.items
    finally:
        oa.catalog.get, cr.resolve_connection = orig_get, orig_resolve
        restore_pgdriver()
        restore_store()


def test_postgres_discover_works_when_catalog_has_no_connection_snapshot():
    """Bootstrap: a catalog entry with NO connection_string still discovers,
    because the connection comes from resolve_connection (node/live-env)."""
    captured = {}

    def _resolve(system_id):
        return {"system_id": system_id, "connection_type": "PostgreSQL",
                "connection_string": _RESOLVER_DSN, "secret_arn": ""}

    restore_pgdriver = _install_fake_pgdriver_connect(captured)
    fake_store, restore_store = _install_fake_store()
    orig_get, orig_resolve = oa.catalog.get, cr.resolve_connection
    # Identity-only entry: NO connection_string at all (the not-yet-onboarded case).
    oa.catalog.get = lambda sid: {"id": sid, "name": "ERP",
                                  "connection_type": "PostgreSQL", "schema": "erp_data"}
    cr.resolve_connection = _resolve
    try:
        res = oa.discover("erp")
        assert captured.get("conn_str") == _RESOLVER_DSN, captured
        assert "error" not in res, res
    finally:
        oa.catalog.get, cr.resolve_connection = orig_get, orig_resolve
        restore_pgdriver()
        restore_store()


def test_discover_invokes_resolve_connection_for_unknown_branch_after_lookup():
    """resolve_connection is the connection source; an unknown system never reaches it."""
    calls = []

    def _resolve(system_id):
        calls.append(system_id)
        return {"system_id": system_id, "connection_type": "PostgreSQL",
                "connection_string": "", "secret_arn": ""}

    orig_get, orig_resolve = oa.catalog.get, cr.resolve_connection
    oa.catalog.get = lambda sid: None  # unknown system
    cr.resolve_connection = _resolve
    try:
        res = oa.discover("nope")
        assert res == {"error": "System nope not found"}, res
        assert calls == [], "resolve_connection must not run for unknown systems"
    finally:
        oa.catalog.get, cr.resolve_connection = orig_get, orig_resolve


_TESTS = [
    test_postgres_discover_connects_with_resolver_dsn_not_catalog_snapshot,
    test_postgres_discover_works_when_catalog_has_no_connection_snapshot,
    test_discover_invokes_resolve_connection_for_unknown_branch_after_lookup,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} onboard_agent.discover resolve_connection tests passed")


def test_structured_secret_dbname_key_reaches_the_dsn(monkeypatch):
    """Measured live (ADR-0069 follow-up): CDK-generated RDS secrets carry the
    database under ``dbname`` (and the user under ``username``); the tool path
    hand-built its DSN reading only ``database`` → empty → libpq defaulted the
    database to the USER name → 'database "erp_admin" does not exist'. The
    wizard path never hit this because it goes through the ONE resolver
    (``connection_resolver.pg_conn_params``), which accepts both aliases. The
    tool path now builds through the same resolver."""
    import agents.onboard_agent as oa
    secret = {"host": "db.internal", "port": 5432, "dbname": "widgets_db",
              "username": "widgets_admin", "password": "pw"}
    monkeypatch.setattr(oa, "_fetch_credentials", lambda arn: dict(secret))
    conn_str, schema = oa._discover_postgres_dsn(
        {"schema": "erp_data"}, {"secret_arn": "arn:aws:secretsmanager:x:y:secret:z"})
    assert schema == "erp_data"
    assert "/widgets_db" in conn_str, conn_str
    assert "widgets_admin" in conn_str and "pw" in conn_str
    assert "search_path%3Derp_data" in conn_str or "search_path=erp_data" in conn_str
