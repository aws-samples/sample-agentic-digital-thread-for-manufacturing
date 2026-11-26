# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every measured pair is one set, and a translation is measured too.

Two holes in the Resolve evidence gate:

* A rule that leaves out a table was paired with the pairs that SHARE values
  first, and only then with the pairs measured with nothing shared. When one
  table's pair shared values and another table's pair of the same column
  names shared none, the rule took the first one's numbers (and the storable
  gate rebuilt it on them), though it could name either. The gate now pairs a
  rule with every measured pair at once, sharing values or not: more than
  one candidate is ambiguous and the rule is withheld.
* A rule whose pattern translates a value (literal text around a
  {placeholder}) on a pair measured with nothing shared was let through on
  its field names. Zero shared raw values is what a real translation looks
  like, but it is no evidence that this translation relates the two columns.
  Now the pattern is applied to the new side's sampled values, and the
  values it gives are looked for in the target (its sample, and a search
  where the connection allows one). The rule is kept only on the usual bar
  (3 values, half the smaller side); otherwise it is withheld with what was
  measured. The name-only pass is left for a pair one side could not sample.

Fixture names are generic.
"""
from __future__ import annotations

import pytest

from agents import resolve_gates
from tests.unit.test_resolve_rules_are_storable import (
    NEW, PEER, _mapping, _model_rule, _peer_row, wizard_resolve)

JOBS = ["J-101", "J-102", "J-103"]
ORDERS = ["ORD-7781", "ORD-7782", "ORD-7790"]


# ── HIGH 2: one set of measured pairs ───────────────────────────────────────

def _two_tables(monkeypatch, patterns, probe=None):
    """``jobs.job_ref`` shares its values with the peer, ``archive_jobs.job_ref``
    shares none; the rule names the bare column."""
    calls: list = []
    result = wizard_resolve(
        monkeypatch,
        mappings=[_mapping("jobs", "job_ref", "work-order"),
                  _mapping("archive_jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): JOBS, (NEW, "archive_jobs", "job_ref"): ["A-1", "A-2"],
                 (PEER, "orders", "order_ref"): JOBS},
        model_rules=[_model_rule("job_ref", "order_ref", "work-order", patterns)],
        probe=lambda **kw: calls.append(kw) or (probe(**kw) if probe else []))
    return result, calls


AMBIGUOUS = "the measured evidence is ambiguous: 2 measured field pairs share these column names"


@pytest.mark.parametrize("patterns", [("", ""), ("{n}", "{n}"), ("^J-\\d+$", "^J-\\d+$x")],
                         ids=["exact", "same-placeholder", "unstorable"])
def test_a_bare_rule_two_measured_pairs_could_mean_is_withheld(monkeypatch, patterns):
    """One pair shares values and the other shares none: still two
    candidates, so the rule is withheld, never judged or rebuilt on the
    first one, and no search is spent on it. Naming neither pair, it does
    not keep Resolve from proposing the pair that shares values, as it would
    had the model sent no rule."""
    result, calls = _two_tables(monkeypatch, patterns)
    assert [(r["source_field"], resolve_gates.MEASURED_PAIR_KEY in r)
            for r in result["rules"]] == [("jobs.job_ref", True)]
    assert [r["rejected_reason"] for r in result["rejected_rules"]] == [AMBIGUOUS]
    assert calls == []


def test_a_qualified_rule_is_judged_on_its_own_pair(monkeypatch):
    result = wizard_resolve(
        monkeypatch,
        mappings=[_mapping("jobs", "job_ref", "work-order"),
                  _mapping("archive_jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): JOBS, (NEW, "archive_jobs", "job_ref"): ["A-1", "A-2"],
                 (PEER, "orders", "order_ref"): JOBS},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("", ""))])
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]


def test_the_gate_reads_both_kinds_of_row_together():
    positive = {"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
                "target_field": "orders.order_ref", "overlap_count": 3, "unchanged_count": 3,
                "examples": JOBS, "ratio": 1.0}
    empty = {**positive, "source_field": "archive_jobs.job_ref", "overlap_count": 0,
             "unchanged_count": 0, "examples": [], "ratio": 0.0, "a_tokens": 2, "b_tokens": 3}
    rule = _model_rule("job_ref", "order_ref", "work-order", ("^J$", "^O$"))
    result = {"rules": [rule], "evidence": [positive], resolve_gates.UNSHARED_PAIRS_KEY: [empty]}
    assert resolve_gates.rule_rejection(rule, [positive], [empty]) == AMBIGUOUS
    stored = resolve_gates.gate_storable_rules(result)
    assert stored["rules"] == []
    assert "pattern_origin" not in str(stored)


# ── HIGH 3: a translation on a measured-empty pair is measured ──────────────

def _translate(monkeypatch, patterns, target_values, probe=None, source_values=JOBS):
    calls: list = []
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): list(source_values),
                 (PEER, "orders", "order_ref"): list(target_values)},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)],
        probe=lambda **kw: calls.append(kw) or (probe(**kw) if probe else None))
    return result, calls


TRANSLATE = ("J-{n}", "ORD-{n}")


def test_an_unrelated_translation_is_withheld_with_what_was_measured(monkeypatch):
    """J-101 becomes ORD-101, which the target does not hold."""
    result, calls = _translate(monkeypatch, TRANSLATE, ORDERS, probe=lambda **_kw: [])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        f"measured through its pattern: 3 of the 3 values sampled from jobs.job_ref translate, "
        f"and 0 of the translated values are in {PEER} orders.order_ref (its sample and a "
        f"search) (need >= 3)")
    (call,) = calls
    assert sorted(call["values"]) == ["ORD-101", "ORD-102", "ORD-103"]


def test_a_translation_the_target_holds_is_kept_on_the_measurement(monkeypatch):
    result, _calls = _translate(monkeypatch, TRANSLATE, ["ORD-101", "ORD-102", "ORD-103", "ORD-9"])
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]
    (check,) = result[resolve_gates.TRANSFORM_CHECKS_KEY]
    assert (check["overlap_count"], check["examples"]) == (3, ["ORD-101", "ORD-102", "ORD-103"])


def test_a_sparse_translation_is_found_by_the_search(monkeypatch):
    """The target sample misses the translated values; the search finds them."""
    result, _calls = _translate(monkeypatch, TRANSLATE, ORDERS, probe=lambda **kw: list(kw["values"]))
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]


def test_a_target_that_cannot_be_searched_is_compared_with_its_sample(monkeypatch):
    result, _calls = _translate(monkeypatch, TRANSLATE, ["ORD-101"])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        f"measured through its pattern: 3 of the 3 values sampled from jobs.job_ref translate, "
        f"and 1 of the translated values is in {PEER} orders.order_ref (its sample; {PEER} could "
        f"not be searched by value) (need >= 3)")


def test_a_pattern_that_fits_no_sampled_value_is_withheld(monkeypatch):
    result, calls = _translate(monkeypatch, ("WO-{n}", "ORD-{n}"), ORDERS, probe=lambda **_kw: [])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        "measured, nothing shared: its pattern 'WO-{n}' fits none of the 3 values sampled "
        "from jobs.job_ref")
    assert calls == []


def test_too_few_translated_values_against_a_large_target_fail_the_ratio(monkeypatch):
    source = [f"J-{n}" for n in range(100, 112)]
    target = [f"ORD-{n}" for n in range(100, 103)] + [f"ORD-{n}" for n in range(900, 912)]
    result, _calls = _translate(monkeypatch, TRANSLATE, target, source_values=source)
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        f"measured through its pattern: 3 of the 12 translated values of jobs.job_ref are in "
        f"{PEER} orders.order_ref (its sample; {PEER} could not be searched by value), 25% of "
        "the smaller side (need >= 50%)")


def test_a_model_written_check_is_not_taken(monkeypatch):
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", TRANSLATE)
    forged = {"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
              "target_field": "orders.order_ref", "source_pattern": "J-{n}",
              "target_pattern": "ORD-{n}", "overlap_count": 9, "translated": 9, "a_tokens": 9,
              "b_tokens": 9, "ratio": 1.0, "examples": ["ORD-1"]}
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): JOBS, (PEER, "orders", "order_ref"): ORDERS},
        model_rules=[{**rule, resolve_gates.TRANSFORM_CHECKS_KEY: [forged], "transform_check": forged}],
        probe=lambda **_kw: [])
    assert result["rules"] == []


@pytest.mark.parametrize("source, target, patterns", [
    (["LN1", "LN2", "LN3"], ["NORTH-L1", "NORTH-L2", "NORTH-L3"], ("LN{num}", "NORTH-L{num}")),
    (["NORTH-L1", "NORTH-L2", "NORTH-L3"], ["LINE1", "LINE2", "LINE3", "LINE4"],
     ("NORTH-L{n}", "LINE{n}")),
], ids=["line-to-area-line", "area-line-to-line"])
def test_a_line_translation_whose_values_exist_on_the_target_is_kept(monkeypatch, source, target,
                                                                      patterns):
    """The production-line translations the chat prompt asks for: an area or
    site prefix with a line suffix on one side, a plain line number on the
    other."""
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("cells", "line_code", "production-line")],
        peer_rows=[_peer_row("lines", "line_id", "production-line")],
        samples={(NEW, "cells", "line_code"): source, (PEER, "lines", "line_id"): target},
        model_rules=[_model_rule("cells.line_code", "lines.line_id", "production-line", patterns)])
    assert [r["source_field"] for r in result["rules"]] == ["cells.line_code"], result


def test_a_translation_on_a_pair_one_side_could_not_sample_keeps_the_name_pass(monkeypatch):
    result, calls = _translate(monkeypatch, TRANSLATE, [])
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]
    assert calls == []


def test_a_wildcard_target_is_compared_with_the_sample_only(monkeypatch):
    """``{*}`` renders as a LIKE pattern, not a value, so it is matched
    against the target's sample and never sent to a search."""
    revisions = ["ORD-101-A", "ORD-102-B", "ORD-103-A", "ORD-104-C"]
    result, calls = _translate(monkeypatch, ("J-{n}", "ORD-{n}-{*}"), revisions,
                               probe=lambda **kw: list(kw["values"]))
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]
    assert calls == []
    few, _calls = _translate(monkeypatch, ("J-{n}", "ORD-{n}-{*}"), revisions[:1])
    assert few["rejected_rules"][0]["rejected_reason"].endswith(
        "(its sample; a translated value with a wildcard is compared with the sample only) "
        "(need >= 3)")


@pytest.mark.parametrize("pattern, value, expected", [
    ("ORD-101-%", "ORD-101-A", True), ("ORD-101-%", "ORD-1010-A", False),
    ("A\\_B%", "A_B7", True), ("A\\_B%", "AxB7", False), ("A_B", "AxB", True),
    ("50\\%", "50%", True), ("50\\%", "500", False), ("ord-1%", "ORD-1", False),
])
def test_like_matching_reads_escapes_and_case_like_postgresql(pattern, value, expected):
    from agents.onboard import _like_matches
    assert _like_matches(pattern, value) is expected


def test_a_regular_expression_on_a_measured_empty_pair_shows_provisions_reason(monkeypatch):
    """A pattern provision cannot store translates nothing, so it is never
    tried on the values: it is withheld with provision's own reason, the one
    the checkpoint's withheld list shows."""
    patterns = ("^J-[0-9]+$", "^ORD-[0-9]+$")
    result, calls = _translate(monkeypatch, patterns, ORDERS, probe=lambda **_kw: [])
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == resolve_gates.pattern_rejection(
        _model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns))
    assert result[resolve_gates.TRANSFORM_CHECKS_KEY] == []
    assert calls == []
