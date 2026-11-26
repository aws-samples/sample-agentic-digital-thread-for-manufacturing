# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Database and external service helpers for the Agentic Digital Thread sample."""
import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

import boto3
from botocore.config import Config as BotoConfig

from . import graph_resubmit, pgdriver, stage_timing
from .pgdriver import sql as psql
from .errors import MissingDbPasswordError
# The write check a leased operation binds and the graph-read scope: the graph
# writers below run the check, the reader runs a statement as a read only when
# it is one.
from .lease_guard import _WRITE_GUARD, check_write_guard, graph_read, write_guard  # noqa: F401

logger = logging.getLogger(__name__)


def validate_identifier(name: str) -> str:
    """Validate a SQL/Cypher identifier. Raises ValueError if invalid."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return name


def search_path_sql(schema: str) -> psql.Composed:
    """``SET search_path TO "<schema>"``. A schema name cannot be a bound
    parameter, so the driver quotes it."""
    return psql.SQL("SET search_path TO {}").format(psql.Identifier(schema))


def table_sql(template: str, schema: str, table: str) -> psql.Composed:
    """*template* (a literal with two ``{}`` slots) with the schema and table
    names filled in, each quoted by the driver."""
    return psql.SQL(template).format(psql.Identifier(schema), psql.Identifier(table))


def validate_system_id(system_id: str) -> str:
    """Validate a system_id used as a namespace key. Raises ValueError if invalid.

    Unlike ``validate_identifier`` (a strict SQL/Cypher identifier), system_ids in
    this codebase are hyphenated (e.g. 'mes-mil', 'digital-thread') because
    ``connect_new_system`` generates them via ``name.lower().replace(' ', '-')``.
    This validator therefore ALLOWS hyphens and underscores but REJECTS the
    namespace-escape characters that matter for keys like
    ``adt/systems/{system_id}``: path separators ('/' and '\\'),
    '..', whitespace, and empty. Errors propagate — no fallback.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]+', system_id or '') or '..' in system_id:
        raise ValueError(f"Invalid system_id: {system_id!r}")
    return system_id


def get_conn_params() -> dict:
    """Return Postgres connection params from environment.

    POSTGRES_PASSWORD MUST be set explicitly. We deliberately do not fall back to a
    hard-coded default so a deployed runtime can never silently
    connect with well-known throwaway credentials (fail-closed). For local development,
    set POSTGRES_PASSWORD in your environment (e.g. ``export POSTGRES_PASSWORD=postgres``).
    """
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise MissingDbPasswordError(
            "POSTGRES_PASSWORD is not set. Set it explicitly — no insecure default is used. "
            "For local development, export POSTGRES_PASSWORD before starting the app."
        )
    return {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        # "adt_demo" here is the fictitious demo company's database — demo content, not engine identity (see rename plan)
        "dbname": os.environ.get("POSTGRES_DB", "adt_demo"),
        "user": os.environ.get("POSTGRES_USER", "postgres"),
        "password": password,
    }


def _env_conn_params_for_schema(schema: str) -> dict:
    """ENV-ONLY connect params for *schema* — the records-first fallback.

    The demo schema→DB routing decision (ERP Aurora vs the demo seed database)
    is demo knowledge and lives in :mod:`agents.demo.db_routing` (ADR-0044
    Decision 2) — the local ERP-prefix tuple this module used to carry is
    deleted. When the demo module declines (non-ERP schema, or no ERP host is
    known), fall through to the generic single-DB env config
    (:func:`get_conn_params` — engine-side, not demo knowledge).

    Fail-closed on both branches: a missing explicit password raises
    ``RuntimeError`` — never a well-known default.

    This is also the recursion-safe seam for
    ``connection_resolver.pg_conn_params``'s env-credential bootstrap: it
    never consults records, so ``pg_conn_params → env bootstrap →
    records-first → resolve_connection → pg_conn_params`` cannot loop
    (see the guard in :func:`_get_conn_params_for_schema`).
    """
    from .demo.db_routing import demo_conn_params_for_schema  # lazy: one-way demo seam
    params = demo_conn_params_for_schema(schema)
    if params is not None:
        return params
    return get_conn_params()


# Records-first TTL cache for _get_conn_params_for_schema: query_pg is on hot
# dashboard/resolver paths and must not add a graph round-trip per query.
# {schema: (params_dict | None, expires_monotonic)} — ``None`` is the negative
# sentinel "no usable record; use the LIVE env fallback" (the env read itself
# is never cached, preserving byte-identical env semantics within the TTL).
_SCHEMA_CONN_CACHE: dict = {}
_SCHEMA_CONN_CACHE_TTL = 60.0
_SCHEMA_CONN_CACHE_LOCK = threading.Lock()


def invalidate_schema_conn_cache(schema: str | None = None) -> None:
    """Drop the records-first conn-params cache entry for *schema* (all if None).

    Called best-effort after an admin connection update so the hot ``query_pg``
    path picks up the freshly persisted record immediately instead of serving
    the old params for up to the TTL. Cheap and always safe — the cache
    repopulates on the next read.
    """
    with _SCHEMA_CONN_CACHE_LOCK:
        if schema is None:
            _SCHEMA_CONN_CACHE.clear()
        else:
            _SCHEMA_CONN_CACHE.pop(schema, None)

# Re-entrancy guard: connection_resolver.pg_conn_params' env-credential
# bootstrap calls back into this module. While WE are the ones invoking
# pg_conn_params (records-first assembly), that callback must be served by the
# env-only variant or the pair recurses (plan risk R3). Thread-local so
# concurrent request threads don't interfere.
_RECORDS_FIRST_GUARD = threading.local()


