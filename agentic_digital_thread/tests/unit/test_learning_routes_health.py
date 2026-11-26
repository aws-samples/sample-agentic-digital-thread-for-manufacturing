# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the learning admin router's ``GET /health``
(ADR-0052 B1).

``fastapi.testclient`` against the bare router with every impure seam
monkeypatched (the ``test_vocabulary_routes.py`` idiom) — no AWS, no
network. Covers:

  * 200 with the pinned health-response shape (Wave-2 consumers and the B1
    audit script depend on it VERBATIM);
  * blocking stat assembly runs off-loop (``build_health`` is called);
  * a thrown backend error degrades to the all-zeros shape — NEVER a 500;
  * staging counts are never folded into any pending number;
  * import purity — no boto3/pg8000 at module import time.

Auth note: ``/api/admin/*`` is gated by ``AuthMiddleware.ADMIN_PREFIXES``
before any handler runs (pinned in the middleware's own tests); the app
built here mounts the router bare, exercising handler behavior only.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.learning_health as lh
import agents.learning_routes as lr


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(lr.router)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app())


_HEALTHY = {
    "integrity_mode": "report-only",
    "usage_ledger": {"rows_30d": 5, "artifacts": 2,
                     "evidence_classes": ["tool-text"]},
    "staging": {"rows": 3, "distinct_subjects": 2},
    "pending_ages_days": [0.1, 2.5],
    "quarantine": {"pending_quarantined": 4},
    "guidance_budget": {"available": True, "systems": 2,
                        "over_budget": True,
                        "over_by": 88, "over_budget_systems": [
                            {"system_id": "sys-a", "over_by": 88}]},
    "guidance_delivery": {"available": True, "systems": 2, "field_cap": 1024,
                          "truncated": True, "notes_dropped": 1,
                          "chars_dropped": 2932, "truncated_systems": [
                              {"system_id": "sys-a",
                               "fields": ["query_guidance"],
                               "notes_dropped": 1, "notes_truncated": 0,
                               "chars_dropped": 2932}]},
    "graph_integrity": {"authority_rule_evidence": {
        "available": True, "active_rules": 2, "orphan_count": 1,
        "evidence_orphans": [{"rule_id": "ar-system-a-state",
                              "system_id": "system-a", "concept": "state",
                              "mapped_systems": []}]},
    },
}

_PINNED_KEYS = {"integrity_mode", "usage_ledger", "staging",
                "pending_ages_days", "quarantine", "guidance_budget",
                "guidance_delivery", "graph_integrity"}


def test_health_returns_pinned_shape(client, monkeypatch):
    monkeypatch.setattr(lh, "build_health", lambda: dict(_HEALTHY))

    response = client.get("/api/admin/learning/health")

    assert response.status_code == 200
    body = response.json()
    assert body == _HEALTHY
    assert set(body) == _PINNED_KEYS
    # Sub-shapes pinned exactly — Wave-2 consumers key on these fields.
    assert set(body["usage_ledger"]) == {"rows_30d", "artifacts",
                                         "evidence_classes"}
    assert set(body["staging"]) == {"rows", "distinct_subjects"}
    assert set(body["quarantine"]) == {"pending_quarantined"}


def test_health_calls_shared_module_builder(client, monkeypatch):
    """The route delegates to the SHARED ``learning_health.build_health`` —
    the session requirement (B1 script and B5 digest consume the same
    functions, never duplicated queries)."""
    calls = []

    def spy():
        calls.append(True)
        return dict(_HEALTHY)

    monkeypatch.setattr(lh, "build_health", spy)

    assert client.get("/api/admin/learning/health").status_code == 200
    assert calls == [True]


def test_health_backend_error_degrades_to_zeros_never_500(client, monkeypatch):
    def boom():
        raise RuntimeError("backend down")

    monkeypatch.setattr(lh, "build_health", boom)
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)

    response = client.get("/api/admin/learning/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == _PINNED_KEYS
    assert body["usage_ledger"] == {"rows_30d": 0, "artifacts": 0,
                                    "evidence_classes": []}
    assert body["staging"] == {"rows": 0, "distinct_subjects": 0}
    assert body["pending_ages_days"] == []
    assert body["quarantine"] == {"pending_quarantined": 0}
    assert body["guidance_budget"] == {
        "available": False, "systems": None, "over_budget": False, "over_by": 0,
        "over_budget_systems": [],
    }
    assert body["integrity_mode"] == "report-only"


def test_health_end_to_end_with_fake_backends(client, monkeypatch):
    """Full path through the real ``build_health`` with fake table/ledger —
    staging rows are counted in the census and NEVER as pending."""
    import agents.artifact_usage as artifact_usage
    import agents.self_learning as self_learning

    class FakeTable:
        def scan(self, **kwargs):
            # Only the staged-prefix census scans here.
            return {"Items": [
                {"mapping_id": "staged#x-uuid", "subject_key": "staged#subj-a"},
                {"mapping_id": "staged#y-uuid", "subject_key": "staged#subj-a"},
            ]}

    monkeypatch.setattr(self_learning, "_get_table", lambda: FakeTable())
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda **_k: [])
    monkeypatch.setattr(artifact_usage, "read_artifact_usage",
                        lambda days=30: {})
    monkeypatch.setattr(lh, "read_graph_integrity", lambda: {
        "authority_rule_evidence": {
            "available": True, "active_rules": 0, "orphan_count": 0,
            "evidence_orphans": [],
        }})
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")

    body = client.get("/api/admin/learning/health").json()

    assert body["integrity_mode"] == "enforce"
    assert body["staging"] == {"rows": 2, "distinct_subjects": 1}
    assert body["pending_ages_days"] == []           # staged ≠ pending
    assert body["quarantine"] == {"pending_quarantined": 0}


def test_router_prefix_is_admin_gated_path():
    """The auto-gated ``/api/admin`` prefix (AuthMiddleware ADMIN_PREFIXES)
    — the router must live under it, same posture as vocabulary_routes."""
    assert lr.router.prefix == "/api/admin/learning"


def test_module_source_has_no_impure_top_level_imports():
    for module in (lr, lh):
        src = open(module.__file__).read()
        assert "import boto3" not in src
        assert "import pg8000" not in src and "import pgdriver" not in src
