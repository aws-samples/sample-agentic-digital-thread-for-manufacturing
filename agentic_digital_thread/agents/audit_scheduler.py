# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounded triage annotation — ONE entry, a registration-only flag, and a
global UTC-day budget claimed atomically (ADR-0062 D11).

This is a BOUNDED TRIAGE ANNOTATOR, never "deep-context auditing" (D12): its
inputs are flagged review-queue items — thumbs-down feedback and the newest
persisted reports' failed rows — not the curated evidence documents
``scripts/audit_answer.py`` consumes.

Shape (do not reshape without the ADR):

* **ONE public entry** — :func:`request_audit` takes ``trigger`` and NOTHING
  else (pinned by signature introspection). ``trigger`` is PROVENANCE ONLY: it
  is stored on the run record and drives zero behavioral branches, so a manual
  and a scheduled run over the same candidates produce identical outcomes apart
  from run id, timestamps and the trigger label. Candidate selection, the cap,
  the model call, validation, persistence and telemetry live ONLY in the single
  worker :func:`_run_audit`.
* **The budget is claimed BEFORE any model call, atomically, and is GLOBAL
  across manual and scheduled runs combined** — ``job_store``'s conditional
  claim primitive (:func:`~agents.job_store.claim_budget_slots_detailed`)
  against the ``audit-budget`` bucket for the current UTC day. The entry claims
  the ADMISSION slot (so a refusal is a synchronous, distinct route status) and
  the worker claims ONE MORE slot immediately before every further converse
  call. Exactly one slot is committed per model call, so the daily cap holds
  under concurrency: two racing runs cannot jointly exceed it.
* **Cloud-mode claim failure FAILS CLOSED** — ``BudgetUnavailable`` means the
  guarantee is unknown, so nothing is spent: the run is recorded
  ``skipped_budget`` with zero converse calls (and the worker stops claiming
  rather than continuing).
* **The cap is a pinned constant read INSIDE the worker** —
  :data:`DAILY_AUDIT_CAP` bounds both the daily budget and one run's candidate
  selection. Neither entry point may pass a cap, a count, a model or a source.
* **No memory, no learning, no chat** — the worker reaches Bedrock only through
  the ``audit_core`` converse seam. This module imports no chat, session or
  memory module at any depth (pinned by an import-graph walk; the
  ``audit_learning.py --strict`` lane does NOT prove memory absence).
* **Findings ride the Wave-1 ledger** — :func:`~agents.eval_findings.replace_findings`
  is the ONLY findings writer used here (replace-not-accrete per source scope;
  steward rows survive). A completed run fires the REAL usage ledger with an
  ``("audit-run", <run_id>)`` ref — the slot ``eval_findings`` already reads
  back, so this writer arrives with its read path in place (ADR-0052).
* **Proposals are NEVER filed automatically** — mapping values stay in the 7 d
  customer-derived detail row until a steward files them through the
  findings-ledger route.

Storage: ``JobStore("audit-run", ttl 7 d)`` carries the active-slot lock, one
record per run, and ONE bounded newest-first ``index`` item (the
replace-not-accrete summary shape ``eval_runs`` uses). No new table.

Import-pure module (``catalog.py`` discipline): no boto3 at import; every
cross-module dependency beyond ``audit_core`` is imported lazily inside the
function that needs it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from . import audit_core

logger = logging.getLogger(__name__)

# ── pinned bounds (ADR-0062 D11 — do not relax without the ADR) ─────────────
#: The GLOBAL UTC-day cap: one slot per model call, manual + scheduled
#: COMBINED, and the per-run candidate ceiling. Read INSIDE the worker only.
DAILY_AUDIT_CAP = 10
#: Budget namespace for the conditional claim primitive.
BUDGET_KIND = "audit-budget"
#: Explicit ``maxTokens`` on every converse call (the MaxTokens invariant).
MAX_TOKENS = 2000
RUN_TTL_SECONDS = 7 * 24 * 3600
#: Bounded newest-first run history on the ONE index item.
MAX_INDEX_RUNS = 20
#: Runs reported by :func:`audit_status`.
MAX_STATUS_RUNS = 10
#: A stuck active slot is taken over after this long (the ``eval_runs``
#: tradeoff: this bounds waste, it is not a correctness guarantee — the BUDGET
#: is what bounds spend).
STALE_SECONDS = 2 * 3600

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"
#: The only accepted triggers. PROVENANCE ONLY — no behavioral branch.
TRIGGERS = (TRIGGER_MANUAL, TRIGGER_SCHEDULED)

