# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents.coverage`` (S4.2 Wave 2).

All tests are pure: the graph-read seam is injected (``query=...``) or the real
``neptune_query`` is exercised with its boto3 client patched to raise, proving the
swallow → ``[]`` → ``frozenset()`` contract. ``covered`` is injected as a
``frozenset`` so no AWS/graph/LLM is touched.
"""
from __future__ import annotations

import agents.coverage as cov
from agents.card_layer import get_card_layer


# --------------------------------------------------------------------------
# covered_concepts — ONE graph read via the injected seam (db.py error → [])
# --------------------------------------------------------------------------

def test_covered_concepts_rows():
    def fake(_cypher, parameters=None):
        return [{"id": "mro-inventory"}, {"id": "availability"}, {"id": "availability"}]
    out = cov.covered_concepts(query=fake)
    assert out == frozenset({"mro-inventory", "availability"})
    assert isinstance(out, frozenset)


def test_covered_concepts_empty():
    assert cov.covered_concepts(query=lambda *_a, **_k: []) == frozenset()


def test_covered_concepts_filters_rows_without_id():
    def fake(_cypher, parameters=None):
        return [{"id": "mro-inventory"}, {"other": "x"}, {"id": None}]
    assert cov.covered_concepts(query=fake) == frozenset({"mro-inventory"})


def test_covered_concepts_swallows_graph_error(monkeypatch):
    # The real neptune_query catches all exceptions → [] (no-graph safe). With the
    # default arg, a raising boto3 client must degrade to frozenset().
    import agents.db as db

    def boom(*_a, **_k):
        raise RuntimeError("no graph")

    monkeypatch.setattr(db.boto3, "client", boom)
    assert cov.covered_concepts() == frozenset()


def test_covered_concepts_uses_locked_cypher():
    seen = {}

    def fake(cypher, parameters=None):
        seen["cypher"] = cypher
        return []

    cov.covered_concepts(query=fake)
    assert "MATCH (s:System)-[:HAS_SCHEMA]->" in seen["cypher"]
    assert "MAPS_TO]->(c:Concept) RETURN DISTINCT c.id AS id" in seen["cypher"]


# --------------------------------------------------------------------------
# suggestion_for — deterministic S4.3/ADR 0007 seam
# --------------------------------------------------------------------------

def test_suggestion_for_text():
    assert cov.suggestion_for("performance") == "Onboard a system providing performance"


# --------------------------------------------------------------------------
# card_coverage — per-card satisfiable + cheapest-path missing (one source)
# --------------------------------------------------------------------------

def _card(card_id):
    return get_card_layer().get_card(card_id)


def test_card_coverage_direct_satisfiable():
    cc = cov.card_coverage(_card("spare-parts-on-hand"), frozenset({"mro-inventory"}))
    assert cc == {"card_id": "spare-parts-on-hand", "satisfiable": True,
                  "missing_concepts": []}


def test_card_coverage_direct_missing():
    cc = cov.card_coverage(_card("spare-parts-on-hand"), frozenset())
    assert cc["satisfiable"] is False
    assert cc["missing_concepts"] == ["mro-inventory"]


def test_card_coverage_oee_cheapest_path_missing():
    # The `oee` card is now a GAUGE whose refs are (oee, availability,
    # performance, oee-quality). With nothing covered, card_coverage unions each
    # unsatisfiable ref's cheapest unmet path: oee → {oee-score} (the prefer_stored
    # shortcut), availability/performance direct, and oee-quality → {quality-yield-rate}
    # (its own prefer_stored shortcut — cheaper than the two derive inputs).
    cc = cov.card_coverage(_card("oee"), frozenset())
    assert cc["satisfiable"] is False
    assert cc["missing_concepts"] == [
        "availability", "oee-score", "performance", "quality-yield-rate"]


def test_card_coverage_oee_satisfiable_via_inputs():
    # Covering the sub-reading concepts satisfies BOTH the composite oee (via its
    # formula inputs) AND the direct/derived sub-reading refs. oee-quality needs
    # BOTH the good-count (quality-metric) and actual-quantity concepts.
    covered = frozenset({"availability", "performance", "quality-metric", "actual-quantity"})
    cc = cov.card_coverage(_card("oee"), covered)
    assert cc == {"card_id": "oee", "satisfiable": True, "missing_concepts": []}


def test_card_coverage_oee_stored_alone_insufficient_for_gauge():
    # `oee-score` alone satisfies the composite oee (stored shortcut) but NOT the
    # direct/derived sub-reading refs the gauge also renders, so the CARD is still
    # unsatisfiable and reports the missing sub-readings (oee-quality's cheapest
    # path is its quality-yield-rate prefer_stored shortcut).
    cc = cov.card_coverage(_card("oee"), frozenset({"oee-score"}))
    assert cc["satisfiable"] is False
    assert cc["missing_concepts"] == [
        "availability", "performance", "quality-yield-rate"]


def test_card_coverage_quality_by_line_bar():
    # The `quality-by-line` BAR card references quality-yield-rate (concept
    # quality-yield-rate); covering it makes the card satisfiable.
    qbl = _card("quality-by-line")
    assert cov.card_coverage(qbl, frozenset())["missing_concepts"] == ["quality-yield-rate"]
    assert cov.card_coverage(qbl, frozenset({"quality-yield-rate"})) == {
        "card_id": "quality-by-line", "satisfiable": True, "missing_concepts": []}

def test_card_coverage_ot_native_kpi_cards():
    # Each abstract OT-native KPI card is satisfiable exactly when its single
    # concept (id == card id == metric id) is covered — proving any system that
    # covers the generic OT concept lights up the card.
    for card_id in ("throughput", "machine-speed", "scrap-waste", "runtime-hours"):
        card = _card(card_id)
        assert cov.card_coverage(card, frozenset())["missing_concepts"] == [card_id]
        assert cov.card_coverage(card, frozenset({card_id})) == {
            "card_id": card_id, "satisfiable": True, "missing_concepts": []}


def test_card_coverage_machine_state_bar():
    # The machine-state BAR card references machine-state-distribution (concept
    # machine-state); covering machine-state makes the card satisfiable. Proves
    # the PART B requirement: validates + satisfiable when machine-state is covered.
    card = _card("machine-state")
    assert cov.card_coverage(card, frozenset())["missing_concepts"] == ["machine-state"]
    assert cov.card_coverage(card, frozenset({"machine-state"})) == {
        "card_id": "machine-state", "satisfiable": True, "missing_concepts": []}



# --------------------------------------------------------------------------
# thread_coverage — per-card + inverted gaps (blocks_cards sorted) + summary
# --------------------------------------------------------------------------

def test_thread_coverage_all_uncovered():
    out = cov.thread_coverage(frozenset())
    # Fifteen seed cards now: spare-parts-on-hand, oee (gauge), quality-by-line
    # (bar), the four OT-native KPI cards (throughput, machine-speed,
    # scrap-waste, runtime-hours), the machine-state distribution bar, the
    # concept-driven work-orders-by-status and output-by-line bars, the
    # multi-site scrap-by-line and throughput-by-line bars, plus the
    # performance gauge, active-alarms KPI, and inspections-by-result bar.
    assert out["summary"] == {"total": 15, "satisfiable": 0, "uncovered": 15}
    # gaps sorted by concept
    concepts = [g["concept"] for g in out["gaps"]]
    assert concepts == sorted(concepts)
    assert {"mro-inventory", "oee-score", "quality-yield-rate",
            "throughput", "machine-speed", "scrap-waste",
            "runtime-hours", "machine-state", "work-order",
            "failure-mode", "inspection-result"} <= set(concepts)
    for g in out["gaps"]:
        assert g["suggestion"] == f"Onboard a system providing {g['concept']}"


def test_thread_coverage_partial_with_mro():
    out = cov.thread_coverage(frozenset({"mro-inventory"}))
    cards = {c["card_id"]: c for c in out["cards"]}
    assert cards["spare-parts-on-hand"]["satisfiable"] is True
    assert cards["oee"]["satisfiable"] is False
    # The gauge unions the stored shortcut (oee-score) with the sub-readings; the
    # oee-quality yield contributes its quality-yield-rate prefer_stored shortcut.
    assert cards["oee"]["missing_concepts"] == [
        "availability", "oee-score", "performance", "quality-yield-rate"]
    # spare-parts satisfiable (mro-inventory covered); the other fourteen uncovered.
    assert out["summary"] == {"total": 15, "satisfiable": 1, "uncovered": 14}
    gap = next(g for g in out["gaps"] if g["concept"] == "oee-score")
    assert gap == {"concept": "oee-score", "blocks_cards": ["oee"],
                   "suggestion": "Onboard a system providing oee-score"}


def test_thread_coverage_blocks_cards_sorted():
    # Inject two cards that both miss the same concept; blocks_cards is in
    # list_cards() order. Use the metric layer's real seed via card_layer stub.
    class _CL:
        def list_cards(self):
            return [_card("oee"), _card("spare-parts-on-hand")]
    out = cov.thread_coverage(frozenset(), card_layer=_CL())
    # mro-inventory blocks only spare-parts; oee-score blocks only oee
    blocks = {g["concept"]: g["blocks_cards"] for g in out["gaps"]}
    assert blocks["oee-score"] == ["oee"]
    assert blocks["mro-inventory"] == ["spare-parts-on-hand"]
    # gaps overall sorted by concept
    assert [g["concept"] for g in out["gaps"]] == sorted(blocks)


def test_thread_coverage_all_satisfiable():
    # All fifteen seed cards satisfiable: spare-parts (mro-inventory), the oee gauge
    # (composite + the three sub-readings), quality-by-line (quality-yield-rate), the
    # four OT-native KPIs (throughput, machine-speed, scrap-waste, runtime-hours),
    # the machine-state distribution bar (machine-state), work-orders-by-status
    # (work-order), output-by-line (actual-quantity), the multi-site
    # scrap-by-line (scrap-waste) and throughput-by-line (throughput) bars, the
    # performance gauge (performance stored + the two speed concepts), the
    # active-alarms KPI (failure-mode), and inspections-by-result
    # (inspection-result).
    covered = frozenset({"mro-inventory", "availability", "performance",
                         "quality-metric", "actual-quantity", "quality-yield-rate",
                         "throughput", "machine-speed", "scrap-waste",
                         "runtime-hours", "machine-state", "work-order",
                         "target-machine-speed", "failure-mode",
                         "inspection-result"})
    out = cov.thread_coverage(covered)
    assert out["summary"] == {"total": 15, "satisfiable": 15, "uncovered": 0}
    assert out["gaps"] == []


# --------------------------------------------------------------------------
# Import purity (ADR 0001): no boto3/LLM symbol at module load. The only impure
# seam is the lazily-invoked, injectable neptune_query reference.
# --------------------------------------------------------------------------

def test_module_is_import_pure():
    assert not hasattr(cov, "boto3")
    assert not hasattr(cov, "bedrock_call")
    assert not hasattr(cov, "bedrock_stream")
    # neptune_query is referenced as an injectable default seam (not invoked at import)
    assert callable(cov.neptune_query)
