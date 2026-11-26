# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Full-window coverage for bounded SiteWise history reads (ADR-0065, 2026-09-18).

Live finding: a 72 h, four-property, 5-minute-cadence read (864 points per
property, 3456 total) came back ``partial`` with reason ``page_limit`` and its
24 downsampled points covered only ~44 of the 72 requested hours — the tail
(where the anomaly lived) was silently dropped. The page budget
(``HISTORY_MAX_PAGES_PER_CHUNK`` × the MODEL-chosen ``max_results`` page size,
default 100) capped a chunk at 800 raw points regardless of the window, so
whether a read completed depended on how many entries the model batched and
which page size it happened to pick.

Contract pinned here:
* the service page size is the API maximum, never a model-chosen number;
* a request inside the raw-point budget returns the FULL window, downsampled
  to ``HISTORY_DOWNSAMPLE_POINTS`` near-evenly spaced points that always
  include the first and last sample;
* a genuinely truncated read says WHICH window it covered.
"""
from __future__ import annotations

import datetime as dt

import pytest

from agents import aws_session
from agents import sitewise_read as sw

_CADENCE_S = 300
_WINDOW_H = 72
_POINTS_PER_PROPERTY = _WINDOW_H * 3600 // _CADENCE_S  # 864
_START = dt.datetime(2026, 9, 15, 16, 38, tzinfo=dt.UTC)
_END = _START + dt.timedelta(hours=_WINDOW_H)


class _PagedSeriesClient:
    """Serves one ascending series per entry, ``maxResults`` points per page
    across all entries of a batch (the service contract), with an opaque
    continuation token."""

    def __init__(self, points_per_entry: int = _POINTS_PER_PROPERTY,
                 start: dt.datetime = _START, cadence_s: int = _CADENCE_S):
        self.history_calls: list[dict] = []
        self.points_per_entry = points_per_entry
        self.start_s = int(start.timestamp())
        self.cadence_s = cadence_s

    def _point(self, entry_index: int, i: int) -> dict:
        ts = self.start_s + i * self.cadence_s
        # Ramp 1.8 -> ~2.8 so a dropped tail is visible in the max.
        value = 1.8 + (i / max(self.points_per_entry - 1, 1)) + entry_index * 0.001
        return {"value": {"doubleValue": round(value, 4)},
                "timestamp": {"timeInSeconds": ts}, "quality": "GOOD"}

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        self.history_calls.append({"entries": entries, "maxResults": maxResults,
                                   "nextToken": nextToken})
        offset = int(nextToken) if nextToken else 0
        total = len(entries) * self.points_per_entry
        stop = min(offset + maxResults, total)
        by_entry: dict[str, list] = {}
        for flat in range(offset, stop):
            entry_index, i = divmod(flat, self.points_per_entry)
            by_entry.setdefault(entries[entry_index]["entryId"], []).append(
                self._point(entry_index, i))
        response = {
            "successEntries": [{"entryId": eid, "assetPropertyValueHistory": pts}
                               for eid, pts in by_entry.items()],
            "errorEntries": [], "skippedEntries": [],
        }
        if stop < total:
            response["nextToken"] = str(stop)
        return response


def _patch(monkeypatch, client):
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)


def _entries(n: int) -> list[dict]:
    return [{"assetId": "asset-1", "propertyId": f"prop-{i}"} for i in range(n)]


def _read(entries, **kwargs):
    return sw.get_property_history(
        {}, "us-east-1", entries,
        start_iso=_START.isoformat(), end_iso=_END.isoformat(), **kwargs)


@pytest.mark.parametrize("n_props", [3, 4])
def test_72h_5min_multi_property_read_returns_full_window(monkeypatch, n_props):
    client = _PagedSeriesClient()
    _patch(monkeypatch, client)
    out = _read(_entries(n_props))  # defaults: the model passed nothing special
    for i in range(n_props):
        item = out[f"asset-1_prop-{i}"]
        assert isinstance(item, list), f"prop-{i} truncated: {item!r}"
        assert len(item) == sw.HISTORY_DOWNSAMPLE_POINTS
        assert item[0]["timestamp"] == int(_START.timestamp())
        last_ts = int(_START.timestamp()) + (_POINTS_PER_PROPERTY - 1) * _CADENCE_S
        assert item[-1]["timestamp"] == last_ts, "tail sample dropped"
        assert max(p["value"] for p in item) > 2.75, "ramp peak lives in the tail"


def test_model_page_size_does_not_shape_coverage(monkeypatch):
    client = _PagedSeriesClient()
    _patch(monkeypatch, client)
    out = _read(_entries(4), max_results=100)
    # Every series is complete; the sampling summary and the requested window
    # beside them are not series.
    assert all(isinstance(v, list) for k, v in out.items()
               if k not in (sw.HISTORY_SAMPLING_KEY, sw.HISTORY_WINDOW_KEY))
    assert {c["maxResults"] for c in client.history_calls} == {sw.HISTORY_FETCH_PAGE_SIZE}
    assert len(client.history_calls) <= sw.HISTORY_MAX_PAGES_PER_CHUNK


def test_downsample_is_near_evenly_spaced_and_keeps_endpoints(monkeypatch):
    client = _PagedSeriesClient()
    _patch(monkeypatch, client)
    points = _read(_entries(1))["asset-1_prop-0"]
    ts = [p["timestamp"] for p in points]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    ideal = (ts[-1] - ts[0]) / (len(ts) - 1)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert all(0.6 * ideal <= g <= 1.4 * ideal for g in gaps), gaps


def test_partial_result_states_the_covered_window(monkeypatch):
    monkeypatch.setattr(sw, "HISTORY_MAX_PAGES_PER_CHUNK", 1)
    monkeypatch.setattr(sw, "HISTORY_FETCH_PAGE_SIZE", 200)
    client = _PagedSeriesClient()
    _patch(monkeypatch, client)
    item = _read(_entries(1))["asset-1_prop-0"]
    assert item["status"] == "partial" and item["truncated"] is True
    assert item["reason"] == "page_limit"
    covered_last = _START + dt.timedelta(seconds=199 * _CADENCE_S)
    assert item["covered_start"] == _START.isoformat()
    assert item["covered_end"] == covered_last.isoformat()
    assert item["requested_start"] == _START.isoformat()
    assert item["requested_end"] == _END.isoformat()
    assert item["covered_start"] in item["note"] and item["covered_end"] in item["note"]
    assert item["requested_end"] in item["note"]
    assert item["points"][-1]["timestamp"] == int(covered_last.timestamp())


def test_partial_without_points_still_states_requested_window(monkeypatch):
    monkeypatch.setattr(sw, "HISTORY_MAX_TOTAL_CALLS", 0)
    client = _PagedSeriesClient()
    _patch(monkeypatch, client)
    item = _read(_entries(1))["asset-1_prop-0"]
    assert item["status"] == "partial" and item["reason"] == "call_limit"
    assert item["covered_start"] is None and item["covered_end"] is None
    assert item["requested_start"] == _START.isoformat()
    assert "no points" in item["note"]


def test_raw_point_budget_is_per_chunk_so_later_chunks_are_not_starved(monkeypatch):
    # Chunk 1 (16 entries) blows the per-chunk raw budget; chunk 2 (1 entry)
    # must still complete — a global budget would have starved it.
    monkeypatch.setattr(sw, "HISTORY_MAX_RAW_POINTS_PER_CHUNK", 1000)
    client = _PagedSeriesClient(points_per_entry=100)
    _patch(monkeypatch, client)
    out = _read(_entries(17))
    first_chunk = [out[f"asset-1_prop-{i}"] for i in range(16)]
    assert any(isinstance(v, dict) and v["reason"] == "raw_point_limit" for v in first_chunk)
    tail = out["asset-1_prop-16"]
    assert isinstance(tail, list) and len(tail) == sw.HISTORY_DOWNSAMPLE_POINTS
