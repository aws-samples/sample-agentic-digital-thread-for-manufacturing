# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A query-bridge runtime that still carries the old plaintext token is moved.

When the app holds the x-quick-auth token by reference
(``QUICK_GATEWAY_TOKEN_REF``) but the existing ``adt_query_bridge`` runtime was
provisioned with the plaintext ``QUICK_GATEWAY_TOKEN``, its token is the old
one. The fast path (READY runtime + READY target) would register it as it is,
and every bridge call would then be refused. ``ensure_query_bridge_target``
must instead provision through ``_start_cloud``, which rewrites the runtime
env, and fail (so the provisioner retries) if the runtime still carries the
old env afterwards. No AWS, no network.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import AgentManager, QUERY_BRIDGE_SYSTEM_ID  # noqa: E402

_REF = "arn:aws:secretsmanager:us-east-1:111122223333:secret:QuickGatewayTokenSecret-abc"
_ENV = {"QUICK_GATEWAY_ID": "quick-gw-222",
        "QUICK_GATEWAY_URL": "https://quick-gw-222.gateway.example.com/mcp",
        "QUICK_GATEWAY_TOKEN_REF": _REF}


class _Control:
    """Runtime list + detail for the bridge runtime; ``envs`` is returned in order."""

    def __init__(self, envs):
        self._envs = list(envs)

    def list_agent_runtimes(self, **_):
        return {"agentRuntimes": [{"agentRuntimeName": AgentManager._runtime_name(QUERY_BRIDGE_SYSTEM_ID),
                                   "agentRuntimeId": "rt-bridge", "status": "READY"}]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803
        env = self._envs.pop(0) if len(self._envs) > 1 else self._envs[0]
        if isinstance(env, Exception):
            raise env
        return {"agentRuntimeId": agentRuntimeId, "status": "READY", "environmentVariables": env}


def _mgr() -> AgentManager:
    return AgentManager(mode="cloud", gateway_id="shared-gw-111",
                        gateway_url="https://shared-gw-111.gateway.example.com/mcp",
                        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:hash",
                        runtime_role_arn="arn:aws:iam::111122223333:role/adt-runtime")


def _run(envs):
    calls = []
    mgr = _mgr()
    control = _Control(envs)
    with patch.dict("os.environ", _ENV, clear=False), \
            patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "start_agent",
                         side_effect=lambda **kw: calls.append("fast-or-start") or "url"), \
            patch.object(AgentManager, "_start_cloud",
                         side_effect=lambda **kw: calls.append("start_cloud") or "url"), \
            patch.object(AgentManager, "_remove_stale_bridge_target_from_shared_gateway"):
        mgr.ensure_query_bridge_target()
    return calls


def test_a_bridge_with_the_plaintext_token_is_reprovisioned_not_fast_pathed():
    old = {"ADT_APP_URL": "http://alb", "QUICK_GATEWAY_TOKEN": "old-token-value"}
    new = {"ADT_APP_URL": "http://alb", "QUICK_GATEWAY_TOKEN_REF": _REF}
    assert _run([old, new]) == ["start_cloud"]


def test_a_bridge_that_keeps_the_old_env_after_provisioning_fails_loudly():
    old = {"ADT_APP_URL": "http://alb", "QUICK_GATEWAY_TOKEN": "old-token-value"}
    with pytest.raises(RuntimeError, match="old x-quick-auth"):
        _run([old, old])


def test_a_bridge_already_on_the_reference_takes_the_normal_path():
    new = {"ADT_APP_URL": "http://alb", "QUICK_GATEWAY_TOKEN_REF": _REF}
    assert _run([new]) == ["fast-or-start"]


def test_an_unreadable_bridge_env_is_reprovisioned_not_fast_pathed():
    new = {"ADT_APP_URL": "http://alb", "QUICK_GATEWAY_TOKEN_REF": _REF}
    assert _run([RuntimeError("throttled"), new]) == ["start_cloud"]


def test_an_unverifiable_env_after_reprovisioning_fails_loudly():
    old = {"ADT_APP_URL": "http://alb", "QUICK_GATEWAY_TOKEN": "old-token-value"}
    with pytest.raises(RuntimeError, match="old x-quick-auth"):
        _run([old, RuntimeError("throttled")])
