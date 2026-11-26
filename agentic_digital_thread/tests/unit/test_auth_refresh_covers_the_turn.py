# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A gateway bearer outlives the turn that carries it.

Each turn's bearer is chosen once, when the turn starts, and every gateway call
in the turn sends it: an interactive turn forwards the user's access token
(ADR-0049), and a service turn (an async Quick ask) sends the cached
client-credentials token. A bearer that expires mid-turn fails every later
gateway call in that turn.

Seen on the 2026-10-14 workshop test event (Team 2, module 6 step 5): the user
signed in about 12:31Z (60-minute tokens), the turn ran 13:29:26Z to 13:36:08Z,
so it started with about 2.5 minutes left on the token. That was just outside
the old 120 s refresh leeway, so the token wasn't refreshed and expired
mid-turn. The same question asked again after expiry refreshed and worked.

Pinned here:

* the refresh leeway is derived from the app's turn ceiling
  (``agents.turn_limits``, the ONE home, which the async ask's abandon
  ceiling also reads) plus a margin; ``AUTH_REFRESH_LEEWAY_SECONDS`` still
  overrides it;
* a turn starting with 2.5 minutes left, or with less than a ceiling-length
  turn left, gets a fresh token;
* the refresh doesn't fire on every request: a just-refreshed 60-minute token
  isn't refreshed again, and a token whose own lifetime is short is refreshed
  only in the second half of its life;
* the service token cache replaces its token on the same rule, so an async
  ask can't start with a service token that ends before the ceiling.
"""
from __future__ import annotations

import time
import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import jwt

import agents.auth.middleware as middleware_mod
from agents.auth.cognito import TokenExpiredError, TokenResponse, UserInfo
from agents.auth.middleware import (
    SESSION_COOKIE_NAME, _REFRESH_COOKIE_NAME, AuthMiddleware, _encode_cookie,
)


def _token(remaining: float, lifetime: float | None = 3600.0) -> str:
    """A locally signed access token with ``remaining`` seconds left; issued
    ``lifetime - remaining`` seconds ago (no ``iat`` when ``lifetime`` is None)."""
    now = time.time()
    claims = {"exp": now + remaining, "sub": "u-1"}
    if lifetime is not None:
        claims["iat"] = now + remaining - lifetime
    return jwt.encode(claims, "unit-test-key", algorithm="HS256")


class _Auth:
    """CognitoAuth stand-in: exp-driven validation, recorded refreshes that
    issue a fresh 60-minute token."""

    refreshes: list = []
    issued: list = []

    def __init__(self, *a, **k):
        pass

    #: Times a test hands back for a token that isn't a JWT at all.
    opaque_times: dict = {}

    def validate_token(self, token, id_token=None, **_):
        if token in type(self).opaque_times:
            exp, iat = type(self).opaque_times[token]
        else:
            # Stands in for CognitoAuth's signature check; the claims it returns are trusted.
            claims = jwt.decode(token, "unit-test-key", algorithms=["HS256"],
                                options={"verify_exp": False})
            exp, iat = claims.get("exp"), claims.get("iat")
        if exp is not None and exp <= time.time():
            raise TokenExpiredError("Token expired")
        return UserInfo(user_id="u-1", email="u@example.com", username="u-1",
                        expires_at=exp, issued_at=iat)

    def refresh_tokens(self, refresh_token, username):
        fresh = _token(3600.0)
        type(self).refreshes.append(refresh_token)
        type(self).issued.append(fresh)
        return TokenResponse(access_token=fresh, id_token="new-id",  # nosec B106 - offline test token
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
    _Auth.refreshes, _Auth.issued = [], []
    monkeypatch.setattr(middleware_mod, "CognitoAuth", _Auth)
    monkeypatch.setattr(middleware_mod, "get_config",
                        lambda: types.SimpleNamespace(has_cognito=True))
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUICK_GATEWAY_TOKEN", raising=False)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/whoami")
    async def whoami(request: Request):
        return {"access_token": request.state.session.get("access_token", "")}

    return TestClient(app, follow_redirects=False)


def _ask(client, access_token: str) -> str:
    """One request as the browser sends it; the access token the request ran with."""
    cookies = {
        SESSION_COOKIE_NAME: _encode_cookie({
            "access_token": access_token, "id_token": "", "username": "u@example.com"}),
        _REFRESH_COOKIE_NAME: _encode_cookie({"refresh_token": "rt-1"}),
    }
    resp = client.get("/api/whoami", cookies=cookies)
    assert resp.status_code == 200
    return resp.json()["access_token"]


def test_a_turn_starting_with_two_and_a_half_minutes_left_gets_a_fresh_token(client):
    """The live timing: 150 s left at the turn's start."""
    used = _ask(client, _token(150.0))
    assert _Auth.refreshes == ["rt-1"]
    assert used == _Auth.issued[0]


