# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding runtime does not export the tools the app runs itself.

The onboarding gateway (CUSTOM_JWT) accepts the app's tokens with no group
check, and no per-tool policy is attached yet. The runtime behind it
exported every ``ONBOARD_TOOLS`` entry, so a client holding any app token
could call ``connect_new_system``, ``provision_system``, ``reset_system``,
``get_catalog`` or ``store_credentials`` on the gateway directly, past the
app's integrator/Admin check. The runtime's ``provision_system`` also starts
the old Step Functions provisioner, and its ``store_credentials`` writes a
system's secret under the runtime's role.

The deployed chat never calls those five on the gateway: the app answers
them itself (``onboard_chat_lifecycle``) and its gateway client hides the
gateway's (``GATEWAY_TOOL_FILTERS``). ``discover`` is the app's too, but the
app forwards a case the runtime owns to the gateway's ``discover``
(``_forward_discover``), so it stays exported.

The exported set comes from the same names the chat's filter uses, so the
two cannot drift. Local mode keeps the full in-process list.

Offline: ``tools/list`` runs no tool body, and the gateway client is never
connected (only its tool filter is exercised).
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.types import Tool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import agents.onboard_agent as oa
from tests.unit._mcp_tool_filter import client_keeps  # noqa: E402
import agents.onboard_chat_lifecycle as lifecycle
from agents.config import get_config
from agents.onboarding_runtime_server import TARGET_NAME, create_onboarding_server

#: The tools the app runs itself and never calls on the gateway, enumerated
#: here independently of the code so a change to either side shows.
APP_ONLY = {"get_catalog", "provision_system", "reset_system", "connect_new_system",
            "store_credentials"}

_SCHEMA = {"type": "object", "properties": {"system_id": {"type": "string"}},
           "required": ["system_id"]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Construct the server under the local-mode gate (its cloud boot check
    needs a full contract env, ADR-0069); the chat-side tests set their own
    mode."""
    for var in ("DEV_MODE", "ONBOARDING_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def _exported() -> list:
    """The tool names the onboarding runtime serves on ``tools/list``, in order."""
    return [t.name for t in asyncio.run(create_onboarding_server().list_tools())]


def _onboard_names() -> list:
    return [t.tool_name for t in oa.ONBOARD_TOOLS]


def test_the_runtime_exports_onboard_tools_without_the_ones_the_app_runs_itself():
    """Exported == ONBOARD_TOOLS - (APP_TOOL_NAMES - {discover}), order kept."""
    app_only = lifecycle.APP_TOOL_NAMES - {"discover"}
    assert _exported() == [n for n in _onboard_names() if n not in app_only]


@pytest.mark.parametrize("name", sorted(APP_ONLY))
def test_a_tool_the_app_runs_itself_is_not_on_the_gateway(name):
    assert name in _onboard_names()  # still a wired in-process tool
    assert name not in _exported()


def test_the_exported_set_and_the_chats_filter_share_one_source():
    """The runtime's list is derived from the chat's own tool names, so adding
    a tool the app runs itself hides it from the chat AND drops it from the
    gateway in one place."""
    from agents.onboarding_runtime_server import RUNTIME_TOOLS
    assert lifecycle.FORWARDED_TO_GATEWAY == {"discover"}
    assert lifecycle.NOT_ON_GATEWAY == lifecycle.APP_TOOL_NAMES - lifecycle.FORWARDED_TO_GATEWAY
    assert lifecycle.NOT_ON_GATEWAY == APP_ONLY
    assert [t.tool_name for t in RUNTIME_TOOLS] == _exported()
    assert set(_exported()) == set(_onboard_names()) - lifecycle.NOT_ON_GATEWAY


def test_the_gateway_serves_what_the_chat_takes_from_it_and_the_forwarded_discover():
    """The chat's filtered gateway set is a subset of the exported set; with
    the app's own tools it is the whole ONBOARD_TOOLS list; and the only app
    tool the gateway still serves is the discover the chat forwards to."""
    exported = _exported()
    client = oa._create_onboarding_gateway_client("https://gw.example.com/mcp", "tok")
    kept = {n for n in exported if client_keeps(client,
        MCPAgentTool(Tool(name=f"{TARGET_NAME}___{n}", inputSchema=_SCHEMA), client))}
    app_tools = {t.tool_name for t in lifecycle.CLOUD_APP_TOOLS}
    assert kept <= set(exported)
    assert kept.isdisjoint(app_tools)
    assert kept | app_tools == set(_onboard_names())

    calls: list = []

    class _Gateway:
        def call_tool_sync(self, tool_use_id, name, arguments):
            calls.append(name)
            return {"status": "success", "content": []}

    lifecycle.bind_runtime_discover(_Gateway())
    try:
        lifecycle._forward_discover("system-a")
    finally:
        lifecycle.bind_runtime_discover(None)
    target, _, tool = calls[0].rpartition("___")
    assert target == TARGET_NAME and tool in exported
    assert set(exported) - kept == {tool} == {"discover"}


def test_local_mode_keeps_the_full_in_process_list_while_the_runtime_exports_less():
    tools = oa._get_onboard_tools(None)
    assert [t.tool_name for t in tools] == _onboard_names()
    assert set(_onboard_names()) - set(_exported()) == APP_ONLY
