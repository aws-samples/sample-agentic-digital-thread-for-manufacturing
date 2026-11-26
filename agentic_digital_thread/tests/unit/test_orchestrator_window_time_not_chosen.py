# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator gives a window and leaves its event to the system agent.

Measured in a live dry run: a percentile question named a day and no event.
The orchestrator's delegation asked the system agent for rows "whose start
timestamp falls in the window", although every earlier run of the same
question windowed on completion. The system agent owns a state-time rule
(each state's rows windowed on that state's own time) but reads a time the
delegation names as the caller's choice, so the answer covered a different
set of rows. Step 4 already says to describe WHAT is needed and never HOW to
fetch it; the delegation broke that by choosing the window's event in plain
words.

Step 4 now says, for any delegated record window, in two branches:

* when the user's own question names or plainly ties the window to an event
  (for example placed, started, shipped, completed), the delegation keeps that
  choice;
* otherwise it gives only the window and lets the system agent apply its own
  time rule (the orchestrator does not know whether records carry several
  times);

and in both, state and population filters the question asks for ("completed
only") stay in the delegation: only the event that places a record in the
window is left out.

These tests read the composed Mode A prompts (full, module default, Amazon
Quick, and the query-workflow tool text) and pin that meaning, not the exact
prose. Whether the model follows it cannot be shown offline; the live dry run
measures that.
"""

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt, get_workflow_guidance

_DASH = "\u2014"
_STEP_4 = f"Step 4 {_DASH} DELEGATE an intent-level, PRE-TRANSLATED sub-question:"
_STEP_5 = f"Step 5 {_DASH} SYNTHESIZE"
_NEXT_RULE = "CONFIRMED JOINS BEAT IDENTIFIER GUESSES:"
#: Short stable anchors of the existing Step 4 rule the sentence follows.
_WHAT_NOT_HOW = f"describe WHAT you need {_DASH} never HOW to fetch it."
_OWNS_SCHEMA_END = "self-recovers from its own schema errors."

_OPEN = "When you delegate a question about records in a time window"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _mode_a_prompts():
    yield "full-agent", _flat(orch._build_orchestrator_prompt("agent"))
    yield "module-default", _flat(orch.ORCHESTRATOR_PROMPT)
    yield "quick-agent", _flat(compose_quick_system_prompt("agent"))
    yield "query-workflow-agent", _flat(get_workflow_guidance("agent"))


PROMPTS = list(_mode_a_prompts())
IDS = [name for name, _ in PROMPTS]


def _rule(prompt: str) -> str:
    """The new rule's text: from its opening words to the next rule."""
    start = prompt.index(_OPEN)
    return prompt[start:prompt.index(_NEXT_RULE, start)]


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_it_applies_to_any_record_window(name, prompt):
    rule = _rule(prompt).lower()
    assert prompt.count(_OPEN) == 1
    # Not conditioned on the orchestrator knowing the records carry several
    # times: it does not know the system's schema.
    assert "carry several times" not in rule
    assert "event" in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_a_user_named_or_plainly_tied_event_is_kept(name, prompt):
    rule = _rule(prompt).lower()
    assert "the user's own question" in rule
    assert "names or plainly ties" in rule
    assert "keep that choice" in rule
    # Ordinary business wording counts, not only a literal time name.
    for word in ("placed", "started", "shipped", "completed"):
        assert word in rule, word


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_otherwise_only_the_window_is_given(name, prompt):
    rule = _rule(prompt).lower()
    assert "otherwise" in rule
    assert "give only the window" in rule
    assert "its own time rule" in rule
    assert "do not say which time or event places a record in the window" in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_state_and_population_filters_are_kept(name, prompt):
    rule = _rule(prompt).lower()
    assert "state and population filters" in rule
    assert "completed only" in rule
    assert "only the event that places a record in the window is left out" in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_rule_sits_in_step_4_after_the_what_not_how_rule(name, prompt):
    step_4 = prompt.index(_STEP_4)
    at = prompt.index(_OPEN)
    assert step_4 < prompt.index(_WHAT_NOT_HOW, step_4) < at < prompt.index(_NEXT_RULE, step_4)
    if _STEP_5 in prompt:
        assert at < prompt.index(_STEP_5, step_4)
    # Directly after the existing paragraph's last sentence.
    assert prompt.index(_OWNS_SCHEMA_END, step_4) + len(_OWNS_SCHEMA_END) + 1 == at
    assert prompt.count(_WHAT_NOT_HOW) == 1


def test_the_rule_is_dataset_neutral_plain_words():
    rule = _rule(_flat(orch._MODE_A_DELEGATE_BLOCK))
    assert "\u2014" not in rule and "\u2013" not in rule
    assert "_" not in rule and "`" not in rule
    assert not any(ch.isdigit() for ch in rule)
    # No capitalised names (tables, states, systems) after each sentence's
    # opening word.
    for sentence in rule.split(". "):
        assert sentence[1:] == sentence[1:].lower(), sentence


def test_mode_b_prompts_do_not_carry_it():
    # Mode B composes its own SQL; there is no system agent to leave the event to.
    for prompt in (orch._build_orchestrator_prompt("orchestrator"),
                   compose_quick_system_prompt("orchestrator")):
        assert _OPEN not in _flat(prompt)
