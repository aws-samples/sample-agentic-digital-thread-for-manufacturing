# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Instance-independent async-job store (B1 root-cause fix).

B1 reproduction (single process) proved the Athena schema-write code is correct;
the ACTUAL culprit for "No schema found" is that the onboarding map/provision
async job state was a per-PROCESS in-memory dict. On a multi-replica deploy a
/status poll routed to a different replica than the one that started the job
returns "Unknown job_id", so the client never receives the map result and
provisions with EMPTY mappings -> System node with no schema.

These tests pin the fix: a ``JobStore`` is a dict-like KV that, when a shared
DynamoDB backend is available, makes a job written by ONE instance readable by
ANOTHER (cross-replica visibility). With NO backend it degrades to an in-memory
dict (local/offline) so the onboarding flow is never broken.

Fully offline: a fake in-memory DynamoDB Table double stands in for the shared
store; no AWS, no network.
"""
from __future__ import annotations

import logging

import pytest

from agents.job_store import JobStore


class _FakeTable:
    """Minimal shared DynamoDB Table double (put_item/get_item/delete_item)."""

    def __init__(self, store: dict):
        self._store = store  # shared dict simulates the durable, cross-instance table

    def put_item(self, Item):  # noqa: N803 (boto3 kwarg)
        self._store[Item["system_id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)


def _shared_store():
    return {}


# --------------------------------------------------------------------------- #
# Cross-instance visibility (the fix)
# --------------------------------------------------------------------------- #

def test_job_written_by_one_instance_is_readable_by_another():
    shared = _shared_store()
    a = JobStore("map", table=_FakeTable(shared))
    b = JobStore("map", table=_FakeTable(shared))  # "different replica"

    a["job-1"] = {"status": "done", "result": {"mappings": [{"field": "t.c"}]}}

    # b never saw the write locally, but reads it from the shared backend.
    assert b.get("job-1") == {"status": "done", "result": {"mappings": [{"field": "t.c"}]}}
    assert b["job-1"]["status"] == "done"
    assert "job-1" in b


def test_delete_on_one_instance_visible_to_another():
    shared = _shared_store()
    a = JobStore("provision", table=_FakeTable(shared))
    b = JobStore("provision", table=_FakeTable(shared))
    a["j"] = {"status": "running"}
    assert b.get("j") == {"status": "running"}
    del a["j"]
    assert b.get("j") is None
    assert "j" not in b


def test_pop_round_trips_through_backend():
    shared = _shared_store()
    a = JobStore("map", table=_FakeTable(shared))
    b = JobStore("map", table=_FakeTable(shared))
    a["j"] = {"status": "done", "result": {"x": 1}}
    popped = b.pop("j")
    assert popped == {"status": "done", "result": {"x": 1}}
    assert b.get("j") is None


def test_kind_namespaces_isolate_jobs():
    shared = _shared_store()
    m = JobStore("map", table=_FakeTable(shared))
    p = JobStore("provision", table=_FakeTable(shared))
    r = JobStore("resolve", table=_FakeTable(shared))
    m["same-id"] = {"status": "map"}
    p["same-id"] = {"status": "prov"}
    r["same-id"] = {"status": "resolve"}
    assert m.get("same-id") == {"status": "map"}
    assert p.get("same-id") == {"status": "prov"}
    assert r.get("same-id") == {"status": "resolve"}


def test_resolve_job_written_by_one_instance_is_readable_by_another():
    """Same root-cause class as map/provision: the resolve async job state must be
    cross-replica visible so a /resolve/status poll routed to a different replica
    than the one that started the job still sees it."""
    shared = _shared_store()
    a = JobStore("resolve", table=_FakeTable(shared))
    b = JobStore("resolve", table=_FakeTable(shared))  # "different replica"

    a["r-1"] = {"status": "done", "result": {"rules": [{"id": "x"}]}}

    assert b.get("r-1") == {"status": "done", "result": {"rules": [{"id": "x"}]}}
    assert "r-1" in b


# --------------------------------------------------------------------------- #
# In-memory fallback (no backend) behaves like a dict
# --------------------------------------------------------------------------- #

def test_in_memory_fallback_behaves_like_dict():
    s = JobStore("map", table=None)  # no backend
    s["j"] = {"status": "running"}
    assert s.get("j") == {"status": "running"}
    assert s["j"] == {"status": "running"}
    assert "j" in s
    assert s.pop("j") == {"status": "running"}
    assert s.get("j") is None
    assert s.get("missing", "dflt") == "dflt"


def test_missing_key_getitem_raises_keyerror():
    s = JobStore("map", table=None)
    with pytest.raises(KeyError):
        _ = s["nope"]


def test_backend_failure_is_fail_soft_to_memory():
    class _BoomTable:
        def put_item(self, Item):  # noqa: N803
            raise RuntimeError("ddb down")

        def get_item(self, Key):  # noqa: N803
            raise RuntimeError("ddb down")

        def delete_item(self, Key):  # noqa: N803
            raise RuntimeError("ddb down")

    s = JobStore("map", table=_BoomTable())
    # Never raises to the caller; the job is still tracked in-memory locally.
    s["j"] = {"status": "running"}
    assert s.get("j") == {"status": "running"}
    s.pop("j", None)


def test_backend_failure_logs_stable_fallback_warning(caplog):
    """A backend write/read error that degrades to the in-memory path must emit a
    stable-key WARNING ('job_store_fallback') so an empty-mappings provision
    (a map-job result lost across replicas) is diagnosable in CloudWatch. The
    happy path (in-memory tracking) is unchanged — the job is still readable."""

    class _BoomTable:
        def put_item(self, Item):  # noqa: N803
            raise RuntimeError("ddb down")

        def get_item(self, Key):  # noqa: N803
            raise RuntimeError("ddb down")

        def delete_item(self, Key):  # noqa: N803
            raise RuntimeError("ddb down")

    s = JobStore("map", table=_BoomTable())
    with caplog.at_level(logging.WARNING, logger="agents.job_store"):
        s["j"] = {"status": "running"}  # first backend error triggers the fallback

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the fail-soft fallback must be logged at >= WARNING"
    assert any("job_store_fallback" in r.getMessage() for r in warnings), (
        "the fallback warning must carry the stable 'job_store_fallback' key"
    )
    # Happy fallback preserved: the job is still tracked in-memory.
    assert s.get("j") == {"status": "running"}


# --------------------------------------------------------------------------- #
# memory_cap: a bound on the in-memory copies a store keeps (opt-in)
# --------------------------------------------------------------------------- #
#
# The async-ask result route no longer frees a finished record as it delivers
# it (a reply that reached a caller who had stopped listening was lost,
# 2026-10-16). A memory-only store has no table TTL, so the ask store bounds
# its memory copies instead: at most ``memory_cap`` copies that
# ``memory_evictable`` accepts, the oldest written dropped first, and none of
# them past the store's TTL (the age bound is tested further down).

def _finished(state):
    return state.get("status") == "done"


def test_a_store_without_a_memory_cap_keeps_a_plain_dict():
    assert type(JobStore("map", table=None)._mem) is dict


def test_memory_cap_drops_the_oldest_written_evictable_copies_only():
    store = JobStore("k", table=None, memory_cap=3, memory_evictable=_finished)
    store["live"] = {"status": "running"}
    for n in range(5):
        store[f"d{n}"] = {"status": "done", "n": n}
    assert set(store._mem) == {"live", "d2", "d3", "d4"}
    store["d2"] = {"status": "done", "n": 2}       # a re-write makes it the newest
    store["d5"] = {"status": "done", "n": 5}
    assert set(store._mem) == {"live", "d2", "d4", "d5"}
    assert store.get("d3") is None and store.get("live") == {"status": "running"}


def test_memory_cap_never_drops_a_copy_the_predicate_refuses():
    store = JobStore("k", table=None, memory_cap=1, memory_evictable=_finished)
    for n in range(5):
        store[f"r{n}"] = {"status": "running"}
    store["d"] = {"status": "done"}
    assert set(store._mem) == {"r0", "r1", "r2", "r3", "r4", "d"}


def test_memory_cap_counts_a_copy_from_the_moment_it_becomes_evictable():
    store = JobStore("k", table=None, memory_cap=1, memory_evictable=_finished)
    store["a"] = {"status": "running"}
    store["b"] = {"status": "done"}
    assert store.finish_if_running("a", {"status": "done"})   # a is now the newest
    assert set(store._mem) == {"a"}
    store.pop("a")
    store["c"] = {"status": "done"}
    assert set(store._mem) == {"c"}


def test_memory_cap_drops_the_memory_copy_only_never_the_shared_record():
    shared = _shared_store()
    store = JobStore("k", table=_FakeTable(shared), memory_cap=1, memory_evictable=_finished)
    store["a"] = {"status": "done", "answer": "1"}
    store["b"] = {"status": "done", "answer": "2"}
    assert set(store._mem) == {"b"}
    assert len(shared) == 2
    assert store.get("a") == {"status": "done", "answer": "1"}   # read back from the table


def test_memory_cap_rejects_a_cap_without_a_predicate():
    with pytest.raises(ValueError):
        JobStore("k", table=None, memory_cap=3)


# --------------------------------------------------------------------------- #
# Every mutator and every read of a capped store goes through the bound
# --------------------------------------------------------------------------- #
#
# The capped copies were a dict subclass that overrode only item assignment,
# ``pop`` and ``del``: dict's own ``setdefault``, ``update``, ``|=``, ``clear``
# and ``popitem`` bypassed the bound and its lock, and iteration read the live
# dict while another thread wrote it.

def _capped(cap, evictable=_finished):
    return JobStore("k", table=None, memory_cap=cap, memory_evictable=evictable)._mem


_DONE = {"status": "done"}


def test_setdefault_goes_through_the_bound():
    mem = _capped(1)
    mem["a"] = _DONE
    assert mem.setdefault("b", _DONE) == _DONE
    assert set(mem) == {"b"}
    assert mem.setdefault("b", {"status": "done", "other": 1}) == _DONE   # present: unchanged


def test_update_goes_through_the_bound_for_a_mapping_pairs_and_keywords():
    mem = _capped(2)
    mem.update({"a": _DONE}, b=_DONE)
    mem.update([("c", _DONE)])
    assert set(mem) == {"b", "c"}


def test_in_place_merge_goes_through_the_bound_and_keeps_the_object():
    mem = _capped(1)
    same = mem
    mem |= {"a": _DONE, "b": _DONE}
    assert mem is same
    assert set(mem) == {"b"}


def test_clear_empties_the_copies_and_the_bound_starts_again():
    mem = _capped(2)
    mem["a"], mem["b"] = _DONE, _DONE
    mem.clear()
    assert len(mem) == 0 and list(mem) == []
    for key in ("c", "d", "e"):
        mem[key] = _DONE
    assert set(mem) == {"d", "e"}


def test_popitem_removes_one_copy_from_the_bound_too():
    mem = _capped(2)
    mem["a"], mem["b"] = _DONE, _DONE
    key, value = mem.popitem()
    assert value == _DONE
    mem["c"] = _DONE
    assert set(mem) == ({"a", "b"} - {key}) | {"c"}
    with pytest.raises(KeyError):
        _capped(2).popitem()


def test_pop_and_del_remove_a_copy_and_report_a_missing_one():
    mem = _capped(2)
    mem["a"], mem["b"] = _DONE, _DONE
    assert mem.pop("a") == _DONE
    assert mem.pop("a", "gone") == "gone"
    with pytest.raises(KeyError):
        mem.pop("a")
    del mem["b"]
    with pytest.raises(KeyError):
        del mem["b"]
    mem["c"], mem["d"] = _DONE, _DONE
    assert set(mem) == {"c", "d"}


def test_writing_while_iterating_reads_a_snapshot():
    mem = _capped(10)
    for key in ("a", "b", "c"):
        mem[key] = _DONE
    for key in mem:
        mem[f"{key}-next"] = _DONE
    assert set(mem) == {"a", "b", "c", "a-next", "b-next", "c-next"}


def test_items_and_copy_are_snapshots_of_one_moment():
    mem = _capped(10)
    mem["a"] = _DONE
    items, copied = mem.items(), mem.copy()
    mem["z"] = _DONE
    assert list(items) == [("a", _DONE)]
    assert type(copied) is dict and copied == {"a": _DONE}


def test_a_snapshot_taken_while_a_write_is_in_progress_never_exceeds_the_cap():
    """The writer is held inside its write (the predicate blocks); a reader on
    another thread must see the copies as they were, never cap + 1."""
    import threading
    paused, resume = threading.Event(), threading.Event()

    def evictable(state):
        if state.get("pause"):
            paused.set()
            resume.wait(5)
        return state.get("status") == "done"

    mem = _capped(1, evictable)
    mem["a"] = _DONE
    writer = threading.Thread(target=mem.__setitem__,
                              args=("b", {"status": "done", "pause": True}))
    writer.start()
    try:
        assert paused.wait(5)
        seen = [dict(mem.items()), mem.copy(), dict.fromkeys(mem)]
    finally:
        resume.set()
        writer.join(5)
    assert all(len(snapshot) <= 1 for snapshot in seen), seen
    assert set(mem) == {"b"}


def test_concurrent_reads_and_writes_never_raise_or_exceed_the_cap():
    import threading
    mem = _capped(3)
    stop, errors = threading.Event(), []

    def write():
        n = 0
        while not stop.is_set():
            mem[f"k{n % 50}"] = _DONE
            mem.setdefault(f"s{n % 7}", _DONE)
            mem.pop(f"k{(n + 25) % 50}", None)
            n += 1

    writer = threading.Thread(target=write)
    writer.start()
    try:
        for _ in range(3000):
            for snapshot in (list(mem.items()), mem.copy(), [k for k in mem]):
                assert len(snapshot) <= 3, snapshot
            assert len(mem) <= 3
    except Exception as exc:  # noqa: BLE001 - recorded, then the writer is stopped
        errors.append(exc)
    finally:
        stop.set()
        writer.join(5)
    assert not errors, errors


# --------------------------------------------------------------------------- #
# A table item past its TTL is absent to every read (DynamoDB deletes lazily)
# --------------------------------------------------------------------------- #
#
# DynamoDB removes an item some time after its ``ttl``, possibly hours later,
# and serves it to every read until then. The store's reads now treat an item
# whose ``ttl`` is at or before now as gone. A lease is read by its own
# ``expires_at`` (the attribute its conditional write tests), and ``mutate``
# reads the item its revision-conditional write will be checked against.

import json as _json  # noqa: E402 - local to this section
import time as _time  # noqa: E402

from tests.unit._shared_table import SharedTable  # noqa: E402


def _expire(table, store, job_id, when=None):
    item = table.items[store._key(job_id)]
    item["ttl"] = int(_time.time()) - 3600 if when is None else when


def test_an_item_past_its_ttl_reads_as_absent_and_is_not_cached():
    table = SharedTable()
    store = JobStore("k", table=table)
    store["j"] = {"status": "done", "answer": "old"}
    _expire(table, store, "j")
    assert store.get("j") is None
    assert "j" not in store._mem          # the stale copy is dropped, not cached
    assert "j" not in store
    assert store.get_strict("j") is None
    assert table.items                    # nothing was deleted: the read decides


def test_an_item_inside_its_ttl_still_reads():
    table = SharedTable()
    store = JobStore("k", table=table)
    store["j"] = {"status": "done"}
    assert JobStore("k", table=table).get("j") == {"status": "done"}
    assert JobStore("k", table=table).get_strict("j") == {"status": "done"}


def test_the_ttl_boundary_is_at_or_before_now():
    table = SharedTable()
    store = JobStore("k", table=table, clock=lambda: 1000.0)
    store["j"] = {"status": "done"}
    assert table.items[store._key("j")]["ttl"] == 1000 + 3600   # stamped by the store's clock
    _expire(table, store, "j", when=1001)
    assert store.get("j") == {"status": "done"}
    _expire(table, store, "j", when=1000)
    assert store.get("j") is None


def test_an_unreadable_backend_still_falls_back_to_memory():
    table = SharedTable()
    store = JobStore("k", table=table)
    store["j"] = {"status": "done"}
    table.fail_reads = True
    assert store.get("j") == {"status": "done"}   # None (no answer), not absent


class _BatchTable(SharedTable):
    """SharedTable plus the low-level ``batch_get_item`` ``get_many`` uses
    (typed attribute values, as the boto3 client returns them)."""
    name = "sessions"

    @property
    def meta(self):
        import types
        return types.SimpleNamespace(client=self)

    def batch_get_item(self, RequestItems):  # noqa: N803 - boto3 name
        keys = RequestItems[self.name]["Keys"]
        out = []
        for key in keys:
            item = self.items.get(key["system_id"])
            if item:
                out.append({k: ({"N": str(v)} if isinstance(v, (int, float)) else {"S": v})
                            for k, v in item.items()})
        return {"Responses": {self.name: out}}


def test_get_many_leaves_out_and_uncaches_an_item_past_its_ttl():
    table = _BatchTable()
    store = JobStore("k", table=table)
    store["old"] = {"status": "done", "n": 1}
    store["new"] = {"status": "done", "n": 2}
    _expire(table, store, "old")
    assert store.get_many(["old", "new"]) == {"new": {"status": "done", "n": 2}}
    assert set(store._mem) == {"new"}


def test_a_lease_counts_by_its_own_expiry_not_its_items_ttl():
    """lease_acquire's conditional write tests ``lease_expires_at``; a read
    that also applied the item TTL could call a lease absent that the write
    still refuses, and report it held by nobody."""
    table = SharedTable()
    store = JobStore("reset", table=table)
    now = int(_time.time())
    mine = {"owner": "o1", "scope": "s", "expires_at": now + 600}
    assert store.lease_acquire("L", mine, now=now) is None
    _expire(table, store, "L")
    assert store.lease_read("L", now=now) == mine
    theirs = {"owner": "o2", "scope": "s", "expires_at": now + 600}
    assert store.lease_acquire("L", theirs, now=now) == mine


class _RevisionTable(SharedTable):
    """SharedTable that also evaluates ``mutate``'s revision conditions."""

    def put_item(self, Item, ConditionExpression=None,  # noqa: N803
                 ExpressionAttributeValues=None, ExpressionAttributeNames=None, **_kw):
        from tests.unit._shared_table import _conditional_failure
        current = self.items.get(Item["system_id"])
        cond = ConditionExpression or ""
        if cond == "#rev = :rev":
            ok = current is not None and current.get("job_rev") == ExpressionAttributeValues[":rev"]
        elif cond == "attribute_not_exists(#k)":
            ok = current is None
        elif cond == "attribute_exists(#k) AND attribute_not_exists(#rev)":
            ok = current is not None and "job_rev" not in current
        else:
            ok = not cond
        if not ok:
            raise _conditional_failure("PutItem")
        self.items[Item["system_id"]] = dict(Item)


