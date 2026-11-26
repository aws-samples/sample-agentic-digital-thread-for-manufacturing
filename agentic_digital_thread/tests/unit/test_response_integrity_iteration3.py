# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Adversarial probes for deterministic response integrity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import re
import pytest
from pydantic import ValidationError

from agents import graph_tools
from agents.response_integrity import (
    MAX_FINAL_BUFFER_BYTES,
    AnswerClaim,
    ClaimInput,
    ClaimKind,
    FinalAnswer,
    FinalAnswerDraft,
    IntegrityContext,
    LearningProposal,
    LearningProposalKind,
    ReasonCode,
    RelationshipProof,
    SystemAnswerDraft,
    capture_orchestrator_tool_result,
    capture_trusted_tool_result,
    final_draft_from_result,
    gate_final_result,
    render_final_answer,
    system_answer_from_result,
    validate_final_answer,
)

def _provenance(payload, boundary):
    if boundary not in {"sql", "athena"} or not isinstance(payload.get("columns"), list):
        return {}
    projections, schema = [], {}
    rows = payload.get("rows", [])
    for index, column in enumerate(payload["columns"]):
        if column.startswith("__adt_source_"):
            source, data_type = f"observed_{index}", "timestamp"
        else:
            source = column
            values = [row[index] for row in rows if isinstance(row, list) and index < len(row)]
            data_type = "varchar" if any(isinstance(value, str) for value in values) else "integer"
            if column in {"observed_at", "updated_at", "timestamp"}:
                data_type = "timestamp"
        schema[source] = data_type
        projections.append(f"{source} AS {column}")
    return {"executed_query": f"SELECT {', '.join(projections)} FROM records", "physical_schema": {"records": {"relation_kind": "base_table", "columns": schema}}}  # nosec B608 - test fixture composing expected SQL from module constants

UTC = timezone.utc
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _system(payload, claims, *, system="system-a", boundary="sql", prompt=""):
    state = {}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state,
        system_id=system, boundary=boundary, tool_name="read_values",
        current_intent=bool(prompt), queried_at=NOW,
        **_provenance(payload, boundary),
    )
    result = SimpleNamespace(structured_output=SystemAnswerDraft(claims=tuple(claims)))
    answer = system_answer_from_result(result, state)
    assert answer is not None
    return answer


def _context(*answers, prompt="Show historical values from last year"):
    context = IntegrityContext(prompt)
    for answer in answers:
        target = answer.evidence[0].system_id if answer.evidence else "system-a"
        capture_orchestrator_tool_result(
            f"{target}___query", {"content": [{"text": answer.model_dump_json()}]}, context,
        )
    return context


def _input(answer, value, index=0, role="none"):
    evidence = answer.evidence[index]
    source = answer.claims[0].inputs[index] if answer.claims else None
    return ClaimInput(
        label="ignored", value=value, evidence_id=evidence.evidence_id,
        semantic_field=evidence.semantic_field, locator=evidence.locator,
        source_timestamp=evidence.source_observed_end, temporal_role=role,
        entity_label=source.entity_label if source else "",
    )


def test_none_claim_value_is_derived_at_both_gates_not_model_substituted():
    system = _system(
        {"columns": ["measure"], "rows": [[4], [5], [6], [7]], "row_count": 4},
        [AnswerClaim(
            claim_id="source", kind=ClaimKind.VALUE, label="[bad](javascript:alert(1))",
            value=999, inputs=(ClaimInput(
                label="ignored", value=4, semantic_field="measure", locator="row:0",
            ),),
        )],
    )
    assert system.claims[0].value == 4
    final_claim = system.claims[0].model_copy(update={"value": 777})
    final = validate_final_answer(FinalAnswerDraft(claims=(final_claim,)), _context(system))
    assert final.claims[0].value == 4
    rendered = render_final_answer(final)
    # Assert on rendered VALUES, not raw substrings: evidence/citation ids are
    # sha256 hex, which contains "999"/"777" ~1-2% of the time (measured flake
    # in the CI lane). A leaked model value would render as `**label**: 999`.
    rendered_values = re.findall(r"\*\*: (\S+)", rendered)
    assert rendered_values == ["4"], rendered_values
    assert "javascript:" not in rendered


