# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Orchestrator-side F1 provenance-context attachment (Wave 5, frozen F1).

Offline pins for ``_wrap_gateway_client_with_provenance_context`` and its
wiring in ``_build_gateway_client``/``_get_orchestrator_tools``:

  * INTERACTIVE turns (forwarded user token + non-empty session id) merge the
    reserved ``_adt_provenance`` arg whose ``query_ref`` is the EXACT existing
    D3 derivation ``mint_query_ref(session_id, tool_use_id)`` — the join pin's
    orchestrator half — without mutating the caller's argument dict;
  * SERVICE/background turns stay BYTE-IDENTICAL to today (golden-bytes
    fixture through the retry wrapper, mirroring the identity pin);
  * fail-soft: empty session id ⇒ client returned UNWRAPPED; empty
    tool_use_id ⇒ nothing attached, never an error;
  * the provenance context and the identity claim are SEPARATE args (never
    merged into one another — F1 rationale);
  * retries re-attach the same deterministic context (applied BEFORE the
    retry wrapper).
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

orch = pytest.importorskip("agents.orchestrator")
from agents.auth import identity_claim as ic  # noqa: E402
from agents.provenance import (  # noqa: E402
    RESERVED_PROVENANCE_ARG,
    mint_query_ref,
    parse_provenance_context,
)

SID = "sess-w5.test"
SUCCESS = {"status": "success", "content": [{"text": "ok"}]}


class FakeClient:
    """Minimal MCPClient stand-in recording call_tool_async invocations."""

    def __init__(self):
        self.calls = []
        self.list_tools_sync = lambda *a, **k: []

        async def _call_tool_async(tool_use_id, name, arguments=None,
                                   read_timeout_seconds=None, meta=None):
            self.calls.append(
                {"tool_use_id": tool_use_id, "name": name, "arguments": arguments,
                 "read_timeout_seconds": read_timeout_seconds, "meta": meta}
            )
            return dict(SUCCESS)

        self.call_tool_async = _call_tool_async


# --------------------------------------------------------------------------- #
# interactive attachment — the join pin (orchestrator half)
# --------------------------------------------------------------------------- #
def test_wrapper_attaches_d3_ref_for_tool_use_id():
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_provenance_context(client, SID)
    caller_args = {"question": "open orders?"}
    result = asyncio.run(
        wrapped.call_tool_async("tooluse_X", "erp___query_erp", caller_args))
    assert result["status"] == "success"
    sent = client.calls[0]["arguments"]
    assert RESERVED_PROVENANCE_ARG in sent
    parsed = parse_provenance_context(sent[RESERVED_PROVENANCE_ARG])
    assert parsed == (SID, mint_query_ref(SID, "tooluse_X"))
    # other args pass through; the CALLER's dict is not mutated
    assert sent["question"] == "open orders?"
    assert caller_args == {"question": "open orders?"}


def test_wrapper_ref_differs_per_tool_use_id_same_session():
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_provenance_context(client, SID)
    asyncio.run(wrapped.call_tool_async("t1", "erp___query_erp", {}))
    asyncio.run(wrapped.call_tool_async("t2", "mes___query_mes", {}))
    refs = [c["arguments"][RESERVED_PROVENANCE_ARG]["query_ref"]
            for c in client.calls]
    assert refs == [mint_query_ref(SID, "t1"), mint_query_ref(SID, "t2")]
    assert refs[0] != refs[1]


# --------------------------------------------------------------------------- #
# fail-soft
# --------------------------------------------------------------------------- #
def test_empty_session_returns_client_unwrapped():
    client = FakeClient()
    original = client.call_tool_async
    assert orch._wrap_gateway_client_with_provenance_context(client, "") is client
    assert client.call_tool_async is original


def test_empty_tool_use_id_attaches_nothing_and_never_raises():
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_provenance_context(client, SID)
    asyncio.run(wrapped.call_tool_async("", "erp___query_erp", {"q": 1}))
    asyncio.run(wrapped.call_tool_async(None, "erp___query_erp", None))
    for call in client.calls:
        args = call["arguments"] or {}
        assert RESERVED_PROVENANCE_ARG not in args


# --------------------------------------------------------------------------- #
# SERVICE turns: byte-identical payload (mirror of the identity golden pin)
# --------------------------------------------------------------------------- #
SERVICE_FIXTURE_ARGS = {"query": "SELECT count(*) FROM orders", "limit": 5}
SERVICE_FIXTURE_GOLDEN = b'{"limit": 5, "query": "SELECT count(*) FROM orders"}'


