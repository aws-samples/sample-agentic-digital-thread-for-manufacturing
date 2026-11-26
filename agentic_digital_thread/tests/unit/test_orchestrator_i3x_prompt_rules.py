# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pins for the i3X telemetry-layer routing rules in the orchestrator prompt (Wave 2 B5).

The i3X port added a :Property spine to the knowledge graph and three one-call
telemetry tools. These tests pin the prompt fragments that make the orchestrator
use them efficiently:

1. serial-sitewise rule — never fan out parallel sitewise tool calls (each
   parallel invocation spawns a fresh cold runtime); other systems may still
   parallelize alongside the serial sitewise chain.
2. one-call preference — the block STRONGLY PREFERS the three batched tools
   (chart / composite / batch-history) over N single-id round trips.
3. property_count guard — never request history on a structural-only Asset
   (property_count == 0).
4. start_time/end_time ALWAYS required for history/telemetry calls.
5. graph-shape additions — HAS_PROPERTY edge (gated on property_count > 0),
   Property node field docs, the MODELED_BY concept-bridge traversal, and
   property_count on the Asset docs.
6. chart hygiene (hook era): a tool's charts reach the screen by themselves
   and their data is withheld from the model (ChartRedactionHook); the model
   authors no chart for a series they cover, nor for a telemetry series no
   tool chart shows (it says that series is not charted), while charts for
   its other computed series remain explicitly allowed.
7. MAPS_TO_ASSET routing — equipment questions spanning maintenance +
   telemetry fetch the stored ValueMapping bridge FIRST (one hop:
   maintenance tag + telemetry UUID + path); ValueMapping docs carry
   to_element_id/to_path.
8. fuzzy resolution — an "ambiguous" error carries a candidates list; the
   model re-calls once with the exact name instead of giving up.
9. demo-literal DENYLIST — the assembled prompt stays generic (project
   constraint: demo data must not appear in core files).

