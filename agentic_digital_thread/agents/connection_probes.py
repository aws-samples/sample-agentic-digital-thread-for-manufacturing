# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live connectivity probes for connection management.

Extracted from ``connection_resolver`` (behavior-preserving split). Owns the
per-type probes (:func:`_probe_postgres`/``_probe_mcp``/``_probe_rest``/
``_probe_sitewise``), the validated-IP pinning helper :func:`_pin_host_to_ip`,
the coarse outcome enum (:data:`_REASON_DETAIL`), the failure classifier
:func:`_classify_exception`, and the :func:`probe_connection` dispatcher. The
SSRF guard and secret scrubbing are imported from ``connection_security``. These
names are re-exported by ``connection_resolver`` so existing import sites keep
working unchanged. See ADR 0031.
"""

import logging
import os
import time
from urllib.parse import urlsplit, urlunsplit

from .connection_security import (
    ConnectionTargetError,
    _safe_detail,
    validate_connection_target,
)
from .errors import MissingDbPasswordError

logger = logging.getLogger(__name__)

# Coarse, enumerated probe outcome reasons returned to callers (no host/IP/port).
# The verbose underlying detail is logged server-side only — see probe_connection.
_REASON_DETAIL = {
    "ok": "connection ok",
    "connection_refused": "connection refused",
    "timeout": "connection timed out",
    "auth_failed": "authentication failed",
    "credential_unresolved": "configured credential could not be resolved",
    "assume_role_denied": (
        "ADT's deploy has not granted sts:AssumeRole for this role (or the "
        "data-account trust/ExternalId rejects it). Redeploy once with "
        "--cross-account-role convention, or add this role ARN to the explicit "
        "grant list. See docs/cross-account-onboarding.md"
        "#dynamic-onboarding-what-must-exist-ahead-of-time-deploy-once-checklist"
    ),
    "blocked_target": "connection target blocked by SSRF/scheme policy",
    "request_rejected": ("reached and signed in, but the system rejected the "
                         "request (check the site or target setting)"),
    "invalid_config": "the base URL or target setting is not valid",
    "unreachable": "host unreachable",
    "error": "connection failed",
}


class CredentialUnresolvedError(Exception):
    """Raised internally when a configured secret could not be resolved.

    Surfaced to callers only as the coarse ``credential_unresolved`` reason; the
    underlying secret-fetch exception is logged server-side, never returned.
    """


class AssumeRoleDeniedError(Exception):
    """Raised internally when the cross-account STS AssumeRole PREFLIGHT is denied.

    Surfaced to callers only as the coarse, actionable ``assume_role_denied``
    reason (see :data:`_REASON_DETAIL`) — the underlying STS exception (which may
    carry the role ARN) is logged server-side, never returned.
    """


def _preflight_assume_role(resolved: dict) -> None:
    """STS AssumeRole PREFLIGHT for cross-account systems (no-op otherwise).

    When the resolved config carries a ``role_arn`` (top-level after
    ``resolve_connection``'s flatten, or nested under ``connection_config`` for
    callers passing the raw shape), verify the caller can actually assume the
    role BEFORE running the type-specific probe — reusing the single assume
    implementation in :mod:`agents.aws_session` (``session_for`` eagerly assumes
    and fails closed). Without this, a missing caller-side ``sts:AssumeRole``
    grant (the ADT deploy never granted it — see ``stacks/_cross_account.py``)
    surfaces as a generic, unexplained probe failure.

    Classification contract:
      * AccessDenied on ``sts:AssumeRole`` → :class:`AssumeRoleDeniedError`
        (→ coarse ``assume_role_denied`` reason with an actionable detail).
      * Any other STS/session error (throttle, malformed ARN, ...) propagates
        unchanged → existing generic failure semantics; no new reasons.
      * No ``role_arn`` → returns immediately (behavior identical to today).
    """
    cfg = resolved.get("connection_config") if isinstance(resolved.get("connection_config"), dict) else {}
    role_arn = str(resolved.get("role_arn") or cfg.get("role_arn") or "").strip()
    if not role_arn:
        return
    # Reuse the shared cross-account machinery (DRY): session_for eagerly
    # AssumeRoles (fail-closed) and caches the session per identity, so a
    # successful preflight costs at most one STS call per credential lifetime.
    from .aws_session import AwsSessionError, session_for

    conn_cfg = {
        "role_arn": role_arn,
        "external_id": resolved.get("external_id") or cfg.get("external_id") or "",
        "region": resolved.get("region") or cfg.get("region") or "",
    }
    try:
        session_for(conn_cfg, service="probe")
    except AwsSessionError as exc:
        # AwsSessionError wraps the original botocore ClientError (``from e``);
        # classify AccessDenied on the AssumeRole call as the dedicated reason.
        cause = exc.__cause__
        code = ""
        if cause is not None:
            code = str((getattr(cause, "response", None) or {}).get("Error", {}).get("Code", "") or "")
        if code in ("AccessDenied", "AccessDeniedException"):
            raise AssumeRoleDeniedError(str(exc)) from exc
        raise


def _normalize_type(connection_type: str, resolved: dict | None = None) -> str:
    """Map a connection type (+ optional resolved config) to a probe key.

    SiteWise is ambiguous: the catalog ships TWO flavors —
      * ``SiteWise (Direct)`` / ``iotsitewise://...`` — the native AWS API, probed
        with boto3 ``list_asset_models`` (→ ``"sitewise"``).
      * ``SiteWise (i3X)`` — an HTTP/i3X REST API (OpenAPI spec + ``base_url``),
        which must use the REST probe (→ ``"rest"``), NOT boto3.

    Routing rule: choose the boto3 SiteWise probe ONLY for the DIRECT flavor —
    i.e. the type says "direct", or the connection_string uses the
    ``iotsitewise://`` scheme. Anything that is i3X (type contains "i3x") or that
    carries an HTTP(S) ``base_url``/``connection_string`` is treated as REST.
    """
    ct = (connection_type or "").lower()
    if "postgres" in ct:
        return "postgres"
    if "athena" in ct:
        return "athena"
    if "mcp" in ct:
        return "mcp"
    if "sitewise" in ct or "iotsitewise" in ct:
        resolved = resolved or {}
        base_url = str(resolved.get("base_url", "") or "")
        conn_str = str(resolved.get("connection_string", "") or "")
        is_direct = (
            "direct" in ct
            or conn_str.lower().startswith("iotsitewise://")
        )
        is_i3x_or_http = (
            "i3x" in ct
            or base_url.lower().startswith(("http://", "https://"))
            or conn_str.lower().startswith(("http://", "https://"))
        )
        # Direct wins only when it is NOT clearly an HTTP/i3X target.
        if is_direct and not is_i3x_or_http:
            return "sitewise"
        if is_i3x_or_http:
            return "rest"
        # Fallback: a bare iotsitewise type with no HTTP markers → boto3 Direct.
        return "sitewise"
    if "rest" in ct or "openapi" in ct or "api" in ct:
        return "rest"
    return ct


def _mcp_auth_headers(token: str, auth_scheme: str) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not token:
        return headers
    if auth_scheme == "bearer_literal":
        headers["Authorization"] = token
    elif auth_scheme == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif auth_scheme == "api_key":
        headers["X-API-Key"] = token
    elif auth_scheme == "basic":
        # token is the pre-encoded base64 of ``apiKey:secret`` (HTTP Basic).
        headers["Authorization"] = f"Basic {token}"
    return headers


def _pin_host_to_ip(url: str, ip: str) -> tuple[str, str, str]:
    """Rewrite ``url`` so the socket connects to the validated ``ip`` while the
    Host header + TLS SNI still carry the ORIGINAL hostname.

    Returns ``(pinned_url, host_header, sni_host)``:
      * ``pinned_url`` — same scheme/port/path/query, host replaced by ``ip``
        (bracketed for IPv6), so httpx connects straight to the validated address
        and performs NO second DNS lookup — this closes the DNS-rebinding TOCTOU
        window for the arbitrary-URL Update endpoint.
      * ``host_header`` — original ``host[:port]`` (userinfo stripped) for the HTTP
        ``Host`` header so virtual-host routing still works.
      * ``sni_host`` — original hostname for the httpx ``sni_hostname`` request
        extension, so the TLS handshake/cert validation still targets the real
        hostname rather than the bare IP.
    """
    parts = urlsplit(url)
    sni_host = parts.hostname or ""
    netloc = parts.netloc
    if "@" in netloc:  # strip any userinfo (through the last '@') from the Host header
        netloc = netloc.rsplit("@", 1)[1]
    host_header = netloc
    ip_host = f"[{ip}]" if ":" in ip else ip
    new_netloc = f"{ip_host}:{parts.port}" if parts.port is not None else ip_host
    pinned_url = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    return pinned_url, host_header, sni_host


def _pg_node_params(resolved: dict) -> dict:
    """Connect kwargs for a PostgreSQL system with no DSN. A provisioned node
    keeps host/port/dbname and a secret pointer, no DSN (req6c), so assemble
    the params its agents connect with (``pg_conn_params``), passing the
    catalog entry so a user registration keeps its env-fill refusal. A system
    not yet provisioned takes its schema from the entry, as preflight does, so
    the env fill routes to that schema's database."""
    from .catalog import catalog  # lazy: connection_resolver imports this module
    from .connection_resolver import pg_conn_params
    entry = catalog.get(str(resolved.get("system_id") or "")) or {}
    if not (resolved.get("search_path") or resolved.get("schema")) and entry.get("schema"):
        resolved = {**resolved, "schema": entry["schema"]}
    params = pg_conn_params(resolved, entry=entry)
    if not params.get("host"):
        raise RuntimeError("no connection string configured")
    return params


