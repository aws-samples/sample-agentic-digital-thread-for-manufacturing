# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only graph-shape audit for the onboarding pipeline (S0.1).

This module verifies that the discover -> map -> resolve -> provision pipeline
actually produced the three facts the deterministic resolver depends on for at
least one onboarded system:

  (a) field -> concept mappings  (System-HAS_SCHEMA->SchemaEntry-HAS_FIELD->Field-MAPS_TO->Concept)
  (b) authority                  (AuthorityRule-APPLIES_TO->System, with ar.concept property)
  (c) joins                      (JoinPath {from_system/to_system ...}, also FROM_SYSTEM/TO_SYSTEM edges)

and, when a system owning ``:Property`` nodes is onboarded (the i3X telemetry
layer — SiteWise (i3X) connection type), a fourth family:

  (d) property layer             (Asset-HAS_PROPERTY->Property ownership,
                                  Asset.property_count == HAS_PROPERTY out-degree,
                                  and NO orphaned Property graph-wide)

Family (d) SKIPS (pass) when no system owns Property nodes — relational-only
deployments stay green. The graph-wide orphan check always runs (it passes
trivially on a Property-free graph) because an orphaned ``:Property`` after a
reset/offboard indicates an interrupted cleanup (``onboard_reset_system``
DETACH-DELETEs the system's Property layer) or a writer that skipped the
``HAS_PROPERTY`` edge.

A fifth family covers the equipment identity bridge:

  (e) equipment VM layer          (every equipment-concept ValueMapping with a
                                   non-empty ``to_element_id`` has EXACTLY ONE
                                   ``MAPS_TO_ASSET`` edge whose target Asset's
                                   ``source_id`` equals ``to_element_id``; and
                                   NO ``MAPS_TO_ASSET`` edge leaves a VM missing
                                   ``to_element_id`` — the writer guard)

Family (e) SKIPS (pass) when no equipment VM carries ``to_element_id`` AND no
``MAPS_TO_ASSET`` edge exists (relational-only / pre-bridge graphs stay
green). A report-only metamodel probe (``probe_metamodel_edges``) additionally
reconciles ``:MetaEdge`` rows against live edge types both directions:
documented-but-absent is INFO not FAIL (the metamodel may lead the data);
live-but-undocumented mirrors the fetch-side drift suffix the orchestrator
sees (``metamodel.fetch_metamodel_text``).

It is strictly READ-ONLY: it only calls ``agents.db.neptune_query`` (graph reads)
and, for the optional best-effort concept value read, the read-only
``agents.db.query_pg``. It NEVER calls ``neptune_write``, never touches
``dashboard.py`` / ``UNIFIED_*_SQL``, and never invokes an LLM.

Run it standalone from ``agentic_digital_thread/``::

    python -m tests.audit.audit_onboarding_graph --system-id cmms

When no graph is reachable or nothing has been onboarded, it prints a clear
"no onboarded system found — nothing to audit" message and exits 0 (no
traceback). It exits non-zero ONLY when the (a)/(b)/(c)/(d) hard assertions
fail against a reachable graph.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from agents.db import neptune_query, query_pg, validate_identifier

# ---------------------------------------------------------------------------
# Canonical fact-family cypher (a / b / c).
#
# These strings are the SOURCE OF TRUTH for the graph-shape contract doc — they
# are copied verbatim there so doc and code provably agree. They mirror the
# resolver-relevant traversals in:
#   - orchestrator.py::get_system_schema   (System-HAS_SCHEMA->SchemaEntry-HAS_FIELD->Field-MAPS_TO->Concept)
#   - graph_analytics.py::concept_coverage / impact   (MAPS_TO {role, confidence}, JoinPath {from_system,...})
#   - onboard.py writers                    (MAPS_TO sets r.confidence/r.role; AuthorityRule; JoinPath)
# Do NOT edit these without updating docs/graph-shape-contract.md in lockstep.
# ---------------------------------------------------------------------------

# (a) field -> concept mappings
CYPHER_FIELD_CONCEPT = (
    "MATCH (s:System {id:$sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept) "
    "RETURN se.name AS table, f.name AS column, c.id AS concept, r.role AS role, r.confidence AS confidence"
)

# (b) authority
CYPHER_AUTHORITY = (
    "MATCH (ar:AuthorityRule)-[:APPLIES_TO]->(s:System {id:$sid}) "
    "RETURN ar.concept, ar.system_id, ar.rule, ar.priority"
)

# (c) joins
CYPHER_JOINS = (
    "MATCH (jp:JoinPath) WHERE jp.from_system=$sid OR jp.to_system=$sid "
    "RETURN jp.from_system, jp.from_table, jp.from_field, jp.to_system, jp.to_table, jp.to_field"
)

# (d) i3X telemetry / property layer. Asserted only when a system owning
# >=1 :Property node is onboarded (the i3X walker is the sole Property
# writer); the orphan probe is graph-wide and always runs.
CYPHER_SYSTEMS_WITH_PROPERTIES = (
    "MATCH (s:System)<-[:FROM_SYSTEM]-(:Property) RETURN DISTINCT s.id"
)
CYPHER_PROPERTY_TOTAL = (
    "MATCH (p:Property {system:$sid}) RETURN count(p) AS n"
)
CYPHER_PROPERTY_WITHOUT_OWNER = (
    "MATCH (p:Property {system:$sid}) WHERE NOT (p)<-[:HAS_PROPERTY]-(:Asset) RETURN count(p) AS n"
)
CYPHER_ASSET_PROPERTY_DEGREE = (
    "MATCH (a:Asset {system:$sid}) OPTIONAL MATCH (a)-[:HAS_PROPERTY]->(p:Property) "
    "RETURN a.source_id AS source_id, a.name AS name, a.property_count AS property_count, count(p) AS degree"
)
CYPHER_ORPHANED_PROPERTIES = (
    "MATCH (p:Property) WHERE NOT (p)<-[:HAS_PROPERTY]-(:Asset) RETURN count(p) AS n"
)

# (e) equipment ValueMapping / MAPS_TO_ASSET layer. Writer:
# onboard.py::_persist_equipment_value_mappings — the MAPS_TO_ASSET MERGE is
# guarded on a non-empty ``to_element_id``, so the two invariants below hold
# by construction; the audit proves them against the live graph.
CYPHER_EQUIPMENT_VM_ASSET_EDGES = (
    "MATCH (vm:ValueMapping {concept:$concept}) WHERE vm.to_element_id <> '' "
    "OPTIONAL MATCH (vm)-[:MAPS_TO_ASSET]->(a:Asset) "
    "RETURN vm.id AS vm_id, vm.to_element_id AS to_element_id, "
    "count(a) AS edge_count, collect(a.source_id) AS asset_source_ids"
)
CYPHER_MAPS_TO_ASSET_FROM_BARE_VM = (
    "MATCH (vm:ValueMapping)-[:MAPS_TO_ASSET]->(:Asset) "
    "WHERE vm.to_element_id IS NULL OR vm.to_element_id = '' RETURN count(vm) AS n"
)

# Metamodel reconciliation (report-only): :MetaEdge rows vs live edge types.
CYPHER_META_EDGE_NAMES = (
    "MATCH (me:MetaEdge) RETURN DISTINCT me.name AS name"
)
CYPHER_LIVE_EDGE_TYPES = (
    "MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel"
)

# Reachability / target discovery: any system that has >=1 MAPS_TO edge.
CYPHER_SYSTEMS_WITH_MAPPINGS = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->(:Concept) "
    "RETURN DISTINCT s.id"
)

