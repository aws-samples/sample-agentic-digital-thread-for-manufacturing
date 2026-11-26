# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``eval_core`` answer-text redaction + golden-set provenance (ADR-0064 D3/D5).

Offline, stdlib-only, pure functions throughout — no store, no runner, no
network. Pins here:

* ``redact_answer_text`` removes EVERY answer-derived fragment a row can
  carry: the ``answer_excerpt``, the ``fabrication_flags`` excerpts (in the
  honesty block AND in ``failures``, where the D2 detector also lands them),
  and any failure detail derived from stream ``errors``;
* the deterministic, STEWARD-AUTHORED failure strings survive VERBATIM — they
  are the diagnostic value of the lane and contain no customer turn text;
* the flag labels the redactor keeps are the labels the REAL detector emits
  (``FABRICATION_FLAG_LABELS`` is cross-checked against
  ``fabrication_flags`` output, so the two can never drift apart);
* the redactor is PURE (input never mutated, a NEW dict returned) and
  IDEMPOTENT (a second pass is a no-op);
* ``build_eval_report`` stamps ``golden_set_source`` ONLY when a caller states
  it — the additive-key discipline that keeps the CLI report shape
  byte-identical (``test_eval_repeats.py`` pins that legacy key set).
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402

# A sentinel standing in for model-produced answer prose. Deliberately NOT
# identifier-shaped, so it can only reach a row through an answer excerpt.
SENTINEL = "canarysentinelalpha"

# A golden whose expectations engage the D2 fabrication detector.
_ABSENCE_EXPECT = {"absence_honesty": True, "must_not_quantify": ["vibration"]}
_QUESTION_TEXT = "is vibration tracked anywhere?"
_FABRICATING_ANSWER = f"vibration is 42 rpm on WX-9911 {SENTINEL}"


def _row(**overrides) -> dict:
    row = {
        "id": "u-1", "session_id": "eval-run-abc-q1", "verdict": "fail",
        "failures": ["answer missing required pattern 'never-present'"],
        "systems_queried": 2, "neptune_queries": 1, "elapsed_seconds": 3.0,
        "answer_excerpt": f"the answer {SENTINEL}",
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": {"total_tool_calls": 4},
    }
    row.update(overrides)
    return row


# ── the detector's real output reduces to the pinned labels ─────────────────

def test_real_detector_flags_reduce_to_exactly_the_pinned_labels():
    """Anti-drift: ``FABRICATION_FLAG_LABELS`` must be what
    ``fabrication_flags`` actually emits — otherwise the redactor would leave
    an answer excerpt in place while looking like it worked."""
    flags = eval_core.fabrication_flags(
        _ABSENCE_EXPECT, _QUESTION_TEXT, _FABRICATING_ANSWER)
    assert len(flags) == 2, flags
    assert any(SENTINEL in flag for flag in flags)  # the excerpt IS answer text
    reduced = eval_core.redact_answer_text(
        _row(honesty={"absence_markers_matched": [],
                      "fabrication_flags": flags}))
    assert set(reduced["honesty"]["fabrication_flags"]) == \
        set(eval_core.FABRICATION_FLAG_LABELS)


def test_scored_row_failures_carry_the_flags_and_are_redacted_too():
    """The D2 detector's flags also land in ``failures`` (the gating path), so
    redacting only the honesty block would leak the excerpt."""
    question = {"id": "u-1", "text": _QUESTION_TEXT, "expect": _ABSENCE_EXPECT}
    scored = eval_core._scored_row(question, "eval-run-abc-q1", {
        "done": True, "answer": _FABRICATING_ANSWER, "summary": {},
        "errors": [], "elapsed_seconds": 1.0})
    assert any(SENTINEL in f for f in scored["failures"])
    redacted = eval_core.redact_answer_text(scored)
    assert SENTINEL not in json.dumps(redacted)
    assert set(redacted["failures"]) <= set(eval_core.FABRICATION_FLAG_LABELS)


