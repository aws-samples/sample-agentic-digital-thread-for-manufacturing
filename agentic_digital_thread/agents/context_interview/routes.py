# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guided-onboarding HTTP surface (T3.1 Wave 2).

Six endpoints, deliberately thin — the decisions live in :mod:`questions`
(which questions) and :mod:`landing` (what happens to an answer), both pure and
unit-tested without HTTP:

* ``GET  /api/onboard/guided/state``            — first-launch + draft + answered
* ``POST /api/onboard/guided/dismiss``          — steward declines the flow
* ``GET  /api/onboard/guided/questions``        — the deployment-scope question set
* ``POST /api/onboard/guided/turn``             — one deployment-scope Q/A
* ``POST /api/onboard/guided/probe/{system_id}``— per-system pre-map questions
* ``POST /api/onboard/guided/confirm-context``  — write the draft BC, batched
* ``GET  /api/onboard/guided/runtime-questions``— pending runtime steward questions
* ``POST /api/onboard/guided/runtime-turn``     — answer one (ADR-0059 D4)

**Why probing is a separate call rather than a hook in ``/map``:** ``/map`` spawns
a background thread and returns a ``job_id`` immediately, with the client polling
for status. Pausing that for human input would put a person inside a polling
contract, risk half-mapped state on abandonment, and expose the 120 s origin cap.
Instead the client probes BETWEEN discover and its own call to ``/map``, so
``/map`` is untouched and a client that never probes behaves exactly as today —
which is also what makes the skip path free rather than special-cased.

This router is mounted on the same ``/api/onboard`` prefix and therefore inherits
its ``require_onboard_access`` dependency; these endpoints are steward-authority
surfaces, not public ones.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..offload import offload, offload_holding
from ..startup_restore import refuse_while_restoring
from . import state
from .landing import (
    HELD_FOR_CONFIRM,
    LAND_BUSINESS_CONTEXT,
    NOT_INGESTED,
    OUTCOME_CONFLICT,
    OUTCOME_DRAFT,
    OUTCOME_PROPOSED,
    OUTCOME_REJECTED,
    OUTCOME_SKIPPED,
    land_answer,
)
from .questions import (MAX_QUESTIONS_PER_SYSTEM, QUESTIONS, QUESTIONS_BY_ID,
                        RUNTIME_QUESTION_TEMPLATES, derive_answers,
                        deployment_questions, schema_facts_from_tables,
                        select_questions)

logger = logging.getLogger(__name__)

# Deliberately declares NO dependencies: ``require_onboard_access`` lives in
# ``onboard`` (importing it here would be circular), so the gate is applied at
# the INCLUDE SITE in that module. Verified end-to-end: an unauthenticated
# request to a guided route returns 401, same as any other onboard route.
router = APIRouter(prefix="/guided", tags=["onboard-guided"])


class TurnRequest(BaseModel):
    question_id: str
    answer: str | None = None
    system_id: str = ""
    concept_id: str = ""


class ProbeAnswersRequest(BaseModel):
    """Schema facts from discover, plus any answers being submitted."""

    schema_facts: dict = {}
    # RAW discover tables, posted verbatim by the wizard (which has no persisted
    # discovery). Mapped server-side through the ONE producer,
    # ``schema_facts_from_tables``, so no client mirrors the fact shape.
    tables: list = []
    answers: list[TurnRequest] = []
    # The caller has no confirm step (the wizard's pre-map card): ask only
    # questions whose answer is filed when given, and refuse a held one
    # (``landing.HELD_FOR_CONFIRM``) rather than keep it where nothing saves it.
    durable_answers_only: bool = False


class ConfirmContextRequest(BaseModel):
    """Explicit steward confirm. ``business_context`` may be an edited draft."""

    systems: list[str]
    business_context: str = ""


def _question_or_400(question_id: str):
    q = QUESTIONS_BY_ID.get(question_id)
    if q is None:
        raise HTTPException(status_code=400, detail="unknown_question_id")
    return q


