# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Replay corpus + shadow pins for the ADR-0067 concept-evidence check.

The defect this pins: an answer presented REAL rows from one canonical
concept's field under a heading claiming a DIFFERENT concept — every token
genuine and evidenced, so annotate (ADR-0047) and the typed gate (ADR-0046)
both passed it. The shadow check must flag exactly that case as ``violation``
and must resolve every uncertain basis to ``unknown``, never ``violation``.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from agents.response_integrity_concepts import (
    BINDING_CAPPED, BINDING_COMPLETE, BINDING_UNRESOLVED,
    CONCEPT_EVIDENCE_MISMATCH, MAX_SIDECAR_CHARS, MAX_SIDECAR_ENTRIES,
    REASON_CAPPED, REASON_NO_EVIDENCE, REASON_STALE_REVISION, REASON_SUPPORTED,
    REASON_UNBOUND, REASON_UNMAPPED, VERDICT_SUPPORTED, VERDICT_UNKNOWN,
    VERDICT_VIOLATION, concept_shadow_check, resolve_evidence_concepts,
)
from agents.response_integrity_contracts import (
    AnswerClaim, ClaimInput, ClaimKind, FinalAnswerDraft, IntegrityContext,
    ReasonCode, SourceEvidence, SystemAnswerDraft,
)
from agents.response_integrity_capture import capture_trusted_tool_result
from agents.response_integrity_rendering import gate_final_result, render_final_answer
from agents.response_integrity_validation import (
    capture_orchestrator_tool_result, system_answer_from_result,
    validate_final_answer,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# Generic replay vocabulary (no customer literals): the proxy concept a
# transactional system's status column maps to, and the requested concept only
# a telemetry owner carries.
PROXY = "concept-record-status"
REQUESTED = "concept-live-equipment-state"
REV = "rev-1"


def _evidence(eid="ev-a", system="system-a", physical="records.status_col",
              concepts=None, binding=BINDING_UNRESOLVED, revision=""):
    return SourceEvidence(
        evidence_id=eid, system_id=system, boundary="sql",
        tool_name=f"query_{system}", queried_at=NOW, semantic_field="status",
        physical_field=physical, locator="row:0", canonical_type="string",
        concept_ids=tuple(concepts or ()), concept_binding=binding,
        concept_schema_revision=revision,
    )


def _claim(concept, eids=("ev-a",), cid="c1", label="Requested state"):
    return AnswerClaim(
        claim_id=cid, kind=ClaimKind.VALUE, label=label,
        proposed_concept_id=concept, value="ACTIVE",
        inputs=(ClaimInput(label="v", value="ACTIVE", evidence_id=eids[0],
                           semantic_field="status"),),
        evidence_ids=tuple(eids),
    )


def _one(sidecar):
    assert sidecar is not None and sidecar["checked"] == len(sidecar["claims"])
    return sidecar["claims"][0]


# --- replay corpus -----------------------------------------------------------


def test_mislabelled_real_rows_are_a_violation():
    """The reproduced defect: genuine proxy rows under the requested label."""
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_COMPLETE, revision=REV)]
    sidecar = concept_shadow_check([_claim(REQUESTED)], evidence)
    entry = _one(sidecar)
    assert entry["verdict"] == VERDICT_VIOLATION
    assert entry["reason"] == CONCEPT_EVIDENCE_MISMATCH
    assert entry["claimed_concept"] == REQUESTED
    assert entry["evidence_concepts"] == [PROXY]
    assert entry["systems"] == ["system-a"]
    assert sidecar["violations"] == 1


def test_identical_rows_correctly_labelled_are_supported():
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_COMPLETE, revision=REV)]
    entry = _one(concept_shadow_check([_claim(PROXY)], evidence))
    assert entry["verdict"] == VERDICT_SUPPORTED
    assert entry["reason"] == REASON_SUPPORTED


def test_honest_limitation_answer_is_supported():
    """'Requested state unavailable; related proxy follows' — each claim
    labelled by its own validated concept must be supported."""
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_COMPLETE, revision=REV)]
    claims = [_claim(PROXY, cid="c1", label="Related proxy value"),
              _claim(PROXY, cid="c2", label="Second proxy row")]
    sidecar = concept_shadow_check(claims, evidence)
    assert sidecar["violations"] == 0 and sidecar["supported"] == 2


def test_owner_system_requested_state_is_supported():
    evidence = [_evidence(system="system-hist", physical="telemetry.state",
                          concepts=[REQUESTED], binding=BINDING_COMPLETE,
                          revision=REV)]
    entry = _one(concept_shadow_check([_claim(REQUESTED)], evidence))
    assert entry["verdict"] == VERDICT_SUPPORTED


