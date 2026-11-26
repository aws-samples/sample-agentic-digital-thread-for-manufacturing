# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A translation Resolve keeps is read alike by both identifier readers.

Two runtime readers apply an identity rule's pattern pair, each in both
directions: the resolver (``resolve.transform_id``) translates one stored
value, the route seed (``identifier_translation.apply``) finds identifiers
in a person's question. The route seed never reads FROM a pattern with no
literal text: a bare ``{id}`` would match every word of a question. So a
model's ``{id} -> ORD-{id}`` kept on measured values worked for the resolver
both ways and for the route seed only from the ``ORD-`` side.

Now a model's translation must be readable by both readers from both sides.
When a side has no literal text but every value the placeholder captured
there shares literal text at its start or end (cut back to where a run of
letters or digits ends, so a run is never split), Resolve moves that text
into both patterns (``{id} -> MFG-{id}`` over ``WO1001`` becomes
``WO{id} -> MFG-WO{id}``), measures the completed pair and keeps it in that
form, marked. Otherwise it is withheld with the reason, and so is a pair the
two readers would read differently. Provision refuses such a pair from a
request the same way. The deterministic affinity pass's rules (a measured
method) keep their documented one-way reading, as before. Fixture names are
generic.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents import resolve_gates
from agents.identifier_translation import apply
from agents.resolve import transform_id
from tests.unit.test_resolve_measured_pairs_and_translations import _translate
from tests.unit.test_resolve_rules_are_storable import PEER, _model_rule

ONE_WAY_SOURCE = (
    "pattern '{id}' -> 'ORD-{id}' works in one direction only: its source pattern has no literal "
    "text, so the question reader never translates a value from that side")


def _both_readers(sp, tp, value, expected):
    """Both readers translate ``value`` to ``expected`` and back again."""
    assert transform_id(sp, tp, value) == expected
    assert apply(sp, tp, value) == expected
    assert transform_id(tp, sp, expected) == value
    assert apply(tp, sp, expected) == value


def test_the_reviewers_case_reads_one_way_on_the_old_tree():
    """The cause: the resolver reads the bare side, the route seed cannot."""
    assert transform_id("{id}", "ORD-{id}", "101") == "ORD-101"
    assert apply("{id}", "ORD-{id}", "101") is None
    assert apply("ORD-{id}", "{id}", "ORD-101") == "101"


def test_a_bare_side_with_nothing_to_add_is_withheld(monkeypatch):
    result, _calls = _translate(monkeypatch, ("{id}", "ORD-{id}"), ["ORD-101", "ORD-102", "ORD-103"],
                                source_values=["101", "102", "103"])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        "measured through its pattern: its source pattern '{id}' has no literal text, so the "
        "question reader never translates a value of jobs.job_ref, and the values sampled from it "
        "share no literal text to add")


def test_values_that_differ_in_their_first_letters_give_nothing_to_add(monkeypatch):
    result, _calls = _translate(monkeypatch, ("{id}", "ORD-{id}"),
                                ["ORD-A101", "ORD-B102", "ORD-C103"],
                                source_values=["A101", "B102", "C103"])
    assert result["rules"] == []
    assert "share no literal text to add" in result["rejected_rules"][0]["rejected_reason"]


@pytest.mark.parametrize("patterns, source, target, completed", [
    (("{id}", "MFG-{id}"), ["WO1001", "WO1002", "WO1017"],
     ["MFG-WO1001", "MFG-WO1002", "MFG-WO1017"], ("WO{id}", "MFG-WO{id}")),
    (("{id}", "P-{id}"), ["4471-B", "4472-B", "4480-B"],
     ["P-4471-B", "P-4472-B", "P-4480-B"], ("{id}-B", "P-{id}-B")),
    (("X-{id}", "{id}"), ["X-WO1", "X-WO2", "X-WO3"], ["WO1", "WO2", "WO3"],
     ("X-WO{id}", "WO{id}")),
    (("{n}", "LINE-{n}"), ["PLANT1-0042", "PLANT1-0043", "PLANT1-0051"],
     ["LINE-PLANT1-0042", "LINE-PLANT1-0043", "LINE-PLANT1-0051"],
     ("PLANT1-{n}", "LINE-PLANT1-{n}")),
], ids=["shared-prefix", "shared-suffix", "bare-target", "prefix-cut-at-a-separator"])
def test_a_bare_side_whose_values_share_literal_text_is_completed(monkeypatch, patterns, source,
                                                                  target, completed):
    """The completed pair is kept, marked, and both readers translate every
    sampled value the same way in both directions."""
    result, _calls = _translate(monkeypatch, patterns, target, source_values=source)
    (rule,) = result["rules"]
    assert (rule["source_pattern"], rule["target_pattern"]) == completed
    assert "set from the measurement" in rule["pattern_origin"]
    for value, expected in zip(source, target):
        _both_readers(*completed, value, expected)


def test_a_completed_pair_the_readers_read_differently_is_withheld(monkeypatch):
    """``Q1.2``: the resolver's placeholder takes ``1.2``, the route seed's
    one word cannot."""
    result, _calls = _translate(monkeypatch, ("{id}", "R{id}"), ["RQ1.2", "RQ3.4", "RQ5.6"],
                                source_values=["Q1.2", "Q3.4", "Q5.6"])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        "measured through its pattern: the two identifier readers would read value 'Q1.2' "
        "differently with 'Q{id}' -> 'RQ{id}'")


