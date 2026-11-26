# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A rule is judged on what its runtime reader compares, not on the tokens.

The overlap's token reader sets padding aside and reads every scalar member
of a JSON value, so two fields can share tokens while no whole value of one is
a value of the other: two work orders with different ids share their site,
status and type. A rule that reads values unchanged (no pattern, or the same
pattern on both sides) is joined at runtime on the whole value: the JoinPath
reader compares the target column, cast to text, with the value as it is
(``resolve._join_row``), and the route seed carries the identifier it found as
it is (``identifier_translation.find_owners``). So it is kept only when
enough shared values are the same whole value on both sides
(``unchanged_count``), on the usual bar: 3 values and half the smaller side.
A rule whose pattern translates a value is joined on the value its pattern
gives (``resolve.transform_id``), so it is judged on its translation check,
whether or not tokens already overlap.

Fixture names are generic.
"""
from __future__ import annotations

import json

import pytest

import agents.onboard_agent as oa
from agents import resolve_gates
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, world)
from tests.unit.test_chat_resolve_measures_evidence import _setup
from tests.unit.test_resolve_rules_are_storable import (
    NEW, PEER, _mapping, _model_rule, _peer_row, wizard_resolve)


def _resolve(monkeypatch, new_values, target_values, patterns):
    return wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): list(new_values),
                 (PEER, "orders", "order_ref"): list(target_values)},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)])


def _work_order(ident: str) -> str:
    return json.dumps({"id": ident, "site": "PLT-A", "status": "OPEN", "type": "WORK"})


#: The review's case: one object on each side, different ids, the same metadata.
UNRELATED = ([_work_order("SRC-900")], [_work_order("TGT-411")])
ORDERS = ["WO-1001", "WO-1002", "WO-1003", "WO-1004"]
TRANSLATED = ["MFG-1001", "MFG-1002", "MFG-1003", "MFG-1004"]
TRANSLATION = ("WO-{n}", "MFG-{n}")


def _only_reason(result) -> str:
    assert result["rules"] == []
    (withheld,) = result["rejected_rules"]
    return withheld["rejected_reason"]


# ── a rule that reads values unchanged ──────────────────────────────────────

@pytest.mark.parametrize("patterns", [("", ""), ("{n}", "{n}")], ids=["no-pattern", "same-pattern"])
def test_unrelated_objects_that_share_metadata_do_not_prove_an_unchanged_rule(monkeypatch,
                                                                             patterns):
    result = _resolve(monkeypatch, *UNRELATED, patterns)
    (row,) = result["evidence"]
    assert (row["overlap_count"], row["unchanged_count"], row["ratio"]) == (3, 0, 0.75)
    assert _only_reason(result).startswith(
        "0 of the 3 shared values are the same whole value in both fields (need >= 3)")


@pytest.mark.parametrize("dress", [lambda v: json.dumps([v]), lambda v: f" {v} "],
                         ids=["inside-a-json-value", "padded"])
def test_values_equal_only_after_the_token_reader_do_not_prove_an_unchanged_rule(monkeypatch,
                                                                                dress):
    """The tokens match once the reader has set padding aside or read the
    value out of a JSON array; the runtime compares the values as they are."""
    result = _resolve(monkeypatch, [dress(v) for v in ORDERS], ORDERS, ("", ""))
    (row,) = result["evidence"]
    assert (row["overlap_count"], row["unchanged_count"]) == (4, 0)
    assert _only_reason(result).startswith(
        "0 of the 4 shared values are the same whole value in both fields (need >= 3)")


def test_whole_values_under_half_the_smaller_side_do_not_prove_an_unchanged_rule(monkeypatch):
    """Ten shared tokens, three of them whole values: 30% of the smaller side."""
    target = [f"WO-{i}" for i in range(1, 11)]
    new = target[:3] + [f" {v} " for v in target[3:]]
    result = _resolve(monkeypatch, new, target, ("", ""))
    (row,) = result["evidence"]
    assert (row["overlap_count"], row["unchanged_count"]) == (10, 3)
    assert _only_reason(result) == (
        "3 of the 10 shared values are the same whole value in both fields, 30% of the "
        "smaller side (need >= 50%)")


@pytest.mark.parametrize("patterns", [("", ""), ("{n}", "{n}")], ids=["no-pattern", "same-pattern"])
def test_an_unchanged_rule_on_whole_shared_values_is_kept(monkeypatch, patterns):
    result = _resolve(monkeypatch, ORDERS[:3] + ["WO-1009"], ORDERS, patterns)
    (row,) = result["evidence"]
    assert (row["overlap_count"], row["unchanged_count"]) == (3, 3)
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]


# ── a rule whose pattern translates ─────────────────────────────────────────

def test_a_translation_whose_tokens_already_overlap_is_still_checked_through_its_pattern(
        monkeypatch):
    """Both fields hold the same values, so every token overlaps; the pattern
    turns them into values the target does not hold."""
    result = _resolve(monkeypatch, ORDERS, ORDERS, TRANSLATION)
    (row,) = result["evidence"]
    assert row["overlap_count"] == 4
    assert _only_reason(result).startswith(
        "measured through its pattern: 4 of the 4 values sampled from jobs.job_ref translate, "
        f"and 0 of the translated values are in {PEER} orders.order_ref")


def test_a_translation_riding_shared_metadata_is_checked_through_its_pattern(monkeypatch):
    result = _resolve(monkeypatch, *UNRELATED, ("SRC-{n}", "TGT-{n}"))
    assert result["evidence"][0]["overlap_count"] == 3
    assert _only_reason(result).startswith("measured through its pattern: its pattern 'SRC-{n}' "
                                           "fits none of the 1 value sampled from jobs.job_ref")


@pytest.mark.parametrize("incidental", [1, 3])
def test_a_translation_its_check_supports_is_kept_however_many_tokens_overlap(monkeypatch,
                                                                             incidental):
    """The target holds the four translated values and some of the source's
    own values besides: the tokens those share do not decide the rule."""
    result = _resolve(monkeypatch, ORDERS, TRANSLATED + ORDERS[:incidental], TRANSLATION)
    (row,) = result["evidence"]
    assert row["overlap_count"] == incidental
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]
    (check,) = result["transform_checks"]
    assert (check["overlap_count"], check["fit"]) == (4, 4)


def test_a_translation_on_a_pair_the_search_turned_into_evidence_is_still_checked(monkeypatch):
    """An unchanged rule's search finds three of the new side's values in
    the target column, so the pair becomes evidence; a translation on the
    same pair is then checked through its pattern on that pair's samples."""
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): ORDERS, (PEER, "orders", "order_ref"): TRANSLATED},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("", "")),
                     _model_rule("jobs.job_ref", "orders.order_ref", "work-order", TRANSLATION)],
        probe=lambda **kw: [v for v in kw["values"] if v in ORDERS[:3]])
    (row,) = result["evidence"]
    assert row["probed"] and row["unchanged_count"] == 3
    (check,) = result["transform_checks"]
    assert (check["source_pattern"], check["overlap_count"]) == (TRANSLATION[0], 4)


