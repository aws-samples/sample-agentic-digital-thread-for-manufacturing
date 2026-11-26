# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""UPLOADED customer golden-set runs (agents/eval_runs.py) — ADR-0064.

Offline — no AWS, no network. The three registries are in-memory
``JobStore(..., table=None)``, the stream is scripted through the module-level
``_stream_factory`` seam, and the worker runs INLINE (a fake Thread executes
the target synchronously) — the ``test_eval_runs.py`` harness, reused.

Pins (the ADR-0064 Pinned-by table, Wave-2 rows):

* **#1 persisted NOWHERE** — a canary sentinel planted in the fake stream's
  answer text and the uploaded QUESTION TEXT appear in no store write, no
  report, no index row and no log record; every filesystem write seam
  (``open``-for-write, ``Path.write_*``, ``tempfile.*``, ``shutil`` copies) is
  spied and stays untouched. The uploaded path never reaches
  ``load_golden_set`` / ``golden_file_sha`` either — no file is read.
* **#7 provenance cannot be confused** — the report carries
  ``golden_set == "uploaded"`` (the fixed literal, never a path or filename),
  ``golden_set_source == "uploaded"`` and the CALLER's sha; the bounded index
  summary carries ``golden_set_source`` + ``question_count``.
* **#8 no answer-derived text** — every persisted row's ``answer_excerpt`` is
  ``""``, fabrication flags are reduced to their labels, stream-``errors``
  detail is a count, and the report says ``answer_text_redacted: True`` — while
  the deterministic (steward-authored) failure strings persist VERBATIM.
* **#9 TTL tiers** — an uploaded report item is written with a ~7 d ``ttl``
  attribute, a packaged one with ~30 d (read off an injected fake table).
* **#13 synthetic containment** — an uploaded run's session ids all carry the
  ``eval-run-`` prefix and every seam call carries ``write_memory=False``
  (exercised through the PRODUCTION ``_default_stream`` seam).
* **#14 ONE run at a time across BOTH entry points** — the shared
  ``_ACTIVE_KEY`` claim, in both orders, with the distinct 409 reason.
* **#15 packaged runs unchanged** — the packaged report equals a pinned dict
  apart from the additive provenance keys, and keeps its answer excerpts.
* the question-count ceiling is DERIVED from ``_suite_deadline_seconds`` and
  is a REFUSAL with its own reason, never a silent truncation.