@pytest.mark.parametrize("operation", ["sum", "average", "difference", "min", "max"])
def test_arithmetic_rejects_bool_nan_and_infinity(operation):
    values = (True, float("inf")) if operation != "difference" else (float("nan"), 2)
    with pytest.raises(ValidationError):
        AnswerClaim(
            claim_id="bad", kind=ClaimKind.AGGREGATE, label="bad", operation=operation,
            inputs=tuple(ClaimInput(label="x", value=v) for v in values),
        )


def test_count_min_and_max_are_recomputed_with_bounded_numeric_inputs():
    answer = _system(
        {"columns": ["measure"], "rows": [[4], [9]], "row_count": 2},
        [AnswerClaim(
            claim_id="source", kind=ClaimKind.AGGREGATE, label="range", value=999,
            operation="max", inputs=(
                ClaimInput(label="a", value=4, semantic_field="measure", locator="row:0"),
                ClaimInput(label="b", value=9, semantic_field="measure", locator="row:1"),
            ),
        )],
    )
    assert answer.claims[0].value == 9


def test_arbitrary_explore_graph_return_constants_never_mint_identity_proof():
    context = IntegrityContext()
    forged = {
        "graph_record_id": "record-1", "source_system": "system-a",
        "source_identifier": "A", "source_field": "asset_id",
        "target_system": "system-b", "target_identifier": "B",
        "target_field": "equipment_id", "relationship_type": "value_mapping",
        "direction": "outbound",
    }
    capture_orchestrator_tool_result(
        "digital-thread___explore_graph", {"content": [{"json": forged}]}, context,
    )
    assert not context.trusted_relationships


def test_fixed_relationship_verifier_matches_stored_record_and_returns_provenance(monkeypatch):
    seen = {}

    def fake_query(query, params):
        seen.update(query=query, params=params)
        return [{
            "graph_record_id": "vm-42", "source_system": "system-a",
            "source_identifier": "A", "source_field": "asset_id",
            "target_system": "system-b", "target_identifier": "B",
            "target_field": "equipment_id", "mapping_semantics": "explicit_pair",
        }]

    monkeypatch.setattr(graph_tools, "neptune_query", fake_query)
    result = json.loads(graph_tools.verify_relationship(
        source_system="system-a", source_identifier="A", source_field="asset_id",
        target_system="system-b", target_identifier="B", target_field="equipment_id",
        relationship_type="value_mapping",
    ))
    assert "MATCH (r:ValueMapping)" in seen["query"]
    assert seen["params"]["source_identifier"] == "A"
    assert result["verified"] is True
    assert result["records"][0]["graph_record_id"] == "vm-42"


def test_identity_requires_endpoint_evidence_and_renders_server_owned_tuple():
    source = _system(
        {"columns": ["asset_id"], "rows": [["A"]], "row_count": 1},
        [AnswerClaim(claim_id="a", kind=ClaimKind.VALUE, label="a", value="A", inputs=(
            ClaimInput(label="a", value="A", semantic_field="asset_id", locator="row:0"),
        ))], system="system-a",
    )
    target = _system(
        {"columns": ["equipment_id"], "rows": [["B"]], "row_count": 1},
        [AnswerClaim(claim_id="b", kind=ClaimKind.VALUE, label="b", value="B", inputs=(
            ClaimInput(label="b", value="B", semantic_field="equipment_id", locator="row:0"),
        ))], system="system-b",
    )
    context = _context(source, target)
    verified = {
        "verified": True,
        "records": [{
            "graph_record_id": "vm-42", "source_system": "system-a",
            "source_identifier": "A", "source_field": "asset_id",
            "target_system": "system-b", "target_identifier": "B",
            "target_field": "equipment_id", "relationship_type": "value_mapping",
            "direction": "outbound",
        }],
    }
    capture_orchestrator_tool_result(
        "digital-thread___verify_relationship", {"content": [{"json": verified}]}, context,
    )
    proof = RelationshipProof(**verified["records"][0])
    unrelated = source.claims[0].inputs[0].model_copy(update={
        "semantic_field": "unrelated", "value": "A",
    })
    rejected = validate_final_answer(FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="identity", kind=ClaimKind.IDENTITY, label="[forged link](https://x)",
        value="arbitrary prose", inputs=(unrelated, target.claims[0].inputs[0]),
        relationship_proof=proof,
    ),)), context)
    assert rejected.claims == ()

    accepted = validate_final_answer(FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="identity", kind=ClaimKind.IDENTITY, label="ignored", value="ignored",
        inputs=(source.claims[0].inputs[0], target.claims[0].inputs[0]),
        relationship_proof=proof,
    ),)), context)
    text = render_final_answer(accepted)
    assert "system\\-a" in text and "asset\\_id" in text and "A" in text
    assert "system\\-b" in text and "equipment\\_id" in text and "B" in text
    assert "vm\\-42" in text and "arbitrary prose" not in text


