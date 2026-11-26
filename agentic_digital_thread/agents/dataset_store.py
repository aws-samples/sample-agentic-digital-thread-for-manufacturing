# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3-backed ``dataset_ref`` store — the claim-check redemption write/read
side (provenance design, Wave 4; the maintainers' deferred-work list contract).

``_cap_history_payload`` (``agents/system_agent.py``) replaces every long VQT
series with stats + landmarks + a stable ``dataset_ref`` handle
(``tel-<uuid12>``). This module persists the WITHHELD full series at cap time
and redeems it later for the Tier-3 "View full data" client view. Storage is
S3 per the design doc's Wave-4 prescription — the app's EXISTING
stream-capture/forensics bucket (nothing here is a system of record), under
the ``dataset-refs/`` key prefix:

- **Bucket**: ``DATASET_REF_BUCKET`` env when set, else the existing
  ``STREAM_CAPTURE_BUCKET`` (both roles already wired: the ECS app holds
  Get/PutObject; the AgentCore runtime role gains PutObject on the prefix in
  ``adt_compute_stack``). No env ⇒ persistence is a documented no-op and the
  redemption route serves an honest 404 — local/unit stay offline-pure.
- **TTL (deliberate)**: ``DATASET_REF_TTL_SECONDS`` = 24 h read-side expiry,
  the same horizon (and rationale) as ``PROVENANCE_QUERY_TTL_SECONDS`` — a
  Tier-3 transcript reopened across a workday must still redeem. Physical
  deletion is the bucket's existing 7-day lifecycle rule; between 24 h and
  7 d the reader treats the object as expired (same structured 404).
- **Size cap**: ``MAX_DATASET_BYTES`` — an over-cap series is honestly NOT
  persisted (never truncated mid-series); redemption then 404s, exactly the
  degradation the handle contract anticipates.
- **Logging is digest-only**: handles appear as ``sha256[:12]`` digests plus
  byte lengths; point values/timestamps NEVER reach a log line (the
  claim-check posture extended to this store).

CRITICAL INVARIANT (claim-check policy): the persisted payload
serves the CLIENT ONLY. ``get_dataset`` has exactly one production reader —
the admin-gated redemption route in ``agents_trace_routes.py`` (grep-pinned
by ``tests/unit/test_dataset_ref_persistence.py``). It never re-enters any
LLM token path and never rides the SSE stream. Writes are fail-soft end to
end: ``put_dataset`` never raises — a persistence failure must never break
the tool's capped payload (whose bytes are identical either way).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from agents.provenance_store import digest12

logger = logging.getLogger(__name__)

#: The ONE handle-shape home for the read/write sides (the deferred.md
#: contract shape — ``system_agent._summarize_vqt_series`` mints it and must
#: never change; the redemption route imports this rather than re-growing a
#: copy).
DATASET_REF_RE = re.compile(r"^tel-[0-9a-f]{12}$")

#: DELIBERATE read-side TTL — 24 h, mirroring the provenance-query store's
#: rationale (a steward reopening a morning transcript in the afternoon still
#: redeems). Physical deletion is the bucket's 7-day lifecycle rule; past the
#: read TTL the route returns the same honest structured 404.
DATASET_REF_TTL_SECONDS = 24 * 3600

#: Hard cap on one persisted dataset object (serialized JSON bytes). Sized to
#: the i3X history streamed-body ceiling (``_I3X_HISTORY_BODY_CAP``, 5 MB) —
#: a single withheld series can never legitimately exceed the response it
#: came from. Over-cap ⇒ honestly not persisted (never silently truncated).
MAX_DATASET_BYTES = 5_000_000

#: Key prefix inside the bucket (the runtime role's PutObject grant is
#: scoped to exactly this prefix).
_KEY_PREFIX = "dataset-refs/"

# Lazily-constructed module singleton; tests inject a fake by assigning
# ``dataset_store._s3_client`` directly (mirrors provenance_store._job_store).
_s3_client: Any | None = None


def _bucket() -> str:
    """Target bucket — ``DATASET_REF_BUCKET`` override, else the existing
    stream-capture bucket; '' (⇒ no-op persistence, honest 404 on read) when
    neither is configured."""
    return (os.environ.get("DATASET_REF_BUCKET", "").strip()
            or os.environ.get("STREAM_CAPTURE_BUCKET", "").strip())


def _client():
    global _s3_client
    if _s3_client is None:
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        _s3_client = boto3.client("s3", region_name=region)
    return _s3_client


def _key(dataset_ref: str) -> str:
    return f"{_KEY_PREFIX}{dataset_ref}.json"


def put_dataset(dataset_ref: str, series: list) -> bool:
    """Persist one withheld VQT series under its claim-check handle.

    NEVER raises (fail-soft by contract — a persist failure must not break
    the capped tool payload); returns ``True`` only on a completed write.
    Invalid handle shape, missing bucket, non-list payload, over-cap body,
    and any backend error all return ``False`` — the redemption route then
    serves its honest structured 404. Logging is digest-only.
    """
    if not (isinstance(dataset_ref, str) and DATASET_REF_RE.fullmatch(dataset_ref)):
        return False
    bucket = _bucket()
    if not bucket or not isinstance(series, list):
        return False
    try:
        body = json.dumps({
            "dataset_ref": dataset_ref,
            "stored_at": int(time.time()),
            "point_count": len(series),
            "series": series,
        }, default=str).encode("utf-8")
        if len(body) > MAX_DATASET_BYTES:
            logger.info("dataset_store: over size cap ref_sha=%s len_bytes=%d",
                        digest12(dataset_ref), len(body))
            return False
        _client().put_object(Bucket=bucket, Key=_key(dataset_ref), Body=body,
                             ContentType="application/json")
    except Exception as exc:  # nosec B110 - fail-soft by contract (see docstring)
        logger.warning("dataset_store: put failed ref_sha=%s category=%s "
                       "(claim-check payload unaffected; redemption will 404)",
                       digest12(dataset_ref), type(exc).__name__)
        return False
    logger.info("dataset_store: stored ref_sha=%s len_bytes=%d points=%d",
                digest12(dataset_ref), len(body), len(series))
    return True


def get_dataset(dataset_ref: str) -> dict | None:
    """The stored dataset document, or ``None`` (unknown/expired/invalid).

    ``None`` is the ONLY miss signal — the redemption route maps it to the
    structured 404 ("Unknown or expired dataset_ref"). Objects older than
    ``DATASET_REF_TTL_SECONDS`` are treated as expired even before the
    bucket lifecycle physically deletes them. Never raises on malformed
    input or backend errors; never fabricates a document.

    CLIENT-ONLY by the claim-check policy: the sole production caller is the
    admin-gated redemption route (grep-pinned) — never an LLM token path.
    """
    if not (isinstance(dataset_ref, str) and DATASET_REF_RE.fullmatch(dataset_ref)):
        return None
    bucket = _bucket()
    if not bucket:
        return None
    try:
        response = _client().get_object(Bucket=bucket, Key=_key(dataset_ref))
        document = json.loads(response["Body"].read())
    except Exception as exc:  # nosec B110 - miss/backend error ⇒ honest None
        logger.info("dataset_store: miss ref_sha=%s category=%s",
                    digest12(dataset_ref), type(exc).__name__)
        return None
    if not isinstance(document, dict):
        return None
    stored_at = document.get("stored_at")
    if not isinstance(stored_at, int) or (
            time.time() - stored_at > DATASET_REF_TTL_SECONDS):
        logger.info("dataset_store: expired ref_sha=%s", digest12(dataset_ref))
        return None
    return document
