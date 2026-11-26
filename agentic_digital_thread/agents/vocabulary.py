# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Vocabulary read model (ADR 0050): the unified concept list behind
``GET /api/admin/vocabulary/concepts``.

A derived projection over ``concept_layer.flat_index()`` (core ∪ dynamic — ADR 0021)
enriched with ONE additional graph read for the per-concept Concept-node metadata
(``description`` / ``synonyms`` / ``retired`` / ``source``) and the mapped-field count
(``MATCH (c:Concept)<-[m:MAPS_TO]-() … count(m)``).

Design discipline — copied from ``concept_layer.py`` (ADR 0001 / 0020 / 0021):
  * Import-pure — no boto3/pg8000 at import. The ONLY impure seam is the injected
    ``query=neptune_query`` graph read (a default arg, swappable in tests); the seam is
    a LAZY wrapper that imports ``.db`` on first call and degrades to ``[]`` when
    runtime deps are absent (no-graph safe).
  * Graph failure degrades, never raises: a raising ``query`` yields the core-only
    projection (mirroring ``flat_index`` degradation) with zeroed counts/metadata.
  * Usage data is COMPOSED, not read here: the caller (the vocabulary route) passes an
    optional ``usage`` dict built from the artifact-usage ledger, keeping this module
    decoupled from DynamoDB.
"""
from __future__ import annotations


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


# --- Locked Cypher: the ONE additional read beyond flat_index() -------------
# Per-concept mapped_field_count (count of inbound MAPS_TO edges — the pack-uninstall
# guard shape) PLUS the Concept-node vocabulary properties, in a single round-trip.
_CONCEPT_DETAIL_CYPHER = (
    "MATCH (c:Concept) "
    "OPTIONAL MATCH (c)<-[m:MAPS_TO]-() "
    "RETURN c.id AS id, c.description AS description, c.synonyms AS synonyms, "
    "c.retired AS retired, c.source AS source, count(m) AS mapped_field_count"
)


def _no_graph_query(cypher: str, parameters=None) -> list:
    """Empty-graph seam used for degradation (core-only projection)."""
    return []


def provenance_label(source) -> str:
    """Human-facing provenance bucket derived from a Concept's ``source`` property.

    Exactly five shapes (ADR 0050): ``core`` | ``pack:<id>`` | ``pack:custom:<slug>``
    | ``minted`` | ``custom``. Pack sources are returned verbatim (``pack:custom:*``
    is just a pack whose id carries the ``custom:`` prefix — the bulk-import
    provenance). Anything else — ``'custom'``, user-asserted, legacy/dynamic markers,
    or a missing source — buckets to ``custom``. Pure."""
    s = (source or "").strip()
    if s == "core":
        return "core"
    if s == "minted":
        return "minted"
    if s.startswith("pack:"):
        return s
    return "custom"


def _as_synonym_list(value) -> list:
    """Normalize a Concept ``synonyms`` property to ``list[str]``.

    Graph nodes may carry the JSON array string the vocabulary apply writes, a
    list, a legacy comma-separated string, or nothing
    (:func:`vocab_import_core.decode_synonyms`). Pure."""
    from .vocab_import_core import decode_synonyms
    return decode_synonyms(value)


def _concept_row(row: dict, detail: dict, usage: dict, core_defs) -> dict:
    """Shape ONE fully-formed concept row (the ``GET /concepts`` row contract).

    Pure merge of a ``flat_index`` row with its ``_CONCEPT_DETAIL_CYPHER`` record
    and the caller-composed usage entry. Descriptions resolve like
    ``concept_layer.concept_description``: core ids take their curated
    ``core_defs`` text; everything else reads the graph value (absent/null → ``''``)."""
    cid = row["id"]
    d = detail.get(cid) or {}
    source = row.get("source") or d.get("source") or ""
    if cid in core_defs:
        description = core_defs[cid]
    else:
        description = d.get("description") or ""
    u = usage.get(cid) or {}
    return {
        "id": cid,
        "name": row.get("name") or cid,
        "description": description,
        "synonyms": _as_synonym_list(d.get("synonyms")),
        "layer": row.get("layer") or "",
        "source": source,
        "provenance_label": provenance_label(source),
        "mapped_field_count": int(d.get("mapped_field_count") or 0),
        "usage_30d": int(u.get("usage_30d") or 0),
        "last_used_at": u.get("last_used_at"),
        "retired": bool(d.get("retired")),
    }


def list_concepts(query=neptune_query, usage=None) -> list[dict]:
    """The vocabulary read model: every concept as a fully-shaped row.

    Merges ``concept_layer.flat_index()`` (core ∪ dynamic; core wins on id collision)
    with ONE additional graph read (``_CONCEPT_DETAIL_CYPHER``) for per-concept
    ``mapped_field_count`` and the Concept-node ``description``/``synonyms``/
    ``retired``/``source`` properties, plus an optional caller-composed ``usage``
    dict (``concept_id → {"usage_30d": int, "last_used_at": str|None}``).

    Row shape (the ``GET /concepts`` contract):
    ``{id, name, description, synonyms, layer, source, provenance_label,
    mapped_field_count, usage_30d, last_used_at, retired}``.

    Descriptions resolve like ``concept_layer.concept_description``: core ids take
    their curated ``CORE_CONCEPT_DEFINITIONS`` text; everything else reads the graph
    value (absent/null → ``''``). A raising ``query`` degrades to the core-only
    projection (zeroed counts/metadata) — never raises. Import-pure (lazy imports;
    injected query seam)."""
    from . import concept_layer  # lazy: keeps this module import-light
    from .catalog import CORE_CONCEPT_DEFINITIONS  # lazy (ADR 0020 discipline)

    try:
        base = concept_layer.flat_index(query=query)
    except Exception:  # graph failure → core-only list (mirror flat_index degradation)
        base = concept_layer.flat_index(query=_no_graph_query)

    try:
        detail_rows = query(_CONCEPT_DETAIL_CYPHER)
    except Exception:  # graph failure → no metadata/counts, list still serves
        detail_rows = []
    detail = {r.get("id"): r for r in (detail_rows or []) if r.get("id")}

    usage = usage or {}
    return [_concept_row(row, detail, usage, CORE_CONCEPT_DEFINITIONS) for row in base]
