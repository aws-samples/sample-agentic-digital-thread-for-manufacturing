# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An unscoped question on a multi-site account covers every plant or asks.

Observed (rehearsal, 2026-10-02, four onboarded sites): "How is the plant
doing?" answered for one site only in 3 of 4 runs (golden
``g-ambiguous-scope``). The route seed already carries each System's
``plant``; the standing prompt now says what to do when the question does
not fix one.
"""

import re

import pytest

import agents.orchestrator as orch


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_both_modes_carry_the_site_scope_rule_next_to_the_plant_property(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count(orch._SITE_SCOPE_RULE) == 1
    assert prompt.index(orch._SITE_SCOPE_RULE) == (
        prompt.index(orch._SITE_SCOPE_ANCHOR) + len(orch._SITE_SCOPE_ANCHOR))


def test_rule_covers_every_plant_or_asks_and_names_no_site():
    rule = orch._SITE_SCOPE_RULE.lower()
    assert "every plant" in rule and "ask which plant" in rule
    assert "machine" in rule  # a machine or order fixes the site: no question needed
    assert not re.search(r"\b[A-Z][a-z]+ (?:Rapids|Plant)\b", orch._SITE_SCOPE_RULE)