def test_mutate_reads_an_expired_but_present_item_so_its_write_still_lands():
    table = _RevisionTable()
    store = JobStore("ask", table=table)
    assert store.mutate("active", lambda cur: {"status": "idle", "n": 1})
    _expire(table, store, "active")
    seen = []

    def bump(cur):
        seen.append(cur)
        return {**(cur or {}), "n": (cur or {}).get("n", 0) + 1}

    assert store.mutate("active", bump)
    assert seen[-1] == {"status": "idle", "n": 1}
    assert _json.loads(table.items[store._key("active")]["job_state"])["n"] == 2


# --------------------------------------------------------------------------- #
# A capped store's evictable copies also stop answering at the store's TTL
# --------------------------------------------------------------------------- #

def test_a_capped_memory_only_copy_stops_answering_at_the_ttl():
    clock = {"now": 1000.0}
    store = JobStore("k", table=None, memory_cap=10, memory_evictable=_finished,
                     ttl_seconds=100, clock=lambda: clock["now"])
    store["done"] = {"status": "done"}
    store["live"] = {"status": "running"}
    clock["now"] = 1099.0
    assert store.get("done") == {"status": "done"}
    clock["now"] = 1100.0
    assert store.get("done") is None and "done" not in store
    assert set(store._mem) == {"live"}           # a copy the predicate refuses never ages out
    clock["now"] = 99999.0
    assert store.get("live") == {"status": "running"}


