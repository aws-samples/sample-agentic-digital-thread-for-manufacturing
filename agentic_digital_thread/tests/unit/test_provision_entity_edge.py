# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the S5.2 Wave 3.1 table→entity edge (ADR 0021).

Provisioning a system must emit exactly one ``(SchemaEntry)-[:MAPS_TO]->(Concept)``
edge per NON-excluded table, with the entity drawn deterministically from the curated
``TABLE_ENTITY_MAP`` and guaranteed to be entity-capable (``∈ ENTITY_CONCEPT_IDS``).
Excluded tables get NO edge; an unmapped, non-excluded DOMAIN table in a curated schema
fails loud (totality).

Pure in-process: ``_do_provision`` is driven directly with every impure seam
monkeypatched at the module-global level — no AWS, no Neptune, no Postgres, no LLM. The
graph writer is replaced by a recorder that captures ``(cypher, params)`` tuples so the
emitted edges can be asserted exactly.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents.catalog import ENTITY_CONCEPT_IDS, catalog
from agents.models import ProvisionRequest

import pytest as _pytest_seed


@_pytest_seed.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Base mode ships an EMPTY live catalog (ADR-0030) and the demo-seed machinery
    has been DELETED from production (ADR-0035). These tests provision demo systems
    BY NAME, so temporarily seed the shared catalog from the test-local
    ``DEMO_SYSTEMS`` fixture, then restore the empty base-mode catalog."""
    from agents.catalog import catalog as _cat
    try:
        from unit._demo_catalog import DEMO_SYSTEMS
    except ImportError:  # pragma: no cover - alternate invocation path
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from _demo_catalog import DEMO_SYSTEMS
    _cat._systems = [dict(s) for s in DEMO_SYSTEMS]
    try:
        yield
    finally:
        _cat._systems = []


# --------------------------------------------------------------------------- #
# Fakes / seams
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    """Minimal agent-manager stand-in: start_agent returns a stub MCP url."""

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    """boto3 SSM stub whose get_parameter raises so _do_provision takes the
    deterministic region-derived default model (no network, no creds)."""

    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    """Neutralize every impure seam in the PostgreSQL provision path and return the
    list that records every ``neptune_write(cypher, params)`` call."""
    writes: list[tuple[str, dict]] = []

    def _record_write(cypher, params=None):
        writes.append((cypher, params or {}))

    monkeypatch.setattr(onboard, "neptune_write", _record_write)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub system prompt")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    # SSM (per-system agent_model lookup) — keep it offline.
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    # Self-learning re-apply reads DynamoDB — stub to empty.
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    return writes


def _mapping(table: str, concept: str) -> dict:
    """A minimal column→concept mapping row for one table (drives tables_seen)."""
    return {
        "table": table,
        "field": f"{table}.id_col",
        "concept": concept,
        "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.9}],
    }


def _run_provision(monkeypatch, *, system_id, schema, tables):
    """Drive ``_do_provision`` for a PostgreSQL system and return recorded writes.

    Restores the catalog status afterward so the shared in-memory catalog is not
    mutated across tests.
    """
    writes = _install_seams(monkeypatch)
    entry = catalog.get(system_id)
    assert entry is not None, f"expected seed system '{system_id}'"
    prior_status = entry["status"]
    body = ProvisionRequest(
        system_id=system_id,
        system_name=system_id.upper(),
        schema=schema,
        connection_type="PostgreSQL",
        mappings=[_mapping(t, c) for t, c in tables],
    )
    try:
        onboard._do_provision(body)
    finally:
        catalog.update_status(system_id, prior_status)
    return writes


# --------------------------------------------------------------------------- #
# Helpers to isolate the table→entity edge writes
# --------------------------------------------------------------------------- #
_ENTITY_EDGE_FRAGMENT = "MERGE (se)-[:MAPS_TO]->(c)"
_ENTITY_EDGE_MATCH = "(se:SchemaEntry {id: $seid}), (c:Concept {id: $entity})"


def _entity_edges(writes) -> list[dict]:
    """Params of every emitted SchemaEntry-[:MAPS_TO]->Concept (table→entity) edge."""
    return [
        p
        for stmt, p in writes
        if _ENTITY_EDGE_FRAGMENT in stmt and _ENTITY_EDGE_MATCH in stmt
    ]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_one_entity_edge_per_table_with_correct_entity(monkeypatch):
    """ERP provision emits exactly one entity edge per non-excluded table, each with the
    curated entity and entity ∈ ENTITY_CONCEPT_IDS. Spot-checks customer_orders→sales-order
    and shipments→shipment. product_site_routing (excluded) gets NO edge."""
    writes = _run_provision(
        monkeypatch,
        system_id="erp",
        schema="erp_data",
        tables=[
            ("customer_orders", "sales-order"),
            ("shipments", "shipment"),
            ("product_site_routing", "product"),  # excluded → no entity edge
        ],
    )
    edges = _entity_edges(writes)
    # Excluded table contributes no edge → exactly 2 edges for 3 provisioned tables.
    assert len(edges) == 2
    by_seid = {p["seid"]: p["entity"] for p in edges}
    assert by_seid == {
        "erp.customer_orders": "sales-order",
        "erp.shipments": "shipment",
    }
    assert "erp.product_site_routing" not in by_seid
    for p in edges:
        assert p["entity"] in ENTITY_CONCEPT_IDS


def test_cmms_spot_checks(monkeypatch):
    """CMMS: assets→asset and work_orders→work-order, one edge each, entity-capable."""
    writes = _run_provision(
        monkeypatch,
        system_id="cmms",
        schema="cmms_maintenance",
        tables=[("assets", "equipment-id"), ("work_orders", "work-order")],
    )
    by_seid = {p["seid"]: p["entity"] for p in _entity_edges(writes)}
    assert by_seid == {
        "cmms.assets": "asset",
        "cmms.work_orders": "work-order",
    }
    assert all(e in ENTITY_CONCEPT_IDS for e in by_seid.values())


def test_mes_spot_checks(monkeypatch):
    """MES: work_orders→work-order and production_lines→production-line (dimension entity)."""
    writes = _run_provision(
        monkeypatch,
        system_id="mes-mil",
        schema="mes_milwaukee",
        tables=[("work_orders", "work-order"), ("production_lines", "production-line")],
    )
    by_seid = {p["seid"]: p["entity"] for p in _entity_edges(writes)}
    assert by_seid == {
        "mes-mil.work_orders": "work-order",
        "mes-mil.production_lines": "production-line",
    }
    assert all(e in ENTITY_CONCEPT_IDS for e in by_seid.values())


def test_excluded_table_emits_no_entity_edge(monkeypatch):
    """An excluded table (product_site_routing) provisions cleanly with NO entity edge."""
    writes = _run_provision(
        monkeypatch,
        system_id="erp",
        schema="erp_data",
        tables=[("product_site_routing", "product")],
    )
    assert _entity_edges(writes) == []
    # The SchemaEntry node itself is still written (additive change, not a removal).
    assert any("MERGE (se:SchemaEntry {id: $seid})" in stmt for stmt, _ in writes)


def test_every_site_mes_view_resolves_in_every_curated_mes_schema():
    """Every view a site MES schema exposes (``SITE_MES_VIEW_NAMES``, built by db-init
    on every deploy) has a curated entity, so provisioning a curated MES never trips
    the fail-loud totality. Regression: ``production_runs`` joined the site views
    without a map entry, and every mes-mil/mes-det/mes-cr provision (the workshop
    seed included) raised ValueError."""
    from agents.concept_tagging import ENTITY_MAPPED_SCHEMAS, entity_for_table
    from agents.demo.site_views import SITE_MES_VIEW_NAMES

    mes_schemas = sorted(s for s in ENTITY_MAPPED_SCHEMAS if s.startswith("mes_"))
    assert mes_schemas, "no curated MES schema found"
    for schema in mes_schemas:
        for view in SITE_MES_VIEW_NAMES:
            assert entity_for_table(schema, view) in ENTITY_CONCEPT_IDS, f"{schema}.{view}"


def test_unmapped_domain_table_fails_loud(monkeypatch):
    """A discovered DOMAIN table with no map entry and not excluded raises (totality),
    naming the offending schema.table."""
    with pytest.raises(ValueError) as exc:
        _run_provision(
            monkeypatch,
            system_id="cmms",
            schema="cmms_maintenance",
            tables=[("gremlins", "equipment-id")],
        )
    msg = str(exc.value)
    assert "cmms_maintenance.gremlins" in msg


# --------------------------------------------------------------------------- #
# ADR 0040 — dynamic entity + identity for NON-curated systems
# --------------------------------------------------------------------------- #
_IDENTIFIED_BY_FRAGMENT = "MERGE (se)-[:IDENTIFIED_BY]->(f)"
_MINT_CONCEPT_FRAGMENT = "c.source = 'minted', c.layer = 'entity'"


def _identified_by_edges(writes) -> list[dict]:
    """Params of every SchemaEntry-[:IDENTIFIED_BY]->Field identity-key edge."""
    return [p for stmt, p in writes if _IDENTIFIED_BY_FRAGMENT in stmt]


def _minted_entity_concepts(writes) -> list[dict]:
    """Params of every minted-entity Concept MERGE (source='minted', layer='entity')."""
    return [p for stmt, p in writes if _MINT_CONCEPT_FRAGMENT in stmt]


def _run_noncurated(monkeypatch, *, system_id, schema, mappings, classify=None,
                    table_entities=None, system_type="OT"):
    """Drive ``_do_provision`` for a NON-curated system (schema outside the curated
    entity vocabulary → entity_for_table returns None, no raise). The catalog is
    stubbed (this system is not a seeded demo system) and ``_classify_table_entity``
    may be replaced to exercise the inferred/mint/fail-soft branches deterministically.
    """
    writes = _install_seams(monkeypatch)
    monkeypatch.setattr(onboard.catalog, "get", lambda _sid: {"system_type": system_type})
    monkeypatch.setattr(onboard.catalog, "get_onboarded_ids", lambda: set())
    monkeypatch.setattr(onboard.catalog, "update_status", lambda *_a, **_k: None)
    if classify is not None:
        monkeypatch.setattr(onboard, "_classify_table_entity", classify)
    body = ProvisionRequest(
        system_id=system_id,
        system_name=system_id.upper(),
        schema=schema,
        connection_type="PostgreSQL",
        mappings=mappings,
        table_entities=table_entities or [],
    )
    onboard._do_provision(body)
    return writes


def test_noncurated_table_gets_inferred_entity_and_identity_edges(monkeypatch):
    """A non-curated table with a confident inference writes BOTH the entity MAPS_TO edge
    and the IDENTIFIED_BY identity-key edge (net-new for non-curated systems)."""
    def _fake_classify(system_id, schema, table, columns, mappings, samples=None,
                       *, override=None, connection_type=""):
        return {"entity": "asset", "identity_fields": ["barcode_id"],
                "mint": None, "confidence": 0.9}

    writes = _run_noncurated(
        monkeypatch,
        system_id="lake-athena",
        schema="segment_lake",  # NOT a curated entity schema
        mappings=[_mapping("segment_record_details_raw", "equipment-id")],
        classify=_fake_classify,
    )
    edges = _entity_edges(writes)
    assert {p["seid"]: p["entity"] for p in edges} == {
        "lake-athena.segment_record_details_raw": "asset",
    }
    assert all(e in ENTITY_CONCEPT_IDS for e in (p["entity"] for p in edges))
    ident = _identified_by_edges(writes)
    assert {(p["seid"], p["fid"]) for p in ident} == {
        ("lake-athena.segment_record_details_raw",
         "lake-athena.segment_record_details_raw.barcode_id"),
    }
    # Fail-soft is NOT triggered here → no minted-entity concept node.
    assert _minted_entity_concepts(writes) == []


def test_noncurated_entity_mint_merges_minted_concept_then_edge(monkeypatch):
    """A __MINT__ result MERGEs a NEW entity Concept (source='minted', layer='entity')
    BEFORE the entity edge, and the edge targets the minted id."""
    def _fake_classify(system_id, schema, table, columns, mappings, samples=None,
                       *, override=None, connection_type=""):
        return {"entity": "segment-record", "identity_fields": ["record_id"],
                "mint": {"id": "segment-record", "name": "Segment Record",
                         "description": "A recorded production segment."},
                "confidence": 0.85}

    writes = _run_noncurated(
        monkeypatch,
        system_id="lake-athena",
        schema="segment_lake",
        mappings=[_mapping("segment_record_details_raw", "needs_review")],
        classify=_fake_classify,
    )
    minted = _minted_entity_concepts(writes)
    assert len(minted) == 1
    assert minted[0]["cid"] == "segment-record"
    assert minted[0]["name"] == "Segment Record"
    assert minted[0]["desc"] == "A recorded production segment."
    # The entity edge points at the minted concept id.
    assert {p["seid"]: p["entity"] for p in _entity_edges(writes)} == {
        "lake-athena.segment_record_details_raw": "segment-record",
    }
    # MERGE of the minted Concept precedes the edge MATCH (target must pre-exist).
    stmts = [stmt for stmt, _ in writes]
    mint_i = next(i for i, s in enumerate(stmts) if _MINT_CONCEPT_FRAGMENT in s)
    edge_i = next(i for i, s in enumerate(stmts) if _ENTITY_EDGE_FRAGMENT in s)
    assert mint_i < edge_i


def test_noncurated_failsoft_writes_no_entity_edge_and_does_not_raise(monkeypatch):
    """Fail-soft: inference returns entity=None → NO entity edge, NO IDENTIFIED_BY edge,
    NO minted concept — but the SchemaEntry + Field nodes are still written (no raise)."""
    def _fake_classify(system_id, schema, table, columns, mappings, samples=None,
                       *, override=None, connection_type=""):
        return {"entity": None, "identity_fields": [], "mint": None, "confidence": 0.3}

    writes = _run_noncurated(
        monkeypatch,
        system_id="lake-athena",
        schema="segment_lake",
        mappings=[_mapping("mystery_table", "needs_review")],
        classify=_fake_classify,
    )
    assert _entity_edges(writes) == []
    assert _identified_by_edges(writes) == []
    assert _minted_entity_concepts(writes) == []
    # SchemaEntry + Field nodes still written (fail-soft, not fail-loud).
    assert any("MERGE (se:SchemaEntry {id: $seid})" in stmt for stmt, _ in writes)
    assert any("MERGE (f:Field {id: $fid})" in stmt for stmt, _ in writes)


def test_noncurated_user_override_wins_over_inference(monkeypatch):
    """A UI-confirmed override in body.table_entities is honored VERBATIM (real
    _classify_table_entity, no stub): its entity + identity fields are written even
    though the schema is non-curated and no inference stub is installed."""
    writes = _run_noncurated(
        monkeypatch,
        system_id="lake-athena",
        schema="segment_lake",
        mappings=[_mapping("segment_record_details_raw", "needs_review")],
        table_entities=[{
            "table": "segment_record_details_raw",
            "entity": "work-order",
            "identity_fields": ["wo_ref"],
            "mint": None,
        }],
    )
    assert {p["seid"]: p["entity"] for p in _entity_edges(writes)} == {
        "lake-athena.segment_record_details_raw": "work-order",
    }
    assert {(p["seid"], p["fid"]) for p in _identified_by_edges(writes)} == {
        ("lake-athena.segment_record_details_raw",
         "lake-athena.segment_record_details_raw.wo_ref"),
    }


# --------------------------------------------------------------------------- #
# ADR 0040 — _classify_table_entity control flow (direct, offline, LLM stubbed)
# --------------------------------------------------------------------------- #
def _classify(monkeypatch, **kwargs):
    """Call _classify_table_entity with a stubbed catalog system_type. bedrock_call is
    the ONLY LLM seam and is left to the caller to patch per-test."""
    monkeypatch.setattr(onboard.catalog, "get",
                        lambda _sid: {"system_type": kwargs.pop("system_type", "OT")})
    return onboard._classify_table_entity(**kwargs)


def test_classify_curated_fast_path_is_verbatim(monkeypatch):
    """Curated (schema, table) resolves via entity_for_table VERBATIM — no LLM."""
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: pytest.fail("LLM must not be called on the curated fast-path"))
    out = _classify(monkeypatch, system_id="cmms", schema="cmms_maintenance",
                    table="assets", columns=["asset_id"],
                    mappings=[{"field": "asset_id", "column": "asset_id",
                               "mappings": [{"concept": "equipment-id", "role": "identifier"}]}])
    assert out["entity"] == "asset"
    assert out["mint"] is None
    assert out["identity_fields"] == ["asset_id"]


def test_classify_excluded_table_never_infers(monkeypatch):
    """An excluded infra table (simulation_*) is never inferred even off a curated schema."""
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: pytest.fail("LLM must not be called for an excluded table"))
    out = _classify(monkeypatch, system_id="cmms", schema="cmms_maintenance",
                    table="simulation_clock", columns=["tick"], mappings=[])
    assert out == {"entity": None, "identity_fields": [], "mint": None,
                   "confidence": 0.0, "tier": "excluded"}


def test_classify_heuristic_precedent_wins_without_llm(monkeypatch):
    """A high-scoring entity_suggestions candidate is taken WITHOUT an LLM call."""
    monkeypatch.setattr(onboard, "entity_suggestions",
                        lambda *_a, **_k: [{"entity": "asset", "score": 3.0, "reason": "precedent"}])
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: pytest.fail("LLM must not be called when the heuristic clears the bar"))
    out = _classify(monkeypatch, system_id="lake", schema="lake", table="gizmos",
                    columns=["gizmo_id"],
                    mappings=[{"field": "gizmo_id", "column": "gizmo_id",
                               "mappings": [{"concept": "equipment-id", "role": "identifier"}]}])
    assert out["entity"] == "asset"
    assert out["mint"] is None
    assert out["identity_fields"] == ["gizmo_id"]


def test_classify_llm_mint_authors_new_entity_at_high_confidence(monkeypatch):
    """A confident __MINT__ authors a NEW entity concept (id/name/description)."""
    monkeypatch.setattr(onboard, "entity_suggestions", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: '{"entity": "__MINT__", "confidence": 0.9}')
    monkeypatch.setattr(onboard, "_author_concept_description",
                        lambda *_a, **_k: "A recorded production segment.")
    out = _classify(monkeypatch, system_id="lake", schema="lake",
                    table="segment_records", columns=["record_id"],
                    mappings=[{"field": "record_id", "column": "record_id",
                               "mappings": [{"concept": "needs_review", "role": "correlate"}]}])
    assert out["entity"] == "segment-records"
    assert out["mint"] == {"id": "segment-records", "name": "Segment Records",
                           "description": "A recorded production segment."}
    assert out["identity_fields"] == ["record_id"]


def test_classify_llm_existing_entity_at_high_confidence(monkeypatch):
    """A confident LLM pick of an EXISTING entity id is reused (no mint)."""
    monkeypatch.setattr(onboard, "entity_suggestions", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: '{"entity": "work-order", "confidence": 0.85}')
    out = _classify(monkeypatch, system_id="lake", schema="lake", table="jobs",
                    columns=["job_no"],
                    mappings=[{"field": "job_no", "column": "job_no",
                               "mappings": [{"concept": "needs_review", "role": "correlate"}]}])
    assert out["entity"] == "work-order"
    assert out["mint"] is None


def test_classify_llm_low_confidence_fails_soft(monkeypatch):
    """A low-confidence LLM answer FAILS SOFT — entity None, no mint, no raise."""
    monkeypatch.setattr(onboard, "entity_suggestions", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: '{"entity": "work-order", "confidence": 0.4}')
    out = _classify(monkeypatch, system_id="lake", schema="lake", table="junk",
                    columns=["c"], mappings=[])
    assert out["entity"] is None
    assert out["mint"] is None


def test_classify_llm_unparseable_fails_soft(monkeypatch):
    """A raising / unparseable LLM response FAILS SOFT (never propagates)."""
    monkeypatch.setattr(onboard, "entity_suggestions", lambda *_a, **_k: [])

    def _boom(*_a, **_k):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(onboard, "bedrock_call", _boom)
    out = _classify(monkeypatch, system_id="lake", schema="lake", table="junk",
                    columns=["c"], mappings=[])
    assert out == {"entity": None, "identity_fields": [], "mint": None,
                   "confidence": 0.0, "tier": "none"}


def test_classify_override_wins_over_curated_and_inference(monkeypatch):
    """An explicit override beats even the curated fast-path (no LLM, verbatim entity)."""
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: pytest.fail("LLM must not be called when an override is present"))
    out = _classify(monkeypatch, system_id="cmms", schema="cmms_maintenance",
                    table="assets", columns=["asset_id"], mappings=[],
                    override={"table": "assets", "entity": "spare-part",
                              "identity_fields": ["part_no"], "mint": None})
    assert out["entity"] == "spare-part"
    assert out["identity_fields"] == ["part_no"]
    assert out["mint"] is None
