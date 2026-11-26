# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's removal of an onboarded system is the card's background removal.

The chat's ``reset_system`` used to run the card's full removal waited, inside
the chat's tool call: the chat's stream stayed silent for the whole cascade,
and the cascade waited for the agent runtime's delete only as long as a
request may wait (``onboard._DELETE_WAIT_DEADLINE_S``). A VPC runtime delete
takes longer, so the removal stopped part-way: the catalog entry
``removal_failed``, the System node kept with its removal marker, the stored
credentials not scheduled for deletion and the onboarding session left.

Now the tool claims the removal as the card's ``?wait=false`` removal does and
the reset job runs the cascade to its end on its own thread, whatever happens
to the chat's request; the tool answers at once. A later call reports how the
job is doing, or how it ended, and starts nothing while it runs. The session
is deleted by the job when the removal finishes. Offline, on the fixtures of
test_chat_removal_is_the_app_removal.py.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit._chat_removal import wait_for_chat_removals
from tests.unit.test_chat_removal_is_the_app_removal import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _NODES, _fresh, _onboarded_user_pg, _system_nodes, cascade, cat, no_dns,
    secrets, session_store, sm)

#: How long a VPC agent runtime's delete took, live (2026-09-27: ~380 s).
RUNTIME_DELETE_SECONDS = 380.0

#: How long a step or job may take on a slow, loaded runner.
JOB_WAIT = 60.0
#: The bound on an answer that comes at once (the removal itself is held for
#: longer than this, so an answer that waits for it never comes in time).
AT_ONCE = JOB_WAIT / 2


def _removal_finished() -> None:
    wait_for_chat_removals(SID)


@pytest.fixture(autouse=True)
def _checks_answer_at_once(monkeypatch):
    monkeypatch.setattr(onboard, "CHAT_REMOVAL_CHECK_WAIT_SECONDS", 0.0, raising=False)


@pytest.fixture
def graph_node_goes(cascade, monkeypatch):
    """The cascade's graph delete removes the System node too."""
    real = cascade.graph

    def _delete(sid):
        real(sid)
        _NODES.discard(sid)
    monkeypatch.setattr(onboard, "_reset_delete_graph_artifacts_invalidating", _delete)


def _blocked_cleanup(cascade, monkeypatch):
    """The AgentCore cleanup (runtime and target deletes) waits on an event."""
    entered, release = threading.Event(), threading.Event()

    def _cleanup(sid, deadline):
        entered.set()
        release.wait(JOB_WAIT)
        return cascade.cleanup(sid, deadline)
    monkeypatch.setattr(onboard, "_reset_cleanup_agentcore_sync", _cleanup)
    return entered, release


def _answer_within(fn, seconds: float) -> dict:
    """``fn()`` on a thread (as the agent runs a tool); its answer, or
    ``{}`` when none came within ``seconds``."""
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(fn()), daemon=True)
    t.start()
    t.join(seconds)
    return dict(out)


def _assert_removed_completely(cat, sm, session_store, cascade) -> None:
    assert cat.get(SID) is None, "the catalog entry was kept"
    assert cascade.cleanup.call_args.args[0] == SID, "the runtime and target were not removed"
    assert cascade.graph_deletes == [SID] and SID not in _NODES, "the System node was kept"
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]
    session_store.delete_session.assert_called_once_with(SID)
    assert onboard._RESET_JOBS[SID]["state"] == "done"


# ── the removal runs on without the chat's request ─────────────────────────

def test_the_chat_answers_at_once_and_the_removal_runs_on(cat, sm, session_store, cascade,
                                                          graph_node_goes, monkeypatch):
    _onboarded_user_pg(cat)
    entered, release = _blocked_cleanup(cascade, monkeypatch)
    try:
        started = _answer_within(lambda: oa.reset_system(SID), AT_ONCE)
        assert entered.wait(JOB_WAIT)
        assert started.get("status") == "removing", started
        session_store.delete_session.assert_not_called()      # the job deletes it, at its end
        assert cat.get(SID) is not None
    finally:
        release.set()
    _removal_finished()
    _assert_removed_completely(cat, sm, session_store, cascade)
    done = oa.reset_system(SID)
    assert done.get("status") == "reset", done
    assert done.get("removed") is True and done.get("own_secret_deletion_scheduled") is True
    assert cascade.cleanup.call_count == 1


