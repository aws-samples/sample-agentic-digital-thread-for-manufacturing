# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Core tests for deterministic structural response integrity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, FinalAnswerDraft, FreshnessClass,
    IntegrityContext, ReasonCode, SourceEvidence, SystemAnswerDraft,
    capture_orchestrator_tool_result, capture_trusted_tool_result,
    gate_final_result, render_final_answer, system_answer_from_result,
    validate_final_answer,
)

UTC = timezone.utc

def _provenance(payload):
    projections, schema = [], {}
    for index, column in enumerate(payload["columns"]):
        if column.startswith("__adt_source_"):
            source, data_type = f"observed_{index}", "timestamp"
        else:
            source = column
            values = [row[index] for row in payload["rows"] if index < len(row)]
            data_type = "varchar" if any(isinstance(value, str) for value in values) else "integer"
            if column in {"observed_at", "updated_at", "timestamp"}:
                data_type = "timestamp"
        schema[source] = data_type
        projections.append(f"{source} AS {column}")
    return {"executed_query": f"SELECT {', '.join(projections)} FROM records", "physical_schema": {"records": {"relation_kind": "base_table", "columns": schema}}}  # nosec B608 - test fixture composing expected SQL from module constants
NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _answer(value=10, *, observed=NOW, system="system-a", field="measure", current=False):
    payload = {
        "columns": [field, f"__adt_source_time__{field}"],
        "rows": [[value, observed.isoformat()]],
        "row_count": 1,
    }
    state = {}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state,
        system_id=system, boundary="sql", tool_name=f"query_{system}",
        current_intent=current, queried_at=NOW + timedelta(seconds=30),
        **_provenance(payload),
    )
    draft = SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="source", kind=ClaimKind.VALUE, label="Measured value", value=value,
        inputs=(ClaimInput(label="value", value=value, semantic_field=field, locator="row:0"),),
    ),))
    result = system_answer_from_result(SimpleNamespace(structured_output=draft), state)
    assert result is not None and result.claims
    return result


def _context(*answers, prompt="Show the reported value"):
    context = IntegrityContext(prompt)
    for answer in answers:
        capture_orchestrator_tool_result(
            f"{answer.evidence[0].system_id}___query",
            {"content": [{"text": answer.model_dump_json()}]}, context,
        )
    return context


def _input(answer, value, *, role="none"):
    evidence = answer.evidence[0]
    return ClaimInput(
        label="value", value=value, evidence_id=evidence.evidence_id,
        semantic_field=evidence.semantic_field, locator=evidence.locator,
        source_timestamp=evidence.source_observed_end, temporal_role=role,
    )


def test_structural_evidence_is_immutable_and_rejects_raw_rows():
    evidence = _answer().evidence[0]
    assert isinstance(evidence, SourceEvidence)
    with pytest.raises(ValidationError):
        evidence.system_id = "changed"
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate({**evidence.model_dump(), "rows": [["private"]]})


def test_queried_at_never_establishes_source_freshness():
    state = {}
    payload = {"columns": ["measure"], "rows": [[7]], "row_count": 1}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]},
        state, system_id="system-a", boundary="sql", tool_name="query_system_a",
        queried_at=NOW, **_provenance(payload),
    )
    result = system_answer_from_result(SimpleNamespace(structured_output=SystemAnswerDraft(claims=(
        AnswerClaim(
            claim_id="value", kind=ClaimKind.VALUE, label="Measured value", value=7,
            inputs=(ClaimInput(label="value", value=7, semantic_field="measure", locator="row:0"),),
        ),
    ))), state)
    assert result is not None and result.evidence[0].source_observed_end is None
    assert result.evidence[0].freshness == FreshnessClass.UNKNOWN


def test_system_arithmetic_is_recomputed_from_structurally_bound_operands():
    state = {}
    payload = {
        "columns": ["first", "second", "observed_at"],
        "rows": [[4, 6, NOW.isoformat()]], "row_count": 1,
    }
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state,
        system_id="system-a", boundary="sql", tool_name="query_system_a",
        queried_at=NOW, **_provenance(payload),
    )
    draft = SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="sum", kind=ClaimKind.AGGREGATE, label="Combined measure",
        value=999, operation="sum", inputs=(
            ClaimInput(label="first", value=4, semantic_field="first", locator="row:0"),
            ClaimInput(label="second", value=6, semantic_field="second", locator="row:0"),
        ),
    ),))
    answer = system_answer_from_result(SimpleNamespace(structured_output=draft), state)
    assert answer is not None and answer.claims[0].value == 10


