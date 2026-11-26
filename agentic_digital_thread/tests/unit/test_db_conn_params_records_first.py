# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 2.1 (plan sa.plan-demo-agnostic-engine-plan-v2) — records-first
``db._get_conn_params_for_schema``.

Pins ADR-0044 Decision 1 on the hot ``query_pg`` path:

* (a) a REGISTERED schema with a configured node gets its params from the
  persisted record — a poisoned env proves the env is not consulted;
* (b) an UNREGISTERED schema behaves byte-identically to the legacy env-only
  routing (ERP-family → ERP env, everything else → ``get_conn_params``),
  with the routing decision now delegated to ``agents.demo.db_routing``
  (the local ``erp_schemas`` tuple in db.py is deleted);
* (c) ``resolve_connection`` raising / returning ``_unconfigured`` falls back
  to env, without raising;
* (d) the ~60s TTL cache: a second call within the TTL does not re-invoke
  ``resolve_connection``; expiry re-resolves;
* (e) NO recursion between ``connection_resolver.pg_conn_params``'s
  env-credential bootstrap and the records-first read (plan risk R3) — the
  thread-local guard serves the env-only variant on re-entry;
* fail-closed: the no-password ``RuntimeError`` semantics are preserved on
  every env path (never a well-known default).
"""

import pytest

import agents.db as db
from agents.catalog import SystemCatalog
from agents.errors import MissingDbPasswordError


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh cache, empty catalog, clean env, and NO SSM fallback per test."""
    db._SCHEMA_CONN_CACHE.clear()
    db._RECORDS_FIRST_GUARD.active = False
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "catalog", SystemCatalog([]))
    for var in ("ERP_POSTGRES_HOST", "ERP_POSTGRES_PORT", "ERP_POSTGRES_DB",
                "ERP_POSTGRES_USER", "ERP_POSTGRES_PASSWORD",
                "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
                "POSTGRES_USER", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    # Offline: the demo SSM pointer read must never leave the process.
    import agents.demo.db_routing as db_routing
    monkeypatch.setattr(db_routing, "_demo_db_pointer", lambda kind: {})
    yield
    db._SCHEMA_CONN_CACHE.clear()
    db._RECORDS_FIRST_GUARD.active = False


def _register(monkeypatch, system_id="cmms", schema="cmms_maintenance",
              status="onboarded"):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(
        catalog_mod, "catalog",
        SystemCatalog([{"id": system_id, "schema": schema, "status": status}]),
    )


_CONFIGURED = {
    "system_id": "cmms", "connection_type": "PostgreSQL",
    "host": "record-host.example.com", "port": 5432, "dbname": "record_db",
    "schema": "cmms_maintenance", "user": "record_user",
    "password": "record-pw", "secret_arn": "arn:aws:secretsmanager:x",
    "_unconfigured": False, "_secret_unresolved": False,
}


# --------------------------------------------------------------------------- #
# (a) records-first: registered schema + configured node → record wins
# --------------------------------------------------------------------------- #

def test_registered_schema_uses_record_env_not_consulted(monkeypatch):
    _register(monkeypatch)
    # Poison env: if any env host leaked into the result, this would show.
    monkeypatch.setenv("POSTGRES_HOST", "poison-adt-host")
    monkeypatch.setenv("POSTGRES_PASSWORD", "poison-pw")
    monkeypatch.setenv("ERP_POSTGRES_HOST", "poison-erp-host")
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: dict(_CONFIGURED))

    params = db._get_conn_params_for_schema("cmms_maintenance")

    assert params["host"] == "record-host.example.com"
    assert params["dbname"] == "record_db"
    assert params["user"] == "record_user"
    assert params["password"] == "record-pw"
    assert "poison" not in str(params)


def test_registered_schema_resolves_by_system_id(monkeypatch):
    _register(monkeypatch, system_id="plm", schema="plm_engineering")
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    seen = []
    import agents.connection_resolver as cr

    def _resolve(sid):
        seen.append(sid)
        cfg = dict(_CONFIGURED)
        cfg["system_id"] = sid
        return cfg

    monkeypatch.setattr(cr, "resolve_connection", _resolve)
    db._get_conn_params_for_schema("plm_engineering")
    assert seen == ["plm"]


# --------------------------------------------------------------------------- #
# (b) unregistered schema → byte-identical legacy env routing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("schema", ["erp_data", "mes_milwaukee", "mes_detroit",
                                    "mes_cedarrapids", "public"])
def test_unregistered_erp_family_routes_to_erp_env(monkeypatch, schema):
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    monkeypatch.setenv("ERP_POSTGRES_PASSWORD", "erp-pw")
    params = db._get_conn_params_for_schema(schema)
    assert params == {
        "host": "erp.example.com", "port": "5432",
        "dbname": "erp_simulation", "user": "postgres", "password": "erp-pw",
    }


