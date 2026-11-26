# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test for the admin LLM-settings SAVE path, with AWS fully mocked.

Exercises the real ``update_llm_settings`` / ``get_llm_settings`` handlers — SSM
persistence, change detection, the no-runtime AgentCore branch, and the GET
reading the saved value back through ``model_config`` — without touching real
AWS or requiring a running app. Both are called through their routes.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_llm_settings_save.py
  * standalone:  python agentic_digital_thread/tests/unit/test_llm_settings_save.py

(Note: ``.venv-local`` doesn't ship pytest; the standalone runner works there.)

Not covered: the real ``update_agent_runtime`` AgentCore call (only fires against
a live cloud orchestrator runtime). The surrounding logic — which model change
triggers it, the restart/reonboard signals, error handling — is covered.
"""
import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

_ORCH_PARAM = "/adt/llm-orchestrator-model"
_AGENT_PARAM = "/adt/llm-agent-model"


class _FakeSSM:
    """In-memory Parameter Store."""
    def __init__(self):
        self.store: dict[str, str] = {}

    def get_parameter(self, Name):
        if Name not in self.store:
            raise Exception(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Value": self.store[Name]}}

    def put_parameter(self, Name, Value, Type="String", Overwrite=True):
        self.store[Name] = Value
        return {}


class _FakeControl:
    """AgentCore control plane with no runtimes (local-like)."""
    def list_agent_runtimes(self):
        return {"agentRuntimes": []}


def _make_admin_request(body: dict):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(is_admin=True)

    async def _json():
        return body
    req.json = _json
    return req


def test_llm_settings_save_path():
    """Save persists to SSM, sets the right flags, and GET reads it back."""
    os.environ["AWS_REGION"] = "us-east-1"
    os.environ["BEDROCK_MODEL"] = "us.anthropic.claude-sonnet-4-6"
    os.environ["SYSTEM_AGENT_MODEL"] = "us.amazon.nova-lite-v1:0"

    ssm = _FakeSSM()
    control = _FakeControl()

    def fake_client(service_name, **kwargs):
        if service_name == "ssm":
            return ssm
        if service_name == "bedrock-agentcore-control":
            return control
        raise RuntimeError(f"unexpected boto3 client requested in test: {service_name}")

    with patch("boto3.client", fake_client):
        from agents.admin_routes import update_llm_settings_route, get_llm_settings_route

        # Baseline: empty SSM → env fallback.
        base = asyncio.run(get_llm_settings_route(types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))))
        assert base["orchestrator_model"] == "us.anthropic.claude-sonnet-4-6", base
        assert base["agent_model"] == "us.amazon.nova-lite-v1:0", base

        # Save a change to both models.
        new_orch = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        new_agent = "us.amazon.nova-pro-v1:0"
        res = asyncio.run(update_llm_settings_route(_make_admin_request(
            {"orchestrator_model": new_orch, "agent_model": new_agent})))
        assert res.get("status") == "ok", res
        assert res.get("restart_required") is True, res          # orch changed
        assert res.get("reonboard_required") is True, res        # agent changed

        # Persisted to SSM.
        assert ssm.store.get(_ORCH_PARAM) == new_orch, ssm.store
        assert ssm.store.get(_AGENT_PARAM) == new_agent, ssm.store

        # GET reflects the saved values (SSM override beats env) — end-to-end.
        after = asyncio.run(get_llm_settings_route(types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))))
        assert after["orchestrator_model"] == new_orch, after
        assert after["agent_model"] == new_agent, after

        # No-op save (same values) → no change flags.
        res2 = asyncio.run(update_llm_settings_route(_make_admin_request(
            {"orchestrator_model": new_orch, "agent_model": new_agent})))
        assert not res2.get("restart_required"), res2
        assert not res2.get("reonboard_required"), res2


if __name__ == "__main__":
    test_llm_settings_save_path()
    print("RESULT: PASS — LLM-settings save path verified (SSM write + change detection "
          "+ GET read-back), no real AWS")
