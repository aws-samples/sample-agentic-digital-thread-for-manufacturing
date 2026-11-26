# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Immutable contracts and aggregate budgets for deterministic Mode A output."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
import re
from typing import Any, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr,
    field_validator, model_validator,
)

SOURCE_METADATA_KEY = "_adt_source"
PRIVATE_SOURCE_STATE_KEY = "_adt_private_source_evidence"
INTEGRITY_CONTEXT_STATE_KEY = "_adt_response_integrity"
DEFAULT_CURRENT_MAX_AGE_SECONDS = 300
FUTURE_TIMESTAMP_SKEW_SECONDS = 120
MAX_RESULT_BYTES = 200_000
MAX_TOTAL_TOOL_PAYLOAD_BYTES = 1_000_000
MAX_RESULT_NODES = 20_000
MAX_RESULT_DEPTH = 16
MAX_EVIDENCE_COUNT = 1_024
MAX_SCALAR_LENGTH = 4_096
MAX_LINEAGE_RECORDS = 1_024
MAX_FINAL_BUFFER_BYTES = 256_000
MAX_PROPOSALS_PER_TURN = 3
MAX_SYSTEM_ANSWERS_PER_TURN = 64
MAX_CLAIMS_PER_ANSWER = 256
MAX_INPUTS_PER_CLAIM = 64
MAX_SYSTEMS_PER_TURN = 32
MAX_TOOL_CALLS_PER_TURN = 64
MAX_GRAPH_TUPLES_PER_TURN = 128
MAX_PROGRESS_EVENTS = 256
MAX_NUMERIC_MAGNITUDE = Decimal("1e30")
MAX_NUMERIC_SIGNIFICANT_DIGITS = 18

