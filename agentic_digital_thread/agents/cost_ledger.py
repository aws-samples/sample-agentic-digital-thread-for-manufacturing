# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Day-bucketed cost ledger for measured orchestrator token usage (E7,
ADR-0053).

Sibling of ``artifact_usage.py`` (the ADR-0050 ledger idiom): after an
interactive turn, the runtime hook (``runtime_app._bump_turn_cost_hook``)
bumps ONE day-bucketed row with the turn's MEASURED orchestrator-model token
usage so the steward digest can show a cost trend ("graph made answers
cheaper" evidence).

Measurement honesty (ADR-0053 D-E7 — binding)
---------------------------------------------
- Numbers are MEASURED only: the strands 1.42.0
  ``AgentResult.metrics.accumulated_usage`` surface, already validated into
  the pinned payload by ``runtime_app._usage_cost_payload``. This module
  never estimates, never converts to dollars, and never writes zeros for an
  unmeasured turn (a falsy/malformed ``usage`` writes NOTHING).
- Scope is the ORCHESTRATOR runtime's own model calls only — system agents
  run as separate AgentCore runtimes behind the gateway and their model
  usage is not visible here. The read path (steward digest, Wave 3) stamps
  ``"scope": "orchestrator-model-only"`` on everything it renders.

Ledger
------
Day-bucketed rows on the EXISTING proposals DynamoDB table (via
``self_learning._get_table()`` — no new infrastructure):

    mapping_id    = "cost#turns"            (partition key — ONE row family)
    discovered_at = "YYYY-MM-DD"            (sort key = the day bucket)
    input_tokens / output_tokens / turns — atomic ``ADD`` (absent-safe;
                                           ``turns`` +1 per bump)

Rows deliberately carry **NO ``status`` attribute**, so every status-GSI
read (``get_all_mappings`` and friends) never sees them. Reads use a bounded
key query (fixed partition key + sort-key cutoff, page cap 20) behind a 60 s
in-process cache (``_reset_read_cache()`` test seam).

Failure contract: best-effort / SWALLOWS (ADR-0003 F bulk category) — every
backend failure degrades to ``0`` / ``[]``, never raises. A ledger write
never fails a turn.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_COST_ROW_ID = "cost#turns"
_QUERY_PAGE_CAP = 20
_READ_CACHE_TTL_SECONDS = 60.0

# In-process read cache: {days: (expires_at_monotonic, rows)}.
_read_cache: dict[int, tuple[float, list[dict]]] = {}
_read_cache_lock = threading.Lock()


def _token_count(usage: dict, key: str) -> "int | None":
    """Non-negative int for ``usage[key]``, else ``None`` (bools rejected)."""
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def bump_turn_cost(usage: dict) -> int:
    """Atomically fold one turn's MEASURED usage into today's day bucket.

    ``usage`` is the pinned ``intelligence_summary.cost`` payload built by
    ``runtime_app._usage_cost_payload`` (``input_tokens``/``output_tokens``
    snake_case ints). One ``update_item``: ``ADD input_tokens :in_t,
    output_tokens :out_t, turns :one`` (absent-safe). The row carries NO
    ``status`` attribute, so status-GSI reads never see it. Falsy or
    malformed ``usage`` writes NOTHING (absence = absent — an unmeasured
    turn must never appear as zeros). Best-effort / SWALLOWS: returns 1 when
    a bump was written, ``0`` on skip or any backend failure — a ledger
    write never fails a turn.
    """
    try:
        if not isinstance(usage, dict) or not usage:
            return 0
        input_tokens = _token_count(usage, "input_tokens")
        output_tokens = _token_count(usage, "output_tokens")
        if input_tokens is None or output_tokens is None:
            return 0
        from . import self_learning
        table = self_learning._get_table()
        bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        table.update_item(
            Key={"mapping_id": _COST_ROW_ID, "discovered_at": bucket},
            UpdateExpression=(
                "ADD input_tokens :in_t, output_tokens :out_t, turns :one"),
            ExpressionAttributeValues={
                ":in_t": input_tokens, ":out_t": output_tokens, ":one": 1},
        )
        return 1
    except Exception as e:  # noqa: BLE001 - a ledger write never fails a turn
        logger.warning(f"bump_turn_cost failed (non-critical): {e}")
        return 0


def _window_cutoff_bucket(days: int) -> str:
    """First ``YYYY-MM-DD`` day bucket inside the trailing ``days`` window."""
    return (
        datetime.now(timezone.utc) - timedelta(days=max(int(days), 1) - 1)
    ).strftime("%Y-%m-%d")


def _query_cost_rows(table, cutoff: str) -> list[dict]:
    """Bounded key query: fixed partition key + sort-key cutoff, page cap."""
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression":
            "mapping_id = :mid AND discovered_at >= :cutoff",
        "ExpressionAttributeValues": {":mid": _COST_ROW_ID, ":cutoff": cutoff},
    }
    for _ in range(_QUERY_PAGE_CAP):
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def _fold_cost_item(out: dict[str, dict[str, Any]], item: dict,
                    cutoff: str) -> None:
    """Fold one ledger row into the per-day aggregate (in place).

    Skips rows outside the window or without a day bucket (defensive
    client-side mirror of the query cutoff — honesty must not depend on the
    backend honoring the condition). Numeric attrs coerce int-safe
    (DynamoDB Decimals); missing/malformed fold as 0.
    """
    bucket = str(item.get("discovered_at", ""))
    if not bucket or bucket < cutoff:
        return
    entry = out.setdefault(bucket, {
        "date": bucket, "turns": 0, "input_tokens": 0, "output_tokens": 0,
    })
    for attr in ("turns", "input_tokens", "output_tokens"):
        try:
            entry[attr] += int(item.get(attr, 0))
        except (TypeError, ValueError):
            pass


def read_cost_trend(days: int = 7) -> list[dict]:
    """Per-day cost aggregates over the trailing ``days`` window (inclusive).

    Returns ``[{"date", "turns", "input_tokens", "output_tokens"}, ...]``
    sorted ascending by date — day buckets with no row are ABSENT from the
    list, never fabricated as zeros (the honest-data rule: the reader
    distinguishes "measured zero traffic day" — impossible here, a bump
    implies a turn — from "no measurement"). Results are cached in-process
    for 60 s (per ``days`` value; ``_reset_read_cache()`` test seam).
    Best-effort / SWALLOWS: ``[]`` on any backend failure.
    """
    now_monotonic = time.monotonic()
    with _read_cache_lock:
        cached = _read_cache.get(days)
        if cached and cached[0] > now_monotonic:
            return cached[1]
    try:
        from . import self_learning
        table = self_learning._get_table()
        cutoff = _window_cutoff_bucket(days)
        out: dict[str, dict[str, Any]] = {}
        for item in _query_cost_rows(table, cutoff):
            _fold_cost_item(out, item, cutoff)
        rows = [out[bucket] for bucket in sorted(out)]
        with _read_cache_lock:
            _read_cache[days] = (now_monotonic + _READ_CACHE_TTL_SECONDS, rows)
        return rows
    except Exception as e:  # noqa: BLE001 - reads degrade to empty, never raise
        logger.warning(f"read_cost_trend failed (non-critical): {e}")
        return []


def _reset_read_cache() -> None:
    """Test seam: clear the in-process read cache."""
    with _read_cache_lock:
        _read_cache.clear()
