# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system's removal deletes its onboarding session, from every caller.

The card's Remove system (``_run_reset_cascade``, default and ``full``) and
Reset All (``_global_reset_cascade``) removed a system's runtime, gateway
target, proposals and graph data but left its ``adt-onboarding-sessions``
item. Only the chat's removal deleted it. A session left over makes the
chat's discover of the same id refuse (``_existing_session_refusal``) and tell
the model to go on from a step of the removed onboarding, for up to the
session's 7-day TTL. Seen live 2026-10-10: ``chat-scratch-mcp`` was
fully removed from its card and its "resolved" session stayed until it was
deleted by hand.

Pinned here:
  1. both modes of the single removal delete the session once the graph
     artifacts are gone (before the catalog step), and report it;
  2. a session-store failure is logged and the removal still answers
     ``reset`` with its usual fields;
  3. Reset All deletes the session of every removed id; one failing id
     stops neither the others nor the reset;
  4. a removal that stops before its graph step leaves the session;
  5. end to end: after the removal, the chat's discover gate lets discover
     run again;
  6. Reset All also clears the session of a system that was registered but
     never onboarded (no graph node, runtime or target), without handing it
     to proposal retirement;
  7. the cleared counts count sessions that were there, not delete calls.

Offline: AgentCore, the graph, the catalog and DynamoDB are fakes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.offboard_retirement as retirement  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.onboarding_session import (  # noqa: E402
    STATUS_RESOLVED,
    OnboardingSessionStore,
    SessionNotFoundError,
)

SID = "synth-sess-1"


# ── fakes ───────────────────────────────────────────────────────────────────

class _RecordingStore:
    """The onboarding session store: records each delete in ``events``
    (shared with the cascade's other steps, so the order shows); ids in
    ``fail`` raise."""

    def __init__(self, events: list, fail: set | None = None):
        self.events = events
        self.fail = fail or set()
        self.deleted: list = []

    def delete_session(self, system_id):
        if system_id in self.fail:
            raise RuntimeError("dynamodb unavailable")
        self.deleted.append(system_id)
        self.events.append(("session", system_id))
        return True

    def get_session(self, system_id):
        raise SessionNotFoundError(system_id)


class _Table:
    """A dict-backed DynamoDB table: enough of the API for the real
    OnboardingSessionStore's create, read and delete (``ReturnValues=ALL_OLD``
    answers the deleted item, as DynamoDB does)."""

    def __init__(self):
        self.items: dict = {}

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg
        item = self.items.get(Key["system_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        self.items[Item["system_id"]] = dict(Item)

    def delete_item(self, Key, ReturnValues=None, **_kw):  # noqa: N803
        item = self.items.pop(Key["system_id"], None)
        return {"Attributes": item} if item and ReturnValues == "ALL_OLD" else {}


class _Resource:
    def __init__(self, table):
        self._table = table

    def Table(self, _name):  # noqa: N802 - boto3 resource API
        return self._table


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))


def _cleanup() -> MagicMock:
    return MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [SID],
                                   "wait_timed_out": False})


def _single(monkeypatch, events: list, store, *, graph_fails: bool = False) -> MagicMock:
    """Fake every side effect of the single-system cascade; the graph step and
    the catalog record into ``events``. Returns the catalog."""
    cat = MagicMock()
    cat.get.return_value = {"id": SID, "status": "onboarded"}
    cat.remove.side_effect = lambda sid: events.append(("catalog", sid)) or True
    cat.update_status.side_effect = lambda sid, status: events.append(("catalog", sid))

    def _graph(sid):
        if graph_fails:
            raise onboard.ResetIncompleteError("graph delete failed")
        events.append(("graph", sid))

    for target, value in (("neptune_write", MagicMock(return_value=True)),
                          ("catalog", cat),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", _cleanup()),
                          ("_reset_delete_graph_artifacts_invalidating", _graph),
                          ("_delete_own_secret_or_incomplete", lambda sid: ""),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)
    monkeypatch.setattr(oa, "_store", lambda: store)
    return cat


# ── 1. the single removal deletes the session after the graph step ──────────

