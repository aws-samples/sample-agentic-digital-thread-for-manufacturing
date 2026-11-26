# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Storing a system's credentials runs in the app, as one durable step.

The onboarding gateway (CUSTOM_JWT) accepts any of the app's tokens, and its
per-tool policy is not attached. The onboarding runtime behind it exported
``store_credentials``, which creates or overwrites ``adt/systems/<id>`` in
AWS Secrets Manager under the runtime's role, so a signed-in user without the
integrator or Admin role could call it on the gateway directly, past the
app's check on ``/api/onboard/*``. The runtime no longer exports it.

Two entry points store credentials, both in the app and both behind the
router-level integrator/Admin gate: the chat's ``store_credentials`` tool
(``onboard_chat_lifecycle.store_credentials``) and the secure form the chat
shows (``POST /api/onboard/credentials``). They run ONE implementation
(``onboard_chat_lifecycle.store_system_credentials``), so every test of the
store below runs through both.

The app and the onboarding runtime are separate processes with separate
catalogs, and the runtime resolves a connection from the System node only. So
the store holds the system's connection lock (the one Edit connection takes),
reads the System node checked and classifies the system from it, writes the
secret, drops the app's cached copy of it, and points the node at it with ONE
compare-and-set statement that only lands while the node's connection type and
pointer are still the ones it read. Only then does the app's catalog entry
change and the answer say "stored". Systems whose credentials the next steps
never read from there are refused with nothing written. A running agent, and
a reader holding a cached copy of the same secret, keep the old credentials
for a while; the answer says so instead of claiming they are in use.

Offline: Secrets Manager, the graph and the session store are fakes; the
gateway client is spied and never connected.
"""
from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.types import Tool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit._mcp_tool_filter import client_keeps  # noqa: E402
import agents.onboard_chat_lifecycle as lifecycle
from agents.catalog import SystemCatalog
from agents.config import get_config
from agents.connection_lock import PROVISION, ConnectionBusyError, connection_lock
from agents.job_store import JobStore
from tests.unit.test_discover_gate_names_the_next_step import sessions  # noqa: F401 - fixture

#: The onboarding tools that change a secret, the catalog, a runtime or the
#: graph (Decision 2's commit tier), enumerated here independently of the code.
COMMIT_TIER = {"connect_new_system", "store_credentials", "provision_system", "reset_system"}

_SCHEMA = {"type": "object", "properties": {"system_id": {"type": "string"}},
           "required": ["system_id"]}

#: A REST-family system the deployment registered (not Connect New System),
#: onboarded. Its System node points at an older secret (OLD_ARN, a secret
#: recreated since); every store writes the system's one named secret
#: ``adt/systems/<id>``, whose ARN is NEW_ARN (Secrets Manager answers the
#: same ARN for every later write of that name).
SID = "line-api"
URL = "https://line-api.example.com"
_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:"
OLD_ARN = f"{_ARN}adt/systems/{SID}-Old000"
NEW_ARN = f"{_ARN}adt/systems/{SID}-New000"
_NEW = {"token": "new-token"}
VIAS = ["chat", "form"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("AGENT_MODE", "DEV_MODE", "ONBOARDING_GATEWAY_URL", "SYSTEM_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    get_config.cache_clear()
    yield
    lifecycle.bind_runtime_discover(None)
    get_config.cache_clear()


def _sys_arn(sid: str) -> str:
    return NEW_ARN if sid == SID else f"{_ARN}adt/systems/{sid}-New000"


def _node(connection_type: str, secret_arn: str, config: dict, status="onboarded") -> dict:
    return {"status": status, "connection_type": connection_type,
            "connection_config": json.dumps(config), "secret_arn": secret_arn,
            "connection_string": ""}


def _entry(sid: str, connection_type: str, **extra) -> dict:
    return {"id": sid, "name": sid, "plant": "", "system_type": "MES", "schema": "",
            "status": "onboarded", "connection_type": connection_type,
            "connection_origin": "demo", **extra}


class _Secrets:
    """Secrets Manager as the resolver's client sees it (reads only)."""

    def __init__(self, vault):
        self.vault, self.reads = vault, []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 keyword
        self.reads.append(SecretId)
        return {"SecretString": json.dumps(self.vault[SecretId])}


