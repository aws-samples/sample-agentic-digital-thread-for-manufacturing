# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""i3X onboarding graph provisioning + data-model discovery.

Two sibling capabilities for the ``SiteWise (i3X)`` connection type:

* :func:`provision_i3x_hierarchy` / :func:`bridge_i3x_schema` — the provision
  WALKER. Mirrors the i3X server's object hierarchy into Neptune (``:Asset`` /
  ``:Property`` / ``:AssetModel``) so the orchestrator resolves assets and
  telemetry channels by name/site without live API calls, then bridges the
  instance layer to the concept layer (``MODELED_BY`` / ``LOCATED_AT``). This
  is the i3X sibling of ``onboard._create_asset_hierarchy`` (the SiteWise
  Direct writer) — same ``:Asset`` node shape, plus the ``:Property`` layer.
  Metadata ONLY (ids/names/types/paths) — property VALUES are never stored
  (rules-engine constraint). Every failure is non-fatal (log + continue).

* :func:`discover_i3x_data_model` — data-model DISCOVERY. The i3X 1.0 spec
  describes an information-modeling surface, not a CRUD REST API; treating
  each ``/v1/...`` path as a "table" produces nonsense (one entry per
  /subscriptions/* admin endpoint with invented columns). The real queryable
  shape is Namespace / ObjectType / Object / property value, addressable by
  elementId, typeElementId, or hierarchy path in O(1) lookups. Discovery
  surfaces all three axes so the LLM mapping step can produce concept edges
  that exploit them: one table per real ObjectType (asset model), plus a
  Namespace table and a Hierarchy table.

Hierarchy edges follow the [F6] convention: ``HAS_PARENT`` (child→parent) is
the SINGLE canonical edge — the orchestrator traverses ``<-[:HAS_PARENT*]-``.
The redundant inverse ``HAS_CHILD`` is never written (S0.1 audit / ADR-0011).
All cypher is parameterized. The pure envelope/shape helpers live in
``i3x_envelope`` (shared with the system agent's i3X tools).
"""
from __future__ import annotations

import logging

from .db import neptune_query, neptune_write
from .i3x_envelope import (
    flatten_related_entries,
    is_i3x_scalar_type,
    is_property_child,
    make_i3x_client,
)

logger = logging.getLogger(__name__)

# ── Walker ──────────────────────────────────────────────────────────────────

# Structural ObjectTypes (Enterprise / Site / Area / Line) whose names are the
# authoritative hierarchy level. Level comes from the asset's TYPE, not tree
# depth — depth-based labelling breaks the moment a hierarchy skips or adds a
# tier (e.g. a site holding lines directly with no area tier), and it silently
# mislabels the site tier that the Plant linkage keys on.
_LEVEL_BY_TYPE = {"enterprise": "enterprise", "site": "site",
                  "area": "area", "line": "line"}

# Cycle guard only. i3X hierarchies terminate naturally (a leaf asset has only
# property children, so no further recursion happens). A generous cap prevents
# runaway on a malformed/cyclic graph without truncating deep-but-valid trees;
# a fixed shallow gate would silently drop the properties of deeper assets.
_WALK_DEPTH_CAP = 15


def _i3x_type_names(get) -> dict:
    """typeElementId -> displayName so Asset.asset_type is the human-readable
    model name, matching what the SiteWise (Direct) branch writes from its
    asset-model summaries. Built unfiltered so synthetic types still resolve
    if any asset references them. Non-fatal on failure (empty map)."""
    names: dict = {}
    try:
        for ot in (get("/objecttypes") or []):
            if not isinstance(ot, dict):
                continue
            tid = ot.get("elementId") or ot.get("typeElementId") or ot.get("id")
            if tid:
                names[tid] = ot.get("displayName") or tid
    except Exception as e:
        logger.debug(f"i3X /objecttypes lookup failed (non-fatal): {e}")
    return names


def _level_for(model_name: str, depth: int) -> str:
    """Hierarchy level from the asset's TYPE, depth fallback for the rest.

    Non-structural assets are descriptive only: equipment near the top of a
    line, component deeper down.
    """
    normalized = "".join(ch for ch in (model_name or "").lower() if ch.isalnum())
    if normalized in _LEVEL_BY_TYPE:
        return _LEVEL_BY_TYPE[normalized]
    return "equipment" if depth <= 3 else "component"


def _write_property_child(system_id: str, parent_eid: str, parent_path: str,
                          obj: dict) -> None:
    """MERGE a property child as ``:Property`` + HAS_PROPERTY + FROM_SYSTEM.

    The data type is the URN tail (DOUBLE / INTEGER / BOOLEAN / STRING /
    STRUCT). Metadata only — no values.
    """
    cid = obj.get("elementId")
    cname = obj.get("displayName") or cid
    ctype_id = obj.get("typeElementId") or ""
    dtype = ctype_id.split(":")[-1].upper() if ctype_id else ""
    neptune_write(
        "MERGE (p:Property {source_id: $sid}) "
        "SET p.name = $name, p.parent_id = $pid, "
        "    p.system = $sys, p.data_type = $dtype, "
        "    p.path = $path",
        {"sid": cid, "name": cname, "pid": parent_eid,
         "sys": system_id, "dtype": dtype,
         "path": f"{parent_path}/{cname}"}
    )
    neptune_write(
        "MATCH (a:Asset {source_id: $pid}), (p:Property {source_id: $sid}) "
        "MERGE (a)-[:HAS_PROPERTY]->(p)",
        {"pid": parent_eid, "sid": cid}
    )
    neptune_write(
        "MATCH (p:Property {source_id: $sid}), (s:System {id: $sys}) "
        "MERGE (p)-[:FROM_SYSTEM]->(s)",
        {"sid": cid, "sys": system_id}
    )


def _write_asset_child(system_id: str, parent_eid: str, obj: dict,
                       cmodel: str, level: str, cpath: str) -> None:
    """MERGE a composition child as ``:Asset`` (Direct-branch node shape) with
    the single canonical HAS_PARENT hierarchy edge ([F6]) + INSTANCE_OF +
    FROM_SYSTEM."""
    cid = obj.get("elementId")
    cname = obj.get("displayName") or cid
    neptune_write(
        "MERGE (a:Asset {source_id: $sid}) "
        "SET a.name = $name, a.asset_type = $atype, "
        "    a.system = $sys, a.level = $level, a.walker_level = $level, "
        "    a.path = $path",
        {"sid": cid, "name": cname, "atype": cmodel,
         "sys": system_id, "level": level, "path": cpath}
    )
    # [F6] HAS_PARENT (child→parent) is the SINGLE canonical hierarchy edge —
    # the orchestrator traverses <-[:HAS_PARENT*]- . Never write HAS_CHILD.
    neptune_write(
        "MATCH (c:Asset {source_id: $cid}), (p:Asset {source_id: $pid}) MERGE (c)-[:HAS_PARENT]->(p)",
        {"cid": cid, "pid": parent_eid}
    )
    neptune_write(
        "MERGE (m:AssetModel {name: $mname}) WITH m MATCH (a:Asset {source_id: $sid}) MERGE (a)-[:INSTANCE_OF]->(m)",
        {"mname": cmodel, "sid": cid}
    )
    neptune_write(
        "MATCH (a:Asset {source_id: $sid}), (s:System {id: $sys}) MERGE (a)-[:FROM_SYSTEM]->(s)",
        {"sid": cid, "sys": system_id}
    )


def _fetch_children(post, parent_eid: str) -> list:
    """Direct HasChildren entries for a parent, double-envelope flattened.

    i3X 1.0 mixes two kinds of children under HasChildren: composition assets
    (typeElementId is a real ObjectType — recurse into them) and property
    children (the telemetry holders — property-URN typeElementId). A
    composition asset CAN hold property children of its own in addition to
    composition children — a valid i3X shape (no "leaves only" assumption).
    Entries whose sourceRelationship is not HasChildren are dropped
    defensively (a server may return mixed directions despite the request
    param). Non-fatal: any failure yields [].
    """
    try:
        related = post("/objects/related", {
            "elementIds": [parent_eid],
            "relationshipType": "HasChildren",
        }) or []
    except Exception as e:
        logger.debug(f"i3X /objects/related HasChildren failed for {parent_eid}: {e}")
        return []
    entries = []
    for entry in flatten_related_entries(related):
        rel = entry.get("sourceRelationship") or "HasChildren"
        if rel != "HasChildren":
            continue
        obj = entry.get("object") if "object" in entry else entry
        if isinstance(obj, dict) and obj.get("elementId"):
            entries.append(obj)
    return entries


def _i3x_walk(post, system_id: str, type_names: dict, parent_eid: str,
              parent_path: str, depth: int) -> int:
    """Recursively mirror a subtree; return the parent's own property count.

    Every asset (including structural-only ones) is stamped with
    ``property_count`` so the orchestrator can tell whether an asset holds its
    own measurements without traversing HAS_PROPERTY edges. Underscore-prefixed
    children (the server's name-path/asset-path STRING metadata) are skipped —
    server plumbing, not user-visible telemetry.
    """
    if depth > _WALK_DEPTH_CAP:
        logger.warning(f"i3X walk hit depth cap ({_WALK_DEPTH_CAP}) at {parent_eid} — possible cycle; stopping descent")
        return 0
    parent_property_count = 0
    for obj in _fetch_children(post, parent_eid):
        cname = obj.get("displayName") or obj.get("elementId")
        if cname.startswith("_"):
            continue
        if is_property_child(obj):
            _write_property_child(system_id, parent_eid, parent_path, obj)
            parent_property_count += 1
            continue
        ctype_id = obj.get("typeElementId") or ""
        cmodel = type_names.get(ctype_id, ctype_id) or "Unknown"
        cpath = f"{parent_path}/{cname}"
        _write_asset_child(system_id, parent_eid, obj, cmodel,
                           _level_for(cmodel, depth), cpath)
        # Always descend — the tree terminates naturally at leaves. Stamp the
        # child with its own count (an asset that holds telemetry directly AND
        # has composition children gets the right property_count).
        child_property_count = _i3x_walk(post, system_id, type_names,
                                         obj["elementId"], cpath, depth + 1)
        neptune_write(
            "MATCH (a:Asset {source_id: $sid}) SET a.property_count = $n",
            {"sid": obj["elementId"], "n": child_property_count}
        )
    return parent_property_count


def _write_root(get, post, system_id: str, type_names: dict, root: dict) -> None:
    """MERGE one hierarchy root (level 'enterprise') and walk its subtree."""
    reid = root.get("elementId")
    rname = root.get("displayName") or reid
    rtype_id = root.get("typeElementId") or ""
    rmodel = type_names.get(rtype_id, rtype_id) or "Unknown"
    neptune_write(
        "MERGE (a:Asset {source_id: $sid}) "
        "SET a.name = $name, a.asset_type = $atype, "
        "    a.system = $sys, a.level = 'enterprise', a.walker_level = 'enterprise', "
        "    a.path = $path",
        {"sid": reid, "name": rname, "atype": rmodel, "sys": system_id, "path": rname}
    )
    neptune_write(
        "MERGE (m:AssetModel {name: $mname}) WITH m MATCH (a:Asset {source_id: $sid}) MERGE (a)-[:INSTANCE_OF]->(m)",
        {"mname": rmodel, "sid": reid}
    )
    neptune_write(
        "MATCH (a:Asset {source_id: $sid}), (s:System {id: $sys}) MERGE (a)-[:FROM_SYSTEM]->(s)",
        {"sid": reid, "sys": system_id}
    )
    # Walk the subtree and stamp the root with its own property_count (root
    # assets can hold telemetry too).
    root_property_count = _i3x_walk(post, system_id, type_names, reid, rname, depth=0)
    neptune_write(
        "MATCH (a:Asset {source_id: $sid}) SET a.property_count = $n",
        {"sid": reid, "n": root_property_count}
    )


def _i3x_walk_counters(system_id: str) -> dict:
    """Post-walk graph counts — the honest signal for what the walk produced."""
    try:
        assets = (neptune_query(
            "MATCH (a:Asset {system: $sid}) RETURN count(a) AS n", {"sid": system_id},
        ) or [{}])[0].get("n", 0)
        properties = (neptune_query(
            "MATCH (p:Property {system: $sid}) RETURN count(p) AS n", {"sid": system_id},
        ) or [{}])[0].get("n", 0)
        return {"assets": int(assets or 0), "properties": int(properties or 0)}
    except Exception as e:
        logger.warning(f"i3X walk counter query failed (non-fatal): {e}")
        return {"assets": -1, "properties": -1}


def _walk_i3x_roots(get, post, system_id: str, type_names, result: dict) -> None:
    """Fetch the root objects and walk each; counts into ``result``."""
    try:
        roots = get("/objects", root="true") or []
    except Exception as e:
        from .public_errors import public_error_text
        logger.warning("i3X /objects?root=true failed (non-fatal): %s", type(e).__name__)
        result["warnings"].append("/objects?root=true failed: " + public_error_text(
            e, "i3x.walk.roots", default="not_reachable"))
        roots = []
    if isinstance(roots, list):
        for root in roots:
            if isinstance(root, dict) and root.get("elementId"):
                _write_root(get, post, system_id, type_names, root)
                result["roots"] += 1


def provision_i3x_hierarchy(system_id: str, system_name: str, base_url: str,  # nosec B107 - empty default sentinel; real value supplied by caller
                            token: str = "", auth_scheme: str = "bearer") -> dict:
    """Mirror the i3X hierarchy into Neptune. NEVER raises (log + continue):
    a broken walk degrades name resolution, never the onboarding.

    Returns a RESULT DICT — ``{"roots", "assets", "properties", "warnings"}``
    — so callers can tell a zero-asset walk (auth/TLS failure, empty server)
    from success instead of both looking identical. Failures are
    still non-fatal, but they are COUNTED and NAMED in ``warnings``.
    """
    result: dict = {"roots": 0, "assets": 0, "properties": 0, "warnings": []}
    try:
        get, post = make_i3x_client(base_url, token, auth_scheme)
        type_names = _i3x_type_names(get)
        # Create the System node early — Asset FROM_SYSTEM edges need it. The
        # provision graph write MERGEs it again with the full property set;
        # this MERGE is a strict subset and is overwritten.
        neptune_write(
            "MERGE (s:System {id: $sid}) SET s.name = $name, s.status = 'onboarded', s.connection_type = $ctype",
            {"sid": system_id, "name": system_name, "ctype": "SiteWise (i3X)"}
        )
        _walk_i3x_roots(get, post, system_id, type_names, result)
        result.update(_i3x_walk_counters(system_id))
        if result["assets"] == 0:
            result["warnings"].append("walk wrote ZERO Asset nodes")
            logger.warning(
                "i3X hierarchy walk for %s wrote ZERO assets (%d root(s)) — "
                "check auth/TLS/base_url; telemetry name resolution will be "
                "degraded", system_id, result["roots"])
        else:
            logger.info(
                "i3X asset hierarchy written for %s: %d root(s), %d asset(s), "
                "%d propert(ies)", system_id, result["roots"],
                result["assets"], result["properties"])
    except Exception as e:
        from .public_errors import public_error_text
        logger.warning("i3X asset hierarchy creation failed (non-fatal): %s", type(e).__name__)
        result["warnings"].append("walk failed: " + public_error_text(e, "i3x.walk"))
    return result


def bridge_i3x_schema(system_id: str) -> None:
    """i3X post-schema bridge — run AFTER the SchemaEntry/Field graph write.

    The walker created the Asset/Property/AssetModel instance layer and the
    provision graph write created SchemaEntry/Field/MAPS_TO. Only once BOTH
    layers exist can they be bridged:

    1. ``AssetModel-[:MODELED_BY]->SchemaEntry`` (normalized-name match) so
       telemetry Properties inherit concepts: Property<-HAS_PROPERTY-Asset-
       INSTANCE_OF->AssetModel-MODELED_BY->SchemaEntry-HAS_FIELD->Field-
       MAPS_TO->Concept.
    2. site-level ``Asset-[:LOCATED_AT]->Plant`` so i3X telemetry shares the
       plant dimension with the relational systems for cross-system
       correlation.

    Bridging earlier (inside the walker) would match zero on a clean onboard —
    the SchemaEntries don't exist yet. Non-fatal: a failed bridge degrades
    concept inheritance, never the onboarding.
    """
    try:
        neptune_write(
            "MATCH (m:AssetModel)<-[:INSTANCE_OF]-(:Asset {system: $sys}) WITH DISTINCT m "
            "MATCH (:System {id: $sys})-[:HAS_SCHEMA]->(se:SchemaEntry) "
            "WHERE toLower(replace(replace(replace(m.name,'-',''),'_',''),' ','')) "
            "    = toLower(replace(replace(replace(se.name,'-',''),'_',''),' ','')) "
            "MERGE (m)-[:MODELED_BY]->(se)",
            {"sys": system_id}
        )
        logger.info(f"i3X AssetModel->SchemaEntry bridge written for {system_id}")
    except Exception as e:
        logger.warning(f"i3X AssetModel->SchemaEntry bridge failed (non-fatal): {e}")
    try:
        neptune_write(
            "MATCH (a:Asset {system: $sys}) WHERE a.level = 'site' "
            "MATCH (pl:Plant) WHERE toLower(pl.name) <> 'all sites' AND ("
            "     toLower(replace(a.name,' ','')) CONTAINS toLower(replace(pl.name,' ','')) "
            "  OR toLower(replace(pl.name,' ','')) CONTAINS toLower(replace(a.name,' ','')) ) "
            "MERGE (a)-[:LOCATED_AT]->(pl)",
            {"sys": system_id}
        )
        logger.info(f"i3X site->Plant linkage written for {system_id}")
    except Exception as e:
        logger.warning(f"i3X site->Plant linkage failed (non-fatal): {e}")


# ── Data-model discovery ────────────────────────────────────────────────────

# ISA-95 location containers only — structural/hierarchy container types are
# not equipment; their columns are location metadata, not telemetry. Skipping
# them keeps junk SchemaEntry ('enterprise'/'site'/...) out of the concept
# graph (the walker captures the hierarchy as Asset instances, and the
# synthetic Namespace/Hierarchy tables cover location concepts). NOTE: do NOT
# add 'level' or 'plant' — a server may expose real measurement assets whose
# MODEL NAME collides with those words (e.g. a tank fill-level model with
# dozens of telemetry channels); excluding them by name orphans their
# properties from the concept graph. 'root'/'namespace'/'hierarchy' are i3X
# synthetic/plumbing types safe to skip.
_STRUCTURAL_TYPE_NAMES = {"enterprise", "site", "area", "line", "namespace",
                          "hierarchy", "root"}

# i3X core identifier columns every per-type table inherits, so the LLM
# produces consistent equipment-id mappings across types — this is what
# enables cross-system join inference (e.g. <mes-system>.work_orders.asset_id
# ↔ <asset-model-name>.elementId).
_IDENTIFIER_COLUMN_NAMES = {"elementId", "displayName", "typeElementId",
                            "namespaceUri", "parentElementId", "path"}


def _namespaces(get) -> list[dict]:
    """Registered top-level scopes (``/v1/namespaces``). The SiteWise default is
    urn:aws:iotsitewise; deployments bridging multiple sources surface several.
    Non-fatal on failure (empty list)."""
    try:
        ns_resp = get("/namespaces")
        if isinstance(ns_resp, list):
            return [n for n in ns_resp if isinstance(n, dict)]
    except Exception as e:
        logger.warning(f"i3X /namespaces discovery failed: {e}")
    return []


def _fetch_real_types(get, namespaces: list[dict]) -> list[dict]:
    """Real (non-scalar) ObjectTypes, pulled per-namespace so the mapper sees
    which namespace each model belongs to (namespaceUri is the canonical
    filter — matches how the i3X system-agent tools query in production).
    Falls back to the unfiltered type list when /namespaces is unimplemented.
    """
    seen: set = set()
    real_types: list[dict] = []

    def _collect(types, ns_uri=None):
        if not isinstance(types, list):
            return
        for t in types:
            if not isinstance(t, dict):
                continue
            tid = t.get("elementId") or t.get("typeElementId") or t.get("id")
            if not tid or is_i3x_scalar_type(tid) or tid in seen:
                continue
            seen.add(tid)
            if ns_uri:
                # Stamp the originating namespace for the per-type table's
                # namespaceUri column metadata.
                t.setdefault("_namespaceUri", ns_uri)
            real_types.append(t)

    if namespaces:
        for ns in namespaces:
            ns_uri = ns.get("uri") or ns.get("namespaceUri")
            if not ns_uri:
                continue
            try:
                _collect(get("/objecttypes", namespaceUri=ns_uri), ns_uri)
            except Exception as e:
                logger.warning(f"i3X /objecttypes?namespaceUri={ns_uri} failed: {e}")
    else:
        try:
            _collect(get("/objecttypes"))
        except Exception as e:
            logger.warning(f"i3X /objecttypes (no-namespace) failed: {e}")
    return real_types


def _identifier_columns(type_id: str, ns_uri: str) -> list[dict]:
    """The i3X core identifier columns for a per-type table."""
    return [
        {'name': 'elementId',        'data_type': 'string (unique i3X object id — direct lookup key)'},
        {'name': 'displayName',      'data_type': 'string (instance label, e.g. "<asset-name>")'},
        {'name': 'typeElementId',    'data_type': f"string (asset model = '{type_id}')"},
        {'name': 'namespaceUri',     'data_type': f"string (origin namespace = '{ns_uri}')" if ns_uri else 'string (origin namespace)'},
        {'name': 'parentElementId',  'data_type': 'string (immediate parent in the asset hierarchy)'},
        {'name': 'path',             'data_type': 'string (full hierarchy path, slash-separated displayNames)'},
    ]


def _schema_property_columns(ot: dict) -> list[dict]:
    """Structural properties from the type's JSON Schema, unitOfMeasure-aware.

    The i3X spec uses ``schema`` (CESMII naming) — older builds may emit
    ``jsonSchema`` instead, so both are accepted. A declared unit lets the
    mapper distinguish a measure from a correlate; the description hint is
    truncated to keep the prompt manageable.
    """
    schema_obj = ot.get("schema") or ot.get("jsonSchema") or {}
    props = schema_obj.get("properties") if isinstance(schema_obj, dict) else {}
    if not isinstance(props, dict):
        props = {}
    columns = []
    for prop_name, prop_def in props.items():
        if prop_name in _IDENTIFIER_COLUMN_NAMES:
            continue
        if not isinstance(prop_def, dict):
            prop_def = {}
        ptype = prop_def.get("type") or "string"
        unit = prop_def.get("unit") or prop_def.get("unitOfMeasure") or ""
        description = prop_def.get("description") or ""
        data_type = f"i3x_property_{ptype}"
        if unit:
            data_type += f" ({unit})"
        if description:
            hint = description.strip().split("\n")[0][:120]
            data_type += f" — {hint}"
        columns.append({"name": prop_name, "data_type": data_type})
    return columns


def _live_channel_columns(post, instance_id: str, existing: set) -> list[dict]:
    """Live telemetry channels sampled from one instance's property children.

    Many i3X types declare no measurement properties in their JSON Schema —
    the real channels exist only as property children on each instance.
    Without them the type table has no measures, the mapper produces no
    concept-mapped Fields, and every instance Property of that model stays
    concept-unmapped (unreachable by semantic/cross-system queries). This is
    what lets the MODELED_BY bridge give ALL instance properties a concept.
    One extra POST per type; failures are non-fatal (schema columns remain).
    """
    columns: list[dict] = []
    try:
        related = post("/objects/related", {
            "elementIds": [instance_id],
            "relationshipType": "HasChildren",
        }) or []
        for entry in flatten_related_entries(related):
            obj = entry.get("object") if "object" in entry else entry
            if not isinstance(obj, dict):
                continue
            cname = obj.get("displayName") or ""
            ctype = obj.get("typeElementId") or ""
            # Skip server metadata (underscore-prefixed).
            if not cname or cname.startswith("_") or cname in existing:
                continue
            # Spec-correct asset-vs-measurement test: measurements are
            # isComposition=false. Fall back to the property-URN shape only
            # when the flag is absent. DELIBERATE divergence from the
            # walker's is_property_child (URN-prefix-only fallback): via
            # is_i3x_scalar_type discovery also accepts the BARE scalar form
            # ("DOUBLE") some servers emit — a missed channel here silently
            # drops a schema column, while the walker's stricter test guards
            # graph writes and must not over-classify assets as properties.
            ic = obj.get("isComposition")
            if isinstance(ic, bool):
                if ic is True:
                    continue  # composition asset, not a measurement
            elif not is_i3x_scalar_type(ctype):
                continue
            dtype = ctype.split(":")[-1].upper() if ":" in ctype else (ctype.upper() or "DOUBLE")
            columns.append({"name": cname,
                            "data_type": f"i3x_property_{dtype} (live telemetry channel)"})
            existing.add(cname)
    except Exception as e:
        logger.debug(f"i3X instance-children enrichment failed for {instance_id}: {e}")
    return columns


def _object_type_table(get, post, ot: dict) -> dict | None:
    """One table for a real ObjectType (None for structural containers)."""
    type_id = ot.get("elementId") or ot.get("typeElementId") or ot.get("id") or ""
    display = ot.get("displayName") or type_id
    ns_uri = ot.get("_namespaceUri") or ot.get("namespaceUri") or ""
    if "".join(ch for ch in display.lower() if ch.isalnum()) in _STRUCTURAL_TYPE_NAMES:
        return None
    columns = _identifier_columns(type_id, ns_uri) + _schema_property_columns(ot)
    # Sample-count instances of this type. Cheap because i3X is indexed by
    # typeElementId; only a representative count is needed.
    row_count = 0
    sample = None
    try:
        sample = get("/objects", typeElementId=type_id)
        if isinstance(sample, list):
            row_count = len(sample)
    except Exception as e:
        logger.debug(f"i3X /objects?typeElementId={type_id} sample failed: {e}")
    if isinstance(sample, list) and sample and isinstance(sample[0], dict):
        instance_id = sample[0].get("elementId")
        if instance_id:
            existing = {c["name"] for c in columns}
            columns += _live_channel_columns(post, instance_id, existing)
    return {"name": display, "columns": columns, "row_count": row_count}


def _hierarchy_table(get) -> dict:
    """Virtual table for the parent/child edge set so location-routed queries
    ("everything under <site>", "all assets on <line>") can map to a Hierarchy
    concept and be planned with i3X's O(1) path-prefix lookups instead of
    recursive joins."""
    root_count = 0
    try:
        roots = get("/objects", root="true")
        if isinstance(roots, list):
            root_count = len(roots)
    except Exception as e:
        logger.debug(f"i3X /objects?root=true sample failed: {e}")
    return {
        "name": "Hierarchy",
        "columns": [
            {'name': 'rootElementId',    'data_type': 'string (top-of-tree object, e.g. enterprise or site)'},
            {'name': 'parentElementId',  'data_type': 'string (immediate parent — HasParent edge target)'},
            {'name': 'childElementId',   'data_type': 'string (immediate child — HasChildren edge target)'},
            {'name': 'path',             'data_type': 'string (slash-separated displayNames from root)'},
            {'name': 'depth',            'data_type': 'integer (level from root, 0 = enterprise)'},
            {'name': 'relationshipType', 'data_type': 'string (HasParent | HasChildren | composition)'},
        ],
        "row_count": root_count,
    }


def _fallback_tables() -> list[dict]:
    """Minimal abstract Asset/AssetModel/Property schema for servers that
    haven't populated their asset catalog yet or don't implement /namespaces
    and /objecttypes (a non-conformant 1.0 implementation). Still gives the
    LLM enough columns to produce concept mappings."""
    return [
        {"name": "Asset",
         "columns": [
             {'name': 'elementId',        'data_type': 'string (unique i3X object id)'},
             {'name': 'displayName',      'data_type': 'string (asset name)'},
             {'name': 'typeElementId',    'data_type': 'string (asset model id)'},
             {'name': 'namespaceUri',     'data_type': 'string (origin namespace)'},
             {'name': 'parentElementId',  'data_type': 'string (parent in hierarchy)'},
             {'name': 'path',             'data_type': 'string (hierarchy path)'},
         ],
         "row_count": 0},
        {"name": "AssetModel",
         "columns": [
             {'name': 'typeElementId', 'data_type': 'string (model id)'},
             {'name': 'displayName',   'data_type': 'string (model name)'},
             {'name': 'namespaceUri',  'data_type': 'string (origin namespace)'},
         ],
         "row_count": 0},
        {"name": "Property",
         "columns": [
             {'name': 'elementId', 'data_type': 'string (object_id.property_name)'},
             {'name': 'value',     'data_type': 'number | string | boolean'},
             {'name': 'timestamp', 'data_type': 'rfc3339 datetime (sample time)'},
             {'name': 'quality',   'data_type': 'enum (Good | Bad | Uncertain | GoodNoData)'},
             {'name': 'unit',      'data_type': 'string (unit of measure)'},
         ],
         "row_count": 0},
    ]


def discover_i3x_data_model(entry: dict) -> dict:
    """Query a live i3X 1.0 server and surface its data model as tables.

    Reads the connection from the passed dict's ``connection_config``
    (base_url/token/auth_scheme). Callers supply a dict whose connection was
    acquired via ``resolve_connection`` plus the submitted body input, NOT a
    persisted catalog connection snapshot (ADR 0031). Honors
    ``I3X_TLS_VERIFY`` (via the shared client). Unreachable/empty servers fall
    back to the minimal abstract schema — never raises.
    """
    cc = entry.get("connection_config") or {}
    base_url = (cc.get("base_url") or "").rstrip("/")
    if not base_url:
        return {"error": "i3X server base_url not configured (connection_config.base_url)"}
    get, post = make_i3x_client(base_url, cc.get("token") or "",
                                cc.get("auth_scheme") or "bearer")

    tables: list[dict] = []
    namespaces = _namespaces(get)
    if namespaces:
        tables.append({
            "name": "Namespace",
            "columns": [
                {'name': 'uri',         'data_type': 'string (i3X namespace URI, e.g. urn:aws:iotsitewise)'},
                {'name': 'displayName', 'data_type': 'string (human-readable namespace label)'},
            ],
            "row_count": len(namespaces),
        })
    for ot in _fetch_real_types(get, namespaces):
        table = _object_type_table(get, post, ot)
        if table:
            tables.append(table)
    tables.append(_hierarchy_table(get))

    # Nothing useful beyond the Hierarchy entry → abstract fallback so the
    # mapping step still has something to work with.
    if len(tables) <= 1:
        logger.warning(
            "i3X discovery surfaced no ObjectTypes from %s; "
            "falling back to abstract Asset/AssetModel/Property schema", base_url,
        )
        return {"tables": _fallback_tables()}
    return {"tables": tables}