def test_the_leeway_covers_the_longest_turn_the_app_allows(client):
    from agents import server
    from agents.turn_limits import ASK_RUN_CEILING_SECONDS
    assert server._ASK_RUN_CEILING_SECONDS == ASK_RUN_CEILING_SECONDS  # one home
    assert middleware_mod.REFRESH_LEEWAY > ASK_RUN_CEILING_SECONDS
    # A token that would end before a ceiling-length turn does is replaced.
    used = _ask(client, _token(ASK_RUN_CEILING_SECONDS + 30.0))
    assert used == _Auth.issued[0]


def test_the_leeway_is_the_ceiling_plus_the_margin_unless_overridden():
    from agents.turn_limits import ASK_RUN_CEILING_SECONDS, BEARER_REFRESH_MARGIN_SECONDS
    assert middleware_mod._refresh_leeway({}) == (
        ASK_RUN_CEILING_SECONDS + BEARER_REFRESH_MARGIN_SECONDS)
    assert middleware_mod._refresh_leeway({"AUTH_REFRESH_LEEWAY_SECONDS": "300"}) == 300


def test_a_just_refreshed_token_is_not_refreshed_on_the_next_request(client):
    first = _ask(client, _token(150.0))
    second = _ask(client, first)
    assert second == first
    assert len(_Auth.refreshes) == 1  # one refresh, not one per request


def test_a_short_lived_token_is_refreshed_only_in_the_second_half_of_its_life(client):
    """A 10-minute token is shorter than the leeway: refreshing it whenever it
    is inside the leeway would refresh on every request."""
    assert _ask(client, _token(540.0, lifetime=600.0)) != ""
    assert _Auth.refreshes == []
    used = _ask(client, _token(200.0, lifetime=600.0))
    assert _Auth.refreshes == ["rt-1"] and used == _Auth.issued[0]


def test_a_token_without_iat_keeps_the_full_leeway(client):
    _ask(client, _token(150.0, lifetime=None))
    assert _Auth.refreshes == ["rt-1"]


def test_the_refresh_timing_comes_from_the_validated_user(client, monkeypatch):
    """exp and iat are read from what validate_token verified, not decoded
    again from the raw token: a token string that is no JWT at all, whose
    validated user says 150 s are left, still gets a fresh token."""
    now = time.time()
    monkeypatch.setattr(_Auth, "opaque_times", {"opaque-access": (now + 150.0, now - 3450.0)})
    used = _ask(client, "opaque-access")
    assert _Auth.refreshes == ["rt-1"] and used == _Auth.issued[0]


def test_an_async_ask_never_starts_with_a_service_token_that_ends_mid_turn(monkeypatch):
    """Async Quick asks use the client-credentials service token, cached per
    process; one with 6 minutes left is replaced, not reused."""
    orch = pytest.importorskip("agents.orchestrator")
    from agents.turn_limits import ASK_RUN_CEILING_SECONDS
    assert orch._SERVICE_TOKEN_REFRESH_MARGIN_S > ASK_RUN_CEILING_SECONDS
    monkeypatch.setenv(orch.SERVICE_CLIENT_ID_ENV, "service-client-abc")
    monkeypatch.setattr(orch, "_service_token", "cached-token")
    monkeypatch.setattr(orch, "_service_token_expires_at", time.monotonic() + 360.0)
    monkeypatch.setattr(orch, "_service_client_secret_for", lambda *a, **k: "sekrit")
    monkeypatch.setattr(orch, "_resolve_app_domain_prefix", lambda cfg, pool: "adt-app-x")
    monkeypatch.setattr(orch, "mint_client_credentials_token",
                        lambda **kw: ("fresh-token", 3600))
    cfg = types.SimpleNamespace(aws_region="us-east-1", cognito_user_pool_id="us-east-1_EXAMPLE")
    try:
        assert orch._fetch_service_token(cfg) == "fresh-token"
        assert orch._fetch_service_token(cfg) == "fresh-token"  # reused, not re-minted
    finally:
        orch.reset_service_token_cache()
