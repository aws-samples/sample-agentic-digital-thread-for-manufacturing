# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import mode_a_integrity_enforced
from .memory_client import MemoryClient, MemoryRecordNotFoundError, MemoryUnavailableError
from .memory_labels import strip_turn_preambles
from .memory_turns import (display_blocks, question_turns, with_reload_charts,
                           with_route_contexts, with_trace_telemetry)
from .offload import offload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory")
_client = MemoryClient()


def _user_id(request: Request):
    user = getattr(request.state, "user", None)
    return user.user_id if user else None


def _route_contexts(user_id: str, session_id: str, tool_ids) -> dict:
    """The route context each of these calls was sent (``route_context_store``)."""
    from . import route_context_store
    return route_context_store.for_session(user_id, session_id, tool_ids)


def _trace_telemetry(session_id: str, tool_ids) -> dict:
    """The telemetry coverage and sample lines the system runtime stored for
    each of these calls of the user's own session (``investigation_trace_store.
    telemetry_for``)."""
    from . import investigation_trace_store
    return investigation_trace_store.telemetry_for(session_id, tool_ids)


def _with_kept_context(shown: list, events: list, user_id: str, session_id: str) -> list:
    """``shown`` with what the live chat kept for each system delegation, for
    an ungated reload only: ``route_context``, the ROUTE block the gateway
    sent the system (agents.route_context_store), and ``telemetry_coverage``
    and ``sample_line``, the telemetry lines its runtime stored under the
    call's capability, shown above and below the summary and never read from
    the result's text (agents.investigation_trace_store)."""
    shown = with_route_contexts(shown, lambda ids: _route_contexts(user_id, session_id, ids))
    return with_trace_telemetry(shown, events, lambda ids: _trace_telemetry(session_id, ids))


@router.get("/events")
async def get_events_route(request: Request, session_id: str):
    """A session's turns as the chat replays them. Sync body: ``get_events``."""
    return await offload(get_events, request, session_id)


def get_events(request: Request, session_id: str):
    """``GET /api/memory/events``, run off the event loop (``get_events_route``):
    the AgentCore read, the route-context reads and the kept-evidence read are
    all blocking calls."""
    user_id = _user_id(request)
    if not user_id:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    try:
        events = _client.get_events(session_id=session_id, user_id=user_id)
    except MemoryUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"detail": "memory temporarily unavailable", "error": True},
        )
    # Display boundary: the persisted interactive user turn is the COMPOSED
    # ``{route seed}\n\n---\n\n[Temporal context: ...] {question}`` blob —
    # rehydrated history must show the question the user typed, never the
    # seed. strip_turn_preambles is display-only (stored memory stays raw)
    # and returns '' for a seed-only turn, which is dropped rather than
    # rendered as an empty user bubble.
    messages = _reloaded_messages(events, user_id, session_id)
    return {
        "messages": messages,
        "sessionId": session_id,
        "totalCount": len(messages),
    }


def _reloaded_messages(events, user_id: str, session_id: str) -> list:
    """The session's messages as the chat replays them (``get_events``)."""
    # ``blocks`` (agents.memory_turns.display_blocks): the turn's ordered
    # text / tool_use / tool_result pieces, so the chat rebuilds a reopened
    # session the way it rendered live (Reasoning trace, numbered Sources,
    # follow-up chips). A turn the response-integrity gate ran for gets only
    # what the gated stream showed: tool names and metadata-only result
    # summaries, no narration, inputs or answer text, in its blocks AND its
    # display content. The gate decision is the live chat's own
    # (config.mode_a_integrity_enforced). An ungated delegation's tool result
    # also carries what the live chat kept for the call (_with_kept_context).
    # A seeded question carries ``prelude``: the Knowledge Graph step the live
    # trace showed, rebuilt from the stored seed (memory_turns.seed_step_blocks;
    # the seed itself is never sent). An answered question also carries
    # ``provenance``: the metadata frame its live stream sent, which the chat's
    # Sources chips open their evidence cards from (agents.turn_evidence_store;
    # this actor and session only). A tool result's ``charts``: the stored
    # chart tags its turn's stream sent the browser, as the app recorded it
    # (memory_turns.with_reload_charts); a question whose turn's charts are not
    # all shown carries ``charts_not_shown``.
    enforced = mode_a_integrity_enforced()
    shown = display_blocks(events, enforced=enforced)
    if not enforced:
        shown = _with_kept_context(shown, events, user_id, session_id)
    turns = question_turns(events, shown, enforced=enforced)
    kept = _kept_evidence(user_id, session_id, turns)
    shown, marked = _with_sent_charts(shown, events, turns, kept)
    return [m for m in (_shown_message(event, view, kept.get(i, {}).get("provenance"),
                                       charts_not_shown=i in marked)
                        for i, (event, view) in enumerate(zip(events, shown))) if m]