Offline / pure import — no AWS, no Neptune, no network.
"""
from __future__ import annotations

import re

import pytest

# Demo literals that must NEVER appear in the assembled prompt (word-boundary,
# case-insensitive — catches e.g. 'Line1Filler' embedded in an example path).
_DEMO_DENYLIST = (
    "Milwaukee",
    "Line1Filler",
    "AnyCompany",
    "Filler",
    "WasherPump",
    "BottlingMachine",
)


@pytest.fixture(scope="module")
def prompt() -> str:
    orch = pytest.importorskip("agents.orchestrator")
    return orch._build_orchestrator_prompt("agent")


def test_serial_sitewise_rule_absent(prompt: str):
    """The serial-telemetry rule is GONE, and must not be reintroduced as prompt
    text.

    Measured 2026-07-30 against the gateway with no model involved: parallel
    same-system calls completed in 5.1 s wall against serial's 14.7 s, all
    success — the rule's premise (parallel spawns a cold runtime and is far
    slower) did not reproduce, so it cost latency for no benefit. If serialism is
    ever required again it belongs in CODE: a prompt-only constraint is
    model-specific by construction and its presence in the prompt proves
    nothing about behaviour.
    """
    assert "NEVER fan out parallel sitewise tool calls" not in prompt
    assert "SERIAL RULE FOR TELEMETRY SYSTEMS" not in prompt


def test_one_call_delegation_names_tools_as_system_owned(prompt: str):
    """C1 reconciliation: the one-call telemetry tools live on the OWNING
    system's agent — the orchestrator can NOT call sitewise_* tools in either
    deployed mode (Mode A exposes query_<id>, Mode B run_query). The block
    names the tools only to say they are system-owned and steers the
    orchestrator to delegate ONE rich sub-question instead."""
    assert "DELEGATE ONE RICH SUB-QUESTION" in prompt
    assert "NOT in your toolset" in prompt
    assert "never attempt to call sitewise_* tools yourself" in prompt
    # The three one-call tools are still NAMED (so the model recognizes them
    # in system answers/errors) as system-owned capabilities.
    assert "sitewise_get_telemetry_chart" in prompt
    assert "sitewise_get_asset_telemetry" in prompt
    assert "sitewise_get_assets_history" in prompt
    # One self-contained sub-question instead of N narrow serial calls.
    assert "ONE self-contained sub-question" in prompt
    assert "Do NOT loop the" in prompt
    assert "never ask it channel-by-channel" in prompt


def test_property_count_guard(prompt: str):
    """Structural-only assets (property_count == 0) must never be queried for
    history/current values — resolve to Property leaves first."""
    assert "property_count GUARD" in prompt
    assert "property_count is 0" in prompt
    assert "structural-only" in prompt
    assert "Property leaves first" in prompt


def test_time_window_always_required(prompt: str):
    assert "start_time and end_time are ALWAYS required" in prompt
    assert 'no "default to recent values" behavior' in prompt


def test_graph_shape_has_property_and_modeled_by_lines(prompt: str):
    """GRAPH SCHEMA must document the Property spine: the HAS_PROPERTY edge
    (gated on property_count > 0), the MODELED_BY concept bridge, Property node
    fields, and property_count on the Asset docs."""
    assert "-[:HAS_PROPERTY]-> (Property)" in prompt
    assert "property_count > 0" in prompt
    # The full concept-bridge traversal is spelled out.
    assert "-[:MODELED_BY]-> (SchemaEntry)" in prompt
    assert (
        "(p:Property)<-[:HAS_PROPERTY]-(a:Asset)-[:INSTANCE_OF]->(m:AssetModel)"
        "-[:MODELED_BY]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[:MAPS_TO]->(c:Concept)"
    ) in prompt
    # Node-field documentation.
    assert "Property nodes contain: name, source_id (the elementId" in prompt
    assert "parent_id (the owning Asset's source_id)" in prompt
    assert "data_type" in prompt
    # property_count documented on Asset nodes.
    assert "property_count (how many telemetry channels the asset ITSELF holds" in prompt
    assert "0 means structural-only" in prompt


def test_chart_blocks_never_reemitted(prompt: str):
    """Hook era: the ChartRedactionHook takes chart data out of model-visible
    tool results and the runtime streams the tags to the page, so the prompt
    says a tool's charts reach the screen by themselves and the model authors
    no chart for a series they cover, while the old 'output it VERBATIM'
    rule stays gone."""
    text = " ".join(prompt.split())
    assert "A tool's charts reach the user's screen by themselves" in text
    assert ("- A tool chart on screen covers it: author no chart-block (no copy, no "
            "placeholder)") in text
    assert "output it VERBATIM" not in prompt
    assert "ALREADY DISPLAYED" not in prompt  # old interim wording replaced
    # Extended in place — exactly one CHART RENDERING section.
    assert prompt.count("CHART RENDERING") == 1
    # Telemetry charts come from the tool, never hand-built.
    assert ("- A telemetry or sensor-history series with no tool chart on screen") in text
    assert "or a note counts it as left out): author no chart-block." in text


def test_hand_built_sql_chart_allowance_is_explicit(prompt: str):
    """The hook's toolResult gate deliberately permits model-AUTHORED charts
    (hand-built from tabular SQL results) — the prompt must say so explicitly,
    not leave the allowance implied: a series that is neither covered by a
    tool chart nor telemetry gets a chart in the documented tag."""
    text = " ".join(prompt.split())
    assert ("- Any other time-series or trend from this turn's results: render a chart "
            "with this EXACT tag:") in text


def test_maps_to_asset_routing_rule(prompt: str):
    """Equipment questions spanning maintenance + telemetry must fetch the stored
    ValueMapping bridge FIRST (one hop: maintenance tag + telemetry UUID + path)
    and never free-text search a maintenance system for mapped equipment."""
    assert (
        "(vm:ValueMapping {concept:'equipment'})-[:MAPS_TO_ASSET]->(a:Asset)"
    ) in prompt
    assert "maintenance_tag" in prompt
    assert "telemetry_uuid" in prompt
    assert "NEVER free-text search a maintenance system" in prompt
    # ValueMapping node docs carry the new fields.
    assert "to_element_id (the telemetry-system UUID)" in prompt
    assert "to_path (the telemetry" in prompt


def test_fuzzy_resolution_candidates_recall(prompt: str):
    """The owning system fuzzy-resolves descriptive names; an 'ambiguous' answer
    carries a candidates list — the model must re-delegate once with the exact
    name, not give up on the first ambiguity error."""
    assert "NEVER give up on the first ambiguity error" in prompt
    assert '"ambiguous" with a candidates list' in prompt
    assert "re-delegate ONCE with the exact name" in prompt
    assert "known_assets" in prompt


def test_chart_tool_explicit_window_for_event_reviews(prompt: str):
    """Event reviews use the event's OWN timestamps as an explicit window —
    never 'the last N hours'."""
    assert "work-order / outage / event reviews" in prompt
    assert "EXPLICIT" in prompt
    assert "window (start_time, end_time)" in prompt
    assert 'never "the last N hours"' in prompt


@pytest.mark.parametrize("literal", _DEMO_DENYLIST)
def test_prompt_contains_no_demo_literals(prompt: str, literal: str):
    """Project constraint: demo/dataset literals must not appear in core prompt
    text — placeholders (<asset-name>, <site>) only."""
    assert re.search(r"\b" + re.escape(literal) + r"\b", prompt, re.IGNORECASE) is None, (
        f"demo literal {literal!r} leaked into the assembled Mode A orchestrator prompt"
    )


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_rules_shared_across_both_modes(mode: str):
    """The telemetry routing rules are mode-independent (shared prompt sections)."""
    orch = pytest.importorskip("agents.orchestrator")
    p = orch._build_orchestrator_prompt(mode)
    assert "NEVER fan out parallel sitewise tool calls" not in p
    assert "DELEGATE ONE RICH SUB-QUESTION" in p
    assert "property_count GUARD" in p
    assert "MAPS_TO_ASSET" in p
    assert "A tool's charts reach the user's screen by themselves" in p


def test_anchor_drift_raises_loudly():
    """The anchored post-pass must fail LOUD (no silent fallback) if the base
    prompt no longer contains an anchor sentence."""
    orch = pytest.importorskip("agents.orchestrator")
    with pytest.raises(RuntimeError, match="anchor drifted"):
        orch._apply_i3x_prompt_rules("a prompt without any of the anchors")
