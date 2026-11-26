# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E5 schema-drift PURE diff (ADR-0053) — offline, no AWS.

``schema_drift.diff_schemas`` compares the graph's onboarded baseline
(``schema_check.load_system_schema`` shape) against a fresh
``onboard_discover`` result's ``tables`` list. Pins:

- the five drift kinds (column added/removed/retyped, table added/removed)
  with the pinned ``from_field`` grain (``table.column``; ``table.`` for
  table-level events);
- an ``unknown``/absent type NEVER emits ``column_retyped`` (honest
  indeterminate) — type comparison is case-folded + whitespace-normalized;
- dotted/struct column paths use the SAME prefix-tolerant matching
  ``schema_check.column_in_schema`` uses ⇒ no false added/removed drift
  (the lake-athena struct-path lesson);
- deterministic ordering (stable across runs).
"""
from __future__ import annotations

from agents.schema_drift import diff_schemas


def _baseline(columns, types=None, table="work_orders"):
    entry = {"columns": list(columns), "concept_map": {},
             "relation_kind": "base_table"}
    if types:
        entry["column_types"] = dict(types)
    return {table: entry}


def _fresh(columns, table="work_orders"):
    """Fresh discover shape: columns as [{"name", "data_type"}]."""
    return [{"name": table,
             "columns": [{"name": n, "data_type": t} for n, t in columns]}]


def test_added_removed_retyped_detected():
    baseline = _baseline(
        ["id", "status", "priority"],
        types={"id": "integer", "status": "text", "priority": "text"})
    fresh = _fresh([("id", "integer"), ("status", "varchar"),
                    ("assigned_to", "text")])

    drifts = diff_schemas(baseline, fresh)

    by_kind = {d["reason"]: d for d in drifts}
    assert set(by_kind) == {"column_added", "column_removed", "column_retyped"}
    assert by_kind["column_added"]["from_field"] == "work_orders.assigned_to"
    assert by_kind["column_removed"]["from_field"] == "work_orders.priority"
    retyped = by_kind["column_retyped"]
    assert retyped["from_field"] == "work_orders.status"
    assert retyped["description"] == "was text, now varchar"


def test_no_drift_on_identical_schema():
    baseline = _baseline(["id", "status"],
                         types={"id": "integer", "status": "text"})
    fresh = _fresh([("id", "integer"), ("status", "text")])
    assert diff_schemas(baseline, fresh) == []


def test_type_comparison_is_casefolded_and_whitespace_normalized():
    baseline = _baseline(["id"], types={"id": "Character   Varying"})
    fresh = _fresh([("id", "character varying")])
    assert diff_schemas(baseline, fresh) == []


def test_unknown_type_never_retypes():
    # Baseline has NO stored type; fresh has one — and vice versa. Neither
    # direction may fabricate a retype (honest indeterminate).
    baseline = _baseline(["id", "status"], types={"status": "text"})
    fresh = [{"name": "work_orders",
              "columns": [{"name": "id", "data_type": "integer"},
                          {"name": "status"}]}]
    assert diff_schemas(baseline, fresh) == []


def test_struct_path_no_false_drift():
    # The graph stores flattened struct leaves; the fresh read reports the
    # struct PARENT (and vice versa). Prefix-tolerant matching (the exact
    # schema_check semantics) ⇒ NO added/removed drift, and no retype either
    # (the exact column name never matches, so types stay indeterminate).
    baseline = _baseline(["oee.availability", "oee.performance"],
                         types={"oee.availability": "double"},
                         table="asset_metrics")
    fresh = [{"name": "asset_metrics",
              "columns": [{"name": "oee", "data_type": "struct"}]}]
    assert diff_schemas(baseline, fresh) == []
    # And the mirror direction: baseline parent, fresh leaves.
    baseline2 = _baseline(["oee"], table="asset_metrics")
    fresh2 = [{"name": "asset_metrics",
               "columns": [{"name": "oee.availability", "data_type": "double"}]}]
    assert diff_schemas(baseline2, fresh2) == []


def test_table_events_use_table_dot_subject():
    baseline = _baseline(["id"], table="old_table")
    fresh = [{"name": "new_table", "columns": [{"name": "id",
                                                "data_type": "integer"}]}]

    drifts = diff_schemas(baseline, fresh)

    by_kind = {d["reason"]: d for d in drifts}
    assert set(by_kind) == {"table_added", "table_removed"}
    # Pinned grain: table-level events subject on "<table>." (trailing dot).
    assert by_kind["table_added"]["from_field"] == "new_table."
    assert by_kind["table_removed"]["from_field"] == "old_table."


def test_table_matching_is_case_insensitive():
    baseline = _baseline(["id"], types={"id": "integer"}, table="Work_Orders")
    fresh = _fresh([("id", "integer")], table="work_orders")
    assert diff_schemas(baseline, fresh) == []


def test_deterministic_ordering():
    baseline = _baseline(["b_col", "a_col"])
    fresh = [{"name": "work_orders",
              "columns": [{"name": "z_col"}, {"name": "c_col"}]}]
    first = diff_schemas(baseline, fresh)
    second = diff_schemas(baseline, fresh)
    assert first == second
    assert [d["from_field"] for d in first] == sorted(
        d["from_field"] for d in first)
