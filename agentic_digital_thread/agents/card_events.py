# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dedicated in-process pub/sub broker for card re-derivation events.

This is the corrections-side event bus (ADR 0005 C: re-derivation on
correction). It is intentionally a *separate* module from
``mqtt_subscriber`` — telemetry fan-out and corrections fan-out are
distinct concerns and must not be overloaded onto the same registry — but
it mirrors ``mqtt_subscriber``'s fan-out semantics exactly: a bounded
``queue.Queue`` per subscriber, registered under a single module ``_lock``,
with put-nowait fan-out and orphan-drop after a persistent run of
``queue.Full`` strikes.

Single-process validity (ADR 0005 / FLAG b): the accept route, worker,
broker, and SSE generator all share one ``uvicorn`` worker process, so an
in-process broker is sufficient. A multi-instance shared bus (Redis/SNS) is
the documented cloud-first-later deferral.

stdlib only (``queue``, ``threading``); NO awscrt / boto3.
"""

import queue
import threading

# Registry: subscriber queue -> consecutive-full strike count.
_subscribers: dict[queue.Queue, int] = {}
_lock = threading.Lock()


def subscribe() -> queue.Queue:
    """Register a new subscriber; returns a Queue that receives card events."""
    q: queue.Queue = queue.Queue(maxsize=100)
    with _lock:
        _subscribers[q] = 0
    return q


def unsubscribe(q: queue.Queue) -> None:
    """Remove a previously registered subscriber queue."""
    with _lock:
        _subscribers.pop(q, None)


def publish(event: dict) -> None:
    """Fan-out an event to every subscriber. Never raises to the caller.

    Mirrors ``mqtt_subscriber._dispatch``: under ``_lock``, ``put_nowait``
    to each subscriber; on ``queue.Full`` increment the queue's strike
    count and drop it after >100 consecutive drops (orphaned consumer).
    """
    with _lock:
        dead = []
        for q, strikes in _subscribers.items():
            try:
                q.put_nowait(event)
                _subscribers[q] = 0
            except queue.Full:
                _subscribers[q] = strikes + 1
                if _subscribers[q] > 100:  # 100 consecutive drops = orphaned
                    dead.append(q)
        for q in dead:
            del _subscribers[q]


def reset() -> None:
    """Test hook: clear all registered subscribers."""
    with _lock:
        _subscribers.clear()
