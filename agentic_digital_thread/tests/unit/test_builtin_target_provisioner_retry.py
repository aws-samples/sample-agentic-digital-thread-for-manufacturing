# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two built-in gateway targets (digital-thread, query-bridge) are
provisioned by a background runner that retries until it succeeds.

Event 34 (2026-10-18): the single-shot provisioner logged "provisioning
failed (non-fatal)" once and the Quick gateway stayed empty for the life of
the task, though the same create succeeded minutes later on sibling accounts.
Offline: the ensure callable is a stub; the sleep is injected.
"""
from __future__ import annotations

import logging

import pytest

from agents import server


def _runner(outcomes, delays=(1, 2, 3)):
    """An ensure() that pops one scripted outcome per call: an exception
    instance raises, anything else is returned. Returns (run, calls, slept)."""
    calls: list[int] = []
    slept: list[int] = []

    def ensure():
        calls.append(1)
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def run():
        return server._provision_builtin_target_with_retry(
            "query-bridge", ensure, delays=delays, sleep=slept.append)

    return run, calls, slept


def test_succeeds_first_time_without_sleeping():
    run, calls, slept = _runner(["https://gw/mcp"])
    assert run() is True
    assert len(calls) == 1 and slept == []


def test_retries_after_failures_until_success():
    run, calls, slept = _runner([RuntimeError("InternalError"), TimeoutError("600s"),
                                 "https://gw/mcp"])
    assert run() is True
    assert len(calls) == 3
    assert slept == [1, 2]  # one pause per failure, in order


def test_gives_up_after_the_last_pause(caplog):
    run, calls, slept = _runner([RuntimeError("x")] * 4)
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        assert run() is False
    assert len(calls) == 4  # len(delays) + 1 attempts
    assert slept == [1, 2, 3]
    levels = [r.levelno for r in caplog.records if "query-bridge" in r.getMessage()]
    assert levels[:-1] == [logging.WARNING] * 3 and levels[-1] == logging.ERROR


def test_a_falsy_return_is_success_not_failure():
    """ensure() returns "" when it skipped (local mode): nothing to retry."""
    run, calls, slept = _runner([""])
    assert run() is True
    assert len(calls) == 1 and slept == []


def test_default_delays_span_the_cold_account_window():
    """A fresh account's control plane settles within minutes; the default
    schedule keeps trying for over half an hour without a hot loop."""
    delays = server.BUILTIN_TARGET_RETRY_DELAYS_S
    assert min(delays) >= 60
    assert sum(delays) >= 30 * 60


@pytest.mark.parametrize("starter,env", [
    ("_start_digital_thread_provisioner", {}),
    ("_start_query_bridge_provisioner", {"QUICK_API_ENABLED": "true"}),
])
def test_starters_run_the_retrying_runner_in_a_daemon_thread(monkeypatch, starter, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    started: list[dict] = []

    class _Thread:
        def __init__(self, target=None, args=(), name="", daemon=False):
            started.append({"target": target, "args": args, "name": name, "daemon": daemon})

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", _Thread)
    getattr(server, starter)()
    assert len(started) == 1
    t = started[0]
    assert t["target"] is server._provision_builtin_target_with_retry
    assert t["daemon"] is True
    assert t["args"][0] in ("digital-thread", "query-bridge")
    assert callable(t["args"][1])


def test_query_bridge_starter_is_gated_on_quick(monkeypatch):
    monkeypatch.delenv("QUICK_API_ENABLED", raising=False)
    started = []
    monkeypatch.setattr(server.threading, "Thread",
                        lambda *a, **k: started.append(1) or _Noop())
    server._start_query_bridge_provisioner()
    assert started == []


class _Noop:
    def start(self):
        pass
