# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A SiteWise (Direct) history read says when its points are a sample.

The reader keeps at most ``HISTORY_DOWNSAMPLE_POINTS`` evenly spaced points
per series, however many values the window holds. Without a statement of
that in the tool result, an agent asked to list every reading in a window
presents the returned points as all of them. The result now carries a
top-level sampling summary naming, per sampled series, how many points it
returned and how many values it read, with a fixed note saying what the two
counts mean. A series whose points are every value read gets no entry.

The summary key starts with ``_`` so the readers that walk the per-series
outcomes (coverage lines, chart channels, the capture hook's cut check) skip
it, and the tool keeps it out of the source metadata's item count. Ids here
are opaque and arbitrary: nothing depends on any dataset's names.

The counts reach the system agent's model in the tool result, and the
orchestrator only through the Mode A answer. So the capture hook records
them and the answer carries one engine line naming each sampled series with
both counts (``telemetry_coverage.sample_line``), whatever the answer says.

Fully offline (no AWS, no sockets).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from types import SimpleNamespace

import agents.system_agent as sa
from agents import aws_session
from agents import sitewise_read as sw
from agents import telemetry_coverage as tc
from agents import tool_result_capture as trc

_KEY = "_sampling"
_ASSET = "unit-0c4f"
_PROPS = ("chan-alpha", "chan-beta")
_START = dt.datetime(2026, 3, 2, 8, 0, tzinfo=dt.UTC)
_END = _START + dt.timedelta(hours=1)


class _SeriesClient:
    """Serves ``counts[propertyId]`` ascending GOOD points per entry, evenly
    spread over the hour, on a single page."""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def _points(self, prop: str) -> list[dict]:
        n = self.counts.get(prop, 0)
        step = 3600 / max(n, 1)
        base = int(_START.timestamp())
        return [{"value": {"doubleValue": 1.0 + i / 1000},
                 "timestamp": {"timeInSeconds": base + int(i * step)},
                 "quality": "GOOD"} for i in range(n)]

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        return {"successEntries": [{"entryId": e["entryId"],
                                    "assetPropertyValueHistory": self._points(e["propertyId"])}
                                   for e in entries],
                "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value_history(self, **kwargs):
        return {"assetPropertyValueHistory": []}


def _patch(monkeypatch, counts: dict[str, int]) -> None:
    client = _SeriesClient(counts)
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)


def _entries(*props: str) -> list[dict]:
    return [{"assetId": _ASSET, "propertyId": p} for p in props]


def _read(*props: str) -> dict:
    return sw.get_property_history({}, "us-east-1", _entries(*props),
                                   start_iso=_START.isoformat(), end_iso=_END.isoformat())


def _history_tool():
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={},
                                          system_id="sys-sampled")
    return next(t for t in tools if t.tool_name == "sitewise_get_asset_property_history")


def _tool_input(*props: str) -> dict:
    return {"entries_json": json.dumps(_entries(*props)),
            "start_iso": _START.isoformat(), "end_iso": _END.isoformat()}


# --------------------------------------------------------------------------- #
# Reader: the summary names both counts for every sampled series
# --------------------------------------------------------------------------- #

def test_reader_says_the_points_are_a_sample_of_the_values_read(monkeypatch):
    _patch(monkeypatch, {_PROPS[0]: 720})
    out = _read(_PROPS[0])
    key = f"{_ASSET}_{_PROPS[0]}"
    assert len(out[key]) == sw.HISTORY_DOWNSAMPLE_POINTS
    assert out[_KEY]["series"] == {
        key: {"points_returned": sw.HISTORY_DOWNSAMPLE_POINTS, "values_read": 720}}
    note = out[_KEY]["note"]
    assert "sample" in note and "points_returned" in note and "values_read" in note
    # Fixed text whose only figure is the reader's own cap, so every other
    # number comes from the counts.
    assert set(re.findall(r"\d+", note)) == {str(sw.HISTORY_DOWNSAMPLE_POINTS)}


def test_only_series_with_values_left_out_are_listed(monkeypatch):
    _patch(monkeypatch, {_PROPS[0]: 300, _PROPS[1]: 7})
    out = _read(*_PROPS)
    assert list(out[_KEY]["series"]) == [f"{_ASSET}_{_PROPS[0]}"]
    assert len(out[f"{_ASSET}_{_PROPS[1]}"]) == 7