def test_a_copy_cached_from_the_table_keeps_the_items_own_ttl():
    clock = {"now": 1000.0}
    table = SharedTable()
    writer = JobStore("k", table=table, clock=lambda: clock["now"])
    writer["j"] = {"status": "done"}                # item ttl 1000 + 3600
    store = JobStore("k", table=table, memory_cap=10, memory_evictable=_finished,
                     clock=lambda: clock["now"])
    clock["now"] = 3000.0
    assert store.get("j") == {"status": "done"}     # read from the table, cached
    table.fail_reads = True                         # from now on: memory only
    clock["now"] = 4599.0
    assert store.get("j") == {"status": "done"}
    clock["now"] = 4600.0                           # the item's ttl, not 3000 + 3600
    assert store.get("j") is None


# --------------------------------------------------------------------------- #
# One expiry per write: the memory copy a write keeps expires
# at exactly the ``ttl`` its table item got, even when the table write takes
# long enough for the clock to cross a second; and a table item with no ``ttl``
# (which never expires on read) does not expire from memory either.
# --------------------------------------------------------------------------- #

class _SlowTable(SharedTable):
    """Accepts any conditional write (``finish_if_running``, the slot paths)
    and moves the store's clock forward while each put is in flight."""

    def __init__(self, clock: dict):
        super().__init__()
        self._clock = clock
        self.put_seconds = 0.0

    def put_item(self, Item, ConditionExpression=None, **_kw):  # noqa: N803
        self._write("PutItem", self.fail_puts)
        self._clock["now"] += self.put_seconds
        self.items[Item["system_id"]] = dict(Item)