def test_sql_timestamp_binding_requires_exact_reserved_value_alias():
    payload = {
        "columns": ["measure", "updated_at", "__adt_source_time__other"],
        "rows": [[7, NOW.isoformat(), NOW.isoformat()]], "row_count": 1,
    }
    answer = _system(payload, [AnswerClaim(
        claim_id="v", kind=ClaimKind.VALUE, label="v", value=7, inputs=(
            ClaimInput(label="v", value=7, semantic_field="measure", locator="row:0"),
        ),
    )])
    assert answer.evidence[0].source_observed_end is None

    exact = _system({
        "columns": ["measure", "__adt_source_time__measure"],
        "rows": [[7, NOW.isoformat()]], "row_count": 1,
    }, [AnswerClaim(
        claim_id="v", kind=ClaimKind.VALUE, label="v", value=7, inputs=(
            ClaimInput(label="v", value=7, semantic_field="measure", locator="row:0"),
        ),
    )])
    assert exact.evidence[0].source_observed_end == NOW


def test_current_terms_win_in_mixed_prompt_and_current_role_requires_freshness():
    context = IntegrityContext("Compare current output to yesterday")
    assert context.current_intent is True
    past = _system(
        {"columns": ["measure", "__adt_source_time__measure"],
         "rows": [[5, (NOW - timedelta(days=1)).isoformat()]], "row_count": 1},
        [AnswerClaim(claim_id="p", kind=ClaimKind.VALUE, label="p", value=5, inputs=(
            ClaimInput(label="p", value=5, semantic_field="measure", locator="row:0"),
        ))],
    )
    unknown = _system(
        {"columns": ["measure"], "rows": [[9]], "row_count": 1},
        [AnswerClaim(claim_id="c", kind=ClaimKind.VALUE, label="c", value=9, inputs=(
            ClaimInput(label="c", value=9, semantic_field="measure", locator="row:0"),
        ))],
    )
    context = _context(past, unknown, prompt="Compare current output to yesterday")
    claim = AnswerClaim(
        claim_id="cmp", kind=ClaimKind.COMPARISON, label="cmp", operation="difference",
        inputs=(
            past.claims[0].inputs[0].model_copy(update={"temporal_role": "past"}),
            unknown.claims[0].inputs[0].model_copy(update={"temporal_role": "current"}),
        ),
    )
    final = validate_final_answer(FinalAnswerDraft(claims=(claim,)), context)
    assert final.claims == ()
    assert ReasonCode.CURRENT_EVIDENCE_REQUIRED in final.reason_codes


def test_nested_rejected_over_budget_and_mixed_shapes_are_partial():
    for payload in (
        {"items": [{"result": {"rejected": True}}]},
        {"items": [{"result": {"over_budget": True}}]},
        {"items": [{"status": "success"}, "unknown-shape"]},
    ):
        answer = _system(payload, [])
        assert answer.status != "complete"
        assert ReasonCode.SOURCE_PARTIAL in answer.reason_codes or ReasonCode.SOURCE_UNAVAILABLE in answer.reason_codes


