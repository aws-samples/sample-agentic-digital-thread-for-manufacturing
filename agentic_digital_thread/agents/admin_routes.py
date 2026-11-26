# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from datetime import datetime, timedelta, timezone
import logging
import os
import re
import threading
from urllib.parse import quote, urlsplit, urlunsplit
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from agents.feedback import FeedbackService
from agents.usage import UsageService
from agents.guardrail_client import GuardrailService
from . import admin_conversations as _conversations
from . import demo
from .offload import offload, offload_holding
from .public_errors import public_error, public_error_text
from .runtime_provision_helpers import RUNTIME_CONFIG_LOCK
from .startup_restore import refuse_while_restoring
from . import live_settings
from . import prompts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")


# Live AgentManager. Mirrors agents_routes.set_agent_manager — server.py injects the
# manager into agents_routes at startup; admin_routes reuses that SAME instance via
# _get_agent_manager() (no server.py edit required). set_agent_manager() lets tests
# inject a mock manager directly.
_agent_manager = None


def set_agent_manager(mgr):
    """Inject the live AgentManager used by the connection-update live reconnect."""
    global _agent_manager
    _agent_manager = mgr


def _get_agent_manager():
    """Return the live AgentManager.

    Prefer an explicitly-injected instance; otherwise reuse the manager that
    server.py already injected into ``agents_routes`` (reuse of an existing
    accessor — avoids a server.py edit). Returns ``None`` if neither is available.
    """
    if _agent_manager is not None:
        return _agent_manager
    try:
        from . import agents_routes
        return getattr(agents_routes, "_agent_manager", None)
    except Exception:  # nosec B110 - best-effort accessor; reconnect degrades gracefully
        return None


def _check_admin(request: Request):
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def admin_refuse_while_restoring(request: Request) -> None:
    """Route dependency: the admin check, then the startup-restore refusal
    (``startup_restore``), so a non-admin gets its 403 during a restore too.
    The route body keeps its own admin check."""
    _check_admin(request)
    refuse_while_restoring()


def _default_range() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=7)).isoformat(), now.isoformat()


def dashboard(request: Request):
    """Usage totals for the last 7 days from the usage table. Sync body (a
    DynamoDB scan, now that ``adt-usage`` exists); the route offloads it."""
    _check_admin(request)
    start, end = _default_range()
    records = UsageService().get_by_time_range(start, end)
    if not records:
        return {"total_requests": 0, "total_tokens": 0, "unique_users": 0, "avg_tokens_per_request": 0, "period_days": 7}
    total_tokens = sum(r.get("total_tokens", 0) for r in records)
    return {
        "total_requests": len(records),
        "total_tokens": total_tokens,
        "unique_users": len({r.get("user_id") for r in records if r.get("user_id")}),
        "avg_tokens_per_request": total_tokens // len(records) if records else 0,
        "period_days": 7,
    }


@router.get("/dashboard")
async def dashboard_route(request: Request):
    """Usage KPI totals. Sync body: ``dashboard``."""
    return await offload(dashboard, request)


_CONVERSATIONS_UNCONFIGURED = (
    "AgentCore Memory is not configured for this deployment, so there are no "
    "saved conversations to list.")
_CONVERSATIONS_READ_FAILED = {
    "memory": "The conversation memory could not be read.",
    "cognito": "User logins could not be read from the user pool.",
}


def admin_conversations(request: Request, days: int, user: "str | None", limit: int,
                        cursor: "str | None"):
    """Every user's web-chat sessions from AgentCore Memory, newest first,
    each with its questions, final answers and feedback. Bounds, paging and
    the honest ``more`` markers: ``agents/admin_conversations.py``. A memory
    or Cognito failure is a 503 through ``public_errors``, never an empty
    list. Sync body (memory, Cognito and feedback reads); the route offloads it."""
    _check_admin(request)
    try:
        return _conversations.list_conversations(days=days, user=user, limit=limit,
                                                 cursor=cursor)
    except _conversations.CursorError:
        return JSONResponse(status_code=400, content={
            "detail": "The cursor is not valid for this filter; reload the first page."})
    except _conversations.MemoryNotConfiguredError:
        return JSONResponse(status_code=503, content={
            "detail": _CONVERSATIONS_UNCONFIGURED, "source": "memory", "error": True})
    except _conversations.ConversationsError as exc:
        return JSONResponse(status_code=503, content={
            "detail": _CONVERSATIONS_READ_FAILED[exc.source], "source": exc.source,
            **public_error(exc.cause, f"admin.conversations.{exc.source}",
                           default="upstream_error")})


@router.get("/conversations")
async def admin_conversations_route(
        request: Request,
        days: int = Query(_conversations.DEFAULT_DAYS, ge=1, le=_conversations.MAX_DAYS),
        user: "str | None" = Query(None, max_length=128,
                                   pattern=_conversations.ACTOR_ID_PATTERN),
        limit: int = Query(_conversations.DEFAULT_PAGE_SIZE, ge=1,
                           le=_conversations.MAX_PAGE_SIZE),
        cursor: "str | None" = Query(None, max_length=_conversations.MAX_CURSOR_CHARS)):
    """All users' web-chat conversations (admin only). Sync body: ``admin_conversations``."""
    return await offload(admin_conversations, request, days, user, limit, cursor)


@router.get("/feedback")
async def feedback(request: Request, sentiment: str = "all", start: str = None, end: str = None):
    _check_admin(request)
    if not start or not end:
        start, end = _default_range()
    svc = FeedbackService()
    sentiment_filter = None if sentiment == "all" else sentiment
    records = svc.get_by_time_range(start, end, sentiment=sentiment_filter)
    stats = svc.get_stats(start, end)
    return {"records": records, "stats": stats, "filters": {"sentiment": sentiment, "start": start, "end": end}}


@router.get("/guardrails")
async def guardrails(request: Request, start: str = None, end: str = None):
    _check_admin(request)
    if not start or not end:
        start, end = _default_range()
    violations = GuardrailService().get_by_time_range(start, end)
    return {"violations": violations, "total": len(violations), "filters": {"start": start, "end": end}}


#: Quick-link seed sentinels. deploy.sh seeds "placeholder" when the optional
#: infra (HighByte / ERP dashboard / SiteWise i3x) isn't deployed; "pending"
#: marks a system whose stack finishes after the app task has started, until
#: its real URL is written.
_QUICKLINK_SENTINELS = frozenset({"placeholder", "pending"})


def _normalize_quicklink(value: str) -> str:
    """Normalize a sentinel (any case) and any CDK synth dummy to empty so the
    UI treats the feature as unconfigured and never renders a dead link."""
    v = (value or "").strip()
    if v.lower() in _QUICKLINK_SENTINELS or v.startswith("dummy-value-for-"):
        return ""
    return v


def _quicklink_env(var: str) -> str:
    """A task-start quick-link env value, normalized."""
    return _normalize_quicklink(os.environ.get(var, ""))


#: SSM name -> (monotonic expiry, value) of the last live quick-link read.
_QUICKLINK_CACHE: dict[str, tuple[float, str]] = {}
_QUICKLINK_TTL_S = 60.0
#: A failed read is cached this long as "not configured", so an SSM outage
#: costs one bounded request per window instead of one per page load.
_QUICKLINK_FAILURE_TTL_S = 15.0
#: One request, no retry: botocore's ``max_attempts`` counts RETRIES (1 means
#: two requests), ``total_max_attempts`` counts requests. With the 2 s connect
#: and read timeouts, a page load spends at most one ~4 s request on SSM.
_QUICKLINK_SSM_CONFIG = {"connect_timeout": 2, "read_timeout": 2,
                         "retries": {"total_max_attempts": 1}}
_QUICKLINK_SSM_CLIENT = None
#: SSM name -> monotonic time the read behind its cache entry STARTED, so a
#: read that started earlier never replaces a later one's result.
_QUICKLINK_READ_AT: dict[str, float] = {}
#: Single flight for the live reads. FastAPI runs the sync quick-links body on
#: worker threads, so several page loads can find the same expired entry at
#: once; the lock makes them share one client and one GetParameters request
#: (a caller that waited re-checks the cache the first one just filled). The
#: wait is bounded by that one request (~4 s at most).
_QUICKLINK_LOCK = threading.Lock()
_QUICKLINK_CLIENT_LOCK = threading.Lock()

#: Task-start env var -> the SSM parameter ECS resolved it from
#: (stacks/adt_compute_stack.py ``_quicklink_ssm_secrets``).
_QUICKLINK_PARAMETERS = {
    "HIGHBYTE_URL": "/adt/highbyte-url",
    "ERP_DASHBOARD_URL": "/adt/erp-dashboard-url",
    "SITEWISE_I3X_URL": "/adt/sitewise-i3x-url",
    "QUICK_DESKTOP_URL": "/adt/quick-desktop-url",
}


def _quicklink_ssm():
    """The one SSM client the live quick-link reads share."""
    global _QUICKLINK_SSM_CLIENT
    with _QUICKLINK_CLIENT_LOCK:
        if _QUICKLINK_SSM_CLIENT is None:
            import boto3 as _boto3
            from botocore.config import Config
            _QUICKLINK_SSM_CLIENT = _boto3.client(
                "ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                config=Config(**_QUICKLINK_SSM_CONFIG))
        return _QUICKLINK_SSM_CLIENT


def _quicklink_store(name: str, value: str, ttl: float, read_at: float) -> None:
    """Cache *value* for *name* unless a read that started later already
    stored its result (an older answer, such as a failure that finished last,
    never overwrites a newer good value). Expiry counts from when the read
    finished."""
    import time
    if read_at < _QUICKLINK_READ_AT.get(name, float("-inf")):
        return
    _QUICKLINK_READ_AT[name] = read_at
    _QUICKLINK_CACHE[name] = (time.monotonic() + ttl, value)


def _ssm_quicklinks(names: list[str]) -> dict[str, str]:
    """Live quick-link parameters *names*, normalized, in ONE GetParameters
    request for every name not cached. A successful read is cached for a
    minute; a failure reads as "not configured" and is cached for
    ``_QUICKLINK_FAILURE_TTL_S``. This runs on an admin page load, so a slow
    SSM costs one short request, not one per link with retries."""
    with _QUICKLINK_LOCK:
        return _ssm_quicklinks_locked(names)


def _cached_quicklinks(names: list[str]) -> tuple[dict[str, str], list[str]]:
    """(the unexpired cached values, the names that need a read)."""
    import time
    now = time.monotonic()
    out: dict[str, str] = {}
    missing = []
    for name in names:
        expires, cached = _QUICKLINK_CACHE.get(name, (0.0, ""))
        if now < expires:
            out[name] = cached
        else:
            missing.append(name)
    return out, missing


def _ssm_quicklinks_locked(names: list[str]) -> dict[str, str]:
    """:func:`_ssm_quicklinks` under ``_QUICKLINK_LOCK``."""
    import time
    out, missing = _cached_quicklinks(names)
    if not missing:
        return out
    read_at = time.monotonic()
    try:
        resp = _quicklink_ssm().get_parameters(Names=missing)
        values = {p.get("Name"): _normalize_quicklink(p.get("Value", "")).rstrip("/")
                  for p in resp.get("Parameters", [])}
        ttl = _QUICKLINK_TTL_S
    except Exception as e:  # noqa: BLE001 - optional links; report the class only
        logger.info("live quick-link read (%s) failed: %s", ", ".join(missing), type(e).__name__)
        values, ttl = {}, _QUICKLINK_FAILURE_TTL_S
    for name in missing:
        _quicklink_store(name, values.get(name, ""), ttl, read_at)
        out[name] = _QUICKLINK_CACHE[name][1]
    return out


def _quicklinks_live(env_vars: list[str]) -> dict[str, str]:
    """Task-start quick links, read live from SSM while the task-start value
    is a sentinel (all such links in one request).

    ECS resolves these from SSM once at task start. On a first ``--highbyte``
    deploy the task starts while ``/adt/highbyte-url`` still holds deploy.sh's
    "placeholder" (deploy.sh writes the real URL after CDK and does not
    restart the app), and the workshop root starts the app while a shipped
    system's link still holds "pending". Only in that case (the task-start
    value IS a sentinel) read SSM live, so the links work without a restart.
    An unset env (local dev) never calls SSM.
    """
    out: dict[str, str] = {}
    live: dict[str, str] = {}
    for var in env_vars:
        raw = os.environ.get(var, "")
        url = _normalize_quicklink(raw)
        if url or raw.strip().lower() not in _QUICKLINK_SENTINELS:
            out[var] = url
        else:
            live[var] = _QUICKLINK_PARAMETERS[var]
    if live:
        values = _ssm_quicklinks(list(live.values()))
        out.update({var: values[name] for var, name in live.items()})
    return out


def _quicklink_live(var: str) -> str:
    """One task-start quick link (:func:`_quicklinks_live`)."""
    return _quicklinks_live([var])[var]


def _quicklink_highbyte_url() -> str:
    """HighByte URL for the quick links, the i3X explorer link and the
    Settings token card (:func:`_quicklink_live`)."""
    return _quicklink_live("HIGHBYTE_URL")


#: The i3X Explorer web build that server.py mounts at /i3x-explorer/ when it
#: exists. The sample does not include the explorer, so the build is absent and
#: the admin page hides its links.
_I3X_EXPLORER_INDEX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "i3x-explorer", "index.html")


def quick_links():
    """Return quick-access URLs and presence-flags for tokens. Booleans tell the UI
    whether a token is configured, but never expose the actual value.
    The i3x explorer launcher fetches the actual token via /i3x-explorer-link.
    Sync body (may read SSM, once for every sentinel link); the route offloads it.
    """
    _val = _quicklink_env
    live = _quicklinks_live(list(_QUICKLINK_PARAMETERS))
    return {
        "i3x_explorer": os.path.isfile(_I3X_EXPLORER_INDEX),
        "highbyte": live["HIGHBYTE_URL"],
        "highbyte_i3x_token": bool(_val("HIGHBYTE_I3X_TOKEN")),
        "erp_dashboard": live["ERP_DASHBOARD_URL"],
        "console_start": _val("CONSOLE_START_URL"),
        "sitewise_i3x_url": live["SITEWISE_I3X_URL"],
        "sitewise_i3x_token": bool(_val("SITEWISE_I3X_TOKEN")),
        # Optional AWS Quick desktop URL for the admin tile. Resolved from
        # SSM /adt/quick-desktop-url at task start; the "placeholder" seed (and
        # CDK synth dummies) normalize to empty so the admin tile only renders
        # where an operator has set a URL (per-env, optional).
        "quick_desktop": live["QUICK_DESKTOP_URL"],
    }


@router.get("/quick-links")
async def quick_links_route(request: Request):
    """Quick-access URLs and token presence flags. Sync body: ``quick_links``."""
    _check_admin(request)
    return await offload(quick_links)


def i3x_explorer_link(which: str, request: Request):
    """Return a one-shot URL for the i3x explorer with the appropriate bearer token.
    `which` is 'highbyte' or 'sitewise'. Token is read fresh from Secrets Manager
    (or env var fallback) on every call — never cached in JSON. Endpoint is admin-only.
    Sync body (Secrets Manager, and SSM for a task started before the HighByte
    URL was written); the route offloads it.
    """
    _check_admin(request)

    if which == "highbyte":
        base_url = _quicklink_highbyte_url()
        if not base_url:
            return {"status": "error", "message": "HighByte URL not configured"}
        target_url = base_url.rstrip("/") + "/i3x"
        # Resolve the token from the canonical conn/highbyte secret via the
        # resolver (node secret_arn -> Secrets Manager). The legacy
        # adt/highbyte-mcp-token secret is GONE (Step 3 cut-over,
        # T5.1) — there is no legacy-secret read here anymore. Never cached.
        token = ""  # nosec B105 - not a credential value (placeholder/key/secret arn)
        try:
            from . import connection_resolver as cr
            token = (cr.resolve_connection("highbyte").get("token") or "").strip()
        except Exception as e:
            logger.warning("%s: failed to resolve the HighByte token from conn/highbyte", e)  # error text only, never a credential value
            token = ""  # nosec B105 - not a credential value
        name = "HighByte"
    elif which == "sitewise":
        target_url = _quicklink_live("SITEWISE_I3X_URL")
        if not target_url:
            return {"status": "error", "message": "SiteWise i3x URL not configured"}
        # SiteWise i3x token comes from CDK stack output / env var (not in Secrets Manager today)
        token = os.environ.get("SITEWISE_I3X_TOKEN", "").strip()
        name = "SiteWise"
    else:
        return {"status": "error", "message": f"Unknown target: {which}"}

    from urllib.parse import quote
    href = "/i3x-explorer/?url=" + quote(target_url, safe="") + "&name=" + quote(name)
    if token and token != "placeholder":  # nosec B105 - not a credential value (placeholder/key/secret arn)
        # The bearer token rides in the URL FRAGMENT (#token=...), not the query
        # string: fragments are never sent to the server, so the token stays out
        # of access logs, proxies, and the Referer header. The vendored
        # i3x-explorer client reads it from location.hash (App.tsx).
        href += "#token=" + quote(token, safe="")
    return {"status": "ok", "href": href}


@router.get("/i3x-explorer-link/{which}")
async def i3x_explorer_link_route(which: str, request: Request):
    """One-shot tokenized i3x explorer URL. Sync body: ``i3x_explorer_link``."""
    return await offload(i3x_explorer_link, which, request)


@router.put("/highbyte-token", dependencies=[Depends(admin_refuse_while_restoring)])
async def update_highbyte_token(request: Request):
    """Store the HighByte MCP Bearer token — thin shim over the generalized
    connection-update flow (DRY: one update path).

    Delegates to :func:`_do_update_connection` with ``system_id='highbyte'``,
    ``secret={'token': ...}`` and ``validate=False`` so the token is STORED
    UNCONDITIONALLY (written to ``adt/conn/highbyte`` and the
    in-process ``HIGHBYTE_I3X_TOKEN`` env var refreshed) and then BEST-EFFORT
    reconnected — it does NOT 400-block on a failed live probe. This preserves the
    documented post-deploy "store the token unconditionally" primitive; the NEW
    generalized ``PUT /api/admin/connection/{id}`` keeps ``validate=True``
    (validate-first). Kept as a route so the existing Admin UI keeps working.
    (Step 3 cut-over: the legacy ``highbyte-mcp-token`` dual-write is GONE — only
    the canonical ``conn/highbyte`` secret is written.)
    """
    _check_admin(request)
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}
    token = (body.get("token") or "").strip()
    if not token:
        return {"status": "error", "message": "Token is required"}

    result = await offload(
        _do_update_connection, "highbyte", request, {"token": token},
        connection_type="MCP Server", validate=False,
    )
    # Back-compat: keep the in-process env var in sync so existing readers that
    # use HIGHBYTE_I3X_TOKEN (catalog._get_highbyte_token) pick it up immediately.
    if isinstance(result, dict) and result.get("status") == "ok":
        os.environ["HIGHBYTE_I3X_TOKEN"] = token
        return {"status": "ok", "message": "HighByte token saved. MCP onboarding will use this token.",
                "reconnected": result.get("reconnected", False)}
    return result


# Non-secret connection fields accepted from an Update body.
_NON_SECRET_FIELDS = {"url", "host", "port", "scheme", "base_url",
                      "connection_string", "auth_scheme", "health_path"}
# Athena: the workgroup an Edit may move the system to (it sets the per-query
# scan limit and the results location). Taken only for an Athena system; the
# validate-first probe describes the new workgroup through the system's own
# role before anything is written. Athena's own name rule.
_ATHENA_WORKGROUP_FIELD = "workgroup"
_ATHENA_WORKGROUP_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
# Max accepted length for any submitted string field (defensive cap).
_MAX_FIELD_LEN = 4096


