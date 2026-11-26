# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests — the runtime steward-question answering surface (ADR-0059 D4).

``GET /api/onboard/guided/runtime-questions`` + ``POST …/runtime-turn`` in
``agents/context_interview/routes.py``. Fully offline: the proposal store is a
monkeypatched ``get_all_mappings`` / ``_get_table`` pair, and landing is
exercised both faked (outcome control) and REAL (``land_answer`` with a
captured ``log_proposed_mapping``) so the D4 contract — answers land through
the one machinery, nothing bespoke — is pinned end to end.

Load-bearing pins:

* GET shapes pending ``steward_question`` rows for the UI (id/system/shape/
  prompt/filed_at) and lists NOTHING else;
* POST relays landing outcomes verbatim — including rejection reasons — and
  only a LANDED (``proposed``) answer supersedes the question row;
* the supersede is STEWARD-WINS: conditional on ``pending`` AND machine
  source; a lost condition (or store fault) never fails the request, because
  the landed proposals are already durable;
* auth posture: both endpoints ride the SAME router the include site gates
  with ``require_onboard_access`` — a denying gate 401s them like siblings.
"""
import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.context_interview.routes as routes  # noqa: E402
from agents.context_interview.landing import LandingResult  # noqa: E402
from agents.context_interview.questions import (  # noqa: E402
    LAND_IDENTIFIER_SEMANTICS,
)


def _row(**over):
    row = {
        "mapping_id": "q-row-1",
        "discovered_at": "2026-08-07T00:00:00+00:00",
        "from_system": "sys-a",
        "reason": "empty_join",
        "to_description": "Queries joining sys-a and sys-b repeatedly return "
                          "no rows when matching on that field. …",
        "proposed_type": "steward_question",
        "status": "pending",
        "source": "agent",
        "concept": "that value",
    }
    row.update(over)
    return row


def _patch_mappings(monkeypatch, rows):
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "get_all_mappings", lambda: rows)


class FakeTable:
    """Records update_item kwargs; optionally raises."""

    def __init__(self, raise_exc=None):
        self.calls = []
        self.raise_exc = raise_exc

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc


def _patch_table(monkeypatch, table):
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "_get_table", lambda: table)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/onboard")
    return TestClient(app)


# ── GET /runtime-questions ───────────────────────────────────────────────────

def test_get_lists_pending_questions_shaped_for_ui(client, monkeypatch):
    _patch_mappings(monkeypatch, [
        _row(mapping_id="q-2", discovered_at="2026-08-07T02:00:00+00:00",
             reason="terminology_miss", from_system="sys-b",
             to_description="What does that term refer to…"),
        _row(),  # q-row-1, older
        # Noise: never listed.
        _row(mapping_id="q-3", status="superseded"),
        _row(mapping_id="q-4", status="accepted"),
        {"mapping_id": "vm-1", "status": "pending",
         "proposed_type": "value_mapping"},
        {},
    ])
    body = client.get("/api/onboard/guided/runtime-questions").json()
    assert [q["id"] for q in body["questions"]] == ["q-row-1", "q-2"]  # oldest first
    first = body["questions"][0]
    assert first == {
        "id": "q-row-1",
        "system": "sys-a",
        "shape": "empty_join",
        "prompt": _row()["to_description"],
        "filed_at": "2026-08-07T00:00:00+00:00",
    }
    second = body["questions"][1]
    assert second["shape"] == "terminology_miss"
    assert second["system"] == "sys-b"


def test_context_not_reflected_exposes_only_structured_bounded_terms(client, monkeypatch):
    """Promotion authority is structured data; prompt prose is display-only."""
    _patch_mappings(monkeypatch, [_row(
        reason="context_not_reflected", from_value="minutes", to_value="seconds",
        to_description="Narrative says use hours, never days.")])
    question = client.get(
        "/api/onboard/guided/runtime-questions").json()["questions"][0]
    assert question["requirement_terms"] == {
        "asserted": "minutes", "negated": "seconds"}

    _patch_mappings(monkeypatch, [_row(
        reason="context_not_reflected", from_value="", to_value="",
        to_description="Narrative says minutes, never seconds.")])
    question = client.get(
        "/api/onboard/guided/runtime-questions").json()["questions"][0]
    assert "requirement_terms" not in question


def test_get_empty_when_no_pending_questions(client, monkeypatch):
    _patch_mappings(monkeypatch, [
        {"mapping_id": "vm-1", "status": "pending",
         "proposed_type": "value_mapping"}])
    body = client.get("/api/onboard/guided/runtime-questions").json()
    assert body == {"questions": []}


# ── POST /runtime-turn — lookup errors ───────────────────────────────────────

def test_post_unknown_question_id_404(client, monkeypatch):
    _patch_mappings(monkeypatch, [_row()])
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "nope", "answer": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_question_id"


def test_post_non_pending_question_404(client, monkeypatch):
    """A superseded/accepted question is no longer answerable — same 404 as
    unknown (it is absent from the pending queue)."""
    _patch_mappings(monkeypatch, [_row(status="superseded")])
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1", "answer": "x"})
    assert resp.status_code == 404


def test_post_unmodeled_shape_400(client, monkeypatch):
    _patch_mappings(monkeypatch, [_row(reason="not_a_shape")])
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1", "answer": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unmodeled_question_shape"


# ── POST /runtime-turn — landing delegation (faked outcomes) ─────────────────

def _fake_landing(monkeypatch, result):
    captured = {}

    def fake(question, answer, *, system_id="", actor="interview",
             concept_id=""):
        captured.update(question=question, answer=answer,
                        system_id=system_id, concept_id=concept_id)
        return result
    monkeypatch.setattr(routes, "land_answer", fake)
    return captured


def test_post_landed_answer_relays_and_supersedes(client, monkeypatch):
    _patch_mappings(monkeypatch, [_row()])
    table = FakeTable()
    _patch_table(monkeypatch, table)
    captured = _fake_landing(monkeypatch, LandingResult(
        outcome="proposed", question_id="runtime-empty-join-q-row-1",
        landing=LAND_IDENTIFIER_SEMANTICS, proposal_ids=["p-1", "p-2"],
        clean_text="one side strips a prefix"))
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1",
                             "answer": "one side strips a prefix"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "proposed"
    assert body["proposal_ids"] == ["p-1", "p-2"]
    assert body["question_id"] == "q-row-1"  # the ROW id, not the composed id
    assert body["question_superseded"] is True
    # The recomposed Question: template landing + the ROW's prompt, and the
    # landing call carries the row's system/concept.
    q = captured["question"]
    assert q.landing == LAND_IDENTIFIER_SEMANTICS
    assert q.prompt == _row()["to_description"]
    assert captured["system_id"] == "sys-a"
    assert captured["concept_id"] == "that value"
    # Supersede mechanics: steward-wins conditional + answered stamps + ids.
    assert len(table.calls) == 1
    call = table.calls[0]
    assert call["Key"] == {"mapping_id": "q-row-1",
                           "discovered_at": "2026-08-07T00:00:00+00:00"}
    assert "superseded_reason" in call["UpdateExpression"]
    assert "answer_proposal_ids" in call["UpdateExpression"]
    vals = call["ExpressionAttributeValues"]
    assert vals[":why"] == "answered"
    assert vals[":pids"] == ["p-1", "p-2"]
    cond = call["ConditionExpression"]
    assert ":pending" in cond and ":agent" in cond and " AND " in cond


@pytest.mark.parametrize("outcome,extra", [
    ("rejected", {"reason": "secret_shape",
                  "detail": "the answer looks like a credential"}),
    ("skipped", {"reason": "not_ingested",
                 "detail": "the proposal was gated or staged upstream"}),
])
def test_post_unlanded_outcomes_relay_and_never_supersede(
        client, monkeypatch, outcome, extra):
    """A rejected answer is re-askable and a gated landing leaves the question
    pending — neither may touch the row (relayed reason included)."""
    _patch_mappings(monkeypatch, [_row()])
    table = FakeTable()
    _patch_table(monkeypatch, table)
    _fake_landing(monkeypatch, LandingResult(
        outcome=outcome, question_id="x",
        landing=LAND_IDENTIFIER_SEMANTICS, **extra))
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1", "answer": "whatever"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == outcome
    assert body["reason"] == extra["reason"]
    assert body["detail"] == extra["detail"]
    assert body["question_superseded"] is False
    assert table.calls == []


def test_post_steward_wins_lost_condition_reports_not_superseded(
        client, monkeypatch):
    """A steward who accepted/rejected the row first WINS: the conditional
    write loses, the request still succeeds (the answer landed durably)."""
    _patch_mappings(monkeypatch, [_row()])
    exc = type("ConditionalCheckFailedException", (Exception,), {})()
    _patch_table(monkeypatch, FakeTable(raise_exc=exc))
    _fake_landing(monkeypatch, LandingResult(
        outcome="proposed", question_id="x",
        landing=LAND_IDENTIFIER_SEMANTICS, proposal_ids=["p-1"]))
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1", "answer": "a"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_ids"] == ["p-1"]
    assert body["question_superseded"] is False


def test_post_supersede_store_fault_still_relays_landing(client, monkeypatch):
    _patch_mappings(monkeypatch, [_row()])
    _patch_table(monkeypatch, FakeTable(raise_exc=RuntimeError("throttled")))
    _fake_landing(monkeypatch, LandingResult(
        outcome="proposed", question_id="x",
        landing=LAND_IDENTIFIER_SEMANTICS, proposal_ids=["p-1"]))
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1", "answer": "a"})
    assert resp.status_code == 200
    assert resp.json()["question_superseded"] is False


# ── POST /runtime-turn — the REAL landing machinery (D4 pin) ─────────────────

def test_post_real_landing_files_through_log_proposed_mapping(
        client, monkeypatch):
    """No bespoke answer path: the REAL ``land_answer`` routes the answer into
    ``log_proposed_mapping`` as a steward-sourced pending proposal of the
    template's landing type, and the returned id supersedes the question."""
    import agents.self_learning as sl
    _patch_mappings(monkeypatch, [_row()])
    table = FakeTable()
    _patch_table(monkeypatch, table)
    filed = {}

    def fake_lpm(**kwargs):
        filed.update(kwargs)
        return "mid-77"
    monkeypatch.setattr(sl, "log_proposed_mapping", fake_lpm)
    resp = client.post(
        "/api/onboard/guided/runtime-turn",
        json={"question_id": "q-row-1",
              "answer": "the identifiers match after removing a site prefix"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "proposed"
    assert body["proposal_ids"] == ["mid-77"]
    assert body["question_superseded"] is True
    # empty_join lands on LAND_IDENTIFIER_SEMANTICS ⇒ value_mapping, steward.
    assert filed["proposed_type"] == "value_mapping"
    assert filed["source"] == "steward"
    assert filed["from_system"] == "sys-a"
    assert filed["description"] == (
        "the identifiers match after removing a site prefix")
    assert table.calls[0]["ExpressionAttributeValues"][":pids"] == ["mid-77"]


def test_post_blank_answer_is_a_skip_and_leaves_question_pending(
        client, monkeypatch):
    """REAL land_answer: an empty answer is a legitimate SKIP — relayed as
    such, no proposal filed, question untouched."""
    _patch_mappings(monkeypatch, [_row()])
    table = FakeTable()
    _patch_table(monkeypatch, table)
    resp = client.post("/api/onboard/guided/runtime-turn",
                       json={"question_id": "q-row-1", "answer": "   "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "skipped"
    assert body["question_superseded"] is False
    assert table.calls == []


def test_post_hostile_answer_rejected_by_real_sanitizer(client, monkeypatch):
    """REAL land_answer: secret-shaped text is rejected at write (ADR-0054)
    — the rejection reason is relayed, nothing is filed or superseded."""
    _patch_mappings(monkeypatch, [_row()])
    table = FakeTable()
    _patch_table(monkeypatch, table)
    resp = client.post(
        "/api/onboard/guided/runtime-turn",
        json={"question_id": "q-row-1",
              "answer": "password: hunter2please"})  # nosec B105 - offline test fixture
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "rejected"
    assert body["reason"] == "secret_like_content"
    assert body["question_superseded"] is False
    assert table.calls == []


# ── auth posture ─────────────────────────────────────────────────────────────

def test_new_routes_ride_the_gated_router():
    """Structural pin: both endpoints live on the SAME router the onboard
    include site wraps with ``require_onboard_access`` — that placement IS the
    auth contract (the router deliberately declares no dependency itself)."""
    paths = {r.path for r in routes.router.routes}
    assert "/guided/runtime-questions" in paths
    assert "/guided/runtime-turn" in paths


def test_denying_include_site_gate_yields_401_for_both(monkeypatch):
    """Mounted behind a denying gate (as onboard mounts it), both endpoints
    401 before any handler code runs — sibling posture."""
    def deny():
        raise HTTPException(status_code=401, detail="unauthorized")

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/onboard",
                       dependencies=[Depends(deny)])
    client = TestClient(app)
    assert client.get(
        "/api/onboard/guided/runtime-questions").status_code == 401
    assert client.post(
        "/api/onboard/guided/runtime-turn",
        json={"question_id": "q", "answer": "a"}).status_code == 401
