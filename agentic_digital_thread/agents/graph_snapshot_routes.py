# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin routes for the operational graph snapshot (export / import / status).

Mounted by server.py exactly like the other admin routers (see
``agents_trace_routes``). Every route is admin-gated with the same
defense-in-depth ``request.state.is_admin`` check the traces routes use —
the auth middleware already 403s non-admins on the /api/admin prefix, and the
in-route check fails closed when the attribute is absent.

EXPORT — paginated chunks, NOT an async job. Justification: each chunk is one
bounded SKIP/LIMIT page (a sub-second query), so every request completes far
inside the CloudFront 120 s origin timeout with NO server-side job state to
store or reap — the CLI walks ``?cursor=...`` and assembles the file
(``graph_snapshot.assemble_snapshot``). An async job would need somewhere to
park a potentially tens-of-MB payload (JobStore rows are DynamoDB items,
capped at 400 KB) and a second download channel; stateless pagination avoids
both.

IMPORT — DESTRUCTIVE, background job. A large-graph restore takes minutes, so
a synchronous route would 504 at the same 120 s CloudFront timeout (the exact
husk-record failure mode the onboarding reset solved). Follows the same
pattern as ``onboard.py``'s ``?wait=false`` reset: JobStore-backed state +
``GET .../import/status`` polling. The body must carry
``{"confirm": "wipe-and-restore"}`` — anything else is a 400.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from . import graph_snapshot
from .job_store import JobStore
from .offload import offload
from .public_errors import public_error
from .startup_restore import refuse_while_restoring

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/graph/snapshot")

#: Confirmation phrase the import body must carry, verbatim.
CONFIRM_PHRASE = "wipe-and-restore"

# Single-slot import job (one restore at a time — it wipes the whole graph).
# JobStore = same machinery as onboard.py's _RESET_JOBS: write-through to the
# shared onboarding-sessions table when deployed multi-replica, in-memory
# fail-soft otherwise. Only small status dicts are stored, never the payload.
_IMPORT_JOBS = JobStore("graph-snapshot-import")
_IMPORT_JOB_KEY = "global"