@pytest.mark.parametrize("schema", ["cmms_maintenance", "plm_engineering",
                                    "adt_demo"])
def test_unregistered_adt_seed_schemas_route_to_adt_env(monkeypatch, schema):
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    params = db._get_conn_params_for_schema(schema)
    assert params == {
        "host": "adt.example.com", "port": "5432",
        "dbname": "adt_demo", "user": "postgres", "password": "adt-pw",
    }


def test_erp_schema_without_erp_host_falls_to_adt_env(monkeypatch):
    """Legacy parity: ERP-family schema with NO ERP host set uses get_conn_params."""
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    params = db._get_conn_params_for_schema("erp_data")
    assert params["host"] == "adt.example.com"
    assert params["password"] == "adt-pw"


def test_erp_password_falls_back_to_shared_postgres_password(monkeypatch):
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "shared-pw")
    params = db._get_conn_params_for_schema("erp_data")
    assert params["password"] == "shared-pw"


def test_local_routing_tuple_is_deleted():
    """The db.py copy of the ERP-prefix routing tuple is gone (owner directive);
    the routing decision is delegated to agents.demo.db_routing."""
    import inspect
    src = inspect.getsource(db)
    assert "erp_schemas" not in src
    assert "demo_conn_params_for_schema" in src


# --------------------------------------------------------------------------- #
# fail-closed no-password RuntimeError semantics (preserved on every env path)
# --------------------------------------------------------------------------- #

def test_erp_env_without_any_password_raises(monkeypatch):
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    with pytest.raises(RuntimeError, match="ERP_POSTGRES_PASSWORD"):
        db._get_conn_params_for_schema("erp_data")


def test_adt_env_without_password_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        db._get_conn_params_for_schema("cmms_maintenance")


def test_env_only_variant_is_fail_closed_too(monkeypatch):
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    with pytest.raises(RuntimeError):
        db._env_conn_params_for_schema("erp_data")
    with pytest.raises(RuntimeError):
        db._env_conn_params_for_schema("adt_demo")


# --------------------------------------------------------------------------- #
# (c) resolve failure / unconfigured node → env fallback, no raise
# --------------------------------------------------------------------------- #

def test_unconfigured_node_falls_back_to_env(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": None, "connection_string": "",
        "secret_arn": "", "_unconfigured": True, "_secret_unresolved": False})
    params = db._get_conn_params_for_schema("cmms_maintenance")
    assert params["host"] == "adt.example.com"


