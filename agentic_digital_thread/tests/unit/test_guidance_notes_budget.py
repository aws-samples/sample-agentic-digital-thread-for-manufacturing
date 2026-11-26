# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The stored guidance cap budgets steward notes, not the generic base.

The base is the connection-type default prose. Delivery already gives it only
the room the notes leave (``fit_delivery_sections``) and the system agent's
mirror never carries it, so counting it against the stored cap only shrank
the room for notes: a long base left a few hundred characters before an
answer requirement started evicting query hints.
"""
from __future__ import annotations

import pytest

import agents.guidance_acceptance as acceptance
import agents.self_learning as learning
from agents import guidance_scope as gs

CAP = acceptance.MAX_GUIDANCE_LEN
LONG_BASE = "Generic connection-type usage prose. " * 80   # ~3,000 chars


def _document(base: str, *, hints=(), requirements=(), legacy=()) -> str:
    document = gs.empty_document(base)
    document["query_hints"] = list(hints)
    document["answer_requirements"] = list(requirements)
    document["legacy_unscoped"] = list(legacy)
    return gs.serialize_document(document)


def _notes_length(text: str) -> int:
    return len(gs.delivery_document(text))


def test_long_base_is_longer_than_the_room_it_used_to_leave():
    assert len(LONG_BASE.strip()) > 2_800


@pytest.mark.parametrize("base", ["", "Short base.", LONG_BASE, LONG_BASE * 3],
                         ids=["no-base", "short-base", "long-base", "base-over-cap"])
def test_notes_up_to_the_cap_fit_whatever_the_base(base):
    hints = [f"hint {i}: use the bounded query path for table_x" for i in range(10)]
    current = _document(base, hints=hints)
    room = CAP - _notes_length(current) - len('""')   # first entry, no comma
    requirement = "R" * room
    preview = acceptance.preview_guidance(
        current, requirement, max_len=CAP, scope="answer_requirement")
    assert preview["outcome"] == "append"
    assert preview["evicted_note_digests"] == []
    result = gs.evolve_document(current, requirement, "answer_requirement", CAP)
    assert result is not None
    assert _notes_length(result) == CAP
    assert gs.parse_document(result)["base"] == base.strip()
    assert gs.parse_document(result)["query_hints"] == hints


def test_one_char_past_the_cap_still_evicts():
    current = _document(LONG_BASE, hints=["hint a"])
    room = CAP - _notes_length(current) - len('""')
    preview = acceptance.preview_guidance(
        current, "R" * (room + 1), max_len=CAP, scope="answer_requirement")
    assert preview["outcome"] == "would_evict"
    assert preview["evicted_note_digests"] == [acceptance.opaque_digest("hint a")]


def test_a_base_longer_than_the_cap_does_not_block_notes():
    current = _document("B" * (CAP + 500))
    preview = acceptance.preview_guidance(
        current, "Return exact totals.", max_len=CAP, scope="answer_requirement")
    assert preview["outcome"] == "append"


def test_the_base_is_kept_verbatim_and_never_evicted():
    current = _document(LONG_BASE, hints=["h" * 1_500], legacy=["l" * 1_500])
    result = gs.evolve_document(current, "r" * 1_500, "answer_requirement", CAP)
    assert result is not None
    assert gs.parse_document(result)["base"] == LONG_BASE.strip()
    assert _notes_length(result) <= CAP
    assert len(result) > CAP          # the stored document may exceed the cap


@pytest.mark.parametrize("added_scope", ["query_hint", "answer_requirement"])
def test_eviction_order_is_unchanged_and_independent_of_the_base(added_scope):
    notes = {"legacy": ["legacy old", "legacy new"],
             "hints": ["hint old", "hint new"],
             "requirements": ["requirement old", "requirement new"]}
    small_cap = _notes_length(_document("", hints=notes["hints"],
                                        requirements=notes["requirements"],
                                        legacy=notes["legacy"])) + 10
    added = "x" * 40
    evicted = {}
    for base in ("", LONG_BASE):
        current = _document(base, hints=notes["hints"],
                            requirements=notes["requirements"],
                            legacy=notes["legacy"])
        result = gs.evolve_document(current, added, added_scope, small_cap)
        assert result is not None
        assert gs.contains_note(result, added, added_scope)
        evicted[base] = [text for _scope, text in gs.evicted_entries(current, result)]
    assert evicted[""] == evicted[LONG_BASE]
    # Legacy goes first, then the OLDEST query hint; answer requirements and
    # the newer hint survive.
    assert set(evicted[""]) == {"legacy old", "legacy new", "hint old"}


def test_query_hints_go_before_answer_requirements():
    current = _document(LONG_BASE, hints=["h" * 1_800], requirements=["r" * 1_800])
    result = gs.evolve_document(current, "n" * 500, "answer_requirement", CAP)
    assert result is not None
    document = gs.parse_document(result)
    assert document["query_hints"] == []
    assert document["answer_requirements"] == ["r" * 1_800, "n" * 500]


def test_budget_status_counts_notes_not_the_base():
    current = _document("B" * (CAP + 500), hints=["hint a"])
    status = learning.guidance_budget_status(current)
    assert status["over_budget"] is False
    assert status["length"] == _notes_length(current)
    assert status["stored_length"] == len(current)
    over = _document(LONG_BASE, hints=["h" * CAP])
    status = learning.guidance_budget_status(over)
    assert status["over_budget"] is True
    assert status["over_by"] == _notes_length(over) - CAP
