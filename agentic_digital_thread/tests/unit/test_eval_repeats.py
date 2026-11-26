# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline T0.2 tests: --repeats, --no-memory containment ids, the L4-1
unstick procedure, and the golden-loader marking validation.

Canned SSE fixtures only — no network. Same repo-root-script import
mechanism as test_eval_answers_report.py. Contracts pinned here:
- N>1 rows gain ADDITIVE repeats[]/majority_verdict/state_instability keys;
  an N=1 default run keeps the legacy report shape byte-compatible.
- --no-memory session ids start with tryit- (runtime _is_tryit_session
  learning/usage/cost containment) AND contain briefing (server is_briefing
  memory suppression) in marker mode; field mode keeps the tryit- prefix
  and sends write_memory:false in the body instead of the briefing marker.
- '[Briefing skipped' is a latched server flag: exactly ONE plain unstick
  ask (neither briefing- nor tryit-marked) is sent, fully drained, then the
  eval ask retried; a second skip after the unstick aborts distinctly.
"""
import io
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402
from scripts import smoke_prompts as smoke  # noqa: E402

_PASS_SUMMARY = {"system_queries": 2, "neptune_queries": 1,
                 "total_tool_calls": 3, "rules_applied": [{"id": "r1"}],
                 "planned_with": {"value_mappings": 2}}
_FAIL_SUMMARY = {"system_queries": 0, "neptune_queries": 0,
                 "total_tool_calls": 1, "rules_applied": [{"id": "r1"}],
                 "planned_with": {"value_mappings": 2}}
_LEGACY_REPORT_KEYS = {"base_url", "run_id", "session_prefix", "golden_set",
                       "strict", "run_path", "variant", "golden_set_sha",
                       "suite", "questions"}
_LEGACY_ROW_KEYS = {"id", "session_id", "verdict", "failures",
                    "systems_queried", "neptune_queries", "elapsed_seconds",
                    "answer_excerpt", "honesty", "metrics"}


class _TrackedStream(io.BytesIO):
    """SSE byte stream that records whether it was drained to EOF."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.exhausted = False

    def __next__(self):
        try:
            return super().__next__()
        except StopIteration:
            self.exhausted = True
            raise


def _sse(answer="grounded answer", summary=_PASS_SUMMARY, done_text=""):
    """Canned /api/ask SSE stream (token + metadata + done events)."""
    parts = []
    if answer:
        parts.append(f"event: token\ndata: {json.dumps({'text': answer})}\n\n")
    if summary is not None:
        payload = {"intelligence_summary": summary}
        parts.append(f"event: metadata\ndata: {json.dumps(payload)}\n\n")
    parts.append(f"event: done\ndata: {json.dumps({'text': done_text})}\n\n")
    return _TrackedStream("".join(parts).encode())


def _skip_stream():
    return _sse(answer="", summary=None,
                done_text="[Briefing skipped — query in progress]")


class _FakeOpener:
    """Scripted urllib opener: login always ok; /api/ask pops streams."""

    def __init__(self, streams):
        self.streams = list(streams)
        self.ask_bodies = []

    def open(self, request, timeout=None):
        if request.get_full_url().endswith("/api/auth/login"):
            return io.BytesIO(b'{"status": "ok"}')
        self.ask_bodies.append(json.loads(request.data.decode()))
        return self.streams.pop(0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ADT_BASE_URL", "ADT_USERNAME", "ADT_PASSWORD",
                "EVAL_GOLDEN_SET", "EVAL_STRICT", "EVAL_REPORT",
                "EVAL_VARIANT", "EVAL_STRICT_REGRESSION", "EVAL_REPEATS"):
        monkeypatch.delenv(var, raising=False)


def _install_app(monkeypatch, opener):
    real_app = smoke.App  # capture BEFORE patching (the factory shadows it)

    def _factory(base_url):
        app = object.__new__(real_app)
        app.base = base_url.rstrip("/")
        app.opener = opener
        return app
    monkeypatch.setattr(smoke, "App", _factory)


def _golden_file(tmp_path, questions):
    path = tmp_path / "goldens.json"
    path.write_text(json.dumps({"version": 1, "questions": questions}))
    return str(path)


