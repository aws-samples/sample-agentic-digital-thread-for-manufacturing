# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Steward routes for the calendar policy (``agents.calendar_policy``).

``GET  /api/admin/calendar-policy`` — steward only, READ-ONLY: the stored
policy (``null`` when none) plus the two rules' plain labels.

``POST /api/admin/calendar-policy`` — steward only; body ``{"bare_date",
"relative_days", "site_day_start", "sites": [{"site", "timezone",
"day_start"?}]}`` (``extra="forbid"``, strict strings). Validated by
``calendar_policy.parse_policy``: 400 ``{"error": "invalid_policy", "errors":
[...]}`` naming every problem, 422 for a malformed body. Written through
``calendar_policy.write_policy`` (MERGE, exact readback, audit stamps): 200
``{"status": "ok", "policy": ...}``, 503 ``{"status": "failed"}`` when the
readback does not confirm the write.

``POST /api/admin/calendar-policy/clear`` — steward only: remove the policy
(back to today's behaviour, nothing resolved).

The ``/api/admin`` prefix is admin-gated by ``AuthMiddleware``; each route
also carries the router steward gate (fail-closed). EVENT-LOOP OFFLOAD
PATTERN (``agents/offload.py``): the sync bodies are the module-level
:func:`calendar_policy_read` / :func:`calendar_policy_set` /
:func:`calendar_policy_clear`; each async handler only awaits ``offload``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from . import calendar_policy as cp
from .offload import offload
from .steward_gate import check_steward

router = APIRouter(prefix="/api/admin")


class SiteZoneBody(BaseModel):
    """One site's time zone, with an optional day start override."""

    model_config = ConfigDict(extra="forbid")

    site: StrictStr = Field(max_length=128)
    timezone: StrictStr = Field(max_length=128)
    day_start: StrictStr | None = Field(default=None, max_length=16)


class CalendarPolicyBody(BaseModel):
    """The whole policy (a write replaces the stored one)."""

    model_config = ConfigDict(extra="forbid")

    bare_date: StrictStr = Field(max_length=32)
    relative_days: StrictStr = Field(max_length=32)
    site_day_start: StrictStr = Field(default="00:00", max_length=16)
    sites: list[SiteZoneBody] = Field(default_factory=list, max_length=cp.MAX_SITES + 1)


def _actor(request: Request) -> str:
    user = getattr(getattr(request, "state", None), "user", None)
    return str(getattr(user, "email", "") or getattr(user, "username", "") or "unknown")


def calendar_policy_read():
    """Sync body: the stored policy (``None`` when there is none). A failed
    graph read is a 503, never 'no policy'."""
    try:
        policy = cp.read_policy()
    except Exception:  # noqa: BLE001 - reported as a fixed category, never the text
        return JSONResponse(status_code=503, content={"error": "graph_unavailable"})
    return {"policy": policy.to_dict() if policy else None, "rules": dict(cp.RULE_LABELS)}


@router.get("/calendar-policy", dependencies=[Depends(check_steward)])
async def calendar_policy_read_route():
    return await offload(calendar_policy_read)


def calendar_policy_set(body: CalendarPolicyBody, request: Request):
    """Sync body: validate, then write with readback."""
    try:
        policy = cp.parse_policy(body.model_dump())
    except cp.PolicyError as err:
        return JSONResponse(status_code=400,
                            content={"error": "invalid_policy", "errors": err.errors})
    result = cp.write_policy(policy, _actor(request))
    if result["status"] != "ok":
        return JSONResponse(status_code=503, content=result)
    return result


@router.post("/calendar-policy", dependencies=[Depends(check_steward)])
async def calendar_policy_set_route(body: CalendarPolicyBody, request: Request):
    return await offload(calendar_policy_set, body, request)


def calendar_policy_clear(request: Request):
    """Sync body: remove the policy and confirm it is gone."""
    result = cp.clear_policy(_actor(request))
    if result["status"] != "cleared":
        return JSONResponse(status_code=503, content=result)
    return result


@router.post("/calendar-policy/clear", dependencies=[Depends(check_steward)])
async def calendar_policy_clear_route(request: Request):
    return await offload(calendar_policy_clear, request)
