# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The unsupported-AZ subnet learned by one runtime create is remembered for
every later create in the process.

Event 24 (2026-10-03): nine runtimes per fresh account (orchestrator, bridge,
seven systems) EACH went CREATE_FAILED on the same ``subnet-032bd572b5ee6b137``
("unsupported availability zone"), were deleted, and were recreated with the
subnet stripped: about a minute of provisioning per runtime spent re-learning
one fact. Offline: boto3 control client is a fake; sleeps are patched out.
"""
from __future__ import annotations

import pytest

import agents.agent_manager as am


class _Control:
    """create -> CREATE_FAILED (unsupported AZ) the first time a bad subnet is
    offered, READY otherwise; records every create's subnets."""

    BAD = "subnet-bad"

    def __init__(self):
        self.creates: list[list[str]] = []
        self._status: dict[str, str] = {}
        self._n = 0

    def create_agent_runtime(self, **kwargs):
        subnets = list(kwargs["networkConfiguration"]["networkModeConfig"]["subnets"])
        self.creates.append(subnets)
        self._n += 1
        rid = f"rt-{self._n}"
        self._status[rid] = "CREATE_FAILED" if self.BAD in subnets else "READY"
        return {"agentRuntimeArn": f"arn:{rid}", "agentRuntimeId": rid}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        status = self._status.get(agentRuntimeId)
        if status is None:
            raise RuntimeError("ResourceNotFoundException")
        out = {"status": status}
        if status == "CREATE_FAILED":
            out["failureReason"] = (f"Runtime cannot launch: unsupported availability zone "
                                    f"for {self.BAD}")
        return out

    def delete_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        self._status.pop(agentRuntimeId, None)
        return {}


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setenv("VPC_PRIVATE_SUBNETS", "subnet-good,subnet-bad")
    monkeypatch.setenv("AGENTCORE_RUNTIME_SG", "sg-1")
    monkeypatch.setattr(am.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(am, "_shared_container_uri", lambda uri: uri or "uri:1")
    monkeypatch.setattr(am.AgentManager, "_wait_runtime_deleted",
                        staticmethod(lambda *_a, **_k: True))
    am._forget_unsupported_subnets()
    return am.AgentManager(mode="cloud", gateway_id="gw", gateway_url="https://gw/mcp",
                           ecr_image_uri="uri:1", runtime_role_arn="arn:role")


def test_second_runtime_never_offers_the_subnet_the_first_one_stripped(mgr):
    control = _Control()
    arn1, _ = mgr._create_runtime_with_az_retry(control, "adt_erp", {}, None)
    arn2, _ = mgr._create_runtime_with_az_retry(control, "adt_mes", {}, None)
    assert arn1 and arn2
    # First runtime: one failed create with both subnets, one good retry.
    assert control.creates[0] == ["subnet-good", "subnet-bad"]
    assert control.creates[1] == ["subnet-good"]
    # Second runtime: the bad subnet is never offered, so exactly one create.
    assert control.creates[2:] == [["subnet-good"]]


def test_memory_is_per_process_and_resettable(mgr):
    control = _Control()
    mgr._create_runtime_with_az_retry(control, "adt_erp", {}, None)
    assert am.unsupported_subnets() == {"subnet-bad"}
    am._forget_unsupported_subnets()
    assert am.unsupported_subnets() == set()
    mgr._create_runtime_with_az_retry(control, "adt_plm", {}, None)
    assert control.creates[-2:] == [["subnet-good", "subnet-bad"], ["subnet-good"]]
