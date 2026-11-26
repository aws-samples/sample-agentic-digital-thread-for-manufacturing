# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding chat's ``reset_system`` is the app's removal, not a second one.

The chat tool used to run its own removal: it deleted the System node,
scheduled the user's own secret and dropped the catalog entry, always as a
full removal, and took none of the app removal's claims. So it could:

* finish an unfinished DEFAULT removal as a full one, scheduling the
  credentials of a registration the user asked to keep;
* run during Reset All, during another removal of the same system, or during
  an admin connection Edit;
* leave the system's agent runtime and gateway target behind (it never ran
  the AgentCore cleanup).

In the app process the tool now runs the app's removal as the card starts
it (``POST /reset/<id>?full=true&wait=false``): the same claim, reset domain,
connection-lock ordering and sticky mode; the reset job runs the cascade and
the tool reports it (test_chat_removal_runs_as_the_cards_background_removal.py).
In the onboarding runtime container (``SYSTEM_ID=onboarding``) the
app's removal cannot run: that process has no permission to delete AgentCore
runtimes or targets and does not hold the app's catalog or locks. There the
tool removes nothing and says to remove the system from its card in the
Onboard view. Offline: Secrets Manager, the graph, AgentCore and the session
store are fakes; concurrency is driven by events.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents import connection_lock as cl  # noqa: E402
from agents.catalog import REMOVAL_FAILED  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_offboard_own_secret import _FakeSM  # noqa: E402
from tests.unit._chat_removal import removed_by_the_chat, wait_for_chat_removals  # noqa: E402

#: How long a test waits on a removal step or job: generous, for a slow,
#: loaded runner (a passing run never waits it).
JOB_WAIT = 60.0


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    if hasattr(onboard, "_ResetDomain"):
        monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    monkeypatch.setattr(onboard, "CHAT_REMOVAL_CHECK_WAIT_SECONDS", 0.0)
    with cl._REGISTRY_LOCK:
        cl._LOCKS.clear()
        cl._HOLDERS.clear()


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: "")
    return fake


@pytest.fixture
def session_store(monkeypatch):
    """The session store; no system here has an onboarding session."""
    from agents.onboarding_session import SessionNotFoundError
    store = MagicMock()
    store.get_session.side_effect = SessionNotFoundError("none")
    monkeypatch.setattr(oa, "_store", lambda: store)
    return store


class _Cascade:
    """The app removal's AgentCore / graph / proposal steps, faked; the
    catalog and the secret step are real. ``fail`` makes the graph delete
    raise."""

    def __init__(self):
        self.fail = False
        self.graph_deletes: list = []
        self.cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                               "wait_timed_out": False})
        self.writes: list = []

    def graph(self, sid):
        if self.fail:
            raise RuntimeError("transient graph delete failure")
        self.graph_deletes.append(sid)
        _NODES.discard(sid)

    def write(self, cypher, params=None):
        self.writes.append(cypher)
        return True


@pytest.fixture
def cascade(monkeypatch):
    c = _Cascade()
    for target, value in (("neptune_write", c.write),
                          ("neptune_query", MagicMock(return_value=[{}])),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", c.cleanup),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_reset_delete_graph_artifacts_invalidating", c.graph),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(oa, "neptune_write", c.write)
    return c


def _onboarded_user_pg(cat):
    """A user registration that was onboarded: its catalog entry and its
    System node."""
    _register_pg()
    cat.update_status(SID, "onboarded")
    _NODES.add(SID)


#: The system ids whose System node exists (``_onboarded_user_pg``).
_NODES: set = set()


@pytest.fixture(autouse=True)
def _system_nodes(secrets, monkeypatch):
    """After ``secrets`` (which fakes a graph with no nodes): the graph holds
    the System nodes in ``_NODES``."""
    _NODES.clear()
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: {"id": sid, "status": "onboarded"} if sid in _NODES else None)
    yield
    _NODES.clear()


# ── a fresh chat removal is the app's full removal ─────────────────────────

def test_a_fresh_chat_removal_runs_the_app_removal(cat, sm, session_store, cascade):
    _onboarded_user_pg(cat)
    res = removed_by_the_chat(oa.reset_system, SID)
    assert res.get("status") == "reset", res
    assert res.get("own_secret_deletion_scheduled") is True
    cascade.cleanup.assert_called_once()
    assert cascade.cleanup.call_args.args[0] == SID, "the agent runtime was not removed"
    assert cascade.graph_deletes == [SID]
    assert cat.get(SID) is None
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]
    assert onboard._RESET_JOBS[SID]["state"] == "done"
    assert onboard._RESET_JOBS[SID]["full"] is True
    session_store.delete_session.assert_called_once_with(SID)


def test_a_chat_removal_of_an_unknown_system_answers_the_app_refusal(cat, sm, session_store,
                                                                       cascade):
    res = oa.reset_system("nope")
    assert res == {"error": "Unknown system_id 'nope'"}
    cascade.cleanup.assert_not_called()
    session_store.delete_session.assert_not_called()


# ── sticky mode: a chat retry never changes an unfinished removal's mode ────

def _failed_default_removal(cat, cascade):
    cascade.fail = True
    with pytest.raises(Exception):
        asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=False))
    cascade.fail = False
    assert cat.get(SID)["status"] == REMOVAL_FAILED


