# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System registration rules. Offline — no AWS, no Neptune, no
PostgreSQL, no network.

The rules these pin:

* an endpoint and its stored credentials come from ONE server-resolved source;
  a caller-supplied URL never receives a stored token;
* a user's own connection secret that cannot be read, or that lacks a user and
  password, never gets the deployment's database credentials filled in, and a
  restart skips only that system;
* generic REST API registration is refused until it can be provisioned;
* a secret counts as a system's own only on an exact ``adt/conn/<id>-XXXXXX``
  resource match in this Region AND a payload bound to that system;
* registration claims its id atomically and rolls the claim back on failure;
  the PostgreSQL schema is derived from the DSN and a schema maps to one
  system; targets are validated at registration and discover; new reads in
  async routes are offloaded;
* one explicit origin marker (``user`` for the HTTP route and the chat tool,
  ``demo`` for the demo hook); demo/workshop systems never touch the
  registration helpers.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_mod  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.errors as errors  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
import agents.server as server  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register, _register_pg, cat, no_dns, secrets)

PINNED_IP = "203.0.113.10"
HB_URL, HB_TOKEN = "https://hb.example.com", "hb-token-123"  # nosec B105 - fixture
USER_TOKEN = "user-token-456"  # nosec B105 - fixture
MCP_URL = "https://mcp.example.com/mcp"
ATTACKER = "https://attacker.example/mcp"
OTHER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/quality-lab-2-AbCdEf"
DEMO_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt-db-credentials-XyZ123"


class _FakeHttpx:
    """httpx.Client stand-in: records every POST (url + Authorization) and
    fails the request, so _discover_mcp returns early."""
    calls: list = []
    client_kwargs: list = []

    def __init__(self, **kwargs):
        _FakeHttpx.client_kwargs.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        _FakeHttpx.calls.append((url, (headers or {}).get("Authorization", "")))
        raise RuntimeError("stop after the first request")


@pytest.fixture
def httpx_spy(monkeypatch):
    import httpx
    _FakeHttpx.calls, _FakeHttpx.client_kwargs = [], []
    monkeypatch.setattr(httpx, "Client", _FakeHttpx)
    return _FakeHttpx


@pytest.fixture
def highbyte(monkeypatch):
    monkeypatch.setattr(catalog_mod, "_get_highbyte_url", lambda: HB_URL)
    monkeypatch.setattr(catalog_mod, "_get_highbyte_token", lambda: HB_TOKEN)


def _mcp_discover(system_id, body_url):
    body = DiscoverRequest(system_id=system_id, connection_type="MCP Server",
                           connection_string=body_url)
    return asyncio.run(onboard._discover_mcp_server(body, system_id, body_url, "", "MCP Server"))


def _register_mcp(token=USER_TOKEN):
    cfg = {"token": token} if token else {}
    return _register({"name": "Quality Lab", "connection_type": "MCP Server",
                      "connection_string": MCP_URL, "connection_config": cfg})


def _add_demo_mcp(cat):
    cat.add({"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
             "schema": "", "status": "registered"})


# ── a stored token never travels to a caller-supplied URL ───────────

def test_h1_custom_mcp_body_url_override_is_rejected(cat, secrets, httpx_spy):
    _register_mcp()
    res = _mcp_discover(SID, ATTACKER)
    assert "error" in res, res
    assert all(url != ATTACKER for url, _ in httpx_spy.calls), httpx_spy.calls
    assert all(USER_TOKEN not in auth for _, auth in httpx_spy.calls)


def test_h1_custom_mcp_same_url_after_normalisation_uses_its_token(cat, secrets, httpx_spy):
    _register_mcp()
    _mcp_discover(SID, "HTTPS://MCP.example.com:443/mcp")
    # A bare user token goes out as a Bearer token (test_connect_mcp_default_bearer.py).
    assert httpx_spy.calls == [(MCP_URL, f"Bearer {USER_TOKEN}")], httpx_spy.calls


