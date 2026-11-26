# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Findings-ledger routes — bound import, minimized reads, steward filing
(ADR-0062 D6/D7/D8/D9).

A CHILD router: no prefix of its own, ``include_router``-ed into the existing
``eval_routes.py`` parent, so the whole Evals surface keeps ONE mount in
``server.py`` and ONE ``_check_admin`` boundary (D9).

Contract:

* ``POST /api/admin/evals/findings/import`` — a RAW ``Request`` handler. The
  ingress cap is enforced BEFORE parsing: an oversized ``Content-Length`` is
  rejected 413 without touching the stream, and the stream itself is read
  under a hard byte ceiling (a lying or absent ``Content-Length`` cannot get
  through) — a pydantic body model would run AFTER FastAPI had already
  parsed, which is not a cap at all. The payload carries ``findings_file``
  and ``source_report`` TOGETHER (D7: the source binding is fail-CLOSED —
  there is no "resolve the referenced report if we happen to have it" path).
  ``source_report`` is the report's TEXT, because the binding is
  ``sha256`` over the EXACT bytes the auditor hashed; a re-serialized object
  would not reproduce that digest and is refused with the pinned
  ``invalid_source_report``. An OPTIONAL third part, ``evidence``, carries the
  auditor's ``--evidence`` bundle so referential integrity resolves against
  report ids UNION evidence ids; it is transient (never persisted, logged or
  echoed), a malformed bundle is a pinned ``invalid_evidence`` 422 rather than
  a silent drop, and it can only WIDEN the known-id set — a ref resolving in
  neither half is still discarded and counted. The ingress cap covers the
  WHOLE body, so the third part buys no extra ceiling.
  Validation, minimization, untrusted-LLM-output
  treatment and replace-not-accrete all live in ``eval_findings`` — the
  uploaded report is parsed transiently there and NEVER handed to a store.
* ``GET /api/admin/evals/findings`` — the taxonomy-grouped ledger (minimized
  index entries; refs stay refs). With ``?window=7d`` it returns the
  read-time weekly digest instead. Both are COMPUTED ON READ and write
  NOTHING (pinned by spy persistence seams, not by inspection). Window
  bounds are NOT re-declared here: ``weekly_digest`` clamps them, so the
  bounds keep one home.
* ``GET /api/admin/evals/findings/{id}`` — the ONLY door that resolves a
  ref-bearing row against its 7 d customer-derived detail. An expired detail
  is ``detail_expired: true`` — a NORMAL state, never an error (D6).
* ``POST /api/admin/evals/findings/{id}/annotate`` — steward status/note.
  The note gets the untrusted-text treatment inside ``eval_findings``; a
  rejection is a 400 whose detail NEVER echoes the note.
* ``POST /api/admin/evals/findings/{id}/file-proposal`` — steward-TRIGGERED
  only, and the ONLY door through which ``suggested_mapping`` VALUES enter
  durable storage (D6). It requires the 7 d detail to still exist, because
  that is where the mapping lives: after expiry it is **409**. Nothing in
  this codebase files a proposal automatically. A filing that LANDS fires the
  REAL usage ledger with a ``("proposed-mapping", <mapping_id>)`` ref — this
  writer's own ADR-0052 telemetry, distinct from the import writer's
  ``eval-finding`` refs and never fired by an import or by a read.
* ``GET /api/admin/evals/writer-telemetry`` — the ADR-0052/D10 READ path:
  ``eval_findings.writer_telemetry()`` over the SAME
  ``artifact_usage`` ledger, per writer kind, refs only. It has its own path
  segment rather than ``/findings/telemetry`` so ``/findings/{finding_id}``
  can never shadow it, and it writes NOTHING (pinned).
