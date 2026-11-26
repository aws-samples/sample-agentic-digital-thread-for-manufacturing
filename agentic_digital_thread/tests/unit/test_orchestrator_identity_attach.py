# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Orchestrator-side identity-claim attachment (ADR-0049, Wave I2.1).

Offline pins for ``_wrap_gateway_client_with_identity_claim`` and its wiring in
``_get_orchestrator_tools``:

  * INTERACTIVE turns (forwarded user token — the SAME discriminator the bearer
    selection and the rebuild-seam gate use) attach a fresh, verifiable claim
    under the reserved arg on every tool call, without mutating the caller's
    argument dict;
  * SERVICE/background turns are BYTE-IDENTICAL to today — the wrapper is never
    applied, pinned with a golden-bytes regression fixture through the retry
    wrapper (the full service-path call stack);
  * fail-soft: no signing secret / undecodable token ⇒ client returned
    UNWRAPPED (claim-less turn, never an error);
  * ``_get_orchestrator_tools`` applies the wrapper ONLY when a user token is
    present (both directions pinned via the gateway-mode seam).
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

orch = pytest.importorskip("agents.orchestrator")
from agents.auth import identity_claim as ic  # noqa: E402


SECRET = "attach-test-key"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
SUCCESS = {"status": "success", "content": [{"text": "ok"}]}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_user_token(sub="user-1", groups=("ops",)) -> str:
    header = _b64url(json.dumps({"alg": "RS256"}).encode())
    body = _b64url(json.dumps({"sub": sub, "cognito:groups": list(groups)}).encode())
    return f"{header}.{body}.sig"


class FakeClient:
    """Minimal MCPClient stand-in recording call_tool_async invocations."""

    def __init__(self):
        self.calls = []
        # needed by _wrap_gateway_client_exclude_guidance (rebinds this attr)
        self.list_tools_sync = lambda *a, **k: []

        async def _call_tool_async(tool_use_id, name, arguments=None,
                                   read_timeout_seconds=None, meta=None):
            self.calls.append(
                {"tool_use_id": tool_use_id, "name": name, "arguments": arguments,
                 "read_timeout_seconds": read_timeout_seconds, "meta": meta}
            )
            return dict(SUCCESS)

        self.call_tool_async = _call_tool_async


@pytest.fixture(autouse=True)
def _fresh_secret_cache():
    orch.reset_identity_claim_secret_cache()
    yield
    orch.reset_identity_claim_secret_cache()


@pytest.fixture()
def signing_secret(monkeypatch):
    monkeypatch.setattr(orch, "_fetch_identity_claim_secret", lambda: SECRET)
    return SECRET


# --------------------------------------------------------------------------- #
# interactive attachment
# --------------------------------------------------------------------------- #
def test_interactive_wrapper_attaches_verifiable_claim(signing_secret):
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_identity_claim(client, _fake_user_token())
    caller_args = {"query": "SELECT 1"}
    result = asyncio.run(wrapped.call_tool_async("t1", "erp___run_query", caller_args))
    assert result["status"] == "success"
    sent = client.calls[0]["arguments"]
    assert ic.RESERVED_CLAIM_ARG in sent
    claims = ic.verify_claim(sent[ic.RESERVED_CLAIM_ARG], SECRET)
    assert claims["sub"] == "user-1"
    assert claims["groups"] == ["ops"]
    # other args pass through; the CALLER's dict is not mutated
    assert sent["query"] == "SELECT 1"
    assert caller_args == {"query": "SELECT 1"}


def test_interactive_wrapper_attaches_on_every_call(signing_secret):
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_identity_claim(client, _fake_user_token())
    asyncio.run(wrapped.call_tool_async("t1", "erp___run_query", {"a": 1}))
    asyncio.run(wrapped.call_tool_async("t2", "mes___run_query", None))
    for call in client.calls:
        assert ic.RESERVED_CLAIM_ARG in call["arguments"]
        ic.verify_claim(call["arguments"][ic.RESERVED_CLAIM_ARG], SECRET)


