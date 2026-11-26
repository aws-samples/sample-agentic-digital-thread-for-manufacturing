# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A closed-session reopen never revives a client that an auth rebuild replaced.

A service turn's auth recovery (``_rebuild_and_retry_once``) can ADOPT a fresh
gateway client and stop the stale-bearer one while other calls of the same turn
are waiting for a delegation slot or are still in flight on the old client.
Those calls must use the client that is current when they are sent, and the
discarded client must never be reopened: its stop cleared the tool-provider
state the registry's teardown relies on, so a reopened old client would outlive
the turn with no owner (a queued call held the old client, saw it refused,
reopened it, then sent on the fresh one).

Offline fakes, real ``_wrap_gateway_client_with_retry`` and its keeper.
"""
from __future__ import annotations

import asyncio

import pytest
from strands.types.exceptions import MCPClientInitializationError

orch = pytest.importorskip("agents.orchestrator")

AUTH_ERROR = {"status": "error",
              "content": [{"text": "McpException - MCP invocation failed: Authorization error when sending message"}]}
CLOSED = {"status": "error", "content": [{"text": "Tool execution failed: Connection to the MCP server was closed"}]}
SUCCESS = {"status": "success", "content": [{"text": "rows"}]}
ERP = "erp___query_erp"


class _Client:
    """A gateway client fake: ``holds`` maps a tool name to the event its call
    waits on (it holds a delegation slot meanwhile); ``entered`` records which
    calls started. A stopped client refuses new calls as strands does, and a
    call in flight when it is stopped returns the closed-session error."""

    def __init__(self, name, auth_fails=False):
        self.name, self.auth_fails = name, auth_fails
        self.active, self.stops, self.starts = True, 0, 0
        self.sent: list = []
        self.holds: dict = {}
        self.entered: dict = {}
        self._tool_provider_started = True
        self._consumers = {"agent-registry"}

    def _is_session_active(self):
        return self.active

    async def call_tool_async(self, tool_use_id=None, name=None, arguments=None, *a, **k):
        if not self.active:
            raise MCPClientInitializationError("the client session is not running")
        self.sent.append(name)
        self.entered.setdefault(name, asyncio.Event()).set()
        if name in self.holds:
            await self.holds[name].wait()
            if not self.active:
                return CLOSED
        if self.auth_fails and name == "digital-thread___explore_graph":
            return AUTH_ERROR
        return SUCCESS

    def stop(self, *a):
        self.stops += 1
        self.active = False
        self._tool_provider_started = False
        self._consumers = set()

    def start(self):
        self.starts += 1
        self.active = True
        return self


@pytest.fixture
def turn(monkeypatch):
    """One service turn: OLD (its bearer fails auth on explore_graph) wrapped as
    production wraps it; the rebuild seam hands back FRESH."""
    monkeypatch.setattr(orch, "_GATEWAY_AUTH_RETRY_BACKOFFS_S", ())  # exhaust at once

    def build(concurrency: int):
        monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, str(concurrency))
        old, fresh = _Client("old", auth_fails=True), _Client("fresh")
        orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: fresh)
        return old, fresh
    return build


async def _until(predicate, what: str):
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"timed out waiting for {what}")


def _call(client, tool_use_id, name):
    return asyncio.ensure_future(client.call_tool_async(tool_use_id=tool_use_id, name=name, arguments={}))


def test_a_call_queued_behind_the_semaphore_never_reopens_the_replaced_client(turn):
    old, fresh = turn(concurrency=2)

    async def scenario():
        b_go, a_go, d_go = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old.holds["blocker-b"] = b_go
        fresh.holds["digital-thread___explore_graph"] = a_go
        fresh.holds["blocker-d"] = d_go
        b = _call(old, "b", "blocker-b")  # holds slot 1 on OLD
        await _until(lambda: "blocker-b" in old.entered, "B in flight")
        a = _call(old, "a", "digital-thread___explore_graph")  # auth fails, rebuild to FRESH
        await _until(lambda: "digital-thread___explore_graph" in fresh.entered,
                     "A's fresh call holding slot 2")
        d = _call(old, "d", "blocker-d")  # queued first: takes A's slot when it frees
        await asyncio.sleep(0)
        c = _call(old, "c", ERP)          # queued behind D while OLD is still current
        await asyncio.sleep(0)
        a_go.set()                        # A adopts FRESH and stops OLD
        assert await a == SUCCESS
        await _until(lambda: old.stops == 1, "OLD stopped by the adoption")
        b_go.set()                        # frees slot 1 for C
        c_result = await c
        d_go.set()
        await asyncio.gather(b, d)
        return c_result

    c_result = asyncio.run(scenario())
    assert c_result == SUCCESS
    assert old.starts == 0, "the replaced client was reopened"
    assert not old.active
    assert fresh.sent.count(ERP) == 1 and ERP not in old.sent


def test_a_call_in_flight_on_the_replaced_client_does_not_reopen_it(turn):
    old, fresh = turn(concurrency=3)

    async def scenario():
        c_go = asyncio.Event()
        old.holds[ERP] = c_go
        c = _call(old, "c", ERP)  # in flight on OLD
        await _until(lambda: ERP in old.entered, "C in flight")
        assert await _call(old, "a", "digital-thread___explore_graph") == SUCCESS
        await _until(lambda: old.stops == 1, "OLD stopped by the adoption")
        c_go.set()                # C's response is lost with OLD's session
        return await c

    c_result = asyncio.run(scenario())
    assert c_result["status"] == "error"
    assert "unknown whether the call took effect" in c_result["content"][0]["text"].lower()
    assert old.starts == 0, "the replaced client was reopened"
    assert old.sent.count(ERP) == 1 and ERP not in fresh.sent  # sent once, never again
