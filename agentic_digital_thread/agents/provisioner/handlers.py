# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discrete, individually unit-testable onboarding provisioner handlers (2.3R).

This package relocates the privileged create sequence currently inlined in
``agent_manager._start_cloud`` ::

    create_agent_runtime -> wait READY -> create_gateway_target
    -> synchronize_gateway_targets -> verify

into **discrete handler functions** that the Step Functions provisioner state
machine (built in the infra stage, NOT in this wave) invokes one state at a
time. Each handler:

* takes a small typed input (:mod:`agents.provisioner.models`) and returns a
  typed result;
* accepts an injectable ``boto3`` client / session store (keyword-only) so it is
  unit-testable in isolation with a stub — no live AWS;
* **propagates** errors (no fallback, no silent swallow). A terminal failure is
  caught by the state machine and routed to ``write_terminal_status`` to record
  ``status='failed'`` in the durable onboarding session.

IMPORTANT (onboarding Wave 2 scope): this is **additive**. ``_start_cloud`` and
the in-app provisioning path are left INTACT — the in-app→SFN cutover and the
AdtTaskRole shrink are gated to onboarding Wave 3. These handlers do not import or
mutate ``agent_manager``.

Who calls them today: ``scripts/provision_runtimes.py`` (deploy.sh and the
workshop custom resource) and ``agents/runtime_provision_helpers.py``. The
Step Functions provisioner state machine they were first written for had no
caller and was removed on 2026-10-29, with its Lambda entrypoint
(``lambda_handler``). Text below that names the state machine describes that
original design.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import boto3

from ..connection_types import KNOWLEDGE_GRAPH_CONNECTION_TYPE
from ..onboarding_session import (
    STATUS_PROVISIONING,
    STATUS_ONBOARDED,
    STATUS_FAILED,
    OnboardingSessionStore,
)
from ..runtime_provision_helpers import (
    GatewayTargetUpdateError,
    agent_config_secret_value as _agent_config_secret_value,
    bring_gateway_target_in_line as _bring_gateway_target_in_line,
    container_uri as _container_uri,
    gateway_target_drift as _gateway_target_drift,
    list_all_gateway_targets as _list_all_gateway_targets,
    network_config as _network_config,
    runtime_connection_env as _runtime_connection_env,
    upsert_connection_secret as _upsert_connection_secret,
)
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

logger = logging.getLogger(__name__)

# Terminal AgentCore runtime states (poll surfaces these as a hard failure).
_RUNTIME_FAILED_STATES = ("CREATE_FAILED", "UPDATE_FAILED")

# Default READY poll budget (relocated from _start_cloud: 300s).
_READY_DEADLINE_SECONDS = 300.0


def _control_client(region: str):
    """AgentCore control-plane client (the privileged create/poll surface)."""
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _secrets_client(region: str):
    return boto3.client("secretsmanager", region_name=region)


# --------------------------------------------------------------------------- #
# 1. validate_session — defense-in-depth + orphan-execution gate
# --------------------------------------------------------------------------- #
def validate_session(
    inp: ValidateSessionInput, *, store: OnboardingSessionStore | None = None
) -> ValidateSessionResult:
    """Re-read the durable session and assert it is safe to provision.

    Two checks (ADR-0033 §5 / decision 5), both load-bearing:

    1. **status == 'provisioning'** — the session must have actually won the
       ``resolved -> provisioning`` claim. A privileged provisioner execution
       never runs against a session in any other state.
    2. **provision_execution_arn == this execution's ARN** — the session's
       recorded execution ARN must match the ARN of the execution currently
       running this handler. This closes the **orphan-execution gap** (Wave 1
       review): a second or forged Step Functions execution that did not win the
       claim (its ARN was never recorded on the item) is rejected here, so it
       cannot drive provisioning for a session another execution owns.

    Raises :class:`SessionValidationError` (wrong status) or
    :class:`OrphanExecutionError` (ARN mismatch). The underlying
    ``store.get_session`` raises ``SessionNotFoundError`` for a missing session
    (propagated, not swallowed).
    """
    store = store or OnboardingSessionStore()
    session = store.get_session(inp.system_id)  # raises SessionNotFoundError

    status = session.get("status")
    if status != STATUS_PROVISIONING:
        raise SessionValidationError(
            f"Onboarding session for system_id={inp.system_id!r} is in status "
            f"{status!r}, not {STATUS_PROVISIONING!r}; refusing to provision."
        )

    stored_arn = session.get("provision_execution_arn", "")
    if stored_arn != inp.execution_arn:
        raise OrphanExecutionError(
            f"Execution ARN mismatch for system_id={inp.system_id!r}: this "
            f"execution {inp.execution_arn!r} is not the one that claimed the "
            f"session (recorded {stored_arn!r}). Orphan execution rejected."
        )

    return ValidateSessionResult(
        system_id=inp.system_id,
        status=status,
        initiated_by=session.get("initiated_by", ""),
        provision_execution_arn=stored_arn,
    )


