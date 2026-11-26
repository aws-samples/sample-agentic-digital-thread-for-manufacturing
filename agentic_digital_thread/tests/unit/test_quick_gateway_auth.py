# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AuthMiddleware x-quick-auth gateway gate (Quick integration).

The Quick API Gateway (kiosk path) and the query-bridge Gateway target reach
the app's async ask endpoints without a browser Cognito session, carrying the
shared secret header x-quick-auth == QUICK_GATEWAY_TOKEN. This suite pins the
gate's exact scope:

  * no header  -> 401;
  * wrong header -> 401;
  * correct header on an ALLOWED path -> passes, as a non-admin identity;
  * correct header on a NON-allowed path (/api/admin/*) -> still blocked;
  * GET-only carve-out: GET /api/prompts passes, POST /api/prompts does not;
  * fail-closed posture: an empty or "placeholder"-seeded token never admits;
  * a request that came through CloudFront (X-Amz-Cf-Id present) never
    passes on the secret, even when it is correct; it falls through to normal
    session auth, so a signed-in browser is unaffected.

Offline: Cognito is stubbed; no network.
"""

import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import agents.auth.middleware as middleware_mod
from agents.auth.cognito import TokenValidationError, UserInfo
from agents.auth.middleware import AuthMiddleware

_TOKEN = "unit-test-gateway-token"  # nosec B105 - test fixture


class _StubAuth:
    """CognitoAuth stand-in: every session/token is invalid (forces the gate)."""

    def __init__(self, *a, **k):
        pass

    def validate_token(self, token, id_token=None, **_kwargs):
        raise TokenValidationError("bad token")

    def get_user_groups(self, username):
        return []

    @staticmethod
    def is_admin(groups):
        return False

    @staticmethod
    def is_integrator(groups):
        return False


_SESSION_ACCESS_TOKEN = "unit-test-session-access-token"  # nosec B105 - test fixture


class _SessionAuth(_StubAuth):
    """CognitoAuth stand-in that accepts exactly one session access token."""

    def validate_token(self, token, id_token=None, **_kwargs):
        if token != _SESSION_ACCESS_TOKEN:
            raise TokenValidationError("bad token")
        return UserInfo(user_id="session-user", email="user@example.com",
                        username="user@example.com")


@pytest.fixture()
def client(monkeypatch):
    return _build_client(monkeypatch, _StubAuth)


def _build_client(monkeypatch, auth_cls):
    monkeypatch.setattr(middleware_mod, "CognitoAuth", auth_cls)
    monkeypatch.setattr(
        middleware_mod, "get_config",
        lambda: types.SimpleNamespace(has_cognito=True),
    )
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", _TOKEN)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.post("/api/ask/async")
    async def ask_async(request: Request):
        return {"who": request.state.user.user_id,
                "is_admin": request.state.is_admin}

    @app.get("/api/ask/result/{job_id}")
    async def ask_result(job_id: str):
        return {"status": "running", "job_id": job_id}

    @app.get("/api/prompts")
    async def prompts_get():
        return {"prompts": []}

    @app.post("/api/prompts")
    async def prompts_post():
        return {"saved": True}

    @app.get("/api/admin/quick-links")
    async def admin_quick_links():
        return {"admin": True}

    @app.get("/api/dashboard")
    async def dashboard():
        return {"cards": []}

    return TestClient(app, follow_redirects=False)


def _hdr(value=_TOKEN):
    return {AuthMiddleware.QUICK_GATEWAY_HEADER: value}


def test_no_header_is_unauthorized(client):
    assert client.post("/api/ask/async", json={"question": "q"}).status_code == 401
    assert client.get("/api/ask/result/j1").status_code == 401


def test_wrong_header_is_unauthorized(client):
    resp = client.post("/api/ask/async", json={"question": "q"},
                       headers=_hdr("not-the-token"))
    assert resp.status_code == 401
    assert client.get("/api/ask/result/j1", headers=_hdr("x")).status_code == 401


def test_correct_header_passes_on_allowed_paths_as_non_admin(client):
    resp = client.post("/api/ask/async", json={"question": "q"}, headers=_hdr())
    assert resp.status_code == 200
    assert resp.json() == {"who": "quick-gateway", "is_admin": False}
    resp = client.get("/api/ask/result/j1", headers=_hdr())
    assert resp.status_code == 200 and resp.json()["job_id"] == "j1"


def test_correct_header_on_non_allowed_paths_is_still_blocked(client):
    # The token must never open admin or general API surfaces.
    assert client.get("/api/admin/quick-links", headers=_hdr()).status_code == 401
    assert client.get("/api/dashboard", headers=_hdr()).status_code == 401


def test_prompts_carve_out_is_get_only(client):
    # GET /api/prompts (the query-bridge greeting catalog) passes...
    assert client.get("/api/prompts", headers=_hdr()).status_code == 200
    # ...but POST /api/prompts (creating prompts) keeps normal session auth.
    assert client.post("/api/prompts", json={}, headers=_hdr()).status_code == 401


def test_empty_or_placeholder_token_fails_closed(client, monkeypatch):
    # An environment whose SSM token was never written (deploy.sh seeds the
    # literal "placeholder") must reject even a matching header.
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "placeholder")
    resp = client.post("/api/ask/async", json={"question": "q"},
                       headers=_hdr("placeholder"))
    assert resp.status_code == 401
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "")
    assert client.post("/api/ask/async", json={"question": "q"},
                       headers=_hdr("")).status_code == 401


# ---------------------------------------------------------------------------
# Requests that came through CloudFront (X-Amz-Cf-Id present).
# The only legitimate senders of the secret (the Quick API Gateway over its
# VPC link, the query-bridge runtime) reach the internal ALB directly. The
# public distribution forwards viewer headers, so the gate refuses the secret
# on anything CloudFront forwarded; the edge strip function is the other layer.
# ---------------------------------------------------------------------------

_CF_ID = {AuthMiddleware.CLOUDFRONT_REQUEST_ID_HEADER: "unit-test-cloudfront-request-id"}

_ALLOWLISTED = (
    ("POST", "/api/ask/async"),
    ("GET", "/api/ask/result/j1"),
    ("GET", "/api/prompts"),
)


@pytest.mark.parametrize(("method", "path"), _ALLOWLISTED)
def test_correct_secret_through_cloudfront_is_unauthorized(client, method, path):
    resp = client.request(method, path, json={"question": "q"},
                          headers={**_hdr(), **_CF_ID})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(("method", "path"), _ALLOWLISTED)
def test_correct_secret_without_cloudfront_is_still_admitted(client, method, path):
    # The direct ALB callers are unchanged.
    resp = client.request(method, path, json={"question": "q"}, headers=_hdr())
    assert resp.status_code == 200


def test_cloudfront_header_name_matching_is_case_insensitive(client):
    # CloudFront sends "X-Amz-Cf-Id"; HTTP header names are case-insensitive.
    resp = client.post("/api/ask/async", json={"question": "q"},
                       headers={**_hdr(), "X-Amz-Cf-Id": "unit-test-cloudfront-request-id"})
    assert resp.status_code == 401


def test_refusal_through_cloudfront_logs_no_values(client, caplog):
    with caplog.at_level("WARNING", logger=middleware_mod.logger.name):
        client.post("/api/ask/async", json={"question": "q"},
                    headers={**_hdr(), **_CF_ID})
    messages = [r.getMessage() for r in caplog.records]
    assert "quick_gateway outcome=refused reason=via_cloudfront" in messages
    assert not any(_TOKEN in m or "unit-test-cloudfront-request-id" in m
                   for m in messages)


def test_signed_in_browser_through_cloudfront_is_unaffected(monkeypatch):
    # A valid session cookie still authenticates through CloudFront, on the
    # allowlisted routes and on normal routes, with or without a stray
    # x-quick-auth header.
    session_client = _build_client(monkeypatch, _SessionAuth)
    cookie = middleware_mod._encode_cookie(
        {"access_token": _SESSION_ACCESS_TOKEN, "username": "user@example.com"})
    session_client.cookies.set(middleware_mod.SESSION_COOKIE_NAME, cookie)
    resp = session_client.post("/api/ask/async", json={"question": "q"}, headers=_CF_ID)
    assert resp.status_code == 200
    assert resp.json() == {"who": "session-user", "is_admin": False}
    resp = session_client.post("/api/ask/async", json={"question": "q"},
                               headers={**_hdr(), **_CF_ID})
    assert resp.status_code == 200
    assert resp.json()["who"] == "session-user"
    assert session_client.get("/api/ask/result/j1", headers=_CF_ID).status_code == 200
    assert session_client.get("/api/prompts", headers=_CF_ID).status_code == 200
    assert session_client.get("/api/dashboard", headers=_CF_ID).status_code == 200


def test_cloudfront_header_alone_changes_nothing_on_other_routes(client):
    # No session, no secret: normal routes keep their plain 401.
    assert client.get("/api/dashboard", headers=_CF_ID).status_code == 401
    assert client.get("/api/admin/quick-links",
                      headers={**_hdr(), **_CF_ID}).status_code == 401
