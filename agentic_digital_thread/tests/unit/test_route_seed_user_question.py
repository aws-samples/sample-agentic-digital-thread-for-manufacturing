# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route seed reads the question the user typed, not the enriched turn.

The app sends the runtime the enriched turn as ``prompt`` (a
``[Temporal context: ...]`` line, with a plant selected a ``[Plant context:
...]`` line, then the question) and the user's own question as ``rawPrompt``.
The route seed ranks each system's rules by how many of the question's words
their field and concept names contain, and the Knowledge Graph trace step is
labelled with the question. Both used to read ``prompt``, so:

* the time line's own words (date, time, start, shift, record, system, ...)
  counted as question words on every turn, ranking rules with date and time
  fields ahead of rules that match what the user asked about;
* the trace step read "...to answer: [Temporal context: Current UTC time: ..."
  and was cut before the user's words (live rehearsal run, 2026-10-16).

They now read ``rawPrompt``, byte for byte, never a parse of ``prompt``: a
question the user starts with their own ``[Temporal context: ...]`` text keeps
it. The model still gets the whole enriched ``prompt``.
"""
import asyncio
import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agents import orchestrator as orch
from agents import runtime_app
from agents.time_context import render_time_context

_REAL_BUILD_ROUTE_SEED = orch.build_route_seed

_QUESTION = ("What is the total value of parts shipped to customers from the batches "
             "that drew the same supplier lot as batch X-2847?")
_PLANT = ("[Plant context: Plant A — scope Neptune queries to systems at this plant. "
          "If data isn't found here, mention that and ask if the user wants to search other "
          "plants.]")
_NOW = datetime(2026, 10, 16, 3, 13, 8, tzinfo=timezone.utc)
# A question that itself starts with text shaped like a server preamble.
_OWN_BRACKET = "[Temporal context: previous shift] Which supplier lots failed?"


def _enriched(question=_QUESTION, plant=True):
    """The ``prompt`` shape ``server._prepare_question`` sends the runtime."""
    time_line = render_time_context("America/Denver", "en-US", now=_NOW)
    return f"{time_line} {_PLANT + ' ' if plant else ''}{question}"


def _rule(rule_id, concept, source_field, target_field):
    return {"id": rule_id, "concept": concept, "source_system": "mes-x",
            "source_field": source_field, "source_pattern": "X-{id}",
            "target_system": "erp-x", "target_field": target_field,
            "target_pattern": "PLANT-2026-0{id}", "confidence": 0.5,
            "method": "value_core_match", "provenance": "50 of 50 sampled value pairs"}


def _system_row(system_id, name, **over):
    row = {"system_id": system_id, "system_name": name, "plants": [], "capabilities": None,
           "connection_type": None, "query_guidance": None}
    row.update(over)
    return row


def _fake_query(rules):
    inventory = [_system_row("mes-x", "Line MES"), _system_row("erp-x", "Plant ERP")]
    rows = [_system_row("mes-x", "Line MES", mapped_concepts=["batch"],
                        shared_concepts=[{"concept": "material_lot", "peer_system": "erp-x"}],
                        join_paths=[], value_mappings=[], identity_rules=rules)]

    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return query


def _capture_question(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    seen = []

    def fake_build(candidate=None, question=None):
        seen.append(question)
        return None  # no seed: the prelude falls back to the unseeded turn
    monkeypatch.setattr(orch, "build_route_seed", fake_build)
    return seen


def _seeded(monkeypatch, prompt, rules, **kwargs):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    q = _fake_query(rules)
    monkeypatch.setattr(orch, "build_route_seed",
                        lambda candidate=None, question=None: _REAL_BUILD_ROUTE_SEED(
                            query=q, question=question))
    return runtime_app._graph_first_prelude(prompt, **kwargs)


def _seed_rule_ids(augmented):
    fragment = augmented.split(runtime_app._ROUTE_SEED_RESULT_MARKER, 1)[1].lstrip()
    payload = json.JSONDecoder().raw_decode(fragment)[0]
    return [rule["id"] for system in payload["systems"]
            for rule in (system.get("rules") or {}).get("identity_rules") or []]


def _trace_label(events):
    return events[0]["current_tool_use"]["input"]["question"]


# ── the prelude ──────────────────────────────────────────────────────────────

def test_the_seed_builder_gets_the_raw_question_not_the_enriched_prompt(monkeypatch):
    seen = _capture_question(monkeypatch)
    runtime_app._graph_first_prelude(_enriched(), question=_QUESTION)
    assert seen == [_QUESTION]


def test_with_no_raw_question_the_prompt_is_the_question(monkeypatch):
    seen = _capture_question(monkeypatch)
    runtime_app._graph_first_prelude(_QUESTION)
    assert seen == [_QUESTION]


def test_rules_rank_by_the_users_words_not_the_time_line(monkeypatch):
    # Same tier and confidence: only question-word overlap orders them. The
    # time line's words (date, time, start, shift) match the first rule; the
    # user's word "supplier" matches the second.
    rules = [_rule("ir-clock", "shift", "event_date", "start_time"),
             _rule("ir-supplier", "supplier", "supplier_batch", "material_lot")]
    augmented, _events, _planned = _seeded(monkeypatch, _enriched(), rules, question=_QUESTION)
    assert _seed_rule_ids(augmented) == ["ir-supplier", "ir-clock"]


def test_the_model_still_gets_the_whole_enriched_prompt(monkeypatch):
    prompt = _enriched()
    rules = [_rule("ir-supplier", "supplier", "supplier_batch", "material_lot")]
    augmented, _events, _planned = _seeded(monkeypatch, prompt, rules, question=_QUESTION)
    assert augmented.endswith(prompt)


def test_the_trace_step_is_labelled_with_the_users_question(monkeypatch):
    rules = [_rule("ir-supplier", "supplier", "supplier_batch", "material_lot")]
    _augmented, events, _planned = _seeded(monkeypatch, _enriched(), rules, question=_QUESTION)
    label = _trace_label(events)
    assert "Temporal context" not in label and "Plant context" not in label
    assert "What is the total value of parts shipped" in label


def test_a_users_own_bracketed_prefix_reaches_the_builder_byte_for_byte(monkeypatch):
    # A parse of the enriched prompt would also remove the user's own
    # "[Temporal context: ...]" text, and how much it removed depended on
    # whether a plant was selected. The raw question has no such ambiguity.
    seen = _capture_question(monkeypatch)
    for plant in (False, True):
        runtime_app._graph_first_prelude(_enriched(_OWN_BRACKET, plant=plant),
                                         question=_OWN_BRACKET)
    assert seen == [_OWN_BRACKET, _OWN_BRACKET]


def test_a_saved_prompt_with_that_prefix_is_kept_whole(monkeypatch):
    # Saved prompts, scheduled runs and try-it send the text with no enrichment.
    seen = _capture_question(monkeypatch)
    runtime_app._graph_first_prelude(_OWN_BRACKET)
    assert seen == [_OWN_BRACKET]


# ── rawPrompt reaches the prelude from both runtime entry points ─────────────

def test_routing_kwargs_take_raw_prompt_only_when_it_differs():
    kw = runtime_app._routing_question_kwargs
    assert kw({"prompt": "p", "rawPrompt": "q"}, "p") == {"question": "q"}
    assert kw({"prompt": "q", "rawPrompt": "q"}, "q") == {}
    assert kw({"prompt": "p"}, "p") == {}
    assert kw({"prompt": "p", "rawPrompt": "  "}, "p") == {}
    assert kw({"prompt": "p", "rawPrompt": 7}, "p") == {}


def test_the_legacy_entry_point_routes_on_raw_prompt(monkeypatch):
    # No plant: a parse of the prompt would peel the server's time line AND the
    # user's own bracket.
    seen = _capture_question(monkeypatch)
    prompt = _enriched(_OWN_BRACKET, plant=False)
    payload = {"prompt": prompt, "rawPrompt": _OWN_BRACKET, "sessionId": "s-1"}
    state = runtime_app._new_turn_state(payload, SimpleNamespace(session_id="rt"))
    assert seen == [_OWN_BRACKET]
    assert state.prompt.endswith(prompt)


def test_the_legacy_entry_point_without_raw_prompt_is_unchanged(monkeypatch):
    seen = _capture_question(monkeypatch)
    runtime_app._new_turn_state({"prompt": _QUESTION, "sessionId": "s-1"},
                                SimpleNamespace(session_id="rt"))
    assert seen == [_QUESTION]


def test_the_integrity_entry_point_routes_on_raw_prompt(monkeypatch):
    seen = _capture_question(monkeypatch)

    def build_orchestrator(**_kwargs):
        raise RuntimeError("stop after the prelude")

    payload = {"prompt": _enriched(), "rawPrompt": _QUESTION, "sessionId": "s-1"}

    async def run():
        return [event async for event in runtime_app._invoke_integrity_runtime(
            payload, None, build_orchestrator)]

    asyncio.run(run())
    assert seen == [_QUESTION]


def test_both_entry_points_take_the_routing_question_from_the_payload():
    for fn in (runtime_app._invoke_integrity_runtime, runtime_app._reserved_key_prelude):
        assert "_routing_question_kwargs(payload, raw_prompt)" in inspect.getsource(fn)
