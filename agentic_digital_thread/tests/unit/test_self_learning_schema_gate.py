# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Upstream schema-existence gate for self-learning proposals (offline).

Proves the ROBUST fix for the "blocked" review-queue problem: a proposal that
references a ``table.column`` the onboarded schema DEFINITIVELY does not contain is
REJECTED at CREATION (``log_proposed_mapping`` step 3c) — it never enters the
reviewable queue — while an INDETERMINATE verdict (schema unavailable / ambiguous →
``None``) is KEPT (degrade gracefully, never reject on uncertainty), and a verified
column passes through unchanged.

The schema-existence check is performed at ingestion time by calling
``schema_check.proposal_missing_field`` for SCHEMA_CHECKED_TYPES (field_annotation,
value_mapping, join_path). This replaces the earlier reconcile-based cleanup approach.

No AWS / Neptune / LLM / network — the DynamoDB table is an in-memory ``FakeTable``
and the schema verdict is a monkeypatched ``schema_check.column_exists``. GENERIC —
synthetic system/field/value literals only.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
import agents.schema_check as schema_check


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)

from agents.schema_check import proposal_missing_field, SCHEMA_CHECKED_TYPES


# --------------------------------------------------------------------------- #
# In-memory DynamoDB double: put + subject-key query + status query + scan + delete
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def delete_item(self, Key: dict) -> None:
        self._items.pop((Key["mapping_id"], Key["discovered_at"]), None)

    def scan(self, **kwargs) -> dict:
        return {"Items": [dict(v) for v in self._items.values()]}

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

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]


@pytest.fixture
def fake_table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


def _set_verdicts(monkeypatch, verdicts: dict[str, bool | None]):
    """Monkeypatch ``schema_check.column_exists`` to a deterministic lookup keyed on
    ``"system.field"`` → ``True``/``False``/``None`` (default ``None`` = unavailable).
    ``proposal_missing_field`` reads the module-global ``column_exists``, so patching
    it here drives both the ingestion gate and reconcile."""
    def fake_column_exists(system_id: str, field: str):
        return verdicts.get(f"{system_id}.{field}", None)
    monkeypatch.setattr(schema_check, "column_exists", fake_column_exists)


# ===========================================================================
# proposal_missing_field — the pure decision function
# ===========================================================================
def test_proposal_missing_field_reports_definitive_false(monkeypatch):
    _set_verdicts(monkeypatch, {"erp.work_orders.status": False})
    assert proposal_missing_field(
        "field_annotation", "erp", "work_orders.status", "erp", "work_orders.status"
    ) == "erp.work_orders.status"


def test_proposal_missing_field_none_when_verified(monkeypatch):
    _set_verdicts(monkeypatch, {"erp.orders.id": True, "cmms.wo.order_id": True})
    assert proposal_missing_field(
        "value_mapping", "erp", "orders.id", "cmms", "wo.order_id"
    ) is None


def test_proposal_missing_field_none_on_indeterminate(monkeypatch):
    _set_verdicts(monkeypatch, {})  # everything unknown → None
    assert proposal_missing_field(
        "join_path", "erp", "orders.id", "cmms", "wo.order_id"
    ) is None


def test_proposal_missing_field_not_gated_for_non_checked_types(monkeypatch):
    # identity_rule (correlation) carries a pattern in its field slot — NEVER gated,
    # even if the verdict would be False.
    _set_verdicts(monkeypatch, {"erp.some pattern text.x": False})
    for ptype in ("identity_rule", "crosswalk", "concept_mapping"):
        assert ptype not in SCHEMA_CHECKED_TYPES
        assert proposal_missing_field(
            ptype, "erp", "some pattern text", "cmms", "other pattern"
        ) is None


def test_proposal_missing_field_reports_to_endpoint(monkeypatch):
    # from exists, to does not → the to endpoint is reported (a mapping to a
    # nonexistent target column is equally unusable).
    _set_verdicts(monkeypatch, {
        "erp.orders.id": True, "cmms.wo.bogus_col": False,
    })
    assert proposal_missing_field(
        "value_mapping", "erp", "orders.id", "cmms", "wo.bogus_col"
    ) == "cmms.wo.bogus_col"


