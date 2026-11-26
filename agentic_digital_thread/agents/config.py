# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(key, default)
    return val if val else default


def _bool_env(key: str, default: bool = False) -> bool:
    return os.environ.get(key, "").lower() in ("1", "true", "yes")


_DEV_NO_AUTH_LOCAL_SIGNALS = {
    "DEV_NO_AUTH": "1",
    "DEV_MODE": "1",
    "AGENT_MODE": "local",
}
# Standard ECS runtime markers plus stack-owned values injected into the adt-app
# task in every deployed mode. Presence of any marker wins over local flags so an
# accidentally copied local trio cannot turn a cloud task into an auth-free app.
_DEV_NO_AUTH_CLOUD_MARKERS = (
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
    "AWS_EXECUTION_ENV",
    "COGNITO_USER_POOL_ID",
    "AGENTCORE_GATEWAY_URL",
    "ONBOARDING_GATEWAY_URL",
)


def dev_no_auth_allowed() -> bool:
    """Return whether the login-free local-development identity may be used.

    The bypass requires the exact documented local trio. It fails closed when
    any ECS or stack-owned deployment marker is present, even if all local flags
    were accidentally injected. This policy is intentionally separate from
    :attr:`AppConfig.is_local_mode`, whose broader semantics select agent
    execution rather than establish an authentication trust boundary.
    """
    if any(marker in os.environ for marker in _DEV_NO_AUTH_CLOUD_MARKERS):
        return False
    return all(
        os.environ.get(key) == expected
        for key, expected in _DEV_NO_AUTH_LOCAL_SIGNALS.items()
    )


def local_mode_env(environ=None) -> bool:
    """The ONE local-mode env predicate (ADR-0023 signal, ADR-0069 consumer).

    True when the process runs the agents in-process for local development —
    ``AGENT_MODE=local`` or ``DEV_MODE`` truthy. Module-level (no
    :func:`get_config`) so runtime entry points can gate their boot-time
    env validation without triggering config's runtime/memory discovery
    side effects; :attr:`AppConfig.is_local_mode` delegates here.
    """
    env = os.environ if environ is None else environ
    return (env.get("AGENT_MODE", "").strip().lower() == "local"
            or env.get("DEV_MODE", "").lower() in ("1", "true", "yes"))


def response_integrity_mode() -> str:
    """Return the Mode A response-integrity mode: ``'enforce'``, ``'annotate'``
    or ``'report-only'``.

    Single source of truth for the ADR-0047 feature flag. Reads the
    ``RESPONSE_INTEGRITY_MODE`` env var (stripped, lowercased); any value other
    than ``'enforce'`` / ``'annotate'`` — including unset — resolves to
    ``'report-only'``, the default.

    - ``report-only``: legacy model-authored answers; integrity capture hooks
      still run for shadow metrics.
    - ``annotate``: legacy model-authored answers pass through UNCHANGED, but
      the server verifies the answer TEXT against the captured trusted
      observations (numeric literals + identifier tokens), attaches a grounding
      score as sidecar metadata, and hard-blocks ONLY the true-fabrication case
      (a factual-sounding answer with ZERO supporting tool evidence).
    - ``enforce``: the full ADR-0046 deterministic typed-evidence gate (the
      envelope REPLACES the answer; strictest, for compliance-driven deployments).
    """
    value = os.environ.get("RESPONSE_INTEGRITY_MODE", "").strip().lower()
    return value if value in ("enforce", "annotate") else "report-only"


def response_integrity_annotated() -> bool:
    """True when ``response_integrity_mode()`` is ``'annotate'`` (ADR-0047).

    Annotate keeps the model-authored answer as the transport and uses the
    integrity capture as an AUDIT: deterministic text-vs-observations
    verification plus a zero-evidence fabrication block.
    """
    return response_integrity_mode() == "annotate"


def response_integrity_enforced() -> bool:
    """True when ``response_integrity_mode()`` is ``'enforce'`` (ADR-0046 gate on).

    ``report-only`` (the default, ADR-0047) restores legacy model-authored
    answers while the integrity capture machinery keeps running in shadow;
    ``'enforce'`` activates the full ADR-0046 deterministic evidence gate.
    """
    return response_integrity_mode() == "enforce"


def mode_a_integrity_enforced() -> bool:
    """True when the live chat runs the response-integrity gate: Mode A
    (``SYSTEM_QUERY_MODE`` other than ``orchestrator``) AND
    ``RESPONSE_INTEGRITY_MODE=enforce``. Mode B never runs it, whatever the
    flag says. The one decision shared by the local and cloud chat streams,
    the runtime entrypoint and the Recent sessions reload, so a reopened
    session never shows what its live stream hid."""
    mode_a = os.environ.get("SYSTEM_QUERY_MODE", "agent").strip().lower() != "orchestrator"
    return mode_a and response_integrity_enforced()


