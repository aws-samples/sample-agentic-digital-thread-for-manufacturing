# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared pure helpers for building AgentCore runtime create-args (Wave 2, 2.3R).

These four helpers are shared by the in-app create path
(``agent_manager._start_cloud``) and the provisioner
(``agents.provisioner.handlers.create_runtime``) so the two
callers cannot drift. All are PURE except :func:`upsert_connection_secret`, which
takes an injected Secrets Manager client (no module-level boto3 client, so unit
tests pass a stub).

ADDITIVE / behavior-preserving: ``_start_cloud`` now imports these instead of
inlining them; its surrounding control flow (the list/reuse-existing-runtime
branch, the AZ-strip retry loop, and the outer Secrets-Manager try/except
fallback) is UNCHANGED.
"""
from __future__ import annotations

import copy
import functools
import json
import logging
import os
import threading

from .connection_types import (
    KNOWLEDGE_GRAPH_CONNECTION_TYPE,
    QUERY_BRIDGE_CONNECTION_TYPE,
)
from .response_integrity_security import sensitive_field
from .runtime_env_contract import (  # noqa: F401 - re-exported for compat
    ONBOARDING_SESSIONS_TABLE_DEFAULT,
    QUICK_GATEWAY_TOKEN_REF,
    RuntimeEnvSources,
    parse_quick_gateway_token_ref,
    reconcile_env,
)

logger = logging.getLogger(__name__)

#: The ONE lock for read-modify-writes of an AgentCore runtime's config (and
#: the agent-config secret the runtime reads) inside the app process.
#: ``update_agent_runtime`` is full replacement, so a writer that reads a
#: runtime, lets another writer land, then writes back silently reverts it
#: (a Settings model switch undone by a business-context refresh, an auth
#: repair undone by a model bump). Hold it around the read and the write,
#: never across a READY wait, and never on the event loop thread: every
#: holder runs in a worker thread. Re-entrant because the settings save holds
#: it across its fleet loop and each per-runtime bump takes it again.
RUNTIME_CONFIG_LOCK = threading.RLock()

# The CDK-fixed physical name of the onboarding-sessions table now lives in
# agents/runtime_env_contract.py (the ONE runtime-env source of truth) and is
# re-exported above for existing importers. Waves 5/6 ride provenance-query
# and investigation-trace sidecars on this table through JobStore, so EVERY
# agent runtime env must carry ONBOARDING_SESSIONS_TABLE.


def provenance_store_env() -> dict:
    """The provenance-store env var every agent runtime must carry (Waves 5/6).

    Ambient ``ONBOARDING_SESSIONS_TABLE`` wins when set (deploy-time override
    convention, same as the app task env); otherwise fall back to the
    CDK-fixed physical table name so a caller whose OWN process env lacks the
    variable (deploy shell, ad-hoc refresh) can never write a runtime env
    without it.
    """
    return {
        "ONBOARDING_SESSIONS_TABLE": (
            os.environ.get("ONBOARDING_SESSIONS_TABLE", "").strip()
            or ONBOARDING_SESSIONS_TABLE_DEFAULT
        )
    }


def list_all_agent_runtimes(control) -> list:
    """EVERY AgentCore runtime, across ALL ``list_agent_runtimes`` pages.

    The control-plane API returns at most one page (10 items observed) plus a
    ``nextToken``. Every raw ``control.list_agent_runtimes()`` call silently
    truncates once the account has >10 runtimes — which made a runtime on
    page 2 look ABSENT: the startup fast path then "repaired" a phantom
    dangling target by deleting/recreating that runtime on EVERY task start,
    and the restore loop aborted before the direct-source sweep. This helper
    is the ONE sanctioned way to enumerate runtimes; do not call
    ``list_agent_runtimes`` directly for existence/lookup checks.

    Normalizes the response item key (``agentRuntimes`` today,
    ``agentRuntimeSummaries`` defensively). Raises whatever the client raises
    — callers keep their existing error handling.
    """
    items: list = []
    for page in iter_agent_runtime_pages(control):
        items.extend(page)
    return items


class RuntimeListingCapped(RuntimeError):
    """A capped :func:`iter_agent_runtime_pages` listing had pages left."""


def iter_agent_runtime_pages(control, *, max_results: int | None = None,
                             max_pages: int | None = None):
    """Yield each ``list_agent_runtimes`` page's runtime summaries, in order.

    The paging loop behind :func:`list_all_agent_runtimes`, for a caller that
    must bound its own work (a per-request route): the API is called only
    when the caller asks for the next page, so a caller that stops iterating
    makes no further call. ``max_results`` is sent as ``maxResults`` (left
    out when ``None``, so the API's default page size applies). With
    ``max_pages``, a ``nextToken`` still left after that many pages raises
    :class:`RuntimeListingCapped` instead of making another call, so a capped
    listing can never pass for a complete one. Client errors propagate.
    """
    token = None
    pages = 0
    while True:
        kwargs = {} if max_results is None else {"maxResults": max_results}
        if token:
            kwargs["nextToken"] = token
        resp = control.list_agent_runtimes(**kwargs)
        pages += 1
        yield resp.get("agentRuntimes", resp.get("agentRuntimeSummaries", []))
        token = resp.get("nextToken")
        if not token:
            return
        if max_pages is not None and pages >= max_pages:
            raise RuntimeListingCapped(
                f"AgentCore runtimes still to list after {max_pages} pages")


def list_all_gateway_targets(control, gateway_id: str) -> list:
    """EVERY target on ``gateway_id``, across ALL ``list_gateway_targets`` pages.

    The control-plane API returns at most 10 targets per call plus a
    ``nextToken``. A single unpaginated call cannot see a target on page 2,
    so a reset that looks a target up by name finds nothing, deletes the
    runtime, and reports done while the target survives, pointing at a
    deleted runtime (every orchestrator turn then sees a tool whose MCP
    initialize fails). Absence of a target is only proven over ALL pages.
    This helper is the ONE sanctioned way to enumerate gateway targets under
    ``agents/``; do not call ``list_gateway_targets`` directly.

    The first call passes ONLY ``gatewayIdentifier``; ``nextToken`` is added
    only when the previous page returned a non-empty string token. Every page
    is collected before returning, so a caller that deletes targets while
    iterating never invalidates a pagination token. Normalizes the response
    item key (``items`` today, ``targets`` defensively). Raises whatever the
    client raises — callers keep their existing error handling — and raises
    ``RuntimeError`` if the API repeats a token (the listing could otherwise
    never finish, and a truncated list would read as a false absence).
    """
    items: list = []
    seen: set = set()
    token = None
    while True:
        if token:
            resp = control.list_gateway_targets(gatewayIdentifier=gateway_id,
                                                nextToken=token)
        else:
            resp = control.list_gateway_targets(gatewayIdentifier=gateway_id)
        items.extend(resp.get("items", resp.get("targets", [])) or [])
        token = resp.get("nextToken")
        if not (isinstance(token, str) and token):
            return items
        if token in seen:
            raise RuntimeError(
                f"list_gateway_targets repeated nextToken for gateway {gateway_id}; "
                "the target listing is incomplete")
        seen.add(token)


def list_all_oauth2_credential_providers(control) -> list:
    """EVERY OAuth2 credential provider, across ALL
    ``list_oauth2_credential_providers`` pages.

    A single call returns one page plus a ``nextToken``. Reading only page 1
    misses a provider on page 2: the ``adt-m2m`` lookup then falls through
    to create, create raises ConflictException, the recovery lookup misses
    it again, and gateway targets are created WITHOUT outbound auth to their
    JWT-protected runtimes. The first call passes no arguments; ``nextToken``
    is added only when the previous page returned a non-empty string token.
    Normalizes the item key (``credentialProviders``, or
    ``oauth2CredentialProviders`` defensively). Raises whatever the client
    raises, and ``RuntimeError`` on a repeated token (a truncated list would
    read as a false absence).
    """
    items: list = []
    seen: set = set()
    token = None
    while True:
        if token:
            resp = control.list_oauth2_credential_providers(nextToken=token)
        else:
            resp = control.list_oauth2_credential_providers()
        items.extend(resp.get("credentialProviders",
                              resp.get("oauth2CredentialProviders", [])) or [])
        token = resp.get("nextToken")
        if not (isinstance(token, str) and token):
            return items
        if token in seen:
            raise RuntimeError(
                "list_oauth2_credential_providers repeated nextToken; "
                "the provider listing is incomplete")
        seen.add(token)


class GatewayTargetLookupError(RuntimeError):
    """The gateway's targets could not be listed, so whether a target exists
    is UNKNOWN — not absent.

    A removal that hits this must stop BEFORE deleting the runtime: deleting
    a runtime whose target may survive strands that target pointing at a
    deleted runtime (every orchestrator turn then sees a tool whose MCP
    initialize fails). The removal stays incomplete and retryable."""

    def __init__(self, gateway_id: str, target_name: str):
        super().__init__(
            f"could not list the targets on gateway {gateway_id}; whether target "
            f"{target_name!r} still exists is unknown")
        self.gateway_id = gateway_id
        self.target_name = target_name


#: The ONE status an updated target is accepted in — and only once its
#: read-back carries exactly what was sent (see bring_gateway_target_in_line).
GATEWAY_TARGET_ACCEPTED_STATUSES = ("READY",)


def gateway_target_update_did_not_take(status: str) -> bool:
    """Whether *status* ends an update attempt at once: the target is going
    away (``DELETING``), ``FAILED``, any ``*_UNSUCCESSFUL`` state, or any
    ``*_PENDING_AUTH`` state (waiting on an authorization no provision can
    grant). Every other non-accepted status (``CREATING``/``UPDATING``/
    ``SYNCHRONIZING``, empty, or one the service adds later) is polled."""
    return (status in ("DELETING", "FAILED") or status.endswith("_UNSUCCESSFUL")
            or status.endswith("_PENDING_AUTH"))


#: Transitional NON-deleting target states a delete must be RETRIED through,
#: not mistaken for "already deleting". ``DeleteGatewayTarget`` is refused with
#: a ValidationException while the target is still settling (observed live
#: 2026-10-09: a target reset ~5 s after it finished provisioning was
#: ``Synchronizing`` and the service said "Retry the request when the target is
#: in a terminal state"). The target returns to a terminal state on its own, so
#: a retry succeeds; treating the refusal as "already deleting" waited 900 s on
#: a target that never left.
_TARGET_RETRY_ERROR_MARKERS = ("retry the request", "synchronizing state",
                               "creating state", "updating state")
#: Markers for a target that is genuinely going away — do NOT retry the delete,
#: just wait for absence.
_TARGET_DELETING_ERROR_MARKERS = ("deleting state", "already deleting")
#: The ONLY SDK error codes whose refusal may be a transitional (retryable)
#: state. ``DeleteGatewayTarget`` returns ValidationException while the target
#: is settling ("Retry the request when the target is in a terminal state") and
#: ConflictException for a concurrent state change. A permanent error such as
#: AccessDeniedException / UnauthorizedOperation / ThrottlingException is NEVER
#: transitional even when a separate ``get_gateway_target`` happens to read
#: SYNCHRONIZING — retrying it just burns the whole deadline on an error that
#: will never clear.
_TARGET_RETRYABLE_ERROR_CODES = ("ValidationException", "ConflictException")


def gateway_delete_error_code(exc: Exception) -> str:
    """The SDK error code for a ``delete_gateway_target`` failure: botocore's
    ``e.response['Error']['Code']`` for a ClientError, else the exception class
    name. ``""`` when neither is available."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__ or ""


def classify_gateway_target_delete(err_text: str, status: str,
                                   err_code: str = "") -> str:
    """Classify a refused ``delete_gateway_target`` from the SDK error CODE
    first, then the error text and the independently-read target status.

    Returns one of:
      * ``"deleting"`` — the target is genuinely going away (error text says a
        Deleting state, or ``status`` is ``DELETING``); wait for absence, do not
        re-issue the delete.
      * ``"transitional"`` — a RETRYABLE code (``ValidationException`` /
        ``ConflictException``) refused the delete with a SETTLING-STATE MARKER in
        the text ("Retry the request", "Synchronizing state", "Creating state",
        "Updating state"); re-issue the delete after a short backoff.
      * ``"other"`` — anything else, including a permanent error
        (``AccessDeniedException``/``UnauthorizedOperation``/throttling), a
        retryable code with NO settling-state reason (a request-defect
        ``ValidationException``, a generic ``ConflictException``), i.e. the
        removal is NOT done.

    The transitional decision is driven by the error CODE + the refusal TEXT,
    NOT by the independently-read ``status``: a separate ``get_gateway_target``
    reading ``SYNCHRONIZING`` must not turn a non-state error into a retry (it
    would burn the whole deadline on a failure that will never clear). ``status``
    is used ONLY for the ``DELETING`` short-circuit.

    ``err_text`` is matched case-insensitively; ``err_code`` is the SDK code
    (see :func:`gateway_delete_error_code`); ``status`` is the current
    ``get_gateway_target`` status (``""`` when unknown)."""
    low = (err_text or "").lower()
    st = (status or "").upper()
    code = err_code or ""
    # Genuinely going away wins regardless of code.
    if any(m in low for m in _TARGET_DELETING_ERROR_MARKERS) or st == "DELETING":
        return "deleting"
    # Transitional retries ONLY for a known retryable code whose refusal TEXT
    # names a settling state. The independently-read status is NOT sufficient.
    if code in _TARGET_RETRYABLE_ERROR_CODES and any(
            m in low for m in _TARGET_RETRY_ERROR_MARKERS):
        return "transitional"
    return "other"


def read_gateway_target_status(control, gateway_id: str, target_id: str) -> tuple[str, bool]:
    """Best-effort ``(status, gone)`` for one target via ``get_gateway_target``.

    ``gone`` is True when the read reports the target absent (a
    ResourceNotFound-shaped error); ``status`` is ``""`` when unknown. Never
    raises — a read failure returns ``("", False)`` so a classifier falls back
    to the error text alone."""
    try:
        detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        return str((detail or {}).get("status") or ""), False
    except Exception as e:  # noqa: BLE001 - best-effort status read
        name = type(e).__name__
        if "ResourceNotFound" in name or "NotFound" in name or "notfound" in str(e).lower():
            return "", True
        return "", False


class GatewayTargetUpdateError(RuntimeError):
    """An existing gateway target could not be brought in line with the
    runtime it serves.

    The target is never deleted on this path, but it may still point at the
    wrong runtime, so the provision is NOT done: it raises rather than
    reporting success, and a retry runs the same check again."""


class GatewayTargetRecreateError(GatewayTargetUpdateError):
    """A non-READY gateway target was to be deleted and created again, and
    the provision cannot vouch for what is left: the delete went through but
    the create failed (no target exists), or the delete failed and the old
    target is gone, going (DELETING) or unreadable.

    Same contract as the update path: raised out of the provision, nothing
    is registered against the missing target, and a retry starts over (the
    name is free, or the target is still found non-READY)."""


def _mcp_server_endpoint(target_configuration) -> str | None:
    """``targetConfiguration.mcp.mcpServer.endpoint``, or ``None`` when absent."""
    mcp = (target_configuration or {}).get("mcp") or {}
    endpoint = (mcp.get("mcpServer") or {}).get("endpoint")
    return endpoint if isinstance(endpoint, str) else None


def gateway_target_endpoint(detail: dict) -> str | None:
    """The MCP server endpoint a ``get_gateway_target`` *detail* carries, or
    ``None`` when its configuration has none (missing or not a string)."""
    return _mcp_server_endpoint((detail or {}).get("targetConfiguration"))


def _credential_signature(configs) -> tuple:
    """The outbound-auth fields provisioning sets, per credential config:
    type, OAuth provider ARN, scopes (order-free) and grant type."""
    signature = []
    for cfg in configs or []:
        cfg = cfg or {}
        oauth = (cfg.get("credentialProvider") or {}).get("oauthCredentialProvider") or {}
        signature.append((cfg.get("credentialProviderType"), oauth.get("providerArn"),
                          tuple(sorted(oauth.get("scopes") or ())), oauth.get("grantType")))
    return tuple(signature)


def gateway_target_drift(desired: dict, detail: dict) -> list[str]:
    """What an existing gateway target (``get_gateway_target`` *detail*) has
    that this provision would not create (*desired* = the
    ``create_gateway_target`` kwargs). Empty means reuse it unchanged.

    * ``"endpoint"`` — its MCP server endpoint is not, byte for byte, the one
      built for this runtime (both strings come from the same URL builder, so
      no parsing). A target whose runtime was deleted and recreated still
      reads READY but points at the old runtime: every tool call 404s.
    * ``"credentials"`` — this provision sets an outbound credential config
      and the target does not carry the same one.

    A provision WITHOUT a credential config never flags credentials: the
    credential-provider lookup also comes back empty on a transient failure,
    and stripping a working target's outbound auth then would break it. Pure.
    """
    drift = []
    want_endpoint = _mcp_server_endpoint(desired.get("targetConfiguration"))
    if (want_endpoint is not None
            and _mcp_server_endpoint(detail.get("targetConfiguration")) != want_endpoint):
        drift.append("endpoint")
    want_creds = desired.get("credentialProviderConfigurations")
    if want_creds and (_credential_signature(detail.get("credentialProviderConfigurations"))
                       != _credential_signature(want_creds)):
        drift.append("credentials")
    return drift


def gateway_target_update_kwargs(target_id: str, desired: dict, detail: dict) -> dict:
    """``update_gateway_target`` kwargs that bring an existing target to *desired*.

    Built like a create, not a patch (the API requires
    ``targetConfiguration``, so the call replaces it): gateway, target id,
    name, description and the desired target configuration; the credential
    config this provision sets, or the target's own when it sets none (never
    downgrade outbound auth, see :func:`gateway_target_drift`); and the
    target's own ``metadataConfiguration`` and ``privateEndpoint`` when it
    has them (create never sets either, so only what drifted changes; leaving
    ``privateEndpoint`` out of a full replacement could cut the target's VPC
    connectivity). Pure; never mutates its inputs."""
    kwargs = {
        "gatewayIdentifier": desired["gatewayIdentifier"],
        "targetId": target_id,
        "name": desired.get("name") or detail.get("name", ""),
        "targetConfiguration": copy.deepcopy(desired["targetConfiguration"]),
    }
    description = desired.get("description") or detail.get("description")
    if description:
        kwargs["description"] = description
    creds = (desired.get("credentialProviderConfigurations")
             or detail.get("credentialProviderConfigurations"))
    if creds:
        kwargs["credentialProviderConfigurations"] = copy.deepcopy(creds)
    for carried in ("metadataConfiguration", "privateEndpoint"):
        if detail.get(carried):
            kwargs[carried] = copy.deepcopy(detail[carried])
    return kwargs


def bring_gateway_target_in_line(control, target_id: str, desired: dict, detail: dict,
                                 *, sleep, poll_seconds: float = 5.0,
                                 max_polls: int = 24) -> dict:
    """Update an existing target to *desired* and wait until the update has
    verifiably landed; returns the settled detail (the caller then syncs).

    Accepted ONLY when a read-back is ``READY`` (GATEWAY_TARGET_ACCEPTED_STATUSES)
    AND carries no drift against *desired* — a READY read-back that still
    shows the old endpoint/credentials is stale and polled again. Polls at
    most ``max_polls`` times ``poll_seconds``. Raises
    :class:`GatewayTargetUpdateError` when the update or a read-back call
    fails, at once when a read-back status means the update did not take
    (:func:`gateway_target_update_did_not_take`), and when the budget runs
    out. Never deletes the target."""
    gateway_id = desired["gatewayIdentifier"]
    try:
        control.update_gateway_target(
            **gateway_target_update_kwargs(target_id, desired, detail))
    except Exception as exc:  # noqa: BLE001 - re-raised typed, cause chained
        raise GatewayTargetUpdateError(
            f"update_gateway_target failed for target {target_id} on gateway "
            f"{gateway_id}: {exc}") from exc
    status, drift = "", []
    for poll in range(max_polls + 1):
        try:
            settled = control.get_gateway_target(gatewayIdentifier=gateway_id,
                                                 targetId=target_id)
        except Exception as exc:  # noqa: BLE001 - re-raised typed, cause chained
            raise GatewayTargetUpdateError(
                f"could not read gateway target {target_id} back after the update: "
                f"{exc}") from exc
        status = settled.get("status", "")
        if gateway_target_update_did_not_take(status):
            raise GatewayTargetUpdateError(
                f"gateway target {target_id} is {status} after the update: "
                f"{settled.get('statusReasons') or 'no reason given'}")
        drift = gateway_target_drift(desired, settled)
        if status in GATEWAY_TARGET_ACCEPTED_STATUSES and not drift:
            return settled
        if poll < max_polls:
            sleep(poll_seconds)
    state = (f"{status} but still reads back a different {' and '.join(drift)}"
             if status in GATEWAY_TARGET_ACCEPTED_STATUSES else status or "without a status")
    raise GatewayTargetUpdateError(
        f"gateway target {target_id} still {state} {max_polls * poll_seconds:.0f}s "
        "after the update")


def strip_credential_keys(config: dict) -> dict:
    """Return a copy of ``config`` with credential-bearing keys removed.

    Key sensitivity is decided by the shared
    :func:`agents.response_integrity_security.sensitive_field` classifier
    (password / passphrase / secret / token / api_key / credential …), so the
    env-fallback path below and the integrity data paths cannot drift on what
    counts as a secret. Non-secret keys (host, port, database, …) are kept.
    """
    return {k: v for k, v in (config or {}).items() if not sensitive_field(k)}


def _query_bridge_env() -> dict:
    """QueryBridge composed runtime env — the ADT app itself is the "connection".

    The internal ALB URL (the app's own ADT_APP_INTERNAL_URL env — never
    hardcoded) plus the x-quick-auth shared secret the app's middleware
    validates. No credential secret exists for this target (like
    KnowledgeGraph). The "placeholder" SSM seed sentinel is normalized away so
    a not-yet-written token is honestly absent rather than guessable.

    When the app task carries ``QUICK_GATEWAY_TOKEN_REF`` (the ARN of the
    secret that holds the token, set by the connector path; format in
    ``runtime_env_contract.parse_quick_gateway_token_ref``), the runtime gets
    that ARN INSTEAD of the token: runtime env is readable by any caller of
    GetAgentRuntime, and the bridge reads the value with its own role on first
    use (agents/quick_gateway_token.py). With no reference (a --quick-api or
    base stack, or one from before references existed) the env is exactly
    what it was before. A
    reference that does not parse is ignored with a warning, which also keeps
    the env as before."""
    app_url = (os.environ.get("ADT_APP_URL")
               or os.environ.get("ADT_APP_INTERNAL_URL", "")).strip()
    token = os.environ.get("QUICK_GATEWAY_TOKEN", "").strip()
    if token.lower() == "placeholder":  # nosec B105 - deploy.sh SSM seed sentinel, not a credential
        token = ""  # nosec B105 - cleared sentinel, not a credential
    ref = os.environ.get(QUICK_GATEWAY_TOKEN_REF, "").strip()
    env = {}
    if app_url:
        env["ADT_APP_URL"] = app_url
    if ref and parse_quick_gateway_token_ref(ref) is not None:
        env[QUICK_GATEWAY_TOKEN_REF] = ref
        return env
    if ref:
        logger.warning(
            "%s is not a Secrets Manager secret ARN or an SSM parameter ARN; "
            "the query-bridge runtime gets the token in its env as before",
            QUICK_GATEWAY_TOKEN_REF)
    if token:
        env["QUICK_GATEWAY_TOKEN"] = token
    return env


def runtime_connection_env(
    connection_type: str, connection_secret_arn: str, effective_config: dict
) -> dict:
    """Connection-specific runtime env vars.

    KnowledgeGraph systems (digital-thread) connect to the Neptune graph via the
    runtime IAM role — there is no DB credential secret, so we pass
    GRAPH_IDENTIFIER (which names the graph). Other systems pass the Secrets
    Manager ARN (preferred) or a truncated inline CONNECTION_CONFIG fallback
    (kept under AgentCore's 4000-byte env limit).

    SECURITY: the inline fallback is only reached when the Secrets Manager
    write failed (or was skipped). Runtime env vars are plaintext — visible in
    the AgentCore console and describe calls — so credential keys are STRIPPED
    from the fallback config (never silently dropping the non-secret keys).
    The runtime cannot authenticate to the system until the agent-config
    secret is repaired (re-onboard / re-run provisioning).
    """
    if connection_type == KNOWLEDGE_GRAPH_CONNECTION_TYPE:
        graph_id = os.environ.get("GRAPH_IDENTIFIER", "")
        return {"GRAPH_IDENTIFIER": graph_id} if graph_id else {}
    if connection_type == QUERY_BRIDGE_CONNECTION_TYPE:
        return _query_bridge_env()
    if connection_secret_arn:
        return {"CONNECTION_SECRET_ARN": connection_secret_arn}
    safe_config = strip_credential_keys(effective_config)
    dropped = sorted(set(effective_config or {}) - set(safe_config))
    if dropped:
        logger.warning(
            "SECURITY: keys %s were STRIPPED from the plaintext CONNECTION_CONFIG "
            "runtime env var and will NOT reach the runtime. That env var is the "
            "fallback when there is no Secrets Manager ARN. The agent-config secret "
            "must be repaired (re-onboard or re-run provisioning) before this system "
            "can authenticate with those credential keys.",
            dropped,
        )
    return {"CONNECTION_CONFIG": json.dumps(safe_config)[:400]}


def container_uri(ecr_image_uri: str) -> str:
    """Normalize an ECR image URI.

    DockerImageAsset URIs are fully qualified — either tagged
    (``<acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<hash>``) or digested
    (``...@sha256:...``). Only append ``:latest`` for a bare repo URI that has
    neither a digest nor a tag on its final path segment.
    """
    last_segment = ecr_image_uri.rsplit("/", 1)[-1]
    if "@sha256" not in ecr_image_uri and ":" not in last_segment:
        return f"{ecr_image_uri}:latest"
    return ecr_image_uri


def network_config(subnets: list[str], security_group: str) -> dict:
    """Build the AgentCore ``networkConfiguration`` block (VPC if both supplied)."""
    if subnets and security_group:
        return {
            "networkMode": "VPC",
            "networkModeConfig": {"subnets": subnets, "securityGroups": [security_group]},
        }
    return {"networkMode": "PUBLIC"}


def upsert_connection_secret(secrets_client, secret_name: str, secret_value: str) -> str:
    """Create-or-update a Secrets Manager secret; return its ARN.

    Mirrors the create→ResourceExistsException→put_secret_value upsert that both
    callers used inline. ``secrets_client`` is injected (no module-level client)
    so the provisioner handler can pass a stub in unit tests; the in-app caller
    keeps its own outer try/except fallback around this call (unchanged).
    """
    try:
        resp = secrets_client.create_secret(Name=secret_name, SecretString=secret_value)
        return resp["ARN"]
    except secrets_client.exceptions.ResourceExistsException:
        secrets_client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        return secrets_client.describe_secret(SecretId=secret_name)["ARN"]


# --------------------------------------------------------------------------- #
# Agent-config secret size
# --------------------------------------------------------------------------- #
# The adt/agent-config/<id> secret carries the connection config (login
# included), the baked table description, the system prompt, the business
# context and the KNOWN CONCEPT MAPPINGS items in ONE value. Secrets Manager
# refuses a value over 65,536 bytes, and a refused write leaves the runtime
# without its login (runtime_connection_env strips credentials from the
# plaintext fallback). The concept mappings are the one part that can shrink
# without losing anything the agent needs to connect, so every writer of the
# secret fits them to the serialized value first.

#: Byte bound for a serialized agent-config value: 4 KiB under the Secrets
#: Manager limit of 65,536 bytes.
AGENT_CONFIG_SECRET_MAX_BYTES = 61_440

#: Secrets Manager's own limit on a secret value, in bytes.
SECRETS_MANAGER_VALUE_LIMIT = 65_536

#: The agent-config key that carries the KNOWN CONCEPT MAPPINGS items.
CONCEPT_MAPPINGS_KEY = "concept_mappings"


def _concept_item_key(item: dict) -> tuple:
    return (str(item.get("concept") or ""), str(item.get("field") or ""))


def concept_round_robin_order(items: list) -> list:
    """``items`` (``{"concept", "field", ...}`` dicts) in fair order: each
    concept's first item (concepts sorted, a concept's items in field order),
    then each concept's second, and so on. Any prefix keeps every concept
    before any concept gets a second item, while there are no more concepts
    than the prefix is long."""
    by_concept: dict = {}
    for it in sorted(items, key=_concept_item_key):
        by_concept.setdefault(_concept_item_key(it)[0], []).append(it)
    queues = [by_concept[c] for c in sorted(by_concept)]
    order: list = []
    for depth in range(max((len(q) for q in queues), default=0)):
        order.extend(q[depth] for q in queues if depth < len(q))
    return order


def round_robin_concept_items(items: list, max_items: int) -> list:
    """At most ``max_items`` of ``items``, one per concept per round
    (:func:`concept_round_robin_order`), sorted by concept then field."""
    return sorted(concept_round_robin_order(items)[:max(max_items, 0)],
                  key=_concept_item_key)


def _value_bytes(payload: dict) -> int:
    return len(json.dumps(payload).encode("utf-8"))


def fit_agent_config_payload(payload: dict, *, label: str = "",
                             max_bytes: int = AGENT_CONFIG_SECRET_MAX_BYTES) -> dict:
    """``payload`` with its concept mappings trimmed so ``json.dumps`` of it is
    at most ``max_bytes`` bytes.

    Within the bound the payload is returned as it is (byte-identical value).
    Over it, the items are trimmed in :func:`concept_round_robin_order`, so
    every concept keeps an item before any keeps a second, to the most that
    fit; when even one item per concept does not fit, the key is dropped.
    Without its concept mappings a payload over the bound but within Secrets
    Manager's limit is returned without them, since that value is still
    accepted; one over the limit even without them is returned unchanged,
    since nothing here can make it fit. Logs counts and sizes only, never
    values (``label`` names the system)."""
    if _value_bytes(payload) <= max_bytes:
        return payload
    label = label or "this system"
    items = payload.get(CONCEPT_MAPPINGS_KEY)
    rest = {k: v for k, v in payload.items() if k != CONCEPT_MAPPINGS_KEY}
    rest_bytes = _value_bytes(rest)
    if (isinstance(items, list) and items
            and max_bytes < rest_bytes <= SECRETS_MANAGER_VALUE_LIMIT):
        logger.warning("agent-config for %s is %d bytes without concept mappings, "
                       "over the %d-byte bound: concept mappings dropped (%d items)",
                       label, rest_bytes, max_bytes, len(items))
        return rest
    if not isinstance(items, list) or not items or rest_bytes > max_bytes:
        logger.warning("agent-config for %s is %d bytes, over the %d-byte bound, "
                       "and %d bytes without concept mappings: written unchanged",
                       label, _value_bytes(payload), max_bytes, rest_bytes)
        return payload
    order = concept_round_robin_order([it for it in items if isinstance(it, dict)])

    def with_first(n: int) -> dict:
        out = dict(payload)
        out[CONCEPT_MAPPINGS_KEY] = sorted(order[:n], key=_concept_item_key)
        return out

    fits, too_many = len({_concept_item_key(it)[0] for it in order}), len(order) + 1
    if _value_bytes(with_first(fits)) > max_bytes:
        logger.warning("agent-config for %s: one concept mapping per concept (%d) "
                       "does not fit the %d-byte bound; concept mappings dropped "
                       "(%d items)", label, fits, max_bytes, len(items))
        return rest
    while too_many - fits > 1:    # largest prefix that fits (sizes grow with n)
        mid = (fits + too_many) // 2
        if _value_bytes(with_first(mid)) <= max_bytes:
            fits = mid
        else:
            too_many = mid
    logger.warning("agent-config for %s: concept mappings trimmed to %d of %d items "
                   "to fit the %d-byte bound", label, fits, len(items), max_bytes)
    return with_first(fits)


def agent_config_secret_value(payload: dict, *, label: str = "") -> str:
    """The agent-config secret value for ``payload``: ``json.dumps`` of it
    after :func:`fit_agent_config_payload`."""
    return json.dumps(fit_agent_config_payload(payload, label=label))


def _with_provenance_store(env_vars: dict) -> dict:
    """Copy of ``env_vars`` with ``ONBOARDING_SESSIONS_TABLE`` filled from
    :func:`provenance_store_env` when absent or empty (Waves 5/6: a
    full-replacement update must never strip the provenance store just
    because the CALLING process env lacked the variable — the deploy-shell
    [2f] refresh was the live instance). A caller-supplied non-empty value
    always wins. Delegates to the generalized
    :func:`agents.runtime_env_contract.reconcile_env` with TABLE-ONLY
    sources, so the merge writers' behavior is unchanged (the system role's
    identity-ARN overwrite policy no-ops on an empty source)."""
    return reconcile_env("system", env_vars, RuntimeEnvSources(
        onboarding_sessions_table=provenance_store_env()[
            "ONBOARDING_SESSIONS_TABLE"]))


def reconcile_required_env(env_vars: dict) -> dict:
    """Public alias of the merge-path required-key fill (see
    :func:`_with_provenance_store`) for out-of-package merge writers
    (``scripts/_query_mode_lib.py``'s recreate path)."""
    return _with_provenance_store(env_vars)


def sanitized_network_config(details: dict) -> dict:
    """The runtime's ``networkConfiguration`` with the immutable field dropped.

    ``requireServiceS3Endpoint`` is IMMUTABLE for runtimes created on/after
    2026-06-11: ``get_agent_runtime`` returns it, but re-sending it fails
    ``update_agent_runtime`` with a ValidationException. Falls back to PUBLIC
    when absent. The ONE spelling — deploy.sh, admin_routes and
    scripts/_query_mode_lib all route through builders that use this."""
    net_cfg = details.get("networkConfiguration") or {"networkMode": "PUBLIC"}
    nmc = net_cfg.get("networkModeConfig")
    if isinstance(nmc, dict) and "requireServiceS3Endpoint" in nmc:
        net_cfg = {
            **net_cfg,
            "networkModeConfig": {k: v for k, v in nmc.items()
                                  if k != "requireServiceS3Endpoint"},
        }
    return net_cfg


#: Optional runtime settings GetAgentRuntime returns and UpdateAgentRuntime
#: accepts, carried into every update verbatim. ``update_agent_runtime`` is a
#: full replacement, so a setting an update leaves out is reset. This is every
#: field the two shapes share in the botocore service model except the ones
#: :func:`runtime_update_kwargs` builds on purpose (artifact, role, network
#: config, env, protocol, authorizer); tests/unit/test_runtime_update_carries_settings.py
#: compares it with the installed model.
RUNTIME_UPDATE_CARRIED_FIELDS = (
    "description",
    "requestHeaderConfiguration",
    "lifecycleConfiguration",
    "filesystemConfigurations",
    "capacityProviderConfiguration",
    "metadataConfiguration",
    "hooksConfiguration",
    "resourceConfigurations",
    "platformVersion",
    "allowedPorts",
)


@functools.lru_cache(maxsize=4)
def _update_agent_runtime_input_fields(region: str | None = None) -> frozenset:
    """The field names the INSTALLED botocore ``UpdateAgentRuntime`` input shape
    accepts, read from the service model (no network, no credentials). Cached:
    the model is static for a process. ``frozenset()`` when the model cannot be
    loaded, which makes :func:`supported_carried_fields` fall back to the
    always-present floor rather than guess."""
    try:
        import botocore.session
        model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
        return frozenset(model.operation_model("UpdateAgentRuntime").input_shape.members)
    except Exception:  # noqa: BLE001 - absence degrades to "carry only the always-present ones"
        return frozenset()


#: The carried fields present in the ``UpdateAgentRuntime`` input shape across
#: every botocore version this app has shipped on; the floor used when the
#: service model cannot be read.
_ALWAYS_SUPPORTED_CARRIED_FIELDS = (
    "description",
    "requestHeaderConfiguration",
    "lifecycleConfiguration",
    "filesystemConfigurations",
    "capacityProviderConfiguration",
    "metadataConfiguration",
)


def supported_carried_fields() -> tuple:
    """:data:`RUNTIME_UPDATE_CARRIED_FIELDS` narrowed to the fields the INSTALLED
    ``UpdateAgentRuntime`` input shape actually accepts, in declared order.

    ``update_agent_runtime`` rejects a field its model does not define, so a
    field the declared superset names but the deployed SDK has not shipped yet
    (``hooksConfiguration``, ``resourceConfigurations``, ``platformVersion``,
    ``allowedPorts`` were added after botocore 1.43) must not be sent: carrying
    it fails the whole bump against the deployed app image. When the model is
    unavailable, only the always-present floor is kept, so a bump never sends an
    unsupported field."""
    accepted = _update_agent_runtime_input_fields()
    if not accepted:
        return _ALWAYS_SUPPORTED_CARRIED_FIELDS
    return tuple(f for f in RUNTIME_UPDATE_CARRIED_FIELDS if f in accepted)


def carried_runtime_settings(details: dict) -> dict:
    """Copies of the carried settings *details* holds, limited to the fields the
    installed ``UpdateAgentRuntime`` model accepts (:func:`supported_carried_fields`).

    An empty value (``""``, ``{}``, ``[]``, ``None``) is left out: it means
    the same as an absent one, and some empties are invalid input (an empty
    description, an empty tagged union such as requestHeaderConfiguration). A
    field the deployed SDK does not define is left out too, so the bump is never
    rejected for carrying an unsupported setting."""
    return {key: copy.deepcopy(details[key])
            for key in supported_carried_fields() if details.get(key)}


def runtime_update_kwargs(runtime_id: str, details: dict, env_vars: dict, *,
                          artifact: dict | None = None,
                          role_arn: str | None = None,
                          authorizer_config: dict | None = None,
                          protocol_override: dict | None = None) -> dict:
    """Build ``update_agent_runtime`` kwargs from ``get_agent_runtime`` *details*.

    FULL-REPLACEMENT API: any omitted field resets to its service default, which
    is why ``protocolConfiguration`` is ROUND-TRIPPED — omitting it downgrades an
    MCP runtime to HTTP and presents as MCP ``initialize`` hangs on a READY
    runtime (ADR-0056). Never hardcoded: the orchestrator is legitimately HTTP.
    Also drops the immutable ``requireServiceS3Endpoint``, passes
    ``authorizerConfiguration`` only when a ``customJWTAuthorizer`` is set
    (a tagged union: ``{}`` fails the call), and carries every other mutable
    setting the runtime has (:data:`RUNTIME_UPDATE_CARRIED_FIELDS`: description,
    request headers, lifecycle, filesystems, capacity provider, metadata).
    Read-only fields (status, version, timestamps, ids) are never sent.

    Overrides serve the ``_start_cloud`` reuse path (fresh artifact/role/
    authorizer). ``None`` means "use the details"; an explicit authorizer dict
    (even ``{}``) is authoritative. ``protocol_override`` REPAIRS an
    already-wiped protocol, which round-tripping cannot — it preserves the
    downgrade. No client, no I/O; the ONLY ambient read is the
    :func:`_with_provenance_store` fill on ``env_vars``.
    """
    env_vars = _with_provenance_store(env_vars)
    net_cfg = sanitized_network_config(details)
    kwargs = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": (artifact if artifact is not None
                                 else details.get("agentRuntimeArtifact", {})),
        "roleArn": role_arn if role_arn is not None else details.get("roleArn", ""),
        "networkConfiguration": net_cfg,
        "environmentVariables": env_vars,
        **carried_runtime_settings(details),
    }
    proto = protocol_override or details.get("protocolConfiguration") or {}
    if proto.get("serverProtocol"):
        kwargs["protocolConfiguration"] = dict(proto)
    auth_cfg = (authorizer_config if authorizer_config is not None
                else details.get("authorizerConfiguration") or {})
    if auth_cfg.get("customJWTAuthorizer"):
        kwargs["authorizerConfiguration"] = auth_cfg
    return kwargs