def _check_admin(request: Request):
    """Defense-in-depth admin check (mirrors agents_trace_routes; the auth
    middleware already 403s non-admins on the /api/admin prefix)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("")
def get_snapshot_chunk(request: Request, cursor: str = ""):
    """One bounded export chunk; the CLI walks cursors and assembles the file."""
    _check_admin(request)
    try:
        return graph_snapshot.export_chunk(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _run_import_job(payload: dict) -> None:
    """Background wrapper — records progress in the job store, never raises."""
    try:
        result = await asyncio.to_thread(graph_snapshot.import_snapshot, payload)
        # ALWAYS append the read-only reconcile report: a restored husk
        # (graph says onboarded, runtime gone) must never be silent.
        try:
            result["reconcile"] = await asyncio.to_thread(_reconcile_report)
        except Exception as exc:  # noqa: BLE001 - report is best-effort
            result["reconcile_error"] = public_error(
                exc, "graph_snapshot.import.reconcile", default="upstream_error")["error"]
        _IMPORT_JOBS[_IMPORT_JOB_KEY] = {"state": "done", "result": result}
    except Exception as exc:  # surface, never swallow: the poller renders it
        logger.exception("graph snapshot import failed")
        _IMPORT_JOBS[_IMPORT_JOB_KEY] = {
            "state": "error",
            **public_error(exc, "graph_snapshot.import", default="graph_query_failed")}


@router.post("/import")
async def start_snapshot_import(request: Request):
    """DESTRUCTIVE wipe-and-restore, run as a background job (see module doc).

    400 unless the body carries the literal ``confirm`` token and a snapshot
    that validates (unknown format versions are refused BEFORE any wipe).
    503 (``startup_restore``) while the startup restore still runs in the
    background: the wipe would delete what the restore is reading, and the
    restore would then register systems and write records the imported
    graph does not hold. Checked after the admin check, before the body.
    """
    _check_admin(request)
    refuse_while_restoring()
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc
    if not isinstance(body, dict) or body.get("confirm") != CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f"import requires body field confirm={CONFIRM_PHRASE!r}")
    try:
        graph_snapshot.validate_snapshot(body.get("snapshot"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing = _IMPORT_JOBS.get(_IMPORT_JOB_KEY)
    if existing and existing.get("state") == "running":
        return {"status": "already-running"}
    _IMPORT_JOBS[_IMPORT_JOB_KEY] = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    asyncio.get_event_loop().create_task(_run_import_job(body["snapshot"]))
    return {"status": "started"}


@router.get("/import/status")
def snapshot_import_status(request: Request):
    """Poll the import job: none | running | done | error."""
    _check_admin(request)
    job = _IMPORT_JOBS.get(_IMPORT_JOB_KEY)
    if not job:
        return {"state": "none"}
    return dict(job)


# ── Reconcile + repair (husk detection / re-provisioning) ────────────────────

# Single-slot repair job — a repair re-provisions runtimes (minutes), so it
# runs as a background job exactly like the import; the check alone is two
# control-plane listings and stays synchronous.
_REPAIR_JOBS = JobStore("graph-snapshot-repair")
_REPAIR_JOB_KEY = "global"
# The one-repair-at-a-time slot, claimed ONLY through JobStore.claim_slot and
# released exact-owner by the job that claimed it. ``global`` stays the status
# record that /reconcile/status and the notification feed read, and only the
# slot's current owner writes it.
_REPAIR_ACTIVE_KEY = "active"
# A running repair refreshes its claim every heartbeat (JobStore.touch_slot),
# however long server._restore takes; a claim not refreshed for the stale
# bound belongs to an abandoned job (a replaced task) and is taken over.
_REPAIR_HEARTBEAT_SECONDS = 120
_REPAIR_STALE_SECONDS = 900


def _reconcile_report() -> list[dict]:
    """Read-only husk check against live AgentCore state (module-level so
    both the import job and the reconcile route share one implementation)."""
    import boto3

    from . import onboard as _onboard
    manager = _onboard._agent_manager
    if manager is None or getattr(manager, "mode", "") != "cloud":
        return []  # local mode: no cloud runtimes to reconcile against
    import os
    control = boto3.client(
        "bedrock-agentcore-control",
        region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return graph_snapshot.reconcile_systems(manager=manager, control=control)


def _claim_repair_slot() -> "str | None":
    """Atomically claim the repair slot; the new run id, or None when a fresh
    repair already holds it. Two concurrent requests run this in two worker
    threads, so the claim (not a read of ``global``) decides which one starts."""
    run_id = uuid.uuid4().hex[:16]
    now = time.time()
    if not _REPAIR_JOBS.claim_slot(
            _REPAIR_ACTIVE_KEY,
            {"status": "running", "run_id": run_id, "started_at": now},
            stale_seconds=_REPAIR_STALE_SECONDS, now=now):
        return None
    _REPAIR_JOBS[_REPAIR_JOB_KEY] = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    return run_id


def _release_repair_slot(run_id: str) -> None:
    """Exact-owner release: a stale run finishing late never frees a newer claim."""
    if not _REPAIR_JOBS.release_slot(
            _REPAIR_ACTIVE_KEY, run_id,
            {"status": "idle", "run_id": run_id, "finished_at": time.time()}):
        logger.warning("graph snapshot repair slot not released: run=%s "
                       "(taken over by a newer repair)", run_id)


def _touch_repair_slot(run_id: str) -> bool:
    """Refresh *run_id*'s claim; False once another repair has taken it over."""
    return _REPAIR_JOBS.touch_slot(_REPAIR_ACTIVE_KEY, run_id, now=time.time())


