# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E5 drift-check async job + scheduler (ADR-0053) — offline.

``fastapi.testclient`` against the bare learning router with every impure
seam monkeypatched (the ``test_tryit_job.py`` idiom); the job registry is an
in-memory ``JobStore("drift", table=None)`` and the worker thread runs INLINE
(deterministic). Pins:

- POST /api/admin/learning/drift-check → 202 ``{"job_id"}``; body
  ``{"system_id"}`` targets ONE system, ``{}``/no body sweeps ALL onboarded
  systems (serially);
- 409 while a drift job is running (ONE concurrent job app-wide; a stale
  claim is taken over);
- per-system skips are HONEST (``skipped:no-baseline`` /
  ``skipped:<ExceptionClass>`` — never a fabricated empty diff);
- an errored job surfaces the sanitized ``error`` line (exception CLASS
  only — the JobStore sentinel never masks it);
- the scheduler seam: ``DRIFT_CHECK_INTERVAL_SECONDS`` env-tunable, default
  86400 (daily), ``0`` disables (no job registered).
"""
from __future__ import annotations

import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.learning_routes as lr
import agents.prompts as prompts
import agents.schema_check as schema_check
import agents.schema_drift as schema_drift
from agents.job_store import JobStore

_OK_ENTRY = {"system_id": "erp", "status": "ok", "drifts_found": 2,
             "proposals_filed": 2, "proposal_noops": 0, "kinds": {"column_added": 1,
                                             "column_removed": 1}}


class _InlineThread:
    """Deterministic worker: ``start()`` runs the target synchronously."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _fresh_drift_jobs(monkeypatch):
    """Fresh in-memory job registry + inline worker per test."""
    store = JobStore("drift", table=None)
    monkeypatch.setattr(lr, "_DRIFT_JOBS", store)
    monkeypatch.setattr(lr, "threading",
                        types.SimpleNamespace(Thread=_InlineThread))
    yield store


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(lr.router)
    return TestClient(app)


# ── run_drift_check honest skips (no routes involved) ───────────────────────

def test_skips_are_honest(monkeypatch):
    filed: list = []
    monkeypatch.setattr(schema_drift, "_file_drift_proposals",
                        lambda sid, drifts: filed.append(drifts) or 0)

    # No baseline in the graph ⇒ skipped:no-baseline.
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)
    assert schema_drift.run_drift_check("erp") == {
        "system_id": "erp", "status": "skipped:no-baseline",
        "drifts_found": 0, "proposals_filed": 0, "proposal_noops": 0, "kinds": {},
        "resolved_swept": 0, "refiled_after_accept": 0}

    baseline = {"work_orders": {"columns": ["id"], "concept_map": {},
                                "relation_kind": "base_table"}}
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: baseline)

    # Discover raising ⇒ skipped:<ExceptionClass> (class only, no message).
    def boom(sid):
        raise TimeoutError("host db.internal unreachable")

    monkeypatch.setattr(schema_drift, "_discover_fresh", boom)
    skipped = schema_drift.run_drift_check("erp")
    assert skipped["status"] == "skipped:TimeoutError"
    assert "db.internal" not in str(skipped)

    # Discover returning an error payload ⇒ skipped:DiscoveryError.
    monkeypatch.setattr(schema_drift, "_discover_fresh",
                        lambda sid: {"error": "no creds"})
    assert schema_drift.run_drift_check("erp")["status"] == "skipped:DiscoveryError"

    assert filed == []  # a skipped system never files anything


def test_scheduled_sweep_is_serial_and_survives_a_bad_system(monkeypatch):
    order: list = []

    def fake_check(sid):
        order.append(sid)
        if sid == "bad":
            raise RuntimeError("boom")
        return dict(_OK_ENTRY, system_id=sid)

    monkeypatch.setattr(schema_drift, "onboarded_system_ids",
                        lambda: ["bad", "erp", "mes"])
    monkeypatch.setattr(schema_drift, "run_drift_check", fake_check)

    results = schema_drift.run_scheduled_drift_check()

    assert order == ["bad", "erp", "mes"]  # SERIAL, deterministic order
    assert results[0]["status"] == "skipped:RuntimeError"  # honest, not fatal
    assert [r["system_id"] for r in results] == ["bad", "erp", "mes"]


# ── routes ───────────────────────────────────────────────────────────────────

