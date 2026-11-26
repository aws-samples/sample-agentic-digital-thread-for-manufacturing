# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""_query_mode_lib.py — pure, importable core behind ``scripts/set_query_mode.py``.

This module is the reusable, offline-unit-testable core that reconciles the
``SYSTEM_QUERY_MODE`` env var (ADR-0035 A/B toggle) across the whole ADT stack to
a single target mode. Like ``_reonboard_lib.py`` / ``_reconcile_query_gateway_lib.py``
it holds NO system-id / dataset / model literals and constructs NO boto3 clients —
every function that touches AWS takes an *injected* control-plane client so unit
tests can stub it and no live AWS is required.

Background (verified)
---------------------
``SYSTEM_QUERY_MODE`` selects:

* ``agent`` (Mode A) — each system runtime exposes ONE natural-language
  ``query_<id>`` tool; the per-system agent owns SQL.
* ``orchestrator`` (Mode B) — generic ``describe_schema`` / ``run_query`` /
  ``connect`` tools driven by the orchestrator.

It is read at runtime by ``agents/system_agent.py::create_mcp_server`` via
``os.environ.get("SYSTEM_QUERY_MODE", "agent")`` and must be set CONSISTENTLY on
the orchestrator runtime, every system runtime, the query gateway targets, and
the ECS app task — otherwise the stack ends up in a split state. This module
supplies the two pieces of logic that are pure (and therefore unit-testable) and
would otherwise be tangled up with boto3:

1. :func:`merged_environment` — compute the updated ``environmentVariables`` map
   for a runtime, preserving every existing key and overwriting only
   ``SYSTEM_QUERY_MODE``.
2. :func:`select_system_runtime_names` — from a list of runtime names, pick the
   per-system ``adt_*`` runtimes, EXCLUDING the infra runtimes that do not read
   ``SYSTEM_QUERY_MODE`` (orchestrator / onboarding / digital-thread).

Plus :func:`validate_mode` (the CLI arg gate),
:func:`build_runtime_update_kwargs` (DELEGATES to the ONE sanctioned builder,
``agents.runtime_provision_helpers.runtime_update_kwargs`` — used for the
ORCHESTRATOR runtime, which has no gateway target so an in-place update is
safe), and the
RECREATE path for per-system runtimes (:func:`capture_runtime_create_kwargs`,
:func:`capture_target_create_kwargs`, :func:`recreate_runtime_with_mode`) —
required because an in-place env flip leaves the gateway target unable to
re-synchronize against the cold VPC runtime (see the recreate section below).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# The only two legal query modes (ADR-0035). Anything else is a hard error.
VALID_MODES = ("agent", "orchestrator")

# The orchestrator runtime name (single well-known name; see deploy.sh §2c).
ORCHESTRATOR_RUNTIME_NAME = "adt_orchestrator"

# Infra runtimes that DO NOT read SYSTEM_QUERY_MODE — never touched as "system"
# runtimes. (The orchestrator IS updated, but by the dedicated orchestrator step,
# not the per-system sweep, so it is excluded here too.)
INFRA_RUNTIME_NAMES = frozenset(
    {"adt_orchestrator", "adt_onboarding", "adt_digital_thread"}
)

# The env var this tool reconciles.
QUERY_MODE_ENV_KEY = "SYSTEM_QUERY_MODE"

# AgentCore READY status token (mirrors _reonboard_lib.READY — kept as a local
# constant so the module's pure helpers need no import).
READY_STATUS = "READY"


class QueryModeError(RuntimeError):
    """Base class for all set-query-mode failures (errors propagate)."""


class InvalidModeError(QueryModeError):
    """The requested mode was not exactly 'agent' or 'orchestrator'."""

    def __init__(self, mode: str):
        self.mode = mode
        super().__init__(
            f"invalid query mode {mode!r}; expected one of {', '.join(VALID_MODES)}"
        )


def validate_mode(mode: str) -> str:
    """Return *mode* unchanged if it is exactly one of :data:`VALID_MODES`.

    No normalization / coercion — the caller must pass the exact token so a
    typo fails loudly rather than silently defaulting. Raises
    :class:`InvalidModeError` otherwise.
    """
    if mode not in VALID_MODES:
        raise InvalidModeError(mode)
    return mode