#: Registration-only flag (default OFF). It gates whether the nightly job is
#: REGISTERED — never whether the one-shot entry works.
SCHEDULED_FLAG = "NIGHTLY_AUDIT_ENABLED"
SCHEDULED_JOB_ID = "nightly-audit"
SCHEDULED_INTERVAL_SECONDS = 24 * 3600
_TRUTHY = ("1", "true", "yes", "on")

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_SKIPPED_BUDGET = "skipped_budget"

#: DISTINCT refusal reasons — the route maps each to its own status/detail and
#: they must never be collapsed: one says a run is in flight, one says the
#: budget was consulted and is exhausted, one says it could not be consulted.
REASON_ACTIVE = "active-run"
REASON_BUDGET_EXHAUSTED = "budget-exhausted"
REASON_BUDGET_UNAVAILABLE = "budget-unavailable"

#: Every persisted report/finding from this surface is server-side traffic.
RUN_PATH = "server"

_ACTIVE_KEY = "active"
_INDEX_KEY = "index"
_SCOPE_SALT = "adt:bounded-triage-annotation"

_JOBS = None  # lazy JobStore("audit-run")


def _jobs():
    """The run/lock registry (lazy — ``job_store`` transitively imports
    boto3-importing modules). Tests inject ``JobStore(..., table=None)``."""
    global _JOBS
    if _JOBS is None:
        from .job_store import JobStore
        _JOBS = JobStore("audit-run", ttl_seconds=RUN_TTL_SECONDS)
    return _JOBS


def _reset_state() -> None:
    """Test seam: drop the registry (a fresh in-memory store per test)."""
    global _JOBS
    _JOBS = None


def scheduled_enabled() -> bool:
    """Whether SCHEDULED runs are enabled in this environment (default OFF).

    ONE home for the flag: ``server.register_nightly_audit_job`` gates
    REGISTRATION on it and :func:`audit_status` reports it. It never gates the
    one-shot entry — that route IS the live test path (D11).
    """
    return os.environ.get(SCHEDULED_FLAG, "").strip().lower() in _TRUTHY


def _iso(moment: float | None = None) -> str:
    return datetime.fromtimestamp(moment if moment is not None else time.time(),
                                  tz=timezone.utc).isoformat()


# ── the ONE public entry ────────────────────────────────────────────────────

def request_audit(trigger: str) -> "tuple[str | None, str | None]":
    """Request ONE bounded triage-annotation run: ``(run_id, None)`` or
    ``(None, reason)``.

    The signature is the contract: ``trigger`` and nothing else — no count, no
    cap, no model, no source (pinned by introspection). Gate order, all before
    any model call: active-slot claim → ATOMIC admission-slot claim on the
    global UTC-day budget → daemon worker.

    Refusals are distinct: :data:`REASON_ACTIVE` (a run is in flight),
    :data:`REASON_BUDGET_EXHAUSTED` (consulted, no room) and
    :data:`REASON_BUDGET_UNAVAILABLE` (cloud-mode claim failure — FAIL CLOSED).
    Both budget refusals record a ``skipped_budget`` run, so a refusal to spend
    leaves evidence instead of vanishing.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}")
    run_id = uuid.uuid4().hex[:16]
    if not _claim_active(run_id):
        return None, REASON_ACTIVE
    claim, reason = _claim_admission()
    if claim is None:
        _release_active(run_id)
        _finish_run(run_id, trigger, STATUS_SKIPPED_BUDGET, {"audited": 0},
                    slots=0, day_key=_budget_day())
        return None, reason
    _start_run(run_id, trigger, claim)
    threading.Thread(target=_run_audit, args=(run_id, trigger, claim),
                     daemon=True).start()
    return run_id, None


def _budget_day() -> str:
    """Today's UTC budget bucket (``job_store`` owns the key format)."""
    from .job_store import utc_day_key
    return utc_day_key()


