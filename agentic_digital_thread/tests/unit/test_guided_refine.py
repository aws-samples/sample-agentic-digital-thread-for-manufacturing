# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POST /api/onboard/guided/refine — the AI-assisted business-context editor.

The load-bearing assertions are the SAFETY ones: the endpoint never writes
anything (the only write path stays the admin PUT), untrusted steward text is
fenced in the composed prompt, the draft is bounded server-side, and a model
failure is an honest 502 — never a fabricated draft.

The LLM seam is ``agents.db.bedrock_call`` — the same seam the interview
clarifier uses and its tests mock.
"""
import json

import pytest
from fastapi.testclient import TestClient

from agents.context_interview import state
from agents.context_interview.refine import (MAX_DRAFT_LEN,
                                             MAX_INSTRUCTION_LEN,
                                             truncate_to_sentence)

REFINE_URL = "/api/onboard/guided/refine"


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_for_tests()
    yield
    state.reset_for_tests()


@pytest.fixture
def client(monkeypatch, local_auth_env):
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    from agents.server import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _known_systems(monkeypatch):
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"mes-1"})


def _stub_llm(monkeypatch, reply=None, exc=None, prompts=None):
    def fake(prompt, timeout=90, model=None):
        if prompts is not None:
            prompts.append(prompt)
        if exc is not None:
            raise exc
        return reply
    monkeypatch.setattr("agents.db.bedrock_call", fake, raising=False)


def _body(**over):
    payload = {"system_id": "mes-1",
               "instruction": "make the failure-code guidance clearer",
               "current_text": "Tracks work orders. Failure codes vary."}
    payload.update(over)
    return payload


GOOD_REPLY = json.dumps({
    "draft": "Tracks work orders. Failure codes starting with W are routine "
             "workflow outcomes and are excluded from failure counts.",
    "notes": "Clarified failure codes; KPI definitions remain unaddressed.",
})


# --------------------------------------------------------------------------- #
# Validation gates
# --------------------------------------------------------------------------- #
def test_unknown_system_is_a_404(client):
    r = client.post(REFINE_URL, json=_body(system_id="ghost"))
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown_system"


def test_missing_system_id_is_a_404(client):
    assert client.post(REFINE_URL, json=_body(system_id="")).status_code == 404


def test_oversize_instruction_is_a_400_and_never_reaches_the_model(client, monkeypatch):
    prompts = []
    _stub_llm(monkeypatch, reply=GOOD_REPLY, prompts=prompts)
    r = client.post(REFINE_URL,
                    json=_body(instruction="x" * (MAX_INSTRUCTION_LEN + 1)))
    assert r.status_code == 400
    assert f"exceeds {MAX_INSTRUCTION_LEN} chars" in r.json()["detail"]
    assert prompts == []


def test_empty_instruction_is_a_400(client):
    r = client.post(REFINE_URL, json=_body(instruction="   "))
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_instruction"


def test_secret_shaped_instruction_is_rejected_without_echo(client, monkeypatch):
    prompts = []
    _stub_llm(monkeypatch, reply=GOOD_REPLY, prompts=prompts)
    r = client.post(REFINE_URL,
                    json=_body(instruction="use creds AKIAIOSFODNN7EXAMPLE ok"))
    assert r.status_code == 400
    assert "secret-shaped" in r.json()["detail"]
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert prompts == []


def test_secret_shaped_current_text_is_rejected(client, monkeypatch):
    """A pasted credential must not be pumped into an LLM prompt either."""
    prompts = []
    _stub_llm(monkeypatch, reply=GOOD_REPLY, prompts=prompts)
    r = client.post(REFINE_URL,
                    json=_body(current_text="password: hunter2please"))
    assert r.status_code == 400
    assert prompts == []


# --------------------------------------------------------------------------- #
# Prompt composition: untrusted text is FENCED, topics come from the bank
# --------------------------------------------------------------------------- #
def test_composed_prompt_fences_both_untrusted_inputs(client, monkeypatch):
    from agents.untrusted_text import FENCE_CLOSE, FENCE_OPEN
    prompts = []
    _stub_llm(monkeypatch, reply=GOOD_REPLY, prompts=prompts)
    r = client.post(REFINE_URL, json=_body())
    assert r.status_code == 200
    assert len(prompts) == 1
    prompt = prompts[0]
    # Current text rides the SAME business-context fence idiom, origin-labeled.
    assert FENCE_OPEN in prompt and FENCE_CLOSE in prompt
    assert prompt.index(FENCE_OPEN) < prompt.index("Tracks work orders.")
    assert prompt.index("Tracks work orders.") < prompt.index(FENCE_CLOSE)
    assert "business_context:mes-1" in prompt
    # The instruction rides the clarifier's marker idiom.
    assert ("---BEGIN REVISION REQUEST---" in prompt
            and "---END REVISION REQUEST---" in prompt)
    assert "make the failure-code guidance clearer" in prompt


def test_prompt_carries_the_intake_topics_from_the_one_bank(client, monkeypatch):
    """Topic text is REUSED from questions.py, never duplicated — every
    question's prompt appears verbatim."""
    from agents.context_interview.questions import QUESTIONS
    prompts = []
    _stub_llm(monkeypatch, reply=GOOD_REPLY, prompts=prompts)
    client.post(REFINE_URL, json=_body())
    for q in QUESTIONS:
        assert q.prompt in prompts[0], q.id


