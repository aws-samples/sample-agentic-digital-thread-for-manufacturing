# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An updated gateway target is accepted only once the update verifiably landed.

``bring_gateway_target_in_line`` used to return as soon as a read-back
status was outside a short "transient" list — ``DELETING``, any
``*_PENDING_AUTH``, ``SYNCHRONIZE_UNSUCCESSFUL`` and a READY read-back still
carrying the OLD endpoint were all taken as success, so callers synced (and
the in-app path registered) a target still pointing at a deleted runtime.
These tests pin:

* success only on a ``READY`` read-back with NO drift against what was sent;
* a stale ``READY`` read-back is polled again within the budget, then raises;
* every going/failed/unsuccessful/pending-auth status raises at once;
* a read-back call that fails raises the typed error;
* the full-replacement update carries an existing ``privateEndpoint`` (deep
  copied) and still validates against the botocore model.

All fixtures are synthetic; no AWS call is made.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import pytest  # noqa: E402

from agents.runtime_provision_helpers import (  # noqa: E402
    GatewayTargetUpdateError,
    bring_gateway_target_in_line,
    gateway_target_update_kwargs,
)

_GW = "gw-example-111"
_TID = "tid-example-system"
_OLD = ("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore"
        "%3Aus-east-1%3A111122223333%3Aruntime%2Fadt_example-OLD/invocations?qualifier=DEFAULT")
_NEW = _OLD.replace("adt_example-OLD", "adt_example-NEW")
_PRIVATE = {"managedVpcResource": {
    "vpcIdentifier": "vpc-0example", "subnetIds": ["subnet-0aa", "subnet-0bb"],
    "endpointIpAddressType": "IPV4", "securityGroupIds": ["sg-0example"]}}


def _detail(endpoint: str = _OLD, status: str = "READY", **extra) -> dict:
    detail = {"targetId": _TID, "name": "example-system", "status": status,
              "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}}}
    detail.update(extra)
    return detail


def _desired(endpoint: str = _NEW) -> dict:
    return {"gatewayIdentifier": _GW, "name": "example-system",
            "description": "System agent for example",
            "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}}}


class _ReadBacks:
    """Control whose read-backs after the update are scripted: each entry is
    a ``(status, endpoint)`` pair (the last one repeats), or an Exception."""

    def __init__(self, *read_backs):
        self._read_backs = list(read_backs)
        self.updates: list[dict] = []
        self.gets = 0

    def update_gateway_target(self, **kwargs):
        self.updates.append(copy.deepcopy(kwargs))
        return {"status": "UPDATING"}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.gets += 1
        item = self._read_backs.pop(0) if len(self._read_backs) > 1 else self._read_backs[0]
        if isinstance(item, Exception):
            raise item
        status, endpoint = item
        return _detail(endpoint, status)


def _run(control, max_polls: int = 6):
    sleeps: list[float] = []
    result = bring_gateway_target_in_line(control, _TID, _desired(), _detail(),
                                          sleep=sleeps.append, poll_seconds=5.0,
                                          max_polls=max_polls)
    return result, sleeps


# --------------------------------------------------------------------------- #
# H1: accept only READY with no drift
# --------------------------------------------------------------------------- #

def test_ready_read_back_with_the_new_endpoint_is_accepted():
    result, sleeps = _run(_ReadBacks(("READY", _NEW)))
    assert result["status"] == "READY" and sleeps == []


def test_a_stale_ready_read_back_is_polled_until_the_update_shows():
    control = _ReadBacks(("READY", _OLD), ("UPDATING", _NEW), ("READY", _NEW))
    result, sleeps = _run(control)
    assert result["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _NEW
    assert control.gets == 3 and sleeps == [5.0, 5.0]


def test_a_ready_read_back_that_never_shows_the_update_raises_at_the_budget():
    control = _ReadBacks(("READY", _OLD))
    with pytest.raises(GatewayTargetUpdateError, match="still READY but still reads back a "
                                                       "different endpoint"):
        _run(control, max_polls=3)
    assert control.gets == 4


@pytest.mark.parametrize("status", [
    "DELETING", "FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL",
    "CREATE_PENDING_AUTH", "UPDATE_PENDING_AUTH", "SYNCHRONIZE_PENDING_AUTH",
])
def test_a_status_that_means_the_update_did_not_take_raises_at_once(status):
    control = _ReadBacks((status, _NEW))
    with pytest.raises(GatewayTargetUpdateError, match=status):
        _run(control)
    assert control.gets == 1


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "SYNCHRONIZING", "", "NEW_STATE"])
def test_other_statuses_are_polled_never_accepted(status):
    control = _ReadBacks((status, _NEW))
    with pytest.raises(GatewayTargetUpdateError, match="still "):
        _run(control, max_polls=2)
    assert control.gets == 3


def test_a_failed_read_back_is_the_typed_error():
    control = _ReadBacks(RuntimeError("ThrottlingException"))
    with pytest.raises(GatewayTargetUpdateError, match="ThrottlingException"):
        _run(control)


# --------------------------------------------------------------------------- #
# M4: privateEndpoint survives the full-replacement update
# --------------------------------------------------------------------------- #

def test_update_kwargs_carry_a_deep_copy_of_the_private_endpoint():
    detail = _detail(privateEndpoint=_PRIVATE)
    kwargs = gateway_target_update_kwargs(_TID, _desired(), detail)
    assert kwargs["privateEndpoint"] == _PRIVATE
    kwargs["privateEndpoint"]["managedVpcResource"]["subnetIds"].append("subnet-0cc")
    assert detail["privateEndpoint"]["managedVpcResource"]["subnetIds"] == [
        "subnet-0aa", "subnet-0bb"]


def test_update_kwargs_without_a_private_endpoint_send_none():
    assert "privateEndpoint" not in gateway_target_update_kwargs(_TID, _desired(), _detail())


def test_update_kwargs_with_a_private_endpoint_validate_against_botocore():
    botocore_session = pytest.importorskip("botocore.session")
    from botocore.validate import validate_parameters

    detail = _detail(privateEndpoint=_PRIVATE,
                     metadataConfiguration={"allowedRequestHeaders": ["x-example"]})
    kwargs = gateway_target_update_kwargs(_TID, _desired(), detail)
    model = botocore_session.get_session().get_service_model("bedrock-agentcore-control")
    validate_parameters(kwargs, model.operation_model("UpdateGatewayTarget").input_shape)


def test_the_sent_update_carries_the_private_endpoint():
    control = _ReadBacks(("READY", _NEW))
    bring_gateway_target_in_line(control, _TID, _desired(), _detail(privateEndpoint=_PRIVATE),
                                 sleep=lambda _s: None)
    assert control.updates[0]["privateEndpoint"] == _PRIVATE