def _claim_admission():
    """``(BudgetClaim, None)`` for one granted admission slot, else
    ``(None, reason)``. The claim is ATOMIC and precedes every model call."""
    from .job_store import BudgetUnavailable, claim_budget_slots_detailed
    try:
        claim = claim_budget_slots_detailed(BUDGET_KIND, _budget_day(), 1,
                                            DAILY_AUDIT_CAP)
    except BudgetUnavailable as exc:
        logger.warning("audit_budget_unavailable: failing closed (%s)",
                       type(exc).__name__)
        return None, REASON_BUDGET_UNAVAILABLE
    if claim.granted < 1:
        logger.info("audit_budget_exhausted: day=%s cap=%d", claim.day_key,
                    DAILY_AUDIT_CAP)
        return None, REASON_BUDGET_EXHAUSTED
    return claim, None


def _claim_one_more(day_key: str) -> int:
    """ONE more slot immediately before a further converse call (0 = stop).

    An unavailable budget is a STOP, not a retry: the cap is what bounds spend,
    so an unknown guarantee must not buy a model call.
    """
    from .job_store import BudgetUnavailable, claim_budget_slots
    try:
        return claim_budget_slots(BUDGET_KIND, day_key, 1, DAILY_AUDIT_CAP)
    except BudgetUnavailable as exc:
        logger.warning("audit_budget_unavailable mid-run: stopping (%s)",
                       type(exc).__name__)
        return 0


# ── active-slot lock (bounds waste; the budget bounds spend) ────────────────

def _claim_active(run_id: str) -> bool:
    """Atomic active-slot claim with stale takeover (``JobStore.claim_slot``;
    with no shared table the guarantee is per-replica)."""
    now = time.time()
    return _jobs().claim_slot(
        _ACTIVE_KEY,
        {"status": STATUS_RUNNING, "run_id": run_id, "started_at": now},
        stale_seconds=STALE_SECONDS, now=now,
    )


def _release_active(run_id: str) -> None:
    """Release the slot iff this run still holds it (never steal a takeover)."""
    _jobs().release_slot(_ACTIVE_KEY, run_id,
                         {"status": "idle", "run_id": run_id,
                          "finished_at": time.time()})


# ── run records + the ONE bounded index item ───────────────────────────────

def _start_run(run_id: str, trigger: str, claim) -> dict:
    """Record the run as ``running``. ``trigger`` is stored, never branched on."""
    record = {"run_id": run_id, "trigger": trigger, "status": STATUS_RUNNING,
              "started_at": _iso(), "finished_at": None, "counts": {},
              "day_key": claim.day_key, "slots_claimed": claim.granted,
              "budget_durable": claim.durable, "run_path": RUN_PATH}
    _jobs()[run_id] = record
    _update_index(record)
    return record


def _finish_run(run_id: str, trigger: str, status: str, counts: dict, *,
                slots: int, day_key: str, usage=None, error: str = "",
                durable=None) -> dict:
    """Record a terminal run state. ``usage`` is measured tokens or ABSENT —
    never zero-as-unknown, never dollars (ADR-0053)."""
    existing = _jobs().get(run_id)
    record = dict(existing) if isinstance(existing, dict) else {
        "run_id": run_id, "trigger": trigger, "started_at": _iso(),
        "run_path": RUN_PATH}
    record.update({"status": status, "counts": dict(counts),
                   "finished_at": _iso(), "slots_claimed": slots,
                   "day_key": day_key})
    if durable is not None:
        record["budget_durable"] = bool(durable)
    if usage is not None:
        record["usage"] = dict(usage)
    if error:
        record["error"] = error
    _jobs()[run_id] = record
    _update_index(record)
    return record


def _summary(record: dict) -> dict:
    """ONE bounded index entry — the shape the status route publishes."""
    keys = ("run_id", "trigger", "status", "counts", "started_at",
            "finished_at", "day_key", "slots_claimed", "budget_durable",
            "usage", "error")
    return {key: record[key] for key in keys if key in record}