def test_h1_highbyte_body_url_override_is_rejected(cat, secrets, highbyte, httpx_spy):
    _add_demo_mcp(cat)
    res = _mcp_discover("highbyte", ATTACKER)
    assert "error" in res, res
    assert all(HB_TOKEN not in auth for _, auth in httpx_spy.calls), httpx_spy.calls


def test_h1_highbyte_without_body_url_keeps_its_bootstrap_pair(cat, secrets, highbyte, httpx_spy):
    _add_demo_mcp(cat)
    _mcp_discover("highbyte", "")
    assert httpx_spy.calls == [(HB_URL + "/mcp", f"Bearer {HB_TOKEN}")], httpx_spy.calls


def test_h1_body_only_url_runs_tokenless(cat, secrets, highbyte, httpx_spy):
    _mcp_discover("", "https://lab.example.com/mcp")
    assert httpx_spy.calls == [("https://lab.example.com/mcp", "")], httpx_spy.calls


# ── never fill a user's own secret from the deployment env ──────────

def _own_resolved(**extra):
    """A resolved dict as resolve_connection builds it from a node that points
    at the user's own bound secret (the secret payload merged last)."""
    return {"system_id": SID, "connection_type": "PostgreSQL", "secret_arn": CONN_ARN,
            "host": "attacker.example", "port": "5432", "dbname": "lims",
            "schema": "lims", "connection_origin": "user", "_secret_unresolved": False,
            **extra}


def _typed_error():
    return getattr(errors, "ConnectionSecretUnavailableError", None)


@pytest.mark.parametrize("case", [
    {"_secret_unresolved": True},
    {"user": "lab_reader"},                      # readable but no password
    {"password": PASSWORD},                      # readable but no user
])
def test_h2_pg_conn_params_refuses_env_fill_for_own_secret(monkeypatch, case):
    called = []
    monkeypatch.setattr(db, "_env_conn_params_for_schema",
                        lambda schema: called.append(schema) or {"password": "deploy-pw"})
    err = _typed_error()
    assert err is not None, "agents.errors.ConnectionSecretUnavailableError is missing"
    with pytest.raises(err):
        cr.pg_conn_params(_own_resolved(**case))
    assert called == [], "no deployment credential may be read for a user's own secret"


def test_h2_pg_conn_params_demo_pointer_keeps_env_fill(monkeypatch):
    monkeypatch.setattr(db, "_env_conn_params_for_schema",
                        lambda schema: {"user": "postgres", "password": "deploy-pw",
                                        "host": "seed", "port": "5432", "dbname": "adt_demo"})
    params = cr.pg_conn_params({"system_id": "cmms", "secret_arn": DEMO_ARN,
                                "host": "seed", "schema": "cmms_maintenance"})
    assert params["password"] == "deploy-pw" and params["host"] == "seed"


def test_h2_restore_skips_only_the_system_with_an_unreadable_own_secret(monkeypatch):
    from unittest.mock import MagicMock, patch
    from tests.unit import test_restore_isolation as iso
    resolved = {"user-sys": {"system_id": "user-sys", "secret_arn": CONN_ARN.replace(
                    SID, "user-sys"), "host": "attacker.example", "_secret_unresolved": True},
                "demo-sys": {"system_id": "demo-sys", "secret_arn": DEMO_ARN, "host": "seed",
                             "schema": "demo_data"}}
    env_reads = []
    monkeypatch.setattr(db, "_env_conn_params_for_schema",
                        lambda schema: env_reads.append(schema) or {
                            "user": "postgres", "password": "deploy-pw", "host": "seed",
                            "port": "5432", "dbname": "adt_demo"})
    starts = iso._Starts(set(), RuntimeError("unused"))
    mgr = MagicMock()
    mgr.start_agent = starts
    cat_mock = MagicMock()
    cat_mock.get_all.return_value = [{"id": "user-sys"}, {"id": "demo-sys"}]
    with patch.object(server, "_agent_manager", mgr), \
            patch.object(server, "_sql_tables_desc_from_graph", return_value=""), \
            patch.object(server, "neptune_query", MagicMock(side_effect=[
                [{"id": "user-sys", "name": "U", "schema": "u_data", "status": "onboarded"},
                 {"id": "demo-sys", "name": "D", "schema": "demo_data",
                  "status": "onboarded"}], []])), \
            patch.object(server, "pg", iso._fake_pg), \
            patch.object(server, "catalog", cat_mock), \
            patch.object(server, "add_system_agent", MagicMock()), \
            patch.object(cr, "resolve_connection", MagicMock(side_effect=lambda s: resolved[s])), \
            patch("agents.onboard.neptune_query_checked", MagicMock(return_value=[])), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()
    assert starts.calls == ["demo-sys"], starts.calls
    assert env_reads == ["demo_data"], env_reads


