# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E1 onboarding preflight (ADR-0053) — an async diagnostic gate BEFORE
discover→map.

``probe_connection`` already validates the connection and the cross-account
STS AssumeRole preflight; this module adds the missing layers — per-table
bounded SERIAL sample reads and a discovery-completeness score — surfaced as
one of three pinned verdicts:

* ``blocked`` — the connection/role probe failed (``assume_role_denied``,
  unreachable, bad credentials), or a USER-registered PostgreSQL login can do
  more than read (``privileged_login``: superuser, or CREATE / write in the
  schema). The UI refuses to start discover; the API refuses nothing here
  (preflight is a diagnostic gate, not an authz gate), and provisioning
  refuses the privileged login itself (``check_registered_pg_login``).
* ``degraded`` — connection OK but ≥1 table sample read failed, or Athena
  physical-column discovery would be skipped (the lake-athena failure class).
* ``ready`` — all probes and sample reads passed.

Sample reads are BOUNDED (LIMIT 1 per table / one asset list per model / one
namespace list per MCP-i3X source) and SERIAL (the telemetry-serial invariant
generalizes). No sample VALUE ever enters the result — presence/error CLASS
only (rules-engine constraint: production data never enters the graph or the
job record). Preflight NEVER writes to the graph or the catalog.

Sample-reading a large source can exceed the CloudFront 120 s origin cap, so
the run is a ``JobStore("preflight")`` background job (the ``_RESET_JOBS`` /
try-it idiom — replica-safe) polled by fast GETs, never a blocking POST. One
concurrent job PER SYSTEM (check-and-set keyed by system_id, 15 min staleness
takeover); a run past the 300 s wall clamp stores an honest timeout error.

Routes are mounted on the existing ``/api/onboard`` router (``onboard.py``
delegates to :func:`start_preflight` / :func:`preflight_status`), so
``require_onboard_access`` applies unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid

from .job_store import JobStore

logger = logging.getLogger(__name__)

_PREFLIGHT_STALE_SECONDS = 900        # 15 min: stuck-claim takeover threshold
_PREFLIGHT_WALL_CLAMP_SECONDS = 300   # whole-run wall clamp (honest timeout)
_PREFLIGHT_MAX_TABLES = 50            # bounded catalog read breadth

_EMPTY_COMPLETENESS = {"tables_visible": 0, "tables_sample_read": 0,
                       "columns_discovered": 0, "physical_columns": "n/a"}

# Replica-safe job registry (the _RESET_JOBS idiom): fail-soft to in-memory
# without a durable sessions table. Tests swap the instance (table=None).
_PREFLIGHT_JOBS = JobStore("preflight")


class PreflightTimeoutError(Exception):
    """Raised when the serial sample-read loop exceeds the 300 s wall clamp.

    The job worker stores the honest ``preflight timed out`` error — never a
    partial result masquerading as a complete one.
    """


def _check_deadline(deadline: float) -> None:
    """Raise :class:`PreflightTimeoutError` once ``deadline`` (monotonic) passes."""
    if time.monotonic() > deadline:
        raise PreflightTimeoutError(
            f"preflight exceeded the {_PREFLIGHT_WALL_CLAMP_SECONDS}s wall clamp")


def _read_outcome(name: str, read) -> dict:
    """One bounded sample read → the pinned per-table entry.

    Success ⇒ ``ok``; exception ⇒ ``failed`` with the EXCEPTION CLASS only —
    never message content, which could carry values, hosts, or ARNs. The
    read's return value is deliberately DISCARDED (no row/sample echo).
    """
    try:
        read()
        return {"name": name, "sample_read": "ok", "detail": ""}
    except Exception as exc:  # noqa: BLE001 - coarse class only, by contract
        return {"name": name, "sample_read": "failed", "detail": type(exc).__name__}


def preflight_verdict(connection: dict, tables: list, physical) -> str:
    """PURE pinned verdict derivation (plan D-E1 / ADR-0053).

    Connection fail ⇒ ``blocked``; else any ``sample_read == "failed"`` OR
    ``physical_columns`` starting with ``skipped`` ⇒ ``degraded``; else
    ``ready``.
    """
    if (connection or {}).get("status") != "pass":
        return "blocked"
    if any((t or {}).get("sample_read") == "failed" for t in tables or []):
        return "degraded"
    if str(physical or "").startswith("skipped"):
        return "degraded"
    return "ready"


