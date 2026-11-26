# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ``allowed_models`` CDK context -> the app container's ``ALLOWED_MODELS``
(stacks/_model_allowlist.py). Offline and synth-free: the helper is pure.

Run:  PYTHONPATH=. python -m pytest tests/test_model_allowlist_context.py -v
"""
from __future__ import annotations

import pytest

from stacks import _model_allowlist as ma

SONNET_5 = "anthropic.claude-sonnet-5"
HAIKU = "anthropic.claude-haiku-4-5-20251001-v1:0"
CONFIGURED = {"BEDROCK_MODEL": f"us.{SONNET_5}", "SYSTEM_AGENT_MODEL": f"us.{HAIKU}"}


@pytest.mark.parametrize("raw", [None, "", "  ", []])
def test_unset_adds_nothing(raw):
    assert ma.allowed_models_env(raw, CONFIGURED) == {}


def test_set_emits_sorted_families():
    env = ma.allowed_models_env(f"us.{SONNET_5}, {HAIKU}", CONFIGURED)
    assert env == {"ALLOWED_MODELS": f"{HAIKU},{SONNET_5}"}


def test_cdk_json_list_form_is_accepted():
    assert ma.allowed_models_env([SONNET_5, HAIKU], CONFIGURED) == {
        "ALLOWED_MODELS": f"{HAIKU},{SONNET_5}"}


def test_unknown_id_fails_the_synth():
    with pytest.raises(ValueError, match="anthropic.claude-sonet-5"):
        ma.allowed_models_env(f"{HAIKU},anthropic.claude-sonet-5", CONFIGURED)


def test_excluding_the_deployments_own_model_fails_the_synth():
    with pytest.raises(ValueError) as err:
        ma.allowed_models_env(SONNET_5, CONFIGURED)
    assert "SYSTEM_AGENT_MODEL" in str(err.value)
    assert "BEDROCK_MODEL" not in str(err.value)


def test_configured_models_in_the_wrong_role_fail_the_synth():
    """The allowlist offers each role its own models, so a deployment that
    configures the pair the other way round would run models its Admin page
    refuses."""
    with pytest.raises(ValueError) as err:
        ma.allowed_models_env(f"{SONNET_5},{HAIKU}", {
            "BEDROCK_MODEL": f"global.{HAIKU}", "SYSTEM_AGENT_MODEL": f"global.{SONNET_5}"})
    assert "BEDROCK_MODEL" in str(err.value) and "SYSTEM_AGENT_MODEL" in str(err.value)


def test_allows_checks_the_role():
    raw = f"{SONNET_5},{HAIKU}"
    assert ma.allows(raw, f"global.{SONNET_5}", "orchestrator")
    assert not ma.allows(raw, f"global.{HAIKU}", "orchestrator")
    assert ma.allows(raw, f"global.{HAIKU}", "agent")
    assert ma.allows("", f"global.{HAIKU}", "orchestrator")


def test_uses_the_apps_own_parser():
    """One rule for synth and boot: the helper loads the app's catalog."""
    catalog = ma.load_model_catalog()
    assert catalog.ALLOWED_MODELS_ENV == "ALLOWED_MODELS"
    assert ma.MODEL_CATALOG.endswith("agentic_digital_thread/agents/model_catalog.py")
