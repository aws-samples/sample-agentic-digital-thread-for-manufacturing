#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""provision_runtimes.py — the ONE engine runtime-provisioning driver.

Extraction of deploy.sh §2c/§2e CONTROL FLOW (list/create-or-update, AZ-strip
retry, READY polls, onboarding gateway target + sync) into a single Python
driver with two consumers: deploy.sh (Wave 2 cutover) and the workshop
engine-bootstrap custom resource (its handler delegates here — the code is
vendored into the CodeBuild buildspec at synth time).

``refresh_system_runtimes`` does deploy.sh [2f]'s job (move every per-system
``adt_*`` runtime to the current image) for the custom resource's Update.
deploy.sh keeps its own bash [2f], which its behaviour tests drive through a
fake ``aws`` CLI; both follow the same rules and are pinned by matching tests.

Built ON the shared library layer — never mirrored:

* env — ``agents.runtime_env_contract.build_env`` (the ONE contract; the CLI
  reads contract inputs from the ambient process env via
  ``RuntimeEnvSources.from_ambient``, read-only);
* update kwargs — ``agents.runtime_provision_helpers.runtime_update_kwargs``
  (ADR-0056 full-replacement protocol round-trip; the MCP ``protocol_override``
  is applied ONLY for a spec with ``protocol_mcp=True`` — the orchestrator is
  legitimately HTTP);
* enumeration — ``list_all_agent_runtimes`` (pagination-safe: the bash
  ``--query`` on one page silently truncated past 10 runtimes);
* READY / target semantics — ``agents.provisioner.handlers``
  (``wait_ready`` / ``create_target`` / ``synchronize`` / ``verify``).

Deliberate behavior deltas vs the bash (plan, golden-pinned): pagination-safe
enumeration; update failures and non-AZ create failures are FATAL (the bash
warned-and-continued); required-output pre-checks STAY in deploy.sh; §2c-eval
SSM persist STAYS in deploy.sh (§2d ECS restart retired 2026-09-10 — config.py re-attempts discovery).

Contract rules: import closure = stdlib + boto3 + the agents leaf modules only
(never ``agents.config``/strands); ``os.environ`` is NEVER mutated (JobStore
local-dev gate); all clients injectable, keyword-only, no module-level boto3
client; the CLI emits EXACTLY one JSON object on stdout, diagnostics to
stderr, nonzero exit on ANY failure.

Usage (from ``agentic_digital_thread/``, contract inputs exported like the
Phase 0 shim invocations)::

    PYTHONPATH=. python3 scripts/provision_runtimes.py --role orchestrator \
        --region <r> --image <uri> --role-arn <arn> --subnets s-1,s-2 --sg sg-1
    PYTHONPATH=. python3 scripts/provision_runtimes.py --role onboarding \
        ... --gateway-id <gw> --m2m-discovery-url <u> --m2m-client-id <c> \
        --provider-arn <arn>
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from dataclasses import dataclass, field

import boto3

from agents.provisioner.errors import RuntimeCreateFailedError
from agents.provisioner.handlers import (
    create_target,
    synchronize,
    verify,
    version_reached,
    wait_ready,
)
from agents.provisioner.models import (
    CreateTargetInput,
    SynchronizeInput,
    VerifyInput,
    WaitReadyInput,
)
from agents.runtime_env_contract import (
    SYSTEM_GUIDANCE_TABLE_DEFAULT,
    RuntimeEnvSources,
    build_env,
    reconcile_env,
)
from agents.runtime_provision_helpers import (
    iter_agent_runtime_pages,
    list_all_agent_runtimes,
    network_config,
    runtime_update_kwargs,
)

ORCHESTRATOR_RUNTIME_NAME = "adt_orchestrator"
ONBOARDING_RUNTIME_NAME = "adt_onboarding"
ADT_RUNTIME_PREFIX = "adt_"

# Onboarding gateway target contract (deploy.sh §2e — step-parity pinned).
ONBOARDING_TARGET_NAME = "onboarding"
ONBOARDING_TARGET_DESCRIPTION = (
    "Onboarding MCP tools (single target; JWT auth, Cedar not attached)"
)
M2M_SCOPES = ("mcp/tools.read", "mcp/tools.execute")
MCP_PROTOCOL = {"serverProtocol": "MCP"}

# Step-parity wait budgets (deploy.sh §2c/§2e cadences — fake-clock pinned).
CREATE_POLL_DEADLINE_SECONDS = 300.0  # 60 × 5 s
#: The deploy.sh [2c]/[2e] command line waits longer: a runtime create took
#: 5 to 9 minutes on a fresh deploy and the 300 s poll stopped the deploy.
#: The workshop bootstrap keeps 300 s; its CodeBuild budget is sized on it.
CLI_CREATE_POLL_DEADLINE_SECONDS = 900.0  # 180 × 5 s
CREATE_POLL_INTERVAL_SECONDS = 5.0
UPDATE_POLL_DEADLINE_SECONDS = 300.0  # 30 × 10 s
UPDATE_POLL_INTERVAL_SECONDS = 10.0
#: AgentCore runtime deletion is slow and variable: 120 s (the original
#: 24 × 5 s parity) expired with both engine runtimes still present in a live
#: workshop teardown (2026-08-21), and their service-managed ENIs took far
#: longer still. Budget generously — this wait exists so runtimes are gone
#: before CloudFormation tears the subnets down.
DELETE_WAIT_DEADLINE_SECONDS = 600.0  # 120 × 5 s
DELETE_WAIT_INTERVAL_SECONDS = 5.0
AZ_STRIP_MAX_ATTEMPTS = 3
TARGET_WARMUP_SECONDS = 30

_AZ_FAILURE_NEEDLE = "availability zone"
_SUBNET_ID_RE = re.compile(r"subnet-[0-9a-fA-F]+")


class ProvisionRuntimeError(RuntimeError):
    """Fatal driver error (fail-loud — the CLI exits nonzero on it)."""


@dataclass(frozen=True)
class RuntimeSpec:
    """One engine runtime to ensure (orchestrator | onboarding)."""

    name: str  # "adt_orchestrator" | "adt_onboarding"
    image_uri: str
    role_arn: str
    subnet_ids: list[str]
    security_group_id: str
    env: dict[str, str] = field(default_factory=dict)
    protocol_mcp: bool = False  # onboarding only (ADR-0056: never global)
    authorizer: dict | None = None  # onboarding M2M JWT only

    @property
    def artifact(self) -> dict:
        """The ``agentRuntimeArtifact`` block (deploy.sh *_ARTIFACT parity)."""
        return {"containerConfiguration": {"containerUri": self.image_uri}}


def _log(msg: str) -> None:
    """Diagnostics go to stderr ONLY — stdout carries exactly one JSON object."""
    print(msg, file=sys.stderr)


