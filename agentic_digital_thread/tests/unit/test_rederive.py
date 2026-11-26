# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the affected-card composer + debounce/coalesce worker
(S3.2 / W2.2).

No AWS, no SSE, no network. The composer is pure (real metric/card layers). The
worker is driven deterministically: ``RESOLVER_REDERIVE`` is set on, the debounce
``_WINDOW`` is shrunk via the module seam, the per-card resolver ``_resolve_card``
is replaced by a counting stub, and the coalesced batch is observed on a
subscribed ``card_events`` queue. The worker thread is always stopped in teardown.
"""
from __future__ import annotations

import queue
import time

import pytest

import agents.card_events as ce
import agents.rederive as rederive


# --------------------------------------------------------------------------- #
# Composer (pure, no flag, no worker)
# --------------------------------------------------------------------------- #
def test_affected_cards_oee_input_concept():
    # `quality-metric` feeds quality-good-count, which is used by oee-quality
    # (sub-reading of oee gauge). `actual-quantity` is used by oee-quality AND
    # (directly) by the output-by-line bar. `quality-yield-rate` is used by
    # quality-by-line bar AND (as oee-quality's prefer_stored dependency) by the
    # oee gauge. `availability` feeds only the oee gauge.
    assert rederive.affected_cards("quality-metric") == ["oee"]
    assert rederive.affected_cards("actual-quantity") == ["oee", "output-by-line"]
    assert rederive.affected_cards("quality-yield-rate") == ["oee", "quality-by-line"]
    assert rederive.affected_cards("availability") == ["oee"]


def test_affected_cards_mro_inventory():
    assert rederive.affected_cards("mro-inventory") == ["spare-parts-on-hand"]


def test_affected_cards_unknown_concept_empty():
    assert rederive.affected_cards("nope") == []


def test_affected_cards_for_concepts_union_dedups():
    # The OEE-input concepts availability/performance/quality-metric/actual-quantity
    # all affect the oee gauge; actual-quantity ALSO affects the output-by-line bar;
    # performance ALSO affects the performance gauge (its prefer_stored source).
    # Adding quality-yield-rate adds quality-by-line.
    result = rederive.affected_cards_for_concepts(
        ["availability", "performance", "quality-metric", "actual-quantity", "quality-yield-rate"]
    )
    assert result == ["oee", "quality-by-line", "output-by-line", "performance"]


def test_affected_cards_for_concepts_mixed():
    result = rederive.affected_cards_for_concepts(["mro-inventory", "availability"])
    assert set(result) == {"spare-parts-on-hand", "oee"}
    assert len(result) == 2  # ordered-unique, no dup


# --------------------------------------------------------------------------- #
# Worker fixture — deterministic, always torn down
# --------------------------------------------------------------------------- #
@pytest.fixture
def worker_env(monkeypatch):
    """Reset broker + worker state, shrink the debounce window, install a
    counting per-card resolver, and guarantee the worker thread is stopped."""
    ce.reset()
    # Fresh work queue (no leftover items/sentinels between tests).
    monkeypatch.setattr(rederive, "_queue", queue.Queue())
    monkeypatch.setattr(rederive, "_worker", None)
    monkeypatch.setattr(rederive, "_WINDOW", 0.2)

    calls: dict[str, int] = {}

    def _counting_resolve(card_id: str) -> dict:
        calls[card_id] = calls.get(card_id, 0) + 1
        return {"card_id": card_id, "title": card_id, "viz_type": "kpi",
                "group_by": None, "metrics": []}

    monkeypatch.setattr(rederive, "_resolve_card", _counting_resolve)

    try:
        yield calls
    finally:
        rederive.stop_worker()
        ce.reset()


def _drain_until_updated(q: queue.Queue, timeout: float = 3.0) -> list[dict]:
    """Collect broker events until the coalesced ``updated`` batch arrives;
    then briefly confirm no SECOND batch follows."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev = q.get(timeout=deadline - time.monotonic())
        except queue.Empty:
            break
        events.append(ev)
        if ev.get("status") == "updated":
            try:  # grace window to catch an (unexpected) second batch
                events.append(q.get(timeout=0.3))
            except queue.Empty:
                pass
            break
    return events


# --------------------------------------------------------------------------- #
# Debounce / coalesce — a burst within one window → ONE batch
# --------------------------------------------------------------------------- #
def test_burst_coalesces_into_one_batch(monkeypatch, worker_env):
    calls = worker_env
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")

    sub = ce.subscribe()

    # Enqueue concepts that union to both oee gauge AND quality-by-line bar:
    # - availability + performance affect oee
    # - actual-quantity affects oee (via oee-quality)
    # - quality-yield-rate affects quality-by-line bar
    rederive.enqueue("availability")
    rederive.enqueue("performance")
    rederive.enqueue("actual-quantity")
    rederive.enqueue("quality-yield-rate")

    rederive.start_worker()

    events = _drain_until_updated(sub)

    updated = [e for e in events if e.get("status") == "updated"]
    # Exactly ONE coalesced 'updated' batch.
    assert len(updated) == 1
    batch = updated[0]
    assert batch["event"] == "card_update"
    # Union of the four concepts → the oee gauge, the quality-by-line bar, the
    # output-by-line bar (actual-quantity feeds it directly), and the
    # performance gauge (the `performance` concept is its prefer_stored source).
    assert [c["card_id"] for c in batch["cards"]] == [
        "oee", "quality-by-line", "output-by-line", "performance"]
    # The per-card resolver is called exactly once per distinct affected card.
    assert calls == {"oee": 1, "quality-by-line": 1, "output-by-line": 1,
                     "performance": 1}


