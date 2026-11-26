# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A field pair measured with no shared value is not an unmeasured pair.

The Resolve concept pass samples both fields of every candidate pair and
measures their overlap, but it kept a row only when the overlap was above
zero. A pair sampled on both sides that shared nothing then looked exactly
like a pair it could not sample, and the evidence gate let both through
whenever the two field names look like identifiers: a model's guess that two
unrelated identifiers match unchanged reached the checkpoint and was written
unchecked.

Now each pair sampled on both sides with nothing shared is kept under
``unshared_pairs`` on the Resolve result, with how many values each side
sampled. The model prompt and the measured ``evidence`` are what they were.
For a candidate rule on such a pair that says the values match unchanged,
the new side's sampled values are looked up in the target column when its
connection can be searched by value; values found there become a measured
evidence row judged like any other, and otherwise the rule is withheld with
a reason that states what was measured. The identifier-name pass is left
only for a pair one side could not sample, and a translation pattern is not
judged by unchanged values. Fixture names are generic.
"""
from __future__ import annotations

import pytest

from agents import resolve_gates
from tests.unit.test_resolve_rules_are_storable import (
    NEW, PEER, _mapping, _model_rule, _peer_row, wizard_resolve)

JOBS = ["J-101", "J-102", "J-103"]
ORDERS = ["ORD-7781", "ORD-7782", "ORD-7790"]
MAPPINGS = [_mapping("jobs", "job_ref", "work-order")]
PEERS = [_peer_row("orders", "order_ref", "work-order")]
BOTH_SAMPLED = {(NEW, "jobs", "job_ref"): JOBS, (PEER, "orders", "order_ref"): ORDERS}
UNCHANGED = ("{id}", "{id}")
MEASURED = (f"measured, nothing shared: none of the 3 values sampled from jobs.job_ref is "
            f"among the 3 sampled from {PEER} orders.order_ref")


def _resolve(monkeypatch, patterns=UNCHANGED, samples=None, probe=None, **kw):
    calls: list = []

    def probe_fn(**args):
        calls.append(args)
        return probe(**args) if probe else None

    result = wizard_resolve(
        monkeypatch, mappings=MAPPINGS, peer_rows=PEERS,
        samples=BOTH_SAMPLED if samples is None else samples,
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)],
        probe=probe_fn, **kw)
    return result, calls


def test_a_pair_sampled_on_both_sides_with_nothing_shared_is_kept_as_measured(monkeypatch):
    result, _calls = _resolve(monkeypatch)
    assert result["evidence"] == []
    (row,) = result["unshared_pairs"]
    assert {k: row[k] for k in ("concept", "source_field", "target_system", "target_field",
                                "overlap_count", "a_tokens", "b_tokens")} == {
        "concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
        "target_field": "orders.order_ref", "overlap_count": 0, "a_tokens": 3, "b_tokens": 3}


def test_an_unchanged_value_guess_on_a_measured_empty_pair_is_withheld(monkeypatch):
    """Both names look like identifiers, which used to be enough."""
    result, calls = _resolve(monkeypatch)
    assert result["rules"] == []
    (withheld,) = result["rejected_rules"]
    assert withheld["rejected_reason"] == (
        f"{MEASURED}; {PEER} could not be searched by value, so only the samples were compared")
    (call,) = calls
    assert (call["system_id"], call["table"], call["field"]) == (PEER, "orders", "order_ref")
    assert sorted(call["values"]) == JOBS


def test_the_reason_says_when_the_target_was_searched_and_held_none(monkeypatch):
    result, _calls = _resolve(monkeypatch, probe=lambda **_kw: [])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        f"{MEASURED}; a search of {PEER} orders.order_ref for all 3 found none")


def test_values_the_search_finds_become_measured_evidence_and_the_rule_is_judged_on_it(
        monkeypatch):
    """A sparse key: the target sample missed the values, the search finds
    them. The rule is judged on what was found, like any measured pair."""
    result, _calls = _resolve(monkeypatch, probe=lambda **kw: list(kw["values"]))
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]
    (row,) = result["evidence"]
    assert (row["source_field"], row["target_field"], row["overlap_count"], row["unchanged_count"],
            row["examples"]) == ("jobs.job_ref", "orders.order_ref", 3, 3, JOBS)
    assert row["probed"] is True
    assert result["unshared_pairs"] == []


def test_too_few_found_values_are_withheld_on_the_usual_count(monkeypatch):
    result, _calls = _resolve(monkeypatch, probe=lambda **_kw: ["J-102"])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == "only 1 shared value(s) (need >= 3)"


def test_a_pair_one_side_could_not_sample_keeps_the_identifier_name_pass(monkeypatch):
    result, calls = _resolve(monkeypatch, samples={(NEW, "jobs", "job_ref"): JOBS})
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]
    assert result.get("unshared_pairs", []) == []
    assert calls == []


def test_a_translation_pattern_is_searched_for_its_translated_values(monkeypatch):
    """Raw values of a translated pair share nothing by design, so the
    search asks for what the pattern makes of them, never the raw values
    (``test_resolve_measured_pairs_and_translations`` judges the result)."""
    result, calls = _resolve(monkeypatch, patterns=("J-{n}", "ORD-{n}"), probe=lambda **_kw: [])
    assert result["rules"] == []
    (call,) = calls
    assert sorted(call["values"]) == ["ORD-101", "ORD-102", "ORD-103"]


def test_the_model_prompt_and_value_mappings_are_unchanged(monkeypatch):
    prompts: list = []
    mapping = {"from_system": NEW, "from_field": "jobs.job_ref", "from_value": "J-101",
               "to_system": PEER, "to_field": "orders.order_ref", "to_value": "ORD-7781",
               "concept": "work-order", "confidence": 0.8, "method": "name_evidence"}
    result, _calls = _resolve(monkeypatch, prompts=prompts, value_mappings=[mapping])
    (prompt,) = prompts
    assert "(no measured value overlap found between candidate fields)" in prompt
    assert "unshared" not in prompt and "0 shared" not in prompt
    assert result["value_mappings"] == [mapping]


def test_an_entity_anchored_pair_with_nothing_shared_is_kept_as_measured(monkeypatch):
    entity_rows = [
        {"sys_id": NEW, "table_name": "jobs", "entity": "work-order", "id_fields": ["job_ref"]},
        {"sys_id": PEER, "table_name": "orders", "entity": "work-order", "id_fields": ["order_ref"]},
    ]
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "needs_review")], peer_rows=PEERS,
        samples=BOTH_SAMPLED, entity_rows=entity_rows,
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", UNCHANGED)])
    assert [(r["source_field"], r["target_field"]) for r in result["unshared_pairs"]] == [
        ("jobs.job_ref", "orders.order_ref")]
    assert result["rules"] == []


# ── the gate itself ─────────────────────────────────────────────────────────

def _unshared(source_field, target_field, **extra):
    return {"concept": "work-order", "source_field": source_field, "target_system": PEER,
            "target_field": target_field, "overlap_count": 0, "examples": [], "a_tokens": 12,
            "b_tokens": 40, "ratio": 0.0, **extra}


def test_gate_overlap_rules_reads_the_measured_empty_pairs():
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("", ""))
    out = resolve_gates.gate_overlap_rules(
        {"rules": [rule], "evidence": [], "unshared_pairs": [_unshared("jobs.job_ref",
                                                                       "orders.order_ref")]})
    assert out["rules"] == []
    assert out["rejected_rules"][0]["rejected_reason"] == (
        f"measured, nothing shared: none of the 12 values sampled from jobs.job_ref is among "
        f"the 40 sampled from {PEER} orders.order_ref")


def test_a_rule_that_dropped_a_table_two_measured_empty_pairs_share_is_withheld():
    rule = _model_rule("job_ref", "order_ref", "work-order", ("*", "*"))
    out = resolve_gates.gate_overlap_rules({"rules": [rule], "evidence": [], "unshared_pairs": [
        _unshared("jobs.job_ref", "orders.order_ref"),
        _unshared("archive_jobs.job_ref", "orders.order_ref")]})
    assert out["rules"] == []
    assert out["rejected_rules"][0]["rejected_reason"] == (
        "the measured evidence is ambiguous: 2 measured field pairs share these column names")


@pytest.mark.parametrize("rule", [
    _model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("", "")),
    _model_rule("jobs.job_ref", "other.order_ref", "work-order", ("", "")),
], ids=["measured-with-overlap", "never-measured"])
def test_positive_evidence_and_unmeasured_pairs_are_judged_as_before(rule):
    evidence = [{"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
                 "target_field": "orders.order_ref", "overlap_count": 3, "unchanged_count": 3,
                 "examples": JOBS, "ratio": 1.0}]
    out = resolve_gates.gate_overlap_rules({"rules": [rule], "evidence": evidence,
                                            "unshared_pairs": [_unshared("jobs.job_ref",
                                                                         "other.x")]})
    assert out["rules"] == [rule]


def test_a_bare_rule_one_shared_and_one_empty_pair_could_mean_is_withheld(monkeypatch):
    """The rule leaves out the table; one measured pair shares values and an
    archive table's pair shares none. Both are candidates, so the rule is
    ambiguous: withheld, and no search is spent on it. It names neither
    pair, so Resolve still proposes the one that shares values."""
    mappings = MAPPINGS + [_mapping("archive_jobs", "job_ref", "work-order")]
    samples = {**BOTH_SAMPLED, (PEER, "orders", "order_ref"): JOBS,
               (NEW, "archive_jobs", "job_ref"): ["A-1", "A-2", "A-3"]}
    calls: list = []
    result = wizard_resolve(
        monkeypatch, mappings=mappings, peer_rows=PEERS, samples=samples,
        model_rules=[_model_rule("job_ref", "order_ref", "work-order", ("", ""))],
        probe=lambda **kw: calls.append(kw) or list(kw["values"]))
    assert [(r["source_field"], resolve_gates.MEASURED_PAIR_KEY in r)
            for r in result["rules"]] == [("jobs.job_ref", True)]
    assert result["rejected_rules"][0]["rejected_reason"] == (
        "the measured evidence is ambiguous: 2 measured field pairs share these column names")
    assert calls == []


def test_one_pair_is_searched_once_however_many_rules_name_it(monkeypatch):
    calls: list = []
    rules = [_model_rule("jobs.job_ref", "orders.order_ref", "work-order", p)
             for p in (("", ""), UNCHANGED)]
    result = wizard_resolve(monkeypatch, mappings=MAPPINGS, peer_rows=PEERS, samples=BOTH_SAMPLED,
                            model_rules=rules, probe=lambda **kw: calls.append(kw) or [])
    assert len(calls) == 1
    assert result["rules"] == []


def test_the_storable_gate_never_builds_a_rule_the_evidence_gate_refuses():
    """Two shared values, unchanged: an exact-agreement rule would still be
    below the evidence bar, so the model's unstorable rule is withheld with
    provision's reason rather than rebuilt."""
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("^J", "^O"))
    row = {"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
           "target_field": "orders.order_ref", "overlap_count": 2, "unchanged_count": 2,
           "examples": JOBS[:2], "ratio": 1.0}
    out = resolve_gates.gate_storable_rules({"rules": [rule], "evidence": [row]})
    assert out["rules"] == []
    assert out["rejected_rules"][0]["rejected_reason"] == resolve_gates.pattern_rejection(rule)
