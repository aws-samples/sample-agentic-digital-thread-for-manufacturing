# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ingestion-time auto-accept + degeneracy drop.

No AWS, no Neptune, no network: the DynamoDB table is a tiny in-memory fake and
``accept_mapping`` is a spy. Covers:
  - ``_maybe_auto_accept`` fires ONLY when flag on + ai_confidence high + type in
    (value_mapping, identity_rule); never for field_annotation / medium; never
    when the flag is off;
  - an auto-accept failure is best-effort (never raises out of the propose path);
  - ``log_proposed_mapping`` drops a degenerate field_annotation (no store, returns
    None) and stores a real one; auto-accept is wired through the real logging path.
"""
from __future__ import annotations

import agents.self_learning as self_learning

_UUID = "12345678-1234-1234-1234-123456789abc"


class _FakeTable:
    """Minimal in-memory table: subject-key query returns no prior (→ insert)."""

    def __init__(self):
        self.items: list[dict] = []

    def query(self, **kwargs):
        idx = kwargs.get("IndexName")
        vals = kwargs.get("ExpressionAttributeValues", {})
        if idx == "subject-key-index":
            return {"Items": []}
        if idx == "status-index":
            return {"Items": [i for i in self.items if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self.items if i.get("mapping_id") == vals.get(":mid")]}

    def put_item(self, Item):
        self.items.append(dict(Item))

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **kwargs):
        """Apply simple SET clauses so the ADR-0051 P4 verified stamp is observable."""
        if Key is None:
            return
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        expr = UpdateExpression.strip()
        if not expr.upper().startswith("SET "):
            return
        for item in self.items:
            if item.get("mapping_id") == Key.get("mapping_id"):
                for clause in expr[4:].split(","):
                    lhs, rhs = (s.strip() for s in clause.split("="))
                    item[names.get(lhs, lhs)] = values.get(rhs)


def _spy(calls):
    def _accept(mapping_id):
        calls.append(mapping_id)
        return {"status": "accepted", "neptune_node_id": "n1"}
    return _accept


# --------------------------------------------------------------------------- #
# _maybe_auto_accept matrix (direct)
# --------------------------------------------------------------------------- #
def _item(ptype, **over):
    base = {"mapping_id": "m1", "proposed_type": ptype, "to_value": _UUID,
            "from_value": "x", "confidence": "0.9", "from_field": "a.b",
            "to_field": "c.d", "from_system": "sys-a", "to_system": "sys-b"}
    base.update(over)
    return base


def test_auto_accept_fires_for_high_value_mapping_when_flag_on(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    # ADR-0051 P4: a UUID-shaped target needs a passing verification — stub it
    # here so this test keeps pinning the flag/score path in isolation.
    monkeypatch.setattr(
        self_learning, "_verify_target",
        lambda item: item.update({"verified": "true",
                                  "verified_scope": "system"}) or True)
    self_learning._maybe_auto_accept("m1", _item("value_mapping"))
    assert calls == ["m1"]


def test_auto_accept_off_by_default(monkeypatch):
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    self_learning._maybe_auto_accept("m1", _item("value_mapping"))
    assert calls == []


def test_auto_accept_never_for_field_annotation(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "true")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    # field_annotation with a valid single-dot field scores 'medium' anyway, but the
    # type gate must exclude it regardless.
    self_learning._maybe_auto_accept("m1", _item("field_annotation", from_field="a.b"))
    assert calls == []


def test_auto_accept_never_for_medium_value_mapping(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "yes")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    # UUID but confidence below 0.85 → medium → must NOT auto-accept.
    self_learning._maybe_auto_accept("m1", _item("value_mapping", confidence="0.5"))
    assert calls == []


def test_auto_accept_never_for_identity_rule_medium(monkeypatch):
    # identity_rule always scores 'medium' — even though it is a permitted type,
    # the high-confidence gate means it is never auto-accepted.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "on")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    self_learning._maybe_auto_accept("m1", _item("identity_rule"))
    assert calls == []


def test_auto_accept_failure_is_best_effort(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")

    def _boom(mapping_id):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(self_learning, "accept_mapping", _boom)
    # Must NOT raise — best-effort contract.
    self_learning._maybe_auto_accept("m1", _item("value_mapping"))


# --------------------------------------------------------------------------- #
# log_proposed_mapping: degeneracy drop + auto-accept wiring
# --------------------------------------------------------------------------- #
def test_log_drops_degenerate_field_annotation(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    result = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.columns", from_value="",
        to_system="erp", to_field="orders.columns", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: these are the columns",
        proposed_type="field_annotation",
    )
    assert result is None
    assert fake.items == [], "degenerate field_annotation must not be stored"


def test_log_stores_real_field_annotation(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    result = self_learning.log_proposed_mapping(
        from_system="cmms", from_field="work_orders.status", from_value="",
        to_system="cmms", to_field="work_orders.status", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: Enum values OPEN/CLOSED — a type gotcha",
        proposed_type="field_annotation",
    )
    assert result is not None
    assert len(fake.items) == 1


def test_log_value_mapping_triggers_auto_accept_when_flag_on(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    # ADR-0051 P5: staging off — this test pins the auto-accept wiring, not
    # the co-reference buffer (covered by test_self_learning_lifecycle).
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    # ADR-0051 P4: UUID targets verify against the graph — return a matching node.
    monkeypatch.setattr(self_learning, "_neptune_query",
                        lambda c, p=None: [{"id": _UUID, "name": "cust-1",
                                            "system": "crm"}])
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.customer_id", from_value="cust-1",
        to_system="crm", to_field="customers.id", to_value=_UUID,
        concept="customer", confidence=0.9, query_context="join",
        proposed_type="value_mapping",
    )
    assert mid is not None
    assert calls == [mid], "high-confidence value_mapping should auto-accept the new id"
    stored = next(i for i in fake.items if i["mapping_id"] == mid)
    assert stored.get("verified") == "true"  # the passing check is stamped


# --------------------------------------------------------------------------- #
# ADR-0051 P4 hardening: positional guesses and unverified UUID targets
# --------------------------------------------------------------------------- #
def test_hierarchy_position_never_auto_accepts(monkeypatch):
    # The audited defect: a positional guess ("the Nth element matched") rode a
    # 0.95 confidence + UUID-shaped target into auto-accept. Never again —
    # method=hierarchy_position blocks BEFORE any verification, in both paths.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    monkeypatch.setattr(
        self_learning, "_verify_target",
        lambda item: item.update({"verified": "true",
                                  "verified_scope": "system"}) or True)
    item = _item("value_mapping", method="hierarchy_position", confidence="0.95")
    self_learning._maybe_auto_accept("m1", item)
    assert calls == []
    import agents.proposal_scoring as proposal_scoring
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [item])
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    assert self_learning.sweep_auto_accept() == {"accepted": 0, "failed": 0}
    assert calls == []


def test_uuid_target_without_existence_check_stays_pending(monkeypatch):
    # Existence verification returns no node → the proposal must stay pending
    # (never accept-on-doubt).
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    self_learning._maybe_auto_accept("m1", _item("value_mapping"))
    assert calls == []


def test_uuid_target_verification_error_stays_pending(monkeypatch):
    # A raising graph seam is doubt, not permission.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))

    def _boom(c, p=None):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(self_learning, "_neptune_query", _boom)
    self_learning._maybe_auto_accept("m1", _item("value_mapping"))
    assert calls == []


def test_uuid_target_name_mismatch_stays_pending(monkeypatch):
    # The node exists but its name contradicts the claimed source value —
    # name-consistency fails ⇒ pending.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    monkeypatch.setattr(self_learning, "_neptune_query",
                        lambda c, p=None: [{"id": _UUID, "name": "entirely-unrelated"}])
    self_learning._maybe_auto_accept(
        "m1", _item("value_mapping", from_value="station-7"))
    assert calls == []


def test_uuid_target_with_passing_check_auto_accepts(monkeypatch):
    # Flag-gated happy path: node exists AND the name is consistent → accept.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    monkeypatch.setattr(self_learning, "_neptune_query",
                        lambda c, p=None: [{"id": _UUID, "name": "Station-7 press",
                                            "system": "sys-b"}])
    self_learning._maybe_auto_accept(
        "m1", _item("value_mapping", from_value="station-7",
                    discovered_at="2026-07-24T00:00:00+00:00"))
    assert calls == ["m1"]


def test_recommendation_text_never_says_confirmed_without_check(monkeypatch):
    # Through the REAL propose path with a failing verification: the row stays
    # pending, carries NO verified stamp, and no stored text claims the target
    # was confirmed/verified — a claim requires an actual passing check.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    # ADR-0051 P5: staging off — this test pins the stored-text honesty rule.
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.customer_id", from_value="cust-1",
        to_system="crm", to_field="customers.id", to_value=_UUID,
        concept="customer", confidence=0.9, query_context="join",
        proposed_type="value_mapping",
    )
    stored = next(i for i in fake.items if i["mapping_id"] == mid)
    assert stored["status"] == "pending"
    assert "verified" not in stored
    for text_field in ("analysis", "query_context", "review_reason"):
        text = str(stored.get(text_field, "")).lower()
        assert "confirmed" not in text and "verified" not in text
