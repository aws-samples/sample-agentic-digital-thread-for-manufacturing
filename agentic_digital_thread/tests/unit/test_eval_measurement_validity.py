# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Measurement-validity contracts for repeated golden-eval campaigns."""
from pathlib import Path
import sys
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402


def _delivered(verdict: str, *, rules: int = 1, planned: bool = True) -> dict:
    return {
        "id": "g-1",
        "session_id": f"sid-{verdict}-{rules}",
        "verdict": verdict,
        "failures": [] if verdict == "pass" else ["content mismatch"],
        "systems_queried": 1,
        "neptune_queries": 1,
        "elapsed_seconds": 1.0,
        "answer_excerpt": "answer",
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": {
            "rules_applied_count": rules,
            "planned_with_present": planned,
        },
    }


def _transport() -> dict:
    return {
        "id": "g-1",
        "session_id": "sid-transport",
        "verdict": "fail",
        "failures": ["incomplete stream — transport, not content"],
        "transport_failure": True,
        "systems_queried": 0,
        "neptune_queries": 0,
        "elapsed_seconds": None,
        "answer_excerpt": "",
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": None,
    }


def test_pass_pass_transport_keeps_content_pass_but_invalidates_measurement():
    row = eval_answers._fold_repeats([
        _delivered("pass"), _delivered("pass"), _transport()])

    assert row["verdict"] == "pass"
    assert row["content_verdict"] == "pass"
    assert row["majority_verdict"] == "pass"
    assert row["measurement_valid"] is False
    assert row["delivered_count"] == 2
    assert row["transport_failure_count"] == 1
    assert "transport" in row["invalidity_reason"]
    assert len(row["invalidity_reason"]) <= 160
    assert [repeat["content_verdict"] for repeat in row["repeats"]] == [
        "pass", "pass", None]
    assert [repeat["transport_failure"] for repeat in row["repeats"]] == [
        False, False, True]
    assert eval_answers.suite_exit_code([row]) == 1


def test_all_delivered_pass_fail_pass_is_valid_content_pass():
    row = eval_answers._fold_repeats([
        _delivered("pass"), _delivered("fail"), _delivered("pass")])

    assert row["verdict"] == "pass"
    assert row["content_verdict"] == "pass"
    assert row["majority_verdict"] == "pass"
    assert row["measurement_valid"] is True
    assert row["delivered_count"] == 3
    assert row["transport_failure_count"] == 0
    assert row["invalidity_reason"] is None
    assert eval_answers.suite_exit_code([row]) == 0


def test_state_instability_invalidates_campaign_without_changing_content():
    row = eval_answers._fold_repeats([
        _delivered("pass", rules=1),
        _delivered("fail", rules=2),
        _delivered("pass", rules=1),
    ])

    assert row["content_verdict"] == "pass"
    assert row["state_instability"] is True
    assert row["measurement_valid"] is False
    assert "state_instability" in row["invalidity_reason"]
    assert eval_answers.suite_exit_code([row]) == 1


def test_ab_invalid_vs_valid_is_non_comparable_not_content_regression():
    valid = eval_answers._fold_repeats([
        _delivered("pass"), _delivered("pass"), _delivered("pass")])
    invalid = eval_answers._fold_repeats([
        _delivered("fail"), _delivered("fail"), _transport()])
    previous = {"golden_set_sha": "same", "questions": [valid]}

    diff = eval_answers.ab_diff(
        previous, [invalid], variant="invalid-arm", golden_set_sha="same")

    assert diff["comparable"] is False
    entry = diff["questions"][0]
    assert entry["comparable"] is False
    assert entry["transition"] == "non-comparable"
    assert entry["previous_verdict"] == "pass"
    assert entry["verdict"] == "fail"
    assert diff["suite"]["regressions"] == 0
    assert diff["suite"]["non_comparable"] == 1


def test_campaign_report_separates_content_transport_and_availability():
    valid = eval_answers._fold_repeats([
        _delivered("pass"), _delivered("fail"), _delivered("pass"),
        _delivered("pass"), _delivered("fail")])
    invalid = eval_answers._fold_repeats([
        _delivered("pass"), _delivered("pass"), _transport(),
        _delivered("pass"), _delivered("fail")])

    report = eval_answers.build_eval_report(
        base_url="u",
        namespace=SimpleNamespace(run_id="rid", prefix="eval-rid"),
        golden_set="g",
        strict=False,
        rows=[valid, invalid],
        repeats=5,
    )

    assert report["suite"]["pass"] == 2
    assert report["suite"]["fail"] == 0
    assert report["suite"]["content_pass"] == 2
    assert report["suite"]["content_fail"] == 0
    assert report["suite"]["content_unavailable"] == 0
    assert report["suite"]["measurement_valid"] is False
    assert report["suite"]["delivered_count"] == 9
    assert report["suite"]["transport_failure_count"] == 1
    assert report["suite"]["invalid_question_count"] == 1


def test_campaign_majority_mode_requires_one_or_at_least_five_repeats(capsys):
    args = eval_answers.build_parser({}).parse_args([
        "--base-url", "https://example.invalid", "--password", "x",
        "--repeats", "3",
    ])

    assert eval_answers._prepare_run(args) is None
    assert "1 or >= 5" in capsys.readouterr().out