def merged_environment(existing_env: dict | None, mode: str) -> dict:
    """Return a new env map = *existing_env* with ``SYSTEM_QUERY_MODE`` set to *mode*.

    Every existing key/value is PRESERVED (this is a runtime env, not a full
    replacement); only ``SYSTEM_QUERY_MODE`` is added/overwritten. The input is
    not mutated. *mode* is validated first (fail loud on a bad mode).
    """
    validate_mode(mode)
    merged = dict(existing_env or {})
    merged[QUERY_MODE_ENV_KEY] = mode
    return merged


def select_system_runtime_names(
    all_names: Iterable[str], *, explicit_system_ids: Iterable[str] | None = None
) -> list[str]:
    """Return the per-system ``adt_*`` runtime names to update, in a stable order.

    Two modes:

    * ``explicit_system_ids`` given (the ``--systems a,b,c`` path): map each id to
      its ``adt_<id>`` runtime name (mirroring the ``_reonboard_lib.runtime_name``
      convention: ``adt_`` prefix, ``-``\u2192``_``, \u226448 chars) and return them in the
      GIVEN order. No filtering — the operator named them explicitly.
    * ``explicit_system_ids`` is ``None`` (the ``--all`` path): from *all_names*
      select those starting with ``adt_``, EXCLUDING :data:`INFRA_RUNTIME_NAMES`
      (orchestrator / onboarding / digital-thread do not read
      ``SYSTEM_QUERY_MODE``), sorted for determinism.

    Pure: takes plain strings, returns plain strings — unit-testable offline.
    """
    if explicit_system_ids is not None:
        # Mirror _reonboard_lib.runtime_name so we target exactly the runtime the
        # onboarding path created. (Imported lazily-by-mirror to keep this module
        # importable from the repo root the same way _reconcile_query_gateway_lib
        # imports runtime_name.)
        from _reonboard_lib import runtime_name

        return [runtime_name(sid) for sid in explicit_system_ids]

    return sorted(
        name
        for name in all_names
        if name.startswith("adt_") and name not in INFRA_RUNTIME_NAMES
    )


def _app_helpers():
    """Import ``agents.runtime_provision_helpers`` — the ONE sanctioned
    full-replacement builder home (ADR-0056). This module never mirrors
    its ``requireServiceS3Endpoint`` strip / protocol round-trip / authorizer
    union handling: a local copy that skips its required-key fill would let a
    fleet query-mode flip on a starved runtime rewrite env without the
    provenance store. Bootstrap: when run from the repo root (set_query_mode.py), the app
    package dir is added to ``sys.path`` lazily (mirrors the lazy
    ``_reonboard_lib`` imports elsewhere in this module)."""
    try:
        from agents import runtime_provision_helpers as helpers
    except ImportError:
        import sys
        from pathlib import Path

        app_root = Path(__file__).resolve().parents[1] / "agentic_digital_thread"
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        from agents import runtime_provision_helpers as helpers
    return helpers


def build_runtime_update_kwargs(details: dict, mode: str) -> dict:
    """Build the ``update_agent_runtime`` kwargs that set the mode, preserving config.

    DELEGATED to ``agents.runtime_provision_helpers.runtime_update_kwargs`` —
    the ONE sanctioned full-replacement builder (ADR-0056: protocol
    round-trip, immutable ``requireServiceS3Endpoint`` strip, tagged-union
    authorizer handling, and the required-key fill so a query-mode flip can
    never strip ``ONBOARDING_SESSIONS_TABLE`` from a starved runtime). This
    module contributes only the query-mode-specific piece: the
    ``SYSTEM_QUERY_MODE`` overwrite via :func:`merged_environment`.

    ``details`` MUST carry ``agentRuntimeId`` (the update key). Pure: no
    client, no I/O — the CLI passes the ``get_agent_runtime`` response in.
    """
    validate_mode(mode)
    runtime_id = details.get("agentRuntimeId") or details.get("id") or ""
    if not runtime_id:
        raise QueryModeError(
            "runtime details missing agentRuntimeId; cannot build update payload"
        )
    return _app_helpers().runtime_update_kwargs(
        runtime_id, details,
        merged_environment(details.get("environmentVariables", {}), mode),
    )


