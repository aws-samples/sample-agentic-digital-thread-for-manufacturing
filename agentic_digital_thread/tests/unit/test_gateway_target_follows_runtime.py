# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A reused gateway target must point at the runtime it serves.

Seen live: a system's runtime was deleted while its gateway target survived
(READY, endpoint still naming the deleted runtime). Re-registering the system
created a NEW runtime, ``create_gateway_target`` hit ConflictException on the
name, and the create-or-reuse path reused the READY target UNCHANGED — so every
tool call 404'd on MCP initialize against the deleted runtime.

These tests pin, for the in-app path (``AgentManager``) and the provisioner
Lambda path (``handlers.create_target``):

* a READY target that already matches this provision is reused with no update
  and (in-app) no sync — today's fast behavior;
* a target whose MCP endpoint differs is UPDATED in place (full field set,
  never deleted) and then synchronized;
* a target whose outbound credential config differs gets the same treatment;
* an update that fails or does not settle raises (retryable), registers
  nothing, and leaves the target in place;
* the shared helpers' update kwargs validate against the botocore model.

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
from agents.provisioner import (  # noqa: E402
    CreateTargetInput,
    ProvisionerError,
    TargetUpdateError,
    create_target,
)
from agents.runtime_provision_helpers import (  # noqa: E402
    GatewayTargetUpdateError,
    bring_gateway_target_in_line,
    gateway_target_drift,
    gateway_target_update_kwargs,
)

_REGION = "us-east-1"
_GW = "gw-example-111"
_SYSTEM = "example_system"
_TARGET = "example-system"
_TID = "tid-example-system"
_OLD_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_example_system-OLD0000001"
_NEW_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_example_system-NEW0000002"
_PROVIDER = "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/adt-m2m"
_OTHER_PROVIDER = "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/old"
_SCOPES = ["mcp/tools.read", "mcp/tools.execute"]


def _endpoint(arn: str) -> str:
    return AgentManager._runtime_mcp_url(arn, _REGION)


def _creds(provider: str = _PROVIDER, scopes=None) -> list:
    return [{
        "credentialProviderType": "OAUTH",
        "credentialProvider": {"oauthCredentialProvider": {
            "providerArn": provider,
            "scopes": list(_SCOPES if scopes is None else scopes),
            "grantType": "CLIENT_CREDENTIALS",
        }},
    }]


def _detail(arn: str = _OLD_ARN, *, creds=None, status: str = "READY", **extra) -> dict:
    """A ``get_gateway_target`` response for the existing target."""
    detail = {
        "targetId": _TID, "name": _TARGET, "status": status,
        "description": f"System agent for {_SYSTEM}",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": _endpoint(arn)}}},
        "credentialProviderConfigurations": _creds() if creds is None else creds,
    }
    detail.update(extra)
    return detail


def _desired(arn: str = _NEW_ARN, *, creds=True) -> dict:
    """The ``create_gateway_target`` kwargs the in-app path builds."""
    kwargs = {
        "gatewayIdentifier": _GW, "name": _TARGET,
        "description": f"System agent for {_SYSTEM}",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": _endpoint(arn)}}},
    }
    if creds:
        kwargs["credentialProviderConfigurations"] = _creds()
    return kwargs


class _ConflictException(Exception):
    pass


