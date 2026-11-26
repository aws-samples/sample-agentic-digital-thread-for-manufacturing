# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Step 1 and Step 5 standing rules stay inside fixed prompt budgets.

These rules ride the standing orchestrator prompt in both modes and both Quick
instruction variants, so every character is paid on every turn, so the growth must be
bounded, not only placed. Each budget is measured on the COMPOSED prompts: each one is
rebuilt with its rules emptied and the difference must stay within the budget. The
defect-record and record-key rules share ``MAX_GROWTH`` (raised 900 -> 1000 for the
defect rule's "where the failed one was made, as recorded" clause); the quality
containment rule has its own ``CONTAINMENT_MAX_GROWTH`` (raised 750 -> 1000 for the
clauses that say what a containment plan asks for and when a named scope holds, then
1000 -> 1400 for the clauses that close an interval on a verified correction, start a new
one when the source's newest records show the cause or a sign of it again, and hold the implicated source
itself while the current interval has no verified correction); the instructions-in-data
rule has ``INSTRUCTIONS_IN_DATA_MAX_GROWTH``.
"""

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt

MAX_GROWTH = 1000
CONTAINMENT_MAX_GROWTH = 1400
INSTRUCTIONS_IN_DATA_MAX_GROWTH = 600
MODES = ("agent", "orchestrator")
#: (rule constants emptied together, their shared budget).
BUDGETS = (
    (("_DEFECT_RECORD_RULE", "_RECORD_KEY_RULE"), MAX_GROWTH),
    (("_CONTAINMENT_RULE",), CONTAINMENT_MAX_GROWTH),
    (("_INSTRUCTIONS_IN_DATA_RULE",), INSTRUCTIONS_IN_DATA_MAX_GROWTH),
)
BUDGET_IDS = ("defect-record-and-record-key", "containment", "instructions-in-data")


def _without_rules(monkeypatch, names=("_DEFECT_RECORD_RULE", "_RECORD_KEY_RULE")):
    for name in names:
        monkeypatch.setattr(orch, name, "")


@pytest.mark.parametrize("names,budget", BUDGETS, ids=BUDGET_IDS)
def test_rule_texts_fit_the_budget(names, budget):
    assert sum(len(getattr(orch, name)) for name in names) <= budget


@pytest.mark.parametrize("names,budget", BUDGETS, ids=BUDGET_IDS)
@pytest.mark.parametrize("mode", MODES)
def test_orchestrator_prompt_growth_is_bounded(mode, names, budget, monkeypatch):
    with_rules = orch._build_orchestrator_prompt(mode)
    _without_rules(monkeypatch, names)
    growth = len(with_rules) - len(orch._build_orchestrator_prompt(mode))
    assert 0 < growth <= budget


#: The Quick prompts carry the instructions-in-data rule in its compact form
#: (``workflow_guidance._QUICK_INSTRUCTIONS_IN_DATA_RULE``); emptying the full
#: rule removes the anchor the compact form replaces, so it is measured directly.
QUICK_BUDGETS = BUDGETS[:2]
QUICK_BUDGET_IDS = BUDGET_IDS[:2]
#: 285 leaves room for the full rule's three verbs (ignore, disregard, override).
QUICK_INSTRUCTIONS_IN_DATA_MAX = 285


def test_quick_instructions_in_data_rule_is_compact():
    from agents.workflow_guidance import _QUICK_INSTRUCTIONS_IN_DATA_RULE

    assert len(_QUICK_INSTRUCTIONS_IN_DATA_RULE) <= QUICK_INSTRUCTIONS_IN_DATA_MAX


@pytest.mark.parametrize("names,budget", QUICK_BUDGETS, ids=QUICK_BUDGET_IDS)
@pytest.mark.parametrize("mode", MODES)
def test_quick_instruction_growth_is_bounded(mode, names, budget, monkeypatch):
    with_rules = compose_quick_system_prompt(mode)
    _without_rules(monkeypatch, names)
    # Quick slices the module-level prompt, which renders at import.
    monkeypatch.setattr(orch, "ORCHESTRATOR_PROMPT", orch._build_orchestrator_prompt("agent"))
    growth = len(with_rules) - len(compose_quick_system_prompt(mode))
    assert 0 < growth <= budget
