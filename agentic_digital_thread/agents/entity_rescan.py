# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""entity_rescan — first-class, idempotent entity/identity rescan (ADR-0040 D6).

Re-runs the layered table→entity classification (curated → heuristic → LLM; see
``agents.onboard._classify_table_entity``) for ALREADY-onboarded systems and writes the
missing ``SchemaEntry-[:MAPS_TO]->Concept`` entity edges plus
``SchemaEntry-[:IDENTIFIED_BY]->Field`` identity-key edges to the graph. This is the
library core behind BOTH:

  * the admin API — ``POST /api/admin/entities/rescan[/{system_id}]`` in
    ``agents.admin_routes``, and
  * the CLI — ``scripts/backfill_entity_identity.py`` (a thin wrapper).

Guarantees (identical to the provision-time write path — no new review flow):

  * ADDITIVE   — only MERGEs new nodes/edges; nothing is ever deleted.
  * IDEMPOTENT — the selector (:func:`tables_missing_entity_edge`) skips any SchemaEntry
    that already carries an entity edge, and every write is a MERGE. Re-running when the
    graph already has the edges writes nothing new (``entities_written == 0``).
  * PROVENANCE — an LLM-minted entity Concept is MERGEd with the SAME marker onboarding
    uses (``source='minted'``, ``layer='entity'``); see onboard.py's provision block.

