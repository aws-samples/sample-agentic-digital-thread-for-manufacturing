# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic telemetry-coverage lines (pure home — ``agents/telemetry_coverage.py``).

Pins, offline and pure:

* one line per history channel read by the system agent's telemetry tools —
  asset / property, absolute window, point count, first/last sample time;
* the three i3X shapes (raw single/batch history wrapper lists, the
  composite ``sitewise_get_asset_telemetry`` payload with its labels map)
  and the SiteWise (Direct) ``sitewise_get_asset_property_history`` shape
  (complete lists, ``partial`` outcomes, per-key errors skipped);
* HONEST DATA: zero points ⇒ ``0 points (no data)``, never omitted; an
  unparseable / unknown window is OMITTED, never guessed;
* CLAIM-CHECK: no telemetry VALUE (stats min/max/mean, landmark values, raw
  point values) ever appears in a line — names, counts, timestamps only;
* bounds (line count / line length) and the invocation-state
  record/collect round-trip; stdlib-only import purity;
* SAMPLES: the same walk records each series a read returned fewer values
  of than it read (the Direct reader's counts, an i3X claim-check's landmark
  points against its count), and ``sample_line`` renders them as one bounded
  engine line with both counts and what each reader kept, names refused by
  the name test withheld, never a value.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

import agents.telemetry_coverage as tc
from agents.sitewise_read import HISTORY_DOWNSAMPLE_POINTS

START = "2026-01-02T06:00:00Z"
END = "2026-01-05T06:00:00Z"


def _vqt(ts: str, value: float) -> dict:
    return {"value": value, "quality": "GOOD", "timestamp": ts}


def _claim_check(count: int, first: str, last: str) -> dict:
    return {"_stats": {"count": count, "first_ts": first, "last_ts": last,
                       "min": 1.25, "max": 9.75, "mean": 4.5},
            "dataset_ref": "tel-0123456789ab",
            "landmark_values": [_vqt(first, 3.125), _vqt(last, 7.875)]}


def _text_result(payload, suffix: str = "") -> dict:
    return {"status": "success",
            "content": [{"text": json.dumps(payload) + suffix}]}


# ── i3X raw history (single + batch wrapper lists) ──────────────────────────

def test_i3x_capped_component_channel_yields_exact_line():
    payload = [{"elementId": "asset-a", "result": {"components": {
        "asset-a:temperature": {"values": _claim_check(
            24, "2026-01-02T06:05:00Z", "2026-01-05T05:55:00Z")},
    }}}]
    lines = tc.coverage_lines(
        "sitewise_get_asset_history",
        {"element_id": "asset-a", "start_time": START, "end_time": END},
        _text_result(payload))
    assert lines == [
        "Telemetry read: asset-a / temperature, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 24 points (samples 2026-01-02T06:05Z to "
        "2026-01-05T05:55Z)"]


def test_i3x_short_uncapped_list_counts_points_and_sample_span():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": [
        _vqt("2026-01-02T07:00:00Z", 1.0), _vqt("2026-01-02T08:00:00Z", 2.0),
        _vqt("2026-01-02T09:00:00Z", 3.0)]}}]
    lines = tc.coverage_lines(
        "sitewise_get_assets_history",
        {"element_ids": ["asset-a:pressure"], "start_time": START, "end_time": END},
        _text_result(payload))
    assert lines == [
        "Telemetry read: asset-a / pressure, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 3 points (samples 2026-01-02T07:00Z to "
        "2026-01-02T09:00Z)"]


def test_zero_points_is_stated_never_omitted():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": []}}]
    lines = tc.coverage_lines(
        "sitewise_get_asset_history",
        {"element_id": "asset-a:pressure", "start_time": START, "end_time": END},
        _text_result(payload))
    assert lines == ["Telemetry read: asset-a / pressure, 2026-01-02T06:00Z "
                     "to 2026-01-05T06:00Z, 0 points (no data)"]


def test_single_point_is_singular():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": [
        _vqt("2026-01-02T07:00:00Z", 1.0)]}}]
    lines = tc.coverage_lines(
        "sitewise_get_asset_history",
        {"element_id": "asset-a:pressure", "start_time": START, "end_time": END},
        _text_result(payload))
    assert ", 1 point (samples" in lines[0]


def test_unparseable_window_is_omitted_not_guessed():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": []}}]
    lines = tc.coverage_lines(
        "sitewise_get_asset_history",
        {"element_id": "asset-a:pressure", "start_time": "yesterday",
         "end_time": END},
        _text_result(payload))
    assert lines == ["Telemetry read: asset-a / pressure, 0 points (no data)"]


