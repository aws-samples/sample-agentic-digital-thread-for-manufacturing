# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic Amazon Athena connector tools for a system agent.

A GENERIC connection type — nothing here is specific to any one dataset. An
Athena system is described entirely by its ``connection_config``:

    {
      "workgroup":       "<athena workgroup>",        # required
      "database":        "<glue database / schema>",  # required
      "catalog":         "AwsDataCatalog",             # optional (default)
      "output_location": "s3://.../",                  # optional (else workgroup default)
      "region":          "us-east-1",                   # optional (else AWS_REGION)
      "role_arn":        "arn:aws:iam::<acct>:role/..",  # optional (cross-account)
      "external_id":     "<sts external id>"             # optional (with role_arn)
    }

Because Athena reads through the Glue Data Catalog, the SAME connector queries
native Glue tables, S3 data, and cross-account federated sources (e.g. S3 Tables
/ Iceberg shared via Lake Formation) with no code differences — the cross-account
resolution happens at the catalog/Lake-Formation layer, transparent to this tool.

Credentials use the ambient boto3 chain (task role in cloud, the developer's
profile locally) — identical to the SiteWise (Direct) connector. When the
OPTIONAL ``role_arn`` (and, when required, ``external_id``) are supplied, the
shared :mod:`agents.aws_session` helper STS AssumeRoles into that role instead;
blank => ambient credentials, unchanged.

Read-only: every statement passes :func:`sql_safety.ensure_select_only` and
:func:`sql_safety.apply_row_cap`, and fetched rows are additionally bounded.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit

import boto3  # noqa: F401 - kept for callers/tests that patch athena_tools.boto3

from .aws_session import boto3_client_for
from .response_integrity import attach_source_metadata, utc_now
from .sql_safety import MAX_ROWS, apply_row_cap, ensure_select_only

logger = logging.getLogger(__name__)

# Terminal Athena query states.
_DONE = {"SUCCEEDED", "FAILED", "CANCELLED"}
_DEFAULT_POLL_TIMEOUT = 60.0
_DEFAULT_POLL_INTERVAL = 0.5

# S3 bucket-name rules (the common subset Athena/S3 accept): 3-63 chars, lowercase
# alphanumerics plus dot/hyphen, must start and end with an alphanumeric.
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

# --- Actionable-error enrichment (turn a guessed-name dead-end into a hint) --
# When a query FAILS with a guessable-name error, we do a bounded, best-effort
# information_schema lookup and append the REAL available names to the error so
# the agent self-corrects instead of re-guessing. Kept small and fail-safe.
_ENRICH_POLL_TIMEOUT = 10.0   # short bound for the internal introspection query
_MAX_ENRICH_TABLES = 5        # cap distinct tables introspected per COLUMN_NOT_FOUND
_MAX_ENRICH_NAMES = 200       # cap identifiers echoed back into the error text
# Best-effort extraction of table references from a FROM/JOIN clause. We only
# need the bare (last dotted segment) table name for an information_schema lookup.
_FROM_JOIN_RE = re.compile(r"(?:\bFROM\b|\bJOIN\b)\s+([A-Za-z_][\w.\"]*)", re.IGNORECASE)

# Env knobs (all optional; unset => permissive/dev-friendly behavior):
#   ADT_ATHENA_OUTPUT_ALLOWLIST  comma-separated s3:// prefixes a provided
#                                output_location must start with (defense-in-depth).
#   ADT_ATHENA_MAX_SCANNED_BYTES integer app-level cap on bytes scanned per query.
#   ADT_ATHENA_POLL_TIMEOUT_SECONDS
#                                global override for the DATA-query poll timeout
#                                (seconds; default 60.0).
#   ADT_ATHENA_INTROSPECTION_TIMEOUT_SECONDS
#                                global override for the short INTERNAL
#                                introspection poll bound (seconds; default 10.0).
_ENV_OUTPUT_ALLOWLIST = "ADT_ATHENA_OUTPUT_ALLOWLIST"
_ENV_MAX_SCANNED_BYTES = "ADT_ATHENA_MAX_SCANNED_BYTES"
_ENV_POLL_TIMEOUT_SECONDS = "ADT_ATHENA_POLL_TIMEOUT_SECONDS"
_ENV_INTROSPECTION_TIMEOUT_SECONDS = "ADT_ATHENA_INTROSPECTION_TIMEOUT_SECONDS"
#   ADT_ATHENA_SCHEMA_CACHE_TTL_SECONDS
#                                TTL of the per-process table-schema cache
#                                (information_schema.columns reads; default
#                                600.0; 0 disables). Measured 2026-09-24: 28%
#                                of a golden campaign's Athena executions were
#                                the SAME describe query, each one exposed to
#                                the on-demand queue like a data query.
_ENV_SCHEMA_CACHE_TTL_SECONDS = "ADT_ATHENA_SCHEMA_CACHE_TTL_SECONDS"
_DEFAULT_SCHEMA_CACHE_TTL = 600.0

# Per-system connection_config key for the (admin-configurable) scan budget.
_CONFIG_MAX_SCANNED_BYTES = "max_scanned_bytes"

# Per-system connection_config keys for the tunable poll timeouts. Same
# precedence + fail-closed coercion pattern as the scan budget:
# per-system config > env override > module default. The poll timeout applies
# to DATA queries only; introspection keeps its own SHORT bound so a slow
# metadata lookup can never hold a data query's generous window.
_CONFIG_POLL_TIMEOUT_SECONDS = "poll_timeout_seconds"
_CONFIG_INTROSPECTION_TIMEOUT_SECONDS = "introspection_timeout_seconds"

# Safe default when NOTHING configures a budget (neither per-system config nor the
# env override): ``None`` => no app-level cap, i.e. behavior is unchanged unless an
# operator opts in. The AUTHORITATIVE cost control remains the Athena workgroup's
# BytesScannedCutoffPerQuery; this app-level guard is a secondary, per-system knob.
_DEFAULT_MAX_SCANNED_BYTES: int | None = None


class AthenaConfigError(ValueError):
    """Raised when the connection_config is missing required Athena fields."""


class AthenaScanBudgetError(RuntimeError):
    """Raised when a query exceeds the configured scanned-bytes budget.

    A RuntimeError subclass (so existing ``except RuntimeError`` / propagation is
    unchanged) that additionally carries STRUCTURED, actionable fields — the bytes
    scanned, the budget, and human guidance — so a caller (the tool wrapper) can
    surface a clear "this would scan ~X; narrow the range or raise the budget"
    message instead of a cryptic failure. :meth:`to_dict` is the JSON-serializable
    projection the Strands tool returns.
    """

    def __init__(self, message: str, *, scanned_bytes: int, budget_bytes: int,
                 guidance: list[str] | None = None, partition_hint: str = ""):
        super().__init__(message)
        self.scanned_bytes = scanned_bytes
        self.budget_bytes = budget_bytes
        self.guidance = guidance or []
        self.partition_hint = partition_hint

    def to_dict(self) -> dict:
        out = {
            "error": str(self),
            "error_type": "scan_budget_exceeded",
            # A budget trip is a transient, narrowable limit — a narrower query
            # can succeed. It is NEVER a deterministic data-integrity block.
            "retryable": True,
            "scanned_bytes": self.scanned_bytes,
            "budget_bytes": self.budget_bytes,
            "guidance": self.guidance,
            # Advisory only: the tool NEVER rewrites and re-runs the query.
            "suggestion": bounded_degradation_suggestion(),
        }
        if self.partition_hint:
            out["partition_hint"] = self.partition_hint
        return out


# Fixed, honest guidance for an ENGINE scan-limit rejection. The engine was
# reachable and evaluated the query — it refused it only because the query would
# read more data than the engine allows per query. Worded to avoid EVERY
# orchestrator transient/auth marker and the chat_client timeout classifier, so
# the honest error is never retried blindly or mislabeled as a service outage.
# It never says "add a LIMIT": a LIMIT cuts only the rows returned, an aggregate
# still scans everything its WHERE lets through, and the advice pushed agents
# toward answering from a LIMIT sample. Every scan-limit message says so.
_LIMIT_BOUNDS_ROWS = "LIMIT bounds only the rows returned, not what an aggregate scans"
#: The narrowing a scan-limit recovery asks for: the question's own window,
#: never a partition bound the question did not ask for (it leaves rows out and
#: changes the result; ``storage_bound_rule.PARTITION_RULE``).
_NARROW_WINDOW = (
    "narrow the WHERE filter on the time column the question asks about and select "
    "fewer columns. A bound on a partition column the question did not ask for leaves "
    "rows out and changes the result, so do not add one")
_NARROW_WINDOW_HINT = f" | {_NARROW_WINDOW}"
_ENGINE_SCAN_LIMIT_GUIDANCE = (
    "The system answered: the query read more data than the engine permits per "
    "query. Narrow it: " + _NARROW_WINDOW + ", then run it again. A COUNT, SUM, MIN "
    "or MAX that is over the limit is computed exactly by the tool unless the result "
    "says why it could not be. The tool may decline when too little time is left for "
    "the pieces; then run a narrower query. A " + _LIMIT_BOUNDS_ROWS + ". This "
    "is a query that was too broad, not the system being down."
)


class AthenaEngineScanLimitError(RuntimeError):
    """A query the ENGINE refused because it would scan more than the per-query
    limit (workgroup ``BytesScannedCutoffPerQuery`` or an engine resource bound).

    Distinct from :class:`AthenaScanBudgetError` (the APP-side budget, tripped by
    polling ``DataScannedInBytes`` mid-flight): this one arrives as a FAILED
    terminal state whose ``StateChangeReason`` matches :func:`_is_scan_limit`.
    Before this class it was flattened to ``query_unavailable`` and the agent
    narrated the system as down; now it carries the SAME structured, retryable
    ``scan_budget_exceeded`` shape as its sibling, plus a ``limit_source`` marker
    and the partition hint computed from the referenced table(s).
    """

    def __init__(self, message: str, *, limit_source: str,
                 scanned_bytes: int | None = None, partition_hint: str = ""):
        super().__init__(message)
        self.limit_source = limit_source
        self.scanned_bytes = scanned_bytes
        self.partition_hint = partition_hint
        # Set by run_athena_query_exact when the exact-total split was tried
        # and could not give a total: why, in a few plain words.
        self.exact_split: dict | None = None
        # The Athena client that ran the refused query, so the exact split
        # reuses it instead of building one (never serialized).
        self.athena_client = None

    def to_dict(self) -> dict:
        out = {
            "error": str(self),
            "error_type": "scan_budget_exceeded",
            # Narrowable, so retryable with a tighter query — NEVER a permanent
            # data-integrity block, and NEVER a service outage.
            "retryable": True,
            "limit_source": self.limit_source,
            "guidance": _ENGINE_SCAN_LIMIT_GUIDANCE,
            "suggestion": bounded_degradation_suggestion(),
        }
        if self.exact_split:
            out["exact_split"] = dict(self.exact_split)
        # Only surface a measurement the engine actually reported — never a
        # fabricated zero.
        if isinstance(self.scanned_bytes, int) and not isinstance(self.scanned_bytes, bool):
            out["scanned_bytes"] = self.scanned_bytes
        if self.partition_hint:
            out["partition_hint"] = self.partition_hint
        return out


def bounded_degradation_suggestion(system_id: str = "", table: str = "") -> dict:
    """Structured bounded-degradation SUGGESTION for an over-budget/blocked query.

    Names the concrete mapped event-time column when the graph knows it (the
    SAME lookup ``metric_dimension._resolve_order_column`` uses), otherwise the
    generic phrasing. Advisory ONLY — the caller may choose to run a narrower
    approximate query; nothing here (or in any consumer) auto-executes it.
    """
    element = "time-bound predicate on the mapped event-time column"
    # Short-circuit when no graph is configured: this runs on an ERROR path,
    # and without GRAPH_IDENTIFIER the lookup can only fail (or open a live
    # socket toward a default endpoint) — never attempt it.
    if system_id and table and os.environ.get("GRAPH_IDENTIFIER"):
        try:
            from . import metric_dimension
            from .db import validate_identifier
            column = str(metric_dimension._resolve_order_column(system_id, table) or "")
            from .engine_names import printable
            if column and len(column) <= 128 and printable(column):
                # The column name is GRAPH-DERIVED text interpolated into
                # prose the model reads — validate it as a strict, bounded
                # identifier (fail-closed: a hostile/odd name falls back to
                # the generic phrasing via the ValueError below).
                element = (
                    "time-bound predicate on the mapped event-time column "
                    f"'{validate_identifier(column)}'"
                )
        except Exception:  # noqa: BLE001 - best-effort naming; never break the error path
            pass
    return {
        "strategy": "bounded_estimate",
        "elements": [
            element,
            "LIMIT",
            "approx_percentile(...) instead of exact percentile, and report the figures "
            "as approximate",
        ],
        "note": (
            "an approximation the caller may choose to run — not executed "
            "automatically"
        ),
    }



class AthenaQueryTimeoutError(RuntimeError):
    """Structured timeout for a query that did not reach a terminal state.

    Carries only execution metadata needed for diagnosis. It never includes SQL,
    credentials, or result rows.
    """

    def __init__(self, *, query_id: str, last_state: str,
                 statistics: dict | None = None, poll_timeout: float):
        self.query_id = str(query_id)
        self.last_state = str(last_state or "UNKNOWN")
        self.statistics = dict(statistics or {})
        self.scanned_bytes = self.statistics.get("DataScannedInBytes")
        self.poll_timeout = float(poll_timeout)
        super().__init__(
            f"Athena query {self.query_id} did not finish within "
            f"{self.poll_timeout:.0f}s (last state {self.last_state})"
        )

    def to_dict(self) -> dict:
        out = {
            "error": str(self),
            "error_type": "query_timeout",
            "query_id": self.query_id,
            "last_state": self.last_state,
            "scanned_bytes": self.scanned_bytes,
            "statistics": self.statistics,
        }
        if getattr(self, "retried", False):
            out["retried_once"] = True
        return out

