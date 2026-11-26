# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared synthetic fakes + run harnesses for the gateway-target reset tests.

Split out of the former single ``test_reset_target_synchronizing.py`` (which
exceeded the 800-line file limit). Three focused test files import from here:

  * ``test_reset_target_classifier.py``  — the onboard reset-target
    ``_accept_target_delete`` outcomes + ``classify_gateway_target_delete``;
  * ``test_reset_target_head_start.py``  — ``AgentManager`` head-start helper +
    ``delete_cloud_agent`` resource ordering;
  * ``test_reset_target_cleanup.py``     — single-reset / reset-all cleanup
    ordering + shared-deadline accounting.

All fixtures are synthetic — no live AWS/Neptune/HTTP. A virtual clock replaces
real time so old-code replays fail FAST by assertion rather than busy-waiting a
real 60/180/900 s deadline.

OLD-CODE PROOF TAXONOMY (replay against the code before the fix, and against
the version with only the transitional-retry fix):
Most tests fail on old code by a BEHAVIOURAL assertion (wrong resource order,
retried a permanent error, multiplied the deadline). A handful instead fail by
TypeError/ImportError because they exercise the NEW function SHAPE — the
``deadline_at`` keyword / 3-tuple return of ``_reset_delete_gateway_target`` and
the 3-argument ``classify_gateway_target_delete``. Those are marked ``API-SHAPE``
in their docstrings: change-detectors for the new signatures, not proofs of old
runtime behaviour. The SIGNATURE-STABLE behavioural proof of the onboard reset
path is ``test_reset_cleanup_retries_synchronizing_then_completes`` in
``test_reset_target_cleanup.py`` (calls ``_reset_cleanup_agentcore_sync`` with
its unchanged ``(sid, deadline_s)`` signature); it fails on the code before
the fix by behaviour (one delete attempt, reported incomplete). The head-start
twin ``test_head_start_synchronizing_is_retried_until_accepted`` likewise
fails there by behaviour.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.agent_manager import AgentManager  # noqa: E402


GW = "gw-test-123"
SID = "prompt-lib-244"
TID = "tid-prompt-lib-244"


