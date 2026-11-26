# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure deterministic validation for system and final Mode A drafts."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import json
import logging
import re
from typing import Any, Iterable

from .response_integrity_capture import (
    _CaptureState, _PrivateObservation, _as_utc, _fingerprint, _state,
    bounded_payloads, raw_envelope_completeness,
)
from .response_integrity_contracts import (
    MAX_GRAPH_TUPLES_PER_TURN, MAX_PROPOSALS_PER_TURN, MAX_SYSTEMS_PER_TURN,
    MAX_SYSTEM_ANSWERS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN, MAX_TOTAL_TOOL_PAYLOAD_BYTES, AnswerClaim, ClaimInput,
    ClaimKind, Completeness, FinalAnswer, FinalAnswerDraft, FreshnessClass,
    IntegrityContext, LearningProposal, LearningProposalKind, ReasonCode,
    RelationshipProof, SourceEvidence, SystemAnswer, SystemAnswerDraft,
)
from .response_integrity_security import sensitive_field, sensitive_text
from .response_integrity_proposals import actionable_proposal

logger = logging.getLogger(__name__)

_CAUSAL_RE = re.compile(r"\b(caused?\b|due to|led to|drives?|resulted in)\b", re.I)
_COMPLETENESS_SENSITIVE_OPERATIONS = {
    "sum", "average", "count", "min", "max", "rank_asc", "rank_desc",
}


def _incomplete_operation_reason(
    claim: AnswerClaim, evidence: Iterable[SourceEvidence],
) -> ReasonCode | None:
    sensitive = (
        claim.operation in _COMPLETENESS_SENSITIVE_OPERATIONS
        or claim.kind in {ClaimKind.RANKING, ClaimKind.COMPARISON}
    )
    if sensitive and any(item.completeness != Completeness.COMPLETE for item in evidence):
        return ReasonCode.SOURCE_PARTIAL
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or abs(number) > Decimal("1e30"):
        return None
    return number if len(number.as_tuple().digits) <= 18 else None


def _number_out(value: Decimal) -> int | Decimal | None:
    if not value.is_finite() or abs(value) > Decimal("1e30"):
        return None
    if len(value.as_tuple().digits) > 18:
        return None
    if value == value.to_integral_value():
        return int(value)
    return value


def _derive_value_from_context_inputs(claim: AnswerClaim) -> Any | None:
    """Derive an op-none value from several BOUND context inputs.

    Every input was already verified against trusted observations; derive the
    unique input whose value canonically equals the claimed value — ambiguity
    or no match fails (fail-closed).
    """
    claimed = claim.value
    if isinstance(claimed, (list, tuple)) and len(claimed) == 1:
        claimed = claimed[0]
    target = _decimal(claimed) if _decimal(claimed) is not None else str(claimed)
    matches = []
    for item in claim.inputs:
        candidate = _decimal(item.value)
        value = candidate if candidate is not None else str(item.value)
        if value == target:
            matches.append(item.value)
    distinct = {str(item) for item in matches}
    return matches[0] if len(distinct) == 1 else None


def _derive_claim_value(claim: AnswerClaim) -> Any | None:
    if claim.operation == "none":
        if len(claim.inputs) == 1:
            return claim.inputs[0].value
        # A value claim may carry several BOUND context inputs (e.g. the group
        # keys alongside the group's count) — see the helper.
        return _derive_value_from_context_inputs(claim)
    if claim.operation == "count":
        return len(claim.inputs) if claim.inputs else None
    if claim.operation in {"rank_asc", "rank_desc"}:
        if not claim.inputs or any(_decimal(item.value) is None or not item.entity_label for item in claim.inputs):
            return None
        labels = [item.entity_label for item in claim.inputs]
        if len(set(labels)) != len(labels):
            return None
        ordered = sorted(
            claim.inputs, key=lambda item: _decimal(item.value),
            reverse=claim.operation == "rank_desc",
        )
        return [item.entity_label for item in ordered]
    values = [_decimal(item.value) for item in claim.inputs]
    if not values or any(item is None for item in values):
        return None
    numeric = [item for item in values if item is not None]
    with localcontext() as context:
        context.prec = 34
        if claim.kind == ClaimKind.COMPARISON and claim.operation == "difference":
            past = next((_decimal(item.value) for item in claim.inputs if item.temporal_role == "past"), None)
            current = next((_decimal(item.value) for item in claim.inputs if item.temporal_role == "current"), None)
            return _number_out(current - past) if current is not None and past is not None else None
        if claim.operation == "sum":
            value = sum(numeric, Decimal(0))
        elif claim.operation == "average":
            value = sum(numeric, Decimal(0)) / Decimal(len(numeric))
        elif claim.operation == "difference" and len(numeric) == 2:
            value = numeric[0] - numeric[1]
        elif claim.operation == "min":
            value = min(numeric)
        elif claim.operation == "max":
            value = max(numeric)
        else:
            return None
    return _number_out(value)