# Supplementary probes (NOT part of the canonical fact families) — report-only.
CYPHER_JOINS_FROM_SYSTEM_EDGE = (
    "MATCH (jp:JoinPath)-[:FROM_SYSTEM]->(s:System {id:$sid}) RETURN count(jp) AS n"
)
CYPHER_JOINS_TO_SYSTEM_EDGE = (
    "MATCH (jp:JoinPath)-[:TO_SYSTEM]->(s:System {id:$sid}) RETURN count(jp) AS n"
)
CYPHER_IDENTITY_RESOLVES = (
    "MATCH (:IdentityRule)-[:RESOLVES]->(:Concept) RETURN count(*) AS n"
)
CYPHER_IDENTITY_LINKS_CONCEPT = (
    "MATCH (:IdentityRule)-[:LINKS_CONCEPT]->(:Concept) RETURN count(*) AS n"
)

# Path of the JSON evidence report the findings doc cites.
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last-run-report.json")

NOTHING_TO_AUDIT_MSG = "no onboarded system found — nothing to audit"


# ---------------------------------------------------------------------------
# Small helpers (defensive parsing — Neptune returns aliased OR expression keys)
# ---------------------------------------------------------------------------

def _row_get(row: dict, *keys):
    """Return the first present key from a result row (handles 'col' vs 'ar.col')."""
    for k in keys:
        if k in row:
            return row[k]
    return None


