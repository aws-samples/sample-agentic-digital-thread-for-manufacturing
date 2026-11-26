# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo concept-description overlays (demo addon — ADR 0019 / ADR 0044).

CONTENT half of the T1.2 core-concept neutrality split: the dataset-specific
examples removed from ``agents.catalog.CORE_CONCEPT_DEFINITIONS`` (demo asset
codes, site codes, plant names, equipment-class lists, and the batch/lot table
references) live HERE — the ADR-0044-sanctioned demo home — as pure data,
shaped for the T1.3 ``ConceptOverlay`` primitive: per concept id →
``examples`` / ``synonyms`` / ``value_patterns`` / ``note``.

The MECHANISM is the ONE proposal lifecycle (ADR-0003): overlays land as
``proposed_type='concept_overlay'`` proposals through
``self_learning.log_proposed_mapping`` and, on accept,
``self_learning._apply_concept_overlay`` writes the standalone
``(:ConceptOverlay)`` node (no new edge type — ADR-0050 D1). Composition back
into the map prompts happens through ``catalog``'s shared composition point,
fenced as untrusted text (T1.7). :func:`demo_concept_overlay_proposals` is a
PURE shaping helper (no I/O) returning ``log_proposed_mapping`` kwargs; wiring
it into a deploy/onboard seam is deliberately deferred to the lane-M-gated
overlay-seeding task.

This module is pure data + the demo-enablement gate; it depends only on its
own package (one-way dependency: engine callers route through the functions
here, never the reverse). Engine files carry ZERO of these literals.
"""
from __future__ import annotations

import json

# The pack id + source stamp demo overlays carry (uninstall = delete by pack_id).
DEMO_CONCEPT_OVERLAY_PACK_ID = "demo"
DEMO_CONCEPT_OVERLAY_SOURCE = "pack:demo@1"

# concept id -> overlay text fields (examples / synonyms / value_patterns / note).
# Every literal below was MOVED out of engine code: the examples/synonyms/notes
# out of CORE_CONCEPT_DEFINITIONS (T1.2) verbatim, and the production-line
# value_patterns (the OPC UA demo tag path + the CMMS 'area' line values) out
# of onboard_agent.py's map/resolve prompt text (R2 prompt-text tranche).
DEMO_CONCEPT_OVERLAYS: dict[str, dict[str, str]] = {
    "equipment-id": {
        "examples": "MKE-WASH-001, DET-CNC1-001, CRR-MIXR-001",
        "note": (
            "Equipment types: washer, filler, capper, labeler, checkweigher, "
            "case_packer, palletizer (bottling); cnc_machine, heat_treat, "
            "surface_treat, assembly, cmm_inspection (automotive); mixer, "
            "extruder, oven, cooler, packager, metal_detector (food)."),
    },
    "asset-class": {
        "examples": (
            "Bottling: washer, filler, capper, labeler, carbonator, "
            "checkweigher, case_packer, palletizer. Automotive: cnc_machine, "
            "heat_treat, surface_treat, assembly, cmm_inspection, "
            "vision_inspection. Food: mixer, extruder, oven, cooler, "
            "packager, metal_detector."),
    },
    "asset": {
        "examples": "KF2000-MKE-01",
    },
    "production-line": {
        "examples": "Bottling-L1, AutoParts-L2, FoodProc-L3, MKE-L1",
        "value_patterns": (
            "OPC UA tag paths: AnyCompany/Milwaukee/Bottling/Line1/"
            "Line1Washer/Line1Washer_Down — 'Line1' is the production-line "
            "path segment; CMMS 'area' fields carry Bottling-L1 / "
            "AutoParts-L2 / FoodProc-L3."),
    },
    "production-run": {
        "examples": "B-2847, WO-9921",
    },
    "batch": {
        "examples": "DET-RUN-0001, MKE-BATCH-001, B-2847",
        "note": (
            "erp shipment_items.batch_id: the shipped run's number; erp "
            "lot_mes_tracking.batch_id: the MES work-order number; "
            "every site MES production_runs.run_number: its run numbers; "
            "plant MES batch_number: batch id."),
    },
    "inspection": {
        "examples": "CMM, vision, metal detector, checkweigher",
    },
    "food-safety": {
        "note": "Cedar Rapids food processing.",
    },
    "lot-number": {
        "examples": "LOT-DET-ALU-2026-001",
        "note": "Tables: lot_mes_tracking (batch_id).",
    },
    "facility": {
        "examples": "Milwaukee, Detroit, Cedar Rapids",
    },
    "site": {
        "examples": (
            "MKE, DET, CR, MILWAUKEE_BOTTLING, DETROIT_AUTOPARTS, "
            "CEDARRAPIDS_FOOD"),
    },
    "work-order": {
        "examples": "CMMS: WO-MKE-2026-0041. MES: WO-9921.",
    },
}


def demo_concept_overlays() -> dict:
    """The demo overlay content, or ``{}`` when the demo addon is disabled —
    engine call sites treat an empty dict as "nothing to seed", so non-demo
    deployments are untouched (same gate shape as ``curated_annotations``)."""
    from . import is_enabled
    if not is_enabled():
        return {}
    return {cid: dict(fields) for cid, fields in DEMO_CONCEPT_OVERLAYS.items()}


def demo_concept_overlay_proposals() -> list[dict]:
    """PURE shaping (no I/O): one ``log_proposed_mapping`` kwargs dict per demo
    overlay, matching the ``concept_overlay`` item contract
    (``concept`` = concept id, ``from_value`` = pack id, ``to_value`` = source
    stamp, ``query_context`` = the JSON text fields). ``[]`` when the demo
    addon is disabled."""
    return [
        {
            "proposed_type": "concept_overlay",
            "from_system": "", "from_field": "",
            "from_value": DEMO_CONCEPT_OVERLAY_PACK_ID,
            "to_system": "", "to_field": "",
            "to_value": DEMO_CONCEPT_OVERLAY_SOURCE,
            "concept": cid,
            "confidence": 1.0,
            "query_context": json.dumps(fields, sort_keys=True),
            "source": "user",
            "provenance": "curated-seed",
        }
        for cid, fields in demo_concept_overlays().items()
    ]
