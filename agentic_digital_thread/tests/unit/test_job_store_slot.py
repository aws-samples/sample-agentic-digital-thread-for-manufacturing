# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``JobStore.claim_slot`` / ``release_slot`` — the single-slot ownership
primitive behind every "one active job" record (eval runs, feedback
exploration, try-it, drift, audit scheduler).

Before: a read-then-``__setitem__`` check-then-set with an UNCONDITIONAL
release, so a stale worker finishing late released a NEWER worker's slot and
two expensive runs could overlap. Now:

* claim is atomic — a DynamoDB ``ConditionExpression`` over the slot's
  mirrored top-level attributes (``slot_status`` / ``slot_owner`` /
  ``slot_started_at``) when a table is configured, a locked CAS in memory
  otherwise;
* a fresh ``running`` holder refuses the claim, a stale one is taken over;
* release writes ONLY if the caller still owns the slot.

The fake table honours the exact condition shapes the primitive emits and
serialises writes under one lock (DynamoDB's per-item semantics). Offline.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import agents.audit_scheduler as audit_scheduler
import agents.eval_runs as eval_runs
import agents.feedback_exploration as fx
import agents.graph_snapshot_routes as gsr
import agents.learning_routes as lr
from agents.job_store import JobStore

SLOT = "active"
STALE = 900


class _ConditionalCheckFailedException(Exception):
    """Shaped like botocore's ClientError for a refused condition."""

    def __init__(self):
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeSlotTable:
    """DynamoDB Table double honouring the slot primitive's two conditions."""

    def __init__(self, store: dict | None = None):
        self._store = store if store is not None else {}
        self._lock = threading.Lock()
        self.puts: list[dict] = []

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)

    def put_item(self, Item, **kwargs):  # noqa: N803
        with self._lock:
            self.puts.append({"Item": dict(Item), **kwargs})
            cond = kwargs.get("ConditionExpression")
            if cond is not None:
                current = self._store.get(Item["system_id"])
                values = kwargs["ExpressionAttributeValues"]
                if not self._passes(cond, current, values):
                    raise _ConditionalCheckFailedException()
            self._store[Item["system_id"]] = dict(Item)

    @staticmethod
    def _passes(cond: str, current: dict | None, values: dict) -> bool:
        if current is None:
            return "attribute_not_exists(#k)" in cond
        if cond == "attribute_exists(#k) AND #st = :running":  # finish_if_running
            return current.get("slot_status") == values[":running"]
        if "#o = :me AND #st = :running" in cond:  # touch (heartbeat)
            return (current.get("slot_owner") == values[":me"]
                    and current.get("slot_status") == values[":running"])
        if "#o = :me" in cond:  # release
            return current.get("slot_owner") == values[":me"]
        # claim
        status = current.get("slot_status")
        started = current.get("slot_started_at")
        if status is None or status != values[":running"]:
            return True
        return started is None or started < values[":stale_before"]


def _running(owner: str, started_at: float, key: str = "run_id") -> dict:
    return {"status": "running", key: owner, "started_at": started_at}


def _idle(owner: str, key: str = "run_id") -> dict:
    return {"status": "idle", key: owner, "finished_at": 1000.0}


@pytest.fixture(params=["memory", "remote"])
def store(request):
    if request.param == "memory":
        return JobStore("slot-test", table=None)
    return JobStore("slot-test", table=_FakeSlotTable())


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #

def test_free_slot_is_claimed(store):
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    assert store.get(SLOT)["run_id"] == "a"


def test_fresh_holder_refuses_a_second_claim(store):
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    assert not store.claim_slot(SLOT, _running("b", 1010.0), stale_seconds=STALE, now=1010.0)
    assert store.get(SLOT)["run_id"] == "a", "the refused claim must not write"


def test_stale_holder_is_taken_over(store):
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    later = 1000.0 + STALE + 1
    assert store.claim_slot(SLOT, _running("b", later), stale_seconds=STALE, now=later)
    assert store.get(SLOT)["run_id"] == "b"


