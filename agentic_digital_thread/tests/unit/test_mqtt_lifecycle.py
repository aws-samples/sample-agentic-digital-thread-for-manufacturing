# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests — MQTT subscriber lifecycle truth (dashboard-overhaul Task 1C).

Pins the two halves of the fix:

1. ``mqtt_subscriber`` lifecycle honesty: ``get_stats()`` exposes
   ``enabled`` / ``endpoint_configured`` / ``started`` / ``connected`` /
   ``last_error`` so "never started" vs "started, can't connect" vs
   "connected, silent" are distinguishable from ``/api/iot/stats``;
   ``start_subscriber()`` is idempotent and gated ONLY on configuration
   (MQTT_USE_IOT_CORE + IOT_ENDPOINT) — never on which systems are onboarded
   (``mqtt_subscriber`` never touches the catalog; these tests run with no
   catalog whatsoever, i.e. no historian / ``iot_historian`` entry exists).

2. server.py startup wiring (owner-directive cleanup): the ``iot_historian``
   schema-literal gate is GONE — ``lifespan`` calls
   ``mqtt_subscriber.start_subscriber()`` unconditionally, and no
   historian/demo literal remains anywhere in server.py.

Runnable two ways (matches test_server_restore.py):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_mqtt_lifecycle.py
  * standalone:  python agentic_digital_thread/tests/unit/test_mqtt_lifecycle.py