def _count(cypher: str, params: dict | None = None) -> int:
    """Run a count query and pull the first numeric value out (0 on empty/error)."""
    rows = neptune_query(cypher, params)
    if not rows:
        return 0
    first = rows[0]
    if isinstance(first, dict):
        for v in first.values():
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def pick_target_system(preferred: tuple[str, ...] = ("cmms", "erp")) -> str | None:
    """Return a system id with >=1 MAPS_TO edge.

    Prefers the ids in ``preferred`` (in order); otherwise falls back to any
    system that has mappings. Returns ``None`` when no graph is reachable or no
    system has been onboarded (no MAPS_TO edges anywhere) — this doubles as the
    reachability check the runner and the pytest wrapper key off of.
    """
    rows = neptune_query(CYPHER_SYSTEMS_WITH_MAPPINGS)
    ids = [sid for sid in (_row_get(r, "s.id", "id") for r in (rows or [])) if sid]
    if not ids:
        return None
    for p in preferred:
        if p in ids:
            return p
    return ids[0]


# ---------------------------------------------------------------------------
# Fact family (a): field -> concept mappings
# ---------------------------------------------------------------------------

def assert_field_concept_mappings(system_id: str) -> dict:
    """Verify fact family (a): >=1 field->concept mapping for the system.

    Records the row count, the distinct concepts mapped, and how many rows are
    missing ``role`` or ``confidence`` (these counts feed a contract finding).
    """
    rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": system_id}) or []
    concepts: set[str] = set()
    missing_role = 0
    missing_confidence = 0
    samples = []
    for r in rows:
        concept = _row_get(r, "concept", "c.id")
        role = _row_get(r, "role", "r.role")
        confidence = _row_get(r, "confidence", "r.confidence")
        if concept:
            concepts.add(concept)
        if role in (None, ""):
            missing_role += 1
        if confidence in (None, ""):
            missing_confidence += 1
        if len(samples) < 5:
            samples.append({
                "table": _row_get(r, "table", "se.name"),
                "column": _row_get(r, "column", "f.name"),
                "concept": concept,
                "role": role,
                "confidence": confidence,
            })
    count = len(rows)
    return {
        "pass": count >= 1,
        "cypher": CYPHER_FIELD_CONCEPT,
        "system_id": system_id,
        "count": count,
        "distinct_concepts": sorted(concepts),
        "distinct_concept_count": len(concepts),
        "rows_missing_role": missing_role,
        "rows_missing_confidence": missing_confidence,
        "sample": samples,
    }


# ---------------------------------------------------------------------------
# Fact family (b): authority
# ---------------------------------------------------------------------------

