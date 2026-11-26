# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An interview answer filed as guidance carries the answer's text.

A steward answered "I don't know" to one pre-map question and typed an answer
to the next. The typed one was filed as a pending ``guidance_note``, and the
review card for it read "No note text supplied.": the text was lost.

The ``guidance_note`` item contract (``self_learning``: ``from_system`` = system
id, ``to_value`` = note text) is what every guidance reader uses: the review
card, the delivery preview, acceptance, reconciliation, retire, and the
canonical key that keeps distinct notes apart. The interview landing wrote the
answer only to ``description`` and left ``to_value`` empty, so the card showed
no text, acceptance had no note to deliver, and every interview note on one
system shared one subject (a second note deduped into the first).

These tests file through the REAL ``log_proposed_mapping`` into an in-memory
proposal table. The schema and answers are unlike the seed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.context_interview import landing, state
from agents.context_interview.questions import (
    QUESTIONS,
    QUESTIONS_BY_ID,
    RUNTIME_QUESTION_TEMPLATES,
)
from tests.unit.test_vocab_lifecycle import FakeTable, _install

SYSTEM = "works-b-upkeep"
TABLES = [
    {"name": "tickets", "row_count": 25, "columns": [
        {"name": "ticket_no", "data_type": "text"},
        {"name": "asset_ref", "data_type": "text"},
        {"name": "opened_on", "data_type": "date"},
        {"name": "fault_kind", "data_type": "text"}],
     "sample_values": {"ticket_no": ["WB-14-002", "WB-14-003"]}},
]
TYPED = "People on the floor call asset_ref K-14 the Blue Press."

def _files_guidance(question) -> bool:
    return landing._PROPOSED_TYPE_BY_LANDING.get(question.landing) == "guidance_note"


#: Every bank question, and every runtime question shape, whose answer files
#: as a guidance note.
GUIDANCE_QUESTION_IDS = [q.id for q in QUESTIONS if _files_guidance(q)]
RUNTIME_GUIDANCE_SHAPES = [shape for shape, q in RUNTIME_QUESTION_TEMPLATES.items()
                           if _files_guidance(q)]


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_for_tests()
    yield
    state.reset_for_tests()


@pytest.fixture
def table(monkeypatch) -> FakeTable:
    fake = FakeTable()
    _install(monkeypatch, fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI

    from agents.context_interview.routes import router
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    app = FastAPI()
    app.include_router(router, prefix="/api/onboard")
    return TestClient(app)


def _rows_for(table: FakeTable, question_id: str) -> list[dict]:
    tag = f"context-interview:{question_id}"
    return [r for r in table.all_items() if r.get("query_context") == tag]


def _probe(client, answers: list[dict]) -> dict:
    response = client.post(f"/api/onboard/guided/probe/{SYSTEM}", json={
        "tables": TABLES, "answers": answers, "durable_answers_only": True})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_guidance_question_sets_are_not_empty():
    assert len(GUIDANCE_QUESTION_IDS) >= 2, "too few bank guidance questions"
    assert RUNTIME_GUIDANCE_SHAPES, "no runtime shape files a guidance note"


@pytest.mark.parametrize("question_id", GUIDANCE_QUESTION_IDS)
def test_a_guidance_answer_is_filed_with_its_text(table, question_id):
    res = landing.land_answer(QUESTIONS_BY_ID[question_id], TYPED,
                              system_id=SYSTEM)
    assert res.outcome == landing.OUTCOME_PROPOSED
    row = table.by_id(res.proposal_ids[0])
    assert row["proposed_type"] == "guidance_note"
    assert row["from_system"] == SYSTEM
    assert row["to_value"] == TYPED


@pytest.mark.parametrize("shape", RUNTIME_GUIDANCE_SHAPES)
def test_a_runtime_guidance_answer_is_filed_with_its_text(table, shape):
    res = landing.land_answer(RUNTIME_QUESTION_TEMPLATES[shape], TYPED,
                              system_id=SYSTEM)
    assert table.by_id(res.proposal_ids[0])["to_value"] == TYPED


def test_a_derived_guidance_answer_is_filed_with_its_text(table):
    question = QUESTIONS_BY_ID[GUIDANCE_QUESTION_IDS[0]]
    res = landing.land_answer(question, TYPED, system_id=SYSTEM,
                              actor="derived")
    assert table.by_id(res.proposal_ids[0])["to_value"] == TYPED


def test_two_answers_on_one_system_are_two_notes(table):
    first, second = GUIDANCE_QUESTION_IDS[:2]
    a = landing.land_answer(QUESTIONS_BY_ID[first], TYPED, system_id=SYSTEM)
    b = landing.land_answer(QUESTIONS_BY_ID[second],
                            "Codes in fault_kind starting Z are safety stops.",
                            system_id=SYSTEM)
    assert a.proposal_ids != b.proposal_ids
    texts = {table.by_id(a.proposal_ids[0])["to_value"],
             table.by_id(b.proposal_ids[0])["to_value"]}
    assert texts == {TYPED, "Codes in fault_kind starting Z are safety stops."}


def test_a_skip_then_a_typed_probe_answer_keeps_the_typed_text(client, table):
    asked = [q["id"] for q in _probe(client, [])["questions"]]
    typed_id = next(i for i in asked if i in GUIDANCE_QUESTION_IDS)
    skipped_id = next(i for i in asked if i != typed_id)
    _probe(client, [{"question_id": skipped_id, "answer": ""}])
    body = _probe(client, [{"question_id": typed_id, "answer": TYPED}])
    assert body["results"][0]["outcome"] == "proposed"
    assert _rows_for(table, skipped_id) == []
    rows = _rows_for(table, typed_id)
    assert [r["to_value"] for r in rows] == [TYPED]
    assert rows[0]["status"] == "pending"
