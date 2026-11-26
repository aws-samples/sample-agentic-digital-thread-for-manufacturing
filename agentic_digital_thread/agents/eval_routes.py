# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin routes for server-side golden-eval runs (the Evals admin tab).

The API contract (all behind the ``_check_admin`` posture from
``admin_routes.py``):

* ``POST /api/admin/evals/run`` — body ``{}``; an optional ``variant``
  string label is the ONLY accepted key. Any subset-selection key
  (``count``, ``category``, ``ids``, ``question_ids``, …) is rejected 422
  by pydantic ``extra="forbid"`` — the **run-ALL-only** pin. Returns 202
  ``{"run_id", "status": "running"}``; 409 with DISTINCT details when a run
  is already active vs when the golden set is missing/empty.
* ``POST /api/admin/evals/run-upload`` — the steward's OWN golden set, as the
  file's RAW bytes (ADR-0064). A DISTINCT route, never a new key on ``/run``:
  the body model's ``extra="forbid"`` pin is what makes run-ALL structural,
  so ``/run`` stays byte-unchanged. The body is the raw bytes rather than an
  envelope carrying the text as a string, because the sha must bind the
  steward's file BYTE-EXACTLY (``golden_bytes_sha(raw) ==
  golden_file_sha(their path)``); a re-serialized object would not reproduce
  that digest. Run-ALL is structural here too: ``?variant=`` is the ONLY
  accepted query parameter (explicit allowlist ⇒ 422), so there is no
  ``count``/``ids``/``category`` to forbid. Those bytes are TRANSIENT
  request/process data — never persisted, never logged, never echoed; the
  parsed questions reach the runner as values and live only in the worker
  thread's locals. Returns 202 ``{"run_id", "status", "golden_set_source",
  "golden_set_sha", "question_count"}``.
* ``GET /api/admin/evals/runs`` — newest-first history, bounded 50.
* ``GET /api/admin/evals/runs/{run_id}`` — progress while running, the full
  persisted report when complete, 404 for unknown ids.

The async-job shape (202 + poll) exists because a full golden run is
minutes long — a blocking POST would reproduce the documented offboard-504
class (the 120 s CloudFront origin timeout).

This module is also the ONE Evals router (ADR-0062 D9): child route modules
(``eval_queue_routes.py``, …) are ``include_router``-ed below so ``server.py``
keeps a single mount and a single ``_check_admin`` boundary.

Nothing here touches a filesystem. The server has EXACTLY ONE filesystem
golden source — ``eval_core.default_golden_set_path()``, reached only from
``eval_runs`` — and an uploaded set reaches the loader as BYTES
(``eval_core.parse_golden_set``), so it can never acquire a path, a temp file
or a cached copy (ADR-0064 D4, pinned by
``tests/unit/test_customer_golden_boundary.py``).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import (eval_core, eval_findings_routes, eval_queue_routes, eval_runs,
               untrusted_text)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/evals")

# ONE Evals router (ADR-0062 D9): child modules are included HERE so
# ``server.py`` keeps its single mount and single ``_check_admin`` boundary.
router.include_router(eval_queue_routes.router)
router.include_router(eval_findings_routes.router)


def _check_admin(request: Request):
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


class RunRequest(BaseModel):
    """POST /run body — ``variant`` is the ONLY accepted key (run-ALL-only:
    any subset-selection key 422s via ``extra="forbid"``)."""
    model_config = ConfigDict(extra="forbid")
    variant: str | None = None


# Distinct 409 details (pinned — never collapse them).
_DETAIL_ACTIVE = "eval run already active"
_DETAIL_NO_GOLDENS = "golden set missing or empty"


@router.post("/run", status_code=202)
async def start_run(request: Request, body: RunRequest | None = None):
    _check_admin(request)
    variant = body.variant if body is not None else None
    run_id, reason = eval_runs.start_run(variant)
    if run_id is None:
        detail = (_DETAIL_ACTIVE if reason == eval_runs.REASON_ACTIVE
                  else _DETAIL_NO_GOLDENS)
        raise HTTPException(status_code=409, detail=detail)
    return {"run_id": run_id, "status": "running"}


# ── uploaded customer golden set (ADR-0064) ─────────────────────────────────
# A steward's OWN golden set, posted as the file's RAW bytes and used for ONE
# run. Those bytes are transient request data: nothing below writes them,
# logs them, or echoes any fragment of them, and every refusal detail is
# either a pinned literal or a bounded message the loader built.

#: Ingress ceiling, enforced BEFORE any parsing (ADR-0064 D6) — the
#: findings-import 512 KiB pattern halved: a 40-question set with its
#: expectations is tens of KiB.
MAX_GOLDEN_UPLOAD_BYTES = 256 * 1024

