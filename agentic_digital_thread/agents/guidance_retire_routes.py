# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Steward guidance retire-by-digest routes (ADR-0063 D7, ADR-0052).

``GET /api/admin/guidance/{system_id}/digests`` — steward only, READ-ONLY:
the per-note digest listing (``guidance_retire.list_digests``).

``POST /api/admin/guidance/{system_id}/retire`` — steward only; body
``{"digest": <12 hex>, "expected_current_digest": "sha256:<64 hex>"}``
(``extra="forbid"``, strict strings). Delegates to
``guidance_retire.retire_by_digest`` (the EXISTING prune saga + exact
graph/delivery readback). 200 ``retired``; 404 unknown system/note; 409
stale digest / ambiguous digest / pending saga / readback mismatch; 422
malformed body or a ``system_id`` that fails ``db.validate_system_id``.

EVENT-LOOP OFFLOAD PATTERN (``agents/offload.py``): the sync bodies are the
module-level :func:`guidance_digests` / :func:`guidance_retire`; each async
handler only awaits ``offload`` (Neptune + DynamoDB I/O).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from . import db
from . import guidance_retire as retire
from .offload import offload
from .steward_gate import check_steward

router = APIRouter(prefix="/api/admin/guidance")


class RetireRequest(BaseModel):
    """Exactly the note digest plus the fresh opaque current-state digest."""

    model_config = ConfigDict(extra="forbid")

    digest: StrictStr = Field(pattern=retire.DIGEST_PATTERN)
    expected_current_digest: StrictStr = Field(pattern=retire.CURRENT_DIGEST_PATTERN)


def _respond(result: dict):
    body = dict(result)
    status = body.pop("http_status", 200)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


def _invalid_system_id(system_id: str):
    """422 when *system_id* fails the repo's ONE namespace-key validator."""
    try:
        db.validate_system_id(system_id)
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "invalid_system_id"})
    return None


def guidance_digests(system_id: str):
    """Sync body: the read-only digest listing."""
    return _invalid_system_id(system_id) or _respond(retire.list_digests(system_id))


@router.get("/{system_id}/digests", dependencies=[Depends(check_steward)])
async def guidance_digests_route(system_id: str):
    return await offload(guidance_digests, system_id)


def guidance_retire(system_id: str, body: RetireRequest):
    """Sync body: retire one note by digest through the existing saga."""
    return _invalid_system_id(system_id) or _respond(retire.retire_by_digest(
        system_id, body.digest, body.expected_current_digest))


@router.post("/{system_id}/retire", dependencies=[Depends(check_steward)])
async def guidance_retire_route(system_id: str, body: RetireRequest):
    return await offload(guidance_retire, system_id, body)
