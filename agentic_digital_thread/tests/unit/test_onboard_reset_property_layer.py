# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the reset/offboard telemetry/instance-layer cleanup (Task A4).

i3X (and SiteWise Direct) onboards create Asset/Property/AssetModel nodes in
addition to the SQL-style System/SchemaEntry/Field spine. A per-system reset
must delete them, or a re-onboard stacks fresh nodes on top of the stale ones
(the disconnected stale-asset subgraph). Contract:

- ``onboard_reset_system`` issues, for the target system only:
    ``MATCH (p:Property {system: $sid}) DETACH DELETE p``
    ``MATCH (a:Asset {system: $sid}) DETACH DELETE a``
  plus the orphan-only AssetModel sweep
    ``MATCH (m:AssetModel) WHERE NOT (m)<-[:INSTANCE_OF]-() DETACH DELETE m``
  (AssetModel is shared-by-name across systems, so only models left with no
  INSTANCE_OF edge are removed — mirroring the orphaned-Concept cleanup);
- the full-reset label list (``_RESET_NODE_LABELS``) includes ``Property``.

No live Neptune/AWS — every side effect is mocked; all fixtures synthetic.
"""
from __future__ import annotations

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

_SID = "example-i3x"


# --------------------------------------------------------------------------- #
# Full reset: label list carries Property
# --------------------------------------------------------------------------- #
def test_full_reset_label_list_includes_property():
    labels = onboard._RESET_NODE_LABELS
    assert "Property" in labels
    # The rest of the telemetry/instance spine is still present.
    assert "Asset" in labels
    assert "AssetModel" in labels


def _run_global_reset_capturing_writes():
    writes: list[str] = []
    fake_catalog = MagicMock()
    fake_catalog.get_onboarded.return_value = []

    def _boto_raises(*_a, **_k):  # AgentCore cleanup is best-effort/try-excepted
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


def test_global_reset_issues_detach_delete_for_property():
    writes, result = _run_global_reset_capturing_writes()
    assert result["status"] == "reset"
    pat = re.compile(r"MATCH\s*\(\s*n\s*:\s*Property\s*\)\s*DETACH DELETE\s+n")
    assert any(pat.search(w) for w in writes), writes


# --------------------------------------------------------------------------- #
# Per-system reset: Property/Asset/orphan-AssetModel deletes
# --------------------------------------------------------------------------- #
def _run_system_reset_capturing_writes(system_id=_SID):
    """Invoke onboard_reset_system with every side effect mocked; return the
    ordered (cypher, params) writes."""
    writes: list[tuple[str, dict]] = []

    fake_catalog = MagicMock()
    fake_catalog.get.return_value = {
        "id": system_id, "name": "Example i3X", "status": "onboarded",
        "connection_type": "SiteWise (i3X)",
    }
    fake_catalog.remove.return_value = True

    def _boto_raises(*_a, **_k):  # AgentCore cleanup is best-effort/try-excepted
        raise RuntimeError("boto3 disabled in unit test")

    import boto3
    with patch.object(onboard, "neptune_write",
                      side_effect=lambda q, p=None: writes.append((q, p or {})) or True), \
            patch.object(onboard, "neptune_query", return_value=[]), \
            patch.object(onboard, "catalog", fake_catalog), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(boto3, "client", side_effect=_boto_raises):
        result = asyncio.run(onboard.onboard_reset_system(system_id))
    return writes, result


def test_system_reset_deletes_property_layer():
    writes, result = _run_system_reset_capturing_writes()
    assert result["status"] == "reset"

    prop_deletes = [(c, p) for c, p in writes
                    if "MATCH (p:Property {system: $sid}) DETACH DELETE p" in c]
    assert prop_deletes, "reset must delete the system's Property nodes"
    assert prop_deletes[0][1] == {"sid": _SID}, \
        "Property delete must be parameterized on the target system id"

    asset_deletes = [(c, p) for c, p in writes
                     if "MATCH (a:Asset {system: $sid}) DETACH DELETE a" in c]
    assert asset_deletes, "reset must delete the system's Asset nodes"
    assert asset_deletes[0][1] == {"sid": _SID}


def test_system_reset_sweeps_only_orphaned_asset_models():
    writes, _result = _run_system_reset_capturing_writes()
    model_deletes = [c for c, _p in writes
                     if ":AssetModel)" in c and "DETACH DELETE" in c]
    assert model_deletes, "reset must sweep orphaned AssetModels"
    for cypher in model_deletes:
        assert "NOT (m)<-[:INSTANCE_OF]-()" in cypher, \
            "AssetModel delete must be orphan-only (shared-by-name across systems)"


def test_system_reset_deletes_derived_asset_model_roles_only():
    writes, _result = _run_system_reset_capturing_writes()
    role_deletes = [(c, p) for c, p in writes if "AssetModelRole" in c and "DETACH DELETE" in c]
    assert len(role_deletes) == 1
    cypher, params = role_deletes[0]
    assert "{system: $sid}" in cypher and "<> 'steward'" in cypher and "sid" in params


def test_system_reset_deletes_assets_before_the_system_node():
    """Ordering: the instance-layer deletes (Property/Asset/AssetModel/
    AssetModelRole) run with (not instead of) the existing spine deletes, and
    BEFORE the :System node, which is deleted last (it is the sentinel a retry
    needs if any earlier delete fails: test_offboard_graph_delete_checked.py).
    The reset keeps the "System last" order alongside the
    role-node delete and the role-pass lock. The params always target one
    system."""
    writes, _result = _run_system_reset_capturing_writes(system_id="mes-example")
    cyphers = [c for c, _p in writes]
    sys_del_idx = next(i for i, c in enumerate(cyphers)
                       if "MATCH (s:System {id: $sid}) DETACH DELETE s" in c)
    prop_del_idx = next(i for i, c in enumerate(cyphers)
                        if "Property {system: $sid}" in c)
    assert prop_del_idx < sys_del_idx == len(cyphers) - 1
    # Every $sid-parameterized delete targets exactly the reset system.
    for c, p in writes:
        if "DETACH DELETE" in c and "sid" in p:
            assert p["sid"] == "mes-example", (c, p)


def test_system_reset_still_cleans_identity_rules_and_orphan_concepts():
    """Regression: the pre-existing cleanup statements still run."""
    writes, _result = _run_system_reset_capturing_writes()
    cyphers = [c for c, _p in writes]
    assert any("IdentityRule" in c and "DETACH DELETE" in c for c in cyphers)
    assert any("c.isa95_level IS NULL AND c.source IS NULL" in c for c in cyphers)
