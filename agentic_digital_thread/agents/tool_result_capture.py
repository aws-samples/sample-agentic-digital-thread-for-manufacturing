# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounded, metadata-only capture for incomplete system-agent turns.

Tool payloads are untrusted: they may contain SQL rows, credentials, PII, or
third-party text. This module never returns arbitrary payload content. It only
retains a small allowlist of counts/statuses and a coarse error category.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

TOOL_RESULTS_STATE_KEY = "_adt_successful_tool_results"
#: Private flag on a captured attempt whose trusted executed query was a
#: catalog probe (``provenance.is_discovery_statement``). Read by the Mode A
#: no-data line; never part of the public metadata (``_public_metadata``).
DISCOVERY_FLAG = "_discovery"
TOOL_RESULT_CAPTURE_MAX = 4
TOOL_RESULT_CAPTURE_CHARS = 1024
_MAX_CONTENT_FRAGMENTS = 16
_MAX_TOP_LEVEL_LIST_ITEMS = 256
_MAX_SITEWISE_ITEMS = 256
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SAFE_ERROR_TYPES = {
    "query_timeout",
    "scan_budget_exceeded",
    "scope_required",
    "invalid_request",
    "sitewise_error",
    "tool_error",
    "transport_error",
    "tool_exception",
}
_COUNT_FIELDS = (
    "row_count",
    "item_count",
    "count",
    "success_count",
    "error_count",
    "scanned_bytes",
    "budget_bytes",
)
#: What kind of read a tool makes, declared by the API tool factories
#: (:func:`declare_read`). ``discovery`` reads say what exists (lists,
#: descriptions, hierarchy, schema, mappings); ``data`` reads return
#: readings, records, status or events. Only a data read can be evidence for
#: an answer: the capture hook keeps the read count of a declared data tool
#: only (:func:`record_api_read`). A tool that declares nothing (SQL tools,
#: MCP server tools) is never counted as an API read.
READ_KIND_ATTR = "_adt_read_kind"
DATA_READ = "data"
DISCOVERY_READ = "discovery"
_READ_KINDS = frozenset({DATA_READ, DISCOVERY_READ})


def declare_read(kind: str, tool: Any) -> Any:
    """``tool``, declared as making a ``kind`` read (``DATA_READ`` or
    ``DISCOVERY_READ``). Called where the tool is defined."""
    if kind not in _READ_KINDS:
        raise ValueError(f"unknown read kind: {kind!r}")
    setattr(tool, READ_KIND_ATTR, kind)
    return tool


def declared_read_kinds(tools: Any) -> dict[str, str]:
    """``{tool name: declared read kind}`` for the tools that declare one."""
    kinds: dict[str, str] = {}
    for item in tools if isinstance(tools, list) else ():
        name = getattr(item, "tool_name", None) or getattr(item, "name", None)
        kind = getattr(item, READ_KIND_ATTR, None)
        if isinstance(name, str) and kind in _READ_KINDS:
            kinds[name] = kind
    return kinds


#: How much data one tool call read, reported by the tool itself. A declared
#: data tool calls :func:`report_read_count` with the number of values,
#: points or records it parsed out of the source's response: its own parse,
#: never a count field of the source's JSON, so a source that answers with a
#: field named like a count cannot make a read. The capture hook opens one
#: slot per call (BeforeToolCallEvent, :func:`open_read_slot`) and reads it
#: back after the call (:func:`close_read_slot`). strands runs both hooks and
#: the tool in the call's own task, and a sync tool in a thread that copies
#: that task's context, so the tool writes the very slot the hook opened, and
#: concurrent calls never share one. The last report of a call is its count:
#: a call that reads again (a rerun after a lookup) answers with its last
#: read. A read that only supports the answer (a tag lookup, the chart
#: render a history read carries) runs inside :func:`reads_not_counted`.
_READ_SLOT: ContextVar["dict | None"] = ContextVar("adt_read_slot", default=None)
#: ``invocation_state`` key: ``{toolUseId: reported count}`` of the kept
#: calls of declared data tools (:func:`record_api_read`).
API_READS_STATE_KEY = "_adt_api_reads"
#: Calls kept per invocation (the provenance store keeps at most 32 per
#: entry as well).
MAX_COUNTED_CALLS = 32


def open_read_slot() -> dict:
    """Open the read-count slot of the call about to run."""
    slot = {"count": None, "suppressed": 0}
    _READ_SLOT.set(slot)
    return slot


def close_read_slot() -> "int | None":
    """The count the call reported (``None``: none), and close its slot."""
    slot = _READ_SLOT.get()
    _READ_SLOT.set(None)
    return slot.get("count") if isinstance(slot, dict) else None


def report_read_count(count: Any) -> None:
    """Report how many values, points or records the running call parsed.
    A no-op outside a call the capture hook watches (Mode B, onboarding
    checks), inside :func:`reads_not_counted`, and for anything but a
    non-negative int."""
    slot = _READ_SLOT.get()
    if (isinstance(slot, dict) and not slot["suppressed"] and isinstance(count, int)
            and not isinstance(count, bool) and count >= 0):
        slot["count"] = count


@contextmanager
def reads_not_counted():
    """Reads inside this block are not the call's read (a lookup, a chart
    render): their reports are dropped."""
    slot = _READ_SLOT.get()
    if isinstance(slot, dict):
        slot["suppressed"] += 1
    try:
        yield
    finally:
        if isinstance(slot, dict):
            slot["suppressed"] -= 1


def record_api_read(invocation_state: dict, tool_use: Any, count: Any) -> None:
    """Keep one kept data call's reported ``count``, keyed by its toolUseId
    (a call that reported none is kept as zero), at most
    ``MAX_COUNTED_CALLS`` calls."""
    reads = invocation_state.setdefault(API_READS_STATE_KEY, {})
    if not isinstance(reads, dict):
        return
    call_id = tool_use.get("toolUseId") if isinstance(tool_use, dict) else None
    key = call_id if isinstance(call_id, str) and call_id else f"#{len(reads)}"
    if key not in reads and len(reads) >= MAX_COUNTED_CALLS:
        return
    reads[key] = count if isinstance(count, int) and not isinstance(count, bool) else 0


def collect_api_reads(invocation_state: Any) -> int:
    """How many kept data calls of this invocation reported reading at least
    one value, point or record (``0`` on foreign state)."""
    state = invocation_state if isinstance(invocation_state, dict) else {}
    reads = state.get(API_READS_STATE_KEY)
    if not isinstance(reads, dict):
        return 0
    return sum(1 for count in list(reads.values())[:MAX_COUNTED_CALLS]
               if isinstance(count, int) and not isinstance(count, bool) and count > 0)

#: Fixed incomplete-envelope text for a turn the system agent's model stopped
#: at its own output-token cap (``stop_reason == "max_tokens"``) — typically
#: while still writing a large tool request, so the system was never asked.
#: Fixed text, never model text. Deliberately free of every outage/transient
#: marker (orchestrator retry markers, chat_client error classifiers, smoke /
#: eval outage detectors — pinned in test_mode_a_output_limit.py): this is not
#: an outage and must not be retried verbatim or reported as one.
OUTPUT_LIMIT_MESSAGE = (
    "The system agent's own request grew past its output limit before it could "
    "be sent, so the system was not reached for it and is not known to be down."
)
OUTPUT_LIMIT_PARTIAL_MESSAGE = (
    "Agent completed some tool work, then its own output grew past its output "
    "limit before it finished; the system is not known to be down."
)
OUTPUT_LIMIT_GUIDANCE = (
    "Ask again for a narrower slice (fewer assets, fewer properties or a shorter "
    "window) or for one aggregate; repeating the same request stops at the same limit."
)
#: Output limit AFTER data queries were attempted and none returned usable
#: data: the system WAS reached, so neither "not reached" nor "not known to be
#: down" may be claimed. The captured failure categories stay in
#: ``tool_results`` (and in the plain line) for the caller's model. They are
#: not a cue for a same-call retry: the failed query ran inside the turn that
#: hit the cap (a query_timeout is the agent's own slow query), so the same ask
#: would repeat both; the answer arrives as a successful tool result, so no
#: code-level retry applies either.
OUTPUT_LIMIT_FAILED_MESSAGE = (
    "The system agent's output grew past its output limit after the data queries "
    "it attempted failed or returned no usable data."
)
OUTPUT_LIMIT_FAILED_GUIDANCE = (
    "Ask again for a narrower slice (fewer assets, fewer properties or a shorter "
    "window) or for one aggregate; repeating the same request re-runs the same "
    "queries and stops at the same limit."
)

#: Claim-check handle shape (``system_agent._summarize_vqt_series`` — the
#: deferred.md contract). Only fullmatch-validated handles are retained;
#: a handle is an opaque server-minted reference, NOT payload content, so
#: retaining it does not relax this module's allowlist posture.
_DATASET_REF_RE = re.compile(r"^tel-[0-9a-f]{12}$")
_MAX_DATASET_REFS = 4
_DATASET_REF_NODE_BUDGET = 2000
#: Capped-payload JSON (stats + landmarks per capped series) routinely
#: exceeds the 1 KB metadata text bound, so the ref scan alone re-parses
#: text content up to the model-facing connector result ceiling
#: (``response_integrity_query.MAX_QUERY_RESULT_BYTES``, 200 KB — spelled
#: locally to keep this module stdlib-pure). Only validated handles are
#: extracted from the larger parse; nothing else is read.
_DATASET_REF_SCAN_CHARS = 200_000


def _collect_dataset_refs(node: Any, refs: list[str], budget: list[int]) -> None:
    """Bounded recursive scan for fullmatch-valid ``dataset_ref`` handles."""
    if len(refs) >= _MAX_DATASET_REFS or budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(node, dict):
        ref = node.get("dataset_ref")
        if isinstance(ref, str) and _DATASET_REF_RE.fullmatch(ref) and ref not in refs:
            refs.append(ref)
        for value in node.values():
            _collect_dataset_refs(value, refs, budget)
    elif isinstance(node, list):
        for item in node[:_MAX_TOP_LEVEL_LIST_ITEMS]:
            _collect_dataset_refs(item, refs, budget)


