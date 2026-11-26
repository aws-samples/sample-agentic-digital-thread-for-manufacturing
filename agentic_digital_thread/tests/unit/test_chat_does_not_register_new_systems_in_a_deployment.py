# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""In a deployment the onboarding chat does not register new systems.

In a deployment (cloud mode) the chat's tools run in the onboarding runtime,
whose catalog is not the app's: a system the runtime registered is unknown to
the app's provision, and a system the app registered is invisible to the
runtime's discover until it is onboarded. So there the chat sends a new
system to the Onboard view: the gateway's connect_new_system is hidden, the
agent gets the app's own tool of the same name and parameters, which registers
nothing and answers how a new system is added (Connect New System, then the
wizard's Discover, Map, Resolve and Provision), and the agent's prompt says the
same. The app's provision_system answers the same way for a system the app
does not know. Local mode keeps the in-process tools and prompt exactly as
they are: there one catalog serves the whole flow.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest
from mcp.types import Tool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard_agent as oa  # noqa: E402
from tests.unit._mcp_tool_filter import client_keeps  # noqa: E402
from agents.config import get_config  # noqa: E402

_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}
_STEPS = ("Connect New System", "Discover", "Map", "Resolve", "Provision")


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    for var in ("AGENT_MODE", "DEV_MODE", "ONBOARDING_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def _spy_gateway(monkeypatch):
    import agents.gateway_auth as ga

    class _SpyMCPClient:
        def __init__(self, transport_callable, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_tools_sync(self):
            return ["onboarding___discover"]

    monkeypatch.setattr(ga, "MCPClient", _SpyMCPClient)
    monkeypatch.setattr(ga, "streamablehttp_client", lambda url, headers=None: None)
    return _SpyMCPClient


def _cloud(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()


def _local(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()


def _cloud_tools(monkeypatch) -> dict:
    _cloud(monkeypatch)
    spy = _spy_gateway(monkeypatch)
    tools = oa._get_onboard_tools("user-token")
    assert isinstance(tools[0], spy)
    return {getattr(t, "tool_name", "gateway"): t for t in tools}


# ── the tool set ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["onboarding___connect_new_system", "connect_new_system"])
def test_the_gateway_connect_new_system_is_hidden(name):
    client = oa._create_onboarding_gateway_client("https://gw.example.com/mcp", "tok")
    tool = MCPAgentTool(Tool(name=name, inputSchema=_SCHEMA), client)
    assert client_keeps(client, tool) is False


def test_in_a_deployment_the_agent_gets_the_apps_connect_new_system(monkeypatch):
    tools = _cloud_tools(monkeypatch)
    assert "connect_new_system" in tools
    assert tools["connect_new_system"] is not oa.connect_new_system
    assert tools["connect_new_system"].tool_spec["inputSchema"] == \
        oa.connect_new_system.tool_spec["inputSchema"]


def test_local_mode_keeps_the_in_process_connect_new_system(monkeypatch):
    _local(monkeypatch)
    tools = oa._get_onboard_tools(None)
    assert [t.tool_name for t in tools] == [t.tool_name for t in oa.ONBOARD_TOOLS]
    by_name = {t.tool_name: t for t in tools}
    assert by_name["connect_new_system"] is oa.connect_new_system


# ── the app's connect_new_system registers nothing ──────────────────────────

@pytest.fixture
def nothing_written(monkeypatch):
    """Every place a registration is written, watched."""
    import agents.catalog as catalog_mod
    import agents.connection_resolver as cr
    import agents.onboard as onboard
    from agents.catalog import SystemCatalog
    cat = SystemCatalog([])
    for mod in (catalog_mod, onboard, oa):
        monkeypatch.setattr(mod, "catalog", cat)
    writes = MagicMock(side_effect=AssertionError("written"))
    for target, name in ((cr, "_store_connection_secret"), (cr, "write_connection"),
                         (cr, "_neptune_write"), (oa, "neptune_write"), (oa, "_store_secret"),
                         (onboard, "neptune_write")):
        monkeypatch.setattr(target, name, writes)
    store = MagicMock()
    monkeypatch.setattr(oa, "_store", lambda: store)
    return type("W", (), {"cat": cat, "writes": writes, "store": store})


def test_it_registers_nothing_and_says_how_a_new_system_is_added(monkeypatch, nothing_written):
    tool = _cloud_tools(monkeypatch)["connect_new_system"]
    res = tool(name="Line SCADA", system_type="SCADA", plant="Plant A",
               connection_type="MCP Server", connection_string="https://mcp.example.com/mcp",
               connection_config={"token": "Bearer secret-example-token"})
    assert res.get("reason") == "connect_in_onboard_view", res
    text = res["error"]
    for step in _STEPS:
        assert step in text, step
    assert "Nothing was registered" in text
    assert "explain" in res.get("offer", "")
    assert "secret-example-token" not in json.dumps(res)
    assert nothing_written.cat.get_all() == []
    nothing_written.writes.assert_not_called()
    assert nothing_written.store.mock_calls == []


# ── provision_system for a system the app does not know ─────────────────────

def test_provision_of_a_system_the_app_does_not_know_points_to_connect_new_system(monkeypatch):
    from tests.unit.test_chat_provision_is_the_wizard_provision import World
    world = World(monkeypatch)
    store = world.store
    store.create_session("line-new", initiated_by="test", connection_type="MCP Server")
    for prev, nxt in (("discovering", "discovered"), ("discovered", "mapped"),
                      ("mapped", "resolved")):
        store.advance_status("line-new", prev, nxt)
    _local(monkeypatch)
    tools = {t.tool_name: t for t in oa._get_onboard_tools(None)}
    res = tools["provision_system"]("line-new")
    assert res.get("reason") == "connect_in_onboard_view", res
    assert "Unknown system_id" not in res["error"]
    for step in _STEPS:
        assert step in res["error"], step
    assert store.get_session("line-new")["status"] == "resolved"
    assert world.job_bodies == []


def _walk_session(store, sid: str, status: str) -> None:
    """A session of ``sid`` walked to ``status`` through the store's own
    transitions (``""``: no session)."""
    if not status:
        return
    store.create_session(sid, initiated_by="test", connection_type="MCP Server")
    walk = [("discovering", "discovered"), ("discovered", "mapped"), ("mapped", "resolved")]
    for prev, nxt in walk:
        if prev == status:
            return
        store.advance_status(sid, prev, nxt)
    if status == "resolved":
        return
    store.claim_provisioning(sid, provision_job_id="job-1")
    if status != "provisioning":
        store.finalize(sid, status)


def _provision_tool(monkeypatch, mode):
    """The chat agent's provision_system as the mode gives it."""
    if mode == "cloud":
        return _cloud_tools(monkeypatch)["provision_system"]
    _local(monkeypatch)
    return {t.tool_name: t for t in oa._get_onboard_tools(None)}["provision_system"]


@pytest.mark.parametrize("mode", ["cloud", "local"])
@pytest.mark.parametrize("status", ["", "discovered", "provisioning", "failed"],
                         ids=["no-session", "discovered", "provisioning", "failed"])
def test_provision_of_an_unknown_id_points_to_connect_new_system_whatever_its_session(
        monkeypatch, mode, status):
    """An id the app's catalog does not hold is answered before the session
    is read: with no session (a system nobody registered) as with any other."""
    from tests.unit.test_chat_provision_is_the_wizard_provision import World
    world = World(monkeypatch)
    _walk_session(world.store, "not-a-system", status)
    before = {k: dict(v) for k, v in world.table.items.items()}
    res = _provision_tool(monkeypatch, mode)("not-a-system")
    assert res.get("reason") == "connect_in_onboard_view", res
    assert res.get("system_id") == "not-a-system"
    assert "resolve_identities" not in res["error"] and "Unknown system_id" not in res["error"]
    for step in _STEPS:
        assert step in res["error"], step
    assert world.table.items == before, "the session table changed"
    assert world.job_bodies == [] and world.discovers == 0
    import agents.onboard as onboard
    assert not onboard._provision_running("not-a-system")


@pytest.mark.parametrize("mode", ["cloud", "local"])
def test_provision_of_a_registered_system_without_a_session_still_asks_for_resolve(
        monkeypatch, mode):
    from tests.unit.test_chat_provision_is_the_wizard_provision import SID, World
    world = World(monkeypatch)
    assert world.catalog.get(SID) is not None
    res = _provision_tool(monkeypatch, mode)(SID)
    assert res == {"error": "No resolved mappings. Call resolve_identities first."}, res
    assert world.table.items == {} and world.job_bodies == []


# ── the prompt ──────────────────────────────────────────────────────────────

def _prompt_given(monkeypatch, mode) -> str:
    captured: dict = {}
    monkeypatch.setattr(oa, "Agent", lambda **kw: captured.update(kw) or MagicMock())
    monkeypatch.setattr(oa, "_get_onboard_tools", lambda token: [])
    mode(monkeypatch)
    oa.get_onboard_agent(user_access_token="tok")  # nosec B106 - offline placeholder
    return captured["system_prompt"]


def test_in_a_deployment_the_prompt_sends_a_new_system_to_the_onboard_view(monkeypatch):
    prompt = _prompt_given(monkeypatch, _cloud)
    assert "call connect_new_system first, then discover" not in prompt
    assert "guide the user to connect_new_system" not in prompt
    assert "register a new system (PostgreSQL" not in prompt
    assert "Connect New System in the Onboard view" in prompt
    assert "Discover, Map, Resolve and Provision" in prompt
    assert "offer to explain" in prompt
    assert "no systems connected yet" in prompt


def test_local_mode_keeps_its_prompt(monkeypatch):
    prompt = _prompt_given(monkeypatch, _local)
    assert prompt is oa.ONBOARD_AGENT_PROMPT
    assert "call connect_new_system first, then discover" in prompt
    assert "guide the user to connect_new_system" in prompt
