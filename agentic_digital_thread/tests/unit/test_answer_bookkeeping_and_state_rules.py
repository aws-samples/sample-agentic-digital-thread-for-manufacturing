# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Five standing answer rules from a test event's chat and Quick answers.

Each rule is general wording (no figures, ids or story names) and is pinned
in both mode prompts, and in both Quick instruction variants when the rule
rides the shared Step 5 region the Quick slice carries.

1. Own bookkeeping stays out of the answer: an answer relayed to Quick said
   the turn needed "the one allowed follow-up" after an "output limit". The
   never-mention-internals block now names the call budget and follow-up
   allowance, output limits, ``[engine]`` lines and retry counts, and says to
   state what was not found or returned instead. That block sits before the
   Quick slice, so it is pinned in the two mode prompts only.
2. No invented reason for a difference: an aside said a system's own summary
   "rounded" a figure, a reason no result gave. DISCARDED FIGURES now forbids
   a reason for a difference between two figures that no result states.
3. Current state from the newest records: a containment answer called a
   machine "repaired" from an earlier cycle's completed repair while a later
   alarm order was open. CURRENT STATE sits beside CONSISTENCY / EVENT ORDER.
4. Follow-ups are held to the answer's rows: a suggested follow-up named "the
   two customers" when the answer's rows showed a different set. CONSISTENCY
   now names follow-up questions and suggested actions, and any count or set
   of records they name.
5. Times sent to a system are absolute UTC: one answer's window end was off
   by the user's UTC offset. The orchestrator now sends every time to a
   system as UTC (ISO with Z in questions and time arguments). The answer
   writes times in UTC too and the browser converts them
   (tests/unit/test_answer_times_utc.py).

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

import re

import pytest

import agents.orchestrator as orch
from agents.demo import story_lookup
from agents.workflow_guidance import compose_quick_system_prompt

_MODES = ("agent", "orchestrator")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _mode_prompts():
    return [(f"full-{mode}", orch._build_orchestrator_prompt(mode)) for mode in _MODES]


def _all_prompts():
    return _mode_prompts() + [
        (f"quick-{mode}", compose_quick_system_prompt(mode)) for mode in _MODES]


_MODE_PROMPTS = _mode_prompts()
_ALL_PROMPTS = _all_prompts()
_MODE_IDS = [name for name, _ in _MODE_PROMPTS]
_ALL_IDS = [name for name, _ in _ALL_PROMPTS]


# --------------------------------------------------------------------------- #
# 1. Own bookkeeping
# --------------------------------------------------------------------------- #
_BOOKKEEPING_FRAGMENTS = (
    "Never describe your own bookkeeping to the user",
    "your call budget or follow-up allowance",
    "output limits",
    "the [engine] lines in tool results",
    "how many times you retried a call",
    "Say what was not found or not returned instead",
)


@pytest.mark.parametrize("name,prompt", _MODE_PROMPTS, ids=_MODE_IDS)
def test_never_mention_block_names_the_agents_own_bookkeeping(name, prompt):
    flat = _flat(prompt)
    start = flat.index("NEVER mention internal implementation details")
    # The rule lives inside the never-mention list, before the next section.
    section = flat[start:flat.index("HANDLING SYSTEM ERRORS", start)]
    for fragment in _BOOKKEEPING_FRAGMENTS:
        assert fragment in section, (name, fragment)
    assert flat.count(_BOOKKEEPING_FRAGMENTS[0]) == 1, name


def test_bookkeeping_rule_keeps_cut_results_disclosed():
    # Not a licence to hide truncation: a cut result is still reported as
    # incomplete, only the plumbing words go.
    assert "a cut result is still reported as incomplete" in _flat(
        orch._INTERNAL_BOOKKEEPING_RULE)


# --------------------------------------------------------------------------- #
# 2. No invented reason for a difference between two figures
# --------------------------------------------------------------------------- #
_NO_REASON = ("Never give a reason for a difference between two figures (rounding, timing, "
              "a later correction, another basis) that no result states")
_RELY_ON = "say which figure you rely on and that the other was not used"


@pytest.mark.parametrize("name,prompt", _ALL_PROMPTS, ids=_ALL_IDS)
def test_discarded_figures_forbids_an_invented_reason(name, prompt):
    flat = _flat(prompt)
    start = prompt.index("DISCARDED FIGURES:")
    end = prompt.find("\n\n", start)
    discarded = _flat(prompt[start:] if end < 0 else prompt[start:end])
    assert _NO_REASON in discarded, name
    assert _RELY_ON in discarded, name
    assert flat.count(_NO_REASON) == 1, name


# --------------------------------------------------------------------------- #
# 3. Current state comes from the newest records
# --------------------------------------------------------------------------- #
_CURRENT_STATE = (
    "CURRENT STATE: a machine's or record's current condition comes from its newest records "
    "and live readings",
    "A completed repair or returned-to-service entry describes the time it was logged",
    "when a later alarm or reading on the same asset and condition shows otherwise, report "
    "that as the current state and give the repair as history with its date",
    "An order's status is not the machine's condition",
)


