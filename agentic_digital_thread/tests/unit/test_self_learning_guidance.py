# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure guidance planning and strict prune/clear lifecycle contracts."""
from __future__ import annotations

import agents.self_learning as learning
import agents.system_guidance as guidance
from agents.guidance_scope import delivery_document, parse_document
from unit.test_system_guidance import FakeBackend


def _neg(table, column):
    return (f"{table}: no column '{column}' — use the exact concept-mapped "
            "columns from get_system_schema")


def test_consolidate_negative_notes_is_deterministic_and_idempotent():
    notes = [_neg("orders", "qty"), "keep", _neg("orders", "sku")]
    once = learning.consolidate_guidance_notes(notes)
    twice = learning.consolidate_guidance_notes(once)
    assert once == [
        "orders: avoid columns [qty, sku]; use concept-mapped columns", "keep"]
    assert twice == once


def test_evolve_consolidates_prunes_oldest_and_keeps_new_note():
    current = " ".join(
        f"{learning._LEARNED_PREFIX}{_neg('orders', f'c{i}')}"
        for i in range(8))
    evolved = learning.evolve_query_guidance(
        current, _neg("orders", "c99"), max_len=200)
    assert evolved is not None and len(evolved) <= 200
    assert "c99" in evolved and "avoid columns" in evolved

    padded = " ".join(
        f"{learning._LEARNED_PREFIX}note {i} with padding"
        for i in range(20))
    pruned = learning.evolve_query_guidance(
        padded, "freshly learned", max_len=100)
    assert pruned is not None
    assert "freshly learned" in pruned and "note 0 " not in pruned


def test_evolve_refuses_when_base_alone_exceeds_cap_and_is_idempotent():
    assert learning.evolve_query_guidance(
        "X" * 300, "new", max_len=200) is None
    current = " ".join(
        f"{learning._LEARNED_PREFIX}{_neg('orders', f'c{i}')}"
        for i in range(4))
    first = learning.evolve_query_guidance(
        current, _neg("orders", "c99"), max_len=200)
    assert learning.evolve_query_guidance(
        first, _neg("orders", "c99"), max_len=200) == first


def test_eviction_preview_names_oldest_first_without_text_logging():
    base = "BASE GUIDANCE. "
    current = base + "LEARNED: alpha. LEARNED: beta."
    evicted = learning.guidance_notes_evicted_by(
        current, "long replacement", max_len=len(base) + 35)
    assert evicted and evicted[0] == "alpha."
    assert learning.guidance_notes_evicted_by(current, "", max_len=20) == []


def test_get_all_query_guidance_parses_scopes(monkeypatch):
    monkeypatch.setattr(learning, "_neptune_query", lambda *_a, **_k: [
        {"id": "erp", "qg": "Base. LEARNED: legacy"},
        {"id": "mes", "qg": "Only base"}])
    rows = learning.get_all_query_guidance()
    assert rows[0]["system_id"] == "erp"
    assert rows[0]["legacy_unscoped"] == ["legacy"]
    assert rows[0]["learned_count"] == 1
    assert rows[1]["learned_count"] == 0


def test_get_all_query_guidance_swallows_read_error(monkeypatch):
    monkeypatch.setattr(
        learning, "_neptune_query",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("private")))
    assert learning.get_all_query_guidance() == []


def _install_strict_state(monkeypatch, current: str):
    graph = {"text": current, "events": []}

    def query(_cypher, _params=None):
        graph["events"].append("graph_read")
        return [{"qg": graph["text"]}]

    def write(_cypher, params=None):
        params = params or {}
        graph["events"].append("graph_cas")
        if graph["text"] != params.get("expected"):
            return False
        graph["text"] = params["desired"]
        return True

    monkeypatch.setattr(learning, "_neptune_query", query)
    monkeypatch.setattr(learning, "_neptune_write", write)
    backend = FakeBackend()
    backend.save("erp", delivery_document(current), 1)
    prior = guidance._store
    guidance.set_store(guidance.SystemGuidanceStore(backend=backend))
    return graph, backend, prior


