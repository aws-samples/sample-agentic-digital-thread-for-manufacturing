# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The orchestrator's gateway client recovers from a closed MCP session within
the turn, without sending again a call that may have taken effect.

Live symptom (workshop test event, 2026-10-14, an interactive
module 6 turn): the first ERP call failed with "Tool execution failed:
Connection to the MCP server was closed", and the next three with "Error: the
client session is not running ...". Both texts are strands-agents 1.42.0
``MCPClient`` text, from the orchestrator's ONE per-turn gateway client: any
non-2xx answer to a POST (or a transport error) ends the streamable-HTTP
session, the call in flight returns the first text, and every later call on
that client RAISES ``MCPClientInitializationError`` with the second (the
executor prefixes "Error: "). ``_wrap_gateway_client_with_retry`` matched
neither, so the turn kept reusing the dead client until it ended.

The rules pinned here (the system-agent keeper's rules, applied at the
orchestrator-to-gateway hop):

* the session counts as closed only by the client's own state, never by text;
* a call the client refused because the session was already closed never
  reached the gateway: the session is reopened in place and the call is sent;
* a call whose session closed under it may have acted: the session is
  reopened, the call is NOT sent again (by the reopen or by the transient
  retry loop), and the model gets a retryable "outcome unknown" error;
* a 401 or 403 inside why the session closed: an error naming the status, no
  reopen, nothing sent again;
* one reopen at a time, and the reopened session still ends with the turn's
  tool registry (no leaked session thread);
* identified turns (no auth rebuild seam) recover too, keeping their
  identity claim, because the same client instance is restarted in place.

The client is the real strands ``MCPClient`` over the real MCP streamable-HTTP
transport, answered by the in-process fake server (``_fake_mcp_hub``) through
``httpx.MockTransport``: no socket opens.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

orch = pytest.importorskip("agents.orchestrator")

from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from strands.tools.mcp import MCPClient  # noqa: E402

from tests.unit import _fake_mcp_hub as fh  # noqa: E402

URL = "https://gw.example.com/mcp"
ERP = "erp___query_erp"
SAVE = "digital-thread___save_correlation"
CLOSED = "connection to the mcp server was closed"
NOT_RUNNING = "client session is not running"


class _Gateway(fh.FakeHub):
    """The fake server, also recording each ``tools/call``'s argument names."""

    def __init__(self):
        super().__init__(accept="bearer", config_tools=False)
        self.argument_names: list[set] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            message = json.loads(request.content or b"{}")
            if message.get("method") == "tools/call" and self.listed:
                args = (message.get("params") or {}).get("arguments") or {}
                self.argument_names.append(set(args))
        return super().handle(request)


def _fake_user_jwt() -> str:
    """An unsigned JWT-shaped token with a subject (decoded, never verified)."""
    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg({'sub': 'user-1', 'cognito:groups': []})}.sig"


@pytest.fixture
def gateway(monkeypatch):
    return _Gateway()


@pytest.fixture
def build(monkeypatch, gateway):
    """Build the turn's gateway client the way ``_get_orchestrator_tools`` does,
    over the fake server, load its tools as the Agent's registry does, and tear
    it down as the registry does at the end of the turn."""
    built: list = []

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                 transport=httpx.MockTransport(gateway.handle))

    def create(url, use_sigv4=False, bearer_token=None):
        headers = {"Authorization": f"Bearer {fh.KEY}"}
        client = MCPClient(lambda: streamablehttp_client(
            url, headers=headers, httpx_client_factory=factory))
        built.append(client)
        return client

    monkeypatch.setattr(orch, "_create_mcp_client", create)
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda config, tok: fh.KEY)
    # These tests reopen more than once per test; the reopen cooldown is pinned
    # in test_orchestrator_gateway_session_restart.py.
    monkeypatch.setattr(orch, "_GATEWAY_SESSION_RESTART_COOLDOWN_S", 0.0)
    monkeypatch.setattr(orch, "_fetch_identity_claim_secret",
                        lambda: "test-only-claim-signing-key-0123456789")  # nosec B105
    config = SimpleNamespace(agentcore_gateway_url=URL, aws_region="us-east-1")

    def _build(user_access_token=None, session_id=""):
        client = orch._build_gateway_client(config, user_access_token, session_id)
        asyncio.run(client.load_tools())
        client.add_consumer("agent-registry")
        return client

    yield _build
    for client in built:
        try:
            client.stop(None, None, None)
        except RuntimeError:
            pass  # stop() re-raises why a session closed, after cleaning up


def _call(client, tool_use_id: str, name: str = ERP) -> dict:
    """The call as ``MCPAgentTool.stream`` makes it (keyword arguments)."""
    return asyncio.run(client.call_tool_async(
        tool_use_id=tool_use_id, name=name, arguments={"question": "q"}))


def _text(result: dict) -> str:
    return " ".join(b.get("text", "") for b in result.get("content") or []).lower()


TURNS = {
    "service turn": {},
    "interactive turn": {"user_access_token": _fake_user_jwt(), "session_id": "sess-1"},
}


@pytest.mark.parametrize("turn", list(TURNS))
def test_later_calls_recover_after_the_session_closes_under_a_call(build, gateway, turn):
    """The live shape: one call loses its session, the next calls must work."""
    client = build(**TURNS[turn])
    assert _call(client, "t1")["status"] == "success"
    gateway.drop_after_call = 502  # the gateway acts, then its answer is lost
    lost = _call(client, "t2")
    gateway.drop_after_call = None
    after = [_call(client, f"t{n}") for n in (3, 4, 5)]
    assert [r["status"] for r in after] == ["success"] * 3, after
    assert not any(NOT_RUNNING in _text(r) for r in after)
    assert gateway.calls == [ERP] * 5  # the lost call was sent once, never again
    assert lost["status"] == "error"
    assert "unknown" in _text(lost) and CLOSED not in _text(lost), lost


