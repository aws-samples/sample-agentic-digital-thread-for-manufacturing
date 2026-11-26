# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Concept Layer (S5.2 / ADR 0021): a derived read-model over the two-layer concept
vocabulary, plus the *minting* gate and entity-scoped reuse-first suggestion.

Design discipline — copied from ``coverage.py`` (ADR 0001):
  * Import-pure core — deterministic, no LLM/eval/I-O at import. The ONLY impure seam
    is the injected ``neptune_query`` graph read (a default arg, swappable in tests).
  * ``neptune_query`` is a LAZY wrapper (imports ``.db`` on first call) so this module
    imports WITHOUT pg8000/boto3 — the catalog.py discipline (ADR 0020) — and degrades
    to ``[]`` when runtime deps are absent (no-graph safe).
  * Graph is the single source of truth (ADR 0021). The flat index is a *derived
    projection* (core ∪ dynamic), never a competing authority.

The minting gate (``field_earns_concept``) implements the asymmetric ADR 0021 rule:
mapping a column to an *existing* concept is unconditional and lives in onboarding;
this predicate gates ONLY the act of MINTING a new dynamic field concept.
"""
from __future__ import annotations

import re

from .catalog import (
    CONCEPT_AFFINITY,
    CONCEPT_LAYER,
    CONCEPT_NAME,
    CORE_CONCEPT_DEFINITIONS,
    ENTITY_CONCEPT_IDS,
    FIELD_CONCEPT_IDS,
    GENERIC_FIELD_NAMES,
    is_generic_field,
)
from .card_layer import get_card_layer
from .metric_layer import get_metric_layer


def neptune_query(cypher: str, parameters=None) -> list:
    """The ONLY impure seam — a lazy, no-graph-safe graph read.

    Imports the real Neptune reader on first call so this module imports without
    pg8000/boto3 (catalog.py discipline, ADR 0020). If the runtime deps are absent
    the read degrades to ``[]`` (``db.neptune_query`` already swallows graph errors)."""
    try:
        from .db import neptune_query as _nq
    except Exception:  # nosec B110 - runtime deps absent (CDK venv) → no-graph safe
        return []
    return _nq(cypher, parameters)


# --- Locked Cypher (one read per function; injected seam swaps it in tests) ---

_DYNAMIC_CONCEPTS_CYPHER = (
    "MATCH (c:Concept) WHERE c.source IS NOT NULL AND c.source <> 'core' "
    "RETURN c.id AS id, c.name AS name, c.source AS source"
)
_SYSTEMS_FOR_ENTITY_CYPHER = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)-[:MAPS_TO]->(c:Concept {id: $e}) "
    "RETURN DISTINCT s.id AS id"
)
_PRECEDENT_CYPHER = (
    "MATCH (se:SchemaEntry)-[:MAPS_TO]->(:Concept {id: $e}), "
    "(se)-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->(fc:Concept) "
    "RETURN DISTINCT fc.id AS id"
)
_ENTITY_PRECEDENT_CYPHER = (
    "MATCH (se:SchemaEntry)-[:MAPS_TO]->(ec:Concept), "
    "(se)-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->(fc:Concept) "
    "WHERE fc.id IN $fcs "
    "RETURN DISTINCT ec.id AS id"
)
_CONCEPT_DESCRIPTION_CYPHER = (
    "MATCH (c:Concept {id: $id}) RETURN c.description AS description"
)
_CONCEPT_DESCRIPTIONS_CYPHER = (
    "MATCH (c:Concept) WHERE c.id IN $ids "
    "RETURN c.id AS id, c.description AS description"
)

# --- Suggestion weights (precedent > affinity-prior > name match) ---
_W_PRECEDENT = 3.0
_W_AFFINITY = 2.0
_W_NAME = 1.0


# --- Catalog accessors (no graph) ------------------------------------------

def entity_concepts() -> list[str]:
    """Entity-capable core concept ids (curated; ADR 0021), sorted. No graph."""
    return sorted(ENTITY_CONCEPT_IDS)


def field_concepts() -> list[str]:
    """Field-layer core concept ids (ADR 0021), sorted. No graph."""
    return sorted(FIELD_CONCEPT_IDS)


def is_entity(cid: str) -> bool:
    """True iff ``cid`` is an entity-capable concept (ADR 0021). No graph."""
    return cid in ENTITY_CONCEPT_IDS


_NON_CONCEPT_SENTINELS = frozenset({"schema-annotation", "needs_review", "unknown"})


def concept_layer_of(cid: str) -> str:
    """Canonical rendering layer of a concept — 'entity' | 'field' | '' (ADR 0021). No graph.

    Core concepts come from ``CONCEPT_LAYER``; a non-empty unknown id is a dynamic/minted
    concept, which is field-layer by construction (matching ``flat_index``). Empty id, or a
    non-concept sentinel (``schema-annotation``/``needs_review``/``unknown`` — used by
    field_annotation/unmatched proposals, not real concepts), → '' (no layer badge)."""
    if cid in CONCEPT_LAYER:
        return CONCEPT_LAYER[cid]
    if not cid or cid in _NON_CONCEPT_SENTINELS:
        return ""
    return "field"


def concept_description(cid: str, query=neptune_query) -> str:
    """Human-readable definition of a concept (ADR 0021 cosmetic metadata).

    Core concepts return their curated ``CORE_CONCEPT_DEFINITIONS`` text (no graph). Any
    other id triggers ONE graph read for the minted ``Concept.description``; returns '' when
    the concept is absent or carries no description. Import-pure (injected query); the lazy
    seam degrades to [] → '' with no graph."""
    if not cid:
        return ""
    if cid in CORE_CONCEPT_DEFINITIONS:
        return CORE_CONCEPT_DEFINITIONS[cid]
    rows = query(_CONCEPT_DESCRIPTION_CYPHER, {"id": cid})
    if rows:
        return rows[0].get("description") or ""
    return ""


def concept_descriptions(concept_ids, query=neptune_query) -> dict[str, str]:
    """Batch sibling of ``concept_description`` — resolve MANY ids in AT MOST ONE graph read.

    Core ids resolve from ``CORE_CONCEPT_DEFINITIONS`` (no graph). The DISTINCT non-core ids
    trigger a SINGLE graph read (``c.id IN $ids``); each maps to its ``Concept.description``,
    or '' when the concept is absent or carries no description. Empty/None ids are skipped.
    Import-pure (injected query); the lazy seam degrades to [] → '' with no graph."""
    out: dict[str, str] = {}
    non_core: set[str] = set()
    for cid in concept_ids:
        if not cid or cid in out or cid in non_core:
            continue
        if cid in CORE_CONCEPT_DEFINITIONS:
            out[cid] = CORE_CONCEPT_DEFINITIONS[cid]
        else:
            non_core.add(cid)
    if non_core:
        rows = query(_CONCEPT_DESCRIPTIONS_CYPHER, {"ids": sorted(non_core)})
        found = {r.get("id"): (r.get("description") or "") for r in rows if r.get("id")}
        for cid in non_core:
            out[cid] = found.get(cid, "")
    return out


# --- Flat index: derived projection over core ∪ dynamic --------------------

def flat_index(query=neptune_query) -> list[dict]:
    """Derived read-model: every concept as ``{id, name, layer, source}``.

    Core concepts come from the catalog (layer from ``CONCEPT_LAYER``, ``source='core'``);
    dynamic concepts come from ONE graph read (``source <> 'core'``) and are field-layer by
    construction (ADR 0021). Core wins on id collisions. Pure projection — no minting."""
    rows: list[dict] = [
        {"id": cid, "name": CONCEPT_NAME.get(cid, cid),
         "layer": CONCEPT_LAYER[cid], "source": "core"}
        for cid in CONCEPT_LAYER
    ]
    seen = {r["id"] for r in rows}
    for r in query(_DYNAMIC_CONCEPTS_CYPHER):
        cid = r.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        rows.append({"id": cid, "name": r.get("name") or cid,
                     "layer": "field", "source": r.get("source") or "dynamic"})
    return rows


# --- Entity sharing (graph reads) ------------------------------------------

def systems_for_entity(entity_id, *, exclude_system=None, query=neptune_query) -> set[str]:
    """Set of system ids whose table (SchemaEntry) maps to ``entity_id`` (ONE read).

    ``exclude_system`` drops the named system (e.g. the candidate being onboarded).
    Graph errors degrade to ``[]`` → ``set()`` (no-graph safe)."""
    ids = {r["id"] for r in query(_SYSTEMS_FOR_ENTITY_CYPHER, {"e": entity_id}) if r.get("id")}
    if exclude_system is not None:
        ids.discard(exclude_system)
    return ids


def entity_is_shared(entity_id, candidate_system, *, query=neptune_query) -> bool:
    """True iff ≥2 distinct systems map ``entity_id`` (the candidate + ≥1 other).

    Implemented as "≥1 OTHER system already maps it", so the candidate need not yet
    have its edge written. No-graph safe (falls to False)."""
    others = systems_for_entity(entity_id, exclude_system=candidate_system, query=query)
    return len(others) >= 1


# --- Card / Metric backing (code-owned layers; injected for tests) ---------

def feeds_card_or_metric(concept_id, *, metric_layer=None, card_layer=None) -> bool:
    """True iff ``concept_id`` backs any Metric (and thereby any Card) — ADR 0021 (b).

    A concept that backs a Metric backs every Card referencing that Metric, so a
    non-empty metric reverse-index is sufficient; the card layer is consulted (and
    injectable) to keep the seam explicit. Pure — no graph."""
    ml = metric_layer or get_metric_layer()
    metric_ids = ml.metrics_for_concept(concept_id)
    if not metric_ids:
        return False
    cl = card_layer or get_card_layer()
    return any(cl.cards_for_metric(mid) for mid in metric_ids) or bool(metric_ids)


# --- The minting gate (ADR 0021) -------------------------------------------

def field_earns_concept(entity_id, candidate_system, column_name, field_concept_id,
                        *, query=neptune_query) -> bool:
    """GATES MINTING ONLY (never mapping — ADR 0021). A new dynamic field concept is
    earned iff the column is NOT generic AND (its entity is shared by ≥2 systems OR it
    feeds a Card/derived Metric). Pure rule over the injected graph seam."""
    if is_generic_field(column_name):
        return False
    return (entity_is_shared(entity_id, candidate_system, query=query)
            or feeds_card_or_metric(field_concept_id))


# --- Entity-scoped reuse-first suggestion (ADR 0021 / ADR 0007) ------------

def _precedent_concepts(entity_id, query) -> set[str]:
    """Field concepts other systems already mapped under the SAME entity (ONE read)."""
    return {r["id"] for r in query(_PRECEDENT_CYPHER, {"e": entity_id}) if r.get("id")}


def _name_matches(column_name: str, cid: str, name: str) -> bool:
    """Loose name/alias overlap: a ≥3-char column token appearing in the concept id or name."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", column_name.lower()) if len(t) >= 3]
    hay = f"{cid} {name}".lower()
    return any(t in hay for t in tokens)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t}


