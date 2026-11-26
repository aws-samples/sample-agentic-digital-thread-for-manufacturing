# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime env contract — the ONE source of truth for AgentCore runtime env.

Every writer of ``create/update-agent-runtime`` ``environmentVariables`` builds
its payload here: deploy.sh (via the ``python3 -m agents.runtime_env_contract``
shim), ``agent_manager._base_env_vars``/``_prepare_runtime_env``, the provisioner
handlers (``agents/provisioner/handlers.py``, run by
``scripts/provision_runtimes.py``), the merge writers
(``runtime_provision_helpers.runtime_update_kwargs`` →
:func:`reconcile_env`), and — later — the workshop runtime-half custom
resource. The API is FULL-REPLACEMENT (ADR-0056: any omitted key is silently
wiped), which is why one spelling matters: a key added to one assembler but
not the others was wiped on the next write (bitten twice, 2026-08-04).

Contract rules (all acceptance-tested):

* **Import-light** — stdlib only. The deploy.sh shim must run on machines
  without the app venv, and the workshop CR CodeBuild image vendors this
  module. Never import boto3/strands/agents.config here.
* **Payload-only** — every function returns a dict; ``os.environ`` is NEVER
  mutated or seeded. ``agents/job_store.py::_get_table`` uses the *presence*
  of ``ONBOARDING_SESSIONS_TABLE`` in the process env as the durable-backend
  opt-in (ADR-0027 local runs have working creds) — the contract may default
  keys only in payloads written onto cloud runtimes.
* **Per-key merge policy** — :data:`REQUIRED_KEYS` maps each required key to
  a policy, because the two live merge semantics differ:
  ``ONBOARDING_SESSIONS_TABLE`` is fill-if-absent-or-empty (an existing
  non-empty value is authoritative), while
  ``SYSTEM_IDENTITY_CLAIM_SECRET_ARN`` is overwrite-when-source-nonempty —
  deploy.sh §2f unconditionally overwrites it whenever the deploy resolved an
  ARN (the ADR-0049 rotation/propagation path); a fill-only policy would
  strand stale signing-secret ARNs on every existing runtime forever.
* **Never touch unknown keys** — :func:`reconcile_env` writes only
  :data:`REQUIRED_KEYS`, plus the one model respelling of
  :data:`SAME_MODEL_RESPELL_KEYS` (the same model moved to the deploy's
  inference-profile id; a different, live-swapped model is never touched);
  ``SYSTEM_REQUIRED_GROUP`` (per-system operator choice, ADR-0049) and
  ``CONNECTION_SECRET_ARN`` pass through verbatim.

Model defaults: :func:`default_bedrock_model` (the orchestrator,
``global.anthropic.claude-sonnet-5-5``) and :func:`default_system_agent_model`
(the per-system agents, ``global.anthropic.claude-haiku-5-5``) are the
deployment defaults. Both are Amazon Bedrock global cross-Region inference
profiles, so the answer is the same in every Region; the ``region`` argument
is kept for callers. They are read by the runtime env built here and the ECS
task (``stacks/_model_allowlist.py`` loads this module for
``stacks/adt_compute_stack.py``); ``cdk.json``'s ``bedrock_model`` context
names the same orchestrator default, pinned by test. The workshop never
reaches these defaults: it pins its own models (``stacks/_workshop_models.py``).
The app's in-process fallback when no model env var is set is the model
catalog (``agents/model_catalog.py``).

Ambient-default semantics follow bash ``${VAR:-default}`` (empty string ⇒
default), matching deploy.sh — the historical ``os.environ.get(k, d)``
(empty-string-wins) spelling is retired with the mirrors.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

ROLES = ("orchestrator", "onboarding", "system")

# CDK-fixed physical name of the onboarding-sessions table
# (stacks/onboarding_stack.py::ONBOARDING_SESSIONS_TABLE_NAME pins
# ``table_name="adt-onboarding-sessions"`` — a literal, never suffixed).
# Canonical home (was runtime_provision_helpers; re-exported there for compat).
ONBOARDING_SESSIONS_TABLE_DEFAULT = "adt-onboarding-sessions"
SYSTEM_GUIDANCE_TABLE_DEFAULT = "adt-system-guidance"

# Workshop runtime-env fragment (docs/workshop-mode.md). Injected
# into the orchestrator/onboarding payloads when Sources.workshop is True;
# NOT sticky (ADR-0030): the flag is passed per invocation, never persisted.
WORKSHOP_ENV = {
    "ADT_WORKSHOP": "1",
    "ADT_MAPPING_OVERRIDES_FILE":
        "/app/agents/demo/fixtures/workshop_mapping_overrides.json",
}