def _assemble_pg_connection_string(non_secret: dict, secret: dict, existing: dict) -> str | None:
    """Build a PostgreSQL connection_string from partial Update fields.

    The onboard.js Postgres edit form posts ``host``/``port``/``password`` (not a
    full ``connection_string``), so assemble one here: host/port from the submitted
    body, user/db/query (search_path etc.) preserved from the EXISTING resolved
    connection_string, and the password from the submitted ``password`` secret —
    falling back to the existing inline password when the submitted one is blank
    (so a host/port-only edit keeps the current credential). Returns ``None`` when
    there is nothing to assemble (no host submitted and no existing string).
    """
    existing_cs = str(existing.get("connection_string", "") or "")
    ex = urlsplit(existing_cs) if existing_cs else None
    sub_host = str(non_secret.get("host", "") or "").strip()
    if not sub_host and ex is None:
        return None  # nothing to build from
    host = sub_host or (ex.hostname if ex else "")
    if not host:
        return None
    port = str(non_secret.get("port", "") or "").strip() or (str(ex.port) if ex and ex.port else "")
    user = (ex.username if ex else "") or str(existing.get("user", "") or "")
    # Submitted password wins; otherwise preserve the existing inline password.
    password = str(secret.get("password", "") or "").strip() or (ex.password if ex else "") or ""
    path = ex.path if ex else ""        # e.g. /erp_simulation (the db name)
    query = ex.query if ex else ""      # e.g. options=-csearch_path%3Derp_data
    # Percent-encode userinfo: a user/password carrying URL-reserved chars
    # (@ : / ? # space) would otherwise corrupt the netloc and break urlsplit
    # parsing of the assembled DSN. quote(..., safe="") encodes every reserved
    # char so the credential round-trips through urlsplit/unquote intact.
    enc_user = quote(user, safe="")
    enc_password = quote(password, safe="")
    userinfo = enc_user if not password else f"{enc_user}:{enc_password}"
    netloc = f"{userinfo}@{host}" if userinfo else host
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("postgresql", netloc, path, query, ""))


def _parse_connection_body(body: dict, connection_type: str, *, existing: dict | None = None,
                           user_mcp: bool = False, stored_type: str = ""):
    """Split an Update body into ``(non_secret, secret, error)``.

    Validates length caps, the scheme allowlist, an
    Athena system's ``workgroup`` name, and the required field(s) per
    connection_type. Secret-bearing keys
    (token/password/secret/api_key/key) are separated into ``secret``;
    recognized non-secret fields into ``non_secret``.

    ``existing`` is the system's currently-resolved config; it lets two partial-
    update flows work:
      * **secret-only / token rotation** — when the body carries no url/
        connection_string but one ALREADY resolves from ``existing``, the update
        proceeds with just the secret (the legacy highbyte token-only rotation).
      * **PostgreSQL host/port/password edits** — the onboard.js PG form posts
        host/port/password; a full ``connection_string`` is assembled from those
        plus the existing user/db (see :func:`_assemble_pg_connection_string`).

    Returns ``(None, None, "<reason>")`` on a validation error. ``user_mcp``
    (a user-registered MCP Server) also checks its ``auth_scheme``
    (:func:`_check_mcp_auth_scheme`); no other system's update changes.
    """
    from . import connection_resolver as cr

    if not isinstance(body, dict):
        return None, None, "body must be a JSON object"
    existing = existing or {}
    ct = (connection_type or "").lower()
    non_secret, secret, err = _split_body_fields(body, cr)
    err = (err or _take_athena_workgroup(body, ct, non_secret, stored_type)
           or _check_body_schemes(non_secret, cr)
           or (_check_mcp_auth_scheme(non_secret) if user_mcp else None))
    if err:
        return None, None, err
    _assemble_pg_into(ct, non_secret, secret, existing)
    err = _missing_required_field(ct, non_secret, existing)
    if err:
        return None, None, err
    return non_secret, secret, None


def _split_body_fields(body: dict, cr) -> tuple:
    """``(non_secret, secret, error)``: secret-bearing keys into ``secret``,
    recognized non-secret fields into ``non_secret``, anything else dropped."""
    non_secret: dict = {}
    secret: dict = {}
    for key, value in body.items():
        if isinstance(value, str) and len(value) > _MAX_FIELD_LEN:
            return None, None, f"field '{key}' exceeds maximum length"
        lkey = str(key).lower()
        if lkey == "connection_type":
            continue
        if lkey in cr._REDACT_KEYS:
            if value:
                secret[key] = value
        elif lkey in _NON_SECRET_FIELDS:
            non_secret[key] = value
    return non_secret, secret, None


def _take_athena_workgroup(body: dict, ct: str, non_secret: dict,
                           stored_type: str) -> str | None:
    """Put a submitted ``workgroup`` into ``non_secret`` for an Athena system:
    one registered as Athena (``stored_type``, the catalog's) and edited as
    Athena (``ct``), so a body cannot hand the field to another system by
    naming another type. Ignored otherwise, and when blank (the Edit form's
    "keep current"). The error for a value Athena could not name a workgroup."""
    if "athena" not in ct or "athena" not in (stored_type or "").lower():
        return None
    for key, value in body.items():
        if str(key).lower() != _ATHENA_WORKGROUP_FIELD or value in (None, ""):
            continue
        if not (isinstance(value, str) and _ATHENA_WORKGROUP_NAME.fullmatch(value)):
            return ("'workgroup' must be an Athena workgroup name: 1 to 128 letters, "
                    "digits, '.', '_' or '-'")
        non_secret[_ATHENA_WORKGROUP_FIELD] = value
    return None


def _check_body_schemes(non_secret: dict, cr) -> str | None:
    """Scheme allowlist: check any submitted URL and any explicit scheme field.
    Uses the resolver's single source of truth (DRY), see SCHEME_ALLOWLIST.
    A submitted URL that needs percent-encoding (or has an invalid port) is
    refused with the plain message of ``connection_security.credential_url_error``."""
    from .connection_security import credential_url_error
    for field in ("url", "base_url", "connection_string"):
        value = non_secret.get(field)
        err = credential_url_error(value) if isinstance(value, str) else ""
        if err:
            return err
    target = (non_secret.get("url") or non_secret.get("base_url")
              or non_secret.get("connection_string") or "")
    if target and "://" in str(target):
        scheme = (urlsplit(str(target)).scheme or "").lower()
        if scheme not in cr.SCHEME_ALLOWLIST:
            return f"scheme '{scheme}' is not allowed"
    explicit_scheme = str(non_secret.get("scheme", "") or "").lower()
    if explicit_scheme and explicit_scheme not in cr.SCHEME_ALLOWLIST:
        return f"scheme '{explicit_scheme}' is not allowed"
    return None


#: The Authorization headers an MCP Server token can be sent with (the Edit
#: form's "Authorization header" select): ``Bearer <token>``, ``X-API-Key``,
#: or the token as entered.
_MCP_AUTH_SCHEMES = ("bearer", "api_key", "bearer_literal")


def _check_mcp_auth_scheme(non_secret: dict) -> str | None:
    """A user MCP Server's ``auth_scheme`` is one of :data:`_MCP_AUTH_SCHEMES`
    (its Edit form's Authorization header select), so a typo is refused
    instead of being stored and sending no header."""
    for key in [k for k in non_secret if str(k).lower() == "auth_scheme"]:
        value = str(non_secret[key] or "").strip().lower()
        if value not in _MCP_AUTH_SCHEMES:
            return ("auth_scheme must be bearer (Authorization: Bearer <token>), api_key "
                    "(X-API-Key: <token>) or bearer_literal (the token as entered)")
        non_secret[key] = value
    return None


def _assemble_pg_into(ct: str, non_secret: dict, secret: dict, existing: dict) -> None:
    """PostgreSQL: assemble a connection_string from host/port/password (+ existing
    user/db) when the body did not carry a full connection_string itself. A
    connection stored as fields (:func:`_pg_stored_as_fields`) has no string to
    take the user and database from, so a host/port edit with no password
    stays fields (an edit with a password is assembled and refused as before)."""
    fields_only = _pg_stored_as_fields(existing) and not (
        secret.get("password") or secret.get("Password"))
    if (("postgres" in ct or "sql" in ct) and not non_secret.get("connection_string")
            and not fields_only):
        assembled = _assemble_pg_connection_string(non_secret, secret, existing)
        if assembled:
            non_secret["connection_string"] = assembled
            # The password is now inline in the connection_string (consistent with
            # the existing PG entries) — drop it from the separate secret so no
            # spurious conn/<id> secret is created.
            secret.pop("password", None)
            secret.pop("Password", None)


def _missing_required_field(ct: str, non_secret: dict, existing: dict) -> str | None:
    """Required field(s) per connection_type. A field already present in the
    EXISTING resolved config satisfies the requirement (allows secret-only
    updates / token rotation without re-supplying the url)."""
    if "mcp" in ct:
        if not (non_secret.get("url") or non_secret.get("connection_string")
                or existing.get("url") or existing.get("connection_string")):
            return "MCP Server requires a 'url'"
    elif "postgres" in ct or "sql" in ct:
        if not (non_secret.get("connection_string") or existing.get("connection_string")
                or _pg_stored_as_fields(existing)):
            return "PostgreSQL requires a 'connection_string'"
    elif "rest" in ct or "i3x" in ct or "openapi" in ct or "api" in ct:
        if not (non_secret.get("base_url") or non_secret.get("connection_string")
                or existing.get("base_url") or existing.get("connection_string")):
            return "REST API requires a 'base_url'"
    # SiteWise (Direct) and other types have safe defaults — no strict requirement.
    return None


def _pg_stored_as_fields(existing: dict) -> bool:
    """True for a PostgreSQL connection resolved from stored fields (host and
    database, plus the secret it points at) with no connection string at all:
    a demo or workshop system on the deployment's database, whose node holds
    ``host``/``port``/``dbname``/``schema``/``search_path``."""
    return (not existing.get("connection_string") and bool(existing.get("host"))
            and bool(existing.get("dbname") or existing.get("database")))


def _same_stored_value(key: str, value, stored) -> bool:
    """A submitted value equals the stored one (text compare; host ignores case)."""
    new, old = str(value or "").strip(), str(stored or "").strip()
    return new.lower() == old.lower() if key == "host" else new == old


def _unchanged_pg_fields_edit(cat_entry: dict, existing: dict, merged: dict, secret) -> bool:
    """True when an Edit of a demo PostgreSQL system stored as fields changes
    nothing, so it may skip the probe. The catalog entry, never the request
    body, must say PostgreSQL; the system must not be a user registration and
    must run on the deployment's own credentials; no credential may be sent;
    and every merged non-secret value, including any connection type, must
    equal the stored one (``{}``, or the host and port the Edit form shows)."""
    from . import connection_resolver as cr
    stored_type = str((cat_entry or {}).get("connection_type") or "")
    if secret or "postgres" not in stored_type.lower() or cr.is_user_registration(cat_entry):
        return False
    if not (_runs_on_deployment_credentials(existing) and _pg_stored_as_fields(existing)):
        return False
    stored = {**existing, "connection_type": stored_type}
    return all(_same_stored_value(k, v, stored.get(k)) for k, v in merged.items())


def _reconnect_unchanged(system_id: str, connection_type: str, merged: dict,
                         cat_entry: dict, actor: str) -> dict:
    """An Edit that changes nothing in a connection stored as fields: there is
    nothing to validate or write, so only the live wiring is re-synced from
    the stored connection and its secret (``_reconnect_system``). This is the
    one-time step that moves a demo system onto its schema's read-only login
    (docs/deployment.md, "Upgrading an existing deployment")."""
    from . import connection_resolver as cr
    reconnected = _reconnect_system(system_id, connection_type, cat_entry)
    cr.audit_connection_event("update", system_id, actor=actor, outcome="ok",
                              extra={"reconnected": reconnected, "unchanged": True})
    return {"status": "ok", "system_id": system_id,
            "connection": cr.mask_connection(merged), "reconnected": reconnected}


def _reconnect_cloud_system(system_id: str, connection_type: str, cat_entry: dict) -> bool:
    """Cloud / gateway-mode reconnect: re-persist the changed connection AND
    force the running runtime to pick it up.

    ``start_agent`` CANNOT do this: with a READY gateway target already present
    (the steady state) it fast-paths and returns the gateway URL WITHOUT calling
    ``_start_cloud``, so ``adt/agent-config/<id>`` is never rewritten; and a
    running runtime resolves its downstream connection ONCE at container start.
    ``resync_cloud_connection`` rewrites the agent-config secret and deploys a new
    runtime version so the replacement container re-reads the fresh config. It
    returns False (never raises) on any failure.

    Bug fix (secret_arn preservation): ``resync_cloud_connection`` REWRITES
    agent-config from this resolved config, and the runtime resolves credentials
    by following its ``secret_arn`` pointer. If an earlier failed
    discover/provision never persisted secret_arn onto the node,
    ``resolve_connection`` returns it EMPTY — the rewritten agent-config then
    carries an empty pointer and the runtime cannot resolve credentials (every
    read fails authentication). Fall back to the catalog entry's
    registration-time secret_arn (the same pre-node source
    onboard_preflight / resolve use). Affects EVERY secret-based connector
    reconnected via Edit (Tulip / REST / i3X / MCP).
    """
    from . import connection_resolver as cr
    resolved: dict = {}
    try:
        mgr = _get_agent_manager()
        if mgr is None:
            logger.warning("update_connection: no agent_manager (gateway mode) — "
                           "cannot re-sync runtime config for %s", system_id)
            return False
        resolved = cr.resolve_connection(system_id)
        if not resolved.get("secret_arn") and cat_entry.get("secret_arn"):
            resolved = {**resolved, "secret_arn": cat_entry["secret_arn"]}
        return bool(mgr.resync_cloud_connection(
            system_id=system_id,
            connection_type=connection_type,
            connection_config=resolved,
        ))
    except Exception as exc:  # noqa: BLE001 - reconnect is best-effort; never 500
        return _reconnect_fail_log(system_id, exc, resolved)


def _reconnect_system(system_id: str, connection_type: str, cat_entry: dict) -> bool:
    """Live-reconnect a system after a config change — no app restart.

    The connection config has ALREADY been persisted by the caller before this
    runs; this only refreshes the live wiring. Wrapped in a broad try/except and
    returns ``reconnected=False`` (never raises) on ANY failure so the Update
    endpoint never turns a successful-write-but-failed-reconnect into a 500.
    Dispatches to :func:`_reconnect_cloud_system` (gateway mode) or
    :func:`_reconnect_local_system` (local mode); each branch's mechanism and the
    secret_arn-preservation fix are documented on those helpers.
    """
    from .config import get_config
    # Dispatch only — each helper owns its own best-effort try/except (so the
    # scrub uses that branch's resolved config) and never raises.
    if get_config().has_gateway:
        return _reconnect_cloud_system(system_id, connection_type, cat_entry)
    return _reconnect_local_system(system_id, connection_type)


def _reconnect_fail_log(system_id: str, exc: Exception, resolved: dict) -> bool:
    """Shared best-effort failure path for the reconnect helpers: log a
    secret-scrubbed warning (using THIS branch's resolved config so a downstream
    error embedding the token/password cannot leak it) and return False — the
    config write already succeeded, so a failed reconnect is reconnected=False,
    never a 500."""
    from . import connection_resolver as cr
    logger.warning("update_connection: live reconnect failed for %s: %s: %s",
                   system_id, type(exc).__name__, cr._safe_detail(str(exc), resolved))
    return False


def _reconnect_local_system(system_id: str, connection_type: str) -> bool:
    """Local-mode reconnect. Only true streamable-HTTP MCP servers get the
    orchestrator client swap; everything else is invalidate-only.

    The orchestrator ONLY holds streamable-HTTP MCP clients in ``_mcp_clients``;
    SiteWise (Direct)=boto3, SQL=pgdriver and REST/OpenAPI=query_router httpx are
    NOT orchestrator MCP clients. For an MCP-http system, ``remove_system_agent``
    drops the cached client and ``add_system_agent`` re-registers the resolved
    MCP URL (returns False if removed but a valid http(s) URL cannot be re-added,
    never a false True). All other local systems have no client to swap — just
    invalidate the cached secret so the next resolve/query re-resolves the fresh
    config. Best-effort: any failure is scrubbed-logged and returns False."""
    from . import orchestrator as _orch
    from . import connection_resolver as cr
    resolved: dict = {}
    try:
        resolved = cr.resolve_connection(system_id)
        return _reconnect_local_system_inner(system_id, connection_type, resolved, _orch, cr)
    except Exception as exc:  # noqa: BLE001 - reconnect is best-effort; never 500
        return _reconnect_fail_log(system_id, exc, resolved)


def _reconnect_local_system_inner(system_id: str, connection_type: str,
                                  resolved: dict, _orch, cr) -> bool:
    normalized = cr._normalize_type(connection_type, resolved)
    url = (resolved.get("url") or resolved.get("connection_string")
           or resolved.get("base_url") or "")
    scheme = (urlsplit(str(url)).scheme or "").lower() if "://" in str(url) else ""
    if normalized == "mcp" and scheme in ("http", "https"):
        mcp_url = resolved.get("url") or resolved.get("connection_string") or ""
        mcp_scheme = (urlsplit(str(mcp_url)).scheme or "").lower() \
            if "://" in str(mcp_url) else ""
        _orch.remove_system_agent(system_id)
        if mcp_url and mcp_scheme in ("http", "https"):
            _orch.add_system_agent(system_id, mcp_url)
            return True
        logger.warning("update_connection: %s — orchestrator MCP client removed "
                       "but no valid http(s) MCP URL to re-add; reconnected=False",
                       system_id)
        return False
    secret_arn = resolved.get("secret_arn", "") or ""
    if secret_arn:
        cr.invalidate_secret(secret_arn)
    logger.info("update_connection: %s (type=%s) is not an orchestrator MCP "
                "client — cache invalidated; next resolve/query re-resolves "
                "(no client to swap)", system_id, connection_type)
    return True


def _fill_existing_from_catalog(existing: dict, cat_entry: dict) -> dict:
    """Seed the resolved ``existing`` config with the catalog entry's pre-node
    source (secret_arn + non-secret connection_config).

    Bug fix (secret_arn preservation, validate side): for a system that stopped
    at preflight and was NEVER provisioned, ``resolve_connection`` returns an
    UNCONFIGURED result with NO secret_arn (the node is node-only, ADR-0031).
    Editing it (e.g. fixing a wrong target, leaving the secret fields blank =
    keep current) would then build a validate-first candidate with EMPTY
    credentials and fail "credentials incomplete" — even though the user entered
    every credential at Connect time (they live in adt/conn/<id>, pointed to by
    the catalog entry's secret_arn). Carry the catalog entry's secret_arn (+ its
    non-secret config) onto ``existing`` so the candidate can resolve creds. Same
    pre-node catalog-fallback as onboard_preflight._bootstrap_typed_from_catalog.
    """
    out = dict(existing)
    if not out.get("secret_arn") and cat_entry.get("secret_arn"):
        out["secret_arn"] = cat_entry["secret_arn"]
    cat_cfg = cat_entry.get("connection_config") or {}
    if isinstance(cat_cfg, dict):
        for k, v in cat_cfg.items():
            if v and not out.get(k):
                out[k] = v
    return out


def _validate_first_probe(system_id: str, candidate: dict, actor: str):
    """Probe the candidate config BEFORE any write. Returns a ``JSONResponse`` on
    error / non-pass (caller returns it unchanged), else ``None`` to proceed.
    Secret-scrubbed logging: the candidate carries the resolved token/password."""
    from . import connection_resolver as cr
    try:
        result = cr.probe_connection(system_id, candidate)
    except Exception as e:  # noqa: BLE001 - surfaced as a scrubbed 500
        logger.error("update_connection: probe error for %s: %s: %s", system_id,
                     type(e).__name__, cr._safe_detail(str(e), candidate))
        cr.audit_connection_event("update", system_id, actor=actor, outcome="error")
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "Connection update failed (see server logs)"})
    if result.get("status") != "pass":
        cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed",
                                     "reason": result.get("reason"),
                                     "detail": result.get("detail")})
    return None


