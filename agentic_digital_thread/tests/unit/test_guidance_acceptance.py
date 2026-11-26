# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guidance lifecycle integrity — pure preview and strict acceptance."""
from __future__ import annotations

import agents.guidance_acceptance as acceptance
import agents.system_guidance as guidance
from agents.guidance_scope import delivery_document


class _State:
    def __init__(self, current: str):
        self.graph = current
        self.delivery = guidance.GuidanceSnapshot.present(
            delivery_document(current), 3)
        self.proposal = "pending"
        self.events = []
        self.fail_delivery = False
        self.fail_graph_restore = False

    def graph_cas(self, expected, desired):
        self.events.append(("graph_cas", expected, desired))
        if self.graph != expected:
            return False
        if self.fail_graph_restore and len([
                e for e in self.events if e[0] == "graph_cas"]) > 1:
            return False
        self.graph = desired
        return True

    def graph_read(self):
        self.events.append(("graph_read", self.graph))
        return self.graph

    def delivery_cas(self, expected, target):
        self.events.append(("delivery_cas", expected, target))
        if self.fail_delivery:
            raise guidance.GuidanceUnavailable("private detail")
        if self.delivery != expected:
            raise guidance.GuidanceConflict()
        self.delivery = guidance.GuidanceSnapshot.present(
            target.text, expected.version + 1)
        return self.delivery

    def delivery_read(self):
        self.events.append(("delivery_read", self.delivery))
        return self.delivery

    def terminal_cas(self, graph_text, delivered):
        self.events.append(("terminal_cas", graph_text, delivered))
        if self.proposal != "pending":
            return False
        self.proposal = "accepted"
        return True

    def terminal_read(self):
        self.events.append(("terminal_read", self.proposal))
        return self.proposal

    def terminal_restore(self):
        self.events.append(("terminal_restore",))
        self.proposal = "pending"
        return True

    def kwargs(self):
        return {
            "graph_compare_and_set": self.graph_cas,
            "graph_read": self.graph_read,
            "delivery_before": self.delivery,
            "delivery_compare_and_set": self.delivery_cas,
            "delivery_read": self.delivery_read,
            "terminal_compare_and_set": self.terminal_cas,
            "terminal_read": self.terminal_read,
            "terminal_restore": self.terminal_restore,
        }


def _execute(state: _State, note="new requirement", **extra):
    return acceptance.execute_guidance_acceptance(
        system_id="system-a", current=extra.pop("current", state.graph),
        note=note, confirmation=extra.pop("confirmation", None),
        **state.kwargs(), **extra)


def test_preview_append_is_deterministic_opaque_and_side_effect_free():
    current = "Base guidance. LEARNED: existing requirement"
    note = "New answer requirement"
    first = acceptance.preview_guidance(current, note, max_len=4000)
    second = acceptance.preview_guidance(current, note, max_len=4000)
    assert first == second
    assert first["outcome"] == "append"
    assert first["current_length"] == len(current)
    assert first["result_length"] > first["current_length"]
    assert first["evicted_note_digests"] == []
    assert current not in repr(first) and note not in repr(first)


def test_preview_reports_all_bounded_outcomes_without_text():
    current = "Base. LEARNED: oldest requirement LEARNED: newer requirement"
    duplicate = acceptance.preview_guidance(
        current, "newer requirement", max_len=4000, scope="legacy_unscoped")
    unavailable = acceptance.preview_guidance(None, "requirement", max_len=4000)
    no_room = acceptance.preview_guidance("B" * 30, "requirement", max_len=10)
    eviction = acceptance.preview_guidance(
        current, "replacement requirement", max_len=150)
    assert duplicate["outcome"] == "duplicate"
    assert unavailable["outcome"] == "unavailable"
    assert no_room["outcome"] == "no_room"
    assert eviction["outcome"] == "would_evict"
    assert eviction["evicted_note_digests"] == [
        acceptance.opaque_digest("oldest requirement"),
        acceptance.opaque_digest("newer requirement")]
    assert current not in repr(eviction)


def _eviction_case():
    current = "Base. LEARNED: oldest requirement LEARNED: newer requirement"
    note = "replacement requirement"
    preview = acceptance.preview_guidance(current, note, max_len=150)
    confirmation = {
        "current_digest": preview["current_digest"],
        "evicted_note_digests": preview["evicted_note_digests"],
    }
    return current, note, preview, confirmation


