# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system being removed is neither available nor a candidate.

Test event 31 (2026-10-14): a system was removed from its card and another
system onboarded eighteen seconds later. The removal cascade tears the
runtime down FIRST (minutes) and reaches the graph only afterwards, so the
removed system's schema tree, mappings and sampled values were still in the
graph when the new system's Identity Rules step read its candidates from it:
four equipment-id rules were proposed against the removed system, each backed
by real value overlap. Meanwhile the catalog entry stayed 'onboarded' until
the cascade's last step, so in every other tab (and for the onboarding chat)
the card looked available.

Pinned here:
  1. the entry is ``removing`` from the removal marker to the cascade's own
     catalog step (then 'registered' / removed / ``removal_failed``), and in
     that window it is neither onboarded nor a candidate;
  2. the reset preflight admits a ``removing`` entry as it is (a second click
     answers already-running; no Neptune read, no rebuilt failed removal);
  3. a graph hydrate that sees the running removal's marker leaves the entry
     ``removing`` (it must not offer a retry of a removal still running);
  4. the concept pass's rules, value mappings and evidence naming a system
     being removed or half-removed, by id or by display name, are dropped;
     everything naming a live system is kept, and nothing changes when no
     removal is known;
  5. join discovery's system list excludes systems being removed;
  6. end to end: the resolve job stores only the proposals that name live
     systems.

Offline: AgentCore, the graph, DynamoDB and the model are fakes.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.join_discovery as jd  # noqa: E402
import agents.offboard_retirement as retirement  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.catalog import (  # noqa: E402
    REMOVAL_FAILED,
    REMOVAL_MODE,
    REMOVING,
    SystemCatalog,
)
from agents.job_store import JobStore  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402

GONE = "mes-site"
GONE_NAME = "Site MES"
LIVE = "erp"
NEW = "plant-cmms"


def _catalog(gone_status: str = "onboarded") -> SystemCatalog:
    return SystemCatalog([
        {"id": GONE, "name": GONE_NAME, "status": gone_status, "schema": "mes",
         "plant": "p1", "connection_type": "REST API", "system_type": "MES"},
        {"id": LIVE, "name": "ERP", "status": "onboarded", "schema": "erp",
         "plant": "p1", "connection_type": "PostgreSQL", "system_type": "ERP"},
        {"id": NEW, "name": "Plant CMMS", "status": "registered", "schema": "cmms",
         "plant": "p1", "connection_type": "PostgreSQL", "system_type": "CMMS"},
    ])


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))


def _cascade(monkeypatch, cat: SystemCatalog, *, graph_fails: bool = False) -> dict:
    """Fake every side effect of the single-system cascade on a REAL catalog.
    The graph step records the catalog's view of the system as it runs."""
    seen: dict = {}

    def _graph(sid):
        seen["status_at_graph_step"] = (cat.get(sid) or {}).get("status")
        seen["onboarded_at_graph_step"] = cat.get_onboarded_ids()
        seen["removal_keys_at_graph_step"] = cat.get_removal_keys()
        if graph_fails:
            raise onboard.ResetIncompleteError("graph delete failed")

    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [GONE],
                                      "wait_timed_out": False})
    for target, value in (("neptune_write", MagicMock(return_value=True)),
                          ("catalog", cat),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", cleanup),
                          ("_reset_delete_graph_artifacts_invalidating", _graph),
                          ("_delete_onboarding_session", lambda sid: False),
                          ("_delete_own_secret_or_incomplete", lambda sid: ""),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)
    return seen


# ── 1. removing from the marker to the catalog step ────────────────────────

def test_the_entry_is_removing_while_the_cascade_runs_then_registered(monkeypatch):
    cat = _catalog()
    seen = _cascade(monkeypatch, cat)
    result = asyncio.run(onboard._run_reset_steps(GONE, full=False))
    assert result["status"] == "reset"
    assert seen["status_at_graph_step"] == REMOVING
    assert GONE not in seen["onboarded_at_graph_step"], "served while being removed"
    assert LIVE in seen["onboarded_at_graph_step"]
    assert seen["removal_keys_at_graph_step"] == {GONE, GONE_NAME}
    assert cat.get(GONE)["status"] == "registered", "the default removal keeps the registration"
    assert cat.get_removal_keys() == set()


