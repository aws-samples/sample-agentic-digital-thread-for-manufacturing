# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline route tests for the ADR-0063 context-placement check."""
from __future__ import annotations

import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.learning_routes as lr
from agents.job_store import JobStore


class _AdminMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


class _InlineThread:
    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _client(is_admin: bool) -> TestClient:
    app = FastAPI()
    app.include_router(lr.router)
    app.add_middleware(_AdminMiddleware, is_admin=is_admin)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    store = JobStore("context-placement", table=None)
    monkeypatch.setattr(lr, "_CONTEXT_PLACEMENT_JOBS", store)
    monkeypatch.setattr(lr, "threading", types.SimpleNamespace(Thread=_InlineThread))
    return store


def test_post_and_get_fail_closed_for_non_admin():
    client = _client(False)
    assert client.post("/api/admin/learning/context-placement-check").status_code == 403
    assert client.get("/api/admin/learning/context-placement-check/unknown").status_code == 403


def test_scoped_start_runs_and_poll_returns_done(monkeypatch):
    import agents.context_placement as cp
    calls = []
    monkeypatch.setattr(cp, "run_context_placement_check", lambda ids: (
        calls.append(ids) or {"systems_checked": 2, "terms_checked": 3,
                              "findings": 1, "filed": 1, "resolved_swept": 0,
                              "skipped": [], "truncated": False}))
    client = _client(True)

    started = client.post("/api/admin/learning/context-placement-check",
                          json={"system_id": "sys-a"})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    result = client.get(
        f"/api/admin/learning/context-placement-check/{job_id}").json()

    assert calls == [["sys-a"]]
    assert result["status"] == "done"
    assert result["result"]["filed"] == 1
    assert "elapsed_seconds" in result["result"]


def test_duplicate_running_scope_coalesces_without_second_worker(
        monkeypatch, _fresh_jobs):
    import agents.context_placement as cp
    calls = []
    monkeypatch.setattr(cp, "run_context_placement_check",
                        lambda ids: calls.append(ids) or {})
    _fresh_jobs["active:all"] = {
        "status": "running", "job_id": "inflight", "started_at": time.time()}

    response = _client(True).post(
        "/api/admin/learning/context-placement-check")

    assert response.status_code == 202
    assert response.json() == {"job_id": "inflight"}
    assert calls == []


def test_different_scopes_have_independent_slots(monkeypatch):
    import agents.context_placement as cp
    monkeypatch.setattr(cp, "run_context_placement_check", lambda _ids: {
        "systems_checked": 0, "terms_checked": 0, "findings": 0, "filed": 0,
        "resolved_swept": 0, "skipped": [], "truncated": False})
    client = _client(True)

    first = client.post("/api/admin/learning/context-placement-check",
                        json={"system_id": "sys-a"})
    second = client.post("/api/admin/learning/context-placement-check",
                         json={"system_id": "sys-b"})

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]


def test_stale_slot_is_replaced(monkeypatch, _fresh_jobs):
    import agents.context_placement as cp
    monkeypatch.setattr(cp, "run_context_placement_check", lambda _ids: {
        "systems_checked": 0, "terms_checked": 0, "findings": 0, "filed": 0,
        "resolved_swept": 0, "skipped": [], "truncated": False})
    _fresh_jobs["active:all"] = {
        "status": "running", "job_id": "stale", "started_at": 0.0}

    response = _client(True).post(
        "/api/admin/learning/context-placement-check")

    assert response.status_code == 202
    assert response.json()["job_id"] != "stale"


def test_scope_slot_key_and_unknown_job_are_not_pollable():
    client = _client(True)
    assert client.get(
        "/api/admin/learning/context-placement-check/active:all").status_code == 404
    assert client.get(
        "/api/admin/learning/context-placement-check/missing").status_code == 404