def _records_conn_params_for_schema(schema: str) -> dict | None:
    """Connect params for *schema* from the PERSISTED record, or ``None``.

    Maps the schema to a registered system via the in-memory catalog, resolves
    the ``System`` node (node-authoritative, ADR-0031/ADR-0044 Decision 1) and
    assembles connect params via ``connection_resolver.pg_conn_params``.
    Returns ``None`` on a miss or an unconfigured node — the caller falls
    through to the live env read. Fail-soft on unexpected errors (``None``),
    EXCEPT the fail-closed no-password :class:`~agents.errors.MissingDbPasswordError`
    from the credential bootstrap, which propagates: it is a deliberate
    refusal, not a read error. Any OTHER ``RuntimeError`` (a transient graph /
    resolver failure) is NOT a refusal and keeps the fail-soft env fallback.
    """
    try:
        system = _records_system_for_schema(schema)
        if not system or not system.get("id"):
            return None
        from .connection_resolver import (  # lazy
            is_user_registration, pg_conn_params, registration_connection,
            resolve_connection)
        resolved = resolve_connection(system["id"])
        if not resolved or resolved.get("_unconfigured"):
            # A USER PostgreSQL registration (no node until its first provision)
            # samples ITS OWN database, never the env fallback; every other
            # (demo/workshop) entry falls through exactly as before.
            if not is_user_registration(system):
                return None
            resolved = registration_connection(system["id"], system)
            if resolved.get("connection_type") != "PostgreSQL":
                return None
        _RECORDS_FIRST_GUARD.active = True
        try:
            # Refuses (ConnectionSecretUnavailableError) instead of env-filling a
            # user's own unreadable / credential-less secret, a user registration,
            # or a target other than the env's own database.
            return pg_conn_params(resolved, entry=system)
        finally:
            _RECORDS_FIRST_GUARD.active = False
    except MissingDbPasswordError:
        raise  # fail-closed no-password refusal — never swallowed
    except Exception:
        logger.warning(
            "records-first conn params failed for schema=%s; using env fallback",
            schema,
        )
        return None


def _records_system_for_schema(schema: str) -> dict | None:
    """The catalog entry records-first routes ``schema`` to: the first entry
    with that schema, exactly as before — EXCEPT when a USER registration shares
    the schema with another entry, which raises (a fail-closed refusal) rather
    than guess which database to read. Registration refuses a schema that is
    already registered, so for a user system the schema names exactly its own
    system id."""
    from . import catalog as _catalog  # lazy: catalog must not import db at module level
    from .connection_resolver import is_user_registration
    matches = [s for s in _catalog.catalog.get_all() if s.get("schema") == schema]
    if len(matches) > 1 and any(is_user_registration(s) for s in matches):
        raise MissingDbPasswordError(
            f"schema {schema} is registered by more than one system; refusing to "
            "guess which database to read")
    return matches[0] if matches else None


#: The connect-timeout cap :func:`connect_timeout_cap` sets for the current
#: context, in whole seconds (None outside one: connections open exactly as
#: configured).
_CONNECT_TIMEOUT_CAP_S: contextvars.ContextVar = contextvars.ContextVar(
    "pg_connect_timeout_cap_s", default=None)


@contextlib.contextmanager
def connect_timeout_cap(seconds):
    """Inside the block, every Postgres connection this module opens (through
    :func:`_get_conn_params_for_schema`: :func:`query_pg`, :func:`pg` and the
    other helpers here) passes a ``connect_timeout`` of at most
    ``seconds`` (whole seconds, at least 1; the driver waits at least 2). A
    stricter configured ``connect_timeout`` (the connection params', else
    ``PGCONNECT_TIMEOUT``) still wins, and a nested block keeps the stricter
    cap. Outside a block nothing changes, so no other caller's connection is
    affected.

    What it does not bound: the driver applies ``connect_timeout`` to each address
    a host name resolves to, and the name lookup itself is not timed, so one
    connect can take longer than ``seconds``. A caller with a total time
    budget must read the wall clock after each call rather than assume the
    cap. The cap is held in a ContextVar for the synchronous caller only: a
    thread started inside the block does not see it, and an asyncio task
    created inside it keeps a copy, so don't open connections from either."""
    cap = max(1, int(seconds))
    outer = _CONNECT_TIMEOUT_CAP_S.get()
    token = _CONNECT_TIMEOUT_CAP_S.set(cap if outer is None else min(cap, outer))
    try:
        yield
    finally:
        _CONNECT_TIMEOUT_CAP_S.reset(token)


def current_connect_timeout_cap() -> "int | None":
    """The cap :func:`connect_timeout_cap` set for this context, or None."""
    return _CONNECT_TIMEOUT_CAP_S.get()


def _configured_connect_timeout(params: dict) -> "int | None":
    """The positive ``connect_timeout`` a connection with ``params`` uses
    without a cap: the params' own, else ``PGCONNECT_TIMEOUT`` (the driver's
    fallback). None when unset, zero or negative (it waits without limit)
    or not a whole number."""
    raw = params.get("connect_timeout")
    if raw is None:
        raw = os.environ.get("PGCONNECT_TIMEOUT")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _with_connect_timeout_cap(params: dict) -> dict:
    """``params`` with the context's connect-timeout cap applied (unchanged
    outside :func:`connect_timeout_cap`)."""
    cap = _CONNECT_TIMEOUT_CAP_S.get()
    if cap is None:
        return params
    configured = _configured_connect_timeout(params)
    return {**params, "connect_timeout": cap if configured is None else min(cap, configured)}


def _get_conn_params_for_schema(schema: str) -> dict:
    """Connect params for *schema* — RECORDS-FIRST, env fallback.

    Precedence (ADR-0044 Decision 1): the persisted onboarding record wins —
    schema → registered system (catalog) → ``resolve_connection`` (the
    ``System`` node is the sole runtime source) → ``pg_conn_params``. Only on
    a miss, an unconfigured node, or a fail-soft resolution error does it fall
    through to :func:`_env_conn_params_for_schema` (a LIVE env read each call
    — never a stale snapshot).

    A short TTL cache (module-level, ~60s) wraps the records lookup because
    ``query_pg`` is on hot dashboard/resolver paths; the negative outcome is
    cached as a sentinel so registered-but-unresolvable schemas don't re-read
    the graph per query, while the env fallback itself stays live.

    Inside :func:`connect_timeout_cap` the params also carry its capped
    ``connect_timeout``; outside one they are exactly as resolved.
    """
    return _with_connect_timeout_cap(_conn_params_for_schema(schema))


def _conn_params_for_schema(schema: str) -> dict:
    """:func:`_get_conn_params_for_schema` without the connect-timeout cap."""
    if getattr(_RECORDS_FIRST_GUARD, "active", False):
        # Re-entered from pg_conn_params' env-credential bootstrap while this
        # thread is assembling records-first params — break the recursion by
        # serving the env-only variant (identical semantics to the Task-2.3
        # repoint of connection_resolver onto _env_conn_params_for_schema).
        return _env_conn_params_for_schema(schema)

    now = time.monotonic()
    with _SCHEMA_CONN_CACHE_LOCK:
        cached = _SCHEMA_CONN_CACHE.get(schema)
    if cached is not None and cached[1] > now:
        params = cached[0]
        if params is not None:
            return dict(params)
        return _env_conn_params_for_schema(schema)

    params = _records_conn_params_for_schema(schema)
    with _SCHEMA_CONN_CACHE_LOCK:
        _SCHEMA_CONN_CACHE[schema] = (
            dict(params) if params is not None else None,
            now + _SCHEMA_CONN_CACHE_TTL,
        )
    if params is not None:
        return dict(params)
    return _env_conn_params_for_schema(schema)