# Workshop fragment for the ORCHESTRATOR payload only (the one role that runs
# chat with AgentCore long-term memory). The workshop shares ONE login, so
# every attendee is the same actorId and actor-wide LTM retrieval injected
# other attendees' chats into a fresh one (answers "from memory", no tool
# calls, no Sources). ``session`` retrieves only this chat's summaries
# (agents/config.py::memory_ltm_scope; writes unchanged). Story-independent;
# NOT sticky (ADR-0030) — rebuilt from Sources.workshop on every build.
WORKSHOP_ORCHESTRATOR_ENV = {
    "MEMORY_LTM_SCOPE": "session",
}

# --- per-key merge-policy registry -------------------------------------------
FILL_IF_ABSENT = "fill-if-absent-or-empty"
OVERWRITE_WHEN_SOURCE_NONEMPTY = "overwrite-when-source-nonempty"

#: The deployment model allowlist (agents/model_catalog.ALLOWED_MODELS_ENV).
#: Written on every role only when set (_allowed_models_env); the refresh
#: paths reconcile, so an existing runtime gets it on the next deploy.
ALLOWED_MODELS_KEY = "ALLOWED_MODELS"

REQUIRED_KEYS: dict[str, dict[str, str]] = {
    # RESPONSE_INTEGRITY_MODE (ADR-0047) rides the SAME overwrite-when-source-
    # nonempty policy as the identity ARN, on EVERY role: the per-system and
    # orchestrator refresh paths RECONCILE rather than rebuild, so without this
    # an opted-in deploy left existing runtimes on the old posture — the app
    # annotating while its agents did not, the exact half-configured state
    # ADR-0047 warns about (observed live: app+orchestrator annotate, system
    # runtimes absent). Empty source ⇒ untouched, so OPT-IN still holds.
    # ALLOWED_MODELS follows the same rule for the same reason.
    "orchestrator": {
        "ONBOARDING_SESSIONS_TABLE": FILL_IF_ABSENT,
        "RESPONSE_INTEGRITY_MODE": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        ALLOWED_MODELS_KEY: OVERWRITE_WHEN_SOURCE_NONEMPTY,
    },
    "onboarding": {
        "ONBOARDING_SESSIONS_TABLE": FILL_IF_ABSENT,
        "RESPONSE_INTEGRITY_MODE": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        # The deployment account (ACCOUNT_KEY_ROLES): the refresh paths
        # reconcile, so an existing runtime gets it on the next deploy.
        "AWS_ACCOUNT_ID": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        ALLOWED_MODELS_KEY: OVERWRITE_WHEN_SOURCE_NONEMPTY,
    },
    "system": {
        "ONBOARDING_SESSIONS_TABLE": FILL_IF_ABSENT,
        # ADR-0049 rotation/propagation: §2f overwrites whenever the deploy
        # resolved an ARN; empty source leaves the existing value untouched.
        "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        "ADT_SYSTEM_GUIDANCE_TABLE": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        "RESPONSE_INTEGRITY_MODE": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        "AWS_ACCOUNT_ID": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        ALLOWED_MODELS_KEY: OVERWRITE_WHEN_SOURCE_NONEMPTY,
    },
}

#: Roles whose runtimes read connection secrets by full ARN (the onboarding
#: tools; a system runtime's CONNECTION_SECRET_ARN at start-up), so they are
#: given the deployment account (``AWS_ACCOUNT_ID``) whenever the writer knows
#: it. Optional at boot: without it the runtime asks STS, as before.
ACCOUNT_KEY_ROLES = frozenset({"onboarding", "system"})

#: Every inference-profile prefix Bedrock uses (a copy of
#: ``model_catalog.PROFILE_PREFIXES``, parity-tested: this module stays
#: stdlib-only and import-light).
PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.", "au.", "jp.", "in.", "ap.")

#: Model keys a reconcile RESPELLS, per role: when the deploy supplies a
#: model (non-empty source) and the runtime runs the SAME model under another
#: inference-profile id (``us.`` where the deploy now pins ``global.``), the
#: runtime takes the deploy's id. A DIFFERENT model on the runtime is a live
#: Admin choice (ADR-0055) and is never touched; an empty source or an absent
#: key changes nothing. Separate from :data:`REQUIRED_KEYS`: these are
#: live-swap keys (:data:`LIVE_SWAP_KEYS`), never required.
SAME_MODEL_RESPELL_KEYS: dict[str, tuple[str, ...]] = {
    "system": ("SYSTEM_AGENT_MODEL",),
}


