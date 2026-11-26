# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The deployment model allowlist reaches every runtime, so a runtime's
catalog fallback never lands outside it.

The runtimes fall back to the catalog default model when a model env var is
unset: the onboarding runtime's map step (``onboard._map_model``) and the
orchestrator's wasted-step analysis (``runtime_app._invoke_analysis_model``).
The catalog default is allowlist-aware, but the runtimes never carried the
allowlist. While the catalog default and the workshop's models were the same
(Sonnet 5, Haiku 4.5) that did not show; with Claude 5.5 as the default it
would have moved the workshop's onboarding map step onto Haiku 5.5. The
contract now writes ``ALLOWED_MODELS`` onto every role when it is set, and
nowhere when it is not, so a deployment without an allowlist builds exactly
the env it built before.

Offline: pure contract calls and fake clients only.
"""
from __future__ import annotations

import pytest

from agents import runtime_env_contract as rec
from agents.runtime_env_contract import (
    ALLOWED_MODELS_KEY,
    OVERWRITE_WHEN_SOURCE_NONEMPTY,
    REQUIRED_KEYS,
    RuntimeEnvSources,
    build_env,
    known_keys,
    reconcile_env,
)

WORKSHOP_ALLOWLIST = "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0"
_SYSTEM_KW = dict(system_id="erp", system_name="ERP", schema="public",
                  connection_type="PostgreSQL", system_agent_model="m")


def _build(role: str, sources: RuntimeEnvSources) -> dict:
    return build_env(role, sources, **(_SYSTEM_KW if role == "system" else {}))


@pytest.mark.parametrize("role", rec.ROLES)
def test_no_allowlist_writes_no_key(role):
    for sources in (RuntimeEnvSources(), RuntimeEnvSources(allowed_models="  ")):
        assert ALLOWED_MODELS_KEY not in _build(role, sources)


@pytest.mark.parametrize("role", rec.ROLES)
def test_allowlist_is_written_on_every_role(role):
    env = _build(role, RuntimeEnvSources(allowed_models=f" {WORKSHOP_ALLOWLIST} "))
    assert env[ALLOWED_MODELS_KEY] == WORKSHOP_ALLOWLIST


@pytest.mark.parametrize("role", rec.ROLES)
def test_allowlist_reconciles_onto_existing_runtimes(role):
    """The refresh paths reconcile rather than rebuild: an existing runtime
    gets the allowlist on the next deploy, and an empty source leaves it."""
    assert REQUIRED_KEYS[role][ALLOWED_MODELS_KEY] == OVERWRITE_WHEN_SOURCE_NONEMPTY
    existing = {"SYSTEM_ID": "erp", "SYSTEM_REQUIRED_GROUP": "g"}
    out = reconcile_env(role, existing, RuntimeEnvSources(allowed_models=WORKSHOP_ALLOWLIST))
    assert out[ALLOWED_MODELS_KEY] == WORKSHOP_ALLOWLIST
    assert out["SYSTEM_REQUIRED_GROUP"] == "g"
    kept = reconcile_env(role, {ALLOWED_MODELS_KEY: "x"}, RuntimeEnvSources())
    assert kept[ALLOWED_MODELS_KEY] == "x"
    assert ALLOWED_MODELS_KEY not in reconcile_env(role, {}, RuntimeEnvSources())


@pytest.mark.parametrize("role", rec.ROLES)
def test_allowlist_is_a_known_key_never_foreign(role):
    assert ALLOWED_MODELS_KEY in known_keys(role)


def test_allowlist_is_not_required_at_boot():
    for role in rec.ROLES:
        assert ALLOWED_MODELS_KEY not in rec.STALE_CHECKED_KEYS[role]
        assert rec.missing_required(role, {}) == rec.missing_required(
            role, {ALLOWED_MODELS_KEY: WORKSHOP_ALLOWLIST})


def test_from_ambient_reads_the_allowlist_and_agent_model():
    sources = RuntimeEnvSources.from_ambient({
        "ALLOWED_MODELS": f" {WORKSHOP_ALLOWLIST} ",
        "SYSTEM_AGENT_MODEL": " us.anthropic.claude-haiku-4-5-20251001-v1:0 ",
    })
    assert sources.allowed_models == WORKSHOP_ALLOWLIST
    assert sources.system_agent_model == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    blank = RuntimeEnvSources.from_ambient({})
    assert blank.allowed_models == "" and blank.system_agent_model == ""


def test_models_to_warm_follows_the_configured_agent_model():
    pinned = RuntimeEnvSources(bedrock_model="global.anthropic.claude-sonnet-5",
                               system_agent_model="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert rec.models_to_warm(pinned) == [
        "global.anthropic.claude-sonnet-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"]
    assert rec.models_to_warm(RuntimeEnvSources()) == [
        "global.anthropic.claude-sonnet-5-5", "global.anthropic.claude-haiku-5-5"]


def test_shim_reconcile_carries_the_allowlist(monkeypatch, capsys):
    """deploy.sh §2f's reconcile shim reads the exported ALLOWED_MODELS."""
    import io
    import json
    monkeypatch.setenv("ALLOWED_MODELS", WORKSHOP_ALLOWLIST)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"SYSTEM_ID": "erp"})))
    assert rec._main(["--role", "system", "--reconcile"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[ALLOWED_MODELS_KEY] == WORKSHOP_ALLOWLIST


# --------------------------------------------------------------------------- #
# A redeploy moves an existing system runtime to the deploy's profile spelling
# --------------------------------------------------------------------------- #

_HAIKU_45 = "anthropic.claude-haiku-4-5-20251001-v1:0"


def test_reconcile_moves_the_same_agent_model_to_the_deploys_profile():
    """A workshop system runtime onboarded when the pin was the us. profile
    moves to the global pin on the next deploy: same model, new profile."""
    existing = {"SYSTEM_ID": "erp", "SYSTEM_AGENT_MODEL": f"us.{_HAIKU_45}"}
    out = reconcile_env("system", existing,
                        RuntimeEnvSources(system_agent_model=f"global.{_HAIKU_45}"))
    assert out["SYSTEM_AGENT_MODEL"] == f"global.{_HAIKU_45}"
    assert out["SYSTEM_ID"] == "erp"


def test_reconcile_keeps_a_live_swapped_agent_model():
    """A different model on the runtime is a live Admin choice (ADR-0055): a
    redeploy never undoes it, whatever the deploy's own SYSTEM_AGENT_MODEL."""
    existing = {"SYSTEM_AGENT_MODEL": "global.anthropic.claude-haiku-5-5"}
    out = reconcile_env("system", existing,
                        RuntimeEnvSources(system_agent_model=f"global.{_HAIKU_45}"))
    assert out["SYSTEM_AGENT_MODEL"] == "global.anthropic.claude-haiku-5-5"


@pytest.mark.parametrize("existing", [{}, {"SYSTEM_AGENT_MODEL": f"us.{_HAIKU_45}"}])
def test_reconcile_without_a_deploy_agent_model_changes_nothing(existing):
    """Base/demo deploys export no SYSTEM_AGENT_MODEL: the runtime keeps what
    it has, and an absent key stays absent (provisioning owns it)."""
    out = reconcile_env("system", existing, RuntimeEnvSources())
    assert out.get("SYSTEM_AGENT_MODEL") == existing.get("SYSTEM_AGENT_MODEL")
    assert ("SYSTEM_AGENT_MODEL" in out) == ("SYSTEM_AGENT_MODEL" in existing)


def test_shim_reconcile_moves_a_workshop_system_runtime_to_the_global_pin(monkeypatch, capsys):
    """The exact deploy.sh §2f path: --workshop exports the pin, the shim
    reconciles the runtime's existing env."""
    import io
    import json
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", f"global.{_HAIKU_45}")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"SYSTEM_ID": "erp", "SYSTEM_AGENT_MODEL": f"us.{_HAIKU_45}"})))
    assert rec._main(["--role", "system", "--reconcile"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["SYSTEM_AGENT_MODEL"] == f"global.{_HAIKU_45}"


def test_contract_profile_prefixes_match_the_catalog():
    """The contract is stdlib-only and import-light, so it keeps its own copy
    of the prefix list; it must be the catalog's."""
    from agents import model_catalog
    assert rec.PROFILE_PREFIXES == model_catalog.PROFILE_PREFIXES


# --------------------------------------------------------------------------- #
# The fallbacks the workshop runtimes actually take
# --------------------------------------------------------------------------- #

def _workshop_runtime_env(monkeypatch, role: str) -> None:
    """Make this process look like a workshop runtime: exactly the env the
    contract builds for *role* from the workshop's pins."""
    env = _build(role, RuntimeEnvSources(
        bedrock_model="global.anthropic.claude-sonnet-5",
        allowed_models=WORKSHOP_ALLOWLIST, workshop=True))
    for key in ("BEDROCK_MODEL", "SYSTEM_AGENT_MODEL", "MAP_MODEL", "ALLOWED_MODELS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if role != "system":
        monkeypatch.delenv("SYSTEM_AGENT_MODEL", raising=False)


def test_workshop_onboarding_runtime_maps_on_haiku_4_5(monkeypatch):
    import agents.onboard as onboard
    _workshop_runtime_env(monkeypatch, "onboarding")
    assert onboard._map_model() == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert onboard._escalation_model() == "global.anthropic.claude-sonnet-5"


def test_base_onboarding_runtime_maps_on_the_catalog_default(monkeypatch):
    import agents.onboard as onboard
    for key in ("MAP_MODEL", "ALLOWED_MODELS", "BEDROCK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    assert onboard._map_model() == "global.anthropic.claude-haiku-5-5"
    assert onboard._escalation_model() == "global.anthropic.claude-sonnet-5-5"


def test_workshop_orchestrator_runtime_analysis_runs_haiku_4_5(monkeypatch):
    import agents.aws_session as aws_session
    import agents.runtime_app as runtime_app

    class _Fake:
        kwargs = None

        def converse(self, **kwargs):
            _Fake.kwargs = kwargs
            return {"output": {"message": {"content": [{"text": "{}"}]}}}

    _workshop_runtime_env(monkeypatch, "orchestrator")
    monkeypatch.setattr(aws_session, "bedrock_runtime_client", lambda *a, **k: _Fake())
    runtime_app._invoke_analysis_model("PROMPT")
    assert _Fake.kwargs["modelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
