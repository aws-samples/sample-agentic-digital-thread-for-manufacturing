# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data-driven live-card drivers (flag-gated ``CARD_LIVE``, default OFF).

The declarative KPI/metric cards already live-update over the EXISTING
``/api/dashboard/stream`` SSE channel: ``card_events`` broker → ``rederive``
worker → ``dashboard.dashboard_stream`` ``event: card_update`` →
``frontend/src/views/dashboard/stream.ts`` (``card_update`` handler) →
``Cards.tsx``. That path is
driven TODAY only by mapping corrections (``RESOLVER_REDERIVE``). This module
adds the missing *new-data* trigger, generically and connector-agnostically, by
converging on the SAME primitive — ``rederive.enqueue_cards`` → the same
debounce/coalesce + ``_build_card`` re-resolve + ``card_events`` publish. No new
broker, no new SSE event type, and **no frontend change**.

Two producers, one convergence point. The **scheduled poll is the BASELINE for
EVERY connected system** (any card whose backing system resolves — SiteWise,
Athena, PostgreSQL, and any future onboarded connector); **MQTT push is an
OPTIONAL add-on** that makes genuinely-streamed sources more responsive but is
never relied upon (many environments have no IoT-Core MQTT feed — e.g. a
cross-account SiteWise read purely via API/MCP — so a push-only design would
never fire).

* **SCHEDULED POLL (baseline — ALL covered cards):** a fixed-interval job on the
  SHARED ``AsyncIOScheduler`` (``prompts.add_interval_job``) re-resolves EVERY
  card whose authority system resolves to a known connection_type (the union of
  the stream + query families — :func:`covered_cards`), EXCLUDING uncovered/
  no-source cards, every ``CARD_LIVE_POLL_SECONDS`` (default 60). This is what
  makes SiteWise (and any pull source read via API/MCP) live-update without
  depending on an MQTT feed. The scheduler runs the sync callable in its worker
  threadpool, so the (blocking) resolve never blocks the event loop.
* **PUSH (optional add-on, EXPLICIT opt-in — streamed sources only):** gated
  behind its OWN flag ``CARD_LIVE_MQTT_PUSH`` (default **false**), a daemon
  thread subscribes to the existing ``mqtt_subscriber`` fan-out and, on each
  telemetry tick, enqueues the stream-family cards for a lower-latency refresh
  than the poll cadence. Pragmatic default: refresh the stream-family card set on
  any tick and let the debounce window coalesce a burst into ONE refresh (the
  precise topic→concept→cards mapping is a later optimization behind the
  ``_cards_for_entry`` seam). Because ``MQTT_TOPIC_FILTER`` defaults to ``#``,
  that is a continuous re-resolve treadmill on a live feed — layered on top of
  the poll baseline which ALREADY covers the same cards — so it must be chosen
  deliberately. Absence of the flag ⇒ poll baseline only. If no MQTT feed exists
  it simply never fires either way.

Source classification is DERIVED dynamically from the card → metric → concept →
authority-system → ``connection_type`` chain — never from hardcoded dataset,
system-id, or model literals. All re-resolution happens on the ``rederive``
daemon thread / the scheduler threadpool, NEVER on the event loop or the SSE
generator (Deliverable 5.1).

Gate: ``CARD_LIVE`` unset ⇒ every entry point here is a no-op, so the runtime
(and, with ``RESOLVER_REDERIVE`` also off, the SSE byte-stream) is unchanged.
``CARD_LIVE`` set ⇒ the poll baseline only; the broad MQTT push add-on needs the
separate ``CARD_LIVE_MQTT_PUSH`` opt-in (default false).