def test_idle_record_seeded_via_setitem_is_claimable(store):
    store[SLOT] = _idle("old")
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)


def test_running_record_seeded_via_setitem_refuses(store):
    """Existing callers/tests seed the occupant through ``__setitem__`` — the
    mirror attributes make that record a real holder for the primitive."""
    store[SLOT] = _running("other", 1000.0, key="job_id")
    assert not store.claim_slot(SLOT, _running("a", 1001.0, key="job_id"),
                                stale_seconds=STALE, now=1001.0)


def test_claim_requires_a_running_owner_state(store):
    with pytest.raises(ValueError):
        store.claim_slot(SLOT, {"status": "idle"}, stale_seconds=STALE)
    with pytest.raises(ValueError):
        store.claim_slot(SLOT, {"status": "running"}, stale_seconds=STALE)


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #

def test_owner_release_writes_idle(store):
    store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    assert store.release_slot(SLOT, "a", _idle("a"))
    assert store.get(SLOT)["status"] == "idle"


def test_stale_owner_cannot_release_a_newer_claim(store):
    """THE reported defect: A claims, goes stale, B takes over, A finishes
    late — A's release must be refused and B stays the holder."""
    store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    later = 1000.0 + STALE + 1
    assert store.claim_slot(SLOT, _running("b", later), stale_seconds=STALE, now=later)
    assert not store.release_slot(SLOT, "a", _idle("a"))
    active = store.get(SLOT)
    assert active["status"] == "running" and active["run_id"] == "b"
    # ...and a third claimant is still refused while B is fresh.
    assert not store.claim_slot(SLOT, _running("c", later + 1), stale_seconds=STALE, now=later + 1)


def test_release_of_an_empty_slot_is_idempotent(store):
    assert store.release_slot(SLOT, "a", _idle("a"))
    assert store.get(SLOT)["status"] == "idle"


# --------------------------------------------------------------------------- #
# Touch (heartbeat)
# --------------------------------------------------------------------------- #

def test_owner_touch_keeps_a_long_claim_from_going_stale(store):
    """A long job that heartbeats is never taken over, however old its
    original claim; the refreshed record keeps its owner and fields."""
    store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    beat = 1000.0 + STALE - 10
    assert store.touch_slot(SLOT, "a", now=beat)
    later = 1000.0 + STALE + 1  # past the ORIGINAL claim's stale bound
    assert not store.claim_slot(SLOT, _running("b", later), stale_seconds=STALE, now=later)
    active = store.get(SLOT)
    assert active["run_id"] == "a" and active["started_at"] == beat
    # Without a further beat the claim does go stale in time.
    stale = beat + STALE + 1
    assert store.claim_slot(SLOT, _running("b", stale), stale_seconds=STALE, now=stale)


@pytest.mark.parametrize("setup", ["other_owner", "released", "empty"])
def test_touch_refuses_when_the_caller_holds_no_running_claim(store, setup):
    """No write, and False, when the slot is someone else's, idle or absent."""
    if setup == "other_owner":
        store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
        later = 1000.0 + STALE + 1
        store.claim_slot(SLOT, _running("b", later), stale_seconds=STALE, now=later)
    elif setup == "released":
        store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
        store.release_slot(SLOT, "a", _idle("a"))
    before = store.get(SLOT)
    assert not store.touch_slot(SLOT, "a", now=5000.0)
    assert store.get(SLOT) == before