#: The ONLY accepted query parameter. Run-ALL is STRUCTURAL here (ADR-0064
#: D3): with no selection parameter in the contract there is no ``count`` /
#: ``ids`` / ``category`` / ``subset`` / ``limit`` to forbid, and anything
#: else at all — including a typo — is a 422 rather than a silent subset.
UPLOAD_QUERY_PARAMS = frozenset({"variant"})

#: Pinned refusal reasons (the ADR-0064 D3 table) — never collapse them.
REASON_TOO_LARGE = "payload_too_large"
REASON_INVALID_JSON = "invalid_json"
REASON_INVALID_GOLDEN = "invalid_golden_set"
REASON_TOO_MANY_QUESTIONS = "too_many_questions"
REASON_UNKNOWN_PARAMS = "unknown_query_params"
REASON_UPLOAD_ACTIVE = "active_run"
REASON_UPLOAD_NO_GOLDENS = "no_goldens"

#: Statuses in the ``eval_findings_routes`` register: 413 oversized, 422 a
#: malformed request, 400 a steward VALUE the platform refused, 409 a
#: conflicting run. ``too_many_questions`` is a malformed REQUEST (422),
#: NEVER a 409 — nothing is in flight; the set is too big for one run.
TOO_LARGE_STATUS = 413
INVALID_STATUS = 422
REJECT_STATUS = 400
CONFLICT_STATUS = 409

#: Bound on any refusal detail quoting the loader — even a message the loader
#: is trusted to build stays bounded before it reaches a client.
DETAIL_MAX_CHARS = 300

#: ``eval_core`` raises BOTH bytes-level refusals (a UTF-8 BOM; undecodable
#: bytes) with the computed sha appended, because that sha is how a steward
#: reconciles the refusal against their own file — so those two are surfaced
#: verbatim (bounded) and classified ``invalid_json``. The marker is the sha
#: clause itself, which no schema message carries.
_SHA_MESSAGE_MARKER = "sha256[:12] as given:"

#: The ONE loader message embedding a repr of the offending question (i.e.
#: possibly customer question TEXT). It is REPLACED, never echoed.
_QUESTION_REPR_MESSAGE = "golden question missing id/text/expect"


def _reject(status: int, reason: str, detail: str) -> HTTPException:
    """ONE rejection shape ``{"reason", "detail"}`` (the findings-import
    idiom). Only the pinned reason is logged — never the detail, and never a
    fragment of the body."""
    logger.info("eval_upload_rejected: reason=%s", reason)
    return HTTPException(status_code=status,
                         detail={"reason": reason, "detail": detail})


def _check_upload_params(request: Request) -> None:
    """``variant`` is the ONLY query parameter (run-ALL, ADR-0064 D3).

    The detail names the ALLOWED key and never the supplied ones: a rejected
    parameter NAME is client-supplied text like any other.
    """
    if not set(request.query_params) <= UPLOAD_QUERY_PARAMS:
        raise _reject(INVALID_STATUS, REASON_UNKNOWN_PARAMS,
                      "'variant' is the only accepted query parameter — a "
                      "run scores the WHOLE uploaded set")


def _declared_bytes(request: Request) -> int:
    """The declared body size, or -1 when absent/unparseable (⇒ the stream
    ceiling below is what holds)."""
    try:
        return int(request.headers.get("content-length"))
    except (TypeError, ValueError):
        return -1


async def _read_capped(request: Request) -> bytes:
    """The raw golden bytes under a HARD ceiling, BEFORE any parsing.

    Two independent gates, because either alone is bypassable (the
    findings-import idiom): an oversized ``Content-Length`` is refused with
    the stream UNTOUCHED, and the read aborts the moment the accumulated size
    crosses the ceiling — so a header that lies or is missing buys nothing. A
    pydantic body model would run AFTER FastAPI parsed the body, which is not
    a cap at all.
    """
    if _declared_bytes(request) > MAX_GOLDEN_UPLOAD_BYTES:
        raise _reject(TOO_LARGE_STATUS, REASON_TOO_LARGE,
                      f"body exceeds {MAX_GOLDEN_UPLOAD_BYTES} bytes")
    chunks: list = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_GOLDEN_UPLOAD_BYTES:
            raise _reject(TOO_LARGE_STATUS, REASON_TOO_LARGE,
                          f"body exceeds {MAX_GOLDEN_UPLOAD_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_untrusted(text: str, field: str, label: str,
                      max_len: int) -> None:
    """The untrusted-text treatment for steward-supplied input (ADR-0064 D6).

    An uploaded set enters the model prompt path, so a pasted credential must
    not; the ``variant`` label gets the same treatment because it is the ONE
    place a steward types free text that a report persists.
    ``validate_untrusted``'s details never include the text itself, and the
    status split follows the findings register: a refused VALUE is 400, a
    malformed request is 422.
    """
    problem = untrusted_text.validate_untrusted(
        text, max_len=max_len, field=field, label=label)
    if problem is None:
        return
    reason, detail = problem
    status = (REJECT_STATUS if reason == "secret_like_content"
              else INVALID_STATUS)
    raise _reject(status, reason, detail)


