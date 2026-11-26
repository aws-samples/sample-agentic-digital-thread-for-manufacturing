# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the fast-path runtime-verification fix in ``AgentManager.start_agent``.

Background (the bug this test pins down): in cloud mode ``start_agent`` has a
"fast path" that, to avoid re-creating cloud resources on every ECS task restart,
short-circuits and just registers the gateway URL whenever a gateway TARGET for the
system exists and is READY. It used to check ONLY the gateway target status, NOT
whether the underlying AgentCore RUNTIME actually exists. When a runtime was deleted
(or never created) but its gateway target lingered as stale-READY, the fast path left
a DANGLING target: queries routed to a non-existent runtime returned "endpoint not
responding" and the orchestrator then fabricated answers — silently breaking the
whole data plane.

The fix: the fast path now takes the short-circuit only when BOTH
  (a) a READY gateway target exists, AND
  (b) a backing runtime named ``adt_<id>`` exists and is READY.
Otherwise it falls through to ``_start_cloud`` to (re)create the runtime.

These tests mock ``bedrock-agentcore-control`` (no real AWS) and assert:
  (i)  target READY + NO matching runtime  -> does NOT fast-path, calls _start_cloud
  (ii) target READY + runtime READY         -> fast-paths, does NOT call _start_cloud
Plus the stale-runtime variant (target READY + runtime CREATE_FAILED -> falls through).

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_agent_manager_fastpath_runtime.py
  * standalone:  python agentic_digital_thread/tests/unit/test_agent_manager_fastpath_runtime.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import AgentManager  # noqa: E402

_SYSTEM_ID = "example_sitewise"
_RUNTIME_NAME = "adt_example_sitewise"          # _start_cloud naming: adt_<id>, hyphens->underscores
_TARGET_NAME = "example-sitewise"              # gateway target naming: underscores->hyphens
_GATEWAY_URL = "https://gw.example.com/mcp"


class _FakeControl:
    """bedrock-agentcore-control stand-in.

    Configurable to present a READY gateway target and, independently, a runtime in
    a chosen state (or none at all)."""

    def __init__(self, target_ready=True, runtime_status="READY", include_runtime=True):
        self._target_ready = target_ready
        self._runtime_status = runtime_status
        self._include_runtime = include_runtime
        self.list_agent_runtimes_calls = 0

    def list_gateway_targets(self, gatewayIdentifier=None):  # noqa: N803 - boto3 kwarg name
        return {"items": [{
            "name": _TARGET_NAME,
            "targetId": "tgt-1",
            "status": "READY" if self._target_ready else "FAILED",
        }]}

    def list_agent_runtimes(self):
        self.list_agent_runtimes_calls += 1
        if not self._include_runtime:
            return {"agentRuntimes": []}
        return {"agentRuntimes": [{
            "agentRuntimeName": _RUNTIME_NAME,
            "agentRuntimeId": "rt-1",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:444455556666:runtime/rt-1",
            "status": self._runtime_status,
        }]}


def _mgr():
    return AgentManager(
        mode="cloud",
        gateway_id="gw-123",
        gateway_url=_GATEWAY_URL,
        ecr_image_uri="444455556666.dkr.ecr.us-east-1.amazonaws.com/adt-agents:hash",
        runtime_role_arn="arn:role",
    )


def _call_start_agent(control):
    """Invoke start_agent with _start_cloud patched to a sentinel so we can assert
    whether the fast path was taken (no call) or bypassed (called)."""
    def _client(service, region_name=None):
        assert service == "bedrock-agentcore-control", service
        return control
    with patch("boto3.client", _client), \
            patch.object(AgentManager, "_start_cloud",
                         autospec=True, return_value="cloud://created") as start_cloud:
        url = _mgr().start_agent(
            system_id=_SYSTEM_ID, system_name="SiteWise", schema="",
            system_prompt="p", tables_desc="t",
            connection_type="IoT SiteWise", connection_config={"region": "us-east-1"},
        )
    return url, start_cloud


def test_target_ready_but_no_runtime_falls_through_to_start_cloud():
    """(i) READY gateway target + NO matching runtime -> must NOT fast-path; must
    call _start_cloud (a dangling target must never be registered)."""
    control = _FakeControl(target_ready=True, include_runtime=False)
    url, start_cloud = _call_start_agent(control)

    assert start_cloud.called, "must fall through to _start_cloud when runtime is absent"
    assert url == "cloud://created"


def test_target_ready_and_runtime_ready_takes_fast_path():
    """(ii) READY gateway target + READY runtime -> fast-paths; _start_cloud is NOT
    called and the gateway URL is returned directly."""
    control = _FakeControl(target_ready=True, runtime_status="READY", include_runtime=True)
    url, start_cloud = _call_start_agent(control)

    assert not start_cloud.called, "must NOT call _start_cloud when target+runtime are both READY"
    assert url == _GATEWAY_URL
    # A single list_agent_runtimes call is acceptable for the healthy case.
    assert control.list_agent_runtimes_calls == 1, control.list_agent_runtimes_calls


def test_target_ready_but_runtime_not_ready_falls_through():
    """READY gateway target + runtime present-but-not-READY (e.g. CREATE_FAILED) ->
    must fall through to _start_cloud, not register the (unusable) dangling target."""
    control = _FakeControl(target_ready=True, runtime_status="CREATE_FAILED", include_runtime=True)
    url, start_cloud = _call_start_agent(control)

    assert start_cloud.called, "must fall through when the backing runtime is not READY"
    assert url == "cloud://created"


def test_runtime_name_matches_start_cloud_convention():
    """The shared _runtime_name helper must reproduce _start_cloud's adt_<id> naming
    (hyphens folded to underscores, capped at 48) so the fast-path health check and
    the create path can never look up different runtimes."""
    assert AgentManager._runtime_name("example-sitewise") == "adt_example_sitewise"
    assert AgentManager._runtime_name("example_sitewise") == "adt_example_sitewise"
    long_id = "x" * 80
    assert AgentManager._runtime_name(long_id) == f"adt_{long_id}"[:48]
    assert len(AgentManager._runtime_name(long_id)) == 48


_TESTS = [
    test_target_ready_but_no_runtime_falls_through_to_start_cloud,
    test_target_ready_and_runtime_ready_takes_fast_path,
    test_target_ready_but_runtime_not_ready_falls_through,
    test_runtime_name_matches_start_cloud_convention,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} fast-path runtime-verification tests passed "
          "(no real AWS)")
