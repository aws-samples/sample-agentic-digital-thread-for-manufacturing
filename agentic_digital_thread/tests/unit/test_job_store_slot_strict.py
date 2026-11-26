# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``claim_slot`` / ``touch_slot`` / ``release_slot`` with ``strict=True``.

The slot primitives are fail-soft by default (``test_job_store_slot.py``):
a backend error, or a backend the fail-soft path already turned off, makes
the decision against this process's own copy. For a slot that must hold
across app tasks (the memory extraction model save), that copy is no
coordination at all: two tasks that both hit a throttle would both "claim"
the slot. With ``strict=True`` and a table configured, a backend error or a
disabled backend makes claim and touch return False with no in-memory
fallback, and release report False. With no table configured (local dev,
the unit lane) strict is the same locked in-memory CAS as before. Offline.
"""
from __future__ import annotations

import pytest

from agents.job_store import JobStore
from tests.unit.test_job_store_slot import _FakeSlotTable, _idle, _running

SLOT = "active"
STALE = 300


class _ThrottledTable:
    """Every write raises a non-conditional backend error (a throttle, an
    IAM or configuration error)."""

    def __init__(self):
        self.puts = 0

    def put_item(self, **_kwargs):
        self.puts += 1
        raise RuntimeError("ProvisionedThroughputExceededException")

    def get_item(self, **_kwargs):
        raise RuntimeError("ProvisionedThroughputExceededException")


class _FailingAfter(_FakeSlotTable):
    """A working table whose writes start raising once ``broken`` is set."""

    def __init__(self, store: dict | None = None):
        super().__init__(store)
        self.broken = False

    def put_item(self, Item, **kwargs):  # noqa: N803
        if self.broken:
            raise RuntimeError("ThrottlingException")
        return super().put_item(Item, **kwargs)


# -- a configured table that errors: refused, never decided from memory ------------

def test_two_tasks_on_an_erroring_table_both_refused_under_strict():
    task1 = JobStore("slot-strict", table=_ThrottledTable())
    task2 = JobStore("slot-strict", table=_ThrottledTable())
    assert task1.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True) is False
    assert task2.claim_slot(SLOT, _running("b", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True) is False
    # Nothing was kept in process memory as if it were a claim.
    assert task1._mem.get(SLOT) is None and task2._mem.get(SLOT) is None


def test_the_same_two_tasks_without_strict_still_fail_soft():
    """The default is unchanged: each task decides from its own copy."""
    task1 = JobStore("slot-soft", table=_ThrottledTable())
    task2 = JobStore("slot-soft", table=_ThrottledTable())
    assert task1.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)
    assert task2.claim_slot(SLOT, _running("b", 1000.0), stale_seconds=STALE, now=1000.0)


def test_a_strict_backend_error_does_not_disable_the_backend():
    """A strict call never turns the fail-soft path to memory-only: a later
    strict call asks the table again."""
    table = _FailingAfter()
    store = JobStore("slot-strict", table=table)
    table.broken = True
    assert not store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                                now=1000.0, strict=True)
    table.broken = False
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    assert table._store[store._key(SLOT)]["slot_owner"] == "a"


# -- a disabled backend: refused under strict ---------------------------------------

def test_a_disabled_backend_refuses_under_strict():
    table = _FakeSlotTable()
    store = JobStore("slot-strict", table=table)
    store._disable_backend(RuntimeError("an earlier fail-soft error"))
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True) is False
    assert table.puts == [] and store._mem.get(SLOT) is None
    # The fail-soft default still decides from memory on the same store.
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE, now=1000.0)


def test_a_configured_table_that_cannot_be_opened_refuses_under_strict(monkeypatch):
    import boto3
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "adt-onboarding-sessions")

    def no_resource(*_args, **_kwargs):
        raise RuntimeError("NoRegionError")
    monkeypatch.setattr(boto3, "resource", no_resource)
    store = JobStore("slot-strict")
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True) is False
    assert store._mem.get(SLOT) is None


# -- a healthy table: the same conditional claim as the default ------------------------

def test_strict_on_a_healthy_table_claims_refuses_and_takes_over():
    shared: dict = {}
    task1 = JobStore("slot-strict", table=_FakeSlotTable(shared))
    task2 = JobStore("slot-strict", table=_FakeSlotTable(shared))
    assert task1.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    assert not task2.claim_slot(SLOT, _running("b", 1001.0), stale_seconds=STALE,
                                now=1001.0, strict=True)
    later = 1000.0 + STALE + 1
    assert task2.claim_slot(SLOT, _running("b", later), stale_seconds=STALE,
                            now=later, strict=True)
    assert shared[task1._key(SLOT)]["slot_owner"] == "b"


# -- touch -------------------------------------------------------------------------------

def test_strict_touch_on_an_erroring_table_is_false():
    table = _FailingAfter()
    store = JobStore("slot-strict", table=table)
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    table.broken = True
    assert store.touch_slot(SLOT, "a", now=1100.0, strict=True) is False
    # Non-strict keeps its fail-soft answer.
    assert store.touch_slot(SLOT, "a", now=1100.0) is True


def test_strict_touch_on_a_disabled_backend_is_false():
    store = JobStore("slot-strict", table=_FakeSlotTable())
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    store._disable_backend(RuntimeError("an earlier fail-soft error"))
    assert store.touch_slot(SLOT, "a", now=1100.0, strict=True) is False


def test_strict_touch_refreshes_a_held_claim_and_refuses_a_lost_one():
    shared: dict = {}
    task1 = JobStore("slot-strict", table=_FakeSlotTable(shared))
    task2 = JobStore("slot-strict", table=_FakeSlotTable(shared))
    assert task1.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    assert task1.touch_slot(SLOT, "a", now=1200.0, strict=True)
    assert shared[task1._key(SLOT)]["slot_started_at"] == 1200
    later = 1200.0 + STALE + 1
    assert task2.claim_slot(SLOT, _running("b", later), stale_seconds=STALE,
                            now=later, strict=True)
    assert task1.touch_slot(SLOT, "a", now=later + 1, strict=True) is False


# -- release -------------------------------------------------------------------------------

def test_strict_release_on_an_erroring_table_reports_false():
    table = _FailingAfter()
    store = JobStore("slot-strict", table=table)
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    table.broken = True
    assert store.release_slot(SLOT, "a", _idle("a"), strict=True) is False


def test_strict_release_on_a_disabled_backend_reports_false():
    store = JobStore("slot-strict", table=_FakeSlotTable())
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    store._disable_backend(RuntimeError("an earlier fail-soft error"))
    assert store.release_slot(SLOT, "a", _idle("a"), strict=True) is False


def test_strict_release_by_the_owner_frees_the_slot_and_never_a_newer_claim():
    shared: dict = {}
    task1 = JobStore("slot-strict", table=_FakeSlotTable(shared))
    task2 = JobStore("slot-strict", table=_FakeSlotTable(shared))
    assert task1.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    assert task1.release_slot(SLOT, "a", _idle("a"), strict=True)
    assert task2.claim_slot(SLOT, _running("b", 1001.0), stale_seconds=STALE,
                            now=1001.0, strict=True)
    assert task1.release_slot(SLOT, "a", _idle("a"), strict=True) is False
    assert shared[task1._key(SLOT)]["slot_owner"] == "b"


# -- no table configured: strict is the in-memory CAS ---------------------------------------

@pytest.mark.parametrize("make", [
    lambda: JobStore("slot-strict-mem", table=None),
    lambda: JobStore("slot-strict-mem"),       # resolved from the env: none set
])
def test_strict_without_a_table_is_the_in_memory_cas(make, monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    store = make()
    assert store.claim_slot(SLOT, _running("a", 1000.0), stale_seconds=STALE,
                            now=1000.0, strict=True)
    assert not store.claim_slot(SLOT, _running("b", 1001.0), stale_seconds=STALE,
                                now=1001.0, strict=True)
    assert store.touch_slot(SLOT, "a", now=1100.0, strict=True)
    assert not store.touch_slot(SLOT, "b", now=1100.0, strict=True)
    assert not store.release_slot(SLOT, "b", _idle("b"), strict=True)
    assert store.release_slot(SLOT, "a", _idle("a"), strict=True)
    assert store.claim_slot(SLOT, _running("b", 1200.0), stale_seconds=STALE,
                            now=1200.0, strict=True)
