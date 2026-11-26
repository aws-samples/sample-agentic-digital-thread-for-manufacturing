# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B1 — the _cap_history_payload claim-check (module-level pure function).

Raw VQT point series must NEVER reach a tool return path: any VQT list longer
than the cap is replaced by a stats summary + <=cap landmark points + a stable
``dataset_ref`` claim-check handle. On ANY internal failure the safety net
returns a STRUCTURED ERROR — never the raw payload.

Fully offline — pure data in, pure data out.
"""
import re

import agents.system_agent as sa


def _vqt(minute: int, value):
    return {"timestamp": f"2026-04-30T07:{minute:02d}:00Z", "value": value,
            "quality": "GOOD"}


def _series(n: int, base: float = 10.0):
    # Put an obvious min at index 3 and max at index n-2.
    vals = [base + i * 0.5 for i in range(n)]
    vals[3] = base - 5.0
    vals[n - 2] = base + 100.0
    return [_vqt(i, v) for i, v in enumerate(vals)]


def assert_no_raw_series(node, cap: int = 8):
    """Recursive gate: no VQT list longer than cap anywhere in the payload."""
    if isinstance(node, list):
        if len(node) > cap:
            assert not all(sa._is_vqt_point(x) for x in node[:5]), \
                f"raw VQT series of {len(node)} points leaked"
        for item in node:
            assert_no_raw_series(item, cap)
    elif isinstance(node, dict):
        for value in node.values():
            assert_no_raw_series(value, cap)


def test_long_vqt_list_replaced_by_stats_and_landmarks():
    out = sa._cap_history_payload(_series(40))
    assert set(out) == {"_stats", "dataset_ref", "landmark_values"}
    stats = out["_stats"]
    assert stats["count"] == 40
    assert stats["first_ts"] == "2026-04-30T07:00:00Z"
    assert stats["last_ts"] == "2026-04-30T07:39:00Z"
    assert stats["min"] == 5.0 and stats["min_ts"] == "2026-04-30T07:03:00Z"
    assert stats["max"] == 110.0 and stats["max_ts"] == "2026-04-30T07:38:00Z"
    assert isinstance(stats["mean"], float)
    # Landmarks: <= cap, and include first/min/max/last.
    lm = out["landmark_values"]
    assert 1 <= len(lm) <= 8
    lm_ts = {p["timestamp"] for p in lm}
    assert {"2026-04-30T07:00:00Z", "2026-04-30T07:03:00Z",
            "2026-04-30T07:38:00Z", "2026-04-30T07:39:00Z"} <= lm_ts


def test_never_reconstruct_note_present():
    out = sa._cap_history_payload(_series(20))
    note = out["_stats"]["note"]
    assert "NEVER reconstruct" in note
    assert "sitewise_get_telemetry_chart" in note


def test_dataset_ref_is_tel_uuid12():
    out = sa._cap_history_payload(_series(20))
    assert re.fullmatch(r"tel-[0-9a-f]{12}", out["dataset_ref"])


def test_short_series_passes_through_untouched():
    short = _series(8)  # exactly at cap — not longer, so untouched
    out = sa._cap_history_payload(short)
    assert out == short


def test_nested_components_dict_is_recursed():
    payload = [{
        "elementId": "asset-1",
        "result": {"components": {
            "asset-1:prop-1": {"values": _series(30)},
            "asset-1:prop-2": {"values": _series(5)},
        }},
    }]
    out = sa._cap_history_payload(payload)
    comp = out[0]["result"]["components"]
    assert "_stats" in comp["asset-1:prop-1"]["values"]
    assert comp["asset-1:prop-2"]["values"] == _series(5)
    assert_no_raw_series(out)


def test_non_numeric_values_tolerated():
    mixed = [_vqt(i, "not-a-number") for i in range(12)]
    out = sa._cap_history_payload(mixed)
    stats = out["_stats"]
    assert stats["count"] == 12
    assert "min" not in stats and "max" not in stats  # no numerics to rank
    assert out["landmark_values"] == []


def test_exception_in_walk_returns_structured_error_never_raw(monkeypatch):
    """The old except-fallback returned the RAW (≤5 MB) points into
    model context — the exact regression the claim-check exists to prevent.
    A cap failure must return a structured error instead."""
    def boom(node, cap):
        raise RuntimeError("summarizer exploded")

    monkeypatch.setattr(sa, "_summarize_vqt_series", boom)
    raw = _series(25)
    out = sa._cap_history_payload(raw)
    assert out is not raw
    assert out["error"] == "telemetry_payload_cap_failed"
    assert "narrower time window" in out["message"]
    assert_no_raw_series(out)


def test_multichannel_payload_has_no_raw_series_anywhere():
    payload = {
        "results": [
            {"elementId": "a", "result": {"values": _series(100)}},
            {"elementId": "b", "result": {"components": {
                "b:p1": {"values": _series(64)},
                "b:p2": {"values": _series(9)},
            }}},
        ],
        "unrelated": [1, 2, 3] * 10,  # long non-VQT list stays a list
    }
    out = sa._cap_history_payload(payload)
    assert_no_raw_series(out)
    assert out["unrelated"] == [1, 2, 3] * 10
