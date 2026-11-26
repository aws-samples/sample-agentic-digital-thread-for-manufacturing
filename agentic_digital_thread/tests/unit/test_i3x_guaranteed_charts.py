# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guaranteed charts on the raw history tools + the composition pin.

The batch and composite history tools append rendered <chart-block/> tags as
a plain-text suffix AFTER the claim-checked JSON payload (charts are a
product requirement, not a model choice). Composition pin (the plan's
explicit requirement): (1) _cap_history_payload has ALREADY claim-checked
the JSON — ``dataset_ref`` present, no raw >8-point VQT array anywhere;
(2) the suffix rides OUTSIDE the JSON; (3) the ChartRedactionHook strips the
tags from the model-visible tool result (queuing them for runtime injection)
while the claim-checked JSON survives redaction intact. All three compose.

Charts are best-effort: a chart-path failure never breaks the raw result.

Fully synthetic — fake _I3xClient + a plain-namespace agent; no AWS,
no network.
"""
import json
import re
from types import SimpleNamespace

import pytest

import agents.system_agent as sa
from agents.orchestrator import ChartRedactionHook

from .test_cap_history_payload import _series, assert_no_raw_series


@pytest.fixture(autouse=True)
def _no_graph_env(monkeypatch):
    """Chart-title graph lookups are best-effort — keep them OFF by default
    so a leaked GRAPH_IDENTIFIER can't route to a real boto3 client."""
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)


class ChartServingClient:
    """_I3xClient double answering history + /objects/list for chart renders."""

    def __init__(self, history, fail_after_first_history=False):
        self.calls = []
        self._history = history
        self._fail_late = fail_after_first_history
        self._history_calls = 0

    def request(self, method, path, payload, body_cap=0):
        self.calls.append({"method": method, "path": path,
                           "payload": payload, "body_cap": body_cap})
        if path == "/objects/history":
            self._history_calls += 1
            if self._fail_late and self._history_calls > 1:
                raise RuntimeError("chart-window fetch exploded")
            return self._history
        if path == "/objects/list":
            return [{"result": {"elementId": "asset-1",
                                "displayName": "AssetOne"}}]
        raise AssertionError(f"unexpected path {path}")


def _history():
    return [{
        "elementId": "asset-1",
        "result": {"components": {
            "asset-1:prop-1": {"values": _series(40)},
        }},
    }]


def _batch_out(client, ids=("asset-1:prop-1",)):
    batch = sa._make_i3x_batch_history_tool(client)
    return batch._tool_func(element_ids=list(ids),
                            start_time="2026-04-30T00:00:00Z",
                            end_time="2026-04-30T01:00:00Z")


def test_batch_payload_carries_claim_check_and_chart_suffix():
    """THE COMPOSITION PIN, part 1: one payload holds BOTH the claim-check
    evidence (dataset_ref) and the chart-tag suffix, and no raw >8-point VQT
    array exists anywhere in it."""
    raw = _batch_out(ChartServingClient(_history()))
    parsed, end = json.JSONDecoder().raw_decode(raw)
    assert "dataset_ref" in json.dumps(parsed)          # claim-check ran
    assert_no_raw_series(parsed)                        # no raw series inside
    suffix = raw[end:]
    assert "<chart-block" in suffix                     # charts attached
    assert suffix.startswith("\n\n")                    # plain-text, after JSON
    assert '"timestamp"' not in suffix                  # no VQT dicts in suffix


def test_chart_window_matches_the_raw_request_window():
    client = ChartServingClient(_history())
    _batch_out(client)
    chart_fetches = [c for c in client.calls if c["path"] == "/objects/history"
                     and c["payload"]["elementIds"] == ["asset-1"]]
    assert len(chart_fetches) == 1
    assert chart_fetches[0]["payload"]["startTime"] == "2026-04-30T00:00:00Z"
    assert chart_fetches[0]["payload"]["endTime"] == "2026-04-30T01:00:00Z"


