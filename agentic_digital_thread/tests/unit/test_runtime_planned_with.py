# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``planned_with`` route-seed capture in runtime_app (Task A1 / D3).

``_planned_with_from_seed`` derives the additive routing summary from the
Mode-A route seed: it parses the JSON after the ``"ROUTE SEED RESULT:\\n"``
marker (BOTH the full and PARTIAL headers end with that literal), aggregates
JoinPath/ValueMapping/IdentityRule counts across the seed's systems, and
dedupes shared-concept names. Fail-soft: Mode B seeds (no marker), empty
systems, and ANY parse failure ⇒ ``None`` — the summary key stays absent
(HONESTY: no route seed this turn ⇒ no ``planned_with`` key).

``_graph_first_prelude`` returns it as a third element; ``_new_turn_state``
stores it so ``_build_summary_payload`` threads it to the ONE builder on the
legacy path (RESPONSE_INTEGRITY_MODE unset / report-only).

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import agents.orchestrator as orch
from agents import identifier_translation as it
from agents import runtime_app, self_learning

SEED_PAYLOAD = {"systems": [
    {"id": "sys-a",
     "shared_concepts": [{"concept": "work order", "peer_system": "sys-b"}],
     "rules": {"join_paths": [{"id": "jp-1"}], "value_mappings": [],
               "identity_rules": [{"id": "ir-1"}, {"id": "ir-2"}]}},
    {"id": "sys-b",
     "shared_concepts": [{"concept": "work order", "peer_system": "sys-a"},
                         {"concept": "asset", "peer_system": "sys-a"}],
     "rules": {"join_paths": [], "value_mappings": [{"from_system": "sys-a"}],
               "identity_rules": []}},
]}

EXPECTED = {"systems": 2, "shared_concepts": ["work order", "asset"],
            "join_paths": 1, "value_mappings": 1, "identity_rules": 2}


# --------------------------------------------------------------------------- #
# _planned_with_from_seed — aggregation, dedupe, both headers, fail-soft
# --------------------------------------------------------------------------- #
def test_seed_counts_aggregated_and_concepts_deduped():
    seed = orch._ROUTE_SEED_HEADER + json.dumps(SEED_PAYLOAD)
    assert runtime_app._planned_with_from_seed(seed) == EXPECTED


def test_partial_header_parses_identically():
    # The PARTIAL header also ends with the "ROUTE SEED RESULT:\n" literal.
    seed = orch._ROUTE_SEED_PARTIAL_HEADER + json.dumps(SEED_PAYLOAD)
    assert runtime_app._planned_with_from_seed(seed) == EXPECTED


def test_compact_seed_entries_without_rule_keys_count_zero():
    seed = orch._ROUTE_SEED_HEADER + json.dumps(
        {"systems": [{"id": "sys-a"}], "inventory_compact": True})
    assert runtime_app._planned_with_from_seed(seed) == {
        "systems": 1, "shared_concepts": [], "join_paths": 0,
        "value_mappings": 0, "identity_rules": 0}


_TRANSLATION_RULE = {"id": "ir-1", "source_system": "sys-a", "source_field": "code",
                     "source_pattern": "X-{id}", "target_system": "sys-b",
                     "target_field": "item_id", "target_pattern": "PLANT-2026-0{id}",
                     "confidence": 0.85, "method": "value_core_match"}


def test_trailing_translations_block_after_the_json_still_parses():
    # Regression (Wave 4 / Task C finding): the seed appends an IDENTIFIER
    # TRANSLATIONS block after the JSON; json.loads over the whole tail
    # raised "Extra data" and silently dropped planned_with.
    block = it.render(it.find_translations("Which runs used X-2847?", [_TRANSLATION_RULE]))
    assert block.startswith(it.HEADER)
    seed = orch._ROUTE_SEED_HEADER + json.dumps(SEED_PAYLOAD) + "\n" + block
    assert runtime_app._planned_with_from_seed(seed) == EXPECTED


def test_trailing_wildcard_note_after_the_json_still_parses():
    # ADR-0086: every seed with a {*} IdentityRule ends with the note.
    seed = orch._ROUTE_SEED_HEADER + json.dumps(SEED_PAYLOAD) + "\n" + it.WILDCARD_SEED_NOTE
    assert runtime_app._planned_with_from_seed(seed) == EXPECTED
    block = it.render(it.find_translations("Which runs used X-2847?", [_TRANSLATION_RULE]))
    both = (orch._ROUTE_SEED_HEADER + json.dumps(SEED_PAYLOAD) + "\n" + block
            + it.WILDCARD_SEED_NOTE)
    assert runtime_app._planned_with_from_seed(both) == EXPECTED


def test_fail_soft_paths_return_none():
    # No marker (Mode B graph-first seed shape), malformed JSON, empty
    # systems (emergency seed), and non-string input all yield None.
    assert runtime_app._planned_with_from_seed("routed_system_schemas: {}") is None
    assert runtime_app._planned_with_from_seed(
        orch._ROUTE_SEED_HEADER + "{not json") is None
    assert runtime_app._planned_with_from_seed(
        orch._ROUTE_SEED_HEADER + '{"systems": []}') is None
    assert runtime_app._planned_with_from_seed(None) is None


