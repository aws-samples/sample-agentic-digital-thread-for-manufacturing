# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0077 D1 — the steward's ACCEPT materialises additive schema drift (offline).

End to end through ``self_learning.accept_mapping``: a ``column_added`` /
``table_added`` row MERGEs Field/SchemaEntry nodes through ``schema_fields``
over the module's loud seams, with exact readback; destructive kinds stay
acknowledge-only; a materialised row cannot be requeued (no DETACH DELETE).
"""
from __future__ import annotations

import pytest

import agents.schema_drift as schema_drift
import agents.schema_fields as sf
import agents.schema_repair as schema_repair
import agents.self_learning as self_learning

try:
    from unit.test_schema_drift_proposals import _install, _stub_discover
    from unit.test_schema_repair import GraphFake
    from unit.test_self_learning_lifecycle import FakeTable
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_schema_drift_proposals import _install, _stub_discover
    from test_schema_repair import GraphFake
    from test_self_learning_lifecycle import FakeTable

_SOURCE = [{"name": "id", "data_type": "integer"},
           {"name": "status", "data_type": "text"},
           {"name": "priority", "data_type": "text"}]


def _setup(monkeypatch, fresh_columns, *, graph=None, source=_SOURCE):
    fake = FakeTable()
    graph = graph or GraphFake(existing_fields=("erp.work_orders.id",
                                                "erp.work_orders.status"))
    _install(monkeypatch, fake)
    monkeypatch.setattr(self_learning, "_neptune_write", graph.write)
    monkeypatch.setattr(self_learning, "_neptune_query", graph.query)
    monkeypatch.setattr(schema_repair, "_source_columns",
                        lambda sid, table: (list(source), True))
    _stub_discover(monkeypatch, fresh_columns)
    schema_drift.run_drift_check("erp")
    graph.writes.clear()
    return fake, graph


_ADDED = [("id", "integer"), ("status", "text"), ("priority", "text")]


def test_accept_column_added_materialises_the_field(monkeypatch):
    fake, graph = _setup(monkeypatch, _ADDED)
    (row,) = fake.all_items()

    result = self_learning.accept_mapping(row["mapping_id"], actor="steward@example.com")

    assert result["materialised"] is True and result["status"] == "accepted"
    assert result["written"]["fields"] == ["priority"]
    assert result["neptune_node_id"] == "erp.work_orders.priority"
    assert graph.cyphers() == [sf.FIELD_MERGE, sf.HAS_FIELD_MERGE,
                               sf.DISCOVERY_COMPLETENESS_SET]
    assert graph.writes[-1][1] == {"seid": "erp.work_orders", "complete": True, "count": 3}
    stored = fake.by_id(row["mapping_id"])
    assert stored["status"] == "accepted"
    assert stored["neptune_node_id"] == "erp.work_orders.priority"
    assert stored["accepted_by"] == "steward@example.com"


def test_no_maps_to_authority_or_rule_writes(monkeypatch):
    fake, graph = _setup(monkeypatch, _ADDED)
    self_learning.accept_mapping(fake.all_items()[0]["mapping_id"])
    joined = " ".join(graph.cyphers())
    for forbidden in ("MAPS_TO", "AuthorityRule", "IdentityRule", "JoinPath",
                      "DELETE"):
        assert forbidden not in joined


def test_column_gone_from_source_is_422_and_stays_pending(monkeypatch):
    fake, graph = _setup(monkeypatch, _ADDED, source=_SOURCE[:2])
    (row,) = fake.all_items()

    result = self_learning.accept_mapping(row["mapping_id"])

    assert result["http_status"] == 422 and result["materialised"] is False
    assert "not visible in the source" in result["error"]
    assert graph.writes == []
    assert fake.by_id(row["mapping_id"])["status"] == "pending"


def test_readback_miss_raises_and_stays_pending(monkeypatch):
    graph = GraphFake(existing_fields=("erp.work_orders.id",), readback=False)
    fake, graph = _setup(monkeypatch, _ADDED, graph=graph)
    (row,) = fake.all_items()

    with pytest.raises(RuntimeError, match="readback"):
        self_learning.accept_mapping(row["mapping_id"])
    stored = fake.by_id(row["mapping_id"])
    assert stored["status"] == "pending" and "neptune_node_id" not in stored


def test_accept_table_added_materialises_entry_and_columns(monkeypatch):
    fake = FakeTable()
    graph = GraphFake()
    _install(monkeypatch, fake)
    monkeypatch.setattr(self_learning, "_neptune_write", graph.write)
    monkeypatch.setattr(self_learning, "_neptune_query", graph.query)
    monkeypatch.setattr(schema_repair, "_source_columns", lambda sid, table: (
        [{"name": "id", "data_type": "integer"}], True))
    monkeypatch.setattr(schema_drift, "_discover_fresh", lambda sid: {"tables": [
        {"name": "work_orders", "columns": [{"name": "id", "data_type": "integer"},
                                            {"name": "status", "data_type": "text"}]},
        {"name": "segment_events", "columns": [{"name": "id", "data_type": "integer"}]}]})
    schema_drift.run_drift_check("erp")
    (row,) = [r for r in fake.all_items() if r["reason"] == "table_added"]

    result = self_learning.accept_mapping(row["mapping_id"])

    assert result["neptune_node_id"] == "erp.segment_events"
    assert result["written"]["table_created"] is True
    assert graph.cyphers() == [sf.SCHEMA_ENTRY_MERGE, sf.FIELD_MERGE,
                               sf.HAS_FIELD_MERGE, sf.DISCOVERY_COMPLETENESS_SET]


def test_materialised_row_cannot_be_requeued(monkeypatch):
    fake, graph = _setup(monkeypatch, _ADDED)
    mapping_id = fake.all_items()[0]["mapping_id"]
    self_learning.accept_mapping(mapping_id)
    graph.writes.clear()

    with pytest.raises(ValueError, match="requeue is not supported"):
        self_learning.requeue_mapping(mapping_id)
    assert graph.writes == []  # never a DETACH DELETE from the queue
    assert fake.by_id(mapping_id)["status"] == "accepted"


@pytest.mark.parametrize("fresh, reason", [
    ([("id", "integer")], "column_removed"),
    ([("id", "integer"), ("status", "uuid")], "column_retyped"),
])
def test_destructive_kinds_stay_acknowledge_only(monkeypatch, fresh, reason):
    fake, graph = _setup(monkeypatch, fresh)
    monkeypatch.setattr(self_learning, "reconcile_blocked_proposals", lambda: {})
    (row,) = [r for r in fake.all_items() if r["reason"] == reason]

    result = self_learning.accept_mapping(row["mapping_id"])

    assert graph.writes == []
    assert result["materialised"] is False and result["acknowledged"] is True
    assert "neptune_node_id" not in fake.by_id(row["mapping_id"])


def test_table_removed_stays_acknowledge_only(monkeypatch):
    fake, graph = _setup(monkeypatch, _ADDED)
    monkeypatch.setattr(self_learning, "reconcile_blocked_proposals", lambda: {})
    monkeypatch.setattr(schema_drift, "_discover_fresh", lambda sid: {"tables": []})
    schema_drift.run_drift_check("erp")
    (row,) = [r for r in fake.all_items() if r["reason"] == "table_removed"]
    graph.writes.clear()

    result = self_learning.accept_mapping(row["mapping_id"])

    assert graph.writes == [] and result["materialised"] is False


@pytest.mark.parametrize("status", ["accepted", "rejected", "superseded"])
def test_materialising_accept_requires_a_pending_row(monkeypatch, status):
    """A graph-mutating accept never re-applies a decided row by id: 409, zero writes."""
    from agents import self_learning as sl
    writes: list = []
    monkeypatch.setattr(sl, "_neptune_write_loud", lambda *a, **k: writes.append(a))
    item = {"mapping_id": "m-1", "discovered_at": "2026-09-01T00:00:00+00:00",
            "status": status, "proposed_type": "schema_drift", "reason": "column_added",
            "from_system": "erp", "from_field": "work_orders.c"}
    out = sl._accept_schema_drift_materialise(object(), item, "steward")
    assert out["http_status"] == 409 and out["materialised"] is False
    assert writes == []