def _one_golden(tmp_path):
    return _golden_file(tmp_path, [
        {"id": "g-1", "text": "How many units shipped?",
         "expect": {"systems_min": 1}}])


def _run_main(monkeypatch, tmp_path, streams, extra_args, goldens=None):
    """Drive main() offline; returns (exit_code, report|None, opener).

    Passes --no-warmup: these tests exercise the MEASUREMENT path with
    exactly-scripted streams; the warmup readiness gate has its own tests
    below (which script the warmup stream explicitly)."""
    opener = _FakeOpener(streams)
    _install_app(monkeypatch, opener)
    report_path = tmp_path / "report.json"
    code = eval_answers.main(
        ["--base-url", "https://example.invalid", "--password", "x",
         "--golden-set", goldens or _one_golden(tmp_path),
         "--report", str(report_path), "--no-warmup"] + extra_args)
    report = (json.loads(report_path.read_text())
              if report_path.exists() else None)
    return code, report, opener


def test_repeats_majority_and_fresh_sessions(monkeypatch, tmp_path):
    # N=5, verdicts pass/fail/pass/pass/fail -> majority pass; fresh sessions.
    code, report, opener = _run_main(
        monkeypatch, tmp_path,
        [_sse(), _sse(summary=_FAIL_SUMMARY), _sse(), _sse(),
         _sse(summary=_FAIL_SUMMARY)], ["--repeats", "5"])
    assert code == 0
    assert report["repeats"] == 5
    row = report["questions"][0]
    assert row["verdict"] == "pass"
    assert row["majority_verdict"] == "pass"
    assert [r["verdict"] for r in row["repeats"]] == [
        "pass", "fail", "pass", "pass", "fail"]
    assert [r["run"] for r in row["repeats"]] == [1, 2, 3, 4, 5]
    # planned_with presence and rules_applied_count agree across repeats.
    assert row["state_instability"] is False
    sessions = [b["session_id"] for b in opener.ask_bodies]
    assert len(sessions) == 5 and len(set(sessions)) == 5


def test_repeats_majority_fail_gates_exit(monkeypatch, tmp_path):
    code, report, _ = _run_main(
        monkeypatch, tmp_path,
        [_sse(summary=_FAIL_SUMMARY), _sse(),
         _sse(summary=_FAIL_SUMMARY), _sse(),
         _sse(summary=_FAIL_SUMMARY)],
        ["--repeats", "5"])
    assert code == 1
    row = report["questions"][0]
    assert row["majority_verdict"] == "fail"
    # The base row is a run whose verdict MATCHES the majority.
    assert row["verdict"] == "fail" and row["failures"]


def test_fold_repeats_tie_and_state_instability():
    def _row(verdict, rules=1, planned=True):
        return {"verdict": verdict, "failures": [],
                "metrics": {"rules_applied_count": rules,
                            "planned_with_present": planned}}
    # Even-N tie is NOT a majority pass (H.3 strictly-more-than-half rule).
    folded = eval_answers._fold_repeats([_row("pass"), _row("fail")])
    assert folded["majority_verdict"] == "fail"
    # rules_applied_count disagreement across repeats => state_instability.
    unstable = eval_answers._fold_repeats(
        [_row("pass", rules=1), _row("pass", rules=2), _row("pass", rules=1)])
    assert unstable["state_instability"] is True
    # planned_with presence disagreement flags too.
    planned_flip = eval_answers._fold_repeats(
        [_row("pass", planned=True), _row("pass", planned=False),
         _row("pass", planned=True)])
    assert planned_flip["state_instability"] is True


def test_single_run_report_keeps_legacy_shape(monkeypatch, tmp_path):
    # Default N=1, no --no-memory: report + row shape byte-compatible with
    # the legacy key sets — NONE of the T0.2 additive keys appear.
    code, report, _ = _run_main(monkeypatch, tmp_path, [_sse()], [])
    assert code == 0
    assert set(report) == _LEGACY_REPORT_KEYS
    assert set(report["questions"][0]) == _LEGACY_ROW_KEYS


