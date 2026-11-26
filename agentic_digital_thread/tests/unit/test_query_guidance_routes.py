# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the learned query-guidance admin endpoints.

  GET  /api/query-guidance
  POST /api/query-guidance/{system_id}/prune
  POST /api/query-guidance/{system_id}/clear

No AWS, no Neptune, no network: a minimal FastAPI app registers the three real
handlers from ``agents.server`` and drives them via ``TestClient`` (mirrors the
minimal-app style in ``test_proposed_mappings_bulk_routes.py``). The
``self_learning`` reader/mutators are monkeypatched at the module level (the
routes import them lazily from the module, so patching the attribute is enough).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.self_learning as self_learning
from agents.server import (
    clear_query_guidance_endpoint,
    list_query_guidance,
    prune_query_guidance,
)


def _client():
    app = FastAPI()
    app.add_api_route("/api/query-guidance", list_query_guidance, methods=["GET"])
    app.add_api_route("/api/query-guidance/{system_id}/prune",
                      prune_query_guidance, methods=["POST"])
    app.add_api_route("/api/query-guidance/{system_id}/clear",
                      clear_query_guidance_endpoint, methods=["POST"])
    return TestClient(app)


# --------------------------------------------------------------------------- #
# GET /api/query-guidance
# --------------------------------------------------------------------------- #
def test_list_query_guidance_returns_systems(monkeypatch):
    systems = [
        {"system_id": "erp", "base": "Base.", "notes": ["a", "b"],
         "learned_count": 2, "length": 20, "max_len": 4000},
        {"system_id": "mes", "base": "Base.", "notes": [],
         "learned_count": 0, "length": 5, "max_len": 4000},
    ]
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda: systems)
    resp = _client().get("/api/query-guidance")
    assert resp.status_code == 200
    assert resp.json() == {"systems": systems, "count": 2}


def test_list_query_guidance_empty(monkeypatch):
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda: [])
    resp = _client().get("/api/query-guidance")
    assert resp.json() == {"systems": [], "count": 0}


# --------------------------------------------------------------------------- #
# POST /api/query-guidance/{system_id}/prune
# --------------------------------------------------------------------------- #
def test_prune_success(monkeypatch):
    captured = {}

    def _prune(system_id, note):
        captured["args"] = (system_id, note)
        return {"status": "pruned", "removed": 1, "remaining": 2}

    monkeypatch.setattr(self_learning, "prune_query_guidance_note", _prune)
    resp = _client().post("/api/query-guidance/erp/prune", json={"note": "drop me"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pruned", "removed": 1, "remaining": 2}
    assert captured["args"] == ("erp", "drop me")


def test_prune_missing_body_note(monkeypatch):
    monkeypatch.setattr(self_learning, "prune_query_guidance_note",
                        lambda s, n: {"error": "system_id and note are required"})
    resp = _client().post("/api/query-guidance/erp/prune", json={})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_prune_note_not_found_is_404(monkeypatch):
    monkeypatch.setattr(self_learning, "prune_query_guidance_note",
                        lambda s, n: {"error": "note not found", "removed": 0})
    resp = _client().post("/api/query-guidance/erp/prune", json={"note": "x"})
    assert resp.status_code == 404


def test_prune_unknown_system_is_404(monkeypatch):
    monkeypatch.setattr(self_learning, "prune_query_guidance_note",
                        lambda s, n: {"error": "System ghost not found"})
    resp = _client().post("/api/query-guidance/ghost/prune", json={"note": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/query-guidance/{system_id}/clear
# --------------------------------------------------------------------------- #
def test_clear_success(monkeypatch):
    monkeypatch.setattr(self_learning, "clear_query_guidance",
                        lambda s: {"status": "cleared", "removed": 3})
    resp = _client().post("/api/query-guidance/erp/clear")
    assert resp.status_code == 200
    assert resp.json() == {"status": "cleared", "removed": 3}


def test_clear_unknown_system_is_404(monkeypatch):
    monkeypatch.setattr(self_learning, "clear_query_guidance",
                        lambda s: {"error": "System ghost not found"})
    resp = _client().post("/api/query-guidance/ghost/clear")
    assert resp.status_code == 404
