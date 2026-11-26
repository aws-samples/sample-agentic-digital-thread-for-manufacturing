# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared foundation for dynamic, onboarding-driven connection management.

Public ENTRYPOINT for connection management. Owns connection resolution (Neptune
``System`` node config + secret ARN — NODE-ONLY, no catalog/env fallback), the
``System``-node write path, the short-TTL Secrets Manager cache, SQL connect-param
reassembly, the masked read-only "current connection" summary, and secret-free
audit logging. The SSRF/scheme guard + canonical masking live in
``connection_security`` and the connectivity probes live in ``connection_probes``;
both are RE-EXPORTED here (see the explicit re-export block below) so existing
import sites — ``from .connection_resolver import validate_connection_target /
mask_connection / probe_connection / ...`` and ``connection_resolver.<name>``
attribute access — keep working unchanged after the file split.

As of the Step-3 cut-over (ADR 0031, *Step 3 final decisions*) the ``System``
node is the SOLE runtime source of a system's connection. ``resolve_connection``
NEVER reads the catalog connection snapshot: an unregistered system (no node
``connection_config``) or a graph-read failure yields an ``_unconfigured`` result
rather than a catalog default, and the read path NEVER raises. The MCP probe is
the simple ``httpx`` POST from ``onboard_agent.test_connection`` (avoids
``asyncio.run`` hangs). See ADR 0031.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Re-exports — the file split (RW6 / Task 6.1) moved the SSRF/masking guard into
# ``connection_security`` and the probes into ``connection_probes``. They are
# imported here (and listed in ``__all__``) so EVERY existing consumer import
# keeps resolving against ``connection_resolver`` with no edit — both
# ``from .connection_resolver import <name>`` and ``cr.<name>`` attribute access
# (incl. the underscore-prefixed internals the unit tests monkeypatch/mutate).
# --------------------------------------------------------------------------- #
from .connection_security import (  # noqa: F401  (re-exported)
    SCHEME_ALLOWLIST,
    ConnectionTargetError,
    _DENIED_IPS,
    _DENIED_NETWORKS,
    _LINK_LOCAL_V4,
    _LINK_LOCAL_V6,
    _METADATA_HOSTS,
    _REDACT_KEYS,
    _REDACT_QUERY_PARAMS,
    _RFC1918_NETWORKS,
    _URL_KEYS,
    _embedded_ipv4s,
    _extract_host_port,
    _is_denied_address,
    _mask_url,
    _reject_if_denied,
    _safe_detail,
    _strip_url_credentials,
    mask_connection,
    resolve_and_validate,
    validate_connection_target,
)
from .connection_probes import (  # noqa: F401  (re-exported)
    _PROBES,
    _REASON_DETAIL,
    CredentialUnresolvedError,
    _classify_exception,
    _health_url,
    _mcp_auth_headers,
    _normalize_type,
    _pin_host_to_ip,
    _probe_mcp,
    _probe_postgres,
    _probe_rest,
    _probe_sitewise,
    _result,
    probe_connection,
)

logger = logging.getLogger(__name__)

# The Tulip connection-type string (``connection_types`` is stdlib-only, so a
# top-level import adds no cycle). The one place a resolver branch keys on Tulip:
# deriving its HTTP Basic token from the seat secret's api_key/api_secret.
from .connection_types import TULIP as _TULIP_CONNECTION_TYPE  # noqa: E402

# Non-secret connection fields persisted onto the Neptune ``System`` node. Secret
# material (token/password/external_id/etc.) is NEVER written here — it lives only
# in Secrets Manager and is referenced by ``secret_arn``.
#
# The tuple is grouped by connector family so adding a key is a one-line edit:
#   * relational / URL targets (PostgreSQL, REST, MCP, SiteWise-i3X);
#   * ``connection_string`` — SiteWise-Direct's service endpoint (a non-credential
#     URL; still passed through the at-rest credential strip below for safety);
#   * typed Athena target (``workgroup``/``database``/``catalog``/
#     ``output_location``/``region``) — a boto3 workgroup/catalog target, not a DSN;
#   * cross-account ``role_arn`` — an IAM role ARN (an identifier, NOT a secret; the
#     paired ``external_id`` IS a secret and is routed through ``secret=`` instead);
#   * admin-tunable Athena reliability knobs (``max_scanned_bytes``, the two
#     poll bounds ``poll_timeout_seconds`` / ``introspection_timeout_seconds``,
#     and the exact-total split caps ``exact_split_max_pieces`` /
#     ``exact_split_max_total_bytes`` / ``exact_split_max_seconds`` /
#     ``exact_split_max_groups``) —
#     each is the TOP tier of its own config > env > default precedence
#     (athena_tools.resolve_scan_budget / resolve_poll_timeout /
#     resolve_introspection_timeout), so without persistence the per-system tier
#     would silently evaporate on node persist and every system would fall back
#     to the env/default tier after a task recycle.
# Without the typed + cross-account keys an onboarded Athena / cross-account system
# would lose its connection on node persist and NOT survive an ECS task recycle.
#: The admin-tunable Athena reliability knobs (see the note above), named once:
#: the node keeps them, and a same-target connection resync keeps the ones a
#: system's agent-config holds that its node does not
#: (``agent_manager.AgentManager._carried_keys``).
ATHENA_ADMIN_CONFIG_KEYS = ("max_scanned_bytes",
                            "poll_timeout_seconds", "introspection_timeout_seconds",
                            "exact_split_max_pieces", "exact_split_max_total_bytes",
                            "exact_split_max_seconds", "exact_split_max_groups")
#: The admin knobs an admin route sets AND clears on the System node
#: (:func:`write_scan_budget`). The node is their only source, so a resync takes
#: them from the resolved connection alone: a value left in an older
#: agent-config must not bring back a budget the route cleared.
NODE_MANAGED_ADMIN_KEYS = ("max_scanned_bytes",)
_NODE_CONFIG_KEYS = ("url", "host", "port", "scheme", "base_url", "dbname",
                     "schema", "search_path", "auth_scheme", "health_path",
                     "connection_string",
                     "workgroup", "database", "catalog", "output_location", "region",
                     "role_arn", *ATHENA_ADMIN_CONFIG_KEYS,
                     "workspace_id",   # Tulip instance-wide-account workspace scope (the /api/v3/w/{id}/… path variable; non-secret)
                     "table_label_prefix",  # Tulip table scope: only tables whose label starts with it (non-secret; optional)
                     # S3 Tables target + scope + tuning (ADR-0099; all
                     # non-secret, the connector is secretless). table_bucket_arn is
                     # REQUIRED for restore: without it the agent cannot rebuild after
                     # a recycle and tool construction fails eagerly (review DDB-04).
                     "table_bucket_arn", "namespaces", "tables", "catalog_endpoint",
                     "duckdb_memory_limit", "duckdb_threads", "query_timeout_s", "max_rows",
                     "credential_source")  # the connection's credential SOURCE marker (SERVER-set; "secret_arn" = a named Secrets Manager secret, pinned; absent/"inline" = a pasted key)

# Non-secret ``connection_config`` keys that may be SERVED to the catalog/UI
# (the system-card typed-connector summary: 'workgroup · database · region' /
# 'region · rolename'). STRICT ALLOWLIST — anything not listed is dropped by
# :func:`sanitize_connection_config`, which is how ``external_id`` / ``token`` /
# ``password`` / ``secret*`` / ``credential*`` / ``api_key`` can never reach a
# catalog entry or a ``GET /api/onboard/catalog`` response. ``role_arn`` is an
# identifier, NOT a secret (the paired ``external_id`` IS — see _NODE_CONFIG_KEYS
# note above). A subset of ``_NODE_CONFIG_KEYS`` on purpose: display-relevant
# target fields only.
CATALOG_SAFE_CONFIG_KEYS = frozenset({
    "workgroup", "database", "catalog", "output_location", "region",
    "role_arn", "base_url", "url", "host", "port", "dbname",
    "workspace_id",  # Tulip instance-wide-account workspace scope (non-secret; /api/v3/w/<id>/)
    "table_label_prefix",  # Tulip table scope (non-secret; shown on the system card)
    # S3 Tables: the bucket target and scope are display-relevant and
    # non-secret (ADR-0099). table_bucket_arn must round-trip for restore (DDB-04).
    "table_bucket_arn", "namespaces", "tables", "catalog_endpoint",
    "credential_source",  # the credential-source marker (non-secret; "secret_arn" when the key comes from a named secret)
})


def sanitize_connection_config(cfg) -> dict:
    """Reduce a connection config to its NON-SECRET, catalog/UI-safe subset.

    Pure (no IO). ALLOWLIST approach — only :data:`CATALOG_SAFE_CONFIG_KEYS`
    survive; every other key (explicitly: ``external_id``, ``token``,
    ``password``, ``secret*``, ``credential*``, ``api_key``) is DROPPED, so new
    secret-shaped keys are safe by default. Tolerates a JSON-string blob (the
    node-persisted form) and any non-dict input (→ ``{}``). ``url``/``base_url``
    values additionally have inline ``user:pass@`` credentials stripped
    (defense-in-depth, same primitive as :func:`write_connection`).
    """
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            return {}
    if not isinstance(cfg, dict):
        return {}
    out: dict = {}
    for k, v in cfg.items():
        if k not in CATALOG_SAFE_CONFIG_KEYS or v in (None, ""):
            continue
        if k in ("url", "base_url") and isinstance(v, str):
            v = _strip_url_credentials(v)
        out[k] = v
    return out
# Secret cache TTL (seconds). Short by design — see ADR 0031.
_TTL = int(os.environ.get("CONN_SECRET_TTL_SEC", "45"))
# arn -> (value: dict, expiry_monotonic: float)
_secret_cache: dict[str, tuple[dict, float]] = {}

# Public API surface (for ``from .connection_resolver import *`` and discoverability).
# Underscore-prefixed internals are intentionally NOT listed but ARE importable as
# ``cr.<name>`` (the unit tests monkeypatch several of them).
__all__ = [
    # security (re-exported)
    "SCHEME_ALLOWLIST", "ConnectionTargetError", "mask_connection",
    "resolve_and_validate", "validate_connection_target",
    # probes (re-exported)
    "CredentialUnresolvedError", "probe_connection",
    # resolver (defined here)
    "resolve_connection", "write_connection", "describe_connection",
    "publish_secret_pointer", "node_connection_checked", "ConnectionChangedError",
    "pg_conn_params", "db_secret_arn_for_schema", "invalidate_secret",
    "read_secret_uncached",
    "audit_connection_event",
    "sanitize_connection_config", "CATALOG_SAFE_CONFIG_KEYS",
    # Connect New System registrations (pre-provision connection)
    "CONNECTION_ORIGIN_KEY", "ORIGIN_USER", "ORIGIN_DEMO", "PG_DSN_CONFIG_KEY",
    "is_user_registration", "is_own_connection_secret", "is_bound_user_secret",
    "is_bound_secret", "own_registration_pointer",
    "normalize_endpoint_url", "endpoint_identity", "same_endpoint",
    "parse_pg_registration_dsn", "registration_connection",
    "registered_pg_dsn", "pg_params_from_dsn",
]


def db_secret_arn_for_schema(schema: str) -> str:
    """Resolve the SHARED demo DB-secret ARN backing a SQL schema, SERVER-SIDE.

    Used by ``onboard`` (provision-confirm node write) to populate a SQL node's
    ``secret_arn`` SERVER-SIDE (never from a request body — ADR-0031 guardrail).
    The schema→DB routing behind it (which schemas live in the ERP Aurora
    database vs the demo seed RDS, and which shared secret backs each) is DEMO
    knowledge and lives in the ONE sanctioned demo module: this function keeps
    its public name/signature but DELEGATES to
    :func:`agents.demo.db_routing.demo_db_secret_arn` (ADR-0044 Decision 2 —
    env first, then the ``/adt/demo/*`` SSM pointer's
    ``secret_arn``). Customer systems never route through here: they get a
    dedicated ``conn/<id>`` secret via :func:`write_connection`. Returns ``""``
    when no ARN can be derived (fail-soft) — only ever an ARN, never a secret
    VALUE.
    """
    from .demo.db_routing import demo_db_secret_arn  # lazy: one-way demo seam
    return demo_db_secret_arn(schema)


