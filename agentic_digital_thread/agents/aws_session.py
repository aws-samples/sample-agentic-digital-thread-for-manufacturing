# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic, connector-agnostic cross-account AWS credential abstraction.

A SINGLE shared helper reused by every AWS-native connector (Athena, SiteWise
Direct, ...). Cross-account access is entirely OPTIONAL and driven by three
optional keys on a connector's ``connection_config`` — there is no rigid schema
and nothing connector-specific here:

    {
      "role_arn":    "arn:aws:iam::<account>:role/<name>",  # optional
      "external_id": "<sts external id>",                    # optional
      "region":      "us-east-1"                              # optional
    }

Behavior:

* **No ``role_arn`` (absent/blank)** — return an *ambient* boto3 client/session
  using the default credential chain (task role in the cloud, the developer's
  profile locally). This is the historical behavior and is UNCHANGED.
* **``role_arn`` set** — validate it looks like an IAM *role* ARN, then STS
  ``AssumeRole`` into it (passing ``ExternalId`` only when one is supplied) and
  return a client/session bound to the resulting TEMPORARY credentials. The
  credentials are wrapped in botocore :class:`RefreshableCredentials` so
  long-lived agents transparently re-assume the role on expiry — we do NOT
  cache a static access key. The assembled *session* is cached per
  ``(role_arn, external_id, region)`` and reused so a burst of backend calls
  shares one auto-refreshing session instead of one AssumeRole per call.

Region precedence: ``connection_config.region`` → ``default_region`` argument →
``AWS_REGION`` env → ``us-east-1``.

Security: ``external_id`` is treated as a secret and is NEVER logged. ``role_arn``
and ``region`` are safe to log and are logged at INFO for debuggability.

Fail-closed: a malformed ``role_arn`` or a failed ``AssumeRole`` raises
:class:`AwsSessionError` with an actionable message rather than silently falling
back to ambient credentials.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import uuid

import boto3
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session as _get_botocore_session

logger = logging.getLogger(__name__)

# An IAM *role* ARN. Deliberately strict: 12-digit account id and a non-empty
# role path/name. Rejects user ARNs, service ARNs, and truncated account ids.
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/.+$")

_DEFAULT_REGION = "us-east-1"

# --- Assumed-session cache (cost control) ------------------------------------
# Every assumed-role ``session_for`` call eagerly STS ``AssumeRole``s and builds a
# fresh :class:`RefreshableCredentials`-backed session. Callers build a client
# PER backend call (e.g. every SiteWise ``get_asset_property_value`` during a
# dashboard resolve), so without a cache a single card refresh triggers many
# AssumeRoles — hundreds of STS calls in minutes for a live dashboard. Cache the
# assembled session keyed by ``(role_arn, external_id, region)`` and reuse it: the
# wrapped ``RefreshableCredentials`` transparently RE-ASSUMES on expiry, so a
# reused session stays valid across the credential lifetime (≈1 AssumeRole/hour/
# role instead of one per call) with NO change to the security posture — creds
# still rotate, ``external_id`` is still required, and a malformed ARN still fails
# closed. Thread-safe (resolves run on the scheduler threadpool + rederive daemon
# + request handlers). The ambient (no-``role_arn``) path is NOT cached — it is a
# cheap local client build with no STS call.
_session_cache: dict[tuple[str, str, str], "boto3.Session"] = {}
_session_cache_lock = threading.Lock()


def _reset_session_cache() -> None:
    """Drop all cached assumed-role sessions (test seam / force a re-assume).

    Used by the unit lane to isolate tests and available operationally to force a
    fresh AssumeRole on the next call (e.g. after a role/trust-policy change)."""
    with _session_cache_lock:
        _session_cache.clear()


class AwsSessionError(RuntimeError):
    """Raised on a malformed cross-account ``role_arn`` or a failed AssumeRole.

    Fail-closed: callers get a clear, actionable error instead of an unexpected
    silent fallback to ambient credentials.
    """


def _clean(value) -> str:
    """Coerce an optional config value to a stripped string ('' when missing)."""
    return str(value or "").strip()


