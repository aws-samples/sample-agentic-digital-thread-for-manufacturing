# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A rule stated to the system agent earlier is superseded in every later question.

The ROW EXCLUSIONS fact is sent with each question (``test_row_exclusion_live_findings``),
but the system agent is a long-lived runtime singleton whose conversation keeps earlier
delegations: a later model request still holds an earlier question's ROW EXCLUSIONS
block and its predicate. Before this change, a question after a steward's clear carried
no fact at all, so nothing told the model the earlier block no longer held; after a
replace, nothing said the new rule replaced the old one. A model could reuse the old
predicate, and the engine, with no rule to compare it to, would run the query.

Now each question's block states the current rules "as of this question" and that they
replace any stated earlier in the conversation, and names every table stated earlier
whose rule is gone (every row is a record now) or cannot be confirmed (its reads are
held). Exercised with the real Strands ``Agent`` and a scripted model, two delegations
on one agent: the second model request holds the first block in its history and the
superseding statement in its current message.

Fully offline (``_row_exclusion_graph.RxGraph``); synthetic names.
"""
from __future__ import annotations

import asyncio
import copy

import pytest
from strands import Agent
from strands.models.model import Model

import agents.system_agent as sa
from agents import record_key
from agents import row_exclusion as rx
from tests.unit._row_exclusion_graph import RxGraph

_SYS = "system-a"
_COLUMNS = {"pipeline": "varchar", "run_ref": "varchar", "state": "varchar"}
_DESC = "job_events(pipeline: varchar; run_ref: varchar; state: varchar)"
_F1 = [{"column": "pipeline", "op": "not_empty"}, {"column": "run_ref", "op": "not_empty"}]
_F2 = [{"column": "state", "op": "in", "values": ["SHADOW"]}]
_P1 = rx.predicate_sql(rx.exclusion_from("shadow copies", _F1))
_P2 = rx.predicate_sql(rx.exclusion_from("shadow copies", _F2))
_ASK = "How many steps failed?"


class _ScriptedModel(Model):
    """Answers every model request with one final text; keeps each request."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def update_config(self, **_config):
        return None

    def get_config(self):
        return {}

    def structured_output(self, *_a, **_k):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
        self.calls.append(copy.deepcopy(messages))
        for event in ({"messageStart": {"role": "assistant"}},
                      {"contentBlockDelta": {"delta": {"text": "The count is 42."}}},
                      {"contentBlockStop": {}}, {"messageStop": {"stopReason": "end_turn"}}):
            yield event


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    rx.reset()
    record_key.reset()
    yield
    rx.reset()
    record_key.reset()


def _graph() -> RxGraph:
    return RxGraph({_SYS: {"job_events": _COLUMNS}})


def _set(g, conditions):
    return rx.set_row_exclusion(_SYS, "job_events", conditions, label="shadow copies",
                                actor="dana", write=g.write, query=g.query)


def _clear(g):
    return rx.clear_row_exclusion(_SYS, "job_events", actor="dana", write=g.write,
                                  query=g.query)


def _text(message: dict) -> str:
    return "\n".join(b.get("text", "") for b in message.get("content", []) if "text" in b)


def _two_delegations(monkeypatch, g, between) -> list:
    """One real agent, two delegations through ``_run_mode_a_query`` with
    ``between`` (a steward write or a fault) in between. Returns the second
    model request's messages."""
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    sa._remember_turn_time_facts(_SYS, [], _DESC, connection_type="Athena")
    model = _ScriptedModel()
    agent = Agent(model=model, system_prompt="prompt", tools=[], callback_handler=None)
    asyncio.run(sa._run_mode_a_query(agent, _SYS, _ASK, ""))
    between()
    asyncio.run(sa._run_mode_a_query(agent, _SYS, _ASK, ""))
    assert len(model.calls) == 2
    return model.calls[1]


def _history_and_current(sent) -> tuple:
    user = [m for m in sent if m.get("role") == "user"]
    return "\n".join(_text(m) for m in user[:-1]), _text(user[-1])


def test_the_conversation_keeps_the_earlier_block(monkeypatch):
    """The premise: the second model request holds the first question's block."""
    g = _graph()
    _set(g, _F1)
    sent = _two_delegations(monkeypatch, g, lambda: None)
    history, _current = _history_and_current(sent)
    assert _P1 in history


