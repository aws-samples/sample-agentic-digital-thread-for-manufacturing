# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the S4.3 affinity additions to ``agents.coverage`` (ADR 0007).

All tests are pure: ``affinity_suggestion_for`` reads only the frozen
``CONCEPT_AFFINITY`` table (no env, no graph), the graph-read seam of
``system_covered_concepts`` is injected (``query=...``), and ``thread_coverage`` is
driven with an injected ``suggester`` / ``covered`` ``frozenset``. No AWS/graph/LLM.
"""
from __future__ import annotations

import agents.coverage as cov
from agents.card_layer import get_card_layer


# --------------------------------------------------------------------------
# affinity_suggestion_for — 1/2/3+ oxford join + generic fallback (pure, no env)
# --------------------------------------------------------------------------

def test_affinity_suggestion_one_type():
    # maintenance-event → only CMMS.
    assert (cov.affinity_suggestion_for("maintenance-event")
            == "Onboard a CMMS — typically provides maintenance-event")


def test_affinity_suggestion_two_types():
    # oee-score → ('Historian', 'MES') sorted → "a Historian or MES".
    assert (cov.affinity_suggestion_for("oee-score")
            == "Onboard a Historian or MES — typically provides oee-score")


def test_affinity_suggestion_three_types():
    # equipment-id → ('CMMS', 'Historian', 'MES') sorted → oxford comma join.
    assert (cov.affinity_suggestion_for("equipment-id")
            == "Onboard a CMMS, Historian, or MES — typically provides equipment-id")


def test_affinity_suggestion_unassigned_falls_back_to_generic():
    # facility is deliberately unassigned → () → generic suggestion_for string.
    assert (cov.affinity_suggestion_for("facility")
            == cov.suggestion_for("facility")
            == "Onboard a system providing facility")


# --------------------------------------------------------------------------
# thread_coverage suggester injection — default byte-identical, injected enriched
# --------------------------------------------------------------------------

def test_thread_coverage_default_suggester_byte_identical():
    # No suggester arg ⇒ generic S4.2 strings (byte-identical default behavior).
    out = cov.thread_coverage(frozenset())
    for g in out["gaps"]:
        assert g["suggestion"] == f"Onboard a system providing {g['concept']}"


def test_thread_coverage_injected_affinity_suggester():
    out = cov.thread_coverage(frozenset({"mro-inventory"}),
                              suggester=cov.affinity_suggestion_for)
    gap = next(g for g in out["gaps"] if g["concept"] == "oee-score")
    # Gap dict keys/types unchanged; only the suggestion string is enriched.
    assert set(gap.keys()) == {"concept", "blocks_cards", "suggestion"}
    assert gap["blocks_cards"] == ["oee"]
    assert gap["suggestion"] == "Onboard a Historian or MES — typically provides oee-score"
    assert isinstance(gap["suggestion"], str)


# --------------------------------------------------------------------------
# system_covered_concepts — ONE scoped graph read via the injected seam
# --------------------------------------------------------------------------

def test_system_covered_concepts_rows():
    def fake(_cypher, parameters=None):
        return [{"id": "equipment-id"}, {"id": "downtime"}, {"id": "downtime"}]
    out = cov.system_covered_concepts("cmms", query=fake)
    assert out == frozenset({"equipment-id", "downtime"})
    assert isinstance(out, frozenset)


def test_system_covered_concepts_filters_rows_without_id():
    def fake(_cypher, parameters=None):
        return [{"id": "equipment-id"}, {"other": "x"}, {"id": None}]
    assert cov.system_covered_concepts("cmms", query=fake) == frozenset({"equipment-id"})


def test_system_covered_concepts_scopes_by_system_id():
    seen = {}

    def fake(cypher, parameters=None):
        seen["cypher"] = cypher
        seen["parameters"] = parameters
        return []

    cov.system_covered_concepts("cmms", query=fake)
    assert "MATCH (s:System {id: $sid})" in seen["cypher"]
    assert "MAPS_TO]->(c:Concept) RETURN DISTINCT c.id AS id" in seen["cypher"]
    assert seen["parameters"] == {"sid": "cmms"}


def test_system_covered_concepts_swallows_graph_error(monkeypatch):
    # The real neptune_query catches all exceptions → [] (no-graph safe).
    import agents.db as db

    def boom(*_a, **_k):
        raise RuntimeError("no graph")

    monkeypatch.setattr(db.boto3, "client", boom)
    assert cov.system_covered_concepts("cmms") == frozenset()
