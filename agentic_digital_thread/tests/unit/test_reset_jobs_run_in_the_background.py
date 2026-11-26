# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reset All and a single removal run as background jobs, never inside the
request.

Both waited for the AgentCore deletions (up to 180 s per phase) inside the
request, past the load balancer's and CloudFront's 120 s origin timeout: the
caller saw a 504 while the reset kept running, with no way to learn how it
ended. Now Reset All runs as a job through its own route
(``POST /api/onboard/reset-all``; ``POST /api/onboard/reset`` keeps its
synchronous contract for the bundles a rolling deploy still serves, see
test_reset_all_routes_across_a_rolling_deploy.py): it claims the reset domain,
answers its refusals as before (409, 503), and starts the reset as a job the
caller polls (``GET /api/onboard/reset-all/{job_id}``); ``POST
/api/onboard/reset/{system_id}`` always starts its job (``?wait=true`` no
longer makes the request wait). A job whose reset lease is lost stops before
its next write and says so.

Offline: the cascades are replaced by steps that record their call (and hold
the first one when asked), the routes run over HTTP on the real router, the
graph backend is a recorder.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import agents.db as db
import agents.onboard as onboard
from agents import reset_lease
from tests.unit.test_one_reset_lease import (  # noqa: F401 - fixtures
    JOB_WAIT, SID, Cascades, Replica, _one_process, cascades, clock, table)


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def roles(request: Request, call_next):
        request.state.is_admin = True
        request.state.is_integrator = True
        return await call_next(request)

    app.include_router(onboard.router)
    return app


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    out: list = []
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run", lambda cypher, parameters=None: out.append(cypher) or [])
    return out


def _poll(client, job_id: str) -> dict:
    deadline = time.monotonic() + JOB_WAIT
    while time.monotonic() < deadline:
        status = client.get(f"/api/onboard/reset-all/{job_id}").json()
        if status.get("state") != "running":
            return status
        time.sleep(0.02)
    pytest.fail("the reset job did not finish")


def _ended(job_id: str) -> dict:
    """The Reset All job's own terminal record. For a test whose fake clock
    jumps past the lease: the status route then already answers a running
    record as ``reset_stopped``."""
    deadline = time.monotonic() + JOB_WAIT
    while (onboard._RESET_ALL_JOBS.get_consistent(job_id)["state"] == "running"
           and time.monotonic() < deadline):
        time.sleep(0.01)
    return onboard._RESET_ALL_JOBS.get_consistent(job_id)


def _released() -> bool:
    """Whether Reset All's claim is released (the job writes its terminal
    record first, then releases the claim)."""
    deadline = time.monotonic() + JOB_WAIT
    while onboard._RESET_DOMAIN.reset_all_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    return not onboard._RESET_DOMAIN.reset_all_running()


# ── Reset All ──────────────────────────────────────────────────────────────

def test_reset_all_answers_at_once_and_runs_as_a_job(cascades, client, clock):
    cascades.hold_next = True
    res = client.post("/api/onboard/reset-all")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "started" and body["job_id"] and body["full"] is False
    assert cascades.entered.wait(JOB_WAIT), "the job never reached its cascade"
    job_id = body["job_id"]
    status = client.get(f"/api/onboard/reset-all/{job_id}").json()
    assert status["state"] == "running", status
    assert onboard._RESET_DOMAIN.reset_all_running()
    # Its lease is renewed while it runs, and its running record follows.
    clock.advance(onboard._RESET_LEASE_SECONDS // 2)
    renewal = onboard._RESET_DOMAIN.renewal(reset_lease.ALL)
    assert renewal.renew_once() is True
    assert onboard._RESET_ALL_JOBS.get_consistent(job_id)["expires_at"] == renewal.lease["expires_at"]
    # A second one is refused at once while it runs; nothing more starts.
    second = client.post("/api/onboard/reset-all?full=true")
    assert second.status_code == 409 and "already being reset" in second.json()["detail"]
    cascades.release.set()
    status = _poll(client, job_id)
    assert status["state"] == "done" and status["result"]["status"] == "reset", status
    assert cascades.calls == [("all", False)]
    assert _released()
    assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL) is None


def test_reset_all_reads_full_from_the_body(cascades, client):
    res = client.post("/api/onboard/reset-all", json={"full": True})
    assert res.status_code == 200 and res.json()["full"] is True
    assert _poll(client, res.json()["job_id"])["state"] == "done"
    assert cascades.calls == [("all", True)]


def test_reset_all_is_refused_at_once_while_a_removal_runs(cascades, client):
    """Refused before it claims anything: no renewal of a lease it never
    took is left running, and the removal's own renewal is untouched."""
    onboard._reset_publish_claim(SID, False)
    try:
        res = client.post("/api/onboard/reset-all")
        assert onboard._RESET_DOMAIN.renewal(SID).held(), "the refusal stopped the removal's lease"
    finally:
        onboard._RESET_DOMAIN.release_single(SID)
    assert res.status_code == 409 and SID in res.json()["detail"], res.text
    assert cascades.calls == []
    assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL) is None