def _coerce_budget(value, *, source: str = _ENV_MAX_SCANNED_BYTES) -> int | None:
    """Coerce a scan-budget value (from config or env) into a positive int, or None.

    Accepts an int, a numeric string, or None/"" (=> None, "no budget configured").
    Fail-closed: a malformed or non-positive value is a configuration error so a
    typo never silently disables the guard. ``source`` names the offending knob in
    the error message (env var or config key).
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass, reject explicitly
        raise AthenaConfigError(f"{source} must be an integer number of bytes, got {value!r}")
    if isinstance(value, int):
        budget = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            budget = int(raw)
        except ValueError as e:
            raise AthenaConfigError(
                f"{source} must be an integer number of bytes, got {raw!r}"
            ) from e
    if budget <= 0:
        raise AthenaConfigError(f"{source} must be a positive integer, got {budget}")
    return budget


def _output_allowlist() -> list[str]:
    """Parse ``ADT_ATHENA_OUTPUT_ALLOWLIST`` into a list of s3:// prefixes.

    Empty/unset => empty list (format-validate only). This is an opt-in
    defense-in-depth control layered on top of format validation.
    """
    raw = os.environ.get(_ENV_OUTPUT_ALLOWLIST, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _validate_output_location(output_location: str) -> None:
    """Fail-closed validation of a user-supplied Athena result OutputLocation.

    A user-supplied output_location is written by the task IAM role, so an
    arbitrary/cross-account bucket here is a data-egress risk. We require a
    well-formed ``s3://<bucket>[/<prefix>]`` URI and, when an allowlist is
    configured, that it fall under an approved prefix.

    NOTE: The authoritative control is the Athena WORKGROUP's *enforced*
    ResultConfiguration (set ``EnforceWorkGroupConfiguration=true`` and pin the
    workgroup OutputLocation in production). This app-level check is a secondary,
    generic guard for when a workgroup is not (or cannot be) enforcing output.
    """
    parsed = urlsplit(output_location)
    if parsed.scheme != "s3":
        raise AthenaConfigError(
            f"Athena output_location must be an s3:// URI, got scheme {parsed.scheme!r}"
        )
    bucket = parsed.netloc
    if not bucket or not _S3_BUCKET_RE.match(bucket):
        raise AthenaConfigError(
            f"Athena output_location has an invalid S3 bucket name: {bucket!r}"
        )
    allowlist = _output_allowlist()
    if allowlist and not any(output_location.startswith(prefix) for prefix in allowlist):
        raise AthenaConfigError(
            f"Athena output_location {output_location!r} is not under an allowed prefix "
            f"({_ENV_OUTPUT_ALLOWLIST})"
        )


def _max_scanned_bytes_budget() -> int | None:
    """Parse ``ADT_ATHENA_MAX_SCANNED_BYTES`` into a positive int budget, or None.

    Unset/empty => None (no cap; behavior unchanged). Malformed/non-positive is
    a fail-closed configuration error. This is the GLOBAL env override; a
    per-system ``connection_config`` budget takes precedence (see
    :func:`resolve_scan_budget`).
    """
    return _coerce_budget(os.environ.get(_ENV_MAX_SCANNED_BYTES, ""))


def resolve_scan_budget(connection_config: dict | None) -> int | None:
    """Resolve the effective scanned-bytes budget for a system, or None (no cap).

    Precedence (generic — no dataset knowledge):
      1. the per-system ``connection_config['max_scanned_bytes']`` (admin-set);
      2. the global ``ADT_ATHENA_MAX_SCANNED_BYTES`` env override;
      3. the safe default ``_DEFAULT_MAX_SCANNED_BYTES`` (``None`` => no cap).

    Both sources are coerced fail-closed (a malformed value raises
    :class:`AthenaConfigError`), so a typo never silently disables the guard.
    """
    cfg = connection_config or {}
    per_system = _coerce_budget(cfg.get(_CONFIG_MAX_SCANNED_BYTES),
                                source=_CONFIG_MAX_SCANNED_BYTES)
    if per_system is not None:
        return per_system
    env_budget = _max_scanned_bytes_budget()
    if env_budget is not None:
        return env_budget
    return _DEFAULT_MAX_SCANNED_BYTES


def _coerce_timeout(value, *, source: str, require_positive: bool = True) -> float | None:
    """Coerce a timeout value (from config or env) into a positive float, or None.

    Accepts an int/float, a numeric string, or None/"" (=> None, "not
    configured"). Fail-closed — the SAME pattern as :func:`_coerce_budget`: a
    malformed, NON-FINITE or non-positive value raises :class:`AthenaConfigError`,
    so a typo never silently zeroes (or disables) a poll bound. ``source`` names
    the offending knob (env var or config key) in the error message.

    Finiteness is a HARD requirement on every path. ``float()`` happily parses
    ``"inf"``/``"Infinity"``/``"nan"``, and neither survives
    :func:`_poll_athena_query`'s ``deadline = time.monotonic() + timeout``:
    ``inf`` makes ``monotonic() < deadline`` permanently true — an UNBOUNDED poll
    loop that outlives the request — while ``nan`` makes every comparison false,
    collapsing the poll to an immediate timeout that reports a nonsense bound.
    Both are configuration errors, never a usable timeout.

    ``require_positive=False`` relaxes ONLY the positivity check (not finiteness)
    for the explicit ``poll_timeout=`` override, where a zero/negative deadline
    is a legitimate "poll once, then time out" probe.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass, reject explicitly
        raise AthenaConfigError(f"{source} must be a number of seconds, got {value!r}")
    if isinstance(value, (int, float)):
        timeout = float(value)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            timeout = float(raw)
        except ValueError as e:
            raise AthenaConfigError(
                f"{source} must be a number of seconds, got {raw!r}"
            ) from e
    if not math.isfinite(timeout):
        raise AthenaConfigError(
            f"{source} must be a finite number of seconds, got {value!r}"
        )
    if require_positive and timeout <= 0:
        raise AthenaConfigError(f"{source} must be a positive number of seconds, got {timeout}")
    return timeout


def resolve_poll_timeout(connection_config: dict | None) -> float:
    """Resolve the effective DATA-query poll timeout (seconds) for a system.

    Precedence (generic — the same pattern as :func:`resolve_scan_budget`):
      1. the per-system ``connection_config['poll_timeout_seconds']`` (admin-set);
      2. the global ``ADT_ATHENA_POLL_TIMEOUT_SECONDS`` env override;
      3. the module default ``_DEFAULT_POLL_TIMEOUT`` (60.0).

    Applies to DATA queries ONLY — the internal introspection bound is resolved
    separately by :func:`resolve_introspection_timeout` and stays short. Both
    sources are coerced fail-closed (malformed raises :class:`AthenaConfigError`).
    """
    cfg = connection_config or {}
    per_system = _coerce_timeout(cfg.get(_CONFIG_POLL_TIMEOUT_SECONDS),
                                 source=_CONFIG_POLL_TIMEOUT_SECONDS)
    if per_system is not None:
        return per_system
    env_timeout = _coerce_timeout(os.environ.get(_ENV_POLL_TIMEOUT_SECONDS, ""),
                                  source=_ENV_POLL_TIMEOUT_SECONDS)
    if env_timeout is not None:
        return env_timeout
    return _DEFAULT_POLL_TIMEOUT


def resolve_introspection_timeout(connection_config: dict | None) -> float:
    """Resolve the SHORT internal-introspection poll bound (seconds) for a system.

    Precedence (same pattern as :func:`resolve_poll_timeout`):
      1. the per-system ``connection_config['introspection_timeout_seconds']``;
      2. the global ``ADT_ATHENA_INTROSPECTION_TIMEOUT_SECONDS`` env override;
      3. the short module default ``_ENRICH_POLL_TIMEOUT`` (10.0).

    Used at every internal introspection call site (partition-column discovery,
    error enrichment) so a slow information_schema read stays bounded and can
    never consume the data query's generous window.
    """
    cfg = connection_config or {}
    per_system = _coerce_timeout(cfg.get(_CONFIG_INTROSPECTION_TIMEOUT_SECONDS),
                                 source=_CONFIG_INTROSPECTION_TIMEOUT_SECONDS)
    if per_system is not None:
        return per_system
    env_timeout = _coerce_timeout(
        os.environ.get(_ENV_INTROSPECTION_TIMEOUT_SECONDS, ""),
        source=_ENV_INTROSPECTION_TIMEOUT_SECONDS)
    if env_timeout is not None:
        return env_timeout
    return _ENRICH_POLL_TIMEOUT


def _resolve_config(connection_config: dict | None) -> dict:
    """Normalize + validate an Athena connection_config. Fail-closed on missing required fields."""
    cfg = dict(connection_config or {})
    workgroup = str(cfg.get("workgroup", "") or "").strip()
    database = str(cfg.get("database", "") or cfg.get("schema", "") or "").strip()
    if not workgroup:
        raise AthenaConfigError("Athena connection_config requires 'workgroup'")
    if not database:
        raise AthenaConfigError("Athena connection_config requires 'database'")
    output_location = str(cfg.get("output_location", "") or "").strip()
    if output_location:
        _validate_output_location(output_location)
    return {
        "workgroup": workgroup,
        "database": database,
        "catalog": str(cfg.get("catalog", "") or "AwsDataCatalog").strip(),
        "output_location": output_location,
        "region": str(cfg.get("region", "") or os.environ.get("AWS_REGION", "us-east-1")).strip(),
        # OPTIONAL cross-account keys (generic, connector-agnostic). Preserved so
        # the shared aws_session helper can STS AssumeRole when role_arn is set;
        # blank => ambient credentials (unchanged behavior). external_id is a
        # secret and is never logged (see agents.aws_session).
        "role_arn": str(cfg.get("role_arn", "") or "").strip(),
        "external_id": str(cfg.get("external_id", "") or "").strip(),
        # OPTIONAL per-system scanned-bytes budget (admin-configurable). Coerced
        # fail-closed to a positive int or None (no per-system cap). Threaded
        # through so run_athena_query can enforce a per-system limit; see
        # resolve_scan_budget for the config>env>default precedence.
        "max_scanned_bytes": _coerce_budget(cfg.get(_CONFIG_MAX_SCANNED_BYTES),
                                            source=_CONFIG_MAX_SCANNED_BYTES),
        # OPTIONAL per-system poll timeouts (admin-configurable). Coerced
        # fail-closed to a positive float or None so a typo raises at
        # tool-construction time. Preserved so the resolved config (the dict
        # every guarded/tool path re-passes) keeps carrying them; see
        # resolve_poll_timeout / resolve_introspection_timeout for precedence.
        _CONFIG_POLL_TIMEOUT_SECONDS: _coerce_timeout(
            cfg.get(_CONFIG_POLL_TIMEOUT_SECONDS),
            source=_CONFIG_POLL_TIMEOUT_SECONDS),
        _CONFIG_INTROSPECTION_TIMEOUT_SECONDS: _coerce_timeout(
            cfg.get(_CONFIG_INTROSPECTION_TIMEOUT_SECONDS),
            source=_CONFIG_INTROSPECTION_TIMEOUT_SECONDS),
        # OPTIONAL per-system caps of the exact-total split (admin knobs next
        # to max_scanned_bytes). Coerced fail-closed by athena_exact_split.
        **_exact_split_config(cfg),
    }


def _exact_split_config(cfg: dict) -> dict:
    from .athena_exact_split import coerce_config
    return coerce_config(cfg)


def _safe_sql_literal(value: str) -> str:
    """Guard a value destined for a SQL string literal (identifier introspection).

    Rejects quotes/semicolons/backslashes so a table/schema name cannot break out
    of the ``information_schema`` predicate. Not a substitute for parameterization,
    but Athena's information_schema does not support bind parameters, so this is
    the fail-closed guard for the two internal introspection queries.
    """
    if any(ch in value for ch in ("'", '"', ";", "\\", "\n", "\r")):
        raise ValueError(f"invalid identifier value: {value!r}")
    return value


# --- Physical-column discovery (real columns + partition keys) ---------------
# ``information_schema.columns`` returns the REAL physical columns of a Glue/Hive
# table, their ``data_type``, and an ``extra_info`` marker that is the literal
# string ``"partition key"`` for Hive partition columns (generic across the Hive
# connector — no dataset knowledge). Nested-struct columns come back as a single
# ``row(...)`` type, so we flatten them into the dotted leaf paths a query must
# actually reference (e.g. ``state_context_map.started.code``). All pure/parsing —
# no IO — so it is exhaustively unit-testable without AWS.


def _split_top_level(s: str) -> list[str]:
    """Split ``s`` on commas that are NOT nested inside parentheses/brackets."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _flatten_struct(prefix: str, data_type: str) -> list[tuple[str, str]]:
    """Flatten a (possibly nested) ``row(...)`` type into ``(dotted_path, leaf_type)``.

    A scalar type returns ``[(prefix, data_type)]`` unchanged. A ``row(a int, b
    row(c varchar))`` under ``prefix='s'`` yields ``[('s.a','int'), ('s.b.c',
    'varchar')]``. Non-``row`` complex types (``array(...)``, ``map(...)``) are
    treated as leaves — they cannot be dotted into without UNNEST, so the column
    is surfaced as-is. Malformed structs fall back to the bare column.
    """
    dt = (data_type or "").strip()
    low = dt.lower()
    if low.startswith("row(") and dt.endswith(")"):
        inner = dt[dt.index("(") + 1 : -1]
        out: list[tuple[str, str]] = []
        for field in _split_top_level(inner):
            # Each field is "<name> <type>"; the name is the first whitespace token.
            sp = field.split(None, 1)
            if len(sp) != 2:
                continue
            fname = sp[0].strip().strip('"')
            ftype = sp[1].strip()
            if not fname:
                continue
            out.extend(_flatten_struct(f"{prefix}.{fname}", ftype))
        return out or [(prefix, dt)]
    return [(prefix, dt)]


def flatten_athena_columns(rows) -> list[dict]:
    """Turn ``information_schema.columns`` rows into physical column descriptors.

    ``rows`` is an iterable of sequences ``[column_name, data_type, extra_info?]``
    (as returned by ``run_athena_query``). Returns a de-duplicated list of
    ``{"name", "data_type", "is_partition"}`` with nested ``row(...)`` columns
    flattened to dotted leaf paths. A column is a partition key when its
    ``extra_info`` contains ``"partition key"`` (the Hive connector marker).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not row:
            continue
        name = str(row[0]).strip() if row[0] is not None else ""
        if not name:
            continue
        data_type = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        extra = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
        is_partition = "partition key" in extra.lower()
        for leaf_name, leaf_type in _flatten_struct(name, data_type):
            if leaf_name in seen:
                continue
            seen.add(leaf_name)
            out.append(
                {"name": leaf_name, "data_type": leaf_type, "is_partition": is_partition}
            )
    return out


def describe_physical_columns(
    table_name: str, connection_config: dict, *, client=None
) -> list[dict]:
    """Return the REAL physical columns of ``table_name`` (flattened + partition-flagged).

    A metadata-only ``information_schema.columns`` lookup (no data scan) run through
    the normal execute/poll path with ``_enrich=False`` (no recursive enrichment).
    Returns ``[{"name","data_type","is_partition"}, ...]``; raises on config/query
    error (callers wrap best-effort).
    """
    return describe_physical_columns_bounded(
        table_name, connection_config, client=client,
    )[0]


def describe_physical_columns_bounded(
    table_name: str, connection_config: dict, *, client=None, refresh: bool = False
) -> tuple[list[dict], bool]:
    """:func:`describe_physical_columns` plus an AFFIRMATIVE completeness flag.

    Returns ``(columns, introspection_complete)``. ``introspection_complete`` is
    ``True`` ONLY when the ``information_schema.columns`` read came back strictly
    under the :data:`MAX_ROWS` row cap, i.e. the column list is provably whole.
    Callers that want to bake "this table has NO partition keys" MUST gate on it:
    a capped read could have truncated the very partition-key rows that would
    have flipped the answer, and an unflagged-but-partitioned table is exactly
    the false negative the baked-metadata fast path must never produce.

    ``refresh=True`` BYPASSES the schema-cache read (the fresh result is still
    stored): the steward's accept of an additive schema_drift row must write
    the CURRENT source, never a describe warmed minutes earlier (ADR-0077 D1).
    """
    cfg = _resolve_config(connection_config)
    athena = client or boto3_client_for("athena", cfg, default_region=cfg["region"])
    rows, complete = _introspect_columns_bounded(athena, cfg, table_name, refresh=refresh)
    return flatten_athena_columns(rows), complete


# --- Partition pruning (scan-cost guard for composed aggregate SQL) ----------
# A full-table GROUP BY / aggregate over a large partitioned table blows through
# the workgroup's BytesScannedCutoffPerQuery. The deterministic metric paths
# (metric_resolver/_dimension) therefore restrict their composed SQL to the N most
# recent partitions when the table is partitioned. Everything here is GENERIC (no
# dataset knowledge), metadata-only, and STRICTLY BEST-EFFORT: any discovery
# failure yields ``None`` => the caller runs the query unpruned (today's behavior,
# an honest env-side error if the limit then trips).

# Default number of most-recent partitions a pruned query is restricted to.
RECENT_PARTITIONS_LIMIT = 7

# Process-lifetime cache of a table's partition columns, keyed by the identity of
# the physical table: (region, catalog, database, role_arn, table). Partition
# COLUMNS are schema (stable for a process lifetime); partition VALUES are data
# and are re-read on every pruned query (new partitions land continuously).
_PARTITION_COLUMNS_CACHE: dict[tuple, list[dict]] = {}

# TTL cache of a table's information_schema.columns read, keyed by the SAME
# physical-table identity as the partition cache. Schema is stable on the order
# of minutes, so both the agent-facing ``athena_describe_table`` tool and the
# internal ``_introspect_columns_bounded`` share ONE entry per table — a
# describe the agent just ran is the introspection the enrichment path would
# otherwise re-run. Failures are NEVER cached (a transient error must not pin
# an empty schema); an entry stores exactly what the uncached call returned.
_SCHEMA_CACHE: dict[tuple, tuple[float, object]] = {}


def _schema_cache_ttl() -> float:
    raw = os.environ.get(_ENV_SCHEMA_CACHE_TTL_SECONDS)
    if raw is None or not str(raw).strip():
        return _DEFAULT_SCHEMA_CACHE_TTL
    try:
        ttl = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{_ENV_SCHEMA_CACHE_TTL_SECONDS} must be a number of seconds (got {raw!r})"
        ) from exc
    if ttl != ttl or ttl < 0 or ttl == float("inf"):
        raise ValueError(f"{_ENV_SCHEMA_CACHE_TTL_SECONDS} must be finite and >= 0 (got {raw!r})")
    return ttl