def _probe_postgres(resolved: dict) -> str:
    conn_str = resolved.get("connection_string", "")
    params = {} if conn_str else _pg_node_params(resolved)
    pinned_ip = validate_connection_target(conn_str or str(params["host"]),
                                           connection_type="PostgreSQL")
    from . import pgdriver
    # Pin the validated IP to the SOCKET via ``hostaddr`` while keeping the
    # original ``host`` (DSN or params) for TLS/ident — this closes the
    # DNS-rebinding TOCTOU window for PG (the name cannot re-resolve to a denied
    # address between validation and connect because we connect to the IP we
    # validated). pgdriver merges these kwargs over the DSN.
    pin = {"hostaddr": pinned_ip} if pinned_ip else {}
    conn = pgdriver.connect(conn_str, **pin) if conn_str \
        else pgdriver.connect(**params, **pin)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
    finally:
        conn.close()
    return "SELECT 1 ok"


def _probe_mcp(resolved: dict) -> str:
    url = resolved.get("url", "") or resolved.get("connection_string", "")
    if not url:
        raise RuntimeError("no MCP server URL configured")
    pinned_ip = validate_connection_target(url, connection_type="MCP Server")
    # Pin the connection to the validated IP while preserving the original Host
    # header + TLS SNI — closes the DNS-rebinding TOCTOU window the arbitrary-URL
    # Update endpoint now exposes (the name cannot re-resolve to a denied address
    # between validation and connect because we connect to the IP we validated).
    pinned_url, host_header, sni_host = _pin_host_to_ip(url, pinned_ip)
    extensions = {"sni_hostname": sni_host} if sni_host else {}
    headers = _mcp_auth_headers(resolved.get("token", ""), resolved.get("auth_scheme", "bearer_literal"))
    headers["Host"] = host_header
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "adt-probe", "version": "1.0"}}}
    import httpx
    # Simple HTTP test — avoids asyncio.run() hanging on auth failures. Never
    # follow redirects (a 30x to the metadata endpoint would bypass the guard).
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        r = client.post(pinned_url, json=init, headers=headers, extensions=extensions)
        if r.status_code == 401:
            raise RuntimeError("authentication failed (401) — check token")
        r.raise_for_status()
        session_id = r.headers.get("mcp-session-id", "")
        sh = {**headers, "mcp-session-id": session_id} if session_id else dict(headers)
        r2 = client.post(pinned_url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                         headers=sh, extensions=extensions)
        r2.raise_for_status()
        tools = r2.json().get("result", {}).get("tools", [])
    return f"{len(tools)} tool(s) discovered"