def _listed_fields(cid: str) -> set[str]:
    """The example field names a core concept's description lists after ``Fields:``."""
    m = re.search(r"Fields: ([^.]*)\.", CORE_CONCEPT_DEFINITIONS.get(cid, ""))
    return {f.strip().lower() for f in m.group(1).split(",")} if m else set()


#: :func:`column_support` for a column the concept's own field list names.
_LISTED_FIELD_SUPPORT = 100


def column_support(column_name: str, cid: str, name: str = "") -> int:
    """How specifically ``column_name`` itself names concept ``cid``; 0 = not at all.

    The concept's description lists the column (``_LISTED_FIELD_SUPPORT``), or
    EVERY token of the concept id, or of its display name, is a token of the
    column (the token count). A single shared token is not support: the token
    ``quantity`` does not make ``reject_quantity`` an ``actual-quantity``. A
    ONE-token concept name needs the column to BE that word: ``cost_center``
    is not a ``cost``, nor ``batch_status`` a ``batch``.
    """
    col = str(column_name or "").lower()
    if col and col in _listed_fields(cid):
        return _LISTED_FIELD_SUPPORT
    have = _tokens(col)
    best = 0
    for wanted in (_tokens(cid), _tokens(name)):
        if len(wanted) == 1 and have != wanted:
            continue
        if wanted and wanted <= have:
            best = max(best, len(wanted))
    return best


