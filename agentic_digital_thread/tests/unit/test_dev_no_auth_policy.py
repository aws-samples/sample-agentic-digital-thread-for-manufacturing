# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Security boundary tests for the local-only authentication bypass."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agents.auth import middleware
from agents.auth.middleware import AuthMiddleware
from agents.config import dev_no_auth_allowed

_LOCAL_SIGNALS = {
    "DEV_NO_AUTH": "1",
    "DEV_MODE": "1",
    "AGENT_MODE": "local",
}
_CLOUD_MARKERS = (
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
    "AWS_EXECUTION_ENV",
    "COGNITO_USER_POOL_ID",
    "AGENTCORE_GATEWAY_URL",
    "ONBOARDING_GATEWAY_URL",
)


@pytest.fixture(autouse=True)
def _clean_auth_environment(monkeypatch):
    for key in (*_LOCAL_SIGNALS, *_CLOUD_MARKERS):
        monkeypatch.delenv(key, raising=False)


def _set_local_signals(monkeypatch):
    for key, value in _LOCAL_SIGNALS.items():
        monkeypatch.setenv(key, value)


def _app(monkeypatch, *, has_cognito=False) -> FastAPI:
    monkeypatch.setattr(
        middleware,
        "get_config",
        lambda: SimpleNamespace(
            has_cognito=has_cognito,
            dev_user_id="dev-user-001",
        ),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/private")
    def private(request: Request):
        user = request.state.user
        return {
            "user_id": user.user_id,
            "is_admin": request.state.is_admin,
            "is_integrator": request.state.is_integrator,
        }

    @app.get("/private-page")
    def private_page():
        return {"status": "ok"}

    @app.get("/{path:path}")
    def public_probe(path: str):
        return {"path": path}

    return app


def test_complete_exact_local_contract_allows_bypass(monkeypatch):
    _set_local_signals(monkeypatch)
    assert dev_no_auth_allowed() is True


@pytest.mark.parametrize(
    "signals",
    [
        {},
        {"DEV_NO_AUTH": "1"},
        {"DEV_NO_AUTH": "1", "DEV_MODE": "1"},
        {"DEV_NO_AUTH": "1", "AGENT_MODE": "local"},
        {"DEV_MODE": "1", "AGENT_MODE": "local"},
        {"DEV_NO_AUTH": "true", "DEV_MODE": "1", "AGENT_MODE": "local"},
        {"DEV_NO_AUTH": "1", "DEV_MODE": "true", "AGENT_MODE": "local"},
        {"DEV_NO_AUTH": "1", "DEV_MODE": "1", "AGENT_MODE": "LOCAL"},
        {"DEV_NO_AUTH": " 1", "DEV_MODE": "1", "AGENT_MODE": "local"},
    ],
)
def test_unset_partial_or_malformed_local_contract_is_refused(monkeypatch, signals):
    for key, value in signals.items():
        monkeypatch.setenv(key, value)
    assert dev_no_auth_allowed() is False


@pytest.mark.parametrize("marker", _CLOUD_MARKERS)
def test_any_cloud_or_deployed_marker_overrides_complete_local_flags(monkeypatch, marker):
    _set_local_signals(monkeypatch)
    monkeypatch.setenv(marker, "present")
    assert dev_no_auth_allowed() is False


def test_complete_local_bypass_keeps_documented_identity_and_roles(monkeypatch, caplog):
    _set_local_signals(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=middleware.__name__):
        response = TestClient(_app(monkeypatch)).get("/api/private")
    assert response.status_code == 200
    assert response.json() == {
        "user_id": "dev-user-001",
        "is_admin": True,
        "is_integrator": True,
    }
    assert "local authentication bypass enabled" in caplog.text.lower()


@pytest.mark.parametrize("path, expected_status", [
    ("/api/private", 401),
    ("/private-page", 302),
])
def test_dev_no_auth_alone_is_refused_for_api_and_html(
    monkeypatch, caplog, path, expected_status,
):
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    with caplog.at_level(logging.ERROR, logger=middleware.__name__):
        response = TestClient(_app(monkeypatch), follow_redirects=False).get(path)
    assert response.status_code == expected_status
    assert "requested but refused" in caplog.text.lower()


def test_refusal_log_does_not_expose_environment_values(monkeypatch, caplog):
    _set_local_signals(monkeypatch)
    marker_value = "sensitive-runtime-marker-value"
    monkeypatch.setenv("AWS_EXECUTION_ENV", marker_value)
    with caplog.at_level(logging.ERROR, logger=middleware.__name__):
        response = TestClient(_app(monkeypatch)).get("/api/private")
    assert response.status_code == 401
    assert "requested but refused" in caplog.text.lower()
    assert marker_value not in caplog.text


def test_cognito_configuration_always_uses_real_auth_path(monkeypatch):
    _set_local_signals(monkeypatch)
    response = TestClient(_app(monkeypatch, has_cognito=True)).get("/api/private")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("path", [
    "/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/branding",
    "/assets/app.js",
    "/css/app.css",
    "/js/app.js",
    "/fonts/app.woff2",
    "/login.html",
])
def test_existing_public_routes_remain_public_under_cloud_markers(monkeypatch, path):
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")
    response = TestClient(_app(monkeypatch)).get(path)
    assert response.status_code == 200
