# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A stop at a limit is not a rule decline, said where the model reads the stop.

Measured on a rehearsal deployment: a system agent hit its time limit on two
questions and ended both answers with the rule-decline marker line
(kind=not_connected). The orchestrator read each as a real decline and refused
every later ask about that concept in the turn, so one answer lost detail and
the other lost its total. "Never write that line for a budget stop" was stated
only in the RULE DECLINE note, far from the stop; the cancel text that tells
the model to answer now said nothing about it.

Pinned here, as meaning rather than exact prose:

* every text that tells the model to stop and answer now after a limit (the
  budget hook's cancel text for either cap, the standing BUDGET note) says the
  limit by itself is not a rule decline, and that a query which failed, timed
  out or returned no rows does not show a source is not connected;
* it does NOT restate its own list of when to decline: it defers to every
  condition of the RULE DECLINE note exactly as stated, so the not-computable
  metric and not-connected source cases (kinds withheld_metric, not_connected)
  keep their marker after a stop;
* the bounded elicitation retry, which runs after any empty first answer
  (not only after a stop), says the empty answer is not by itself a decline and
  never calls it a stop;
* the RULE DECLINE note, its three kinds and the partial-marker contracts are
  unchanged, and the parser still reads a real decline on a stopped answer.

Neutral fixtures, fully offline.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.delegation_decline as dd
import agents.system_agent as sa

_KINDS = ("rule", "not_connected", "withheld_metric")


class _FakeToolCallEvent:
    """Writable stand-in for strands' BeforeToolCallEvent."""

    def __init__(self, invocation_state):
        self.invocation_state = invocation_state
        self.cancel_tool = False


def _cancel_text(cap: str) -> str:
    """The cancel text of the first call past the call cap or the time cap."""
    budget = sa._ToolCallBudget(max_calls=2 if cap == "calls" else 100,
                                max_wall_seconds=600.0 if cap == "calls" else 60.0)
    state: dict = {}
    budget._on_invocation_start(SimpleNamespace(invocation_state=state))
    if cap == "wall":
        state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 75.0
    for _ in range(5):
        event = _FakeToolCallEvent(state)
        budget._on_before_tool_call(event)
        if event.cancel_tool:
            return event.cancel_tool
    raise AssertionError("the budget never tripped")


def _says_a_limit_is_no_decline(text: str) -> None:
    """The limit sentence's meaning, in any wording that keeps these parts."""
    low = text.lower()
    assert "not by itself a rule decline" in low
    assert "declined-by-rule" in low
    # Defers to the whole RULE DECLINE note instead of a partial list.
    assert "every condition of the rule decline note" in low
    assert "exactly as stated" in low
    # The observed failure: a timed-out query read as a missing source.
    for part in ("failed", "timed out", "no rows", "not connected"):
        assert part in low, part


def _no_partial_trigger_list(text: str) -> None:
    """A restated, narrower list of decline triggers would be read last and
    win over the RULE DECLINE note (round 1 review: it dropped the
    not-computable metric and not-connected source cases)."""
    low = text.lower()
    assert "only when" not in low
    assert "a query hint" not in low


@pytest.mark.parametrize("cap", ["calls", "wall"])
def test_the_cancel_text_says_the_limit_is_no_decline(cap):
    text = _cancel_text(cap)
    _says_a_limit_is_no_decline(text)
    _no_partial_trigger_list(text)
    # Read at the stop, after the instruction to answer now.
    assert text.index("Answer NOW") < text.lower().index("not by itself a rule decline")


@pytest.mark.parametrize("cap, marker", [
    ("calls", sa._budget_partial_marker(2)),
    ("wall", sa._wall_partial_marker(60.0)),
])
def test_the_cancel_text_keeps_the_partial_marker_contract(cap, marker):
    text = _cancel_text(cap)
    assert text.startswith("TOOL BUDGET EXHAUSTED (")
    assert f"START your answer with '{marker}'" in text
    assert marker.startswith("partial: ") and "partial: stopped at" in marker
    assert "NEVER fabricate or estimate values you did not actually read." in text


def test_the_standing_budget_note_says_it_too():
    note = sa._budget_prompt()
    _says_a_limit_is_no_decline(note)
    _no_partial_trigger_list(note)
    assert note.index("TOOL BUDGET EXHAUSTED") < note.lower().index("not by itself a rule decline")
    assert sa._budget_partial_marker(sa._tool_call_budget()) in note


@pytest.mark.parametrize("connection_type", ["Athena", "PostgreSQL", "MCP Server", "REST API"])
def test_every_decline_kind_and_trigger_stays_in_the_prompt_it_defers_to(connection_type):
    """The note the limit sentence points at is in the same system prompt,
    before it, with all three kinds and the two triggers a partial list
    dropped."""
    tail = sa._policy_tail(connection_type)
    assert sa._RULE_DECLINE_PROMPT in tail
    start = tail.index(sa._RULE_DECLINE_PROMPT)
    assert start < tail.lower().index("not by itself a rule decline")
    for kind in _KINDS:
        assert kind in sa._RULE_DECLINE_PROMPT
    for trigger in ("a metric is not computable from this system",
                    "the answer needs a source that is not connected"):
        assert trigger in sa._RULE_DECLINE_PROMPT


def _retry_prompts() -> list[str]:
    """The prompts the non-enforce answer path sends when the first pass
    returns no text (no limit involved): the question, then the bounded
    elicitation retry."""
    prompts: list[str] = []

    def agent(prompt, invocation_state, limits, **_kwargs):
        prompts.append(prompt)
        text = "" if len(prompts) == 1 else "Covered part of the question."
        return SimpleNamespace(message={"role": "assistant", "content": [{"text": text}]},
                               stop_reason="end_turn")

    answer = sa._legacy_mode_a_answer(agent, "How many items?", "report-only", {})
    assert answer == "Covered part of the question."
    return prompts


def test_the_elicitation_retry_calls_the_empty_answer_no_decline_and_no_stop():
    prompts = _retry_prompts()
    assert len(prompts) == 2 and prompts[0] == "How many items?"
    retry = prompts[1]
    low = retry.lower()
    assert retry.startswith("Produce your final answer NOW from the tool results already "
                            "in this conversation.")
    assert "Do not call any tools. Do not output JSON." in retry
    assert "empty first answer is not by itself a rule decline" in low
    assert "rule decline note" in low and "exactly as stated" in low
    # This retry runs after any empty first answer, so it must not claim a stop.
    assert "stop" not in low.replace("non-stop", "")
    _no_partial_trigger_list(retry)


def test_the_new_text_has_no_dashes():
    for text in (sa._LIMIT_STOP_NOT_A_DECLINE, sa._EMPTY_ANSWER_NOT_A_DECLINE):
        assert "\u2014" not in text and "\u2013" not in text


def test_the_rule_decline_note_is_unchanged():
    note = sa._rule_decline_prompt()
    assert note == sa._RULE_DECLINE_PROMPT
    assert ("Never write that line for a failed or empty query, a budget stop, or an "
            "identifier miss.") in note
    assert "not by itself a rule decline" not in note.lower()


def test_a_real_decline_on_a_stopped_answer_is_still_read():
    """No code screen: a stopped answer whose item a stated rule forbids still
    ends with the marker, and the parser reads it as before."""
    answer = (f"{sa._wall_partial_marker(60.0)}.\n"
              "Item a is not given here: the stated rule says it needs a source that "
              "is not connected.\n"
              "DECLINED-BY-RULE kind=not_connected concept=item_a")
    head, decline = dd.extract_marker(answer)
    assert decline == {"reason_kind": "not_connected", "concept": "item_a"}
    assert head.startswith("partial: stopped at") and dd.MARKER not in head
