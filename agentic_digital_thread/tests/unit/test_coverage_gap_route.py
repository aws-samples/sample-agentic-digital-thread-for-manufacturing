# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Coverage-gap investigate route + onboarding-completion hook — offline.

``fastapi.testclient`` against the bare learning router with the job registry
swapped for an in-memory store and the worker thread run INLINE
(deterministic — the ``test_schema_drift_job.py`` idiom). Pins:

- POST /api/admin/learning/coverage-gap-check → 202 ``{"job_id"}``; body
  ``{"system_id"}`` scopes to ONE system, ``{}``/no body checks every
  onboarded system;
- UNLIKE the drift check, concurrent jobs are ALLOWED (no single app-wide
  slot, no 409) — two stewards investigating two different cards must not
  block each other;
- the poll GET surfaces the pinned done/error shapes, sanitized on error
  (exception class only);
- ``run_coverage_gap_check`` runs scoped to ONE system after a successful
  onboarding provision, best-effort (a check failure must never fail or
  alter the provision result).
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.learning_routes as lr
from agents.job_store import JobStore


class _InlineThread:
    """Deterministic worker: ``start()`` runs the target synchronously."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _fresh_coverage_gap_jobs(monkeypatch):
    """Fresh in-memory job registry + inline worker per test."""
    store = JobStore("coverage-gap", table=None)
    monkeypatch.setattr(lr, "_COVERAGE_GAP_JOBS", store)
    monkeypatch.setattr(lr, "threading",
                        types.SimpleNamespace(Thread=_InlineThread))
    yield store


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(lr.router)
    return TestClient(app)


def test_start_scoped_to_one_system_and_poll_done(monkeypatch, client):
    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check", lambda ids: (
        {"gaps_checked": 3, "systems_checked": 1, "findings": 2, "filed": 2}
        if ids == ["sys-a"] else (_ for _ in ()).throw(AssertionError("wrong scope"))
    ))

    started = client.post("/api/admin/learning/coverage-gap-check",
                          json={"system_id": "sys-a"})
    assert started.status_code == 202
    job_id = started.json()["job_id"]

    body = client.get(f"/api/admin/learning/coverage-gap-check/{job_id}").json()
    assert body["status"] == "done"
    assert body["result"]["filed"] == 2
    assert body["result"]["systems_checked"] == 1
    assert "elapsed_seconds" in body["result"]


def test_start_with_no_body_sweeps_every_onboarded_system(monkeypatch, client):
    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check", lambda ids: (
        {"gaps_checked": 5, "systems_checked": 2, "findings": 0, "filed": 0}
        if ids is None else (_ for _ in ()).throw(AssertionError("expected None scope"))
    ))

    started = client.post("/api/admin/learning/coverage-gap-check")
    assert started.status_code == 202
    body = client.get(
        f"/api/admin/learning/coverage-gap-check/{started.json()['job_id']}"
    ).json()
    assert body["status"] == "done"
    assert body["result"]["systems_checked"] == 2


def test_concurrent_jobs_are_allowed_no_409(monkeypatch, client):
    """Unlike the drift check, two investigate clicks on different cards must
    not block each other."""
    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: {"gaps_checked": 0, "systems_checked": 0,
                                     "findings": 0, "filed": 0})

    first = client.post("/api/admin/learning/coverage-gap-check",
                        json={"system_id": "sys-a"})
    second = client.post("/api/admin/learning/coverage-gap-check",
                         json={"system_id": "sys-b"})
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]


def test_duplicate_same_scope_request_coalesces_onto_the_running_job(
        monkeypatch, _fresh_coverage_gap_jobs, client):
    """HIGH finding (closed): a second identical-scope request while one is in
    flight returns the RUNNING job's id and spawns NO second worker — two
    unscoped sweeps can no longer race the same filing/supersede pass."""
    import time as _time

    import agents.coverage_gap_analyzer as cga
    calls: list = []
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: calls.append(ids) or {
                            "gaps_checked": 0, "systems_checked": 0,
                            "findings": 0, "filed": 0})
    # Simulate an in-flight unscoped job (the inline test worker would
    # otherwise finish before the second POST).
    _fresh_coverage_gap_jobs["active:all"] = {
        "status": "running", "job_id": "inflight1", "started_at": _time.time()}

    resp = client.post("/api/admin/learning/coverage-gap-check")

    assert resp.status_code == 202
    assert resp.json()["job_id"] == "inflight1"   # coalesced, not a new job
    assert calls == []                            # NO duplicate worker ran


def test_a_stale_same_scope_slot_is_taken_over(monkeypatch,
                                               _fresh_coverage_gap_jobs, client):
    """A crashed worker's stale slot must not wedge the scope forever."""
    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: {"gaps_checked": 0, "systems_checked": 0,
                                     "findings": 0, "filed": 0})
    _fresh_coverage_gap_jobs["active:all"] = {
        "status": "running", "job_id": "stale1", "started_at": 0.0}

    resp = client.post("/api/admin/learning/coverage-gap-check")

    assert resp.status_code == 202
    assert resp.json()["job_id"] != "stale1"      # fresh takeover job
    body = client.get(
        f"/api/admin/learning/coverage-gap-check/{resp.json()['job_id']}"
    ).json()
    assert body["status"] == "done"