def assert_authority(system_id: str) -> dict:
    """Verify fact family (b): >=1 AuthorityRule resolvable to the system.

    Pass requires at least one AuthorityRule (via APPLIES_TO) whose ``ar.concept``
    property names a concept the system actually maps. Authority is queried
    canonically by the ``ar.concept`` *property*; there is no ABOUT_CONCEPT edge
    (removed per the S0.1 audit [F3] / ADR-0011 — it was a redundant lazy backfill).
    """
    rows = neptune_query(CYPHER_AUTHORITY, {"sid": system_id}) or []
    rules = []
    rule_concepts: set[str] = set()
    for r in rows:
        concept = _row_get(r, "ar.concept", "concept")
        if concept:
            rule_concepts.add(concept)
        rules.append({
            "concept": concept,
            "system_id": _row_get(r, "ar.system_id", "system_id"),
            "rule": _row_get(r, "ar.rule", "rule"),
            "priority": _row_get(r, "ar.priority", "priority"),
        })

    # Concepts the system actually maps (fact family a) — authority should refer
    # to at least one of them to be "resolvable to a concept the system maps".
    mapped_rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": system_id}) or []
    mapped_concepts = {c for c in (_row_get(m, "concept", "c.id") for m in mapped_rows) if c}
    resolvable = sorted(rule_concepts & mapped_concepts)

    queryable_by_property = len(rule_concepts) >= 1

    return {
        # Pass: an authority fact exists for the system AND points at a concept
        # the system maps. (If authority exists but no overlap, that is itself a
        # finding, so still surface counts.)
        "pass": len(rules) >= 1 and len(resolvable) >= 1,
        "cypher": CYPHER_AUTHORITY,
        "system_id": system_id,
        "count": len(rules),
        "rule_concepts": sorted(rule_concepts),
        "resolvable_to_mapped_concepts": resolvable,
        "queryable_by_property": queryable_by_property,
        "rules": rules[:20],
    }


# ---------------------------------------------------------------------------
# Fact family (c): joins
# ---------------------------------------------------------------------------

def assert_joins(system_id: str) -> dict:
    """Verify fact family (c): >=1 JoinPath touching the system.

    Pass is based on the *property* form (``jp.from_system`` / ``jp.to_system``).
    Also probes the *edge* form (``FROM_SYSTEM`` / ``TO_SYSTEM``) and reports
    property-vs-edge agreement, since JoinPath endpoints are stored both ways.
    """
    rows = neptune_query(CYPHER_JOINS, {"sid": system_id}) or []
    joins = []
    for r in rows:
        joins.append({
            "from_system": _row_get(r, "jp.from_system", "from_system"),
            "from_table": _row_get(r, "jp.from_table", "from_table"),
            "from_field": _row_get(r, "jp.from_field", "from_field"),
            "to_system": _row_get(r, "jp.to_system", "to_system"),
            "to_table": _row_get(r, "jp.to_table", "to_table"),
            "to_field": _row_get(r, "jp.to_field", "to_field"),
        })

    property_count = len(rows)
    from_edge_count = _count(CYPHER_JOINS_FROM_SYSTEM_EDGE, {"sid": system_id})
    to_edge_count = _count(CYPHER_JOINS_TO_SYSTEM_EDGE, {"sid": system_id})
    edge_count = from_edge_count + to_edge_count

    return {
        "pass": property_count >= 1,
        "cypher": CYPHER_JOINS,
        "system_id": system_id,
        "count": property_count,
        "property_count": property_count,
        "from_system_edge_count": from_edge_count,
        "to_system_edge_count": to_edge_count,
        "edge_count": edge_count,
        # Do property and edge representations agree on "there are joins"?
        "property_edge_agreement": (property_count >= 1) == (edge_count >= 1),
        "joins": joins[:20],
    }


# ---------------------------------------------------------------------------
# Fact family (d): i3X telemetry / property layer
# ---------------------------------------------------------------------------

def pick_i3x_target_system() -> str | None:
    """Return a system id that owns >=1 ``:Property`` node, or ``None``.

    Property nodes are written only by the i3X walker
    (``agents/onboard_i3x.py::provision_i3x_hierarchy``), so a system reached
    through ``(:Property)-[:FROM_SYSTEM]->(:System)`` is an onboarded
    i3X-style system. ``None`` doubles as the family-(d) skip signal (no
    reachable graph, or no telemetry-rich system onboarded).
    """
    rows = neptune_query(CYPHER_SYSTEMS_WITH_PROPERTIES)
    ids = sorted(sid for sid in (_row_get(r, "s.id", "id") for r in (rows or [])) if sid)
    return ids[0] if ids else None


