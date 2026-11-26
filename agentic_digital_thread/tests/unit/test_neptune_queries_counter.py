# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``neptune_queries`` must count the graph traversal in BOTH query modes.

Regression: the intelligence-summary counter (and the discovery gate) matched
only the bare in-process tool name ``explore_graph``. In ``--query-mode agent``
the AgentCore Gateway emits the target-prefixed name
``digital-thread___explore_graph``, so ``neptune_queries`` was always 0 and the
``scripts/smoke_prompts.py`` suite-level graph gate could never pass.

The fix is ``runtime_app._is_explore_graph``, which recognizes both spellings.
"""
from __future__ import annotations

import agents.runtime_app as runtime_app
from agents.runtime_app import _is_explore_graph, _check_for_discoveries
import agents.self_learning as self_learning


def test_is_explore_graph_matches_bare_and_gateway_prefixed():
    # Mode-B / in-process orchestrator spelling.
    assert _is_explore_graph("explore_graph") is True
    # Agent-mode gateway spelling.
    assert _is_explore_graph("digital-thread___explore_graph") is True
    # Not the graph tool.
    assert _is_explore_graph("erp___query") is False
    assert _is_explore_graph("get_system_schema") is False
    assert _is_explore_graph("digital-thread___get_system_schema") is False


def _count_neptune(tool_calls_log):
    """Mirror the intelligence-summary counter expression in runtime_app."""
    return sum(1 for t in tool_calls_log if _is_explore_graph(t["name"]))


def test_summary_counter_counts_gateway_prefixed_explore_graph():
    """Under agent mode the tool arrives prefixed — the counter must still see it."""
    agent_mode_log = [
        {"toolUseId": "t0", "name": "digital-thread___explore_graph"},
        {"toolUseId": "t1", "name": "erp___query"},
    ]
    assert _count_neptune(agent_mode_log) == 1

    # Orchestrator / Mode-B: bare name still counted.
    modeb_log = [{"toolUseId": "t0", "name": "explore_graph"}]
    assert _count_neptune(modeb_log) == 1

    # No graph consulted → 0.
    no_graph_log = [{"toolUseId": "t0", "name": "erp___query"}]
    assert _count_neptune(no_graph_log) == 0


def test_discovery_gate_recognizes_gateway_prefixed_graph(monkeypatch):
    """The cross-system discovery gate requires >=1 graph call; in agent mode
    that call is ``digital-thread___explore_graph``. With the prefixed name the
    gate now proceeds and reaches ``log_proposed_mapping`` for a directional
    cross-system value flow (it previously bailed at ``if not neptune_calls``)."""
    uuid = "12345678-1234-1234-1234-123456789abc"
    tool_calls = [
        {"toolUseId": "t0", "name": "digital-thread___explore_graph"},
        {"toolUseId": "t1", "name": "erp___query"},
        {"toolUseId": "t2", "name": "cmms___query"},
        {"toolUseId": "t3", "name": "erp___query"},
    ]
    tool_inputs = {
        "t0": "MATCH (n) RETURN n",
        "t1": "SELECT id FROM erp_data.orders",
        "t2": f"SELECT * FROM cmms_data.work_orders WHERE asset_id = '{uuid}'",  # nosec B608 - test fixture composing expected SQL from module constants
        "t3": "SELECT id FROM erp_data.customers",
    }
    tool_results = {
        "t0": "graph result",
        "t1": f"row: {uuid}",
        "t2": "ok",
        "t3": "more rows",
    }

    calls = {"n": 0}
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "id-x",
    )

    _check_for_discoveries(tool_calls, tool_inputs, tool_results, "cross-system flow", "sess-agent")
    assert calls["n"] == 1, "prefixed explore_graph must satisfy the graph gate in agent mode"
