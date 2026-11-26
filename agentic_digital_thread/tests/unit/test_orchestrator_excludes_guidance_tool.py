# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator EXCLUDES the guidance-only gateway tools from its own assembled
toolset while leaving them on the gateway for external MCP clients.

The digital-thread gateway target exposes ``get_query_workflow`` (a pure, read-only
tool returning the deployed-mode workflow) and ``get_client_instructions`` (the full
mode-matched client setup prompt) so an external client like AWS Quick can discover
the graph-first workflow in-session. OUR orchestrator already embeds that workflow in
its system prompt, so calling either tool would be a wasted round-trip — both must be
dropped from the toolset THIS agent assembles, without touching the gateway catalog
(a separate client still sees them).

Offline: no AWS / LLM / gateway / Neptune. We stub the gateway MCP client's tool
listing (including bare + gateway-prefixed guidance entries) and assert the filtering
seam drops only the guidance tools and keeps the real tools.
"""
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
from agents.digital_thread_server import GUIDANCE_TOOL_NAME, GUIDANCE_TOOL_NAMES

# Both guidance-only tool names, as a stable sorted tuple for parametrization.
_GUIDANCE_NAMES = tuple(sorted(GUIDANCE_TOOL_NAMES))


# --------------------------------------------------------------------------- #
# Synthetic tool-listing stubs (no dataset literals).
# --------------------------------------------------------------------------- #
def _tool(name):
    """A minimal MCPAgentTool stand-in: only ``tool_name`` is read by the filter."""
    return SimpleNamespace(tool_name=name)


class _PaginatedListStub(list):
    """A list subclass mirroring Strands' PaginatedList (carries a pagination token).

    The filter must preserve this object (and its token) so the ToolProvider's
    pagination loop is unaffected.
    """

    def __init__(self, items, pagination_token=None):
        super().__init__(items)
        self.pagination_token = pagination_token


class _FakeGatewayClient:
    """Stands in for the Strands MCPClient the orchestrator wraps.

    Exposes the two bound methods the wrappers rebind: ``call_tool_async`` (retry
    wrapper) and ``list_tools_sync`` (exclusion wrapper).
    """

    def __init__(self, tools, pagination_token=None):
        self._tools = tools
        self._pagination_token = pagination_token

    async def call_tool_async(self, *args, **kwargs):  # pragma: no cover - not exercised here
        return {"status": "success", "content": []}

    def list_tools_sync(self, *args, **kwargs):
        return _PaginatedListStub(list(self._tools), pagination_token=self._pagination_token)


# Real tools (bare + gateway-prefixed) that MUST survive the filter. The
# gateway-prefixed explore_graph and get_system_schema are NOT here: the
# orchestrator drops both (test_orchestrator_drops_gateway_graph_dup).
_REAL_NAMES = [
    "explore_graph",
    "get_system_schema",
    "run_query",
    "save_value_mapping",
    "save_correlation",
    "digital-thread___save_correlation",
    "erp___run_query",
]

# Every guidance entry (bare + gateway-prefixed) that MUST be dropped.
_GUIDANCE_ENTRIES = [
    entry
    for name in _GUIDANCE_NAMES
    for entry in (name, f"digital-thread___{name}")
]


# --------------------------------------------------------------------------- #
# The guidance name set itself: both tools, singular alias intact.
# --------------------------------------------------------------------------- #
def test_guidance_tool_names_cover_both_tools():
    assert GUIDANCE_TOOL_NAMES == frozenset({
        "get_query_workflow",
        "get_client_instructions",
    })
    # Deprecated singular alias still points at the workflow tool.
    assert GUIDANCE_TOOL_NAME == "get_query_workflow"


# --------------------------------------------------------------------------- #
# _is_excluded_guidance_tool: bare + gateway-prefixed match, real tools do not.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", _GUIDANCE_NAMES)
def test_matches_bare_guidance_tools(name):
    assert orch._is_excluded_guidance_tool(name) is True


@pytest.mark.parametrize("name", _GUIDANCE_NAMES)
def test_matches_gateway_prefixed_guidance_tools(name):
    assert orch._is_excluded_guidance_tool(f"digital-thread___{name}") is True


@pytest.mark.parametrize("name", _REAL_NAMES)
def test_does_not_match_real_tools(name):
    assert orch._is_excluded_guidance_tool(name) is False


@pytest.mark.parametrize("name", _GUIDANCE_NAMES)
def test_does_not_match_lookalike_substring(name):
    # A tool whose name merely CONTAINS a guidance name as a substring (not the
    # final ``___`` segment) must NOT be excluded.
    assert orch._is_excluded_guidance_tool(f"{name}_v2") is False


# --------------------------------------------------------------------------- #
# _wrap_gateway_client_exclude_guidance: filters the client's tool listing.
# --------------------------------------------------------------------------- #
def test_wrap_excludes_bare_and_prefixed_guidance_keeps_real():
    tools = [_tool(n) for n in _REAL_NAMES] + [_tool(n) for n in _GUIDANCE_ENTRIES]
    client = _FakeGatewayClient(tools)

    wrapped = orch._wrap_gateway_client_exclude_guidance(client)
    listed = wrapped.list_tools_sync()
    names = {t.tool_name for t in listed}

    for entry in _GUIDANCE_ENTRIES:
        assert entry not in names
    assert names == set(_REAL_NAMES)


@pytest.mark.parametrize("guidance_name", _GUIDANCE_NAMES)
def test_wrap_preserves_paginated_list_and_token(guidance_name):
    tools = [_tool("explore_graph"), _tool(guidance_name)]
    client = _FakeGatewayClient(tools, pagination_token="next-page")  # nosec B106 - offline test fixture/placeholder credential, not a live secret

    wrapped = orch._wrap_gateway_client_exclude_guidance(client)
    listed = wrapped.list_tools_sync()

    # Return type still exposes the pagination token so load_tools() can paginate.
    assert listed.pagination_token == "next-page"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    assert [t.tool_name for t in listed] == ["explore_graph"]


def test_wrap_is_noop_when_guidance_tools_absent():
    tools = [_tool("explore_graph"), _tool("run_query")]
    client = _FakeGatewayClient(tools)

    wrapped = orch._wrap_gateway_client_exclude_guidance(client)
    names = [t.tool_name for t in wrapped.list_tools_sync()]

    assert names == ["explore_graph", "run_query"]


# --------------------------------------------------------------------------- #
# End-to-end through _get_orchestrator_tools (gateway mode): the appended gateway
# client's tool listing excludes BOTH guidance tools but keeps the real tools.
# --------------------------------------------------------------------------- #
def test_get_orchestrator_tools_excludes_guidance_from_gateway_client(monkeypatch):
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=True, agentcore_gateway_url="https://gw.example.com/mcp"),
    )
    # Avoid real token minting and real MCP transport.
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda config, tok: "bearer-token")
    gateway_tools = [_tool(n) for n in _REAL_NAMES] + [_tool(n) for n in _GUIDANCE_ENTRIES]
    monkeypatch.setattr(
        orch, "_create_mcp_client",
        lambda url, bearer_token=None: _FakeGatewayClient(gateway_tools),
    )

    tools = orch._get_orchestrator_tools()

    # The gateway client is the last entry appended in gateway mode.
    gateway_client = tools[-1]
    names = {t.tool_name for t in gateway_client.list_tools_sync()}

    for entry in _GUIDANCE_ENTRIES:
        assert entry not in names
    assert names == set(_REAL_NAMES)
