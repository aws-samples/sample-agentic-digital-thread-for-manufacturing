# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SiteWise (Direct) schema seeding — stop the per-question discovery crawl.

Every question to a blind SiteWise agent triggered a 20-27-call crawl
(list_asset_models -> list_assets -> describe-per-asset -> property reads)
purely to re-derive the inventory onboarding already discovered. The fix seeds
the agent's system prompt at construction with the asset-model/property
inventory from the SAME reader seam describe_schema uses
(``sitewise_read.list_asset_models_with_properties``), plus a SiteWise-specific
'schema already provided — do NOT re-discover' directive. Empty/missing schema
or ANY fetch failure -> no injection, behavior unchanged (the crawl is the
correct fallback).

All offline: the reader seam is monkeypatched; no AWS client call, no Bedrock
model (mirrors the test_system_agent_self_recovery stub idiom).
"""
import pytest

import agents.system_agent as sa
import agents.sitewise_read as sitewise_read


# --------------------------------------------------------------------------- #
# Stubs (mirror test_system_agent_self_recovery's prompt-inspection pattern)
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


_STUB_MODELS = [
    {
        "id": "11111111-aaaa-bbbb-cccc-222222222222",
        "name": "Washer",
        "properties": [
            {"id": "33333333-dddd-eeee-ffff-444444444444", "name": "RunningSpeed",
             "dataType": "DOUBLE", "unit": "rpm"},
            {"id": "55555555-dddd-eeee-ffff-666666666666", "name": "State",
             "dataType": "STRING", "unit": ""},
        ],
    },
    {
        "id": "77777777-aaaa-bbbb-cccc-888888888888",
        "name": "Filler",
        "properties": [
            {"id": "99999999-dddd-eeee-ffff-000000000000", "name": "ProducedCount",
             "dataType": "INTEGER", "unit": "units"},
        ],
    },
]


def _make_sitewise_agent(monkeypatch, models=None, fetch_raises=False, tables_desc=""):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)

    def _stub_enumeration(cfg, region, max_models=None):
        if fetch_raises:
            raise RuntimeError("simulated SiteWise outage")
        return models if models is not None else []

    # Patch the shared reader seam (system_agent imports it lazily via
    # ``from . import sitewise_read`` so the module attribute is honored).
    monkeypatch.setattr(
        sitewise_read, "list_asset_models_with_properties", _stub_enumeration
    )
    return sa.create_system_agent(
        system_id="example-sitewise",
        system_name="Example SiteWise",
        schema="sitewise-direct",
        system_prompt="",
        tables_desc=tables_desc,
        connection_type="SiteWise (Direct)",
        connection_config={},
    )


# --------------------------------------------------------------------------- #
# 1. format_sitewise_inventory — the pure renderer
# --------------------------------------------------------------------------- #
def test_renderer_emits_models_properties_and_real_ids():
    out = sa.format_sitewise_inventory(_STUB_MODELS)
    # Model names + ids (ids are what let the agent query directly).
    assert "Washer (model_id=11111111-aaaa-bbbb-cccc-222222222222)" in out
    assert "Filler (model_id=77777777-aaaa-bbbb-cccc-888888888888)" in out
    # Properties with type, unit, and property_id.
    assert "RunningSpeed: DOUBLE, rpm (property_id=33333333-dddd-eeee-ffff-444444444444)" in out
    assert "State: STRING (property_id=55555555-dddd-eeee-ffff-666666666666)" in out
    assert "ProducedCount: INTEGER, units" in out


def test_renderer_empty_or_degenerate_inventory_returns_empty():
    assert sa.format_sitewise_inventory([]) == ""
    assert sa.format_sitewise_inventory(None) == ""
    # Entries with no name/id are dropped; all-degenerate -> "".
    assert sa.format_sitewise_inventory([{"name": "", "id": "", "properties": []}]) == ""


def test_renderer_model_without_properties_still_listed():
    out = sa.format_sitewise_inventory([{"id": "m-1", "name": "Bare", "properties": []}])
    assert "Bare (model_id=m-1): (no properties)" in out


def test_renderer_enforces_size_cap_with_truncation_note():
    many = [
        {
            "id": f"model-{i:04d}-aaaa-bbbb-cccc-dddddddddddd",
            "name": f"Model{i}",
            "properties": [
                {"id": f"prop-{i:04d}-{j}-eeee-ffff-000000000000",
                 "name": f"Property{j}", "dataType": "DOUBLE", "unit": "u"}
                for j in range(10)
            ],
        }
        for i in range(200)
    ]
    out = sa.format_sitewise_inventory(many)
    assert len(out) <= sa._SITEWISE_INVENTORY_CAP
    # Whole-model truncation with an explicit note naming shown/total counts and
    # the discovery tools for ONLY the unlisted remainder.
    assert "inventory truncated: showing" in out
    assert "of 200 asset models" in out
    assert "sitewise_list_asset_models" in out
    # A custom (smaller) cap is honored too.
    small = sa.format_sitewise_inventory(many, cap=1000)
    assert len(small) <= 1000
    assert "inventory truncated" in small


def test_renderer_under_cap_has_no_truncation_note():
    out = sa.format_sitewise_inventory(_STUB_MODELS)
    assert "truncated" not in out
    assert len(out) <= sa._SITEWISE_INVENTORY_CAP


# --------------------------------------------------------------------------- #
# 2. Prompt assembly — seeded agent carries the map + no-rediscovery guidance
# --------------------------------------------------------------------------- #
def test_seeded_agent_prompt_contains_property_map_and_guidance(monkeypatch):
    agent = _make_sitewise_agent(monkeypatch, models=_STUB_MODELS)
    prompt = agent.system_prompt

    # The injected inventory (with real ids) is in the system context.
    assert "Onboarded asset models and properties:" in prompt
    assert "Washer (model_id=11111111-aaaa-bbbb-cccc-222222222222)" in prompt
    assert "RunningSpeed: DOUBLE, rpm" in prompt

    # The no-rediscovery guidance: query the map directly; list_* re-discovery
    # only after a real failure.
    assert "SCHEMA ALREADY PROVIDED" in prompt
    assert "onboarded schema" in prompt
    lower_prompt = prompt.lower()
    assert "do not call sitewise_list_asset_models" in lower_prompt
    assert "concrete failed exact read" in lower_prompt
    # Asset-id resolution stays permitted (the inventory has model/property ids,
    # not asset ids) — the directive must NOT strand the agent.
    assert "sitewise_list_assets(asset_model_id=" in prompt
    # Reconciled with the standing directives.
    assert "SELF-RECOVERY" in prompt
    assert "EFFICIENCY" in prompt


def test_caller_supplied_tables_desc_is_not_overwritten(monkeypatch):
    called = {"n": 0}

    def _counting(cfg, region, max_models=None):
        called["n"] += 1
        return _STUB_MODELS

    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _counting)
    agent = sa.create_system_agent(
        system_id="example-sitewise",
        system_name="Example SiteWise",
        schema="sitewise-direct",
        system_prompt="",
        tables_desc="- Preseeded (model_id=m-0): P: DOUBLE",
        connection_type="SiteWise (Direct)",
        connection_config={},
    )
    # No fetch when the caller already provided a schema; the provided one wins.
    assert called["n"] == 0
    assert "Preseeded (model_id=m-0)" in agent.system_prompt


# --------------------------------------------------------------------------- #
# 3. Empty / failing schema -> no injection, no crash (today's behavior)
# --------------------------------------------------------------------------- #
def test_empty_inventory_means_no_injection_and_no_crash(monkeypatch):
    agent = _make_sitewise_agent(monkeypatch, models=[])
    prompt = agent.system_prompt
    assert "SCHEMA ALREADY PROVIDED" not in prompt
    assert "Onboarded asset models and properties:" not in prompt
    # The standing directives (incl. the discovery-capable SELF-RECOVERY) survive,
    # so the discovery crawl remains available as the fallback.
    assert "SELF-RECOVERY" in prompt
    assert "EFFICIENCY" in prompt


def test_fetch_failure_falls_back_silently(monkeypatch):
    agent = _make_sitewise_agent(monkeypatch, fetch_raises=True)
    prompt = agent.system_prompt
    assert "SCHEMA ALREADY PROVIDED" not in prompt
    assert "Onboarded asset models and properties:" not in prompt
    assert "SELF-RECOVERY" in prompt


def test_helper_never_raises_and_prefers_config_region(monkeypatch):
    seen = {}

    def _capture(cfg, region, max_models=None):
        seen["region"] = region
        return _STUB_MODELS

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture)
    out = sa._sitewise_inventory_desc({"region": "eu-central-1"}, "us-east-1")
    assert seen["region"] == "eu-central-1"
    assert "Washer" in out

    def _boom(cfg, region, max_models=None):
        raise RuntimeError("denied")

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _boom)
    assert sa._sitewise_inventory_desc({}, "us-east-1") == ""


# --------------------------------------------------------------------------- #
# 4. Non-SiteWise types are untouched by the seeding path
# --------------------------------------------------------------------------- #
def test_postgres_prompt_path_unchanged(monkeypatch):
    def _never(cfg, region, max_models=None):  # pragma: no cover - must not run
        raise AssertionError("SiteWise seeding must not run for PostgreSQL")

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _never)
    prompt = sa._compose_system_prompt(
        system_id="cmms-x",
        system_name="CMMS X",
        schema="cmms",
        system_prompt="",
        tables_desc="assets(asset_id: varchar)",
        connection_type="PostgreSQL",
    )
    assert "Available tables:" in prompt
    assert "Onboarded asset models and properties:" not in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def _assert_one_sitewise_policy(prompt: str):
    assert prompt.count("SITEWISE QUERY POLICY (required)") == 1
    assert prompt.count("TRAVERSAL BUDGET:") == 1
    assert "When an onboarded schema is present:" not in prompt
    assert "When no onboarded schema is present:" not in prompt


def test_seeded_prompt_requires_one_logical_batch_read(monkeypatch):
    prompt = _make_sitewise_agent(monkeypatch, models=_STUB_MODELS).system_prompt
    _assert_one_sitewise_policy(prompt)
    lower = prompt.lower()
    assert "at most once per model" in lower
    assert "one logical batch read" in lower
    assert "concrete failed exact read" in lower
    assert "list root" not in lower


def _assert_unseeded_final_prompt_is_consistent(prompt: str):
    _assert_one_sitewise_policy(prompt)
    lower = prompt.lower()
    assert "schema already provided" not in lower
    assert "no onboarded schema" in lower
    assert "sitewise_list_asset_models" in lower
    assert "bounded" in lower or "at most once" in lower
    assert "never invent" in lower or "never fabricate" in lower
    # This seeded-only prerequisite would make first discovery impossible.
    assert "only after a concrete failed exact read may you introspect" not in lower


def test_unseeded_final_prompt_has_one_bounded_discovery_policy(monkeypatch):
    agent = _make_sitewise_agent(monkeypatch, models=[])
    _assert_unseeded_final_prompt_is_consistent(agent.system_prompt)


def test_fetch_failure_final_prompt_has_same_bounded_discovery_policy(monkeypatch):
    agent = _make_sitewise_agent(monkeypatch, fetch_raises=True)
    _assert_unseeded_final_prompt_is_consistent(agent.system_prompt)


# --------------------------------------------------------------------------- #
# Asset-instantiation samples in the seeded inventory
# --------------------------------------------------------------------------- #
def test_inventory_renders_the_asset_sample_for_a_model():
    out = sa.format_sitewise_inventory([{
        "id": "m1", "name": "State_Event",
        "properties": [{"id": "p1", "name": "reason_code", "dataType": "STRING"}],
        "asset_samples": {"names": ["Line1_State", "Line2_State"], "total": 2},
    }])
    assert "[assets: Line1_State, Line2_State]" in out


def test_inventory_marks_a_truncated_asset_sample_honestly():
    out = sa.format_sitewise_inventory([{
        "id": "m1", "name": "State_Event",
        "properties": [{"id": "p1", "name": "reason_code"}],
        "asset_samples": {"names": ["A_State"], "total": 12},
    }])
    assert "[assets (1 of 12): A_State, …]" in out


def test_inventory_unchanged_when_no_sample_present():
    """No sample ⇒ byte-identical to the pre-existing seed."""
    model = [{"id": "m1", "name": "M",
              "properties": [{"id": "p1", "name": "v", "dataType": "DOUBLE"}]}]
    assert "[assets" not in sa.format_sitewise_inventory(model)


def test_structural_only_models_are_not_sampled(monkeypatch):
    """property_count == 0 is a CORRECT value and never gets a history read, so
    sampling its assets would spend prompt budget for nothing."""
    seen = {}

    def fake_sampler(cfg, region, ids, **kw):
        seen["ids"] = list(ids)
        return {}

    monkeypatch.setattr(sitewise_read, "sample_asset_names_by_model", fake_sampler)
    models = [
        {"id": "structural", "name": "Site", "properties": []},
        {"id": "eventful", "name": "State_Event",
         "properties": [{"id": "p", "name": "reason"}]},
    ]
    sa._attach_asset_samples({}, "us-east-1", models)
    assert seen["ids"] == ["eventful"]
