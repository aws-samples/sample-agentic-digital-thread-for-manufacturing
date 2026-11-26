# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SHADOW-ONLY deterministic label-vs-provenance check (ADR-0067).

WHY THIS EXISTS
---------------
The observed failure this catches is NOT fabrication: an answer presented
genuine, fully evidenced rows from one canonical concept's field under a label
claiming a DIFFERENT concept. Every token verified against captured
observations, so the annotate surface (ADR-0047) passed it unchanged, and the
typed-evidence gate (ADR-0046) had no vocabulary to reject it — trusted
evidence carried ``system_id``/``semantic_field``/``physical_field`` but no
canonical concept id, and ``AnswerClaim`` carried only a model-authored
free-text label. This module supplies the missing binding and the deterministic
rule over it.

CONTRACT
--------
* **Server-derived bindings, never model-authored.** Evidence concept bindings
  (``SourceEvidence.concept_ids`` / ``concept_binding`` /
  ``concept_schema_revision``) are filled ONLY by :func:`resolve_evidence_concepts`
  from already-read graph field-to-concept mapping rows pinned to a schema
  revision. A claim's ``proposed_concept_id`` is an LLM-proposed typed draft
  field; the server decides (ADR-0046 authority rules).
* **Computed, never LLM-judged.** No model call participates anywhere in this
  module. The rule inspects the EXACT evidence field each claim cites — never
  merely whether the source system carries *some* field for the concept (one
  system may legitimately hold several related concepts in different fields).
* **Fail to ``unknown``, never to ``violation``.** Absence is asserted only
  from an affirmatively COMPLETE field mapping at a pinned, matching schema
  revision. Graph read failure (unresolved binding), a capped mapping set, an
  unmapped physical field, an unpinned or stale schema revision, and missing
  evidence ALL yield ``unknown``.
* **SHADOW ONLY.** The verdict is a bounded log/telemetry sidecar. Zero answer
  bytes change, nothing blocks, no ``ReasonCode``/error type is added, no mode
  flips. ``CONCEPT_EVIDENCE_MISMATCH`` exists only in this sidecar. Enforcement
  criteria live in ADR-0067 and are explicitly out of scope here.
* **Bounded.** The sidecar carries verdicts, ids, and reason words only — no
  answer excerpts, no raw rows, no model rationale — and is capped in entry
  count and serialized size.

Pure and stdlib-only (plus the frozen pydantic contracts): no graph client, no
AWS, no LLM. Callers supply already-read rows, mirroring
``agents/concept_authority.py``.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .response_integrity_contracts import AnswerClaim, SourceEvidence

# Verdicts (pinned three-value vocabulary).
VERDICT_SUPPORTED = "supported"
VERDICT_VIOLATION = "violation"
VERDICT_UNKNOWN = "unknown"

# The one violation reason. Deliberately NOT a ReasonCode member: it must never
# reach an answer envelope while the check is shadow-only.
CONCEPT_EVIDENCE_MISMATCH = "CONCEPT_EVIDENCE_MISMATCH"

# Binding states carried on SourceEvidence. Only COMPLETE permits absence
# inference; CAPPED is a mapping list that hit a read cap (may be truncated);
# UNRESOLVED is the default (no resolver ran, or the graph read failed).
BINDING_UNRESOLVED = "unresolved"
BINDING_COMPLETE = "complete"
BINDING_CAPPED = "capped"

# Reason words for supported / unknown entries. Priority order for unknown is
# the tuple order below (first matching wins) — deterministic, no scoring.
REASON_SUPPORTED = "field_maps_to_claimed_concept"
REASON_NO_EVIDENCE = "no_evidence"
REASON_STALE_REVISION = "stale_schema_revision"
REASON_UNPINNED_REVISION = "unpinned_schema_revision"
REASON_UNBOUND = "unbound_field"
REASON_CAPPED = "capped_mapping"
REASON_UNMAPPED = "unmapped_field"
_UNKNOWN_PRIORITY = (
    REASON_NO_EVIDENCE, REASON_STALE_REVISION, REASON_UNPINNED_REVISION,
    REASON_UNBOUND, REASON_CAPPED, REASON_UNMAPPED,
)

