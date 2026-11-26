# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lifecycle discipline for the ``user-feedback`` proposal source.

``user-feedback`` rows come from an UNTRUSTED-INPUT path (free-text chat
feedback, ADR-0054): they must NEVER be auto-accepted — ``insight`` already
sits in ``_NEVER_AUTO_ACCEPT_TYPES``, and ``_NEVER_AUTO_ACCEPT_SOURCES``
backs it as defense-in-depth in BOTH auto-accept paths regardless of type.
Machine-authored reason text from this source is phrasing-normalized like any
other machine source (``source != 'user'``).

The source deny-tuple now also carries ``"answer-audit"`` (ADR-0062 D6) — its
behavioral pin lives in ``test_answer_audit_auto_accept.py``; this file stays
scoped to ``user-feedback`` and asserts only that its own entry survives.

Offline — the REAL ``self_learning`` functions run against the in-memory
``FakeTable`` (the ``test_self_learning_provenance_phrasing.py`` idiom).
Pins kill plan mutations M19 (normalization skipped for user-feedback), M20
(``_maybe_auto_accept`` accepts user-feedback), and M21 (sweep accepts).
Each deny test carries a POSITIVE control proving the test can fail.
"""
from __future__ import annotations

import agents.proposal_scoring as proposal_scoring
import agents.self_learning as self_learning

_UUID = "12345678-1234-1234-1234-123456789abc"


class FakeTable:
    """In-memory stand-in for the proposals table (lifecycle-test idiom)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

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
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def scan(self, **kwargs) -> dict:
        return {"Items": self.all_items()}


def _install(monkeypatch, table: FakeTable) -> None:
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", lambda *a, **k: True)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


# --------------------------------------------------------------------------- #
# M19 — reason phrasing normalized for source='user-feedback'
# --------------------------------------------------------------------------- #
def test_user_feedback_reason_is_phrasing_normalized(monkeypatch):
    """Kills: exempting user-feedback from the machine-phrasing normalizer
    (only source == 'user' — the steward — is exempt, ADR-0054)."""
    table = FakeTable()
    _install(monkeypatch, table)
    mapping_id = self_learning.log_proposed_mapping(
        "sys-a", "", "", "sys-b", "", "",
        "user-feedback", 0.3,
        "User-feedback exploration: which lots were affected?",
        proposed_type="insight", source="user-feedback",
        method="feedback-exploration", provenance="feedback-exploration",
        reason="Routing known at site9 from prior sessions diverged.")
    assert mapping_id is not None
    rows = [i for i in table.all_items() if i["mapping_id"] == mapping_id]
    assert len(rows) == 1
    assert rows[0]["reason"] == (
        "Routing known at site9 from earlier learned mappings diverged.")
    # never pre-accepted at write: the row lands PENDING with its source
    assert rows[0]["status"] == "pending"
    assert rows[0]["source"] == "user-feedback"
    assert rows[0]["provenance"] == "feedback-exploration"


# --------------------------------------------------------------------------- #
# M20 — _maybe_auto_accept denies the user-feedback source
# --------------------------------------------------------------------------- #
def _item(**over) -> dict:
    """A shape that WOULD auto-accept (high-confidence value_mapping)."""
    base = {"mapping_id": "m1", "proposed_type": "value_mapping",
            "to_value": _UUID, "from_value": "x", "confidence": "0.9",
            "from_field": "a.b", "to_field": "c.d"}
    base.update(over)
    return base


def _spy(calls):
    def _accept(mapping_id):
        calls.append(mapping_id)
        return {"status": "accepted", "neptune_node_id": "n1"}
    return _accept


def test_maybe_auto_accept_denies_user_feedback_source(monkeypatch):
    """Kills: removing the source deny from _maybe_auto_accept."""
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    calls: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(calls))
    def _verify(item):
        item.update({"verified": "true", "target_verified": "true",
                     "verified_scope": "system"})
        return True

    monkeypatch.setattr(self_learning, "_verify_target", _verify)
    # positive control: WITHOUT the source, this exact item auto-accepts
    self_learning._maybe_auto_accept("m1", _item(source="agent"))
    assert calls == ["m1"]
    # the SAME item from the untrusted user-feedback source never does
    self_learning._maybe_auto_accept("m2", _item(mapping_id="m2",
                                                 source="user-feedback"))
    assert calls == ["m1"]


# --------------------------------------------------------------------------- #
# M21 — sweep_auto_accept skips the user-feedback source
# --------------------------------------------------------------------------- #
def test_sweep_auto_accept_skips_user_feedback_source(monkeypatch):
    """Kills: removing the source deny from sweep_auto_accept."""
    pending = [
        _item(mapping_id="vm-agent", source="agent"),
        _item(mapping_id="vm-feedback", source="user-feedback"),
    ]
    accepted: list[str] = []
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _spy(accepted))
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    monkeypatch.setattr(self_learning, "_auto_accept_block_reason",
                        lambda m: "")
    result = self_learning.sweep_auto_accept()
    assert accepted == ["vm-agent"]  # positive control included
    assert result == {"accepted": 1, "failed": 0}
    # the deliberate tuple edit (ADR-0062 D6) never drops this entry
    assert "user-feedback" in self_learning._NEVER_AUTO_ACCEPT_SOURCES