def test_final_ranking_without_structural_entity_identity_fails_closed():
    first, second = _answer(8, system="system-a"), _answer(3, system="system-b")
    final = validate_final_answer(FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="rank", kind=ClaimKind.RANKING, label="Order", value=["beta", "alpha"],
        operation="rank_desc", inputs=(
            _input(first, 8).model_copy(update={"label": "alpha"}),
            _input(second, 3).model_copy(update={"label": "beta"}),
        ),
    ),)), _context(first, second))
    assert final.claims == ()
    assert ReasonCode.RANKING_INPUT_UNSUPPORTED in final.reason_codes


def test_memory_without_current_system_envelope_is_blocked():
    final = validate_final_answer(FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="memory", kind=ClaimKind.VALUE, label="Remembered value", value=12,
        inputs=(ClaimInput(
            label="memory", value=12, evidence_id="memory",
            semantic_field="measure", locator="row:0",
        ),),
    ),)), IntegrityContext("What is the value now?"))
    assert final.claims == ()
    assert ReasonCode.NO_CURRENT_INVOCATION_EVIDENCE in final.reason_codes
    assert not final.learning_allowed


def test_valid_comparison_uses_temporal_roles_and_recomputes_current_minus_past():
    past = _answer(5, observed=NOW - timedelta(days=1), system="system-history")
    current = _answer(9, observed=NOW, system="system-current")
    final = validate_final_answer(FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="change", kind=ClaimKind.COMPARISON, label="Change", operation="difference",
        inputs=(_input(current, 9, role="current"), _input(past, 5, role="past")),
    ),)), _context(past, current))
    assert final.claims[0].value == 4


def test_renderer_discloses_exact_source_and_query_times_with_server_templates(monkeypatch):
    monkeypatch.setattr(
        "agents.response_integrity_capture.secrets.token_hex",
        lambda count: "ab" * count,
    )
    answer = _answer(10)
    final = validate_final_answer(FinalAnswerDraft(claims=(answer.claims[0],)), _context(answer))
    rendered = render_final_answer(final)
    assert "## Freshness and limitations" in rendered
    assert "source_observed_at=" in rendered
    assert "queried_at=" in rendered
    assert "completeness=complete" in rendered
    assert answer.evidence[0].evidence_id.replace("-", "\\-") in rendered


def test_invalid_envelope_is_metadata_only_and_disables_learning():
    text, final = gate_final_result(SimpleNamespace(structured_output=None), IntegrityContext())
    assert "No requested value passed deterministic" in text
    assert "No validated current-turn source evidence" in text
    assert ReasonCode.INVALID_TYPED_ENVELOPE in final.reason_codes
    assert not final.learning_allowed


def test_local_sse_buffers_unvalidated_draft_before_emitting(monkeypatch):
    import agents.chat_client as chat
    from agents.events import DoneEvent, MessageEvent, MetadataEvent

    # Enforce-path semantics (ADR-0046) — opt in per ADR-0047.
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")

    class _Hooks:
        def add_hook(self, _hook):
            return None

    class _Agent:
        hooks = _Hooks()
        callback_handler = None

        def __call__(self, _prompt, **kwargs):
            assert kwargs["structured_output_model"] is FinalAnswerDraft
            self.callback_handler(data="fabricated draft value 999")
            return SimpleNamespace(structured_output=None)

    agent = _Agent()
    monkeypatch.setattr(chat, "build_orchestrator", lambda *_args, **_kwargs: agent)
    events = list(chat.LocalChatClient().stream("question", "session", "user"))
    messages = [event.content for event in events if isinstance(event, MessageEvent)]
    assert messages and all("fabricated draft value 999" not in text for text in messages)
    assert "No requested value passed deterministic" in messages[-1]
    integrity = [e.data["response_integrity"] for e in events
                 if isinstance(e, MetadataEvent) and "response_integrity" in e.data]
    assert integrity and integrity[0]["verdict"] == "rejected"
    assert isinstance(events[-1], DoneEvent)