def _update_index(record: dict) -> None:
    """Replace-not-accrete: the entry for this run id is REPLACED and the list
    stays bounded newest-first."""
    jobs = _jobs()
    index = jobs.get(_INDEX_KEY)
    entries = list((index or {}).get("runs") or []) if isinstance(index, dict) \
        else []
    kept = [entry for entry in entries
            if isinstance(entry, dict) and entry.get("run_id")
            != record.get("run_id")]
    jobs[_INDEX_KEY] = {"runs": ([_summary(record)] + kept)[:MAX_INDEX_RUNS]}


def get_run(run_id: str) -> "dict | None":
    """One run record for polling, or ``None``. Writes nothing."""
    record = _jobs().get(run_id)
    return _summary(record) if isinstance(record, dict) else None


def _recent_runs(limit: int) -> list:
    """The bounded newest-first run summaries (ONE read, zero writes)."""
    index = _jobs().get(_INDEX_KEY)
    entries = list((index or {}).get("runs") or []) if isinstance(index, dict) \
        else []
    return [entry for entry in entries if isinstance(entry, dict)][:limit]


def _budget_block(runs: list) -> dict:
    """Today's GLOBAL budget consumption, derived from the persisted runs.

    ``durable`` is reported ONLY when a run recorded which mode its claim
    committed in — an unknown guarantee is absent, never asserted.
    """
    day_key = _budget_day()
    today = [run for run in runs if run.get("day_key") == day_key]
    claimed = 0
    for run in today:
        try:
            claimed += int(run.get("slots_claimed") or 0)
        except (TypeError, ValueError):
            continue
    block = {"day_key": day_key, "claimed": claimed, "cap": DAILY_AUDIT_CAP}
    for run in today:
        if isinstance(run.get("budget_durable"), bool):
            block["durable"] = run["budget_durable"]
            break
    return block


def audit_status(limit: int = MAX_STATUS_RUNS) -> dict:
    """``{scheduled_enabled, runs, budget}`` — COMPUTED ON READ, zero writes.

    The ``telemetry`` block the panel also renders comes from the ADR-0052
    ledger read path (``eval_findings.writer_telemetry``) and is composed in
    the route — it is not re-implemented here.
    """
    runs = _recent_runs(max(1, min(int(limit or MAX_STATUS_RUNS),
                                   MAX_INDEX_RUNS)))
    return {"scheduled_enabled": scheduled_enabled(), "runs": runs,
            "budget": _budget_block(runs)}


# ── candidates: FLAGGED ONLY, computed on read ─────────────────────────────

def _candidates() -> list:
    """Undecided FLAGGED review-queue items, newest/highest-band first.

    The computed queue (``eval_queue.compute_queue`` — no writes) is already
    the union of "thumbs-down feedback" and "the newest reports' failed rows",
    so there is no second selection contract here. Filtered to genuinely
    flagged, still-open, still-resolvable items: a decided item is the
    steward's, an expired source has nothing to annotate, and a non-negative
    feedback row was never flagged.
    """
    from . import eval_queue
    items = []
    for item in (eval_queue.compute_queue().get("items") or []):
        if not isinstance(item, dict) or item.get("decision") \
                or item.get("source_expired"):
            continue
        if item.get("source") == eval_queue.SOURCE_FEEDBACK \
                and item.get("sentiment") != "negative":
            continue
        items.append(item)
    return items


def _question_id(item: dict) -> str:
    """The bundle's question id: the golden question id for a report failure,
    else the OPAQUE queue ref sha (a feedback row has no question id)."""
    from . import eval_queue
    if item.get("source") == eval_queue.SOURCE_GOLDEN_FAILURE:
        return str(item.get("question_id") or item.get("ref_sha") or "")
    return str(item.get("ref_sha") or "")