def _monotonic() -> float:
    """Indirection over the clock so tests can monkeypatch TTL behavior."""
    return time.monotonic()


def _secrets_client():
    import boto3
    return boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _parse_secret(raw: str) -> dict:
    """Parse a Secrets Manager string: JSON objects as-is; a bare string (e.g. the
    HighByte MCP token) wrapped as ``{"token": <raw>}`` for merging."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"token": raw}
    return data if isinstance(data, dict) else {"token": str(data)}


def _fetch_secret(secret_arn: str) -> dict:
    """Fetch a secret by ARN with a short-TTL in-process cache. A full ARN in
    another AWS account than the deployment's is refused
    (:func:`refuse_foreign_account_secret`)."""
    if not secret_arn:
        return {}
    refuse_foreign_account_secret(secret_arn)
    now = _monotonic()
    cached = _secret_cache.get(secret_arn)
    if cached is not None and cached[1] > now:
        return cached[0]
    resp = _secrets_client().get_secret_value(SecretId=secret_arn)
    value = _parse_secret(resp.get("SecretString", ""))
    _secret_cache[secret_arn] = (value, now + _TTL)
    return value


class ForeignAccountSecretError(RuntimeError):
    """A secret ARN names another AWS account than the deployment's. Raised
    before any Secrets Manager call; callers treat it as an unreadable secret
    (fail closed). The message carries the account ids only."""


class AccountUnknownSecretError(ForeignAccountSecretError):
    """The deployment's own account could not be determined, so a full-ARN
    secret's account cannot be checked and the secret is not read (fail
    closed, retryable: the lookup is retried after
    :data:`_ACCOUNT_RETRY_SECONDS`)."""


#: The deployment's account id, learned once (``AWS_ACCOUNT_ID`` env, set by
#: the CDK on the app task; else STS GetCallerIdentity, which needs no IAM
#: permission). A failed lookup is retried after :data:`_ACCOUNT_RETRY_SECONDS`;
#: until then every full-ARN secret read is refused.
_ACCOUNT_CACHE: dict = {"value": None, "until": 0.0}
_ACCOUNT_RETRY_SECONDS = 10.0
_ACCOUNT_ID = re.compile(r"\d{12}")


def _sts_account_id() -> str:
    """STS GetCallerIdentity → the caller's account id (short timeouts, one
    attempt). The offline unit lane replaces this seam."""
    import boto3
    from botocore.config import Config
    client = boto3.client("sts", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                          config=Config(connect_timeout=2, read_timeout=2,
                                        retries={"max_attempts": 1}))
    return str(client.get_caller_identity().get("Account") or "")


def deployment_account_id() -> str:
    """The AWS account this deployment runs in, or ``""`` when it cannot be
    learned (then every full-ARN secret read is refused,
    :func:`refuse_foreign_account_secret`)."""
    env = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    if _ACCOUNT_ID.fullmatch(env):
        return env
    now = _monotonic()
    value = _ACCOUNT_CACHE["value"]
    if value is not None and (value or now < _ACCOUNT_CACHE["until"]):
        return value
    try:
        found = _sts_account_id()
    except Exception as exc:  # noqa: BLE001 - unknown account: full-ARN reads refused
        logger.warning("deployment_account_id: STS lookup failed: %s", type(exc).__name__)
        found = ""
    value = found if _ACCOUNT_ID.fullmatch(found) else ""
    _ACCOUNT_CACHE.update(value=value, until=now + _ACCOUNT_RETRY_SECONDS)
    return value


def refuse_foreign_account_secret(secret_id) -> None:
    """Raise :class:`ForeignAccountSecretError` when ``secret_id`` is a full
    Secrets Manager ARN whose account is not the deployment's, and
    :class:`AccountUnknownSecretError` when the deployment's account cannot be
    determined (the ARN's account cannot be checked, so it is not read; a
    retry follows the next lookup). A bare name resolves in the caller's own
    account and is not compared. Every secret this app reads is in its own
    account (registration secrets are written by the app; the demo database
    secrets are its own stacks'); cross-account sources use a role, never a
    foreign secret."""
    parts = str(secret_id or "").split(":", 6)
    if len(parts) != 7 or parts[0] != "arn" or parts[2] != "secretsmanager":
        return
    own = deployment_account_id()
    if not own:
        raise AccountUnknownSecretError(
            "this deployment's AWS account could not be determined, so the secret was not "
            "read; retry in a moment")
    if parts[4] != own:
        raise ForeignAccountSecretError(
            f"secret is in account {parts[4]}, not this deployment's account {own}")


def invalidate_secret(secret_arn: str) -> None:
    """Drop a cached secret so the next resolve re-fetches it (post-update)."""
    _secret_cache.pop(secret_arn, None)


def read_secret_uncached(secret_id: str) -> dict:
    """Read a secret by ARN or name, bypassing the TTL cache.

    For read-modify-write (an Edit merging a partial credential update): the
    cache is keyed by the exact id string and a write invalidates only the ARN
    it returns, so a cached read could hand back values a moment-earlier write
    already replaced. Raises on any Secrets Manager error, and on a full ARN in
    another account (:func:`refuse_foreign_account_secret`).
    """
    refuse_foreign_account_secret(secret_id)
    resp = _secrets_client().get_secret_value(SecretId=secret_id)
    return _parse_secret(resp.get("SecretString", ""))


#: The AWSCURRENT version id the current operation wrote to each system's own
#: secret (``adt/conn/<id>``). :func:`_store_connection_secret` records it;
#: :func:`snapshot_durable_record` clears a system's entry at the start of a
#: re-provision attempt and :func:`restore_durable_record` reads it so a
#: rollback mutates the secret only when AWSCURRENT still equals the version
#: THIS attempt wrote (CAS), never a version a newer lease owner staged
#: afterwards. Single-process, same-thread provision flow.
_LAST_WRITTEN_SECRET_VERSION: dict[str, str] = {}


def _store_connection_secret(system_id: str, secret: dict) -> str:
    """Write a connection secret to Secrets Manager at ``adt/conn/<id>``.

    Mirrors the existing ``onboard_agent._store_secret`` / admin
    ``update_highbyte_token`` create-or-``put_secret_value`` pattern: create on first
    write, fall back to ``put_secret_value`` when the secret already exists. Returns
    the secret ARN. The AWSCURRENT version id of the write is recorded in
    :data:`_LAST_WRITTEN_SECRET_VERSION` so a failed re-provision's rollback can
    prove it still owns the version before touching it. Secret
    VALUES are never logged.
    """
    client = _secrets_client()
    secret_name = f"adt/conn/{system_id}"
    payload = json.dumps(secret)
    try:
        resp = client.create_secret(Name=secret_name, SecretString=payload)
        return _record_written_version(system_id, resp)
    except client.exceptions.ResourceExistsException:
        resp = client.put_secret_value(SecretId=secret_name, SecretString=payload)
        return _record_written_version(system_id, resp)
    except client.exceptions.InvalidRequestException as exc:
        # The name's previous secret is scheduled for deletion (a full removal
        # of a user registration with this id, delete_own_secret_before_removal):
        # cancel that and write this payload as its current value, so the id
        # can be registered again inside the recovery window.
        if "deletion" not in str(exc).lower():
            raise
        client.restore_secret(SecretId=secret_name)
        resp = client.put_secret_value(SecretId=secret_name, SecretString=payload)
        logger.info("%s was scheduled for deletion; _store_connection_secret restored "
                    "and overwrote it", secret_name)
        return _record_written_version(system_id, resp)


def _record_written_version(system_id: str, resp: dict) -> str:
    """Record the AWSCURRENT version id a secret write just produced (keyed by
    system) and return the write's ARN. The version id lets a rollback CAS-check
    it still owns what this attempt wrote."""
    version = resp.get("VersionId")
    if version:
        _LAST_WRITTEN_SECRET_VERSION[system_id] = version
    return resp["ARN"]


def resolve_registered_token(system_id: str, inline_token: str = "",  # nosec B107 - empty default sentinel; real value supplied by caller
                             inline_scheme: str = "", *, entry: dict | None = None) -> tuple:
    """Token + auth scheme for a URL/token connector (i3X / REST / MCP),
    valid BEFORE and AFTER the system's graph node exists.

    The i3X sibling of ``resolve_evidence._with_cross_account_credentials``
    (same precedence, DRY on the same primitives):

      1. An INLINE token (request body / direct caller input) wins.
      2. The node-based :func:`resolve_connection` (node config + the secret
         its ``secret_arn`` points at) — the live source for PROVISIONED
         systems; the node is server-written and deleted on offboard.
      3. The REGISTRATION-time secret, reached ONLY through the catalog
         ``entry``'s own pointer (exactly ``adt/conn/<system_id>``, written by
         this registration) whose payload is bound to this system and its
         origin (:func:`is_bound_secret`) — a freshly registered system has no
         graph node yet. Never ``adt/conn/<system_id>`` by NAME: that secret
         outlives an offboard, so a by-name read let a new registration reusing
         the id inherit the removed system's token.

    Returns ``(token, auth_scheme)`` — ``("", "bearer")`` when nothing is
    configured. Fail-soft: resolution errors are logged (never values) and
    fall through to the next source; this never raises.
    """
    if inline_token:
        return inline_token, (inline_scheme or "bearer")
    try:
        resolved = resolve_connection(system_id) or {}
    except Exception as exc:  # noqa: BLE001 - fail-soft; node source is optional here
        logger.info("%s: node resolve skipped (%s) while resolving the registered token",  # logs the exception type only, never a value
                    system_id, type(exc).__name__)
        resolved = {}
    if resolved.get("token"):
        return resolved["token"], (resolved.get("auth_scheme")
                                   or inline_scheme or "bearer")
    secret = _entry_bound_secret(system_id, entry)
    if secret.get("token"):
        return secret["token"], (secret.get("auth_scheme")
                                 or inline_scheme or "bearer")
    return "", (inline_scheme or "bearer")


def _entry_bound_secret(system_id: str, entry: dict | None) -> dict:
    """The registration secret the catalog ``entry`` points at, when that
    pointer is exactly this system's own ``adt/conn/<id>`` and the payload is
    bound to this system and the entry's origin; else ``{}``. Fail-soft."""
    arn = str((entry or {}).get("secret_arn") or "")
    if not is_own_connection_secret(arn, system_id):
        return {}
    try:
        secret = _fetch_secret(arn)
    except Exception as exc:  # noqa: BLE001 - fail-soft; the secret may be gone
        logger.info("%s: %s; resolve_registered_token skipped the registered-secret read",
                    system_id, type(exc).__name__)
        return {}
    origin = (entry or {}).get(CONNECTION_ORIGIN_KEY) or ""
    return secret if is_bound_secret(secret, system_id, origin) else {}


def _neptune_write(cypher: str, parameters: dict) -> bool:
    """Indirection over :func:`db.neptune_write` for the System-node write.

    Kept as a thin module-level wrapper with a LAZY import so (a) there is no
    circular import at module load and (b) the node write is patchable in unit
    tests (no live Neptune/Memgraph). :func:`db.neptune_write` is itself
    fail-soft (returns ``False`` on error); this wrapper only adds the lazy import.
    """
    from .db import neptune_write
    return neptune_write(cypher, parameters)


class ConnectionNodeWriteError(RuntimeError):
    """The System-node write of :func:`write_connection` failed and the caller
    asked for it to be required (``require_node=True``). Retryable; the message
    names the system only, never a connection value."""


class DurableSnapshotError(RuntimeError):
    """:func:`snapshot_durable_record` could not read the prior durable record
    (a checked graph read failed). The re-provision MUST abort before any write
    rather than treat the unreadable record as absent.
    Retryable; names the system only, never a connection value."""


class DurableRestoreError(RuntimeError):
    """A durable-record restore step could not be CONFIRMED (an absence restore
    whose graph write was not acknowledged). Names the system
    only, never a connection value."""