def claim_check_dataset_refs(result: Any) -> list[str]:
    """Every fullmatch-valid claim-check handle in a tool result (≤4, order
    of appearance). Handles only — no stats, landmarks, or values are read
    out. Used by ``safe_tool_result_metadata`` so the Tier-3 provenance
    record can carry the redemption handle (Wave 4)."""
    refs: list[str] = []
    budget = [_DATASET_REF_NODE_BUDGET]
    for payload in _payload_candidates(result, _DATASET_REF_SCAN_CHARS):
        _collect_dataset_refs(payload, refs, budget)
        if len(refs) >= _MAX_DATASET_REFS:
            break
    return refs


def _safe_count(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 0 <= value <= 10**15:
        return value
    return None


def _error_category(payload: dict) -> str | None:
    error_type = payload.get("error_type")
    if isinstance(error_type, str):
        normalized = error_type.strip().lower()
        if normalized in _SAFE_ERROR_TYPES:
            return normalized
        if normalized:
            return "tool_error"
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "failure"}:
        return "tool_error"
    if payload.get("error") not in (None, "", [], {}):
        return "tool_error"
    return None


#: Error-envelope vocabulary, in two tiers. Keys are matched after lowercasing
#: and dropping every non-alphanumeric character, so ``request_id``,
#: ``requestId`` and ``x-amzn-RequestId`` match ``requestid`` and
#: ``xamznrequestid``, and ``__type`` matches ``type``. Engine metadata
#: (``_adt_*``) counts as weak vocabulary.
#:
#: STRONG fields say a call failed: a non-empty error list, exception or
#: fault (or ``isError: true``), an explanation of the failure, or an HTTP
#: status code of 400 or more (``_strong_signal``). ``error`` and
#: ``error_type`` are stronger still: they are structural errors whatever
#: else the payload carries (``_record_verdict``).
_STRONG_FAULT_FIELDS = frozenset({"errors", "exception", "fault"})
_EXPLANATION_FIELDS = frozenset({
    "message", "msg", "detail", "details", "reason", "errormessage", "errordescription",
    "errordetail", "errordetails", "faultstring"})
_HTTP_STATUS_FIELDS = frozenset({"httpstatus", "httpstatuscode", "httpcode", "statuscode"})
#: WEAK fields are names an envelope often carries but ordinary business
#: records carry too (a quality event's code, description and timestamp; a
#: check's title, type and path). They never establish an envelope; they may
#: sit inside one that already has a strong signal without turning it into a
#: record.
_WEAK_ENVELOPE_FIELDS = frozenset({
    "status", "success", "ok", "iserror", "errorcode", "code", "description", "title",
    "type", "instance", "hint", "help", "documentationurl", "moreinfo", "path",
    "timestamp", "retryable", "retryafter", "retryafterseconds", "requestid", "traceid",
    "spanid", "correlationid", "xrequestid", "xamznrequestid", "xcorrelationid"})
_ENVELOPE_FIELDS = (_STRONG_FAULT_FIELDS | _EXPLANATION_FIELDS | _HTTP_STATUS_FIELDS
                    | _WEAK_ENVELOPE_FIELDS | {"error", "errortype"})
_NON_ALNUM = re.compile(r"[^a-z0-9]")
#: ``_record_verdict`` for a business record whose OWN status field says
#: failed (a check that failed): the data asked for, never a failed call.
_RECORD = "record"


def _field_name(key: Any) -> str:
    text = str(key)
    return "adt" if text.startswith("_adt_") else _NON_ALNUM.sub("", text.lower())


def _is_envelope_field(key: Any) -> bool:
    name = _field_name(key)
    return name == "adt" or name in _ENVELOPE_FIELDS


def _http_error_code(value: Any) -> bool:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return isinstance(value, int) and not isinstance(value, bool) and value >= 400


def _strong_signal(payload: dict) -> bool:
    """True when ``payload`` carries a STRONG error-envelope field with a
    value that says so (see ``_STRONG_FAULT_FIELDS``): empty values, a false
    ``isError`` and an HTTP status below 400 are no signal."""
    for key, value in payload.items():
        name = _field_name(key)
        if name == "iserror" and value is True:
            return True
        if name in _HTTP_STATUS_FIELDS and _http_error_code(value):
            return True
        if (name in _STRONG_FAULT_FIELDS or name in _EXPLANATION_FIELDS) and value not in (
                None, "", [], {}, False):
            return True
    return False


def _record_verdict(payload: dict) -> str | None:
    """The ONE capture verdict for a dict: its error category, ``_RECORD``, or
    ``None`` (no error signal).

    A non-empty ``error`` field or an ``error_type`` is a structural error.
    A failure word in ``status`` is an error only with a STRONG signal
    (``_strong_signal``) and nothing outside the envelope vocabulary
    (``_ENVELOPE_FIELDS``); weak fields never establish an envelope alone.
    Every other shape is ambiguous, and an ambiguous shape is a record
    (usable data): contradicting a valid record is worse than leaving a
    failed call unmarked."""
    category = _error_category(payload)
    if category is None:
        return None
    error_type = payload.get("error_type")
    structural = ((isinstance(error_type, str) and error_type.strip())
                  or payload.get("error") not in (None, "", [], {}))
    if structural or (_strong_signal(payload)
                      and all(_is_envelope_field(key) for key in payload)):
        return category
    return _RECORD


def _sitewise_counts(payload: dict) -> tuple[int, int, int, bool]:
    """Count recognizable per-entry SiteWise outcomes without reading values."""
    success = error = partial = 0
    truncated = len(payload) > _MAX_SITEWISE_ITEMS
    for index, value in enumerate(payload.values()):
        if index >= _MAX_SITEWISE_ITEMS:
            break
        if not isinstance(value, (dict, list)):
            continue
        if isinstance(value, dict):
            verdict = _record_verdict(value)
            if verdict == _RECORD:
                success += 1
                continue
            if verdict:
                error += 1
                continue
            status = str(value.get("status") or "").lower()
            if status == "partial":
                if isinstance(value.get("points"), list):
                    success += 1
                    partial += 1
                else:
                    error += 1
                continue
            if "value" in value or isinstance(value.get("points"), list):
                success += 1
                continue
        elif isinstance(value, list):
            success += 1
    return success, error, partial, truncated


def _summarize_list(payload: list) -> dict:
    inspected = payload[:_MAX_TOP_LEVEL_LIST_ITEMS]
    truncated = len(payload) > len(inspected)
    verdicts = [_record_verdict(item) for item in inspected if isinstance(item, dict)]
    error_categories = [v for v in verdicts if v and v != _RECORD]
    error_count = len(error_categories)
    success_count = len(inspected) - error_count
    metadata = {
        "item_count": len(payload),
        "success_count": success_count,
        "error_count": error_count,
        "truncated": truncated,
    }
    if not inspected:
        return {**metadata, "app_status": "success", "_has_success": True}
    if error_count == len(inspected):
        if truncated:
            return {**metadata, "app_status": "unknown", "_has_success": False}
        category = error_categories[0] if len(set(error_categories)) == 1 else "tool_error"
        return {
            **metadata,
            "app_status": "error",
            "safe_error_category": category,
            "_has_success": False,
        }
    if error_count:
        return {**metadata, "app_status": "partial", "_has_success": True}
    return {**metadata, "app_status": "success", "_has_success": True}


def _summarize_payload(payload: Any) -> dict:
    """Return safe metadata; never stringify or copy arbitrary scalar content."""
    if isinstance(payload, list):
        return _summarize_list(payload)
    if not isinstance(payload, dict):
        return {"app_status": "unknown", "_has_success": False}

    verdict = _record_verdict(payload)
    if verdict and verdict != _RECORD:
        return {
            "app_status": "error",
            "safe_error_category": verdict,
            "_has_success": False,
        }

    metadata: dict[str, Any] = {}
    for field in _COUNT_FIELDS:
        count = _safe_count(payload.get(field))
        if count is not None:
            metadata[field] = count
    if "row_count" not in metadata and isinstance(payload.get("rows"), list):
        metadata["row_count"] = len(payload["rows"])
    if isinstance(payload.get("items"), list):
        metadata.setdefault("item_count", len(payload["items"]))
    if isinstance(payload.get("truncated"), bool):
        metadata["truncated"] = payload["truncated"]

    # A business record whose own status says failed is usable data.
    has_sql_or_count = (bool(metadata) or "rows" in payload or "items" in payload
                        or verdict == _RECORD)
    if has_sql_or_count:
        metadata.update(app_status="success", _has_success=True)
        return metadata

    success, error, partial, truncated = _sitewise_counts(payload)
    if success or error:
        status = "partial" if (partial or (success and error)) else ("success" if success else "error")
        metadata = {
            "app_status": status,
            "item_count": success + error,
            "success_count": success,
            "error_count": error,
            "truncated": truncated,
            "_has_success": success > 0,
        }
        if error and not success:
            metadata["safe_error_category"] = "sitewise_error"
        return metadata
    return {"app_status": "unknown", "_has_success": False}


def _bounded_json_text(text: Any, max_chars: int) -> Any | None:
    if not isinstance(text, str):
        return None
    if len(text) > max_chars:
        return None
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


def _payload_candidates(result: Any, max_chars: int):
    """Yield at most a fixed number of already-structured or bounded JSON values."""
    if isinstance(result, dict):
        content = result.get("content")
        if content is None:
            yield result
            return
    else:
        content = getattr(result, "content", None)
    if isinstance(content, list):
        for item in content[:_MAX_CONTENT_FRAGMENTS]:
            if isinstance(item, dict):
                if isinstance(item.get("json"), (dict, list)):
                    yield item["json"]
                else:
                    parsed = _bounded_json_text(item.get("text"), max_chars)
                    if parsed is not None:
                        yield parsed
        return
    if isinstance(content, (dict, list)):
        yield content
        return
    parsed = _bounded_json_text(content, max_chars)
    if parsed is not None:
        yield parsed


