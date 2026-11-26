# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A removal stays refused until the whole provision job has ended.

The in-process provision count wrapped only the provision steps, so it was
released when ``_do_provision`` returned. The background job then kept
writing for the same system: the schema relation metadata, the drift
supersede sweep, the coverage-gap check, the cross-system mining and the
schema-shape capture (and the confirm path wrote the relation metadata after
the count too). A removal admitted then could delete the graph while those
phases were still reading and writing it, leaving stale findings behind.

The count now covers the whole provision job and the whole confirm, until
they reach a terminal state, and is released when they fail. The job also
reported ``done`` before those phases ran, so a client that saw ``done`` and
removed the system right away got a 409: the job is now reported finished
only once its last phase is over and the count is released. Offline: each
phase is replaced by a step that waits on an event.
"""
from __future__ import annotations

import asyncio
import threading
import types

import pytest
from fastapi import HTTPException

import agents.onboard as onboard
from agents.catalog import SystemCatalog

SID = "line-mes"

_BODY = types.SimpleNamespace(system_id=SID, mappings=[{"table": "t", "field": "f"}],
                              connection_type="PostgreSQL")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    if hasattr(onboard, "_ResetDomain"):
        monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.setattr(onboard, "_provision_jobs", {})
    cat = SystemCatalog([{"id": SID, "name": "Line MES", "status": "onboarded",
                          "connection_type": "PostgreSQL", "schema": "mes"}])
    monkeypatch.setattr(onboard, "catalog", cat)
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body, dry_run=False: {
        "status": "provisioned", "system_id": SID})
    for name in ("_persist_schema_relation_kinds", "_post_provision_mining",
                 "_post_provision_schema_shape"):
        monkeypatch.setattr(onboard, name, lambda *a, **k: None)
    import agents.coverage_gap_analyzer as gap
    import agents.schema_drift as drift
    monkeypatch.setattr(drift, "sweep_resolved_drift", lambda *a, **k: None)
    monkeypatch.setattr(gap, "run_coverage_gap_check", lambda *a, **k: None)


@pytest.fixture
def cascade(monkeypatch):
    ran: list = []

    async def _steps(system_id, full=False, **_kw):
        ran.append(system_id)
        return {"status": "reset", "system_id": system_id}
    monkeypatch.setattr(onboard, "_run_reset_steps", _steps)
    return ran


def _remove():
    return asyncio.run(onboard.onboard_reset_system(SID, wait=True))


def _blocking(*, raises: bool = False):
    entered, release = threading.Event(), threading.Event()

    def _step(*_a, **_kw):
        entered.set()
        release.wait(10)
        if raises:
            raise RuntimeError("phase failed")
    return _step, entered, release


_PHASES = {
    "relation kinds": ("agents.onboard", "_persist_schema_relation_kinds"),
    "drift sweep": ("agents.schema_drift", "sweep_resolved_drift"),
    "coverage gap": ("agents.coverage_gap_analyzer", "run_coverage_gap_check"),
    "mining": ("agents.onboard", "_post_provision_mining"),
    "schema shape": ("agents.onboard", "_post_provision_schema_shape"),
}


def _patch_phase(monkeypatch, phase: str, step) -> None:
    import importlib
    module, attr = _PHASES[phase]
    monkeypatch.setattr(importlib.import_module(module), attr, step)


@pytest.mark.parametrize("phase", list(_PHASES))
def test_a_removal_is_refused_until_the_provision_job_ends(monkeypatch, cascade, phase):
    step, entered, release = _blocking()
    _patch_phase(monkeypatch, phase, step)
    onboard._provision_jobs["j1"] = {"status": "running"}     # as the route records it
    t = threading.Thread(target=onboard._run_provision_job, args=("j1", _BODY), daemon=True)
    t.start()
    assert entered.wait(5), f"the job never reached the {phase} phase"
    try:
        assert onboard._provision_jobs["j1"]["status"] == "running", \
            f"the job said done during the {phase} phase"
        with pytest.raises(HTTPException) as err:
            _remove()
        assert err.value.status_code == 409
        assert "being provisioned" in err.value.detail
        assert cascade == [], f"the removal ran during the {phase} phase"
    finally:
        release.set()
        t.join(10)
    assert onboard._provision_jobs["j1"]["status"] == "done"
    assert _remove()["status"] == "reset"
    assert cascade == [SID]


@pytest.mark.parametrize("outcome", ["done", "error"])
def test_the_job_is_reported_finished_only_after_the_count_is_released(
        monkeypatch, cascade, outcome):
    """A client polling the job sees ``done`` (or ``error``) only when a
    removal of the system would be admitted."""
    seen: list = []

    class _Jobs(dict):
        def __setitem__(self, key, value):
            if value.get("status") in ("done", "error"):
                seen.append((value["status"], onboard._provision_running(SID)))
            super().__setitem__(key, value)
    monkeypatch.setattr(onboard, "_provision_jobs", _Jobs())
    if outcome == "error":
        def _fails(*_a, **_k):
            raise RuntimeError("relation metadata write failed")
        _patch_phase(monkeypatch, "relation kinds", _fails)
    onboard._run_provision_job("j3", _BODY)
    assert seen == [(outcome, False)], seen


def test_a_failed_provision_job_releases_the_count(monkeypatch, cascade):
    step, entered, release = _blocking(raises=True)
    _patch_phase(monkeypatch, "relation kinds", step)
    release.set()
    onboard._run_provision_job("j2", _BODY)
    assert onboard._provision_jobs["j2"]["status"] == "error"
    assert _remove()["status"] == "reset"


def test_a_removal_is_refused_until_the_confirm_ends(monkeypatch, cascade):
    step, entered, release = _blocking()
    _patch_phase(monkeypatch, "relation kinds", step)
    import agents.models as models
    monkeypatch.setattr(models, "ProvisionRequest", lambda **kw: _BODY)
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "r", onboard.onboard_provision_confirm({"provision_body": {}})), daemon=True)
    t.start()
    assert entered.wait(5)
    try:
        with pytest.raises(HTTPException) as err:
            _remove()
        assert err.value.status_code == 409
        assert cascade == []
    finally:
        release.set()
        t.join(10)
    assert "error" not in out["r"] and out["r"]["system_id"] == SID, out
    assert _remove()["status"] == "reset"


def test_a_failed_confirm_releases_the_count(monkeypatch, cascade):
    step, _entered, release = _blocking(raises=True)
    release.set()
    _patch_phase(monkeypatch, "relation kinds", step)
    import agents.models as models
    monkeypatch.setattr(models, "ProvisionRequest", lambda **kw: _BODY)
    assert onboard.onboard_provision_confirm({"provision_body": {}})["status"] == "error"
    assert _remove()["status"] == "reset"