def test_wrapper_composes_with_retry_wrapper_and_retries_carry_claim(
    signing_secret, monkeypatch
):
    """Identity wrap is applied BEFORE the retry wrapper — retried attempts of a
    transient error still carry a (fresh) claim."""
    sleeps = []

    async def _fake_sleep(d, *a, **k):
        sleeps.append(d)

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
    client = orch._wrap_gateway_client_with_identity_claim(client, _fake_user_token())
    client = orch._wrap_gateway_client_with_retry(client)
    result = asyncio.run(client.call_tool_async("t1", "erp___run_query", {"q": 1}))
    assert result["status"] == "success"
    assert len(attempts) == 2
    for sent in attempts:
        ic.verify_claim(sent[ic.RESERVED_CLAIM_ARG], SECRET)


# --------------------------------------------------------------------------- #
# fail-soft: no secret / bad token ⇒ UNWRAPPED client
# --------------------------------------------------------------------------- #
def test_no_signing_secret_returns_client_unwrapped(monkeypatch):
    monkeypatch.setattr(orch, "_fetch_identity_claim_secret", lambda: None)
    client = FakeClient()
    original = client.call_tool_async
    assert orch._wrap_gateway_client_with_identity_claim(client, _fake_user_token()) is client
    assert client.call_tool_async is original


def test_undecodable_user_token_returns_client_unwrapped(signing_secret):
    client = FakeClient()
    original = client.call_tool_async
    orch._wrap_gateway_client_with_identity_claim(client, "not-a-jwt")
    assert client.call_tool_async is original


def test_env_unset_fetch_returns_none(monkeypatch):
    monkeypatch.delenv(orch.IDENTITY_CLAIM_SECRET_ARN_ENV, raising=False)
    assert orch._fetch_identity_claim_secret() is None


# --------------------------------------------------------------------------- #
# SERVICE turns: byte-identical payload (golden-bytes regression fixture)
# --------------------------------------------------------------------------- #
# Today's service path: the client is wrapped ONLY by the retry wrapper (and the
# guidance list-filter, which does not touch call payloads). If any future change
# makes service tool-call arguments differ from what the model produced, this
# golden fixture fails.
SERVICE_FIXTURE_ARGS = {"query": "SELECT count(*) FROM orders", "limit": 5}
SERVICE_FIXTURE_GOLDEN = b'{"limit": 5, "query": "SELECT count(*) FROM orders"}'


def test_service_turn_payload_byte_identical_regression_fixture():
    client = FakeClient()
    wrapped = orch._wrap_gateway_client_with_retry(client)  # service path: no identity wrap
    asyncio.run(
        wrapped.call_tool_async("t1", "erp___run_query", dict(SERVICE_FIXTURE_ARGS))
    )
    sent = client.calls[0]["arguments"]
    assert json.dumps(sent, sort_keys=True).encode() == SERVICE_FIXTURE_GOLDEN
    assert ic.RESERVED_CLAIM_ARG not in sent


def test_get_orchestrator_tools_wires_identity_only_for_interactive(monkeypatch):
    """The seam gate: _get_orchestrator_tools applies the identity wrapper IFF a
    forwarded user token is present (the existing interactive-vs-service
    discriminator, same one that gates the rebuild seam)."""
    wrapped_with = []

    class _Cfg:
        has_gateway = True
        agentcore_gateway_url = "https://gw.example/mcp"

    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: FakeClient())
    monkeypatch.setattr(
        orch,
        "_wrap_gateway_client_with_identity_claim",
        lambda client, token: wrapped_with.append(token) or client,
    )

    orch._get_orchestrator_tools(user_access_token=None)
    assert wrapped_with == []  # service turn: identity wrapper NEVER applied

    orch._get_orchestrator_tools(user_access_token="user-token")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    assert wrapped_with == ["user-token"]  # interactive turn: applied exactly once


# --------------------------------------------------------------------------- #
# ADR-0079: DELEGATED turns (verified Quick-gateway subject, no user token)
# --------------------------------------------------------------------------- #
class _Cfg:
    has_gateway = True
    agentcore_gateway_url = "https://gw.example/mcp"