def test_no_memory_marker_session_ids(monkeypatch, tmp_path):
    code, report, opener = _run_main(
        monkeypatch, tmp_path, [_sse() for _ in range(5)],
        ["--repeats", "5", "--no-memory"])
    assert code == 0
    sessions = [b["session_id"] for b in opener.ask_bodies]
    assert len(sessions) == 5 and len(set(sessions)) == 5
    for session_id in sessions:
        assert session_id.startswith("tryit-")   # runtime containment
        assert "briefing" in session_id          # server memory suppression
    assert all("write_memory" not in b for b in opener.ask_bodies)
    assert report["no_memory"] is True
    assert report["memory_suppression"] == "marker"
    assert report["unsticks"] == []


def test_no_memory_field_mode_sends_body_field(monkeypatch, tmp_path):
    code, report, opener = _run_main(
        monkeypatch, tmp_path, [_sse()],
        ["--no-memory", "--memory-suppression", "field"])
    assert code == 0
    body = opener.ask_bodies[0]
    assert body["write_memory"] is False
    assert body["session_id"].startswith("tryit-")  # runtime containment kept
    # The body field REPLACES the briefing leg (post-T1.7 contract).
    assert "briefing" not in body["session_id"]
    assert report["memory_suppression"] == "field"


def test_briefing_skip_runs_exactly_one_unstick_then_retries(
        monkeypatch, tmp_path):
    unstick_stream = _sse(answer="OK")
    code, report, opener = _run_main(
        monkeypatch, tmp_path, [_skip_stream(), unstick_stream, _sse()],
        ["--no-memory"])
    assert code == 0
    bodies = opener.ask_bodies
    assert len(bodies) == 3
    # Exactly ONE unstick ask: canned question, id neither briefing- nor
    # tryit-marked, stream drained to completion (executes the flag clear).
    unstick = bodies[1]
    assert unstick["question"] == eval_answers.UNSTICK_QUESTION
    assert not unstick["session_id"].startswith("tryit-")
    assert "briefing" not in unstick["session_id"]
    assert unstick_stream.exhausted is True
    # The eval ask is then RETRIED with the containment markers intact.
    retry = bodies[2]
    assert retry["question"] == bodies[0]["question"]
    assert retry["session_id"] != bodies[0]["session_id"]
    assert retry["session_id"].startswith("tryit-")
    assert "briefing" in retry["session_id"]
    # The unstick is recorded in report metadata.
    assert len(report["unsticks"]) == 1
    assert report["unsticks"][0]["question_id"] == "g-1"
    assert report["unsticks"][0]["session_id"] == unstick["session_id"]
    assert report["questions"][0]["verdict"] == "pass"


def test_second_briefing_skip_after_unstick_aborts(monkeypatch, tmp_path,
                                                   capsys):
    code, report, opener = _run_main(
        monkeypatch, tmp_path,
        [_skip_stream(), _sse(answer="OK"), _skip_stream()], ["--no-memory"])
    assert code == 2
    assert report is None  # aborted before the report write
    out = capsys.readouterr().out
    assert "ABORT (env unhealthy)" in out
    assert "persisted after an unstick" in out
    assert len(opener.ask_bodies) == 3  # no blind retry loop


def test_more_than_two_unsticks_aborts(monkeypatch, tmp_path, capsys):
    goldens = _golden_file(tmp_path, [
        {"id": f"g-{n}", "text": f"Question {n}?", "expect": {}}
        for n in (1, 2, 3)])
    streams = [_skip_stream(), _sse(answer="OK"), _sse(),   # g-1: unstick 1
               _skip_stream(), _sse(answer="OK"), _sse(),   # g-2: unstick 2
               _skip_stream()]                              # g-3: would be #3
    code, report, _ = _run_main(monkeypatch, tmp_path, streams,
                                ["--no-memory"], goldens=goldens)
    assert code == 2
    assert report is None
    out = capsys.readouterr().out
    assert "ABORT (env unhealthy)" in out
    assert "more than 2 unsticks" in out


