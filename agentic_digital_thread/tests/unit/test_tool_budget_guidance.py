# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression pins for the tool-budget / consolidate-after-misses guidance.

Live finding (post-reonboard smoke, 2026-07-14): one graph-guided trace prompt
took 942s with 34 tool calls. The ordered SSE re-run showed the orchestrator
delegating four consecutive near-identical sub-questions to the SAME telemetry
system after each returned a capability-gap answer ("cannot enumerate child
assets"), before eventually succeeding via a list-by-model fallback. The
existing discipline covered only explore_graph thrash (empty graph results) and
transient-error retries — a non-transient miss (empty result / capability
limit) had no budget, so the model rephrased indefinitely.

These tests pin the two GENERIC levers (no dataset/system literals):
1. orchestrator prompt — TOOL BUDGET rule in the shared DELEGATION DISCIPLINE
   block (both modes): at most one rephrased follow-up per system per question
   after a miss, then consolidate and answer honestly.
2. connection_types SiteWise-Direct query_guidance — TRAVERSAL BUDGET: after a
   drill-down step fails twice, fall back to list-per-model + filter instead of
   retrying variants.

Offline / pure import — no AWS, no network.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_prompt_carries_tool_budget_consolidation_rule(mode: str):
    orch = pytest.importorskip("agents.orchestrator")
    low = orch._build_orchestrator_prompt(mode).lower()
    assert "tool budget" in low, f"{mode}: TOOL BUDGET rule must be present"
    assert "consolidate after repeated misses" in low, (
        f"{mode}: rule must instruct consolidating after repeated misses"
    )
    # The budget: one follow-up per system after a miss or a gap, then stop
    # and answer (stated once, in MISS FOLLOW-UP, since 2026-10-05).
    assert "plus one follow-up only after a miss or a gap; nothing more" in low, (
        f"{mode}: budget must cap follow-ups at one per system"
    )
    assert "stop querying that system" in low, (
        f"{mode}: after the follow-up misses, the model must stop querying that system"
    )
    # Capability limits are FINAL — not retryable transient errors.
    assert "capability limit" in low and "not a transient error" in low, (
        f"{mode}: a capability-limit response must be treated as final, not retryable"
    )
    # Honest-gap contract is preserved, not replaced.
    assert "state the gap honestly" in low, (
        f"{mode}: consolidation must keep the honest-gap contract"
    )


def test_sitewise_direct_guidance_has_traversal_budget():
    from agents.connection_types import SITEWISE_DIRECT, default_query_guidance

    g = default_query_guidance(SITEWISE_DIRECT)
    low = g.lower()
    assert "traversal budget" in low, "SiteWise guidance must carry a TRAVERSAL BUDGET note"
    assert "twice" in low, "budget must bound retries of a failing drill-down step"
    assert "do not keep retrying rephrased variants" in low, (
        "budget must forbid retrying rephrased variants of the same failed step"
    )
    # The fallback is the list-per-model path that succeeded in the live trace.
    assert "list assets per asset model" in low and "filter by name/path" in low, (
        "budget must point to the list-per-model + name/path-filter fallback"
    )
    # The original ordered workflow and no-fabrication mandate must remain intact.
    assert "sitewise_list_asset_models" in g
    assert "sitewise_list_assets" in g
    assert "sitewise_describe_asset" in g
    assert "fabricate" in low


def test_sitewise_i3x_guidance_budgets_one_call_and_serial_only():
    """Latency lever (delegation framing): the i3X guidance must
    steer the ORCHESTRATOR toward ONE self-contained delegated sub-question
    (graph-resolved context first) instead of N narrow serial ones, and keep
    calls to the system serial (parallel fan-out spawns cold runtimes). This
    is the per-type analogue of the orchestrator's TOOL BUDGET rule."""
    from agents.connection_types import SITEWISE_I3X, default_query_guidance

    g = default_query_guidance(SITEWISE_I3X)
    low = g.lower()
    # One-delegation preference: graph context first, then a single rich ask.
    assert "one self-contained sub-question" in low
    assert "resolve names, hierarchy paths and property_count" in low
    # The batched capability is named — one round trip, never N narrow asks.
    assert "batched tools do in one round trip" in low
    assert "n narrow sub-questions" in low
    # Serial-only discipline is preserved.
    assert "never call this system in parallel with itself" in low
    assert "serially" in low