stdlib only (``os``, ``queue``, ``threading``, ``logging``) + existing modules.
"""
from __future__ import annotations

import logging
import os
import queue
import threading

from . import connection_types, live_settings, mqtt_subscriber, rederive
from .card_layer import get_card_layer
from .metric_layer import get_metric_layer

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Source-family classification (generic — connection_type constants, no literals)
# --------------------------------------------------------------------------- #
# STREAMED = telemetry sources with a push signal (the mqtt_subscriber fan-out).
# SiteWise Direct / i3X are the direct time-series types; a HighByte/OT source is
# published through MQTT into SiteWise, so from a card's AUTHORITY-system view it
# is one of these SiteWise types — no separate constant is needed.
STREAM_CONNECTION_TYPES: tuple[str, ...] = (
    connection_types.SITEWISE_DIRECT,
    connection_types.SITEWISE_I3X,
)
# QUERY-BASED = pull sources with NO push signal → refreshed by scheduled poll.
QUERY_CONNECTION_TYPES: tuple[str, ...] = (
    connection_types.POSTGRESQL,
    connection_types.ATHENA,
    connection_types.S3TABLES_DUCKDB,
)


def _enabled() -> bool:
    """True iff the ``CARD_LIVE`` flag is set (non-empty). Mirrors
    ``rederive._enabled`` (the ``RESOLVER_REDERIVE`` gate)."""
    return bool(os.environ.get("CARD_LIVE", "").strip())


# The push opt-in is parsed as a true-ALLOWLIST, never a false-denylist: an
# opt-in a typo can satisfy is not an opt-in. Absent, blank, any known false
# spelling, and anything UNKNOWN all stay OFF (unknown values warn once so an
# operator typo is visible rather than silently failing safe).
_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_SPELLINGS = frozenset({"", "0", "false", "no", "off"})
_warned_push_values: set[str] = set()


def _push_enabled() -> bool:
    """True iff the broad MQTT PUSH driver was explicitly opted into.

    Separate from :func:`_enabled` and default **FALSE**. ``CARD_LIVE`` alone
    keeps the scheduled poll BASELINE (which already covers every card the push
    driver would touch); the push add-on answers ANY tick on the
    ``MQTT_TOPIC_FILTER`` fan-out — which defaults to ``#`` — by marking the
    whole stream-family card set dirty, i.e. a continuous re-resolve treadmill
    layered on top of that baseline. On the 2026-08-24 Management run a single
    task (cpu=512, one Uvicorn process) held ~99.9-100% CPU for ~45 minutes with
    ALB averages of 3-6 s; this is one of the amplifiers, so the broad
    subscription now requires a deliberate choice. Absence ⇒ safe default-off,
    so no deploy/CDK env change is needed to get the safe behavior, and setting
    it to a recognized true value restores the previous behavior exactly."""
    raw = os.environ.get("CARD_LIVE_MQTT_PUSH", "").strip().lower()
    if raw in _TRUE_SPELLINGS:
        return True
    if raw not in _FALSE_SPELLINGS and raw not in _warned_push_values:
        _warned_push_values.add(raw)
        logger.warning(
            "CARD_LIVE_MQTT_PUSH=%r is not a recognized value; treating the "
            "MQTT push add-on as OFF (recognized true values: %s)",
            raw, ", ".join(sorted(_TRUE_SPELLINGS)),
        )
    return False


def _poll_seconds() -> int:
    """Poll interval for query-family cards (``CARD_LIVE_POLL_SECONDS``, default
    60). A conservative default bounds Athena bytes-scanned / SiteWise call
    volume (Deliverable 5.3). Malformed values fall back to 60."""
    try:
        return int(os.environ.get("CARD_LIVE_POLL_SECONDS", "60"))
    except (TypeError, ValueError):
        return 60


def live_mode() -> str:
    """Effective broad-push mode for the current replica.

    The poll-only answer includes a disabled/malformed push opt-in and a push
    flag whose parent CARD_LIVE gate is off. Delegating to ``_push_enabled``
    keeps this read path on the same true allowlist as runtime behavior.
    """
    return "mqtt-push" if _enabled() and _push_enabled() else "poll-only"


# --------------------------------------------------------------------------- #
# Card → backing-system connection_type (test seam: reassignable)
# --------------------------------------------------------------------------- #
def _card_connection_types(card) -> set[str]:
    """The ``connection_type`` of every AUTHORITY system backing ``card``.

    Chain (all reused, all deterministic, no LLM): ``card.metric_refs`` →
    ``metric_layer.get_metric`` → the metric's concept closure →
    ``metric_resolver._resolve_authority`` (the authoritative system per concept)
    → ``metric_resolver._connection_type``. Any concept whose authority cannot be
    resolved (uncovered/ambiguous) is skipped — an uncovered card simply lands in
    NEITHER family (correct: no live driver touches it).

    This is the single classification seam; tests reassign it to inject a
    connection_type per card WITHOUT any hardcoded system name.
    """
    from . import metric_resolver  # lazy: avoid heavy transitive imports at load
    metric_layer = get_metric_layer()
    types: set[str] = set()
    for ref in card.metric_refs:
        try:
            metric = metric_layer.get_metric(ref)
        except KeyError:
            continue
        for concept in metric_layer._concepts_of(metric):
            try:
                src = metric_resolver._resolve_authority(concept, None)
            except Exception:  # noqa: BLE001 - uncovered/ambiguous concept: skip  # nosec B112 - best-effort loop: skip the failed item and continue
                continue
            if src and src.system_id:
                try:
                    types.add(metric_resolver._connection_type(src.system_id))
                except Exception:  # noqa: BLE001 - transient catalog miss: skip  # nosec B112 - best-effort loop: skip the failed item and continue
                    continue
    return types


def _bucket(card) -> str | None:
    """Classify a card into ``"stream"`` / ``"query"`` / ``None``.

    A card backed by ANY streamed authority system is stream-family (push is more
    responsive and would otherwise be double-covered by the poll); else if backed
    by any query-based system it is query-family; else unclassified (``None``).

    By-dimension (``group_by``) BAR cards (``quality-by-line``, ``machine-state``)
    are NEVER live-polled/pushed: they carry per-row grouped data resolved
    client-side via ``/api/metrics/resolve-by-dimension`` (and Line State via
    ``/api/dashboard/line-state``), and are EXCLUDED from the scalar snapshot
    (``dashboard._build_cards``). Publishing them as scalar ``card_update`` events
    made the frontend render a broken "Unavailable" KPI tile — so they are excluded
    at this single classifier seam (defense-in-depth behind the frontend render
    guard). ``getattr`` keeps the offline test fakes (no ``group_by`` attr) working."""
    if getattr(card, "group_by", None) is not None:
        return None
    types = _card_connection_types(card)
    if types & set(STREAM_CONNECTION_TYPES):
        return "stream"
    if types & set(QUERY_CONNECTION_TYPES):
        return "query"
    return None


def stream_cards() -> list[str]:
    """Ordered card ids whose authority system is a STREAMED source."""
    return [c.id for c in get_card_layer().list_cards() if _bucket(c) == "stream"]


def query_cards() -> list[str]:
    """Ordered card ids whose authority system is a QUERY-BASED source."""
    return [c.id for c in get_card_layer().list_cards() if _bucket(c) == "query"]


def covered_cards() -> list[str]:
    """Ordered card ids whose backing system RESOLVES to a known connection_type.

    The poll BASELINE target set: the union of the stream + query families
    (:func:`_bucket` returns non-``None``), EXCLUDING uncovered/no-source cards
    (``_bucket`` ``None`` — an authority system that cannot be resolved to any
    connection_type). Data-driven off ``connection_type`` alone (no dataset /
    system-id / model literals), so ANY onboarded system — SiteWise, Athena,
    PostgreSQL, or a future connector whose type is in the stream/query
    constants — is covered automatically. Preserves ``list_cards`` order for a
    deterministic refresh batch."""
    return [c.id for c in get_card_layer().list_cards() if _bucket(c) is not None]


# --------------------------------------------------------------------------- #
# Producer 1 — PUSH (subscribe to the mqtt_subscriber fan-out)
# --------------------------------------------------------------------------- #
_stream_thread: threading.Thread | None = None
_stream_q: "queue.Queue | None" = None
_stream_stop = threading.Event()
_stream_lock = threading.Lock()


def _cards_for_entry(entry: dict) -> list[str]:
    """Map a telemetry tick → affected card ids.

    Pragmatic default (plan Deliverable 3b): on ANY stream tick mark the whole
    stream-family card set dirty; the ``rederive`` debounce window coalesces a
    burst of ticks into ONE refresh of that set. The precise
    topic→concept→cards mapping (``parse_isa95_topic`` + a data_type→concept
    lookup + ``affected_cards_for_concepts``) is a later optimization behind this
    same seam. ``entry`` is accepted (unused today) so the precise mapping can be
    slotted in without changing the caller."""
    return stream_cards()


def _on_telemetry(entry: dict) -> None:
    """Feed one telemetry tick into the shared enqueue path.

    Opt-in gate (P0-B): unless ``CARD_LIVE_MQTT_PUSH`` is explicitly true this is
    a NO-OP — no classification, no enqueue, no rebuild. Checked HERE as well as
    at subscription time so a tick delivered by any route (a pre-existing
    subscription, a direct call) cannot restart the treadmill.

    Runtime pause gate: when live updates are PAUSED
    (:func:`live_settings.live_updates_enabled` is False) this is a NO-OP — the
    tick is dropped, so no stream-family card is re-resolved and no backend query
    / ``card_update`` publish fires while paused. Checked per tick so the toggle
    flips without a redeploy (layered on top of the ``CARD_LIVE`` boot flag)."""
    if not _push_enabled():
        return
    if not live_settings.live_updates_enabled():
        return
    card_ids = _cards_for_entry(entry)
    if card_ids:
        rederive.enqueue_cards(card_ids)


def _stream_loop(q: "queue.Queue") -> None:
    """Daemon loop: drain the mqtt fan-out and enqueue affected cards.

    Mirrors ``mqtt_subscriber``'s thread discipline; all work is off the event
    loop. Exceptions per tick are logged, never fatal."""
    while not _stream_stop.is_set():
        try:
            entry = q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            _on_telemetry(entry)
        except Exception:  # noqa: BLE001 - one bad tick must not kill the driver
            logger.exception("card_live: stream tick handling failed")


def start_stream_driver() -> None:
    """Start the push driver daemon thread. Idempotent; NO-OP unless BOTH
    ``CARD_LIVE`` and the explicit ``CARD_LIVE_MQTT_PUSH`` opt-in are set — so by
    default nothing subscribes to the broad ``MQTT_TOPIC_FILTER`` fan-out and the
    poll baseline is the only producer (see :func:`_push_enabled`)."""
    global _stream_thread, _stream_q
    if not _enabled() or not _push_enabled():
        return
    with _stream_lock:
        if _stream_thread is not None and _stream_thread.is_alive():
            return  # already running — idempotent
        _stream_q = mqtt_subscriber.subscribe()
        _stream_stop.clear()
        _stream_thread = threading.Thread(
            target=_stream_loop, args=(_stream_q,), daemon=True, name="card-live-stream"
        )
        _stream_thread.start()
        logger.info("card_live: push (stream) driver started")


def stop_stream_driver() -> None:
    """Stop the push driver and unsubscribe from the mqtt fan-out. Idempotent."""
    global _stream_thread, _stream_q
    with _stream_lock:
        _stream_stop.set()
        if _stream_q is not None:
            mqtt_subscriber.unsubscribe(_stream_q)
            _stream_q = None
        if _stream_thread is not None:
            _stream_thread.join(timeout=5)
            _stream_thread = None


# --------------------------------------------------------------------------- #
# Producer 2 — SCHEDULED POLL (shared AsyncIOScheduler via prompts.add_interval_job)
# The poll is the BASELINE for EVERY connected system (all covered-backing cards),
# NOT just query/pull ones — so SiteWise (and any source read via API/MCP without
# an MQTT feed) live-updates here. MQTT push (Producer 1) only ADDS lower-latency
# refresh for genuinely-streamed cards on top of this baseline.
# --------------------------------------------------------------------------- #
_POLL_JOB_ID = "card-live-poll"


def _poll_covered_cards() -> None:
    """Enqueue EVERY covered-backing card for a refresh (one scheduled tick).

    Targets :func:`covered_cards` — the union of the stream + query families,
    excluding uncovered/no-source cards — so SiteWise, Athena, PostgreSQL, and
    any future onboarded connector all refresh on the poll baseline (no reliance
    on an MQTT feed). A plain sync callable — the scheduler runs it in its worker
    threadpool, so the blocking classification/enqueue never blocks the event
    loop. The ids flow through the shared ``rederive`` debounce/coalesce + the
    ``_build_card`` re-resolve + ``card_events`` publish (no new broker / event
    type). Empty target set (no covered cards) is a clean no-op.

    Runtime pause gate: when live updates are PAUSED
    (:func:`live_settings.live_updates_enabled` is False) this tick is a NO-OP —
    no card is re-resolved, no cache is warmed, and no ``card_update`` is
    published, so the recurring Athena/SiteWise backend queries stop accruing
    cost. Checked EACH tick so the toggle flips at runtime WITHOUT a redeploy; the
    job stays registered (``CARD_LIVE`` wired it at boot) and resumes full
    behavior the moment the toggle is turned back on."""
    if not live_settings.live_updates_enabled():
        return
    card_ids = covered_cards()
    if card_ids:
        rederive.enqueue_cards(card_ids)


def register_poll_job() -> None:
    """Register the fixed-interval poll job on the shared scheduler. NO-OP when
    flag off. Idempotent — ``add_interval_job`` replaces any existing job id."""
    if not _enabled():
        return
    from . import prompts  # lazy: prompts owns the shared AsyncIOScheduler
    prompts.add_interval_job(_POLL_JOB_ID, _poll_covered_cards, _poll_seconds())
    logger.info("card_live: poll (baseline) job registered (every %ss)", _poll_seconds())
