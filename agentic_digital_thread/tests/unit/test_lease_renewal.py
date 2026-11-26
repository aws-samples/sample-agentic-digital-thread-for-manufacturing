# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A lease this process holds is renewed while its operation runs, and an
operation whose lease is lost stops before its next graph write.

A provision held its process-local claim for the whole run, but its shared
provision lease (``_PROVISION_LEASE_SECONDS``, 30 minutes) was written once
and never renewed: a provision that outlived it no longer kept a provision of
the same system in another process (or a removal there) away, while the
graph-resubmit opt-ins rely on exactly that exclusion. The reset lease of a
removal or Reset All had the same fixed bound, and so did the removal's
``running`` record.

Now every lease this process takes is renewed (``reset_lease.Renewal``)
with a write that lands only while the lease is still its own and still
live, and is given back after its renewal stopped. A lease whose renewal is
refused, or that was not renewed before its expiry margin, is lost: the
graph writes of its operation (``db.write_guard``) refuse from then on.

Offline: the shared table is a fake (``SharedTable``), the clock is a fake,
and the graph backend is a recorder.
"""
from __future__ import annotations

import time

import pytest

import agents.db as db
import agents.onboard as onboard
from agents import reset_lease
from agents.job_store import JobStore, JobStoreUnavailableError
from tests.unit.test_one_reset_lease import (  # noqa: F401 - fixtures
    SID, Replica, _one_process, clock, table)

KEY = reset_lease.provision_key(SID)


@pytest.fixture
def sent(monkeypatch):
    """The graph statements that reached the backend."""
    out: list = []
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run", lambda cypher, parameters=None: out.append(cypher) or [])
    return out


def _held_lease(store, clock, seconds=600):
    lease = reset_lease.new(SID, seconds)
    assert store.lease_acquire(KEY, lease, now=clock()) is None
    return lease


# ── JobStore.lease_renew ───────────────────────────────────────────────────

@pytest.mark.parametrize("shared", [True, False], ids=["shared-table", "memory-only"])
def test_a_renewal_moves_a_held_lease_on(table, clock, shared):
    store = JobStore("reset", table=table if shared else None)
    lease = _held_lease(store, clock)
    clock.advance(500)
    assert store.lease_renew(KEY, {**lease, "expires_at": clock() + 600}, now=clock()) is True
    clock.advance(500)                      # 1000 s after it was taken
    assert (store.lease_read(KEY, now=clock()) or {}).get("owner") == lease["owner"]
    if shared:
        other = JobStore("reset", table=table)
        assert other.lease_acquire(KEY, reset_lease.new(SID, 600), now=clock()) is not None


@pytest.mark.parametrize("shared", [True, False], ids=["shared-table", "memory-only"])
@pytest.mark.parametrize("how", ["expired", "given-back", "taken"])
def test_a_renewal_never_brings_back_a_lease_that_is_no_longer_its_own(table, clock, shared, how):
    store = JobStore("reset", table=table if shared else None)
    lease = _held_lease(store, clock)
    if how == "expired":
        clock.advance(601)
    elif how == "given-back":
        assert store.lease_release(KEY, lease["owner"]) is True
    else:
        clock.advance(601)
        assert store.lease_acquire(KEY, reset_lease.new(SID, 600), now=clock()) is None
    assert store.lease_renew(KEY, {**lease, "expires_at": clock() + 600}, now=clock()) is False
    held = store.lease_read(KEY, now=clock())
    assert (held or {}).get("owner") != lease["owner"], "a renewal brought the lease back"


def test_a_renewal_the_table_cannot_write_raises(table, clock):
    store = JobStore("reset", table=table)
    lease = _held_lease(store, clock)
    table.fail_puts = True
    with pytest.raises(JobStoreUnavailableError):
        store.lease_renew(KEY, {**lease, "expires_at": clock() + 600}, now=clock())


# ── reset_lease.Renewal ────────────────────────────────────────────────────

def test_a_renewal_renews_and_reports_each_renewal(table, clock):
    store = JobStore("reset", table=table)
    lease = _held_lease(store, clock)
    seen: list = []
    renewal = reset_lease.Renewal(store, KEY, lease, 600, on_renewed=seen.append)
    for _ in range(4):
        clock.advance(300)
        assert renewal.renew_once() is True
    assert renewal.held()
    renewal.check()
    assert [s["expires_at"] for s in seen] == [clock() - 900 + 600, clock() - 600 + 600,
                                               clock() - 300 + 600, clock() + 600]
    assert int(table.items[f"__job__:reset:{KEY}"]["lease_expires_at"]) == clock() + 600


def test_a_refused_renewal_loses_the_lease(table, clock):
    store = JobStore("reset", table=table)
    lease = _held_lease(store, clock)
    renewal = reset_lease.Renewal(store, KEY, lease, 600)
    assert store.lease_release(KEY, lease["owner"])        # it is no longer there
    assert renewal.renew_once() is None
    assert not renewal.held()
    with pytest.raises(reset_lease.LeaseLostError):
        renewal.check()


def test_renewals_that_keep_failing_lose_the_lease_before_it_expires(table, clock):
    store = JobStore("reset", table=table)
    lease = _held_lease(store, clock)
    renewal = reset_lease.Renewal(store, KEY, lease, 600)
    table.fail_puts = True
    clock.advance(300)
    assert renewal.renew_once() is False                   # tried again soon
    assert renewal.held()
    clock.advance(600 - 300 - reset_lease.LOST_MARGIN_SECONDS)
    assert not renewal.held(), "a lease past its expiry margin still read as held"
    with pytest.raises(reset_lease.LeaseLostError):
        renewal.check()
    table.fail_puts = False
    assert renewal.renew_once() is None, "a renewal revived a lease past its margin"


def test_a_stopped_renewal_neither_renews_nor_reports(table, clock):
    store = JobStore("reset", table=table)
    lease = _held_lease(store, clock)
    seen: list = []
    renewal = reset_lease.Renewal(store, KEY, lease, 600, on_renewed=seen.append)
    renewal.stop()
    assert not renewal.held()
    clock.advance(300)
    assert renewal.renew_once() is None
    assert seen == []
    assert int(table.items[f"__job__:reset:{KEY}"]["lease_expires_at"]) == lease["expires_at"]


def test_a_stop_during_a_renewal_write_keeps_its_report_out(table, clock):
    """``stop`` arrives while the renewal's write is in flight: the write
    lands, but nothing is reported after ``stop`` returned (the terminal
    record written after it is never overwritten)."""
    inner = JobStore("reset", table=table)
    lease = _held_lease(inner, clock)
    seen: list = []

    class _StoppedMidWrite:
        def lease_renew(self, *args, **kwargs):
            renewal.stop()
            return inner.lease_renew(*args, **kwargs)
    renewal = reset_lease.Renewal(_StoppedMidWrite(), KEY, lease, 600, on_renewed=seen.append)
    clock.advance(300)
    assert renewal.renew_once() is None
    assert seen == []


def test_no_lease_is_renewed_past_its_longest_renewal(table, clock):
    store = JobStore("reset", table=table)
    lease = _held_lease(store, clock)
    renewal = reset_lease.Renewal(store, KEY, lease, 600, max_seconds=1200)
    clock.advance(500)
    assert renewal.renew_once() is True
    clock.advance(500)
    assert renewal.renew_once() is True
    assert renewal.lease["expires_at"] == lease["started_at"] + 1200, "renewed past the cap"
    clock.advance(1200 - 1000 - reset_lease.LOST_MARGIN_SECONDS)
    assert not renewal.held()
    assert renewal.renew_once() is None


def test_the_renewal_thread_renews_until_stopped(table, monkeypatch):
    monkeypatch.setattr(reset_lease, "RENEW_SECONDS", 0.01)
    store = JobStore("reset", table=table)
    lease = reset_lease.new(SID, 600)
    assert store.lease_acquire(KEY, lease, now=reset_lease.now()) is None
    renewed: list = []
    renewal = reset_lease.Renewal(store, KEY, lease, 600, on_renewed=renewed.append).start()
    deadline = time.monotonic() + 10
    while len(renewed) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    renewal.stop()
    assert len(renewed) >= 3, "the renewal thread did not renew"
    count = len(renewed)
    time.sleep(0.1)
    assert len(renewed) == count, "the renewal thread renewed after stop"


# ── db.write_guard ─────────────────────────────────────────────────────────

def _refuse():
    raise reset_lease.LeaseLostError("lost")


@pytest.mark.parametrize("write", [db.neptune_write, db.neptune_write_outcome,
                                   db.neptune_write_returning],
                         ids=["neptune_write", "neptune_write_outcome",
                              "neptune_write_returning"])
def test_a_write_guard_stops_every_graph_write_before_it_is_sent(sent, write):
    write("MERGE (n:X {id: 1})")
    with db.write_guard(_refuse):
        with pytest.raises(reset_lease.LeaseLostError):
            write("MERGE (n:X {id: 2})")
        db.neptune_query("MATCH (n) RETURN n")           # reads are not guarded
    write("MERGE (n:X {id: 3})")
    assert sent == ["MERGE (n:X {id: 1})", "MATCH (n) RETURN n", "MERGE (n:X {id: 3})"]


# ── the provision lease ────────────────────────────────────────────────────

def test_a_provision_lease_is_renewed_while_its_claim_is_held(table, clock):
    a, b = Replica(table), Replica(table)
    with a:
        claim = onboard._claim_provision(SID)
        renewal = onboard._PROVISION_LEASES[SID]
    try:
        for _ in range(3):
            clock.advance(onboard._PROVISION_LEASE_SECONDS // 2)
            assert renewal.renew_once() is True
        # Past one and a half lease lengths: unrenewed it had expired.
        with b:
            assert reset_lease.provision_holding(onboard._RESET_LEASES, SID, lambda _sid: None)
            with pytest.raises(onboard.ProvisionBusyError):
                onboard._claim_provision(SID)
    finally:
        with a:
            claim.release()
    assert not renewal.held(), "the renewal kept going after the claim was released"
    assert renewal.renew_once() is None
    with b:
        assert not reset_lease.provision_holding(onboard._RESET_LEASES, SID, lambda _sid: None)
        onboard._claim_provision(SID).release()


@pytest.mark.parametrize("how", ["taken-over", "not-renewed"])
def test_a_provision_whose_lease_was_lost_stops_before_its_next_graph_write(
        table, clock, sent, how):
    a, b = Replica(table), Replica(table)
    with a:
        claim = onboard._claim_provision(SID)
        renewal = onboard._PROVISION_LEASES[SID]
        with claim:                                   # the job's own thread
            assert onboard.neptune_write("MERGE (n:X {id: 1})")
            if how == "taken-over":
                clock.advance(onboard._PROVISION_LEASE_SECONDS + 1)
                with b:
                    onboard._claim_provision(SID)     # another process provisions it now
                assert renewal.renew_once() is None
            else:
                table.fail_puts = True
                clock.advance(onboard._PROVISION_LEASE_SECONDS // 2)
                assert renewal.renew_once() is False
                clock.advance(onboard._PROVISION_LEASE_SECONDS // 2)
            with pytest.raises(reset_lease.LeaseLostError):
                onboard.neptune_write_resubmit_safe("MERGE (n:X {id: 2})")
            with pytest.raises(reset_lease.LeaseLostError):
                onboard.neptune_write_returning("MERGE (n:X {id: 3}) RETURN n")
    assert sent == ["MERGE (n:X {id: 1})"]


def test_a_provision_counted_in_flight_checks_its_lease_too(table, clock, sent):
    a = Replica(table)
    with a:
        with onboard._provision_in_flight(SID):
            assert onboard.neptune_write("MERGE (n:X {id: 1})")
            clock.advance(onboard._PROVISION_LEASE_SECONDS)
            with pytest.raises(reset_lease.LeaseLostError):
                onboard.neptune_write("MERGE (n:X {id: 2})")
    assert sent == ["MERGE (n:X {id: 1})"]


def test_a_claim_entered_after_its_release_writes_nothing(table, sent):
    with Replica(table):
        claim = onboard._claim_provision(SID)
        claim.release()
        with claim:
            with pytest.raises(reset_lease.LeaseLostError):
                onboard.neptune_write("MERGE (n:X {id: 1})")
    assert sent == []


# ── the reset lease ────────────────────────────────────────────────────────

def test_a_removal_lease_and_its_running_record_are_renewed_while_it_runs(table, clock):
    a, b = Replica(table), Replica(table)
    with a:
        onboard._reset_publish_claim(SID, False)
        renewal = onboard._RESET_DOMAIN.renewal(SID)
        for _ in range(3):
            clock.advance(onboard._RESET_LEASE_SECONDS // 2)
            assert renewal.renew_once() is True
        assert onboard._RESET_JOBS[SID]["expires_at"] == renewal.lease["expires_at"]
    with b:
        assert (onboard._reset_conflict_or_none(SID) or {}).get("reason") == "reset_in_progress"
        assert onboard._reset_in_progress(SID), "the running record expired while it ran"
    with a:
        onboard._end_removal_record(SID, {"state": "done", "full": False})
        clock.advance(60)
        assert renewal.renew_once() is None
        assert onboard._RESET_JOBS[SID]["state"] == "done", "a renewal put running back"
        onboard._RESET_DOMAIN.release_single(SID)
    assert not renewal.held()
    with b:
        assert onboard._reset_conflict_or_none(SID) is None


def test_reset_all_releases_its_renewal_with_its_claim(table, clock):
    with Replica(table):
        assert onboard._RESET_DOMAIN.claim_all() is None
        renewal = onboard._RESET_DOMAIN.renewal(reset_lease.ALL)
        clock.advance(onboard._RESET_LEASE_SECONDS // 2)
        assert renewal.renew_once() is True
        onboard._RESET_DOMAIN.release_all()
        assert onboard._RESET_DOMAIN.renewal(reset_lease.ALL) is None
        assert not renewal.held()
        assert onboard._RESET_LEASES.lease_read(reset_lease.RESET_LEASE_KEY, now=clock()) is None


def test_a_reset_all_record_is_never_put_back_to_running_after_its_end(table, clock):
    with Replica(table):
        job = "job-end"
        assert onboard._RESET_DOMAIN.claim_all(
            on_renewed=lambda lease: onboard._reset_all_renewed(job, False, lease)) is None
        onboard._publish_reset_all_job(job, False)
        renewal = onboard._RESET_DOMAIN.renewal(reset_lease.ALL)
        clock.advance(60)
        assert renewal.renew_once() is True
        assert onboard._RESET_ALL_JOBS[job]["state"] == "running"
        assert onboard._RESET_ALL_JOBS[job]["expires_at"] == renewal.lease["expires_at"]
        onboard._end_reset_all_record(job, {"state": "done", "job_id": job})
        clock.advance(60)
        assert renewal.renew_once() is None
        assert onboard._RESET_ALL_JOBS[job]["state"] == "done", "a renewal put running back"
        onboard._RESET_DOMAIN.release_all()


@pytest.mark.parametrize("what", ["reset-all", "removal"])
def test_a_renewal_record_write_never_replaces_a_finished_record(table, clock, what):
    """The record a renewal writes is a compare-and-set over a running
    record only: one that arrives after the terminal record (from anywhere)
    leaves it as it is."""
    lease = reset_lease.new(SID, 600)
    with Replica(table):
        if what == "reset-all":
            onboard._RESET_ALL_JOBS.put_strict("job-x", {"state": "done", "job_id": "job-x"})
            onboard._reset_all_renewed("job-x", False, lease)
            record = onboard._RESET_ALL_JOBS.get_consistent("job-x")
        else:
            onboard._RESET_JOBS.put_strict(SID, {"state": "done", "full": False})
            onboard._removal_renewed(SID, False, lease)
            record = onboard._RESET_JOBS.get_consistent(SID)
    assert record["state"] == "done", record


def test_a_removal_refused_inside_its_claim_leaves_no_renewal(table, clock):
    """``publish`` refuses (a "Remove again" of a system that recovered):
    the lease the claim took is given back and its renewal stopped."""
    with Replica(table):
        with pytest.raises(onboard.ResetRetryNotNeededError):
            onboard._reset_publish_claim(SID, False, retry=True)
        assert onboard._RESET_DOMAIN.renewal(SID) is None
        assert onboard._RESET_LEASES.lease_read(reset_lease.RESET_LEASE_KEY, now=clock()) is None


@pytest.mark.parametrize("what", ["provision", "removal"])
def test_a_held_lease_is_renewed_on_its_own(table, clock, monkeypatch, what):
    """The claim starts the renewal thread itself: nobody calls it."""
    monkeypatch.setattr(reset_lease, "RENEW_SECONDS", 0.01)
    store_key = (f"__job__:reset:{KEY}" if what == "provision"
                 else f"__job__:reset:{reset_lease.RESET_LEASE_KEY}")
    with Replica(table):
        if what == "provision":
            claim = onboard._claim_provision(SID)
            release = claim.release
        else:
            onboard._reset_publish_claim(SID, False)
            release = lambda: onboard._RESET_DOMAIN.release_single(SID)  # noqa: E731
        try:
            first = int(table.items[store_key]["lease_expires_at"])
            clock.advance(100)
            deadline = time.monotonic() + 10
            while (int(table.items[store_key]["lease_expires_at"]) == first
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            assert int(table.items[store_key]["lease_expires_at"]) == first + 100
        finally:
            release()
        assert store_key not in table.items
