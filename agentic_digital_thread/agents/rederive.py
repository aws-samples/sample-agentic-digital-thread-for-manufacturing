# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Affected-card composer + debounce/coalesce re-derivation worker (S3.2/W2.2).

This is the *reactive* half of the corrections loop (ADR 0005 C). It does two
things, both deterministic and LLM-free (ADR 0001):

1. **Composer (pure):** maps a corrected *concept* to the set of dashboard
   *cards* that must re-derive, via the static
   ``concept -> metrics_for_concept -> cards_for_metric`` index (NOT stored
   lineage, ADR 0017). ``affected_cards`` / ``affected_cards_for_concepts``.

2. **Worker (thread-based, FLAG c):** a single background daemon thread
   (mirrors ``mqtt_subscriber``'s thread model — NOT an asyncio task, so the
   synchronous ``resolve_metric``/``_build_card`` work runs off the event loop
   with no ``run_in_executor``). ``enqueue`` pushes a corrected concept; the
   loop opens a short debounce window, *unions* every concept that arrives
   inside it (merging audit), re-resolves EACH affected card exactly once via
   the shared ``dashboard._build_card`` path, and publishes ONE coalesced
   ``card_update`` batch to the ``card_events`` broker (W1.4).

The whole worker side is gated behind ``RESOLVER_REDERIVE`` (FLAG d): when the
flag is off, ``start_worker`` is a no-op so the runtime is byte-identical.

Test seams (offline-unit-friendly): the module-level ``_resolve_card``
indirection is reassignable (stub the per-card resolver), and ``_WINDOW`` is a
reassignable module global (force a burst into one window).

