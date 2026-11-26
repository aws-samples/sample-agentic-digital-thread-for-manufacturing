# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``insight`` is structurally excluded from auto-accept, and its accept path
never writes a graph rule (offline).

Pins the ``_NEVER_AUTO_ACCEPT_TYPES`` deny-tuple contract: the deny check runs
BEFORE the ``_AUTO_ACCEPT_TYPES`` allowlist in BOTH ``_maybe_auto_accept`` and
``sweep_auto_accept``, so a future edit adding a denied type to the allowlist
cannot silently admit it. Also proves the "insights are findings, never graph
rules" invariant: ``accept_mapping`` on an insight issues ZERO Neptune writes
and stamps an ``insight-`` applied-node id.

No AWS / Neptune / LLM / network — seams monkeypatched per
``test_sweep_auto_accept.py`` / ``test_self_learning_auto_accept.py``
conventions.
"""
from __future__ import annotations

import agents.proposal_scoring as proposal_scoring
import agents.self_learning as self_learning


def _mapping(mapping_id, ptype, conf="high"):
    return {"mapping_id": mapping_id, "proposed_type": ptype, "_conf": conf}


def _score_by_marker(mapping):
    return {"ai_confidence": mapping.get("_conf", "low")}


# ===========================================================================
# Deny-tuple contract
# ===========================================================================
def test_deny_tuple_contains_insight_and_field_annotation():
    for ptype in ("insight", "field_annotation", "crosswalk",
                  "field_concept_mint", "concept_mapping"):
        assert ptype in self_learning._NEVER_AUTO_ACCEPT_TYPES
    # The allowlist semantics are unchanged.
    assert self_learning._AUTO_ACCEPT_TYPES == ("value_mapping", "identity_rule")
    # Deny and allow are disjoint — no type can be both.
    assert not set(self_learning._NEVER_AUTO_ACCEPT_TYPES) & set(
        self_learning._AUTO_ACCEPT_TYPES)


# ===========================================================================
# sweep_auto_accept never accepts an insight — even scored high
# ===========================================================================
def test_sweep_never_accepts_high_insight(monkeypatch):
    pending = [
        _mapping("ins-high", "insight", "high"),        # denied (type)
        _mapping("vm-high", "value_mapping", "high"),   # accepted (existing behavior)
    ]
    accepted_ids: list[str] = []

    def _accept(mid):
        accepted_ids.append(mid)
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)
    monkeypatch.setattr(proposal_scoring, "score_proposal", _score_by_marker)

    result = self_learning.sweep_auto_accept()

    assert result == {"accepted": 1, "failed": 0}
    assert accepted_ids == ["vm-high"]


def test_sweep_deny_beats_a_future_allowlist_edit(monkeypatch):
    # FUTURE-SAFETY: even if someone adds 'insight' to the allowlist, the
    # deny-tuple must still exclude it.
    monkeypatch.setattr(self_learning, "_AUTO_ACCEPT_TYPES",
                        ("value_mapping", "identity_rule", "insight"))
    monkeypatch.setattr(self_learning, "get_pending_mappings",
                        lambda: [_mapping("ins-high", "insight", "high")])
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: {"status": "accepted"})
    monkeypatch.setattr(proposal_scoring, "score_proposal", _score_by_marker)

    assert self_learning.sweep_auto_accept() == {"accepted": 0, "failed": 0}


# ===========================================================================
# _maybe_auto_accept never accepts an insight — flag on, scored high
# ===========================================================================
def test_maybe_auto_accept_never_accepts_insight(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: calls.append(mid) or {"status": "accepted"})

    self_learning._maybe_auto_accept("m1", {"mapping_id": "m1",
                                            "proposed_type": "insight"})
    assert calls == []


def test_maybe_auto_accept_deny_beats_a_future_allowlist_edit(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(self_learning, "_AUTO_ACCEPT_TYPES",
                        ("value_mapping", "identity_rule", "insight"))
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: calls.append(mid) or {"status": "accepted"})

    self_learning._maybe_auto_accept("m1", {"mapping_id": "m1",
                                            "proposed_type": "insight"})
    assert calls == []


# ===========================================================================
# Accepting an insight writes NOTHING to the graph (explicit no-op branch)
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


def test_accept_insight_writes_nothing_to_neptune(monkeypatch):
    fake = _FakeTable()
    neptune_calls: list[tuple] = []
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda c, p=None: neptune_calls.append((c, p)) or True)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)

    mid = self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="", from_value="",
        to_system="", to_field="", to_value="",
        concept="correlation", confidence=0.7,
        query_context="Auto-analysis: downtime spikes precede quality drops",
        proposed_type="insight",
    )
    assert mid is not None

    result = self_learning.accept_mapping(mid)

    assert result["status"] == "accepted"
    assert result["neptune_node_id"].startswith("insight-")
    assert neptune_calls == []  # zero graph writes — findings are never rules
    stored = next(i for i in fake.items if i["mapping_id"] == mid)
    assert stored["status"] == "accepted"


def test_logged_insight_stays_pending_under_auto_accept_flag(monkeypatch):
    # End-to-end through the REAL propose path: flag on + scorer forced high —
    # a freshly-logged insight must be left pending (never auto-accepted).
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    neptune_calls: list[tuple] = []
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda c, p=None: neptune_calls.append((c, p)) or True)

    mid = self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="", from_value="",
        to_system="", to_field="", to_value="",
        concept="correlation", confidence=0.7,
        query_context="Auto-analysis: downtime spikes precede quality drops",
        proposed_type="insight",
    )
    assert mid is not None
    stored = next(i for i in fake.items if i["mapping_id"] == mid)
    assert stored["status"] == "pending"
    assert neptune_calls == []
