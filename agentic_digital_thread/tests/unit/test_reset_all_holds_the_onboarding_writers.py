# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Onboarding waits while all systems are being reset.

Reset All deletes every system's agent runtime, gateway target, graph data
and onboarding session before it puts the catalog back. It held only this
process's claim on the reset domain, so nothing an onboarding writer reads
said it was running: once the graph and the sessions were gone, a chat tool
or the wizard's discover, map or provision could start a new onboarding
(or a provision) in the middle of the reset. Reset All now takes the shared
reset lease (``reset_lease``, scope ``all``) before it removes anything and
gives it back when it ends, however it ends. The HTTP writers
(``onboard._reset_conflict_or_none``) and the chat tools' guard
(``onboard_agent._removal_running_refusal``, in the app and in the
onboarding runtime container) refuse while it is held. A second Reset All
or a single removal is refused too (test_one_reset_lease.py).

Offline: AgentCore, the graph sweeps and the session store are fakes; the
reset route, its lease, its catalog step and the gates are real.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import agents.offboard_retirement as retirement
import agents.onboard as onboard
import agents.onboard_agent as oa
from agents.job_store import JobStore
from agents.models import DiscoverRequest, MapRequest, ProvisionRequest
from tests.unit._shared_table import SharedTable
from tests.unit.test_chat_tools_wait_for_a_running_removal import (  # noqa: F401 - fixture
    GUARDED, JOB_WAIT, _call, _no_aws)
from tests.unit.test_discover_gate_names_the_next_step import (  # noqa: F401 - fixtures
    SID, nodes, sessions, system)

#: The wizard's writers, as the Onboard view calls them.
HTTP_WRITERS = {
    "http.discover": lambda: onboard._onboard_discover_integrity(DiscoverRequest(system_id=SID)),
    "http.map": lambda: onboard.onboard_map(MapRequest(system_id=SID, tables=[])),
    "http.provision": lambda: onboard.onboard_provision(ProvisionRequest(system_id=SID)),
}


@pytest.fixture
def table():
    return SharedTable()


@pytest.fixture(autouse=True)
def _reset_store(monkeypatch, table):
    """The app's reset stores write through to a shared table."""
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=table))
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=table))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.delenv("SYSTEM_ID", raising=False)


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset_all_fakes(monkeypatch, cat, nodes, *, pause=None, fail=None) -> MagicMock:
    """Reset All's AgentCore cleanup, graph sweeps and agent stops faked; its
    domain claim, lease, session deletes and catalog step are real.
    ``pause``: (entered, release); the reset waits after its session deletes
    and before its catalog step. ``fail``: the graph wipe raises it.
    Returns the AgentCore cleanup."""
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})

    def _wipe():
        if fail is not None:
            raise fail
        nodes.clear()
        return 0
    manager = MagicMock()
    if pause is not None:
        entered, release = pause

        def _stop_all():
            entered.set()
            release.wait(JOB_WAIT)
        manager.stop_all.side_effect = _stop_all
    for target, value in (("catalog", cat),
                          ("neptune_write", MagicMock(return_value=True)),
                          ("_graph_system_ids", lambda: []),
                          ("_global_reset_cleanup_agentcore_sync", cleanup),
                          ("_delete_user_registration_secrets", lambda: 0),
                          ("_global_reset_wipe_graph", _wipe),
                          ("reset_agents", MagicMock()),
                          ("_agent_manager", manager)):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)
    return cleanup