#: Internal read helpers: statement deadline and row cap. ``query_pg_bounded``
#: (the LLM-facing tool path) keeps the tighter ``sql_safety.MAX_ROWS`` /
#: byte caps; these bound the SERVER's own catalog and metric reads, which are
#: never rendered to the model as a whole.
_PG_STATEMENT_TIMEOUT_MS = 30_000
#: The session's ``statement_timeout`` (milliseconds, a bound string): SET
#: cannot take a bound parameter, set_config can.
STATEMENT_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, false)"
_PG_INTERNAL_MAX_ROWS = 5_000


def query_pg(schema: str, sql: str, params=None, *,
             max_rows: int = _PG_INTERNAL_MAX_ROWS,
             statement_timeout_ms: int | None = None) -> str:
    """Execute a read-only query against a specific Postgres schema; JSON string.

    Bounded: a 30 s ``statement_timeout`` (``statement_timeout_ms`` sets a
    shorter one for an optional read; it is clamped to 1 ms .. the default, so
    it can never switch the deadline off or extend it), at most *max_rows* rows (the result
    carries ``truncated: true`` / ``partial_reason: "row_limit"`` when the
    query had more), and the cursor/connection are closed on EVERY exit path.
    Raises on failure — callers that want outage-as-empty use :func:`pg`.
    """
    timeout_ms = _PG_STATEMENT_TIMEOUT_MS
    if statement_timeout_ms is not None:
        timeout_ms = min(max(int(statement_timeout_ms), 1), _PG_STATEMENT_TIMEOUT_MS)
    conn = pgdriver.connect(**_get_conn_params_for_schema(schema))
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        try:
            cur.execute(STATEMENT_TIMEOUT_SQL, (str(timeout_ms),))
            cur.execute(search_path_sql(schema))
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = list(cur.fetchmany(max_rows + 1)) if cur.description else []
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            result = {"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows)}
            if truncated:
                result["truncated"] = True
                result["partial_reason"] = "row_limit"
                logger.warning("pg query truncated: schema=%s max_rows=%d", schema, max_rows)
            return json.dumps(result, default=str)
        finally:
            cur.close()
    finally:
        conn.close()


def pg(schema: str, sql: str, params=None, *, statement_timeout_ms: int | None = None) -> dict:
    """Run query_pg and return the parsed dict.

    On ANY failure the result is EMPTY **and marked**: ``unavailable: true``
    plus the exception ``error_category`` — so a source outage is
    distinguishable from a genuinely empty result (the honest-data rule).
    Callers that only read ``rows`` keep working unchanged.
    ``statement_timeout_ms`` is passed to :func:`query_pg` (None keeps its default).
    """
    try:
        # The keyword only when set: every existing caller's call is unchanged.
        deadline = {} if statement_timeout_ms is None else {"statement_timeout_ms": statement_timeout_ms}
        return json.loads(query_pg(schema, sql, params, **deadline))
    except Exception as exc:
        logger.warning("pg query failed: schema=%s category=%s", schema, type(exc).__name__)
        return {"columns": [], "rows": [], "row_count": 0,
                "unavailable": True, "error_category": type(exc).__name__}


def query_pg_bounded(schema: str, sql: str, params=None) -> dict:
    """Execute PostgreSQL with max-plus-one fetching and bounded materialization."""
    from .response_integrity_query import bounded_query_result, fetch_max_plus_one
    from .sql_safety import MAX_ROWS

    conn = pgdriver.connect(**_get_conn_params_for_schema(schema))
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        try:
            cur.execute(STATEMENT_TIMEOUT_SQL, (str(_PG_STATEMENT_TIMEOUT_MS),))
            cur.execute(search_path_sql(schema))
            cur.execute(sql, params)
            columns = [item[0] for item in cur.description] if cur.description else []
            rows, exceeded = fetch_max_plus_one(cur, MAX_ROWS, columns)
            return bounded_query_result(columns, rows, exceeded)
        finally:
            cur.close()
    finally:
        conn.close()


#: Planner-statistics row estimates for every physical relation in a schema —
#: ONE catalog read, never a scan. ``reltuples`` is ``-1`` on PostgreSQL 14+
#: for a relation that has never been analyzed (omitted from the result so the
#: caller can decide whether an exact count is worth it); views have no rows
#: of their own and are not listed.
PG_ROW_ESTIMATE_SQL = (
    "SELECT c.relname AS table_name, c.reltuples::bigint AS estimate "
    "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'm')"
)


def pg_row_estimates(cur, schema: str) -> dict[str, int]:
    """``{relation_name: estimated_rows}`` from ``pg_class.reltuples`` for
    *schema*, using an open cursor (plain or RealDict). Relations without a
    usable estimate are absent."""
    cur.execute(PG_ROW_ESTIMATE_SQL, (schema,))
    estimates: dict[str, int] = {}
    for row in cur.fetchall() or []:
        if isinstance(row, dict):
            name, estimate = row.get("table_name"), row.get("estimate")
        else:
            name, estimate = row[0], row[1]
        try:
            value = int(estimate)
        except (TypeError, ValueError):
            continue
        if isinstance(name, str) and value >= 0:
            estimates[name] = value
    return estimates


def pg_inspect_tables(cur, schema: str, table_names: list) -> list[dict]:
    """``[{name, columns, row_count[, row_count_estimated]}]`` for *table_names*
    in *schema* over an open ``RealDictCursor``.

    Row counts come from :func:`pg_row_estimates` (ONE catalog read); an exact
    ``COUNT(*)`` — a full scan on a production source — runs only for a
    relation without statistics (a view, or a table never analyzed). This is
    the single home for the loop both onboarding discovery paths used to
    duplicate.
    """
    estimates = pg_row_estimates(cur, schema)
    tables: list[dict] = []
    for tname in table_names:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, tname),
        )
        columns = [{"name": c["column_name"], "data_type": c["data_type"]} for c in cur.fetchall()]
        validate_identifier(tname)
        if tname in estimates:
            tables.append({"name": tname, "columns": columns, "row_count": estimates[tname],
                           "row_count_estimated": True})
            continue
        cur.execute(table_sql("SELECT COUNT(*) AS cnt FROM {}.{}", schema, tname))
        tables.append({"name": tname, "columns": columns, "row_count": cur.fetchone()["cnt"]})
    return tables


