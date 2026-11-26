# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reset All and a single-system removal exclude each other.

Single-system removals were serialized among themselves, but Reset All read
and wrote neither their claim nor its own: a Reset All could wipe the graph,
schedule user secrets and clear the catalog while a single removal was still
deleting the same system's runtime, secret and node (or put it back to
``registered`` afterwards).

One reset-domain claim now covers both: Reset All answers 409 while any
single removal runs (or another Reset All), and a single removal answers 409
while Reset All runs. Both orders, default and full, async and sync single
removals. Offline: the two cascades are replaced by steps that wait on an
event, so each interleaving is deterministic.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import agents.onboard as onboard
from agents.catalog import SystemCatalog

SID = "line-mes"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    if hasattr(onboard, "_ResetDomain"):
        monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    cat = SystemCatalog([{"id": SID, "name": "Line MES", "status": "onboarded",
                          "connection_type": "PostgreSQL", "schema": "mes"}])
    monkeypatch.setattr(onboard, "catalog", cat)


class _Cascades:
    """Both cascades, replaced by steps that record their call and wait on
    an event."""

    def __init__(self, monkeypatch):
        self.single_gate = asyncio.Event()
        self.all_gate = asyncio.Event()
        self.single_in = asyncio.Event()
        self.all_in = asyncio.Event()
        self.single_ran: list = []
        self.all_ran: list = []
        cascades = self

        async def _single(system_id, full=False, **_kw):
            cascades.single_ran.append(full)
            cascades.single_in.set()
            await cascades.single_gate.wait()
            return {"status": "reset", "system_id": system_id}

        async def _all(full):
            cascades.all_ran.append(full)
            cascades.all_in.set()
            await cascades.all_gate.wait()
            return {"status": "reset", "full": full}
        monkeypatch.setattr(onboard, "_run_reset_steps", _single)
        monkeypatch.setattr(onboard, "_global_reset_steps", _all)


async def _refused(call):
    """Await a call that must be refused at once (a call that instead runs
    its cascade would wait on the cascade's event: a timeout)."""
    return await asyncio.wait_for(call, 5)


async def _settled(system_id: str) -> dict:
    for _ in range(500):
        job = onboard._RESET_JOBS.get(system_id) or {}
        if job.get("state") != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("the removal never settled")


@pytest.mark.parametrize("single_full", [False, True])
@pytest.mark.parametrize("all_full", [False, True])
def test_reset_all_is_refused_while_a_single_removal_runs(monkeypatch, single_full, all_full):
    async def _scenario():
        c = _Cascades(monkeypatch)
        started = await onboard.onboard_reset_system(SID, wait=False, full=single_full)
        assert started["status"] == "started"
        await asyncio.wait_for(c.single_in.wait(), 5)
        with pytest.raises(HTTPException) as err:
            await _refused(onboard.onboard_reset(None, full=all_full))
        assert err.value.status_code == 409
        assert SID in err.value.detail
        assert c.all_ran == [], "Reset All ran while a single removal was running"
        c.single_gate.set()
        assert (await _settled(SID))["state"] == "done"
        c.all_gate.set()
        assert (await onboard.onboard_reset(None, full=all_full))["status"] == "reset"
        assert c.all_ran == [all_full]
    asyncio.run(_scenario())


def test_reset_all_is_refused_while_a_sync_single_removal_runs(monkeypatch):
    async def _scenario():
        c = _Cascades(monkeypatch)
        single = asyncio.ensure_future(onboard.onboard_reset_system(SID, wait=True))
        await asyncio.wait_for(c.single_in.wait(), 5)
        with pytest.raises(HTTPException) as err:
            await _refused(onboard.onboard_reset(None))
        assert err.value.status_code == 409
        assert c.all_ran == []
        c.single_gate.set()
        assert (await single)["status"] == "reset"
        c.all_gate.set()
        assert (await onboard.onboard_reset(None))["status"] == "reset"
    asyncio.run(_scenario())


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("single_full", [False, True])
@pytest.mark.parametrize("all_full", [False, True])
def test_a_single_removal_is_refused_while_reset_all_runs(monkeypatch, wait, single_full,
                                                           all_full):
    async def _scenario():
        c = _Cascades(monkeypatch)
        reset_all = asyncio.ensure_future(onboard.onboard_reset(None, full=all_full))
        await asyncio.wait_for(c.all_in.wait(), 5)
        with pytest.raises(HTTPException) as err:
            await _refused(onboard.onboard_reset_system(SID, wait=wait, full=single_full))
        assert err.value.status_code == 409
        assert "All systems are being reset" in err.value.detail
        assert c.single_ran == [], "a single removal ran during Reset All"
        assert not onboard._reset_in_progress(SID), "the refused removal left a claim"
        c.all_gate.set()
        assert (await reset_all)["status"] == "reset"
        c.single_gate.set()
        result = await onboard.onboard_reset_system(SID, wait=True, full=single_full)
        assert result["status"] == "reset"
        assert c.single_ran == [single_full]
    asyncio.run(_scenario())


def test_a_second_reset_all_is_refused_while_one_runs(monkeypatch):
    async def _scenario():
        c = _Cascades(monkeypatch)
        first = asyncio.ensure_future(onboard.onboard_reset(None))
        await asyncio.wait_for(c.all_in.wait(), 5)
        with pytest.raises(HTTPException) as err:
            await _refused(onboard.onboard_reset(None, full=True))
        assert err.value.status_code == 409
        assert c.all_ran == [False]
        c.all_gate.set()
        assert (await first)["status"] == "reset"
    asyncio.run(_scenario())


def test_a_failed_reset_all_releases_the_domain(monkeypatch):
    async def _scenario():
        c = _Cascades(monkeypatch)

        async def _fails(full):
            raise onboard.ResetIncompleteError("still deleting")
        monkeypatch.setattr(onboard, "_global_reset_steps", _fails)
        with pytest.raises(HTTPException) as err:
            await onboard.onboard_reset(None)
        assert err.value.status_code == 503
        c.single_gate.set()
        assert (await onboard.onboard_reset_system(SID, wait=True))["status"] == "reset"
    asyncio.run(_scenario())


def test_a_job_record_left_by_an_earlier_process_does_not_block_reset_all(monkeypatch):
    """A 'running' job no removal in this process owns (the task restarted
    mid-removal) is not a running removal."""
    async def _scenario():
        c = _Cascades(monkeypatch)
        onboard._RESET_JOBS[onboard._ACTIVE_RESET_KEY] = {"system_id": SID}
        onboard._RESET_JOBS[SID] = {"state": "running"}
        c.all_gate.set()
        assert (await onboard.onboard_reset(None))["status"] == "reset"
    asyncio.run(_scenario())
