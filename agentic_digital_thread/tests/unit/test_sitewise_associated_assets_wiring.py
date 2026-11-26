# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``sitewise_list_associated_assets`` agent tool is wired into the SiteWise
(Direct) Mode A tool set and nowhere it should not be, its result shape is
summarized, the SiteWise (Direct) guidance mentions it, and the
``iotsitewise:ListAssociatedAssets`` grant is present on BOTH deploy paths
(CDK app + workshop package).

Fully synthetic — no AWS, no network, no dataset literals.
"""
from __future__ import annotations

import json
import pathlib

import agents.aws_session as aws_session
import agents.connection_types as ct
import agents.system_agent as sa
import agents.tool_result_summary as trs

_TOOL = "sitewise_list_associated_assets"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class _TreeClient:
    """Minimal iotsitewise double: one asset whose one child is a leaf."""

    def describe_asset(self, assetId):  # noqa: N803
        return {
            "assetId": assetId, "assetName": "SiteA", "assetModelId": "mdl-site",
            "assetProperties": [],
            "assetHierarchies": [{"id": "h-area", "name": "areas"}],
        }

    def get_paginator(self, name):
        assert name == "list_associated_assets", name

        def _paginate(**kwargs):
            self.last_kwargs = dict(kwargs)
            return iter([{"assetSummaries": [
                {"id": "area-1", "name": "AreaA1", "assetModelId": "mdl-area",
                 "hierarchies": []},
            ]}])
        return type("P", (), {"paginate": staticmethod(_paginate)})()


def _tools(monkeypatch, client=None):
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client or _TreeClient())
    return sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})


def _by_name(tools, name):
    return next((t for t in tools if t.tool_name == name), None)


# --------------------------------------------------------------------------- #
# Tool is in the Mode A SiteWise (Direct) set
# --------------------------------------------------------------------------- #
def test_tool_is_in_the_sitewise_direct_mode_a_set(monkeypatch):
    tools = _tools(monkeypatch)
    assert _by_name(tools, _TOOL) is not None


def test_tool_is_not_in_the_i3x_sitewise_set(monkeypatch):
    """i3X/HighByte SiteWise mode has its own tools and must NOT gain this one."""
    # The i3X tool set is built by the OpenAPI/i3X factory, which exposes
    # sitewise_get_related_objects, never sitewise_list_associated_assets.
    src = pathlib.Path(sa.__file__).read_text()
    # The new child-asset tool is registered exactly once, in the Direct factory.
    assert src.count(f'name="{_TOOL}"') == 1


def test_tool_walks_children_and_returns_summarizable_shape(monkeypatch):
    tools = _tools(monkeypatch)
    out = json.loads(_by_name(tools, _TOOL)._tool_func(asset_id="siteA", direction="CHILD"))
    assert out["assets"][0]["name"] == "AreaA1"
    assert out["truncated"] is False
    assert out["assets"][0]["path"] == ["AreaA1"]


def test_tool_default_depth_reaches_equipment_under_a_site(monkeypatch):
    # The tool's own default must walk site -> area -> line -> equipment.
    tools = _tools(monkeypatch)
    out = json.loads(_by_name(tools, _TOOL)._tool_func(asset_id="siteA"))
    assert out["requested_max_depth"] == 3


def test_tool_bad_asset_id_surfaces_actionable_error(monkeypatch):
    class _Boom(_TreeClient):
        def get_paginator(self, name):
            def _paginate(**kwargs):
                raise Exception("ResourceNotFoundException: nope")
            return type("P", (), {"paginate": staticmethod(_paginate)})()
    tools = _tools(monkeypatch, _Boom())
    out = json.loads(_by_name(tools, _TOOL)._tool_func(asset_id="ghost"))
    assert "error" in out
    assert "do NOT guess" in out["error"]


def test_tool_invalid_direction_surfaces_invalid_request(monkeypatch):
    tools = _tools(monkeypatch)
    out = json.loads(_by_name(tools, _TOOL)._tool_func(asset_id="siteA", direction="UP"))
    assert out["error_type"] == "invalid_request"


# --------------------------------------------------------------------------- #
# Result-shape summary (tool_result_summary)
# --------------------------------------------------------------------------- #
def _wrap(payload) -> dict:
    """Wrap a payload the way summarize_tool_result consumes it."""
    return {"content": [{"text": json.dumps(payload)}]}


def test_result_summary_describes_associated_assets_shape():
    payload = {
        "assets": [
            {"id": "a1", "name": "Line1CNC1", "depth": 1},
            {"id": "a2", "name": "Line1CNC2", "depth": 1},
            {"id": "a3", "name": "Line1CNC3", "depth": 1},
        ],
        "truncated": False, "reason": "", "direction": "CHILD", "root": "areaA1",
    }
    summary = trs.summarize_tool_result(_TOOL, _wrap(payload))
    assert "Line1CNC1" in summary
    assert "3" in summary


def test_result_summary_notes_truncation():
    payload = {
        "assets": [{"id": "a1", "name": "M1", "depth": 1}],
        "truncated": True, "reason": "max_assets", "direction": "CHILD", "root": "r",
    }
    summary = trs.summarize_tool_result(_TOOL, _wrap(payload))
    assert "truncated" in summary.lower() or "max_assets" in summary


# --------------------------------------------------------------------------- #
# Guidance mentions the tool
# --------------------------------------------------------------------------- #
def test_sitewise_direct_guidance_mentions_the_tool():
    assert _TOOL in ct._SITEWISE_DIRECT_GUIDANCE


# --------------------------------------------------------------------------- #
# IAM: iotsitewise:ListAssociatedAssets on BOTH deploy paths.
# A packaged (nested-stack) build reuses the SAME CDK child stacks
# (ADTAgentCoreStack / ADTComputeStack), so the runtime-role grant defined in
# stacks/ is what the workshop deploys. These source-level assertions lock the
# action in place (a missing grant passes every behavioural test but fails
# live). Offline, no synth required.
# --------------------------------------------------------------------------- #
_ACTION = '"iotsitewise:ListAssociatedAssets"'


def test_iam_runtime_role_grants_list_associated_assets_cdk_app():
    """The per-system AgentCore RuntimeRole (CDK app deploy path)."""
    text = (_REPO_ROOT / "stacks" / "agentcore_stack.py").read_text()
    assert _ACTION in text


def test_iam_app_role_grants_list_associated_assets_cdk_app():
    """The ECS app/compute role (CDK app deploy path)."""
    text = (_REPO_ROOT / "stacks" / "adt_compute_stack.py").read_text()
    assert _ACTION in text


