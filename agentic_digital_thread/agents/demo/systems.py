# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared demo-system manifest (single source of truth).

The SIX demo SQL systems (ERP + three plant MES + CMMS + PLM) as an EXPLICIT
id/name/plant/system_type/connection_type/schema manifest. This is the demo-seam
manifest ONLY — it is NOT added to the base ``catalog`` (which stays EMPTY in base
mode, ADR-0030). It is consumed by:

  * the server startup registration hook (``server._register_demo_systems``), which
    — when the demo addon is enabled (``ADT_DEMO`` / runtime override) — REGISTERS
    each of these systems through the SAME ``connect_new_system`` path any system
    uses, so they appear in ``GET /api/onboard/catalog`` (status ``registered``)
    and can be onboarded; and
  * the offline test fixture ``tests/unit/_demo_catalog.py``, which imports these
    identities (keeping the fixture DRY with the real manifest).

The ``id`` values are the CANONICAL, tool-name-safe system ids the onboarding
harness onboards BY and asserts ``adt_<id>`` runtimes for — ``erp``, ``mes-mil``,
``mes-det``, ``mes-cr``, ``cmms``, ``plm`` — so they MUST be used VERBATIM at
registration (see ``connect_new_system(system_id=...)``), never re-derived by
slugifying the display name (which would yield e.g. ``milwaukee-mes`` != ``mes-mil``).

Generic dependency-free stdlib module — safe to import from the demo seam without
dragging in the agent stack.
"""
from __future__ import annotations

# The six demo SQL systems. ``schema`` is the PostgreSQL schema each system lives
# in; ``demo.db_routing.demo_pg_dsn(schema)`` routes erp_*/mes_* to the ERP/Aurora DB and
# everything else (cmms_*/plm_*) to the demo-RDS DB. Plants mirror the historical
# demo catalog identities.
#
# ``agent_query_notes`` (optional): column meanings for the SYSTEM AGENT that
# writes the system's SQL, nothing else. Provision appends them to that
# agent's prompt as a fenced QUERY HINTS block (``onboard._agent_query_notes_block``
# through :func:`agent_query_notes`). They are deliberately NOT
# ``query_guidance``: the route seed would deliver them to the orchestrator,
# which never sends column names to a system agent, and the workshop's route
# seed has no room left for them (the plant systems' own guidance and the
# identifier owners would be cut). Registration never reads the key.
#: ERP (test event, 2026-10-15): goods received from a set of batches were
#: valued with the invoice header total, tax included, not the order line.
#: Rehearsal account (2026-10-17): asked for "the monetary value of that
#: shipment", the agent summed each shipment's invoice total. The note says a
#: shipment has no amount, names both header totals and says not to use them
#: as the value of goods shipped or received, and keeps them for an order's
#: total and an amount billed (the open-orders golden and the Daily Briefing
#: ask for those).
_ERP_AGENT_QUERY_NOTES = (
    "A shipment has no amount of its own. The value of a shipment, or of the parts a "
    "customer received in it, is the sum of shipment_items.quantity_shipped x "
    "order_line_items.unit_price, joining each shipment item to its order line on "
    "line_item_id. A whole order line is worth its line_total (quantity x unit_price, before "
    "tax). invoices.total_amount and customer_orders.total_amount are whole-document "
    "figures: subtotal plus tax (an order's also shipping). Never use them as the value of "
    "goods shipped or received. Use customer_orders.total_amount for an order's total or "
    "total value, invoices.total_amount for an amount billed or invoiced, and read subtotal "
    "and tax_amount before describing what a header figure includes."
)

DEMO_SYSTEMS: list[dict] = [
    {"id": "erp", "name": "ERP", "plant": "All Sites", "system_type": "ERP",
     "connection_type": "PostgreSQL", "schema": "erp_data",
     "agent_query_notes": _ERP_AGENT_QUERY_NOTES},
    {"id": "mes-mil", "name": "Milwaukee MES", "plant": "Milwaukee", "system_type": "MES",
     "connection_type": "PostgreSQL", "schema": "mes_milwaukee"},
    {"id": "mes-det", "name": "Detroit MES", "plant": "Detroit", "system_type": "MES",
     "connection_type": "PostgreSQL", "schema": "mes_detroit"},
    {"id": "mes-cr", "name": "Cedar Rapids MES", "plant": "Cedar Rapids", "system_type": "MES",
     "connection_type": "PostgreSQL", "schema": "mes_cedarrapids"},
    {"id": "cmms", "name": "CMMS", "plant": "All Sites", "system_type": "CMMS",
     "connection_type": "PostgreSQL", "schema": "cmms_maintenance"},
    {"id": "plm", "name": "PLM", "plant": "All Sites", "system_type": "PLM",
     "connection_type": "PostgreSQL", "schema": "plm_engineering"},
]


def manifest_systems() -> list[dict]:
    """All systems the startup hook must REGISTER (copies).

    The demo six always; plus — when workshop mode is enabled
    (``agents.demo.workshop.is_enabled``, the ``ADT_WORKSHOP`` env flag) — the
    deployed story's workshop systems (``workshop.registered_systems``: the
    storyline systems the seed CR onboards, then the register-only ones); or —
    workshop disabled — the demo plant systems (``workshop.DEMO_PLANT_SYSTEMS``:
    the plant MES view + maintenance). Every extra is REGISTERED only: none is
    in a bulk-onboard default (``DEMO_SYSTEMS`` stays the six; see
    ``workshop.ONBOARD_EXCLUDE`` and docs/workshop-mode.md).
    """
    from . import workshop
    systems = [dict(s) for s in DEMO_SYSTEMS]
    if workshop.is_enabled():
        systems.extend(workshop.registered_systems())
    else:
        systems.extend(dict(s) for s in workshop.DEMO_PLANT_SYSTEMS)
    return systems


def agent_query_notes(system_id: str, schema: str) -> str:
    """The ``agent_query_notes`` of the manifest system with this id AND schema
    ("" when it has none). The schema must match too: one id can name a
    different system in another story or mode (a frozen storyline schema vs the
    live one), and that one is not this manifest entry. The caller decides
    whether the registration is the manifest's (a user's own registration under
    the same id never is). Reads static manifests only, no detection."""
    from . import workshop
    candidates = (*DEMO_SYSTEMS, *workshop.WORKSHOP_SYSTEMS, *workshop.LIVE_SEEDED_SYSTEMS,
                  workshop.HIGHBYTE_SYSTEM)
    for entry in candidates:
        if entry["id"] == system_id and (entry.get("schema") or "") == (schema or ""):
            return entry.get("agent_query_notes", "") or ""
    return ""