# --------------------------------------------------------------------------- #
# Injected-client drivers — the client is dependency-injected (never built here)
# so these are unit-testable offline with a stub, exactly like _reonboard_lib.
# --------------------------------------------------------------------------- #
def list_runtime_summaries(control) -> list:
    """EVERY runtime summary, across ALL ``list_agent_runtimes`` pages.

    DELEGATED to ``agents.runtime_provision_helpers.list_all_agent_runtimes``
    (the ONE sanctioned enumerator, tolerant of the ``agentRuntimes`` |
    ``agentRuntimeSummaries`` response shapes): one call returns a single page
    plus a ``nextToken``, so a runtime on page 2 would read as absent.
    ``control`` is injected."""
    return _app_helpers().list_all_agent_runtimes(control)


def runtime_id_by_name(control, name: str) -> str:
    """Return the ``agentRuntimeId`` for the runtime named *name*, or "" if absent.

    Reads every page (:func:`list_runtime_summaries`). ``control`` is injected.
    """
    for rt in list_runtime_summaries(control):
        if rt.get("agentRuntimeName") == name:
            return rt.get("agentRuntimeId") or (
                rt.get("agentRuntimeArn", "").rsplit("/", 1)[-1]
            )
    return ""


def current_query_mode(details: dict) -> str:
    """Return the runtime's current ``SYSTEM_QUERY_MODE`` env value, or "" if unset.

    Reads from a ``get_agent_runtime`` response. Pure — no client, no I/O.
    """
    return (details.get("environmentVariables") or {}).get(QUERY_MODE_ENV_KEY, "") or ""


def update_runtime_query_mode(control, runtime_id: str, mode: str, *, force: bool = False) -> dict:
    """Set ``SYSTEM_QUERY_MODE=mode`` on runtime *runtime_id*, preserving config.

    Reads the runtime via ``get_agent_runtime`` (injected *control*). If the
    runtime is ALREADY at *mode* and ``force`` is False, this is a NO-OP — it does
    NOT call ``update_agent_runtime`` (which would needlessly restart a runtime
    that is already correct), and returns ``{"skipped": True, "current": mode}``.
    Otherwise it computes the env-preserving update payload with
    :func:`build_runtime_update_kwargs`, calls ``update_agent_runtime``, and
    returns the kwargs actually sent (no ``"skipped"`` key). Errors
    propagate. ``mode`` is validated first — this makes the reconciler safe to
    re-run and avoids restarting resources already at the target mode.
    """
    validate_mode(mode)
    details = control.get_agent_runtime(agentRuntimeId=runtime_id)
    if not force and current_query_mode(details) == mode:
        return {"skipped": True, "current": mode}
    kwargs = build_runtime_update_kwargs(details, mode)
    control.update_agent_runtime(**kwargs)
    return kwargs



# --------------------------------------------------------------------------- #
# Recreate path (ADR-0035 deterministic toggle — see set_query_mode.py docstring)
#
# Empirical facts (live debugging): an IN-PLACE update_agent_runtime(env flip)
# followed by a gateway target sync FAILS deterministically for VPC runtimes —
# the gateway's MCP initialize against the updated (idle) runtime cold-starts
# ~130s, exceeding the gateway's fixed 40s sync timeout, so the target lands in
# SYNCHRONIZE_UNSUCCESSFUL/FAILED forever (warm instances are session-bound;
# pre-warming does not help; background retries churn the runtime). FRESHLY
# CREATED runtimes + immediately created targets sync RELIABLY (every onboarding
# provision does create-runtime → create-target and goes READY fast). The toggle
# therefore RECREATES rather than updates: capture full config, delete
# target + runtime, re-create both with only SYSTEM_QUERY_MODE changed.
# --------------------------------------------------------------------------- #

# get_agent_runtime response keys that create_agent_runtime does NOT accept
# (read-only / server-assigned, per the botocore service model). Notably
# ``metadataConfiguration`` IS accepted by update_agent_runtime but NOT by
# create_agent_runtime — it must be stripped on the recreate path.
_RUNTIME_READONLY_FIELDS = frozenset(
    {
        "agentRuntimeId",
        "agentRuntimeArn",
        "agentRuntimeVersion",
        "status",
        "failureReason",
        "createdAt",
        "lastUpdatedAt",
        "workloadIdentityDetails",
        "metadataConfiguration",
        "ResponseMetadata",
    }
)

