# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reset All across a rolling deploy, and job state every app task sees.

Answering ``started`` from ``POST /api/onboard/reset`` (instead of the final
result) would change its contract. During a rolling deploy two app versions serve the same
browsers: an old bundle reading ``started`` from a new task would reseed while
the reset still ran, and a new bundle reading the final result from an old
task would not reseed at all. Now ``POST /api/onboard/reset`` keeps its
synchronous contract (with its reset lease renewed and its writes stopped once
the lease is lost), and the background job is a new route,
``POST /api/onboard/reset-all``, with its status at
``GET /api/onboard/reset-all/{job_id}``. An old task answers the new route 405
(and its status 404), which is how the new bundle knows to fall back.

The job's records were written fail-soft and read with an eventually
consistent read, so another task could answer ``none`` for a running job, and
a job whose first record never reached the shared table still answered
``started``. Now the first record is written strictly before the job starts (a
failed write starts nothing: 503), every later record is written strictly
(a failed write is retried, and the shared table is used again once it
answers), and the status is read consistently. A single removal's status
follows the same rule.

Offline: the shared table is a fake shared by two replicas; the cascades are
recorders that can hold.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import agents.db as db
import agents.onboard as onboard
from agents import reset_lease
from agents.job_store import JobStore
from tests.unit.test_one_reset_lease import (  # noqa: F401 - fixtures
    JOB_WAIT, SID, Replica, _one_process, cascades, clock, table)


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


def _until(predicate, what: str):
    deadline = time.monotonic() + JOB_WAIT
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for {what}")


def _fail_puts_of(table, prefix: str, times: int = 10 ** 9) -> list:
    """Make the next *times* puts of items whose key starts with *prefix*
    fail as an unavailable table does; returns the list of failed keys."""
    failed: list = []
    real = table.put_item

    def _put(Item, **kw):  # noqa: N803 - boto3 kwarg
        if Item["system_id"].startswith(prefix) and len(failed) < times:
            failed.append(Item["system_id"])
            raise RuntimeError("PutItem: ProvisionedThroughputExceededException")
        return real(Item=Item, **kw)
    table.put_item = _put
    return failed


# ── the old route keeps its contract ───────────────────────────────────────

def test_the_reset_route_still_answers_the_finished_reset(cascades, client):
    """An old bundle reseeds right after this answer: the reset has ended."""
    res = client.post("/api/onboard/reset")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "reset", "full": False}
    assert cascades.calls == [("all", False)]
    assert not onboard._RESET_DOMAIN.reset_all_running()
    assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL) is None


def test_the_reset_route_reads_full_from_the_body(cascades, client):
    assert client.post("/api/onboard/reset", json={"full": True}).json()["full"] is True
    assert cascades.calls == [("all", True)]


def test_the_reset_route_renews_its_lease_and_stops_once_it_is_lost(
        clock, client, monkeypatch):
    sent: list = []
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run", lambda c, p=None: sent.append(c) or [])

    async def _steps(full):
        renewal = onboard._RESET_DOMAIN.renewal(reset_lease.ALL)
        clock.advance(onboard._RESET_LEASE_SECONDS // 2)
        assert renewal.renew_once() is True                 # renewed while it runs
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:Field) DETACH DELETE n")
        clock.advance(onboard._RESET_LEASE_SECONDS)         # then never again: lost
        await asyncio.to_thread(onboard.neptune_write, "MATCH (n:System) DETACH DELETE n")
        return {"status": "reset"}
    monkeypatch.setattr(onboard, "_global_reset_steps", _steps)
    res = client.post("/api/onboard/reset")
    assert res.status_code == 503, res.text
    assert "lease" in res.json()["detail"] and "retry the reset" in res.json()["detail"]
    assert sent == ["MATCH (n:Field) DETACH DELETE n"]
    assert not onboard._RESET_DOMAIN.reset_all_running()


# ── the new route runs it as a job ─────────────────────────────────────────