def _preflight_warnings(connection: dict, tables: list, physical) -> list:
    """One honest sentence per degradation — coarse classes only (never
    values, hosts, ARNs, or raw exception messages)."""
    if (connection or {}).get("status") != "pass":
        return [f"connection probe failed ({connection.get('reason', 'error')}): "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                f"{connection.get('detail', '')}"]
    warnings = [f"sample read failed for table '{t.get('name', '')}' "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                f"({t.get('detail', '')})"
                for t in tables or [] if (t or {}).get("sample_read") == "failed"]
    if str(physical or "").startswith("skipped"):
        warnings.append(
            f"Athena physical-column discovery would be skipped ({physical}) — "
            "struct-path columns may be unverifiable at provision time")
    return warnings


# ── connection seams (module attrs so tests monkeypatch them) ────────────────

def _resolve(system_id: str) -> dict:
    """Resolve the live connection config (node-only; never raises)."""
    from .connection_resolver import resolve_connection
    return resolve_connection(system_id)


def _bootstrap_unregistered(resolved: dict, system_id: str) -> dict:
    """Mirror discover's first-onboard bootstrap (onboard.py, PostgreSQL branch).

    A built-in/seed pg system has NO node connection before its first provision
    (``resolve_connection`` is node-only), and preflight runs BEFORE discover —
    so without this the diagnostic gate reports ``connection failed`` and blocks
    every FIRST onboard of a bundled system (observed live: the wizard's
    Preflight-Blocked checkpoint on a fresh catalog card). Bootstrap the same
    live seed-DB env connection discover uses (``demo.db_routing.demo_pg_dsn`` —
    the ADR-0044 routing home; a live env read, never a catalog snapshot).
    No-op when a connection is already resolved, and when the env carries no
    seed DB (real deployments without demo data). TYPED connectors (Tulip / i3X /
    Athena / REST / MCP) carry their catalog connection_type + non-secret config
    + secret_arn so ``probe_connection`` doesn't misjudge them as PostgreSQL."""
    if resolved.get("connection_string"):
        return resolved
    entry = _catalog_entry(system_id) or {}
    from .connection_resolver import is_user_registration
    if resolved.get("_unconfigured") and is_user_registration(entry):
        # A user registration (HTTP route / chat tool) has no graph node until
        # its first provision: probe the connection it registered (every type —
        # without this a fresh Athena/Tulip/MCP system was probed as PostgreSQL
        # with no connection string, always ``blocked``), never the seed DB.
        from .connection_resolver import registration_connection
        return registration_connection(system_id, entry)
    ctype = entry.get("connection_type") or "PostgreSQL"
    if ctype == "MCP Server" and not (resolved.get("url") or resolved.get("connection_string")):
        builtin = _bootstrap_builtin_highbyte(resolved, system_id)
        if builtin is not None:
            return builtin
    if ctype != "PostgreSQL":
        return _bootstrap_typed_from_catalog(resolved, entry, ctype)
    return _bootstrap_seed_pg(resolved, entry.get("schema") or "", ctype)


def _bootstrap_builtin_highbyte(resolved: dict, system_id: str) -> "dict | None":
    """Pre-node bootstrap for the built-in HighByte system: the deployment's
    hub URL + key as Bearer, the same source discover and provision use
    (``builtin_highbyte.bootstrap_source``). ``None`` for any other system, or
    when the deployment has no hub URL."""
    from . import builtin_highbyte
    if not builtin_highbyte.is_builtin(system_id):
        return None
    source = builtin_highbyte.bootstrap_source()
    if not source:
        return None
    out = {k: v for k, v in resolved.items() if k != "_unconfigured"}
    out.update(connection_type="MCP Server", url=source["url"], token=source["token"],
               auth_scheme=source["auth_scheme"])
    return out


