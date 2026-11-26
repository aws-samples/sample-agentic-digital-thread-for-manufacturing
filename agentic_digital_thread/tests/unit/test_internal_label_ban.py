# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Internal labels never reach the user — standing-prompt ban + clean canned text.

Observed (rehearsal R2, module 3): the refusal answer opened with step
narration — "Now I have the run start time. ... However, looking at the
connected systems in the route seed ..." — before the pinned refusal
sentence. The only ban lived in the route-seed header (per-turn user message),
so the standing instructions never forbade the label and never forbade the
narration that carried it. The ban now lives in the system prompt for every
mode, names every internal label, and forbids narrating steps; the pinned
refusal addendum forbids anything before its sentence.
"""

import re

import pytest

import agents.orchestrator as orch
from agents.demo import workshop


def _contains_label(text: str) -> list[str]:
    low = text.lower()
    return [label for label in orch.INTERNAL_LABELS
            if re.search(r"\b" + re.escape(label.lower()) + r"\b", low)]


def test_internal_labels_cover_the_observed_leaks():
    assert {"route seed", "system prompt", "kg_context"} <= set(orch.INTERNAL_LABELS)


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
@pytest.mark.parametrize("workshop_on", [False, True])
def test_standing_prompt_bans_every_internal_label(monkeypatch, mode, workshop_on):
    if workshop_on:
        monkeypatch.setenv("ADT_WORKSHOP", "1")
    else:
        monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    prompt = orch._build_orchestrator_prompt(mode)
    assert orch._INTERNAL_LABEL_RULE in prompt
    # The rule sits inside the never-mention-internals section of the prompt.
    section_start = prompt.index("NEVER mention internal implementation details")
    assert prompt.index(orch._INTERNAL_LABEL_RULE) > section_start
    for label in orch.INTERNAL_LABELS:
        assert f'"{label}"' in orch._INTERNAL_LABEL_RULE


def test_rule_forbids_step_narration_and_names_the_plain_phrase():
    rule = orch._INTERNAL_LABEL_RULE.lower()
    assert "narrat" in rule
    assert "the connected systems" in rule


def test_pinned_refusal_sentence_names_no_internal_label():
    assert _contains_label(workshop.PINNED_MAINTENANCE_REFUSAL) == []


def test_refusal_addendum_names_no_internal_label_and_forbids_a_preamble():
    addendum = workshop.refusal_addendum()
    assert _contains_label(addendum) == []
    low = addendum.lower()
    assert "nothing comes before" in low
    assert "narrat" in low


def test_route_seed_origin_note_plain_phrases_are_not_labels():
    # The phrases the note tells the model to SAY must themselves be clean.
    said = re.findall(r'Say "([^"]+)" or "([^"]+)"', orch._route_seed_origin_note())
    assert said, "origin note no longer names the plain phrases"
    for phrase in said[0]:
        assert _contains_label(phrase) == []
