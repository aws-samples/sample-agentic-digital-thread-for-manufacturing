# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Both provisioning paths give a new system runtime the saved agent model.

The in-app path (``AgentManager._system_agent_model``) runs the model saved in
Admin > Settings (SSM ``/adt/llm-agent-model``), else ``SYSTEM_AGENT_MODEL``,
else the catalog default (``model_config.configured_agent_model``). The
provisioner handlers' ``CreateRuntimeInput`` (``scripts/provision_runtimes.py``;
first written for the Step Functions provisioner, removed 2026-10-29) built
its input with the catalog default only, so the two disagreed as soon as a
model was saved. Now ``CreateRuntimeInput`` resolves the same way and an
explicit model in the input still wins. Offline: SSM is a fake.
"""
from __future__ import annotations

import pytest

SAVED = "us.anthropic.claude-sonnet-4-6"


@pytest.fixture
def saved_model(monkeypatch):
    import agents.model_config as mc
    monkeypatch.setattr(mc, "_ssm_value", lambda name, region: SAVED
                        if name == "/adt/llm-agent-model" else "")
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)


def test_the_sfn_create_input_uses_the_saved_agent_model(saved_model):
    from agents.provisioner.models import CreateRuntimeInput
    inp = CreateRuntimeInput.from_dict({"system_id": "sys-a", "region": "us-east-1"})
    assert inp.system_agent_model == SAVED


def test_both_provisioning_paths_agree(saved_model, monkeypatch):
    from agents.agent_manager import AgentManager
    from agents.provisioner.models import CreateRuntimeInput
    for env in ({}, {"SYSTEM_AGENT_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sfn = CreateRuntimeInput.from_dict({"system_id": "sys-a", "region": "us-east-1"})
        assert sfn.system_agent_model == AgentManager._system_agent_model({}, "us-east-1")


def test_guard_an_explicit_model_in_the_input_still_wins(saved_model):
    from agents.provisioner.models import CreateRuntimeInput
    inp = CreateRuntimeInput.from_dict({"system_id": "sys-a", "system_agent_model": "pinned"})
    assert inp.system_agent_model == "pinned"
