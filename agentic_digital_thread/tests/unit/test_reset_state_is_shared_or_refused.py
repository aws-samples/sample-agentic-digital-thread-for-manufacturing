# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A removal's running state is in the shared table, or nothing happens.

The onboarding runtime container is a separate process: it knows a removal
or Reset All is running only from what the app writes to the shared
onboarding-sessions table (the reset lease, ``reset_lease``). Two gaps let
its chat tools run in the middle of one:

* its guard read the app's records through the fail-soft ``JobStore``,
  which answers from an in-memory copy (empty in that process) when
  DynamoDB errors, and then stops asking DynamoDB for good;
* the app wrote them fail-soft too, so a write that did not land looked the
  same as no removal at all.

Now the runtime guard reads the lease with a strict consistent read (no
memory fallback, no backend disable) and refuses, retryably, when the read
fails; and a removal or Reset All takes the lease before it removes
anything, and stops with nothing removed (503) when the write does not
land.

Offline: the shared table, the graph and AgentCore are fakes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import agents.offboard_retirement as retirement
import agents.onboard as onboard
import agents.onboard_agent as oa
import agents.job_store as job_store
from agents.job_store import JobStore
from tests.unit._shared_table import SharedTable
from tests.unit.test_chat_tools_wait_for_a_running_removal import (  # noqa: F401 - fixture
    GUARDED, _call, _no_aws)
from tests.unit.test_discover_gate_names_the_next_step import (  # noqa: F401 - fixtures
    SID, nodes, sessions, system)

#: The guarded tools the onboarding runtime container serves.
RUNTIME_TOOLS = [(m, n) for m, n in GUARDED if m is oa]


@pytest.fixture
def table():
    return SharedTable()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.delenv("SYSTEM_ID", raising=False)


# ── JobStore: a strict read and a strict write ─────────────────────────────

def test_get_strict_tells_absent_from_present_from_unavailable(table):
    store = JobStore("reset", table=table)
    assert store.get_strict("x") is None
    store["x"] = {"state": "running"}
    assert JobStore("reset", table=table).get_strict("x") == {"state": "running"}
    table.fail_reads = True
    with pytest.raises(job_store.JobStoreUnavailableError):
        store.get_strict("x")


def test_get_strict_without_a_shared_table_is_unavailable():
    store = JobStore("reset", table=None)
    store["x"] = {"state": "running"}
    with pytest.raises(job_store.JobStoreUnavailableError):
        store.get_strict("x")


def test_a_failed_read_does_not_stop_later_strict_reads(table):
    store = JobStore("reset", table=table)
    table.fail_reads = True
    assert store.get("x") is None            # the fail-soft read gives up on the table
    with pytest.raises(job_store.JobStoreUnavailableError):
        store.get_strict("x")
    table.fail_reads = False
    table.items.clear()
    JobStore("reset", table=table)["x"] = {"state": "running"}
    assert store.get_strict("x") == {"state": "running"}


def test_put_strict_raises_and_keeps_nothing_when_the_write_does_not_land(table):
    store = JobStore("reset", table=table)
    table.fail_puts = True
    with pytest.raises(job_store.JobStoreUnavailableError):
        store.put_strict("x", {"state": "running"})
    assert store.get("x") is None and table.items == {}


def test_put_strict_writes_through_a_backend_a_failed_write_turned_off(table):
    store = JobStore("reset", table=table)
    table.fail_puts = True
    store["y"] = {"state": "done"}          # fail-soft: kept in memory, table given up on
    table.fail_puts = False
    store.put_strict("x", {"state": "running"})
    assert JobStore("reset", table=table).get_strict("x") == {"state": "running"}


def test_put_strict_on_a_memory_only_store_keeps_the_state_in_memory():
    store = JobStore("reset", table=None)
    store.put_strict("x", {"state": "running"})
    assert store.get("x") == {"state": "running"}


# ── the onboarding runtime's guard refuses when it cannot read ─────────────

@pytest.fixture
def runtime(monkeypatch, table):
    """The onboarding runtime container, reading the app's reset state from
    the shared table."""
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    jobs = JobStore("reset", table=table)
    monkeypatch.setattr(oa, "_shared_reset_jobs", lambda: jobs, raising=False)
    return jobs