def assert_property_ownership(system_id: str) -> dict:
    """Verify fact family (d1): every ``:Property`` of the system has an
    owning ``:Asset`` via ``HAS_PROPERTY``.

    Pass requires >=1 Property for the system (the target was picked because
    it owns some) and ZERO Properties without an owning Asset. An unowned
    Property is unreachable by the orchestrator's asset-centric traversals.
    """
    total = _count(CYPHER_PROPERTY_TOTAL, {"sid": system_id})
    unowned = _count(CYPHER_PROPERTY_WITHOUT_OWNER, {"sid": system_id})
    return {
        "pass": total >= 1 and unowned == 0,
        "cypher": CYPHER_PROPERTY_WITHOUT_OWNER,
        "system_id": system_id,
        "property_count": total,
        "unowned_count": unowned,
    }


def assert_property_count_degree(system_id: str) -> dict:
    """Verify fact family (d2): ``Asset.property_count`` equals the actual
    ``HAS_PROPERTY`` out-degree for every asset of the system.

    The i3X walker stamps EVERY asset it writes (0 = structural-only asset
    holding no telemetry of its own), so a missing stamp is itself a mismatch
    — the orchestrator's property_count=0 guard reads the stamp, not the
    edges.
    """
    rows = neptune_query(CYPHER_ASSET_PROPERTY_DEGREE, {"sid": system_id}) or []
    mismatch_count = 0
    missing_stamp = 0
    samples = []
    for r in rows:
        stamped = _row_get(r, "property_count", "a.property_count")
        try:
            degree = int(_row_get(r, "degree", "count(p)") or 0)
        except (TypeError, ValueError):
            degree = 0
        ok = stamped is not None and int(stamped) == degree
        if ok:
            continue
        mismatch_count += 1
        if stamped is None:
            missing_stamp += 1
        if len(samples) < 20:
            samples.append({
                "source_id": _row_get(r, "source_id", "a.source_id"),
                "name": _row_get(r, "name", "a.name"),
                "property_count": stamped,
                "has_property_degree": degree,
            })
    return {
        "pass": len(rows) >= 1 and mismatch_count == 0,
        "cypher": CYPHER_ASSET_PROPERTY_DEGREE,
        "system_id": system_id,
        "asset_count": len(rows),
        "mismatch_count": mismatch_count,
        "assets_missing_stamp": missing_stamp,
        "mismatches": samples,
    }


def assert_no_orphaned_properties() -> dict:
    """Verify fact family (d3): NO ``:Property`` anywhere lacks an owning
    ``:Asset`` (graph-wide, not per-system).

    Reset/offboard DETACH-DELETEs the system's Property layer alongside its
    Assets (``onboard.py::onboard_reset_system``; the global reset label list
    includes ``Property``), so an orphaned Property indicates an interrupted
    cleanup or a writer that skipped the ``HAS_PROPERTY`` edge. Trivially
    passes on a graph with no Property nodes.
    """
    orphans = _count(CYPHER_ORPHANED_PROPERTIES)
    return {
        "pass": orphans == 0,
        "cypher": CYPHER_ORPHANED_PROPERTIES,
        "orphaned_property_count": orphans,
    }


def audit_property_layer() -> dict:
    """Combined family-(d) per-system report; skips (pass) when no system
    owns Property nodes so relational-only deployments stay green."""
    target = pick_i3x_target_system()
    if target is None:
        return {
            "status": "skipped",
            "target_system": None,
            "pass": True,
            "reason": "no onboarded system owns :Property nodes (no i3X-style system to audit)",
        }
    ownership = assert_property_ownership(target)
    count_degree = assert_property_count_degree(target)
    return {
        "status": "checked",
        "target_system": target,
        "pass": bool(ownership["pass"] and count_degree["pass"]),
        "ownership": ownership,
        "count_degree": count_degree,
    }


# ---------------------------------------------------------------------------
# Fact family (e): equipment ValueMapping / MAPS_TO_ASSET layer
# ---------------------------------------------------------------------------

