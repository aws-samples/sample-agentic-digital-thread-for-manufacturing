# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 3C (ADR-0040 D6): offline tests for the entity/identity backfill selector.

Only the PURE selection portion is unit-tested — the graph reads/writes and the
``_classify_table_entity`` reuse run against a VPC-private Neptune (ADR-0024) and are
exercised manually against a deployed env. These tests assert the idempotency-critical
selector: a SchemaEntry that already carries an entity edge is NEVER re-selected.
"""
from scripts.backfill_entity_identity import (
    tables_missing_entity_edge,
    restrict_to_systems,
)


def test_selects_only_schema_entries_without_entity_edge():
    rows = [
        {"system_id": "athena-wh", "table": "records", "entity_edges": 0},
        {"system_id": "athena-wh", "table": "events", "entity_edges": 0},
        {"system_id": "erp-x", "table": "orders", "entity_edges": 1},   # already has an edge
        {"system_id": "sitewise-y", "table": "assets", "entity_edges": 0},
    ]
    missing = tables_missing_entity_edge(rows)
    assert missing == [("athena-wh", "events"), ("athena-wh", "records"), ("sitewise-y", "assets")]
    # The curated system that already has an edge is NOT selected (idempotency).
    assert ("erp-x", "orders") not in missing


def test_idempotency_second_run_selects_nothing():
    """After a run, every SchemaEntry carries an edge (entity_edges >= 1); a re-run's
    selector must return nothing — this is the guarantee that re-running is a no-op."""
    rows_after = [
        {"system_id": "athena-wh", "table": "records", "entity_edges": 1},
        {"system_id": "sitewise-y", "table": "assets", "entity_edges": 2},
    ]
    assert tables_missing_entity_edge(rows_after) == []


def test_has_entity_flag_alias_and_bad_rows_ignored():
    rows = [
        {"system_id": "a", "table": "t1", "has_entity": True},    # flag alias → present
        {"system_id": "a", "table": "t2", "has_entity": False},   # flag alias → missing
        {"system_id": "", "table": "t3", "entity_edges": 0},       # no system → ignored
        {"system_id": "b", "table": "", "entity_edges": 0},        # no table → ignored
    ]
    assert tables_missing_entity_edge(rows) == [("a", "t2")]


def test_deterministic_sorted_and_deduped():
    rows = [
        {"system_id": "z", "table": "b", "entity_edges": 0},
        {"system_id": "z", "table": "a", "entity_edges": 0},
        {"system_id": "z", "table": "a", "entity_edges": 0},  # duplicate
        {"system_id": "m", "table": "c", "entity_edges": 0},
    ]
    assert tables_missing_entity_edge(rows) == [("m", "c"), ("z", "a"), ("z", "b")]


def test_restrict_to_systems_front_loads_and_orders():
    pairs = [("athena-wh", "records"), ("athena-wh", "events"),
             ("sitewise-y", "assets"), ("erp-x", "orders")]
    # Only the requested systems, in the requested order, tables sorted within a system.
    out = restrict_to_systems(pairs, ["sitewise-y", "athena-wh"])
    assert out == [("sitewise-y", "assets"), ("athena-wh", "events"), ("athena-wh", "records")]


def test_restrict_to_systems_none_keeps_input_order():
    pairs = [("m", "c"), ("z", "a")]
    assert restrict_to_systems(pairs, None) == pairs
    assert restrict_to_systems(pairs, []) == pairs