def _observation_value_matches(item: ClaimInput, observation: _PrivateObservation) -> bool:
    if observation.canonical_fingerprint == _fingerprint(item.value):
        return True
    if observation.public.canonical_type not in {"integer", "number"} or not isinstance(item.value, str):
        return False
    try:
        candidate = Decimal(item.value.strip())
        observed = Decimal(str(observation.canonical_value))
        return candidate.is_finite() and candidate == observed
    except (InvalidOperation, ValueError):
        return False


def _causal_label(label: str) -> bool:
    return bool(_CAUSAL_RE.search(label or ""))


def _match_observation(item: ClaimInput, observations: Iterable[_PrivateObservation]) -> _PrivateObservation | None:
    matches = [obs for obs in observations if (
        obs.public.semantic_field == item.semantic_field
        and _observation_value_matches(item, obs)
    )]
    # ``locator`` is a corroborating HINT: prefer the exact row when it
    # matches, otherwise fall back to the field+verbatim-value match (models
    # reorder result rows for display and mis-copy row indices). The value must
    # still exist VERBATIM under that semantic_field in trusted output, and
    # aggregate operand distinctness is separately enforced by evidence-id
    # dedup (_distinct_operand_reason), so the fallback stays fail-closed.
    if item.locator:
        narrowed = [obs for obs in matches if obs.public.locator == item.locator]
        matches = narrowed or matches
    if item.evidence_id:
        matches = [obs for obs in matches if obs.public.evidence_id == item.evidence_id]
    if item.source_timestamp is not None:
        timestamp = _as_utc(item.source_timestamp)
        matches = [obs for obs in matches if obs.public.source_observed_end == timestamp]
    return matches[0] if matches else None


def _operation_reason(claim: AnswerClaim) -> ReasonCode | None:
    if claim.kind == ClaimKind.RANKING and not claim.inputs:
        return ReasonCode.RANKING_INPUTS_REQUIRED
    if claim.kind in {ClaimKind.AGGREGATE, ClaimKind.RANKING, ClaimKind.COMPARISON} and claim.operation == "none":
        return ReasonCode.OPERATION_REQUIRED
    if claim.kind == ClaimKind.VALUE and claim.operation != "none":
        return ReasonCode.OPERATION_REQUIRED
    return None


def _distinct_operand_reason(
    claim: AnswerClaim, evidence: dict[str, SourceEvidence] | None = None,
) -> ReasonCode | None:
    operations = {"sum", "count", "average", "difference", "min", "max", "rank_asc", "rank_desc"}
    if claim.kind == ClaimKind.COMPARISON or claim.operation not in operations or len(claim.inputs) < 2:
        return None
    evidence_ids = [item.evidence_id for item in claim.inputs]
    if any(not value for value in evidence_ids) or len(set(evidence_ids)) != len(evidence_ids):
        return ReasonCode.DISTINCT_EVIDENCE_OPERANDS_REQUIRED
    if evidence is None:
        locators = [(item.semantic_field, item.locator) for item in claim.inputs]
    else:
        locators = [(
            evidence[item.evidence_id].system_id, evidence[item.evidence_id].boundary,
            evidence[item.evidence_id].tool_name, item.semantic_field, item.locator,
        ) for item in claim.inputs]
    return ReasonCode.DISTINCT_EVIDENCE_OPERANDS_REQUIRED if len(set(locators)) != len(locators) else None