# --------------------------------------------------------------------------- #
# authority_rules — T1.6 additive, HONEST-NULL: counted only when the seed
# carries rules.authority_rules (the builder's consulted marker); legacy
# seeds (all fixtures above) omit the key — pinned by EXPECTED == equality.
# --------------------------------------------------------------------------- #
def _seed_with(systems: list) -> str:
    return orch._ROUTE_SEED_HEADER + json.dumps({"systems": systems})


def test_authority_rules_counted_across_systems():
    planned = runtime_app._planned_with_from_seed(_seed_with([
        {"id": "sys-a",
         "rules": {"join_paths": [], "value_mappings": [], "identity_rules": [],
                   "authority_rules": [{"id": "ar-1", "concept": "machine-state"},
                                       {"id": "ar-2", "concept": "downtime"}]}},
        {"id": "sys-b",
         "rules": {"join_paths": [], "value_mappings": [], "identity_rules": [],
                   "authority_rules": []}},
    ]))
    assert planned["authority_rules"] == 2
    assert planned["systems"] == 2


def test_authority_rules_zero_when_consulted_but_none_loaded():
    # Key present with empty lists everywhere ⇒ honest 0, not absent.
    planned = runtime_app._planned_with_from_seed(_seed_with([
        {"id": "sys-a",
         "rules": {"join_paths": [{"id": "jp-1"}], "value_mappings": [],
                   "identity_rules": [], "authority_rules": []}},
    ]))
    assert planned["authority_rules"] == 0
    assert planned["join_paths"] == 1


def test_authority_rules_absent_on_legacy_and_hostile_seeds():
    # Legacy seed (no authority_rules key anywhere) ⇒ key ABSENT — the
    # pre-T1.6 output shape is byte-identical (EXPECTED above has no key).
    seed = orch._ROUTE_SEED_HEADER + json.dumps(SEED_PAYLOAD)
    assert runtime_app._planned_with_from_seed(seed) == EXPECTED
    # A hostile non-list value is unknown, never coerced to a number.
    planned = runtime_app._planned_with_from_seed(_seed_with([
        {"id": "sys-a", "rules": {"authority_rules": "many"}},
    ]))
    assert "authority_rules" not in planned


# --------------------------------------------------------------------------- #
# _graph_first_prelude — third element per mode; fail-safe unchanged
# --------------------------------------------------------------------------- #
def test_prelude_mode_a_returns_planned_with(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.setattr(
        orch, "build_route_seed",
        lambda candidate=None, question=None: orch._ROUTE_SEED_HEADER + json.dumps(SEED_PAYLOAD))
    prompt = "How many open work orders?"
    augmented, events, planned_with = runtime_app._graph_first_prelude(prompt)
    assert augmented.rstrip().endswith(prompt)
    assert len(events) == 2
    assert planned_with == EXPECTED


def test_prelude_mode_b_planned_with_is_none(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setattr(orch, "build_graph_first_seed", lambda: "SEED")
    _augmented, events, planned_with = runtime_app._graph_first_prelude("hello")
    assert len(events) == 2
    assert planned_with is None


def test_prelude_failure_and_empty_seed_yield_none(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")

    def _boom(candidate=None, question=None):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(orch, "build_route_seed", _boom)
    assert runtime_app._graph_first_prelude("hi") == ("hi", [], None)
    monkeypatch.setattr(orch, "build_route_seed", lambda candidate=None, question=None: None)
    assert runtime_app._graph_first_prelude("hi") == ("hi", [], None)


def test_prelude_seam_tolerates_legacy_two_tuple_stub(monkeypatch):
    """The prelude is a documented monkeypatch seam; 2-tuple stubs carry no
    planned_with (protects existing test-module stubs)."""
    monkeypatch.setattr(runtime_app, "_graph_first_prelude", lambda p: (p, []))
    assert runtime_app._prelude_with_planned("hi") == ("hi", [], None)


# --------------------------------------------------------------------------- #
# Legacy path — _new_turn_state stores it; _build_summary_payload threads it
# --------------------------------------------------------------------------- #
def _legacy_state(planned_with) -> SimpleNamespace:
    return SimpleNamespace(tool_calls_log=[], tool_results={},
                           rule_ids=set(), matched_rows=[],
                           planned_with=planned_with)


def test_new_turn_state_stores_planned_with(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(
        runtime_app, "_graph_first_prelude",
        lambda p: (p, [], dict(EXPECTED)))
    s = runtime_app._new_turn_state(
        {"prompt": "hello", "sessionId": "s", "userId": "u"},
        SimpleNamespace(session_id="rt"))
    assert s.planned_with == EXPECTED


def test_legacy_summary_payload_carries_planned_with(monkeypatch):
    # Legacy path serves BOTH RESPONSE_INTEGRITY_MODE unset and report-only
    # (single source: agents/config.py) — the payload is mode-independent.
    for mode in (None, "report-only"):
        if mode is None:
            monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
        else:
            monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)
        monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
        payload = runtime_app._build_summary_payload(
            _legacy_state(dict(EXPECTED)), None)
        assert payload["planned_with"] == EXPECTED


def test_legacy_summary_payload_omits_planned_with_when_none(monkeypatch):
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    payload = runtime_app._build_summary_payload(_legacy_state(None), None)
    assert "planned_with" not in payload
    # Defensive: a state WITHOUT the attribute (older fabricated states) also
    # omits the key rather than raising.
    bare = SimpleNamespace(tool_calls_log=[], tool_results={},
                           rule_ids=set(), matched_rows=[])
    payload = runtime_app._build_summary_payload(bare, None)
    assert "planned_with" not in payload