* ``POST /api/admin/evals/audit/runs`` — the bounded triage annotator's ONE
  admin entry (ADR-0062 D11) and the LIVE test path for a job that ships
  default-off. The body is EMPTY with ``extra="forbid"``: no caller-supplied
  count, id list, model or token override exists, because the cap, the
  candidates and the model belong to the worker. It returns 202 with the
  ``run_id`` (a blocking run POST would reproduce the offboard-504 class), 409
  when a run is active and 429 — with DISTINCT details — when the day's global
  budget is exhausted or could not be consulted (fail closed).
  ``GET /audit/runs/{run_id}`` polls one run; ``GET /audit/status`` reports the
  flag state, the bounded run history, today's global budget and the
  per-writer usage-ledger counts, the last of which come from
  ``eval_findings.writer_telemetry`` rather than a second aggregation.

The filing call passes the ``audit_core.SUGGESTED_MAPPING_KEYS`` allowlist
and nothing else finding-derived, plus ``source="answer-audit"`` (which is in
``self_learning._NEVER_AUTO_ACCEPT_SOURCES``, so a filed proposal always
waits for a steward). ``confidence`` and ``query_context`` are REQUIRED
parameters of ``log_proposed_mapping`` — they cannot be omitted — so they are
supplied from module constants: a fixed :data:`FILED_CONFIDENCE` and an
OPAQUE ``answer-audit:<finding_id>`` provenance string. No excerpt, no
rationale, no question text is ever passed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import eval_findings
from .audit_core import SUGGESTED_MAPPING_KEYS

logger = logging.getLogger(__name__)

#: No prefix — the parent (``/api/admin/evals``) supplies it.
router = APIRouter()

#: Ingress ceiling for the import payload, enforced BEFORE ``json.loads``.
MAX_IMPORT_BYTES = 512 * 1024

#: The two halves the import BINDS (D7) plus the ONE optional transient key.
#: ``evidence`` is the auditor's evidence bundle (``audit_answer.py
#: --evidence <path>...``: investigation traces, intelligence summaries, graph
#: rules, onboarded schema), used ONLY so referential integrity can be
#: re-checked against the same ids the CLI saw. Like the report it is parsed
#: transiently and never persisted, logged or echoed — it is customer-derived
#: (D1). Supplying it WIDENS the known-id set; it never suppresses a discard.
REQUIRED_IMPORT_KEYS = ("findings_file", "source_report")
OPTIONAL_IMPORT_KEYS = ("evidence",)

#: Steward-input and binding rejections: 400 for a steward value the store
#: refused, 422 for a malformed request, 413 for an oversized one.
REJECT_STATUS = 400
INVALID_STATUS = 422
TOO_LARGE_STATUS = 413

#: DISTINCT 409 details — never collapse them: one says the 7 d detail aged
#: out, the other says this finding never carried a mapping.
DETAIL_EXPIRED = "detail expired"
DETAIL_NO_MAPPING = "finding carries no suggested mapping"

#: The learning source for a steward-filed audit proposal. Pinned into
#: ``self_learning._NEVER_AUTO_ACCEPT_SOURCES``.
PROPOSAL_SOURCE = "answer-audit"

#: ``log_proposed_mapping`` demands a confidence; a machine-suggested mapping
#: a steward chose to FILE is not thereby endorsed, so the value is fixed and
#: modest. It cannot shortcut review: the source never auto-accepts.
FILED_CONFIDENCE = 0.5

#: The steward status a successful filing records.
FILED_STATUS = "filed"

#: The subset of :data:`~agents.audit_core.SUGGESTED_MAPPING_KEYS` that
#: ``log_proposed_mapping`` has no default for — absent ones are sent as ""
#: rather than dropped, so the call stays well-formed. Pinned by signature
#: introspection in the tests.
REQUIRED_MAPPING_KEYS = ("from_system", "from_field", "from_value",
                         "to_system", "to_field", "to_value", "concept")

_WINDOW_RE = re.compile(r"^([0-9]{1,3})d?$")
_ACTOR_STRIP = re.compile(r"[^A-Za-z0-9_.:@|-]")


