# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for scripts/audit_answer.py — the annotate-never-gate
contract and the measured-only cost rule.

Pinned invariants (ADR-0053 posture):
  * exit 0 with findings from a fake LLM seam AND the input report file
    byte-unchanged — findings annotate, they never flip verdicts/exit codes;
  * NO exit-1 path exists anywhere in the module (0 = completed, 2 = error);
  * usage carries MEASURED token counts from the converse responses or is
    ABSENT — never estimated, never dollars;
  * the default model id comes from agents/model_catalog.py (single source —
    no literal model ids).

scripts/ lives at the repo root (outside the agentic_digital_thread
PYTHONPATH), so we add the repo root to sys.path and import
`scripts.audit_answer` as a namespace package (same mechanism as
test_audit_learning_probes.py). The module-level `_converse_factory` seam
(the feedback_exploration `_stream_factory` idiom) keeps every test off the
network (--disable-socket lane).
"""
import inspect
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import audit_answer  # noqa: E402

from agents import model_catalog  # noqa: E402


def _eval_report_doc() -> dict:
    """A minimal eval_answers-shaped report with one failing question."""
    return {
        "base_url": "https://example.test",
        "run_id": "r1",
        "session_prefix": "eval",
        "golden_set": "golden_evals.json",
        "run_path": "cli",
        "suite": {"pass": 0, "fail": 1, "skipped": 0, "total": 1},
        "questions": [{
            "id": "q-defect-chain", "session_id": "eval-r1-1",
            "verdict": "fail",
            "failures": ["expected system_queries >= 2 (got 0)"],
            "systems_queried": 0, "neptune_queries": 1,
            "elapsed_seconds": 12.5, "answer_excerpt": "No batch data found",
            "honesty": {"absence_markers_matched": [],
                        "fabrication_flags": []},
            "metrics": None,
        }],
    }


def _write_report(tmp_path, doc=None) -> Path:
    path = tmp_path / "eval_report.json"
    path.write_text(json.dumps(doc or _eval_report_doc()), encoding="utf-8")
    return path


def _write_evidence(tmp_path) -> Path:
    doc = {"trace": {"session_id": "eval-r1-1", "steps": [
        {"id": "trace-step-7", "summary": "queried MES for batch"},
        {"rule_id": "ir-mes-0123456789abcdef"},
    ]}}
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _fake_converse(findings, usage=None, calls=None):
    """A scripted seam: returns the findings as the model text; records calls."""
    def fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        response = {"output": {"message": {"content": [
            {"text": json.dumps(findings)}]}}}
        if usage is not None:
            response["usage"] = dict(usage)
        return response
    return fake


_VALID_FINDING = {
    "taxonomy": "mapping_gap",
    "question_id": "q-defect-chain",
    "claim_excerpt": "No batch data found",
    "evidence_refs": ["trace-step-7"],
    "rationale": "the MES trace shows the batch join was never attempted",
}


# ── annotate-never-gate ──────────────────────────────────────────────────────

def test_exit_zero_with_findings_and_input_report_byte_unchanged(
        tmp_path, monkeypatch):
    """Pinned: findings never flip verdicts/exit codes AND the input report
    is never touched (byte-for-byte)."""
    report_path = _write_report(tmp_path)
    evidence_path = _write_evidence(tmp_path)
    before = report_path.read_bytes()
    monkeypatch.setattr(
        audit_answer, "_converse_factory",
        _fake_converse([_VALID_FINDING],
                       usage={"inputTokens": 321, "outputTokens": 45}))
    out_path = tmp_path / "findings.json"
    code = audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"])
    assert code == 0
    assert report_path.read_bytes() == before
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["run_path"] == "cli"
    assert doc["discarded_invalid"] == 0
    assert len(doc["findings"]) == 1
    finding = doc["findings"][0]
    assert finding["taxonomy"] == "mapping_gap"
    assert finding["evidence_refs"] == ["trace-step-7"]
    assert finding["id"]  # deterministic id assigned when the model gave none


def test_no_exit_one_path_exists():
    """Pinned: there is NO exit 1 — the module source carries no such path."""
    src = inspect.getsource(audit_answer)
    assert not re.search(r"\breturn\s+1\b", src)
    assert not re.search(r"\bexit\(\s*1\s*\)", src)


def test_no_flagged_rows_skips_llm_and_exits_zero(tmp_path, monkeypatch):
    doc = _eval_report_doc()
    doc["questions"][0]["verdict"] = "pass"
    doc["questions"][0]["failures"] = []
    doc["suite"] = {"pass": 1, "fail": 0, "skipped": 0, "total": 1}
    report_path = _write_report(tmp_path, doc)
    calls: list = []
    monkeypatch.setattr(audit_answer, "_converse_factory",
                        _fake_converse([], calls=calls))
    out_path = tmp_path / "findings.json"
    code = audit_answer.main(["--report", str(report_path),
                              "--out", str(out_path),
                              "--model", "test-model"])
    assert code == 0
    assert calls == []  # nothing flagged ⇒ the LLM is never asked
    findings_doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert findings_doc["findings"] == []
    assert findings_doc["discarded_invalid"] == 0


def test_skipped_rows_never_audit():
    report = _eval_report_doc()
    report["questions"][0]["verdict"] = "skipped"
    assert audit_answer.failed_or_flagged_rows(report) == []


def test_flagged_pass_row_with_fabrication_flags_audits():
    report = _eval_report_doc()
    row = report["questions"][0]
    row["verdict"] = "pass"
    row["honesty"]["fabrication_flags"] = ["quantified an untracked metric"]
    assert audit_answer.failed_or_flagged_rows(report) == [row]


def test_smoke_shaped_rows_flag_on_grounding_and_errors():
    report = {"questions": [
        {"title": "ok prompt", "done": True, "grounding": "grounded",
         "err_labels": [], "fallback_signals": []},
        {"title": "ungrounded prompt", "done": True,
         "grounding": "ungrounded", "err_labels": [],
         "fallback_signals": []},
        {"title": "errored prompt", "done": True, "grounding": "grounded",
         "err_labels": ["TIMEOUT"], "fallback_signals": []},
    ]}
    flagged = audit_answer.failed_or_flagged_rows(report)
    assert [r["title"] for r in flagged] == ["ungrounded prompt",
                                             "errored prompt"]


# ── cost: measured-only, never estimated, never dollars ─────────────────────

def test_usage_carries_measured_tokens_from_converse(tmp_path, monkeypatch):
    report_path = _write_report(tmp_path)
    evidence_path = _write_evidence(tmp_path)
    monkeypatch.setattr(
        audit_answer, "_converse_factory",
        _fake_converse([_VALID_FINDING],
                       usage={"inputTokens": 1000, "outputTokens": 200}))
    out_path = tmp_path / "findings.json"
    assert audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"]) == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["usage"] == {"input_tokens": 1000, "output_tokens": 200}


def test_usage_absent_when_responses_carry_no_counts(tmp_path, monkeypatch):
    report_path = _write_report(tmp_path)
    evidence_path = _write_evidence(tmp_path)
    monkeypatch.setattr(audit_answer, "_converse_factory",
                        _fake_converse([_VALID_FINDING]))  # no usage block
    out_path = tmp_path / "findings.json"
    assert audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"]) == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert "usage" not in doc  # ABSENT — never estimated
    # never dollars, anywhere in the document
    dumped = json.dumps(doc).lower()
    for forbidden in ("cost", "usd", "dollar", "estimated", "price"):
        assert forbidden not in dumped


def test_findings_doc_top_level_keys_are_the_pinned_schema(tmp_path,
                                                           monkeypatch):
    report_path = _write_report(tmp_path)
    evidence_path = _write_evidence(tmp_path)
    monkeypatch.setattr(
        audit_answer, "_converse_factory",
        _fake_converse([_VALID_FINDING],
                       usage={"inputTokens": 1, "outputTokens": 1}))
    out_path = tmp_path / "findings.json"
    audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"])
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(doc) == {"version", "source_report_sha", "run_path",
                        "findings", "discarded_invalid", "usage"}


# ── exit contract: 2 = usage/input/model error ──────────────────────────────

def test_missing_report_arg_is_usage_error(monkeypatch):
    monkeypatch.delenv("AUDIT_ANSWER_REPORT", raising=False)
    assert audit_answer.main([]) == 2


def test_unreadable_report_is_input_error(tmp_path):
    assert audit_answer.main(
        ["--report", str(tmp_path / "missing.json")]) == 2


def test_non_report_json_is_input_error(tmp_path):
    path = tmp_path / "not_a_report.json"
    path.write_text(json.dumps({"rules": []}), encoding="utf-8")
    assert audit_answer.main(["--report", str(path)]) == 2


def test_unreadable_evidence_is_input_error(tmp_path):
    report_path = _write_report(tmp_path)
    assert audit_answer.main([
        "--report", str(report_path),
        "--evidence", str(tmp_path / "missing_evidence.json")]) == 2


def test_converse_failure_is_model_error(tmp_path, monkeypatch):
    report_path = _write_report(tmp_path)

    def boom(**kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(audit_answer, "_converse_factory", boom)
    assert audit_answer.main(["--report", str(report_path),
                              "--model", "test-model"]) == 2


# ── sibling conventions ──────────────────────────────────────────────────────

def test_build_parser_env_fallbacks():
    env = {"AUDIT_ANSWER_REPORT": "/tmp/r.json",
           "AUDIT_ANSWER_OUT": "/tmp/o.json",
           "AUDIT_ANSWER_MODEL": "some-model"}
    args = audit_answer.build_parser(environ=env).parse_args([])
    assert args.report == "/tmp/r.json"
    assert args.out == "/tmp/o.json"
    assert args.model == "some-model"


def test_default_model_id_is_the_catalog_single_source():
    """No literal model ids: the default comes from agents/model_catalog."""
    assert audit_answer.default_model_id() == model_catalog.default_model(
        model_catalog.ORCHESTRATOR)
    src = inspect.getsource(audit_answer)
    assert "anthropic." not in src
    assert "openai." not in src


def test_max_tokens_is_explicit_in_every_converse_call(tmp_path, monkeypatch):
    report_path = _write_report(tmp_path)
    calls: list = []
    monkeypatch.setattr(audit_answer, "_converse_factory",
                        _fake_converse([], calls=calls))
    assert audit_answer.main(["--report", str(report_path),
                              "--out", str(tmp_path / "f.json"),
                              "--model", "test-model",
                              "--max-tokens", "1234"]) == 0
    assert calls, "the flagged row must reach the seam"
    assert all(call["max_tokens"] == 1234 for call in calls)