class _Control:
    """bedrock-agentcore-control stand-in holding ONE existing target.

    ``create_gateway_target`` always conflicts. ``update_gateway_target``
    applies the sent configuration; the target then reports each status in
    ``after_update`` once (the last one sticks)."""

    class exceptions:  # noqa: N801 - boto3 shape
        ConflictException = _ConflictException

    def __init__(self, detail: dict, *, after_update=("UPDATING", "READY"),
                 update_error: Exception | None = None,
                 get_error: Exception | None = None):
        self.detail = copy.deepcopy(detail)
        self._after_update = list(after_update)
        self._update_error = update_error
        self._get_error = get_error
        self.calls: list[str] = []
        self.updates: list[dict] = []
        self.synced: list[list[str]] = []
        self.deleted: list[str] = []

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        self.calls.append("list")
        return {"items": [{"name": self.detail["name"], "targetId": self.detail["targetId"],
                           "status": self.detail["status"]}]}

    def create_gateway_target(self, **kwargs):
        self.calls.append("create")
        raise _ConflictException("target exists")

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.calls.append("get")
        if self._get_error is not None:
            raise self._get_error
        if self.updates and self._after_update:
            status = (self._after_update.pop(0) if len(self._after_update) > 1
                      else self._after_update[0])
            self.detail["status"] = status
        return copy.deepcopy(self.detail)

    def update_gateway_target(self, **kwargs):
        self.calls.append("update")
        self.updates.append(copy.deepcopy(kwargs))
        if self._update_error is not None:
            raise self._update_error
        self.detail["targetConfiguration"] = copy.deepcopy(kwargs["targetConfiguration"])
        if "credentialProviderConfigurations" in kwargs:
            self.detail["credentialProviderConfigurations"] = copy.deepcopy(
                kwargs["credentialProviderConfigurations"])
        return {}

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):  # noqa: N803
        self.calls.append("sync")
        self.synced.append(list(targetIdList))
        return {}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.calls.append("delete")
        self.deleted.append(targetId)
        return {}


_NO_SLEEP = {"sleep": lambda _s: None}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def test_drift_is_empty_for_a_matching_target():
    assert gateway_target_drift(_desired(_OLD_ARN), _detail(_OLD_ARN)) == []


def test_drift_flags_an_endpoint_naming_another_runtime():
    assert gateway_target_drift(_desired(_NEW_ARN), _detail(_OLD_ARN)) == ["endpoint"]


def test_drift_flags_a_target_whose_endpoint_cannot_be_read():
    detail = _detail(_NEW_ARN)
    del detail["targetConfiguration"]
    assert gateway_target_drift(_desired(_NEW_ARN), detail) == ["endpoint"]


