# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-side golden-eval runner (agents/eval_runs.py) — pinned invariants.

Offline — no AWS, no network. Both registries are in-memory
``JobStore(..., table=None)`` (the ``test_feedback_exploration.py`` idiom);
the stream is scripted through the module-level ``_stream_factory`` seam;
the worker runs INLINE (a fake Thread executes the target synchronously).

Pins:

* run-ALL only — the seam receives EVERY golden, in order (no subset path
  exists in the module);
* synthetic markers — every seam session id starts with ``eval-run-`` and
  BOTH ``_default_stream`` branches carry ``write_memory=False`` +
  ``user_id="eval-run"``;
* run-path tag — the persisted report AND the index summary carry
  ``run_path == "server"``;
* lock — second start 409s (distinct reason), stale takeover past the
  threshold, slot released in ``finally`` even on worker exception;
* cross-replica status — a poll through a FRESH store backed by the same
  fake table (the ``test_job_store.py`` idiom) still resolves;
* size guard — progressive ``answer_excerpt`` then ``trend`` drops;
* index — replace-not-accrete, bounded newest-50;
* no demo literals in ``eval_runs.py`` / ``eval_routes.py`` (the goldens
  path comes from ``eval_core`` — the sanctioned indirection).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_runs as er  # noqa: E402
from agents.events import (  # noqa: E402
    DoneEvent, ErrorEvent, MessageEvent, MetadataEvent,
)
from agents.job_store import JobStore  # noqa: E402

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


# ── harness ─────────────────────────────────────────────────────────────────

