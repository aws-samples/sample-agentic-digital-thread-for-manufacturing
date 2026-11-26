# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue read + steward-decision routes (ADR-0062 D3/D4/D9, plan 2.1).

A CHILD router: it carries no prefix of its own and is ``include_router``-ed
into the existing ``eval_routes.py`` parent, so the whole Evals surface keeps
ONE mount in ``server.py`` and ONE ``_check_admin`` auth boundary (D9).

Contract:

* ``GET /api/admin/evals/queue?window=<days>&limit=<n>`` — the queue is
  COMPUTED ON READ by :func:`eval_queue.compute_queue`: a bounded feedback
  window (negative first) plus the failed rows of the newest-N persisted
  golden reports, joined with persisted steward decisions. Display text is
  joined LIVE here and never persisted; a decided source that no longer
  resolves comes back as a ``source_expired`` item rather than an error.
  **The GET writes nothing** — pinned by spy persistence seams, not by
  inspection. Window/limit bounds are NOT re-declared here: ``compute_queue``
  clamps them, so the bounds have exactly one home.
* ``POST /api/admin/evals/queue/decision`` — body ``extra="forbid"``, verdict
  pinned to the enum, optional bounded note. Notes get the
  ``untrusted_text`` treatment inside ``eval_queue.decide``; a rejection is a
  400 carrying ``{"reason", "detail"}`` and the detail NEVER echoes the note.

* ``POST /api/admin/evals/queue/golden-draft`` — body ``{source_ref}``,
  ``extra="forbid"``. Builds a TRANSIENT promoted-golden draft
  (``eval_promote.draft_golden``) and returns it as a DOWNLOAD: attachment
  with an opaque sha-16 filename, ``Cache-Control: private, no-store`` and
  ``Pragma: no-cache``. POST (never GET) so no cache layer treats it as
  cacheable. It persists NOTHING — no JobStore row, no DynamoDB item, no
  ``/tmp`` file, no draft content in any log record (D5, pinned by a
  persistence-seam test). The only store touch on this path is
  ``compute_queue``/``get_decision``, both pure reads. States that cannot
  produce an honest draft are distinct 409s (``not_decided``,
  ``source_expired``, ``no_question_text``) — never a fabricated draft.

There is deliberately **no refresh route** (and no refresh writer or job):
the queue is computed, so nothing can go stale (D3). The absence is pinned by
a route-table test.

Machine pre-scores travel through untouched — they are TRIAGE ONLY (D4). This
module cannot write a decision on the machine's behalf: the only writer it
reaches is :func:`eval_queue.decide`, called from the steward route with the
verdict the steward supplied.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from . import eval_promote, eval_queue
from .offload import offload

logger = logging.getLogger(__name__)

#: No prefix — the parent (``/api/admin/evals``) supplies it.
router = APIRouter()

#: Every steward-input rejection is one status: a 400 carrying the pinned
#: ``(reason, detail)`` pair from ``eval_queue.decide``.
REJECT_STATUS = 400

#: A source that cannot produce an honest draft yet (undecided, expired, or
#: carrying no promotable question text) is a CONFLICT, not an error.
DRAFT_CONFLICT_STATUS = 409

# Actor ids are narrowed to the charset ``eval_queue`` accepts for
# ``decided_by`` (an email with a '+' tag would otherwise be a spurious 400).
_ACTOR_STRIP = re.compile(r"[^A-Za-z0-9_.:@|-]")


