# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Classifier + onboard reset-target (``_accept_target_delete``) behaviour.

A gateway target in a transitional (non-deleting) state must be RETRIED, not
mistaken for "already deleting" (a live bug on a rehearsal account,
2026-10-09): the reset treated ANY delete error as "already deleting", never
retried, and burned the full 900 s wait. These tests pin the single-system
reset path's outcomes and the SDK-error-code-first classifier. Shared
synthetic fakes live in ``_reset_target_fakes``. See its module docstring for
the old-code proof taxonomy.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

try:  # dual-import: pytest rootdir vs. direct run (matches _demo_catalog usage)
    from unit._reset_target_fakes import (  # noqa: E402
        SID, TargetControl, access_denied_err, conflict_generic_err, deleting_err,
        run_reset_target, synchronizing_err, validation_nonstate_err)
except ImportError:  # pragma: no cover
    from _reset_target_fakes import (  # noqa: E402
        SID, TargetControl, access_denied_err, conflict_generic_err, deleting_err,
        run_reset_target, synchronizing_err, validation_nonstate_err)


# ── onboard _reset_delete_gateway_target / _accept_target_delete outcomes ────
def test_reset_target_synchronizing_is_retried_until_accepted():
    """THE live bug: delete refused with 'Synchronizing ... Retry the request' on
    the first 2 calls, then accepted. New code retries, the 3rd delete is
    accepted, the target goes absent => reported deleted, timed_out False.
    (API-SHAPE on old code — see ``run_reset_target``; the signature-stable
    behavioural proof of this path is in ``test_reset_target_cleanup``.)"""
    control = TargetControl(
        delete_errors=[synchronizing_err(), synchronizing_err()], status="SYNCHRONIZING")
    (awaited, timed_out, delete_failed), _clock = run_reset_target(control, deadline_s=60.0)
    assert awaited == [SID]
    assert timed_out is False, "a retried-then-accepted delete must report done"
    assert delete_failed is False
    assert control.delete_calls == 3, control.delete_calls  # 2 refused + 1 accepted
    assert control.deleted is True


def test_reset_target_genuine_deleting_waits_for_absence():
    """A real 'Deleting state' error means the target IS going away: do NOT retry
    the delete, just wait for absence. Here it is already DELETING and then
    disappears on the next list — reported done."""
    control = TargetControl(delete_errors=[deleting_err()], status="DELETING")
    orig_list = control.list_gateway_targets
    state = {"polls": 0}

    def _list(gatewayIdentifier, nextToken=None):  # noqa: N803
        state["polls"] += 1
        if state["polls"] >= 2:
            control.deleted = True
        return orig_list(gatewayIdentifier, nextToken)

    control.list_gateway_targets = _list
    (awaited, timed_out, delete_failed), _clock = run_reset_target(control, deadline_s=60.0)
    assert awaited == [SID]
    assert timed_out is False
    assert delete_failed is False
    assert control.delete_calls == 1, "a Deleting-state error must not be retried"


def test_reset_target_access_denied_is_not_treated_as_deleting():
    """An unrelated error (AccessDenied) is NOT 'already deleting': the target
    stays present, the removal is reported NOT done, and the distinct
    delete_failed flag is set so the caller skips the runtime delete."""
    control = TargetControl(delete_errors=[access_denied_err()] * 50, status="READY")
    (awaited, timed_out, delete_failed), _clock = run_reset_target(control, deadline_s=20.0)
    assert awaited == [SID]
    assert timed_out is True, "an unrelated delete error must not report done"
    assert delete_failed is True, "an unrelated error must flag the delete as failed"
    assert control.delete_calls == 1, control.delete_calls


def test_reset_target_retry_stops_at_deadline():
    """A target stuck SYNCHRONIZING forever: retries are bounded by the deadline
    and the removal reports NOT done (delete_failed True), never hanging."""
    control = TargetControl(delete_errors=[synchronizing_err()] * 10_000, status="SYNCHRONIZING")
    (awaited, timed_out, delete_failed), clock = run_reset_target(control, deadline_s=15.0)
    assert awaited == [SID]
    assert timed_out is True
    assert delete_failed is True
    assert clock.t >= 15.0, "must stop at/after the deadline, not loop forever"


