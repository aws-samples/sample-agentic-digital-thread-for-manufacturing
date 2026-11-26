# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guaranteed charts on the SiteWise (Direct) history tool.

The orchestrator prompt says telemetry charts come from the owning system and
are never hand-built, but ``sitewise_get_asset_property_history`` returned
readings only, so a trend answer either had no chart or one the model typed
itself (points dropped, values unchecked). The tool now renders the charts
from the SAME payload it returns: no second SiteWise read, no model in the
loop.

Pinned here:

* the readings JSON is the tool result's FIRST text part, byte-identical to
  the chart-free result, and the rendered tags ride a SECOND text part, so
  every whole-text JSON reader (telemetry coverage lines, the capture hook's
  metadata and row-cut tally, the integrity evidence capture) reads exactly
  what it read before;
* one chart per asset per unit group; each chart carries every returned
  numeric GOOD point, labelled by its own timestamp, with the payload's exact
  values;
* no chart for an empty, error, before-first-value, partial-without-points or
  single-point series (the tool result is then the unchanged JSON string);
* the delivery path: the inner hook withholds the tags from the model, the
  Mode A relay carries them across the query boundary, the orchestrator hook
  queues them and the runtime drain injects each exactly once; the trace
  summary of the query call still reads the answer;
* enforce mode delivers nothing and tells the inner model so;
* a chart failure never breaks the read;
* labels keep every distinct timestamp distinct across a shared axis
  (minutes, then seconds, with the date and year when the window spans them,
  then a lossless ISO time), and equal timestamps share one slot;
* chart names and units come from a bounded, system-scoped, cached graph read
  with short timeouts and one attempt; a slow graph costs the charts, not the
  readings (a series with no resolved name is not drawn; see the names-only
  tests in ``test_sitewise_chart_intent_and_names.py``);
* the chart cap is stated in the first delivered chart's title, which reaches
  the injected answer text.

These reads all run inside a chart-intent Mode A call (set by the autouse
fixture), so the ``_wants_chart`` gate is open; the gate itself and the
names-only / printable-token rules are pinned in
``test_sitewise_chart_intent_and_names.py``.

Fully synthetic (no dataset literals): the reader and the knowledge-graph
lookup are monkeypatched; no AWS, no network.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import agents.sitewise_read as sitewise_read
import agents.system_agent as sa
from agents import telemetry_coverage, tool_result_capture
from agents.chart_note import strip_chart_notes
from agents.orchestrator import ChartRedactionHook, drain_pending_charts
from agents.response_integrity_capture import (
    capture_trusted_tool_result, initialize_integrity_state,
)
from agents.response_integrity_contracts import PRIVATE_SOURCE_STATE_KEY
from agents.tool_result_summary import summarize_tool_result

_FIXED_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
_T0 = int(datetime(2026, 1, 5, 11, 0, tzinfo=timezone.utc).timestamp())
_TAG = re.compile(r"<chart-block\b[^>]*/>")
#: The real graph-read seam, captured before the fixture replaces it.
_REAL_GRAPH_READ = getattr(sa, "_sitewise_chart_graph_read", None)

_ASSET = "asset-1"
_ASSET_2 = "asset-2"
_VX, _VY, _TEMP = "prop-vx", "prop-vy", "prop-temp"
#: Two temperature channels that resolve to names sharing the generic
#: "temp" kind keyword (no unit), so they group onto ONE shared axis; the
#: label tests read slots/data by position, not the dataset labels.
_T1, _T2 = "temp-1", "temp-2"
_GRAPH = {
    _ASSET: ("Unit A", ""), _ASSET_2: ("Unit B", ""),
    _VX: ("vib_x", "mm/s"), _VY: ("vib_y", "mm/s"), _TEMP: ("coolant_temp", "°C"),
    _T1: ("coolant_temp_1", ""), _T2: ("coolant_temp_2", ""),
}