def test_a_chat_retry_of_an_unfinished_default_removal_is_refused(cat, sm, session_store,
                                                                   cascade):
    _onboarded_user_pg(cat)
    _failed_default_removal(cat, cascade)
    res = oa.reset_system(SID)
    assert "error" in res and res.get("reason") == "removal_refused", res
    assert "full=false" in res["error"]
    assert sm.deleted == [], "the chat retry scheduled the credentials of a kept registration"
    assert cat.get(SID)["status"] == REMOVAL_FAILED
    session_store.delete_session.assert_not_called()


def test_a_chat_retry_after_a_restart_of_an_unfinished_default_removal_is_refused(
        cat, sm, session_store, cascade):
    """The mode comes from the catalog entry (rebuilt from the node's marker
    after a restart), not only from this process's job record."""
    _onboarded_user_pg(cat)
    cat.mark_removal_failed(SID, False)
    res = oa.reset_system(SID)
    assert res.get("reason") == "removal_refused", res
    assert sm.deleted == []


def test_a_chat_retry_of_an_unfinished_full_removal_finishes_it(cat, sm, session_store,
                                                                 cascade):
    _onboarded_user_pg(cat)
    cascade.fail = True
    with pytest.raises(Exception):
        asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=True))
    cascade.fail = False
    res = removed_by_the_chat(oa.reset_system, SID)
    assert res.get("status") == "reset" and cat.get(SID) is None, res


# ── the chat removal takes the app removal's claims ────────────────────────

def _in_thread(fn):
    out: dict = {}

    def _run():
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out["error"] = exc
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, out


def _block(monkeypatch, target: str, *, is_async: bool):
    """Replace ``onboard.<target>`` with a step that waits on an event."""
    entered, release = threading.Event(), threading.Event()
    if is_async:
        async def _step(*_a, **_kw):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return {"status": "reset", "system_id": SID, "removed": True,
                    "own_secret_deletion_scheduled": False}
    else:
        def _step(*_a, **_kw):
            entered.set()
            release.wait(JOB_WAIT)
            return {"status": "reset", "system_id": SID, "removed": True,
                    "own_secret_deletion_scheduled": False}
    monkeypatch.setattr(onboard, target, _step)
    return entered, release


def test_a_chat_removal_is_refused_while_reset_all_runs(cat, sm, session_store, cascade,
                                                         monkeypatch):
    _onboarded_user_pg(cat)
    entered, release = _block(monkeypatch, "_global_reset_steps", is_async=True)
    t, out = _in_thread(lambda: asyncio.run(onboard.onboard_reset(None)))
    assert entered.wait(JOB_WAIT)
    try:
        res = oa.reset_system(SID)
    finally:
        release.set()
        t.join(JOB_WAIT)
    assert res.get("reason") == "removal_refused", res
    assert "being reset" in res["error"]
    assert sm.deleted == [] and cat.get(SID) is not None
    cascade.cleanup.assert_not_called()


def test_reset_all_is_refused_while_a_chat_removal_runs(cat, sm, session_store, cascade,
                                                         monkeypatch):
    _onboarded_user_pg(cat)
    entered, release = _block(monkeypatch, "_run_reset_steps", is_async=True)
    t, out = _in_thread(lambda: oa.reset_system(SID))
    assert entered.wait(JOB_WAIT)
    try:
        with pytest.raises(onboard.HTTPException) as err:
            asyncio.run(onboard.onboard_reset(None))
    finally:
        release.set()
        t.join(JOB_WAIT)
        wait_for_chat_removals(SID)
    assert err.value.status_code == 409
    assert SID in err.value.detail
    assert out.get("result", {}).get("status") == "removing", out
    assert onboard._RESET_JOBS[SID]["state"] == "done"


def test_a_chat_removal_while_a_rest_removal_of_it_runs_starts_nothing(cat, sm, session_store,
                                                                        cascade, monkeypatch):
    _onboarded_user_pg(cat)
    entered, release = _block(monkeypatch, "_run_reset_steps", is_async=True)
    t, out = _in_thread(
        lambda: asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=False)))
    assert entered.wait(JOB_WAIT)
    try:
        res = oa.reset_system(SID)
    finally:
        release.set()
        t.join(JOB_WAIT)
    assert res.get("status") == "removing" and "still being removed" in res["message"], res
    assert out.get("result", {}).get("status") == "reset", out
    assert onboard._RESET_JOBS[SID]["full"] is False, "the chat changed the running removal"
    session_store.delete_session.assert_not_called()


# ── ordering with the admin connection Edit ─────────────────────────────────


# ── the onboarding runtime container does not remove ───────────────────────

def test_in_the_onboarding_runtime_the_chat_tool_removes_nothing(cat, sm, session_store,
                                                                  cascade, monkeypatch):
    _onboarded_user_pg(cat)
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: {"id": sid, "status": "onboarded"} if sid == SID else {})
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove system" in res["error"] and "Onboard" in res["error"]
    assert cascade.writes == [], "the runtime wrote to the graph"
    assert sm.deleted == []
    assert cat.get(SID)["status"] == "onboarded"
    cascade.cleanup.assert_not_called()
    session_store.delete_session.assert_not_called()


def test_the_runtime_never_runs_the_apps_removal(sm, session_store, monkeypatch):
    """The runtime may read the System node and the session; it never runs
    the app's removal and never writes the graph."""
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: {"id": sid})
    boom = MagicMock(side_effect=AssertionError("ran"))
    with patch.object(onboard, "start_removal_for_chat", boom), \
            patch.object(oa, "neptune_write", boom):
        res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    boom.assert_not_called()