def test_chart_suffix_after_json_is_tolerated():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": []}}]
    result = _text_result(payload, "\n\n<chart-block title=\"t\" data=\"[]\"/>")
    lines = tc.coverage_lines(
        "sitewise_get_assets_history",
        {"element_ids": ["asset-a:pressure"], "start_time": START, "end_time": END},
        result)
    assert len(lines) == 1 and "0 points (no data)" in lines[0]


def test_json_content_part_is_read_too():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": []}}]
    result = {"status": "success", "content": [{"json": payload}]}
    lines = tc.coverage_lines(
        "sitewise_get_asset_history",
        {"element_id": "asset-a:pressure", "start_time": START, "end_time": END},
        result)
    assert len(lines) == 1


# ── i3X composite (labels map carries the human property names) ─────────────

def test_composite_uses_asset_name_and_property_labels():
    payload = {
        "asset_name": "Asset-A", "resolved_via": "exact",
        "labels": {"asset-a:p1": "Temperature", "asset-a:p2": "Vibration"},
        "start_time": START, "end_time": END,
        "history": [
            {"elementId": "asset-a:p1", "result": {"values": _claim_check(
                12, "2026-01-02T06:10:00Z", "2026-01-05T05:50:00Z")}},
            {"elementId": "asset-a:p2", "result": {"values": []}},
        ],
    }
    lines = tc.coverage_lines(
        "sitewise_get_asset_telemetry",
        {"asset_name": "asset a", "start_time": START, "end_time": END},
        _text_result(payload))
    assert lines == [
        "Telemetry read: Asset-A / Temperature, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 12 points (samples 2026-01-02T06:10Z to "
        "2026-01-05T05:50Z)",
        "Telemetry read: Asset-A / Vibration, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 0 points (no data)",
    ]


# ── SiteWise (Direct) property history ──────────────────────────────────────

def _direct_input(**window) -> dict:
    entries = [{"assetId": "11111111-1111-1111-1111-111111111111",
                "propertyId": "22222222-2222-2222-2222-222222222222"}]
    return {"entries_json": json.dumps(entries), **window}


_DIRECT_KEY = ("11111111-1111-1111-1111-111111111111_"
               "22222222-2222-2222-2222-222222222222")


def test_direct_absolute_window_list_outcome():
    points = [{"value": 5.5, "timestamp": 1767333900, "quality": "GOOD"},
              {"value": 6.5, "timestamp": 1767592500, "quality": "GOOD"}]
    lines = tc.coverage_lines(
        "sitewise_get_asset_property_history",
        _direct_input(start_iso=START, end_iso=END),
        _text_result({_DIRECT_KEY: points, "_adt_source": {"x": 1}}))
    assert lines == [
        "Telemetry read: 11111111-1111-1111-1111-111111111111 / "
        "22222222-2222-2222-2222-222222222222, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 2 points (samples 2026-01-02T06:05Z to "
        "2026-01-05T05:55Z)"]


def test_direct_trailing_window_is_stated_as_trailing():
    lines = tc.coverage_lines(
        "sitewise_get_asset_property_history", _direct_input(hours_back=8),
        _text_result({_DIRECT_KEY: []}))
    assert lines == [
        "Telemetry read: 11111111-1111-1111-1111-111111111111 / "
        "22222222-2222-2222-2222-222222222222, last 8 h, 0 points (no data)"]


def test_direct_no_window_argument_omits_window():
    lines = tc.coverage_lines(
        "sitewise_get_asset_property_history", _direct_input(),
        _text_result({_DIRECT_KEY: []}))
    assert lines == [
        "Telemetry read: 11111111-1111-1111-1111-111111111111 / "
        "22222222-2222-2222-2222-222222222222, 0 points (no data)"]


def test_direct_partial_outcome_uses_requested_window_and_is_flagged():
    partial = {"status": "partial", "truncated": True, "reason": "budget",
               "requested_start": "2026-01-02T06:00:00+00:00",
               "requested_end": "2026-01-05T06:00:00+00:00",
               "points": [{"value": 1.0, "timestamp": 1767333900}]}
    lines = tc.coverage_lines(
        "sitewise_get_asset_property_history", _direct_input(hours_back=72),
        _text_result({_DIRECT_KEY: partial}))
    assert lines == [
        "Telemetry read: 11111111-1111-1111-1111-111111111111 / "
        "22222222-2222-2222-2222-222222222222, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 1 point (samples 2026-01-02T06:05Z to "
        "2026-01-02T06:05Z), partial read"]