"""
from __future__ import annotations

import builtins
import copy
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402
import agents.eval_runs as er  # noqa: E402
from agents.events import (  # noqa: E402
    DoneEvent, ErrorEvent, MessageEvent, MetadataEvent,
)
from agents.job_store import JobStore  # noqa: E402

#: Stands in for model-produced answer prose. Not identifier-shaped, so it can
#: only reach a persisted row through an answer excerpt / error detail.
SENTINEL = "canarysentinelalpha"

#: Stands in for CUSTOMER question text — must never be persisted or logged.
Q_SENTINEL = "customerquestioncanary"

UPLOADED_SHA = "abcdef123456"


# ── harness (the test_eval_runs.py idioms) ─────────────────────────────────

class _CapturingTable:
    """DynamoDB Table double that keeps the FULL item (incl. ``ttl``)."""

    def __init__(self):
        self.items: dict = {}

    def put_item(self, Item):  # noqa: N803 (boto3 kwarg)
        self.items[Item["system_id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self.items.pop(Key["system_id"], None)


class _SpyStore:
    """Spy persistence seam over an in-memory ``JobStore`` (ADR-0062 D5)."""

    def __init__(self, kind: str):
        self._inner = JobStore(kind, table=None)
        self.writes: list = []

    def get(self, key, default=None):
        return self._inner.get(key, default)

    def __getitem__(self, key):
        return self._inner[key]

    def __setitem__(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self._inner[key] = value

    def __delitem__(self, key):
        del self._inner[key]

    def pop(self, key, *args):
        return self._inner.pop(key, *args)

    # slot surface (spied like any write — the pin is about payload content)
    def claim_slot(self, key, state, **kwargs):
        ok = self._inner.claim_slot(key, state, **kwargs)
        if ok:
            self.writes.append((key, copy.deepcopy(state)))
        return ok

    def release_slot(self, key, owner, state):
        ok = self._inner.release_slot(key, owner, state)
        if ok:
            self.writes.append((key, copy.deepcopy(state)))
        return ok


@pytest.fixture(autouse=True)
def _fresh_stores(monkeypatch):
    """Fresh in-memory registries per test — including the 7-day tier."""
    monkeypatch.setattr(er, "_JOBS", JobStore("eval-run", table=None))
    monkeypatch.setattr(er, "_REPORTS", JobStore("eval-report", table=None))
    monkeypatch.setattr(er, "_REPORTS_SHORT",
                        JobStore("eval-report", table=None))


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


def _uploaded_goldens() -> list:
    """A 4-question customer set: a pass, a gate failure, an absence-honesty
    golden that trips the D2 fabrication detector, and a skipped one."""
    return [
        {"id": "u-pass", "text": f"{Q_SENTINEL} one?",
         "expect": {"answer_must_match": ["the answer"]}},
        {"id": "u-fail", "text": f"{Q_SENTINEL} two?",
         "expect": {"answer_must_match": ["never-present-pattern"]}},
        {"id": "u-fabricate", "text": f"{Q_SENTINEL} vibration?",
         "expect": {"absence_honesty": True,
                    "must_not_quantify": ["vibration"]}},
        {"id": "u-skip", "text": f"{Q_SENTINEL} gated?", "expect": {},
         "skip_unless_env": "EVAL_OPTIONAL_GATE"},
    ]


_ANSWER = f"the answer {SENTINEL}: vibration is 42 rpm on WX-9911"

_EVENTS = [
    MessageEvent(content=_ANSWER),
    MetadataEvent(data={"intelligence_summary": {
        "system_queries": 2, "neptune_queries": 1}}),
    DoneEvent(text=_ANSWER),
]


def _script_stream(monkeypatch, events=None) -> list:
    calls: list[tuple] = []

    def factory(prompt, session_id):
        calls.append((prompt, session_id))
        return iter(list(events if events is not None else _EVENTS))

    monkeypatch.setattr(er, "_stream_factory", factory)
    return calls


def _start_uploaded(goldens=None, variant=None, sha=UPLOADED_SHA):
    """Start an UPLOADED run. ``goldens=[]`` is passed through as given (the
    default substitution must not hide an empty-set refusal)."""
    return er.start_run(variant,
                        goldens=_uploaded_goldens() if goldens is None
                        else goldens,
                        golden_sha=sha,
                        golden_source=eval_core.GOLDEN_SOURCE_UPLOADED)


def _uploaded_report(run_id: str) -> dict:
    report = er._REPORTS_SHORT.get(run_id)
    assert isinstance(report, dict), "uploaded report not in the 7-day tier"
    return report


@pytest.fixture(autouse=True)
def _no_env_gate(monkeypatch):
    monkeypatch.delenv("EVAL_OPTIONAL_GATE", raising=False)


# ── #7 provenance ──────────────────────────────────────────────────────────

def test_uploaded_run_stamps_the_fixed_label_source_and_supplied_sha(
        monkeypatch):
    _script_stream(monkeypatch)
    run_id, reason = _start_uploaded()
    assert reason is None and run_id
    report = _uploaded_report(run_id)
    assert report["golden_set"] == eval_core.UPLOADED_GOLDEN_LABEL == "uploaded"
    assert report["golden_set_source"] == eval_core.GOLDEN_SOURCE_UPLOADED
    assert report["golden_set_sha"] == UPLOADED_SHA
    assert report["run_path"] == "server"
    assert report["suite"]["total"] == 4
    # ...and the label is not a path of any kind.
    assert "/" not in report["golden_set"] and ".json" not in report["golden_set"]


def test_uploaded_index_row_badges_source_and_question_count(monkeypatch):
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded(variant="arm-cust")
    summary = er.list_runs()["runs"][0]
    assert summary["run_id"] == run_id
    assert summary["golden_set_source"] == eval_core.GOLDEN_SOURCE_UPLOADED
    assert summary["question_count"] == 4
    assert summary["golden_set_sha"] == UPLOADED_SHA
    assert summary["run_path"] == "server"
    assert summary["variant"] == "arm-cust"
    assert Q_SENTINEL not in json.dumps(summary)


def test_uploaded_run_resolves_through_get_run(monkeypatch):
    """A read must resolve the 7-day tier too (JobStore is per-instance
    in-memory without a configured table)."""
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    status = er.get_run(run_id)
    assert status["status"] == "complete"
    assert status["report"]["golden_set_source"] == "uploaded"
    del er._JOBS[run_id]  # the 4h status record reaped
    assert er.get_run(run_id)["report"]["golden_set_sha"] == UPLOADED_SHA


# ── #8 redaction ───────────────────────────────────────────────────────────

def test_uploaded_report_persists_no_answer_derived_text(monkeypatch):
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    report = _uploaded_report(run_id)
    assert report[eval_core.ANSWER_TEXT_REDACTED_KEY] is True
    assert SENTINEL not in json.dumps(report)
    for row in report["questions"]:
        assert row["answer_excerpt"] == ""
        assert row[eval_core.ANSWER_TEXT_REDACTED_KEY] is True
        for flag in row["honesty"]["fabrication_flags"]:
            assert flag in eval_core.FABRICATION_FLAG_LABELS


def test_uploaded_report_keeps_verdicts_ids_and_steward_failures(monkeypatch):
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    report = _uploaded_report(run_id)
    rows = {row["id"]: row for row in report["questions"]}
    assert set(rows) == {"u-pass", "u-fail", "u-fabricate", "u-skip"}
    assert rows["u-pass"]["verdict"] == "pass"
    assert rows["u-skip"]["verdict"] == "skipped"
    assert rows["u-skip"]["skip_reason"] == \
        "skip_unless_env EVAL_OPTIONAL_GATE not set"
    # The steward's OWN authored pattern survives verbatim — the diagnostic
    # value of the lane, and not customer turn text.
    assert rows["u-fail"]["failures"] == [
        "answer missing required pattern 'never-present-pattern'"]
    # The fabrication gate still FAILS the row; only the excerpt is gone.
    assert rows["u-fabricate"]["verdict"] == "fail"
    assert set(rows["u-fabricate"]["failures"]) <= \
        set(eval_core.FABRICATION_FLAG_LABELS)
    assert rows["u-fabricate"]["failures"]
    assert rows["u-pass"]["metrics"]["neptune_queries"] == 1


def test_uploaded_stream_error_detail_becomes_a_measured_count(monkeypatch):
    _script_stream(monkeypatch, events=[
        MessageEvent(content=_ANSWER),
        ErrorEvent(message=f"{SENTINEL} upstream blew up"),
        DoneEvent(text=_ANSWER)])
    run_id, _ = _start_uploaded(goldens=[_uploaded_goldens()[0]])
    report = _uploaded_report(run_id)
    assert SENTINEL not in json.dumps(report)
    assert "error event(s) (redacted, n=1)" in report["questions"][0]["failures"]


def test_uploaded_transport_failure_detail_is_redacted(monkeypatch):
    """A stream that never delivers ``done`` carries error frames — the
    transport row's detail is answer/stream text too."""
    _script_stream(monkeypatch, events=[
        MessageEvent(content=_ANSWER),
        ErrorEvent(message=f"{SENTINEL} dropped")])
    run_id, _ = _start_uploaded(goldens=[_uploaded_goldens()[0]])
    report = _uploaded_report(run_id)
    row = report["questions"][0]
    assert row["transport_failure"] is True
    assert SENTINEL not in json.dumps(report)
    assert "(redacted, n=1)" in row["failures"][0]


