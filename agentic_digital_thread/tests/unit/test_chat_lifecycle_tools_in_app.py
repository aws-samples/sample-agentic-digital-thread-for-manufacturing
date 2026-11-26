# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding chat's provision_system and reset_system run in the app.

The chat's agent loop runs in the app (``POST /api/onboard/chat``) under the
user's verified integrator/Admin session. Its tools came from the onboarding
gateway, whose ``provision_system`` started a Step Functions provisioner the
app never heard back from, and whose ``reset_system`` runs in a container
that may not remove a runtime or a gateway target. The app now hides those
two gateway tools (the gateway names them ``onboarding___<tool>``) and gives
the agent its own tools of the same names and parameters, so the prompt and
the tool contract the model sees are unchanged. In a deployment the app also
hides and answers the gateway's ``get_catalog`` and ``discover`` so they read
the app's in-process catalog (which holds registrations the onboarding
runtime's catalog does not), and the gateway's ``store_credentials`` so a
secret is written from the app (test_store_credentials_runs_in_app.py).
Local mode (tools in process) uses the same app provision and reset tools,
so there is one path.

Offline: the MCP client is real (only its tool filter is exercised, no
connection is made) or spied as in test_onboard_agent_gateway_tools.py.
"""
from __future__ import annotations

import pytest
from mcp.types import Tool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import agents.onboard_agent as oa
from tests.unit._mcp_tool_filter import client_keeps  # noqa: E402
from agents.config import get_config

_SCHEMA = {"type": "object", "properties": {"system_id": {"type": "string"}},
           "required": ["system_id"]}


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    for var in ("AGENT_MODE", "DEV_MODE", "ONBOARDING_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def _names(tools) -> list:
    return [getattr(t, "tool_name", None) for t in tools]


def _gateway_tool(client, name: str) -> MCPAgentTool:
    return MCPAgentTool(Tool(name=name, inputSchema=_SCHEMA), client)


def _local_tool(monkeypatch, name: str):
    """The tool of that name the chat's agent gets in local mode."""
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    return dict(zip(_names(oa._get_onboard_tools(None)), oa._get_onboard_tools(None)))[name]


# ── the gateway's two tools are hidden from the chat's agent ───────────────

@pytest.mark.parametrize("name", ["onboarding___provision_system", "onboarding___reset_system",
                                  "onboarding___get_catalog", "onboarding___discover",
                                  "onboarding___store_credentials",
                                  "provision_system", "reset_system", "get_catalog", "discover",
                                  "store_credentials"])
def test_the_gateway_provision_and_reset_are_hidden(name):
    client = oa._create_onboarding_gateway_client("https://gw.example.com/mcp", "tok")
    assert client_keeps(client, _gateway_tool(client, name)) is False


@pytest.mark.parametrize("name", ["onboarding___map_fields",
                                  "onboarding___resolve_identities", "onboarding___verify_system",
                                  "onboarding___test_connection",
                                  "onboarding___provision_system_status"])
def test_every_other_gateway_tool_stays(name):
    client = oa._create_onboarding_gateway_client("https://gw.example.com/mcp", "tok")
    assert client_keeps(client, _gateway_tool(client, name)) is True


def _spy_gateway(monkeypatch):
    import agents.gateway_auth as ga
    built: list = []

    class _SpyMCPClient:
        def __init__(self, transport_callable, **kwargs):
            built.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_tools_sync(self):
            return ["onboarding___discover"]

    monkeypatch.setattr(ga, "MCPClient", _SpyMCPClient)
    monkeypatch.setattr(ga, "streamablehttp_client", lambda url, headers=None: None)
    return built, _SpyMCPClient


def test_in_cloud_mode_the_agent_gets_the_filtered_gateway_and_the_app_tools(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    built, spy = _spy_gateway(monkeypatch)
    tools = oa._get_onboard_tools("user-token")
    assert isinstance(tools[0], spy)
    assert _names(tools[1:]) == ["get_catalog", "discover", "provision_system",
                                 "reset_system", "connect_new_system", "store_credentials"]
    assert tools[3] is not oa.provision_system and tools[4] is not oa.reset_system
    import agents.onboard_chat_lifecycle as lifecycle
    assert tools[1:] == lifecycle.CLOUD_APP_TOOLS
    assert built[-1].get("tool_filters") == lifecycle.GATEWAY_TOOL_FILTERS


def test_in_local_mode_the_agent_gets_the_app_tools_in_place_of_the_runtime_ones(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    tools = oa._get_onboard_tools(None)
    assert _names(tools) == _names(oa.ONBOARD_TOOLS)
    by_name = dict(zip(_names(tools), tools))
    assert by_name["provision_system"] is not oa.provision_system
    assert by_name["reset_system"] is not oa.reset_system
    import agents.onboard_chat_lifecycle as lifecycle
    assert by_name["provision_system"] is lifecycle.provision_system
    assert by_name["reset_system"] is lifecycle.reset_system


# ── same names and parameters as the tools the model knew ──────────────────

@pytest.mark.parametrize("name", ["provision_system", "reset_system"])
def test_the_app_tools_keep_the_runtime_tools_name_and_parameters(monkeypatch, name):
    app_tool = _local_tool(monkeypatch, name)
    runtime_tool = getattr(oa, name)
    assert app_tool.tool_name == runtime_tool.tool_name == name
    assert app_tool.tool_spec["inputSchema"] == runtime_tool.tool_spec["inputSchema"]


def test_the_app_provision_tool_does_not_describe_the_step_functions_path(monkeypatch):
    description = _local_tool(monkeypatch, "provision_system").tool_spec["description"]
    for stale in ("Step Functions", "state machine", "provisioner", "admin UI polls"):
        assert stale not in description, stale
    assert "again" in description  # how the model checks on a provision it started


def test_the_app_reset_tool_does_not_describe_the_runtime_hand_off(monkeypatch):
    description = _local_tool(monkeypatch, "reset_system").tool_spec["description"]
    assert "runtime container" not in description
    assert "names the action" not in description


def test_the_chat_prompt_no_longer_says_reset_hands_off_to_the_onboard_view():
    assert "handed to the Onboard view" not in oa.ONBOARD_AGENT_PROMPT
