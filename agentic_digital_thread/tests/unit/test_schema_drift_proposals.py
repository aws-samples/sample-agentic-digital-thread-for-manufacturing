# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E5 schema-drift proposal lifecycle (ADR-0053) — offline.

Behavioral proof against the in-memory ``FakeTable``/``NeptuneSpy`` doubles
shared with ``test_self_learning_lifecycle.py``. No AWS, no Neptune, no
network — the discover seam (``schema_drift._discover_fresh``) and the graph
baseline (``schema_check.load_system_schema``) are stubbed entirely.

Pins (plan D-E5):

- the pinned proposal ROW SHAPE (``proposed_type="schema_drift"``,
  ``source="agent"``, ``method="schema-drift"``, ``from_system == to_system``,
  empty value/field/concept slots, ``confidence=1.0``, dated
  ``query_context``, ``reason`` = kind, ``description`` for retyped) and the
  ``proposal_key.py`` GRAIN (subject ``(from_system, from_field)``; target
  adds ``digest(reason + description)``);
- the SAME unresolved drift never duplicates (``dup_pending`` — found, not
  filed) while a type-flap files a DISTINCT row;
- accepting a removal/retype ``schema_drift`` row is ACKNOWLEDGE-ONLY: ZERO
  graph writes, status flips to accepted, no ``neptune_node_id``; removal
  drifts trigger the existing ``reconcile_blocked_proposals`` cleanup
  best-effort (additive kinds materialise — ``test_schema_drift_materialise``);
- the ingestion gates still apply: an agent-sourced drift proposal naming a
  system that matches NO catalog entry drops.
"""
from __future__ import annotations

import pytest

import agents.schema_check as schema_check
import agents.schema_drift as schema_drift
import agents.self_learning as self_learning
from agents.proposal_key import canonical_keys

try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneSpy
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneSpy

_BASELINE = {
    "work_orders": {
        "columns": ["id", "status"],
        "column_types": {"id": "integer", "status": "text"},
        "concept_map": {}, "relation_kind": "base_table",
    },
}


def _install(monkeypatch, table: FakeTable, spy: NeptuneSpy | None = None,
             system_id: str = "erp") -> NeptuneSpy:
    spy = spy or NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    monkeypatch.setattr(self_learning, "known_systems",
                        lambda: [{"id": "erp", "name": "ERP"}])
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: dict(_BASELINE) if sid == system_id else None)
    return spy


def _fresh(columns) -> dict:
    return {"tables": [{"name": "work_orders",
                        "columns": [{"name": n, "data_type": t}
                                    for n, t in columns]}]}


def _stub_discover(monkeypatch, columns) -> None:
    monkeypatch.setattr(schema_drift, "_discover_fresh",
                        lambda sid: _fresh(columns))


def test_row_shape_and_grain(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "text"),
                                 ("priority", "text")])  # one added column

    result = schema_drift.run_drift_check("erp")

    assert result == {"system_id": "erp", "status": "ok", "drifts_found": 1,
                      "proposals_filed": 1, "proposal_noops": 0, "kinds": {"column_added": 1},
                      "resolved_swept": 0, "refiled_after_accept": 0}
    rows = fake.all_items()
    assert len(rows) == 1
    row = rows[0]
    assert row["proposed_type"] == "schema_drift"
    assert row["source"] == "agent"
    assert row["method"] == "schema-drift"
    assert row["status"] == "pending"
    assert row["from_system"] == "erp" and row["to_system"] == "erp"
    assert row["from_field"] == "work_orders.priority"
    assert (row["from_value"], row["to_field"], row["to_value"],
            row["concept"]) == ("", "", "", "")
    assert row["confidence"] == "1.0"  # the diff is deterministic
    assert row["reason"] == "column_added"
    assert row["query_context"].startswith("schema drift detected ")
    assert "column_added" in row["query_context"]
    # Pinned proposal_key grain — subject/target recompute byte-identically.
    expected = canonical_keys("schema_drift", from_system="erp",
                              from_field="work_orders.priority",
                              reason="column_added", description="")
    assert row["subject_key"] == expected.subject_key
    assert row["target_key"] == expected.target_key


def test_same_drift_dedupes_as_found_not_filed(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "text"),
                                 ("priority", "text")])

    first = schema_drift.run_drift_check("erp")
    second = schema_drift.run_drift_check("erp")

    assert first["proposals_filed"] == 1
    # dup_pending: the drift is already on the queue — found, NOT filed, and
    # reported as the one no-op it is.
    assert second == {"system_id": "erp", "status": "ok", "drifts_found": 1,
                      "proposals_filed": 0, "proposal_noops": 1, "kinds": {"column_added": 1},
                      "resolved_swept": 0, "refiled_after_accept": 0}
    assert len(fake) == 1  # no duplicate row


def test_type_flap_files_a_distinct_row(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    _stub_discover(monkeypatch, [("id", "integer"), ("status", "varchar")])
    schema_drift.run_drift_check("erp")   # retyped: was text, now varchar
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "uuid")])
    schema_drift.run_drift_check("erp")   # retyped: was text, now uuid

    drift_rows = [r for r in fake.all_items()
                  if r["proposed_type"] == "schema_drift"]
    # Same subject, DIFFERENT descriptor digest ⇒ two separately-reviewable
    # rows (the newer supersedes the elder pending on the same subject).
    descriptions = sorted(r.get("to_description", "") for r in drift_rows)
    assert descriptions == ["was text, now uuid", "was text, now varchar"]
    assert len({r["target_key"] for r in drift_rows}) == 2


def test_accept_is_acknowledge_only(monkeypatch):
    """Destructive/semantic drift stays acknowledge-only (ADR-0077 D1/D5 —
    the zero-write pin moved off ``column_added``, which now materialises)."""
    fake = FakeTable()
    spy = _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "varchar")])
    schema_drift.run_drift_check("erp")
    (row,) = fake.all_items()
    assert row["reason"] == "column_retyped"
    mapping_id = row["mapping_id"]

    result = self_learning.accept_mapping(mapping_id, actor="steward@example.com")

    assert spy.calls == []  # ZERO graph writes — acknowledge-only, pinned
    assert result["status"] == "accepted"
    assert result["acknowledged"] is True
    assert result["materialised"] is False
    assert result["proposed_type"] == "schema_drift"
    row = fake.by_id(mapping_id)
    assert row["status"] == "accepted"
    assert row["accepted_by"] == "steward@example.com"
    assert "neptune_node_id" not in row  # honest absence — no applied artifact


def test_accept_of_removal_triggers_reconcile(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer")])  # status removed
    schema_drift.run_drift_check("erp")
    (row,) = fake.all_items()
    assert row["reason"] == "column_removed"

    calls: list[str] = []
    monkeypatch.setattr(self_learning, "reconcile_blocked_proposals",
                        lambda: calls.append("reconcile") or {})
    self_learning.accept_mapping(row["mapping_id"])
    assert calls == ["reconcile"]


def test_gates_still_apply(monkeypatch):
    """An agent-sourced drift proposal for a NON-catalog system drops at the
    unknown-system ingestion gate (defense-in-depth — the sweep only ever
    iterates onboarded systems)."""
    fake = FakeTable()
    _install(monkeypatch, fake, system_id="ghost-system")
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "text"),
                                 ("priority", "text")])

    result = schema_drift.run_drift_check("ghost-system")

    assert result["status"] == "ok"
    assert result["drifts_found"] == 1
    assert result["proposals_filed"] == 0  # gate-dropped — honest count
    assert len(fake) == 0


def test_never_auto_accepted(monkeypatch):
    """schema_drift sits in the auto-accept DENY-TUPLE — the flag cannot
    silently acknowledge drift."""
    assert "schema_drift" in self_learning._NEVER_AUTO_ACCEPT_TYPES
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "text"),
                                 ("priority", "text")])
    schema_drift.run_drift_check("erp")
    (row,) = fake.all_items()
    assert row["status"] == "pending"


def test_skips_never_file(monkeypatch):
    """A skipped system files NOTHING (no fabricated empty diff)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)

    result = schema_drift.run_drift_check("erp")

    assert result["status"] == "skipped:no-baseline"
    assert len(fake) == 0


