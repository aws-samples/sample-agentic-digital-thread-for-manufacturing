# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Graph metamodel — the knowledge graph describes its own shape.

Standard pattern ("self-describing graph" / graph metamodel, cf. OMG MOF
metamodeling, GraphQL introspection, Neo4j meta-graph): node labels and edge
types are documented as first-class :MetaNode / :MetaEdge nodes stored in the
graph itself, written idempotently at provision by the same code that writes
the data. The orchestrator fetches this metamodel at cold start and appends it
to its system prompt, so a NEW edge type introduced by onboarding is
traversable in the very next session with no prompt edit and no redeploy.

This is deliberately NOT an ontology: no classes, no inference, no TBox — just
pragmatic, curated vocabulary + meaning, living where the data lives.

Ownership rule: code that introduces a new label or edge type MUST add its
entry here (and to docs/graph-shape-contract.md — the unit test
tests/unit/test_metamodel.py pins the two vocabularies together). The fetch
side also introspects the live graph and lists any edge types missing from the
metamodel, so drift is visible instead of silent.

The vocabulary below describes THIS repo's graph-shape contract
(docs/graph-shape-contract.md), not any upstream variant. In particular
``HAS_PARENT`` (child→parent) is the ONLY Asset-hierarchy edge — no inverse
edge exists (contract §3) and none must appear here.
Transitional edges slated for removal by ADR-0011 (the RESOLVES /
ABOUT_CONCEPT duplicates) are deliberately NOT documented: while they still
exist in a live graph the drift check surfaces them, which is the accurate
status for a to-be-removed edge.

