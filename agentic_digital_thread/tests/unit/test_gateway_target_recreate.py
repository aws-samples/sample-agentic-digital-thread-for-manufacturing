# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Recreating a non-READY gateway target never leaves a system registered
against a target that is not there.

On ConflictException the in-app path deletes a non-READY target and creates
it again. It used to swallow every failure in that sequence and hand back the
OLD target id, so a delete that went through followed by a failed create left
the system registered (and "synced") against a deleted target. These tests
pin, for ``AgentManager._create_or_reuse_gateway_target``:

* delete + create both succeed: the NEW target is returned and synced;
* delete succeeds, create fails (or the old target never goes, so the create
  conflicts): :class:`GatewayTargetRecreateError`, no sync, nothing
  registered, and a retry once the name is free registers the system;
* the delete fails: the old target is kept (and synced, as before) only when it
  still exists, is not DELETING and has a readable MCP endpoint; gone,
  DELETING or unreadable raises the same typed error.

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
from agents.runtime_provision_helpers import (  # noqa: E402
    GatewayTargetRecreateError,
    GatewayTargetUpdateError,
)

_REGION = "us-east-1"
_GW = "gw-example-111"
_SYSTEM = "example_system"
_TARGET = "example-system"
_OLD_TID = "tid-old"
_NEW_TID = "tid-new"
_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_example_system-NEW0000002"


def _endpoint() -> str:
    return AgentManager._runtime_mcp_url(_RUNTIME_ARN, _REGION)


class _ConflictException(Exception):
    pass


class _RecreateControl:
    """bedrock-agentcore-control stand-in holding ONE non-READY target.

    ``on_delete`` is what a ``delete_gateway_target`` call does to it —
    ``"remove"`` (gone at once), ``"deleting"`` (lingers DELETING forever) or
    ``"keep"`` (untouched) — and it happens whether or not the call then
    raises ``delete_error``. A create conflicts while any target holds the
    name; otherwise it raises ``create_error`` or creates the new target."""

    class exceptions:  # noqa: N801 - boto3 shape
        ConflictException = _ConflictException

    def __init__(self, *, status: str = "SYNCHRONIZE_UNSUCCESSFUL", on_delete: str = "remove",
                 delete_error: Exception | None = None,
                 create_error: Exception | None = None, readable: bool = True):
        self.target: dict | None = {"name": _TARGET, "targetId": _OLD_TID, "status": status}
        if readable:
            self.target["targetConfiguration"] = {
                "mcp": {"mcpServer": {"endpoint": _endpoint()}}}
        self.on_delete = on_delete
        self.delete_error = delete_error
        self.create_error = create_error
        self.calls: list[str] = []
        self.synced: list[list[str]] = []

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        self.calls.append("list")
        if self.target is None:
            return {"items": []}
        return {"items": [{k: self.target[k] for k in ("name", "targetId", "status")}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.calls.append("get")
        if self.target is None or self.target["targetId"] != targetId:
            raise RuntimeError("ResourceNotFoundException")
        return copy.deepcopy(self.target)

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.calls.append("delete")
        if self.on_delete == "remove":
            self.target = None
        elif self.on_delete == "deleting" and self.target is not None:
            self.target["status"] = "DELETING"
        if self.delete_error is not None:
            raise self.delete_error
        return {}

    def create_gateway_target(self, **kwargs):
        self.calls.append("create")
        if self.target is not None:
            raise _ConflictException("target exists")
        if self.create_error is not None:
            raise self.create_error
        self.target = {"name": kwargs["name"], "targetId": _NEW_TID, "status": "CREATING",
                       "targetConfiguration": copy.deepcopy(kwargs["targetConfiguration"])}
        return {"targetId": _NEW_TID}

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):  # noqa: N803
        self.calls.append("sync")
        self.synced.append(list(targetIdList))
        return {}


def _bare_manager() -> AgentManager:
    mgr = AgentManager.__new__(AgentManager)  # no __init__ side effects needed
    mgr.gateway_id = _GW
    return mgr


def _upsert(control: _RecreateControl) -> str:
    with patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=""):
        return _bare_manager()._upsert_and_sync_gateway_target(
            control, _REGION, _SYSTEM, _SYSTEM, _endpoint())


# --------------------------------------------------------------------------- #
# The delete goes through
# --------------------------------------------------------------------------- #

