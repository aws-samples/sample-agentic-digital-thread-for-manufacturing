# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the ONE shared high-confidence sweep.

``self_learning.sweep_auto_accept`` is the single source of truth reused by the
accept-high-confidence route, the periodic scheduler safety-net, and the
post-/api/ask background hook. No AWS/Neptune/network: ``get_pending_mappings``,
``accept_mapping`` and the shared ``score_proposal`` are all mocked so the
type/confidence gate and the best-effort failure counting are pinned exactly.
"""
from __future__ import annotations

import agents.proposal_scoring as proposal_scoring
import agents.self_learning as self_learning


def _mapping(mapping_id, ptype, conf):
    """A synthetic proposal carrying its intended scorer verdict in ``_conf``."""
    return {"mapping_id": mapping_id, "proposed_type": ptype, "_conf": conf}


def _score_by_marker(mapping):
    """Mock scorer: read the intended ai_confidence straight off the fixture."""
    return {"ai_confidence": mapping.get("_conf", "low")}


def test_sweep_accepts_only_high_value_mapping_and_identity_rule(monkeypatch):
    pending = [
        _mapping("vm-high", "value_mapping", "high"),      # accepted
        _mapping("ir-high", "identity_rule", "high"),      # accepted
        _mapping("vm-med", "value_mapping", "medium"),     # skipped (not high)
        _mapping("ir-med", "identity_rule", "medium"),     # skipped (not high)
        _mapping("fa-high", "field_annotation", "high"),   # skipped (wrong type)
        _mapping("jp-high", "join_path", "high"),          # skipped (wrong type)
    ]
    accepted_ids: list[str] = []

    def _accept(mid):
        accepted_ids.append(mid)
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)
    monkeypatch.setattr(proposal_scoring, "score_proposal", _score_by_marker)

    result = self_learning.sweep_auto_accept()

    assert result == {"accepted": 2, "failed": 0}
    assert sorted(accepted_ids) == ["ir-high", "vm-high"]


def test_sweep_counts_failures_and_never_raises(monkeypatch):
    pending = [
        _mapping("vm-1", "value_mapping", "high"),   # accepted
        _mapping("vm-2", "value_mapping", "high"),   # raises → failed
        _mapping("vm-3", "value_mapping", "high"),   # returns error dict → failed
    ]

    def _accept(mid):
        if mid == "vm-2":
            raise RuntimeError("neptune down")
        if mid == "vm-3":
            return {"error": "not found"}
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)
    monkeypatch.setattr(proposal_scoring, "score_proposal", _score_by_marker)

    result = self_learning.sweep_auto_accept()  # must not raise

    assert result == {"accepted": 1, "failed": 2}


def test_sweep_empty_pending_is_zero(monkeypatch):
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: {"status": "accepted"})
    monkeypatch.setattr(proposal_scoring, "score_proposal", _score_by_marker)

    assert self_learning.sweep_auto_accept() == {"accepted": 0, "failed": 0}