def rows_to_fields(result: dict) -> list[dict]:
    cols = result.get("columns", [])
    rows = result.get("rows", [])
    if not rows:
        return []
    return [{"label": c, "value": rows[0][i]} for i, c in enumerate(cols) if rows[0][i] is not None]


def _graph_backend() -> str:
    """Which graph engine to use: 'neptune' (default, cloud) or 'local' (Memgraph)."""
    return os.environ.get("GRAPH_BACKEND", "neptune").strip().lower()


_MG_DRIVER = None


def _to_plain(value):
    """Convert neo4j/Memgraph graph types to Neptune-Analytics-style JSON shapes.

    The Neptune ExecuteQuery API serializes a returned node as
    {"~id":..., "~labels":[...], "~properties":{...}} — match that so callers that
    render whole nodes (e.g. the graph-viz endpoints) work on both backends.
    Scalars pass through unchanged."""
    try:
        from neo4j.graph import Node, Relationship, Path
    except Exception:
        return value
    if isinstance(value, Node):
        return {"~id": value.element_id, "~labels": list(value.labels), "~properties": dict(value)}
    if isinstance(value, Relationship):
        return {"~id": value.element_id, "~type": value.type, "~properties": dict(value)}
    if isinstance(value, Path):
        return {"~path_length": len(value)}
    # Neptune serializes a node inside a returned map or list the same way as
    # a top-level one; so does this backend.
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def _memgraph_run(cypher: str, parameters=None) -> list:
    """Run openCypher against a local Memgraph over Bolt. Returns a list of
    {alias: value} dicts — the same shape the Neptune ExecuteQuery API yields."""
    global _MG_DRIVER
    from neo4j import GraphDatabase  # local-only dependency (install extra: local)
    if _MG_DRIVER is None:
        uri = os.environ.get("MEMGRAPH_URI", "bolt://localhost:7687")
        _MG_DRIVER = GraphDatabase.driver(uri, auth=("", ""))
    with _MG_DRIVER.session() as session:
        return [
            {k: _to_plain(v) for k, v in record.items()}
            for record in session.run(cypher, **(parameters or {}))
        ]


#: Server-side limit sent with every ExecuteQuery (``queryTimeoutMilliseconds``;
#: the graph default is 30 minutes, the maximum 60). Neptune Analytics counts it
#: from when the query enters its queue and answers UnprocessableException
#: QUERY_TIMEOUT when it is spent, so the server stops a query before the client
#: gives up on it. 60 s keeps the whole window queries had under the SDK
#: default 60 s read timeout (how many finish close to it is not measured).
GRAPH_QUERY_TIMEOUT_MS = 60_000
#: Margin of the client read timeout over the server limit, for the network
#: and the reply.
GRAPH_READ_MARGIN_S = 5
#: Client read timeout for the default limit. AWS's guidance is no read
#: timeout; the app's callers have deadlines (120 s behind the ALB and
#: CloudFront), so it stays bounded.
GRAPH_READ_TIMEOUT_S = GRAPH_QUERY_TIMEOUT_MS // 1000 + GRAPH_READ_MARGIN_S
#: Connect timeout (SDK default 60 s). The graph endpoint is a VPC interface
#: endpoint; a connection it has not accepted in 10 s will not be.
GRAPH_CONNECT_TIMEOUT_S = 10
#: Server limit for a long statement in a background job (``long_graph_statements``
#: inside ``graph_background_job``): the statements whose size grows with the
#: graph and that nothing in the code bounds (a system's removal deletes, the
#: provision's replace deletes, the graph snapshot import). A cap, not a
#: measured duration (none of them has been timed). Never used where an HTTP
#: caller waits: the 120 s web ceiling is shorter, so a longer limit there
#: would only let the work run on after the caller saw a 504.
GRAPH_LONG_QUERY_TIMEOUT_MS = 300_000

#: The server limit of the innermost ``graph_statement_limit`` scope (``None``
#: outside one: ``GRAPH_QUERY_TIMEOUT_MS``).
_QUERY_TIMEOUT_MS: "ContextVar[int | None]" = ContextVar("graph_query_timeout_ms",
                                                         default=None)
#: Whether the code running is a background job no HTTP caller waits on.
_BACKGROUND_JOB: "ContextVar[bool]" = ContextVar("graph_background_job", default=False)


def graph_query_timeout_ms() -> int:
    """The server limit the next graph statement in this context is sent with."""
    return _QUERY_TIMEOUT_MS.get() or GRAPH_QUERY_TIMEOUT_MS


@contextmanager
def graph_statement_limit(timeout_ms: int = GRAPH_LONG_QUERY_TIMEOUT_MS):
    """Send the graph statements inside with server limit ``timeout_ms`` and a
    client read timeout matched to it (``timeout_ms`` plus
    ``GRAPH_READ_MARGIN_S``), unconditionally; usable as a decorator. For code
    that only ever runs as a background job (the snapshot import); anything a
    synchronous caller may also run uses ``long_graph_statements``.
    Context-local: a statement on another thread keeps that thread's limit
    (``asyncio.to_thread`` copies the context in force when it is called)."""
    token = _QUERY_TIMEOUT_MS.set(int(timeout_ms))
    try:
        yield
    finally:
        _QUERY_TIMEOUT_MS.reset(token)


#: The connect timeout of the innermost ``single_attempt_graph_reads`` scope
#: (``None`` outside one: ``GRAPH_CONNECT_TIMEOUT_S`` and ``graph_resubmit``).
_SINGLE_ATTEMPT_CONNECT_S: "ContextVar[float | None]" = ContextVar(
    "graph_single_attempt_connect_s", default=None)


@contextmanager
def single_attempt_graph_reads(*, timeout_ms: int, connect_timeout_s: float):
    """Send the graph reads inside as ONE attempt each, for a read whose
    answer is optional (its caller has a fallback when it fails): server
    limit ``timeout_ms`` (``graph_statement_limit``, read timeout matched),
    client connect timeout ``connect_timeout_s``, and no resubmit
    (``graph_resubmit`` is not consulted, so no backoff wait either). The
    client is the same one-attempt client (``_neptune_client``); only its
    timeouts change. Context-local, like ``graph_statement_limit``. Never
    wrap a write in it: a write's call site decides its own resubmit."""
    with graph_statement_limit(timeout_ms):
        token = _SINGLE_ATTEMPT_CONNECT_S.set(float(connect_timeout_s))
        try:
            yield
        finally:
            _SINGLE_ATTEMPT_CONNECT_S.reset(token)