def test_after_a_clear_the_next_question_says_the_rule_is_gone(monkeypatch):
    g = _graph()
    _set(g, _F1)
    history, current = _history_and_current(_two_delegations(monkeypatch, g, lambda: _clear(g)))
    assert _P1 in history
    assert _P1 not in current
    assert "replace any row exclusion stated earlier in this conversation" in current
    assert "job_events: no row exclusion now, so every row of it is a record" in current


def test_after_a_replace_the_next_question_states_the_new_rule_as_replacing(monkeypatch):
    g = _graph()
    _set(g, _F1)
    history, current = _history_and_current(
        _two_delegations(monkeypatch, g, lambda: _set(g, _F2)))
    assert _P1 in history and _P2 in current and _P1 not in current
    assert "replace any row exclusion stated earlier in this conversation" in current


def test_a_held_table_stated_earlier_is_named_as_not_confirmed(monkeypatch):
    """The table's entry turned pending with nothing stored (an interrupted
    change): its reads are held, and the question says the rule stated
    earlier cannot be confirmed now."""
    g = _graph()
    _set(g, _F1)

    def interrupted():
        g.store(_SYS, "job_events", None)
        g.list_table(_SYS, "job_events", token="t9", pending=True)
    _history, current = _history_and_current(_two_delegations(monkeypatch, g, interrupted))
    assert _P1 not in current
    assert "job_events: its row exclusion cannot be confirmed now, so its reads are held" in (
        current)


def test_a_rewrite_in_progress_names_the_table_as_not_confirmed(monkeypatch):
    g = _graph()
    _set(g, _F1)

    def rewriting():
        rx.begin_rewrite(_SYS, owner="run-a", query=g.query, write=g.write)
    _history, current = _history_and_current(_two_delegations(monkeypatch, g, rewriting))
    assert _P1 not in current
    assert "cannot be confirmed now" in current


def test_a_manifest_outage_names_the_table_as_not_confirmed(monkeypatch):
    g = _graph()
    _set(g, _F1)
    _history, current = _history_and_current(
        _two_delegations(monkeypatch, g, lambda: g.fail.add("manifest")))
    assert _P1 not in current
    assert "cannot be confirmed now" in current


def test_after_the_outage_a_gone_rule_is_said_to_be_gone(monkeypatch):
    """Held during an outage, cleared meanwhile: once the manifest reads
    again, the table stated earlier is named as having no rule."""
    g = _graph()
    _set(g, _F1)
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    sa._remember_turn_time_facts(_SYS, [], _DESC, connection_type="Athena")
    assert _P1 in rx.current_schema_lines(_SYS)
    g.fail.add("manifest")
    assert "cannot be confirmed now" in rx.current_schema_lines(_SYS)
    g.fail.clear()
    _clear(g)
    assert "no row exclusion now" in rx.current_schema_lines(_SYS)
    # Said again on every later question: the earlier block may still be in
    # the conversation however many questions later.
    assert "no row exclusion now" in rx.current_schema_lines(_SYS)


def test_a_rule_stated_only_in_a_refusal_is_superseded_too(monkeypatch):
    """A rule set after the question's block was built reaches the model only
    in the engine's refusal; once cleared, the next question still names it."""
    g = _graph()
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    sa._remember_turn_time_facts(_SYS, [], _DESC, connection_type="Athena")
    assert rx.current_schema_lines(_SYS) == ""
    _set(g, _F1)
    rx.refresh(_SYS)
    assert rx.decide_for(_SYS, "SELECT COUNT(*) FROM job_events") is not None   # refused
    _clear(g)
    assert "job_events: no row exclusion now" in rx.current_schema_lines(_SYS)


def test_a_system_that_never_had_a_rule_gets_no_block(monkeypatch):
    g = _graph()
    _history, current = _history_and_current(_two_delegations(monkeypatch, g, lambda: None))
    assert "ROW EXCLUSIONS" not in current


def test_a_first_rule_is_stated_without_replacing_anything(monkeypatch):
    g = _graph()
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    sa._remember_turn_time_facts(_SYS, [], _DESC, connection_type="Athena")
    _set(g, _F1)
    block = rx.current_schema_lines(_SYS)
    assert _P1 in block and "stated earlier" not in block