def test_reset_all_that_cannot_check_the_provisions_starts_nothing(cascades, client, monkeypatch):
    def _unreadable():
        raise onboard.ResetNotStartedError("The reset of all systems", "a read failed")
    monkeypatch.setattr(onboard, "_refuse_reset_all_over_provisions", _unreadable)
    res = client.post("/api/onboard/reset-all")
    assert res.status_code == 503 and "Nothing was removed" in res.json()["detail"], res.text
    assert cascades.calls == [] and not onboard._RESET_DOMAIN.reset_all_running()
    # The lease it took is given back and no longer renewed.
    assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL) is None
    assert onboard._RESET_LEASES.lease_read(reset_lease.RESET_LEASE_KEY,
                                            now=reset_lease.now()) is None


@pytest.mark.parametrize("raised,reason", [
    (onboard.ResetIncompleteError("reset of all systems is incomplete: retry the reset"),
     "reset_incomplete"),
    (RuntimeError("secret text 123"), None),
], ids=["incomplete", "unexpected"])
def test_a_reset_all_job_that_fails_says_so(client, monkeypatch, raised, reason):
    async def _fails(full):
        raise raised
    monkeypatch.setattr(onboard, "_global_reset_steps", _fails)
    status = _poll(client, client.post("/api/onboard/reset-all").json()["job_id"])
    assert status["state"] == "error", status
    assert status.get("reason") == reason
    if reason:
        assert "retry the reset" in status["error"]
    else:
        assert "secret text" not in status["error"] and status.get("error_ref")
    assert _released()


def test_a_reset_all_job_whose_lease_was_lost_stops_before_its_next_write(
        client, clock, sent, monkeypatch):
    async def _steps(full):
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:Field) DETACH DELETE n")
        clock.advance(onboard._RESET_LEASE_SECONDS)      # never renewed: lost
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:System) DETACH DELETE n")
        return {"status": "reset"}
    monkeypatch.setattr(onboard, "_global_reset_steps", _steps)
    status = _ended(client.post("/api/onboard/reset-all").json()["job_id"])
    assert status["state"] == "error" and status["reason"] == "reset_lease_lost", status
    assert sent == ["MATCH (n:Field) DETACH DELETE n"]


def test_the_in_process_reset_all_stops_once_its_lease_is_lost(clock, sent, monkeypatch):
    async def _steps(full):
        clock.advance(onboard._RESET_LEASE_SECONDS)
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:System) DETACH DELETE n")
    monkeypatch.setattr(onboard, "_global_reset_steps", _steps)
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset(None))
    assert err.value.status_code == 503 and "retry the reset" in err.value.detail
    assert sent == [] and not onboard._RESET_DOMAIN.reset_all_running()


def test_the_status_of_a_job_whose_process_stopped_is_an_error(client, clock):
    onboard._RESET_ALL_JOBS["gone-1"] = onboard._running_reset_all_record(
        "gone-1", False, clock() + 60)
    assert client.get("/api/onboard/reset-all/gone-1").json()["state"] == "running"
    clock.advance(61)
    status = client.get("/api/onboard/reset-all/gone-1").json()
    assert status["state"] == "error" and status["reason"] == "reset_stopped", status
    assert client.get("/api/onboard/reset-all/never").json() == {
        "state": "none", "job_id": "never"}


# ── a single removal ───────────────────────────────────────────────────────

def _lose_the_lease_during_the_agentcore_wait(monkeypatch, clock, ran):
    """The cascades' AgentCore cleanup outlives the lease (not renewed);
    every step after it records itself when it runs."""
    def _cleanup(*_a, **_kw):
        clock.advance(onboard._RESET_LEASE_SECONDS)
        return {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False}
    for name, step in (("_retire_or_incomplete", "retire"),
                       ("_retire_many_or_incomplete", "retire"),
                       ("_delete_own_secret_or_incomplete", "secret"),
                       ("_delete_user_registration_secrets", "secret"),
                       ("_reset_delete_graph_artifacts_invalidating", "graph"),
                       ("_global_reset_wipe_graph", "graph")):
        monkeypatch.setattr(onboard, name,
                            lambda *_a, _step=step, **_kw: ran.append(_step) or 0)
    monkeypatch.setattr(onboard, "_reset_cleanup_agentcore_sync", _cleanup)
    monkeypatch.setattr(onboard, "_global_reset_cleanup_agentcore_sync", _cleanup)
    monkeypatch.setattr(onboard, "remove_system_agent", lambda _sid: None)