def test_m5_restore_re_marks_a_user_system_from_its_bound_secret(monkeypatch):
    """After a restart the catalog entry is rebuilt from the graph; the origin
    comes back from the durable marker in the system's own secret."""
    from unittest.mock import MagicMock, patch
    from tests.unit import test_restore_isolation as iso
    own = CONN_ARN.replace(SID, "user-sys")
    resolved = {"user-sys": {"system_id": "user-sys", "secret_arn": own, "host": "lab",
                             "user": "u", "password": "p", "connection_origin": "user"},
                "demo-sys": {"system_id": "demo-sys", "secret_arn": DEMO_ARN, "host": "seed",
                             "user": "u", "password": "p"}}
    cat_mock = MagicMock()
    cat_mock.get_all.return_value = [{"id": "user-sys"}, {"id": "demo-sys"}]
    mgr = MagicMock()
    mgr.start_agent = iso._Starts(set(), RuntimeError("unused"))
    with patch.object(server, "_agent_manager", mgr), \
            patch.object(server, "_sql_tables_desc_from_graph", return_value=""), \
            patch.object(server, "neptune_query", MagicMock(side_effect=[
                [{"id": "user-sys", "name": "U", "schema": "u_data", "status": "onboarded"},
                 {"id": "demo-sys", "name": "D", "schema": "demo_data",
                  "status": "onboarded"}], []])), \
            patch.object(server, "pg", iso._fake_pg), \
            patch.object(server, "catalog", cat_mock), \
            patch.object(server, "add_system_agent", MagicMock()), \
            patch.object(cr, "resolve_connection", MagicMock(side_effect=lambda s: resolved[s])), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()
    marks = [c.args for c in cat_mock.set_connection_origin.call_args_list]
    assert marks == [("user-sys", "user")], marks


@pytest.mark.parametrize("payload", [
    {"connection_string": "postgresql://lab_reader@db.example.internal:5432/lims"
                          "?options=-csearch_path%3Dlims"},                # no password
    {"connection_string": DSN, "user": "lab_reader", "password": PASSWORD},  # no origin/binding
    {"connection_string": DSN, "user": "lab_reader", "password": PASSWORD,
     "connection_origin": "user", "system_id": "someone-else"},           # bound elsewhere
])
def test_h2_registered_pg_dsn_rejects_an_unusable_own_secret(cat, secrets, payload):
    _register_pg()
    secrets[CONN_ARN] = payload
    err = _typed_error()
    assert err is not None
    with pytest.raises(err):
        cr.registered_pg_dsn(SID, cat.get(SID))


# ── generic REST API is refused until it can be provisioned ─────────

def test_h3_rest_api_registration_is_refused(cat, secrets):
    res = _register({"name": "Quality Lab", "connection_type": "REST API",
                     "connection_string": "https://api.example.com/openapi.json"})
    assert "error" in res and "not supported" in res["error"], res
    assert cat.get(SID) is None


# ── exact own-secret ownership ──────────────────────────────────────

_OWN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/foo-AbC123"


@pytest.mark.parametrize("arn, own", [
    (_OWN, True),
    ("arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/foo-bar-AbC123", False),
    ("arn:aws:secretsmanager:eu-west-1:111122223333:secret:adt/conn/foo-AbC123", False),
    ("arn:aws:secretsmanager:us-east-1:11112222333:secret:adt/conn/foo-AbC123", False),
    ("arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/foo-AbC12", False),
    ("arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/foo-AbC1234", False),
    ("arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/foo", False),
    ("arn:aws:secretsmanager:us-east-1:111122223333:secret:x/adt/conn/foo-AbC123", False),
    ("arn:aws:ssm:us-east-1:111122223333:secret:adt/conn/foo-AbC123", False),
    (DEMO_ARN, False),
    ("", False),
])
def test_h4_own_secret_is_an_exact_resource_match(monkeypatch, arn, own):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert cr.is_own_connection_secret(arn, "foo") is own