def resolve_region(connection_config: dict | None, default_region: str | None = None) -> str:
    """Resolve the effective region using the documented precedence.

    ``connection_config.region`` → ``default_region`` → ``AWS_REGION`` env →
    ``us-east-1``.
    """
    cfg = connection_config or {}
    return (
        _clean(cfg.get("region"))
        or _clean(default_region)
        or _clean(os.environ.get("AWS_REGION"))
        or _DEFAULT_REGION
    )


def _session_name(service: str) -> str:
    """Build an STS ``RoleSessionName`` like ``adt-<service>-<short>``.

    Bounded well under the 64-char STS limit and within the allowed charset.
    """
    short = uuid.uuid4().hex[:8]
    svc = re.sub(r"[^A-Za-z0-9]+", "-", service or "svc")[:20].strip("-") or "svc"
    return f"adt-{svc}-{short}"


def _assume_role_fetcher(role_arn: str, external_id: str | None, region: str, service: str):
    """Return a zero-arg callable that assumes ``role_arn`` and yields temp creds.

    The SINGLE assume-role implementation (DRY): used both for the eager initial
    fetch and as the ``refresh_using`` callback for
    :class:`RefreshableCredentials`, so expiry triggers a fresh AssumeRole.

    ``external_id`` is passed to STS only when provided and is NEVER logged.
    """
    sts = boto3.client("sts", region_name=region)

    def _fetch() -> dict:
        kwargs = {"RoleArn": role_arn, "RoleSessionName": _session_name(service)}
        if external_id:
            kwargs["ExternalId"] = external_id
        resp = sts.assume_role(**kwargs)
        creds = resp["Credentials"]
        expiration = creds["Expiration"]
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": expiration.isoformat() if hasattr(expiration, "isoformat") else expiration,
        }

    return _fetch


def session_for(
    connection_config: dict | None,
    default_region: str | None = None,
    service: str = "adt",
) -> boto3.Session:
    """Return a :class:`boto3.Session` for ``connection_config``.

    Ambient session (default credential chain) when no ``role_arn`` is set;
    otherwise an assumed-role session backed by auto-refreshing temporary
    credentials. See the module docstring for the full contract.
    """
    cfg = connection_config or {}
    region = resolve_region(cfg, default_region)
    role_arn = _clean(cfg.get("role_arn"))

    if not role_arn:
        return boto3.Session(region_name=region)

    if not _ROLE_ARN_RE.match(role_arn):
        raise AwsSessionError(
            f"connection_config.role_arn is not a valid IAM role ARN: {role_arn!r} "
            f"(expected 'arn:aws:iam::<12-digit-account>:role/<name>')"
        )

    external_id = _clean(cfg.get("external_id")) or None

    # Reuse a cached assumed session when one exists for this exact identity —
    # the wrapped RefreshableCredentials keep it valid (auto re-assume on expiry),
    # so we avoid a fresh STS AssumeRole on every backend call (cost control).
    cache_key = (role_arn, external_id or "", region)
    with _session_cache_lock:
        cached = _session_cache.get(cache_key)
    if cached is not None:
        logger.debug(
            "Reusing cached cross-account session for service=%s role_arn=%s region=%s",
            service, role_arn, region,
        )
        return cached

    # role_arn/region are safe to log; external_id is a secret and is omitted.
    logger.info(
        "Assuming cross-account role for service=%s role_arn=%s region=%s external_id=%s",
        service, role_arn, region, "provided" if external_id else "none",
    )

    fetcher = _assume_role_fetcher(role_arn, external_id, region, service)
    try:
        metadata = fetcher()  # eager: fail closed now if AssumeRole is not permitted
    except Exception as e:  # noqa: BLE001 - normalize any STS/client error to a clear failure
        raise AwsSessionError(
            f"AssumeRole failed for role_arn={role_arn} (region={region}): {e}"
        ) from e

    refreshable = RefreshableCredentials.create_from_metadata(
        metadata=metadata,
        refresh_using=fetcher,
        method="sts-assume-role",
    )
    botocore_session = _get_botocore_session()
    botocore_session._credentials = refreshable
    botocore_session.set_config_variable("region", region)
    session = boto3.Session(botocore_session=botocore_session, region_name=region)

    # Cache for reuse; if another thread populated it while we were assuming,
    # prefer the already-cached session so all callers converge on one.
    with _session_cache_lock:
        existing = _session_cache.get(cache_key)
        if existing is not None:
            return existing
        _session_cache[cache_key] = session
    return session


