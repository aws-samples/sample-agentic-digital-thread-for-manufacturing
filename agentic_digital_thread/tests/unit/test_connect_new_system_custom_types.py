# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System for PostgreSQL / REST API / MCP Server, end to end in
code (offline — no AWS, no Neptune, no PostgreSQL, no network).

The bug: the UI never registered these three types (it minted a client-side
``custom-<timestamp>`` id), so the wizard's first step — preflight — answered
404 "Unknown system_id". Registering them through ``POST /api/onboard/connect``
is necessary but not sufficient:

* preflight resolves connections NODE-ONLY, and a fresh registration has no
  graph node yet, so even the typed connectors that DID register came back
  ``blocked`` (the probe fell back to PostgreSQL with no connection string);
* PostgreSQL provisioning built the node DSN, and the system agent's connect
  params, from the DEMO routing (``demo_pg_dsn`` + the demo DB secret /
  ``POSTGRES_*`` env) — never from the connection string the user entered.

These tests pin the fixed contract. The PostgreSQL connection string carries
the password, so it travels in ``connection_config.dsn`` and lands ONLY in the
``adt/conn/<id>`` secret: never on the catalog entry, the graph node, the
response, or a log line. Every demo/workshop system (registered by the demo
hook, never through the HTTP route) keeps its exact path.
"""
from __future__ import annotations

import inspect
import json
import logging
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_mod  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from agents.models import ConnectRequest, DiscoverRequest, ProvisionRequest  # noqa: E402

PASSWORD = "s3cr3t-pw"  # nosec B105 - synthetic test credential
DSN = (f"postgresql://lab_reader:{PASSWORD}@db.example.internal:5432/lims"
       "?options=-csearch_path%3Dlims")
SID = "quality-lab"
CONN_ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{SID}-AbCdEf"
DEMO_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt-db-credentials-XyZ123"


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Registration and discover validate their targets (a DNS lookup); offline,
    every target 'resolves' to a documentation address."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(cr, "validate_connection_target",
                        lambda target, *a, **kw: "203.0.113.10")


@pytest.fixture
def cat(monkeypatch):
    """A fresh, empty catalog wired into every module that reads it."""
    fresh = SystemCatalog([])
    for mod in (catalog_mod, onboard, onboard_agent):
        monkeypatch.setattr(mod, "catalog", fresh)
    db.invalidate_schema_conn_cache()
    yield fresh
    db.invalidate_schema_conn_cache()


@pytest.fixture
def secrets(monkeypatch):
    """In-memory Secrets Manager: name -> value (ARN keyed by the conn name)."""
    store: dict[str, dict] = {}

    def _store(system_id, secret):
        arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{system_id}-AbCdEf"
        store[arn] = dict(secret)
        return arn

    def _fetch(arn):
        if arn not in store:
            raise RuntimeError("ResourceNotFoundException")
        return dict(store[arn])

    monkeypatch.setattr(cr, "_store_connection_secret", _store)
    monkeypatch.setattr(cr, "_fetch_secret", _fetch)
    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    # No graph node exists for a fresh registration.
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    return store


def _register(body: dict) -> dict:
    """POST /api/onboard/connect through its sync body (the route offloads it)."""
    return onboard.onboard_connect(ConnectRequest(**body))


def _register_pg():
    return _register({"name": "Quality Lab", "system_type": "Custom",
                      "connection_type": "PostgreSQL", "schema": "lims",
                      "connection_config": {"dsn": DSN}})


# ── (b) /api/onboard/connect registers all three types ──────────────────────

def test_connect_route_offloads_its_sync_body():
    """The route does Secrets Manager I/O, so it follows the event-loop offload
    pattern: a sync ``onboard_connect`` plus an async ``onboard_connect_route``."""
    assert not inspect.iscoroutinefunction(onboard.onboard_connect)
    assert inspect.iscoroutinefunction(onboard.onboard_connect_route)
    src = inspect.getsource(onboard.onboard_connect_route)
    assert "offload(onboard_connect" in src


def test_postgres_dsn_goes_to_secrets_manager_never_the_catalog(cat, secrets, caplog):
    caplog.set_level(logging.DEBUG)
    res = _register_pg()
    assert res["status"] == "created" and res["system_id"] == SID, res
    assert PASSWORD not in json.dumps(res)
    entry = cat.get(SID)
    assert entry is not None, "registration must create the catalog entry preflight looks up"
    assert entry["connection_type"] == "PostgreSQL" and entry["schema"] == "lims"
    assert entry["secret_arn"] == CONN_ARN
    assert entry["connection_config"] == {"host": "db.example.internal", "port": "5432",
                                          "dbname": "lims"}
    assert PASSWORD not in json.dumps(entry), entry
    assert "dsn" not in json.dumps(entry)
    assert secrets[CONN_ARN] == {"connection_string": DSN, "user": "lab_reader",
                                 "password": PASSWORD, "connection_origin": "user",
                                 "system_id": SID}
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("dsn, why", [
    ("mysql://u:p@db.example.internal/x", "scheme"),
    ("host=db.example.internal dbname=lims user=u password=p", "URI"),
    ("postgresql://db.example.internal:5432/lims?options=-csearch_path%3Dlims", "password"),
])
def test_postgres_dsn_rejected_without_registering(cat, secrets, dsn, why):
    res = _register({"name": "Quality Lab", "connection_type": "PostgreSQL",
                     "schema": "lims", "connection_config": {"dsn": dsn}})
    assert "error" in res, (why, res)
    assert cat.get(SID) is None
    assert secrets == {}


def test_postgres_secret_failure_fails_the_registration(cat, secrets, monkeypatch):
    def _boom(system_id, secret):
        raise RuntimeError(f"denied {secret}")  # a message that WOULD leak if echoed
    monkeypatch.setattr(cr, "_store_connection_secret", _boom)
    res = _register_pg()
    assert "error" in res and PASSWORD not in json.dumps(res), res
    assert cat.get(SID) is None


@pytest.mark.parametrize("ctype, url", [
    ("MCP Server", "https://mcp.example.com/mcp"),
])
def test_url_types_register_and_preflight_probes_the_registered_url(cat, secrets,
                                                                      monkeypatch, ctype, url):
    res = _register({"name": "Quality Lab", "system_type": "Custom",
                     "connection_type": ctype, "connection_string": url})
    assert res["status"] == "created", res
    seen = {}
    monkeypatch.setattr(op, "_probe", lambda sid, resolved: seen.update(resolved) or
                        {"status": "fail", "reason": "unreachable", "detail": "x"})
    op.run_preflight(SID)
    assert seen.get("connection_type") == ctype, seen
    assert seen.get("connection_string") == url, seen


def test_postgres_preflight_probes_the_entered_database(cat, secrets, monkeypatch):
    _register_pg()
    seen = {}
    monkeypatch.setattr(op, "_probe", lambda sid, resolved: seen.update(resolved) or
                        {"status": "fail", "reason": "unreachable", "detail": "x"})
    op.run_preflight(SID)
    assert seen.get("connection_type") == "PostgreSQL", seen
    assert seen.get("connection_string") == DSN, seen


def test_typed_connector_fresh_registration_preflight_is_not_probed_as_postgres(
        cat, secrets, monkeypatch):
    """Same root cause, already-registering type: a fresh Athena system was
    probed as PostgreSQL with no connection string (always ``blocked``)."""
    _register({"name": "Quality Lab", "connection_type": "Athena",
               "connection_config": {"workgroup": "wg", "database": "lake",
                                     "role_arn": "arn:aws:iam::111122223333:role/reader"}})
    seen = {}
    monkeypatch.setattr(op, "_probe", lambda sid, resolved: seen.update(resolved) or
                        {"status": "fail", "reason": "unreachable", "detail": "x"})
    op.run_preflight(SID)
    assert seen.get("connection_type") == "Athena", seen
    assert seen.get("workgroup") == "wg" and seen.get("database") == "lake", seen


def test_demo_hook_registration_keeps_the_seed_bootstrap(cat, secrets, monkeypatch):
    """A demo/workshop PG system (registered by the demo hook, not the route)
    still bootstraps the seed DSN exactly as before."""
    onboard_agent.register_demo_system(name="Plant Maintenance", system_type="CMMS",
                                     plant="Site One", connection_type="PostgreSQL",
                                     connection_string="postgresql://u:p@seed/adt_demo",
                                     schema="cmms_site_one", system_id="cmms-site")
    import agents.demo.db_routing as routing
    monkeypatch.setattr(routing, "demo_pg_dsn", lambda schema: f"seed-dsn-for-{schema}")
    out = op._bootstrap_unregistered({"system_id": "cmms-site", "connection_string": "",
                                      "_unconfigured": True}, "cmms-site")
    assert out["connection_string"] == "seed-dsn-for-cmms_site_one"


# ── map/resolve sampling (records-first) and discover use the entered DB ────

def test_records_first_conn_params_use_the_registration(cat, secrets):
    _register_pg()
    params = db._records_conn_params_for_schema("lims")
    assert params is not None
    assert (params["host"], str(params["port"]), params["dbname"]) == \
        ("db.example.internal", "5432", "lims")
    assert (params["user"], params["password"]) == ("lab_reader", PASSWORD)


def test_records_first_demo_entry_still_falls_through_to_env(cat, secrets):
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "registered"})
    assert db._records_conn_params_for_schema("cmms_maintenance") is None


def test_records_first_unreadable_own_secret_never_uses_env_credentials(cat, secrets):
    """If the user's own secret cannot be read, sampling refuses — it must not
    send the deployment's env DB password to the user's host."""
    _register_pg()
    secrets.clear()
    with pytest.raises(db.MissingDbPasswordError):
        db._records_conn_params_for_schema("lims")


def test_discover_postgres_without_body_dsn_connects_to_the_registration(
        cat, secrets, monkeypatch):
    _register_pg()
    seen = []

    def _connect(dsn, *a, **k):
        seen.append(dsn)
        raise RuntimeError("stop after connect")
    monkeypatch.setattr(onboard.pgdriver, "connect", _connect)
    body = DiscoverRequest(system_id=SID, connection_type="PostgreSQL")
    with pytest.raises(RuntimeError, match="stop after connect"):
        import asyncio
        asyncio.run(onboard._discover_postgres_schema(body, SID, "", "lims", "PostgreSQL"))
    assert seen == [DSN]


def test_mcp_discover_of_a_registration_never_uses_the_highbyte_bootstrap(
        cat, secrets, monkeypatch):
    """The deployment's HighByte URL/token must never reach a user's MCP server."""
    _register({"name": "Quality Lab", "connection_type": "MCP Server",
               "connection_string": "https://mcp.example.com/mcp"})
    monkeypatch.setattr(catalog_mod, "_get_highbyte_url", lambda: "https://hb.example.com")
    monkeypatch.setattr(catalog_mod, "_get_highbyte_token", lambda: "hb-token-123")
    entry = onboard._mcp_discover_entry(SID, "")
    assert entry["connection_string"] == "https://mcp.example.com/mcp"
    assert entry["connection_config"]["url"] == "https://mcp.example.com/mcp"
    assert "hb-token-123" not in json.dumps(entry)
    # A demo/catalog MCP system keeps the HighByte bootstrap (sent as Bearer).
    cat.add({"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
             "schema": "", "status": "registered"})
    demo = onboard._mcp_discover_entry("highbyte", "")
    assert demo["connection_config"] == {"url": "https://hb.example.com/mcp",
                                         "token": "hb-token-123",
                                         "auth_scheme": "bearer"}


# ── (c) provisioning a non-demo PostgreSQL system uses the entered connection ─

def _provision(entry: dict, monkeypatch, *, node=None):
    """Run the full ``_do_provision`` with the shared offline harness; capture
    the node write and the agent's connect params."""
    from tests.unit import test_onboard_provision_node_write as h
    started = []
    monkeypatch.setattr(h._FakeAgentManager, "start_agent",
                        lambda self, **kw: started.append(kw) or "mcp://test-agent")
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: node)
    result, calls = h._run_provision(entry)
    return result, calls, started


