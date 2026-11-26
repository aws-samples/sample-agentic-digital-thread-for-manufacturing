# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator drops the gateway's prefixed copy of its own graph tool.

``_get_orchestrator_tools`` always adds an in-process ``explore_graph``; the
digital-thread gateway target also exposes ``digital-thread___explore_graph``
for external MCP clients (AWS Quick). With both visible the model sometimes
called the gateway copy too (3 of 26 rehearsal turns, 2026-10-03), and the chat
numbers every tool except the BARE ``explore_graph`` (provenance D2) while the
prompt numbers system agents only, so that one call shifted every [N] badge in
the answer by one. The fix is the orchestrator's view only: the gateway catalog,
and D2 for every other client, are unchanged.

Also pins the identifier-miss recovery footnote: a VALUE-MAPPING-PROPOSAL is
filed PENDING (auto-accept defaults off), so the footnote must not say the
system has already learned the mapping.

Offline: no AWS / LLM / gateway / Neptune.
"""
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
from agents import provenance


def _tool(name):
    return SimpleNamespace(tool_name=name)


class _Listing(list):
    def __init__(self, items, pagination_token=None):
        super().__init__(items)
        self.pagination_token = pagination_token


class _FakeGatewayClient:
    def __init__(self, names):
        self._names = names

    async def call_tool_async(self, *args, **kwargs):  # pragma: no cover
        return {"status": "success", "content": []}

    def list_tools_sync(self, *args, **kwargs):
        return _Listing([_tool(n) for n in self._names], pagination_token="tok")  # nosec B106 - offline fixture token


@pytest.mark.parametrize("name", [
    "digital-thread___explore_graph",
    "some-other-target___explore_graph",
    # Hidden too (2026-10-03): Mode A must not hold physical-schema access
    # (ADR-0035) and the call would shift the [N] badges the same way.
    "digital-thread___get_system_schema",
])
def test_prefixed_graph_tool_is_a_local_duplicate(name):
    assert orch._is_local_duplicate_tool(name) is True
    assert orch._is_excluded_gateway_tool(name) is True


@pytest.mark.parametrize("name", [
    "explore_graph",                       # the in-process tool itself
    "get_system_schema",                   # Mode B's in-process tool
    "___explore_graph",                    # no target prefix
    "digital-thread___explore_graph_v2",   # lookalike
    "digital-thread___save_correlation",   # a real gateway tool
    "erp___query_erp",
])
def test_other_tools_are_not_local_duplicates(name):
    assert orch._is_local_duplicate_tool(name) is False


def test_wrap_drops_prefixed_graph_keeps_system_tools_and_token():
    client = _FakeGatewayClient([
        "digital-thread___explore_graph",
        "digital-thread___get_query_workflow",
        "digital-thread___get_system_schema",
        "digital-thread___save_correlation",
        "erp___query_erp",
        "mes___query_mes",
    ])
    listed = orch._wrap_gateway_client_exclude_guidance(client).list_tools_sync()
    assert [t.tool_name for t in listed] == [
        "digital-thread___save_correlation",
        "erp___query_erp",
        "mes___query_mes",
    ]
    assert listed.pagination_token == "tok"  # nosec B105 - offline fixture


def test_d2_is_unchanged_for_other_clients():
    # The client predicate still numbers a prefixed graph call (D2 parity);
    # the fix keeps the orchestrator from MAKING one, not the counter.
    assert provenance.is_client_cited("digital-thread___explore_graph") is True
    assert provenance.is_client_cited("explore_graph") is False


def test_get_orchestrator_tools_has_one_graph_tool(monkeypatch):
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=True, agentcore_gateway_url="https://gw.example.com/mcp"),
    )
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda config, tok: "bearer-token")
    monkeypatch.setattr(
        orch, "_create_mcp_client",
        lambda url, bearer_token=None: _FakeGatewayClient(
            ["digital-thread___explore_graph", "erp___query_erp"]),
    )
    tools = orch._get_orchestrator_tools()
    gateway_names = [t.tool_name for t in tools[-1].list_tools_sync()]
    assert gateway_names == ["erp___query_erp"]
    local_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools[:-1]]
    assert "explore_graph" in local_names


def test_recovery_footnote_does_not_claim_the_mapping_is_learned():
    text = orch._MODE_A_IDENTIFIER_MISS_RECOVERY
    assert "just learned this mapping" not in text
    assert "proposed for review" in text
    assert "once it is accepted" in text
