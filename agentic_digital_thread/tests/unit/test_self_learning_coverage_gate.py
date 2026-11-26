# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for accepted-rule coverage suppression at proposal ingestion."""
from __future__ import annotations

import agents.self_learning as self_learning
import agents.value_promotion as value_promotion

import pytest


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)



class FakeTable:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.updates: list[dict] = []

    def put_item(self, Item: dict) -> None:
        self.items.append(dict(Item))

    def query(self, **kwargs) -> dict:
        values = kwargs.get("ExpressionAttributeValues", {})
        if kwargs.get("IndexName") == "subject-key-index":
            return {"Items": [
                dict(item) for item in self.items
                if item.get("subject_key") == values.get(":sk")
            ]}
        if kwargs.get("IndexName") == "status-index":
            return {"Items": [
                dict(item) for item in self.items
                if item.get("status") == values.get(":status")
            ]}
        if ":mid" in values:
            return {"Items": [
                dict(item) for item in self.items
                if item.get("mapping_id") == values[":mid"]
            ]}
        return {"Items": []}

    def update_item(self, **kwargs) -> None:
        self.updates.append(kwargs)
        if "#s = :status" not in kwargs.get("UpdateExpression", ""):
            return
        key = kwargs["Key"]
        for item in self.items:
            if (item.get("mapping_id") == key["mapping_id"]
                    and item.get("discovered_at") == key["discovered_at"]):
                item["status"] = kwargs["ExpressionAttributeValues"][":status"]

    def scan(self) -> dict:
        return {"Items": [dict(item) for item in self.items]}

    def delete_item(self, *, Key: dict) -> None:
        self.items = [item for item in self.items if not (
            item.get("mapping_id") == Key["mapping_id"]
            and item.get("discovered_at") == Key["discovered_at"]
        )]


def _log(from_value: str, to_value: str) -> str | None:
    return self_learning.log_proposed_mapping(
        "sys-a", "records.external_id", from_value,
        "sys-b", "entities.canonical_id", to_value,
        "entity-id", 0.8, "resolved identifier",
        proposed_type="value_mapping", source="agent",
    )


def _install(monkeypatch, accepted: list[dict]):
    table = FakeTable()
    table.items.extend(dict(row) for row in accepted)
    queries: list[str] = []
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    def query_status(status: str, **_kwargs):
        queries.append(status)
        return [dict(row) for row in table.items if row.get("status") == status]

    monkeypatch.setattr(self_learning, "_query_by_status", query_status)
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", lambda: [
        {"id": "sys-a", "name": "System A"},
        {"id": "sys-b", "name": "System B"},
    ])
    self_learning._reset_known_systems_cache()
    monkeypatch.setattr(value_promotion, "observe_pending_value_mapping", lambda item: False)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    self_learning._reset_accepted_coverage_cache()
    return table, queries


def test_accepted_rule_suppresses_observation_and_records_cached_evidence(monkeypatch):
    accepted = [{
        "mapping_id": "rule-1",
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "status": "accepted",
        "proposed_type": "join_path",
        "from_system": "sys-a",
        "from_field": "records.external_id",
        "to_system": "sys-b",
        "to_field": "entities.canonical_id",
        "transform": "casefold",
    }]
    table, queries = _install(monkeypatch, accepted)

    assert _log("ITEM-A", "item-a") is None
    assert _log("ITEM-B", "item-b") is None

    assert len(table.items) == 1
    assert table.items[0]["mapping_id"] == "rule-1"
    assert queries == ["accepted"]  # one TTL-cached accepted-row snapshot
    assert len(table.updates) == 2
    assert all("ADD coverage_evidence_count :one" in update["UpdateExpression"]
               for update in table.updates)


def test_without_covering_accepted_rule_observation_flows_through(monkeypatch):
    table, queries = _install(monkeypatch, [])

    mapping_id = _log("ITEM-A", "different-value")

    assert mapping_id is not None
    assert queries == ["accepted"]
    assert len(table.items) == 1
    assert table.items[0]["status"] == "pending"
    assert table.updates == []


def _accepted_identity_rule() -> dict:
    return {
        "mapping_id": "rule-1",
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "status": "accepted",
        "proposed_type": "join_path",
        "from_system": "sys-a",
        "from_field": "records.external_id",
        "to_system": "sys-b",
        "to_field": "entities.canonical_id",
        "transform": "identity",
    }


def test_user_correction_is_visible_to_coverage_gate_immediately(monkeypatch):
    table, queries = _install(monkeypatch, [])
    monkeypatch.setattr(self_learning, "_apply_to_neptune",
                        lambda _item, subject_id, _version: subject_id)

    assert self_learning._accepted_coverage_rows() == []  # warm empty cache
    self_learning.record_user_correction(
        "sys-a", "records.external_id", "ITEM-A",
        "sys-b", "entities.canonical_id", "ITEM-A",
        "entity-id", proposed_type="join_path",
    )

    assert _log("ITEM-A", "ITEM-A") is None
    assert queries == ["accepted", "accepted"]
    assert len([item for item in table.items if item["status"] == "accepted"]) == 1


def test_reset_all_accepted_removes_stale_coverage_suppression(monkeypatch):
    table, queries = _install(monkeypatch, [_accepted_identity_rule()])
    monkeypatch.setattr("agents.onboard.neptune_write", lambda *_args, **_kwargs: True)

    assert _log("ITEM-A", "ITEM-A") is None  # warm covering cache
    assert self_learning.reset_all_accepted() == {"status": "reset", "count": 1}
    assert _log("ITEM-B", "ITEM-B") is not None

    assert queries == ["accepted", "accepted"]
    assert any(item["status"] == "pending" for item in table.items)


def test_clear_all_learnings_removes_stale_coverage_suppression(monkeypatch):
    table, queries = _install(monkeypatch, [_accepted_identity_rule()])
    monkeypatch.setattr("agents.onboard.neptune_write", lambda *_args, **_kwargs: True)

    assert _log("ITEM-A", "ITEM-A") is None  # warm covering cache
    assert self_learning.clear_all_learnings() == {"status": "cleared_all", "count": 1}
    assert _log("ITEM-B", "ITEM-B") is not None

    assert queries == ["accepted", "accepted"]
    assert len(table.items) == 1
    assert table.items[0]["status"] == "pending"


def test_stale_accepted_coverage_snapshot_cannot_publish_after_invalidation(monkeypatch):
    _install(monkeypatch, [])
    captured_generation = self_learning._accepted_coverage_cache_generation
    stale_rows = [_accepted_identity_rule()]

    # Simulate a reset/clear mutation invalidating after the reader fetched rows
    # but before that reader attempts to publish its snapshot.
    self_learning._reset_accepted_coverage_cache()

    published = self_learning._publish_accepted_coverage_cache(
        captured_generation,
        stale_rows,
        expires_at=10**12,
    )

    assert published is False
    assert self_learning._accepted_coverage_cache is None
