# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for eval_answers report shape, exit codes, and A/B diff.

Same repo-root-script import mechanism as test_smoke_prompts_classifiers.py.
The A/B diff is INFORMATIONAL by contract (ADR-0053 D-E2) — it may never
change the DEFAULT exit code (run-to-run metric deltas flake; the gate is the
golden expectations). --strict-regression gates verdict pass→fail transitions
ONLY; metric drift never gates.
"""
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval_answers import (  # noqa: E402
    _metrics_block,
    _scored_row,
    _skipped_row,
    ab_diff,
    build_eval_report,
    build_parser,
    final_exit_code,
    golden_file_sha,
    main,
    suite_exit_code,
)
from scripts.smoke_prompts import create_session_namespace  # noqa: E402

# Pinned C2 metrics-block key set (contract — copy, never improvise).
# planned_with_authority_rules is the T1.6 additive fired-vs-loaded key.
_C2_METRIC_KEYS = {
    "total_tool_calls", "neptune_queries", "system_queries",
    "graph_coverage_pct", "wasted_calls", "deterministic_pct",
    "grounded_steps", "rules_applied_count", "new_proposed_mappings",
    "input_tokens", "output_tokens", "total_tokens", "cost_latency_ms",
    "planned_with_present", "planned_with_authority_rules",
    # computed from the stream's tool_result summaries (not the summary frame)
    "identifier_miss_calls",
}
# The A/B diff contract stays pinned to the 13 numeric keys (W2 T5):
# planned_with_authority_rules is deliberately NOT diffed (T1.6), and neither
# is identifier_miss_calls (read per arm from the metrics block).
_NUMERIC_C2_KEYS = sorted(
    _C2_METRIC_KEYS - {"planned_with_present", "planned_with_authority_rules",
                       "identifier_miss_calls"})

_BASE_SUMMARY = {
    "total_tool_calls": 7, "neptune_queries": 2, "system_queries": 5,
    "graph_coverage_pct": 29, "wasted_calls": 0,
    "rules_applied": [{"id": "r1", "label": "unit rule"}],
    "new_proposed_mappings": 0,
}


def _metrics(**overrides):
    block = _metrics_block(dict(_BASE_SUMMARY))
    block.update(overrides)
    return block


def _row(qid, verdict, elapsed=10.0, failures=None, metrics=None):
    return {
        "id": qid, "session_id": f"eval-run-{qid}", "verdict": verdict,
        "failures": failures or [], "systems_queried": 2, "neptune_queries": 1,
        "elapsed_seconds": elapsed, "answer_excerpt": "excerpt",
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": metrics,
    }


def test_metrics_block_nulls_never_zeros():
    # Absent optional inputs are null — a zero is a measurement, null is
    # "not measured" (honest-data rule).
    block = _metrics_block(dict(_BASE_SUMMARY))
    # identifier_miss_calls is added by the row scorer from the stream, not here.
    assert set(block) == _C2_METRIC_KEYS - {"identifier_miss_calls"}
    for absent in ("deterministic_pct", "grounded_steps", "input_tokens",
                   "output_tokens", "total_tokens", "cost_latency_ms"):
        assert block[absent] is None, absent
    assert block["planned_with_present"] is False
    # Measured values pass through — including honest zeros.
    assert block["total_tool_calls"] == 7
    assert block["wasted_calls"] == 0
    assert block["new_proposed_mappings"] == 0
    assert block["rules_applied_count"] == 1

    # Optional additive inputs present -> populated (cost latency optional).
    rich = _metrics_block({**_BASE_SUMMARY, "deterministic_pct": 80,
                           "grounded_steps": 4, "planned_with": "rules",
                           "cost": {"input_tokens": 900, "output_tokens": 100,
                                    "total_tokens": 1000,
                                    "scope": "orchestrator-model-only"}})
    assert rich["total_tokens"] == 1000
    assert rich["cost_latency_ms"] is None  # optional cost key absent
    assert rich["planned_with_present"] is True
    assert rich["deterministic_pct"] == 80

    # No summary at all -> the whole block is None (matches skipped rows).
    assert _metrics_block(None) is None
    assert _metrics_block({}) is None


def test_metrics_block_authority_rules_null_safe():
    """T1.6: planned_with.authority_rules pickup — null-safe on every legacy
    shape; honest values (including 0) pass through untouched."""
    # Legacy report shapes predating the key -> null, never a fabricated 0.
    assert _metrics_block(dict(_BASE_SUMMARY))[
        "planned_with_authority_rules"] is None                # no planned_with
    assert _metrics_block({**_BASE_SUMMARY, "planned_with": {
        "systems": 2, "value_mappings": 12}})[
        "planned_with_authority_rules"] is None                # pre-T1.6 payload
    assert _metrics_block({**_BASE_SUMMARY, "planned_with": "rules"})[
        "planned_with_authority_rules"] is None                # hostile non-dict
    # Measured values pass through — 0 is an honest measurement.
    assert _metrics_block({**_BASE_SUMMARY, "planned_with": {
        "systems": 2, "authority_rules": 2}})[
        "planned_with_authority_rules"] == 2
    assert _metrics_block({**_BASE_SUMMARY, "planned_with": {
        "systems": 2, "authority_rules": 0}})[
        "planned_with_authority_rules"] == 0


def test_row_shape_backwards_compatible():
    # Every pre-existing top-level row key stays byte-compatible (C2).
    question = {"id": "g-a", "text": "unit question?",
                "expect": {"systems_min": 1}}
    result = {"summary": dict(_BASE_SUMMARY), "answer": "fine", "done": True,
              "errors": [], "elapsed_seconds": 3.2, "tool_summaries": []}
    row = _scored_row(question, "sid-1", result, strict=False)
    legacy_keys = ("id", "session_id", "verdict", "failures",
                   "systems_queried", "neptune_queries", "elapsed_seconds",
                   "answer_excerpt")
    assert {key: row[key] for key in legacy_keys} == {
        "id": "g-a", "session_id": "sid-1", "verdict": "pass",
        "failures": [], "systems_queried": 5, "neptune_queries": 2,
        "elapsed_seconds": 3.2, "answer_excerpt": "fine",
    }
    # Additive blocks: metrics on scored rows; null on skipped rows.
    assert row["metrics"]["system_queries"] == 5
    assert set(row["metrics"]) == _C2_METRIC_KEYS
    skipped = _skipped_row({"id": "g-s"}, "requires_seed skipped")
    assert skipped["metrics"] is None
    assert skipped["honesty"] == {"absence_markers_matched": [],
                                  "fabrication_flags": []}


def test_variant_and_golden_sha_stamped(tmp_path):
    # golden_file_sha = sha256[:12] of the file BYTES; None when unreadable.
    golden = tmp_path / "goldens.json"
    golden.write_bytes(b'{"version": 1}')
    expected = hashlib.sha256(b'{"version": 1}').hexdigest()[:12]
    assert golden_file_sha(str(golden)) == expected
    assert golden_file_sha(str(tmp_path / "missing.json")) is None

    namespace = create_session_namespace(prefix="eval")
    rows = [_row("g-a", "pass")]
    report = build_eval_report(
        base_url="u", namespace=namespace, golden_set=str(golden),
        strict=False, rows=rows, variant="baseline", golden_set_sha=expected)
    assert report["variant"] == "baseline"
    assert report["golden_set_sha"] == expected
    # Empty variant -> null (not "").
    unstamped = build_eval_report(base_url="u", namespace=namespace,
                                  golden_set="g", strict=False, rows=rows)
    assert unstamped["variant"] is None
    assert unstamped["golden_set_sha"] is None

    # CLI/env plumbing: --variant / EVAL_VARIANT, --strict-regression /
    # EVAL_STRICT_REGRESSION.
    env_args = build_parser({"EVAL_VARIANT": "baseline",
                             "EVAL_STRICT_REGRESSION": "1"}).parse_args([])
    assert env_args.variant == "baseline"
    assert env_args.strict_regression is True
    cli_args = build_parser({}).parse_args(
        ["--variant", "plus-business-context", "--strict-regression"])
    assert cli_args.variant == "plus-business-context"
    assert cli_args.strict_regression is True
    defaults = build_parser({}).parse_args([])
    assert defaults.variant == "" and defaults.strict_regression is False


def test_ab_diff_deltas_and_pct():
    previous = {"variant": "baseline", "golden_set_sha": "abc123abc123",
                "questions": [_row("g-a", "pass", 15.0,
                                   metrics=_metrics(total_tool_calls=9))]}
    rows = [_row("g-a", "pass", 12.5, metrics=_metrics(total_tool_calls=7))]
    diff = ab_diff(previous, rows, variant="plus-business-context",
                   golden_set_sha="abc123abc123")
    assert diff["informational"] is True
    assert diff["comparable"] is True
    assert diff["variant"] == "plus-business-context"
    assert diff["previous_variant"] == "baseline"
    entry = diff["questions"][0]
    assert entry["status"] == "compared"
    deltas = entry["deltas"]
    assert deltas["total_tool_calls"] == -2
    assert deltas["total_tool_calls_pct"] == -22.2
    assert deltas["elapsed_seconds"] == -2.5
    # Unmeasured on both sides -> null delta and NO _pct key.
    assert deltas["total_tokens"] is None
    assert "total_tokens_pct" not in deltas
    # One entry per numeric C2 metric + elapsed_seconds.
    for key in _NUMERIC_C2_KEYS + ["elapsed_seconds"]:
        assert key in deltas, key
    # Suite metric totals over both-measured ids only.
    totals = diff["suite"]["metric_totals"]
    assert totals["total_tool_calls"] == {"previous": 9, "current": 7,
                                          "delta_pct": -22.2}
    assert totals["total_tokens"] == {"previous": None, "current": None,
                                      "delta_pct": None}
    assert totals["elapsed_seconds"] == {"previous": 15.0, "current": 12.5,
                                         "delta_pct": -16.7}


def test_ab_diff_accepts_legacy_report_without_authority_key():
    """T1.6 back-compat: a previous report written BEFORE the additive
    planned_with_authority_rules key existed must still parse and diff —
    the key is not part of the pinned 13-key diff contract."""
    legacy_metrics = _metrics()
    legacy_metrics.pop("planned_with_authority_rules")
    previous = {"variant": "pre-t16", "golden_set_sha": "abc123abc123",
                "questions": [_row("g-a", "pass", 15.0,
                                   metrics=legacy_metrics)]}
    rows = [_row("g-a", "pass", 12.5, metrics=_metrics())]
    diff = ab_diff(previous, rows, variant="post-t16",
                   golden_set_sha="abc123abc123")
    assert diff["comparable"] is True
    entry = diff["questions"][0]
    assert entry["status"] == "compared"
    assert "planned_with_authority_rules" not in entry["deltas"]


def test_ab_diff_regression_and_improvement_transitions():
    sha = "feedfeedfeed"
    previous = {"golden_set_sha": sha, "questions": [
        _row("g-a", "pass", metrics=_metrics()),
        _row("g-b", "fail", metrics=_metrics()),
        _row("g-c", "pass", metrics=_metrics()),
        _row("g-gone", "pass", metrics=_metrics())]}
    rows = [_row("g-a", "fail", metrics=_metrics()),
            _row("g-b", "pass", metrics=_metrics()),
            _row("g-c", "pass", metrics=_metrics()),
            _row("g-new", "pass", metrics=_metrics())]
    diff = ab_diff(previous, rows, variant=None, golden_set_sha=sha)
    by_id = {entry["id"]: entry for entry in diff["questions"]}
    assert by_id["g-a"]["transition"] == "regression"
    assert by_id["g-b"]["transition"] == "improvement"
    assert by_id["g-c"]["transition"] == "stable"
    assert by_id["g-new"]["status"] == "new"
    assert by_id["g-gone"]["status"] == "missing"
    assert by_id["g-gone"]["previous_verdict"] == "pass"
    suite = diff["suite"]
    assert suite["pass_previous"] == 3 and suite["pass_current"] == 3
    assert suite["regressions"] == 1 and suite["improvements"] == 1


def test_ab_diff_old_report_without_sha_suppresses_comparison_no_crash():
    previous = {"questions": [
        {"id": "g-a", "verdict": "pass", "elapsed_seconds": 10.0}]}
    rows = [_row("g-a", "pass", 12.0, metrics=_metrics())]
    diff = ab_diff(previous, rows, variant=None, golden_set_sha="abc")
    assert diff["questions"] == [{
        "id": "g-a", "status": "non-comparable",
        "reason": "golden-set-sha-missing"}]
    assert diff["comparable"] is False
    assert diff["previous_variant"] is None
    assert diff["suite"] == {}


def test_ab_diff_golden_sha_mismatch_suppresses_all_comparison_products():
    previous = {"golden_set_sha": "aaaaaaaaaaaa", "questions": [
        _row("g-a", "pass", 10.0, metrics=_metrics())]}
    rows = [_row("g-a", "fail", 11.0, metrics=_metrics())]
    diff = ab_diff(previous, rows, variant="v2",
                   golden_set_sha="bbbbbbbbbbbb")
    assert diff["comparable"] is False
    entry = diff["questions"][0]
    assert entry == {"id": "g-a", "status": "non-comparable",
                     "reason": "golden-set-sha-mismatch"}
    assert diff["suite"] == {}
    assert final_exit_code(rows, {"trend": diff}, strict_regression=True) == 1
    # Matching shas restore normal comparison products.
    match = ab_diff(previous, rows, variant="v2",
                    golden_set_sha="aaaaaaaaaaaa")
    assert match["comparable"] is True
    assert match["questions"][0]["transition"] == "regression"


def _drift_case(key, prev_value, cur_value):
    """Single-question diff for one drift metric; returns the flags list."""
    if key == "elapsed_seconds":
        prev_row = _row("g-x", "pass", prev_value, metrics=_metrics())
        cur_row = _row("g-x", "pass", cur_value, metrics=_metrics())
    else:
        prev_row = _row("g-x", "pass", metrics=_metrics(**{key: prev_value}))
        cur_row = _row("g-x", "pass", metrics=_metrics(**{key: cur_value}))
    diff = ab_diff({"golden_set_sha": "s", "questions": [prev_row]},
                   [cur_row], variant=None, golden_set_sha="s")
    return diff["questions"][0]["flags"]


def test_metric_drift_flags_at_pinned_thresholds():
    # Tool calls: +25% AND +2 absolute (inclusive at the boundary).
    assert _drift_case("total_tool_calls", 8, 10) == [
        "metric-drift: total_tool_calls +25%"]
    assert _drift_case("total_tool_calls", 8, 9) == []      # +2 pct short
    assert _drift_case("total_tool_calls", 4, 5) == []      # +25% abs short
    # Total tokens: +25% (no absolute component).
    assert _drift_case("total_tokens", 100, 125) == [
        "metric-drift: total_tokens +25%"]
    assert _drift_case("total_tokens", 100, 124) == []
    # Latency: +50% AND +10s.
    assert _drift_case("elapsed_seconds", 20.0, 30.0) == [
        "metric-drift: elapsed_seconds +50%"]
    assert _drift_case("elapsed_seconds", 20.0, 29.0) == []  # +45% pct short
    assert _drift_case("elapsed_seconds", 10.0, 15.5) == []  # +5.5s abs short
    # Decreases never flag.
    assert _drift_case("total_tool_calls", 10, 7) == []


def test_metric_drift_flags_from_zero_on_absolute_threshold():
    """Pct is undefined from a zero previous value, so the absolute
    threshold ALONE flags — a 0→8 tool-call jump must not be invisible."""
    assert _drift_case("total_tool_calls", 0, 8) == [
        "metric-drift: total_tool_calls +8 (from 0)"]
    assert _drift_case("total_tool_calls", 0, 2) == [
        "metric-drift: total_tool_calls +2 (from 0)"]   # inclusive boundary
    assert _drift_case("total_tool_calls", 0, 1) == []  # below +2 absolute
    assert _drift_case("elapsed_seconds", 0, 15.0) == [
        "metric-drift: elapsed_seconds +15 (from 0)"]
    assert _drift_case("elapsed_seconds", 0, 9.0) == []  # below +10s absolute
    # Pct-only metrics (total_tokens) CANNOT flag from zero — no absolute
    # threshold to fall back on (documented in ADR-0054 D3).
    assert _drift_case("total_tokens", 0, 5000) == []
    # Zero→zero and unmeasured sides never flag.
    assert _drift_case("total_tool_calls", 0, 0) == []


def test_from_zero_drift_never_touches_exit_codes():
    """From-zero drift is informational ONLY — even under --strict-regression
    a drift-flagged, all-passing diff exits 0."""
    sha = "cafecafecafe"
    previous = {"golden_set_sha": sha, "questions": [
        _row("g-a", "pass", metrics=_metrics(total_tool_calls=0))]}
    rows = [_row("g-a", "pass", metrics=_metrics(total_tool_calls=8))]
    trend = ab_diff(previous, rows, variant=None, golden_set_sha=sha)
    assert trend["questions"][0]["flags"] == [
        "metric-drift: total_tool_calls +8 (from 0)"]
    assert trend["suite"]["regressions"] == 0
    report = {"trend": trend}
    assert final_exit_code(rows, report, strict_regression=False) == 0
    assert final_exit_code(rows, report, strict_regression=True) == 0


def test_strict_regression_exit_code():
    sha = "cafecafecafe"
    previous = {"golden_set_sha": sha, "questions": [
        _row("g-a", "pass", metrics=_metrics()),
        _row("g-b", "pass", metrics=_metrics())]}
    rows = [_row("g-a", "pass", metrics=_metrics()),
            _row("g-b", "skipped", None)]
    # pass->skipped is NOT a regression (verdict pass->fail ONLY).
    report = {"trend": ab_diff(previous, rows, variant=None,
                               golden_set_sha=sha)}
    assert report["trend"]["suite"]["regressions"] == 0
    assert final_exit_code(rows, report, strict_regression=True) == 0
    # Drift-only diffs never gate, even under the flag.
    drift_only = {"trend": {"suite": {"regressions": 0, "improvements": 1}}}
    assert final_exit_code(rows, drift_only, strict_regression=True) == 0
    # regressions >= 1 gates ONLY with the flag.
    regressed = {"trend": {"suite": {"regressions": 1}}}
    assert final_exit_code(rows, regressed, strict_regression=True) == 1
    assert final_exit_code(rows, regressed, strict_regression=False) == 0
    # Failing rows dominate regardless of the flag.
    failing = [_row("g-a", "fail")]
    assert final_exit_code(failing, {"trend": {"suite": {"regressions": 0}}},
                           strict_regression=True) == 1
    # No trend block at all: the flag is a no-op.
    assert final_exit_code(rows, {}, strict_regression=True) == 0


def test_default_exit_contract_unchanged():
    # The DEFAULT exit contract is byte-identical to suite_exit_code: a diff
    # full of regressions cannot flip a passing suite without the flag.
    previous = {"golden_set_sha": "s", "questions": [
        _row("g-a", "pass", metrics=_metrics()),
        _row("g-b", "pass", metrics=_metrics())]}
    rows = [_row("g-a", "pass", metrics=_metrics()),
            _row("g-b", "pass", metrics=_metrics(total_tool_calls=99))]
    report = build_eval_report(
        base_url="u", namespace=create_session_namespace(prefix="eval"),
        golden_set="g", strict=False, rows=rows, golden_set_sha="s",
        previous={"golden_set_sha": "s", "questions": [
            _row("g-a", "fail", metrics=_metrics()),
            _row("g-b", "fail", metrics=_metrics())]})
    assert report["trend"]["suite"]["improvements"] == 2
    assert suite_exit_code(rows) == 0
    assert final_exit_code(rows, report, strict_regression=False) == 0
    # And with regressions in the trend, default still passes.
    regressed_report = {"trend": ab_diff(
        previous, [_row("g-a", "fail", metrics=_metrics()),
                   _row("g-b", "pass", metrics=_metrics())],
        variant=None, golden_set_sha="s")}
    assert regressed_report["trend"]["suite"]["regressions"] == 1
    current_rows = [_row("g-a", "pass"), _row("g-b", "pass")]
    assert final_exit_code(current_rows, regressed_report,
                           strict_regression=False) == 0


def test_report_shape_and_exit_codes(monkeypatch, tmp_path):
    namespace = create_session_namespace(prefix="eval")
    rows = [_row("g-a", "pass", 10.0, metrics=_metrics()),
            _row("g-b", "fail", 30.0, ["system_queries 1 < 2"],
                 metrics=_metrics()),
            _row("g-c", "skipped", None)]
    report = build_eval_report(
        base_url="https://example.invalid", namespace=namespace,
        golden_set="/path/goldens.json", strict=False, rows=rows)

    # Pinned per-question shape (legacy keys + additive honesty/metrics).
    for question in report["questions"]:
        for key in ("id", "session_id", "verdict", "failures",
                    "systems_queried", "neptune_queries", "elapsed_seconds",
                    "answer_excerpt", "honesty", "metrics"):
            assert key in question, key
    # Suite totals: pass/fail/skipped/total + latency stats.
    suite = report["suite"]
    assert suite == {"pass": 1, "fail": 1, "skipped": 1, "total": 3,
                     "avg_latency_seconds": 20.0, "max_latency_seconds": 30.0}
    # No trend block without --previous.
    assert "trend" not in report
    # Credential-free: no password anywhere in the report.
    assert "password" not in json.dumps(report).lower()

    # Exit contract: 0 = all ran questions pass (skips never gate); 1 = >=1 fail.
    assert suite_exit_code([_row("g-a", "pass"), _row("g-c", "skipped", None)]) == 0
    assert suite_exit_code(rows) == 1

    # Exit 2 = usage error (no base-url/password).
    for var in ("ADT_BASE_URL", "ADT_PASSWORD", "EVAL_GOLDEN_SET",
                "EVAL_STRICT", "EVAL_REPORT", "EVAL_VARIANT",
                "EVAL_STRICT_REGRESSION"):
        monkeypatch.delenv(var, raising=False)
    assert main([]) == 2

    # Exit 2 = golden set unreadable (checked before any network use).
    assert main(["--base-url", "https://example.invalid", "--password", "x",
                 "--golden-set", str(tmp_path / "missing.json")]) == 2

    # Exit 2 = malformed golden set.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "questions": []}))
    assert main(["--base-url", "https://example.invalid", "--password", "x",
                 "--golden-set", str(bad)]) == 2


def test_answer_excerpt_capped_at_400():
    # The report contract caps answer_excerpt at 400 chars.
    from scripts.eval_answers import ANSWER_EXCERPT_CHARS
    question = {"id": "g-a", "expect": {"systems_min": 0}}
    result = {"summary": {"system_queries": 1}, "answer": "x" * 1000,
              "done": True,
              "errors": [], "elapsed_seconds": 1.0, "tool_summaries": []}
    row = _scored_row(question, "sid", result, strict=False)
    assert len(row["answer_excerpt"]) == ANSWER_EXCERPT_CHARS == 400


def test_trend_block_present_only_with_previous():
    namespace = create_session_namespace(prefix="eval")
    rows = [_row("g-a", "pass")]
    with_prev = build_eval_report(base_url="u", namespace=namespace,
                                  golden_set="g", strict=False, rows=rows,
                                  previous={"questions": []})
    without = build_eval_report(base_url="u", namespace=namespace,
                                golden_set="g", strict=False, rows=rows)
    assert "trend" in with_prev and "trend" not in without
    assert with_prev["trend"]["informational"] is True


def test_ab_diff_missing_sha_has_no_transitions_deltas_or_regression_counts():
    previous = {"questions": [_row("g-a", "pass", 10.0, metrics=_metrics())]}
    rows = [_row("g-a", "fail", 11.0, metrics=_metrics())]

    diff = ab_diff(previous, rows, golden_set_sha=None)

    assert diff["comparable"] is False
    assert diff["questions"] == [{
        "id": "g-a", "status": "non-comparable",
        "reason": "golden-set-sha-missing"}]
    assert diff["suite"] == {}
    report = {"trend": diff}
    assert final_exit_code(rows, report, strict_regression=True) == \
        final_exit_code(rows, report, strict_regression=False)


# --------------------------------------------------------------------------- #
# 2026-09-27 — a verdict without the answer is not diagnosable: FAILED rows keep
# the full answer (CLI lanes only); passing rows keep the excerpt shape.
# --------------------------------------------------------------------------- #
def test_failed_row_keeps_the_full_answer_and_passing_row_does_not():
    import types
    from scripts import eval_answers as ea
    from agents import eval_core
    long_answer = "x" * (eval_core.ANSWER_EXCERPT_CHARS + 500)
    args = types.SimpleNamespace(no_failed_answers=False)
    failed = ea._keep_failed_answer({"verdict": "fail", "answer_excerpt": long_answer[:400]},
                                    {"answer": long_answer}, args)
    assert failed[eval_core.FAILED_ANSWER_KEY] == long_answer
    assert len(failed["answer_excerpt"]) == eval_core.ANSWER_EXCERPT_CHARS
    passed = ea._keep_failed_answer({"verdict": "pass"}, {"answer": long_answer}, args)
    assert eval_core.FAILED_ANSWER_KEY not in passed
    opted_out = ea._keep_failed_answer({"verdict": "fail"}, {"answer": long_answer},
                                       types.SimpleNamespace(no_failed_answers=True))
    assert eval_core.FAILED_ANSWER_KEY not in opted_out


def test_smoke_lane_accepts_the_opt_out_flag():
    from scripts import smoke_prompts as sp
    parser_src = open(sp.__file__, encoding="utf-8").read()
    assert "--no-failed-answers" in parser_src
    assert '"answer_full": result.get("answer")' in parser_src


def test_ab_rollup_survives_a_non_comparable_trend(capsys):
    """--previous against a report from another golden set (2026-10-01)
    wrote the report, then crashed the lane with KeyError 'pass_previous':
    a non-comparable trend carries an EMPTY suite."""
    from scripts.eval_answers import _print_ab_rollup
    _print_ab_rollup({"trend": {"informational": True, "variant": "sonnet5",
                                "previous_variant": None, "comparable": False,
                                "questions": [], "suite": {}}})
    assert "non-comparable" in capsys.readouterr().out
