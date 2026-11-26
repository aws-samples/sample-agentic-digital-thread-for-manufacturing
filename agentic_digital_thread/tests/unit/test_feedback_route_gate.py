# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route gate for POST /api/feedback (the feedback-exploration entry point).

The feedback route accepts UNTRUSTED free text that can start a background
exploration filing self-learning proposals — it must stay authenticated
(never in the middleware public lists) and the exploration's synthetic
stream must never write memory or carry a caller identity claim.

Offline (``test_onboard_route_gate.py`` style). Pins kill plan mutations
M24 (route made public), M25 (401 removed), M26 (route dropped), plus a
source-level gate that the exploration module never flips ``write_memory``
or supplies ``_adt_identity`` (behavioral pins live in
``test_feedback_exploration.py``).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import agents.feedback as fb
import agents.feedback_exploration as fx
from agents.auth.middleware import AuthMiddleware


def test_feedback_route_is_not_public():
    """M24: route made public — /api/feedback must require auth middleware."""
    assert "/api/feedback" not in AuthMiddleware.PUBLIC_ROUTES
    assert not any("/api/feedback".startswith(p)
                   for p in AuthMiddleware.PUBLIC_PREFIXES)


def test_router_exposes_post_api_feedback():
    """M26: route dropped — the gate SEES the route."""
    routes = [r for r in fb.router.routes
              if isinstance(r, APIRoute) and r.path == "/api/feedback"]
    assert len(routes) == 1
    assert routes[0].methods == {"POST"}


def test_post_feedback_401_without_user(monkeypatch):
    """M25: 401 removed — no request.state.user ⇒ 401 before any storage."""
    stored: list = []
    monkeypatch.setattr(
        fb, "_service", SimpleNamespace(store=lambda r: stored.append(r)))
    app = FastAPI()
    app.include_router(fb.router)
    client = TestClient(app)
    resp = client.post("/api/feedback", json={
        "message_id": "m-1", "session_id": "s-1", "user_message": "q",
        "assistant_response": "a", "tools_used": [],
        "sentiment": "negative", "user_comment": "wrong"})
    assert resp.status_code == 401
    assert stored == []


def test_exploration_source_never_writes_memory_or_identity():
    """Source-level gate (grep-style): the exploration module's stream setup
    carries ``write_memory=False`` on EVERY stream call and never mentions
    ``_adt_identity`` — the behavioral kwargs pins are in
    ``test_feedback_exploration.py`` (M22/M23)."""
    source = inspect.getsource(fx)
    assert "write_memory=True" not in source
    assert source.count("write_memory=False") >= 2  # both client branches
    assert "_adt_identity=" not in source  # never passed as a kwarg
    assert "extra_payload=" not in source  # no payload smuggling seam


def test_exploration_module_never_writes_accepted_status():
    """Companion gate: nothing in the new module writes status=accepted —
    proposals land ONLY through log_proposed_mapping (never pre-accepted)."""
    source = inspect.getsource(fx)
    assert "accept_mapping" not in source
    assert '"accepted"' not in source and "'accepted'" not in source
    assert "log_proposed_mapping" in source