def test_no_summary_when_every_value_read_is_returned(monkeypatch):
    # Guard: a series at or under the cap is returned whole and says nothing.
    _patch(monkeypatch, {_PROPS[0]: sw.HISTORY_DOWNSAMPLE_POINTS})
    out = _read(_PROPS[0])
    assert _KEY not in out
    assert len(out[f"{_ASSET}_{_PROPS[0]}"]) == sw.HISTORY_DOWNSAMPLE_POINTS


def test_partial_read_counts_only_the_values_read_before_the_cut(monkeypatch):
    _patch(monkeypatch, {_PROPS[0]: 720})
    monkeypatch.setattr(sw, "HISTORY_MAX_RAW_POINTS_PER_CHUNK", 200)
    out = _read(_PROPS[0])
    key = f"{_ASSET}_{_PROPS[0]}"
    assert out[key]["status"] == "partial"
    assert out[_KEY]["series"][key] == {
        "points_returned": len(out[key]["points"]), "values_read": 200}
    assert "partial" in out[_KEY]["note"]


def test_summary_is_bounded_by_the_entry_cap(monkeypatch):
    props = [f"chan-{i:03d}" for i in range(sw.HISTORY_MAX_ENTRIES)]
    _patch(monkeypatch, {p: 40 for p in props})
    out = _read(*props)
    assert len(out[_KEY]["series"]) == sw.HISTORY_MAX_ENTRIES
    assert set(out[_KEY]) == {"note", "series"}


# --------------------------------------------------------------------------- #
# The tool result the system agent's model reads
# --------------------------------------------------------------------------- #

def test_history_tool_result_carries_the_sample_counts(monkeypatch):
    _patch(monkeypatch, {_PROPS[0]: 720, _PROPS[1]: 5})
    raw = _history_tool()._tool_func(**_tool_input(*_PROPS))
    payload = json.loads(raw)
    key = f"{_ASSET}_{_PROPS[0]}"
    assert payload[_KEY]["series"] == {
        key: {"points_returned": sw.HISTORY_DOWNSAMPLE_POINTS, "values_read": 720}}
    # The summary is not a reading: the source metadata counts the two series.
    assert payload["_adt_source"]["item_count"] == 2


def test_summary_leaves_coverage_lines_charts_and_cut_check_unchanged(monkeypatch):
    # Guard: readers of the per-series outcomes skip the summary key.
    _patch(monkeypatch, {_PROPS[0]: 720})
    raw = _history_tool()._tool_func(**_tool_input(_PROPS[0]))
    with_key = json.loads(raw)
    without = {k: v for k, v in with_key.items() if k != _KEY}
    assert _KEY in with_key

    def result(payload):
        return {"status": "success", "content": [{"text": json.dumps(payload)}]}

    name = "sitewise_get_asset_property_history"
    tool_input = _tool_input(_PROPS[0])
    assert (tc.coverage_lines(name, tool_input, result(with_key))
            == tc.coverage_lines(name, tool_input, result(without)))
    assert trc.result_row_cut(result(with_key)) == trc.result_row_cut(result(without))
    assert (sa._sitewise_chart_channels(with_key, _entries(_PROPS[0]))
            == sa._sitewise_chart_channels(without, _entries(_PROPS[0])))


def test_mode_b_property_history_result_carries_the_sample_counts(monkeypatch):
    _patch(monkeypatch, {_PROPS[0]: 500})
    request = json.dumps({"op": "property_history", "entries": _entries(_PROPS[0]),
                          "start_iso": _START.isoformat(), "end_iso": _END.isoformat()})
    res = sa._mode_b_run_query("SiteWise (Direct)", request, "", None, {})
    assert res["result"][_KEY]["series"][f"{_ASSET}_{_PROPS[0]}"]["values_read"] == 500


# --------------------------------------------------------------------------- #
# The Mode A answer: the orchestrator gets the counts as an engine line
# --------------------------------------------------------------------------- #

_HISTORY_TOOL = "sitewise_get_asset_property_history"
#: What the system agent says when nothing tells it the points are a sample.
_CLAIM = "Here is the complete, unsampled list of readings for the window."
_SAMPLE_LINE = (
    f"[engine] Telemetry samples, not every value: {_ASSET} / {_PROPS[0]}: 24 of 457 "
    f"stored values returned (this app's history tool returns at most "
    f"{sw.HISTORY_DOWNSAMPLE_POINTS} values per series per read, whatever the window or "
    f"max_results; only a window holding no more than {sw.HISTORY_DOWNSAMPLE_POINTS} stored "
    "values returns every one; first and last value read included, the rest picked toward "
    "evenly spaced times between them, so spacing is uneven where the data has gaps).")


