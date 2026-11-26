# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""P5 (ADR-0033): orchestrator gateway bearer-JWT token selection.

The query gateway flipped AWS_IAM -> CUSTOM_JWT, so the orchestrator reaches it
with an adt-app bearer token instead of SigV4. Token selection is DETERMINISTIC
with NO SigV4 fallback:
  1. forwarded end-user adt-app token  -> use it.
  2. else adt-app-service client-credentials token -> use it.
  3. else (neither, cloud mode)        -> raise a clear error.

These tests exercise _resolve_gateway_bearer directly (the service-token network
fetch is mocked) plus the _create_mcp_client bearer wiring.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents import orchestrator


def _fake_config():
    # Minimal shape _resolve_gateway_bearer / _fetch_service_token read.
    return SimpleNamespace(aws_region="us-east-1", cognito_user_pool_id="us-east-1_pool")


# ── 1. user path: forwarded end-user token wins, no service fetch ─────────────

def test_bearer_uses_forwarded_user_token():
    with patch.object(orchestrator, "_fetch_service_token") as fetch:
        tok = orchestrator._resolve_gateway_bearer(_fake_config(), "user-jwt-abc")
    assert tok == "user-jwt-abc"
    fetch.assert_not_called()  # user token short-circuits — no service mint


# ── 2. service path: no user token -> mint a adt-app-service token ─────────────

def test_bearer_falls_to_service_token_when_no_user_token():
    with patch.object(orchestrator, "_fetch_service_token", return_value="svc-jwt-xyz") as fetch:
        tok = orchestrator._resolve_gateway_bearer(_fake_config(), None)
    assert tok == "svc-jwt-xyz"
    fetch.assert_called_once()


# ── 3. error path: neither available -> hard error, NO SigV4 fallback ─────────

def test_bearer_raises_when_neither_token_available():
    with patch.object(orchestrator, "_fetch_service_token", return_value=None):
        with pytest.raises(RuntimeError) as ei:
            orchestrator._resolve_gateway_bearer(_fake_config(), None)
    msg = str(ei.value)
    assert "CUSTOM_JWT" in msg
    assert "SigV4" in msg  # explicitly documents the no-fallback contract


# ── _fetch_service_token returns None when the service client is unconfigured ─

def test_fetch_service_token_none_when_client_id_unset(monkeypatch):
    monkeypatch.delenv(orchestrator.SERVICE_CLIENT_ID_ENV, raising=False)
    # No env client id + a config pool id present -> still None (client id required).
    assert orchestrator._fetch_service_token(_fake_config()) is None


def test_fetch_service_token_none_when_no_pool(monkeypatch):
    monkeypatch.setenv(orchestrator.SERVICE_CLIENT_ID_ENV, "svc-client")
    monkeypatch.delenv(orchestrator.SERVICE_POOL_ID_ENV, raising=False)
    cfg = SimpleNamespace(aws_region="us-east-1", cognito_user_pool_id=None)
    assert orchestrator._fetch_service_token(cfg) is None


# ── _create_mcp_client bearer wiring: sends Authorization: Bearer ─────────────
# The bearer transport is built by the shared agents.gateway_auth.bearer_mcp_client
# helper (DRY seam), so the streamable-HTTP spy is installed on that module.