# --------------------------------------------------------------------------- #
# 2. create_runtime
# --------------------------------------------------------------------------- #
def _runtime_env_vars(inp: CreateRuntimeInput, connection_secret_arn: str,
                      effective_config: dict) -> dict:
    """Runtime env for the SFN path — delegated to the ONE contract builder
    (``agents/runtime_env_contract.py::build_env("system", …)``); the old
    hand-maintained mirror of ``agent_manager``'s builders is gone, so a key
    added to the contract reaches BOTH provisioning paths by construction.

    Unification note (2026-08-05): the mirror had ALREADY drifted — the SFN
    path was starved of GRAPH_IDENTIFIER, I3X_TLS_VERIFY,
    SYSTEM_IDENTITY_CLAIM_SECRET_ARN and DATASET_REF_BUCKET (the exact
    wipe-class bug this module class caused before). Delegation adds those
    four keys here, sourced from the provisioner Lambda's ambient env (empty
    string when the Lambda env lacks them — deploy.sh §2f's reconcile then
    propagates the identity-claim ARN, ADR-0049).
    """
    from ..runtime_env_contract import RuntimeEnvSources, build_env
    env_vars = build_env(
        "system",
        RuntimeEnvSources.from_ambient(
            region=inp.region, bedrock_model=inp.bedrock_model),
        system_id=inp.system_id,
        system_name=inp.system_name,
        schema=inp.schema,
        connection_type=inp.connection_type,
        system_agent_model=inp.system_agent_model,
    )
    env_vars.update(
        _runtime_connection_env(inp.connection_type, connection_secret_arn, effective_config)
    )
    return env_vars


def _create_runtime_kwargs(inp: CreateRuntimeInput, runtime_name: str,
                           env_vars: dict) -> dict:
    """The full ``create_agent_runtime`` kwargs (MCP protocol + optional M2M
    JWT authorizer) — extracted verbatim from ``create_runtime``."""
    create_kwargs = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": _container_uri(inp.ecr_image_uri)}
        },
        "networkConfiguration": _network_config(inp.vpc_subnets, inp.vpc_security_group),
        "protocolConfiguration": {"serverProtocol": "MCP"},
        "roleArn": inp.runtime_role_arn,
        "environmentVariables": env_vars,
    }
    if inp.m2m_discovery_url and inp.m2m_client_id:
        create_kwargs["authorizerConfiguration"] = {
            "customJWTAuthorizer": {
                "discoveryUrl": inp.m2m_discovery_url,
                "allowedClients": [inp.m2m_client_id],
            }
        }
    return create_kwargs


def _reuse_existing_runtime(control, runtime_name: str) -> "tuple[str, str]":
    """ConflictException reuse: find the existing ``adt_<id>`` runtime by name
    (mirrors create_target's conflict handling). An unmatched conflict
    (exists but not findable) fails fast — no empty id."""
    from ..runtime_provision_helpers import list_all_agent_runtimes
    for rt in list_all_agent_runtimes(control):
        if rt.get("agentRuntimeName") == runtime_name:
            return rt.get("agentRuntimeArn", ""), rt.get("agentRuntimeId", "")
    raise ProvisionerError(
        f"create_agent_runtime raised ConflictException for "
        f"{runtime_name!r} but no existing runtime with that name was "
        f"found in list_agent_runtimes; cannot reuse — failing fast."
    )


