# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The memory extraction model switch, run as a background job.

AgentCore took about 131 s to settle a switch on adt-demo (2026-10-27), and
a request through the app's origin lasts at most 120 s (CloudFront's origin
read timeout and the ALB idle timeout), so the switch never runs inside a
request (the offboard-504 class, ADR-0061):

* ``POST /api/admin/llm-settings/memory-model`` (:func:`start`) checks the
  model from the deployment alone (``memory_model.refusal``: offered here,
  the allowlist, the workshop; no memory lookup and no AWS call), takes the
  save slot (``memory_model.claim_save_slot``; the claim names the job, its
  model and when it started, so it is the running switch's record on every
  task), starts a worker thread and answers 202 with the running record;
* the worker (:func:`_run`) writes the job's record (and stops, changing
  nothing, when the shared table will not take it), does the save
  (``memory_model.run_save``: it finds the memory, ending the job with an
  error when there is none, then the UpdateMemory, the wait, the SSM write
  and the read after it), then records the outcome twice where every task
  reads it: the job record, and the slot's exact-owner release, which
  carries the same result;
* ``GET /api/admin/llm-settings/memory-model/job/{id}`` (:func:`status`)
  and ``.../job`` (:func:`current`, the slot's last owner) read the slot,
  then the job record (:func:`_answer`).

Every call to the shared table goes through the bounded store client
(``memory_model.store_client_config``), and each route makes at most two of
them one after the other (:data:`POST_STORE_CALLS`,
:data:`STATUS_STORE_CALLS`), so it answers well within the origin's 120 s
under the failure modes those bounds cover (``memory_model``'s docstring
says which they are, and which they are not).

A switch whose outcome reached neither record reads as ``running`` while its
slot still holds it, and as ``unknown`` once the slot has been quiet for
:data:`SILENT_SECONDS`: a running switch writes it at least every
``memory_model.JOB_SILENCE_MAX_SECONDS``. Nothing is kept as a task-local
copy. Every message is fixed text, model ids and AgentCore status words, or
``public_error_text``; never an exception's own text. All functions are
synchronous; routes offload them.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from . import memory_model as mm
from .public_errors import public_error_text

logger = logging.getLogger(__name__)

#: A ``running`` job record older than this, read while the slot cannot be,
#: belongs to a switch that ended without a recorded outcome: the slot's
#: stale bound, which is above the longest switch job
#: (``memory_model.JOB_MAX_SECONDS``).
JOB_STALE_SECONDS = mm.SAVE_SLOT_STALE_SECONDS
#: A slot still held by a switch that has written nothing to it for this
#: long reads as ``unknown``: the switch stopped (a replaced task) or could
#: not record how it ended. At least twice the longest a running switch goes
#: between two writes of its slot (``memory_model.JOB_SILENCE_MAX_SECONDS``).
#: Another switch can take the slot over only after
#: ``memory_model.SAVE_SLOT_STALE_SECONDS``.
SILENT_SECONDS = 300.0
#: The most store calls each route makes, one after the other: the POST's
#: claim, then the read that tells a held slot from an unreachable store, or
#: the release after a worker that would not start; a status read's slot,
#: then the job record.
POST_STORE_CALLS = 2
STATUS_STORE_CALLS = 2
#: The longest each route waits on the shared table (``memory_model``'s
#: bounded store client), under the failure modes its bounds cover (connect,
#: read, attempts); not a wall-clock limit: DNS, a refresh of the task's
#: credentials and an answer that trickles are not bounded
#: (``memory_model``'s docstring, ``docs/deployment.md``).
POST_MAX_SECONDS = POST_STORE_CALLS * mm.STORE_CALL_SECONDS
STATUS_MAX_SECONDS = STATUS_STORE_CALLS * mm.STORE_CALL_SECONDS

#: The worker's first write did not land, so it changed nothing.
_NOT_RECORDED = ("The memory extraction model switch could not be coordinated with the other "
                 "app tasks (the shared store did not take its record); try again. Nothing "
                 "was changed.")
_NOT_STARTED = ("The memory extraction model switch could not be started; try again. Nothing "
                "was changed.")
_STATUS_UNREADABLE = ("The memory extraction model switch's status could not be read just now "
                      "(the shared store did not answer); it is read again shortly.")
_UNKNOWN_JOB = ("No such memory extraction model switch is known; a finished one is kept for "
                "an hour.")
_CHECK = ("Reload this page to read what the memory runs and what is saved, and check the "
          "memory before switching again.")
_TERMINAL = ("done", "error", "unknown")

_JOBS = None  # lazy JobStore("memory-model-job"); tests inject an in-memory one
_now = time.time  # a seam for the status reads' clock


def _jobs():
    """The shared job records (lazy, so the module imports without boto3), on
    the bounded store client."""
    global _JOBS
    if _JOBS is None:
        from .job_store import JobStore
        _JOBS = JobStore("memory-model-job", client_config=mm.store_client_config())
    return _JOBS


def _start_worker(target, *args) -> None:
    """Run *target* in a daemon thread (a seam: tests run it inline)."""
    threading.Thread(target=target, args=args, name="memory-model-switch",
                     daemon=True).start()


def _refused(status: int, message: str, reason: str = "") -> tuple[int, dict]:
    body = {"status": "error", "message": message}
    if reason:
        body["reason"] = reason
    return status, body


def _record(job_id: str, record: dict) -> bool:
    """Write *record* where every app task reads it; ``False`` when the
    shared table could not take it (nothing is kept then)."""
    from .job_store import JobStoreUnavailableError
    try:
        _jobs().put_strict(job_id, record)
    except JobStoreUnavailableError:
        return False
    return True


def _outcome(owner: str, target: str, started: float, result: dict) -> dict:
    """The terminal record of *result* (``run_save``'s, or a refusal)."""
    ok = result.get("status") == "ok"
    record = {"status": "done" if ok else "error", "job_id": owner, "memory_model": target,
              "started_at": started, "finished_at": time.time(),
              "message": result.get("message")
              or mm.done_message(target, bool(result.get("memory_model_changed")))}
    if "memory_model_changed" in result:
        record["memory_model_changed"] = bool(result["memory_model_changed"])
    return record


def start(model_id: str, region: str) -> tuple[int, dict]:
    """POST /api/admin/llm-settings/memory-model: ``(HTTP status, body)``.

    400 when *model_id* is refused (not offered here, the workshop, an
    allowlist with no memory model; checked from the deployment alone, so
    the request never waits on the app's config), 409 while another switch
    holds the slot, 503 when the shared store cannot decide the claim or the
    worker cannot start; each before anything is read from or written to the
    memory or SSM. Otherwise 202 with the running record (``job_id``,
    ``memory_model``, ``started_at``); a deployment with no memory is the
    job's error (``memory_model.NO_MEMORY``). At most
    :data:`POST_STORE_CALLS` store calls."""
    message = mm.refusal(model_id)
    target = "" if message else mm.resolve(model_id, region)
    if not target:
        return _refused(400, message or "memory_model is not a memory extraction model.")
    owner = uuid.uuid4().hex
    started = time.time()
    reason = mm.claim_save_slot(owner, target, now=started)
    if reason:
        return _refused(mm.CLAIM_REFUSED_STATUS[reason], mm.CLAIM_REFUSED[reason], reason)
    record = {"status": "running", "job_id": owner, "memory_model": target,
              "started_at": started}
    try:
        _start_worker(_run, owner, target, region, started)
    except Exception as e:  # noqa: BLE001 - public text only
        detail = public_error_text(e, "admin.llm_settings.memory_job_start")
        mm.release_save_slot(owner, _outcome(owner, target, started, {
            "status": "error", "message": f"{_NOT_STARTED} ({detail})"}))
        return _refused(503, _NOT_STARTED)
    return 202, record


def _run(owner: str, target: str, region: str, started: float) -> None:
    """The worker: its record first (a switch no task could see does not
    run), then the save, then the outcome where every task reads it."""
    running = {"status": "running", "job_id": owner, "memory_model": target,
               "started_at": started}
    result = {"status": "error", "message": (
        f"The memory extraction model switch to {target} stopped before it finished. "
        f"{_CHECK}")}
    try:
        if not _record(owner, running):
            result = {"status": "error", "message": _NOT_RECORDED}
            return
        result = mm.run_save(target, region, owner)
    except Exception as e:  # noqa: BLE001 - recorded as public text, never raised
        detail = public_error_text(e, "admin.llm_settings.memory_job", default="upstream_error")
        result = {"status": "error", "message": (
            f"The memory extraction model switch to {target} stopped with an unexpected "
            f"error ({detail}). {_CHECK}")}
    finally:
        _finish(owner, target, started, result)


def _finish(owner: str, target: str, started: float, result: dict) -> None:
    """Record the outcome in the job record, then release the slot carrying
    it: either one is enough for every task to read it (:func:`_answer`)."""
    record = _outcome(owner, target, started, result)
    recorded = _record(owner, record)
    released = mm.release_save_slot(owner, record)
    if not recorded:
        logger.warning("memory model switch %s: outcome not written to the job record; %s",
                       owner, "the slot's release carries it" if released else (
                           "the slot was not released either, so it reads as unknown "
                           f"after {int(SILENT_SECONDS)} s"))


def _read_slot():
    """``(slot, readable)``: the slot's record (``None`` when there is none)
    and whether it could be read."""
    from .job_store import JobStoreUnavailableError
    try:
        return mm.read_save_slot(), True
    except JobStoreUnavailableError:
        return None, False


def _read_record(job_id: str):
    """``(record, readable)`` for *job_id*: a consistent read of the shared
    table when one is configured, else this process's copy."""
    from .job_store import JobStoreUnavailableError
    store = _jobs()
    try:
        record = store.get_strict(job_id) if store.has_shared_table() else store.get(job_id)
    except JobStoreUnavailableError:
        return None, False
    return record, True


def _seconds_since(value, now: float) -> float:
    try:
        return now - float(value)
    except (TypeError, ValueError):
        return float("inf")


def _unknown(job_id: str, model: str, started, message: str) -> dict:
    return {"status": "unknown", "stale": True, "job_id": job_id, "memory_model": model,
            "started_at": started, "message": message}


def _silent(job_id: str, model: str, started, quiet_for: float) -> dict:
    model = model or "the new model"
    return _unknown(job_id, model, started, (
        f"The memory extraction model switch to {model} has not reported how it ended: "
        f"nothing has come from it for {int(quiet_for)} s, and a running switch reports at "
        f"least every {int(mm.JOB_SILENCE_MAX_SECONDS)} s. The app task that ran it may have "
        "stopped, or it could not record its outcome (the shared store did not take it). "
        f"{_CHECK}"))


def _unrecorded(job_id: str, model: str, started) -> dict:
    model = model or "the new model"
    return _unknown(job_id, model, started, (
        f"The memory extraction model switch to {model} is no longer running, but its "
        f"outcome could not be recorded (the shared store did not take it). {_CHECK}"))


def _overdue(job_id: str, model: str, started, age: float) -> dict:
    model = model or "the new model"
    return _unknown(job_id, model, started, (
        f"The memory extraction model switch to {model} started {int(age)} s ago and has "
        f"not reported how it ended; a switch runs at most {int(mm.JOB_MAX_SECONDS)} s. "
        "The app task that ran it may have stopped, or it could not record its outcome "
        f"(the shared store did not take it). {_CHECK}"))


def _answer(job_id: str, slot, slot_readable: bool) -> tuple[int, dict]:
    """The status of *job_id* from the slot read first, then (unless the
    slot carries this job's result) its job record. Reading in that order,
    a switch that ends between the two reads is never missed: its job
    record is written before its slot is released."""
    now = _now()
    mine = isinstance(slot, dict) and slot.get("job_id") == job_id
    if mine:
        result = slot.get("result")
        if isinstance(result, dict) and result.get("job_id") == job_id \
                and result.get("status") in _TERMINAL:
            return 200, result
    record, record_readable = _read_record(job_id)
    if not (isinstance(record, dict) and record.get("job_id") == job_id):
        record = None
    if record is not None and record.get("status") in _TERMINAL:
        return 200, record
    if mine and slot.get("status") == "running":
        model = slot.get("memory_model") or (record or {}).get("memory_model") or ""
        started = slot.get("claimed_at", slot.get("started_at"))
        quiet_for = _seconds_since(slot.get("started_at"), now)
        if quiet_for > SILENT_SECONDS:
            return 200, _silent(job_id, model, started, quiet_for)
        return 200, record or {"status": "running", "job_id": job_id, "memory_model": model,
                               "started_at": started}
    if record is not None:                      # running, but the slot does not hold it
        model = record.get("memory_model") or ""
        if slot_readable:
            return 200, _unrecorded(job_id, model, record.get("started_at"))
        age = _seconds_since(record.get("started_at"), now)
        if age > JOB_STALE_SECONDS:
            return 200, _overdue(job_id, model, record.get("started_at"), age)
        return 200, record
    if not record_readable or not slot_readable:
        return _refused(503, _STATUS_UNREADABLE, "status_unreadable")
    if mine:                                    # released without a result, no record
        return 200, _unrecorded(job_id, slot.get("memory_model") or "",
                                slot.get("claimed_at"))
    return _refused(404, _UNKNOWN_JOB, "unknown_job")


def status(job_id: str) -> tuple[int, dict]:
    """GET /api/admin/llm-settings/memory-model/job/{id}: ``(HTTP status,
    body)``; 404 for an unknown job, 503 when the shared store cannot
    answer, never a 500. At most :data:`STATUS_STORE_CALLS` store calls."""
    slot, readable = _read_slot()
    return _answer(job_id, slot, readable)


def current() -> tuple[int, dict]:
    """GET /api/admin/llm-settings/memory-model/job: the latest switch (the
    slot's last owner) as :func:`status` reports it, ``{"status": "none"}``
    when there is none to report. At most :data:`STATUS_STORE_CALLS` store
    calls."""
    slot, readable = _read_slot()
    if not readable:
        return _refused(503, _STATUS_UNREADABLE, "status_unreadable")
    job_id = slot.get("job_id") if isinstance(slot, dict) else None
    if not isinstance(job_id, str) or not job_id:
        return 200, {"status": "none"}
    code, body = _answer(job_id, slot, readable)
    return (200, {"status": "none"}) if code == 404 else (code, body)
