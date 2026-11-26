# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-side golden-eval runner (the Evals admin tab backend).

Runs the SAME deterministic golden lane as ``scripts/eval_answers.py``
in-process, modeled on ``feedback_exploration.py`` throughout: a JobStore
active-slot lock + daemon-thread worker + the internal chat-client stream
(``write_memory=False``, no ``_adt_identity``). This runner COMPLEMENTS the
CLI lanes — it never replaces them (complement rule).

Pinned invariants:

* **run-ALL only** — a run streams EVERY golden in the set it was given, in
  order; there is NO subset selection anywhere in this module (the route
  layer 422s subset keys — ``eval_routes.py``).
* **TWO entry points, ONE worker and ONE lock** (ADR-0064 D1/D3) — the
  packaged demo set is LOADED here from the ONE filesystem golden source
  (``eval_core.default_golden_set_path()``); an UPLOADED customer set
  arrives already parsed, as a request-scoped value that lives only in this
  module's locals and the worker thread's. Both run through ``_run_suite``
  under the same ``_ACTIVE_KEY`` claim, so a demo run and a customer run can
  never overlap. Uploaded golden bytes/question text are persisted NOWHERE:
  no JobStore item, no file, no log record.
* **Provenance cannot be confused** — every server report and index summary
  carries ``golden_set_source`` (``"packaged" | "uploaded"``) and
  ``question_count``; an uploaded report's ``golden_set`` is the fixed
  literal ``eval_core.UPLOADED_GOLDEN_LABEL``, never a path and never a
  client-supplied filename.
* **Uploaded runs persist NO answer-derived text** — every row goes through
  ``eval_core.redact_answer_text`` BEFORE it is appended, and the report
  carries ``answer_text_redacted: True`` (ADR-0064 D5). Packaged runs keep
  today's full-excerpt behaviour.
* **Synthetic containment** — every question's session id is
  ``eval-run-<run_id>-q<i>`` (the ``eval-run-`` prefix is wired into
  ``runtime_app._SYNTHETIC_SESSION_PREFIXES`` and
  ``memory_client.AUTOMATED_SESSION_PREFIXES``) and every stream call
  carries ``write_memory=False`` — eval traffic never writes memory or
  trips the learning writers. Identical for uploaded runs: the system never
  trains on a customer's test set.
