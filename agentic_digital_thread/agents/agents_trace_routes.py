# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-session observability drill-down routes (opt-in, admin-only).

Reads OTEL spans via Logs Insights from the places AgentCore writes THIS ADT
deployment's spans and exposes them per chat session for the Agents-page
drill-down panel:

- the ``spans`` log stream of each live ADT runtime's OWN log group
  (``/aws/bedrock-agentcore/runtimes/<agentRuntimeId>-DEFAULT``) — AgentCore's
  unified span destination, the default for agents created once the Region
  supports it (``UNIFIED_TRACES_DESTINATION_ENABLED`` overrides it per agent);
- the shared CloudWatch Transaction Search log group ``aws/spans``, where
  older agents (and agents opted out of the unified destination) keep writing.

Span sources, per request
-------------------------
The runtimes are created by ``deploy.sh``, not CDK, so the sources are worked
out on each request: every runtime the AgentCore control plane lists now
whose name carries the ADT prefix (``fleet_metrics._RUNTIME_PREFIX``),
intersected with the log groups that exist (a runtime has no log group until
its first invocation, and StartQuery fails on a missing group), plus
``aws/spans`` when it exists. Another application's AgentCore runtimes in the
same account are never read, and neither is the leftover log group of a
retired (deleted) ADT runtime. A failed runtime listing, a failed group
check, or a group that cannot be queried is an honest error, never a
quieter answer from the sources that did work.

Working that out is bounded whatever the account holds: at most 10
ListAgentRuntimes pages of 100 (the listing stops early once the ADT groups
alone are more than one query reads), then at most two DescribeLogGroups by
name over those candidates plus ``aws/spans``. Past either bound the request
is an honest error, never a partial source list.

All sources are read by exactly ONE StartQuery per request, so a session's
``stats ... by session_id`` count and window cover every source and the
detail query's ``limit 1000`` is global. More EXISTING sources than one query
takes (50; an absent ``aws/spans`` or a runtime with no log group yet takes no
slot) is an error and runs no query; splitting would multiply the scan work
and per-request latency. Known
limitation: a query still running when the poll gives up (about 25 s) is not
stopped (no StopQuery), the same as before this change.

Kept OUT of ``agents_routes.py`` on purpose:
that file is reserved for the concurrent evaluations change-set (Integration
2), and the file-size standard caps route modules anyway.

Feature gate
------------
``AGENTS_OBSERVABILITY_DRILLDOWN`` (set to ``"true"``/``"false"`` by
``stacks/_observability.wire_observability_reader`` from the sticky
``--observability`` deploy flag). When it is anything other than ``"true"``,
BOTH spans-reader routes return ``{"enabled": false}`` with HTTP 200 and make
ZERO boto3 calls — the frontend feature-detects and renders nothing. The
Tier-3 provenance redemption route at the bottom of this module reads a
different data source and is deliberately NOT gated (see its docstring).

FROZEN response schema (the frontend wave builds against this — do not change)
------------------------------------------------------------------------------
``GET /api/admin/agents/traces/sessions?hours=``::

    {"enabled": true,
     "sessions": [{"session_id": str, "start_ms": int|null,
                   "end_ms": int|null, "last_seen_ms": int|null,
                   "span_count": int}]}

``GET /api/admin/agents/traces/sessions/{session_id}?hours=``::

    {"enabled": true, "session_id": str,
     "spans": [{"name": str, "start_ms": int|null, "duration_ms": int|null,
                "status": str, "tokens_in": int|null, "tokens_out": int|null,
                "kind": "tool"|"model"|"internal"}],
     "totals": {"tokens_in": int, "tokens_out": int, "tool_calls": int}}

On an Insights failure both routes keep the shape, leave the rows EMPTY, and
add an ``"error"`` string — honest errors, never fabricated data (an empty
timeline must be distinguishable from a failed query).