def safe_tool_result_metadata(result: Any, *, tool_name: str, max_chars: int) -> dict:
    raw_transport = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    normalized_transport = str(raw_transport or "").lower()
    if normalized_transport in {"success", "succeeded", "ok"}:
        transport_status = "success"
    elif normalized_transport in {"error", "failed", "failure", "cancelled", "canceled"}:
        transport_status = "error"
    else:
        transport_status = "unknown"
    safe_name = tool_name if _SAFE_TOOL_NAME.fullmatch(tool_name or "") else "tool"
    base = {"tool": safe_name, "transport_status": transport_status}
    if transport_status == "error":
        return {
            **base,
            "app_status": "error",
            "safe_error_category": "transport_error",
            "_has_success": False,
        }

    summaries = [_summarize_payload(value) for value in _payload_candidates(result, max_chars)]
    content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    fragment_overflow = isinstance(content, list) and len(content) > _MAX_CONTENT_FRAGMENTS
    # Wave 4: retain fullmatch-valid claim-check handles (opaque server-minted
    # references, never payload content). Attached on every non-transport
    # path — capped gateway payloads often ride as >1 KB text, where the
    # summary above is honestly "unknown" but the handle is still evidence.
    refs = claim_check_dataset_refs(result)

    def _with_refs(metadata: dict) -> dict:
        return {**metadata, "dataset_refs": refs} if refs else metadata

    successful = [item for item in summaries if item.get("_has_success")]
    if successful:
        # Preserve a mixed/partial status over a plain success when both appear.
        chosen = next((item for item in reversed(successful) if item.get("app_status") == "partial"), successful[-1])
        if fragment_overflow:
            chosen = {**chosen, "app_status": "partial", "truncated": True}
        return _with_refs({**base, **chosen})
    failed = [item for item in summaries if item.get("app_status") == "error"]
    if failed:
        return _with_refs({**base, **failed[-1]})
    return _with_refs({**base, "app_status": "unknown", "_has_success": False})


def _trusted_executed_query(result: Any, fallback: str, max_chars: int) -> str:
    del fallback
    for payload in _payload_candidates(result, max_chars):
        if isinstance(payload, dict):
            query = payload.get("_adt_executed_query")
            if isinstance(query, str) and len(query) <= max_chars:
                return query
    return ""


#: Wave-5 provenance sidecar: invocation-local retention of the trusted
#: executed-query rows (query text + projection entity + safe counts) so the
#: outer Mode-A frame can write ONE out-of-band store entry per cited call.
#: Invocation-state keyed (thread-safe by construction — deliberately NOT a
#: contextvar read inside tool executor threads). The retained query text is
#: server-trusted capture (`_adt_executed_query`) — it NEVER enters a tool
#: result, the SSE stream, or any LLM token path (the store is its only sink).
SIDECAR_STATEMENTS_STATE_KEY = "_adt_sidecar_statements"
#: ``{toolUseId: {"recorded": record, "engine": statement}}`` for this
#: invocation: the statement the engine received for one call's result when
#: the connector wrote it (``system_agent._keep_engine_statement``, from that
#: call's own ``athena_tools.engine_statements`` sink). The capture of that
#: call, and of no other, keeps it on its sidecar row as
#: ``engine_query_text`` (:func:`record_sidecar_statement`), the statement the
#: evidence card shows as executed. Never in a tool result.
ENGINE_STATEMENTS_STATE_KEY = "_adt_engine_statements"
#: The most calls whose engine statement one invocation keeps.
MAX_ENGINE_STATEMENT_CALLS = 64
#: ``{toolUseId: {"recorded": record, "rules": [rule]}}`` for this
#: invocation: the row exclusion one call's query ran with, as it was
#: applied (``system_agent._keep_applied_exclusion``, from that call's own
#: sink; ``row_exclusion.card_record``). The capture of that call, and of no
#: other, keeps it on its sidecar row as ``row_exclusions``
#: (:func:`record_sidecar_statement`) for the call's evidence card. Never in
#: a tool result.
APPLIED_EXCLUSIONS_STATE_KEY = "_adt_applied_row_exclusions"
#: The most calls whose applied row exclusion one invocation keeps.
MAX_APPLIED_EXCLUSION_CALLS = 64
_MAX_SIDECAR_STATEMENTS = 32


def structural_row_count(result: Any) -> int | None:
    """Actual row count of one query tool result, counted STRUCTURALLY from
    the payload: an integer ``row_count`` field, else ``len(rows)`` when
    ``rows`` is a list, else the length of a bare list payload. ``None``
    honestly when nothing rows-shaped parses — NEVER estimated. Scans with
    the same char ceiling as the ref scan: connector results routinely
    exceed the 1 KB metadata text bound, so a scan bounded by that text would
    yield ``row_count: None`` for large results."""
    for payload in _payload_candidates(result, _DATASET_REF_SCAN_CHARS):
        if isinstance(payload, dict):
            count = _safe_count(payload.get("row_count"))
            if isinstance(count, int):
                return count
            if isinstance(payload.get("rows"), list):
                return len(payload["rows"])
        elif isinstance(payload, list):
            return len(payload)
    return None


def _is_discovery_query(executed_query: str) -> bool:
    """True when the trusted executed query is a catalog probe; fail-soft."""
    if not executed_query.strip():
        return False
    try:
        from .provenance import is_discovery_statement
        return is_discovery_statement(executed_query)
    except Exception:  # nosec B110 - an unreadable query is treated as data
        return False


def _statement_entity(executed_query: str, physical_schema) -> tuple[str, list[str]]:
    """``(table, columns)`` for one captured statement — the projection proof
    first (``validate_direct_projections``; the same proof that mints
    ``physical_field "table.column"`` evidence), then the bounded pure
    tokenizer parse of the executed SQL (``provenance.sql_statement_entity``)
    when the proof yields nothing. The proof-only rule left live Wave-5
    entries ALWAYS honest-empty (Mode-A captures rarely satisfy the
    single-table proof), so the parse fallback is the fix at the writer
    seam; parse failure keeps today's honest empty. Never guessed beyond
    the final SELECT's primary FROM relation."""
    from .response_integrity_sql import validate_direct_projections
    proof = validate_direct_projections(executed_query, physical_schema or {})
    if proof.valid and proof.projections:
        tables = {p.table for p in proof.projections if p.table}
        if len(tables) == 1:
            columns: list[str] = []
            for projection in proof.projections:
                if projection.column and projection.column not in columns:
                    columns.append(projection.column)
            return next(iter(tables)), columns
    from .provenance import sql_statement_entity
    return sql_statement_entity(executed_query)


def record_sidecar_statement(invocation_state: dict, *, executed_query: str,
                             metadata: Any, physical_schema=None,
                             result: Any = None, call_id: Any = None) -> None:
    """Retain one bounded ``{query_text, table, columns, row_count,
    queried_at}`` row for the Wave-5 provenance sidecar.

    Only TRUSTED capture text qualifies (``_trusted_executed_query`` — the
    ``_adt_executed_query`` connector field); empty ⇒ no row (honest
    absence, never fabricated). ``row_count`` comes from the safe-metadata
    allowlist, else counted structurally from the tool result
    (``structural_row_count`` — the metadata path is bounded at 1 KB and
    misses large results); rows are capped at ``_MAX_SIDECAR_STATEMENTS``.
    The row keeps ``engine_query_text`` when the statement the engine
    received was kept for this very call (``call_id``, its toolUseId) and
    record (:func:`_engine_statement_of`).
    """
    if not (isinstance(executed_query, str) and executed_query.strip()):
        return
    rows = invocation_state.setdefault(SIDECAR_STATEMENTS_STATE_KEY, [])
    if not isinstance(rows, list) or len(rows) >= _MAX_SIDECAR_STATEMENTS:
        return
    table, columns = _statement_entity(executed_query, physical_schema)
    row_count = metadata.get("row_count") if isinstance(metadata, dict) else None
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        row_count = structural_row_count(result) if result is not None else None
    from datetime import datetime, timezone

    from .provenance import format_captured_at
    row = {
        "query_text": executed_query,
        "table": table,
        "columns": columns,
        "row_count": row_count,
        "queried_at": format_captured_at(datetime.now(timezone.utc)),
    }
    engine = _engine_statement_of(invocation_state, call_id, executed_query)
    if engine:
        row["engine_query_text"] = engine
    rules = _applied_exclusions_of(invocation_state, call_id, executed_query)
    if rules:
        row["row_exclusions"] = rules
    if result is not None:
        _attach_result_records(row, result)
    rows.append(row)


def _engine_statement_of(invocation_state: dict, call_id: Any, executed_query: str) -> str:
    """The statement the engine received for one call's result, when the
    connector wrote it: the entry kept under the call's id
    (:data:`ENGINE_STATEMENTS_STATE_KEY`) for this very record, taken once;
    ``""`` for anything else, so a card never shows another call's
    statement."""
    kept = invocation_state.get(ENGINE_STATEMENTS_STATE_KEY)
    if not (isinstance(kept, dict) and isinstance(call_id, str) and call_id):
        return ""
    entry = kept.pop(call_id, None)
    if not isinstance(entry, dict) or entry.get("recorded") != executed_query:
        return ""
    engine = entry.get("engine")
    return engine if isinstance(engine, str) and engine != executed_query else ""


def _applied_exclusions_of(invocation_state: dict, call_id: Any, executed_query: str) -> list:
    """The row exclusions one call's query ran with, as they were applied:
    the entry kept under the call's id (:data:`APPLIED_EXCLUSIONS_STATE_KEY`)
    for this very record, taken once; ``[]`` for anything else, so a card
    never shows another call's rule."""
    kept = invocation_state.get(APPLIED_EXCLUSIONS_STATE_KEY)
    if not (isinstance(kept, dict) and isinstance(call_id, str) and call_id):
        return []
    entry = kept.pop(call_id, None)
    if not isinstance(entry, dict) or entry.get("recorded") != executed_query:
        return []
    rules = entry.get("rules")
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def _attach_result_records(row: dict, result: Any) -> None:
    """Put on one sidecar ``row`` the records of its ``result`` that the
    engine lines read: the exact split, the storage bound, the totals, the
    id-format families and the population its text filters chose.
    Approximate results are kept apart, outside the sidecar cap
    (:func:`record_approximate_result`)."""
    pieces = split_piece_count(result)
    if pieces:
        row["pieces"] = pieces
        split = split_record(result)
        if split:
            row["split"] = split
    bound = storage_bound_record(result)
    if bound:
        row["storage_bound"] = bound
    basis = ingestion_basis_record(result)
    if basis:
        row["storage_basis"] = basis
    totals = totals_record_of(result)
    if totals:
        row["totals"] = totals
        keyed = keyed_subtotals_record_of(result)
        if keyed:
            row["keyed"] = keyed
    families = families_record_of(result)
    if families:
        row["families"] = families
    population = population_record_of(result)
    if population:
        row["population"] = population


