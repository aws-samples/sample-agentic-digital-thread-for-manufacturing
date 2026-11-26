# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""FIX 3: ``get_system_schema`` must surface an onboarded system's REAL table name.

For an Athena system whose SchemaEntry nodes were written under their real table
names (the goal of the ``_mapping_table_field`` qualified-name fix in
``onboard.py``), ``get_system_schema`` groups by ``se.name`` and returns
``{table_name: {columns, concept_map}}``. This test stubs the Neptune read so the
whole thing is offline (the graph is VPC-private per ADR-0024) and asserts the real
table name is the top-level key — so the orchestrator does NOT have to fall back to
athena_list_tables to discover it.
"""
from __future__ import annotations

import json

import agents.graph_tools as graph_tools


def test_get_system_schema_surfaces_real_table_name(monkeypatch):
    """A system whose SchemaEntry ``name`` is the real table 'example_segments' is
    surfaced by get_system_schema under that literal table name."""
    rows = [
        {"table_name": "example_segments", "field_name": "segment_id",
         "concept": "batch", "role": "identifier", "disambiguation": None},
        {"table_name": "example_segments", "field_name": "serial_number",
         "concept": "equipment-id", "role": "identifier", "disambiguation": None},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)

    out = json.loads(graph_tools.get_system_schema("example-athena-x"))

    # The REAL table name is the surfaced key — not a database/schema name.
    assert "example_segments" in out
    assert set(out["example_segments"]["columns"]) == {"segment_id", "serial_number"}
    assert out["example_segments"]["concept_map"]["segment_id"]["concept"] == "batch"


def test_get_system_schema_surfaces_partition_keys(monkeypatch):
    """ROOT FIX: get_system_schema flags a table's PARTITION columns (f.is_partition)
    so the orchestrator filters on them FIRST to bound scan cost. The exact partition
    column name is surfaced in a per-table ``partition_keys`` list."""
    rows = [
        {"table_name": "example_segments", "field_name": "id",
         "concept": "batch", "role": "identifier", "disambiguation": None,
         "is_partition": False},
        {"table_name": "example_segments", "field_name": "ingest_date",
         "concept": None, "role": None, "disambiguation": None,
         "is_partition": True},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)

    out = json.loads(graph_tools.get_system_schema("example-athena-x"))

    assert set(out["example_segments"]["columns"]) == {"id", "ingest_date"}
    assert out["example_segments"]["partition_keys"] == ["ingest_date"]


def test_get_system_schema_no_partition_keys_key_when_none(monkeypatch):
    """A table with no partition columns carries NO ``partition_keys`` key (kept lean)."""
    rows = [
        {"table_name": "t", "field_name": "a", "concept": None, "role": None,
         "disambiguation": None, "is_partition": False},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)
    out = json.loads(graph_tools.get_system_schema("sys"))
    assert "partition_keys" not in out["t"]


def test_get_system_schema_no_schema_returns_error(monkeypatch):
    """An un-onboarded system (no rows) returns a structured error envelope (which the
    summarizer flags), not an empty/mislabeled success."""
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: [])
    out = json.loads(graph_tools.get_system_schema("nope"))
    assert "error" in out


# --------------------------------------------------------------------------- #
# FIX 1 (BETTER-INFO — types): surface each column's DATA TYPE alongside its name
# so the agent can CAST a timestamp/date column when comparing to a string literal
# (the TYPE_MISMATCH residual) instead of guessing that the types are compatible.
# --------------------------------------------------------------------------- #
def test_get_system_schema_surfaces_column_data_types(monkeypatch):
    """A system whose Field nodes carry ``f.data_type`` (persisted from
    information_schema) surfaces a per-table ``column_types`` map so the agent knows
    e.g. ``started_timestamp: timestamp`` and casts appropriately."""
    rows = [
        {"table_name": "example_segments", "field_name": "id",
         "concept": "batch", "role": "identifier", "disambiguation": None,
         "is_partition": False, "data_type": "varchar"},
        {"table_name": "example_segments", "field_name": "started_timestamp",
         "concept": None, "role": None, "disambiguation": None,
         "is_partition": False, "data_type": "timestamp(6)"},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)

    out = json.loads(graph_tools.get_system_schema("example-athena-x"))

    # Column names still surfaced (backward-compatible).
    assert set(out["example_segments"]["columns"]) == {"id", "started_timestamp"}
    # NEW: each column's data type is surfaced so the agent can CAST correctly.
    assert out["example_segments"]["column_types"]["started_timestamp"] == "timestamp(6)"
    assert out["example_segments"]["column_types"]["id"] == "varchar"


def test_get_system_schema_surfaces_full_nested_leaves_with_types(monkeypatch):
    """FIX 2 (BETTER-INFO — complete leaves): the FULL set of real nested-struct leaf
    columns (dotted paths) is surfaced with types, and a sibling leaf that does NOT
    exist is absent — so the agent references only the leaves that really exist
    instead of inventing e.g. ``state_context_map.failed.code``."""
    rows = [
        {"table_name": "segments", "field_name": "state_context_map.started.code",
         "concept": None, "role": None, "disambiguation": None,
         "is_partition": False, "data_type": "integer"},
        {"table_name": "segments", "field_name": "state_context_map.completed.code",
         "concept": None, "role": None, "disambiguation": None,
         "is_partition": False, "data_type": "integer"},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)

    out = json.loads(graph_tools.get_system_schema("example-athena-x"))

    cols = set(out["segments"]["columns"])
    assert "state_context_map.started.code" in cols
    assert "state_context_map.completed.code" in cols
    # A leaf that does NOT exist is NOT advertised.
    assert "state_context_map.failed.code" not in cols
    # Types surfaced for the real leaves.
    types = out["segments"]["column_types"]
    assert types["state_context_map.started.code"] == "integer"
    assert types["state_context_map.completed.code"] == "integer"


def test_get_system_schema_no_column_types_key_when_absent(monkeypatch):
    """A system with no persisted data types (older onboarding / non-Athena) carries
    NO ``column_types`` key — kept lean and backward-compatible."""
    rows = [
        {"table_name": "t", "field_name": "a", "concept": None, "role": None,
         "disambiguation": None, "is_partition": False, "data_type": None},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)
    out = json.loads(graph_tools.get_system_schema("sys"))
    assert "column_types" not in out["t"]


def test_get_system_schema_renders_discovery_block_when_recorded(monkeypatch):
    """ADR-0077 D3: a table that recorded partial discovery SAYS so."""
    rows = [
        {"table_name": "work_orders", "field_name": "id", "data_type": "integer",
         "discovery_complete": False, "discovered_columns": 2},
        {"table_name": "work_orders", "field_name": "status", "data_type": "text",
         "discovery_complete": False, "discovered_columns": 2},
        {"table_name": "segment_events", "field_name": "id",
         "discovery_complete": True, "discovered_columns": 1},
    ]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)

    out = json.loads(graph_tools.get_system_schema("erp"))

    assert out["work_orders"]["discovery"] == {"complete": False, "discovered_columns": 2}
    assert out["segment_events"]["discovery"] == {"complete": True, "discovered_columns": 1}


def test_get_system_schema_without_the_fact_is_byte_identical(monkeypatch):
    """Absent = unknown: no fabricated ``discovery`` block, no extra keys."""
    rows = [{"table_name": "work_orders", "field_name": "id", "data_type": "integer",
             "discovery_complete": None, "discovered_columns": None}]
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: rows)

    out = json.loads(graph_tools.get_system_schema("erp"))

    assert set(out["work_orders"]) == {"columns", "concept_map", "relation_kind",
                                       "column_types"}


def test_schema_query_projects_the_discovery_props(monkeypatch):
    seen: list = []
    monkeypatch.setattr(graph_tools, "neptune_query",
                        lambda cypher, params=None: seen.append(cypher) or [])
    graph_tools._schema_query("erp")
    assert "se.discovery_complete AS discovery_complete" in seen[0]
    assert "se.discovered_columns AS discovered_columns" in seen[0]
