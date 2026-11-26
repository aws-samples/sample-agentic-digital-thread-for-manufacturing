# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the `onboarding` MCP tool server (Wave 1 Task 1.4).

Mirrors ``test_digital_thread_server.py``. The onboarding tools touch AWS (Secrets
Manager / Bedrock), Neptune, PostgreSQL, and the DynamoDB session store at call
time, but ``tools/list`` never executes a tool body — so these tests need no live
backends and assert the static surface of the server:

  - the server exposes EXACTLY the 8 onboarding tools the deployed chat may call on
    the gateway, with their canonical names + docstrings: the 13 wired
    ``ONBOARD_TOOLS`` without the five the app runs itself in a deployment
    (``get_catalog``, ``provision_system``, ``reset_system``,
    ``connect_new_system``, ``store_credentials``; ``discover`` stays because
    the chat forwards to it). That leaves no commit-tier tool on the gateway.
    Guards F1/F9 too: a defined-but-unwired ``@tool`` (e.g. the deleted
    ``get_field_mappings``) must NOT auto-export as an orphaned extra tool;
  - the exported set is ``onboard_agent.ONBOARD_TOOLS`` (the single source of truth
    shared with the in-process ``get_onboard_agent``) minus
    ``onboard_chat_lifecycle.NOT_ON_GATEWAY`` (derived from the chat's own
    ``APP_TOOL_NAMES``), so the wired agent set, the chat's gateway filter and the
    gateway-exported set cannot drift;
  - the tool names match what the gateway/prompt reference, so the gateway-prefixed
    names (``onboarding___<tool>``) are predictable;
  - input schemas are introspected from the tool signatures (Strands ``@tool``
    objects register cleanly on FastMCP).
"""

import asyncio

import pytest

from agents.onboard_agent import ONBOARD_TOOLS
from agents.onboarding_runtime_server import create_onboarding_server, TARGET_NAME


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    """These tests assert the STATIC tool surface with no backends. ADR-0069
    made cloud construction fail CLOSED on an incomplete contract env — run
    under the documented local-mode gate (AGENT_MODE=local), where the env is
    intentionally minimal and the boot check never raises."""
    monkeypatch.setenv("AGENT_MODE", "local")


# The intended onboarding tool sets, enumerated INDEPENDENTLY of ONBOARD_TOOLS and
# NOT_ON_GATEWAY so the test also guards the source lists against an accidental
# add/drop.
#: Every wired onboarding tool (ONBOARD_TOOLS: 9 explore + 4 commit).
WIRED_TOOLS = {
    # explore tier
    "get_catalog",
    "get_system_info",
    "test_connection",
    "discover",
    "assess_vocabulary_fit",
    "propose_concepts",
    "map_fields",
    "resolve_identities",
    "verify_system",
    # commit tier
    "connect_new_system",
    "store_credentials",
    "provision_system",
    "reset_system",
}
#: The ones the app runs itself in a deployment and never calls on the gateway.
APP_ONLY_TOOLS = {"get_catalog", "connect_new_system", "provision_system", "reset_system",
                  "store_credentials"}
#: What the runtime exports to the gateway.
EXPECTED_TOOLS = WIRED_TOOLS - APP_ONLY_TOOLS


def _list_tools(mcp):
    """Enumerate the server's registered MCP tools (list_tools is async)."""
    return asyncio.run(mcp.list_tools())


def test_target_name_is_onboarding():
    # Drives the gateway tool prefix: onboarding___<tool>.
    assert TARGET_NAME == "onboarding"


def test_server_exposes_exactly_the_gateway_onboarding_tools():
    names = {t.name for t in _list_tools(create_onboarding_server())}
    assert names == EXPECTED_TOOLS
    # F1/F9: exactly 8, no orphaned extra tool auto-exports and none of the
    # tools the app runs itself.
    assert len(names) == 8


def test_exposed_set_equals_the_single_source_of_truth():
    # The server iterates ONBOARD_TOOLS, the same list get_onboard_agent wires,
    # without NOT_ON_GATEWAY, the names the chat's gateway filter is built from,
    # so the gateway-exported set and the in-process agent set cannot drift.
    from agents.onboard_chat_lifecycle import NOT_ON_GATEWAY
    names = {t.name for t in _list_tools(create_onboarding_server())}
    assert names == {t.tool_name for t in ONBOARD_TOOLS} - NOT_ON_GATEWAY
    assert NOT_ON_GATEWAY == APP_ONLY_TOOLS
    assert len(ONBOARD_TOOLS) == 13


def test_tool_descriptions_carry_the_canonical_docstrings():
    tools = {t.name: t for t in _list_tools(create_onboarding_server())}
    # The docstrings ARE the tool descriptions (single source of truth + the text
    # the gateway ranks on). The ordering-gate guidance lives in the docstrings.
    assert "discover first" in (tools["map_fields"].description or "")
    assert "map_fields first" in (tools["resolve_identities"].description or "")
    assert "after discover" in (tools["assess_vocabulary_fit"].description or "")


def test_input_schema_is_introspected_from_the_tool_signature():
    tools = {t.name: t for t in _list_tools(create_onboarding_server())}
    # discover(system_id: str) — the gateway needs the parameter schema.
    discover_props = tools["discover"].inputSchema.get("properties", {})
    assert "system_id" in discover_props


def test_tool_names_match_canonical_function_names():
    # The server registers t.tool_name; the in-process agent wraps the same objects.
    # Keeping these stable is what makes the gateway-prefixed names predictable.
    by_name = {t.tool_name: t for t in ONBOARD_TOOLS}
    assert set(by_name) == WIRED_TOOLS