_BOUNDARIES = Literal["sql", "athena", "sitewise", "i3x", "rest", "mcp", "external"]
Scalar = Decimal | StrictFloat | StrictInt | StrictStr | StrictBool
Operation = Literal[
    "none", "sum", "average", "difference", "min", "max", "count",
    "rank_asc", "rank_desc",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FreshnessClass(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"
    UNKNOWN = "unknown"


class Completeness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ClaimKind(str, Enum):
    VALUE = "value"
    AGGREGATE = "aggregate"
    RANKING = "ranking"
    COMPARISON = "comparison"
    IDENTITY = "identity"
    CAUSAL = "causal"


class LearningProposalKind(str, Enum):
    FIELD_ANNOTATION = "field_annotation"
    VALUE_MAPPING = "value_mapping"
    JOIN_PATH = "join_path"
    INSIGHT = "insight"


class ReasonCode(str, Enum):
    NO_CURRENT_INVOCATION_EVIDENCE = "NO_CURRENT_INVOCATION_EVIDENCE"
    VALUE_NOT_IN_SOURCE = "VALUE_NOT_IN_SOURCE"
    STRUCTURAL_LINEAGE_MISMATCH = "STRUCTURAL_LINEAGE_MISMATCH"
    EVIDENCE_REFERENCE_MISSING = "EVIDENCE_REFERENCE_MISSING"
    SOURCE_TIME_UNKNOWN = "SOURCE_TIME_UNKNOWN"
    CLAIMED_SOURCE_TIME_CONFLICT = "CLAIMED_SOURCE_TIME_CONFLICT"
    CURRENT_EVIDENCE_REQUIRED = "CURRENT_EVIDENCE_REQUIRED"
    STALE_CURRENT_DATA = "STALE_CURRENT_DATA"
    FUTURE_SOURCE_TIMESTAMP = "FUTURE_SOURCE_TIMESTAMP"
    COMPARISON_TIMESTAMPS_REQUIRED = "COMPARISON_TIMESTAMPS_REQUIRED"
    COMPARISON_TEMPORAL_ORDER_INVALID = "COMPARISON_TEMPORAL_ORDER_INVALID"
    COMPARISON_EVIDENCE_NOT_DISTINCT = "COMPARISON_EVIDENCE_NOT_DISTINCT"
    GRAPH_RELATIONSHIP_PROOF_REQUIRED = "GRAPH_RELATIONSHIP_PROOF_REQUIRED"
    IDENTITY_ENDPOINT_MISMATCH = "IDENTITY_ENDPOINT_MISMATCH"
    SYSTEM_TARGET_MISMATCH = "SYSTEM_TARGET_MISMATCH"
    CAUSAL_CLAIMS_UNSUPPORTED = "CAUSAL_CLAIMS_UNSUPPORTED"
    ARITHMETIC_INPUT_UNSUPPORTED = "ARITHMETIC_INPUT_UNSUPPORTED"
    RANKING_INPUT_UNSUPPORTED = "RANKING_INPUT_UNSUPPORTED"
    RANKING_INPUTS_REQUIRED = "RANKING_INPUTS_REQUIRED"
    OPERATION_REQUIRED = "OPERATION_REQUIRED"
    INVALID_TYPED_ENVELOPE = "INVALID_TYPED_ENVELOPE"
    INCOMPLETE_SYSTEM_RESPONSE = "INCOMPLETE_SYSTEM_RESPONSE"
    SOURCE_PARTIAL = "SOURCE_PARTIAL"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TRAVERSAL_BUDGET_EXCEEDED = "TRAVERSAL_BUDGET_EXCEEDED"
    LEARNING_PROPOSAL_UNSUPPORTED = "LEARNING_PROPOSAL_UNSUPPORTED"
    LEARNING_PROPOSAL_NON_ACTIONABLE = "LEARNING_PROPOSAL_NON_ACTIONABLE"
    DISTINCT_EVIDENCE_OPERANDS_REQUIRED = "DISTINCT_EVIDENCE_OPERANDS_REQUIRED"
    OUTPUT_SENSITIVITY_REJECTED = "OUTPUT_SENSITIVITY_REJECTED"


def _valid_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    digits = len(number.as_tuple().digits)
    return number.is_finite() and abs(number) <= MAX_NUMERIC_MAGNITUDE and digits <= MAX_NUMERIC_SIGNIFICANT_DIGITS


class SourceMetadata(_FrozenModel):
    evidence_id: str
    system_id: str = ""
    boundary: _BOUNDARIES
    tool_name: str = ""
    queried_at: datetime
    source_observed_start: datetime | None = None
    source_observed_end: datetime | None = None
    freshness: FreshnessClass = FreshnessClass.UNKNOWN
    completeness: Completeness = Completeness.COMPLETE
    row_count: int | None = Field(default=None, ge=0)
    item_count: int | None = Field(default=None, ge=0)


class SourceEvidence(SourceMetadata):
    """Public structural metadata; raw values and fingerprints remain private."""

    semantic_field: str = Field(min_length=1, max_length=256)
    physical_field: str = Field(default="", max_length=256)
    locator: str = Field(min_length=1, max_length=256)
    canonical_type: Literal["string", "integer", "number", "boolean"]
    # SERVER-DERIVED field-to-concept binding (ADR-0067; shadow-only consumer).
    # Filled ONLY by response_integrity_concepts.resolve_evidence_concepts from
    # already-read graph mapping rows at a pinned schema revision — never
    # model-authored. "complete" is the AFFIRMATIVE completeness marker: it is
    # the only state that permits absence inference; "capped" (read cap hit)
    # and the "unresolved" default always resolve to an "unknown" verdict.
    concept_ids: tuple[str, ...] = Field(default=(), max_length=16)
    concept_binding: Literal["unresolved", "complete", "capped"] = "unresolved"
    concept_schema_revision: str = Field(default="", max_length=100)


class ClaimInput(_FrozenModel):
    label: str = Field(min_length=1, max_length=160)
    value: Scalar
    evidence_id: str = Field(default="", max_length=100)
    semantic_field: str = Field(
        default="", max_length=256,
        description="EXACT column/property name from the tool result that produced "
                    "this value (e.g. 'cell', 'manufacturing_attempt_status').",
    )
    locator: str = Field(
        default="", max_length=256,
        description="Optional row locator within the tool result: 'row:<index>' "
                    "(0-based) for SQL rows. Omit when unsure — the server binds "
                    "by semantic_field + exact value.",
    )
    source_timestamp: datetime | None = None
    temporal_role: Literal["current", "past", "none"] = "none"
    entity_label: str = Field(default="", max_length=160)

    @field_validator("value")
    @classmethod
    def bounded_scalar(cls, value: Scalar) -> Scalar:
        if isinstance(value, str) and len(value) > MAX_SCALAR_LENGTH:
            raise ValueError("scalar exceeds bounded length")
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and not _valid_number(value):
            raise ValueError("numeric scalar is non-finite or outside bounded precision/magnitude")
        return value


class RelationshipProof(_FrozenModel):
    graph_record_id: str = Field(min_length=1, max_length=200)
    source_system: str = Field(min_length=1, max_length=100)
    source_identifier: str = Field(min_length=1, max_length=300)
    source_field: str = Field(min_length=1, max_length=200)
    target_system: str = Field(min_length=1, max_length=100)
    target_identifier: str = Field(min_length=1, max_length=300)
    target_field: str = Field(min_length=1, max_length=200)
    relationship_type: Literal["value_mapping"]
    direction: Literal["outbound"]


class AnswerClaim(_FrozenModel):
    claim_id: str = Field(min_length=1, max_length=100)
    kind: ClaimKind
    label: str = Field(min_length=1, max_length=160)
    proposed_concept_id: str = Field(
        default="", max_length=160,
        description="Optional canonical concept id this claim asserts (the concept "
                    "the label is about). Copy it EXACTLY as the route seed spells "
                    "it (a system's mapped_concepts/shared_concepts entry, or a "
                    "concept_authority boundary concept); never invent an id. "
                    "PROPOSED only: the server validates it against the evidence's "
                    "graph-derived concept bindings and "
                    "decides; it never changes the answer. Omit when the seed does "
                    "not name the concept, or when unsure.",
    )
    value: Scalar | list[str] | None = None
    operation: Operation = "none"
    inputs: tuple[ClaimInput, ...] = Field(default=(), max_length=MAX_INPUTS_PER_CLAIM)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=MAX_INPUTS_PER_CLAIM)
    relationship_proof: RelationshipProof | None = None

    @field_validator("value")
    @classmethod
    def bounded_claim_value(cls, value: Scalar | list[str] | None):
        if value is None:
            return value
        if isinstance(value, list):
            if len(value) > MAX_INPUTS_PER_CLAIM or any(len(item) > 160 for item in value):
                raise ValueError("claim list exceeds bounded cardinality or item length")
            return value
        if isinstance(value, str) and len(value) > MAX_SCALAR_LENGTH:
            raise ValueError("claim scalar exceeds bounded length")
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and not _valid_number(value):
            raise ValueError("claim numeric output is non-finite or outside bounds")
        return value

    @model_validator(mode="after")
    def validate_operation_inputs(self) -> "AnswerClaim":
        arithmetic = {"sum", "average", "difference", "min", "max"}
        if self.operation in arithmetic and any(not _valid_number(item.value) for item in self.inputs):
            raise ValueError("arithmetic operands must be finite bounded non-boolean numbers")
        return self


class LearningProposal(_FrozenModel):
    proposal_id: str = Field(min_length=1, max_length=100)
    kind: LearningProposalKind
    from_system: str = Field(min_length=1, max_length=100)
    from_field: str = Field(default="", max_length=256)
    from_value: str = Field(default="", max_length=512)
    to_system: str = Field(default="", max_length=100)
    to_field: str = Field(default="", max_length=256)
    to_value: str = Field(default="", max_length=512)
    concept: str = Field(min_length=1, max_length=160)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=MAX_INPUTS_PER_CLAIM)
    relationship_proof: RelationshipProof | None = None
    relationship_type: Literal["value_mapping", "field_annotation", "join_path", "insight"]
    direction: Literal["outbound", "self"]

    @model_validator(mode="after")
    def bind_relationship(self) -> "LearningProposal":
        if self.kind == LearningProposalKind.VALUE_MAPPING:
            proof = self.relationship_proof
            endpoints = (
                self.from_system, self.from_value, self.from_field,
                self.to_system, self.to_value, self.to_field,
            )
            proven = None if proof is None else (
                proof.source_system, proof.source_identifier, proof.source_field,
                proof.target_system, proof.target_identifier, proof.target_field,
            )
            if self.relationship_type != "value_mapping" or self.direction != "outbound" or endpoints != proven:
                raise ValueError("value mapping must exactly match its accepted proof")
        if self.kind == LearningProposalKind.FIELD_ANNOTATION:
            one_endpoint = not self.to_system and not self.to_field and not self.to_value
            same_endpoint = (
                self.from_system == self.to_system and self.from_field == self.to_field
                and self.from_value == self.to_value
            )
            if self.relationship_type != "field_annotation" or not (one_endpoint or same_endpoint):
                raise ValueError("field annotation target must be absent or identical")
        return self


