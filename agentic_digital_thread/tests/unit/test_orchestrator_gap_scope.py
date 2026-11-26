# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One call budget, a miss scoped to what was searched (2026-10-05 rehearsal).

A lot-to-work-order question sent run numbers to the ERP's lot-tracking rows,
which key on work-order numbers. The system agent found nothing and the answer
said the ERP records no lot linkage at all; a passing run of the same question
recovered only by retrying with the work-order numbers. The prompt stated the
per-system call budget four ways ("at most one", "never a second call", "one
follow-up", "does not count against the cap"), so either outcome was a legal
reading. There is now ONE rule, MISS FOLLOW-UP, inside the shared workflow (so
the AWS Quick instructions carry it too): one call per system plus one
follow-up after a miss, a miss distinct from a capability limit, another
identifier for the same records counts as the follow-up, and a gap is stated as
what was searched. Mode A also admits a measured ``value_exact_match`` JoinPath
in ``join_rule:``.
"""

import re

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")
RULE = "MISS FOLLOW-UP — ONE, THEN STOP"
_ABSOLUTE_ONE_CALL = re.compile(
    r"AT MOST ONE \S+ call per system|Never issue a second call|"
    r"not count against the one-call-per-system cap|cap is not suspended by a miss", re.I)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompts():
    for mode in MODES:
        yield f"full-{mode}", _flat(orch._build_orchestrator_prompt(mode))
        yield f"quick-{mode}", _flat(compose_quick_system_prompt(mode))


PROMPTS = list(_prompts())
IDS = [name for name, _ in PROMPTS]


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_every_prompt_states_the_one_budget_once(name, prompt):
    assert prompt.count(RULE) == 1
    assert "Per system: ONE" in prompt and "plus ONE follow-up only after a miss or a gap; nothing more." in prompt
    assert not _ABSOLUTE_ONE_CALL.search(prompt), _ABSOLUTE_ONE_CALL.search(prompt)


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_a_miss_is_not_a_capability_limit(name, prompt):
    rule = prompt[prompt.index(RULE):]
    assert "reply that it cannot find or link the values you sent" in rule
    assert "another identifier you hold for the same records" in rule and "each of these IS the one follow-up" in rule
    assert "holds no data of that KIND at all" in rule
    assert rule.index("A miss is") < rule.index("capability limit")


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_a_gap_is_scoped_to_what_was_searched(name, prompt):
    rule = prompt[prompt.index(RULE):]
    assert "state the gap honestly as what you searched and did not find (which system, which key)" in rule
    assert "a miss on one key or table never proves the data or link does not exist." in rule


@pytest.mark.parametrize("prompt", [_flat(orch._build_orchestrator_prompt("agent")),
                                    _flat(compose_quick_system_prompt("agent"))],
                         ids=["full-agent", "quick-agent"])
def test_identifier_miss_recovery_is_the_follow_up_not_an_extra_call(prompt):
    assert "This re-delegation IS that system's one miss follow-up (MISS FOLLOW-UP), never an extra call." in prompt
    assert "TOOL BUDGET follow-up cap" not in prompt


def test_mode_a_join_rule_admits_measured_exact_joins():
    block = _flat(orch._MODE_A_DELEGATE_BLOCK)
    assert "`user_confirmed`, `crosswalk_confirmed` or `value_exact_match`" in block
    assert ("CONFIRMED JOINS BEAT IDENTIFIER GUESSES: when the route seed carries a join_paths "
            "entry with one of those methods") in block
    assert "steward-confirmed JoinPath in the seed" not in block


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_the_wording_names_no_story_identifier(name, prompt):
    rule = prompt[prompt.index(RULE):prompt.index(RULE) + 900]
    assert not re.search(r"\b(?:RUN|LOT|WO|SHIP)-\d", rule)


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_every_recovery_retry_is_counted_as_the_follow_up(name, prompt):
    """Transient retries repeat the same call; every semantic recovery IS the follow-up."""
    rule = prompt[prompt.index(RULE):]
    assert "Retrying a TRANSIENT error (timeout, cold start, endpoint not found) repeats the same call and is not the follow-up." in rule
    assert 'an "ambiguous" name with candidates' in rule
    assert "each of these IS the one follow-up" in rule
    if name.endswith("-orchestrator"):
        assert 'an "ambiguous" name with candidates, or a schema_mismatch' in rule
        assert "the exact name, or the corrected query; each of these IS the one follow-up" in rule
    if name.endswith("-agent"):
        assert "schema_mismatch" not in prompt   # Mode A system agents own schema errors
    if "re-delegate ONCE with the exact name" in prompt:     # telemetry fuzzy resolution
        assert "re-delegate ONCE with the exact name of the asset the user means (that IS the system's one MISS FOLLOW-UP)" in prompt
    if "schema_mismatch`, it includes a `schema_hint`" in prompt:   # Mode B SQL self-correction
        assert "retry the query once (that retry IS the system's one MISS FOLLOW-UP)" in prompt


@pytest.mark.parametrize("mode", MODES)
def test_full_prompts_tie_transient_retries_to_the_same_call(mode):
    prompt = _flat(orch._build_orchestrator_prompt(mode))
    assert "RETRY THE SAME" in prompt
    assert "These retries repeat the SAME call; they are not the MISS FOLLOW-UP." in prompt


def test_the_quick_skill_doc_carries_the_rule_and_no_one_call_limit():
    from agents.workflow_guidance import render_quick_skill_markdown
    doc = _flat(render_quick_skill_markdown())
    assert doc.count(RULE) == 1
    assert "Per system: ONE run_query call" in doc
    assert not _ABSOLUTE_ONE_CALL.search(doc)
    assert "at most ONE call per system" not in doc