class _World:
    """The graph (System nodes), Secrets Manager and the app's catalog, kept
    apart from the onboarding runtime: the runtime reads the graph only."""

    def __init__(self):
        self.graph: dict = {SID: _node("REST API", OLD_ARN, {"base_url": URL})}
        self.vault: dict = {OLD_ARN: {"token": "old-token"}}
        self.secrets = _Secrets(self.vault)
        self.stored: list = []          # (system_id, credentials) written to Secrets Manager
        self.node_writes: list = []     # every System-node write, as (cypher, params)
        self.write_answer = True        # False: the graph client refuses the write
        self.apply_writes = True        # False: a write that answers but lands nowhere
        self.during_store = None        # called inside the Secrets Manager write
        self.app = SystemCatalog([])

    def put(self, sid, connection_type, node_arn, config, **entry):
        self.graph[sid] = _node(connection_type, node_arn, config)
        self.app.add(_entry(sid, connection_type, **entry))

    def rotation(self):
        """The node already points at the system's named secret, which holds
        the old token: the next store overwrites that same secret."""
        self.graph[SID]["secret_arn"] = NEW_ARN
        self.vault[NEW_ARN] = {"token": "old-token"}
        self.app.get(SID)["secret_arn"] = NEW_ARN


@pytest.fixture
def world(monkeypatch, sessions):  # noqa: F811 - fixture
    w = _World()
    w.app.add(_entry(SID, "REST API", secret_arn=OLD_ARN,
                     connection_config={"base_url": URL}))
    monkeypatch.setattr(w.app, "_graph_rows", lambda: [])
    monkeypatch.setattr(onboard, "catalog", w.app)
    monkeypatch.setattr(oa, "catalog", w.app)
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: dict(w.graph[sid]) if sid in w.graph else None)
    monkeypatch.setattr(cr, "_secrets_client", lambda: w.secrets)
    monkeypatch.setattr(cr, "_secret_cache", {})

    def _write(cypher, params):
        """An unconditional pointer write (what a store must not rely on)."""
        w.node_writes.append((cypher, dict(params)))
        if w.write_answer and w.apply_writes and params.get("sid") in w.graph \
                and cypher.startswith("MATCH ") and "s.secret_arn = $arn" in cypher:
            w.graph[params["sid"]]["secret_arn"] = params["arn"]
        return w.write_answer
    monkeypatch.setattr(cr, "_neptune_write", _write)

    def _write_returning(cypher, params):
        """The graph running the store's compare-and-set statement: it lands
        only while the node's type and pointer still equal the expected ones,
        and answers the rows it matched (``None``: the backend refused it)."""
        w.node_writes.append((cypher, dict(params)))
        if not w.write_answer:
            return None
        node = w.graph.get(params.get("sid"))
        if (node is None or (node.get("connection_type") or "") != params["ctype"]
                or (node.get("secret_arn") or "") != params["expected"]):
            return []
        if w.apply_writes:
            node["secret_arn"] = params["arn"]
        return [{"secret_arn": node.get("secret_arn") or ""}]
    monkeypatch.setattr(cr, "_neptune_write_returning", _write_returning, raising=False)

    def _store(sid, credentials):
        w.stored.append((sid, dict(credentials)))
        w.vault[_sys_arn(sid)] = dict(credentials)
        if w.during_store:
            w.during_store()
        return _sys_arn(sid)
    monkeypatch.setattr(oa, "_store_secret", _store)
    monkeypatch.setattr(onboard, "_cfg_backfill_attempted", set())
    return w


def _app(*, is_admin=False, is_integrator=True) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _signed_in(request, call_next):
        # What the auth middleware sets for a validated session.
        request.state.is_admin = is_admin
        request.state.is_integrator = is_integrator
        request.state.session = {"access_token": "user-token"}
        return await call_next(request)
    app.include_router(onboard.router)
    return app


