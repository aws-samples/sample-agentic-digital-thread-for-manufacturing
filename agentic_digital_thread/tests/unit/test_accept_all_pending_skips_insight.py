# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``accept_all_pending`` never bulk-accepts an ``insight`` (offline).

Found 2026-07-13: the Demo env's ~400 "accepted" entries
came through ``POST /api/proposed-mappings/accept-all`` → ``accept_all_pending``
— which accepted EVERY pending row with no type gate. An ``insight`` is a
narrative FINDING, never a graph rule: bulk-accepting it would mislabel it as
reviewed. Contract pinned here:

  * pending ``value_mapping`` / ``identity_rule`` / ``field_annotation`` are
    accepted by the bulk path (existing behavior, unchanged);
  * pending ``insight`` is SKIPPED — never passed to ``accept_mapping``, left
    ``pending``, and not counted in the result;
  * individual ``accept_mapping`` on an insight stays allowed (a harmless
    no-op ``insight-`` node id — pinned in
    ``test_insight_never_auto_accepted.py``).

No AWS / Neptune / LLM / network — seams monkeypatched per
``test_sweep_auto_accept.py`` / ``test_insight_never_auto_accepted.py``
conventions.
"""
from __future__ import annotations

import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes

import pytest


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)



def _mapping(mapping_id, ptype):
    return {"mapping_id": mapping_id, "proposed_type": ptype}


# ===========================================================================
# Mixed pending: everything but insight is accepted; insight is skipped
# ===========================================================================
def test_accept_all_accepts_non_insight_types_and_skips_insight(monkeypatch):
    pending = [
        _mapping("vm-1", "value_mapping"),
        _mapping("ir-1", "identity_rule"),
        _mapping("fa-1", "field_annotation"),
        _mapping("ins-1", "insight"),
    ]
    accepted_ids: list[str] = []

    def _accept(mid):
        accepted_ids.append(mid)
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)

    result = self_learning.accept_all_pending()

    assert accepted_ids == ["vm-1", "ir-1", "fa-1"]  # insight never reached accept
    assert result == {"status": "accepted_all", "count": 3, "errors": [],
                      "skipped_insights": 1}


def test_accept_all_with_only_insights_accepts_nothing(monkeypatch):
    pending = [_mapping("ins-1", "insight"), _mapping("ins-2", "insight")]
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: calls.append(mid) or {"status": "accepted"})

    result = self_learning.accept_all_pending()

    assert calls == []
    assert result == {"status": "accepted_all", "count": 0, "errors": [],
                      "skipped_insights": 2}


def test_accept_all_error_isolation_unchanged(monkeypatch):
    # A failing accept on one row still processes the rest (existing contract),
    # and the insight skip does not disturb the error accounting.
    pending = [
        _mapping("vm-bad", "value_mapping"),
        _mapping("ins-1", "insight"),
        _mapping("vm-good", "value_mapping"),
    ]

    def _accept(mid):
        if mid == "vm-bad":
            raise RuntimeError("boom")
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)

    result = self_learning.accept_all_pending()

    assert result["status"] == "accepted_all"
    assert result["count"] == 1
    # The per-row error names the row and a public category + reference;
    # the exception's own text stays in the server log.
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith("vm-bad: ")
    assert "Reference: " in result["errors"][0] and "boom" not in result["errors"][0]
    assert result["skipped_insights"] == 1  # additive F8 key — skip still counted


# ===========================================================================
# End-to-end through the REAL store: insight stays pending, vm flips accepted
# ===========================================================================
class _FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def query(self, **kwargs):
        idx = kwargs.get("IndexName")
        vals = kwargs.get("ExpressionAttributeValues", {})
        if idx == "subject-key-index":
            return {"Items": [i for i in self.items
                              if i.get("subject_key") == vals.get(":sk")]}
        if idx == "status-index":
            return {"Items": [i for i in self.items
                              if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self.items
                          if i.get("mapping_id") == vals.get(":mid")]}

    def put_item(self, Item):
        self.items.append(dict(Item))

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None):
        for item in self.items:
            if item["mapping_id"] == Key["mapping_id"]:
                names = ExpressionAttributeNames or {}
                values = ExpressionAttributeValues or {}
                for clause in UpdateExpression.removeprefix("SET ").split(","):
                    lhs, rhs = (s.strip() for s in clause.split("="))
                    item[names.get(lhs, lhs)] = values[rhs]


def test_accept_all_leaves_logged_insight_pending(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "_neptune_write", lambda c, p=None: True)
    route_returning_writes(monkeypatch, lambda c, p=None: True)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)

    ins_id = self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="", from_value="",
        to_system="", to_field="", to_value="",
        concept="correlation", confidence=0.7,
        query_context="Auto-analysis: downtime spikes precede quality drops",
        proposed_type="insight",
    )
    vm_id = self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="orders.line", from_value="LINE1",
        to_system="sys-b", to_field="events.line_code", to_value="DET-L1",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert ins_id is not None and vm_id is not None

    result = self_learning.accept_all_pending()

    assert result["status"] == "accepted_all"
    assert result["count"] == 1
    assert result["skipped_insights"] == 1  # the logged insight, left pending
    insight = next(i for i in fake.items if i["mapping_id"] == ins_id)
    vm = next(i for i in fake.items if i["mapping_id"] == vm_id)
    assert insight["status"] == "pending"   # finding left for review
    assert vm["status"] == "accepted"       # existing behavior unchanged
