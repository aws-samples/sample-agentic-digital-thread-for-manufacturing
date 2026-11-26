# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The lease renewer never leaves a live detached worker unrenewed.

Each process renews the shared leases of its own live detached ask workers
with one renewer thread. Two lifecycle gaps let a live worker go unrenewed,
so its lease expired after the 180 s grace and it stopped counting toward
the deployment-wide bound:

1. A new worker was recorded BEFORE it was started. An existing renewer
   (about to exit: its last worker had just ended) could wake in that gap,
   see the new thread as not alive, find no live worker, clear itself and
   exit; the worker then started with no renewer.
2. A renewer that ended unexpectedly was not replaced until a later ask was
   accepted, and with two detached workers no ask is accepted, so the
   leases expired first.

Now the worker is started and recorded, and the renewer elected, as one
operation under the process lock (a start failure records nothing and
releases the slot), and the renewer's ``finally`` clears its ownership and
starts a replacement that renews at once whenever live workers remain.
Deterministic: the renewer interval is long, every pass is woken by the test,
and the clock is fake; the other "replica" is a second JobStore over the same
fake conditional table.
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


class _Die(BaseException):
    """Ends the renewer thread the way an unexpected error would."""


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
    monkeypatch.setattr(server, "_ASK_LEASE_RENEW_SECONDS", 30.0)   # passes are test-woken
    monkeypatch.setattr(server, "_ASK_LOCAL_WORKERS", {})
    monkeypatch.setattr(server, "_ASK_RENEWER", {"thread": None})
    monkeypatch.setattr(threading, "excepthook", lambda args: None)  # the forced renewer death
    server._ASK_RENEWER_WAKE.clear()                     # no wake left over from another test
    _GatedClient.gates = {}
    yield types.SimpleNamespace(a=a, b=b, clock=clock)
    for gate in _GatedClient.gates.values():
        gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and any(
            t.is_alive() and getattr(t, "_target", None) is server._run_ask_job
            for t in threading.enumerate()):
        time.sleep(0.02)
    for _ in range(3):                                   # let the renewer see no worker and end
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


def _finish(question: str, job_id: str) -> None:
    _GatedClient.gates[question].set()
    t = _worker(job_id)
    if t is not None:
        t.join(5)


def _abandon(env, job_id: str) -> float:
    """Expire ``job_id`` at the ceiling: it is detached, its lease starts now."""
    env.clock["now"] = env.a.get(job_id)["started_at"] + server._ASK_RUN_CEILING_SECONDS + 1
    asyncio.run(server.ask_result(job_id, wait=0.0))
    assert job_id in server._ask_detached(env.b.get(server._ASK_ACTIVE_KEY), env.clock["now"])
    return env.clock["now"]


def _lease(env, job_id: str):
    return ((env.b.get(server._ASK_ACTIVE_KEY) or {}).get("detached") or {}).get(job_id)


def _step_past_the_grace(env, job_id: str, since: float) -> list:
    """Advance the clock in steps shorter than the grace, waking the renewer
    each time, until past ``since + grace``; the leases seen at each step."""
    seen = []
    step = server._ASK_LEASE_GRACE_SECONDS / 3
    while env.clock["now"] <= since + server._ASK_LEASE_GRACE_SECONDS + step:
        env.clock["now"] += step
        server._ASK_RENEWER_WAKE.set()
        deadline = time.time() + 2
        while time.time() < deadline and _lease(env, job_id) != env.clock["now"]:
            time.sleep(0.01)
        seen.append(_lease(env, job_id) == env.clock["now"])
    return seen


def _still_counted_elsewhere(env, job_id: str) -> bool:
    return job_id in server._ask_detached(env.b.get(server._ASK_ACTIVE_KEY), env.clock["now"])


def test_a_renewer_waking_as_a_new_worker_starts_never_leaves_it_unrenewed(env, monkeypatch):
    # The boundary state: a renewer is alive and its only worker just ended.
    q0 = _submit("q0")
    _finish("q0", q0)
    renewer = server._ASK_RENEWER["thread"]
    assert renewer is not None and renewer.is_alive()

    real_start = threading.Thread.start

    def _start_with_the_renewer_waking(self):
        if getattr(self, "_target", None) is server._run_ask_job:
            # The renewer wakes right at this point; give it every chance to
            # scan (where it cannot, because the registry lock is held, the
            # join just times out).
            server._ASK_RENEWER_WAKE.set()
            renewer.join(1.0)
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", _start_with_the_renewer_waking)
    q1 = _submit("q1")
    monkeypatch.setattr(threading.Thread, "start", real_start)

    since = _abandon(env, q1)
    renewed = _step_past_the_grace(env, q1, since)
    assert all(renewed), renewed
    assert _still_counted_elsewhere(env, q1), (env.clock, env.b.get(server._ASK_ACTIVE_KEY))
    alive = server._ASK_RENEWER["thread"]
    assert alive is not None and alive.is_alive()


def test_a_renewer_that_dies_while_a_detached_worker_lives_is_replaced(env, monkeypatch):
    q1 = _submit("q1")
    since = _abandon(env, q1)
    real_renew = server._renew_detached_leases
    calls = {"n": 0}

    def _renew_then_die_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Die()
        return real_renew(*args, **kwargs)
    monkeypatch.setattr(server, "_renew_detached_leases", _renew_then_die_once)
    first = server._ASK_RENEWER["thread"]
    renewed = _step_past_the_grace(env, q1, since)
    assert calls["n"] >= 2, "nothing renewed after the renewer died"
    assert not first.is_alive()
    assert _still_counted_elsewhere(env, q1), (env.clock, env.b.get(server._ASK_ACTIVE_KEY))
    assert renewed[-1], renewed
    replacement = server._ASK_RENEWER["thread"]
    assert replacement is not first and replacement.is_alive()


def test_a_worker_that_cannot_start_records_nothing_and_frees_the_slot(env, monkeypatch):
    real_start = threading.Thread.start

    def _fail_for_workers(self):
        if getattr(self, "_target", None) is server._run_ask_job:
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", _fail_for_workers)
    _GatedClient.gates["q1"] = threading.Event()
    with pytest.raises(RuntimeError):
        server.ask_async(AskRequest(question="q1"))
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert server._ASK_LOCAL_WORKERS == {}
    slot = env.a.get(server._ASK_ACTIVE_KEY)
    assert slot["status"] == "idle", slot
    job = next(v for k, v in env.a._mem.items() if k != server._ASK_ACTIVE_KEY)
    assert job["status"] == "error", job
    assert _submit("q2")                                  # the slot is free again


def test_guard_the_renewer_still_ends_when_no_worker_is_left(env):
    q1 = _submit("q1")
    _finish("q1", q1)
    renewer = server._ASK_RENEWER["thread"]
    server._ASK_RENEWER_WAKE.set()
    renewer.join(2)
    assert not renewer.is_alive()
    assert server._ASK_RENEWER["thread"] is None