def test_resolve_connection_raising_falls_back_to_env(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    import agents.connection_resolver as cr

    def _boom(sid):
        raise ValueError("graph unavailable")

    monkeypatch.setattr(cr, "resolve_connection", _boom)
    params = db._get_conn_params_for_schema("cmms_maintenance")
    assert params["host"] == "adt.example.com"


def test_catalog_read_error_falls_back_to_env(monkeypatch):
    import agents.catalog as catalog_mod

    class _Broken:
        def get_all(self):
            raise ValueError("catalog unavailable")

    monkeypatch.setattr(catalog_mod, "catalog", _Broken())
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    params = db._get_conn_params_for_schema("cmms_maintenance")
    assert params["host"] == "adt.example.com"


# --------------------------------------------------------------------------- #
# (d) TTL cache on the hot query_pg path
# --------------------------------------------------------------------------- #

class _FakeTime:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


def test_ttl_cache_second_call_does_not_reresolve(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    fake = _FakeTime()
    monkeypatch.setattr(db, "time", fake)
    calls = []
    import agents.connection_resolver as cr

    def _resolve(sid):
        calls.append(sid)
        return dict(_CONFIGURED)

    monkeypatch.setattr(cr, "resolve_connection", _resolve)

    p1 = db._get_conn_params_for_schema("cmms_maintenance")
    fake.now += 30  # within TTL
    p2 = db._get_conn_params_for_schema("cmms_maintenance")
    assert len(calls) == 1, calls
    assert p1 == p2
    # Cached copies are independent dicts (a caller mutating one must not
    # poison the cache).
    p1["host"] = "mutated"
    assert db._get_conn_params_for_schema("cmms_maintenance")["host"] == \
        "record-host.example.com"


def test_ttl_cache_expiry_reresolves(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    fake = _FakeTime()
    monkeypatch.setattr(db, "time", fake)
    calls = []
    import agents.connection_resolver as cr

    def _resolve(sid):
        calls.append(sid)
        return dict(_CONFIGURED)

    monkeypatch.setattr(cr, "resolve_connection", _resolve)

    db._get_conn_params_for_schema("cmms_maintenance")
    fake.now += db._SCHEMA_CONN_CACHE_TTL + 1
    db._get_conn_params_for_schema("cmms_maintenance")
    assert len(calls) == 2, calls


def test_negative_cache_skips_graph_but_env_stays_live(monkeypatch):
    """A records miss is cached as a sentinel (no graph read per hot query),
    but the env fallback itself is a LIVE read — an env change within the TTL
    is honored (byte-identical env semantics, never a stale snapshot)."""
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "adt-a.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    fake = _FakeTime()
    monkeypatch.setattr(db, "time", fake)
    calls = []
    import agents.connection_resolver as cr

    def _resolve(sid):
        calls.append(sid)
        return {"system_id": sid, "secret_arn": "", "_unconfigured": True}

    monkeypatch.setattr(cr, "resolve_connection", _resolve)

    assert db._get_conn_params_for_schema("cmms_maintenance")["host"] == "adt-a.example.com"
    monkeypatch.setenv("POSTGRES_HOST", "adt-b.example.com")
    fake.now += 30  # within TTL: no re-resolve, but the env read is live
    assert db._get_conn_params_for_schema("cmms_maintenance")["host"] == "adt-b.example.com"
    assert len(calls) == 1, calls


# --------------------------------------------------------------------------- #
# (e) recursion guard — pg_conn_params ↔ _get_conn_params_for_schema (risk R3)
# --------------------------------------------------------------------------- #

def test_pg_conn_params_empty_resolved_does_not_recurse(monkeypatch):
    """The plan's canonical probe: ``pg_conn_params({})`` with only env set.
    The env-credential bootstrap re-enters db; an unregistered ''-schema falls
    straight to the env path — and must terminate."""
    from agents.connection_resolver import pg_conn_params
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    params = pg_conn_params({})
    assert params["host"] == "adt.example.com"
    assert params["password"] == "adt-pw"


def test_records_first_credential_bootstrap_does_not_recurse(monkeypatch):
    """The hard case: a REGISTERED schema whose resolved record has no
    password. records-first invokes the REAL pg_conn_params, whose env
    bootstrap calls back into db._get_conn_params_for_schema for the SAME
    schema — without the thread-local guard this recurses (risk R3). The
    guard must serve the env-only variant: host from the record, credential
    from the env. (The env credentials fill only the env
    target, so the record names the env's own host + database here — a demo
    record built through demo_pg_dsn always does; a foreign record host is
    refused, test_records_first_never_env_fills_a_foreign_host.)"""
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "record-host.example.com")
    monkeypatch.setenv("POSTGRES_DB", "record_db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "PostgreSQL",
        "host": "record-host.example.com", "port": 5433, "dbname": "record_db",
        "schema": "cmms_maintenance", "secret_arn": "",
        "_unconfigured": False, "_secret_unresolved": False})

    params = db._get_conn_params_for_schema("cmms_maintenance")

    # Non-secret target from the record; credential bootstrapped from env.
    assert params["host"] == "record-host.example.com"
    assert params["port"] == 5433
    assert params["dbname"] == "record_db"
    assert params["user"] == "postgres"
    assert params["password"] == "env-pw"
    assert params["options"] == "-csearch_path=cmms_maintenance"
    # Guard is released afterwards.
    assert not getattr(db._RECORDS_FIRST_GUARD, "active", False)


def test_records_first_never_env_fills_a_foreign_host(monkeypatch):
    """A record whose host differs from the env's own database host is
    refused — the deployment's password never goes to another server."""
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "env-adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    import agents.connection_resolver as cr
    from agents.errors import MissingDbPasswordError
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "PostgreSQL",
        "host": "record-host.example.com", "port": 5432, "dbname": "adt_demo",
        "schema": "cmms_maintenance", "secret_arn": "",
        "_unconfigured": False, "_secret_unresolved": False})
    with pytest.raises(MissingDbPasswordError):
        db._get_conn_params_for_schema("cmms_maintenance")
    assert not getattr(db._RECORDS_FIRST_GUARD, "active", False)


def test_guard_released_even_when_bootstrap_raises(monkeypatch):
    """Fail-closed RuntimeError inside the re-entrant bootstrap (no password
    anywhere) propagates AND releases the guard for subsequent calls."""
    _register(monkeypatch)
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "PostgreSQL",
        "host": "record-host.example.com", "schema": "cmms_maintenance",
        "secret_arn": "", "_unconfigured": False, "_secret_unresolved": False})
    monkeypatch.setenv("POSTGRES_HOST", "record-host.example.com")   # the env's own host
    with pytest.raises(RuntimeError):
        db._get_conn_params_for_schema("cmms_maintenance")
    assert not getattr(db._RECORDS_FIRST_GUARD, "active", False)
    # And a later env-satisfied call on the same thread still works.
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    db._SCHEMA_CONN_CACHE.clear()
    params = db._get_conn_params_for_schema("cmms_maintenance")
    assert params["password"] == "env-pw"