def test_create_mcp_client_bearer_sets_authorization_header():
    import agents.gateway_auth as ga

    captured = {}

    def _fake_streamablehttp_client(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return object()

    with patch.object(ga, "streamablehttp_client", _fake_streamablehttp_client):
        client = orchestrator._create_mcp_client("https://gw.example.com/mcp", bearer_token="tok-123")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        # MCPClient stores the transport factory; invoke it to trigger our fake.
        client._transport_callable()

    assert captured["url"] == "https://gw.example.com/mcp"
    assert captured["headers"] == {"Authorization": "Bearer tok-123"}


def test_create_mcp_client_bearer_takes_precedence_over_sigv4():
    """bearer_token must win even if use_sigv4 is also set (no SigV4 import)."""
    import agents.gateway_auth as ga

    def _fake_streamablehttp_client(url, headers=None):
        return object()

    with patch.object(ga, "streamablehttp_client", _fake_streamablehttp_client):
        # If use_sigv4 were honored it would try to import mcp_proxy_for_aws;
        # bearer precedence means it never does.
        client = orchestrator._create_mcp_client(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
            "https://gw.example.com/mcp", use_sigv4=True, bearer_token="tok-xyz"
        )
        client._transport_callable()


# ── get_orchestrator() singleton is LOCAL-ONLY: fails fast under has_gateway ──

def test_get_orchestrator_raises_when_has_gateway(monkeypatch):
    """The process-wide singleton must never run the cloud path (it would bake one
    expiring adt-app-service bearer token process-wide). It fails fast instead."""
    cloud_cfg = SimpleNamespace(has_gateway=True)
    monkeypatch.setattr(orchestrator, "get_config", lambda: cloud_cfg)
    with pytest.raises(RuntimeError) as ei:
        orchestrator.get_orchestrator()
    msg = str(ei.value)
    assert "build_orchestrator" in msg  # points cloud callers at the per-request path
    assert "singleton" in msg.lower()


def test_get_orchestrator_builds_in_local_mode(monkeypatch):
    """Local mode (has_gateway False) still returns a cached singleton Agent."""
    local_cfg = SimpleNamespace(has_gateway=False)
    monkeypatch.setattr(orchestrator, "get_config", lambda: local_cfg)
    monkeypatch.setattr(orchestrator, "_orchestrator", None)
    sentinel = object()
    monkeypatch.setattr(orchestrator, "Agent", lambda **kwargs: sentinel)
    monkeypatch.setattr(orchestrator, "_get_orchestrator_tools", lambda: [])
    monkeypatch.setattr(orchestrator, "_make_conversation_manager", lambda: None)
    assert orchestrator.get_orchestrator() is sentinel


# ── service-token cache: mint runs ONCE across many calls (throttling) ────────

def test_service_token_cached_across_calls(monkeypatch):
    """The adt-app-service token is process-cached: describe_user_pool_client + STS +
    token-mint run ONCE, then subsequent calls reuse the cached token until expiry."""
    orchestrator.reset_service_token_cache()
    monkeypatch.setenv(orchestrator.SERVICE_CLIENT_ID_ENV, "svc-client")
    cfg = SimpleNamespace(aws_region="us-east-1", cognito_user_pool_id="us-east-1_pool")

    secret_calls = {"n": 0}
    account_calls = {"n": 0}
    mint_calls = {"n": 0}

    def _fake_secret(config, pool_id, client_id):
        secret_calls["n"] += 1
        return "shh-secret"

    def _fake_account(config):
        account_calls["n"] += 1
        return "111122223333"

    def _fake_mint(*, token_url, client_id, client_secret, scope, timeout=10):
        mint_calls["n"] += 1
        return ("svc-token-abc", 3600)

    monkeypatch.setattr(orchestrator, "_service_client_secret_for", _fake_secret)
    monkeypatch.setattr(orchestrator, "_service_domain_account", _fake_account)
    monkeypatch.setattr(orchestrator, "mint_client_credentials_token", _fake_mint)

    # Five calls, but the expensive control-plane work happens exactly once.
    tokens = [orchestrator._fetch_service_token(cfg) for _ in range(5)]
    assert tokens == ["svc-token-abc"] * 5
    assert mint_calls["n"] == 1
    assert secret_calls["n"] == 1
    assert account_calls["n"] == 1

    orchestrator.reset_service_token_cache()


def test_service_token_refreshes_when_expired(monkeypatch):
    """Once the cached token is within the refresh margin of expiry, the next call
    re-mints (a fresh token, one more mint call)."""
    orchestrator.reset_service_token_cache()
    monkeypatch.setenv(orchestrator.SERVICE_CLIENT_ID_ENV, "svc-client")
    cfg = SimpleNamespace(aws_region="us-east-1", cognito_user_pool_id="us-east-1_pool")

    monkeypatch.setattr(orchestrator, "_service_client_secret_for", lambda *a, **k: "s")
    monkeypatch.setattr(orchestrator, "_service_domain_account", lambda c: "111122223333")

    minted = {"n": 0}

    def _fake_mint(**kwargs):
        minted["n"] += 1
        # expires_in=0 forces the next call to see the token as expired.
        return (f"tok-{minted['n']}", 0)

    monkeypatch.setattr(orchestrator, "mint_client_credentials_token", _fake_mint)

    first = orchestrator._fetch_service_token(cfg)
    second = orchestrator._fetch_service_token(cfg)
    assert first == "tok-1"
    assert second == "tok-2"  # re-minted because expires_in=0 is inside the margin
    assert minted["n"] == 2

    orchestrator.reset_service_token_cache()
