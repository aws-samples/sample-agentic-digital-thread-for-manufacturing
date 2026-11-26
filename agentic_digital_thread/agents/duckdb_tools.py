# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic DuckDB-over-S3-Tables connector tools for a system agent.

A GENERIC connection type, the in-process sibling of :mod:`agents.athena_tools`.
Both read the same Amazon S3 Tables (Apache Iceberg) data; this one reads it
through embedded DuckDB (the official `duckdb` package) instead of Amazon Athena,
so it has no per-query Athena scan cap and warm reads are fast. It is ADDITIVE:
it replaces nothing. An existing Athena or SiteWise system is untouched; this is
one more connection type a deployment can choose (see ADR-0099).

A DuckDB/S3 Tables system is described entirely by its ``connection_config``:

    {
      "table_bucket_arn": "arn:aws:s3tables:<region>:<acct>:bucket/<name>",  # required
      "catalog_endpoint": "s3_tables",     # optional: "s3_tables" (default) | "glue"
      "role_arn":         "arn:aws:iam::<acct>:role/<name>",  # optional (cross-account)
      "external_id":      "<sts external id>",  # optional secret, with role_arn
      "namespaces":       ["ns1", "ns2"],   # optional scope (empty = all)
      "tables":           ["ns.table"],      # optional scope (empty = all)
      "duckdb_memory_limit": "2GB",          # optional DuckDB tuning
      "duckdb_threads":   2,                   # optional
      "query_timeout_s":  120.0,               # optional
      "max_rows":         500                  # optional row cap (default sql_safety.MAX_ROWS)
    }

Nothing here is specific to any dataset. The engine recognizes ADT's SFC
``readings`` schema as a first-class layout, but these tools expose generic
list / describe / read-only-SQL operations over WHATEVER tables the bucket
holds, so the connector works on any S3 Tables bucket, not just our seed data.

Credentials use the ambient boto3 chain (the agent task role in the cloud, the
developer's profile locally) via DuckDB's ``credential_chain`` secret, exactly
like the Athena and SiteWise (Direct) connectors. There is no pasted secret and
no Secrets Manager ARN: from onboarding's point of view this is a SECRETLESS,
structured-config connector, the same shape as Athena.

Read-only: the catalog is attached ``READ_ONLY`` (the engine never configures
write tables here), and every model-written statement additionally passes
:func:`sql_safety.ensure_select_only` and :func:`sql_safety.apply_row_cap`
before it reaches DuckDB, the same guards Athena applies.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

from .public_errors import public_error, public_error_text
from .sql_safety import MAX_ROWS, apply_row_cap, ensure_select_only

logger = logging.getLogger(__name__)

# The attached-catalog name we ATTACH the S3 Tables bucket as.
CATALOG = "s3t"

# S3 Tables bucket ARN. We parse region + bucket name out of it ourselves rather
# matches arn:aws[-partition]:s3tables:<region>:<acct>:bucket/<name>; we parse
# cannot break us. Matches arn:aws[-partition]:s3tables:<region>:<acct>:bucket/<name>.
_BUCKET_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:s3tables:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):bucket/(?P<name>[a-z0-9-]{3,63})$"
)

# How the bucket's Iceberg catalog is reached. ``s3_tables`` is the S3 Tables
# Iceberg REST endpoint (same-account, or a bucket policy that allows the
# caller). ``glue`` is the Glue Iceberg REST endpoint over the bucket's
# federated ``s3tablescatalog`` entry, governed by Lake Formation: the path a
# bucket in ANOTHER account takes when it is shared through Lake Formation
# (the same grants the Athena connector reads through).
_CATALOG_ENDPOINTS = ("s3_tables", "glue")
_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,128}$")
# Reconnect with fresh assumed-role credentials this long before they expire,
# so a long-lived warm reader never runs a query on credentials about to lapse.
_CREDENTIAL_REFRESH_MARGIN_S = 300


class DuckDBConfigError(ValueError):
    """The connection_config is missing or invalid (e.g. no table_bucket_arn).

    A :data:`agents.public_errors.SAFE_MESSAGE_TYPES` member: it validates the
    caller's own settings and quotes back only that input, so its message is
    surfaced verbatim.
    """


class DuckDBAuthorizationError(ValueError):
    """Model-written SQL was refused by the fail-closed authorization pass.

    A :data:`agents.public_errors.SAFE_MESSAGE_TYPES` member: the message states
    the fixed reason a statement is out of bounds (not a SELECT/WITH, a table
    function, a file/URL/secret reach, or an out-of-scope relation) and never
    echoes engine internals, so it surfaces verbatim as fix-the-SQL guidance.
    """


# Base-relation scope. Either a legacy tuple of (namespace, table) pairs, or a
# :class:`ScopeSpec` that keeps the namespace and table constraints separate.
# Empty = the whole bucket (the documented default grant).
Scope = tuple


class ScopeSpec(tuple):
    """The read scope of one system: ``tables`` pairs and ``namespaces``.

    A tuple subclass whose items are the ``(namespace, table)`` pairs, so code
    that only knows the legacy pairs tuple keeps working. ``namespaces`` and
    ``pairs`` are enforced independently and intersected: a relation must be in
    a configured namespace when any is set, and in a configured pair when any
    table was configured. ``deny_all`` is set when tables were configured but
    none parsed, so a bad entry never widens to the whole bucket.
    """

    def __new__(cls, pairs=(), namespaces=(), deny_all=False):
        self = super().__new__(cls, tuple(pairs))
        self.namespaces = frozenset(n.lower() for n in namespaces)
        self.deny_all = bool(deny_all)
        return self


def _scope_allows(scope: "Scope", namespace: str, table: str) -> bool:
    """True when (namespace, table) is within ``scope`` (both constraints)."""
    namespaces = getattr(scope, "namespaces", frozenset())
    if getattr(scope, "deny_all", False):
        return False
    if namespaces and namespace.lower() not in namespaces:
        return False
    if not scope:
        return True
    want = (namespace.lower(), table.lower())
    return any((ns.lower(), tbl.lower()) == want for ns, tbl in scope)


def _authz_fail(reason: str) -> "DuckDBAuthorizationError":
    return DuckDBAuthorizationError(
        "This query is not allowed against an S3 Tables system: " + reason
        + ". Use a single read-only SELECT/WITH over s3t.<namespace>.<table> tables only."
    )


# Scalar/table function NAME prefixes and names that read data outside the
# attached catalog (files, URLs, secrets, other databases, or dynamic SQL).
# A DuckDB function such as read_text/read_blob works in SCALAR position too, so
# the base-relation allowlist alone is not enough: any function whose name hits
# this list is rejected wherever it appears (select list, WHERE, subquery, CTE).
_FORBIDDEN_FN_PREFIXES = ("read_", "parquet_", "sniff_")
_FORBIDDEN_FN_NAMES = frozenset({
    "glob", "query", "query_table", "getvariable",
    "duckdb_secrets", "duckdb_settings", "duckdb_databases", "duckdb_extensions",
    "which_secret", "pragma_database_list",
})


def _fn_forbidden(name: str) -> bool:
    n = (name or "").lower()
    return (n in _FORBIDDEN_FN_NAMES
            or n.startswith(_FORBIDDEN_FN_PREFIXES)
            or "secret" in n)