def test_a_call_lost_with_its_session_is_not_sent_again(build, gateway):
    """Never replay a call that may have acted: a write here."""
    client = build()
    gateway.drop_after_call = 502
    result = _call(client, "w1", name=SAVE)
    gateway.drop_after_call = None
    assert result["status"] == "error" and "unknown" in _text(result), result
    assert gateway.calls == [SAVE]  # sent once, by the model's own call
    assert client._is_session_active()  # the session is open again for the next call


def test_calls_refused_by_a_closed_session_are_sent_after_one_reopen(build, gateway):
    """The gateway was down (the session closed and could not be reopened);
    once it is back, calls the client refuses before sending anything are sent
    after ONE reopen, however many arrive together."""
    client = build()
    gateway.down_status = 503
    down = _call(client, "d1")
    assert down["status"] == "error" and "503" in _text(down), down
    gateway.down_status = None
    sessions = len(gateway.sessions)

    async def three():
        return await asyncio.gather(*(client.call_tool_async(
            tool_use_id=f"c{n}", name=ERP, arguments={"question": "q"}) for n in range(3)))

    results = asyncio.run(three())
    assert [r["status"] for r in results] == ["success"] * 3, results
    assert gateway.calls == [ERP] * 3
    assert len(gateway.sessions) == sessions + 1  # exactly one reopen


def test_a_401_is_named_without_a_reopen_or_a_resend(build, gateway):
    client = build()
    sessions = len(gateway.sessions)
    gateway.accept = "nobody"  # the gateway stops taking this bearer
    result = _call(client, "a1")
    assert result["status"] == "error"
    assert "401" in _text(result), result
    assert len(gateway.sessions) == sessions  # no initialize was accepted
    assert gateway.calls == []


def test_server_error_text_on_an_open_session_is_returned_unchanged(build, gateway):
    """Text never counts as a closed session: the result is the server's own."""
    client = build()
    gateway.error_text = "Connection to the MCP server was closed"
    result = _call(client, "e1")
    assert result["status"] == "error" and CLOSED in _text(result)
    assert gateway.calls == [ERP]  # not called again
    assert len(gateway.sessions) == 1  # the load's session, never reopened


def test_the_reopened_session_still_ends_with_the_tool_registry(build, gateway):
    """The reopen restarts the same client in place; the registry's teardown
    (remove_consumer -> stop) must still stop the reopened session."""
    client = build()
    gateway.drop_after_call = 502
    _call(client, "r1")
    gateway.drop_after_call = None
    assert _call(client, "r2")["status"] == "success"
    client.remove_consumer("agent-registry")
    assert not client._is_session_active()
    assert client._background_thread is None  # no session thread left running


def test_an_interactive_turn_keeps_its_identity_claim_on_the_call_after_a_reopen(
        build, gateway):
    """Identified turns have no auth rebuild seam (a new client would drop the
    claim); restarting the same client keeps every wrapper, so the call sent
    after the reopen still carries the claim and the provenance context."""
    client = build(**TURNS["interactive turn"])
    assert _call(client, "i1")["status"] == "success"
    gateway.down_status = 503
    _call(client, "i2")  # closes the session; the reopen fails while it is down
    gateway.down_status = None
    assert _call(client, "i3")["status"] == "success"
    from agents.auth.identity_claim import RESERVED_CLAIM_ARG
    from agents.provenance import RESERVED_PROVENANCE_ARG
    first, last = gateway.argument_names[0], gateway.argument_names[-1]
    assert {RESERVED_CLAIM_ARG, RESERVED_PROVENANCE_ARG} <= first
    assert last == first


# --------------------------------------------------------------------------- #
# A lost call never reaches the transient retry loop, whatever text the reopen
# failure carries (scripted client: its reopen fails with a timeout-named
# error, which the transient classifier would otherwise retry).
# --------------------------------------------------------------------------- #
class _DyingClient:
    """Loses its session under the first call; its reopen raises ``error``."""

    def __init__(self, error: BaseException):
        self.error = error
        self.active = True
        self.sent: list = []

    def _is_session_active(self) -> bool:
        return self.active

    async def call_tool_async(self, tool_use_id=None, name=None, arguments=None, *a, **k):
        if not self.active:
            from strands.types.exceptions import MCPClientInitializationError
            raise MCPClientInitializationError("the client session is not running")
        self.sent.append(name)
        self.active = False
        return {"status": "error", "toolUseId": tool_use_id,
                "content": [{"text": "Tool execution failed: Connection to the MCP server was closed"}]}

    def stop(self, *a):
        pass

    def start(self):
        raise self.error


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("connect timed out"),
                                   TimeoutError("timed out")])
def test_a_lost_call_is_never_retried_when_the_reopen_fails(monkeypatch, error):
    async def no_sleep(delay, *a, **k):
        raise AssertionError(f"the transient retry loop ran (backoff {delay})")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    client = _DyingClient(error)
    orch._wrap_gateway_client_with_retry(client)
    result = asyncio.run(client.call_tool_async(tool_use_id="x", name=SAVE))
    assert result["status"] == "error"
    assert client.sent == [SAVE]  # sent once; never again
