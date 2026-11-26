# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Vocabulary drift — post-import field-binding PROPOSALS (ADR-0081).

A concept imported AFTER a system was onboarded has no ``MAPS_TO`` edge, so
metrics and queries that need it resolve nothing until someone re-onboards.
This module is the deliberate middle path: for every onboarded system it
scores the system's UNBOUND columns (no ``MAPS_TO`` edge) against ONLY the
just-imported concept ids, using ``vocab_fit``'s computed token/synonym
matcher, and files each winner as a PENDING ``concept_mapping`` proposal
through the ONE ``self_learning.log_proposed_mapping`` pipeline (gates,
suppression, tombstone damping, dedup all apply). The steward accepts through
the existing queue; re-onboarding is NOT required.

Contract (do not relax — pinned by ``tests/unit/test_vocabulary_drift.py``):

* PROPOSALS ONLY — this module NEVER writes ``MAPS_TO`` or any graph node;
  no graph-write helper is imported or called (AST-scanned).
* GRAPH BASELINE ONLY — the input is ``schema_check.load_system_schema``
  (the same source the daily drift check reads); NO live discover, NO Athena.
* COMPUTED fit — ``vocab_fit`` tokens/synonyms (ADR-0068); never LLM-judged.
* Unbound columns are queried in the UNBOUND direction
  (``WHERE NOT (f)-[:MAPS_TO]->()``): the ``neptune_query`` ``[]`` failure
  degrade then yields ZERO proposals, never "everything unbound".
* ONE-TO-ONE across BOTH (system, concept) AND (system, column), losers
  VISIBLY counted (ADR-0081 amendment 2026-09-25) — the accept writer
  (``_repoint_maps_to``) DELETEs every prior ``MAPS_TO`` edge for the
  ``(system, concept)`` pair before MERGEing the chosen one (a 2+ binding
  withholds as ``ambiguous_binding``, ADR-0076) AND its R5 column-keyed cleanup deletes every other
  edge leaving the chosen column, so coexisting proposals on EITHER axis
  would make bulk accept silently destructive; the ``proposal_key.py``
  ``concept_mapping`` subject grain ``(to_system, concept)`` would collapse
  same-concept duplicates by supersede anyway. Greedy best-score-first
  assignment (deterministic lexicographic tie-break); outscored columns are
  reported per system under ``skipped.runner_up`` and a concept that lost
  every matched column to another concept under ``skipped.column_conflict``
  — dropped visibly, never silently.
* Idempotency is the STORE's subject-key supersede/dedup — no dedupe state
  of our own (ADR-0081 D3).
* Dotted struct columns (``oee.availability``) are SCORED like any other
  column (ADR-0076 D5): a Glue-flattened name is ONE physical column, the
  accept writer's Field id is the concatenation ``system.table.column``
  (split point irrelevant), and the metric path renders it as ONE quoted
  identifier (ADR-0076 D4). A winner files ``to_field = table.column``.
* Generic bookkeeping columns (``vocab_fit.is_generic_column``) never bind
  to a domain concept by name alone (ADR-0081 D2).
* Skips are HONEST, per system (the ``schema_drift`` posture): no baseline ⇒
  ``skipped:no-baseline``; a per-system failure ⇒ ``skipped:<error class>``
  — never a fabricated zero, and never kills the pass.