def assert_equipment_vm_asset_links(rows: list) -> dict:
    """Verify fact family (e1/e2): every equipment-concept ValueMapping with a
    non-empty ``to_element_id`` has EXACTLY ONE ``MAPS_TO_ASSET`` edge, and the
    target Asset's ``source_id`` equals ``vm.to_element_id``.

    ``rows`` come from ``CYPHER_EQUIPMENT_VM_ASSET_EDGES`` (one row per such
    VM, with the edge count and collected target source_ids). Vacuously passes
    on zero rows — the SKIP decision lives in ``audit_equipment_vm_layer``.
    """
    mismatch_count = 0
    samples = []
    for r in rows:
        teid = _row_get(r, "to_element_id", "vm.to_element_id")
        try:
            edge_count = int(_row_get(r, "edge_count", "count(a)") or 0)
        except (TypeError, ValueError):
            edge_count = 0
        targets = list(_row_get(r, "asset_source_ids", "collect(a.source_id)") or [])
        if edge_count == 1 and targets == [teid]:
            continue
        mismatch_count += 1
        if len(samples) < 20:
            samples.append({
                "vm_id": _row_get(r, "vm_id", "vm.id"),
                "to_element_id": teid,
                "edge_count": edge_count,
                "asset_source_ids": targets[:3],
            })
    return {
        "pass": mismatch_count == 0,
        "cypher": CYPHER_EQUIPMENT_VM_ASSET_EDGES,
        "vm_count": len(rows),
        "mismatch_count": mismatch_count,
        "mismatches": samples,
    }


def assert_no_bare_vm_asset_edges() -> dict:
    """Verify fact family (e3): NO ``MAPS_TO_ASSET`` edge leaves a ValueMapping
    missing ``to_element_id`` (the writer MERGEs the edge only when the element
    id is non-empty — a bare-VM edge means a foreign writer or a corrupted VM).
    Trivially passes on a graph with no MAPS_TO_ASSET edges."""
    n = _count(CYPHER_MAPS_TO_ASSET_FROM_BARE_VM)
    return {
        "pass": n == 0,
        "cypher": CYPHER_MAPS_TO_ASSET_FROM_BARE_VM,
        "bare_vm_edge_count": n,
    }


def audit_equipment_vm_layer() -> dict:
    """Combined family-(e) report; skips (pass) when no equipment VM carries
    ``to_element_id`` AND no MAPS_TO_ASSET edge exists, so relational-only /
    pre-bridge graphs stay green."""
    rows = neptune_query(
        CYPHER_EQUIPMENT_VM_ASSET_EDGES, {"concept": "equipment"}) or []
    bare = assert_no_bare_vm_asset_edges()
    if not rows and bare["bare_vm_edge_count"] == 0:
        return {
            "status": "skipped",
            "pass": True,
            "reason": ("no equipment ValueMapping carries to_element_id and no "
                       "MAPS_TO_ASSET edge exists (no equipment identity bridge to audit)"),
        }
    links = assert_equipment_vm_asset_links(rows)
    return {
        "status": "checked",
        "pass": bool(links["pass"] and bare["pass"]),
        "links": links,
        "bare_vm_edges": bare,
    }


# ---------------------------------------------------------------------------
# Probe (report-only): metamodel vs live edge types
# ---------------------------------------------------------------------------

def probe_metamodel_edges() -> dict:
    """Report-only reconciliation of ``:MetaEdge`` rows against live edge types.

    Drift is surfaced BOTH directions, neither fails the audit:
    ``documented_but_absent`` is INFO not FAIL (the metamodel may lead the
    data — e.g. MAPS_TO_ASSET documented before the first equipment onboard);
    ``live_but_undocumented`` mirrors the fetch-side drift suffix the
    orchestrator sees (``metamodel.fetch_metamodel_text``), so the two views
    stay reconcilable. ``status='no_metamodel'`` flags a graph provisioned
    before the metamodel write existed.
    """
    doc_rows = neptune_query(CYPHER_META_EDGE_NAMES) or []
    documented = {n for n in (_row_get(r, "name", "me.name") for r in doc_rows) if n}
    live_rows = neptune_query(CYPHER_LIVE_EDGE_TYPES) or []
    live = {t for t in (_row_get(r, "rel", "type(r)") for r in live_rows) if t}
    return {
        "status": "checked" if documented else "no_metamodel",
        "info_only": True,
        "documented_count": len(documented),
        "live_edge_type_count": len(live),
        "documented_but_absent": sorted(documented - live),
        "live_but_undocumented": sorted(live - documented),
        "cypher_documented": CYPHER_META_EDGE_NAMES,
        "cypher_live": CYPHER_LIVE_EDGE_TYPES,
        "note": ("INFO only, never pass/fail: documented-but-absent means the "
                 "metamodel leads the data; live-but-undocumented means a writer "
                 "skipped the metamodel ownership rule (agents/metamodel.py)."),
    }