def _model_family(model_id: str) -> str:
    text = (model_id or "").strip()
    for prefix in PROFILE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text

# --- audit registries and the operator allowlist ------------------------------
# Operator-owned per-system keys: NEVER flagged foreign by the drift lane,
# NEVER written by the contract (ADR-0049 "set NOWHERE here").
OPERATOR_OWNED_KEYS = frozenset({"SYSTEM_REQUIRED_GROUP"})

# Keys that swap LIVE after deploy (PUT /api/admin/llm-settings — ADR-0055;
# scripts/set_query_mode.py sticky-SSM flips; ADR-0049 secret rotation).
# The drift lane must NEVER classify these as stale — model-swapped env is
# NOT stale; they are reported informationally only.
LIVE_SWAP_KEYS = frozenset({
    "BEDROCK_MODEL",
    "SYSTEM_AGENT_MODEL",
    "SYSTEM_QUERY_MODE",
    "SELF_LEARNING_AUTO_ACCEPT",
    "ORCHESTRATOR_DELEGATION_CONCURRENCY",
    "SYSTEM_IDENTITY_CLAIM_SECRET_ARN",
    # ADR-0047 posture: an operator can flip it per deploy, and it is ABSENT
    # entirely on a default (report-only) deployment — never "stale".
    "RESPONSE_INTEGRITY_MODE",
})

# Deterministic, CFN-derived values the drift lane MAY stale-check (and only
# when the audit-time expected value is non-empty). Everything else is either
# live-swap (above), per-system computed, or a constant.
STALE_CHECKED_KEYS: dict[str, frozenset] = {
    "orchestrator": frozenset({
        "GRAPH_IDENTIFIER", "AWS_REGION", "AGENTCORE_GATEWAY_URL",
        "AGENTCORE_GATEWAY_ID", "ADT_APP_SERVICE_CLIENT_ID",
        "ADT_APP_USER_POOL_ID", "ONBOARDING_SESSIONS_TABLE",
    }),
    "onboarding": frozenset({
        "ONBOARDING_SESSIONS_TABLE", "AWS_REGION", "GRAPH_IDENTIFIER",
    }),
    "system": frozenset({
        "ONBOARDING_SESSIONS_TABLE", "ADT_SYSTEM_GUIDANCE_TABLE", "AWS_REGION",
    }),
}

# Per-system keys composed ON TOP of build_env("system") by the provisioning
# callers (runtime_provision_helpers.runtime_connection_env) — known to the
# contract for audit purposes, never built here. ADT_APP_URL,
# QUICK_GATEWAY_TOKEN and QUICK_GATEWAY_TOKEN_REF belong to the QueryBridge
# connection type (the `query-bridge` Quick-integration target, on
# quick_api_enabled deploys only). A bridge carries EITHER the reference (the
# connector path sets one) OR the plaintext token (a --quick-api stack, or an
# older stack, sets none).
QUICK_GATEWAY_TOKEN_REF = "QUICK_GATEWAY_TOKEN_REF"  # nosec B105 - env var NAME, not a credential
COMPOSED_SYSTEM_KEYS = frozenset({
    "CONNECTION_SECRET_ARN", "CONNECTION_CONFIG",
    "ADT_APP_URL", "QUICK_GATEWAY_TOKEN", QUICK_GATEWAY_TOKEN_REF,
})

# QUICK_GATEWAY_TOKEN_REF value format: one full ARN, of a Secrets Manager
# secret or of an SSM parameter. The resource part must carry this prefix.
_TOKEN_REF_RESOURCE_PREFIX = {"secretsmanager": "secret:", "ssm": "parameter/"}


@dataclass(frozen=True)
class QuickGatewayTokenRef:
    """A parsed QUICK_GATEWAY_TOKEN_REF: the store (``secretsmanager`` or
    ``ssm``), the full ARN, and the ARN's Region."""

    service: str
    arn: str
    region: str