def _result_payload(res) -> dict:
    payload = {
        "outcome": res.outcome,
        "question_id": res.question_id,
        "landing": res.landing,
    }
    for key in ("reason", "detail", "conflict_with"):
        value = getattr(res, key, "")
        if value:
            payload[key] = value
    if res.proposal_ids:
        payload["proposal_ids"] = res.proposal_ids
    return payload


@router.get("/state")
async def guided_state():
    """Read-only. The frontend consumes this rather than re-deriving the rule."""
    return state.guided_state()


@router.post("/dismiss")
async def guided_dismiss():
    """Decline the guided flow. Write failures propagate (a silently-failed
    dismissal means the steward gets asked again, which is the bug)."""
    state.set_dismissed(True)
    return {"status": "ok", "dismissed": True}


@router.get("/questions")
async def guided_questions():
    """The deployment-scope question list, minus already-answered topics."""
    picked = deployment_questions(state.answered_topics())
    return {"questions": [
        {"id": s.question.id, "prompt": s.question.prompt,
         "treatment_phase": s.question.treatment_phase,
         "examples": list(s.question.examples)}
        for s in picked]}


class ClarifyRequest(BaseModel):
    question_id: str
    answer: str = ""
    system_id: str = ""
    evidence: str = ""


class RefineRequest(BaseModel):
    """One stateless refine ask: the steward's instruction plus the textarea's
    CURRENT text (the server holds no session — back-and-forth is successive
    calls with the evolving text)."""

    system_id: str = ""
    instruction: str = ""
    current_text: str = ""


def _validated_refine_inputs(body: RefineRequest) -> tuple[str, str, str]:
    """Validate + sanitize the refine request; raises HTTPException.

    Both text fields are UNTRUSTED steward input headed for an LLM prompt, so
    they get the same shared treatment as every other such surface (bounded,
    secret-shape-rejected, fence-delimiter-rejected) — detail strings never
    echo the text.
    """
    from ..untrusted_text import sanitize_untrusted, validate_untrusted
    from .refine import MAX_INSTRUCTION_LEN

    system_id = (body.system_id or "").strip()
    known = _known_system_ids()
    if not system_id or (known and system_id not in known):
        raise HTTPException(status_code=404, detail="unknown_system")
    instruction = sanitize_untrusted(body.instruction or "",
                                     max_len=MAX_INSTRUCTION_LEN)
    if not instruction:
        raise HTTPException(status_code=400, detail="empty_instruction")
    verdict = validate_untrusted(instruction, max_len=MAX_INSTRUCTION_LEN,
                                 field="'instruction'", label="instruction")
    if verdict is not None:
        raise HTTPException(status_code=400, detail=verdict[1])
    current = sanitize_untrusted(body.current_text or "")
    verdict = validate_untrusted(current, field="'current_text'",
                                 label="current context")
    if verdict is not None:
        raise HTTPException(status_code=400, detail=verdict[1])
    return system_id, instruction, current


@router.post("/refine")
async def guided_refine(body: RefineRequest):
    """Suggest a REPLACEMENT business context from the steward's instruction.

    STATELESS and read-only: returns ``{"draft", "notes"}`` and writes NOTHING
    — the only write path stays the admin PUT with its secret-shape gate and
    roll warning. Rides this router's include-site ``require_onboard_access``
    gate like every other guided endpoint. Synchronous by design (natural
    rate limit). Model failure is an honest 502, never a fabricated draft.
    """
    from .refine import RefineUnavailable, refine

    system_id, instruction, current = _validated_refine_inputs(body)
    try:
        return refine(system_id, instruction, current)
    except RefineUnavailable as exc:
        raise HTTPException(
            status_code=502,
            detail=f"context assistant unavailable ({exc}) — try again")


@router.post("/clarify")
async def guided_clarify(body: ClarifyRequest):
    """One bounded, goal-anchored clarifying follow-up (T3.2).

    Adaptive DEPTH on a deterministic question SET: the model may probe the
    same topic (max 2 turns per question per system, enforced server-side) and
    every failure degrades to no follow-up. The steward's clarified answer is
    submitted through the normal ``/turn``/``/probe`` path — this endpoint
    never lands anything.
    """
    from .clarifier import clarify

    q = _question_or_400(body.question_id)
    return clarify(q, body.answer, system_id=body.system_id,
                   evidence=body.evidence)


