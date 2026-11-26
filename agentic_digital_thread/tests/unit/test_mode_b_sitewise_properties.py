# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 2.2 — Mode A tools delegate to the shared reader; Mode B enumerates model
properties and performs a structured live read (ADR-0037).

Fully synthetic: ``sitewise_read`` is patched with SYNTHETIC model/property names.
No AWS, no network, no dataset literals, no SQL emitted for SiteWise.
"""
from __future__ import annotations

import json

import pytest

import agents.sitewise_read as sitewise_read
import agents.system_agent as sa


def _tool_by_name(tools, name):
    return next(t for t in tools if t.tool_name == name)


# --------------------------------------------------------------------------- #
# Mode A — thin shims over the shared reader
# --------------------------------------------------------------------------- #

def test_mode_a_history_tool_routes_through_shared_reader(monkeypatch):
    captured = {}

    def fake_history(cfg, region, entries, hours_back=8, max_results=100,
                     start_iso=None, end_iso=None):
        captured["entries"] = entries
        captured["hours_back"] = hours_back
        captured["start_iso"] = start_iso
        captured["end_iso"] = end_iso
        return {"a_p": [{"value": 1.0, "timestamp": 10, "quality": "GOOD"}]}

    monkeypatch.setattr(sitewise_read, "get_property_history", fake_history)
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    hist = _tool_by_name(tools, "sitewise_get_asset_property_history")

    out = json.loads(hist._tool_func(
        entries_json='[{"assetId": "a", "propertyId": "p"}]', hours_back=3))

    assert captured["entries"] == [{"assetId": "a", "propertyId": "p"}]
    assert captured["hours_back"] == 3
    # Blank tool defaults normalize to None (trailing-window behavior).
    assert captured["start_iso"] is None and captured["end_iso"] is None
    assert out["a_p"][0]["value"] == 1.0


def test_mode_a_history_tool_forwards_absolute_window(monkeypatch):
    captured = {}

    def fake_history(cfg, region, entries, hours_back=8, max_results=100,
                     start_iso=None, end_iso=None):
        captured["start_iso"] = start_iso
        captured["end_iso"] = end_iso
        return {}

    monkeypatch.setattr(sitewise_read, "get_property_history", fake_history)
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    hist = _tool_by_name(tools, "sitewise_get_asset_property_history")

    hist._tool_func(
        entries_json='[{"assetId": "a", "propertyId": "p"}]',
        start_iso=" 2026-07-25T00:00:00Z ", end_iso="2026-07-26T00:00:00Z")

    # Whitespace-trimmed and forwarded verbatim to the shared reader.
    assert captured["start_iso"] == "2026-07-25T00:00:00Z"
    assert captured["end_iso"] == "2026-07-26T00:00:00Z"


def test_mode_a_describe_asset_model_tool_returns_properties(monkeypatch):
    def fake_dam(cfg, region, model_id):
        return {"id": model_id, "name": "evt_model",
                "properties": [{"id": "p1", "name": "dur", "dataType": "DOUBLE", "unit": "s"}]}

    monkeypatch.setattr(sitewise_read, "describe_asset_model", fake_dam)
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    dam = _tool_by_name(tools, "sitewise_describe_asset_model")

    out = json.loads(dam._tool_func(asset_model_id="mdl-evt"))
    assert out["name"] == "evt_model"
    assert out["properties"][0]["name"] == "dur"


def test_mode_a_value_tool_routes_through_shared_reader(monkeypatch):
    monkeypatch.setattr(sitewise_read, "get_property_value",
                        lambda cfg, region, a, p: {"value": 7, "timestamp": 1, "quality": "GOOD"})
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    val = _tool_by_name(tools, "sitewise_get_asset_property_value")
    out = json.loads(val._tool_func(asset_id="a", property_id="p"))
    assert out["value"] == 7


# --------------------------------------------------------------------------- #
# Mode B — describe_schema returns properties; run_query does a structured read
# --------------------------------------------------------------------------- #

def _patch_models(monkeypatch):
    monkeypatch.setattr(
        sitewise_read, "list_asset_models_with_properties",
        lambda cfg, region, max_models=None: [
            {"id": "mdl-evt", "name": "evt_model",
             "properties": [{"id": "p-dur", "name": "dur", "dataType": "DOUBLE", "unit": "s"}]},
        ],
    )


def test_mode_b_describe_schema_includes_properties(monkeypatch):
    _patch_models(monkeypatch)
    res = sa._mode_b_describe_schema("SiteWise (Direct)", "", None, {})
    assert res["connection_type"] == "SiteWise (Direct)"
    model = res["asset_models"][0]
    assert model["name"] == "evt_model"
    assert model["properties"][0]["name"] == "dur"
    assert model["properties"][0]["dataType"] == "DOUBLE"


def test_mode_b_run_query_property_value_via_shared_reader(monkeypatch):
    captured = {}

    def fake_value(cfg, region, asset_id, property_id):
        captured["asset_id"] = asset_id
        captured["property_id"] = property_id
        return {"value": 42, "timestamp": 5, "quality": "GOOD"}

    monkeypatch.setattr(sitewise_read, "get_property_value", fake_value)
    req = json.dumps({"op": "property_value", "assetId": "asset-1", "propertyId": "p-dur"})
    res = sa._mode_b_run_query("SiteWise (Direct)", req, "", None, {})
    assert res["op"] == "property_value"
    assert res["result"]["value"] == 42
    assert captured == {"asset_id": "asset-1", "property_id": "p-dur"}


def test_mode_b_run_query_property_history_via_shared_reader(monkeypatch):
    captured = {}

    def fake_history(cfg, region, entries, hours_back=8, max_results=100,
                     start_iso=None, end_iso=None):
        captured["entries"] = entries
        captured["hours_back"] = hours_back
        captured["start_iso"] = start_iso
        captured["end_iso"] = end_iso
        return {"asset-1_p-dur": [{"value": 1.0, "timestamp": 10, "quality": "GOOD"}]}

    monkeypatch.setattr(sitewise_read, "get_property_history", fake_history)
    req = json.dumps({
        "op": "property_history",
        "entries": [{"assetId": "asset-1", "propertyId": "p-dur"}],
        "hours_back": 12,
    })
    res = sa._mode_b_run_query("SiteWise (Direct)", req, "", None, {})
    assert res["op"] == "property_history"
    assert "asset-1_p-dur" in res["result"]
    assert captured["hours_back"] == 12
    assert captured["start_iso"] is None and captured["end_iso"] is None


def test_mode_b_run_query_property_history_forwards_absolute_window(monkeypatch):
    """A day-targeted question must be expressible through the Mode B path too —
    otherwise the orchestrator can only ever ask for a trailing window."""
    captured = {}

    def fake_history(cfg, region, entries, hours_back=8, max_results=100,
                     start_iso=None, end_iso=None):
        captured["start_iso"] = start_iso
        captured["end_iso"] = end_iso
        return {}

    monkeypatch.setattr(sitewise_read, "get_property_history", fake_history)
    req = json.dumps({
        "op": "property_history",
        "entries": [{"assetId": "asset-1", "propertyId": "p-dur"}],
        "start_iso": "2026-07-25T00:00:00Z",
        "end_iso": "2026-07-26T00:00:00Z",
    })
    res = sa._mode_b_run_query("SiteWise (Direct)", req, "", None, {})
    assert res["op"] == "property_history"
    assert captured["start_iso"] == "2026-07-25T00:00:00Z"
    assert captured["end_iso"] == "2026-07-26T00:00:00Z"


def test_mode_b_guidance_names_the_absolute_window_option():
    """The guidance error is how the model learns the surface exists."""
    res = sa._mode_b_run_query("SiteWise (Direct)", "SELECT 1", "", None, {})
    assert "start_iso" in res["error"] and "end_iso" in res["error"]


def test_mode_b_run_query_non_json_returns_guidance_not_crash():
    res = sa._mode_b_run_query("SiteWise (Direct)", "SELECT * FROM foo", "", None, {})
    assert "error" in res
    assert "structured" in res["error"].lower()
    assert "describe_schema" in res["error"]
    # No SQL is emitted / attempted for SiteWise.
    assert "op" not in res


def test_mode_b_run_query_unknown_op_returns_guidance():
    req = json.dumps({"op": "bogus"})
    res = sa._mode_b_run_query("SiteWise (Direct)", req, "", None, {})
    assert "error" in res and "op" not in res


def test_mode_b_other_nonsql_types_still_sql_only():
    for ct in ("SiteWise (i3X)", "REST API", "MCP Server"):
        res = sa._mode_b_run_query(ct, "anything", "", None, {})
        assert "SQL-only" in res["error"]


def test_mode_b_history_invalid_hours_returns_structured_invalid_request(monkeypatch):
    monkeypatch.setattr(
        sitewise_read,
        "_client",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("AWS must not be called")),
    )
    req = json.dumps({
        "op": "property_history",
        "entries": [{"assetId": "asset-1", "propertyId": "property-1"}],
        "hours_back": {"bad": "shape"},
    })
    out = sa._mode_b_run_query("SiteWise (Direct)", req, "", None, {})
    assert out["result"]["error_type"] == "invalid_request"
    assert "hours_back" in out["result"]["error"]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("hours_back", {"n": 1}),
        ("hours_back", [8]),
        ("hours_back", True),
        ("hours_back", 10**100),
        ("max_results", {"n": 1}),
        ("max_results", [100]),
        ("max_results", False),
        ("max_results", 10**100),
    ],
)
def test_mode_b_history_rejects_invalid_raw_bounds(monkeypatch, field, bad_value):
    monkeypatch.setattr(
        sitewise_read,
        "_client",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("AWS must not be called")),
    )
    request = {
        "op": "property_history",
        "entries": [{"assetId": "asset-1", "propertyId": "property-1"}],
        field: bad_value,
    }
    out = sa._mode_b_run_query(
        "SiteWise (Direct)", json.dumps(request), "", None, {}
    )
    assert out["result"]["error_type"] == "invalid_request"
    assert field in out["result"]["error"]


@pytest.mark.parametrize(
    "entries",
    [
        {"assetId": "asset-1", "propertyId": "property-1"},
        [True],
        [{"assetId": "asset-1"}],
        [{"assetId": "asset-1", "propertyId": False}],
    ],
)
def test_mode_b_history_rejects_malformed_entries_without_io(monkeypatch, entries):
    monkeypatch.setattr(
        sitewise_read,
        "_client",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("AWS must not be called")),
    )
    out = sa._mode_b_run_query(
        "SiteWise (Direct)",
        json.dumps({"op": "property_history", "entries": entries}),
        "",
        None,
        {},
    )
    assert out["result"]["error_type"] == "invalid_request"


@pytest.mark.parametrize("field", ["assetId", "propertyId"])
@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param({"bad": "shape"}, id="dict"),
        pytest.param(["bad"], id="list"),
        pytest.param(True, id="bool"),
        pytest.param(123, id="numeric"),
        pytest.param(" \t\n", id="blank"),
        pytest.param(
            "x" * (sitewise_read.SITEWISE_IDENTIFIER_MAX_CHARS + 1),
            id="oversized",
        ),
    ],
)
def test_mode_b_property_value_rejects_invalid_identifier_without_io(
    monkeypatch, field, bad_value
):
    monkeypatch.setattr(
        sitewise_read,
        "get_property_value",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("SiteWise reader must not be called")
        ),
    )
    request = {
        "op": "property_value",
        "assetId": "asset-1",
        "propertyId": "property-1",
    }
    request[field] = bad_value

    out = sa._mode_b_run_query(
        "SiteWise (Direct)", json.dumps(request), "", None, {}
    )

    assert out["connection_type"] == "SiteWise (Direct)"
    assert out["op"] == "property_value"
    assert out["result"]["error_type"] == "invalid_request"
    assert field in out["result"]["error"]
    if isinstance(bad_value, str) and len(bad_value) > sitewise_read.SITEWISE_IDENTIFIER_MAX_CHARS:
        assert out["result"]["limit"] == sitewise_read.SITEWISE_IDENTIFIER_MAX_CHARS