def population_record_of(result: Any) -> dict | None:
    """The top-level text pins, figures and code-like cells of this ONE
    result (``answer_provenance.population_record``), else ``None``. Kept on
    the sidecar row only for the engine line ``_run_mode_a_query`` appends."""
    from .answer_provenance import population_record
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        found = population_record(payload)
        if found:
            return found
    return None


def families_record_of(result: Any) -> dict | None:
    """The id-format shapes of this ONE result when it expands a serialized
    list and groups by an element id (``answer_identifier_families``), made
    JSON-safe for the sidecar row, else ``None``. Kept only for the engine line
    ``_run_mode_a_query`` appends."""
    from .answer_identifier_families import families_record, record_for_sidecar
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            record = families_record(payload)
            if record:
                return record_for_sidecar(record)
    return None


def totals_record_of(result: Any) -> dict | None:
    """The engine totals of this ONE result when it is a complete grouped
    result (``answer_figures.totals_record``), else ``None``. Kept on the
    sidecar row only for the engine line ``_run_mode_a_query`` appends."""
    from .answer_figures import totals_record
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            return totals_record(payload)
    return None


def keyed_subtotals_record_of(result: Any) -> dict | None:
    """The per-key subtotals of this ONE result (``answer_figures.keyed_subtotals``)
    as a JSON-safe record, else ``None``. Kept on the sidecar row (beside
    ``totals``) only for the cross-result vouch sums ``_run_mode_a_query``
    uses; the provenance store never reads it. Subtotals are stringified and
    the aggregate text (so two results contribute to one key's cross-result
    sum only when they group by the same column with the same aggregate) rides
    along so the row stays serialisable."""
    from .answer_figures import keyed_subtotals
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            found = keyed_subtotals(payload)
            if not found:
                return None
            return {
                "keys": {str(column): {str(value): format(subtotal, "f")
                                       for value, subtotal in groups.items()}
                         for column, groups in found["keys"].items()},
                "measure": str(found["measure"]),
            }
    return None


def storage_bound_record(result: Any) -> dict | None:
    """The storage-column bound this ONE result ran with, cleaned for its
    engine line (``answer_provenance.storage_record``), else ``None``."""
    from .answer_provenance import storage_record
    for payload in _payload_candidates(result, _DATASET_REF_SCAN_CHARS):
        found = storage_record(payload)
        if found:
            return found
    return None


def ingestion_basis_record(result: Any) -> dict | None:
    """``{"columns": [...]}`` when this ONE result ran on a declared
    write-time basis (a ``storage_bound`` record with ``basis`` "ingestion"),
    whatever the query's shape, else ``None``. Kept apart from
    :func:`storage_bound_record`, which needs WHERE conjuncts for its line."""
    for payload in _payload_candidates(result, _DATASET_REF_SCAN_CHARS):
        bound = payload.get("storage_bound") if isinstance(payload, dict) else None
        columns: list = []
        for record in bound if isinstance(bound, list) else [bound]:
            if isinstance(record, dict) and record.get("basis") == "ingestion":
                columns.extend(str(c) for c in record.get("columns") or () if str(c).strip())
        if columns:
            # Every read of a multi-read call, de-duplicated and bounded.
            return {"columns": list(dict.fromkeys(columns))[:_MAX_BASIS_COLUMNS]}
    return None


#: The most write-time basis columns one result's record keeps.
_MAX_BASIS_COLUMNS = 8


def approximate_record_of(result: Any) -> dict | None:
    """The approximate aggregates this ONE result was computed with and the
    figures of its approximate columns (``answer_provenance.approximate_record``),
    else ``None``."""
    from .answer_provenance import approximate_record
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        found = approximate_record(payload)
        if found:
            return found
    return None


def data_cells_of(result: Any) -> list[str] | None:
    """The numeric cells of this ONE result when it is a successful data
    result (rows, no error), every column, as the engine rendered them
    (``answer_provenance.result_cells``), else ``None``."""
    from .answer_provenance import result_cells
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        if (isinstance(payload, dict) and isinstance(payload.get("rows"), list)
                and "error" not in payload):
            return result_cells(payload)
    return None


#: The call's approximate results and, after the first, the numeric cells of
#: each later successful data result (the values a later exact result holds),
#: in call order: ``{"kind": "approximate", "functions", "figures"}`` or
#: ``{"kind": "shadow", "figures"}``. Kept apart from the sidecar rows, whose
#: cap is lower than the call's tool-call budget, so neither is dropped.
APPROXIMATE_STATE_KEY = "_adt_approximate_results"


def _approximate_cap() -> int:
    """The bound on :data:`APPROXIMATE_STATE_KEY`: the system agent's
    per-question tool-call budget, one entry per call at most."""
    from .system_agent import _tool_call_budget
    return _tool_call_budget()


def record_approximate_result(invocation_state: dict, *, executed_query: str,
                              result: Any) -> None:
    """Keep one entry for this ONE result when it is approximate, or when it
    is a successful data result after an approximate one in this call (its
    cells shadow equal approximate figures in the engine line). Only a
    trusted executed query counts, and never a catalog probe."""
    if not (isinstance(executed_query, str) and executed_query.strip()) or \
            _is_discovery_query(executed_query):
        return
    entries = invocation_state.setdefault(APPROXIMATE_STATE_KEY, [])
    if not isinstance(entries, list) or len(entries) >= _approximate_cap():
        return
    approximate = approximate_record_of(result)
    if approximate:
        entries.append({"kind": "approximate", **approximate})
        return
    if any(isinstance(e, dict) and e.get("kind") == "approximate" for e in entries):
        cells = data_cells_of(result)
        if cells:
            entries.append({"kind": "shadow", "figures": cells})


def collect_approximate_results(invocation_state) -> list[dict]:
    """Copies of this call's :data:`APPROXIMATE_STATE_KEY` entries, ``[]`` when none."""
    raw = (invocation_state or {}).get(APPROXIMATE_STATE_KEY)
    return [dict(e) for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def split_record(result: Any) -> dict | None:
    """The exact-total split's pieces, table and computed figures for this
    ONE result (``answer_provenance.split_record``), else ``None``. Kept on
    the sidecar row only for the engine line ``_run_mode_a_query`` appends;
    the provenance store entry never reads it."""
    from .answer_provenance import split_record as record_of
    for payload in _payload_candidates(result, _DATASET_REF_SCAN_CHARS):
        found = record_of(payload)
        if found:
            return found
    return None


def split_piece_count(result: Any) -> int | None:
    """How many partition pieces an exact-total split computed this ONE
    logical result from (``exact_split.pieces``), else ``None``. The engine
    line then names the query once, with that count, never per piece."""
    for payload in _payload_candidates(result, _DATASET_REF_SCAN_CHARS):
        if not isinstance(payload, dict):
            continue
        split = payload.get("exact_split")
        if isinstance(split, dict) and split.get("applied") is True:
            pieces = _safe_count(split.get("pieces"))
            return pieces if isinstance(pieces, int) and pieces > 0 else None
    return None


def sidecar_statement_cap() -> int:
    """How many statement rows the sidecar keeps per call (``record_sidecar_statement``)."""
    return _MAX_SIDECAR_STATEMENTS


def collect_sidecar_statements(invocation_state) -> list[dict]:
    """PURE read-only accessor over the invocation-local sidecar rows
    (Wave 5): bounded copies of the retained statement rows, [] when none.
    Read in the OUTER ``_run_mode_a_query`` frame only — the write side is
    the capture hook above, so no contextvar crosses executor threads."""
    raw = (invocation_state or {}).get(SIDECAR_STATEMENTS_STATE_KEY)
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw[:_MAX_SIDECAR_STATEMENTS]
            if isinstance(row, dict)
            and isinstance(row.get("query_text"), str) and row["query_text"]]


#: Per-turn tally of the system agent's tool results and how many of them a
#: connector cut by rows, by page or by time window (``result_row_cut``):
#: ``{"results": N, "cut": K, "rows": [rows kept per cut, None if unknown]}``.
#: It feeds the ONE fixed engine line ``_run_mode_a_query`` appends to the
#: answer (``result_cut_line``), because the orchestrator only learns about a
#: cut if the system agent's prose mentions it, and measured answers did not
#: always. Invocation-state keyed like the sidecar rows and deliberately NOT
#: reset by ``_on_invocation_start``: the bounded elicitation retry is a second
#: invocation on the same state (told not to call tools), and the answer it
#: writes still rests on the first pass's results.
RESULT_CUTS_STATE_KEY = "_adt_result_cuts"
#: Reserved member carrying the engine line when the Mode A answer is ONE JSON
#: object (report-only/annotate): the answer stays valid JSON and the
#: orchestrator still reads the fact. A JSON array answer is wrapped as
#: ``{RESULT_CUT_ITEMS_KEY: [...], RESULT_CUT_NOTE_KEY: line}``; ``items`` is
#: one of the list keys ``runtime_app._extract_result_rows`` reads and none of
#: the keys the trace summary routes on (``results``/``rows``/``columns``).
RESULT_CUT_NOTE_KEY = "_adt_engine_note"
RESULT_CUT_ITEMS_KEY = "items"
_MAX_CUT_ROWS = 32
#: A bounded result is at most ``MAX_QUERY_RESULT_BYTES`` (200 KB) of JSON
#: before the SQL tool adds ``_adt_executed_query`` (up to 200 K chars, the
#: ``_success_metadata`` bound) and the source metadata, so the cut scan reads
#: up to three times the ref-scan ceiling.
_CUT_SCAN_CHARS = 3 * _DATASET_REF_SCAN_CHARS
#: The cut signatures below are EXACT and CONTEXTUAL: each connector's partial
#: shape is matched only where that connector puts it (the parsed payload
#: root, or directly under a property key of a SiteWise history map) and only
#: with its full key set or envelope, so a complete business record that
#: happens to carry ``partial``/``returned`` or ``status``/``truncated``/
#: ``reason`` keys is never read as a cut. Shape
#: matching, not a tag added at the source: a tag would change the payload
#: every connector returns to the system agent's model and every test pinning
#: it, while the two SiteWise shapes already carry engine-built fixed text and
#: the bounded shapes their own envelopes. ``_adt_*`` members are engine
#: metadata (``_adt_source``, ``_adt_executed_query``) and are ignored.
#:
#: SiteWise asset-list cuts (``sitewise_read._finalize``, comma-joined
#: ``reason``): ``max_assets`` stops the walk at the asset cap and
#: ``read_failed`` leaves out every asset under a branch whose children could
#: not be read. Either way assets are missing from the list.
_LIST_CUT_REASONS = frozenset({"max_assets", "read_failed"})
#: The full ``list_associated_assets`` result when ``_finalize`` marked it
#: partial (``note`` is optional: the depth-limit note).
_ASSOCIATED_PARTIAL_KEYS = frozenset({
    "assets", "truncated", "reason", "direction", "root", "requested_max_depth",
    "requested_max_assets", "skipped", "skipped_count", "below_depth_limit",
    "status", "partial_note"})