@router.get("/manifest")
async def guided_manifest():
    """Everything this interview session proposes to write, by destination.

    The review step renders this so a steward confirms the FULL blast radius,
    not only the business-context prose: drafts (held until confirm), proposals
    (already pending in the lifecycle — removable via the standard reject
    route), and conflicts (need explicit resolution). Editing stays on the
    existing surfaces; this endpoint only aggregates.
    """
    entries = state.landings()
    return {
        "draft_business_context": state.draft_context(),
        "landings": entries,
        "editable_later": True,
    }


def _onboarding_landing(question, answer, *, system_id: str = "", actor: str = "interview",
                        concept_id: str = ""):
    """``land_answer`` for the onboarding interview (a submitted answer and a
    derived one alike), so no caller can miss the rule below. A nonempty
    answer whose proposal the lifecycle did not file (``skipped`` with
    :data:`NOT_INGESTED`) is ``rejected``: it is not answered, gets no manifest
    row and no displayed text, and is asked or derived again. The steward's own
    empty skip carries no reason and stays answered. The runtime-question route
    keeps ``land_answer``'s own outcome: there a pending question stays pending."""
    res = land_answer(question, answer, system_id=system_id, actor=actor,
                      concept_id=concept_id)
    if res.outcome == OUTCOME_SKIPPED and res.reason == NOT_INGESTED:
        return replace(res, outcome=OUTCOME_REJECTED)
    return res


def _apply_turn(body: TurnRequest) -> dict:
    """Land one answer, updating the draft/answered store. Shared by turn+probe."""
    q = _question_or_400(body.question_id)
    res = _onboarding_landing(q, body.answer, system_id=body.system_id,
                              concept_id=body.concept_id)
    if res.outcome == OUTCOME_DRAFT and q.landing == LAND_BUSINESS_CONTEXT:
        state.append_draft(res.draft_text, max_len=q.max_len)
    # A REJECTED answer must be re-asked (the steward fixes and resubmits). A
    # CONFLICT is a settled outcome — the question was answered, the answer just
    # cannot be applied without deactivating an existing rule — so record it or
    # the steward is asked the same unanswerable question on every reload.
    if res.outcome != OUTCOME_REJECTED:
        state.record_topic(q.topic, body.system_id or "")
        _record_manifest(q, res, body.system_id or "", actor="steward")
    return _result_payload(res)


def _record_manifest(question, res, system_id: str, *, actor: str) -> None:
    """One review-manifest row per non-rejected, non-skipped landing.

    ``clean_text`` is the SANITIZED answer (landing guarantees rejected text
    never reaches it), bounded again here purely for display."""
    if res.outcome not in (OUTCOME_DRAFT, "proposed", OUTCOME_CONFLICT):
        return
    state.record_landing({
        "question_id": res.question_id,
        "topic": question.topic,
        "prompt": question.prompt,
        "landing": res.landing,
        "outcome": res.outcome,
        "system_id": system_id,
        "actor": actor,
        "proposal_ids": list(res.proposal_ids or []),
        "text": (res.clean_text or "")[:400],
        "conflict_with": res.conflict_with or "",
    })


@router.post("/turn")
async def guided_turn(body: TurnRequest):
    """One deployment-scope question/answer exchange."""
    payload = _apply_turn(body)
    payload["draft_business_context"] = state.draft_context()
    return payload


