# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
""""Remove again" is a server-side precondition, not a client guess.

"Remove again" finishes a removal that did not finish, often as a full
removal (the registration forgotten, the user's own credentials scheduled
for deletion). The client checked the catalog before posting, but a read
cannot be atomic with the POST: if another tab or Reset All recovered the
system in between (it is onboarded again, or back to Registered), the server
took the retry as a fresh removal and removed the recovered system.

The retry now says so (``retry=true``, sent by "Remove again" only). Inside
the reset claim, in the same locked section that publishes the claim, the
server refuses it (409, nothing removed) unless the system is still
half-removed (its catalog entry is ``removal_failed``, rebuilt from the
System node's marker after a restart). A fresh Remove system / Remove
registration does not send the flag and is unchanged. Offline: the cascade
and Secrets Manager are fakes; the route is called over HTTP.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import agents.connection_resolver as cr
import agents.onboard as onboard
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import _FakeSM


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def roles(request: Request, call_next):
        request.state.is_admin = True
        request.state.is_integrator = True
        return await call_next(request)

    app.include_router(onboard.router)
    return app


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: "")
    return fake


@pytest.fixture
def cascade(monkeypatch):
    """The cascade's AgentCore / graph / proposal steps, recorded; the catalog
    and the secret step are real."""
    ran: list = []
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    for target, value in (("neptune_write", MagicMock(return_value=True)),
                          ("neptune_query", MagicMock(return_value=[{}])),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", cleanup),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_reset_delete_graph_artifacts_invalidating",
                           lambda sid: ran.append(sid)),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    return ran


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


def _remove(client, *, full: bool, retry: bool):
    q = "full=true" if full else "full=false"
    return client.post(f"/api/onboard/reset/{SID}?{q}" + ("&retry=true" if retry else ""))


def _removed(client, *, full: bool, retry: bool) -> dict:
    """A removal the route accepted, polled to its end (the route always runs
    it as a background job): its final status record."""
    res = _remove(client, full=full, retry=retry)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "started", res.json()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get(f"/api/onboard/reset/{SID}/status").json()
        if status.get("state") != "running":
            return status
        time.sleep(0.02)
    pytest.fail("the removal job did not finish")


# ── a retry of a system that is no longer half-removed is refused ─────────

def test_a_retry_of_an_onboarded_system_is_refused_and_nothing_is_removed(
        cat, sm, cascade, client):
    _register_pg()
    cat.update_status(SID, "onboarded")
    res = _remove(client, full=True, retry=True)
    assert res.status_code == 409, res.text
    assert "no longer half-removed" in res.json()["detail"]
    assert "nothing was removed" in res.json()["detail"].lower()
    assert cascade == [] and sm.deleted == []
    assert cat.get(SID)["status"] == "onboarded"
    assert not onboard._reset_in_progress(SID), "a refused retry left its claim"


def test_a_retry_of_a_registered_user_system_is_refused_and_nothing_is_removed(
        cat, sm, cascade, client):
    """Back to Registered (another tab finished the default removal, or
    Reset All): a full retry would have forgotten it and scheduled its
    credentials."""
    _register_pg()
    res = _remove(client, full=True, retry=True)
    assert res.status_code == 409, res.text
    assert "no longer half-removed" in res.json()["detail"]
    assert cascade == [] and sm.deleted == []
    assert cat.get(SID)["status"] == "registered"


def test_a_retry_of_a_registered_demo_system_is_refused(cat, sm, cascade, client):
    cat.add({"id": "plant-cmms", "name": "CMMS", "status": "registered",
             "connection_type": "PostgreSQL", "schema": "cmms", "connection_origin": "demo"})
    res = client.post("/api/onboard/reset/plant-cmms?retry=true")
    assert res.status_code == 409, res.text
    assert cascade == []


def test_a_retry_of_a_system_that_is_gone_is_refused(cat, sm, cascade, client):
    res = client.post("/api/onboard/reset/nope?full=true&retry=true")
    assert res.status_code == 409, res.text
    assert "no longer half-removed" in res.json()["detail"]
    assert cascade == []


def test_a_change_landing_inside_the_claim_still_refuses_the_retry(
        cat, sm, cascade, client, monkeypatch):
    """The check sits in the same locked section that publishes the claim:
    a removal that finished just before the claim makes the retry refuse."""
    _register_pg()
    cat.mark_removal_failed(SID, True)
    real = onboard._provision_running

    def _recovered_meanwhile(system_id):
        cat.update_status(SID, "registered")
        return real(system_id)
    monkeypatch.setattr(onboard, "_provision_running", _recovered_meanwhile)
    res = _remove(client, full=True, retry=True)
    assert res.status_code == 409, res.text
    assert cascade == [] and sm.deleted == []
    assert cat.get(SID)["status"] == "registered"


# ── a retry of a half-removed system proceeds; a fresh removal is unchanged ─

def test_a_retry_of_a_half_removed_system_finishes_it(cat, sm, cascade, client):
    _register_pg()
    cat.mark_removal_failed(SID, True)
    status = _removed(client, full=True, retry=True)
    assert status["state"] == "done", status
    assert status["result"]["status"] == "reset" and status["result"]["removed"] is True
    assert cascade == [SID]
    assert cat.get(SID) is None


def test_a_default_retry_of_a_half_removed_system_keeps_its_registration(
        cat, sm, cascade, client):
    _register_pg()
    cat.mark_removal_failed(SID, False)
    assert _removed(client, full=False, retry=True)["state"] == "done"
    assert cat.get(SID)["status"] == "registered"


@pytest.mark.parametrize("full", [False, True])
def test_a_fresh_removal_is_unaffected(cat, sm, cascade, client, full):
    _register_pg()
    cat.update_status(SID, "onboarded")
    status = _removed(client, full=full, retry=False)
    assert status["state"] == "done" and status["result"]["status"] == "reset", status
    assert cascade == [SID]


def test_a_fresh_remove_registration_is_unaffected(cat, sm, cascade, client):
    _register_pg()
    assert _removed(client, full=True, retry=False)["state"] == "done"
    assert cat.get(SID) is None
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]


def test_an_async_retry_of_a_recovered_system_is_refused(cat, sm, cascade, client):
    """The card posts ?wait=false: the same refusal, before any job starts."""
    _register_pg()
    cat.update_status(SID, "onboarded")
    res = client.post(f"/api/onboard/reset/{SID}?wait=false&full=true&retry=true")
    assert res.status_code == 409, res.text
    assert onboard._RESET_JOBS.get(SID) is None
    assert cat.get(SID)["status"] == "onboarded"