* **run_path tag** — every persisted report and every index summary carries
  ``run_path == "server"`` (internal-client latencies are incomparable with
  the CLI's through-CloudFront ones — an untagged report would be unusable).
* **Scoring is computed** — rows come from ``eval_core._scored_row`` /
  ``_skipped_row``; the LLM never produces a verdict.

Containment is the documented feedback_exploration tradeoff: the one-run
lock is a NON-ATOMIC check-then-set over ``JobStore`` (fail-soft KV — with
no shared table the bound is per-replica); a stale slot is taken over after
``_STALE_SECONDS``. These bounds limit waste, they are not correctness
guarantees.

Storage (no new tables — both kinds ride the onboarding-sessions table):

* ``JobStore("eval-run", ttl 4 h)`` — the active-slot lock + per-run
  status/progress records (a full run with 300 s/question headroom can pass
  an hour).
* ``JobStore("eval-report", ttl 30 d)`` — persisted reports plus ONE
  bounded ``index`` item (replace-not-accrete: regeneration rewrites the
  newest-50 summary list — ADR-0052 shape). Reports are size-guarded under
  the DynamoDB item limit (progressively drop ``answer_excerpt``s, then the
  ``trend`` block).
* ``JobStore("eval-report", ttl 7 d)`` — the SAME kind, a second writer
  instance for UPLOADED-run reports (the ADR-0062 D6 customer-derived-detail
  horizon; the ``eval_findings._store`` per-tier-TTL precedent). Same kind
  means a deployed read resolves through either instance; without a
  configured table each JobStore keeps its own per-process dict, so
  ``_stored_report`` consults both (JobStore's fail-soft memory-only mode).

Import-pure module (``catalog.py`` discipline): no boto3 at import.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

from . import eval_core

logger = logging.getLogger(__name__)

# ── pinned bounds (do not relax without an ADR) ────────────────────────────
_RUN_TTL_SECONDS = 4 * 3600           # run status/lock records
_REPORT_TTL_SECONDS = 30 * 24 * 3600  # persisted reports + index
_UPLOADED_REPORT_TTL_SECONDS = 7 * 24 * 3600  # uploaded-run reports (ADR-0064)
_STALE_SECONDS = 4 * 3600             # stuck-slot takeover threshold
_QUESTION_CLAMP_SECONDS = 300         # per-question wall clamp
_SUITE_SLACK_SECONDS = 600            # suite-deadline headroom beyond N*300s
_MAX_INDEX_RUNS = 50                  # bounded newest-first run history
_MAX_REPORT_BYTES = 350_000           # size guard under the DDB 400KB limit
_ACTIVE_KEY = "active"                # ONE concurrent run per replica
_INDEX_KEY = "index"                  # the replace-not-accrete summary item

#: Question-count ceiling for ONE run, DERIVED from ``_suite_deadline_seconds``
#: (ADR-0064 D6): at 40 questions the deadline is 40*300+600 = 12,600 s, which
#: still COVERS the 12,000 s of clamped work and stays inside the 4 h
#: ``_RUN_TTL_SECONDS`` status/stale horizon. Past ~n=47 the ``14400-300`` cap
#: bites and the deadline stops covering the work, so a run could silently
#: outlive its own status record. Over the ceiling the start is REFUSED with a
#: distinct reason — never a silent truncation (run-ALL, ADR-0061 D3).
MAX_UPLOADED_QUESTIONS = 40

#: Session-id prefix for eval-run synthetic traffic. Cross-pinned with
#: ``runtime_app._SYNTHETIC_SESSION_PREFIXES`` and
#: ``memory_client.AUTOMATED_SESSION_PREFIXES`` (Wave-1 Task 2).
SESSION_PREFIX = "eval-run-"

#: Distinct start_run rejection reasons — the route maps each to its own
#: 409 detail string (never collapse them).
REASON_NO_GOLDENS = "no-goldens"
REASON_ACTIVE = "active-run"
REASON_TOO_MANY_QUESTIONS = "too-many-questions"

_JOBS = None     # lazy JobStore("eval-run") — keeps module import pure
_REPORTS = None  # lazy JobStore("eval-report")
_REPORTS_SHORT = None  # lazy JobStore("eval-report", 7 d) — uploaded runs


def _jobs():
    """The run status/lock registry (lazy — ``job_store`` transitively
    imports boto3-importing modules). Tests inject
    ``JobStore(..., table=None)`` via the module attribute."""
    global _JOBS
    if _JOBS is None:
        from .job_store import JobStore
        _JOBS = JobStore("eval-run", ttl_seconds=_RUN_TTL_SECONDS)
    return _JOBS


def _reports():
    """The long-TTL report store (reports + the bounded index item)."""
    global _REPORTS
    if _REPORTS is None:
        from .job_store import JobStore
        _REPORTS = JobStore("eval-report", ttl_seconds=_REPORT_TTL_SECONDS)
    return _REPORTS


def _reports_short():
    """The SHORT-TTL report store for UPLOADED runs (7 d — the ADR-0062 D6
    customer-derived-detail horizon). Same ``eval-report`` kind as
    ``_reports()``, so a deployed read resolves through either instance; only
    the per-item ``ttl`` differs (``eval_findings._store`` precedent)."""
    global _REPORTS_SHORT
    if _REPORTS_SHORT is None:
        from .job_store import JobStore
        _REPORTS_SHORT = JobStore("eval-report",
                                  ttl_seconds=_UPLOADED_REPORT_TTL_SECONDS)
    return _REPORTS_SHORT


def _stored_report(run_id: str) -> "dict | None":
    """One persisted report, whichever TTL tier wrote it.

    Deployed, both stores are the same table and the first read resolves.
    Offline/local (``JobStore``'s fail-soft memory-only mode) each instance
    keeps its own per-process dict, so an uploaded run's report is only in the
    short-TTL instance — consulted only when it EXISTS, so a read never
    constructs a store the process never wrote to.
    """
    report = _reports().get(run_id)
    if isinstance(report, dict):
        return report
    if _REPORTS_SHORT is None:
        return None
    report = _REPORTS_SHORT.get(run_id)
    return report if isinstance(report, dict) else None


def _reset_state() -> None:
    """Test seam: drop all registries (fresh in-memory stores per test)."""
    global _JOBS, _REPORTS, _REPORTS_SHORT
    _JOBS = None
    _REPORTS = None
    _REPORTS_SHORT = None


def _golden_set_path() -> str:
    """The packaged demo golden set — the server's ONE FILESYSTEM golden
    source (ADR-0064 D4). A customer golden set reaches the runner as parsed
    QUESTIONS via ``start_run(goldens=...)`` and never acquires a path. The
    path literal lives in ``eval_core`` (which points into ``agents/demo/`` —
    the sanctioned demo home); no demo literal enters this module."""
    return eval_core.default_golden_set_path()


def _packaged_goldens() -> tuple:
    """``(questions, sha, label)`` for the packaged demo set.

    The ONLY place this module reads a golden set from disk, and both reads
    take the argument-less ``_golden_set_path()`` — a path no request can
    influence cannot become a second source.
    """
    return (eval_core.load_golden_set(_golden_set_path()),
            eval_core.golden_file_sha(_golden_set_path()),
            _golden_set_path())


def _claim_active_slot(run_id: str, now: float) -> bool:
    """Claim the ONE per-replica active slot, shared by BOTH entry points so
    a demo run and a customer run can never overlap (ADR-0064 D3).

    ATOMIC via ``JobStore.claim_slot`` (a DynamoDB conditional write when a
    table is configured, a locked check-then-set in memory otherwise) with
    stale takeover past ``_STALE_SECONDS``. The matching release in
    ``_run_suite`` is exact-owner: a stale run finishing late can never
    release a newer run's claim.
    """
    return _jobs().claim_slot(
        _ACTIVE_KEY,
        {"status": "running", "run_id": run_id, "started_at": now},
        stale_seconds=_STALE_SECONDS, now=now,
    )


def start_run(variant: "str | None" = None, *, goldens: "list | None" = None,
              golden_sha: "str | None" = None,
              golden_source: str = eval_core.GOLDEN_SOURCE_PACKAGED,
              ) -> "tuple[str | None, str | None]":
    """Start a full-suite eval run; returns ``(run_id, None)`` on success or
    ``(None, reason)`` with a distinct rejection reason.

    With ``goldens is None`` the behaviour is the packaged path, unchanged:
    load the demo set from the ONE filesystem golden source and stamp
    ``golden_set_source == "packaged"`` (a caller cannot relabel a run over
    bytes it did not supply). With ``goldens`` supplied — an UPLOADED customer
    set, already validated by ``eval_core.parse_golden_set`` and hashed by
    ``eval_core.golden_bytes_sha`` — no file is read, ``golden_sha`` is used as
    given, ``golden_set`` becomes ``eval_core.UPLOADED_GOLDEN_LABEL``, and the
    rows are answer-text redacted (ADR-0064). Those questions live ONLY in
    this call's locals and the worker thread's; nothing is persisted or logged.

    Gate order: goldens present → question-count ceiling → active-slot claim
    → daemon worker thread.
    """
    import uuid
    golden_label = eval_core.UPLOADED_GOLDEN_LABEL
    if goldens is None:
        golden_source = eval_core.GOLDEN_SOURCE_PACKAGED
        try:
            goldens, golden_sha, golden_label = _packaged_goldens()
        except (OSError, ValueError) as exc:
            logger.warning("eval_run_no_goldens: category=%s",
                           type(exc).__name__)
            return None, REASON_NO_GOLDENS
    if not goldens:
        return None, REASON_NO_GOLDENS
    if len(goldens) > MAX_UPLOADED_QUESTIONS:
        logger.warning("eval_run_refused: reason=%s question_count=%d",
                       REASON_TOO_MANY_QUESTIONS, len(goldens))
        return None, REASON_TOO_MANY_QUESTIONS
    now = time.time()
    run_id = uuid.uuid4().hex[:16]
    if not _claim_active_slot(run_id, now):
        return None, REASON_ACTIVE
    jobs = _jobs()
    jobs[run_id] = {"status": "running", "started_at": now,
                    "variant": variant or None,
                    "progress": {"current": 0, "total": len(goldens)}}
    threading.Thread(target=_run_suite,
                     args=(run_id, variant, goldens, golden_label,
                           golden_sha, golden_source),
                     daemon=True).start()
    return run_id, None


def _default_stream(prompt: str, session_id: str):
    """One question's event stream via the internal chat client (the
    ``feedback_exploration._default_stream`` branch structure): AgentCore
    runtime when configured, else the local integrity-aware client.
    ``write_memory=False`` always (synthetic flow) and NO
    ``_adt_identity``/extra payload is supplied."""
    from .config import get_config
    if get_config().has_runtime:
        from .chat_client import AgentCoreChatClient
        return AgentCoreChatClient().stream(
            prompt=prompt, session_id=session_id,
            user_id="eval-run", write_memory=False)
    from .chat_client import LocalChatClient
    return LocalChatClient().stream(
        prompt=prompt, session_id=session_id,
        user_id="eval-run", write_memory=False)


# Module-level seam (the ``rederive`` idiom): tests script the stream by
# assigning a fake; production uses the default above.
_stream_factory = _default_stream


class SuiteDeadlineExceeded(TimeoutError):
    """The whole suite ran past its honest deadline — the run FAILS."""


def _suite_deadline_seconds(question_count: int) -> float:
    """Suite deadline: per-question headroom plus slack, always resolving
    before the 4 h run-record TTL/stale-takeover horizon."""
    return min(question_count * _QUESTION_CLAMP_SECONDS + _SUITE_SLACK_SECONDS,
               _RUN_TTL_SECONDS - _QUESTION_CLAMP_SECONDS)


def _ask_question(question_text: str, session_id: str,
                  deadline: float) -> dict:
    """Stream ONE golden through the seam and adapt its events into the
    ``{answer, summary, errors, done, elapsed_seconds}`` result dict
    ``eval_core._scored_row`` consumes.

    A question running past ``_QUESTION_CLAMP_SECONDS`` stops consuming and
    returns ``done=False`` (scored as an honest transport failure — the
    turn was never delivered). The suite-wide ``deadline``
    (``time.monotonic()`` value) raises instead — the whole RUN fails
    honestly past it.
    """
    from .events import DoneEvent, ErrorEvent, MessageEvent, MetadataEvent

    started = time.monotonic()
    answer_parts: list[str] = []
    errors: list[str] = []
    summary = None
    done = False
    stream = _stream_factory(question_text, session_id)
    try:
        for event in stream:
            if time.monotonic() >= deadline:
                raise SuiteDeadlineExceeded(
                    "eval suite exceeded its deadline")
            if time.monotonic() - started > _QUESTION_CLAMP_SECONDS:
                errors.append(
                    f"question exceeded {_QUESTION_CLAMP_SECONDS}s wall clamp")
                break
            if isinstance(event, MessageEvent):
                answer_parts.append(event.content)
            elif isinstance(event, ErrorEvent):
                errors.append(event.message)
            elif isinstance(event, MetadataEvent):
                block = (event.data or {}).get("intelligence_summary")
                if isinstance(block, dict):
                    summary = block
            elif isinstance(event, DoneEvent):
                done = True
                if event.text and not answer_parts:
                    answer_parts.append(event.text)
    finally:
        getattr(stream, "close", lambda: None)()
    return {"answer": "".join(answer_parts), "summary": summary,
            "errors": errors, "done": done,
            "elapsed_seconds": round(time.monotonic() - started, 2)}


def _shrink_report(report: dict) -> dict:
    """Size-guard the report under ``_MAX_REPORT_BYTES`` before persisting
    (DynamoDB 400 KB item limit): progressively drop ``answer_excerpt``s,
    then the informational ``trend`` block. Never drops verdicts/failures."""
    def _size(candidate: dict) -> int:
        return len(json.dumps(candidate, default=str).encode("utf-8"))

    if _size(report) <= _MAX_REPORT_BYTES:
        return report
    shrunk = dict(report)
    shrunk["questions"] = [
        {**row, "answer_excerpt": ""} for row in report.get("questions") or []]
    shrunk["answer_excerpts_dropped"] = True
    if _size(shrunk) <= _MAX_REPORT_BYTES:
        return shrunk
    if "trend" in shrunk:
        shrunk = {k: v for k, v in shrunk.items() if k != "trend"}
        shrunk["trend_dropped"] = True
    if _size(shrunk) > _MAX_REPORT_BYTES:
        # Honest last resort: the record must fit — keep verdict rows only.
        logger.warning("eval_report_oversized_after_shrink: run=%s",
                       report.get("run_id"))
    return shrunk


def _run_summary(run_id: str, record: dict, report: dict) -> dict:
    """ONE bounded index entry (the pinned run-summary shape).

    ``golden_set_source`` + ``question_count`` are ADDITIVE (ADR-0064 D3): the
    history table badges a customer run without fetching the report, so an
    uploaded run can never be read as a demo run. Neither carries any golden
    content — the source is a fixed label and the count is an integer.
    """
    return {
        "run_id": run_id,
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "status": record.get("status"),
        "run_path": report.get("run_path"),
        "variant": report.get("variant"),
        "suite": {key: (report.get("suite") or {}).get(key)
                  for key in ("pass", "fail", "skipped", "total")},
        "golden_set_sha": report.get("golden_set_sha"),
        "golden_set_source": report.get("golden_set_source"),
        "question_count": (report.get("suite") or {}).get("total"),
    }


def _update_index(summary: dict) -> None:
    """Replace-not-accrete: rewrite the newest-50 summary list (regeneration
    replaces the prior generated list — ADR-0052 shape)."""
    reports = _reports()
    existing = reports.get(_INDEX_KEY)
    runs = list((existing or {}).get("runs") or []) \
        if isinstance(existing, dict) else []
    runs = [r for r in runs if isinstance(r, dict)
            and r.get("run_id") != summary.get("run_id")]
    runs.insert(0, summary)
    reports[_INDEX_KEY] = {"runs": runs[:_MAX_INDEX_RUNS]}


def _persist_run_results(run_id: str, variant: "str | None",
                         started_at: float, rows: list,
                         golden_label: str, golden_sha: "str | None",
                         golden_source: str) -> None:
    """Success epilogue: build/shrink/persist the ``run_path="server"``
    report, mark the run complete, update the bounded index.

    ``golden_label`` is the packaged path for a demo run and the fixed
    ``eval_core.UPLOADED_GOLDEN_LABEL`` for an uploaded one — never a
    filename. ``golden_sha`` is the sha computed by the CALLER over the exact
    bytes it had, so ``golden_file_sha`` is never reached on an upload path.
    An uploaded report is marked answer-text redacted and written through the
    7-day store; reads resolve either tier (``_stored_report``).
    """
    uploaded = golden_source == eval_core.GOLDEN_SOURCE_UPLOADED
    report = eval_core.build_eval_report(
        base_url="internal",
        namespace=SimpleNamespace(run_id=run_id,
                                  prefix=f"{SESSION_PREFIX}{run_id}"),
        golden_set=golden_label,
        strict=False,
        rows=rows,
        run_path="server",
        variant=variant or "",
        golden_set_sha=golden_sha,
        golden_set_source=golden_source)
    if uploaded:
        report[eval_core.ANSWER_TEXT_REDACTED_KEY] = True
    report = _shrink_report(report)
    (_reports_short() if uploaded else _reports())[run_id] = report
    total = len(rows)
    record = {"status": "complete", "started_at": started_at,
              "finished_at": time.time(), "variant": variant or None,
              "progress": {"current": total, "total": total}}
    _jobs()[run_id] = record
    _update_index(_run_summary(run_id, record, report))


def _connected_types() -> "set[str] | None":
    """System types this deployment has ONBOARDED (the catalog the
    ``/api/systems`` route reads); ``None`` when the catalog cannot be read,
    so a ``requires_connected_types`` golden runs rather than skips."""
    try:
        from .catalog import catalog
        return {s.get("system_type") or "" for s in catalog.get_all()
                if s.get("status") == "onboarded"}
    except Exception:  # noqa: BLE001 - unknown state never skips a golden
        logger.info("eval: catalog unreadable; connected-type gates run")
        return None


def _story_answer_key() -> None:
    """The server lane has no story answer key: this module stays free of the
    demo package (ADR-0044), so a ``story_fields`` golden reports ``skipped``
    here with a named reason (never a bare ``{name}`` sent to the model) and
    runs on the CLI lane, which reads the steward story route."""
    return None


def _question_row(run_id: str, index: int, question: dict, deadline: float,
                  redact: bool) -> dict:
    """Score ONE golden into its report row.

    When ``redact`` (an UPLOADED set), the row passes through
    ``eval_core.redact_answer_text`` HERE, before the caller appends it — the
    unredacted row exists only as this frame's local, so no answer-derived
    text can reach the ``rows`` list, the report, or the store (ADR-0064 D5).
    """
    skip_reason = eval_core.should_skip(question,
                                        connected_types=_connected_types())
    if not skip_reason:
        question, skip_reason = eval_core.fill_story_fields(question, _story_answer_key)
    if skip_reason:
        row = eval_core._skipped_row(question, skip_reason)
        return eval_core.redact_answer_text(row) if redact else row
    if time.monotonic() >= deadline:
        raise SuiteDeadlineExceeded("eval suite exceeded its deadline")
    session_id = f"{SESSION_PREFIX}{run_id}-q{index}"
    result = _ask_question(question.get("text", ""), session_id, deadline)
    row = eval_core._scored_row(question, session_id, result)
    if not redact:
        return row
    return eval_core.redact_answer_text(
        row, error_count=len(result.get("errors") or []))


def _run_suite(run_id: str, variant: "str | None", goldens: list,
               golden_label: str, golden_sha: "str | None" = None,
               golden_source: str = eval_core.GOLDEN_SOURCE_PACKAGED) -> None:
    """Worker: stream EVERY golden in order, score deterministically, build
    and persist the ``run_path="server"`` report, update the bounded index.

    ONE worker for BOTH entry points (ADR-0064 D3). ``goldens`` is the ONLY
    place an uploaded customer set lives beyond the request — this thread's
    locals — and it is never written down or logged.

    Progress is written after each question so a status poll (any replica —
    JobStore) sees ``{current, total}``. Errors are stored with the
    exception TYPE only (never message/stack); the single per-replica slot
    is ALWAYS released; never re-raises.
    """
    jobs = _jobs()
    total = len(goldens)
    started_at = time.time()
    redact = golden_source == eval_core.GOLDEN_SOURCE_UPLOADED
    try:
        deadline = time.monotonic() + _suite_deadline_seconds(total)
        rows: list[dict] = []
        for i, question in enumerate(goldens, start=1):
            rows.append(_question_row(run_id, i, question, deadline, redact))
            jobs[run_id] = {"status": "running", "started_at": started_at,
                            "variant": variant or None,
                            "progress": {"current": i, "total": total}}
        _persist_run_results(run_id, variant, started_at, rows, golden_label,
                             golden_sha, golden_source)
    except Exception as exc:  # noqa: BLE001 - stored honestly, never re-raised
        logger.warning("eval_run_failed: id=%s category=%s",
                       run_id, type(exc).__name__)
        jobs[run_id] = {"status": "failed", "started_at": started_at,
                        "finished_at": time.time(), "variant": variant or None,
                        "error": f"eval run failed: {type(exc).__name__}"}
    finally:
        released = jobs.release_slot(
            _ACTIVE_KEY, run_id,
            {"status": "idle", "run_id": run_id, "finished_at": time.time()})
        if not released:
            logger.warning("eval_run_slot_not_released: id=%s reason=%s",
                           run_id, "taken over by a newer run")


# ── read-side helpers ───────────────────────────────────────────────────────

def get_run(run_id: str) -> "dict | None":
    """Status/report for ONE run — cross-replica via JobStore.

    Returns ``{status, progress?, error?, report?}``: progress while
    running, the full persisted report once complete (also resolving a
    completed run whose short-TTL status record already expired), or
    ``None`` for an unknown run id.
    """
    if not isinstance(run_id, str) or run_id in (_ACTIVE_KEY, _INDEX_KEY):
        return None
    record = _jobs().get(run_id)
    if isinstance(record, dict):
        out = {"status": record.get("status")}
        if record.get("progress") is not None:
            out["progress"] = record["progress"]
        if record.get("error"):
            out["error"] = record["error"]
        if record.get("status") == "complete":
            report = _stored_report(run_id)
            if isinstance(report, dict):
                out["report"] = report
        return out
    report = _stored_report(run_id)
    if isinstance(report, dict):
        return {"status": "complete", "report": report}
    return None


def list_runs() -> dict:
    """The bounded newest-first run history (the index item)."""
    index = _reports().get(_INDEX_KEY)
    runs = list((index or {}).get("runs") or []) \
        if isinstance(index, dict) else []
    return {"runs": runs[:_MAX_INDEX_RUNS]}