def _store(via: str, sid: str = SID, credentials: dict | None = None) -> dict:
    """Store credentials the way the chat's tool or the secure form does."""
    credentials = _NEW if credentials is None else credentials
    if via == "chat":
        return lifecycle.store_credentials(sid, json.dumps(credentials))
    resp = TestClient(_app()).post("/api/onboard/credentials",
                                   json={"system_id": sid, "credentials": credentials})
    assert resp.status_code == 200, resp.text
    return resp.json()


class _Gateway:
    """A gateway MCP client that records every tool it is asked to call."""

    built: list = []

    def __init__(self, transport_callable=None, **kwargs):
        self.calls: list = []
        _Gateway.built.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_tools_sync(self):
        return ["onboarding___discover"]

    def call_tool_sync(self, tool_use_id, name, arguments):
        self.calls.append(name)
        return {"status": "success", "content": []}


def _cloud_tools(monkeypatch) -> list:
    """The tool list a deployment's onboarding chat agent gets."""
    import agents.gateway_auth as ga
    _Gateway.built = []
    monkeypatch.setattr(ga, "MCPClient", _Gateway)
    monkeypatch.setattr(ga, "streamablehttp_client", lambda url, headers=None: None)
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    return oa._get_onboard_tools("user-token")


def _by_name(tools) -> dict:
    return {getattr(t, "tool_name", None): t for t in tools}


def _provision_inputs(system_id: str = SID):
    """What the app's provision of a REST-family system takes as its URL,
    token, auth scheme and secret pointer (``onboard._dp_i3x_connection_inputs``)."""
    ctx = SimpleNamespace(system_id=system_id, connection_type="REST API",
                          body=SimpleNamespace(connection_string=""))
    return onboard._dp_i3x_connection_inputs(ctx)


# ── the gateway no longer serves it ────────────────────────────────────────

def test_the_onboarding_runtime_exports_no_commit_tier_tool(monkeypatch):
    from agents.onboarding_runtime_server import RUNTIME_TOOLS, create_onboarding_server
    assert "store_credentials" not in [t.tool_name for t in lifecycle.gateway_tools(
        oa.ONBOARD_TOOLS)]
    assert not ({t.tool_name for t in RUNTIME_TOOLS} & COMMIT_TIER)
    monkeypatch.setenv("AGENT_MODE", "local")  # the server's boot check needs no env here
    get_config.cache_clear()
    served = {t.name for t in asyncio.run(create_onboarding_server().list_tools())}
    assert not (served & COMMIT_TIER), served & COMMIT_TIER
    assert "discover" in served  # the chat still forwards a runtime-owned discover to it


@pytest.mark.parametrize("name", ["onboarding___store_credentials", "store_credentials"])
def test_the_chats_gateway_client_hides_the_gateways_store_credentials(name):
    client = oa._create_onboarding_gateway_client("https://gw.example.com/mcp", "tok")
    assert client_keeps(client,
        MCPAgentTool(Tool(name=name, inputSchema=_SCHEMA), client)) is False


def test_in_a_deployment_the_chat_gets_the_apps_own_store_credentials(monkeypatch):
    tools = _cloud_tools(monkeypatch)
    app_tool = _by_name(tools[1:])["store_credentials"]
    assert app_tool is lifecycle.store_credentials
    assert app_tool is not oa.store_credentials
    # The model sees the same tool: same name and parameters as before.
    assert app_tool.tool_name == oa.store_credentials.tool_name
    assert app_tool.tool_spec["inputSchema"] == oa.store_credentials.tool_spec["inputSchema"]


def test_the_chats_store_calls_no_gateway(monkeypatch, world):
    tool = _by_name(_cloud_tools(monkeypatch)[1:])["store_credentials"]
    assert tool(SID, json.dumps(_NEW))["status"] == "stored"
    assert _Gateway.built and all(g.calls == [] for g in _Gateway.built)


# ── a store is one durable step every later reader sees ────────────────────