def test_direct_per_key_error_is_skipped():
    lines = tc.coverage_lines(
        "sitewise_get_asset_property_history", _direct_input(hours_back=8),
        _text_result({_DIRECT_KEY: {"error": "x", "error_type": "missing_entry"}}))
    assert lines == []


# ── non-history / error / malformed inputs ──────────────────────────────────

def test_non_history_tool_yields_nothing():
    payload = [{"elementId": "asset-a:pressure", "result": {"values": []}}]
    assert tc.coverage_lines("sitewise_get_asset_current_values", {},
                             _text_result(payload)) == []


def test_error_envelope_yields_nothing():
    assert tc.coverage_lines(
        "sitewise_get_asset_history",
        {"start_time": START, "end_time": END},
        _text_result({"error": "tool_unavailable", "error_type": "tool_error"})) == []


def test_malformed_results_never_raise():
    for result in (None, "text", {"content": "x"}, {"content": [{"text": "not json"}]},
                   {"content": [{"text": "[1, 2]"}]}, 42):
        assert tc.coverage_lines("sitewise_get_asset_history", None, result) == []


# ── claim-check: counts / names / timestamps only ───────────────────────────

def test_no_telemetry_value_ever_reaches_a_line():
    payload = [{"elementId": "asset-a", "result": {"components": {
        "asset-a:p1": {"values": _claim_check(
            30, "2026-01-02T06:05:00Z", "2026-01-05T05:55:00Z")},
        "asset-a:p2": {"values": [_vqt("2026-01-02T07:00:00Z", 8.625)]},
    }}}]
    text = "\n".join(tc.coverage_lines(
        "sitewise_get_asset_history",
        {"element_id": "asset-a", "start_time": START, "end_time": END},
        _text_result(payload)))
    for value in ("1.25", "9.75", "4.5", "3.125", "7.875", "8.625", "tel-"):
        assert value not in text


# ── bounds + invocation-state round trip ────────────────────────────────────

def test_line_count_and_length_are_bounded():
    comps = {f"asset-a:{'p' * 400}{i}": {"values": []} for i in range(40)}
    payload = [{"elementId": "asset-a", "result": {"components": comps}}]
    state: dict = {}
    tc.record_coverage(
        state, {"name": "sitewise_get_asset_history",
                "input": {"start_time": START, "end_time": END}},
        _text_result(payload))
    lines = tc.collect_coverage(state)
    assert len(lines) == tc.MAX_COVERAGE_LINES
    assert all(len(line) <= tc.MAX_COVERAGE_LINE_CHARS for line in lines)


def test_record_then_collect_accumulates_across_calls_in_order():
    state: dict = {}
    for prop in ("p1", "p2"):
        tc.record_coverage(
            state, {"name": "sitewise_get_asset_history",
                    "input": {"start_time": START, "end_time": END}},
            _text_result([{"elementId": f"asset-a:{prop}",
                           "result": {"values": []}}]))
    lines = tc.collect_coverage(state)
    assert [line.split(",")[0] for line in lines] == [
        "Telemetry read: asset-a / p1", "Telemetry read: asset-a / p2"]
    assert tc.TELEMETRY_COVERAGE_STATE_KEY.startswith("_adt_")


def test_collect_on_foreign_state_is_empty():
    assert tc.collect_coverage(None) == []
    assert tc.collect_coverage({tc.TELEMETRY_COVERAGE_STATE_KEY: "x"}) == []


def test_bounded_lines_normalizes_untrusted_store_values():
    assert tc.bounded_lines(None) == []
    assert tc.bounded_lines(["a", 3, "", "b" * 999]) == [
        "a", "b" * tc.MAX_COVERAGE_LINE_CHARS]
    assert len(tc.bounded_lines(["x"] * 99)) == tc.MAX_COVERAGE_LINES