def test_drift_compares_the_endpoint_exactly():
    detail = _detail(_NEW_ARN)
    detail["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] += "&x=1"
    assert gateway_target_drift(_desired(_NEW_ARN), detail) == ["endpoint"]


@pytest.mark.parametrize("existing", [
    _creds(_OTHER_PROVIDER),
    _creds(scopes=["mcp/tools.read"]),
    [],
    [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
])
def test_drift_flags_a_different_credential_config(existing):
    assert gateway_target_drift(_desired(_NEW_ARN), _detail(_NEW_ARN, creds=existing)) == [
        "credentials"]


def test_drift_ignores_scope_order():
    detail = _detail(_NEW_ARN, creds=_creds(scopes=list(reversed(_SCOPES))))
    assert gateway_target_drift(_desired(_NEW_ARN), detail) == []


def test_drift_never_flags_credentials_when_this_provision_sets_none():
    """No provider this time (the lookup also comes back empty on a transient
    failure): a target's existing outbound auth is not a mismatch."""
    assert gateway_target_drift(_desired(_NEW_ARN, creds=False), _detail(_NEW_ARN)) == []


def test_update_kwargs_carry_every_field_and_validate_against_botocore():
    botocore_session = pytest.importorskip("botocore.session")
    from botocore.validate import validate_parameters

    detail = _detail(_OLD_ARN, creds=_creds(_OTHER_PROVIDER),
                     metadataConfiguration={"allowedRequestHeaders": ["x-example"]})
    before = copy.deepcopy(detail)
    desired = _desired(_NEW_ARN)
    kwargs = gateway_target_update_kwargs(_TID, desired, detail)
    assert kwargs == {
        "gatewayIdentifier": _GW, "targetId": _TID, "name": _TARGET,
        "description": f"System agent for {_SYSTEM}",
        "targetConfiguration": desired["targetConfiguration"],
        "credentialProviderConfigurations": _creds(),
        "metadataConfiguration": {"allowedRequestHeaders": ["x-example"]},
    }
    assert detail == before
    model = botocore_session.get_session().get_service_model("bedrock-agentcore-control")
    validate_parameters(kwargs, model.operation_model("UpdateGatewayTarget").input_shape)


def test_update_kwargs_keep_the_targets_auth_when_this_provision_has_none():
    kwargs = gateway_target_update_kwargs(_TID, _desired(_NEW_ARN, creds=False), _detail(_OLD_ARN))
    assert kwargs["credentialProviderConfigurations"] == _creds()
    assert kwargs["targetConfiguration"] == _desired(_NEW_ARN)["targetConfiguration"]


def test_bring_in_line_waits_for_the_update_to_settle():
    control = _Control(_detail(_OLD_ARN), after_update=("UPDATING", "UPDATING", "READY"))
    sleeps = []
    settled = bring_gateway_target_in_line(control, _TID, _desired(), control.detail,
                                           sleep=sleeps.append, poll_seconds=5.0)
    assert settled["status"] == "READY"
    assert sleeps == [5.0, 5.0]
    assert control.deleted == []


def test_bring_in_line_raises_when_the_update_call_fails():
    control = _Control(_detail(_OLD_ARN), update_error=RuntimeError("ValidationException"))
    with pytest.raises(GatewayTargetUpdateError, match="ValidationException"):
        bring_gateway_target_in_line(control, _TID, _desired(), control.detail, **_NO_SLEEP)
    assert control.deleted == []


def test_bring_in_line_raises_when_the_update_does_not_take():
    control = _Control(_detail(_OLD_ARN), after_update=("UPDATE_UNSUCCESSFUL",))
    with pytest.raises(GatewayTargetUpdateError, match="UPDATE_UNSUCCESSFUL"):
        bring_gateway_target_in_line(control, _TID, _desired(), control.detail, **_NO_SLEEP)


def test_bring_in_line_raises_when_the_target_never_settles():
    control = _Control(_detail(_OLD_ARN), after_update=("UPDATING",))
    with pytest.raises(GatewayTargetUpdateError, match="still UPDATING"):
        bring_gateway_target_in_line(control, _TID, _desired(), control.detail,
                                     max_polls=3, **_NO_SLEEP)


# --------------------------------------------------------------------------- #
# In-app path (AgentManager)
# --------------------------------------------------------------------------- #

def _bare_manager() -> AgentManager:
    mgr = AgentManager.__new__(AgentManager)  # no __init__ side effects needed
    mgr.gateway_id = _GW
    return mgr


def _upsert(control: _Control, *, provider: str = _PROVIDER, runtime_arn: str = _NEW_ARN) -> str:
    mgr = _bare_manager()
    with patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=provider):
        return mgr._upsert_and_sync_gateway_target(
            control, _REGION, _SYSTEM, _SYSTEM, _endpoint(runtime_arn))


def test_in_app_matching_target_is_reused_without_update_or_sync():
    control = _Control(_detail(_NEW_ARN))
    assert _upsert(control) == _TID
    assert "update" not in control.calls
    assert control.synced == []


def test_in_app_target_naming_a_deleted_runtime_is_updated_then_synced():
    control = _Control(_detail(_OLD_ARN))
    assert _upsert(control) == _TID
    (sent,) = control.updates
    assert sent["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _endpoint(_NEW_ARN)
    assert sent["targetId"] == _TID and sent["name"] == _TARGET
    assert sent["credentialProviderConfigurations"] == _creds()
    assert control.synced == [[_TID]]
    assert control.deleted == []
    assert control.calls.index("update") < control.calls.index("sync")


def test_in_app_credential_mismatch_is_updated_then_synced():
    control = _Control(_detail(_NEW_ARN, creds=_creds(_OTHER_PROVIDER)))
    assert _upsert(control) == _TID
    (sent,) = control.updates
    assert sent["credentialProviderConfigurations"] == _creds()
    assert control.synced == [[_TID]]


def test_in_app_failed_update_raises_and_never_syncs_or_deletes():
    control = _Control(_detail(_OLD_ARN), update_error=RuntimeError("AccessDenied"))
    with pytest.raises(GatewayTargetUpdateError):
        _upsert(control)
    assert control.synced == [] and control.deleted == []


def test_in_app_unreadable_target_raises_instead_of_reusing_it():
    control = _Control(_detail(_OLD_ARN), get_error=RuntimeError("ThrottlingException"))
    with pytest.raises(GatewayTargetUpdateError, match="ThrottlingException"):
        _upsert(control)
    assert "update" not in control.calls and control.synced == []


def _cloud_manager() -> AgentManager:
    return AgentManager(
        mode="cloud", gateway_id=_GW, gateway_url="https://gw.example.com/mcp",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:hash",
        runtime_role_arn="arn:aws:iam::111122223333:role/adt-runtime",
    )


def _start(control: _Control, mgr: AgentManager) -> str:
    """start_agent through _start_cloud for a system whose runtime was just
    recreated (new ARN) while its old gateway target survived."""
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
            patch.object(AgentManager, "_ensure_credential_provider", return_value=_PROVIDER):
        return mgr.start_agent(system_id=_SYSTEM, system_name=_SYSTEM, schema="",
                               system_prompt="")


def test_reregistering_after_the_runtime_was_recreated_repoints_the_target():
    control = _Control(_detail(_OLD_ARN))
    mgr = _cloud_manager()
    assert _start(control, mgr) == "https://gw.example.com/mcp"
    assert control.detail["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _endpoint(
        _NEW_ARN)
    assert control.synced == [[_TID]]
    assert mgr._agents[_SYSTEM].gateway_target_id == _TID


def test_reregistering_registers_nothing_when_the_target_cannot_be_repointed():
    control = _Control(_detail(_OLD_ARN), after_update=("UPDATE_UNSUCCESSFUL",))
    mgr = _cloud_manager()
    with pytest.raises(GatewayTargetUpdateError):
        _start(control, mgr)
    assert _SYSTEM not in mgr._agents
    assert control.deleted == [] and control.synced == []


# --------------------------------------------------------------------------- #
# Provisioner Lambda path (handlers.create_target)
# --------------------------------------------------------------------------- #

def _target_input(arn: str = _NEW_ARN, provider: str = _PROVIDER) -> CreateTargetInput:
    return CreateTargetInput(system_id=_SYSTEM, system_name=_SYSTEM, gateway_id=_GW,
                             runtime_mcp_url=_endpoint(arn), region=_REGION,
                             credential_provider_arn=provider)


def test_provisioner_matching_target_is_reused_without_update():
    control = _Control(_detail(_NEW_ARN))
    res = create_target(_target_input(), control=control, **_NO_SLEEP)
    assert res.gateway_target_id == _TID
    assert "update" not in control.calls


def test_provisioner_target_naming_a_deleted_runtime_is_updated():
    control = _Control(_detail(_OLD_ARN, description="System agent for example_system"))
    res = create_target(_target_input(), control=control, **_NO_SLEEP)
    assert res.gateway_target_id == _TID
    (sent,) = control.updates
    assert sent["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _endpoint(_NEW_ARN)
    assert sent["credentialProviderConfigurations"] == _creds()
    assert sent["description"] == f"System agent for {_SYSTEM}"
    assert control.deleted == []


def test_provisioner_credential_mismatch_is_updated():
    control = _Control(_detail(_NEW_ARN, creds=_creds(_OTHER_PROVIDER)))
    create_target(_target_input(), control=control, **_NO_SLEEP)
    (sent,) = control.updates
    assert sent["credentialProviderConfigurations"] == _creds()


def test_provisioner_failed_update_is_a_typed_provisioner_error():
    control = _Control(_detail(_OLD_ARN), update_error=RuntimeError("AccessDeniedException"))
    with pytest.raises(TargetUpdateError, match="AccessDeniedException") as excinfo:
        create_target(_target_input(), control=control, **_NO_SLEEP)
    assert isinstance(excinfo.value, ProvisionerError)
    assert control.deleted == []
