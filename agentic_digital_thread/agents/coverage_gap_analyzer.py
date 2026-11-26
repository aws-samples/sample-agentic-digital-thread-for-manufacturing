# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Coverage-gap analysis — a PROPOSAL class, never a graph mutation.

A dashboard card can render "awaiting data" for a reason that is NOT data
absence: the concept it needs has a candidate field sitting right there in an
onboarded system's schema, but nothing ever mapped it (observed live —
``Metric_Event.amount_per_5s`` existed and was even mapped to a NEIGHBORING
concept, ``actual-quantity``, while ``throughput`` sat uncovered).

This module closes that loop WITHOUT a human re-deriving it by hand each time:
it reuses ``coverage.thread_coverage`` (the SAME gap list the dashboard
strip already computes — one source, ADR-0016) to find every currently
UNCOVERED concept, cross-references each against the ONBOARDED schema
(``schema_check.load_system_schema`` — the exact columns the graph already
knows about, zero extra connector round-trips) using the concept catalog's
``Fields: a, b, c`` candidate-name clause, and files one ``coverage_gap``
proposal per (system, concept, field) hit through
``self_learning.log_proposed_mapping``. Filing through the pending lifecycle
(never ``record_user_correction``'s already-accepted path) means the gates and
canonical-key dedup apply, and a steward reviews before anything happens.

Hard rules (mirrors schema_drift.py's contract exactly):

* NEVER calls map/resolve/provision/remap directly; NEVER writes a MAPS_TO
  edge itself — this module only FINDS and FILES candidates.
* Accepting a filed ``coverage_gap`` row is ACKNOWLEDGE-ONLY
  (``self_learning._accept_coverage_gap`` — zero graph writes, ever; the
  ``_accept_schema_drift`` pattern). APPLYING the candidate mapping stays the
  steward's separate explicit remap action (``POST /api/onboard/remap`` —
  MERGE-only, never deletes). This type deliberately does NOT reuse
  ``concept_mapping``'s accept path: ``_repoint_maps_to``'s column-keyed
  delete (the R5 win-rule for genuine user corrections) would strip a
  NEIGHBORING concept's edge off the candidate column — and the next rule
  makes that a live hazard, not a theoretical one.
* A field ALREADY mapped to ANY concept still qualifies as a candidate for a
  DIFFERENT concept (observed live: ``amount_per_5s`` was mapped to
  ``actual-quantity`` AND is a ``throughput`` candidate) — concept mapping is
  many-to-one on the field side by design (a field can measure more than one
  thing), so this must not skip already-mapped fields.
* TWO candidate fields for the same unmapped concept COEXIST as separate
  reviewable pending rows: the ``proposal_key.py`` ``coverage_gap`` grain puts
  the candidate column in the SUBJECT (``(to_system, concept, to_field)``), so
  a second candidate is a distinct subject (insert), never a same-subject
  supersede of the first. The same re-detected finding dedupes as
  ``dup_pending``.
* RESOLVED gaps are swept (``sweep_resolved_gaps`` — the
  ``schema_drift.sweep_resolved_drift`` pattern, ADR-0052 replace-not-accrete):
  once a concept is covered (or the candidate column disappears), pending
  agent-sourced ``coverage_gap`` rows no longer in the current finding set are
  machine-legally superseded. A mid-sweep steward decision wins.
* An EMPTY covered-concept set is only trusted after a graph-reachability
  cross-check (``_graph_reachable`` — the ``_repoint_maps_to`` honesty-guard
  idiom): the ``db.neptune_query`` read seam swallows backend errors into
  ``[]``, so without the check a transient graph outage would make EVERY
  concept look like a gap. Unreachable (or nothing onboarded) ⇒ honest
  skipped zero-result, no filing.
* PURE analysis (``candidate_field_matches``, ``analyze_coverage_gaps``) —
  fully offline-assertable; IO (schema/coverage reads, the proposal write) is
  isolated in the one orchestrating entrypoint, ``run_coverage_gap_check``.
* Runs at the SAME two triggers as schema drift: on-demand (steward-gated
  route) and after onboarding completes for a system (so the gap surfaces
  immediately, not after the next scheduled sweep) — never inside the hot
  chat/query path.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Mirrors concept_suggest._SYNONYM_RE's shape for the catalog's sibling
# "Fields: a, b, c." clause — the candidate PHYSICAL COLUMN NAMES a concept's
# definition already documents (distinct from the ISA-95 "Synonyms (...)"
# clause, which concept_suggest.isa95_synonym_candidates already covers).
_FIELDS_CLAUSE_RE = re.compile(r"Fields:\s*([^.]+)\.", re.IGNORECASE)

# The pinned per-row reason attribute (rides the canonical key RHS — the same
# unresolved finding dedupes; also the accept-time diagnostic marker).
GAP_REASON = "concept_unmapped"


def extract_candidate_field_names(definition_text: str) -> list[str]:
    """Pull the lower-cased candidate field names from a concept definition's
    ``Fields: a, b, c.`` clause. Returns ``[]`` when no such clause exists.
    Pure — mirrors ``concept_suggest.extract_synonyms``."""
    m = _FIELDS_CLAUSE_RE.search(definition_text or "")
    if not m:
        return []
    return [p.strip().lower() for p in re.split(r"[,;]", m.group(1)) if p.strip()]


def _name_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def candidate_field_matches(
    concept_id: str, concept_definition: str, schema: dict,
) -> list[dict]:
    """Onboarded ``table.column`` entries whose name token-matches one of the
    concept's documented candidate field names. Pure — no graph/connector IO;
    ``schema`` is the already-fetched ``load_system_schema`` shape.

    Token-SET containment (not substring): a candidate name's tokens must be a
    subset of the column's tokens, so ``amount_per_5s`` matches candidate
    ``amount`` but a column named merely ``amount_unit`` does not spuriously
    match a candidate ``amount_unit_price``. Same discipline as
    ``concept_suggest.isa95_synonym_candidates`` — token match, never fuzzy.
    """
    candidates = extract_candidate_field_names(concept_definition)
    if not candidates:
        return []
    candidate_tokens = [(c, _name_tokens(c)) for c in candidates]
    hits: list[dict] = []
    for table, meta in (schema or {}).items():
        if not isinstance(meta, dict):
            continue
        for column in meta.get("columns") or []:
            col_tokens = _name_tokens(str(column))
            if not col_tokens:
                continue
            for candidate_name, cand_tokens in candidate_tokens:
                if cand_tokens and cand_tokens <= col_tokens:
                    hits.append({
                        "table": str(table), "column": str(column),
                        "matched_candidate": candidate_name,
                    })
                    break
    return hits


def analyze_coverage_gaps(gaps: list, schemas_by_system: dict) -> list:
    """PURE: cross-reference every ``thread_coverage`` gap against every
    onboarded system's schema. Returns one finding dict per (system, field,
    concept) hit: ``{system_id, table, column, concept, matched_candidate,
    blocks_cards}``. ``gaps`` is ``thread_coverage(...)['gaps']``;
    ``schemas_by_system`` is ``{system_id: load_system_schema(system_id)}``
    (callers fetch — this stays IO-free)."""
    from .catalog import CORE_CONCEPT_DEFINITIONS

    findings: list[dict] = []
    for gap in gaps or []:
        concept_id = str(gap.get("concept") or "")
        definition = CORE_CONCEPT_DEFINITIONS.get(concept_id, "")
        if not definition:
            continue  # non-core concepts carry no curated Fields: clause
        for system_id, schema in (schemas_by_system or {}).items():
            if not schema:
                continue
            for hit in candidate_field_matches(concept_id, definition, schema):
                findings.append({
                    "system_id": system_id, "table": hit["table"],
                    "column": hit["column"], "concept": concept_id,
                    "matched_candidate": hit["matched_candidate"],
                    "blocks_cards": list(gap.get("blocks_cards") or []),
                })
    return sorted(findings, key=lambda f: (f["system_id"], f["table"], f["column"], f["concept"]))


def _file_coverage_gap_proposals(findings: list) -> int:
    """File one pending ``coverage_gap`` proposal per finding through
    ``log_proposed_mapping`` (gates + canonical-key dedup apply — the
    ``proposal_key.py`` ``coverage_gap`` grain: subject
    ``(to_system, concept, to_field)``, so a SECOND candidate field for the
    same unmapped concept is a DISTINCT subject and files as its own pending
    row — a steward compares candidates instead of the machine silently
    picking one). Returns the count of NEWLY filed rows — a gate drop or
    ``dup_pending`` counts as found, not filed."""
    if not findings:
        return 0
    from . import self_learning
    existing = {str(r.get("mapping_id"))
                for r in self_learning.get_pending_mappings()
                if r.get("proposed_type") == "coverage_gap"
                and r.get("source") == "agent"}
    detected = datetime.now(timezone.utc).date().isoformat()
    filed = 0
    for f in findings:
        to_field = f"{f['table']}.{f['column']}"
        cards = ", ".join(f["blocks_cards"]) or "no dashboard card"
        mapping_id = self_learning.log_proposed_mapping(
            f["system_id"], "", "",
            f["system_id"], to_field, "",
            f["concept"], 0.5,
            f"coverage gap detected {detected}: '{f['system_id']}.{to_field}' "
            f"name-matches unmapped concept '{f['concept']}' (candidate field "
            f"name '{f['matched_candidate']}'), which currently blocks: {cards}. "
            f"This is a NAME match only, not a value/schema check — accepting "
            f"marks it reviewed; apply it explicitly via remap if confirmed.",
            proposed_type="coverage_gap", source="agent",
            method="coverage-gap-name-match", reason=GAP_REASON,
        )
        if mapping_id and mapping_id not in existing:
            filed += 1
    return filed


def _current_gap_target_keys(findings: list) -> set:
    """Recompute each current finding's canonical ``target_key`` (the
    ``proposal_key.py`` ``coverage_gap`` grain). Stored rows CARRY this key
    (``self_learning._build_item`` writes ``target_key`` on every proposal
    item), so the sweep compares recomputed vs stored. Pure."""
    from .proposal_key import canonical_keys
    return {
        canonical_keys(
            "coverage_gap",
            to_system=str(f.get("system_id") or ""),
            concept=str(f.get("concept") or ""),
            to_field=f"{f.get('table')}.{f.get('column')}",
            reason=GAP_REASON,
        ).target_key
        for f in findings or []
    }


def sweep_resolved_gaps(system_ids: list, findings: list) -> int:
    """Machine-legal supersede of RESOLVED coverage-gap rows (the
    ``schema_drift.sweep_resolved_drift`` pattern — ADR-0052
    replace-not-accrete; the machine's ONLY legal terminal is ``superseded``,
    never a steward-only status).

    Flips every pending, agent-sourced, non-quarantined ``coverage_gap`` row
    whose ``to_system`` is in ``system_ids`` and whose stored ``target_key``
    is NOT in the current finding set to ``superseded`` (+ ``superseded_at``,
    ``superseded_reason="gap-resolved"``). DynamoDB-only — never touches the
    graph. Scoped to the systems this run actually checked, so a one-system
    investigate never sweeps another system's still-open findings. A row with
    no stored ``target_key`` is never swept on a guess. Each flip is
    CONDITIONAL on a still-pending row (the ADR-0043 subsume idiom) — a
    mid-sweep steward decision WINS (skipped, uncounted). Fail-loud otherwise;
    the entrypoint's never-raise boundary wraps it."""
    from . import self_learning
    from .value_promotion import _is_conditional_check_failed
    scope = {str(s) for s in system_ids or []}
    current_keys = _current_gap_target_keys(findings)
    superseded_at = datetime.now(timezone.utc).isoformat()
    table = self_learning._get_table()
    swept = 0
    for row in self_learning.get_pending_mappings():
        if (row.get("proposed_type") != "coverage_gap"
                or str(row.get("to_system") or "") not in scope
                or str(row.get("source") or "") != "agent"
                or str(row.get("quarantined") or "") == "true"
                or str(row.get("status") or "") != "pending"):
            continue
        stored_key = str(row.get("target_key") or "")
        if not stored_key or stored_key in current_keys:
            continue
        try:
            self_learning._update_status(
                table, row["mapping_id"], row["discovered_at"], "superseded",
                {"superseded_at": superseded_at,
                 "superseded_reason": "gap-resolved"},
                expect_status="pending")
        except Exception as exc:
            if not _is_conditional_check_failed(exc):
                raise  # fail-loud on genuine write failures
            continue  # a steward decision landed mid-sweep — it wins
        swept += 1
    if swept:
        logger.info("coverage-gap sweep: superseded %d resolved row(s) for "
                    "systems %s", swept, sorted(scope))
    return swept


def _graph_reachable() -> bool:
    """Honesty guard for the empty covered-concept set (the
    ``_repoint_maps_to`` idiom): the ``db.neptune_query`` read seam swallows
    backend errors and returns ``[]`` — indistinguishable from "nothing is
    mapped". Cross-check the System nodes: at least one MUST exist for any
    coverage question to be meaningful (no systems ⇒ no schemas ⇒ nothing to
    file anyway). Returns True only on a positive count."""
    from .db import neptune_query
    rows = neptune_query("MATCH (s:System) RETURN count(s) AS n")
    n = (rows[0].get("n") if rows else 0) or 0
    return int(n) >= 1


def run_coverage_gap_check(system_ids: "list[str] | None" = None) -> dict:
    """Orchestrating entrypoint: coverage gaps → schema cross-reference → filed
    proposals → resolved-gap sweep. Bounded to the given system ids (default:
    every onboarded system — the SAME set schema drift scans,
    ``schema_drift.onboarded_system_ids``). Never raises: any failure degrades
    to an honest zero-result dict (a coverage check must never break a
    dashboard/onboarding-completion caller). An empty covered set with an
    unreachable graph is a SKIP (``skipped: graph-unavailable``), never a
    mass gap-filing.
    """
    zero = {"gaps_checked": 0, "systems_checked": 0, "findings": 0,
            "filed": 0, "resolved_swept": 0}
    try:
        from . import coverage
        from .schema_check import load_system_schema
        from .schema_drift import onboarded_system_ids

        ids = system_ids if system_ids is not None else onboarded_system_ids()
        covered = coverage.covered_concepts()
        if not covered and not _graph_reachable():
            logger.warning("coverage-gap check skipped: empty covered set and "
                           "no System nodes readable (graph unavailable or "
                           "nothing onboarded)")
            return {**zero, "skipped": "graph-unavailable"}
        report = coverage.thread_coverage(covered)
        gaps = report.get("gaps") or []
        schemas = {sid: load_system_schema(sid) for sid in ids}
        findings = analyze_coverage_gaps(gaps, schemas)
        filed = _file_coverage_gap_proposals(findings)
        swept = sweep_resolved_gaps(ids, findings)
        return {"gaps_checked": len(gaps), "systems_checked": len(ids),
                "findings": len(findings), "filed": filed,
                "resolved_swept": swept}
    except Exception as e:  # noqa: BLE001 - a coverage check must never break its caller
        logger.warning(f"coverage-gap check failed (non-critical): {e}")
        return zero