# --- config bound-checking (DDB-08): fail-closed, product-capped ------------
_MAX_ROWS_CEILING = 100_000     # hard product max; operator config is clamped to it
_INT_MAX = 2**63 - 1            # accept any positive int for max_rows, then clamp
_MAX_DUCKDB_THREADS = 64        # a sane upper bound for an in-process query thread pool
_MAX_QUERY_TIMEOUT_S = 900.0    # must sit under the per-turn/proxy ceilings
#: Seconds one query may run when ``query_timeout_s`` is not configured. Well
#: under the AgentCore gateway's ~300 s tool-call ceiling, so an unbounded scan
#: stops with guidance the agent can act on instead of the gateway dropping the
#: whole system call (Athena's data-query poll limit is 60 s).
_DEFAULT_QUERY_TIMEOUT_S = 120.0
#: The most any one query may run, whatever ``query_timeout_s`` says: a margin
#: under the gateway's ~300 s ceiling for the rest of the system call.
_GATEWAY_SAFE_QUERY_TIMEOUT_S = 240.0


def _time_limit_error(seconds: float, data_query: bool) -> "QueryError":
    """The error for a read stopped at the time limit. A data query gets
    narrowing guidance; a metadata read (table listing, describe) gets
    guidance it can follow. Worded without any of the orchestrator's
    transient-fault markers, so it is acted on rather than retried as written."""
    logger.info("duckdb read stopped at the %gs limit (data_query=%s)", seconds, data_query)
    if data_query:
        return QueryError("query_time_limit", (
            f"The query did not finish within {seconds:g} s and was stopped: it reads "
            "too much data for one query. Narrow the WHERE filter on the time column the "
            "question asks about and select fewer columns, then run it again. A LIMIT "
            "bounds only the rows returned, not what an aggregate reads."))
    return QueryError("metadata_time_limit", (
        f"Reading this system's table metadata did not finish within {seconds:g} s and "
        "was stopped. It does not depend on any filter you can change: ask for it once "
        "more, and if it stops again, say that the table metadata could not be read."))


class _QueryStopper:
    """Interrupts one cursor at a time limit, synchronized with its close.

    The timer callback and :meth:`finish` share one lock: once ``finish``
    returns, the callback has either run to completion or will do nothing, so
    an interrupt never reaches a cursor the caller has since closed."""

    def __init__(self, cur, seconds: float):
        self._cur = cur
        self._lock = threading.Lock()
        self._done = False
        self._stopped = False
        self._timer = threading.Timer(seconds, self._stop)
        self._timer.daemon = True
        self._timer.start()

    def _stop(self) -> None:
        with self._lock:
            if self._done:
                return
            self._stopped = True
            try:
                self._cur.interrupt()
            except Exception as exc:  # noqa: BLE001 - a finished query needs no interrupt
                logger.debug("time-limit interrupt not delivered (%s)", type(exc).__name__)

    def finish(self) -> bool:
        """End the time limit (idempotent); True when it stopped the query."""
        self._timer.cancel()
        with self._lock:
            self._done = True
            return self._stopped
# DuckDB memory-limit grammar (e.g. "2GB", "512MiB"): a positive number + unit.
_MEMORY_LIMIT_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)
# A scope identifier (namespace or table name) — conservative, no dots/quotes.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _bounded_int(value: Any, field: str, *, low: int, high: int) -> int:
    # Reject bool (a subclass of int), non-ints, and out-of-range values.
    if isinstance(value, bool) or not isinstance(value, int):
        raise DuckDBConfigError(f"connection_config.{field} must be an integer")
    if not (low <= value <= high):
        raise DuckDBConfigError(f"connection_config.{field} must be between {low} and {high}")
    return value


def _bounded_float(value: Any, field: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DuckDBConfigError(f"connection_config.{field} must be a number")
    v = float(value)
    if not math.isfinite(v) or not (low <= v <= high):
        raise DuckDBConfigError(f"connection_config.{field} must be between {low} and {high}")
    return v


def _valid_memory_limit(value: Any) -> str:
    s = str(value).strip()
    if not _MEMORY_LIMIT_RE.match(s):
        raise DuckDBConfigError(
            "connection_config.duckdb_memory_limit must look like '2GB' or '512MiB'")
    return s


def _valid_identifier(value: Any, field: str) -> str:
    s = str(value).strip()
    if not _IDENT_RE.match(s):
        raise DuckDBConfigError(f"connection_config.{field} has an invalid name: {s!r}")
    return s


def _valid_scope_entry(value: Any) -> str:
    parts = str(value).strip().split(".")
    if len(parts) != 2 or not all(_IDENT_RE.match(p) for p in parts):
        raise DuckDBConfigError(
            f"connection_config.tables entry must be 'namespace.table': {value!r}")
    return ".".join(parts)


def _walk_json(node, on_table_function, on_base_table, on_function=None):
    """Recurse the DuckDB json_serialize_sql tree, invoking the callbacks for
    every TABLE_FUNCTION and BASE_TABLE ``from_table`` node, and (when given) for
    every scalar FUNCTION-class node, encountered anywhere in the statement."""
    if isinstance(node, dict):
        t = node.get("type")
        if t == "TABLE_FUNCTION":
            on_table_function(node)
        elif t == "BASE_TABLE":
            on_base_table(node)
        if on_function is not None and (node.get("class") == "FUNCTION" or t == "FUNCTION"):
            on_function(node)
        for v in node.values():
            _walk_json(v, on_table_function, on_base_table, on_function)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, on_table_function, on_base_table, on_function)


def _collect_cte_names(node) -> set:
    """Every CTE name defined anywhere in the statement tree, lower-cased. A CTE
    reference is a local alias, not a base relation, so these are exempt from the
    catalog/scope check."""
    names: set = set()

    def walk(n):
        if isinstance(n, dict):
            cm = n.get("cte_map")
            if isinstance(cm, dict):
                for item in cm.get("map", []) or []:
                    key = item.get("key")
                    if isinstance(key, str):
                        names.add(key.lower())
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return names