def test_graph_unavailable_is_unknown_never_violation():
    entry = _one(concept_shadow_check(
        [_claim(REQUESTED)], [_evidence(binding=BINDING_UNRESOLVED)]))
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert entry["reason"] == REASON_UNBOUND


def test_capped_mapping_is_unknown_never_violation():
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_CAPPED, revision=REV)]
    entry = _one(concept_shadow_check([_claim(REQUESTED)], evidence))
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert entry["reason"] == REASON_CAPPED


def test_unmapped_physical_field_is_unknown_never_violation():
    evidence = [_evidence(concepts=[], binding=BINDING_COMPLETE, revision=REV)]
    entry = _one(concept_shadow_check([_claim(REQUESTED)], evidence))
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert entry["reason"] == REASON_UNMAPPED


def test_stale_schema_revision_is_unknown_never_violation():
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_COMPLETE,
                          revision="rev-0")]
    entry = _one(concept_shadow_check([_claim(REQUESTED)], evidence,
                                      expected_schema_revision=REV))
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert entry["reason"] == REASON_STALE_REVISION


def test_disagreeing_pinned_revisions_are_unknown():
    evidence = [
        _evidence(eid="ev-a", concepts=[PROXY], binding=BINDING_COMPLETE,
                  revision="rev-1"),
        _evidence(eid="ev-b", physical="records.other_col", concepts=[PROXY],
                  binding=BINDING_COMPLETE, revision="rev-2"),
    ]
    entry = _one(concept_shadow_check([_claim(REQUESTED, eids=("ev-a", "ev-b"))],
                                      evidence))
    assert entry["verdict"] == VERDICT_UNKNOWN


def test_missing_evidence_reference_is_unknown():
    entry = _one(concept_shadow_check([_claim(REQUESTED, eids=("ev-gone",))], []))
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert entry["reason"] == REASON_NO_EVIDENCE


def test_one_system_holding_both_concepts_is_supported_via_exact_field():
    """One system can legitimately hold BOTH concepts: the EXACT cited field
    decides, not system-level coverage."""
    evidence = [_evidence(concepts=[PROXY, REQUESTED], binding=BINDING_COMPLETE,
                          revision=REV)]
    entry = _one(concept_shadow_check([_claim(REQUESTED)], evidence))
    assert entry["verdict"] == VERDICT_SUPPORTED


def test_sibling_field_mapping_to_requested_concept_still_convicts_exact_field():
    """The system carries the requested concept in ANOTHER field; the claim
    cites the proxy field only — still a violation (exact-field rule)."""
    evidence = [
        _evidence(eid="ev-a", concepts=[PROXY], binding=BINDING_COMPLETE,
                  revision=REV),
        _evidence(eid="ev-b", physical="records.live_state_col",
                  concepts=[REQUESTED], binding=BINDING_COMPLETE, revision=REV),
    ]
    entry = _one(concept_shadow_check([_claim(REQUESTED, eids=("ev-a",))],
                                      evidence))
    assert entry["verdict"] == VERDICT_VIOLATION


def test_capped_binding_containing_claimed_concept_supports():
    """A positive mapping needs no completeness — capped lists may support,
    never convict."""
    evidence = [_evidence(concepts=[REQUESTED], binding=BINDING_CAPPED,
                          revision=REV)]
    entry = _one(concept_shadow_check([_claim(REQUESTED)], evidence))
    assert entry["verdict"] == VERDICT_SUPPORTED


# --- resolver ----------------------------------------------------------------


def test_resolver_fills_bindings_only_from_server_rows():
    items = [_evidence(eid="ev-a"), _evidence(eid="ev-b", physical="records.other")]
    resolved = resolve_evidence_concepts(items, {
        ("system-a", "records.status_col"): {"concepts": [PROXY], "complete": True},
    }, REV)
    assert resolved[0].concept_binding == BINDING_COMPLETE
    assert resolved[0].concept_ids == (PROXY,)
    assert resolved[0].concept_schema_revision == REV
    assert resolved[1].concept_binding == BINDING_UNRESOLVED


def test_resolver_marks_incomplete_rows_capped():
    resolved = resolve_evidence_concepts([_evidence()], {
        ("system-a", "records.status_col"): {"concepts": [PROXY], "complete": False},
    }, REV)
    assert resolved[0].concept_binding == BINDING_CAPPED


