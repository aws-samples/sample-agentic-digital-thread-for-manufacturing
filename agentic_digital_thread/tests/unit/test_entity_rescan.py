# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for ``agents.entity_rescan.rescan_entities`` (ADR-0040 D6).

Every impure seam (graph query/write, the LLM-backed classifier, the catalog) is
injected, so these run with no AWS / Neptune / LLM. Asserts the endpoint contract:

  * IDEMPOTENCY — a second run over an up-to-date graph writes NOTHING new.
  * per-tier counting (curated / heuristic / llm / none) via the classifier's tier.
  * unknown system → :class:`UnknownSystemError` (the route maps it to 404).
  * minted entities carry onboarding's exact provenance (source='minted',
    layer='entity') — no new review flow.
  * dry_run performs no writes but reports would-be counts.
"""
from __future__ import annotations

import pytest

from agents.entity_rescan import UnknownSystemError, rescan_entities


class _FakeGraph:
    """In-memory stand-in for the two graph reads + the MERGE writer."""

    def __init__(self, schema_rows, table_fields=None):
        # schema_rows: [{"system_id", "table", "entity_edges"}]
        self.schema_rows = schema_rows
        # table_fields: {(sid, tbl): [{"field": ..., "maps": [...]}]}
        self.table_fields = table_fields or {}
        self.writes: list[tuple[str, dict]] = []

    def query(self, cypher, params=None):
        params = params or {}
        if "HAS_FIELD" in cypher:
            return list(self.table_fields.get((params.get("sid"), params.get("tbl")), []))
        return list(self.schema_rows)

    def write(self, cypher, params=None):
        self.writes.append((cypher, params or {}))


def _catalog_get_factory(known: dict):
    return lambda sid: known.get(sid)


CATALOG = {
    "erp-x": {"schema": "erp_data", "connection_type": "PostgreSQL"},
    "athena-wh": {"schema": "", "connection_type": "Athena"},
}


def test_first_run_writes_and_second_run_is_noop():
    """Idempotency: run 1 writes entity+identity edges; run 2 (graph now has the
    edges) writes 0 and reports everything as skipped_existing."""
    graph = _FakeGraph(
        schema_rows=[
            {"system_id": "erp-x", "table": "orders", "entity_edges": 0},
            {"system_id": "erp-x", "table": "invoices", "entity_edges": 1},  # pre-existing
        ],
        table_fields={("erp-x", "orders"): [
            {"field": "order_id", "maps": [{"concept": "work-order", "role": "identifier"}]},
        ]},
    )

    def classify(sid, schema, tbl, columns, mappings, connection_type=""):
        return {"entity": "work-order", "identity_fields": ["order_id"],
                "mint": None, "confidence": 1.0, "tier": "curated"}

    out = rescan_entities("erp-x", query=graph.query, write=graph.write,
                          classify=classify, catalog_get=_catalog_get_factory(CATALOG))
    s = out["systems"]["erp-x"]
    assert s["tables_scanned"] == 2
    assert s["skipped_existing"] == 1        # invoices already had its edge
    assert s["entities_written"] == 1        # orders → work-order
    assert s["identities_written"] == 1      # orders IDENTIFIED_BY order_id
    assert s["tier_counts"] == {"curated": 1}
    assert s["errors"] == []
    # One MAPS_TO + one IDENTIFIED_BY MERGE, no mint node (curated pick).
    assert len(graph.writes) == 2
    assert all("MERGE" in c for c, _ in graph.writes)

    # ---- run 2: the graph now carries the edge for orders too ----
    graph2 = _FakeGraph(schema_rows=[
        {"system_id": "erp-x", "table": "orders", "entity_edges": 1},
        {"system_id": "erp-x", "table": "invoices", "entity_edges": 1},
    ])
    out2 = rescan_entities("erp-x", query=graph2.query, write=graph2.write,
                           classify=lambda *a, **k: pytest.fail("must not re-classify"),
                           catalog_get=_catalog_get_factory(CATALOG))
    s2 = out2["systems"]["erp-x"]
    assert s2["entities_written"] == 0
    assert s2["identities_written"] == 0
    assert s2["skipped_existing"] == 2
    assert graph2.writes == []               # NOTHING written on the re-run


def test_per_tier_counting_across_tables():
    graph = _FakeGraph(schema_rows=[
        {"system_id": "athena-wh", "table": "a_curated", "entity_edges": 0},
        {"system_id": "athena-wh", "table": "b_heur", "entity_edges": 0},
        {"system_id": "athena-wh", "table": "c_llm", "entity_edges": 0},
        {"system_id": "athena-wh", "table": "d_none", "entity_edges": 0},
    ])
    tiers = {"a_curated": ("curated", "asset"), "b_heur": ("heuristic", "asset"),
             "c_llm": ("llm", "work-order"), "d_none": ("none", None)}

    def classify(sid, schema, tbl, columns, mappings, connection_type=""):
        tier, entity = tiers[tbl]
        return {"entity": entity, "identity_fields": [], "mint": None,
                "confidence": 0.9, "tier": tier}

    out = rescan_entities("athena-wh", query=graph.query, write=graph.write,
                          classify=classify, catalog_get=_catalog_get_factory(CATALOG))
    s = out["systems"]["athena-wh"]
    assert s["tier_counts"] == {"curated": 1, "heuristic": 1, "llm": 1, "none": 1}
    assert s["entities_written"] == 3        # d_none fails soft → no edge
    assert s["errors"] == []


def test_unknown_system_raises_clean_error():
    graph = _FakeGraph(schema_rows=[])
    with pytest.raises(UnknownSystemError):
        rescan_entities("does-not-exist", query=graph.query, write=graph.write,
                        classify=lambda *a, **k: None,
                        catalog_get=_catalog_get_factory(CATALOG))
    assert graph.writes == []


def test_known_system_with_no_graph_rows_returns_empty_summary():
    graph = _FakeGraph(schema_rows=[])
    out = rescan_entities("erp-x", query=graph.query, write=graph.write,
                          classify=lambda *a, **k: pytest.fail("nothing to classify"),
                          catalog_get=_catalog_get_factory(CATALOG))
    assert out["systems"]["erp-x"] == {
        "tables_scanned": 0, "entities_written": 0, "identities_written": 0,
        "skipped_existing": 0, "tier_counts": {}, "errors": [],
    }


def test_minted_entity_carries_onboarding_provenance():
    """An LLM mint MERGEs the Concept with the SAME provenance marker onboarding's
    provision write uses: source='minted', layer='entity'."""
    graph = _FakeGraph(schema_rows=[
        {"system_id": "athena-wh", "table": "segment_records", "entity_edges": 0},
    ])

    def classify(sid, schema, tbl, columns, mappings, connection_type=""):
        return {"entity": "segment-records", "identity_fields": ["record_id"],
                "mint": {"id": "segment-records", "name": "Segment Records",
                         "description": "A recorded production segment."},
                "confidence": 0.9, "tier": "llm"}

    out = rescan_entities("athena-wh", query=graph.query, write=graph.write,
                          classify=classify, catalog_get=_catalog_get_factory(CATALOG))
    mint_writes = [(c, p) for c, p in graph.writes if "c.source = 'minted'" in c]
    assert len(mint_writes) == 1
    cypher, params = mint_writes[0]
    assert "c.layer = 'entity'" in cypher
    assert "ON CREATE SET" in cypher          # never overwrites an existing concept
    assert params["cid"] == "segment-records"
    s = out["systems"]["athena-wh"]
    assert s["entities_written"] == 1 and s["identities_written"] == 1


def test_all_systems_sweep_and_dry_run_writes_nothing():
    graph = _FakeGraph(schema_rows=[
        {"system_id": "erp-x", "table": "orders", "entity_edges": 0},
        {"system_id": "athena-wh", "table": "records", "entity_edges": 0},
    ])

    def classify(sid, schema, tbl, columns, mappings, connection_type=""):
        return {"entity": "work-order", "identity_fields": ["id1"],
                "mint": None, "confidence": 1.0, "tier": "curated"}

    out = rescan_entities(None, dry_run=True, query=graph.query, write=graph.write,
                          classify=classify, catalog_get=_catalog_get_factory(CATALOG))
    assert out["dry_run"] is True
    assert set(out["systems"]) == {"erp-x", "athena-wh"}
    # Would-be counts are reported but NO write happened.
    assert out["systems"]["erp-x"]["entities_written"] == 1
    assert graph.writes == []


def test_per_table_classify_error_is_fail_soft_and_reported():
    graph = _FakeGraph(schema_rows=[
        {"system_id": "erp-x", "table": "bad", "entity_edges": 0},
        {"system_id": "erp-x", "table": "good", "entity_edges": 0},
    ])

    def classify(sid, schema, tbl, columns, mappings, connection_type=""):
        if tbl == "bad":
            raise RuntimeError("curated vocabulary gap")
        return {"entity": "asset", "identity_fields": [], "mint": None,
                "confidence": 1.0, "tier": "curated"}

    out = rescan_entities("erp-x", query=graph.query, write=graph.write,
                          classify=classify, catalog_get=_catalog_get_factory(CATALOG))
    s = out["systems"]["erp-x"]
    assert s["entities_written"] == 1        # 'good' still processed
    assert len(s["errors"]) == 1 and "bad" in s["errors"][0]


def test_script_is_a_thin_wrapper_over_the_library():
    """The CLI script re-exports the library core — no duplicated logic."""
    import agents.entity_rescan as lib
    from scripts import backfill_entity_identity as cli
    assert cli.rescan_entities is lib.rescan_entities
    assert cli.tables_missing_entity_edge is lib.tables_missing_entity_edge
    assert cli.restrict_to_systems is lib.restrict_to_systems
