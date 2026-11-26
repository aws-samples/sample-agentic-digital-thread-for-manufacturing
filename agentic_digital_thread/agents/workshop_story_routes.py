# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Facilitator workshop-story routes (ADR-0085, ADR-0089).

``GET /api/admin/workshop/story`` — steward (admin) only, READ-ONLY. Returns
the deployed story's answer key (``agents.demo.story_lookup.lookup``) so a
facilitator can check an attendee's answer against this account's data.
404 when workshop mode is off, so a demo deploy exposes no new surface.

``POST /api/admin/workshop/story/clock`` — steward only. Places the live
story clock (``agents.demo.story_clock.place``: a dry-run preview, or ONE
RequestResponse invoke of the story-history function, which re-plans the era
CAS-guarded). 404 when workshop mode is off, 409 unless the deployed story is
live; the placement's own statuses (404/409/422/502) pass through.

EVENT-LOOP OFFLOAD PATTERN (``agents/offload.py``): the sync bodies are the
module-level :func:`workshop_story` / :func:`workshop_story_clock`; each async
handler only awaits ``offload`` (database connections / a Lambda invoke). Pinned by
``tests/unit/test_workshop_story_routes.py`` and the repo-wide tripwire in
``tests/unit/test_event_loop_offload.py``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .demo import story_clock, story_lookup, workshop
from .offload import offload
from .steward_gate import check_steward

router = APIRouter(prefix="/api/admin/workshop")


def workshop_story(request: Request) -> dict:
    """Sync body: 404 unless workshop mode is on, else the story lookup."""
    if not workshop.is_enabled():
        raise HTTPException(status_code=404, detail="Workshop mode is not enabled")
    return story_lookup.lookup(workshop.story())


@router.get("/story", dependencies=[Depends(check_steward)])
async def workshop_story_route(request: Request):
    return await offload(workshop_story, request)


def workshop_story_clock(request: Request, body: story_clock.PlaceClockRequest) -> dict:
    """Sync body: 404 unless workshop mode is on, 409 unless the story is live,
    else the placement (preview or apply)."""
    if not workshop.is_enabled():
        raise HTTPException(status_code=404, detail="Workshop mode is not enabled")
    if workshop.story() != story_clock.LIVE_STORY:
        raise HTTPException(status_code=409,
                            detail=f"the story clock exists only in the {story_clock.LIVE_STORY} story")
    return story_clock.place(body)


@router.post("/story/clock", dependencies=[Depends(check_steward)])
async def workshop_story_clock_route(request: Request, body: story_clock.PlaceClockRequest):
    return await offload(workshop_story_clock, request, body)