# ===========================================================================
# Ingestion gate (log_proposed_mapping step 3c): reject definitive-False at CREATION
# ===========================================================================
def test_value_mapping_nonexistent_column_rejected_at_ingestion(fake_table, monkeypatch):
    _set_verdicts(monkeypatch, {"cmms.work_orders.asset_name": False})
    mid = self_learning.log_proposed_mapping(
        from_system="cmms", from_field="work_orders.asset_name", from_value="Line-3",
        to_system="mes-cr", to_field="production_events.line", to_value="L3",
        concept="equipment-id", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_field_annotation_nonexistent_column_rejected_at_ingestion(fake_table, monkeypatch):
    # A GUESSED column from the agent's COLUMN_NOT_FOUND recovery loop — the #1 source
    # of blocked items. Annotation is non-degenerate so only the schema gate can drop.
    _set_verdicts(monkeypatch, {"erp.work_orders.status": False})
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="work_orders.status", from_value="",
        to_system="erp", to_field="work_orders.status", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: tracks the lifecycle stage of each order",
        proposed_type="field_annotation",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_join_path_nonexistent_to_column_rejected_at_ingestion(fake_table, monkeypatch):
    _set_verdicts(monkeypatch, {
        "erp.work_orders.asset_id": True, "cmms.work_orders.bogus_id": False,
    })
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="work_orders.asset_id", from_value="u-1",
        to_system="cmms", to_field="work_orders.bogus_id", to_value="u-1",
        concept="asset-id", confidence=0.85, query_context="ctx",
        proposed_type="join_path",
    )
    assert mid is None
    assert fake_table.all_items() == []


# ===========================================================================
# Degrade gracefully: an INDETERMINATE (None) verdict KEEPS the proposal
# ===========================================================================
def test_unknown_verdict_keeps_proposal(fake_table, monkeypatch):
    _set_verdicts(monkeypatch, {})  # schema unavailable → None everywhere
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="work_orders.asset_id", from_value="u-1",
        to_system="cmms", to_field="work_orders.asset_id", to_value="u-1",
        concept="asset-id", confidence=0.85, query_context="ctx",
        proposed_type="join_path",
    )
    assert mid is not None
    assert len(fake_table.all_items()) == 1


def test_verified_column_passes_ingestion(fake_table, monkeypatch):
    _set_verdicts(monkeypatch, {
        "erp.orders.customer_id": True, "cmms.assets.customer_id": True,
    })
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.customer_id", from_value="C-1",
        to_system="cmms", to_field="assets.customer_id", to_value="C-1",
        concept="customer-id", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is not None
    assert len(fake_table.all_items()) == 1


def test_correlation_identity_rule_not_schema_gated(fake_table, monkeypatch):
    # identity_rule (correlation) from_field is a natural-language pattern; even a
    # False verdict on that string must NOT drop it — it is not schema-checked.
    _set_verdicts(monkeypatch, {"cmms.downtime correlates with quality.x": False})
    mid = self_learning.log_proposed_mapping(
        from_system="cmms", from_field="downtime correlates with quality drops",
        from_value="",
        to_system="mes-mil", to_field="downtime correlates with quality drops",
        to_value="",
        concept="correlation", confidence=0.7, query_context="ctx",
        proposed_type="identity_rule",
    )
    assert mid is not None
    assert len(fake_table.all_items()) == 1


# ===========================================================================
# Schema gate at ingestion — blocked proposals can NEVER enter the system
# ===========================================================================
# The reconcile approach was replaced by an upfront gate: when a proposal is logged,
# it is checked against the schema. If the column DEFINITIVELY doesn't exist (False
# verdict), the proposal is dropped immediately — it never reaches the review queue.
# This guarantees the safety property: a nonexistent-column proposal can never be
# accepted, because it can never be created in the first place.

