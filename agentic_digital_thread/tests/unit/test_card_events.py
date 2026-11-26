# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the dedicated ``card_events`` broker (S3.2 / W1.4).

No AWS, no awscrt, no network — the broker is stdlib ``queue``/``threading`` only.
Mirrors ``mqtt_subscriber``'s fan-out semantics: bounded per-subscriber queues,
put-nowait fan-out under a single lock, orphan-drop after a persistent run of
``queue.Full`` strikes, never raising to the caller. ``ce.reset()`` clears the
module registry between tests (autouse fixture).
"""
from __future__ import annotations

import queue
import threading

import pytest

import agents.card_events as ce


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the broker registry before and after each test (deterministic)."""
    ce.reset()
    yield
    ce.reset()


# --------------------------------------------------------------------------- #
# Fan-out: subscribe / publish / unsubscribe
# --------------------------------------------------------------------------- #
def test_subscribe_returns_queue():
    q = ce.subscribe()
    assert isinstance(q, queue.Queue)


def test_publish_fans_out_to_all_subscribers():
    a = ce.subscribe()
    b = ce.subscribe()
    event = {"event": "card_update", "x": 1}

    ce.publish(event)

    assert a.get_nowait() == event
    assert b.get_nowait() == event
    # No extra deliveries.
    assert a.empty()
    assert b.empty()


def test_unsubscribe_stops_delivery():
    a = ce.subscribe()
    b = ce.subscribe()

    ce.unsubscribe(a)
    ce.publish({"y": 2})

    # `a` is removed → no delivery; `b` still receives.
    assert a.empty()
    assert b.get_nowait() == {"y": 2}


def test_unsubscribe_unknown_queue_is_noop():
    # Removing a queue that was never registered must not raise.
    stray: queue.Queue = queue.Queue()
    ce.unsubscribe(stray)  # no exception


def test_reset_clears_all_subscribers():
    a = ce.subscribe()
    ce.subscribe()
    ce.reset()
    # After reset, publishing reaches nobody (the previous queues are orphaned).
    ce.publish({"z": 3})
    assert a.empty()


# --------------------------------------------------------------------------- #
# Orphan-drop: a persistently Full subscriber is dropped, never raising
# --------------------------------------------------------------------------- #
def test_publish_to_full_queue_never_raises_and_drops_orphan():
    q = ce.subscribe()
    # Fill the bounded queue to capacity (maxsize=100) directly.
    for i in range(100):
        q.put_nowait(i)
    assert q.full()

    # Now every publish hits queue.Full. The broker increments a strike count
    # and drops the queue after >100 consecutive strikes — without ever raising.
    for _ in range(101):
        ce.publish({"event": "card_update"})  # must not raise

    # After >100 consecutive Full strikes the orphan is removed from the registry.
    assert q not in ce._subscribers
    assert len(ce._subscribers) == 0

    # A subsequent publish reaches nobody and still does not raise.
    ce.publish({"event": "card_update"})


def test_full_strike_resets_on_successful_delivery():
    q = ce.subscribe()
    for i in range(100):
        q.put_nowait(i)

    # A few Full strikes accumulate but stay under the drop threshold.
    for _ in range(5):
        ce.publish({"event": "card_update"})
    assert ce._subscribers.get(q) == 5

    # Drain one slot so the next publish succeeds → strike count resets to 0.
    q.get_nowait()
    ce.publish({"event": "card_update"})
    assert ce._subscribers.get(q) == 0


# --------------------------------------------------------------------------- #
# Thread-safety: concurrent subscribe + publish (no corruption / exception)
# --------------------------------------------------------------------------- #
def test_concurrent_subscribe_and_publish_is_safe():
    errors: list[Exception] = []
    delivered = ce.subscribe()  # a stable subscriber that should receive events

    def subscriber_churn():
        try:
            for _ in range(200):
                q = ce.subscribe()
                ce.unsubscribe(q)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def publisher():
        try:
            for i in range(200):
                ce.publish({"event": "card_update", "i": i})
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    t1 = threading.Thread(target=subscriber_churn)
    t2 = threading.Thread(target=publisher)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent access raised: {errors}"
    assert not t1.is_alive() and not t2.is_alive()
    # The stable subscriber received at least one event (fan-out worked under
    # concurrency); exact count is non-deterministic by design.
    assert delivered.qsize() >= 1
