# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A runtime provisioned AFTER an Admin > Settings agent-model save gets the
saved model.

The save writes ``/adt/llm-agent-model`` and bumps the RUNNING runtimes in
place, and Settings (``model_config.get_configured_models``) reports the saved
value as the agent model. Provisioning used to ignore it: ``SYSTEM_AGENT_MODEL``
for a new, re-onboarded or recreated runtime came from the app task's own env
(the deploy-time model), so a system onboarded after the save ran the old model
while the graph node and Settings said otherwise, and the save's own "re-onboard
<id> for the new agent model" advice could not deliver it.

Resolution order for a provisioned runtime: per-system ``system_model`` ->
saved model -> app env ``SYSTEM_AGENT_MODEL`` -> catalog default. Offline: the
SSM read is the ``model_config._ssm_value`` seam, patched in every test.
"""
from __future__ import annotations

from agents import model_config
from agents.agent_manager import AgentManager

SAVED = "us.anthropic.claude-sonnet-5"
DEPLOY_ENV = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-east-1"


def _ssm(saved_agent: str = "", saved_orchestrator: str = ""):
    """Fake ``_ssm_value``: the two model parameters, everything else unset."""
    values = {model_config._SSM_AGENT: saved_agent,
              model_config._SSM_ORCHESTRATOR: saved_orchestrator}
    calls: list[tuple[str, str]] = []

    def _read(name: str, region: str) -> str:
        calls.append((name, region))
        return values.get(name, "")
    return _read, calls


def _env(cfg=None) -> dict:
    return AgentManager(mode="cloud")._base_env_vars(
        "sys1", "Example Corp", "public", "PostgreSQL", REGION, cfg or {})


def test_provision_uses_the_saved_agent_model(monkeypatch):
    read, calls = _ssm(saved_agent=SAVED)
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    assert _env()["SYSTEM_AGENT_MODEL"] == SAVED
    # Only the agent parameter is read, in the provisioning Region.
    assert calls == [(model_config._SSM_AGENT, REGION)]


def test_provision_matches_what_settings_reports(monkeypatch):
    read, _ = _ssm(saved_agent=SAVED, saved_orchestrator="us.anthropic.claude-sonnet-4-6")
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    reported = model_config.get_configured_models(REGION)["agent"]
    assert _env()["SYSTEM_AGENT_MODEL"] == reported == SAVED


def test_per_system_override_beats_the_saved_model(monkeypatch):
    read, calls = _ssm(saved_agent=SAVED)
    monkeypatch.setattr(model_config, "_ssm_value", read)
    cfg = {"system_model": "  us.anthropic.claude-sonnet-4-6  "}
    assert _env(cfg)["SYSTEM_AGENT_MODEL"] == "us.anthropic.claude-sonnet-4-6"
    assert calls == []  # an override needs no SSM read


def test_no_saved_model_keeps_the_deploy_env_model(monkeypatch):
    read, _ = _ssm()  # unset, or unreadable (``_ssm_value`` returns "")
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    assert _env()["SYSTEM_AGENT_MODEL"] == DEPLOY_ENV


def test_no_saved_model_and_no_env_uses_the_catalog_default(monkeypatch):
    read, _ = _ssm()
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.delenv("SYSTEM_AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    from agents.model_catalog import AGENT, default_model
    assert _env()["SYSTEM_AGENT_MODEL"] == default_model(AGENT, REGION)


def test_ssm_failure_is_the_old_behaviour_not_an_error(monkeypatch):
    """The real seam swallows every SSM error and returns "" (local dev,
    missing permission): provisioning then falls back to the env model exactly
    as before this change, and never fails because SSM is unreachable."""
    import boto3

    def _boom(*_a, **_k):
        raise RuntimeError("no SSM here")
    monkeypatch.setattr(boto3, "client", _boom)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    assert _env()["SYSTEM_AGENT_MODEL"] == DEPLOY_ENV


def test_saved_model_outside_the_allowlist_is_not_provisioned(monkeypatch, caplog):
    """A model saved before a later deploy tightened ALLOWED_MODELS: the save
    route would refuse it now, so provisioning ignores it (and says so) and
    Settings reports the model a new runtime actually gets."""
    read, _ = _ssm(saved_agent="us.amazon.nova-pro-v1:0")
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("ALLOWED_MODELS",
                       "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    with caplog.at_level("WARNING", logger="agents.model_config"):
        assert _env()["SYSTEM_AGENT_MODEL"] == DEPLOY_ENV
    assert model_config.get_configured_models(REGION)["agent"] == DEPLOY_ENV
    assert any("not offered for that role by this deployment's model allowlist" in r.getMessage()
               for r in caplog.records)


def test_saved_model_inside_the_allowlist_is_provisioned(monkeypatch):
    saved_haiku_55 = "global.anthropic.claude-haiku-5-5"
    read, _ = _ssm(saved_agent=saved_haiku_55)
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("ALLOWED_MODELS",
                       "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0,"
                       "anthropic.claude-haiku-5-5")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    assert _env()["SYSTEM_AGENT_MODEL"] == saved_haiku_55


def test_saved_orchestrator_model_in_the_agent_slot_is_not_provisioned(monkeypatch):
    """Sonnet 5 is allowed, but as the orchestrator: under the workshop
    allowlist a new system runtime gets Haiku 4.5, never the swapped pair."""
    read, _ = _ssm(saved_agent=SAVED)
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("ALLOWED_MODELS",
                       "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    assert _env()["SYSTEM_AGENT_MODEL"] == DEPLOY_ENV


def test_the_ssm_read_is_bounded(monkeypatch):
    """Short timeouts and one attempt: a slow SSM costs seconds on the
    provisioning path, never the SDK defaults with retries."""
    import boto3
    seen: dict = {}

    class _Client:
        def get_parameter(self, Name):
            return {"Parameter": {"Value": SAVED}}

    def _client(service, region_name=None, config=None):
        seen.update(service=service, region=region_name, config=config)
        return _Client()
    monkeypatch.setattr(boto3, "client", _client)
    assert model_config._ssm_value(model_config._SSM_AGENT, REGION) == SAVED
    cfg = seen["config"]
    assert seen["service"] == "ssm" and seen["region"] == REGION
    assert cfg.connect_timeout == 2 and cfg.read_timeout == 2
    assert cfg.retries == {"max_attempts": 1}


def test_the_provisioning_env_carries_the_saved_model(monkeypatch):
    """Through the production builder ``_start_cloud`` uses for both a new
    runtime (create) and a reused one (image refresh update)."""
    read, _ = _ssm(saved_agent=SAVED)
    monkeypatch.setattr(model_config, "_ssm_value", read)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", DEPLOY_ENV)
    mgr = AgentManager(mode="cloud")
    monkeypatch.setattr(mgr, "_upsert_agent_config_secret",
                        lambda *_a, **_k: "arn:aws:secretsmanager:us-east-1:111122223333:secret:x")
    env = mgr._prepare_runtime_env(
        "sys1", "Example Corp", "public", "prompt", "tables", "PostgreSQL",
        {"host": "db"}, None, REGION)
    assert env["SYSTEM_AGENT_MODEL"] == SAVED
