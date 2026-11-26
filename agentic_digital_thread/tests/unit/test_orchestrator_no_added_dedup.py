# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator does not add a de-duplication the user did not ask for.

Measured in a live dry run: the user asked exactly how many records of one
kind a site completed in a week. The orchestrator's delegation added that the
count should cover only distinct records, "not repeated revision rows". The
system agent counted distinct ids, called the difference revision rows
without reading any revision field, and the answer led with the distinct
figure, about one seventeenth of the right count, and called the right count
an overstatement. The run before, on the same build line, delegated the same
question without that clause and answered it right.

The division-of-labour rule already says the orchestrator does not know how a
system stores its data; the clause broke it for a count. Step 4 now says:

* ask for the count of what the user's question counts, and add no
  de-duplication it does not ask for (only distinct or unique ones, each one
  once, rows left out as repeats, duplicates or revisions);
* never tell the system agent that its rows repeat: it decides from its own
  schema how to count what the user named;
* listing the distinct values of something, or breaking a count down by each
  distinct value, is not a de-duplication and stays allowed (the truncation
  rule asks for exactly that).

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
_NEXT_RULE = "CONFIRMED JOINS BEAT IDENTIFIER GUESSES:"
#: Last sentence of the record-window rule this rule follows.
_WINDOW_RULE_END = "only the event that places a record in the window is left out."

_OPEN = "Ask for the count of what the user's question counts"


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
def test_every_mode_a_prompt_carries_it_once(name, prompt):
    assert prompt.count(_OPEN) == 1


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_no_de_duplication_the_question_does_not_ask_for(name, prompt):
    rule = _rule(prompt).lower()
    assert "do not add a de-duplication it does not ask for" in rule
    # Each form the measured clause took, or could take, is named.
    for form in ("only distinct or unique ones", "each one once",
                 "repeats, duplicates or revisions"):
        assert form in rule, form


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_it_never_claims_how_a_system_s_rows_repeat(name, prompt):
    rule = _rule(prompt).lower()
    assert "you do not know how a system's rows repeat" in rule
    assert "never say they do" in rule
    assert "the system agent decides from its own schema how to count what the user named" in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_a_system_s_own_repeated_rows_note_still_gets_its_re_ask(name, prompt):
    # The totals rule tells the orchestrator to ask a system once more for a
    # total "joined so that each of its rows matches once" when that system's
    # own result says its rows repeat. The new rule must not forbid that.
    rule = _rule(prompt).lower()
    assert "unless that system's own result says so" in rule


#: The totals rule's own words for the grain of its re-ask.
_TOTALS_GRAIN = "at the grain of the records being totaled"


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_rule_governs_every_ask_except_the_totals_follow_up(name, prompt):
    # Saying that a system may say its rows repeat is not enough: the rule
    # also bans "counting each one once", which is what the totals re-ask
    # does. The rule has to name that one follow-up as its only exception,
    # and keep governing every other ask (a re-delegation, a narrowed ask,
    # a gap follow-up), where an added de-duplication is the same defect.
    rule = _rule(prompt).lower()
    assert "this governs every ask you send, except one" in rule
    assert "is not an added de-duplication and this rule does not forbid it" in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_rule_is_not_limited_to_the_first_ask(name, prompt):
    rule = _rule(prompt).lower()
    assert "first ask" not in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_exemption_covers_both_totals_statuses(name, prompt):
    # "repeated" (rows repeat) and "unchecked" (could not be checked) each
    # require a follow-up in the totals rule.
    rule = _rule(prompt).lower()
    assert "says its rows repeat or a total could not be checked" in rule


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_exemption_names_the_follow_up_the_totals_rule_asks_for(name, prompt):
    # Tied to the totals rule's own grain wording, so the two cannot drift
    # apart without one of these tests failing.
    rule = _rule(prompt).lower()
    assert f"once more for that total {_TOTALS_GRAIN}" in rule


def test_the_totals_rule_still_asks_for_that_grain():
    prompt = _flat(orch._build_orchestrator_prompt("agent"))
    totals = prompt[prompt.index("UNVERIFIED AND UNCHECKED TOTALS:"):]
    assert f"ONCE more for that total {_TOTALS_GRAIN}" in totals


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_exemption_adds_no_requirement_where_there_is_no_totals_rule(name, prompt):
    # The quick and query-workflow prompts carry this rule but not the totals
    # rule, so the exemption may only lift the ban, never require a re-ask.
    rule = _rule(prompt).lower()
    for word in ("you must", "you still", "always ask"):
        assert word not in rule, word


def test_the_repeated_rows_re_ask_is_still_in_the_prompt():
    prompt = _flat(orch._build_orchestrator_prompt("agent"))
    assert "joined so that each of its rows matches once" in prompt
    assert prompt.count("unless that system's own result says so") == 1


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_listing_distinct_values_stays_allowed(name, prompt):
    rule = _rule(prompt).lower()
    assert "listing the distinct values of something" in rule
    assert "breaking a count down by each distinct value" in rule
    assert "stays allowed" in rule


def test_it_does_not_contradict_the_truncation_rule():
    # The truncation rule asks a system for the distinct values with a count
    # per value; the new rule must leave that instruction standing.
    prompt = _flat(orch._build_orchestrator_prompt("agent"))
    assert "the distinct values with a count per value" in prompt


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_it_sits_in_step_4_right_after_the_window_rule(name, prompt):
    step_4 = prompt.index(_STEP_4)
    at = prompt.index(_OPEN)
    window_end = prompt.index(_WINDOW_RULE_END, step_4) + len(_WINDOW_RULE_END)
    assert window_end + 1 == at
    assert at < prompt.index(_NEXT_RULE, step_4)


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
    # Mode B composes its own SQL; there is no system agent to leave the count to.
    for prompt in (orch._build_orchestrator_prompt("orchestrator"),
                   compose_quick_system_prompt("orchestrator")):
        assert _OPEN not in _flat(prompt)
