# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One shared reset lease makes removals, Reset All and provisions exclude
each other across processes.

The app runs one ECS task, but a rolling deploy runs two for a while, and
the onboarding runtime container is always a separate process. A removal
and Reset All each held only this process's reset-domain claim and wrote
status rows to the shared table without a condition, so:

* a guard in another process read those rows with an eventually consistent
  read, and could miss one just written;
* another app task could start a second Reset All, or a removal during one,
  over a running one;
* a status row whose last write failed (or whose process was killed) said
  ``running`` until DynamoDB's TTL cleanup deleted it, which has no bound;
* Reset All started over a provision already in flight.

Now each claim takes ONE lease row in the shared reset store with a
conditional write before anything is removed (owner token, scope ``all`` or
the system id, an explicit ``expires_at``) and gives it back owner-checked,
retrying a failed write a few times. Guards read it with ConsistentRead and
refuse when the read fails; an expired lease counts as absent. A provision
takes a provision lease of its system, and Reset All and a removal refuse
while one is live.

Offline: the shared table is a fake that can hide a write from an
eventually consistent read and fail any write; the cascades are replaced by
steps that record their call (and hold the first one when asked).
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import agents.onboard as onboard
import agents.onboard_agent as oa
from agents.catalog import SystemCatalog
from agents.job_store import JobStore
from tests.unit._shared_table import SharedTable

SID, SID2 = "line-mes", "plant-cmms"
JOB_WAIT = 60.0

#: The module state one app process holds: its reset domain, reset status
#: store, lease store and in-process provision records.
_PROCESS_STATE = ("_RESET_DOMAIN", "_RESET_JOBS", "_RESET_LEASES", "_PROVISIONS_RUNNING",
                  "_PROVISION_CLAIMS", "_PROVISION_LEASES", "_RESET_ALL_JOBS")


@pytest.fixture
def table():
    return SharedTable()


@pytest.fixture(autouse=True)
def _one_process(monkeypatch):
    """This test's own app process (every name it swaps is restored at the end)."""
    for name in _PROCESS_STATE:
        monkeypatch.setattr(onboard, name, getattr(onboard, name, None), raising=False)
    Replica(None).install()
    cat = SystemCatalog([{"id": SID, "name": "Line MES", "status": "onboarded",
                          "connection_type": "PostgreSQL", "schema": "mes"},
                         {"id": SID2, "name": "Plant CMMS", "status": "onboarded",
                          "connection_type": "PostgreSQL", "schema": "cmms"}])
    monkeypatch.setattr(onboard, "catalog", cat)
    monkeypatch.setattr(onboard, "_graph_system_ids", lambda: [])
    monkeypatch.delenv("SYSTEM_ID", raising=False)


class Replica:
    """One app process on the shared table (``table`` None: memory only)."""

    def __init__(self, table):
        self.state = {"_RESET_DOMAIN": onboard._ResetDomain(),
                      "_RESET_JOBS": JobStore("reset", table=table),
                      "_RESET_LEASES": JobStore("reset", table=table),
                      "_RESET_ALL_JOBS": JobStore("reset-all", table=table),
                      "_PROVISIONS_RUNNING": {}, "_PROVISION_CLAIMS": set(),
                      "_PROVISION_LEASES": {}}

    def install(self):
        for name, value in self.state.items():
            setattr(onboard, name, value)

    def __enter__(self):
        self._saved = {name: getattr(onboard, name, None) for name in _PROCESS_STATE}
        self.install()
        return self

    def __exit__(self, *_exc):
        for name, value in self._saved.items():
            setattr(onboard, name, value)
        return False