def test_sensitive_parent_and_neutral_key_secret_content_never_mint_or_render():
    payload = {
        "profile": {"credentials": {"value": "visible-looking"}},
        "notes": ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjMifQ", "signature")),
        "safe_measure": 7,
    }
    answer = _system(payload, [
        AnswerClaim(claim_id="secret", kind=ClaimKind.VALUE, label="secret", value="visible-looking", inputs=(
            ClaimInput(label="x", value="visible-looking", semantic_field="value", locator="profile:credentials"),
        )),
        AnswerClaim(claim_id="jwt", kind=ClaimKind.VALUE, label="jwt", value=payload["notes"], inputs=(
            ClaimInput(label="x", value=payload["notes"], semantic_field="notes", locator="item:0"),
        )),
    ])
    assert answer.claims == ()
    rendered = render_final_answer(validate_final_answer(FinalAnswerDraft(), _context(answer)))
    assert "visible-looking" not in rendered and "eyJhbGci" not in rendered


def test_ranking_requires_distinct_structural_entity_labels():
    ranked = _system(
        {"columns": ["machine_id", "measure"], "rows": [["M-1", 8], ["M-2", 3]], "row_count": 2},
        [AnswerClaim(
            claim_id="rank", kind=ClaimKind.RANKING, label="rank", operation="rank_desc",
            inputs=(
                ClaimInput(label="ignored", value=8, semantic_field="measure", locator="row:0"),
                ClaimInput(label="ignored", value=3, semantic_field="measure", locator="row:1"),
            ),
        )],
    )
    assert ranked.claims[0].value == ["M-1", "M-2"]
    assert [item.entity_label for item in ranked.claims[0].inputs] == ["M-1", "M-2"]

    no_identity = _system(
        {"columns": ["measure"], "rows": [[8], [3]], "row_count": 2},
        [AnswerClaim(
            claim_id="rank", kind=ClaimKind.RANKING, label="rank", operation="rank_desc",
            inputs=(
                ClaimInput(label="ignored", value=8, semantic_field="measure", locator="row:0"),
                ClaimInput(label="ignored", value=3, semantic_field="measure", locator="row:1"),
            ),
        )],
    )
    assert no_identity.claims == ()
    assert ReasonCode.RANKING_INPUT_UNSUPPORTED in no_identity.reason_codes


def test_6000_claim_draft_and_repeated_calls_fail_closed_with_bounded_output():
    raw = {"claims": [{
        "claim_id": f"c-{i}", "kind": "value", "label": "x", "value": i,
        "inputs": [{"label": "x", "value": i}],
    } for i in range(6000)]}
    result = SimpleNamespace(structured_output=raw)
    assert final_draft_from_result(result) is None
    text, final = gate_final_result(result, IntegrityContext())
    assert len(text.encode("utf-8")) <= MAX_FINAL_BUFFER_BYTES
    assert ReasonCode.INVALID_TYPED_ENVELOPE in final.reason_codes

    context = IntegrityContext()
    envelope = {"status": "complete", "claims": [], "evidence": [], "reason_codes": []}
    for _ in range(200):
        capture_orchestrator_tool_result(
            "system-a___query", {"content": [{"json": envelope}]}, context,
        )
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in context.capture_reason_codes


def test_typed_proposal_persistence_keeps_safe_proven_raw_values(monkeypatch):
    from agents import runtime_integrity
    calls = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or "vm-stub")
    proof = RelationshipProof(
        graph_record_id="vm-safe", source_system="system-a", source_identifier="A-1",
        source_field="asset_id", target_system="system-b", target_identifier="B-1",
        target_field="equipment_id", relationship_type="value_mapping", direction="outbound",
    )
    proposal = LearningProposal(
        proposal_id="p", kind=LearningProposalKind.VALUE_MAPPING,
        from_system="system-a", from_field="asset_id", from_value="A-1",
        to_system="system-b", to_field="equipment_id", to_value="B-1",
        concept="asset", evidence_ids=("ev-a", "ev-b"), relationship_proof=proof,
        relationship_type="value_mapping", direction="outbound",
    )
    answer = FinalAnswer(
        claims=(), evidence=(), proposals=(proposal,), reason_codes=(), complete=True,
        learning_allowed=True, current_intent=False,
    )
    assert runtime_integrity.persist_typed_proposals(answer, tool_calls_count=2) == 1
    assert calls[0]["from_value"] == "A-1" and calls[0]["to_value"] == "B-1"
    assert "sha256:" not in json.dumps(calls)