@pytest.mark.parametrize("full", [False, True], ids=["keep-registration", "full"])
def test_a_removal_deletes_the_session_after_the_graph_step(monkeypatch, full):
    events: list = []
    store = _RecordingStore(events)
    _single(monkeypatch, events, store)
    result = asyncio.run(onboard._run_reset_cascade(SID, full=full))
    assert result["status"] == "reset" and result["system_id"] == SID
    assert result["kept_registration"] is (not full)
    assert store.deleted == [SID], "the system's onboarding session was not deleted"
    assert result.get("session_cleared") is True
    assert events.index(("graph", SID)) < events.index(("session", SID)) \
        < events.index(("catalog", SID)), events


@pytest.mark.parametrize("full", [False, True], ids=["keep-registration", "full"])
def test_a_session_store_failure_is_logged_and_the_removal_still_completes(
        monkeypatch, caplog, full):
    events: list = []
    store = _RecordingStore(events, fail={SID})
    cat = _single(monkeypatch, events, store)
    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        result = asyncio.run(onboard._run_reset_cascade(SID, full=full))
    assert result["status"] == "reset" and result["system_id"] == SID
    assert result.get("session_cleared") is False
    assert result["removed"] is full and result["kept_registration"] is (not full)
    if full:
        cat.remove.assert_called_once_with(SID)
    else:
        cat.update_status.assert_called_once_with(SID, "registered")
    assert any(SID in r.getMessage() and "onboarding session" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING), caplog.text


def test_a_removal_that_stops_before_its_graph_step_keeps_the_session(monkeypatch):
    events: list = []
    store = _RecordingStore(events)
    _single(monkeypatch, events, store, graph_fails=True)
    with pytest.raises(onboard.ResetIncompleteError):
        asyncio.run(onboard._run_reset_cascade(SID, full=False))
    assert store.deleted == []


# ── 3. Reset All deletes every removed system's session ────────────────────

def _global(monkeypatch, events: list, store, *, wipe_fails: bool = False,
            catalog_entries: list | None = None, retired: list | None = None) -> MagicMock:
    """Fake Reset All's side effects. ``catalog_entries``: every catalog entry
    (default: the two onboarded ones); ``retired`` collects the ids handed to
    proposal retirement."""
    cat = MagicMock()
    cat.get_onboarded.return_value = [{"id": "sys-a"}, {"id": "sys-b"}]
    cat.get_all.return_value = catalog_entries if catalog_entries is not None else [
        {"id": "sys-a", "status": "onboarded"}, {"id": "sys-b", "status": "onboarded"}]
    cat.clear_all.return_value = 2
    cat.reset_all.side_effect = lambda: events.append(("catalog", "all"))

    def _wipe():
        if wipe_fails:
            raise onboard.ResetIncompleteError("graph wipe failed")
        events.append(("graph", "all"))
        return 0

    for target, value in (("catalog", cat),
                          ("_graph_system_ids", lambda: ["sys-d"]),
                          ("_global_reset_cleanup_agentcore_sync",
                           MagicMock(return_value={"runtimes_awaited": [],
                                                   "targets_awaited": ["sys-a", "sys-c"],
                                                   "wait_timed_out": False})),
                          ("_delete_user_registration_secrets", lambda: 0),
                          ("_global_reset_wipe_graph", _wipe),
                          ("reset_agents", MagicMock()),
                          ("_agent_manager", MagicMock())):
        monkeypatch.setattr(onboard, target, value)
    sink = retired if retired is not None else []
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems",
                        lambda ids: sink.extend(ids) or 0)
    monkeypatch.setattr(oa, "_store", lambda: store)
    return cat


@pytest.mark.parametrize("full", [False, True], ids=["keep-registrations", "full"])
def test_reset_all_deletes_the_session_of_every_removed_system(monkeypatch, full):
    events: list = []
    store = _RecordingStore(events)
    _global(monkeypatch, events, store)
    result = asyncio.run(onboard._global_reset_cascade(full))
    assert result["status"] == "reset"
    assert sorted(store.deleted) == ["sys-a", "sys-b", "sys-c", "sys-d"]
    assert result.get("sessions_cleared") == 4
    first_session = min(i for i, e in enumerate(events) if e[0] == "session")
    assert events.index(("graph", "all")) < first_session, events
    assert max(i for i, e in enumerate(events) if e[0] == "session") \
        < events.index(("catalog", "all")), events


def test_one_failing_session_delete_stops_neither_the_others_nor_the_reset(
        monkeypatch, caplog):
    events: list = []
    store = _RecordingStore(events, fail={"sys-b"})
    cat = _global(monkeypatch, events, store)
    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        result = asyncio.run(onboard._global_reset_cascade(False))
    assert result["status"] == "reset"
    assert sorted(store.deleted) == ["sys-a", "sys-c", "sys-d"]
    assert result.get("sessions_cleared") == 3
    cat.reset_all.assert_called_once()
    assert any("sys-b" in r.getMessage() and "onboarding session" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING), caplog.text