def test_reset_accept_access_denied_synchronizing_one_attempt_failed():
    """onboard _accept_target_delete: AccessDenied while status reads SYNCHRONIZING
    must be ONE delete attempt and outcome failed — not retried to the deadline."""
    control = TargetControl(delete_errors=[access_denied_err()] * 50, status="SYNCHRONIZING")
    (awaited, timed_out, delete_failed), _clock = run_reset_target(control, deadline_s=600.0)
    assert awaited == [SID]
    assert timed_out is True and delete_failed is True
    assert control.delete_calls == 1, "a permanent error must not be retried as transitional"


def test_reset_nonstate_validation_synchronizing_one_attempt_failed():
    """onboard _accept_target_delete: a request-defect ValidationException while
    status reads SYNCHRONIZING must be ONE attempt, outcome failed — the status
    alone must not drive a retry."""
    control = TargetControl(delete_errors=[validation_nonstate_err()] * 50, status="SYNCHRONIZING")
    (_awaited, timed_out, delete_failed), _clock = run_reset_target(control, deadline_s=600.0)
    assert timed_out is True and delete_failed is True
    assert control.delete_calls == 1, "a non-state ValidationException must not be retried"


def test_reset_generic_conflict_synchronizing_one_attempt_failed():
    """onboard _accept_target_delete: a generic ConflictException + SYNCHRONIZING
    status => ONE attempt, failed."""
    control = TargetControl(delete_errors=[conflict_generic_err()] * 50, status="SYNCHRONIZING")
    (_awaited, timed_out, delete_failed), _clock = run_reset_target(control, deadline_s=600.0)
    assert timed_out is True and delete_failed is True
    assert control.delete_calls == 1, "a generic ConflictException must not be retried"


# ── classify_gateway_target_delete ──────────────────────────────────────────
def test_classifier_access_denied_with_synchronizing_status_is_other():
    """classify_gateway_target_delete must key on the error CODE + settling-state
    TEXT, not the independently-read status.

    API-SHAPE on old code: the 3-argument form (``err_code``) does not exist, so
    this fails by TypeError with only the transitional-retry fix in place and
    by ImportError before any fix — a
    change-detector for the new classifier signature. The behaviour it pins is
    proved behaviourally by the one-attempt helper tests above/head-start."""
    from agents.runtime_provision_helpers import classify_gateway_target_delete
    assert classify_gateway_target_delete(str(access_denied_err()), "SYNCHRONIZING",
                                          "AccessDeniedException") == "other"
    # A retryable ValidationException with a settling-state marker IN THE TEXT is
    # transitional; the status alone must NOT be enough.
    assert classify_gateway_target_delete("Retry the request", "SYNCHRONIZING",
                                          "ValidationException") == "transitional"
    # A non-state ValidationException is 'other' even when status says SYNCHRONIZING.
    assert classify_gateway_target_delete(str(validation_nonstate_err()), "SYNCHRONIZING",
                                          "ValidationException") == "other"
    # A generic ConflictException (no settling-state reason) is 'other' too.
    assert classify_gateway_target_delete(str(conflict_generic_err()), "SYNCHRONIZING",
                                          "ConflictException") == "other"
    # The exact live 2026-10-09 message still retries (code + text marker).
    assert classify_gateway_target_delete(str(synchronizing_err()), "SYNCHRONIZING",
                                          "ValidationException") == "transitional"
    # DELETING still wins regardless of code.
    assert classify_gateway_target_delete("whatever", "DELETING",
                                          "AccessDeniedException") == "deleting"


_TESTS = [
    test_reset_target_synchronizing_is_retried_until_accepted,
    test_reset_target_genuine_deleting_waits_for_absence,
    test_reset_target_access_denied_is_not_treated_as_deleting,
    test_reset_target_retry_stops_at_deadline,
    test_reset_accept_access_denied_synchronizing_one_attempt_failed,
    test_reset_nonstate_validation_synchronizing_one_attempt_failed,
    test_reset_generic_conflict_synchronizing_one_attempt_failed,
    test_classifier_access_denied_with_synchronizing_status_is_other,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} classifier/reset-target tests passed")