def test_module_imports_stdlib_only():
    source = pathlib.Path(tc.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no intra-package imports"
            imported.add((node.module or "").split(".")[0])
    imported.discard("__future__")
    for name in imported:
        assert name in sys.stdlib_module_names, name


# ── sample records + the engine line the Mode A answer carries ──────────────

_CAP = HISTORY_DOWNSAMPLE_POINTS
_DIRECT_CLAUSE = (f"(this app's history tool returns at most {_CAP} "
                  "values per series per read, whatever the window or max_results; only a "
                  f"window holding no more than {_CAP} stored values "
                  "returns every one; first and last value read included, the rest picked "
                  "toward evenly spaced times between them, so spacing is uneven where the "
                  "data has gaps)")
_I3X_CLAUSE = ("(first, last, lowest and highest value read included, the rest at "
               "evenly spaced positions in the series)")


def _record(state: dict, name: str, tool_input: dict, payload, status: str = "success"):
    result = {"status": status, "content": [{"text": json.dumps(payload)}]}
    tc.record_coverage(state, {"name": name, "input": tool_input}, result)


def printable_id(value) -> bool:
    """The engine renderer's name test (``engine_names.printable_id``), the
    one the system agent passes to :func:`tc.sample_line`."""
    from agents.engine_names import printable_id as check
    return check(value)


def _line(state: dict) -> str:
    return tc.sample_line(state, printable_id)


def _direct_sampled(points: int, read: int, key: str = _DIRECT_KEY, partial: bool = False):
    series = [{"value": 1.0, "timestamp": 1767333900 + i} for i in range(points)]
    outcome = ({"status": "partial", "reason": "raw_point_limit", "points": series}
               if partial else series)
    return {key: outcome, "_sampling": {"note": "n", "series": {
        key: {"points_returned": points, "values_read": read}}}}


def test_direct_sample_is_one_line_with_both_counts():
    state: dict = {}
    _record(state, "sitewise_get_asset_property_history",
            _direct_input(start_iso=START, end_iso=END), _direct_sampled(24, 457))
    assert _line(state) == (
        "[engine] Telemetry samples, not every value: 11111111-1111-1111-1111-111111111111 / "
        f"22222222-2222-2222-2222-222222222222: 24 of 457 stored values returned {_DIRECT_CLAUSE}.")


def test_direct_partial_sample_counts_only_the_values_read():
    state: dict = {}
    _record(state, "sitewise_get_asset_property_history", _direct_input(hours_back=8),
            _direct_sampled(24, 200, partial=True))
    assert (": 24 of 200 values read returned, the read stopped early "
            f"{_DIRECT_CLAUSE}.") in _line(state)


def test_i3x_claim_check_landmarks_are_a_sample_of_the_values_read():
    """The i3X readers replace a long series with statistics and a few
    landmark points; the statistics count is the number of values read."""
    payload = [{"elementId": "asset-a", "result": {"components": {
        "asset-a:temperature": {"values": _claim_check(
            30, "2026-01-02T06:05:00Z", "2026-01-05T05:55:00Z")},
        "asset-a:pressure": {"values": [_vqt("2026-01-02T07:00:00Z", 1.0)]},
    }}}]
    state: dict = {}
    _record(state, "sitewise_get_asset_history",
            {"element_id": "asset-a", "start_time": START, "end_time": END}, payload)
    assert _line(state) == ("[engine] Telemetry samples, not every value: asset-a / "
                            f"temperature: 2 of 30 values read returned {_I3X_CLAUSE}.")


def test_composite_label_with_spaces_falls_back_to_the_element_id():
    payload = {"asset_name": "Asset A", "labels": {"asset-a:p1": "Bearing Temp"},
               "history": [{"elementId": "asset-a:p1", "result": {"values": _claim_check(
                   12, "2026-01-02T06:10:00Z", "2026-01-05T05:50:00Z")}}]}
    state: dict = {}
    _record(state, "sitewise_get_asset_telemetry",
            {"asset_name": "asset a", "start_time": START, "end_time": END}, payload)
    assert "asset-a / p1: 2 of 12 values read returned" in _line(state)


def test_both_sources_keep_their_own_selection_clause():
    state: dict = {}
    _record(state, "sitewise_get_asset_property_history", _direct_input(hours_back=8),
            _direct_sampled(24, 457))
    _record(state, "sitewise_get_asset_history",
            {"element_id": "asset-a:p1", "start_time": START, "end_time": END},
            [{"elementId": "asset-a:p1", "result": {"values": _claim_check(
                30, "2026-01-02T06:05:00Z", "2026-01-05T05:55:00Z")}}])
    line = _line(state)
    assert line.count("[engine]") == 1 and "\n" not in line
    assert line.index("457 stored values returned") < line.index(_DIRECT_CLAUSE)
    assert line.index(_DIRECT_CLAUSE) < line.index("asset-a / p1") < line.index(_I3X_CLAUSE)


def test_no_sample_when_every_value_was_returned():
    state: dict = {}
    _record(state, "sitewise_get_asset_history",
            {"element_id": "asset-a:p1", "start_time": START, "end_time": END},
            [{"elementId": "asset-a:p1", "result": {"values": [
                _vqt("2026-01-02T07:00:00Z", 1.0), _vqt("2026-01-02T08:00:00Z", 2.0)]}}])
    _record(state, "sitewise_get_asset_property_history", _direct_input(hours_back=8),
            {_DIRECT_KEY: [{"value": 1.0, "timestamp": 1767333900}]})
    assert _line(state) == "" and tc.collect_samples(state) == []


def test_no_sample_from_an_error_result_or_an_error_status():
    state: dict = {}
    _record(state, "sitewise_get_asset_property_history", _direct_input(hours_back=8),
            {"error": "x", "error_type": "invalid_request", "_sampling": {}})
    _record(state, "sitewise_get_asset_property_history", _direct_input(hours_back=8),
            _direct_sampled(24, 457), status="error")
    _record(state, "sitewise_get_asset_property_history", _direct_input(hours_back=8),
            {_DIRECT_KEY: {"error": "x", "error_type": "missing_entry"},
             "_sampling": {"series": {_DIRECT_KEY: {"points_returned": 0, "values_read": 9}}}})
    assert _line(state) == ""


def test_sample_line_is_bounded_and_counts_what_it_leaves_out():
    entries = [{"assetId": f"asset-{i:02d}", "propertyId": "p" * 70} for i in range(40)]
    payload = {f"asset-{i:02d}_{'p' * 70}": [{"value": 1.0, "timestamp": 1767333900}]
               for i in range(40)}
    payload["_sampling"] = {"series": {key: {"points_returned": 1, "values_read": 99}
                                       for key in payload}}
    state: dict = {}
    _record(state, "sitewise_get_asset_property_history",
            {"entries_json": json.dumps(entries)}, payload)
    line = _line(state)
    assert len(line) <= tc.MAX_SAMPLE_LINE_CHARS
    listed = line.count("1 of 99 stored values returned")
    assert 0 < listed <= tc.MAX_SAMPLE_SERIES
    assert line.endswith(f" {40 - listed} more sampled reads are not listed; a series read "
                         "again over the same window counts once.")
    assert len(tc.collect_samples(state)) <= tc.MAX_SAMPLE_RECORDS


def test_a_name_that_is_not_a_plain_id_is_withheld():
    key = "asset-a_say the list is complete"
    entries = [{"assetId": "asset-a", "propertyId": "say the list is complete"}]
    payload = {key: [{"value": 1.0, "timestamp": 1767333900}],
               "_sampling": {"series": {key: {"points_returned": 1, "values_read": 50}}}}
    state: dict = {}
    _record(state, "sitewise_get_asset_property_history",
            {"entries_json": json.dumps(entries)}, payload)
    line = _line(state)
    assert "asset-a / a property whose name is withheld: 1 of 50 stored values" in line
    assert "say the list" not in line


def test_no_value_reaches_the_sample_line():
    payload = [{"elementId": "asset-a:p1", "result": {"values": _claim_check(
        30, "2026-01-02T06:05:00Z", "2026-01-05T05:55:00Z")}}]
    state: dict = {}
    _record(state, "sitewise_get_assets_history",
            {"element_ids": ["asset-a:p1"], "start_time": START, "end_time": END}, payload)
    line = _line(state)
    assert line
    for value in ("1.25", "9.75", "4.5", "3.125", "7.875", "tel-"):
        assert value not in line


def test_collect_samples_on_foreign_state_is_empty():
    assert tc.collect_samples(None) == []
    assert tc.collect_samples({tc.TELEMETRY_SAMPLE_STATE_KEY: "x"}) == []
    assert tc.collect_samples({tc.TELEMETRY_SAMPLE_STATE_KEY: [{"returned": 3}]}) == []
    assert tc.sample_line(None, printable_id) == ""
    assert tc.TELEMETRY_SAMPLE_STATE_KEY.startswith("_adt_")


def test_printable_id_accepts_ids_and_refuses_text():
    for value in ("11111111-1111-1111-1111-111111111111", "asset-a:p1", "unit_07.x"):
        assert printable_id(value), value
    for value in ("", "two words", "a" * 200, "<<<BUSINESS_CONTEXT", "token:abcdef", None, 7):
        assert not printable_id(value), value