def _capture(state: dict, raw: str, tool_input: dict, *, status: str = "success") -> None:
    """Run one history result through the system agent's capture hook, as a
    completed tool call does live."""
    event = SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": _HISTORY_TOOL, "input": tool_input},
        invocation_state=state, result={"status": status, "content": [{"text": raw}]},
        exception=None, retry=False, agent=SimpleNamespace(messages=[]))
    trc.InvocationToolResultCapture(system_id="sys-sampled")._on_after_tool_call(event)


def _sampled_read(monkeypatch, counts: dict[str, int]) -> tuple[str, dict]:
    _patch(monkeypatch, counts)
    tool_input = _tool_input(*counts)
    return _history_tool()._tool_func(**tool_input), tool_input


def _engine_tail(state: dict, answer: str = _CLAIM) -> str:
    out = sa._append_engine_facts(answer, state)
    assert out.startswith(answer), "the answer itself was changed"
    return out[len(answer):]


def _sample_lines(tail: str) -> list[str]:
    return [line for line in tail.splitlines() if "stored values returned" in line
            or "values read returned" in line]


def test_mode_a_answer_carries_one_sample_line_with_both_counts(monkeypatch):
    """Reproduction: the history read returned 24 of 457 stored values for
    one series and every value for the other; the answer calls the list
    complete. The orchestrator gets one engine line naming the sampled series
    with both counts, and nothing about the series returned whole."""
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457, _PROPS[1]: 7})
    state: dict = {}
    _capture(state, raw, tool_input)
    lines = _sample_lines(_engine_tail(state))
    assert lines == [_SAMPLE_LINE]
    assert _PROPS[1] not in lines[0]
    # Counts and names only: no reading value is printed.
    assert not re.search(r"\d\.\d", lines[0])


def test_sample_line_survives_a_json_answer(monkeypatch):
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457})
    state: dict = {}
    _capture(state, raw, tool_input)
    answer = json.dumps({"readings": [{"value": 1.5}]})
    note = json.loads(sa._append_engine_facts(answer, state))[trc.RESULT_CUT_NOTE_KEY]
    assert _SAMPLE_LINE in note


def test_no_line_when_every_value_read_was_returned(monkeypatch):
    # Guard: a series at the cap is returned whole, so there is no sample.
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: sw.HISTORY_DOWNSAMPLE_POINTS})
    state: dict = {}
    _capture(state, raw, tool_input)
    assert _sample_lines(_engine_tail(state)) == []


def test_no_line_when_the_sampling_summary_is_absent(monkeypatch):
    # Guard: a reader that reports no counts gets no line; nothing is guessed
    # from the number of points alone.
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457})
    payload = json.loads(raw)
    payload.pop(_KEY)
    state: dict = {}
    _capture(state, json.dumps(payload), tool_input)
    assert _sample_lines(_engine_tail(state)) == []


def test_no_line_when_the_summary_counts_are_equal_or_disagree(monkeypatch):
    # Guard: only a summary whose returned count matches the points in the
    # result and is below the values read is reported.
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457})
    key = f"{_ASSET}_{_PROPS[0]}"
    for counts in ({"points_returned": 24, "values_read": 24},
                   {"points_returned": 20, "values_read": 457},
                   {"points_returned": "24", "values_read": 457}):
        payload = json.loads(raw)
        payload[_KEY]["series"][key] = counts
        state: dict = {}
        _capture(state, json.dumps(payload), tool_input)
        assert _sample_lines(_engine_tail(state)) == [], counts


def test_no_line_when_the_result_was_an_error(monkeypatch):
    # Guard: an error envelope, or a tool result marked as an error, is not a read.
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457})
    state: dict = {}
    _capture(state, json.dumps({"error": "window too large", "error_type": "invalid_request"}),
             tool_input)
    _capture(state, raw, tool_input, status="error")
    assert _sample_lines(_engine_tail(state)) == []


def test_sample_line_is_fail_soft(monkeypatch):
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457})
    state: dict = {}
    _capture(state, raw, tool_input)

    def boom(*_a, **_k):
        raise RuntimeError("sample line bug")
    monkeypatch.setattr(tc, "sample_line", boom)
    out = sa._append_engine_facts(_CLAIM, state)
    # No sample line, and the answer itself is unchanged; the window line of
    # the same read (telemetry_window) still rides.
    assert out.startswith(_CLAIM) and "stored values returned" not in out
    assert "[engine] Telemetry samples" not in out


