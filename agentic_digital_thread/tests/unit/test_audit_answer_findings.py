# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the deterministic post-LLM validation in
scripts/audit_answer.py.

Pinned invariants:
  * taxonomy is EXACTLY the pinned 7 values — anything else (e.g.
    "performance") is discarded and counted in discarded_invalid;
  * referential integrity — every evidence_ref must resolve to an id present
    in the report+evidence bundle; a finding citing an absent id is
    discarded+counted, one citing a present id survives;
  * suggested_mapping keys are FIELD-COMPATIBLE with
    agents/self_learning.py::log_proposed_mapping kwargs, pinned by
    signature INTROSPECTION (no drift possible).

Validation is pure/computed — no network, no LLM (--disable-socket lane).
scripts/ lives at the repo root, so we add the repo root to sys.path and
import `scripts.audit_answer` as a namespace package (same mechanism as
test_audit_learning_probes.py).
"""
import inspect
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import audit_answer  # noqa: E402

from agents.self_learning import log_proposed_mapping  # noqa: E402

_KNOWN_IDS = {"trace-step-7", "eval-r1-1", "ir-mes-0123456789abcdef"}


def _finding(**overrides) -> dict:
    base = {
        "taxonomy": "data_gap",
        "question_id": "q-defect-chain",
        "claim_excerpt": "the answer quantified scrap without data",
        "evidence_refs": ["trace-step-7"],
        "rationale": "no system row backs the quantity",
    }
    base.update(overrides)
    return base


# ── taxonomy pin ─────────────────────────────────────────────────────────────

def test_taxonomy_is_pinned_to_exactly_seven_values():
    assert audit_answer.TAXONOMY == (
        "data_gap", "mapping_gap", "schema_drift", "prompt_gap",
        "code_bug", "ux_gap", "golden_candidate")


def test_all_seven_pinned_taxonomy_values_are_accepted():
    findings = [_finding(taxonomy=value) for value in audit_answer.TAXONOMY]
    valid, discarded = audit_answer.validate_findings(findings, _KNOWN_IDS)
    assert len(valid) == len(audit_answer.TAXONOMY)
    assert discarded == 0
    assert [f["taxonomy"] for f in valid] == list(audit_answer.TAXONOMY)


def test_performance_taxonomy_is_discarded_and_counted():
    valid, discarded = audit_answer.validate_findings(
        [_finding(taxonomy="performance")], _KNOWN_IDS)
    assert valid == []
    assert discarded == 1


def test_missing_taxonomy_is_discarded():
    finding = _finding()
    del finding["taxonomy"]
    valid, discarded = audit_answer.validate_findings([finding], _KNOWN_IDS)
    assert valid == []
    assert discarded == 1


# ── referential integrity ────────────────────────────────────────────────────

def test_finding_citing_absent_trace_id_is_discarded_and_counted():
    valid, discarded = audit_answer.validate_findings(
        [_finding(evidence_refs=["ghost-trace-id"])], _KNOWN_IDS)
    assert valid == []
    assert discarded == 1


def test_finding_citing_present_id_survives():
    valid, discarded = audit_answer.validate_findings(
        [_finding(evidence_refs=["trace-step-7", "eval-r1-1"])], _KNOWN_IDS)
    assert discarded == 0
    assert len(valid) == 1
    assert valid[0]["evidence_refs"] == ["trace-step-7", "eval-r1-1"]


def test_mixed_present_and_absent_refs_discard_the_whole_finding():
    valid, discarded = audit_answer.validate_findings(
        [_finding(evidence_refs=["trace-step-7", "ghost-id"])], _KNOWN_IDS)
    assert valid == []
    assert discarded == 1


def test_empty_evidence_refs_is_discarded():
    valid, discarded = audit_answer.validate_findings(
        [_finding(evidence_refs=[])], _KNOWN_IDS)
    assert valid == []
    assert discarded == 1


def test_valid_and_invalid_findings_split_correctly():
    findings = [
        _finding(),                                   # survives
        _finding(taxonomy="performance"),             # bad taxonomy
        _finding(evidence_refs=["ghost-id"]),         # dangling ref
        _finding(taxonomy="ux_gap"),                  # survives
    ]
    valid, discarded = audit_answer.validate_findings(findings, _KNOWN_IDS)
    assert len(valid) == 2
    assert discarded == 2
    assert [f["taxonomy"] for f in valid] == ["data_gap", "ux_gap"]


def test_collect_known_ids_walks_nested_report_and_evidence():
    report = {"run_id": "r9", "questions": [
        {"id": "q-1", "session_id": "eval-r9-1"}]}
    evidence = [{"traces": [{"steps": [
        {"id": "trace-step-3"},
        {"rule_id": "ir-erp-fedcba9876543210"},
    ]}]}]
    ids = audit_answer.collect_known_ids(report, evidence)
    assert {"r9", "q-1", "eval-r9-1", "trace-step-3",
            "ir-erp-fedcba9876543210"} <= ids


def test_non_dict_findings_are_discarded():
    valid, discarded = audit_answer.validate_findings(
        ["not a dict", 42, None], _KNOWN_IDS)
    assert valid == []
    assert discarded == 3


# ── suggested_mapping ↔ log_proposed_mapping field compatibility ─────────────

def test_suggested_mapping_keys_are_log_proposed_mapping_kwargs():
    """Pinned by INTROSPECTION: every schema key must be an actual kwarg of
    agents/self_learning.py::log_proposed_mapping — no drift possible."""
    params = set(inspect.signature(log_proposed_mapping).parameters)
    missing = set(audit_answer.SUGGESTED_MAPPING_KEYS) - params
    assert not missing, (
        f"suggested_mapping keys drifted from log_proposed_mapping "
        f"kwargs: {sorted(missing)}")


def test_full_suggested_mapping_fixture_survives_validation():
    mapping = {key: f"value-for-{key}"
               for key in audit_answer.SUGGESTED_MAPPING_KEYS}
    valid, discarded = audit_answer.validate_findings(
        [_finding(suggested_mapping=mapping)], _KNOWN_IDS)
    assert discarded == 0
    assert len(valid) == 1
    assert valid[0]["suggested_mapping"] == mapping


def test_suggested_mapping_with_foreign_key_is_discarded():
    mapping = {"from_system": "erp", "to_system": "mes",
               "priority": "high"}  # 'priority' is NOT a schema key
    valid, discarded = audit_answer.validate_findings(
        [_finding(suggested_mapping=mapping)], _KNOWN_IDS)
    assert valid == []
    assert discarded == 1


def test_suggested_mapping_must_be_a_non_empty_dict_of_strings():
    for bad in ({}, "erp->mes", {"from_system": 42}):
        valid, discarded = audit_answer.validate_findings(
            [_finding(suggested_mapping=bad)], _KNOWN_IDS)
        assert valid == [], f"suggested_mapping={bad!r} must discard"
        assert discarded == 1


def test_finding_without_suggested_mapping_omits_the_key():
    valid, _ = audit_answer.validate_findings([_finding()], _KNOWN_IDS)
    assert "suggested_mapping" not in valid[0]


# ── normalized finding shape ─────────────────────────────────────────────────

def test_surviving_findings_get_deterministic_sequential_ids():
    valid, _ = audit_answer.validate_findings(
        [_finding(), _finding(taxonomy="prompt_gap")], _KNOWN_IDS)
    assert [f["id"] for f in valid] == ["finding-001", "finding-002"]


def test_model_supplied_finding_id_is_preserved():
    valid, _ = audit_answer.validate_findings(
        [_finding(id="my-finding")], _KNOWN_IDS)
    assert valid[0]["id"] == "my-finding"


def test_claim_excerpt_is_bounded():
    valid, _ = audit_answer.validate_findings(
        [_finding(claim_excerpt="x" * 5000)], _KNOWN_IDS)
    assert len(valid[0]["claim_excerpt"]) == audit_answer.CLAIM_EXCERPT_CHARS