def _bootstrap_typed_from_catalog(resolved: dict, entry: dict, ctype: str) -> dict:
    """Pre-node bootstrap for typed connectors (Tulip / SiteWise (i3X) / Athena /
    REST / MCP).

    Before the System node exists (preflight runs BEFORE discover, and
    ``resolve_connection`` is node-only per ADR-0031), the connection lives ONLY
    on the catalog entry + the registration secret (``adt/conn/<id>``) — never on
    a node yet. Without this, ``resolved.connection_type`` is None and
    ``probe_connection`` defaults it to "PostgreSQL", so EVERY typed connector's
    first UI onboard is falsely blocked (the PostgreSQL probe fails with "no
    connection string configured"). Mirror discover's "catalog entry as the
    pre-node source": carry the entry's connection_type + its non-secret
    connection_config onto the resolved dict, plus the secret_arn pointer so the
    per-type probe can resolve credentials from ``adt/conn/<id>``. A live pre-node
    bootstrap, NOT a persisted catalog-config snapshot read at query time.
    """
    out = dict(resolved)
    out["connection_type"] = ctype
    cfg = entry.get("connection_config") or {}
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if v and not out.get(k):
                out[k] = v
    if entry.get("connection_string") and not out.get("connection_string"):
        out["connection_string"] = entry["connection_string"]
    if entry.get("secret_arn") and not out.get("secret_arn"):
        out["secret_arn"] = entry["secret_arn"]
    out.pop("_unconfigured", None)
    return out


def _bootstrap_seed_pg(resolved: dict, schema: str, ctype: str) -> dict:
    """Pre-node bootstrap for a PostgreSQL seed/demo system.

    Bootstrap the live seed-DB env connection discover uses
    (``demo.db_routing.demo_pg_dsn`` — the ADR-0044 routing home; a live env read,
    never a catalog snapshot). No-op when the env carries no seed DB (real
    deployments without demo data → honest blocked verdict).
    """
    if not schema:
        return resolved
    try:
        from .demo.db_routing import demo_pg_dsn
        dsn = demo_pg_dsn(schema)
    except Exception:  # noqa: BLE001 - no seed env → keep the honest blocked verdict
        return resolved
    if not dsn:
        return resolved
    out = dict(resolved)
    out["connection_string"] = dsn
    out.setdefault("schema", schema)
    out["connection_type"] = ctype
    out.pop("_unconfigured", None)
    return out


def _probe(system_id: str, resolved: dict) -> dict:
    """REUSE ``probe_connection`` — AssumeRole preflight, SSRF policy, and
    the coarse reason enum come free (never re-implement connection logic)."""
    from .connection_resolver import probe_connection
    return probe_connection(system_id, resolved)


def _catalog_entry(system_id: str):
    """The catalog row backing the POST 404 check (unknown system)."""
    if not system_id:
        return None
    from .catalog import catalog
    return catalog.get(system_id)


# ── per-type bounded SERIAL sample reads ─────────────────────────────────────

def _pf_sample_one_table(conn, schema: str, name: str) -> None:
    """Read at most one row of ``schema.name`` (a name discovery found, checked
    by ``validate_identifier``); the row is discarded."""
    from .db import table_sql, validate_identifier
    validate_identifier(name)
    c = conn.cursor()
    try:
        c.execute(table_sql("SELECT 1 FROM {}.{} LIMIT 1", schema, name))
        c.fetchall()
    finally:
        c.close()


def _pf_postgres(resolved: dict, system_id: str, deadline: float) -> tuple:
    """PostgreSQL: ``SELECT 1 FROM <table> LIMIT 1`` per discovered table.

    Table list from a bounded ``information_schema`` read; identifiers pass
    the same ``validate_identifier`` discovery uses; the socket pins to the
    SSRF-validated IP (the ``_probe_postgres`` posture). Row content is
    never read. For a USER-registered login the privilege check runs FIRST:
    positive evidence raises :class:`PrivilegedLoginError` before any table
    is read; a check that cannot run adds its warning as the third element.
    """
    from .db import validate_identifier
    conn_str = resolved.get("connection_string", "")
    schema = str(resolved.get("schema") or resolved.get("search_path") or "public")
    validate_identifier(schema)
    conn = _pg_connect(conn_str)
    conn.autocommit = True  # a failed sample read must not abort the rest
    try:
        warnings: list = []
        if _is_user_login(system_id):
            refusal, warnings = _pg_login_check(conn, schema)
            if refusal:
                raise PrivilegedLoginError(refusal)
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_name LIMIT %s", (schema, _PREFLIGHT_MAX_TABLES))
        names = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s", (schema,))
        columns_discovered = int(cur.fetchone()[0])
        cur.close()
        tables = []
        for name in names:  # SERIAL — never fan out parallel reads
            _check_deadline(deadline)
            tables.append(_read_outcome(
                name, lambda n=name: _pf_sample_one_table(conn, schema, n)))
    finally:
        conn.close()
    ok = sum(1 for t in tables if t["sample_read"] == "ok")
    return tables, {"tables_visible": len(names), "tables_sample_read": ok,
                    "columns_discovered": columns_discovered,
                    "physical_columns": "n/a"}, warnings