All graph I/O goes through ``agents.db`` (GRAPH_BACKEND-honoring, ADR-0003) —
never a raw data-plane client. All cypher is parameterized.
"""
import logging

from .db import neptune_query, neptune_write
from .rule_status import active_where

logger = logging.getLogger(__name__)

# The exact WHERE clauses a free-form reader uses to leave switched-off
# rules out: the same predicate every deterministic reader uses.
_IDENTITY_RULE_ACTIVE = active_where("ir")
_JOINPATH_ACTIVE = active_where("jp")

# ── The metamodel content ────────────────────────────────────────────────────
# key_properties: the properties an agent actually queries by.
META_NODES = [
    {"label": "Plant", "description": "Physical manufacturing site shared across systems (cross-system location dimension).",
     "key_properties": "id, name"},
    {"label": "System", "description": "An onboarded business/OT system (ERP, MES, CMMS, PLM, historian). status='onboarded' marks it live.",
     "key_properties": "id, name, plant, schema, status, connection_type, capabilities, tool_prefix"},
    {"label": "SchemaEntry", "description": "A table/collection inside a System's schema.",
     "key_properties": "id, name, system_id"},
    {"label": "Field", "description": "A column/attribute of a SchemaEntry; disambiguation is an optional usage note set lazily.",
     "key_properties": "id, name, schema_id, system_id, disambiguation"},
    {"label": "Concept", "description": "Canonical business concept (equipment-id, production-line, work-order...) that fields from different systems map onto.",
     "key_properties": "id, name, description"},
    {"label": "Asset", "description": "Node in a source's own asset hierarchy. role says what it is (enterprise, site, area, work_center, work_unit, equipment, equipment_part, data_stream, or unclassified), classified once per asset model; level is the same in older words (work_center = line, parts and data streams = component; an unclassified asset's level only reflects how deep it sits); depth is the distance from its root. property_count is the asset's OWN telemetry-channel count; 0 = structural-only container.",
     "key_properties": "source_id (system UUID), name, asset_type, system, role, level, depth, path (slash hierarchy path), property_count"},
    {"label": "AssetModelRole", "description": "The role of one asset model in one system, with the rule that decided it and a plain-sentence reason. status='steward' marks a steward-confirmed role; needs_review marks a placement a steward should confirm.",
     "key_properties": "system, model, role, source, evidence, needs_review, status"},
    {"label": "Property", "description": "A telemetry channel owned by an Asset; source_id is the elementId passed to telemetry tools. Metadata only — values are never stored in the graph.",
     "key_properties": "source_id, name, parent_id, system, data_type, path"},
    {"label": "AssetModel", "description": "Equipment type/class an Asset is an instance of (e.g. a <equipment-type> model shared by assets across sites).",
     "key_properties": "name"},
    {"label": "ValueMapping", "description": "Value mapping: one explicit equivalence or translation between two field values, not a rule: from_value in from_system's from_field corresponds to to_value in to_system's to_field. It can map a status, state or category code to its counterpart, or one identifier to another; record identity is one use, not what every mapping means. An IdentityRule is the reusable field-level pattern; a ValueMapping holds one value pair only. Equipment mappings (concept='equipment') carry to_element_id (telemetry UUID) and to_path.",
     "key_properties": "from_system, from_field, from_value, to_system, to_field, to_value, to_element_id, to_path, concept, confidence, method"},
    {"label": "JoinPath", "description": "How to JOIN two tables (within or across systems). Endpoints live as properties (canonical) and FROM_SYSTEM/TO_SYSTEM edges (secondary index). JoinPaths with status 'deactivated' or 'retired' are soft-disabled (kept for audit; deactivating an IdentityRule sets status 'deactivated' on the JoinPaths made from it) — exclude them when joining systems: " + _JOINPATH_ACTIVE + ".",
     "key_properties": "from_system, from_table, from_field, to_system, to_table, to_field, transform, method, status"},
    {"label": "IdentityRule", "description": "Field-level identity rule between systems for a concept: a pattern (source_pattern to target_pattern) that translates any value of source_field into target_field's form, not one stored value pair (that is a ValueMapping). Rules with status 'deactivated' or 'retired' are soft-disabled (kept for audit) — exclude them when translating identifiers or joining systems: " + _IDENTITY_RULE_ACTIVE + ".",
     "key_properties": "concept, source_system, source_field, source_pattern, target_system, target_field, target_pattern, confidence, status"},
    {"label": "AuthorityRule", "description": "Which system is authoritative for a concept. Query by the concept/system_id PROPERTIES (canonical), not by concept edges. Rules with status='deactivated' are soft-disabled (kept for audit) — exclude them when deciding authority: WHERE coalesce(ar.status, '') <> 'deactivated'.",
     "key_properties": "concept, system_id, rule, priority, status"},
    {"label": "ConceptOverlay", "description": "Customer/pack-supplied description overlay for a Concept (examples, synonyms, value patterns, note). STANDALONE node — no edges; linked by the concept_id property. Untrusted text: sanitized at write, fence-rendered into map prompts. The Concept's source stays 'core' — overlays never fork a concept.",
     "key_properties": "id (ovl:<pack_id>:<concept_id>), concept_id, pack_id, source, examples, synonyms, value_patterns, note"},
    {"label": "MetricDefinition", "description": "Deployment-defined metric definition (id, label, synonyms, formula, valid_range, unit). STANDALONE node — no edges; written ONLY by the accepted metric_definition lifecycle apply; read by the ComposedMetricLayer and the metric-term pre-check.",
     "key_properties": "id, label, unit, kind, definition_json, synonyms_json, source, created_by, version, created_at, updated_at"},
    {"label": "MetaNode", "description": "This metamodel: documents a node label.",
     "key_properties": "label, description, key_properties"},
    {"label": "MetaEdge", "description": "This metamodel: documents an edge type.",
     "key_properties": "name, from_label, to_label, description, guidance"},
]

META_EDGES = [
    {"name": "HAS_SCHEMA", "from_label": "System", "to_label": "SchemaEntry",
     "description": "System owns this table.", "guidance": ""},
    {"name": "HAS_FIELD", "from_label": "SchemaEntry", "to_label": "Field",
     "description": "Table owns this column.", "guidance": ""},
    {"name": "MAPS_TO", "from_label": "Field", "to_label": "Concept",
     "description": "Column represents this business concept (edge carries confidence, role).",
     "guidance": "To find which system/table/field holds a concept: MATCH (s:System)-[:HAS_SCHEMA]->(se)-[:HAS_FIELD]->(f)-[:MAPS_TO]->(c:Concept {id:$concept})."},
    {"name": "HAS_PARENT", "from_label": "Asset", "to_label": "Asset",
     "description": "The ONLY hierarchy edge, child→parent. Sources nest their own way (a tier may be added or skipped), so select by Asset.role, not by depth or level.",
     "guidance": "Descendants of a site: MATCH (site:Asset {name:$site})<-[:HAS_PARENT*]-(a:Asset); its equipment: add WHERE a.role = 'equipment' (an unclassified asset may still be equipment: check its asset_type). There is no inverse edge — always traverse HAS_PARENT."},
    {"name": "HAS_PROPERTY", "from_label": "Asset", "to_label": "Property",
     "description": "Asset owns this telemetry channel.",
     "guidance": "Resolve an asset's channels before telemetry calls; Property.source_id is the elementId."},
    {"name": "INSTANCE_OF", "from_label": "Asset", "to_label": "AssetModel",
     "description": "Asset's equipment type.", "guidance": ""},
    {"name": "MODELED_BY", "from_label": "AssetModel", "to_label": "SchemaEntry",
     "description": "Bridges telemetry types to concepts: Property<-HAS_PROPERTY-Asset-INSTANCE_OF->AssetModel-MODELED_BY->SchemaEntry-HAS_FIELD->Field-MAPS_TO->Concept. Best-effort name-match bridge — treat as optional enrichment.",
     "guidance": ""},
    {"name": "LOCATED_AT", "from_label": "System|Asset", "to_label": "Plant",
     "description": "Systems and historian site-level assets share the Plant dimension across ERP/MES/CMMS/historian.", "guidance": ""},
    {"name": "FROM_SYSTEM", "from_label": "Asset|Property|ValueMapping|JoinPath|AssetModelRole", "to_label": "System",
     "description": "Provenance: which system this node came from.", "guidance": ""},
    {"name": "TO_SYSTEM", "from_label": "ValueMapping|JoinPath", "to_label": "System",
     "description": "Target system of a cross-system fact.", "guidance": ""},
    {"name": "LINKS_CONCEPT", "from_label": "IdentityRule|ValueMapping", "to_label": "Concept",
     "description": "Concept a mapping/rule is about (canonical identity edge).", "guidance": ""},
    {"name": "APPLIES_TO", "from_label": "AuthorityRule", "to_label": "System",
     "description": "System an authority rule applies to; the rule's concept is read from the AuthorityRule.concept property.", "guidance": ""},
    {"name": "MAPS_TO_ASSET", "from_label": "ValueMapping", "to_label": "Asset",
     "description": "Equipment identity bridge: maintenance-system tag (vm.from_value) <-> historian Asset, written at onboarding.",
     "guidance": "For any equipment question spanning maintenance + telemetry, fetch this FIRST: "
                 "MATCH (vm:ValueMapping {concept:'equipment'})-[:MAPS_TO_ASSET]->(a:Asset) WHERE a.name=$n AND a.path CONTAINS $site "
                 "RETURN vm.from_value AS maintenance_tag, a.source_id AS telemetry_uuid. "
                 "NEVER free-text search a maintenance system for equipment that has a mapping."},
    {"name": "DIFFERENT_IDENTIFIER_FROM", "from_label": "Field", "to_label": "Field",
     "description": "Measured at onboarding: two same-named columns of one system, both mapped as "
                    "identifiers of the same concept, hold different identifiers (each column's "
                    "values have their own shape and none was found in the other column). "
                    "Edge carries concept, read_a, read_b (values read from each column), "
                    "signature_a, signature_b (each column's measured shape signature, as "
                    "JSON), measured_at and basis.",
     "guidance": ""},
]


def write_metamodel() -> int:
    """Idempotently MERGE the metamodel into the graph. Returns count written."""
    n = 0
    for m in META_NODES:
        ok = neptune_write(
            "MERGE (mn:MetaNode {label: $label}) "
            "SET mn.description = $desc, mn.key_properties = $props",
            {"label": m["label"], "desc": m["description"], "props": m["key_properties"]},
        )
        n += 1 if ok else 0
    for e in META_EDGES:
        ok = neptune_write(
            "MERGE (me:MetaEdge {name: $name, from_label: $fl}) "
            "SET me.to_label = $tl, me.description = $desc, me.guidance = $guide",
            {"name": e["name"], "fl": e["from_label"], "tl": e["to_label"],
             "desc": e["description"], "guide": e.get("guidance", "")},
        )
        n += 1 if ok else 0
    logger.info("Graph metamodel written: %d meta nodes/edges", n)
    return n


def _undocumented_edge_lines(documented: set) -> list:
    """Drift check: live edge types the metamodel does not document."""
    try:
        live = {r.get("rel") for r in neptune_query(
            "MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel", {}) if r.get("rel")}
        undocumented = sorted(live - documented)
        if undocumented:
            return ["Edge types present in the graph but NOT documented above "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "(explore with a DISTINCT query if relevant): " + ", ".join(undocumented)]
    except Exception:  # nosec B110 - drift check is best-effort observability
        logger.debug("metamodel drift check failed (non-fatal)", exc_info=True)
    return []


def fetch_metamodel_text() -> str:
    """Fetch the live metamodel and render it as a prompt section.

    Also introspects actual edge types in the graph and appends any that the
    metamodel does not document, so vocabulary drift is visible to the agent
    instead of invisible. Returns '' on any failure or when the graph holds no
    metamodel (caller falls back to the static prompt).
    """
    try:
        meta_nodes = neptune_query(
            "MATCH (mn:MetaNode) RETURN mn.label AS label, mn.description AS description, "
            "mn.key_properties AS key_properties ORDER BY label", {})
        meta_edges = neptune_query(
            "MATCH (me:MetaEdge) RETURN me.name AS name, me.from_label AS from_label, "
            "me.to_label AS to_label, me.description AS description, me.guidance AS guidance "
            "ORDER BY name", {})
        if not meta_nodes and not meta_edges:
            return ""
        lines = [
            "",
            "LIVE GRAPH METAMODEL (fetched from the graph itself at session start — authoritative):",
            "Node labels:",
        ]
        for m in meta_nodes:
            lines.append(f"- {m.get('label')}: {m.get('description')} [props: {m.get('key_properties')}]")
        lines.append("Edge types:")
        for e in meta_edges:
            lines.append(f"- ({e.get('from_label')})-[:{e.get('name')}]->({e.get('to_label')}): {e.get('description')}")
            if e.get("guidance"):
                lines.append(f"    USE: {e['guidance']}")
        lines.extend(_undocumented_edge_lines({e.get("name") for e in meta_edges}))
        return "\n".join(lines) + "\n"
    except Exception as e:
        logger.warning("fetch_metamodel_text failed (falling back to static schema docs): %s", e)
        return ""