def test_batch_chart_fetches_capped_at_two_distinct_assets():
    client = ChartServingClient(_history())
    _batch_out(client, ids=["a1:p1", "a2:p1", "a3:p1", "a1:p2"])
    singles = [c["payload"]["elementIds"][0] for c in client.calls
               if c["path"] == "/objects/history"
               and len(c["payload"]["elementIds"]) == 1]
    assert singles == ["a1", "a2"]  # sorted, deduped, capped at 2


def test_chart_failure_leaves_raw_payload_intact():
    raw = _batch_out(ChartServingClient(_history(), fail_after_first_history=True))
    parsed = json.loads(raw)  # NO suffix — the whole output is the JSON
    assert "dataset_ref" in json.dumps(parsed)
    assert "<chart-block" not in raw


def test_composite_payload_carries_chart_suffix(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    from .test_i3x_composite_telemetry import FakeNeptune, _ROWS
    monkeypatch.setattr(sa.boto3, "client", lambda *a, **k: FakeNeptune(_ROWS))
    client = ChartServingClient([
        {"elementId": "a1:p1", "result": {"values": _series(60)}},
    ])
    raw = sa._make_i3x_composite_telemetry_tool(client)._tool_func(
        asset_name="<asset-name>", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z")
    parsed, end = json.JSONDecoder().raw_decode(raw)
    assert "dataset_ref" in json.dumps(parsed)
    assert_no_raw_series(parsed)
    assert "<chart-block" in raw[end:]
    # Composite charts only the FIRST id's asset — one single-id fetch.
    singles = [c for c in client.calls if c["path"] == "/objects/history"
               and c["payload"]["elementIds"] == ["a1"]]
    assert len(singles) == 1


def test_redaction_hook_strips_suffix_tags_but_keeps_claim_checked_json():
    """THE COMPOSITION PIN, part 2: feeding the guaranteed-charts tool output
    through the ChartRedactionHook redacts every tag from the model-visible
    result (queued for runtime injection) while the claim-checked JSON —
    stats, dataset_ref, landmarks — survives verbatim."""
    raw = _batch_out(ChartServingClient(_history()))
    n_tags = len(re.findall(r"<chart-block\b[^>]*/>", raw))
    assert n_tags >= 1
    msg = {"role": "user", "content": [
        {"toolResult": {"toolUseId": "t1", "status": "success",
                        "content": [{"text": raw}]}},
    ]}
    agent = SimpleNamespace()
    ChartRedactionHook()._on_message_added(SimpleNamespace(message=msg, agent=agent))
    visible = msg["content"][0]["toolResult"]["content"][0]["text"]
    assert "<chart-block" not in visible                 # model never sees tags
    assert "ALREADY RENDERED" in visible                 # instructive stub
    assert "dataset_ref" in visible                      # claim-check untouched
    parsed, _ = json.JSONDecoder().raw_decode(visible)   # JSON prefix intact
    assert_no_raw_series(parsed)
    queued = agent._pending_chart_blocks
    assert len(queued) == n_tags
    assert all(t.startswith("<chart-block") and t.endswith("/>") for t in queued)


# --------------------------------------------------------------------------- #
# Past two assets the batch read charts no more, and both models are told how
# many assets were left out, as a digit-only attribute on the first chart tag
# and a fixed sentence in the chart note. No left-out asset's name is told.
# --------------------------------------------------------------------------- #
class PerAssetClient:
    """_I3xClient double: each asset has its own series and display name."""

    def __init__(self):
        self.calls = []

    def request(self, method, path, payload, body_cap=0):
        self.calls.append({"path": path, "payload": payload})
        ids = payload.get("elementIds") or []
        if path == "/objects/history":
            return [{"elementId": e.split(":")[0], "result": {"components": {
                f"{e.split(':')[0]}:p1": {"values": _series(40, base=10.0 + n)}}}}
                for n, e in enumerate(ids)]
        if path == "/objects/list":
            return [{"result": {"elementId": e, "displayName": f"Cell {e}"}} for e in ids]
        return []


_ASSETS = ["a1", "a2", "a3", "a4"]


def _batch_through_both_models(monkeypatch, mode, assets):
    """Run the real batch tool, the inner hook, the relay and the outer hook:
    ``(raw tool text, inner model text, outer model text, delivered tags)``."""
    from agents.orchestrator import drain_pending_charts
    raw = _batch_out(PerAssetClient(), ids=[f"{a}:p1" for a in assets])
    if mode == "enforce":
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    inner = SimpleNamespace()
    msg = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "t1", "status": "success", "content": [{"text": raw}]}}]}
    ChartRedactionHook()._on_message_added(SimpleNamespace(message=msg, agent=inner))
    inner_text = msg["content"][0]["toolResult"]["content"][0]["text"]
    answer = "Readings are steady."
    relayed = sa._relay_inner_chart_tags(inner, answer)
    if mode == "enforce":
        assert relayed == answer
        relayed = answer + "\n\n" + "\n\n".join(re.findall(r"<chart-block\b[^>]*/>", raw))
    outer = SimpleNamespace()
    out_msg = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "q1", "status": "success", "content": [{"text": relayed}]}}]}
    ChartRedactionHook()._on_message_added(SimpleNamespace(message=out_msg, agent=outer))
    outer_text = out_msg["content"][0]["toolResult"]["content"][0]["text"]
    return raw, inner_text, outer_text, drain_pending_charts(outer)


