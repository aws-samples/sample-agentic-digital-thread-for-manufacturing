# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single-system reset and reset-all cleanup: the runtime is deleted ONLY after
the gateway target is verifiably ABSENT, and one shared deadline bounds the
whole cleanup (no per-phase / per-target multiplication).

Includes the SIGNATURE-STABLE behavioural proof of the onboard reset path
through ``_reset_cleanup_agentcore_sync(sid, deadline_s)`` (its signature is
unchanged from the code before the fix), so that code fails by BEHAVIOUR, not
a new keyword.
Shared synthetic fakes live in ``_reset_target_fakes``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
# This worktree's package root first, so a direct run (`python <file>`) imports
# THIS checkout's agents/, not whatever checkout the shared editable venv points
# at. Must come before `import agents.onboard` below.
_ROOT = _HERE.parents[1]
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402

try:
    from unit._reset_target_fakes import (  # noqa: E402
        AcceptedControl, CleanupControl, LingerControl, ResetAllControl,
        RetryThenAcceptControl, StuckControl, access_denied_err, clocked_cleanup,
        run_reset_all, run_single_cleanup)
except ImportError:  # pragma: no cover
    from _reset_target_fakes import (  # noqa: E402
        AcceptedControl, CleanupControl, LingerControl, ResetAllControl,
        RetryThenAcceptControl, StuckControl, access_denied_err, clocked_cleanup,
        run_reset_all, run_single_cleanup)


# ── Signature-stable behavioural proof of the onboard reset PATH ─────────────
def test_reset_cleanup_retries_synchronizing_then_completes():
    """The primary live-bug path, proved through the UNCHANGED
    ``_reset_cleanup_agentcore_sync(sid, deadline_s)`` signature so it is a RED
    behavioural proof on the code before the fix (where the delete is attempted
    ONCE, treated as 'already deleting', and the reset reports incomplete:
    delete_calls 1 != 3), and GREEN now (retries until accepted,
    target+runtime confirmed absent, complete). NOTE: with only the
    transitional-retry fix in place this test already passes (a GUARD); its
    RED proof target is the code before that fix."""
    control = RetryThenAcceptControl(sid="sys-a", refusals=2)
    result, _clock = run_single_cleanup(control, deadline_s=120.0)
    assert control.delete_calls == 3, control.delete_calls  # 2 refused + 1 accepted
    assert result["wait_timed_out"] is False, "retried-then-accepted reset must complete"
    assert control.deleted_runtimes == ["adt_sys_a-id"], control.deleted_runtimes
    assert result["targets_awaited"] == ["sys-a"]


# ── Single reset: runtime deleted only after verified target absence ─────────
def test_single_reset_does_not_delete_runtime_after_unrelated_target_error():
    """An unrelated target-delete error (AccessDenied) must leave the live target
    in place AND skip delete_agent_runtime (else the target strands against a
    missing runtime)."""
    control = CleanupControl(access_denied_err())
    result, _clock = run_single_cleanup(control, deadline_s=10.0)
    assert control.delete_runtime_calls == 0, "runtime must not be deleted when target survives"
    assert result["wait_timed_out"] is True


def test_single_reset_no_runtime_delete_when_accepted_target_never_absent():
    """The delete is ACCEPTED but the target never leaves the listing before the
    deadline. The runtime must NOT be deleted and the reset reports incomplete."""
    control = AcceptedControl(never_absent=True)
    result, _clock = run_single_cleanup(control, deadline_s=15.0)
    assert control.delete_runtime_calls == 0, "runtime must not be deleted while target survives"
    assert result["wait_timed_out"] is True


def test_single_reset_no_runtime_delete_when_absence_listing_raises():
    """The delete is ACCEPTED but a later list_gateway_targets fails, so absence
    is UNKNOWN. The runtime must NOT be deleted and the reset reports incomplete."""
    control = AcceptedControl(never_absent=False, list_raises=True)
    result, _clock = run_single_cleanup(control, deadline_s=15.0)
    assert control.delete_runtime_calls == 0, "runtime must not be deleted when absence is unknown"
    assert result["wait_timed_out"] is True


# ── Reset-all: verify target absence before the all-or-none runtime sweep ────
def test_reset_all_does_not_delete_runtimes_after_unrelated_target_error():
    """When ANY target delete fails with an unrelated error, NO runtimes are
    deleted and the reset reports incomplete."""
    control = ResetAllControl(
        targets=["orchestrator", "lake-athena", "telem-sitewise"],
        runtimes=["adt_lake_athena", "adt_telem_sitewise"],
        fail_targets={"lake-athena"}, error=access_denied_err())
    result, _clock = run_reset_all(control)
    assert control.deleted_runtimes == [], "no runtime may be deleted when a target delete failed"
    assert result["wait_timed_out"] is True