@pytest.fixture()
def gateway_seams(monkeypatch, signing_secret):
    """Real wrappers; stubbed bearer + client + retry capture."""
    seen = {"bearer_token_arg": [], "rebuild": [], "clients": []}

    def _bearer(cfg, tok):
        seen["bearer_token_arg"].append(tok)
        return "service-bearer"

    def _client(url, bearer_token=None):
        client = FakeClient()
        seen["clients"].append((client, bearer_token))
        return client

    real_retry = orch._wrap_gateway_client_with_retry

    def _retry(client, rebuild_client=None, **kwargs):
        seen["rebuild"].append(rebuild_client)
        return real_retry(client, rebuild_client=rebuild_client, **kwargs)

    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", _bearer)
    monkeypatch.setattr(orch, "_create_mcp_client", _client)
    monkeypatch.setattr(orch, "_wrap_gateway_client_with_retry", _retry)
    return seen


def test_delegated_turn_mints_a_fresh_claim_per_call_with_the_service_bearer(gateway_seams):
    client = orch._build_gateway_client(_Cfg(), None, "sess-1",
                                        delegated_subject="delegated-sub")
    asyncio.run(client.call_tool_async("t1", "erp___run_query", {"q": 1}))
    asyncio.run(client.call_tool_async("t2", "mes___run_query", None))
    fake, bearer = gateway_seams["clients"][0]
    assert bearer == "service-bearer"
    assert gateway_seams["bearer_token_arg"] == [None]  # the SERVICE token path
    for call in fake.calls:
        claims = ic.verify_claim(call["arguments"][ic.RESERVED_CLAIM_ARG], SECRET)
        assert claims["sub"] == "delegated-sub"
        assert claims["groups"] == []
    assert gateway_seams["rebuild"] == [None]  # a rebuild can never drop the claim


def test_delegated_turn_carries_provenance_context(gateway_seams):
    from agents.provenance import RESERVED_PROVENANCE_ARG
    client = orch._build_gateway_client(_Cfg(), None, "sess-1",
                                        delegated_subject="delegated-sub")
    asyncio.run(client.call_tool_async("t1", "erp___run_query", {"q": 1}))
    fake, _ = gateway_seams["clients"][0]
    assert RESERVED_PROVENANCE_ARG in fake.calls[0]["arguments"]


def test_service_turn_stays_byte_identical_and_rebuildable(gateway_seams):
    client = orch._build_gateway_client(_Cfg(), None, "sess-1")
    asyncio.run(client.call_tool_async("t1", "erp___run_query", dict(SERVICE_FIXTURE_ARGS)))
    fake, _ = gateway_seams["clients"][0]
    sent = fake.calls[0]["arguments"]
    assert json.dumps(sent, sort_keys=True).encode() == SERVICE_FIXTURE_GOLDEN
    assert callable(gateway_seams["rebuild"][0])


def test_user_token_wins_over_a_delegated_subject(gateway_seams):
    client = orch._build_gateway_client(_Cfg(), _fake_user_token(sub="user-1"), "s",
                                        delegated_subject="delegated-sub")
    asyncio.run(client.call_tool_async("t1", "erp___run_query", {}))
    fake, _ = gateway_seams["clients"][0]
    claims = ic.verify_claim(fake.calls[0]["arguments"][ic.RESERVED_CLAIM_ARG], SECRET)
    assert claims["sub"] == "user-1"


def test_delegated_wrapper_fails_soft_without_a_secret(monkeypatch):
    monkeypatch.setattr(orch, "_fetch_identity_claim_secret", lambda: None)
    client = FakeClient()
    original = client.call_tool_async
    assert orch._wrap_gateway_client_with_delegated_claim(client, "sub") is client
    assert client.call_tool_async is original


def test_build_orchestrator_threads_the_delegated_subject(monkeypatch):
    seen = {}

    def _tools(user_access_token=None, session_id="", delegated_subject=None):
        seen["delegated_subject"] = delegated_subject
        return []

    monkeypatch.setattr(orch, "_get_orchestrator_tools", _tools)
    monkeypatch.setattr(orch, "get_config", lambda: type("C", (), {"has_memory": False})())
    monkeypatch.setattr(orch, "_make_conversation_manager", lambda: None)
    monkeypatch.setattr(orch, "_base_agent_kwargs", lambda tools, cm: {})
    monkeypatch.setattr(orch, "Agent", lambda **kw: object())
    orch.build_orchestrator(session_id="s", user_id="u", delegated_subject="d-sub")
    assert seen == {"delegated_subject": "d-sub"}