def write_connection(system_id: str, *, non_secret: dict, secret: dict | None = None,
                     secret_arn: str | None = None, entry: dict | None = None,
                     require_node: bool = False, allow_schema_change: bool = False,
                     strict: bool = False) -> str:
    """Persist a system's connection config; return the secret ARN ("" if none).

    Steps: (1) validate the URL/host (SSRF/scheme) BEFORE any write; (2) resolve
    exactly one credential source via :func:`_write_connection_secret` (``secret``
    mints/updates ``adt/conn/<id>`` keeping any registration binding; a
    server-side ``secret_arn`` is stored as-is; neither = no ARN); (3) write the
    credential-free config + ``secret_arn`` + ``connection_type`` onto the
    Neptune ``System`` node (:func:`_persist_node_connection_config`), fail-soft
    unless ``require_node``. ``secret_arn`` is never accepted from a request
    body; ``entry`` carries a not-yet-provisioned registration's own pointer;
    ``allow_schema_change`` lets a claimed user-PostgreSQL schema edit through.

    ``strict`` (re-provision durability): check the operation's
    write guard BEFORE any secret write, so a lost lease raises before the
    credential value is mutated, and REQUIRE a confirmed node write. Both
    failures propagate (not fail-soft), so a caller committing a replacement
    never switches the route or stops the old agent after an unconfirmed or
    lease-lost durable write. Secret values are never logged.
    """
    non_secret = dict(non_secret or {})
    connection_type = non_secret.get("connection_type") or ""
    # (1) Validate the primary target BEFORE any write (reject bad scheme/SSRF).
    target = (non_secret.get("url") or non_secret.get("base_url")
              or non_secret.get("connection_string") or non_secret.get("host") or "")
    if target:
        validate_connection_target(target, connection_type=connection_type or "connection")
    # Strict mode: the lease must still be held BEFORE the secret value is
    # written, so a lost lease never mutates credentials.
    if strict:
        from .lease_guard import check_write_guard
        check_write_guard()
    # (2) Resolve the single credential source → secret ARN (+ the non-secret
    # parts of a rotated user PostgreSQL DSN, so the node keeps dbname/schema).
    resolved_arn, rotated_non_secret = _write_connection_secret(
        system_id, non_secret, secret, secret_arn, entry, allow_schema_change)
    non_secret.update(rotated_non_secret)
    # (3) Credential-free copy onto the Neptune System node. Strict mode REQUIRES
    # a confirmed node write (it propagates instead of failing soft, review
    # R10-F1).
    _persist_node_connection_config(system_id, non_secret, connection_type, resolved_arn,
                                    require_node=require_node or strict)
    logger.info("write_connection: persisted connection for %s (config=%s; %s)",
                system_id, json.dumps(mask_connection(non_secret), default=str),
                "connection secret set" if resolved_arn else "no connection secret")
    return resolved_arn


def snapshot_durable_record(system_id: str) -> dict:
    """Capture the COMPLETE pre-attempt durable record for ``system_id`` so a
    failed re-provision can restore it EXACTLY. Returns a dict:

    * ``existed``: whether the System node held a connection record at all;
    * ``connection_config`` / ``connection_type`` / ``secret_arn``: the node's
      full non-secret record, including the top-level connection type;
    * ``own_secret_version``: the AWSCURRENT version id of this system's OWN
      secret (``adt/conn/<id>``) when it exists, so a rollback can stage the
      prior credential value back; ``None`` when the system has no own secret.

    Uses a CHECKED graph read: an unreadable prior record RAISES
    :class:`DurableSnapshotError` so the re-provision aborts BEFORE any write,
    never recording an unreadable record as absent."""
    _LAST_WRITTEN_SECRET_VERSION.pop(system_id, None)   # start of a fresh attempt
    try:
        node = _fetch_node_config_checked(system_id)
    except Exception as exc:  # noqa: BLE001 - abort before any write; stay retryable
        raise DurableSnapshotError(
            f"the prior durable record of {system_id} could not be read") from exc
    # A CONNECTION record existed only if the node carries at least one
    # connection property; an existing System node with all four connection
    # fields absent snapshots as absent, so rollback restores that absence
    # (not an empty-valued record).
    existed = _node_has_connection_record(node)
    cfg_raw = (node or {}).get("connection_config")
    try:
        cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else dict(cfg_raw or {})
    except (json.JSONDecodeError, ValueError, TypeError):
        cfg = {}
    return {
        "existed": existed,
        "connection_config": cfg,
        "connection_type": (node or {}).get("connection_type") or "",
        "secret_arn": (node or {}).get("secret_arn") or "",
        "own_secret_version": _own_secret_current_version(system_id),
    }


def _node_has_connection_record(node: dict | None) -> bool:
    """Whether a snapshot node row holds an actual CONNECTION record: at least
    one of the four connection properties present (non-null, non-empty). A
    System node that exists but carries none of them is NOT a connection record
    """
    if not node:
        return False
    return any(node.get(k) not in (None, "")
               for k in ("connection_config", "connection_type", "secret_arn",
                         "connection_string"))


def restore_durable_record(system_id: str, snapshot: dict | None) -> None:
    """Restore the durable record to ``snapshot`` after a failed re-provision
    Lease/CAS-safe through the canonical writers, so it can
    never overwrite a NEWER provision. Order: own-secret stage first (so the
    credential value is the prior one before the node points at it), then the
    node.

    * Own secret: move ``AWSCURRENT`` back to the snapshot's prior version
      (:func:`_restore_own_secret_version`); a secret THIS attempt created
      (none at snapshot time, one now) is deleted.
    * Node: rewrite the exact prior record (config + connection_type +
      secret_arn) via the canonical write; if the record did not exist before,
      delete the System node's connection (:func:`_delete_node_connection`).

    Fail-soft per step: the original provisioning error still surfaces."""
    if snapshot is None:
        return
    _restore_own_secret_version(system_id, snapshot.get("own_secret_version"),
                                _LAST_WRITTEN_SECRET_VERSION.get(system_id))
    try:
        if snapshot.get("existed"):
            _restore_node_record(system_id, snapshot)
        else:
            _delete_node_connection(system_id)
    except Exception as exc:  # noqa: BLE001 - original error still surfaces
        logger.warning("durable record restore (node) for %s failed: %s", system_id, exc)


_NODE_SNAPSHOT_QUERY = (
    "MATCH (s:System {id: $sid}) RETURN "
    "s.secret_arn AS secret_arn, s.connection_type AS connection_type, "
    "s.connection_config AS connection_config, s.connection_string AS connection_string")


def _fetch_node_config_checked(system_id: str) -> dict | None:
    """The System node's connection record via a CHECKED graph read
    (:func:`neptune_query_checked`): ``None`` only when the node genuinely holds
    no record, and RAISES ``db.GraphQueryError`` on a read fault. The snapshot
    read must tell "absent" apart from "unreadable", so a
    rollback never deletes a record it merely failed to read."""
    from .db import neptune_query_checked
    rows = neptune_query_checked(_NODE_SNAPSHOT_QUERY, {"sid": system_id})
    return rows[0] if rows else None


def _own_secret_current_version(system_id: str) -> str | None:
    """The AWSCURRENT version id of this system's OWN secret
    (``adt/conn/<system_id>``), or ``None`` ONLY when the secret is confirmed
    ABSENT (an explicit ``ResourceNotFoundException``). Every other Secrets
    Manager failure RAISES :class:`DurableSnapshotError` so the re-provision
    aborts before anything is persisted, rather than treating a transient
    DescribeSecret fault as absence and later deleting a secret that exists or
    losing its prior credential version."""
    try:
        resp = _secrets_client().describe_secret(SecretId=f"adt/conn/{system_id}")
    except Exception as exc:  # noqa: BLE001 - only confirmed-absent is None; else abort
        if _is_secret_absent(exc):
            return None
        raise DurableSnapshotError(
            f"the own secret of {system_id} could not be read") from exc
    for version, stages in (resp.get("VersionIdsToStages") or {}).items():
        if "AWSCURRENT" in stages:
            return version
    # The secret exists but no version carries AWSCURRENT, so its prior version
    # cannot be named; absence is reserved for ResourceNotFound.
    raise DurableSnapshotError(
        f"the own secret of {system_id} has no AWSCURRENT version")


