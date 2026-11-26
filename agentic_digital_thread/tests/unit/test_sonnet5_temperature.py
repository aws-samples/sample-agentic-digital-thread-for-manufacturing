# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Claude Sonnet 5 rejects a sampling ``temperature``.

Live (2026-10-01, us-east-1): ``converse`` with ``temperature`` returned
ValidationException "`temperature` is deprecated for this model", and the
same for an InvokeModel Anthropic body. The app pins temperature in two
places (``db._converse_request`` for onboarding's structured calls and
``runtime_app._invoke_analysis_model``), so a deployment that pins its
orchestrator model to Sonnet 5 (the workshop package) failed those calls.
The catalog now says which models reject it; every other model keeps the
exact request it had. The onboarding map escalation also follows the
deployment's configured model instead of the catalog default, so a pinned
deployment never escalates onto a model it does not use.

Offline: fake clients only.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents import model_catalog
from agents.model_catalog import ORCHESTRATOR, accepts_temperature, default_model

SONNET_5 = "us.anthropic.claude-sonnet-5"
SONNET_46 = "us.anthropic.claude-sonnet-4-6"


def test_catalog_marks_only_sonnet_5_as_rejecting_temperature():
    assert accepts_temperature(SONNET_5) is False
    assert accepts_temperature("anthropic.claude-sonnet-5") is False
    assert accepts_temperature("eu.anthropic.claude-sonnet-5") is False
    assert accepts_temperature(SONNET_46) is True
    assert accepts_temperature("us.anthropic.claude-haiku-4-5-20251001-v1:0") is True
    # Ids outside the catalog keep the old behavior.
    assert accepts_temperature("us.anthropic.claude-sonnet-4-20250514-v1:0") is True
    assert accepts_temperature("") is True
    # The Claude 5 generation rejects it: Sonnet 5 (2026-10-01) and the three
    # Claude 5.5 models (live Converse probe, us-east-1, 2026-10-21).
    rejecting = {m.family for m in model_catalog.MODELS if not m.temperature}
    assert rejecting == {"anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5-5",
                         "anthropic.claude-opus-5-5", "anthropic.claude-haiku-5-5"}
    for model_id in ("global.anthropic.claude-sonnet-5-5", "us.anthropic.claude-opus-5-5",
                     "global.anthropic.claude-haiku-5-5"):
        assert accepts_temperature(model_id) is False, model_id


def test_anthropic_body_omits_temperature_for_sonnet_5():
    import agents.db as db
    req = db._converse_request("PROMPT", SONNET_5)
    assert "temperature" not in req["inferenceConfig"]
    assert req["inferenceConfig"]["maxTokens"] == 8192
    assert req["messages"] == [{"role": "user", "content": [{"text": "PROMPT"}]}]


def test_anthropic_request_keeps_greedy_decoding_for_models_that_accept_it():
    import agents.db as db
    req = db._converse_request("PROMPT", SONNET_46)
    assert req["inferenceConfig"] == {"maxTokens": 8192, "temperature": 0.0}
    assert req["modelId"] == SONNET_46


def test_nova_request_is_the_same_converse_shape():
    import agents.db as db
    req = db._converse_request("P", "us.amazon.nova-pro-v1:0")
    assert req["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.0}
    assert req["messages"] == [{"role": "user", "content": [{"text": "P"}]}]


class _FakeConverse:
    def __init__(self):
        self.kwargs = None

    def converse(self, **kwargs):
        self.kwargs = kwargs
        return {"output": {"message": {"content": [{"text": "{}"}]}}}


def _analysis_inference(monkeypatch, agent_model):
    import agents.aws_session as aws_session
    import agents.runtime_app as runtime_app
    fake = _FakeConverse()
    monkeypatch.setattr(aws_session, "bedrock_runtime_client", lambda *a, **k: fake)
    if agent_model is None:
        monkeypatch.delenv("SYSTEM_AGENT_MODEL", raising=False)
    else:
        monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_model)
    runtime_app._invoke_analysis_model("PROMPT")
    return fake.kwargs


def test_analysis_call_drops_temperature_for_sonnet_5(monkeypatch):
    kwargs = _analysis_inference(monkeypatch, SONNET_5)
    assert kwargs["modelId"] == SONNET_5
    assert kwargs["inferenceConfig"] == {"maxTokens": 1500}


