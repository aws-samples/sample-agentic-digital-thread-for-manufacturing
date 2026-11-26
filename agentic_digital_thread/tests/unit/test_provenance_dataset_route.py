# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 4 — the ``dataset_ref`` redemption route (claim-check resolver).

Pins the contract on
``GET /api/admin/agents/traces/datasets/{dataset_ref}?session_id=``,
mirroring the D5 queries route's posture (test_provenance_query_route.py):

- admin-gated FIRST (403 before any validation or store read);
- structured 400 on fullmatch-invalid ``dataset_ref`` (``^tel-[0-9a-f]{12}$``)
  / ``session_id`` — the store is never touched, the detail never echoes the
  invalid value;
- structured 404 ``{"detail": "Unknown or expired dataset_ref"}`` on a store
  miss AND past the deliberate 24 h read TTL — never a payload echo, never
  fabricated points;
- 200 returns the EXACT document persisted at cap time;
- NOT gated by ``AGENTS_OBSERVABILITY_DRILLDOWN`` (that flag gates the
  CloudWatch spans reader — a different data source);
- logging is digest-only: no handles, no point values.

Fully offline: an injected fake S3 client backs the real store — no boto3,
no network (socket-blocked lane enforces zero leakage).
"""
from __future__ import annotations

import io
import json
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agents.agents_trace_routes as atr
import agents.dataset_store as dataset_store

_SID = "sess-abc.123"
_REF = "tel-0123456789ab"
_SERIES = [
    {"timestamp": "2026-04-30T07:00:00Z", "value": 10.5, "quality": "GOOD"},
    {"timestamp": "2026-04-30T07:01:00Z", "value": 11.0, "quality": "GOOD"},
]


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def _admin_request(is_admin: bool = True):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin))


@pytest.fixture()
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(dataset_store, "_s3_client", fake)
    monkeypatch.setenv("DATASET_REF_BUCKET", "test-bucket")
    monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
    return fake


def _seed(fake_s3, ref: str = _REF) -> dict:
    assert dataset_store.put_dataset(ref, _SERIES) is True
    return dataset_store.get_dataset(ref)


# --------------------------------------------------------------------------- #
# Auth posture — admin gate FIRST
# --------------------------------------------------------------------------- #

def test_non_admin_forbidden_before_anything_else(monkeypatch):
    calls = []
    monkeypatch.setattr(dataset_store, "get_dataset",
                        lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        # Even an INVALID ref must 403 first — admin gate precedes validation.
        atr.get_provenance_dataset("not-a-ref", _admin_request(is_admin=False),
                                   session_id=_SID)
    assert exc.value.status_code == 403
    assert calls == []


# --------------------------------------------------------------------------- #
# Structured 400 — fullmatch-invalid ids, store never touched, no value echo
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_ref", [
    "tel-XYZXYZXYZXYZ",          # non-hex
    "tel-" + "a" * 11,           # too short
    "tel-" + "a" * 13,           # too long
    "TEL-0123456789ab",          # wrong case prefix
    "0123456789ab",              # missing prefix
    "tel-0123456789ab'--",       # injection shape
    "q-0123456789ab",            # the OTHER handle family
    "tel-0123456789ab\n",        # fullmatch, not match: trailing newline
    "",
])
def test_invalid_dataset_ref_is_structured_400(monkeypatch, bad_ref):
    calls = []
    monkeypatch.setattr(dataset_store, "get_dataset",
                        lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_dataset(bad_ref, _admin_request(), session_id=_SID)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid dataset_ref"
    if bad_ref:                                  # never an echo of the value
        assert bad_ref not in str(exc.value.detail)
    assert calls == []                           # store never touched


@pytest.mark.parametrize("bad_sid", ['s"1', "a b", "x|y", "s\\1", "", "🙂",
                                     "s" * 257])
def test_invalid_session_id_is_structured_400(monkeypatch, bad_sid):
    calls = []
    monkeypatch.setattr(dataset_store, "get_dataset",
                        lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_dataset(_REF, _admin_request(), session_id=bad_sid)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid session id"
    assert calls == []


def test_session_id_defaults_to_missing_and_400s():
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_dataset(_REF, _admin_request())
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Structured 404 — unknown/expired, fixed detail, no ref echo
# --------------------------------------------------------------------------- #

def test_unknown_ref_is_structured_404(fake_s3):
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_dataset(_REF, _admin_request(), session_id=_SID)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown or expired dataset_ref"
    assert _REF not in str(exc.value.detail)


def test_expired_ref_is_the_same_structured_404(fake_s3):
    stale = {
        "dataset_ref": _REF,
        "stored_at": int(time.time()) - dataset_store.DATASET_REF_TTL_SECONDS - 5,
        "point_count": 2,
        "series": _SERIES,
    }
    fake_s3.objects[("test-bucket", f"dataset-refs/{_REF}.json")] = (
        json.dumps(stale).encode("utf-8"))
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_dataset(_REF, _admin_request(), session_id=_SID)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown or expired dataset_ref"


# --------------------------------------------------------------------------- #
# 200 — the exact persisted document from the real store
# --------------------------------------------------------------------------- #

def test_hit_returns_the_persisted_document(fake_s3):
    document = _seed(fake_s3)
    out = atr.get_provenance_dataset(_REF, _admin_request(), session_id=_SID)
    assert out == document
    assert out["dataset_ref"] == _REF
    assert out["point_count"] == 2
    assert out["series"] == _SERIES
    assert set(out) == {"dataset_ref", "stored_at", "point_count", "series"}


# --------------------------------------------------------------------------- #
# NOT gated by the observability drill-down flag
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", [None, "false", "true"])
def test_route_serves_regardless_of_drilldown_flag(monkeypatch, fake_s3, flag):
    if flag is None:
        monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
    else:
        monkeypatch.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", flag)
    monkeypatch.setattr(atr, "_boto_session",
                        lambda: pytest.fail("boto3 touched by datasets route"))
    _seed(fake_s3)
    out = atr.get_provenance_dataset(_REF, _admin_request(), session_id=_SID)
    assert "enabled" not in out
    assert out["series"] == _SERIES


def test_route_registered_on_admin_trace_prefix():
    paths = [r.path for r in atr.router.routes]
    assert "/api/admin/agents/traces/datasets/{dataset_ref}" in paths


# --------------------------------------------------------------------------- #
# Digest-only logging — no handles, no point values
# --------------------------------------------------------------------------- #

def test_logs_carry_no_raw_handle_or_point_values(fake_s3, caplog):
    _seed(fake_s3)
    with caplog.at_level(logging.DEBUG):
        atr.get_provenance_dataset(_REF, _admin_request(), session_id=_SID)
        with pytest.raises(HTTPException):
            atr.get_provenance_dataset("tel-aaaaaaaaaaaa", _admin_request(),
                                       session_id=_SID)
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("agents."))
    assert _REF not in text                # only sha256[:12] digests
    assert "tel-aaaaaaaaaaaa" not in text
    assert _SID not in text
    assert "10.5" not in text              # no point values
    assert "2026-04-30" not in text        # no timestamps
