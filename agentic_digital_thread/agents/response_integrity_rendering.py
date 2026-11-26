# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Safe, bounded server-owned rendering for validated Mode A answers."""
from __future__ import annotations

from typing import Any
import json
import logging
import re

from .response_integrity_capture import _iso
from .response_integrity_contracts import (
    MAX_FINAL_BUFFER_BYTES, ClaimKind, FinalAnswer, FreshnessClass, IntegrityContext,
    ReasonCode,
)
from .response_integrity_security import markdown_literal, sensitive_text
from .response_integrity_validation import final_draft_from_result, validate_final_answer

logger = logging.getLogger(__name__)

_EVIDENCE_ID_RE = re.compile(r"\Aev-[0-9a-f]{20}\Z")
_RENDERED_EVIDENCE_ID_RE = re.compile(r"ev\\-[0-9a-f]{20}")


def _citation_identifier(value: Any) -> str:
    """Render only an exact server evidence ID without generic PII redaction."""
    text = str(value)
    if _EVIDENCE_ID_RE.fullmatch(text):
        return text.replace("-", r"\-")
    return markdown_literal(text, max_length=100)


def _display_value(value: Any) -> str:
    if isinstance(value, float):
        return markdown_literal(f"{value:.12g}")
    if isinstance(value, list):
        return ", ".join(markdown_literal(item, max_length=160) for item in value[:100])
    return markdown_literal(value)


def _claim_prefix(claim, evidence) -> str:
    items = [item for item in evidence if item.evidence_id in claim.evidence_ids]
    historical = [item for item in items if item.freshness == FreshnessClass.HISTORICAL]
    if historical:
        latest = max(item.source_observed_end for item in historical if item.source_observed_end)
        return f"Latest available as of {_iso(latest)} — "
    if any(item.freshness == FreshnessClass.UNKNOWN for item in items):
        return "Source time unavailable — "
    return ""


def _identity_line(claim, refs: str) -> str:
    proof = claim.relationship_proof
    if proof is None:
        return "- Validated relationship unavailable."
    source = ".".join((
        markdown_literal(proof.source_system, max_length=100),
        markdown_literal(proof.source_field, max_length=200),
    ))
    target = ".".join((
        markdown_literal(proof.target_system, max_length=100),
        markdown_literal(proof.target_field, max_length=200),
    ))
    relation = markdown_literal(proof.relationship_type, max_length=100)
    record = markdown_literal(proof.graph_record_id, max_length=200)
    return (
        f"- **Validated relationship**: {source} identifier "
        f"{markdown_literal(proof.source_identifier, max_length=300)} {relation} "
        f"{target} identifier {markdown_literal(proof.target_identifier, max_length=300)} "
        f"(graph record {record}){refs}"
    )


def _claim_line(claim, evidence) -> str:
    refs = "".join(f" [{_citation_identifier(eid)}]" for eid in claim.evidence_ids)
    if claim.kind == ClaimKind.IDENTITY:
        return _identity_line(claim, refs)
    claim_evidence = [item for item in evidence if item.evidence_id in claim.evidence_ids]
    structural_label = " / ".join(dict.fromkeys(item.semantic_field for item in claim_evidence)) or "Validated value"
    label = markdown_literal(structural_label, max_length=256)
    value = _display_value(claim.value)
    prefix = _claim_prefix(claim, evidence)
    templates = {
        ClaimKind.VALUE: "{prefix}**{label}**: {value}",
        ClaimKind.AGGREGATE: "{prefix}**Computed {label}**: {value}",
        ClaimKind.RANKING: "{prefix}**Validated ranking — {label}**: {value}",
        ClaimKind.COMPARISON: "{prefix}**Computed change — {label}**: {value}",
        ClaimKind.CAUSAL: "**Unsupported causal claim**",
    }
    return "- " + templates[claim.kind].format(prefix=prefix, label=label, value=value) + refs


def _render_unbounded(answer: FinalAnswer) -> str:
    lines = ["## Answer"]
    if not answer.claims:
        lines.append("No requested value passed deterministic current-turn validation.")
    lines.extend(_claim_line(claim, answer.evidence) for claim in answer.claims)
    lines.extend(["", "## Freshness and limitations"])
    if answer.evidence:
        for evidence in answer.evidence:
            observed = _iso(evidence.source_observed_end) or "unknown (query time is not source freshness)"
            lines.append(
                f"- [{_citation_identifier(evidence.evidence_id)}] "
                f"system={markdown_literal(evidence.system_id or 'unspecified', max_length=100)}; "
                f"source={markdown_literal(str(evidence.boundary), max_length=20)}; "
                f"field={markdown_literal(evidence.semantic_field, max_length=256)}; "
                f"source_observed_at={observed}; queried_at={_iso(evidence.queried_at)}; "
                f"freshness={evidence.freshness.value}; completeness={evidence.completeness.value}."
            )
    else:
        lines.append("- No validated current-turn source evidence was available.")
    lines.extend(f"- Limitation [{reason.value}]." for reason in answer.reason_codes)
    if not answer.complete:
        lines.append("- This response is incomplete; unsupported claims were omitted.")
    return "\n".join(lines)


def _fixed_fallback(reason: ReasonCode) -> str:
    return (
        "## Answer\nNo requested value passed deterministic current-turn validation.\n\n"
        "## Freshness and limitations\n"
        "- No validated current-turn source evidence was available.\n"
        f"- Limitation [{reason.value}].\n"
        "- This response is incomplete; unsupported claims were omitted."
    )


def _sensitive_public_output(rendered: str) -> bool:
    scan_text = _RENDERED_EVIDENCE_ID_RE.sub("evidence-id", rendered)
    return sensitive_text(scan_text) or "REDACTED" in rendered


def render_final_answer(answer: FinalAnswer) -> str:
    """Render dynamic data literally, scanning again before public release."""
    rendered = _render_unbounded(answer)
    if _sensitive_public_output(rendered):
        return _fixed_fallback(ReasonCode.OUTPUT_SENSITIVITY_REJECTED)
    if len(rendered.encode("utf-8")) <= MAX_FINAL_BUFFER_BYTES:
        return rendered
    return _fixed_fallback(ReasonCode.TRAVERSAL_BUDGET_EXCEEDED)


def gate_final_result(result: Any, context: IntegrityContext) -> tuple[str, FinalAnswer]:
    validated = validate_final_answer(final_draft_from_result(result), context)
    if context.concept_shadow is not None:
        # ADR-0067 SHADOW-ONLY sidecar: bounded ids/verdicts/reason words only
        # (no answer excerpts, no raw rows, no model rationale). Logged for
        # telemetry; it never alters the rendered answer below.
        logger.info(
            "concept-shadow: %s",
            json.dumps(context.concept_shadow, ensure_ascii=False,
                       sort_keys=True, separators=(",", ":")),
        )
    rendered = render_final_answer(validated)
    if len(rendered.encode("utf-8")) <= MAX_FINAL_BUFFER_BYTES:
        return rendered, validated
    fallback = FinalAnswer(
        claims=(), evidence=(), proposals=(),
        reason_codes=tuple(dict.fromkeys((*validated.reason_codes, ReasonCode.TRAVERSAL_BUDGET_EXCEEDED))),
        complete=False, learning_allowed=False, current_intent=validated.current_intent,
    )
    return render_final_answer(fallback), fallback
