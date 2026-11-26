# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The quality containment rule says what a containment plan asks for.

A planner given the containment rule can still read it narrowly: treat a
containment request that gives no reason as something other than a quality
containment, treat the record that revealed the problem as the scope, and ask
only for the latest record or what is open now. Each gap is closed in the
rule's own words:

- a request for a containment of made product counts as a quality one unless
  another reason is given; any other hold or quarantine still needs a stated
  quality reason;
- the plan asks for the cause's history and for everything made on or with the
  implicated source in the suspect interval, each with its own quality results
  and where it is now, never only the latest record or what is open now;
- any limit the user puts on what to hold is kept when the user asks for
  nothing wider;
- a verified correction closes the interval, so output made after it stays
  released; the plan also asks for the source's newest records and readings,
  and when they show the cause or a sign of it again a new interval starts
  after the last known good output, its onset unknown unless the records show
  it (a sign is enough: a rising reading is evidence, not a diagnosis);
- while the current interval has no verified correction, the containment also
  holds the implicated source itself, whatever its state, and what was made on
  or with it in that interval that can still be held.

On the answer side, calling a record unrelated to a cause, or unaffected by
it, needs returned evidence that establishes it, not just a returned record.

These pin wording, not model behaviour. Offline and pure: no graph, no model.
"""
from __future__ import annotations

import re

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _rule() -> str:
    return _flat(orch._CONTAINMENT_RULE).lower()


def _prompts():
    return ([(f"full-{m}", orch._build_orchestrator_prompt(m)) for m in MODES]
            + [(f"quick-{m}", compose_quick_system_prompt(m)) for m in MODES])


_PROMPTS = _prompts()
_IDS = [name for name, _ in _PROMPTS]

#: The rule's additions, in its own words (whitespace folded, case ignored).
DEFAULT_REASON = "a containment for made product counts unless another reason is given"
ASK_FOR = ("then ask for the cause's history and for everything made on or with the "
           "implicated source in that interval, each with its own quality results and where "
           "it is now, never only the latest record or what is open now")
NAMED_SCOPE = "if the user limits what to hold and asks for nothing wider, keep that scope"
UNRELATED = ("call a record unrelated to or unaffected by a cause only when returned evidence "
             "establishes that")
CLOSES = "a verified correction closes the interval"
NEW_INTERVAL = ("also ask for the source's newest records and readings: if they show the "
                "cause or a sign of it again, a new interval starts after the last known good "
                "output, its onset unknown unless the records show it")
HOLDS_SOURCE = ("while the current interval has no verified correction, also hold the "
                "implicated source itself, whatever its state, and what was made on or with "
                "it in that interval that can still be held")


def test_a_containment_request_that_gives_no_reason_is_a_quality_one():
    low = _rule()
    assert DEFAULT_REASON in low
    # It qualifies the trigger itself, before the principle.
    assert low.index(DEFAULT_REASON) < low.index("the containment covers")


def test_only_a_containment_of_made_product_defaults_to_quality():
    """Any other hold or quarantine with no reason does not default: the
    default names a containment of made product, not every hold verb."""
    low = _rule()
    trigger = low[:low.index("the containment covers")]
    assert "because product is suspected of a quality defect" in trigger
    assert "assume so" not in trigger
    assert not re.search(r"\b(?:any|every) (?:hold|quarantine)\b", trigger)


def test_the_plan_asks_for_the_interval_on_the_implicated_source():
    low = _rule()
    assert ASK_FOR in low
    # The ask follows the interval it is about.
    assert low.index("establish the suspect interval") < low.index(ASK_FOR)
    # It is tied to the implicated source, never to all production in a window.
    assert "everything made across" not in low
    assert "all production" not in low


def test_a_named_scope_is_kept_when_nothing_wider_is_asked():
    low = _rule()
    assert NAMED_SCOPE in low
    assert "keep that scope." in low


def test_any_limit_the_user_puts_on_the_hold_is_kept_not_only_named_record_types():
    """A list of record types (batches, lots or orders) would leave a limit to
    these shipments, these units or one product open to widening. The scope
    clause names no record type, so any limit the user sets wins."""
    low = _rule()
    scope = low[low.index("if the user limits"):low.index("keep that scope.")]
    assert not re.search(r"\b(?:batch|batches|lots?|orders?|shipments?|units?|products?)\b",
                         scope), scope


def test_a_sign_of_the_cause_starts_the_new_interval_not_only_the_cause_itself():
    """A rising reading is evidence of the suspected cause, not a diagnosis,
    and the cause rule forbids calling co-movement proof. A trigger that
    needed the cause itself would let a compliant plan skip the new interval,
    so a sign of it is enough."""
    low = _rule()
    assert "if they show the cause or a sign of it again" in low
    assert "if they show the cause again" not in low


def test_a_verified_correction_closes_its_interval():
    """A verified correction still closes the interval, so output made after it
    is not held again: nothing reopens an interval once it has closed."""
    low = _rule()
    assert CLOSES in low
    # It qualifies the interval the plan has just established and asked for.
    assert low.index(ASK_FOR) < low.index(CLOSES)
    assert "the interval is open" not in low
    assert "reopen" not in low
    assert "since the interval began" not in low


def test_a_recurring_cause_starts_a_new_interval_after_the_last_known_good_output():
    """The plan asks for the source's newest records and readings itself (so a
    fresh chat checks them too). A recurrence starts a NEW interval rather than
    reopening the closed one, and it starts after the last output known good,
    not at the first reading that shows the cause: output made in between was
    never shown good. Its onset stays unknown unless the records show it."""
    low = _rule()
    assert NEW_INTERVAL in low
    assert low.index(CLOSES) < low.index(NEW_INTERVAL)
    assert "starts there" not in low


def test_the_current_interval_holds_the_source_itself_whatever_its_state():
    """The implicated source is on the hold whatever its current state, with
    what it made in the CURRENT interval that can still be held."""
    low = _rule()
    assert HOLDS_SOURCE in low
    assert low.index(NEW_INTERVAL) < low.index(HOLDS_SOURCE)
    # A named scope still limits it: the user's scope is said after it.
    assert low.index(HOLDS_SOURCE) < low.index(NAMED_SCOPE)


def test_the_source_hold_names_no_run_state_place_or_dataset_literal():
    """Whether the source is running decides nothing, so no run state is
    named; nor is any place product sits, or anything from a dataset."""
    for text in (CLOSES, NEW_INTERVAL, HOLDS_SOURCE):
        assert not re.search(r"\b(?:idle|down|stopped|active|running|in production)\b",
                             text), text
        assert not re.search(r"deliver|ship|customer|recall|stock|warehouse|transit|order",
                             text), text
        assert not re.search(r"\d|_|`", text), text
        for word in ("machine", "bearing", "vibration", "batch", "lot"):
            assert not re.search(rf"\b{word}\b", text), word


def test_the_ask_names_no_answer_category_or_dataset_literal():
    """The ask says what to request, never where product sits or what to do
    with it, and names nothing from a dataset."""
    ask = ASK_FOR
    assert not re.search(r"deliver|ship|customer|recall|scrap|stock|warehouse|transit", ask)
    assert not re.search(r"\d|_|`", ask)
    for word in ("machine", "bearing", "vibration", "run", "batch", "work order"):
        assert not re.search(rf"\b{word}\b", ask), word


def test_an_unrelated_claim_needs_evidence_that_establishes_it():
    """A returned record of the other item is not enough: the evidence must
    establish the relationship (or its absence)."""
    flat = _flat(orch._ANSWER_CONSISTENCY_RULE).lower()
    assert UNRELATED in flat
    assert "only from a returned record" not in flat


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_every_prompt_carries_the_source_hold_once(name, prompt):
    flat = _flat(prompt).lower()
    for text in (CLOSES, NEW_INTERVAL, HOLDS_SOURCE):
        assert flat.count(text) == 1, (name, text)


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_every_prompt_carries_the_unrelated_claim_rule_once(name, prompt):
    flat = _flat(prompt).lower()
    assert flat.count(UNRELATED) == 1, name


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_unrelated_claim_rule_rides_with_the_cause_rule(name, prompt):
    flat = _flat(prompt).lower()
    cause = flat.index("cause: two measures moving together")
    assert cause < flat.index(UNRELATED) < flat.index("current state:"), name


@pytest.mark.parametrize("mode", MODES)
def test_the_quick_prompt_stays_under_its_budget(mode):
    """The Quick instructions budget is shared with every other rule; this
    change must not push either mode over it."""
    from tests.unit.test_workflow_guidance_no_drift import QUICK_INSTRUCTIONS_BUDGET

    assert len(compose_quick_system_prompt(mode)) <= QUICK_INSTRUCTIONS_BUDGET