@contextmanager
def graph_background_job():
    """Mark the code inside as a background job (the provision job, a removal
    run with ``wait=false``): no HTTP caller waits on it, so its
    ``long_graph_statements`` get the long limit. Context-local, like
    ``graph_statement_limit``."""
    token = _BACKGROUND_JOB.set(True)
    try:
        yield
    finally:
        _BACKGROUND_JOB.reset(token)


#: Whether the graph write running is one its call site stated is safe to run
#: twice (``resubmit_safe_write``).
_RESUBMIT_SAFE: "ContextVar[bool]" = ContextVar("graph_write_resubmit_safe", default=False)
#: Whether that statement also covers a dropped connection
#: (``resubmit_safe_write(after_drop=True)``).
_RESUBMIT_AFTER_DROP: "ContextVar[bool]" = ContextVar("graph_write_resubmit_after_drop",
                                                      default=False)


@contextmanager
def resubmit_safe_write(*, after_drop: bool = False):
    """State that the ONE graph write inside has the same effect run twice as
    once, so after a failure that leaves open whether it ran (a 422 resubmit
    request, a 500, a 503, a 409 conflict) it may be sent again
    (``graph_resubmit``). Without it a write is sent again only when it
    certainly never ran. ``after_drop=True`` also states that a copy of it
    still running alongside the resend, or landing after it, leaves the graph
    as one run, so it may be sent again after a dropped connection or a 502
    too (never after a timeout). Only for a statement that writes nothing but
    MERGE steps on nodes matched by id (or an edge between two of them), since
    Neptune Analytics's MERGE step sees concurrent writes and creates one
    element; whose SETs outside ON CREATE write only values that are part of
    the node's own id (a Field's name, schema id and system id), so a late
    copy writes what every other writer of that id writes; and which nothing
    later in the same run deletes. Wrap exactly one statement that has been
    read and checked for that; a test pins each one. Context-local, like
    ``graph_statement_limit``."""
    token = _RESUBMIT_SAFE.set(True)
    drop_token = _RESUBMIT_AFTER_DROP.set(bool(after_drop))
    try:
        yield
    finally:
        _RESUBMIT_AFTER_DROP.reset(drop_token)
        _RESUBMIT_SAFE.reset(token)


@contextmanager
def long_graph_statements(timeout_ms: int = GRAPH_LONG_QUERY_TIMEOUT_MS):
    """``graph_statement_limit(timeout_ms)`` inside a ``graph_background_job``;
    the default limit anywhere else (a synchronous request such as a
    ``wait=true`` removal or a provision confirm). Usable as a decorator."""
    if not _BACKGROUND_JOB.get():
        yield
        return
    with graph_statement_limit(timeout_ms):
        yield


_ENV_REGION = object()


def _neptune_client(region_name=_ENV_REGION, read_timeout_s: "float | None" = None,
                    connect_timeout_s: "float | None" = None):
    """The neptune-graph client every graph read and write uses: ONE attempt
    per call (``total_max_attempts: 1``, standard mode), as AWS recommends for
    ExecuteQuery, so the SDK never resends a query on its own. With SDK
    defaults botocore resent a 429, a 5xx or a read timeout up to four times
    under the same invocation id, which Neptune refused with "Retry for SDK
    query requests is suppressed, please resubmit the query" and the app saw
    only that last error. ``graph_resubmit`` now decides what is sent again.
    The region defaults to ``AWS_REGION`` (else us-east-1); a caller with its
    own region rule passes it (``None`` leaves it to boto3's chain). Built per
    call, as before (no connection is reused across queries). The connect
    timeout defaults to ``GRAPH_CONNECT_TIMEOUT_S`` (shorter inside
    ``single_attempt_graph_reads``)."""
    if region_name is _ENV_REGION:
        region_name = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client(
        "neptune-graph", region_name=region_name,
        config=BotoConfig(retries={"total_max_attempts": 1, "mode": "standard"},
                          read_timeout=read_timeout_s or GRAPH_READ_TIMEOUT_S,
                          connect_timeout=connect_timeout_s or GRAPH_CONNECT_TIMEOUT_S))


def _neptune_execute(cypher: str, parameters=None, *, graph_identifier: "str | None" = None,
                     region_name=_ENV_REGION) -> list:
    """Run openCypher against Amazon Neptune Analytics via the ExecuteQuery data plane.

    The one Neptune call every graph helper makes (and the i3X telemetry
    tools' reads, which name their own graph and region). The client makes
    one attempt (``_neptune_client``); an error a new request can fix is
    resubmitted here, bounded (``graph_resubmit``): a read after anything but
    a timeout, a write only when it certainly never ran, or, inside
    ``resubmit_safe_write``, also after a 422 resubmit request, a 500, a 503
    or a 409 conflict, and with ``after_drop`` after a dropped connection.
    Every other error reaches the caller on the first attempt, as before.
    Inside ``single_attempt_graph_reads`` nothing is resubmitted: the first
    attempt's error reaches the caller."""
    def once():
        return _neptune_execute_once(cypher, parameters, graph_identifier=graph_identifier,
                                     region_name=region_name)
    if _SINGLE_ATTEMPT_CONNECT_S.get() is not None:
        return once()
    return graph_resubmit.run(
        once,
        cypher, log=logger, resubmit_safe=_RESUBMIT_SAFE.get(),
        after_drop=_RESUBMIT_AFTER_DROP.get())


def neptune_execute(cypher: str, parameters=None, *, graph_identifier: str,
                    region_name=_ENV_REGION) -> list:
    """Neptune Analytics ExecuteQuery for a caller that names its own graph
    and region (the i3X telemetry tools), through the same one-attempt client,
    server limit and resubmit as every other graph statement. Always the
    Neptune backend; raises on failure."""
    return _neptune_execute(cypher, parameters, graph_identifier=graph_identifier,
                            region_name=region_name)