def _registered_entry(cat, secrets):
    _register_pg()
    entry = dict(cat.get(SID))
    entry["_provision"] = {"system_id": SID, "system_name": "Quality Lab", "plant": "Custom",
                           "schema": "lims", "connection_type": "PostgreSQL"}
    return entry


def test_provision_persists_the_entered_connection_not_the_demo_dsn(cat, secrets, monkeypatch):
    entry = _registered_entry(cat, secrets)
    result, calls, started = _provision(entry, monkeypatch)
    assert result["status"] == "provisioned", result
    node = [c for c in calls if c["system_id"] == SID]
    assert len(node) == 1, calls
    ns = node[0]["non_secret"]
    assert (ns["host"], ns["port"], ns["dbname"], ns["schema"]) == \
        ("db.example.internal", "5432", "lims", "lims")
    assert node[0]["secret_arn"] == CONN_ARN and node[0]["secret"] is None
    assert PASSWORD not in json.dumps(ns) and "lab_reader" not in json.dumps(ns)
    assert started, "the PostgreSQL system agent must start"
    assert started[-1]["conn_params"] == {"host": "db.example.internal", "port": "5432",
                                          "dbname": "lims", "user": "lab_reader",
                                          "password": PASSWORD}


def test_reprovision_after_restart_keeps_the_entered_connection(cat, secrets, monkeypatch):
    """After an ECS restart the catalog entry is rebuilt from the graph and
    re-marked user-origin from the bound secret (server._restore); the node's
    own adt/conn/<id> pointer is the durable signal, so a re-onboard must not
    repoint the system at the demo DB."""
    _register_pg()
    restored = {"id": SID, "name": "Quality Lab", "plant": "Custom", "system_type": "",
                "connection_type": "PostgreSQL", "connection_string": "", "schema": "lims",
                "status": "onboarded", "connection_origin": "user",
                "_provision": {"system_id": SID, "system_name": "Quality Lab",
                               "plant": "Custom", "schema": "lims",
                               "connection_type": "PostgreSQL"}}
    node = {"connection_config": json.dumps({"host": "db.example.internal"}),
            "secret_arn": CONN_ARN, "connection_type": "PostgreSQL"}
    result, calls, started = _provision(restored, monkeypatch, node=node)
    ns = [c for c in calls if c["system_id"] == SID][0]
    assert ns["non_secret"]["host"] == "db.example.internal", ns
    assert ns["secret_arn"] == CONN_ARN
    assert started[-1]["conn_params"]["password"] == PASSWORD