#: Closed vocabulary for ``MEMORY_LTM_SCOPE``; the first entry is the default.
MEMORY_LTM_SCOPES = ("actor", "session")


def memory_ltm_scope(environ=None) -> str:
    """Which AgentCore long-term-memory namespaces a chat turn RETRIEVES.

    ``actor`` (default — unset/blank): the actor-wide facts + preferences
    namespaces plus this chat's summaries (unchanged behaviour). ``session``:
    ONLY this chat's ``/summaries/{actorId}/{sessionId}/`` — same-chat
    continuity stays, and actor-wide facts/preferences are no longer injected
    into a fresh chat's MODEL CONTEXT. For deployments where many people share
    ONE login (the workshop), so one ``actorId`` spans every attendee's chats.

    Retrieval only, and NOT isolation: users of the shared login can still see
    and continue each other's chats (history sidebar, memory routes), and
    facts/preferences are still WRITTEN for the shared actor. Returning to
    ``actor`` (a redeploy without ``--workshop``) retrieves them all, so the
    workshop teardown purges them (``scripts/purge_actor_memory.py``).

    Any other value RAISES ``ValueError`` (fail loud): an unknown scope must
    never silently fall back to actor-wide retrieval. Reads *environ* (default
    ``os.environ``) without mutating it.
    """
    env = os.environ if environ is None else environ
    value = (env.get("MEMORY_LTM_SCOPE") or "").strip().lower()
    if not value:
        return MEMORY_LTM_SCOPES[0]
    if value not in MEMORY_LTM_SCOPES:
        raise ValueError(
            f"MEMORY_LTM_SCOPE={value!r} is invalid; expected one of "
            f"{', '.join(MEMORY_LTM_SCOPES)} (unset = actor)")
    return value


def _discover_orchestrator_arn(region: str) -> Optional[str]:
    """Look up the adt_orchestrator runtime ARN from AgentCore."""
    try:
        import boto3
        client = boto3.client("bedrock-agentcore-control", region_name=region)
        from .runtime_provision_helpers import list_all_agent_runtimes
        for rt in list_all_agent_runtimes(client):
            if rt.get("agentRuntimeName", "").startswith("adt_orchestrator"):
                return rt["agentRuntimeArn"]
        logger.warning(
            "Orchestrator ARN discovery: no AgentCore runtime named 'adt_orchestrator*' "
            "found in region %s. /api/ask will fail until deploy.sh creates it and the "
            "app restarts.", region,
        )
    except Exception:
        # Discovery is best-effort (callers handle a None ARN via has_runtime), but the
        # cause (e.g. missing bedrock-agentcore-control:ListAgentRuntimes permission) must
        # be visible, not silently swallowed.
        logger.warning(
            "Orchestrator ARN discovery failed in region %s", region,
        )
    return None


# The CfnMemory in stacks/agentcore_stack.py is named "adt_memory";
# the live resource id looks like "adt_memory-XXXX". The account
# also holds 5 UNRELATED leftover prototype memories (mfg_thread_memory*,
# mfg_thread_discovery_memory*, factoryfloor*, customersupport*, testharness*), so
# we match ONLY by this exact name prefix — never a substring like "memory".
_MEMORY_NAME_PREFIX = "adt_memory"


def _active_memory_matches(client) -> list[dict]:
    matches: list[dict] = []
    next_token: Optional[str] = None
    for _page in range(100):
        kwargs = {"nextToken": next_token} if next_token else {}
        response = client.list_memories(**kwargs)
        for memory in response.get("memories", []):
            name = memory.get("name") or ""
            memory_id = memory.get("id") or ""
            active = str(memory.get("status", "")).upper() == "ACTIVE"
            if active and (memory_id.startswith(_MEMORY_NAME_PREFIX) or name.startswith(_MEMORY_NAME_PREFIX)):
                matches.append(memory)
        next_token = response.get("nextToken")
        if not (isinstance(next_token, str) and next_token):
            break
    return matches


def _select_memory_id(matches: list[dict]) -> Optional[str]:
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].get("id")
    matching_ids = sorted(memory.get("id") or "<no-id>" for memory in matches)
    logger.error(
        "AgentCore Memory discovery is AMBIGUOUS: %d ACTIVE memories match the "
        "prefix %r (%s). Refusing to guess which one to bind — memory is "
        "DISABLED. Delete the stale resource(s) or set MEMORY_ID explicitly to "
        "the intended id.",
        len(matches), _MEMORY_NAME_PREFIX, ", ".join(matching_ids),
    )
    return None