def test_the_row_factory_redacts_before_the_caller_can_append(monkeypatch):
    """Structural pin: ``_question_row`` returns an ALREADY-redacted row, so
    the unredacted row exists only inside that frame — there is no window in
    which ``rows`` (and therefore the report) holds answer text."""
    _script_stream(monkeypatch)
    question = _uploaded_goldens()[2]  # the fabrication-detector golden
    plain = er._question_row("rid", 1, question, time.monotonic() + 60, False)
    assert SENTINEL in json.dumps(plain)  # the unredacted shape DOES carry it
    redacted = er._question_row("rid", 1, question,
                                time.monotonic() + 60, True)
    assert SENTINEL not in json.dumps(redacted)
    assert redacted["answer_excerpt"] == ""
    assert redacted[eval_core.ANSWER_TEXT_REDACTED_KEY] is True
    assert redacted["verdict"] == plain["verdict"] == "fail"


# ── #1 persisted NOWHERE ───────────────────────────────────────────────────

def test_no_store_write_carries_the_golden_bytes_or_question_text(monkeypatch):
    jobs, reports, short = (_SpyStore("eval-run"), _SpyStore("eval-report"),
                            _SpyStore("eval-report"))
    monkeypatch.setattr(er, "_JOBS", jobs)
    monkeypatch.setattr(er, "_REPORTS", reports)
    monkeypatch.setattr(er, "_REPORTS_SHORT", short)
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    all_writes = jobs.writes + reports.writes + short.writes
    assert all_writes
    blob = json.dumps(all_writes, default=str)
    assert Q_SENTINEL not in blob   # customer QUESTION text
    assert SENTINEL not in blob     # model ANSWER text
    # The report went to the 7-day tier only; the 30-day store holds the
    # bounded index item and nothing else.
    assert [key for key, _ in short.writes] == [run_id]
    assert [key for key, _ in reports.writes] == [er._INDEX_KEY]