def _health_url(resolved: dict) -> str:
    """Build the health-check URL for a REST probe.

    Prefer ``base_url`` for the health path. When ``base_url`` is absent, derive
    the ORIGIN (``scheme://host[:port]``) from ``connection_string`` rather than
    appending to the full string — the connection_string is frequently an OpenAPI
    spec URL (e.g. ``https://x/openapi.json``), so naive appending would yield an
    invalid ``https://x/openapi.json/health``. The health path defaults to
    ``/health`` and is overridable via the ``health_path`` field.
    """
    base_url = (resolved.get("base_url", "") or "").rstrip("/")
    if not base_url:
        conn_str = resolved.get("connection_string", "") or ""
        if not conn_str:
            raise RuntimeError("no base URL configured")
        parts = urlsplit(conn_str)
        if not parts.scheme or not parts.netloc:
            raise RuntimeError("no base URL configured")
        base_url = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    health_path = resolved.get("health_path", "/health") or "/health"
    return f"{base_url}/{health_path.lstrip('/')}"


def _probe_rest(resolved: dict) -> str:
    url = _health_url(resolved)
    pinned_ip = validate_connection_target(url, connection_type="REST API")
    token = resolved.get("token", "")
    headers = {}
    if token:
        scheme = resolved.get("auth_scheme", "bearer")
        if scheme == "bearer_literal":
            headers["Authorization"] = token
        elif scheme == "api_key":
            headers["X-API-Key"] = token
        elif scheme == "basic":
            # token is the pre-encoded base64 of ``apiKey:secret`` (HTTP Basic).
            headers["Authorization"] = f"Basic {token}"
        else:  # "bearer" (default)
            headers["Authorization"] = f"Bearer {token}"
    # Pin the connection to the validated IP while preserving the original Host
    # header + TLS SNI — closes the DNS-rebinding TOCTOU window the arbitrary-URL
    # Update endpoint now exposes. Never follow redirects — a 30x to the metadata
    # endpoint would bypass the SSRF guard, which only validated the original target.
    pinned_url, host_header, sni_host = _pin_host_to_ip(url, pinned_ip)
    headers["Host"] = host_header
    extensions = {"sni_hostname": sni_host} if sni_host else {}
    import httpx
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        r = client.get(pinned_url, headers=headers, extensions=extensions)
        r.raise_for_status()
    return f"HTTP {r.status_code}"