def test_h4_a_node_pointer_to_another_systems_secret_is_refused(cat, secrets, monkeypatch):
    _register_pg()
    entry = dict(cat.get(SID))
    entry.pop("secret_arn")
    secrets[OTHER_ARN] = {"connection_string": DSN, "user": "x", "password": "y",
                          "connection_origin": "user", "system_id": "quality-lab-2"}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: {"secret_arn": OTHER_ARN})
    err = _typed_error()
    assert err is not None
    with pytest.raises(err):
        cr.registered_pg_dsn(SID, entry)


# ── atomic claim + rollback ──────────────────────────────────────────

def test_m1_concurrent_same_name_registrations_create_one_entry(cat, secrets, monkeypatch):
    barrier = threading.Barrier(2)
    real_lookup = cat.lookup

    def _slow_lookup(sid):
        found = real_lookup(sid)
        barrier.wait(timeout=5)
        return found
    monkeypatch.setattr(cat, "lookup", _slow_lookup)
    results = []
    threads = [threading.Thread(target=lambda: results.append(_register_pg())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted("error" in r for r in results) == [False, True], results
    assert [s["id"] for s in cat.get_all()] == [SID]
    assert list(secrets) == [CONN_ARN]


def test_m1_a_failed_secret_write_rolls_the_claim_back(cat, secrets, monkeypatch):
    def _boom(system_id, secret):
        raise RuntimeError("denied")
    monkeypatch.setattr(cr, "_store_connection_secret", _boom)
    assert "error" in _register_pg()
    assert cat.get(SID) is None
    assert "error" not in _register_mcp(token="")  # nosec B106 - an empty test token; the id is free again for a retry


# ── schema from the DSN; one system per schema ──────────────────────

def _pg(**kw):
    body = {"name": "Quality Lab", "connection_type": "PostgreSQL",
            "connection_config": {"dsn": DSN}}
    body.update(kw)
    return _register(body)


def test_m2_schema_is_derived_from_the_dsn(cat, secrets):
    res = _pg()
    assert res.get("status") == "created", res
    assert cat.get(SID)["schema"] == "lims"


@pytest.mark.parametrize("kw", [
    {"schema": "other"},
    {"connection_config": {"dsn": DSN.split("?")[0]}},                         # no search_path
    {"connection_config": {"dsn": DSN.replace("5432/lims", "5432/")}},          # no database
])
def test_m2_bad_schema_or_database_is_rejected(cat, secrets, kw):
    assert "error" in _pg(**kw)
    assert cat.get(SID) is None and secrets == {}


def test_m2_a_schema_already_registered_is_rejected(cat, secrets):
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "lims", "status": "registered"})
    res = _pg()
    assert "error" in res and "lims" in res["error"], res
    assert secrets == {}


def test_m2_records_first_refuses_a_schema_shared_with_a_user_registration(cat, secrets):
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "shared", "status": "registered"})
    cat.add({"id": "lab", "name": "Lab", "connection_type": "PostgreSQL", "schema": "shared",
             "status": "registered", "connection_origin": "user"})
    with pytest.raises(errors.MissingDbPasswordError):
        db._records_conn_params_for_schema("shared")


# ── validate at registration and at every discover use ──────────────

def test_m3_registration_validates_the_target_before_storing(cat, secrets, monkeypatch):
    def _deny(target, connection_type=""):
        raise cr.ConnectionTargetError("connection target resolves to a denied address")
    monkeypatch.setattr(cr, "validate_connection_target", _deny)
    for res in (_register_pg(), _register_mcp(token="")):  # nosec B106 - an empty test token
        assert "error" in res and "denied" in res["error"], res
    assert cat.get_all() == [] and secrets == {}


