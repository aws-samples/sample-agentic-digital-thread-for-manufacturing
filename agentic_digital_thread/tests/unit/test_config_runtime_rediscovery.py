# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""get_config() must re-attempt orchestrator ARN discovery while unresolved.

Regression for the Workshop Studio ordering defect (2026-09-10): the ECS app
task started before the EngineRuntimeBootstrap custom resource created the
``adt_orchestrator`` runtime, discovery returned None, and the old
``@lru_cache`` pinned that None for the life of the process -- every
/api/ask was a 500 until a manual ECS redeploy.
"""

import pytest

from agents import config as config_mod
from agents.config import get_config


@pytest.fixture
def cloud_env(monkeypatch):
    """Cloud-mode env: gateway wired, runtime ARN left to discovery."""
    for k in ("AGENT_MODE", "DEV_MODE", "DEV_NO_AUTH", "AGENTCORE_RUNTIME_ARN", "MEMORY_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://gw.example/mcp")
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", "gw-123")
    # Memory discovery is out of scope here; keep it quiet and deterministic.
    monkeypatch.setattr(config_mod, "_discover_memory_id", lambda region: None)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def test_rediscovers_runtime_arn_after_initial_miss(cloud_env, monkeypatch):
    arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_orchestrator-abc"
    answers = iter([None, None, arn])
    calls = {"n": 0}

    def fake_discover(region):
        calls["n"] += 1
        return next(answers)

    monkeypatch.setattr(config_mod, "_discover_orchestrator_arn", fake_discover)

    clock = {"t": 1000.0}
    monkeypatch.setattr(config_mod.time, "monotonic", lambda: clock["t"])

    # First load: runtime does not exist yet -> unresolved, cached.
    assert get_config().agentcore_runtime_arn is None
    assert calls["n"] == 1

    # Inside the retry window: served from cache, no control-plane call.
    clock["t"] += 1.0
    assert get_config().agentcore_runtime_arn is None
    assert calls["n"] == 1

    # Past the window: re-attempt (still missing).
    clock["t"] += config_mod._UNRESOLVED_RETRY_SECS
    assert get_config().agentcore_runtime_arn is None
    assert calls["n"] == 2

    # Past the window again: runtime now exists -> resolved.
    clock["t"] += config_mod._UNRESOLVED_RETRY_SECS
    assert get_config().agentcore_runtime_arn == arn
    assert calls["n"] == 3

    # Resolved config is pinned: no further discovery even after a long time.
    clock["t"] += 10_000.0
    assert get_config().agentcore_runtime_arn == arn
    assert calls["n"] == 3


def test_local_mode_never_retries(cloud_env, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    calls = {"n": 0}

    def fake_discover(region):
        calls["n"] += 1
        return None

    monkeypatch.setattr(config_mod, "_discover_orchestrator_arn", fake_discover)
    clock = {"t": 0.0}
    monkeypatch.setattr(config_mod.time, "monotonic", lambda: clock["t"])

    first = get_config()
    clock["t"] += 10 * config_mod._UNRESOLVED_RETRY_SECS
    assert get_config() is first
    assert first.has_runtime is False  # local fallback branch, no RuntimeError


def test_cache_clear_forces_reload(cloud_env, monkeypatch):
    monkeypatch.setattr(config_mod, "_discover_orchestrator_arn", lambda region: "arn:x")
    a = get_config()
    get_config.cache_clear()
    b = get_config()
    assert a is not b
    assert b.agentcore_runtime_arn == "arn:x"
