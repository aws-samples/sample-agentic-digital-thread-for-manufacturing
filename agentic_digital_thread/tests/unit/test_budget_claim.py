# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Atomic budget-claim primitive (``agents/job_store.py`` — Task 1.5).

A spend cap that is shared by TWO trigger paths (a manual one-shot route and a
scheduled job, possibly on different replicas) cannot be enforced by the
surrounding ``JobStore``: that store is a fail-soft blob KV whose lock is, by
its own docstring in ``eval_runs.py``, "a NON-ATOMIC check-then-set". These
tests pin the replacement:

* the claim is ONE conditional ``UpdateItem`` — never a read-then-write (the
  fake table's ``get_item`` raises, so any read fails the test);
* two RACING claimants cannot jointly exceed the cap — asserted both over a fake conditional store with DynamoDB's
  per-item serialisation and over the in-process fallback;
* a refusal (``granted == 0``) and an UNAVAILABLE backend
  (``BudgetUnavailable``) are distinct, and cloud mode never degrades to a
  silent success;
* the local/no-table seam is honest: ``durable is False`` in the return value
  plus a stable-key ``budget_claim_memory_only`` WARNING, so it can never
  masquerade as a durable guarantee.

Fully offline: an in-memory Table double implements the conditional-write
semantics; no AWS, no network.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agents import job_store
from agents.job_store import (
    BudgetClaim,
    BudgetUnavailable,
    JobStore,
    claim_budget_slots,
    claim_budget_slots_detailed,
    utc_day_key,
)

_KIND = "audit-budget"
_DAY = "2026-08-18"


@pytest.fixture(autouse=True)
def _clean_budget_state(monkeypatch):
    """No env table by default (memory mode) and no leaked counters/dedupe."""
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    job_store.reset_budget_claims()
    yield
    job_store.reset_budget_claims()


# --------------------------------------------------------------------------- #
# Fake conditional store
# --------------------------------------------------------------------------- #

class _ConditionalCheckFailed(Exception):
    """Shape-compatible stand-in for botocore's ConditionalCheckFailedException."""

    def __init__(self, old_item: dict | None):
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        if old_item is not None:
            self.response["Item"] = old_item


class _FakeConditionalTable:
    """DynamoDB Table double honouring ``ADD claimed :n`` + ConditionExpression.

    A single lock models DynamoDB's per-item serialisation of conditional
    writes: the condition is evaluated against the last COMMITTED value. Any
    ``get_item`` is a test failure — the primitive must not read-then-write.
    """

    def __init__(self, store: dict | None = None):
        self._store = store if store is not None else {}
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    # -- the pin: no reads on this path -- #
    def get_item(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("read-then-write: get_item must not be used")

    def put_item(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("unconditional put_item must not be used")

    def update_item(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            key = kwargs["Key"]["system_id"]
            values = kwargs["ExpressionAttributeValues"]
            n, room = values[":n"], values[":room"]
            item = self._store.get(key)
            claimed = item.get("claimed") if item else None
            if claimed is not None and claimed > room:
                old = dict(item) if kwargs.get(
                    "ReturnValuesOnConditionCheckFailure") == "ALL_OLD" else None
                raise _ConditionalCheckFailed(old)
            self._store[key] = {
                "system_id": key,
                "claimed": (claimed or 0) + n,
                "ttl": values[":ttl"],
                "day_key": values[":day"],
            }
            return {}

    def claimed(self, kind: str = _KIND, day: str = _DAY) -> int:
        item = self._store.get(f"__budget__:{kind}:{day}")
        return int(item["claimed"]) if item else 0


class _BoomTable:
    """A configured backend that errors for a NON-conditional reason."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("ProvisionedThroughputExceeded")
        self.calls = 0

    def update_item(self, **kwargs):
        self.calls += 1
        raise self._exc


# --------------------------------------------------------------------------- #
# Shape of the write: ONE conditional UpdateItem, no read-then-write
# --------------------------------------------------------------------------- #

def test_claim_is_one_conditional_update_item_with_no_read():
    table = _FakeConditionalTable()

    claim = claim_budget_slots_detailed(_KIND, _DAY, 3, 10, table=table)

    assert claim == BudgetClaim(granted=3, durable=True, requested=3,
                                kind=_KIND, day_key=_DAY)
    assert len(table.calls) == 1, "the happy path must be a SINGLE UpdateItem"
    kwargs = table.calls[0]
    assert "ADD claimed :n" in kwargs["UpdateExpression"], (
        "the claim must be an atomic ADD, not a computed overwrite"
    )
    condition = kwargs["ConditionExpression"]
    assert "attribute_not_exists(claimed)" in condition
    assert "claimed <= :room" in condition
    assert kwargs["ExpressionAttributeValues"][":room"] == 10 - 3
    # TTL is set on the same write so a budget bucket reaps itself.
    assert kwargs["ExpressionAttributeValues"][":ttl"] > 0


def test_budget_item_key_cannot_collide_with_a_job_store_item():
    shared: dict = {}
    table = _FakeConditionalTable(shared)
    claim_budget_slots(_KIND, _DAY, 1, 5, table=table)

    class _PlainTable:
        def put_item(self, Item):  # noqa: N803
            shared[Item["system_id"]] = dict(Item)

        def get_item(self, Key):  # noqa: N803
            item = shared.get(Key["system_id"])
            return {"Item": item} if item is not None else {}

        def delete_item(self, Key):  # noqa: N803
            shared.pop(Key["system_id"], None)

    JobStore(_KIND, table=_PlainTable())[_DAY] = {"status": "running"}

    budget_keys = [k for k in shared if k.startswith("__budget__:")]
    job_keys = [k for k in shared if k.startswith("__job__:")]
    assert len(budget_keys) == 1 and len(job_keys) == 1
    assert shared[budget_keys[0]]["claimed"] == 1, (
        "a same-kind JobStore write must not touch the budget item"
    )


# --------------------------------------------------------------------------- #
# Exhaustion, partial grant, refusal
# --------------------------------------------------------------------------- #

def test_claims_accumulate_until_cap_then_refuse():
    table = _FakeConditionalTable()
    granted = [claim_budget_slots(_KIND, _DAY, 4, 10, table=table) for _ in range(4)]
    assert granted == [4, 4, 2, 0], (
        "third claim partially granted to the cap, fourth refused"
    )
    assert table.claimed() == 10


def test_partial_grant_is_visible_in_the_return_value():
    table = _FakeConditionalTable()
    claim_budget_slots(_KIND, _DAY, 8, 10, table=table)

    claim = claim_budget_slots_detailed(_KIND, _DAY, 5, 10, table=table)

    assert claim.granted == 2 and claim.requested == 5
    assert claim.partial is True and claim.refused is False
    assert table.claimed() == 10


def test_request_over_cap_is_clamped_not_refused():
    table = _FakeConditionalTable()
    claim = claim_budget_slots_detailed(_KIND, _DAY, 25, 10, table=table)
    assert claim.granted == 10 and claim.partial is True
    assert table.claimed() == 10


def test_exhausted_budget_refuses_without_raising():
    table = _FakeConditionalTable()
    assert claim_budget_slots(_KIND, _DAY, 10, 10, table=table) == 10
    claim = claim_budget_slots_detailed(_KIND, _DAY, 1, 10, table=table)
    assert claim.refused and claim.granted == 0
    assert claim.durable is True, "a refusal is still a durable, consulted answer"
    assert table.claimed() == 10


def test_zero_cap_refuses_immediately_without_a_write():
    table = _FakeConditionalTable()
    assert claim_budget_slots(_KIND, _DAY, 1, 0, table=table) == 0
    assert table.calls == [], "no slots can exist, so no write is attempted"


def test_conditional_failure_without_an_old_item_under_grants():
    """No ALL_OLD payload ⇒ remaining room unknown ⇒ refuse (never over-grant)."""

    class _OpaqueRefusal:
        def update_item(self, **kwargs):
            raise _ConditionalCheckFailed(None)

    assert claim_budget_slots(_KIND, _DAY, 4, 10, table=_OpaqueRefusal()) == 0


def test_kinds_and_caps_are_isolated_per_bucket():
    table = _FakeConditionalTable()
    assert claim_budget_slots("audit-budget", _DAY, 10, 10, table=table) == 10
    assert claim_budget_slots("other-budget", _DAY, 3, 10, table=table) == 3
    assert table.claimed("other-budget") == 3


def test_decimal_claimed_from_dynamodb_is_handled():
    """The boto3 resource returns numbers as ``Decimal`` — room maths must cope."""

    class _DecimalRefusal:
        def update_item(self, **kwargs):
            if kwargs["ExpressionAttributeValues"][":n"] > 3:
                raise _ConditionalCheckFailed({"claimed": Decimal("7")})
            return {}

    assert claim_budget_slots(_KIND, _DAY, 6, 10, table=_DecimalRefusal()) == 3


def test_unparseable_claimed_refuses_rather_than_guessing():
    class _JunkRefusal:
        def update_item(self, **kwargs):
            raise _ConditionalCheckFailed({"claimed": "not-a-number"})

    assert claim_budget_slots(_KIND, _DAY, 6, 10, table=_JunkRefusal()) == 0


def test_retries_are_bounded_and_end_in_a_refusal():
    """A backend that keeps shrinking the room must not spin forever."""

    class _AlwaysShrinking:
        def __init__(self):
            self.calls = 0
            self._claimed = 6

        def update_item(self, **kwargs):
            self.calls += 1
            old, self._claimed = self._claimed, self._claimed + 1
            raise _ConditionalCheckFailed({"claimed": old})

    table = _AlwaysShrinking()
    assert claim_budget_slots(_KIND, _DAY, 5, 10, table=table) == 0
    assert table.calls == job_store._MAX_CLAIM_ATTEMPTS, (
        "conditional retries must be bounded"
    )


# --------------------------------------------------------------------------- #
# UTC day key + rollover
# --------------------------------------------------------------------------- #

def test_utc_day_key_is_utc_not_local():
    # 01:30 at +05:30 is still the PREVIOUS UTC day.
    aware = datetime(2026, 8, 18, 1, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert utc_day_key(aware) == "2026-08-17"
    # 23:30 at -07:00 is already the NEXT UTC day.
    aware_west = datetime(2026, 8, 18, 23, 30, tzinfo=timezone(timedelta(hours=-7)))
    assert utc_day_key(aware_west) == "2026-08-19"
    # A naive datetime is read as UTC (never local).
    assert utc_day_key(datetime(2026, 8, 18, 0, 0)) == "2026-08-18"
    # Default is "now", in UTC.
    assert utc_day_key() == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_day_rollover_starts_a_fresh_budget_durable():
    table = _FakeConditionalTable()
    assert claim_budget_slots(_KIND, "2026-08-18", 10, 10, table=table) == 10
    assert claim_budget_slots(_KIND, "2026-08-18", 1, 10, table=table) == 0
    # The next UTC day is a different item -> full cap available again.
    assert claim_budget_slots(_KIND, "2026-08-19", 6, 10, table=table) == 6
    assert table.claimed(day="2026-08-18") == 10
    assert table.claimed(day="2026-08-19") == 6


def test_day_rollover_starts_a_fresh_budget_in_memory():
    assert claim_budget_slots(_KIND, "2026-08-18", 10, 10, table=None) == 10
    assert claim_budget_slots(_KIND, "2026-08-18", 1, 10, table=None) == 0
    assert claim_budget_slots(_KIND, "2026-08-19", 10, 10, table=None) == 10


# --------------------------------------------------------------------------- #
# Cloud mode FAILS CLOSED
# --------------------------------------------------------------------------- #

def test_backend_error_in_cloud_mode_raises_instead_of_granting():
    table = _BoomTable()
    with pytest.raises(BudgetUnavailable):
        claim_budget_slots_detailed(_KIND, _DAY, 1, 10, table=table)
    assert table.calls == 1
    # Nothing was recorded anywhere: the failure must not silently fall back to
    # the in-process counter (that is how a fail-soft no-op authorises spend).
    assert job_store._MEM_CLAIMS == {}


def test_backend_error_int_form_also_raises_never_returns_zero():
    """A caller using the count-only form must not read an error as a refusal."""
    with pytest.raises(BudgetUnavailable):
        claim_budget_slots(_KIND, _DAY, 1, 10, table=_BoomTable())


def test_backend_error_logs_stable_unavailable_key(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.job_store"):
        with pytest.raises(BudgetUnavailable):
            claim_budget_slots(_KIND, _DAY, 1, 10, table=_BoomTable())
    assert any("budget_claim_unavailable" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING)


def test_configured_table_that_cannot_be_resolved_raises(monkeypatch):
    """Env says a durable table exists but the client cannot be built ⇒ closed."""
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "adt-onboarding-sessions")
    import boto3
    monkeypatch.setattr(boto3, "resource",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no creds")))
    with pytest.raises(BudgetUnavailable):
        claim_budget_slots(_KIND, _DAY, 1, 10)
    assert job_store._MEM_CLAIMS == {}


def test_invalid_arguments_raise_value_error():
    for bad in (0, -1, 1.5, True, "2"):
        with pytest.raises(ValueError):
            claim_budget_slots(_KIND, _DAY, bad, 10, table=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        claim_budget_slots(_KIND, _DAY, 1, -1, table=None)
    with pytest.raises(ValueError):
        claim_budget_slots("", _DAY, 1, 10, table=None)
    with pytest.raises(ValueError):
        claim_budget_slots(_KIND, "", 1, 10, table=None)


# --------------------------------------------------------------------------- #
# The local seam is honest, not a fake guarantee
# --------------------------------------------------------------------------- #

def test_no_table_configured_claims_in_process_and_says_so(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.job_store"):
        claim = claim_budget_slots_detailed(_KIND, _DAY, 2, 10, table=None)

    assert claim.granted == 2
    assert claim.durable is False, (
        "the in-process fallback must NOT report a durable guarantee"
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("budget_claim_memory_only" in r.getMessage() for r in warnings), (
        "the non-durable fallback must be announced under a stable log key"
    )


def test_memory_only_warning_is_deduped_per_kind(caplog):
    with caplog.at_level(logging.DEBUG, logger="agents.job_store"):
        for _ in range(3):
            claim_budget_slots(_KIND, _DAY, 1, 10, table=None)
    warnings = [r for r in caplog.records
                if r.levelno >= logging.WARNING
                and "budget_claim_memory_only" in r.getMessage()]
    assert len(warnings) == 1, "flooding the log group is how the signal gets muted"
    debugs = [r for r in caplog.records
              if r.levelno == logging.DEBUG
              and "budget_claim_memory_only" in r.getMessage()]
    assert len(debugs) == 2, "later occurrences stay visible at DEBUG"


def test_env_signal_absent_selects_memory_mode_without_an_explicit_table():
    """The real local-dev path: nothing injected, no table env var ⇒ in-process."""
    claim = claim_budget_slots_detailed(_KIND, _DAY, 4, 10)
    assert claim.granted == 4 and claim.durable is False
    assert job_store._MEM_CLAIMS[(_KIND, _DAY)] == 4


def test_env_signal_present_selects_the_shared_table(monkeypatch):
    """``ONBOARDING_SESSIONS_TABLE`` is the same cloud-mode signal JobStore uses."""
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "adt-onboarding-sessions")
    table = _FakeConditionalTable()
    import boto3

    class _Resource:
        def Table(self, name):  # noqa: N802 (boto3 API shape)
            assert name  # the sessions table name, resolved from onboarding_session
            return table

    monkeypatch.setattr(boto3, "resource", lambda *a, **k: _Resource())

    claim = claim_budget_slots_detailed(_KIND, _DAY, 3, 10)
    assert claim.granted == 3 and claim.durable is True
    assert table.claimed() == 3
    assert job_store._MEM_CLAIMS == {}, "cloud mode must not touch the fallback"


def test_reset_budget_claims_is_a_test_seam_only():
    assert claim_budget_slots(_KIND, _DAY, 10, 10, table=None) == 10
    assert claim_budget_slots(_KIND, _DAY, 1, 10, table=None) == 0
    job_store.reset_budget_claims()
    assert claim_budget_slots(_KIND, _DAY, 10, 10, table=None) == 10


# --------------------------------------------------------------------------- #
# RACE — racing claimants
# --------------------------------------------------------------------------- #

def test_two_concurrent_claimants_cannot_jointly_exceed_the_budget():
    """Two threads, cap 10, 6 slots each: the second is trimmed to the remainder.

    The fake store serialises conditional writes exactly as DynamoDB does, so a
    check-then-set implementation would grant 12 here.
    """
    table = _FakeConditionalTable()
    results: list[int] = []
    barrier = threading.Barrier(2)

    def claimant():
        barrier.wait()
        results.append(claim_budget_slots(_KIND, _DAY, 6, 10, table=table))

    threads = [threading.Thread(target=claimant) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sum(results) == 10, f"jointly over/under-granted: {results}"
    assert sorted(results) == [4, 6]
    assert table.claimed() == 10


def test_many_concurrent_claimants_over_conditional_store_total_exactly_cap():
    """Four threads taking 1-slot bites against cap 10 ⇒ exactly 10 grants."""
    table = _FakeConditionalTable()
    granted: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def claimant():
        barrier.wait()
        for _ in range(20):  # each thread tries far more than the cap allows
            got = claim_budget_slots(_KIND, _DAY, 1, 10, table=table)
            if not got:
                return
            with lock:
                granted.append(got)

    threads = [threading.Thread(target=claimant) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sum(granted) == 10, f"total grants must equal the cap, got {granted}"
    assert table.claimed() == 10


def test_race_over_in_process_fallback_totals_exactly_cap():
    """The local seam is a weaker guarantee, but it must still be thread-safe."""
    granted: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def claimant():
        barrier.wait()
        for _ in range(20):
            got = claim_budget_slots(_KIND, _DAY, 1, 10, table=None)
            if not got:
                return
            with lock:
                granted.append(got)

    threads = [threading.Thread(target=claimant) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sum(granted) == 10
    assert job_store._MEM_CLAIMS[(_KIND, _DAY)] == 10
