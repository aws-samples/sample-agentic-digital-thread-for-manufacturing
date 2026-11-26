# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two-layer concept tagging (S5.2 / ADR 0021).

Extracted from ``catalog.py`` so that foundational module stays under its size cap.
This module owns the static tagging vocabulary and the pure helpers that resolve a
table to its entity concept.

Import discipline (ADR 0020/0021): this module is SELF-CONTAINED and import-pure — it
holds the entity-id set and every static map/helper directly, has NO module-level import
of ``catalog`` (which would create a circular import, since ``catalog`` re-exports these
names) and needs no runtime deps (pg8000/boto3). ``ENTITY_CONCEPT_IDS`` and
``entity_for_table`` are fully resolvable here.

``CONCEPT_LAYER`` / ``FIELD_CONCEPT_IDS`` are intentionally NOT here: they are derived
over ``CORE_CONCEPT_DEFINITIONS`` (which, with its import-time validator, lives in
``catalog``) and so are built in ``catalog`` from the entity-id set this module owns.
"""
from __future__ import annotations

# A concept's layer is its CANONICAL RENDERING role, NOT a mapping constraint: 'entity' (record
# type → table/SchemaEntry) or 'field' (a column's meaning). The curated entity set; all else field.
_ENTITY_CONCEPTS = frozenset({
    "asset", "production-run", "production-order", "batch", "inspection",
    "specification", "non-conformance", "maintenance-event", "maintenance-schedule",
    "downtime", "alarm", "spare-part", "material", "inventory-transaction",
    "material-consumption", "bill-of-materials", "product", "engineering-change",
    "purchase-order", "sales-order", "customer", "supplier", "invoice", "payment",
    "shipment", "personnel", "work-order",
})

# Dimension dual-role (ADR 0021 / Locked Wave-0 decision #6): production-line, location, site
# render as FIELDS (canonical dimension columns) but ALSO anchor the base tables (production_lines,
# inventory_locations) — so they are entity-CAPABLE. Layer tag governs rendering; this set
# governs legal table→entity targets. Hence ENTITY_CONCEPT_IDS = entity-tagged ∪ these 3.
_DIMENSION_ENTITY_IDS = frozenset({"production-line", "location", "site"})
ENTITY_CONCEPT_IDS: frozenset[str] = _ENTITY_CONCEPTS | _DIMENSION_ENTITY_IDS

# Generic/audit columns that must NOT mint a new field concept (Locked Wave-0 decision #5).
GENERIC_FIELD_NAMES: frozenset[str] = frozenset({
    "created_at", "updated_at", "notes", "is_active", "entity_type",
    "source_system", "unit_of_measure",
})


def is_generic_field(column_name: str) -> bool:
    """do-not-MINT predicate (pure, case-insensitive): a generic/audit name or a surrogate
    ``*_id``/``id`` key. Does NOT gate MAPPING to an existing concept (ADR 0021) — only minting."""
    name = column_name.strip().lower()
    return name in GENERIC_FIELD_NAMES or name == "id" or name.endswith("_id")


# Curated table→entity map (S5.2 Wave 0, from live `discover`). Logical spec: system →
# {bare_table: entity}; line-item/base tables fold to their parent entity.
_TABLE_ENTITY_BY_SYSTEM: dict[str, dict[str, str]] = {
    "cmms": {
        "assets": "asset", "work_orders": "work-order", "work_orders_raw": "work-order",
        "maintenance_schedules": "maintenance-schedule", "maintenance_schedules_raw": "maintenance-schedule", "spare_parts": "spare-part",
    },
    "mes": {  # shared table names across all three MES schemas
        "work_orders": "work-order", "production_events": "production-run", "quality_checks": "inspection",
        "downtime_events": "downtime", "material_consumption": "material-consumption", "production_lines": "production-line",
        "line_status": "production-line",  # per-line current status (machine-state/runtime-hours/line-speed/throughput)
        "production_runs": "production-run",  # the ERP production order a site's work orders execute (site view)
        "equipment": "asset",  # the site's equipment registry: one row per line's work center (site view)
    },
    "plm": {"products": "product", "bom_items": "bill-of-materials", "specifications": "specification"},
    "erp": {
        "customers": "customer", "suppliers": "supplier", "products": "product", "customer_orders": "sales-order", "order_line_items": "sales-order",
        "purchase_orders": "purchase-order", "po_line_items": "purchase-order", "po_receipts": "purchase-order", "invoices": "invoice", "invoice_line_items": "invoice",
        "payments": "payment", "inventory_items": "material", "inventory_transactions": "inventory-transaction", "inventory_transfers": "inventory-transaction",
        "lot_warehouse_movements": "inventory-transaction", "inventory_balances": "inventory-transaction", "material_lots": "batch", "lot_production_usage": "batch",
        "lot_mes_tracking": "batch", "bill_of_materials": "bill-of-materials", "shipments": "shipment", "shipment_items": "shipment",
        "inventory_locations": "location", "item_suppliers": "supplier",
    },
}

# Static (schema, system_type, id) of the curated SQL systems — kept here so TABLE_ENTITY_MAP is
# built WITHOUT importing the live ``catalog`` instance (which would be a circular import). These
# mirror the curated SQL demo schemas exactly; non-SQL systems (sitewise/highbyte, empty schema) and
# the cmms2 synonym schema contribute nothing, so the result is byte-identical to building from
# ``catalog.get_all()``.
_ENTITY_LAYER_SYSTEMS: tuple[dict[str, str], ...] = (
    {"id": "erp", "system_type": "ERP", "schema": "erp_data"},
    {"id": "mes-mil", "system_type": "MES", "schema": "mes_milwaukee"},
    {"id": "mes-det", "system_type": "MES", "schema": "mes_detroit"},
    {"id": "mes-cr", "system_type": "MES", "schema": "mes_cedarrapids"},
    {"id": "cmms", "system_type": "CMMS", "schema": "cmms_maintenance"},
    {"id": "cmms2", "system_type": "CMMS", "schema": "cmms2_maintenance"},
    {"id": "plm", "system_type": "PLM", "schema": "plm_engineering"},
)


def _build_table_entity_map(systems) -> dict[tuple[str, str], str]:
    """Expand _TABLE_ENTITY_BY_SYSTEM into {(schema, bare_table): entity} (pure, no DB). MES
    systems share table names (keyed "mes"); cmms/plm/erp key off system id, so the cmms2
    synonym schema maps to nothing."""
    return {
        (s["schema"], t): e
        for s in systems if s.get("schema")
        for t, e in _TABLE_ENTITY_BY_SYSTEM.get(
            "mes" if s.get("system_type") == "MES" else s["id"], {}).items()
    }


TABLE_ENTITY_MAP: dict[tuple[str, str], str] = _build_table_entity_map(_ENTITY_LAYER_SYSTEMS)

# Table-level exclude-set (S5.2 Wave 0): a non-domain table maps to NO entity — an explicit bare
# name plus an infra/config prefix/suffix rule. Distinct from GENERIC_FIELD_NAMES (column-level).
TABLE_EXCLUDE: frozenset[str] = frozenset({"product_site_routing"})
_EXCLUDE_PREFIXES: tuple[str, ...] = ("simulation_",)
_EXCLUDE_SUFFIXES: tuple[str, ...] = ("_config",)


def is_excluded_table(table_name: str) -> bool:
    """True if a table is excluded from the entity layer: an explicit name, an infra prefix
    (simulation_*), or a config suffix (*_config). Pure; case-insensitive; strips any schema."""
    bare = table_name.split(".")[-1].strip().lower()
    return (bare in TABLE_EXCLUDE
            or any(bare.startswith(p) for p in _EXCLUDE_PREFIXES)
            or any(bare.endswith(s) for s in _EXCLUDE_SUFFIXES))


# Curated schemas the table→entity layer covers — derived from TABLE_ENTITY_MAP's keys. The
# entity edge (and its fail-loud totality) applies ONLY to these. Synonym schemas (cmms2's
# cmms2_maintenance, which deliberately maps to nothing) and non-SQL systems ('mcp'/'rest'/
# 'sitewise' / empty schema) fall outside the curated vocabulary and get NO entity edge.
ENTITY_MAPPED_SCHEMAS: frozenset[str] = frozenset(schema for (schema, _t) in TABLE_ENTITY_MAP)


def entity_for_table(schema: str, table: str) -> str | None:
    """Resolve ``(schema, table)`` → entity concept id for the SchemaEntry-[:MAPS_TO]->Concept
    edge (S5.2 Wave 3.1, ADR 0021). Pure: a dict lookup over the import-time TABLE_ENTITY_MAP;
    no DB / graph / LLM.

    Fail-loud totality over CURATED domain schemas:
      * returns an entity concept id (guaranteed ∈ ENTITY_CONCEPT_IDS) when the table is mapped;
      * returns ``None`` when the table is excluded (``is_excluded_table``) — caller writes no
        edge and skips the table cleanly;
      * returns ``None`` when ``schema`` is outside the curated entity vocabulary
        (ENTITY_MAPPED_SCHEMAS) — the entity layer simply does not apply there;
      * RAISES ``ValueError`` when a table in a CURATED schema is neither mapped nor excluded
        (a vocabulary gap — never silently skipped), or resolves to a non-entity concept
        (no silent fallback).
    """
    bare = table.split(".")[-1].strip()
    if is_excluded_table(bare):
        return None
    if schema not in ENTITY_MAPPED_SCHEMAS:
        return None
    entity = TABLE_ENTITY_MAP.get((schema, bare))
    if entity is None:
        raise ValueError(
            f"No table→entity mapping for domain table '{schema}.{bare}'. Add it to "
            f"concept_tagging._TABLE_ENTITY_BY_SYSTEM or exclude it (TABLE_EXCLUDE) — refusing to "
            f"silently skip a vocabulary gap (S5.2 fail-loud totality)."
        )
    if entity not in ENTITY_CONCEPT_IDS:
        raise ValueError(
            f"Table→entity mapping '{schema}.{bare}' -> '{entity}' is not entity-capable "
            f"(not in ENTITY_CONCEPT_IDS) — no silent fallback (S5.2)."
        )
    return entity