class SystemAnswerDraft(_FrozenModel):
    status: Literal["complete", "partial", "unavailable"] = "complete"
    claims: tuple[AnswerClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_ANSWER)
    limitations: tuple[str, ...] = Field(default=(), max_length=64)


class SystemAnswer(_FrozenModel):
    status: Literal["complete", "partial", "unavailable"]
    claims: tuple[AnswerClaim, ...] = Field(max_length=MAX_CLAIMS_PER_ANSWER)
    evidence: tuple[SourceEvidence, ...] = Field(max_length=MAX_EVIDENCE_COUNT)
    reason_codes: tuple[ReasonCode, ...] = ()


class FinalAnswerDraft(_FrozenModel):
    claims: tuple[AnswerClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_ANSWER)
    proposals: tuple[LearningProposal, ...] = Field(default=(), max_length=64)
    limitations: tuple[str, ...] = Field(default=(), max_length=64)


class FinalAnswer(_FrozenModel):
    claims: tuple[AnswerClaim, ...] = Field(max_length=MAX_CLAIMS_PER_ANSWER)
    evidence: tuple[SourceEvidence, ...] = Field(max_length=MAX_EVIDENCE_COUNT)
    proposals: tuple[LearningProposal, ...] = Field(max_length=MAX_PROPOSALS_PER_TURN)
    reason_codes: tuple[ReasonCode, ...]
    complete: bool
    learning_allowed: bool
    current_intent: bool

    def quality_metadata(self) -> dict[str, Any]:
        return {
            "verdict": "accepted" if self.claims and not self.reason_codes else "rejected",
            "reason_codes": [code.value for code in self.reason_codes],
            "current_turn_evidence": bool(self.evidence),
            "completeness": "complete" if self.complete else "partial",
            "learning_allowed": self.learning_allowed,
            "claim_count": len(self.claims),
            "current_intent": self.current_intent,
        }


