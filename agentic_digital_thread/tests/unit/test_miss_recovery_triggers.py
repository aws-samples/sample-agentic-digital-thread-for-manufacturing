# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What starts the identifier-miss recovery, in each mode, stated once.

Mode A (the orchestrator delegates to system agents). The IDENTIFIER FORMS
rule says no rows after sending a form another field holds is a wrong-form
miss, recovered even without an IDENTIFIER-MISS block. The recovery contract
right after it used to say that ONLY the block starts recovery, never a bare
empty result. A system agent following ZERO-ROW LOOKUPS reports a wrong-form
lookup in words, often without the block, so the two sentences told the
orchestrator both to recover and not to. The contract now names both
triggers. Its steps (one lookup, one re-delegation, at most two recoveries a
turn, disclosure, learning) are unchanged.

Mode B (the orchestrator composes SQL and calls run_query itself). There is
no system agent, so ZERO-ROW LOOKUPS never runs and no recovery loop exists.
An empty result is a miss under MISS FOLLOW-UP: one follow-up, which may use
another identifier the orchestrator already holds for the same records, then
the gap is stated as what was searched and not found. Nothing here adds a
retry.

Fully offline; prompt text only.
"""
import inspect
import re

import pytest

import agents.orchestrator as orch
import agents.system_agent as sa
from agents.workflow_guidance import compose_quick_system_prompt

_MODE_A = {"full-agent": lambda: orch._build_orchestrator_prompt("agent"),
           "quick-agent": lambda: compose_quick_system_prompt("agent")}
_MODE_B = {"full-orchestrator": lambda: orch._build_orchestrator_prompt("orchestrator"),
           "quick-orchestrator": lambda: compose_quick_system_prompt("orchestrator")}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _recovers_without_block(flat: str) -> bool:
    return "even without an IDENTIFIER-MISS block" in flat


#: Any sentence that makes the block the only trigger.
_ONLY_THE_BLOCK = re.compile(
    r"(?i)\bonly\s+(?:that|the|this|a)\s+(?:fenced\s+)?(?:IDENTIFIER-MISS\s+)?(?:marker|block)\b"
    r"|never a bare empty result")


def _only_block_triggers(flat: str) -> bool:
    return bool(_ONLY_THE_BLOCK.search(flat))


# --------------------------------------------------------------------------- #
# Mode A
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", list(_MODE_A))
def test_mode_a_never_says_both_recover_without_a_block_and_only_the_block(name):
    flat = _flat(_MODE_A[name]())
    # The wrong-form clause must stay; the contradiction is the pair.
    assert _recovers_without_block(flat), name
    assert not (_recovers_without_block(flat) and _only_block_triggers(flat)), name


def test_the_contradiction_detector_catches_the_old_wording():
    old = ("No rows after sending another field's form is a wrong-form miss even without an "
           "IDENTIFIER-MISS block. That marker — and ONLY that marker, never a bare empty "
           "result or a plain 'no data' — triggers this bounded recovery.")
    assert _recovers_without_block(old) and _only_block_triggers(old)


@pytest.mark.parametrize("name", list(_MODE_A))
def test_mode_a_recovery_names_both_triggers(name):
    flat = _flat(_MODE_A[name]())
    start = flat.index("IDENTIFIER-MISS RECOVERY — BOUNDED, DISCLOSED, NEVER SILENT")
    head = flat[start:flat.index("1. Make ONE explore_graph", start)]
    for fragment in (
            "Two signals, and only these two, trigger this bounded recovery for an "
            "identifier you sent",
            "(a) a fenced IDENTIFIER-MISS block (token / looked_in / my_format)",
            "(b) a wrong-form miss: no rows after you sent the identifier in the form "
            "another field holds, per the route seed's IDENTIFIER FORMS block, with or "
            "without the block",
            "the identifier you sent is the token; the field the reply says it looked in "
            "is looked_in",
            "Any other empty result or plain 'no data' triggers nothing",
            "It is the ONE sanctioned exception to the 'STOP RE-QUERYING THE GRAPH AFTER "
            "EMPTY RESULTS' rule"):
        assert fragment in head, (name, fragment)


@pytest.mark.parametrize("name", list(_MODE_A))
@pytest.mark.parametrize("fragment", [
    "Make ONE explore_graph translation lookup scoped to exactly that token",
    "re-delegate ONCE to the SAME system",
    "This re-delegation IS that system's one miss follow-up (MISS FOLLOW-UP)",
    "If the graph has NO translation: do NOT retry",
    "AT MOST 2 such recoveries per turn",
    "DISCLOSE the substitution",
    "NEVER substitute silently",
    "CONFIRMED RECOVERY → LEARN IT",
])
def test_mode_a_bounded_and_disclosure_rules_are_kept_once(name, fragment):
    assert _flat(_MODE_A[name]()).count(fragment) == 1, (name, fragment)


# --------------------------------------------------------------------------- #
# Mode B: an honest miss, no new loop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", list(_MODE_B))
def test_mode_b_has_no_recovery_loop_and_no_system_agent_rule(name):
    prompt = _MODE_B[name]()
    for text in ("IDENTIFIER-MISS", "IDENTIFIER FORMS", "wrong-form miss",
                 "ZERO-ROW LOOKUPS"):
        assert text not in prompt, (name, text)


@pytest.mark.parametrize("name", list(_MODE_B))
@pytest.mark.parametrize("fragment", [
    "plus ONE follow-up only after a miss or a gap; nothing more.",
    "A miss is an empty result",
    "another identifier you hold for the same records",
    "After the follow-up, stop querying that system, answer from what you have, and state "
    "the gap honestly as what you searched and did not find (which system, which key)",
    "a miss on one key or table never proves the data or link does not exist",
])
def test_mode_b_reports_an_empty_result_as_a_miss_after_one_follow_up(name, fragment):
    assert fragment in _flat(_MODE_B[name]()), (name, fragment)


def test_mode_b_run_query_is_direct_sql_not_a_system_agent():
    # Why ZERO-ROW LOOKUPS cannot apply in Mode B: run_query executes the
    # orchestrator's SQL; no system-agent prompt is composed on that path.
    source = inspect.getsource(sa._register_mode_b_run)
    assert "_mode_b_run_query" in source
    assert "_compose_system_prompt" not in source
    assert "_zero_row_lookup_prompt" not in inspect.getsource(sa._mode_b_run_query)


@pytest.mark.parametrize("name", list(_MODE_A))
def test_mode_a_blockless_miss_asks_for_no_format_hint_it_cannot_have(name):
    """A wrong-form miss can arrive without the fenced block, so it carries
    no my_format value. The no-translation step and the over-cap line must
    say what to report in that case instead of requiring the hint."""
    flat = _flat(_MODE_A[name]())
    start = flat.index("3. If the graph has NO translation")
    step3 = flat[start:flat.index("4. If MULTIPLE candidate", start)]
    assert "when the miss came in a fenced block" in step3, name
    assert "name the field and form the IDENTIFIER FORMS block expects" in step3, name
    assert "with no example values" in step3, name
    assert "or the expected field and form" in flat, name