Import-lazy for all impure deps (``agents.db``, ``self_learning``,
``schema_check``, ``schema_drift``); ``vocab_fit`` is import-pure and
imported at the top.
"""
from __future__ import annotations

import logging

from . import vocab_fit

logger = logging.getLogger(__name__)

# Field-scoped unbound query (SchemaEntry nodes carry their own entity-level
# MAPS_TO edges — those must not mask an unbound Field).
_UNBOUND_FIELDS_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(:SchemaEntry)"
    "-[:HAS_FIELD]->(f:Field) "
    "WHERE NOT (f)-[:MAPS_TO]->() "
    "RETURN f.id AS id, f.name AS name, f.schema_id AS schema_id"
)

# ONE read of ONLY the given concept ids; retired concepts excluded.
_CONCEPT_TERMS_CYPHER = (
    "MATCH (c:Concept) WHERE c.id IN $ids "
    "AND coalesce(c.retired, false) = false "
    "RETURN c.id AS id, c.name AS name, c.description AS description"
)


def _zero_report() -> dict:
    """The bounded report skeleton — counters only, no payload text."""
    return {"systems_scanned": 0, "columns_scored": 0, "proposals_filed": 0,
            "skipped": {"bound": 0, "generic": 0, "tombstoned_or_gated": 0},
            "per_system": []}


def _concept_terms(concept_ids: list[str]) -> dict[str, list[frozenset]]:
    """``{concept_id: [term token-sets]}`` for the GIVEN ids only.

    One graph read, parameterized with exactly the input ids (the pass never
    re-scores the whole vocabulary — ADR-0081 D2). Unknown/retired ids simply
    produce no terms; a graph failure degrades to ``{}`` (zero proposals)."""
    from .db import neptune_query
    rows = neptune_query(_CONCEPT_TERMS_CYPHER, {"ids": list(concept_ids)})
    terms: dict[str, list[frozenset]] = {}
    for row in rows or []:
        cid = str(row.get("id") or "")
        if not cid:
            continue
        term_sets = vocab_fit._concept_term_sets([{
            "id": cid, "name": row.get("name") or "",
            "description": row.get("description") or ""}])
        if term_sets:
            terms[cid] = term_sets
    return terms


def _unbound_columns(system_id: str, baseline: dict) -> list[dict]:
    """The system's unbound Field rows as ``[{"table", "column"}]``, sorted.

    Queried in the UNBOUND direction (fail-safe: an outage ⇒ ``[]`` ⇒ zero
    proposals) and intersected with the graph BASELINE — a stale Field node
    whose table is no longer in the baseline is dropped (the baseline stays
    the input, per ADR-0081 D1)."""
    from .db import neptune_query
    from .schema_check import _resolve_table
    rows = neptune_query(_UNBOUND_FIELDS_CYPHER, {"sid": system_id})
    prefix = system_id + "."
    out: list[dict] = []
    seen: set[tuple] = set()
    for row in rows or []:
        column = str(row.get("name") or "")
        schema_id = str(row.get("schema_id") or "")
        if not column or not schema_id.startswith(prefix):
            continue
        table = schema_id[len(prefix):]
        if not table or _resolve_table(baseline, table) is None:
            continue
        if (table, column) in seen:
            continue
        seen.add((table, column))
        out.append({"table": table, "column": column})
    return sorted(out, key=lambda c: (c["table"], c["column"]))


def _score(col_tokens: frozenset, terms: list[frozenset]) -> float:
    """Max containment ratio of a matching term set — a term whose tokens are
    a SUBSET of the column-name tokens matches (the ``vocab_fit.compute_fit``
    rule); the ratio prefers the tighter column. Deterministic, in (0, 1]."""
    best = 0.0
    for term in terms:
        if term and term <= col_tokens:
            best = max(best, len(term) / len(col_tokens))
    return best


def _match_candidates(columns: list[dict],
                      concept_terms: dict) -> list[tuple]:
    """Every scoring (concept, column) match as ``(-score, concept_id,
    to_field)``, sorted best-first with a deterministic lexicographic
    tie-break (concept id, then field). Pure."""
    out: list[tuple[float, str, str]] = []
    for cid in sorted(concept_terms):
        for col in columns:
            col_tokens = vocab_fit._name_tokens(col["column"])
            if not col_tokens:
                continue
            score = _score(col_tokens, concept_terms[cid])
            if score > 0.0:
                out.append((-score, cid, f"{col['table']}.{col['column']}"))
    out.sort()
    return out


def _best_bindings(columns: list[dict],
                   concept_terms: dict) -> tuple[list, int, int]:
    """ONE-TO-ONE assignment across BOTH (system, concept) and
    (system, column) (ADR-0081 amendment 2026-09-25): the accept writer
    makes the chosen column the SOLE authoritative ``MAPS_TO`` edge for the
    ``(system, concept)`` pair AND — the R5 column-keyed cleanup — the most
    recently applied edge the ONLY live one for a column, so coexisting
    proposals on either axis would make bulk accept silently destructive.
    Greedy best-score-first over the deterministic candidate order; a
    candidate whose concept OR column is already assigned is dropped
    VISIBLY: as a ``runner_up`` when its concept still won another column,
    as a distinct ``column_conflict`` (per losing concept) when the concept
    won nothing. Returns ``(bindings, runners_up, conflicts)`` with
    ``bindings = [(concept_id, to_field, score)]``."""
    candidates = _match_candidates(columns, concept_terms)
    taken_concepts: set[str] = set()
    taken_fields: set[str] = set()
    bindings: list[tuple[str, str, float]] = []
    dropped: list[str] = []
    for neg_score, cid, to_field in candidates:
        if cid in taken_concepts or to_field in taken_fields:
            dropped.append(cid)
            continue
        taken_concepts.add(cid)
        taken_fields.add(to_field)
        bindings.append((cid, to_field, -neg_score))
    runners_up = sum(1 for cid in dropped if cid in taken_concepts)
    conflicts = len({cid for cid in dropped if cid not in taken_concepts})
    return bindings, runners_up, conflicts


def _file_bindings(system_id: str, bindings: list,
                   provenance: str) -> tuple[int, int]:
    """File each winner through the ONE proposal pipeline. A ``None`` return
    is a documented gate/tombstone drop (counted, never retried here).
    Returns ``(filed, tombstoned_or_gated)``."""
    from . import self_learning
    filed = 0
    gated = 0
    for concept, to_field, score in bindings:
        mapping_id = self_learning.log_proposed_mapping(
            "", "", "",                    # from_system, from_field, from_value
            system_id, to_field, "",       # to_system, to_field, to_value
            concept, score,
            f"vocabulary drift (ADR-0081): concept '{concept}' imported "
            f"after onboarding matches unbound column {system_id}.{to_field} "
            f"by vocab_fit token match",
            proposed_type="concept_mapping", source="vocabulary_drift",
            method="vocab_fit", provenance=provenance)
        if mapping_id:
            filed += 1
        else:
            gated += 1
    return filed, gated


def _scan_system(system_id: str, concept_terms: dict,
                 provenance: str) -> dict:
    """One system's pass → its per-system report entry (counters only)."""
    from .schema_check import load_system_schema
    baseline = load_system_schema(system_id)
    if not baseline:
        return {"system_id": system_id, "status": "skipped:no-baseline"}
    unbound = _unbound_columns(system_id, baseline)
    total = sum(len((meta or {}).get("columns") or [])
                for meta in baseline.values() if isinstance(meta, dict))
    generic = [c for c in unbound if vocab_fit.is_generic_column(c["column"])]
    candidates = [c for c in unbound if c not in generic]
    bindings, runners_up, conflicts = _best_bindings(candidates,
                                                     concept_terms)
    filed, gated = _file_bindings(system_id, bindings, provenance)
    return {"system_id": system_id, "status": "ok",
            "columns_scored": len(candidates), "proposals_filed": filed,
            "skipped": {"bound": max(0, total - len(unbound)),
                        "generic": len(generic),
                        "tombstoned_or_gated": gated,
                        "runner_up": runners_up,
                        "column_conflict": conflicts}}