def _slow_store(clock):
    table = _SlowTable(clock)
    store = JobStore("k", table=table, memory_cap=10, memory_evictable=_finished,
                     clock=lambda: clock["now"])
    return table, store


def test_a_finished_copy_expires_with_its_item_when_the_write_crosses_a_second():
    clock = {"now": 990.0}
    table, store = _slow_store(clock)
    store["j"] = {"status": "running"}
    clock["now"] = 1000.9
    table.put_seconds = 0.2                         # the write lands at 1001.1
    assert store.finish_if_running("j", {"status": "done", "answer": "42"})
    assert table.items[store._key("j")]["ttl"] == 4600
    table.fail_reads = True                         # from now on: memory only
    clock["now"] = 4599.5
    assert store.get("j") == {"status": "done", "answer": "42"}
    clock["now"] = 4600.5                           # past the item's ttl
    assert store.get("j") is None


def test_a_plain_write_that_crosses_a_second_keeps_one_expiry():
    clock = {"now": 1000.9}
    table, store = _slow_store(clock)
    table.put_seconds = 0.2
    store["j"] = {"status": "done", "answer": "42"}
    assert table.items[store._key("j")]["ttl"] == 4600
    table.fail_reads = True
    clock["now"] = 4600.0
    assert store.get("j") is None


def test_an_item_with_no_ttl_does_not_expire_from_memory_either():
    clock = {"now": 1000.0}
    table = SharedTable()
    store = JobStore("k", table=table, memory_cap=10, memory_evictable=_finished,
                     clock=lambda: clock["now"])
    table.items[store._key("j")] = {"system_id": store._key("j"),
                                    "job_state": _json.dumps({"status": "done"})}
    assert store.get("j") == {"status": "done"}     # no ttl: live on the table
    table.fail_reads = True
    clock["now"] = 1000.0 + 10 * 3600
    assert store.get("j") == {"status": "done"}     # and live in memory too