def supported_reuse_choice(suggestions: list[dict]) -> dict | None:
    """The reuse suggestion a column ITSELF supports, or ``None``.

    Only suggestions with ``column_match`` count. The most specific one wins
    (``column_support``), then the higher score; two different concepts equally
    specific is ambiguous and labels nothing (``inspection_result`` names both
    ``inspection`` and, more specifically, ``inspection-result``: that one is
    not a tie)."""
    supported = [s for s in suggestions or [] if s.get("column_match")]
    if not supported:
        return None
    top = max(int(s.get("column_support") or 0) for s in supported)
    best = [s for s in supported if int(s.get("column_support") or 0) == top]
    if len({s["concept"] for s in best}) > 1:
        return None
    return max(best, key=lambda s: s.get("score", 0.0))


def reuse_first_suggestions(entity_id, system_type, column_name,
                            *, query=neptune_query) -> list[dict]:
    """Ranked, entity-scoped candidates for a column → field concept (ADR 0021 reuse-first).

    Three signals, highest-wins per concept: (1) precedent — concepts mapped by other
    systems under the SAME entity; (2) CONCEPT_AFFINITY[system_type] type-prior (ADR 0007);
    (3) name/alias substring vs the flat index. Returns ``[{concept,score,reason,
    column_match}]`` desc by score (ties → concept id); ``[]`` when nothing fits.

    Precedent and affinity are entity-wide: they never look at the column, so on
    their own they only RANK. ``column_support`` (:func:`column_support`) says how
    specifically the column itself names the concept and ``column_match`` is
    whether it does at all; a caller that LABELS a column picks with
    :func:`supported_reuse_choice` (provision's reuse-first step)."""
    out: dict[str, dict] = {}
    names: dict[str, str] = {}

    def _add(cid: str, score: float, reason: str) -> None:
        cur = out.get(cid)
        if cur is None or score > cur["score"]:
            out[cid] = {"concept": cid, "score": score, "reason": reason}

    for cid in _precedent_concepts(entity_id, query):
        _add(cid, _W_PRECEDENT, "precedent: mapped by another system on this entity")
    for cid in sorted(CONCEPT_AFFINITY.get(system_type, frozenset())):
        _add(cid, _W_AFFINITY, f"affinity: typical for {system_type}")
    for row in flat_index(query=query):
        names[row["id"]] = row.get("name", "")
        if _name_matches(column_name, row["id"], row.get("name", "")):
            _add(row["id"], _W_NAME, f"name match on {column_name!r}")
        elif column_support(column_name, row["id"], row.get("name", "")) > 0:
            # A field the concept itself lists (or whose tokens cover the concept
            # name) is a candidate even outside precedent and affinity.
            _add(row["id"], _W_NAME, f"listed field {column_name!r}")

    for cid, d in out.items():
        d["column_support"] = column_support(column_name, cid, names.get(cid, CONCEPT_NAME.get(cid, "")))
        d["column_match"] = d["column_support"] > 0
    return sorted(out.values(), key=lambda d: (-d["score"], d["concept"]))


