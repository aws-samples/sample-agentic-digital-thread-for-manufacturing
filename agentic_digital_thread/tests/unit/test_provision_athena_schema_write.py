# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for the provision schema-write against degraded connector
mapping shapes (B1: Athena "No schema found").

The Amazon Athena onboarding path was observed to emit column→concept mappings
that carry the column under a dotted ``field`` / ``column_name`` WITHOUT a separate
``table`` key. The old schema-write derived an empty table, wrote 0 ``SchemaEntry``
and 0 ``Field`` nodes, and ``get_system_schema`` reported "No schema found" for the
whole system. These tests exercise the REAL ``_do_provision`` schema-write (every
impure seam recorded/stubbed — no AWS/Neptune/Postgres/LLM) and assert that
``SchemaEntry`` + ``Field`` + ``HAS_FIELD`` nodes ARE written for such shapes.

Also unit-tests the pure ``_mapping_table_field`` locator helper directly.
"""
from __future__ import annotations

import agents.onboard as onboard
from agents.connection_types import ATHENA
from agents.models import ProvisionRequest


# --------------------------------------------------------------------------- #
# Pure helper: _mapping_table_field
# --------------------------------------------------------------------------- #
def test_mapping_table_field_canonical_shape():
    """Canonical (table + dotted field): returns the explicit table and last segment."""
    t, f = onboard._mapping_table_field(
        {"table": "orders", "field": "orders.serial_number"}, default_table="db"
    )
    assert (t, f) == ("orders", "serial_number")


def test_mapping_table_field_recovers_table_from_dotted_field():
    """No ``table`` key but a dotted ``field`` → recover the REAL table from the prefix
    (lossless — does NOT fall back to default_table)."""
    t, f = onboard._mapping_table_field(
        {"field": "orders.serial_number"}, default_table="db"
    )
    assert (t, f) == ("orders", "serial_number")


def test_mapping_table_field_recovers_table_from_qualified_field():
    """FIX 3: a database/catalog-qualified dotted ``field`` (``db.table.column``, as
    Athena/Trino identifiers are commonly written) recovers the REAL table — the
    second-to-last segment — NOT the database (first segment). The old code returned
    the database name, collapsing the real table into the ``default_table`` fallback
    so ``get_system_schema`` never surfaced the real table name."""
    t, f = onboard._mapping_table_field(
        {"field": "example_athena_db.example_segments.segment_id"}, default_table="example_athena_db"
    )
    assert (t, f) == ("example_segments", "segment_id")
    # Catalog-qualified (four-part) resolves the same way.
    t2, f2 = onboard._mapping_table_field(
        {"field": "awsdatacatalog.example_athena_db.example_segments.segment_id"},
        default_table="example_athena_db",
    )
    assert (t2, f2) == ("example_segments", "segment_id")


def test_mapping_table_field_column_name_key_falls_back():
    """``column_name`` only (no table, no dot) → column captured, table = default."""
    t, f = onboard._mapping_table_field(
        {"column_name": "serial_number"}, default_table="example_athena_db"
    )
    assert (t, f) == ("example_athena_db", "serial_number")


def test_mapping_table_field_column_key_and_bare_field():
    """``column`` key and a bare ``field`` (no dot) both fall back to default_table."""
    assert onboard._mapping_table_field({"column": "qty"}, default_table="d") == ("d", "qty")
    assert onboard._mapping_table_field({"field": "qty"}, default_table="d") == ("d", "qty")


def test_mapping_table_field_empty_when_no_locator():
    """No column locator at all → ("<default>", "") — the caller skips empty fields."""
    t, f = onboard._mapping_table_field({"concept": "x"}, default_table="d")
    assert f == ""


def test_mapping_table_field_preserves_multi_dot_flattened_column():
    """M5: explicit table + table-prefixed multi-dot field (a flattened struct
    path) keeps the VERBATIM remainder as the column. The old last-segment split
    mangled ``group_a.sub.code`` → ``code``, which the Athena physical gate then
    dropped — every mapped multi-dot column died at provision."""
    t, f = onboard._mapping_table_field(
        {"table": "wide_table", "field": "wide_table.group_a.sub.code"},
        default_table="db",
    )
    assert (t, f) == ("wide_table", "group_a.sub.code")


def test_mapping_table_field_explicit_table_unprefixed_field_unchanged():
    """An explicit table whose field is NOT table-prefixed (e.g. a database-
    qualified identifier) keeps the existing last-segment behavior — no
    regression for the qualified shapes."""
    t, f = onboard._mapping_table_field(
        {"table": "example_segments",
         "field": "example_athena_db.example_segments.segment_id"},
        default_table="db",
    )
    assert (t, f) == ("example_segments", "segment_id")


def test_mapping_table_field_table_less_longest_physical_suffix():
    """M5: a TABLE-LESS locator resolves by longest-suffix match against the
    known physical columns — disambiguating a flattened struct path from a
    database-qualified identifier."""
    physical = {"wide_table": {"group_a.sub.code", "id"}}
    # A bare flattened path IS the column.
    t, f = onboard._mapping_table_field(
        {"field": "group_a.sub.code"}, default_table="db",
        physical_columns=physical,
    )
    assert (t, f) == ("wide_table", "group_a.sub.code")
    # A table-qualified locator resolves to the same physical column.
    t2, f2 = onboard._mapping_table_field(
        {"field": "wide_table.group_a.sub.code"}, default_table="db",
        physical_columns=physical,
    )
    assert (t2, f2) == ("wide_table", "group_a.sub.code")


def test_mapping_table_field_table_less_no_physical_match_falls_back():
    """A table-less locator matching NO physical column keeps the current
    second-to-last-segment recovery precedence (no behavior change)."""
    physical = {"wide_table": {"id"}}
    t, f = onboard._mapping_table_field(
        {"field": "orders.serial_number"}, default_table="db",
        physical_columns=physical,
    )
    assert (t, f) == ("orders", "serial_number")


def test_longest_physical_suffix_longest_then_consistent_table_wins():
    """Longest known physical column wins; equal-length ties prefer the table
    consistent with the locator prefix (deterministic — sorted tables)."""
    physical = {"t_a": {"sub.code"}, "t_b": {"group_a.sub.code"}}
    assert onboard._longest_physical_suffix(
        "t_a.group_a.sub.code", physical) == ("t_b", "group_a.sub.code")
    tie = {"t_a": {"sub.code"}, "t_b": {"sub.code"}}
    assert onboard._longest_physical_suffix("t_b.sub.code", tie) == ("t_b", "sub.code")
    assert onboard._longest_physical_suffix("nope", physical) is None


# --------------------------------------------------------------------------- #
# REAL schema-write: _do_provision for an Athena system
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    """Record every ``neptune_write(cypher, params)`` and neutralize impure seams."""
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    return writes


def _provision_athena(monkeypatch, mappings):
    writes = _install_seams(monkeypatch)
    body = ProvisionRequest(
        system_id="example-athena-x",
        system_name="Example Athena",
        schema="example_athena_db",
        connection_type=ATHENA,
        connection_config={"workgroup": "wg", "database": "example_athena_db", "region": "us-east-1"},
        mappings=mappings,
        rules=[],
    )
    onboard._do_provision(body)
    return writes


def _schema_entry_ids(writes):
    return [
        p["seid"]
        for c, p in writes
        if "MERGE (se:SchemaEntry {id: $seid})" in c and "seid" in p
    ]


def _field_ids(writes):
    return [
        p["fid"]
        for c, p in writes
        if "MERGE (f:Field {id: $fid})" in c and "fid" in p
    ]


def _has_field_edges(writes):
    return [
        (p.get("seid"), p.get("fid"))
        for c, p in writes
        if "MERGE (se)-[:HAS_FIELD]->(f)" in c
    ]


def test_athena_column_name_only_writes_schema_and_fields(monkeypatch):
    """B1 regression: an Athena-shaped mapping carrying ``column_name`` only (no
    table/field) writes SchemaEntry + Field + HAS_FIELD nodes (was 0 before the fix)."""
    writes = _provision_athena(
        monkeypatch,
        [
            {"column_name": "serial_number", "concept": "equipment-id",
             "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
            {"column_name": "work_order_id", "concept": "work-order",
             "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]},
        ],
    )
    ses = _schema_entry_ids(writes)
    fids = _field_ids(writes)
    edges = _has_field_edges(writes)
    # Exactly one SchemaEntry (the schema/database fallback table) and two Fields.
    assert ses == ["example-athena-x.example_athena_db"]
    assert set(fids) == {
        "example-athena-x.example_athena_db.serial_number",
        "example-athena-x.example_athena_db.work_order_id",
    }
    # Every Field is wired under the SchemaEntry via HAS_FIELD.
    assert set(edges) == {
        ("example-athena-x.example_athena_db", "example-athena-x.example_athena_db.serial_number"),
        ("example-athena-x.example_athena_db", "example-athena-x.example_athena_db.work_order_id"),
    }


def test_athena_dotted_field_recovers_real_table(monkeypatch):
    """A mapping with a dotted ``field`` but NO separate ``table`` key recovers the
    REAL table from the field prefix (not the fallback)."""
    writes = _provision_athena(
        monkeypatch,
        [
            {"field": "production_orders.serial_number", "concept": "equipment-id",
             "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
        ],
    )
    assert _schema_entry_ids(writes) == ["example-athena-x.production_orders"]
    assert _field_ids(writes) == ["example-athena-x.production_orders.serial_number"]
    assert _has_field_edges(writes) == [
        ("example-athena-x.production_orders", "example-athena-x.production_orders.serial_number")
    ]


def test_athena_canonical_shape_unchanged(monkeypatch):
    """Canonical table+field mappings are unaffected (no regression)."""
    writes = _provision_athena(
        monkeypatch,
        [
            {"table": "production_orders", "field": "production_orders.serial_number",
             "concept": "equipment-id",
             "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
        ],
    )
    assert _schema_entry_ids(writes) == ["example-athena-x.production_orders"]
    assert _field_ids(writes) == ["example-athena-x.production_orders.serial_number"]


def test_athena_qualified_field_surfaces_real_table(monkeypatch):
    """FIX 3: a database-qualified dotted ``field`` (``db.table.column``) writes the
    SchemaEntry under the REAL table name, NOT the database. Before the fix the table
    was read as the database (first segment) — identical to the ``default_table``
    fallback — so ``get_system_schema`` surfaced the database name and the real table
    ('example_segments') was never surfaced, forcing an athena_list_tables round-trip."""
    writes = _provision_athena(
        monkeypatch,
        [
            {"field": "example_athena_db.example_segments.segment_id", "concept": "batch",
             "mappings": [{"concept": "batch", "role": "identifier", "confidence": 0.9}]},
        ],
    )
    # SchemaEntry is the REAL table, not the database name ("example-athena-x.example_athena_db").
    assert _schema_entry_ids(writes) == ["example-athena-x.example_segments"]
    assert _field_ids(writes) == ["example-athena-x.example_segments.segment_id"]
    assert _has_field_edges(writes) == [
        ("example-athena-x.example_segments", "example-athena-x.example_segments.segment_id")
    ]
