# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An expired async ask stays expired, and abandoned workers are bounded.

After the 20-minute ceiling the result route reported an error, deleted the
record and released the slot, but the background worker kept running: its
later unconditional write recreated the record (the same job id could then
answer ``done`` after a terminal error), and a replacement query could run
alongside it, any number of times. Now the expiry leaves a terminal
tombstone, the worker's completion is a conditional ``running -> terminal``
write (a no-op on the tombstone), the answer says the query was
"abandoned" (the worker cannot be interrupted), and a new submit is refused
while more than one abandoned worker is still alive. Offline: JobStore runs
memory-only; the chat client is a scripted stand-in.
"""

import asyncio
import threading
import time
import types

import pytest

import agents.server as server
from agents.events import DoneEvent, MessageEvent
from agents.job_store import JobStore
from agents.models import AskRequest

ABANDONED = "the query ran longer than 20 minutes and was abandoned; ask again"


class _GatedClient:
    """A chat client whose stream blocks until its gate opens (a wedged run)."""

    gates: dict = {}

    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        # The prompt carries a temporal-context prefix: key on its suffix.
        key = next(k for k in type(self).gates if prompt.endswith(k))
        gate = type(self).gates[key]
        gate.wait(10)
        yield MessageEvent(content=f"late answer to {key}")
        yield DoneEvent()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    import agents.chat_client as chat_client_mod
    store = JobStore("ask", table=None)
    monkeypatch.setattr(server, "_ASK_JOBS", store)
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _GatedClient)
    clock = {"now": time.time()}
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    _GatedClient.gates = {}
    yield types.SimpleNamespace(jobs=store, clock=clock)
    for gate in _GatedClient.gates.values():
        gate.set()
    for t in list(getattr(server, "_ASK_WORKERS", {}).values()):
        t.join(5)


def _submit(question: str):
    _GatedClient.gates[question] = threading.Event()
    return server.ask_async(AskRequest(question=question))


def _poll(job_id: str) -> dict:
    return asyncio.run(server.ask_result(job_id, wait=0.0))


def _expire(env, job_id: str) -> dict:
    env.clock["now"] = env.jobs.get(job_id)["started_at"] + server._ASK_RUN_CEILING_SECONDS + 1
    return _poll(job_id)


def _wait_worker_done(job_id: str) -> None:
    """Wait for the job's worker thread to end (the registry when the code
    keeps one, else the worker's own thread by its target)."""
    t = getattr(server, "_ASK_WORKERS", {}).get(job_id)
    if t is not None:
        t.join(5)
        return
    deadline = time.time() + 5
    while time.time() < deadline and any(
            th.is_alive() and getattr(th, "_target", None) is server._run_ask_job
            and job_id in (getattr(th, "_args", ()) or ())
            for th in threading.enumerate()):
        time.sleep(0.02)


def test_the_expired_answer_says_abandoned_not_stopped(env):
    job = _submit("q1")["job_id"]
    reply = _expire(env, job)
    assert reply == {"status": "error", "error": "ask_failed", "error_detail": ABANDONED}, reply


def test_an_old_worker_finishing_after_expiry_cannot_resurrect_its_record(env):
    """An old worker finishing after expiry: expire, start a replacement,
    finish the old worker; the old record stays terminal and the replacement's
    slot holds."""
    old = _submit("q-old")["job_id"]
    _expire(env, old)
    env.clock["now"] = time.time()
    new = _submit("q-new")
    assert new.get("status") == "running", new
    slot_before = dict(env.jobs.get(server._ASK_ACTIVE_KEY))
    _GatedClient.gates["q-old"].set()
    _wait_worker_done(old)
    assert env.jobs.get(old)["status"] == "error", env.jobs.get(old)
    assert _poll(old)["error_detail"] == ABANDONED      # still terminal, every time
    assert _poll(old)["error_detail"] == ABANDONED
    slot = env.jobs.get(server._ASK_ACTIVE_KEY)
    holder = ("status", "job_id", "started_at")
    assert {k: slot[k] for k in holder} == {k: slot_before[k] for k in holder}, slot
    assert slot["job_id"] == new["job_id"], slot
    # The old worker ended, so it is no longer counted as detached.
    assert old in slot_before["detached"] and old not in slot["detached"], (slot_before, slot)
    # The replacement still delivers its own answer.
    _GatedClient.gates["q-new"].set()
    _wait_worker_done(new["job_id"])
    assert _poll(new["job_id"]) == {"status": "done", "answer": "late answer to q-new"}


def test_a_new_submit_is_refused_while_two_abandoned_workers_are_alive(env):
    first = _submit("q-a")["job_id"]
    _expire(env, first)
    env.clock["now"] = time.time()
    second = _submit("q-b")["job_id"]                   # one abandoned alive: allowed
    _expire(env, second)
    env.clock["now"] = time.time()
    refused = _submit("q-c")
    assert getattr(refused, "status_code", None) == 429, refused
    body = refused.body.decode()
    assert "abandoned" in body and "still finishing" in body, body
    # Once one of them finishes, a new query is accepted again.
    _GatedClient.gates["q-a"].set()
    _wait_worker_done(first)
    assert _submit("q-d").get("status") == "running"


def test_guard_a_worker_that_finishes_in_time_is_delivered(env):
    job = _submit("q-ok")["job_id"]
    _GatedClient.gates["q-ok"].set()
    _wait_worker_done(job)
    assert _poll(job) == {"status": "done", "answer": "late answer to q-ok"}


def test_guard_a_job_finishing_just_before_its_expiry_is_delivered(env, monkeypatch):
    """The expiry is conditional too: a job whose worker wrote 'done' between
    the poll's read and its expiry write is delivered, not overwritten."""
    job = _submit("q-race")["job_id"]
    _GatedClient.gates["q-race"].set()
    _wait_worker_done(job)
    stale = {"status": "running", "started_at": env.jobs.get(job).get("started_at", 0)}
    assert server._expire_overdue_ask(env.jobs, job, stale)["status"] == "done"