def test_remote_touch_refused_after_another_replica_took_over():
    """Replica 1 still believes it owns the slot; replica 2 took it over in
    the shared table. Replica 1's heartbeat is one conditional put, refused."""
    shared: dict = {}
    table = _FakeSlotTable(shared)
    replica1 = JobStore("slot-replicas", table=table)
    replica2 = JobStore("slot-replicas", table=_FakeSlotTable(shared))
    replica1.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    later = 1000.0 + STALE + 1
    assert replica2.claim_slot(SLOT, _running("b", later), stale_seconds=STALE, now=later)
    puts_before = len(table.puts)
    assert not replica1.touch_slot(SLOT, "a", now=later + 1)
    put = table.puts[-1]
    assert len(table.puts) == puts_before + 1
    assert put["ConditionExpression"] == "#o = :me AND #st = :running"
    assert replica2.get(SLOT)["run_id"] == "b"


# --------------------------------------------------------------------------- #
# Concurrency: many racing claimants, exactly one wins
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["memory", "remote"])
def test_exactly_one_of_many_racing_claims_wins(mode):
    table = None if mode == "memory" else _FakeSlotTable()
    store = JobStore("slot-race", table=table)
    barrier = threading.Barrier(16)

    def _try(i: int) -> bool:
        barrier.wait()
        return store.claim_slot(SLOT, _running(f"w{i}", 1000.0),
                                stale_seconds=STALE, now=1000.0)

    with ThreadPoolExecutor(max_workers=16) as pool:
        wins = list(pool.map(_try, range(16)))
    assert sum(wins) == 1, wins


def test_remote_claim_is_one_conditional_put_with_no_read():
    table = _FakeSlotTable()
    store = JobStore("slot-shape", table=table)
    store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    assert len(table.puts) == 1
    put = table.puts[0]
    assert "attribute_not_exists(#k)" in put["ConditionExpression"]
    assert "#sa < :stale_before" in put["ConditionExpression"]
    assert put["Item"]["slot_owner"] == "a"
    assert put["Item"]["slot_status"] == "running"
    assert put["Item"]["slot_started_at"] == 1000


def test_remote_backend_error_degrades_to_memory_decision():
    class _Boom:
        def put_item(self, **_):
            raise RuntimeError("ProvisionedThroughputExceeded")

        def get_item(self, **_):
            return {}

    store = JobStore("slot-boom", table=_Boom())
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    assert not store.claim_slot(SLOT, _running("b", 1001.0), stale_seconds=STALE, now=1001.0)


# --------------------------------------------------------------------------- #
# finish_if_running: a job's terminal write lands only on a running record
# --------------------------------------------------------------------------- #

def test_a_running_job_record_is_finished(store):
    store["job-1"] = {"status": "running", "started_at": 1000.0}
    assert store.finish_if_running("job-1", {"status": "done", "answer": "a"})
    assert store.get("job-1") == {"status": "done", "answer": "a"}


@pytest.mark.parametrize("current", [
    None,
    {"status": "error", "error": "x", "abandoned": True},
    {"status": "done", "answer": "first"},
], ids=["missing", "tombstone", "already-done"])
def test_a_terminal_or_missing_record_is_never_overwritten(store, current):
    if current is not None:
        store["job-1"] = current
    assert not store.finish_if_running("job-1", {"status": "done", "answer": "late"})
    after = store.get("job-1")
    assert after is None if current is None else after["status"] == current["status"]
    assert (after or {}).get("answer") != "late"


def test_a_finished_error_carries_the_kind_sentinel(store):
    store["job-1"] = {"status": "running"}
    assert store.finish_if_running("job-1", {"status": "error", "error": "raw internals"})
    assert store.get("job-1")["error"] == "slot-test_failed"


# --------------------------------------------------------------------------- #
# Callers: every slot owner routes through the primitive (source tripwire)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("module", [eval_runs, fx, lr, audit_scheduler, gsr])
def test_slot_owners_never_assign_the_active_key_directly(module):
    import inspect
    source = inspect.getsource(module)
    offenders = [line.strip() for line in source.splitlines()
                 if "ACTIVE_KEY] = {" in line]
    assert offenders == [], offenders
    assert ".claim_slot(" in source and ".release_slot(" in source