def _claim_derivation_reason(claim: AnswerClaim, derived: Any | None) -> ReasonCode | None:
    if derived is not None:
        return None
    if claim.kind == ClaimKind.RANKING:
        return ReasonCode.RANKING_INPUT_UNSUPPORTED
    return ReasonCode.ARITHMETIC_INPUT_UNSUPPORTED if claim.operation != "none" else ReasonCode.VALUE_NOT_IN_SOURCE


def _current_reason(evidence: Iterable[SourceEvidence]) -> ReasonCode | None:
    items = list(evidence)
    if any(item.freshness == FreshnessClass.UNKNOWN for item in items):
        return ReasonCode.CURRENT_EVIDENCE_REQUIRED
    if any(item.freshness == FreshnessClass.HISTORICAL for item in items):
        return ReasonCode.STALE_CURRENT_DATA
    return None


def _bind_system_claim(
    claim: AnswerClaim, state: _CaptureState,
) -> tuple[AnswerClaim | None, list[_PrivateObservation], ReasonCode | None, ReasonCode | None]:
    reason = _operation_reason(claim)
    if reason:
        return None, [], reason, None
    if claim.kind == ClaimKind.CAUSAL or _causal_label(claim.label):
        return None, [], ReasonCode.CAUSAL_CLAIMS_UNSUPPORTED, None
    if claim.kind == ClaimKind.IDENTITY:
        return None, [], ReasonCode.GRAPH_RELATIONSHIP_PROOF_REQUIRED, None
    if not claim.inputs:
        return None, [], ReasonCode.VALUE_NOT_IN_SOURCE, None
    matched = [_match_observation(item, state.observations) for item in claim.inputs]
    if any(item is None for item in matched):
        return None, [], ReasonCode.STRUCTURAL_LINEAGE_MISMATCH, None
    observations = [item for item in matched if item is not None]
    incomplete = _incomplete_operation_reason(claim, (item.public for item in observations))
    if incomplete:
        return None, observations, incomplete, None
    inputs = tuple(original.model_copy(update={
        "value": observation.canonical_value,
        "evidence_id": observation.public.evidence_id,
        "source_timestamp": observation.public.source_observed_end,
        "entity_label": observation.entity_label,
    }) for original, observation in zip(claim.inputs, observations))
    rebound = claim.model_copy(update={"inputs": inputs})
    distinct = _distinct_operand_reason(rebound)
    if distinct:
        return None, observations, distinct, None
    derived = _derive_claim_value(rebound)
    reason = _claim_derivation_reason(rebound, derived)
    if reason:
        return None, [], reason, None
    current = _current_reason(item.public for item in observations) if state.current_intent else None
    if current in {ReasonCode.CURRENT_EVIDENCE_REQUIRED, ReasonCode.STALE_CURRENT_DATA}:
        return None, observations, current, None
    accepted = rebound.model_copy(update={
        "value": derived,
        "evidence_ids": tuple(dict.fromkeys(item.public.evidence_id for item in observations)),
    })
    return accepted, observations, None, current


def _system_status(
    draft_status: str, state: _CaptureState, accepted: list[AnswerClaim],
    reasons: list[ReasonCode], rejected: bool,
) -> Completeness:
    status = Completeness.COMPLETE
    if Completeness.UNAVAILABLE in state.completeness:
        status = Completeness.PARTIAL if state.observations else Completeness.UNAVAILABLE
        reasons.append(ReasonCode.SOURCE_UNAVAILABLE)
    if Completeness.PARTIAL in state.completeness or ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in reasons:
        status = Completeness.PARTIAL
        reasons.append(ReasonCode.SOURCE_PARTIAL)
    if draft_status != "complete":
        status = Completeness.UNAVAILABLE if draft_status == "unavailable" and not accepted else Completeness.PARTIAL
        reasons.append(ReasonCode.INCOMPLETE_SYSTEM_RESPONSE)
    if rejected:
        status = Completeness.PARTIAL if state.observations else Completeness.UNAVAILABLE
        reasons.append(ReasonCode.INCOMPLETE_SYSTEM_RESPONSE)
    return status