def _clear_chart_cache():
    clear = getattr(sa, "_sitewise_chart_cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Pinned clock + metadata stamp (the real stamp carries a random id) and
    a synthetic knowledge-graph read seam; nothing reaches boto3.

    A chart-intent Mode A call is set for the whole module: every read in this
    file exercises the delivery path for a question that ASKS for a chart, so
    the intent gate (``system_agent._wants_chart``) is open. Tests that pin
    the no-chart paths (nothing to draw, enforce mode) stay no-chart for their
    own reasons; the intent gate itself is covered in
    ``test_sitewise_chart_intent_and_names.py``."""
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(sa, "utc_now", lambda: _FIXED_NOW)
    monkeypatch.setattr(sa, "attach_source_metadata",
                        lambda payload, **meta: {**payload, "_meta": meta})
    _graph(monkeypatch, _GRAPH)
    # No runtime chart bit here: the gate falls back to the Mode A question
    # below. Reset the contextvar so a prior test that set it cannot leak in.
    from agents import runtime_asgi
    chart_token = runtime_asgi.chart_intent_var.set(None)
    token = sa._MODE_A_CALL.set(sa._ModeACall("show me the trend chart over time"))
    try:
        yield
    finally:
        sa._MODE_A_CALL.reset(token)
        runtime_asgi.chart_intent_var.reset(chart_token)
        _clear_chart_cache()


def _graph(monkeypatch, known, system="sys-x"):
    """Answer graph reads from ``known`` for ``system``; returns the reads."""
    reads = []

    def fake_read(cypher, parameters):
        reads.append((cypher, parameters))
        if parameters.get("sys") != system:
            return []
        return [{"id": key, "name": name, "unit": unit}
                for key, (name, unit) in known.items() if key in parameters["ids"]]

    monkeypatch.setattr(sa, "_sitewise_chart_graph_read", fake_read, raising=False)
    _clear_chart_cache()
    return reads


def _points(values, start=_T0, step=150, quality="GOOD"):
    out = []
    for index, value in enumerate(values):
        stamp = start + index * step
        out.append({"value": value, "timestamp": stamp,
                    "time": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(),
                    "quality": quality})
    return out


def _series(n, base=1.0, start=_T0, step=150):
    return _points([round(base + 0.0137 * i + (0.001 * i) ** 2, 6) for i in range(n)],
                   start=start, step=step)


def _install(monkeypatch, payload):
    calls = []

    def fake_history(cfg, region, entries, **kwargs):
        calls.append(entries)
        return payload

    monkeypatch.setattr(sitewise_read, "get_property_history", fake_history)
    return calls


def _tool():
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={},
                                          system_id="sys-x")
    return next(t for t in tools if t.tool_name == "sitewise_get_asset_property_history")


def _entries(*pairs):
    return json.dumps([{"assetId": a, "propertyId": p} for a, p in pairs])


def _read(monkeypatch, payload, *pairs, **window):
    _install(monkeypatch, payload)
    return _tool()._tool_func(entries_json=_entries(*pairs), **(window or {"hours_back": 1}))


def _chart_free(payload):
    """The tool's result before this change: the readings JSON alone."""
    return sa._sitewise_json({**payload, "_meta": {
        "boundary": "sitewise", "system_id": "sys-x", "queried_at": _FIXED_NOW}})


def _parts(out):
    assert isinstance(out, dict), "a charted read returns a two-part tool result"
    assert out["status"] == "success"
    texts = [part["text"] for part in out["content"]]
    assert len(texts) == 2
    return texts


def _charts(text):
    charts = []
    for tag in _TAG.findall(text):
        charts.append({
            "title": re.search(r'title="([^"]*)"', tag).group(1),
            "labels": json.loads(re.search(r"labels='([^']*)'", tag).group(1)),
            "datasets": json.loads(re.search(r"datasets='([^']*)'", tag).group(1)),
        })
    return charts


def _hhmm(point):
    """The chart label for a reading: its UTC instant to the minute, ISO with Z."""
    return datetime.fromtimestamp(point["timestamp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _hhmmss(point):
    """The chart label for a reading to the second, ISO with Z."""
    return datetime.fromtimestamp(point["timestamp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# 1. Shape: unchanged JSON part + a chart part, one chart per unit group
# --------------------------------------------------------------------------- #
def test_history_read_carries_one_chart_per_unit_group(monkeypatch):
    payload = {f"{_ASSET}_{_VX}": _series(24, 1.9), f"{_ASSET}_{_VY}": _series(24, 1.7),
               f"{_ASSET}_{_TEMP}": _series(24, 21.0)}
    calls = _install(monkeypatch, payload)
    out = _tool()._tool_func(entries_json=_entries((_ASSET, _VX), (_ASSET, _VY), (_ASSET, _TEMP)),
                             hours_back=1)
    assert len(calls) == 1                              # no second SiteWise read
    readings, chart_text = _parts(out)
    assert readings == _chart_free(payload)            # JSON part byte-identical
    charts = _charts(chart_text)
    assert [c["title"].split(",")[0] for c in charts] == [
        "Unit A: vib_x", "Unit A: coolant_temp (°C)"]
    vib, temp = charts
    assert vib["title"].startswith("Unit A: vib_x, vib_y (mm/s), 2026-01-05 11:00 UTC to 2026-01-05")
    assert vib["title"].endswith(" UTC")
    assert [d["label"] for d in vib["datasets"]] == ["vib_x", "vib_y"]
    assert [d["label"] for d in temp["datasets"]] == ["coolant_temp"]


def test_each_chart_plots_every_returned_point_with_the_payload_values(monkeypatch):
    vx, vy = _series(24, 1.9), _series(24, 1.7)
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": vx, f"{_ASSET}_{_VY}": vy},
                (_ASSET, _VX), (_ASSET, _VY))
    (chart,) = _charts(_parts(out)[1])
    assert chart["labels"] == [_hhmm(p) for p in vx]   # 24 labels, own timestamps
    assert chart["datasets"][0]["data"] == [p["value"] for p in vx]   # exact values
    assert chart["datasets"][1]["data"] == [p["value"] for p in vy]


def test_misaligned_channels_keep_every_point_on_one_axis(monkeypatch):
    """Two channels sampled 20 s apart, some in the same minute: every point
    is still drawn under its own time (to the second, since minutes would
    merge different readings), with the line spanning the other channel's
    slots."""
    vx = _series(6, 1.0, start=_T0 + 50)                 # hh:mm:50
    vy = _series(6, 2.0, start=_T0 + 70)                 # 20 s later: next minute
    out = _read(monkeypatch, {f"{_ASSET}_{_T1}": vx, f"{_ASSET}_{_T2}": vy},
                (_ASSET, _T1), (_ASSET, _T2))
    (chart,) = _charts(_parts(out)[1])
    for dataset, series in zip(chart["datasets"], (vx, vy)):
        plotted = [(label, value) for label, value in zip(chart["labels"], dataset["data"])
                   if value is not None]
        assert plotted == [(_hhmmss(p), p["value"]) for p in series]
        assert dataset["spanGaps"] is True
    assert chart["labels"] == sorted(chart["labels"])


def test_two_channels_at_different_seconds_in_the_same_minutes_keep_four_slots(monkeypatch):
    """Minute labels would put 11:00:05 and 11:00:55 in one slot and draw two
    readings as simultaneous; each distinct time keeps its own slot."""
    vx = _points([1.0, 1.1], start=_T0 + 5, step=60)    # 11:00:05, 11:01:05
    vy = _points([2.0, 2.1], start=_T0 + 55, step=60)   # 11:00:55, 11:01:55
    out = _read(monkeypatch, {f"{_ASSET}_{_T1}": vx, f"{_ASSET}_{_T2}": vy},
                (_ASSET, _T1), (_ASSET, _T2))
    (chart,) = _charts(_parts(out)[1])
    assert chart["labels"] == ["2026-01-05T11:00:05Z", "2026-01-05T11:00:55Z",
                               "2026-01-05T11:01:05Z", "2026-01-05T11:01:55Z"]
    assert chart["datasets"][0]["data"] == [1.0, None, 1.1, None]
    assert chart["datasets"][1]["data"] == [None, 2.0, None, 2.1]


def test_labels_are_full_instants_then_go_lossless_when_shorter_labels_collide(monkeypatch):
    new_year = int(datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc).timestamp())
    series = _points([1.0, 2.0, 3.0], start=new_year, step=3600)
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": series}, (_ASSET, _VX))
    (chart,) = _charts(_parts(out)[1])
    assert chart["labels"] == ["2026-12-31T23:00Z", "2027-01-01T00:00Z", "2027-01-01T01:00Z"]

    vx = [{"value": v, "timestamp": _T0 + s, "time": "", "quality": "GOOD"}
          for v, s in ((1.0, 5.25), (1.1, 65.25))]
    vy = [{"value": v, "timestamp": _T0 + s, "time": "", "quality": "GOOD"}
          for v, s in ((2.0, 5.75), (2.1, 65.75))]
    out = _read(monkeypatch, {f"{_ASSET}_{_T1}": vx, f"{_ASSET}_{_T2}": vy},
                (_ASSET, _T1), (_ASSET, _T2))
    (chart,) = _charts(_parts(out)[1])
    assert chart["labels"] == [
        "2026-01-05T11:00:05.250000Z", "2026-01-05T11:00:05.750000Z",
        "2026-01-05T11:01:05.250000Z", "2026-01-05T11:01:05.750000Z"]
    assert chart["datasets"][0]["data"] == [1.0, None, 1.1, None]


def test_readings_in_the_same_minute_are_labelled_to_the_second(monkeypatch):
    series = _series(6, 1.0, step=20)
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": series}, (_ASSET, _VX))
    (chart,) = _charts(_parts(out)[1])
    assert chart["labels"] == [
        datetime.fromtimestamp(p["timestamp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for p in series]


def test_a_channel_with_two_readings_in_one_second_gets_its_own_chart(monkeypatch):
    """A shared axis cannot hold two readings of one channel under one label,
    so that group splits into one chart per channel; each still carries every
    point."""
    vx = _points([1.0, 1.1, 1.2], step=0)                # three readings, one second
    vy = _series(3, 2.0)
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": vx, f"{_ASSET}_{_VY}": vy},
                (_ASSET, _VX), (_ASSET, _VY))
    charts = _charts(_parts(out)[1])
    assert [[d["data"] for d in c["datasets"]] for c in charts] == [
        [[1.0, 1.1, 1.2]], [[p["value"] for p in vy]]]


def test_a_window_across_dates_labels_carry_the_date(monkeypatch):
    series = _series(4, 1.0, start=_T0 + 12 * 3600, step=3 * 3600)   # 23:00 -> 08:00
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": series}, (_ASSET, _VX))
    (chart,) = _charts(_parts(out)[1])
    assert chart["labels"] == ["2026-01-05T23:00Z", "2026-01-06T02:00Z",
                               "2026-01-06T05:00Z", "2026-01-06T08:00Z"]
    assert "2026-01-05 23:00 UTC to 2026-01-06 08:00 UTC" in chart["title"]


def test_each_asset_gets_its_own_charts(monkeypatch):
    payload = {f"{_ASSET}_{_VX}": _series(5), f"{_ASSET_2}_{_VX}": _series(5, 3.0)}
    out = _read(monkeypatch, payload, (_ASSET, _VX), (_ASSET_2, _VX))
    titles = [c["title"] for c in _charts(_parts(out)[1])]
    assert [t.split(",")[0] for t in titles] == ["Unit A: vib_x (mm/s)", "Unit B: vib_x (mm/s)"]


# --------------------------------------------------------------------------- #
# 2. Nothing to chart: the result stays the unchanged JSON string
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("outcome", [
    [],
    {"error": "boom", "error_type": "sitewise_error"},
    {"status": "before_first_value", "points": [], "data_starts_at": "2026-01-06T00:00:00+00:00",
     "note": "window predates the data"},
    {"status": "partial", "truncated": True, "reason": "call_limit", "covered_start": None,
     "covered_end": None, "requested_start": None, "requested_end": None,
     "note": "Partial history (call_limit): no points returned", "points": []},
    _points([1.5]),
    {"status": "something_else", "points": _points([1.0, 2.0])},
], ids=["empty", "error", "before_first_value", "partial_without_points", "one_point",
        "unknown_wrapper"])
def test_no_chart_for_series_with_nothing_to_draw(monkeypatch, outcome):
    """The series with nothing to draw is left off the chart its unit-mate
    gets; read alone, it leaves the result the unchanged JSON string."""
    payload = {f"{_ASSET}_{_VX}": _series(3), f"{_ASSET}_{_VY}": outcome}
    out = _read(monkeypatch, payload, (_ASSET, _VX), (_ASSET, _VY))
    (chart,) = _charts(_parts(out)[1])
    assert [d["label"] for d in chart["datasets"]] == ["vib_x"]

    alone = {f"{_ASSET}_{_VY}": outcome}
    assert _read(monkeypatch, alone, (_ASSET, _VY)) == _chart_free(alone)


def test_a_reader_error_envelope_gets_no_chart(monkeypatch):
    charted = _read(monkeypatch, {f"{_ASSET}_{_VX}": _series(3)}, (_ASSET, _VX))
    assert _charts(_parts(charted)[1])
    payload = {"error": "entries must be a list", "error_type": "invalid_request"}
    _install(monkeypatch, payload)
    out = _tool()._tool_func(entries_json="{}")
    assert isinstance(out, str) and "<chart-block" not in out


def test_partial_read_with_points_is_charted_and_says_partial(monkeypatch):
    points = _series(8)
    partial = {"status": "partial", "truncated": True, "reason": "page_limit",
               "covered_start": points[0]["time"], "covered_end": points[-1]["time"],
               "requested_start": "2026-01-05T08:00:00+00:00",
               "requested_end": "2026-01-05T12:00:00+00:00",
               "note": "Partial history (page_limit): points cover the head", "points": points}
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": partial}, (_ASSET, _VX))
    (chart,) = _charts(_parts(out)[1])
    assert "partial read" in chart["title"]
    assert chart["datasets"][0]["data"] == [p["value"] for p in points]


def test_readings_that_are_not_good_or_not_numbers_are_left_out_and_counted(monkeypatch):
    points = _series(6)
    points[1]["quality"] = "BAD"
    points[2]["quality"] = "UNCERTAIN"
    points[3]["value"] = "offline"
    points[4]["value"] = float("nan")
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": points}, (_ASSET, _VX))
    (chart,) = _charts(_parts(out)[1])
    assert chart["datasets"][0]["data"] == [points[0]["value"], points[5]["value"]]
    assert "4 readings not plotted" in chart["title"]


# --------------------------------------------------------------------------- #
# 3. Every JSON reader of the result reads what it read before
# --------------------------------------------------------------------------- #
def _tool_result(out):
    if isinstance(out, str):
        return {"toolUseId": "t1", "status": "success", "content": [{"text": out}]}
    return {"toolUseId": "t1", **out}


def _integrity_state(result):
    state: dict = {}
    initialize_integrity_state(state, "is it trending up")
    capture_trusted_tool_result(result, state, system_id="sys-x", boundary="sitewise",
                                tool_name="sitewise_get_asset_property_history")
    capture = state[PRIVATE_SOURCE_STATE_KEY]
    return (len(capture.observations), list(capture.reason_codes), list(capture.completeness))


@pytest.mark.parametrize("points", [3, 24], ids=["small", "module_6_size"])
def test_coverage_capture_and_integrity_read_the_json_part_unchanged(monkeypatch, points):
    head = _series(points)
    partial = {"status": "partial", "truncated": True, "reason": "page_limit",
               "covered_start": head[0]["time"], "covered_end": head[-1]["time"],
               "requested_start": "2026-01-05T08:00:00+00:00",
               "requested_end": "2026-01-05T12:00:00+00:00",
               "note": "Partial history (page_limit): points cover the head", "points": head}
    payload = {f"{_ASSET}_{_VX}": _series(points), f"{_ASSET}_{_VY}": partial,
               f"{_ASSET}_{_TEMP}": _series(points, 21.0)}
    pairs = ((_ASSET, _VX), (_ASSET, _VY), (_ASSET, _TEMP))
    tool_input = {"entries_json": _entries(*pairs), "hours_back": 1}
    charted = _tool_result(_read(monkeypatch, payload, *pairs))
    assert len(charted["content"]) == 2
    bare = _tool_result(_chart_free(payload))
    name = "sitewise_get_asset_property_history"
    assert (telemetry_coverage.coverage_lines(name, tool_input, charted)
            == telemetry_coverage.coverage_lines(name, tool_input, bare))
    assert len(telemetry_coverage.coverage_lines(name, tool_input, charted)) == 3
    assert (tool_result_capture.safe_tool_result_metadata(charted, tool_name=name, max_chars=1024)
            == tool_result_capture.safe_tool_result_metadata(bare, tool_name=name, max_chars=1024))
    assert tool_result_capture.result_row_cut(charted) == tool_result_capture.result_row_cut(bare)
    assert tool_result_capture.result_row_cut(charted) == (True, None)
    assert _integrity_state(charted) == _integrity_state(bare)
    assert _integrity_state(charted)[0] > 0


# --------------------------------------------------------------------------- #
# 4. Delivery: hook -> relay -> orchestrator hook -> drain, exactly once
# --------------------------------------------------------------------------- #
def test_the_agent_framework_passes_the_two_part_result_through(monkeypatch):
    """Through the real Strands tool call path (not ``_tool_func``): the
    returned dict becomes the ToolResult as-is, with the call's toolUseId."""
    import asyncio
    payload = {f"{_ASSET}_{_VX}": _series(4)}
    _install(monkeypatch, payload)
    tool_use = {"toolUseId": "tu-1", "name": "sitewise_get_asset_property_history",
                "input": {"entries_json": _entries((_ASSET, _VX)), "hours_back": 1}}

    async def run():
        return [event async for event in _tool().stream(tool_use, {})]

    result = asyncio.run(run())[-1].tool_result
    assert result["toolUseId"] == "tu-1" and result["status"] == "success"
    assert result["content"][0]["text"] == _chart_free(payload)
    assert len(_TAG.findall(result["content"][1]["text"])) == 1


def _fire_hook(result, agent):
    message = {"role": "user", "content": [{"toolResult": result}]}
    ChartRedactionHook()._on_message_added(SimpleNamespace(message=message, agent=agent))
    return message["content"][0]["toolResult"]


def test_charts_reach_the_answer_stream_exactly_once(monkeypatch):
    payload = {f"{_ASSET}_{_VX}": _series(24, 1.9), f"{_ASSET}_{_TEMP}": _series(24, 21.0)}
    out = _read(monkeypatch, payload, (_ASSET, _VX), (_ASSET, _TEMP))
    rendered = _TAG.findall(_parts(out)[1])
    assert len(rendered) == 2

    # Inner system agent: the model sees the readings and a stub, never a tag.
    inner = SimpleNamespace()
    seen = _fire_hook(_tool_result(out), inner)
    assert seen["content"][0]["text"] == _chart_free(payload)
    assert "<chart-block" not in seen["content"][1]["text"]
    assert "2 chart(s) ALREADY RENDERED to the user. " in seen["content"][1]["text"]
    assert "Unit A" not in seen["content"][1]["text"]
    assert inner._pending_chart_blocks == rendered

    # Mode A relay carries the tags across the query boundary.
    answer = sa._relay_inner_chart_tags(inner, "Vibration rose over the hour.")
    assert answer.startswith("Vibration rose over the hour.")
    assert _TAG.findall(answer) == rendered

    # Orchestrator: hook queues them, the runtime drain injects each once.
    orchestrator = SimpleNamespace()
    query_result = {"toolUseId": "q1", "status": "success", "content": [{"text": answer}]}
    visible = _fire_hook(query_result, orchestrator)["content"][0]["text"]
    assert "<chart-block" not in visible
    second_call = {"toolUseId": "q2", "status": "success", "content": [{"text": answer}]}
    _fire_hook(second_call, orchestrator)               # same charts re-queued
    assert drain_pending_charts(orchestrator) == rendered

    # The query call's trace row (summarized after redaction) still reads the
    # answer, not a bare "Rendered N chart(s)." line.
    summary = summarize_tool_result("", {"content": [{"text": visible}]})
    assert summary.startswith("Vibration rose over the hour.")
    assert "<chart-block" not in summary


def _injected_text(out, answer):
    """Run one charted tool result through the whole delivery path (inner
    hook, Mode A relay, orchestrator hook, runtime drain) and return the text
    the runtime injects into the answer stream."""
    import agents.runtime_app as runtime_app
    inner = SimpleNamespace()
    _fire_hook(_tool_result(out), inner)
    relayed = sa._relay_inner_chart_tags(inner, answer)
    orchestrator = SimpleNamespace()
    _fire_hook({"toolUseId": "q1", "status": "success", "content": [{"text": relayed}]},
               orchestrator)
    return runtime_app._drain_pending_chart_text(orchestrator)


def test_the_chart_cap_disclosure_reaches_the_injected_answer(monkeypatch):
    """Past the cap, the first delivered chart's title says how many were
    drawn, the drop policy, and which charts were not drawn (bounded); that
    title is part of the text the runtime injects, not only the inner model's
    view."""
    cap = getattr(sa, "_SITEWISE_CHART_MAX_CHARTS", 8)
    assets = [f"asset-{i:02d}" for i in range(cap + 6)]
    _graph(monkeypatch, {**{a: (f"Unit {a[-2:]}", "") for a in assets}, _VX: ("vib_x", "mm/s")})
    payload = {f"{a}_{_VX}": _series(3) for a in assets}
    out = _read(monkeypatch, payload, *[(a, _VX) for a in assets])
    injected = _injected_text(out, "Vibration is flat on every unit.")
    charts = _charts(injected)
    assert len(charts) == cap
    first = charts[0]["title"]
    assert f"Showing the first {cap} of {cap + 6} charts, in request order." in first
    assert first.startswith("Unit 00: vib_x (mm/s), 2026-01-05 11:00 UTC to 2026-01-05 11:05 UTC. ")
    assert (f"Showing the first {cap} of {cap + 6} charts, in request order. "
            "Not drawn: Unit 08: vib_x (mm/s); Unit 09: vib_x (mm/s); "
            "Unit 10: vib_x (mm/s); and 3 more.") in first
    assert len(first) <= sa._SITEWISE_CHART_TITLE_CHARS + sa._SITEWISE_CHART_NOTICE_CHARS + 2
    assert all("Showing the first" not in c["title"] for c in charts[1:])
    assert [c["title"].split(",")[0] for c in charts] == [
        f"Unit {i:02d}: vib_x (mm/s)" for i in range(cap)]

    few = {f"{a}_{_VX}": _series(3) for a in assets[:cap]}
    out = _read(monkeypatch, few, *[(a, _VX) for a in assets[:cap]])
    assert "Showing the first" not in _injected_text(out, "Flat.")   # no cap, no notice


def test_enforce_mode_delivers_no_chart_and_tells_the_inner_model(monkeypatch):
    payload = {f"{_ASSET}_{_VX}": _series(5)}
    out = _read(monkeypatch, payload, (_ASSET, _VX))
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    inner = SimpleNamespace()
    seen = _fire_hook(_tool_result(out), inner)
    assert "withheld and NOT delivered" in seen["content"][1]["text"]
    assert seen["content"][0]["text"] == _chart_free(payload)
    assert sa._relay_inner_chart_tags(inner, "answer") == "answer"


# --------------------------------------------------------------------------- #
# 4b. What was not drawn reaches both models as counts. The page notice rides
#     the first chart's title, which the models never read, so the first tag
#     also carries the two counts as digit-only attributes and the chart note
#     states them in fixed words. No omitted name reaches either model.
# --------------------------------------------------------------------------- #
_UNNAMED_ASSET = "0f6c2a9e-1111-4222-8333-444455556666"
_UNNAMED_PROPS = [f"{i:08x}-1111-4222-8333-444455556666" for i in range(3)]
_FIRST_BASE = "Unit 00: vib_x (mm/s), 2026-01-05 11:00 UTC to 2026-01-05 11:05 UTC"
_CAP_NOTICE = ("Showing the first 8 of 14 charts, in request order. Not drawn: "
               "Unit 08: vib_x (mm/s); Unit 09: vib_x (mm/s); Unit 10: vib_x (mm/s); "
               "and 3 more.")
_UNNAMED_NOTICE = "3 series not charted: no name recorded."
_OMISSIONS = {  # case -> (charts drawn, charts not drawn, series not named, page notice)
    "cap": (8, 6, 0, _CAP_NOTICE),
    "unnamed": (1, 0, 3, _UNNAMED_NOTICE),
    "both": (8, 6, 3, f"{_CAP_NOTICE} {_UNNAMED_NOTICE}"),
}


def _omission_read(monkeypatch, case):
    """One history read that hits the chart cap, leaves series unnamed, or both."""
    assert sa._SITEWISE_CHART_MAX_CHARTS == 8
    named = [f"asset-{i:02d}" for i in range(1 if case == "unnamed" else 14)]
    _graph(monkeypatch, {**{a: (f"Unit {a[-2:]}", "") for a in named}, _VX: ("vib_x", "mm/s")})
    pairs = [(a, _VX) for a in named]
    if case != "cap":
        pairs += [(_UNNAMED_ASSET, p) for p in _UNNAMED_PROPS]
    payload = {f"{a}_{p}": _series(3) for a, p in pairs}
    return _read(monkeypatch, payload, *pairs)


def _assert_page_unchanged(chart_text, case):
    """The page sees the same chart count, titles and notice as before."""
    drawn, _not_drawn, _not_named, notice = _OMISSIONS[case]
    charts = _charts(chart_text)
    assert len(charts) == drawn
    assert [c["title"].split(",")[0] for c in charts] == [
        f"Unit {i:02d}: vib_x (mm/s)" for i in range(drawn)]
    assert charts[0]["title"] == f"{_FIRST_BASE}. {notice}"
    assert all("Showing the first" not in c["title"] and "not charted" not in c["title"]
               for c in charts[1:])


def _assert_counts_ride_the_first_tag(chart_text, case):
    _drawn, not_drawn, not_named, _notice = _OMISSIONS[case]
    tags = _TAG.findall(chart_text)
    assert ('charts_not_drawn="6"' in tags[0]) == bool(not_drawn)
    assert (f'series_not_named="{not_named}"' in tags[0]) == bool(not_named)
    assert all("charts_not_drawn" not in t and "series_not_named" not in t for t in tags[1:])


def _assert_model_reads_omissions(text, mode, case):
    drawn, not_drawn, not_named, _notice = _OMISSIONS[case]
    head = (f"[{drawn} chart(s) withheld and NOT delivered to the user (integrity-enforce mode)"
            if mode == "enforce" else f"[{drawn} chart(s) ALREADY RENDERED to the user")
    assert head in text
    assert (f"; {not_drawn} more chart(s) were not drawn (chart limit)" in text) == bool(not_drawn)
    assert (f"; {not_named} series were not charted (no name recorded)" in text) == bool(not_named)
    for leak in ("Unit", "Showing the first", "Not drawn:", "<chart-block", "series_not_named"):
        assert leak not in text
    stripped, rendered, withheld = strip_chart_notes(text)
    assert (rendered, withheld) == ((0, drawn) if mode == "enforce" else (drawn, 0))
    assert "not drawn" not in stripped and "not charted" not in stripped


@pytest.mark.parametrize("case", ["cap", "unnamed", "both"])
@pytest.mark.parametrize("mode", ["normal", "enforce"])
def test_omissions_reach_both_models_as_counts(monkeypatch, mode, case):
    out = _omission_read(monkeypatch, case)
    chart_text = _parts(out)[1]
    tags = _TAG.findall(chart_text)
    _assert_page_unchanged(chart_text, case)
    if mode == "enforce":
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    # The system agent's model.
    inner = SimpleNamespace()
    seen = _fire_hook(_tool_result(out), inner)
    _assert_model_reads_omissions(seen["content"][1]["text"], mode, case)
    assert inner._pending_chart_blocks == tags
    # The orchestrator's model. Enforce mode relays no tag, so its own hook is
    # fed the same tags directly, as a chart-bearing result on its side.
    answer = "Vibration is flat on every unit."
    relayed = sa._relay_inner_chart_tags(inner, answer)
    if mode == "enforce":
        assert relayed == answer
        relayed = f"{answer}\n\n{chart_text}"
    outer = SimpleNamespace()
    visible = _fire_hook({"toolUseId": "q1", "status": "success",
                          "content": [{"text": relayed}]}, outer)["content"][0]["text"]
    assert visible.startswith(answer)
    _assert_model_reads_omissions(visible, mode, case)
    assert drain_pending_charts(outer) == tags
    assert summarize_tool_result("", {"content": [{"text": visible}]}) == answer
    _assert_counts_ride_the_first_tag(chart_text, case)


def test_no_omission_means_no_counts_on_any_tag_or_note(monkeypatch):
    out = _read(monkeypatch, {f"{_ASSET}_{_VX}": _series(4)}, (_ASSET, _VX))
    chart_text = _parts(out)[1]
    assert "charts_not_drawn" not in chart_text and "series_not_named" not in chart_text
    seen = _fire_hook(_tool_result(out), SimpleNamespace())
    note = seen["content"][1]["text"]
    assert "not drawn" not in note and "not charted" not in note


#: What a note with omission counts tells the model to do with them.
_COUNT_CLAUSE = ("Say in plain words how many were not charted, from these counts, never "
                 "by name, and chart none of them.")


@pytest.mark.parametrize("case", ["cap", "unnamed", "both"])
def test_a_capped_read_tells_both_models_to_count_and_chart_nothing(monkeypatch, case):
    """Past the chart cap, or with unnamed series, both models read the counts
    and are told to say how many were not charted and to chart none of them,
    with no name to give."""
    out = _omission_read(monkeypatch, case)
    inner = SimpleNamespace()
    inner_text = _fire_hook(_tool_result(out), inner)["content"][1]["text"]
    relayed = sa._relay_inner_chart_tags(inner, "Vibration is flat on every unit.")
    outer_text = _fire_hook({"toolUseId": "q1", "status": "success",
                             "content": [{"text": relayed}]},
                            SimpleNamespace())["content"][0]["text"]
    for text in (inner_text, outer_text):
        assert f"). {_COUNT_CLAUSE} Author no chart for their series" in text
        assert "Unit" not in text and "Not drawn:" not in text


# --------------------------------------------------------------------------- #
# 4c. The note counts the charts the page gets. Delivery drops byte-identical
#     repeats (``drain_pending_charts``), so the note does too: the same read
#     relayed twice counts once, and two different reads add up.
# --------------------------------------------------------------------------- #
def _outer_note(monkeypatch, mode, chart_texts):
    """The orchestrator model's text and the delivered tags for one query
    result that relays the chart parts of ``chart_texts``."""
    answer = "Vibration is flat on every unit."
    inner = SimpleNamespace()
    for text in chart_texts:
        _fire_hook({"toolUseId": "t1", "status": "success",
                    "content": [{"text": "{}"}, {"text": text}]}, inner)
    if mode == "enforce":
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
        assert sa._relay_inner_chart_tags(inner, answer) == answer
        relayed = answer + "\n\n" + "\n\n".join(chart_texts)
    else:
        relayed = sa._relay_inner_chart_tags(inner, answer)
    outer = SimpleNamespace()
    visible = _fire_hook({"toolUseId": "q1", "status": "success",
                          "content": [{"text": relayed}]}, outer)["content"][0]["text"]
    assert visible.startswith(answer)
    return visible, drain_pending_charts(outer)


def _head(mode, count):
    return (f"[{count} chart(s) withheld and NOT delivered to the user (integrity-enforce mode)"
            if mode == "enforce" else f"[{count} chart(s) ALREADY RENDERED to the user")


@pytest.mark.parametrize("mode", ["normal", "enforce"])
def test_a_read_relayed_twice_counts_once(monkeypatch, mode):
    chart_text = _parts(_omission_read(monkeypatch, "both"))[1]
    visible, delivered = _outer_note(monkeypatch, mode, [chart_text, chart_text])
    assert delivered == _TAG.findall(chart_text)
    assert (f"{_head(mode, 8)}; 6 more chart(s) were not drawn (chart limit); "
            "3 series were not charted (no name recorded).") in visible
    assert "16 chart(s)" not in visible and "12 more" not in visible


@pytest.mark.parametrize("mode", ["normal", "enforce"])
def test_two_different_reads_add_up(monkeypatch, mode):
    capped = _parts(_omission_read(monkeypatch, "cap"))[1]
    unnamed = _parts(_omission_read(monkeypatch, "unnamed"))[1]
    visible, delivered = _outer_note(monkeypatch, mode, [capped, unnamed])
    assert delivered == _TAG.findall(capped) + _TAG.findall(unnamed)
    assert len(delivered) == 9
    assert (f"{_head(mode, 9)}; 6 more chart(s) were not drawn (chart limit); "
            "3 series were not charted (no name recorded).") in visible

# --------------------------------------------------------------------------- #
# 5. Best effort and bounds
# --------------------------------------------------------------------------- #
def test_without_graph_names_series_are_not_drawn_and_counted_not_titled_by_id(monkeypatch):
    """No resolved name -> not drawn (a raw id never titles a chart). A named
    channel beside unnamed ones still draws; the unnamed ones are reported by
    count in the notice, with no id substring anywhere in the chart text."""
    _graph(monkeypatch, {})
    asset = "0f6c2a9e-1111-4222-8333-444455556666"
    first, second = "9a8b7c6d-1111-4222-8333-444455556666", "5e4d3c2b-1111-4222-8333-444455556666"
    payload = {f"{asset}_{first}": _series(4), f"{asset}_{second}": _series(4, 2.0)}
    # Both channels unnamed -> nothing drawable, but a chart WAS asked for, so
    # the count rides a notice tag ; never silence, never an id.
    out = _read(monkeypatch, payload, (asset, first), (asset, second))
    chart_text = _parts(out)[1]
    assert "2 series not charted: no name recorded" in chart_text
    assert asset[:8] not in chart_text and first[:8] not in chart_text and "…" not in chart_text

    # One named channel beside two unnamed ones: only the named one draws, and
    # the two unnamed are counted in the notice by number, never by id.
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("vib_x", "mm/s")})
    mixed = {f"{_ASSET}_{_VX}": _series(4, 1.9),
             f"{asset}_{first}": _series(4), f"{asset}_{second}": _series(4, 2.0)}
    out = _read(monkeypatch, mixed, (_ASSET, _VX), (asset, first), (asset, second))
    chart_text = _parts(out)[1]
    charts = _charts(chart_text)
    assert [c["title"].split(",")[0] for c in charts] == ["Unit A: vib_x (mm/s)"]
    assert charts[0]["title"].startswith("Unit A: vib_x (mm/s), 2026-01-05 11:00 UTC to")
    assert "2 series not charted: no name recorded" in chart_text
    assert asset[:8] not in chart_text and first[:8] not in chart_text and "…" not in chart_text

    payload = {f"{_ASSET}_{p}": _series(4) for p in ("p-1", "p-2")}
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), "p-1": ("vibration_x", ""),
                         "p-2": ("vibration_y", "")})
    out = _read(monkeypatch, payload, (_ASSET, "p-1"), (_ASSET, "p-2"))
    (chart,) = _charts(_parts(out)[1])                   # named, no unit: kind groups them
    assert [d["label"] for d in chart["datasets"]] == ["vibration_x", "vibration_y"]