def _sync_catalog_connection_config(system_id: str, non_secret: dict,
                                    preserve_arn: str) -> None:
    """Mirror a just-written node connection onto the in-memory catalog entry.

    The Neptune node is the durable source, but provision reads the CATALOG's
    connection_config FIRST (onboard._dp_*_connection_inputs). Without this
    write-back an Edit that fixed a wrong base_url/target updated only the node,
    leaving the catalog stale — provision then baked the stale value into the
    per-system agent-config secret (observed live: a base_url held the
    target string, so every tool failed before any HTTP call). Mirrors
    _persist_node_connection: persist the sanitized non-secret subset
    (CATALOG_SAFE_CONFIG_KEYS) + the preserved ARN pointer. Best-effort — the
    node write is already durable, so a catalog hiccup never fails the update.
    """
    from . import connection_resolver as cr
    try:
        from .catalog import catalog
        safe_cfg = cr.sanitize_connection_config(non_secret)
        if safe_cfg or preserve_arn:
            catalog.set_connection_config(system_id, safe_cfg,
                                          secret_arn=preserve_arn or None)
    except Exception:  # nosec B110 - advisory catalog sync; node write already persisted
        logger.warning("update_connection: catalog connection_config sync failed for %s "
                       "(node write already persisted)", system_id)


def _persist_and_reconnect(system_id: str, connection_type: str, non_secret: dict,
                           secret: dict | None, existing: dict, cat_entry: dict,
                           candidate: dict, actor: str, *,
                           schema_move: "tuple[str, str] | None" = None):
    """Persist the connection via ``write_connection``, then best-effort
    live-reconnect + success audit. Returns an error ``JSONResponse`` on a write
    failure, else the masked success dict. A reserved user PostgreSQL schema
    move (``schema_move``) is committed the moment the write lands, before the
    catalog sync, the reconnect and the audit.

    Bug fix (secret_arn preservation, write side): write_connection REWRITES the
    node's secret_arn — passing neither ``secret`` nor ``secret_arn`` writes it
    EMPTY, wiping the credential pointer on an edit that changed only non-secret
    fields. When no NEW secret is supplied, preserve the existing pointer (the
    resolved node's, else the catalog entry's) so a non-credential edit keeps the
    system authenticated (``secret_arn`` is a server-side pointer, never a
    client-supplied value — the write_connection security guardrail).
    """
    from . import connection_resolver as cr
    preserve_arn = (existing.get("secret_arn") or cat_entry.get("secret_arn") or "") if not secret else ""
    refused = _write_connection_or_error(system_id, non_secret, secret, preserve_arn,
                                         cat_entry, candidate, actor, bool(schema_move))
    if refused is not None:
        return refused
    if schema_move:
        # Before anything else: routing by schema must follow the new DSN now.
        _commit_schema_move(system_id, schema_move)
    _sync_catalog_connection_config(system_id, non_secret, preserve_arn)
    # Write succeeded and is persisted. Best-effort: drop the records-first
    # schema→conn-params cache entry so the hot query_pg path picks up the new
    # connection immediately (no schema → clear all; harmless, repopulates on the
    # next read). Never fails the update.
    try:
        from .db import invalidate_schema_conn_cache
        invalidate_schema_conn_cache(None if schema_move
                                     else (cat_entry.get("schema") or None))
    except Exception:  # nosec B110 - advisory cache drop; the write is already persisted
        pass
    # The success audit fires INDEPENDENT of the reconnect outcome (reconnect is
    # best-effort and never raises) — reconnected is recorded in the audit extra.
    reconnected = _reconnect_system(system_id, connection_type, cat_entry)
    cr.audit_connection_event("update", system_id, actor=actor, outcome="ok",
                              extra={"reconnected": reconnected})
    return {"status": "ok", "system_id": system_id,
            "connection": cr.mask_connection(non_secret), "reconnected": reconnected}


def _write_connection_or_error(system_id: str, non_secret: dict, secret, preserve_arn: str,
                               cat_entry: dict, candidate: dict, actor: str,
                               allow_schema_change: bool):
    """``write_connection``; ``None`` when it landed, else the error response
    (a blocked target 400, any other failure a scrubbed 500, both audited)."""
    from . import connection_resolver as cr
    try:
        # ``entry``: a user registration not yet provisioned has its own
        # registration pointer only on the catalog entry; write_connection keeps
        # that secret's binding and lands a user PostgreSQL credentials edit in it.
        cr.write_connection(system_id, non_secret=non_secret, secret=secret or None,
                            secret_arn=preserve_arn or None, entry=cat_entry,
                            **({"allow_schema_change": True} if allow_schema_change else {}))
    except cr.ConnectionTargetError:
        cr.audit_connection_event("update", system_id, actor=actor, outcome="blocked_target")
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed", "reason": "blocked_target",
                                     "detail": cr._REASON_DETAIL["blocked_target"]})
    except Exception as e:  # noqa: BLE001 - surfaced as a scrubbed 500
        logger.error("update_connection: write error for %s: %s: %s", system_id,
                     type(e).__name__, cr._safe_detail(str(e), candidate))
        cr.audit_connection_event("update", system_id, actor=actor, outcome="error")
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "Connection update failed (see server logs)"})
    return None


class _CurrentSecretUnreadable(Exception):
    """The system's stored credentials could not be read, so a partial
    credential edit cannot be merged and nothing is written."""


def _resolve_existing(system_id: str, cat_entry: dict) -> dict:
    """The system's CURRENT resolved config (fail-soft), seeded with the
    pre-node catalog source (see :func:`_fill_existing_from_catalog`).

    A USER PostgreSQL or MCP Server registration not yet provisioned (no graph
    node) is the exception: its current connection IS its registration
    (``connection_resolver.registration_connection``: entry settings + its own
    bound ``adt/conn/<id>`` secret: the DSN with user, database and
    search_path, or the MCP URL and token; own pointer), so an Edit form that
    sends only host/port/password assembles a full DSN from it, and an MCP
    token-only rotation keeps the registered URL. The rotation lands in the
    registration's own secret with its binding (``write_connection(entry=)``).
    An own secret that cannot be read raises :class:`_CurrentSecretUnreadable`:
    nothing is changed.

    "Not yet provisioned" is decided by a CHECKED node read
    (``connection_resolver.node_state_checked``): ``resolve_connection``
    also reports an unconfigured node when the graph read fails, so a graph
    error, or a node pointer or config the checked read finds although the
    unchecked one saw no node, raises :class:`_CurrentSecretUnreadable` instead
    of sending a system that has a node down the registration path. A user
    PostgreSQL node that holds settings but no credentials pointer raises
    :class:`_CredentialsNotLinked` (see :func:`_user_pg_node_without_pointer`)."""
    from . import connection_resolver as cr
    try:
        existing = dict(cr.resolve_connection(system_id))
    except Exception:  # nosec B110 - fail-soft; parser/candidate fall back to submitted values
        existing = {}
    ctype = cat_entry.get("connection_type")
    if (existing.get("_unconfigured") and cr.is_user_registration(cat_entry)
            and ctype in _REGISTRATION_EDIT_TYPES):
        _require_no_configured_node(system_id)
        registered = dict(cr.registration_connection(system_id, cat_entry))
        if registered.get("_secret_unresolved") or not registered.get("connection_string"):
            raise _CurrentSecretUnreadable("registration")
        return registered
    if _user_pg_node_without_pointer(existing, cat_entry):
        raise _CredentialsNotLinked(system_id)
    return _fill_existing_from_catalog(existing, cat_entry)


#: User registrations whose Edit before the first provision reads the current
#: connection from the registration (URL / DSN + its own bound secret).
_REGISTRATION_EDIT_TYPES = ("PostgreSQL", "MCP Server")


class _CredentialsNotLinked(Exception):
    """A user PostgreSQL system's node holds connection settings but no
    pointer to its credentials: nothing is changed (see
    :func:`_credentials_not_linked`)."""


def _user_pg_node_without_pointer(existing: dict, cat_entry: dict) -> bool:
    """True for a USER PostgreSQL registration whose graph node is configured
    (it holds connection settings) but points at no secret — a half-written or
    legacy node. Its settings carry no user, so a candidate built from them can
    only fail the probe; the registration's pointer on the catalog entry is not
    borrowed to fill the gap."""
    from . import connection_resolver as cr
    return (bool(existing) and not existing.get("_unconfigured")
            and not existing.get("secret_arn")
            and cr.is_user_registration(cat_entry)
            and cat_entry.get("connection_type") == "PostgreSQL")


def _credentials_not_linked(system_id: str, actor: str):
    from . import connection_resolver as cr
    logger.warning("update_connection: %s node has connection settings but no credentials "
                   "pointer; nothing written", system_id)
    cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
    return JSONResponse(status_code=400, content={
        "status": "validation_failed", "reason": "credentials_not_linked",
        "detail": "This system's stored connection is not linked to its credentials, so "
                  "it cannot be edited and nothing was changed. Remove the system (its "
                  "registration is kept) and onboard it again: that links the credentials "
                  "it was registered with. Then edit the connection if it still needs a "
                  "change."})


def _require_no_configured_node(system_id: str) -> None:
    """Raise :class:`_CurrentSecretUnreadable` unless a CHECKED graph read
    succeeds and finds neither a node pointer nor a node config for
    ``system_id`` (a half-written node with a config and no pointer is a node,
    not "not yet provisioned")."""
    from . import connection_resolver as cr
    from .db import GraphQueryError
    try:
        state = cr.node_state_checked(system_id)
    except GraphQueryError as exc:
        raise _CurrentSecretUnreadable("graph") from exc
    if state["secret_arn"] or state["has_config"]:
        raise _CurrentSecretUnreadable("node")


def _is_own_conn_secret(secret_arn: str, system_id: str) -> bool:
    """True when ``secret_arn`` names this system's ``adt/conn/<id>`` secret (a
    full ARN carries Secrets Manager's 7-char ``-XXXXXX`` suffix; a bare name
    does not). A shared secret, such as a database secret, is never this."""
    name = str(secret_arn or "").split(":secret:", 1)[-1]
    own = f"adt/conn/{system_id}"
    return name == own or (name.startswith(own + "-") and len(name) == len(own) + 7)


def _stored_secret_values(system_id: str, existing: dict) -> dict:
    """Every key currently stored in this system's own ``adt/conn/<id>`` secret
    ({} when the system points at no secret, or at one it does not own).
    Raises :class:`_CurrentSecretUnreadable` when the secret exists but cannot
    be read; a missing secret has nothing to keep."""
    from . import connection_resolver as cr
    arn = existing.get("secret_arn") or ""
    if not _is_own_conn_secret(arn, system_id):
        return {}
    try:
        return dict(cr.read_secret_uncached(arn) or {})
    except Exception as exc:  # noqa: BLE001 - classified below; values never logged
        if type(exc).__name__ == "ResourceNotFoundException":
            return {}
        raise _CurrentSecretUnreadable(type(exc).__name__) from exc


def _moves_workgroup(existing: dict, non_secret: dict) -> bool:
    """True when the Edit names a different Athena workgroup than the stored one
    (``non_secret`` holds a workgroup only for an Athena system)."""
    new = non_secret.get(_ATHENA_WORKGROUP_FIELD)
    return bool(new) and new != existing.get(_ATHENA_WORKGROUP_FIELD)


def _merge_with_stored(system_id: str, existing: dict, non_secret: dict,
                       secret: dict | None):
    """Overlay the submitted fields on the stored ones. The Edit form sends a
    blank secret field as "keep current" and may send only the setting being
    fixed, so replacing instead of merging would delete the rest (rotating one
    of three credentials, or fixing base_url and losing ``target``). Stored
    non-secret keys come from ``existing`` limited to the node allowlist;
    stored credentials are read from the system's own secret, and only when
    the Edit submits a credential (a settings-only Edit keeps the pointer)."""
    from . import connection_resolver as cr
    stored = {k: v for k, v in existing.items()
              if k in cr._NODE_CONFIG_KEYS and v not in (None, "")}
    if _moves_workgroup(existing, non_secret):
        # The results location belonged to the old workgroup: drop it so the
        # new workgroup's own location applies (Athena sends a stored one).
        stored.pop("output_location", None)
    merged_non_secret = {**stored, **non_secret}
    if not secret:
        return merged_non_secret, secret
    return merged_non_secret, {**_stored_secret_values(system_id, existing), **secret}


def _fields_the_secret_overrides(body: dict, existing: dict, secret: dict | None) -> list[str]:
    """Submitted fields whose new value would never be read back.

    ``resolve_connection`` merges the credentials secret LAST, so any field the
    secret also holds comes back from the secret. A CDK database secret, for
    example, carries ``host``, ``port`` and ``dbname`` as well as the password.
    The secret that will be read after this Edit is the merged one being
    written (``secret``), else the one the system already points at. Returns
    key NAMES only. An unreadable secret blocks nothing: the check cannot know,
    and the probe and write paths keep their own handling."""
    from . import connection_resolver as cr
    if secret:
        effective = secret
    else:
        arn = existing.get("secret_arn") or ""
        if not arn:
            return []
        try:
            effective = dict(cr.read_secret_uncached(arn) or {})
        except Exception:  # nosec B110 - advisory check; never blocks on an unreadable secret
            return []
        if _rewritten_by_the_edit(existing, effective):
            return []
    return sorted(
        key for key, value in (body or {}).items()
        if str(key).lower() != "connection_type" and value not in (None, "")
        and key in effective and effective[key] not in (None, "")
        and str(effective[key]) != str(value))


def _rewritten_by_the_edit(existing: dict, effective: dict) -> bool:
    """True when the secret the system points at is a USER PostgreSQL
    registration's own bound secret: ``write_connection`` rewrites it from the
    edit's assembled connection string (``_rotated_user_pg_secret``), so every
    field it holds IS read back and none needs refusing."""
    from . import connection_resolver as cr
    sid = str(existing.get("system_id") or "")
    return (cr.is_own_connection_secret(existing.get("secret_arn"), sid)
            and cr.is_bound_user_secret(effective, sid)
            and str(effective.get("connection_string") or "").startswith("postgres"))


def _deployment_credentials_edit(system_id: str, cat_entry: dict, existing: dict,
                                 body: dict) -> bool:
    """True when an Edit of a PostgreSQL system that runs on the DEPLOYMENT's
    database credentials (a demo / workshop system: not a user registration,
    and no credentials of its own, :func:`_runs_on_deployment_credentials`)
    submits something those credentials cannot serve: a
    password (it rides inline in the assembled DSN and is stripped at rest, so
    it is never stored), another host, or another database (the deployment's
    credentials only go to its own database, ``_refuse_foreign_env_target``).
    A port change on the same host is not one."""
    from . import connection_resolver as cr
    if ("postgres" not in str(cat_entry.get("connection_type") or "").lower()
            or cr.is_user_registration(cat_entry)
            or not _runs_on_deployment_credentials(existing)):
        return False
    body = body if isinstance(body, dict) else {}
    cs = str(body.get("connection_string") or "")
    try:
        parts = urlsplit(cs) if "://" in cs else None
        dsn_host, dsn_password = (parts.hostname or "", parts.password) if parts else ("", None)
    except ValueError:
        return False                    # left to the parser's own refusal
    password = dsn_password or any(str(k).lower() == "password" and v for k, v in body.items())
    new_host = str(body.get("host") or dsn_host or "").strip().lower()
    new_db = parts.path.lstrip("/") if parts else ""
    cur_host = str(existing.get("host") or "").strip().lower()
    cur_db = str(existing.get("dbname") or "")
    return bool(password or (new_host and new_host != cur_host)
                or (new_db and cur_db and new_db != cur_db))


def _runs_on_deployment_credentials(existing: dict) -> bool:
    """True when the resolved connection holds no credentials of the system's
    own: its pointer is a shared database secret or none (never an ``adt/conn/``
    or ``adt/systems/`` per-system secret), and its connection string carries
    no inline password. Such a system's password comes from that shared
    secret or the deployment's env."""
    name = str(existing.get("secret_arn") or "").split(":secret:", 1)[-1]
    if name.startswith(("adt/conn/", "adt/systems/")):
        return False
    cs = str(existing.get("connection_string") or "")
    try:
        return not (urlsplit(cs).password if "://" in cs else None)
    except ValueError:
        return True


def _deployment_credentials_refused(system_id: str, actor: str):
    from . import connection_resolver as cr
    cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
    return JSONResponse(status_code=400, content={
        "status": "validation_failed", "reason": "deployment_credentials",
        "detail": "This system uses the deployment's own database credentials, which only "
                  "go to the deployment's database. A different host or database, or a "
                  "password entered here, would never be used, so nothing was changed. To "
                  "connect another database, register it with Connect New System."})


def _set_by_secret(system_id: str, fields: list[str], actor: str):
    from . import connection_resolver as cr
    cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
    names = ", ".join(fields)
    return JSONResponse(status_code=400, content={
        "status": "validation_failed", "reason": "set_by_secret",
        "detail": (f"Not saved: {names} {'is' if len(fields) == 1 else 'are'} also "
                   "stored in the credentials secret this system reads, and the "
                   "secret's value is the one used. Change it in that secret instead.")})


def _current_secret_unreadable(system_id: str, actor: str):
    from . import connection_resolver as cr
    logger.error("update_connection: %s: current credentials unreadable; nothing written",
                 system_id)
    cr.audit_connection_event("update", system_id, actor=actor, outcome="error")
    return JSONResponse(status_code=500, content={
        "status": "error",
        "detail": "Could not read the current credentials, so nothing was changed. "
                  "Try again; if it keeps failing, check the service's access to "
                  "Secrets Manager (see server logs)."})


#: Body fields that name a connection DESTINATION for some connector type (a
#: The ONLY body fields an ARN-sourced system's admin Edit may carry: the
#: connection type (which must MATCH the registered type) and metadata that
#: cannot change where a credentialed request goes. Everything else -- every
#: routing/target field, auth field, AND probe-path field like ``health_path``
#: -- is refused, so the edit can never move the secret-derived request. An
#: allowlist, not a destination denylist: a new body field is refused by
#: default rather than silently accepted.
_ARN_SOURCED_EDITABLE_FIELDS = frozenset({
    "connection_type",   # must equal the registered type (checked below)
    "name", "description", "query_guidance", "capabilities",
})


def _refuse_arn_sourced_type_or_target(system_id: str, cat_entry: dict, existing: dict,
                                       body: dict, connection_type: str, actor: str):
    """The 400 an Edit returns on an ARN-sourced system when the body carries a
    field it may not change, else ``None`` (dropping any accepted no-op field
    from ``body`` in place). The key of a system whose credentials come from a
    request-NAMED secret is only ever sent to that secret's own endpoint, so an
    Edit may change ONLY metadata (:data:`_ARN_SOURCED_EDITABLE_FIELDS`): a
    pinned routing field equal to the secret's value is a dropped no-op
    (:func:`_pinned_body_conflict`), a mismatch is refused (``set_by_secret``),
    a different ``connection_type`` is refused (``type_fixed_by_secret``), and
    every OTHER field (an auth field, the REST probe's ``health_path``, an
    unknown future field) is refused (``field_fixed_by_secret``). Reads no
    secret; fail-open when not ARN-sourced."""
    from . import connection_resolver as cr
    existing = existing or {}
    arn_sourced = (str(existing.get("credential_source") or "") == "secret_arn"
                   or str(((cat_entry or {}).get("connection_config") or {}
                           ).get("credential_source") or "") == "secret_arn")
    if not arn_sourced:
        return None

    def _refuse(reason: str, detail: str):
        cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
        return JSONResponse(status_code=400, content={
            "status": "validation_failed", "reason": reason, "detail": detail})

    registered_type = str((cat_entry or {}).get("connection_type") or "")
    submitted_type = str((body or {}).get("connection_type") or "").strip()
    if submitted_type and registered_type and submitted_type != registered_type:
        return _refuse("type_fixed_by_secret",
                       "Not saved: this system's credentials come from a named secret, so its "
                       "connection type cannot be changed here.")
    conflict = _pinned_body_conflict(body, existing, registered_type or connection_type)
    if conflict:
        return _refuse("set_by_secret",
                       f"Not saved: {conflict} is fixed by the credentials secret this system "
                       "reads and cannot be changed here; leave it blank to use the "
                       "secret's value.")
    # Allowlist: refuse every REMAINING body field that is not metadata
    # (health_path and any auth/unknown field included).
    refused = _non_metadata_fields(body)
    if refused:
        return _refuse("field_fixed_by_secret",
                       "Not saved: this system's credentials come from a named secret and are "
                       "only ever sent to that secret's own endpoint; this edit may change only "
                       f"its name and description. Remove {', '.join(refused)} and try again.")
    return None