@router.post("/probe/{system_id}")
async def guided_probe(system_id: str, body: ProbeAnswersRequest):
    """Per-system pre-map probing: submit answers, get the remaining questions.

    Called BETWEEN discover and ``/map``. Never blocks mapping — a client may
    ignore this endpoint entirely.
    """
    results = [_probe_answer(answer, system_id, body.durable_answers_only)
               for answer in body.answers or []]
    already = state.answered_topics(system_id) | state.answered_topics()
    # Derive what the schema already answers BEFORE asking anything. This is what
    # makes fully automated onboarding produce real context: an API caller that
    # never answers a question still gets the coverage and query-strategy notes.
    # Pinned precedence: explicit facts > raw body tables > stored discovery.
    if body.schema_facts:
        facts = body.schema_facts
    elif body.tables:
        facts = schema_facts_from_tables(body.tables)
    else:
        facts = _discovered_facts(system_id)
    derived = _land_derived(facts, already, system_id)
    already = already | {d["topic"] for d in derived}
    picked = _probe_questions(facts, already, body.durable_answers_only)
    return {
        "system_id": system_id,
        "results": results,
        "derived": derived,
        "questions": [
            {"id": s.question.id, "prompt": s.question.prompt,
             "evidence": s.evidence,
             "treatment_phase": s.question.treatment_phase}
            for s in picked],
        "draft_business_context": state.draft_context(),
    }


@router.post("/confirm-context", dependencies=[Depends(refuse_while_restoring)])
async def guided_confirm_context(body: ConfirmContextRequest, request: Request):
    """Write the reviewed business context for N systems in ONE batched roll.

    Business context is delivered to system agents as a secret read at container
    start, so each write rolls a runtime. Writing per answer would roll N times
    per system; batching through the shared coordinator rolls each system once,
    serially, with a readiness gate — the T1.5 contract.
    """
    text = (body.business_context or state.draft_context() or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty_business_context")
    systems = [s for s in (body.systems or []) if s]
    if not systems:
        raise HTTPException(status_code=400, detail="no_systems")

    from ..business_context import sanitize_business_context, validate_business_context
    clean = sanitize_business_context(text)
    verdict = validate_business_context(clean)
    if verdict is not None:
        reason, detail = verdict
        raise HTTPException(status_code=400,
                            detail={"reason": reason, "detail": detail})

    report = await _confirm_once(systems, clean, _actor(request))
    all_written = bool(report) and all(v.get("written") for v in report.values())
    return {"status": "ok" if all_written else "partial", "systems": report}


# One guided confirm at a time. A confirm snapshots the draft on the loop and,
# when its offloaded write finishes, clears that prefix. Two overlapping
# confirms would each write their own snapshot (last writer wins on the System
# node) and the earlier one's clear would run against the later one's state,
# so a late answer could be lost while both report ok. Before the offload the
# blocked loop ran confirms one after another; a second confirm is now refused.
# The confirm also claims its systems through business_context.claim_save, so
# an admin PUT of the same system cannot overlap it either.
_CONFIRM_SLOT = threading.Lock()
CONFIRM_IN_FLIGHT_DETAIL = ("A business-context confirm is already saving. "
                            "Wait for it to finish, then confirm again.")


async def _confirm_once(systems: list[str], clean: str, actor: str) -> dict:
    """Take the confirm slot and the systems' save claim, snapshot the draft,
    and run the write off-loop.

    The claims are taken and the snapshot read on the loop with no await
    between them, and released only when the offloaded write is DONE
    (``offload_holding``), not when this coroutine ends: a client disconnect
    cancels the handler but the worker thread keeps writing, and releasing
    early would let the next save overlap it."""
    from ..business_context import SAVE_IN_FLIGHT_DETAIL, claim_save, release_save

    if not _CONFIRM_SLOT.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=CONFIRM_IN_FLIGHT_DETAIL)
    if not claim_save(systems):
        _CONFIRM_SLOT.release()
        raise HTTPException(status_code=409, detail=SAVE_IN_FLIGHT_DETAIL)

    def release() -> None:
        release_save(systems)
        _CONFIRM_SLOT.release()

    try:
        reviewed = (state.draft_context(), state.landing_count())
    except BaseException:
        release()
        raise
    return await offload_holding(release, _confirm_context_write,
                                 systems, clean, actor, *reviewed)