def sitewise_connection_url(region: str) -> str:
    """Build a SiteWise (Direct) connection URL with a RESOLVABLE host.

    The scheme is ``iotsitewise`` (allowlisted) and the host is the REAL AWS
    service endpoint ``iotsitewise.<region>.amazonaws.com`` — NOT the bare
    ``<region>.amazonaws.com``, which does not resolve in DNS and made the
    SSRF/scheme guard's DNS-resolution step reject the target (the same class of
    bug the Athena probe hit, fixed the same way: ``athena.<region>.amazonaws.com``).
    ``region`` falls back to ``us-east-1`` when empty. Pure — safe offline."""
    region = (region or "").strip() or "us-east-1"
    return f"iotsitewise://iotsitewise.{region}.amazonaws.com"


def _probe_sitewise(resolved: dict) -> str:
    region = os.environ.get("AWS_REGION", "us-east-1")
    target = resolved.get("connection_string", "") or sitewise_connection_url(region)
    validate_connection_target(target, connection_type="SiteWise")
    resp = _probe_client(resolved, "iotsitewise", region).list_asset_models(maxResults=1)
    return f"{len(resp.get('assetModelSummaries', []))} asset model(s)"


def _probe_client(resolved: dict, service: str, region: str):
    """Boto3 client for a probe — CROSS-ACCOUNT-AWARE: assumed-role session
    when the resolved config carries role_arn (the identity the agents will
    actually use), ambient otherwise. Live defect 2026-07-26: probes built
    plain boto3 clients with the APP task's credentials AFTER the assume-role
    preflight passed, probing the WRONG account and reporting a false
    blocked verdict for healthy cross-account systems."""
    from .aws_session import session_for
    cfg = dict(resolved.get("connection_config") or {})
    for k in ("role_arn", "external_id", "region"):
        if resolved.get(k) and not cfg.get(k):
            cfg[k] = resolved[k]
    return session_for(cfg, default_region=region, service="probe").client(
        service, region_name=region)


