# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic reproduction fixture: the Mode-A route seed at 7, 8 and 9 systems.

Mirrors the SHAPE measured live on 2026-10-04 on a nine-system workshop
deployment (generic placeholders, no dataset literals): one ledger-like system
holding ten evidence-backed (tier-1) rules, five more systems of one
connection type that carry only that type's generic default guidance, three
plant systems with short registration guidance that says which system owns
what, and tier-2 shared concepts on every system. Seven systems is the state
before the maintenance systems are onboarded, eight adds the plant maintenance
system, nine adds a second generic-default system.

``build_seed`` runs the PRODUCTION serializer through the injectable graph
seam. ``main_behaviour()`` disables the three delivery levers (shared default
sections, the bounded report aggregate, empty-key omission) and so reproduces
the serializer as it was before them; ``measure`` turns a seed into the
numbers the tests pin and the merge request quotes.
"""
from __future__ import annotations

import contextlib
import json
from unittest import mock

from agents import orchestrator as _orch
from agents.connection_types import (
    POSTGRESQL,
    SITEWISE_DIRECT,
    default_capabilities,
    default_query_guidance,
)
from agents.guidance_scope import GuidanceScope, append_note, shared_reference_key

GENERIC_BASE = default_query_guidance(POSTGRESQL)
GENERIC_CAPABILITIES = default_capabilities(POSTGRESQL)

#: The serialized budget the three delivery levers were measured and pinned
#: under (2026-10-04). The production budget has since been raised
#: (``orchestrator._ROUTE_SEED_SERIALIZED_BUDGET``, ADR-0035 amendment
#: 2026-10-08); at that budget this fixture is no longer under pressure, so the
#: lever pins run here to keep measuring what they were written to measure.
#: Raised 8000 -> 8200 on 2026-10-18 (ADR-0035 amendment): the
#: route-seed header grew ~146 chars to state the value-level ValueMapping
#: contract (count, not values), which pushed the 9-system "after" lever's
#: composition peak just over 8000 so it yielded the generic default it is
#: meant to deliver here. 8200 restores the exact delivery the lever pins; the
#: "before" variant still shows the pressure the lever solves, and production
#: delivery is unaffected (every base delivers at the 16000 production budget).
#: The workshop "previous budget cut a base" demonstration pins its own 8000
#: locally (test_route_seed_workshop_budget) so it is not coupled to this value.
LEVER_BUDGET = 8_200

#: The ledger-like system first; the 9th system is a second generic one.
_GENERIC_SEVEN = ["ledger", "product-data", "site-a-execution",
                  "site-b-execution", "site-c-execution"]
_GENERIC_NINE_EXTRA = "enterprise-maintenance"

#: id -> (connection type, registration guidance, capabilities)
REGISTRATION = {
    "plant-execution": (
        POSTGRESQL,
        "Answers which line and work order built a production run and its "
        "quality outcome. Equipment E-301 on LINE3 is the machining center the "
        "floor calls M-100. Translate other systems' identifiers through the "
        "graph's identity rules, never by guessing.",
        "Live plant-floor execution for this plant's lines LINE1, LINE2 and "
        "LINE3: work orders, production events, quality checks, downtime "
        "events and line status."),
    "plant-maintenance": (
        POSTGRESQL,
        "Answers maintenance history and open/deferred work orders per asset; "
        "the M-100 is asset A-1001-M-100.",
        "Maintenance for this plant: assets (asset_num A-1001-M-100), work "
        "orders, preventive-maintenance schedules and deferrals."),
    "plant-sensors": (
        SITEWISE_DIRECT,
        "The ONLY sensor source: what a machine's sensors read NOW and over "
        "the last hours/days. Use for 'is vibration trending up' questions.",
        "Live OT telemetry for this plant's machines from the plant sensor "
        "store (asset Machining-Center-M-100: vibration, coolant temperature)."),
}

LEARNED_NOTES = [
    f"Learned note {index}: filter production runs by run_number and join "
    f"quality checks on the run; report the line and the work order number, "
    f"never row ids. ({index})" for index in range(4)]


def system_ids(count: int) -> list[str]:
    """The connected-system ids for a 7-, 8- or 9-system deployment."""
    ids = [*_GENERIC_SEVEN, "plant-execution", "plant-sensors"]
    if count >= 8:
        ids.append("plant-maintenance")
    if count >= 9:
        ids.append(_GENERIC_NINE_EXTRA)
    assert len(ids) == count, count
    return sorted(ids)


def generic_ids(count: int) -> list[str]:
    return [sid for sid in system_ids(count) if sid not in REGISTRATION]


def _guidance(system_id: str, learned_notes: int):
    if system_id in REGISTRATION:
        guidance = REGISTRATION[system_id][1]
        if system_id == "plant-execution":
            for note in LEARNED_NOTES[:learned_notes]:
                guidance = append_note(guidance, note, GuidanceScope.QUERY_HINT)
        return guidance
    return GENERIC_BASE


def inventory_rows(count: int, learned_notes: int = 0) -> list[dict]:
    rows = []
    for sid in system_ids(count):
        connection_type, _base, capabilities = REGISTRATION.get(
            sid, (POSTGRESQL, GENERIC_BASE, GENERIC_CAPABILITIES))
        rows.append({"system_id": sid, "system_name": sid.replace("-", " ").title(),
                     "plants": ["plant-one" if sid in REGISTRATION else "all-sites"],
                     "capabilities": capabilities, "connection_type": connection_type,
                     "query_guidance": _guidance(sid, learned_notes)})
    return rows


def _rule_rows(count: int) -> list[dict]:
    peers = ["plant-execution", "site-a-execution", "site-b-execution",
             "site-c-execution"]
    joins = [{"id": f"jp-{peer}", "from_system": peer,
              "from_field": f"{peer.replace('-', '_')}.production_runs.run_number",
              "to_system": "ledger", "to_field": "ledger_data.shipment_items.batch_id",
              "transform": "identity", "method": "value_exact_match",
              "confidence": 0.85, "provenance": "measured-overlap"} for peer in peers]
    joins.append({"id": "jp-product-data", "from_system": "product-data",
                  "from_field": "product_data.products.item_id", "to_system": "ledger",
                  "to_field": "ledger_data.products.engineering_part_number",
                  "transform": "identity", "method": "value_exact_match",
                  "confidence": 0.85, "provenance": "measured-overlap"})
    identities = [{"id": f"ir-{peer}", "concept": "batch", "source_system": peer,
                   "source_field": "run_number", "target_system": "ledger",
                   "target_field": "batch_id", "source_pattern": "",
                   "target_pattern": "", "transform": None,
                   "method": "value_exact_match", "confidence": 0.85,
                   "provenance": "measured-overlap"} for peer in peers]
    identities.append({"id": "ir-product-data", "concept": "product",
                       "source_system": "product-data", "source_field": "item_id",
                       "target_system": "ledger",
                       "target_field": "engineering_part_number",
                       "source_pattern": "", "target_pattern": "",
                       "method": "value_exact_match", "confidence": 0.85,
                       "provenance": "measured-overlap"})
    ids = system_ids(count)
    rows = [{"system_id": "ledger", "join_paths": joins, "identity_rules": identities,
             "value_mappings": []}]
    for sid in ids:  # tier-2 shared concepts, as an onboarded graph returns them
        rows.append({"system_id": sid, "shared_concepts": [
            {"concept": concept, "peer_system": peer}
            for concept in ("batch", "equipment-id", "work-order")
            for peer in ids if peer != sid][:12]})
    return rows


def build_seed(count: int, *, learned_notes: int = 0, orchestrator=None,
               budget: "int | None" = None) -> str:
    """The route seed for ``count`` systems through the production serializer.

    ``budget`` runs it under that serialized budget instead of the production
    one (``LEVER_BUDGET`` reproduces the pressure the levers were pinned under).
    """
    orch = orchestrator or _orch
    rows = inventory_rows(count, learned_notes)
    rules = _rule_rows(count)

    def fake_query(cypher, *_args, **_kwargs):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return rows
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rules
        return []

    if budget is None:
        return orch.build_route_seed(query=fake_query)
    with mock.patch.object(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget):
        return orch.build_route_seed(query=fake_query)


@contextlib.contextmanager
def main_behaviour(orchestrator=None):
    """The serializer WITHOUT the three delivery levers (the behaviour before)."""
    orch = orchestrator or _orch
    with mock.patch.multiple(
            orch,
            _share_default_guidance=lambda payload: False,
            _aggregate_guidance_delivery_reports=lambda public: None,
            _EMPTY_OMITTED_SEED_KEYS=frozenset()):
        yield


def _payload(seed: str) -> dict:
    return json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]


def _compact(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")))


def measure(seed: str, count: int) -> dict:
    """Delivery numbers for one seed: what reached the prompt, and at what cost."""
    payload = _payload(seed)
    systems = {s["id"]: s for s in payload["systems"]}
    shared = payload.get("shared_guidance") or {}
    rows = {row["system_id"]: row for row in inventory_rows(count)}

    def base_delivered(sid: str) -> bool:
        guidance = systems[sid].get("query_guidance") or ""
        key = shared_reference_key(guidance)
        if key is not None:
            return GENERIC_BASE in (shared.get(key) or "")
        return GENERIC_BASE in guidance

    reports = [s["guidance_composition"]["delivery_truncation"]
               for s in payload["systems"]
               if "delivery_truncation" in (s.get("guidance_composition") or {})]
    if "guidance_delivery_truncation" in payload:
        reports.append(payload["guidance_delivery_truncation"])
    return {
        "seed_chars": len(seed),
        "registration_bases": sum(
            REGISTRATION[sid][1] in (systems[sid].get("query_guidance") or "")
            for sid in systems if sid in REGISTRATION),
        "generic_bases": sum(base_delivered(sid) for sid in generic_ids(count)),
        "capabilities": sum(systems[sid].get("capabilities") == rows[sid]["capabilities"]
                            for sid in systems),
        "rules": sum(len(rules) for s in payload["systems"]
                     for rules in (s.get("rules") or {}).values()),
        "drop_report_chars": sum(_compact(report) for report in reports),
    }
