# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Authorization@Edge session acceptance (agents/auth/edge_session.py + the
AuthMiddleware/logout seams).

* **Cookie parsing** — the SAR's ``CognitoIdentityServiceProvider.<clientId>.*``
  triple is extracted only when complete; missing LastAuthUser/accessToken or a
  wrong client id yields None (the middleware then behaves exactly as before).
* **Middleware acceptance** — with EDGE_AUTH_CLIENT_ID set and valid cookies,
  request.state is populated exactly like the adt_session path (user, session
  dict with access_token/id_token/username, roles) and the token validation is
  pinned to the EDGE client id.
* **/login.html redirect** — an edge-authenticated GET /login.html 302s to "/"
  (humans who signed in through the hosted UI never see the form).
* **Env off = zero behavior change** — without the env vars every seam is
  inert: no cookie acceptance, no login.html redirect, logout responds
  ``{"status": "ok"}`` with no redirect hint.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.auth import edge_session  # noqa: E402
import agents.auth.middleware as middleware_mod  # noqa: E402
import agents.auth.routes as routes  # noqa: E402
from agents.auth.cognito import TokenValidationError, UserInfo  # noqa: E402
from agents.auth.middleware import AuthMiddleware  # noqa: E402

_CLIENT_ID = "edgeclient123"
_PREFIX = f"CognitoIdentityServiceProvider.{_CLIENT_ID}"