def _check_admin(request: Request):
    """The established admin posture (mirrors ``eval_routes.py``)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def _actor(request: Request) -> str:
    """Steward id for the annotation row — id-shaped, bounded, never text."""
    email = getattr(getattr(request.state, "user", None), "email", None)
    cleaned = _ACTOR_STRIP.sub("", str(email or "")).strip()
    return cleaned[:eval_findings.ANNOTATED_BY_MAX_CHARS] or "unknown"


def _reject(status: int, reason: str, detail: str):
    """One rejection shape ``{"reason", "detail"}``. Both come from pinned
    literals, so a rejected value can never round-trip to the client."""
    logger.info("eval_findings_route_rejected: reason=%s", reason)
    return HTTPException(status_code=status,
                         detail={"reason": reason, "detail": detail})


# ── ingress: the cap runs BEFORE any parsing ────────────────────────────────

def _declared_bytes(request: Request) -> int:
    """The declared body size, or -1 when absent/unparseable (⇒ the stream
    ceiling below is what holds)."""
    try:
        return int(request.headers.get("content-length"))
    except (TypeError, ValueError):
        return -1


async def _read_capped(request: Request) -> bytes:
    """The request body under a HARD byte ceiling.

    Two independent gates, because either alone is bypassable: an oversized
    ``Content-Length`` is refused with the stream UNTOUCHED, and the stream
    read aborts the moment the accumulated size crosses the ceiling — so a
    header that lies (or is missing entirely) buys nothing. The ceiling is
    over the WHOLE body, so the optional ``evidence`` part shares the same
    512 KB budget as the two bound halves rather than adding to it.
    """
    if _declared_bytes(request) > MAX_IMPORT_BYTES:
        raise _reject(TOO_LARGE_STATUS, "payload_too_large",
                      f"body exceeds {MAX_IMPORT_BYTES} bytes")
    chunks: list = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_IMPORT_BYTES:
            raise _reject(TOO_LARGE_STATUS, "payload_too_large",
                          f"body exceeds {MAX_IMPORT_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _import_payload(raw: bytes) -> dict:
    """The parsed import payload, or a 422. Both halves are REQUIRED (the
    binding is the point) and unknown keys are refused, mirroring the
    ``extra="forbid"`` posture of the pydantic bodies on this surface."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _reject(INVALID_STATUS, "invalid_json",
                      "body must be a JSON object")
    if not isinstance(payload, dict):
        raise _reject(INVALID_STATUS, "invalid_json",
                      "body must be a JSON object")
    missing = [key for key in REQUIRED_IMPORT_KEYS if key not in payload]
    if missing:
        raise _reject(INVALID_STATUS, "missing_fields",
                      "'findings_file' and 'source_report' are both required")
    allowed = set(REQUIRED_IMPORT_KEYS) | set(OPTIONAL_IMPORT_KEYS)
    if not set(payload).issubset(allowed):
        raise _reject(INVALID_STATUS, "unknown_keys",
                      f"allowed keys: {', '.join(sorted(allowed))}")
    return payload


def _evidence_docs(payload: dict) -> "list | None":
    """The OPTIONAL transient evidence bundle, or ``None`` when absent.

    A malformed bundle is a 422 rather than a silent drop: dropping it would
    re-open the very failure this input exists to fix — the steward supplies
    evidence, the refs it carries still resolve nowhere, and the findings come
    back ``discarded_invalid`` with nothing saying why. Only the SHAPE is
    checked here (a list of JSON objects); the ids inside are walked by
    ``audit_core.collect_known_ids``, and no value is echoed back.
    """
    if "evidence" not in payload:
        return None
    docs = payload["evidence"]
    if isinstance(docs, dict):
        docs = [docs]                 # a single evidence document, unwrapped
    if not isinstance(docs, list) or not all(
            isinstance(doc, dict) for doc in docs):
        raise _reject(INVALID_STATUS, "invalid_evidence",
                      "'evidence' must be a JSON object or a list of them")
    return docs


