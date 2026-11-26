# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The AgentCore Memory extraction model: Haiku 5.5, except on the workshop.

The memory's custom strategies need an explicit extraction/consolidation
model id. Base and demo deploys use Claude Haiku 5.5, whose global inference
profile is the same id in every Region. A workshop synth (``with_workshop``,
set by ``deploy.sh --workshop`` and ``package.py --profile workshop``) keeps
Amazon Nova Pro, the workshop's pin in ``stacks/_workshop_models.py``. Nova
Pro has no global profile, so its id carries the deploy Region's geo prefix
(``us.``/``eu.``/``apac.``) from the model catalog, exactly as before: the
workshop's templates do not change.

The Region comes from the stack's concrete env (deploy.sh), or, for the
account-agnostic synth (the workshop package, the root suite), from the
``region`` context the synth was given. ``MEMORY_EXTRACTION_MODEL`` set at
synth time still wins on every profile.

Built in process with ``assertions.Template`` (offline, no CDK CLI).
"""
from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from stacks.agentcore_stack import AgentCoreStack

ACCOUNT = "111122223333"   # AWS documentation placeholder
BASE_ID = "global.anthropic.claude-haiku-5-5"
WORKSHOP_US_ID = "us.amazon.nova-pro-v1:0"
MEMORY = "AWS::BedrockAgentCore::Memory"
WORKSHOP = {"with_workshop": "true"}


def _model_ids(value) -> list:
    """Every ``ModelId`` value under *value*, in document order."""
    if isinstance(value, dict):
        found = [v for k, v in value.items() if k == "ModelId"]
        for k, v in value.items():
            if k != "ModelId":
                found += _model_ids(v)
        return found
    if isinstance(value, list):
        return [m for item in value for m in _model_ids(item)]
    return []


def _memory_properties(tmp_path, *, region: str | None = None,
                       context: dict | None = None) -> dict:
    app = cdk.App(context=context or {}, outdir=str(tmp_path))
    env = cdk.Environment(account=ACCOUNT, region=region) if region else None
    stack = AgentCoreStack(app, "ADTAgentCoreStack", env=env)
    # Without an env the stack is account-agnostic: its Region is a token.
    assert cdk.Token.is_unresolved(stack.region) == (region is None)
    memories = assertions.Template.from_stack(stack).find_resources(MEMORY)
    assert len(memories) == 1, memories.keys()
    return next(iter(memories.values()))["Properties"]


def _extraction_models(tmp_path, *, region: str | None = None,
                       context: dict | None = None) -> list:
    ids = _model_ids(_memory_properties(tmp_path, region=region, context=context))
    # facts (extraction + consolidation), summaries (consolidation),
    # preferences (extraction + consolidation)
    assert len(ids) == 5, ids
    return ids


@pytest.fixture(autouse=True)
def _no_synth_override(monkeypatch):
    monkeypatch.delenv("MEMORY_EXTRACTION_MODEL", raising=False)


# --- base and demo deploys: Claude Haiku 5.5, one id in every Region ---------

@pytest.mark.parametrize("region", [
    "us-east-1", "us-west-2", "eu-west-1", "eu-central-1",
    "ap-southeast-2", "ap-northeast-1",
])
def test_base_deploy_uses_haiku_5_5_global_profile(tmp_path, region):
    assert _extraction_models(tmp_path, region=region) == [BASE_ID] * 5


@pytest.mark.parametrize("synth_context", [
    {"region": "us-east-1"},     # the engine package's default
    {"region": "eu-west-1"},
    {"region": "ap-southeast-2"},
    {},                          # no Region at all
])
def test_base_account_agnostic_synth_uses_haiku_5_5(tmp_path, synth_context):
    """The engine package (``package.py --profile engine``) is a base deploy:
    a plain global id, no Region-derived literal."""
    assert _extraction_models(tmp_path, context=synth_context) == [BASE_ID] * 5


@pytest.mark.parametrize("flag", ["false", "False", "", "no"])
def test_anything_but_true_is_not_a_workshop(tmp_path, flag):
    """Same reading as app.py and the other stacks: only ``true`` (any case)
    is a workshop synth."""
    ids = _extraction_models(tmp_path, region="us-east-1",
                             context={"with_workshop": flag})
    assert ids == [BASE_ID] * 5


# --- the workshop: Nova Pro, byte-identical to before ------------------------

@pytest.mark.parametrize("region, expected", [
    ("us-east-1", WORKSHOP_US_ID),
    ("us-west-2", WORKSHOP_US_ID),
    ("eu-west-1", "eu.amazon.nova-pro-v1:0"),
    ("eu-central-1", "eu.amazon.nova-pro-v1:0"),
    ("ap-southeast-2", "apac.amazon.nova-pro-v1:0"),
    ("ap-northeast-1", "apac.amazon.nova-pro-v1:0"),
])
def test_workshop_concrete_region_keeps_nova_pro_geo_profile(tmp_path, region, expected):
    """``deploy.sh --workshop`` passes ``-c with_workshop=true``."""
    assert _extraction_models(tmp_path, region=region, context=WORKSHOP) == [expected] * 5


@pytest.mark.parametrize("synth_context, expected", [
    ({**WORKSHOP, "region": "us-east-1"}, WORKSHOP_US_ID),   # package.py default
    ({**WORKSHOP, "region": "eu-west-1"}, "eu.amazon.nova-pro-v1:0"),
    ({**WORKSHOP, "region": "ap-southeast-2"}, "apac.amazon.nova-pro-v1:0"),
    ({"with_workshop": "True"}, WORKSHOP_US_ID),             # no Region: unchanged id
])
def test_workshop_account_agnostic_synth_keeps_nova_pro(tmp_path, synth_context, expected):
    """``package.py --profile workshop`` synths account-agnostic with
    ``with_workshop=true``: the stack Region is the ``AWS::Region`` token, so
    the Nova Pro id is resolved for the Region the synth was given and stays a
    plain string (no mapping or intrinsic in the template)."""
    assert _extraction_models(tmp_path, context=synth_context) == [expected] * 5


def test_workshop_and_base_memory_differ_only_in_model_ids(tmp_path):
    """The two profiles build the same memory resource apart from the model
    ids, so the profile switch touches nothing else. This does not prove the
    workshop template is unchanged from before the switch: that was checked
    by synthesising both revisions and comparing the templates."""
    base = _memory_properties(tmp_path / "base", region="us-east-1")
    workshop = _memory_properties(tmp_path / "ws", region="us-east-1", context=WORKSHOP)

    def strip(value):
        if isinstance(value, dict):
            return {k: ("<model>" if k == "ModelId" else strip(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    assert strip(base) == strip(workshop)


def test_workshop_pin_lives_in_the_workshop_models_module():
    """One home for the workshop's model pins: the stack reads its memory
    family from stacks/_workshop_models.py rather than its own literal."""
    from stacks import _workshop_models
    from stacks.agentcore_stack import memory_extraction_family

    assert _workshop_models.WORKSHOP_MEMORY_EXTRACTION_FAMILY == "amazon.nova-pro-v1:0"
    assert memory_extraction_family(with_workshop=True) == \
        _workshop_models.WORKSHOP_MEMORY_EXTRACTION_FAMILY
    assert memory_extraction_family(with_workshop=False) == "anthropic.claude-haiku-5-5"


def test_workshop_memory_pin_is_not_an_admin_selectable_model():
    """The workshop's allowlist stays its two Admin-page models: its memory
    extraction model is not added to it (the page shows it read-only)."""
    from stacks import _workshop_models

    assert _workshop_models.WORKSHOP_ALLOWED_MODELS == (
        "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0")


# --- the synth-time override wins everywhere ---------------------------------

@pytest.mark.parametrize("context", [{}, WORKSHOP])
def test_synth_time_env_override_wins(tmp_path, monkeypatch, context):
    monkeypatch.setenv("MEMORY_EXTRACTION_MODEL", "eu.amazon.nova-lite-v1:0")
    ids = _extraction_models(tmp_path, region="ap-southeast-2", context=context)
    assert ids == ["eu.amazon.nova-lite-v1:0"] * 5


@pytest.mark.parametrize("context, expected", [
    ({}, BASE_ID),
    (WORKSHOP, "eu.amazon.nova-pro-v1:0"),
])
def test_blank_env_override_is_ignored(tmp_path, monkeypatch, context, expected):
    monkeypatch.setenv("MEMORY_EXTRACTION_MODEL", "  ")
    assert _extraction_models(tmp_path, region="eu-west-1", context=context) == [expected] * 5


def test_both_families_are_catalog_models():
    """Each family names a catalog model (a typo would otherwise resolve to a
    profile that does not exist). Haiku 5.5 has a global profile, which is why
    the base id is the same everywhere; Nova Pro has none, which is why the
    workshop id needs the geo prefix."""
    from stacks import _model_allowlist
    from stacks.agentcore_stack import memory_extraction_family

    catalog = _model_allowlist.load_model_catalog()
    by_family = {m.family: m for m in catalog.MODELS}
    base = by_family.get(memory_extraction_family(with_workshop=False))
    workshop = by_family.get(memory_extraction_family(with_workshop=True))
    assert base is not None and workshop is not None
    assert base.global_profile
    assert not workshop.global_profile
