# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway MCP dead-session restart (workshop event 31 finding).

The gateway client's background session can close mid-turn: the first call
fails with "Connection to the MCP server was closed" and every later call in the
turn fails with "the client session is not running" (strands raises that one
before sending). The retry wrapper's session keeper restarts the SAME client in
place, one restart shared by concurrent calls and at most one per
``_GATEWAY_SESSION_RESTART_COOLDOWN_S``.

Which calls go out again is decided by the client's state, never by the tool's
name or the error text: a call refused because the session was already closed
never reached the gateway and is sent after the restart; a call whose session
closed under it may have acted (a ``query_<id>`` delegation can reach a
customer MCP server's write tools), so it is never sent again and gets an
"outcome unknown" error. Real-MCP-transport coverage of the same rules:
``test_orchestrator_gateway_session_reopen.py``. Offline: fake clients, no
network, no real sleeps.
"""
from __future__ import annotations

import asyncio

import pytest
from strands.types.exceptions import MCPClientInitializationError

orch = pytest.importorskip("agents.orchestrator")

CLOSED = {"status": "error", "content": [{"text": "Tool execution failed: Connection to the MCP server was closed"}]}
SUCCESS = {"status": "success", "content": [{"text": "rows"}]}


class _FakeClient:
    """Dead until restarted: the first call goes out and its session closes
    under it (CLOSED), later calls are refused before sending (strands raises
    MCPClientInitializationError), and start() brings the session back."""

    def __init__(self):
        self.alive = True
        self.dies_on_next_call = True
        self.starts = 0
        self.stops = 0
        self.calls = []
        self._tool_provider_started = True

    def _is_session_active(self):
        return self.alive

    async def call_tool_async(self, tool_use_id, name, arguments=None, **kw):
        if not self.alive:
            raise MCPClientInitializationError(
                "the client session is not running. Ensure the agent is used within ...")
        self.calls.append(name)
        if self.dies_on_next_call:
            self.dies_on_next_call = False
            self.alive = False
            return CLOSED
        return SUCCESS

    def stop(self, *a):
        self.stops += 1
        self.alive = False
        self._tool_provider_started = False

    def start(self):
        self.starts += 1
        self.alive = True
        return self

    def list_tools_sync(self, *a, **k):
        return []


_wrap = None  # bound per test by the autouse fixture below


@pytest.fixture(autouse=True)
def _production_chain(monkeypatch):
    """Wrap a fake exactly as a turn's gateway client is built
    (``_build_gateway_client``, service turn): every wrapper in production
    order, so no inner wrapper can replay a call unseen."""
    from types import SimpleNamespace

    global _wrap
    config = SimpleNamespace(agentcore_gateway_url="https://gw.example.com/mcp")
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")

    def wrap(client):
        monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: client)
        return orch._build_gateway_client(config, None)

    _wrap = wrap
    yield
    _wrap = None


def _text(result) -> str:
    return " ".join(b.get("text", "") for b in result.get("content") or []).lower()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fake_sleep(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


def test_closed_is_judged_by_client_state_not_by_text_or_tool_name():
    """A query tool whose server writes the closed-session text on an OPEN
    session gets that result back: no restart, no second send."""
    client = _FakeClient()
    client.dies_on_next_call = False

    async def _server_text(tool_use_id, name, arguments=None, **kw):
        client.calls.append(name)
        return CLOSED

    client.call_tool_async = _server_text
    wrapped = _wrap(client)
    result = asyncio.run(wrapped.call_tool_async("t0", "erp___query_erp", {}))
    assert result is CLOSED
    assert client.calls == ["erp___query_erp"] and client.starts == 0


def test_a_sent_call_whose_session_closed_is_not_replayed_even_for_a_query_tool():
    client = _FakeClient()
    wrapped = _wrap(client)
    result = asyncio.run(wrapped.call_tool_async("t1", "erp___query_erp", {"q": "x"}))
    assert result["status"] == "error" and "unknown whether the call took effect" in _text(result)
    assert client.calls == ["erp___query_erp"]  # sent once, never again
    # Restarted in place for the next call, and the Agent's later
    # remove_consumer must still tear the new session down.
    assert client.starts == 1 and client.stops == 1
    assert client._tool_provider_started is True
    assert asyncio.run(wrapped.call_tool_async("t1b", "erp___query_erp", {})) == SUCCESS


def test_not_running_raise_is_absorbed_and_healed():
    client = _FakeClient()
    client.alive = False  # the session already died on an earlier call
    client.dies_on_next_call = False
    wrapped = _wrap(client)
    assert asyncio.run(wrapped.call_tool_async("t2", "erp___query_erp", {})) == SUCCESS
    assert client.starts == 1 and client.calls == ["erp___query_erp"]


def test_write_tool_is_not_replayed():
    client = _FakeClient()
    wrapped = _wrap(client)
    result = asyncio.run(wrapped.call_tool_async("t3", "digital-thread___save_correlation", {}))
    assert result["status"] == "error" and "unknown" in _text(result)
    assert client.calls == ["digital-thread___save_correlation"]


def test_concurrent_dead_calls_share_one_restart():
    """The first call loses its session; the two refused behind it are sent
    after ONE restart."""
    client = _FakeClient()
    wrapped = _wrap(client)

    async def _run():
        return await asyncio.gather(*[
            wrapped.call_tool_async(f"t{i}", "erp___query_erp", {}) for i in range(3)
        ])

    results = asyncio.run(_run())
    assert "unknown" in _text(results[0])
    assert results[1:] == [SUCCESS, SUCCESS]
    assert client.starts == 1
    assert client.calls == ["erp___query_erp"] * 3


def test_failed_restart_names_no_raw_exception_text():
    client = _FakeClient()
    client.alive = False
    client.dies_on_next_call = False

    def _boom():
        raise RuntimeError("handshake failed at https://internal.example.com/secret")

    client.start = _boom
    wrapped = _wrap(client)
    result = asyncio.run(wrapped.call_tool_async("t4", "erp___query_erp", {}))
    assert result["status"] == "error"
    assert "could not be reopened" in _text(result)
    assert "handshake" not in _text(result) and "internal.example.com" not in _text(result)
    assert client.calls == []


def test_a_second_close_inside_the_cooldown_is_not_restarted(monkeypatch):
    monkeypatch.setattr(orch, "_GATEWAY_SESSION_RESTART_COOLDOWN_S", 3600.0)
    client = _FakeClient()
    wrapped = _wrap(client)
    asyncio.run(wrapped.call_tool_async("c1", "erp___query_erp", {}))  # closes, restarted
    client.dies_on_next_call = True
    asyncio.run(wrapped.call_tool_async("c2", "erp___query_erp", {}))  # closes again
    refused = asyncio.run(wrapped.call_tool_async("c3", "erp___query_erp", {}))
    assert client.starts == 1  # bounded: no restart storm
    assert refused["status"] == "error" and "not reopened" in _text(refused)
    assert "was not sent" in _text(refused)
    assert client.calls == ["erp___query_erp"] * 2  # c3 never went out


def test_the_session_is_restarted_again_once_the_cooldown_has_passed(monkeypatch):
    monkeypatch.setattr(orch, "_GATEWAY_SESSION_RESTART_COOLDOWN_S", 0.0)
    client = _FakeClient()
    wrapped = _wrap(client)
    asyncio.run(wrapped.call_tool_async("c1", "erp___query_erp", {}))
    client.dies_on_next_call = True
    asyncio.run(wrapped.call_tool_async("c2", "erp___query_erp", {}))
    assert asyncio.run(wrapped.call_tool_async("c3", "erp___query_erp", {})) == SUCCESS
    assert client.starts == 2


def test_unrelated_exception_still_raises():
    class _Raiser(_FakeClient):
        async def call_tool_async(self, *a, **k):
            raise ValueError("bad arguments")

    wrapped = _wrap(_Raiser())
    with pytest.raises(ValueError):
        asyncio.run(wrapped.call_tool_async("t5", "erp___query_erp", {}))