def _is_not_found(exc: Exception) -> bool:
    """NotFound classification for both botocore ClientError and hand stubs."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
    return "NotFound" in (code or type(exc).__name__)


def strip_unsupported_subnets(
    subnets: list[str], failure_reason: str
) -> list[str] | None:
    """PURE AZ-strip planner (deploy.sh §2c/§2e grep parity).

    Parses the AgentCore ``failureReason`` for unsupported-AZ subnet ids and
    returns the surviving subnets. Returns ``None`` for a non-AZ failure —
    the caller must NOT retry (fatal, per the plan's tightening delta)."""
    if _AZ_FAILURE_NEEDLE not in (failure_reason or "").lower():
        return None
    bad = set(_SUBNET_ID_RE.findall(failure_reason or ""))
    return [s for s in subnets if s not in bad]


def _find_runtime_arn(control, name: str) -> str:
    """The runtime ARN for *name*, "" when absent (pagination-safe)."""
    for rt in list_all_agent_runtimes(control):
        if rt.get("agentRuntimeName") == name:
            return rt.get("agentRuntimeArn", "")
    return ""


def _update_runtime(
    spec: RuntimeSpec, arn: str, *, control, region, sleep, monotonic
) -> dict:
    """Update-in-place (§2c/§2e update branch) + READY wait — FATAL on failure.

    Kwargs are built ONLY through ``runtime_update_kwargs`` (project
    invariant): protocol round-trips from the details, EXCEPT the MCP
    ``protocol_override`` for a ``protocol_mcp`` spec — round-trip alone would
    perpetuate a previously-wiped HTTP state (ADR-0056 repair semantics). A
    ``None`` authorizer round-trips the details; a spec authorizer (onboarding
    M2M JWT) is authoritative — mirroring the bash's explicit flags."""
    runtime_id = arn.rsplit("/", 1)[-1]
    details = control.get_agent_runtime(agentRuntimeId=runtime_id)
    kwargs = runtime_update_kwargs(
        runtime_id,
        details,
        dict(spec.env),
        artifact=spec.artifact,
        role_arn=spec.role_arn,
        authorizer_config=spec.authorizer,
        protocol_override=dict(MCP_PROTOCOL) if spec.protocol_mcp else None,
    )
    resp = control.update_agent_runtime(**kwargs) or {}
    sent = str(resp.get("agentRuntimeVersion") or "")
    if sent:
        _log(f"{spec.name}: update accepted (version {sent}) — waiting for READY...")
    else:
        _log(f"{spec.name}: update accepted (no version returned; its first READY "
             f"counts) — waiting for READY...")
    # READY counts only at the version this update returned (or a later one):
    # a read straight after the update can still describe the previous
    # version as READY, which is not this update finishing.
    ready = wait_ready(
        WaitReadyInput(runtime_id=runtime_id, region=region),
        control=control,
        sleep=sleep,
        monotonic=monotonic,
        deadline_seconds=UPDATE_POLL_DEADLINE_SECONDS,
        poll_interval=UPDATE_POLL_INTERVAL_SECONDS,
        min_version=sent or None,
    )
    _log(f"{spec.name}: READY at version {ready.version or 'unknown'} (update returned "
         f"{sent or 'no version'}; {ready.stale_polls} poll(s) still showed the "
         f"previous version)")
    if sent and ready.version and ready.version != sent:
        # A later version: another writer updated the runtime after this one.
        # It counts only if it still runs this deploy's image.
        image = _runtime_image(control, runtime_id)
        if image != spec.image_uri:
            raise ProvisionRuntimeError(
                f"{spec.name}: a later update (version {ready.version}) runs "
                f"{image or 'an unreadable image'}, not {spec.image_uri}")
    _log(f"{spec.name}: ✅ updated to current image")
    return {"arn": arn, "runtime_id": runtime_id, "action": "updated"}


def _runtime_image(control, runtime_id: str) -> str:
    """The container image the runtime runs now ("" when unreadable)."""
    return _container_uri(control.get_agent_runtime(agentRuntimeId=runtime_id))


def _container_uri(details: dict) -> str:
    """The container image in ``get_agent_runtime`` *details* ("" when absent)."""
    container = ((details.get("agentRuntimeArtifact") or {})
                 .get("containerConfiguration") or {})
    return str(container.get("containerUri") or "")


def _create_kwargs(spec: RuntimeSpec, subnets: list[str]) -> dict:
    """``create_agent_runtime`` kwargs (§2c/§2e create-call parity):
    protocol/authorizer blocks appear ONLY when the spec carries them."""
    kwargs = {
        "agentRuntimeName": spec.name,
        "agentRuntimeArtifact": spec.artifact,
        "roleArn": spec.role_arn,
        "networkConfiguration": network_config(
            list(subnets), spec.security_group_id
        ),
        "environmentVariables": dict(spec.env),
    }
    if spec.protocol_mcp:
        kwargs["protocolConfiguration"] = dict(MCP_PROTOCOL)
    if spec.authorizer:
        kwargs["authorizerConfiguration"] = spec.authorizer
    return kwargs


def _wait_runtime_gone(control, runtime_id: str, *, sleep) -> bool:
    """Poll until the runtime is deleted (24 × 5 s parity). NotFound ⇒ gone;
    any other error propagates. False ⇒ it survived the wait budget."""
    attempts = int(DELETE_WAIT_DEADLINE_SECONDS / DELETE_WAIT_INTERVAL_SECONDS)
    for _ in range(attempts):
        try:
            control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_not_found(exc):
                return True
            raise
        sleep(DELETE_WAIT_INTERVAL_SECONDS)
    return False


#: Error codes and exception class names a sweep retries inside its budget:
#: throttles and service-side 5xx that outlast the SDK's own retries, and the
#: botocore transport errors a short client timeout raises. Any ClientError
#: whose ResponseMetadata.HTTPStatusCode is 5xx or 429 is also retried,
#: whatever its code string (see _is_retryable).
_RETRYABLE_CODES = frozenset({
    "ThrottlingException", "Throttling", "TooManyRequestsException",
    "RequestLimitExceeded", "ServiceUnavailableException",
    "InternalServerException", "InternalFailure", "ServiceUnavailable",
})
_RETRYABLE_CLASS_NAMES = frozenset({
    "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
    "ConnectionClosedError",
})


def _is_retryable(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
        if code in _RETRYABLE_CODES:
            return True
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if isinstance(status, int) and (500 <= status < 600 or status == 429):
            return True
    return type(exc).__name__ in _RETRYABLE_CLASS_NAMES


def _delete_failed_runtime(control, runtime_id: str, *, sleep) -> None:
    """Delete a failed create before any retry (§2c/§2e parity: a survivor
    after the wait budget does not abort — the retry proceeds, as the bash
    did)."""
    try:
        control.delete_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as exc:  # noqa: BLE001 - classified below
        if not _is_not_found(exc):
            raise
    _wait_runtime_gone(control, runtime_id, sleep=sleep)


def _create_runtime_with_az_strip(
    spec: RuntimeSpec, *, control, region, sleep, monotonic,
    create_poll_seconds=CREATE_POLL_DEADLINE_SECONDS,
) -> dict:
    """Create with the ≤3-attempt AZ-strip retry loop (§2c/§2e create branch).

    A terminal CREATE_FAILED deletes the failed runtime (+ delete-wait) and
    retries with the offending subnets stripped; a non-AZ failure and a READY
    timeout are FATAL (``RuntimeNotReadyError`` propagates)."""
    subnets = list(spec.subnet_ids)
    reason = ""
    for attempt in range(1, AZ_STRIP_MAX_ATTEMPTS + 1):
        if not subnets:
            raise ProvisionRuntimeError(
                f"{spec.name}: no subnets left to try after stripping "
                f"unsupported AZs (last failure: {reason or 'none'})"
            )
        resp = control.create_agent_runtime(**_create_kwargs(spec, subnets))
        runtime_id = resp["agentRuntimeId"]
        arn = resp["agentRuntimeArn"]
        try:
            wait_ready(
                WaitReadyInput(runtime_id=runtime_id, region=region),
                control=control,
                sleep=sleep,
                monotonic=monotonic,
                deadline_seconds=create_poll_seconds,
                poll_interval=CREATE_POLL_INTERVAL_SECONDS,
            )
            _log(f"{spec.name}: ✅ created ({arn})")
            return {"arn": arn, "runtime_id": runtime_id, "action": "created"}
        except RuntimeCreateFailedError as exc:
            reason = exc.failure_reason
            _log(f"{spec.name}: CREATE_FAILED (attempt {attempt}): {reason}")
            _delete_failed_runtime(control, runtime_id, sleep=sleep)
            remaining = strip_unsupported_subnets(subnets, reason)
            if remaining is None:
                raise ProvisionRuntimeError(
                    f"{spec.name}: non-AZ create failure — not retrying: "
                    f"{reason}"
                ) from exc
            _log(
                f"{spec.name}: stripped unsupported subnet(s) "
                f"{sorted(set(subnets) - set(remaining))} — retrying"
            )
            subnets = remaining
    raise ProvisionRuntimeError(
        f"{spec.name}: create failed after {AZ_STRIP_MAX_ATTEMPTS} AZ-strip "
        f"attempts (last failure: {reason})"
    )


def ensure_runtime(
    spec: RuntimeSpec,
    *,
    control,
    region: str,
    sleep=time.sleep,
    monotonic=time.monotonic,
    create_poll_seconds: float = CREATE_POLL_DEADLINE_SECONDS,
) -> dict:
    """Ensure the runtime exists on the current image/env (idempotent).

    Exists ⇒ full-replacement update via ``runtime_update_kwargs`` + READY
    wait (fatal on timeout/FAILED); absent ⇒ create with AZ-strip retry (≤3)
    + a READY wait of *create_poll_seconds* per attempt. Returns
    ``{"arn":…, "runtime_id":…, "action": "created"|"updated"}``."""
    arn = _find_runtime_arn(control, spec.name)
    if arn:
        _log(f"{spec.name}: exists ({arn}) — updating image + env")
        return _update_runtime(
            spec, arn, control=control, region=region,
            sleep=sleep, monotonic=monotonic,
        )
    _log(f"{spec.name}: creating (VPC mode, AZ-strip retry)")
    return _create_runtime_with_az_strip(
        spec, control=control, region=region, sleep=sleep, monotonic=monotonic,
        create_poll_seconds=create_poll_seconds,
    )


def _runtime_mcp_url(region: str, runtime_arn: str) -> str:
    """The runtime's MCP invocation endpoint (§2e ENCODED_ARN parity)."""
    encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/"
            f"runtimes/{encoded_arn}/invocations?qualifier=DEFAULT")


def ensure_onboarding_target(
    *,
    control,
    gateway_id: str,
    runtime_arn: str,
    provider_arn: str,
    region: str,
    warmup_seconds: int = TARGET_WARMUP_SECONDS,
    sleep=time.sleep,
) -> str:
    """Create/sync/verify the SINGLE onboarding gateway target (§2e parity).

    Reuses ``handlers.create_target`` (ConflictException reuse-by-name),
    ``synchronize`` (10 attempts, 5 s/15 s backoffs) and ``verify``. The
    warmup mirrors agent_manager's pre-sync wait. Returns the target id."""
    result = create_target(
        CreateTargetInput(
            system_id=ONBOARDING_TARGET_NAME,
            system_name=ONBOARDING_TARGET_NAME,
            gateway_id=gateway_id,
            runtime_mcp_url=_runtime_mcp_url(region, runtime_arn),
            region=region,
            credential_provider_arn=provider_arn,
            m2m_scopes=list(M2M_SCOPES),
            description=ONBOARDING_TARGET_DESCRIPTION,
        ),
        control=control,
        sleep=sleep,
    )
    target_id = result.gateway_target_id
    if warmup_seconds:
        _log(f"waiting {warmup_seconds}s for runtime warmup before gateway sync")
        sleep(warmup_seconds)
    _wait_target_created(
        control, gateway_id=gateway_id, target_id=target_id, sleep=sleep
    )
    _synchronize_until_ready(
        control, gateway_id=gateway_id, target_id=target_id, region=region,
        sleep=sleep,
    )
    verify(
        VerifyInput(
            gateway_id=gateway_id, gateway_target_id=target_id, region=region
        ),
        control=control,
    )
    _log(f"onboarding gateway target synced ✅ ({target_id})")
    return target_id


#: Re-synchronize budget for a target that settles SYNCHRONIZE_UNSUCCESSFUL.
#: A gateway sync fetches the runtime's MCP tool list, and a freshly created
#: runtime reports READY before its MCP endpoint serves — live-observed in a
#: cold account (workshop CR, 2026-08-21): one 30 s warmup + a single
#: synchronize landed SYNCHRONIZE_UNSUCCESSFUL and the driver aborted the
#: whole deploy. Interactive deploy.sh users simply re-ran §2e; unattended
#: provisioning has to do that itself. Backoffs are generous because the
#: alternative is a failed stack.
SYNC_RETRY_WAITS = (30, 60, 90, 120)


def _target_diagnosis(detail: dict) -> str:
    """The service's own explanation for a target's state, for logs.

    ``statusReasons`` is where AgentCore puts the actual cause of a
    ``SYNCHRONIZE_UNSUCCESSFUL`` target (auth rejected, endpoint unreachable,
    tool-list error). Keys are read defensively — this runs inside a CR where
    a missing field must never mask the real failure.
    """
    reasons = detail.get("statusReasons") or detail.get("statusReason") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    parts = [f"status={detail.get('status', '?')}"]
    if reasons:
        parts.append("reasons=" + " | ".join(str(r)[:300] for r in reasons))
    else:
        # No reasons field: dump the non-config keys so the log still carries
        # something actionable (never the credential config).
        parts.append(
            "no statusReasons; keys=" + ",".join(sorted(detail)) or "none"
        )
    return "; ".join(parts)


def _synchronize_until_ready(
    control, *, gateway_id: str, target_id: str, region: str, sleep=time.sleep,
) -> None:
    """Synchronize, settle, and RE-synchronize while the target reports
    ``SYNCHRONIZE_UNSUCCESSFUL`` (the runtime's MCP endpoint is not serving
    yet). Any other terminal status falls through to ``verify()``'s honest
    failure, exactly as before."""
    attempt = 0
    while True:
        synchronize(
            SynchronizeInput(
                gateway_id=gateway_id, gateway_target_id=target_id, region=region
            ),
            control=control,
            sleep=sleep,
        )
        _wait_target_settled(
            control, gateway_id=gateway_id, target_id=target_id, sleep=sleep
        )
        detail = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        status = detail.get("status", "")
        if status != TARGET_SYNC_FAILED_STATUS:
            return
        # Unattended provisioning must say WHY, not just that it failed: the
        # service carries the diagnosis in statusReasons (live triage of a
        # SYNCHRONIZE_UNSUCCESSFUL target, 2026-08-24, had to guess without
        # this). Logged on EVERY failed attempt — a transient first reason and
        # a persistent last one are different problems.
        _log(f"target {target_id} sync failure detail: {_target_diagnosis(detail)}")
        if attempt >= len(SYNC_RETRY_WAITS):
            _log(
                f"target {target_id} still {status} after "
                f"{len(SYNC_RETRY_WAITS)} re-syncs — verifying anyway"
            )
            return
        wait = SYNC_RETRY_WAITS[attempt]
        attempt += 1
        _log(
            f"target {target_id} is {status} (runtime MCP endpoint likely not "
            f"serving yet) — waiting {wait}s, then re-synchronizing "
            f"({attempt}/{len(SYNC_RETRY_WAITS)})"
        )
        sleep(wait)


#: Transient statuses a just-synchronized target passes through before READY.
TARGET_SETTLE_TRANSIENT = ("SYNCHRONIZING", "CREATING", "UPDATING")
#: Terminal-but-retryable: the sync ran and failed (commonly because the
#: runtime's MCP endpoint was not serving yet). Distinct from a permanently
#: broken target, which verify() reports honestly.
TARGET_SYNC_FAILED_STATUS = "SYNCHRONIZE_UNSUCCESSFUL"
#: Bounded settle-wait budget between synchronize and verify (seconds).
TARGET_SETTLE_MAX_SECONDS = 120
TARGET_SETTLE_POLL_SECONDS = 10


#: A freshly created gateway target can sit in ``CREATING`` for minutes in a
#: cold account. ``synchronize`` rejects that state and its own retry covers
#: only ~45 s (10 × 5 s), which a Workshop Studio test event exhausted on
#: 2026-09-12 (target still CREATING 75 s after creation → GatewaySyncError →
#: root CREATE_FAILED after 9 green nested stacks). Wait it out first, bounded
#: generously: the alternative is a failed stack, and the CR has a 55-min
#: budget of which this step normally uses seconds.
TARGET_CREATE_MAX_SECONDS = 600
TARGET_CREATE_POLL_SECONDS = 10


def _wait_target_created(
    control, *, gateway_id: str, target_id: str, sleep=time.sleep
) -> None:
    """Block while the target reports ``CREATING`` (bounded), logging the
    wait, so the first synchronize is issued against a target the service
    will accept. Any other status returns immediately — READY proceeds to
    sync, a failed state falls through to synchronize/verify's honest
    error."""
    waited = 0
    while waited < TARGET_CREATE_MAX_SECONDS:
        detail = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        status = detail.get("status", "")
        if status != "CREATING":
            if waited:
                _log(f"target {target_id} left CREATING after {waited}s (status={status})")
            return
        if waited == 0:
            _log(f"target {target_id} is CREATING — waiting up to {TARGET_CREATE_MAX_SECONDS}s")
        sleep(TARGET_CREATE_POLL_SECONDS)
        waited += TARGET_CREATE_POLL_SECONDS
    _log(f"target {target_id} still CREATING after {waited}s — attempting sync anyway")


def _wait_target_settled(
    control, *, gateway_id: str, target_id: str, sleep=time.sleep
) -> None:
    """Poll the target through TRANSIENT statuses before the strict verify().

    ``verify`` is deliberately single-shot (fail-loud); but a target that was
    just synchronized commonly reads ``SYNCHRONIZING`` for a few seconds
    (live-observed 2026-08-05 — it settled READY moments after the driver
    aborted). Wait THROUGH the transient states only, bounded; any terminal
    non-READY status falls straight through to verify()'s honest failure."""
    waited = 0
    while waited < TARGET_SETTLE_MAX_SECONDS:
        detail = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        if detail.get("status", "") not in TARGET_SETTLE_TRANSIENT:
            return
        sleep(TARGET_SETTLE_POLL_SECONDS)
        waited += TARGET_SETTLE_POLL_SECONDS
    _log(f"target {target_id} still transient after {waited}s — verifying anyway")


def sweep_adt_runtimes(
    *, control, region: str, gateway_ids: list[str], sleep=time.sleep,
    tolerate_survivors: bool = False, monotonic=None,
) -> list[str]:
    """DELETE path: remove ALL targets on the given gateways, then every
    ``adt_*`` runtime (prefix-scoped — non-``adt_`` runtimes are NEVER
    touched). Tolerates NotFound mid-sweep (CFN delete retries converge).

    Every runtime delete is issued before any wait, and the whole sweep runs
    on ONE wall-clock budget (``DELETE_WAIT_DEADLINE_SECONDS``, measured with
    ``monotonic`` so slow calls count against it). The deadline is checked
    BEFORE each call (delete, status read, each page of a listing), so once
    it is reached no further call starts and the overshoot is bounded by at
    most one in-flight call, not one call per runtime. A listing whose
    pagination token repeats fails loudly instead of paging forever. AgentCore takes about 5 minutes per
    runtime and a workshop account ends an event with a dozen or more: waited
    out one at a time they outlasted the bootstrap build's timeout and the
    custom resource never answered CloudFormation. A throttle or 5xx that
    outlasts the SDK's own retries — including the gateway-target and runtime
    listings — is retried inside the budget.

    ``tolerate_survivors`` decides what an UNFINISHED delete at the budget end
    means. Interactive callers (the CLI/deploy.sh) keep the strict default:
    raise, so a wedged delete is VISIBLE. The CloudFormation CR passes True,
    because raising there posts FAILED and wedges the stack in DELETE_FAILED,
    which is worse than proceeding — BUT only for a delete AgentCore actually
    ACCEPTED: that delete is progressing, and CloudFormation still has 20+
    minutes of nested-stack teardown ahead of the subnets it protects. A
    runtime or target whose delete was NEVER accepted (every call throttled
    or the budget ran out before it was issued) is NOT a tolerated survivor:
    returning SUCCESS would drop this resource from CloudFormation's
    cleanup-retry path while the runtime (and its ENIs) can still block subnet
    teardown or the target can still block gateway deletion. That always
    raises, even in tolerate mode. Survivors are logged either way."""
    del region  # part of the frozen interface; the injected client is bound
    if monotonic is None:
        if sleep is time.sleep:
            monotonic = time.monotonic
        else:  # a caller that fakes sleep only: its sleeps are the clock
            slept = [0.0]
            inner_sleep = sleep

            def sleep(seconds):  # noqa: F811 - wraps the injected sleep
                slept[0] += seconds
                inner_sleep(seconds)

            def monotonic():
                return slept[0]
    deadline = monotonic() + DELETE_WAIT_DEADLINE_SECONDS

    def spent() -> bool:
        return monotonic() >= deadline

    def attempt(call) -> str:
        """'deadline' (budget spent — call NOT started), 'ok', 'gone'
        (NotFound) or 'retry' (retryable). The deadline is checked BEFORE the
        call so a slow call cannot overshoot by more than one in-flight
        call."""
        if spent():
            return "deadline"
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_not_found(exc):
                return "gone"
            if _is_retryable(exc):
                _log(f"sweep: will retry after {type(exc).__name__}: {exc}")
                return "retry"
            raise
        return "ok"

    def list_within_budget(call):
        """Run ONE listing call (one page), retrying a retryable failure
        inside the budget (the SDK's own retries can be outlasted by a
        throttle). Returns the response, or None when the budget ran out
        before it succeeded."""
        while True:
            if spent():
                return None
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - classified below
                if _is_retryable(exc):
                    _log(f"sweep: will retry listing after "
                         f"{type(exc).__name__}: {exc}")
                    if spent():
                        return None
                    sleep(DELETE_WAIT_INTERVAL_SECONDS)
                    continue
                raise

    def paged_within_budget(fetch_page, items_of, what):
        """Every item across a paginated listing. Each PAGE goes through
        ``list_within_budget``, so the deadline is checked before every page
        call, not once around the whole listing. A nextToken seen before
        would page forever and fails loudly. None when the budget ran out
        before the last page."""
        items: list = []
        token = None
        seen: set[str] = set()
        while True:
            resp = list_within_budget(lambda: fetch_page(token))
            if resp is None:
                return None
            items.extend(items_of(resp))
            token = resp.get("nextToken")
            if not token:
                return items
            if token in seen:
                raise ProvisionRuntimeError(
                    f"{what} listing repeated a pagination token; "
                    f"stopping instead of paging forever")
            seen.add(token)

    def target_page(gateway_id, token):
        kwargs = {"gatewayIdentifier": gateway_id}
        if token:
            kwargs["nextToken"] = token
        return control.list_gateway_targets(**kwargs)

    def target_ids(resp):
        return [item["targetId"]
                for item in resp.get("items", resp.get("targets", []))
                if item.get("targetId")]

    def runtime_page(token):
        return control.list_agent_runtimes(**({"nextToken": token} if token else {}))

    def runtime_items(resp):
        return resp.get("agentRuntimes", resp.get("agentRuntimeSummaries", []))

    # --- gateway targets: delete every target, then every adt_* runtime --- #
    # A target delete that returns ok/gone is ACCEPTED (the target is gone or
    # going); one that only ever returns 'retry'/'deadline' was never accepted
    # and still blocks gateway deletion — a survivor that must raise.
    unaccepted_targets: list[str] = []
    for gateway_id in gateway_ids:
        # Every page is collected BEFORE deleting, so pagination tokens are
        # never invalidated mid-sweep.
        listed = paged_within_budget(
            lambda token: target_page(gateway_id, token), target_ids,
            f"gateway {gateway_id} target")
        if listed is None:
            _log(f"sweep: ran out of budget listing targets on {gateway_id}")
            continue
        targets = list(listed)
        while targets:
            left = []
            for target_id in targets:
                if attempt(lambda: control.delete_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=target_id)) in ("retry", "deadline"):
                    left.append(target_id)
            targets = left
            if not targets or spent():
                break
            sleep(DELETE_WAIT_INTERVAL_SECONDS)
        unaccepted_targets += [f"{gateway_id}/{t}" for t in targets]
    if unaccepted_targets:
        message = (f"gateway target deletes were never accepted: "
                   f"{unaccepted_targets}")
        # Never-accepted deletes block gateway deletion and must raise even in
        # tolerate mode — tolerating would drop the CR from CFN's retry path.
        raise ProvisionRuntimeError(message)

    listed_runtimes = paged_within_budget(runtime_page, runtime_items, "runtime")
    if listed_runtimes is None:
        raise ProvisionRuntimeError(
            "ran out of budget listing adt_* runtimes before any delete — "
            "the runtime sweep could not even start")
    names: dict[str, str] = {}
    for rt in listed_runtimes:
        name = rt.get("agentRuntimeName", "")
        if not name.startswith(ADT_RUNTIME_PREFIX):
            continue
        runtime_id = (
            rt.get("agentRuntimeId")
            or rt.get("agentRuntimeArn", "").rsplit("/", 1)[-1]
        )
        names[runtime_id] = name
    to_delete = list(names)
    waiting: list[str] = []
    accepted: set[str] = set()  # a delete AgentCore took (ok or already gone)
    gone: set[str] = set()
    _log(f"sweep: deleting {len(names)} runtime(s): {sorted(names.values())}")
    while True:
        retry_delete = []
        for runtime_id in to_delete:
            outcome = attempt(
                lambda: control.delete_agent_runtime(agentRuntimeId=runtime_id))
            if outcome in ("retry", "deadline"):
                retry_delete.append(runtime_id)
            else:
                accepted.add(runtime_id)
                if outcome == "gone":
                    gone.add(runtime_id)
                else:
                    waiting.append(runtime_id)
        to_delete = retry_delete
        still = []
        for runtime_id in waiting:
            if attempt(lambda: control.get_agent_runtime(
                    agentRuntimeId=runtime_id)) == "gone":
                gone.add(runtime_id)
            else:
                still.append(runtime_id)
        waiting = still
        if not (to_delete or waiting) or spent():
            break
        sleep(DELETE_WAIT_INTERVAL_SECONDS)
    deleted = [n for rid, n in names.items() if rid in gone]
    # A delete AgentCore never accepted blocks the subnets this sweep protects;
    # one it accepted that is still DELETING is progressing and tolerable.
    unaccepted = sorted(n for rid, n in names.items() if rid not in accepted)
    accepted_survivors = sorted(
        n for rid, n in names.items() if rid in accepted and rid not in gone)
    _log(f"sweep: {len(deleted)} of {len(names)} runtime(s) deleted")
    if unaccepted:
        # Always fatal — even tolerate_survivors must not drop a never-accepted
        # delete from CloudFormation's cleanup-retry path.
        raise ProvisionRuntimeError(
            f"runtime deletes were never accepted: {unaccepted}")
    if accepted_survivors:
        message = (f"runtimes survived the delete wait (delete accepted, "
                   f"still deleting): {accepted_survivors}")
        if not tolerate_survivors:
            raise ProvisionRuntimeError(message)
        _log(
            f"{message} — continuing so the stack teardown is not wedged "
            "(an accepted delete keeps going)"
        )
    return deleted


