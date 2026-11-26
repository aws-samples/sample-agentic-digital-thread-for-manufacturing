# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Compact asset x property form for the SiteWise multi-entry read tools.

The observed failure: a SiteWise system agent asked for every asset of one
model x ~7 properties in ONE sitewise_batch_get_property_values call. Typing
an {assetId, propertyId} UUID pair per entry ran past the agent's own
8192-token output cap mid tool call, so the request never reached SiteWise.
The server already pages the batch (16 entries per API call); the only limit
hit was the model having to TYPE N x M pairs.

The fix keeps ``entries_json`` byte-for-byte and adds ``asset_ids_json`` +
``property_ids_json``: the tool expands the cross product server-side, so the
model types N + M ids instead of N x M pairs.

Fully offline: the reader seam is monkeypatched; no AWS client, no model.
"""
import json
from datetime import datetime, timezone

import pytest

import agents.sitewise_read as sitewise_read
import agents.system_agent as sa
from agents import connection_types as ct
from agents.telemetry_coverage import coverage_lines

_FIXED_NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)
_ASSETS = '["asset-1", "asset-2"]'
_PROPS = '["prop-a", "prop-b"]'
_CROSS = [
    {"assetId": "asset-1", "propertyId": "prop-a"},
    {"assetId": "asset-1", "propertyId": "prop-b"},
    {"assetId": "asset-2", "propertyId": "prop-a"},
    {"assetId": "asset-2", "propertyId": "prop-b"},
]


def _tool(name, config=None):
    tools = sa._make_sitewise_direct_tool(
        region="us-east-1", connection_config=config or {}, system_id="sys-x")
    return next(t for t in tools if t.tool_name == name)


def _batch():
    return _tool("sitewise_batch_get_property_values")


def _history():
    return _tool("sitewise_get_asset_property_history")


def _pin_metadata(monkeypatch):
    """Deterministic clock + source-metadata stamp (the real stamp carries a
    random id), so a whole tool result can be compared byte for byte."""
    monkeypatch.setattr(sa, "utc_now", lambda: _FIXED_NOW)
    monkeypatch.setattr(sa, "attach_source_metadata",
                        lambda payload, **meta: {**payload, "_meta": meta})


@pytest.fixture
def batch_spy(monkeypatch):
    calls = []

    def spy(cfg, region, entries):
        calls.append(entries)
        return {f"{e['assetId']}_{e['propertyId']}": {"value": 1.0, "timestamp": 10,
                                                      "quality": "GOOD"} for e in entries}

    monkeypatch.setattr(sitewise_read, "batch_get_property_values", spy)
    _pin_metadata(monkeypatch)
    return calls


@pytest.fixture
def history_spy(monkeypatch):
    calls = []

    def spy(cfg, region, entries, hours_back=8, max_results=100,
            start_iso=None, end_iso=None):
        calls.append({"entries": entries, "hours_back": hours_back,
                      "max_results": max_results, "start_iso": start_iso,
                      "end_iso": end_iso})
        return {f"{e['assetId']}_{e['propertyId']}": [] for e in entries}

    monkeypatch.setattr(sitewise_read, "get_property_history", spy)
    _pin_metadata(monkeypatch)
    return calls


# --------------------------------------------------------------------------- #
# 1. Current-value batch: compact form expands server-side
# --------------------------------------------------------------------------- #
def test_batch_compact_form_reads_every_asset_property_pair(batch_spy):
    out = json.loads(_batch()._tool_func(asset_ids_json=_ASSETS, property_ids_json=_PROPS))
    assert batch_spy == [_CROSS]
    assert set(out) >= {"asset-1_prop-a", "asset-1_prop-b", "asset-2_prop-a", "asset-2_prop-b"}


def test_batch_compact_form_drops_repeated_ids_keeping_order(batch_spy):
    _batch()._tool_func(asset_ids_json='["asset-2", "asset-1", "asset-2"]',
                        property_ids_json='["prop-a", "prop-a"]')
    assert batch_spy == [[{"assetId": "asset-2", "propertyId": "prop-a"},
                          {"assetId": "asset-1", "propertyId": "prop-a"}]]


@pytest.mark.parametrize("kwargs", [
    # Both forms at once is ambiguous.
    {"entries_json": '[{"assetId": "asset-1", "propertyId": "prop-a"}]',
     "asset_ids_json": _ASSETS, "property_ids_json": _PROPS},
    # Half of the pair.
    {"asset_ids_json": _ASSETS},
    {"property_ids_json": _PROPS},
    # Not JSON / not a list / empty / non-string / blank / overlong id.
    {"asset_ids_json": "asset-1", "property_ids_json": _PROPS},
    {"asset_ids_json": '{"asset-1": 1}', "property_ids_json": _PROPS},
    {"asset_ids_json": "[]", "property_ids_json": _PROPS},
    {"asset_ids_json": _ASSETS, "property_ids_json": "[1, 2]"},
    {"asset_ids_json": '["asset-1", "  "]', "property_ids_json": _PROPS},
    {"asset_ids_json": json.dumps(["a" * 257]), "property_ids_json": _PROPS},
])
def test_batch_compact_form_rejects_bad_requests_without_reading(batch_spy, kwargs):
    out = json.loads(_batch()._tool_func(**kwargs))
    assert out.get("error")
    assert out.get("error_type") == "invalid_request"
    assert batch_spy == []


def test_batch_compact_form_over_the_bound_asks_to_narrow(batch_spy):
    limit = sa.SITEWISE_MAX_COMPACT_ENTRIES
    assets = json.dumps([f"asset-{i}" for i in range(limit // 2 + 1)])
    out = json.loads(_batch()._tool_func(asset_ids_json=assets,
                                         property_ids_json=_PROPS))
    assert "narrow" in out["error"]
    assert out["error_type"] == "invalid_request"
    assert out["limit"] == limit
    assert batch_spy == []


def test_batch_compact_form_at_the_bound_is_read(batch_spy):
    limit = sa.SITEWISE_MAX_COMPACT_ENTRIES
    assets = json.dumps([f"asset-{i}" for i in range(limit // 2)])
    _batch()._tool_func(asset_ids_json=assets, property_ids_json=_PROPS)
    assert len(batch_spy[0]) == limit


# --------------------------------------------------------------------------- #
# 2. entries_json is byte-for-byte unchanged
# --------------------------------------------------------------------------- #
def test_batch_entries_json_call_is_unchanged(batch_spy):
    raw = '[{"assetId": "asset-1", "propertyId": "prop-a"}]'
    out = _batch()._tool_func(entries_json=raw)
    assert out == (
        '{"asset-1_prop-a": {"value": 1.0, "timestamp": 10, "quality": "GOOD"}, '
        '"_meta": {"boundary": "sitewise", "system_id": "sys-x", '
        '"queried_at": "2026-01-05 00:00:00+00:00"}}')
    assert batch_spy == [[{"assetId": "asset-1", "propertyId": "prop-a"}]]


def test_blank_compact_arguments_take_the_entries_json_path(batch_spy):
    raw = '[{"assetId": "asset-1", "propertyId": "prop-a"}]'
    batch = _batch()
    assert batch._tool_func(entries_json=raw, asset_ids_json="",
                            property_ids_json="") == batch._tool_func(entries_json=raw)
    assert batch_spy == [[{"assetId": "asset-1", "propertyId": "prop-a"}]] * 2


def test_batch_entries_json_errors_are_unchanged(batch_spy):
    batch = _batch()
    assert batch._tool_func(entries_json="not-json") == (
        '{"error": "entries_json is not valid JSON"}')
    assert batch._tool_func(entries_json='[{"assetId": "a"}]') == (
        '{"error": "entries_json must contain assetId/propertyId objects"}')
    assert batch_spy == []


def test_history_entries_json_call_is_unchanged(history_spy):
    raw = '[{"assetId": "asset-1", "propertyId": "prop-a"}]'
    out = _history()._tool_func(entries_json=raw, hours_back=3, max_results=50)
    assert out == (
        '{"asset-1_prop-a": [], "_meta": {"boundary": "sitewise", "system_id": '
        '"sys-x", "queried_at": "2026-01-05 00:00:00+00:00"}}')
    assert history_spy == [{"entries": [{"assetId": "asset-1", "propertyId": "prop-a"}],
                            "hours_back": 3, "max_results": 50,
                            "start_iso": None, "end_iso": None}]


def test_history_entries_json_errors_are_unchanged(history_spy):
    hist = _history()
    assert hist._tool_func(entries_json="not-json") == (
        '{"error": "entries_json is not valid JSON", "error_type": "invalid_request"}')
    oversized = "x" * (sitewise_read.HISTORY_MAX_REQUEST_JSON_CHARS + 1)
    assert hist._tool_func(entries_json=oversized) == (
        '{"error": "entries_json is too large", "error_type": "invalid_request"}')
    assert history_spy == []


# --------------------------------------------------------------------------- #
# 3. History: same compact form, bounded by the reader's own entry cap
# --------------------------------------------------------------------------- #
def test_history_compact_form_expands_and_forwards_the_window(history_spy):
    _history()._tool_func(asset_ids_json=_ASSETS, property_ids_json=_PROPS,
                          start_iso=" 2026-01-04T00:00:00Z ", end_iso="2026-01-05T00:00:00Z")
    assert history_spy[0]["entries"] == _CROSS
    assert history_spy[0]["start_iso"] == "2026-01-04T00:00:00Z"
    assert history_spy[0]["end_iso"] == "2026-01-05T00:00:00Z"


def test_history_compact_form_over_the_reader_cap_asks_to_narrow(history_spy):
    cap = sitewise_read.HISTORY_MAX_ENTRIES
    assets = json.dumps([f"asset-{i}" for i in range(cap // 2 + 1)])
    out = json.loads(_history()._tool_func(asset_ids_json=assets,
                                           property_ids_json=_PROPS))
    assert "narrow" in out["error"]
    assert out["error_type"] == "invalid_request"
    assert out["limit"] == cap
    assert history_spy == []


def test_history_compact_form_rejects_mixed_forms(history_spy):
    out = json.loads(_history()._tool_func(
        entries_json='[{"assetId": "asset-1", "propertyId": "prop-a"}]',
        asset_ids_json=_ASSETS, property_ids_json=_PROPS))
    assert out["error_type"] == "invalid_request"
    assert history_spy == []


# --------------------------------------------------------------------------- #
# 4. The model is told about the compact form
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", [_batch, _history])
def test_tool_specs_offer_the_compact_form(factory):
    spec = factory().tool_spec
    schema = spec["inputSchema"]["json"]
    assert {"asset_ids_json", "property_ids_json", "entries_json"} <= set(schema["properties"])
    assert "entries_json" not in schema.get("required", [])
    description = spec["description"]
    assert "asset_ids_json" in description and "property_ids_json" in description


def test_seeded_and_unseeded_policies_name_the_compact_form():
    for policy in (ct.sitewise_prompt_policy(True), ct.sitewise_prompt_policy(False),
                   ct.sitewise_prompt_policy(True, partial=True),
                   ct.sitewise_dual_path_guidance()):
        assert "asset_ids_json" in policy and "property_ids_json" in policy
        assert "one logical batch read" in policy


# --------------------------------------------------------------------------- #
# 5. Telemetry coverage lines read the compact form exactly
# --------------------------------------------------------------------------- #
def test_coverage_lines_name_asset_and_property_for_compact_input():
    tool_input = {"asset_ids_json": '["asset_1"]', "property_ids_json": '["prop_a"]',
                  "hours_back": 8}
    result = {"content": [{"text": json.dumps(
        {"asset_1_prop_a": [{"value": 1.0, "timestamp": 1767571200}]})}]}
    lines = coverage_lines("sitewise_get_asset_property_history", tool_input, result)
    # Underscored ids would split wrongly on the "<asset>_<property>" key
    # heuristic; the tool input is the exact source.
    assert lines == ["Telemetry read: asset_1 / prop_a, last 8 h, 1 point "
                     "(samples 2026-01-05T00:00Z to 2026-01-05T00:00Z)"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
