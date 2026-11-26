# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A provision-written identity rule with no measured method keeps the 0.7
confidence ceiling.

The W4 calibration caps an onboarding rule's confidence at 0.7 because the
model's own number does not discriminate. It used to lift that cap whenever
the provision pattern check found one source value with the pattern's shape
(``sample_check_passed``), so a rule provision never confirmed in both columns
was written at the model's 0.74 or 0.96 while ``PROVISION_SUMMARY`` counted
it unchecked. Now a rule whose method is not a measured one
(``onboard._has_measured_method``: ``value_exact_match`` or
``value_core_match``, after provision's re-measurement) is written at
min(model confidence, 0.7), then the fan-out division, whether or not its
pattern fit. 0.7 is a ceiling, never a floor: a model 0.5 stays 0.5.

Unchanged, and pinned here: an exact-agreement rule keeps its probe-derived
number; a core-match rule keeps the cap-unless-the-sample-check-passed rule
and is never divided; a ``provision-request`` rule is capped at 0.7 whatever
its check found; a method-less direct match whose values were found in both
columns is written as before.

Rules a person confirmed are written outside provision:
``test_identity_rule_confidence_steward_confirmed.py``.

Neutral fixture: a shift log (``log-sys``, ``shift_notes.signed_by``) and a
line's records (``line-sys``: ``stops.closed_by``, ``checks.signed_off_by``),
both holding operator logins. SQLite stands in for PostgreSQL.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.identity_affinity import AFFINITY_CONFIDENCE, AFFINITY_METHOD, EXACT_METHOD  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "log-sys", "log_db"
TGT_SYS, TGT_SCHEMA = "line-sys", "line_db"
LOGINS = ["tech_01", "tech_02", "tech_07", "lead"]
LISTED = "tech_01|tech_02|tech_07|lead"
#: A transform pattern with a named slot, the shape of an operator-login rule.
OPERATOR = "tech_{operator_id}"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


@pytest.fixture
def writes(monkeypatch):
    written: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: written.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "catalog", FakeCatalog({
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL"}}))
    monkeypatch.setattr(onboard, "pg", RuleCheckDb({
        SRC_SCHEMA: {"shift_notes": (["note_id", "signed_by"], _rows(["A. Lee", *LOGINS]))},
        TGT_SCHEMA: {"stops": (["stop_id", "closed_by"], _rows(LOGINS)),
                     "checks": (["check_id", "signed_off_by"], _rows(LOGINS))},
    }))
    return written


def _rule(**over) -> dict:
    base = {"concept": "operator", "source_system": SRC_SYS,
            "source_field": "shift_notes.signed_by", "source_pattern": OPERATOR,
            "target_system": TGT_SYS, "target_field": "stops.closed_by",
            "target_pattern": OPERATOR, "confidence": 0.74}
    base.update(over)
    return base


def _run(*rules, from_checkpoint: bool = True) -> onboard._ProvisionState:
    # A checkpoint rule is one the request says came from the checkpoint AND
    # that its Resolve result listed (``checkpoint_rule_ids``); without the
    # binding every rule is a provision request's and capped at 0.7 anyway,
    # which would make the checkpoint cases here pass without testing the cap.
    rules = list(rules)
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=SRC_SYS, system_name="Shift Log",
        plant="", schema=SRC_SCHEMA, rules=rules,
        mappings=[{"table": "shift_notes", "field": "shift_notes.signed_by",
                   "concept": "operator"}],
        connection_type="PostgreSQL", cm_items=[], committable=set(),
        tables_seen={"shift_notes"}, rules_from_checkpoint=from_checkpoint,
        checkpoint_rule_ids=frozenset(id(r) for r in rules) if from_checkpoint else frozenset())
    onboard._dp_identity_rules(ctx)
    return ctx


def _written(writes) -> dict:
    """``{target field: params}`` of every IdentityRule MERGE."""
    return {p["tgt_field"]: p for c, p in writes if c.startswith("MERGE (ir:IdentityRule")}


def _unchecked_rules(ctx) -> list:
    """The target column of each rule counted unchecked (the target check
    cuts the field to its column)."""
    return [e["rule"]["target_field"] for e in ctx.rules_unchecked]


# ── the calibration itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("model,written", [(0.74, 0.7), (0.96, 0.7)])
def test_a_pattern_fit_no_longer_lifts_the_cap_of_a_rule_without_a_measured_method(model, written):
    """REPRODUCTION. One source value with the pattern's shape lifted the cap,
    so the model's own 0.74 or 0.96 was written."""
    assert onboard._calibrated_rule_confidence(
        {"confidence": model}, sample_check_passed=True, fanout_k=1) == written


def test_the_cap_is_a_ceiling_not_a_fixed_value():
    """Pin: a model 0.5 stays 0.5, with or without a pattern fit."""
    for fit in (True, False):
        assert onboard._calibrated_rule_confidence(
            {"confidence": 0.5}, sample_check_passed=fit, fanout_k=1) == 0.5


def test_fan_out_divides_the_capped_number():
    """REPRODUCTION. K=2 with a pattern fit: 0.96 -> 0.7 -> 0.35 (was 0.48)."""
    assert onboard._calibrated_rule_confidence(
        {"confidence": 0.96}, sample_check_passed=True, fanout_k=2) == 0.35


def test_an_exact_rule_keeps_its_probe_derived_number():
    """Pin: no cap, no fan-out division, whatever the sample check says."""
    rule = {"method": EXACT_METHOD, "confidence": AFFINITY_CONFIDENCE}
    for fit in (True, False):
        for k in (1, 2):
            assert onboard._calibrated_rule_confidence(
                rule, sample_check_passed=fit, fanout_k=k) == AFFINITY_CONFIDENCE


def test_a_core_rule_keeps_todays_calibration():
    """Pin: a core-match rule whose sample check passed keeps its number and
    is never divided; without the sample check it is capped at 0.7."""
    rule = {"method": AFFINITY_METHOD, "confidence": AFFINITY_CONFIDENCE}
    assert onboard._calibrated_rule_confidence(rule, sample_check_passed=True, fanout_k=1) == 0.85
    assert onboard._calibrated_rule_confidence(rule, sample_check_passed=True, fanout_k=2) == 0.85
    assert onboard._calibrated_rule_confidence(rule, sample_check_passed=False, fanout_k=1) == 0.7
    assert onboard._calibrated_rule_confidence(rule, sample_check_passed=False, fanout_k=2) == 0.7


def test_a_provision_request_rule_is_still_capped():
    """Pin: the provision-request cap applies whatever the check found."""
    method = onboard.PROVISION_REQUEST_METHOD
    assert onboard._written_confidence({"confidence": 0.96}, method,
                                       sample_check_passed=True, fanout_k=1) == 0.7
    assert onboard._written_confidence({"confidence": 0.5}, method,
                                       sample_check_passed=True, fanout_k=1) == 0.5


def test_measured_means_the_two_resolve_methods():
    """The one predicate the calibration and the unchecked count share."""
    assert onboard._has_measured_method({"method": EXACT_METHOD})
    assert onboard._has_measured_method({"method": AFFINITY_METHOD})
    for method in ("", None, "onboarding-llm", onboard.PROVISION_REQUEST_METHOD, "user_confirmed"):
        assert not onboard._has_measured_method({"method": method})


# ── through the provision writer ─────────────────────────────────────────────

@pytest.mark.parametrize("model,written", [(0.74, 0.7), (0.96, 0.7), (0.5, 0.5)])
def test_a_checkpoint_rule_whose_pattern_fits_is_written_at_most_0_7(writes, model, written):
    """REPRODUCTION (0.74, 0.96): a method-less operator rule whose pattern
    fits a shift-log login was written at the model's number while provision
    counted it unchecked. Now it is written at min(model, 0.7), still counted
    unchecked, and its origin still says what the check found."""
    ctx = _run(_rule(confidence=model))
    assert ctx.rules_rejected == []
    assert _unchecked_rules(ctx) == ["closed_by"]
    params = _written(writes)["closed_by"]
    assert params["conf"] == written
    assert "fits the pattern" in params["octx"]


def test_a_fanned_out_checkpoint_rule_with_a_fit_is_divided_after_the_cap(writes):
    """REPRODUCTION. One login column feeds two target columns (K=2), both
    patterns fit: 0.96 -> 0.7 -> 0.35 each (was 0.48)."""
    _run(_rule(confidence=0.96),
         _rule(confidence=0.96, target_field="checks.signed_off_by"))
    assert {f: p["conf"] for f, p in _written(writes).items()} == {
        "closed_by": 0.35, "signed_off_by": 0.35}


def test_a_demoted_measured_claim_with_a_fit_is_capped(writes, monkeypatch):
    """REPRODUCTION. A rule sent as ``value_core_match`` whose re-measurement
    does not rebuild it loses the method; its pattern still fits a source
    value. It was written at the claim's 0.85. Without a measured method it
    is capped like any method-less rule, and counted unchecked."""
    monkeypatch.setattr(onboard, "_remeasured_rule",
                        lambda *_a, **_k: (None, "found no transform the sampled values share"))
    ctx = _run(_rule(method=AFFINITY_METHOD, confidence=AFFINITY_CONFIDENCE))
    assert _unchecked_rules(ctx) == ["closed_by"]
    params = _written(writes)["closed_by"]
    assert params["conf"] == 0.7
    assert params["method"] == ""


def test_a_provision_request_rule_with_a_fit_is_still_capped(writes):
    """Pin: a request that did not come from the checkpoint, pattern fit."""
    _run(_rule(confidence=0.96), from_checkpoint=False)
    params = _written(writes)["closed_by"]
    assert (params["method"], params["conf"]) == (onboard.PROVISION_REQUEST_METHOD, 0.7)


@pytest.mark.parametrize("model,written", [(0.8, 0.7), (0.5, 0.5)])
def test_a_direct_match_found_in_both_columns_is_written_as_before(writes, model, written):
    """Pin: a method-less direct match whose named logins are in both columns
    is value evidence, not unchecked, and its confidence is min(model, 0.7)
    exactly as before (a direct match never reported a sample pass)."""
    ctx = _run(_rule(source_pattern=LISTED, target_pattern=LISTED, confidence=model))
    assert ctx.rules_rejected == [] and ctx.rules_unchecked == []
    params = _written(writes)["closed_by"]
    assert (params["src_pat"], params["tgt_pat"], params["conf"]) == ("", "", written)
    assert "4 of the 4 values" in params["octx"]
