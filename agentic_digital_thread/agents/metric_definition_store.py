# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Graph-backed metric-definition storage (ADR-0066 C3/C4).

The graph holds metadata describing how metrics are computed, never production
values. Reads are bounded and cached. A cold read failure raises rather than
fabricating an empty definition set; after one successful load, refresh failure
serves the last good snapshot for one further TTL window. A cold failure also
opens a short cooldown during which the raise is served from the remembered
failure — the raise is unchanged, the repeated graph read is not.

This module is import-pure. Graph and metric-layer dependencies are imported only
inside call paths, and the module-level wrappers remain monkeypatchable seams for
offline tests.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import logging
import math
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 300.0
_MAX_DEFINITIONS = 500

#: A COLD graph outage must not be re-queried once per caller. Two callers read
#: this store directly on every metric-shaped turn — the pre-check notice's
#: vocabulary build (``metric_term_resolution.known_metric_vocabulary``) and the
#: ADR-0066 D6 usage bump (``runtime_app._graph_definition_precheck_ids``) — and
#: the composed layer's own cooldown covers NEITHER, because neither goes through
#: it. Inside this window a cold read still RAISES (D4 honesty is unchanged: the
#: pre-check keeps failing open to silence and never claims a term is undefined),
#: it just raises from the remembered failure instead of hitting the graph again.
#: Deliberately much shorter than the default TTL — it bounds retry pressure
#: only — and cleared outright by :func:`invalidate`. The first cold failure
#: re-raises the graph's OWN exception and remembers it; callers inside the window
#: raise a ``RuntimeError`` that carries that remembered detail, so a cold outage
#: costs ONE attempt per window across ALL callers and recovers automatically
#: after it. A WARM snapshot is never subject to this: it keeps the D4 stale-serve
#: path, whose retry boundary is the TTL.
COLD_FAILURE_COOLDOWN_SECONDS = 60.0

# OPTIONAL MATCH deliberately returns one all-null row when the label has no
# nodes. agents.db returns [] on a graph failure, so [] remains an unambiguous
# failure signal instead of being mistaken for an honestly empty deployment.
_LOAD_CYPHER = (
    "OPTIONAL MATCH (m:MetricDefinition) "
    "RETURN m.id AS id, m.definition_json AS definition_json "
    "ORDER BY id LIMIT 500"
)
_READBACK_CYPHER = (
    "MATCH (m:MetricDefinition {id: $id}) "
    "RETURN m.definition_json AS definition_json LIMIT 1"
)
_WRITE_CYPHER = (
    "MERGE (m:MetricDefinition {id: $id}) "
    "ON CREATE SET m.source = $source, m.created_by = $actor, "
    "m.created_at = $timestamp "
    "SET m.label = $label, m.unit = $unit, m.kind = $kind, "
    "m.definition_json = $definition_json, m.synonyms_json = $synonyms_json, "
    "m.version = $version, m.updated_at = $timestamp"
)

_snapshot: dict[str, dict] | None = None
_loaded_at: float | None = None
_shadowed_or_invalid_rows: list[dict] = []
_cold_failure_until: float | None = None
_cold_failure_detail: str = ""
_snapshot_lock = threading.RLock()


def neptune_query(cypher: str, parameters=None) -> list:
    """Lazy, monkeypatchable graph-read seam."""
    from .db import neptune_query as _query
    return _query(cypher, parameters)


def neptune_write(cypher: str, parameters=None) -> bool:
    """Lazy, monkeypatchable graph-write seam."""
    from .db import neptune_write as _write
    return _write(cypher, parameters)


def _parse_definition(raw: str | dict) -> dict:
    from .metric_definition import parse_definition
    return parse_definition(raw)


def _canonical_json(doc: dict) -> str:
    from .metric_definition import canonical_json
    return canonical_json(doc)


def _code_metric_ids() -> frozenset[str]:
    from .metric_layer import CodeMetricLayer
    return frozenset(metric.id for metric in CodeMetricLayer().list_metrics())


def _ttl_seconds() -> float:
    """The snapshot TTL: a FINITE, positive number of seconds, fail-loud.

    ``inf`` is rejected as hard as ``nan`` and as hard as a negative value. An
    infinite TTL is not a large bound, it is the ABSENCE of one: the snapshot
    would never refresh, staleness would be unbounded, and a process that missed
    an accept would never converge — silently removing the very bound ADR-0066 D4
    states as the staleness guarantee. This matches the Athena-budget posture
    (``athena_tools._coerce_timeout``), where a non-finite configured bound raises
    rather than quietly meaning "no limit".
    """
    raw = os.environ.get("METRIC_DEFINITION_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS))
    try:
        ttl = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "METRIC_DEFINITION_TTL_SECONDS must be a number of seconds, "
            f"got {raw!r}"
        ) from exc
    if not math.isfinite(ttl):
        raise ValueError(
            "METRIC_DEFINITION_TTL_SECONDS must be a finite number of seconds, "
            f"got {raw!r}"
        )
    if not ttl > 0:
        raise ValueError("METRIC_DEFINITION_TTL_SECONDS must be positive")
    return ttl


