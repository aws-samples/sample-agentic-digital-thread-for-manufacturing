# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the ``CARD_LIVE`` live-card data drivers (``card_live``).

No AWS, no network, no SSE server. Every seam is stubbed: the card layer, the
card→connection_type classifier, the mqtt fan-out, the shared scheduler, and
``rederive.enqueue_cards``. The classifier is asserted purely on INJECTED
connection types — never a hardcoded system name — and the flag-off paths are
asserted to be no-ops (byte-identical runtime). The dashboard-stream cases
mirror ``test_dashboard_cards`` case 8/9 to prove the CARD_LIVE gate broadens
the SSE drain while keeping the BOTH-flags-off stream byte-identical.
"""
from __future__ import annotations

import asyncio
import json
import queue
import time
from unittest.mock import Mock

import pytest

import agents.card_events as ce
import agents.card_live as card_live
import agents.connection_types as ct
import agents.dashboard as dashboard


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeCard:
    def __init__(self, card_id: str, metric_refs=()):
        self.id = card_id
        self.metric_refs = tuple(metric_refs)


class _FakeLayer:
    def __init__(self, cards):
        self._cards = list(cards)

    def list_cards(self):
        return list(self._cards)


# --------------------------------------------------------------------------- #
# Classifier — buckets by INJECTED connection_type (no system-name literals)
# --------------------------------------------------------------------------- #
def test_classifier_buckets_stream_vs_query(monkeypatch):
    cards = [
        _FakeCard("c-sitewise"),
        _FakeCard("c-i3x"),
        _FakeCard("c-athena"),
        _FakeCard("c-postgres"),
        _FakeCard("c-other"),
    ]
    monkeypatch.setattr(card_live, "get_card_layer", lambda: _FakeLayer(cards))
    conn = {
        "c-sitewise": {ct.SITEWISE_DIRECT},
        "c-i3x": {ct.SITEWISE_I3X},
        "c-athena": {ct.ATHENA},
        "c-postgres": {ct.POSTGRESQL},
        "c-other": {ct.MCP_SERVER},          # neither family
    }
    monkeypatch.setattr(card_live, "_card_connection_types", lambda card: conn[card.id])

    assert card_live.stream_cards() == ["c-sitewise", "c-i3x"]
    assert card_live.query_cards() == ["c-athena", "c-postgres"]
    # covered_cards() = the poll BASELINE target = union of stream+query families
    # (SiteWise, i3X, Athena, Postgres) in list order, EXCLUDING the uncovered
    # c-other. This is what makes the SiteWise cards get POLLED (Fix 1) instead of
    # depending on an MQTT push that never fires.
    assert card_live.covered_cards() == ["c-sitewise", "c-i3x", "c-athena", "c-postgres"]


def test_classifier_uncovered_card_in_neither_family(monkeypatch):
    cards = [_FakeCard("c-uncovered")]
    monkeypatch.setattr(card_live, "get_card_layer", lambda: _FakeLayer(cards))
    # An uncovered/ambiguous card resolves to NO connection types.
    monkeypatch.setattr(card_live, "_card_connection_types", lambda card: set())
    assert card_live.stream_cards() == []
    assert card_live.query_cards() == []
    # ... and is EXCLUDED from the poll baseline (no backing system resolves).
    assert card_live.covered_cards() == []


def test_covered_cards_polls_sitewise_and_excludes_uncovered(monkeypatch):
    # Fix 1 regression guard: a SiteWise-backed card (previously mis-bucketed to
    # PUSH-only, which never fires without an MQTT feed) IS now in the poll
    # baseline, while an uncovered/no-source card is NOT — purely data-driven off
    # the injected connection_type, no system-name literals.
    cards = [_FakeCard("c-sitewise"), _FakeCard("c-uncovered")]
    monkeypatch.setattr(card_live, "get_card_layer", lambda: _FakeLayer(cards))
    conn = {"c-sitewise": {ct.SITEWISE_DIRECT}, "c-uncovered": set()}
    monkeypatch.setattr(card_live, "_card_connection_types", lambda card: conn[card.id])
    assert card_live.covered_cards() == ["c-sitewise"]


def test_classifier_mixed_prefers_stream(monkeypatch):
    cards = [_FakeCard("c-mixed")]
    monkeypatch.setattr(card_live, "get_card_layer", lambda: _FakeLayer(cards))
    # Backed by BOTH a streamed and a query system → stream family wins.
    monkeypatch.setattr(
        card_live, "_card_connection_types",
        lambda card: {ct.SITEWISE_DIRECT, ct.ATHENA},
    )
    assert card_live.stream_cards() == ["c-mixed"]
    assert card_live.query_cards() == []


def test_group_by_bar_cards_excluded_from_all_families(monkeypatch):
    # Regression guard (yellow "Unavailable" bar-card bug): a by-dimension BAR card
    # carrying a `group_by` (e.g. `quality-by-line`, `machine-state`) must NEVER be
    # live-polled/pushed/covered — it renders via /api/metrics/resolve-by-dimension
    # and /api/dashboard/line-state, and is excluded from the scalar snapshot. Even
    # though its backing system resolves to a known connection_type, _bucket returns
    # None purely because group_by is set (single-seam exclusion). A sibling scalar
    # card over the SAME connection type stays covered.
    class _BarCard(_FakeCard):
        def __init__(self, card_id):
            super().__init__(card_id)
            self.group_by = "production-line"

    cards = [_BarCard("quality-by-line"), _FakeCard("throughput")]
    monkeypatch.setattr(card_live, "get_card_layer", lambda: _FakeLayer(cards))
    # Both would classify as query-family by connection type alone.
    monkeypatch.setattr(card_live, "_card_connection_types",
                        lambda card: {ct.POSTGRESQL})

    assert card_live._bucket(cards[0]) is None      # excluded by group_by
    assert card_live._bucket(cards[1]) == "query"   # scalar sibling still covered
    assert card_live.covered_cards() == ["throughput"]
    assert card_live.stream_cards() == []
    assert card_live.query_cards() == ["throughput"]


# --------------------------------------------------------------------------- #
# Push path — a telemetry tick maps to the stream-family cards → enqueue_cards
# --------------------------------------------------------------------------- #
def test_push_path_enqueues_stream_cards(monkeypatch):
    # The broad MQTT push add-on is opt-in (CARD_LIVE_MQTT_PUSH, default false —
    # see test_card_live_push_optin.py); with it set, behavior is unchanged.
    monkeypatch.setenv("CARD_LIVE_MQTT_PUSH", "1")
    got: list[list[str]] = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    monkeypatch.setattr(card_live, "stream_cards", lambda: ["s1", "s2"])

    card_live._on_telemetry({"topic": "a/b/c/d/e/Temperature", "payload": 1})

    assert got == [["s1", "s2"]]


def test_push_path_no_stream_cards_is_noop(monkeypatch):
    got: list = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    monkeypatch.setattr(card_live, "stream_cards", lambda: [])
    card_live._on_telemetry({"topic": "x", "payload": 1})
    assert got == []


def test_stream_driver_on_tick_enqueues(monkeypatch):
    monkeypatch.setenv("CARD_LIVE", "1")
    monkeypatch.setenv("CARD_LIVE_MQTT_PUSH", "1")  # explicit push opt-in
    fake_q: queue.Queue = queue.Queue()
    monkeypatch.setattr(card_live.mqtt_subscriber, "subscribe", lambda: fake_q)
    unsub: dict = {}
    monkeypatch.setattr(card_live.mqtt_subscriber, "unsubscribe",
                        lambda q: unsub.setdefault("q", q))
    monkeypatch.setattr(card_live, "stream_cards", lambda: ["s1"])
    got: list[list[str]] = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    # Fresh driver state.
    monkeypatch.setattr(card_live, "_stream_thread", None)
    monkeypatch.setattr(card_live, "_stream_q", None)
    card_live._stream_stop.clear()

    try:
        card_live.start_stream_driver()
        fake_q.put({"topic": "t", "payload": 1})
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not got:
            time.sleep(0.02)
    finally:
        card_live.stop_stream_driver()

    assert got and got[0] == ["s1"]
    assert unsub.get("q") is fake_q


# --------------------------------------------------------------------------- #
# Scheduled poll path — enqueue ALL covered-backing cards (the baseline)
# --------------------------------------------------------------------------- #
def test_poll_path_enqueues_covered_cards(monkeypatch):
    got: list[list[str]] = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    # The poll targets covered_cards() — the union of stream+query families
    # (incl. SiteWise), NOT just the query family.
    monkeypatch.setattr(card_live, "covered_cards", lambda: ["c-sitewise", "q1", "q2"])

    card_live._poll_covered_cards()

    assert got == [["c-sitewise", "q1", "q2"]]


def test_poll_path_no_covered_cards_is_noop(monkeypatch):
    got: list = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    monkeypatch.setattr(card_live, "covered_cards", lambda: [])
    card_live._poll_covered_cards()
    assert got == []


# --------------------------------------------------------------------------- #
# Runtime pause toggle — live_updates OFF ⇒ background producers NO-OP (no
# backend resolve / cache warm / card_update publish), independent of CARD_LIVE
# (which is checked only at boot for job/driver registration).
# --------------------------------------------------------------------------- #
def test_poll_noop_when_live_updates_paused(monkeypatch):
    got: list = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    # covered_cards() would return a non-empty batch — but the pause gate must
    # short-circuit BEFORE any classification/enqueue (no backend query fires).
    def _boom():
        raise AssertionError("covered_cards must not be consulted while paused")
    monkeypatch.setattr(card_live, "covered_cards", _boom)
    monkeypatch.setattr(card_live.live_settings, "live_updates_enabled", lambda: False)

    card_live._poll_covered_cards()

    assert got == []  # nothing enqueued while paused


def test_poll_runs_when_live_updates_enabled(monkeypatch):
    got: list[list[str]] = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    monkeypatch.setattr(card_live, "covered_cards", lambda: ["c-sitewise", "q1"])
    monkeypatch.setattr(card_live.live_settings, "live_updates_enabled", lambda: True)

    card_live._poll_covered_cards()

    assert got == [["c-sitewise", "q1"]]


def test_telemetry_noop_when_live_updates_paused(monkeypatch):
    got: list = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    def _boom():
        raise AssertionError("stream_cards must not be consulted while paused")
    monkeypatch.setattr(card_live, "stream_cards", _boom)
    monkeypatch.setattr(card_live.live_settings, "live_updates_enabled", lambda: False)

    card_live._on_telemetry({"topic": "a/b", "payload": 1})

    assert got == []  # no push-driven refresh while paused


def test_telemetry_runs_when_live_updates_enabled(monkeypatch):
    monkeypatch.setenv("CARD_LIVE_MQTT_PUSH", "1")  # explicit push opt-in
    got: list[list[str]] = []
    monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                        lambda ids, **kw: got.append(list(ids)))
    monkeypatch.setattr(card_live, "stream_cards", lambda: ["s1"])
    monkeypatch.setattr(card_live.live_settings, "live_updates_enabled", lambda: True)

    card_live._on_telemetry({"topic": "a/b", "payload": 1})

    assert got == [["s1"]]


def test_register_poll_job_uses_shared_scheduler(monkeypatch):
    monkeypatch.setenv("CARD_LIVE", "1")
    monkeypatch.setenv("CARD_LIVE_POLL_SECONDS", "30")
    import agents.prompts as prompts
    rec: dict = {}
    monkeypatch.setattr(prompts, "add_interval_job",
                        lambda job_id, fn, seconds: rec.update(
                            job_id=job_id, fn=fn, seconds=seconds))

    card_live.register_poll_job()

    assert rec["job_id"] == "card-live-poll"
    assert rec["seconds"] == 30
    assert rec["fn"] is card_live._poll_covered_cards


def test_poll_seconds_default_and_malformed(monkeypatch):
    monkeypatch.delenv("CARD_LIVE_POLL_SECONDS", raising=False)
    assert card_live._poll_seconds() == 60
    monkeypatch.setenv("CARD_LIVE_POLL_SECONDS", "not-a-number")
    assert card_live._poll_seconds() == 60


# --------------------------------------------------------------------------- #
# Flag OFF ⇒ every driver entry point is a no-op
# --------------------------------------------------------------------------- #
def test_flag_off_start_stream_driver_noop(monkeypatch):
    monkeypatch.delenv("CARD_LIVE", raising=False)
    monkeypatch.setattr(card_live, "_stream_thread", None)

    def _boom():
        raise AssertionError("must not subscribe when CARD_LIVE off")

    monkeypatch.setattr(card_live.mqtt_subscriber, "subscribe", _boom)
    card_live.start_stream_driver()
    assert card_live._stream_thread is None


def test_flag_off_register_poll_job_noop(monkeypatch):
    monkeypatch.delenv("CARD_LIVE", raising=False)
    import agents.prompts as prompts

    def _boom(*a, **k):
        raise AssertionError("must not register a job when CARD_LIVE off")

    monkeypatch.setattr(prompts, "add_interval_job", _boom)
    card_live.register_poll_job()  # no exception ⇒ no-op


# --------------------------------------------------------------------------- #
# dashboard_stream gate — CARD_LIVE broadens the drain; BOTH-off is byte-identical
# --------------------------------------------------------------------------- #
def test_dashboard_stream_emits_card_update_when_card_live_on(monkeypatch):
    # CARD_LIVE on WITHOUT RESOLVER_REDERIVE ⇒ the SSE generator still subscribes
    # to card_events and emits the SAME event:card_update frame.
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    monkeypatch.setenv("CARD_LIVE", "1")
    ce.reset()

    fake_q: queue.Queue = queue.Queue()
    fake_q.put_nowait({"topic": "adt/test", "payload": "x"})
    monkeypatch.setattr(dashboard.mqtt_subscriber, "subscribe", lambda: fake_q)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "unsubscribe", lambda q: None)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "get_stats", lambda: {})

    async def drive() -> list[str]:
        resp = await dashboard.dashboard_stream(plant="all")
        it = resp.body_iterator
        frames: list[str] = [await it.__anext__()]  # first: event: dashboard
        ce.publish({
            "event": "card_update", "status": "updated",
            "cards": [{"card_id": "throughput", "metrics": []}],
        })
        for _ in range(6):
            frames.append(await it.__anext__())
            if any(f.startswith("event: card_update") for f in frames):
                break
        await it.aclose()
        return frames

    frames = asyncio.run(drive())
    ce.reset()

    card_updates = [f for f in frames if f.startswith("event: card_update")]
    assert card_updates, f"no card_update frame emitted; got {frames!r}"
    payload = json.loads(card_updates[0].split("data: ", 1)[1].strip())
    assert payload["status"] == "updated"
    assert payload["cards"][0]["card_id"] == "throughput"


def test_dashboard_stream_byte_identical_when_both_flags_off(monkeypatch):
    # HARD INVARIANT: with BOTH RESOLVER_REDERIVE and CARD_LIVE off, the generator
    # never subscribes to card_events and never emits a card_update frame — the
    # SSE byte-stream is identical to today.
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    monkeypatch.delenv("CARD_LIVE", raising=False)

    subscribe_spy = Mock(name="card_events.subscribe")
    monkeypatch.setattr(dashboard.card_events, "subscribe", subscribe_spy)

    fake_q: queue.Queue = queue.Queue()
    fake_q.put_nowait({"topic": "adt/test", "payload": "x"})
    monkeypatch.setattr(dashboard.mqtt_subscriber, "subscribe", lambda: fake_q)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "unsubscribe", lambda q: None)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "get_stats", lambda: {})

    async def drive() -> list[str]:
        resp = await dashboard.dashboard_stream(plant="all")
        it = resp.body_iterator
        frames = [await it.__anext__()]            # dashboard frame
        frames.append(await it.__anext__())        # mqtt frame
        await it.aclose()
        return frames

    frames = asyncio.run(drive())

    subscribe_spy.assert_not_called()
    assert not any(f.startswith("event: card_update") for f in frames)
