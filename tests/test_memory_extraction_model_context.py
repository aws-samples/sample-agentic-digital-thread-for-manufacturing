# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The saved memory extraction model reaches the stack as CDK context.

The Admin LLM settings page switches the AgentCore Memory's strategies live
and saves the model in SSM; deploy.sh passes it back as
``-c memory_extraction_model=<id>`` so the next deploy keeps it. Precedence:
``MEMORY_EXTRACTION_MODEL`` (env) > the context > the catalog default
(``model_catalog.MEMORY_DEFAULT_FAMILY``, the one home). The context must
name one of ``model_catalog.MEMORY_EXTRACTION_FAMILIES`` (any profile
spelling) and is resolved for the stack's Region; anything else fails the
synth, and a workshop synth refuses it (the workshop pins Nova Pro).

Built in process with ``assertions.Template`` (offline, no CDK CLI).
"""
from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from stacks import _model_allowlist
from stacks.agentcore_stack import AgentCoreStack, memory_extraction_family

ACCOUNT = "111122223333"   # AWS documentation placeholder
MEMORY = "AWS::BedrockAgentCore::Memory"
KEY = "memory_extraction_model"


def _model_ids(value) -> list:
    if isinstance(value, dict):
        found = [v for k, v in value.items() if k == "ModelId"]
        for k, v in value.items():
            if k != "ModelId":
                found += _model_ids(v)
        return found
    if isinstance(value, list):
        return [m for item in value for m in _model_ids(item)]
    return []


def _ids(tmp_path, *, region: str | None = "us-east-1", context: dict | None = None) -> list:
    app = cdk.App(context=context or {}, outdir=str(tmp_path))
    env = cdk.Environment(account=ACCOUNT, region=region) if region else None
    stack = AgentCoreStack(app, "ADTAgentCoreStack", env=env)
    memories = assertions.Template.from_stack(stack).find_resources(MEMORY)
    (memory,) = memories.values()
    ids = _model_ids(memory["Properties"])
    assert len(ids) == 5, ids
    return ids


@pytest.fixture(autouse=True)
def _no_synth_override(monkeypatch):
    monkeypatch.delenv("MEMORY_EXTRACTION_MODEL", raising=False)


def test_the_stack_default_is_the_catalog_memory_default():
    catalog = _model_allowlist.load_model_catalog()
    assert memory_extraction_family(with_workshop=False) == catalog.MEMORY_DEFAULT_FAMILY


@pytest.mark.parametrize("saved, region, expected", [
    ("global.anthropic.claude-sonnet-5-5", "us-east-1", "global.anthropic.claude-sonnet-5-5"),
    ("anthropic.claude-sonnet-5", "eu-west-1", "global.anthropic.claude-sonnet-5"),
    # Nova Pro saved in one Region resolves for the stack's.
    ("us.amazon.nova-pro-v1:0", "eu-west-1", "eu.amazon.nova-pro-v1:0"),
    ("us.amazon.nova-pro-v1:0", "ap-southeast-2", "apac.amazon.nova-pro-v1:0"),
    ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "us-west-2",
     "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
])
def test_saved_context_sets_every_strategy_model(tmp_path, saved, region, expected):
    assert _ids(tmp_path, region=region, context={KEY: saved}) == [expected] * 5


def test_account_agnostic_synth_resolves_the_context_for_the_region_context(tmp_path):
    ids = _ids(tmp_path, region=None,
               context={KEY: "amazon.nova-pro-v1:0", "region": "eu-west-1"})
    assert ids == ["eu.amazon.nova-pro-v1:0"] * 5


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_context_is_the_default(tmp_path, blank):
    assert _ids(tmp_path, context={KEY: blank}) == ["global.anthropic.claude-haiku-5-5"] * 5


def test_env_beats_the_context(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTION_MODEL", "us.amazon.nova-lite-v1:0")
    ids = _ids(tmp_path, context={KEY: "global.anthropic.claude-sonnet-5-5"})
    assert ids == ["us.amazon.nova-lite-v1:0"] * 5


@pytest.mark.parametrize("saved", [
    "global.anthropic.claude-opus-5-5",      # a catalog model AgentCore Memory was not checked with
    "global.anthropic.claude-sonnet-4-6",
    "openai.gpt-5.6-luna",                   # Mantle: not served by AgentCore Memory
    "made.up-model",
])
def test_a_context_outside_the_memory_models_fails_the_synth(tmp_path, saved):
    with pytest.raises(ValueError, match=KEY):
        _ids(tmp_path, context={KEY: saved})


@pytest.mark.parametrize("saved", ["us.amazon.nova-pro-v1:0", "global.anthropic.claude-sonnet-5-5"])
def test_a_workshop_synth_refuses_the_context(tmp_path, saved):
    with pytest.raises(ValueError, match="workshop"):
        _ids(tmp_path, context={KEY: saved, "with_workshop": "true"})


def test_a_workshop_synth_without_the_context_keeps_nova_pro(tmp_path):
    assert _ids(tmp_path, context={"with_workshop": "true"}) == ["us.amazon.nova-pro-v1:0"] * 5


# -- deploy.sh's allowlist question (stacks/_model_allowlist.py allows ... memory) ----

@pytest.mark.parametrize("raw, model_id, expected", [
    ("", "global.anthropic.claude-sonnet-5-5", True),
    ("", "us.amazon.nova-pro-v1:0", True),
    ("", "global.anthropic.claude-opus-5-5", False),   # not a memory model
    ("", "openai.gpt-5.6-luna", False),
    ("anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0",
     "global.anthropic.claude-haiku-4-5-20251001-v1:0", True),
    ("anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0",
     "global.anthropic.claude-haiku-5-5", False),
])
def test_allows_answers_for_the_memory_role(raw, model_id, expected):
    assert _model_allowlist.allows(raw, model_id, "memory") is expected


# -- one rule for a saved value: deploy.sh, the app's helper and the stack agree --------

_ALLOWLISTS = ["", "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0",
               "anthropic.claude-sonnet-4-6"]


@pytest.mark.parametrize("raw", _ALLOWLISTS)
def test_deploy_sh_rule_the_app_helper_and_the_stack_agree(raw):
    """``_model_allowlist.allows(..., "memory")`` (what deploy.sh asks before
    passing the saved value), ``model_catalog.effective_memory_model`` (what
    the app's GET and save use) and the stack's resolution of the context
    name the same model for every saved spelling, or all refuse it."""
    from stacks.agentcore_stack import memory_extraction_model
    catalog = _model_allowlist.load_model_catalog()
    allowed = catalog.parse_allowed_models(raw)
    values = [p + m.family for m in catalog.MODELS for p in ("", "global.", "us.", "eu.")]
    for value in values + ["", "made.up-model"]:
        for region in ("us-east-1", "eu-west-1", "ap-southeast-2"):
            effective = catalog.effective_memory_model(value, region, allowed)
            assert _model_allowlist.allows(raw, value, "memory") is bool(effective), value
            if effective:
                assert memory_extraction_model(region, saved=value) == effective, value
