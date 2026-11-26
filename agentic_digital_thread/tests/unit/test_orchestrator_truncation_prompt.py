# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-drift guard for honest disclosure of truncated system results.

System agents can return structured ``truncated`` flags or explicit partial-result
markers. The shared orchestrator prompt must carry those signals into the user-facing
answer rather than presenting an incomplete dataset as complete.

Offline: no AWS, Neptune, or network access.
"""
from __future__ import annotations

import re

import pytest

import agents.orchestrator as orch

_BLOCK_HEADER = "TRUNCATED / PARTIAL TOOL RESULTS — DISCLOSE THEM:"
_MODES = ("agent", "orchestrator")
_SENTINELS = (
    "ANY tool result indicates truncation, partial data, a result cap, or that only a subset",
    "state this plainly in the final answer",
    "shown and total counts when available",
    '"showing first N of M records"',
    "Never present partial data as complete",
    "targeted follow-up to retrieve the remainder or all results",
    "narrower filter or pagination request",
)


@pytest.mark.parametrize("mode", _MODES)
def test_truncation_disclosure_directive_present_in_both_modes(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    normalized_prompt = " ".join(prompt.split())

    assert prompt.count(_BLOCK_HEADER) == 1
    for sentinel in _SENTINELS:
        assert sentinel in normalized_prompt, (
            f"mode={mode}: truncation-handling directive is missing {sentinel!r}"
        )


@pytest.mark.parametrize("mode", _MODES)
def test_truncation_directive_is_adjacent_to_result_honesty_rules(mode):
    prompt = orch._build_orchestrator_prompt(mode)

    errors_idx = prompt.index("HANDLING SYSTEM ERRORS")
    truncation_idx = prompt.index(_BLOCK_HEADER)
    missing_system_idx = prompt.index("SYSTEM NOT ONBOARDED")
    assert errors_idx < truncation_idx < missing_system_idx


# --------------------------------------------------------------------------- #
# Re-ask for the full set when a system's answer is partial (2026-10-11)
#
# Measured on a rehearsal account: a system's first answer was a row listing
# cut at the row cap, and the item the question was about fell past the cut.
# Runs that asked that system once more for the distinct set passed; runs that
# only disclosed the cut (or were never told of it) answered from the partial
# list as if it were complete. Disclosure alone is not enough when the
# question needs the whole set.
# --------------------------------------------------------------------------- #
_REQUERY_SENTINELS = (
    "A system's answer is incomplete when it says a result was cut off, truncated, "
    "capped or sampled",
    "or when a count it gives is larger than the items it lists",
    "When the question needs the complete set of those items (which ones, all of "
    "them) or your answer would say that none of them matches",
    "do NOT answer from the items listed and never infer the rest",
    "tool ONCE more for the set itself at its own grain",
    "(the distinct values with a count per value, same filters and window)",
    "and answer from that",
    "When an item it returned already shows that at least one matches, that yes "
    "needs no second call",
    "still say that the rest was not checked",
    "That second call is the one follow-up that system gets, shared with any other gap",
    "Disclose what is still missing only if that second answer is cut too",
)


def _truncation_block(prompt: str) -> str:
    start = prompt.index(_BLOCK_HEADER)
    # The block ends at the next section (the aggregate-status rule, then SYSTEM NOT ONBOARDED).
    ends = [prompt.find(header, start) for header in ("UNVERIFIED AND UNCHECKED TOTALS",
                                                      "UNVERIFIED TOTALS",
                                                      "SYSTEM NOT ONBOARDED")]
    return prompt[start:min(e for e in ends if e > start)]


@pytest.mark.parametrize("mode", _MODES)
def test_partial_answer_requery_rule_sits_in_the_truncation_block(mode):
    block = " ".join(_truncation_block(orch._build_orchestrator_prompt(mode)).split())
    for sentinel in _REQUERY_SENTINELS:
        assert sentinel in block, f"mode={mode}: missing {sentinel!r}"
    # The re-ask names the mode's own query tool, never a hardcoded one.
    tool = "run_query" if mode == "orchestrator" else "query_<system>"
    assert f"call that system's {tool} tool ONCE more" in block
    # The general disclosure rule still comes first; the re-ask refines it.
    assert block.index("Never present partial data as complete") < block.index(
        _REQUERY_SENTINELS[0])


@pytest.mark.parametrize("mode", _MODES)
def test_a_proven_yes_does_not_force_the_second_call(mode):
    # One returned item that matches proves "at least one does"; rows the
    # system did not return cannot undo it, so the re-ask is only for a full
    # list or a "none of them" conclusion.
    block = " ".join(_truncation_block(orch._build_orchestrator_prompt(mode)).split())
    trigger = block[block.index("When the question needs the complete set"):
                    block.index("do NOT answer from the items listed")]
    assert "did any of them" not in trigger
    assert "none of them matches" in trigger
    exception = block.index("that yes needs no second call")
    assert block.index("ONCE more") < exception
    assert "still say that the rest was not checked" in block[exception:]


@pytest.mark.parametrize("mode", _MODES)
def test_never_reask_rules_name_the_partial_answer_exception(mode):
    # DELEGATION DISCIPLINE and RESPONSE LENGTH DISCIPLINE both forbid
    # re-asking a system that returned data. Without the exception named at
    # those lines the two rules contradict the re-ask, which spends the
    # one follow-up MISS FOLLOW-UP allows (2026-10-15).
    prompt = " ".join(orch._build_orchestrator_prompt(mode).split())
    assert ("never re-ask a system that already returned data — use what you got "
            "(the one exception: a gap in what it returned, which spends that "
            "system's one follow-up, see MISS FOLLOW-UP)") in prompt
    assert ("Do NOT re-ask a system if the first sub-question returned data — use "
            "what you got (except a gap in what it returned, which spends that "
            "system's one follow-up, see MISS FOLLOW-UP)") in prompt


@pytest.mark.parametrize("mode", _MODES)
def test_truncation_block_names_no_dataset_word_or_literal(mode):
    from agents.demo import story_lookup

    block = _truncation_block(orch._build_orchestrator_prompt(mode))
    low = block.lower()
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in low, token
    for word in ("shipment", "customer", "supplier", "machine", "batch",
                 "lot", "inspection", "quality", "product", "part"):
        assert not re.search(rf"\b{word}s?\b", low), word
    # No digits at all, and the only identifier-shaped token is the mode's own
    # query tool name (Mode A's ``query_<system>`` has no second word).
    assert not re.search(r"\d", block)
    expected = ["run_query"] if mode == "orchestrator" else []
    assert re.findall(r"\b[A-Za-z0-9]+_[A-Za-z0-9]+\b", block) == expected
    capitals = set(re.findall(r"\b[A-Z]{2,}\b", block))
    assert capitals <= {"TRUNCATED", "PARTIAL", "TOOL", "RESULTS", "DISCLOSE", "THEM",
                        "ANY", "MUST", "NOT", "ONCE"}, capitals
