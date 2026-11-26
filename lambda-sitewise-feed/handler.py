# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SiteWise degradation-feed publisher for the workshop OT chapter.

Writes a degrading vibration trend (plus coolant temperature) for the
5-axis milling center CMX-441 into AWS IoT SiteWise, matching the seeded
story in lambda-adt-seed/009_historian.sql: vibration escalates from
~1.8 to ~2.8 mm/s while a deferred PM goes unperformed.

Invoked from the ERP simulator dashboard button ("Start machine
degradation feed"). Demo/workshop-facing only — the ADT engine never
imports this module.

TARGET RESOLUTION (no hardcoded UUIDs)
    The asset model ("CNC") and asset ("5-Axis-Milling-CMX-441") are
    resolved BY NAME at runtime against the models that
    highbyte/DeploySitewiseModels.py creates unconditionally in
    deploy.sh §1a. If the MODEL is missing the Lambda fails loudly
    (deploy.sh did not run / wrong region). The ASSET normally does not
    exist without the HighByte pipeline, so the Lambda creates it from
    the resolved model when absent (idempotent: found-by-name wins).
    Missing property names on the asset also fail loudly, listing what
    IS available.

TIMESTAMPS
    The seeded story dates (Feb 2026) are older than the SiteWise 7-day
    ingest window for BatchPutAssetPropertyValue, so the trend is
    anchored to "now": a BACKFILL_HOURS window ending at invocation
    time, climbing to ~2.8 mm/s at the most recent sample. Requests for
    more than 7 days of backfill are rejected with a clear error.

IDEMPOTENCY SEMANTICS (narrow — read before relying on it)
    Timestamps are aligned to the INTERVAL_SECONDS grid and SiteWise
    replaces an existing value at the same (asset, property, timestamp),
    so points are never duplicated or interleaved. The jitter is a
    deterministic function of the absolute timestamp, but the RAMP is
    anchored to each invocation's own window (first/last grid point), so
    a re-run is idempotent ONLY while it lands in the same 5-minute
    bucket (same end-aligned grid). A later re-run rewrites the
    overlapping window with a re-anchored ramp (the same timestamp gets a
    different, lower value) and extends the series forward to the new
    "now" — the most recent sample always sits at the ~2.8 mm/s peak.

API LIMITS
    BatchPutAssetPropertyValue allows 10 entries/request and 10 values/
    entry; batches are built to those caps and sent SERIALLY. Every
    errorEntry in a response is surfaced as a raised error with context
    — no silent truncation, no fabricated success.
"""

import hashlib
import logging
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ASSET_MODEL_NAME = "CNC"
ASSET_NAME = "5-Axis-Milling-CMX-441"

# Story shape (lambda-adt-seed/009_historian.sql): vibration 1.8 -> 2.8 mm/s.
VIB_START_MM_S = 1.8
VIB_END_MM_S = 2.8
# Per-axis scale factors — X carries the headline trend; Y/Z track it lower.
VIBRATION_PROPERTIES = {"Vibration_X": 1.0, "Vibration_Y": 0.92, "Vibration_Z": 0.85}
# Bearing friction also drives coolant temperature up (cheap second signal).
TEMP_PROPERTY = "CoolantTemp"
TEMP_START_C = 68.0
TEMP_END_C = 84.0

BACKFILL_HOURS_DEFAULT = 72
MAX_BACKFILL_HOURS = 167  # < SiteWise 7-day past-ingest limit, with margin
INTERVAL_SECONDS = 300  # 5-minute samples -> 864 points per property at 72h

MAX_ENTRIES_PER_REQUEST = 10  # BatchPutAssetPropertyValue hard limit
MAX_VALUES_PER_ENTRY = 10  # BatchPutAssetPropertyValue hard limit
# SiteWise ingests at most 10 BatchPutAssetPropertyValue ENTRIES per second per
# asset property (data stream). A 72 h backfill is 87 entries per property;
# sending them back-to-back, 10 per request, grouped by property, tripped
# ThrottlingException after ~500 values on the first live run (Workshop Studio
# test event 13, 2026-09-14). Entries are therefore interleaved across the
# properties (each request carries at most a few entries per stream) and the
# requests are paced; a throttled entry is retried with backoff, never dropped.
MIN_SECONDS_BETWEEN_REQUESTS = 0.4  # 10 entries / 4 streams / 0.4 s = ~6 entries/s/stream
THROTTLE_ERROR_CODE = "ThrottlingException"
THROTTLE_RETRY_ATTEMPTS = 6
THROTTLE_BACKOFF_SECONDS = 1.0  # doubles per attempt: 1, 2, 4, 8, 16, 32 s


class SitewiseFeedError(RuntimeError):
    """Loud, contextual failure — propagated as the Lambda error message."""


def _jitter(ts: int, span: float) -> float:
    """Deterministic pseudo-noise in [-span, +span] derived from the timestamp.

    Hash-based so re-invocation reproduces the exact same value at the
    same timestamp (the idempotency contract).
    """
    digest = hashlib.sha256(str(ts).encode("utf-8")).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return (unit * 2.0 - 1.0) * span


def _grid_timestamps(end_epoch: int, hours: int, interval: int = INTERVAL_SECONDS) -> list:
    """Ascending, interval-aligned epoch seconds covering the backfill window.

    Alignment to the interval grid makes timestamps stable across
    invocations, so SiteWise overwrites rather than interleaves points.
    """
    if hours <= 0 or hours > MAX_BACKFILL_HOURS:
        raise SitewiseFeedError(
            f"backfill_hours must be 1..{MAX_BACKFILL_HOURS} (SiteWise rejects values "
            f"older than 7 days); got {hours}"
        )
    end_aligned = (end_epoch // interval) * interval
    start_aligned = end_aligned - hours * 3600
    return list(range(start_aligned + interval, end_aligned + interval, interval))


def _ramp(ts: int, t0: int, t1: int, start: float, end: float) -> float:
    """Linear ramp from start@t0 to end@t1, clamped outside the window."""
    if t1 <= t0:
        return end
    frac = min(1.0, max(0.0, (ts - t0) / (t1 - t0)))
    return start + (end - start) * frac


def generate_series(end_epoch: int, hours: int = BACKFILL_HOURS_DEFAULT) -> dict:
    """Build {property_name: [(epoch_seconds, value), ...]} for the story.

    Vibration climbs 1.8 -> 2.8 mm/s across the window (±2% deterministic
    jitter); coolant temperature climbs 68 -> 84 °C (±0.5 °C jitter).
    All series share one ascending timestamp grid.
    """
    grid = _grid_timestamps(end_epoch, hours)
    t0, t1 = grid[0], grid[-1]
    series = {}
    for prop, scale in VIBRATION_PROPERTIES.items():
        series[prop] = [
            (ts, round((_ramp(ts, t0, t1, VIB_START_MM_S, VIB_END_MM_S) + _jitter(ts, 0.05)) * scale, 4))
            for ts in grid
        ]
    series[TEMP_PROPERTY] = [
        (ts, round(_ramp(ts, t0, t1, TEMP_START_C, TEMP_END_C) + _jitter(ts + 1, 0.5), 2))
        for ts in grid
    ]
    return series


def _find_model_id(client, model_name: str) -> str:
    """Resolve an asset model id by exact name; fail loudly if absent."""
    for page in client.get_paginator("list_asset_models").paginate():
        for model in page.get("assetModelSummaries", []):
            if model["name"] == model_name:
                return model["id"]
    raise SitewiseFeedError(
        f"SiteWise asset model '{model_name}' not found in this region. "
        "The models are created by highbyte/DeploySitewiseModels.py "
        "(deploy.sh step 1a) — run it, or check you deployed to this region."
    )


def _find_asset_id(client, model_id: str, asset_name: str):
    """Return the asset id for asset_name under model_id, or None."""
    paginator = client.get_paginator("list_assets")
    for page in paginator.paginate(assetModelId=model_id):
        for asset in page.get("assetSummaries", []):
            if asset["name"] == asset_name:
                return asset["id"]
    return None


def _wait_asset_active(client, asset_id: str, timeout_seconds: int = 120) -> None:
    """Poll until the asset is ACTIVE; fail loudly on FAILED or timeout."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = client.describe_asset(assetId=asset_id)["assetStatus"]
        state = status.get("state")
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise SitewiseFeedError(
                f"asset {asset_id} entered FAILED state: {status.get('error', {})}"
            )
        time.sleep(2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
    raise SitewiseFeedError(
        f"asset {asset_id} did not become ACTIVE within {timeout_seconds}s"
    )


def ensure_asset(client, model_name: str = ASSET_MODEL_NAME, asset_name: str = ASSET_NAME) -> dict:
    """Resolve (or create) the target asset by NAME and map property names to ids.

    - Model missing -> loud failure (deploy.sh §1a did not run here).
    - Asset missing -> created from the model. Without the HighByte
      pipeline nothing else creates assets, so this is the supported
      no-HighByte data path; found-by-name always wins (idempotent).
    Returns {"asset_id": ..., "created": bool, "properties": {name: id}}.
    """
    model_id = _find_model_id(client, model_name)
    asset_id = _find_asset_id(client, model_id, asset_name)
    created = False
    if asset_id is None:
        logger.info("Asset '%s' not found under model '%s' — creating it", asset_name, model_name)
        asset_id = client.create_asset(assetName=asset_name, assetModelId=model_id)["assetId"]
        created = True
    _wait_asset_active(client, asset_id)
    described = client.describe_asset(assetId=asset_id)
    prop_ids = {p["name"]: p["id"] for p in described.get("assetProperties", [])}
    needed = list(VIBRATION_PROPERTIES) + [TEMP_PROPERTY]
    missing = [name for name in needed if name not in prop_ids]
    if missing:
        raise SitewiseFeedError(
            f"asset '{asset_name}' is missing expected properties {missing}; "
            f"available: {sorted(prop_ids)}. The '{model_name}' model schema in "
            "highbyte/DeploySitewiseModels.py may have diverged."
        )
    return {"asset_id": asset_id, "created": created,
            "properties": {name: prop_ids[name] for name in needed}}


def build_batches(asset_id: str, prop_ids: dict, series: dict) -> list:
    """Chunk the series into BatchPutAssetPropertyValue request payloads.

    Each entry holds <= MAX_VALUES_PER_ENTRY ascending TVQs for one
    property; each request holds <= MAX_ENTRIES_PER_REQUEST entries.
    Every generated value lands in exactly one entry (no truncation).
    """
    per_stream = []
    for prop_name, points in series.items():
        prop_id = prop_ids[prop_name]
        stream = []
        for i in range(0, len(points), MAX_VALUES_PER_ENTRY):
            chunk = points[i:i + MAX_VALUES_PER_ENTRY]
            stream.append({
                "entryId": f"{prop_name}-{chunk[0][0]}",
                "assetId": asset_id,
                "propertyId": prop_id,
                "propertyValues": [
                    {
                        "value": {"doubleValue": value},
                        "timestamp": {"timeInSeconds": ts},
                        "quality": "GOOD",
                    }
                    for ts, value in chunk
                ],
            })
        per_stream.append(stream)
    # Round-robin across streams so no request concentrates on one property.
    entries = [entry for group in zip_longest_streams(per_stream) for entry in group]
    return [entries[i:i + MAX_ENTRIES_PER_REQUEST]
            for i in range(0, len(entries), MAX_ENTRIES_PER_REQUEST)]


def zip_longest_streams(streams: list) -> list:
    """Interleave lists of unequal length: [a1,b1,c1],[a2,b2],... (order kept)."""
    longest = max((len(s) for s in streams), default=0)
    return [[s[i] for s in streams if i < len(s)] for i in range(longest)]


def _split_throttled(response: dict) -> tuple:
    """(throttled entry ids, hard failure strings) from one response body."""
    throttled, failures = set(), []
    for err in response.get("errorEntries", []):
        for e in err.get("errors", []):
            if e.get("errorCode") == THROTTLE_ERROR_CODE:
                throttled.add(err.get("entryId"))
            else:
                failures.append(f"{err.get('entryId')}: {e.get('errorCode')} {e.get('errorMessage')}")
    return throttled, failures


def publish_series(client, asset_id: str, prop_ids: dict, series: dict) -> int:
    """Send all batches SERIALLY; raise with full context on any error entry.

    Returns the number of values written. SiteWise reports per-entry
    errors in the response body (HTTP 200), so each response's
    errorEntries is inspected — a failed entry is never silently dropped.
    """
    batches = build_batches(asset_id, prop_ids, series)
    written = 0
    for batch in batches:
        pending, attempt = batch, 0
        while pending:
            response = client.batch_put_asset_property_value(entries=pending)
            throttled, failures = _split_throttled(response)
            if failures:
                raise SitewiseFeedError(
                    f"SiteWise rejected {len(failures)} entr(y/ies) "
                    f"(wrote {written} values before the failure): " + "; ".join(failures[:5])
                )
            written += sum(len(e["propertyValues"]) for e in pending if e["entryId"] not in throttled)
            pending = [e for e in pending if e["entryId"] in throttled]
            if pending:
                if attempt >= THROTTLE_RETRY_ATTEMPTS:
                    raise SitewiseFeedError(
                        f"SiteWise kept throttling {len(pending)} entr(y/ies) after "
                        f"{attempt} retries (wrote {written} values): " + pending[0]["entryId"])
                time.sleep(THROTTLE_BACKOFF_SECONDS * (2 ** attempt))  # nosemgrep: arbitrary-sleep
                attempt += 1
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)  # per-data-stream ingest pacing  # nosemgrep: arbitrary-sleep
    return written


def handler(event, context):
    """Lambda entrypoint: resolve target, generate the trend, publish it.

    event (all optional): {"backfill_hours": int (default 72, max 167),
    "end_epoch": int (default now — test hook)}.
    """
    import boto3  # runtime-only import keeps the module unit-testable offline

    event = event or {}
    hours = int(event.get("backfill_hours", BACKFILL_HOURS_DEFAULT))
    end_epoch = int(event.get("end_epoch", time.time()))
    client = boto3.client("iotsitewise")

    target = ensure_asset(client)
    series = generate_series(end_epoch, hours)
    written = publish_series(client, target["asset_id"], target["properties"], series)

    grid_points = len(next(iter(series.values())))
    result = {
        "asset_name": ASSET_NAME,
        "asset_id": target["asset_id"],
        "asset_created": target["created"],
        "properties": sorted(series),
        "points_per_property": grid_points,
        "values_written": written,
        "backfill_hours": hours,
        "vibration_end_mm_s": VIB_END_MM_S,
    }
    logger.info("SiteWise degradation feed complete: %s", result)
    return result
