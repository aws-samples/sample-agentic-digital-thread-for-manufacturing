# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pins the curated model catalog: Claude Sonnet 5.5 is the DEFAULT orchestrator
and Claude Haiku 5.5 the default agent (standard list pricing — never an
introductory discount rate, so cost display never understates spend); Sonnet 5,
Sonnet 4.6 and Haiku 4.5 stay selectable."""

from agents.model_catalog import (
    AGENT,
    ORCHESTRATOR,
    catalog,
    default_model,
    pricing,
)


def test_default_orchestrator_is_sonnet_5_5():
    assert default_model(ORCHESTRATOR, "us-east-1") == "global.anthropic.claude-sonnet-5-5"


def test_default_agent_is_haiku_5_5():
    assert default_model(AGENT, "us-east-1") == "global.anthropic.claude-haiku-5-5"


def test_default_orchestrator_region_prefixes():
    # A model with a global profile resolves to it in every Region.
    assert default_model(ORCHESTRATOR, "eu-west-1") == "global.anthropic.claude-sonnet-5-5"
    assert default_model(ORCHESTRATOR, "ap-southeast-1") == "global.anthropic.claude-sonnet-5-5"
    # A model without one keeps its geo profile; APAC's prefix is apac.
    from agents.model_catalog import resolve_id
    assert resolve_id("amazon.nova-pro-v1:0", "ap-southeast-1") == "apac.amazon.nova-pro-v1:0"
    assert resolve_id("amazon.nova-pro-v1:0", "eu-west-1") == "eu.amazon.nova-pro-v1:0"
    assert resolve_id("amazon.nova-pro-v1:0", "us-east-1") == "us.amazon.nova-pro-v1:0"


def test_sonnet_5_pricing_is_the_billed_list_rate():
    # $2/$10 per MTok global (0.002/0.010 per 1K): the rate the Price List
    # bills (2026-10-22). Its launch notice named $3/$15 from 2026-09-01; that
    # rise never reached the Price List, so $2/$10 is the standard rate.
    assert pricing("global.anthropic.claude-sonnet-5") == {"input": 0.002, "output": 0.010}


def test_claude_5_5_pricing_is_the_global_standard_rate():
    # Price List API, us-east-1, 2026-10-21, on-demand Standard tier, global
    # profile ($ per MTok): Opus 5.5 4/20, Sonnet 5.5 2/10, Haiku 5.5 0.10/0.50.
    assert pricing("global.anthropic.claude-opus-5-5") == {"input": 0.004, "output": 0.020}
    assert pricing("global.anthropic.claude-sonnet-5-5") == {"input": 0.002, "output": 0.010}
    assert pricing("global.anthropic.claude-haiku-5-5") == {"input": 0.0001, "output": 0.0005}


def test_claude_5_5_models_resolve_to_their_global_profiles():
    from agents.model_catalog import resolve_id
    for family in ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5",
                   "anthropic.claude-haiku-5-5"):
        for region in ("us-east-1", "eu-west-1", "ap-southeast-1"):
            assert resolve_id(family, region) == f"global.{family}"


def test_every_model_with_a_global_profile_resolves_to_it():
    """Opus 4.7/4.8 and Nova Lite v2 publish ACTIVE global profiles (checked
    2026-10-22 in us-east-1, us-west-2, eu-west-1 and ap-southeast-2, and
    answered Converse on them in us-east-1 and us-west-2). Only Nova Lite v1
    and Nova Pro, which have none, keep the Region's geo profile; Mantle ids
    stay bare."""
    from agents.model_catalog import MODELS, resolve_id
    geo_only = {"amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"}
    for m in MODELS:
        for region in ("us-east-1", "eu-west-1", "ap-southeast-2"):
            got = resolve_id(m.family, region)
            if m.mantle:
                assert got == m.family
            elif m.family in geo_only:
                assert not got.startswith("global."), (m.family, region)
            else:
                assert got == f"global.{m.family}", (m.family, region)
    assert resolve_id("amazon.nova-pro-v1:0", "eu-west-1") == "eu.amazon.nova-pro-v1:0"
    assert resolve_id("amazon.nova-pro-v1:0", "ap-southeast-2") == "apac.amazon.nova-pro-v1:0"


def test_sonnet_4_6_remains_selectable_orchestrator_non_default():
    entries = catalog("us-east-1")[ORCHESTRATOR]
    by_id = {e["id"]: e for e in entries}
    assert "global.anthropic.claude-sonnet-4-6" in by_id
    # Sonnet 4.6 and Sonnet 5 stay selectable but are not the default.
    assert by_id["global.anthropic.claude-sonnet-4-6"]["default"] is False
    assert by_id["global.anthropic.claude-sonnet-5"]["default"] is False
    assert by_id["global.anthropic.claude-sonnet-5"]["label"] == "Claude Sonnet 5"
    assert by_id["global.anthropic.claude-sonnet-5-5"]["default"] is True
    assert by_id["global.anthropic.claude-sonnet-5-5"]["label"] == "Claude Sonnet 5.5"
    assert by_id["global.anthropic.claude-opus-5-5"]["default"] is False
    assert by_id["global.anthropic.claude-opus-5-5"]["label"] == "Claude Opus 5.5"


def test_exactly_one_default_per_role():
    for role in (ORCHESTRATOR, AGENT):
        defaults = [e for e in catalog("us-east-1")[role] if e["default"]]
        assert len(defaults) == 1, f"{role}: {defaults}"


def test_mantle_models_exempt_from_inference_profile_availability():
    """Mantle (openai.*) ids are NOT inference profiles — resolve_id returns
    them unchanged and list_inference_profiles never lists them, so judging
    them against the enabled-profile set would ALWAYS mark them unavailable
    (a false "(not enabled in region)" in the UI). They stay available."""
    available = {"global.anthropic.claude-sonnet-4-6"}  # no openai.* ids, ever
    entries = catalog("us-east-1", available_ids=available)[ORCHESTRATOR]
    by_id = {e["id"]: e for e in entries}
    assert by_id["openai.gpt-5.6-sol"]["available"] is True
    assert by_id["openai.gpt-5.6-terra"]["available"] is True
    # Non-mantle models are still judged against the set.
    assert by_id["global.anthropic.claude-sonnet-4-6"]["available"] is True
    assert by_id["global.anthropic.claude-opus-4-8"]["available"] is False


def test_both_role_lists_offer_every_model():
    """Steward's informed choice: each role's dropdown offers the FULL catalog
    (with pricing fields for the label hint); per-role defaults unchanged."""
    from agents.model_catalog import MODELS

    cat = catalog("us-east-1")
    for role in (ORCHESTRATOR, AGENT):
        entries = cat[role]
        assert len(entries) == len(MODELS)
        for e in entries:
            assert isinstance(e["input_per_1k"], float)
            assert isinstance(e["output_per_1k"], float)
    # Defaults stay per-role even though the lists are identical in membership.
    orch_defaults = [e["id"] for e in cat[ORCHESTRATOR] if e["default"]]
    agent_defaults = [e["id"] for e in cat[AGENT] if e["default"]]
    assert orch_defaults == ["global.anthropic.claude-sonnet-5-5"]
    assert agent_defaults == ["global.anthropic.claude-haiku-5-5"]


def test_role_field_keeps_native_role_in_both_lists():
    cat = catalog("us-east-1")
    orch_by_id = {e["id"]: e for e in cat[ORCHESTRATOR]}
    # An agent-native model listed under orchestrator keeps role="agent" so the
    # UI can annotate provenance; default is False there.
    haiku = orch_by_id["global.anthropic.claude-haiku-4-5-20251001-v1:0"]
    assert haiku["role"] == AGENT
    assert haiku["default"] is False