def _edge_cookies(username="alice", access="edge-access-token", id_tok="edge-id-token"):  # nosec B107 - offline test fixtures, not credentials
    return {
        f"{_PREFIX}.LastAuthUser": username,
        f"{_PREFIX}.{username}.accessToken": access,
        f"{_PREFIX}.{username}.idToken": id_tok,
        f"{_PREFIX}.{username}.refreshToken": "edge-refresh",  # nosec B105 - test fixture
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_parse_edge_cookies_complete_triple():
    tokens = edge_session.parse_edge_cookies(_edge_cookies(), _CLIENT_ID)
    assert tokens is not None
    assert tokens.username == "alice"
    assert tokens.access_token == "edge-access-token"  # nosec B105 - test fixture
    assert tokens.id_token == "edge-id-token"  # nosec B105 - test fixture


def test_parse_edge_cookies_missing_last_auth_user():
    cookies = _edge_cookies()
    del cookies[f"{_PREFIX}.LastAuthUser"]
    assert edge_session.parse_edge_cookies(cookies, _CLIENT_ID) is None


def test_parse_edge_cookies_missing_access_token():
    cookies = _edge_cookies()
    del cookies[f"{_PREFIX}.alice.accessToken"]
    assert edge_session.parse_edge_cookies(cookies, _CLIENT_ID) is None


def test_parse_edge_cookies_missing_id_token_is_ok():
    cookies = _edge_cookies()
    del cookies[f"{_PREFIX}.alice.idToken"]
    tokens = edge_session.parse_edge_cookies(cookies, _CLIENT_ID)
    assert tokens is not None and tokens.id_token is None


def test_parse_edge_cookies_wrong_client_id():
    assert edge_session.parse_edge_cookies(_edge_cookies(), "other-client") is None


def test_parse_edge_cookies_empty_client_id():
    assert edge_session.parse_edge_cookies(_edge_cookies(), "") is None


def test_edge_client_id_env(monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    assert edge_session.edge_client_id() is None
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", "  ")
    assert edge_session.edge_client_id() is None
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    assert edge_session.edge_client_id() == _CLIENT_ID


def test_edge_auth_enabled_env(monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_ENABLED", raising=False)
    assert edge_session.edge_auth_enabled() is False
    monkeypatch.setenv("EDGE_AUTH_ENABLED", "true")
    assert edge_session.edge_auth_enabled() is True
    monkeypatch.setenv("EDGE_AUTH_ENABLED", "false")
    assert edge_session.edge_auth_enabled() is False


# ---------------------------------------------------------------------------
# External-IdP-shaped session (ADR-0072)
#
# A user who signed in through the optional external OIDC provider is stored
# in the pool under ``<ProviderName>_<subject>`` — that is the LastAuthUser
# value the SAR writes, so it flows through untouched as the session username
# (and therefore as the per-person AgentCore memory actor). Only presence and
# shape are handled here; the token itself is validated by the middleware.
# ---------------------------------------------------------------------------

_EXTERNAL_USERNAME = "CorporateIdP_abc123"


def test_parse_edge_cookies_external_idp_username_passes_through_verbatim():
    cookies = _edge_cookies(username=_EXTERNAL_USERNAME)
    tokens = edge_session.parse_edge_cookies(cookies, _CLIENT_ID)
    assert tokens is not None
    assert tokens.username == _EXTERNAL_USERNAME
    assert tokens.access_token == "edge-access-token"  # nosec B105 - test fixture
    assert tokens.id_token == "edge-id-token"  # nosec B105 - test fixture


# ---------------------------------------------------------------------------
# Middleware behavior
# ---------------------------------------------------------------------------

class _StubAuth:
    """CognitoAuth stand-in; records the client_id it was pinned to."""

    last_client_id = None

    def __init__(self, client_id=None, **_kwargs):
        _StubAuth.last_client_id = client_id

    def validate_token(self, token, id_token=None, **_kwargs):
        if token != "edge-access-token":  # nosec B105 - test fixture
            raise TokenValidationError("bad token")
        return UserInfo(user_id="u-edge", email="alice@example.com", username="alice")

    def get_user_groups(self, username):
        return ["Admin"]

    @staticmethod
    def is_admin(groups):
        return "Admin" in groups

    @staticmethod
    def is_integrator(groups):
        return "integrator" in groups or "Admin" in groups


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setattr(middleware_mod, "CognitoAuth", _StubAuth)
    monkeypatch.setattr(
        middleware_mod, "get_config",
        lambda: types.SimpleNamespace(has_cognito=True),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/whoami")
    async def whoami(request: Request):
        return {
            "username": request.state.session.get("username"),
            "access_token": request.state.session.get("access_token"),
            "id_token": request.state.session.get("id_token"),
            "user_id": request.state.user.user_id,
            "is_admin": request.state.is_admin,
            "is_integrator": request.state.is_integrator,
        }

    @app.get("/login.html")
    async def login_page():
        return {"page": "login-form"}

    return TestClient(app, follow_redirects=False)


def test_env_off_no_session_is_unauthorized_unchanged(app_client, monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    resp = app_client.get("/api/whoami", cookies=_edge_cookies())
    assert resp.status_code == 401  # edge cookies ignored without the env


def test_env_off_login_html_serves_the_form(app_client, monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    resp = app_client.get("/login.html", cookies=_edge_cookies())
    assert resp.status_code == 200 and resp.json() == {"page": "login-form"}


def test_edge_cookies_accepted_and_state_matches_session_path(app_client, monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    resp = app_client.get("/api/whoami", cookies=_edge_cookies())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "username": "alice",
        "access_token": "edge-access-token",
        "id_token": "edge-id-token",
        "user_id": "u-edge",
        "is_admin": True,
        "is_integrator": True,
    }
    # Token validation was pinned to the EDGE client id, not the app client.
    assert _StubAuth.last_client_id == _CLIENT_ID


def test_external_idp_session_accepted_without_pool_groups(app_client, monkeypatch):
    """An external-IdP user (``<Provider>_<subject>``, no pool groups) signs in
    with NO elevated roles — group membership is looked up in the pool by
    username and an externally provisioned user has none (ADR-0072)."""
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setattr(_StubAuth, "get_user_groups", lambda self, username: [])
    resp = app_client.get("/api/whoami", cookies=_edge_cookies(username=_EXTERNAL_USERNAME))
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == _EXTERNAL_USERNAME
    assert body["is_admin"] is False
    assert body["is_integrator"] is False


def test_edge_cookies_invalid_token_still_unauthorized(app_client, monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    cookies = _edge_cookies(access="tampered-token")
    resp = app_client.get("/api/whoami", cookies=cookies)
    assert resp.status_code == 401


def test_edge_cookies_incomplete_still_unauthorized(app_client, monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    cookies = _edge_cookies()
    del cookies[f"{_PREFIX}.alice.accessToken"]
    resp = app_client.get("/api/whoami", cookies=cookies)
    assert resp.status_code == 401


def test_login_html_redirects_home_with_valid_edge_session(app_client, monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    resp = app_client.get("/login.html", cookies=_edge_cookies())
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_login_html_serves_form_when_edge_cookies_invalid(app_client, monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    resp = app_client.get("/login.html", cookies=_edge_cookies(access="tampered-token"))
    assert resp.status_code == 200 and resp.json() == {"page": "login-form"}


# ---------------------------------------------------------------------------
# Logout route
# ---------------------------------------------------------------------------

@pytest.fixture()
def logout_client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_logout_env_off_unchanged(logout_client, monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    resp = logout_client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_logout_edge_auth_keeps_edge_cookies_and_hints_signout(logout_client, monkeypatch):
    """The SAR SignOut handler at /signout needs the id token to log out of
    Cognito and expire the edge cookies at their own paths; without it the
    edge answers /signout with a redirect into sign-in (ADR-0070). So the API
    expires only the app's own session cookies."""
    monkeypatch.setenv("EDGE_AUTH_ENABLED", "true")
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    resp = logout_client.post("/api/auth/logout", cookies=_edge_cookies())
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "redirect": "/signout"}
    expired = [c.split("=", 1)[0] for c in resp.headers.get_list("set-cookie")]
    assert "adt_session" in expired
    for name in _edge_cookies():
        assert name not in expired, name


def test_logout_then_signout_still_carries_the_id_token(logout_client, monkeypatch):
    """Browser-jar view: after the logout response is applied, the request
    the frontend sends to /signout still carries the SAR id-token cookie."""
    monkeypatch.setenv("EDGE_AUTH_ENABLED", "true")
    monkeypatch.setenv("EDGE_AUTH_CLIENT_ID", _CLIENT_ID)
    for name, value in {**_edge_cookies(), "adt_session": "s"}.items():
        logout_client.cookies.set(name, value)
    resp = logout_client.post("/api/auth/logout")
    assert resp.json()["redirect"] == "/signout"
    jar = dict(logout_client.cookies)
    assert jar.get(f"{_PREFIX}.alice.idToken") == "edge-id-token"
    assert jar.get(f"{_PREFIX}.LastAuthUser") == "alice"