def test_m3_pg_discover_of_a_registration_pins_the_validated_ip(cat, secrets, monkeypatch):
    _register_pg()
    seen = []

    def _connect(dsn, **kw):
        seen.append((dsn, kw.get("hostaddr")))
        raise RuntimeError("stop after connect")
    monkeypatch.setattr(onboard.pgdriver, "connect", _connect)
    body = DiscoverRequest(system_id=SID, connection_type="PostgreSQL")
    with pytest.raises(RuntimeError, match="stop after connect"):
        asyncio.run(onboard._discover_postgres_schema(body, SID, "", "lims", "PostgreSQL"))
    assert seen == [(DSN, PINNED_IP)], seen


def test_m3_mcp_discover_validates_the_registered_url(cat, secrets, monkeypatch, httpx_spy):
    _register_mcp(token="")  # nosec B106 - an empty test token

    def _deny(target, connection_type=""):
        raise cr.ConnectionTargetError("connection target resolves to a denied address")
    monkeypatch.setattr(cr, "validate_connection_target", _deny)
    res = _mcp_discover(SID, "")
    assert "error" in res and httpx_spy.calls == [], (res, httpx_spy.calls)


def test_m3_mcp_discover_never_follows_redirects(cat, secrets, httpx_spy):
    _register_mcp(token="")  # nosec B106 - an empty test token
    _mcp_discover(SID, "")
    assert httpx_spy.client_kwargs and all(
        not kw.get("follow_redirects") for kw in httpx_spy.client_kwargs)


# ── new sync reads in async discover go through offload ─────────────

def test_m4_discover_offloads_the_registration_reads(cat, secrets, monkeypatch, httpx_spy):
    _register_pg()
    offloaded = []

    async def _offload(fn, *a, **kw):
        offloaded.append(fn.__name__)
        return fn(*a, **kw)
    monkeypatch.setattr(onboard, "offload", _offload)
    monkeypatch.setattr(onboard.pgdriver, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    body = DiscoverRequest(system_id=SID, connection_type="PostgreSQL")
    with pytest.raises(RuntimeError):
        asyncio.run(onboard._discover_postgres_schema(body, SID, "", "lims", "PostgreSQL"))
    _mcp_discover("", "https://lab.example.com/mcp")
    assert "_pg_discover_target" in offloaded and "_mcp_discover_entry" in offloaded, offloaded


# ── one explicit origin marker; demo never touches the helpers ──────

def test_m5_the_chat_tool_marks_user_origin_durably(cat, secrets):
    onboard_agent.connect_new_system(name="Quality Lab", system_type="Custom", plant="",
                                     connection_type="PostgreSQL",
                                     connection_config={"dsn": DSN})
    assert cat.get(SID).get(cr_attr("CONNECTION_ORIGIN_KEY", "connection_origin")) == "user"
    assert secrets[CONN_ARN].get("connection_origin") == "user"
    assert secrets[CONN_ARN].get("system_id") == SID


def cr_attr(name, default):
    return getattr(cr, name, default)


def test_m5_chat_discover_uses_the_registered_database(cat, secrets, monkeypatch):
    onboard_agent.connect_new_system(name="Quality Lab", system_type="Custom", plant="",
                                     connection_type="PostgreSQL",
                                     connection_config={"dsn": DSN})
    seen = {}

    class _NoSession:  # discover's gate reads the session store first: none yet
        def get_session(self, system_id):
            raise onboard_agent.SessionNotFoundError(system_id)
    monkeypatch.setattr(onboard_agent, "_store", _NoSession)
    monkeypatch.setattr(onboard_agent, "_discover_backend_result",
                        lambda sid, entry, ctype, resolved: seen.update(resolved) or {"error": "x"})
    onboard_agent.discover(system_id=SID)
    assert seen.get("connection_string") == DSN, seen


def test_m5_demo_registration_is_explicitly_demo_origin(cat, monkeypatch):
    from unittest.mock import patch
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    monkeypatch.setattr(server, "catalog", cat)
    with patch("agents.demo.is_enabled", return_value=True), \
            patch("agents.demo.db_routing.demo_pg_dsn", side_effect=lambda s: f"dsn::{s}"):
        server._register_demo_systems()
    assert cat.get("erp") is not None
    assert cat.get("erp").get("connection_origin") == "demo"


@pytest.fixture
def no_registration_helpers(monkeypatch):
    """Every registration helper raises if called; a demo path must not call one."""
    hits = []
    for name in ("registration_connection", "registered_pg_dsn", "pg_params_from_dsn"):
        def _trip(*a, _n=name, **k):
            hits.append(_n)
            raise AssertionError(f"demo path called {_n}")
        monkeypatch.setattr(cr, name, _trip)
    return hits


def test_m5_demo_preflight_discover_and_sampling_never_call_the_helpers(
        cat, secrets, no_registration_helpers, monkeypatch):
    import agents.demo.db_routing as routing
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "registered", "connection_origin": "demo"})
    monkeypatch.setattr(routing, "demo_pg_dsn", lambda schema: f"seed-dsn-for-{schema}")
    out = op._bootstrap_unregistered({"system_id": "cmms", "connection_string": "",
                                      "_unconfigured": True}, "cmms")
    assert out["connection_string"] == "seed-dsn-for-cmms_maintenance"
    assert db._records_conn_params_for_schema("cmms_maintenance") is None
    seen = []
    monkeypatch.setattr(onboard.pgdriver, "connect",
                        lambda dsn, **k: seen.append((dsn, k)) or (_ for _ in ()).throw(
                            RuntimeError("stop")))
    body = DiscoverRequest(system_id="cmms", connection_type="PostgreSQL")
    with pytest.raises(RuntimeError):
        asyncio.run(onboard._discover_postgres_schema(body, "cmms", "", "cmms_maintenance",
                                                      "PostgreSQL"))
    assert seen == [("seed-dsn-for-cmms_maintenance", {})], seen
    assert no_registration_helpers == []