@pytest.mark.parametrize("via", VIAS)
def test_the_runtime_resolves_the_new_credentials_from_the_graph(world, via):
    """The onboarding runtime's test_connection and discover resolve a
    connection from the System node (``resolve_connection``), never from the
    app's catalog, so the store must point the node at the new secret."""
    runtime_catalog = SystemCatalog([])        # the runtime's own catalog, never written
    runtime_catalog.add(_entry(SID, "REST API", secret_arn=OLD_ARN))
    res = _store(via)
    assert res["status"] == "stored" and res["secret_arn"] == NEW_ARN, res
    assert world.stored == [(SID, _NEW)]
    resolved = cr.resolve_connection(SID)
    assert resolved["secret_arn"] == NEW_ARN
    assert resolved["token"] == "new-token"
    # Only the pointer moved: the node keeps its connection settings and type.
    assert json.loads(world.graph[SID]["connection_config"]) == {"base_url": URL}
    assert world.graph[SID]["connection_type"] == "REST API"
    assert runtime_catalog.get(SID)["secret_arn"] == OLD_ARN


@pytest.mark.parametrize("via", VIAS)
def test_provision_and_a_restarted_app_use_the_new_credentials(monkeypatch, world, via):
    """The app's provision reads the new token, and so does a restarted app:
    its catalog is rebuilt from the graph (the entry gets the node's pointer
    back through the catalog's backfill)."""
    assert _store(via)["status"] == "stored"
    assert world.app.get(SID)["secret_arn"] == NEW_ARN
    url, token, _scheme, secret_arn = _provision_inputs()
    assert (url, token, secret_arn) == (URL, "new-token", NEW_ARN)
    restarted = SystemCatalog([])
    rows = [{"id": sid, "name": sid, **node} for sid, node in world.graph.items()]
    monkeypatch.setattr(restarted, "_graph_rows", lambda: rows)
    assert restarted.hydrate() == 1
    monkeypatch.setattr(onboard, "catalog", restarted)
    onboard._lazy_backfill_connection_config(SID)
    assert restarted.get(SID)["secret_arn"] == NEW_ARN
    url, token, _scheme, secret_arn = _provision_inputs()
    assert (url, token, secret_arn) == (URL, "new-token", NEW_ARN)


def test_the_pointer_is_published_by_one_compare_and_set_statement(world):
    assert _store("chat")["status"] == "stored"
    assert len(world.node_writes) == 1, world.node_writes
    cypher, params = world.node_writes[0]
    assert cypher.startswith("MATCH (s:System {id: $sid})") and "MERGE" not in cypher
    assert "WHERE" in cypher and "s.connection_type" in cypher and "$expected" in cypher
    assert "SET s.secret_arn = $arn" in cypher and "RETURN" in cypher
    assert (params["expected"], params["ctype"], params["arn"]) == (OLD_ARN, "REST API",
                                                                    NEW_ARN)


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("failure", ["write_refused", "not_landed"])
def test_a_pointer_the_graph_did_not_take_is_reported_and_no_catalog_changes(
        world, via, failure):
    if failure == "write_refused":
        world.write_answer = False
    else:
        world.apply_writes = False
    res = _store(via)
    assert res.get("reason") == "pointer_not_saved", res
    assert "status" not in res
    assert "written to AWS Secrets Manager" in res["error"], res
    assert "not in use" in res["error"] and "still apply" in res["error"], res
    assert world.app.get(SID)["secret_arn"] == OLD_ARN
    assert cr.resolve_connection(SID)["secret_arn"] == OLD_ARN


@pytest.mark.parametrize("via", VIAS)
def test_an_unreadable_graph_refuses_before_anything_is_written(monkeypatch, world, via):
    from agents import db
    from agents.db import GraphQueryError

    def _down(cypher, parameters=None):
        raise GraphQueryError("ServiceUnavailable", "graph unreachable")
    monkeypatch.setattr(db, "neptune_query_checked", _down)
    res = _store(via)
    assert res.get("reason") == "graph_unavailable", res
    assert world.stored == [] and world.node_writes == []
    assert world.app.get(SID)["secret_arn"] == OLD_ARN


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("node", ["none", "no_config"])
def test_a_system_with_no_connection_record_is_refused(world, via, node):
    world.app.add(_entry("new-api", "REST API"))
    if node == "no_config":
        world.graph["new-api"] = {"status": "onboarded", "connection_type": "REST API",
                                  "connection_config": "", "secret_arn": ""}
    res = _store(via, "new-api")
    assert res.get("reason") == "no_connection_record", res
    assert "Provision" in res["error"] and "Nothing was stored" in res["error"], res
    assert world.stored == [] and world.node_writes == []