def test_sample_line_stays_on_the_surfaces_a_person_reads(monkeypatch):
    """The line quotes counts the reader measured, not a value the answer
    made up, so it is not an orchestrator-only line: the stored trace and the
    tool summary show it too."""
    from agents.internal_lines import is_marker_line, public_text
    raw, tool_input = _sampled_read(monkeypatch, {_PROPS[0]: 457})
    state: dict = {}
    _capture(state, raw, tool_input)
    answer = sa._append_engine_facts(_CLAIM, state)
    assert public_text(answer) == answer
    assert not is_marker_line(_SAMPLE_LINE)


def test_coverage_reader_uses_the_readers_own_summary_key():
    assert tc.DIRECT_SAMPLING_KEY == sw.HISTORY_SAMPLING_KEY


class _GapClient:
    """``n`` values a few seconds apart, then a long gap, then the rest, for
    every requested entry: the reducer's time grid collapses inside the gap,
    so fewer than ``HISTORY_DOWNSAMPLE_POINTS`` points come back."""

    def __init__(self, before: int, after: int, step: int = 6, gap: int = 1200):
        base = int(_START.timestamp()) + 4
        self.stamps = ([base + step * i for i in range(before)]
                       + [base + step * (before - 1) + gap + step * i for i in range(after)])

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        points = [{"value": {"doubleValue": 2.5}, "timestamp": {"timeInSeconds": t},
                   "quality": "GOOD"} for t in self.stamps]
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValueHistory": points}
                                   for e in entries],
                "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value_history(self, **kwargs):
        return {"assetPropertyValueHistory": []}


#: What the reducer guarantees about the points it keeps, in the words both
#: the reader's note and the engine line use.
_SELECTION = ("picked toward evenly spaced times between them, so spacing is uneven "
              "where the data has gaps")


def test_reader_note_states_only_what_the_reducer_keeps_after_a_gap(monkeypatch):
    """After a long gap the reducer returns fewer than its cap, unevenly
    spaced: several evenly spaced target times pick the same stored point.
    The note the system agent reads must not call that an evenly spaced
    sample; it keeps the first-and-last guarantee, as the engine line does."""
    client = _GapClient(57, 400)
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)
    out = _read(_PROPS[0])
    points = out[f"{_ASSET}_{_PROPS[0]}"]
    assert len(points) < sw.HISTORY_DOWNSAMPLE_POINTS
    assert points[0]["timestamp"] == client.stamps[0]
    assert points[-1]["timestamp"] == client.stamps[-1]
    steps = {b["timestamp"] - a["timestamp"] for a, b in zip(points, points[1:])}
    assert max(steps) > 2 * min(steps), "the fixture did not produce uneven spacing"
    note = " ".join(out[_KEY]["note"].split())
    assert "evenly spaced sample" not in note
    assert "The first and last value read are always included" in note
    assert _SELECTION in note
    assert _SELECTION in tc._SAMPLE_CLAUSES["direct"]


def test_every_series_of_a_compact_read_with_a_gap_gets_its_own_counts(monkeypatch):
    """Three series read with the compact asset and property id arrays, each
    with a long gap in its window: the line names each series with the number
    of points the reader actually returned and the number it read."""
    client = _GapClient(57, 400)
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)
    props = ("chan-x", "chan-y", "chan-z")
    tool_input = {"asset_ids_json": json.dumps([_ASSET]),
                  "property_ids_json": json.dumps(list(props)),
                  "start_iso": _START.isoformat(), "end_iso": _END.isoformat()}
    raw = _history_tool()._tool_func(**tool_input)
    returned = {len(json.loads(raw)[f"{_ASSET}_{p}"]) for p in props}
    assert len(returned) == 1 and returned.pop() < sw.HISTORY_DOWNSAMPLE_POINTS
    state: dict = {}
    _capture(state, raw, tool_input)
    [line] = _sample_lines(_engine_tail(state))
    shown = len(json.loads(raw)[f"{_ASSET}_{props[0]}"])
    for prop in props:
        assert f"{_ASSET} / {prop}: {shown} of 457 stored values returned" in line
    assert line.count("[engine]") == 1 and "2.5" not in line
