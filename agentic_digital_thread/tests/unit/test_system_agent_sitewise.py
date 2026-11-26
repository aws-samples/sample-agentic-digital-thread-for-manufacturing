# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wiring test: the SiteWise (Direct) connector builds its iotsitewise client
through the generic ``agents.aws_session`` helper, so an OPTIONAL ``role_arn`` in
``connection_config`` triggers cross-account assumption while a blank config
keeps ambient credentials (unchanged behavior).

Fully synthetic — the helper is monkeypatched to return a fake iotsitewise
client; no AWS, no network, no dataset literals.
"""
import json

import pytest

import agents.aws_session as aws_session
import agents.system_agent as sa


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class FakeSiteWise:
    """Minimal iotsitewise client double for list_asset_models."""

    def __init__(self):
        self.paginator_names = []

    def get_paginator(self, name):
        self.paginator_names.append(name)
        return _FakePaginator([
            {"assetModelSummaries": [
                {"id": "m1", "name": "Site", "status": {"state": "ACTIVE"}, "description": ""},
            ]}
        ])


def _invoke(tool):
    """Call the underlying function behind a Strands @tool wrapper."""
    return tool._tool_func()


def test_sitewise_direct_builds_client_via_helper_with_config(monkeypatch):
    captured = {}

    def fake_helper(service, connection_config, default_region=None):
        captured["service"] = service
        captured["config"] = connection_config
        captured["default_region"] = default_region
        return FakeSiteWise()

    monkeypatch.setattr(aws_session, "boto3_client_for", fake_helper)

    cfg = {"role_arn": "arn:aws:iam::111122223333:role/sw", "external_id": "sw-ext", "region": "us-west-2"}
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config=cfg)
    # tools[0] == sitewise_list_asset_models
    out = json.loads(_invoke(tools[0]))

    assert out and out[0]["name"] == "Site"
    assert captured["service"] == "iotsitewise"
    # The cross-account config flows through to the shared helper unchanged.
    assert captured["config"]["role_arn"] == "arn:aws:iam::111122223333:role/sw"
    assert captured["config"]["external_id"] == "sw-ext"


def test_sitewise_direct_ambient_when_no_role_arn(monkeypatch):
    captured = {}

    def fake_helper(service, connection_config, default_region=None):
        captured["config"] = connection_config
        return FakeSiteWise()

    monkeypatch.setattr(aws_session, "boto3_client_for", fake_helper)

    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    _invoke(tools[0])
    # Blank config passes through — the helper resolves this to ambient credentials.
    assert captured["config"] == {}


# --------------------------------------------------------------------------- #
# B2 — list_assets with NO asset_model_id must return TOP_LEVEL root assets
#      (not raise "AssetModelID is required when listing ALL assets").
# B3 — describe_asset for an unknown id returns an ACTIONABLE guidance error.
# --------------------------------------------------------------------------- #

class _CapturingPaginator:
    def __init__(self, pages, sink):
        self._pages = pages
        self._sink = sink

    def paginate(self, **kwargs):
        self._sink.append(kwargs)
        return iter(self._pages)


class _FakeSiteWiseAssets:
    """iotsitewise double that records list_assets paginate kwargs and can raise
    a ResourceNotFoundException-shaped error from describe_asset."""

    def __init__(self):
        self.paginate_kwargs = []

    def get_paginator(self, name):
        assert name == "list_assets", name
        return _CapturingPaginator(
            [{"assetSummaries": [
                {"id": "root-1", "name": "Root", "assetModelId": "m1",
                 "status": {"state": "ACTIVE"}, "hierarchies": []},
            ]}],
            self.paginate_kwargs,
        )

    def describe_asset(self, assetId):  # noqa: N803 (boto3 kwarg name)
        raise Exception("ResourceNotFoundException: Asset bogus-id not found")


def _sitewise_tools(monkeypatch, client):
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)
    return sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})


def _tool_by_name(tools, name):
    return next(t for t in tools if t.tool_name == name)


def test_list_assets_without_model_id_uses_top_level_filter(monkeypatch):
    client = _FakeSiteWiseAssets()
    tools = _sitewise_tools(monkeypatch, client)
    list_assets = _tool_by_name(tools, "sitewise_list_assets")

    out = json.loads(list_assets._tool_func(asset_model_id=""))

    assert client.paginate_kwargs == [{"filter": "TOP_LEVEL"}]
    assert out and out[0]["name"] == "Root"


def test_list_assets_with_model_id_filters_by_model(monkeypatch):
    client = _FakeSiteWiseAssets()
    tools = _sitewise_tools(monkeypatch, client)
    list_assets = _tool_by_name(tools, "sitewise_list_assets")

    list_assets._tool_func(asset_model_id="m1")

    assert client.paginate_kwargs == [{"assetModelId": "m1"}]


def test_describe_asset_unknown_id_returns_actionable_guidance(monkeypatch):
    client = _FakeSiteWiseAssets()
    tools = _sitewise_tools(monkeypatch, client)
    describe = _tool_by_name(tools, "sitewise_describe_asset")

    out = json.loads(describe._tool_func(asset_id="bogus-id"))

    assert "error" in out
    lower = out["error"].lower()
    # Actionable and fixed: points to recovery without echoing the caller ID.
    assert "bogus-id" not in out["error"]
    assert "sitewise_list_assets" in out["error"] or "list" in lower
    assert out["error_type"]


# --------------------------------------------------------------------------- #
# B2 DRY — the SHARED enumeration helper (used by BOTH the @tool path and the
#          create_mcp_server gateway path) and the gateway path itself.
# --------------------------------------------------------------------------- #

class _KwargCapturingClient:
    """iotsitewise double: records list_assets paginate kwargs; batch call is a no-op."""

    def __init__(self):
        self.paginate_kwargs = []

    def get_paginator(self, name):
        assert name == "list_assets", name
        return _CapturingPaginator(
            [{"assetSummaries": [
                {"id": "root-1", "name": "Root", "assetModelId": "m1",
                 "status": {"state": "ACTIVE"}, "hierarchies": []},
            ]}],
            self.paginate_kwargs,
        )

    def batch_get_asset_property_value(self, entries):
        raise Exception("no _path in test")  # exercised path swallows this


def test_shared_helper_no_model_id_uses_top_level():
    """_sitewise_list_asset_summaries with NO model id -> filter=TOP_LEVEL."""
    client = _KwargCapturingClient()
    out = sa._sitewise_list_asset_summaries(client, "")
    assert client.paginate_kwargs == [{"filter": "TOP_LEVEL"}]
    assert out[0]["name"] == "Root"


def test_shared_helper_with_model_id_filters_by_model():
    """_sitewise_list_asset_summaries with a model id -> assetModelId=<that model>."""
    client = _KwargCapturingClient()
    sa._sitewise_list_asset_summaries(client, "m1")
    assert client.paginate_kwargs == [{"assetModelId": "m1"}]


def test_describe_error_helper_actionable_for_not_found():
    """Not-found guidance is actionable without raw identifiers or service text."""
    out = json.loads(sa._sitewise_describe_error(
        "graph-site-node", Exception("ResourceNotFoundException: nope")))
    assert "graph-site-node" not in out["error"]
    assert "ResourceNotFoundException" not in out["error"]
    assert "sitewise_list_assets" in out["error"]


def test_describe_error_helper_passthrough_for_other_errors():
    """Other service errors also use the fixed public boundary."""
    out = json.loads(sa._sitewise_describe_error("a", Exception("ThrottlingException")))
    assert "ThrottlingException" not in out["error"]
    assert out["error_type"] == "Exception"


def test_gateway_sitewise_direct_exposes_only_query_tool(monkeypatch):
    """NEW contract (Task 1A / ADR-0035): ``create_mcp_server`` for SiteWise (Direct)
    now routes through ``create_system_agent`` and exposes ONLY the single
    ``query_<id>`` gateway tool. The raw ``sitewise_*`` tools (incl.
    ``sitewise_list_assets``) are INTERNAL to the system agent — no longer on the
    gateway surface, so calling one raises "Unknown tool". The TOP_LEVEL enumeration
    behavior those internal tools implement is covered by the @tool-path and shared-
    helper tests above (``test_list_assets_without_model_id_uses_top_level_filter`` /
    ``test_shared_helper_no_model_id_uses_top_level``)."""
    import asyncio

    # Keep the agent build offline — stub create_system_agent to an inert sentinel
    # so no Bedrock model / AWS client is constructed.
    monkeypatch.setattr(sa, "create_system_agent", lambda *a, **k: object())
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")

    mcp = sa.create_mcp_server(
        system_id="example-sitewise", system_name="Example SiteWise", schema="sitewise",
        system_prompt="test", connection_type="SiteWise (Direct)", connection_config={},
    )
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    # Exactly one gateway tool; the raw enumeration tool is no longer exposed.
    assert names == {"query_example_sitewise"}
    assert "sitewise_list_assets" not in names
    with pytest.raises(Exception):
        asyncio.run(mcp.call_tool("sitewise_list_assets", {"asset_model_id": ""}))