# --------------------------------------------------------------------------- #
# Per-system runtime refresh (deploy.sh [2f]; the engine bootstrap CR's Update)
# --------------------------------------------------------------------------- #
#: The engine runtimes have their own update step (``ensure_runtime``). Every
#: other ``adt_*`` runtime (one per onboarded system, plus the ones the app
#: creates itself) only moves to a new image through this refresh.
ENGINE_RUNTIME_NAMES = (ORCHESTRATOR_RUNTIME_NAME, ONBOARDING_RUNTIME_NAME)
#: One limit for the whole refresh, counted from the first update: the updates
#: and the READY wait after them share it (deploy.sh [2f]'s default). 900 s,
#: the same as the command-line create wait: an in-place update of six
#: runtimes can take longer than 600 s.
SYSTEM_REFRESH_DEADLINE_SECONDS = CLI_CREATE_POLL_DEADLINE_SECONDS
SYSTEM_REFRESH_POLL_SECONDS = 10.0
#: Listing pages read before a listing counts as runaway (an endless token).
SYSTEM_REFRESH_MAX_LIST_PAGES = 100
_REFRESH_WAIT_STATES = ("CREATING", "UPDATING")
_REFRESH_SETTLED_STATES = ("READY", "CREATE_FAILED", "UPDATE_FAILED")
_ERROR_TEXT_LIMIT = 240