# Optional create_agent_runtime fields carried over verbatim when present in the
# get_agent_runtime details (all are valid CreateAgentRuntime input members).
_RUNTIME_OPTIONAL_CREATE_FIELDS = (
    "protocolConfiguration",
    "description",
    "requestHeaderConfiguration",
    "lifecycleConfiguration",
    "filesystemConfigurations",
)

# Optional create_gateway_target fields carried over verbatim when present in
# the get_gateway_target details.
_TARGET_OPTIONAL_CREATE_FIELDS = (
    "description",
    "credentialProviderConfigurations",
    "metadataConfiguration",
)


def capture_runtime_create_kwargs(details: dict, mode: str) -> dict:
    """Build ``create_agent_runtime`` kwargs from ``get_agent_runtime`` *details*.

    Allow-list construction (never a blind copy), so every read-only field in
    :data:`_RUNTIME_READONLY_FIELDS` — ``agentRuntimeId``/``Arn``/``Version``,
    ``status``, ``createdAt``/``lastUpdatedAt``, ``workloadIdentityDetails``, and
    ``metadataConfiguration`` (update-only, NOT a create field) — is stripped.

    The result preserves the runtime's full mutable config:

    * ``agentRuntimeName`` (required — the recreate keeps the same name);
    * ``agentRuntimeArtifact`` / ``roleArn``;
    * ``networkConfiguration`` with the immutable ``requireServiceS3Endpoint``
      dropped (same sanitization as :func:`build_runtime_update_kwargs`);
    * ``environmentVariables`` merged with ``SYSTEM_QUERY_MODE=mode`` — every
      other env key preserved (via :func:`merged_environment`);
    * ``protocolConfiguration`` / ``description`` / ``requestHeaderConfiguration``
      / ``lifecycleConfiguration`` / ``filesystemConfigurations`` when present;
    * ``authorizerConfiguration`` ONLY when a real ``customJWTAuthorizer`` is
      configured (tagged union — ``{}`` fails).

    Pure: no client, no I/O. Raises :class:`QueryModeError` when the details
    carry no ``agentRuntimeName`` (cannot recreate a nameless runtime).
    """
    validate_mode(mode)
    name = details.get("agentRuntimeName", "")
    if not name:
        raise QueryModeError(
            "runtime details missing agentRuntimeName; cannot build create payload"
        )

    helpers = _app_helpers()
    kwargs = {
        "agentRuntimeName": name,
        "agentRuntimeArtifact": details.get("agentRuntimeArtifact", {}),
        "roleArn": details.get("roleArn", ""),
        "networkConfiguration": helpers.sanitized_network_config(details),
        # Required-key fill (defect-3): never re-create a runtime starved.
        "environmentVariables": helpers.reconcile_required_env(
            merged_environment(details.get("environmentVariables", {}), mode)
        ),
    }
    for key in _RUNTIME_OPTIONAL_CREATE_FIELDS:
        if details.get(key):
            kwargs[key] = details[key]
    auth_cfg = details.get("authorizerConfiguration") or {}
    if auth_cfg.get("customJWTAuthorizer"):
        kwargs["authorizerConfiguration"] = auth_cfg
    return kwargs


