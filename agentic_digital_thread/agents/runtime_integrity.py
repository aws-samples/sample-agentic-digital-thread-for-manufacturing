# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Small deterministic helpers for runtime Mode A gating."""
from __future__ import annotations

import re
from typing import Any

from .response_integrity import FinalAnswer, MAX_FINAL_BUFFER_BYTES

MAX_PUBLIC_STREAM_BYTES = 5_000_000
MAX_TOOL_PROGRESS_CHARS = 2_000
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_TOOL_ENUMS = {
    "transport_status": {"success", "error", "unknown"},
    "app_status": {"success", "partial", "error", "unknown"},
    "safe_error_category": {
        "query_timeout", "scan_budget_exceeded", "scope_required", "invalid_request",
        "schema_mismatch", "sitewise_error", "tool_error", "transport_error",
        "tool_exception", "connection_error", "authorization_error",
    },
}
_TOOL_COUNTS = {
    "row_count", "item_count", "count", "success_count", "error_count",
    "scanned_bytes", "budget_bytes",
}
_INTEGRITY_ENUMS = {
    "verdict": {"accepted", "rejected"},
    "completeness": {"complete", "partial"},
}
_INTEGRITY_BOOLS = {"current_turn_evidence", "learning_allowed", "current_intent"}


def public_scalar_metadata(metadata: Any, *, integrity: bool = False) -> dict:
    """Rebuild public metadata from exact field/value policies."""
    if not isinstance(metadata, dict):
        return {}
    safe: dict[str, Any] = {}
    enums = _INTEGRITY_ENUMS if integrity else _TOOL_ENUMS
    for key, values in enums.items():
        value = metadata.get(key)
        if isinstance(value, str) and value in values:
            safe[key] = value
    if integrity:
        for key in _INTEGRITY_BOOLS:
            if isinstance(metadata.get(key), bool):
                safe[key] = metadata[key]
        value = metadata.get("claim_count")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
            safe["claim_count"] = value
        return safe
    for key in _TOOL_COUNTS:
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10**15:
            safe[key] = value
    if isinstance(metadata.get("truncated"), bool):
        safe["truncated"] = metadata["truncated"]
    tool = metadata.get("tool")
    if isinstance(tool, str) and _SAFE_NAME.fullmatch(tool):
        safe["tool"] = tool
    return safe


class BoundedTextBuffer:
    """UTF-8 byte-bounded private buffer used only by legacy Mode B learning."""

    def __init__(self, limit: int = MAX_FINAL_BUFFER_BYTES):
        self.limit = max(1, int(limit))
        self._parts: list[str] = []
        self._bytes = 0
        self.truncated = False

    def append(self, value: Any) -> None:
        if not isinstance(value, str) or self.truncated:
            return
        encoded = value.encode("utf-8", "replace")
        remaining = self.limit - self._bytes
        if len(encoded) > remaining:
            self.truncated = True
            if remaining:
                self._parts.append(encoded[:remaining].decode("utf-8", "ignore"))
                self._bytes = self.limit
            return
        self._parts.append(value)
        self._bytes += len(encoded)

    def text(self) -> str:
        return "".join(self._parts)


def safe_tool_progress(event: Any, registered_tools: set[str] | None = None) -> dict | None:
    """Allow only bounded progress for a tool registered on this invocation."""
    if not isinstance(event, dict):
        return None
    current = event.get("current_tool_use")
    if not isinstance(current, dict) or not current.get("name"):
        return None
    name = str(current.get("name") or "")
    if not _SAFE_NAME.fullmatch(name):
        return None
    if registered_tools is not None and name not in registered_tools:
        return None
    return {"current_tool_use": {
        "toolUseId": _public_identifier(current.get("toolUseId")),
        "name": name,
        "input": {},
    }}


def _public_identifier(value: Any) -> str:
    text = str(value or "")
    if _SAFE_NAME.fullmatch(text):
        return text
    import hashlib
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24]


def public_tool_result_event(*, tool_use_id: str, tool_name: str, metadata: dict) -> dict:
    """Build metadata-only tool progress; rows, values and identifiers are absent."""
    safe = public_scalar_metadata(metadata)
    name = tool_name if _SAFE_NAME.fullmatch(str(tool_name or "")) else "tool"
    return {"tool_result_metadata": {
        "toolUseId": _public_identifier(tool_use_id),
        "name": name,
        "metadata": safe,
    }}


def integrity_metadata(answer: FinalAnswer) -> dict:
    # ``mode`` is an ADDITIVE key (A-lite marker, C3): both emit sites of this
    # function are enforce-gated (``_invoke_integrity_runtime.final_events``
    # and the local ``_emit_local_integrity_result``), so the literal is the
    # path actually taken. ``public_scalar_metadata``/``stream_capture``
    # allowlist fields and simply omit it; the raw SSE metadata event —
    # what smoke_prompts reads — carries it.
    return {"response_integrity": {**answer.quality_metadata(), "mode": "enforce"}}


def _actionable_proposal(proposal) -> bool:
    from .response_integrity_proposals import actionable_proposal
    return actionable_proposal(proposal)


def _proposal_query_context(proposal) -> str:
    from .response_integrity_contracts import LearningProposalKind
    if proposal.kind == LearningProposalKind.FIELD_ANNOTATION:
        return str(proposal.concept or "semantic field annotation")[:500]
    return "Validated structural learning proposal"


def _persist_one(proposal, tool_calls_count: int) -> "str | None":
    """Route one typed proposal through ``log_proposed_mapping``; returns the
    mapping_id, or ``None`` when a learning gate dropped it. The
    ``deterministic-response-integrity`` provenance BYPASSES the ADR-0051 P5
    staging buffer (each proposal is bound to verified current-turn evidence
    ids — the ≥2-co-reference bar is met by construction)."""
    from .self_learning import log_proposed_mapping

    evidence = ",".join(proposal.evidence_ids)
    return log_proposed_mapping(
        from_system=proposal.from_system, from_field=proposal.from_field,
        from_value=proposal.from_value, to_system=proposal.to_system,
        to_field=proposal.to_field, to_value=proposal.to_value,
        concept=proposal.concept, confidence=0.9,
        query_context=_proposal_query_context(proposal),
        tool_calls_count=tool_calls_count, proposed_type=proposal.kind.value,
        provenance="deterministic-response-integrity",
        method="validated structural lineage",
        reason=f"current-turn evidence: {evidence}"[:500],
    )


def persist_typed_proposals(answer: FinalAnswer, *, tool_calls_count: int) -> int:
    """Persist only lifecycle-required safe values already bound to accepted proof.

    Counts a proposal as persisted ONLY when ``log_proposed_mapping`` returned
    a mapping_id: a gate-dropped (``None``) outcome is not a
    persisted row and must not inflate the reported count."""
    if not answer.learning_allowed or not answer.proposals:
        return 0
    persisted = 0
    for proposal in answer.proposals:
        if not _actionable_proposal(proposal):
            continue
        if _persist_one(proposal, tool_calls_count) is not None:
            persisted += 1
    return persisted
