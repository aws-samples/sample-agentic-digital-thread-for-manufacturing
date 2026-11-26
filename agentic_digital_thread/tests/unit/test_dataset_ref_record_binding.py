# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 4 — ``dataset_ref`` binding into the provenance record (handles only).

Two additive seams carry the claim-check handle from a capped tool result to
the Tier-3 "View full data" affordance:

1. ``tool_result_capture.safe_tool_result_metadata`` retains ≤4
   fullmatch-VALIDATED ``tel-<uuid12>`` handles as ``dataset_refs`` — opaque
   server-minted references, never payload content (the module's allowlist
   posture is unchanged);
2. ``provenance._build_record`` adds an ADDITIVE ``dataset_refs`` key to the
   D1 record ONLY when the capture retained handles — absent refs ⇒ the
   record is byte-identical to its pre-Wave-4 self (the pinned
   ``set(record)`` D1 shape in test_provenance_builder.py stays green).

Plus the never-on-SSE leak pin: the serialized provenance frame carries the
HANDLE but none of the capped series' stats, landmarks, timestamps, or
values — the raw data is redeemable only through the admin-gated route.
"""
from __future__ import annotations

import json

from agents.provenance import build_provenance_records
from agents.tool_result_capture import (
    claim_check_dataset_refs,
    safe_tool_result_metadata,
)

_REF = "tel-0123456789ab"
_REF2 = "tel-ffffffffffff"


def _capped_payload(ref: str = _REF) -> dict:
    """The exact claim-check shape ``_summarize_vqt_series`` emits."""
    return {
        "_stats": {
            "count": 40,
            "first_ts": "2026-04-30T07:00:00Z",
            "last_ts": "2026-04-30T07:39:00Z",
            "min": 5.0, "min_ts": "2026-04-30T07:03:00Z",
            "max": 110.0, "max_ts": "2026-04-30T07:38:00Z",
            "mean": 19.1375,
            "note": "Full series intentionally omitted.",
        },
        "dataset_ref": ref,
        "landmark_values": [
            {"timestamp": "2026-04-30T07:00:00Z", "value": 10.0,
             "quality": "GOOD"},
            {"timestamp": "2026-04-30T07:03:00Z", "value": 5.0,
             "quality": "GOOD"},
        ],
    }


def _tool_result(payload) -> dict:
    return {"status": "success", "content": [{"json": payload}]}


# --------------------------------------------------------------------------- #
# claim_check_dataset_refs — bounded, validated extraction
# --------------------------------------------------------------------------- #

def test_top_level_claim_check_ref_extracted():
    assert claim_check_dataset_refs(_tool_result(_capped_payload())) == [_REF]


def test_nested_multichannel_refs_extracted_and_deduplicated():
    payload = [{
        "elementId": "asset-1",
        "result": {"components": {
            "asset-1:prop-1": {"values": _capped_payload(_REF)},
            "asset-1:prop-2": {"values": _capped_payload(_REF2)},
            "asset-1:prop-3": {"values": _capped_payload(_REF)},  # duplicate
        }},
    }]
    assert claim_check_dataset_refs(_tool_result(payload)) == [_REF, _REF2]


def test_invalid_handle_shapes_never_retained():
    for bad in ("tel-XYZXYZXYZXYZ", "TEL-0123456789ab", "tel-" + "a" * 13,
                "q-0123456789ab", "", 42, None, ["tel-0123456789ab"]):
        payload = {"dataset_ref": bad, "landmark_values": []}
        assert claim_check_dataset_refs(_tool_result(payload)) == []


def test_extraction_is_capped_at_four_handles():
    payload = {f"ch{i}": _capped_payload(f"tel-{i:012x}") for i in range(6)}
    refs = claim_check_dataset_refs(_tool_result(payload))
    assert len(refs) == 4


def test_large_text_content_still_yields_refs():
    """Capped-payload JSON routinely exceeds the 1 KB metadata text bound —
    the ref scan re-parses text content up to its own larger ceiling."""
    big = _capped_payload()
    big["_stats"]["note"] = "x" * 4000                 # > 1 KB as text
    result = {"status": "success", "content": [{"text": json.dumps(big)}]}
    assert claim_check_dataset_refs(result) == [_REF]
    # And the metadata attaches them even though the bounded summary is
    # honestly "unknown" for over-bound text (the gateway's common shape).
    meta = safe_tool_result_metadata(
        result, tool_name="sitewise_get_asset_history", max_chars=1024)
    assert meta["app_status"] == "unknown"
    assert meta["dataset_refs"] == [_REF]


# --------------------------------------------------------------------------- #
# safe_tool_result_metadata — additive dataset_refs, allowlist unchanged
# --------------------------------------------------------------------------- #

def test_metadata_carries_validated_refs_for_capped_result():
    meta = safe_tool_result_metadata(
        _tool_result(_capped_payload()),
        tool_name="sitewise_get_asset_history", max_chars=1024)
    assert meta["dataset_refs"] == [_REF]
    # The allowlist posture is unchanged: no stats, landmarks, or values.
    dumped = json.dumps(meta)
    assert "landmark_values" not in dumped
    assert "2026-04-30" not in dumped
    assert "110.0" not in dumped


def test_metadata_without_capped_series_has_no_refs_key():
    meta = safe_tool_result_metadata(
        {"status": "success", "content": [{"json": {"rows": [[1, 2]],
                                                    "row_count": 1}}]},
        tool_name="query_database", max_chars=1024)
    assert "dataset_refs" not in meta          # absent, not empty — additive


def test_transport_error_result_has_no_refs_key():
    meta = safe_tool_result_metadata(
        {"status": "error", "content": [{"json": _capped_payload()}]},
        tool_name="sitewise_get_asset_history", max_chars=1024)
    assert "dataset_refs" not in meta


# --------------------------------------------------------------------------- #
# The D1 record — additive dataset_refs, absent key otherwise
# --------------------------------------------------------------------------- #

def _build(meta_by_tool):
    return build_provenance_records(
        session_id="sess-1",
        tool_calls=[{"name": "sysa___get_asset_history", "toolUseId": "t-1"},
                    {"name": "sysa___query", "toolUseId": "t-2"}],
        tool_inputs={},
        tool_results_metadata=meta_by_tool,
        evidence_by_tool={},
        rules_applied=[],
        verdict=None,
        captured_at="2026-08-03T19:00:00Z",
    )


def test_record_carries_additive_dataset_refs():
    meta = safe_tool_result_metadata(
        _tool_result(_capped_payload()),
        tool_name="sitewise_get_asset_history", max_chars=1024)
    payload = _build({"t-1": meta})
    by_id = {r["tool_id"]: r for r in payload["records"]}
    assert by_id["t-1"]["dataset_refs"] == [_REF]
    # The un-capped sibling record has NO key — the pinned D1 shape verbatim.
    assert "dataset_refs" not in by_id["t-2"]


def test_hostile_meta_refs_dropped_at_the_record_boundary():
    payload = _build({"t-1": {"app_status": "success",
                              "dataset_refs": ["tel-0123456789ab",
                                               "not-a-handle",
                                               "tel-XYZXYZXYZXYZ",
                                               42]}})
    record = payload["records"][0]
    assert record["dataset_refs"] == [_REF]


def test_meta_with_only_invalid_refs_yields_no_key():
    payload = _build({"t-1": {"app_status": "success",
                              "dataset_refs": ["nope", ""]}})
    assert "dataset_refs" not in payload["records"][0]


# --------------------------------------------------------------------------- #
# Never-on-SSE leak pin — the frame carries the handle, never the data
# --------------------------------------------------------------------------- #

def test_provenance_frame_carries_handle_but_no_series_data():
    """The claim-check policy end to end: the ONLY thing the streamed
    provenance frame may carry about a capped series is the redemption
    handle. Stats, landmarks, timestamps, and values are structurally
    absent — they are served exclusively by the admin-gated route."""
    meta = safe_tool_result_metadata(
        _tool_result(_capped_payload()),
        tool_name="sitewise_get_asset_history", max_chars=1024)
    frame = json.dumps(_build({"t-1": meta}))
    assert _REF in frame                       # the handle rides
    for leaked in ("landmark_values", "_stats", "2026-04-30",
                   "110.0", "19.1375", "GOOD",
                   "Full series intentionally omitted"):
        assert leaked not in frame             # the data never does