def test_schema_gate_blocks_value_mapping_with_missing_column(fake_table, monkeypatch):
    """A value_mapping referencing a nonexistent column is dropped at ingestion."""
    _set_verdicts(monkeypatch, {"erp.orders.missing_field": False})
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.missing_field", from_value="",
        to_system="erp", to_field="orders.missing_field", to_value="",
        concept="test-concept", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_schema_gate_blocks_field_annotation_with_missing_column(fake_table, monkeypatch):
    """A field_annotation referencing a nonexistent column is dropped at ingestion."""
    _set_verdicts(monkeypatch, {"mes.events.fake_column": False})
    mid = self_learning.log_proposed_mapping(
        from_system="mes", from_field="events.fake_column", from_value="",
        to_system="mes", to_field="events.fake_column", to_value="",
        concept="annotation", confidence=0.9, query_context="Auto-analysis: test",
        proposed_type="field_annotation",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_schema_gate_blocks_join_path_with_missing_to_column(fake_table, monkeypatch):
    """A join_path with a missing 'to' column is dropped at ingestion."""
    _set_verdicts(monkeypatch, {
        "erp.orders.id": True, "cmms.assets.nonexistent_id": False,
    })
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.id", from_value="",
        to_system="cmms", to_field="assets.nonexistent_id", to_value="",
        concept="join-id", confidence=0.85, query_context="ctx",
        proposed_type="join_path",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_schema_gate_allows_verified_columns(fake_table, monkeypatch):
    """A proposal with verified columns passes through."""
    _set_verdicts(monkeypatch, {
        "erp.orders.id": True, "cmms.assets.order_id": True,
    })
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.id", from_value="",
        to_system="cmms", to_field="assets.order_id", to_value="",
        concept="order-id", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is not None
    assert len(fake_table.all_items()) == 1


# ===========================================================================
# proposal_missing_field — preloaded schema CACHE path (pure, no graph I/O)
# ===========================================================================
# The bulk caller (reconcile_blocked_proposals) passes a preloaded
# ``{system_id: schema|None}`` cache so a large backlog is verified against ONE
# batch of schema loads instead of an N+1 of graph reads (the reconcile timeout bug).
def test_proposal_missing_field_cache_verified_column():
    schemas = {"erp": {"orders": {"columns": ["id"]}}}
    assert proposal_missing_field(
        "field_annotation", "erp", "orders.id", "erp", "orders.id", schemas=schemas
    ) is None


def test_proposal_missing_field_cache_definitive_miss():
    # single-dot column absent from a present table → definitive False → miss
    schemas = {"erp": {"orders": {"columns": ["id"]}}}
    assert proposal_missing_field(
        "field_annotation", "erp", "orders.missing_col", "erp", "orders.missing_col",
        schemas=schemas,
    ) == "erp.orders.missing_col"


def test_proposal_missing_field_cache_table_absent_fallback():
    # ambiguous multi-dot field whose leading table is absent → column verdict is
    # indeterminate (None) but the table definitively does not exist → miss (the
    # table_exists fallback resolved purely against the cache).
    schemas = {"erp": {"orders": {"columns": ["id"]}}}
    assert proposal_missing_field(
        "field_annotation", "erp", "ghosttable.a.b", "erp", "ghosttable.a.b",
        schemas=schemas,
    ) == "erp.ghosttable.a.b"


def test_proposal_missing_field_cache_indeterminate_when_schema_absent():
    # system not in the cache → schema None → indeterminate → NOT a miss (degrade
    # gracefully, never reject on uncertainty).
    assert proposal_missing_field(
        "field_annotation", "ghost", "t.c", "ghost", "t.c", schemas={}
    ) is None


# ===========================================================================
# reconcile_blocked_proposals — bulk cleanup of legacy pre-gate items, using a
# ONE-TIME preloaded schema cache (no per-item N+1 of graph reads → no timeout).
# ===========================================================================
def _put_raw(fake, mapping_id, proposed_type, from_system, from_field,
             status="pending", to_system=None, to_field=None):
    """Insert an item DIRECTLY (bypassing the ingestion gate) to simulate a legacy
    proposal that predates the gate."""
    fake.put_item(Item={
        "mapping_id": mapping_id,
        "discovered_at": f"2026-01-01T00:00:00Z#{mapping_id}",
        "status": status,
        "proposed_type": proposed_type,
        "from_system": from_system,
        "from_field": from_field,
        "to_system": to_system if to_system is not None else from_system,
        "to_field": to_field if to_field is not None else from_field,
    })


def test_reconcile_purges_definitive_miss_keeps_verified_indeterminate_accepted(
    fake_table, monkeypatch
):
    schemas = {"erp": {"orders": {"columns": ["id"]}}}
    monkeypatch.setattr(
        schema_check, "load_system_schema", lambda sid: schemas.get(sid)
    )

    _put_raw(fake_table, "m-miss", "field_annotation", "erp", "orders.missing_col")
    _put_raw(fake_table, "m-verif", "field_annotation", "erp", "orders.id")
    _put_raw(fake_table, "m-indet", "field_annotation", "ghost", "t.c")
    _put_raw(fake_table, "m-accpt", "field_annotation", "erp", "orders.gone",
             status="accepted")

    result = self_learning.reconcile_blocked_proposals()

    assert "error" not in result
    assert result["deleted"] == 1                      # only the definitive miss
    assert result["checked"] == 3                       # pending only; accepted not scanned
    remaining = {i["mapping_id"] for i in fake_table.all_items()}
    assert remaining == {"m-verif", "m-indet", "m-accpt"}


def test_reconcile_no_op_when_all_valid(fake_table, monkeypatch):
    schemas = {"erp": {"orders": {"columns": ["id", "customer_id"]}}}
    monkeypatch.setattr(
        schema_check, "load_system_schema", lambda sid: schemas.get(sid)
    )
    _put_raw(fake_table, "m-ok", "value_mapping", "erp", "orders.id",
             to_system="erp", to_field="orders.customer_id")
    result = self_learning.reconcile_blocked_proposals()
    assert result == {"deleted": 0, "checked": 1}
    assert len(fake_table.all_items()) == 1


def test_reconcile_loads_each_schema_once_no_n_plus_1(fake_table, monkeypatch):
    """The whole point of the fix: over a backlog of N items referencing the same
    system, the schema is loaded ONCE (bounded by distinct systems), not per item."""
    schemas = {"erp": {"orders": {"columns": ["id"]}}}
    calls: list[str] = []

    def _counting_load(sid):
        calls.append(sid)
        return schemas.get(sid)

    monkeypatch.setattr(schema_check, "load_system_schema", _counting_load)
    for n in range(25):
        _put_raw(fake_table, f"m-{n}", "field_annotation", "erp", "orders.id")

    result = self_learning.reconcile_blocked_proposals()

    assert "error" not in result and result["checked"] == 25
    assert calls == ["erp"]                             # exactly one load, not 25+