def _is_secret_absent(exc: Exception) -> bool:
    """True only for an explicit Secrets Manager ``ResourceNotFoundException``
    (the secret is confirmed to not exist). Every other failure -- a transient
    fault, throttling, access denied -- is NOT absence."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and response.get("Error", {}).get(
            "Code") == "ResourceNotFoundException":
        return True
    return type(exc).__name__ == "ResourceNotFoundException"


def _restore_own_secret_version(system_id: str, prior_version: str | None,
                                written_version: str | None) -> None:
    """Roll this system's own secret back, lease + CAS safe.
    Touch the secret ONLY while the lease is held AND AWSCURRENT still equals
    ``written_version`` -- the version THIS attempt wrote. If AWSCURRENT is some
    other version (a newer lease owner staged its own value after ours), leave
    the secret alone and log. When this attempt wrote nothing
    (``written_version is None``) there is nothing to roll back. Otherwise, with
    the gate held: a prior version is staged back to ``AWSCURRENT``; a secret
    this attempt CREATED (no prior version) is deleted. Fail-soft."""
    secret_name = f"adt/conn/{system_id}"
    if written_version is None:
        return                                  # this attempt wrote no secret
    try:
        if not _own_secret_rollback_permitted(system_id, written_version):
            logger.warning("%s: rollback of its own connection entry skipped: AWSCURRENT is "
                           "not the version this attempt wrote (or the lease is not held)", system_id)
            return
        if prior_version is None:               # this attempt created the secret
            _secrets_client().delete_secret(
                SecretId=secret_name, ForceDeleteWithoutRecovery=True)
        elif prior_version != written_version:
            _secrets_client().update_secret_version_stage(
                SecretId=secret_name, VersionStage="AWSCURRENT",
                MoveToVersionId=prior_version, RemoveFromVersionId=written_version)
        else:
            return                              # already the prior value
        invalidate_secret(secret_name)
    except Exception as exc:  # noqa: BLE001 - original error still surfaces
        logger.warning("%s: stage rollback of its own connection entry failed: %s", system_id, exc)


def _own_secret_rollback_permitted(system_id: str, written_version: str) -> bool:
    """True only when it is safe to mutate this system's own secret in a
    rollback: the operation's lease is still held (``check_write_guard``) AND
    AWSCURRENT still equals ``written_version`` -- what THIS attempt wrote. A
    lost lease or a newer owner's version returns False, so the secret is left
    untouched."""
    try:
        from .lease_guard import check_write_guard
        check_write_guard()
    except Exception:  # noqa: BLE001 - lease lost: do not touch the secret
        return False
    return _own_secret_current_version(system_id) == written_version


def _restore_node_record(system_id: str, snapshot: dict) -> None:
    """Rewrite the EXACT prior System-node connection record (config +
    connection_type + secret_arn) via the canonical node write (review
    R10-F2). ``require_node=True`` so a failed graph write is not silently
    dropped."""
    _persist_node_connection_config(
        system_id, dict(snapshot.get("connection_config") or {}),
        snapshot.get("connection_type") or "", snapshot.get("secret_arn") or "",
        require_node=True)


def _delete_node_connection(system_id: str) -> None:
    """Remove the System node's connection fields when there was NO prior record
    to restore (this attempt created it), so durable state returns to absence
    A targeted field clear (MATCH, never MERGE: it creates no
    node), leaving any non-connection node data intact. The write must be
    CONFIRMED: an unacknowledged write (``_neptune_write`` returns ``False``)
    RAISES :class:`DurableRestoreError` rather than being silently dropped, so a
    caller is never told absence was restored when it was not."""
    confirmed = _neptune_write(
        "MATCH (s:System {id: $sid}) REMOVE s.connection_config, s.secret_arn, "
        "s.connection_type, s.connection_string",
        {"sid": system_id})
    if confirmed is False:
        raise DurableRestoreError(
            f"the connection fields of {system_id} could not be confirmed removed")
    invalidate_secret(f"adt/conn/{system_id}")


def _write_connection_secret(system_id: str, non_secret: dict, secret: dict | None,
                             secret_arn: str | None, entry: dict | None = None,
                             allow_schema_change: bool = False) -> tuple[str, dict]:
    """Step 2 of :func:`write_connection` → ``(secret_arn, extra_non_secret)``.

    An EXISTING server-determined ``secret_arn`` (e.g. the shared demo DB
    secret) is stored as-is: no secret is written and no node is read. Otherwise
    the system's own registration secret is consulted (:func:`_node_bound_payload`,
    through :func:`own_registration_pointer`: the ``entry``'s pointer, else the
    node's — so an edit BEFORE the first provision is covered too):

    * a rewrite of a REGISTRATION-BOUND secret keeps its binding
      (``connection_origin`` + ``system_id``), so a rotation (the admin
      Edit-connection form, the Settings HighByte token card) never strips the
      durable origin marker the registration paths require;
    * a USER PostgreSQL registration's credentials edit (the admin form
      assembles a full DSN into ``connection_string`` and drops the separate
      password) lands in its OWN secret (:func:`_rotated_user_pg_secret`) —
      writing no secret would wipe the node pointer, and the deployment's env
      password would then be filled in for the user's host.

    Every other call writes exactly what it wrote before. A preserved own
    ``adt/conn/<id>`` pointer (the admin route passes the current pointer on an
    edit with no new secret) does not skip the user-PostgreSQL check when the
    call carries a connection string (an edit always assembles one); without
    one (provision re-pointing the node at the registration's own secret) the
    preserved pointer is stored as-is, as it is whenever that check does not
    apply.
    """
    is_pg = "postgres" in (non_secret.get("connection_type") or "").lower()
    preserved = secret_arn if (secret_arn and not secret) else ""
    if preserved and not (is_pg and is_own_connection_secret(preserved, system_id)
                          and non_secret.get("connection_string")):
        # Never accepted from a request body (security guardrail). A provision
        # re-pointing a user PostgreSQL node at its own secret carries no
        # connection string (nothing to rotate), so it stores the pointer too.
        return preserved, {}
    if not (secret or is_pg):
        return "", {}
    bound = _node_bound_payload(system_id, entry)
    extra: dict = {}
    if is_pg and bound.get(CONNECTION_ORIGIN_KEY) == ORIGIN_USER and bound.get("connection_string"):
        secret, extra = _rotated_user_pg_secret(non_secret, bound, allow_schema_change)
    if not secret:
        return preserved, {}
    if bound:
        secret = {**secret, CONNECTION_ORIGIN_KEY: bound[CONNECTION_ORIGIN_KEY],
                  "system_id": system_id}
    resolved_arn = _store_connection_secret(system_id, secret)
    invalidate_secret(resolved_arn)
    return resolved_arn, extra


def _node_bound_payload(system_id: str, entry: dict | None = None) -> dict:
    """The payload of this system's own registration secret — reached through
    :func:`own_registration_pointer` (the catalog ``entry``'s exact own pointer,
    else the node's), never by name — when it carries a registration binding for
    this system; else ``{}``. Fail-soft (an unreadable node or secret is
    "nothing bound")."""
    arn = own_registration_pointer(system_id, entry)
    if not arn:
        return {}
    payload = _read_registration_secret(system_id, arn) or {}
    origin = payload.get(CONNECTION_ORIGIN_KEY) or ""
    return payload if is_bound_secret(payload, system_id, origin) else {}


def _rotated_user_pg_secret(non_secret: dict, bound: dict,
                            allow_schema_change: bool = False) -> tuple[dict, dict]:
    """``(secret, extra_non_secret)`` for a USER PostgreSQL registration's
    credentials edit: the new DSN + user + password for its own secret, and the
    DSN's non-secret host/port/dbname/schema/search_path for the node. Raises
    :class:`ConnectionTargetError` (nothing is written) unless the edit is a full
    registration DSN for the SAME schema — records-first sampling routes this
    system by its schema — or ``allow_schema_change`` (the caller claimed the
    new schema for an unprovisioned registration)."""
    try:
        new_ns, new_secret = parse_pg_registration_dsn(non_secret.get("connection_string", ""))
        old_ns, _ = parse_pg_registration_dsn(bound.get("connection_string", ""))
    except ValueError as exc:
        raise ConnectionTargetError(
            f"a registered PostgreSQL system's connection must be a full connection "
            f"string ({exc})") from None
    if new_ns["schema"] != old_ns["schema"] and not allow_schema_change:
        raise ConnectionTargetError(
            "a registered PostgreSQL system keeps its schema; register a new system "
            "for another schema")
    return new_secret, {**new_ns, "search_path": new_ns["schema"]}


def _persist_node_connection_config(system_id: str, non_secret: dict,
                                    connection_type: str, resolved_arn: str,
                                    *, require_node: bool = False) -> None:
    """Step 3 of :func:`write_connection`. Build a credential-free copy for
    AT-REST persistence: strip inline user:pass@ userinfo AND secret query
    params from url/base_url/connection_string so a secret carried inline in a
    URL is never stored in cleartext on the node or catalog (FIX 2), then MERGE
    the non-secret config onto the Neptune System node — the SOLE source of
    connection state. Fail-soft: a node-write failure (no Memgraph locally,
    transient Neptune error) must NOT abort the secret write — log a WARNING
    and continue. ``require_node``: a failed write (an exception, or the graph
    client's ``False``) raises :class:`ConnectionNodeWriteError` instead."""
    persist_ns = dict(non_secret)
    for _uk in ("url", "base_url", "connection_string"):
        if isinstance(persist_ns.get(_uk), str):
            persist_ns[_uk] = _strip_url_credentials(persist_ns[_uk])
    node_cfg = ({k: persist_ns[k] for k in _NODE_CONFIG_KEYS if k in persist_ns}
                or _existing_node_config(system_id))     # credentials-only: keep config
    from .db import resubmit_safe_write
    try:
        # Safe to run twice (a MERGE and SETs of parameters built here; text
        # pinned in a test): it may be sent again after an ambiguous failure.
        with resubmit_safe_write():
            written = _neptune_write(
                "MERGE (s:System {id: $sid}) "
                "SET s.connection_config = $cfg_json, s.secret_arn = $arn, "
                "s.connection_type = $ctype",
                {"sid": system_id, "cfg_json": json.dumps(node_cfg, default=str),
                 "arn": resolved_arn or "", "ctype": connection_type or ""},
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft; node write never blocks
        logger.warning("write_connection: node write failed for %s (fail-soft): %s "
                       "(config=%s)", system_id, _safe_detail(exc, non_secret),
                       json.dumps(mask_connection(node_cfg), default=str))
        written = False
    if require_node and written is False:
        raise ConnectionNodeWriteError(f"the System node of {system_id} could not be written")


def _existing_node_config(system_id: str) -> dict:
    """The node's current non-secret ``connection_config`` (``{}`` when there is
    none or it cannot be read) — kept by a credentials-only update."""
    return {k: v for k, v in _node_connection(system_id).items()
            if k in _NODE_CONFIG_KEYS and k != "connection_string" and v not in (None, "")}


def write_scan_budget(system_id: str, max_scanned_bytes: int | None) -> None:
    """Persist ONLY the per-system Athena scanned-bytes budget onto the System node.

    A TARGETED, non-secret update: it reads the node's current
    ``connection_config`` blob, sets (or, when ``max_scanned_bytes is None``,
    removes) the ``max_scanned_bytes`` key, and writes back ONLY
    ``s.connection_config`` — deliberately NOT touching ``secret_arn`` or
    ``connection_type``, so it can never wipe a system's credential pointer the way
    a full :func:`write_connection` (which rewrites secret_arn) would.

    ``max_scanned_bytes`` is the caller-validated value (a positive int, or None to
    clear the per-system budget and fall back to the env/default). Fail-LOUD: a
    graph write failure raises so the admin route surfaces an error rather than
    silently dropping the setting.
    """
    node = _fetch_node_config(system_id)
    cfg: dict = {}
    if node and node.get("connection_config"):
        node_cfg = node["connection_config"]
        try:
            cfg = json.loads(node_cfg) if isinstance(node_cfg, str) else dict(node_cfg)
        except (json.JSONDecodeError, ValueError, TypeError):
            cfg = {}
    if max_scanned_bytes is None:
        cfg.pop("max_scanned_bytes", None)
    else:
        cfg["max_scanned_bytes"] = int(max_scanned_bytes)
    ok = _neptune_write(
        "MERGE (s:System {id: $sid}) SET s.connection_config = $cfg_json",
        {"sid": system_id, "cfg_json": json.dumps(cfg, default=str)},
    )
    if not ok:
        raise RuntimeError(f"failed to persist scan budget for {system_id}")


#: The pointer-only System-node write of :func:`publish_secret_pointer`, a
#: compare-and-set in ONE statement (one transaction): MATCH, never MERGE, so
#: it creates no node; it lands only while the node's connection type and
#: pointer are still the ones the caller read; it sets ``secret_arn`` alone and
#: RETURNs it, so a row is the read-back and no row is a changed node.
_POINT_NODE_AT_SECRET = (  # nosec B105 - a Cypher statement that names the secret_arn property; no credential
    "MATCH (s:System {id: $sid}) "
    "WHERE coalesce(s.connection_type, '') = $ctype "
    "AND coalesce(s.secret_arn, '') = $expected "
    "SET s.secret_arn = $arn RETURN s.secret_arn AS secret_arn")

#: The System node's connection, read CHECKED (:func:`node_connection_checked`).
_NODE_CONNECTION_QUERY = (
    "MATCH (s:System {id: $sid}) RETURN s.status AS status, "
    "s.connection_type AS connection_type, s.connection_origin AS connection_origin, "
    "s.connection_config AS connection_config, s.secret_arn AS secret_arn")


class ConnectionChangedError(RuntimeError):
    """The System node's connection type or pointer was no longer the one the
    caller read, so :func:`publish_secret_pointer` changed nothing (another
    change of the connection landed in between). Retryable after a fresh read;
    the message names the system only."""


def _neptune_write_returning(cypher: str, parameters: dict):
    """Indirection over :func:`db.neptune_write_returning` (lazy import, and
    patchable in unit tests like :func:`_neptune_write`): the rows the
    statement RETURNs, or ``None`` when the backend refused or could not run it."""
    from .db import neptune_write_returning
    return neptune_write_returning(cypher, parameters)


def node_connection_checked(system_id: str) -> dict:
    """``{exists, status, connection_type, connection_origin, secret_arn,
    has_config}`` for the System node, read with ``neptune_query_checked`` (a
    graph read failure RAISES ``db.GraphQueryError``). The strings are ``""``
    when unset, the values :func:`publish_secret_pointer` compares against.
    Never a secret value."""
    from .db import neptune_query_checked
    rows = neptune_query_checked(_NODE_CONNECTION_QUERY, {"sid": system_id})
    row = (rows[0] if rows else None) or {}
    return {"exists": bool(rows),
            "status": str(row.get("status") or ""),
            "connection_type": str(row.get("connection_type") or ""),
            "connection_origin": str(row.get("connection_origin") or ""),
            "secret_arn": str(row.get("secret_arn") or ""),
            "has_config": bool(row.get("connection_config"))}


def publish_secret_pointer(system_id: str, secret_arn: str, *, expected_arn: str,
                           connection_type: str) -> None:
    """Point an EXISTING System node at ``secret_arn``, only while its
    connection type is still ``connection_type`` and its pointer still
    ``expected_arn`` (what :func:`node_connection_checked` read).

    A TARGETED update like :func:`write_scan_budget`, the other way round: it
    sets only ``s.secret_arn`` and leaves ``connection_config`` and
    ``connection_type`` as they are (a full :func:`write_connection` rewrites
    both). The node is the sole source every process resolves a connection
    from (:func:`resolve_connection`: the onboarding runtime's test_connection
    and discover, the app's provision, a restarted app's catalog), so a caller
    that stored a new secret is done only once this returns. One statement
    (:data:`_POINT_NODE_AT_SECRET`): it writes and returns the pointer, so no
    other write can land between a check and the set. Raises
    :class:`ConnectionChangedError` when the node no longer has the expected
    type and pointer (nothing changed), and :class:`ConnectionNodeWriteError`
    when the graph client refused the statement or the pointer it returned is
    not ``secret_arn``. This process's cached copy of the secret is dropped on
    success."""
    rows = _neptune_write_returning(_POINT_NODE_AT_SECRET, {
        "sid": system_id, "arn": secret_arn, "expected": expected_arn or "",
        "ctype": connection_type or ""})
    if rows is None:
        raise ConnectionNodeWriteError(f"the System node of {system_id} could not be written")
    if not rows:
        raise ConnectionChangedError(f"the connection of {system_id} changed")
    if str((rows[0] or {}).get("secret_arn") or "") != secret_arn:
        raise ConnectionNodeWriteError(
            f"the System node of {system_id} does not point at the new secret")
    invalidate_secret(secret_arn)


def _catalog_get(system_id: str) -> dict | None:
    """Canonical catalog-read seam. No production resolve/write path reads the
    catalog (the ``System`` node is the sole source); this seam is retained only
    so the unit suite can assert that invariant — a tripwire monkeypatch raises if
    any read path ever calls it again.
    """
    from .catalog import catalog
    return catalog.get(system_id)


def _fetch_node_config(system_id: str) -> dict | None:
    """Read non-secret config + secret ARN from the Neptune System node.

    Returns ``None`` when the node has no stored config (tolerated now; Step 3
    populates it). Never returns secret values — only the secret ARN.
    """
    from .db import neptune_query
    rows = neptune_query(
        "MATCH (s:System {id: $sid}) RETURN "
        "s.connection_config AS connection_config, s.secret_arn AS secret_arn, "
        "s.connection_type AS connection_type, s.connection_string AS connection_string",
        {"sid": system_id},
    )
    return rows[0] if rows else None


def node_secret_arn_checked(system_id: str) -> str:
    """The System node's ``secret_arn`` (``""`` when there is no node or no
    pointer), reading the graph with ``neptune_query_checked``: a graph read
    failure RAISES ``db.GraphQueryError`` instead of looking like "no node". For
    a caller whose decision would be wrong on a failed read (the PostgreSQL
    origin decision, ``onboard._pg_entry_with_origin``)."""
    from .db import neptune_query_checked
    rows = neptune_query_checked(
        "MATCH (s:System {id: $sid}) RETURN s.secret_arn AS secret_arn",
        {"sid": system_id},
    )
    return str((rows[0] if rows else {}).get("secret_arn") or "")


_NODE_STATE_QUERY = ("MATCH (s:System {id: $sid}) "
                     "RETURN s.secret_arn AS secret_arn, s.connection_config AS connection_config")


def node_state_checked(system_id: str) -> dict:
    """``{exists, secret_arn, has_config}`` for the System node, read with
    ``neptune_query_checked`` (a graph read failure RAISES
    ``db.GraphQueryError``). Tells "no node" apart from "a node with no
    pointer": a half-written or legacy node can hold a ``connection_config``
    with an empty ``secret_arn``, and a caller deciding "not yet provisioned"
    must not read that as no node. Never a secret value."""
    from .db import neptune_query_checked
    rows = neptune_query_checked(_NODE_STATE_QUERY, {"sid": system_id})
    if not rows:
        return {"exists": False, "secret_arn": "", "has_config": False}
    row = rows[0] or {}
    return {"exists": True, "secret_arn": str(row.get("secret_arn") or ""),
            "has_config": bool(row.get("connection_config"))}


def _unconfigured_result(system_id: str) -> dict:
    """Return the canonical 'unconfigured' resolved result for an unregistered system.

    Emitted when the ``System`` node has no stored ``connection_config`` (the
    system was never registered) or the graph read fails. It is a benign,
    connection-free shape — callers already guard on a missing
    url/base_url/connection_string + empty secret_arn and skip. NEVER a catalog
    default: the node is the sole source.
    """
    return {"system_id": system_id, "connection_type": None,
            "connection_string": "", "secret_arn": "",
            "_unconfigured": True, "_secret_unresolved": False}


def resolve_connection(system_id: str) -> dict:
    """Resolve a system's live connection config — NODE-ONLY, no catalog fallback.

    The Neptune/Memgraph ``System`` node is the SOLE source of a system's
    non-secret connection config (``url``/``host``/``port``/``scheme``/``base_url``/
    ``dbname``/...), its ``connection_type``/``connection_string``, and the
    ``secret_arn`` pointer. Resolution:

      1. Read the node's stored ``connection_config`` + ``secret_arn``
         (:func:`_fetch_node_config`).
      2. If the node has NO ``connection_config`` (unregistered system) OR the
         graph read fails → return :func:`_unconfigured_result` (``_unconfigured:
         True``). The catalog is NEVER read, and this path NEVER raises.
      3. Otherwise merge the node's non-secret config, then fetch the referenced
         secret by ARN (TTL-cached) and merge it last so it wins over any
         placeholder.

    Fail-soft on the secret fetch too: a fetch failure leaves the resolved config
    without secret fields and sets ``_secret_unresolved=True`` (the probe surfaces
    the accurate ``credential_unresolved`` reason). The read path never raises.

    NOTE: brand-new systems are registered with their connection supplied as
    INPUT to the discover/provision API (``body.connection_string``) — not via a
    fallback — so an unconfigured node here means genuinely unregistered.
    """
    try:
        node = _fetch_node_config(system_id)
    except Exception as exc:  # noqa: BLE001 - fail-soft; never raise on the read path
        logger.warning("resolve_connection: graph read failed for %s — "
                       "returning unconfigured (node is sole source): %s", system_id, exc)
        return _unconfigured_result(system_id)

    # Unregistered: no node row, or a node with no stored connection_config.
    if not (node and node.get("connection_config")):
        return _unconfigured_result(system_id)
    resolved, secret_arn = _node_resolved(system_id, node)
    return _resolve_node_secret(resolved, system_id, secret_arn)


def _node_resolved(system_id: str, node: dict) -> tuple[dict, str]:
    """``(resolved, secret_arn)`` from a configured System node: its non-secret
    config merged over the system id / type / connection string, the pointer,
    and ``_secret_unresolved`` False. No IO; a bad config is logged and skipped."""
    resolved: dict = {"system_id": system_id,
                      "connection_type": None, "connection_string": ""}
    non_secret: dict = {}
    secret_arn = ""  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture

    node_cfg = node.get("connection_config")
    try:
        parsed = json.loads(node_cfg) if isinstance(node_cfg, str) else dict(node_cfg)
        non_secret.update(parsed)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("resolve_connection: bad node connection_config for %s: %s",
                       system_id, exc)
    if node.get("secret_arn"):
        secret_arn = node["secret_arn"]
    if node.get("connection_type"):
        resolved["connection_type"] = node["connection_type"]
    if node.get("connection_string"):
        resolved["connection_string"] = node["connection_string"]

    resolved.update(non_secret)
    resolved["secret_arn"] = secret_arn
    # Tracks whether a CONFIGURED secret could not be fetched. The probe surfaces
    # this as the coarse 'credential_unresolved' reason instead of running
    # tokenless and reporting a misleading auth failure. Stays fail-soft (no raise).
    resolved["_secret_unresolved"] = False
    return resolved, secret_arn


def _resolve_node_secret(resolved: dict, system_id: str, secret_arn: str) -> dict:
    """Finish :func:`resolve_connection`: merge the node's secret (or, for a
    seat-sourced Tulip node, its whole request tuple). Fail-soft."""
    # An ARN-sourced Tulip node (its pointer is a NAMED seat secret, not the
    # system's own adt/conn/<id>) must resolve its WHOLE request tuple from ONE
    # fresh read: base_url/workspace_id/key_scope/token all come from the seat
    # secret, validated as a complete contract, and NO node-snapshot field is
    # kept beside them. An incomplete/invalid/unreadable fresh secret is a
    # retryable miss (nothing sent), never a split tuple.
    tulip_arn_sourced = (
        resolved.get("connection_type") == _TULIP_CONNECTION_TYPE
        and bool(secret_arn) and not is_own_connection_secret(secret_arn, system_id))
    if tulip_arn_sourced:
        return _resolve_tulip_seat(resolved, system_id, secret_arn)

    # Merge secret fields last so they win over any placeholder in the config.
    if secret_arn:
        try:
            resolved.update(_fetch_secret(secret_arn))
        except Exception as exc:  # noqa: BLE001 - fail-soft; never raise on read
            logger.warning("resolve_connection: %s: %s (secret fetch failed)", system_id, exc)
            resolved["_secret_unresolved"] = True
    # A Tulip system whose secret carries the raw api_key/api_secret (the seat
    # secret contract) gets its HTTP Basic token derived from the SAME read, so
    # the routing (base_url/workspace_id/key_scope, already merged above) and the
    # credential travel together. An explicit token in the secret wins.
    if resolved.get("connection_type") == _TULIP_CONNECTION_TYPE:
        from . import tulip_seat_contract as _tsc
        _tsc.with_derived_token(resolved)
    return resolved


def _resolve_tulip_seat(resolved: dict, system_id: str, secret_arn: str) -> dict:
    """Finish ``resolve_connection`` for an ARN-sourced Tulip node: the WHOLE
    request tuple comes from ONE fresh seat-secret read (``request_tuple``), or
    the source is a retryable miss with no routing/credential field kept from
    the node snapshot (``_arn_unresolved``). Fail-soft; never raises."""
    from . import tulip_seat_contract as _tsc
    _arn_unresolved(resolved)   # strip every node-snapshot routing/credential field
    resolved["_secret_unresolved"] = False
    try:
        secret = _fetch_secret(secret_arn)
    except Exception as exc:  # noqa: BLE001 - fail-soft; unreadable ⇒ retryable miss
        logger.warning("resolve_connection: %s: %s (Tulip seat secret fetch failed)",
                       system_id, exc)
        return _arn_unresolved(resolved)
    try:
        req = _tsc.request_tuple(secret)
    except _tsc.TulipContractError as exc:
        logger.warning("resolve_connection: %s: %s (the Tulip seat secret is not a "
                       "complete contract; unusable)", system_id, exc)
        return _arn_unresolved(resolved)
    # The entire routing + credential tuple from the one read, nothing else.
    resolved["base_url"] = req.base_url
    resolved["workspace_id"] = req.workspace_id
    resolved["key_scope"] = req.key_scope
    resolved["token"] = req.token          # the tuple's validated credential
    resolved["auth_scheme"] = req.auth_scheme
    _set_table_scope(resolved, req)
    resolved["credential_source"] = "secret_arn"
    return resolved


def _set_table_scope(out: dict, req) -> None:
    """Set ``out``'s Tulip table scope from the request tuple ``req`` (one
    fresh seat read): its ``table_label_prefix`` when the seat names one, else
    none. A scope left in a stored or submitted config never survives beside
    the seat's routing."""
    out.pop("table_label_prefix", None)
    if req.table_label_prefix:
        out["table_label_prefix"] = req.table_label_prefix


def _refuse_env_fill_for_own_secret(resolved: dict, user, password,
                                   entry: dict | None = None) -> None:
    """Raise :class:`~agents.errors.ConnectionSecretUnavailableError` when the
    deployment's env credentials would fill a USER's connection: the catalog
    ``entry`` is a user registration without a user and password of its own
    (even on the env host — the deployment's superuser must never stand in for
    the user's account), or the resolved connection points at the system's OWN
    ``adt/conn/<id>`` secret and that secret could not be read, or is a user
    registration without a user and password."""
    sid = str(resolved.get("system_id") or (entry or {}).get("id") or "")
    if is_user_registration(entry) and not (user and password):
        raise _unavailable(sid, "has no user and password of its own")
    if not is_own_connection_secret(resolved.get("secret_arn"), sid):
        return
    if resolved.get("_secret_unresolved"):
        raise _unavailable(sid, "could not be read")
    if resolved.get(CONNECTION_ORIGIN_KEY) == ORIGIN_USER and not (user and password):
        raise _unavailable(sid, "has no user and password")


def _refuse_foreign_env_target(system_id: str, host, dbname, env_params: dict) -> None:
    """The deployment's env credentials fill ONLY the env target: a resolved
    host or database that differs from the env's (an admin moved the system to
    another server, or a node names another database) is refused rather than
    sent the deployment's password. Hosts compare case-insensitively."""
    env_host = str(env_params.get("host") or "").strip().lower()
    if host and str(host).strip().lower() != env_host:
        raise _unavailable(system_id or "this system",
                           "is missing, and the deployment's database credentials are only "
                           "for the deployment's own database host")
    if dbname and env_params.get("dbname") and str(dbname) != str(env_params["dbname"]):
        raise _unavailable(system_id or "this system",
                           "is missing, and the deployment's database credentials are only "
                           "for the deployment's own database")


def _env_credential_fill(search_path: str, host, port, dbname, user,
                         system_id: str = "") -> tuple:
    """Live-env credential bootstrap (NOT a catalog read): ``user``/``password``
    ALWAYS from env, ``host``/``port``/``dbname`` only where the node supplied
    none — and only for the env target itself (:func:`_refuse_foreign_env_target`).
    Routed ERP vs demo by schema via db's ENV-ONLY split — deliberately NOT
    the records-first ``_get_conn_params_for_schema``, which calls
    resolve_connection → pg_conn_params and would recurse (plan risk R3; db.py's
    thread-local ``_RECORDS_FIRST_GUARD`` is the belt to this suspender)."""
    from .db import _env_conn_params_for_schema
    env_params = _env_conn_params_for_schema(search_path)
    _refuse_foreign_env_target(system_id, host, dbname, env_params)
    return (host or env_params.get("host"),
            port if port is not None else env_params.get("port"),
            dbname or env_params.get("dbname"),
            user or env_params.get("user"),
            env_params.get("password"))


def pg_conn_params(resolved: dict, *, entry: dict | None = None) -> dict:
    """Assemble pgdriver connect kwargs from a resolved connection dict (DRY).

    Single SQL-reassembly point (``server._restore``, the ``system_agent`` PG
    path, records-first ``db``). ``host``/``port``/``dbname`` (``database``
    alias) come from the resolved node config, ``user`` (``username`` alias)/
    ``password`` from the secret :func:`resolve_connection` merged, and
    ``search_path`` (or ``schema``) becomes ``options=-csearch_path=<value>``.
    With no ``password`` the gaps come from the live env
    (:func:`_env_credential_fill`; ADR-0044 Decision 1) — only for the env
    target itself, and never for a user registration (``entry``, passed by the
    caller: this module reads no catalog) or a system's own registration secret
    (:func:`_refuse_env_fill_for_own_secret`). The password is never logged.

    Returns pgdriver kwargs ``{host, port, dbname, user, password[, options]}``.
    """
    resolved = resolved or {}
    search_path = resolved.get("search_path") or resolved.get("schema") or ""
    host = resolved.get("host")
    port = resolved.get("port")
    dbname = resolved.get("dbname") or resolved.get("database")
    user = resolved.get("user") or resolved.get("username")
    password = resolved.get("password")
    _refuse_env_fill_for_own_secret(resolved, user, password, entry)
    credential_source = "resolved"
    if not password:
        host, port, dbname, user, password = _env_credential_fill(
            search_path, host, port, dbname, user, str(resolved.get("system_id") or ""))
        credential_source = "env"

    params: dict = {"host": host, "port": port, "dbname": dbname,
                    "user": user, "password": password}
    if search_path:
        params["options"] = f"-csearch_path={search_path}"

    # Target-only debug line — NEVER includes the password value.
    logger.debug("pg_conn_params: assembled connect params host=%s port=%s "
                 "dbname=%s user=%s search_path=%s (%s credentials)",
                 host, port, dbname, user, search_path or None, credential_source)
    return params


def describe_connection(system_id: str) -> dict:
    """Return a masked, browser-safe summary of a system's resolved connection.

    Backend half of the read-only admin "current connection" display (req6a),
    and the GUARANTEED server-side record read client tooling uses to derive a
    system's connection from the persisted ``System`` node instead of a local
    shell env (``scripts/reonboard.py``, ADR-0044 Decision 3). It resolves the
    connection, masks it via :func:`mask_connection`, then projects ONLY
    non-secret fields plus presence indicators. It MUST NOT return
    ``connection_string``, a ``url`` with inline credentials, ``token``,
    ``password``, or any other secret value — secrets are reported as
    presence-only (``has_secret`` + the secret-field KEY NAMES, never values).
    ``secret_arn`` is an ARN *pointer* (an identifier the caller's own AWS
    credentials must be entitled to resolve), NEVER a secret value —
    same non-secret classification as the node persist and
    :data:`CATALOG_SAFE_CONFIG_KEYS`. ``search_path`` accepts the node's
    ``schema`` alias, mirroring :func:`pg_conn_params`.

    Returns ``{system_id, connection_type, host, port, scheme, base_url,
    dbname, search_path, secret_arn, has_secret, secret_fields}``.
    """
    resolved = resolve_connection(system_id)
    return _describe_resolved(system_id, resolved)


def describe_unprovisioned_registration(system_id: str, entry: dict | None) -> dict | None:
    """The :func:`describe_connection` summary of a USER registration not yet
    provisioned, or ``None`` for any other system (the caller summarises the
    node as before).

    Before its first provision a user registration has no graph node, so the
    node summary is empty. Its summary then comes from its registration
    (:func:`registration_connection`: the entry's non-secret settings + its
    exact own bound ``adt/conn/<id>`` pointer for PostgreSQL or an inline Tulip
    system, or the
    request-NAMED seat secret's routing for an ARN-sourced Tulip system — the
    secret supplies the ``base_url`` / ``workspace_id`` / ``credential_source``
    the raw entry leaves blank), projected exactly like the node one (non-secret
    fields + presence only). Provisioned or not is decided by a CHECKED node
    read (:func:`node_state_checked`): a node holding a pointer OR a config is
    summarised from the node, exactly as a provisioned system is (a half-written
    node with a config and no pointer shows ``has_secret: false``, never the
    registration's pointer beside the node's host). A graph error raises
    ``db.GraphQueryError``; a pointer or config the checked read finds while the
    node itself reads as unconfigured, or an unreadable / unbound own secret,
    raises :class:`~agents.errors.ConnectionSecretUnavailableError`. Reads no
    catalog: ``entry`` is the caller's.

    Only PostgreSQL and Tulip registrations are summarised here. Every other
    type (an MCP server with no token, Athena, SiteWise and the
    other IAM-only types) returns ``None`` as before, because its usable
    configuration need not carry a pointer or a ``base_url``."""
    if not _summarised_registration(entry):
        return None
    state = node_state_checked(system_id)
    if state["secret_arn"] or state["has_config"]:
        resolved = resolve_connection(system_id)
        if resolved.get("_unconfigured"):
            raise _unavailable(system_id, "node could not be read")
        return _describe_resolved(system_id, resolved)
    registered = registration_connection(system_id, entry)
    # A registration that resolved nothing usable cannot be summarised. A
    # PostgreSQL registration needs its own pointer. A Tulip registration needs
    # its own pointer (API key and secret) or the base_url its named seat
    # secret supplied (Secrets Manager secret); an unreadable seat secret sets
    # _secret_unresolved and strips the routing.
    usable = registered.get("secret_arn") or (
        entry.get("connection_type") == _TULIP_CONNECTION_TYPE and registered.get("base_url"))
    if registered.get("_secret_unresolved") or not usable:
        raise _unavailable(system_id, "cannot be read")
    return _describe_resolved(system_id, registered)


#: The user registration types :func:`describe_unprovisioned_registration`
#: summarises from the registration before the first provision.
_REGISTRATION_SUMMARY_TYPES = frozenset({"PostgreSQL", _TULIP_CONNECTION_TYPE})


def _summarised_registration(entry: dict | None) -> bool:
    """Whether :func:`describe_unprovisioned_registration` summarises ``entry``:
    a USER registration of a :data:`_REGISTRATION_SUMMARY_TYPES` type."""
    if not is_user_registration(entry):
        return False
    ctype = (entry or {}).get("connection_type")
    return ctype in _REGISTRATION_SUMMARY_TYPES


def _describe_resolved(system_id: str, resolved: dict) -> dict:
    """The masked, non-secret projection of a resolved connection."""
    masked = mask_connection(resolved)
    # Secret-bearing fields STORED on the resolved dict — names only, no values.
    # A key whose value is empty (a client_id left blank) is not configured,
    # so the Edit form must not badge it as such.
    secret_fields = sorted(str(k) for k, v in resolved.items()
                           if str(k).lower() in _REDACT_KEYS and str(v or "").strip())
    has_secret = bool(resolved.get("secret_arn")) or bool(secret_fields)
    summary = {
        "system_id": system_id,
        "connection_type": masked.get("connection_type"),
        "host": masked.get("host"),
        "port": masked.get("port"),
        "scheme": masked.get("scheme"),
        "base_url": masked.get("base_url"),
        "dbname": masked.get("dbname"),
        "search_path": masked.get("search_path") or masked.get("schema"),
        "secret_arn": masked.get("secret_arn") or "",
        "has_secret": has_secret,
        "secret_fields": secret_fields,
        # Non-secret routing/credential-source markers so the admin Edit form
        # can render a secret-sourced (Secrets Manager) connection read-only:
        # workspace_id / key_scope are non-secret routing fields, and
        # credential_source == "secret_arn" marks an ARN-sourced system (never a
        # secret value).
        "workspace_id": masked.get("workspace_id") or "",
        "key_scope": masked.get("key_scope") or "",
        "credential_source": masked.get("credential_source") or "",
    }
    # The Tulip table scope (non-secret), only when one is set, so a summary
    # of a system without one is unchanged.
    if masked.get("table_label_prefix"):
        summary["table_label_prefix"] = masked["table_label_prefix"]
    return summary


def audit_connection_event(action: str, system_id: str, actor: str, outcome: str,
                           *, extra: dict | None = None) -> None:
    """Emit a structured, secret-free audit log line for a connection event."""
    event = {"event": "conn_audit", "action": action, "system_id": system_id,
             "actor": actor, "outcome": outcome,
             "ts": datetime.now(timezone.utc).isoformat()}
    if extra:
        for key, value in extra.items():
            if str(key).lower() not in _REDACT_KEYS:  # never include secret material
                event[key] = value
    logger.info(json.dumps(event))


# --------------------------------------------------------------------------- #
# Connect New System registrations — the connection a USER registered, before
# its first provision writes the graph node (``resolve_connection`` is node-only,
# ADR-0031), plus the rules that keep that connection's credentials its own.
#
# Every registration carries ONE explicit origin marker on its catalog entry
# (``CONNECTION_ORIGIN_KEY``): ``ORIGIN_USER`` for the HTTP route and the
# onboarding chat tool, ``ORIGIN_DEMO`` for the demo/workshop registration hook.
# Only ``ORIGIN_USER`` entries ever reach the helpers below, so a demo/workshop
# system keeps its exact path. A PostgreSQL user registration's DSN lives ONLY in
# its own ``adt/conn/<id>`` secret, whose payload repeats the origin and the
# system id — the durable marker that survives a task restart (``server._restore``
# re-marks the entry from it).
# --------------------------------------------------------------------------- #

#: Catalog-entry (and PostgreSQL registration-secret) origin marker.
CONNECTION_ORIGIN_KEY = "connection_origin"
ORIGIN_USER = "user"
ORIGIN_DEMO = "demo"

#: Key of a PostgreSQL connection string inside a registration's
#: ``connection_config``. The DSN carries the password, so it rides ONLY here and
#: lands ONLY in the ``adt/conn/<id>`` secret (never the catalog or the node).
PG_DSN_CONFIG_KEY = "dsn"

_PG_SCHEMES = ("postgres", "postgresql")
_SECRET_NAME_SUFFIX = re.compile(r"-[A-Za-z0-9]{6}")
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEARCH_PATH_OPTION = re.compile(r"search_path\s*=\s*(\S+)")
_DEFAULT_PORTS = {"https": 443, "http": 80}


def is_user_registration(entry) -> bool:
    """True iff a catalog entry was registered by a user (HTTP route or chat tool)."""
    return bool(entry) and entry.get(CONNECTION_ORIGIN_KEY) == ORIGIN_USER


def is_own_connection_secret(secret_arn, system_id) -> bool:
    """True iff ``secret_arn`` is exactly this system's own ``adt/conn/<id>``
    secret: a Secrets Manager ARN in this Region (``AWS_REGION``, the Region
    ``_store_connection_secret`` writes to), a 12-digit account, and a resource
    name of exactly ``adt/conn/<system_id>`` plus the 6-character suffix Secrets
    Manager appends. No substring match (``foo`` never owns ``foo-bar``'s secret).
    The account is not compared here (a pure shape check); every read of a
    secret refuses an ARN in another account than the deployment's
    (:func:`refuse_foreign_account_secret`), so an own-NAMED secret in another
    account is unreadable, which fails closed. A secret is only USED once its
    payload is bound to the system (:func:`is_bound_user_secret`).
    """
    parts = str(secret_arn or "").split(":", 6)
    if len(parts) != 7 or not system_id:
        return False
    head, partition, service, region, account, rtype, resource = parts
    if (head != "arn" or not partition.startswith("aws") or service != "secretsmanager"
            or region != os.environ.get("AWS_REGION", "us-east-1") or rtype != "secret"
            or not re.fullmatch(r"\d{12}", account)):
        return False
    name = f"adt/conn/{system_id}"
    return (resource.startswith(name)
            and _SECRET_NAME_SUFFIX.fullmatch(resource[len(name):]) is not None)


def is_bound_user_secret(payload, system_id) -> bool:
    """True iff a secret payload (or a resolved dict it was merged into) is a
    user registration bound to exactly ``system_id``."""
    return is_bound_secret(payload, system_id, ORIGIN_USER)


def is_bound_secret(payload, system_id, origin) -> bool:
    """True iff a secret payload carries the registration binding written by
    ``onboard_agent`` (``connection_origin`` + ``system_id``) for exactly this
    system and this (non-empty) origin."""
    return (isinstance(payload, dict) and bool(origin)
            and payload.get(CONNECTION_ORIGIN_KEY) == origin
            and payload.get("system_id") == system_id)


def _node_connection(system_id: str) -> dict:
    """The System node's non-secret connection (``connection_config`` parsed,
    plus ``secret_arn`` / ``connection_type`` / ``connection_string``), ``{}``
    when there is no node or it cannot be read. Never a secret value."""
    try:
        node = _fetch_node_config(system_id) or {}
    except Exception as exc:  # noqa: BLE001 - an unreadable node is "no node"
        logger.warning("registration: node read failed for %s: %s",
                       system_id, type(exc).__name__)
        return {}
    cfg = node.get("connection_config") or {}
    try:
        cfg = json.loads(cfg) if isinstance(cfg, str) else dict(cfg)
    except (ValueError, TypeError):
        cfg = {}
    return {**{k: v for k, v in cfg.items() if k in _NODE_CONFIG_KEYS},
            "secret_arn": str(node.get("secret_arn") or ""),
            "connection_type": node.get("connection_type") or "",
            "connection_string": (node.get("connection_string")
                                  or cfg.get("connection_string") or "")}


def own_registration_pointer(system_id: str, entry: dict | None,
                             node: dict | None = None) -> str:
    """THE pointer rule for a user registration's own secret: the catalog
    entry's ``secret_arn`` when it is exactly this system's own
    ``adt/conn/<id>``, else the graph node's (a task restart rebuilds the entry
    without the pointer), else ``""``. The payload binding is checked by the
    caller that reads the secret."""
    arn = str((entry or {}).get("secret_arn") or "")
    if is_own_connection_secret(arn, system_id):
        return arn
    node = _node_connection(system_id) if node is None else node
    arn = str(node.get("secret_arn") or "")
    return arn if is_own_connection_secret(arn, system_id) else ""


def _credential_source(entry: dict | None, node: dict | None) -> str:
    """The connection's ``credential_source`` marker (``"secret_arn"`` for a
    system whose key comes from a request-NAMED secret), read from the graph
    node's config first (authoritative once written), else the catalog entry's
    non-secret snapshot. ``""`` for an inline-key / IAM-role system."""
    for cfg in (_node_config_dict(node), (entry or {}).get("connection_config")):
        parsed = sanitize_connection_config(cfg or {})
        src = str(parsed.get("credential_source") or "").strip()
        if src:
            return src
    return ""


def _node_config_dict(node: dict | None) -> dict:
    """The node's stored ``connection_config`` as a dict (handles the JSON-string
    at-rest form), ``{}`` when absent/unparseable."""
    cfg = (node or {}).get("connection_config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            return {}
    return cfg if isinstance(cfg, dict) else {}


def _is_arn_sourced(entry: dict | None, node: dict | None) -> bool:
    """True when this system takes its credentials from a request-NAMED secret
    (``credential_source == "secret_arn"``) with a pointer to resolve."""
    return (_credential_source(entry, node) == "secret_arn"
            and bool(str((entry or {}).get("secret_arn")
                         or (node or {}).get("secret_arn") or "")))


#: Routing + credential fields cleared when an ARN source is unusable, so no
#: stored/submitted routing value survives beside a (missing) secret-derived
#: credential. The secret's own values are the ONLY acceptable routing.
_ARN_SOURCE_FIELDS = ("base_url", "workspace_id", "key_scope", "table_label_prefix",
                      "token", "auth_scheme", "api_key", "api_secret")


def _arn_unresolved(out: dict, fields: tuple = _ARN_SOURCE_FIELDS) -> dict:
    """Mark an ARN source as a retryable miss AND strip every routing/credential
    field (``fields``: :data:`_ARN_SOURCE_FIELDS`), so an
    incomplete/unreadable/disallowed secret never leaves a caller or stored
    base_url/workspace in place. The pointer and the marker stay (the probe
    reports credential_unresolved)."""
    out["_secret_unresolved"] = True
    for key in fields:
        out.pop(key, None)
    return out


def _resolve_arn_source(out: dict, system_id: str, entry: dict | None,
                        node: dict | None) -> dict:
    """Finish :func:`registration_connection` for an ARN-sourced system.

    The pointer is the request-NAMED secret (the node's when provisioned, else
    the entry's). It is re-checked against the allowlist on every read (a deploy
    that removed the prefix stops resolving it), then its payload is merged and
    its PINNED fields (``connection_pinning.pinned_fields``) forced to WIN over
    any value the node/entry carried; the secret decides where the key is sent.
    An unreadable or now-disallowed secret is a retryable miss
    (``_secret_unresolved``), never a tokenless run.

    For Tulip (:func:`_apply_tulip_seat`) the
    secret must carry a COMPLETE, valid contract; the whole routing + credential
    tuple comes from the one read, and an incomplete contract is a retryable
    miss that leaves no registration routing beside the secret-derived
    credential. Fail-soft; never raises."""
    from . import connection_pinning as _cp
    from . import tulip_seat_contract as _tsc
    arn = str((node or {}).get("secret_arn") or (entry or {}).get("secret_arn") or "")
    out["secret_arn"] = arn
    out["credential_source"] = "secret_arn"
    out["_secret_unresolved"] = False
    fields = _ARN_SOURCE_FIELDS
    try:
        _cp.check_arn_allowed(arn)
    except _cp.SecretArnNotAllowedError as exc:
        logger.warning("resolve: %s: %s (named secret not allowed)", system_id, exc)
        return _arn_unresolved(out, fields)
    secret = _read_registration_secret(system_id, arn)
    if secret is None:
        return _arn_unresolved(out, fields)
    if out.get("connection_type") == _TULIP_CONNECTION_TYPE:
        return _apply_tulip_seat(out, secret, system_id)
    # Any other opted-in type (none today beyond Tulip) keeps the
    # generic merge.
    out.update(secret)                       # token/key/non-secret fields
    out.update(_cp.pinned_values(secret))    # the secret's url/workspace/key_scope WIN
    _tsc.with_derived_token(out)
    return out


def _apply_tulip_seat(out: dict, secret, system_id: str) -> dict:
    """Set ``out``'s Tulip routing and credential from ONE read of the named
    secret. A Tulip ARN source resolves its WHOLE routing + credential tuple
    through the ONE validator/constructor (``request_tuple``), the same one the
    provisioned resolver and the runtime use, so registration, provisioned app
    and runtime cannot drift. An incomplete/invalid contract is a retryable
    miss (no fallback to the stored/submitted routing value)."""
    from . import tulip_seat_contract as _tsc
    try:
        req = _tsc.request_tuple(secret)
    except _tsc.TulipContractError as exc:
        logger.warning("resolve: %s: %s (the named Tulip secret is not a complete "
                       "contract; unusable)", system_id, exc)
        return _arn_unresolved(out)
    out["base_url"] = req.base_url
    out["workspace_id"] = req.workspace_id
    out["key_scope"] = req.key_scope
    out["token"] = req.token
    out["auth_scheme"] = req.auth_scheme
    _set_table_scope(out, req)
    return out


def endpoint_identity(url) -> tuple | None:
    """What an endpoint URL names, as a comparable tuple ``(scheme, userinfo,
    host, port, path, query)``, or ``None`` for a non-URL. Scheme lower-case;
    host lower-case and IDNA-encoded, an IPv6 address kept as its own identity
    (never merged with a port); the EFFECTIVE port (the explicit one whenever
    ``port is not None``, so ``:0`` stays ``0``, else the scheme's default);
    the path exactly as given (``/mcp`` and ``/mcp/`` are different resources;
    an empty path is ``/``, RFC 3986 6.2.3); the query exactly. Only the
    fragment is ignored."""
    from urllib.parse import urlsplit
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if not parts.scheme or not host:
        return None
    scheme = parts.scheme.lower()
    if ":" not in host and not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else None
    return (scheme, userinfo, host.lower(),
            port if port is not None else _DEFAULT_PORTS.get(scheme),
            parts.path or "/", parts.query)


def same_endpoint(a, b) -> bool:
    """True iff ``a`` and ``b`` name the same endpoint (:func:`endpoint_identity`);
    two non-URLs compare as their stripped text."""
    ia, ib = endpoint_identity(a), endpoint_identity(b)
    if ia is None or ib is None:
        return ia is None and ib is None and str(a or "").strip() == str(b or "").strip()
    return ia == ib


def normalize_endpoint_url(url) -> str:
    """The canonical text of :func:`endpoint_identity` (the input, stripped,
    for a non-URL): an IPv6 host keeps its brackets, a port other than the
    scheme's default is kept (``:0`` included), the path and query are
    exact, the fragment is dropped. Two URLs normalise to the same text iff
    they have the same identity."""
    from urllib.parse import urlunsplit
    ident = endpoint_identity(url)
    if ident is None:
        return str(url or "").strip()
    scheme, userinfo, host, port, path, query = ident
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    if userinfo is not None:
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, path, query, ""))


def _dsn_schema(query: str) -> str:
    """The single schema a PostgreSQL URI's ``options=-csearch_path=<schema>``
    sets, else ``""``."""
    from urllib.parse import parse_qs
    for option in parse_qs(query or "").get("options", []):
        found = _SEARCH_PATH_OPTION.search(option)
        if found and _SQL_IDENTIFIER.fullmatch(found.group(1)):
            return found.group(1)
    return ""


def parse_pg_registration_dsn(dsn: str) -> tuple[dict, dict]:
    """Split a user-entered PostgreSQL URI into ``(non_secret, secret)``.

    ``non_secret`` = ``{host, port, dbname, schema}`` (``schema`` derived from
    the URI's ``options=-csearch_path=<schema>``); ``secret`` =
    ``{connection_string, user, password}`` for ``adt/conn/<id>``. Raises
    ``ValueError`` with a credential-free reason unless the URI is
    ``postgres(ql)://user:password@host[:port]/db?options=-csearch_path%3D<schema>``.
    A user and password are REQUIRED (without them the deployment's env
    credentials would be filled in and sent to the user's host).
    """
    from urllib.parse import unquote, urlsplit
    text = str(dsn or "").strip()
    if "://" not in text:
        raise ValueError("connection string must be a postgresql:// URI")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ValueError("connection string is not a valid URI") from exc
    if (parts.scheme or "").lower() not in _PG_SCHEMES:
        raise ValueError("connection string must use the postgresql:// scheme")
    if not parts.hostname:
        raise ValueError("connection string must name a host")
    user, password = unquote(parts.username or ""), unquote(parts.password or "")
    if not user or not password:
        raise ValueError("connection string must include a user and password")
    dbname = (parts.path or "").lstrip("/")
    if not dbname:
        raise ValueError("connection string must name a database")
    schema = _dsn_schema(parts.query)
    if not schema:
        raise ValueError("connection string must set one schema with "
                         "options=-csearch_path%3D<schema>")
    non_secret = {"host": parts.hostname, "port": str(port or 5432),
                  "dbname": dbname, "schema": schema}
    return non_secret, {"connection_string": text, "user": user, "password": password}


def registration_connection(system_id: str, entry: dict | None) -> dict:
    """``resolve_connection``-shaped dict for a USER registration (``{}`` for
    any other entry), valid before AND after a restart.

    Non-secret fields: the catalog entry (connection_type, schema,
    ``connection_string`` / ``connection_config``) overlaid by the graph node's
    config when a node exists (the node is authoritative once written; a
    restart rebuilds the entry without its config). The secret (token /
    external_id / the PostgreSQL DSN and credentials) is merged last, reached
    through :func:`own_registration_pointer` and accepted only when bound to
    this system (a PostgreSQL secret always; any other type when the pointer
    came from the entry and the node does not hold the same pointer — a node
    pointer is server-written). Never raises: an
    unreadable or unbound secret sets ``_secret_unresolved`` (the probe reports
    ``credential_unresolved``; ``pg_conn_params`` refuses; MCP / i3X / Tulip
    discover and provision return a retryable error).
    """
    if not is_user_registration(entry):
        return {}
    node = _node_connection(system_id)
    out: dict = {"system_id": system_id,
                 "connection_type": entry.get("connection_type") or node.get("connection_type") or None,
                 "connection_string": entry.get("connection_string") or node.get("connection_string") or ""}
    out.update(sanitize_connection_config(entry.get("connection_config") or {}))
    out.update({k: v for k, v in node.items()
                if k in _NODE_CONFIG_KEYS and k != "connection_string" and v not in (None, "")})
    if entry.get("schema"):
        out["schema"] = entry["schema"]
    if _is_arn_sourced(entry, node):
        # Credentials come from a request-NAMED allowlisted secret (not the
        # system's own adt/conn/<id>). Resolve url/workspace/key/token from it;
        # the secret's pinned fields WIN over the node config (apply_pins).
        return _resolve_arn_source(out, system_id, entry, node)
    arn = own_registration_pointer(system_id, entry, node)
    out["secret_arn"] = arn
    out["_secret_unresolved"] = False
    if arn:
        # A pointer the node also holds is server-written (provision, or an
        # admin rotation through write_connection), so a non-PostgreSQL payload
        # there needs no binding; an entry-only pointer does.
        entry_arn = str(entry.get("secret_arn") or "")
        need_binding = (out["connection_type"] == "PostgreSQL"
                        or (arn == entry_arn and arn != node.get("secret_arn")))
        secret = _read_registration_secret(system_id, arn)
        if secret is None or (need_binding and not is_bound_user_secret(secret, system_id)):
            out["_secret_unresolved"] = True
        else:
            out.update(secret)
    if (out["connection_type"] == "MCP Server" and out.get("token")
            and not out.get("auth_scheme")):
        out["auth_scheme"] = user_mcp_default_scheme(out["token"])
    return out


def user_mcp_default_scheme(token) -> str:
    """The auth scheme a USER-registered MCP token is sent with when the
    system has none of its own (the registration stores only the token):
    ``bearer`` (``Authorization: Bearer <token>``), or ``bearer_literal`` (the
    value as stored) when the token already starts with ``Bearer `` — never
    ``Bearer Bearer``. HighByte, demo systems and a scheme already set on the
    node or by an admin Edit never get here."""
    return "bearer_literal" if str(token or "").lower().startswith("bearer ") else "bearer"


def _read_registration_secret(system_id: str, arn: str):
    """The secret at ``arn`` (TTL-cached), or ``None`` when unreadable (logged
    by exception TYPE only — the message could echo values)."""
    try:
        return _fetch_secret(arn)
    except Exception as exc:  # noqa: BLE001 - fail-soft; surfaced as credential_unresolved
        logger.warning("registration_connection: %s: %s while fetching the connection secret",  # logs the exception type only, never a value
                       system_id, type(exc).__name__)
        return None


def _unavailable(system_id: str, why: str):
    from .errors import ConnectionSecretUnavailableError
    return ConnectionSecretUnavailableError(f"the connection secret for {system_id} {why}")


#: Recovery window for a removed user registration's own secret (Secrets
#: Manager accepts 7-30 days). Deletion is always scheduled with a recovery
#: window, never ``ForceDeleteWithoutRecovery``.
OWN_SECRET_RECOVERY_DAYS = 7

#: Never scheduled for deletion by an offboard, whatever the registration says:
#: the deployment's HighByte token secret (``adt/conn/highbyte``).
_NEVER_DELETED_SYSTEM_IDS = frozenset({"highbyte"})


class OwnSecretDeletionError(RuntimeError):
    """A full removal could not verify or schedule deletion of a USER
    registration's own secret. Raised BEFORE the registration's graph node and
    catalog entry (the only record of the pointer and its ownership) are
    removed, so the caller keeps the registration and the removal is retried.
    The message carries exception TYPES only."""


def _sm_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return type(exc).__name__


def _secret_gone(exc: Exception) -> bool:
    return _sm_error_code(exc) == "ResourceNotFoundException"


def _secret_pending_deletion(exc: Exception) -> bool:
    """Secrets Manager refuses a read or a second delete of a secret already
    scheduled for deletion with InvalidRequestException ("...marked for
    deletion"): an earlier attempt of this removal already did the work."""
    return (_sm_error_code(exc) == "InvalidRequestException"
            and "deletion" in str(exc).lower())


def delete_own_secret_before_removal(system_id: str, entry: dict | None) -> str:
    """Schedule deletion of a USER registration's own ``adt/conn/<id>``
    secret for a FULL removal, called BEFORE the registration's node and
    catalog entry go (they are the only record of the pointer).

    Only a :func:`is_user_registration` entry, never HighByte, only the exact
    own pointer (the entry's, else the node's, read with a CHECKED graph
    read), only a payload bound to this user system
    (:func:`is_bound_user_secret`). A demo, workshop, HighByte, legacy or
    unbound secret, another system's secret, a secret in another account and
    a secret named by anything but its pointer are never touched.
    ``DeleteSecret`` with the :data:`OWN_SECRET_RECOVERY_DAYS` recovery window
    (never ``ForceDeleteWithoutRecovery``). Returns the ARN scheduled (also
    when an earlier attempt already scheduled it), or ``""`` when there is
    nothing of this registration's to delete (left in place, logged). Raises
    :class:`OwnSecretDeletionError` when the graph, the account check, the
    secret read or the deletion fails, so the removal stops with the
    registration intact (fail closed, retryable)."""
    if not is_user_registration(entry) or system_id in _NEVER_DELETED_SYSTEM_IDS:
        return ""
    arn = _own_pointer_for_removal(system_id, entry)
    if not arn:
        return ""
    payload = _own_payload_for_removal(system_id, arn)
    if payload is _PENDING_DELETION:
        return arn
    if not is_bound_user_secret(payload, system_id):
        logger.warning("offboard %s: own secret is not bound to this user registration "
                       "(or is gone / in another account); left in place", system_id)
        return ""
    try:
        _secrets_client().delete_secret(SecretId=arn,
                                        RecoveryWindowInDays=OWN_SECRET_RECOVERY_DAYS)
    except Exception as exc:  # noqa: BLE001 - classified below
        if _secret_gone(exc):
            return ""
        if not _secret_pending_deletion(exc):
            raise OwnSecretDeletionError(
                f"its deletion could not be scheduled ({type(exc).__name__})") from exc
    invalidate_secret(arn)
    logger.info("offboard %s: own secret scheduled for deletion (recovery window %d days)",
                system_id, OWN_SECRET_RECOVERY_DAYS)
    return arn


#: :func:`_own_payload_for_removal`'s answer for a secret an earlier attempt
#: already scheduled for deletion.
_PENDING_DELETION = object()


def _own_pointer_for_removal(system_id: str, entry: dict | None) -> str:
    """The exact own pointer: the entry's, else the node's (a CHECKED read: a
    graph failure raises :class:`OwnSecretDeletionError`), else ``""``."""
    arn = str((entry or {}).get("secret_arn") or "")
    if is_own_connection_secret(arn, system_id):
        return arn
    try:
        arn = node_secret_arn_checked(system_id)
    except Exception as exc:  # noqa: BLE001 - re-raised: the pointer may only be there
        raise OwnSecretDeletionError(
            f"the graph could not be read ({type(exc).__name__})") from exc
    return arn if is_own_connection_secret(arn, system_id) else ""


def _own_payload_for_removal(system_id: str, arn: str):
    """The secret's payload; ``None`` when it is gone or in another account
    (not this deployment's to delete); :data:`_PENDING_DELETION` when already
    scheduled. Raises :class:`OwnSecretDeletionError` on any other failure,
    including an unknown deployment account (retryable)."""
    try:
        return _fetch_secret(arn)
    except AccountUnknownSecretError as exc:
        raise OwnSecretDeletionError("the deployment account could not be determined") from exc
    except ForeignAccountSecretError:
        return None
    except Exception as exc:  # noqa: BLE001 - classified below
        if _secret_gone(exc):
            return None
        if _secret_pending_deletion(exc):
            return _PENDING_DELETION
        raise OwnSecretDeletionError(
            f"the secret could not be read ({type(exc).__name__})") from exc


def registered_pg_dsn(system_id: str, entry: dict | None) -> tuple[str, str]:
    """``(dsn, secret_arn)`` of the PostgreSQL connection a USER registered.

    Call only for a :func:`is_user_registration` entry. The pointer follows
    :func:`own_registration_pointer` (the entry's, else the graph node's), its
    payload must be bound to this system, and its DSN must pass the full
    registration validation. Anything else raises
    :class:`~agents.errors.ConnectionSecretUnavailableError` — never a fallback to
    the demo routing, which would silently repoint the user's system.
    """
    arn = own_registration_pointer(system_id, entry)
    if not arn:
        raise _unavailable(system_id, "is not this system's own secret")
    try:
        secret = _fetch_secret(arn)
    except Exception as exc:  # noqa: BLE001 - the message is dropped (it could echo values)
        raise _unavailable(system_id, f"is unreadable ({type(exc).__name__})") from None
    if not is_bound_user_secret(secret, system_id):
        raise _unavailable(system_id, "is not bound to this system")
    dsn = str(secret.get("connection_string") or "")
    try:
        parse_pg_registration_dsn(dsn)
    except ValueError as exc:
        raise _unavailable(system_id, f"holds an unusable connection string ({exc})") from None
    return dsn, arn


def pg_params_from_dsn(dsn: str) -> dict:
    """Connect params ``{host, port, dbname, user, password}`` from a PostgreSQL URI —
    the system agent's ``conn_params`` shape (runtime_app pops exactly these)."""
    from .pgdriver import parse_dsn
    parsed = parse_dsn(dsn)
    return {k: parsed[k] for k in ("host", "port", "dbname", "user", "password")
            if parsed.get(k)}