def _evidence_for(item: dict) -> list:
    """ONE bounded context document per candidate.

    Its ``id`` enters the bundle's known-id set, so the model can cite it and
    ``validate_findings`` can check the citation. The text is the flagged
    turn's own — it goes to the model and into the 7 d customer-derived detail
    scope, never into a durable row.
    """
    doc = {"id": f"{item.get('ref_sha')}-context",
           "source": item.get("source"),
           "occurred_at": item.get("occurred_at")}
    for key in ("question", "user_comment", "sentiment", "cited_systems",
                "run_id", "question_id"):
        if item.get(key):
            doc[key] = item[key]
    return [doc]


def _verdict(item: dict) -> str:
    """The bundle's verdict field: a report failure IS a fail; a feedback row
    carries its sentiment. Never invented — the queue only holds flagged items."""
    from . import eval_queue
    if item.get("source") == eval_queue.SOURCE_GOLDEN_FAILURE:
        return "fail"
    return str(item.get("sentiment") or item.get("source") or "")


def _bundle_for(item: dict) -> dict:
    """The claim/evidence bundle for one candidate — built by ``audit_core``
    (never re-implemented). No error-label classifier is injected: it lives in
    ``scripts/``, which the container image does not ship, and error labels
    annotate rather than gate."""
    row = {"id": _question_id(item), "session_id": item.get("ref_sha"),
           "verdict": _verdict(item),
           "failures": list(item.get("failures") or []),
           "err_labels": [], "honesty": None,
           "answer_excerpt": item.get("answer_excerpt") or ""}
    return audit_core.build_bundle(row, _evidence_for(item))


# ── persistence scope: one per originating source ──────────────────────────

def _scope_sha(run_id) -> str:
    """The findings scope this candidate's rows are replaced within.

    Per originating report run (and one for feedback), so re-annotating
    REPLACES that scope's prior generated rows (replace-not-accrete, ADR-0052)
    without touching another source's — and steward rows, keyed off the
    deterministic finding id, survive.
    """
    return hashlib.sha256(
        f"{_SCOPE_SALT}:{run_id or 'feedback'}".encode("utf-8")).hexdigest()


def _scope_of(item: dict) -> "tuple[str, str | None]":
    """``(scope sha, run_id or None)`` for one candidate."""
    from . import eval_queue
    run_id = item.get("run_id") \
        if item.get("source") == eval_queue.SOURCE_GOLDEN_FAILURE else None
    return _scope_sha(run_id), (str(run_id) if run_id else None)


def _local_id(item: dict, taxonomy: str, index: int) -> str:
    """A local finding id that is DETERMINISTIC in (candidate, taxonomy,
    position) — so a re-annotation keeps the same finding id and a steward
    annotation stays bound to the same finding rather than to a queue
    position."""
    return f"{item.get('ref_sha')}-{taxonomy}-{index:02d}"


# ── the SOLE worker: model calls, validation, persistence, telemetry ────────

def _model_id(region: "str | None") -> str:
    """The configured annotator model — the AGENT role from the ONE configured
    -models source (honours the live model swap, ADR-0055). Never a literal."""
    from .model_config import get_configured_models
    return get_configured_models(region).get("agent") or ""


def _annotate_candidate(item: dict, model_id: str,
                        region: "str | None") -> tuple:
    """ONE converse call for ONE flagged candidate.

    Returns ``(valid findings, measured usage pair or None, discarded count)``.
    The model proposes; ``audit_core.validate_findings`` decides (deterministic,
    citations re-checked against the bundle's own ids); invalid findings are
    DISCARDED and counted, never repaired.
    """
    bundle = _bundle_for(item)
    raw, usage = audit_core.audit_bundle(bundle, model_id=model_id,
                                         max_tokens=MAX_TOKENS, region=region)
    valid, discarded = audit_core.validate_findings(
        raw, audit_core.collect_known_ids(bundle, []))
    for index, finding in enumerate(valid, start=1):
        finding["id"] = _local_id(item, finding["taxonomy"], index)
    return valid, usage, discarded


