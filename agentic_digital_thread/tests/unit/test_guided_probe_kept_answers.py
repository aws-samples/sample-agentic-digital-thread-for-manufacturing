# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The wizard's pre-map questions ask only what an answer can keep.

With the probe's 422 fixed, the wizard's pre-map card asks its selected
questions, so it must not tell the steward the answers shape the mapping.
None does: a business-context answer is an in-memory draft
that only ``/guided/confirm-context`` writes, and that write
(``business_context.write_business_context``, ``MATCH (s:System) SET``) needs
a System node, which a system has only after its first provision (a removed
system's node is deleted). The other answers are filed as proposals at once,
for review. So the wizard declares ``durable_answers_only``: the server asks
it only questions whose answer is filed when it is given, refuses a held one,
and the card says where answers go.

The schema is unlike the seed (other table and column names).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.context_interview import state

SYSTEM = "plant-x-upkeep"
TABLES = [
    {"name": "jobs", "row_count": 40, "columns": [
        {"name": "fault_code", "data_type": "text"},
        {"name": "unit_tag", "data_type": "text"},
        {"name": "serial_no", "data_type": "text"},
        {"name": "job_state", "data_type": "text"},
        {"name": "retry_seq", "data_type": "integer"}]},
]


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_for_tests()
    yield
    state.reset_for_tests()


@pytest.fixture
def client(monkeypatch):
    """The guided router alone (its access gate is the include site's and is
    covered by the guided route tests), so no app start-up runs."""
    from fastapi import FastAPI

    from agents.context_interview.routes import router
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    app = FastAPI()
    app.include_router(router, prefix="/api/onboard")
    return TestClient(app)


@pytest.fixture
def filed(monkeypatch):
    """The proposal store: every filed answer, as the lifecycle receives it."""
    rows: list[dict] = []

    def _file(**kwargs):
        rows.append(kwargs)
        return f"proposal-{len(rows)}"

    monkeypatch.setattr("agents.self_learning.log_proposed_mapping", _file)
    return rows


def _probe(client, *, durable: bool, answers=()):
    body = {"tables": TABLES, "answers": list(answers)}
    if durable:
        body["durable_answers_only"] = True
    response = client.post(f"/api/onboard/guided/probe/{SYSTEM}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _held_ids() -> set[str]:
    from agents.context_interview.landing import HELD_FOR_CONFIRM
    from agents.context_interview.questions import QUESTIONS
    return {q.id for q in QUESTIONS if q.landing in HELD_FOR_CONFIRM}


def test_the_wizard_is_asked_no_question_whose_answer_is_only_held(client):
    asked = {q["id"] for q in _probe(client, durable=True)["questions"]}
    assert asked, "the schema should still trigger a kept question"
    assert not asked & _held_ids(), asked
    assert "identifier-family" in asked


def test_the_chat_card_is_still_asked_the_held_questions(client):
    """The chat flow's card has a confirm step, so it keeps every question."""
    asked = {q["id"] for q in _probe(client, durable=False)["questions"]}
    assert asked & _held_ids(), asked


def test_a_wizard_answer_is_filed_as_a_proposal_for_the_system(client, filed):
    """The intended effect, end to end through the route: the answer reaches
    the proposal lifecycle, named for the system, and nothing is held."""
    body = _probe(client, durable=True, answers=[
        {"question_id": "identifier-family", "answer": "Unit tags are <SITE>-<UNIT>-<NN>."}])
    (result,) = body["results"]
    assert result["outcome"] == "proposed" and result["proposal_ids"] == ["proposal-1"]
    (row,) = filed
    assert row["from_system"] == SYSTEM
    assert row["description"] == "Unit tags are <SITE>-<UNIT>-<NN>."
    assert row["proposed_type"] == "value_mapping"
    assert row["query_context"] == "context-interview:identifier-family"
    assert state.draft_context() == ""
    asked = {q["id"] for q in body["questions"]}
    assert "identifier-family" not in asked


def test_a_held_answer_sent_by_the_wizard_is_refused_and_not_held(client, filed):
    (held, *_rest) = sorted(_held_ids())
    body = _probe(client, durable=True, answers=[{"question_id": held, "answer": "A real answer."}])
    (result,) = body["results"]
    assert result["outcome"] == "rejected"
    assert result["reason"] == "not_kept_here"
    assert state.draft_context() == ""
    assert filed == []
    assert held not in state.answered_topics(SYSTEM)


def test_the_chat_card_still_holds_a_business_context_answer(client):
    (held, *_rest) = sorted(_held_ids())
    body = _probe(client, durable=False, answers=[{"question_id": held, "answer": "A real answer."}])
    assert body["results"][0]["outcome"] == "draft"
    assert "A real answer." in state.draft_context()


# --------------------------------------------------------------------------- #
# an answer the proposal lifecycle did not file
# --------------------------------------------------------------------------- #

@pytest.fixture
def not_filed(monkeypatch):
    """The lifecycle gates or stages every proposal (it returns no id)."""
    calls: list[dict] = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or None)
    return calls


@pytest.mark.parametrize("durable", [True, False], ids=["wizard", "chat-card"])
def test_an_answer_that_was_not_filed_is_refused_and_asked_again(client, not_filed, durable):
    """A nonempty answer the lifecycle did not file must not come back
    ``skipped`` with its topic recorded (a reload would hide the question and
    the chat card would move on). It is ``rejected`` with ``not_ingested``, its
    topic is not answered, and the question is still listed."""
    body = _probe(client, durable=durable, answers=[
        {"question_id": "identifier-family", "answer": "Unit tags are <SITE>-<UNIT>-<NN>."}])
    (result,) = body["results"]
    assert result["outcome"] == "rejected"
    assert result["reason"] == "not_ingested"
    assert result["detail"]
    assert len(not_filed) == 1
    assert "identifier-family" not in state.answered_topics(SYSTEM)
    assert "identifier-family" in {q["id"] for q in body["questions"]}
    assert not any(e["question_id"] == "identifier-family" for e in state.landings())


def test_an_i_dont_know_skip_is_still_answered(client, not_filed):
    """An empty answer is the steward's own skip: answered, not asked again,
    and nothing reaches the lifecycle."""
    body = _probe(client, durable=True, answers=[
        {"question_id": "identifier-family", "answer": ""}])
    (result,) = body["results"]
    assert result["outcome"] == "skipped"
    assert "identifier-family" in state.answered_topics(SYSTEM)
    assert "identifier-family" not in {q["id"] for q in body["questions"]}
    assert not_filed == []


# --------------------------------------------------------------------------- #
# a DERIVED answer the proposal lifecycle did not file
# --------------------------------------------------------------------------- #

LARGE = [{"name": "readings", "row_count": 5_000_000,
          "columns": [{"name": "reading_value", "data_type": "double precision"}]}]


def _probe_large(client):
    response = client.post(f"/api/onboard/guided/probe/{SYSTEM}",
                           json={"tables": LARGE, "answers": [], "durable_answers_only": True})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_derived_answer_that_was_not_filed_is_not_noted_and_is_retried(client, not_filed):
    """An unfiled derivation must not come back ``skipped/not_ingested`` with
    its topic recorded (no later probe would retry it) and its text returned
    (both cards would show it as noted). It is ``rejected/not_ingested`` with no text and no topic, and
    the next probe derives it again."""
    body = _probe_large(client)
    (entry,) = [d for d in body["derived"] if d["question_id"] == "query-strategy"]
    assert entry["outcome"] == "rejected" and entry["reason"] == "not_ingested"
    assert "derived_text" not in entry and "evidence" not in entry
    assert "query-strategy" not in state.answered_topics(SYSTEM)
    assert not any(e["question_id"] == "query-strategy" for e in state.landings())
    assert len(not_filed) == 1
    again = _probe_large(client)
    assert [d["question_id"] for d in again["derived"]] == ["query-strategy"]
    assert len(not_filed) == 2


def test_a_filed_derived_answer_is_still_noted_once(client, filed):
    body = _probe_large(client)
    (entry,) = body["derived"]
    assert entry["outcome"] == "proposed" and entry["derived_text"]
    assert "query-strategy" in state.answered_topics(SYSTEM)
    assert _probe_large(client)["derived"] == []
    assert len(filed) == 1