def test_resolver_without_pinned_revision_is_a_no_op():
    resolved = resolve_evidence_concepts([_evidence()], {
        ("system-a", "records.status_col"): {"concepts": [PROXY], "complete": True},
    }, "")
    assert resolved[0].concept_binding == BINDING_UNRESOLVED


def test_resolver_falls_back_to_semantic_field():
    resolved = resolve_evidence_concepts([_evidence(physical="")], {
        ("system-a", "status"): {"concepts": [PROXY], "complete": True},
    }, REV)
    assert resolved[0].concept_binding == BINDING_COMPLETE


# --- shadow posture pins -----------------------------------------------------


def test_no_proposed_concepts_yields_no_sidecar():
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_COMPLETE, revision=REV)]
    claim = _claim("", cid="c-none")
    assert concept_shadow_check([claim], evidence) is None


def test_sidecar_is_bounded_and_carries_no_row_values():
    evidence = [_evidence(concepts=[PROXY], binding=BINDING_COMPLETE, revision=REV)]
    claims = [_claim(REQUESTED, cid=f"c{i}") for i in range(100)]
    sidecar = concept_shadow_check(claims, evidence)
    assert sidecar["checked"] == 100
    assert len(sidecar["claims"]) <= MAX_SIDECAR_ENTRIES
    assert sidecar["truncated"] is True
    serialized = json.dumps(sidecar, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))
    assert len(serialized) <= MAX_SIDECAR_CHARS
    # No answer excerpts, no raw row values, no model rationale.
    assert "ACTIVE" not in serialized
    assert "Requested state" not in serialized


def test_mismatch_is_not_a_reason_code():
    """Shadow-only: no new error type exists anywhere in the answer contract."""
    assert "CONCEPT_EVIDENCE_MISMATCH" not in ReasonCode.__members__
    assert CONCEPT_EVIDENCE_MISMATCH not in {code.value for code in ReasonCode}


_MODULE = (Path(__file__).resolve().parents[2]
           / "agents" / "response_integrity_concepts.py")


def test_no_llm_participates_in_the_check():
    source = _MODULE.read_text(encoding="utf-8")
    for forbidden in ("strands", "bedrock", "boto3", "openai", "anthropic",
                      "build_model", "invoke_model", "requests", "urllib"):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    imports = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [None]) or [None]
    }
    allowed = {"__future__", "json", "typing", ".response_integrity_contracts",
               "agents.response_integrity_contracts",
               "response_integrity_contracts"}
    assert all(
        name in allowed or (name or "").endswith("response_integrity_contracts")
        for name in imports
    ), imports


def test_module_is_domain_free():
    """No customer or demo domain literals in the engine module (ADR-0044)."""
    source = _MODULE.read_text(encoding="utf-8").lower()
    for literal in ("machine", "lifecycle", "sitewise", "cmms", "highbyte",
                    "b-2847"):
        assert literal not in source, literal
    for word in ("erp", "mes", "mke"):
        assert not re.search(rf"\b{word}\b", source), word


# --- zero-answer-byte pins (full validate/render path) -----------------------


def _provenance(payload):
    projections, schema = [], {}
    for index, column in enumerate(payload["columns"]):
        if column.startswith("__adt_source_"):
            source, data_type = f"observed_{index}", "timestamp"
        else:
            source = column
            values = [row[index] for row in payload["rows"] if index < len(row)]
            data_type = "varchar" if any(isinstance(v, str) for v in values) else "integer"
        schema[source] = data_type
        projections.append(f"{source} AS {column}")
    return {
        "executed_query": f"SELECT {', '.join(projections)} FROM records",  # nosec B608 - test fixture
        "physical_schema": {"records": {"relation_kind": "base_table", "columns": schema}},
    }


def _system_answer(concept=""):
    field = "status"
    payload = {"columns": [field, f"__adt_source_time__{field}"],
               "rows": [["ACTIVE", NOW.isoformat()]], "row_count": 1}
    state = {}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state, system_id="system-a",
        boundary="sql", tool_name="query_system_a",
        queried_at=NOW + timedelta(seconds=30), **_provenance(payload),
    )
    draft = SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="c1", kind=ClaimKind.VALUE, label="Reported status",
        proposed_concept_id=concept, value="ACTIVE",
        inputs=(ClaimInput(label="v", value="ACTIVE", semantic_field=field,
                           locator="row:0"),),
    ),))
    answer = system_answer_from_result(SimpleNamespace(structured_output=draft), state)
    assert answer is not None and answer.claims
    return answer


def _context(answer, prompt="What was the recorded status yesterday?"):
    context = IntegrityContext(prompt)
    capture_orchestrator_tool_result(
        "system-a___query", {"content": [{"text": answer.model_dump_json()}]},
        context,
    )
    return context