def test_analysis_call_drops_temperature_for_the_default_agent_model(monkeypatch):
    """The default agent model is Claude Haiku 5.5, which rejects a
    temperature like the rest of the Claude 5 generation."""
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    kwargs = _analysis_inference(monkeypatch, None)
    assert kwargs["modelId"] == "global.anthropic.claude-haiku-5-5"
    assert kwargs["inferenceConfig"] == {"maxTokens": 1500}


def test_analysis_call_keeps_temperature_for_haiku_4_5(monkeypatch):
    """Haiku 4.5 (the workshop's agent model) keeps its exact request."""
    kwargs = _analysis_inference(monkeypatch, "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert kwargs["inferenceConfig"] == {"maxTokens": 1500, "temperature": 0.1}


def test_analysis_call_under_the_workshop_allowlist_uses_haiku_4_5(monkeypatch):
    """A runtime without SYSTEM_AGENT_MODEL falls back to the catalog default;
    under the workshop's ALLOWED_MODELS (written onto its runtimes) that is
    Haiku 4.5, not the newer catalog default."""
    monkeypatch.setenv("ALLOWED_MODELS",
                       "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0")
    kwargs = _analysis_inference(monkeypatch, None)
    assert kwargs["modelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert kwargs["inferenceConfig"] == {"maxTokens": 1500, "temperature": 0.1}


def test_escalation_model_follows_the_configured_orchestrator_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL", SONNET_5)
    assert onboard._escalation_model() == SONNET_5
    monkeypatch.setenv("BEDROCK_MODEL", "  ")
    assert onboard._escalation_model() == default_model(ORCHESTRATOR)
    monkeypatch.delenv("BEDROCK_MODEL")
    assert onboard._escalation_model() == default_model(ORCHESTRATOR)


def test_weak_batch_escalates_on_the_configured_model(monkeypatch):
    calls: list = []

    def _fake(prompt, model=None, timeout=90):
        calls.append(model)
        return "[]"

    monkeypatch.setenv("BEDROCK_MODEL", SONNET_5)
    monkeypatch.setattr(onboard, "bedrock_call", _fake)
    batch = [{"name": "t", "columns": [{"name": "work_order_id"}]}]
    weak = [{"field": "t.work_order_id", "mappings": []}]
    _out, _adopted, escalated = onboard._maybe_escalate_batch(
        weak, batch, "PROMPT", job_id="j", batch_num=1, total_batches=1,
        system_id="sys", budget_left=1)
    assert escalated is True
    assert calls == [SONNET_5]


class _RecordingConverse:
    def __init__(self):
        self.model_ids = []

    def converse(self, **kwargs):
        self.model_ids.append(kwargs["modelId"])
        return {"output": {"message": {"content": [{"text": "ok"}]}}}


@pytest.mark.parametrize("region, agent", [
    ("us-east-1", "us.amazon.nova-pro-v1:0"),
    ("eu-west-1", "eu.amazon.nova-pro-v1:0"),
    ("ap-southeast-2", "apac.amazon.nova-pro-v1:0"),
])
def test_bedrock_call_first_try_is_the_catalog_profile_for_the_region(monkeypatch, region, agent):
    """The first try is the configured system-agent model as the catalog
    resolves it. With no model env and an allowlist whose only agent model is
    Nova Pro (no global profile), that is Nova Pro's geo profile for the
    Region (apac. in Asia Pacific; a hand-built us. id cannot be called from
    there)."""
    import agents.aws_session as aws_session
    import agents.db as db
    import agents.model_config as model_config
    fake = _RecordingConverse()
    monkeypatch.setattr(aws_session, "bedrock_runtime_client", lambda *a, **k: fake)
    monkeypatch.setattr(model_config, "_ssm_value", lambda name, region: "")
    for key in ("SYSTEM_AGENT_MODEL", "AGENT_MODEL", "BEDROCK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALLOWED_MODELS", "anthropic.claude-sonnet-5-5,amazon.nova-pro-v1:0")
    monkeypatch.setenv("AWS_REGION", region)
    assert db.bedrock_call("P") == "ok"
    assert fake.model_ids == [agent]
