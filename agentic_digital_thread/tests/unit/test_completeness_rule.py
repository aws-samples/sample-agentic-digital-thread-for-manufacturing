# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A list is called complete only when the results show every value came back.

Asked to list every reading of a few telemetry series over a window, the
orchestrator asked the system for "the complete, unsampled list", the history
read returned an evenly spaced sample of the stored values, the system agent
called its list complete and unsampled, and the orchestrator repeated that.
The counts that showed the sample reach the orchestrator as an engine line
(``telemetry_coverage.sample_line``) or, in Mode B, in the result itself. The
orchestrator writes the answer and the delegations, so the rule sits in its
CONSISTENCY block beside CAUSE, in both mode prompts and both Quick
instruction variants. It names no measure, asset or story: it is a general
rule about when a list may be called complete.

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

import re

import pytest

import agents.orchestrator as orch
from agents.demo import story_lookup
from agents.workflow_guidance import compose_quick_system_prompt

_MODES = ("agent", "orchestrator")

_FRAGMENTS = (
    "COMPLETENESS: call a list of readings or rows complete, all, every or unsampled only "
    "when this turn's results show that every stored value was returned and report no "
    "sample or cut.",
    "When an [engine] line or a result reports a sample (some of the stored values "
    "returned), say the list is a sample and give both counts.",
    "A system agent's own statement that it returned everything is not evidence of it.",
    "When the user asks for all or every reading, keep that scope: ask the system for every "
    "reading in the window.",
    "Never tell it that its answer must be complete or unsampled; the counts it returns "
    "decide what your answer may claim.",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompts():
    return ([(f"full-{m}", orch._build_orchestrator_prompt(m)) for m in _MODES]
            + [(f"quick-{m}", compose_quick_system_prompt(m)) for m in _MODES])


_PROMPTS = _prompts()
_IDS = [name for name, _ in _PROMPTS]


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_every_prompt_carries_the_completeness_rule_once(name, prompt):
    flat = _flat(prompt)
    for fragment in _FRAGMENTS:
        assert flat.count(fragment) == 1, (name, fragment)


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_completeness_rule_sits_between_event_order_and_cause(name, prompt):
    flat = _flat(prompt)
    assert (flat.index("EVENT ORDER:") < flat.index("COMPLETENESS:")
            < flat.index("CAUSE: two measures moving together")
            < flat.index("CURRENT STATE:")), name


def _rule_text() -> str:
    text = orch._ANSWER_CONSISTENCY_RULE
    start = text.index("COMPLETENESS:")
    return text[start:text.index("CAUSE:", start)]


def test_completeness_rule_names_no_story_identifier():
    text = _rule_text().lower()
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in text, token
    for word in ("temperature", "vibration", "spindle", "load", "coolant", "axis"):
        assert word not in text, word


def test_completeness_rule_carries_no_figure_or_identifier_shape():
    text = _rule_text()
    assert not re.search(r"\d", text)
    assert not re.search(r"\b\w+_\w+\b", text)
    assert "\u2014" not in text and "\u2013" not in text


def test_a_system_agents_claim_is_not_evidence_of_completeness():
    """The claim that showed up as fact: a system agent's statement that its
    list is complete. The rule asks for the results' own counts instead."""
    flat = _flat(_rule_text())
    assert "own statement that it returned everything is not evidence" in flat
    assert "give both counts" in flat


def test_an_all_readings_request_keeps_its_scope_in_the_delegation():
    """The rule must not read as an instruction to drop the user's "every":
    the delegation still asks for every reading, it only never makes
    completeness a property the system's answer must have."""
    flat = _flat(_rule_text())
    keep = flat.index("When the user asks for all or every reading, keep that scope")
    never = flat.index("Never tell it that its answer must be complete or unsampled")
    assert keep < never
    assert "ask the system for every reading in the window" in flat
    assert "the counts it returns decide what your answer may claim" in flat
    # The earlier wording that could be read as narrowing the request is gone.
    assert "ask for the readings and their window" not in flat


def test_cause_rule_text_is_unchanged_by_the_new_rule():
    """CAUSE keeps its own words and its place before CURRENT STATE: a cause
    backed by returned evidence stays sayable as a labelled inference."""
    text = orch._ANSWER_CONSISTENCY_RULE
    cause = _flat(text[text.index("CAUSE:"):text.index("CURRENT STATE:")])
    assert cause == (
        "CAUSE: two measures moving together over a window is an association, not proof of "
        "cause. Never present co-movement alone as showing that one drives the other or that "
        "a third factor drives both. A returned record may establish a cause; other returned "
        "evidence may support a likely cause: name that evidence and say the conclusion is an "
        "inference. Treat a system agent's unlabelled interpretation as a hypothesis, not a "
        "recorded cause. When you discuss whether co-movement shows a cause, say that "
        "co-movement alone does not establish it. Call a record unrelated to or unaffected by "
        "a cause only when returned evidence establishes that.")