def test_names_and_units_come_from_one_bounded_graph_read_scoped_to_the_system(monkeypatch):
    reads = _graph(monkeypatch, {"p-1": ("vib_x", "mm/s")})
    ids = ["p-1"] + [f"id-{i}" for i in range(150)] + ["id-0", "p-1"]
    assert sa._sitewise_chart_names(ids, "sys-x") == {"p-1": ("vib_x", "mm/s")}
    (cypher, parameters), = reads
    assert "(n:Asset OR n:Property)" in cypher
    assert "n.system = $sys" in cypher and "n.source_id IN $ids" in cypher
    assert parameters["sys"] == "sys-x"
    assert parameters["ids"][0] == "p-1"
    assert len(parameters["ids"]) == sa._SITEWISE_CHART_MAX_GRAPH_IDS
    assert len(set(parameters["ids"])) == len(parameters["ids"])

    _clear_chart_cache()
    assert sa._sitewise_chart_names(["p-1"], "") == {}          # no system: no read
    monkeypatch.delenv("GRAPH_IDENTIFIER")
    assert sa._sitewise_chart_names(["p-1"], "sys-x") == {}     # no graph: no read
    assert len(reads) == 1


def test_a_repeated_read_names_charts_from_the_cache_without_a_second_graph_read(monkeypatch):
    reads = _graph(monkeypatch, _GRAPH)
    payload = {f"{_ASSET}_{_VX}": _series(5), f"{_ASSET}_{_TEMP}": _series(5, 21.0)}
    pairs = ((_ASSET, _VX), (_ASSET, _TEMP))
    first = _charts(_parts(_read(monkeypatch, payload, *pairs))[1])
    second = _charts(_parts(_read(monkeypatch, payload, *pairs))[1])
    assert len(reads) == 1
    assert first == second
    assert first[0]["title"].startswith("Unit A: vib_x (mm/s), ")

    # A later read with one new id asks the graph for that id only.
    more = {**payload, f"{_ASSET_2}_{_VX}": _series(5)}
    _read(monkeypatch, more, *pairs, (_ASSET_2, _VX))
    assert len(reads) == 2 and reads[1][1]["ids"] == [_ASSET_2]

    # The cache is per graph: another graph id reads again.
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-other")
    _read(monkeypatch, payload, *pairs)
    assert len(reads) == 3