_WRITE_SEAMS = ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile",
                "TemporaryDirectory", "mkstemp", "mkdtemp")


def test_uploaded_run_touches_no_filesystem_write_seam(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must never fire
        raise AssertionError("an uploaded golden set reached a write seam")

    real_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            _boom()
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)
    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "write_bytes", _boom)
    for name in _WRITE_SEAMS:
        monkeypatch.setattr(tempfile, name, _boom)
    for name in ("copyfile", "copyfileobj", "copy2", "copytree", "move"):
        monkeypatch.setattr(shutil, name, _boom)
    _script_stream(monkeypatch)
    run_id, reason = _start_uploaded()
    assert reason is None and run_id


def test_uploaded_run_reads_no_golden_file(monkeypatch):
    """The upload path reaches ``parse_golden_set`` only — never a path
    reader. ``golden_file_sha`` in particular must not be consulted: the sha
    the caller computed over the exact bytes is the one that gets stamped."""
    def _boom(*args, **kwargs):  # pragma: no cover - must never fire
        raise AssertionError("the uploaded path read a golden file")

    monkeypatch.setattr(eval_core, "load_golden_set", _boom)
    monkeypatch.setattr(eval_core, "golden_file_sha", _boom)
    _script_stream(monkeypatch)
    run_id, reason = _start_uploaded()
    assert reason is None and run_id
    assert _uploaded_report(run_id)["golden_set_sha"] == UPLOADED_SHA