def test_the_scope_slot_is_released_after_the_run(monkeypatch,
                                                  _fresh_coverage_gap_jobs, client):
    """The worker's finally releases the per-scope slot, so a follow-up
    request starts a NEW job rather than coalescing onto a finished one."""
    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: {"gaps_checked": 0, "systems_checked": 0,
                                     "findings": 0, "filed": 0})

    first = client.post("/api/admin/learning/coverage-gap-check")
    second = client.post("/api/admin/learning/coverage-gap-check")

    assert first.json()["job_id"] != second.json()["job_id"]
    assert _fresh_coverage_gap_jobs["active:all"]["status"] == "idle"


def test_polling_a_scope_slot_key_is_404(client):
    resp = client.get("/api/admin/learning/coverage-gap-check/active:all")
    assert resp.status_code == 404


def test_unexpected_exception_surfaces_sanitized_error(monkeypatch, client):
    import agents.coverage_gap_analyzer as cga

    def boom(ids):
        raise TimeoutError("host graph.internal unreachable")

    monkeypatch.setattr(cga, "run_coverage_gap_check", boom)

    started = client.post("/api/admin/learning/coverage-gap-check")
    body = client.get(
        f"/api/admin/learning/coverage-gap-check/{started.json()['job_id']}"
    ).json()
    assert body["status"] == "error"
    assert "TimeoutError" in body["error"]
    assert "graph.internal" not in str(body)  # sanitized: class only, no message


def test_unknown_job_id_is_404(client):
    resp = client.get("/api/admin/learning/coverage-gap-check/does-not-exist")
    assert resp.status_code == 404


# ── Onboarding-completion hook (agents/onboard.py::_run_provision_job) ──────

def test_provision_job_triggers_a_scoped_coverage_gap_check(monkeypatch):
    import agents.onboard as onboard

    calls: list = []
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: {"ok": True})
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_provision_jobs", {})

    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift",
                        lambda *_a, **_k: None)

    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: calls.append(ids) or {
                            "gaps_checked": 0, "systems_checked": 0,
                            "findings": 0, "filed": 0})

    body = types.SimpleNamespace(system_id="sys-a", mappings=[])
    onboard._run_provision_job("job-1", body)

    assert calls == [["sys-a"]]  # scoped to THIS system, never a full sweep
    assert onboard._provision_jobs["job-1"]["status"] == "done"


def test_a_failing_coverage_gap_check_never_fails_the_provision_result(monkeypatch):
    import agents.onboard as onboard

    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: {"ok": True})
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_provision_jobs", {})

    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift",
                        lambda *_a, **_k: None)

    import agents.coverage_gap_analyzer as cga
    def boom(ids):
        raise RuntimeError("graph unreachable")
    monkeypatch.setattr(cga, "run_coverage_gap_check", boom)

    body = types.SimpleNamespace(system_id="sys-a", mappings=[])
    onboard._run_provision_job("job-1", body)

    # The provision result stands DONE despite the best-effort check failing.
    assert onboard._provision_jobs["job-1"]["status"] == "done"
    assert onboard._provision_jobs["job-1"]["result"] == {"ok": True}