@dataclass
class SystemRefreshResult:
    """What one per-system refresh did: the runtimes refreshed (READY on the
    image), the ones already READY on it (no update sent), and each runtime
    that was NOT refreshed with its reason, in the order they failed."""

    refreshed: list = field(default_factory=list)
    current: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)

    def failure_summary(self) -> str:
        """``name (reason), ...`` for every runtime that was not refreshed."""
        return ", ".join(f"{name} ({why})" for name, why in self.failed.items())


class _NotRefreshed(Exception):
    """One runtime cannot be refreshed; the message is the reason."""


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str((resp.get("Error") or {}).get("Code") or "")
    return ""


def _error_text(exc: Exception) -> str:
    """A bounded one-line description of a control-plane error."""
    code = _error_code(exc) or type(exc).__name__
    text = str(exc)
    if code not in text:
        text = f"{code}: {text}"
    return text[:_ERROR_TEXT_LIMIT]


def _refresh_clock(sleep, monotonic):
    """``(sleep, monotonic)`` for the refresh's time limit. A caller that
    fakes only ``sleep`` gets a clock its own sleeps drive, so its wait stays
    bounded."""
    if monotonic is not None:
        return sleep, monotonic
    if sleep is time.sleep:
        return sleep, time.monotonic
    slept = [0.0]

    def counted(seconds):
        slept[0] += seconds
        sleep(seconds)

    return counted, lambda: slept[0]