def _reason(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"invalid definition_json: {detail or type(exc).__name__}"


def _snapshot_from_rows(rows: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    code_ids = _code_metric_ids()
    definitions: dict[str, dict] = {}
    skipped: list[dict] = []

    for row in rows:
        row_id = str(row.get("id") or "")
        raw = row.get("definition_json")
        # The all-null OPTIONAL MATCH sentinel represents a successful empty read.
        if not row_id and raw is None:
            continue
        try:
            if not isinstance(raw, str):
                raise ValueError("must be a JSON string")
            doc = _parse_definition(raw)
            doc_id = str(doc.get("id") or "")
            if not row_id or doc_id != row_id:
                raise ValueError("id does not match the graph node id")
            if _canonical_json(doc) != raw:
                raise ValueError("bytes are not canonical JSON")
        except Exception as exc:
            skipped.append({"id": row_id, "reason": _reason(exc)})
            continue

        if row_id in code_ids:
            logger.warning("Graph metric definition %s is shadowed by a code metric id", row_id)
            skipped.append({"id": row_id, "reason": "code-id collision"})
            continue
        definitions[row_id] = doc

    return definitions, skipped


def _copy_snapshot(snapshot: dict[str, dict]) -> dict[str, dict]:
    return copy.deepcopy(snapshot)


def _cooldown_raise(detail: str) -> None:
    """Raise for a cold read suppressed by :data:`COLD_FAILURE_COOLDOWN_SECONDS`.

    A cold failure RAISES unconditionally — the store must never fabricate an
    empty snapshot, because a partial vocabulary presented as complete would let
    the pre-check claim a graph-defined term is undefined in this deployment
    (ADR-0066 D4). The cooldown changes only WHERE the raise comes from.
    """
    raise RuntimeError(
        f"metric-definition graph read unavailable (cold-failure cooldown): {detail}"
    )


def load_definitions() -> dict[str, dict]:
    """Return the bounded, TTL-cached graph-definition snapshot.

    A cold graph failure RAISES — always, including inside the cold-failure
    cooldown window (see :func:`_cooldown_raise` and
    :data:`COLD_FAILURE_COOLDOWN_SECONDS`). Once a good snapshot exists, refresh
    failure serves that stale snapshot and advances the retry boundary by one TTL
    so a failing graph is not queried once per caller.
    """
    global _snapshot, _loaded_at, _shadowed_or_invalid_rows
    global _cold_failure_until, _cold_failure_detail

    with _snapshot_lock:
        now = time.monotonic()
        ttl = _ttl_seconds()
        if (_snapshot is not None and _loaded_at is not None
                and now - _loaded_at < ttl):
            return _copy_snapshot(_snapshot)

        if (_snapshot is None and _cold_failure_until is not None
                and now < _cold_failure_until):
            _cooldown_raise(_cold_failure_detail)

        try:
            rows = neptune_query(_LOAD_CYPHER)
            if not rows:
                raise RuntimeError("metric-definition graph read returned no sentinel")
            loaded, skipped = _snapshot_from_rows(rows[:_MAX_DEFINITIONS])
        except Exception as exc:
            if _snapshot is None:
                _cold_failure_until = now + COLD_FAILURE_COOLDOWN_SECONDS
                _cold_failure_detail = f"{type(exc).__name__}: {exc}".strip()
                raise
            _loaded_at = now
            logger.warning(
                "Metric-definition refresh failed; serving stale snapshot category=%s",
                type(exc).__name__,
            )
            return _copy_snapshot(_snapshot)

        _snapshot = loaded
        _shadowed_or_invalid_rows = skipped
        _loaded_at = now
        _cold_failure_until = None
        _cold_failure_detail = ""
        return _copy_snapshot(_snapshot)


def write_definition(doc: dict, actor: str, version: Any) -> str:
    """MERGE one definition and prove exact canonical-byte graph readback."""
    parsed = _parse_definition(doc)
    definition_json = _canonical_json(parsed)
    metric_id = str(parsed["id"])
    timestamp = datetime.now(timezone.utc).isoformat()
    params = {
        "id": metric_id,
        "label": str(parsed["label"]),
        "unit": str(parsed.get("unit") or ""),
        "kind": str(parsed["kind"]),
        "definition_json": definition_json,
        "synonyms_json": json.dumps(
            parsed.get("synonyms") or [], ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ),
        "source": "custom",
        "actor": str(actor or ""),
        "version": version,
        "timestamp": timestamp,
    }
    if neptune_write(_WRITE_CYPHER, params) is not True:
        raise RuntimeError(f"metric definition write failed for {metric_id!r}")

    rows = neptune_query(_READBACK_CYPHER, {"id": metric_id})
    actual = rows[0].get("definition_json") if rows else None
    if not isinstance(actual, str) or actual.encode("utf-8") != definition_json.encode("utf-8"):
        raise RuntimeError(f"metric definition exact readback mismatch for {metric_id!r}")
    return f"metricdef:{metric_id}"


def invalidate() -> None:
    """Drop the process-local snapshot, its skipped-row report, and any
    cold-failure cooldown (an accept must be able to retry the graph at once)."""
    global _snapshot, _loaded_at, _shadowed_or_invalid_rows
    global _cold_failure_until, _cold_failure_detail
    with _snapshot_lock:
        _snapshot = None
        _loaded_at = None
        _shadowed_or_invalid_rows = []
        _cold_failure_until = None
        _cold_failure_detail = ""


def shadowed_or_invalid() -> list[dict]:
    """Return rows skipped by the most recent successful snapshot load."""
    with _snapshot_lock:
        return copy.deepcopy(_shadowed_or_invalid_rows)