def _non_metadata_fields(body: dict) -> list:
    """The filled body fields an ARN-sourced Edit may not change (every one
    outside :data:`_ARN_SOURCED_EDITABLE_FIELDS`), sorted."""
    return sorted(k for k in (body or {})
                  if str(k).lower() not in _ARN_SOURCED_EDITABLE_FIELDS
                  and str(body[k] or "").strip())


def _pinned_body_conflict(body: dict, existing: dict, connection_type: str) -> str:
    """The first pinned field (``connection_pinning.pinned_fields`` of
    ``connection_type``) whose submitted value disagrees with the value the
    system resolves (``existing`` already has the secret merged), or ``""``.
    Every submitted pinned field that MATCHES is a no-op re-save and is DROPPED
    from ``body`` in place, so nothing is probed or written against it."""
    from . import connection_pinning as cp
    fields = cp.pinned_fields(connection_type)
    pinned = {k: existing[k] for k in fields if str(existing.get(k) or "").strip()}
    for field in fields:
        if field not in (body or {}) or not str(body[field] or "").strip():
            continue
        if cp.first_pin_conflict({field: pinned.get(field)}, {field: body[field]},
                                 fields=fields):
            return field
        body.pop(field, None)   # matching no-op: drop it
    return ""


def _refuse_pinned_field_conflict(system_id: str, existing: dict, secret, body: dict,
                                  actor: str, *, connection_type: str = ""):
    """The 400 an Edit returns when the body changes a field that is PINNED
    for this system (``connection_pinning.pinned_fields`` of
    ``connection_type``: Tulip's set (``base_url`` / ``workspace_id`` / ``key_scope`` /
    ``table_label_prefix``, every other type's too), else ``None``.

    A field is pinned when the system takes its credentials from a named secret
    (``credential_source == "secret_arn"``), or when the credentials secret
    itself carries that non-secret field. The pinned VALUE is the one the system
    currently resolves (``existing`` already has the secret merged, so it holds
    the secret's url/workspace when present, else the node's). The key must never
    be probed or stored against a url/workspace/scope the caller chose. Reads no
    secret itself; fail-open on nothing to pin."""
    from . import connection_pinning as cp
    from .connection_types import SECRET_ARN_CREDENTIAL_TYPES
    existing = existing or {}
    arn_sourced = str(existing.get("credential_source") or "") == "secret_arn"
    # Pin to: the merged secret being written when IT carries pinned fields;
    # else, for an ARN-sourced system, the values it currently resolves
    # (secret-first). A non-ARN system with a url on its node is NOT pinned —
    # only a named-secret source or a secret that itself names the url is.
    # The type's own pinned set for a named-secret source; every other system
    # keeps the original set, so an inline Edit is checked exactly as before.
    fields = (cp.pinned_fields(connection_type or existing.get("connection_type"))
              if arn_sourced else cp.PINNED_SECRET_FIELDS)
    secret_pins = cp.pinned_values(secret, fields=fields) if secret else {}
    if secret_pins:
        pinned = secret_pins
    elif arn_sourced:
        pinned = {k: v for k, v in existing.items()
                  if k in fields and str(v or "").strip()}
    else:
        pinned = {}
    if not pinned:
        return None
    _ = SECRET_ARN_CREDENTIAL_TYPES  # the opted-in set; the node marker is the gate
    submitted = {k: v for k, v in (body or {}).items()
                 if str(k).lower() in fields}
    conflict = cp.first_pin_conflict(pinned, submitted, fields=fields)
    if conflict:
        from . import connection_resolver as cr
        cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
        return JSONResponse(status_code=400, content={
            "status": "validation_failed", "reason": "set_by_secret",
            "detail": (f"Not saved: {conflict} is fixed by the credentials secret this system "
                       "reads and cannot be changed here; leave it blank to use the secret's "
                       "value.")})
    return None


def _do_update_connection(system_id: str, request: Request, body: dict,
                          *, connection_type: str | None = None,
                          validate: bool = True):
    """Shared implementation for the connection-update flow (admin-only).

    Steps: admin-gate → parse/validate inputs → (validate=True) VALIDATE-FIRST
    probe (400 + no-write on non-pass) → persist via ``write_connection`` →
    best-effort live-reconnect → audit → masked echo. ``validate=False`` is the
    legacy ``highbyte-token`` unconditional-store primitive (no pre-write probe;
    the SSRF/scheme guard in ``write_connection`` still applies). Pre-node catalog
    fallback + secret_arn preservation live in ``_fill_existing_from_catalog`` /
    ``_persist_and_reconnect``. Everything after the admin gate runs under the
    system's keyed connection lock (``connection_lock``): two offloaded edits of
    one system, or an edit and that system's provision, never interleave; a
    lock that stays busy past its timeout is a retryable 409; while a provision
    of the system holds the lock (it does through runtime creation, minutes)
    the 409 comes at once (``fail_fast_behind``), saying so. A removal of the
    system publishes its running claim under the same lock, so an Edit that
    takes the lock after it refuses (409) before its first read or write. A
    body that names a Secrets Manager secret is refused before all of that
    (:func:`_refuse_credential_source_switch`).
    """
    _check_admin(request)
    refusal = _refuse_credential_source_switch(system_id, request, body)
    if refusal is not None:
        return refusal
    from .connection_lock import PROVISION, ConnectionBusyError, connection_lock
    try:
        with connection_lock(system_id, fail_fast_behind=(PROVISION,)):
            refusal = _refuse_if_reset_in_progress(system_id, request)
            if refusal is not None:
                return refusal
            return _update_connection_locked(system_id, request, body,
                                             connection_type, validate)
    except ConnectionBusyError as exc:
        return _connection_busy(system_id, request, exc.holder)


def _refuse_credential_source_switch(system_id: str, request: Request, body: dict):
    """The 400 an Edit returns when its body names a Secrets Manager secret
    (a non-blank ``secret_arn``, any case), else ``None``. Where a system's
    credentials come from (inline, or a secret named when it was connected)
    is chosen at registration; an Edit changes settings or rotates inline
    credentials and never switches the source. So the field is refused for
    every connection type, before anything is read, probed or written, instead
    of being dropped while the rest of the Edit goes through. Audited (no
    values); the message names no value."""
    named = any(str(key).lower() == "secret_arn" and str(value or "").strip()
                for key, value in (body or {}).items())
    if not named:
        return None
    from . import connection_resolver as cr
    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
    return JSONResponse(status_code=400, content={
        "status": "validation_failed", "reason": "credential_source_not_editable",
        "detail": "Not saved: an Edit cannot change where this system's credentials come "
                  "from. To use a Secrets Manager secret, connect the system again and "
                  "choose that secret as its credentials."})


def _refuse_if_reset_in_progress(system_id: str, request: Request):
    """The 409 an Edit returns when a removal of ``system_id`` is in progress
    or did not finish (``onboard._reset_conflict_or_none``), else ``None``:
    the Edit must not rewrite the secret or node a removal is deleting.
    Audited (no secrets)."""
    from . import onboard as _onboard
    from . import connection_resolver as cr
    conflict = _onboard._reset_conflict_or_none(system_id)
    if not conflict:
        return None
    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    cr.audit_connection_event("update", system_id, actor=actor, outcome="reset_in_progress")
    return JSONResponse(status_code=409, content={
        "status": "error", "reason": conflict.get("reason", "reset_in_progress"),
        "detail": conflict.get("error", "This system is being removed.")})


def _connection_busy(system_id: str, request: Request, holder: str = ""):
    from . import connection_resolver as cr
    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    cr.audit_connection_event("update", system_id, actor=actor, outcome="busy")
    return _busy_response(holder)


def _busy_response(holder: str = ""):
    """The retryable 409 every keyed-lock connection change returns when the
    system's lock stays busy past its timeout (nothing was written), or at
    once while the system is being provisioned."""
    from .connection_lock import PROVISION
    if holder == PROVISION:
        return JSONResponse(status_code=409, content={
            "status": "error", "reason": "provisioning",
            "detail": "This system is being provisioned. Nothing was changed; try again "
                      "when provisioning finishes."})
    return JSONResponse(status_code=409, content={
        "status": "error", "reason": "busy",
        "detail": "Another change to this system's connection is in progress. "
                  "Nothing was changed; try again in a moment."})


def _update_connection_locked(system_id: str, request: Request, body: dict,
                              connection_type: str | None, validate: bool):
    """The body of :func:`_do_update_connection`, run under the system's lock."""
    from .catalog import catalog
    from . import connection_resolver as cr

    cat_entry = catalog.get(system_id)
    if cat_entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")

    connection_type = (connection_type or body.get("connection_type")
                       or cat_entry.get("connection_type") or "")
    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"

    # Resolve the CURRENT config (fail-soft), then seed the pre-node catalog source.
    try:
        existing = _resolve_existing(system_id, cat_entry)
    except _CurrentSecretUnreadable:
        return _current_secret_unreadable(system_id, actor)
    except _CredentialsNotLinked:
        return _credentials_not_linked(system_id, actor)

    # For an ARN-sourced system the registered connection type is authoritative:
    # a body may not switch the type or name an alternate destination field, or
    # the secret-derived token would be sent to a URL the caller chose.
    type_refusal = _refuse_arn_sourced_type_or_target(
        system_id, cat_entry, existing, body, connection_type, actor)
    if type_refusal is not None:
        return type_refusal

    non_secret, secret, err = _parse_connection_body(body, connection_type, existing=existing,
                                                     user_mcp=_is_user_mcp(cat_entry),
                                                     stored_type=cat_entry.get("connection_type") or "")
    if err:
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed", "reason": "invalid_input",
                                     "detail": err})
    non_secret["connection_type"] = connection_type
    # An Edit changes only what it sends: overlay it on the stored config/secret.
    try:
        non_secret, secret = _merge_with_stored(system_id, existing, non_secret, secret)
    except _CurrentSecretUnreadable:
        return _current_secret_unreadable(system_id, actor)
    overridden = _fields_the_secret_overrides(body, existing, secret)
    if overridden:
        return _set_by_secret(system_id, overridden, actor)
    pin_refusal = _refuse_pinned_field_conflict(system_id, existing, secret, body, actor)
    if pin_refusal is not None:
        return pin_refusal
    if _deployment_credentials_edit(system_id, cat_entry, existing, body):
        return _deployment_credentials_refused(system_id, actor)

    # VALIDATE-FIRST (validate=True only): candidate = current overlaid with the
    # submitted values; probe and do NOT write on a non-pass result.
    candidate = {**existing, **non_secret, "connection_type": connection_type,
                 **(secret or {})}
    return _probe_and_persist_moving_schema(system_id, connection_type, non_secret, secret,
                                            existing, cat_entry, candidate, actor, validate)


def _probe_and_persist_moving_schema(system_id: str, connection_type: str, non_secret: dict,
                                     secret, existing: dict, cat_entry: dict,
                                     candidate: dict, actor: str, validate: bool):
    """:func:`_probe_then_persist`, around a user PostgreSQL schema move: the
    new schema is RESERVED first (the entry keeps routing on the old one),
    committed the moment the write lands (:func:`_persist_and_reconnect`,
    before the reconnect and the audit); any other end drops only the
    reservation."""
    schema_move = _pg_schema_move(cat_entry, non_secret)
    if schema_move:
        refused = _reserve_new_schema(system_id, schema_move, actor)
        if refused is not None:
            return refused
    try:
        return _probe_then_persist(system_id, connection_type, non_secret, secret,
                                   existing, cat_entry, candidate, actor, validate,
                                   schema_move)
    finally:
        if schema_move:   # a no-op once the write committed the move
            _release_schema_reservation(system_id, schema_move)


def _probe_then_persist(system_id: str, connection_type: str, non_secret: dict, secret,
                        existing: dict, cat_entry: dict, candidate: dict, actor: str,
                        validate: bool, schema_move: "tuple[str, str] | None"):
    """The validate-first probe (``validate=True``), then the write. An Edit
    that changes nothing in a connection stored as fields re-syncs instead."""
    if _unchanged_pg_fields_edit(cat_entry, existing, non_secret, secret):
        return _reconnect_unchanged(system_id, connection_type, non_secret, cat_entry, actor)
    if validate:
        probe_err = _validate_first_probe(system_id, candidate, actor)
        if probe_err is not None:
            return probe_err
    refused, warnings = _registered_pg_login_gate(system_id, cat_entry, connection_type,
                                                  candidate)
    if refused is not None:
        from . import connection_resolver as cr
        cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed",
                                     "reason": "privileged_login", "detail": refused})
    result = _persist_and_reconnect(system_id, connection_type, non_secret, secret,
                                    existing, cat_entry, candidate, actor,
                                    schema_move=schema_move)
    if warnings and isinstance(result, dict):
        result = {**result, "warnings": warnings}
    return result


def _registered_pg_login_gate(system_id: str, cat_entry: dict, connection_type: str,
                              candidate: dict) -> tuple:
    """``(refusal, warnings)`` for a USER PostgreSQL Edit's fully merged
    candidate, before anything is written: the advice when the login can do
    more than read (superuser, or CREATE / write in the schema the candidate
    names), the same check onboarding runs
    (``onboard_preflight.check_registered_pg_login``). A check that cannot run
    refuses nothing and returns its warning for the response. Demo systems and
    other connection types: ``(None, [])``."""
    from . import connection_resolver as cr
    from . import onboard_preflight
    if not cr.is_user_registration(cat_entry):
        return None, []
    if "PostgreSQL" not in (cat_entry.get("connection_type"), connection_type):
        return None, []
    dsn = str(candidate.get("connection_string") or "")
    if not dsn:
        return None, [onboard_preflight.LOGIN_UNCHECKED_WARNING]
    try:
        schema = cr.parse_pg_registration_dsn(dsn)[0]["schema"]
    except ValueError:
        schema = str(cat_entry.get("schema") or "public")
    refusal, warnings = onboard_preflight.check_registered_pg_login(dsn, schema)
    if refusal:
        logger.warning("update_connection: %s login refused (privileged_login)", system_id)
    return refusal, warnings


def _pg_schema_move(cat_entry: dict, non_secret: dict) -> tuple[str, str] | None:
    """``(old_schema, new_schema)`` when a USER PostgreSQL registration's Edit
    submits a full registration DSN whose search_path names a schema other than
    the registration's, else ``None`` (an unparseable DSN is left to the write
    path, which refuses it as before)."""
    from . import connection_resolver as cr
    cs = str(non_secret.get("connection_string") or "")
    if not (cr.is_user_registration(cat_entry)
            and cat_entry.get("connection_type") == "PostgreSQL" and cs):
        return None
    try:
        new_schema = cr.parse_pg_registration_dsn(cs)[0]["schema"]
    except ValueError:
        return None
    old_schema = str(cat_entry.get("schema") or "")
    return (old_schema, new_schema) if new_schema != old_schema else None


def _reserve_new_schema(system_id: str, move: tuple[str, str], actor: str):
    """Reserve the new schema for a registration not yet onboarded (the
    catalog's atomic duplicate-schema check, as at registration) while the
    entry keeps routing on the old one; a refusal response (nothing changed)
    or ``None``. An onboarded system keeps its schema: its graph and running
    agent describe the old schema's tables (a removal flips it back to
    'registered' and clears that graph)."""
    from . import connection_resolver as cr
    from .catalog import catalog
    old, new = move
    if (catalog.get(system_id) or {}).get("status") == "onboarded":
        cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
        return JSONResponse(status_code=400, content={
            "status": "validation_failed", "reason": "schema_change_needs_reonboard",
            "detail": f"This system is onboarded with schema '{old}', and its knowledge graph "
                      f"and agent describe that schema's tables, so its schema cannot be "
                      f"changed here and nothing was changed. To use schema '{new}', remove "
                      "the system (also forgetting its registration) and register it again."})
    if not catalog.reserve_schema(system_id, new):
        from .onboard_agent import _schema_owner
        owner = _schema_owner(new) or "another system"
        cr.audit_connection_event("update", system_id, actor=actor, outcome="validation_failed")
        return JSONResponse(status_code=400, content={
            "status": "validation_failed", "reason": "duplicate_schema",
            "detail": f"schema '{new}' is already registered by system {owner}; each "
                      "PostgreSQL system needs its own schema. Nothing was changed."})
    return None


def _release_schema_reservation(system_id: str, move: tuple[str, str]) -> None:
    """Drop the Edit's reservation of the new schema (an aborted move; a no-op
    once :func:`_commit_schema_move` took it). The old schema never left the
    entry, so nothing can have taken it."""
    from .catalog import catalog
    catalog.release_schema_reservation(system_id, move[1])


def _commit_schema_move(system_id: str, move: tuple[str, str]) -> None:
    """Move the entry onto the reserved schema and drop the schema routing
    cache. Called the moment ``write_connection`` landed the new DSN, so
    records-first routing never sends the old schema to the new connection
    (the reconnect that follows can take a while)."""
    from .catalog import catalog
    _old, new = move
    if not catalog.commit_schema(system_id, new):
        logger.error("update_connection: %s wrote schema %s but its reservation was gone; "
                     "the catalog still routes the old schema", system_id, new)
    try:
        from .db import invalidate_schema_conn_cache
        invalidate_schema_conn_cache(None)   # routing by schema just changed
    except Exception:  # nosec B110 - advisory cache drop; the move is committed
        pass


@router.put("/connection/{system_id}", dependencies=[Depends(admin_refuse_while_restoring)])
async def update_connection(system_id: str, request: Request, body: dict):
    """Update a system's live connection (admin-only): validate-first, then write
    + live-reconnect. See :func:`_do_update_connection`. ``body`` is parsed inline
    (no Pydantic) and never echoes secret values back. The probe, the write and
    the reconnect (secret rewrite + runtime roll) run in the offload pool."""
    return await offload(_do_update_connection, system_id, request, body)


