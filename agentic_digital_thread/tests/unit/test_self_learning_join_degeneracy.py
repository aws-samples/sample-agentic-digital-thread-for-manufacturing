# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Join-proposal degeneracy suppression + intuitive analysis wording (offline).

Proves the ITEM-1 fix to the self-learning analyzer:
  - ``log_proposed_mapping`` DROPS (returns ``None``, stores nothing) a degenerate
    join-like proposal — a same-system/same-field self-join, or one that names the
    knowledge-graph target itself — so the analyzer can never emit a "JoinPath
    between X and X" or call a same-graph traversal a "cross-system join".
  - A GENUINE cross-system join still stores.
  - A same-system reason-code → label value_mapping (different/empty target value)
    still stores (not mistaken for a self-join).
  - The stored ``analysis`` text is intuitive + accurate: a genuine cross-system
    join reads "cross-system join"; a self-referential/graph proposal is never
    stored (and so never worded confusingly).

No AWS / Neptune / LLM / network — the DynamoDB table is an in-memory ``FakeTable``
wired via ``monkeypatch``. GENERIC — synthetic system/field/value literals only.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
from agents.self_learning import _proposal_analysis


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)



# --------------------------------------------------------------------------- #
# In-memory table double (put + subject-key query is all log_proposed_mapping needs)
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]


@pytest.fixture
def fake_table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


# ===========================================================================
# Degenerate join proposals are DROPPED at the source (nothing stored)
# ===========================================================================
def test_self_referential_same_field_join_is_dropped(fake_table):
    """The live bug: a JoinPath between a field and ITSELF within one system must
    NOT be stored (returns None)."""
    mid = self_learning.log_proposed_mapping(
        from_system="digital-thread", from_field="Asset.path", from_value="p1",
        to_system="digital-thread", to_field="Asset.path", to_value="p1",
        concept="asset", confidence=0.85, query_context="ctx",
        proposed_type="join_path",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_join_naming_knowledge_graph_is_dropped(fake_table):
    """A "cross-system join" naming the knowledge graph itself on either side is a
    same-graph traversal, not a cross-system join — dropped."""
    mid = self_learning.log_proposed_mapping(
        from_system="Digital Thread", from_field="Asset.path", from_value="p1",
        to_system="erp", to_field="orders.asset_id", to_value="p1",
        concept="asset", confidence=0.85, query_context="ctx",
        proposed_type="join_path",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_genuine_cross_system_join_is_stored(fake_table):
    """A real erp → cmms join (distinct data systems) still stores, and its analysis
    reads as a genuine cross-system join."""
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="work_orders.asset_id", from_value="u-1",
        to_system="cmms", to_field="work_orders.asset_id", to_value="u-1",
        concept="asset-id", confidence=0.85, query_context="ctx",
        proposed_type="join_path", tool_calls_count=4,
    )
    assert mid is not None
    items = fake_table.all_items()
    assert len(items) == 1
    analysis = items[0]["analysis"]
    assert "cross-system join" in analysis
    # Never the old self-referential phrasing.
    assert "between X and X" not in analysis
    assert "erp" in analysis and "cmms" in analysis


def test_same_system_reason_code_value_mapping_is_stored(fake_table):
    """A same-system code→label value_mapping (empty/different target value) is NOT
    a self-join and must still store."""
    mid = self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="reason_code", from_value="131607000",
        to_system="sys-a", to_field="reason_code", to_value="",
        concept="failure-mode", confidence=0.3, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is not None
    assert len(fake_table.all_items()) == 1


def test_value_mapping_value_to_itself_is_dropped(fake_table):
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.state", from_value="OPEN",
        to_system="erp", to_field="orders.state", to_value="OPEN",
        concept="state", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is None
    assert fake_table.all_items() == []


# ===========================================================================
# _proposal_analysis wording — intuitive + accurate, no self-referential phrasing
# ===========================================================================
class TestAnalysisWording:
    def test_cross_system_join_wording(self):
        text = _proposal_analysis(
            proposed_type="join_path", from_system="erp", from_field="orders.id",
            from_value="u-1", to_system="cmms", to_field="work_orders.order_id",
            to_value="u-1", concept="order", tool_calls_count=3,
        )
        assert "cross-system join" in text
        assert "erp.id" in text and "cmms.order_id" in text
        assert "tool call(s)" in text

    def test_value_mapping_wording_with_target(self):
        text = _proposal_analysis(
            proposed_type="value_mapping", from_system="erp", from_field="orders.name",
            from_value="Widget", to_system="cmms", to_field="asset.name",
            to_value="a-9", concept="asset", tool_calls_count=0,
        )
        assert "resolve this identifier directly" in text
        assert "'Widget'" in text and "'a-9'" in text

    def test_field_annotation_wording(self):
        text = _proposal_analysis(
            proposed_type="field_annotation", from_system="cmms",
            from_field="work_orders.status", from_value="", to_system="",
            to_field="", to_value="", concept="", tool_calls_count=0,
        )
        assert "what this column means" in text
        assert "cmms.work_orders.status" in text
