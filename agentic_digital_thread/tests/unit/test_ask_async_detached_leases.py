# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A detached ask worker is counted while it lives, however long that is.

The shared slot record counts the detached workers (taken over, or abandoned
at the ceiling) that are still alive, and a submit is refused while more
than one is. An entry was dropped after a fixed hour, on the assumption that
an hour-old entry meant its process had died. Age does not prove that: the
runtime's idle heartbeats keep a wedged stream open, so a worker can live
far past an hour, and once its entry aged out a new wedged ask was admitted,
one more live worker every hour. The entries are now LEASES: each process
keeps a registry of its own live workers and one renewer thread refreshes
their shared timestamps through ``JobStore.mutate``; a worker that ends
removes its entry; only a lease not renewed within the dead-process grace
(3 x the renew interval) expires, and renewal never brings back an entry
that was removed or has expired. Offline: two JobStores (two "replicas")
over one fake DynamoDB table that evaluates the conditions; fake clocks.
"""

import threading
import time
import types

import pytest

import agents.server as server
from agents.job_store import JobStore
from agents.models import AskRequest
from tests.unit.test_ask_async_abandoned_bound_shared import (  # noqa: F401 - helpers
    _GatedClient, _SharedTable, _wait_done)

T0 = 1_800_000_000.0
HOUR = 3600.0


@pytest.fixture
def two():
    table = _SharedTable()
    return types.SimpleNamespace(table=table, a=JobStore("ask", table=table),
                                 b=JobStore("ask", table=table))


def _detached(store) -> dict:
    return dict((store.get(server._ASK_ACTIVE_KEY) or {}).get("detached") or {})


def _abandon(store, job_id: str, at: float) -> None:
    """``job_id`` claims the slot at ``at`` and is abandoned (detached) right away."""
    assert server._claim_ask_slot(store, job_id, at) == ""
    server._ask_slot_detach(store, job_id, at)


def test_the_lease_constants_are_a_renewal_and_a_three_interval_grace():
    assert server._ASK_LEASE_GRACE_SECONDS == 3 * server._ASK_LEASE_RENEW_SECONDS
    assert server._ASK_LEASE_GRACE_SECONDS < HOUR


def test_a_live_worker_past_an_hour_stays_counted_across_two_stores(two):
    _abandon(two.a, "q1", T0)
    now = T0
    while now < T0 + 2 * HOUR:                     # replica A's renewer, every interval
        now += server._ASK_LEASE_RENEW_SECONDS
        server._renew_detached_leases(two.a, {"q1"}, now)
    # Two hours on, replica B still counts q1: one more abandoned ask is the
    # limit, a third live wedged worker is refused.
    assert "q1" in _detached(two.b)
    _abandon(two.b, "q2", now)
    assert set(_detached(two.b)) == {"q1", "q2"}
    assert server._claim_ask_slot(two.b, "q3", now + 1) == "abandoned"


def test_a_dead_replicas_unrenewed_lease_expires_after_the_grace(two):
    _abandon(two.a, "q1", T0)                      # replica A then dies: no renewal
    _abandon(two.b, "q2", T0)                      # replica B's worker dies too
    grace = server._ASK_LEASE_GRACE_SECONDS
    assert server._claim_ask_slot(two.b, "q3", T0 + grace - 1) == "abandoned"
    assert server._claim_ask_slot(two.b, "q3", T0 + grace + 1) == ""
    assert _detached(two.a) == {}, _detached(two.a)


def test_only_the_live_replica_keeps_its_lease(two):
    _abandon(two.a, "q1", T0)
    _abandon(two.b, "q2", T0)
    now = T0
    for _ in range(10):                            # only A renews (B died)
        now += server._ASK_LEASE_RENEW_SECONDS
        server._renew_detached_leases(two.a, {"q1"}, now)
    assert set(_detached(two.b)) == {"q1"}, _detached(two.b)


def test_renewal_never_resurrects_a_removed_entry(two):
    _abandon(two.a, "q1", T0)
    server._ask_slot_release(two.b, "q1", T0 + 5)  # the worker ended
    server._renew_detached_leases(two.a, {"q1"}, T0 + 60)
    assert "q1" not in _detached(two.b)


def test_renewal_never_resurrects_an_expired_lease(two):
    _abandon(two.a, "q1", T0)
    late = T0 + server._ASK_LEASE_GRACE_SECONDS + 1
    server._renew_detached_leases(two.a, {"q1"}, late)
    slot = two.b.get(server._ASK_ACTIVE_KEY)
    assert server._ask_detached(slot, late) == {}, slot      # not counted
    assert _detached(two.b).get("q1") in (None, T0), slot     # never renewed


def test_renewal_racing_a_release_on_another_replica_does_not_resurrect(two):
    """Replica A's renewal reads the slot, replica B (another process: a
    direct revisioned write to the shared table) releases q1, then A writes:
    the revision condition refuses the stale write, and the re-read finds q1
    gone."""
    import json

    def _other_process_releases(job_id: str):
        key = two.b._key(server._ASK_ACTIVE_KEY)
        with two.table.lock:
            item = dict(two.table.items[key])
            state = json.loads(item["job_state"])
            state["detached"].pop(job_id)
            item.update(job_state=json.dumps(state), job_rev=int(item["job_rev"]) + 1)
            two.table.items[key] = item

    class _Racing(JobStore):
        hook = None

        def _remote_get_raw(self, job_id):
            read = super()._remote_get_raw(job_id)
            hook, type(self).hook = type(self).hook, None
            if hook:
                hook()
            return read
    racing = _Racing("ask", table=two.table)
    _abandon(two.a, "q1", T0)
    _Racing.hook = lambda: _other_process_releases("q1")
    server._renew_detached_leases(racing, {"q1"}, T0 + 60)
    assert "q1" not in _detached(two.b)
    assert two.table.conflicts >= 1


def test_guard_renewal_only_touches_this_processes_live_workers(two):
    _abandon(two.a, "q1", T0)
    _abandon(two.b, "q2", T0 + 10)
    server._renew_detached_leases(two.a, {"q1", "not-detached"}, T0 + 60)
    leases = _detached(two.b)
    assert leases["q1"] == T0 + 60 and leases["q2"] == T0 + 10, leases
    assert "not-detached" not in leases


# ── the renewer thread, end to end ────────────────────────────────────────

def test_the_renewer_keeps_a_wedged_workers_lease_and_drops_it_when_it_ends(monkeypatch):
    import agents.chat_client as chat_client_mod
    table = _SharedTable()
    store = JobStore("ask", table=table)
    clock = {"now": T0}
    monkeypatch.setattr(server, "_ASK_JOBS", store)
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _GatedClient)
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    monkeypatch.setattr(server, "_ASK_LEASE_RENEW_SECONDS", 0.05)
    _GatedClient.gates = {"wedged": threading.Event()}
    try:
        job = server.ask_async(AskRequest(question="wedged"))["job_id"]
        assert job in server._ASK_LOCAL_WORKERS
        clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
        import asyncio
        asyncio.run(server.ask_result(job, wait=0.0))          # abandoned: detached
        # Two hours pass with the worker alive; each step is shorter than the
        # grace, and the renewer refreshes the lease at every step.
        step = server._ASK_LEASE_GRACE_SECONDS / 2
        while clock["now"] < T0 + 2 * HOUR:
            clock["now"] += step
            server._ASK_RENEWER_WAKE.set()             # a renewer already asleep wakes too
            deadline = time.time() + 5
            while time.time() < deadline and _detached(store).get(job) != clock["now"]:
                time.sleep(0.01)
            assert _detached(store).get(job) == clock["now"], (clock, _detached(store))
        assert server._claim_ask_slot(store, "other", clock["now"]) == ""
    finally:
        _GatedClient.gates["wedged"].set()
    _wait_done(job)
    assert job not in server._ASK_LOCAL_WORKERS
    assert job not in _detached(store)
