# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Credential keys must NEVER reach the plaintext CONNECTION_CONFIG env fallback.

Security fix: when the Secrets Manager write fails,
``_start_cloud`` falls back to passing the connection config inline via the
CONNECTION_CONFIG runtime env var — which previously included credential keys
(password/secret/token/api_key/passphrase) in PLAINTEXT (visible in the
AgentCore console / describe calls).

Covered here, at both call sites:

1. The shared :func:`agents.runtime_provision_helpers.runtime_connection_env`
   (the one seam both the in-app create path and the provisioner handler use):
   with no secret ARN, credential keys are STRIPPED, non-secret keys are KEPT
   (the config is never silently dropped wholesale), and a loud WARNING tells
   the operator the agent-config secret must be repaired.
2. ``AgentManager._start_cloud`` (agent_manager.py, secret-write-fails path):
   a failing Secrets Manager client leads to a runtime env whose
   CONNECTION_CONFIG carries no credential values, plus the repair warning.

Sensitivity classification is delegated to
``agents.response_integrity_security.sensitive_field`` (single source).
"""
import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import AgentManager  # noqa: E402
from agents.runtime_provision_helpers import (  # noqa: E402
    runtime_connection_env,
    strip_credential_keys,
)

_CREDS = {
    "password": "hunter2",
    "secret": "s3cr3t-value",
    "token": "tok-abc123",
    "api_key": "AKIAIOSFODNN7EXAMPLE",
    "passphrase": "open-sesame",
}
_NON_SECRETS = {"host": "db.example.internal", "port": 5432, "database": "erp",
                "username": "app_user"}


# --------------------------------------------------------------------------- #
# strip_credential_keys — the classifier seam
# --------------------------------------------------------------------------- #
def test_strip_credential_keys_removes_all_credential_keys_keeps_rest():
    cfg = {**_NON_SECRETS, **_CREDS}
    out = strip_credential_keys(cfg)
    assert out == _NON_SECRETS
    for key in _CREDS:
        assert key not in out


def test_strip_credential_keys_handles_none_and_empty():
    assert strip_credential_keys({}) == {}
    assert strip_credential_keys(None) == {}


# --------------------------------------------------------------------------- #
# Call site 1: the shared runtime_connection_env fallback
# --------------------------------------------------------------------------- #
def test_env_fallback_contains_no_credential_keys_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.runtime_provision_helpers"):
        env = runtime_connection_env("PostgreSQL", connection_secret_arn="",  # nosec B106 - offline test fixture/placeholder credential, not a live secret
                                     effective_config={**_NON_SECRETS, **_CREDS})
    payload = json.loads(env["CONNECTION_CONFIG"])
    # Credential keys/values stripped...
    for key, value in _CREDS.items():
        assert key not in payload, payload
        assert value not in env["CONNECTION_CONFIG"], env["CONNECTION_CONFIG"]
    # ...non-secret keys kept (never silently drop the whole config).
    assert payload == _NON_SECRETS
    # Loud repair warning logged, without leaking the credential values.
    assert "STRIPPED" in caplog.text and "repaired" in caplog.text
    for value in _CREDS.values():
        assert value not in caplog.text


def test_env_fallback_no_warning_when_config_has_no_credentials(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.runtime_provision_helpers"):
        env = runtime_connection_env("PostgreSQL", connection_secret_arn="",  # nosec B106 - offline test fixture/placeholder credential, not a live secret
                                     effective_config=dict(_NON_SECRETS))
    assert json.loads(env["CONNECTION_CONFIG"]) == _NON_SECRETS
    assert "STRIPPED" not in caplog.text


def test_secret_arn_path_unchanged_no_inline_config():
    env = runtime_connection_env(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        "PostgreSQL",
        connection_secret_arn="arn:aws:secretsmanager:us-east-1:111122223333:secret:cfg",
        effective_config={**_NON_SECRETS, **_CREDS},
    )
    assert env == {"CONNECTION_SECRET_ARN":
                   "arn:aws:secretsmanager:us-east-1:111122223333:secret:cfg"}


def test_agent_manager_wrapper_strips_credentials_too():
    """The in-app staticmethod wrapper delegates to the shared helper."""
    env = AgentManager._runtime_connection_env(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        "PostgreSQL", connection_secret_arn="",
        effective_config={**_NON_SECRETS, **_CREDS},
    )
    payload = json.loads(env["CONNECTION_CONFIG"])
    assert payload == _NON_SECRETS


def test_provisioner_handler_import_is_the_shared_helper():
    """The provisioner call site imports the SAME function — no drift possible."""
    from agents.provisioner import handlers
    assert handlers._runtime_connection_env is runtime_connection_env


# --------------------------------------------------------------------------- #
# Call site 2: AgentManager._start_cloud with a FAILING Secrets Manager write
# --------------------------------------------------------------------------- #
class _FailingSecrets:
    """Secrets Manager stand-in whose write always fails (e.g. AccessDenied)."""

    class ResourceExistsException(Exception):
        pass

    def __init__(self):
        self.exceptions = types.SimpleNamespace(
            ResourceExistsException=self.ResourceExistsException)

    def create_secret(self, **kwargs):
        raise RuntimeError("AccessDeniedException: not authorized to CreateSecret")


class _FakeControl:
    """bedrock-agentcore-control stand-in with a pre-existing READY runtime, so
    _start_cloud takes the update path and we can capture the env it sends."""

    def __init__(self, runtime_name="adt_example_erp"):
        self._runtime_name = runtime_name
        self.update_calls = []

    def list_agent_runtimes(self):
        return {"agentRuntimes": [{
            "agentRuntimeName": self._runtime_name,
            "agentRuntimeId": "rt-1",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-1",
            "status": "READY",
        }]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg name
        return {
            "status": "READY",
            "environmentVariables": {"SYSTEM_ID": "example_erp"},
            "agentRuntimeArtifact": {"containerConfiguration": {
                "containerUri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:h"}},
            "roleArn": "arn:role",
            "networkConfiguration": {"networkMode": "PUBLIC"},
        }

    def update_agent_runtime(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"agentRuntimeId": kwargs.get("agentRuntimeId")}


def _fake_client_factory(secrets, control):
    def _client(service, region_name=None):
        if service == "secretsmanager":
            return secrets
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"unexpected boto3 client: {service}")
    return _client


def test_start_cloud_secret_write_failure_strips_credentials_from_env(caplog):
    control = _FakeControl()
    secrets = _FailingSecrets()
    mgr = AgentManager(
        mode="cloud", gateway_id="", gateway_url="https://gw/mcp",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:h",
        runtime_role_arn="arn:role",
    )
    with patch("boto3.client", _fake_client_factory(secrets, control)), \
            patch.object(AgentManager, "_warmup_runtime", lambda self, url, region: None), \
            patch.object(AgentManager, "_ensure_credential_provider",
                         lambda self, control, region: ""), \
            caplog.at_level(logging.WARNING):
        mgr._start_cloud(
            system_id="example_erp", system_name="ERP", schema="public",
            system_prompt="p", tables_desc="t",
            connection_type="PostgreSQL",
            connection_config={**_NON_SECRETS, **_CREDS},
        )

    assert len(control.update_calls) == 1, control.update_calls
    env = control.update_calls[0]["environmentVariables"]
    assert "CONNECTION_SECRET_ARN" not in env  # the write failed
    payload = json.loads(env["CONNECTION_CONFIG"])
    assert payload == _NON_SECRETS
    blob = json.dumps(env)
    for value in _CREDS.values():
        assert value not in blob, f"credential value leaked into runtime env: {env}"
    # Loud repair warning fires during _start_cloud (emitted by the shared
    # runtime_provision_helpers fallback — the one seam both call sites use;
    # the 468-line _start_cloud itself is frozen by the changed-function
    # maintainability gate, so the message lives in the helper).
    assert "STRIPPED" in caplog.text and "repaired" in caplog.text
