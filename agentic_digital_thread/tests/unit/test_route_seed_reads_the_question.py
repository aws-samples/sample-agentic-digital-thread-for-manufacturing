# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route seed reads the user's question, not the preambles the server adds.

The runtime builds the seed from the payload prompt, which the server composes
as ``[Temporal context: ...] [Plant context: ...] {question}``. The temporal
line is the same on every turn and names "this shift", "today", "dates",
"times" and "timestamps". Matched against the whole prompt, a vocabulary
concept named "shift" got a concept note on every question and took one of
the few note slots, and the rule ranking counted the line's words as the
question's own (a rule on a time or shift column moved up on every turn).

``memory_labels.strip_turn_preambles`` already recovers the question for
memory display and for the resolved time windows; the seed now uses it too,
so the seed for the composed prompt is the seed for the question.

Offline: graph reads use an injected fake; fixtures are synthetic and generic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import agents.concept_notes as cn
import agents.orchestrator as orch
from agents.time_context import render_time_context


@pytest.fixture(autouse=True)
def _isolated_turn_notes():
    token = cn._TURN_NOTES.set(())
    yield
    cn._TURN_NOTES.reset(token)


SHIFT = cn.Concept(id="shift", name="Shift",
                   description="A shift is one crew's working period on a line.")
STALLED = cn.Concept(id="stalled_unit", name="Stalled Unit",
                     description="A unit is stalled when it sits open past its step limit.")
VOCAB = (SHIFT, STALLED)

_NOW = datetime(2026, 3, 4, 15, 30, tzinfo=timezone.utc)


def _composed(question: str, *, plant: str = "") -> str:
    """The prompt the server sends: temporal line, optional plant line, question."""
    if plant:
        question = (f"[Plant context: {plant} — scope Neptune queries to systems at this "
                    "plant. If data isn't found here, mention that and ask if the user "
                    f"wants to search other plants.] {question}")
    return f"{render_time_context('America/Denver', 'en-US', now=_NOW)} {question}"


def _row(system_id):
    return {"system_id": system_id, "system_name": system_id.upper(), "plants": ["site-1"],
            "capabilities": f"{system_id} records", "connection_type": "PostgreSQL"}


def _join(rule_id, field):
    return {"id": rule_id, "from_system": "sys-a", "from_field": field,
            "to_system": "sys-b", "to_field": "t_b.ref", "confidence": 0.8,
            "provenance": "accepted"}


#: Two equal-tier, equal-confidence joins. Only the question's own words
#: should order them: "batch" puts the batch join first. The temporal line's
#: "shift" would tie them, and the id tie-break would put the shift join first.
_RULES = [{"system_id": "sys-a",
           "join_paths": [_join("jp-a", "t_a.shift_code"), _join("jp-b", "t_a.batch_ref")]}]


def _query(cypher, *_args, **_kwargs):
    if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
        return [_row("sys-a"), _row("sys-b")]
    if cypher == orch._ROUTE_SEED_CYPHER:
        return _RULES
    return []


def _payload(seed: str) -> dict:
    return json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]


QUESTION = "How many stalled units does batch 7 have at cell_a?"


def test_the_composed_prompt_gets_the_question_s_concept_notes():
    composed = orch.build_route_seed(query=_query, question=_composed(QUESTION), concepts=VOCAB)
    asked = orch.build_route_seed(query=_query, question=QUESTION, concepts=VOCAB)

    assert _payload(composed).get("concept_notes") == _payload(asked).get("concept_notes")
    assert "(shift)" not in _payload(composed)["concept_notes"]
    assert "(stalled_unit)" in _payload(composed)["concept_notes"]
    # A question that itself names the concept still gets its note.
    named = "Which shift had the most stalled units?"
    notes = _payload(orch.build_route_seed(
        query=_query, question=_composed(named), concepts=VOCAB))["concept_notes"]
    assert "(shift)" in notes and "(stalled_unit)" in notes


def test_the_plant_line_is_peeled_too():
    composed = orch.build_route_seed(query=_query, question=_composed(QUESTION, plant="site-1"),
                                     concepts=VOCAB)

    assert composed == orch.build_route_seed(query=_query, question=QUESTION, concepts=VOCAB)


def test_the_seed_for_the_composed_prompt_is_the_seed_for_the_question():
    composed = orch.build_route_seed(query=_query, question=_composed(QUESTION), concepts=VOCAB)

    assert composed == orch.build_route_seed(query=_query, question=QUESTION, concepts=VOCAB)
    joins = _payload(composed)["systems"][0]["rules"]["join_paths"]
    assert [j["id"] for j in joins] == ["jp-b", "jp-a"]


def test_the_turn_notes_handed_to_delegations_match_the_question():
    orch.build_route_seed(query=_query, question=_composed(QUESTION), concepts=VOCAB)

    assert [note.concept for note in cn.turn_notes()] == ["stalled_unit"]
