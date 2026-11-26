# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ingestion shape-gate: sentence-shaped identity_rule/join_path → insight (offline).

Proves ``log_proposed_mapping`` gate 3e: an ``identity_rule``/``join_path`` whose
``from_field`` or ``to_field`` fails ``proposal_scoring.is_table_column_shaped``
(a sentence / prose pattern, not a ``table.column`` reference) is RE-ROUTED to
``proposed_type='insight'`` — a narrative FINDING that never becomes a graph rule
— with the original narrative preserved in ``query_context``. Well-shaped rules
pass through unchanged, and ``value_mapping`` is never shape-gated.

No AWS / Neptune / LLM / network — the DynamoDB table is an in-memory
``FakeTable`` (``test_self_learning_schema_gate.py`` conventions). GENERIC —
synthetic system/field literals only.
"""
from __future__ import annotations

import pytest

import agents.schema_check as schema_check
import agents.self_learning as self_learning


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


_SENTENCE = "Operator IDs in MES correlate with ERP work orders"


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
            result = [i for i in items if i.get("subject_key") == values.get(":sk")]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values.get(":status")]
        else:
            result = [i for i in items if i.get("mapping_id") == values.get(":mid")]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def update_item(self, **kwargs) -> None:
        pass

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]


@pytest.fixture
def fake_table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


def _log(*, from_field, to_field, proposed_type, query_context="ctx",
         from_system="sys-a", to_system="sys-b"):
    return self_learning.log_proposed_mapping(
        from_system=from_system, from_field=from_field, from_value="",
        to_system=to_system, to_field=to_field, to_value="",
        concept="correlation", confidence=0.7, query_context=query_context,
        proposed_type=proposed_type,
    )


# ===========================================================================
# Sentence-shaped rules are RE-ROUTED to insight (never stored as rules)
# ===========================================================================
def test_sentence_identity_rule_rerouted_to_insight(fake_table):
    mid = _log(from_field=_SENTENCE, to_field=_SENTENCE,
               proposed_type="identity_rule")
    assert mid is not None
    items = fake_table.all_items()
    assert len(items) == 1
    item = items[0]
    assert item["proposed_type"] == "insight"
    assert item["from_field"] == ""
    assert item["to_field"] == ""
    assert item["subject_key"].startswith("insight|")


def test_sentence_join_path_to_field_rerouted_to_insight(fake_table):
    # Only ONE malformed endpoint is enough to re-route.
    mid = _log(from_field="work_orders.operator_id", to_field=_SENTENCE,
               proposed_type="join_path")
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["proposed_type"] == "insight"
    assert item["from_field"] == ""
    assert item["to_field"] == ""


def test_rerouted_insight_preserves_existing_query_context(fake_table):
    ctx = "Auto-analysis: operators correlate across MES and ERP"
    _log(from_field=_SENTENCE, to_field=_SENTENCE,
         proposed_type="identity_rule", query_context=ctx)
    item = fake_table.all_items()[0]
    assert item["query_context"] == ctx  # original narrative kept


def test_rerouted_insight_uses_malformed_text_when_no_context(fake_table):
    _log(from_field=_SENTENCE, to_field="orders", proposed_type="identity_rule",
         query_context="")
    item = fake_table.all_items()[0]
    # The malformed field text IS the finding when no context was provided.
    assert _SENTENCE in item["query_context"]


def test_rerouted_insight_analysis_says_never_a_graph_rule(fake_table):
    _log(from_field=_SENTENCE, to_field=_SENTENCE, proposed_type="identity_rule")
    item = fake_table.all_items()[0]
    assert "never becomes a graph rule" in item["analysis"]


def test_reroute_beats_schema_gate(fake_table, monkeypatch):
    # If the schema gate (3c) saw the sentence-shaped join_path, a definitive
    # False verdict would DROP it — the re-route must win (insight is not
    # schema-checked), preserving the finding.
    monkeypatch.setattr(schema_check, "column_exists", lambda s, f: False)
    mid = _log(from_field=_SENTENCE, to_field=_SENTENCE, proposed_type="join_path")
    assert mid is not None
    assert fake_table.all_items()[0]["proposed_type"] == "insight"


def test_reroute_beats_degenerate_join_drop_for_same_system_sentence(fake_table):
    # F5 REGRESSION: a SAME-SYSTEM sentence-shaped identity_rule looks like a
    # self-join (same system, identical "fields" on both sides) — if the
    # degenerate-join drop (3b) ran first it was silently LOST. The shape-gate
    # (3e) must run first, so the narrative becomes an insight instead.
    mid = _log(from_field=_SENTENCE, to_field=_SENTENCE,
               proposed_type="identity_rule",
               from_system="sys-a", to_system="sys-a")
    assert mid is not None
    items = fake_table.all_items()
    assert len(items) == 1
    item = items[0]
    assert item["proposed_type"] == "insight"
    assert item["from_field"] == ""
    assert item["to_field"] == ""


def test_well_shaped_same_system_self_join_is_still_dropped(fake_table):
    # Gate semantics preserved: a WELL-SHAPED same-system same-field
    # identity_rule is still a degenerate self-join → dropped at 3b.
    mid = _log(from_field="orders.operator_id", to_field="orders.operator_id",
               proposed_type="identity_rule",
               from_system="sys-a", to_system="sys-a")
    assert mid is None
    assert fake_table.all_items() == []


# ===========================================================================
# Well-shaped rules pass through unchanged (no regression)
# ===========================================================================
def test_valid_join_path_stored_unchanged(fake_table):
    mid = _log(from_field="work_orders.wo_id", to_field="events.work_order_id",
               proposed_type="join_path")
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["proposed_type"] == "join_path"
    assert item["from_field"] == "work_orders.wo_id"
    assert item["to_field"] == "events.work_order_id"


def test_valid_identity_rule_stored_unchanged(fake_table):
    mid = _log(from_field="orders.operator_id", to_field="shifts.operator_id",
               proposed_type="identity_rule")
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["proposed_type"] == "identity_rule"
    assert item["subject_key"].startswith("identity_rule|")


def test_value_mapping_is_not_shape_gated(fake_table):
    # Empty/dotless fields are legal for value_mapping — never re-routed.
    mid = self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="reason_code", from_value="131607000",
        to_system="sys-a", to_field="reason_desc", to_value="Feeder jam",
        concept="failure-mode", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is not None
    assert fake_table.all_items()[0]["proposed_type"] == "value_mapping"


# ===========================================================================
# Best-effort contract: a raising predicate must not break the propose path
# ===========================================================================
def test_shape_gate_failure_falls_through_to_storage(fake_table, monkeypatch):
    import agents.proposal_scoring as proposal_scoring

    def _boom(field):
        raise RuntimeError("predicate boom")

    monkeypatch.setattr(proposal_scoring, "is_table_column_shaped", _boom)
    mid = _log(from_field="work_orders.wo_id", to_field="events.work_order_id",
               proposed_type="join_path")
    assert mid is not None  # stored (as join_path) rather than crashing
    assert fake_table.all_items()[0]["proposed_type"] == "join_path"