# ── answer excerpt + error details ─────────────────────────────────────────

def test_answer_excerpt_is_emptied_and_the_row_is_marked():
    redacted = eval_core.redact_answer_text(_row())
    assert redacted["answer_excerpt"] == ""
    assert redacted[eval_core.ANSWER_TEXT_REDACTED_KEY] is True
    assert SENTINEL not in json.dumps(redacted)


def test_error_event_failure_is_replaced_by_a_measured_count():
    row = _row(failures=[f"error event(s): ['{SENTINEL} exploded']"])
    redacted = eval_core.redact_answer_text(row, error_count=3)
    assert redacted["failures"] == ["error event(s) (redacted, n=3)"]


def test_error_count_is_never_invented_when_it_was_not_measured():
    row = _row(failures=[f"error event(s): ['{SENTINEL}']"])
    redacted = eval_core.redact_answer_text(row)
    assert redacted["failures"] == ["error event(s) (redacted, n=?)"]


def test_transport_failure_error_frames_detail_is_replaced():
    question = {"id": "u-1", "text": "q?", "expect": {}}
    row = eval_core._transport_failure_row(question, "eval-run-abc-q1", {
        "done": False, "answer": f"partial {SENTINEL}",
        "errors": [f"{SENTINEL} upstream failure"], "elapsed_seconds": 2.0})
    assert SENTINEL in json.dumps(row)  # the unredacted row DOES carry it
    redacted = eval_core.redact_answer_text(row, error_count=1)
    assert SENTINEL not in json.dumps(redacted)
    assert redacted["failures"] == [
        "incomplete stream — no done frame (transport, not content) "
        "(error event(s) (redacted, n=1))"]
    assert redacted["transport_failure"] is True  # the marker survives


# ── steward-authored detail survives verbatim ──────────────────────────────

_STEWARD_FAILURES = [
    "answer missing required pattern 'open work orders'",
    "answer matched forbidden pattern 'not tracked'",
    "system_queries 1 < 2",
    "no intelligence_summary (needed neptune_queries >= 1)",
    "answer lacks an honest-absence marker",
    "answer echoed the injection canary",
]


@pytest.mark.parametrize("failure", _STEWARD_FAILURES)
def test_deterministic_failure_strings_are_persisted_verbatim(failure):
    redacted = eval_core.redact_answer_text(_row(failures=[failure]),
                                            error_count=2)
    assert redacted["failures"] == [failure]


def test_verdicts_ids_metrics_and_skip_reasons_are_untouched():
    skipped = eval_core._skipped_row(
        {"id": "u-skip"}, "skip_unless_env EVAL_GATE not set")
    redacted = eval_core.redact_answer_text(skipped)
    assert redacted["id"] == "u-skip"
    assert redacted["verdict"] == "skipped"
    assert redacted["skip_reason"] == "skip_unless_env EVAL_GATE not set"
    full = eval_core.redact_answer_text(_row())
    assert full["verdict"] == "fail"
    assert full["session_id"] == "eval-run-abc-q1"
    assert full["metrics"] == {"total_tool_calls": 4}
    assert full["systems_queried"] == 2 and full["neptune_queries"] == 1
    assert full["elapsed_seconds"] == 3.0


def test_absence_markers_are_engine_constants_and_survive():
    markers = list(eval_core.ABSENCE_HONESTY_MARKERS[:1])
    redacted = eval_core.redact_answer_text(_row(honesty={
        "absence_markers_matched": markers, "fabrication_flags": []}))
    assert redacted["honesty"]["absence_markers_matched"] == markers


# ── purity + idempotency ───────────────────────────────────────────────────