def _discover_memory_id(region: str) -> Optional[str]:
    """Resolve the one ACTIVE memory matching the stack-owned exact prefix."""
    try:
        import boto3
        client = boto3.client("bedrock-agentcore-control", region_name=region)
        return _select_memory_id(_active_memory_matches(client))
    except Exception:  # nosec B110 - log the cause, then disable memory
        # exc_info is REQUIRED here (pinned by test_config_discovery): the
        # message alone cannot distinguish a missing IAM permission from a
        # bad region or an SDK/API change, and this failure silently DISABLES
        # memory — so a discovery failure with no cause is unactionable.
        logger.error(
            "AgentCore Memory discovery failed (region=%s); memory DISABLED.",
            region, exc_info=True,
        )
        return None


@dataclass(frozen=True)
class AppConfig:
    cognito_user_pool_id: Optional[str]
    cognito_client_id: Optional[str]
    cognito_client_secret: Optional[str]
    agentcore_runtime_arn: Optional[str]
    agentcore_gateway_url: Optional[str]
    agentcore_gateway_id: Optional[str]
    onboarding_gateway_url: Optional[str]
    memory_id: Optional[str]
    aws_region: str
    dev_mode: bool
    dev_user_id: str
    guardrail_id: Optional[str]
    guardrail_version: Optional[str]
    guardrail_enabled: bool
    feedback_table_name: Optional[str]
    usage_table_name: Optional[str]
    guardrail_table_name: Optional[str]
    app_settings_table_name: Optional[str]
    prompt_templates_table_name: Optional[str]
    dashboard_cards_table_name: Optional[str]

    @property
    def has_cognito(self) -> bool:
        return bool(self.cognito_user_pool_id and self.cognito_client_id)

    @property
    def has_runtime(self) -> bool:
        if self.agentcore_runtime_arn:
            return True
        # Local/dev mode runs the orchestrator in-process (chat_client.LocalChatClient
        # + orchestrator.get_orchestrator), so no AgentCore runtime is expected. The
        # call sites (get_chat_client, the scheduler, /api/ask) take their local
        # fallback branch when this returns False.
        if self.is_local_mode:  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
            return False
        # Cloud mode with no runtime ARN is fatal: the app cannot reach the orchestrator.
        raise RuntimeError(
            "AGENTCORE_RUNTIME_ARN not configured and orchestrator runtime not found. "
            "Run deploy.sh to create the orchestrator runtime, or set AGENT_MODE=local "
            "for local development."
        )

    @property
    def is_local_mode(self) -> bool:
        """True when the app runs the agents in-process (ADR-0023), NOT behind a
        cloud AgentCore gateway/runtime.

        The single explicit local-mode signal, shared by ``has_runtime``, the
        onboarding tool-source switch (``onboard_agent._get_onboard_tools``)
        and the runtime boot-time env gate (:func:`local_mode_env` is the
        module-level home): set ``AGENT_MODE=local`` (or ``DEV_MODE``) for
        local development. Everything else is treated as cloud/deployed. Kept
        explicit so the onboarding agent never *silently* falls back to
        in-process tools when a gateway URL is merely missing (project
        steering: no silent fallback).
        """
        return local_mode_env() or self.dev_mode

    @property
    def has_gateway(self) -> bool:
        return bool(self.agentcore_gateway_url and self.agentcore_gateway_id)

    @property
    def has_onboarding_gateway(self) -> bool:
        """True when the onboarding AgentCore gateway URL is configured.

        Set from the ``ONBOARDING_GATEWAY_URL`` env (ADTOnboardingStack output). In
        cloud mode this MUST be present — the in-app onboarding agent federates its
        tools from this gateway (CUSTOM_JWT) with the forwarded user bearer token.
        """
        return bool(self.onboarding_gateway_url)

    @property
    def has_memory(self) -> bool:
        return bool(self.memory_id)

    @property
    def has_guardrail(self) -> bool:
        return bool(self.guardrail_id and self.guardrail_enabled)

    @classmethod
    def from_env(cls) -> AppConfig:
        # AGENTCORE_RUNTIME_ARN may be "placeholder" if deploy.sh hasn't run yet.
        # Auto-discover the real ARN from AgentCore control plane in that case.
        runtime_arn = _env("AGENTCORE_RUNTIME_ARN")
        if not runtime_arn or runtime_arn == "placeholder":
            region = _env("AWS_REGION") or "us-east-1"
            gateway_url = _env("AGENTCORE_GATEWAY_URL")
            gateway_id = _env("AGENTCORE_GATEWAY_ID")
            if gateway_url and gateway_id:
                runtime_arn = _discover_orchestrator_arn(region)

        # Memory id resolution (ADR-0006). There is no cross-stack MEMORY_ID env
        # injection; discovery-by-name is the PRIMARY source in BOTH local and
        # cloud mode:
        #   (a) MEMORY_ID set/non-empty -> use it verbatim (explicit override
        #       escape hatch; no control-plane call).
        #   (b) else -> discover our Memory resource by exact name prefix in
        #       BOTH modes. _discover_memory_id fails LOUD on ambiguity (>1
        #       ACTIVE match logs ERROR and returns None) and stays quiet when
        #       nothing is deployed yet (0 match -> None -> has_memory False).
        memory_id = _env("MEMORY_ID")
        if not memory_id:
            memory_id = _discover_memory_id(_env("AWS_REGION") or "us-east-1")

        return cls(
            cognito_user_pool_id=_env("COGNITO_USER_POOL_ID"),
            cognito_client_id=_env("COGNITO_CLIENT_ID"),
            cognito_client_secret=_env("COGNITO_CLIENT_SECRET"),
            agentcore_runtime_arn=runtime_arn,
            agentcore_gateway_url=_env("AGENTCORE_GATEWAY_URL"),
            agentcore_gateway_id=_env("AGENTCORE_GATEWAY_ID"),
            onboarding_gateway_url=_env("ONBOARDING_GATEWAY_URL"),
            memory_id=memory_id,
            aws_region=_env("AWS_REGION") or "us-east-1",
            dev_mode=_bool_env("DEV_MODE"),
            dev_user_id=_env("DEV_USER_ID") or "dev-user-001",
            guardrail_id=_env("GUARDRAIL_ID"),
            guardrail_version=_env("GUARDRAIL_VERSION"),
            guardrail_enabled=os.environ.get("GUARDRAIL_ENABLED", "true").lower() not in ("0", "false", "no"),
            feedback_table_name=_env("FEEDBACK_TABLE_NAME"),
            usage_table_name=_env("USAGE_TABLE_NAME"),
            guardrail_table_name=_env("GUARDRAIL_TABLE_NAME"),
            app_settings_table_name=_env("APP_SETTINGS_TABLE_NAME"),
            prompt_templates_table_name=_env("PROMPT_TEMPLATES_TABLE_NAME"),
            dashboard_cards_table_name=_env("DASHBOARD_CARDS_TABLE_NAME"),
        )