def test_the_new_route_starts_a_job_and_its_status_says_how_it_ended(cascades, client):
    cascades.hold_next = True
    res = client.post("/api/onboard/reset-all", json={"full": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "started" and body["job_id"] and body["full"] is True
    assert cascades.entered.wait(JOB_WAIT)
    status = client.get(f"/api/onboard/reset-all/{body['job_id']}").json()
    assert status["state"] == "running", status
    cascades.release.set()
    _until(lambda: client.get(f"/api/onboard/reset-all/{body['job_id']}").json()["state"]
           != "running", "the job to end")
    status = client.get(f"/api/onboard/reset-all/{body['job_id']}").json()
    assert status["state"] == "done" and status["result"]["status"] == "reset", status
    assert cascades.calls == [("all", True)]


def test_an_unknown_job_is_none(client):
    assert client.get("/api/onboard/reset-all/never").json() == {
        "state": "none", "job_id": "never"}


# ── every app task sees the job ────────────────────────────────────────────

def test_another_replica_sees_the_running_job_through_an_eventual_read_lag(
        table, cascades, client):
    a, b = Replica(table), Replica(table)
    cascades.hold_next = True
    with a:
        table.hold_back()        # an eventually consistent read misses what follows
        job_id = client.post("/api/onboard/reset-all").json()["job_id"]
        assert cascades.entered.wait(JOB_WAIT)
    with b:
        seen = client.get(f"/api/onboard/reset-all/{job_id}").json()
    with a:
        cascades.release.set()
        _until(lambda: not onboard._RESET_DOMAIN.reset_all_running(), "the job to end")
    assert seen["state"] == "running", seen
    with b:
        assert client.get(f"/api/onboard/reset-all/{job_id}").json()["state"] == "done"


def test_a_job_whose_first_record_cannot_be_written_starts_nothing(table, cascades, client):
    a = Replica(table)
    failed = _fail_puts_of(table, "__job__:reset-all:")
    with a:
        res = client.post("/api/onboard/reset-all")
        assert res.status_code == 503, res.text
        assert "Nothing was removed" in res.json()["detail"]
        assert failed, "the first record was never written"
        assert cascades.calls == []
        assert not onboard._RESET_DOMAIN.reset_all_running()
        assert onboard._RESET_LEASES.lease_read(reset_lease.RESET_LEASE_KEY,
                                                now=reset_lease.now()) is None


def test_a_terminal_record_whose_write_fails_once_still_reaches_the_shared_table(
        table, cascades, client, monkeypatch):
    monkeypatch.setattr(onboard, "_JOB_RECORD_RETRY_S", 0.0)
    a, b = Replica(table), Replica(table)
    cascades.hold_next = True
    with a:
        job_id = client.post("/api/onboard/reset-all").json()["job_id"]
        assert cascades.entered.wait(JOB_WAIT)
        failed = _fail_puts_of(table, f"__job__:reset-all:{job_id}", times=1)
        cascades.release.set()
        _until(lambda: not onboard._RESET_DOMAIN.reset_all_running(), "the job to end")
    assert failed, "the terminal write never failed"
    with b:
        assert client.get(f"/api/onboard/reset-all/{job_id}").json()["state"] == "done"


def test_a_running_record_whose_renewal_write_failed_is_written_by_the_next_one(
        table, clock, cascades, client):
    a, b = Replica(table), Replica(table)
    cascades.hold_next = True
    with a:
        job_id = client.post("/api/onboard/reset-all").json()["job_id"]
        assert cascades.entered.wait(JOB_WAIT)
        renewal = onboard._RESET_DOMAIN.renewal(reset_lease.ALL)
        failed = _fail_puts_of(table, f"__job__:reset-all:{job_id}", times=1)
        clock.advance(60)
        assert renewal.renew_once() is True     # the lease is renewed; its record write failed
        clock.advance(60)
        assert renewal.renew_once() is True
    assert failed
    with b:
        record = onboard._RESET_ALL_JOBS.get_consistent(job_id)
    assert record["state"] == "running" and record["expires_at"] == renewal.lease["expires_at"]
    with a:
        cascades.release.set()
        _until(lambda: not onboard._RESET_DOMAIN.reset_all_running(), "the job to end")


def test_a_status_that_cannot_be_read_is_a_503_not_none(table, client):
    a = Replica(table)
    with a:
        onboard._RESET_ALL_JOBS.put_strict("job-x", {"state": "running", "job_id": "job-x",
                                                     "full": False,
                                                     "expires_at": reset_lease.now() + 60})
        table.fail_reads = True
        res = client.get("/api/onboard/reset-all/job-x")
    assert res.status_code == 503, res.text


# ── a single removal's status follows the same rule ────────────────────────

def test_another_replica_sees_a_running_removal_through_an_eventual_read_lag(
        table, cascades, client, monkeypatch):
    monkeypatch.setattr(onboard, "_reset_preflight", lambda sid, full=False: (None, None))
    a, b = Replica(table), Replica(table)
    cascades.hold_next = True
    with a:
        table.hold_back()
        assert client.post(f"/api/onboard/reset/{SID}").json()["status"] == "started"
        assert cascades.entered.wait(JOB_WAIT)
    with b:
        seen = client.get(f"/api/onboard/reset/{SID}/status").json()
    with a:
        cascades.release.set()
        _until(lambda: onboard._RESET_JOBS.get_consistent(SID)["state"] != "running",
               "the removal to end")
    assert seen["state"] == "running", seen


def test_a_removal_whose_running_record_cannot_be_written_starts_nothing(
        table, cascades, client, monkeypatch):
    monkeypatch.setattr(onboard, "_reset_preflight", lambda sid, full=False: (None, None))
    failed = _fail_puts_of(table, f"__job__:reset:{SID}")
    with Replica(table):
        res = client.post(f"/api/onboard/reset/{SID}")
        assert res.status_code == 503, res.text
        assert "Nothing was removed" in res.json()["detail"]
        assert failed and cascades.calls == []
        assert onboard._RESET_DOMAIN.renewal(SID) is None


def test_a_removal_status_that_cannot_be_read_is_a_503_not_none(table, client):
    with Replica(table):
        onboard._RESET_JOBS.put_strict(SID, {"state": "running", "full": False,
                                             "expires_at": reset_lease.now() + 60})
        table.fail_reads = True
        res = client.get(f"/api/onboard/reset/{SID}/status")
    assert res.status_code == 503, res.text


def test_a_memory_only_store_answers_the_status_from_memory(client):
    onboard._RESET_ALL_JOBS = JobStore("reset-all", table=None)
    onboard._RESET_ALL_JOBS["job-m"] = {"state": "done", "job_id": "job-m"}
    assert client.get("/api/onboard/reset-all/job-m").json()["state"] == "done"


def test_a_removal_running_record_whose_renewal_write_failed_is_written_by_the_next_one(
        table, clock, cascades, client, monkeypatch):
    monkeypatch.setattr(onboard, "_reset_preflight", lambda sid, full=False: (None, None))
    a, b = Replica(table), Replica(table)
    cascades.hold_next = True
    with a:
        assert client.post(f"/api/onboard/reset/{SID}").json()["status"] == "started"
        assert cascades.entered.wait(JOB_WAIT)
        renewal = onboard._RESET_DOMAIN.renewal(SID)
        failed = _fail_puts_of(table, f"__job__:reset:{SID}", times=1)
        clock.advance(60)
        assert renewal.renew_once() is True     # the lease is renewed; its record write failed
        clock.advance(60)
        assert renewal.renew_once() is True
    assert failed
    with b:
        record = onboard._RESET_JOBS.get_consistent(SID)
    assert record["state"] == "running" and record["expires_at"] == renewal.lease["expires_at"]
    with a:
        cascades.release.set()
        _until(lambda: onboard._RESET_JOBS.get_consistent(SID)["state"] != "running",
               "the removal to end")