def test_ab_diff_compares_majority_verdict_when_both_carry_it():
    prev_report = {"golden_set_sha": "s", "questions": [
        {"id": "g-1", "verdict": "fail", "majority_verdict": "pass"},
        {"id": "g-2", "verdict": "fail"}]}
    rows = [{"id": "g-1", "verdict": "pass", "majority_verdict": "pass"},
            {"id": "g-2", "verdict": "pass", "majority_verdict": "pass"}]
    trend = eval_answers.ab_diff(prev_report, rows, golden_set_sha="s")
    by_id = {e["id"]: e for e in trend["questions"]}
    # Both sides carry majority_verdict -> compared on it: pass->pass stable.
    assert by_id["g-1"]["transition"] == "stable"
    # One side lacks it -> falls back to verdict: fail->pass improvement.
    assert by_id["g-2"]["transition"] == "improvement"


def test_loader_accepts_and_defaults_new_marking_keys(tmp_path):
    path = _golden_file(tmp_path, [
        {"id": "g-m", "text": "q?", "expect": {}, "improvable": True,
         "leak_class": "fact", "treatment_phase": "query-time"},
        {"id": "g-s", "text": "q?", "expect": {}, "improvable": True,
         "leak_class": "semantic", "treatment_phase": "map-time"},
        {"id": "g-n", "text": "q?", "expect": {}, "improvable": False},
        {"id": "g-legacy", "text": "q?", "expect": {}}])
    questions = eval_answers.load_golden_set(path)
    assert [q["id"] for q in questions] == ["g-m", "g-s", "g-n", "g-legacy"]


@pytest.mark.parametrize("question, needle", [
    ({"id": "g-x", "text": "q?", "expect": {}, "improvable": True,
      "treatment_phase": "query-time"}, "requires leak_class"),
    ({"id": "g-x", "text": "q?", "expect": {}, "improvable": True,
      "leak_class": "fact"}, "requires treatment_phase"),
    ({"id": "g-x", "text": "q?", "expect": {}, "leak_class": "fact"},
     "requires treatment_phase"),  # improvable defaults true once marked
    ({"id": "g-x", "text": "q?", "expect": {}, "improvable": True,
      "leak_class": "bogus", "treatment_phase": "query-time"},
     "leak_class must be one of"),
    ({"id": "g-x", "text": "q?", "expect": {}, "improvable": True,
      "leak_class": "fact", "treatment_phase": "sometime"},
     "treatment_phase must be one of"),
    ({"id": "g-x", "text": "q?", "expect": {}, "improvable": "yes"},
     "improvable must be a bool"),
])
def test_loader_rejects_invalid_markings(tmp_path, question, needle):
    path = _golden_file(tmp_path, [question])
    with pytest.raises(ValueError, match=needle):
        eval_answers.load_golden_set(path)


def test_loader_accepts_unmodified_demo_golden_set():
    # The bundled demo goldens carry NONE of the new keys — defaults apply
    # and the file loads byte-unchanged.
    raw = json.loads(Path(eval_answers.DEFAULT_GOLDEN_SET).read_text())
    questions = eval_answers.load_golden_set(eval_answers.DEFAULT_GOLDEN_SET)
    assert questions == raw["questions"]
    assert not any(key in q for q in questions
                   for key in ("improvable", "leak_class", "treatment_phase"))
    # The demo file does NOT set require_markings — the lenient path stays.
    assert "require_markings" not in raw


_MARKED = {"id": "g-marked", "text": "q?", "expect": {},
           "improvable": True, "leak_class": "fact",
           "treatment_phase": "query-time"}
_NOT_IMPROVABLE = {"id": "g-static", "text": "q?", "expect": {},
                   "improvable": False}
_UNMARKED = {"id": "g-bare", "text": "q?", "expect": {}}


def _require_markings_file(tmp_path, questions, require=True):
    path = tmp_path / "req.json"
    path.write_text(json.dumps({"version": 1, "require_markings": require,
                                "questions": questions}))
    return str(path)


def test_require_markings_rejects_fully_unmarked_question(tmp_path):
    # File-level opt-in: an unmarked question is a hard load error that
    # NAMES the offending question id (the T0.2 silent-corruption hole).
    path = _require_markings_file(tmp_path, [_MARKED, _UNMARKED])
    with pytest.raises(ValueError, match=r"g-bare.*require_markings"):
        eval_answers.load_golden_set(path)


