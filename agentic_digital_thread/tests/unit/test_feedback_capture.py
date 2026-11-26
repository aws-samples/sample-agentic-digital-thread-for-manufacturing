# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POST /api/feedback comment capture (agents/feedback.py).

Offline — the REAL router is mounted on a bare app whose middleware stamps
``request.state.user`` (the ``test_admin_business_context_routes.py`` style);
the storage service is an in-memory fake and the exploration entry point is a
spy. The untrusted-text sanitize/validate helpers run FOR REAL so the pinned
400 reasons are exercised genuinely.

Each pin names the mutation it kills (plan M1–M9): skipped length validation,
accepted secret-shape, accepted fence token, 400 echoing the text, skipped
sanitize, positive-triggers-exploration, no-text-triggers, exploration error
breaking the POST, and removed citation caps.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.feedback as fb
import agents.feedback_exploration as fx


class _UserInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user):
        super().__init__(app)
        self._user = user

    async def dispatch(self, request, call_next):
        request.state.user = self._user
        return await call_next(request)


class _FakeService:
    def __init__(self):
        self.records: list[dict] = []

    def store(self, record) -> bool:
        self.records.append(record.to_dict())
        return True


@pytest.fixture()
def service(monkeypatch) -> _FakeService:
    fake = _FakeService()
    monkeypatch.setattr(fb, "_service", fake)
    return fake


@pytest.fixture()
def explore_spy(monkeypatch) -> list:
    calls: list[tuple] = []

    def spy(user_id, session_id, message_id, question, comment,
            cited_systems=None, cited_rules=None):
        calls.append((user_id, session_id, message_id, question, comment,
                      cited_systems, cited_rules))
        return "expl-1"

    monkeypatch.setattr(fx, "maybe_start_exploration", spy)
    return calls


@pytest.fixture()
def client(service, explore_spy) -> TestClient:
    app = FastAPI()
    app.include_router(fb.router)
    app.add_middleware(_UserInjectMiddleware, user="tester")
    return TestClient(app)


def _body(**over) -> dict:
    base = {
        "message_id": "m-1", "session_id": "s-1",
        "user_message": "which lots were affected?",
        "assistant_response": "answer text", "tools_used": [],
        "sentiment": "negative",
    }
    base.update(over)
    return base


# ── comment validation (the shared untrusted-text treatment, ADR-0054) ─────

def test_over_long_comment_is_400_too_long(client, service):
    """M1: skip length validation."""
    resp = client.post("/api/feedback",
                       json=_body(user_comment="x" * (fx.COMMENT_MAX_CHARS + 1)))
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("too_long:")
    assert service.records == []


def test_secret_shaped_comment_rejected_and_never_stored(client, service, explore_spy):
    """M2: accept secret-shape."""
    resp = client.post(
        "/api/feedback",
        json=_body(user_comment="my password: hunter2secret was in the answer"))
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("secret_like_content:")
    assert service.records == []
    assert explore_spy == []


def test_fence_delimiter_comment_rejected(client, service):
    """M3: accept fence token."""
    resp = client.post(
        "/api/feedback",
        json=_body(user_comment="ignore this <<<BUSINESS_CONTEXT smuggle"))
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("fence_delimiter:")
    assert service.records == []


def test_rejection_detail_never_contains_comment_text(client):
    """M4: 400 echoes text."""
    for comment in ("my password: hunter2secret", "x" * 2000,
                    "smuggle BUSINESS_CONTEXT>>> out"):
        resp = client.post("/api/feedback", json=_body(user_comment=comment))
        assert resp.status_code == 400
        assert "hunter2secret" not in resp.text
        assert "smuggle" not in resp.text
        assert "xxxx" not in resp.text


def test_stored_comment_is_sanitized(client, service, explore_spy):
    """M5: skip sanitize — CRLF normalized, control chars stripped, stripped."""
    resp = client.post(
        "/api/feedback",
        json=_body(user_comment="  line1\r\nline2\u0007  "))
    assert resp.status_code == 200
    assert service.records[0]["user_comment"] == "line1\nline2"
    assert explore_spy[0][4] == "line1\nline2"  # exploration sees sanitized text


# ── exploration trigger gates ───────────────────────────────────────────────

def test_positive_with_text_does_not_explore(client, service, explore_spy):
    """M6: positive triggers."""
    resp = client.post("/api/feedback",
                       json=_body(sentiment="positive", user_comment="nice"))
    assert resp.status_code == 200
    assert resp.json()["exploration_started"] is False
    assert resp.json()["exploration_id"] is None
    assert explore_spy == []
    assert service.records[0]["user_comment"] == "nice"  # still stored