def _final_draft(answer, concept=""):
    evidence = answer.evidence[0]
    return FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="f1", kind=ClaimKind.VALUE, label="Reported status",
        proposed_concept_id=concept, value="ACTIVE",
        inputs=(ClaimInput(label="v", value="ACTIVE",
                           evidence_id=evidence.evidence_id,
                           semantic_field="status", locator="row:0"),),
    ),))


def test_proposed_concept_survives_system_and_final_binding():
    answer = _system_answer(concept=REQUESTED)
    assert answer.claims[0].proposed_concept_id == REQUESTED
    context = _context(answer)
    final = validate_final_answer(_final_draft(answer, concept=REQUESTED), context)
    assert final.claims and final.claims[0].proposed_concept_id == REQUESTED


def test_answer_bytes_identical_with_and_without_proposed_concepts():
    answer = _system_answer()
    baseline = validate_final_answer(
        _final_draft(answer, concept=""), _context(answer))
    shadowed = validate_final_answer(
        _final_draft(answer, concept=REQUESTED), _context(answer))
    assert render_final_answer(baseline) == render_final_answer(shadowed)
    assert baseline.reason_codes == shadowed.reason_codes
    assert baseline.complete == shadowed.complete
    assert baseline.quality_metadata() == shadowed.quality_metadata()


def test_validate_final_answer_populates_shadow_without_gating(caplog):
    answer = _system_answer(concept=REQUESTED)
    context = _context(answer)
    final = validate_final_answer(_final_draft(answer, concept=REQUESTED), context)
    assert context.concept_shadow is not None
    entry = context.concept_shadow["claims"][0]
    # Unwired graph resolution ⇒ unresolved bindings ⇒ unknown, never violation.
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert final.claims  # the claim itself was accepted untouched
    # And a violation verdict, injected via bindings, still gates nothing:
    bound = answer.model_copy(update={"evidence": tuple(
        item.model_copy(update={
            "concept_ids": (PROXY,), "concept_binding": BINDING_COMPLETE,
            "concept_schema_revision": REV,
        }) for item in answer.evidence
    )})
    context2 = _context(bound)
    final2 = validate_final_answer(_final_draft(bound, concept=REQUESTED), context2)
    assert context2.concept_shadow["claims"][0]["verdict"] == VERDICT_VIOLATION
    assert final2.claims and final2.reason_codes == final.reason_codes
    assert render_final_answer(final2) == render_final_answer(final)


def test_gate_final_result_logs_sidecar_and_output_is_unchanged(caplog):
    answer = _system_answer()
    draft = _final_draft(answer, concept=REQUESTED)
    plain_draft = _final_draft(answer, concept="")
    import logging
    with caplog.at_level(logging.INFO, logger="agents.response_integrity_rendering"):
        rendered, validated = gate_final_result(
            SimpleNamespace(structured_output=draft), _context(answer))
        plain_rendered, _ = gate_final_result(
            SimpleNamespace(structured_output=plain_draft), _context(answer))
    assert rendered == plain_rendered
    assert "concept-shadow" not in rendered
    shadow_logs = [r for r in caplog.records if "concept-shadow" in r.getMessage()]
    assert len(shadow_logs) == 1  # only the concept-proposing turn emits
    assert validated.claims


def test_shadow_failure_fails_open(monkeypatch):
    import agents.response_integrity_validation as validation

    def boom(*_args, **_kwargs):
        raise RuntimeError("shadow defect")

    import agents.response_integrity_concepts as concepts
    monkeypatch.setattr(concepts, "concept_shadow_check", boom)
    answer = _system_answer(concept=REQUESTED)
    context = _context(answer)
    final = validation.validate_final_answer(
        _final_draft(answer, concept=REQUESTED), context)
    assert context.concept_shadow is None
    assert final.claims  # the turn still validated and rendered normally


def test_legacy_envelopes_without_concept_fields_still_validate():
    """Older SystemAnswer payloads (no concept fields) parse unchanged."""
    answer = _system_answer()
    payload = json.loads(answer.model_dump_json())
    for item in payload["evidence"]:
        item.pop("concept_ids"), item.pop("concept_binding")
        item.pop("concept_schema_revision")
    for claim in payload["claims"]:
        claim.pop("proposed_concept_id")
    context = IntegrityContext("What was the recorded status yesterday?")
    capture_orchestrator_tool_result(
        "system-a___query", {"content": [{"json": payload}]}, context)
    assert context.system_answers and context.system_answers[0].evidence
