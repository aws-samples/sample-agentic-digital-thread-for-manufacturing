# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An empty SiteWise history window that ends BEFORE the property's first
stored value says so (2026-10-04).

Live finding: on a fresh account the telemetry feed only starts minutes before
use, so an answer that asked for a machine's history on an earlier date got an
empty series and said "no historical data", which reads like lost data. When a
window returns zero points the reader makes ONE bounded extra call for the
property's earliest stored value; if that value is after the window's end the
outcome is a short structured note (timestamps only, no payload). Every other
outcome is unchanged, and a failed extra call degrades to the plain empty list.
"""
from __future__ import annotations

import datetime as dt
import json

from agents import aws_session
from agents import sitewise_read as sw
from agents import system_agent
from agents import telemetry_coverage

_WINDOW_START = "2026-09-20T00:00:00Z"
_WINDOW_END = "2026-09-21T00:00:00Z"
_END_S = int(dt.datetime(2026, 9, 21, tzinfo=dt.UTC).timestamp())
_FEED_START_S = _END_S + 12 * 86400  # first stored value: 12 days after the window


def _point(ts: int) -> dict:
    return {"value": {"doubleValue": 1.5}, "timestamp": {"timeInSeconds": ts},
            "quality": "GOOD"}


class _Client:
    """Batch history returns ``window_points`` per entry; the single-property
    probe returns ``first_points`` (or raises ``probe_error``)."""

    def __init__(self, window_points=(), first_points=(), probe_error=None):
        self.window_points = list(window_points)
        self.first_points = list(first_points)
        self.probe_error = probe_error
        self.probe_calls: list[dict] = []

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        return {"successEntries": [
            {"entryId": entry["entryId"], "assetPropertyValueHistory": self.window_points}
            for entry in entries
        ]}

    def get_asset_property_value_history(self, **kwargs):
        self.probe_calls.append(kwargs)
        if self.probe_error:
            raise self.probe_error
        return {"assetPropertyValueHistory": self.first_points}


def _install(monkeypatch, client: _Client) -> None:
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda *_args, **_kwargs: client)


def _read(entries=None):
    entries = entries or [{"assetId": "a1", "propertyId": "p1"}]
    return sw.get_property_history({}, "us-east-1", entries,
                                   start_iso=_WINDOW_START, end_iso=_WINDOW_END)


def test_window_before_first_value_states_where_data_starts(monkeypatch):
    client = _Client(first_points=[_point(_FEED_START_S)])
    _install(monkeypatch, client)
    outcome = _read()["a1_p1"]
    starts = dt.datetime.fromtimestamp(_FEED_START_S, tz=dt.UTC).isoformat()
    assert outcome["status"] == "before_first_value"
    assert outcome["points"] == []
    assert outcome["data_starts_at"] == starts
    assert outcome["requested_end"] == "2026-09-21T00:00:00+00:00"
    assert starts in outcome["note"]
    assert set(outcome) == {"status", "points", "data_starts_at", "requested_start",
                            "requested_end", "probed_from", "note"}
    assert len(client.probe_calls) == 1
    probe = client.probe_calls[0]
    assert (probe["assetId"], probe["propertyId"]) == ("a1", "p1")
    assert probe["timeOrdering"] == "ASCENDING" and probe["maxResults"] == 1
    assert probe["startDate"] < dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
    # The single-property history API rejects fractional seconds.
    assert probe["startDate"].microsecond == 0 and probe["endDate"].microsecond == 0


def test_empty_window_where_data_exists_stays_an_empty_series(monkeypatch):
    _install(monkeypatch, _Client(first_points=[_point(_END_S - 30 * 86400)]))
    assert _read()["a1_p1"] == []


def test_failed_probe_degrades_to_the_empty_series(monkeypatch):
    _install(monkeypatch, _Client(probe_error=RuntimeError("throttled")))
    assert _read()["a1_p1"] == []


def test_property_with_no_stored_values_stays_an_empty_series(monkeypatch):
    _install(monkeypatch, _Client(first_points=[]))
    assert _read()["a1_p1"] == []


def test_malformed_probe_response_degrades_to_the_empty_series(monkeypatch):
    client = _Client()
    client.get_asset_property_value_history = lambda **_kwargs: "not-a-dict"
    _install(monkeypatch, client)
    assert _read()["a1_p1"] == []


def test_non_empty_window_makes_no_extra_call_and_is_unchanged(monkeypatch):
    client = _Client(window_points=[_point(_END_S - 3600), _point(_END_S - 60)])
    _install(monkeypatch, client)
    outcome = _read()["a1_p1"]
    assert isinstance(outcome, list) and len(outcome) == 2
    assert client.probe_calls == []


def test_probes_are_bounded_per_request(monkeypatch):
    client = _Client(first_points=[_point(_FEED_START_S)])
    _install(monkeypatch, client)
    entries = [{"assetId": f"a{i}", "propertyId": "p"} for i in range(20)]
    result = _read(entries)
    assert len(client.probe_calls) == sw.HISTORY_MAX_FIRST_VALUE_PROBES
    marked = [k for k, v in result.items() if isinstance(v, dict) and k != sw.HISTORY_WINDOW_KEY]
    assert len(marked) == sw.HISTORY_MAX_FIRST_VALUE_PROBES
    assert all(v == [] for k, v in result.items()
               if k not in marked and k != sw.HISTORY_WINDOW_KEY)


def test_mode_b_property_history_carries_the_note(monkeypatch):
    _install(monkeypatch, _Client(first_points=[_point(_FEED_START_S)]))
    out = system_agent._mode_b_sitewise_query(json.dumps({
        "op": "property_history", "assetId": "a1", "propertyId": "p1",
        "start_iso": _WINDOW_START, "end_iso": _WINDOW_END,
    }), {})
    assert out["result"]["a1_p1"]["status"] == "before_first_value"


def test_direct_tool_carries_the_note_and_coverage_names_where_data_starts(monkeypatch):
    """The coverage line distinguishes "nothing stored for this window" from
    "nothing stored yet": it names the first stored value's time (a timestamp,
    claim-check policy) instead of the plain ``(no data)``."""
    _install(monkeypatch, _Client(first_points=[_point(_FEED_START_S)]))
    history = system_agent._make_sitewise_history_tool(sw, {}, "us-east-1", "historian")
    tool_input = {"entries_json": json.dumps([{"assetId": "a1", "propertyId": "p1"}]),
                  "start_iso": _WINDOW_START, "end_iso": _WINDOW_END}
    raw = history._tool_func(**tool_input)
    assert json.loads(raw)["a1_p1"]["status"] == "before_first_value"
    lines = telemetry_coverage.coverage_lines(
        "sitewise_get_asset_property_history", tool_input,
        {"status": "success", "content": [{"text": raw}]})
    assert len(lines) == 1
    assert "0 points (data starts 2026-10-03T00:00Z)" in lines[0]
    assert "(no data)" not in lines[0] and "partial" not in lines[0]


# --- Further edge cases ----------------------------------------------------


def test_probe_start_covers_a_requested_window_older_than_the_lookback(monkeypatch):
    """A window older than the fixed lookback must be inside the probed span,
    or a value found inside the lookback would be mislabeled as the first
    stored value while older data could sit before AND inside the window."""
    client = _Client(first_points=[_point(_FEED_START_S)])
    _install(monkeypatch, client)
    old_start, old_end = "2010-03-01T00:00:00Z", "2010-03-02T00:00:00Z"
    outcome = sw.get_property_history({}, "us-east-1", [{"assetId": "a1", "propertyId": "p1"}],
                                      start_iso=old_start, end_iso=old_end)["a1_p1"]
    assert outcome["status"] == "before_first_value"
    probe = client.probe_calls[0]
    assert probe["startDate"] <= dt.datetime(2010, 3, 1, tzinfo=dt.UTC)
    assert probe["startDate"].microsecond == 0
    # The outcome says what span was probed, so the claim is bounded, not absolute.
    assert outcome["probed_from"] == probe["startDate"].isoformat()


def test_probe_start_for_a_recent_window_is_the_lookback(monkeypatch):
    client = _Client(first_points=[_point(_FEED_START_S)])
    _install(monkeypatch, client)
    outcome = _read()["a1_p1"]
    probe = client.probe_calls[0]
    now = dt.datetime.now(dt.UTC)
    assert now - sw._FIRST_VALUE_LOOKBACK - dt.timedelta(minutes=5) <= probe["startDate"]
    assert probe["startDate"] <= now - sw._FIRST_VALUE_LOOKBACK
    assert outcome["probed_from"] == probe["startDate"].isoformat()


def test_probes_count_against_the_total_call_budget(monkeypatch):
    """Batch pages and probes share ``HISTORY_MAX_TOTAL_CALLS``: 24 pages plus
    8 probes could exceed the gateway window. Three entries read in one batch
    call under a budget of three leaves room for exactly two probes; the third
    series stays the plain empty list."""
    client = _Client(first_points=[_point(_FEED_START_S)])
    _install(monkeypatch, client)
    monkeypatch.setattr(sw, "HISTORY_MAX_TOTAL_CALLS", 3)
    entries = [{"assetId": f"a{i}", "propertyId": "p"} for i in range(3)]
    result = _read(entries)
    assert len(client.probe_calls) == 2
    assert result["a0_p"]["status"] == "before_first_value"
    assert result["a1_p"]["status"] == "before_first_value"
    assert result["a2_p"] == []


def test_a_spent_call_budget_makes_no_probe(monkeypatch):
    client = _Client(first_points=[_point(_FEED_START_S)])
    _install(monkeypatch, client)
    monkeypatch.setattr(sw, "HISTORY_MAX_TOTAL_CALLS", 1)
    assert _read()["a1_p1"] == []
    assert client.probe_calls == []


def test_coverage_does_not_read_an_unknown_status_wrapper_as_zero_points():
    """Only the two wrappers the reader emits (partial, before_first_value)
    are reads; any other status dict is not counted as a clean 0-point read."""
    tool_input = {"entries_json": json.dumps([{"assetId": "a1", "propertyId": "p1"}]),
                  "start_iso": _WINDOW_START, "end_iso": _WINDOW_END}
    payload = {"a1_p1": {"status": "something_else", "points": []}}
    lines = telemetry_coverage.coverage_lines(
        "sitewise_get_asset_property_history", tool_input,
        {"status": "success", "content": [{"text": json.dumps(payload)}]})
    assert lines == []
    partial = {"a1_p1": {"status": "partial", "points": [], "reason": "call_limit",
                         "requested_start": _WINDOW_START, "requested_end": _WINDOW_END}}
    lines = telemetry_coverage.coverage_lines(
        "sitewise_get_asset_property_history", tool_input,
        {"status": "success", "content": [{"text": json.dumps(partial)}]})
    assert len(lines) == 1 and "0 points (no data)" in lines[0] and "partial read" in lines[0]