@pytest.mark.parametrize("module,name", RUNTIME_TOOLS, ids=[n for _m, n in RUNTIME_TOOLS])
def test_every_runtime_tool_refuses_when_the_reset_state_cannot_be_read(
        system, sessions, nodes, runtime, table, module, name):
    table.fail_reads = True
    res = _call(module, name)
    assert res.get("reason") == "reset_state_unavailable", res
    assert res.get("system_id") == SID
    assert "nothing was done" in res["error"] and "try again" in res["error"].lower(), res
    assert sessions.items == {}, "a tool ran without knowing whether a removal runs"


def test_a_failed_read_in_the_runtime_is_not_sticky(system, sessions, nodes, runtime, table,
                                                     monkeypatch):
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=table))
    table.fail_reads = True
    assert oa.discover(SID).get("reason") == "reset_state_unavailable"
    table.fail_reads = False
    assert onboard._RESET_DOMAIN.claim_single(SID, lambda: None) is None   # the app's removal
    assert oa.discover(SID).get("reason") == "reset_in_progress", "a stale empty copy was read"
    onboard._RESET_DOMAIN.release_single(SID)
    assert "error" not in oa.discover(SID)
    assert sessions.items[SID]["status"] == "discovered"


def test_the_runtime_guard_reads_reset_all_from_the_shared_table(
        system, sessions, nodes, runtime, table, monkeypatch):
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=table))
    assert onboard._RESET_DOMAIN.claim_all() is None                      # the app's Reset All
    assert oa.discover(SID).get("reason") == "reset_in_progress"
    assert sessions.items == {}
    onboard._RESET_DOMAIN.release_all()


# ── a lease that does not land: nothing is removed ─────────────────────────

def _single_removal_fakes(monkeypatch):
    """A card removal of an onboarded system with its AgentCore, proposal and
    graph steps faked; returns (catalog, cleanup, graph delete)."""
    cat = MagicMock()
    cat.get.return_value = {"id": SID, "status": "onboarded"}
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    graph = MagicMock()
    for target, value in (("catalog", cat),
                          ("neptune_write", MagicMock(return_value=True)),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", cleanup),
                          ("_reset_delete_graph_artifacts_invalidating", graph)):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)
    return cat, cleanup, graph


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "background"])
def test_a_removal_whose_lease_does_not_land_removes_nothing(monkeypatch, table, wait):
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=table))
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=table))
    cat, cleanup, graph = _single_removal_fakes(monkeypatch)
    table.fail_puts = True
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset_system(SID, wait=wait, full=False))
    assert err.value.status_code == 503, err.value.detail
    assert "Nothing was removed" in err.value.detail and "retry" in err.value.detail
    cleanup.assert_not_called()
    graph.assert_not_called()
    cat.update_status.assert_not_called()
    cat.remove.assert_not_called()
    assert onboard._RESET_JOBS.get(SID) is None, "a running record was kept in memory only"
    table.fail_puts = False
    retried = asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=False))
    assert retried["status"] == "reset", "the refused removal kept its claim"


def test_a_reset_all_whose_lease_does_not_land_removes_nothing(monkeypatch, table):
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=table))
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=table))
    cat = MagicMock()
    cat.get_onboarded.return_value = [{"id": SID}]
    writes = MagicMock(return_value=True)
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    for target, value in (("catalog", cat), ("neptune_write", writes),
                          ("_graph_system_ids", lambda: []),
                          ("_global_reset_cleanup_agentcore_sync", cleanup),
                          ("_global_reset_wipe_graph", lambda: 0),
                          ("reset_agents", MagicMock()), ("_agent_manager", MagicMock())):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)
    table.fail_puts = True
    with pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset(full=False))
    assert err.value.status_code == 503, err.value.detail
    assert "Nothing was removed" in err.value.detail and "retry" in err.value.detail
    writes.assert_not_called()               # not even the removal marker
    cleanup.assert_not_called()
    cat.reset_all.assert_not_called()
    assert onboard._RESET_DOMAIN.reset_all_running() is False
    table.fail_puts = False
    assert asyncio.run(onboard.onboard_reset(full=False))["status"] == "reset"
