# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Asked what values or formats appear in a field, ask for every one with its count.

Measured in an answer eval: asked what identifier format appears in a field,
every delegation asked the system agent for "a representative sample" or "10-20
distinct examples". The agent's top rows by count were then filled by a few
frequent values, and a mid-sized identifier family never reached the answer.
The orchestrator rule: such a sub-question asks for every format (or value)
with its count under the question's own filters, never a sample, examples or
the top few. It rides the end of Step 1 (DECOMPOSE), after the defect-record
rule, so both modes and both Quick instruction variants carry it.
"""
from __future__ import annotations

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")


def _prompt(kind: str, mode: str) -> str:
    if kind == "quick":
        return compose_quick_system_prompt(mode)
    return orch._build_orchestrator_prompt(mode)


@pytest.mark.parametrize("kind", ("standing", "quick"))
@pytest.mark.parametrize("mode", MODES)
def test_every_prompt_carries_the_rule_after_the_defect_rule(kind, mode):
    prompt = _prompt(kind, mode)
    assert (orch._DEFECT_RECORD_RULE + orch._FIELD_VALUES_RULE) in prompt
    assert prompt.count(orch._FIELD_VALUES_RULE) == 1


def test_the_rule_asks_for_every_format_with_its_count_never_a_sample():
    low = " ".join(orch._FIELD_VALUES_RULE.split()).lower()
    assert "values, formats or identifier" in low
    assert "every format" in low and "with its count" in low
    assert "own filters" in low
    never = low[low.index("never"):]
    for word in ("sample", "examples", "top"):
        assert word in never, word


def test_the_rule_is_short_and_uses_no_em_or_en_dashes():
    rule = orch._FIELD_VALUES_RULE
    assert "\u2014" not in rule and "\u2013" not in rule
    assert len(rule) <= 450, len(rule)