def _neptune_execute_once(cypher: str, parameters=None, *,
                          graph_identifier: "str | None" = None,
                          region_name=_ENV_REGION) -> list:
    """One ExecuteQuery call (see ``_neptune_execute``), under the limit of
    the current ``graph_statement_limit`` scope (and the connect timeout of
    the current ``single_attempt_graph_reads`` scope)."""
    timeout_ms = graph_query_timeout_ms()
    connect_s = _SINGLE_ATTEMPT_CONNECT_S.get()
    client = _neptune_client(region_name, timeout_ms / 1000 + GRAPH_READ_MARGIN_S,
                             **({"connect_timeout_s": connect_s} if connect_s else {}))
    kwargs = {
        "graphIdentifier": graph_identifier or os.environ.get("GRAPH_IDENTIFIER", "adt"),
        "queryString": cypher,
        "language": "OPEN_CYPHER",
        "queryTimeoutMilliseconds": timeout_ms,
    }
    if parameters:
        kwargs["parameters"] = parameters
    resp = client.execute_query(**kwargs)
    body = json.loads(resp["payload"].read().decode())
    results = body.get("results")
    if not isinstance(results, list):
        # No results list is no answer, not an answer of no rows: noted for a
        # caller that acts on what a read did not return
        # (``recording_incomplete_reads``), still [] for everyone else.
        note_incomplete_read("no_results")
        return []
    return results


class GraphQueryError(RuntimeError):
    """A graph query the backend rejected or could not run.

    ``category`` is the exception class name (``ValidationException`` for an
    openCypher the engine refused); ``detail`` is the backend's own message,
    bounded, with ARNs and account ids masked (``_graph_error_detail``)."""

    def __init__(self, category: str, detail: str):
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


#: The incomplete reads of the innermost ``recording_incomplete_reads`` scope
#: (``None`` outside one, where nothing is collected).
_INCOMPLETE_READS: "ContextVar[list | None]" = ContextVar("incomplete_reads", default=None)


@contextmanager
def recording_incomplete_reads():
    """Collect, for the code inside it, every read that answered less than its
    source holds: a graph read ``neptune_query`` degraded to no rows, and
    whatever a caller notes itself (``note_incomplete_read``: a ``pg`` result
    marked ``unavailable`` or ``truncated``, a step whose failure it swallowed).
    Yields the list, one category per incomplete read. For a caller that acts
    on what a read did NOT return (a reconcile that deletes what is absent),
    so must tell "no rows" from "no answer". Context-local: a read on another
    thread is not collected."""
    sink: list = []
    token = _INCOMPLETE_READS.set(sink)
    try:
        yield sink
    finally:
        _INCOMPLETE_READS.reset(token)


def note_incomplete_read(category: str) -> None:
    """Record one incomplete read in the current ``recording_incomplete_reads``
    scope; nothing outside one."""
    sink = _INCOMPLETE_READS.get()
    if sink is not None:
        sink.append(str(category))


def incomplete_read_count() -> int:
    """How many incomplete reads the current scope has recorded (0 outside one)."""
    sink = _INCOMPLETE_READS.get()
    return len(sink) if sink is not None else 0


_GRAPH_ERROR_DETAIL_CHARS = 300
_ARN_RE = re.compile(r"arn:[A-Za-z0-9\-]+:[^\s\"',;)]+")
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")


def _graph_error_detail(exc: Exception) -> str:
    """The backend's message for ``exc``: a botocore ``Error.Message`` when
    present, else ``str(exc)``; ARNs and 12-digit account ids masked, whitespace
    collapsed, capped at ``_GRAPH_ERROR_DETAIL_CHARS``."""
    response = getattr(exc, "response", None)
    message = ""
    if isinstance(response, dict):
        message = str((response.get("Error") or {}).get("Message") or "")
    message = message or str(exc)
    message = _ACCOUNT_RE.sub("<account>", _ARN_RE.sub("<arn>", message))
    return " ".join(message.split())[:_GRAPH_ERROR_DETAIL_CHARS]


@stage_timing.timed("io.graph_read")
def neptune_query_checked(cypher: str, parameters=None) -> list:
    """Execute an openCypher query; RAISE ``GraphQueryError`` on failure.

    For callers that must tell "no rows" from "the query was rejected" —
    ``graph_tools.explore_graph`` runs model-written openCypher, and reporting a
    rejected query as an empty graph told the model the data did not exist.
    Same backend switch and failure log line as ``neptune_query``. A
    statement the read-only classifier finds no write in is a read, so a
    leased operation's lost lease does not stop it; any other statement (a
    write, or one it cannot read) runs the bound lease check first, on both
    backends (``lease_guard.graph_read``)."""
    try:
        with graph_read(cypher):
            if _graph_backend() == "local":
                return _memgraph_run(cypher, parameters)
            return _neptune_execute(cypher, parameters)
    except Exception as exc:
        logger.warning(
            "Neptune query failed backend=%s category=%s",
            _graph_backend(), type(exc).__name__,
        )
        raise GraphQueryError(type(exc).__name__, _graph_error_detail(exc)) from exc


def neptune_query(cypher: str, parameters=None) -> list:
    """Execute an openCypher query. Routes to Memgraph (GRAPH_BACKEND=local) or
    Neptune Analytics (default, cloud). Returns list of result dicts; [] on failure.

    Both backends run the SAME openCypher — this is the single switch point, so
    queries stay DRY (no per-engine reimplementation). See ADR 0003. The
    return-[]-on-failure degrade is deliberate (ADR-0069); a caller that must
    see the failure uses ``neptune_query_checked``. Because the degrade hides
    the failure from the caller, the engine's message (masked and bounded as in
    ``GraphQueryError.detail``) is logged here, and noted for a caller inside
    ``recording_incomplete_reads``."""
    try:
        return neptune_query_checked(cypher, parameters)
    except GraphQueryError as exc:
        logger.warning("Neptune query degraded to no rows category=%s detail=%s",
                       exc.category, exc.detail)
        note_incomplete_read(exc.category)
        return []


#: What ``neptune_write_outcome`` answers for a write that raised an error
#: ``graph_resubmit.failure_kind`` does not name (a validation error, a
#: denial, a server-side limit, a local-backend error).
WRITE_UNCLASSIFIED = "unclassified"


def _graph_write_outcome(cypher: str, parameters=None) -> "str | None":
    """The body of ``neptune_write`` and ``neptune_write_outcome``: ``None``
    when the write was confirmed, else how it ended (logged). The bound
    ``write_guard`` runs first and its error propagates."""
    check_write_guard()
    try:
        if _graph_backend() == "local":
            _memgraph_run(cypher, parameters)
        else:
            _neptune_execute(cypher, parameters)
        return None
    except Exception as exc:
        logger.warning(
            "Neptune write failed backend=%s category=%s detail=%.300s cypher=%.160s",
            _graph_backend(), type(exc).__name__, str(exc), (cypher or "").replace("\n", " "),
        )
        return graph_resubmit.failure_kind(exc) or WRITE_UNCLASSIFIED