def test_service_turn_payload_byte_identical_with_wave5_in_tree():
    """Service path (retry wrapper only — neither identity NOR provenance
    wrapper applied): the tool-call payload must stay byte-identical."""
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_retry(client)
    asyncio.run(
        wrapped.call_tool_async("t1", "erp___run_query", dict(SERVICE_FIXTURE_ARGS)))
    sent = client.calls[0]["arguments"]
    assert json.dumps(sent, sort_keys=True).encode() == SERVICE_FIXTURE_GOLDEN
    assert RESERVED_PROVENANCE_ARG not in sent
    assert ic.RESERVED_CLAIM_ARG not in sent


# --------------------------------------------------------------------------- #
# composition: separate args; retries re-carry the context
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_user_token(sub="user-1", groups=("ops",)) -> str:
    header = _b64url(json.dumps({"alg": "RS256"}).encode())
    body = _b64url(json.dumps({"sub": sub, "cognito:groups": list(groups)}).encode())
    return f"{header}.{body}.sig"


def test_identity_and_provenance_are_separate_args(monkeypatch):
    monkeypatch.setattr(orch, "_fetch_identity_claim_secret", lambda: "k")
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_identity_claim(client, _fake_user_token())
    wrapped = orch._wrap_gateway_client_with_provenance_context(wrapped, SID)
    asyncio.run(wrapped.call_tool_async("t1", "erp___query_erp", {"q": 1}))
    sent = client.calls[0]["arguments"]
    assert RESERVED_PROVENANCE_ARG in sent
    assert ic.RESERVED_CLAIM_ARG in sent
    # neither value embeds the other's reserved key
    assert RESERVED_PROVENANCE_ARG not in str(sent[ic.RESERVED_CLAIM_ARG])
    # session_id/query_ref always; write_token is the additive Wave-7 write
    # capability (server-only; stripped at the ASGI seam before any tool sees it).
    prov_keys = set(sent[RESERVED_PROVENANCE_ARG].keys())
    assert {"session_id", "query_ref"} <= prov_keys
    assert prov_keys <= {"session_id", "query_ref", "write_token"}


def test_retries_recarry_the_same_deterministic_context(monkeypatch):
    async def _fake_sleep(d, *a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    transient = {"status": "error", "content": [{"text": "endpoint not found"}]}
    attempts = []

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()

            async def _call(tool_use_id, name, arguments=None,
                            read_timeout_seconds=None, meta=None):
                attempts.append(arguments)
                return dict(transient) if len(attempts) == 1 else dict(SUCCESS)

            self.call_tool_async = _call

    client = FlakyClient()
    client = orch._wrap_gateway_client_with_provenance_context(client, SID)
    client = orch._wrap_gateway_client_with_retry(client)
    result = asyncio.run(client.call_tool_async("t1", "erp___query_erp", {"q": 1}))
    assert result["status"] == "success"
    assert len(attempts) == 2
    expected = mint_query_ref(SID, "t1")
    for sent in attempts:
        assert sent[RESERVED_PROVENANCE_ARG]["query_ref"] == expected


# --------------------------------------------------------------------------- #
# seam gate: _build_gateway_client wires the wrapper interactive-only
# --------------------------------------------------------------------------- #
def test_build_gateway_client_wires_provenance_only_for_interactive(monkeypatch):
    wrapped_with = []

    class _Cfg:
        has_gateway = True
        agentcore_gateway_url = "https://gw.example/mcp"

    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")
    monkeypatch.setattr(orch, "_create_mcp_client",
                        lambda url, bearer_token=None: FakeClient())
    monkeypatch.setattr(orch, "_wrap_gateway_client_with_identity_claim",
                        lambda client, token: client)
    monkeypatch.setattr(
        orch,
        "_wrap_gateway_client_with_provenance_context",
        lambda client, sid: wrapped_with.append(sid) or client,
    )

    orch._get_orchestrator_tools(user_access_token=None, session_id=SID)
    assert wrapped_with == []  # service turn: NEVER applied

    orch._get_orchestrator_tools(user_access_token="user-token", session_id=SID)  # nosec B106 - offline test fixture
    assert wrapped_with == [SID]  # interactive turn: applied exactly once
