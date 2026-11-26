# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A question about why something happened asks for its records through now.

Participant walk, 2026-11-02, module 5 step 2: asked for the likely root cause
of a failed batch, the plan asked the maintenance system for its history over
"the two weeks before" the batch ran. The system queried work orders reported
before the next day, so the emergency repair order, reported at the failure
just after midnight and carrying the recorded root cause, never came back. The
answer then named a hypothesis as the cause and left out the repair. When the
asked windows reached past the event (the same question's first step, and its
epilogue), the repair order and its recorded cause came back.

The rule: asked why a past event happened or what caused it, extend each
planned records search, not sensor readings, to now unless the user names a
window, because repairs and recorded causes are often logged later. Only a
why or cause question triggers it, and it moves only the end of searches the
plan already chose, so their subject and start stay. Sensor readings keep the event's own
window (the history bullet's rule): a sampled read over a longer window would
thin the points around the event. It rides Step 1 directly after the
containment rule, in both modes and both Quick variants.

These pin wording, not model behaviour. Offline and pure: no graph, no model.
"""
from __future__ import annotations

import re

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")
RULE = ("asked why a past event happened or what caused it, extend each planned records "
        "search, not sensor readings, to now unless the user names a window: repairs and "
        "recorded causes are often logged later")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompts():
    return ([(f"full-{m}", orch._build_orchestrator_prompt(m)) for m in MODES]
            + [(f"quick-{m}", compose_quick_system_prompt(m)) for m in MODES])


_PROMPTS = _prompts()
_IDS = [name for name, _ in _PROMPTS]


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_every_prompt_asks_for_a_cause_records_through_now_once(name, prompt):
    flat = _flat(prompt).lower()
    assert flat.count(RULE) == 1, name


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_the_rule_rides_right_after_the_containment_rule(name, prompt):
    flat = _flat(prompt).lower()
    containment_end = "is not a containment."
    assert flat.index(containment_end) < flat.index(RULE), name


def test_a_window_the_user_names_still_wins():
    """The rule widens only a window the user did not give."""
    assert "unless the user names a window" in RULE


def test_sensor_readings_keep_the_events_own_window():
    """The widening covers related records, never sensor readings, whose
    history reads keep the event's own timestamps."""
    assert "records search, not sensor readings" in RULE
    full = _flat(orch._build_orchestrator_prompt("agent")).lower()
    assert "pass the event's own timestamps as an explicit window" in full


def test_only_a_why_or_cause_question_triggers_it():
    """A trigger such as "to explain a past event" would leave open whether a
    descriptive question counts. The trigger is a why or what-caused-it
    question, so a question about what happened, or one with its own window,
    leaves the plan's searches as they are."""
    assert RULE.startswith("asked why a past event happened or what caused it,")
    assert "to explain" not in RULE


def test_it_moves_only_the_end_of_searches_the_plan_already_chose():
    """Asking for "its related records" would let runs choose different
    systems or record families. The rule extends each planned search to now
    and adds none, so its subject and start stay."""
    assert "extend each planned records search" in RULE
    assert "to now" in RULE
    assert "related records" not in RULE
    assert "through now" not in RULE


def test_the_rule_names_no_story_or_dataset_literal():
    assert not re.search(r"\d|_|`", RULE)
    for word in ("bearing", "vibration", "machine", "batch", "work order", "pm", "cmms",
                 "maintenance", "deferred"):
        assert not re.search(rf"\b{word}\b", RULE), word
    assert "\u2014" not in RULE and "\u2013" not in RULE