def test_env_only_variant_never_reads_records(monkeypatch):
    """_env_conn_params_for_schema must not consult the catalog or the graph —
    it is the recursion-safe seam Task 2.3 repoints connection_resolver onto."""
    import agents.catalog as catalog_mod

    class _MustNotRead:
        def get_all(self):
            raise AssertionError("_env_conn_params_for_schema read the catalog")

    monkeypatch.setattr(catalog_mod, "catalog", _MustNotRead())
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    params = db._env_conn_params_for_schema("cmms_maintenance")
    assert params["password"] == "env-pw"


# --------------------------------------------------------------------------- #
# X3 — the refusal is a DEDICATED error; unrelated RuntimeErrors stay fail-soft
# --------------------------------------------------------------------------- #

def test_no_password_refusal_is_dedicated_error(monkeypatch):
    """Both env refusal sites raise MissingDbPasswordError — a RuntimeError
    subclass, so every existing ``except RuntimeError`` /
    ``pytest.raises(RuntimeError)`` pin is unchanged."""
    assert issubclass(MissingDbPasswordError, RuntimeError)
    with pytest.raises(MissingDbPasswordError, match="POSTGRES_PASSWORD"):
        db.get_conn_params()
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    with pytest.raises(MissingDbPasswordError, match="ERP_POSTGRES_PASSWORD"):
        db._get_conn_params_for_schema("erp_data")


def test_unrelated_runtime_error_in_records_path_falls_soft_to_env(monkeypatch):
    """A plain RuntimeError from the resolver (e.g. a transient graph failure)
    is NOT the fail-closed refusal — the records path falls soft to env instead
    of propagating (the old blanket ``except RuntimeError: raise`` re-raised it)."""
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "adt-pw")
    import agents.connection_resolver as cr

    def _transient(sid):
        raise RuntimeError("graph transiently unavailable")

    monkeypatch.setattr(cr, "resolve_connection", _transient)
    params = db._get_conn_params_for_schema("cmms_maintenance")
    assert params["host"] == "adt.example.com"


def test_missing_password_refusal_still_propagates_from_records_path(monkeypatch):
    """The deliberate refusal (MissingDbPasswordError) raised inside the
    records-first credential bootstrap still propagates — never swallowed."""
    _register(monkeypatch)
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "PostgreSQL",
        "host": "record-host.example.com", "schema": "cmms_maintenance",
        "secret_arn": "", "_unconfigured": False, "_secret_unresolved": False})
    with pytest.raises(MissingDbPasswordError):
        db._get_conn_params_for_schema("cmms_maintenance")


# --------------------------------------------------------------------------- #
# X4 — invalidate_schema_conn_cache (called best-effort on connection update)
# --------------------------------------------------------------------------- #

def _spy_resolver(monkeypatch, calls):
    import agents.connection_resolver as cr

    def _resolve(sid):
        calls.append(sid)
        return dict(_CONFIGURED)

    monkeypatch.setattr(cr, "resolve_connection", _resolve)


def test_invalidate_specific_schema_forces_reresolve(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    calls = []
    _spy_resolver(monkeypatch, calls)

    db._get_conn_params_for_schema("cmms_maintenance")
    db._get_conn_params_for_schema("cmms_maintenance")
    assert len(calls) == 1  # served from cache

    db.invalidate_schema_conn_cache("cmms_maintenance")
    db._get_conn_params_for_schema("cmms_maintenance")
    assert len(calls) == 2  # cache entry dropped → re-resolved


def test_invalidate_specific_schema_leaves_others_cached(monkeypatch):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "catalog", SystemCatalog([
        {"id": "cmms", "schema": "cmms_maintenance", "status": "onboarded"},
        {"id": "plm", "schema": "plm_engineering", "status": "onboarded"},
    ]))
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    calls = []
    _spy_resolver(monkeypatch, calls)

    db._get_conn_params_for_schema("cmms_maintenance")
    db._get_conn_params_for_schema("plm_engineering")
    db.invalidate_schema_conn_cache("cmms_maintenance")
    db._get_conn_params_for_schema("plm_engineering")   # still cached
    db._get_conn_params_for_schema("cmms_maintenance")  # re-resolved
    assert calls == ["cmms", "plm", "cmms"]


def test_invalidate_all_clears_every_entry(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-pw")
    calls = []
    _spy_resolver(monkeypatch, calls)

    db._get_conn_params_for_schema("cmms_maintenance")
    db.invalidate_schema_conn_cache()  # schema=None → clear all
    assert db._SCHEMA_CONN_CACHE == {}
    db._get_conn_params_for_schema("cmms_maintenance")
    assert len(calls) == 2


def test_invalidate_unknown_schema_is_a_noop():
    db.invalidate_schema_conn_cache("never_cached")  # must not raise