# ---------------------------------------------------------------------------
# Probe (report-only): IdentityRule edge name discrepancy
# ---------------------------------------------------------------------------

def probe_identity_rule_edge(system_id: str) -> dict:
    """Report-only probe of the suspected IdentityRule edge-name discrepancy.

    ``resolve.py`` reads ``IdentityRule-[:RESOLVES]->Concept`` while
    onboarding / server.py / self_learning.py write
    ``IdentityRule-[:LINKS_CONCEPT]->Concept``. This counts both edge forms so
    the findings doc can quantify the mismatch. It NEVER asserts — it is a
    finding, not a pass/fail.
    """
    resolves = _count(CYPHER_IDENTITY_RESOLVES)
    links_concept = _count(CYPHER_IDENTITY_LINKS_CONCEPT)
    return {
        "system_id": system_id,
        "resolves_edge_count": resolves,
        "links_concept_edge_count": links_concept,
        "cypher_resolves": CYPHER_IDENTITY_RESOLVES,
        "cypher_links_concept": CYPHER_IDENTITY_LINKS_CONCEPT,
        "note": (
            "resolve.py reads RESOLVES; onboard.py/server.py/self_learning.py write "
            "LINKS_CONCEPT. A nonzero LINKS_CONCEPT with zero RESOLVES indicates the "
            "resolver would not find identity rules — candidate ADR/CONTEXT finding."
        ),
    }


# ---------------------------------------------------------------------------
# Optional best-effort: read one real concept value end-to-end
# ---------------------------------------------------------------------------

def read_one_concept_value(system_id: str) -> dict | None:
    """Best-effort: read one real value for a mapped concept (S0.4 preview).

    Picks one field->concept mapping, resolves the system's Postgres schema from
    the catalog, and runs a read-only ``SELECT <column> FROM <table> LIMIT 1``.
    Fully wrapped in try/except — any failure records ``value: null`` plus a
    reason and NEVER fails the audit.
    """
    result: dict = {
        "system_id": system_id,
        "table": None,
        "column": None,
        "concept": None,
        "schema": None,
        "value": None,
        "reason": None,
    }
    try:
        rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": system_id}) or []
        if not rows:
            result["reason"] = "no field->concept mappings to read from"
            return result

        # Resolve the system's Postgres schema from the catalog entry.
        from agents.catalog import catalog as _catalog
        entry = _catalog.get(system_id)
        schema = (entry or {}).get("schema") if entry else None
        result["schema"] = schema
        if not schema:
            result["reason"] = f"no Postgres schema in catalog for system '{system_id}' (non-SQL system?)"
            return result

        # Pick the first row with a usable table+column.
        chosen = None
        for r in rows:
            tbl = _row_get(r, "table", "se.name")
            col = _row_get(r, "column", "f.name")
            concept = _row_get(r, "concept", "c.id")
            if tbl and col:
                chosen = (tbl, col, concept)
                break
        if not chosen:
            result["reason"] = "no row had both a table and a column name"
            return result

        tbl, col, concept = chosen
        result.update({"table": tbl, "column": col, "concept": concept})

        # Validate identifiers (regex-guarded) before composing SQL.
        safe_table = validate_identifier(tbl)
        safe_column = validate_identifier(col)
        sql = f"SELECT {safe_column} FROM {safe_table} LIMIT 1"  # nosec B608 - identifiers regex-validated; read-only connection

        raw = query_pg(schema, sql)
        parsed = json.loads(raw)
        data_rows = parsed.get("rows", [])
        result["value"] = data_rows[0][0] if data_rows and data_rows[0] else None
        if not data_rows:
            result["reason"] = "query returned no rows"
        return result
    except Exception as e:  # never fail the audit on the optional read
        result["reason"] = f"{type(e).__name__}: {e}"
        return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _audit_families(target: str) -> tuple[dict, bool]:
    """Run every fact family + probe for the chosen target system.

    Returns the report sections (insertion-ordered, mirroring the report
    layout) and the hard pass/fail verdict. Hard verdict = families (a)–(e);
    the metamodel/identity probes and the value read are report-only.
    """
    sections = {
        "field_concept": assert_field_concept_mappings(target),
        "authority": assert_authority(target),
        "joins": assert_joins(target),
        "i3x_property_layer": audit_property_layer(),
        "orphaned_properties": assert_no_orphaned_properties(),
        "equipment_vm_layer": audit_equipment_vm_layer(),
        "metamodel_edges": probe_metamodel_edges(),
        "identity_rule_edge_probe": probe_identity_rule_edge(target),
        "value_read": read_one_concept_value(target),
    }
    hard_pass = bool(
        sections["field_concept"]["pass"] and sections["authority"]["pass"]
        and sections["joins"]["pass"] and sections["i3x_property_layer"]["pass"]
        and sections["orphaned_properties"]["pass"]
        and sections["equipment_vm_layer"]["pass"]
    )
    return sections, hard_pass