def test_unreadable_own_secret_fails_provision_instead_of_repointing(cat, secrets, monkeypatch):
    """A transient Secrets Manager failure must not silently rewrite a user's
    system onto the demo database: provision returns an error (retryable)
    before any graph write or agent start."""
    entry = _registered_entry(cat, secrets)
    secrets.clear()
    result, calls, started = _provision(entry, monkeypatch)
    assert "unreadable" in result.get("error", ""), result
    assert calls == [] and started == []


def test_demo_system_provision_is_unchanged(cat, secrets, monkeypatch):
    """A demo PG system's node write and agent params are exactly the demo
    routing's, even with a demo-DB secret pointer already on its node."""
    from tests.unit import test_onboard_provision_node_write as h
    monkeypatch.setenv("DB_SECRET_ARN", DEMO_ARN)
    monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
    monkeypatch.setenv("POSTGRES_DB", "adt_demo")
    node = {"connection_config": json.dumps({"host": "db.example.com"}),
            "secret_arn": DEMO_ARN, "connection_type": "PostgreSQL"}
    result, calls, started = _provision(h._pg_entry(), monkeypatch, node=node)
    call = [c for c in calls if c["system_id"] == "plm"][0]
    assert call["secret_arn"] == DEMO_ARN and call["secret"] is None
    assert call["non_secret"]["host"] == "db.example.com"
    assert started[-1]["conn_params"] == onboard._agent_conn_params_for_schema("plm_engineering")