def _system_refresh_sources(sources: RuntimeEnvSources) -> RuntimeEnvSources:
    """*sources* with the durable guidance table defaulted, the way the
    deploy.sh [2f] reconcile (``runtime_env_contract --role system
    --reconcile``) defaults it for the system role."""
    if sources.system_guidance_table:
        return sources
    return dataclasses.replace(
        sources, system_guidance_table=SYSTEM_GUIDANCE_TABLE_DEFAULT)


def _system_runtime_ids(control, exclude) -> dict:
    """``{name: runtime id}`` for every ``adt_*`` runtime not in *exclude*,
    across every page (bounded). A listing that fails raises: a failed list
    is never reported as an empty one."""
    ids: dict = {}
    try:
        for page in iter_agent_runtime_pages(
                control, max_pages=SYSTEM_REFRESH_MAX_LIST_PAGES):
            for rt in page:
                name = rt.get("agentRuntimeName", "")
                if name.startswith(ADT_RUNTIME_PREFIX) and name not in exclude:
                    ids[name] = (rt.get("agentRuntimeId")
                                 or rt.get("agentRuntimeArn", "").rsplit("/", 1)[-1])
    except Exception as exc:  # noqa: BLE001 - any listing failure stops the refresh
        raise ProvisionRuntimeError(
            "could not list agent runtimes, so the per-system runtimes were "
            f"NOT refreshed: {_error_text(exc)}") from exc
    return ids