# ── systems the next steps never read these credentials for ────────────────

_OWN_CONN = f"{_ARN}adt/conn/edited-api-AbCdEf"


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("sid,ctype,node_arn,reason", [
    ("highbyte", "MCP Server", "", "edit_connection"),
    ("edited-api", "REST API", _OWN_CONN, "edit_connection"),
    ("plant-mcp", "MCP Server", "", "edit_connection"),
    ("plant-erp", "PostgreSQL", f"{_ARN}DatabaseSecret-AbCdEf", "deployment_credentials"),
    ("plant-sitewise", "SiteWise (Direct)", "", "deployment_credentials"),
    ("plant-lake", "Athena", "", "deployment_credentials"),
], ids=["builtin-highbyte", "own-connection-secret", "mcp", "postgresql", "sitewise", "athena"])
def test_a_system_whose_credentials_are_kept_elsewhere_is_refused(
        world, via, sid, ctype, node_arn, reason):
    """The built-in HighByte's key is adt/conn/highbyte; an Admin's Edit
    connection writes the system's own adt/conn/<id>; an MCP Server's
    provision reads its token only from its own connection secret; and a
    PostgreSQL, SiteWise (Direct) or Athena system the deployment registered
    connects with the deployment's database secret or an AWS IAM role. A
    secret written here would be read by none of the next steps."""
    world.put(sid, ctype, node_arn, {"url": "https://hub.example.com/mcp"})
    res = _store(via, sid)
    assert res.get("reason") == reason, res
    assert "Nothing was stored" in res["error"], res
    assert world.stored == [] and world.node_writes == []
    assert world.graph[sid]["secret_arn"] == node_arn


@pytest.mark.parametrize("via", VIAS)
def test_the_system_is_classified_from_its_graph_node_not_the_app_catalog(world, via):
    """The app's entry still says REST API; the node, which an Edit changed,
    says MCP Server. The node decides."""
    world.graph[SID] = _node("MCP Server", "", {"url": "https://hub.example.com/mcp"})
    res = _store(via)
    assert res.get("reason") == "edit_connection", res
    assert world.stored == [] and world.node_writes == []


@pytest.mark.parametrize("via", VIAS)
def test_a_system_registered_with_connect_new_system_is_refused(world, via):
    """Its credentials live in its own bound adt/conn/<id> secret (the Onboard
    view's Edit connection changes them)."""
    world.app.get(SID)["connection_origin"] = "user"
    res = _store(via)
    assert res.get("reason") == "edit_connection", res
    assert "Edit connection" in res["error"] and "Nothing was stored" in res["error"]
    assert world.stored == [] and world.node_writes == []
    assert world.app.get(SID)["secret_arn"] == OLD_ARN


@pytest.mark.parametrize("via", VIAS)
def test_a_system_the_app_does_not_know_is_refused(world, via):
    res = _store(via, "not-registered")
    assert res.get("reason") == "connect_in_onboard_view", res
    assert "Nothing was registered" in res["error"]
    assert world.stored == []


@pytest.mark.parametrize("credentials", ["not json", "[]", "{}", '"text"'])
def test_credentials_that_are_not_a_json_object_are_refused(world, credentials):
    res = lifecycle.store_credentials(SID, credentials)
    assert "error" in res and "JSON object" in res["error"], res
    assert world.stored == [] and world.node_writes == []


def test_an_invalid_system_id_is_rejected_before_anything_is_read(world):
    with pytest.raises(ValueError):
        lifecycle.store_credentials("../escape", json.dumps(_NEW))
    res = _store("form", "../escape")
    assert "error" in res and "status" not in res, res
    assert world.stored == []


# ── never over a concurrent change of the connection ───────────────────────

