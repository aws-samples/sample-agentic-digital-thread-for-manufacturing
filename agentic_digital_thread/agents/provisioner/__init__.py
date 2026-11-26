# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding provisioner — discrete task handlers (onboarding Wave 2, 2.3R).

The privileged create sequence (create runtime → READY → create target →
synchronize → verify → write terminal status), relocated out of
``agent_manager._start_cloud`` into discrete, individually unit-testable handler
functions that the Step Functions provisioner state machine invokes. Additive:
the in-app ``_start_cloud`` path is left intact (cutover gated to Wave 3).

Who calls them today: ``scripts/provision_runtimes.py`` (deploy.sh and the
workshop custom resource) and ``agents/runtime_provision_helpers.py``. The
Step Functions provisioner state machine they were first written for had no
caller and was removed on 2026-10-29, with its Lambda entrypoint
(``lambda_handler``). Text below that names the state machine describes that
original design.
"""
from .errors import (
    GatewaySyncError,
    OrphanExecutionError,
    ProvisionerError,
    RuntimeCreateFailedError,
    RuntimeNotReadyError,
    SessionValidationError,
    TargetNotReadyError,
    TargetUpdateError,
)
from .handlers import (
    create_runtime,
    create_target,
    synchronize,
    validate_session,
    verify,
    wait_ready,
    write_terminal_status,
)
from .models import (
    CreateRuntimeInput,
    CreateRuntimeResult,
    CreateTargetInput,
    CreateTargetResult,
    SynchronizeInput,
    SynchronizeResult,
    ValidateSessionInput,
    ValidateSessionResult,
    VerifyInput,
    VerifyResult,
    WaitReadyInput,
    WaitReadyResult,
    WriteTerminalStatusInput,
    WriteTerminalStatusResult,
)

__all__ = [
    # handlers
    "validate_session",
    "create_runtime",
    "wait_ready",
    "create_target",
    "synchronize",
    "verify",
    "write_terminal_status",
    # errors
    "ProvisionerError",
    "SessionValidationError",
    "OrphanExecutionError",
    "RuntimeCreateFailedError",
    "RuntimeNotReadyError",
    "GatewaySyncError",
    "TargetNotReadyError",
    "TargetUpdateError",
    # models
    "ValidateSessionInput",
    "ValidateSessionResult",
    "CreateRuntimeInput",
    "CreateRuntimeResult",
    "WaitReadyInput",
    "WaitReadyResult",
    "CreateTargetInput",
    "CreateTargetResult",
    "SynchronizeInput",
    "SynchronizeResult",
    "VerifyInput",
    "VerifyResult",
    "WriteTerminalStatusInput",
    "WriteTerminalStatusResult",
]