def test_reset_all_no_runtime_sweep_when_accepted_target_never_absent():
    """All deletes ACCEPTED but one target never leaves the listing. The runtime
    sweep must be skipped (verify absence first) and the reset reports
    incomplete."""
    control = ResetAllControl(
        targets=["orchestrator", "lake-athena", "telem-sitewise"],
        runtimes=["adt_lake_athena", "adt_telem_sitewise"],
        never_absent={"telem-sitewise"})
    result, _clock = run_reset_all(control)
    assert control.deleted_runtimes == [], "runtime sweep must wait for target absence"
    assert result["wait_timed_out"] is True


def test_reset_all_no_runtime_sweep_when_absence_listing_raises():
    """Deletes accepted, but the absence listing fails, so absence is UNKNOWN.
    The runtime sweep must be skipped and the reset reports incomplete."""
    control = ResetAllControl(
        targets=["orchestrator", "lake-athena", "telem-sitewise"],
        runtimes=["adt_lake_athena", "adt_telem_sitewise"],
        list_raises_after=1)  # first list (the sweep's lookup) ok, absence list raises
    result, _clock = run_reset_all(control)
    assert control.deleted_runtimes == [], "runtime sweep must not run when absence is unknown"
    assert result["wait_timed_out"] is True


# ── Shared deadline accounting ───────────────────────────────────────────────
def test_single_reset_shares_one_deadline_across_both_waits():
    """The target absence wait consumes MOST of the budget, so under one SHARED
    deadline the runtime wait gets only the leftover and times out (bounded
    total). With a deadline per wait (the code before this fix) the runtime
    wait got a FRESH full deadline, so the lingering runtime went absent and
    the whole thing ran past one window — this fails there by assertion (both
    the timed_out flag and the time bound)."""
    poll = onboard._DELETE_WAIT_POLL_S
    deadline_s = 30.0  # 10 polls of 3 s
    # Target lingers 7 polls (~21 s) — most of the budget. Runtime then needs 5
    # polls (~15 s): under sharing only ~3 polls remain, so the runtime wait
    # times out; under a fresh full deadline (old code) it succeeds at ~36 s.
    control = LingerControl("sys-a", target_linger_polls=7, runtime_linger_polls=5)
    result, clock = run_single_cleanup(control, deadline_s=deadline_s)
    assert result["wait_timed_out"] is True, "shared budget must starve the runtime wait"
    assert clock.t <= deadline_s + poll + 0.001, f"total virtual time {clock.t} exceeded one deadline"


def test_reset_all_target_phase_within_one_aggregate_deadline():
    """Three stuck targets share ONE small aggregate deadline (round-robin), the
    runtime sweep is skipped, and the attempt order is round-robin
    (t1,t2,t3,...), not serial. Before this fix each target got its own full
    deadline serially, so elapsed time blew past the patched aggregate and t2's
    first attempt came only after t1 exhausted."""
    control = StuckControl(targets=["t1", "t2", "t3"], runtimes=["adt_t1"])
    small = 12.0  # 4 rounds of 3 s
    with patch.object(onboard, "_DELETE_WAIT_DEADLINE_S", small), \
            patch("boto3.client", lambda *a, **k: control), \
            patch.dict(os.environ, {"AGENTCORE_GATEWAY_ID": "gw-x", "AWS_REGION": "us-east-1"}):
        result, clock = clocked_cleanup(onboard._global_reset_cleanup_agentcore_sync)
    assert result["wait_timed_out"] is True
    assert control.deleted_runtimes == [], "no runtime sweep when targets never go absent"
    assert clock.t <= small + onboard._DELETE_WAIT_POLL_S + 0.001, clock.t
    assert control.delete_attempts[:3] == ["t1", "t2", "t3"], control.delete_attempts[:6]


_TESTS = [
    test_reset_cleanup_retries_synchronizing_then_completes,
    test_single_reset_does_not_delete_runtime_after_unrelated_target_error,
    test_single_reset_no_runtime_delete_when_accepted_target_never_absent,
    test_single_reset_no_runtime_delete_when_absence_listing_raises,
    test_reset_all_does_not_delete_runtimes_after_unrelated_target_error,
    test_reset_all_no_runtime_sweep_when_accepted_target_never_absent,
    test_reset_all_no_runtime_sweep_when_absence_listing_raises,
    test_single_reset_shares_one_deadline_across_both_waits,
    test_reset_all_target_phase_within_one_aggregate_deadline,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} cleanup/deadline tests passed")