def capture_target_create_kwargs(
    target_details: dict, gateway_id: str, *, endpoint: str | None = None,
    name_override: str | None = None,
) -> dict:
    """Build ``create_gateway_target`` kwargs from ``get_gateway_target`` *details*.

    Carries over ``name`` (required), ``targetConfiguration`` (required), and —
    when present — ``description`` / ``credentialProviderConfigurations`` /
    ``metadataConfiguration``. Read-only fields (``targetId``, ``status``,
    ``statusReasons``, ``gatewayArn``, ``createdAt``/``updatedAt``/
    ``lastSynchronizedAt``, ``protocolType``, ``authorizationData``) are never
    copied (allow-list construction).

    ``endpoint`` (when given) REPLACES ``targetConfiguration.mcp.mcpServer
    .endpoint`` without mutating the input — required on the recreate path
    because the captured endpoint embeds the OLD (deleted) runtime's ARN and the
    fresh runtime has a NEW ARN. ``name_override`` (when given) replaces the
    template's name — used on the re-entrant recreate path where a SIBLING
    runtime-backed target serves as the template for a target a previous
    (interrupted) run already deleted. Pure: no client, no I/O.
    """
    if not gateway_id:
        raise QueryModeError("gateway_id is required to build a target create payload")
    name = name_override or target_details.get("name", "")
    if not name:
        raise QueryModeError(
            "target details missing name; cannot build create payload"
        )

    cfg = target_details.get("targetConfiguration") or {}
    if endpoint:
        mcp = dict(cfg.get("mcp") or {})
        server = dict(mcp.get("mcpServer") or {})
        server["endpoint"] = endpoint
        cfg = {**cfg, "mcp": {**mcp, "mcpServer": server}}

    kwargs = {
        "gatewayIdentifier": gateway_id,
        "name": name,
        "targetConfiguration": cfg,
    }
    for key in _TARGET_OPTIONAL_CREATE_FIELDS:
        if target_details.get(key):
            kwargs[key] = target_details[key]
    if name_override:
        # A sibling-template description names the WRONG system — rewrite it.
        kwargs["description"] = f"ADT system agent for {name}"
    return kwargs