@router.get("/connection/{system_id}")
async def get_connection(system_id: str, request: Request):
    """Return a masked, READ-ONLY summary of a system's current connection (admin-only).

    Backend half of the read-only "current connection" display. Admin-gated via
    :func:`_check_admin` (403 non-admin); 404 for an unknown system. The body is
    produced by :func:`connection_resolver.describe_connection`, which resolves the
    connection, masks it, then projects ONLY non-secret fields (host/port/scheme/
    base_url) plus presence indicators (``has_secret`` + the secret-field KEY
    NAMES — never values). A USER PostgreSQL registration not yet provisioned is
    summarised from its registration (``describe_unprovisioned_registration``);
    when its stored connection cannot be read the answer is a 500
    ``{"status": "error", "detail": ...}``, never a guess. The reads run
    offloaded (:func:`_describe_connection_for`).

    BAKE-IN RULE: this endpoint and the ``PUT /connection/{id}`` masked echo are
    the ONLY connection-data paths to the browser, and BOTH pass through
    mask/describe server-side. No token/password/secret VALUE and no inline-cred
    connection string is ever returned (asserted in the unit tests).
    """
    _check_admin(request)
    from .catalog import catalog

    entry = catalog.get(system_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    return await offload(_describe_connection_for, system_id, entry)


def _describe_connection_for(system_id: str, entry: dict):
    """The sync body of :func:`get_connection`: a user PostgreSQL registration's
    summary (registration before its first provision, else its node), every
    other system's node summary exactly as before."""
    from . import connection_resolver as cr
    from .db import GraphQueryError
    from .errors import ConnectionSecretUnavailableError
    try:
        described = cr.describe_unprovisioned_registration(system_id, entry)
    except (GraphQueryError, ConnectionSecretUnavailableError) as exc:
        logger.warning("get_connection: stored connection for %s unreadable (%s)",
                       system_id, type(exc).__name__)
        return JSONResponse(status_code=500, content={
            "status": "error",
            "detail": "Could not read this system's stored connection. Try again; if it "
                      "keeps failing, check the service's access to Secrets Manager and "
                      "the graph (see server logs)."})
    out = described if described is not None else cr.describe_connection(system_id)
    if _is_user_mcp(entry):
        # The scheme a USER MCP token is sent with: the node's (an admin Edit),
        # else the registration default by token shape (registration_connection).
        # The Edit form's "Authorization header" select shows it. Only here:
        # every other system's summary is unchanged.
        registered = cr.registration_connection(system_id, entry)
        out = {**out, "auth_scheme": registered.get("auth_scheme") or ""}
    return out


def _is_user_mcp(entry: dict | None) -> bool:
    """A user-registered MCP Server: the only system whose Edit form has the
    Authorization header select (its summary reports the scheme and its
    update checks it). HighByte and demo MCP systems are not."""
    from . import connection_resolver as cr
    return cr.is_user_registration(entry) and (entry or {}).get("connection_type") == "MCP Server"


@router.get("/scan-budget/{system_id}")
async def get_scan_budget(system_id: str, request: Request):
    """Return a system's per-system Athena scanned-bytes budget (admin-only).

    Admin-gated (403 non-admin); 404 for an unknown system. Reports BOTH the raw
    per-system configured value (``max_scanned_bytes`` — ``None`` when unset) AND
    the ``effective_max_scanned_bytes`` the connector would actually enforce
    (per-system config > ``ADT_ATHENA_MAX_SCANNED_BYTES`` env > safe default). The
    budget is a non-secret config value, so it is returned as-is.
    """
    _check_admin(request)
    from .catalog import catalog
    from . import connection_resolver as cr
    from .athena_tools import resolve_scan_budget

    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    resolved = cr.resolve_connection(system_id)
    configured = resolved.get("max_scanned_bytes")
    try:
        effective = resolve_scan_budget(resolved)
    except Exception:  # noqa: BLE001 - a malformed env override must not 500 the read
        effective = configured
    return {
        "system_id": system_id,
        "connection_type": resolved.get("connection_type"),
        "max_scanned_bytes": configured,
        "effective_max_scanned_bytes": effective,
    }


@router.put("/scan-budget/{system_id}", dependencies=[Depends(admin_refuse_while_restoring)])
async def set_scan_budget(system_id: str, request: Request):
    """Set (or clear) a system's per-system Athena scanned-bytes budget (admin-only).

    Body: ``{"max_scanned_bytes": <positive int> | null}`` — ``null``/omitted clears
    the per-system budget (falls back to the env/default). The value is validated
    fail-closed via the connector's own coercer (a malformed/non-positive value is
    a 400), then persisted with :func:`connection_resolver.write_scan_budget`, a
    TARGETED node write that never touches the system's secret pointer — offloaded,
    under the system's keyed connection lock (:func:`_set_scan_budget_locked`).
    """
    _check_admin(request)
    from .catalog import catalog
    from .athena_tools import AthenaConfigError, _coerce_budget

    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}

    raw = body.get("max_scanned_bytes", None)
    try:
        budget = _coerce_budget(raw, source="max_scanned_bytes")
    except AthenaConfigError as e:
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed",
                                     "reason": "invalid_input", "detail": str(e)})

    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    # The node read-modify-write runs offloaded, under the system's keyed
    # connection lock (see _set_scan_budget_locked).
    return await offload(_set_scan_budget_locked, system_id, budget, actor)


def _set_scan_budget_locked(system_id: str, budget, actor: str):
    """Persist a validated scan budget under ``connection_lock(system_id)``:
    ``write_scan_budget`` reads the node's whole ``connection_config`` and writes
    it back, so an Edit or a provision of the same system landing between the
    two would be undone (the old endpoint restored next to a new credential
    pointer). Busy -> the Edit's retryable 409, nothing written (at once while
    the system is being provisioned)."""
    from . import connection_resolver as cr
    from .connection_lock import PROVISION, ConnectionBusyError, connection_lock
    try:
        with connection_lock(system_id, fail_fast_behind=(PROVISION,)):
            cr.write_scan_budget(system_id, budget)
    except ConnectionBusyError as exc:
        cr.audit_connection_event("scan_budget", system_id, actor=actor, outcome="busy")
        return _busy_response(exc.holder)
    except Exception as e:  # noqa: BLE001 - fail-loud but scrub the detail
        logger.error("set_scan_budget: write error for %s: %s: %s", system_id,
                     type(e).__name__, e)
        cr.audit_connection_event("scan_budget", system_id, actor=actor, outcome="error")
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "Scan budget update failed (see server logs)"})
    cr.audit_connection_event("scan_budget", system_id, actor=actor, outcome="ok",
                              extra={"max_scanned_bytes": budget})
    return {"status": "ok", "system_id": system_id, "max_scanned_bytes": budget}


# Max accepted length for a system_type label (defensive cap; mirrors _MAX_FIELD_LEN intent).
_MAX_SYSTEM_TYPE_LEN = 64


@router.put("/system-type/{system_id}", dependencies=[Depends(admin_refuse_while_restoring)])
async def set_system_type(system_id: str, request: Request):
    """Set (or clear) a system's free-form ``system_type`` label (admin-only).

    Body: ``{"system_type": "<label>"}`` — a GENERIC, free-form descriptive label
    (e.g. ``MES``, ``Historian``, ``ERP``). No value is hardcoded in this handler:
    the specific label is supplied by the caller AT RUNTIME. The label is
    descriptive metadata that drives the fleet UI and onboarding affinity
    suggestions ONLY — concept PRECEDENCE is governed by per-concept
    ``AuthorityRule`` nodes, NOT by ``system_type`` (see
    ``metric_resolver.set_authority`` / ``_resolve_authority``).

    Persisted in BOTH places so it survives an ECS restart (which re-hydrates the
    catalog from Neptune): (1) the durable ``System`` node
    (``SET s.system_type``) — the source restored by ``server._restore`` — and
    (2) the in-process catalog entry (immediate effect for the current replica).
    Admin-gated (403 non-admin); 404 for an unknown system; fail-LOUD on a graph
    write error (500) so a dropped label surfaces rather than silently no-ops.
    """
    _check_admin(request)
    from .catalog import catalog
    from .db import neptune_write

    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed", "reason": "invalid_input",
                                     "detail": "Invalid JSON body"})
    if not isinstance(body, dict) or "system_type" not in body:
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed", "reason": "invalid_input",
                                     "detail": "body must be a JSON object with a 'system_type' field"})
    raw = body.get("system_type")
    if not isinstance(raw, str):
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed", "reason": "invalid_input",
                                     "detail": "'system_type' must be a string"})
    system_type = raw.strip()
    if len(system_type) > _MAX_SYSTEM_TYPE_LEN:
        return JSONResponse(status_code=400,
                            content={"status": "validation_failed", "reason": "invalid_input",
                                     "detail": f"'system_type' exceeds {_MAX_SYSTEM_TYPE_LEN} chars"})

    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    return await offload(_set_system_type_sync, system_id, system_type, actor)


def _set_system_type_sync(system_id: str, system_type: str, actor: str):
    """Sync body of ``set_system_type`` (EVENT-LOOP OFFLOAD PATTERN)."""
    from .catalog import catalog
    from .db import neptune_write
    # (1) Durable: persist onto the System node (the source server._restore reads).
    try:
        ok = neptune_write(
            "MATCH (s:System {id: $sid}) SET s.system_type = $stype",
            {"sid": system_id, "stype": system_type},
        )
    except Exception as e:  # noqa: BLE001 - fail-loud but scrub detail
        logger.error("set_system_type: node write error for %s: %s: %s",
                     system_id, type(e).__name__, e)
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "system_type update failed (see server logs)"})
    if ok is False:
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "system_type update failed (see server logs)"})
    # (2) Immediate: update the in-process catalog entry for this replica.
    catalog.set_system_type(system_id, system_type)
    logger.info("set_system_type: %s -> %r (actor=%s)", system_id, system_type, actor)
    return {"status": "ok", "system_id": system_id, "system_type": system_type}


# --- steward business context (ADR-0054 C4a) --------------------------------

def _bc_validation_failed(reason: str, detail: str) -> JSONResponse:
    """400 body pinned by C4a — reasons: invalid_input | too_long | secret_like_content | fence_delimiter."""
    return JSONResponse(status_code=400,
                        content={"status": "validation_failed",
                                 "reason": reason, "detail": detail})


def _parse_business_context_body(body):
    """Validate a PUT business-context body → ``(text, enabled, None)`` or
    ``(None, None, <400 JSONResponse>)``. Sanitation + the pinned 400 reasons
    come from ``agents.business_context`` (T3) — never duplicated here."""
    from . import business_context as bc
    if not isinstance(body, dict) or "business_context" not in body:
        return None, None, _bc_validation_failed(
            "invalid_input",
            "body must be a JSON object with a 'business_context' field")
    enabled = body.get("enabled", True)  # optional, default true (C4a)
    if not isinstance(enabled, bool):
        return None, None, _bc_validation_failed(
            "invalid_input", "'enabled' must be a boolean")
    text = bc.sanitize_business_context(body.get("business_context"))
    err = bc.validate_business_context(text)
    if err is not None:
        return None, None, _bc_validation_failed(err[0], err[1])
    return text, enabled, None


def _refresh_business_context_best_effort(system_id: str) -> str:
    """Best-effort delivery seam (T6 contract): ``"ok"|"failed"|"skipped"``.

    ``skipped`` = no live AgentManager / no cloud runtime path (local mode).
    A refresh failure NEVER rolls back the durable graph write — it is
    reported truthfully as ``"failed"`` in the PUT response.
    """
    mgr = _get_agent_manager()
    refresh = getattr(mgr, "refresh_business_context", None)
    if not callable(refresh):
        return "skipped"
    try:
        result = refresh(system_id)
    except Exception as e:  # noqa: BLE001 - best-effort; durable write already landed
        logger.error("business_context refresh error for %s: %s: %s",
                     system_id, type(e).__name__, e)
        return "failed"
    return result if result in ("ok", "failed", "skipped") else "failed"


# The BC PUT sits behind the CloudFront 120 s origin cap — the coordinator's
# readiness gate must return BEFORE it (an expired gate is the honest
# ``readiness: "not_ready"`` partial, never a 504-stranded request).
_BC_PUT_ROLL_TIMEOUT_SECONDS = 100


def _refresh_business_context_batched(system_id: str) -> tuple[str, dict | None]:
    """T1.5: route the BC refresh through the batched roll coordinator.

    Returns ``(tri-state status, readiness block | None)``. The readiness
    block is the coordinator's per-system report (``status`` + ``readiness``
    verified flag); ``None`` when no coordinator is available (older/injected
    managers without ``refresh_systems_batched`` — the T6 best-effort seam
    then answers alone, keeping existing callers working). A coordinator
    failure NEVER rolls back the durable graph write.
    """
    mgr = _get_agent_manager()
    batched = getattr(mgr, "refresh_systems_batched", None)
    if not callable(batched):
        return _refresh_business_context_best_effort(system_id), None
    try:
        report = batched([system_id], reason="business-context PUT",
                         artifact_classes=("business_context",),
                         timeout_seconds=_BC_PUT_ROLL_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 - best-effort; durable write already landed
        logger.error("business_context batched refresh error for %s: %s: %s",
                     system_id, type(e).__name__, e)
        return "failed", None
    entry = dict((report or {}).get(system_id) or {})
    status = entry.get("status")
    if status not in ("ok", "failed", "skipped"):
        status, entry["status"] = "failed", "failed"
    return status, {system_id: entry}


@router.get("/business-context/{system_id}")
async def get_business_context_route(system_id: str, request: Request):
    """Offloaded wrapper (the Neptune read must not run on the event loop);
    see ``get_business_context``."""
    return await offload(get_business_context, system_id, request)


def get_business_context(system_id: str, request: Request):
    """Return a system's steward business context (admin-only; ADR-0054 C4a).

    Admin-gated (403 non-admin); 404 for an unknown system. Unset context is
    the honest empty shape: ``business_context: ""``, ``enabled: false``,
    ``updated_at``/``context_hash``: null. The durable source is the Neptune
    ``System`` node (read via ``business_context.read_business_context``).
    Sync body (a graph read); the route offloads it.
    """
    _check_admin(request)
    from .catalog import catalog
    from . import business_context as bc

    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    state = bc.read_business_context(system_id)
    if state is None:  # unreadable graph ≠ unset (ADR-0054 D4) — fail loud
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "business context read failed "
                                               "(see server logs)"})
    return {"system_id": system_id, **state}


def put_business_context(system_id: str, request: Request, body):
    """Write a system's steward business context + A/B toggle (admin-only; C4a).

    Body: ``{"business_context": "<≤2000 chars after strip>", "enabled": true}``
    (``enabled`` optional, default true; ``""`` clears). Flow: admin gate → 404 →
    validation (pinned 400 reasons, T3) → durable graph write (500 fail-loud) →
    best-effort runtime refresh through the T1.5 batched roll coordinator (its
    failure never rolls back the write; the response's additive ``readiness``
    block carries the per-system roll status + warm-up verification) →
    secret-free audit event (hash/length/enabled — NEVER the text). The
    ``enabled`` flag is the A/B toggle, scriptable via this API alone (D4).
    Sync body (graph write, then the runtime roll and its READY polling);
    the route reads the JSON body and offloads this.
    """
    _check_admin(request)
    from .catalog import catalog
    from . import business_context as bc
    from . import connection_resolver as cr

    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    if body is _INVALID_JSON:
        return _bc_validation_failed("invalid_input", "Invalid JSON body")
    text, enabled, error = _parse_business_context_body(body)
    if error is not None:
        return error

    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    try:
        ok = bc.write_business_context(system_id, text, enabled)
    except Exception as e:  # noqa: BLE001 - fail-loud but scrub detail (never the text)
        logger.error("put_business_context: write error for %s: %s: %s",
                     system_id, type(e).__name__, e)
        ok = False
    if not ok:
        cr.audit_connection_event("business_context", system_id, actor=actor,
                                  outcome="error")
        return JSONResponse(status_code=500,
                            content={"status": "error",
                                     "detail": "business context update failed (see server logs)"})
    runtime_refresh, readiness = _refresh_business_context_batched(system_id)
    chash = bc.context_hash(text)
    cr.audit_connection_event("business_context", system_id, actor=actor,
                              outcome="ok",
                              extra={"context_hash": chash, "enabled": enabled,
                                     "length": len(text)})
    return {"status": "ok", "system_id": system_id, "enabled": enabled,
            "context_hash": chash, "runtime_refresh": runtime_refresh,
            "readiness": readiness}


@router.put("/business-context/{system_id}", dependencies=[Depends(admin_refuse_while_restoring)])
async def put_business_context_route(system_id: str, request: Request):
    """Write a system's business context and roll its runtime. Sync body:
    ``put_business_context``. Reading the JSON body is the one async step.

    The system is claimed for the save on the loop (``claim_save``, shared
    with the guided confirm) and released when the write and roll are done,
    so two saves of one system never overlap; the second gets 409."""
    from . import business_context as bc

    _check_admin(request)
    try:
        body = await request.json()
    except Exception:
        body = _INVALID_JSON
    if not bc.claim_save([system_id]):
        raise HTTPException(status_code=409, detail=bc.SAVE_IN_FLIGHT_DETAIL)
    return await offload_holding(lambda: bc.release_save([system_id]),
                                 put_business_context, system_id, request, body)


# --- steward record key (a table's declared record key) --------------------

def _record_key_graph_seams():
    """The graph write/query seams the record-key store uses (lazy, so the
    module imports without the app runtime)."""
    from .db import neptune_query, neptune_write
    return neptune_write, neptune_query


#: Ingress ceiling for the record-key PUT body, enforced BEFORE json.loads.
MAX_RECORD_KEY_BYTES = 16 * 1024
#: Per-field length bounds for the accepted body.
_RK_MAX_TABLE_LEN = 256
_RK_MAX_SOURCE_LEN = 2000
#: Backends whose record key comes from the catalog; a declared key is refused.
_CATALOG_KEYED_BACKENDS = ("postgres", "sql")


def _resolve_steward_table(system_id: str, submitted: str, query):
    """Resolve a steward-submitted table name to the system's actual
    SchemaEntry name, accepting the terminal or the qualified form (so an S3
    Tables entry stored as ``<namespace>.<table>`` is reachable by either). An
    Athena single-part name resolves to itself unchanged.

    Returns ``(resolved_name, None)``, or ``(None, error)`` where ``error`` is
    ``"ambiguous_table"`` (the terminal matches more than one entry). An unknown
    table resolves to the submitted name unchanged, so the store's own
    ``unknown_table`` check reports it."""
    from . import schema_fields
    try:
        resolved = schema_fields.resolve_schema_entry_name(system_id, submitted, query=query)
    except schema_fields.TableNameAmbiguous:
        return None, "ambiguous_table"
    return (resolved or submitted), None


def _schema_rewrite_in_progress(system_id: str, query) -> bool:
    """True while a re-provision's schema-rewrite marker stands on the system's
    row-exclusion manifest (``row_exclusion_manifest.rewrite_of``). The marker
    is the single System-node signal a re-provision raises while it carries the
    steward facts across its schema rewrite; the record-key set/clear honours it
    the same way the row-exclusion PUT does, so a steward write cannot be
    overwritten by the carry's restore (review live-carry F3). A read that
    cannot determine it is treated as not-in-progress, so a transient graph
    fault never blocks every steward write (the carry itself fails closed)."""
    from . import row_exclusion_manifest as manifest
    try:
        raw, _entries = manifest.read(system_id, query=query)
        return manifest.rewrite_of(raw) is not None
    except Exception:  # noqa: BLE001 - cannot read the marker: do not block
        return False


def _rk_reject(status: int, reason: str, detail: str) -> JSONResponse:
    """One rejection shape for the record-key route (``{status, reason,
    detail}``); detail never echoes a rejected value."""
    return JSONResponse(status_code=status,
                        content={"status": "validation_failed" if status < 500 else "error",
                                 "reason": reason, "detail": detail})


def _rk_declared_bytes(request: Request) -> int:
    try:
        return int(request.headers.get("content-length"))
    except (TypeError, ValueError):
        return -1


async def _read_capped_record_key(request: Request) -> bytes:
    """The PUT body under a HARD byte ceiling, two independent gates (the
    ``eval_findings_routes._read_capped`` idiom): an oversized ``Content-
    Length`` is refused with the stream untouched, and the stream read aborts
    the moment the accumulated size crosses the ceiling, so a lying or absent
    header buys nothing."""
    if _rk_declared_bytes(request) > MAX_RECORD_KEY_BYTES:
        raise HTTPException(status_code=413,
                            detail={"reason": "payload_too_large",
                                    "detail": f"body exceeds {MAX_RECORD_KEY_BYTES} bytes"})
    chunks: list = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_RECORD_KEY_BYTES:
            raise HTTPException(status_code=413,
                                detail={"reason": "payload_too_large",
                                        "detail": f"body exceeds {MAX_RECORD_KEY_BYTES} bytes"})
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_record_key_body(raw: bytes) -> dict:
    """The parsed, type-checked PUT body or a 422 (``extra`` forbidden).

    ``table`` and ``source`` are bounded strings, ``record_key`` a list whose
    each element is a column string or an object with exactly ``column`` (a
    string) and an optional ``coalesced`` (a JSON boolean only)."""
    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=422,
                            detail={"reason": "invalid_json", "detail": "body must be a JSON object"})
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail={"reason": "invalid_json", "detail": "body must be a JSON object"})
    allowed = {"table", "record_key", "source"}
    if not set(payload).issubset(allowed):
        raise HTTPException(status_code=422,
                            detail={"reason": "unknown_keys",
                                    "detail": f"allowed keys: {', '.join(sorted(allowed))}"})
    _check_record_key_fields(payload)
    return payload