def _schema_cache_key(cfg: dict, table: str, variant: str) -> tuple:
    return (cfg["region"], cfg.get("catalog"), cfg["database"], cfg.get("role_arn"),
            table, variant)


def _schema_cached(key: tuple):
    """Return the cached value or ``None`` (expired entries are evicted)."""
    ttl = _schema_cache_ttl()
    if ttl <= 0:
        return None
    hit = _SCHEMA_CACHE.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if time.monotonic() - stored_at > ttl:
        _SCHEMA_CACHE.pop(key, None)
        return None
    return value


def _schema_store(key: tuple, value) -> None:
    if _schema_cache_ttl() > 0:
        _SCHEMA_CACHE[key] = (time.monotonic(), value)


def clear_schema_cache() -> None:
    """Test/ops hook: drop every cached schema read (table formats included)."""
    _SCHEMA_CACHE.clear()
    _TABLE_FORMAT_CACHE.clear()
    _PARTITION_FIELDS_CACHE.clear()


# --- Catalog metadata API (introspection WITHOUT a query execution) -----------
# ``athena:GetTableMetadata`` / ``ListTableMetadata`` are synchronous Glue-catalog
# reads: no StartQueryExecution, no on-demand capacity queue, no result S3 object.
# Measured 2026-09-24 in a cold data account: information_schema reads queued
# 60-135 s behind NOTHING (peak in-flight = 1) purely because on-demand capacity
# had to be allocated; the metadata API answers in ~100 ms. Every failure
# (AccessDenied on a least-privilege role, an unsupported catalog, a test double
# without the method) falls back to the information_schema query that always
# worked, so this is an optimization with no new failure mode.
_INTROSPECTION_PARTITION_MARKER = "partition key"


def _table_columns_via_metadata_api(athena, cfg: dict, table: str):
    """``[(name, type, extra_info)]`` from GetTableMetadata, or ``None`` to fall back.

    Partition keys come LAST with ``extra_info='partition key'`` — the same
    order and marker ``information_schema.columns`` returns, so every consumer
    (flatten_athena_columns, the partition cache, the enrichment path) is shape-
    identical whichever source answered.
    """
    try:
        meta = athena.get_table_metadata(
            CatalogName=cfg.get("catalog") or "AwsDataCatalog",
            DatabaseName=cfg["database"], TableName=table,
        )["TableMetadata"]
    except Exception as exc:  # noqa: BLE001 - ANY failure => SQL fallback (documented above)
        if "EntityNotFound" in str(exc) or "Entity Not Found" in str(exc):
            # The catalog is authoritative: a table it does not know has no
            # columns. Running the information_schema query would return the
            # same empty set after a queue wait, so answer here.
            return []
        logger.debug("GetTableMetadata unavailable for %s (%s) — falling back to "
                     "information_schema", table, type(exc).__name__)
        return None
    _record_table_format(cfg, table, meta)
    rows = [(str(c.get("Name")), str(c.get("Type") or ""), "")
            for c in meta.get("Columns") or [] if c.get("Name")]
    rows += [(str(c.get("Name")), str(c.get("Type") or ""), _INTROSPECTION_PARTITION_MARKER)
             for c in meta.get("PartitionKeys") or [] if c.get("Name")]
    return rows


# --- Table storage format (hidden partitioning) --------------------------------
# An Apache Iceberg table partitions by TRANSFORMS of ordinary columns (the day
# of a timestamp, a region value), so the catalog lists NO partition key for it
# while its storage is partitioned. "Zero partition keys" therefore proves
# nothing about an Iceberg table, and calling it unpartitioned is wrong. The
# catalog read that already describes a table (GetTableMetadata) carries the
# format in ``Parameters.table_type``; it is recorded here as a side effect of
# that same read, so knowing the format costs no extra call. Process lifetime
# like the partition-column cache (a table's format does not change); cleared
# with the schema cache. Only an AFFIRMATIVE "iceberg" is ever acted on: an
# unread table, or one read only through information_schema, stays as before.
_TABLE_FORMAT_CACHE: dict[tuple, str] = {}
_ICEBERG_FORMAT = "iceberg"
_OTHER_FORMAT = "other"
# A partition field name as the ``$partitions`` row value renders it.
_PARTITION_FIELD_RE = re.compile(r"(?:^\{|,\s*)([A-Za-z_][A-Za-z0-9_]*)=")
_MAX_PARTITION_FIELDS = 16


def _table_format_key(cfg: dict, table: str) -> tuple:
    return (cfg["region"], cfg.get("catalog"), cfg["database"], cfg.get("role_arn"), table)


def _record_table_format(cfg: dict, table: str, meta: dict) -> None:
    params = meta.get("Parameters") if isinstance(meta.get("Parameters"), dict) else {}
    kinds = {str(value).strip().lower() for key, value in params.items()
             if str(key).strip().lower() == "table_type"}
    kinds.add(str(meta.get("TableType") or "").strip().lower())
    _TABLE_FORMAT_CACHE[_table_format_key(cfg, table)] = (
        _ICEBERG_FORMAT if _ICEBERG_FORMAT in kinds else _OTHER_FORMAT)


def known_hidden_partitioning(table_name: str, connection_config: dict) -> bool:
    """True only when a catalog read in this process said ``table_name`` is an
    Iceberg table (hidden partitioning). Pure cache lookup, never I/O; an
    unread table or an unusable config is ``False`` (nothing is claimed)."""
    try:
        cfg = _resolve_config(connection_config)
    except Exception:  # noqa: BLE001 - an unusable config claims nothing
        return False
    return _TABLE_FORMAT_CACHE.get(_table_format_key(cfg, table_name)) == _ICEBERG_FORMAT