# --- Entity-scoped inference: table → entity concept (ADR 0040) ------------

def _entity_precedent(field_concepts, query) -> set[str]:
    """Entity concepts other SchemaEntries map that CO-OCCUR with the given field
    concepts (ONE read). ``[]`` field_concepts ⇒ no read, ``set()`` (no-graph safe)."""
    fcs = sorted({c for c in (field_concepts or []) if c})
    if not fcs:
        return set()
    return {r["id"] for r in query(_ENTITY_PRECEDENT_CYPHER, {"fcs": fcs}) if r.get("id")}


def entity_suggestions(field_concepts, system_type, table_name,
                       *, query=neptune_query) -> list[dict]:
    """Ranked entity-concept candidates for a table (ADR 0040, entity-layer analogue
    of ``reuse_first_suggestions``).

    Three signals, highest-wins per entity concept: (1) precedent — entity concepts
    other SchemaEntries already map that co-occur with the SAME field concepts (one
    graph read); (2) ``CONCEPT_AFFINITY[system_type]`` type-prior filtered to
    entity-capable concepts (ADR 0007); (3) name/alias substring of ``table_name``
    against an entity concept's id/name. Candidates are always drawn from the entity
    layer only. Returns ``[{entity,score,reason}]`` desc by score (ties → entity id);
    ``[]`` when nothing fits. Import-pure — the injected ``query`` seam is the only I/O."""
    out: dict[str, dict] = {}

    def _add(cid: str, score: float, reason: str) -> None:
        cur = out.get(cid)
        if cur is None or score > cur["score"]:
            out[cid] = {"entity": cid, "score": score, "reason": reason}

    for cid in _entity_precedent(field_concepts, query):
        if is_entity(cid):
            _add(cid, _W_PRECEDENT, "precedent: entity co-occurs with these field concepts")
    for cid in sorted(CONCEPT_AFFINITY.get(system_type, frozenset())):
        if is_entity(cid):
            _add(cid, _W_AFFINITY, f"affinity: typical entity for {system_type}")
    for cid in entity_concepts():
        if _name_matches(table_name, cid, CONCEPT_NAME.get(cid, cid)):
            _add(cid, _W_NAME, f"name match on {table_name!r}")

    return sorted(out.values(), key=lambda d: (-d["score"], d["entity"]))


