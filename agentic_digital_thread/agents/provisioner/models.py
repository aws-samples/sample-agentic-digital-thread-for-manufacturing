# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Typed inputs/results for the onboarding provisioner handlers (2.3R).

Each handler in :mod:`agents.provisioner.handlers` takes a small typed input and
returns a typed result. The Step Functions state machine (built later in the
infra stage, NOT in this wave) passes JSON between states; the dataclasses here
``from_dict`` / ``to_dict`` cleanly so the same handlers are callable both from
the state machine (JSON in/out) and from unit tests (typed in/out).

These are intentionally small and explicit — one dataclass per handler boundary
— so each handler is individually unit-testable with a stubbed ``boto3`` client
and a fake session store.

Who calls them today: ``scripts/provision_runtimes.py`` (deploy.sh and the
workshop custom resource) and ``agents/runtime_provision_helpers.py``. The
Step Functions provisioner state machine they were first written for had no
caller and was removed on 2026-10-29, with its Lambda entrypoint
(``lambda_handler``). Text below that names the state machine describes that
original design.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _configured_agent_model(region: str) -> str:
    """The configured system-agent model, resolved exactly as the in-app path
    resolves it (``AgentManager._system_agent_model`` without a per-system
    override): the model saved in Admin > Settings (SSM
    ``/adt/llm-agent-model``), else ``SYSTEM_AGENT_MODEL``, else the model
    catalog's AGENT default (``model_config.configured_agent_model``).

    Lazy import so the dataclass module stays import-light for unit tests.
    """
    from agents.model_config import configured_agent_model
    return configured_agent_model(region)


# --------------------------------------------------------------------------- #
# validate_session
# --------------------------------------------------------------------------- #
@dataclass
class ValidateSessionInput:
    """Input to ``validate_session``.

    ``execution_arn`` is the ARN of the *currently running* Step Functions
    execution. It is injected by the state machine from the **trusted context
    object** (``$$.Execution.Id``) — NOT from caller-supplied input — and is
    checked against the session's stored ``provision_execution_arn``. This is the
    anti-hijack / orphan-execution binding: a forged or second execution that did
    not win the ``resolved -> provisioning`` claim carries a different
    ``$$.Execution.Id`` and is rejected, so it cannot drive provisioning for a
    session another execution owns.
    """

    system_id: str
    execution_arn: str

    @classmethod
    def from_dict(cls, data: dict) -> "ValidateSessionInput":
        return cls(system_id=data["system_id"], execution_arn=data["execution_arn"])


@dataclass
class ValidateSessionResult:
    system_id: str
    status: str
    initiated_by: str = ""
    provision_execution_arn: str = ""

    def to_dict(self) -> dict:
        return {
            "system_id": self.system_id,
            "status": self.status,
            "initiated_by": self.initiated_by,
            "provision_execution_arn": self.provision_execution_arn,
        }


# --------------------------------------------------------------------------- #
# create_runtime
# --------------------------------------------------------------------------- #
@dataclass
class CreateRuntimeInput:
    """Everything ``create_runtime`` needs to call ``create_agent_runtime``.

    Mirrors the create path in ``agent_manager._start_cloud`` but as a small
    explicit record. Infra-supplied fields (``ecr_image_uri``,
    ``runtime_role_arn``, VPC config, M2M authorizer) are passed in by the state
    machine from the runtime env, not discovered here.
    """

    system_id: str
    system_name: str = ""
    schema: str = ""
    connection_type: str = "PostgreSQL"
    connection_config: dict = field(default_factory=dict)
    conn_params: dict = field(default_factory=dict)
    region: str = "us-east-1"
    ecr_image_uri: str = ""
    runtime_role_arn: str = ""
    vpc_subnets: list[str] = field(default_factory=list)
    vpc_security_group: str = ""
    # M2M JWT authorizer (gateway -> runtime). Empty disables the authorizer.
    m2m_discovery_url: str = ""
    m2m_client_id: str = ""
    # Extra model envs passed straight through to the runtime. An empty
    # system_agent_model (the state machine passes none) is the configured
    # agent model for ``region`` (see __post_init__), so both provisioning
    # paths give a new runtime the same model. An empty bedrock_model (the
    # state machine passes none) is the provisioner's BEDROCK_MODEL env (the
    # deployment's model, set by the onboarding stack), else the runtime-env
    # contract's default: RuntimeEnvSources.from_ambient applies both.
    system_agent_model: str = ""
    bedrock_model: str = ""

    def __post_init__(self) -> None:
        if not self.system_agent_model:
            self.system_agent_model = _configured_agent_model(self.region)

    @classmethod
    def from_dict(cls, data: dict) -> "CreateRuntimeInput":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CreateRuntimeResult:
    system_id: str
    runtime_arn: str
    runtime_id: str
    runtime_mcp_url: str
    connection_secret_arn: str = ""

    def to_dict(self) -> dict:
        return {
            "system_id": self.system_id,
            "runtime_arn": self.runtime_arn,
            "runtime_id": self.runtime_id,
            "runtime_mcp_url": self.runtime_mcp_url,
            "connection_secret_arn": self.connection_secret_arn,
        }


