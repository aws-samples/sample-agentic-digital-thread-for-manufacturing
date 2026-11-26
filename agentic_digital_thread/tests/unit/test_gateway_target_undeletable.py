# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A non-READY gateway target that cannot be deleted is kept only if it
matches this provision.

When the recreate path's delete fails, the old target used to be kept and
synced whenever it still existed with SOME readable endpoint — including one
naming a deleted runtime, so the system was synced and registered against
the wrong runtime. These tests pin, for
``AgentManager._create_or_reuse_gateway_target``:

* a kept target whose endpoint/credentials already match is synced with no
  update (unchanged);
* a mismatched endpoint, or mismatched credentials, is brought in line
  through the verified update path, then synced;
* a mismatched target whose update fails or does not verifiably land raises
  :class:`GatewayTargetRecreateError`: no sync, nothing registered.

All fixtures are synthetic; no AWS call is made.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import pytest  # noqa: E402

from agents.agent_manager import AgentManager  # noqa: E402
from agents.runtime_provision_helpers import GatewayTargetRecreateError  # noqa: E402

_REGION = "us-east-1"
_GW = "gw-example-111"
_SYSTEM = "example_system"
_TARGET = "example-system"
_TID = "tid-old"
_OLD_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_example_system-OLD0000001"
_NEW_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_example_system-NEW0000002"
_PROVIDER = "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/adt-m2m"
_OTHER = "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/old"


def _endpoint(arn: str) -> str:
    return AgentManager._runtime_mcp_url(arn, _REGION)


def _creds(provider: str) -> list:
    return [{"credentialProviderType": "OAUTH", "credentialProvider": {
        "oauthCredentialProvider": {"providerArn": provider,
                                    "scopes": ["mcp/tools.read", "mcp/tools.execute"],
                                    "grantType": "CLIENT_CREDENTIALS"}}}]


class _ConflictException(Exception):
    pass


class _UndeletableControl:
    """One non-READY target whose delete always fails (AccessDenied).

    ``update_gateway_target`` applies what was sent unless ``update_error``
    is set; ``after_update`` is the status read back afterwards."""

    class exceptions:  # noqa: N801 - boto3 shape
        ConflictException = _ConflictException

    def __init__(self, *, arn: str = _OLD_ARN, creds=None, after_update: str = "READY",
                 update_error: Exception | None = None):
        self.target = {"name": _TARGET, "targetId": _TID, "status": "SYNCHRONIZE_UNSUCCESSFUL",
                       "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": _endpoint(arn)}}}}
        if creds is not None:
            self.target["credentialProviderConfigurations"] = creds
        self.after_update = after_update
        self.update_error = update_error
        self.updates: list[dict] = []
        self.synced: list[list[str]] = []
        self.created = 0

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        return {"items": [{k: self.target[k] for k in ("name", "targetId", "status")}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        return copy.deepcopy(self.target)

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        raise RuntimeError("AccessDeniedException: not authorized to delete")

    def create_gateway_target(self, **kwargs):
        self.created += 1
        raise _ConflictException("target exists")

    def update_gateway_target(self, **kwargs):
        self.updates.append(copy.deepcopy(kwargs))
        if self.update_error is not None:
            raise self.update_error
        self.target["targetConfiguration"] = copy.deepcopy(kwargs["targetConfiguration"])
        if "credentialProviderConfigurations" in kwargs:
            self.target["credentialProviderConfigurations"] = copy.deepcopy(
                kwargs["credentialProviderConfigurations"])
        self.target["status"] = self.after_update
        return {}

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):  # noqa: N803
        self.synced.append(list(targetIdList))
        return {}


def _upsert(control: _UndeletableControl, provider: str = "") -> str:
    mgr = AgentManager.__new__(AgentManager)  # no __init__ side effects needed
    mgr.gateway_id = _GW
    with patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=provider):
        return mgr._upsert_and_sync_gateway_target(
            control, _REGION, _SYSTEM, _SYSTEM, _endpoint(_NEW_ARN))


def test_a_matching_undeletable_target_is_synced_without_an_update():
    control = _UndeletableControl(arn=_NEW_ARN)
    assert _upsert(control) == _TID
    assert control.updates == [] and control.synced == [[_TID]]


def test_an_undeletable_target_naming_another_runtime_is_updated_then_synced():
    control = _UndeletableControl(arn=_OLD_ARN)
    assert _upsert(control) == _TID
    (sent,) = control.updates
    assert sent["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _endpoint(_NEW_ARN)
    assert control.synced == [[_TID]]


def test_an_undeletable_target_with_other_credentials_is_updated_then_synced():
    control = _UndeletableControl(arn=_NEW_ARN, creds=_creds(_OTHER))
    assert _upsert(control, provider=_PROVIDER) == _TID
    (sent,) = control.updates
    assert sent["credentialProviderConfigurations"] == _creds(_PROVIDER)
    assert control.synced == [[_TID]]


def test_a_drifted_undeletable_target_whose_update_fails_raises_and_never_syncs():
    control = _UndeletableControl(arn=_OLD_ARN, update_error=RuntimeError("AccessDenied"))
    with pytest.raises(GatewayTargetRecreateError, match="could not bring it in line"):
        _upsert(control)
    assert control.synced == []


def test_a_drifted_undeletable_target_whose_update_does_not_take_raises():
    control = _UndeletableControl(arn=_OLD_ARN, after_update="UPDATE_UNSUCCESSFUL")
    with pytest.raises(GatewayTargetRecreateError, match="UPDATE_UNSUCCESSFUL"):
        _upsert(control)
    assert control.synced == []


def test_start_agent_registers_nothing_when_the_drifted_target_cannot_be_fixed():
    control = _UndeletableControl(arn=_OLD_ARN, update_error=RuntimeError("AccessDenied"))
    mgr = AgentManager(
        mode="cloud", gateway_id=_GW, gateway_url="https://gw.example.com/mcp",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:hash",
        runtime_role_arn="arn:aws:iam::111122223333:role/adt-runtime")
    with patch.dict("os.environ", {"AWS_REGION": _REGION}), \
            patch("boto3.client", lambda *a, **k: control), \
            patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_cloud_fast_path_ready", return_value=False), \
            patch.object(AgentManager, "_apply_business_context",
                         side_effect=lambda _sid, cfg, _region: cfg), \
            patch.object(AgentManager, "_prepare_runtime_env", return_value={}), \
            patch.object(AgentManager, "_m2m_authorizer_config", return_value=None), \
            patch.object(AgentManager, "_find_reusable_runtime",
                         return_value=(_NEW_ARN, "adt_example_system-NEW0000002")), \
            patch.object(AgentManager, "_refresh_existing_runtime_image", return_value=None), \
            patch.object(AgentManager, "_warmup_runtime", return_value=None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=""), \
            pytest.raises(GatewayTargetRecreateError):
        mgr.start_agent(system_id=_SYSTEM, system_name=_SYSTEM, schema="", system_prompt="")
    assert _SYSTEM not in mgr._agents and control.synced == []