@pytest.mark.parametrize("mode", ["normal", "enforce"])
def test_assets_past_the_chart_limit_reach_both_models_as_a_count(monkeypatch, mode):
    from agents.chart_note import strip_chart_notes
    raw, inner_text, outer_text, delivered = _batch_through_both_models(
        monkeypatch, mode, _ASSETS)
    tags = re.findall(r"<chart-block\b[^>]*/>", raw)
    assert len(tags) == 2                                   # two assets charted
    assert "Cell a1" in tags[0] and "Cell a2" in tags[1]    # page titles unchanged
    assert 'assets_not_charted="2"' in tags[0]
    assert "assets_not_charted" not in tags[1]
    assert delivered == tags
    head = ("[2 chart(s) withheld and NOT delivered to the user (integrity-enforce mode)"
            if mode == "enforce" else "[2 chart(s) ALREADY RENDERED to the user")
    for text in (inner_text, outer_text):
        assert f"{head}; 2 more asset(s) were not charted (asset limit)." in text
        assert "Cell a3" not in text and "Cell a4" not in text
        assert "<chart-block" not in text and "assets_not_charted" not in text
        stripped, _rendered, _withheld = strip_chart_notes(text)
        assert "not charted" not in stripped


def test_assets_past_the_chart_limit_are_counted_and_never_charted_by_a_model(monkeypatch):
    """Past the asset limit both models are told to say how many were not
    charted, from the counts, and to chart none of them; no left-out asset's
    name reaches either."""
    _raw, inner_text, outer_text, _delivered = _batch_through_both_models(
        monkeypatch, "normal", _ASSETS)
    for text in (inner_text, outer_text):
        assert ("; 2 more asset(s) were not charted (asset limit). Say in plain words how "
                "many were not charted, from these counts, never by name, and chart none "
                "of them. Author no chart for their series") in text
        assert "Cell a3" not in text and "Cell a4" not in text


def test_two_assets_or_fewer_carry_no_asset_count(monkeypatch):
    raw, inner_text, outer_text, delivered = _batch_through_both_models(
        monkeypatch, "normal", ["a1", "a2", "a1"])
    assert "assets_not_charted" not in raw
    assert len(delivered) == 2
    for text in (inner_text, outer_text):
        assert "[2 chart(s) ALREADY RENDERED to the user. " in text
        assert "not charted" not in text