class _SystemRuntimeRefresh:
    """One refresh run: send each runtime's update in turn, then wait for all
    of them together, inside ONE time limit counted from the first update."""

    def __init__(self, control, image_uri, sources, ids, *, deadline_seconds,
                 poll_seconds, sleep, monotonic):
        self.control = control
        self.image_uri = image_uri
        self.sources = sources
        self.ids = dict(ids)
        self.limit = deadline_seconds
        self.poll_seconds = poll_seconds
        self.sleep, self.monotonic = _refresh_clock(sleep, monotonic)
        self.result = SystemRefreshResult()
        self.sent: dict = {}  # name -> the version its update returned ("" none)
        self.last: dict = {}  # name -> its last status read since that update
        self.waiting: list = []  # update accepted, waiting for READY
        self.retry: list = []  # update met a ConflictException (already UPDATING)
        self.started = self.monotonic()

    def _deadline_passed(self) -> bool:
        return self.monotonic() - self.started >= self.limit

    def _limit_text(self) -> str:
        return f"{self.limit:g}s"

    def _fail(self, name, why):
        _log(f"    {name}: NOT refreshed: {why}")
        self.result.failed[name] = why

    def _current(self, name):
        _log(f"    {name}: already READY on the current image, nothing sent")
        self.result.current.append(name)

    def _refreshed(self, name, version):
        shown = f" (version {version})" if version else ""
        _log(f"    {name}: READY on the current image{shown}")
        self.result.refreshed.append(name)

    def _prepare(self, name):
        """The update rebuilt from the runtime's CURRENT state (its own role,
        env reconciled for the system role, network config, authorizer, MCP
        protocol), or None when it is already READY on the image. Runs again
        before a retry, so a retry never re-sends what another writer has
        since replaced."""
        runtime_id = self.ids[name]
        try:
            details = self.control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:  # noqa: BLE001 - named per runtime
            raise _NotRefreshed(f"could not read runtime: {_error_text(exc)}") from exc
        if (_container_uri(details) == self.image_uri
                and details.get("status") == "READY"):
            return None
        role = str(details.get("roleArn") or "")
        if not role:
            raise _NotRefreshed("could not read roleArn")
        try:
            env = reconcile_env(
                "system", details.get("environmentVariables") or {}, self.sources,
                span_emission=self.sources.observability)
        except Exception as exc:  # noqa: BLE001 - named per runtime, env untouched
            raise _NotRefreshed(f"env reconcile failed: {_error_text(exc)}") from exc
        return runtime_update_kwargs(
            runtime_id, details, env,
            artifact={"containerConfiguration": {"containerUri": self.image_uri}},
            role_arn=role, protocol_override=dict(MCP_PROTOCOL))

    def _send(self, name, kwargs) -> bool:
        """One update call: True when accepted, False on a ConflictException
        (the runtime is already UPDATING); raises _NotRefreshed otherwise."""
        try:
            resp = self.control.update_agent_runtime(**kwargs) or {}
        except Exception as exc:  # noqa: BLE001 - classified below
            if (_error_code(exc) or type(exc).__name__) == "ConflictException":
                return False
            raise _NotRefreshed(f"update rejected: {_error_text(exc)}") from exc
        self.sent[name] = str(resp.get("agentRuntimeVersion") or "")
        self.last[name] = ""
        shown = (f"version {self.sent[name]}" if self.sent[name]
                 else "no version returned; its first READY counts")
        _log(f"    {name}: update accepted ({shown}), waiting for READY")
        if "authorizerConfiguration" not in kwargs:
            _log(f"    {name}: WARNING: carries no inbound JWT authorizer; a gateway "
                 "target's M2M JWT is rejected by a runtime without one")
        return True

    def start_all(self, names):
        """Send each runtime's update in turn. Once the limit has run out no
        further runtime is read or updated; each one left is named."""
        for name in names:
            if self._deadline_passed():
                self._fail(name, f"not sent: the {self._limit_text()} refresh "
                                 "limit ran out before its update")
                continue
            if not self.ids.get(name):
                self._fail(name, "could not resolve ARN")
                continue
            _log(f"  {name}: refreshing to the current image")
            try:
                kwargs = self._prepare(name)
                if kwargs is None:
                    self._current(name)
                elif self._send(name, kwargs):
                    self.waiting.append(name)
                else:
                    _log(f"    {name}: already UPDATING (ConflictException); the "
                         "update is rebuilt and retried once when that finishes")
                    self.last[name] = ""
                    self.retry.append(name)
            except _NotRefreshed as exc:
                self._fail(name, str(exc))

    def _poll(self, name, is_retry):
        """Read one runtime's status. Returns "wait" or "retry" to keep it,
        None once it is settled (refreshed, current or failed)."""
        try:
            details = self.control.get_agent_runtime(agentRuntimeId=self.ids[name])
        except Exception as exc:  # noqa: BLE001 - named per runtime
            _log(f"    {name}: could not read its status: {_error_text(exc)}")
            details = {}
        status = str(details.get("status") or "")
        if not status:
            self._fail(name, "could not read status")
            return None
        self.last[name] = status
        if status in _REFRESH_WAIT_STATES:
            return "retry" if is_retry else "wait"
        if status not in _REFRESH_SETTLED_STATES:
            self._fail(name, f"unexpected status {status}")
            return None
        if is_retry:
            return self._retry_once(name, status)
        if status != "READY":
            reason = str(details.get("failureReason") or "")
            self._fail(name, f"{status}: {reason}" if reason else status)
            return None
        return self._ready(name, details)

    def _ready(self, name, details):
        """READY counts at the version the update returned. A later version
        (another writer's update) counts only while it still runs this image;
        an earlier one is the previous version, so keep waiting."""
        version = str(details.get("agentRuntimeVersion") or "")
        want = self.sent.get(name, "")
        if not want or not version or version == want:
            self._refreshed(name, version)
        elif not version_reached(version, want):
            self.last[name] = f"READY at version {version}, waiting for version {want}"
            return "wait"
        elif _container_uri(details) == self.image_uri:
            self._refreshed(name, version)
        else:
            self._fail(name, f"a later update (version {version}) replaced "
                             "this deploy's image")
        return None

    def _retry_once(self, name, status):
        """A conflicted runtime has left UPDATING: rebuild its update from its
        current settings and send it once more."""
        if self._deadline_passed():
            return "retry"
        _log(f"    {name}: retrying the update once (status {status}), rebuilt "
             "from its current settings")
        try:
            kwargs = self._prepare(name)
        except _NotRefreshed as exc:
            self._fail(name, f"{exc}, before the retry")
            return None
        if kwargs is None:
            self._current(name)
            return None
        try:
            if self._send(name, kwargs):
                return "wait"
        except _NotRefreshed as exc:
            _log(f"    {name}: {exc}")
        self._fail(name, "update rejected again after waiting")
        return None

    def wait_all(self):
        """Poll every runtime still in flight together until each settles or
        the limit runs out. The limit is checked before every poll and retry,
        and a sleep never runs past it."""
        while self.waiting or self.retry:
            keep: dict = {"wait": [], "retry": []}
            for name in self.waiting + self.retry:
                is_retry = name in self.retry
                if self._deadline_passed():
                    keep["retry" if is_retry else "wait"].append(name)
                    continue
                outcome = self._poll(name, is_retry)
                if outcome:
                    keep[outcome].append(name)
            self.waiting, self.retry = keep["wait"], keep["retry"]
            if not (self.waiting or self.retry):
                return
            if self._deadline_passed():
                self._fail_unfinished()
                return
            remaining = self.limit - (self.monotonic() - self.started)
            self.sleep(max(0.0, min(self.poll_seconds, remaining)))

    def _fail_unfinished(self):
        """Name every runtime the limit left in flight, with what was last seen."""
        limit = self._limit_text()
        for name in self.waiting:
            last = self.last.get(name, "")
            self._fail(name, f"not READY after {limit}, last status {last}" if last
                       else f"not READY after {limit}, not polled since its update was sent")
        for name in self.retry:
            last = self.last.get(name, "")
            if last in _REFRESH_WAIT_STATES:
                why = f"still {last} after {limit}, update not retried"
            elif last:
                why = f"update not retried within {limit}, last status {last}"
            else:
                why = f"update not retried within {limit}, status not read"
            self._fail(name, why)
        self.waiting, self.retry = [], []