def validate_system_answer(
    draft: SystemAnswerDraft, private: _CaptureState | Iterable[_PrivateObservation],
) -> SystemAnswer:
    state = private if isinstance(private, _CaptureState) else _CaptureState()
    if not isinstance(private, _CaptureState):
        state.observations.extend(private)
    accepted: list[AnswerClaim] = []
    used: dict[str, SourceEvidence] = {}
    reasons: list[ReasonCode] = list(state.reason_codes)
    if ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in reasons:
        reasons.append(ReasonCode.SOURCE_PARTIAL)
        return SystemAnswer(
            status=Completeness.PARTIAL.value, claims=(), evidence=(),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )
    rejected = False
    for claim in draft.claims:
        bound, observations, reason, warning = _bind_system_claim(claim, state)
        if reason:
            reasons.append(reason)
            if reason in {ReasonCode.CURRENT_EVIDENCE_REQUIRED, ReasonCode.STALE_CURRENT_DATA}:
                used.update((item.public.evidence_id, item.public) for item in observations)
            rejected = True
            continue
        if warning:
            reasons.append(warning)
        if bound:
            accepted.append(bound)
            used.update((item.public.evidence_id, item.public) for item in observations)
    status = _system_status(draft.status, state, accepted, reasons, rejected)
    if not state.observations:
        reasons.append(ReasonCode.NO_CURRENT_INVOCATION_EVIDENCE)
    return SystemAnswer(
        status=status.value, claims=tuple(accepted), evidence=tuple(used.values()),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def system_answer_from_result(result: Any, invocation_state: dict) -> SystemAnswer | None:
    structured = getattr(result, "structured_output", None)
    if structured is None:
        return None
    try:
        draft = structured if isinstance(structured, SystemAnswerDraft) else SystemAnswerDraft.model_validate(structured)
    except Exception:
        return None
    return validate_system_answer(draft, _state(invocation_state))


def _tool_target(tool_name: str) -> str:
    name = (tool_name or "").strip()
    if "___" in name:
        return name.split("___", 1)[0]
    return name[6:] if name.startswith("query_") else ""


def _proof_tuple(proof: RelationshipProof) -> tuple[str, ...]:
    return (
        proof.graph_record_id, proof.source_system, proof.source_identifier,
        proof.source_field, proof.relationship_type, proof.target_system,
        proof.target_identifier, proof.target_field, proof.direction,
    )


def _payload_bytes(payloads: list[Any]) -> int:
    total = 0
    for payload in payloads:
        try:
            total += len(json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return MAX_TOTAL_TOOL_PAYLOAD_BYTES + 1
    return total


def _capture_verified_relationships(payload: Any, context: IntegrityContext) -> None:
    if not isinstance(payload, dict) or payload.get("verified") is not True:
        return
    records = payload.get("records")
    if not isinstance(records, list):
        return
    for raw in records:
        try:
            proof = RelationshipProof.model_validate(raw)
        except Exception:  # nosec B112 - best-effort loop: skip the failed item and continue
            continue
        if len(context.trusted_relationships) >= MAX_GRAPH_TUPLES_PER_TURN:
            context.mark_budget_exceeded(); return
        context.trusted_relationships.add(_proof_tuple(proof))


def _inspect_orchestrator_envelope(raw_result: Any, context: IntegrityContext) -> list[Any]:
    payloads, overflow, completeness = raw_envelope_completeness(raw_result)
    if overflow:
        context.mark_budget_exceeded()
        context.capture_reason_codes.append(ReasonCode.SOURCE_PARTIAL)
    if completeness != Completeness.COMPLETE:
        context.capture_reason_codes.append(ReasonCode.SOURCE_PARTIAL)
        if completeness == Completeness.UNAVAILABLE:
            context.capture_reason_codes.append(ReasonCode.SOURCE_UNAVAILABLE)
    return payloads


def capture_orchestrator_tool_result(tool_name: str, raw_result: Any, context: IntegrityContext) -> None:
    context.tool_calls += 1
    payloads = _inspect_orchestrator_envelope(raw_result, context)
    context.payload_bytes += _payload_bytes(payloads)
    if context.tool_calls > MAX_TOOL_CALLS_PER_TURN or context.payload_bytes > MAX_TOTAL_TOOL_PAYLOAD_BYTES:
        context.mark_budget_exceeded(); return
    name = (tool_name or "").lower()
    if context.tool_system_ids is None:
        target = _tool_target(tool_name)
    else:
        target = context.tool_system_ids.get(tool_name, "")
        if "___" in tool_name and not target:
            context.capture_reason_codes.append(ReasonCode.SYSTEM_TARGET_MISMATCH)
            return
    if name.endswith("explore_graph") or name == "explore_graph":
        context.graph_queried = True
        return  # arbitrary Cypher projections are informational and never proof
    if name.endswith("verify_relationship") or name == "verify_relationship":
        if target and target != "digital-thread":
            context.capture_reason_codes.append(ReasonCode.SYSTEM_TARGET_MISMATCH); return
        for payload in payloads:
            _capture_verified_relationships(payload, context)
        return
    if target and not payloads:
        context.capture_reason_codes.append(ReasonCode.SOURCE_PARTIAL)
        return
    parsed_system_answer = False
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        try:
            answer = SystemAnswer.model_validate(payload)
        except Exception:  # nosec B112 - best-effort loop: skip the failed item and continue
            continue
        parsed_system_answer = True
        systems = {evidence.system_id for evidence in answer.evidence}
        if not target or not systems or systems != {target}:
            context.capture_reason_codes.append(ReasonCode.SYSTEM_TARGET_MISMATCH); continue
        context.systems.update(systems)
        if len(context.systems) > MAX_SYSTEMS_PER_TURN:
            context.mark_budget_exceeded(); return
        if len(context.system_answers) >= MAX_SYSTEM_ANSWERS_PER_TURN:
            context.mark_budget_exceeded(); return
        context.system_answers.append(answer)
    if target and not parsed_system_answer:
        context.capture_reason_codes.append(ReasonCode.SOURCE_PARTIAL)


def _evidence_map(context: IntegrityContext) -> dict[str, SourceEvidence]:
    return {e.evidence_id: e for answer in context.system_answers for e in answer.evidence}


def _supported_inputs(context: IntegrityContext) -> set[tuple[str, str, str, str]]:
    return {
        (item.evidence_id, item.semantic_field, item.locator, _fingerprint(item.value))
        for answer in context.system_answers for claim in answer.claims for item in claim.inputs
    }


def _supported_ranking_inputs(context: IntegrityContext) -> set[tuple[str, str, str, str, str]]:
    return {
        (item.evidence_id, item.semantic_field, item.locator, _fingerprint(item.value), item.entity_label)
        for answer in context.system_answers for claim in answer.claims for item in claim.inputs
    }


def _proof_supported(proof: RelationshipProof | None, context: IntegrityContext) -> bool:
    return bool(proof and _proof_tuple(proof) in context.trusted_relationships)


def _validate_comparison(claim: AnswerClaim, evidence: dict[str, SourceEvidence]) -> ReasonCode | None:
    if len(claim.inputs) != 2 or {item.temporal_role for item in claim.inputs} != {"past", "current"}:
        return ReasonCode.COMPARISON_TIMESTAMPS_REQUIRED
    if len({item.evidence_id for item in claim.inputs}) != 2:
        return ReasonCode.COMPARISON_EVIDENCE_NOT_DISTINCT
    past = next(item for item in claim.inputs if item.temporal_role == "past")
    current = next(item for item in claim.inputs if item.temporal_role == "current")
    past_ev, current_ev = evidence[past.evidence_id], evidence[current.evidence_id]
    if not past_ev.source_observed_end or not current_ev.source_observed_end:
        return ReasonCode.CURRENT_EVIDENCE_REQUIRED if not current_ev.source_observed_end else ReasonCode.COMPARISON_TIMESTAMPS_REQUIRED
    if current_ev.freshness != FreshnessClass.CURRENT:
        return ReasonCode.STALE_CURRENT_DATA
    if past.source_timestamp != past_ev.source_observed_end or current.source_timestamp != current_ev.source_observed_end:
        return ReasonCode.CLAIMED_SOURCE_TIME_CONFLICT
    return None if past_ev.source_observed_end < current_ev.source_observed_end else ReasonCode.COMPARISON_TEMPORAL_ORDER_INVALID


def _identity_endpoints_match(
    claim: AnswerClaim, proof: RelationshipProof, evidence: dict[str, SourceEvidence],
) -> bool:
    if len(claim.inputs) != 2 or len({item.evidence_id for item in claim.inputs}) != 2:
        return False
    expected = {
        (proof.source_system, proof.source_field, proof.source_identifier),
        (proof.target_system, proof.target_field, proof.target_identifier),
    }
    actual = {
        (evidence[item.evidence_id].system_id, evidence[item.evidence_id].semantic_field, str(item.value))
        for item in claim.inputs
    }
    return actual == expected


def _proposal_endpoint_matches(
    system: str, field: str, value: str, ids: set[str],
    bound: list[tuple[ClaimInput, SourceEvidence]], *, value_optional: bool = False,
) -> bool:
    return any(
        item.evidence_id in ids and evidence.system_id == system
        and evidence.semantic_field == field
        and (value_optional and not value or str(item.value) == value)
        for item, evidence in bound
    )


def _proposal_has_exact_evidence(
    proposal: LearningProposal, ids: set[str],
    bound: list[tuple[ClaimInput, SourceEvidence]],
) -> bool:
    if proposal.kind == LearningProposalKind.FIELD_ANNOTATION:
        return any(
            item.evidence_id in ids and evidence.system_id == proposal.from_system
            and evidence.physical_field == proposal.from_field
            for item, evidence in bound
        )
    source = _proposal_endpoint_matches(
        proposal.from_system, proposal.from_field, proposal.from_value, ids, bound,
    )
    if proposal.kind != LearningProposalKind.VALUE_MAPPING:
        return False
    return source and _proposal_endpoint_matches(
        proposal.to_system, proposal.to_field, proposal.to_value, ids, bound,
    )


def _validate_proposals(
    proposals: Iterable[LearningProposal], claims: Iterable[AnswerClaim],
    evidence: dict[str, SourceEvidence], context: IntegrityContext,
) -> tuple[LearningProposal, ...]:
    bound = [
        (item, evidence[item.evidence_id]) for claim in claims for item in claim.inputs
        if item.evidence_id in evidence
    ]
    used = {item.evidence_id for item, _ in bound}
    accepted: list[LearningProposal] = []
    for proposal in proposals:
        ids = set(proposal.evidence_ids)
        if not ids or not ids.issubset(used):
            continue
        if any(sensitive_field(field) for field in (proposal.from_field, proposal.to_field)):
            continue
        if any(sensitive_text(value) for value in (proposal.from_value, proposal.to_value, proposal.concept)):
            continue
        if not actionable_proposal(proposal):
            continue
        if not _proposal_has_exact_evidence(proposal, ids, bound):
            continue
        if proposal.kind == LearningProposalKind.VALUE_MAPPING and not _proof_supported(
            proposal.relationship_proof, context,
        ):
            continue
        accepted.append(proposal)
        if len(accepted) >= MAX_PROPOSALS_PER_TURN:
            break
    return tuple(accepted)


def _base_claim_reason(claim: AnswerClaim) -> ReasonCode | None:
    operation = _operation_reason(claim)
    if operation:
        return operation
    if claim.kind == ClaimKind.CAUSAL or _causal_label(claim.label):
        return ReasonCode.CAUSAL_CLAIMS_UNSUPPORTED
    if not claim.inputs:
        return ReasonCode.RANKING_INPUTS_REQUIRED if claim.kind == ClaimKind.RANKING else ReasonCode.VALUE_NOT_IN_SOURCE
    return None


def _bind_final_claim(
    claim: AnswerClaim, context: IntegrityContext, evidence: dict[str, SourceEvidence],
    supported: set[tuple[str, str, str, str]],
    ranking_supported: set[tuple[str, str, str, str, str]],
) -> tuple[AnswerClaim | None, ReasonCode | None, ReasonCode | None]:
    reason = _base_claim_reason(claim)
    if reason:
        return None, reason, None
    if any(item.evidence_id not in evidence for item in claim.inputs):
        return None, ReasonCode.EVIDENCE_REFERENCE_MISSING, None
    distinct = _distinct_operand_reason(claim, evidence)
    if distinct:
        return None, distinct, None
    if any((item.evidence_id, item.semantic_field, item.locator, _fingerprint(item.value)) not in supported for item in claim.inputs):
        reason = ReasonCode.RANKING_INPUT_UNSUPPORTED if claim.kind == ClaimKind.RANKING else ReasonCode.ARITHMETIC_INPUT_UNSUPPORTED
        return None, reason, None
    if claim.kind == ClaimKind.RANKING and any(
        (item.evidence_id, item.semantic_field, item.locator, _fingerprint(item.value), item.entity_label)
        not in ranking_supported for item in claim.inputs
    ):
        return None, ReasonCode.RANKING_INPUT_UNSUPPORTED, None
    if any(item.source_timestamp is not None and _as_utc(item.source_timestamp) != evidence[item.evidence_id].source_observed_end for item in claim.inputs):
        return None, ReasonCode.CLAIMED_SOURCE_TIME_CONFLICT, None
    if claim.kind == ClaimKind.IDENTITY:
        proof = claim.relationship_proof
        if not _proof_supported(proof, context):
            return None, ReasonCode.GRAPH_RELATIONSHIP_PROOF_REQUIRED, None
        if proof is None or not _identity_endpoints_match(claim, proof, evidence):
            return None, ReasonCode.IDENTITY_ENDPOINT_MISMATCH, None
        derived = None
    else:
        if claim.kind == ClaimKind.COMPARISON:
            reason = _validate_comparison(claim, evidence)
            if reason:
                return None, reason, None
        derived = _derive_claim_value(claim)
        reason = _claim_derivation_reason(claim, derived)
        if reason:
            return None, reason, None
    claim_evidence = [evidence[item.evidence_id] for item in claim.inputs]
    if incomplete := _incomplete_operation_reason(claim, claim_evidence):
        return None, incomplete, None
    current = _current_reason(claim_evidence) if context.current_intent and claim.kind != ClaimKind.COMPARISON else None
    if current in {ReasonCode.CURRENT_EVIDENCE_REQUIRED, ReasonCode.STALE_CURRENT_DATA}:
        return None, current, None
    accepted = claim.model_copy(update={
        "value": derived,
        "evidence_ids": tuple(dict.fromkeys(item.evidence_id for item in claim.inputs)),
    })
    return accepted, None, current


def _finalize_answer(
    draft: FinalAnswerDraft, context: IntegrityContext, evidence: dict[str, SourceEvidence],
    accepted: list[AnswerClaim], reasons: list[ReasonCode], rejected: bool,
) -> FinalAnswer:
    for answer in context.system_answers:
        if answer.status == "partial":
            reasons.append(ReasonCode.SOURCE_PARTIAL)
        elif answer.status == "unavailable":
            reasons.append(ReasonCode.SOURCE_UNAVAILABLE)
        reasons.extend(answer.reason_codes)
    used_ids = {eid for claim in accepted for eid in claim.evidence_ids}
    if any(code in reasons for code in (ReasonCode.CURRENT_EVIDENCE_REQUIRED, ReasonCode.STALE_CURRENT_DATA)):
        used_ids.update(evidence)
    if ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in reasons:
        accepted.clear()
        used_ids.clear()
    public_evidence = tuple(evidence[eid] for eid in sorted(used_ids) if eid in evidence)
    incomplete = bool(reasons) or any(answer.status != "complete" for answer in context.system_answers)
    proposals = () if incomplete or rejected else _validate_proposals(draft.proposals, accepted, evidence, context)
    expected = min(len(draft.proposals), MAX_PROPOSALS_PER_TURN)
    if draft.proposals and len(proposals) != expected:
        reasons.append(ReasonCode.LEARNING_PROPOSAL_NON_ACTIONABLE)
    reason_codes = tuple(dict.fromkeys(reasons))
    complete = bool(accepted) and not reason_codes and all(answer.status == "complete" for answer in context.system_answers)
    learning_allowed = complete and not rejected and len(proposals) == len(draft.proposals)
    return FinalAnswer(
        claims=tuple(accepted), evidence=public_evidence, proposals=proposals,
        reason_codes=reason_codes, complete=complete, learning_allowed=learning_allowed,
        current_intent=context.current_intent,
    )


def _shadow_bound_evidence(
    evidence: dict[str, SourceEvidence],
) -> tuple[dict[str, SourceEvidence], str]:
    """Server-derived concept bindings on a PRIVATE COPY of the evidence map.

    The copy is the whole point: the claim binders below keep the caller's
    unbound map, so no binding this read produces can reach a claim, a reason
    code, or a rendered byte — the shadow posture is structural here, not merely
    tested. ``agents.concept_bindings`` owns the bounded, revision-pinned graph
    read and answers ``({}, "")`` for every uncertainty, which leaves bindings
    ``unresolved`` and the verdict ``unknown``.
    """
    from .concept_bindings import read_field_concepts
    from .response_integrity_concepts import resolve_evidence_concepts

    items = tuple(evidence.values())
    field_concepts, revision = read_field_concepts(items)
    if not field_concepts or not revision:
        return evidence, ""
    resolved = resolve_evidence_concepts(items, field_concepts, revision)
    return {item.evidence_id: item for item in resolved}, revision


def _shadow_concept_check(draft: FinalAnswerDraft, evidence: dict[str, SourceEvidence],
                          context: IntegrityContext) -> None:
    """ADR-0067 SHADOW-ONLY label-vs-provenance check.

    Runs alongside the claim binders but writes ONLY ``context.concept_shadow``
    — it never adds a reason code, never rejects a claim, and never changes a
    byte of the answer. Fail-open by construction: an internal defect here must
    not fail a real turn, so the one broad guard is deliberate.

    A turn whose claims propose NO concept id exits before touching the graph:
    there is nothing to check, so it must cost nothing.
    """
    try:
        from .response_integrity_concepts import concept_shadow_check
        if not any(str(claim.proposed_concept_id or "").strip()
                   for claim in draft.claims):
            context.concept_shadow = None
            return
        bound, revision = _shadow_bound_evidence(evidence)
        context.concept_shadow = concept_shadow_check(draft.claims, bound, revision)
    except Exception as exc:  # nosec B110 - shadow surface must never gate a turn
        context.concept_shadow = None
        logger.warning("concept shadow check failed open: %r", exc)


def validate_final_answer(draft: FinalAnswerDraft | None, context: IntegrityContext) -> FinalAnswer:
    evidence = _evidence_map(context)
    supported = _supported_inputs(context)
    ranking_supported = _supported_ranking_inputs(context)
    accepted: list[AnswerClaim] = []
    reasons: list[ReasonCode] = list(context.capture_reason_codes)
    rejected = False
    if draft is None:
        draft = FinalAnswerDraft()
        reasons.append(ReasonCode.INVALID_TYPED_ENVELOPE)
    _shadow_concept_check(draft, evidence, context)
    if not context.system_answers:
        reasons.append(ReasonCode.NO_CURRENT_INVOCATION_EVIDENCE)
    for claim in draft.claims:
        bound, reason, warning = _bind_final_claim(
            claim, context, evidence, supported, ranking_supported,
        )
        if reason:
            reasons.append(reason)
            rejected = True
            continue
        if warning:
            reasons.append(warning)
        if bound:
            accepted.append(bound)
    return _finalize_answer(draft, context, evidence, accepted, reasons, rejected)


def final_draft_from_result(result: Any) -> FinalAnswerDraft | None:
    structured = getattr(result, "structured_output", None)
    if structured is None:
        return None
    try:
        return structured if isinstance(structured, FinalAnswerDraft) else FinalAnswerDraft.model_validate(structured)
    except Exception:
        return None