def test_empty_current_text_is_allowed(client, monkeypatch):
    prompts = []
    _stub_llm(monkeypatch, reply=GOOD_REPLY, prompts=prompts)
    r = client.post(REFINE_URL, json=_body(current_text=""))
    assert r.status_code == 200
    assert "no current context text" in prompts[0]


# --------------------------------------------------------------------------- #
# Response contract: bounded draft, honest failure, NEVER a write
# --------------------------------------------------------------------------- #
def test_happy_path_returns_draft_and_notes(client, monkeypatch):
    _stub_llm(monkeypatch, reply=GOOD_REPLY)
    body = client.post(REFINE_URL, json=_body()).json()
    assert set(body) == {"draft", "notes"}
    assert body["draft"].startswith("Tracks work orders.")
    assert "KPI definitions remain unaddressed" in body["notes"]


def test_overlong_draft_is_bounded_at_a_sentence_boundary(client, monkeypatch):
    sentence = "This sentence is exactly forty chars ok. "
    long_draft = (sentence * 80).strip()  # ~3300 chars
    _stub_llm(monkeypatch, reply=json.dumps({"draft": long_draft, "notes": "n"}))
    body = client.post(REFINE_URL, json=_body()).json()
    assert len(body["draft"]) <= MAX_DRAFT_LEN
    assert body["draft"].endswith("ok.")  # sentence boundary, not a hard cut


def test_llm_failure_is_an_honest_502_never_a_fabricated_draft(client, monkeypatch):
    _stub_llm(monkeypatch, exc=RuntimeError("bedrock down SECRETVALUE"))
    r = client.post(REFINE_URL, json=_body())
    assert r.status_code == 502
    assert "unavailable" in r.json()["detail"]
    assert "draft" not in r.json()
    # Category only — the exception text must not be echoed.
    assert "SECRETVALUE" not in r.text


def test_malformed_model_output_is_a_502(client, monkeypatch):
    _stub_llm(monkeypatch, reply="I think you should just write it yourself.")
    assert client.post(REFINE_URL, json=_body()).status_code == 502


def test_empty_draft_in_model_output_is_a_502(client, monkeypatch):
    _stub_llm(monkeypatch, reply=json.dumps({"draft": "  ", "notes": "n"}))
    assert client.post(REFINE_URL, json=_body()).status_code == 502


def test_refine_never_writes_business_context_or_state(client, monkeypatch):
    """The ONLY write path stays the admin PUT. Any write here is the bug."""
    def bomb(*a, **k):
        raise AssertionError("refine must never write business context")

    monkeypatch.setattr("agents.business_context.write_business_context", bomb)
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping", bomb)
    _stub_llm(monkeypatch, reply=GOOD_REPLY)
    assert client.post(REFINE_URL, json=_body()).status_code == 200
    assert state.draft_context() == ""
    assert state.landings() == []


def test_fence_tokens_in_the_draft_are_neutralized(client, monkeypatch):
    """A model-emitted fence delimiter must not make the eventual PUT fail."""
    from agents.untrusted_text import FENCE_OPEN, validate_untrusted
    _stub_llm(monkeypatch, reply=json.dumps(
        {"draft": f"Good text. {FENCE_OPEN} smuggled.", "notes": ""}))
    body = client.post(REFINE_URL, json=_body()).json()
    assert FENCE_OPEN not in body["draft"]
    assert validate_untrusted(body["draft"]) is None


def test_whole_line_fence_artifacts_are_dropped_from_the_draft(
        client, monkeypatch):
    """Observed live (2026-08-18): the model wrapped its draft in copies of the
    prompt's fence delimiters. Neutralization is correct but a line that is
    ONLY the replacement marker is an artifact, not steward content — it must
    not reach the textarea. Embedded markers inside real prose still survive
    (the safety property)."""
    from agents.untrusted_text import (FENCE_OPEN, FENCE_TOKEN_REPLACEMENT,
                                       validate_untrusted)
    _stub_llm(monkeypatch, reply=json.dumps(
        {"draft": f"{FENCE_OPEN}\nSegments are the unit of work.\n{FENCE_OPEN}",
         "notes": "tightened"}))
    draft = client.post(REFINE_URL, json=_body()).json()["draft"]
    assert draft == "Segments are the unit of work."
    assert FENCE_TOKEN_REPLACEMENT not in draft
    assert validate_untrusted(draft) is None

    # Embedded (mid-prose) tokens are still neutralized, never dropped silently.
    _stub_llm(monkeypatch, reply=json.dumps(
        {"draft": f"Real prose {FENCE_OPEN} continues here.", "notes": ""}))
    embedded = client.post(REFINE_URL, json=_body()).json()["draft"]
    assert FENCE_TOKEN_REPLACEMENT in embedded
    assert "Real prose" in embedded and "continues here." in embedded


# --------------------------------------------------------------------------- #
# truncate_to_sentence — pure helper pins
# --------------------------------------------------------------------------- #
def test_truncate_short_text_is_untouched():
    assert truncate_to_sentence("Short. Text.", 100) == "Short. Text."


def test_truncate_prefers_the_last_sentence_end_inside_the_cap():
    text = "One. Two. Three thousand words follow here"
    assert truncate_to_sentence(text, 20) == "One. Two."


def test_truncate_falls_back_to_whitespace_then_hard_cut():
    assert truncate_to_sentence("no sentence ends here at all", 10) == "no"
    assert truncate_to_sentence("x" * 50, 10) == "x" * 10
