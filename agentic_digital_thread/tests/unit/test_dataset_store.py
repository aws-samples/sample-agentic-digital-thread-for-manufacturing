# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 4 — the S3-backed ``dataset_ref`` store (``agents/dataset_store.py``).

Pins the claim-check redemption storage contract (the maintainers' deferred-work list):

- handle-shape fullmatch at write AND read (``tel-<uuid12>`` — the ONE
  ``DATASET_REF_RE`` home);
- round-trip through an injected fake S3 client (no boto3, no network —
  socket-blocked lane);
- honest degradation everywhere: no bucket env ⇒ no-op, over-cap body ⇒ NOT
  persisted (never truncated), backend errors swallowed (``put_dataset``
  NEVER raises), expiry past the deliberate 24 h read TTL ⇒ ``None``;
- digest-only logging: neither the raw handle nor any point value/timestamp
  ever reaches a log line.

Fully offline — the fake client is a dict.
"""
from __future__ import annotations

import io
import json
import logging
import time

import pytest

import agents.dataset_store as dataset_store

_REF = "tel-0123456789ab"
_SERIES = [
    {"timestamp": "2026-04-30T07:00:00Z", "value": 10.5, "quality": "GOOD"},
    {"timestamp": "2026-04-30T07:01:00Z", "value": 11.0, "quality": "GOOD"},
]


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0

    def put_object(self, *, Bucket, Key, Body, ContentType):
        assert ContentType == "application/json"
        self.put_calls += 1
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class ExplodingS3:
    def put_object(self, **kwargs):
        raise RuntimeError("backend exploded")

    def get_object(self, **kwargs):
        raise RuntimeError("backend exploded")


@pytest.fixture()
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(dataset_store, "_s3_client", fake)
    monkeypatch.setenv("DATASET_REF_BUCKET", "test-bucket")
    monkeypatch.delenv("STREAM_CAPTURE_BUCKET", raising=False)
    return fake


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #

def test_put_get_round_trip(fake_s3):
    assert dataset_store.put_dataset(_REF, _SERIES) is True
    document = dataset_store.get_dataset(_REF)
    assert document["dataset_ref"] == _REF
    assert document["point_count"] == 2
    assert document["series"] == _SERIES
    assert isinstance(document["stored_at"], int)
    # Key rides the prefix the runtime-role grant is scoped to.
    assert list(fake_s3.objects) == [("test-bucket", f"dataset-refs/{_REF}.json")]


def test_stream_capture_bucket_is_the_fallback(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(dataset_store, "_s3_client", fake)
    monkeypatch.delenv("DATASET_REF_BUCKET", raising=False)
    monkeypatch.setenv("STREAM_CAPTURE_BUCKET", "capture-bucket")
    assert dataset_store.put_dataset(_REF, _SERIES) is True
    assert ("capture-bucket", f"dataset-refs/{_REF}.json") in fake.objects
    assert dataset_store.get_dataset(_REF)["series"] == _SERIES


# --------------------------------------------------------------------------- #
# Handle-shape fullmatch — write AND read
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_ref", [
    "tel-XYZXYZXYZXYZ",          # non-hex
    "tel-" + "a" * 11,           # too short
    "tel-" + "a" * 13,           # too long
    "TEL-0123456789ab",          # wrong case prefix
    "0123456789ab",              # missing prefix
    "tel-0123456789ab'--",       # injection shape
    "q-0123456789ab",            # the OTHER handle family
    "",
    None,
    42,
])
def test_invalid_handles_rejected_both_sides(fake_s3, bad_ref):
    assert dataset_store.put_dataset(bad_ref, _SERIES) is False
    assert fake_s3.put_calls == 0
    assert dataset_store.get_dataset(bad_ref) is None


# --------------------------------------------------------------------------- #
# Honest degradation
# --------------------------------------------------------------------------- #

def test_no_bucket_env_is_a_noop(monkeypatch):
    monkeypatch.delenv("DATASET_REF_BUCKET", raising=False)
    monkeypatch.delenv("STREAM_CAPTURE_BUCKET", raising=False)
    # Poison the client so any boto3/S3 touch fails the test.
    monkeypatch.setattr(dataset_store, "_client",
                        lambda: pytest.fail("S3 touched without a bucket"))
    assert dataset_store.put_dataset(_REF, _SERIES) is False
    assert dataset_store.get_dataset(_REF) is None


def test_non_list_series_not_persisted(fake_s3):
    assert dataset_store.put_dataset(_REF, {"not": "a list"}) is False
    assert fake_s3.put_calls == 0


def test_over_cap_series_honestly_not_persisted(fake_s3, monkeypatch, caplog):
    monkeypatch.setattr(dataset_store, "MAX_DATASET_BYTES", 64)
    with caplog.at_level(logging.INFO, logger="agents.dataset_store"):
        assert dataset_store.put_dataset(_REF, _SERIES) is False
    assert fake_s3.put_calls == 0                      # never truncated instead
    assert any("over size cap" in r.getMessage() for r in caplog.records)


def test_put_swallows_backend_errors_and_logs_honestly(monkeypatch, caplog):
    monkeypatch.setattr(dataset_store, "_s3_client", ExplodingS3())
    monkeypatch.setenv("DATASET_REF_BUCKET", "test-bucket")
    with caplog.at_level(logging.WARNING, logger="agents.dataset_store"):
        assert dataset_store.put_dataset(_REF, _SERIES) is False   # never raises
    messages = [r.getMessage() for r in caplog.records]
    assert any("put failed" in m for m in messages)


def test_get_swallows_backend_errors(monkeypatch):
    monkeypatch.setattr(dataset_store, "_s3_client", ExplodingS3())
    monkeypatch.setenv("DATASET_REF_BUCKET", "test-bucket")
    assert dataset_store.get_dataset(_REF) is None      # never raises


def test_get_miss_is_none(fake_s3):
    assert dataset_store.get_dataset(_REF) is None


def test_malformed_stored_document_is_none(fake_s3):
    fake_s3.objects[("test-bucket", f"dataset-refs/{_REF}.json")] = b'["not a dict"]'
    assert dataset_store.get_dataset(_REF) is None


# --------------------------------------------------------------------------- #
# The deliberate 24 h read TTL
# --------------------------------------------------------------------------- #

def test_expired_document_is_none(fake_s3):
    stale = {
        "dataset_ref": _REF,
        "stored_at": int(time.time()) - dataset_store.DATASET_REF_TTL_SECONDS - 5,
        "point_count": 2,
        "series": _SERIES,
    }
    fake_s3.objects[("test-bucket", f"dataset-refs/{_REF}.json")] = (
        json.dumps(stale).encode("utf-8"))
    assert dataset_store.get_dataset(_REF) is None


def test_document_without_stored_at_is_none(fake_s3):
    fake_s3.objects[("test-bucket", f"dataset-refs/{_REF}.json")] = (
        json.dumps({"dataset_ref": _REF, "series": _SERIES}).encode("utf-8"))
    assert dataset_store.get_dataset(_REF) is None


def test_ttl_is_the_deliberate_24h_horizon():
    """The TTL mirrors PROVENANCE_QUERY_TTL_SECONDS' documented rationale —
    a transcript reopened across a workday still redeems."""
    from agents.provenance_store import PROVENANCE_QUERY_TTL_SECONDS
    assert dataset_store.DATASET_REF_TTL_SECONDS == PROVENANCE_QUERY_TTL_SECONDS == 24 * 3600


# --------------------------------------------------------------------------- #
# Digest-only logging — no handles, no point values
# --------------------------------------------------------------------------- #

def test_logs_carry_no_raw_handle_or_point_values(fake_s3, caplog):
    with caplog.at_level(logging.DEBUG, logger="agents.dataset_store"):
        dataset_store.put_dataset(_REF, _SERIES)
        dataset_store.get_dataset(_REF)
        dataset_store.get_dataset("tel-aaaaaaaaaaaa")   # miss path logs too
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert _REF not in text                 # only sha256[:12] digests
    assert "tel-aaaaaaaaaaaa" not in text
    assert "2026-04-30" not in text         # no timestamps
    assert "10.5" not in text               # no values
