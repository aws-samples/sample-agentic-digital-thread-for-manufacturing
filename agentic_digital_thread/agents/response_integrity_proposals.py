# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure executable actionability policy for validated learning proposals."""
from __future__ import annotations

import re

from .response_integrity_contracts import LearningProposal, LearningProposalKind
from .response_integrity_security import classified_string_field, sensitive_field, sensitive_text

_SAFE_SYSTEM = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$-]{0,255}$")
_QUALIFIED_PHYSICAL_FIELD = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$-]*(?:\.[A-Za-z_][A-Za-z0-9_$-]*)+$"
)


def _safe_endpoint(system: str, field: str) -> bool:
    return bool(_SAFE_SYSTEM.fullmatch(system or "") and _SAFE_FIELD.fullmatch(field or ""))


def proposal_actionability_reason(proposal: LearningProposal) -> str | None:
    """Return a fixed policy reason, or ``None`` when persistence may store it."""
    fields = (proposal.from_field, proposal.to_field)
    if not _safe_endpoint(proposal.from_system, proposal.from_field):
        return "invalid_endpoint"
    if proposal.to_system or proposal.to_field:
        if not _safe_endpoint(proposal.to_system, proposal.to_field):
            return "invalid_endpoint"
    if any(field and sensitive_field(field) for field in fields):
        return "sensitive_field"
    if any(field and not classified_string_field(field) for field in fields):
        return "unclassified_field"
    if sensitive_text(proposal.concept):
        return "sensitive_concept"
    if proposal.kind == LearningProposalKind.FIELD_ANNOTATION:
        if proposal.from_value or proposal.to_value:
            return "field_annotation_contains_value"
        if not _QUALIFIED_PHYSICAL_FIELD.fullmatch(proposal.from_field or ""):
            return "qualified_physical_endpoint_required"
        return None
    if proposal.kind != LearningProposalKind.VALUE_MAPPING:
        return "unsupported_kind"
    proof = proposal.relationship_proof
    if proof is None:
        return "proof_required"
    endpoints = (
        proposal.from_system, proposal.from_value, proposal.from_field,
        proposal.to_system, proposal.to_value, proposal.to_field,
    )
    proven = (
        proof.source_system, proof.source_identifier, proof.source_field,
        proof.target_system, proof.target_identifier, proof.target_field,
    )
    if endpoints != proven:
        return "proof_endpoint_mismatch"
    if not all(classified_string_field(field) for field in fields):
        return "unclassified_field"
    values = (proposal.from_value, proposal.to_value)
    if not all(0 < len(value) <= 512 for value in values):
        return "invalid_endpoint_value"
    if any(sensitive_text(value) for value in values):
        return "sensitive_endpoint_value"
    return None


def actionable_proposal(proposal: LearningProposal) -> bool:
    return proposal_actionability_reason(proposal) is None