def _record_repair_outcome(run_id: str, outcome: dict) -> None:
    """Write ``global`` only while *run_id* still owns the slot. The touch
    just before the write makes the claim fresh, so no takeover can land
    between the ownership check and the write."""
    if _touch_repair_slot(run_id):
        _REPAIR_JOBS[_REPAIR_JOB_KEY] = outcome
    else:
        logger.warning("graph snapshot repair outcome not recorded: run=%s "
                       "(taken over by a newer repair)", run_id)


async def _heartbeat_repair(run_id: str, stop: asyncio.Event) -> None:
    """Keep the repair's claim fresh until *stop*, or until it is lost."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_REPAIR_HEARTBEAT_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        if not await asyncio.to_thread(_touch_repair_slot, run_id):
            logger.warning("graph snapshot repair lost its slot: run=%s", run_id)
            return


async def _run_repair_job(run_id: str) -> None:
    """Background repair — reuses server._restore (the ECS-startup path).
    Heartbeats its claim while it runs, records its outcome only while it
    still owns the slot, and releases the slot whatever the outcome."""
    stop = asyncio.Event()
    heartbeat = asyncio.ensure_future(_heartbeat_repair(run_id, stop))
    try:
        try:
            from . import server as _server
            report = await asyncio.to_thread(_reconcile_report)
            result = await asyncio.to_thread(
                graph_snapshot.repair_systems, report,
                restore_fn=_server._restore)
            result["report_after"] = await asyncio.to_thread(_reconcile_report)
            outcome = {"state": "done", "result": result}
        except Exception as exc:
            logger.exception("graph snapshot repair failed")
            outcome = {"state": "error",
                       **public_error(exc, "graph_snapshot.repair", default="upstream_error")}
        await asyncio.to_thread(_record_repair_outcome, run_id, outcome)
    finally:
        stop.set()
        await heartbeat
        await asyncio.to_thread(_release_repair_slot, run_id)


def reconcile(request: Request, body, loop: asyncio.AbstractEventLoop):
    """Husk check; ``{"repair": true}`` additionally starts the repair job.

    Check-only responds synchronously (two listings, sub-second). Repair
    re-provisions runtimes — minutes — so it returns ``started`` and is
    polled at ``GET .../reconcile/status`` (CloudFront cuts at 120 s).

    Sync body (control-plane listings and the job-store record); the route
    reads the JSON body and offloads this. It runs in a worker thread, so a
    started repair job is handed to *loop*, the request's event loop, and
    the one-repair rule is the atomic slot claim. A repair is refused (503,
    ``startup_restore``) while the startup restore still runs in the
    background: the repair runs ``server._restore`` again, which replaces the
    same ``AgentManager.start_agent`` and writes the same catalog, agents and
    records. The check alone still answers."""
    _check_admin(request)
    repair = isinstance(body, dict) and body.get("repair") is True
    if repair:
        refuse_while_restoring()
    report = _reconcile_report()
    if not repair:
        return {"report": report}
    run_id = _claim_repair_slot()
    if run_id is None:
        return {"status": "already-running", "report": report}
    try:
        asyncio.run_coroutine_threadsafe(_run_repair_job(run_id), loop)
    except Exception as exc:  # the job never started: free the slot it held
        _REPAIR_JOBS[_REPAIR_JOB_KEY] = {
            "state": "error", **public_error(exc, "graph_snapshot.repair.start")}
        _release_repair_slot(run_id)
        raise
    return {"status": "started", "report": report}


@router.post("/reconcile")
async def reconcile_route(request: Request):
    """Husk check, optionally starting the repair job. Sync body: ``reconcile``.
    Reading the JSON body is the one async step, so it stays here."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await offload(reconcile, request, body, asyncio.get_running_loop())


@router.get("/reconcile/status")
def reconcile_status(request: Request):
    """Poll the repair job: none | running | done | error."""
    _check_admin(request)
    job = _REPAIR_JOBS.get(_REPAIR_JOB_KEY)
    if not job:
        return {"state": "none"}
    return dict(job)