def test_prune_replaces_graph_and_delivery_exactly(monkeypatch):
    current = "Base. LEARNED: keep me LEARNED: drop me"
    graph, backend, prior = _install_strict_state(monkeypatch, current)
    try:
        result = learning.prune_query_guidance_note("erp", "drop me")
        assert result == {"status": "pruned", "removed": 1, "remaining": 1}
        assert parse_document(graph["text"])["legacy_unscoped"] == ["keep me"]
        assert backend.store["erp"] == delivery_document(graph["text"])
    finally:
        guidance.set_store(prior)


def test_clear_replaces_graph_and_delivery_exactly(monkeypatch):
    current = "Onboarded base. LEARNED: a LEARNED: b"
    graph, backend, prior = _install_strict_state(monkeypatch, current)
    try:
        result = learning.clear_query_guidance("erp")
        assert result == {"status": "cleared", "removed": 2}
        document = parse_document(graph["text"])
        assert document["base"] == "Onboarded base."
        assert document["legacy_unscoped"] == []
        assert backend.store["erp"] == delivery_document(graph["text"])
    finally:
        guidance.set_store(prior)


def test_clear_without_notes_still_proves_parity(monkeypatch):
    graph, backend, prior = _install_strict_state(monkeypatch, "Just base")
    try:
        result = learning.clear_query_guidance("erp")
        assert result == {"status": "cleared", "removed": 0}
        assert backend.store["erp"] == delivery_document(graph["text"])
        assert "graph_read" in graph["events"]
    finally:
        guidance.set_store(prior)


def test_prune_and_clear_reject_preexisting_delivery_drift(monkeypatch):
    current = "Base. LEARNED: drop me"
    graph, backend, prior = _install_strict_state(monkeypatch, current)
    backend.store["erp"] = "different"
    try:
        prune = learning.prune_query_guidance_note("erp", "drop me")
        clear = learning.clear_query_guidance("erp")
        assert prune["status"] == clear["status"] == "pending"
        assert prune["drift"]["delivery"] is True
        assert clear["drift"]["delivery"] is True
        assert graph["text"] == current
    finally:
        guidance.set_store(prior)


def test_prune_missing_inputs_system_or_note(monkeypatch):
    assert "error" in learning.prune_query_guidance_note("", "note")
    assert "error" in learning.prune_query_guidance_note("erp", " ")
    monkeypatch.setattr(learning, "_neptune_query", lambda *_a, **_k: [])
    assert learning.prune_query_guidance_note("ghost", "note") == {
        "error": "System ghost not found"}


def test_prune_missing_note_does_not_mutate(monkeypatch):
    current = "Base. LEARNED: only"
    graph, _backend, prior = _install_strict_state(monkeypatch, current)
    try:
        assert learning.prune_query_guidance_note("erp", "other") == {
            "error": "note not found", "removed": 0}
        assert "graph_cas" not in graph["events"]
    finally:
        guidance.set_store(prior)


def test_get_all_query_guidance_reports_budget_overage_without_mutation(monkeypatch):
    from agents.guidance_scope import notes_length

    guidance = "Base. LEARNED: " + ("steward note " * 340)
    monkeypatch.setattr(learning, "_neptune_query", lambda *_a, **_k: [
        {"id": "erp", "qg": guidance}])

    row = learning.get_all_query_guidance()[0]

    assert row["length"] == notes_length(guidance)
    assert row["stored_length"] == len(guidance)
    assert row["max_len"] == learning._MAX_GUIDANCE_LEN
    assert row["over_budget"] is True
    assert row["over_by"] == notes_length(guidance) - learning._MAX_GUIDANCE_LEN
    assert row["notes"] == [("steward note " * 340).strip()]