def _probe_athena(resolved: dict) -> str:
    """Cheap, scan-free Athena reachability check: describe the configured workgroup.

    Uses ``get_work_group`` (metadata only — never runs a data query, so it incurs
    no Athena scan cost). Validates a synthetic ``athena://<region>`` target first
    so the scheme allowlist + cloud-metadata SSRF guard apply, matching the
    SiteWise (Direct) probe's AWS-native pattern.
    """
    cfg = resolved.get("connection_config") if isinstance(resolved.get("connection_config"), dict) else {}
    region = (resolved.get("region") or cfg.get("region") or os.environ.get("AWS_REGION", "us-east-1"))
    workgroup = (resolved.get("workgroup") or cfg.get("workgroup") or "")
    if not workgroup:
        raise RuntimeError("no Athena workgroup configured")
    # Validate the REAL, resolvable Athena service endpoint against the SSRF/scheme
    # guard (scheme 'athena' is allowlisted). Using the actual endpoint host — not a
    # bare 'region.amazonaws.com' — so the guard's DNS-resolution step passes.
    validate_connection_target(f"athena://athena.{region}.amazonaws.com", connection_type="Athena")
    resp = _probe_client(resolved, "athena", region).get_work_group(WorkGroup=workgroup)
    state = resp.get("WorkGroup", {}).get("State", "") or "reachable"
    return f"workgroup {workgroup} {state}"


_PROBES = {"postgres": _probe_postgres, "mcp": _probe_mcp,
           "rest": _probe_rest, "sitewise": _probe_sitewise, "athena": _probe_athena}


def _vendor_probe(connection_type: str):
    """The REST-vendor registry's ``probe`` hook for this declared type, if
    the vendor supplies one (for example when liveness is an OAuth exchange, not
    a health GET). None falls back to the probe table. Lazy import: the registry
    imports this module's probes inside its hooks."""
    from .rest_vendor_registry import resolve_vendor
    adapter = resolve_vendor(connection_type or "")
    return adapter.probe if adapter is not None else None


def _result(status: str, latency_ms: int, connection_type: str, detail: str,
            reason: str | None = None) -> dict:
    """Build a probe result.

    ``reason`` is a coarse enumerated value (see :data:`_REASON_DETAIL`); when
    omitted it defaults to ``"ok"`` for a pass and ``"error"`` otherwise. ``detail``
    is a COARSE, secret-free human string (never raw host/IP/port/stack).
    """
    if reason is None:
        reason = "ok" if status == "pass" else "error"
    return {"status": status, "latency_ms": latency_ms,
            "connection_type": connection_type, "reason": reason, "detail": detail}


