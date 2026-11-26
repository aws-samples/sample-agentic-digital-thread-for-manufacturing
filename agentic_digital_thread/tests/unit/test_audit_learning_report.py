# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the gate/report/CLI contract of
scripts/audit_learning.py — the exit-code matrix (default vs --strict), the
JSON report shape (every probe's verdict + counts + offending ids), and the
parser's env fallbacks. Pure — no network (repo-root namespace import, same
mechanism as test_smoke_prompts_classifiers.py)."""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.audit_learning import (  # noqa: E402
    build_parser,
    build_report,
    gate_failures,
    run_probes,
)


def _clean_probes(**overrides):
    """A fully-clean probe result set; overrides splice in one violation."""
    probes = run_probes({"rules": [], "join_paths_detail": [],
                         "authority_rules_detail": []},
                        {"mappings": []},
                        {"integrity_mode": "report-only",
                         "usage_ledger": {"rows_30d": 2, "artifacts": 1,
                                          "evidence_classes": ["tool-text"]}})
    for name, value in overrides.items():
        probes[name] = {**probes[name], **value}
    return probes


# ── Exit-code matrix ─────────────────────────────────────────────────────────

def test_clean_probes_pass_default_and_strict():
    probes = _clean_probes()
    assert gate_failures(probes, strict=False) == []
    assert gate_failures(probes, strict=True) == []


def test_default_gate_fails_on_each_structural_probe():
    structural = {
        "duplicates": {"count": 1},
        "id_shapes": {"count": 1, "legacy_ids": ["ir-alpha-14"]},
        "forbidden_pairings": {"count": 2},
        "auto_accept": {"count": 1},
    }
    for name, violation in structural.items():
        failures = gate_failures(_clean_probes(**{name: violation}),
                                 strict=False)
        assert len(failures) == 1, name


def test_unexplained_cross_site_gates_only_with_strict():
    probes = _clean_probes(cross_site={"unexplained": ["m1"], "count": 1})
    assert gate_failures(probes, strict=False) == []
    strict_failures = gate_failures(probes, strict=True)
    assert len(strict_failures) == 1
    assert "probe 5" in strict_failures[0]


def test_ledger_starvation_gates_only_with_strict():
    probes = _clean_probes(usage_ledger={"verdict": "starved", "rows_30d": 0})
    assert gate_failures(probes, strict=False) == []
    strict_failures = gate_failures(probes, strict=True)
    assert len(strict_failures) == 1
    assert "probe 7" in strict_failures[0]


def test_expected_gap_never_fails_even_strict():
    probes = _clean_probes(
        usage_ledger={"verdict": "expected-gap", "rows_30d": 0,
                      "integrity_mode": "report-only"})
    assert gate_failures(probes, strict=True) == []


def test_unavailable_health_never_fails():
    probes = _clean_probes(
        usage_ledger={"verdict": "unavailable", "rows_30d": None})
    assert gate_failures(probes, strict=True) == []


def test_trends_never_gate():
    probes = _clean_probes(trends={"pending_total": 99,
                                   "pending_quarantined": 99})
    assert gate_failures(probes, strict=True) == []


# ── Probe 8 exit-code matrix ─────────────────────────────────────────────────

def test_over_age_drift_gates_only_with_strict():
    probes = _clean_probes(
        unresolved_drift={"verdict": "over-age", "pending": 2,
                          "over_age": ["m-drift-1"], "oldest_age_days": 9.5,
                          "max_age_days": 7.0})
    assert gate_failures(probes, strict=False) == []
    strict_failures = gate_failures(probes, strict=True)
    assert len(strict_failures) == 1
    assert "probe 8" in strict_failures[0]
    assert "m-drift-1" in strict_failures[0]


def test_drift_unavailable_never_fails_even_strict():
    probes = _clean_probes(
        unresolved_drift={"verdict": "unavailable", "pending": 0,
                          "over_age": [], "oldest_age_days": None,
                          "max_age_days": 7.0})
    assert gate_failures(probes, strict=True) == []


def test_fresh_pending_drift_never_fails_even_strict():
    probes = _clean_probes(
        unresolved_drift={"verdict": "ok", "pending": 3, "over_age": [],
                          "oldest_age_days": 2.1, "max_age_days": 7.0})
    assert gate_failures(probes, strict=True) == []


def test_report_drift_verdict_tracks_strictness():
    probes = _clean_probes(
        unresolved_drift={"verdict": "over-age", "pending": 1,
                          "over_age": ["m-drift-1"], "oldest_age_days": 8.0,
                          "max_age_days": 7.0})
    lax = build_report(base_url="u", probes=probes,
                       failures=gate_failures(probes, strict=False),
                       strict=False)
    assert lax["probes"]["unresolved_drift"]["verdict"] == "warn"
    assert lax["passed"] is True
    strict = build_report(base_url="u", probes=probes,
                          failures=gate_failures(probes, strict=True),
                          strict=True)
    assert strict["probes"]["unresolved_drift"]["verdict"] == "fail"
    assert strict["passed"] is False


def test_report_drift_unavailable_verdict_passes_through():
    probes = _clean_probes()  # no schema_drift rows in the clean fixture
    report = build_report(base_url="u", probes=probes, failures=[],
                          strict=True)
    assert report["probes"]["unresolved_drift"]["verdict"] == "unavailable"
    assert report["passed"] is True


# ── Report shape ─────────────────────────────────────────────────────────────

def test_report_carries_every_probe_verdict_counts_and_offenders():
    probes = _clean_probes(
        id_shapes={"count": 1, "legacy_ids": ["ir-alpha-14"]})
    failures = gate_failures(probes, strict=False)
    report = build_report(base_url="https://example.invalid", probes=probes,
                          failures=failures, strict=False)
    assert report["passed"] is False
    assert report["strict"] is False
    assert sorted(report["probes"]) == sorted([
        "duplicates", "id_shapes", "forbidden_pairings", "auto_accept",
        "cross_site", "trends", "usage_ledger", "unresolved_drift"])
    for probe in report["probes"].values():
        assert "verdict" in probe
    assert report["probes"]["id_shapes"]["verdict"] == "fail"
    assert report["probes"]["id_shapes"]["legacy_ids"] == ["ir-alpha-14"]
    assert report["probes"]["duplicates"]["verdict"] == "pass"
    assert report["probes"]["trends"]["verdict"] == "report-only"
    assert report["probes"]["usage_ledger"]["verdict"] == "ok"
    assert "generated_at" in report


def test_report_verdicts_track_strictness_for_probe5_and_probe7():
    probes = _clean_probes(
        cross_site={"unexplained": ["m1"], "count": 1},
        usage_ledger={"verdict": "starved", "rows_30d": 0})
    lax = build_report(base_url="u", probes=probes,
                       failures=gate_failures(probes, strict=False),
                       strict=False)
    assert lax["probes"]["cross_site"]["verdict"] == "warn"
    assert lax["probes"]["usage_ledger"]["verdict"] == "warn"
    assert lax["passed"] is True
    strict = build_report(base_url="u", probes=probes,
                          failures=gate_failures(probes, strict=True),
                          strict=True)
    assert strict["probes"]["cross_site"]["verdict"] == "fail"
    assert strict["probes"]["usage_ledger"]["verdict"] == "fail"
    assert strict["passed"] is False


def test_report_is_credential_free():
    probes = _clean_probes()
    report = build_report(base_url="https://example.invalid", probes=probes,
                          failures=[], strict=True)
    text = str(report).lower()
    assert "password" not in text
    assert "cookie" not in text


# ── CLI parser env fallbacks ─────────────────────────────────────────────────

def test_parser_env_fallbacks():
    env = {"ADT_BASE_URL": "https://example.invalid",
           "ADT_PASSWORD": "placeholder-secret",
           "AUDIT_REPORT": "/tmp/custom-audit.json",  # nosec B108 - test fixture path
           "AUDIT_STRICT": "1"}
    args = build_parser(environ=env).parse_args([])
    assert args.base_url == "https://example.invalid"
    assert args.username == "ADT"
    assert args.password == "placeholder-secret"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    assert args.report == "/tmp/custom-audit.json"  # nosec B108 - test fixture path
    assert args.strict is True


def test_parser_defaults_and_flag_overrides():
    args = build_parser(environ={}).parse_args([])
    assert args.base_url == ""
    assert args.username == "ADT"
    assert args.strict is False
    assert args.report == ""
    args = build_parser(environ={}).parse_args(
        ["--base-url", "https://x.invalid", "--strict",
         "--report", "/tmp/r.json", "--username", "steward"])  # nosec B108 - test fixture path
    assert args.base_url == "https://x.invalid"
    assert args.strict is True
    assert args.report == "/tmp/r.json"  # nosec B108 - test fixture path
    assert args.username == "steward"


def test_audit_strict_env_flag_spellings():
    for value, expected in (("true", True), ("YES", True), ("on", True),
                            ("0", False), ("", False), ("off", False)):
        args = build_parser(environ={"AUDIT_STRICT": value}).parse_args([])
        assert args.strict is expected, value


def test_guidance_budget_annotation_never_changes_gate_result():
    probes = _clean_probes()
    health = {"guidance_budget": {
        "systems": 3, "over_budget": True, "over_by": 1884,
        "over_budget_systems": [{"system_id": "sys-a", "over_by": 1884}],
    }}

    for strict in (False, True):
        failures = gate_failures(probes, strict=strict)
        report = build_report(base_url="u", probes=probes, failures=failures,
                              strict=strict, health=health)
        assert failures == []
        assert report["passed"] is True
        assert report["annotations"]["guidance_budget"] == {
            "available": True, "systems": 3, "over_budget": True,
            "over_by": 1884,
            "over_budget_systems": [
                {"system_id": "sys-a", "over_by": 1884}],
        }


def test_guidance_budget_annotation_is_honestly_unavailable_on_older_env():
    report = build_report(base_url="u", probes=_clean_probes(), failures=[],
                          strict=True, health={})
    assert report["annotations"]["guidance_budget"] == {
        "available": False, "over_budget": False, "over_by": 0,
        "systems": None, "over_budget_systems": [],
    }


def test_authority_evidence_annotation_never_changes_gate_in_either_mode():
    probes = _clean_probes()
    orphan = {
        "rule_id": "ar-system-a-state", "system_id": "system-a",
        "concept": "machine-state",
        "mapped_systems": [
            {"system_id": "system-b", "system_name": "System B"}],
    }
    health = {"graph_integrity": {"authority_rule_evidence": {
        "available": True, "active_rules": 8, "orphan_count": 1,
        "evidence_orphans": [orphan],
    }}}

    for strict in (False, True):
        failures = gate_failures(probes, strict=strict)
        report = build_report(base_url="u", probes=probes, failures=failures,
                              strict=strict, health=health)
        assert failures == []
        assert report["passed"] is True
        assert report["annotations"]["authority_rule_evidence"] == {
            "available": True, "active_rules": 8, "orphan_count": 1,
            "evidence_orphans": [orphan],
        }


def test_authority_evidence_annotation_unavailable_on_older_environment():
    report = build_report(base_url="u", probes=_clean_probes(), failures=[],
                          strict=True, health={})
    assert report["annotations"]["authority_rule_evidence"] == {
        "available": False, "active_rules": None, "orphan_count": None,
        "evidence_orphans": [],
    }


def test_unavailable_authority_evidence_never_carries_an_orphan_list():
    """An admitted-unknown read must not still name orphans.

    ``available: False`` already nulls the counts; shipping the raw list beside
    them would let a stale payload state WHICH rules are orphans while the read
    that would have confirmed it is known to have failed.
    """
    health = {"graph_integrity": {"authority_rule_evidence": {
        "available": False, "active_rules": 8, "orphan_count": 1,
        "evidence_orphans": [{"rule_id": "ar-stale", "system_id": "system-a",
                              "concept": "machine-state",
                              "mapped_systems": []}],
    }}}
    report = build_report(base_url="u", probes=_clean_probes(), failures=[],
                          strict=True, health=health)
    assert report["annotations"]["authority_rule_evidence"] == {
        "available": False, "active_rules": None, "orphan_count": None,
        "evidence_orphans": [],
    }
    assert report["passed"] is True