@router.post("/findings/import")
async def import_findings(request: Request):
    """Import findings TOGETHER WITH the report bytes they were derived from.

    ``evidence`` is optional and TRANSIENT: it only widens the id set
    referential integrity resolves against (report ids UNION evidence ids), so
    a finding citing an id that lives solely in an ``--evidence`` document is
    kept instead of discarded. It is never persisted, and a ref that resolves
    in NEITHER half is still discarded and counted.
    """
    _check_admin(request)
    payload = _import_payload(await _read_capped(request))
    summary, problem = eval_findings.import_findings(
        payload["findings_file"], payload["source_report"],
        evidence_docs=_evidence_docs(payload))
    if problem is not None:
        reason, detail = problem
        raise _reject(INVALID_STATUS, reason, detail)
    return summary


# ── reads (zero writes) ─────────────────────────────────────────────────────

def _window_days(window: str) -> int:
    """``"7d"``/``"7"`` → 7. Clamping stays in ``weekly_digest`` (one home)."""
    match = _WINDOW_RE.match(str(window).strip())
    if not match:
        raise _reject(INVALID_STATUS, "invalid_window",
                      "'window' must look like '7d'")
    return int(match.group(1))


@router.get("/findings")
async def list_findings(request: Request, window: str | None = None):
    """The taxonomy-grouped ledger, or the read-time digest for ``?window=7d``.
    Neither path writes."""
    _check_admin(request)
    if window is None:
        return eval_findings.list_findings()
    return eval_findings.weekly_digest(_window_days(window))


@router.get("/findings/{finding_id}")
async def get_finding(request: Request, finding_id: str):
    """One finding joined with its detail IF unexpired — the only ref-resolving
    door."""
    _check_admin(request)
    row = eval_findings.get_finding(finding_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown finding_id")
    return row


@router.get("/writer-telemetry")
async def writer_telemetry(request: Request, window: str | None = None):
    """Per-writer usage-ledger evidence — the ADR-0052/D10 READ path.

    Its own path segment (NOT ``/findings/telemetry``), so the
    ``/findings/{finding_id}`` route cannot shadow it whatever order the
    parent includes things in. Reads the REAL ledger back through
    ``eval_findings.writer_telemetry`` and writes nothing; the window bounds
    are clamped there (one home), as for the digest.

    OFF-LOOP: the ledger read is a bounded DynamoDB scan (blocking), so it
    goes through ``asyncio.to_thread`` — the ``vocabulary_routes``
    ``read_artifact_usage`` idiom.
    """
    _check_admin(request)
    days = (eval_findings.TELEMETRY_WINDOW_DAYS if window is None
            else _window_days(window))
    return await asyncio.to_thread(eval_findings.writer_telemetry, days)


# ── steward annotation ──────────────────────────────────────────────────────

class AnnotateRequest(BaseModel):
    """POST /findings/{id}/annotate body.

    ``extra="forbid"`` (422 on any unknown key) and the status is the pinned
    lifecycle enum. The note carries NO pydantic length rule on purpose:
    length, secret-shape and fence-delimiter rejection live in
    ``eval_findings`` behind ONE pinned reason set, and a pydantic length
    error would echo the rejected note back in ``input``.
    """
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "annotated", "filed", "dismissed"] | None = None
    note: str | None = None


@router.post("/findings/{finding_id}/annotate")
async def annotate(request: Request, finding_id: str,
                   body: AnnotateRequest | None = None):
    """Record ONE steward annotation (a separate scope — survives re-import)."""
    _check_admin(request)
    body = body or AnnotateRequest()
    row, problem = eval_findings.annotate(
        finding_id, status=body.status, note=body.note,
        annotated_by=_actor(request))
    if problem is not None:
        reason, detail = problem
        if reason == "unknown_finding":
            raise HTTPException(status_code=404, detail="Unknown finding_id")
        raise _reject(REJECT_STATUS, reason, detail)
    return {"status": "recorded", "annotation": row}


# ── steward-triggered proposal filing (the ONE door for mapping values) ──────