def _partition_fields_from_value(value) -> list[str]:
    """Field names of one ``$partitions`` ``partition`` value (``{a=x, b=y}``).

    Anything that does not read as that row shape gives ``[]`` (fail soft)."""
    text = str(value or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return []
    return list(dict.fromkeys(_PARTITION_FIELD_RE.findall(text)))[:_MAX_PARTITION_FIELDS]


def hidden_partition_fields(
    table_name: str, connection_config: dict, *, client=None
) -> list[str] | None:
    """The partition FIELDS of a table with hidden partitioning, for the schema line.

    ``None`` when the table is not known to be an Iceberg table (no query runs).
    For one that is, reads one row of the metadata-only ``"<table>$partitions"``
    relation and returns the field names of its ``partition`` value; any failure
    returns ``[]`` (still hidden partitioning, fields unknown). Called where the
    schema is described at onboarding, never per data query.
    """
    if not known_hidden_partitioning(table_name, connection_config):
        return None
    try:
        cfg = _resolve_config(connection_config)
        sql = (
            'SELECT "partition" FROM '  # nosec B608 - identifiers validated fail-closed (_quoted_identifier); no untrusted values
            f"{_quoted_identifier(cfg['database'])}."
            f"{_quoted_identifier(table_name + '$partitions')} LIMIT 1"
        )
        result = run_athena_query(
            sql, cfg, client=client,
            poll_timeout=resolve_introspection_timeout(cfg), _enrich=False,
        )
    except Exception as exc:  # noqa: BLE001 - fields are best-effort; the format stands
        logger.info("Partition fields unread for %s (%s)", table_name, type(exc).__name__)
        return []
    rows = result.get("rows") or []
    return _partition_fields_from_value(rows[0][0]) if rows and rows[0] else []


def _table_names_via_metadata_api(athena, cfg: dict):
    """Sorted table names from ListTableMetadata (paginated), or ``None`` to fall back."""
    names: list[str] = []
    token = None
    try:
        while True:
            kwargs = {"CatalogName": cfg.get("catalog") or "AwsDataCatalog",
                      "DatabaseName": cfg["database"], "MaxResults": 50}
            if token:
                kwargs["NextToken"] = token
            page = athena.list_table_metadata(**kwargs)
            names += [str(t.get("Name")) for t in page.get("TableMetadataList") or []
                      if t.get("Name")]
            token = page.get("NextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 - ANY failure => SQL fallback
        logger.debug("ListTableMetadata unavailable (%s) — falling back to "
                     "information_schema", type(exc).__name__)
        return None
    return sorted(set(names))

# Numeric Athena/Trino types whose partition values are emitted as bare literals.
_NUMERIC_TYPE_PREFIXES = (
    "tinyint", "smallint", "int", "integer", "bigint",
    "real", "double", "float", "decimal",
)
_NUMERIC_VALUE_RE = re.compile(r"^-?\d+(\.\d+)?$")


def list_partition_columns(
    table_name: str, connection_config: dict, *, client=None
) -> list[dict]:
    """The partition columns of ``table_name`` as ``[{"name","data_type"}, ...]``.

    Discovered generically via ``information_schema.columns`` (the Hive connector
    marks partition keys with ``extra_info='partition key'``) through the SAME
    cross-account client plumbing every other Athena call uses. Cached for the
    process lifetime per physical table (failures are NOT cached, so a transient
    error retries next call). Raises on config/query error (callers wrap).
    """
    cfg = _resolve_config(connection_config)
    key = (cfg["region"], cfg["catalog"], cfg["database"], cfg["role_arn"], table_name)
    if key in _PARTITION_COLUMNS_CACHE:
        return _PARTITION_COLUMNS_CACHE[key]
    # ``client`` may be None: run_athena_query creates one lazily only when the
    # REAL connector runs (keeps patched-connector tests free of boto3/STS).
    cols = _introspect_columns(client, cfg, table_name)
    out = [
        {"name": name, "data_type": data_type}
        for (name, data_type, extra) in cols
        if "partition key" in extra.lower()
    ]
    _PARTITION_COLUMNS_CACHE[key] = out
    return out


def _quoted_identifier(name: str) -> str:
    """Double-quote an identifier destined for composed SQL, rejecting embedded
    quotes/control characters (metadata-sourced, but validated fail-closed)."""
    if not name or any(ch in name for ch in ('"', "'", ";", "\\", "\n", "\r")):
        raise ValueError(f"invalid identifier: {name!r}")
    return f'"{name}"'


def _render_partition_literal(value, data_type: str) -> str:
    """Render one partition VALUE as a safely-quoted SQL literal for its type.

    Values are DATA, not identifiers: numeric types emit a regex-validated bare
    number; date/timestamp emit typed literals (Trino compares strictly); anything
    else emits a single-quoted string with embedded quotes doubled. Raises on an
    unrenderable value (caller falls back to no pruning).
    """
    if value is None:
        raise ValueError("NULL partition value")
    text = str(value)
    dtype = (data_type or "").strip().lower()
    if dtype.startswith(_NUMERIC_TYPE_PREFIXES):
        if not _NUMERIC_VALUE_RE.match(text):
            raise ValueError(f"non-numeric value {text!r} for type {data_type!r}")
        return text
    escaped = text.replace("'", "''")
    if dtype.startswith("timestamp"):
        return f"TIMESTAMP '{escaped}'"
    if dtype.startswith("date"):
        return f"DATE '{escaped}'"
    return f"'{escaped}'"


def _recent_partition_rows(
    athena, cfg: dict, table_name: str, pcols: list[dict], limit: int
) -> list[list]:
    """The ``limit`` most recent partition-value rows of ``table_name``.

    Reads the Trino/Athena ``"<table>$partitions"`` metadata view — a
    metadata-only relation whose columns are exactly the partition columns — so
    this never scans table data. Ordered by every partition column DESC so
    hierarchical layouts (e.g. year/month/day) sort most-recent-first.
    """
    select_list = ", ".join(_quoted_identifier(c["name"]) for c in pcols)
    order_list = ", ".join(f"{_quoted_identifier(c['name'])} DESC" for c in pcols)
    db_ident = _quoted_identifier(cfg["database"])
    table_ident = _quoted_identifier(f"{table_name}$partitions")
    sql = (
        f"SELECT DISTINCT {select_list} FROM {db_ident}.{table_ident} "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        f"ORDER BY {order_list} LIMIT {int(limit)}"
    )
    result = run_athena_query(sql, cfg, client=athena, _enrich=False)
    return [row for row in result.get("rows", []) if row]


def build_recent_partition_predicate(
    table_name: str,
    connection_config: dict,
    *,
    qualifier: str = "",
    limit: int = RECENT_PARTITIONS_LIMIT,
    client=None,
) -> str | None:
    """Best-effort WHERE predicate restricting ``table_name`` to its ``limit``
    most recent partitions, or ``None`` (=> caller runs the query unpruned).

    ``None`` when the table has no partition columns, when discovery/metadata
    reads fail, or when a partition value cannot be rendered safely — this
    function NEVER raises, so pruning can only narrow behavior, never break it.
    ``qualifier`` prefixes column references (e.g. ``"m."`` for an aliased join).

    Single partition column  → ``"pcol" IN (v1, v2, ...)``.
    Multiple partition cols  → ``(("p1"=v AND "p2"=v) OR ...)`` per recent row.
    """
    try:
        pcols = list_partition_columns(table_name, connection_config, client=client)
        if not pcols:
            return None
        cfg = _resolve_config(connection_config)
        rows = _recent_partition_rows(client, cfg, table_name, pcols, limit)
        if not rows:
            return None
        if len(pcols) == 1:
            col_ref = f"{qualifier}{_quoted_identifier(pcols[0]['name'])}"
            literals = []
            for row in rows:
                lit = _render_partition_literal(row[0], pcols[0]["data_type"])
                if lit not in literals:
                    literals.append(lit)
            return f"{col_ref} IN ({', '.join(literals)})"
        clauses = []
        for row in rows:
            if len(row) < len(pcols):
                return None
            conj = " AND ".join(
                f"{qualifier}{_quoted_identifier(c['name'])} = "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                f"{_render_partition_literal(row[i], c['data_type'])}"
                for i, c in enumerate(pcols)
            )
            clause = f"({conj})"
            if clause not in clauses:
                clauses.append(clause)
        return "(" + " OR ".join(clauses) + ")"
    except Exception:  # noqa: BLE001 - pruning is best-effort, never breaks the query
        return None


# --- Lake Formation column-level denial handling (bounded prune-and-retry) ---
# LIVE-CONFIRMED: Glue/graph metadata can list columns whose SELECT is DENIED by
# Lake Formation column-level grants. Athena then FAILS the whole query with
# ``COLUMN_NOT_FOUND: ... Column <x> cannot be resolved or requester is not
# authorized to access requested resources`` — indistinguishable from a typo at
# the engine level, but the column IS in our composed SELECT, so the denial is
# recoverable by dropping that column and retrying. Everything here is GENERIC
# (no dataset knowledge) and bounded.

# Max denied-column retries per query (each retry may surface ANOTHER denied
# column). The effective bound is min(this, half the composed columns).
MAX_DENIED_COLUMN_RETRIES = 5

# Robust extraction of the column named by a COLUMN_NOT_FOUND failure. Quote
# styles vary across engine versions ('x', "x", `x`, or bare); the name may be
# qualified (alias.col) — callers compare on the LAST dotted segment.
_DENIED_COLUMN_RE = re.compile(
    r"Column\s+[\"'`]?([A-Za-z_][\w.]*)[\"'`]?\s+cannot be resolved",
    re.IGNORECASE,
)


class AthenaColumnDeniedError(RuntimeError):
    """A composed column exists in the catalog metadata but SELECT on it is
    denied (Lake Formation column-level permissions) and the query has nothing
    to drop (the column IS the measure/dimension). Carries the column so callers
    can surface a clear, actionable message instead of the cryptic
    COLUMN_NOT_FOUND."""

    def __init__(self, message: str, *, column: str):
        super().__init__(message)
        self.column = column


def parse_denied_column(detail) -> str | None:
    """Best-effort: the (unqualified) column named by a COLUMN_NOT_FOUND error.

    Returns ``None`` when the text is not a COLUMN_NOT_FOUND failure or the
    column name cannot be parsed (callers then keep today's error path)."""
    text = str(detail or "")
    if "COLUMN_NOT_FOUND" not in text.upper():
        return None
    m = _DENIED_COLUMN_RE.search(text)
    if not m:
        return None
    return m.group(1).split(".")[-1].strip("\"'`") or None


def _lake_formation_denied_message(column: str) -> str:
    """Clear, actionable message for a non-droppable column denial. A column
    name that is not a plain identifier (``engine_names``) is never printed."""
    from .engine_names import printable
    shown = repr(column) if printable(column) else "a column whose name is withheld"
    return (
        f"column {shown} is restricted for this principal: the catalog lists "
        f"it but SELECT on it is denied (Lake Formation column-level "
        f"permissions). Ask the data owner to grant SELECT on this column, or "
        f"re-map the concept to an authorized column."
    )


def _match_denied_column(cols: list[str], column: str) -> str | None:
    """Match a parsed denied column against the composed column names (pure).

    Composed names may be FULL DOTTED struct paths (the Athena section path
    tracks/prunes nested ROW fields by their full dotted name — see
    ``entity_render.build_athena_section_sql``), while ``parse_denied_column``
    yields the LAST dotted segment. Exact (case-insensitive) match wins; else
    the first composed name whose dotted-path leaf matches. ``None`` when
    nothing matches (callers keep the original-error path)."""
    low = str(column).lower()
    exact = next((c for c in cols if c.lower() == low), None)
    if exact is not None:
        return exact
    return next((c for c in cols if c.lower().split(".")[-1] == low), None)


def _prunable_denied_column(exc: Exception, cols: list[str],
                            essential_lower: set, budget: int) -> str:
    """The composed column to PRUNE for a denial failure — or (re-)raise.

    Raises :class:`AthenaColumnDeniedError` when the denied column is essential
    (the measure/dimension — nothing to drop); re-raises ``exc`` when the error
    is unparsable, the column is not among the composed names, the retry budget
    is exhausted, or only one column remains (today's original-error path)."""
    column = parse_denied_column(str(exc))
    if not column:
        raise exc
    if column.lower() in essential_lower:
        raise AthenaColumnDeniedError(
            _lake_formation_denied_message(column), column=column,
        ) from exc
    match = _match_denied_column(cols, column)
    if match is None or budget <= 0 or len(cols) <= 1:
        raise exc
    return match


def run_athena_select_with_column_retry(
    compose_sql,
    columns: list[str],
    connection_config: dict,
    *,
    essential=(),
    max_retries: int | None = None,
    run=None,
) -> dict:
    """Execute a composed SELECT, pruning Lake-Formation-denied columns and
    retrying (bounded). The SHARED seam for the section renderer and the
    scalar/grouped metric paths.

    ``compose_sql(column_names)`` recomposes the SELECT from the remaining
    columns; ``columns`` is the initially-composed column-name list. On a
    failure whose COLUMN_NOT_FOUND names one of those columns:

    * a column in ``essential`` (the measure/dimension — nothing to drop) raises
      :class:`AthenaColumnDeniedError` with a clear LF message;
    * any other composed column is DROPPED and the query retried, bounded by
      ``min(MAX_DENIED_COLUMN_RETRIES, len(columns) // 2)`` (each retry may name
      another denied column) and never below one remaining column.

    An unparsable error, a column not in the composed list, or an exhausted
    bound re-raises the ORIGINAL error (today's path). Returns
    ``{"result", "columns", "denied"}`` where ``columns`` are the ACTUALLY
    selected names (lineage must reflect them) and ``denied`` the dropped ones.
    """
    # The default runner computes a refused COUNT/SUM/MIN/MAX exactly from
    # partition pieces; any other SQL behaves exactly as run_athena_query.
    runner = run or run_athena_query_exact
    cols = list(columns)
    denied: list[str] = []
    budget = (min(MAX_DENIED_COLUMN_RETRIES, len(cols) // 2)
              if max_retries is None else max_retries)
    essential_lower = {str(c).lower() for c in essential}
    while True:
        sql = compose_sql(cols)
        try:
            result = runner(sql, connection_config)
            return {"result": result, "columns": cols, "denied": denied}
        except AthenaScanBudgetError:
            raise
        except Exception as e:
            match = _prunable_denied_column(e, cols, essential_lower, budget)
            cols.remove(match)
            denied.append(match)
            budget -= 1


def _referenced_tables(sql: str) -> list[str]:
    """Best-effort: bare table names referenced in FROM/JOIN clauses (deduped, bounded).

    Used ONLY to enrich a COLUMN_NOT_FOUND error with the real columns of the
    referenced table(s). Returns the last dotted segment (``cat.db.tbl`` -> ``tbl``)
    since ``information_schema.columns`` keys on the unqualified ``table_name``.
    ``information_schema`` references are skipped so we never introspect the
    catalog view itself.
    """
    names: list[str] = []
    for match in _FROM_JOIN_RE.finditer(sql):
        raw = match.group(1).replace('"', "")
        if "information_schema" in raw.lower():
            continue
        table = raw.split(".")[-1]
        if table and table not in names:
            names.append(table)
        if len(names) >= _MAX_ENRICH_TABLES:
            break
    return names


def _first_column_values(athena, cfg: dict, sql: str) -> list[str]:
    """Run an internal introspection SELECT and return its first column's values.

    Reuses the SAME client and the normal execute/poll/collect path, but with
    ``_enrich=False`` (no recursive enrichment) and the short, tunable
    introspection poll bound (:func:`resolve_introspection_timeout`).
    """
    result = run_athena_query(
        sql, cfg, client=athena,
        poll_timeout=resolve_introspection_timeout(cfg), _enrich=False,
    )
    return [row[0] for row in result.get("rows", []) if row and row[0] is not None]


def _introspect_columns(athena, cfg: dict, table: str) -> list[tuple[str, str, str]]:
    """Return ``(column_name, data_type, extra_info)`` rows for one table.

    Metadata-only ``information_schema.columns`` lookup, shared by the actionable-
    error enrichment (TYPE_MISMATCH / scan-limit) and physical-column discovery.
    ``_enrich=False`` + the short, tunable introspection poll bound
    (:func:`resolve_introspection_timeout`) so it never recurses.
    """
    return _introspect_columns_bounded(athena, cfg, table)[0]


def _introspect_columns_bounded(
    athena, cfg: dict, table: str, *, refresh: bool = False,
) -> tuple[list[tuple[str, str, str]], bool]:
    """:func:`_introspect_columns` plus a row-cap completeness flag.

    Returns ``(rows, complete)``. The read is COMPLETE only when it came back
    strictly under :data:`MAX_ROWS` — the row cap this query is executed with. At
    exactly the cap the column list may have been truncated, so any conclusion
    drawn from the ABSENCE of a row (notably "no partition keys exist") is
    unsound; the flag lets the one caller that needs an affirmative answer fail
    closed instead of guessing. ``refresh=True`` skips the cache READ only.
    """
    cache_key = _schema_cache_key(cfg, table, "introspect")
    cached = None if refresh else _schema_cached(cache_key)
    if cached is not None:
        return cached
    api_rows = _table_columns_via_metadata_api(athena, cfg, table)
    if api_rows is not None:
        value = (api_rows, True)  # the catalog returns the WHOLE column list
        _schema_store(cache_key, value)
        return value
    safe_table = _safe_sql_literal(table)
    db = _safe_sql_literal(cfg["database"])
    result = run_athena_query(
        "SELECT column_name, data_type, extra_info FROM information_schema.columns "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        f"WHERE table_schema = '{db}' AND table_name = '{safe_table}' ORDER BY ordinal_position",
        cfg, client=athena,
        poll_timeout=resolve_introspection_timeout(cfg), _enrich=False,
    )
    raw_rows = result.get("rows", []) or []
    out: list[tuple[str, str, str]] = []
    for r in raw_rows:
        if not r:
            continue
        name = r[0]
        if name is None:
            continue
        data_type = (r[1] if len(r) > 1 else "") or ""
        extra = (r[2] if len(r) > 2 else "") or ""
        out.append((str(name), str(data_type), str(extra)))
    value = (out, len(raw_rows) < MAX_ROWS)
    _schema_store(cache_key, value)
    return value


# The ONE anchored engine-diagnostic parser both classifiers share. A terminal
# Athena StateChangeReason surfaces the engine error code as the LEADING token
# followed by ':', optionally behind the app's own wrapper
# ``"Athena query {state}: "`` (state ∈ the non-success members of _DONE). The
# wrapper words are DERIVED from _DONE so the parser cannot drift from
# ``_raise_failed_query``'s own ``f"Athena query {state.lower()}: ..."``.
_WRAPPER_STATES = "|".join(
    sorted(s.lower() for s in _DONE if s != "SUCCEEDED")
)
_LEADING_ENGINE_CODE_RE = re.compile(
    # Only the app's own wrapper words are case-insensitive; the captured code
    # stays case-SENSITIVE upper-snake, so a title-case prose prefix such as
    # "Query:" or "Error:" is never read as an engine error code.
    rf"^\s*(?:(?i:athena query (?:{_WRAPPER_STATES})):\s*)?([A-Z][A-Z0-9_]*):",
)


def _leading_engine_code(detail: str) -> str:
    """The leading engine error-code token (e.g. ``SYNTAX_ERROR``), or ``""``.

    Anchored at the start, allowing ONE optional ``Athena query <state>:`` wrapper
    (states derived from _DONE). A code is an all-upper-snake identifier
    immediately before a ``:``. A token appearing later — inside a quoted value or
    the message body — is NOT the leading code and is never returned. The wrapper
    keyword match is case-insensitive, but the captured code group is only the
    exact substring: callers compare it against upper-case allowlists.
    """
    m = _LEADING_ENGINE_CODE_RE.match(detail or "")
    return m.group(1) if m else ""


# Known engine error codes that ARE themselves a bytes-scanned cutoff. Athena
# publishes no such dedicated code (the cutoff surfaces as cancellation prose,
# not a NAME: code), so this is empty — a reason that leads with ANY engine code
# is therefore NOT a bytes-scanned cutoff. Kept as a seam so a documented cutoff
# code can be added without re-deriving the rule.
_SCAN_LIMIT_CODES: frozenset[str] = frozenset()

# Structural bytes-scanned CUTOFF diagnostic: the "bytes scanned" subject, the
# "limit"/"cutoff" noun, AND "exceeded" (the measured wording is
# "Bytes scanned limit was exceeded", sometimes behind cancellation prose like
# "Query cancelled: "). Requiring "exceeded" plus the no-leading-foreign-code
# guard in _is_scan_limit keeps a quoted phrase from classifying.
_SCAN_LIMIT_RE = re.compile(
    r"\bbytes\s+scanned\b[^.]{0,40}?\b(?:limit|cutoff)\b[^.]{0,40}?\bexceeded\b"
    r"|\bbytes\s+scanned\b[^.]{0,40}?\bexceeded\b[^.]{0,40}?\b(?:limit|cutoff)\b",
    re.IGNORECASE,
)


def _is_scan_limit(detail: str) -> bool:
    """True ONLY for an explicit bytes-scanned CUTOFF diagnostic.

    Structural, not an arbitrary phrase occurrence:
      1. the reason must not lead with a DIFFERENT engine error code (a reason
         that starts with ``NAME:`` is that engine error, not a cutoff — unless
         ``NAME`` is itself a known cutoff code, of which there are none today);
      2. it must carry the bytes-scanned cutoff shape INCLUDING "exceeded".
    Takes the RAW reason text; the patterns are case-insensitive. Generic
    resource exhaustion and a scan phrase quoted inside another error's message
    both return False, keeping today's mapping.
    """
    text = detail or ""
    code = _leading_engine_code(text)
    if code and code not in _SCAN_LIMIT_CODES:
        return False
    return bool(_SCAN_LIMIT_RE.search(text))


# CLOSED allowlist of Trino/Athena DETERMINISTIC SQL-rejection error NAMES. The
# engine was reachable and rejected the SQL deterministically — a different
# query is needed, retrying the same one cannot help. COLUMN_NOT_FOUND and
# TABLE_NOT_FOUND are deliberately EXCLUDED here: they keep the existing
# schema_mismatch mapping (which points the agent at athena_describe_table).
# Only the fixed NAME is ever surfaced to the model, never the raw engine text.
_INVALID_REQUEST_ERROR_NAMES = (
    "SYNTAX_ERROR",
    "EXPRESSION_NOT_AGGREGATE",
    "MISSING_GROUP_BY",
    "FUNCTION_NOT_FOUND",
    "INVALID_FUNCTION_ARGUMENT",
    "TYPE_MISMATCH",
    "INVALID_CAST_ARGUMENT",
    "NOT_SUPPORTED",
    "AMBIGUOUS_NAME",
    "TOO_MANY_ARGUMENTS",
    "INVALID_ARGUMENTS",
    "OPERATOR_NOT_FOUND",
)

# Fixed, honest guidance for a deterministic SQL rejection. Worded to avoid
# every orchestrator transient/auth marker and the chat_client timeout
# classifier, so a reachable-but-invalid rejection is never retried as a cold
# runtime nor mislabeled as a service outage.
_INVALID_REQUEST_GUIDANCE = (
    "The engine rejected the SQL; the system is reachable. Fix the query and run "
    "it again — call athena_describe_table for the exact columns and types. Never "
    "report this as the system being down or unavailable."
)


def _invalid_request_name(detail: str) -> str:
    """Return the allowlisted Trino/Athena error NAME when it is the LEADING
    engine error code, else ``""``.

    Uses the shared anchored parser (:func:`_leading_engine_code`) so it agrees
    with :func:`_is_scan_limit` on what "the engine's error code" is: a single
    all-upper-snake token at the start, allowing ONE optional
    ``Athena query <state>:`` wrapper (states derived from _DONE). The captured
    token is accepted ONLY as an exact allowlist member — a quoted data value, a
    larger identifier that merely contains an allowlisted name, or a complete
    ``Athena query failed: SYNTAX_ERROR:`` sequence embedded LATER in another
    message never matches, because the parser is anchored at the string start.
    """
    code = _leading_engine_code(detail)
    return code if code in _INVALID_REQUEST_ERROR_NAMES else ""


# A query StartQueryExecution itself refuses: the request was rejected before
# any query ran, so its message is the engine's parser or analyzer diagnostic
# about the SQL text. Only a diagnostic from the closed set in
# ``athena_diagnostics`` is read, and only its id, checked tokens and position
# reach the agent, never the message. A FAILED state's StateChangeReason (which
# can quote lake values) keeps the fixed-name mapping.
_SUBMIT_OPERATION = "StartQueryExecution"
_SUBMIT_REJECTION_CODE = "InvalidRequestException"


def _submit_rejection_reading(exc: Exception):
    """The ``athena_diagnostics.Reading`` when ``exc`` is StartQueryExecution
    refusing the SQL with a known parser or analyzer diagnostic, else ``None``.

    Only a botocore error on that operation with code
    ``InvalidRequestException`` qualifies, and only when its ``Error.Message``
    (never ``str(exc)``) matches one of ``athena_diagnostics.DIAGNOSTICS``. A
    permission, workgroup or output-location refusal, unknown text, or a
    ``line L:C:`` position with no known diagnostic keeps today's
    ``query_unavailable`` mapping and is never reported as something to fix in
    the SQL."""
    if getattr(exc, "operation_name", None) != _SUBMIT_OPERATION:
        return None
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    if not isinstance(error, dict) or error.get("Code") != _SUBMIT_REJECTION_CODE:
        return None
    from . import athena_diagnostics
    return athena_diagnostics.read(error.get("Message"), _INVALID_REQUEST_ERROR_NAMES)


def _submit_rejection_payload(exc: Exception) -> dict | None:
    """The agent-facing ``invalid_request`` for a known submit-time SQL
    diagnostic: its id, checked tokens, the fixed sentence built from them,
    the position and the fixed guidance."""
    reading = _submit_rejection_reading(exc)
    if reading is None:
        return None
    payload = {"error": "invalid_request", "error_type": "invalid_request"}
    if reading.name:
        payload["error_name"] = reading.name
    payload.update(diagnostic=reading.diagnostic, diagnostic_tokens=dict(reading.tokens),
                   diagnostic_text=reading.text)
    if reading.position:
        payload["position"] = dict(reading.position)
    payload["guidance"] = _INVALID_REQUEST_GUIDANCE
    return payload



def _partition_hint_from_tables(athena, cfg: dict, sql: str) -> str:
    """Best-effort scan-cost recovery hint: the PARTITION columns of the referenced
    table(s), named as facts, and the narrowing that keeps the result the one
    asked for (the question's own time window; a partition bound the question
    did not ask for leaves rows out). A LIMIT bounds only the rows returned, so
    it is named as no help. Returns ``""`` only on an introspection failure
    (never masks the real error)."""
    from .engine_names import Withheld
    names = Withheld()
    try:
        parts: list[str] = []
        for table in _referenced_tables(sql):
            try:
                cols = _introspect_columns(athena, cfg, table)
            except Exception:  # noqa: BLE001 - per-table best-effort  # nosec B112 - best-effort loop: skip the failed item and continue
                continue
            pcols = [n for (n, _t, extra) in cols if "partition key" in extra.lower()]
            if pcols:
                shown = ", ".join(names.show(c) for c in pcols[:_MAX_ENRICH_NAMES])
                parts.append(f"{names.show(table, 'table')}: {shown}")
        known = (" | partition columns — " + " ; ".join(parts)
                 + (f".{names.sentence()}" if names.count else "")) if parts else ""
        return known + _NARROW_WINDOW_HINT + " | a " + _LIMIT_BOUNDS_ROWS
    except Exception:  # noqa: BLE001 - enrichment must never mask the real error
        return ""


_GENERIC_CAST_FIX = (
    "CAST operands to a common type before comparing (e.g. CAST(col AS varchar))"
)
_TEMPORAL_CAST_FIX = (
    "compare a temporal column to a TYPED literal — TIMESTAMP '2026-01-31 "
    "00:00:00' / DATE '2026-01-31', or date_parse / from_iso8601_timestamp — and "
    "use a half-open range for one day; do NOT cast the COLUMN to varchar (it "
    "defeats partition pruning)"
)


def _segment_quoted_column(detail: str, available: list) -> tuple[str, str] | None:
    """``(missing, column)`` when the COLUMN_NOT_FOUND name has dots and IS
    the available ``column`` (exact, case-insensitive), else ``None``."""
    match = re.search(r"Column '([^']+)' cannot be resolved", detail)
    if not match:
        return None
    missing = match.group(1).strip('"')
    if "." not in missing:
        return None
    column = next((col for col in available if col.lower() == missing.lower()), None)
    return (missing, column) if column is not None else None


def _segment_quoting_hint(detail: str, available: list) -> str:
    """When the 'missing' column IS an available dotted column, the name was
    right and the QUOTING was wrong (segment-split as ``"a"."b"`` — parsed as
    qualified access instead of the one physical name).

    Observed live: the agent referenced a real dotted column segment-quoted;
    the enrichment then listed the SAME name as available, which reads as
    "the column exists" and invites an identical retry. Say what actually
    happened. Deterministic: exact (case-insensitive) membership only."""
    found = _segment_quoted_column(detail, available)
    missing = found[0] if found else ""
    if found:
        return (f' | NOTE: "{missing}" IS a real column — the dots are part '
                f'of its NAME. It failed because it was referenced as '
                f'segment-quoted qualified access ("a"."b"). Reference it as '
                f'ONE double-quoted identifier instead: "{missing}".')
    return ""


def _underscored_variant_of(detail: str, available: list) -> tuple[str, str] | None:
    """``(missing, column)`` when the COLUMN_NOT_FOUND name is the available
    dotted ``column`` with its dots written as underscores (exact), else
    ``None``."""
    match = re.search(r"Column '([^']+)' cannot be resolved", detail)
    if not match:
        return None
    missing = match.group(1).rsplit(".", 1)[-1].strip('"').lower()
    for col in available:
        if col.replace(".", "_").lower() == missing and "." in col:
            return missing, col
    return None


def _underscored_variant_hint(sql: str, detail: str, available: list) -> str:
    """When the 'missing' column is an UNDERSCORED variant of a real dotted
    column, name the real column exactly.

    Observed live: with the dotted-name guidance available, an agent still
    normalized ``a.b.c`` to ``a_b_c`` (unquoted) — a name that does not exist.
    The generic available-columns list contains the real dotted name, but the
    agent needs the explicit connection to stop retrying variants. Deterministic:
    fires only when dots->underscores over an available column equals the
    missing name exactly."""
    found = _underscored_variant_of(detail, available)
    if found is None:
        return ""
    missing, col = found
    return (f' | NOTE: no column named \'{missing}\' exists; the real '
            f'column is "{col}" (dots are part of the name): reference '
            f'it as that ONE double-quoted identifier, never an '
            f'underscored variant.')


def _self_alias_name(sql: str, detail: str) -> str:
    """The COLUMN_NOT_FOUND name (its last dotted part) when it is an alias
    (``AS <name>``) the SELECT list of the failing reference's own query
    block defines (``sql_alias_scope.same_scope_alias``: comments and
    literals ignored, the engine's position used when it falls on a
    reference), else ``""``."""
    from .sql_alias_scope import same_scope_alias
    match = re.search(r"(?:line (\d+):(\d+): )?Column '([^']+)' cannot be resolved", detail)
    if not match:
        return ""
    name = match.group(3).rsplit(".", 1)[-1]
    position = (int(match.group(1)), int(match.group(2))) if match.group(1) else None
    return name if same_scope_alias(sql, name, position) else ""


def _self_alias_hint(sql: str, detail: str) -> str:
    """When the 'missing' column is an alias the SAME query defines, say so.

    Observed live: a query defined ``CASE ... END AS station_family`` and then
    referenced ``station_family`` in another expression at the same SELECT
    level — COLUMN_NOT_FOUND three times in a row, because the generic
    available-columns hint can never help: the "missing column" is the query's
    OWN alias, invisible at its own scope on this engine. Name the actual
    problem and the two mechanical fixes."""
    name = _self_alias_name(sql, detail)
    if not name:
        return ""
    return (f" | NOTE: '{name}' is an alias THIS query defines — an alias is "  # nosec B608 - a prose note to the model, not a query
            "not visible in the same SELECT scope on this engine. Either "
            "repeat the full expression where you reference it, or compute "
            "the alias in a subquery/CTE and reference it from the OUTER "
            "query.")


def _enrich_column_not_found(athena, cfg, sql: str, detail: str, db: str,
                             columns_out: list | None = None) -> str:
    """Append the REAL columns of each referenced table (bounded, best-effort).

    Extracted from ``_enrich_failure_detail`` to keep that dispatcher under the
    maintainability limit. When the missing column is the query's own alias,
    the self-alias note leads — the available-columns list cannot contain an
    alias, so without the note the agent retries the identical mistake.
    ``columns_out``, when given, receives every column name read (the full
    lists, not the display cap), so the failure can carry them.
    """
    alias_hint = _self_alias_hint(sql, detail)
    if alias_hint:
        detail = f"{detail}{alias_hint}"
    hints: list[str] = []
    for table in _referenced_tables(sql):
        try:
            _safe_sql_literal(table)  # fail-closed identifier validation only
        except ValueError:
            continue
        # Shared introspection (catalog metadata API first, TTL-cached) — the
        # column list the agent just described is the one we need here, and
        # this enrichment must never itself queue behind on-demand capacity.
        try:
            all_cols = [name for name, _type, _extra in _introspect_columns(athena, cfg, table)]
        except Exception:  # noqa: BLE001 - enrichment is best-effort; keep the base error
            all_cols = []
        if columns_out is not None:
            columns_out.extend(all_cols)
        # Underscored-variant + segment-quoting checks run over the FULL list
        # (the real dotted column may sit beyond the display cap); display
        # stays bounded.
        variant_hint = _underscored_variant_hint(sql, detail, all_cols)
        if variant_hint:
            detail = f"{detail}{variant_hint}"
        quoting_hint = _segment_quoting_hint(detail, all_cols)
        if quoting_hint:
            detail = f"{detail}{quoting_hint}"
        cols = all_cols[:_MAX_ENRICH_NAMES]
        if cols:
            hints.append(f"{table}: {', '.join(cols)}")
    if hints:
        return f"{detail} | available columns — {' ; '.join(hints)}"
    return detail


def _enrich_type_mismatch(athena, cfg, sql: str, detail: str) -> str:
    """Surface referenced columns WITH their types plus the RIGHT cast recipe.

    A temporal-vs-string mismatch is the dominant real case (measured live: 8 of
    10 failed queries were "timestamp(6) <= varchar"), and the generic
    "CAST to a common type" advice invites CAST(col AS varchar) — which compiles,
    silently defeats partition pruning, and scans far more data.
    """
    hints = []
    for table in _referenced_tables(sql):
        try:
            cols = _introspect_columns(athena, cfg, table)
        except Exception:  # noqa: BLE001 - per-table best-effort  # nosec B112 - best-effort loop: skip the failed item and continue
            continue
        if cols:
            typed = ", ".join(f"{n} {t}" for (n, t, _e) in cols[:_MAX_ENRICH_NAMES])
            hints.append(f"{table}: {typed}")
    if not hints:
        return f"{detail} | {_GENERIC_CAST_FIX}"
    joined = " ".join(hints).lower()
    temporal = "timestamp" in joined or "date" in joined
    fix = _TEMPORAL_CAST_FIX if temporal else _GENERIC_CAST_FIX
    return f"{detail} | column types — {' ; '.join(hints)} | {fix}"


def _enrich_failure_detail(athena, cfg: dict, sql: str, detail: str,
                           columns_out: list | None = None) -> str:
    """Append REAL schema facts to a self-correctable Athena error.

    Turns a dead-end error into a self-correcting hint, generically:
      - COLUMN_NOT_FOUND → the real columns of the referenced table(s);
      - TABLE_NOT_FOUND  → the real tables in the database;
      - TYPE_MISMATCH    → the referenced table(s)' column types + a cast-to-compare hint;
      - scan/bytes limit → the referenced table(s)' PARTITION columns + a filter+LIMIT hint.
    Strictly best-effort and bounded: any failure in the lookup returns ``detail``
    UNCHANGED, and unrecognized failures are left as-is. ``columns_out``
    receives the column names a COLUMN_NOT_FOUND lookup read.
    """
    upper = detail.upper()
    known = (
        "COLUMN_NOT_FOUND" in upper
        or "TABLE_NOT_FOUND" in upper
        or "TYPE_MISMATCH" in upper
        or _is_scan_limit(upper)
    )
    if not known:
        return detail
    try:
        db = _safe_sql_literal(cfg["database"])
        if "TABLE_NOT_FOUND" in upper:
            tables = _first_column_values(
                athena,
                cfg,
                "SELECT table_name FROM information_schema.tables "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
                f"WHERE table_schema = '{db}' ORDER BY table_name",
            )[:_MAX_ENRICH_NAMES]
            if tables:
                return f"{detail} | available tables in {cfg['database']}: {', '.join(tables)}"
            return detail
        if "COLUMN_NOT_FOUND" in upper:
            return _enrich_column_not_found(athena, cfg, sql, detail, db, columns_out)
        if "TYPE_MISMATCH" in upper:
            return _enrich_type_mismatch(athena, cfg, sql, detail)
        # scan/bytes limit
        hint = _partition_hint_from_tables(athena, cfg, sql)
        return f"{detail}{hint}" if hint else detail
    except Exception:  # noqa: BLE001 - enrichment must never mask the real error
        return detail


def run_athena_query(
    sql: str, connection_config: dict, *, max_rows: int = MAX_ROWS,
    poll_timeout: float | None = None, client=None, _enrich: bool = True,
    source_system: str = "", _telemetry: dict | None = None, _row_budget=None,
) -> dict:
    """Execute bounded read-only Athena SQL through the tested implementation.

    ``poll_timeout=None`` (the default) resolves the tunable DATA-query bound
    via :func:`resolve_poll_timeout` (config > env > 60.0); an explicit value
    (the internal introspection paths) is honored as-is once it passes the
    fail-closed finiteness gate (:func:`_coerce_timeout` — inf/nan raise
    :class:`AthenaConfigError` rather than hanging the poll loop).

    ``_row_budget`` (internal, unattributed reads only: the exact split's
    grouped pieces) pages every row up to ``max_rows``, each row charged to
    that budget (``width(n)`` and ``take(row)``, which raise when it is
    spent), because those rows are merged before the model-facing bound
    applies; an attributed call with it raises.
    """
    if _row_budget is not None and source_system:
        raise ValueError("whole-row reads are internal: a model-facing result stays bounded")
    return _run_athena_query_impl(
        sql, connection_config, max_rows=max_rows, poll_timeout=poll_timeout,
        client=client, _enrich=_enrich, source_system=source_system,
        _telemetry=_telemetry, _row_budget=_row_budget,
    )


def run_athena_query_exact(
    sql: str, connection_config: dict, *, source_system: str = "", client=None,
    glue_client=None,
) -> dict:
    """:func:`run_athena_query`, and when the ENGINE refuses the query for
    scan size, the exact answer computed from partition pieces
    (:mod:`agents.athena_exact_split`) instead of the refusal.

    A query the split cannot answer exactly (its shape, the table, a piece that
    failed, a cap) re-raises the ORIGINAL :class:`AthenaEngineScanLimitError`
    with ``exact_split`` saying why, so the honest error is unchanged apart
    from that reason. Every other outcome is exactly ``run_athena_query``'s.
    """
    # Only the arguments a caller set, so the one-shot call is exactly the
    # plain connector call it replaces.
    kwargs = {"source_system": source_system} if source_system else {}
    if client is not None:
        kwargs["client"] = client
    try:
        return _run_once_retried(sql, connection_config, kwargs)
    except AthenaEngineScanLimitError as refusal:
        from . import athena_exact_split
        # The client that ran the refused query: the split builds none.
        client = client or refusal.athena_client
        refusal.athena_client = None
        # The pieces read the query as it ran: its not-empty conjuncts written
        # so the engine skips NULL pages (``_written_for_null_pages``). The
        # result records the query as given (``_as_given``).
        given = sql
        sql = _split_sql(sql, connection_config, source_system, client)
        try:
            result = athena_exact_split.compute_exact(
                sql, connection_config, refusal=refusal, source_system=source_system,
                client=client, glue_client=glue_client,
            )
        except athena_exact_split.ExactSplitRefused as why:
            refusal.exact_split = why.to_dict()
            raise refusal from None
        except Exception as fault:  # noqa: BLE001 - a split fault never replaces the honest refusal
            from .chat_client import _redact_digits
            logger.warning("athena exact split failed %s: %s", type(fault).__name__,
                           _redact_digits(fault, limit=240))
            refusal.exact_split = athena_exact_split.ExactSplitRefused(
                "split_failed", "the split could not be planned").to_dict()
            raise refusal from None
    result = _split_with_storage_bound(result, sql, connection_config, source_system, client)
    return _as_given(result, given, sql)


#: Where the connector reports the statement the engine received for an
#: attributed query when it differs from the recorded ``_adt_executed_query``
#: (the connector wrote a not-empty conjunct so the engine skips NULL pages,
#: ``sql_empty_string``): ``{recorded query: engine statement}``, bound by the
#: caller with :func:`engine_statements`, one sink per tool call. A later run
#: of the same record that the engine receives as recorded drops the earlier
#: report. The result itself carries only the query as given, so its payload,
#: and every reader and byte budget of it, is what it was without the writing;
#: the two statements return the same rows.
_ENGINE_STATEMENTS: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "adt_athena_engine_statements", default=None)
#: The most statements one bound sink keeps.
MAX_ENGINE_STATEMENTS = 64


@contextlib.contextmanager
def engine_statements(sink: dict):
    """Report into ``sink`` each ``{recorded: engine}`` pair the connector
    runs inside the block (:data:`_ENGINE_STATEMENTS`)."""
    token = _ENGINE_STATEMENTS.set(sink)
    try:
        yield sink
    finally:
        _ENGINE_STATEMENTS.reset(token)


def _report_engine_statement(recorded: str, engine_sql: str) -> None:
    sink = _ENGINE_STATEMENTS.get()
    if not isinstance(sink, dict):
        return
    if recorded == engine_sql:
        sink.pop(recorded, None)
    elif recorded in sink or len(sink) < MAX_ENGINE_STATEMENTS:
        sink[recorded] = engine_sql


def _recorded(given: str, sql: str, capped_sql: str, max_rows: int) -> str:
    """The ``_adt_executed_query`` of a query given as ``given`` that the
    engine runs as ``capped_sql``: the query as given with the same cap; the
    engine statement is reported (:func:`_report_engine_statement`)."""
    recorded = capped_sql if sql == given else apply_row_cap(given, max_rows)[0]
    _report_engine_statement(recorded, capped_sql)
    return recorded


def _as_given(result, given: str, sql: str):
    """An exact split's ``result`` of ``sql``, the query given as ``given``,
    recording the query as given the way the split records any query
    (``apply_row_cap`` of it at ``MAX_ROWS``); the statement it planned is
    reported."""
    from .sql_safety import MAX_ROWS
    if not isinstance(result, dict) or not isinstance(result.get("_adt_executed_query"), str):
        return result
    if sql == given:
        _report_engine_statement(result["_adt_executed_query"], result["_adt_executed_query"])
        return result
    recorded = apply_row_cap(given, MAX_ROWS)[0]
    _report_engine_statement(recorded, result["_adt_executed_query"])
    return {**result, "_adt_executed_query": recorded}


def _split_sql(sql: str, connection_config: dict, source_system: str, client) -> str:
    """The query an exact split plans and runs its pieces from: an attributed
    query as the connector ran it (``_written_for_null_pages``). Fail-soft:
    the query as written stands."""
    if not source_system or client is None:
        return sql
    try:
        return _written_for_null_pages(client, _resolve_config(connection_config), sql)
    except Exception as exc:  # noqa: BLE001 - the split always has the query as written
        logger.info("split query left as written (%s)", type(exc).__name__)
        return sql


def _split_with_storage_bound(result: dict, sql: str, connection_config: dict,
                              source_system: str, client) -> dict:
    """A split result carrying the storage bound the model's query ran with,
    as the plain result would (the check already passed for this SQL, so
    this reads only cached catalog facts). Fail-soft: the result stands."""
    if not source_system:
        return result
    try:
        cfg = _resolve_config(connection_config)
        athena = client or boto3_client_for("athena", cfg, default_region=cfg["region"])
        return _with_storage_bound(result, _storage_bound_check(athena, cfg, sql, source_system))
    except Exception as exc:  # noqa: BLE001 - the exact result never depends on the label
        logger.info("storage bound label skipped on a split result (%s)", type(exc).__name__)
        return result


def _run_once_retried(sql: str, connection_config: dict, kwargs: dict) -> dict:
    """:func:`run_athena_query`, run once more as a fresh query when its poll
    window ends with the query unfinished (measured: a poll saw QUEUED for
    its whole window while the engine had run the query in seconds). The
    poll already stopped the first query; a second timeout is raised. A retry
    the engine refuses for scan size carries the first attempt's bytes
    (``timed_out_attempt_bytes``) into the exact split's byte budget.

    Inside a model tool call's deadline (``athena_call_time``) the retry runs
    only when it fits before the deadline. The stopped attempt credits only
    the engine waiting its own poll recorded (``athena_call_time.PollWatch``),
    never its poll window as such: a query stopped while RUNNING was working."""
    from . import athena_call_time
    try:
        return run_athena_query(sql, connection_config, **kwargs)
    except AthenaQueryTimeoutError as first:
        if not athena_call_time.retry_fits():
            raise
        logger.info("athena query %s did not finish in its poll window; retrying once",
                    first.query_id)
        scanned = first.scanned_bytes
    try:
        return run_athena_query(sql, connection_config, **kwargs)
    except AthenaQueryTimeoutError as second:
        second.retried = True
        raise
    except AthenaEngineScanLimitError as refusal:
        refusal.timed_out_attempt_bytes = (
            int(scanned) if isinstance(scanned, (int, float)) and not isinstance(scanned, bool) else 0)
        raise


def _start_athena_query(athena, config: dict, capped_sql: str,
                        client_request_token: str | None = None) -> str:
    """Submit ``capped_sql``; ``client_request_token`` (the exact split's
    metadata reads) makes the submission idempotent: a second one with the
    same token returns the same query and starts no other."""
    context = {"Database": config["database"]}
    if config["catalog"]:
        context["Catalog"] = config["catalog"]
    kwargs = {
        "QueryString": capped_sql,
        "QueryExecutionContext": context,
        "WorkGroup": config["workgroup"],
    }
    if config["output_location"]:
        kwargs["ResultConfiguration"] = {"OutputLocation": config["output_location"]}
    if client_request_token is not None:
        kwargs["ClientRequestToken"] = client_request_token
    return athena.start_query_execution(**kwargs)["QueryExecutionId"]


def _raise_scan_budget(athena, query_id: str, scanned: int, budget: int) -> None:
    try:
        athena.stop_query_execution(QueryExecutionId=query_id)
    except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
        pass
    guidance = [
        f"This query scanned ~{scanned} bytes, over the configured budget of {budget} bytes.",
        "This is a transient, narrowable limit — a narrower query can succeed; "
        "never report it as a permanent data-integrity failure.",
        # Metadata-safe wording: this path has NO partition knowledge, so it
        # must never advise partition predicates that may not exist.
        "Narrow the time range to reduce the scan. A " + _LIMIT_BOUNDS_ROWS + ".",
        f"Raise the per-system scan budget or the {_ENV_MAX_SCANNED_BYTES} override.",
    ]
    raise AthenaScanBudgetError(
        f"Athena query exceeded its scanned-bytes budget ({scanned} > {budget}); "
        "narrow the time range (a " + _LIMIT_BOUNDS_ROWS + ").",
        scanned_bytes=scanned, budget_bytes=budget, guidance=guidance,
    )


def _safe_statistics(execution: dict) -> dict:
    allowed = {
        "DataScannedInBytes", "EngineExecutionTimeInMillis",
        "QueryQueueTimeInMillis", "QueryPlanningTimeInMillis",
        "ServicePreProcessingTimeInMillis", "ServiceProcessingTimeInMillis",
        "TotalExecutionTimeInMillis",
    }
    statistics = execution.get("Statistics", {})
    return {key: value for key, value in statistics.items()
            if key in allowed and isinstance(value, (int, float))}


def _poll_athena_query(athena, query_id: str, timeout: float,
                       max_scanned: int | None) -> tuple[str, str, dict]:
    """Poll to a terminal state; returns ``(state, reason, safe_statistics)``.

    The third element is the LAST observed :func:`_safe_statistics` projection
    (small numeric metadata only) so a successful caller can attach headroom
    telemetry without an extra API call. Inside a model tool call
    (``athena_call_time``) the window is capped by the call's deadline and
    the time Athena held the query is recorded (``athena_call_time.PollWatch``),
    from the statistics read once more after a stop (:func:`_statistics_after_stop`).
    """
    from . import athena_call_time
    timeout = athena_call_time.capped_poll_window(timeout)
    watch = athena_call_time.PollWatch()
    deadline = time.monotonic() + timeout
    state, last_statistics, terminal = "QUEUED", {}, None
    try:
        while time.monotonic() < deadline:
            execution = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
            last_statistics = _safe_statistics(execution)
            scanned = execution.get("Statistics", {}).get("DataScannedInBytes")
            if max_scanned is not None and scanned is not None and scanned > max_scanned:
                _raise_scan_budget(athena, query_id, scanned, max_scanned)
            status = execution["Status"]
            state = status["State"]
            watch.saw(state)
            if state in _DONE:
                return state, status.get("StateChangeReason", ""), last_statistics
            time.sleep(_DEFAULT_POLL_INTERVAL)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        try:
            athena.stop_query_execution(QueryExecutionId=query_id)
        except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
            pass
        watch.stopped()
        terminal = _statistics_after_stop(athena, query_id, watch)
    finally:
        watch.end(last_statistics, state, terminal)
    raise AthenaQueryTimeoutError(
        query_id=query_id, last_state=state, statistics=last_statistics,
        poll_timeout=timeout,
    )


def _statistics_after_stop(athena, query_id: str, watch) -> dict | None:
    """The statistics of a query just stopped, read at most
    ``athena_call_time.TERMINAL_READS`` times until its state is terminal
    (Athena reports the time it held a stopped query only then): the newest
    read, or ``None`` when none could be read. A stop still not terminal (or
    unreadable) then is kept on the watch (``PollWatch.defer``), so the
    question's budget reads it again before its next check
    (``athena_call_time.reconcile``). Only inside a recording
    (``watch.recording``): otherwise nothing is read, as before."""
    from . import athena_call_time
    if not watch.recording:
        return None
    found = None
    for attempt in range(athena_call_time.TERMINAL_READS):
        if attempt:
            time.sleep(athena_call_time.TERMINAL_READ_PAUSE_SECONDS)  # bounded: one short pause  # nosemgrep: arbitrary-sleep
        try:
            terminal, found = _read_stopped(athena, query_id)
        except Exception:  # noqa: BLE001 - an unreadable stop credits what the polls saw
            break
        if terminal:
            return found
    watch.defer(lambda: _read_stopped(athena, query_id))
    return found


def _read_stopped(athena, query_id: str) -> tuple[bool, dict]:
    """``(terminal, safe statistics)`` of one read of a stopped query."""
    execution = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
    return (execution.get("Status") or {}).get("State") in _DONE, _safe_statistics(execution)


class AthenaColumnNotResolved(RuntimeError):
    """A query the engine failed with COLUMN_NOT_FOUND. The message is the
    enriched one, as before (it is logged, never shown to the agent).
    ``reason`` is the engine's own reason, ``sql`` the SQL that ran and
    ``columns`` the referenced tables' column names, so :func:`_tool_error`
    can name the cause (:func:`_column_cause`)."""

    def __init__(self, message: str, *, reason: str, sql: str, columns=()):
        super().__init__(message)
        self.reason = reason
        self.sql = sql
        self.columns = tuple(columns)


def _raise_failed_query(athena, config: dict, sql: str, state: str, reason: str,
                        enrich: bool, statistics: dict | None = None) -> None:
    detail = reason or "no reason reported"
    upper = detail.upper()
    # Named SCHEMA errors win FIRST: a COLUMN_NOT_FOUND / TABLE_NOT_FOUND whose
    # message merely quotes the words "bytes scanned" must keep its schema
    # recovery path (enriched RuntimeError -> _tool_error schema_mismatch), never
    # be reclassified as a scan limit.
    schema_named = "COLUMN_NOT_FOUND" in upper or "TABLE_NOT_FOUND" in upper
    # An ENGINE scan-limit rejection (an explicit per-query BytesScannedCutoffPerQuery
    # diagnostic) means the system ANSWERED and refused a too-broad query. Raise it
    # as the structured, retryable scan_budget_exceeded shape so the tool wrapper
    # never flattens it to query_unavailable (which read to the agent as a service
    # outage) and the computed partition hint is preserved.
    if not schema_named and _is_scan_limit(detail):
        scanned = (statistics or {}).get("DataScannedInBytes")
        partition_hint = ""
        if enrich:
            partition_hint = _partition_hint_from_tables(athena, config, sql)
        refusal = AthenaEngineScanLimitError(
            f"Athena query {state.lower()}: the engine scan limit was exceeded",
            limit_source="engine",
            scanned_bytes=scanned if isinstance(scanned, int) and not isinstance(scanned, bool) else None,
            partition_hint=partition_hint,
        )
        refusal.athena_client = athena      # the exact split reuses it
        raise refusal
    columns: list = []
    if enrich:
        detail = _enrich_failure_detail(athena, config, sql, detail, columns)
    message = f"Athena query {state.lower()}: {detail}"
    if "COLUMN_NOT_FOUND" in upper:
        raise AthenaColumnNotResolved(message, reason=reason or "", sql=sql, columns=columns)
    raise RuntimeError(message)


def _athena_result(athena, query_id: str, max_rows: int, was_capped: bool,
                   source_system: str, queried_at, capped_sql: str,
                   types_out: list | None = None) -> dict:
    result = (_collect_results(athena, query_id, max_rows) if types_out is None
              else _collect_results(athena, query_id, max_rows, types_out))
    if not source_system:
        return result
    if was_capped and result.get("row_count", 0) >= max_rows:
        from .response_integrity_query import mark_rows_cut
        # The injected LIMIT was reached, so later rows may exist even when
        # the collector only saw a column cap: the note must name both.
        result = {**mark_rows_cut(result), "capped_at_limit": True}
    enriched = attach_source_metadata(
        result, boundary="athena", system_id=source_system, queried_at=queried_at,
    )
    return {**enriched, "_adt_executed_query": capped_sql}


def _attach_headroom(result: dict, statistics: dict, budget: int | None) -> dict:
    """Attach scanned-bytes headroom telemetry to a SUCCESSFUL query result.

    Small numeric metadata only, added CONDITIONALLY: the caller applies it
    ONLY on the attributed agent-facing path (``source_system`` set), so
    internal introspection reads and legacy unattributed shapes stay
    byte-identical; nothing is added when the engine reported no
    ``DataScannedInBytes``; and the budget fields appear only when a scan
    budget is actually configured. Error paths never reach here (they already
    carry ``scanned_bytes`` on their structured errors).
    """
    scanned = statistics.get("DataScannedInBytes")
    if isinstance(scanned, bool) or not isinstance(scanned, (int, float)):
        return result
    out = {**result, "scanned_bytes": int(scanned)}
    if budget is not None:
        out["budget_bytes"] = int(budget)
        out["budget_used_pct"] = int(round(scanned * 100 / budget))
    return out


def _known_column_types(athena, cfg: dict, table: str) -> dict:
    """``{column: type}`` (lowercased) from the schema cache or the catalog
    metadata read, ``{}`` when neither answers. Never runs an Athena query:
    a type it cannot learn this way is simply unknown."""
    key = _schema_cache_key(cfg, table, "introspect")
    cached = _schema_cached(key)
    if cached is not None:
        rows = cached[0]
    else:
        rows = _table_columns_via_metadata_api(athena, cfg, table)
        if rows is None:
            return {}
        _schema_store(key, (rows, True))  # the catalog returns the WHOLE list
    return {str(name).lower(): str(dtype).lower() for name, dtype, _extra in rows}


def _known_flat_column_types(athena, cfg: dict, table: str) -> dict:
    """``{column: type}`` (lowercased) like :func:`_known_column_types`, but
    with nested ``row(...)`` structs FLATTENED to dotted leaf paths
    (``flatten_athena_columns``), so a struct field is a key in its own right
    (``quality_data.defect_type``). The record-own-time check reads this, which
    needs the sibling leaves the catalog delivers as one ``row(...)`` column;
    ``_known_column_types`` and its other callers keep the unflattened map.
    Cached per table under its own key; never runs an Athena query."""
    flat_key = _schema_cache_key(cfg, table, "introspect_flat")
    cached = _schema_cached(flat_key)
    if cached is not None:
        return cached
    rows_key = _schema_cache_key(cfg, table, "introspect")
    rows_cached = _schema_cached(rows_key)
    if rows_cached is not None:
        rows = rows_cached[0]
    else:
        rows = _table_columns_via_metadata_api(athena, cfg, table)
        if rows is None:
            return {}
        _schema_store(rows_key, (rows, True))
    flat = {col["name"].lower(): str(col["data_type"]).lower()
            for col in flatten_athena_columns(rows)}
    _schema_store(flat_key, flat)
    return flat


def _catalog_columns(athena, cfg: dict):
    """``table_columns(name parts)`` for the SQL checks that tie a reference to
    its base table: the catalogued ``{column: type}`` of a table in the
    system's own catalog and database (cached), ``None`` when unknown."""
    database = str(cfg["database"]).lower()
    catalog = str(cfg.get("catalog") or "AwsDataCatalog").lower()

    def table_columns(parts: tuple):
        if not 1 <= len(parts) <= 3 or (len(parts) >= 2 and parts[-2] != database) \
                or (len(parts) == 3 and parts[0] != catalog):
            return None
        return _known_column_types(athena, cfg, parts[-1]) or None
    return table_columns


def _refuse_midnight_comparisons(athena, cfg: dict, sql: str) -> None:
    """Refuse, before it runs, a model-written query that compares a TIMESTAMP
    column to a date at midnight (``sql_time_hygiene``, which ties each
    reference to its relation by SQL scope). A base table's columns come from
    the catalog; a table in another catalog or database is unknown, and so is
    any column the catalog read does not answer. No candidate comparison reads
    no catalog."""
    from . import sql_time_hygiene
    if not sql_time_hygiene.has_candidates(sql):
        return
    sql_time_hygiene.check(sql, _catalog_columns(athena, cfg))


def _written_for_null_pages(athena, cfg: dict, sql: str) -> str:
    """``sql`` with each top-level ``<string column> <> ''`` WHERE conjunct
    written as ``(col IS NOT NULL AND length(col) > 0)``, the same filter the
    engine can skip NULL pages with (``sql_empty_string``). No candidate reads
    no catalog; a failed catalog read leaves the query as written."""
    from . import sql_empty_string
    if not sql_empty_string.has_candidates(sql):
        return sql
    try:
        return sql_empty_string.rewrite(sql, _catalog_columns(athena, cfg))
    except Exception as exc:  # noqa: BLE001 - the query as written always stands
        logger.info("not-empty conjunct left as written (%s)", type(exc).__name__)
        return sql


#: An Iceberg table's partition fields, read once from ``$partitions``:
#: ``{table key: (monotonic read time, fields)}``. A read that found fields
#: stands for the process lifetime (a table's partition spec does not change
#: under a running agent); an empty or failed read is tried again after
#: :data:`_PARTITION_FIELDS_RETRY_SECONDS`.
_PARTITION_FIELDS_CACHE: dict[tuple, tuple[float, list[str]]] = {}
_PARTITION_FIELDS_RETRY_SECONDS = 300.0


def _cached_partition_fields(athena, cfg: dict, table: str) -> list[str]:
    key = _table_format_key(cfg, table)
    cached = _PARTITION_FIELDS_CACHE.get(key)
    if cached and (cached[1] or time.monotonic() - cached[0] < _PARTITION_FIELDS_RETRY_SECONDS):
        return list(cached[1])
    fields = hidden_partition_fields(table, cfg, client=athena) or []
    _PARTITION_FIELDS_CACHE[key] = (time.monotonic(), list(fields))
    return list(fields)


def _storage_columns(athena, cfg: dict, table: str, column_types: dict,
                     source_system: str) -> set[str]:
    """The columns ``table``'s storage is partitioned on, from what the
    connector already read: the Hive partition keys in the cached catalog
    read (``_known_column_types`` just made it, or the scan guard's partition
    lookup), the partition sources the system's baked schema names
    (``storage_bounds``), and, for an Iceberg table whose baked schema names
    none, one cached ``$partitions`` read. No other query runs."""
    from . import storage_bound_rule, storage_bounds
    cached = _schema_cached(_schema_cache_key(cfg, table, "introspect"))
    rows = cached[0] if cached else []
    out = {str(name).lower() for name, _type, extra in rows
           if _INTROSPECTION_PARTITION_MARKER in str(extra).lower()}
    key = (cfg["region"], cfg["catalog"], cfg["database"], cfg["role_arn"], table)
    out |= {str(c.get("name")).lower() for c in _PARTITION_COLUMNS_CACHE.get(key, [])}
    baked = {str(k).lower(): v for k, v in storage_bounds.storage_map_for(source_system).items()}
    out |= {str(c).lower() for c in baked.get(table.lower(), ())}
    if table.lower() not in baked and known_hidden_partitioning(table, cfg):
        out |= storage_bound_rule.field_sources(
            _cached_partition_fields(athena, cfg, table), column_types)
    return out


def _storage_facts(athena, cfg: dict, sql: str, source_system: str, *,
                   require_bound: bool = True, include_same_column_or: bool = False):
    """``(table, bounded columns, storage columns, column types, iceberg)`` for
    a model-written query that reads one table in this database and bounds a
    column of it (``storage_bound_rule.bounded_columns``), else ``None``. Only
    a query that bounds a column reads the catalog, unless ``require_bound``
    is False (a rule that reads bounds the top-level AND does not hold, such
    as one under an OR). ``include_same_column_or`` (the window-rule caller
    only) also reads a same-column OR of ranges as a bound; the connector's
    own check never passes it, so its bounds stay byte-identical."""
    from . import storage_bound_rule
    read = storage_bound_rule.bounded_columns(
        sql, include_same_column_or=include_same_column_or)
    if not read or (require_bound and not read[1]):
        return None
    parts, bounded = read
    database = str(cfg["database"]).lower()
    if len(parts) > 3 or (len(parts) >= 2 and parts[-2].lower() != database):
        return None
    table = parts[-1]
    # One synchronous catalog read (no query) gives the column types and, as a
    # side effect, the table's format; a cached describe that did not record
    # the format gets one read for it. A read that fails claims nothing and is
    # not repeated per query.
    types = _known_column_types(athena, cfg, table)
    if _table_format_key(cfg, table) not in _TABLE_FORMAT_CACHE:
        _table_columns_via_metadata_api(athena, cfg, table)
        _TABLE_FORMAT_CACHE.setdefault(_table_format_key(cfg, table), _OTHER_FORMAT)
    storage = _storage_columns(athena, cfg, table, types, source_system)
    return ".".join(parts), bounded, storage, types, known_hidden_partitioning(table, cfg)


def storage_facts(sql: str, connection_config: dict, *, source_system: str, client=None,
                  require_bound: bool = True, include_same_column_or: bool = False):
    """What the storage-bound check reads for ``sql`` (:func:`_storage_facts`),
    for a caller that applies its own rule before the query runs: the system
    agent's guarded tool, which knows the question (``storage_bound_rule
    .decide_window``, ``state_times.decide``). ``include_same_column_or`` reads
    a date window written as a same-column OR of ranges as a bound, for the
    window rules only. Catalog reads only, cached like the check's own."""
    cfg = _resolve_config(connection_config)
    athena = client or boto3_client_for("athena", cfg, default_region=cfg["region"])
    return _storage_facts(athena, cfg, sql, source_system, require_bound=require_bound,
                          include_same_column_or=include_same_column_or)


def flat_column_types(table: str, connection_config: dict, *, source_system: str,
                      client=None) -> dict:
    """The table's catalog column types with nested ``row(...)`` structs
    FLATTENED to dotted leaves (:func:`_known_flat_column_types`), for the
    record-own-time check, which must see a struct's sibling leaves the
    catalog delivers as one column. ``table`` may be a ``db.table`` spelling
    (``storage_facts``'s first field); only this database's tables are read.
    Catalog reads only, cached like the storage check's own; ``{}`` for a
    table in another database or an unreadable catalog."""
    cfg = _resolve_config(connection_config)
    parts = [p for p in str(table or "").split(".") if p]
    database = str(cfg["database"]).lower()
    if not parts or len(parts) > 3 or (len(parts) >= 2 and parts[-2].lower() != database):
        return {}
    athena = client or boto3_client_for("athena", cfg, default_region=cfg["region"])
    return _known_flat_column_types(athena, cfg, parts[-1])


def _storage_bound_check(athena, cfg: dict, sql: str, source_system: str):
    """Refuse, before it runs, a model-written query that bounds an Iceberg
    table's storage column next to a different time column, or return the
    storage bound it runs with (``storage_bound_rule.decide``, the one
    decision; :class:`StorageBoundError` is the refusal). ``None`` when the
    query bounds no column, reads another database, or bounds no storage
    column. Only a query that bounds a column reads the catalog."""
    from . import storage_bound_rule
    facts = _storage_facts(athena, cfg, sql, source_system)
    if facts is None:
        return None
    table, bounded, storage, types, iceberg = facts
    return storage_bound_rule.decide(table, bounded, storage, types, iceberg=iceberg)


def _run_athena_query_impl(
    sql: str, connection_config: dict, *, max_rows: int = MAX_ROWS,
    poll_timeout: float | None = None, client=None,
    _enrich: bool = True, source_system: str = "",
    _telemetry: dict | None = None, _row_budget=None,
) -> dict:
    """Execute one bounded read-only Athena query with exact SQL provenance."""
    config = _resolve_config(connection_config)
    if poll_timeout is None:
        poll_timeout = resolve_poll_timeout(config)
    else:
        # An explicit override BYPASSES resolve_poll_timeout, so it needs its own
        # fail-closed gate: a non-finite bound (inf/nan) would either hang the
        # poll loop forever or collapse it to a nonsense immediate timeout.
        # Positivity is deliberately NOT required here — an internal caller may
        # legitimately pass 0 for a "poll once" probe.
        poll_timeout = _coerce_timeout(
            poll_timeout, source="poll_timeout", require_positive=False,
        )
        if poll_timeout is None:  # blank override => fall back to the tunable bound
            poll_timeout = resolve_poll_timeout(config)
    queried_at = utc_now()
    ensure_select_only(sql)
    athena = client or boto3_client_for("athena", config, default_region=config["region"])
    bound, given = None, sql
    if source_system and _enrich:
        _refuse_midnight_comparisons(athena, config, sql)
        sql = _written_for_null_pages(athena, config, sql)
        bound = _storage_bound_check(athena, config, sql, source_system)
    capped_sql, was_capped = apply_row_cap(sql, max_rows)
    recorded_sql = _recorded(given, sql, capped_sql, max_rows) if source_system else capped_sql
    query_id = _start_athena_query(athena, config, capped_sql)
    budget = resolve_scan_budget(config)
    telemetry = _telemetry if isinstance(_telemetry, dict) else {}
    telemetry.update(query_id=query_id, capped_sql=capped_sql, was_capped=was_capped)
    state, reason, statistics = _poll_athena_query(
        athena, query_id, poll_timeout, budget,
    )
    telemetry["statistics"] = statistics
    if state != "SUCCEEDED":
        _raise_failed_query(athena, config, sql, state, reason, _enrich, statistics)
    types: list | None = [] if isinstance(_telemetry, dict) else None
    result = (_collect_whole_rows(athena, query_id, max_rows, types, _row_budget)
              if _row_budget is not None else
              _athena_result(athena, query_id, max_rows, was_capped, source_system, queried_at,
                             recorded_sql, types))
    telemetry["column_types"] = types or []
    if not source_system:
        return result  # internal/unattributed reads keep their exact legacy shape
    return _with_storage_bound(_attach_headroom(result, statistics, budget), bound)


def _with_storage_bound(result: dict, bound) -> dict:
    """``result`` carrying the storage bound it ran with (``storage_bound``:
    table, bounded storage columns, their WHERE conjuncts), so the answer can
    disclose it; unchanged when it ran with none."""
    if bound is None:
        return result
    return {**result, "storage_bound": bound.to_dict()}


def _column_type(info: dict) -> str:
    """The engine's type of one result column; a decimal with its declared
    precision and scale (``decimal(38,2)``), which ``Type`` alone omits."""
    name = str(info.get("Type") or "")
    precision, scale = info.get("Precision"), info.get("Scale")
    if (name.lower() == "decimal" and isinstance(precision, int) and isinstance(scale, int)
            and not isinstance(precision, bool) and not isinstance(scale, bool)):
        return f"decimal({precision},{scale})"
    return name


def _collect_results(athena, qid: str, max_rows: int, types_out: list | None = None) -> dict:
    """Page Athena rows into the shared bounded accumulator one row at a time.

    ``types_out`` (internal callers only) receives the engine's column types."""
    from .response_integrity_query import BoundedQueryAccumulator

    accumulator = None
    next_token = None
    first_page = True
    seen = 0
    truncated = False
    while seen <= max_rows and not truncated:
        kwargs = {"QueryExecutionId": qid, "MaxResults": min(1000, max_rows - seen + 2)}
        if next_token:
            kwargs["NextToken"] = next_token
        result = athena.get_query_results(**kwargs)
        result_set = result.get("ResultSet", {})
        if accumulator is None:
            meta = result_set.get("ResultSetMetadata", {}).get("ColumnInfo", [])
            raw_columns = [c.get("Name", "") for c in meta]
            accumulator = BoundedQueryAccumulator(raw_columns)
            if types_out is not None:
                types_out[:] = [_column_type(c) for c in meta]
        page_rows = result_set.get("Rows", [])
        if first_page and page_rows:
            header = [d.get("VarCharValue") for d in page_rows[0].get("Data", [])]
            if header == raw_columns:
                page_rows = page_rows[1:]
        first_page = False
        for row in page_rows:
            if seen >= max_rows:
                truncated = True
                break
            cells = [item.get("VarCharValue") for item in row.get("Data", [])]
            if not accumulator.add(cells):
                truncated = True
                break
            seen += 1
        next_token = result.get("NextToken")
        if not next_token:
            break
    if accumulator is None:
        accumulator = BoundedQueryAccumulator([])
    return accumulator.result(truncated or bool(next_token))



def _collect_whole_rows(athena, qid: str, max_rows: int, types_out: list | None, budget) -> dict:
    """Every row of a finished internal query, up to ``max_rows`` (``truncated``
    when more were there), each charged to ``budget`` as it is paged: its
    ``width`` checks the column count once, its ``take`` each row's cells
    and bytes, and either raises to stop the fetch. ``types_out`` receives
    the engine's column types."""
    columns: list[str] = []
    rows: list[list] = []
    token, first = None, True
    while len(rows) <= max_rows:
        kwargs = {"QueryExecutionId": qid, "MaxResults": 1000}
        if token:
            kwargs["NextToken"] = token
        response = athena.get_query_results(**kwargs)
        page = response.get("ResultSet", {})
        body = page.get("Rows", [])
        if first:
            meta = page.get("ResultSetMetadata", {}).get("ColumnInfo", [])
            columns = [c.get("Name", "") for c in meta]
            budget.width(len(columns))
            if types_out is not None:
                types_out[:] = [_column_type(c) for c in meta]
            if body and [d.get("VarCharValue") for d in body[0].get("Data", [])] == columns:
                body = body[1:]
        first = False
        for item in body:
            row = [d.get("VarCharValue") for d in item.get("Data", [])]
            budget.take(row)
            rows.append(row)
            if len(rows) > max_rows:
                break
        token = response.get("NextToken")
        if not token:
            break
    return {"columns": columns, "rows": rows[:max_rows], "row_count": min(len(rows), max_rows),
            "truncated": len(rows) > max_rows}


_SCHEMA_MARKERS = (
    "column_not_found", "table_not_found", "cannot be resolved",
    "column does not exist", "relation does not exist",
)
_SCHEMA_GUIDANCE = "Call athena_describe_table and retry using exact physical columns."
_ALIAS_GUIDANCE = (
    "Change only the alias reference: use its GROUP BY position or repeat the full "
    "expression. The table's column names are not the cause of this error; keep them."
)


def _tool_error(exc: Exception) -> dict:
    """Map tool failures to fixed actionable categories without raw messages.

    The underlying cause is LOGGED (class + digit-masked message — the runtime's
    OTel handler discards tracebacks, so it goes in the message) and the class
    name rides the payload as ``error_class``: structured, no raw text. Measured
    live: a ``query_unavailable`` whose cause was dropped here was unrecoverable
    from any log, and the agent narrated an adjacent scope note as the cause.

    An engine scan-limit (``AthenaEngineScanLimitError``) and a deterministic SQL
    rejection carrying a known Trino/Athena NAME are surfaced HONESTLY — the
    system answered — instead of being flattened to ``query_unavailable``. A
    rejection StartQueryExecution raises with a known parser or analyzer
    diagnostic carries that diagnostic's id and checked tokens
    (:func:`_submit_rejection_payload`), never the engine's text, and so does
    a column the engine could not resolve (:func:`_schema_mismatch_payload`).
    """
    from .sql_time_hygiene import TimestampDateComparisonError
    from .storage_bound_rule import StorageBoundError
    if isinstance(exc, (AthenaScanBudgetError, AthenaQueryTimeoutError,
                        AthenaEngineScanLimitError, TimestampDateComparisonError,
                        StorageBoundError)):
        return exc.to_dict()
    from .chat_client import _redact_digits
    logger.warning("athena tool error %s: %s", type(exc).__name__,
                   _redact_digits(exc, limit=240))
    detail = str(exc)
    lowered = detail.lower()
    if isinstance(exc, AthenaColumnNotResolved) or any(
            marker in lowered for marker in _SCHEMA_MARKERS):
        return _schema_mismatch_payload(exc)
    # Refused at submission, before anything ran: the engine's own parser
    # message is what the agent needs to change its SQL.
    submitted = _submit_rejection_payload(exc)
    if submitted is not None:
        return submitted
    # A known deterministic SQL-rejection NAME at an error-code position means
    # the engine was reachable and refused the query — honest "fix the SQL"
    # guidance, not a false outage. Boundary-aware: a name quoted in a data value
    # or buried in a larger code never matches.
    name = _invalid_request_name(detail)
    if name:
        return {
            "error": "invalid_request",
            "error_type": "invalid_request",
            "error_name": name,
            "guidance": _INVALID_REQUEST_GUIDANCE,
        }
    return {"error": "query_unavailable", "error_type": "tool_error",
            "error_class": type(exc).__name__}



def _schema_mismatch_payload(exc: Exception) -> dict:
    """The ``schema_mismatch`` payload. A column the engine could not resolve
    also carries its cause (:func:`_column_cause`): the diagnostic id, the
    checked names and the fixed sentence, and for a SELECT alias the alias
    guidance in place of the describe-table one. Anything else, or a name
    that does not pass the checks, keeps the three fixed keys."""
    payload = {"error": "schema_mismatch", "error_type": "schema_mismatch",
               "guidance": _SCHEMA_GUIDANCE}
    reading = _column_cause(exc) if isinstance(exc, AthenaColumnNotResolved) else None
    if reading is None:
        return payload
    if reading.diagnostic == "select_alias":
        payload["guidance"] = _ALIAS_GUIDANCE
    payload.update(diagnostic=reading.diagnostic, diagnostic_tokens=dict(reading.tokens),
                   diagnostic_text=reading.text)
    return payload


def _named_in_sql(name: str, sql: str) -> bool:
    """True when ``sql`` writes ``name`` as a whole dotted name outside
    comments and literals (``"a"."b"``, ``a.b`` and ``"a.b"`` are all
    ``a.b``; case-insensitive). Unreadable SQL writes nothing."""
    from .sql_alias_scope import written_names
    return any(text.lower() == name.lower() for text, _parts in written_names(sql) or ())


def _named_in_parts(column: str, sql: str) -> bool:
    """True when ``sql`` writes ``column`` other than as ONE double-quoted
    identifier (unquoted, or quoted part by part). When every reference is
    already ``"a.b"`` the quoting was right and the cause is elsewhere (a
    subquery that does not project it, for one)."""
    from .sql_alias_scope import written_names
    return any(text.lower() == column.lower() and parts > 1
               for text, parts in written_names(sql) or ())


def _column_cause(exc: AthenaColumnNotResolved):
    """The ``athena_diagnostics.Reading`` the agent is given for a column the
    engine could not resolve, or ``None``.

    Only the engine's own reason is read, against the closed
    ``column_not_resolved`` pattern; the name it captures must pass the
    token check and :func:`engine_names.printable` and be written in the
    agent's own SQL, else ``None``. The cause comes from the same checks the
    logged enrichment makes: a real dotted column referenced in parts
    (``dotted_name_split``, only when the SQL does write it in parts), an
    alias the failing reference's own SELECT defines and that is not also a
    column of the tables read (``select_alias``), or an underscored
    spelling of a real dotted column (``underscored_variant``); a catalog
    name is named only when it is printable. Otherwise the plain
    ``column_not_resolved`` reading."""
    from . import athena_diagnostics
    from .engine_names import printable
    reading = athena_diagnostics.read(exc.reason, ("COLUMN_NOT_FOUND",))
    if reading is None or reading.diagnostic != "column_not_resolved":
        return None
    name = reading.tokens.get("column", "")
    if not printable(name) or not _named_in_sql(name, exc.sql):
        return None
    split = _segment_quoted_column(exc.reason, exc.columns)
    alias = _self_alias_name(exc.sql, exc.reason)
    if alias.lower() in {column.lower() for column in exc.columns}:
        alias = ""  # also a physical column: "not a column" would be wrong
    variant = _underscored_variant_of(exc.reason, exc.columns)
    if split and printable(split[1]) and _named_in_parts(split[1], exc.sql):
        found = athena_diagnostics.cause("dotted_name_split", {"column": split[1]})
    elif alias:
        found = athena_diagnostics.cause("select_alias", {"column": alias})
    elif variant and printable(variant[1]):
        found = athena_diagnostics.cause("underscored_variant",
                                         {"column": name, "real_column": variant[1]})
    else:
        found = None
    return found or reading


def _list_tables_cached(cfg: dict, source_system: str) -> str:
    """JSON payload of ``athena_list_tables``: catalog metadata API first, then
    the information_schema query; TTL-cached like a describe."""
    cache_key = _schema_cache_key(cfg, "*", "list_tables")
    cached = _schema_cached(cache_key)
    if cached is not None:
        return cached
    athena = boto3_client_for("athena", cfg, default_region=cfg["region"])
    names = _table_names_via_metadata_api(athena, cfg)
    if names is not None:
        from .response_integrity_query import bounded_query_result
        payload = json.dumps(bounded_query_result(["table_name"], [[n] for n in names], False))
    else:
        sql = (
            "SELECT table_name FROM information_schema.tables "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
            f"WHERE table_schema = '{_safe_sql_literal(cfg['database'])}' ORDER BY table_name"
        )
        payload = json.dumps(run_athena_query(sql, cfg, source_system=source_system))
    _schema_store(cache_key, payload)
    return payload


def _describe_table_cached(cfg: dict, table_name: str, source_system: str) -> str:
    """JSON payload of ``athena_describe_table``, served from the schema cache
    when a fresh entry exists (see _SCHEMA_CACHE); failures propagate uncached."""
    cache_key = _schema_cache_key(cfg, table_name, "describe")
    cached = _schema_cached(cache_key)
    if cached is not None:
        return cached
    athena = boto3_client_for("athena", cfg, default_region=cfg["region"])
    api_rows = _table_columns_via_metadata_api(athena, cfg, table_name)
    if api_rows is not None:
        from .response_integrity_query import bounded_query_result
        payload = json.dumps(bounded_query_result(
            ["column_name", "data_type"], [[n, t] for n, t, _extra in api_rows], False))
        _schema_store(cache_key, payload)
        return payload
    sql = (
        "SELECT column_name, data_type FROM information_schema.columns "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        f"WHERE table_schema = '{_safe_sql_literal(cfg['database'])}' "
        f"AND table_name = '{_safe_sql_literal(table_name)}' ORDER BY ordinal_position"
    )
    payload = json.dumps(run_athena_query(sql, cfg, source_system=source_system))
    _schema_store(cache_key, payload)
    return payload


def make_athena_tools(connection_config: dict | None, source_system: str = ""):
    """Build three validated Athena tools for one system configuration."""
    from strands import tool

    cfg = _resolve_config(connection_config)  # eager validation

    @tool(name="athena_list_tables")
    def athena_list_tables() -> str:
        """List tables in this Athena system's Glue database. Call this first to discover tables."""
        try:
            return _list_tables_cached(cfg, source_system)
        except Exception as e:  # noqa: BLE001 - surface as tool error, not a crash
            return json.dumps(_tool_error(e))

    @tool(name="athena_describe_table")
    def athena_describe_table(table_name: str) -> str:
        """Get column names and data types for one table.
        Args:
            table_name: The table to describe (from athena_list_tables).
        """
        try:
            return _describe_table_cached(cfg, table_name, source_system)
        except Exception as e:  # noqa: BLE001 - surface as tool error, not a crash
            return json.dumps(_tool_error(e))

    @tool(name="athena_run_query")
    def athena_run_query(sql: str) -> str:
        """Run a read-only SQL SELECT against this Athena system (Presto/Trino dialect).
        Only SELECT/WITH/SHOW/DESCRIBE statements are allowed; results are row-capped.
        Bound the time column the question asks about. Bound a partition column only when
        the question asks for that column's range: it drops every row outside it and changes
        the result. A COUNT, SUM, MIN or MAX over the scan limit is computed exactly by the
        tool, so a total never needs a partition bound. The tool may decline when too little
        time is left for the pieces; then run a narrower query.
        Args:
            sql: A single read-only SQL SELECT statement.
        """
        try:
            return json.dumps(run_athena_query_exact(sql, cfg, source_system=source_system))
        except Exception as e:  # noqa: BLE001 - surface as tool error, not a crash
            return json.dumps(_tool_error(e))

    return [athena_list_tables, athena_describe_table, athena_run_query]