# --------------------------------------------------------------------------- #
# wait_ready
# --------------------------------------------------------------------------- #
@dataclass
class WaitReadyInput:
    runtime_id: str
    region: str = "us-east-1"

    @classmethod
    def from_dict(cls, data: dict) -> "WaitReadyInput":
        return cls(runtime_id=data["runtime_id"], region=data.get("region", "us-east-1"))


@dataclass
class WaitReadyResult:
    runtime_id: str
    status: str
    # The version the READY read reported ("" when none), and how many READY
    # reads were skipped because they still showed an older version than
    # ``wait_ready(min_version=...)`` asked for. Not in ``to_dict``: the
    # state machine's payload is unchanged.
    version: str = ""
    stale_polls: int = 0

    def to_dict(self) -> dict:
        return {"runtime_id": self.runtime_id, "status": self.status}


# --------------------------------------------------------------------------- #
# create_target
# --------------------------------------------------------------------------- #
@dataclass
class CreateTargetInput:
    system_id: str
    system_name: str
    gateway_id: str
    runtime_mcp_url: str
    region: str = "us-east-1"
    credential_provider_arn: str = ""
    m2m_scopes: list[str] = field(default_factory=lambda: ["mcp/tools.read", "mcp/tools.execute"])
    # Optional explicit target description (additive).
    # Empty preserves the historical "System agent for {system_name}" text; the
    # provision_runtimes driver passes the deploy.sh §2e onboarding wording.
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "CreateTargetInput":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CreateTargetResult:
    system_id: str
    gateway_target_id: str

    def to_dict(self) -> dict:
        return {"system_id": self.system_id, "gateway_target_id": self.gateway_target_id}


# --------------------------------------------------------------------------- #
# synchronize
# --------------------------------------------------------------------------- #
@dataclass
class SynchronizeInput:
    gateway_id: str
    gateway_target_id: str
    region: str = "us-east-1"

    @classmethod
    def from_dict(cls, data: dict) -> "SynchronizeInput":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SynchronizeResult:
    gateway_target_id: str
    synchronized: bool

    def to_dict(self) -> dict:
        return {
            "gateway_target_id": self.gateway_target_id,
            "synchronized": self.synchronized,
        }


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
@dataclass
class VerifyInput:
    gateway_id: str
    gateway_target_id: str
    region: str = "us-east-1"

    @classmethod
    def from_dict(cls, data: dict) -> "VerifyInput":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class VerifyResult:
    gateway_target_id: str
    status: str

    def to_dict(self) -> dict:
        return {"gateway_target_id": self.gateway_target_id, "status": self.status}


# --------------------------------------------------------------------------- #
# write_terminal_status
# --------------------------------------------------------------------------- #
@dataclass
class WriteTerminalStatusInput:
    """Input to ``write_terminal_status``.

    ``status`` is ``'onboarded'`` (success path) or ``'failed'`` (the state
    machine's catch path). ``failure_reason`` is recorded on the failed item.
    """

    system_id: str
    status: str
    failure_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "WriteTerminalStatusInput":
        return cls(
            system_id=data["system_id"],
            status=data["status"],
            failure_reason=data.get("failure_reason", ""),
        )


@dataclass
class WriteTerminalStatusResult:
    system_id: str
    status: str

    def to_dict(self) -> dict:
        return {"system_id": self.system_id, "status": self.status}