The graph/LLM seams (``query`` / ``write`` / ``classify`` / ``catalog_get``) are
injectable keyword arguments so the logic is offline-unit-testable
(``tests/unit/test_entity_rescan.py``); the defaults resolve lazily to the live seams.
"""
from __future__ import annotations

import logging

from .public_errors import public_error_text

logger = logging.getLogger(__name__)


class UnknownSystemError(ValueError):
    """Raised when a rescan names a system that is not in the onboarded catalog."""


# =========================================================================
# PURE selection logic (offline-unit-tested — no graph, no boto3)
# =========================================================================

def tables_missing_entity_edge(schema_rows: list[dict]) -> list[tuple[str, str]]:
    """Return the ``(system_id, table)`` pairs that LACK a
    ``SchemaEntry-[:MAPS_TO]->Concept`` entity edge, deterministically sorted.

    ``schema_rows`` are the rows of the "all SchemaEntries + their entity-edge count"
    graph read: each ``{"system_id", "table", "entity_edges": int}`` (``entity_edges``
    also accepted under the alias ``has_entity`` as a truthy flag). This is the crux of
    IDEMPOTENCY: a SchemaEntry that already carries an entity edge is NEVER selected, so a
    re-run does nothing.
    """
    missing: set[tuple[str, str]] = set()
    for r in schema_rows:
        sid = r.get("system_id")
        tbl = r.get("table")
        if not sid or not tbl:
            continue
        count = r.get("entity_edges")
        if count is None:
            count = 1 if r.get("has_entity") else 0
        if not count:
            missing.add((sid, tbl))
    return sorted(missing)


def restrict_to_systems(pairs: list[tuple[str, str]],
                        systems: list[str] | None) -> list[tuple[str, str]]:
    """Filter/order the missing pairs. With no ``systems`` filter the input order (already
    deterministically sorted) is kept; with a filter, pairs are grouped in the caller's
    system order (front-loading), tables sorted within each system."""
    if not systems:
        return pairs
    wanted = [s.strip() for s in systems if s.strip()]
    by_sys: dict[str, list[str]] = {}
    for sid, tbl in pairs:
        if sid in wanted:
            by_sys.setdefault(sid, []).append(tbl)
    ordered: list[tuple[str, str]] = []
    for sid in wanted:
        for tbl in sorted(by_sys.get(sid, [])):
            ordered.append((sid, tbl))
    return ordered


# =========================================================================
# Graph I/O helpers (thin, seam-driven)
# =========================================================================

def _load_schema_rows(query) -> list[dict]:
    """Read every onboarded SchemaEntry with a count of its entity edges."""
    rows = query(
        "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry) "
        "OPTIONAL MATCH (se)-[:MAPS_TO]->(c:Concept) "
        "RETURN s.id AS system_id, se.name AS table, count(c) AS entity_edges",
        {}
    ) or []
    return rows


def _load_table_context(sid: str, tbl: str, query) -> tuple[list[str], list[dict]]:
    """Read a table's columns + their existing field-concept mappings from the graph, in
    the shape ``_classify_table_entity`` expects (``columns``, ``mappings``)."""
    rows = query(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {name: $tbl})-[:HAS_FIELD]->(f:Field) "
        "OPTIONAL MATCH (f)-[r:MAPS_TO]->(c:Concept) "
        "RETURN f.name AS field, collect({concept: c.id, role: r.role, confidence: r.confidence}) AS maps",
        {"sid": sid, "tbl": tbl}
    ) or []
    columns: list[str] = []
    mappings: list[dict] = []
    for r in rows:
        fld = r.get("field")
        if not fld:
            continue
        columns.append(str(fld).split(".")[-1])
        role_maps = [m for m in (r.get("maps") or []) if m.get("concept")]
        mappings.append({"table": tbl, "field": fld,
                         "concept": (role_maps[0]["concept"] if role_maps else ""),
                         "mappings": role_maps})
    return columns, mappings


def _write_entity_edges(sid: str, tbl: str, resolved: dict, *, write,
                        dry_run: bool) -> tuple[int, int]:
    """Write the entity + IDENTIFIED_BY edges for one classified table. MERGE-only
    (idempotent), byte-identical to the provision-time write path — a minted entity
    Concept carries onboarding's exact provenance marker (``source='minted'``,
    ``layer='entity'``). Returns ``(entity_edges, identity_edges)`` written (would-be
    counts in dry-run, with no writes performed)."""
    entity = resolved.get("entity")
    if not entity:
        logger.info("  %s.%s → no entity (fail-soft) — skipping", sid, tbl)
        return 0, 0
    seid = f"{sid}.{tbl}"
    mint = resolved.get("mint")
    id_fields = [c for c in (resolved.get("identity_fields") or [])
                 if str(c).split(".")[-1]]
    if dry_run:
        logger.info("  [dry-run] %s.%s → entity=%s%s identity=%s",
                    sid, tbl, entity, " (MINT)" if mint else "", id_fields)
        return 1, len(id_fields)
    if mint:
        # SAME provenance marker as onboarding's provision write (onboard.py):
        # source='minted', layer='entity' — no new review flow.
        write(
            "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $name, "
            "c.source = 'minted', c.layer = 'entity', c.description = $desc",
            {"cid": entity, "name": mint.get("name") or entity,
             "desc": mint.get("description") or ""}
        )
    write(
        "MATCH (se:SchemaEntry {id: $seid}), (c:Concept {id: $entity}) "
        "MERGE (se)-[:MAPS_TO]->(c)",
        {"seid": seid, "entity": entity}
    )
    identity_edges = 0
    for col in id_fields:
        col_clean = str(col).split(".")[-1]
        write(
            "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) "
            "MERGE (se)-[:IDENTIFIED_BY]->(f)",
            {"seid": seid, "fid": f"{sid}.{tbl}.{col_clean}"}
        )
        identity_edges += 1
    logger.info("  %s.%s → entity=%s%s identity=%s",
                sid, tbl, entity, " (MINT)" if mint else "", id_fields)
    return 1, identity_edges


def _empty_summary() -> dict:
    return {"tables_scanned": 0, "entities_written": 0, "identities_written": 0,
            "skipped_existing": 0, "tier_counts": {}, "errors": []}


# =========================================================================
# The library entry point
# =========================================================================

def rescan_entities(system_id: str | None = None, *, dry_run: bool = False,
                    query=None, write=None, classify=None, catalog_get=None) -> dict:
    """Re-run the ADR-0040 entity/identity classification for onboarded systems.

    ``system_id=None`` rescans EVERY onboarded system found in the graph; a specific
    ``system_id`` restricts the rescan to that system (raising
    :class:`UnknownSystemError` when it is not in the catalog).

    Returns::

        {"dry_run": bool,
         "systems": {"<system_id>": {
             "tables_scanned": int,      # SchemaEntries for the system considered
             "entities_written": int,    # MAPS_TO entity edges written this run
             "identities_written": int,  # IDENTIFIED_BY edges written this run
             "skipped_existing": int,    # tables skipped: entity edge already present
             "tier_counts": {tier: int}, # classification tier per newly-classified table
                                         # (override|curated|excluded|heuristic|llm|none)
             "errors": ["<table>: <msg>", ...],
         }, ...}}

    IDEMPOTENT: tables whose SchemaEntry already carries an entity edge are counted in
    ``skipped_existing`` and never re-classified/re-written, so a second run over an
    up-to-date graph reports ``entities_written == identities_written == 0``. In
    ``dry_run`` the counts reflect what WOULD be written; no graph write is performed.

    ``query``/``write``/``classify``/``catalog_get`` are injectable seams (offline
    tests); the defaults resolve lazily to the live app seams.
    """
    if query is None or write is None:
        from agents.db import neptune_query, neptune_write
        query = query or neptune_query
        write = write or neptune_write
    if classify is None:
        from agents.onboard import _classify_table_entity
        classify = _classify_table_entity
    if catalog_get is None:
        from agents.catalog import catalog
        catalog_get = catalog.get

    if system_id is not None and catalog_get(system_id) is None:
        raise UnknownSystemError(f"Unknown system: {system_id}")

    schema_rows = _load_schema_rows(query)
    systems_filter = [system_id] if system_id else None

    # Group ALL rows per system first so tables_scanned / skipped_existing are
    # reported even when nothing is missing (the idempotent no-op re-run).
    summaries: dict[str, dict] = {}
    for r in schema_rows:
        sid, tbl = r.get("system_id"), r.get("table")
        if not sid or not tbl:
            continue
        if system_id is not None and sid != system_id:
            continue
        s = summaries.setdefault(sid, _empty_summary())
        s["tables_scanned"] += 1
        count = r.get("entity_edges")
        if count is None:
            count = 1 if r.get("has_entity") else 0
        if count:
            s["skipped_existing"] += 1
    if system_id is not None and system_id not in summaries:
        # In the catalog but no SchemaEntries in the graph yet — report an empty,
        # well-formed summary rather than failing (nothing to rescan).
        summaries[system_id] = _empty_summary()

    missing = restrict_to_systems(tables_missing_entity_edge(schema_rows), systems_filter)
    logger.info("entity_rescan: %d SchemaEntry(ies) missing an entity edge%s.",
                len(missing), f" (system={system_id})" if system_id else "")

    for sid, tbl in missing:
        s = summaries.setdefault(sid, _empty_summary())
        entry = catalog_get(sid) or {}
        schema = entry.get("schema", "") or ""
        ctype = entry.get("connection_type", "") or ""
        try:
            columns, mappings = _load_table_context(sid, tbl, query)
            resolved = classify(sid, schema, tbl, columns, mappings,
                                connection_type=ctype)
        except Exception as exc:  # noqa: BLE001 - per-table fail-soft (e.g. vocabulary gap)
            logger.warning("  %s.%s → classify raised (%s); skipping",
                           sid, tbl, type(exc).__name__)
            s["errors"].append(f"{tbl}: {public_error_text(exc, 'entity_rescan.classify')}")
            continue
        tier = resolved.get("tier") or ("none" if not resolved.get("entity") else "llm")
        s["tier_counts"][tier] = s["tier_counts"].get(tier, 0) + 1
        try:
            ents, ids = _write_entity_edges(sid, tbl, resolved, write=write,
                                            dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - per-table fail-soft on a write error
            logger.warning("  %s.%s → write raised (%s); skipping",
                           sid, tbl, type(exc).__name__)
            s["errors"].append(f"{tbl}: " + public_error_text(
                exc, "entity_rescan.write", default="graph_query_failed"))
            continue
        s["entities_written"] += ents
        s["identities_written"] += ids

    return {"dry_run": dry_run, "systems": summaries}