def _mapping_of(row: dict) -> dict:
    """The suggested mapping from the 7 d detail row, or a DISTINCT 409.

    Filing needs the detail that justifies it, and the mapping values live
    ONLY there (D6) — so an expired detail is a refusal, not a silent filing
    of a mapping nobody can review.
    """
    detail = row.get("detail")
    if row.get("detail_expired") or not isinstance(detail, dict):
        raise HTTPException(status_code=409, detail=DETAIL_EXPIRED)
    mapping = detail.get("suggested_mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise HTTPException(status_code=409, detail=DETAIL_NO_MAPPING)
    return mapping


def _proposal_kwargs(mapping: dict, finding_id: str) -> dict:
    """Exactly the ALLOWLIST keys (iterated, never the input) plus the three
    non-finding-derived kwargs the call cannot go without."""
    kwargs = {key: mapping[key] for key in SUGGESTED_MAPPING_KEYS
              if isinstance(mapping.get(key), str) and mapping[key]}
    for key in REQUIRED_MAPPING_KEYS:
        kwargs.setdefault(key, "")
    kwargs["source"] = PROPOSAL_SOURCE
    kwargs["confidence"] = FILED_CONFIDENCE
    kwargs["query_context"] = f"{PROPOSAL_SOURCE}:{finding_id}"
    return kwargs


def _file_mapping(mapping: dict, finding_id: str) -> "str | None":
    """File the mapping through the EXISTING proposal plumbing.

    ``None`` is normal — ``log_proposed_mapping`` gates, absorbs and stages
    proposals, and every caller must tolerate that. Only key names are ever
    logged.
    """
    from . import self_learning
    kwargs = _proposal_kwargs(mapping, finding_id)
    try:
        return self_learning.log_proposed_mapping(**kwargs)
    except Exception as exc:                     # noqa: BLE001 - see below
        # Values must not reach a log record, so only the exception TYPE and
        # the kwarg NAMES are recorded.
        logger.warning("eval_findings_proposal_failed: type=%s fields=%s",
                       type(exc).__name__, sorted(kwargs))
        raise HTTPException(status_code=502,
                            detail="proposal filing failed") from None


def _bump_filed_usage(mapping_id: str) -> None:
    """Fire the REAL usage ledger for the PROPOSAL-FILED writer (ADR-0052,
    ADR-0062 D10).

    Fires ONLY here: on a steward-triggered filing that actually LANDED a
    mapping id. A gated/absorbed filing bumps nothing (nothing landed), an
    import bumps its own ``eval-finding`` refs instead, and a read bumps
    nothing at all. The ref is the mapping id — opaque, never a finding
    excerpt, rationale or mapping VALUE. Best-effort like every other ledger
    caller: telemetry must not fail a steward action.
    """
    try:
        from .artifact_usage import bump_artifact_usage
        bump_artifact_usage(
            [(eval_findings.WRITER_KIND_PROPOSAL_FILED, mapping_id)])
    except Exception as exc:                     # noqa: BLE001
        logger.debug("filed-proposal telemetry bump skipped: %s",
                     type(exc).__name__)


@router.post("/findings/{finding_id}/file-proposal")
async def file_proposal(request: Request, finding_id: str):
    """Steward-TRIGGERED filing of ONE mapping-shaped finding."""
    _check_admin(request)
    row = eval_findings.get_finding(finding_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown finding_id")
    mapping_id = _file_mapping(_mapping_of(row), finding_id)
    if mapping_id is None:
        # Gated, absorbed or staged by the learning pipeline: the finding is
        # NOT marked filed, because nothing landed for a steward to review —
        # and nothing landed means nothing to record in the usage ledger.
        return {"status": "gated", "finding_id": finding_id,
                "mapping_id": None}
    _bump_filed_usage(mapping_id)
    annotation, problem = eval_findings.annotate(
        finding_id, status=FILED_STATUS, annotated_by=_actor(request))
    if problem is not None:
        logger.info("eval_findings_filed_status_not_recorded: reason=%s",
                    problem[0])
    return {"status": FILED_STATUS, "finding_id": finding_id,
            "mapping_id": mapping_id, "annotation": annotation}


# ── bounded triage annotation: the one-shot entry + its read paths ──────────

class AuditRunRequest(BaseModel):
    """POST /audit/runs body — INTENTIONALLY EMPTY.

    ``extra="forbid"`` with no fields means a caller cannot supply a candidate
    count, an id list, a model or a token override: the cap, the selection and
    the model live inside the worker (ADR-0062 D11). A body is optional; any
    key at all is a 422.
    """
    model_config = ConfigDict(extra="forbid")


#: DISTINCT refusal details for the one-shot — never collapse them. One says a
#: run is in flight; one says the day's budget was consulted and is exhausted;
#: one says it could not be consulted at all, so nothing was spent.
AUDIT_ACTIVE_DETAIL = "audit run already active"
AUDIT_BUDGET_DETAIL = "daily audit budget exhausted"
AUDIT_BUDGET_UNAVAILABLE_DETAIL = ("daily audit budget unavailable — "
                                   "failing closed")
AUDIT_ACTIVE_STATUS = 409
AUDIT_BUDGET_STATUS = 429


def _audit_refusal(reason: str) -> HTTPException:
    """Map ONE pinned refusal reason to its own status + detail."""
    from . import audit_scheduler
    if reason == audit_scheduler.REASON_ACTIVE:
        return HTTPException(status_code=AUDIT_ACTIVE_STATUS,
                             detail=AUDIT_ACTIVE_DETAIL)
    if reason == audit_scheduler.REASON_BUDGET_UNAVAILABLE:
        return HTTPException(status_code=AUDIT_BUDGET_STATUS,
                             detail=AUDIT_BUDGET_UNAVAILABLE_DETAIL)
    return HTTPException(status_code=AUDIT_BUDGET_STATUS,
                         detail=AUDIT_BUDGET_DETAIL)


@router.post("/audit/runs", status_code=202)
async def start_audit_run(request: Request,
                          body: AuditRunRequest | None = None):
    """Start ONE bounded triage-annotation run — 202 with the ``run_id``.

    The route is the LIVE test path for the annotator (the scheduled job ships
    default-off), so it works regardless of ``NIGHTLY_AUDIT_ENABLED``. It calls
    the ONE entry with ``trigger="manual"`` — provenance only — and passes
    NOTHING else; the worker owns the cap, the candidates and the model. The
    call returns as soon as the budget slot is claimed, so a run can never hold
    the request open past the origin timeout (the offboard-504 class).
    """
    _check_admin(request)
    from . import audit_scheduler
    run_id, reason = audit_scheduler.request_audit(
        audit_scheduler.TRIGGER_MANUAL)
    if run_id is None:
        raise _audit_refusal(reason or "")
    return audit_scheduler.get_run(run_id) or {"run_id": run_id}


@router.get("/audit/runs/{run_id}")
async def get_audit_run(request: Request, run_id: str):
    """Poll ONE annotation run. Writes nothing."""
    _check_admin(request)
    from . import audit_scheduler
    record = audit_scheduler.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return record


def _telemetry_counts(report: dict) -> dict:
    """``{writer kind: measured uses}`` folded from the ADR-0052 ledger read.

    The panel renders one count per writer kind, so the status block carries
    exactly that — folded from ``eval_findings.writer_telemetry`` rather than
    re-derived, so this surface keeps ONE ledger aggregation contract. Uses are
    MEASURED counts; there is no rate, estimate or money figure in the shape.
    """
    counts: dict = {}
    for kind, slot in (report.get("writers") or {}).items():
        if isinstance(slot, dict) and slot.get("fired"):
            counts[str(kind)] = int(slot.get("uses") or 0)
    return counts


@router.get("/audit/status")
async def audit_status(request: Request):
    """The annotator's read path: flag state, recent runs, today's GLOBAL
    budget, and the usage-ledger counts per writer.

    Every block is COMPUTED ON READ and writes nothing. The telemetry block
    calls ``eval_findings.writer_telemetry`` (the same read path
    ``GET /writer-telemetry`` serves) — off-loop through ``asyncio.to_thread``,
    since the ledger read is a bounded blocking scan.
    """
    _check_admin(request)
    from . import audit_scheduler
    status = audit_scheduler.audit_status()
    report = await asyncio.to_thread(eval_findings.writer_telemetry)
    status["telemetry"] = _telemetry_counts(report)
    return status
