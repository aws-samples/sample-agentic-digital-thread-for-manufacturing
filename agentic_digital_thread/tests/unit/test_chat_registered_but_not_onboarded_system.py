# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""In a deployment the onboarding chat gets a system that is registered but not
onboarded right.

Seen live on a rehearsal account (2026-10-11): HighByte was removed from its
card KEEPING its registration — the app's catalog still holds it as
'registered', its onboarding session is gone. The chat was asked to onboard
it. The gateway's get_catalog
(the runtime's catalog, onboarded systems only) did not list it and the
gateway's discover answered "System highbyte not found", so the model told the
user it "isn't a registered system in this deployment" and to add it with
Connect New System in the Onboard view — wrong: it IS registered, and Connect
New System for that id is refused ("already exists"). The right answer is: it's
registered here, the chat can't onboard it in this deployment, onboard it with
the wizard's steps on its card (Discover, Map, Resolve, Provision).

The fix makes the chat answer get_catalog and discover itself in a deployment
(the pattern connect_new_system / provision_system / reset_system already use),
reading the app's in-process catalog — so the tool OUTPUT is correct:

  * get_catalog reports the app catalog's statuses (lists the registered id).
  * discover answers a registered-but-not-onboarded id with "onboard it with the
    wizard's steps on its card", never Connect New System;
  * an onboarded / in-progress id gets the SAME session-gate answer as before
    (its next step), unchanged;
  * an id the app does not know still gets Connect New System;
  * local mode keeps the in-process get_catalog / discover exactly as they are.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard_agent as oa  # noqa: E402
from tests.unit._mcp_tool_filter import client_keeps  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from agents.config import get_config  # noqa: E402
from agents.onboarding_session import OnboardingSessionStore, SessionNotFoundError  # noqa: E402

_STEPS = ("Discover", "Map", "Resolve", "Provision")


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    for var in ("AGENT_MODE", "DEV_MODE", "ONBOARDING_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def _bind_runtime_discover(client):
    """Call ``onboard_chat_lifecycle.bind_runtime_discover`` only when the
    module has it. On a build that predates the API this file still runs (the
    behavioural assertions fail on the wrong RESULT, not on a missing API)."""
    import agents.onboard_chat_lifecycle as lifecycle
    bind = getattr(lifecycle, "bind_runtime_discover", None)
    if bind is not None:
        bind(client)


@pytest.fixture(autouse=True)
def _reset_runtime_discover():
    """The chat's discover forwards the runtime-owned cases through a bound
    gateway client; unbind it after each test so no binding leaks. A no-op on
    a revision that predates the API."""
    yield
    _bind_runtime_discover(None)


class _ClientFromFn:
    """A stand-in for the onboarding gateway MCPClient: its ``call_tool_sync``
    records (tool_use_id, name, arguments) and returns what ``fn(system_id)``
    gives, so a test can assert the forward's name/arguments and that its
    result is returned unchanged."""

    def __init__(self, fn):
        self._fn = fn
        self.calls: list = []

    def call_tool_sync(self, tool_use_id, name, arguments=None):
        self.calls.append((tool_use_id, name, dict(arguments or {})))
        return self._fn((arguments or {}).get("system_id"))


# ── a session store with just the reads the chat's discover makes ────────────

class _FakeStore:
    """Only ``get_session`` is exercised by the chat's discover gate."""

    def __init__(self, sessions: dict | None = None):
        self._sessions = dict(sessions or {})

    def get_session(self, system_id: str) -> dict:
        try:
            return self._sessions[system_id]
        except KeyError:
            raise SessionNotFoundError(system_id)


def _world(monkeypatch, systems: list[dict], sessions: dict | None = None,
           runtime_client=None) -> SystemCatalog:
    """A fresh app catalog + session store wired into both modules the chat's
    app-run tools read, plus the graph reads stubbed so no removal-marker read
    dials AWS. ``runtime_discover`` (when given) is bound as the forward path
    the chat's discover uses for a case the runtime owns (onboarded, or any
    non-'registered' status, with no session)."""
    import agents.onboard as onboard
    cat = SystemCatalog(systems)
    for mod in (onboard, oa):
        monkeypatch.setattr(mod, "catalog", cat)
    monkeypatch.setattr(oa, "_store", lambda: _FakeStore(sessions))
    monkeypatch.setattr(oa, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [])
    # The removal-marker read a start-over hint / removal gate might do.
    monkeypatch.setattr(oa, "_removal_marker_row", lambda sid: {})
    _bind_runtime_discover(runtime_client)
    return cat


def _spy_gateway(monkeypatch, tool_names=("onboarding___discover", "onboarding___get_catalog")):
    import agents.gateway_auth as ga

    class _SpyMCPClient:
        def __init__(self, transport_callable, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_tools_sync(self):
            return list(tool_names)

    monkeypatch.setattr(ga, "MCPClient", _SpyMCPClient)
    monkeypatch.setattr(ga, "streamablehttp_client", lambda url, headers=None: None)
    return _SpyMCPClient


def _cloud_tools(monkeypatch) -> dict:
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    spy = _spy_gateway(monkeypatch)
    tools = oa._get_onboard_tools("user-token")
    assert isinstance(tools[0], spy)
    return {getattr(t, "tool_name", "gateway"): t for t in tools}


def _local_tools(monkeypatch) -> dict:
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    return {t.tool_name: t for t in oa._get_onboard_tools(None)}


def _registered_entry(sid="highbyte"):
    return {"id": sid, "name": "HighByte Intelligence Hub", "system_type": "Historian",
            "plant": "Plant A", "connection_type": "MCP Server",
            "connection_string": "", "schema": "", "status": "registered"}


def _onboarded_entry(sid="sitewise-direct"):
    return {"id": sid, "name": "AWS IoT SiteWise", "system_type": "Historian",
            "plant": "Plant A", "connection_type": "SiteWise (Direct)",
            "connection_string": "", "schema": "", "status": "onboarded"}


# ── the gateway's get_catalog / discover are hidden; the app answers them ────

@pytest.mark.parametrize("name", ["get_catalog", "discover",
                                  "onboarding___get_catalog", "onboarding___discover"])
def test_the_gateway_get_catalog_and_discover_are_hidden(name):
    from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
    from mcp.types import Tool
    client = oa._create_onboarding_gateway_client("https://gw.example.com/mcp", "tok")
    schema = {"type": "object", "properties": {"system_id": {"type": "string"}}}
    assert client_keeps(client,
        MCPAgentTool(Tool(name=name, inputSchema=schema), client)) is False


def test_in_a_deployment_the_agent_gets_the_apps_get_catalog_and_discover(monkeypatch):
    tools = _cloud_tools(monkeypatch)
    for name in ("get_catalog", "discover"):
        assert name in tools, name
        assert tools[name] is not getattr(oa, name)  # the app's own, not the in-process tool


# ── get_catalog reports the app catalog's statuses (lists the registered id) ─

def test_get_catalog_lists_a_registered_not_onboarded_system(monkeypatch):
    _world(monkeypatch, [_registered_entry(), _onboarded_entry()])
    res = _cloud_tools(monkeypatch)["get_catalog"]()
    by_id = {s["id"]: s for s in res["systems"]}
    assert by_id["highbyte"]["status"] == "registered"
    assert by_id["sitewise-direct"]["status"] == "onboarded"


# ── discover of a registered-but-not-onboarded id: onboard on its card ───────

def test_discover_of_a_registered_not_onboarded_system_points_to_its_card(monkeypatch):
    """The live case: registered, no onboarding session. The answer must say it
    is registered and to onboard it with the wizard's steps on its card — never
    'not found', never Connect New System."""
    _world(monkeypatch, [_registered_entry()])  # no session for highbyte
    res = _cloud_tools(monkeypatch)["discover"]("highbyte")
    text = res.get("error", "") + " " + res.get("message", "")
    assert res.get("reason") == "registered_not_onboarded", res
    assert res.get("system_id") == "highbyte"
    assert "not found" not in text.lower()
    # It does not SEND the user to Connect New System (that is only for an id
    # the app does not know); it tells them it is already registered.
    assert res.get("reason") != "connect_in_onboard_view"
    assert "Connect New System" in text and "already registered" in text.lower()
    assert "registered" in text.lower()
    assert "\u2014" not in text and "\u2013" not in text   # no em/en dashes
    for step in _STEPS:
        assert step in text, step


# ── discover of an onboarded / in-progress id: the same gate answer as before ─

def test_discover_of_an_onboarded_system_is_the_session_gate_answer(monkeypatch):
    _world(monkeypatch, [_onboarded_entry()],
           sessions={"sitewise-direct": {"status": "onboarded"}})
    res = _cloud_tools(monkeypatch)["discover"]("sitewise-direct")
    # The existing discover gate's onboarded answer — unchanged, not the new one.
    assert res.get("reason") != "registered_not_onboarded", res
    assert "already has an onboarding session" in res.get("error", "")


def test_discover_of_an_in_progress_id_names_its_next_step(monkeypatch):
    _world(monkeypatch, [_registered_entry("line-x")],
           sessions={"line-x": {"status": "resolved"}})
    res = _cloud_tools(monkeypatch)["discover"]("line-x")
    assert res.get("reason") != "registered_not_onboarded", res
    assert "provision_system" in res.get("error", "")


# ── discover of an id the app does not know: Connect New System ──────────────

def test_discover_of_an_unknown_id_points_to_connect_new_system(monkeypatch):
    _world(monkeypatch, [])  # app knows nothing
    res = _cloud_tools(monkeypatch)["discover"]("nope")
    assert res.get("reason") == "connect_in_onboard_view", res
    assert res.get("system_id") == "nope"
    assert "Connect New System" in res["error"]


# ── local mode keeps the in-process get_catalog / discover ───────────────────

# ── a system the runtime owns (no session) forwards to the runtime ───────────

def _ok(system_id):
    """A gateway discover success result (MCPToolResult shape)."""
    return {"toolUseId": "x", "status": "success",
            "content": [{"text": f'{{"system_id": "{system_id}", "tables": []}}'}]}


def test_onboarded_without_a_session_forwards_to_the_runtime_discover(monkeypatch):
    """REGRESSION: an ONBOARDED system can have no onboarding
    session (seeded / provisioner-onboarded, or the session was cleared). Its
    chat discover must run the runtime's discover, not be told it is 'not yet
    onboarded'. The forward is invoked with the same system_id and its result
    is returned unchanged."""
    _world(monkeypatch, [_onboarded_entry("chat-scratch-mcp-4")], sessions={})  # no session
    discover = _cloud_tools(monkeypatch)["discover"]
    client = _ClientFromFn(_ok)
    _bind_runtime_discover(client)                         # bind after the tool set is built
    res = discover("chat-scratch-mcp-4")
    assert res == _ok("chat-scratch-mcp-4"), res          # the runtime result, unchanged
    assert len(client.calls) == 1
    _tuid, name, args = client.calls[0]
    assert name == "onboarding___discover"
    assert args == {"system_id": "chat-scratch-mcp-4"}
    assert res.get("reason") != "registered_not_onboarded"


def test_a_non_registered_non_onboarded_status_without_a_session_forwards(monkeypatch):
    """Any status other than exactly 'registered' (with no session) is the
    runtime's to answer: forwarded, not given registered_not_onboarded."""
    entry = _registered_entry("odd-x")
    entry["status"] = "provisioning"  # not 'registered', not onboarded
    _world(monkeypatch, [entry], sessions={})
    discover = _cloud_tools(monkeypatch)["discover"]
    client = _ClientFromFn(_ok)
    _bind_runtime_discover(client)
    res = discover("odd-x")
    assert res == _ok("odd-x"), res
    assert [c[1] for c in client.calls] == ["onboarding___discover"]
    assert res.get("reason") != "registered_not_onboarded"


def test_only_registered_plus_no_session_gets_registered_not_onboarded(monkeypatch):
    """A 'registered' entry with no session is NOT forwarded; it gets the
    registered_not_onboarded answer and the runtime is never called."""
    _world(monkeypatch, [_registered_entry("hb")], sessions={})
    discover = _cloud_tools(monkeypatch)["discover"]
    client = _ClientFromFn(_ok)
    _bind_runtime_discover(client)
    res = discover("hb")
    assert res.get("reason") == "registered_not_onboarded", res
    assert client.calls == []                              # the runtime was never called


def test_registered_not_onboarded_text_has_no_em_dash():
    """User-facing strings must not use em or en dashes."""
    import agents.onboard_chat_lifecycle as lifecycle
    text = " ".join(str(v) for v in lifecycle.registered_not_onboarded("hb").values()
                     if isinstance(v, str))
    assert "\u2014" not in text and "\u2013" not in text, text


def test_local_mode_keeps_the_in_process_get_catalog_and_discover(monkeypatch):
    tools = _local_tools(monkeypatch)
    assert tools["get_catalog"] is oa.get_catalog
    assert tools["discover"] is oa.discover
    assert [t.tool_name for t in oa._get_onboard_tools(None)] == \
        [t.tool_name for t in oa.ONBOARD_TOOLS]
    assert [t.tool_name for t in oa._get_onboard_tools(None)] == \
        [t.tool_name for t in oa.ONBOARD_TOOLS]
