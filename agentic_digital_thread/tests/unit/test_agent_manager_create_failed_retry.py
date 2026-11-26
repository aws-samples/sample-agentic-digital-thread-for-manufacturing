# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A runtime create that fails for a reason other than an unsupported AZ is
retried, and a CREATE_FAILED runtime is never reused.

Event 34 (2026-10-18), one of three fresh accounts: both built-in runtimes
(adt_digital_thread, adt_query_bridge) went CREATE_FAILED with the service's
own "Network config provisioning/deletion failed for DEFAULT. Reason:
InternalError" about five minutes after creation. The sibling accounts'
identical creates went READY at the same mark. The old code raised on any
non-AZ failure, polled for only 300 s, and on a task restart picked the
CREATE_FAILED runtime up as "existing" (which would have synced a gateway
target at a dead endpoint). Offline: fake control client, sleeps patched out.
"""
from __future__ import annotations

import pytest

import agents.agent_manager as am

TRANSIENT = "Network config provisioning/deletion failed for DEFAULT. Reason: InternalError"


class _Control:
    """create -> the status scripted for that create (by order); records
    creates and deletes."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.creates: list[dict] = []
        self.deletes: list[str] = []
        self._status: dict[str, str] = {}

    def create_agent_runtime(self, **kwargs):
        self.creates.append(kwargs)
        rid = f"rt-{len(self.creates)}"
        self._status[rid] = self.statuses.pop(0) if self.statuses else "READY"
        return {"agentRuntimeArn": f"arn:{rid}", "agentRuntimeId": rid}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        status = self._status.get(agentRuntimeId)
        if status is None:
            raise RuntimeError("ResourceNotFoundException")
        out = {"status": status}
        if status == "CREATE_FAILED":
            out["failureReason"] = TRANSIENT
        return out

    def delete_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        self.deletes.append(agentRuntimeId)
        self._status.pop(agentRuntimeId, None)
        return {}


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setenv("VPC_PRIVATE_SUBNETS", "subnet-a,subnet-b")
    monkeypatch.setenv("AGENTCORE_RUNTIME_SG", "sg-1")
    monkeypatch.setattr(am.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(am, "_shared_container_uri", lambda uri: uri or "uri:1")
    monkeypatch.setattr(am.AgentManager, "_wait_runtime_deleted",
                        staticmethod(lambda *_a, **_k: True))
    am._forget_unsupported_subnets()
    return am.AgentManager(mode="cloud", gateway_id="gw", gateway_url="https://gw/mcp",
                           ecr_image_uri="uri:1", runtime_role_arn="arn:role")


def test_transient_create_failed_is_deleted_and_recreated(mgr):
    control = _Control(["CREATE_FAILED", "READY"])
    arn, rid = mgr._create_runtime_with_az_retry(control, "adt_query_bridge", {}, None)
    assert (arn, rid) == ("arn:rt-2", "rt-2")
    assert control.deletes == ["rt-1"]  # the failed one is gone before the retry
    # Same subnets both times: nothing was stripped for a non-AZ reason.
    subnets = [c["networkConfiguration"]["networkModeConfig"]["subnets"] for c in control.creates]
    assert subnets == [["subnet-a", "subnet-b"], ["subnet-a", "subnet-b"]]
    assert am.unsupported_subnets() == set()


def test_transient_retry_pauses_between_attempts(mgr, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(am.time, "sleep", lambda s, *_a, **_k: slept.append(s))
    control = _Control(["CREATE_FAILED", "CREATE_FAILED", "READY"])
    mgr._create_runtime_with_az_retry(control, "adt_digital_thread", {}, None)
    assert len(control.creates) == 3
    # The per-attempt pauses, in order (the 5 s poll cadence never fires here
    # because every poll answers on its first read).
    pauses = [s for s in slept if s in am.RUNTIME_CREATE_RETRY_DELAYS_S]
    assert pauses == list(am.RUNTIME_CREATE_RETRY_DELAYS_S[:2])


def test_last_attempt_failure_raises_with_the_reason(mgr):
    control = _Control(["CREATE_FAILED"] * am.RUNTIME_CREATE_ATTEMPTS)
    with pytest.raises(RuntimeError, match="InternalError"):
        mgr._create_runtime_with_az_retry(control, "adt_query_bridge", {}, None)
    assert len(control.creates) == am.RUNTIME_CREATE_ATTEMPTS
    # Every failed runtime but the last is cleaned up; the last stays for a
    # human to read its failureReason.
    assert control.deletes == [f"rt-{i}" for i in range(1, am.RUNTIME_CREATE_ATTEMPTS)]


def test_ready_deadline_covers_a_cold_account(monkeypatch, mgr):
    """A runtime still CREATING at 300 s is waited for, not abandoned."""
    now = [1000.0]
    monkeypatch.setattr(am.time, "time", lambda: now[0])

    class _Slow:
        reads = 0

        def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
            self.reads += 1
            now[0] += 100  # each poll costs 100 s of wall clock
            return {"status": "READY" if now[0] >= 1450 else "CREATING"}

    slow = _Slow()
    retry, _, _ = mgr._poll_new_runtime(slow, "rt-x", ["subnet-a"], 0)
    assert retry is False
    assert slow.reads == 5  # READY at t+450, past the old 300 s deadline
    assert am.RUNTIME_READY_TIMEOUT_S >= 600


def test_unsupported_az_still_strips_the_subnet(mgr):
    """The AZ branch is unchanged: the named subnet is stripped and remembered."""
    control = _Control(["CREATE_FAILED", "READY"])
    control.get_agent_runtime = lambda agentRuntimeId=None: (  # noqa: E731
        {"status": "READY"} if agentRuntimeId == "rt-2" else
        {"status": "CREATE_FAILED",
         "failureReason": "Runtime cannot launch: unsupported availability zone for subnet-b"})
    mgr._create_runtime_with_az_retry(control, "adt_erp", {}, None)
    subnets = [c["networkConfiguration"]["networkModeConfig"]["subnets"] for c in control.creates]
    assert subnets == [["subnet-a", "subnet-b"], ["subnet-a"]]
    assert am.unsupported_subnets() == {"subnet-b"}


class _Listing:
    def __init__(self, runtimes):
        self.runtimes = runtimes
        self.deletes: list[str] = []

    def list_agent_runtimes(self, **_kw):
        return {"agentRuntimes": list(self.runtimes)}

    def delete_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        self.deletes.append(agentRuntimeId)
        return {}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        raise RuntimeError("ResourceNotFoundException")


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED"])
def test_failed_runtime_is_never_reused(mgr, status):
    control = _Listing([{"agentRuntimeName": "adt_query_bridge", "agentRuntimeId": "rt-dead",
                         "agentRuntimeArn": "arn:rt-dead", "status": status}])
    arn, rid = mgr._find_reusable_runtime(control, "adt_query_bridge", None)
    assert (arn, rid) == (None, None)  # caller creates afresh
    assert control.deletes == ["rt-dead"]


def test_ready_runtime_is_still_reused(mgr):
    control = _Listing([{"agentRuntimeName": "adt_erp", "agentRuntimeId": "rt-ok",
                         "agentRuntimeArn": "arn:rt-ok", "status": "READY"}])
    assert mgr._find_reusable_runtime(control, "adt_erp", None) == ("arn:rt-ok", "rt-ok")
    assert control.deletes == []
