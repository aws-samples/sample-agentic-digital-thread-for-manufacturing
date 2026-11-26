# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SiteWise batch CURRENT-value tool + multi-asset prompt directive.

The observed failure: 'current machine speed and throughput for all assets
across all sites' drove the SiteWise agent through 56 internal per-asset
``get_asset_property_value`` reads, overrunning the ~75s gateway window into a
silent-empty envelope the orchestrator then retried. The fix is (a) ONE batch
current-value tool — ``sitewise_batch_get_property_values`` — that delegates to
the existing ``sitewise_read.batch_get_property_values`` seam (no duplicated
paging logic), and (b) a compact MULTI-ASSET READS prompt directive making the
batch tool mandatory for multi-asset reads (<12 tool calls per question).

Fully offline: the reader seam is monkeypatched (spy); no AWS client, no
Bedrock model (mirrors the test_sitewise_schema_seed stub idiom).
"""
import json

import pytest

import agents.sitewise_read as sitewise_read
import agents.system_agent as sa


def _tool_by_name(tools, name):
    return next(t for t in tools if t.tool_name == name)


class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []
        self.hooks = kwargs.get("hooks") or []


# --------------------------------------------------------------------------- #
# 1. Tool registration + delegation to the shared reader seam (spy)
# --------------------------------------------------------------------------- #
def test_batch_tool_registered_in_sitewise_direct_toolset():
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    names = {t.tool_name for t in tools}
    assert "sitewise_batch_get_property_values" in names
    # The single-read + batch-history tools remain (batch is additive).
    assert "sitewise_get_asset_property_value" in names
    assert "sitewise_get_asset_property_history" in names


def test_batch_tool_delegates_to_shared_reader_seam(monkeypatch):
    captured = {}

    def spy(cfg, region, entries):
        captured["cfg"] = cfg
        captured["region"] = region
        captured["entries"] = entries
        return {"a1_p1": {"value": 42.0, "timestamp": 10, "quality": "GOOD"},
                "a2_p1": {"error": "AccessDenied"}}

    monkeypatch.setattr(sitewise_read, "batch_get_property_values", spy)
    tools = sa._make_sitewise_direct_tool(
        region="us-east-1", connection_config={"role_arn": "arn:aws:iam::111122223333:role/sw"})
    batch = _tool_by_name(tools, "sitewise_batch_get_property_values")

    out = json.loads(batch._tool_func(
        entries_json='[{"assetId": "a1", "propertyId": "p1"}, {"assetId": "a2", "propertyId": "p1"}]'))

    # Delegation: the seam got the parsed entries + config/region unchanged.
    assert captured["entries"] == [
        {"assetId": "a1", "propertyId": "p1"},
        {"assetId": "a2", "propertyId": "p1"},
    ]
    assert captured["region"] == "us-east-1"
    assert captured["cfg"]["role_arn"] == "arn:aws:iam::111122223333:role/sw"
    # Seam result surfaces as-is (per-entry values AND per-entry errors).
    assert out["a1_p1"]["value"] == 42.0
    assert out["a2_p1"]["error"] == "AccessDenied"


def test_batch_tool_rejects_bad_json_without_calling_seam(monkeypatch):
    def _never(cfg, region, entries):  # pragma: no cover - must not run
        raise AssertionError("seam must not be called for bad input")

    monkeypatch.setattr(sitewise_read, "batch_get_property_values", _never)
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    batch = _tool_by_name(tools, "sitewise_batch_get_property_values")

    assert "error" in json.loads(batch._tool_func(entries_json="not-json"))
    # Well-formed JSON but the wrong shape is rejected too.
    assert "error" in json.loads(batch._tool_func(entries_json='{"assetId": "a"}'))
    assert "error" in json.loads(batch._tool_func(entries_json='[{"assetId": "a"}]'))


def test_batch_tool_description_prefers_batch_over_repeated_single_reads():
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})
    batch = _tool_by_name(tools, "sitewise_batch_get_property_values")
    spec_desc = batch.tool_spec.get("description", "")
    assert "ONE call" in spec_desc
    assert "sitewise_get_asset_property_value" in spec_desc
    # The single-read tool points back at the batch tool for multi-asset reads.
    single = _tool_by_name(tools, "sitewise_get_asset_property_value")
    assert "sitewise_batch_get_property_values" in single.tool_spec.get("description", "")


# --------------------------------------------------------------------------- #
# 2. Prompt directive — multi-asset reads MUST batch, <12 calls per question
# --------------------------------------------------------------------------- #
def _sitewise_prompt(tables_desc=""):
    return sa._compose_system_prompt(
        system_id="example-sitewise",
        system_name="Example SiteWise",
        schema="sitewise-direct",
        system_prompt="",
        tables_desc=tables_desc,
        connection_type="SiteWise (Direct)",
    )


def test_sitewise_prompt_contains_one_central_policy_with_seeded_schema():
    prompt = _sitewise_prompt(tables_desc="- Washer (model_id=m-1): Speed: DOUBLE")
    assert prompt.count("SITEWISE QUERY POLICY (required)") == 1
    assert prompt.count("TRAVERSAL BUDGET:") == 1
    assert "sitewise_batch_get_property_values" in prompt
    assert "one logical batch read" in prompt
    assert "SCHEMA ALREADY PROVIDED" in prompt


def test_sitewise_prompt_contains_one_central_policy_without_seeded_schema():
    prompt = _sitewise_prompt(tables_desc="")
    assert prompt.count("SITEWISE QUERY POLICY (required)") == 1
    assert prompt.count("TRAVERSAL BUDGET:") == 1
    assert "sitewise_batch_get_property_values" in prompt
    assert "one logical batch read" in prompt


def test_non_sitewise_prompts_do_not_get_the_batch_directive():
    for ctype in ("PostgreSQL", "Athena"):
        prompt = sa._compose_system_prompt(
            system_id="erp-x", system_name="ERP X", schema="erp_data",
            system_prompt="", tables_desc="t(a: int)", connection_type=ctype,
        )
        assert "MULTI-ASSET READS" not in prompt
        assert "sitewise_batch_get_property_values" not in prompt


# --------------------------------------------------------------------------- #
# 3. Non-SiteWise agents: toolset unchanged (no batch tool leaks across types)
# --------------------------------------------------------------------------- #
def test_postgres_agent_toolset_unchanged(monkeypatch):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    agent = sa.create_system_agent(
        system_id="erp-x", system_name="ERP X", schema="erp_data",
        system_prompt="", tables_desc="t(a: int)",
        conn_params={"host": "h", "dbname": "d"}, connection_type="PostgreSQL",
    )
    names = {t.tool_name for t in agent.tools}
    assert names == {"query_erp_data"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