class _PausedResetAll:
    """Reset All run on its own thread (as the route runs it), held after its
    session deletes until the block ends; then it finishes."""

    def __init__(self, monkeypatch, cat, nodes):
        self.entered, self.release = threading.Event(), threading.Event()
        _reset_all_fakes(monkeypatch, cat, nodes, pause=(self.entered, self.release))
        self.out: dict = {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.out["result"] = asyncio.run(onboard.onboard_reset(full=False))
        except Exception as exc:  # noqa: BLE001 - the test reads it
            self.out["error"] = exc

    def __enter__(self):
        self.thread.start()
        assert self.entered.wait(JOB_WAIT), "Reset All never reached its catalog step"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(JOB_WAIT)
        assert not self.thread.is_alive()
        return False


def _lease() -> dict:
    """The live reset lease, read as another process reads it."""
    from agents import reset_lease
    return onboard._RESET_LEASES.lease_read(reset_lease.RESET_LEASE_KEY, now=reset_lease.now(),
                                            shared_only=True) or {}


# ── while Reset All runs: every onboarding writer refuses ───────────────────

WRITERS = [f"{m.__name__.split('.')[-1]}.{n}" for m, n in GUARDED] + sorted(HTTP_WRITERS)


def _write(writer: str) -> dict:
    if writer in HTTP_WRITERS:
        return _await(HTTP_WRITERS[writer]())
    module, name = next((m, n) for m, n in GUARDED
                        if f"{m.__name__.split('.')[-1]}.{n}" == writer)
    return _call(module, name)


@pytest.mark.parametrize("writer", WRITERS)
def test_every_onboarding_writer_refuses_while_all_systems_are_reset(
        system, sessions, nodes, monkeypatch, writer):
    sessions.put("resolved")
    with _PausedResetAll(monkeypatch, system, nodes) as reset:
        assert SID not in sessions.items, "Reset All did not delete the session"
        res = _write(writer)
        assert res.get("reason") == "reset_in_progress", res
        assert res.get("system_id") == SID
        assert sessions.items == {}, "a writer started an onboarding mid-reset"
    assert reset.out.get("result", {}).get("status") == "reset", reset.out
    assert _lease() == {}, "Reset All kept its lease"


def test_onboarding_runs_again_once_reset_all_is_done(system, sessions, nodes, monkeypatch):
    with _PausedResetAll(monkeypatch, system, nodes):
        assert _lease().get("scope") == "all"
    assert onboard._reset_conflict_or_none(SID) is None
    assert "error" not in oa.discover(SID)
    assert sessions.items[SID]["status"] == "discovered"


def test_the_lease_is_held_before_anything_is_removed(system, sessions, nodes, monkeypatch):
    cleanup = _reset_all_fakes(monkeypatch, system, nodes)
    seen: list = []
    cleanup.side_effect = lambda: seen.append(dict(_lease())) or {
        "runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False}
    asyncio.run(onboard.onboard_reset(full=False))
    assert seen and seen[0].get("scope") == "all", seen
    assert _lease() == {}


# ── another process sees the lease ─────────────────────────────────────────

def test_the_onboarding_runtime_reads_the_lease_from_the_shared_table(
        system, sessions, nodes, monkeypatch, table):
    """The runtime container holds neither the app's reset domain nor its
    reset store: only the shared table tells it."""
    with _PausedResetAll(monkeypatch, system, nodes):
        monkeypatch.setenv("SYSTEM_ID", "onboarding")
        runtime = JobStore("reset", table=table)
        monkeypatch.setattr(oa, "_shared_reset_jobs", lambda: runtime, raising=False)
        res = oa.discover(SID)
        monkeypatch.delenv("SYSTEM_ID")
    assert res.get("reason") == "reset_in_progress", res
    assert sessions.items == {}


def test_another_app_replica_reads_the_lease_from_the_shared_table(
        system, sessions, nodes, monkeypatch, table):
    with _PausedResetAll(monkeypatch, system, nodes):
        here = (onboard._RESET_JOBS, onboard._RESET_LEASES, onboard._RESET_DOMAIN)
        onboard._RESET_JOBS, onboard._RESET_LEASES, onboard._RESET_DOMAIN = (
            JobStore("reset", table=table), JobStore("reset", table=table),
            onboard._ResetDomain())
        try:
            refusal = onboard._reset_conflict_or_none(SID)
            discovered = _await(HTTP_WRITERS["http.discover"]())
        finally:
            onboard._RESET_JOBS, onboard._RESET_LEASES, onboard._RESET_DOMAIN = here
    assert (refusal or {}).get("reason") == "reset_in_progress", refusal
    assert discovered.get("reason") == "reset_in_progress", discovered


# ── a Reset All that stops gives its lease back ────────────────────────────

@pytest.mark.parametrize("fail", [onboard.ResetIncompleteError("graph wipe failed"),
                                  RuntimeError("the graph went away")],
                         ids=["incomplete", "crash"])
def test_a_reset_all_that_stops_gives_its_lease_back(system, sessions, nodes,
                                                      monkeypatch, fail):
    _reset_all_fakes(monkeypatch, system, nodes, fail=fail)
    with pytest.raises((HTTPException, RuntimeError)):
        asyncio.run(onboard.onboard_reset(full=False))
    assert _lease() == {}, "a stopped Reset All kept its lease"
    assert (onboard._reset_conflict_or_none(SID) or {}).get("reason") != "reset_in_progress"
    assert "error" not in oa.discover(SID), "a stopped Reset All still held onboarding"


def test_the_lease_is_not_listed_as_a_systems_removal(system, sessions, nodes, monkeypatch):
    """The notification feed lists the reset store's records as removals;
    the lease is not one."""
    from agents import notification_routes as nr
    from agents import reset_lease
    with _PausedResetAll(monkeypatch, system, nodes):
        listed = [i["system_id"] for i in nr._removal_items()]
    assert reset_lease.RESET_LEASE_KEY not in listed and listed == [], listed