def run_audit(system_id: str | None = None) -> dict:
    """Run the full read-only audit and return a single structured report.

    Pretty-prints the report as JSON to stdout and writes it to
    ``tests/audit/last-run-report.json``. When no graph is reachable / nothing
    is onboarded, returns a ``status='nothing_to_audit'`` report (the caller
    exits 0). Hard failure of (a)/(b)/(c)/(d)/(e) against a reachable graph is
    reflected in ``status='fail'``.
    """
    # Reachability + target selection in one shot: pick_target_system returns
    # None when no system anywhere has MAPS_TO edges (unreachable or empty).
    reachable_target = pick_target_system()
    target = system_id or reachable_target

    if reachable_target is None or target is None:
        report = {
            "status": "nothing_to_audit",
            "message": NOTHING_TO_AUDIT_MSG,
            "requested_system_id": system_id,
            "target_system": None,
            "graph_identifier": os.environ.get("GRAPH_IDENTIFIER"),
            "aws_region": os.environ.get("AWS_REGION"),
        }
        _emit(report)
        return report

    sections, hard_pass = _audit_families(target)
    report = {
        "status": "pass" if hard_pass else "fail",
        "target_system": target,
        "requested_system_id": system_id,
        "auto_picked": system_id is None,
        "graph_identifier": os.environ.get("GRAPH_IDENTIFIER"),
        "aws_region": os.environ.get("AWS_REGION"),
        **sections,
    }
    _emit(report)
    return report


def _emit(report: dict) -> None:
    """Pretty-print to stdout and persist the report next to this module."""
    text = json.dumps(report, indent=2, default=str)
    print(text)
    try:
        with open(REPORT_PATH, "w") as fh:
            fh.write(text + "\n")
    except OSError as e:
        print(f"warning: could not write report to {REPORT_PATH}: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_onboarding_graph",
        description="Read-only graph-shape audit of the onboarding pipeline (S0.1).",
    )
    parser.add_argument(
        "--system-id",
        dest="system_id",
        default=None,
        help="System id to audit (e.g. cmms). Default: auto-pick a system that has MAPS_TO edges.",
    )
    args = parser.parse_args(argv)

    report = run_audit(args.system_id)
    status = report.get("status")
    if status == "nothing_to_audit":
        # Clean, no-traceback exit when there is nothing to verify.
        print(f"\n{NOTHING_TO_AUDIT_MSG}", file=sys.stderr)
        return 0
    if status == "pass":
        return 0
    # Reachable graph but a (a)/(b)/(c)/(d) hard assertion failed.
    return 1


if __name__ == "__main__":
    sys.exit(main())