stdlib only (``os``, ``queue``, ``threading``, ``time``).
"""

import logging
import os
import queue
import threading
import time
from collections.abc import Iterable

from . import card_events
from .card_layer import get_card_layer
from .metric_layer import get_metric_layer

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Composer (pure, no I/O, no LLM)
# --------------------------------------------------------------------------- #
def affected_cards(concept_id: str) -> list[str]:
    """Ordered-unique list of card ids that depend on ``concept_id``.

    Union of ``card.id`` for each ``m in metrics_for_concept(concept_id)`` and
    each ``card in cards_for_metric(m)``. Pure; composes W1.2 + W1.3.
    """
    return affected_cards_for_concepts((concept_id,))


def affected_cards_for_concepts(concepts: Iterable[str]) -> list[str]:
    """Ordered-unique union of affected card ids over a set of concepts."""
    metric_layer = get_metric_layer()
    card_layer = get_card_layer()
    metrics: set[str] = set()
    for concept_id in concepts:
        metrics |= metric_layer.metrics_for_concept(concept_id)
    # Iterate cards in list_cards() order (NOT the metric SET's non-deterministic
    # iteration order) so the result is a stable, list_cards()-ordered unique list:
    # a card is affected iff any of its metric_refs is in the affected-metric set.
    seen: dict[str, None] = {}
    for card in card_layer.list_cards():
        if any(ref in metrics for ref in card.metric_refs):
            seen.setdefault(card.id, None)
    return list(seen)


# --------------------------------------------------------------------------- #
# Worker state + test seams
# --------------------------------------------------------------------------- #
_queue: "queue.Queue[tuple[str, dict | None] | tuple[object, tuple, dict | None] | object]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

# Debounce window (seconds). Reassignable module global = test seam.
_WINDOW: float = float(os.environ.get("REDERIVE_WINDOW_MS", "400")) / 1000

# Per-card resolver indirection. Bound lazily to ``dashboard._build_card`` to
# avoid an import cycle (dashboard imports card_layer/metric_resolver). Tests
# reassign this to a counting stub.
_resolve_card = None

# Sentinel pushed by ``stop_worker`` to unblock the blocking ``get``.
_SENTINEL = object()

# Tag marking a *direct card-ids* queue item so the worker can tell it apart from
# a ``(concept_id, audit)`` corrections tuple. A distinct sentinel object → it can
# never collide with a real concept-id string at ``item[0]``.
_CARDS_TAG = object()


def _enabled() -> bool:
    """True iff the ``RESOLVER_REDERIVE`` flag (FLAG d) is set (non-empty)."""
    return bool(os.environ.get("RESOLVER_REDERIVE", "").strip())


def _get_resolver():
    """Lazily bind and return the per-card resolver (``dashboard._build_card``)."""
    global _resolve_card
    if _resolve_card is None:
        from . import dashboard  # lazy import to avoid a module cycle
        _resolve_card = dashboard._build_card
    return _resolve_card


# --------------------------------------------------------------------------- #
# Enqueue + lifecycle
# --------------------------------------------------------------------------- #
def enqueue(concept_id: str, *, audit: dict | None = None) -> None:
    """Push a corrected concept onto the worker queue.

    Optionally announces an ``updating`` ripple immediately so the UI can mark
    the soon-to-refresh cards (the coalesced ``updated`` batch follows from the
    worker). Never raises if there are no affected cards.
    """
    _queue.put((concept_id, audit))
    card_ids = affected_cards(concept_id)
    if card_ids:
        card_events.publish(
            {"event": "card_update", "status": "updating", "card_ids": card_ids}
        )


def enqueue_cards(card_ids: Iterable[str], *, audit: dict | None = None) -> None:
    """Push EXPLICIT card ids onto the worker queue (data-driven trigger).

    The corrections path enqueues a *concept* (:func:`enqueue`, which derives the
    affected cards); this is the parallel producer seam for callers that already
    know the exact cards to refresh — the live-data drivers in ``card_live`` (new
    telemetry / a scheduled poll), not a mapping correction. The ids flow through
    the SAME debounce/coalesce window and the SAME ``_publish_batch`` as
    corrections — they are simply *unioned in directly* rather than derived from a
    concept, so a burst of ticks still collapses to ONE coalesced ``updated``
    batch and NO new broker or event type is introduced. Announces the same
    immediate ``updating`` ripple so the UI can mark the soon-to-refresh cards. A
    blank/empty list is a clean no-op (never enqueues, never publishes).
    """
    ids = [c for c in (card_ids or []) if c]
    if not ids:
        return
    _queue.put((_CARDS_TAG, tuple(ids), audit))
    card_events.publish(
        {"event": "card_update", "status": "updating", "card_ids": ids}
    )


def start_worker() -> None:
    """Start the daemon worker thread. Idempotent; NO-OP when flag is off."""
    global _worker
    if not _enabled():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return  # already running — idempotent
        _worker = threading.Thread(
            target=_worker_loop, daemon=True, name="rederive-worker"
        )
        _worker.start()


def stop_worker() -> None:
    """Stop the worker thread cleanly (unblocks the queue with a sentinel)."""
    global _worker
    with _worker_lock:
        if _worker is None:
            return
        _queue.put(_SENTINEL)
        _worker.join(timeout=5)
        _worker = None


# --------------------------------------------------------------------------- #
# Worker loop (runs on the background thread)
# --------------------------------------------------------------------------- #
def _worker_loop() -> None:
    """Block for work, debounce/coalesce a burst, publish ONE batch each round."""
    while True:
        first = _queue.get()
        if first is _SENTINEL:
            return
        concepts, direct_cards, audit = _drain_window(first)
        try:
            _publish_batch(concepts, direct_cards, audit)
        except Exception:  # fail-loud in logs; never kill the worker thread
            logger.exception(
                "rederive: batch publish failed for concepts=%s cards=%s",
                concepts, list(direct_cards),
            )


def _drain_window(first) -> tuple[set[str], dict, dict]:
    """Open a ``_WINDOW`` debounce window; union concepts + direct cards + audit.

    Handles BOTH queue item shapes: a ``(concept_id, audit)`` corrections tuple
    and a ``(_CARDS_TAG, card_ids, audit)`` direct-cards item. Returns the
    coalesced concept set, an ordered-unique direct-card map (``{card_id: None}``
    — dict preserves first-seen order for a deterministic batch), and the merged
    audit. A sentinel arriving mid-window is re-queued so the outer loop
    terminates on the next round.
    """
    concepts: set[str] = set()
    direct_cards: dict[str, None] = {}
    merged: dict = {}

    def _absorb(item) -> None:
        if item and item[0] is _CARDS_TAG:
            _, ids, aud = item
            for cid in ids:
                direct_cards.setdefault(cid, None)
        else:
            concept_id, aud = item
            concepts.add(concept_id)
        if aud:
            merged.update(aud)

    _absorb(first)
    deadline = time.monotonic() + _WINDOW
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            item = _queue.get(timeout=remaining)
        except queue.Empty:
            break
        if item is _SENTINEL:
            _queue.put(_SENTINEL)  # let the outer loop see it and exit
            break
        _absorb(item)
    return concepts, direct_cards, merged


def _publish_batch(concepts: set[str], direct_cards: dict, audit: dict) -> None:
    """Re-resolve each affected card ONCE and publish one coalesced batch.

    The card set is the ordered-unique union of the concept-derived cards
    (corrections path, ``affected_cards_for_concepts``) and any directly-supplied
    card ids (data-driven path). Each distinct card is resolved exactly once.
    """
    seen: dict[str, None] = {}
    for card_id in affected_cards_for_concepts(concepts):
        seen.setdefault(card_id, None)
    for card_id in direct_cards:
        seen.setdefault(card_id, None)
    card_ids = list(seen)
    if not card_ids:
        return
    resolve = _get_resolver()
    cards = [resolve(card_id) for card_id in card_ids]
    # Unified single-resolve (perf): the SAME resolved batch that drives the SSE
    # card_update ripple also warms the /api/dashboard TTL cache — so the live
    # poll (card_live) keeps the dashboard warm WITHOUT a second full _build_cards
    # scan (no doubling of backend queries). Fail-soft + lazy import to avoid a
    # module cycle; a no-op when the cache is unwarmed/disabled.
    try:
        from . import dashboard  # lazy: dashboard imports card_layer/metric_resolver
        dashboard.update_cards_cache(cards)
    except Exception:  # never let a cache-warm error kill the worker batch
        logger.warning("rederive: dashboard cache warm failed", exc_info=True)
    card_events.publish(
        {"event": "card_update", "status": "updated", "cards": cards, "audit": audit}
    )
