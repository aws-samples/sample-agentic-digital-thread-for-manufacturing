# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the two flag-gated self-learning auto-accept triggers.

Both triggers reuse the ONE shared sweep (``self_learning.sweep_auto_accept``)
and must no-op unless SELF_LEARNING_AUTO_ACCEPT is truthy. No AWS/network:
``add_interval_job`` and the sweep are mocked.

  - ``register_auto_accept_job`` — the periodic safety-net; registers exactly one
    interval job on the SHARED scheduler when the flag is on, nothing when off.
  - ``_auto_accept_background_task`` — the post-/api/ask hook; returns a
    ``BackgroundTask`` (that runs the shared sweep) when on, ``None`` when off.
  - ``_run_auto_accept_sweep`` — the shared best-effort wrapper both triggers call;
    swallows sweep failures (never raises into scheduler/request).
"""
from __future__ import annotations

from starlette.background import BackgroundTask

import agents.self_learning as self_learning
import agents.server as server


# --------------------------------------------------------------------------- #
# 2. Scheduled safety-net: register_auto_accept_job (flag gate)
# --------------------------------------------------------------------------- #
def test_scheduled_job_registered_when_flag_on(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[tuple] = []
    monkeypatch.setattr(server, "add_interval_job",
                        lambda job_id, func, seconds: calls.append((job_id, func, seconds)))

    server.register_auto_accept_job()

    assert len(calls) == 1
    job_id, func, seconds = calls[0]
    assert job_id == "self-learning-auto-accept"
    assert seconds == 300
    assert callable(func)


def test_scheduled_job_not_registered_when_flag_off(monkeypatch):
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(server, "add_interval_job",
                        lambda *a, **k: calls.append(a))

    server.register_auto_accept_job()

    assert calls == []


def test_scheduled_job_func_invokes_shared_sweep(monkeypatch):
    """The registered callable must drive the ONE shared sweep."""
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    captured: dict = {}
    monkeypatch.setattr(server, "add_interval_job",
                        lambda job_id, func, seconds: captured.update(func=func))
    swept: list[bool] = []
    monkeypatch.setattr(self_learning, "sweep_auto_accept",
                        lambda: swept.append(True) or {"accepted": 0, "failed": 0})

    server.register_auto_accept_job()
    captured["func"]()  # simulate a scheduler tick

    assert swept == [True]


# --------------------------------------------------------------------------- #
# 3. Post-run hook: _auto_accept_background_task (flag gate)
# --------------------------------------------------------------------------- #
def test_post_run_hook_schedules_task_when_flag_on(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "true")
    swept: list[bool] = []
    monkeypatch.setattr(self_learning, "sweep_auto_accept",
                        lambda: swept.append(True) or {"accepted": 1, "failed": 0})

    task = server._auto_accept_background_task()

    assert isinstance(task, BackgroundTask)
    # Task deferred (nothing swept yet) — it only fires when the response runs it.
    assert swept == []
    task.func(*task.args, **task.kwargs)  # simulate Starlette running it post-response
    assert swept == [True]


def test_post_run_hook_none_when_flag_off(monkeypatch):
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    swept: list[bool] = []
    monkeypatch.setattr(self_learning, "sweep_auto_accept",
                        lambda: swept.append(True))

    assert server._auto_accept_background_task() is None
    assert swept == []


# --------------------------------------------------------------------------- #
# Shared wrapper: _run_auto_accept_sweep is best-effort (never raises)
# --------------------------------------------------------------------------- #
def test_run_auto_accept_sweep_swallows_failure(monkeypatch):
    def _boom():
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(self_learning, "sweep_auto_accept", _boom)

    # Must not raise into scheduler/request path.
    assert server._run_auto_accept_sweep("scheduled") == {"accepted": 0, "failed": 0}


def test_run_auto_accept_sweep_returns_result(monkeypatch):
    monkeypatch.setattr(self_learning, "sweep_auto_accept",
                        lambda: {"accepted": 3, "failed": 1})
    assert server._run_auto_accept_sweep("post-query") == {"accepted": 3, "failed": 1}