def test_unconfirmed_or_wildcard_eviction_never_writes():
    current, note, _preview, confirmation = _eviction_case()
    for bad in (None, {**confirmation, "force": "*"},
                {**confirmation, "current_digest": "sha256:stale"},
                {**confirmation, "evicted_note_digests": ["*"]}):
        state = _State(current)
        result = _execute(
            state, current=current, note=note, confirmation=bad, max_len=150)
        assert result["status"] == "pending"
        assert result["error"] == "guidance_update_failed"
        assert result["http_status"] == 409
        assert state.events == []


def test_exact_confirmation_orders_graph_delivery_then_terminal_readbacks():
    current, note, preview, confirmation = _eviction_case()
    state = _State(current)
    result = _execute(
        state, current=current, note=note, confirmation=confirmation, max_len=150)
    assert result["status"] == "delivered"
    assert result["outcome"] == "would_evict"
    assert result["preview"] == preview
    assert result["delivery_digest"] == state.delivery.digest
    assert result["delivery_version"] == state.delivery.version
    names = [event[0] for event in state.events]
    assert names == [
        "graph_read", "delivery_read", "graph_cas", "graph_read",
        "delivery_cas", "delivery_read", "terminal_cas", "terminal_read"]


def test_duplicate_no_room_and_unavailable_never_write():
    cases = [
        ("Base. LEARNED: same", "same", 4000, "legacy_unscoped", "duplicate"),
        ("B" * 30, "new", 10, "query_hint", "no_room"),
    ]
    for current, note, cap, scope, outcome in cases:
        state = _State(current)
        result = _execute(state, note=note, max_len=cap, scope=scope)
        assert result["status"] == "pending"
        assert result["outcome"] == outcome
        assert state.events == []


def test_preexisting_graph_and_delivery_drift_are_rejected_before_write():
    graph_state = _State("Base.")
    graph_state.graph = "Concurrent."
    graph = _execute(graph_state, current="Base.")
    assert graph["drift"] == {"graph": True, "delivery": False, "proposal": False}
    assert not any(event[0].endswith("cas") for event in graph_state.events)

    delivery_state = _State("Base.")
    delivery_state.delivery = guidance.GuidanceSnapshot.present("wrong", 3)
    delivery = _execute(delivery_state, current="Base.")
    assert delivery["drift"] == {"graph": False, "delivery": True, "proposal": False}
    assert not any(event[0].endswith("cas") for event in delivery_state.events)


def test_delivery_failure_compensates_graph_and_keeps_proposal_pending():
    state = _State("Base.")
    state.fail_delivery = True
    result = _execute(state)
    assert result == {
        "status": "pending", "error": "guidance_update_failed",
        "drift": {"graph": False, "delivery": False, "proposal": False},
        "outcome": "append", "http_status": 503,
        "preview": result["preview"]}
    assert state.graph == "Base."
    assert state.proposal == "pending"
    assert "private detail" not in repr(result)


def test_graph_compensation_failure_is_distinct_and_opaque():
    state = _State("Base.")
    state.fail_delivery = True
    state.fail_graph_restore = True
    result = _execute(state)
    assert result["status"] == "pending"
    assert result["drift"] == {"graph": True, "delivery": False, "proposal": False}
    assert state.proposal == "pending"


def test_no_guidance_noop_accepted_path_exists():
    state = _State("Base. LEARNED: same")
    result = _execute(state, note="same", scope="legacy_unscoped")
    assert result["status"] == "pending"
    assert "guidance-noop-" not in repr(result)


def test_proposal_becomes_accepted_only_after_all_readbacks(monkeypatch):
    import agents.self_learning as learning
    from unit.test_self_learning_lifecycle import FakeTable
    from unit.test_system_guidance import FakeBackend

    table = FakeTable()
    monkeypatch.setattr(learning, "_get_table", lambda: table)
    monkeypatch.setattr(learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    graph = {"guidance": "Base."}
    monkeypatch.setattr(
        learning, "_neptune_query",
        lambda *_a, **_k: [{"qg": graph["guidance"]}])

    def graph_write(cypher, params=None):
        params = params or {}
        if ("SET s.query_guidance = $desired" in cypher
                and graph["guidance"] == params.get("expected")):
            graph["guidance"] = params["desired"]
        return True

    monkeypatch.setattr(learning, "_neptune_write", graph_write)
    guidance.set_store(guidance.SystemGuidanceStore(backend=FakeBackend()))
    filed = learning.assert_vocab(
        proposed_type="guidance_note", system_id="system-a",
        note="Return the required provenance.", is_admin=False)
    accepted = learning.accept_mapping(filed["mapping_id"], actor="steward")
    assert accepted["status"] == "accepted"
    assert table.by_id(filed["mapping_id"])["status"] == "accepted"
    assert "Return the required provenance." in graph["guidance"]
    assert "Return the required provenance." in guidance.read("system-a")