def _audit_selected(selected: list, day_key: str, granted: int,
                    counts: dict) -> tuple:
    """Annotate the selected candidates under the atomic budget.

    ONE slot is committed per converse call: the admission slot covers the
    first candidate and every later one claims its own slot FIRST, so the
    global daily cap holds even with a concurrent run. A refused or unavailable
    claim stops the loop (fail closed) and the remainder is counted
    ``skipped_budget``.
    """
    region = os.environ.get("AWS_REGION", "").strip() or None
    model_id = _model_id(region)
    groups: dict = {}
    measured: list = []
    for index, item in enumerate(selected):
        if index >= granted:
            if _claim_one_more(day_key) < 1:
                counts["skipped_budget"] = len(selected) - index
                break
            granted += 1
        try:
            findings, usage, discarded = _annotate_candidate(item, model_id,
                                                             region)
        except Exception as exc:  # noqa: BLE001 - every model failure class
            logger.warning("audit_model_error: category=%s", type(exc).__name__)
            counts["model_errors"] = counts.get("model_errors", 0) + 1
            continue
        counts["audited"] += 1
        counts["discarded_invalid"] = counts.get("discarded_invalid", 0) \
            + discarded
        if usage is not None:
            measured.append(usage)
        groups.setdefault(_scope_of(item), []).extend(findings)
    return groups, measured, granted


def _persist(groups: dict, usage, counts: dict) -> None:
    """Write every scope's findings through the ONE ledger writer.

    ``replace_findings`` is the sole findings writer on this path: per scope it
    DELETES the prior generated rows (durable + 7 d detail), writes minimized
    rows, and leaves steward annotations byte-identical. Nothing here files a
    proposal — mapping values wait in the detail scope for a steward.
    """
    from . import eval_findings
    for (scope_sha, run_id), findings in groups.items():
        summary = eval_findings.replace_findings(
            scope_sha, findings, run_path=RUN_PATH, usage=usage,
            run_id=run_id)
        counts["findings"] = counts.get("findings", 0) + summary["written"]
        counts["replaced"] = counts.get("replaced", 0) + summary["deleted"]
        counts["rejected_fields"] = counts.get("rejected_fields", 0) \
            + sum(summary["rejected_fields"].values())


def _bump_run_usage(run_id: str) -> None:
    """Fire the REAL usage ledger for the ANNOTATOR writer (ADR-0052/D10).

    The ref kind is ``eval_findings.WRITER_KIND_AUDIT_RUN`` — the slot that
    surface already reads back — and the ref is the opaque run id. Best-effort
    like every other ledger caller: telemetry is evidence, never a dependency.
    """
    try:
        from . import eval_findings
        from .artifact_usage import bump_artifact_usage
        bump_artifact_usage([(eval_findings.WRITER_KIND_AUDIT_RUN, run_id)])
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a run
        logger.debug("audit-run telemetry bump skipped: %s", type(exc).__name__)


def _run_audit(run_id: str, trigger: str, claim) -> dict:
    """The SOLE worker: select, annotate, validate, persist, record.

    The cap is read HERE (:data:`DAILY_AUDIT_CAP`) — no caller supplies one —
    and it bounds this run's candidate selection as well as the day's spend.
    ``trigger`` is carried into the record and branched on NOWHERE.
    """
    counts = {"candidates": 0, "audited": 0, "skipped_over_cap": 0,
              "skipped_budget": 0}
    slots = claim.granted
    usage = None
    try:
        candidates = _candidates()
        counts["candidates"] = len(candidates)
        selected = candidates[:DAILY_AUDIT_CAP]
        counts["skipped_over_cap"] = len(candidates) - len(selected)
        groups, measured, slots = _audit_selected(selected, claim.day_key,
                                                  claim.granted, counts)
        usage = audit_core.summarize_usage(measured, counts["audited"])
        _persist(groups, usage, counts)
        _bump_run_usage(run_id)
        status, error = STATUS_COMPLETE, ""
    except Exception as exc:  # noqa: BLE001 - a worker failure is recorded, never raised
        logger.warning("audit_run_failed: run=%s category=%s", run_id,
                       type(exc).__name__)
        status, error = STATUS_FAILED, type(exc).__name__
    finally:
        _release_active(run_id)
    return _finish_run(run_id, trigger, status, counts, slots=slots,
                       day_key=claim.day_key, usage=usage, error=error,
                       durable=claim.durable)