#: The full ``sitewise_read._history_partial`` outcome, the ONE builder of a
#: history window cut; its note always opens ``Partial history (<reason>): ``.
_HISTORY_PARTIAL_KEYS = frozenset({
    "status", "truncated", "reason", "covered_start", "covered_end",
    "requested_start", "requested_end", "note", "points"})
#: A history read returns at most ``sitewise_read.HISTORY_MAX_ENTRIES`` (128)
#: keys; a larger map is not a history result and is not walked.
_MAX_HISTORY_OUTCOMES = 256
#: Reasons that never mean rows or a window were left out: a column cap keeps
#: every row, and a response/source cut has no row semantics the engine knows.
_NOT_ROW_CUT_REASONS = frozenset({"column_limit", "response_limit", "source_partial"})


def _kept_rows(node: dict) -> int | None:
    count = _safe_count(node.get("row_count"))
    if not isinstance(count, int):
        rows = node.get("rows")
        count = len(rows) if isinstance(rows, list) else None
    return count


def _own_keys(node: dict) -> set[str]:
    return {key for key in map(str, node) if not key.startswith("_adt_")}


def _bounded_cut(payload: dict) -> bool:
    """An engine bounded result (PostgreSQL, Athena, Tulip, REST/MCP through
    ``bounded_query_result``) at the payload root, cut by rows or by page."""
    from .response_integrity_query import ROW_CUT_REASONS
    reason = payload.get("partial_reason")
    return (payload.get("truncated") is True
            and isinstance(payload.get("columns"), list)
            and isinstance(payload.get("rows"), list)
            and isinstance(reason, str) and reason in ROW_CUT_REASONS)


def _associated_cut(payload: dict) -> bool:
    """A SiteWise ``list_associated_assets`` result at the payload root that
    ``_finalize`` marked partial for an asset-cap or branch-read cut."""
    if _own_keys(payload) - {"note"} != _ASSOCIATED_PARTIAL_KEYS:
        return False
    reason = payload.get("reason")
    parts = reason.split(",") if isinstance(reason, str) else []
    return (payload.get("truncated") is True and payload.get("status") == "partial"
            and bool(parts) and len(set(parts)) == len(parts)
            and set(parts) <= _LIST_CUT_REASONS
            and isinstance(payload.get("assets"), list)
            and isinstance(payload.get("skipped"), list)
            and isinstance(payload.get("partial_note"), str))


def _history_window_cut(outcome: Any) -> bool:
    """One property's outcome is exactly ``sitewise_read._history_partial``."""
    if not isinstance(outcome, dict) or _own_keys(outcome) != _HISTORY_PARTIAL_KEYS:
        return False
    reason, note = outcome.get("reason"), outcome.get("note")
    return (outcome.get("status") == "partial" and outcome.get("truncated") is True
            and isinstance(reason, str) and bool(reason) and "," not in reason
            and reason not in _NOT_ROW_CUT_REASONS
            and isinstance(outcome.get("points"), list)
            and isinstance(note, str) and note.startswith(f"Partial history ({reason}): ")
            and all(outcome.get(key) is None or isinstance(outcome.get(key), str)
                    for key in ("covered_start", "covered_end",
                                "requested_start", "requested_end")))


def _history_map_cut(payload: dict) -> bool:
    """The payload root is a SiteWise history map (every non-metadata value a
    list of points or a dict outcome, at most ``_MAX_HISTORY_OUTCOMES``) and
    one property's outcome directly under its key is a window cut."""
    outcomes = [value for key, value in payload.items()
                if not str(key).startswith("_adt_")]
    if not outcomes or len(outcomes) > _MAX_HISTORY_OUTCOMES:
        return False
    if not all(isinstance(value, (list, dict)) for value in outcomes):
        return False
    return any(_history_window_cut(value) for value in outcomes)


def _scan_result(result: Any) -> tuple[bool, int | None, dict | None]:
    """``(cut, rows_kept, repeated_rows)`` for one tool result: the shape
    checks of :func:`result_row_cut`, plus the ``repeated_rows`` fact an
    engine bounded result carries when most of its kept rows are exact copies
    (``response_integrity_query.with_repeat_note``). One parse per result."""
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        if not isinstance(payload, dict):
            continue
        if _bounded_cut(payload):
            return True, _kept_rows(payload), _repeated_rows_fact(payload)
        if _associated_cut(payload):
            return True, None, None
        if _history_map_cut(payload):
            return True, None, None
    return False, None, None


def _repeated_rows_fact(payload: dict) -> dict | None:
    """The validated ``repeated_rows`` of a bounded cut, or None. Both counts
    must be plain integers with at least half the rows copies, so nothing a
    model wrote into the payload reaches the engine line."""
    from .response_integrity_query import REPEATED_ROWS_KEY
    fact = payload.get(REPEATED_ROWS_KEY)
    if not isinstance(fact, dict):
        return None
    rows, distinct = fact.get("rows"), fact.get("distinct_rows")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (rows, distinct)):
        return None
    if distinct < 1 or distinct * 2 > rows:
        return None
    return {"rows": rows, "distinct_rows": distinct}


def result_row_cut(result: Any) -> tuple[bool, int | None]:
    """``(cut, rows_kept)`` for one tool result.

    Checks the parsed payload ROOT against each connector's exact partial
    shape: an engine bounded result (rows kept = ``row_count``), a SiteWise
    asset list, or a SiteWise
    history map with a window cut under a property key. ``rows_kept`` is None
    when the shape has no row count (a window or asset-list cut) — never
    estimated. Reads only the shape keys; no value leaves the payload.
    """
    cut, rows_kept, _repeat = _scan_result(result)
    return cut, rows_kept


def _valid_cut_tally(tally: Any) -> bool:
    if not isinstance(tally, dict):
        return False
    results, cut = tally.get("results"), tally.get("cut")
    return (isinstance(results, int) and not isinstance(results, bool)
            and isinstance(cut, int) and not isinstance(cut, bool)
            and 0 <= cut <= results and isinstance(tally.get("rows"), list))


def record_result_cut(invocation_state: dict, result: Any) -> None:
    """Count one completed tool result into the turn's cut tally."""
    tally = invocation_state.get(RESULT_CUTS_STATE_KEY)
    if not _valid_cut_tally(tally):
        tally = {"results": 0, "cut": 0, "rows": []}
        invocation_state[RESULT_CUTS_STATE_KEY] = tally
    cut, rows_kept, repeat = _scan_result(result)
    tally["results"] += 1
    if cut:
        tally["cut"] += 1
        if len(tally["rows"]) < _MAX_CUT_ROWS:
            tally["rows"].append(rows_kept)
        if repeat is not None:
            repeats = tally.get("repeats")
            if not isinstance(repeats, list):
                repeats = tally["repeats"] = []
            if len(repeats) < _MAX_CUT_ROWS:
                repeats.append(repeat)
    record_aggregate_check(invocation_state, result)


#: Per-turn record of the direct SQL results that carried an
#: ``aggregate_check`` (``aggregate_guard``): ``[{"aggregates": [entries]}]``
#: in order. It feeds ``flagged_totals`` (sentences on the same engine line
#: as the cut tally). Invocation-state keyed and not reset by the elicitation
#: retry, like the cut tally.
AGGREGATE_CHECKS_STATE_KEY = "_adt_aggregate_checks"
_MAX_AGGREGATE_RECORDS = 64
_MAX_AGGREGATE_ENTRIES = 16
_MAX_REPEAT_FACTS = 8
_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{16}$")
_AGGREGATE_LABEL = re.compile(r"^[A-Za-z0-9_$.,*() ]{1,160}$")
_AGGREGATE_CLAUSES = frozenset({"select", "having", "order by", "subquery"})


def _valid_repeat(fact: Any) -> dict | None:
    if not isinstance(fact, dict):
        return None
    table, key = fact.get("table"), fact.get("key")
    rows, distinct = fact.get("rows"), fact.get("distinct")
    if not (isinstance(table, str) and _PLAIN_IDENTIFIER.match(table)):
        return None
    if not (key == "" or (isinstance(key, str) and _PLAIN_IDENTIFIER.match(key))):
        return None
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (rows, distinct)):
        return None
    if not 1 <= distinct < rows:
        return None
    return {"table": table, "key": key, "rows": rows, "distinct": distinct}


def _valid_entry(entry: Any) -> dict | None:
    """One validated aggregate entry: a known status, a server-built hex
    signature, a plain label and plain repeat facts, so nothing a model wrote
    into a payload reaches the engine line or clears a flag."""
    from .aggregate_guard import STATUSES
    if not isinstance(entry, dict) or entry.get("status") not in STATUSES:
        return None
    signature, label = entry.get("signature"), entry.get("label")
    if not (isinstance(signature, str) and _SIGNATURE.match(signature)):
        return None
    if not (isinstance(label, str) and _AGGREGATE_LABEL.match(label)):
        return None
    clause = entry.get("clause", "select")
    if clause not in _AGGREGATE_CLAUSES:
        return None
    out = {"status": entry["status"], "signature": signature, "label": label,
           "clause": clause}
    raw = entry.get("repeats")
    if raw is not None:
        if not isinstance(raw, list):
            return None
        facts = [_valid_repeat(f) for f in raw[:_MAX_REPEAT_FACTS]]
        if any(f is None for f in facts):
            return None
        out["repeats"] = facts
    return out