def test_debug_stream_capture_is_default_off_and_cross_owner_denied(monkeypatch):
    from agents.stream_capture import CaptureAccessDenied, open_stream_capture, read_capture
    monkeypatch.delenv("ADT_DEBUG_STREAM_CAPTURE", raising=False)
    assert open_stream_capture(owner_id="user-a", session_id="session-a") is None
    monkeypatch.setenv("ADT_DEBUG_STREAM_CAPTURE", "1")
    capture = open_stream_capture(owner_id="user-a", session_id="session-a")
    assert capture is not None
    capture.write('data: {"token":"secret-value","email":"person@example.com"}')
    capture.close()
    with pytest.raises(CaptureAccessDenied):
        read_capture(
            capture.capture_id, requester_id="user-b", session_id="session-a", is_admin=True,
        )
    content = read_capture(
        capture.capture_id, requester_id="user-a", session_id="session-a", is_admin=True,
    )
    assert b"secret-value" not in content and b"person@example.com" not in content


def test_independent_claim_output_rejects_infinity():
    with pytest.raises(ValidationError):
        AnswerClaim(
            claim_id="non-finite-output", kind=ClaimKind.VALUE, label="value",
            value=float("inf"),
            inputs=(ClaimInput(label="source", value=1.0),),
        )


def test_rendered_final_is_bounded_by_utf8_bytes_not_characters(monkeypatch):
    import agents.response_integrity_rendering as rendering

    answer = FinalAnswer(
        claims=(), evidence=(), proposals=(), reason_codes=(), complete=False,
        learning_allowed=False, current_intent=False,
    )
    monkeypatch.setattr(
        rendering, "_render_unbounded", lambda _answer: "\U0001f642" * MAX_FINAL_BUFFER_BYTES,
    )
    rendered = rendering.render_final_answer(answer)
    assert len(rendered.encode("utf-8")) <= MAX_FINAL_BUFFER_BYTES
    assert "\U0001f642" not in rendered


def test_fixed_relationship_verifier_rejects_ambiguous_records(monkeypatch):
    monkeypatch.setattr(
        graph_tools, "neptune_query",
        lambda _query, _params: [
            {"graph_record_id": "vm-1"}, {"graph_record_id": "vm-2"},
        ],
    )
    result = json.loads(graph_tools.verify_relationship(
        source_system="system-a", source_identifier="A", source_field="asset_id",
        target_system="system-b", target_identifier="B", target_field="equipment_id",
        relationship_type="value_mapping",
    ))
    assert result == {"verified": False, "records": [], "reason": "ambiguous"}


def test_repeated_graph_and_distinct_system_calls_exceed_invocation_quotas():
    proof = {
        "graph_record_id": "vm-1", "source_system": "system-a",
        "source_identifier": "A", "source_field": "asset_id",
        "target_system": "system-b", "target_identifier": "B",
        "target_field": "equipment_id", "relationship_type": "value_mapping",
        "direction": "outbound",
    }
    graph_context = IntegrityContext()
    for _ in range(65):
        capture_orchestrator_tool_result(
            "digital-thread___verify_relationship",
            {"content": [{"json": {"verified": True, "records": [proof]}}]},
            graph_context,
        )
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in graph_context.capture_reason_codes

    systems_context = IntegrityContext()
    for index in range(33):
        system_id = f"system-{index}"
        answer = _system(
            {"value": index},
            [AnswerClaim(
                claim_id="v", kind=ClaimKind.VALUE, label="v", value=index,
                inputs=(ClaimInput(
                    label="v", value=index, semantic_field="value", locator="item:0",
                ),),
            )], system=system_id, boundary="rest",
        )
        capture_orchestrator_tool_result(
            f"{system_id}___query",
            {"content": [{"text": answer.model_dump_json()}]}, systems_context,
        )
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in systems_context.capture_reason_codes


def test_partial_nested_input_disables_all_proposal_learning():
    partial = _system(
        {"items": [{"result": {"rejected": True}}]}, [], system="system-a",
    )
    final = validate_final_answer(FinalAnswerDraft(proposals=()), _context(partial))
    assert final.complete is False
    assert final.learning_allowed is False
    assert final.proposals == ()
