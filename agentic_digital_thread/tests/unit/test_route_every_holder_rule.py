# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The EVERY HOLDER routing rule rides every composed orchestrator prompt.

Observed live (workshop test event 29, three of three trials): after the
attendee onboarded the plant's maintenance system, questions asking which work
orders were at risk for a machine, or what to hold for the machine that made a
run, never delegated to it. The planner treated "work orders" as ONE system's
records and, under the minimize-tool-calls discipline, picked the execution
system as that holder; nothing in the standing prompt said that a record kind
held by several systems means several delegations. The rule states that in
domain-neutral terms (system TYPES only — no plant, system id or story
identifier; the engine domain-neutrality ratchet guards the literal set) and
sits in the shared Step-2 routing text, next to the MISS FOLLOW-UP budget, so
both modes and both Quick slices carry it once.
"""
import re

import pytest

import agents.orchestrator as orch
from agents import workflow_guidance as wg
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")
HEADING = "EVERY HOLDER OF A RECORD KIND"
MAX_RULE_CHARS = 1_000
_ABSOLUTE_ONE_CALL = re.compile(
    r"AT MOST ONE \S+ call per system|Never issue a second call", re.I)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompts():
    for mode in MODES:
        yield f"full-{mode}", _flat(orch._build_orchestrator_prompt(mode))
        yield f"quick-{mode}", _flat(compose_quick_system_prompt(mode))


PROMPTS = list(_prompts())
IDS = [name for name, _ in PROMPTS]


def test_rule_text_is_bounded_and_domain_neutral():
    rule = wg.EVERY_HOLDER_RULE
    assert rule.startswith(HEADING)
    assert len(rule) <= MAX_RULE_CHARS
    # System TYPES may be named; no system id, plant or story identifier may.
    assert not re.search(r"\b[a-z]+-[a-z]{2,4}\b", rule), "looks like a system id"
    assert not re.search(r"\b[A-Z]{2,4}-\d{2,4}\b", rule), "looks like a story identifier"
    assert not _ABSOLUTE_ONE_CALL.search(rule)


@pytest.mark.parametrize("name,prompt", PROMPTS, ids=IDS)
def test_every_prompt_carries_the_rule_once_before_the_miss_budget(name, prompt):
    assert prompt.count(HEADING) == 1
    assert prompt.index(HEADING) < prompt.index("MISS FOLLOW-UP — ONE, THEN STOP")
    rule = _flat(wg.EVERY_HOLDER_RULE)
    assert rule in prompt


@pytest.mark.parametrize("mode", MODES)
def test_rule_sits_in_the_routing_step_after_parallel_delegation(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    parallel = prompt.index("PARALLEL DELEGATION — DELEGATE TO RELEVANT SYSTEMS CONCURRENTLY")
    translate = prompt.index("Step 3 — TRANSLATE IDENTIFIERS")
    assert parallel < prompt.index(HEADING) < translate


def test_rule_names_the_seed_signals_and_the_question_shapes():
    rule = _flat(wg.EVERY_HOLDER_RULE)
    # The signals the planner already has: per-system prose and shared concepts.
    assert "capabilities" in rule and "query_guidance" in rule
    assert "shared_concepts" in rule
    # The question shapes that make every holder a candidate.
    for cue in ("open", "due", "at risk", "affected", "to be held"):
        assert cue in rule, cue
    # Each holder is asked and reported under its own name; one call each.
    assert "EVERY connected system" in rule
    assert "delegate to each of them in the same parallel step" in rule
    assert "under that system's own name" in rule
    assert "routing miss, not a saved call" in rule
    # Guidance saying a system holds none of that kind still excludes it.
    assert "holds no records of that kind is not a candidate" in rule
