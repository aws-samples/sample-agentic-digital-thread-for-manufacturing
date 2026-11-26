# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The edges of the shared reset lease.

* A removal whose process died left its status record ``running``: past its
  ``expires_at`` it no longer held onboarding off, but a background removal
  (the card's Remove again, the chat) still answered ``already-running`` and
  the chat still reported it running, until DynamoDB deleted the record.
* A lease past ``expires_at`` read as expired at that second but could only
  be taken over the next one.

(The Step Functions provision's lease edges were pinned here until that
provisioner, which had no caller, was removed on 2026-10-29.)

Offline: the shared table is a fake; the reset routes and the lease are real.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import agents.onboard as onboard
from agents import reset_lease
from agents.job_store import JobStore
from tests.unit._shared_table import SharedTable
from tests.unit.test_one_reset_lease import (  # noqa: F401 - fixtures
    JOB_WAIT, SID, SID2, Replica, _one_process, clock, table)


# ── a removal whose process died does not hold its retry off ──────────────

def _dead_removal(table, sid=SID) -> dict:
    """A removal of *sid* on app task A that claimed (its lease and its
    running record in the shared table) and then died."""
    a = Replica(table)
    with a:
        onboard._reset_publish_claim(sid, False)
        return a.state["_RESET_LEASES"].lease_read(
            reset_lease.RESET_LEASE_KEY, now=reset_lease.now(), shared_only=True)


@pytest.fixture
def steps(monkeypatch):
    """The cascade replaced: each call records the reset lease it ran under."""
    leases: list = []

    async def _steps(system_id, full=False, **_kw):
        leases.append(onboard._RESET_LEASES.lease_read(
            reset_lease.RESET_LEASE_KEY, now=reset_lease.now(), shared_only=True))
        return {"status": "reset", "system_id": system_id}
    monkeypatch.setattr(onboard, "_run_reset_steps", _steps)
    return leases


async def _remove_again(sid=SID) -> dict:
    """The card's Remove again: a background retry, run until its job ends."""
    started = await onboard.onboard_reset_system(sid, wait=False, full=False, retry=True)
    for _ in range(int(JOB_WAIT * 100) if started.get("status") == "started" else 0):
        if (onboard._RESET_JOBS.get(sid) or {}).get("state") != "running":
            break
        await asyncio.sleep(0.01)
    return started


def test_remove_again_takes_a_fresh_lease_once_a_dead_removal_expired(table, clock, steps):
    dead = _dead_removal(table)
    b = Replica(table)
    with b:
        onboard.catalog.mark_removal_failed(SID, False)
        assert asyncio.run(_remove_again())["status"] == "already-running"
        clock.advance(onboard._RESET_LEASE_SECONDS + 1)
        started = asyncio.run(_remove_again())
        assert started == {"status": "started", "system_id": SID}, started
        assert onboard._RESET_JOBS.get(SID)["state"] == "done", onboard._RESET_JOBS.get(SID)
    assert len(steps) == 1 and steps[0] is not None
    assert steps[0]["owner"] != dead["owner"] and steps[0]["scope"] == SID, steps


def test_the_chat_stops_reporting_a_dead_removal_once_it_expired(table, clock, monkeypatch):
    monkeypatch.setattr(onboard, "CHAT_REMOVAL_CHECK_WAIT_SECONDS", 0.0)
    _dead_removal(table)
    with Replica(table):
        assert (onboard.chat_removal_check(SID) or {}).get("status") == "removing"
        clock.advance(onboard._RESET_LEASE_SECONDS + 1)
        assert onboard.chat_removal_check(SID) is None


def test_another_systems_dead_removal_stops_refusing_once_it_expired(table, clock, steps):
    _dead_removal(table, SID2)
    with Replica(table):
        with pytest.raises(HTTPException) as err:
            asyncio.run(onboard.onboard_reset_system(SID, wait=True))
        assert err.value.status_code == 409
        clock.advance(onboard._RESET_LEASE_SECONDS + 1)
        assert asyncio.run(onboard.onboard_reset_system(SID, wait=True))["status"] == "reset"


# ── a lease is expired, and can be taken, at the same second ──────────────

@pytest.mark.parametrize("shared", [False, True], ids=["memory", "shared-table"])
def test_a_lease_is_taken_at_the_second_it_reads_as_expired(shared):
    store = JobStore("reset", table=SharedTable() if shared else None)
    key = reset_lease.RESET_LEASE_KEY
    assert store.lease_acquire(key, {"owner": "a", "scope": SID, "expires_at": 1000},
                               now=990) is None
    assert store.lease_read(key, now=999)["owner"] == "a"
    assert store.lease_read(key, now=1000) is None
    assert store.lease_acquire(key, {"owner": "b", "scope": SID2, "expires_at": 2000},
                               now=1000) is None, "an expired lease could not be taken"
    assert store.lease_read(key, now=1000)["owner"] == "b"