def test_burst_emits_updating_ripples_before_batch(monkeypatch, worker_env):
    # enqueue() announces an immediate 'updating' ripple per call; the worker
    # then publishes the single coalesced 'updated' batch.
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    sub = ce.subscribe()

    rederive.enqueue("availability")
    rederive.enqueue("performance")
    rederive.start_worker()

    events = _drain_until_updated(sub)
    statuses = [e.get("status") for e in events]
    assert statuses.count("updating") >= 1     # at least one ripple
    assert statuses.count("updated") == 1       # one coalesced batch


# --------------------------------------------------------------------------- #
# Flag-off → start_worker is a no-op (no thread, no publish)
# --------------------------------------------------------------------------- #
def test_start_worker_noop_when_flag_off(monkeypatch, worker_env):
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    sub = ce.subscribe()

    rederive.start_worker()
    assert rederive._worker is None  # no thread started

    # Even if something is enqueued, no worker drains it → no 'updated' batch.
    rederive.enqueue("availability")  # publishes only the synchronous 'updating'
    time.sleep(0.3)
    drained = []
    try:
        while True:
            drained.append(sub.get_nowait())
    except queue.Empty:
        pass
    assert all(e.get("status") != "updated" for e in drained)


def test_start_worker_idempotent_when_flag_on(monkeypatch, worker_env):
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    rederive.start_worker()
    first = rederive._worker
    rederive.start_worker()  # idempotent — same live thread, not a second one
    assert rederive._worker is first
    assert first is not None and first.is_alive()


# --------------------------------------------------------------------------- #
# enqueue_cards — data-driven producer seam (explicit card ids, same path)
# --------------------------------------------------------------------------- #
def test_enqueue_cards_publishes_one_updated_batch(monkeypatch, worker_env):
    calls = worker_env
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    sub = ce.subscribe()

    # Explicit card ids (NOT concept-derived) land in one debounce window.
    rederive.enqueue_cards(["c1", "c2"])
    rederive.start_worker()

    events = _drain_until_updated(sub)
    updated = [e for e in events if e.get("status") == "updated"]
    assert len(updated) == 1
    batch = updated[0]
    assert batch["event"] == "card_update"
    assert [c["card_id"] for c in batch["cards"]] == ["c1", "c2"]
    # Each distinct card resolved exactly once.
    assert calls == {"c1": 1, "c2": 1}


def test_enqueue_cards_burst_coalesces_into_one_batch(monkeypatch, worker_env):
    calls = worker_env
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    sub = ce.subscribe()

    # A burst of enqueue_cards inside one window coalesces to a SINGLE batch,
    # ordered-unique across the union (c1 seen twice ⇒ resolved once).
    rederive.enqueue_cards(["c1", "c2"])
    rederive.enqueue_cards(["c2", "c3"])
    rederive.enqueue_cards(["c1"])
    rederive.start_worker()

    events = _drain_until_updated(sub)
    updated = [e for e in events if e.get("status") == "updated"]
    assert len(updated) == 1
    assert [c["card_id"] for c in updated[0]["cards"]] == ["c1", "c2", "c3"]
    assert calls == {"c1": 1, "c2": 1, "c3": 1}


def test_enqueue_cards_emits_updating_ripple(monkeypatch, worker_env):
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    sub = ce.subscribe()

    rederive.enqueue_cards(["c1", "c2"])
    rederive.start_worker()

    events = _drain_until_updated(sub)
    updating = [e for e in events if e.get("status") == "updating"]
    assert updating and updating[0]["card_ids"] == ["c1", "c2"]


def test_enqueue_cards_empty_is_noop(monkeypatch, worker_env):
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    sub = ce.subscribe()

    rederive.enqueue_cards([])       # blank list
    rederive.enqueue_cards(None)     # explicit None
    rederive.enqueue_cards(["", None])  # only falsy ids

    # Nothing was published (no updating ripple) and nothing enqueued.
    time.sleep(0.1)
    drained = []
    try:
        while True:
            drained.append(sub.get_nowait())
    except queue.Empty:
        pass
    assert drained == []


def test_enqueue_cards_unions_with_concept_path(monkeypatch, worker_env):
    calls = worker_env
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    sub = ce.subscribe()

    # Concept-derived cards (mro-inventory → spare-parts-on-hand) AND an explicit
    # card id coalesce into ONE batch (ordered-unique union).
    rederive.enqueue("mro-inventory")
    rederive.enqueue_cards(["c-extra"])
    rederive.start_worker()

    events = _drain_until_updated(sub)
    updated = [e for e in events if e.get("status") == "updated"]
    assert len(updated) == 1
    ids = [c["card_id"] for c in updated[0]["cards"]]
    assert set(ids) == {"spare-parts-on-hand", "c-extra"}
    assert calls == {"spare-parts-on-hand": 1, "c-extra": 1}