def _aggregate_entries(result: Any) -> list[dict]:
    """The validated aggregate entries one SQL result carries; ``[]`` when it
    carries none (absence never clears a flag)."""
    from .aggregate_guard import AGGREGATE_CHECK_KEY
    for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
        if not (isinstance(payload, dict) and isinstance(payload.get("rows"), list)):
            continue
        check = payload.get(AGGREGATE_CHECK_KEY)
        raw = check.get("aggregates") if isinstance(check, dict) else None
        if not isinstance(raw, list):
            return []
        return [e for e in (_valid_entry(x) for x in raw[:_MAX_AGGREGATE_ENTRIES]) if e]
    return []


def record_aggregate_check(invocation_state: dict, result: Any) -> None:
    """Append one completed tool result's aggregate entries to the turn's
    record (nothing when it carries none)."""
    try:
        entries = _aggregate_entries(result)
    except Exception as exc:  # nosec B110 - the record is best-effort
        logger.debug("aggregate record failed (%s)", type(exc).__name__)
        return
    if not entries:
        return
    checks = invocation_state.get(AGGREGATE_CHECKS_STATE_KEY)
    if not isinstance(checks, list):
        checks = invocation_state[AGGREGATE_CHECKS_STATE_KEY] = []
    if len(checks) < _MAX_AGGREGATE_RECORDS:
        checks.append({"aggregates": entries})


def flagged_totals(invocation_state: Any) -> tuple[list[dict], list[dict]]:
    """``(repeated, unchecked)`` entries still flagged at the end of the turn.

    The record is replayed in order. A ``repeated`` or ``unchecked`` entry
    flags its signature (a ``repeated`` flag is never weakened to
    ``unchecked``); ONLY a later ``verified`` entry with the SAME signature
    clears it (the same total over the same columns, filters and grouping,
    now counted once). A result with no entry, a skipped or timed-out check,
    a ``weighted`` entry or another total never clears one. An entry for a
    total inside a scalar subquery (``clause`` ``subquery``) is recorded for
    the trace only: it neither flags nor clears."""
    from .aggregate_guard import REPEATED, UNCHECKED, VERIFIED
    checks = (invocation_state.get(AGGREGATE_CHECKS_STATE_KEY)
              if isinstance(invocation_state, dict) else None)
    flagged: dict[str, dict] = {}
    for record in checks if isinstance(checks, list) else ():
        entries = record.get("aggregates") if isinstance(record, dict) else None
        for raw in entries if isinstance(entries, list) else ():
            entry = _valid_entry(raw)
            if entry is None or entry["clause"] == "subquery":
                continue
            signature, status = entry["signature"], entry["status"]
            if status == VERIFIED:
                flagged.pop(signature, None)
            elif status == REPEATED or (status == UNCHECKED and flagged.get(
                    signature, {}).get("status") != REPEATED):
                flagged[signature] = entry
    values = list(flagged.values())
    return ([e for e in values if e["status"] == REPEATED],
            [e for e in values if e["status"] == UNCHECKED])


def _repeat_text(fact: dict) -> str:
    if fact["key"]:
        return (f"in {fact['table']}, {fact['rows']} joined rows carry only "
                f"{fact['distinct']} distinct {fact['key']} values")
    return (f"in {fact['table']}, {fact['rows']} joined rows are only {fact['distinct']} "
            "distinct rows")


def unverified_total_sentence(invocation_state: Any) -> str:
    """The engine sentences for the turn's flagged totals; ``""`` when none.
    Counts, table, column and key names only."""
    repeated, unchecked = flagged_totals(invocation_state)
    parts = []
    if repeated:
        shown = "; ".join(
            f"{e['label']} ({'; '.join(_repeat_text(f) for f in e['repeats'])})"
            if e.get("repeats") else e["label"] for e in repeated)
        parts.append(
            f"A total this turn is unverified: {shown}. That SUM, COUNT or AVG was computed "
            "over a JOIN in which the rows being totaled repeat, so it counts some of them "
            "more than once, and no later result this turn verified the same total. Do not "
            "report that total, or a ranking or selection made by it; ask for it again with "
            "that table totaled before the join, or joined so that each of its rows matches "
            "once.")
    if unchecked:
        labels = "; ".join(dict.fromkeys(e["label"] for e in unchecked))
        parts.append(
            f"A total this turn could not be checked for repeated rows: {labels}. If the "
            "answer states that total, ask once for it in a form the engine can check: the "
            "summed table totaled before the join, or joined on its own key.")
    return " ".join(parts)


def engine_result_line(invocation_state: Any) -> str:
    """The ONE engine line a Mode A answer carries: the cut line
    (:func:`result_cut_line`) and the unverified-total sentence
    (:func:`unverified_total_sentence`), whichever apply; ``""`` when neither."""
    cut = result_cut_line(invocation_state)
    total = unverified_total_sentence(invocation_state)
    if cut and total:
        return f"{cut} {total}"
    if total:
        return f"[engine] {total}"
    return cut


#: Closing sentence of the engine line when no row count can be stated (a
#: window or asset-list cut, possibly with nothing returned): it qualifies the
#: returned data, whatever and however little that is.
INCOMPLETE_FROM_RETURNED = (
    "A list of distinct values, a count, or an earliest or latest value read "
    "off the returned data is not the full result.")


def result_cut_line(invocation_state: Any) -> str:
    """The fixed engine line for a turn whose tool results were cut.

    ``""`` when nothing was cut (or the tally is absent/malformed), so an
    uncut answer stays byte-identical. Engine-written, never model text: it
    states how many of the turn's tool results were cut and how many rows
    each kept, then the same sentence the ``partial_note`` uses
    (``INCOMPLETE_FROM_ROWS``). No data value, so it is safe in every
    non-enforce mode. A row count is printed only when every cut's count is
    known; otherwise (a window or asset-list cut, or a count the result did
    not carry) the line says the full rows or time window did not come back
    and closes on ``INCOMPLETE_FROM_RETURNED`` rather than guess a count.
    """
    from .response_integrity_query import INCOMPLETE_FROM_ROWS
    tally = (invocation_state.get(RESULT_CUTS_STATE_KEY)
             if isinstance(invocation_state, dict) else None)
    if not _valid_cut_tally(tally) or tally["cut"] < 1:
        return ""
    cut, results, kept = tally["cut"], tally["results"], tally["rows"]
    known = [r for r in kept
             if isinstance(r, int) and not isinstance(r, bool) and r >= 0]
    if len(kept) == cut and len(known) == cut:
        distinct = list(dict.fromkeys(known))
        if len(distinct) == 1:
            came_back = f"{distinct[0]} rows came back{' from each' if cut > 1 else ''}"
        else:
            listed = ", ".join(str(r) for r in distinct[:-1])
            came_back = f"{listed} and {distinct[-1]} rows came back"
        body = f"{came_back} and the rows after them were not returned."
        closing = INCOMPLETE_FROM_ROWS
    else:
        # No count to state: a window or asset-list cut, which may have
        # returned nothing at all, so say only that the full scope did not
        # come back (true at zero) and qualify whatever data was returned.
        body = "the full rows or time window did not come back."
        closing = INCOMPLETE_FROM_RETURNED
    verb = "was" if cut == 1 else "were"
    line = (f"[engine] {cut} of {results} tool results this turn {verb} cut off: "
            f"{body} {closing}")
    repeat = repeat_sentence(tally.get("repeats"), cut)
    return f"{line} {repeat}" if repeat else line


def repeat_sentence(repeats: Any, cut: int) -> str:
    """The engine line's sentence for cut results whose kept rows are mostly
    exact copies (``record_result_cut`` collects their ``repeated_rows``
    facts); ``""`` when there are none, so every other line stays
    byte-identical. It states the counts and what follows IF the rows being
    totaled should each appear once, which the engine cannot know."""
    if not isinstance(repeats, list):
        return ""
    facts: list[str] = []
    for fact in repeats:
        valid = (_repeated_rows_fact({"repeated_rows": fact})
                 if isinstance(fact, dict) else None)
        if valid is None:
            continue
        text = (f"{valid['rows'] - valid['distinct_rows']} of the {valid['rows']} rows "
                "kept are exact copies of another row")
        if text not in facts:
            facts.append(text)
    if not facts:
        return ""
    which = "that result" if cut == 1 else (
        "one of them" if len(facts) == 1 else "some of them")
    return (f"In {which}, {'; '.join(facts)}. If the rows being totaled or counted "
            "should each appear once, a total or count over the same joins, also one "
            "from a separate aggregate query, counts some of them more than once; "
            "check that total at the grain of those records.")