def test_recreated_target_is_returned_and_synced():
    control = _RecreateControl()
    assert _upsert(control) == _NEW_TID
    assert control.synced == [[_NEW_TID]]


def test_failed_create_after_a_delete_raises_and_never_syncs():
    control = _RecreateControl(create_error=RuntimeError("ValidationException"))
    with pytest.raises(GatewayTargetRecreateError, match="ValidationException") as excinfo:
        _upsert(control)
    assert isinstance(excinfo.value, GatewayTargetUpdateError)  # same contract as the update path
    assert control.synced == []
    assert control.target is None  # nothing pretends the old target is still there


def test_a_target_that_never_goes_makes_the_create_conflict_and_raise():
    control = _RecreateControl(on_delete="deleting")
    with pytest.raises(GatewayTargetRecreateError):
        _upsert(control)
    assert control.synced == []


# --------------------------------------------------------------------------- #
# The delete itself fails
# --------------------------------------------------------------------------- #

def test_undeletable_target_that_still_exists_with_an_endpoint_is_synced_as_before():
    control = _RecreateControl(on_delete="keep", delete_error=RuntimeError("AccessDenied"))
    assert _upsert(control) == _OLD_TID
    assert control.synced == [[_OLD_TID]]
    assert "create" not in control.calls[control.calls.index("delete"):]


def test_failed_delete_of_a_target_that_is_gone_anyway_raises():
    control = _RecreateControl(on_delete="remove", delete_error=RuntimeError("ThrottlingException"))
    with pytest.raises(GatewayTargetRecreateError, match="could not read it back"):
        _upsert(control)
    assert control.synced == []


def test_failed_delete_of_a_deleting_target_raises():
    control = _RecreateControl(on_delete="deleting",
                               delete_error=RuntimeError("ConflictException: already deleting"))
    with pytest.raises(GatewayTargetRecreateError, match="DELETING"):
        _upsert(control)
    assert control.synced == []


def test_failed_delete_of_a_target_without_a_readable_endpoint_raises():
    control = _RecreateControl(on_delete="keep", delete_error=RuntimeError("AccessDenied"),
                               readable=False)
    with pytest.raises(GatewayTargetRecreateError, match="readable endpoint"):
        _upsert(control)
    assert control.synced == []


# --------------------------------------------------------------------------- #
# Through start_agent: nothing registered, and a retry recovers
# --------------------------------------------------------------------------- #

def _cloud_manager() -> AgentManager:
    return AgentManager(
        mode="cloud", gateway_id=_GW, gateway_url="https://gw.example.com/mcp",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:hash",
        runtime_role_arn="arn:aws:iam::111122223333:role/adt-runtime",
    )


def _start(control: _RecreateControl, mgr: AgentManager) -> str:
    with patch.dict("os.environ", {"AWS_REGION": _REGION}), \
            patch("boto3.client", lambda *a, **k: control), \
            patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_cloud_fast_path_ready", return_value=False), \
            patch.object(AgentManager, "_apply_business_context",
                         side_effect=lambda _sid, cfg, _region: cfg), \
            patch.object(AgentManager, "_prepare_runtime_env", return_value={}), \
            patch.object(AgentManager, "_m2m_authorizer_config", return_value=None), \
            patch.object(AgentManager, "_find_reusable_runtime",
                         return_value=(_RUNTIME_ARN, "adt_example_system-NEW0000002")), \
            patch.object(AgentManager, "_refresh_existing_runtime_image", return_value=None), \
            patch.object(AgentManager, "_warmup_runtime", return_value=None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=""):
        return mgr.start_agent(system_id=_SYSTEM, system_name=_SYSTEM, schema="",
                               system_prompt="")


def test_failed_recreate_registers_nothing_and_a_retry_registers_the_new_target():
    control = _RecreateControl(create_error=RuntimeError("ServiceQuotaExceededException"))
    mgr = _cloud_manager()
    with pytest.raises(GatewayTargetRecreateError):
        _start(control, mgr)
    assert _SYSTEM not in mgr._agents
    assert control.synced == []

    control.create_error = None  # the transient cause clears; the name is free
    assert _start(control, mgr) == "https://gw.example.com/mcp"
    assert mgr._agents[_SYSTEM].gateway_target_id == _NEW_TID
    assert control.synced == [[_NEW_TID]]
