# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A provision that stops on a retryable condition the server explains says so.

Every provision error dict reached the wizard as the bare ``provision_failed``,
so a provision that found another change of the system's connection holding
its keyed lock (or, for a user registration, a failed graph write) showed a
generic "provision failed" with nothing to act on. The job record, the status
route and the confirm route now carry ``reason`` / ``detail`` / ``retryable``
for exactly those errors; the message is the server's own constant, looked up
by reason (never copied from the result), and every other error stays bare.
Offline.
"""
from __future__ import annotations

import asyncio
import threading
import types

import agents.onboard as onboard
from agents import connection_lock as cl
from agents.job_store import JobStore
from agents.models import ProvisionRequest

BUSY = ("Another change to this system's connection is in progress, so it was not "
        "provisioned. Retry in a moment.")


def _run_job(monkeypatch, result) -> dict:
    jobs = JobStore("provision", table=None)
    monkeypatch.setattr(onboard, "_provision_jobs", jobs)
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: result)
    onboard._run_provision_job("job-x", ProvisionRequest(system_id="sys-a"))
    return jobs


def test_a_busy_provision_job_reports_the_servers_retryable_message(monkeypatch):
    jobs = _run_job(monkeypatch, {"error": BUSY, "reason": "busy"})
    assert jobs.get("job-x") == {"status": "error", "error": "provision_failed",
                                 "reason": "busy", "detail": BUSY, "retryable": True}
    payload = asyncio.run(onboard.onboard_provision_status("job-x"))
    assert payload == {"status": "error", "error": "provision_failed", "reason": "busy",
                       "detail": BUSY, "retryable": True}, payload


def test_the_connection_phase_busy_error_carries_its_reason(monkeypatch):
    monkeypatch.setattr(cl, "CONNECTION_LOCK_TIMEOUT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def _hold():
        with cl.connection_lock("sys-a"):
            held.set()
            release.wait(5)
    t = threading.Thread(target=_hold)
    t.start()
    held.wait(5)
    try:
        err = onboard._dp_connection_phase(types.SimpleNamespace(system_id="sys-a"))
    finally:
        release.set()
        t.join(5)
    assert err == {"error": BUSY, "reason": "busy"}, err
    record = onboard._provision_error_record(err)
    assert record["detail"] == BUSY and record["retryable"] is True, record


def test_a_failed_user_node_write_is_explained_too(monkeypatch):
    msg = getattr(onboard, "_NODE_WRITE_ERROR", "<missing>")
    jobs = _run_job(monkeypatch, {"error": msg, "reason": "node_write_failed"})
    assert jobs.get("job-x")["detail"] == msg and jobs.get("job-x")["retryable"] is True


def test_the_confirm_route_reports_the_busy_message(monkeypatch):
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: {"error": BUSY, "reason": "busy"})
    out = onboard.onboard_provision_confirm({"system_id": "sys-a"})
    assert out == {"status": "error", "error": "provision_failed", "reason": "busy",
                   "detail": BUSY, "retryable": True}, out


def test_guard_any_other_error_stays_the_bare_code(monkeypatch):
    jobs = _run_job(monkeypatch, {"error": "pgdriver said: password=hunter2 rejected"})
    assert jobs.get("job-x") == {"status": "error", "error": "provision_failed"}


def test_guard_a_known_reason_with_other_text_is_not_echoed(monkeypatch):
    jobs = _run_job(monkeypatch, {"error": "token=abc123 leaked", "reason": "busy"})
    assert jobs.get("job-x") == {"status": "error", "error": "provision_failed"}
