# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``steward_question`` can NEVER auto-accept, and its accept is
acknowledge-only — zero graph writes, ever (ADR-0059 D3, offline).

Extends the ``test_insight_never_auto_accepted.py`` deny-tuple pins to the new
grain: the deny check runs BEFORE the ``_AUTO_ACCEPT_TYPES`` allowlist in BOTH
``_maybe_auto_accept`` and ``sweep_auto_accept``, so a future edit adding
``steward_question`` to the allowlist cannot silently admit it. Also pins the
acknowledge-only accept contract (``_accept_steward_question`` — the
``_accept_coverage_gap`` pattern): accepting means "seen", never a graph rule;
the result carries ``acknowledged=True`` and NO ``neptune_node_id`` (honest
absence — there is no applied artifact).

No AWS / Neptune / LLM / network — seams monkeypatched per
``test_insight_never_auto_accepted.py`` conventions.
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
def test_deny_tuple_contains_steward_question():
    assert "steward_question" in self_learning._NEVER_AUTO_ACCEPT_TYPES
    assert "steward_question" not in self_learning._AUTO_ACCEPT_TYPES
    # Deny and allow stay disjoint — no type can be both.
    assert not set(self_learning._NEVER_AUTO_ACCEPT_TYPES) & set(
        self_learning._AUTO_ACCEPT_TYPES)


def test_steward_question_is_acknowledge_only_dispatch():
    assert (self_learning._ACKNOWLEDGE_ONLY_ACCEPTS["steward_question"]
            is self_learning._accept_steward_question)


# ===========================================================================
# sweep_auto_accept never accepts a steward_question — even scored high
# ===========================================================================
def test_sweep_never_accepts_high_steward_question(monkeypatch):
    pending = [
        _mapping("sq-high", "steward_question", "high"),  # denied (type)
        _mapping("vm-high", "value_mapping", "high"),     # accepted (existing behavior)
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
    # FUTURE-SAFETY: even if someone adds 'steward_question' to the allowlist,
    # the deny-tuple must still exclude it.
    monkeypatch.setattr(self_learning, "_AUTO_ACCEPT_TYPES",
                        ("value_mapping", "identity_rule", "steward_question"))
    monkeypatch.setattr(self_learning, "get_pending_mappings",
                        lambda: [_mapping("sq-high", "steward_question", "high")])
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: {"status": "accepted"})
    monkeypatch.setattr(proposal_scoring, "score_proposal", _score_by_marker)

    assert self_learning.sweep_auto_accept() == {"accepted": 0, "failed": 0}


# ===========================================================================
# _maybe_auto_accept never accepts a steward_question — flag on, scored high
# ===========================================================================
def test_maybe_auto_accept_never_accepts_steward_question(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: calls.append(mid) or {"status": "accepted"})

    self_learning._maybe_auto_accept("m1", {"mapping_id": "m1",
                                            "proposed_type": "steward_question"})
    assert calls == []


def test_maybe_auto_accept_deny_beats_a_future_allowlist_edit(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(self_learning, "_AUTO_ACCEPT_TYPES",
                        ("value_mapping", "identity_rule", "steward_question"))
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: calls.append(mid) or {"status": "accepted"})

    self_learning._maybe_auto_accept("m1", {"mapping_id": "m1",
                                            "proposed_type": "steward_question"})
    assert calls == []


# ===========================================================================
# Acknowledge-only accept — ZERO graph writes; no neptune_node_id claimed
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


def _log_question(fake_table):
    """File a steward_question through the REAL propose path."""
    return self_learning.log_proposed_mapping(
        from_system="sys-a", from_field="", from_value="",
        to_system="", to_field="", to_value="",
        concept="work-order", confidence=0.7,
        query_context=("Queries joining two systems repeatedly return no rows "
                       "when matching on this identifier. Is there a "
                       "translation between these identifiers?"),
        proposed_type="steward_question", reason="terminology_miss",
    )


def test_accept_steward_question_writes_nothing_to_neptune(monkeypatch):
    fake = _FakeTable()
    neptune_calls: list[tuple] = []
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda c, p=None: neptune_calls.append((c, p)) or True)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)

    mid = _log_question(fake)
    assert mid is not None

    result = self_learning.accept_mapping(mid, actor="steward@example.com")

    assert result["status"] == "accepted"
    assert result["acknowledged"] is True
    assert result["proposed_type"] == "steward_question"
    assert "neptune_node_id" not in result  # honest absence — no applied artifact
    assert neptune_calls == []              # zero graph writes — EVER
    stored = next(i for i in fake.items if i["mapping_id"] == mid)
    assert stored["status"] == "accepted"
    assert stored["accepted_by"] == "steward@example.com"


def test_logged_steward_question_stays_pending_under_auto_accept_flag(monkeypatch):
    # End-to-end through the REAL propose path: flag on + scorer forced high —
    # a freshly-logged steward_question must be left pending.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    fake = _FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    neptune_calls: list[tuple] = []
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda c, p=None: neptune_calls.append((c, p)) or True)

    mid = _log_question(fake)
    assert mid is not None
    stored = next(i for i in fake.items if i["mapping_id"] == mid)
    assert stored["status"] == "pending"
    assert neptune_calls == []
