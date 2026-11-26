# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A worker that finishes as its job is abandoned leaves no phantom lease.

A finishing ask worker leaves the local registry, releases the shared slot
(exact owner; this also drops it from the detached set) and only then writes
its terminal record. A result poll that hits the 20-minute ceiling in that
gap still sees the job running: it adds the job to the detached set again
and wins the terminal write (the abandoned tombstone), so the worker's own
write is refused. Nothing removed that entry and no renewer owned it, so a
worker that was gone still counted for the 180 s grace, and with one real
detached worker it falsely refused a new query ("two earlier queries were
abandoned"). Now a worker whose terminal write was refused releases again
(exact owner): the entry goes, and a newer holder of the slot is untouched.

Deterministic: the worker is paused right after its first release by a
wrapper, the expiry runs from the test thread, then the worker resumes. A
second JobStore over the same conditional fake table is the other replica.
"""

import asyncio
import threading
import time
import types

import pytest

import agents.server as server
from agents.job_store import JobStore
from agents.models import AskRequest
from tests.unit.test_ask_async_abandoned_bound_shared import _GatedClient, _SharedTable

T0 = 1_800_000_000.0


@pytest.fixture
def env(monkeypatch):
    import agents.chat_client as chat_client_mod
    table = _SharedTable()
    a, b = JobStore("ask", table=table), JobStore("ask", table=table)
    clock = {"now": T0}
    monkeypatch.setattr(server, "_ASK_JOBS", a)
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _GatedClient)
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    monkeypatch.setattr(server, "_ASK_LOCAL_WORKERS", {})
    monkeypatch.setattr(server, "_ASK_RENEWER", {"thread": None})
    monkeypatch.setattr(server, "_ASK_LEASE_RENEW_SECONDS", 30.0)
    _GatedClient.gates = {}
    yield types.SimpleNamespace(a=a, b=b, clock=clock)
    for gate in _GatedClient.gates.values():
        gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and any(
            t.is_alive() and getattr(t, "_target", None) is server._run_ask_job
            for t in threading.enumerate()):
        time.sleep(0.02)
    for _ in range(3):
        server._ASK_RENEWER_WAKE.set()
        renewer = server._ASK_RENEWER.get("thread")
        if renewer is not None:
            renewer.join(2)
    server._ASK_RENEWER_WAKE.clear()


def _submit(question: str) -> str:
    _GatedClient.gates[question] = threading.Event()
    res = server.ask_async(AskRequest(question=question))
    assert res.get("status") == "running", res
    return res["job_id"]


def _worker(job_id: str):
    return next((t for t in threading.enumerate()
                 if getattr(t, "_target", None) is server._run_ask_job
                 and job_id in (getattr(t, "_args", ()) or ())), None)


def _slot(env) -> dict:
    return env.b.get(server._ASK_ACTIVE_KEY) or {}


def _pause_after_first_release(monkeypatch, job_id: str):
    """Wrap _ask_slot_release: the worker's FIRST release of ``job_id`` runs,
    then the worker waits until the test resumes it."""
    released, resume = threading.Event(), threading.Event()
    real = server._ask_slot_release
    calls = {"n": 0}

    def _wrapped(jobs, jid, now):
        real(jobs, jid, now)
        if jid == job_id and threading.current_thread() is _worker(job_id):
            calls["n"] += 1
            if calls["n"] == 1:
                released.set()
                resume.wait(10)
    monkeypatch.setattr(server, "_ask_slot_release", _wrapped)
    return released, resume, calls


def test_a_worker_finishing_as_it_is_abandoned_leaves_no_phantom_lease(env, monkeypatch):
    q1 = _submit("q1")
    released, resume, _calls = _pause_after_first_release(monkeypatch, q1)
    _GatedClient.gates["q1"].set()                        # q1's answer arrives
    assert released.wait(10), "the worker never reached its release"
    # In the gap: the slot is free (a newer ask takes it), q1 is still running.
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    q2 = _submit("q2")
    reply = asyncio.run(server.ask_result(q1, wait=0.0))  # the ceiling: abandoned
    assert reply["error_detail"].endswith("abandoned; ask again"), reply
    assert q1 in _slot(env)["detached"]                   # the expiry re-added it
    resume.set()
    _worker(q1) and _worker(q1).join(5)
    slot = _slot(env)
    assert q1 not in (slot.get("detached") or {}), slot
    assert (slot["job_id"], slot["status"]) == (q2, "running"), slot
    assert env.a.get(q1)["abandoned"] is True             # the tombstone still stands
    # q1 no longer counts on the other replica.
    assert q1 not in server._ask_detached(slot, env.clock["now"])


def test_the_phantom_would_falsely_refuse_the_next_ask(env, monkeypatch):
    """With one real detached worker, a phantom entry made the next ask look
    like a third abandoned one."""
    q0 = _submit("q0")                                     # a genuinely wedged ask
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    asyncio.run(server.ask_result(q0, wait=0.0))          # detached and alive
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 2
    q1 = _submit("q1")
    released, resume, _calls = _pause_after_first_release(monkeypatch, q1)
    _GatedClient.gates["q1"].set()
    assert released.wait(10)
    target = env.clock["now"] + server._ASK_RUN_CEILING_SECONDS + 1
    while env.clock["now"] < target:                       # q0 stays alive: its process renews
        env.clock["now"] = min(target, env.clock["now"] + server._ASK_LEASE_GRACE_SECONDS / 3)
        server._renew_detached_leases(env.a, {q0}, env.clock["now"])
    asyncio.run(server.ask_result(q1, wait=0.0))
    resume.set()
    _worker(q1) and _worker(q1).join(5)
    assert set(server._ask_detached(_slot(env), env.clock["now"])) == {q0}, _slot(env)
    assert server._claim_ask_slot(env.b, "q-next", env.clock["now"]) == ""


def test_guard_a_worker_finishing_normally_releases_once(env, monkeypatch):
    q1 = _submit("q1")
    real = server._ask_slot_release
    calls: list = []
    monkeypatch.setattr(server, "_ask_slot_release",
                        lambda jobs, jid, now: calls.append(jid) or real(jobs, jid, now))
    _GatedClient.gates["q1"].set()
    _worker(q1) and _worker(q1).join(5)
    assert calls == [q1]                                   # no extra pass when it won
    assert env.a.get(q1)["status"] == "done"
    assert _slot(env)["status"] == "idle"
