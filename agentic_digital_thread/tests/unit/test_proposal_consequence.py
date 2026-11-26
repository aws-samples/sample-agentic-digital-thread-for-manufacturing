# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Steward-review consequence enrichment (ADR-0052 program).

Covers the pure ``proposal_scoring`` helpers (evidence strength, consequence,
touches, supersede counts), the pinned R3 tier PARITY between
``proposal_scoring.provenance_tier`` and ``orchestrator._provenance_tier``
(single source — the review badge and the route-seed Cypher CASE must never
drift), and the ``/api/proposed-mappings`` route's ADDITIVE fields
(``provenance_tier`` / ``consequence`` / ``touches``) with the concept
fan-out read folded into the existing gather.

Offline: the endpoint coroutine is called directly with
``self_learning.get_all_mappings`` and the concept-count reader stubbed — no
AWS, no Neptune, no network. GENERIC — synthetic systems/fields only.
"""
from __future__ import annotations

import asyncio

import agents.self_learning as self_learning
import agents.server as server
from agents.orchestrator import _provenance_tier as orchestrator_tier
from agents.proposal_scoring import (
    build_touches,
    consequence,
    evidence_strength,
    provenance_tier,
    supersede_counts,
    touched_systems,
)


# --------------------------------------------------------------------------- #
# evidence_strength — pinned formula
# --------------------------------------------------------------------------- #

def test_evidence_strength_base_levels():
    assert evidence_strength({"ai_confidence": "high"}) == 1.0
    assert evidence_strength({"ai_confidence": "medium"}) == 0.6
    assert evidence_strength({"ai_confidence": "low"}) == 0.2
    assert evidence_strength({}) == 0.2  # unknown folds to low


def test_evidence_strength_schema_verified_floors_at_0_8():
    row = {"ai_confidence": "low", "schema_verdict": "verified"}
    assert evidence_strength(row) == 0.8


def test_evidence_strength_target_verified_floors_at_0_9():
    row = {"ai_confidence": "medium", "verified": "true"}
    assert evidence_strength(row) == 0.9


def test_evidence_strength_quarantine_caps_at_0_1():
    row = {"ai_confidence": "high", "verified": "true", "quarantined": "true"}
    assert evidence_strength(row) == 0.1


# --------------------------------------------------------------------------- #
# consequence — evidence × blast radius; quarantined rows order last
# --------------------------------------------------------------------------- #

def test_blast_radius_counts_concept_sharing_systems():
    row = {"ai_confidence": "high", "from_system": "sys-a", "to_system": "sys-b"}
    # 2 endpoint systems + 3 catalog systems sharing the concept = blast 5.
    assert consequence(row, concept_system_count=3) == 5.0
    # No concept fan-out → endpoint systems only.
    assert consequence(row, concept_system_count=0) == 2.0
    # blast_radius never drops below 1 (single-system annotation rows).
    assert consequence({"ai_confidence": "medium"}, 0) == 0.6


def test_consequence_ordering_quarantined_last():
    quarantined = {"ai_confidence": "high", "quarantined": "true",
                   "from_system": "sys-a", "to_system": "sys-b"}
    strong = {"ai_confidence": "high", "from_system": "sys-a", "to_system": "sys-b"}
    weak = {"ai_confidence": "medium", "from_system": "sys-a"}
    rows = [quarantined, strong, weak]
    ordered = sorted(rows, key=lambda r: consequence(r, 2), reverse=True)
    assert ordered[0] is strong
    assert ordered[-1] is quarantined  # 0.1 cap beats even a big blast radius


# --------------------------------------------------------------------------- #
# touches — systems / concept / conflict / supersede_count
# --------------------------------------------------------------------------- #

def test_touched_systems_distinct_sorted_nonempty():
    assert touched_systems({"from_system": "sys-b", "to_system": "sys-a"}) == \
        ["sys-a", "sys-b"]
    assert touched_systems({"from_system": "sys-a", "to_system": "sys-a"}) == ["sys-a"]
    assert touched_systems({"from_system": "", "to_system": None}) == []


def test_touches_conflict_carries_review_reason():
    reason = "cross-site link vouched only by unverified authority ar-x-1"
    row = {"from_system": "sys-a", "to_system": "sys-b", "concept": "operator-id",
           "review_reason": reason}
    touches = build_touches(row, supersede_count=2)
    assert touches == {"systems": ["sys-a", "sys-b"], "concept": "operator-id",
                       "conflict": reason, "supersede_count": 2}
    # No review_reason → empty conflict, never a missing key.
    assert build_touches({"concept": "site"})["conflict"] == ""


def test_supersede_counts_groups_by_subject_key():
    rows = [
        {"subject_key": "sk-1", "status": "superseded"},
        {"subject_key": "sk-1", "status": "superseded"},
        {"subject_key": "sk-1", "status": "pending"},
        {"subject_key": "sk-2", "status": "accepted"},
        {"status": "superseded"},  # no subject_key → skipped
    ]
    assert supersede_counts(rows) == {"sk-1": 2}


# --------------------------------------------------------------------------- #
# provenance_tier — PINNED parity with orchestrator._provenance_tier (R3)
# --------------------------------------------------------------------------- #

_TIER_MATRIX = [
    ({"method": "user_confirmed"}, 0),
    ({"method": "crosswalk_confirmed", "provenance": "orchestrator"}, 0),
    ({"method": "onboarding-llm", "origin_context": "checkpoint"}, 2),
    ({"provenance": "orchestrator"}, 1),
    ({"origin_context": "Which site produced batch B-1?"}, 1),
    ({}, 2),
    ({"_tier": 0}, 0),  # precomputed payload tier passes through
]


def test_tier_parity_with_orchestrator():
    """The review badge MUST equal the orchestrator's R3 classification for
    every provenance shape — a drift here would show stewards a tier the
    route seed does not use."""
    for row, expected in _TIER_MATRIX:
        assert provenance_tier(dict(row)) == expected, row
        assert provenance_tier(dict(row)) == orchestrator_tier(dict(row)), row


# --------------------------------------------------------------------------- #
# Route: /api/proposed-mappings attaches the ADDITIVE fields
# --------------------------------------------------------------------------- #

def _call(monkeypatch, rows, concept_counts=None, ledger_evidence=None):
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: rows)
    monkeypatch.setattr(server, "_concept_system_counts",
                        lambda _ids: dict(concept_counts or {}))
    monkeypatch.setattr(server, "_ledger_evidence_by_node",
                        lambda: dict(ledger_evidence or {}))
    return asyncio.run(server.get_proposed_mappings_route())


def test_route_attaches_additive_review_fields(monkeypatch):
    rows = [{
        "mapping_id": "m-1", "subject_key": "sk-1", "status": "pending",
        "proposed_type": "join_path", "concept": "operator-id",
        "from_system": "sys-a", "from_field": "work_orders.operator_id",
        "to_system": "sys-b", "to_field": "operators.operator_id",
        "confidence": "0.9", "provenance": "join-discovery",
        "review_reason": "conflicts with an accepted reverse mapping",
    }]
    body = _call(monkeypatch, rows, concept_counts={"operator-id": 3})
    m = body["mappings"][0]
    assert m["provenance_tier"] == 1  # carries provenance → accepted-proposal tier
    # join_path with dotted fields scores 'high' → 1.0 × (2 endpoints + 3) = 5.0
    assert m["consequence"] == 5.0
    assert m["touches"]["systems"] == ["sys-a", "sys-b"]
    assert m["touches"]["concept"] == "operator-id"
    assert m["touches"]["conflict"] == "conflicts with an accepted reverse mapping"
    # ADDITIVE ONLY: every pre-existing field is untouched.
    for key in ("mapping_id", "status", "ai_recommendation", "ai_confidence",
                "concept_description", "concept_layer"):
        assert key in m


def test_route_supersede_count_from_fetched_rows(monkeypatch):
    rows = [
        {"mapping_id": "m-1", "subject_key": "sk-1", "status": "pending",
         "proposed_type": "value_mapping", "concept": "site",
         "from_system": "sys-a", "from_value": "S1", "to_system": "sys-b",
         "to_value": "Site One", "confidence": "0.9"},
        {"mapping_id": "m-0", "subject_key": "sk-1", "status": "superseded",
         "proposed_type": "value_mapping", "concept": "site",
         "from_system": "sys-a", "from_value": "S1", "to_system": "sys-b",
         "to_value": "Old Site", "confidence": "0.5"},
    ]
    body = _call(monkeypatch, rows)
    by_id = {m["mapping_id"]: m for m in body["mappings"]}
    assert by_id["m-1"]["touches"]["supersede_count"] == 1
    assert by_id["m-0"]["touches"]["supersede_count"] == 1  # same subject chain


def test_route_every_item_carries_review_fields(monkeypatch):
    rows = [
        {"mapping_id": "m-a", "proposed_type": "insight", "concept": "",
         "from_system": "sys-a", "confidence": "0.7", "status": "pending"},
        {"mapping_id": "m-b", "proposed_type": "field_concept_mint",
         "concept": "lead-time-days", "to_description": "A definition.",
         "confidence": "0.6", "status": "pending"},
    ]
    body = _call(monkeypatch, rows)
    for m in body["mappings"]:
        assert isinstance(m["provenance_tier"], int)
        assert isinstance(m["consequence"], float)
        assert set(m["touches"]) == {"systems", "concept", "conflict",
                                     "supersede_count"}


# --------------------------------------------------------------------------- #
# _concept_system_counts — ONE batched read, both mapping layers, best-effort
# --------------------------------------------------------------------------- #

def test_concept_system_counts_single_batched_read(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        calls.append((cypher, params or {}))
        return [
            {"id": "operator-id", "systems": ["sys-a", "sys-b"]},
            {"id": "operator-id", "systems": ["sys-b", "sys-c"]},  # UNION branch
            {"id": "site", "systems": ["sys-a"]},
        ]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    counts = server._concept_system_counts({"operator-id", "site", ""})
    assert counts == {"operator-id": 3, "site": 1}  # union-folded, distinct
    assert len(calls) == 1  # ONE read for ALL ids — no N+1
    assert calls[0][1] == {"ids": ["operator-id", "site"]}  # empties dropped


def test_concept_system_counts_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(server, "neptune_query", lambda c, p=None: [])
    assert server._concept_system_counts({"operator-id"}) == {}
    assert server._concept_system_counts(set()) == {}


# --------------------------------------------------------------------------- #
# X-2 — evidence_classes reach the proposal rows the SlCards badge reads
# --------------------------------------------------------------------------- #

def test_route_attaches_evidence_classes_to_accepted_rows(monkeypatch):
    rows = [
        {"mapping_id": "m-acc", "status": "accepted",
         "proposed_type": "value_mapping", "concept": "supplier",
         "from_system": "sys-a", "from_value": "S1",
         "to_system": "sys-b", "to_value": "V1", "confidence": "0.9",
         "neptune_node_id": "ir-sysa-0011223344556677"},
        {"mapping_id": "m-pend", "status": "pending",
         "proposed_type": "value_mapping", "concept": "supplier",
         "from_system": "sys-a", "from_value": "S2",
         "to_system": "sys-b", "to_value": "V2", "confidence": "0.8"},
    ]
    body = _call(monkeypatch, rows, ledger_evidence={
        "ir-sysa-0011223344556677": ["relationship-proof",
                                     "route-intersection"]})
    by_id = {m["mapping_id"]: m for m in body["mappings"]}
    # The accepted row's neptune_node_id maps to its ledger evidence flags.
    assert by_id["m-acc"]["evidence_classes"] == [
        "relationship-proof", "route-intersection"]
    # Rows without ledger evidence carry NO key — the badge degrades silently.
    assert "evidence_classes" not in by_id["m-pend"]


def test_route_without_ledger_evidence_is_byte_identical(monkeypatch):
    """Empty ledger (backend down / cold env) ⇒ the attach is a no-op: no
    row gains the key, nothing else changes, never a 500."""
    rows = [{"mapping_id": "m-acc", "status": "accepted",
             "proposed_type": "value_mapping", "concept": "supplier",
             "from_system": "sys-a", "from_value": "S1",
             "to_system": "sys-b", "to_value": "V1", "confidence": "0.9",
             "neptune_node_id": "ir-sysa-0011223344556677"}]
    body = _call(monkeypatch, rows, ledger_evidence={})
    assert body["count"] == 1
    assert "evidence_classes" not in body["mappings"][0]


def test_ledger_evidence_helper_reads_ledger_once(monkeypatch):
    import agents.artifact_usage as artifact_usage
    calls = []

    def fake_read(days=30):
        calls.append(days)
        return {"ir-x": {"artifact_id": "ir-x", "kind": "rule",
                         "usage_30d": 2,
                         "evidence_classes": ["tool-text"]},
                "concept-a": {"artifact_id": "concept-a", "kind": "concept",
                              "usage_30d": 1, "evidence_classes": []}}

    monkeypatch.setattr(artifact_usage, "read_artifact_usage", fake_read)
    out = server._ledger_evidence_by_node()
    assert calls == [30]  # ONE bounded read — no N+1
    assert out == {"ir-x": ["tool-text"]}  # empty-class entries dropped


def test_ledger_evidence_helper_degrades_to_empty(monkeypatch):
    import agents.artifact_usage as artifact_usage

    def boom(days=30):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(artifact_usage, "read_artifact_usage", boom)
    assert server._ledger_evidence_by_node() == {}
