# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The broad MQTT push driver needs its OWN explicit opt-in (P0-B).

Incident evidence (2026-08-24 Management full-e2e): one app task at cpu=512 /
memory=1024 with a single Uvicorn process pinned ~99.9-100% CPU for ~45 minutes
while memory sat at ~43% — an application WORKLOAD amplification, not a
selector problem. ``MQTT_TOPIC_FILTER`` defaults to ``#`` (the generic engine
subscribes to everything), and ``card_live``'s push driver answers ANY tick on
that fan-out by marking the WHOLE stream-family card set dirty. On a live
telemetry feed that is a continuous re-resolve treadmill layered on top of the
poll baseline that already covers the same cards.

The fix keeps the BASELINE and makes the broad push an explicit choice:

* ``CARD_LIVE`` alone ⇒ the existing fixed-interval poll job still registers
  (deployed as ``CARD_LIVE_POLL_SECONDS=180``). Nothing about the baseline
  changes.
* the broad MQTT subscription + per-tick processing require a NEW generic
  flag ``CARD_LIVE_MQTT_PUSH``, default FALSE. Absence means safe default-off,
  so no deploy/CDK env change is needed to get the safe behavior.
* explicit ``CARD_LIVE_MQTT_PUSH`` true preserves today's behavior exactly.

