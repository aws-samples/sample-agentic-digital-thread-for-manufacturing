# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the live schema-existence layer:

* ``agents.schema_check`` — the reusable ``column_exists`` / ``column_in_schema`` /
  ``load_system_schema`` helpers (Fix A #2).
* ``server.get_proposed_mappings_route`` field_annotation ENRICHMENT (Fix A #3): after the
  pure ``score_proposal`` runs, the recommendation is upgraded to a real verdict
  (✅ verified / ❌ not found) when the schema is available, and left UNCHANGED when
  the schema is unavailable (None fallback).

No AWS, no Neptune, no network — ``get_system_schema`` (graph read) and the route's
mapping/concept reads are monkeypatched, so the graph stays untouched (ADR-0024).
"""
from __future__ import annotations

import asyncio
import json

import agents.schema_check as schema_check


# --------------------------------------------------------------------------- #
# schema_check.column_in_schema — pure, no I/O
# --------------------------------------------------------------------------- #
_SCHEMA = {"orders": {"columns": ["customer_code", "order_id"]},
           "runs": {"columns": ["cust_id"]}}


def test_column_in_schema_present():
    assert schema_check.column_in_schema(_SCHEMA, "orders.customer_code") is True


def test_column_in_schema_column_absent_is_false():
    assert schema_check.column_in_schema(_SCHEMA, "orders.nope") is False


def test_column_in_schema_table_absent_is_false():
    assert schema_check.column_in_schema(_SCHEMA, "ghost.col") is False


def test_column_in_schema_unavailable_schema_is_none():
    assert schema_check.column_in_schema(None, "orders.customer_code") is None


def test_column_in_schema_non_qualified_field_is_none():
    # No dot → cannot resolve a table.column → indeterminate.
    assert schema_check.column_in_schema(_SCHEMA, "customer_code") is None


# --------------------------------------------------------------------------- #
# schema_check.column_in_schema — nested/dotted + case tolerance (false-negative fix)
# --------------------------------------------------------------------------- #
# SiteWise properties and flattened nested-struct columns are stored as dotted
# leaf paths; identifiers also vary in case across connectors. These previously
# produced a misleading "column not found" verdict that contradicted the accept
# rationale. The check now recognises them.
_SW_SCHEMA = {
    "entity_metrics": {"columns": ["oee.availability", "oee.performance", "asset_id"]},
    "AssetModel": {"columns": ["Temperature", "Pressure"]},
}


def test_column_in_schema_case_insensitive_table_and_column():
    assert schema_check.column_in_schema(_SW_SCHEMA, "assetmodel.temperature") is True
    assert schema_check.column_in_schema(_SW_SCHEMA, "ASSETMODEL.PRESSURE") is True


def test_column_in_schema_nested_dotted_leaf_present():
    # Multi-dot table.struct.leaf whose flattened leaf IS stored → verified.
    assert schema_check.column_in_schema(
        _SW_SCHEMA, "entity_metrics.oee.availability"
    ) is True


def test_column_in_schema_struct_parent_matches_stored_leaves():
    # A reference to the struct PARENT (single-dot) matches when its dotted leaves
    # are present — a legitimate field, not a "not found".
    assert schema_check.column_in_schema(_SW_SCHEMA, "entity_metrics.oee") is True


def test_column_in_schema_multidot_unknown_is_indeterminate_not_false():
    # An unresolvable multi-dot reference stays None (indeterminate) — never a
    # misleading False that would fire a contradictory block.
    assert schema_check.column_in_schema(_SW_SCHEMA, "a.b.c") is None


def test_column_in_schema_single_dot_absent_still_false():
    # The definitive single-dot missing case is preserved (a real block).
    assert schema_check.column_in_schema(_SW_SCHEMA, "entity_metrics.ghost") is False
    assert schema_check.column_in_schema(_SW_SCHEMA, "ghost_table.col") is False


# --------------------------------------------------------------------------- #
# schema_check.load_system_schema / column_exists — get_system_schema stubbed
# --------------------------------------------------------------------------- #
def _stub_get_system_schema(monkeypatch, payload):
    import agents.graph_tools as graph_tools
    monkeypatch.setattr(graph_tools, "get_system_schema", lambda _sid: payload)


def test_load_system_schema_parses_table_map(monkeypatch):
    _stub_get_system_schema(monkeypatch, json.dumps(_SCHEMA))
    assert schema_check.load_system_schema("erp") == _SCHEMA


def test_load_system_schema_error_payload_is_none(monkeypatch):
    _stub_get_system_schema(monkeypatch, json.dumps({"error": "not onboarded"}))
    assert schema_check.load_system_schema("erp") is None


def test_load_system_schema_bad_json_is_none(monkeypatch):
    _stub_get_system_schema(monkeypatch, "not json{")
    assert schema_check.load_system_schema("erp") is None


def test_load_system_schema_lookup_raise_is_none(monkeypatch):
    import agents.graph_tools as graph_tools

    def _boom(_sid):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(graph_tools, "get_system_schema", _boom)
    # Never raises — an unavailable schema is an expected None fallback.
    assert schema_check.load_system_schema("erp") is None


def test_column_exists_true_false_none(monkeypatch):
    _stub_get_system_schema(monkeypatch, json.dumps(_SCHEMA))
    assert schema_check.column_exists("erp", "orders.customer_code") is True
    assert schema_check.column_exists("erp", "orders.nope") is False

    _stub_get_system_schema(monkeypatch, json.dumps({"error": "x"}))
    assert schema_check.column_exists("erp", "orders.customer_code") is None


# --------------------------------------------------------------------------- #
# server.get_proposed_mappings_route — field_annotation enrichment
# --------------------------------------------------------------------------- #
def _run_get_proposed_mappings(monkeypatch, mappings, schema_for, known=None):
    """Drive get_proposed_mappings_route offline: stub the mapping/concept reads, the
    per-system schema loader, and the catalog read for the unknown-system verdict.
    ``schema_for`` maps system_id → parsed schema | None; ``known`` is the
    catalog-systems list (default ``[]`` — no basis to judge, the base-mode /
    pre-fix behaviour)."""
    import agents.self_learning as self_learning
    import agents.concept_layer as concept_layer

    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: mappings)
    monkeypatch.setattr(self_learning, "known_systems", lambda: list(known or []))
    monkeypatch.setattr(concept_layer, "concept_descriptions", lambda _ids: {})
    monkeypatch.setattr(concept_layer, "concept_layer_of", lambda _c: "field")
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: schema_for.get(sid))

    from agents.server import get_proposed_mappings_route
    return asyncio.run(get_proposed_mappings_route())


def test_field_annotation_enrichment_verified(monkeypatch):
    mappings = [{"proposed_type": "field_annotation", "from_system": "erp",
                 "from_field": "orders.customer_code", "concept": "customer"}]
    out = _run_get_proposed_mappings(
        monkeypatch, mappings,
        {"erp": {"orders": {"columns": ["customer_code"]}}},
    )
    m = out["mappings"][0]
    assert m["ai_recommendation"] == "✅ Column verified in erp.orders.customer_code"
    assert m["ai_confidence"] == "high"
    # Structured verdict + proof for the UI (trustworthy, non-contradictory display).
    assert m["schema_verdict"] == "verified"
    assert m["schema_checked"] == "erp.orders.customer_code"


def test_field_annotation_enrichment_not_found(monkeypatch):
    mappings = [{"proposed_type": "field_annotation", "from_system": "erp",
                 "from_field": "orders.ghost", "concept": "customer"}]
    out = _run_get_proposed_mappings(
        monkeypatch, mappings,
        {"erp": {"orders": {"columns": ["customer_code"]}}},
    )
    m = out["mappings"][0]
    assert m["ai_recommendation"] == "❌ Column not found in erp schema — do not accept"
    assert m["ai_confidence"] == "low"
    # A definitive block carries proof so the UI can suppress the accept-rationale.
    assert m["schema_verdict"] == "not_found"
    assert m["schema_checked"] == "erp.orders.ghost"


def test_field_annotation_enrichment_schema_unavailable_falls_back(monkeypatch):
    """When the schema is unavailable (None), score_proposal's static text/confidence
    is preserved unchanged (the pre-fix behaviour)."""
    mappings = [{"proposed_type": "field_annotation", "from_system": "erp",
                 "from_field": "orders.customer_code", "concept": "customer"}]
    out = _run_get_proposed_mappings(monkeypatch, mappings, {"erp": None})
    m = out["mappings"][0]
    # Exactly score_proposal's field_annotation (single-dot) verdict.
    assert m["ai_recommendation"] == (
        "⚠️ Review — verify column exists in schema before accepting"
    )
    assert m["ai_confidence"] == "medium"


# --------------------------------------------------------------------------- #
# schema_check.system_in_catalog — pure, three-valued (unknown-system verdict)
# --------------------------------------------------------------------------- #
_KNOWN = [{"id": "erp", "name": "ERP"}, {"id": "siteb-athena", "name": "Site B"}]


def test_system_in_catalog_true_on_id_match():
    assert schema_check.system_in_catalog("erp", _KNOWN) is True


def test_system_in_catalog_definitive_false_on_nonempty_catalog():
    # A plant/site name is NOT a catalog system id — definitive.
    assert schema_check.system_in_catalog("SITEB", _KNOWN) is False


def test_system_in_catalog_no_basis_is_none():
    assert schema_check.system_in_catalog("erp", []) is None
    assert schema_check.system_in_catalog("erp", None) is None
    assert schema_check.system_in_catalog("", _KNOWN) is None


# --------------------------------------------------------------------------- #
# server.get_proposed_mappings_route — DEFINITIVE unknown-system verdict (live
# incident 2026-07-14: from_system was a PLANT name, the schema was
# unavailable, the verdict stayed None, and the UI's ⛔ gate never fired).
# --------------------------------------------------------------------------- #
def test_unknown_system_gets_definitive_not_found_verdict(monkeypatch):
    # from_system names a plant, not a catalog system id; the schema is
    # unavailable (the exact live shape). With a NON-EMPTY catalog this is
    # DEFINITIVE — the UI-gated 'not_found' verdict must fire, not None.
    mappings = [{"proposed_type": "field_annotation", "from_system": "SITEB",
                 "from_field": "ghost_table.status", "concept": "schema-annotation"}]
    out = _run_get_proposed_mappings(monkeypatch, mappings, {"SITEB": None},
                                     known=_KNOWN)
    m = out["mappings"][0]
    assert m["schema_verdict"] == "not_found"
    assert m["schema_checked"] == "SITEB.ghost_table.status"
    assert m["ai_confidence"] == "low"
    assert "not an onboarded system" in m["ai_recommendation"]


def test_unknown_system_blocks_join_like_types_too(monkeypatch):
    mappings = [{"proposed_type": "value_mapping", "from_system": "SITEB",
                 "from_field": "orders.line", "from_value": "A",
                 "to_system": "erp", "to_field": "events.code", "to_value": "B",
                 "concept": "production-line"}]
    out = _run_get_proposed_mappings(monkeypatch, mappings, {"SITEB": None},
                                     known=_KNOWN)
    assert out["mappings"][0]["schema_verdict"] == "not_found"


def test_unknown_system_with_empty_catalog_never_blocks(monkeypatch):
    # No basis to judge (base mode pre-onboarding / failed catalog read) —
    # keep the static text, never a false definitive block.
    mappings = [{"proposed_type": "field_annotation", "from_system": "SITEB",
                 "from_field": "ghost_table.status", "concept": "schema-annotation"}]
    out = _run_get_proposed_mappings(monkeypatch, mappings, {"SITEB": None},
                                     known=[])
    m = out["mappings"][0]
    assert "schema_verdict" not in m
    assert m["ai_confidence"] == "medium"


def test_known_system_with_unavailable_schema_stays_indeterminate(monkeypatch):
    # The system IS in the catalog but its schema is unavailable — the
    # unknown-system branch must NOT fire (only column verdicts may block).
    mappings = [{"proposed_type": "field_annotation", "from_system": "erp",
                 "from_field": "orders.customer_code", "concept": "customer"}]
    out = _run_get_proposed_mappings(monkeypatch, mappings, {"erp": None},
                                     known=_KNOWN)
    m = out["mappings"][0]
    assert "schema_verdict" not in m
    assert m["ai_confidence"] == "medium"
