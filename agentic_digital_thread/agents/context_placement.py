# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic cross-system business-context placement checks.

The detector compares bounded terms from one system's steward context with the
schema and already-baked sample evidence of every other system. It never calls
an LLM or a source connector. Findings reuse the acknowledge-only
``coverage_gap`` lifecycle with ``source=context-placement``: accepting means
"seen" and performs zero graph writes; applying the finding remains the
steward's explicit business-context edit.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_SYSTEMS = 50
MAX_TERMS_PER_CONTEXT = 64
MAX_TERM_LENGTH = 80
MAX_EVIDENCE_VALUES = 1200
MAX_FINDINGS = 200
PLACEMENT_REASON = "business_context_missing"
PLACEMENT_SOURCE = "context-placement"

_QUOTED_RE = re.compile(r"(?<!\w)([\"'])([^\"'\n]{1,80})\1(?!\w)")
_CODE_RE = re.compile(
    r"(?<![\w])(?:"
    r"[A-Za-z][A-Za-z0-9_-]{1,63}\*|"
    r"[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|"
    r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|"
    r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r")(?![\w*])"
)
_SAMPLE_GROUP_RE = re.compile(r"\be\.g\.\s+([^;)]+)", re.IGNORECASE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def extract_candidate_terms(text: str, limit: int = MAX_TERMS_PER_CONTEXT) -> list[str]:
    """Extract unique quoted/code-shaped terms in source order, with hard caps."""
    if not isinstance(text, str) or limit <= 0:
        return []
    bounded = text[:2000]
    matches = [(m.start(), m.group(2)) for m in _QUOTED_RE.finditer(bounded)]
    matches.extend((m.start(), m.group(0)) for m in _CODE_RE.finditer(bounded))
    terms, seen = [], set()
    for _offset, raw in sorted(matches):
        term = raw.strip()
        key = _term_key(term)
        if len(term) < 2 or len(term) > MAX_TERM_LENGTH or not key or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= min(limit, MAX_TERMS_PER_CONTEXT):
            break
    return terms


def _term_key(value: str) -> str:
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


def _family_matches(term: str, value: str) -> bool:
    prefix = "".join(ch for ch in term[:-1] if ch.isalnum())
    candidate = "".join(ch for ch in str(value) if ch.isalnum())
    if not prefix or not candidate:
        return False
    if prefix.isupper():
        return candidate.startswith(prefix)
    return candidate.casefold().startswith(prefix.casefold())


def _matches_term(term: str, value: str) -> bool:
    if term.endswith("*"):
        return _family_matches(term, value)
    return _term_key(term) == _term_key(value)


def term_present_in_context(term: str, context: str) -> bool:
    """True when the target context already carries the term or its family."""
    if not isinstance(context, str) or not context:
        return False
    if term.endswith("*"):
        tokens = re.findall(r"[A-Za-z0-9_-]+\*?", context)
        return any(_matches_term(term, token) for token in tokens)
    parts = [re.escape(p) for p in re.split(
        r"[^A-Za-z0-9]+", _CAMEL_BOUNDARY_RE.sub(" ", term)) if p]
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"[\W_]*".join(parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, context, re.IGNORECASE) is not None


def _append_evidence(out: list, kind: str, ref: str, values) -> None:
    for value in values or []:
        if len(out) >= MAX_EVIDENCE_VALUES:
            return
        if value is not None and str(value).strip():
            out.append((str(value), kind, ref))


def _schema_evidence(schema: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for table, meta in sorted((schema or {}).items(), key=lambda item: str(item[0])):
        if not isinstance(meta, dict):
            continue
        for column in meta.get("columns") or []:
            name = column.get("name") if isinstance(column, dict) else column
            ref = f"{table}.{name}"
            _append_evidence(out, "schema-column", ref, [name])
            if isinstance(column, dict):
                _append_evidence(out, "sample-value", ref,
                                 column.get("sample_values") or [])
        samples = meta.get("sample_values") or {}
        if isinstance(samples, dict):
            for column, values in samples.items():
                _append_evidence(out, "sample-value", f"{table}.{column}", values)
    return out


def parse_cached_samples(tables_desc: str) -> list[str]:
    """Extract capped examples from the existing agent-config schema string."""
    out: list[str] = []
    if not isinstance(tables_desc, str):
        return out
    for match in _SAMPLE_GROUP_RE.finditer(tables_desc[:100_000]):
        _append_evidence(out, "", "", [v.strip() for v in match.group(1).split(",")])
        if len(out) >= MAX_EVIDENCE_VALUES:
            break
    return [item[0] if isinstance(item, tuple) else item for item in out]


def _load_cached_samples(system_id: str) -> list[str]:
    """Read baked samples once per system; never calls the source connector."""
    try:
        from .system_agent import _fetch_credentials
        config = _fetch_credentials(f"adt/agent-config/{system_id}") or {}
        return parse_cached_samples(config.get("tables_desc", ""))
    except Exception as exc:  # noqa: BLE001 - optional cache, schema remains evidence
        logger.info("context placement sample cache unavailable for %s: %s",
                    system_id, type(exc).__name__)
        return []


def _evidence_index(surface: dict) -> tuple[dict, list]:
    values = _schema_evidence(surface.get("schema") or {})
    _append_evidence(values, "sample-value", "cached-schema",
                     surface.get("cached_samples") or [])
    exact: dict[str, tuple[str, str]] = {}
    for value, kind, ref in values:
        exact.setdefault(_term_key(value), (kind, ref))
    return exact, values


def _find_evidence(term: str, index: tuple[dict, list]) -> tuple[str, str] | None:
    exact, values = index
    if not term.endswith("*"):
        return exact.get(_term_key(term))
    for value, kind, ref in values:
        if _matches_term(term, value):
            return kind, ref
    return None


def find_context_placement_hits(surfaces: dict, focus: set[str] | None = None) -> list[dict]:
    """Pure cross-system three-condition detector over preloaded surfaces."""
    findings, indexes = [], {sid: _evidence_index(s) for sid, s in surfaces.items()}
    for source_id in sorted(surfaces):
        terms = extract_candidate_terms(surfaces[source_id].get("context", ""))
        for target_id in sorted(surfaces):
            if source_id == target_id or (focus and not {source_id, target_id} & focus):
                continue
            target_context = surfaces[target_id].get("context", "")
            for term in terms:
                if term_present_in_context(term, target_context):
                    continue
                evidence = _find_evidence(term, indexes[target_id])
                if evidence:
                    findings.append({"term": term, "source_system": source_id,
                                     "target_system": target_id,
                                     "evidence_kind": evidence[0],
                                     "evidence_ref": evidence[1]})
                if len(findings) >= MAX_FINDINGS:
                    return findings
    return findings


def _proposal_fields(finding: dict) -> tuple[str, str]:
    concept = f"context-placement:{finding['term']}"
    to_field = f"business_context.{finding['source_system']}"
    return concept, to_field


def _file_context_placement_proposals(findings: list[dict]) -> int:
    from . import self_learning
    existing = {str(row.get("mapping_id")) for row in self_learning.get_pending_mappings()
                if row.get("proposed_type") == "coverage_gap"
                and row.get("source") == PLACEMENT_SOURCE}
    filed = 0
    for finding in findings:
        concept, to_field = _proposal_fields(finding)
        mapping_id = self_learning.log_proposed_mapping(
            finding["source_system"], "business_context", "",
            finding["target_system"], to_field, "", concept, 0.7,
            f"Context-placement check: term '{finding['term']}' occurs in "
            f"{finding['source_system']}'s business context and in "
            f"{finding['target_system']}'s {finding['evidence_kind']} evidence, "
            f"but is absent from that target's business context. Accepting only "
            f"acknowledges this finding; edit the target context explicitly.",
            proposed_type="coverage_gap", source=PLACEMENT_SOURCE,
            method="context-placement-evidence-match", reason=PLACEMENT_REASON)
        if mapping_id and mapping_id not in existing:
            existing.add(mapping_id)
            filed += 1
    return filed


def _current_target_keys(findings: list[dict]) -> set[str]:
    from .proposal_key import canonical_keys
    keys = set()
    for finding in findings:
        concept, to_field = _proposal_fields(finding)
        keys.add(canonical_keys("coverage_gap", to_system=finding["target_system"],
                                concept=concept, to_field=to_field,
                                reason=PLACEMENT_REASON).target_key)
    return keys


def _sweep_resolved(findings: list[dict], eligible: set[str], focus: set[str]) -> int:
    from . import self_learning
    from .value_promotion import _is_conditional_check_failed
    current, swept = _current_target_keys(findings), 0
    now = datetime.now(timezone.utc).isoformat()
    for row in self_learning.get_pending_mappings():
        endpoints = {str(row.get("from_system") or ""), str(row.get("to_system") or "")}
        if (row.get("proposed_type") != "coverage_gap"
                or row.get("source") != PLACEMENT_SOURCE
                or row.get("reason") != PLACEMENT_REASON
                or row.get("status") != "pending" or not endpoints <= eligible
                or (focus and not endpoints & focus)
                or str(row.get("target_key") or "") in current):
            continue
        try:
            self_learning._update_status(
                self_learning._get_table(), row["mapping_id"], row["discovered_at"],
                "superseded", {"superseded_at": now,
                               "superseded_reason": "context-placement-resolved"},
                expect_status="pending")
        except Exception as exc:
            if not _is_conditional_check_failed(exc):
                raise
            continue
        swept += 1
    return swept


def _load_surfaces(ids: list[str]) -> tuple[dict, list[dict]]:
    from .business_context import read_business_context
    from .schema_check import load_system_schema
    surfaces, skipped = {}, []
    for system_id in ids:
        context = read_business_context(system_id)
        schema = load_system_schema(system_id)
        if context is None:
            skipped.append({"system_id": system_id, "reason": "context-unavailable"})
        elif schema is None:
            skipped.append({"system_id": system_id, "reason": "schema-unavailable"})
        else:
            surfaces[system_id] = {
                "context": str(context.get("business_context") or ""),
                "schema": schema, "cached_samples": _load_cached_samples(system_id)}
    return surfaces, skipped


def run_context_placement_check(system_ids: "list[str] | None" = None) -> dict:
    """Load once per system, detect/file bounded hits, and report honest skips."""
    from .schema_drift import onboarded_system_ids
    all_ids = sorted(set(onboarded_system_ids()))[:MAX_SYSTEMS]
    focus = set(system_ids or all_ids)
    surfaces, skipped = _load_surfaces(all_ids)
    findings = find_context_placement_hits(surfaces, focus)
    filed = _file_context_placement_proposals(findings)
    swept = _sweep_resolved(findings, set(surfaces), focus)
    return {"systems_checked": len(surfaces), "terms_checked": sum(
                len(extract_candidate_terms(s["context"])) for s in surfaces.values()),
            "findings": len(findings), "filed": filed,
            "resolved_swept": swept, "skipped": skipped,
            "truncated": len(onboarded_system_ids()) > MAX_SYSTEMS
                         or len(findings) >= MAX_FINDINGS}