def test_a_reset_all_that_stops_before_its_graph_wipe_keeps_the_sessions(monkeypatch):
    events: list = []
    store = _RecordingStore(events)
    _global(monkeypatch, events, store, wipe_fails=True)
    with pytest.raises(onboard.ResetIncompleteError):
        asyncio.run(onboard._global_reset_cascade(False))
    assert store.deleted == []


# ── 5. end to end: the chat's discover runs again after the removal ────────

@pytest.mark.parametrize("full", [False, True], ids=["keep-registration", "full"])
def test_after_a_card_removal_the_chats_discover_runs_again(monkeypatch, full):
    table = _Table()
    store = OnboardingSessionStore(table_name="t", dynamodb_resource=_Resource(table))
    store.create_session(SID, initiated_by="test", connection_type="MCP Server", schema="")
    table.items[SID]["status"] = STATUS_RESOLVED
    _single(monkeypatch, [], store)
    monkeypatch.setattr(oa, "_removal_marker_row", lambda sid: {})
    refused = oa._existing_session_refusal(SID)
    assert refused and refused.get("next_tool") == "provision_system", refused
    result = asyncio.run(onboard._run_reset_steps(SID, full=full))
    assert result["status"] == "reset"
    assert SID not in table.items, "the removed onboarding's session was kept"
    assert oa._existing_session_refusal(SID) is None


# ── 6. Reset All clears a registered-only system's half-done session ───────

def _real_store(*resolved_ids: str) -> tuple[OnboardingSessionStore, _Table]:
    """The real session store over a dict table holding a ``resolved``
    session for each of ``resolved_ids``."""
    table = _Table()
    store = OnboardingSessionStore(table_name="t", dynamodb_resource=_Resource(table))
    for sid in resolved_ids:
        store.create_session(sid, initiated_by="test", connection_type="MCP Server", schema="")
        table.items[sid]["status"] = STATUS_RESOLVED
    return store, table


@pytest.mark.parametrize("full", [False, True], ids=["keep-registrations", "full"])
def test_reset_all_clears_the_session_of_a_registered_system_never_onboarded(
        monkeypatch, full):
    """A registered entry with a half-done chat onboarding (a resolved
    session) and no System node, agent runtime or gateway target."""
    store, table = _real_store("sys-reg")
    retired: list = []
    _global(monkeypatch, [], store, retired=retired, catalog_entries=[
        {"id": "sys-a", "status": "onboarded"}, {"id": "sys-b", "status": "onboarded"},
        {"id": "sys-reg", "status": "registered"}])
    monkeypatch.setattr(oa, "_removal_marker_row", lambda sid: {})
    result = asyncio.run(onboard._global_reset_cascade(full))
    assert result["status"] == "reset"
    assert "sys-reg" not in table.items, "Reset All kept the registered system's session"
    assert oa._existing_session_refusal("sys-reg") is None
    assert "sys-reg" not in retired, "a registered-only system reached proposal retirement"
    assert sorted(retired) == ["sys-a", "sys-b", "sys-c", "sys-d"]


# ── 7. the cleared counts count sessions that were there ───────────────────

def test_delete_session_answers_whether_a_session_was_there():
    store, table = _real_store(SID)
    assert store.delete_session("never-onboarded") is False
    assert store.delete_session(SID) is True
    assert SID not in table.items
    assert store.delete_session(SID) is False, "a second delete is a no-op"


@pytest.mark.parametrize("full", [False, True], ids=["keep-registration", "full"])
def test_a_removal_with_no_session_reports_none_cleared(monkeypatch, full):
    store, _table = _real_store()
    _single(monkeypatch, [], store)
    result = asyncio.run(onboard._run_reset_cascade(SID, full=full))
    assert result["status"] == "reset"
    assert result.get("session_cleared") is False


def test_reset_all_counts_only_the_sessions_it_deleted(monkeypatch):
    store, table = _real_store("sys-a", "sys-c")
    _global(monkeypatch, [], store)
    result = asyncio.run(onboard._global_reset_cascade(False))
    assert result["status"] == "reset"
    assert table.items == {}
    assert result.get("sessions_cleared") == 2, result