def test_a_literal_bearing_translation_is_kept_as_the_model_wrote_it(monkeypatch):
    source = ["J-101", "J-102", "J-103"]
    target = ["ORD-101", "ORD-102", "ORD-103"]
    result, _calls = _translate(monkeypatch, ("J-{n}", "ORD-{n}"), target, source_values=source)
    (rule,) = result["rules"]
    assert (rule["source_pattern"], rule["target_pattern"]) == ("J-{n}", "ORD-{n}")
    assert "pattern_origin" not in rule
    for value, expected in zip(source, target):
        _both_readers("J-{n}", "ORD-{n}", value, expected)


def test_the_checkpoint_line_finds_the_completed_check(monkeypatch):
    result, _calls = _translate(monkeypatch, ("{id}", "MFG-{id}"),
                                ["MFG-WO1001", "MFG-WO1002", "MFG-WO1017"],
                                source_values=["WO1001", "WO1002", "WO1017"])
    (check,) = result[resolve_gates.TRANSFORM_CHECKS_KEY]
    assert (check["source_pattern"], check["target_pattern"]) == ("WO{id}", "MFG-WO{id}")
    assert (check["proposed_source_pattern"], check["proposed_target_pattern"]) == (
        "{id}", "MFG-{id}")
    assert check["overlap_count"] == 3


def test_an_unmeasured_one_way_rule_is_withheld_too(monkeypatch):
    """The target gave no sample, so the name pass would keep it; a one-way
    pair is withheld whatever its evidence."""
    result, _calls = _translate(monkeypatch, ("{id}", "ORD-{id}"), [],
                                source_values=["101", "102", "103"])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == ONE_WAY_SOURCE


# ── the shared check ────────────────────────────────────────────────────────

@pytest.mark.parametrize("patterns", [("{id}", "{id}"), ("", ""), ("*", "*"), ("A|B", "A|B")],
                         ids=["same-placeholder", "empty", "bare-star", "direct-values"])
def test_a_pair_that_reads_values_unchanged_is_not_one_way(patterns):
    assert resolve_gates.pattern_rejection(_model_rule("a.x", "b.y", "batch", patterns)) == ""


def test_the_affinity_pass_keeps_its_documented_one_way_reading():
    """A bare batch number to many part revisions: built by the
    deterministic pass from measured value cores, read from the part side by
    the route seed (``test_both_readers_read_every_pair_provision_writes_alike``)."""
    rule = _model_rule("runs.batch", "parts.part_no", "batch", ("{id}", "PRT-{id}-R-{*}"))
    for method in ("value_core_match", "value_exact_match"):
        assert resolve_gates.pattern_rejection({**rule, "method": method}) == ""
    assert resolve_gates.pattern_rejection(rule) == (
        "pattern '{id}' -> 'PRT-{id}-R-{*}' works in one direction only: its source pattern has no "
        "literal text, so the question reader never translates a value from that side")


def test_a_bare_target_is_named_in_the_reason():
    rule = _model_rule("a.x", "b.y", "batch", ("X-{id}", "{id}"))
    assert resolve_gates.pattern_rejection(rule) == (
        "pattern 'X-{id}' -> '{id}' works in one direction only: its target pattern has no literal "
        "text, so the question reader never translates a value from that side")


# ── provision ───────────────────────────────────────────────────────────────

def test_provision_refuses_a_one_way_pair_from_a_request(monkeypatch):
    """The same check provision applies to every rule it writes: a request
    that did not come through Resolve is held to it too."""
    from types import SimpleNamespace

    class _Cat:
        def get(self, _sid):
            return None

        def get_onboarded_ids(self):
            return {PEER}

    recorded = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [{"tbl": "orders"}])
    monkeypatch.setattr(onboard, "catalog", _Cat())
    rule = {"concept": "work-order", "source_field": "job_ref", "source_table": "jobs",
            "source_pattern": "{id}", "target_system": PEER, "target_field": "order_ref",
            "target_table": "orders", "target_pattern": "ORD-{id}", "confidence": 0.8}
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="line-upkeep", system_name="Upkeep",
        plant="", schema="", rules=[rule], mappings=[], connection_type="MCP Server",
        cm_items=[], committable=set())
    onboard._dp_identity_rules(ctx)
    assert [c for c, _p in recorded if c.startswith("MERGE (ir:IdentityRule")] == []
    assert onboard._pattern_pair_rejection(rule) == ONE_WAY_SOURCE
    assert ONE_WAY_SOURCE in str(ctx.rules_rejected)


def test_provision_marks_a_completed_pattern_as_set_from_the_measurement():
    rule = {"source_pattern": "WO{id}", "target_pattern": "MFG-WO{id}",
            "pattern_origin": "set from the measurement"}
    note = onboard._measured_pattern_note(rule, onboard._ONBOARDING_RULE_ORIGIN)
    assert note == onboard._COMPLETED_PATTERN_ORIGIN
    assert "\u2014" not in note and "\u2013" not in note
    empty = {**rule, "source_pattern": "", "target_pattern": ""}
    assert onboard._measured_pattern_note(empty, onboard._ONBOARDING_RULE_ORIGIN) == (
        onboard._MEASURED_PATTERN_ORIGIN)