def refresh_system_runtimes(
    *, control, image_uri: str, sources: RuntimeEnvSources,
    exclude=ENGINE_RUNTIME_NAMES,
    deadline_seconds: float = SYSTEM_REFRESH_DEADLINE_SECONDS,
    poll_seconds: float = SYSTEM_REFRESH_POLL_SECONDS,
    sleep=time.sleep, monotonic=None,
) -> SystemRefreshResult:
    """Move every per-system ``adt_*`` runtime to *image_uri*.

    deploy.sh [2f]'s job for callers that run no bash (the engine bootstrap
    custom resource on a stack update). Lists every ``adt_*`` runtime except
    *exclude*, skips any already READY on the image, and rebuilds each other
    one's full-replacement update from its CURRENT state through
    ``runtime_update_kwargs``: its own role, its env reconciled by
    ``reconcile_env("system", ...)`` from *sources*, its network config
    (immutable S3-endpoint flag dropped), its authorizer, its other optional
    settings (``RUNTIME_UPDATE_CARRIED_FIELDS``) and the MCP protocol,
    so nothing another writer set is reverted. Then waits for every update to
    reach READY at the version it returned, inside one time limit counted from
    the first update.

    Per-runtime failures are collected, never raised: the result names each
    runtime that was not refreshed and why, and the caller decides what that
    means. A listing that fails, or a missing image, raises
    ProvisionRuntimeError. No per-system runtimes is a clean no-op."""
    if not image_uri or image_uri == "None":
        raise ProvisionRuntimeError(
            "agent image URI missing, so the per-system runtimes were NOT refreshed")
    ids = _system_runtime_ids(control, frozenset(exclude))
    if not ids:
        _log("per-system runtimes: none found, nothing to refresh (no-op)")
        return SystemRefreshResult()
    run = _SystemRuntimeRefresh(
        control, image_uri, _system_refresh_sources(sources), ids,
        deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
        sleep=sleep, monotonic=monotonic)
    run.start_all(list(ids))
    run.wait_all()
    result = run.result
    _log(f"per-system runtime refresh: {len(result.refreshed)} refreshed (READY), "
         f"{len(result.current)} already current, {len(result.failed)} failed")
    if result.failed:
        _log(f"  not refreshed: {result.failure_summary()}")
    return result