def test_202_then_poll_done_single_system(client, monkeypatch):
    monkeypatch.setattr(schema_drift, "run_drift_check",
                        lambda sid: dict(_OK_ENTRY, system_id=sid))

    started = client.post("/api/admin/learning/drift-check",
                          json={"system_id": "erp"})
    assert started.status_code == 202
    job_id = started.json()["job_id"]

    body = client.get(f"/api/admin/learning/drift-check/{job_id}").json()
    assert body["status"] == "done"
    assert body["result"]["systems"] == [_OK_ENTRY]
    assert isinstance(body["result"]["elapsed_seconds"], float)
    # The app-wide slot was released — a new job is accepted.
    assert client.post("/api/admin/learning/drift-check",
                       json={}).status_code == 202


def test_empty_body_sweeps_all_onboarded(client, monkeypatch):
    checked: list = []
    monkeypatch.setattr(schema_drift, "onboarded_system_ids",
                        lambda: ["cmms", "erp"])
    monkeypatch.setattr(
        schema_drift, "run_drift_check",
        lambda sid: checked.append(sid) or dict(_OK_ENTRY, system_id=sid))

    job_id = client.post("/api/admin/learning/drift-check",
                         json={}).json()["job_id"]

    assert checked == ["cmms", "erp"]  # all onboarded, serial
    body = client.get(f"/api/admin/learning/drift-check/{job_id}").json()
    assert [s["system_id"] for s in body["result"]["systems"]] == ["cmms", "erp"]


def test_409_single_flight(client, monkeypatch, _fresh_drift_jobs):
    monkeypatch.setattr(lr, "_run_drift_job", lambda job_id, ids: None)

    first = client.post("/api/admin/learning/drift-check",
                        json={"system_id": "erp"})
    assert first.status_code == 202

    # ONE concurrent drift job APP-WIDE — even for a different system.
    second = client.post("/api/admin/learning/drift-check",
                         json={"system_id": "mes"})
    assert second.status_code == 409
    assert second.json() == {"error": "drift check already running"}

    # A stale running claim (>15 min) is taken over, not 409'd forever.
    _fresh_drift_jobs[lr._DRIFT_ACTIVE_KEY] = {
        "status": "running", "job_id": "old",
        "started_at": time.time() - lr._DRIFT_STALE_SECONDS - 1}
    takeover = client.post("/api/admin/learning/drift-check",
                           json={"system_id": "erp"})
    assert takeover.status_code == 202


def test_error_job_surfaces_sanitized_error(client, monkeypatch):
    def boom(sid):
        raise RuntimeError("secret host db.internal:5432 exploded")

    monkeypatch.setattr(schema_drift, "run_drift_check", boom)

    job_id = client.post("/api/admin/learning/drift-check",
                         json={"system_id": "erp"}).json()["job_id"]
    body = client.get(f"/api/admin/learning/drift-check/{job_id}").json()

    assert body["status"] == "error"
    assert body["error"] == "drift check failed: RuntimeError"
    assert "db.internal" not in str(body)
    # The slot was released even on error.
    assert client.post("/api/admin/learning/drift-check",
                       json={"system_id": "erp"}).status_code == 202


def test_unknown_and_internal_job_ids_404(client):
    assert client.get("/api/admin/learning/drift-check/nope").status_code == 404
    # The internal app-wide slot key is never a pollable job.
    assert client.get(
        f"/api/admin/learning/drift-check/{lr._DRIFT_ACTIVE_KEY}"
    ).status_code == 404


# ── scheduler seam ───────────────────────────────────────────────────────────

def _spy_scheduler(monkeypatch) -> list:
    registered: list = []
    monkeypatch.setattr(
        prompts, "add_interval_job",
        lambda job_id, func, seconds: registered.append(
            (job_id, func, seconds)))
    return registered


def test_scheduler_env_zero_disables(monkeypatch):
    registered = _spy_scheduler(monkeypatch)
    monkeypatch.setenv("DRIFT_CHECK_INTERVAL_SECONDS", "0")
    schema_drift.register_drift_check_job()
    assert registered == []  # 0 disables — no job registered


def test_scheduler_defaults_to_daily(monkeypatch):
    registered = _spy_scheduler(monkeypatch)
    monkeypatch.delenv("DRIFT_CHECK_INTERVAL_SECONDS", raising=False)
    schema_drift.register_drift_check_job()
    assert registered == [("schema-drift-check",
                           schema_drift.run_scheduled_drift_check, 86400)]


def test_scheduler_env_tunable_and_invalid_falls_back(monkeypatch):
    registered = _spy_scheduler(monkeypatch)
    monkeypatch.setenv("DRIFT_CHECK_INTERVAL_SECONDS", "3600")
    schema_drift.register_drift_check_job()
    assert registered[-1][2] == 3600

    monkeypatch.setenv("DRIFT_CHECK_INTERVAL_SECONDS", "not-a-number")
    schema_drift.register_drift_check_job()
    assert registered[-1][2] == 86400  # honest fallback to the default
