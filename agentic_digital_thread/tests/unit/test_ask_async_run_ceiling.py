# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An async ask that stays 'running' past 20 minutes is reported as an error.

The single app-wide async-ask slot is taken over by a NEW submit after 600 s
(``_ASK_STALE_SECONDS``), but a wedged job's own record stayed ``running``
forever, so a Quick query bridge polling it (it keeps polling while the status
is running) never stopped. The result route now answers ``error`` with a short
reason for a job running longer than ``_ASK_RUN_CEILING_SECONDS`` (20 min: a
real query can take 11+ minutes), replaces the record with a terminal
tombstone (the worker is abandoned, see test_ask_async_abandoned_worker.py),
and releases the slot if
that job still holds it (exact owner, so a newer job's claim is untouched).
The 600 s takeover, the ~20 s hold and every finished job are unchanged.
Offline: JobStore runs memory-only.
"""

import asyncio
import types

import pytest

import agents.server as server
from agents.job_store import JobStore

NOW = 1_800_000_000.0
JOB = "job-wedged01"
REASON = "the query ran longer than 20 minutes and was abandoned; ask again"


@pytest.fixture(autouse=True)
def jobs(monkeypatch):
    store = JobStore("ask", table=None)
    monkeypatch.setattr(server, "_ASK_JOBS", store)
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: NOW, raising=False)
    return store


def _running(store, job_id: str, age: float, *, holds_slot: bool = True) -> None:
    started = NOW - age
    store[job_id] = {"status": "running", "started_at": started, "submitter": None}
    if holds_slot:
        assert store.claim_slot(server._ASK_ACTIVE_KEY,
                                {"status": "running", "job_id": job_id,
                                 "started_at": started},
                                stale_seconds=server._ASK_STALE_SECONDS, now=started)


def _poll(job_id: str = JOB, wait=0.0) -> dict:
    return asyncio.run(server.ask_result(job_id, wait=wait))


def test_the_ceiling_is_twenty_minutes_and_above_the_takeover():
    assert server._ASK_RUN_CEILING_SECONDS == 20 * 60
    assert server._ASK_STALE_SECONDS == 600


def test_a_job_running_past_the_ceiling_is_an_error_and_frees_the_slot(jobs):
    _running(jobs, JOB, age=20 * 60 + 1)
    assert _poll() == {"status": "error", "error": "ask_failed", "error_detail": REASON}
    tomb = jobs.get(JOB)
    assert tomb["status"] == "error" and tomb["abandoned"] is True, (
        "a terminal tombstone stays, so the late worker cannot recreate the record")
    assert _poll() == {"status": "error", "error": "ask_failed", "error_detail": REASON}
    slot = jobs.get(server._ASK_ACTIVE_KEY)
    assert slot["status"] == "idle" and slot["job_id"] == JOB, slot
    # A new ask can claim the slot straight away.
    assert jobs.claim_slot(server._ASK_ACTIVE_KEY, {"status": "running", "job_id": "next",
                                                    "started_at": NOW},
                           stale_seconds=server._ASK_STALE_SECONDS, now=NOW)


def test_a_newer_jobs_slot_is_left_alone(jobs):
    _running(jobs, JOB, age=25 * 60, holds_slot=False)
    _running(jobs, "job-newer", age=30)                 # took the slot over
    assert _poll()["status"] == "error"
    slot = jobs.get(server._ASK_ACTIVE_KEY)
    assert slot["job_id"] == "job-newer" and slot["status"] == "running", slot


def test_an_overdue_job_answers_without_holding(jobs, monkeypatch):
    sleeps: list = []

    async def _sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    _running(jobs, JOB, age=21 * 60)
    assert _poll(wait=None)["status"] == "error"
    assert sleeps == []


def test_guard_a_job_under_the_ceiling_is_still_running(jobs):
    _running(jobs, JOB, age=19 * 60 + 59)
    assert _poll() == {"status": "running", "job_id": JOB}
    assert jobs.get(JOB)["status"] == "running"
    assert jobs.get(server._ASK_ACTIVE_KEY)["status"] == "running"


def test_guard_a_finished_job_is_delivered_whatever_its_age(jobs):
    jobs[JOB] = {"status": "done", "answer": "42 units", "started_at": NOW - 3600}
    assert _poll() == {"status": "done", "answer": "42 units"}


@pytest.mark.parametrize("started", [None, "x", float("nan")])
def test_guard_a_job_without_a_usable_start_time_keeps_running(jobs, started):
    jobs[JOB] = {"status": "running", "started_at": started}
    assert _poll() == {"status": "running", "job_id": JOB}


def test_guard_the_hold_and_takeover_constants_are_unchanged():
    assert server._ASK_RESULT_WAIT_SECONDS == 20.0
    assert server._ASK_RESULT_POLL_STEP_SECONDS == 1.5