# --- Identity-key heuristic: table columns → join-key candidates (ADR 0040) --

# Business-key name suffixes/substrings (a value that identifies a record), distinct
# from the bare surrogate ``id`` (a per-system row number that never joins across
# systems). ``*_id`` (e.g. ``barcode_id``, ``work_order_id``) IS a business key here —
# unlike ``is_generic_field`` which rejects any ``*_id`` for MINTING purposes.
_BUSINESS_KEY_SUFFIXES = ("_id", "_number", "_no", "_code", "_sku", "_serial")
_BUSINESS_KEY_SUBSTRINGS = ("barcode", "serial")


def _roles_for_column(column: str, mappings) -> set[str]:
    """All roles asserted for ``column`` across ``mappings`` (case-insensitive column
    match). Accepts BOTH the flat shape ``{"field"/"column", "role"}`` and the nested
    map-prompt shape ``{"field", "mappings":[{"role",...}]}``. Pure."""
    col = (column or "").strip().lower()
    roles: set[str] = set()
    for m in mappings or []:
        name = (m.get("field") or m.get("column") or "").strip().lower()
        if name != col:
            continue
        if m.get("role"):
            roles.add(str(m["role"]))
        for inner in m.get("mappings") or []:
            if isinstance(inner, dict) and inner.get("role"):
                roles.add(str(inner["role"]))
    return roles


def _is_business_key(column: str) -> bool:
    """True iff ``column`` looks like a cross-system business key — NOT the bare surrogate
    ``id`` and NOT a generic/audit column. Pure, case-insensitive."""
    name = (column or "").strip().lower()
    if not name or name == "id" or name in GENERIC_FIELD_NAMES:
        return False
    return (name.endswith(_BUSINESS_KEY_SUFFIXES)
            or any(s in name for s in _BUSINESS_KEY_SUBSTRINGS))


def identity_field_candidates(columns, mappings) -> list[str]:
    """Deterministic identity-key candidates for a table (ADR 0040). Pure — no graph.

    Tiered, first non-empty tier wins, preserving ``columns`` order within a tier:
      1. Columns a mapping marks ``role='identifier'`` (the authoritative signal).
      2. Business-key names (``*_id``/``*_number``/``*_code``/``barcode``/``serial`` …),
         EXCLUDING the bare surrogate ``id`` and generic/audit columns.
    Returns ``[]`` when neither tier matches (e.g. an all-generic table). Never raises —
    fail-soft callers treat ``[]`` as "no explicit identity key inferred"."""
    cols = [c for c in (columns or []) if c]
    identifiers = [c for c in cols if "identifier" in _roles_for_column(c, mappings)]
    if identifiers:
        return identifiers
    return [c for c in cols if _is_business_key(c)]

