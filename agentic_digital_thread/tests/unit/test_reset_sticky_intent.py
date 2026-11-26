# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Removal intent is STICKY: a retry cannot change the mode of a removal that
did not finish.

A full removal of a USER system schedules its own ``adt/conn/<id>`` secret for
deletion before the graph and catalog steps. When a later step fails the entry
becomes ``removal_failed`` and the reset job records an error. The error
record used to drop ``full``, so a retry through the card ran as a default
removal: it finished the cascade, put the entry back to ``registered`` and
reported ``kept_registration=true`` while the credentials were already pending
deletion.

Every reset job state (running / done / error) now carries ``full``, the
status route returns it, and a retry asking the other mode is refused with 409
instead of being run. Offline: Secrets Manager and the graph are fakes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import agents.connection_resolver as cr
import agents.onboard as onboard
from agents.catalog import REMOVAL_FAILED
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import _FakeSM


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    fake.nodes = {}
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: fake.nodes.get(sid, ""))
    return fake


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    """Each test starts with an empty, in-memory reset job store."""
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))


class _Graph:
    """The cascade's graph-delete step; ``fail`` makes it raise."""

    def __init__(self):
        self.fail = False
        self.calls = 0

    def __call__(self, sid):
        self.calls += 1
        if self.fail:
            raise RuntimeError("transient graph delete failure")


@pytest.fixture
def graph():
    return _Graph()


@pytest.fixture
def stubs(cat, graph):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    patches = [patch.object(onboard, "neptune_write", MagicMock(return_value=True)),
               patch.object(onboard, "neptune_query", MagicMock(return_value=[{}])),
               patch.object(onboard, "_agent_manager", MagicMock()),
               patch.object(onboard, "remove_system_agent", MagicMock()),
               patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
               patch.object(onboard, "_retire_or_incomplete", lambda sid: 0),
               patch.object(onboard, "_reset_delete_graph_artifacts_invalidating", graph),
               patch.object(onboard, "_reregister_demo_system", lambda sid: False)]
    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


def _onboarded_user_pg(cat):
    _register_pg()
    cat.update_status(SID, "onboarded")


async def _remove_async_and_wait(system_id: str, full: bool) -> dict:
    """The card's path: POST ?wait=false, then poll until the job settles."""
    started = await onboard.onboard_reset_system(system_id, wait=False, full=full)
    assert started.get("status") == "started", started
    for _ in range(500):
        job = onboard._RESET_JOBS.get(system_id) or {}
        if job.get("state") != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("the removal job never settled")


def _failed_full_removal(cat, graph, sm) -> None:
    """A full removal that fails at the graph step, after its secret was
    scheduled for deletion."""
    graph.fail = True
    job = asyncio.run(_remove_async_and_wait(SID, full=True))
    assert job["state"] == "error"
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]
    assert cat.get(SID)["status"] == REMOVAL_FAILED
    graph.fail = False


# ── every state carries the mode; the status route returns it ──────────────

def test_a_failed_async_full_removal_keeps_full_on_its_error_record(cat, secrets, sm, graph, stubs):
    _onboarded_user_pg(cat)
    _failed_full_removal(cat, graph, sm)
    status = asyncio.run(onboard.onboard_reset_status(SID))
    assert status["state"] == "error"
    assert status.get("full") is True, "the error record dropped the full-removal intent"


def test_a_failed_sync_removal_keeps_its_mode_on_the_error_record(cat, secrets, sm, graph, stubs):
    _onboarded_user_pg(cat)
    graph.fail = True
    with pytest.raises(Exception):
        asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=True))
    assert onboard._RESET_JOBS[SID].get("full") is True


def test_a_finished_removal_records_its_mode(cat, secrets, sm, graph, stubs):
    _onboarded_user_pg(cat)
    asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=False))
    assert onboard._RESET_JOBS[SID]["state"] == "done"
    assert onboard._RESET_JOBS[SID].get("full") is False


# ── a retry in the other mode is refused, never run ─────────────────────────

def test_a_default_retry_of_a_failed_full_removal_is_refused(cat, secrets, sm, graph, stubs):
    """After a full removal failed with its secret scheduled, a DEFAULT retry
    is refused (409) instead of keeping a registration whose credentials are
    pending deletion."""
    _onboarded_user_pg(cat)
    _failed_full_removal(cat, graph, sm)
    calls_before = graph.calls
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=False))
    assert err.value.status_code == 409
    assert "full=true" in err.value.detail
    assert graph.calls == calls_before, "the refused retry ran the cascade"
    assert cat.get(SID)["status"] == REMOVAL_FAILED
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]


def test_an_async_default_retry_of_a_failed_full_removal_is_refused(cat, secrets, sm, graph, stubs):
    _onboarded_user_pg(cat)
    _failed_full_removal(cat, graph, sm)
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset_system(SID, wait=False, full=False))
    assert err.value.status_code == 409
    assert onboard._RESET_JOBS[SID]["state"] == "error"


def test_a_full_retry_of_a_failed_default_removal_is_refused(cat, secrets, sm, graph, stubs):
    _onboarded_user_pg(cat)
    graph.fail = True
    job = asyncio.run(_remove_async_and_wait(SID, full=False))
    assert job["state"] == "error"
    graph.fail = False
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=True))
    assert err.value.status_code == 409
    assert "full=false" in err.value.detail
    assert sm.deleted == []


def test_a_full_retry_of_a_failed_full_removal_completes(cat, secrets, sm, graph, stubs):
    """The matching retry finishes the removal: registration gone."""
    _onboarded_user_pg(cat)
    _failed_full_removal(cat, graph, sm)
    result = asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=True))
    assert result["status"] == "reset" and result["removed"] is True
    assert cat.get(SID) is None
