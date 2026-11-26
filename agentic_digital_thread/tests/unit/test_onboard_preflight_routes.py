# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the E1 preflight routes on the onboard router.

Offline: ``fastapi.testclient`` against the real ``/api/onboard`` router with
``require_onboard_access`` overridden and every impure seam monkeypatched;
the job registry is an in-memory ``JobStore("preflight", table=None)`` and
the worker thread runs INLINE (deterministic). Pins:

- POST /api/onboard/preflight → 202 ``{"job_id"}``, then the poll GET
  reaches ``done`` with the pinned result contract;
- 409 per-system single-flight (exact pinned copy; OTHER systems still 202;
  a stale claim is taken over);
- 404 for an unknown system and for an unknown/internal job id;
- an errored job surfaces the sanitized honest ``error`` line (timeout /
  ``preflight failed: <ExceptionType>`` — class only, never the message).
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.onboard as onboard  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
from agents.job_store import JobStore  # noqa: E402

_READY_RESULT = {
    "system_id": "erp", "verdict": "ready",
    "connection": {"status": "pass", "reason": "ok", "detail": "SELECT 1 ok",
                   "latency_ms": 8},
    "tables": [{"name": "work_orders", "sample_read": "ok", "detail": ""}],
    "completeness": {"tables_visible": 1, "tables_sample_read": 1,
                     "columns_discovered": 5, "physical_columns": "n/a"},
    "warnings": [], "elapsed_seconds": 0.1,
}


class _InlineThread:
    """Deterministic worker: ``start()`` runs the target synchronously."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    """Fresh in-memory job registry + inline worker per test."""
    store = JobStore("preflight", table=None)
    monkeypatch.setattr(op, "_PREFLIGHT_JOBS", store)
    # Rebind ONLY op's view of threading (never the shared module — the
    # TestClient portal spawns real threads).
    monkeypatch.setattr(op, "threading", types.SimpleNamespace(Thread=_InlineThread))
    yield store


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(op, "_catalog_entry",
                        lambda sid: {"id": sid} if sid == "erp" else None)
    app = FastAPI()
    app.include_router(onboard.router)
    app.dependency_overrides[onboard.require_onboard_access] = lambda: None
    return TestClient(app)


def test_202_then_poll_done(client, monkeypatch):
    monkeypatch.setattr(op, "run_preflight",
                        lambda sid, deadline=None: dict(_READY_RESULT))

    started = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    assert job_id

    job = client.get(f"/api/onboard/preflight/{job_id}")
    assert job.status_code == 200
    body = job.json()
    assert body["status"] == "done"
    assert body["result"] == _READY_RESULT
    # The per-system slot was released — a new preflight is accepted.
    again = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert again.status_code == 202


def test_409_per_system_single_flight(client, monkeypatch, _fresh_jobs):
    monkeypatch.setattr(op, "_run_preflight_job", lambda job_id, sid: None)
    monkeypatch.setattr(op, "_catalog_entry", lambda sid: {"id": sid})

    first = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert first.status_code == 202

    second = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert second.status_code == 409
    assert second.json() == {"error": "preflight already running for erp"}

    # Single-flight is PER SYSTEM — another system is unaffected.
    other = client.post("/api/onboard/preflight", json={"system_id": "mes"})
    assert other.status_code == 202

    # A stale running claim (>15 min) is taken over, not 409'd forever.
    _fresh_jobs[op._active_key("erp")] = {
        "status": "running", "job_id": "old",
        "started_at": time.time() - op._PREFLIGHT_STALE_SECONDS - 1}
    takeover = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert takeover.status_code == 202


def test_404_unknown_system(client):
    response = client.post("/api/onboard/preflight",
                           json={"system_id": "never-onboarded"})
    assert response.status_code == 404
    assert "never-onboarded" in response.json()["error"]

    missing = client.post("/api/onboard/preflight", json={})
    assert missing.status_code == 404


def test_unknown_and_internal_job_ids_404(client):
    assert client.get("/api/onboard/preflight/nope").status_code == 404
    # The internal per-system slot key is never a pollable job.
    assert client.get(
        f"/api/onboard/preflight/{op._active_key('erp')}").status_code == 404


def test_error_job_surfaces_sanitized_error(client, monkeypatch):
    """A crashing run stores exception CLASS only; the poll GET surfaces it
    as the contract's honest ``error`` text (JobStore's ``preflight_failed``
    sentinel never masks it)."""

    def boom(sid, deadline=None):
        raise RuntimeError("secret host db.internal:5432 exploded")

    monkeypatch.setattr(op, "run_preflight", boom)

    job_id = client.post("/api/onboard/preflight",
                         json={"system_id": "erp"}).json()["job_id"]
    body = client.get(f"/api/onboard/preflight/{job_id}").json()

    assert body["status"] == "error"
    assert body["error"] == "preflight failed: RuntimeError"
    assert "db.internal" not in str(body)


def test_timeout_job_surfaces_honest_timeout(client, monkeypatch):
    def timed_out(sid, deadline=None):
        raise op.PreflightTimeoutError("past the wall clamp")

    monkeypatch.setattr(op, "run_preflight", timed_out)

    job_id = client.post("/api/onboard/preflight",
                         json={"system_id": "erp"}).json()["job_id"]
    body = client.get(f"/api/onboard/preflight/{job_id}").json()

    assert body["status"] == "error"
    assert body["error"] == "preflight timed out"


def test_slot_released_after_error(client, monkeypatch):
    monkeypatch.setattr(op, "run_preflight",
                        lambda sid, deadline=None: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    first = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert first.status_code == 202
    # The inline worker already errored — the slot must be free again.
    second = client.post("/api/onboard/preflight", json={"system_id": "erp"})
    assert second.status_code == 202
