# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Mode-B-only cached ``get_system_schema`` orchestrator tool and its prompt
guidance. The in-process tool shares the CANONICAL name with the `digital-thread`
gateway target (graph_tools.get_system_schema) so an AWS Quick MCP client mimics
the orchestrator. Mode A's toolset/prompt stay byte-identical (the ADR-0035 strip is
intact). Offline — no AWS/LLM/gateway. Synthetic data only.
"""
import json
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
import agents.schema_cache as schema_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    schema_cache.reset()
    orch._mcp_clients.clear()
    # Local-mode config so _get_orchestrator_tools never builds a gateway client.
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=False, agentcore_gateway_url=None),
    )
    yield
    schema_cache.reset()


# --------------------------------------------------------------------------- #
# Prompt guidance — present in Mode B, ABSENT in Mode A (byte-identical Mode A)
# --------------------------------------------------------------------------- #
def test_mode_b_prompt_references_get_system_schema():
    p = orch._build_orchestrator_prompt("orchestrator")
    assert "get_system_schema" in p
    # Softened (no longer a hard MANDATORY): prefer fetching, scoped to routed systems.
    assert "systems this question routes to" in p
    # The deterministic self-correction contract must be present in Mode B.
    assert "schema_mismatch" in p
    assert "schema_hint" in p


def test_mode_a_prompt_has_no_get_system_schema():
    p = orch._build_orchestrator_prompt("agent")
    assert "get_system_schema" not in p
    assert "schema_mismatch" not in p


# --------------------------------------------------------------------------- #
# Tool gating — get_system_schema only in Mode B
# --------------------------------------------------------------------------- #
def _tool_names(tools):
    return {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}


def test_tool_present_in_mode_b(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    assert "get_system_schema" in _tool_names(orch._get_orchestrator_tools())


def test_tool_absent_in_mode_a(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    assert "get_system_schema" not in _tool_names(orch._get_orchestrator_tools())


def test_tool_absent_when_mode_unset(monkeypatch):
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    assert "get_system_schema" not in _tool_names(orch._get_orchestrator_tools())


# --------------------------------------------------------------------------- #
# Cache behavior — loader once per (system_id, version); bump reloads
# --------------------------------------------------------------------------- #
def _call_tool(system_id):
    """Invoke the underlying function of the @tool-wrapped get_system_schema."""
    fn = getattr(orch.get_system_schema, "_tool_func", None) or getattr(
        orch.get_system_schema, "__wrapped__", None
    )
    if fn is None:
        # Fall back to the callable directly (older strands wrap forms).
        return orch.get_system_schema(system_id)
    return fn(system_id)


def test_tool_uses_cache_and_reloads_on_version_bump(monkeypatch):
    calls = {"n": 0}

    def _fake_build(system_id, include_samples=True):
        calls["n"] += 1
        return {"system_id": system_id, "tables": {}, "rendered": f"gen{calls['n']}"}

    monkeypatch.setattr("agents.schema_bundle.build_schema_bundle", _fake_build)

    versions = {"v": "v1"}
    monkeypatch.setattr(schema_cache, "cheap_schema_version", lambda _sid: versions["v"])

    r1 = json.loads(_call_tool("sys-x"))
    r2 = json.loads(_call_tool("sys-x"))
    assert calls["n"] == 1  # cached on the second call (same version)
    assert r1 == r2

    versions["v"] = "v2"  # a re-onboard bumped the version
    r3 = json.loads(_call_tool("sys-x"))
    assert calls["n"] == 2  # reloaded
    assert r3["rendered"] == "gen2"


def test_tool_requests_metadata_only(monkeypatch):
    """D2a: the loader builds the bundle with include_samples=False (graph metadata
    only — the orchestrator never surfaces sample values)."""
    seen = {}

    def _fake_build(system_id, include_samples=True):
        seen["include_samples"] = include_samples
        return {"system_id": system_id, "tables": {}, "rendered": ""}

    monkeypatch.setattr("agents.schema_bundle.build_schema_bundle", _fake_build)
    monkeypatch.setattr(schema_cache, "cheap_schema_version", lambda _sid: "v1")
    _call_tool("sys-x")
    assert seen["include_samples"] is False