def recreate_runtime_with_mode(
    control,
    gateway_id: str,
    system_id: str,
    mode: str,
    *,
    region: str,
    runtime_deadline_s: float = 600.0,
    target_deadline_s: float = 600.0,
    delete_deadline_s: float = 180.0,
    poll_s: float = 10.0,
    log: Callable[[str], None] = logger.info,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Flip *system_id* to *mode* by RECREATING its runtime + gateway target.

    Deterministic order (each step blocks before the next):

      1. capture the ``adt_<id>`` runtime's full config (``get_agent_runtime``)
         and — when a per-system gateway target exists — the target's full
         config (``get_gateway_target``);
      2. **skip-fast**: runtime already at *mode* AND target READY →
         ``{"skipped": True, "current": mode}`` — nothing is touched;
      3. delete the gateway target (wait absent — reuses
         ``_reonboard_lib.delete_target``);
      4. delete the runtime (wait absent — ``_reonboard_lib.delete_runtime``);
      5. ``create_agent_runtime`` with the SAME config but
         ``SYSTEM_QUERY_MODE=mode`` (:func:`capture_runtime_create_kwargs`);
         wait READY (``_reonboard_lib.wait_runtimes_ready``). On the documented
         stranded-create strand (terminal ``CREATE_FAILED``/``UPDATE_FAILED`` —
         see ``reonboard.py``) the failed runtime is deleted and the create
         re-triggered exactly ONCE (bounded); a second terminal failure
         propagates;
      6. ``create_gateway_target`` with the SAME endpoint/credential config but
         the endpoint rewritten to the NEW runtime ARN
         (:func:`capture_target_create_kwargs` + ``mcp_url_for_arn``); wait
         READY (``_reonboard_lib.wait_targets_ready``).

    Returns ``{"skipped": True, ...}`` or ``{"recreated": True, "runtime_arn":
    <new-arn>, "target_recreated": bool}``. Errors propagate — no silent
    fallback. ``control`` is injected; ``sleep``/``monotonic`` are seams for
    deterministic offline tests.
    """
    validate_mode(mode)
    if not gateway_id:
        raise QueryModeError("gateway_id is required for the recreate path")

    # Reuse the reonboard primitives + the single-source MCP URL builder (DRY).
    # Lazily imported (mirroring select_system_runtime_names) so this module
    # stays importable standalone.
    from _reconcile_query_gateway_lib import mcp_url_for_arn
    from _reonboard_lib import (
        RuntimeCreateFailedError,
        _target_map,
        delete_runtime,
        delete_target,
        runtime_name,
        target_name,
        wait_runtimes_ready,
        wait_targets_ready,
    )

    rt_name = runtime_name(system_id)
    tgt_name = target_name(system_id)

    rid = runtime_id_by_name(control, rt_name)
    if not rid:
        raise QueryModeError(
            f"runtime {rt_name!r} for system '{system_id}' not found — cannot recreate"
        )
    details = control.get_agent_runtime(agentRuntimeId=rid)
    target_summary = _target_map(control, gateway_id).get(tgt_name)

    # Skip-fast: already at the target mode with a READY target — do not churn.
    if (
        current_query_mode(details) == mode
        and target_summary
        and target_summary.get("status") == READY_STATUS
    ):
        return {"skipped": True, "current": mode}

    # Capture BOTH create payloads up front (before anything is deleted).
    create_kwargs = capture_runtime_create_kwargs(details, mode)
    target_detail = None
    target_name_override = None
    if target_summary:
        target_detail = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_summary["id"]
        )
    else:
        # Re-entrant path: a previous (interrupted) run already deleted this
        # system's target. Rebuild it from a SIBLING runtime-backed target as
        # template (same gateway auth/credential config; name + endpoint are
        # overridden below). Without this, an interrupted run would recreate the
        # runtime but leave the system unreachable through the gateway.
        for sib_name, sib in _target_map(control, gateway_id).items():
            if sib_name == tgt_name:
                continue
            sib_detail = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=sib["id"]
            )
            sib_cfg = (
                (sib_detail.get("targetConfiguration") or {})
                .get("mcp", {})
                .get("mcpServer", {})
            )
            if sib_cfg.get("endpoint"):  # runtime-backed MCP target only
                target_detail = sib_detail
                target_name_override = tgt_name
                log(
                    f"[{system_id}] recreate: target {tgt_name!r} absent — "
                    f"rebuilding from sibling template {sib_name!r}"
                )
                break
        if target_detail is None:
            raise QueryModeError(
                f"target {tgt_name!r} for system '{system_id}' is absent and no "
                f"runtime-backed sibling target exists to use as a template — "
                f"re-onboard the system instead"
            )

    log(f"[{system_id}] recreate: deleting gateway target + runtime")
    delete_target(
        control, gateway_id, system_id, delete_deadline_s, poll_s,
        sleep=sleep, monotonic=monotonic,
    )
    delete_runtime(
        control, system_id, delete_deadline_s, poll_s,
        sleep=sleep, monotonic=monotonic,
    )

    log(f"[{system_id}] recreate: creating runtime {rt_name} with {QUERY_MODE_ENV_KEY}={mode}")
    control.create_agent_runtime(**create_kwargs)
    try:
        ready = wait_runtimes_ready(
            control, [system_id], runtime_deadline_s, poll_s,
            sleep=sleep, monotonic=monotonic,
        )
    except RuntimeCreateFailedError as exc:
        # Bounded, exactly-once re-trigger for the documented stranded-create
        # strand (mirrors reonboard.py). A second terminal failure propagates.
        log(
            f"[{system_id}] runtime {exc.runtime_name} stranded {exc.status} "
            f"({exc.failure_reason or 'no failureReason'}) — deleting and "
            f"re-triggering create ONCE"
        )
        delete_runtime(
            control, system_id, delete_deadline_s, poll_s,
            sleep=sleep, monotonic=monotonic,
        )
        control.create_agent_runtime(**create_kwargs)
        ready = wait_runtimes_ready(
            control, [system_id], runtime_deadline_s, poll_s,
            sleep=sleep, monotonic=monotonic,
        )

    new_arn = ready[system_id].get("agentRuntimeArn", "")
    result = {"recreated": True, "runtime_arn": new_arn, "target_recreated": False}

    if target_detail is not None:
        if not new_arn:
            raise QueryModeError(
                f"recreated runtime {rt_name!r} reports no agentRuntimeArn; "
                f"cannot point the gateway target at it"
            )
        target_kwargs = capture_target_create_kwargs(
            target_detail, gateway_id,
            endpoint=mcp_url_for_arn(new_arn, region),
            name_override=target_name_override,
        )
        log(f"[{system_id}] recreate: creating gateway target {tgt_name} -> new runtime ARN")
        control.create_gateway_target(**target_kwargs)
        wait_targets_ready(
            control, gateway_id, [tgt_name], target_deadline_s, poll_s,
            sleep=sleep, monotonic=monotonic,
        )
        result["target_recreated"] = True

    return result