def _parse_select_node(sql: str) -> dict:
    """Parse ONE read-only SQL statement with the DuckDB dialect and return its
    query node. Fail-closed: rejects ';'-chaining / DDL / DML (via
    ``ensure_select_only``), an unparseable statement, more than one statement,
    or a top node that is not a SELECT / set-operation / WITH query. Raises
    :class:`DuckDBAuthorizationError` on any violation."""
    try:
        ensure_select_only(sql)  # raises a bare ValueError
    except ValueError as e:
        raise _authz_fail(str(e)) from None
    import duckdb  # lazy; the module imports where duckdb is not installed

    try:
        parsed = json.loads(duckdb.connect().execute(
            "SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    except Exception:  # noqa: BLE001 - any parse failure fails closed
        raise _authz_fail("it could not be parsed as DuckDB SQL") from None
    if parsed.get("error"):
        raise _authz_fail("it is not a valid DuckDB statement")
    statements = parsed.get("statements") or []
    if len(statements) != 1:
        raise _authz_fail("exactly one statement is allowed")
    node = statements[0].get("node") or {}
    if node.get("type") not in ("SELECT_NODE", "SET_OPERATION_NODE", "CTE_NODE"):
        raise _authz_fail("only a SELECT or WITH query is allowed")
    return node


def authorize_duckdb_sql(sql: str, scope: Scope = ()) -> None:
    """Fail-closed authorization for ONE model-written DuckDB statement.

    A valid DuckDB ``SELECT`` can read local files, HTTP/S3 URLs, secret
    metadata, or run dynamic SQL through table/scalar functions. This is the
    data-access trust boundary: it allows only a single read-only ``SELECT`` /
    ``WITH`` whose every base relation is an in-scope ``s3t.<namespace>.<table>``
    table, and rejects every table function and every non-``s3t`` catalog. Raises
    :class:`DuckDBAuthorizationError` on any violation; returns None when safe.

    ``scope`` is the (namespace, table) allowlist; empty means the whole bucket
    (the documented default grant).
    """
    node = _parse_select_node(sql)
    violations: list[str] = []
    # CTE names are local aliases, not relations: a reference to one appears as a
    # bare single-segment BASE_TABLE (empty catalog). Collect every CTE name in
    # the tree so those references are not mistaken for out-of-catalog tables.
    cte_names = _collect_cte_names(node)

    def _on_table_function(tf: dict) -> None:
        name = ((tf.get("function") or {}).get("function_name") or "a table function")
        violations.append(f"the table function {name}() is not allowed")

    def _on_function(fn: dict) -> None:
        name = fn.get("function_name") or ""
        if _fn_forbidden(name):
            violations.append(f"the function {name}() is not allowed")

    def _on_base_table(bt: dict) -> None:
        path = ((bt.get("qualified_name") or {}).get("path")) or []
        catalog = bt.get("catalog_name") or (path[0] if len(path) == 3 else "")
        ns = bt.get("schema_name") or (path[-2] if len(path) >= 2 else "")
        tbl = bt.get("table_name") or (path[-1] if path else "")
        # A bare single-segment reference to a CTE name is a local alias, allowed.
        if not catalog and len(path) == 1 and path[0].lower() in cte_names:
            return
        if (catalog or "").lower() != CATALOG:
            violations.append(f"relation {'.'.join(path) or tbl} is outside the {CATALOG} catalog")
        elif not _scope_allows(scope, ns, tbl):
            violations.append(f"relation {ns}.{tbl} is outside this system's configured scope")

    _walk_json(node, _on_table_function, _on_base_table, _on_function)
    if violations:
        raise _authz_fail(violations[0])


def _resolve_config(connection_config: Optional[dict]) -> Dict[str, Any]:
    """Validate and normalize the non-secret connection_config.

    Raises :class:`DuckDBConfigError` when ``table_bucket_arn`` is absent or
    malformed, so a misconfigured system fails fast at tool-build time (eager
    validation), the same contract as ``athena_tools._resolve_config``.
    """
    cfg = dict(connection_config or {})
    arn = str(cfg.get("table_bucket_arn") or "").strip()
    if not arn:
        raise DuckDBConfigError("connection_config.table_bucket_arn is required")
    if _BUCKET_ARN_RE.match(arn) is None:
        raise DuckDBConfigError("connection_config.table_bucket_arn is not a valid S3 Tables bucket ARN")
    out: Dict[str, Any] = {"table_bucket_arn": arn}
    endpoint = str(cfg.get("catalog_endpoint") or "s3_tables").strip().lower()
    if endpoint not in _CATALOG_ENDPOINTS:
        raise DuckDBConfigError(
            "connection_config.catalog_endpoint must be one of: " + ", ".join(_CATALOG_ENDPOINTS))
    out["catalog_endpoint"] = endpoint
    role_arn = str(cfg.get("role_arn") or "").strip()
    if role_arn:
        if _ROLE_ARN_RE.match(role_arn) is None:
            raise DuckDBConfigError("connection_config.role_arn is not a valid IAM role ARN")
        out["role_arn"] = role_arn
        external_id = str(cfg.get("external_id") or "").strip()
        if external_id:
            out["external_id"] = external_id  # a secret: never logged or put in SQL
    if cfg.get("namespaces"):
        out["namespaces"] = [_valid_identifier(v, "namespaces") for v in cfg["namespaces"]]
    if cfg.get("tables"):
        out["tables"] = [_valid_scope_entry(v) for v in cfg["tables"]]
    if cfg.get("duckdb_memory_limit"):
        out["duckdb_memory_limit"] = _valid_memory_limit(cfg["duckdb_memory_limit"])
    if cfg.get("duckdb_threads") is not None:
        out["duckdb_threads"] = _bounded_int(cfg["duckdb_threads"], "duckdb_threads",
                                             low=1, high=_MAX_DUCKDB_THREADS)
    if cfg.get("query_timeout_s") is not None:
        out["query_timeout_s"] = _bounded_float(cfg["query_timeout_s"], "query_timeout_s",
                                                low=0.1, high=_MAX_QUERY_TIMEOUT_S)
    # Row cap: default to sql_safety.MAX_ROWS when unset; otherwise a bounded,
    # positive int capped at the hard product ceiling. 0/negatives fail closed
    # (the old `value or MAX_ROWS` silently turned 0 into the default).
    if cfg.get("max_rows") is None:
        out["max_rows"] = MAX_ROWS
    else:
        # Must be a positive int (0/neg/bool/float fail closed); then CLAMP to the
        # hard ceiling so an operator cannot remove the practical bound.
        requested = _bounded_int(cfg["max_rows"], "max_rows", low=1, high=_INT_MAX)
        out["max_rows"] = min(requested, _MAX_ROWS_CEILING)
    return out


# The four DuckDB extensions the S3 Tables read needs, loaded in order (avro
# before iceberg: iceberg reads Avro manifest files). Baked into the runtime
# image (Dockerfile.runtime S3T_DUCKDB_EXTENSION_DIR); never installed at runtime.
_DUCKDB_EXTENSIONS = ("aws", "httpfs", "avro", "iceberg")
# DuckDB cannot bind an extension name or a catalog alias, so each setup
# statement is a fixed literal per allowed value (pinned equal to the
# composed form by tests/unit/test_duckdb_fixed_statements.py).
_LOAD_SQL = {
    "aws": "LOAD aws",
    "httpfs": "LOAD httpfs",
    "avro": "LOAD avro",
    "iceberg": "LOAD iceberg",
}
_ATTACH_SQL = {
    "s3_tables": "ATTACH ? AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables, READ_ONLY)",
    "glue": "ATTACH ? AS s3t (TYPE iceberg, ENDPOINT_TYPE glue, READ_ONLY)",
}


def _session_for(cfg, default_region=None, service="adt"):
    """Indirection over :func:`agents.aws_session.session_for` (test seam)."""
    from .aws_session import session_for
    return session_for(cfg, default_region=default_region, service=service)


def _attach_target(cfg: Dict[str, Any]):
    """``(attach_target, endpoint_type)`` for the configured catalog endpoint.

    ``s3_tables`` attaches the bucket ARN itself. ``glue`` attaches the bucket's
    federated Glue catalog, ``<account>:s3tablescatalog/<bucket>``, which is how
    Lake Formation exposes an S3 Tables bucket (including one in another account
    that has been shared through Lake Formation)."""
    arn = cfg["table_bucket_arn"]
    endpoint = cfg.get("catalog_endpoint") or "s3_tables"
    if endpoint == "glue":
        match = _BUCKET_ARN_RE.match(arn)
        return f"{match.group('account')}:s3tablescatalog/{match.group('name')}", "glue"
    return arn, "s3_tables"


def _secret_statement(cfg: Dict[str, Any]):
    """``(sql, params, expiry)`` for the DuckDB S3 secret.

    No ``role_arn``: the ambient credential chain (the task role), no expiry to
    track. With ``role_arn``: assume it through the shared ``aws_session`` helper
    (the same identity the Athena connector uses for a cross-account source) and
    pass the temporary keys as BOUND PARAMETERS, so key material never appears in
    SQL text or logs. ``expiry`` lets the reader reconnect before they lapse."""
    region = _BUCKET_ARN_RE.match(cfg["table_bucket_arn"]).group("region")
    if not cfg.get("role_arn"):
        return ("CREATE SECRET s3t_secret (TYPE s3, PROVIDER credential_chain, REGION ?)",
                [region], None)
    session = _session_for({"role_arn": cfg["role_arn"],
                            "external_id": cfg.get("external_id", ""),
                            "region": region},
                           default_region=region, service="s3tables")
    creds = session.get_credentials()
    frozen = creds.get_frozen_credentials()
    expiry = getattr(creds, "_expiry_time", None)
    return ("CREATE SECRET s3t_secret (TYPE s3, KEY_ID ?, SECRET ?, SESSION_TOKEN ?, REGION ?)",
            [frozen.access_key, frozen.secret_key, frozen.token, region], expiry)


class QueryError(RuntimeError):
    """A read failed. ``safe_message`` is client-safe; detail is logged only."""

    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class RowLimitExceeded(QueryError):
    def __init__(self, limit: int):
        super().__init__("row_limit",
                         f"Result exceeds {limit} rows; narrow the query or request fewer rows")
        self.limit = limit


class CatalogMissingRelation(QueryError):
    """A catalog read failed because the referenced TABLE or SCHEMA/namespace
    does not exist (a DuckDB CatalogException), as opposed to a genuine read
    outage (I/O, credentials, timeout). Distinct so the guard can turn it into a
    NON-retryable ``unknown_table`` refusal instead of a retryable
    ``catalog_facts_unavailable`` one -- retrying the same nonexistent name can
    never succeed. Carries no path or ARN."""

    def __init__(self):
        super().__init__("unknown_table", "the referenced table or namespace does not exist")


#: A DuckDB catalog "does not exist" fault for a table, schema/namespace or
#: catalog. Matches the CatalogException family only; a Binder/IO/credentials
#: fault does not match, so a genuine read outage stays retryable.
_MISSING_RELATION_RE = re.compile(
    r"catalog error.*?\b(?:table|schema|catalog)\b.*?does not exist", re.IGNORECASE | re.DOTALL)


def _is_missing_relation_error(text: str) -> bool:
    """True when ``text`` is a catalog "table/schema does not exist" error. Reads
    only the error CATEGORY, never echoes any identifier, path or ARN."""
    t = " ".join(str(text or "").split())
    if _MISSING_RELATION_RE.search(t):
        return True
    # DuckDB also phrases a bare table miss as "Table with name X does not exist"
    # without the "Catalog Error:" prefix; _TABLE_NOT_FOUND_RE recognises it.
    return _TABLE_NOT_FOUND_RE.search(t) is not None


# A safe SQL identifier the engine quoted back from the model's OWN SQL: only
# letters, digits, underscore and a single dot (a qualified name). Anything with
# a slash, colon, scheme, space, or other punctuation is NOT an identifier (it
# could be a path, URL, ARN or secret) and is never surfaced.
# One character per repeat (a dot only before a name start), so no repeat can
# split an input two ways; tests/unit/test_regex_rewrites_match_the_old_patterns.py
# pins it equal to the nested form it replaced.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_](?:[A-Za-z0-9_]|\.(?=[A-Za-z_]))*$")

#: Allowlisted DuckDB SQL-fault structures. Each pattern captures ONE group:
#: the identifier or token the model itself submitted. Only a capture that is a
#: safe SQL identifier (or, for the syntax token, a short word) is surfaced,
#: after redaction. No pattern matches a path, URL, ARN, account id or secret.
_COLUMN_NOT_FOUND_RE = re.compile(
    r'referenced column\s+"?([^"\s]+)"?\s+not found', re.IGNORECASE)
_BINDER_COLUMN_RE = re.compile(
    r'(?:column|binder error:)\s+"?([^"\s]+)"?\s+(?:does not exist|not found|is ambiguous)',
    re.IGNORECASE)
_TABLE_NOT_FOUND_RE = re.compile(
    r'table (?:with name )?"?([^"\s]+)"?\s+does not exist', re.IGNORECASE)
_SYNTAX_TOKEN_RE = re.compile(
    r'syntax error at or near\s+"?([^"\s]+)"?', re.IGNORECASE)
#: DuckDB raises this when a DOTTED physical column is referenced unquoted: the
#: dotted prefix parses as a table/struct reference, e.g. a query on column
#: ``a.b.c`` reports ``Referenced table "a.b" not found``. The capture is the
#: dotted prefix the model itself wrote.
_BINDER_TABLE_NOT_FOUND_RE = re.compile(
    r'referenced table\s+"?([^"\s]+)"?\s+not found', re.IGNORECASE)


def _safe_capture(match, *, token: bool = False) -> "str | None":
    """The capture of ``match`` when it is a safe SQL identifier (or, for a
    syntax ``token``, a short bare word), after secret/digit redaction; else
    ``None``. Never returns a path, URL, ARN, account id or secret."""
    if match is None:
        return None
    from .chat_client import _redact_digits
    value = str(_redact_digits(match.group(1), limit=80)).strip().strip('"')
    if not value:
        return None
    if token:
        return value if re.fullmatch(r"[A-Za-z0-9_#]{1,40}", value) else None
    return value if _SAFE_IDENTIFIER_RE.match(value) else None


def safe_relation_name(name: str) -> "str | None":
    """A dotted relation name (``catalog.namespace.table``) when EVERY part is a
    safe SQL identifier, else ``None``. The allowlist that keeps a quoted
    identifier carrying path/ARN/URL characters (``:``, ``/``, spaces) out of a
    public message: a single unsafe part withholds the whole name rather than
    echoing it. Used by the unknown_table refusal so it never leaks what the
    model quoted into a relation identifier."""
    text = str(name or "").strip()
    if not text:
        return None
    parts = [p.strip().strip('"') for p in text.split(".")]
    if not parts or not all(p and _SAFE_IDENTIFIER_RE.match(p) for p in parts):
        return None
    return ".".join(parts)


def sanitize_engine_error(detail: str) -> "str | None":
    """A client-safe one-line reason for a DuckDB rejection, built from an
    ALLOWLIST of known SQL-fault structures (review F5), else ``None`` (the
    caller then keeps the fixed opaque message).

    Only the identifier or token the MODEL ITSELF submitted is surfaced, and
    only when it is a safe SQL identifier (a bare/qualified name) or a short
    syntax token, after secret and digit redaction. Any other message, and any
    capture that looks like a path, URL, ARN, account id, bucket/catalog
    identifier or secret, yields ``None``. The engine's raw text is never
    returned, so a ``Binder Error: Cannot open file /root/.aws/config`` or a
    ``Catalog Error: Catalog <name> does not exist`` surfaces nothing."""
    text = " ".join(str(detail or "").split())
    if not text:
        return None
    column = _safe_capture(_COLUMN_NOT_FOUND_RE.search(text)) \
        or _safe_capture(_BINDER_COLUMN_RE.search(text))
    if column:
        return f"column {column} not found or not valid; check the column name against the schema"
    # A "Referenced table X not found" whose X is a DOTTED safe identifier is the
    # dotted-column-referenced-unquoted mistake (the live S3 Tables failure): the
    # dots are part of ONE physical column name. Mirror Athena's dotted_name_split
    # diagnostic -- tell the agent to double-quote the whole dotted name. Only a
    # dotted capture takes this path; a bare name falls through to the plain
    # table-not-found hint. Nothing unsafe is echoed (safe-identifier allowlist).
    binder_table = _safe_capture(_BINDER_TABLE_NOT_FOUND_RE.search(text))
    if binder_table and "." in binder_table:
        return (f"{binder_table} is the start of a dotted column name, not a table; a column "
                f'whose name contains dots is one identifier -- reference it double-quoted as '
                f'"{binder_table}.<rest>" (the whole name in one pair of quotes), '
                "never as separate parts")
    table = _safe_capture(_TABLE_NOT_FOUND_RE.search(text)) or binder_table
    if table:
        return f"table {table} not found; list the tables and check the name"
    token = _safe_capture(_SYNTAX_TOKEN_RE.search(text), token=True)
    if token:
        return f"syntax error near {token}; check the SQL syntax"
    low = text.lower()
    # Invalid Input and Conversion errors carry the offending VALUE (a literal,
    # a column value, a format specifier), which is data and must not echo. Give
    # a FIXED, detail-free category so the agent can still act.
    if low.startswith("invalid input error"):
        return "invalid query input; check function arguments and parameter use"
    if low.startswith("conversion error"):
        return "value conversion failed; check the literal and column types in the query"
    return None


class _S3TablesReader:
    """Lazily-started, thread-safe reader over ONE S3 Tables bucket using the
    OFFICIAL ``duckdb`` package (ADR-0099). No external engine dependency: ADT
    issues the ATTACH/secret itself from DuckDB's own documentation.

    Opens one in-process DuckDB, loads the baked extensions, creates an S3 secret
    from the task role's credential chain, and ATTACHes the S3 Tables Iceberg REST
    catalog READ_ONLY as the ``s3t`` catalog. Opening ATTACHes/scans S3, so it must
    not run at tool-build time or on the event loop — it starts on first use and is
    reused (warm reads). ``connection_factory`` injects an already-attached DuckDB
    for tests (no AWS).
    """

    def __init__(self, cfg: Dict[str, Any], connection_factory=None):
        self._cfg = cfg
        self._factory = connection_factory
        self._con = None
        self._expiry = None  # assumed-role credential expiry (None = ambient chain)
        self._lock = threading.Lock()
        # Readers currently holding each connection generation (by id of the
        # connection). A retired generation is closed only when this is zero.
        self._users: Dict[int, int] = {}
        self._retired: Dict[int, Any] = {}

    def _connect(self):
        """Open and fully set up one connection; return ``(connection, expiry)``.

        Nothing on ``self`` changes here. The caller adopts both values together
        under ``_lock``, so a failure part way (secret, ATTACH) leaves the
        current connection and its expiry exactly as they were. The partly set
        up connection is closed before the error propagates."""
        import duckdb

        if self._factory is not None:
            con = self._factory()
            try:
                con.execute("SET TimeZone='UTC'")
            except Exception:
                self._close_quietly(con)
                raise
            return con, None
        config = {
            "memory_limit": self._cfg.get("duckdb_memory_limit", "2GB"),
            "threads": self._cfg.get("duckdb_threads", 2),
            # Never download extension code at runtime; the image bakes them in.
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
        ext_dir = os.environ.get("S3T_DUCKDB_EXTENSION_DIR")
        if ext_dir:
            config["extension_directory"] = ext_dir
        con = duckdb.connect(config=config)
        try:
            return con, self._set_up(con)
        except Exception:
            self._close_quietly(con)
            raise

    def _set_up(self, con):
        """Load the extensions, create the secret and ATTACH the catalog on
        ``con``; return the credential expiry (None for the ambient chain)."""
        for ext in _DUCKDB_EXTENSIONS:
            con.execute(_LOAD_SQL[ext])
        con.execute("SET TimeZone='UTC'")
        # Official DuckDB S3 Tables attach, READ_ONLY. Credentials are the task
        # role's ambient chain, or a cross-account role's temporary keys passed as
        # bound parameters; either way no key material appears in SQL text.
        secret_sql, secret_params, expiry = _secret_statement(self._cfg)
        con.execute(secret_sql, secret_params)
        target, endpoint = _attach_target(self._cfg)
        # endpoint comes from the closed _CATALOG_ENDPOINTS allowlist.
        con.execute(_ATTACH_SQL[endpoint], [target])
        return expiry

    def _credentials_stale(self) -> bool:
        if self._expiry is None:
            return False
        import datetime as _dt
        remaining = (self._expiry - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        return remaining < _CREDENTIAL_REFRESH_MARGIN_S

    def _acquire(self):
        """Lease the current connection generation, refreshing it first when its
        credentials are stale. The lease keeps the generation open: a refresh
        retires the old one, which is closed when its last reader releases."""
        with self._lock:
            if self._con is None or self._credentials_stale():
                # On failure _connect has closed its candidate and raises here,
                # before either field changes: the old connection and its expiry
                # stay, so the next acquire still sees them stale and retries.
                new_con, new_expiry = self._connect()
                old, self._con, self._expiry = self._con, new_con, new_expiry
                if old is not None:
                    self._retire_locked(old)
            con = self._con
            self._users[id(con)] = self._users.get(id(con), 0) + 1
            return con

    def _connection(self):
        """The current connection, refreshed if stale, WITHOUT holding a lease.
        For probes and tests only: a query must use :meth:`_acquire` so a refresh
        cannot close the connection under it."""
        con = self._acquire()
        self._release(con)
        return con

    def _release(self, con) -> None:
        with self._lock:
            left = self._users.get(id(con), 0) - 1
            if left > 0:
                self._users[id(con)] = left
                return
            self._users.pop(id(con), None)
            retired = self._retired.pop(id(con), None)
        if retired is not None:
            self._close_quietly(retired)

    def _retire_locked(self, con) -> None:
        """Close ``con`` now if idle, else when its last reader releases (lock held)."""
        if self._users.get(id(con), 0) > 0:
            self._retired[id(con)] = con
        else:
            self._close_quietly(con)

    @staticmethod
    def _close_quietly(con) -> None:
        try:
            con.close()
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.debug("DuckDB connection close failed (%s)", type(exc).__name__)

    def close(self) -> None:
        """Retire the current connection; it closes once no query is using it.
        A later read reconnects."""
        with self._lock:
            con, self._con = self._con, None
            if con is not None:
                self._retire_locked(con)

    def list_tables(self):
        rows = self.fetch(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = ? ORDER BY 1, 2", [CATALOG], max_rows=100_000)
        return [(r[0], r[1]) for r in rows]

    def columns(self, namespace: str, table: str):
        # DESCRIBE loads the Iceberg schema; identifiers come from catalog metadata
        # and are quoted. Returns ColumnInfo-like (name, upper-case type) tuples.
        ref = f'{CATALOG}."{namespace.replace(chr(34), chr(34) * 2)}"."{table.replace(chr(34), chr(34) * 2)}"'
        rows = self.fetch(f"DESCRIBE {ref}", [], max_rows=100_000)  # nosec B608 - identifiers quoted from metadata
        return [type("ColumnInfo", (), {"name": r[0], "type": str(r[1]).upper()})() for r in rows]

    def _query_time_limit(self) -> float:
        """Seconds one query may run: ``query_timeout_s`` from the connection
        config (already bounds-checked), else :data:`_DEFAULT_QUERY_TIMEOUT_S`,
        and never more than :data:`_GATEWAY_SAFE_QUERY_TIMEOUT_S`."""
        configured = float(self._cfg.get("query_timeout_s") or _DEFAULT_QUERY_TIMEOUT_S)
        return min(configured, _GATEWAY_SAFE_QUERY_TIMEOUT_S)

    def fetch(self, sql: str, params=(), max_rows: Optional[int] = None,
              *, data_query: bool = False):
        """Run one parameterized query on a per-call cursor, fetch up to
        ``max_rows + 1`` rows, and raise :class:`RowLimitExceeded` when the result
        exceeds ``max_rows`` (the caller's look-ahead overflow check). A query
        still running at :meth:`_query_time_limit` is interrupted and raises a
        time-limit :class:`QueryError`: narrowing guidance for a ``data_query``
        (the agent's own SQL), catalog guidance for a metadata read."""
        limit = self._cfg.get("max_rows", MAX_ROWS) if max_rows is None else max_rows
        con = self._acquire()
        try:
            cur = con.cursor()
            stopper = _QueryStopper(cur, self._query_time_limit())
            try:
                cur.execute(sql, list(params))
                rows = cur.fetchmany(limit + 1)
            except (RowLimitExceeded, QueryError):
                raise
            except Exception as exc:  # noqa: BLE001 - never leak engine text to the client
                if stopper.finish():
                    raise _time_limit_error(self._query_time_limit(), data_query) from None
                logger.error("duckdb query failed (%s)", type(exc).__name__)
                if _is_missing_relation_error(str(exc)):
                    # A nonexistent table/namespace: NON-retryable. Raised as a
                    # distinct type so the guard refuses unknown_table rather
                    # than reporting a retryable metadata outage.
                    raise CatalogMissingRelation() from None
                reason = sanitize_engine_error(str(exc))
                if reason is not None:
                    raise QueryError("invalid_query",
                                     f"S3 Tables rejected the query: {reason}") from None
                raise QueryError("query_failed", "Query against S3 Tables failed") from None
            finally:
                # Ends the timer before the cursor closes: an interrupt can
                # never reach a closed cursor. A fetch that completed wins over
                # a stop that raced it (its rows are whole).
                stopper.finish()
                try:
                    cur.close()
                except Exception as exc:  # noqa: BLE001 - best effort
                    logger.debug("DuckDB cursor close failed (%s)", type(exc).__name__)
        finally:
            self._release(con)
        if len(rows) > limit:
            raise RowLimitExceeded(limit)
        return rows


class _EngineHandle:
    """Lazily-started, thread-safe holder for one :class:`_S3TablesReader`.

    Opening ATTACHes/scans S3 (a cold read), so the reader starts on the first
    tool call and is reused thereafter (warm reads). ``connection_factory`` is the
    test seam (an already-attached in-memory DuckDB), passed straight through.
    """

    def __init__(self, cfg: Dict[str, Any], connection_factory=None):
        self._reader = _S3TablesReader(cfg, connection_factory=connection_factory)

    def query_layer(self):
        return self._reader

    def close(self) -> None:
        """Close the DuckDB connection. Used by the short-lived discovery path;
        the long-lived per-system agent keeps its reader open for warm reads."""
        self._reader.close()


def _tool_error(exc: Exception) -> dict:
    """Map a tool failure to a structured payload via the shared public_error.

    Our ``QueryError`` / ``RowLimitExceeded`` carry a client-safe message and a
    code; we surface those honestly (narrow-the-query guidance), and everything
    else goes through :func:`agents.public_errors.public_error` (fixed category +
    reference, detail logged, never raw text).
    """
    if isinstance(exc, QueryError):
        return {"error": exc.safe_message, "error_code": exc.code,
                "error_class": type(exc).__name__}
    return public_error(exc, "duckdb_tools")


def _in_scope(scope: "Scope", namespace: str, table: str) -> bool:
    """True when (namespace, table) is within scope. Empty scope = whole bucket."""
    return _scope_allows(scope, namespace, table)


def _run_list_tables(handle: _EngineHandle, scope: "Scope" = ()) -> str:
    try:
        rows = handle.query_layer().list_tables()
        # DDB-02: list only in-scope tables.
        return json.dumps({"tables": [f"{ns}.{tbl}" for ns, tbl in rows
                                      if _in_scope(scope, ns, tbl)]})
    except Exception as e:  # noqa: BLE001 - surface as tool error, not a crash
        return json.dumps(_tool_error(e))


def _run_describe_table(handle: _EngineHandle, table_name: str, scope: "Scope" = ()) -> str:
    try:
        namespace, _, table = str(table_name).partition(".")
        if not namespace or not table:
            raise DuckDBConfigError("table_name must be 'namespace.table'")
        if not _in_scope(scope, namespace, table):
            raise DuckDBAuthorizationError(
                f"Table {namespace}.{table} is outside this system's configured scope.")
        cols = handle.query_layer().columns(namespace, table)
        return json.dumps({"table": table_name,
                           "columns": [{"name": c.name, "type": c.type} for c in cols]})
    except Exception as e:  # noqa: BLE001
        return json.dumps(_tool_error(e))


def _scope_pairs(cfg: Dict[str, Any]) -> Scope:
    """The read scope from connection_config ``namespaces`` and ``tables``.

    Entries of ``tables`` must be ``namespace.table``. The two settings are
    independent: with both empty the scope is the whole bucket. A malformed
    table entry is dropped, and when every entry is malformed the scope denies
    everything, so a bad entry never widens to the whole bucket."""
    pairs = []
    entries = cfg.get("tables", []) or []
    for entry in entries:
        ns, _, tbl = str(entry).partition(".")
        if ns and tbl:
            pairs.append((ns, tbl))
    namespaces = [str(n) for n in cfg.get("namespaces", []) or []]
    return ScopeSpec(pairs, namespaces, deny_all=bool(entries) and not pairs)


def _is_select_or_with(sql: str) -> bool:
    """Whether the statement's first significant keyword is ``SELECT`` / ``WITH``
    (a projection query), as opposed to ``SHOW`` / ``DESCRIBE`` / ``DESC``. The
    SQL LIMIT rewrite and the ``DESCRIBE (<sql>)`` column probe apply only to
    these. ``ensure_select_only`` has already confirmed the
    statement is one of the read-only prefixes."""
    text = (sql or "").lstrip().lstrip("(").lstrip()
    word = ""
    for ch in text:
        if ch.isalpha():
            word += ch
        else:
            break
    return word.lower() in ("select", "with")


def _query_columns(ql, sql: str) -> list:
    """Column names of a SELECT, via a metadata-only DESCRIBE (no data scan).
    Best-effort: on any failure the result still ships with positional rows. A
    SHOW / DESCRIBE statement is not wrappable in ``DESCRIBE (...)``, so its
    columns come from the result rows instead."""
    if not _is_select_or_with(sql):
        return []
    try:
        return [r[0] for r in ql.fetch(f"DESCRIBE ({sql})", [], max_rows=10_000)]
    except Exception:  # noqa: BLE001 - columns are informational; never fail the read
        return []


def _run_query(handle: _EngineHandle, sql: str, max_rows: int, scope: Scope = (),
               source_system: str = "") -> str:
    """Run one read-only query and return the Athena-contract result shape
    (columns, bounded rows, explicit truncation state, source metadata, and the
    actually-executed SQL), so response-integrity enforce mode and provenance see
    the same shape the SQL connectors produce (DDB-03 + DDB-05)."""
    from .response_integrity_capture import attach_source_metadata
    from .response_integrity_query import bounded_query_result
    try:
        authorize_duckdb_sql(sql, scope)  # DDB-01/02: fail closed before anything runs
        safe = ensure_select_only(sql)
        # DDB-03: cap at max_rows + 1 so the extra look-ahead row proves overflow.
        # The SQL LIMIT rewrite is only valid on SELECT / WITH; DuckDB rejects
        # `SHOW ... LIMIT` and `DESCRIBE ... LIMIT`, so those run as written and
        # rely on the fetch max_rows + 1 look-ahead and the same Python-side
        # truncation.
        if _is_select_or_with(safe):
            capped, _ = apply_row_cap(safe, max_rows + 1)
        else:
            capped = safe
        ql = handle.query_layer()
        rows = [list(r) for r in ql.fetch(capped, [], max_rows=max_rows + 1, data_query=True)]
        truncated = len(rows) > max_rows           # the look-ahead row is present
        rows = rows[:max_rows]                      # never return more than the cap
        columns = _query_columns(ql, safe)
        result = bounded_query_result(columns, rows, truncated, "row_limit" if truncated else "")
        result["_adt_executed_query"] = capped      # the SQL actually run, after capping
        # DuckDB is a SQL source; the generic "sql" boundary is the integrity
        # boundary for its results (same family as PostgreSQL's SQL path).
        result = attach_source_metadata(result, boundary="sql", system_id=source_system)
        return json.dumps(result, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps(_tool_error(e))


def discover_schema(connection_config: Optional[dict], source_system: str = "",
                    connection_factory=None, max_tables: int = 25) -> dict:
    """Introspect the S3 Tables bucket (metadata only, no data scan, no LLM) so
    discover -> map sees the real tables and columns.

    Returns the same contract shape as ``onboard_agent._discover_athena_schema``:
    ``{"system_id", "schema_name", "tables": [{"name", "columns": [...]}],
    "ai_analysis", "connection_type"}``, or ``{"error": str}`` on a bad config or
    a discovery failure. Dataset-neutral: it lists whatever namespaces/tables the
    bucket holds and describes each table's own columns; no SFC name is assumed.
    """
    try:
        cfg = _resolve_config(connection_config)
    except DuckDBConfigError as e:
        return {"error": str(e)}  # SAFE_MESSAGE_TYPE: quotes back only the caller's input
    handle = _EngineHandle(cfg, connection_factory=connection_factory)
    scope = _scope_pairs(cfg)
    try:
        ql = handle.query_layer()
        # DDB-02: discovery lists ONLY in-scope tables (empty scope = all).
        in_scope = [(ns, tbl) for ns, tbl in ql.list_tables()
                    if _scope_allows(scope, ns, tbl)]
        tables = []
        for namespace, table in in_scope[:max_tables]:
            cols = ql.columns(namespace, table)
            tables.append({"name": f"{namespace}.{table}",
                           "columns": [{"name": c.name, "data_type": c.type} for c in cols]})
        truncated = len(in_scope) > max_tables
    except Exception as e:  # noqa: BLE001 - surface discovery failure to the caller
        return {"error": "S3 Tables discovery failed: "
                + public_error_text(e, "discover.duckdb", default="upstream_error")}
    finally:
        handle.close()  # DDB-09: don't leak the discovery connection/threadpool
    partial = " (list truncated; narrow namespaces/tables)" if truncated else ""
    ai_analysis = (
        f"S3 Tables connector over bucket {cfg['table_bucket_arn']}. "
        f"Discovered {len(tables)} table(s) via the Iceberg catalog{partial}. Query with "
        "s3tables_run_query (read-only DuckDB SQL); reference tables as "
        "s3t.<namespace>.<table>. Bound the time column the question asks about."
    )
    return {"system_id": source_system, "schema_name": CATALOG, "tables": tables,
            "ai_analysis": ai_analysis, "connection_type": "S3 Tables",
            "partial": truncated}


class PartitionSpecUnavailable(RuntimeError):
    """The current Iceberg partition spec of a table could not be established
    (the metadata read failed, or the table has no data manifest entry to read
    it from). Distinct from a table that is confirmed UNPARTITIONED (``[]``):
    provisioning must fail closed on this, not bake an empty storage rule
    """


def s3tables_partition_fields(table_name: str, connection_config: Optional[dict],
                              connection_factory=None) -> list:
    """The Iceberg partition field names of one S3 Tables table (metadata only,
    no data scan). TRI-STATE:

    * a non-empty list -- the known partition fields;
    * ``[]`` -- the table is CONFIRMED unpartitioned (a data file path with no
      ``field=value`` segment was read);
    * raises :class:`PartitionSpecUnavailable` -- the spec could not be
      established (the metadata read failed, or there is no data manifest entry
      to read it from). The caller fails provisioning closed on this, rather
      than treat unreadable metadata as "unpartitioned".

    ``table_name`` is the discovered ``namespace.table`` spelling; the field
    names are returned lower-cased, in catalog order, de-duplicated."""
    namespace, _, table = str(table_name).partition(".")
    if not namespace or not table:
        raise PartitionSpecUnavailable(f"not a namespace.table name: {table_name!r}")
    cfg = _resolve_config(connection_config)
    handle = _EngineHandle(cfg, connection_factory=connection_factory)
    try:
        return _read_partition_fields(handle.query_layer(), namespace, table)
    except PartitionSpecUnavailable:
        raise
    except Exception as exc:  # the metadata read failed -> unavailable, not []
        logger.info("duckdb partition-spec read failed (%s)", type(exc).__name__)
        raise PartitionSpecUnavailable(
            f"partition metadata unreadable ({type(exc).__name__})") from None
    finally:
        # Close the attached-catalog, credential-bearing connection on every
        # path (success and exception) so a wide catalog does not retain one per
        # table until garbage collection (review F7).
        handle.close()


_MAX_PARTITION_FIELDS = 16
#: The Iceberg data root segment: partition directories sit BETWEEN it and the
#: data file, so only segments after the LAST ``/data/`` are partition segments.
#: A ``field=value`` segment anywhere in the warehouse PREFIX (e.g.
#: ``.../tenant=corp/data/...``) is not a partition and must be ignored.
_DATA_ROOT = "/data/"


def _read_partition_fields(reader, namespace: str, table: str) -> list:
    """Partition field names for one table, read from the NEWEST data manifest
    entry's ``file_path`` via ``iceberg_metadata`` (one row, metadata-only, no
    data scan), else ``[]``.

    DuckDB 2.0's ``iceberg_metadata(ref)`` lists manifest ENTRIES (columns
    manifest_path, content ('DATA'), file_path, data_sequence_number,
    file_sequence_number, ...); it exposes no partition-spec rows. A partitioned
    Iceberg table writes Hive-style ``field=value`` path segments under the data
    root, so the field NAMES of the newest data file are the current partition
    spec, in spec order. Ordering by the sequence numbers DESC makes the choice
    deterministic across partition-spec evolution (``LIMIT 1`` alone could bake
    an old spec). Returned as written (transform suffix kept, e.g.
    ``created_day``), lower-cased and de-duplicated, to match the shape
    ``athena_tools.hidden_partition_fields`` returns from ``$partitions`` so
    ``storage_bound_rule.field_sources`` maps them to source columns identically
    on either engine. ``[]`` only for an unpartitioned table; a read that cannot
    establish the spec raises so the caller fails closed (review F6)."""
    ref = (f"{CATALOG}.\"{namespace.replace(chr(34), chr(34) * 2)}\""
           f".\"{table.replace(chr(34), chr(34) * 2)}\"")
    rows = reader.fetch(
        "SELECT file_path FROM iceberg_metadata(?) "  # nosec B608 - bound parameter
        "WHERE upper(COALESCE(content, '')) = 'DATA' AND file_path IS NOT NULL "
        "ORDER BY data_sequence_number DESC NULLS LAST, "
        "file_sequence_number DESC NULLS LAST LIMIT 1",
        [ref], max_rows=1)
    if not rows or not rows[0] or not str(rows[0][0] or "").strip():
        # No data manifest entry (or no file_path): the current spec cannot be
        # established from a path, so this is UNAVAILABLE, not unpartitioned.
        raise PartitionSpecUnavailable("no data manifest entry to read the spec from")
    return _partition_fields_from_path(str(rows[0][0] or ""))


def _partition_fields_from_path(file_path: str) -> list:
    """The Hive partition field names of ONE data file path, in spec order,
    lower-cased and de-duplicated; ``[]`` for a path with no partition segment
    (an unpartitioned table).

    Only the segments AFTER the last Iceberg data root (``/data/``) and BEFORE
    the file name are read, so a ``field=value`` segment in the warehouse
    prefix is ignored. Each segment is split on the FIRST ``=`` (a value may
    contain ``=``) and the field name is URL-decoded (Iceberg percent-encodes
    non-identifier names)."""
    from urllib.parse import unquote
    text = str(file_path or "")
    marker = text.rfind(_DATA_ROOT)
    if marker < 0:
        return []
    after = text[marker + len(_DATA_ROOT):]
    segments = after.split("/")[:-1]  # drop the data file name itself
    out: list = []
    for segment in segments:
        if "=" not in segment:
            continue
        field = unquote(segment.split("=", 1)[0]).strip().lower()
        if field and field not in out:
            out.append(field)
    return out[:_MAX_PARTITION_FIELDS]


def make_duckdb_tools(connection_config: Optional[dict], source_system: str = "",
                      connection_factory=None):
    """Build three validated DuckDB/S3-Tables tools for one system configuration.

    Returns ``[s3tables_list_tables, s3tables_describe_table, s3tables_run_query]``,
    the same three-tool shape as :func:`athena_tools.make_athena_tools`. Config is
    validated eagerly here so a misconfigured system fails at build time.
    ``connection_factory`` is the engine's test seam; production callers omit it.
    """
    from strands import tool

    cfg = _resolve_config(connection_config)  # eager validation
    handle = _EngineHandle(cfg, connection_factory=connection_factory)
    max_rows = cfg["max_rows"]
    scope = _scope_pairs(cfg)  # DDB-02: the (namespace, table) read allowlist

    @tool(name="s3tables_list_tables")
    def s3tables_list_tables() -> str:
        """List tables in this S3 Tables bucket (namespace.table). Call this first to discover tables."""
        return _run_list_tables(handle, scope)

    @tool(name="s3tables_describe_table")
    def s3tables_describe_table(table_name: str) -> str:
        """Get column names and data types for one table.
        Args:
            table_name: ``namespace.table`` (from s3tables_list_tables).
        """
        return _run_describe_table(handle, table_name, scope)

    @tool(name="s3tables_run_query")
    def s3tables_run_query(sql: str) -> str:
        """Run a read-only SQL SELECT against this S3 Tables system (DuckDB dialect).
        Only SELECT/WITH/SHOW/DESCRIBE statements are allowed; results are row-capped.
        Reference tables as ``s3t.<namespace>.<table>`` (from s3tables_list_tables).
        Bound the time column the question asks about to keep the read small.
        Args:
            sql: A single read-only SQL SELECT statement.
        """
        return _run_query(handle, sql, max_rows, scope, source_system)

    return [s3tables_list_tables, s3tables_describe_table, s3tables_run_query]


# Warm engines shared by every caller of :func:`run_select` (the metric layer
# reads many aggregates per card refresh; a cold ATTACH per read would dominate).
# A bounded LRU keyed by a hash of the validated config (the external id is part
# of the hash and never stored in clear) plus the test factory's identity, so
# two systems on different roles or scopes never share a reader. An evicted
# engine is closed once the last read using it finishes; all are closed at exit.
_SHARED_HANDLES_MAX = 16
_SHARED_HANDLES: "OrderedDict[Any, _SharedEntry]" = OrderedDict()
_SHARED_LOCK = threading.Lock()
_SHUTDOWN_REGISTERED = False


class _SharedEntry:
    """One cached engine plus the reads currently using it."""

    def __init__(self, handle: "_EngineHandle"):
        self.handle = handle
        self.users = 0
        self.evicted = False


def _config_digest(cfg: Dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()


def _lease_handle(cfg: Dict[str, Any], connection_factory=None) -> "_SharedEntry":
    """The cached engine for ``cfg`` (built on first use), leased to the caller.
    Return it with :func:`_release_handle`."""
    global _SHUTDOWN_REGISTERED
    key = (_config_digest(cfg), id(connection_factory) if connection_factory else 0)
    to_close: list = []
    with _SHARED_LOCK:
        if not _SHUTDOWN_REGISTERED:
            import atexit
            atexit.register(_close_shared_handles)
            _SHUTDOWN_REGISTERED = True
        entry = _SHARED_HANDLES.get(key)
        if entry is None:
            entry = _SHARED_HANDLES[key] = _SharedEntry(
                _EngineHandle(cfg, connection_factory=connection_factory))
        _SHARED_HANDLES.move_to_end(key)
        entry.users += 1
        while len(_SHARED_HANDLES) > _SHARED_HANDLES_MAX:
            _, old = _SHARED_HANDLES.popitem(last=False)
            old.evicted = True
            if old.users == 0:
                to_close.append(old.handle)
    for handle in to_close:
        handle.close()
    return entry


def _release_handle(entry: "_SharedEntry") -> None:
    with _SHARED_LOCK:
        entry.users -= 1
        close_now = entry.evicted and entry.users == 0
    if close_now:
        entry.handle.close()


def _close_shared_handles() -> None:
    """Close and forget every cached engine (shutdown, and test isolation)."""
    with _SHARED_LOCK:
        entries = list(_SHARED_HANDLES.values())
        _SHARED_HANDLES.clear()
        for entry in entries:
            entry.evicted = True
    for entry in entries:
        try:
            entry.handle.close()
        except Exception:  # pragma: no cover - best effort at shutdown
            logger.debug("closing a shared S3 Tables engine failed", exc_info=True)


def run_select(connection_config: Optional[dict], sql: str, source_system: str = "",
               connection_factory=None) -> dict:
    """Run ONE read-only SELECT against an S3 Tables system and return the
    parsed result (``columns``, ``rows``, ...).

    The public read helper for callers that are not the chat tool (the metric
    layer). It applies exactly the guards the tool applies (authorization,
    select-only, row cap, the system's namespace and table scope) on a warm
    engine shared per connection config. Raises :class:`DuckDBConfigError` for
    a bad config and :class:`QueryError` (a client-safe message) for a refused
    or failed statement.
    """
    cfg = _resolve_config(connection_config)
    entry = _lease_handle(cfg, connection_factory)
    try:
        raw = _run_query(entry.handle, sql, cfg["max_rows"], _scope_pairs(cfg),
                         source_system)
    finally:
        _release_handle(entry)
    parsed = json.loads(raw)
    if "error" in parsed:
        raise QueryError(str(parsed.get("error_code") or "query_failed"),
                         str(parsed["error"]))
    return parsed