# Module 6 walk (2026-10-27): the alarm opened a new work order between two
# questions in one chat; the second was answered "unchanged" from [E2]. The
# rule rides the citation-stamp rule, so it reaches only the orchestrator
# prompts: AWS Quick results carry no [E<k>] stamp.
_RE_QUERY = (
    "A question about what is open, active or current now in a system (open orders, active "
    "alarms, live readings) is answered from that system's result in this turn: call it again.",
    "never answer such a question as unchanged from it",
)


@pytest.mark.parametrize("name,prompt", _MODE_PROMPTS, ids=_MODE_IDS)
def test_current_state_question_calls_the_system_again(name, prompt):
    flat = _flat(prompt)
    for fragment in _RE_QUERY:
        assert flat.count(fragment) == 1, (name, fragment)


@pytest.mark.parametrize("mode", _MODES)
def test_quick_prompts_carry_no_re_query_rule(mode):
    assert _RE_QUERY[1] not in _flat(compose_quick_system_prompt(mode))


@pytest.mark.parametrize("name,prompt", _ALL_PROMPTS, ids=_ALL_IDS)
def test_current_state_rule_does_not_read_condition_off_any_later_order(name, prompt):
    # A later open order can be routine preventive work; it does not by itself
    # say the machine is unhealthy or supersede a repair.
    flat = _flat(prompt)
    assert "when a later alarm, open order or reading exists" not in flat, name


@pytest.mark.parametrize("name,prompt", _ALL_PROMPTS, ids=_ALL_IDS)
def test_current_state_rule_sits_beside_event_order(name, prompt):
    flat = _flat(prompt)
    for fragment in _CURRENT_STATE:
        assert flat.count(fragment) == 1, (name, fragment)
    assert (flat.index("CONSISTENCY:") < flat.index("EVENT ORDER:")
            < flat.index("CURRENT STATE:") < flat.index("ENGINE FACTS:")), name


# --------------------------------------------------------------------------- #
# 4. Follow-ups are held to the answer's rows
# --------------------------------------------------------------------------- #
_FOLLOW_UP_CONSISTENCY = (
    "CONSISTENCY: a caveat, scope note, recommendation, follow-up question or suggested "
    "action must agree with the figures in the same answer",
    "A count or set of records a follow-up question or suggested action names must match "
    "this answer's rows; if it cannot, name the records without a count",
)


@pytest.mark.parametrize("name,prompt", _ALL_PROMPTS, ids=_ALL_IDS)
def test_consistency_covers_follow_ups_and_their_counts(name, prompt):
    flat = _flat(prompt)
    for fragment in _FOLLOW_UP_CONSISTENCY:
        assert flat.count(fragment) == 1, (name, fragment)


# --------------------------------------------------------------------------- #
# 5. Times sent to a system are absolute UTC
# --------------------------------------------------------------------------- #
_TIMES_SENT = (
    "TIMES ARE UTC: a time you send to a system, in a delegated question, a tool's time "
    "argument or a query, is absolute UTC, never a local time",
    "Write it as tools return it (ISO with Z) in questions and time arguments",
)


@pytest.mark.parametrize("name,prompt", _ALL_PROMPTS, ids=_ALL_IDS)
def test_orchestrator_sends_times_to_systems_in_utc(name, prompt):
    flat = _flat(prompt)
    for fragment in _TIMES_SENT:
        assert flat.count(fragment) == 1, (name, fragment)
    # Beside the window-echo rule it complements.
    assert flat.index("RESOLVED TIME WINDOW") < flat.index("TIMES ARE UTC:"), name


def test_times_rule_is_not_inside_the_mode_a_delegate_block():
    # The Mode B Quick variant replaces that block; the rule must survive it.
    assert "TIMES ARE UTC:" not in orch._MODE_A_DELEGATE_BLOCK


# --------------------------------------------------------------------------- #
# Domain neutrality of every new text (ADR-0044)
# --------------------------------------------------------------------------- #
def _new_texts() -> list[str]:
    return [orch._INTERNAL_BOOKKEEPING_RULE, orch._TOTALS_RULE, orch._ANSWER_CONSISTENCY_RULE,
            orch._TIMES_SENT_RULE]


def test_new_rule_texts_name_no_story_identifier():
    for text in _new_texts():
        for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                      story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                      story_lookup.BEARING_FAILURE_CODE):
            assert token.lower() not in text.lower(), token


def test_new_rule_texts_carry_no_figure_or_identifier_shape():
    for text in _new_texts():
        assert not re.search(r"\d", text)
        assert not re.search(r"\b\w+_\w+\b", text)
        assert "$" not in text