@stage_timing.timed("io.graph_write")
def neptune_write(cypher: str, parameters=None) -> bool:
    """Execute a write (CREATE/MERGE). Routes to Memgraph (GRAPH_BACKEND=local) or
    Neptune Analytics (default, cloud). Returns success bool."""
    return _graph_write_outcome(cypher, parameters) is None


@stage_timing.timed("io.graph_write")
def neptune_write_outcome(cypher: str, parameters=None) -> "str | None":
    """``neptune_write`` for a caller that must know how a write that was not
    confirmed ended: ``None`` when it was confirmed, else
    ``graph_resubmit.failure_kind`` of the error that ended it (after any
    resubmits), or ``WRITE_UNCLASSIFIED``. ``graph_resubmit.TIMED_OUT`` (a
    read timeout or a 504) means the first copy may still be running until
    the server limit it was sent with (``graph_query_timeout_ms``) stops it.
    Same routing, resubmits and failure log line as ``neptune_write``."""
    return _graph_write_outcome(cypher, parameters)


@stage_timing.timed("io.graph_write")
def neptune_write_returning(cypher: str, parameters=None) -> "list | None":
    """Execute a write that RETURNs what it wrote; answer its rows, or ``None``
    when the backend refused or could not run it (same failure log line as
    ``neptune_write``). One statement is one transaction on both backends, so
    a caller that needs several changes to land together — a node and its
    edges — writes them in one statement and treats an expected row as the
    readback that they did. The bound ``write_guard`` runs first and its
    error propagates."""
    check_write_guard()
    try:
        if _graph_backend() == "local":
            return _memgraph_run(cypher, parameters)
        return _neptune_execute(cypher, parameters)
    except Exception as exc:
        logger.warning(
            "Neptune write failed backend=%s category=%s detail=%.300s cypher=%.160s",
            _graph_backend(), type(exc).__name__, str(exc), (cypher or "").replace("\n", " "),
        )
        return None


class GraphWriteConflict(RuntimeError):
    """The graph aborted a write on a concurrent modification of the same
    data (Neptune Analytics ``ConflictException``, reason
    ``CONCURRENT_MODIFICATION``): the statement's transaction was rolled
    back, so it did not take effect. Raised only by
    :func:`neptune_write_returning_cas`."""


def neptune_write_returning_cas(cypher: str, parameters=None) -> "list | None":
    """:func:`neptune_write_returning` for a compare-and-set statement (its
    WHERE compares the value it replaces, so its caller reads again before it
    resends): its rows, ``None`` when the backend refused or could not run
    it, and :class:`GraphWriteConflict` when the graph aborted it on a
    concurrent modification, so the caller can read again and resend rather
    than report a failure. Nothing is resent here (``graph_resubmit``'s rule
    for a write is unchanged). The bound ``write_guard`` runs first and its
    error propagates."""
    from . import graph_resubmit
    check_write_guard()
    try:
        if _graph_backend() == "local":
            return _memgraph_run(cypher, parameters)
        return _neptune_execute(cypher, parameters)
    except Exception as exc:
        if graph_resubmit.failure_kind(exc) == graph_resubmit.CONFLICT:
            logger.info("Neptune compare-and-set aborted on a concurrent modification "
                        "cypher=%.160s", (cypher or "").replace("\n", " "))
            raise GraphWriteConflict(str(exc)[:300]) from exc
        logger.warning(
            "Neptune write failed backend=%s category=%s detail=%.300s cypher=%.160s",
            _graph_backend(), type(exc).__name__, str(exc), (cypher or "").replace("\n", " "),
        )
        return None


#: Output budget per model family. Nova Pro's output ceiling is below the
#: Anthropic one; a structured-output call never needs more than this.
_CONVERSE_MAX_TOKENS = {"nova": 4096, "default": 8192}


def _converse_request(prompt: str, model_id: str, *, system: str = "",
                      max_tokens: int | None = None) -> dict:
    """The one Bedrock request shape: Converse ``messages`` for
    every model family, so the app has ONE reader for the provider.

    temperature 0 (greedy decoding): every bedrock_call consumer is a
    structured-output task (field mapping, concept proposals, identity
    resolution) where sampling variance is pure nondeterminism — measured
    2026-08-06: two same-day onboards of the same system diverged on 13
    identity rules and a dozen mapping confidences/roles at the provider
    default temperature. Greedy decoding is not perfect determinism, but it
    removes the dominant variance source. A model the catalog says rejects a
    temperature (Sonnet 5, ValidationException) gets none and its default
    applies."""
    from .model_catalog import accepts_temperature
    family = "nova" if "nova" in model_id else "default"
    config: dict = {"maxTokens": int(max_tokens or _CONVERSE_MAX_TOKENS[family])}
    if accepts_temperature(model_id):
        config["temperature"] = 0.0
    request = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": config,
    }
    if system:
        request["system"] = [{"text": system}]
    return request


def converse_output_text(response: dict) -> str:
    """The text of a Converse response, wherever it sits.

    ``output.message.content`` is a list of typed blocks and the text is not
    always block zero: Claude Sonnet 5 answered the onboarding map escalation
    with a leading non-text block (event 22, 2026-10-02) and the old
    ``content[0]["text"]`` read raised KeyError, so every escalation fell
    back to the weak mapping. Text blocks are joined in order; a response
    with none fails naming the block kinds and stop reason it did carry."""
    blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
    texts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
    if not texts:
        kinds = [next(iter(b), "?") if isinstance(b, dict) and b else type(b).__name__
                 for b in blocks]
        raise ValueError(f"Converse response carried no text block: blocks={kinds} "
                         f"stopReason={response.get('stopReason')}")
    return "".join(texts)


def converse_text(client, model_id: str, prompt: str, *, system: str = "",
                  max_tokens: int | None = None) -> str:
    """One Converse round trip, text out. The entry for every caller that
    used to hand-build an InvokeModel body (``prompts``, ``video_briefing``)."""
    return converse_output_text(client.converse(
        **_converse_request(prompt, model_id, system=system, max_tokens=max_tokens)))


def _invoke_bedrock(client, prompt: str, model_id: str, attempt: int) -> str:
    logger.debug("[BEDROCK] converse attempt %s model=%s", attempt + 1, model_id)
    return converse_text(client, model_id, prompt)


_RETRYABLE_BEDROCK_CODES = frozenset({
    "ServiceUnavailableException", "ThrottlingException", "ModelTimeoutException",
})


