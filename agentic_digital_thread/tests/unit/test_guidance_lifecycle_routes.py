# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HTTP contracts for strict guidance preview and acceptance."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.self_learning as self_learning
from agents.server import accept_proposed_mapping, preview_proposed_guidance


def _client():
    app = FastAPI()
    app.add_api_route("/api/proposed-mappings/{mapping_id}/guidance-preview",
                      preview_proposed_guidance, methods=["GET"])
    app.add_api_route("/api/proposed-mappings/{mapping_id}/accept",
                      accept_proposed_mapping, methods=["POST"])
    return TestClient(app)


def _opaque_preview(outcome: str):
    return {"outcome": outcome, "current_digest": "sha256:current",
            "current_length": 50, "note_digest": "sha256:note",
            "note_length": 20, "result_digest": "sha256:result",
            "result_length": 60,
            "evicted_note_digests": ["sha256:evicted"]
            if outcome == "would_evict" else []}


def test_preview_route_is_opaque_and_side_effect_free(monkeypatch):
    calls = []
    monkeypatch.setattr(
        self_learning, "preview_guidance_mapping",
        lambda mapping_id: calls.append(mapping_id) or {
            "status": "preview", "mapping_id": mapping_id,
            "preview": _opaque_preview("would_evict")})

    response = _client().get(
        "/api/proposed-mappings/mapping-1/guidance-preview")

    assert response.status_code == 200
    assert response.json()["preview"]["outcome"] == "would_evict"
    assert calls == ["mapping-1"]
    assert "customer requirement" not in response.text


def test_eviction_confirmation_body_is_forwarded_exactly(monkeypatch):
    captured = []
    confirmation = {"current_digest": "sha256:current",
                    "evicted_note_digests": ["sha256:evicted"]}

    def fake_accept(mapping_id, actor="", guidance_confirmation=None, confirmation_mode=""):
        captured.append(guidance_confirmation)
        return {"status": "accepted", "outcome": "would_evict",
                "proposed_type": "guidance_note", "version": 1}

    monkeypatch.setattr(self_learning, "accept_mapping", fake_accept)
    response = _client().post(
        "/api/proposed-mappings/mapping-1/accept", json=confirmation)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert captured == [confirmation]


@pytest.mark.parametrize(
    "outcome,code,drift",
    [
        ("would_evict", 409, {"graph": False, "delivery": False}),
        ("duplicate", 409, {"graph": False, "delivery": False}),
        ("no_room", 409, {"graph": False, "delivery": False}),
        ("append", 503, {"graph": False, "delivery": False}),
        ("append", 503, {"graph": True, "delivery": False}),
        ("append", 503, {"graph": False, "delivery": True}),
    ],
)
def test_non_delivered_guidance_never_returns_accepted(
        monkeypatch, outcome, code, drift):
    monkeypatch.setattr(
        self_learning, "accept_mapping",
        lambda *args, **kwargs: {
            "status": "pending", "outcome": outcome,
            "error": "guidance_update_failed", "drift": drift,
            "http_status": code, "preview": _opaque_preview(outcome)})

    response = _client().post(
        "/api/proposed-mappings/mapping-1/accept", json={})

    assert response.status_code == code
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["error"] == "guidance_update_failed"
    assert payload["drift"] == drift
    assert "guidance-noop-" not in response.text
    assert "http_status" not in payload


def test_delivered_guidance_accept_returns_200_with_no_error_key(monkeypatch):
    """Measured live twice (two deployments, ~30 accepts): every SUCCESSFUL
    guidance-note accept answered 500 AFTER the graph + delivery + proposal
    writes had completed. ``guidance_saga._success`` carries ``"error": None``
    as its fixed shape; two dict spreads later the accepted result still had
    the key, and the route's codebase-wide ``"error" in result`` idiom read
    key-presence as failure and then did ``"not found" in None`` → TypeError.
    Drives the REAL accept_mapping with only the store and saga seams stubbed
    (the existing route tests stub accept_mapping itself and could not see it)."""
    class _Table:
        def query(self, **kw):
            return {"Items": [{"mapping_id": "m-1", "discovered_at": "t0",
                               "proposed_type": "guidance_note", "status": "pending",
                               "from_system": "system-a", "subject_key": "guidance:system-a",
                               "to_value": "A short steward note.", "version": "1"}]}
    monkeypatch.setattr(self_learning, "_get_table", lambda: _Table())
    delivered = {"status": "synchronized", "error": None,
                 "drift": {"graph": False, "delivery": False},
                 "graph_digest": "sha256:g", "delivery_digest": "sha256:d",
                 "delivery_version": 3}
    monkeypatch.setattr(
        self_learning, "_execute_guidance_item",
        lambda table, item, actor, confirmation=None: {
            **delivered, "status": "delivered", "outcome": "append",
            "preview": _opaque_preview("append")})
    response = _client().post("/api/proposed-mappings/m-1/accept", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted" and body["proposed_type"] == "guidance_note"
    assert "error" not in body