_CURRENT_RE = re.compile(
    r"\b(current|currently|latest|live|now|today|present|compare|comparison|versus|vs\.?|remaining|available|availability|open)\b",
    re.I,
)
_HISTORICAL_RE = re.compile(
    r"\b(historical|history|yesterday|previously|prior|ago|last\s+(?:week|month|year)|was|were|as\s+of)\b|\b\d{4}-\d{2}-\d{2}\b",
    re.I,
)


def requires_current_state(prompt: str) -> bool:
    """Require current evidence unless every substantive request clause is historical."""
    text = (prompt or "").strip()
    if not text:
        return False
    if _CURRENT_RE.search(text):
        return True
    clauses = [part.strip() for part in re.split(r"(?:\band\b|\bthen\b|[;!?]+)", text, flags=re.I) if part.strip()]
    return not clauses or not all(_HISTORICAL_RE.search(clause) for clause in clauses)


class IntegrityContext:
    """Mutable, invocation-local authority and aggregate budget state."""

    def __init__(
        self, prompt: str = "", tool_system_ids: dict[str, str] | None = None,
    ) -> None:
        self.system_answers: list[SystemAnswer] = []
        self.trusted_relationships: set[tuple[str, ...]] = set()
        self.graph_queried = False  # informational traversal only
        self.capture_reason_codes: list[ReasonCode] = []
        self.current_intent = requires_current_state(prompt) if prompt else False
        self.tool_system_ids = tool_system_ids
        self.tool_calls = 0
        self.payload_bytes = 0
        self.systems: set[str] = set()
        self.progress_events = 0
        # ADR-0067 shadow-only sidecar (dict | None). Written by
        # validate_final_answer, read by logging/telemetry only — it never
        # alters an answer, a verdict, or a reason code.
        self.concept_shadow: dict | None = None

    def mark_budget_exceeded(self) -> None:
        if ReasonCode.TRAVERSAL_BUDGET_EXCEEDED not in self.capture_reason_codes:
            self.capture_reason_codes.append(ReasonCode.TRAVERSAL_BUDGET_EXCEEDED)
