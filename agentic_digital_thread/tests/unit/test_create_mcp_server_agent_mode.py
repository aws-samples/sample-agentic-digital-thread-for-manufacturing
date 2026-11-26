# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1A acceptance — `create_mcp_server` collapses to Mode A (ADR-0035).

In the default ``agent`` mode, EVERY connection type must flow through
``create_system_agent`` and expose EXACTLY ONE gateway tool, ``query_<safe_id>``.
The raw-tool early-return branches (SiteWise Direct / i3X / REST / Athena) and the
PostgreSQL ``execute_sql_<id>`` fast path are gone — no raw tool may appear on the
server surface. In ``orchestrator`` mode (Mode B), the server exposes only the
three generic, connection-type-agnostic tools (``describe_schema`` / ``run_query``
/ ``connect``) and does NOT wrap a per-system agent.

Fully offline: ``create_system_agent`` is monkeypatched to a sentinel so no
Bedrock model, AWS client, or DB connection is constructed — the assertions are
purely about the FastMCP tool surface.
"""
import asyncio

import pytest

import agents.system_agent as sa


# The six connection types the dispatch must cover.
CONNECTION_TYPES = [
    "Athena",
    "SiteWise (Direct)",
    "SiteWise (i3X)",
    "REST API",
    "PostgreSQL",
    "MCP Server",
]

# Substrings that identify a raw/structured tool that must NOT reach the surface.
_RAW_TOOL_MARKERS = ("execute_sql", "athena_run_query", "athena_", "sitewise_", "i3x")


def _tool_names(mcp):
    """Enumerate the server's registered MCP tool names (list_tools is async)."""
    return {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.fixture
def stub_system_agent(monkeypatch):
    """Replace create_system_agent so no real agent/AWS/DB is built."""
    def _fake(*args, **kwargs):
        return object()  # a callable-free sentinel is fine; query tool is never invoked

    monkeypatch.setattr(sa, "create_system_agent", _fake)


def _build(connection_type):
    """Build a server for one connection type with inert config.

    ``conn_params`` is passed for PostgreSQL so the tail does not attempt to
    resolve credentials from a secret / ``get_conn_params`` (kept offline).
    """
    return sa.create_mcp_server(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt="",
        tables_desc="",
        conn_params={"host": "h", "dbname": "d"},
        connection_type=connection_type,
        connection_config={},
    )


@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
def test_agent_mode_exposes_exactly_one_query_tool(monkeypatch, stub_system_agent, connection_type):
    # Default mode is "agent"; be explicit so the test is independent of ambient env.
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")

    names = _tool_names(_build(connection_type))

    assert names == {"query_sys_a"}, (
        f"{connection_type}: expected exactly one query tool, got {names}"
    )


@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
def test_agent_mode_exposes_no_raw_tools(monkeypatch, stub_system_agent, connection_type):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")

    names = _tool_names(_build(connection_type))

    for name in names:
        assert not any(marker in name for marker in _RAW_TOOL_MARKERS), (
            f"{connection_type}: raw tool leaked onto the surface: {name}"
        )


def test_agent_mode_is_the_default(monkeypatch, stub_system_agent):
    # With SYSTEM_QUERY_MODE unset, behavior is Mode A.
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)

    names = _tool_names(_build("PostgreSQL"))

    assert names == {"query_sys_a"}


@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
def test_orchestrator_mode_exposes_only_generic_tools(monkeypatch, stub_system_agent, connection_type):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")

    names = _tool_names(_build(connection_type))

    assert names == {"describe_schema", "run_query", "connect"}, (
        f"{connection_type}: Mode B must expose only the three generic tools, got {names}"
    )


def test_orchestrator_mode_does_not_build_a_system_agent(monkeypatch):
    """Mode B must NOT wrap a per-system agent — create_system_agent stays uncalled."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")

    calls = []
    monkeypatch.setattr(sa, "create_system_agent", lambda *a, **k: calls.append(1))

    _build("PostgreSQL")

    assert calls == [], "orchestrator mode must not call create_system_agent"


def test_tool_name_prefix_is_preserved_in_agent_mode(monkeypatch, stub_system_agent):
    """Local mode emulates the gateway '<id>___' prefix — it must reach the tool name."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")

    mcp = sa.create_mcp_server(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt="",
        conn_params={"host": "h", "dbname": "d"},
        connection_type="PostgreSQL",
        connection_config={},
        tool_name_prefix="sys-a___",
    )

    assert _tool_names(mcp) == {"sys-a___query_sys_a"}