def _kept_evidence(user_id: str, session_id: str, turns: dict) -> dict:
    """``{question index: {"provenance": frame, "charts": record}}``: what the
    app kept of each answered turn's stream, for this actor and session
    (``agents.turn_evidence_store``), on the one question with the same turn
    identity (its question and the calls it replays, ``question_turns``). A
    turn shown gated only takes a frame its gated stream sent, and no chart
    record. One read; ``{}`` on any miss."""
    from . import turn_evidence_store
    return turn_evidence_store.kept_for_turns(user_id, session_id, turns)


def _with_sent_charts(shown, events, turns: dict, kept: dict):
    """(``shown`` with the charts each turn's stream sent, the questions
    marked) (``memory_turns.with_reload_charts``). A failure sends no chart
    and marks nothing: the reload still opens."""
    records = {i: k["charts"] for i, k in kept.items() if k.get("charts")}
    try:
        return with_reload_charts(shown, events, turns, records)
    except Exception:  # noqa: BLE001 - a reload without charts is the fallback
        logger.exception("reload charts not attached")
        return shown, set()


def _shown_message(event, view, provenance: "dict | None" = None, *,
                   charts_not_shown: bool = False) -> "dict | None":
    """One event as the chat replays it, or ``None`` for a pure-preamble user
    turn (nothing the user actually typed). ``prelude`` rides only on a
    question that has one, ``provenance`` (the turn's kept metadata
    frame, which the chat replays before the turn's end) only on a question
    that has one, and ``charts_not_shown`` only on a question whose turn's
    charts the reload does not all show, so every other message keeps its
    shape."""
    content = strip_turn_preambles(event.content) if event.role == "user" else event.content
    if event.role == "user" and event.content and not content:
        return None
    if view.label is not None:
        content = view.label
    message = {"role": event.role, "content": content, "timestamp": event.timestamp,
               "blocks": view.blocks}
    if view.prelude:
        message["prelude"] = view.prelude
    if provenance:
        message["provenance"] = provenance
    if charts_not_shown:
        message["charts_not_shown"] = True
    return message


@router.get("/sessions")
async def list_sessions(request: Request):
    user_id = _user_id(request)
    if not user_id:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    try:
        sessions = _client.list_sessions(user_id=user_id)
    except MemoryUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"detail": "memory temporarily unavailable", "error": True},
        )
    return {
        "sessions": [
            {"sessionId": s.session_id, "label": s.label, "createdAt": s.created_at}
            for s in sessions
        ],
        "count": len(sessions),
    }


@router.get("/semantic")
async def get_semantic(request: Request, session_id: str, type: str = "facts"):
    user_id = _user_id(request)
    if not user_id:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    memory_type = type if type in ("facts", "summaries", "preferences") else "facts"
    try:
        facts = _client.get_semantic(session_id=session_id, user_id=user_id, memory_type=memory_type)
    except MemoryUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"detail": "memory temporarily unavailable", "error": True},
        )
    return {
        "items": [
            {
                "id": f.fact_id,
                "content": f.content,
                "structured": f.structured,
                "confidence": f.confidence,
                "createdAt": f.timestamp,
            }
            for f in facts
        ],
        "sessionId": session_id,
        "type": memory_type,
        "count": len(facts),
    }


@router.delete("/record")
async def delete_record(request: Request, record_id: str):
    """Remove ONE semantic memory record (fact / summary / preference) the
    user judged unhelpful. Auth mirrors the sibling memory routes (401 without
    a user); deletion is scoped to the CURRENT user's namespaces inside
    ``MemoryClient.delete_record`` — a record belonging to another actor (or
    absent) returns 404 with no existence leak."""
    user_id = _user_id(request)
    if not user_id:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    try:
        _client.delete_record(record_id=record_id, user_id=user_id)
    except MemoryRecordNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "record not found"})
    except MemoryUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"detail": "memory temporarily unavailable", "error": True},
        )
    return {"deleted": True, "id": record_id}