def _confirm_context_write(systems: list[str], clean: str, actor: str,
                           reviewed_draft: str = "", reviewed_landings: int = 0) -> dict:
    """Sync body of the confirm: the graph writes, the batched runtime roll
    (which takes RUNTIME_CONFIG_LOCK and polls READY for up to the roll
    bound), the audit and the draft clear. The route offloads it, so the
    event loop never waits on the lock or the polling. ``reviewed_draft`` and
    ``reviewed_landings`` are the draft and manifest size the steward
    confirmed, snapshotted on the loop: only those are cleared, so an answer
    that lands while this runs is kept for the next confirm."""
    report = _write_context_batched(systems, clean)
    _audit(systems, report, actor=actor, clean=clean)
    if bool(report) and all(v.get("written") for v in report.values()):
        # Only discard the draft once every system actually took the write —
        # otherwise the steward would lose the text with nothing persisted.
        state.clear_confirmed_draft(reviewed_draft)
        state.clear_landings_before(reviewed_landings)
    return report


def _write_context_batched(systems: list[str], clean: str) -> dict:
    """Persist context for each system, then roll all affected runtimes ONCE."""
    from ..business_context import write_business_context

    written: list[str] = []
    report: dict[str, dict] = {}
    known = _known_system_ids()
    for system_id in systems:
        # An unknown id is a caller error, not a silent no-op: reporting it as
        # written would tell a steward their context landed somewhere it did not.
        if known and system_id not in known:
            report[system_id] = {"written": False, "reason": "unknown_system"}
            continue
        try:
            ok = bool(write_business_context(system_id, clean, True))
        except Exception as exc:  # noqa: BLE001 - fail-loud, but never echo detail
            logger.error("guided business-context write error for %s: %s: %s",
                         system_id, type(exc).__name__, exc)
            ok = False
        if ok:
            written.append(system_id)
            report[system_id] = {"written": True}
        else:
            # write_business_context returns False on a dropped graph write; the
            # admin PUT treats that as a 500, so this path must not report ok.
            report[system_id] = {"written": False, "reason": "write_failed"}
    if written:
        for system_id, outcome in _roll(written).items():
            report.setdefault(system_id, {})["roll"] = outcome
    return report


def _known_system_ids() -> set[str]:
    """Registered system ids, or an empty set when the catalog is unreadable
    (in which case validation is skipped rather than rejecting everything)."""
    try:
        from ..catalog import catalog
        return {s.get("id") for s in catalog.get_all() if s.get("id")}
    except Exception:  # nosec B110 - unreadable catalog disables validation only
        return set()


# Same bound the BC PUT uses: the readiness gate must return BEFORE the CloudFront
# 120 s origin cap, so a slow roll is an honest partial rather than a stranded 504.
_ROLL_TIMEOUT_SECONDS = 100