@pytest.mark.parametrize("via", VIAS)
def test_an_edit_between_the_read_and_the_publish_is_not_overwritten(world, via):
    """Another change (an Edit connection that did not take this process's
    lock, in another task) moves the node to an MCP Server on its own
    adt/conn secret while the store writes the secret. The compare-and-set
    finds the node changed and publishes nothing."""
    def _edit():
        world.graph[SID] = _node("MCP Server", _OWN_CONN.replace("edited-api", SID),
                                 {"url": "https://hub.example.com/mcp"})
    world.during_store = _edit
    res = _store(via)
    assert res.get("reason") == "conflict", res
    assert "status" not in res
    assert "written to AWS Secrets Manager" in res["error"], res
    assert "changed while" in res["error"] and "not in use" in res["error"], res
    assert world.graph[SID]["connection_type"] == "MCP Server"
    assert world.graph[SID]["secret_arn"] == _OWN_CONN.replace("edited-api", SID)
    assert world.app.get(SID)["secret_arn"] == OLD_ARN


@pytest.mark.parametrize("via", VIAS)
def test_the_store_holds_the_connection_lock_an_edit_takes(world, via):
    inside, release = threading.Event(), threading.Event()

    def _pause():
        inside.set()
        release.wait(30)
    world.during_store = _pause
    out: dict = {}
    worker = threading.Thread(target=lambda: out.update(res=_store(via)), daemon=True)
    worker.start()
    try:
        assert inside.wait(30), "the store never reached its secret write"
        with pytest.raises(ConnectionBusyError):
            with connection_lock(SID, timeout=0.2):
                pass
    finally:
        release.set()
        worker.join(30)
    assert out["res"]["status"] == "stored", out
    with connection_lock(SID, timeout=1):
        pass


@pytest.mark.parametrize("via", VIAS)
def test_a_store_while_the_system_is_being_provisioned_is_refused_at_once(world, via):
    held, release = threading.Event(), threading.Event()

    def _provision():
        with connection_lock(SID, holder=PROVISION):
            held.set()
            release.wait(30)
    provision = threading.Thread(target=_provision, daemon=True)
    provision.start()
    try:
        assert held.wait(30)
        started = time.monotonic()
        res = _store(via)
        assert time.monotonic() - started < 10
    finally:
        release.set()
        provision.join(30)
    assert res.get("reason") == "connection_busy", res
    assert "being provisioned" in res["error"] and "Nothing was stored" in res["error"], res
    assert world.stored == [] and world.node_writes == []


# ── a rotation of the same secret, and what keeps the old credentials ──────

@pytest.mark.parametrize("via", VIAS)
def test_a_rotation_of_the_same_secret_drops_the_apps_cached_copy(world, via):
    """The node already points at adt/systems/<id>; the store overwrites that
    same secret, so its ARN is unchanged. The app's resolver cached the old
    value under that ARN; the store drops it, so the app's next read gets the
    new token. The onboarding runtime's own copy can stay up to its cache
    lifetime, and the answer says so."""
    world.rotation()
    assert cr.resolve_connection(SID)["token"] == "old-token"      # the app's cache is warm
    res = _store(via)
    assert res["status"] == "stored" and res["secret_arn"] == NEW_ARN, res
    assert cr.resolve_connection(SID)["token"] == "new-token"
    assert world.graph[SID]["secret_arn"] == NEW_ARN
    assert "next minute" in res.get("note", ""), res


@pytest.mark.parametrize("via", VIAS)
def test_a_rotation_whose_publish_fails_says_the_secret_changed_and_the_record_did_not(
        world, via):
    """The secret's value was replaced, and the node already pointed at it:
    the answer must not say the new credentials are not in use."""
    world.rotation()
    assert cr.resolve_connection(SID)["token"] == "old-token"
    world.write_answer = False
    res = _store(via)
    assert res.get("reason") == "pointer_not_saved", res
    error = res["error"]
    assert "written to AWS Secrets Manager" in error and "could not be updated" in error, res
    assert "already points at that secret" in error, res
    assert "not in use" not in error and "still apply" not in error, res
    assert cr.resolve_connection(SID)["token"] == "new-token"     # the app's cache was dropped


@pytest.mark.parametrize("via", VIAS)
def test_a_provisioned_system_is_told_its_agent_keeps_the_old_credentials(world, via):
    res = _store(via)
    assert res["status"] == "stored", res
    assert "provisioned again" in res.get("note", ""), res
    assert "in use" not in res.get("note", ""), res