def test_a_full_removal_forgets_the_entry_after_removing(monkeypatch):
    cat = _catalog()
    seen = _cascade(monkeypatch, cat)
    result = asyncio.run(onboard._run_reset_steps(GONE, full=True))
    assert result["removed"] is True
    assert seen["status_at_graph_step"] == REMOVING
    assert cat.get(GONE) is None
    assert cat.get_removal_keys() == set()


def test_a_failed_cascade_leaves_removal_failed_with_its_mode(monkeypatch):
    cat = _catalog()
    _cascade(monkeypatch, cat, graph_fails=True)
    with pytest.raises(onboard.ResetIncompleteError):
        asyncio.run(onboard._run_reset_steps(GONE, full=True))
    entry = cat.get(GONE)
    assert entry["status"] == REMOVAL_FAILED and entry[REMOVAL_MODE] == "full"
    # Half-removed: still not a candidate, still not served.
    assert cat.get_removal_keys() == {GONE, GONE_NAME}
    assert GONE not in cat.get_onboarded_ids()


# ── 2. the preflight admits a removing entry as it is ──────────────────────

def test_the_reset_preflight_admits_a_removing_entry_without_the_graph(monkeypatch):
    cat = _catalog(REMOVING)
    graph = MagicMock(side_effect=AssertionError("the graph must not be read"))
    monkeypatch.setattr(onboard, "catalog", cat)
    monkeypatch.setattr(onboard, "neptune_query", graph)
    assert onboard._reset_verify_onboarded(GONE, full=True) is None
    assert cat.get(GONE)["status"] == REMOVING, "rebuilt as a failed removal mid-cascade"
    graph.assert_not_called()


def test_a_remove_again_retry_is_not_refused_while_the_removal_runs(monkeypatch):
    cat = _catalog(REMOVING)
    monkeypatch.setattr(onboard, "catalog", cat)
    assert onboard._retry_refusal(GONE) is None
    cat.update_status(GONE, "registered")
    assert onboard._retry_refusal(GONE), "a recovered system must still refuse the retry"


# ── 3. a hydrate mid-cascade keeps the entry removing ──────────────────────

def test_ensure_removal_failed_leaves_a_removing_entry_alone():
    cat = _catalog(REMOVING)
    node = {"id": GONE, "removal_incomplete": True, "removal_full": True}
    entry = cat.ensure_removal_failed(node)
    assert entry["status"] == REMOVING and REMOVAL_MODE not in entry
    # The same node after the process restarted (no entry): the usual rebuild.
    cold = SystemCatalog([])
    assert cold.ensure_removal_failed({**node, "name": GONE_NAME})["status"] == REMOVAL_FAILED


def test_hydrate_keeps_a_running_removal_out_of_removal_failed(monkeypatch):
    cat = _catalog(REMOVING)
    rows = [{"id": GONE, "name": GONE_NAME, "removal_incomplete": True, "removal_full": True,
             "status": "onboarded"}]
    monkeypatch.setattr(cat, "_graph_rows", lambda: rows)
    cat.hydrate()
    assert cat.get(GONE)["status"] == REMOVING


# ── 4. the concept pass's proposals naming a removed system are dropped ────

