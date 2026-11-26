# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentManager head-start deletion: ``_delete_gateway_target_and_wait`` must
return a going-away outcome ONLY when target absence is positively confirmed, and
``delete_cloud_agent`` must not delete the runtime otherwise.

Shared synthetic fakes live in ``_reset_target_fakes`` (see its module docstring
for the old-code proof taxonomy).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

try:
    from unit._reset_target_fakes import (  # noqa: E402
        AMControl, TargetControl, access_denied_err, conflict_generic_err,
        deleting_err, run_delete_cloud_agent, run_head_start, synchronizing_err,
        validation_nonstate_err)
except ImportError:  # pragma: no cover
    from _reset_target_fakes import (  # noqa: E402
        AMControl, TargetControl, access_denied_err, conflict_generic_err,
        deleting_err, run_delete_cloud_agent, run_head_start, synchronizing_err,
        validation_nonstate_err)


# ── _delete_gateway_target_and_wait ─────────────────────────────────────────
def test_head_start_synchronizing_is_retried_until_accepted():
    """Signature-free behavioural proof: a 'Synchronizing ... Retry the request'
    refusal is retried until the delete lands, then absence is confirmed. The
    old helper treated it as 'already deleting' (one attempt), so
    this fails there by behaviour (delete_calls 1 != 3)."""
    control = TargetControl(
        delete_errors=[synchronizing_err(), synchronizing_err()], status="SYNCHRONIZING")
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 3, control.delete_calls
    assert control.deleted is True
    assert outcome == "gone"  # accepted delete -> absence positively confirmed


def test_head_start_genuine_deleting_is_confirmed_absent_before_gone():
    """A 'Deleting state' refusal is NOT retried, but DELETING is only the
    *current* status of an async delete — not proof of absence. The helper must
    keep polling until the target actually disappears, then return 'gone'. Here
    it stays DELETING for the first poll, then disappears: expect >= 2 status
    reads, one delete attempt, and a final 'gone'."""
    control = TargetControl(delete_errors=[deleting_err()], status="DELETING")
    state = {"gets": 0}
    orig_get = control.get_gateway_target

    def _get(gatewayIdentifier, targetId):  # noqa: N803
        state["gets"] += 1
        # Disappear on the 3rd read: 1 inside _issue_target_delete + >=2 polls.
        if state["gets"] >= 3:
            control.deleted = True
        return orig_get(gatewayIdentifier, targetId)

    control.get_gateway_target = _get
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 1, "a Deleting-state error must not be retried"
    assert control.get_calls >= 2, f"must poll for absence, saw {control.get_calls} reads"
    assert control.deleted is True, "the target actually disappeared"
    assert outcome == "gone", "a DELETING target is 'gone' only once confirmed absent"


def test_head_start_deleting_but_never_absent_is_failed():
    """A DELETING target that NEVER disappears before the 60 s deadline must be
    reported 'failed' (absence not confirmed), not a going-away outcome."""
    control = TargetControl(delete_errors=[deleting_err()], status="DELETING")
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 1
    assert outcome == "failed", "DELETING-but-present-at-deadline must be failed"


def test_head_start_access_denied_is_not_retried_and_reports_failed():
    """AccessDenied on the head-start delete: not retried as transitional, and
    reported as ``failed`` so the caller skips the runtime delete."""
    control = TargetControl(delete_errors=[access_denied_err()] * 50, status="READY")
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 1, control.delete_calls
    assert outcome == "failed"


def test_head_start_access_denied_synchronizing_one_attempt_failed():
    """AccessDenied + SYNCHRONIZING status => one delete attempt, outcome failed."""
    control = TargetControl(delete_errors=[access_denied_err()] * 50, status="SYNCHRONIZING")
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 1, "a permanent error must not be retried as transitional"
    assert outcome == "failed"


def test_head_start_nonstate_validation_synchronizing_one_attempt_failed():
    """Request-defect ValidationException + SYNCHRONIZING => one attempt, failed."""
    control = TargetControl(delete_errors=[validation_nonstate_err()] * 50, status="SYNCHRONIZING")
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 1, "a non-state ValidationException must not be retried"
    assert outcome == "failed"


def test_head_start_generic_conflict_synchronizing_one_attempt_failed():
    """Generic ConflictException + SYNCHRONIZING => one attempt, failed."""
    control = TargetControl(delete_errors=[conflict_generic_err()] * 50, status="SYNCHRONIZING")
    outcome, _clock = run_head_start(control)
    assert control.delete_calls == 1, "a generic ConflictException must not be retried"
    assert outcome == "failed"


# ── delete_cloud_agent resource ordering ────────────────────────────────────
def test_delete_cloud_agent_skips_runtime_on_failed_target():
    """delete_cloud_agent must NOT call delete_agent_runtime when the target
    delete FAILED (AccessDenied). The injected wall clock makes old code fail
    fast by assertion."""
    control = AMControl(delete_error=access_denied_err())
    run_delete_cloud_agent(control)
    assert control.delete_runtime_calls == 0, "runtime must not be deleted when target delete failed"


def test_delete_cloud_agent_skips_runtime_when_target_never_absent():
    """Target delete ACCEPTED but the target never leaves the listing through the
    60 s poll. Absence is not confirmed, so the runtime must NOT be deleted."""
    control = AMControl(never_absent=True)
    run_delete_cloud_agent(control)
    assert control.delete_runtime_calls == 0, "runtime must not be deleted without confirmed absence"


def test_delete_cloud_agent_skips_runtime_on_status_read_error():
    """Target delete ACCEPTED but every status read raises a non-NotFound error
    (Throttling), so absence is UNKNOWN. The runtime must NOT be deleted (old
    code logged the read error as 'confirmed deleted' and removed it)."""
    control = AMControl(get_raises=True)
    run_delete_cloud_agent(control)
    assert control.delete_runtime_calls == 0, "runtime must not be deleted when absence is unknown"


def test_delete_cloud_agent_skips_runtime_when_deleting_target_never_absent():
    """The service reports the target already in Deleting state, but it never
    actually disappears before the deadline. DELETING is not proof of absence,
    so the runtime must NOT be deleted. Fails on the code before this fix,
    where a 'deleting' outcome short-circuited and the runtime was deleted
    immediately."""
    control = AMControl(deleting=True, never_absent=True)
    run_delete_cloud_agent(control)
    assert control.delete_runtime_calls == 0, "a never-absent DELETING target must not free the runtime"


_TESTS = [
    test_head_start_synchronizing_is_retried_until_accepted,
    test_head_start_genuine_deleting_is_confirmed_absent_before_gone,
    test_head_start_deleting_but_never_absent_is_failed,
    test_head_start_access_denied_is_not_retried_and_reports_failed,
    test_head_start_access_denied_synchronizing_one_attempt_failed,
    test_head_start_nonstate_validation_synchronizing_one_attempt_failed,
    test_head_start_generic_conflict_synchronizing_one_attempt_failed,
    test_delete_cloud_agent_skips_runtime_on_failed_target,
    test_delete_cloud_agent_skips_runtime_when_target_never_absent,
    test_delete_cloud_agent_skips_runtime_on_status_read_error,
    test_delete_cloud_agent_skips_runtime_when_deleting_target_never_absent,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} head-start/delete-cloud-agent tests passed")