class InvocationToolResultCapture:
    """Strands hook retaining only bounded, safe, invocation-local metadata."""

    def __init__(self, max_results: int = TOOL_RESULT_CAPTURE_MAX,
                 max_chars: int = TOOL_RESULT_CAPTURE_CHARS, *,
                 system_id: str = "", boundary: str = "external",
                 physical_schema: dict[str, dict[str, str]] | None = None,
                 registered_tools: set[str] | None = None,
                 read_kinds: dict[str, str] | None = None):
        self.max_results = max(1, int(max_results))
        self.max_chars = max(16, int(max_chars))
        self.system_id = str(system_id)[:100]
        self.boundary = boundary
        self.physical_schema = physical_schema or {}
        self.registered_tools = None if registered_tools is None else set(registered_tools)
        #: ``{tool name: declared read kind}`` (:func:`declared_read_kinds`).
        self.read_kinds = dict(read_kinds or {})

    def register_hooks(self, registry, **kwargs) -> None:
        from strands.hooks import (
            AfterToolCallEvent,
            BeforeInvocationEvent,
            BeforeToolCallEvent,
        )

        registry.add_callback(BeforeInvocationEvent, self._on_invocation_start)
        # Wave 6 (W6-F3): the SAME hook object additionally rides
        # BeforeToolCallEvent for trace-step timing — the hooks LIST in
        # system_agent.py stays untouched (indices [0]/[1] are pinned).
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    def _on_invocation_start(self, event) -> None:
        from .response_integrity_capture import initialize_integrity_state
        event.invocation_state[TOOL_RESULTS_STATE_KEY] = []
        initialize_integrity_state(event.invocation_state)

    def _event_tool_name(self, event) -> str:
        """The raw tool name for one Before/After event (selected tool first,
        the tool_use spelling second, 'tool' last) — one derivation for both
        callbacks so the registered-tools filter can never drift."""
        selected = getattr(event, "selected_tool", None)
        return str(
            getattr(selected, "tool_name", None)
            or getattr(selected, "name", None)
            or (event.tool_use.get("name") if isinstance(event.tool_use, dict) else None)
            or "tool"
        )

    def _on_before_tool_call(self, event) -> None:
        """Wave 6 (W6-F3): stamp the monotonic start for this toolUseId so
        the After event can record an honest ``duration_ms``; and open the
        call's read-count slot (:func:`open_read_slot`). Own guard: a trace
        bug must never break the tool call."""
        try:
            if (self.registered_tools is not None
                    and self._event_tool_name(event) not in self.registered_tools):
                return
            open_read_slot()
            from . import investigation_trace
            investigation_trace.record_step_start(
                event.invocation_state, getattr(event, "tool_use", None))
        except Exception as exc:  # nosec B110 - trace capture is best-effort
            logger.debug("trace step-start capture failed (%s)", type(exc).__name__)

    def _record_trace_step(self, event, records_read: "int | None" = None) -> None:
        """Wave 6 (W6-F3): delegate ONE bounded trace-step record to the pure
        module (both branches: exception and success), with the count the
        call's tool reported (:meth:`_record_api_read`). Own guard so a trace
        bug never breaks the Wave-5 metadata path; only the exception TYPE
        NAME may reach a log line."""
        try:
            from . import investigation_trace
            investigation_trace.record_step(
                event.invocation_state,
                tool_use=event.tool_use if isinstance(event.tool_use, dict) else {},
                result=getattr(event, "result", None),
                exception=getattr(event, "exception", None),
                retry=getattr(event, "retry", False),
                messages=getattr(getattr(event, "agent", None), "messages", None) or [],
                records_read=records_read,
            )
        except Exception as exc:  # nosec B110 - trace capture is best-effort
            logger.debug("trace step capture failed (%s)", type(exc).__name__)

    def _record_identifier_sources(self, event) -> None:
        """Keep the values of each result column whose source column the
        executed SQL's SELECT list proves (``identifier_provenance.
        record_result``): this call's results only. A call that raised, is
        being retried or was cancelled returned nothing. Own guard."""
        if (getattr(event, "exception", None) is not None or getattr(event, "retry", False)
                or getattr(event, "cancel_message", None)):
            return
        try:
            from . import identifier_provenance
            result = getattr(event, "result", None)
            sql = _trusted_executed_query(result, "", _DATASET_REF_SCAN_CHARS)
            if not sql:
                return
            for payload in _payload_candidates(result, _CUT_SCAN_CHARS):
                if isinstance(payload, dict) and isinstance(payload.get("rows"), list) \
                        and "error" not in payload:
                    identifier_provenance.record_result(
                        event.invocation_state, sql, payload, self.physical_schema)
                    return
        except Exception as exc:  # nosec B110 - the identifier-source record is best-effort
            logger.debug("identifier sources record failed (%s)", type(exc).__name__)

    def _on_after_tool_call(self, event) -> None:
        raw_name = self._event_tool_name(event)
        if self.registered_tools is not None and raw_name not in self.registered_tools:
            return
        records_read = self._record_api_read(event, raw_name)
        # A retried attempt's result is discarded and the tool called again
        # (strands AfterToolCallEvent.retry): no typed evidence, metadata or
        # sidecar row is taken from it, so no reader sees a discarded result.
        if not getattr(event, "retry", False):
            self._retain(event.invocation_state, self._attempt_metadata(event, raw_name))
        self._record_result_cut(event)
        self._record_key_rows(event)
        self._record_result_values(event)
        self._record_answer_names(event)
        self._record_answer_ids(event)
        self._record_result_identifiers(event)
        self._record_wrong_form(event)
        # Wave 6: bounded trace-step record (W6-F3) — AFTER the Wave-5
        # metadata work, independently guarded, both branches.
        self._record_trace_step(event, records_read)
        self._record_telemetry_coverage(event)
        self._record_identifier_sources(event)

    def _attempt_metadata(self, event, raw_name: str) -> dict:
        """The bounded metadata of one kept attempt: a fixed error record for
        a call that raised, else :meth:`_success_metadata`."""
        if getattr(event, "exception", None) is None:
            return self._success_metadata(event, raw_name)
        return {
            "tool": raw_name if _SAFE_TOOL_NAME.fullmatch(str(raw_name)) else "tool",
            "transport_status": "error",
            "app_status": "error",
            "safe_error_category": "tool_exception",
            "_has_success": False,
        }

    def _record_api_read(self, event, raw_name: str) -> "int | None":
        """Keep the count a declared data tool reported for this call
        (:func:`record_api_read`), for a kept call only: not retried, not
        raised, not cancelled, not returned as an error. Always closes the
        call's slot. Returns the count a kept call of a tool that declares a
        read (data or discovery) reported, else None, for the call's trace
        step (:meth:`_record_trace_step`). Own guard: a bug here never breaks
        the other paths."""
        try:
            count = close_read_slot()
            result = getattr(event, "result", None)
            kind = self.read_kinds.get(raw_name)
            if (kind not in _READ_KINDS or getattr(event, "retry", False)
                    or getattr(event, "exception", None) is not None
                    or getattr(event, "cancel_message", None)
                    or (isinstance(result, dict) and result.get("status") == "error")):
                return None
            if kind == DATA_READ:
                record_api_read(event.invocation_state, getattr(event, "tool_use", None),
                                count)
            return count
        except Exception as exc:  # nosec B110 - the read count is best-effort
            logger.debug("api read record failed (%s)", type(exc).__name__)
            return None

    def _retain(self, invocation_state: dict, metadata: dict) -> None:
        """Keep one attempt's metadata within ``max_results``, successes
        preferred: a success evicts the oldest non-success, and a non-success
        is kept only while there is room or nothing kept succeeded."""
        captured = invocation_state.setdefault(TOOL_RESULTS_STATE_KEY, [])
        if metadata.get("_has_success"):
            captured.append(metadata)
            if len(captured) > self.max_results:
                remove = next((i for i, item in enumerate(captured) if not item.get("_has_success")), 0)
                captured.pop(remove)
        elif len(captured) < self.max_results:
            captured.append(metadata)
        elif not any(item.get("_has_success") for item in captured):
            captured.pop(0)
            captured.append(metadata)

    def _record_result_cut(self, event) -> None:
        """Count this completed tool result into the turn's row/page-cut
        tally (``RESULT_CUTS_STATE_KEY``). A call that raised or is being
        retried returned no result the answer rests on, so it is not counted.
        Own guard: a tally bug never breaks the metadata or trace paths."""
        if getattr(event, "exception", None) is not None or getattr(event, "retry", False):
            return
        try:
            record_result_cut(event.invocation_state, getattr(event, "result", None))
        except Exception as exc:  # nosec B110 - the cut tally is best-effort
            logger.debug("result cut tally failed (%s)", type(exc).__name__)

    def _record_key_rows(self, event) -> None:
        """Keep this result's rows that hold a record key, for the listed-key
        proof (``listed_keys.record_key_rows``): the call's own rows only. A
        call that raised or is being retried returned nothing the answer
        rests on. Own guard: a bug here never breaks the other paths."""
        if getattr(event, "exception", None) is not None or getattr(event, "retry", False):
            return
        try:
            from . import listed_keys
            for payload in _payload_candidates(getattr(event, "result", None),
                                               _CUT_SCAN_CHARS):
                if (isinstance(payload, dict) and isinstance(payload.get("columns"), list)
                        and isinstance(payload.get("rows"), list)):
                    listed_keys.record_key_rows(event.invocation_state, payload)
                    return
        except Exception as exc:  # nosec B110 - the key-row record is best-effort
            logger.debug("listed key rows failed (%s)", type(exc).__name__)

    def _record_result_values(self, event) -> None:
        """Keep every number this result returned, and a snapshot of the
        agent's conversation so far, for the check that a total the answer
        states is in a result (``answer_figures``). A call that raised or is
        being retried returned nothing the answer rests on. Neither did a call
        a hook cancelled (``cancel_message``: strands puts its text in place
        of a result): its numbers are not recorded, and only the conversation
        snapshot is kept. Own guard: a fault here marks the record unread, so
        no line claims an absence it did not check, and never breaks the
        other paths."""
        if getattr(event, "exception", None) is not None or getattr(event, "retry", False):
            return
        try:
            from . import answer_figures
            if not getattr(event, "cancel_message", None):
                answer_figures.record_result_values(event.invocation_state,
                                                    getattr(event, "result", None))
            agent = getattr(event, "agent", None)
            answer_figures.keep_context(event.invocation_state, getattr(agent, "messages", None),
                                        getattr(agent, "system_prompt", None))
        except Exception as exc:  # nosec B110 - the value record is best-effort
            logger.debug("result values failed (%s)", type(exc).__name__)
            try:
                from . import answer_figures
                answer_figures.mark_unread(event.invocation_state)
            except Exception:  # nosec B110 - without the module no figure line exists
                pass

    def _record_answer_names(self, event) -> None:
        """Note which of the agent's schema example names this result returns
        as data (``answer_names.record_result``): this call's results only,
        in the call's own invocation state. A call that raised or is being
        retried returned nothing the answer rests on. Own guard."""
        if getattr(event, "exception", None) is not None or getattr(event, "retry", False):
            return
        try:
            from . import answer_names
            names = getattr(getattr(event, "agent", None), answer_names.SCHEMA_NAMES_ATTR, None)
            if isinstance(names, tuple) and names:
                answer_names.record_result(
                    event.invocation_state, getattr(event, "result", None), names)
        except Exception as exc:  # nosec B110 - the answer-names record is best-effort
            logger.debug("answer names record failed (%s)", type(exc).__name__)

    def _record_answer_ids(self, event) -> None:
        """Keep the identifier tokens with a schema example lead that this
        result returns as data (``answer_ids.record_result``): this call's
        results only, in the call's own invocation state. A call that raised
        or is being retried returned nothing the answer rests on. Own guard."""
        if getattr(event, "exception", None) is not None or getattr(event, "retry", False):
            return
        try:
            from . import answer_ids
            leads = getattr(getattr(event, "agent", None), answer_ids.SCHEMA_LEADS_ATTR, None)
            if isinstance(leads, frozenset) and leads:
                answer_ids.record_result(
                    event.invocation_state, getattr(event, "result", None), leads)
        except Exception as exc:  # nosec B110 - the answer-ids record is best-effort
            logger.debug("answer ids record failed (%s)", type(exc).__name__)

    def _record_result_identifiers(self, event) -> None:
        """Keep the identifier tokens this result's proven tabular row cells
        hold (``result_identifiers.record_result``), with no schema example
        lead needed: this call's results only, in the call's own invocation
        state. A call that raised, is being retried or was cancelled returned
        nothing the answer rests on. Own guard: a fault adds nothing."""
        if (getattr(event, "exception", None) is not None or getattr(event, "retry", False)
                or getattr(event, "cancel_message", None)):
            return
        try:
            from . import result_identifiers
            result_identifiers.record_result(
                event.invocation_state, getattr(event, "result", None), self.physical_schema)
        except Exception as exc:  # noqa: BLE001 - the result-identifiers record is best-effort
            logger.debug("result identifiers record failed (%s)", type(exc).__name__)

    def _record_wrong_form(self, event) -> None:
        """Keep the shape guard's wrong-form refusals of this call, and the
        measured pairs a result row holds both forms of
        (``unresolved_miss.record_result``), with the guard context the agent
        keeps. A call that raised, is being retried or was cancelled returned
        nothing the answer rests on. Own guard: a fault marks the call unread,
        so no line claims what was not checked."""
        if (getattr(event, "exception", None) is not None or getattr(event, "retry", False)
                or getattr(event, "cancel_message", None)):
            return
        try:
            from . import unresolved_miss
            context = getattr(getattr(event, "agent", None), unresolved_miss.SHAPES_ATTR, None)
            if context is not None:
                unresolved_miss.record_result(
                    event.invocation_state, getattr(event, "result", None), context,
                    self.physical_schema)
        except Exception as exc:  # nosec B110 - the refusal record is best-effort
            logger.debug("wrong-form record failed (%s)", type(exc).__name__)
            try:
                from . import unresolved_miss
                unresolved_miss.mark_unread(event.invocation_state)
            except Exception:  # nosec B110 - without the module no line exists
                pass

    def _record_telemetry_coverage(self, event) -> None:
        """Deterministic telemetry-coverage lines (names/counts/timestamps
        only — ``agents/telemetry_coverage.py``) for the out-of-band trace
        write in ``_run_mode_a_query``. Success path only; own guard so a
        coverage bug never breaks the metadata or trace-step paths."""
        if getattr(event, "exception", None) is not None or getattr(event, "retry", False):
            return
        try:
            from . import telemetry_coverage
            telemetry_coverage.record_coverage(
                event.invocation_state, getattr(event, "tool_use", None),
                getattr(event, "result", None))
        except Exception as exc:  # nosec B110 - coverage capture is best-effort
            logger.debug("telemetry coverage capture failed (%s)", type(exc).__name__)
        from . import telemetry_window  # the requested window, for its engine line
        telemetry_window.record_window(event.invocation_state, getattr(event, "tool_use", None),
                                       getattr(event, "result", None))

    def _success_metadata(self, event, raw_name: str) -> dict:
        """Non-exception path: trusted evidence capture + safe metadata —
        the extracted body of ``_on_after_tool_call``'s else branch, plus the
        Wave-5 sidecar-statement retention (same trusted-capture inputs).
        Never called for a retried attempt (:meth:`_on_after_tool_call`)."""
        from .response_integrity import capture_trusted_tool_result
        tool_input = event.tool_use.get("input", {}) if isinstance(event.tool_use, dict) else {}
        executed_query = tool_input.get("sql", "") if isinstance(tool_input, dict) else ""
        executed_query = _trusted_executed_query(
            event.result, executed_query, max(TOOL_RESULT_CAPTURE_CHARS, 200_000),
        )
        capture_trusted_tool_result(
            event.result,
            event.invocation_state,
            system_id=self.system_id,
            boundary=self.boundary,
            tool_name=str(raw_name),
            executed_query=executed_query if isinstance(executed_query, str) else "",
            physical_schema=self.physical_schema,
        )
        metadata = safe_tool_result_metadata(
            event.result, tool_name=str(raw_name), max_chars=self.max_chars
        )
        if isinstance(executed_query, str) and _is_discovery_query(executed_query):
            metadata[DISCOVERY_FLAG] = True
        # Wave 5: retain the bounded sidecar-statement row (trusted-capture
        # query text + projection entity + safe counts) for the out-of-band
        # provenance write in _run_mode_a_query. Runs in ALL integrity modes
        # (report-only populates the capture in shadow, like the state above).
        record_sidecar_statement(
            event.invocation_state,
            executed_query=executed_query if isinstance(executed_query, str) else "",
            metadata=metadata,
            physical_schema=self.physical_schema,
            result=event.result,
            call_id=event.tool_use.get("toolUseId") if isinstance(event.tool_use, dict) else None,
        )
        record_approximate_result(
            event.invocation_state,
            executed_query=executed_query if isinstance(executed_query, str) else "",
            result=event.result,
        )
        return metadata