# Sidecar bounds.
MAX_SIDECAR_ENTRIES = 32
MAX_CONCEPTS_PER_ENTRY = 8
MAX_SYSTEMS_PER_ENTRY = 8
MAX_SIDECAR_CHARS = 6_000


def _norm(value: object) -> str:
    return str(value or "").strip().casefold()


def resolve_evidence_concepts(
    evidence: Iterable[SourceEvidence],
    field_concepts: Mapping[tuple[str, str], Mapping[str, Any]],
    schema_revision: str,
) -> tuple[SourceEvidence, ...]:
    """Fill server-derived concept bindings onto evidence items.

    ``field_concepts`` maps ``(system_id, field)`` — field spelled as the graph
    knows it (physical or semantic) — to ``{"concepts": [...], "complete": bool}``
    rows the CALLER already read from the graph. ``schema_revision`` is the
    opaque revision those rows were read at; without a nonempty pin no
    completeness claim is trustworthy, so evidence is returned unchanged
    (all bindings stay unresolved ⇒ the checker yields ``unknown``).
    """
    if not str(schema_revision or "").strip():
        return tuple(evidence)
    index = {(_norm(system), _norm(field)): row
             for (system, field), row in field_concepts.items()}
    resolved: list[SourceEvidence] = []
    for item in evidence:
        row = (index.get((_norm(item.system_id), _norm(item.physical_field)))
               or index.get((_norm(item.system_id), _norm(item.semantic_field))))
        if row is None:
            resolved.append(item)
            continue
        concepts = tuple(dict.fromkeys(
            _norm(name) for name in (row.get("concepts") or ()) if _norm(name)
        ))[:MAX_CONCEPTS_PER_ENTRY]
        state = BINDING_COMPLETE if row.get("complete") is True else BINDING_CAPPED
        resolved.append(item.model_copy(update={
            "concept_ids": concepts,
            "concept_binding": state,
            "concept_schema_revision": str(schema_revision).strip()[:100],
        }))
    return tuple(resolved)


def _claim_evidence(
    claim: AnswerClaim, evidence: Mapping[str, SourceEvidence],
) -> list[SourceEvidence]:
    ids = dict.fromkeys(
        [item.evidence_id for item in claim.inputs if item.evidence_id]
        + list(claim.evidence_ids)
    )
    return [evidence[eid] for eid in ids if eid in evidence]


def _classify_item(
    item: SourceEvidence, expected_revision: str | None,
) -> tuple[str | None, frozenset[str]]:
    """Return ``(unknown_reason_or_None, positively_mapped_concepts)``."""
    if item.concept_binding == BINDING_UNRESOLVED:
        return REASON_UNBOUND, frozenset()
    revision = item.concept_schema_revision.strip()
    if not revision:
        return REASON_UNPINNED_REVISION, frozenset()
    if expected_revision is not None and revision != expected_revision:
        return REASON_STALE_REVISION, frozenset()
    if item.concept_binding == BINDING_CAPPED:
        return REASON_CAPPED, frozenset(item.concept_ids)
    if not item.concept_ids:
        return REASON_UNMAPPED, frozenset()
    return None, frozenset(item.concept_ids)


def _decide(supported: bool, unknown_reasons: set[str]) -> tuple[str, str]:
    if supported:
        return VERDICT_SUPPORTED, REASON_SUPPORTED
    if unknown_reasons:
        return VERDICT_UNKNOWN, next(
            r for r in _UNKNOWN_PRIORITY if r in unknown_reasons)
    return VERDICT_VIOLATION, CONCEPT_EVIDENCE_MISMATCH


