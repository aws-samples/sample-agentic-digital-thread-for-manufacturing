# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3.2 — the bounded clarifier + the review manifest.

The load-bearing assertions are the BOUNDS, not the happy paths: the clarifier
must be depth-only (deterministic set untouched), capped server-side, and
degrade to silence on any model failure; the manifest must carry every
non-rejected landing and clear only on a fully-written confirm.
"""
import pytest
from fastapi.testclient import TestClient

from agents.context_interview import clarifier, state
from agents.context_interview.questions import QUESTIONS_BY_ID


@pytest.fixture(autouse=True)
def _clean():
    state.reset_for_tests()
    clarifier.reset_turns()
    yield
    state.reset_for_tests()
    clarifier.reset_turns()


@pytest.fixture
def client(monkeypatch, local_auth_env):
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    from agents.server import app
    return TestClient(app)


_Q = QUESTIONS_BY_ID["failure-taxonomy"]
_ANSWER = "codes A and B are real problems, everything else is workflow noise"


def _model_says(monkeypatch, raw):
    monkeypatch.setattr("agents.db.bedrock_call", lambda *a, **k: raw,
                        raising=False)


# --------------------------------------------------------------------------- #
# Clarifier gates (deterministic, checked BEFORE any model call)
# --------------------------------------------------------------------------- #
def test_no_goal_never_clarifies(monkeypatch):
    from dataclasses import replace
    called = []
    monkeypatch.setattr("agents.db.bedrock_call",
                        lambda *a, **k: called.append(1), raising=False)
    res = clarifier.clarify(replace(_Q, goal=""), _ANSWER)
    assert res["follow_up"] is None
    assert called == []  # the gate must run before the model


def test_short_or_empty_answer_never_clarifies(monkeypatch):
    called = []
    monkeypatch.setattr("agents.db.bedrock_call",
                        lambda *a, **k: called.append(1), raising=False)
    assert clarifier.clarify(_Q, "")["follow_up"] is None
    assert clarifier.clarify(_Q, "n/a")["follow_up"] is None
    assert called == []


def test_model_failure_degrades_to_no_follow_up(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bedrock down")
    monkeypatch.setattr("agents.db.bedrock_call", boom, raising=False)
    res = clarifier.clarify(_Q, _ANSWER)
    assert res == {"follow_up": None, "turns_left": clarifier.MAX_CLARIFIER_TURNS}


def test_malformed_model_output_degrades(monkeypatch):
    for raw in ("not json", '{"complete": "maybe"}', '{"follow_up": 42}', ""):
        _model_says(monkeypatch, raw)
        assert clarifier.clarify(_Q, _ANSWER)["follow_up"] is None


def test_complete_answer_gets_no_follow_up(monkeypatch):
    _model_says(monkeypatch, '{"complete": true}')
    assert clarifier.clarify(_Q, _ANSWER)["follow_up"] is None


def test_follow_up_is_returned_and_turns_counted(monkeypatch):
    _model_says(monkeypatch,
                '{"complete": false, "follow_up": "Which codes close retried work?"}')
    res = clarifier.clarify(_Q, _ANSWER, system_id="sysA")
    assert res["follow_up"] == "Which codes close retried work?"
    assert res["turns_left"] == clarifier.MAX_CLARIFIER_TURNS - 1
    # A follow-up only counts against ITS (system, question) pair.
    assert clarifier.turns_used("sysB", _Q.id) == 0


def test_cap_is_enforced_server_side(monkeypatch):
    _model_says(monkeypatch, '{"complete": false, "follow_up": "And this one?"}')
    for _ in range(clarifier.MAX_CLARIFIER_TURNS):
        assert clarifier.clarify(_Q, _ANSWER, system_id="s")["follow_up"]
    capped = clarifier.clarify(_Q, _ANSWER, system_id="s")
    assert capped == {"follow_up": None, "turns_left": 0}


def test_oversized_follow_up_is_dropped(monkeypatch):
    long = "why " * 200
    _model_says(monkeypatch, f'{{"complete": false, "follow_up": "{long}"}}')
    assert clarifier.clarify(_Q, _ANSWER)["follow_up"] is None


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_clarify_route_unknown_question_is_400(client):
    r = client.post("/api/onboard/guided/clarify",
                    json={"question_id": "nope", "answer": _ANSWER})
    assert r.status_code == 400


def test_clarify_route_returns_follow_up(client, monkeypatch):
    _model_says(monkeypatch,
                '{"complete": false, "follow_up": "Which family answers what?"}')
    r = client.post("/api/onboard/guided/clarify",
                    json={"question_id": _Q.id, "answer": _ANSWER,
                          "system_id": "s1", "evidence": "column failure_code"})
    assert r.status_code == 200
    assert r.json()["follow_up"] == "Which family answers what?"


def test_manifest_carries_draft_and_landings(client):
    # A business-context question answered through the normal turn path…
    r = client.post("/api/onboard/guided/turn",
                    json={"question_id": "unit-of-work",
                          "answer": "one record is a discrete item on a route"})
    assert r.status_code == 200 and r.json()["outcome"] == "draft"
    m = client.get("/api/onboard/guided/manifest").json()
    assert "discrete item" in m["draft_business_context"]
    assert m["editable_later"] is True
    rows = m["landings"]
    assert len(rows) == 1
    row = rows[0]
    assert row["question_id"] == "unit-of-work"
    assert row["landing"] == "business_context_draft"
    assert row["outcome"] == "draft"
    assert "discrete item" in row["text"]


def test_rejected_answers_never_reach_the_manifest(client):
    # Secret-shaped content is REJECTED at landing; the manifest must not
    # carry any trace of it.
    r = client.post("/api/onboard/guided/turn",
                    json={"question_id": "unit-of-work",
                          "answer": "password: hunter2hunter2"})
    assert r.json()["outcome"] == "rejected"
    m = client.get("/api/onboard/guided/manifest").json()
    assert m["landings"] == []
    assert "hunter2" not in str(m)


def test_manifest_clears_only_on_fully_written_confirm(client, monkeypatch):
    client.post("/api/onboard/guided/turn",
                json={"question_id": "unit-of-work",
                      "answer": "one record is a discrete item on a route"})
    # Confirm with a write that FAILS: the manifest (and draft) must survive.
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda *a, **k: False, raising=False)
    r = client.post("/api/onboard/guided/confirm-context",
                    json={"systems": ["sys1"]})
    assert r.json()["status"] == "partial"
    assert client.get("/api/onboard/guided/manifest").json()["landings"]
    # Now a write that succeeds: the session manifest is consumed.
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda *a, **k: True, raising=False)
    monkeypatch.setattr(
        "agents.context_interview.routes._known_system_ids",
        lambda: set(), raising=False)
    r = client.post("/api/onboard/guided/confirm-context",
                    json={"systems": ["sys1"]})
    assert r.json()["status"] == "ok"
    m = client.get("/api/onboard/guided/manifest").json()
    assert m["landings"] == [] and m["draft_business_context"] == ""