Table-driven span parsing
-------------------------
The exact attribute names emitted by the ADOT auto-instrumentation +
strands 1.42.0 (``gen_ai.*`` usage attributes, the session-correlation
attribute) are the single biggest unknown in this integration and must be
confirmed against a deployed env with the flag on. They are therefore DATA
(the ``*_FIELDS`` tables below), not code — confirming/renaming an attribute
is a one-line table edit, and the Insights queries are generated from the
same tables so query and parser cannot drift apart.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from . import (
    dataset_store,
    id_name_resolver,
    investigation_trace_store,
    provenance_store,
    trace_display,
)
from ._insights import (
    MAX_LOG_GROUP_IDENTIFIERS,
    MAX_LOG_GROUPS_PER_QUERY,
    InsightsError,
    existing_log_groups,
    parse_row,
    run_insights,
)
from .fleet_metrics import _RUNTIME_PREFIX as ADT_RUNTIME_PREFIX
from .public_errors import public_error
from .runtime_provision_helpers import RuntimeListingCapped, iter_agent_runtime_pages

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/agents/traces")

#: CloudWatch Transaction Search's shared span log group (older agents).
SPANS_LOG_GROUP = "aws/spans"
#: AgentCore runtime log groups are ``<prefix><agentRuntimeId>-<endpoint>``;
#: with the unified span destination a runtime's spans land in the
#: RUNTIME_SPANS_STREAM stream of its own group.
RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
RUNTIME_ENDPOINT = "DEFAULT"
RUNTIME_SPANS_STREAM = "spans"

#: The most span sources one request reads: one StartQuery takes 50 log
#: groups, and one DescribeLogGroups checks at most 50 names.
MAX_SPAN_SOURCES = min(MAX_LOG_GROUPS_PER_QUERY, MAX_LOG_GROUP_IDENTIFIERS)
#: ListAgentRuntimes page size (the API maximum) and the most pages one
#: request reads: past 1,000 runtimes the request is an honest error.
RUNTIME_LIST_PAGE_SIZE = 100
MAX_RUNTIME_LIST_PAGES = 10

_SESSIONS_LIMIT = 50

# ── Table-driven span-field mapping (attribute names are DATA) ──────────────
# Candidate dotted paths, first match wins. "attributes.X" paths are tried
# both as a nested walk and as a FLAT key inside the "attributes" object
# (OTEL JSON encodes attribute names with dots as flat keys).
SESSION_ID_FIELDS = (
    "attributes.session.id",
    "attributes.gen_ai.conversation.id",
)
TOKENS_IN_FIELDS = (
    "attributes.gen_ai.usage.input_tokens",
    "attributes.gen_ai.usage.prompt_tokens",
)
TOKENS_OUT_FIELDS = (
    "attributes.gen_ai.usage.output_tokens",
    "attributes.gen_ai.usage.completion_tokens",
)
NAME_FIELDS = ("name", "spanName")
STATUS_FIELDS = ("status.code", "statusCode")
# (path, factor-to-milliseconds) — all current candidates are Unix nanos.
_NS_TO_MS = 1.0 / 1_000_000
START_TIME_FIELDS = (("startTimeUnixNano", _NS_TO_MS), ("startTime", _NS_TO_MS))
END_TIME_FIELDS = (("endTimeUnixNano", _NS_TO_MS), ("endTime", _NS_TO_MS))
DURATION_FIELDS = (("durationNano", _NS_TO_MS), ("durationInNanos", _NS_TO_MS))

#: OTEL status.code values normalized to the frozen schema's status string.
STATUS_MAP = {
    "0": "UNSET", "1": "OK", "2": "ERROR",
    "STATUS_CODE_UNSET": "UNSET", "STATUS_CODE_OK": "OK",
    "STATUS_CODE_ERROR": "ERROR",
    "UNSET": "UNSET", "OK": "OK", "ERROR": "ERROR",
}

#: Span names matching any of these mark a gateway tool call (the gateway
#: exposes tools as ``<system_id>___<tool>``; see fleet metric aggregation).
TOOL_SPAN_NAME_PATTERNS = (r"___", r"^execute_tool\b")
_TOOL_NAME_RES = tuple(re.compile(p) for p in TOOL_SPAN_NAME_PATTERNS)

#: Conservative allowlist for session ids interpolated into Insights queries.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,256}$")

# D3 Tier-3 handle shape + the hash-digest log helper — imported from their
# ONE home (``agents/provenance_store.py``, the write side) so route and
# store validation can never drift apart.
_QUERY_REF_RE = provenance_store.QUERY_REF_RE
_digest12 = provenance_store.digest12

_MAX_HOURS = 168.0  # 7 days — matches the Agents-page window ceiling


def _drilldown_enabled() -> bool:
    """The opt-in gate: only the literal env value "true" enables the reader."""
    return os.environ.get("AGENTS_OBSERVABILITY_DRILLDOWN", "") == "true"