def test_no_log_record_carries_question_or_answer_text(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    assert run_id
    text = "\n".join(f"{r.name}:{r.getMessage()}" for r in caplog.records)
    assert Q_SENTINEL not in text
    assert SENTINEL not in text


def test_a_failing_uploaded_run_logs_only_the_exception_type(
        monkeypatch, caplog):
    caplog.set_level("DEBUG")

    def boom(prompt, session_id):
        raise RuntimeError(f"{Q_SENTINEL} {SENTINEL} leaked?")

    monkeypatch.setattr(er, "_stream_factory", boom)
    run_id, _ = _start_uploaded()
    record = er._JOBS.get(run_id)
    assert record["error"] == "eval run failed: RuntimeError"
    text = json.dumps(record) + "\n".join(
        r.getMessage() for r in caplog.records)
    assert Q_SENTINEL not in text and SENTINEL not in text
    assert er._JOBS.get(er._ACTIVE_KEY)["status"] == "idle"


# ── #9 TTL tiers ───────────────────────────────────────────────────────────

def _ttl_of(table: _CapturingTable, run_id: str) -> int:
    item = table.items[f"__job__:eval-report:{run_id}"]
    return int(item["ttl"]) - int(time.time())


def test_uploaded_report_item_carries_the_seven_day_ttl(monkeypatch):
    table = _CapturingTable()
    monkeypatch.setattr(er, "_REPORTS_SHORT", JobStore(
        "eval-report", table=table,
        ttl_seconds=er._UPLOADED_REPORT_TTL_SECONDS))
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    assert er._UPLOADED_REPORT_TTL_SECONDS == 7 * 24 * 3600
    assert er._UPLOADED_REPORT_TTL_SECONDS - 60 <= _ttl_of(table, run_id) \
        <= er._UPLOADED_REPORT_TTL_SECONDS


def test_packaged_report_item_keeps_the_thirty_day_ttl(monkeypatch, tmp_path):
    table = _CapturingTable()
    monkeypatch.setattr(er, "_REPORTS", JobStore(
        "eval-report", table=table, ttl_seconds=er._REPORT_TTL_SECONDS))
    _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    assert er._REPORT_TTL_SECONDS == 30 * 24 * 3600
    assert er._REPORT_TTL_SECONDS - 60 <= _ttl_of(table, run_id) \
        <= er._REPORT_TTL_SECONDS


# ── #13 synthetic containment ──────────────────────────────────────────────

def test_uploaded_run_session_ids_all_carry_the_eval_run_prefix(monkeypatch):
    calls = _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    session_ids = [sid for _, sid in calls]
    # 4 goldens, one skipped -> 3 streamed, in file order.
    assert session_ids == [f"{er.SESSION_PREFIX}{run_id}-q{i}"
                           for i in (1, 2, 3)]
    assert all(sid.startswith("eval-run-") for sid in session_ids)


def _fake_chat_module(captured: list) -> ModuleType:
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
def test_uploaded_run_through_the_production_seam_never_writes_memory(
        monkeypatch, has_runtime):
    """The PRODUCTION seam (``_default_stream``) drives an uploaded run: every
    call carries ``write_memory=False`` and no ``_adt_identity``, so the system
    never trains on a customer's test set."""
    assert er._stream_factory is er._default_stream  # the module default
    captured: list[dict] = []
    monkeypatch.setitem(sys.modules, "agents.chat_client",
                        _fake_chat_module(captured))
    config_module = ModuleType("agents.config")
    config_module.get_config = lambda: SimpleNamespace(has_runtime=has_runtime)
    monkeypatch.setitem(sys.modules, "agents.config", config_module)
    run_id, reason = _start_uploaded()
    assert reason is None and run_id
    assert len(captured) == 3
    for call in captured:
        assert call["write_memory"] is False
        assert call["user_id"] == "eval-run"
        assert call["session_id"].startswith(er.SESSION_PREFIX)
        assert "_adt_identity" not in call


# ── #14 ONE run across BOTH entry points ───────────────────────────────────

def _use_packaged(monkeypatch, tmp_path) -> str:
    """Point the packaged golden source at a temp set (the existing
    ``test_eval_runs.py`` seam — ``_golden_set_path`` is the ONE path home)."""
    path = tmp_path / "goldens.json"
    path.write_text(json.dumps({"version": 1, "questions": [
        {"id": "g-demo", "text": "demo question?",
         "expect": {"answer_must_match": ["the answer"]}}]}))
    monkeypatch.setattr(er, "_golden_set_path", lambda: str(path))
    return str(path)


def test_an_active_packaged_run_blocks_an_uploaded_one(monkeypatch, tmp_path):
    _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    _noop_worker(monkeypatch)
    run_id, reason = er.start_run(None)
    assert run_id and reason is None
    assert _start_uploaded() == (None, er.REASON_ACTIVE)


def test_an_active_uploaded_run_blocks_a_packaged_one(monkeypatch, tmp_path):
    _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    _noop_worker(monkeypatch)
    run_id, reason = _start_uploaded()
    assert run_id and reason is None
    assert er.start_run(None) == (None, er.REASON_ACTIVE)


def test_the_lock_is_the_same_single_key_for_both_entry_points(monkeypatch):
    _script_stream(monkeypatch)
    _noop_worker(monkeypatch)
    run_id, _ = _start_uploaded()
    active = er._JOBS.get(er._ACTIVE_KEY)
    assert active == {"status": "running", "run_id": run_id,
                      "started_at": active["started_at"]}


def test_uploaded_start_releases_the_slot_when_the_worker_finishes(monkeypatch):
    _script_stream(monkeypatch)
    run_id, _ = _start_uploaded()
    assert er._JOBS.get(er._ACTIVE_KEY)["status"] == "idle"
    # ...so a second (packaged or uploaded) run is accepted.
    second, reason = _start_uploaded()
    assert second and reason is None and second != run_id


# ── the question-count ceiling ─────────────────────────────────────────────

def test_over_the_question_ceiling_is_a_refusal_not_a_truncation(monkeypatch):
    calls = _script_stream(monkeypatch)
    goldens = [{"id": f"u-{i}", "text": f"{Q_SENTINEL} {i}?", "expect": {}}
               for i in range(er.MAX_UPLOADED_QUESTIONS + 1)]
    run_id, reason = _start_uploaded(goldens=goldens)
    assert run_id is None
    assert reason == er.REASON_TOO_MANY_QUESTIONS
    assert calls == []  # nothing ran — never a silent subset
    active = er._JOBS.get(er._ACTIVE_KEY)
    assert not (isinstance(active, dict) and active.get("status") == "running")


def test_at_the_ceiling_a_run_is_accepted(monkeypatch):
    _script_stream(monkeypatch)
    goldens = [{"id": f"u-{i}", "text": f"q {i}?", "expect": {}}
               for i in range(er.MAX_UPLOADED_QUESTIONS)]
    run_id, reason = _start_uploaded(goldens=goldens)
    assert run_id and reason is None
    assert _uploaded_report(run_id)["suite"]["total"] == \
        er.MAX_UPLOADED_QUESTIONS


def test_the_ceiling_is_derived_from_the_suite_deadline_arithmetic():
    """A run must never outlive its own status record: at the ceiling the
    deadline still COVERS the clamped work and sits inside the 4 h run-TTL
    horizon; past ~n=47 the ``_RUN_TTL_SECONDS - clamp`` cap bites and it
    stops covering the work — which is why the ceiling is a refusal."""
    n = er.MAX_UPLOADED_QUESTIONS
    deadline = er._suite_deadline_seconds(n)
    work = n * er._QUESTION_CLAMP_SECONDS
    assert deadline == work + er._SUITE_SLACK_SECONDS  # unclamped
    assert work < deadline < er._RUN_TTL_SECONDS
    over = 48
    assert er._suite_deadline_seconds(over) < over * er._QUESTION_CLAMP_SECONDS


def test_an_empty_uploaded_set_is_refused_with_the_no_goldens_reason(
        monkeypatch, tmp_path):
    _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    assert _start_uploaded(goldens=[]) == (None, er.REASON_NO_GOLDENS)


# ── #15 packaged runs unchanged (apart from the additive keys) ─────────────

_PACKAGED_ADDITIVE_KEYS = {"golden_set_source"}
_PACKAGED_SUMMARY_ADDITIVE_KEYS = {"golden_set_source", "question_count"}


def test_packaged_report_shape_is_unchanged_apart_from_the_additive_key(
        monkeypatch, tmp_path):
    path = _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run("arm-a")
    report = er._REPORTS.get(run_id)
    assert set(report) == {
        "base_url", "run_id", "session_prefix", "golden_set", "strict",
        "run_path", "variant", "golden_set_sha", "suite", "questions",
    } | _PACKAGED_ADDITIVE_KEYS
    assert report["golden_set"] == path              # still the PATH
    assert report["golden_set_source"] == eval_core.GOLDEN_SOURCE_PACKAGED
    assert report["golden_set_sha"] == eval_core.golden_file_sha(path)
    assert eval_core.ANSWER_TEXT_REDACTED_KEY not in report
    # Packaged runs keep today's answer excerpts, verbatim.
    row = report["questions"][0]
    assert row["answer_excerpt"] == _ANSWER
    assert eval_core.ANSWER_TEXT_REDACTED_KEY not in row


def test_packaged_index_summary_shape_is_unchanged_apart_from_additive_keys(
        monkeypatch, tmp_path):
    _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    run_id, _ = er.start_run(None)
    summary = er.list_runs()["runs"][0]
    assert set(summary) == {
        "run_id", "started_at", "finished_at", "status", "run_path",
        "variant", "suite", "golden_set_sha",
    } | _PACKAGED_SUMMARY_ADDITIVE_KEYS
    assert summary["run_id"] == run_id
    assert summary["golden_set_source"] == eval_core.GOLDEN_SOURCE_PACKAGED
    assert summary["question_count"] == 1


def test_a_caller_cannot_relabel_a_packaged_run_as_uploaded(
        monkeypatch, tmp_path):
    """Provenance is COMPUTED from the source actually used: with no goldens
    supplied the packaged file is what ran, so the report says so."""
    _use_packaged(monkeypatch, tmp_path)
    _script_stream(monkeypatch)
    run_id, reason = er.start_run(
        None, golden_source=eval_core.GOLDEN_SOURCE_UPLOADED)
    assert reason is None
    report = er._REPORTS.get(run_id)
    assert report["golden_set_source"] == eval_core.GOLDEN_SOURCE_PACKAGED
    assert eval_core.ANSWER_TEXT_REDACTED_KEY not in report


def test_reset_state_drops_the_short_ttl_registry():
    er._reset_state()
    assert er._JOBS is None and er._REPORTS is None
    assert er._REPORTS_SHORT is None