``MQTT_TOPIC_FILTER`` is NOT touched (the generic engine still subscribes to
everything for the live feed / SiteWise path) and no demo literal appears here.
"""
from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.card_live as card_live  # noqa: E402

PUSH_FLAG = "CARD_LIVE_MQTT_PUSH"


def _fresh_driver_state(monkeypatch):
    monkeypatch.setattr(card_live, "_stream_thread", None)
    monkeypatch.setattr(card_live, "_stream_q", None)
    card_live._stream_stop.clear()


class TestPollBaselineSurvives:
    """CARD_LIVE alone still registers the interval poll — unchanged."""

    def test_poll_job_registers_with_card_live_alone(self, monkeypatch):
        monkeypatch.setenv("CARD_LIVE", "1")
        monkeypatch.delenv(PUSH_FLAG, raising=False)
        monkeypatch.setenv("CARD_LIVE_POLL_SECONDS", "180")
        rec: dict = {}
        from agents import prompts
        monkeypatch.setattr(prompts, "add_interval_job",
                            lambda job_id, fn, secs: rec.update(
                                job_id=job_id, fn=fn, secs=secs))

        card_live.register_poll_job()

        assert rec["job_id"] == card_live._POLL_JOB_ID
        assert rec["fn"] is card_live._poll_covered_cards
        assert rec["secs"] == 180

    def test_poll_tick_still_enqueues_without_the_push_flag(self, monkeypatch):
        monkeypatch.delenv(PUSH_FLAG, raising=False)
        got: list[list[str]] = []
        monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                            lambda ids, **kw: got.append(list(ids)))
        monkeypatch.setattr(card_live, "covered_cards", lambda: ["c1", "c2"])
        monkeypatch.setattr(card_live.live_settings, "live_updates_enabled",
                            lambda: True)

        card_live._poll_covered_cards()

        assert got == [["c1", "c2"]]


class TestPushIsOptInOnly:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv(PUSH_FLAG, raising=False)
        assert card_live._push_enabled() is False

    def test_explicit_false_spellings_are_off(self, monkeypatch):
        for raw in ("", " ", "0", "false", "False", "no", "off"):
            monkeypatch.setenv(PUSH_FLAG, raw)
            assert card_live._push_enabled() is False, repr(raw)

    def test_explicit_true_spellings_are_on(self, monkeypatch):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv(PUSH_FLAG, raw)
            assert card_live._push_enabled() is True, repr(raw)

    def test_unknown_and_typo_values_are_off(self, monkeypatch):
        """A true-ALLOWLIST, not a false-denylist.

        A denylist would let ``flase``/``disabled``/``2`` re-enable the exact
        CPU-amplifying treadmill this flag exists to default off. An opt-in that
        a typo can satisfy is not an opt-in — unknown spellings fail safe.
        """
        for raw in ("flase", "ture", "disabled", "2", "enabled", "y", "t"):
            monkeypatch.setenv(PUSH_FLAG, raw)
            assert card_live._push_enabled() is False, repr(raw)

    def test_unknown_value_warns_once(self, monkeypatch, caplog):
        """Failing safe silently would hide an operator typo."""
        monkeypatch.setattr(card_live, "_warned_push_values", set())
        monkeypatch.setenv(PUSH_FLAG, "flase")
        with caplog.at_level("WARNING"):
            assert card_live._push_enabled() is False
            assert card_live._push_enabled() is False
        warnings = [r for r in caplog.records if PUSH_FLAG in r.getMessage()]
        assert len(warnings) == 1
        assert "flase" in warnings[0].getMessage()

    def test_known_false_spelling_does_not_warn(self, monkeypatch, caplog):
        monkeypatch.setattr(card_live, "_warned_push_values", set())
        monkeypatch.setenv(PUSH_FLAG, "false")
        with caplog.at_level("WARNING"):
            assert card_live._push_enabled() is False
        assert not [r for r in caplog.records if PUSH_FLAG in r.getMessage()]

    def test_no_mqtt_subscription_when_flag_absent(self, monkeypatch):
        monkeypatch.setenv("CARD_LIVE", "1")
        monkeypatch.delenv(PUSH_FLAG, raising=False)
        _fresh_driver_state(monkeypatch)

        def _boom():
            raise AssertionError("must not subscribe to the mqtt fan-out")

        monkeypatch.setattr(card_live.mqtt_subscriber, "subscribe", _boom)

        card_live.start_stream_driver()

        assert card_live._stream_thread is None
        assert card_live._stream_q is None

    def test_no_mqtt_subscription_when_flag_false(self, monkeypatch):
        monkeypatch.setenv("CARD_LIVE", "1")
        monkeypatch.setenv(PUSH_FLAG, "false")
        _fresh_driver_state(monkeypatch)

        def _boom():
            raise AssertionError("must not subscribe to the mqtt fan-out")

        monkeypatch.setattr(card_live.mqtt_subscriber, "subscribe", _boom)

        card_live.start_stream_driver()

        assert card_live._stream_thread is None

    def test_ticks_do_not_enqueue_when_push_is_off(self, monkeypatch):
        """Defense in depth: even a tick delivered directly must not rebuild."""
        monkeypatch.delenv(PUSH_FLAG, raising=False)
        got: list = []
        monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                            lambda ids, **kw: got.append(list(ids)))
        monkeypatch.setattr(card_live.live_settings, "live_updates_enabled",
                            lambda: True)

        def _boom():
            raise AssertionError("stream_cards must not be consulted")

        monkeypatch.setattr(card_live, "stream_cards", _boom)

        card_live._on_telemetry({"topic": "a/b/c", "payload": 1})

        assert got == []


class TestExplicitOptInPreservesBehavior:
    def test_tick_enqueues_stream_cards_when_opted_in(self, monkeypatch):
        monkeypatch.setenv(PUSH_FLAG, "1")
        got: list[list[str]] = []
        monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                            lambda ids, **kw: got.append(list(ids)))
        monkeypatch.setattr(card_live, "stream_cards", lambda: ["s1", "s2"])
        monkeypatch.setattr(card_live.live_settings, "live_updates_enabled",
                            lambda: True)

        card_live._on_telemetry({"topic": "a/b/c", "payload": 1})

        assert got == [["s1", "s2"]]

    def test_driver_subscribes_and_enqueues_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("CARD_LIVE", "1")
        monkeypatch.setenv(PUSH_FLAG, "1")
        fake_q: queue.Queue = queue.Queue()
        monkeypatch.setattr(card_live.mqtt_subscriber, "subscribe", lambda: fake_q)
        unsub: dict = {}
        monkeypatch.setattr(card_live.mqtt_subscriber, "unsubscribe",
                            lambda q: unsub.setdefault("q", q))
        monkeypatch.setattr(card_live, "stream_cards", lambda: ["s1"])
        got: list[list[str]] = []
        monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                            lambda ids, **kw: got.append(list(ids)))
        _fresh_driver_state(monkeypatch)

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

    def test_pause_toggle_still_wins_over_the_opt_in(self, monkeypatch):
        monkeypatch.setenv(PUSH_FLAG, "1")
        got: list = []
        monkeypatch.setattr(card_live.rederive, "enqueue_cards",
                            lambda ids, **kw: got.append(list(ids)))
        monkeypatch.setattr(card_live.live_settings, "live_updates_enabled",
                            lambda: False)

        card_live._on_telemetry({"topic": "a/b", "payload": 1})

        assert got == []


class TestReportedCardLiveMode:
    def test_true_allowlist_reports_mqtt_push(self, monkeypatch):
        monkeypatch.setenv("CARD_LIVE", "1")
        for raw in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv(PUSH_FLAG, raw)
            assert card_live.live_mode() == "mqtt-push"

    def test_malformed_value_stays_poll_only(self, monkeypatch):
        monkeypatch.setenv("CARD_LIVE", "1")
        monkeypatch.setenv(PUSH_FLAG, "enabled-ish")
        assert card_live.live_mode() == "poll-only"

    def test_push_flag_without_card_live_is_not_effective(self, monkeypatch):
        monkeypatch.delenv("CARD_LIVE", raising=False)
        monkeypatch.setenv(PUSH_FLAG, "true")
        assert card_live.live_mode() == "poll-only"