def _check_record_key_fields(payload: dict) -> None:
    """Field-type checks for :func:`_parse_record_key_body` (422 on any miss)."""
    table = payload.get("table")
    if not isinstance(table, str) or not table.strip() or len(table) > _RK_MAX_TABLE_LEN:
        raise HTTPException(status_code=422,
                            detail={"reason": "invalid_input", "detail": "'table' must be a string"})
    source = payload.get("source", "")
    if not isinstance(source, str) or len(source) > _RK_MAX_SOURCE_LEN:
        raise HTTPException(status_code=422,
                            detail={"reason": "invalid_input", "detail": "'source' must be a string"})
    key = payload.get("record_key")
    if not isinstance(key, list):
        raise HTTPException(status_code=422,
                            detail={"reason": "invalid_input", "detail": "'record_key' must be a list"})
    for item in key:
        _check_record_key_element(item)


def _check_record_key_element(item) -> None:
    if isinstance(item, str):
        return
    if (isinstance(item, dict) and set(item).issubset({"column", "coalesced"})
            and isinstance(item.get("column"), str)
            and isinstance(item.get("coalesced", False), bool)):
        return
    raise HTTPException(status_code=422, detail={
        "reason": "invalid_input",
        "detail": "each record_key element is a column string or "
                  "{column: str, coalesced: bool}"})


def _record_key_actor(request: Request) -> str:
    """The acting user's identity for the record-key provenance stamp, resolved
    the same way the audit does (``_request_actor``): the session email, else
    the username (the demo 'ADT' user has no email, so username is its
    identity), else the literal ``"unknown"`` in auth-disabled/local mode. The
    audit line already named the admin; this keeps the stored declarer from
    diverging to ``"unknown"`` when only an email was read."""
    user = getattr(request.state, "user", None)
    email = getattr(user, "email", None)
    username = getattr(user, "username", None)
    return str(email or username or "unknown")


def set_record_key_sync(system_id: str, body: dict, actor: str, *,
                        connection_type: str = "", seams=None):
    """Sync body of the record-key PUT (admin-gated by the route). Refuses a
    declared key on a catalog-keyed backend (PostgreSQL); otherwise validates
    and stores a table's record key on its SchemaEntry with provenance, or
    clears it when ``record_key`` is an empty list. ``body`` is already parsed
    and type-checked by :func:`_parse_record_key_body`."""
    from . import record_key
    from . import connection_resolver as cr
    ctype = str(connection_type or "").lower()
    if any(b in ctype for b in _CATALOG_KEYED_BACKENDS):
        return _rk_reject(400, "catalog_keyed_backend",
                          "this backend's record key comes from its catalog; a record "
                          "key cannot be declared here")
    key = body["record_key"]
    write, query = seams if seams is not None else _record_key_graph_seams()
    table, name_error = _resolve_steward_table(system_id, body["table"].strip(), query)
    if name_error:
        return _rk_reject(400, name_error,
                          "that table name matches more than one schema entry; use the "
                          "fully-qualified name")
    if _schema_rewrite_in_progress(system_id, query):
        # A re-provision is carrying this system's steward facts across its
        # schema rewrite; a key set now would be overwritten by the carry's
        # restore (review live-carry F3). Refuse, as the row-exclusion PUT does.
        return _rx_conflict("rewrite_in_progress")
    try:
        if key:
            out = record_key.set_record_key(system_id, table, key, actor=actor,
                                            source=str(body.get("source") or ""),
                                            write=write, query=query)
        else:
            out = record_key.clear_record_key(system_id, table, actor=actor,
                                              write=write, query=query)
    except Exception as e:  # noqa: BLE001 - scrub detail
        logger.error("set_record_key: graph error for %s.%s: %s: %s",
                     system_id, table, type(e).__name__, e)
        return JSONResponse(status_code=500, content={
            "status": "error", "detail": "record key update failed (see server logs)"})
    if out.get("status") != "ok":
        return _rk_reject(400, out.get("reason", "invalid_input"),
                          out.get("detail", "invalid record key"))
    cr.audit_connection_event("record_key", system_id, actor=actor, outcome="ok",
                              extra={"table": table, "columns": len(key)})
    return out


@router.put("/record-key/{system_id}")
async def set_record_key_route(system_id: str, request: Request):
    """Set (or clear) a table's declared record key (admin-only).

    Body: ``{"table": "<table>", "record_key": [{"column": "<c>", "coalesced":
    false}, ...], "source": "<steward note>"}``. A bare string element means
    ``coalesced:false``; an empty ``record_key`` clears the fact. The body is
    size-capped before parsing and every field is type-checked. 404 for an
    unknown system, 400 ``catalog_keyed_backend`` for a PostgreSQL system; the
    pinned 400 reasons (unknown_table, unknown_column, duplicate_column,
    empty_key, too_many_columns) come from ``record_key``. Offloaded (the graph
    read/write must not run on the event loop)."""
    _check_admin(request)
    from .catalog import catalog
    cat_entry = catalog.get(system_id)
    if cat_entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    body = _parse_record_key_body(await _read_capped_record_key(request))
    actor = _record_key_actor(request)
    return await offload(set_record_key_sync, system_id, body, actor,
                         connection_type=cat_entry.get("connection_type") or "")


@router.get("/record-key/{system_id}")
async def get_record_key_route(system_id: str, request: Request, table: str):
    """Read a table's declared record key and its provenance (admin-only)."""
    _check_admin(request)
    from .catalog import catalog
    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    return await offload(_get_record_key_sync, system_id, table)


def _get_record_key_sync(system_id: str, table: str):
    from . import record_key
    _write, query = _record_key_graph_seams()
    try:
        resolved, name_error = _resolve_steward_table(system_id, table.strip(), query)
        if name_error:
            return _rk_reject(400, name_error,
                              "that table name matches more than one schema entry; use the "
                              "fully-qualified name")
        return record_key.read_record_key(system_id, resolved, query=query)
    except Exception as e:  # noqa: BLE001 - scrub detail
        logger.error("get_record_key: graph error for %s.%s: %s: %s",
                     system_id, table, type(e).__name__, e)
        return JSONResponse(status_code=500, content={
            "status": "error", "detail": "record key read failed (see server logs)"})


# --- steward row exclusion (rows a table's source owner says are not records) ---

def _row_exclusion_graph_seams():
    """The graph write/query seams the row-exclusion store uses (lazy, so the
    module imports without the app runtime). Both are CHECKED: the write
    answers the rows it wrote (``None`` when refused, ``db.GraphWriteConflict``
    when the graph aborted it on a concurrent modification, which the store
    reads again and resends) and the read raises on a failure, so a failed
    read of the manifest or of a table's existence is never taken for
    "nothing there" (the store's write order depends on it)."""
    from .db import neptune_query_checked, neptune_write_returning_cas
    return neptune_write_returning_cas, neptune_query_checked


#: Type bounds on the accepted body; the label and values have their own
#: tighter bounds in ``row_exclusion``.
_RX_MAX_LABEL_LEN = 256
_RX_MAX_CONDITIONS_LEN = 64


def _rx_invalid(reason: str, detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason": reason, "detail": detail})


def _rx_conflict(reason: str = "concurrent_change") -> JSONResponse:
    """The 409 for a set or clear that was not applied as sent: a concurrent
    change of the SAME table took its manifest entry
    (``row_exclusion_manifest.TokenLost``, ``concurrent_change``), or a
    re-provision is carrying the system's row exclusions across its schema
    rewrite (``RewriteInProgress``, ``rewrite_in_progress``)."""
    detail = ("another change to this table's row exclusion landed while this one was "
              "being applied, so this one was not applied as sent; read the table's "
              "row exclusion, then send the change again if it is still wanted")
    if reason == "rewrite_in_progress":
        detail = ("this system is being re-provisioned, and its row exclusions are being "
                  "carried across its schema rewrite, so this change was not applied as "
                  "sent; once the re-provision ends (if it stopped, provision this system "
                  "again), read the table's row exclusion, then send the change again if it "
                  "is still wanted")
    return JSONResponse(status_code=409, content={
        "status": "conflict", "reason": reason, "detail": detail})


def _parse_row_exclusion_body(raw: bytes) -> dict:
    """The parsed, type-checked PUT body or a 422 (``extra`` forbidden).

    ``table`` is a bounded non-empty string, ``label`` and ``source`` optional
    bounded strings, ``conditions`` a list of objects of exactly ``column`` (a
    string), ``op`` (a string) and an optional ``values`` (a list of strings).
    What those values may be is ``row_exclusion``'s check."""
    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _rx_invalid("invalid_json", "body must be a JSON object")
    if not isinstance(payload, dict):
        raise _rx_invalid("invalid_json", "body must be a JSON object")
    allowed = {"table", "label", "conditions", "source"}
    if not set(payload).issubset(allowed):
        raise _rx_invalid("unknown_keys", f"allowed keys: {', '.join(sorted(allowed))}")
    table = payload.get("table")
    if not isinstance(table, str) or not table.strip() or len(table) > _RK_MAX_TABLE_LEN:
        raise _rx_invalid("invalid_input", "'table' must be a string")
    for key, bound in (("label", _RX_MAX_LABEL_LEN), ("source", _RK_MAX_SOURCE_LEN)):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value) > bound:
            raise _rx_invalid("invalid_input", f"'{key}' must be a string")
    conditions = payload.get("conditions")
    if not isinstance(conditions, list) or len(conditions) > _RX_MAX_CONDITIONS_LEN:
        raise _rx_invalid("invalid_input", "'conditions' must be a list")
    for item in conditions:
        if not (isinstance(item, dict) and set(item).issubset({"column", "op", "values"})
                and isinstance(item.get("column"), str) and isinstance(item.get("op"), str)
                and isinstance(item.get("values", []), list)
                and all(isinstance(v, str) for v in item.get("values", []))):
            raise _rx_invalid("invalid_input", "each condition is {column: str, op: str, "
                                               "values?: [str]}")
    return payload


def _row_exclusion_target(system_id: str, body: dict, connection_type: str, query):
    """``(table, None)`` with the resolved SchemaEntry name, or ``(None,
    rejection)``: a 400 ``unsupported_backend`` for a backend other than Athena
    or S3 Tables (both enforce the exclusion in their own read guard), or a 400
    ``ambiguous_table`` when the submitted name matches more than one entry."""
    from .connection_types import ATHENA, S3TABLES_DUCKDB
    if str(connection_type or "") not in (ATHENA, S3TABLES_DUCKDB):
        return None, _rk_reject(400, "unsupported_backend",
                                "a row exclusion can be declared only on an Athena or "
                                "S3 Tables system")
    table, name_error = _resolve_steward_table(system_id, body["table"].strip(), query)
    if name_error:
        return None, _rk_reject(400, name_error,
                                "that table name matches more than one schema entry; use "
                                "the fully-qualified name")
    return table, None


def set_row_exclusion_sync(system_id: str, body: dict, actor: str, *,
                           connection_type: str = "", seams=None):
    """Sync body of the row-exclusion PUT (admin-gated by the route). Refuses
    any backend but Athena and S3 Tables (both enforce the exclusion in their
    own read guard); otherwise validates and stores a table's row exclusion on
    its SchemaEntry with provenance, or clears it when ``conditions`` is an
    empty list. A
    validation failure is a 422 with ``row_exclusion``'s reason. ``body`` is
    already parsed and type-checked by :func:`_parse_row_exclusion_body`.
    Both writes keep the system's row-exclusion manifest in the crash-safe
    order (``row_exclusion_manifest``); a write that does not land is a 500,
    and sending the same body again completes it. A concurrent set or clear
    of the SAME table that took this write's manifest entry is a 409
    ``concurrent_change``, and a write while a re-provision carries the
    system's row exclusions across its schema rewrite is a 409
    ``rewrite_in_progress``: this request wrote nothing after that point."""
    from . import row_exclusion
    from . import row_exclusion_manifest
    from . import connection_resolver as cr
    conditions = body["conditions"]
    write, query = seams if seams is not None else _row_exclusion_graph_seams()
    table, reject = _row_exclusion_target(system_id, body, connection_type, query)
    if reject is not None:
        return reject
    try:
        if conditions:
            out = row_exclusion.set_row_exclusion(
                system_id, table, conditions, label=body.get("label", ""), actor=actor,
                source=str(body.get("source") or ""), write=write, query=query)
        else:
            out = row_exclusion.clear_row_exclusion(system_id, table, actor=actor,
                                                    write=write, query=query)
    except (row_exclusion_manifest.TokenLost, row_exclusion_manifest.RewriteInProgress,
            row_exclusion_manifest.ManifestContended, row_exclusion.RowExclusionContended) as e:
        rewriting = isinstance(e, row_exclusion_manifest.RewriteInProgress)
        logger.warning("set_row_exclusion: %s.%s not applied: %s", system_id, table, e)
        return _rx_conflict("rewrite_in_progress" if rewriting else "concurrent_change")
    except Exception as e:  # noqa: BLE001 - scrub detail
        logger.error("set_row_exclusion: graph error for %s.%s: %s: %s",
                     system_id, table, type(e).__name__, e)
        return JSONResponse(status_code=500, content={
            "status": "error", "detail": "row exclusion update failed (see server logs)"})
    if out.get("status") != "ok":
        return JSONResponse(status_code=422, content={"detail": {
            "reason": out.get("reason", "invalid_input"),
            "detail": out.get("detail", "invalid row exclusion")}})
    cr.audit_connection_event("row_exclusion", system_id, actor=actor, outcome="ok",
                              extra={"table": table, "conditions": len(conditions)})
    return out


@router.put("/row-exclusion/{system_id}")
async def set_row_exclusion_route(system_id: str, request: Request):
    """Set (or clear) a table's row exclusion: the rows its source owner says
    are not records (admin-only; Athena or S3 Tables).

    Body: ``{"table": "<table>", "label": "<owner's name for the rows>",
    "conditions": [{"column": "<c>", "op": "empty|not_empty"}, {"column": "<c>",
    "op": "in|not_in", "values": ["<v>", ...]}, ...], "source": "<owner's
    statement>"}``; ALL conditions hold for a row that is not a record; an
    empty ``conditions`` clears the fact. The body is size-capped before
    parsing (the record-key ceiling) and every field is type-checked; every
    validation failure is a 422 with a reason (``row_exclusion``). 404 for an
    unknown system, 400 ``unsupported_backend`` for a system that is neither
    Athena nor S3 Tables. Offloaded (the graph read/write must not run on the
    event loop)."""
    _check_admin(request)
    from .catalog import catalog
    cat_entry = catalog.get(system_id)
    if cat_entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    body = _parse_row_exclusion_body(await _read_capped_record_key(request))
    actor = _record_key_actor(request)
    return await offload(set_row_exclusion_sync, system_id, body, actor,
                         connection_type=cat_entry.get("connection_type") or "")


@router.get("/row-exclusion/{system_id}")
async def get_row_exclusion_route(system_id: str, request: Request, table: str):
    """Read a table's row exclusion (label, conditions, canonical predicate)
    and its provenance (admin-only)."""
    _check_admin(request)
    from .catalog import catalog
    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    return await offload(_get_row_exclusion_sync, system_id, table)


def _get_row_exclusion_sync(system_id: str, table: str):
    from . import row_exclusion
    _write, query = _row_exclusion_graph_seams()
    try:
        resolved, name_error = _resolve_steward_table(system_id, table.strip(), query)
        if name_error:
            return _rk_reject(400, name_error,
                              "that table name matches more than one schema entry; use the "
                              "fully-qualified name")
        return row_exclusion.read_row_exclusion(system_id, resolved, query=query)
    except Exception as e:  # noqa: BLE001 - scrub detail
        logger.error("get_row_exclusion: graph error for %s.%s: %s: %s",
                     system_id, table, type(e).__name__, e)
        return JSONResponse(status_code=500, content={
            "status": "error", "detail": "row exclusion read failed (see server logs)"})


def get_llm_settings():
    """The configured models: orchestrator, system agents and the AgentCore
    Memory extraction model (``memory_model.settings_view``: the model, whether
    it may be changed here and why not, and a public error when the read
    fails). Sync body (SSM and AgentCore reads); the route offloads it."""
    from . import memory_model
    from .model_config import get_configured_models
    m = get_configured_models()
    return {
        "orchestrator_model": m["orchestrator"],
        "agent_model": m["agent"],
        "region": m["region"],
        **memory_model.settings_view(m["region"]),
    }


@router.get("/llm-settings")
async def get_llm_settings_route(request: Request):
    """Currently configured models. Sync body: ``get_llm_settings``."""
    _check_admin(request)
    return await offload(get_llm_settings)


def _available_inference_profiles(region: str):
    """Set of inference-profile IDs enabled in the region, or None if unavailable.

    None means "couldn't determine" (e.g. local dev or missing permission) — the
    caller then treats every curated model as available rather than hiding them.
    """
    try:
        import boto3 as _b3
        client = _b3.client("bedrock", region_name=region)
        ids: set[str] = set()
        token = None
        while True:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = client.list_inference_profiles(**kwargs)
            for p in resp.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                if pid:
                    ids.add(pid)
            token = resp.get("nextToken")
            if not token:
                break
        return ids or None
    except Exception as e:  # nosec B110 - availability is best-effort; fall back to full list
        logger.warning(f"list_inference_profiles unavailable ({e}); not filtering models")
        return None


def get_models():
    """Curated model catalog (single source: agents/model_catalog.py), region-
    resolved and annotated with availability from Bedrock list_inference_profiles.
    Sync body (Bedrock listing); the route offloads it.
    """
    from .model_catalog import catalog
    region = os.environ.get("AWS_REGION", "us-east-1")
    return catalog(region, _available_inference_profiles(region))


@router.get("/models")
async def get_models_route(request: Request):
    """Curated model catalog with region availability. Sync body: ``get_models``."""
    _check_admin(request)
    return await offload(get_models)


def _orchestrator_runtime_model(region: str) -> str | None:
    """The BEDROCK_MODEL actually set on the running orchestrator runtime.

    SSM is reporting/override state; the RUNTIME ENV is what the orchestrator
    reads. They drift whenever a deploy changes the built-in default (deploy.sh
    sets BEDROCK_MODEL directly and does not write SSM), and change detection
    against the drifted SSM value made a model switch a silent no-op — the
    steward saw "ok" while the runtime kept the old model. Returns None when
    unreadable, so callers fall back to the SSM/catalog value rather than
    guessing.
    """
    import boto3 as _boto3
    try:
        control = _boto3.client("bedrock-agentcore-control", region_name=region)
        from .runtime_provision_helpers import list_all_agent_runtimes
        for rt in list_all_agent_runtimes(control):
            if rt.get("agentRuntimeName", "") == "adt_orchestrator":
                details = control.get_agent_runtime(
                    agentRuntimeId=rt.get("agentRuntimeId", ""))
                return details.get("environmentVariables", {}).get("BEDROCK_MODEL", "")
    except Exception as e:  # noqa: BLE001 - unreadable is not fatal
        logger.warning("could not read orchestrator runtime model: %s", type(e).__name__)
    return None