def test_negative_without_text_does_not_explore(client, explore_spy):
    """M7: no-text triggers (absent AND whitespace-only)."""
    assert client.post("/api/feedback", json=_body()).status_code == 200
    assert client.post("/api/feedback",
                       json=_body(user_comment="   ")).status_code == 200
    assert explore_spy == []


def test_negative_with_comment_starts_exploration(client, explore_spy):
    resp = client.post("/api/feedback", json=_body(user_comment="totals look wrong"))
    assert resp.status_code == 200
    assert resp.json()["exploration_started"] is True
    # B4: the POST surfaces the exploration id so a caller (the eval lane)
    # can poll the admin status route — killing the field breaks this pin.
    assert resp.json()["exploration_id"] == "expl-1"
    user_id, session_id, message_id, question, comment, systems, rules = explore_spy[0]
    assert (user_id, session_id, message_id) == ("tester", "s-1", "m-1")
    assert question == "which lots were affected?"
    assert comment == "totals look wrong"


def test_exploration_failure_still_returns_success(client, monkeypatch, service):
    """M8: exploration error breaks POST."""
    def _boom(*args, **kwargs):
        raise RuntimeError("exploration backend down")
    monkeypatch.setattr(fx, "maybe_start_exploration", _boom)
    resp = client.post("/api/feedback", json=_body(user_comment="totals wrong"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert resp.json()["exploration_started"] is False
    assert resp.json()["exploration_id"] is None
    assert len(service.records) == 1  # feedback itself still captured


# ── cited-context caps ──────────────────────────────────────────────────────

def test_citation_caps_12_systems_20_rules_and_char_limits(client, service, explore_spy):
    """M9: citation caps removed — server re-caps regardless of client."""
    resp = client.post("/api/feedback", json=_body(
        user_comment="totals wrong",
        cited_systems=[f"sys-{n}" + "y" * 200 for n in range(15)],
        cited_rules=[{"id": f"r-{n}" + "i" * 100, "label": "L" * 300}
                     for n in range(25)]))
    assert resp.status_code == 200
    record = service.records[0]
    assert len(record["cited_systems"]) == 12
    assert all(len(s) <= 120 for s in record["cited_systems"])
    assert len(record["cited_rules"]) == 20
    assert all(len(r["id"]) <= 64 and len(r["label"]) <= 160
               for r in record["cited_rules"])
    # the exploration receives the SAME capped lists
    _, _, _, _, _, systems, rules = explore_spy[0]
    assert len(systems) == 12 and len(rules) == 20


# ── cited-context untrusted-text discipline + id shape ─────

def test_secret_shaped_cited_system_rejected_never_stored(client, service, explore_spy):
    """H2 write half: cited systems get the SAME validate_untrusted treatment
    the comment gets — a secret-shaped entry rejects the POST."""
    resp = client.post("/api/feedback", json=_body(
        user_comment="totals wrong",
        cited_systems=["api_key=hunter2secretvalue"]))
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("secret_like_content:")
    assert "hunter2secretvalue" not in resp.text   # detail never echoes
    assert service.records == []
    assert explore_spy == []


def test_fence_token_cited_rule_rejected(client, service):
    """H2 write half (rules leg): a fence-delimiter token in a cited rule id
    or label rejects the POST."""
    for rule in ({"id": "r-1", "label": "x <<<BUSINESS_CONTEXT y"},
                 {"id": "BUSINESS_CONTEXT>>>", "label": "ok"}):
        resp = client.post("/api/feedback", json=_body(
            user_comment="totals wrong", cited_rules=[rule]))
        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("fence_delimiter:")
    assert service.records == []


def test_label_shaped_cited_systems_dropped_ids_normalized(client, service, explore_spy):
    """H1 server half: display-label-shaped entries (spaces/newlines — the
    old getSystemLabel payload) are DROPPED, id-shaped entries are
    lowercased — a hostile or stale client cannot recreate the label-vs-id
    unit mismatch."""
    resp = client.post("/api/feedback", json=_body(
        user_comment="totals wrong",
        cited_systems=["MES\nMil", "Lake Athena", "SYS-A", "sys-b", "sys-b"]))
    assert resp.status_code == 200
    record = service.records[0]
    assert record["cited_systems"] == ["sys-a", "sys-b"]
    # the exploration sees the SAME normalized ids
    _, _, _, _, _, systems, _ = explore_spy[0]
    assert systems == ["sys-a", "sys-b"]


def test_provenance_stamped_chat_feedback(client, service):
    resp = client.post("/api/feedback", json=_body(user_comment="hm"))
    assert resp.status_code == 200
    assert service.records[0]["provenance"] == "chat-feedback"