def test_redactor_is_pure_the_input_row_is_never_mutated():
    row = _row(honesty={"absence_markers_matched": [],
                        "fabrication_flags": eval_core.fabrication_flags(
                            _ABSENCE_EXPECT, _QUESTION_TEXT,
                            _FABRICATING_ANSWER)})
    before = copy.deepcopy(row)
    redacted = eval_core.redact_answer_text(row, error_count=1)
    assert row == before
    assert redacted is not row
    assert redacted["honesty"] is not row["honesty"]
    assert redacted["failures"] is not row["failures"]


def test_redactor_is_idempotent():
    row = _row(failures=[f"error event(s): ['{SENTINEL}']",
                         "answer missing required pattern 'x'"],
               honesty={"absence_markers_matched": [],
                        "fabrication_flags": eval_core.fabrication_flags(
                            _ABSENCE_EXPECT, _QUESTION_TEXT,
                            _FABRICATING_ANSWER)})
    once = eval_core.redact_answer_text(row, error_count=2)
    twice = eval_core.redact_answer_text(once, error_count=2)
    assert once == twice


def test_duplicate_flag_labels_collapse_once_their_excerpts_are_dropped():
    label = eval_core.FABRICATION_FLAG_LABELS[0]
    row = _row(honesty={"absence_markers_matched": [], "fabrication_flags": [
        f"{label}: 'first {SENTINEL}'", f"{label}: 'second {SENTINEL}'"]})
    redacted = eval_core.redact_answer_text(row)
    assert redacted["honesty"]["fabrication_flags"] == [label]


def test_a_non_dict_row_is_returned_unchanged():
    assert eval_core.redact_answer_text(None) is None


# ── provenance stamping is additive-when-engaged ────────────────────────────

_ROWS = [{
    "id": "g-a", "session_id": "s-1", "verdict": "pass", "failures": [],
    "systems_queried": 2, "neptune_queries": 1, "elapsed_seconds": 3.0,
    "answer_excerpt": "excerpt",
    "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
    "metrics": None,
}]
_LEGACY_REPORT_KEYS = {"base_url", "run_id", "session_prefix", "golden_set",
                       "strict", "run_path", "variant", "golden_set_sha",
                       "suite", "questions"}


def _report(**overrides) -> dict:
    kwargs = dict(base_url="internal",
                  namespace=SimpleNamespace(run_id="r1", prefix="eval-run-r1"),
                  golden_set="g", strict=False, rows=list(_ROWS),
                  run_path="server")
    kwargs.update(overrides)
    return eval_core.build_eval_report(**kwargs)


def test_no_source_stated_keeps_the_legacy_report_key_set():
    assert set(_report()) == _LEGACY_REPORT_KEYS


@pytest.mark.parametrize("source", ["packaged", "uploaded"])
def test_a_stated_source_adds_exactly_one_key(source):
    report = _report(golden_set_source=source)
    assert set(report) == _LEGACY_REPORT_KEYS | {"golden_set_source"}
    assert report["golden_set_source"] == source
    assert json.loads(json.dumps(report))["golden_set_source"] == source


def test_the_source_labels_and_the_uploaded_label_are_the_pinned_literals():
    assert eval_core.GOLDEN_SOURCE_PACKAGED == "packaged"
    assert eval_core.GOLDEN_SOURCE_UPLOADED == "uploaded"
    assert eval_core.UPLOADED_GOLDEN_LABEL == "uploaded"
    assert eval_core.ANSWER_TEXT_REDACTED_KEY == "answer_text_redacted"


def test_redaction_also_empties_the_failed_answer_text():
    from agents import eval_core
    row = {"id": "q", "verdict": "fail", "answer_excerpt": "secret prose",
           eval_core.FAILED_ANSWER_KEY: "secret prose in full", "failures": []}
    redacted = eval_core.redact_answer_text(row, error_count=0)
    assert redacted["answer_excerpt"] == "" and redacted[eval_core.FAILED_ANSWER_KEY] == ""
    assert row[eval_core.FAILED_ANSWER_KEY] == "secret prose in full"  # pure