def _check_admin(request: Request):
    """The established admin posture (mirrors ``eval_routes.py``)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def _actor(request: Request) -> str:
    """Steward id for the decision row — id-shaped, bounded, never text."""
    email = getattr(getattr(request.state, "user", None), "email", None)
    cleaned = _ACTOR_STRIP.sub("", str(email or "")).strip()
    return cleaned[:eval_queue.DECIDED_BY_MAX_CHARS] or "unknown"


class DecisionRequest(BaseModel):
    """POST /queue/decision body.

    ``extra="forbid"`` (422 on any unknown key) and the verdict is the pinned
    enum — the steward's three verdicts and nothing else. The note carries NO
    pydantic length rule on purpose: length, secret-shape and fence-delimiter
    rejection all live in ``eval_queue`` behind ONE pinned reason set, and a
    pydantic length error would echo the rejected note back in ``input``.
    """
    model_config = ConfigDict(extra="forbid")
    source_ref: str
    verdict: Literal["correct", "incorrect", "needs_context"]
    taxonomy_tags: list[str] | None = None
    note: str | None = None


class DraftRequest(BaseModel):
    """POST /queue/golden-draft body — the decided source and nothing else.

    ``extra="forbid"``: no expectation, phrase, filename or golden-subset key
    may be supplied. The draft's content is built server-side from the fixed
    skeleton, so a client cannot inject a gate (ADR-0062 D5).
    """
    model_config = ConfigDict(extra="forbid")
    source_ref: str


@router.get("/queue")
async def get_queue(request: Request, window: int = eval_queue.DEFAULT_WINDOW_DAYS,
                    limit: int = eval_queue.DEFAULT_LIMIT):
    """The computed review queue. Performs no writes.

    Offloaded: ``compute_queue`` is per-UTC-day DynamoDB queries plus the
    newest-N report reads — sync boto3 that must not run on the event loop
    (EVENT-LOOP OFFLOAD PATTERN, ``agents/offload.py``)."""
    _check_admin(request)
    return await offload(eval_queue.compute_queue, window_days=window, limit=limit)


@router.post("/queue/decision")
async def post_decision(request: Request, body: DecisionRequest):
    """Record ONE steward decision (the only write on this surface)."""
    _check_admin(request)
    row, problem = await offload(
        eval_queue.decide,
        body.source_ref, body.verdict, taxonomy_tags=body.taxonomy_tags,
        note=body.note, decided_by=_actor(request))
    if problem is not None:
        reason, detail = problem
        # ``detail`` comes from the pinned reason set and never contains the
        # note itself (secret-shaped input must not round-trip).
        logger.info("eval_queue_decision_rejected: reason=%s", reason)
        raise HTTPException(status_code=REJECT_STATUS,
                            detail={"reason": reason, "detail": detail})
    return {"status": "recorded", "decision": row}


def _resolve_source_turn(source_ref: str) -> "dict | None":
    """The live source turn for one ref, from the COMPUTED queue (pure read).

    The widest bounded window/limit is used so an older decided source still
    resolves; an item whose source has expired is not a turn.
    """
    queue = eval_queue.compute_queue(
        window_days=eval_queue.MAX_WINDOW_DAYS, limit=eval_queue.MAX_LIMIT)
    for item in queue.get("items") or []:
        if item.get("source_ref") == source_ref and not item.get("source_expired"):
            return item
    return None


def _draft_inputs(source_ref: str):
    """``(decision, source_turn, None)`` or ``(None, None, (status, reason,
    detail))`` with pinned reasons. Reads only; writes nothing."""
    if eval_queue.source_of(source_ref) is None:
        return None, None, (REJECT_STATUS, "invalid_source_ref",
                            "'source_ref' is not a recognized source reference")
    decision = eval_queue.get_decision(source_ref)
    if not decision:
        return None, None, (DRAFT_CONFLICT_STATUS, "not_decided",
                            "record a steward decision before drafting a "
                            "golden question")
    source_turn = _resolve_source_turn(source_ref)
    if source_turn is None:
        return None, None, (DRAFT_CONFLICT_STATUS, "source_expired",
                            "the source turn is no longer available")
    if not eval_promote.question_text(source_turn):
        return None, None, (DRAFT_CONFLICT_STATUS, "no_question_text",
                            "this source carries no promotable question text")
    return decision, source_turn, None


@router.post("/queue/golden-draft")
async def post_golden_draft(request: Request, body: DraftRequest):
    """Return ONE transient promoted-golden draft as a download.

    Persists NOTHING (ADR-0062 D5): the draft is built in memory and handed
    straight to the response. The destination is the steward's EXTERNAL,
    customer-owned golden set (``scripts/merge_golden_draft.py``).
    """
    _check_admin(request)
    decision, source_turn, problem = _draft_inputs(body.source_ref)
    if problem is not None:
        status, reason, detail = problem
        # Opaque ref sha only — no question text, no draft body, in any log.
        logger.info("eval_golden_draft_unavailable: reason=%s", reason)
        raise HTTPException(status_code=status,
                            detail={"reason": reason, "detail": detail})
    draft = eval_promote.draft_golden(decision, source_turn)
    return Response(
        content=json.dumps(eval_promote.draft_bundle(draft), indent=2),
        media_type=eval_promote.MEDIA_TYPE,
        headers=eval_promote.download_headers(draft))