@pytest.mark.parametrize("via", VIAS)
def test_a_system_not_yet_provisioned_gets_no_agent_note(world, via):
    """A node an Edit connection created before the first provision carries
    connection settings but not the onboarding marker: no agent runs yet."""
    world.graph[SID]["status"] = ""
    res = _store(via)
    assert res["status"] == "stored", res
    assert "provisioned again" not in res.get("note", ""), res


# ── behind the app's integrator/Admin gate ─────────────────────────────────

class _Agent:
    """The onboarding agent, faked: its turn calls the deployed chat's
    store_credentials, as the model would."""

    def __init__(self):
        self.callback_handler = None
        self.hooks = SimpleNamespace(add_hook=lambda hook: None)

    def __call__(self, prompt):
        tool = _by_name(lifecycle.CLOUD_APP_TOOLS)["store_credentials"]
        return json.dumps(tool(SID, json.dumps(_NEW)))


def _chat(monkeypatch, *, is_admin=False, is_integrator=False):
    built: list = []

    def _get_onboard_agent(**kwargs):
        built.append(kwargs)
        return _Agent()
    monkeypatch.setattr(onboard, "get_onboard_agent", _get_onboard_agent)
    resp = TestClient(_app(is_admin=is_admin, is_integrator=is_integrator)).post(
        "/api/onboard/chat", json={"step": "", "system_id": SID,
                                   "message": "store the credentials"})
    return resp, built


def test_a_caller_without_the_integrator_or_admin_role_never_reaches_the_store(
        monkeypatch, world):
    resp, built = _chat(monkeypatch)
    assert resp.status_code == 403, resp.text
    assert built == [], "the onboarding agent was built for a caller the gate refuses"
    form = TestClient(_app(is_integrator=False)).post(
        "/api/onboard/credentials", json={"system_id": SID, "credentials": _NEW})
    assert form.status_code == 403, form.text
    assert world.stored == [] and world.node_writes == []


@pytest.mark.parametrize("role", ["is_integrator", "is_admin"])
def test_an_integrator_or_admin_chat_stores_the_credentials_in_the_app(
        monkeypatch, world, role):
    resp, built = _chat(monkeypatch, **{role: True})
    assert resp.status_code == 200, resp.text
    assert "event: done" in resp.text, resp.text
    assert built == [{"user_access_token": "user-token"}]
    assert world.stored == [(SID, _NEW)]
    assert world.graph[SID]["secret_arn"] == NEW_ARN


def _references(names: set) -> set:
    """(file, innermost enclosing function, name) for every reference to one
    of ``names`` (a call or a value, not its own definition) in agents/."""
    found: set = set()
    agents_dir = Path(oa.__file__).resolve().parent

    class _Visitor(ast.NodeVisitor):
        def __init__(self, path):
            self.path, self.stack = path, ["<module>"]

        def _function(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()
        visit_FunctionDef = visit_AsyncFunctionDef = _function

        def visit_Name(self, node):
            if node.id in names:
                found.add((self.path.name, self.stack[-1], node.id))

        def visit_Attribute(self, node):
            if node.attr in names:
                found.add((self.path.name, self.stack[-1], node.attr))
            self.generic_visit(node)

    for path in agents_dir.rglob("*.py"):
        _Visitor(path).visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_only_the_gated_chat_route_builds_the_onboarding_agent():
    """The gate applies to the chat's tool calls, not only to its HTTP route:
    the agent (and so every tool it calls) is built only by the chat's worker,
    and that worker is started only by the gated POST /api/onboard/chat. A new
    entry point that builds the agent fails here until it is gated too."""
    refs = _references({"get_onboard_agent", "_get_onboard_tools", "_run_onboard_agent"})
    assert refs == {
        ("onboard_agent.py", "get_onboard_agent", "_get_onboard_tools"),
        ("onboard.py", "_run_onboard_agent", "get_onboard_agent"),
        ("onboard.py", "onboard_chat", "_run_onboard_agent"),
    }, refs
    route = next(r for r in onboard.router.routes if getattr(r, "path", "") == "/api/onboard/chat")
    assert route.endpoint is onboard.onboard_chat
    assert onboard.require_onboard_access in [d.dependency for d in onboard.router.dependencies]