# --------------------------------------------------------------------------- #
# ``proposals_filed`` once counted the no-op-on-accepted-row path as filed
# (the write returns the ACCEPTED row's id, which is not in the PENDING
# snapshot), and ``drifts_found`` was suspected of over-counting when a table
# has multi-mapped fields (duplicated MAPS_TO rows in the schema query).
# --------------------------------------------------------------------------- #

def test_noop_on_an_accepted_retype_row_is_not_counted_as_filed(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer"), ("status", "varchar")])
    first = schema_drift.run_drift_check("erp")       # retyped: text -> varchar
    assert first["proposals_filed"] == 1 and first["proposal_noops"] == 0
    (row,) = fake.all_items()
    self_learning.accept_mapping(row["mapping_id"])   # acknowledge-only accept

    second = schema_drift.run_drift_check("erp")      # same retype, still true

    assert second["drifts_found"] == 1
    assert second["proposals_filed"] == 0             # nothing was written
    assert second["proposal_noops"] == 1              # reported, not hidden
    assert len(fake) == 1


def test_skip_entry_carries_the_noop_counter(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)
    assert schema_drift.run_drift_check("erp")["proposal_noops"] == 0


def test_multi_mapped_fields_never_shrink_the_baseline():
    """130 Field nodes, 27 of them mapped to several concepts (one schema-query
    row per MAPS_TO edge), against 155 fresh columns must diff to exactly the
    25 genuinely absent columns, never 93."""
    from agents.graph_tools import _append_schema_row
    schema: dict = {}
    for i in range(130):
        name = f"col_{i:03d}"
        concepts = ["c-a", "c-b", "c-c"] if i < 27 else [None]
        for concept in concepts:
            _append_schema_row(schema, {
                "table_name": "wide", "relation_kind": "base_table",
                "field_name": name, "concept": concept, "role": "measure",
                "data_type": "string"})
    assert len(schema["wide"]["columns"]) == 130
    fresh = [{"name": "wide", "columns": [{"name": f"col_{i:03d}", "data_type": "string"}
                                          for i in range(155)]}]
    drifts = schema_drift.diff_schemas(schema, fresh)
    assert len(drifts) == 25
    assert {d["reason"] for d in drifts} == {"column_added"}