class _Clock:
    def __init__(self):
        self.t = int(time.time())

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds) -> None:
        self.t += int(seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr("agents.reset_lease.now", c)
    monkeypatch.setattr("agents.reset_lease.RELEASE_BACKOFF_S", 0.0)
    return c


class Cascades:
    """Both cascades replaced: each call is recorded (the destructive steps
    it stands for); with ``hold_next`` the next call waits until released."""

    def __init__(self, monkeypatch):
        self.calls: list = []
        self.hold_next = False
        self.entered, self.release = threading.Event(), threading.Event()
        cascades = self

        async def _single(system_id, full=False, **_kw):
            cascades.calls.append(("single", system_id))
            await cascades._maybe_hold()
            return {"status": "reset", "system_id": system_id}

        async def _all(full):
            cascades.calls.append(("all", full))
            await cascades._maybe_hold()
            return {"status": "reset", "full": full}
        monkeypatch.setattr(onboard, "_run_reset_steps", _single)
        monkeypatch.setattr(onboard, "_global_reset_steps", _all)

    async def _maybe_hold(self):
        if self.hold_next:
            self.hold_next = False
            self.entered.set()
            await asyncio.to_thread(self.release.wait, JOB_WAIT)


@pytest.fixture
def cascades(monkeypatch):
    return Cascades(monkeypatch)


@contextmanager
def held(replica: Replica, cascades: Cascades, call):
    """Run ``call()`` (a coroutine factory) on its own thread as ``replica``,
    held in its cascade until the block ends; the block runs as this test's
    process."""
    out: dict = {}

    def _run():
        try:
            out["result"] = asyncio.run(call())
        except Exception as exc:  # noqa: BLE001 - the test reads it
            out["error"] = exc
    cascades.hold_next = True
    with replica:
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        assert cascades.entered.wait(JOB_WAIT), "the held call never reached its cascade"
    try:
        yield out
    finally:
        with replica:
            cascades.release.set()
            thread.join(JOB_WAIT)
        assert not thread.is_alive()


def _reset_all(full=False):
    return lambda: onboard.onboard_reset(None, full=full)


def _removal(sid=SID, full=False):
    return lambda: onboard.onboard_reset_system(sid, wait=True, full=full)


def _refused(call) -> HTTPException:
    with pytest.raises(HTTPException) as err:
        asyncio.run(call())
    return err.value


# ── one lease across app processes ─────────────────────────────────────────

def test_a_second_reset_all_on_another_replica_is_refused(table, cascades):
    a, b = Replica(table), Replica(table)
    with held(a, cascades, _reset_all()) as out_a:
        with b:
            err = _refused(_reset_all(full=True))
    assert err.status_code == 409 and "already being reset" in err.detail, err.detail
    assert cascades.calls == [("all", False)], "two Reset Alls ran at once"
    assert out_a["result"]["status"] == "reset"


def test_a_removal_on_another_replica_is_refused_during_reset_all(table, cascades):
    a, b = Replica(table), Replica(table)
    with held(a, cascades, _reset_all()):
        with b:
            err = _refused(_removal())
    assert err.status_code == 409 and "All systems are being reset" in err.detail, err.detail
    assert cascades.calls == [("all", False)]


def test_reset_all_on_another_replica_is_refused_during_a_removal(table, cascades):
    a, b = Replica(table), Replica(table)
    with held(a, cascades, _removal()):
        with b:
            err = _refused(_reset_all())
    assert err.status_code == 409 and SID in err.detail, err.detail
    assert cascades.calls == [("single", SID)]


@pytest.mark.parametrize("other", [SID, SID2], ids=["same-system", "other-system"])
def test_a_second_removal_on_another_replica_is_refused(table, cascades, other):
    a, b = Replica(table), Replica(table)
    with held(a, cascades, _removal()):
        with b:
            err = _refused(_removal(other))
    assert err.status_code == 409, err.detail
    assert cascades.calls == [("single", SID)]


def test_the_lease_is_given_back_when_the_reset_ends(table, cascades):
    a, b = Replica(table), Replica(table)
    with a:
        assert asyncio.run(_reset_all()())["status"] == "reset"
    with b:
        assert asyncio.run(_removal()())["status"] == "reset"
        assert asyncio.run(_reset_all()())["status"] == "reset"
    assert [c[0] for c in cascades.calls] == ["all", "single", "all"]


def test_a_lease_that_cannot_be_written_removes_nothing(table, cascades):
    a = Replica(table)
    table.fail_puts = True
    with a:
        for call in (_reset_all(), _removal()):
            err = _refused(call)
            assert err.status_code == 503 and "Nothing was removed" in err.detail, err.detail
        table.fail_puts = False
        assert asyncio.run(_reset_all()())["status"] == "reset", "the refused claim was kept"
    assert cascades.calls == [("all", False)]


# ── guards read the lease consistently, and refuse when they cannot ────────

def test_every_guard_reads_the_lease_with_a_consistent_read(table, cascades, monkeypatch):
    """The table hides the lease from an eventually consistent read."""
    table.hold_back()
    a, b = Replica(table), Replica(table)
    runtime = JobStore("reset", table=table)
    with held(a, cascades, _reset_all()):
        with b:
            app = onboard._reset_conflict_or_none(SID)
            chat = oa._removal_running_refusal(SID)
        monkeypatch.setenv("SYSTEM_ID", "onboarding")
        monkeypatch.setattr(oa, "_shared_reset_jobs", lambda: runtime, raising=False)
        in_runtime = oa._removal_running_refusal(SID)
        monkeypatch.delenv("SYSTEM_ID")
    for res in (app, chat, in_runtime):
        assert (res or {}).get("reason") == "reset_in_progress", res


def test_an_app_replica_that_cannot_read_the_lease_refuses(table):
    b = Replica(table)
    table.fail_reads = True
    with b:
        for res in (onboard._reset_conflict_or_none(SID), oa._removal_running_refusal(SID)):
            assert (res or {}).get("reason") == "reset_state_unavailable", res
        table.fail_reads = False
        assert onboard._reset_conflict_or_none(SID) is None
        assert oa._removal_running_refusal(SID) is None


# ── a lease that is never given back ends at its expires_at ────────────────

def test_a_lease_whose_release_fails_stops_counting_at_its_expiry(table, cascades, clock):
    a, b = Replica(table), Replica(table)
    table.fail_writes_after = 1           # the claim lands; every write after it fails
    with a:
        assert asyncio.run(_reset_all()())["status"] == "reset"
    table.fail_writes_after = None
    with b:
        assert (onboard._reset_conflict_or_none(SID) or {}).get("reason") == "reset_in_progress"
        clock.advance(onboard._RESET_LEASE_SECONDS + 1)
        assert onboard._reset_conflict_or_none(SID) is None, "an expired lease still blocked"
        assert asyncio.run(_reset_all()())["status"] == "reset"


def test_a_lease_whose_owner_died_stops_counting_at_its_expiry(table, cascades, clock):
    a, b = Replica(table), Replica(table)
    with a:
        assert onboard._RESET_DOMAIN.claim_all() is None    # then its process is killed
    with b:
        assert _refused(_reset_all()).status_code == 409
        assert (onboard._reset_conflict_or_none(SID) or {}).get("reason") == "reset_in_progress"
        clock.advance(onboard._RESET_LEASE_SECONDS + 1)
        assert onboard._reset_conflict_or_none(SID) is None
        assert asyncio.run(_reset_all()())["status"] == "reset"
    assert cascades.calls == [("all", False)]


def test_a_removal_record_left_running_stops_blocking_at_its_expiry(clock):
    onboard._RESET_JOBS[SID] = {"state": "running", "full": False,
                                "expires_at": clock() + 60}
    assert (onboard._reset_conflict_or_none(SID) or {}).get("reason") == "reset_in_progress"
    clock.advance(61)
    assert onboard._reset_conflict_or_none(SID) is None


def test_a_lease_is_given_back_only_by_its_owner(table, clock):
    from agents import reset_lease
    store = JobStore("reset", table=table)
    first = reset_lease.new(SID, 60)
    assert store.lease_acquire(reset_lease.RESET_LEASE_KEY, first, now=clock()) is None
    assert store.lease_release(reset_lease.RESET_LEASE_KEY, "someone-else") is True
    assert store.lease_read(reset_lease.RESET_LEASE_KEY, now=clock())["owner"] == first["owner"]
    clock.advance(61)
    second = reset_lease.new(SID2, 60)
    assert store.lease_acquire(reset_lease.RESET_LEASE_KEY, second, now=clock()) is None
    assert store.lease_release(reset_lease.RESET_LEASE_KEY, first["owner"]) is True
    assert store.lease_read(reset_lease.RESET_LEASE_KEY, now=clock())["owner"] == \
        second["owner"], "a late release removed the lease another owner took"


def test_a_release_that_keeps_failing_is_retried_then_reported(table, clock):
    from agents import reset_lease
    store = JobStore("reset", table=table)
    lease = reset_lease.new(SID, 60)
    store.lease_acquire(reset_lease.RESET_LEASE_KEY, lease, now=clock())
    table.fail_deletes = True
    before = table.writes
    assert store.lease_release(reset_lease.RESET_LEASE_KEY, lease["owner"],
                               backoff_s=0.0) is False
    assert table.writes - before == 3, "the release was not retried"
    table.fail_deletes = False
    assert store.lease_release(reset_lease.RESET_LEASE_KEY, lease["owner"]) is True
    assert store.lease_read(reset_lease.RESET_LEASE_KEY, now=clock()) is None


def test_a_strict_write_that_lands_re_enables_the_fail_soft_backend(table):
    store = JobStore("reset", table=table)
    table.fail_puts = True
    store["x"] = {"state": "running"}     # fail-soft: kept in memory, the table given up on
    table.fail_puts = False
    store.put_strict("y", {"state": "running"})
    store["x"] = {"state": "done"}
    assert JobStore("reset", table=table).get_strict("x") == {"state": "done"}


# ── provisions hold Reset All and a removal off ────────────────────────────

def test_reset_all_is_refused_while_a_provision_runs(cascades):
    claim = onboard._claim_provision(SID)
    try:
        err = _refused(_reset_all())
        assert err.status_code == 409 and f"'{SID}' is being provisioned" in err.detail
        assert cascades.calls == [], "Reset All ran over a provision in flight"
    finally:
        claim.release()
    assert asyncio.run(_reset_all()())["status"] == "reset"


def test_reset_all_is_refused_while_another_replica_provisions(table, cascades):
    a, b = Replica(table), Replica(table)
    with b:
        claim = onboard._claim_provision(SID2)
    try:
        with a:
            err = _refused(_reset_all())
        assert err.status_code == 409 and f"'{SID2}' is being provisioned" in err.detail
        assert cascades.calls == []
    finally:
        with b:
            claim.release()
    with a:
        assert asyncio.run(_reset_all()())["status"] == "reset"


def test_a_removal_is_refused_while_another_replica_provisions_the_system(table, cascades):
    a, b = Replica(table), Replica(table)
    with b:
        claim = onboard._claim_provision(SID)
    try:
        with a:
            err = _refused(_removal())
        assert err.status_code == 409 and "being provisioned" in err.detail, err.detail
        assert cascades.calls == []
    finally:
        with b:
            claim.release()


def test_a_provision_on_another_replica_is_refused_while_one_runs(table):
    a, b = Replica(table), Replica(table)
    with b:
        claim = onboard._claim_provision(SID)
    try:
        with a, pytest.raises(onboard.ProvisionBusyError):
            onboard._claim_provision(SID)
    finally:
        with b:
            claim.release()
    with a:
        onboard._claim_provision(SID).release()