# ── the gate on rows as the server builds them ──────────────────────────────

def _row(**extra):
    return {"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
            "target_field": "orders.order_ref", "overlap_count": 4, "examples": ORDERS,
            "ratio": 1.0, "a_tokens": 4, "b_tokens": 4, **extra}


def _rule(patterns):
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)
    rule["target_system"] = PEER
    return rule


def test_a_shared_pair_row_that_does_not_say_its_whole_values_has_none():
    """Every evidence row the server builds counts them; one that does not is
    no evidence of whole-value agreement."""
    assert resolve_gates.rule_rejection(_rule(("", "")), [_row()]).startswith(
        "0 of the 4 shared values are the same whole value")
    assert resolve_gates.rule_rejection(_rule(("", "")), [_row(unchanged_count=4)]) == ""


def test_a_translation_on_a_shared_pair_no_check_was_made_for_is_withheld():
    assert resolve_gates.rule_rejection(_rule(TRANSLATION), [_row(unchanged_count=4)]) == (
        "its pattern was not applied to the values sampled from jobs.job_ref, so the 4 values "
        "the two fields share say nothing about it")


def test_a_rule_the_evidence_gate_withholds_carries_no_pattern_origin():
    rule = {**_rule(("", "")), "pattern_origin": "set from the measurement"}
    out = resolve_gates.gate_overlap_rules({"rules": [rule], "evidence": [_row()]})
    (withheld,) = out["rejected_rules"]
    assert "pattern_origin" not in withheld and withheld["rejected_reason"]


# ── the chat's resolve judges the same way ──────────────────────────────────

def test_the_chat_withholds_an_unchanged_rule_on_unrelated_objects(world):
    _setup(world, source_values=UNRELATED[0], target_values=UNRELATED[1], patterns=("", ""))
    resolved = oa.resolve_identities(SID)
    assert resolved["rules"] == []
    (withheld,) = resolved["rejected_rules"]
    assert withheld["rejected_reason"].startswith(
        "0 of the 3 shared values are the same whole value in both fields (need >= 3)")


def test_the_chat_checks_a_translation_whose_tokens_overlap(world):
    vouchers = ["VCH-5501", "VCH-5502", "VCH-5517"]
    _setup(world, source_values=vouchers, target_values=vouchers, patterns=("VCH-{n}", "RCV-{n}"))
    resolved = oa.resolve_identities(SID)
    assert resolved["rules"] == []
    (withheld,) = resolved["rejected_rules"]
    assert withheld["rejected_reason"].startswith("measured through its pattern: 3 of the 3 values")
