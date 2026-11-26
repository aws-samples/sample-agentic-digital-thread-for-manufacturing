# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the git-derived app version on /health + /api/version.

``agents.server._APP_VERSION`` is read from the ``APP_VERSION`` env var at import
time (baked into the adt-app image env by the Dockerfile's ``APP_VERSION``
build-arg, which deploy.sh derives from git and adt_compute_stack forwards through
the DockerImageAsset build_args). These tests reload ``agents.server`` under a
controlled env and drive the REAL handlers via a minimal FastAPI app +
``TestClient`` (mirrors the minimal-app style in
``test_user_defined_mapping_route.py``). No AWS, no graph, no network.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.server as server


@pytest.fixture(autouse=True)
def _restore_server_module():
    """Reload agents.server after each test (after monkeypatch has restored the
    env) so the module-level ``_APP_VERSION`` reflects the process's real
    environment for any later test that imports from the module."""
    yield
    importlib.reload(server)


def _client_for(monkeypatch, version: str | None) -> TestClient:
    """Reload agents.server with APP_VERSION set (or unset) and return a
    TestClient over a minimal app registering the real handlers."""
    if version is None:
        monkeypatch.delenv("APP_VERSION", raising=False)
    else:
        monkeypatch.setenv("APP_VERSION", version)
    importlib.reload(server)
    app = FastAPI()
    app.add_api_route("/api/version", server.get_version, methods=["GET"])
    app.add_api_route("/health", server.health, methods=["GET"])
    return TestClient(app)


def test_version_route_returns_env_value(monkeypatch):
    client = _client_for(monkeypatch, "abc1234 · 2026-07-15")
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "abc1234 · 2026-07-15"}


def test_health_reports_same_version(monkeypatch):
    client = _client_for(monkeypatch, "abc1234-dirty · 2026-07-15")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "abc1234-dirty · 2026-07-15"}


def test_version_defaults_to_dev_when_env_unset(monkeypatch):
    client = _client_for(monkeypatch, None)
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "dev"}
