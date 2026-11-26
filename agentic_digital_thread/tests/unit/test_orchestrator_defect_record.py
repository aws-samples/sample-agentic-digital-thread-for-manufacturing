# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A defect report that names no record finds it by the defect, not by recency.

Observed (rehearsal, 2026-10-04, an account with three days of data): a
customer complaint about out-of-tolerance parts "received this week" asked
which batch they came from. The customer had hundreds of shipment lines that
week; the turn asked no system for a failed check and named the single most
recent batch. Another run checked only the first ten batches a query
returned. The runs that found the failed batch asked the plant-floor systems
for failures matching the defect. The standing prompt now says so in Step 1.
"""

import re

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_both_modes_carry_the_rule_at_the_end_of_step_one(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count(orch._DEFECT_RECORD_RULE) == 1
    assert prompt.index(orch._DEFECT_RECORD_RULE) == (
        prompt.index(orch._DEFECT_RECORD_ANCHOR) + len(orch._DEFECT_RECORD_ANCHOR))
    assert prompt.index("Step 1 — DECOMPOSE") < prompt.index(orch._DEFECT_RECORD_RULE)
    assert prompt.index(orch._DEFECT_RECORD_RULE) < prompt.index("Step 2 — ROUTE")


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_quick_instructions_carry_the_rule(mode):
    assert orch._DEFECT_RECORD_RULE.strip() in compose_quick_system_prompt(mode)


def test_anchor_appears_once_in_the_base_prompt():
    for mode in ("agent", "orchestrator"):
        assert orch._base_orchestrator_prompt(mode).count(orch._DEFECT_RECORD_ANCHOR) == 1


def test_rule_asks_for_the_failures_and_forbids_recency_volume_and_sampling():
    rule = orch._DEFECT_RECORD_RULE.lower()
    assert "failures" in rule and "quality checks" in rule and "inspections" in rule
    assert "nonconformances" in rule
    assert "recency, volume or a sample" in rule
    # Honest absence: no matching failure means candidates, not a guess.
    assert "say so" in rule and "list the candidates" in rule


def test_rule_triggers_only_on_an_unnamed_defective_record():
    rule = " ".join(orch._DEFECT_RECORD_RULE.split()).lower()
    assert "complaint says goods are defective" in rule
    assert "without naming that record" in rule


def test_rule_exempts_tracing_from_a_named_record():
    """Case: "Batch X failed inspection; which material lot did it
    use?" reports a defect and asks for an unnamed lot, but the defective
    record IS named, so it must be traced through consumption evidence, not
    found again by searching for a new failure."""
    rule = " ".join(orch._DEFECT_RECORD_RULE.split()).lower()
    assert "tracing from a named record (a failed batch to its lots)" in rule
    assert rule.rstrip().endswith("are unaffected.")


def test_rule_leaves_latest_record_questions_alone():
    rule = " ".join(orch._DEFECT_RECORD_RULE.split()).lower()
    assert "asking for the latest record are unaffected" in rule
    # The prohibition is on picking the DEFECTIVE record by recency only.
    assert rule.index("without naming that record") < rule.index("recency")


def test_rule_names_no_story_identifier_or_system():
    rule = orch._DEFECT_RECORD_RULE
    assert not re.search(r"\b(?:RUN|LOT|WO|SHIP)-\d", rule)
    assert not re.search(r"\b\d{4}\b", rule)
    # Company and site literals are held by test_engine_domain_neutrality,
    # which scans agents/orchestrator.py.
    for name in ("Example Corp", "bore", "ERP", "MES"):
        assert name not in rule
    assert "\u2014" not in rule and "\u2013" not in rule


def test_rule_asks_where_the_failed_record_was_made():
    rule = " ".join(orch._DEFECT_RECORD_RULE.split()).lower()
    assert "say where the failed one was made (line, machine) as recorded" in rule
    assert rule.index("match them to the candidates") < rule.index("where the failed one was made")


@pytest.mark.parametrize("mode", ("agent", "orchestrator"))
def test_an_unrecorded_location_is_named_never_inferred(mode):
    """A system that records the line but no machine: the answer names the gap."""
    clause = "name any not recorded and never infer it."
    for prompt in (orch._build_orchestrator_prompt(mode), compose_quick_system_prompt(mode)):
        assert clause in " ".join(prompt.split()).lower()