def boto3_client_for(
    service: str,
    connection_config: dict | None,
    default_region: str | None = None,
    config=None,
):
    """Return a boto3 ``service`` client, cross-account when configured.

    Thin wrapper over :func:`session_for`. When no ``role_arn`` is present this
    is equivalent to ``boto3.client(service, region_name=<resolved region>)`` —
    identical to the pre-existing ambient behavior. ``config`` (a
    ``botocore.config.Config``) is passed to the client when given, for a
    caller that needs its own timeouts or retries; without it the client is
    built exactly as before.
    """
    cfg = connection_config or {}
    region = resolve_region(cfg, default_region)
    role_arn = _clean(cfg.get("role_arn"))
    extra = {} if config is None else {"config": config}

    if not role_arn:
        return boto3.client(service, region_name=region, **extra)

    session = session_for(cfg, default_region=default_region, service=service)
    return session.client(service, region_name=region, **extra)


# ── bedrock-runtime client (the ONE construction site) ───────────────────────
#
# Six modules used to build their own ``boto3.client("bedrock-runtime")`` with
# hand-rolled (and inconsistent) regions and timeouts — ``prompts.py`` even
# pinned ``us-east-1``. Model SELECTION stays in ``model_provider`` /
# ``model_catalog``; this only owns transport: region resolution, connect/read
# timeouts and botocore's standard retry mode. ``audit_core`` keeps its own
# lazy client on purpose (it is pinned import-pure: no intra-package imports).
# Pinned by ``tests/unit/test_bedrock_runtime_client.py``.

BEDROCK_RUNTIME_READ_TIMEOUT_S = 120
BEDROCK_RUNTIME_CONNECT_TIMEOUT_S = 10
BEDROCK_RUNTIME_MAX_ATTEMPTS = 3


def bedrock_runtime_client(region: str | None = None, *,
                           read_timeout: int = BEDROCK_RUNTIME_READ_TIMEOUT_S):
    """A ``bedrock-runtime`` client for *region* (default ``AWS_REGION`` env,
    then ``us-east-1``) with the shared connect/read timeouts and standard
    retries. Streaming callers may pass a longer *read_timeout*."""
    from botocore.config import Config  # lazy: keeps module import cheap
    resolved = _clean(region) or _clean(os.environ.get("AWS_REGION")) or _DEFAULT_REGION
    return boto3.client(
        "bedrock-runtime", region_name=resolved,
        config=Config(
            read_timeout=read_timeout,
            connect_timeout=BEDROCK_RUNTIME_CONNECT_TIMEOUT_S,
            retries={"max_attempts": BEDROCK_RUNTIME_MAX_ATTEMPTS, "mode": "standard"},
        ),
    )


# --- Lambda client (direct RequestResponse invokes) ----------------------------
# A synchronous invoke is NOT idempotent in general (the story clock placement
# re-plans the era), so the default is ONE attempt: a botocore retry after a
# read timeout would run the action a second time.

LAMBDA_READ_TIMEOUT_S = 90
LAMBDA_CONNECT_TIMEOUT_S = 10
LAMBDA_MAX_ATTEMPTS = 1


def lambda_client(region: str | None = None, *, read_timeout: int = LAMBDA_READ_TIMEOUT_S):
    """A ``lambda`` client for *region* (default ``AWS_REGION`` env, then
    ``us-east-1``) with the shared connect timeout, *read_timeout* and ONE
    attempt (no retry of a non-idempotent synchronous invoke)."""
    from botocore.config import Config  # lazy: keeps module import cheap
    resolved = _clean(region) or _clean(os.environ.get("AWS_REGION")) or _DEFAULT_REGION
    return boto3.client(
        "lambda", region_name=resolved,
        config=Config(
            read_timeout=read_timeout,
            connect_timeout=LAMBDA_CONNECT_TIMEOUT_S,
            retries={"max_attempts": LAMBDA_MAX_ATTEMPTS, "mode": "standard"},
        ),
    )