def _invalid_golden_refusal(exc: Exception) -> tuple:
    """``(status, reason, detail)`` for a loader refusal — content-free.

    The bytes-level refusals carry the computed sha and are surfaced as
    ``invalid_json`` so the steward can compare it against
    ``golden_file_sha`` on their own copy; the one message embedding a
    question repr is replaced; anything else is a bounded schema message
    (ids, keys and marking values only).
    """
    message = str(exc)
    if _SHA_MESSAGE_MARKER in message:
        return INVALID_STATUS, REASON_INVALID_JSON, message[:DETAIL_MAX_CHARS]
    if message.startswith(_QUESTION_REPR_MESSAGE):
        return (INVALID_STATUS, REASON_INVALID_GOLDEN,
                f"{_QUESTION_REPR_MESSAGE} (question content omitted)")
    if not isinstance(exc, ValueError):
        return (INVALID_STATUS, REASON_INVALID_GOLDEN,
                "golden set questions must be JSON objects")
    return INVALID_STATUS, REASON_INVALID_GOLDEN, message[:DETAIL_MAX_CHARS]


def _parsed_questions(raw: bytes) -> list:
    """The uploaded set's questions through the ONE validator home.

    ``eval_core.parse_golden_set`` owns the strict-UTF-8/BOM policy and every
    schema rule, so a set that parses here runs on the CLI BY CONSTRUCTION
    (ADR-0064 D2) — this route adds no second parser and no second decode
    policy.
    """
    try:
        return eval_core.parse_golden_set(raw)
    except json.JSONDecodeError as exc:
        raise _reject(INVALID_STATUS, REASON_INVALID_JSON,
                      f"body is not valid JSON (line {exc.lineno} column "
                      f"{exc.colno})") from None
    except (ValueError, AttributeError, TypeError) as exc:
        raise _reject(*_invalid_golden_refusal(exc)) from None


def _upload_refusal(reason: str) -> HTTPException:
    """Map ONE pinned runner reason to its own status + reason + detail.

    The 409 details are the SAME pinned strings ``/run`` uses, because the
    one-run lock is the same lock: a demo run and a customer run can never
    overlap (ADR-0064 D3).
    """
    if reason == eval_runs.REASON_TOO_MANY_QUESTIONS:
        return _reject(INVALID_STATUS, REASON_TOO_MANY_QUESTIONS,
                       "a run scores at most "
                       f"{eval_runs.MAX_UPLOADED_QUESTIONS} questions — "
                       "split the set (a run is never truncated)")
    if reason == eval_runs.REASON_NO_GOLDENS:
        return _reject(CONFLICT_STATUS, REASON_UPLOAD_NO_GOLDENS,
                       _DETAIL_NO_GOLDENS)
    return _reject(CONFLICT_STATUS, REASON_UPLOAD_ACTIVE, _DETAIL_ACTIVE)


@router.post("/run-upload", status_code=202)
async def start_uploaded_run(request: Request):
    """Run the steward's OWN golden set once, from its RAW bytes.

    Gate order is deliberate: admin → query allowlist → ingress cap (before
    ANY parsing) → untrusted-text treatment → the ONE validator → the
    runner's own question-count ceiling and shared one-run lock. The bytes
    are dropped as soon as the sha is computed and the parsed questions are
    handed over, and only the sha and the count are logged.
    """
    _check_admin(request)
    _check_upload_params(request)
    raw = await _read_capped(request)
    variant = request.query_params.get("variant")
    _reject_untrusted(variant or "", "'variant'", "variant label",
                      untrusted_text.DEFAULT_MAX_LEN)
    _reject_untrusted(raw.decode("utf-8", "replace"), "'golden set'",
                      "golden set", MAX_GOLDEN_UPLOAD_BYTES)
    questions = _parsed_questions(raw)
    sha = eval_core.golden_bytes_sha(raw)
    del raw
    run_id, reason = eval_runs.start_run(
        variant, goldens=questions, golden_sha=sha,
        golden_source=eval_core.GOLDEN_SOURCE_UPLOADED)
    if run_id is None:
        raise _upload_refusal(reason or "")
    logger.info("eval_upload_run_started: sha=%s question_count=%d",
                sha, len(questions))
    return {"run_id": run_id, "status": "running",
            "golden_set_source": eval_core.GOLDEN_SOURCE_UPLOADED,
            "golden_set_sha": sha, "question_count": len(questions)}


@router.get("/runs")
async def list_runs(request: Request):
    _check_admin(request)
    return eval_runs.list_runs()


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str):
    _check_admin(request)
    record = eval_runs.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return record