def test_m5_demo_provision_never_calls_the_helpers_or_reads_the_node(
        cat, secrets, no_registration_helpers, monkeypatch):
    from tests.unit import test_onboard_provision_node_write as h
    node_reads = []
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: node_reads.append(sid) or None)
    monkeypatch.setenv("DB_SECRET_ARN", DEMO_ARN)
    monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
    entry = h._pg_entry()
    entry["connection_origin"] = "demo"
    result, calls = h._run_provision(entry)
    assert result["status"] == "provisioned", result
    assert [c["secret_arn"] for c in calls if c["system_id"] == "plm"] == [DEMO_ARN]
    assert no_registration_helpers == [] and node_reads == []


# ── authenticated custom MCP keeps its token at provision ───────────

def _provision_mcp(cat, monkeypatch, body_url=MCP_URL):
    from tests.unit import test_onboard_provision_node_write as h
    started = []
    monkeypatch.setattr(h._FakeAgentManager, "start_agent",
                        lambda self, **kw: started.append(kw) or "mcp://test-agent")
    entry = dict(cat.get(SID))
    entry["_provision"] = {"system_id": SID, "system_name": "Quality Lab", "plant": "",
                           "connection_type": "MCP Server", "connection_string": body_url}
    result, calls = h._run_provision(entry)
    return result, calls, started


def test_m6_provision_uses_the_registered_token_and_keeps_the_pointer(cat, secrets, monkeypatch):
    _register_mcp()
    assert USER_TOKEN not in json.dumps(cat.get(SID))   # sanitized catalog: secret is the only source
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert result["status"] == "provisioned", result
    cfg = started[-1]["connection_config"]
    assert cfg["url"] == MCP_URL and cfg["token"] == USER_TOKEN, cfg
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["secret_arn"] == CONN_ARN and node["secret"] is None, node


def test_m6_provision_refuses_a_body_url_that_differs(cat, secrets, monkeypatch):
    _register_mcp()
    result, calls, started = _provision_mcp(cat, monkeypatch, body_url=ATTACKER)
    assert "error" in result, result
    assert started == [] and calls == []