class ModelConstructionError(RuntimeError):
    """A model that could not be built (a Mantle token that could not be
    minted, a missing dependency). ``bedrock_call`` raises it at once and
    never lets another model answer in its place (ADR-0055 (d)); only a
    failure of a model that was built falls back."""


def _invoke_mantle(prompt: str, model_id: str, attempt: int, region: str) -> str:
    """One tool-free turn on a model only Bedrock Mantle serves (GPT-5.x,
    ADR-0055), text out.

    ``build_model`` binds the id to Mantle with the output budget a non-Nova
    Converse request gets. The turn runs in a strands Agent with no tools, no
    printing callback, no conversation manager and no retry of its own, so
    retries and fallback stay ``bedrock_call``'s. A reply stopped at the cap
    raises (strands' MaxTokensReachedException) rather than returning half of
    it. The text is read the way a Converse reply is: the text blocks in order,
    without the reasoning block."""
    from strands import Agent
    from strands.agent.conversation_manager import NullConversationManager
    from .model_provider import build_model
    logger.debug("[BEDROCK] mantle attempt %s model=%s", attempt + 1, model_id)
    try:
        model = build_model(model_id, max_tokens=_CONVERSE_MAX_TOKENS["default"], region=region)
    except Exception as exc:  # a model that cannot be built is never substituted
        raise ModelConstructionError(
            f"model {model_id!r} could not be built: {type(exc).__name__}: {exc}") from exc
    agent = Agent(model=model, tools=[], callback_handler=None, retry_strategy=None,
                  conversation_manager=NullConversationManager())
    result = agent(prompt)
    return converse_output_text({"output": {"message": result.message},
                                 "stopReason": result.stop_reason})


def _invoke_model(client, prompt: str, model_id: str, attempt: int, region: str) -> str:
    """One try on *model_id*: Bedrock Mantle for a GPT-5.x id, Converse for
    every other id (``_invoke_bedrock``, unchanged)."""
    from .model_provider import is_mantle_only
    if is_mantle_only(model_id):
        return _invoke_mantle(prompt, model_id, attempt, region)
    return _invoke_bedrock(client, prompt, model_id, attempt)


def _mantle_retry_code(exc: Exception) -> str:
    """The Converse error code a transient Mantle failure stands for, else "".

    The OpenAI client behind a Mantle model raises a status error: a throttle
    (HTTP 429, which strands re-raises as ModelThrottledException) is
    ThrottlingException, a 5xx is ServiceUnavailableException and a 408 is
    ModelTimeoutException. A client-side timeout is not retried, as a
    Converse read timeout is not."""
    try:
        import openai
        from strands.types.exceptions import ModelThrottledException
    except ImportError:  # no Mantle dependencies, so the error is not Mantle's
        return ""
    if isinstance(exc, (ModelThrottledException, openai.RateLimitError)):
        return "ThrottlingException"
    status = exc.status_code if isinstance(exc, openai.APIStatusError) else 0
    if status >= 500:
        return "ServiceUnavailableException"
    return "ModelTimeoutException" if status == 408 else ""


def _retry_code(exc: Exception, model_id: str) -> str:
    """Log a failed try; return the retryable error code it carries, or ""
    when it is not retried. A Converse ClientError keeps its own code."""
    from botocore.exceptions import ClientError
    if isinstance(exc, ClientError):
        code = exc.response["Error"]["Code"]
        logger.warning("[BEDROCK] client error category=%s model=%s", code, model_id)
        return code if code in _RETRYABLE_BEDROCK_CODES else ""
    logger.warning("[BEDROCK] request failed category=%s model=%s",
                   type(exc).__name__, model_id)
    return _mantle_retry_code(exc)


def _first_attempt_models(region: str, orchestrator_model: str) -> list[str]:
    """The models the first attempt tries, in order: the configured
    system-agent model (``model_config.configured_agent_model``: the Admin >
    Settings save, else ``SYSTEM_AGENT_MODEL``, else the catalog default under
    the allowlist), then the orchestrator model; once when they are the same
    id. Resolved on every call and never cached, so a save applies to the
    next call in every process (ADR-0055); the SSM client behind the read is
    built once per process. The orchestrator model stays last, so its error
    is the one raised."""
    from .model_config import configured_agent_model
    candidates = (configured_agent_model(region), orchestrator_model)
    return list(dict.fromkeys(m for m in candidates if m))


@stage_timing.timed("io.model")
def bedrock_call(prompt: str, timeout: int = 90, model: str | None = None) -> str:
    """Call Bedrock synchronously and return the reply text.

    Without ``model`` the first attempt tries the deployment's configured
    system-agent model, then the orchestrator model when that call fails;
    after a throttle or a transient error every retry uses the orchestrator
    model. An explicit ``model`` is the only model tried. A GPT-5.x id runs
    on Bedrock Mantle, every other id on Converse (``_invoke_model``). A model
    that cannot be built raises ``ModelConstructionError`` at once."""
    from .aws_session import bedrock_runtime_client
    from .model_catalog import env_orchestrator_model

    region = os.environ.get("AWS_REGION", "us-east-1")
    primary_model = model or env_orchestrator_model(region)
    first_try = [model] if model else _first_attempt_models(region, primary_model)
    client = bedrock_runtime_client(region)
    for attempt in range(4):
        model_ids = first_try if attempt == 0 else [primary_model]
        for index, model_id in enumerate(model_ids):
            try:
                result = _invoke_model(client, prompt, model_id, attempt, region)
                logger.debug("[BEDROCK] response_len=%s model=%s", len(result), model_id)
                return result
            except ModelConstructionError:
                raise
            except Exception as exc:
                code = _retry_code(exc, model_id)
                if code and attempt < 3:
                    stage_timing.incr("io.model.throttled" if code == "ThrottlingException"
                                      else "io.model.retried")
                    time.sleep(2 ** attempt)
                    break
                if index == len(model_ids) - 1:
                    raise
    raise RuntimeError("bedrock_call exhausted all retries")


def bedrock_stream(prompt: str, system: str = ""):
    """Yields text chunks from the Bedrock Converse streaming API."""
    from .aws_session import bedrock_runtime_client
    from .model_catalog import env_orchestrator_model
    client = bedrock_runtime_client(read_timeout=300)
    model_id = env_orchestrator_model()
    request = _converse_request(prompt, model_id, system=system, max_tokens=4000)
    request["inferenceConfig"].pop("temperature", None)  # a narrative stream keeps the model default
    resp = client.converse_stream(**request)
    for event in resp['stream']:
        text = ((event.get('contentBlockDelta') or {}).get('delta') or {}).get('text', '')
        if text:
            yield text