def _claim_verdict(
    claim: AnswerClaim, evidence: Mapping[str, SourceEvidence],
    expected_revision: str | None,
) -> dict:
    """Deterministic three-value verdict for ONE concept-proposing claim.

    ``supported`` — some exact evidence field maps positively to the claimed
    concept (a positive mapping needs no completeness, so even a capped list
    may SUPPORT — it can never convict).
    ``violation`` — every evidence field is affirmatively completely mapped at
    a matching pinned revision, all map positively to other concepts, and none
    maps to the claimed concept (``CONCEPT_EVIDENCE_MISMATCH``).
    ``unknown`` — anything less than that affirmative basis.
    """
    claimed = _norm(claim.proposed_concept_id)
    items = _claim_evidence(claim, evidence)
    concepts: set[str] = set()
    systems: set[str] = set()
    unknown_reasons: set[str] = set()
    if not items:
        unknown_reasons.add(REASON_NO_EVIDENCE)
    resolved_revisions: set[str] = set()
    supported = False
    for item in items:
        systems.add(item.system_id)
        reason, mapped = _classify_item(item, expected_revision)
        concepts.update(mapped)
        if claimed in mapped:
            supported = True
        if reason is not None:
            unknown_reasons.add(reason)
        else:
            resolved_revisions.add(item.concept_schema_revision.strip())
    if expected_revision is None and len(resolved_revisions) > 1:
        unknown_reasons.add(REASON_STALE_REVISION)
    verdict, reason = _decide(supported, unknown_reasons)
    return {
        "claim_id": claim.claim_id[:100],
        "verdict": verdict,
        "claimed_concept": claimed[:160],
        "evidence_concepts": sorted(concepts)[:MAX_CONCEPTS_PER_ENTRY],
        "systems": sorted(systems)[:MAX_SYSTEMS_PER_ENTRY],
        "reason": reason,
    }


def _fit_sidecar(sidecar: dict) -> dict:
    """Drop tail entries until the serialized sidecar honors its hard cap."""
    def size(block: dict) -> int:
        return len(json.dumps(block, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")))
    while sidecar["claims"] and size(sidecar) > MAX_SIDECAR_CHARS:
        sidecar["claims"].pop()
        sidecar["truncated"] = True
    return sidecar


def concept_shadow_check(
    claims: Iterable[AnswerClaim],
    evidence: Iterable[SourceEvidence] | Mapping[str, SourceEvidence],
    expected_schema_revision: str | None = None,
) -> dict | None:
    """Run the shadow rule over every concept-proposing claim.

    Returns the bounded sidecar dict, or ``None`` when no claim proposes a
    concept — a turn with nothing to check emits nothing. The result is
    telemetry/log material ONLY: callers must never alter an answer, block a
    turn, or emit an error from it (ADR-0067 shadow posture).
    """
    if isinstance(evidence, Mapping):
        evidence_map = dict(evidence)
    else:
        evidence_map = {item.evidence_id: item for item in evidence}
    expected = None
    if expected_schema_revision is not None and str(expected_schema_revision).strip():
        expected = str(expected_schema_revision).strip()
    entries = [
        _claim_verdict(claim, evidence_map, expected)
        for claim in claims if _norm(claim.proposed_concept_id)
    ]
    if not entries:
        return None
    counts = {VERDICT_SUPPORTED: 0, VERDICT_VIOLATION: 0, VERDICT_UNKNOWN: 0}
    for entry in entries:
        counts[entry["verdict"]] += 1
    truncated = len(entries) > MAX_SIDECAR_ENTRIES
    return _fit_sidecar({
        "mode": "shadow",
        "check": "concept_evidence_binding",
        "checked": len(entries),
        "supported": counts[VERDICT_SUPPORTED],
        "violations": counts[VERDICT_VIOLATION],
        "unknown": counts[VERDICT_UNKNOWN],
        "claims": entries[:MAX_SIDECAR_ENTRIES],
        "truncated": truncated,
    })