def _classify_exception(exc: Exception) -> str:
    """Map a probe exception to a coarse :data:`_REASON_DETAIL` reason key.

    Never inspects/returns host/IP/port — only the exception type and a lowercased
    message are used to bucket the failure. The verbose exception is logged
    server-side by :func:`probe_connection`, not returned to the caller.
    """
    if isinstance(exc, ConnectionTargetError):
        return "blocked_target"
    # MissingDbPasswordError (and its ConnectionSecretUnavailableError) is
    # pg_conn_params refusing a credential source, e.g. a user registration
    # with no password of its own.
    if isinstance(exc, (CredentialUnresolvedError, MissingDbPasswordError)):
        return "credential_unresolved"
    if isinstance(exc, AssumeRoleDeniedError):
        return "assume_role_denied"
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    # HTTP auth failures (httpx.HTTPStatusError carries .response.status_code).
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in (401, 403) or "401" in msg or "403" in msg or "authentication failed" in msg:
        return "auth_failed"
    if "timeout" in name or "timed out" in msg or "timeout" in msg:
        return "timeout"
    # pgdriver OperationalError "connection refused" / "could not connect".
    if "refused" in msg or "could not connect" in msg:
        return "connection_refused"
    if "did not resolve" in msg or "could not resolve" in msg \
            or "name resolution" in msg or "unreachable" in msg \
            or "no route to host" in msg:
        return "unreachable"
    return "error"


def probe_connection(system_id: str, resolved: dict | None = None) -> dict:
    """Probe live connectivity; returns status/latency_ms/connection_type/reason/detail.

    Validates the target (SSRF guard) before any network call. The result returned
    to callers is deliberately COARSE: ``reason`` is one of a small enum
    (:data:`_REASON_DETAIL`) and ``detail`` is the matching secret-free human
    string — no host/IP/port/stack leaks (avoids a blind-SSRF / port-scan oracle).
    The full verbose failure detail is logged server-side only.
    """
    if resolved is None:
        # Lazy import to avoid a circular import at module load: ``connection_resolver``
        # re-exports from this module, so this module must not import it at top level.
        from .connection_resolver import resolve_connection
        resolved = resolve_connection(system_id)
    connection_type = resolved.get("connection_type") or "PostgreSQL"

    # A CONFIGURED secret that failed to resolve must NOT run tokenless and report
    # a misleading auth failure — surface the accurate coarse reason instead.
    if resolved.get("_secret_unresolved"):
        logger.warning("probe_connection: %s (type=%s): configured secret unresolved, "  # logs the system id and type only, never a value
                       "reporting credential_unresolved", system_id, connection_type)
        return _result("fail", 0, connection_type,
                       _REASON_DETAIL["credential_unresolved"], reason="credential_unresolved")

    probe = (_vendor_probe(connection_type)
             or _PROBES.get(_normalize_type(connection_type, resolved)))
    start = time.perf_counter()
    if probe is None:
        logger.warning("probe_connection: unsupported type %r for %s", connection_type, system_id)
        return _result("fail", 0, connection_type,
                       f"unsupported connection type: {connection_type}", reason="error")
    try:
        # Cross-account PREFLIGHT: verify sts:AssumeRole works before the
        # type-specific probe. No-op when the config carries no role_arn.
        _preflight_assume_role(resolved)
        detail = probe(resolved)
        return _result("pass", int((time.perf_counter() - start) * 1000),
                       connection_type, detail, reason="ok")
    except Exception as exc:  # noqa: BLE001 - report any probe failure as a coarse fail
        latency = int((time.perf_counter() - start) * 1000)
        reason = _classify_exception(exc)
        # Verbose detail (may include host/IP/port/stack) stays SERVER-SIDE only.
        logger.warning("probe_connection: %s probe failed for %s [reason=%s]: %s",
                       connection_type, system_id, reason, _safe_detail(exc, resolved))
        return _result("fail", latency, connection_type,
                       _REASON_DETAIL.get(reason, _REASON_DETAIL["error"]), reason=reason)