def test_require_markings_accepts_fully_marked_file(tmp_path):
    # Valid markings on every question: explicit improvable:false counts,
    # as does improvable (explicit or defaulted) with both leak_class and
    # treatment_phase.
    defaulted = {"id": "g-defaulted", "text": "q?", "expect": {},
                 "leak_class": "semantic", "treatment_phase": "map-time"}
    path = _require_markings_file(
        tmp_path, [_MARKED, _NOT_IMPROVABLE, defaulted])
    questions = eval_answers.load_golden_set(path)
    assert [q["id"] for q in questions] == \
        ["g-marked", "g-static", "g-defaulted"]


def test_file_without_flag_keeps_lenient_per_question_behaviour(tmp_path):
    # No require_markings key (and explicit false): unmarked questions
    # still load — the legacy demo-set contract is unchanged.
    path = _golden_file(tmp_path, [_MARKED, _UNMARKED])
    assert len(eval_answers.load_golden_set(path)) == 2
    explicit_false = _require_markings_file(
        tmp_path, [_UNMARKED], require=False)
    assert len(eval_answers.load_golden_set(explicit_false)) == 1


def test_require_markings_must_be_bool(tmp_path):
    path = _require_markings_file(tmp_path, [_MARKED], require="yes")
    with pytest.raises(ValueError, match="require_markings must be a bool"):
        eval_answers.load_golden_set(path)


def test_parser_new_flags_and_env_defaults():
    help_text = eval_answers.build_parser(environ={}).format_help()
    for flag in ("--repeats", "--no-memory", "--memory-suppression"):
        assert flag in help_text
    args = eval_answers.build_parser(environ={}).parse_args([])
    assert (args.repeats, args.no_memory, args.memory_suppression) == \
        (1, False, "marker")
    assert eval_answers.build_parser(
        environ={"EVAL_REPEATS": "5"}).parse_args([]).repeats == 5
    # Garbage/low env values never crash the parser (default 1).
    assert eval_answers.build_parser(
        environ={"EVAL_REPEATS": "zap"}).parse_args([]).repeats == 1
    assert eval_answers.build_parser(
        environ={"EVAL_REPEATS": "0"}).parse_args([]).repeats == 1


def test_repeats_below_one_is_usage_error(monkeypatch, tmp_path):
    code, report, _ = _run_main(monkeypatch, tmp_path, [], ["--repeats", "0"])
    assert code == 2 and report is None


# --------------------------------------------------------------------------- #
# 2026-08-07 guardrails: transport-vs-content separation + warmup gate.
# Live incident: a post-deploy run scored repeats whose streams never
# delivered a done frame as CONTENT failures ("no intelligence_summary" plus
# one "missing required pattern" per expected pattern), poisoning majority
# verdicts. These tests pin the three guardrails.
# --------------------------------------------------------------------------- #
def _sse_no_done(answer="partial text"):
    """A stream that starts (token) but never delivers metadata or done —
    the shape a mid-stream client timeout / dropped connection produces."""
    parts = [f"event: token\ndata: {json.dumps({'text': answer})}\n\n"]
    return _TrackedStream("".join(parts).encode())


def test_incomplete_stream_retries_once_then_reports_transport(
        monkeypatch, tmp_path):
    """First ask never completes -> ONE redelivery retry; when the retry also
    fails to complete, the row is a TRANSPORT failure: exactly one failure
    line naming transport, transport_failure marker set, and NO per-pattern
    content failures for an answer that was never delivered."""
    monkeypatch.setattr(smoke, "PROMPT_RETRY_BACKOFF_SECONDS", 0)
    goldens = _golden_file(tmp_path, [
        {"id": "g-1", "text": "How many units shipped?",
         "expect": {"systems_min": 1,
                    "answer_must_match": ["alpha", "beta", "gamma"]}}])
    code, report, opener = _run_main(
        monkeypatch, tmp_path, [_sse_no_done(), _sse_no_done()], [],
        goldens=goldens)
    assert code == 1
    row = report["questions"][0]
    assert row["transport_failure"] is True
    assert len(row["failures"]) == 1
    assert "transport, not content" in row["failures"][0]
    # The undelivered answer must NOT be scored against content patterns.
    assert not any("missing required pattern" in f for f in row["failures"])
    # Both asks were attempted (original + redelivery retry).
    assert len(opener.ask_bodies) == 2
    assert "redeliver" in opener.ask_bodies[1]["session_id"]