def create_runtime(
    inp: CreateRuntimeInput, *, control=None, secrets=None
) -> CreateRuntimeResult:
    """Create the AgentCore runtime for the system (single clean create).

    Relocates the create_agent_runtime body of ``_start_cloud``: stores the
    connection config in Secrets Manager (skipped for KnowledgeGraph
    systems), builds the runtime env + optional M2M JWT authorizer (split
    helpers above), and calls ``create_agent_runtime``. Unlike the in-app
    ``_start_cloud`` this carries NO AZ-strip self-heal / conflict-wait
    retry loops — the AZ-allowlist deploy prerequisite (plan Wave 0) is the
    fix; the READY poll is the discrete ``wait_ready`` state.
    """
    control = control or _control_client(inp.region)
    is_knowledge_graph = inp.connection_type == KNOWLEDGE_GRAPH_CONNECTION_TYPE
    effective_config = dict(inp.connection_config or {})
    if inp.connection_type == "PostgreSQL" and inp.conn_params:
        effective_config.update(inp.conn_params)
    # Store connection config in Secrets Manager (avoids the env-var size
    # limit) — the agent-config write that moves onto the provisioner role.
    connection_secret_arn = ""  # nosec B105 - secret ARN placeholder, not a credential
    if not is_knowledge_graph:
        secrets = secrets or _secrets_client(inp.region)
        secret_name = f"adt/agent-config/{inp.system_id}"
        secret_value = _agent_config_secret_value(effective_config, label=inp.system_id)
        connection_secret_arn = _upsert_connection_secret(secrets, secret_name, secret_value)
        logger.info("%s: stored the connection config secret", inp.system_id)  # logs the system id only, never the value

    env_vars = _runtime_env_vars(inp, connection_secret_arn, effective_config)
    runtime_name = f"adt_{inp.system_id}".replace("-", "_")[:48]
    create_kwargs = _create_runtime_kwargs(inp, runtime_name, env_vars)
    # Idempotent create: an SFN retry / re-onboard can re-enter this state.
    try:
        resp = control.create_agent_runtime(**create_kwargs)
        runtime_arn = resp["agentRuntimeArn"]
        runtime_id = resp["agentRuntimeId"]
    except control.exceptions.ConflictException:
        logger.info("Runtime %s already exists — reusing", runtime_name)
        runtime_arn, runtime_id = _reuse_existing_runtime(control, runtime_name)

    encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
    runtime_mcp_url = (
        f"https://bedrock-agentcore.{inp.region}.amazonaws.com/"
        f"runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )
    logger.info("Created runtime %s (%s) for %s", runtime_name, runtime_id, inp.system_id)
    return CreateRuntimeResult(
        system_id=inp.system_id, runtime_arn=runtime_arn, runtime_id=runtime_id,
        runtime_mcp_url=runtime_mcp_url,
        connection_secret_arn=connection_secret_arn)


# --------------------------------------------------------------------------- #
# 3. wait_ready
# --------------------------------------------------------------------------- #
def version_reached(have: str, want: str) -> bool:
    """``have`` is the runtime version ``want`` or a later one.

    AgentCore runtime versions are decimal strings (``[1-9][0-9]{0,4}``); a
    value that is not one only matches itself, never "later".
    """
    if have == want:
        return True
    if not (have.isascii() and want.isascii() and have.isdigit() and want.isdigit()):
        return False
    return int(have) > int(want)


def wait_ready(
    inp: WaitReadyInput,
    *,
    control=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
    deadline_seconds: float = _READY_DEADLINE_SECONDS,
    poll_interval: float = 5.0,
    min_version: Optional[str] = None,
) -> WaitReadyResult:
    """Poll the runtime until ``READY`` (relocated from _start_cloud's 300s poll).

    A terminal ``CREATE_FAILED``/``UPDATE_FAILED`` is surfaced as a hard
    :class:`RuntimeCreateFailedError` carrying the AgentCore ``failureReason``
    verbatim (no AZ self-heal here — that imperative retry stays in the in-app
    ``_start_cloud``; the declarative path relies on the AZ-allowlist deploy
    prerequisite). Exceeding the deadline raises :class:`RuntimeNotReadyError`.

    ``min_version`` (an update's returned ``agentRuntimeVersion``): READY counts
    only at that version or a later one, because a read straight after an
    update can still describe the previous version as READY. A READY read with
    no version counts (nothing to compare). ``None``/empty: the first READY
    counts, unchanged.
    """
    control = control or _control_client(inp.region)
    start = monotonic()
    last_status = ""
    stale_polls = 0
    while True:
        status_resp = control.get_agent_runtime(agentRuntimeId=inp.runtime_id)
        status = status_resp.get("status", "")
        version = str(status_resp.get("agentRuntimeVersion") or "")
        last_status = status
        if status == "READY":
            if not min_version or not version or version_reached(version, min_version):
                return WaitReadyResult(runtime_id=inp.runtime_id, status=status,
                                       version=version, stale_polls=stale_polls)
            stale_polls += 1
            last_status = f"READY at version {version}, waiting for version {min_version}"
        if status in _RUNTIME_FAILED_STATES:
            reason = status_resp.get("failureReason", "")
            raise RuntimeCreateFailedError(inp.runtime_id, reason)
        if monotonic() - start > deadline_seconds:
            raise RuntimeNotReadyError(inp.runtime_id, last_status, deadline_seconds)
        logger.info("Waiting for runtime %s (status=%s)...", inp.runtime_id, last_status)
        sleep(poll_interval)


# --------------------------------------------------------------------------- #
# 4. create_target
# --------------------------------------------------------------------------- #
def _target_create_kwargs(inp: CreateTargetInput, target_name: str) -> dict:
    """The full ``create_gateway_target`` kwargs: the MCP target config at the
    runtime's endpoint plus the outbound OAuth (``adt-m2m``) credential
    provider config when one is given — extracted verbatim from
    ``create_target``."""
    create_kwargs = {
        "gatewayIdentifier": inp.gateway_id,
        "name": target_name,
        # Additive (runtime-half Task 1): an explicit description wins; empty
        # preserves the historical per-system text.
        "description": inp.description or f"System agent for {inp.system_name}",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": inp.runtime_mcp_url}}},
    }
    if inp.credential_provider_arn and inp.m2m_scopes:
        create_kwargs["credentialProviderConfigurations"] = [
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": inp.credential_provider_arn,
                        "scopes": list(inp.m2m_scopes),
                        "grantType": "CLIENT_CREDENTIALS",
                    }
                },
            }
        ]
    return create_kwargs