def test_the_removal_completes_after_the_chat_turn_is_cancelled(cat, sm, session_store,
                                                                cascade, graph_node_goes,
                                                                monkeypatch):
    """The chat's turn (the agent running the tool on a worker thread, then
    waiting for the model) is cancelled mid-removal, as when its stream is
    cut; the removal still finishes."""
    _onboarded_user_pg(cat)
    entered, release = _blocked_cleanup(cascade, monkeypatch)
    answers: list = []
    turn: dict = {}

    async def _chat_turn():
        answers.append(await asyncio.to_thread(oa.reset_system, SID))
        await asyncio.sleep(3600)                               # the model's next step

    def _run_turn():
        loop = asyncio.new_event_loop()
        turn["loop"], turn["task"] = loop, loop.create_task(_chat_turn())
        try:
            loop.run_until_complete(turn["task"])
        except asyncio.CancelledError:
            turn["cancelled"] = True
        finally:
            loop.close()
    runner = threading.Thread(target=_run_turn, daemon=True)
    runner.start()
    try:
        assert entered.wait(JOB_WAIT)
        deadline = time.monotonic() + AT_ONCE
        while not answers and time.monotonic() < deadline:
            time.sleep(0.02)
        assert answers and answers[0].get("status") == "removing", answers
        turn["loop"].call_soon_threadsafe(turn["task"].cancel)
        runner.join(JOB_WAIT)
        assert turn.get("cancelled") and not runner.is_alive()
    finally:
        release.set()
    _removal_finished()
    _assert_removed_completely(cat, sm, session_store, cascade)


def test_a_runtime_delete_longer_than_a_request_may_wait_still_finishes(
        cat, sm, session_store, cascade, graph_node_goes, monkeypatch):
    _onboarded_user_pg(cat)

    def _cleanup(sid, deadline):
        cascade.cleanup(sid, deadline)
        return {"runtimes_awaited": [f"adt_{sid}"], "targets_awaited": [sid],
                "wait_timed_out": deadline < RUNTIME_DELETE_SECONDS}
    monkeypatch.setattr(onboard, "_reset_cleanup_agentcore_sync", _cleanup)
    oa.reset_system(SID)
    _removal_finished()
    _assert_removed_completely(cat, sm, session_store, cascade)
    assert oa.reset_system(SID).get("status") == "reset"


# ── a check reports; it never starts a second removal ──────────────────────

def test_a_check_while_it_runs_answers_progress_and_starts_nothing(cat, sm, session_store,
                                                                   cascade, graph_node_goes,
                                                                   monkeypatch):
    _onboarded_user_pg(cat)
    entered, release = _blocked_cleanup(cascade, monkeypatch)
    steps = []
    real_steps = onboard._run_reset_steps

    async def _counted(*a, **kw):
        steps.append(a[0])
        return await real_steps(*a, **kw)
    monkeypatch.setattr(onboard, "_run_reset_steps", _counted)
    try:
        first = _answer_within(lambda: oa.reset_system(SID), AT_ONCE)
        assert entered.wait(JOB_WAIT)
        second = _answer_within(lambda: oa.reset_system(SID), AT_ONCE)
    finally:
        release.set()
    assert first.get("status") == "removing", first
    assert second.get("status") == "removing", second
    assert "still being removed" in second["message"], second
    _removal_finished()
    assert steps == [SID] and cascade.cleanup.call_count == 1
    _assert_removed_completely(cat, sm, session_store, cascade)


def test_a_check_waits_for_a_removal_about_to_finish(cat, sm, session_store, cascade,
                                                     graph_node_goes, monkeypatch):
    monkeypatch.setattr(onboard, "CHAT_REMOVAL_CHECK_WAIT_SECONDS", JOB_WAIT, raising=False)
    _onboarded_user_pg(cat)
    entered, release = _blocked_cleanup(cascade, monkeypatch)
    try:
        oa.reset_system(SID)
        assert entered.wait(JOB_WAIT)
        threading.Timer(0.3, release.set).start()
        checked = _answer_within(lambda: oa.reset_system(SID), JOB_WAIT)
    finally:
        release.set()
    assert checked.get("status") == "reset", checked     # one check, the finished answer


def test_the_answer_tells_the_model_it_takes_a_few_minutes_and_to_check_back(
        cat, sm, session_store, cascade, graph_node_goes, monkeypatch):
    _onboarded_user_pg(cat)
    entered, release = _blocked_cleanup(cascade, monkeypatch)
    try:
        started = _answer_within(lambda: oa.reset_system(SID), AT_ONCE)
    finally:
        release.set()
    text = started.get("message", "")
    assert started.get("status") == "removing", started
    assert "a few minutes" in text and "Tell the user" in text, text
    assert "call reset_system again" in text, text


def test_a_removal_that_did_not_finish_is_reported_then_finished_on_request(
        cat, sm, session_store, cascade, graph_node_goes):
    _onboarded_user_pg(cat)
    cascade.fail = True
    oa.reset_system(SID)
    _removal_finished()
    reported = oa.reset_system(SID)
    assert reported.get("reason") == "removal_incomplete", reported
    assert "did not finish" in reported["error"] and "reset_system again" in reported["error"]
    assert cascade.cleanup.call_count == 1, "the check started a second removal"
    cascade.fail = False
    again = oa.reset_system(SID)
    assert again.get("status") == "removing", again
    _removal_finished()
    assert oa.reset_system(SID).get("status") == "reset"
    assert cat.get(SID) is None and session_store.delete_session.call_count == 1