class _ClientError(Exception):
    """Minimal botocore ClientError shape: carries ``response['Error']['Code']``,
    which ``gateway_delete_error_code`` reads, plus the message text."""

    def __init__(self, code: str, message: str):
        super().__init__(f"An error occurred ({code}) when calling the "
                         f"DeleteGatewayTarget operation: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def synchronizing_err() -> _ClientError:
    # ValidationException is a RETRYABLE control-plane code; the message/state
    # says the target is still settling.
    return _ClientError(
        "ValidationException",
        "DeleteGatewayTarget operation can't be performed on target when it is "
        "in Synchronizing state. Retry the request when the target is in a "
        "terminal state.")


def deleting_err() -> _ClientError:
    return _ClientError("ValidationException", "target is already in Deleting state.")


def access_denied_err() -> _ClientError:
    # A permanent, NON-retryable code — must be "other" even if a separate
    # status read says SYNCHRONIZING.
    return _ClientError("AccessDeniedException",
                        "not authorized to perform bedrock-agentcore:DeleteGatewayTarget")


def validation_nonstate_err() -> _ClientError:
    # A RETRYABLE code (ValidationException) but a request-defect message with NO
    # settling-state reason — must be "other", not retried even if status reads
    # SYNCHRONIZING.
    return _ClientError("ValidationException",
                        "1 validation error: targetId must match pattern [a-z0-9-]+")


def conflict_generic_err() -> _ClientError:
    # A RETRYABLE code (ConflictException) with no settling-state reason — "other".
    return _ClientError("ConflictException", "a conflicting operation is in progress")


class _ResourceNotFound(Exception):
    """boto3-shaped ResourceNotFoundException for a gone target."""


# ── Injected clock: no real time passes; sleep advances a virtual monotonic ──
class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TargetControl:
    """Fake bedrock-agentcore-control for ONE gateway target named ``SID``.

    ``delete_errors`` is a list of exceptions to raise on successive
    ``delete_gateway_target`` calls; once exhausted the delete is ACCEPTED and
    the target is marked gone (dropped from list / get raises). ``status`` is
    what ``get_gateway_target`` reports until the accepted delete. After the
    accepted delete, ``list`` shows no target and ``get`` raises NotFound.
    """

    class exceptions:  # noqa: N801 - boto3 client shape
        ResourceNotFoundException = _ResourceNotFound

    def __init__(self, delete_errors=None, status="SYNCHRONIZING"):
        self.delete_errors = list(delete_errors or [])
        self.status = status
        self.deleted = False
        self.delete_calls = 0
        self.list_calls = 0
        self.get_calls = 0

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        self.list_calls += 1
        if self.deleted:
            return {"items": []}
        return {"items": [{"name": SID, "targetId": TID, "status": self.status}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.get_calls += 1
        if self.deleted:
            raise _ResourceNotFound("target gone")
        return {"name": SID, "targetId": TID, "status": self.status}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.delete_calls += 1
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        self.deleted = True
        return {}


def run_reset_target(control, *, deadline_s=30.0):
    """Drive ``onboard._reset_delete_gateway_target`` with the injected clock.

    API-SHAPE: passes the new ``deadline_at`` keyword and unpacks the new
    3-tuple return. With only the transitional-retry fix in place every test
    routed through here fails by TypeError (the keyword / arity did not exist
    yet); before any fix the helper had a different shape too.
    Change-detectors for the signature, not behavioural old-code proofs — the
    behavioural proof of the reset PATH is the cleanup-entry-point test."""
    clock = Clock()
    _real_wait = onboard._wait_targets_absent

    def _wait(control_, gateway_id, names, *, deadline_s=deadline_s, poll_s=3.0):
        # Bind the injected clock: the real helper's sleep/monotonic defaults are
        # captured at import time, so patching onboard.time alone would not reach
        # them. Delegates to the real paging/absence logic otherwise.
        return _real_wait(control_, gateway_id, names, deadline_s=deadline_s,
                          poll_s=poll_s, sleep=clock.sleep, monotonic=clock.monotonic)

    with patch.object(onboard.time, "sleep", clock.sleep), \
            patch.object(onboard.time, "monotonic", clock.monotonic), \
            patch.object(onboard, "_wait_targets_absent", _wait):
        awaited, timed_out, delete_failed = onboard._reset_delete_gateway_target(
            control, GW, SID, deadline_at=clock.monotonic() + deadline_s)
        return (awaited, timed_out, delete_failed), clock


def run_head_start(control):
    """Drive ``AgentManager._delete_gateway_target_and_wait`` with the injected
    clock in AgentManager's wall clock (time.time) AND sleep, so old code's 60 s
    busy-wait fails FAST by assertion rather than taking real time."""
    clock = Clock()
    with patch("agents.agent_manager.time.sleep", clock.sleep), \
            patch("agents.agent_manager.time.time", clock.monotonic), \
            patch("agents.agent_manager.time.monotonic", clock.monotonic):
        outcome = AgentManager._delete_gateway_target_and_wait(control, GW, TID, SID)
    return outcome, clock


def clocked_cleanup(fn, *args, deadline_s=None):
    """Run a cleanup entry point with ONE virtual clock injected into
    ``onboard.time`` AND into BOTH absence waiters (``_wait_targets_absent`` /
    ``_wait_runtimes_absent``) — which captured the real ``sleep``/``monotonic``
    at import, so patching ``onboard.time`` alone would not reach them. Returns
    ``(result, clock)``. ``fn`` is called with ``*args`` (+ ``deadline_s`` when
    given)."""
    clock = Clock()
    rt, tg = onboard._wait_runtimes_absent, onboard._wait_targets_absent

    def _wt(c, g, names, *, deadline_s=0.0, poll_s=3.0):
        return tg(c, g, names, deadline_s=deadline_s, poll_s=poll_s,
                  sleep=clock.sleep, monotonic=clock.monotonic)

    def _wr(c, names, *, deadline_s=0.0, poll_s=3.0):
        return rt(c, names, deadline_s=deadline_s, poll_s=poll_s,
                  sleep=clock.sleep, monotonic=clock.monotonic)

    call_args = list(args) + ([deadline_s] if deadline_s is not None else [])
    with patch.object(onboard.time, "sleep", clock.sleep), \
            patch.object(onboard.time, "monotonic", clock.monotonic), \
            patch.object(onboard, "_wait_targets_absent", _wt), \
            patch.object(onboard, "_wait_runtimes_absent", _wr):
        return fn(*call_args), clock


def run_single_cleanup(control, *, deadline_s=10.0, env_gw="gw-x"):
    with patch("boto3.client", lambda *a, **k: control), \
            patch.dict(os.environ, {"AGENTCORE_GATEWAY_ID": env_gw, "AWS_REGION": "us-east-1"}):
        return clocked_cleanup(onboard._reset_cleanup_agentcore_sync, control.sid,
                               deadline_s=deadline_s)


def run_reset_all(control):
    with patch("boto3.client", lambda *a, **k: control), \
            patch.dict(os.environ, {"AGENTCORE_GATEWAY_ID": "gw-x", "AWS_REGION": "us-east-1"}):
        return clocked_cleanup(onboard._global_reset_cleanup_agentcore_sync)


class CleanupControl:
    """Single-system cleanup control: ONE target whose delete always raises
    ``error``, plus ONE present runtime. The runtime would be deleted if the
    cleanup did not stop first. Records calls."""

    def __init__(self, error, sid="sys-a", tstatus="READY"):
        self.error = error
        self.sid = sid
        self.rname = f"adt_{sid.replace('-', '_')}"
        self.tstatus = tstatus
        self.delete_runtime_calls = 0
        self.delete_target_calls = 0

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        return {"items": [{"name": self.sid, "targetId": f"tid-{self.sid}",
                           "status": self.tstatus}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        return {"name": self.sid, "targetId": targetId, "status": self.tstatus}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.delete_target_calls += 1
        raise self.error

    def list_agent_runtimes(self, nextToken=None):  # noqa: N803
        return {"agentRuntimes": [{"agentRuntimeName": self.rname,
                                   "agentRuntimeArn": f"arn:x/runtime/{self.rname}-id"}]}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self.delete_runtime_calls += 1


class AcceptedControl:
    """Single-system cleanup control where the target delete is ACCEPTED but the
    target NEVER leaves the listing (``never_absent``) — or the absence listing
    RAISES (``list_raises``). The runtime exists and would be deleted if the
    cleanup did not gate on verified absence."""

    def __init__(self, sid="sys-a", never_absent=True, list_raises=False):
        self.sid = sid
        self.rname = f"adt_{sid.replace('-', '_')}"
        self.never_absent = never_absent
        self.list_raises = list_raises
        self.deleted = False
        self.delete_runtime_calls = 0
        self._first_list_done = False

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        # The first listing (the lookup) always succeeds so the delete is tried.
        if self._first_list_done and self.list_raises:
            raise RuntimeError("ThrottlingException while listing targets")
        self._first_list_done = True
        if self.deleted and not self.never_absent:
            return {"items": []}
        return {"items": [{"name": self.sid, "targetId": f"tid-{self.sid}", "status": "READY"}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        return {"name": self.sid, "targetId": targetId, "status": "READY"}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.deleted = True  # accepted
        return {}

    def list_agent_runtimes(self, nextToken=None):  # noqa: N803
        return {"agentRuntimes": [{"agentRuntimeName": self.rname,
                                   "agentRuntimeArn": f"arn:x/runtime/{self.rname}-id"}]}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self.delete_runtime_calls += 1


class RetryThenAcceptControl:
    """Single-system cleanup control: the target delete is refused with a
    SYNCHRONIZING ValidationException for the first ``refusals`` calls, then
    ACCEPTED; after acceptance the target disappears from the listing and the
    runtime is deleted and disappears. Used to prove the reset PATH retries the
    live bug and completes (signature-stable, through the cleanup entry point)."""

    def __init__(self, sid="sys-a", refusals=2):
        self.sid = sid
        self.rname = f"adt_{sid.replace('-', '_')}"
        self.refusals = refusals
        self.delete_calls = 0
        self._accepted = False
        self._runtime_deleted = False
        self.deleted_runtimes: list[str] = []

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        if self._accepted:
            return {"items": []}
        return {"items": [{"name": self.sid, "targetId": f"tid-{self.sid}",
                           "status": "SYNCHRONIZING"}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        if self._accepted:
            raise _ResourceNotFound("gone")
        return {"name": self.sid, "targetId": targetId, "status": "SYNCHRONIZING"}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.delete_calls += 1
        if self.delete_calls <= self.refusals:
            raise synchronizing_err()
        self._accepted = True
        return {}

    def list_agent_runtimes(self, nextToken=None):  # noqa: N803
        if self._runtime_deleted:
            return {"agentRuntimes": []}
        return {"agentRuntimes": [{"agentRuntimeName": self.rname,
                                   "agentRuntimeArn": f"arn:x/runtime/{self.rname}-id"}]}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self._runtime_deleted = True
        self.deleted_runtimes.append(agentRuntimeId)


class ResetAllControl:
    """Reset-all control-plane: data targets + data runtimes (plus a preserved
    built-in). The named ``fail_targets`` always raise ``error`` on delete;
    ``never_absent`` targets accept the delete but never leave the listing;
    everything else is deleted and disappears. ``list_raises_after`` makes
    list_gateway_targets raise after N successful calls. Records runtime deletes
    and the order of target delete attempts."""

    def __init__(self, targets, runtimes, *, fail_targets=(), error=None,
                 never_absent=(), list_raises_after=None):
        self._t = {n: {"name": n, "targetId": f"tid-{n}", "status": "READY"} for n in targets}
        self._r = {n: {"agentRuntimeName": n, "agentRuntimeArn": f"arn:x/{n}-id"} for n in runtimes}
        self.fail_targets = set(fail_targets)
        self.error = error
        self.never_absent = set(never_absent)
        self.list_raises_after = list_raises_after
        self.list_calls = 0
        self.deleted_runtimes: list[str] = []
        self.delete_attempts: list[str] = []

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        self.list_calls += 1
        if self.list_raises_after is not None and self.list_calls > self.list_raises_after:
            raise RuntimeError("ThrottlingException while listing targets")
        return {"items": [dict(t) for t in self._t.values()]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        for t in self._t.values():
            if t["targetId"] == targetId:
                return dict(t)
        raise _ResourceNotFound("gone")

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        name = next((n for n, t in self._t.items() if t["targetId"] == targetId), None)
        self.delete_attempts.append(name)
        if name in self.fail_targets:
            raise self.error
        if name not in self.never_absent:
            self._t.pop(name, None)  # accepted and disappears

    def list_agent_runtimes(self, nextToken=None):  # noqa: N803
        return {"agentRuntimes": [dict(r) for r in self._r.values()]}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self.deleted_runtimes.append(agentRuntimeId)


class AMControl:
    """AgentManager-path control for delete_cloud_agent. ONE target + ONE
    runtime. ``delete_error`` (if set) makes the target delete raise; otherwise
    it is accepted. ``deleting`` makes the first delete raise a Deleting-state
    error (service reports going-away). After acceptance/deleting,
    ``get_gateway_target`` stays READY/DELETING forever (``never_absent``),
    raises a NON-NotFound error (``get_raises``), or (default) reports gone."""

    def __init__(self, sid="plm", *, delete_error=None, never_absent=False,
                 get_raises=False, deleting=False):
        self.sid = sid
        self.tid = f"tid-{sid}"
        self.rid = f"adt_{sid}-id"
        self.delete_error = delete_error
        self.never_absent = never_absent
        self.get_raises = get_raises
        self.deleting = deleting
        self._issued = False
        self.get_calls = 0
        self.delete_runtime_calls = 0

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.get_calls += 1
        if self.get_raises:
            raise _ClientError("ThrottlingException", "slow down")
        if self._issued and not self.never_absent:
            raise _ResourceNotFound("gone")
        status = "DELETING" if self.deleting else "READY"
        return {"name": self.sid, "targetId": targetId, "status": status}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        if self.delete_error is not None:
            raise self.delete_error
        self._issued = True
        if self.deleting:
            # Service reports the target already going away.
            raise _ClientError("ValidationException", "target is already in Deleting state.")
        return {}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self.delete_runtime_calls += 1

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
        raise _ResourceNotFound("gone")


def run_delete_cloud_agent(control):
    """Drive AgentManager.delete_cloud_agent with ONE virtual clock injected into
    AgentManager's wall clock (time.time) AND sleep, so old code's 60/120 s
    busy-waits fail FAST by assertion rather than taking real time."""
    clock = Clock()
    mgr = AgentManager.__new__(AgentManager)
    mgr._agents = {}
    mgr.gateway_id = GW
    mgr.gateway_url = "https://gw.example.com/mcp"
    with patch("boto3.client", lambda *a, **k: control), \
            patch("agents.agent_manager.time.sleep", clock.sleep), \
            patch("agents.agent_manager.time.time", clock.monotonic), \
            patch("agents.agent_manager.time.monotonic", clock.monotonic), \
            patch.object(AgentManager, "_lookup_cloud_agent",
                         return_value=(control.rid, control.tid)):
        mgr.delete_cloud_agent(control.sid)
    return clock


class LingerControl:
    """Single-system control that models BOTH a target and a runtime that
    LINGER (stay listed) for a measured number of absence polls after an
    ACCEPTED delete, so the target-absence wait and the runtime-absence wait
    each consume a real, measurable part of the shared budget."""

    def __init__(self, sid, *, target_linger_polls, runtime_linger_polls):
        self.sid = sid
        self.rname = f"adt_{sid.replace('-', '_')}"
        self.t_linger = target_linger_polls
        self.r_linger = runtime_linger_polls
        self._t_present = True
        self._r_present = True
        self._t_polls = 0
        self._r_polls = 0
        self._target_deleted = False
        self._runtime_deleted = False

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        if self._target_deleted:
            self._t_polls += 1
            if self._t_polls > self.t_linger:
                self._t_present = False
        return {"items": ([{"name": self.sid, "targetId": f"tid-{self.sid}", "status": "READY"}]
                          if self._t_present else [])}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        return {"name": self.sid, "targetId": targetId, "status": "READY"}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self._target_deleted = True
        return {}

    def list_agent_runtimes(self, nextToken=None):  # noqa: N803
        if self._runtime_deleted:
            self._r_polls += 1
            if self._r_polls > self.r_linger:
                self._r_present = False
        return {"agentRuntimes": ([{"agentRuntimeName": self.rname,
                                    "agentRuntimeArn": f"arn:x/runtime/{self.rname}-id"}]
                                  if self._r_present else [])}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self._runtime_deleted = True


class StuckControl:
    """Every target delete is refused SYNCHRONIZING forever (a ValidationException
    with a settling-state marker), so only the deadline stops the work. Records
    the order of target delete attempts and any runtime deletes."""

    def __init__(self, targets, runtimes):
        self._t = {n: {"name": n, "targetId": f"tid-{n}", "status": "SYNCHRONIZING"}
                   for n in targets}
        self._r = {n: {"agentRuntimeName": n, "agentRuntimeArn": f"arn:x/{n}-id"} for n in runtimes}
        self.delete_attempts: list[str] = []
        self.deleted_runtimes: list[str] = []

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        return {"items": [dict(t) for t in self._t.values()]}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        for t in self._t.values():
            if t["targetId"] == targetId:
                return dict(t)
        raise _ResourceNotFound("gone")

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        name = next((n for n, t in self._t.items() if t["targetId"] == targetId), None)
        self.delete_attempts.append(name)
        raise synchronizing_err()

    def list_agent_runtimes(self, nextToken=None):  # noqa: N803
        return {"agentRuntimes": [dict(r) for r in self._r.values()]}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self.deleted_runtimes.append(agentRuntimeId)