#: Settle budget for a conflict-reused target that had to be updated (polls x
#: seconds); well inside the state's 2-minute task timeout.
_TARGET_UPDATE_POLLS = 12
_TARGET_UPDATE_POLL_SECONDS = 5.0


def _reuse_conflicting_target(control, target_name: str, create_kwargs: dict,
                              *, sleep) -> str:
    """ConflictException reuse: find the existing target by name and make it
    match this provision before handing it on. A target left pointing at a
    deleted runtime still reads READY, so one whose endpoint or credential
    config differs (``gateway_target_drift``) is updated in place; the next
    state synchronizes it either way. Fails fast when no target with that name
    exists, and raises :class:`TargetUpdateError` when the update does not
    take (the target is never deleted here)."""
    gateway_id = create_kwargs["gatewayIdentifier"]
    gateway_target_id = ""
    for t in _list_all_gateway_targets(control, gateway_id):
        if t.get("name") == target_name:
            gateway_target_id = t.get("targetId", "")
            break
    if not gateway_target_id:
        # Fail fast: returning an empty id would only defer the failure to
        # synchronize (with no target to sync). No silent empty-id fallback.
        raise ProvisionerError(
            f"create_gateway_target raised ConflictException for "
            f"{target_name!r} but no existing target with that name was found "
            f"in list_gateway_targets; cannot reuse — failing fast."
        )
    detail = control.get_gateway_target(gatewayIdentifier=gateway_id,
                                        targetId=gateway_target_id)
    drift = _gateway_target_drift(create_kwargs, detail)
    if not drift:
        logger.info("Gateway target %s already exists and matches — reusing", target_name)
        return gateway_target_id
    logger.warning("Gateway target %s exists but does not match this runtime "
                   "(differs: %s) — updating it in place", target_name, ", ".join(drift))
    try:
        _bring_gateway_target_in_line(
            control, gateway_target_id, create_kwargs, detail, sleep=sleep,
            poll_seconds=_TARGET_UPDATE_POLL_SECONDS, max_polls=_TARGET_UPDATE_POLLS)
    except GatewayTargetUpdateError as exc:
        raise TargetUpdateError(str(exc)) from exc
    return gateway_target_id


def create_target(inp: CreateTargetInput, *, control=None,
                  sleep=time.sleep) -> CreateTargetResult:
    """Create (or reuse) the gateway target pointing at the runtime MCP endpoint.

    Relocates the create_gateway_target body of ``_start_cloud``: builds the MCP
    target config and the outbound OAuth (``adt-m2m``) credential
    provider config, and creates the target. On ``ConflictException`` it looks up
    the existing target and reuses its id (idempotent), first updating it when
    it does not point at this runtime (:func:`_reuse_conflicting_target`).
    Errors propagate.
    """
    control = control or _control_client(inp.region)
    target_name = inp.system_id.replace("_", "-")[:100]
    create_kwargs = _target_create_kwargs(inp, target_name)
    try:
        resp = control.create_gateway_target(**create_kwargs)
        gateway_target_id = resp.get("targetId", "")
    except control.exceptions.ConflictException:
        logger.info("Gateway target %s already exists — checking it", target_name)
        gateway_target_id = _reuse_conflicting_target(
            control, target_name, create_kwargs, sleep=sleep)

    return CreateTargetResult(system_id=inp.system_id, gateway_target_id=gateway_target_id)