def propose_bindings(concept_ids: list[str], *, provenance: str) -> dict:
    """ADR-0081 D1: file PENDING ``concept_mapping`` proposals binding the
    GIVEN concepts to unbound columns of every onboarded system.

    Bounded report: ``{systems_scanned, columns_scored, proposals_filed,
    skipped: {bound, generic, tombstoned_or_gated}, per_system: [...]}`` —
    counters only. Never raises for a per-system failure (honest skip);
    empty/invalid input returns the zero report WITHOUT a graph read, so the
    import hook can call unconditionally. NEVER writes the graph."""
    ids = [i.strip() for i in (concept_ids or [])
           if isinstance(i, str) and i.strip()]
    report = _zero_report()
    if not ids:
        return report
    concept_terms = _concept_terms(ids)
    if not concept_terms:
        return report
    from .schema_drift import onboarded_system_ids
    for system_id in onboarded_system_ids():
        try:
            entry = _scan_system(system_id, concept_terms, provenance)
        except Exception as exc:  # noqa: BLE001 - honest skip, class only
            logger.warning("vocabulary drift pass skipped system %s: %s",
                           system_id, type(exc).__name__)
            entry = {"system_id": system_id,
                     "status": f"skipped:{type(exc).__name__}"}
        report["per_system"].append(entry)
        if entry.get("status") != "ok":
            continue
        report["systems_scanned"] += 1
        report["columns_scored"] += entry["columns_scored"]
        report["proposals_filed"] += entry["proposals_filed"]
        for key in report["skipped"]:
            report["skipped"][key] += entry["skipped"][key]
    return report