"""
import inspect
import sys
import time
from pathlib import Path

import pytest

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.mqtt_subscriber as mqtt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_subscriber_state(monkeypatch):
    """Isolate module-level subscriber state per test (thread, error, flags)."""
    monkeypatch.delenv("MQTT_USE_IOT_CORE", raising=False)
    monkeypatch.delenv("IOT_ENDPOINT", raising=False)
    saved = (mqtt._thread, mqtt._last_error, mqtt._connected)
    mqtt._thread = None
    mqtt._last_error = None
    mqtt._connected = False
    mqtt._stop_event.clear()
    yield
    # Stop anything a test started, then restore.
    mqtt._stop_event.set()
    if mqtt._thread is not None:
        mqtt._thread.join(timeout=5)
    mqtt._stop_event.clear()
    mqtt._thread, mqtt._last_error, mqtt._connected = saved


def _wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# get_stats() lifecycle fields
# --------------------------------------------------------------------------- #

def test_get_stats_exposes_lifecycle_keys_with_thread_stopped():
    """Acceptance: with the thread stopped, the new keys are present and honest
    (started:false, connected:false), alongside the pre-existing counters."""
    stats = mqtt.get_stats()
    assert set(stats) >= {
        "enabled", "endpoint_configured", "started", "connected",
        "last_error", "total_messages", "unique_topics", "buffer_size",
    }, stats
    assert stats["enabled"] is False
    assert stats["endpoint_configured"] is False
    assert stats["started"] is False
    assert stats["connected"] is False
    assert stats["last_error"] is None


def test_get_stats_reflects_env_configuration(monkeypatch):
    monkeypatch.setenv("MQTT_USE_IOT_CORE", "true")
    monkeypatch.setenv("IOT_ENDPOINT", "example-ats.iot.us-east-1.amazonaws.com")
    stats = mqtt.get_stats()
    assert stats["enabled"] is True
    assert stats["endpoint_configured"] is True
    assert stats["started"] is False  # nothing started yet


def test_get_stats_surfaces_recorded_error():
    """last_error carries {message, timestamp} of the most recent failure
    (set by _iot_core_loop's except / import-failure paths via _record_error).
    The stored message is SANITIZED: exception class name + a public category
    message and its reference; the exception's own text goes to the log."""
    mqtt._record_error(RuntimeError("AMQJS0007E connect refused"))
    stats = mqtt.get_stats()
    assert stats["last_error"] is not None
    assert "connect refused" not in stats["last_error"]["message"]
    assert "Reference: " in stats["last_error"]["message"]
    assert stats["last_error"]["message"].startswith("RuntimeError: ")
    assert stats["last_error"]["timestamp"]  # ISO timestamp present


# --------------------------------------------------------------------------- #
# last_error sanitization (fix-pass: /api/iot/stats must not leak internals)
# --------------------------------------------------------------------------- #

def test_record_error_carries_no_message_text():
    mqtt._record_error(ValueError("x" * 5000))
    msg = mqtt.get_stats()["last_error"]["message"]
    assert msg.startswith("ValueError: ") and "xxx" not in msg
    assert len(msg) < 200


def test_record_error_strips_endpoint_hostname(monkeypatch):
    endpoint = "abc123-ats.iot.us-east-1.amazonaws.com"
    monkeypatch.setenv("IOT_ENDPOINT", endpoint)
    mqtt._record_error(ConnectionError(
        f"could not connect to {endpoint}:443 (TLS handshake timed out)"))
    msg = mqtt.get_stats()["last_error"]["message"]
    assert endpoint not in msg
    assert "could not be reached" in msg  # ConnectionError -> not_reachable
    assert msg.startswith("ConnectionError: ")


def test_record_error_endpoint_stripped_before_truncation(monkeypatch):
    # No part of the message reaches the stats payload, however it is padded.
    endpoint = "abc123-ats.iot.us-east-1.amazonaws.com"
    monkeypatch.setenv("IOT_ENDPOINT", endpoint)
    mqtt._record_error(RuntimeError("p" * 190 + endpoint))
    msg = mqtt.get_stats()["last_error"]["message"]
    assert endpoint not in msg


# --------------------------------------------------------------------------- #
# Topic filter (fix-pass, owner directive: generic engine — env-driven filter)
# --------------------------------------------------------------------------- #

def test_topic_filter_defaults_to_everything():
    assert mqtt._topic_filter() == "#"


def test_topic_filter_env_override(monkeypatch):
    monkeypatch.setenv("MQTT_TOPIC_FILTER", "ExampleCorp/PlantA/#")
    assert mqtt._topic_filter() == "ExampleCorp/PlantA/#"


def test_topic_filter_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MQTT_TOPIC_FILTER", "   ")
    assert mqtt._topic_filter() == "#"


def test_no_demo_topic_literal_in_module():
    """Owner directive: the demo namespace never appears in engine code — the
    subscription filter comes from MQTT_TOPIC_FILTER (deploy env)."""
    src = Path(mqtt.__file__).read_text()
    assert "Adt/#" not in src
    assert "MQTT_TOPIC_FILTER" in src


# --------------------------------------------------------------------------- #
# start_subscriber() gating + idempotence (no catalog involved at all)
# --------------------------------------------------------------------------- #

def test_start_subscriber_noop_when_disabled():
    mqtt.start_subscriber()
    assert mqtt._thread is None
    assert mqtt.get_stats()["started"] is False


def test_start_subscriber_noop_without_endpoint(monkeypatch):
    monkeypatch.setenv("MQTT_USE_IOT_CORE", "true")
    mqtt.start_subscriber()
    assert mqtt._thread is None
    assert mqtt.get_stats()["started"] is False


def test_start_subscriber_starts_without_any_catalog_entry_and_is_idempotent(monkeypatch):
    """Acceptance: start_subscriber() starts the thread from env config alone —
    no historian / 'iot_historian' catalog entry exists (the module never reads
    the catalog) — and a second call is a no-op on the live thread."""
    monkeypatch.setenv("MQTT_USE_IOT_CORE", "true")
    monkeypatch.setenv("IOT_ENDPOINT", "example-ats.iot.us-east-1.amazonaws.com")

    # Stub the loop: park on the stop event (no awscrt import, no network).
    monkeypatch.setattr(mqtt, "_iot_core_loop", lambda: mqtt._stop_event.wait())

    mqtt.start_subscriber()
    first = mqtt._thread
    assert first is not None
    assert _wait_until(first.is_alive)
    assert mqtt.get_stats()["started"] is True

    mqtt.start_subscriber()  # idempotent — the live thread is left alone
    assert mqtt._thread is first

    mqtt.stop_subscriber()
    assert not first.is_alive()
    assert mqtt.get_stats()["started"] is False


def test_mqtt_subscriber_never_reads_the_catalog():
    """Start policy is config-driven only — pin that the module has no catalog
    or schema-literal coupling (the demo literal lived in server.py)."""
    src = Path(mqtt.__file__).read_text()
    assert "catalog" not in src
    assert "iot_historian" not in src.replace("iot_historian.sensor_readings", "")
    # (the historian WRITE table is the module's own storage schema, not a gate)


# --------------------------------------------------------------------------- #
# server.py startup wiring (owner directive: schema-literal gate deleted)
# --------------------------------------------------------------------------- #

def test_server_lifespan_starts_subscriber_unconditionally():
    """lifespan calls mqtt_subscriber.start_subscriber() with NO catalog/schema
    condition around it — the call site is a bare statement, not inside an
    `if ... schema/historian` guard."""
    import agents.server as server

    src = inspect.getsource(server.lifespan)
    assert "mqtt_subscriber.start_subscriber()" in src
    call_line = next(
        line for line in src.splitlines() if "mqtt_subscriber.start_subscriber()" in line
    )
    # The old gate indented the call inside `if any(... schema ...)`; the fix
    # leaves it at lifespan body level (4-space indent within the function).
    assert call_line == "    mqtt_subscriber.start_subscriber()", repr(call_line)


def test_server_has_no_iot_historian_literal():
    """grep gate (acceptance): 'iot_historian' / the historian_schemas gate no
    longer appear anywhere in server.py."""
    import agents.server as server

    text = Path(server.__file__).read_text()
    assert "iot_historian" not in text
    assert "historian_schemas" not in text
    assert "historian" not in text.lower()


_TESTS = [
    test_get_stats_exposes_lifecycle_keys_with_thread_stopped,
    test_get_stats_surfaces_recorded_error,
    test_start_subscriber_noop_when_disabled,
    test_mqtt_subscriber_never_reads_the_catalog,
    test_server_lifespan_starts_subscriber_unconditionally,
    test_server_has_no_iot_historian_literal,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} mqtt lifecycle tests passed (no network)")
