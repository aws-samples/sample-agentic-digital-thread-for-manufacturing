# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The abandoned-worker bound holds for the whole deployment, not one process.

An async ask whose worker no longer holds the app-wide slot (taken over after
10 minutes, or abandoned at the 20-minute ceiling) keeps running: the worker
cannot be interrupted. A new submit is refused while more than one such
worker is alive. That count lived in each process's memory, so with two
replicas (a rolling deployment overlaps old and new tasks) each process
allowed its own one, while the job records and the slot were already shared
through DynamoDB. The detached workers are now recorded IN the shared slot
record, which every writer changes through ``JobStore.mutate`` (a
revision-conditional read-modify-write): a claim, a takeover, an expiry and
a worker's finish all see the same set. Offline: two JobStores (two
"replicas") over one fake DynamoDB table that evaluates the conditions; the
chat client is a gated stand-in.
"""

import asyncio
import re
import threading
import time
import types

import pytest

import agents.server as server
from agents.events import DoneEvent, MessageEvent
from agents.job_store import JobStore
from agents.models import AskRequest

BUSY = "two earlier queries were abandoned and are still finishing"


class ConditionalCheckFailedException(Exception):
    pass


_ATOM = re.compile(r"^(attribute_exists|attribute_not_exists)\((#\w+)\)$|^(#\w+) (=|<>|<) (:\w+)$")


def _passes(cond: str, item, names: dict, values: dict) -> bool:
    """A small DynamoDB ConditionExpression evaluator: OR of ANDs of
    attribute_exists / attribute_not_exists / = / <> / < atoms."""
    def atom(text: str) -> bool:
        m = _ATOM.match(text.strip())
        assert m, f"unsupported condition atom {text!r}"
        if m.group(1):
            present = item is not None and names[m.group(2)] in item
            return present if m.group(1) == "attribute_exists" else not present
        attr, op, val = names[m.group(3)], m.group(4), values[m.group(5)]
        if item is None or attr not in item:
            return False
        have = item[attr]
        return {"=": have == val, "<>": have != val, "<": have < val}[op]
    return any(all(atom(a) for a in clause.split(" AND ")) for clause in cond.split(" OR "))


class _SharedTable:
    """One DynamoDB table both replicas write (get / put / delete)."""

    def __init__(self):
        self.items: dict = {}
        self.lock = threading.Lock()
        self.conflicts = 0

    def get_item(self, Key):  # noqa: N803 - boto3 name
        with self.lock:
            item = self.items.get(Key["system_id"])
            return {"Item": dict(item)} if item is not None else {}

    def put_item(self, Item, **kw):  # noqa: N803 - boto3 name
        with self.lock:
            cond = kw.get("ConditionExpression")
            if cond and not _passes(cond, self.items.get(Item["system_id"]),
                                    kw.get("ExpressionAttributeNames", {}),
                                    kw.get("ExpressionAttributeValues", {})):
                self.conflicts += 1
                raise ConditionalCheckFailedException()
            self.items[Item["system_id"]] = dict(Item)

    def delete_item(self, Key):  # noqa: N803 - boto3 name
        with self.lock:
            self.items.pop(Key["system_id"], None)


class _GatedClient:
    gates: dict = {}

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        key = next(k for k in type(self).gates if prompt.endswith(k))
        type(self).gates[key].wait(10)
        yield MessageEvent(content=f"late answer to {key}")
        yield DoneEvent()


@pytest.fixture
def replicas(monkeypatch):
    import agents.chat_client as chat_client_mod
    table = _SharedTable()
    stores = {"a": JobStore("ask", table=table), "b": JobStore("ask", table=table)}
    registries = {"a": {}, "b": {}}      # a per-process registry, where one exists
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _GatedClient)
    clock = {"now": time.time()}
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    _GatedClient.gates = {}

    def use(name: str):
        monkeypatch.setattr(server, "_ASK_JOBS", stores[name])
        monkeypatch.setattr(server, "_ASK_LOCAL_WORKERS", registries[name])
    yield types.SimpleNamespace(table=table, stores=stores, use=use, clock=clock)
    for gate in _GatedClient.gates.values():
        gate.set()
    _join_all()


def _join_all():
    deadline = time.time() + 10
    while time.time() < deadline and any(
            t.is_alive() and getattr(t, "_target", None) is server._run_ask_job
            for t in threading.enumerate()):
        time.sleep(0.02)


def _wait_done(job_id: str):
    deadline = time.time() + 10
    while time.time() < deadline and any(
            t.is_alive() and getattr(t, "_target", None) is server._run_ask_job
            and job_id in (getattr(t, "_args", ()) or ())
            for t in threading.enumerate()):
        time.sleep(0.02)


def _submit(question: str):
    _GatedClient.gates[question] = threading.Event()
    return server.ask_async(AskRequest(question=question))


def _expire(env, job_id: str) -> dict:
    jobs = server._ASK_JOBS
    env.clock["now"] = jobs.get(job_id)["started_at"] + server._ASK_RUN_CEILING_SECONDS + 1
    reply = asyncio.run(server.ask_result(job_id, wait=0.0))
    env.clock["now"] = time.time()
    return reply


def _refused(res) -> bool:
    return getattr(res, "status_code", None) == 429 and BUSY in res.body.decode()


def test_the_bound_holds_across_two_replicas(replicas):
    replicas.use("a")
    q1 = _submit("q1")["job_id"]
    assert _expire(replicas, q1)["error_detail"].endswith("abandoned; ask again")
    replicas.use("b")
    q2 = _submit("q2")
    assert q2.get("status") == "running", q2        # one abandoned worker: allowed
    _expire(replicas, q2["job_id"])
    # Two abandoned workers are alive in the DEPLOYMENT (one per replica):
    # each replica refuses, though each process abandoned only one.
    replicas.use("a")
    assert _refused(_submit("q3a"))
    replicas.use("b")
    assert _refused(_submit("q3b"))
    # One of them finishes (its own replica records it): a new ask is accepted.
    _GatedClient.gates["q1"].set()
    _wait_done(q1)
    replicas.use("b")
    assert _submit("q4").get("status") == "running"


def test_a_takeover_counts_as_a_detached_worker_on_every_replica(replicas):
    replicas.use("a")
    q1 = _submit("q1")["job_id"]
    _expire(replicas, q1)                            # one abandoned
    replicas.use("b")
    q2 = _submit("q2")["job_id"]
    # q2's claim goes stale (over the takeover age, under the ceiling): taking
    # it over would leave two detached workers alive, so replica A refuses.
    stale = time.time() - server._ASK_STALE_SECONDS - 1
    assert replicas.stores["b"].mutate(server._ASK_ACTIVE_KEY,
                                       lambda cur: {**cur, "started_at": stale})
    replicas.use("a")
    assert _refused(_submit("q3"))
    slot = replicas.stores["a"].get(server._ASK_ACTIVE_KEY)
    assert slot["job_id"] == q2 and set(slot["detached"]) == {q1}, slot


def test_concurrent_claims_on_two_replicas_admit_exactly_one(replicas):
    results: list = []
    barrier = threading.Barrier(2)

    def _claim(name):
        store = replicas.stores[name]
        barrier.wait()
        results.append(server._claim_ask_slot(store, f"job-{name}", time.time()))
    threads = [threading.Thread(target=_claim, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(results) == ["", "busy"], results


def test_a_worker_finishing_on_its_replica_frees_its_place_for_the_other(replicas):
    replicas.use("a")
    q1 = _submit("q1")["job_id"]
    _expire(replicas, q1)
    slot = replicas.stores["b"].get(server._ASK_ACTIVE_KEY)
    assert set(slot["detached"]) == {q1}, slot
    _GatedClient.gates["q1"].set()
    _wait_done(q1)
    slot = replicas.stores["b"].get(server._ASK_ACTIVE_KEY)
    assert slot["detached"] == {} and slot["status"] == "idle", slot


def test_a_dead_processes_unrenewed_leases_expire(replicas):
    """Leases nobody renewed for the grace (their process died) stop counting;
    a live worker's lease is renewed and is never dropped for age
    (test_ask_async_detached_leases.py)."""
    store = replicas.stores["a"]
    old = time.time() - server._ASK_LEASE_GRACE_SECONDS - 5
    assert store.mutate(server._ASK_ACTIVE_KEY, lambda cur: {
        "status": "idle", "job_id": "gone", "detached": {"gone-1": old, "gone-2": old}})
    replicas.use("b")
    assert _submit("fresh").get("status") == "running"


# ── JobStore.mutate itself ─────────────────────────────────────────────────

def test_mutate_never_loses_an_update_between_two_stores():
    table = _SharedTable()
    stores = [JobStore("count", table=table) for _ in range(2)]

    def _bump(store):
        for _ in range(25):
            assert store.mutate("n", lambda cur: {"n": (cur or {}).get("n", 0) + 1},
                                attempts=200)
    threads = [threading.Thread(target=_bump, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert stores[0].get("n") == {"n": 50}


def test_mutate_returns_false_and_writes_nothing_when_fn_declines():
    table = _SharedTable()
    store = JobStore("count", table=table)
    assert not store.mutate("n", lambda cur: None)
    assert table.items == {}


def test_guard_mutate_in_memory_for_local_dev():
    store = JobStore("count", table=None)
    assert store.mutate("n", lambda cur: {"n": 1})
    assert store.mutate("n", lambda cur: {"n": cur["n"] + 1})
    assert store.get("n") == {"n": 2}
