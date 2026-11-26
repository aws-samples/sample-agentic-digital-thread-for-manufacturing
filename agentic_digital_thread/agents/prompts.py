# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt library API — CRUD, execution, synthesis, scheduling, and registry seed."""

import logging
import re
import os
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter
from pydantic import BaseModel

from . import demo
from .storage import DashboardCardStorage, PromptTemplateStorage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

_prompt_store: PromptTemplateStorage | None = None
_card_store: DashboardCardStorage | None = None
_scheduler: AsyncIOScheduler | None = None
_execute_fn = None  # callable(prompt_text: str) -> str
_video_fn = None    # callable(prompt_id: str, prompt_text: str) -> str
SCHEDULER_BUSY_RESULT = "[Skipped — user query in progress]"


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init(prompt_table: str | None, card_table: str | None, execute_fn, video_fn=None):
    global _prompt_store, _card_store, _scheduler, _execute_fn, _video_fn
    _prompt_store = PromptTemplateStorage(prompt_table)
    _card_store = DashboardCardStorage(card_table)
    _execute_fn = execute_fn
    _video_fn = video_fn
    _scheduler = AsyncIOScheduler()

    # Re-register any existing scheduled prompts
    for prompt in _prompt_store.list_all():
        if prompt.get("schedule"):
            _add_scheduler_job(prompt["template_id"], prompt["schedule"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SavePromptRequest(BaseModel):
    text: str
    title: str = ""
    category: str = "general"
    required_systems: list[str] = []
    pinned: bool = False
    schedule: str | None = None


class SynthesizeRequest(BaseModel):
    messages: list[dict]


class ScheduleRequest(BaseModel):
    schedule_text: str


class PromptRunRequest(BaseModel):
    """Optional body of a browser-triggered run (execute, pinned-card refresh):
    the browser's IANA zone and locale, validated where the time line renders
    (agents.time_context); invalid or absent values give the UTC-only line."""
    timezone: str | None = None
    locale: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bedrock_client():
    # Region comes from AWS_REGION (was pinned to us-east-1 here, which broke
    # any deployment in another region) — see aws_session.bedrock_runtime_client.
    from .aws_session import bedrock_runtime_client
    return bedrock_runtime_client()


def _invoke_llm(system_prompt: str, user_content: str) -> str:
    from .model_catalog import env_orchestrator_model
    model = env_orchestrator_model()
    from .db import converse_text
    return converse_text(_bedrock_client(), model, user_content,
                         system=system_prompt, max_tokens=1024).strip()


def _add_scheduler_job(template_id: str, cron: str):
    job_id = f"prompt_{template_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    minute, hour, dom, month, dow = cron.split()
    _scheduler.add_job(
        _execute_scheduled_prompt,
        CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow),
        id=job_id,
        args=[template_id],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Fixed namespace for deriving *stable* registry template_ids. Registry prompts
# are re-seeded on every app boot (see seed_registry); using a random uuid4 per
# seed meant concurrent task boots / repeated seeds accumulated duplicate rows
# (same persona+title+text, different ids) that a template_id dedupe could not
# collapse. Deriving the id deterministically from the prompt's stable content
# makes seeding idempotent — a re-seed put()s the same primary key, so the row
# is overwritten rather than duplicated, and concurrent seeds converge.
_REGISTRY_NS = uuid.UUID("6f9b1e6a-1c2d-4e3f-8a5b-ad700e600001")


def _registry_template_id(entry: dict) -> str:
    """Deterministic template_id for a registry prompt, from its stable content."""
    key = "|".join(
        str(entry.get(k, "")) for k in ("persona", "category", "title", "text")
    )
    return str(uuid.uuid5(_REGISTRY_NS, key))


def _dedupe_by_template_id(items: list[dict]) -> list[dict]:
    """Collapse rows sharing a template_id; prefer a registry row on collision.

    Defensive de-duplication for the prompt library. With deterministic registry
    ids (see _registry_template_id) genuine duplicates now share a template_id, so
    this collapses any residual dup rows left over from the pre-fix random-id era
    until the next re-seed prunes them. User-created customs carry their own unique
    ids and are always preserved. On a template_id collision we keep the registry
    copy (canonical) over any other source.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for it in items:
        tid = it.get("template_id")
        if tid is None:
            # No key to dedupe on — keep as-is under a synthetic slot.
            order.append(it)  # type: ignore[arg-type]
            continue
        prev = by_id.get(tid)
        if prev is None:
            by_id[tid] = it
            order.append(tid)
        elif it.get("source") == "registry" and prev.get("source") != "registry":
            by_id[tid] = it  # prefer registry copy on collision
    out: list[dict] = []
    for slot in order:
        out.append(slot if isinstance(slot, dict) else by_id[slot])
    return out


def _filter_satisfiable(items: list[dict]) -> list[dict]:
    """Drop prompts whose ``required_systems`` are not satisfied by the onboarded catalog.

    A prompt is kept iff EVERY entry in its ``required_systems`` matches
    (case-insensitively) an onboarded system **id** OR an onboarded system's
    **system_type**. The id-OR-type match keeps legacy user prompts saved with
    type labels (e.g. ``"MES"``) working alongside canonical-id entries, and
    covers deployment-dependent OT systems (SiteWise / UNS) whose ids are only
    known at runtime. Prompts with an empty ``required_systems`` are always kept.

    FAIL-OPEN: any error reading the catalog — or an EMPTY onboarded set —
    returns the items unfiltered. The prompt library must never vanish because
    of a catalog hiccup, and a not-yet-onboarded deployment (or the demo
    fallback with no live catalog) must still show the full library; the filter
    only bites when there is a NON-EMPTY onboarded set to check against.

    Scope note (ADR-0019): this is the CORE catalog-**availability** concern — a
    generic set check with no demo knowledge, so unsatisfiable prompts (e.g. a
    UNS-mapping prompt in a no-HighByte deployment) are not served. It is NOT
    the core/demo separation strategy ADR-0019 rejected ("leave the curated
    registry in core, just filter by required_systems") — the demo pack itself
    still lives behind the ``ADT_DEMO`` addon seam, unchanged.
    """
    try:
        from .catalog import catalog
        onboarded = catalog.get_onboarded()
        allowed = {str(s.get("id", "")).lower() for s in onboarded}
        allowed |= {str(s.get("system_type", "")).lower() for s in onboarded}
        # connection_type tokens too ("SiteWise (Direct)" → {"sitewise (direct)",
        # "sitewise", "direct"}): deployment-dependent OT prompts are saved with
        # connection-technology labels (e.g. "SiteWise"), which are otherwise
        # unsatisfiable when the system's *type* is a role label like
        # "Historian" — exactly the id-OR-type-OR-technology intent above.
        for s in onboarded:
            ct = str(s.get("connection_type", "")).lower()
            if ct:
                allowed.add(ct)
                allowed |= {tok for tok in re.split(r"[^a-z0-9]+", ct) if tok}
        allowed.discard("")
    except Exception:  # nosec B110 - deliberate fail-open on catalog unavailability
        return items
    if not allowed:
        # FAIL-OPEN on an empty onboarded set: nothing to check against yet
        # (fresh deployment / demo fallback) — serve the full library.
        return items
    return [
        it for it in items
        if all(str(r).lower() in allowed for r in (it.get("required_systems") or []))
    ]


def _run_prompt(prompt: dict, run: "PromptRunRequest | None" = None) -> tuple:
    """Run a saved prompt with a fresh per-turn time line in front (the same
    enrichment /api/ask uses: ``time_context.with_time_context``). ``run``
    carries the browser's zone and locale; a scheduled run has none and gets
    the UTC-only line. Returns ``(result, is_video)``."""
    from . import time_context
    run = run or PromptRunRequest()
    text = time_context.with_time_context(prompt["text"], prompt["text"],
                                          run.timezone, run.locale)
    is_video = prompt.get("type") == "video_briefing" and _video_fn is not None
    result = _video_fn(prompt["template_id"], text) if is_video else _execute_fn(text)
    return result, is_video


def _save_card(prompt: dict, result: str, is_video: bool = False) -> dict:
    existing = _card_store.get_by_prompt(prompt["template_id"])
    card_id = existing[0]["card_id"] if existing else str(uuid.uuid4())
    card = {
        "card_id": card_id,
        "prompt_id": prompt["template_id"],
        "title": prompt.get("title", ""),
        "last_executed": _now(),
        "status": "completed",
    }
    if is_video:
        card["result_video_url"] = result
    else:
        card["result_markdown"] = result
    _card_store.save(card)
    return card


# ---------------------------------------------------------------------------
# Scheduled execution
# ---------------------------------------------------------------------------

async def _execute_scheduled_prompt(template_id: str):
    prompt = _prompt_store.get(template_id)
    if not prompt:
        logger.error("Scheduled prompt not found: %s", template_id)
        return
    try:
        result, is_video = _run_prompt(prompt)
        if result == SCHEDULER_BUSY_RESULT:
            logger.info("Scheduled prompt %s skipped while admission was busy", template_id)
            return
        _save_card(prompt, result, is_video=is_video)
    except Exception as e:
        logger.error("Scheduled execution failed for %s: %s", template_id, e)  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
        raise


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/prompts")
def list_prompts(source: str | None = None):
    results = _prompt_store.list_all(source=source)
    # If empty, fall back to the demo prompt pack — but ONLY when the demo addon
    # is enabled (ADT_DEMO). A clean core has no registry/demo prompts.
    if not results and (source is None or source == "registry") and demo.is_enabled():
        now = _now()
        return _filter_satisfiable([
            {"template_id": _registry_template_id(e), "source": "registry",
             "created": now, "pinned": e.get("pinned", False), "schedule": None, **e}
            for e in demo.demo_prompts()
        ])
    # Defensive: collapse any residual duplicate rows by template_id (registry
    # preferred). Deterministic registry ids make this a no-op once a fresh
    # re-seed has pruned pre-fix random-id dups; it protects the persona panel in
    # the meantime and against any future accidental double-write.
    return _filter_satisfiable(_dedupe_by_template_id(results))


@router.post("/prompts")
def save_prompt(req: SavePromptRequest):
    item = {
        "template_id": str(uuid.uuid4()),
        "source": "user",
        "created": _now(),
        **req.model_dump(),
    }
    _prompt_store.save(item)
    return item


@router.put("/prompts/{template_id}")
def update_prompt(template_id: str, updates: dict):
    _prompt_store.update(template_id, updates)
    return {"success": True}


@router.delete("/prompts/{template_id}")
def delete_prompt(template_id: str):
    _prompt_store.delete(template_id)
    job_id = f"prompt_{template_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"success": True}


@router.post("/prompts/{template_id}/execute")
def execute_prompt(template_id: str, run: PromptRunRequest | None = None):
    prompt = _prompt_store.get(template_id)
    result, is_video = _run_prompt(prompt, run)
    card = None
    if prompt.get("pinned"):
        card = _save_card(prompt, result, is_video=is_video)
    return {"result": result, "card": card}


@router.post("/prompts/synthesize")
def synthesize_prompt(req: SynthesizeRequest):
    conversation = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in req.messages)
    text = _invoke_llm(
        "You are a prompt engineer. Given this conversation between a user and an AI assistant "
        "about manufacturing data, distill it into a single, self-contained prompt that would "
        "produce the same result. Return ONLY the prompt text, nothing else.",
        conversation,
    )
    return {"prompt": text}


@router.post("/prompts/{template_id}/schedule")
def schedule_prompt(template_id: str, req: ScheduleRequest):
    cron = _invoke_llm(
        'Convert this natural language schedule to a cron expression. Return ONLY the cron '
        'expression (5 fields: minute hour day_of_month month day_of_week), nothing else. '
        'Examples: "every weekday at 7am" -> "0 7 * * 1-5", '
        '"twice daily at 8am and 2pm" -> "0 8,14 * * *"',
        req.schedule_text,
    )
    _prompt_store.update(template_id, {"schedule": cron})
    _add_scheduler_job(template_id, cron)
    return {"cron": cron, "schedule_text": req.schedule_text}


@router.get("/dashboard-cards")
def list_cards():
    return _card_store.list_all()


@router.post("/dashboard-cards/{card_id}/refresh")
def refresh_card(card_id: str, run: PromptRunRequest | None = None):
    card = _card_store.get(card_id)
    prompt = _prompt_store.get(card["prompt_id"])
    result, is_video = _run_prompt(prompt, run)
    updated = _save_card(prompt, result, is_video=is_video)
    return updated


# ---------------------------------------------------------------------------
# Registry seed
# ---------------------------------------------------------------------------

def seed_registry():
    if not _prompt_store:
        return
    if not demo.is_enabled():
        return
    # Prune existing registry rows first, then re-seed. Deletes cover ALL current
    # registry rows (incl. pre-fix random-id dups), and re-inserts use deterministic
    # ids so a re-seed overwrites the same primary keys instead of accumulating new
    # rows — making seeding idempotent under repeated boots and concurrent tasks.
    existing = _prompt_store.list_all(source="registry")
    for item in existing:
        try:
            _prompt_store.delete(item["template_id"])
        except Exception:  # nosec B110 - intentional fallback, no recovery action needed
            pass
    now = _now()
    prompts = demo.demo_prompts()
    for entry in prompts:
        item = {
            "template_id": _registry_template_id(entry),
            "source": "registry",
            "created": now,
            "pinned": False,
            "schedule": None,
            **entry,
        }
        _prompt_store.save(item)
    logger.info("Seeded %d registry prompts", len(prompts))


def unseed_registry():
    # Unconditional: disable must remove registry prompts regardless of
    # demo.is_enabled(). Mirrors the delete half of seed_registry().
    if not _prompt_store:
        return
    removed = 0
    for item in _prompt_store.list_all(source="registry"):
        _prompt_store.delete(item["template_id"])
        removed += 1
    logger.info("Unseeded %d registry prompts", removed)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_scheduler():
    _scheduler.start()


def add_interval_job(job_id: str, func, seconds: int):
    """Register (or replace) a fixed-interval background job on the shared scheduler.

    A generic scheduler utility so callers (e.g. the runtime keep-warm job wired in
    ``server.py``) can reuse the single AsyncIOScheduler instead of spinning up a
    second scheduler. Idempotent — replaces any existing job with the same ``job_id``
    so it survives re-init/restart without stacking duplicates. ``func`` may be a
    plain (sync) callable; the AsyncIO executor runs it in a worker thread, so a
    blocking ping is safe.
    """
    if _scheduler is None:
        logger.warning("add_interval_job(%s) called before scheduler init — ignored", job_id)
        return
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    _scheduler.add_job(func, IntervalTrigger(seconds=seconds), id=job_id)
    logger.info("Registered interval job %s (every %ss)", job_id, seconds)


def stop_scheduler():
    _scheduler.shutdown()