def _update_orchestrator_runtime_model(region: str, orch_model: str) -> str:
    """Point the adt_orchestrator AgentCore runtime at *orch_model*.

    Uses the ONE shared update-kwargs builder
    (:func:`agents.runtime_provision_helpers.runtime_update_kwargs`), which
    preserves the runtime's existing config: round-trips
    ``protocolConfiguration`` (full-replacement API — the orchestrator is
    legitimately HTTP, system agents are MCP; omitting the field downgrades
    MCP runtimes to HTTP, ADR-0056), drops the immutable
    ``requireServiceS3Endpoint`` network key, and passes
    ``authorizerConfiguration`` only when one is actually configured (tagged
    union — an empty dict fails validation). Returns "" on success or the
    error text (the caller surfaces it; SSM was already written).
    """
    import boto3 as _boto3
    try:
        control = _boto3.client("bedrock-agentcore-control", region_name=region)
        from .runtime_provision_helpers import (
            list_all_agent_runtimes, runtime_update_kwargs)
        for rt in list_all_agent_runtimes(control):
            if rt.get("agentRuntimeName", "") != "adt_orchestrator":
                continue
            rid = rt.get("agentRuntimeId", "")
            details = control.get_agent_runtime(agentRuntimeId=rid)
            env_vars = dict(details.get("environmentVariables", {}))
            env_vars["BEDROCK_MODEL"] = orch_model
            control.update_agent_runtime(
                **runtime_update_kwargs(rid, details, env_vars))
            logger.info(f"Updated orchestrator runtime {rid} with model {orch_model}")
            break
    except Exception as e:
        logger.error("Failed to update orchestrator runtime env vars (%s)", type(e).__name__)
        return public_error_text(e, "admin.llm_settings.runtime_update", default="upstream_error")
    return ""


#: Stand-in for a request body that did not parse as JSON, so the sync body
#: can answer it after the admin gate exactly as the inline handler did.
_INVALID_JSON = object()


def update_llm_settings(request: Request, body):
    """Update LLM model configuration. Persists to SSM and updates AgentCore runtime env vars.
    Sync body (SSM writes, the orchestrator runtime update and the agent-model
    fleet refresh); the route reads the JSON body and offloads this. A
    ``memory_model`` is refused with 400 before any write: the memory
    extraction model switch is a background job of its own
    (:func:`start_memory_model_switch`)."""
    _check_admin(request)
    if body is _INVALID_JSON:
        return {"status": "error", "message": "Invalid JSON body"}

    orch_model = body.get("orchestrator_model", "").strip()
    agent_model = body.get("agent_model", "").strip()
    refusal = _allowlist_refusal(orch_model, agent_model)  # before ANY write
    if refusal is None:
        refusal = _memory_model_not_here(body.get("memory_model"))
    if refusal is not None:
        return refusal
    with _RUNTIME_CONFIG_LOCK:
        return _apply_llm_settings(orch_model, agent_model)


#: Where the memory extraction model is switched (a background job).
_MEMORY_MODEL_SWITCH_PATH = "/api/admin/llm-settings/memory-model"


def _memory_model_not_here(value) -> "JSONResponse | None":
    """HTTP 400 for a PUT naming a ``memory_model``, or ``None`` (absent or
    blank). The switch takes about two minutes, longer than a request may
    last, so it runs as its own job; this save never touches the memory."""
    if not str(value or "").strip():
        return None
    return JSONResponse(status_code=400, content={"status": "error", "message": (
        "memory_model is not saved with the orchestrator and agent models: the memory "
        "extraction model switch runs as a background job. POST "
        f'{_MEMORY_MODEL_SWITCH_PATH} with {{"memory_model": "<id>"}} and read its status '
        f"at GET {_MEMORY_MODEL_SWITCH_PATH}/job/<job_id>. Nothing was changed.")})


#: The settings save holds the shared runtime-config lock
#: (``runtime_provision_helpers.RUNTIME_CONFIG_LOCK``) across its SSM writes,
#: the orchestrator update and the fleet bump, so SSM and the runtimes always
#: name the same model; the auth/protocol repair holds it across its loop, and
#: every AgentManager runtime read-modify-write (business-context refresh,
#: connection resync, image refresh) takes it too. Inline on the loop these
#: writers could never interleave; in the offload pool they can.
_RUNTIME_CONFIG_LOCK = RUNTIME_CONFIG_LOCK


def _apply_llm_settings(orch_model: str, agent_model: str):
    """The writes of ``update_llm_settings``, run under ``_RUNTIME_CONFIG_LOCK``."""
    import boto3 as _boto3
    region = os.environ.get("AWS_REGION", "us-east-1")
    ssm = _boto3.client("ssm", region_name=region)

    # Detect what actually changed against the EFFECTIVE current models (SSM override
    # OR the catalog default), not the raw SSM params — an unset param would otherwise
    # make an unchanged value read as "changed" (spurious reonboard/restart hints).
    from .model_config import get_configured_models
    _cur = get_configured_models()
    current_orch = _cur.get("orchestrator", "")
    current_agent = _cur.get("agent", "")

    # Compare against what is RUNNING, not what SSM records — see
    # _orchestrator_runtime_model for why those two drift across a deploy.
    _live_orch = _orchestrator_runtime_model(region)
    orch_changed = orch_model and orch_model != (
        _live_orch if _live_orch is not None else current_orch)
    # Agent side is self-correcting: the fleet refresh reads each runtime's env
    # and reports "unchanged" per system, so applying an already-set model is
    # cheap and drift repairs itself.
    agent_changed = bool(agent_model) and agent_model != current_agent

    # Persist to SSM
    try:
        _put_model_params(ssm, orch_model, agent_model)
    except Exception as e:
        logger.error("Failed to persist LLM settings to SSM (%s)", type(e).__name__)
        return {"status": "error", "message": "SSM write failed: " + public_error_text(
            e, "admin.llm_settings.ssm_write", default="upstream_error")}

    # If orchestrator model changed, update the AgentCore runtime environment variables
    if orch_changed:
        err = _update_orchestrator_runtime_model(region, orch_model)
        if err:
            return {"status": "error", "message": f"SSM saved but runtime update failed: {err}"}

    return _llm_settings_result(orch_model, agent_model, orch_changed, agent_changed)


@router.put("/llm-settings", dependencies=[Depends(admin_refuse_while_restoring)])
async def update_llm_settings_route(request: Request):
    """Update LLM model configuration. Sync body: ``update_llm_settings``.
    Reading the JSON body is the one async step, so it stays here."""
    try:
        body = await request.json()
    except Exception:
        body = _INVALID_JSON
    return await offload(update_llm_settings, request, body)


def start_memory_model_switch(request: Request, body):
    """Start switching the AgentCore Memory extraction model
    (``memory_model_job.start``): 202 with the running job record, 400 for a
    body that is not exactly ``{"memory_model": "<id>"}`` or a model this
    deployment does not let the page set, 409 while another switch runs, 503
    when the shared slot store cannot decide; each refusal before anything
    is read or written. Sync body; the route reads the JSON body and
    offloads this."""
    _check_admin(request)
    model_id = body.get("memory_model") if isinstance(body, dict) else None
    if (not isinstance(model_id, str) or not model_id.strip()
            or set(body) != {"memory_model"}):
        return JSONResponse(status_code=400, content={"status": "error", "message": (
            'The body must be exactly {"memory_model": "<model id>"}. Nothing was changed.')})
    from . import memory_model_job
    code, payload = memory_model_job.start(model_id.strip(),
                                           os.environ.get("AWS_REGION", "us-east-1"))
    return JSONResponse(status_code=code, content=payload)


@router.post("/llm-settings/memory-model", status_code=202,
             dependencies=[Depends(admin_refuse_while_restoring)])
async def start_memory_model_switch_route(request: Request):
    """Start a memory extraction model switch. Sync body:
    ``start_memory_model_switch``. Reading the JSON body is the one async
    step, so it stays here."""
    try:
        body = await request.json()
    except Exception:
        body = _INVALID_JSON
    return await offload(start_memory_model_switch, request, body)


def memory_model_job(job_id: "str | None"):
    """A memory extraction model switch job (``memory_model_job.status``),
    or the latest one when *job_id* is None (``memory_model_job.current``):
    the record, 404 for an unknown job, 503 when the shared store cannot
    answer, never a 500. Sync body (a DynamoDB read); the routes offload it."""
    from . import memory_model_job as jobs
    code, payload = jobs.current() if job_id is None else jobs.status(job_id)
    return payload if code == 200 else JSONResponse(status_code=code, content=payload)


@router.get("/llm-settings/memory-model/job")
async def memory_model_job_route(request: Request):
    """The latest memory extraction model switch. Sync body: ``memory_model_job``."""
    _check_admin(request)
    return await offload(memory_model_job, None)


@router.get("/llm-settings/memory-model/job/{job_id}")
async def memory_model_job_by_id_route(job_id: str, request: Request):
    """One memory extraction model switch. Sync body: ``memory_model_job``."""
    _check_admin(request)
    return await offload(memory_model_job, job_id)


def _allowlist_refusal(orch_model: str, agent_model: str) -> "JSONResponse | None":
    """HTTP 400 for a save naming a model its role may not run under the
    deployment allowlist (``ALLOWED_MODELS``; ``model_catalog.role_families``:
    each role is offered the allowed models of its own catalog role, so the
    workshop's pair cannot be swapped between the roles), or ``None`` when the
    save may go ahead. Checked before any SSM write or runtime update, so a
    refused save changes nothing. An empty field means "leave unchanged" and
    is never refused; with no allowlist configured nothing is refused."""
    from .model_catalog import (AGENT, ALLOWED_MODELS_ENV, ORCHESTRATOR, allowed_families,
                                is_allowed_for)
    allowed = allowed_families()
    refused = [f"{field} {value!r}"
               for field, value, role in (("orchestrator_model", orch_model, ORCHESTRATOR),
                                          ("agent_model", agent_model, AGENT))
               if value and not is_allowed_for(value, role, allowed)]
    if not refused:
        return None
    verb = "is" if len(refused) == 1 else "are"
    message = (f"{' and '.join(refused)} {verb} not offered in this deployment "
               f"({ALLOWED_MODELS_ENV} allows: {', '.join(sorted(allowed or ()))})")
    return JSONResponse(status_code=400, content={"status": "error", "message": message})


def _llm_settings_result(orch_model: str, agent_model: str, orch_changed: bool,
                         agent_changed: bool) -> dict:
    """Assemble the PUT response, applying the agent-model live refresh.

    ``reonboard_required`` is set ONLY when the live env bump did not take
    (failed / no manager / nothing updated) — the bump replaces the re-onboard
    for every system it updated, and the flag stays for older consumers."""
    result = {"status": "ok", "orchestrator_model": orch_model,
              "agent_model": agent_model}
    if orch_changed:
        result["restart_required"] = True
    if agent_changed:
        refresh = _refresh_agent_model_fleet(agent_model)
        result["agent_runtime_refresh"] = refresh
        if refresh.get("reonboard_required") or not refresh.get("ok"):
            result["reonboard_required"] = True
    return result


def _put_model_params(ssm, orch_model: str, agent_model: str) -> None:
    """Persist the selected model ids to SSM (extracted for the ≤50-line gate)."""
    if orch_model:
        ssm.put_parameter(Name="/adt/llm-orchestrator-model", Value=orch_model,
                          Type="String", Overwrite=True)
    if agent_model:
        ssm.put_parameter(Name="/adt/llm-agent-model", Value=agent_model,
                          Type="String", Overwrite=True)


def _refresh_agent_model_fleet(agent_model: str) -> dict:
    """Point every onboarded system's RUNNING runtime at the new agent model.

    SYSTEM_AGENT_MODEL is baked in at provision, so this used to require a full
    re-onboard; AgentManager.refresh_agent_model updates the runtime env in
    place instead (same seam as the business-context refresh). Reports the
    honest per-outcome tally — a system whose refresh FAILED still runs the old
    model, and ``reonboard_required`` is set so the steward knows a re-onboard
    is the fallback. Never raises.
    """
    tally: dict = {"ok": [], "failed": [], "skipped": []}
    manager = None
    try:
        manager = _get_agent_manager()
        if manager is None:
            tally["error"] = "no_agent_manager"
            tally["reonboard_required"] = True
            return tally
        for entry in manager.list_agents():
            system_id = entry.get("system_id", "")
            if not system_id:
                continue
            outcome = manager.refresh_agent_model(system_id, agent_model)
            tally.setdefault(outcome, []).append(system_id)
    except Exception as e:  # noqa: BLE001 - report, never fail the settings save
        logger.warning("agent-model fleet refresh failed: %s", type(e).__name__)
        tally["error"] = type(e).__name__
    if tally.get("failed") or tally.get("error"):
        tally["reonboard_required"] = True
    elif tally.get("skipped") and getattr(manager, "mode", "") == "cloud":
        # In cloud mode "skipped" means a listed system has no runtime to
        # switch, so it is not on the new model either; re-onboarding it is
        # the fix. (Local mode skips every system by design.)
        tally["reonboard_required"] = True
    return tally


# ============================================================
# Concept Packs — install/uninstall industry vocabulary
# ============================================================

# Source files in agentic_digital_thread/data/neptune/. Each pack file has
# `MERGE (:Concept {id:'...', source: 'pack:<name>'})` rows.
_PACK_FILES = {
    "food_bev": "pack_food_bev.cypher",
    "automotive": "pack_automotive.cypher",
    "pharma": "pack_pharma.cypher",
    "discrete": "pack_discrete.cypher",
    "sustainability": "pack_sustainability.cypher",
}

_PACK_LABELS = {
    "food_bev": "Food & Beverage",
    "automotive": "Automotive",
    "pharma": "Pharmaceutical",
    "discrete": "Discrete Manufacturing",
    "sustainability": "Sustainability & Safety",
}


