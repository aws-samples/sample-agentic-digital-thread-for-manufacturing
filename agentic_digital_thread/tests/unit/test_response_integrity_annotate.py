# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0047 annotate mode: text-vs-observations verification + fabrication block."""
from __future__ import annotations

import json

from agents.config import response_integrity_annotated, response_integrity_mode
from agents.response_integrity import capture_trusted_tool_result
from agents.response_integrity_annotate import (
    _factual_tokens, annotate_answer, grounding_report,
)
from agents.response_integrity_capture import initialize_integrity_state

_SCHEMA = {"segments": {"relation_kind": "base_table", "columns": {
    "segment_id": "varchar", "cell": "varchar", "state": "varchar",
    "cnt": "bigint",
}}}


def _state_with_observations(question: str = "how many segments per cell?") -> dict:
    invocation_state: dict = {}
    initialize_integrity_state(invocation_state, question)
    payload = {
        "columns": ["cell", "state", "cnt"],
        "rows": [["NRT6", "RUN", 42], ["BWU3", "IDLE", 7]],
        "complete": True,
    }
    result = {"toolUseId": "t1", "status": "success",
              "content": [{"text": json.dumps(payload)}]}
    capture_trusted_tool_result(
        result, invocation_state, system_id="mes", boundary="external",
        tool_name="run_query",
        executed_query="SELECT cell, state, cnt FROM segments",
        physical_schema=_SCHEMA,
    )
    return invocation_state


def _empty_state(question: str = "how many segments per cell?") -> dict:
    invocation_state: dict = {}
    initialize_integrity_state(invocation_state, question)
    return invocation_state


# ── token extraction ─────────────────────────────────────────────────────────

def test_factual_tokens_extracts_numbers_and_identifiers():
    numbers, identifiers = _factual_tokens(
        "Cell NRT6 produced 1,234 units (93.5% yield) on line L7.", "")
    assert "1,234" in numbers and "93.5" in numbers
    assert "NRT6" in identifiers and "L7" in identifiers


def test_factual_tokens_excludes_question_echoes_units_and_trivia():
    numbers, identifiers = _factual_tokens(
        "Over the last 7 days there was 1 event in a 24h window.",
        "show events for the last 7 days")
    assert "7" not in numbers          # question echo
    assert "1" not in numbers          # trivial
    assert "24h" not in identifiers    # duration prose
    assert not identifiers


def test_factual_tokens_ignores_list_markers_and_word_fragments():
    numbers, _ = _factual_tokens("1. First point\n2. Second point about ABC123", "")
    assert numbers == []


# ── grounding report ─────────────────────────────────────────────────────────

def test_grounding_verifies_numbers_and_identifiers_against_observations():
    state = _state_with_observations()
    report = grounding_report(
        "NRT6 has 42 segments in RUN state; BWU3 has 7.", "how many?", state)
    assert report["observations"] == 6
    assert report["checked"] >= 4
    assert report["verified"] >= 4
    assert report["score"] and report["score"] >= 0.8


def test_grounding_flags_invented_values_without_blocking():
    state = _state_with_observations()
    text = "NRT6 has 42 segments; total downtime was 999 minutes on line Z9."
    gated, report = annotate_answer(text, "how many?", state)
    assert gated == text                      # evidence exists -> pass through
    assert report["blocked"] is False
    assert "999" in report["unverified"]
    assert "Z9" in report["unverified"]


# ── fabrication block ────────────────────────────────────────────────────────

def test_zero_evidence_factual_answer_is_blocked():
    state = _empty_state()
    gated, report = annotate_answer(
        "Production totaled 5,412 units across NRT6 and BWU3.", "totals?", state)
    assert report["blocked"] is True
    payload = json.loads(gated)
    assert payload["status"] == "withheld"      # terminal, NOT the
    assert payload["retryable"] is False       # retryable vocabulary
    assert "FABRICATION_BLOCKED" in payload["reason_codes"]
    assert "NO_CURRENT_INVOCATION_EVIDENCE" in payload["reason_codes"]
    assert payload["claims"] == [] and payload["evidence"] == []


def test_zero_evidence_nonfactual_answer_passes_through():
    state = _empty_state()
    text = "No matching records were found for that filter."
    gated, report = annotate_answer(text, "totals?", state)
    assert gated == text
    assert report["blocked"] is False
    assert report["checked"] == 0


def test_seen_but_unprovable_payload_never_blocks():
    """A tool returned rows the proof could not admit (e.g. empty schema):
    capture holds zero observations but the payload WAS seen — annotate must
    not misread capture limits as fabrication."""
    state = _empty_state()
    payload = {"columns": ["cell", "cnt"], "rows": [["NRT6", 42]], "complete": True}
    result = {"toolUseId": "t1", "status": "success",
              "content": [{"text": json.dumps(payload)}]}
    capture_trusted_tool_result(
        result, state, system_id="mes", boundary="external",
        tool_name="run_query", executed_query="SELECT weird expr FROM x",
        physical_schema={},
    )
    text = "NRT6 has 42 segments."
    gated, report = annotate_answer(text, "how many?", state)
    assert report["observations"] == 0        # proof rejected everything
    assert report["payload_seen"] is True
    assert report["blocked"] is False
    assert gated == text


# ── config routing ───────────────────────────────────────────────────────────

def test_mode_flag_three_values(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    assert response_integrity_mode() == "annotate"
    assert response_integrity_annotated() is True
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "ENFORCE")
    assert response_integrity_mode() == "enforce"
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "banana")
    assert response_integrity_mode() == "report-only"
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE")
    assert response_integrity_mode() == "report-only"
    assert response_integrity_annotated() is False


# ── Mode A wiring ────────────────────────────────────────────────────────────

def test_invoke_mode_a_annotate_blocks_zero_evidence(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    from agents import system_agent

    class _Result:
        message = {"content": [{"text": "There are 5,412 segments on NRT6."}]}

    def fake_agent(question, **kwargs):
        # No tool ran — invocation_state keeps zero observations.
        return _Result()

    out = system_agent._invoke_mode_a_agent(fake_agent, "how many segments?")
    payload = json.loads(out)
    assert payload["status"] == "withheld"      # terminal, NOT the
    assert payload["retryable"] is False       # retryable vocabulary
    assert "FABRICATION_BLOCKED" in payload["reason_codes"]


def test_invoke_mode_a_annotate_passes_grounded_text(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    from agents import system_agent

    class _Result:
        message = {"content": [{"text": "No records matched the requested filter."}]}

    def fake_agent(question, **kwargs):
        return _Result()

    out = system_agent._invoke_mode_a_agent(fake_agent, "how many segments?")
    assert out == "No records matched the requested filter."
