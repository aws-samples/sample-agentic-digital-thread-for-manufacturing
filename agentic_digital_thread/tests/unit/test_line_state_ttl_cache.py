# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``dashboard.line_state_cached`` — the short TTL,
single-flight cache in front of the line-state fan-out sweep (event-loop fix:
panel + drill + page reloads share ONE sweep per ~20s window instead of each
recomputing the full multi-machine resolve).

No AWS, no network. Threads are used for the single-flight pin (the cache is a
threading primitive because callers run in worker threads via the offload
pattern) — no sockets are opened.
"""
from __future__ import annotations

import threading
import time

import pytest

import agents.dashboard as dashboard


@pytest.fixture(autouse=True)
def _fresh_cache():
    dashboard.invalidate_line_state_cache()
    yield
    dashboard.invalidate_line_state_cache()


def test_second_call_within_ttl_serves_cached_without_recompute(monkeypatch):
    calls = {"n": 0}

    def compute(plant):
        calls["n"] += 1
        return {"plant": plant, "n": calls["n"]}

    key = ("line-state", "all")
    first = dashboard.line_state_cached(key, compute, "all")
    second = dashboard.line_state_cached(key, compute, "all")

    assert calls["n"] == 1                # resolver invoked exactly once
    assert second is first                # the cached payload is served


def test_distinct_keys_cache_independently():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    assert dashboard.line_state_cached(("line-state", "all"), compute) == 1
    assert dashboard.line_state_cached(("line-state", "Detroit"), compute) == 2
    assert dashboard.line_state_cached(("line-state-machines", "all"), compute) == 3
    # Each key now cached — repeats don't recompute.
    assert dashboard.line_state_cached(("line-state", "all"), compute) == 1
    assert calls["n"] == 3


def test_ttl_expiry_recomputes(monkeypatch):
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "0.05")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    key = ("line-state", "all")
    assert dashboard.line_state_cached(key, compute) == 1
    time.sleep(0.06)                      # let the TTL lapse
    assert dashboard.line_state_cached(key, compute) == 2
    assert calls["n"] == 2


def test_ttl_zero_disables_caching(monkeypatch):
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "0")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    key = ("line-state", "all")
    assert dashboard.line_state_cached(key, compute) == 1
    assert dashboard.line_state_cached(key, compute) == 2


def test_invalid_ttl_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "not-a-number")
    assert dashboard._line_state_ttl_seconds() == 20.0


def test_errors_are_not_cached():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("backend down")
        return "recovered"

    key = ("line-state", "all")
    with pytest.raises(RuntimeError):
        dashboard.line_state_cached(key, compute)
    assert dashboard.line_state_cached(key, compute) == "recovered"


def test_single_flight_under_concurrent_callers():
    """N concurrent callers on a COLD cache produce exactly ONE compute — the
    single-flight lock makes the others wait for (and share) the in-flight
    result instead of stampeding the resolver."""
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    calls = {"n": 0}
    results: list = [None] * n_threads

    def compute():
        calls["n"] += 1
        time.sleep(0.05)                  # a "slow sweep" while others arrive
        return {"sweep": calls["n"]}

    def worker(i):
        barrier.wait()                    # all threads hit the cold cache together
        results[i] = dashboard.line_state_cached(("line-state", "all"), compute)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    assert all(r == {"sweep": 1} for r in results)


def test_invalidate_clears_all_keys():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    dashboard.line_state_cached(("line-state", "all"), compute)
    dashboard.invalidate_line_state_cache()
    assert dashboard.line_state_cached(("line-state", "all"), compute) == 2