def _public_metadata(items: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for item in items[-TOOL_RESULT_CAPTURE_MAX:]:
        if not isinstance(item, dict):
            continue
        public: dict[str, Any] = {}
        tool_name = item.get("tool")
        public["tool"] = tool_name if isinstance(tool_name, str) and _SAFE_TOOL_NAME.fullmatch(tool_name) else "tool"
        transport = item.get("transport_status")
        public["transport_status"] = transport if transport in {"success", "error", "unknown"} else "unknown"
        app_status = item.get("app_status")
        public["app_status"] = app_status if app_status in {"success", "partial", "error", "unknown"} else "unknown"
        category = item.get("safe_error_category")
        if category in _SAFE_ERROR_TYPES:
            public["safe_error_category"] = category
        for key in _COUNT_FIELDS:
            count = _safe_count(item.get(key))
            if count is not None:
                public[key] = count
        if isinstance(item.get("truncated"), bool):
            public["truncated"] = item["truncated"]
        out.append(public)
    return out


def agent_result_text(result: Any) -> str:
    """Extract normal assistant text, including citation text blocks."""
    message = getattr(result, "message", None)
    if not isinstance(message, dict):
        return ""
    content = message.get("content", [])
    if not isinstance(content, list):
        return str(content)[:8192].strip() if isinstance(content, str) else ""
    parts: list[str] = []
    size = 0
    for block in content[:64]:
        if not isinstance(block, dict):
            continue
        candidates = []
        if isinstance(block.get("text"), str):
            candidates.append(block["text"])
        citations = block.get("citationsContent", {}).get("content", [])
        if isinstance(citations, list):
            candidates.extend(
                citation["text"] for citation in citations[:32]
                if isinstance(citation, dict) and isinstance(citation.get("text"), str)
            )
        for text in candidates:
            remaining = 65536 - size
            if remaining <= 0:
                break
            fragment = text[:remaining]
            parts.append(fragment)
            size += len(fragment)
    return " ".join(parts).strip()


def _output_limit_message(metadata: list[dict], has_success: bool) -> dict[str, str]:
    """Fixed text for a ``max_tokens`` stop, by what the turn actually did:
    no tool attempt (the system was not reached), attempts that all failed or
    returned nothing usable (it was reached; say nothing about its health), or
    some successful work (partial)."""
    if has_success:
        return {"message": OUTPUT_LIMIT_PARTIAL_MESSAGE, "guidance": OUTPUT_LIMIT_GUIDANCE}
    if metadata:
        return {"message": OUTPUT_LIMIT_FAILED_MESSAGE, "guidance": OUTPUT_LIMIT_FAILED_GUIDANCE}
    return {"message": OUTPUT_LIMIT_MESSAGE, "guidance": OUTPUT_LIMIT_GUIDANCE}


def _incomplete_message(metadata: list[dict], has_success: bool,
                        stop_reason: str) -> dict[str, str]:
    """Fixed (never model-authored) ``message`` (+ ``guidance``) for an
    incomplete envelope. A ``max_tokens`` stop is the agent's OWN output
    outgrowing its cap, not a system failure: say so, so the caller narrows the
    ask instead of retrying it or reporting the system as down."""
    if stop_reason == "max_tokens":
        return _output_limit_message(metadata, has_success)
    return {"message": (
        "Agent completed some tool work but produced no final text."
        if has_success else
        "Agent produced no final text and no usable tool result containing successful data."
    )}


def extract_mode_a_result(result: Any, invocation_state: dict) -> str:
    """Return normal output or a typed, metadata-only incomplete envelope."""
    structured = getattr(result, "structured_output", None)
    if structured is not None:
        from .response_integrity import system_answer_from_result
        validated = system_answer_from_result(result, invocation_state)
        if validated is not None:
            return validated.model_dump_json()
    # Model-authored text is private in Mode A. Only the metadata-only fallback
    # below may cross the system-agent boundary.
    stop = getattr(result, "stop_reason", "unknown")
    stop_reason = stop if stop in {
        "end_turn", "max_tokens", "tool_use", "stop_sequence",
        "guardrail_intervened", "content_filtered", "unknown",
    } else "unknown"
    metadata = _public_metadata((invocation_state or {}).get(TOOL_RESULTS_STATE_KEY))
    has_success = any(item.get("app_status") in {"success", "partial"} for item in metadata)
    envelope: dict[str, Any] = {
        "status": "partial" if has_success else "error",
        "error_type": "incomplete",
        **_incomplete_message(metadata, has_success, stop_reason),
        "stop_reason": stop_reason,
    }
    if metadata:
        envelope["tool_results"] = metadata
    return json.dumps(envelope, separators=(",", ":"))