def test_a_graph_timeout_returns_the_readings_with_only_a_no_name_notice(monkeypatch):
    """The real seam: its own client with short connect/read timeouts and one
    attempt. A timeout leaves the readings untouched and resolves NO name, so
    no chart is drawn and no id titles anything; a chart was asked for, so the
    count rides a notice tag . A read soon after skips the graph
    instead of waiting again."""
    from botocore.exceptions import ReadTimeoutError
    monkeypatch.setattr(sa, "_sitewise_chart_graph_read", _REAL_GRAPH_READ, raising=False)
    _clear_chart_cache()

    class SlowGraph:
        calls = 0

        def execute_query(self, **kwargs):
            SlowGraph.calls += 1
            raise ReadTimeoutError(endpoint_url="https://graph.example")

    created = []

    def fake_client(service, **kwargs):
        created.append((service, kwargs))
        return SlowGraph()

    monkeypatch.setattr(sa.boto3, "client", fake_client)
    payload = {f"{_ASSET}_{_VX}": _series(5)}
    out = _read(monkeypatch, payload, (_ASSET, _VX))
    readings, chart_text = _parts(out)
    assert readings == _chart_free(payload)             # readings intact
    assert "1 series not charted: no name recorded" in chart_text
    assert _ASSET not in chart_text and _VX not in chart_text and "…" not in chart_text
    assert SlowGraph.calls == 1
    (service, kwargs), = created
    config = kwargs["config"]
    assert service == "neptune-graph"
    assert config.connect_timeout == sa._SITEWISE_CHART_GRAPH_CONNECT_TIMEOUT_S
    assert config.read_timeout == sa._SITEWISE_CHART_GRAPH_READ_TIMEOUT_S
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert sa._SITEWISE_CHART_GRAPH_CONNECT_TIMEOUT_S + sa._SITEWISE_CHART_GRAPH_READ_TIMEOUT_S <= 3

    again = _read(monkeypatch, payload, (_ASSET, _VX))
    assert "1 series not charted: no name recorded" in _parts(again)[1]  # still no chart
    assert SlowGraph.calls == 1                          # backoff: no second wait


def test_a_chart_failure_never_breaks_the_read(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("renderer exploded")

    payload = {f"{_ASSET}_{_VX}": _series(5)}
    # Names resolve, but grouping blows up: the read still returns, chart-free.
    monkeypatch.setattr(sa, "_sitewise_chart_groups", explode, raising=False)
    assert _read(monkeypatch, payload, (_ASSET, _VX)) == _chart_free(payload)

    # A failure earlier, in channel extraction, is equally tolerated.
    monkeypatch.setattr(sa, "_sitewise_chart_channels", explode, raising=False)
    assert _read(monkeypatch, payload, (_ASSET, _VX)) == _chart_free(payload)