# --------------------------------------------------------------------------- #
# 5. synchronize
# --------------------------------------------------------------------------- #
def synchronize(
    inp: SynchronizeInput,
    *,
    control=None,
    sleep=time.sleep,
    max_attempts: int = 10,
    creating_backoff: float = 5.0,
    unsuccessful_backoff: float = 15.0,
) -> SynchronizeResult:
    """Synchronize the gateway target, with the Creating-state retry from _start_cloud.

    Retries while the target is still in ``Creating`` state (short backoff) and
    on ``SYNCHRONIZE_UNSUCCESSFUL`` (longer backoff — the runtime may still be
    warming). Exhausting the retry budget raises :class:`GatewaySyncError`
    (errors propagate; no silent give-up).
    """
    control = control or _control_client(inp.region)
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            control.synchronize_gateway_targets(
                gatewayIdentifier=inp.gateway_id,
                targetIdList=[inp.gateway_target_id],
            )
            logger.info("Gateway target %s synced", inp.gateway_target_id)
            return SynchronizeResult(
                gateway_target_id=inp.gateway_target_id, synchronized=True
            )
        except Exception as sync_err:  # noqa: BLE001 - classified + re-raised below
            last_error = sync_err
            msg = str(sync_err)
            if attempt >= max_attempts - 1:
                break
            if "Creating state" in msg:
                sleep(creating_backoff)
            elif "SYNCHRONIZE_UNSUCCESSFUL" in msg or "unsuccessful" in msg.lower():
                sleep(unsuccessful_backoff)
            else:
                # An unexpected error — do not keep retrying blindly; propagate.
                raise
    raise GatewaySyncError(
        f"Gateway target {inp.gateway_target_id!r} did not synchronize after "
        f"{max_attempts} attempts: {last_error}"
    )


# --------------------------------------------------------------------------- #
# 6. verify
# --------------------------------------------------------------------------- #
def verify(inp: VerifyInput, *, control=None) -> VerifyResult:
    """Verify the gateway target reached ``READY`` after synchronization.

    Reads the target's status; a non-``READY`` status raises
    :class:`TargetNotReadyError` (errors propagate).
    """
    control = control or _control_client(inp.region)
    detail = control.get_gateway_target(
        gatewayIdentifier=inp.gateway_id, targetId=inp.gateway_target_id
    )
    status = detail.get("status", "")
    if status != "READY":
        raise TargetNotReadyError(
            f"Gateway target {inp.gateway_target_id!r} is in status {status!r}, "
            f"not READY after synchronize."
        )
    return VerifyResult(gateway_target_id=inp.gateway_target_id, status=status)


# --------------------------------------------------------------------------- #
# 7. write_terminal_status
# --------------------------------------------------------------------------- #
def write_terminal_status(
    inp: WriteTerminalStatusInput, *, store: OnboardingSessionStore | None = None
) -> WriteTerminalStatusResult:
    """Write the terminal session status (``onboarded`` | ``failed``).

    The success state machine path calls this with ``status='onboarded'``; the
    catch path (any upstream handler error) calls it with ``status='failed'`` and
    the surfaced ``failure_reason``. Delegates to
    ``OnboardingSessionStore.finalize`` (the conditional ``provisioning ->
    terminal`` write). ``finalize`` validates the status value and raises on a
    non-terminal status (propagated).
    """
    if inp.status not in (STATUS_ONBOARDED, STATUS_FAILED):
        raise ValueError(
            f"write_terminal_status status must be {STATUS_ONBOARDED!r} or "
            f"{STATUS_FAILED!r}, got {inp.status!r}"
        )
    store = store or OnboardingSessionStore()
    fields = {}
    if inp.status == STATUS_FAILED and inp.failure_reason:
        fields["failure_reason"] = inp.failure_reason
    store.finalize(inp.system_id, inp.status, **fields)
    logger.info("Onboarding session %s finalized: %s", inp.system_id, inp.status)
    return WriteTerminalStatusResult(system_id=inp.system_id, status=inp.status)
