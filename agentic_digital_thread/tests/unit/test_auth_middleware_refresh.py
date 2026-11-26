# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AuthMiddleware proactive near-expiry refresh + refreshed-cookie write-back.

The signed-in user's adt-app access token is forwarded as the AgentCore
gateway bearer (ADR-0049), so a token that validates at the origin with only
seconds left 403s at the gateway MID-TURN (observed 2026-09-24: a ~30 s
"Query failed." window every 60 min at the token boundary). This suite pins
the fix:

  * exp far away                      -> no refresh, no Set-Cookie;
  * exp within REFRESH_LEEWAY + refresh cookie
                                      -> refreshed ONCE, request.state.session
                                         carries the new access_token, and the
                                         response sets the adt_session cookie;
  * within leeway but refresh RAISES  -> the still-valid token serves the
                                         request (200, original token, no
                                         Set-Cookie) — near-expiry refresh
                                         failure never fails the request;
  * expired + refresh cookie          -> refreshed (existing behaviour) AND
                                         the refreshed session is written back
                                         to the response cookie;
  * expired + refresh RAISES          -> 401 (the expired path stays closed).

Offline: CognitoAuth is stubbed (no network); tokens are locally-encoded JWTs
and the stub returns their exp/iat on the validated user, as CognitoAuth does.
"""

import json
import base64
import time
import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import jwt

import agents.auth.middleware as middleware_mod
from agents.auth.cognito import (
    AuthenticationError, TokenExpiredError, TokenResponse, UserInfo,
)
from agents.auth.middleware import (
    SESSION_COOKIE_NAME, _REFRESH_COOKIE_NAME, AuthMiddleware, _encode_cookie,
)


def _make_token(seconds_from_now: float) -> str:
    return jwt.encode(
        {"exp": time.time() + seconds_from_now, "sub": "u-1"},
        "unit-test-key", algorithm="HS256")


NEW_ACCESS = _make_token(3600)


class _StubAuth:
    """CognitoAuth stand-in: exp-driven validation + recorded refreshes."""

    refresh_calls: list
    refresh_error: Exception | None = None

    def __init__(self, *a, **k):
        pass

    def validate_token(self, token, id_token=None, **_kwargs):
        # Stands in for CognitoAuth's signature check; the claims it returns are trusted.
        claims = jwt.decode(token, "unit-test-key", algorithms=["HS256"],
                            options={"verify_exp": False})
        exp = claims.get("exp")
        if exp is not None and exp <= time.time():
            raise TokenExpiredError("Token expired")
        return UserInfo(user_id="u-1", email="u@example.com", username="u-1",
                        expires_at=exp, issued_at=claims.get("iat"))

    def refresh_tokens(self, refresh_token, username):
        type(self).refresh_calls.append((refresh_token, username))
        if type(self).refresh_error is not None:
            raise type(self).refresh_error
        return TokenResponse(access_token=NEW_ACCESS, id_token="new-id",  # nosec B106 - fake tokens for the refresh test
                             expires_in=3600, token_type="Bearer")

    def get_user_groups(self, username):
        return []

    @staticmethod
    def is_admin(groups):
        return False

    @staticmethod
    def is_integrator(groups):
        return False


@pytest.fixture()
def client(monkeypatch):
    _StubAuth.refresh_calls = []
    _StubAuth.refresh_error = None
    monkeypatch.setattr(middleware_mod, "CognitoAuth", _StubAuth)
    monkeypatch.setattr(
        middleware_mod, "get_config",
        lambda: types.SimpleNamespace(has_cognito=True))
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUICK_GATEWAY_TOKEN", raising=False)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/whoami")
    async def whoami(request: Request):
        return {"access_token": request.state.session.get("access_token", "")}

    return TestClient(app, follow_redirects=False)


def _cookies(access_token: str, with_refresh: bool = True) -> dict:
    cookies = {SESSION_COOKIE_NAME: _encode_cookie({
        "access_token": access_token, "id_token": "", "username": "u@example.com"})}
    if with_refresh:
        cookies[_REFRESH_COOKIE_NAME] = _encode_cookie({"refresh_token": "rt-1"})
    return cookies


def _session_cookie_payload(resp) -> dict | None:
    raw = resp.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    return json.loads(base64.urlsafe_b64decode(raw))


def test_far_expiry_token_no_refresh_no_set_cookie(client):
    token = _make_token(3600)
    resp = client.get("/api/whoami", cookies=_cookies(token))
    assert resp.status_code == 200
    assert resp.json()["access_token"] == token
    assert _StubAuth.refresh_calls == []
    assert SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")


def test_near_expiry_with_refresh_cookie_refreshes_once_and_sets_cookie(client):
    token = _make_token(middleware_mod.REFRESH_LEEWAY - 30)
    resp = client.get("/api/whoami", cookies=_cookies(token))
    assert resp.status_code == 200
    # The request ran with the NEW token — the forwarded gateway bearer can
    # no longer expire mid-turn.
    assert resp.json()["access_token"] == NEW_ACCESS
    assert _StubAuth.refresh_calls == [("rt-1", "u@example.com")]
    payload = _session_cookie_payload(resp)
    assert payload is not None and payload["access_token"] == NEW_ACCESS


def test_near_expiry_refresh_failure_falls_through_with_valid_token(client):
    _StubAuth.refresh_error = AuthenticationError("cognito unavailable")
    token = _make_token(middleware_mod.REFRESH_LEEWAY - 30)
    resp = client.get("/api/whoami", cookies=_cookies(token))
    # Near-expiry refresh failure NEVER fails the request: the still-valid
    # original token serves it, and no stale cookie is written back.
    assert resp.status_code == 200
    assert resp.json()["access_token"] == token
    assert len(_StubAuth.refresh_calls) == 1
    assert SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")


def test_near_expiry_without_refresh_cookie_serves_without_refresh(client):
    token = _make_token(middleware_mod.REFRESH_LEEWAY - 30)
    resp = client.get("/api/whoami", cookies=_cookies(token, with_refresh=False))
    assert resp.status_code == 200
    assert resp.json()["access_token"] == token
    assert _StubAuth.refresh_calls == []


def test_expired_with_refresh_cookie_refreshes_and_sets_cookie(client):
    token = _make_token(-60)
    resp = client.get("/api/whoami", cookies=_cookies(token))
    assert resp.status_code == 200
    assert resp.json()["access_token"] == NEW_ACCESS
    assert _StubAuth.refresh_calls == [("rt-1", "u@example.com")]
    payload = _session_cookie_payload(resp)
    assert payload is not None and payload["access_token"] == NEW_ACCESS


def test_expired_refresh_failure_stays_401(client):
    _StubAuth.refresh_error = AuthenticationError("refresh denied")
    resp = client.get("/api/whoami", cookies=_cookies(_make_token(-60)))
    assert resp.status_code == 401


def test_refreshed_cookie_keeps_login_security_attributes(client):
    """The refreshed adt_session cookie shares the login route's _COOKIE_OPTS."""
    token = _make_token(30)  # inside REFRESH_LEEWAY
    resp = client.get("/api/whoami", cookies=_cookies(token))
    assert resp.status_code == 200
    raw = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in raw
    lowered = raw.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