def _concept_result() -> dict:
    return {
        "rules": [
            {"concept": "equipment", "source_field": "assets.asset_tag", "target_system": GONE,
             "target_field": "equipment.equipment_id", "confidence": 0.9},
            {"concept": "equipment", "source_field": "assets.asset_tag", "target_system": GONE_NAME,
             "target_field": "machines.machine_id", "confidence": 0.9},
            {"concept": "equipment", "source_field": "assets.asset_tag", "target_system": LIVE,
             "target_field": "assets.asset_id", "confidence": 0.8},
            {"concept": "equipment", "source_field": "assets.asset_tag",
             "target_system": "someone-else", "target_field": "x.y", "confidence": 0.5},
        ],
        "value_mappings": [
            {"from_system": NEW, "from_field": "assets.site", "from_value": "P1",
             "to_system": GONE, "to_field": "plants.code", "to_value": "Plant 1"},
            {"from_system": NEW, "from_field": "assets.site", "from_value": "P1",
             "to_system": LIVE, "to_field": "plants.code", "to_value": "Plant 1"},
        ],
        "evidence": [
            {"concept": "equipment", "source_field": "assets.asset_tag", "target_system": GONE,
             "target_field": "equipment.equipment_id", "overlap_count": 12},
            {"concept": "equipment", "source_field": "assets.asset_tag", "target_system": LIVE,
             "target_field": "assets.asset_id", "overlap_count": 9},
        ],
        "recommendations": [{"concept": "equipment"}],
    }


@pytest.mark.parametrize("gone_status", [REMOVING, REMOVAL_FAILED])
def test_proposals_naming_a_removed_system_are_dropped_by_id_and_by_name(monkeypatch, gone_status):
    monkeypatch.setattr(onboard, "catalog", _catalog(gone_status))
    kept = onboard._drop_removed_candidates(_concept_result(), NEW)
    assert [r["target_system"] for r in kept["rules"]] == [LIVE, "someone-else"]
    assert [m["to_system"] for m in kept["value_mappings"]] == [LIVE]
    assert [e["target_system"] for e in kept["evidence"]] == [LIVE]
    assert kept["recommendations"] == [{"concept": "equipment"}], "untouched keys survive"


def test_nothing_changes_when_no_removal_is_known(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _catalog("onboarded"))
    result = _concept_result()
    assert onboard._drop_removed_candidates(result, NEW) is result


def test_a_result_without_proposal_keys_is_tolerated(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _catalog(REMOVING))
    assert onboard._drop_removed_candidates({"rules": []}, NEW) == {
        "rules": [], "value_mappings": [], "evidence": []}


# ── 5. join discovery's system list ────────────────────────────────────────

def test_join_discovery_skips_systems_being_removed(monkeypatch):
    import agents.catalog as catalog_module
    monkeypatch.setattr(catalog_module, "catalog", _catalog(REMOVING))
    monkeypatch.setattr(jd, "_neptune_query",
                        lambda *_a, **_k: [{"id": GONE}, {"id": LIVE}, {"id": NEW}])
    assert jd._onboarded_systems() == sorted([LIVE, NEW])


# ── 6. the resolve job stores only live proposals ──────────────────────────

class _FakeTable:
    def __init__(self):
        self._store: dict = {}

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg
        self._store[Item["system_id"]] = Item

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)


def test_the_resolve_job_keeps_only_proposals_naming_live_systems(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _catalog(REMOVING))
    store = JobStore("resolve", table=_FakeTable())
    body = OnboardResolveRequest(system_id=NEW, mappings=[])
    with patch.object(onboard, "_resolve_jobs", store), \
         patch.object(onboard, "_do_resolve", return_value=_concept_result()), \
         patch.object(onboard, "_augment_with_affinity_rules", lambda body, result: result), \
         patch("agents.resolve_gates.gate_overlap_rules", lambda result: result), \
         patch("agents.join_discovery.discover_joins_for_system",
               lambda sid: {"joins": [], "crosswalks": []}), \
         patch("agents.self_learning.get_pending_mappings", lambda: []):
        onboard._run_resolve_job("job-rm", body)
    job = store.get("job-rm")
    assert job["status"] == "done", job
    targets = [r["target_system"] for r in job["result"]["rules"]]
    assert GONE not in targets and GONE_NAME not in targets, targets
    assert LIVE in targets
    assert [e["target_system"] for e in job["result"]["evidence"]] == [LIVE]