def parse_quick_gateway_token_ref(value) -> "QuickGatewayTokenRef | None":
    """Parse a QUICK_GATEWAY_TOKEN_REF value; ``None`` when it is not one.

    Accepted, and nothing else:
    ``arn:<partition>:secretsmanager:<region>:<account>:secret:<name>`` and
    ``arn:<partition>:ssm:<region>:<account>:parameter/<name>``, with a
    Region, a 12-digit account and no whitespace. PURE (stdlib, no I/O): the
    provisioning side checks the value before passing it on, and the bridge
    (agents/quick_gateway_token.py) uses the same rule to pick the store."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or any(ch.isspace() for ch in text):
        return None
    parts = text.split(":", 5)
    if len(parts) != 6:
        return None
    arn_word, partition, service, region, account, resource = parts
    prefix = _TOKEN_REF_RESOURCE_PREFIX.get(service)
    if (arn_word != "arn" or not partition.startswith("aws") or prefix is None
            or not region or len(account) != 12 or not account.isdigit()
            or not resource.startswith(prefix) or len(resource) == len(prefix)):
        return None
    return QuickGatewayTokenRef(service=service, arn=text, region=region)


def default_bedrock_model(region: str) -> str:
    """Orchestrator model default (see the module docstring): the global
    inference profile, the same in every Region. ``region`` is kept for
    callers; it does not change the answer. The workshop never reaches this
    default: it pins its own model (stacks/_workshop_models.py)."""
    return "global.anthropic.claude-sonnet-5-5"


def default_system_agent_model(region: str) -> str:
    """Model for the app's per-system agents (``SYSTEM_AGENT_MODEL`` on the
    ECS task; cheaper than the orchestrator model): the global profile. Kept
    here so every Anthropic model the engine invokes is spelled in ONE place.
    The workshop pins its own (stacks/_workshop_models.py)."""
    return "global.anthropic.claude-haiku-5-5"


def models_to_warm(sources: "RuntimeEnvSources") -> list:
    """Every distinct Bedrock model the engine will invoke, for a
    provisioning-time warm-up.

    Why: the FIRST invocation of an Anthropic model in an AWS account makes
    Bedrock open a Marketplace subscription, and invocations during the
    subscription window (documented as up to ~2 minutes) fail with
    AccessDeniedException. Every Workshop Studio team account is brand new,
    so without a warm-up the attendee's first question races that window.
    The runtime-bootstrap CR invokes each of these once, retrying until the
    subscription is live, so the race is settled during provisioning.

    The configured models win over the defaults, so a deployment that pins
    them (the workshop) warms the models it actually runs."""
    seen: list = []
    for model in (
        sources.bedrock_model or default_bedrock_model(sources.region),
        sources.system_agent_model or default_system_agent_model(sources.region),
    ):
        if model and model not in seen:
            seen.append(model)
    return seen


@dataclass(frozen=True)
class RuntimeEnvSources:
    """Typed inputs for :func:`build_env` / :func:`reconcile_env`.

    Explicit args win; documented ambient-env fallbacks live in ONE place —
    :meth:`from_ambient`. Empty string means "not configured" (build-time
    defaults apply where the legacy blobs had ``${VAR:-default}``)."""

    region: str = "us-east-1"
    graph_id: str = ""
    gateway_url: str = ""
    gateway_id: str = ""
    app_service_client_id: str = ""
    app_user_pool_id: str = ""
    identity_claim_secret_arn: str = ""
    onboarding_sessions_table: str = ""
    system_guidance_table: str = ""
    bedrock_model: str = ""
    system_query_mode: str = "agent"
    self_learning_auto_accept: str = "off"
    # ADR-0047 posture. Empty ⇒ the code default (report-only), so the key is
    # only written when a deployment opts in — the contract never silently
    # turns on gating. `annotate` is the ADR's recommended posture.
    response_integrity_mode: str = ""
    delegation_concurrency: str = "3"
    dataset_ref_bucket: str = ""
    i3x_tls_verify: str = "true"
    workshop: bool = False
    # Span emission (AGENT_SPAN_EMISSION: on with --observability, set on
    # the ECS app by stacks/_observability.py and passed by deploy.sh). Off ⇒ _observability_env turns the exporters off.
    observability: bool = False
    # The deployment's AWS account (AWS_ACCOUNT_ID). Written onto the
    # onboarding and system runtimes so connection_resolver can check a
    # full-ARN secret's account without an STS call; empty ⇒ not written (the
    # runtime then falls back to STS, as before).
    account_id: str = ""
    # The deployment's system-agent model (the app's SYSTEM_AGENT_MODEL).
    # Read only by models_to_warm; empty ⇒ the contract default.
    system_agent_model: str = ""
    # The deployment's model allowlist (the app's ALLOWED_MODELS, comma-
    # separated catalog families). Written onto EVERY role only when set
    # (_allowed_models_env), so the catalog default a runtime falls back to
    # is one the deployment allows; empty ⇒ key absent, as before.
    allowed_models: str = ""

    @classmethod
    def from_ambient(cls, environ=None, **overrides) -> "RuntimeEnvSources":
        """Build Sources from the process env (read-only — never mutated).

        Centralizes every scattered ``os.environ.get`` convention the four
        assemblers used. ``overrides`` win when truthy (explicit-arg-wins);
        bash ``${VAR:-default}`` semantics — empty env value ⇒ default."""
        env = os.environ if environ is None else environ

        def get(key: str, default: str = "") -> str:
            return env.get(key) or default

        values = dict(
            region=get("AWS_REGION", "us-east-1"),
            graph_id=get("GRAPH_IDENTIFIER"),
            gateway_url=get("AGENTCORE_GATEWAY_URL"),
            gateway_id=get("AGENTCORE_GATEWAY_ID"),
            app_service_client_id=get("ADT_APP_SERVICE_CLIENT_ID"),
            app_user_pool_id=get("ADT_APP_USER_POOL_ID"),
            identity_claim_secret_arn=get("SYSTEM_IDENTITY_CLAIM_SECRET_ARN"),
            onboarding_sessions_table=get("ONBOARDING_SESSIONS_TABLE").strip(),
            system_guidance_table=get("ADT_SYSTEM_GUIDANCE_TABLE").strip(),
            bedrock_model=get("BEDROCK_MODEL"),
            system_query_mode=get("SYSTEM_QUERY_MODE", "agent"),
            self_learning_auto_accept=get("SELF_LEARNING_AUTO_ACCEPT", "off"),
            response_integrity_mode=get("RESPONSE_INTEGRITY_MODE"),
            delegation_concurrency=get(
                "ORCHESTRATOR_DELEGATION_CONCURRENCY", "3"),
            dataset_ref_bucket=(get("DATASET_REF_BUCKET")
                                or get("STREAM_CAPTURE_BUCKET")),
            i3x_tls_verify=get("I3X_TLS_VERIFY", "true"),
            workshop=get("ADT_WORKSHOP") in ("1", "true", "True"),
            observability=get("AGENT_SPAN_EMISSION") == "true",
            account_id=get("AWS_ACCOUNT_ID").strip(),
            system_agent_model=get("SYSTEM_AGENT_MODEL").strip(),
            allowed_models=get("ALLOWED_MODELS").strip(),
        )
        for key, val in overrides.items():
            if val not in (None, ""):
                values[key] = val
        return cls(**values)


def _resolved_table(sources: RuntimeEnvSources) -> str:
    """The onboarding-sessions table every payload carries: configured value,
    else the CDK-fixed physical name. Decision: where the
    legacy ONB_ENV blob wrote "" on a failed CFN-output read, the contract
    fills the default — a required key is never written empty."""
    return sources.onboarding_sessions_table or ONBOARDING_SESSIONS_TABLE_DEFAULT


def _resolved_guidance_table(sources: RuntimeEnvSources) -> str:
    """Fixed durable guidance table used by every system runtime."""
    return sources.system_guidance_table or SYSTEM_GUIDANCE_TABLE_DEFAULT


def _orchestrator_env(s: RuntimeEnvSources) -> dict:
    """The 13-key orchestrator payload (key set lifted from deploy.sh §2c's
    legacy ORCH_ENV blob — golden-pinned) + the workshop fragments (shared +
    orchestrator-only)."""
    env = {
        "GRAPH_IDENTIFIER": s.graph_id,
        "AWS_REGION": s.region,
        "BEDROCK_MODEL": s.bedrock_model or default_bedrock_model(s.region),
        "AGENTCORE_GATEWAY_URL": s.gateway_url,
        "AGENTCORE_GATEWAY_ID": s.gateway_id,
        "ADT_APP_SERVICE_CLIENT_ID": s.app_service_client_id,
        "ADT_APP_USER_POOL_ID": s.app_user_pool_id,
        "SYSTEM_QUERY_MODE": s.system_query_mode,
        "SELF_LEARNING_AUTO_ACCEPT": s.self_learning_auto_accept,
        "ORCHESTRATOR_DELEGATION_CONCURRENCY": s.delegation_concurrency,
        "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": s.identity_claim_secret_arn,
        "ONBOARDING_SESSIONS_TABLE": _resolved_table(s),
        **_observability_env(s),
    }
    if s.workshop:
        env.update(WORKSHOP_ENV)
        env.update(WORKSHOP_ORCHESTRATOR_ENV)
    return env


def _onboarding_env(s: RuntimeEnvSources) -> dict:
    """The 6-key onboarding payload: deploy.sh §2e's legacy 5-key ONB_ENV
    blob less the Step Functions provisioner ARN (that provisioner had no
    caller and was removed 2026-10-29), plus ``GRAPH_IDENTIFIER``,
    ``BEDROCK_MODEL`` and the workshop fragment.

    ``GRAPH_IDENTIFIER`` joined 2026-09-11 (ADR-0069): the onboarding runtime
    carries the same ``ONBOARD_TOOLS`` code as the in-process wizard path and
    executes the ones it exports (``onboarding_runtime_server.RUNTIME_TOOLS``;
    the app runs get_catalog, provision_system, reset_system,
    connect_new_system and store_credentials itself, ADR-0068 amendment). The
    exported tools read the knowledge graph (``discover``,
    ``resolve_identities``, ``propose_concepts``). Without the key,
    ``db._neptune_execute`` falls back to the literal ``"adt"``, which Neptune
    Analytics uses as the endpoint HOSTNAME — a non-existent host — so every
    graph read returned ``[]`` and the agent told stewards the catalog was
    empty. Fail-fast pairing: ``missing_required``.

    ``BEDROCK_MODEL`` joined 2026-10-01: the same tools call Bedrock
    (``db.bedrock_call``'s primary model and the map-batch escalation in
    ``onboard._escalation_model``). Without the key they fell back to model
    ids hardcoded in code, so a deployment that pins its model (the workshop
    package's Sonnet 5) still ran another one in onboarding. Unpinned
    deployments get the same region-keyed default as the orchestrator."""
    env = {
        "SYSTEM_ID": "onboarding",
        "ONBOARDING_SESSIONS_TABLE": _resolved_table(s),
        "AWS_REGION": s.region,
        "GRAPH_IDENTIFIER": s.graph_id,
        "BEDROCK_MODEL": s.bedrock_model or default_bedrock_model(s.region),
        **_observability_env(s),
    }
    if s.workshop:
        env.update(WORKSHOP_ENV)
    return env


def _observability_env(s: RuntimeEnvSources) -> dict:
    """Span emission keys, following the --observability deploy option.

    On: ``AGENT_OBSERVABILITY_ENABLED=true`` makes the AWS OpenTelemetry
    distro export to the X-Ray and CloudWatch Logs OTLP endpoints. Off: no
    span destination is set up, so those exports fail every batch, and the
    plain OpenTelemetry defaults would send to localhost instead. Each
    exporter is set to ``none``."""
    if s.observability:
        return {"AGENT_OBSERVABILITY_ENABLED": "true"}
    return {"AGENT_OBSERVABILITY_ENABLED": "false", "OTEL_TRACES_EXPORTER": "none",
            "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"}


def _system_env(s: RuntimeEnvSources, *, system_id: str, system_name: str,
                schema: str, connection_type: str,
                system_agent_model: str) -> dict:
    """The connection-agnostic system-agent payload — the UNIFIED full set
    (the provisioner-handler path was starved of GRAPH_IDENTIFIER,
    I3X_TLS_VERIFY, SYSTEM_IDENTITY_CLAIM_SECRET_ARN and DATASET_REF_BUCKET;
    both in-app and provisioner-handler builders now delegate here). Connection-specific env
    (``runtime_connection_env``) composes on top. SYSTEM_PROMPT / TABLES_DESC
    stay out (4000-byte env cap); SYSTEM_REQUIRED_GROUP is a per-system
    operator choice — deliberately set NOWHERE here (ADR-0049)."""
    return {
        "SYSTEM_ID": system_id,
        "SYSTEM_NAME": system_name[:100],
        "SCHEMA": schema[:100],
        "CONNECTION_TYPE": connection_type,
        "AWS_REGION": s.region,
        "SYSTEM_QUERY_MODE": s.system_query_mode,
        "SYSTEM_AGENT_MODEL": system_agent_model,
        "BEDROCK_MODEL": s.bedrock_model or default_bedrock_model(s.region),
        **_observability_env(s),
        "GRAPH_IDENTIFIER": s.graph_id,
        "I3X_TLS_VERIFY": s.i3x_tls_verify,
        "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": s.identity_claim_secret_arn,
        "DATASET_REF_BUCKET": s.dataset_ref_bucket,
        "ONBOARDING_SESSIONS_TABLE": _resolved_table(s),
        "ADT_SYSTEM_GUIDANCE_TABLE": _resolved_guidance_table(s),
    }


def build_env(role: str, sources: RuntimeEnvSources, **system_kwargs) -> dict:
    """The COMPLETE ``environmentVariables`` payload for *role*.

    Full-replacement-API-shaped: emits every key the role's runtime needs.
    ``system`` requires the per-system kwargs of :func:`_system_env`."""
    if role == "orchestrator":
        env = _orchestrator_env(sources)
    elif role == "onboarding":
        env = _onboarding_env(sources)
    elif role == "system":
        env = _system_env(sources, **system_kwargs)
    else:
        raise ValueError(
            f"unknown runtime env role {role!r}; expected one of {ROLES}")
    env.update(_integrity_mode_env(sources))
    env.update(_allowed_models_env(sources))
    if role in ACCOUNT_KEY_ROLES and sources.account_id.strip():
        env["AWS_ACCOUNT_ID"] = sources.account_id.strip()
    return env


def _allowed_models_env(s: RuntimeEnvSources) -> dict:
    """``ALLOWED_MODELS`` — written ONLY when the deployment set an allowlist.

    The runtimes fall back to the catalog default model when a model env var
    is unset (the onboarding map step, the orchestrator's wasted-step
    analysis), and the catalog default is allowlist-aware. Without the key a
    runtime would fall back to a model the deployment does not allow: the
    workshop allows Sonnet 5 and Haiku 4.5 while the catalog defaults are
    newer. Same rule on every role, like the integrity posture. Empty ⇒ key
    ABSENT, so a deployment without an allowlist sees no change.
    """
    allowed = (s.allowed_models or "").strip()
    return {ALLOWED_MODELS_KEY: allowed} if allowed else {}


def _integrity_mode_env(s: RuntimeEnvSources) -> dict:
    """``RESPONSE_INTEGRITY_MODE`` — written ONLY when a deployment set it.

    ADR-0047 keeps enforcement OPT-IN with the code default (report-only) as
    the single source of truth, so writing this key unconditionally would move
    that decision into the contract. Empty ⇒ key ABSENT ⇒ the code default
    applies, and an existing deployment sees no change.

    Applied for EVERY role in one place because ADR-0047 requires the flag to
    be set consistently on the app AND every runtime — a half-configured
    posture (app annotating, agents not) is the failure mode to avoid.
    """
    mode = (s.response_integrity_mode or "").strip().lower()
    return {"RESPONSE_INTEGRITY_MODE": mode} if mode else {}


def _required_source_value(key: str, sources: RuntimeEnvSources) -> str:
    """The reconcile source value for a REQUIRED key."""
    if key == "ONBOARDING_SESSIONS_TABLE":
        return _resolved_table(sources)
    if key == "SYSTEM_IDENTITY_CLAIM_SECRET_ARN":
        return sources.identity_claim_secret_arn
    if key == "ADT_SYSTEM_GUIDANCE_TABLE":
        return sources.system_guidance_table
    if key == "RESPONSE_INTEGRITY_MODE":
        return (sources.response_integrity_mode or "").strip().lower()
    if key == "AWS_ACCOUNT_ID":
        return sources.account_id.strip()
    if key == ALLOWED_MODELS_KEY:
        return (sources.allowed_models or "").strip()
    raise KeyError(key)


def _respelled_model_keys(role: str, env: dict, sources: RuntimeEnvSources) -> dict:
    """*env* with each :data:`SAME_MODEL_RESPELL_KEYS` model moved to the
    deploy's inference-profile id when it is the same model under another id."""
    source_of = {"SYSTEM_AGENT_MODEL": sources.system_agent_model}
    for key in SAME_MODEL_RESPELL_KEYS.get(role, ()):
        source = (source_of.get(key) or "").strip()
        current = (env.get(key) or "").strip()
        if (source and current and current != source
                and _model_family(current) == _model_family(source)):
            env[key] = source
    return env


def reconcile_env(role: str, existing: dict | None,
                  sources: RuntimeEnvSources, *,
                  span_emission: bool | None = None) -> dict:
    """Copy of *existing* with the role's :data:`REQUIRED_KEYS` applied per
    policy, and its :data:`SAME_MODEL_RESPELL_KEYS` respelled. NEVER writes,
    overwrites, or deletes any other key — operator keys
    (``SYSTEM_REQUIRED_GROUP``) and per-system keys pass through verbatim.
    Generalizes ``runtime_provision_helpers._with_provenance_store``.

    *span_emission* (the deploy's span option, when the caller knows it)
    also replaces the span keys with :func:`_observability_env`'s, so an
    existing runtime follows the option the way a new one does. ``None``
    leaves them as they are."""
    if role not in REQUIRED_KEYS:
        raise ValueError(
            f"unknown runtime env role {role!r}; expected one of {ROLES}")
    env = dict(existing or {})
    for key in sorted(REQUIRED_KEYS[role]):
        policy = REQUIRED_KEYS[role][key]
        value = _required_source_value(key, sources)
        if policy == FILL_IF_ABSENT:
            if not (env.get(key) or "").strip():
                env[key] = value
        elif policy == OVERWRITE_WHEN_SOURCE_NONEMPTY and (value or "").strip():
            env[key] = value
    if span_emission is not None:
        for key in _observability_env(RuntimeEnvSources(observability=False)):
            env.pop(key, None)
        env.update(_observability_env(RuntimeEnvSources(observability=span_emission)))
    return _respelled_model_keys(role, env, sources)


def known_keys(role: str) -> frozenset:
    """Every env key the contract knows for *role* (build set + workshop frag
    + composed per-system keys + operator-owned keys). The drift lane flags
    anything else as *foreign* — informational only, never gating."""
    dummy = {}
    if role == "system":
        dummy = dict(system_id="x", system_name="x", schema="x",
                     connection_type="x", system_agent_model="x")
    keys = set(build_env(role, RuntimeEnvSources(workshop=True), **dummy))
    keys.update(WORKSHOP_ENV)
    keys.add(ALLOWED_MODELS_KEY)
    if role in ACCOUNT_KEY_ROLES:
        keys.add("AWS_ACCOUNT_ID")
    if role == "system":
        keys |= COMPOSED_SYSTEM_KEYS
    return frozenset(keys | OPERATOR_OWNED_KEYS)


def missing_required(role: str, environ) -> list:
    """Required-but-missing/empty env keys for *role* — the ONE boot-time
    validator (ADR-0069). PURE: reads only the *environ* Mapping handed in
    (never ``os.environ`` implicitly, never mutates anything).

    The required set IS :data:`STALE_CHECKED_KEYS` — the deterministic,
    CFN-derived values every cloud deployment provisions. Nothing in there is
    optional in cloud: they are core-stack outputs present on every deploy
    mode. Live-swap keys
    (``LIVE_SWAP_KEYS``) and per-system composed keys are deliberately NOT
    required here — they are legitimately absent/empty on some deployments.
    Runtime entry points call this in cloud mode and RAISE naming the keys,
    so a half-provisioned runtime fails CLOSED at boot instead of degrading
    into false "the catalog is empty" answers (the ``neptune_query``
    return-``[]``-on-failure hiding path). Local/DEV_MODE processes never
    call it — their env is intentionally minimal (ADR-0023).
    """
    if role not in STALE_CHECKED_KEYS:
        raise ValueError(
            f"unknown runtime env role {role!r}; expected one of {ROLES}")
    return sorted(key for key in STALE_CHECKED_KEYS[role]
                  if not (environ.get(key) or "").strip())


def _main(argv=None) -> int:
    """deploy.sh shim. Contract: stdout carries EXACTLY one
    JSON object, written once after successful serialization; diagnostics go
    to stderr; ANY failure exits nonzero with NOTHING on stdout."""
    parser = argparse.ArgumentParser(prog="python3 -m agents.runtime_env_contract")
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--reconcile", action="store_true",
                        help="read existing env JSON on stdin; apply the "
                             "role's REQUIRED_KEYS merge policies only")
    args = parser.parse_args(argv)
    sources = RuntimeEnvSources.from_ambient(
        system_guidance_table=(SYSTEM_GUIDANCE_TABLE_DEFAULT
                               if args.reconcile and args.role == "system" else ""))
    if args.reconcile:
        existing = json.loads(sys.stdin.read().strip() or "{}")
        if not isinstance(existing, dict):
            raise ValueError("--reconcile stdin must be a JSON object")
        # deploy.sh [2f] passes AGENT_SPAN_EMISSION; absent ⇒ span keys kept.
        span = os.environ.get("AGENT_SPAN_EMISSION")
        env = reconcile_env(args.role, existing, sources,
                            span_emission=None if span is None else span == "true")
    elif args.role == "system":
        raise ValueError(
            "--role system builds require per-system kwargs; the CLI supports "
            "only --reconcile for the system role (builds go through the "
            "in-app provisioners)")
    else:
        env = build_env(args.role, sources)
    sys.stdout.write(json.dumps(env))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except Exception as exc:  # noqa: BLE001 - shim boundary: everything → stderr + exit 1
        print(f"runtime_env_contract: {exc}", file=sys.stderr)
        sys.exit(1)