def _neptune_dir() -> str:
    """Resolve the bundled neptune cypher directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "data", "neptune"),
        os.path.join(here, "..", "..", "agentic_digital_thread", "data", "neptune"),
        os.path.join(os.path.sep, "app", "agentic_digital_thread", "data", "neptune"),
        os.path.join(os.path.sep, "app", "data", "neptune"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return ""


def _count_pack_concepts_in_neptune(pack_id: str) -> int:
    """Return how many concepts from this pack are currently present in Neptune."""
    from agents.db import neptune_query
    try:
        rows = neptune_query(
            "MATCH (c:Concept {source: $src}) RETURN count(c) AS n",
            {"src": f"pack:{pack_id}"},
        )
        if rows:
            return int(rows[0].get("n") or 0)
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return 0


def list_concept_packs(request: Request):
    """List available industry concept packs with installation status.
    Sync body (pack file reads and a Neptune count per pack); the route offloads it."""
    _check_admin(request)
    npdir = _neptune_dir()
    packs = []
    for pack_id, fname in _PACK_FILES.items():
        path = os.path.join(npdir, fname) if npdir else ""
        # Count concepts the pack file would install
        total_in_file = 0
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("MERGE"):
                        total_in_file += 1
        installed = _count_pack_concepts_in_neptune(pack_id)
        packs.append({
            "id": pack_id,
            "name": _PACK_LABELS.get(pack_id, pack_id),
            "concepts_in_file": total_in_file,
            "concepts_installed": installed,
            "is_installed": installed > 0,
        })
    return {"packs": packs}


@router.get("/concept-packs")
async def list_concept_packs_route(request: Request):
    """List industry concept packs with installation status. Sync body: ``list_concept_packs``."""
    return await offload(list_concept_packs, request)


def install_concept_pack(pack_id: str, request: Request):
    """Install (MERGE) all concepts from a pack into Neptune. Idempotent."""
    _check_admin(request)
    if pack_id not in _PACK_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown pack: {pack_id}")
    npdir = _neptune_dir()
    path = os.path.join(npdir, _PACK_FILES[pack_id]) if npdir else ""
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=500, detail=f"Pack file missing on server: {path}")

    from agents.db import neptune_write
    inserted = 0
    failed = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("MERGE"):
                continue
            try:
                # MERGE statements in our pack files use literals — strip trailing ;
                neptune_write(line.rstrip(";"), {})
                inserted += 1
            except Exception as e:
                logger.warning(f"concept pack {pack_id} MERGE failed: {e}")
                failed += 1
    return {
        "status": "ok",
        "pack_id": pack_id,
        "concepts_attempted": inserted + failed,
        "concepts_inserted": inserted,
        "concepts_failed": failed,
    }


@router.post("/concept-packs/{pack_id}/install")
async def install_concept_pack_route(pack_id: str, request: Request):
    """Install (MERGE) all concepts from a pack into Neptune. Idempotent. Sync body: ``install_concept_pack``."""
    return await offload(install_concept_pack, pack_id=pack_id, request=request)


def uninstall_concept_pack(pack_id: str, request: Request):
    """Remove all concepts belonging to this pack from Neptune.
    Only deletes concepts not currently mapped to any field."""
    _check_admin(request)
    if pack_id not in _PACK_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown pack: {pack_id}")

    from agents.db import neptune_write, neptune_query
    src = f"pack:{pack_id}"

    # Count what we'd delete and what's mapped (left alone for safety)
    counts = neptune_query(
        "MATCH (c:Concept {source: $src}) "
        "OPTIONAL MATCH (c)<-[m:MAPS_TO]-() "
        "RETURN count(DISTINCT c) AS total, count(DISTINCT CASE WHEN m IS NOT NULL THEN c END) AS mapped",
        {"src": src},
    )
    total = int(counts[0].get("total") or 0) if counts else 0
    mapped = int(counts[0].get("mapped") or 0) if counts else 0

    # Detach-delete unmapped concepts only
    neptune_write(
        "MATCH (c:Concept {source: $src}) WHERE NOT (c)<-[:MAPS_TO]-() DETACH DELETE c",
        {"src": src},
    )
    return {
        "status": "ok",
        "pack_id": pack_id,
        "deleted": max(0, total - mapped),
        "preserved_due_to_mappings": mapped,
    }


@router.post("/concept-packs/{pack_id}/uninstall")
async def uninstall_concept_pack_route(pack_id: str, request: Request):
    """Remove all concepts belonging to this pack from Neptune. Sync body: ``uninstall_concept_pack``."""
    return await offload(uninstall_concept_pack, pack_id=pack_id, request=request)


def update_branding(request: Request, body: dict):
    """Update branding config (company name, tagline). Writes to SSM + branding.json and reloads module."""
    _check_admin(request)
    import json as _json
    from pathlib import Path as _Path
    import importlib
    from agents import branding as _branding_module

    branding_path = _Path(__file__).resolve().parent.parent / "branding.json"
    # Load existing config
    existing = {}
    if branding_path.exists():
        with open(branding_path, encoding="utf-8") as f:
            existing = _json.load(f)
    # Update only provided fields
    if "company" in body:
        existing["company"] = body["company"].strip()
    if "tagline" in body:
        existing["tagline"] = body["tagline"].strip()
    # Write back to branding.json (in-container, for immediate use)
    with open(branding_path, "w", encoding="utf-8") as f:
        _json.dump(existing, f, indent=2)
    # Also persist to SSM so the setting survives container redeployments
    try:
        import boto3 as _boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        ssm = _boto3.client("ssm", region_name=region)
        if "company" in body:
            ssm.put_parameter(
                Name="/adt/branding-company",
                Value=existing["company"],
                Type="String",
                Overwrite=True,
            )
        if "tagline" in body:
            ssm.put_parameter(
                Name="/adt/branding-tagline",
                Value=existing["tagline"],
                Type="String",
                Overwrite=True,
            )
    except Exception as e:
        logger.warning(f"Failed to persist branding to SSM: {e}")
    # Reload branding module so /api/branding returns new values immediately
    importlib.reload(_branding_module)
    return {"status": "ok", "company": existing.get("company", ""), "tagline": existing.get("tagline", "")}


@router.put("/branding")
async def update_branding_route(request: Request, body: dict):
    """Update branding config (company name, tagline). Writes to SSM + branding.json and reloads module. Sync body: ``update_branding``."""
    return await offload(update_branding, request=request, body=body)


def restore_runtime_auth(request: Request):
    """Repair system-agent runtimes after a field-wiping update.

    ``update_agent_runtime`` is FULL-REPLACEMENT, so any update built without
    round-tripping resets every omitted field. This restores the two that
    matter and are not otherwise recoverable:

    * the JWT authorizer config (the original reason for this endpoint), and
    * ``protocolConfiguration`` -> MCP (ADR-0056). A runtime downgraded to the
      default HTTP answers no MCP ``initialize`` — calls fail after 40 s with
      ``McpInitializationException`` on a READY runtime — and a plain round-trip
      preserves the downgrade, so the correct protocol is ASSERTED here. Safe by
      construction: every ``adt_*`` system-agent runtime is created as MCP and
      the orchestrator is excluded by name.

    Idempotent; each runtime's outcome is reported individually. Sync body
    (AgentCore control-plane list/get/update per runtime); the route offloads it.
    """
    _check_admin(request)
    import boto3 as _boto3
    region = os.environ.get("AWS_REGION", "eu-central-1")
    m2m_discovery_url = os.environ.get("M2M_DISCOVERY_URL", "") or os.environ.get("AGENTCORE_M2M_DISCOVERY_URL", "")
    m2m_client_id = os.environ.get("M2M_CLIENT_ID", "") or os.environ.get("AGENTCORE_M2M_CLIENT_ID", "")

    if not m2m_discovery_url or not m2m_client_id:
        return {"error": "M2M_DISCOVERY_URL or M2M_CLIENT_ID not set in environment"}

    control = _boto3.client("bedrock-agentcore-control", region_name=region)
    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": m2m_discovery_url,
            "allowedClients": [m2m_client_id],
        }
    }

    try:
        with _RUNTIME_CONFIG_LOCK:
            results = _repair_system_runtimes(control, authorizer_config)
    except Exception as e:  # noqa: BLE001 - report, never raise to the client
        return public_error(e, "admin.restore_runtime_auth", default="upstream_error")
    return {"results": results}


@router.post("/restore-runtime-auth", dependencies=[Depends(admin_refuse_while_restoring)])
async def restore_runtime_auth_route(request: Request):
    """Repair system-agent runtime auth + MCP protocol. Sync body: ``restore_runtime_auth``."""
    return await offload(restore_runtime_auth, request)


def _repair_system_runtimes(control, authorizer_config: dict) -> list[dict]:
    """Re-send every system-agent runtime's config with auth + MCP asserted.

    Reads the FULL details per runtime first: ``update_agent_runtime`` is
    full-replacement, so a payload built from the partial LIST entry silently
    wipes env vars and reverts ``protocolConfiguration`` MCP->HTTP (ADR-0056 —
    the very damage this repairs). The orchestrator is skipped by name because
    it is legitimately HTTP.
    """
    from .runtime_provision_helpers import (list_all_agent_runtimes,
                                            runtime_update_kwargs)
    results: list[dict] = []
    for rt in list_all_agent_runtimes(control):
        name = rt.get("agentRuntimeName", "")
        if not name.startswith("adt_") or name == "adt_orchestrator":
            continue
        rid = rt.get("agentRuntimeId", "")
        try:
            details = control.get_agent_runtime(agentRuntimeId=rid)
            before = (details.get("protocolConfiguration") or {}).get("serverProtocol", "")
            control.update_agent_runtime(**runtime_update_kwargs(
                rid, details, dict(details.get("environmentVariables", {})),
                authorizer_config=authorizer_config,
                protocol_override={"serverProtocol": "MCP"}))
            # Report the prior protocol so a steward can SEE which runtimes were
            # actually broken rather than trusting a blanket "updating".
            results.append({"runtime": name, "status": "updating",
                            "protocol_was": before or "unset"})
        except Exception as e:  # noqa: BLE001 - per-runtime, keep going
            results.append({"runtime": name, "status": "error",
                            **public_error(e, "admin.restore_runtime_auth.runtime",
                                           default="upstream_error")})
    return results


@router.get("/settings")
async def get_settings(request: Request):
    _check_admin(request)
    return {"demo_enabled": demo.is_enabled()}


@router.put("/settings")
async def update_settings(request: Request):
    _check_admin(request)
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}

    if "demo_enabled" in body:
        enabled = bool(body["demo_enabled"])
        # set_override may raise if the SSM/store write fails — let it
        # surface as a 500 rather than silently dropping the toggle.
        demo.set_override(enabled)
        if enabled:
            prompts.seed_registry()
        else:
            prompts.unseed_registry()
        return {"status": "ok", "demo_enabled": enabled}

    return {"status": "ok", "demo_enabled": demo.is_enabled()}


def _live_updates_state() -> dict:
    """Current live-updates state for route echoes: the flag plus the EFFECTIVE
    snapshot interval (override → env → 120s default) as an int of seconds."""
    return {
        "live_updates_enabled": live_settings.live_updates_enabled(),
        "interval_seconds": int(live_settings.snapshot_interval_seconds()),
    }


@router.get("/live-updates")
async def get_live_updates(request: Request):
    """Return the current runtime live-updates toggle state (admin-only).

    Mirrors the demo toggle (``GET /settings``). ``live_updates`` is the runtime
    on/off for the recurring background live-card work (the ``CARD_LIVE`` poll +
    MQTT stream driver + the periodic SSE snapshot re-resolve). Turning it OFF
    pauses those recurring backend queries to save cost WITHOUT a redeploy;
    on-demand ``GET /api/dashboard`` still serves cards. Default ON.
    ``interval_seconds`` is the EFFECTIVE snapshot cadence (runtime override →
    ``DASHBOARD_SNAPSHOT_INTERVAL`` env → 120s default)."""
    _check_admin(request)
    return _live_updates_state()


@router.post("/live-updates")
async def set_live_updates_route(request: Request):
    """Set the runtime live-updates toggle and/or snapshot interval (admin-only).

    Mirrors the demo toggle POST/PUT: accepts ``{"live_updates_enabled": bool}``
    and/or ``{"interval_seconds": int}`` and calls the ``live_settings`` seam,
    which persists through to SSM (``/adt/live-updates-enabled`` /
    ``/adt/live-updates-interval``) so the change is effective immediately and
    survives restarts — no redeploy. An out-of-bounds (outside 30..3600s) or
    non-numeric interval is IGNORED (the seam's fail-soft contract); the echoed
    state always reflects what is actually in effect. The seam may raise if the
    SSM write fails; that surfaces as a 500 rather than silently dropping the
    change."""
    _check_admin(request)
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}

    if "live_updates_enabled" in body:
        live_settings.set_live_updates(bool(body["live_updates_enabled"]))
    if "interval_seconds" in body:
        live_settings.set_snapshot_interval(body["interval_seconds"])
    return {"status": "ok", **_live_updates_state()}


def _feedback_exploration_state() -> dict:
    """Effective state + provenance (shared by the GET and the PUT echo)."""
    from . import feedback_exploration_settings as fxs
    enabled, source = fxs.effective()
    return {"feedback_exploration_enabled": enabled, "source": source,
            "env_kill_active": fxs.env_kill_active()}


@router.get("/feedback-exploration")
async def get_feedback_exploration(request: Request):
    """Current feedback-exploration toggle state + provenance (admin-only).

    ``source`` is where the effective value came from — ``env-kill`` (the
    ops kill switch is active; the admin flag cannot override it),
    ``admin-flag`` (the persisted runtime toggle), or ``default`` (nothing
    set ⇒ OFF)."""
    _check_admin(request)
    return _feedback_exploration_state()


@router.put("/feedback-exploration")
async def put_feedback_exploration(request: Request):
    """Set the feedback-exploration admin flag (admin-only).

    Accepts ``{"feedback_exploration_enabled": bool}`` and persists through
    ``feedback_exploration_settings.set_enabled`` (SSM write-through,
    fail-loud — a failed persist surfaces as ``{"status": "error"}``, the
    llm-settings shape). A write attempted while the env KILL switch is
    active is refused with 409 and the persister is never touched — no
    phantom writes under a kill. A body without the key echoes the current
    state (the live-updates convention)."""
    _check_admin(request)
    from . import feedback_exploration_settings as fxs
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}

    if "feedback_exploration_enabled" not in body:
        return {"status": "ok", **_feedback_exploration_state()}

    if fxs.env_kill_active():
        raise HTTPException(
            status_code=409,
            detail="FEEDBACK_EXPLORATION_ENABLED env kill switch is active; "
                   "the admin flag cannot override it and was not written")
    try:
        fxs.set_enabled(bool(body["feedback_exploration_enabled"]))
    except Exception as e:
        logger.error("Failed to persist feedback-exploration setting (%s)", type(e).__name__)
        return {"status": "error", "message": "SSM write failed: " + public_error_text(
            e, "admin.feedback_exploration.ssm_write", default="upstream_error")}
    return {"status": "ok", **_feedback_exploration_state()}


@router.get("/feedback-exploration/status/{exploration_id}")
async def get_feedback_exploration_status(exploration_id: str, request: Request):
    """One exploration's status record (admin-only; the eval lane's poll).

    Delegates to ``feedback_exploration.get_exploration_status`` — the
    allowlisted read surface whose 16-hex shape anchor rejects the registry's
    reserved containment keys by construction. Unknown or non-id-shaped ids
    are 404."""
    _check_admin(request)
    from . import feedback_exploration
    record = feedback_exploration.get_exploration_status(exploration_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown exploration id")
    return record


@router.get("/templates")
async def get_templates(request: Request):
    _check_admin(request)
    return []


@router.put("/templates")
async def update_templates(request: Request):
    _check_admin(request)
    return {"status": "ok"}


def _with_pre_node_catalog(system_id: str, resolved: dict) -> dict:
    """The config the Test button probes. A system registered through /connect
    has no System node until it is provisioned, and the resolver is node-only,
    so it returns an unconfigured result (no connection_type). Use what
    /connect stored on the catalog entry instead, the same pre-node source the
    Edit form validates against, rather than defaulting to PostgreSQL. Once the
    node exists it is the only source and this returns ``resolved`` unchanged.

    A USER registration's pre-node connection comes from
    ``connection_resolver.registration_connection`` (ARN-aware): it resolves the
    whole routing tuple from the secret, so an ARN-only registration (Base URL
    optional, supplied by the secret) is probed with the secret's base_url
    instead of the raw entry's missing one. An ARN-sourced registration never
    falls back to the raw entry (a miss is probed as credential_unresolved);
    for any other user registration, only when that resolves nothing usable
    does the raw catalog fill apply."""
    if resolved.get("connection_type"):
        return resolved
    from .catalog import catalog
    from . import connection_resolver as cr
    cat_entry = catalog.get(system_id) or {}
    if cr.is_user_registration(cat_entry):
        try:
            registered = dict(cr.registration_connection(system_id, cat_entry))
        except Exception:  # noqa: BLE001 - best-effort; fall back to the raw entry
            registered = {}
        if registered.get("credential_source") == "secret_arn":
            # An ARN-sourced registration is probed ONLY with the secret's
            # routing. On a miss registration_connection has already stripped
            # every routing field and set _secret_unresolved, so the probe
            # reports credential_unresolved; a typed Base URL is never probed.
            registered.setdefault("connection_type", cat_entry.get("connection_type"))
            return registered
        if registered and not registered.get("_secret_unresolved") and (
                registered.get("base_url") or registered.get("connection_string")):
            registered.setdefault("connection_type", cat_entry.get("connection_type"))
            return registered
    out = _fill_existing_from_catalog(resolved, cat_entry)
    if cat_entry.get("connection_type"):
        out["connection_type"] = cat_entry["connection_type"]
    return out


@router.post("/connection/{system_id}/test")
async def probe_system_connection(system_id: str, request: Request):
    """Probe live connectivity for a system (admin-only); a registered system
    not yet provisioned is probed from its catalog entry (_with_pre_node_catalog).

    The result carries no secrets and no raw host/IP/port: only a coarse
    ``reason`` enum and a matching human ``detail``. A blocked target (SSRF/scheme
    policy) returns HTTP 400; any other probe failure returns HTTP 200 with
    ``status='fail'`` (a successful probe of an unhealthy target); an unexpected
    server-side exception returns HTTP 500 with a generic message (detail logged
    server-side only). Named ``probe_system_connection`` so it does not collide
    with ``onboard_agent.test_connection`` or pytest's ``test_*`` collection.
    """
    _check_admin(request)

    from .catalog import catalog
    from .connection_resolver import (
        audit_connection_event,
        probe_connection,
        resolve_connection,
    )

    if catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")

    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"

    try:
        resolved = _with_pre_node_catalog(system_id, resolve_connection(system_id))
        result = probe_connection(system_id, resolved)
        audit_connection_event("test", system_id, actor=actor, outcome=result["status"])
        body = {
            "status": result["status"],
            "latency_ms": result["latency_ms"],
            "connection_type": result["connection_type"],
            "reason": result.get("reason"),
            "detail": result["detail"],
        }
        # A blocked target is a client error (400); every other fail is a
        # successful probe of an unhealthy/unreachable target and returns 200.
        if result["status"] == "fail" and result.get("reason") == "blocked_target":
            return JSONResponse(status_code=400, content=body)
        return body
    except Exception as e:
        # Log full detail server-side; return a generic message to the client.
        logger.error("probe_system_connection failed for %s: %s", system_id, e)
        audit_connection_event("test", system_id, actor=actor, outcome="error")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Connection test failed (see server logs)"},
        )


def _run_entity_rescan(system_id: str | None, request: Request):
    """Shared implementation for the entity-rescan admin routes.

    Re-runs the ADR-0040 entity/identity classification (curated → heuristic → LLM;
    the SAME ``onboard._classify_table_entity`` path provision uses) for
    already-onboarded systems and MERGEs the missing entity + IDENTIFIED_BY edges.
    IDEMPOTENT — a rescan over an up-to-date graph writes nothing new (the summary
    reports ``skipped_existing``). Admin-gated via :func:`_check_admin` (403
    non-admin); 404 for an unknown ``system_id``. Deliberately a SYNC handler body:
    the per-table LLM classify can take a while, and the sync path-operation runs
    in FastAPI's threadpool so the event loop is never blocked.
    """
    _check_admin(request)
    from .catalog import catalog
    from . import connection_resolver as cr
    from . import entity_rescan

    if system_id is not None and catalog.get(system_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")

    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    scope = system_id or "all"
    try:
        summary = entity_rescan.rescan_entities(system_id)
    except entity_rescan.UnknownSystemError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001 - fail-loud but generic to the client
        logger.error("entity_rescan failed for %s: %s: %s", scope, type(e).__name__, e)
        cr.audit_connection_event("entity_rescan", scope, actor=actor, outcome="error")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Entity rescan failed (see server logs)"},
        )
    per_sys = summary.get("systems", {})
    totals = {
        "systems": len(per_sys),
        "entities_written": sum(s.get("entities_written", 0) for s in per_sys.values()),
        "identities_written": sum(s.get("identities_written", 0) for s in per_sys.values()),
        "skipped_existing": sum(s.get("skipped_existing", 0) for s in per_sys.values()),
        "errors": sum(len(s.get("errors", [])) for s in per_sys.values()),
    }
    cr.audit_connection_event("entity_rescan", scope, actor=actor, outcome="ok",
                              extra=totals)
    return {"status": "ok", "totals": totals, **summary}


@router.post("/entities/rescan/{system_id}")
def rescan_entities_for_system(system_id: str, request: Request):
    """Re-run entity/identity classification for ONE onboarded system (admin-only).

    Returns ``{"status", "totals", "dry_run", "systems": {<id>: {tables_scanned,
    entities_written, identities_written, skipped_existing, tier_counts, errors[]}}}``.
    See :func:`_run_entity_rescan`.
    """
    return _run_entity_rescan(system_id, request)


@router.post("/entities/rescan")
def rescan_entities_all(request: Request):
    """Re-run entity/identity classification for ALL onboarded systems (admin-only).
    Same response shape as the per-system route. See :func:`_run_entity_rescan`."""
    return _run_entity_rescan(None, request)


@router.post("/asset-roles/reclassify")
def reclassify_asset_roles(request: Request, system_id: str | None = None,
                           dry_run: bool = False, restore: bool = False):
    """Re-derive every asset model's role (and the asset levels it implies) from
    the graph as it stands — no re-walk (admin-only). ``dry_run=true`` reports
    without writing; ``restore=true`` undoes the pass (walker levels back,
    derived roles removed). With ``ADT_ASSET_ROLES=off`` only ``restore`` runs.
    200 only when every system succeeded; 500 with per-system ``error`` (or
    ``coverage_error``: roles written, asset coverage not) otherwise; 404 for an unknown ``system_id``."""
    _check_admin(request)
    from . import asset_role_pass as arp
    from . import connection_resolver as cr
    from .catalog import catalog, REMOVAL_FAILED
    if system_id:
        entry = catalog.lookup(system_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
        if entry.get("status") == REMOVAL_FAILED:
            # A half-removed system (its :System node carries removal_incomplete):
            # re-deriving its roles writes to a system that is being torn down.
            # Refuse until the removal is finished (same shape as the other guards).
            raise HTTPException(status_code=409,
                                detail=f"The removal of '{system_id}' did not finish; "
                                       "remove it again before re-deriving its asset roles.")
        from .onboard import _reset_in_progress
        if _reset_in_progress(system_id):
            # A removal still running: the catalog entry still reads 'onboarded'
            # until the cascade ends, so the status check above can't see it.
            logger.info("asset-roles reclassify refused for %s: removal in progress", system_id)
            raise HTTPException(status_code=409,
                                detail=f"'{system_id}' is being removed; wait for the removal "
                                       "to finish before re-deriving its asset roles.")
    if restore and dry_run:
        raise HTTPException(status_code=400, detail="restore has no dry run")
    if not restore and not arp.enabled():
        raise HTTPException(status_code=409, detail="Asset roles are off (ADT_ASSET_ROLES); "
                                                    "only restore=true is allowed")
    actor = getattr(getattr(request.state, "user", None), "email", None) or "unknown"
    scope = system_id or "all"
    result = _run_asset_role_action(arp, system_id, dry_run, restore)
    # A failed coverage write counts too: the roles landed (``written`` stays
    # true) but the seed-visible counts are not the ones just computed.
    failed = sorted(s for s, v in result["systems"].items()
                    if v.get("error") or v.get("coverage_error"))
    if not dry_run:
        cr.audit_connection_event("asset_roles_restore" if restore else "asset_roles_reclassify",
                                  scope, actor=actor, outcome="error" if failed else "ok",
                                  extra={"systems": len(result["systems"]), "failed": len(failed)})
    body = {"status": "error" if failed else "ok", "dry_run": dry_run, "restore": restore,
            "failed": failed, **result}
    return JSONResponse(status_code=500, content=body) if failed else body


def _run_asset_role_action(arp, system_id, dry_run: bool, restore: bool) -> dict:
    """One system or all, as ``{"systems": {id: summary-or-error}}``."""
    try:
        if restore:
            return ({"systems": {system_id: arp.restore_system(system_id)}} if system_id
                    else arp.restore_all())
        if system_id:
            return {"systems": {system_id: arp.classify_system(system_id, dry_run=dry_run)}}
        return arp.classify_all(dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 - reported per system, generic to the client
        logger.error("asset role action failed: %s: %s", type(e).__name__, e)
        return {"systems": {system_id or "all": {"error": "Asset role pass failed "
                                                          "(see server logs)"}}}


def migrate_erp_mes_views(request: Request):
    """One-time migration: create erp_data and mes_* view schemas in Aurora.
    These views allow ERP and MES systems to be onboarded with clean schema separation.
    Safe to run multiple times (uses CREATE OR REPLACE VIEW). The DDL is demo
    content generated by ``agents.demo.site_views`` (ADR-0044).
    """
    _check_admin(request)
    from .demo.db_routing import demo_pg_dsn
    from .demo.site_views import erp_mes_views_sql
    from . import pgdriver

    conn = None
    try:
        conn = pgdriver.connect(demo_pg_dsn('public'))
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(erp_mes_views_sql())
        cur.close()
        return {"status": "ok", "message": "ERP and MES view schemas created in Aurora"}
    except Exception as e:
        # Client: generic + class name only; server log: the digit-masked message.
        logger.warning("erp-mes-views migration failed (%s: %s)",
                       type(e).__name__, re.sub(r"\d", "#", str(e)))
        return {"status": "error", "error": "migration failed", "error_type": type(e).__name__}
    finally:
        if conn is not None:
            conn.close()


@router.post("/migrate/erp-mes-views")
async def migrate_erp_mes_views_route(request: Request):
    """One-time migration: create erp_data and mes_* view schemas in Aurora. Sync body: ``migrate_erp_mes_views``."""
    return await offload(migrate_erp_mes_views, request=request)