# ---------------------------------------------------------------------------
# Config cache.
#
# The config is loaded once per process and cached -- EXCEPT while the
# orchestrator runtime ARN is unresolved in cloud mode. That state is
# transient by design in unattended (Workshop Studio) provisioning: the ECS
# task can start before the EngineRuntimeBootstrap custom resource has created
# the ``adt_orchestrator`` runtime (observed live 2026-09-10: compute stack
# CREATE_COMPLETE at 15:38, runtime created at 15:55, every /api/ask a 500
# until a manual ECS redeploy). deploy.sh never hits this because it creates
# the runtime before deploying the app. A plain ``@lru_cache`` pinned the
# ``None`` for the life of the process; instead, re-attempt discovery on the
# next call once ``_UNRESOLVED_RETRY_SECS`` has passed, and pin the config
# permanently as soon as an ARN resolves.
# ---------------------------------------------------------------------------
_UNRESOLVED_RETRY_SECS = 15.0
_cached_config: Optional[AppConfig] = None
_cached_config_loaded_at = 0.0


def _runtime_unresolved(cfg: AppConfig) -> bool:
    """True when cloud mode has no orchestrator ARN yet (retry-worthy)."""
    if cfg.agentcore_runtime_arn:
        return False
    if cfg.dev_mode or os.environ.get("AGENT_MODE", "").strip().lower() == "local":
        return False  # local/dev mode never has (or needs) a runtime ARN
    return True


def get_config() -> AppConfig:
    global _cached_config, _cached_config_loaded_at
    cfg = _cached_config
    if cfg is not None:
        if not _runtime_unresolved(cfg):
            return cfg
        if time.monotonic() - _cached_config_loaded_at < _UNRESOLVED_RETRY_SECS:
            return cfg
        logger.info("Orchestrator runtime ARN still unresolved; re-attempting discovery.")
    was_unresolved = cfg is not None
    cfg = AppConfig.from_env()
    _cached_config = cfg
    _cached_config_loaded_at = time.monotonic()
    if was_unresolved and cfg.agentcore_runtime_arn:
        logger.info("Orchestrator runtime ARN resolved: %s", cfg.agentcore_runtime_arn)
    return cfg


def peek_config() -> Optional[AppConfig]:
    """The config :func:`get_config` last loaded, or ``None`` when nothing is
    loaded yet. Never loads, discovers or re-attempts discovery (no AWS
    call), so a request can read what is known without waiting on it."""
    return _cached_config


def _cache_clear() -> None:
    """Drop the cached config (test hook; mirrors functools.lru_cache's API)."""
    global _cached_config, _cached_config_loaded_at
    _cached_config = None
    _cached_config_loaded_at = 0.0


get_config.cache_clear = _cache_clear  # type: ignore[attr-defined]