# --------------------------------------------------------------------------- #
# CLI (deploy.sh consumer)
# --------------------------------------------------------------------------- #
_ONBOARDING_REQUIRED_FLAGS = (
    "gateway_id", "m2m_discovery_url", "m2m_client_id", "provider_arn",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/provision_runtimes.py",
        description="Ensure the ADT engine AgentCore runtimes (deploy.sh "
                    "§2c/§2e extraction). Env-contract inputs are ambient — "
                    "export the same variables the Phase 0 shim consumed.",
    )
    parser.add_argument("--role", required=True,
                        choices=("orchestrator", "onboarding"))
    parser.add_argument("--region", required=True)
    parser.add_argument("--image", required=True, help="runtime image URI")
    parser.add_argument("--role-arn", required=True, help="runtime IAM role")
    parser.add_argument("--subnets", required=True,
                        help="comma-separated subnet ids")
    parser.add_argument("--sg", required=True, help="security group id")
    parser.add_argument("--gateway-id", default="",
                        help="onboarding gateway id (onboarding role only)")
    parser.add_argument("--m2m-discovery-url", default="")
    parser.add_argument("--m2m-client-id", default="")
    parser.add_argument("--provider-arn", default="",
                        help="adt-m2m OAuth credential provider ARN")
    parser.add_argument("--profile", default="",
                        help="AWS credentials profile for the boto3 control "
                             "client. Matches deploy.sh --profile so the "
                             "driver uses the SAME identity as the CDK/CLI "
                             "steps, instead of falling back to the ambient "
                             "default credential resolution.")
    return parser


def _spec_from_args(args) -> RuntimeSpec:
    """RuntimeSpec from CLI flags + the ambient env contract (read-only)."""
    sources = RuntimeEnvSources.from_ambient(region=args.region)
    subnet_ids = [s for s in (args.subnets or "").split(",") if s.strip()]
    if args.role == "orchestrator":
        return RuntimeSpec(
            name=ORCHESTRATOR_RUNTIME_NAME, image_uri=args.image,
            role_arn=args.role_arn, subnet_ids=subnet_ids,
            security_group_id=args.sg,
            env=build_env("orchestrator", sources),
        )
    return RuntimeSpec(
        name=ONBOARDING_RUNTIME_NAME, image_uri=args.image,
        role_arn=args.role_arn, subnet_ids=subnet_ids,
        security_group_id=args.sg,
        env=build_env("onboarding", sources),
        protocol_mcp=True,
        authorizer={"customJWTAuthorizer": {
            "discoveryUrl": args.m2m_discovery_url,
            "allowedClients": [args.m2m_client_id],
        }},
    )


def main(argv=None) -> int:
    """CLI entrypoint: ONE JSON object on stdout, diagnostics on stderr,
    nonzero on ANY failure (fail-loud — same class as the Phase 0 shim
    abort)."""
    args = _build_parser().parse_args(argv)
    if args.role == "onboarding":
        missing = [f"--{k.replace('_', '-')}"
                   for k in _ONBOARDING_REQUIRED_FLAGS
                   if not getattr(args, k)]
        if missing:
            _log(f"provision_runtimes: missing required onboarding flags: "
                 f"{' '.join(missing)}")
            return 2
    try:
        # Honor an explicit --profile (paper-cut #7) via a dedicated Session; with
        # no profile, use the ambient boto3.client so the deploy inherits the same
        # credential chain the CDK/CLI steps use (and the offline test double stubs).
        if args.profile:
            control = boto3.Session(profile_name=args.profile).client(
                "bedrock-agentcore-control", region_name=args.region
            )
        else:
            control = boto3.client(
                "bedrock-agentcore-control", region_name=args.region
            )
        spec = _spec_from_args(args)
        result = {
            args.role: ensure_runtime(spec, control=control, region=args.region,
                                      create_poll_seconds=CLI_CREATE_POLL_DEADLINE_SECONDS)
        }
        if args.role == "onboarding":
            result["target_id"] = ensure_onboarding_target(
                control=control,
                gateway_id=args.gateway_id,
                runtime_arn=result["onboarding"]["arn"],
                provider_arn=args.provider_arn,
                region=args.region,
            )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: fail-loud, no stdout
        _log(f"provision_runtimes: FAILED — {exc}")
        return 1
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