#: (superuser, can CREATE in the schema, can change any table in it) for the
#: connected login. Read from the catalog only; never touches a row.
_LOGIN_PRIVILEGE_SQL = (
    "SELECT r.rolsuper,"
    " EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = %s"
    " AND has_schema_privilege(n.oid, 'CREATE')),"
    " EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = %s AND c.relkind IN ('r', 'p')"
    " AND has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE'))"
    " FROM pg_roles r WHERE r.rolname = current_user"
)


def _is_user_login(system_id: str) -> bool:
    """True for a system whose connection a user registered with their own
    login (the only logins preflight checks)."""
    from .connection_resolver import is_user_registration
    return is_user_registration(_catalog_entry(system_id) or {})


#: The warning when the login's privileges could not be read. Nothing is
#: blocked on a check that did not run; the user is told it did not.
LOGIN_UNCHECKED_WARNING = (
    "the PostgreSQL login's privileges could not be checked, so it was not "
    "refused. The system agent runs SQL its model writes, and that SQL can read "
    "whatever this login can read: make sure it is a dedicated read-only login.")


class PrivilegedLoginError(Exception):
    """A user's PostgreSQL login can do more than read. ``advice`` is the text
    shown to the user (which login to register instead), composed here by
    :func:`_pg_login_check`; it names the schema only, never a host, value or
    secret. Callers read ``advice``, never the exception's own text."""

    def __init__(self, advice: str):
        super().__init__("privileged PostgreSQL login")
        self.advice = advice


