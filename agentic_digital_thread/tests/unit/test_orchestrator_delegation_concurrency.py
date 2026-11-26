# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounded-parallel delegation cap (ADR-0039).

Offline asyncio tests (no AWS / no network) pinning that:
  * ``_delegation_concurrency()`` parses ``ORCHESTRATOR_DELEGATION_CONCURRENCY``
    (default 3; 1 == sequential; garbage/empty → default; < 1 clamps to 1);
  * the per-client ``asyncio.Semaphore`` in ``_wrap_gateway_client_with_retry`` bounds
    the PEAK concurrent gateway tool calls to the configured cap;
  * a cap of 1 serializes calls;
  * the transient-retry semantics are preserved WITH the semaphore acquired per attempt;
  * the semaphore is RELEASED during the retry backoff so a slow retry does not starve
    a second concurrent call of a slot.
"""
from __future__ import annotations

import asyncio

import pytest

orch = pytest.importorskip("agents.orchestrator")


# --------------------------------------------------------------------------- #
# (a) env parsing / clamp
# --------------------------------------------------------------------------- #
def test_delegation_concurrency_env_parsing(monkeypatch):
    monkeypatch.delenv(orch.DELEGATION_CONCURRENCY_ENV, raising=False)
    assert orch._delegation_concurrency() == 3  # default

    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "1")
    assert orch._delegation_concurrency() == 1  # 1 == sequential

    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "5")
    assert orch._delegation_concurrency() == 5

    for bad in ("0", "-4", "", "garbage", "3.5"):
        monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, bad)
        assert orch._delegation_concurrency() >= 1, bad  # never 0/negative (would deadlock)
    # "0"/"-4" specifically clamp to 1; garbage/empty fall back to the default 3.
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "0")
    assert orch._delegation_concurrency() == 1
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "garbage")
    assert orch._delegation_concurrency() == 3


# --------------------------------------------------------------------------- #
# (b) the wrapper bounds peak concurrency to the cap
# --------------------------------------------------------------------------- #
class _InstrumentedClient:
    """A fake MCP client whose call_tool_async tracks current/peak in-flight count."""

    def __init__(self):
        self.current = 0
        self.peak = 0

    async def call_tool_async(self, *a, **k):
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            # Yield a few times so overlapping calls actually interleave.
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            self.current -= 1
        return {"status": "success", "content": [{"text": "ok"}]}


def _run_n_concurrent(client, n):
    async def _go():
        await asyncio.gather(*(client.call_tool_async(name=f"query_{i}") for i in range(n)))

    asyncio.run(_go())


def test_wrapper_bounds_concurrency_to_cap(monkeypatch):
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "2")
    client = _InstrumentedClient()
    orch._wrap_gateway_client_with_retry(client)
    _run_n_concurrent(client, 6)
    assert client.peak == 2, f"peak in-flight should equal the cap (2), got {client.peak}"


def test_cap_of_one_serializes(monkeypatch):
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "1")
    client = _InstrumentedClient()
    orch._wrap_gateway_client_with_retry(client)
    _run_n_concurrent(client, 6)
    assert client.peak == 1, f"cap of 1 must serialize (peak 1), got {client.peak}"


# --------------------------------------------------------------------------- #
# (c) retry semantics preserved under the semaphore
# --------------------------------------------------------------------------- #
def test_retry_still_works_under_semaphore(monkeypatch):
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "3")
    monkeypatch.setattr(orch, "_GATEWAY_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    transient = {"status": "error", "content": [{"text": "No endpoint or agent found"}]}
    success = {"status": "success", "content": [{"text": "42"}]}
    calls = {"n": 0}

    class _C:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return transient if calls["n"] == 1 else success

    c = _C()
    orch._wrap_gateway_client_with_retry(c)
    out = asyncio.run(c.call_tool_async(name="query_x"))
    assert out == success and calls["n"] == 2


def test_semaphore_released_during_backoff(monkeypatch):
    """With cap 1 and a non-trivial backoff, a call that is RETRYING (sleeping in its
    backoff) must NOT hold the single slot: a second concurrent call must be able to
    acquire it and complete during the first call's backoff window."""
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "1")
    monkeypatch.setattr(orch, "_GATEWAY_RETRY_BACKOFFS_S", (0.2, 0.2, 0.2))
    transient = {"status": "error", "content": [{"text": "No endpoint or agent found"}]}
    success = {"status": "success", "content": [{"text": "ok"}]}

    order: list[str] = []

    class _SlowRetry:
        """First call: transient then (after retries) success — spends time in backoff.
        Second call: immediate success."""

        def __init__(self):
            self.first_started = False

        async def call_tool_async(self, *a, **k):
            name = k.get("name")
            if name == "slow":
                # transient on first attempt so the wrapper enters its backoff sleep,
                # success afterwards.
                if not self.first_started:
                    self.first_started = True
                    return transient
                order.append("slow_done")
                return success
            order.append("fast_done")
            return success

    client = _SlowRetry()
    orch._wrap_gateway_client_with_retry(client)

    async def _go():
        slow = asyncio.create_task(client.call_tool_async(name="slow"))
        await asyncio.sleep(0.05)  # let 'slow' take + release the slot (it's now in backoff)
        fast = asyncio.create_task(client.call_tool_async(name="fast"))
        await asyncio.gather(slow, fast)

    asyncio.run(_go())
    # The fast call completed BEFORE the slow call's retry finished — proving the slot
    # was released during the backoff (otherwise 'slow_done' would precede 'fast_done').
    assert order and order[0] == "fast_done", (
        f"fast call should finish during slow's backoff (slot released); order={order}"
    )
