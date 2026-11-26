# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two measures moving together is reported as that, not as a cause.

Asked whether one reading follows another over a window, an answer called
the two "positively correlated" and offered a mechanism driving both that no
result stated. The orchestrator writes the answer, so the rule sits in its
CONSISTENCY block beside EVENT ORDER (which already stops an ordering no
compared timestamps support), in both mode prompts and both Quick
instruction variants. It names no measure, asset or story: it is a general
rule about what co-movement over a window does and does not show.

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
    "CAUSE: two measures moving together over a window is an association, not proof of cause.",
    "Never present co-movement alone as showing that one drives the other or that a third "
    "factor drives both.",
    "A returned record may establish a cause; other returned evidence may support a likely "
    "cause: name that evidence and say the conclusion is an inference.",
    "Treat a system agent's unlabelled interpretation as a hypothesis, not a recorded cause.",
    "When you discuss whether co-movement shows a cause, say that co-movement alone does not "
    "establish it.",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompts():
    return ([(f"full-{m}", orch._build_orchestrator_prompt(m)) for m in _MODES]
            + [(f"quick-{m}", compose_quick_system_prompt(m)) for m in _MODES])


_PROMPTS = _prompts()
_IDS = [name for name, _ in _PROMPTS]


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_every_prompt_carries_the_cause_rule_once(name, prompt):
    flat = _flat(prompt)
    for fragment in _FRAGMENTS:
        assert flat.count(fragment) == 1, (name, fragment)


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_cause_rule_sits_between_event_order_and_current_state(name, prompt):
    flat = _flat(prompt)
    assert (flat.index("EVENT ORDER:") < flat.index("CAUSE: two measures moving together")
            < flat.index("CURRENT STATE:")), name


def _rule_text() -> str:
    text = orch._ANSWER_CONSISTENCY_RULE
    start = text.index("CAUSE:")
    return text[start:text.index("CURRENT STATE:", start)]


def test_cause_rule_names_no_story_identifier():
    text = _rule_text().lower()
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in text, token
    for word in ("temperature", "vibration", "spindle", "load", "coolant"):
        assert word not in text, word


def test_cause_rule_carries_no_figure_or_identifier_shape():
    text = _rule_text()
    assert not re.search(r"\d", text)
    assert not re.search(r"\b\w+_\w+\b", text)
    assert "\u2014" not in text and "\u2013" not in text


def test_cause_rule_keeps_labelled_inference_from_returned_evidence():
    """A root cause backed by returned evidence (an overdue maintenance order,
    a failure note, a reading) stays sayable as a labelled inference. The rule
    restricts only co-movement offered alone as cause, and only an unlabelled
    interpretation is downgraded to a hypothesis."""
    flat = _flat(_rule_text())
    assert "other returned evidence may support a likely cause" in flat
    assert "say the conclusion is an inference" in flat
    assert "co-movement alone" in flat
    # The earlier wording allowed a cause only when a record stated it, and
    # ruled out anything a system agent relayed. Neither restriction remains.
    assert "unless a returned record states that cause" not in flat
    assert "is not such a record" not in flat


def test_cause_caveat_is_conditional_on_discussing_cause():
    """The 'does not establish it' caveat is tied to answers that discuss
    whether co-movement shows a cause, not added to every answer that names
    two trends."""
    flat = _flat(_rule_text())
    assert ("When you discuss whether co-movement shows a cause, say that co-movement "
            "alone does not establish it.") in flat
    assert "Say that moving together over that window does not by itself show cause" not in flat
