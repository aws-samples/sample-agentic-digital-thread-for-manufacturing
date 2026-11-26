# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test for the system-runtime image-staleness fix in ``_start_cloud``.

Background (the bug this test pins down): when a system is re-onboarded,
``AgentManager._start_cloud`` lists the existing AgentCore runtimes, finds the one
named ``adt_<id>``, and REUSES it — but it used to leave the runtime pointed at its
OLD container image. So after a redeploy that rebuilt the agent image with a bug
fix, re-onboarding a system left its runtime on the pre-fix image (observed:
example-sitewise crash-looped on the old image until the runtime was manually DELETED
so a fresh one was created from the new image).

The fix: when the runtime already exists, ``_start_cloud`` now issues
``update_agent_runtime`` with the CURRENT agent image URI (the same source a fresh
create uses), preserving the freshly built environment (incl. CONNECTION_SECRET_ARN)
and waiting for READY.

This test mocks the ``bedrock-agentcore-control`` client and asserts that the
update call carries the current image URI (not omitted / not the stale one).

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_runtime_image_refresh.py
  * standalone:  python agentic_digital_thread/tests/unit/test_runtime_image_refresh.py
"""
import sys
import types
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import AgentManager  # noqa: E402

# The image the runtime is CURRENTLY (stale) on, and the NEW image a redeploy built.
_STALE_IMAGE = "444455556666.dkr.ecr.us-east-1.amazonaws.com/adt-agents:oldhash"
_CURRENT_IMAGE = "444455556666.dkr.ecr.us-east-1.amazonaws.com/adt-agents:newhash"


class _FakeSecrets:
    """Secrets Manager stand-in — create-or-update returns an ARN."""

    class ResourceExistsException(Exception):
        pass

    def __init__(self):
        self.exceptions = types.SimpleNamespace(
            ResourceExistsException=self.ResourceExistsException)

    def create_secret(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/sitewise"}

    def put_secret_value(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/sitewise"}

    def describe_secret(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/sitewise"}


class _FakeControl:
    """bedrock-agentcore-control stand-in with a pre-existing READY runtime."""

    def __init__(self, runtime_name="adt_example_sitewise"):
        self._runtime_name = runtime_name
        # get_agent_runtime returns the runtime STILL on the stale image + READY,
        # so _start_cloud's post-update poll returns immediately.
        self._details = {
            "status": "READY",
            "environmentVariables": {"SYSTEM_ID": "example_sitewise", "AWS_REGION": "us-east-1"},
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": _STALE_IMAGE}},
            "roleArn": "arn:role",
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {"subnets": ["s-1"], "securityGroups": ["sg-1"],
                                      "requireServiceS3Endpoint": True},
            },
        }
        self.update_calls = []

    def list_agent_runtimes(self):
        return {"agentRuntimes": [{
            "agentRuntimeName": self._runtime_name,
            "agentRuntimeId": "rt-1",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:444455556666:runtime/rt-1",
            "status": "READY",
        }]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg name
        return dict(self._details)

    def update_agent_runtime(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"agentRuntimeId": kwargs.get("agentRuntimeId")}


def _fake_client_factory(secrets, control):
    """Stand-in for ``boto3.client``. Patched at the real ``boto3`` module so BOTH
    the module-level ``boto3.client`` in agent_manager AND the function-local
    ``import boto3 as _boto3`` (used for the Secrets Manager write) resolve to these
    fakes — NO real AWS calls are made."""
    def _client(service, region_name=None):
        if service == "secretsmanager":
            return secrets
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"unexpected boto3 client: {service}")
    return _client


def _mgr():
    # gateway_id="" so the gateway-target block is skipped — this test targets only
    # the runtime create-vs-update decision. ecr_image_uri is the NEW (current) image.
    return AgentManager(mode="cloud", gateway_id="", gateway_url="https://gw/mcp",
                        ecr_image_uri=_CURRENT_IMAGE, runtime_role_arn="arn:role")


def _run_start_cloud(control, secrets):
    with patch("boto3.client", _fake_client_factory(secrets, control)), \
            patch.object(AgentManager, "_warmup_runtime", lambda self, url, region: None), \
            patch.object(AgentManager, "_ensure_credential_provider",
                         lambda self, control, region: ""):
        return _mgr()._start_cloud(
            system_id="example_sitewise", system_name="SiteWise", schema="",
            system_prompt="p", tables_desc="t",
            connection_type="IoT SiteWise", connection_config={"region": "us-east-1"})


def test_existing_runtime_is_updated_to_current_image():
    """When the runtime already exists, _start_cloud must call update_agent_runtime
    with the CURRENT agent image URI — not omit the image (the staleness bug)."""
    control = _FakeControl()
    secrets = _FakeSecrets()
    _run_start_cloud(control, secrets)

    # Exactly one image-refresh update on the existing runtime.
    assert len(control.update_calls) == 1, control.update_calls
    upd = control.update_calls[0]
    assert upd["agentRuntimeId"] == "rt-1", upd

    # The artifact/containerUri MUST be present and MUST be the current image.
    artifact = upd.get("agentRuntimeArtifact") or {}
    container = artifact.get("containerConfiguration") or {}
    container_uri = container.get("containerUri")
    assert container_uri == _CURRENT_IMAGE, (
        f"update must carry the CURRENT image; got {container_uri!r}")
    assert container_uri != _STALE_IMAGE, "update must NOT leave the stale image"


def test_update_preserves_env_and_strips_s3_endpoint():
    """The image refresh preserves the freshly built environment (incl.
    CONNECTION_SECRET_ARN wiring) and drops requireServiceS3Endpoint (forbidden on
    update), mirroring resync_cloud_connection."""
    control = _FakeControl()
    secrets = _FakeSecrets()
    _run_start_cloud(control, secrets)

    upd = control.update_calls[0]
    env = upd.get("environmentVariables") or {}
    # CONNECTION_SECRET_ARN wiring is kept intact.
    assert env.get("CONNECTION_SECRET_ARN", "").endswith("agent-config/sitewise"), env
    # requireServiceS3Endpoint stripped from the preserved network config.
    nmc = upd["networkConfiguration"].get("networkModeConfig", {})
    assert "requireServiceS3Endpoint" not in nmc, upd["networkConfiguration"]


_TESTS = [
    test_existing_runtime_is_updated_to_current_image,
    test_update_preserves_env_and_strips_s3_endpoint,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} runtime image-refresh tests passed "
          "(no real AWS)")
