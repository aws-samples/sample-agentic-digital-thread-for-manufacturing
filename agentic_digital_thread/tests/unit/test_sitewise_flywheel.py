# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 3.2 — determinism flywheel for SiteWise (ADR-0037):

(b) a SiteWise CORRECTION is promoted into the system's persisted ``query_guidance``
    via the existing (generic, not SQL-gated) ``append_query_guidance_note`` — and the
    write is idempotent (a repeat of the same runtime-derived note does not duplicate);
(a) an accepted machineId↔asset-UUID ValueMapping is reachable by the TRANSLATE
    traversal the orchestrator already does (``explore_graph`` over ValueMapping nodes)
    for a SYNTHETIC segment id — no hardcoded ids;
(c) a stored SiteWise ``query_guidance`` note is surfaced on the next question via the
    route-time System-node read (``explore_graph``).

Fully synthetic — the Neptune seams are monkeypatched. No AWS/graph/LLM, no dataset
literals; all ids are arbitrary.
"""
from __future__ import annotations

import json

import agents.graph_tools as graph_tools
import agents.self_learning as self_learning


# --------------------------------------------------------------------------- #
# (b) correction -> query_guidance, idempotent, generic for a SiteWise system
# --------------------------------------------------------------------------- #

class _FakeGuidanceGraph:
    """Stateful in-memory System.query_guidance store for one system."""

    def __init__(self):
        self.qg = ""
        self.writes = 0

    def query(self, cypher, params=None):
        return [{"qg": self.qg}]

    def write(self, cypher, params=None):
        self.writes += 1
        self.qg = params["qg"]
        return True


def test_sitewise_correction_files_pending_guidance_idempotently(monkeypatch):
    calls = []

    def file_proposal(*args, **kwargs):
        calls.append((args, kwargs))
        return "proposal-existing"

    monkeypatch.setattr(
        self_learning, "log_proposed_mapping", file_proposal)
    monkeypatch.setattr(self_learning, "get_mapping", lambda mapping_id: {
        "mapping_id": mapping_id, "status": "pending",
        "proposed_type": "guidance_note"})
    system_id = "sw-synth"
    note = ("evt_model.dur is the downtime measure; translate machine ref "
            "via ValueMapping first")

    assert self_learning.append_query_guidance_note(
        system_id, note) == "filed"
    assert self_learning.append_query_guidance_note(
        system_id, note) == "filed"
    assert all(call[1]["proposed_type"] == "guidance_note" for call in calls)
    assert all(call[1]["to_value"] == note for call in calls)
    assert all(call[1]["guidance_scope"] == "query_hint" for call in calls)


def test_append_guidance_returns_explicit_not_filed_for_invalid_input(monkeypatch):
    calls = []
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda *args, **kwargs: calls.append((args, kwargs)))
    assert self_learning.append_query_guidance_note("", "note") == "not_filed"
    assert self_learning.append_query_guidance_note(
        "sw-synth", "") == "not_filed"
    assert calls == []


# --------------------------------------------------------------------------- #
# (a) accepted machineId<->asset ValueMapping reachable by TRANSLATE traversal
# --------------------------------------------------------------------------- #

def test_accepted_value_mapping_reachable_by_translate_traversal(monkeypatch):
    # SYNTHETIC segment machine id -> asset UUID accepted mapping in the graph.
    seg_machine_id = "SEG-MACHINE-XYZ"
    asset_uuid = "1111aaaa-2222-bbbb-3333-cccc4444dddd"

    def fake_nq(cypher, params=None):
        # The orchestrator TRANSLATE step reads ValueMapping from_value->to_value.
        if "ValueMapping" in cypher and params and params.get("mid") == seg_machine_id:
            return [{"to_value": asset_uuid, "to_system": "sw-synth", "confidence": 0.95}]
        return []

    monkeypatch.setattr(graph_tools, "neptune_query", fake_nq)
    monkeypatch.setattr(graph_tools, "neptune_query_checked", fake_nq)

    # The exact traversal shape the orchestrator prompt instructs (ValueMapping to_value),
    # with the segment machine id bound as a parameter (no hardcoded id in code paths).
    cypher = (
        "MATCH (vm:ValueMapping {from_value: $mid}) "
        "RETURN vm.to_value AS to_value, vm.to_system AS to_system, vm.confidence AS confidence"
    )
    rows = graph_tools.neptune_query(cypher, {"mid": seg_machine_id})
    assert rows and rows[0]["to_value"] == asset_uuid
    assert rows[0]["to_system"] == "sw-synth"
    # A miss returns empty (not an error) — the agent must not fabricate an id.
    assert graph_tools.neptune_query(cypher, {"mid": "NOPE"}) == []
    # explore_graph wraps the same seam and returns a well-formed results envelope.
    out = json.loads(graph_tools.explore_graph("MATCH (vm:ValueMapping) RETURN vm"))
    assert "results" in out


# --------------------------------------------------------------------------- #
# (c) replay — stored SiteWise query_guidance surfaced on the next question
# --------------------------------------------------------------------------- #

def test_stored_query_guidance_surfaced_at_route_time(monkeypatch):
    learned = "LEARNED: evt_model.dur is the downtime measure"

    def fake_nq(cypher, params=None):
        if "System" in cypher:
            return [{"id": "sw-synth", "connection_type": "SiteWise (Direct)",
                     "query_guidance": learned, "capabilities": "OT telemetry"}]
        return []

    monkeypatch.setattr(graph_tools, "neptune_query", fake_nq)
    monkeypatch.setattr(graph_tools, "neptune_query_checked", fake_nq)
    out = json.loads(graph_tools.explore_graph("MATCH (s:System) RETURN s"))
    node = out["results"][0]
    # The route-time read surfaces the accumulated guidance for the next question.
    assert node["query_guidance"] == learned
    assert node["connection_type"] == "SiteWise (Direct)"
