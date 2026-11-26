# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A rule is judged on its OWN field pair's measured evidence, never another's.

``resolve_gates.evidence_for_rule`` pairs a rule with the evidence row the
resolver measured for it: the exact table-qualified pair first, then, because
the model sometimes drops the table, the column names only. The column-only
fallback took the FIRST row with the same column names. Two tables of one
system often share a column name (``id``, ``code``), so a rule that dropped
the table could be judged, and shown at the checkpoint, on the overlap of a
different table's pair.

Now the fallback reads an end only as the measured end with qualifiers left
out (a dropped table, or the system id the model was shown added in front),
never as another table's column of the same name, and applies only when
exactly one measured row remains once the concept (or the row's declared
related concept) and the target system match too. When more than one
remains the evidence is ambiguous: the gate withholds the rule and says why
(it never guesses), and the checkpoint shows no evidence line. Fixture names
are generic.
"""
from __future__ import annotations

import pytest

from agents import resolve_gates
from agents.resolve_gates import evidence_for_rule, gate_overlap_rules, rule_rejection

PEER, OTHER = "peer-sys", "other-sys"


def _rule(concept, source_field, target_field, target=PEER):
    return {"concept": concept, "source_field": source_field, "source_pattern": "*",
            "target_system": target, "target_field": target_field, "target_pattern": "*",
            "confidence": 0.8, "reasoning": "measured overlap"}


def _ev(concept, source_field, target_field, examples, *, ratio=1.0, target=PEER, **extra):
    return {"concept": concept, "source_field": source_field, "target_system": target,
            "target_field": target_field, "overlap_count": len(examples),
            "unchanged_count": len(examples), "examples": list(examples), "ratio": ratio,
            **extra}


#: Two measured pairs in different tables whose columns share their names.
STRONG = _ev("work-order", "jobs.code", "orders.code", ["J-101", "J-102", "J-103", "J-104"])
WEAK = _ev("work-order", "tasks.code", "steps.code", ["T-9"], ratio=0.1)
TWO_TABLES = [STRONG, WEAK]


def test_each_qualified_rule_gets_its_own_pair_and_never_the_other():
    assert evidence_for_rule(_rule("work-order", "jobs.code", "orders.code"), TWO_TABLES) is STRONG
    assert evidence_for_rule(_rule("work-order", "tasks.code", "steps.code"), TWO_TABLES) is WEAK
    assert evidence_for_rule(_rule("work-order", "tasks.code", "steps.code"),
                             [STRONG]) is None


def test_a_qualified_rule_on_an_unmeasured_pair_takes_no_other_tables_numbers():
    """Both ends name a table and no row measured that pair: no evidence."""
    rule = _rule("work-order", "tasks.code", "orders.code")
    assert evidence_for_rule(rule, TWO_TABLES) is None
    assert evidence_for_rule(rule, [STRONG]) is None


def test_a_bare_rule_with_two_same_name_pairs_is_ambiguous_and_withheld():
    rule = _rule("work-order", "code", "code")
    assert evidence_for_rule(rule, TWO_TABLES) is None
    reason = rule_rejection(rule, TWO_TABLES)
    assert reason == ("the measured evidence is ambiguous: 2 measured field pairs "
                      "share these column names")
    out = gate_overlap_rules({"rules": [rule], "evidence": TWO_TABLES})
    assert out["rules"] == []
    assert out["rejected_rules"][0]["rejected_reason"] == reason


@pytest.mark.parametrize("rule, expected", [
    (_rule("work-order", "code", "orders.code"), STRONG),
    (_rule("work-order", "jobs.code", "code"), STRONG),
    (_rule("work-order", "tasks.code", "code"), WEAK),
], ids=["source-bare", "target-bare", "target-bare-other-table"])
def test_a_half_qualified_rule_pairs_on_the_end_it_qualified(rule, expected):
    assert evidence_for_rule(rule, TWO_TABLES) is expected


def test_the_column_fallback_needs_the_same_concept_or_its_declared_relation():
    bare = _rule("batch", "code", "code")
    assert evidence_for_rule(bare, [STRONG]) is None
    related = _ev("personnel", "work_log.author", "stops.resolved_by",
                  ["tech_01", "tech_04", "tech_07"], related_concept="operator")
    assert evidence_for_rule(_rule("operator", "author", "resolved_by"), [related]) is related
    assert evidence_for_rule(_rule("personnel", "author", "resolved_by"), [related]) is related


def test_the_column_fallback_needs_the_same_target_system():
    elsewhere = dict(STRONG, target_system=OTHER)
    assert evidence_for_rule(_rule("work-order", "code", "code"), [elsewhere]) is None


def test_one_bare_candidate_still_pairs():
    """The case the fallback exists for: one measured pair, table dropped."""
    rule = _rule("work-order", "code", "code")
    assert evidence_for_rule(rule, [STRONG]) is STRONG
    assert rule_rejection(rule, [STRONG]) == ""
    assert rule_rejection(_rule("work-order", "code", "code"), [WEAK]).startswith(
        "only 1 shared value(s)")


def test_a_rule_that_adds_the_system_id_still_pairs():
    """The samples list fields as ``system.table.column``; a rule in that
    spelling names the measured ``table.column``."""
    rule = _rule("work-order", "new-sys.jobs.code", "peer-sys.orders.code")
    assert evidence_for_rule(rule, TWO_TABLES) is STRONG
    assert evidence_for_rule(_rule("work-order", "new-sys.tasks.code", "orders.code"),
                             TWO_TABLES) is None


def test_an_exact_pair_wins_over_an_ambiguous_column_match():
    rule = _rule("work-order", "jobs.code", "orders.code")
    looser = _ev("work-order", "jobs.code", "code", ["X-1"], ratio=0.1)
    assert evidence_for_rule(rule, [looser, STRONG]) is STRONG
    assert resolve_gates.rule_rejection(rule, [looser, STRONG]) == ""
