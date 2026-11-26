# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The workshop's route-seed states, built from the workshop's own manifests.

Mirrors the offline budget measurement of 2026-10-08 (ADR-0035 amendment): the
systems a fresh workshop account has connected at module 6 and module 8, with
and without an MCP Server hub that holds one exact join (the HighByte hub in
the workshop package). Each system carries its manifest registration text; a
system registered without guidance carries its connection type's default, as
provisioning sets it. The sensor system carries the module 6 attendee note.
Tier-1 rules are the exact joins the exact-agreement pass writes (every MES
run number to the ERP batch, the product item to the ERP part number, the
plant MES work order to the ERP lot-tracking batch, and the hub's machine name
to the sensor asset name), with the pass's own reasoning text; a tier-2 pool
(shared concepts, internal foreign-key joins, one onboarding value mapping,
authority rules) is sized like a fresh account's. Ids, schemas and texts come
from the manifests, never from literals in this file.

``build_seed`` runs the PRODUCTION ``build_route_seed`` through its graph seam.
"""
from __future__ import annotations

import hashlib
import json

from agents import identity_affinity as ia
from agents import orchestrator as _orch
from agents.connection_types import MCP_SERVER, default_capabilities, default_query_guidance
from agents.demo import workshop as _ws
from agents.demo.systems import DEMO_SYSTEMS
from agents.guidance_scope import GuidanceScope, append_note

#: The note module 6 step 3 has the attendee add to the sensor system.
SENSOR_NOTE = ("When reporting a vibration trend for the CMX-441, state the reading at "
               "the start of the window, the lowest reading, and the latest reading, "
               "each with its time.")

#: A module 5 question naming the attendee's own production run: the run
#: number has the measured shape of every MES -> ERP run-number join, so the
#: seed's IDENTIFIER OWNERS block applies.
RUN_NUMBER = "RUN-20261008-90001"
IDENTIFIER_QUESTION = (f"Was machine L3-M1 maintained on schedule before production run "
                       f"{RUN_NUMBER} ran?")

HUB_ID = "uns-hub"
_HUB = {"id": HUB_ID, "name": "Unified Namespace Hub", "plant": "All Sites",
        "system_type": "UNS", "connection_type": MCP_SERVER, "schema": "uns"}
MANIFEST = {s["id"]: dict(s) for s in
            [*DEMO_SYSTEMS, *_ws.registered_systems("live", highbyte_shipped=False), _HUB]}
SENSOR_ID = _ws.LIVE_SITEWISE_SYSTEM["id"]
PLANT_MES_ID = _ws.LIVE_MES_SYSTEM["id"]

#: The register-only system the attendee onboards in module 4 (the plant CMMS).
MODULE_4_ONBOARDED = tuple(sorted(s["id"] for s in _ws.WORKSHOP_REGISTER_ONLY_SYSTEMS))
MODULE_6 = (*_ws.seeded_system_ids("live"), *MODULE_4_ONBOARDED)
MODULE_8 = (*MODULE_6, *(s["id"] for s in DEMO_SYSTEMS if s["id"] not in MODULE_6))
STATES = {"module 6": MODULE_6, "module 8": MODULE_8,
          "module 6 + hub": (*MODULE_6, HUB_ID), "module 8 + hub": (*MODULE_8, HUB_ID)}


def registration_ids(ids) -> list[str]:
    """Systems whose manifest carries registration guidance (not a default)."""
    return [sid for sid in ids if MANIFEST[sid].get("query_guidance")]


def base_guidance(sid: str) -> str:
    entry = MANIFEST[sid]
    return entry.get("query_guidance") or default_query_guidance(entry["connection_type"])


def _inventory(ids) -> list[dict]:
    rows = []
    for sid in sorted(ids):
        entry = MANIFEST[sid]
        guidance = base_guidance(sid)
        if sid == SENSOR_ID:
            guidance = append_note(guidance, SENSOR_NOTE, GuidanceScope.QUERY_HINT)
        rows.append({"system_id": sid, "system_name": entry["name"],
                     "plants": [entry["plant"].lower().replace(" ", "-")],
                     "capabilities": (entry.get("capabilities")
                                      or default_capabilities(entry["connection_type"])),
                     "connection_type": entry["connection_type"], "query_guidance": guidance})
    return rows


def _of_type(system_type: str) -> list[str]:
    return sorted(sid for sid, entry in MANIFEST.items() if entry["system_type"] == system_type)


def _exact_pair(source: tuple, target: tuple, sample: str, concept: str) -> tuple[dict, dict]:
    """One exact IdentityRule + its JoinPath, as the exact pass writes them."""
    (src_sys, src_table, src_field), (tgt_sys, tgt_table, tgt_field) = source, target
    src = ia.SampledField(src_sys, src_table, src_field, concept, (sample,) * 40)
    tgt = ia.SampledField(tgt_sys, tgt_table, tgt_field, concept, (sample,) * 400)
    reasoning = ia._exact_reasoning(src, tgt, ia.literal_shape(sample), set(range(19)), concept)
    # Ids shaped like the live ones (writer prefix, system, 16 hex characters).
    digest = hashlib.sha256(f"{src_sys}.{src_field}-{tgt_sys}.{tgt_field}".encode()).hexdigest()[:16]
    schema = {sid: MANIFEST[sid].get("schema") or sid.replace("-", "_") for sid in (src_sys, tgt_sys)}
    rule = {"id": f"ir-{src_sys}-{digest}", "concept": concept, "source_system": src_sys,
            "source_field": src_field, "target_system": tgt_sys, "target_field": tgt_field,
            "source_pattern": "", "target_pattern": "", "method": ia.EXACT_METHOD,
            "confidence": 0.85, "provenance": reasoning}
    join = {"id": f"jp-{src_sys}-{digest}", "from_system": src_sys,
            "from_field": f"{schema[src_sys]}.{src_table}.{src_field}", "to_system": tgt_sys,
            "to_field": f"{schema[tgt_sys]}.{tgt_table}.{tgt_field}", "transform": "identity",
            "method": ia.EXACT_METHOD, "confidence": 0.85, "provenance": reasoning}
    return rule, join


def tier1_pairs(ids) -> list[tuple[dict, dict]]:
    (erp,), (plm,) = _of_type("ERP"), _of_type("PLM")
    pairs = [_exact_pair((mes, "production_runs", "run_number"),
                         (erp, "shipment_items", "batch_id"), RUN_NUMBER, "batch")
             for mes in _of_type("MES") if mes in ids]
    pairs.append(_exact_pair((plm, "products", "item_id"),
                             (erp, "products", "engineering_part_number"), "PN-1001", "product"))
    pairs.append(_exact_pair((PLANT_MES_ID, "work_orders", "work_order_number"),
                             (erp, "lot_mes_tracking", "batch_id"), "WO-20261008-10001", "batch"))
    if HUB_ID in ids:
        pairs.append(_exact_pair((HUB_ID, "machines", "machine_name"),
                                 (SENSOR_ID, "assets", "asset_name"),
                                 "5-Axis-Milling-CMX-441", "equipment-id"))
    return pairs


_CONCEPTS = ["batch", "equipment-id", "work-order", "product", "bill-of-materials",
             "actual-quantity", "sensor-reading", "inspection", "lot", "customer",
             "shipment", "downtime", "line", "shift", "quality-check", "supplier"]


def _rule_rows(ids) -> list[dict]:
    peers = sorted(ids)
    rows = {sid: {"system_id": sid, "value_mappings": [], "identity_rules": [],
                  "shared_concepts": [{"concept": c, "peer_system": p}
                                      for c in _CONCEPTS[i % 5: i % 5 + 5]
                                      for p in peers if p != sid][:50],
                  "join_paths": [{"id": f"jp-internal-{sid}-{k}", "from_system": sid,
                                  "from_field": f"t{k}.parent_id", "to_system": sid,
                                  "to_field": f"t{k + 1}.id", "transform": "direct",
                                  "method": "foreign_key_match", "confidence": 0.9}
                                 for k in range(7)]}
            for i, sid in enumerate(peers)}
    (cmms_plant,) = MODULE_4_ONBOARDED
    rows[cmms_plant]["value_mappings"].append(
        {"from_system": cmms_plant, "from_field": "assets.asset_num", "to_system": SENSOR_ID,
         "to_field": "asset_name", "concept": "equipment-id", "method": "value_format_match",
         "confidence": 0.95,
         # The traversal groups a field pair's mappings and carries their values.
         "value_pairs": [{"from": "A-100", "to": "A100"}], "value_pair_count": 1})
    for rule, join in tier1_pairs(ids):
        for sid in {rule["source_system"], rule["target_system"]}:  # under BOTH endpoints
            rows[sid]["identity_rules"].append(rule)
            rows[sid]["join_paths"].append(join)
    return [rows[sid] for sid in peers]


def _authority_rows(ids) -> list[dict]:
    return [{"id": f"ar-{sid}-{c}", "concept": c, "system_id": sid, "method": None,
             "confidence": None}
            for i, sid in enumerate(sorted(ids)) for c in _CONCEPTS[i % 7: i % 7 + 4]]


def graph(ids):
    """The fake graph-query seam for ``ids``."""
    inventory, rules, authority = _inventory(ids), _rule_rows(ids), _authority_rows(ids)

    def fake_query(cypher, *_args, **_kwargs):
        if cypher == _orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == _orch._ROUTE_SEED_CYPHER:
            return rules
        if cypher == _orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return authority
        return []
    return fake_query


def build_seed(ids, question: "str | None" = None, orchestrator=None) -> str:
    orch = orchestrator or _orch
    return orch.build_route_seed(query=graph(ids), question=question)


def decode(seed: str) -> tuple[dict, str]:
    """``(payload, the text after the JSON)`` of a seed."""
    body = seed.split("RESULT:\n", 1)[1]
    payload, end = json.JSONDecoder().raw_decode(body)
    return payload, body[end:]


def tier1_total(ids) -> int:
    return len({row["id"] for pair in tier1_pairs(ids) for row in pair})