class _FakeTable:
    """Minimal shared DynamoDB Table double (the test_job_store idiom)."""

    def __init__(self, store: dict):
        self._store = store

    def put_item(self, Item, **_condition):  # noqa: N803 (boto3 kwarg)
        # Conditional kwargs (JobStore.claim_slot/release_slot) are accepted
        # and ignored: this double models a SHARED backend for cross-replica
        # reads; slot semantics are pinned in test_job_store_slot.py.
        self._store[Item["system_id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)


@pytest.fixture(autouse=True)
def _fresh_stores(monkeypatch):
    """Fresh in-memory registries per test."""
    monkeypatch.setattr(er, "_JOBS", JobStore("eval-run", table=None))
    monkeypatch.setattr(er, "_REPORTS", JobStore("eval-report", table=None))


@pytest.fixture(autouse=True)
def _inline_worker(monkeypatch):
    """er's view of threading.Thread executes the target synchronously."""
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(er, "threading", SimpleNamespace(Thread=_InlineThread))


def _noop_worker(monkeypatch):
    """A Thread that never runs — keeps the active slot claimed."""
    class _NoopThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(er, "threading", SimpleNamespace(Thread=_NoopThread))


def _golden(qid: str, text: str, expect=None, **extra) -> dict:
    return {"id": qid, "text": text,
            "expect": expect if expect is not None else
            {"answer_must_match": ["the answer"]}, **extra}


def _write_goldens(tmp_path: Path, questions: list) -> str:
    path = tmp_path / "goldens.json"
    path.write_text(json.dumps({"version": 1, "questions": questions}))
    return str(path)


def _use_goldens(monkeypatch, tmp_path, questions):
    path = _write_goldens(tmp_path, questions)
    monkeypatch.setattr(er, "_golden_set_path", lambda: path)
    return path


_PASSING_EVENTS = [
    MessageEvent(content="the answer"),
    MetadataEvent(data={"intelligence_summary": {
        "system_queries": 2, "neptune_queries": 1,
        "rules_applied": [{"id": "r1", "label": "rule"}]}}),
    DoneEvent(text="the answer"),
]


def _script_stream(monkeypatch, events=None, per_call=None):
    """Script the seam; returns the calls list of (prompt, session_id)."""
    calls: list[tuple] = []

    def factory(prompt, session_id):
        calls.append((prompt, session_id))

        def gen():
            yield from (per_call(len(calls)) if per_call
                        else (events or _PASSING_EVENTS))
        return gen()

    monkeypatch.setattr(er, "_stream_factory", factory)
    return calls


_THREE_GOLDENS = [
    _golden("q-alpha", "first question?"),
    _golden("q-beta", "second question?"),
    _golden("q-gamma", "third question?"),
]


# ── run-ALL only + ordering ─────────────────────────────────────────────────

def test_run_streams_every_golden_in_order(monkeypatch, tmp_path):
    """The seam receives the FULL golden list, in file order — no subset
    machinery exists anywhere in the module."""
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    calls = _script_stream(monkeypatch)
    run_id, reason = er.start_run(None)
    assert reason is None and run_id
    assert [prompt for prompt, _ in calls] == [
        "first question?", "second question?", "third question?"]
    assert len(calls) == len(_THREE_GOLDENS)


def test_skipped_golden_gets_skipped_row_and_no_stream(monkeypatch, tmp_path):
    """A skip_unless_env-gated golden is reported as skipped (never silently
    dropped) and never streamed; the other goldens still ALL run."""
    monkeypatch.delenv("EVAL_OPTIONAL_GATE", raising=False)
    goldens = [
        _golden("q-1", "first question?"),
        _golden("q-gated", "gated question?",
                skip_unless_env="EVAL_OPTIONAL_GATE"),
        _golden("q-3", "third question?"),
    ]
    _use_goldens(monkeypatch, tmp_path, goldens)
    calls = _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    assert [prompt for prompt, _ in calls] == [
        "first question?", "third question?"]
    report = er._reports().get(run_id)
    verdicts = {row["id"]: row["verdict"] for row in report["questions"]}
    assert verdicts["q-gated"] == "skipped"
    assert report["suite"]["total"] == 3


# ── synthetic markers ───────────────────────────────────────────────────────

def test_every_session_id_carries_eval_run_prefix(monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    calls = _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    session_ids = [sid for _, sid in calls]
    assert session_ids == [f"eval-run-{run_id}-q{i}"
                           for i in (1, 2, 3)]
    assert all(sid.startswith("eval-run-") for sid in session_ids)


def _fake_chat_module(captured: list):
    module = ModuleType("agents.chat_client")

    class _Client:
        def stream(self, **kwargs):
            captured.append(kwargs)
            return iter(())

    module.AgentCoreChatClient = _Client
    module.LocalChatClient = _Client
    return module


@pytest.mark.parametrize("has_runtime", [True, False],
                         ids=["agentcore", "local"])
def test_default_stream_write_memory_false_both_branches(
        monkeypatch, has_runtime):
    """BOTH _default_stream branches carry write_memory=False and the
    eval-run user id — eval traffic never writes memory."""
    captured: list[dict] = []
    monkeypatch.setitem(sys.modules, "agents.chat_client",
                        _fake_chat_module(captured))
    config_module = ModuleType("agents.config")
    config_module.get_config = (
        lambda: SimpleNamespace(has_runtime=has_runtime))
    monkeypatch.setitem(sys.modules, "agents.config", config_module)
    er._default_stream("prompt text", "eval-run-abc-q1")
    assert len(captured) == 1
    call = captured[0]
    assert call["write_memory"] is False
    assert call["user_id"] == "eval-run"
    assert call["session_id"] == "eval-run-abc-q1"
    assert "_adt_identity" not in call


# ── run-path tag persisted ──────────────────────────────────────────────────

def test_persisted_report_and_index_carry_run_path_server(
        monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run("arm-b")
    report = er._reports().get(run_id)
    assert report["run_path"] == "server"
    assert report["variant"] == "arm-b"
    assert report["run_id"] == run_id
    assert report["golden_set_sha"]
    index = er._reports().get(er._INDEX_KEY)
    summary = index["runs"][0]
    assert summary["run_id"] == run_id
    assert summary["run_path"] == "server"
    assert summary["variant"] == "arm-b"
    assert summary["suite"]["total"] == 3
    # The tag survives a JSON round-trip (the persisted shape).
    assert json.loads(json.dumps(report))["run_path"] == "server"


def test_report_rows_are_scored_by_eval_core(monkeypatch, tmp_path):
    """Verdicts come from the computed scorer — passing and failing goldens
    are distinguished deterministically."""
    goldens = [
        _golden("q-pass", "first?"),
        _golden("q-fail", "second?",
                expect={"answer_must_match": ["never-present-pattern"]}),
    ]
    _use_goldens(monkeypatch, tmp_path, goldens)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    report = er._reports().get(run_id)
    verdicts = {row["id"]: row["verdict"] for row in report["questions"]}
    assert verdicts == {"q-pass": "pass", "q-fail": "fail"}
    assert report["suite"] == {**report["suite"], "pass": 1, "fail": 1}


def test_server_run_scores_the_answer_body_not_the_followup_chips(
        monkeypatch, tmp_path):
    """The SERVER lane reaches the same eval_core FOLLOWUPS boundary.

    ``---FOLLOWUPS---`` is a reserved protocol marker: everything after it is
    generated suggestion-question text, not an answer. A required value that
    exists ONLY in a chip must NOT pass — otherwise the admin tab reports a
    grounded answer the engine never gave. Fixing only the CLI would leave
    this path scoring chips.
    """
    goldens = [_golden("q-chip", "how many?",
                       expect={"answer_must_match": ["1200 units"]})]
    _use_goldens(monkeypatch, tmp_path, goldens)
    chipped = ("Production data is unavailable for that line.\n"
               "---FOLLOWUPS---\nQ1: Break down the 1200 units by shift?")
    _script_stream(monkeypatch, events=[
        MessageEvent(content=chipped),
        MetadataEvent(data={"intelligence_summary": {
            "system_queries": 2, "neptune_queries": 1}}),
        DoneEvent(text=chipped),
    ])
    run_id, _ = er.start_run(None)
    row = er._reports().get(run_id)["questions"][0]
    assert row["verdict"] == "fail"
    assert any("missing required pattern" in f for f in row["failures"])
    # The full answer stays visible for the report/display path.
    assert "---FOLLOWUPS---" in row["answer_excerpt"]


# ── lock / stale takeover / finally-release ─────────────────────────────────

def test_second_start_rejected_while_active(monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    _noop_worker(monkeypatch)  # slot stays claimed
    run_id, reason = er.start_run(None)
    assert run_id and reason is None
    second_id, second_reason = er.start_run(None)
    assert second_id is None
    assert second_reason == er.REASON_ACTIVE


def test_stale_slot_takeover(monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    er._jobs()[er._ACTIVE_KEY] = {
        "status": "running", "run_id": "stuck",
        "started_at": time.time() - er._STALE_SECONDS - 1}
    run_id, reason = er.start_run(None)
    assert run_id and reason is None


def test_slot_released_and_error_typed_on_worker_exception(
        monkeypatch, tmp_path):
    """finally-release: a worker exception marks the run failed (exception
    TYPE only — never the message) and frees the slot."""
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)

    def boom(prompt, session_id):
        raise RuntimeError("secret internals leaked?")

    monkeypatch.setattr(er, "_stream_factory", boom)
    run_id, reason = er.start_run(None)
    assert run_id and reason is None
    record = er._jobs().get(run_id)
    assert record["status"] == "failed"
    assert record["error"] == "eval run failed: RuntimeError"
    assert "secret" not in json.dumps(record)
    assert er._jobs().get(er._ACTIVE_KEY)["status"] == "idle"


def test_no_goldens_rejects_with_distinct_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "_golden_set_path",
                        lambda: str(tmp_path / "missing.json"))
    run_id, reason = er.start_run(None)
    assert run_id is None
    assert reason == er.REASON_NO_GOLDENS
    # No slot was claimed by the rejected start.
    active = er._jobs().get(er._ACTIVE_KEY)
    assert not (isinstance(active, dict) and active.get("status") == "running")


def test_malformed_golden_set_rejects_not_raises(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 99}))
    monkeypatch.setattr(er, "_golden_set_path", lambda: str(bad))
    assert er.start_run(None) == (None, er.REASON_NO_GOLDENS)


# ── progress + read side ────────────────────────────────────────────────────

def test_progress_updates_per_question(monkeypatch, tmp_path):
    """The status record advances after EVERY question — a poll mid-run sees
    {current, total}."""
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    observed: list[int] = []
    run_holder: dict = {}

    def factory(prompt, session_id):
        record = er._jobs().get(run_holder["run_id"])
        observed.append(record["progress"]["current"])

        def gen():
            yield from _PASSING_EVENTS
        return gen()

    monkeypatch.setattr(er, "_stream_factory", factory)

    real_run_suite = er._run_suite

    def capture_run_id(run_id, *args):
        run_holder["run_id"] = run_id
        return real_run_suite(run_id, *args)

    monkeypatch.setattr(er, "_run_suite", capture_run_id)
    run_id, _ = er.start_run(None)
    assert observed == [0, 1, 2]  # progress BEFORE each question
    status = er.get_run(run_id)
    assert status["status"] == "complete"
    assert status["progress"] == {"current": 3, "total": 3}


def test_get_run_running_shows_progress(monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    _noop_worker(monkeypatch)
    run_id, _ = er.start_run(None)
    status = er.get_run(run_id)
    assert status["status"] == "running"
    assert status["progress"] == {"current": 0, "total": 3}
    assert "report" not in status


def test_get_run_complete_includes_full_report(monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    status = er.get_run(run_id)
    assert status["status"] == "complete"
    assert status["report"]["run_path"] == "server"
    assert len(status["report"]["questions"]) == 3


def test_get_run_unknown_and_reserved_keys_return_none():
    assert er.get_run("nope") is None
    assert er.get_run(er._ACTIVE_KEY) is None
    assert er.get_run(er._INDEX_KEY) is None


def test_get_run_resolves_from_report_after_status_ttl(monkeypatch, tmp_path):
    """A completed run whose short-TTL status record is gone still resolves
    through the 30-day report store."""
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    del er._jobs()[run_id]  # simulate the 4h status TTL reaping
    status = er.get_run(run_id)
    assert status["status"] == "complete"
    assert status["report"]["run_path"] == "server"


def test_cross_replica_status_resolution(monkeypatch, tmp_path):
    """The JobStore fake-table idiom: a run started on replica A resolves on
    replica B (fresh in-memory stores, same injected backend tables)."""
    shared_jobs: dict = {}
    shared_reports: dict = {}
    monkeypatch.setattr(er, "_JOBS",
                        JobStore("eval-run", table=_FakeTable(shared_jobs)))
    monkeypatch.setattr(
        er, "_REPORTS",
        JobStore("eval-report", table=_FakeTable(shared_reports)))
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)

    # "Replica B": fresh stores, same durable backend.
    monkeypatch.setattr(er, "_JOBS",
                        JobStore("eval-run", table=_FakeTable(shared_jobs)))
    monkeypatch.setattr(
        er, "_REPORTS",
        JobStore("eval-report", table=_FakeTable(shared_reports)))
    status = er.get_run(run_id)
    assert status["status"] == "complete"
    assert status["report"]["run_path"] == "server"
    listing = er.list_runs()
    assert listing["runs"][0]["run_id"] == run_id


def test_reset_state_drops_registries():
    er._reset_state()
    assert er._JOBS is None and er._REPORTS is None


# ── clamps ──────────────────────────────────────────────────────────────────

def test_question_clamp_scores_transport_failure(monkeypatch, tmp_path):
    """A question past the wall clamp is an honest transport failure (the
    turn was never delivered), and the run still completes."""
    _use_goldens(monkeypatch, tmp_path, [_golden("q-slow", "slow question?")])
    _script_stream(monkeypatch)
    monkeypatch.setattr(er, "_QUESTION_CLAMP_SECONDS", -1)
    run_id, _ = er.start_run(None)
    report = er._reports().get(run_id)
    row = report["questions"][0]
    assert row["verdict"] == "fail"
    assert row.get("transport_failure") is True
    assert er._jobs().get(run_id)["status"] == "complete"


def test_suite_deadline_fails_run_honestly(monkeypatch, tmp_path):
    _use_goldens(monkeypatch, tmp_path, _THREE_GOLDENS)
    _script_stream(monkeypatch)
    monkeypatch.setattr(er, "_suite_deadline_seconds", lambda n: -1)
    run_id, reason = er.start_run(None)
    assert run_id and reason is None
    record = er._jobs().get(run_id)
    assert record["status"] == "failed"
    assert record["error"] == "eval run failed: SuiteDeadlineExceeded"
    assert er._jobs().get(er._ACTIVE_KEY)["status"] == "idle"


# ── size guard ──────────────────────────────────────────────────────────────

def _bulk_report(rows: int, excerpt_chars: int, trend=None) -> dict:
    report = {
        "run_id": "r", "run_path": "server", "suite": {"total": rows},
        "questions": [{"id": f"q{i}", "verdict": "pass", "failures": [],
                       "answer_excerpt": "x" * excerpt_chars}
                      for i in range(rows)],
    }
    if trend is not None:
        report["trend"] = trend
    return report


def test_shrink_report_noop_when_small():
    report = _bulk_report(3, 100)
    assert er._shrink_report(report) is report


def test_shrink_report_drops_answer_excerpts_first():
    report = _bulk_report(1200, 400)  # ~480KB of excerpts alone
    shrunk = er._shrink_report(report)
    assert shrunk["answer_excerpts_dropped"] is True
    assert all(row["answer_excerpt"] == "" for row in shrunk["questions"])
    assert "trend" not in shrunk  # nothing else touched
    assert len(json.dumps(shrunk).encode()) <= er._MAX_REPORT_BYTES
    # Verdicts survive the shrink.
    assert all(row["verdict"] == "pass" for row in shrunk["questions"])


def test_shrink_report_drops_trend_second():
    report = _bulk_report(1200, 400, trend={"blob": "y" * 400_000})
    shrunk = er._shrink_report(report)
    assert shrunk["answer_excerpts_dropped"] is True
    assert "trend" not in shrunk
    assert shrunk["trend_dropped"] is True


# ── index bounds ────────────────────────────────────────────────────────────

def test_index_bounded_newest_first_replace_not_accrete():
    for i in range(60):
        er._update_index({"run_id": f"run-{i}", "run_path": "server",
                          "started_at": i})
    index = er._reports().get(er._INDEX_KEY)
    assert len(index["runs"]) == 50
    assert index["runs"][0]["run_id"] == "run-59"  # newest first
    assert index["runs"][-1]["run_id"] == "run-10"
    # Re-indexing the same run replaces its entry (never duplicates).
    er._update_index({"run_id": "run-59", "run_path": "server",
                      "started_at": 999})
    index = er._reports().get(er._INDEX_KEY)
    assert [r["run_id"] for r in index["runs"]].count("run-59") == 1
    assert index["runs"][0]["started_at"] == 999


# ── no demo literals ────────────────────────────────────────────────────────

@pytest.mark.parametrize("module_file", ["eval_runs.py", "eval_routes.py"])
def test_no_demo_literals_in_new_modules(module_file):
    """The goldens path comes ONLY from eval_core (which points into
    agents/demo/ — the sanctioned home); neither new module names the demo
    golden file or imports the demo package (ADR-0044)."""
    source = (AGENTS_DIR / module_file).read_text(encoding="utf-8")
    assert "golden_evals" not in source
    assert "from .demo" not in source
    assert "agents.demo" not in source
