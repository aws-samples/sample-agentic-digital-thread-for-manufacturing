# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Typed errors for the onboarding provisioner handlers (onboarding Wave 2, 2.3R).

These are raised by the discrete provisioner task handlers in
:mod:`agents.provisioner.handlers`. Errors **propagate** (no fallback, no silent
swallow — user steering 2026-06-30); the Step Functions state machine catches a
terminal failure and routes it to ``write_terminal_status`` to record
``status='failed'`` in the onboarding session store.

Who calls them today: ``scripts/provision_runtimes.py`` (deploy.sh and the
workshop custom resource) and ``agents/runtime_provision_helpers.py``. The
Step Functions provisioner state machine they were first written for had no
caller and was removed on 2026-10-29, with its Lambda entrypoint
(``lambda_handler``). Text below that names the state machine describes that
original design.
"""
from __future__ import annotations


class ProvisionerError(Exception):
    """Base class for onboarding-provisioner handler errors."""


class SessionValidationError(ProvisionerError):
    """The onboarding session is not in the ``provisioning`` state.

    ``validate_session`` raises this when the durable DDB session item's status
    is not ``provisioning`` — a defense-in-depth re-check (ADR-0033 §5) that a
    privileged provisioner execution only runs against a session that actually
    claimed provisioning.
    """


class OrphanExecutionError(ProvisionerError):
    """The running execution is not the one that claimed this session.

    ``validate_session`` raises this when the session's stored
    ``provision_execution_arn`` does not match the ARN of the Step Functions
    execution currently running. This closes the **orphan-execution gap** from
    the onboarding Wave 1 review: a second/forged execution that did not win the
    ``resolved -> provisioning`` claim cannot drive provisioning for the session.
    """


class RuntimeCreateFailedError(ProvisionerError):
    """The AgentCore runtime reached a terminal ``CREATE_FAILED``/``UPDATE_FAILED``.

    Carries the AgentCore ``failureReason`` verbatim so the state machine can
    surface it into the terminal ``failed`` session record.
    """

    def __init__(self, runtime_id: str, failure_reason: str) -> None:
        self.runtime_id = runtime_id
        self.failure_reason = failure_reason
        super().__init__(
            f"AgentCore runtime {runtime_id!r} create failed: {failure_reason}"
        )


class RuntimeNotReadyError(ProvisionerError):
    """The AgentCore runtime did not reach ``READY`` within the poll deadline."""

    def __init__(self, runtime_id: str, last_status: str, deadline_seconds: float) -> None:
        self.runtime_id = runtime_id
        self.last_status = last_status
        self.deadline_seconds = deadline_seconds
        super().__init__(
            f"AgentCore runtime {runtime_id!r} not READY after "
            f"{deadline_seconds:.0f}s (last status={last_status!r})"
        )


class GatewaySyncError(ProvisionerError):
    """The gateway target failed to synchronize within the retry budget."""


class TargetNotReadyError(ProvisionerError):
    """The gateway target did not reach ``READY`` during verification."""


class TargetUpdateError(ProvisionerError):
    """An existing gateway target found on the conflict-reuse path did not
    match the runtime being provisioned, and updating it in place failed or
    did not settle. The target is not deleted; the execution fails so the
    provision can be retried."""
