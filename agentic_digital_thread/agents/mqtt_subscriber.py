# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lightweight MQTT subscriber — buffers recent messages for agent queries.

Configuration (env):
  MQTT_USE_IOT_CORE   "true" enables AWS IoT Core ingestion (WebSocket + IAM).
  IOT_ENDPOINT        the IoT Core ATS endpoint hostname (required to start).
  MQTT_TOPIC_FILTER   the MQTT topic filter to subscribe to. Default ``#``
                      (everything) — the engine is generic; a deployment scopes
                      it via this env var (e.g. the demo stack sets its own
                      namespace filter at deploy time).
"""

import fnmatch
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone

# NOTE: awscrt / awsiot are heavy native deps used ONLY by _iot_core_loop().
# They are imported lazily inside that function so importing this module — and
# anything that imports it (e.g. agents.dashboard) — works in environments
# without the native CRT libraries installed (e.g. the offline test lane).

logger = logging.getLogger(__name__)

_IOT_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _use_iot_core() -> bool:
    """Whether IoT Core ingestion is enabled (read at call time for testability)."""
    return os.environ.get("MQTT_USE_IOT_CORE", "").lower() == "true"


def _iot_endpoint() -> str:
    """The configured IoT Core endpoint ('' when unset)."""
    return os.environ.get("IOT_ENDPOINT", "")


def _topic_filter() -> str:
    """The MQTT topic filter to subscribe to (read at call time for testability).

    Env ``MQTT_TOPIC_FILTER``, default ``#`` — the generic engine subscribes to
    everything; any deployment-specific namespace (e.g. the demo dataset's) is
    supplied by the deployment environment, never hardcoded here (owner
    directive: no demo literals in engine code)."""
    return os.environ.get("MQTT_TOPIC_FILTER", "").strip() or "#"


def parse_isa95_topic(topic: str) -> dict:
    """Parse ISA-95 topic: Enterprise/Site/Area/Line/Equipment/DataType"""
    parts = topic.split("/")
    if len(parts) >= 6:
        return {
            "enterprise": parts[0],
            "site": parts[1],
            "area": parts[2],
            "line": parts[3],
            "equipment": parts[4],
            "data_type": parts[5],
        }
    return {"raw": topic}

def _write_to_historian(topic: str, payload, timestamp: str):
    """Write numeric sensor data to the historian schema."""
    parsed = parse_isa95_topic(topic)
    if 'equipment' not in parsed or 'data_type' not in parsed:
        return
    value = None
    if isinstance(payload, (int, float)):
        value = payload
    elif isinstance(payload, dict) and 'value' in payload:
        try:
            value = float(payload['value'])
        except (ValueError, TypeError):
            return
    elif isinstance(payload, str):
        try:
            value = float(payload)
        except ValueError:
            return
    if value is None:
        return

    unit_map = {'Vibration': 'mm/s', 'Temperature': 'C', 'Pressure': 'bar', 'Current': 'A', 'FlowRate': 'L/min'}
    unit = unit_map.get(parsed['data_type'], '')

    try:
        _historian_write(timestamp, parsed['equipment'], parsed.get('site', ''), parsed['data_type'].lower(), value, unit)
    except Exception as e:
        logger.debug('Historian write failed: %s', e)


# Persistent DB connection for historian writes (avoid connection-per-message)
_historian_conn = None
_historian_lock = threading.Lock()


def _historian_write(timestamp, equipment_id, plant, measurement_type, value, unit):
    global _historian_conn
    with _historian_lock:
        try:
            if _historian_conn is None or _historian_conn.closed:
                from . import pgdriver
                from .db import get_conn_params
                _historian_conn = pgdriver.connect(**get_conn_params())
                _historian_conn.autocommit = True
            _historian_conn.cursor().execute(
                "INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (timestamp, equipment_id, plant, measurement_type, value, unit)
            )
        except Exception:
            # Connection went bad — reset it, next call will reconnect
            try:
                _historian_conn.close()
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
            _historian_conn = None
            raise


_BUFFER_SIZE = 1000
_RETRY_INTERVAL = 10  # seconds between reconnect attempts

_buffer: deque = deque(maxlen=_BUFFER_SIZE)
_lock = threading.Lock()
_client = None
_stop_event = threading.Event()
_connected = False
_total_messages = 0
_unique_topics: set = set()
_subscribers: dict = {}  # queue -> consecutive_full_count
# Last connect/setup failure ({message, timestamp}) — cleared on a successful
# connect. Makes "started, can't connect" diagnosable from /api/iot/stats.
_last_error: dict | None = None


def _sanitize_error(exc: BaseException) -> str:
    """Render an exception for storage/surfacing: exception CLASS name + the
    public category message and its reference (``public_errors``). The message
    itself goes to the log under that reference, never to the stats payload."""
    from .public_errors import public_error_text
    text = public_error_text(exc, "mqtt.subscriber")
    return f"{type(exc).__name__}: {text}"


def _record_error(exc: BaseException) -> None:
    """Record the most recent subscriber failure for get_stats() (sanitized —
    see :func:`_sanitize_error`)."""
    global _last_error
    _last_error = {
        "message": _sanitize_error(exc),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _dispatch(topic: str, payload_raw: str, qos: int = 0):
    """Common handler for incoming MQTT messages (buffer + historian + SSE fan-out)."""
    global _total_messages
    try:
        try:
            payload = json.loads(payload_raw)
        except (json.JSONDecodeError, ValueError):
            payload = payload_raw
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "topic": topic,
            "payload": payload,
            "qos": qos,
        }
        with _lock:
            _buffer.append(entry)
            try:
                _write_to_historian(topic, payload, entry['timestamp'])
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
            _total_messages += 1
            _unique_topics.add(topic)
            dead = []
            for q, strikes in _subscribers.items():
                try:
                    q.put_nowait(entry)
                    _subscribers[q] = 0
                except queue.Full:
                    _subscribers[q] = strikes + 1
                    if _subscribers[q] > 100:  # 100 consecutive drops = orphaned
                        dead.append(q)
            for q in dead:
                del _subscribers[q]
    except Exception as e:
        logger.debug("MQTT message parse error: %s", e)


# ── Subscriber loop ──────────────────────────────────────────────────────

def _iot_core_loop():
    """Subscribe via awsiotsdk WebSocket + IAM auth."""
    global _client, _connected, _last_error
    try:
        from awscrt import auth as crt_auth, io as crt_io, mqtt as crt_mqtt  # lazy: heavy native dep
        from awsiot import mqtt_connection_builder  # lazy: heavy native dep
    except Exception as e:  # noqa: BLE001 - missing native CRT libs must be observable, not a silent dead thread
        _record_error(e)
        logger.warning("MQTT subscriber unavailable (awsiotsdk import failed): %s", e)
        return
    event_loop_group = crt_io.EventLoopGroup(1)
    host_resolver = crt_io.DefaultHostResolver(event_loop_group)
    bootstrap = crt_io.ClientBootstrap(event_loop_group, host_resolver)
    creds = crt_auth.AwsCredentialsProvider.new_default_chain(bootstrap)

    def _on_iot_msg(topic, payload, **kwargs):
        _dispatch(topic, payload.decode("utf-8", errors="replace"))

    while not _stop_event.is_set():
        try:
            conn = mqtt_connection_builder.websockets_with_default_aws_signing(
                endpoint=_iot_endpoint(),
                region=_IOT_REGION,
                credentials_provider=creds,
                client_bootstrap=bootstrap,
                client_id=f"adt-sub-{int(time.time())}",
                clean_session=False,
                keep_alive_secs=30,
            )
            future = conn.connect()
            future.result(timeout=10)
            _client = conn
            _connected = True
            _last_error = None
            logger.info("MQTT connected to IoT Core %s", _iot_endpoint())
            conn.subscribe(_topic_filter(), crt_mqtt.QoS.AT_LEAST_ONCE, _on_iot_msg)
            _stop_event.wait()
            conn.disconnect().result(timeout=5)
        except Exception as e:
            _connected = False
            _record_error(e)
            logger.warning("IoT Core unavailable (%s), retrying in %ss", e, _RETRY_INTERVAL)
            _stop_event.wait(timeout=_RETRY_INTERVAL)


def subscribe() -> queue.Queue:
    """Register a new SSE client; returns a Queue that receives MQTT entries."""
    q: queue.Queue = queue.Queue(maxsize=100)
    with _lock:
        _subscribers[q] = 0
    return q


def unsubscribe(q: queue.Queue) -> None:
    """Remove a previously registered SSE client queue."""
    with _lock:
        _subscribers.pop(q, None)


_thread: threading.Thread | None = None


def start_subscriber():
    """Start the MQTT subscriber background thread (AWS IoT Core).

    Idempotent — a live thread is left alone, so callers may re-trigger freely
    (server startup, system onboarding). Gated ONLY on configuration
    (MQTT_USE_IOT_CORE + IOT_ENDPOINT), never on which systems are onboarded:
    an idle IoT subscription is cheap, and the thread must exist for the
    /api/iot/stats lifecycle fields ("never started" vs "started, can't
    connect" vs "connected, silent") to be honest.
    """
    global _thread
    if not _use_iot_core():
        logger.warning("MQTT subscriber disabled: set MQTT_USE_IOT_CORE=true to enable IoT Core ingestion")
        return
    if not _iot_endpoint():
        logger.warning("MQTT subscriber not started: IOT_ENDPOINT is not set")
        return
    if _thread is not None and _thread.is_alive():
        return  # already running — idempotent no-op
    _stop_event.clear()
    _thread = threading.Thread(target=_iot_core_loop, daemon=True, name="mqtt-subscriber")
    _thread.start()


def stop_subscriber():
    """Stop the MQTT subscriber cleanly."""
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)


def search_messages(query: str, limit: int = 20) -> list:
    """Fuzzy search topics and payloads for query string."""
    query_lower = query.lower()
    results = []
    with _lock:
        for msg in reversed(_buffer):
            topic_match = query_lower in msg["topic"].lower()
            payload_str = json.dumps(msg["payload"]) if not isinstance(msg["payload"], str) else msg["payload"]
            payload_match = query_lower in payload_str.lower()
            if topic_match or payload_match:
                results.append(msg)
                if len(results) >= limit:
                    break
    return results


def get_recent_messages(topic_filter: str = "#", limit: int = 50) -> list:
    """Get recent messages matching a topic glob pattern."""
    results = []
    with _lock:
        for msg in reversed(_buffer):
            if fnmatch.fnmatch(msg["topic"], topic_filter):
                results.append(msg)
                if len(results) >= limit:
                    break
    return results


def get_topic_tree() -> dict:
    """Return hierarchical topic structure with message counts."""
    tree: dict = {}
    with _lock:
        for msg in _buffer:
            parts = msg["topic"].split("/")
            node = tree
            for part in parts:
                if part not in node:
                    node[part] = {"count": 0, "children": {}}
                node[part]["count"] += 1
                node = node[part]["children"]
    return tree


def get_stats() -> dict:
    """Return subscriber statistics + lifecycle truth.

    Lifecycle fields (additive) distinguish "never started" vs "started,
    can't connect" vs "connected, silent" from /api/iot/stats:
      enabled              MQTT_USE_IOT_CORE is on
      endpoint_configured  IOT_ENDPOINT is set
      started              the subscriber thread is alive
      connected            the MQTT connection is up (existing field)
      last_error           {message, timestamp} of the last connect/setup
                           failure (None after a successful connect)
    """
    with _lock:
        return {
            "enabled": _use_iot_core(),
            "endpoint_configured": bool(_iot_endpoint()),
            "started": _thread is not None and _thread.is_alive(),
            "connected": _connected,
            "last_error": _last_error,
            "total_messages": _total_messages,
            "unique_topics": len(_unique_topics),
            "buffer_size": len(_buffer),
        }