def test_incomplete_stream_recovered_by_retry_scores_normally(
        monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "PROMPT_RETRY_BACKOFF_SECONDS", 0)
    code, report, _ = _run_main(
        monkeypatch, tmp_path, [_sse_no_done(), _sse()], [])
    assert code == 0
    row = report["questions"][0]
    assert row["verdict"] == "pass"
    assert "transport_failure" not in row


def test_fold_repeats_surfaces_transport_count(monkeypatch, tmp_path):
    """A majority row with transport repeats carries transport_failures=N so
    rollups can separate delivery problems from content regressions; the key
    is ABSENT when every repeat delivered (honest absence)."""
    monkeypatch.setattr(smoke, "PROMPT_RETRY_BACKOFF_SECONDS", 0)
    # 4 delivered repeats retain a 3/4 content pass; one transport repeat
    # (original + failed retry) invalidates the five-repeat campaign.
    streams = [_sse(), _sse_no_done(), _sse_no_done(), _sse(), _sse(),
               _sse(summary=_FAIL_SUMMARY)]
    code, report, _ = _run_main(
        monkeypatch, tmp_path, streams, ["--repeats", "5"])
    assert code == 1  # content passes, measurement is invalid
    row = report["questions"][0]
    assert row["majority_verdict"] == "pass"
    assert row["content_verdict"] == "pass"
    assert row["measurement_valid"] is False
    assert row["transport_failure_count"] == 1
    assert row["transport_failures"] == 1
    transported = [r for r in row["repeats"] if r.get("transport_failure")]
    assert len(transported) == 1


def test_clean_rows_carry_no_transport_keys(monkeypatch, tmp_path):
    """Honest absence: a fully-delivered run has neither transport key."""
    clean = _run_main(monkeypatch, tmp_path, [_sse()], [])[1]["questions"][0]
    assert "transport_failures" not in clean
    assert "transport_failure" not in clean


def test_warmup_gate_blocks_measurement_until_a_turn_completes(
        monkeypatch, tmp_path):
    """WITHOUT --no-warmup: the first (warmup) ask must complete before any
    golden is measured. Warmup succeeds -> goldens run; total asks = 1 warmup
    + 1 golden, and the warmup session id is unstick-shaped (never
    briefing-marked)."""
    opener = _FakeOpener([_sse(answer="OK"), _sse()])
    _install_app(monkeypatch, opener)
    report_path = tmp_path / "report.json"
    code = eval_answers.main(
        ["--base-url", "https://example.invalid", "--password", "x",
         "--golden-set", _one_golden(tmp_path), "--report", str(report_path)])
    assert code == 0
    assert len(opener.ask_bodies) == 2
    assert opener.ask_bodies[0]["session_id"].startswith("unstick-warmup-")
    assert "briefing" not in opener.ask_bodies[0]["session_id"]


def test_warmup_gate_aborts_when_env_never_serves(monkeypatch, tmp_path):
    """All warmup attempts incomplete -> exit 2 (usage/abort contract), no
    goldens measured, no misleading scorecard written."""
    monkeypatch.setattr(eval_answers, "_WARMUP_BACKOFF_SECONDS", 0)
    opener = _FakeOpener([_sse_no_done(), _sse_no_done(), _sse_no_done()])
    _install_app(monkeypatch, opener)
    report_path = tmp_path / "report.json"
    code = eval_answers.main(
        ["--base-url", "https://example.invalid", "--password", "x",
         "--golden-set", _one_golden(tmp_path), "--report", str(report_path)])
    assert code == 2
    assert not report_path.exists()
    assert len(opener.ask_bodies) == eval_answers._WARMUP_ATTEMPTS
