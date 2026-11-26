# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provider-factory routing (ADR-0055).

The load-bearing facts these pin, all verified live 2026-07-28:
* GPT-5.x is Mantle-only and served at ``/openai/v1`` — the generic ``/v1``
  path answers "does not support the '/v1/responses' API", which reads like an
  entitlement error but is a WRONG PATH.
* Mantle ids must NOT be region-prefixed.
* Every existing Converse id must pass through UNCHANGED, so today's behavior
  is byte-identical.
"""
from __future__ import annotations

import pytest

from agents.model_provider import (is_mantle_only, resolve_model,
                                   strip_region_prefix)


class TestClassification:
    @pytest.mark.parametrize("model_id", [
        "openai.gpt-5.6-luna", "openai.gpt-5.6-sol", "openai.gpt-5.6-terra",
        "us.openai.gpt-5.6-luna",  # a stray prefix must still classify
        "openai.gpt-5.5",
    ])
    def test_gpt5_family_is_mantle_only(self, model_id):
        assert is_mantle_only(model_id) is True

    @pytest.mark.parametrize("model_id", [
        "global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-4-6",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.amazon.nova-2-lite-v1:0",
        # gpt-oss answers on bedrock-runtime Converse (verified live), so it
        # deliberately rides the plain path.
        "openai.gpt-oss-20b", "us.openai.gpt-oss-120b",
        "", None,
    ])
    def test_converse_models_are_not_mantle_only(self, model_id):
        assert is_mantle_only(model_id) is False

    def test_strip_region_prefix(self):
        assert strip_region_prefix("us.openai.gpt-5.6-luna") == "openai.gpt-5.6-luna"
        assert strip_region_prefix("global.anthropic.claude-sonnet-5") == "anthropic.claude-sonnet-5"
        assert strip_region_prefix("openai.gpt-5.6-luna") == "openai.gpt-5.6-luna"


class TestResolveModel:
    @pytest.mark.parametrize("model_id", [
        "global.anthropic.claude-sonnet-5", "us.amazon.nova-pro-v1:0",
        "openai.gpt-oss-20b",
    ])
    def test_converse_ids_pass_through_unchanged(self, model_id):
        """BYTE-IDENTICAL behavior for every pre-Mantle model: the id string is
        returned as-is so strands builds the same implicit BedrockModel."""
        assert resolve_model(model_id) is model_id

    def test_mantle_id_builds_provider_with_openai_v1_base_url(self, monkeypatch):
        captured = {}

        class FakeModel:
            def __init__(self, model_id=None, client_args=None):
                captured["model_id"] = model_id
                captured["client_args"] = client_args

        import agents.model_provider as mp
        monkeypatch.setattr(mp, "_mantle_model",
                            lambda bare, region, mt=None: FakeModel(
                                model_id=bare,
                                client_args={"base_url": mp._MANTLE_BASE_URL.format(region=region)}))
        out = mp.resolve_model("us.openai.gpt-5.6-luna", region="us-west-2")
        assert isinstance(out, FakeModel)
        # region prefix stripped, and the /openai/ prefix is present — the
        # difference between a 200 and a 400 from Mantle.
        assert captured["model_id"] == "openai.gpt-5.6-luna"
        assert captured["client_args"]["base_url"] == (
            "https://bedrock-mantle.us-west-2.api.aws/openai/v1")

    def test_missing_dependency_fails_loud_never_substitutes(self, monkeypatch):
        """A Mantle model that cannot be built RAISES — silently falling back to
        another model would misreport which model produced an answer and
        corrupt every A/B measurement."""
        import builtins
        real_import = builtins.__import__

        def no_openai(name, *a, **k):
            if name.startswith("strands.models.openai_responses") or name == "aws_bedrock_token_generator":
                raise ImportError("no openai extra")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_openai)
        with pytest.raises(RuntimeError, match="Bedrock Mantle dependencies"):
            resolve_model("openai.gpt-5.6-luna", region="us-east-1")


class TestCatalogIntegration:
    def test_mantle_ids_are_never_region_prefixed(self):
        from agents.model_catalog import resolve_id
        assert resolve_id("openai.gpt-5.6-luna") == "openai.gpt-5.6-luna"
        assert resolve_id("openai.gpt-5.6-luna", "eu-west-1") == "openai.gpt-5.6-luna"
        assert resolve_id("anthropic.claude-sonnet-5", "eu-west-1") == "global.anthropic.claude-sonnet-5"

    def test_mantle_models_are_marked_and_priced_at_standard_rates(self):
        from agents.model_catalog import MODELS
        by_family = {m.family: m for m in MODELS}
        luna = by_family["openai.gpt-5.6-luna"]
        assert luna.mantle is True and luna.role == "agent"
        assert (luna.input_per_1k, luna.output_per_1k) == (0.001, 0.006)
        sol = by_family["openai.gpt-5.6-sol"]
        assert sol.mantle is True and (sol.input_per_1k, sol.output_per_1k) == (0.005, 0.030)
        # Converse models must NOT be marked
        assert by_family["anthropic.claude-sonnet-5"].mantle is False


class TestSystemAgentModelConstruction:
    """The agent role must resolve the ID, not wrap the constructed object.

    Regression: ``resolve_model(_system_agent_model())`` passed a BedrockModel
    INSTANCE to a function that classifies id strings, so it silently returned
    the object unchanged and ``openai.gpt-5.6-luna`` went to ConverseStream —
    "The provided model identifier is invalid" on every tool call, with the
    Mantle path never executing. Two live experiments were invalidated by it.
    """

    def test_converse_id_yields_bedrock_model_with_token_cap(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_AGENT_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
        from agents.system_agent import _system_agent_model
        m = _system_agent_model()
        assert type(m).__name__ == "BedrockModel"
        cfg = getattr(m, "config", {})
        assert cfg.get("model_id") == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert cfg.get("max_tokens") == 8192

    def test_mantle_id_yields_the_provider_not_a_bedrock_model(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_AGENT_MODEL", "openai.gpt-5.6-luna")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        import agents.model_provider as mp
        sentinel = object()
        monkeypatch.setattr(mp, "_mantle_model", lambda bare, region, mt=None: sentinel)
        from agents.system_agent import _system_agent_model
        assert _system_agent_model() is sentinel

    def test_resolve_model_is_a_noop_on_a_non_string(self):
        """Why the bug was silent — documents the trap so it is not re-introduced."""
        from agents.model_provider import is_mantle_only
        class FakeBedrockModel:
            config = {"model_id": "openai.gpt-5.6-luna"}
        assert is_mantle_only(FakeBedrockModel()) is False


class TestBuildModelSharedSeam:
    """One builder for both roles so the token cap cannot be forgotten.

    The orchestrator previously passed a BARE id string, inheriting the Bedrock
    default cap. A model verbose enough to exceed it mid event-loop makes strands
    raise MaxTokensReachedException — unrecoverable, and it surfaced only as a
    generic "Query failed." (Sonnet 5: 6 of 8 interleaved runs, 2026-07-30).
    """

    def test_converse_id_gets_an_explicit_cap(self):
        from agents.model_provider import build_model
        m = build_model("global.anthropic.claude-sonnet-5", max_tokens=8192)
        assert type(m).__name__ == "BedrockModel"
        assert m.config.get("max_tokens") == 8192
        assert m.config.get("model_id") == "global.anthropic.claude-sonnet-5"

    def test_orchestrator_cap_is_at_least_the_agent_cap(self):
        """The orchestrator writes the LONG final answers; capping it below the
        system agents would be backwards."""
        from agents.model_provider import ORCHESTRATOR_MAX_TOKENS
        assert ORCHESTRATOR_MAX_TOKENS >= 8192
        # 8192 was hit by Sonnet 5 on a long multi-plant answer (2026-10-01).
        assert ORCHESTRATOR_MAX_TOKENS >= 16384

    def test_mantle_id_returns_the_provider_untouched(self, monkeypatch):
        import agents.model_provider as mp
        sentinel = object()
        monkeypatch.setattr(mp, "_mantle_model", lambda bare, region, mt=None: sentinel)
        assert mp.build_model("openai.gpt-5.6-luna", max_tokens=8192) is sentinel

    def test_orchestrator_passes_an_id_not_an_object(self, monkeypatch):
        """Guards the no-op trap: build_model must receive a STRING."""
        import inspect

        from agents import orchestrator
        from agents.model_catalog import env_orchestrator_model
        src = inspect.getsource(orchestrator)
        assert "build_model(env_orchestrator_model()" in src
        assert "build_model(resolve_model(" not in src
        # ...and what that helper hands build_model is an id string, set or unset.
        monkeypatch.delenv("BEDROCK_MODEL", raising=False)
        assert isinstance(env_orchestrator_model(), str)
        monkeypatch.setenv("BEDROCK_MODEL", "openai.gpt-5.6-sol")
        assert env_orchestrator_model() == "openai.gpt-5.6-sol"


class TestMantleOutputCap:
    """Mantle models must be capped too — an uncapped model is exposed to the
    same MaxTokensReached abort that took the orchestrator down."""

    def test_cap_maps_to_max_output_tokens(self, monkeypatch):
        captured = {}

        class FakeProvider:
            def __init__(self, **kw):
                captured.update(kw)

        import agents.model_provider as mp
        monkeypatch.setattr(mp, "_mantle_model",
                            lambda bare, region, mt=None: FakeProvider(
                                model_id=bare,
                                params={"max_output_tokens": mt} if mt else None))
        mp.build_model("openai.gpt-5.6-luna", max_tokens=8192)
        assert captured["params"] == {"max_output_tokens": 8192}

    def test_no_cap_means_no_params_key(self):
        """Omitting the cap must not send params=None into the provider."""
        import inspect

        from agents.model_provider import _mantle_model
        src = inspect.getsource(_mantle_model)
        assert 'if max_tokens:' in src
        assert '"params"' in src