def _pg_login_check(conn, schema: str) -> tuple:
    """``(refusal, warnings)`` for the user's PostgreSQL login on ``conn``.

    ``refusal`` is the advice when the catalog shows the login is a superuser,
    or can create objects or change data in ``schema``: a system agent runs
    SQL its model writes, and that SQL can read whatever the login can read.
    A check that cannot run refuses nothing and returns
    :data:`LOGIN_UNCHECKED_WARNING`."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(_LOGIN_PRIVILEGE_SQL, (schema, schema))
            superuser, can_create, can_write = (bool(v) for v in (cur.fetchone() or ()))
        finally:
            cur.close()
    except Exception as exc:  # noqa: BLE001 - an unchecked login is reported, never blocked
        logger.info("preflight login privilege check skipped (%s)", type(exc).__name__)
        return None, [LOGIN_UNCHECKED_WARNING]
    if superuser:
        found = "the PostgreSQL login is a superuser"
    elif can_create or can_write:
        found = f"the PostgreSQL login can create objects or change data in schema '{schema}'"
    else:
        return None, []
    return (f"{found}. The system agent runs SQL its model writes, and that SQL can "
            "read whatever this login can read. Use a dedicated read-only login with "
            f"CONNECT on the database, USAGE on schema '{schema}' and SELECT on its "
            "tables, and nothing else."), []


def _pg_connect(conn_str: str):
    """A PostgreSQL connection pinned to the SSRF-validated IP (the
    ``_probe_postgres`` posture)."""
    from . import pgdriver
    from .connection_security import validate_connection_target
    pinned_ip = validate_connection_target(conn_str, connection_type="PostgreSQL")
    return pgdriver.connect(conn_str, hostaddr=pinned_ip) if pinned_ip \
        else pgdriver.connect(conn_str)


def check_registered_pg_login(conn_str: str, schema: str) -> tuple:
    """:func:`_pg_login_check` over a fresh connection for a USER-registered
    PostgreSQL system (provisioning and the onboarding chat's
    ``test_connection``). A connection that cannot be opened refuses nothing
    and returns :data:`LOGIN_UNCHECKED_WARNING`."""
    try:
        conn = _pg_connect(conn_str)
    except Exception as exc:  # noqa: BLE001 - an unchecked login is reported, never blocked
        logger.info("login privilege check could not connect (%s)", type(exc).__name__)
        return None, [LOGIN_UNCHECKED_WARNING]
    try:
        return _pg_login_check(conn, schema)
    finally:
        conn.close()


def check_pg_login_params(conn_params: dict, schema: str) -> tuple:
    """:func:`check_registered_pg_login` for pgdriver connect kwargs
    (``{host, port, dbname, user, password[, options]}``, the shape
    ``connection_resolver.pg_conn_params`` returns): the startup restore
    checks the exact login it would start the agent with. ``options`` is not
    needed: the privilege query names the schema itself."""
    from urllib.parse import quote
    params = conn_params or {}
    host = str(params.get("host") or "")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # an IPv6 literal
    port = str(params.get("port") or "").strip()
    dsn = (f"postgresql://{quote(str(params.get('user') or ''), safe='')}:"
           f"{quote(str(params.get('password') or ''), safe='')}@{host}"
           f"{':' + port if port else ''}/{quote(str(params.get('dbname') or ''), safe='')}")
    return check_registered_pg_login(dsn, schema)


def _pf_athena(resolved: dict, system_id: str, deadline: float) -> tuple:
    """Athena: one ``SELECT 1 … LIMIT 1`` per table, PLUS per-table physical
    column discovery through the SAME seam onboarding uses — so the
    lake-athena class (catalog visible, data read denied / struct paths
    unverifiable) surfaces HERE instead of degrading silently at
    ``onboard.py``'s physical-column seam.
    """
    from .athena_tools import (_resolve_config, _safe_sql_literal,
                               describe_physical_columns, run_athena_query)
    from .resolve_evidence import _with_cross_account_credentials
    cfg = {k: resolved[k] for k in ("workgroup", "database", "catalog",
                                    "output_location", "region", "role_arn")
           if resolved.get(k)}
    cfg = _with_cross_account_credentials(system_id, cfg)
    acfg = _resolve_config(cfg)
    db = _safe_sql_literal(acfg["database"])
    listing = run_athena_query(
        "SELECT table_name FROM information_schema.tables "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        f"WHERE table_schema = '{db}' ORDER BY table_name", acfg)
    names = [r[0] for r in listing.get("rows", [])][:_PREFLIGHT_MAX_TABLES]
    columns_discovered = 0
    try:
        counted = run_athena_query(
            "SELECT count(*) FROM information_schema.columns "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
            f"WHERE table_schema = '{db}'", acfg)
        columns_discovered = int(counted.get("rows", [["0"]])[0][0])
    except Exception:  # noqa: BLE001 - the completeness count is best-effort  # nosec B110 - best-effort path; failure is deliberately non-fatal
        pass
    tables, physical = [], "ok" if names else "n/a"
    for name in names:  # SERIAL — one data call at a time, never parallel
        _check_deadline(deadline)
        tables.append(_read_outcome(name, lambda n=name: run_athena_query(
            f'SELECT 1 FROM "{_safe_sql_literal(n)}" LIMIT 1', acfg, max_rows=1)))  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        if physical == "ok":
            try:
                describe_physical_columns(name, cfg)
            except Exception as exc:  # noqa: BLE001 - the onboard.py:4117 seam, made visible
                physical = f"skipped:{type(exc).__name__}"
    ok = sum(1 for t in tables if t["sample_read"] == "ok")
    return tables, {"tables_visible": len(names), "tables_sample_read": ok,
                    "columns_discovered": columns_discovered,
                    "physical_columns": physical}


def _pf_sitewise(resolved: dict, system_id: str, deadline: float) -> tuple:
    """SiteWise (Direct): list asset models, then ONE bounded
    ``list_assets(model)`` per model. Property VALUES are never fetched."""
    from . import sitewise_read
    from .resolve_evidence import _with_cross_account_credentials
    cfg = {k: resolved[k] for k in ("role_arn", "external_id", "region")
           if resolved.get(k)}
    cfg = _with_cross_account_credentials(system_id, cfg)
    region = str(cfg.get("region") or "").strip() or os.environ.get("AWS_REGION",
                                                                    "us-east-1")
    models = sitewise_read.list_asset_models_with_properties(
        cfg, region)[:_PREFLIGHT_MAX_TABLES]
    tables, columns_discovered = [], 0
    for model in models:  # SERIAL — the telemetry-serial invariant generalizes
        _check_deadline(deadline)
        columns_discovered += len(model.get("properties") or [])
        tables.append(_read_outcome(
            model.get("name", ""),
            lambda mid=model["id"]: sitewise_read.list_assets(
                cfg, region, asset_model_id=mid)))
    ok = sum(1 for t in tables if t["sample_read"] == "ok")
    return tables, {"tables_visible": len(models), "tables_sample_read": ok,
                    "columns_discovered": columns_discovered,
                    "physical_columns": "n/a"}


def _one_shot_discovery_read(name: str, read) -> tuple:
    """Shared shape for the single-call samplers (MCP / REST-i3X): one
    namespace/endpoint list → one table entry. ``columns_discovered`` counts
    the returned column/tag NAMES; values — and error text, which can carry
    hosts — never enter the result."""
    try:
        result = read()
    except Exception as exc:  # noqa: BLE001 - coarse class only, by contract
        return ([{"name": name, "sample_read": "failed",
                  "detail": type(exc).__name__}], dict(_EMPTY_COMPLETENESS))
    if isinstance(result, dict) and result.get("error"):
        return ([{"name": name, "sample_read": "failed",
                  "detail": str(result.get("error_class") or "DiscoveryError")}],
                dict(_EMPTY_COMPLETENESS))
    columns = sum(len(t.get("columns") or [])
                  for t in (result or {}).get("tables") or [])
    return ([{"name": name, "sample_read": "ok", "detail": ""}],
            {"tables_visible": 1, "tables_sample_read": 1,
             "columns_discovered": columns, "physical_columns": "n/a"})


def _pf_mcp(resolved: dict, system_id: str, deadline: float) -> tuple:
    """MCP/UNS: ONE bounded namespace list call (no per-tag reads).

    The URL is selected like the probe and ``onboard._mcp_discover_entry`` do
    (``url``, else ``connection_string``): a provisioned MCP node stores its URL
    under ``url`` only, so after a restart (catalog entry rebuilt without a
    connection string) ``connection_string`` alone was empty and the sample
    read failed while the probe passed. URL and token both come from the one
    ``resolved`` connection, never a request."""
    _check_deadline(deadline)
    from .onboard_agent import _discover_mcp
    url = resolved.get("url", "") or resolved.get("connection_string", "")
    entry = {"connection_string": url,
             "connection_config": {
                 "url": url,
                 "token": resolved.get("token", ""),
                 "auth_scheme": resolved.get("auth_scheme", "bearer_literal")}}
    return _one_shot_discovery_read("namespace", lambda: _discover_mcp(entry, system_id=system_id))


def _pf_rest(resolved: dict, system_id: str, deadline: float) -> tuple:
    """REST family / SiteWise (i3X): ONE bounded endpoint/node list call.

    Route the sample read through the
    vendor's ``discover`` hook, not a hard-wired ``_discover_openapi``. Before
    this fix a Tulip (or any non-OpenAPI REST vendor) was preflight-sampled by
    the GENERIC OpenAPI parse — the wrong discovery — even though its real
    discover differs. Now a REST vendor is sampled by its OWN discovery; only
    the generic / i3X fall back to ``_discover_openapi``. This is a GENERIC fix
    (it covers every REST vendor).
    """
    _check_deadline(deadline)
    from .onboard_agent import _discover_openapi
    entry = {"connection_string": (resolved.get("connection_string", "")
                                   or resolved.get("base_url", "")),
             "connection_config": {k: resolved[k]
                                   for k in ("base_url", "url", "token", "auth_scheme")
                                   if resolved.get(k)},
             "schema": str(resolved.get("schema") or "")}
    ctype = resolved.get("connection_type") or ""
    try:
        from .rest_vendor_registry import resolve_vendor
        adapter = resolve_vendor(ctype)
    except Exception:  # noqa: BLE001 - fall back to the generic OpenAPI parse
        adapter = None
    # SiteWise (i3X) keeps the OpenAPI parse (its discover hook is a stub); the
    # generic adapter's discover IS the OpenAPI parse, so both reach the same
    # call. A vendor with real discovery (tulip) is sampled by its own hook.
    if adapter is not None and adapter.name not in ("i3x", "generic"):
        return _one_shot_discovery_read(
            "endpoints", lambda: adapter.discover(system_id, entry, resolved))
    return _one_shot_discovery_read("endpoints", lambda: _discover_openapi(entry))


# Dispatched by the SAME probe-type keys probe_connection uses (single source).
_SAMPLERS = {"postgres": _pf_postgres, "athena": _pf_athena,
             "sitewise": _pf_sitewise, "mcp": _pf_mcp, "rest": _pf_rest}


def _sample_reads(resolved: dict, system_id: str, deadline: float) -> tuple:
    """Dispatch the bounded SERIAL sample reads by connection type.

    Returns ``(tables, completeness, extra_warnings)``. A wholesale sampler
    failure (e.g. the table listing itself) degrades HONESTLY: one failed
    ``(table listing)`` read plus a warning — never a silent ``ready``. The
    wall-clamp timeout propagates (the job stores the honest timeout error).
    """
    from .connection_probes import _normalize_type
    kind = _normalize_type(resolved.get("connection_type") or "PostgreSQL", resolved)
    sampler = _SAMPLERS.get(kind)
    if sampler is None:
        return [], dict(_EMPTY_COMPLETENESS), [
            f"no sample-read support for connection type "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            f"'{resolved.get('connection_type')}' — verdict reflects the "
            "connection probe only"]
    try:
        result = sampler(resolved, system_id, deadline)
        tables, completeness = result[0], result[1]
        return tables, completeness, list(result[2]) if len(result) > 2 else []
    except (PreflightTimeoutError, PrivilegedLoginError):
        raise
    except Exception as exc:  # noqa: BLE001 - coarse class only, never hosts/values
        return ([{"name": "(table listing)", "sample_read": "failed",
                  "detail": type(exc).__name__}],
                dict(_EMPTY_COMPLETENESS),
                [f"sample reads could not run ({type(exc).__name__})"])


def run_preflight(system_id: str, deadline: "float | None" = None) -> dict:
    """Orchestrate one preflight run → the PINNED result contract (ADR-0053).

    Connection/role validation first (REUSES ``probe_connection``). A failed
    probe short-circuits: verdict ``blocked``, ZERO sample reads attempted.
    Otherwise bounded SERIAL sample reads run for the system's connection
    type. Never writes to the graph or catalog; no sample VALUES anywhere in
    the payload (the ``probe_connection`` echo posture).
    """
    start = time.monotonic()
    if deadline is None:
        deadline = start + _PREFLIGHT_WALL_CLAMP_SECONDS
    resolved = _bootstrap_unregistered(_resolve(system_id), system_id)
    probe = _probe(system_id, resolved)
    connection = {"status": probe.get("status"), "reason": probe.get("reason"),
                  "detail": probe.get("detail"),
                  "latency_ms": probe.get("latency_ms")}
    tables: list = []
    completeness = dict(_EMPTY_COMPLETENESS)
    extra: list = []
    if connection["status"] == "pass":
        try:
            tables, completeness, extra = _sample_reads(resolved, system_id, deadline)
        except PrivilegedLoginError as exc:
            # A user's PostgreSQL login that can do more than read is refused
            # like a failed connection: verdict ``blocked``, advice as detail.
            connection = {**connection, "status": "fail", "reason": "privileged_login",
                          "detail": exc.advice}
            tables, completeness, extra = [], dict(_EMPTY_COMPLETENESS), []
    physical = completeness.get("physical_columns")
    return {"system_id": system_id,
            "verdict": preflight_verdict(connection, tables, physical),
            "connection": connection, "tables": tables,
            "completeness": completeness,
            "warnings": _preflight_warnings(connection, tables, physical) + extra,
            "elapsed_seconds": round(time.monotonic() - start, 1)}


# ── async job machinery (the _RESET_JOBS / try-it idiom, keyed per system) ───

def _active_key(system_id: str) -> str:
    """The per-system single-flight slot key (never a pollable job id)."""
    return f"active:{system_id}"


def claim_preflight_job(system_id: str) -> "str | None":
    """Check-and-set the per-system single-flight slot (staleness takeover).

    Returns a fresh job id, or None when a non-stale run is already in
    flight for THIS system (the caller answers 409; other systems are
    unaffected). A ``running`` claim older than 15 min is treated as
    abandoned (crashed worker/replica) and taken over.
    """
    active = _PREFLIGHT_JOBS.get(_active_key(system_id))
    if isinstance(active, dict) and active.get("status") == "running":
        try:
            started = float(active.get("started_at") or 0.0)
        except (TypeError, ValueError):
            started = 0.0
        if time.time() - started < _PREFLIGHT_STALE_SECONDS:
            return None
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    _PREFLIGHT_JOBS[_active_key(system_id)] = {"status": "running",
                                               "job_id": job_id, "started_at": now}
    _PREFLIGHT_JOBS[job_id] = {"status": "running", "started_at": now,
                               "system_id": system_id}
    return job_id


def _run_preflight_job(job_id: str, system_id: str) -> None:
    """Worker thread: run the preflight and store the result under the
    pinned contract. Errors are sanitized (exception CLASS only) into
    ``error_detail`` — ``JobStore`` stamps the coarse ``preflight_failed``
    sentinel on ``error``, and the poll GET surfaces ``error_detail`` as the
    contract's honest ``error`` text. Past the 300 s wall clamp the job
    stores ``preflight timed out``. The per-system slot is always released.
    """
    try:
        result = run_preflight(system_id)
        _PREFLIGHT_JOBS[job_id] = {"status": "done", "system_id": system_id,
                                   "result": result}
    except PreflightTimeoutError:
        _PREFLIGHT_JOBS[job_id] = {"status": "error", "system_id": system_id,
                                   "error_detail": "preflight timed out"}
    except Exception as exc:  # noqa: BLE001 - stored honestly, surfaced by the poll
        logger.exception("preflight job %s failed for %s", job_id, system_id)
        _PREFLIGHT_JOBS[job_id] = {
            "status": "error", "system_id": system_id,
            "error_detail": f"preflight failed: {type(exc).__name__}"}
    finally:
        _PREFLIGHT_JOBS[_active_key(system_id)] = {"status": "idle",
                                                   "job_id": job_id,
                                                   "finished_at": time.time()}


# ── route handlers (delegated from onboard.py — /api/onboard prefix) ─────────

async def start_preflight(body: dict):
    """POST /api/onboard/preflight — the pinned contract: 202 ``{"job_id"}``
    on accept; 409 when a preflight is already running for this system; 404
    for an unknown system. Explicit action only — never auto-triggered."""
    from fastapi.responses import JSONResponse
    system_id = str((body or {}).get("system_id") or "").strip()
    entry = await asyncio.to_thread(_catalog_entry, system_id)
    if not entry:
        return JSONResponse(status_code=404,
                            content={"error": f"Unknown system_id '{system_id}'"})
    job_id = await asyncio.to_thread(claim_preflight_job, system_id)
    if job_id is None:
        return JSONResponse(
            status_code=409,
            content={"error": f"preflight already running for {system_id}"})
    threading.Thread(target=_run_preflight_job, args=(job_id, system_id),
                     daemon=True).start()
    return {"job_id": job_id}


async def preflight_status(job_id: str):
    """GET /api/onboard/preflight/{job_id} — fast poll, replica-safe via the
    JobStore write-through. Pinned job shapes: running / done (``result`` =
    the :func:`run_preflight` contract) / error (``error`` = the sanitized
    honest line — timeout or ``preflight failed: <ExceptionType>``)."""
    from fastapi.responses import JSONResponse
    if job_id.startswith("active:"):  # the internal slot key is not pollable
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    job = await asyncio.to_thread(_PREFLIGHT_JOBS.get, job_id)
    if not isinstance(job, dict):
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    if job.get("status") == "error" and job.get("error_detail"):
        job = {**job, "error": job["error_detail"]}
    return job
