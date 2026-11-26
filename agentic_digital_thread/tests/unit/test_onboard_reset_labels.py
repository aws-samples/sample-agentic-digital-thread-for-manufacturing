# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the global /api/onboard/reset node-label clear list.

Regression for the stale-asset leak: the SiteWise-direct provision spine
(``Asset`` / ``AssetModel``) is ONBOARDING-DERIVED — onboard.py
``_create_asset_hierarchy`` is its only writer and each Asset carries
``system``=<onboarding id> as provenance; no seed/demo path creates these nodes
(see docs/graph-shape-contract.md). They were previously omitted from the reset
label list, so a reset left a prior onboarding's assets in the graph and the
resolve step sampled those stale values instead of the freshly-onboarded tree.

These tests assert, with NO live Neptune/AWS/HTTP, that reset issues a wholesale
``DETACH DELETE`` for Asset AND AssetModel (alongside the rest of the onboarding
spine), while ``Concept`` is still cleared-then-reseeded (core concepts survive).
All fixtures are synthetic.
"""
import asyncio
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import pytest  # noqa: E402
import agents.onboard as onboard  # noqa: E402


@pytest.fixture(autouse=True)
def _no_proposal_store(monkeypatch):
    """The offboard cascade retires the system's pending proposals (a DynamoDB
    write). These tests pin graph writes, so stub that seam here;
    tests/unit/test_offboard_retirement.py covers it directly."""
    import agents.offboard_retirement as retirement
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)
    # A failing AgentCore cleanup is now an INCOMPLETE removal (never "done"),
    # so these graph-write tests stub it as a clean, verified removal.
    _clean = {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False}
    monkeypatch.setattr(onboard, "_reset_cleanup_agentcore_sync", lambda sid, *_d: dict(_clean))
    monkeypatch.setattr(onboard, "_global_reset_cleanup_agentcore_sync", lambda: dict(_clean))


def test_reset_label_list_includes_onboarding_derived_asset_nodes():
    labels = onboard._RESET_NODE_LABELS
    # Asset/AssetModel are onboarding-derived and must be cleared on reset.
    assert "Asset" in labels
    assert "AssetModel" in labels
    # The rest of the onboarding spine is still present.
    for expected in ("System", "SchemaEntry", "Field", "Concept",
                     "ValueMapping", "IdentityRule", "JoinPath"):
        assert expected in labels, f"{expected} missing from reset labels"


def _run_reset_capturing_writes():
    """Invoke onboard_reset with every side-effect mocked; return the list of
    Cypher strings passed to neptune_write."""
    writes: list[str] = []

    fake_catalog = MagicMock()
    fake_catalog.get_onboarded.return_value = []
    fake_catalog.reset_all.return_value = None

    def _boto_raises(*a, **k):  # AgentCore cleanup is best-effort / try-excepted.
        raise RuntimeError("boto3 disabled in unit test")

    import boto3
    with patch.object(onboard, "neptune_write",
                      side_effect=lambda q, *a, **k: writes.append(q) or True), \
            patch.object(onboard, "catalog", fake_catalog), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(boto3, "client", side_effect=_boto_raises):
        result = asyncio.run(onboard.onboard_reset())
    return writes, result


def _issued_detach_delete_for(writes, label):
    pat = re.compile(rf"MATCH\s*\(\s*n\s*:\s*{label}\s*\)\s*DETACH DELETE\s+n", re.IGNORECASE)
    return any(pat.search(w) for w in writes)


def test_reset_issues_detach_delete_for_asset_and_asset_model():
    writes, result = _run_reset_capturing_writes()
    assert result["status"] == "reset"
    assert _issued_detach_delete_for(writes, "Asset"), writes
    assert _issued_detach_delete_for(writes, "AssetModel"), writes
    # Sanity: the pre-existing spine clears still fire too.
    assert _issued_detach_delete_for(writes, "System"), writes
    assert _issued_detach_delete_for(writes, "Concept"), writes


# ─────────────────────────────────────────────────────────────────────────────
# Single-system reset (/api/onboard/reset/{system_id}) — offboard cascade.
# Regression for the stranded-JoinPath leak: IdentityRules were deleted by BOTH
# full id and short name (learned rules use short names), but JoinPaths only by
# full id — stranding short-name JoinPath rows that later leaked stale joins
# into the impact drawer. All fixtures are synthetic (generic sys-a1 id).
# ─────────────────────────────────────────────────────────────────────────────
def _run_single_reset_capturing_writes(system_id):
    """Invoke onboard_reset_system with every side-effect mocked; return the
    (cypher, params) tuples passed to neptune_write and the result payload."""
    writes: list[tuple] = []

    fake_catalog = MagicMock()
    fake_catalog.get.return_value = {"id": system_id, "status": "onboarded"}
    fake_catalog.remove.return_value = True

    def _boto_raises(*a, **k):  # AgentCore cleanup is best-effort / try-excepted.
        raise RuntimeError("boto3 disabled in unit test")

    import boto3
    with patch.object(onboard, "neptune_write",
                      side_effect=lambda q, p=None, *a, **k: writes.append((q, p)) or True), \
            patch.object(onboard, "catalog", fake_catalog), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(onboard, "neptune_query", return_value=[{"id": system_id}]), \
            patch.object(boto3, "client", side_effect=_boto_raises):
        # The graph holds only this system (the sibling-lookup sentinel), so it is
        # the last of its family and the legacy short name is safe to delete by.
        result = asyncio.run(onboard.onboard_reset_system(system_id))
    return writes, result


def test_single_reset_deletes_joinpaths_by_full_id_and_short_name():
    """The JoinPath delete must mirror the IdentityRule WHERE clause: full id
    ($sid) AND short name ($short), on both endpoints."""
    writes, result = _run_single_reset_capturing_writes("sys-a1")
    assert result["status"] == "reset" and result["system_id"] == "sys-a1"
    jp_writes = [(q, p) for q, p in writes if "JoinPath" in q]
    assert len(jp_writes) == 1, writes
    cypher, params = jp_writes[0]
    for clause in ("jp.from_system = $sid", "jp.to_system = $sid",
                   "jp.from_system = $short", "jp.to_system = $short"):
        assert clause in cypher, cypher
    assert params == {"sid": "sys-a1", "short": "sys"}


def test_single_reset_identity_rules_still_deleted_by_full_id_and_short_name():
    """The pre-existing IdentityRule short-name coverage is preserved by the
    decomposition."""
    writes, _ = _run_single_reset_capturing_writes("sys-a1")
    ir_writes = [(q, p) for q, p in writes if "IdentityRule" in q]
    assert len(ir_writes) == 1, writes
    cypher, params = ir_writes[0]
    for clause in ("ir.source_system = $sid", "ir.target_system = $sid",
                   "ir.source_system = $short", "ir.target_system = $short"):
        assert clause in cypher, cypher
    assert params == {"sid": "sys-a1", "short": "sys"}


def test_single_reset_not_onboarded_returns_error_without_writes():
    """A system that is not onboarded (in-memory AND Neptune) resets nothing."""
    writes: list[tuple] = []
    fake_catalog = MagicMock()
    fake_catalog.get.return_value = {"id": "sys-a1", "status": "registered"}
    with patch.object(onboard, "neptune_write",
                      side_effect=lambda q, p=None, *a, **k: writes.append((q, p))), \
            patch.object(onboard, "neptune_query", return_value=[{"status": "registered"}]), \
            patch.object(onboard, "catalog", fake_catalog):
        result = asyncio.run(onboard.onboard_reset_system("sys-a1"))
    assert result == {"error": "System 'sys-a1' is not onboarded"}
    assert writes == []


_TESTS = [
    test_reset_label_list_includes_onboarding_derived_asset_nodes,
    test_reset_issues_detach_delete_for_asset_and_asset_model,
    test_single_reset_deletes_joinpaths_by_full_id_and_short_name,
    test_single_reset_identity_rules_still_deleted_by_full_id_and_short_name,
    test_single_reset_not_onboarded_returns_error_without_writes,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} reset-label tests passed")