def _check_admin(request: Request):
    """Defense-in-depth admin check (mirrors admin_routes; the auth middleware
    already 403s non-admins on the /api/admin prefix)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def _boto_session():
    import boto3
    return boto3.Session(region_name=os.environ.get("AWS_REGION", "eu-central-1"))


def _clamp_hours(hours: float) -> float:
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return 24.0
    return max(1.0, min(h, _MAX_HOURS))


# ── Span sources (worked out per request) ───────────────────────────────────

def _adt_group_name(runtime: dict) -> str | None:
    """A listed runtime's log group name, or None when it is not ADT's."""
    if not str(runtime.get("agentRuntimeName") or "").startswith(ADT_RUNTIME_PREFIX):
        return None
    return f"{RUNTIME_LOG_GROUP_PREFIX}{runtime['agentRuntimeId']}-{RUNTIME_ENDPOINT}"


def _adt_runtime_log_groups(control_client) -> list[str]:
    """The log group name of every ADT runtime the control plane lists now.

    A deleted (retired) runtime is no longer listed, so its leftover log
    group drops out; another application's runtimes never carry the ADT
    prefix. The work is bounded whatever the account holds: at most
    :data:`MAX_RUNTIME_LIST_PAGES` pages of :data:`RUNTIME_LIST_PAGE_SIZE`,
    and the listing stops on the page where the ADT groups alone first
    exceed :data:`MAX_SPAN_SOURCES`. Either bound, or a failed listing,
    raises :class:`InsightsError` — never a partial list, and never an empty
    one, which would read as "no runtimes".
    """
    # ListAgentRuntimes is covered by the task role's existing
    # ADTComputeStack._agentcore_core_statements grant (verified live 2026-10-22).
    pages = iter_agent_runtime_pages(control_client, max_results=RUNTIME_LIST_PAGE_SIZE,
                                     max_pages=MAX_RUNTIME_LIST_PAGES)
    groups: set[str] = set()
    try:
        for page in pages:
            groups.update(name for name in map(_adt_group_name, page) if name)
            if len(groups) > MAX_SPAN_SOURCES:
                raise InsightsError(
                    f"At least {len(groups)} ADT runtime log groups exceed the "
                    f"{MAX_SPAN_SOURCES} one Logs Insights query reads; "
                    "no query was run")
    except InsightsError:
        raise
    except RuntimeListingCapped as e:
        raise InsightsError(
            f"{e}, the most one request reads (pages of up to "
            f"{RUNTIME_LIST_PAGE_SIZE}); no query was run") from e
    except Exception as e:
        raise InsightsError(f"Could not list the AgentCore runtimes: {e}") from e
    return sorted(groups)


def span_log_groups(logs_client, control_client) -> list[str]:
    """Every log group that can hold this deployment's spans right now:
    ``aws/spans`` when it exists, then each live ADT runtime's log group
    that exists (a runtime has no log group until its first invocation).

    Existence is checked by name in at most two DescribeLogGroups calls of
    up to :data:`MAX_LOG_GROUP_IDENTIFIERS` names (``aws/spans`` plus at most
    :data:`MAX_SPAN_SOURCES` runtime groups). Only groups that exist count
    toward the one-query limit, so an absent ``aws/spans`` or a runtime not
    yet invoked takes no slot. Raises :class:`InsightsError` when a listing
    or that check fails or is incomplete, or when more than
    :data:`MAX_SPAN_SOURCES` groups exist, so a denied ListAgentRuntimes or
    DescribeLogGroups can never read as "no spans".
    """
    candidates = [SPANS_LOG_GROUP, *_adt_runtime_log_groups(control_client)]
    step = MAX_LOG_GROUP_IDENTIFIERS
    existing = [name for i in range(0, len(candidates), step)
                for name in existing_log_groups(logs_client, candidates[i:i + step])]
    if len(existing) > MAX_SPAN_SOURCES:
        raise InsightsError(
            f"{len(existing)} span log groups exceed the {MAX_SPAN_SOURCES} "
            "one Logs Insights query reads; no query was run")
    return existing


def _span_source_filter() -> str:
    """Keep span events only: all of ``aws/spans``, and only the ``spans``
    stream of a runtime log group (its other streams are application logs).
    ``@log`` is ``<account-id>:<log-group-name>``."""
    shared = SPANS_LOG_GROUP.replace("/", "\\/")
    return f'filter @logStream = "{RUNTIME_SPANS_STREAM}" or @log like /:{shared}$/'


def _span_rows(logs_client, control_client, query: str, hours: float) -> list[list[dict]]:
    """Run ``query`` over every span source in exactly ONE StartQuery.

    No source at all returns ``[]`` without a query (nothing can hold spans).
    :func:`span_log_groups` already refuses more sources than one StartQuery
    takes, before any query: splitting would multiply scan work and latency
    per request. Any listing or query failure raises :class:`InsightsError`
    — never a partial answer from the sources that worked, which would hide
    the failing ones.
    """
    groups = span_log_groups(logs_client, control_client)
    if not groups:
        return []
    return run_insights(logs_client, groups, query, hours)


# ── Query construction (pure — unit-tested) ─────────────────────────────────

def build_sessions_query(limit: int = _SESSIONS_LIMIT) -> str:
    """Group spans by the session-correlation attribute (candidates coalesced)."""
    sid = f"coalesce({', '.join(SESSION_ID_FIELDS)})"
    return (
        f"{_span_source_filter()}\n"
        f"| fields {sid} as session_id\n"
        "| filter ispresent(session_id)\n"
        "| stats count(*) as span_count, min(@timestamp) as first_ts, "
        "max(@timestamp) as last_ts by session_id\n"
        "| sort last_ts desc\n"
        f"| limit {limit}"
    )


def build_session_detail_query(session_id: str, limit: int = 1000) -> str:
    """Fetch the raw span documents for one session (parsed in Python via the
    attribute tables — Insights only filters/orders here)."""
    clauses = " or ".join(f'{path} = "{session_id}"' for path in SESSION_ID_FIELDS)
    return (
        f"{_span_source_filter()}\n"
        "| fields @timestamp, @message\n"
        f"| filter {clauses}\n"
        "| sort @timestamp asc\n"
        f"| limit {limit}"
    )


# ── Table-driven span parsing (pure — unit-tested) ──────────────────────────

def _lookup(doc: dict, path: str):
    """Resolve a dotted path: flat key at root → nested walk → flat key under
    "attributes" (OTEL flat-encodes dotted attribute names)."""
    if path in doc:
        return doc[path]
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            cur = None
            break
    if cur is not None:
        return cur
    if path.startswith("attributes."):
        attrs = doc.get("attributes")
        if isinstance(attrs, dict):
            return attrs.get(path[len("attributes."):])
    return None


def _first(doc: dict, paths) -> object:
    for path in paths:
        v = _lookup(doc, path)
        if v is not None:
            return v
    return None


def _first_ms(doc: dict, scaled_paths) -> int | None:
    for path, factor in scaled_paths:
        v = _lookup(doc, path)
        if v is None:
            continue
        try:
            return int(float(v) * factor)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _status(doc: dict) -> str:
    raw = _first(doc, STATUS_FIELDS)
    if isinstance(raw, dict):  # {"code": ...} nested one level deeper
        raw = raw.get("code")
    if raw is None:
        return "UNSET"
    return STATUS_MAP.get(str(raw).strip().upper(),
                          STATUS_MAP.get(str(raw).strip(), str(raw).strip().upper()))


def _classify_kind(name: str, tokens_in: int | None, tokens_out: int | None) -> str:
    for pattern in _TOOL_NAME_RES:
        if pattern.search(name):
            return "tool"
    if tokens_in is not None or tokens_out is not None:
        return "model"
    return "internal"


def parse_span(doc: dict) -> dict:
    """One span document → the FROZEN per-span schema row."""
    name = str(_first(doc, NAME_FIELDS) or "")
    start_ms = _first_ms(doc, START_TIME_FIELDS)
    duration_ms = _first_ms(doc, DURATION_FIELDS)
    if duration_ms is None:
        end_ms = _first_ms(doc, END_TIME_FIELDS)
        if start_ms is not None and end_ms is not None:
            duration_ms = end_ms - start_ms
    tokens_in = _int_or_none(_first(doc, TOKENS_IN_FIELDS))
    tokens_out = _int_or_none(_first(doc, TOKENS_OUT_FIELDS))
    return {
        "name": name,
        "start_ms": start_ms,
        "duration_ms": duration_ms,
        "status": _status(doc),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "kind": _classify_kind(name, tokens_in, tokens_out),
    }


def _row_to_span_doc(row: list[dict]) -> dict | None:
    """Insights row → parsed @message span JSON (None if not a JSON object)."""
    message = parse_row(row).get("@message")
    if not message:
        return None
    try:
        doc = json.loads(message)
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


def _ts_to_ms(value) -> int | None:
    """Insights @timestamp aggregate ("YYYY-MM-DD HH:MM:SS[.mmm]", UTC) → epoch ms."""
    if value is None:
        return None
    s = str(value).strip()
    try:  # numeric epoch (millis) — some result shapes return numbers
        return int(float(s))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def parse_sessions_rows(rows: list[list[dict]]) -> list[dict]:
    """Insights stats rows → the FROZEN sessions list."""
    sessions = []
    for row in rows:
        r = parse_row(row)
        session_id = r.get("session_id")
        if not session_id:
            continue
        end_ms = _ts_to_ms(r.get("last_ts"))
        sessions.append({
            "session_id": str(session_id),
            "start_ms": _ts_to_ms(r.get("first_ts")),
            "end_ms": end_ms,
            # Additive alias consumed by the frontend's TraceSessionSummary.
            "last_seen_ms": end_ms,
            "span_count": _int_or_none(r.get("span_count")) or 0,
        })
    return sessions


def _totals(spans: list[dict]) -> dict:
    return {
        "tokens_in": sum(s["tokens_in"] or 0 for s in spans),
        "tokens_out": sum(s["tokens_out"] or 0 for s in spans),
        "tool_calls": sum(1 for s in spans if s["kind"] == "tool"),
    }


# ── Routes ──────────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_trace_sessions(request: Request, hours: float = 24):
    _check_admin(request)
    if not _drilldown_enabled():
        return {"enabled": False}
    hours = _clamp_hours(hours)
    session = _boto_session()
    logs = session.client("logs")
    control = session.client("bedrock-agentcore-control")
    try:
        rows = _span_rows(logs, control, build_sessions_query(), hours)
    except InsightsError as e:
        return {"enabled": True, "sessions": [],
                **public_error(e, "traces.sessions", default="upstream_error")}
    return {"enabled": True, "sessions": parse_sessions_rows(rows)}


@router.get("/sessions/{session_id}")
def get_trace_session(session_id: str, request: Request, hours: float = 24):
    _check_admin(request)
    if not _drilldown_enabled():
        return {"enabled": False}
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    hours = _clamp_hours(hours)
    session = _boto_session()
    logs = session.client("logs")
    control = session.client("bedrock-agentcore-control")
    try:
        rows = _span_rows(logs, control, build_session_detail_query(session_id), hours)
    except InsightsError as e:
        return {
            "enabled": True,
            "session_id": session_id,
            "spans": [],
            "totals": {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0},
            **public_error(e, "traces.session_detail", default="upstream_error"),
        }
    spans = []
    for row in rows:
        doc = _row_to_span_doc(row)
        if doc is None:
            continue
        spans.append(parse_span(doc))
    spans.sort(key=lambda s: (s["start_ms"] is None, s["start_ms"] or 0))
    return {
        "enabled": True,
        "session_id": session_id,
        "spans": spans,
        "totals": _totals(spans),
    }


@router.get("/queries/{query_ref}")
def get_provenance_query(query_ref: str, request: Request, session_id: str = ""):
    """Redeem a Tier-3 provenance ``query_ref`` for its stored entry (frozen D5).

    Deliberately NOT gated by ``AGENTS_OBSERVABILITY_DRILLDOWN``: that flag
    gates the CloudWatch Transaction Search spans READER above — an opt-in,
    cost-bearing data source. This route reads a DIFFERENT source, the
    replica-coherent provenance-query store (``agents/provenance_store.py``)
    populated on every deploy, so Tier-3 detail stays available whether or
    not the observability drill-down is enabled.

    Responses (D5 — the frontend Tier-3 expander builds against this):

    - 200: ``{"query_ref", "session_id", "system_id", "tool_name",
      "executed_query", "kg_artifacts", "captured_at"}`` (plus an honest
      ``"note"`` when the query text exceeded the write-side bound, and
      additive ``query_fingerprint``/``query_shape_version`` when trusted
      post-execution capture produced a bounded lexical shape).
    - 400: regex-invalid ``query_ref`` (``^q-[0-9a-f]{12}$``) or
      ``session_id`` (``_SESSION_ID_RE``) — structured detail, never an echo
      of the invalid value.
    - 404: ``{"detail": "Unknown or expired query_ref"}`` — the store's only
      miss signal; never a payload echo, never fabricated text. Entries
      expire after the store's DELIBERATE per-kind TTL
      (``provenance_store.PROVENANCE_QUERY_TTL_SECONDS`` — 24 h, sized so a
      transcript reopened across a workday still redeems; see the constant's
      rationale), after which this route returns the same structured 404.

    Logging is digest-only (stronger than digit masking — the value never
    appears at all): identifiers appear only as ``sha256[:12]`` digests plus
    lengths — no query text, no raw ids.
    """
    _check_admin(request)
    if not _QUERY_REF_RE.fullmatch(query_ref):
        raise HTTPException(status_code=400, detail="Invalid query_ref")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    entry = provenance_store.get_query(session_id, query_ref)
    if entry is None:
        logger.info("[TRACES] provenance query miss ref_sha=%s sid_sha=%s",
                    _digest12(query_ref), _digest12(session_id))
        raise HTTPException(status_code=404, detail="Unknown or expired query_ref")
    logger.info(
        "[TRACES] provenance query hit ref_sha=%s sid_sha=%s "
        "shape=%s version=%s query_len=%d",
        _digest12(query_ref), _digest12(session_id),
        entry.get("query_fingerprint") or "absent",
        entry.get("query_shape_version") or "absent",
        len(entry.get("executed_query") or ""),
    )
    return entry


@router.get("/datasets/{dataset_ref}")
def get_provenance_dataset(dataset_ref: str, request: Request, session_id: str = ""):
    """Redeem a claim-check ``dataset_ref`` for the withheld full series
    (Wave 4 — the S3-backed resolver the maintainers' deferred-work list anticipates;
    the ``tel-<uuid12>`` handle shape and the tool contract are unchanged).

    CLIENT-ONLY by the claim-check policy: this route is the ONE
    production reader of ``dataset_store.get_dataset`` (grep-pinned). The
    payload never re-enters an LLM token path and never rides the SSE stream.
    Like the queries route above, deliberately NOT gated by
    ``AGENTS_OBSERVABILITY_DRILLDOWN`` (that flag gates the CloudWatch spans
    reader — a different, cost-bearing data source).

    ``session_id`` is required and validated exactly as on the queries route
    (same ``_SESSION_ID_RE``, same structured 400). The store lookup itself
    is capability-based: the handle is minted at the connector boundary —
    below the session — as an unguessable random uuid12, so the handle IS
    the capability and the admin gate is the authorization boundary.

    Responses: 200 the cap-time document (``{"dataset_ref", "stored_at",
    "point_count", "series"}``); 400 fullmatch-invalid ``dataset_ref``/
    ``session_id`` (structured detail, never an echo); 404 ``{"detail":
    "Unknown or expired dataset_ref"}`` — for never-persisted handles (no
    bucket, over-cap series, write failure) and past the deliberate read TTL
    (``dataset_store.DATASET_REF_TTL_SECONDS``, 24 h; the bucket's 7-day
    lifecycle does physical deletion). Logging is digest-only
    (``sha256[:12]`` + counts) — never point values or timestamps.
    """
    _check_admin(request)
    if not dataset_store.DATASET_REF_RE.fullmatch(dataset_ref):
        raise HTTPException(status_code=400, detail="Invalid dataset_ref")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    document = dataset_store.get_dataset(dataset_ref)
    if document is None:
        logger.info("[TRACES] dataset miss ref_sha=%s sid_sha=%s",
                    _digest12(dataset_ref), _digest12(session_id))
        raise HTTPException(status_code=404,
                            detail="Unknown or expired dataset_ref")
    logger.info("[TRACES] dataset hit ref_sha=%s sid_sha=%s points=%d",
                _digest12(dataset_ref), _digest12(session_id),
                int(document.get("point_count") or 0))
    return document


@router.get("/investigations/{query_ref}")
def get_investigation_trace(query_ref: str, request: Request, session_id: str = ""):
    """Redeem a ``query_ref`` for its stored investigation trace (Wave 6 —
    the W6-F5 D5-family sibling).

    Like the two siblings above, deliberately NOT gated by
    ``AGENTS_OBSERVABILITY_DRILLDOWN``: that flag gates the CloudWatch
    Transaction Search spans READER — an opt-in, cost-bearing data source.
    This route reads a DIFFERENT source, the replica-coherent
    investigation-trace store (``agents/investigation_trace_store.py``),
    populated on every deploy.

    Responses (W6-F2/F5 — the frontend <InvestigationTrace/> builds against
    this):

    - 200: the stored W6-F2 entry (question, ordered ``steps`` with reasoning
      and bounded tool input/output, truncated flags, ``steps_dropped``, the
      answer without engine lines) as a person is shown it, the store never
      rewritten (:func:`trace_display.public_entry`): no step input or output
      holds an ``_adt_`` member such as ``_adt_source`` or ``_adt_executed_query``
      (Technical detail shows that SQL as executed); text that does not parse
      is cut before one and marked ``*_truncated``. Plus ``final_answer_names``
      (:func:`_with_answer_names`), never under ``RESPONSE_INTEGRITY_MODE=enforce``.
    - 400: regex-invalid ``query_ref`` (``^q-[0-9a-f]{12}$``) or
      ``session_id`` — structured detail, never an echo of the value.
    - 404: ``{"detail": "Unknown or expired investigation trace"}`` — the
      store's only miss signal; covers legacy agents (no Wave-6 writer),
      never-written calls, and the 24 h TTL. A Wave-5 query entry under the
      same key does NOT satisfy this read (kind isolation — the families
      are independent).

    Logging is digest-only: ``sha256[:12]`` digests plus step count and
    answer length — never reasoning, input, output, question, or answer
    text.
    """
    _check_admin(request)
    if not _QUERY_REF_RE.fullmatch(query_ref):
        raise HTTPException(status_code=400, detail="Invalid query_ref")
    if not provenance_store.SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    entry = investigation_trace_store.get_trace(session_id, query_ref)
    if entry is None:
        logger.info("[TRACES] investigation trace miss ref_sha=%s sid_sha=%s",
                    _digest12(query_ref), _digest12(session_id))
        raise HTTPException(status_code=404,
                            detail="Unknown or expired investigation trace")
    logger.info(
        "[TRACES] investigation trace hit ref_sha=%s sid_sha=%s steps=%d answer_len=%d",
        _digest12(query_ref), _digest12(session_id),
        len(entry.get("steps") or []), len(entry.get("final_answer") or ""))
    return _with_answer_names(trace_display.public_entry(entry))


#: Longest graph name :func:`_with_answer_names` returns. A longer one is left
#: out and its id is shown as stored, so one name cannot fill the card.
MAX_ANSWER_NAME_CHARS = 256


def _with_answer_names(entry: dict) -> dict:
    """``entry`` as the evidence card reads it: the entry as given (the
    stored entry with its steps shown by :func:`trace_display.public_entry`),
    plus ``final_answer_names``, ``{id: name}`` for the graph-known ids in
    ``final_answer``, when any is found. The stored entry is never rewritten,
    and neither is the answer text.

    The names come from the chat trace's bounded resolver
    (``id_name_resolver.names_in_text``: at most one lookup of at most 50
    ids, one attempt with a 3 s limit, one lookup at a time per process, a
    pause after a failure), each exactly as the graph gave it; a name over
    :data:`MAX_ANSWER_NAME_CHARS` is left out. The card renders the answer as
    markdown and places the names after parsing
    (``frontend/src/chat/trace-id-names.ts``). Editing the text instead
    cannot keep a name literal: a URL, ``www``, email or UTC-time name needs
    no escape and is still parsed into a link or a time.

    Named here, at the read, because the card is this route's only reader;
    the store's other readers (the chat trace's telemetry and sample lines,
    the Recent sessions reload) name their own copies. Steps get no names:
    their reasoning narrates tool calls whose recorded input and output sit
    beside it with the raw ids.

    Under ``RESPONSE_INTEGRITY_MODE=enforce`` the entry is returned as given
    with no lookup, as before. Never raises: on any fault the entry is
    returned as given."""
    try:
        from .config import response_integrity_enforced
        if response_integrity_enforced():
            return entry
        names = {i: n for i, n in id_name_resolver.names_in_text(entry.get("final_answer")).items()
                 if len(n) <= MAX_ANSWER_NAME_CHARS}
        return {**entry, "final_answer_names": names} if names else entry
    except Exception:  # noqa: BLE001 - naming is display-only, never fatal
        return entry
