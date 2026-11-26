# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's onboarding tools wait for a running removal of the system.

A removal deletes the system's graph data and then its onboarding session
before its reset job says ``done`` (the catalog step comes last). The chat
tools' guard (``onboard_agent._unless_removal_failed``) read only the graph's
removal marker, which leaves with the System node, so in that window a chat
``discover`` created a new session: in a full removal an orphan session of
a removed system, in a removal that keeps the registration an onboarding
started before the removal finished. The HTTP writers already refuse while
the reset job runs (``onboard._reset_conflict_or_none``); every guarded chat
tool now does too, before the tool runs, and runs as before once the job is
done. In the onboarding runtime container the reset lease the app's removal
holds is read from the table the two share.

Offline: the session store, the graph and the cascade's AgentCore steps are
fakes; the guard, the tools and the reset job are real.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agents.onboard as onboard
import agents.onboard_agent as oa
import agents.onboard_chat_lifecycle as lifecycle
from agents.job_store import JobStore
from tests.unit._shared_table import SharedTable
from tests.unit.test_discover_gate_names_the_next_step import (  # noqa: F401 - fixtures
    SID, nodes, sessions, system)

#: How long a test waits on the removal's job: generous, for a slow runner.
JOB_WAIT = 60.0

#: Every tool the guard wraps, by module.
GUARDED = [(oa, name) for name in ("store_credentials", "test_connection", "discover",
                                   "assess_vocabulary_fit", "map_fields",
                                   "resolve_identities", "provision_system", "verify_system")]
GUARDED += [(lifecycle, name) for name in ("discover", "provision_system", "store_credentials")]


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.delenv("SYSTEM_ID", raising=False)


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    """A tool body that ran anyway reaches no AWS service."""
    monkeypatch.setattr(oa, "_secrets_client", MagicMock())
    monkeypatch.setattr(oa, "bedrock_call", MagicMock(side_effect=RuntimeError("offline")))


def _call(module, name: str) -> dict:
    """The tool, called as the chat calls it: system_id first, any other
    required argument a placeholder."""
    tool = getattr(module, name)
    fn = getattr(tool, "original_function", tool)
    params = list(inspect.signature(fn).parameters.values())[1:]
    extra = ["{}" for p in params if p.default is inspect.Parameter.empty]
    return tool(SID, *extra)


def _guarded_names(path: Path) -> set:
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                if (getattr(d, "attr", None) or getattr(d, "id", None)) == "_unless_removal_failed":
                    names.add(node.name)
    return names


def test_the_list_holds_every_tool_the_guard_wraps():
    assert {n for m, n in GUARDED if m is oa} == _guarded_names(Path(oa.__file__))
    assert {n for m, n in GUARDED if m is lifecycle} == _guarded_names(Path(lifecycle.__file__))


# ── while the reset job runs: refused, nothing written ─────────────────────

@pytest.mark.parametrize("module,name", GUARDED, ids=[f"{m.__name__.split('.')[-1]}.{n}"
                                                       for m, n in GUARDED])
def test_every_guarded_tool_refuses_while_the_systems_removal_runs(
        system, sessions, nodes, module, name):
    """The System node and the session are already gone; the job still runs."""
    onboard._RESET_JOBS[SID] = {"state": "running", "full": False}
    res = _call(module, name)
    assert res.get("reason") == "reset_in_progress", res
    assert res.get("system_id") == SID
    assert "being removed" in res["error"] and "nothing was done" in res["error"], res
    assert sessions.items == {}, "a guarded tool wrote a session mid-removal"


def test_in_the_onboarding_runtime_the_shared_reset_lease_decides(
        system, sessions, nodes, monkeypatch):
    """The runtime container does not hold the app's reset state: it reads
    the reset lease the app's removal took in the table they share."""
    from agents import reset_lease
    table = SharedTable()
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=table))
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    shared = JobStore("reset", table=table)
    monkeypatch.setattr(oa, "_shared_reset_jobs", lambda: shared, raising=False)
    assert onboard._RESET_DOMAIN.claim_single(SID, lambda: None, full=True) is None
    assert oa.discover(SID).get("reason") == "reset_in_progress"
    assert sessions.items == {}
    onboard._RESET_DOMAIN.release_single(SID)
    assert shared.lease_read(reset_lease.RESET_LEASE_KEY, now=reset_lease.now(),
                             shared_only=True) is None
    assert "error" not in oa.discover(SID)


# ── a card removal paused after its session delete ─────────────────────────

def _cascade_fakes(monkeypatch, cat, nodes) -> tuple[threading.Event, threading.Event]:
    """The cascade's AgentCore, proposal and graph steps faked; its session
    step is real. Its catalog step (the last) waits until released."""
    paused, release = threading.Event(), threading.Event()
    real_update = cat.update_status

    def _catalog_step(sid, status):
        paused.set()
        release.wait(JOB_WAIT)
        return real_update(sid, status)
    monkeypatch.setattr(cat, "update_status", _catalog_step)
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [SID],
                                      "wait_timed_out": False})
    for target, value in (("catalog", cat),
                          ("neptune_write", MagicMock(return_value=True)),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", cleanup),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_reset_delete_graph_artifacts_invalidating",
                           lambda sid: nodes.pop(sid, None))):
        monkeypatch.setattr(onboard, target, value)
    return paused, release


def test_a_chat_discover_waits_for_a_card_removal_paused_after_its_session_delete(
        system, sessions, nodes, monkeypatch):
    nodes[SID] = {"id": SID, "status": "onboarded"}
    sessions.put("resolved")
    paused, release = _cascade_fakes(monkeypatch, system, nodes)
    onboard._RESET_JOBS[SID] = {"state": "running", "full": False}
    job = threading.Thread(target=lambda: asyncio.run(onboard._run_reset_job(SID, full=False)),
                           daemon=True)
    job.start()
    try:
        assert paused.wait(JOB_WAIT), "the removal never reached its catalog step"
        assert onboard._RESET_JOBS[SID]["state"] == "running"
        assert SID not in nodes
        refused = oa.discover(SID)
        assert refused.get("reason") == "reset_in_progress", refused
        assert SID not in sessions.items, "the removal's session survived, or discover wrote one"
    finally:
        release.set()
        job.join(JOB_WAIT)
    assert not job.is_alive()
    assert onboard._RESET_JOBS[SID]["state"] == "done", onboard._RESET_JOBS[SID]
    assert system.get(SID)["status"] == "registered"
    assert "error" not in oa.discover(SID), "discover did not run once the removal was done"
    assert sessions.items[SID]["status"] == "discovered"