def test_a_removal_whose_lease_was_lost_in_the_agentcore_wait_touches_nothing_after_it(
        clock, sent, monkeypatch):
    """The proposals store and the secret are not graph writes: the cascade
    checks its lease once the long AgentCore wait ends."""
    ran: list = []
    _lose_the_lease_during_the_agentcore_wait(monkeypatch, clock, ran)
    onboard._reset_publish_claim(SID, True)
    asyncio.run(onboard._run_reset_job(SID, full=True))
    record = onboard._RESET_JOBS[SID]
    assert record["state"] == "error" and record["reason"] == "reset_lease_lost", record
    assert ran == [], f"steps ran after the lease was lost: {ran}"
    assert onboard.catalog.get(SID)["status"] == "removal_failed"


def test_a_reset_all_whose_lease_was_lost_in_the_agentcore_wait_touches_nothing_after_it(
        client, clock, sent, monkeypatch):
    ran: list = []
    _lose_the_lease_during_the_agentcore_wait(monkeypatch, clock, ran)
    status = _ended(client.post("/api/onboard/reset-all?full=true").json()["job_id"])
    assert status["state"] == "error" and status["reason"] == "reset_lease_lost", status
    assert ran == [], f"steps ran after the lease was lost: {ran}"

def test_a_removal_job_whose_lease_was_lost_stops_before_its_next_write(
        clock, sent, monkeypatch):
    async def _steps(system_id, full=False, **_kw):
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:Field) DETACH DELETE n")
        clock.advance(onboard._RESET_LEASE_SECONDS)
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:System) DETACH DELETE n")
        return {"status": "reset"}
    monkeypatch.setattr(onboard, "_run_reset_steps", _steps)
    onboard._reset_publish_claim(SID, False)
    asyncio.run(onboard._run_reset_job(SID, full=False))
    record = onboard._RESET_JOBS[SID]
    assert record["state"] == "error" and record["reason"] == "reset_lease_lost", record
    assert sent == ["MATCH (n:Field) DETACH DELETE n"]
    assert onboard._RESET_DOMAIN.renewal(SID) is None


def test_the_in_process_removal_stops_once_its_lease_is_lost(clock, sent, monkeypatch):
    async def _steps(system_id, full=False, **_kw):
        clock.advance(onboard._RESET_LEASE_SECONDS)
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:System) DETACH DELETE n")
    monkeypatch.setattr(onboard, "_run_reset_steps", _steps)
    monkeypatch.setattr(onboard, "_reset_preflight", lambda sid, full=False: (None, None))
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset_system(SID, wait=True))
    assert err.value.status_code == 503 and "retry the removal" in err.value.detail
    assert sent == []
    record = onboard._RESET_JOBS[SID]
    assert record["state"] == "error" and record["reason"] == "reset_lease_lost", record


def test_the_removal_route_never_waits_for_the_cascade(cascades, client, monkeypatch):
    monkeypatch.setattr(onboard, "_reset_preflight", lambda sid, full=False: (None, None))
    cascades.hold_next = True
    res = client.post(f"/api/onboard/reset/{SID}?wait=true")
    assert res.status_code == 200 and res.json() == {"status": "started", "system_id": SID}
    assert cascades.entered.wait(JOB_WAIT)
    assert client.get(f"/api/onboard/reset/{SID}/status").json()["state"] == "running"
    cascades.release.set()
    deadline = time.monotonic() + JOB_WAIT
    while time.monotonic() < deadline:
        status = client.get(f"/api/onboard/reset/{SID}/status").json()
        if status["state"] != "running":
            break
        time.sleep(0.02)
    assert status["state"] == "done", status
    assert cascades.calls == [("single", SID)]


def test_the_removal_route_still_answers_its_refusals_at_once(cascades, client, monkeypatch):
    monkeypatch.setattr(onboard, "_reset_preflight", lambda sid, full=False: (None, None))
    assert onboard._RESET_DOMAIN.claim_all() is None
    try:
        res = client.post(f"/api/onboard/reset/{SID}")
        assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL).held()
    finally:
        onboard._RESET_DOMAIN.release_all()
    assert res.status_code == 409 and "All systems are being reset" in res.json()["detail"]
    assert cascades.calls == []
    assert onboard._RESET_DOMAIN.renewal(SID) is None, "a refused removal left a renewal"


def test_a_refused_in_process_reset_all_raises_its_409_and_keeps_the_running_one(cascades):
    assert onboard._RESET_DOMAIN.claim_all() is None
    renewal = onboard._RESET_DOMAIN.renewal(reset_lease.ALL)
    try:
        with pytest.raises(HTTPException) as err:
            asyncio.run(onboard.onboard_reset(None))
        assert renewal.held(), "the refused one stopped the running one's renewal"
        assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL) is renewal
    finally:
        onboard._RESET_DOMAIN.release_all()
    assert err.value.status_code == 409
    assert not renewal.held()