def _roll(systems: list[str]) -> dict:
    """Roll all affected runtimes through the ONE batched serial coordinator.

    Mirrors ``admin_routes._refresh_business_context_batched``: resolve the
    manager, feature-detect the coordinator (older/injected managers lack it),
    and treat a coordinator failure as a reported failure that NEVER rolls back
    the durable graph write. Absence is reported as ``unknown`` rather than
    implied success — a context write that never reached a runtime is exactly the
    failure this path exists to avoid.
    """
    try:
        from ..admin_routes import _get_agent_manager
        mgr = _get_agent_manager()
    except Exception as exc:  # nosec B110 - manager absence is reported, not fatal
        logger.warning("agent manager unavailable for guided roll: %s", exc)
        return {s: "unknown" for s in systems}
    batched = getattr(mgr, "refresh_systems_batched", None)
    if not callable(batched):
        return {s: "unknown" for s in systems}
    try:
        report = batched(systems, reason="guided onboarding confirm",
                         artifact_classes=("business_context",),
                         timeout_seconds=_ROLL_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - durable write already landed
        logger.error("guided batched roll error: %s: %s", type(exc).__name__, exc)
        return {s: "failed" for s in systems}
    out = {}
    for system_id in systems:
        entry = dict((report or {}).get(system_id) or {})
        status = entry.get("status")
        out[system_id] = status if status in ("ok", "failed", "skipped") else "failed"
    return out


def _actor(request) -> str:
    """The authenticated user's email, matching the admin PUT's attribution.

    A constant actor makes a guided write untraceable to a person, which defeats
    the point of auditing it.
    """
    user = getattr(getattr(request, "state", None), "user", None)
    return getattr(user, "email", None) or "unknown"


def _audit(systems: list[str], report_ok, *, actor: str, clean: str = "") -> None:
    """Mirror the admin PUT's connection-event audit so a guided write is as
    traceable as a manual one. Best-effort: auditing must never block the write.

    ``ok`` outcomes carry the same forensic extras as the admin PUT
    (admin_routes.py) — context_hash/enabled/length over the SANITIZED text —
    so a guided write is diffable against a manual one. Error outcomes carry
    no extra, matching the admin error path.
    """
    try:
        from .. import connection_resolver as cr
        from ..business_context import context_hash
        ok_extra = {"context_hash": context_hash(clean), "enabled": True,
                    "length": len(clean)}
    except Exception:  # nosec B110 - audit is best-effort
        return
    for system_id in systems:
        outcome = "ok" if (report_ok.get(system_id) or {}).get("written") else "error"
        try:
            if outcome == "ok":
                cr.audit_connection_event("business_context", system_id,
                                          actor=actor, outcome=outcome,
                                          extra=ok_extra)
            else:
                cr.audit_connection_event("business_context", system_id,
                                          actor=actor, outcome=outcome)
        except Exception:  # nosec B110 - never block on audit
            pass


def _land_derived(schema_facts: dict, already: set, system_id: str) -> list[dict]:
    """Land machine-derived answers through the SAME landing path as human ones.

    Derived answers are still proposals/drafts — never direct writes — so a
    steward reviews them exactly as they would an interview answer. Deriving does
    not lower the bar; it only removes the typing.
    """
    out = []
    for d in derive_answers(schema_facts, already):
        res = _onboarding_landing(d.question, d.text, system_id=system_id,
                                  actor="derived")
        if res.outcome == OUTCOME_DRAFT:
            state.append_draft(res.draft_text, max_len=d.question.max_len)
        if res.outcome != OUTCOME_REJECTED:
            state.record_topic(d.question.topic, system_id)
            _record_manifest(d.question, res, system_id, actor="derived")
        entry = _result_payload(res)
        entry["topic"] = d.question.topic
        # A REJECTED landing reports the outcome + reason code ONLY — the
        # pre-sanitization text must never be reflected back in a response.
        if res.outcome != OUTCOME_REJECTED:
            entry["evidence"] = d.evidence
            entry["derived_text"] = d.text
        out.append(entry)
    return out


#: Why a held answer is refused from a caller without a confirm step.
NOT_KEPT_HERE = ("not_kept_here", "this answer is kept only after a confirm step; "
                 "give it in the onboarding chat or the system's business context")


def _probe_answer(answer: TurnRequest, system_id: str, durable_only: bool) -> dict:
    """Land one probe answer, or the reason it was not. A held answer from a
    caller without a confirm step is refused, not held (:data:`NOT_KEPT_HERE`).
    A bad entry is reported and the batch goes on: raising would discard the
    results of answers that already landed earlier in it."""
    answer.system_id = answer.system_id or system_id
    q = QUESTIONS_BY_ID.get(answer.question_id)
    if durable_only and q is not None and q.landing in HELD_FOR_CONFIRM:
        reason, detail = NOT_KEPT_HERE
        return {"outcome": "rejected", "question_id": answer.question_id,
                "reason": reason, "detail": detail}
    try:
        return _apply_turn(answer)
    except HTTPException as exc:
        return {"outcome": "rejected", "question_id": answer.question_id,
                "reason": str(exc.detail)}


def _probe_questions(facts: dict, already: set, durable_only: bool) -> list:
    """The per-system questions to ask; for a caller without a confirm step,
    only those whose answer is filed when given, still capped."""
    if not durable_only:
        return select_questions(facts, already)
    picked = select_questions(facts, already, limit=len(QUESTIONS))
    return [s for s in picked
            if s.question.landing not in HELD_FOR_CONFIRM][:MAX_QUESTIONS_PER_SYSTEM]


def _discovered_facts(system_id: str) -> dict:
    """Build fact input from the system's OWN stored discovery.

    REACH LIMITS, measured — do not overstate this:
    * Only systems discovered through the AGENT tool path persist
      ``discovered_tables``; the wizard's HTTP discover does not, so
      wizard-onboarded systems derive nothing from this route and must pass
      ``schema_facts`` explicitly.
    * The session store has a 7-day TTL, so derivation is unavailable for a
      system discovered long ago.
    * Persisting backends (PostgreSQL/OpenAPI/MCP/i3X) emit no ``sample_values``,
      and some use ``row_count`` for a column/instance count rather than rows —
      the producer tolerates every shape, but the value can be a weak signal.

    Without this, ``/guided/probe`` required a client to supply a fact shape no
    shipped code produced, so derivation could never run — the reachability
    defect this closes. Reading the stored discovery server-side means an API
    caller needs to send nothing at all.
    """
    empty = {"columns": [], "row_estimate": 0}
    try:
        from ..onboard_agent import SessionNotFoundError, _store
    except Exception as exc:  # noqa: BLE001 - store unavailable, log and degrade
        logger.warning("guided probe: session store unavailable: %s", exc)
        return empty
    try:
        session = _store().get_session(system_id) or {}
    except SessionNotFoundError:
        # Legitimate and common: nothing discovered yet. Not an error.
        return empty
    except Exception as exc:  # noqa: BLE001 - throttling/auth must not look like "no data"
        logger.error("guided probe: discovery read failed for %s: %s: %s",
                     system_id, type(exc).__name__, exc)
        return empty
    return schema_facts_from_tables(session.get("discovered_tables") or [])


# --------------------------------------------------------------------------- #
# RUNTIME questions (ADR-0059 D4) — the interview continues after onboarding.
#
# The failure-signal sweep files pending ``steward_question`` proposal rows
# (failure_signals.sweep_and_file_questions); these two endpoints are the
# answering surface. They ride this router, so they inherit the include-site
# ``require_onboard_access`` gate like every other guided endpoint.
#
# Answering is NOT the accept action (that is acknowledge-only, ADR-0059 D3):
# the answer routes through the SAME ``land_answer`` machinery as onboarding
# answers — sanitized at write, landing as pending proposals, never applied —
# and only a LANDED answer supersedes the question row
# (``superseded_reason="answered"`` + the landed proposal ids), via a
# steward-wins conditional write (still pending AND machine-sourced; a row the
# steward already accepted/rejected is never machine-mutated).
# --------------------------------------------------------------------------- #

class RuntimeTurnRequest(BaseModel):
    question_id: str
    answer: str | None = None


def _pending_steward_questions() -> list[dict]:
    """Pending ``steward_question`` proposal rows, oldest first (stable)."""
    from ..self_learning import get_all_mappings

    rows = [m for m in (get_all_mappings() or [])
            if isinstance(m, dict)
            and m.get("proposed_type") == "steward_question"
            and m.get("status") == "pending"]
    rows.sort(key=lambda m: (str(m.get("discovered_at", "")),
                             str(m.get("mapping_id", ""))))
    return rows


def _question_view(row: dict) -> dict:
    """The UI shape for one pending question row.

    ``requirement_terms`` is present only for ``context_not_reflected`` rows
    carrying the bounded structured fields written by ``failure_signals``.
    The prompt remains display-only and is never parsed to recover authority.
    """
    view = {
        "id": row.get("mapping_id", ""),
        "system": row.get("from_system", ""),
        "shape": row.get("reason", ""),
        "prompt": row.get("to_description", "") or row.get("description", ""),
        "filed_at": row.get("discovered_at", ""),
    }
    if view["shape"] == "context_not_reflected":
        from ..failure_signals import _directive_term
        asserted = _directive_term(row.get("from_value"))
        negated = _directive_term(row.get("to_value"))
        if negated:
            view["requirement_terms"] = {
                "asserted": asserted, "negated": negated}
    return view


def runtime_questions():
    """The pending runtime steward questions, shaped for the answering UI.
    Sync body (the proposal-store scan); the route offloads it."""
    return {"questions": [_question_view(r) for r in _pending_steward_questions()]}


@router.get("/runtime-questions")
async def runtime_questions_route():
    """Pending runtime steward questions. Sync body: ``runtime_questions``."""
    return await offload(runtime_questions)


def _runtime_question_for_row(row: dict):
    """Recompose the landing Question for a filed row. The template (keyed by
    the row's shape) supplies the LANDING and bounds; the row supplies the
    composed prompt — so the answer lands exactly where the composer intended
    (ADR-0059 D4: no bespoke answer path). Unknown shape ⇒ None (a legacy or
    hand-crafted row this surface does not model)."""
    template = RUNTIME_QUESTION_TEMPLATES.get(str(row.get("reason", "")))
    if template is None:
        return None
    prompt = row.get("to_description", "") or row.get("description", "")
    return replace(template,
                   id=f"{template.id}-{str(row.get('mapping_id', ''))[:12]}",
                   prompt=prompt or template.prompt)


def _supersede_answered(row: dict, proposal_ids: list[str]) -> bool:
    """Mark an answered question row superseded — the machine's only legal
    terminal (ADR-0059 D4). STEWARD-WINS: conditional on the row still being
    ``pending`` AND machine-sourced (the ``sweep_resolved_gaps`` idiom); a
    lost condition means a steward decided first, and their decision stands.
    Returns True iff this write flipped the row."""
    from ..self_learning import _get_table

    try:
        _get_table().update_item(
            Key={"mapping_id": row["mapping_id"],
                 "discovered_at": row["discovered_at"]},
            UpdateExpression=("SET #s = :sup, superseded_reason = :why, "
                              "superseded_at = :now, answer_proposal_ids = :pids"),
            ConditionExpression="#s = :pending AND #src = :agent",
            ExpressionAttributeNames={"#s": "status", "#src": "source"},
            ExpressionAttributeValues={
                ":sup": "superseded", ":pending": "pending", ":agent": "agent",
                ":why": "answered",
                ":now": datetime.now(timezone.utc).isoformat(),
                ":pids": list(proposal_ids),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the ANSWER already landed durably
        # ConditionalCheckFailed = steward-wins (expected); anything else is a
        # store fault. Either way the landed proposals are the durable outcome
        # and failing the request now would tell the steward their answer was
        # lost when it was not — report honestly instead.
        logger.warning("runtime-question supersede skipped for %s: %s",
                       row.get("mapping_id"), type(exc).__name__)
        return False


def runtime_turn(body: RuntimeTurnRequest):
    """Answer ONE runtime steward question.

    The answer lands through ``land_answer`` (sanitize/validate, pending
    proposals only); landing outcomes — including rejection reasons — are
    relayed verbatim in the ``_result_payload`` shape. Only a LANDED
    (``proposed``) answer supersedes the question; a rejected answer is
    re-askable and a skip/gated landing leaves the question pending.
    Sync body (proposal-store scan, landing and supersede writes); the route
    offloads it. ``_RUNTIME_TURN_LOCK`` keeps two answers from interleaving.
    """
    with _RUNTIME_TURN_LOCK:
        row = next((r for r in _pending_steward_questions()
                    if r.get("mapping_id") == body.question_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown_question_id")
        q = _runtime_question_for_row(row)
        if q is None:
            raise HTTPException(status_code=400, detail="unmodeled_question_shape")
        res = land_answer(q, body.answer, system_id=row.get("from_system", ""),
                          concept_id=str(row.get("concept", "") or ""))
        payload = _result_payload(res)
        payload["question_id"] = body.question_id  # the ROW id, not the composed id
        payload["question_superseded"] = (
            res.outcome == OUTCOME_PROPOSED
            and _supersede_answered(row, res.proposal_ids))
        return payload


#: One runtime answer at a time. Reading the pending question, landing the
#: answer and superseding the question is a check-then-act: inline on the
#: event loop a double-submitted answer was serialised, so the second saw
#: the question gone (404); in the offload pool both would read it pending
#: and land the answer twice.
_RUNTIME_TURN_LOCK = threading.Lock()


@router.post("/runtime-turn")
async def runtime_turn_route(body: RuntimeTurnRequest):
    """Answer one runtime steward question. Sync body: ``runtime_turn``."""
    return await offload(runtime_turn, body)
