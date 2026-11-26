# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the S5.2 Wave 3.2 mint/reuse/leave gating (ADR 0021).

After provisioning writes the LLM-mapped ``Field-[:MAPS_TO]->Concept`` edges, each
column the LLM left UNMAPPED is run through the asymmetric ADR 0021 rule:

  * generic/audit/surrogate column           → leave unmapped (no edge, no mint)
  * column on a SHARED entity, no precedent   → exactly one ``mint_field_concept`` call
  * column with a reuse-first precedent       → reuse: edge to the EXISTING concept, no mint
  * column on a SINGLE-system entity, no prec → leave unmapped (no edge, no mint)
  * LLM-mapped column                         → edge as before (untouched)

Pure in-process: every impure seam (Neptune, Postgres, LLM, AWS) is monkeypatched at
the module-global level, and the four concept_layer / self_learning seams are injected
so the gate's decision tree is exercised deterministically with no graph.
"""
from __future__ import annotations

import agents.onboard as onboard
import agents.self_learning as self_learning
from agents.models import ProvisionRequest


# --------------------------------------------------------------------------- #
# Fakes / seams (mirrors test_provision_entity_edge.py)
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    """Neutralize the PostgreSQL provision path; return (writes, mint_calls)."""
    writes: list[tuple[str, dict]] = []
    mint_calls: list[tuple] = []

    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub system prompt")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])

    # Mint seam — record calls, write nothing.
    def _record_mint(system, table, column, concept_id, name, confidence,
                     source="agent", description=""):
        mint_calls.append((system, table, column, concept_id, name, confidence,
                           source, description))
        return "mint-recorded"

    monkeypatch.setattr(self_learning, "mint_field_concept", _record_mint)
    return writes, mint_calls


def _install_concept_layer(monkeypatch, *, entity_map, generic, shared, precedents,
                           suggestions=None):
    """Inject the four concept_layer / concept_tagging seams used by the gate.

    entity_map: {table: entity or None}
    generic:    set of column names treated as generic (no mint)
    shared:     set of entities treated as shared (>=2 systems)
    precedents: {column: concept_id} returned as the top reuse-first suggestion,
                one the column itself supports (``column_match``)
    suggestions: {column: [suggestion, ...]} returned verbatim (overrides precedents)
    """
    monkeypatch.setattr(onboard, "entity_for_table",
                        lambda _schema, table: entity_map.get(table))
    monkeypatch.setattr(onboard, "is_generic_field", lambda col: col in generic)
    monkeypatch.setattr(onboard, "entity_is_shared",
                        lambda entity, _sys, **_k: entity in shared)

    def _reuse(_entity, _stype, column, **_k):
        if suggestions and column in suggestions:
            return suggestions[column]
        cid = precedents.get(column)
        return ([{"concept": cid, "score": 3.0, "reason": "precedent", "column_match": True}]
                if cid else [])

    monkeypatch.setattr(onboard, "reuse_first_suggestions", _reuse)


def _col(table, column, *, concept=None):
    """A column→mapping row. concept=None → LLM left it UNMAPPED (mappings: [])."""
    if concept is None:
        return {"table": table, "field": f"{table}.{column}", "concept": "needs_review",
                "mappings": []}
    return {"table": table, "field": f"{table}.{column}", "concept": concept,
            "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.9}]}


# --------------------------------------------------------------------------- #
# Edge-introspection helpers
# --------------------------------------------------------------------------- #
_FIELD_EDGE_FRAGMENT = "MERGE (f)-[r:MAPS_TO]->(c)"


def _field_concept_edges(writes) -> dict:
    """Map field_id → concept_id for every Field-[:MAPS_TO]->Concept edge written."""
    out = {}
    for stmt, p in writes:
        if _FIELD_EDGE_FRAGMENT in stmt and "fid" in p and "cid" in p:
            out[p["fid"]] = p["cid"]
    return out


def _run(monkeypatch, *, system_id="cmms", schema="cmms_maintenance", mappings,
         entity_map, generic, shared, precedents, suggestions=None):
    writes, mint_calls = _install_seams(monkeypatch)
    _install_concept_layer(monkeypatch, entity_map=entity_map, generic=generic,
                           shared=shared, precedents=precedents, suggestions=suggestions)
    body = ProvisionRequest(
        system_id=system_id,
        system_name=system_id.upper(),
        schema=schema,
        connection_type="PostgreSQL",
        mappings=mappings,
    )
    # No catalog dependency for system_type — patch catalog.get to a stub entry.
    monkeypatch.setattr(onboard.catalog, "get", lambda _sid: {"system_type": "CMMS"})
    monkeypatch.setattr(onboard.catalog, "get_onboarded_ids", lambda: set())
    monkeypatch.setattr(onboard.catalog, "update_status", lambda *_a, **_k: None)
    onboard._do_provision(body)
    return writes, mint_calls


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_unmapped_generic_column_leaves_no_edge_no_mint(monkeypatch):
    """An unmapped generic column (created_at) on a shared entity → no edge, no mint."""
    writes, mint_calls = _run(
        monkeypatch,
        mappings=[_col("assets", "created_at")],
        entity_map={"assets": "asset"},
        generic={"created_at"},
        shared={"asset"},
        precedents={},
    )
    edges = _field_concept_edges(writes)
    assert "cmms.assets.created_at" not in edges
    assert mint_calls == []


def test_unmapped_shared_entity_no_precedent_mints_once(monkeypatch):
    """An unmapped column on a SHARED entity with no precedent → exactly one mint call,
    with a clean kebab concept id/name derived from the column, and no MAPS_TO edge."""
    writes, mint_calls = _run(
        monkeypatch,
        mappings=[_col("spare_parts", "lead_time_days")],
        entity_map={"spare_parts": "spare-part"},
        generic=set(),
        shared={"spare-part"},
        precedents={},
    )
    assert len(mint_calls) == 1
    system, table, column, concept_id, name, confidence, source, description = mint_calls[0]
    assert (system, table, column) == ("cmms", "spare_parts", "lead_time_days")
    assert concept_id == "lead-time-days"
    assert name == "Lead Time Days"
    assert source == "agent"
    # The earned mint authors a cosmetic description (bedrock_call stubbed to a constant).
    assert description == "stub system prompt"
    # Mint does NOT write a Field->Concept edge inline (the proposal lifecycle owns the write).
    assert "cmms.spare_parts.lead_time_days" not in _field_concept_edges(writes)


def test_unmapped_with_precedent_reuses_existing_no_mint(monkeypatch):
    """An unmapped column with a reuse-first precedent → edge to the EXISTING concept, no mint."""
    writes, mint_calls = _run(
        monkeypatch,
        mappings=[_col("spare_parts", "reorder_point")],
        entity_map={"spare_parts": "spare-part"},
        generic=set(),
        shared={"spare-part"},  # shared, but precedent wins → reuse, not mint
        precedents={"reorder_point": "mro-inventory"},
    )
    assert mint_calls == []
    edges = _field_concept_edges(writes)
    assert edges.get("cmms.spare_parts.reorder_point") == "mro-inventory"


def test_unmapped_column_with_only_unsupported_suggestions_leaves_no_edge_no_mint(monkeypatch):
    """Precedent and affinity don't look at the column. A suggestion the column
    itself doesn't support must not label it: a contact column on a shared
    service-call entity is NOT the 'cost' concept another system mapped there.
    It stays unlabeled (no edge) and, as before, mints nothing."""
    writes, mint_calls = _run(
        monkeypatch, system_id="fsm", schema="field_service",
        mappings=[_col("service_calls", "contact_email"),
                  _col("service_calls", "street_line2")],
        entity_map={"service_calls": "work-order"},
        generic=set(),
        shared={"work-order"},
        precedents={},
        suggestions={
            "contact_email": [{"concept": "cost", "score": 3.0, "reason": "precedent",
                               "column_match": False}],
            "street_line2": [{"concept": "actual-quantity", "score": 3.0,
                              "reason": "precedent", "column_match": False},
                             {"concept": "failure-mode", "score": 2.0,
                              "reason": "affinity", "column_match": False}],
        },
    )
    edges = _field_concept_edges(writes)
    assert "fsm.service_calls.contact_email" not in edges
    assert "fsm.service_calls.street_line2" not in edges
    assert mint_calls == []


def test_unmapped_column_takes_the_best_suggestion_it_supports(monkeypatch):
    """A higher-scored suggestion the column doesn't support never outranks one
    it does: total_cost takes 'cost' (a field cost lists), not the precedent."""
    writes, mint_calls = _run(
        monkeypatch, system_id="fsm", schema="field_service",
        mappings=[_col("service_calls", "total_cost")],
        entity_map={"service_calls": "work-order"},
        generic=set(),
        shared={"work-order"},
        precedents={},
        suggestions={"total_cost": [
            {"concept": "actual-quantity", "score": 3.0, "reason": "precedent",
             "column_match": False},
            {"concept": "cost", "score": 1.0, "reason": "name match", "column_match": True},
        ]},
    )
    assert _field_concept_edges(writes).get("fsm.service_calls.total_cost") == "cost"
    assert mint_calls == []


def test_one_token_concepts_dont_label_compound_columns_end_to_end(monkeypatch):
    """Through the real reuse-first suggestions on a shared entity: cost_center,
    batch_status and shift_notes are not 'cost', 'batch' and 'shift', even with
    those concepts as precedent. estimated_cost and shift_id, fields those
    concepts list, still are."""
    from agents import concept_layer as cl

    precedent = [{"id": c} for c in ("cost", "batch", "shift")]

    def _query(cypher, params=None):
        return precedent if cypher == cl._PRECEDENT_CYPHER else []

    writes, mint_calls = _install_seams(monkeypatch)
    monkeypatch.setattr(onboard, "entity_for_table", lambda _schema, _table: "work-order")
    monkeypatch.setattr(onboard, "is_generic_field", lambda _col: False)
    monkeypatch.setattr(onboard, "entity_is_shared", lambda *_a, **_k: True)
    monkeypatch.setattr(onboard, "reuse_first_suggestions",
                        lambda e, st, col, **_k: cl.reuse_first_suggestions(e, st, col, query=_query))
    monkeypatch.setattr(onboard.catalog, "get", lambda _sid: {"system_type": "UNKNOWN-TYPE"})
    monkeypatch.setattr(onboard.catalog, "get_onboarded_ids", lambda: set())
    monkeypatch.setattr(onboard.catalog, "update_status", lambda *_a, **_k: None)
    columns = ("cost_center", "batch_status", "shift_notes", "estimated_cost", "shift_id")
    onboard._do_provision(ProvisionRequest(
        system_id="fsm", system_name="FSM", schema="field_service",
        connection_type="PostgreSQL",
        mappings=[_col("service_calls", c) for c in columns]))
    edges = _field_concept_edges(writes)
    for column in ("cost_center", "batch_status", "shift_notes"):
        assert f"fsm.service_calls.{column}" not in edges, (column, edges)
    assert edges.get("fsm.service_calls.estimated_cost") == "cost"
    assert edges.get("fsm.service_calls.shift_id") == "shift"
    assert mint_calls == []


def test_unmapped_single_system_entity_leaves_no_edge_no_mint(monkeypatch):
    """An unmapped column on a SINGLE-system entity with no precedent → no edge, no mint."""
    writes, mint_calls = _run(
        monkeypatch,
        mappings=[_col("spare_parts", "lead_time_days")],
        entity_map={"spare_parts": "spare-part"},
        generic=set(),
        shared=set(),  # entity not shared → not earned
        precedents={},
    )
    assert mint_calls == []
    assert "cmms.spare_parts.lead_time_days" not in _field_concept_edges(writes)


def test_llm_mapped_column_writes_edge_as_before(monkeypatch):
    """An LLM-mapped column gets its Field->Concept edge and is NOT routed through the gate."""
    writes, mint_calls = _run(
        monkeypatch,
        mappings=[_col("work_orders", "wo_number", concept="work-order")],
        entity_map={"work_orders": "work-order"},
        generic=set(),
        shared={"work-order"},
        precedents={},
    )
    assert mint_calls == []
    edges = _field_concept_edges(writes)
    assert edges.get("cmms.work_orders.wo_number") == "work-order"


def test_unmapped_excluded_table_leaves_no_edge_no_mint(monkeypatch):
    """An unmapped column on a non-curated/excluded table (entity None) → no edge, no mint."""
    writes, mint_calls = _run(
        monkeypatch,
        mappings=[_col("simulation_clock", "some_col")],
        entity_map={"simulation_clock": None},
        generic=set(),
        shared=set(),
        precedents={},
    )
    assert mint_calls == []
    assert "cmms.simulation_clock.some_col" not in _field_concept_edges(writes)


# --------------------------------------------------------------------------- #
# ADR 0040 — the entity-layer inference is DECOUPLED from the field-mint gate.
# A non-curated table can gain an inferred entity MAPS_TO edge while its columns
# still obey the UNCHANGED asymmetric field-mint gate (which independently reads
# entity_for_table → None → no field mint).
# --------------------------------------------------------------------------- #
_ENTITY_EDGE_FRAGMENT = "MERGE (se)-[:MAPS_TO]->(c)"
_ENTITY_EDGE_MATCH = "(se:SchemaEntry {id: $seid}), (c:Concept {id: $entity})"


def test_noncurated_entity_edge_does_not_trigger_field_mint(monkeypatch):
    """Entity inference writes a MAPS_TO entity edge for a non-curated table, yet the
    UNCHANGED field-mint gate still declines to mint (entity_for_table → None), so an
    unmapped column stays unminted. Proves the two gates are independent (ADR 0040)."""
    writes, mint_calls = _install_seams(monkeypatch)
    # Field-mint gate reads entity_for_table directly → None for this non-curated table.
    _install_concept_layer(monkeypatch, entity_map={"gizmos": None},
                           generic=set(), shared=set(), precedents={})
    # But the ENTITY-layer resolver returns a confident inferred entity.
    monkeypatch.setattr(
        onboard, "_classify_table_entity",
        lambda *_a, **_k: {"entity": "asset", "identity_fields": ["gizmo_id"],
                           "mint": None, "confidence": 0.9},
    )
    monkeypatch.setattr(onboard.catalog, "get", lambda _sid: {"system_type": "OT"})
    monkeypatch.setattr(onboard.catalog, "get_onboarded_ids", lambda: set())
    monkeypatch.setattr(onboard.catalog, "update_status", lambda *_a, **_k: None)
    body = ProvisionRequest(
        system_id="lake", system_name="Lake", schema="lake",
        connection_type="PostgreSQL",
        mappings=[_col("gizmos", "some_metric")],  # unmapped, non-generic column
    )
    onboard._do_provision(body)

    # Entity edge WAS written (inference), targeting the inferred entity.
    entity_edges = [p for stmt, p in writes
                    if _ENTITY_EDGE_FRAGMENT in stmt and _ENTITY_EDGE_MATCH in stmt]
    assert {p["seid"]: p["entity"] for p in entity_edges} == {"lake.gizmos": "asset"}
    # Field-mint gate UNCHANGED: the unmapped column is NOT minted.
    assert mint_calls == []
    assert "lake.gizmos.some_metric" not in _field_concept_edges(writes)


# --------------------------------------------------------------------------- #
# _author_concept_description — fail-soft cosmetic authoring (ADR 0021).
# The description is COSMETIC: on ANY bedrock failure the mint must still succeed,
# so authoring degrades to a clear TEMPLATED string AND logs a warning (a logged
# degradation, not a silent fallback). The happy path returns the bedrock text.
# --------------------------------------------------------------------------- #
import logging


def test_author_concept_description_degrades_to_template_and_logs(monkeypatch, caplog):
    """A raising bedrock_call → templated non-empty string + a logged warning."""
    def _boom(*_a, **_k):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(onboard, "bedrock_call", _boom)
    with caplog.at_level(logging.WARNING, logger="agents.onboard"):
        result = onboard._author_concept_description(
            "cmms", "spare_parts", "lead_time_days", "Lead Time Days",
        )

    assert result == "Field concept for spare_parts.lead_time_days in cmms."
    assert result  # non-empty: the mint still carries a description
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "degradation must be logged loudly, not silent"
    assert "authoring failed" in caplog.text.lower()


def test_author_concept_description_happy_path_returns_bedrock_text(monkeypatch):
    """A successful bedrock_call → its (stripped, bounded) text verbatim."""
    monkeypatch.setattr(
        onboard, "bedrock_call",
        lambda *_a, **_k: "  Days between ordering a spare part and its receipt.  ",
    )
    result = onboard._author_concept_description(
        "cmms", "spare_parts", "lead_time_days", "Lead Time Days",
    )
    assert result == "Days between ordering a spare part and its receipt."
