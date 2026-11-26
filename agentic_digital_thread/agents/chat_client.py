# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Chat client abstraction — local agent built with Strands Agents or AgentCore Runtime."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from io import BytesIO
from typing import Any, Generator

import boto3
from strands.hooks import AfterToolCallEvent, HookProvider
from strands.hooks.registry import HookRegistry

from .config import get_config, mode_a_integrity_enforced
from .auth.forwarding import with_user_access_token
from .events import (
    DoneEvent, ErrorEvent, MessageEvent, MetadataEvent, ReasoningEvent,
    SSEEvent, ToolEndEvent, ToolResultEvent, ToolUseEvent,
    log_tool_result_envelope, now_ms, public_error_message,
)
from .orchestrator import build_orchestrator, drain_pending_charts
from .response_integrity import (
    MAX_PROGRESS_EVENTS, FinalAnswerDraft, IntegrityContext, capture_orchestrator_tool_result,
    gate_final_result,
)
from .query_intelligence import (
    build_intelligence_summary, format_rule_label, is_failed_result,
)
from .provenance import build_provenance_records, build_sidecar_entries, format_captured_at
from .provenance import enrich_records_from_sidecar
from .provenance import uncited_query_refs
from .tool_result_summary import summarize_tool_result as _summarize_tool_result_raw
from .usage_tracking import extract_rule_node_ids
from .metric_turn_render import without_reserved_results
from .failure_fingerprint import exception_fingerprint, format_fields, frame_label, redact_secrets


#: The RESERVED single key of a runtime keepalive heartbeat frame (mirrors
#: runtime_app.HEARTBEAT_KEY — the two run in SEPARATE processes). A heartbeat
#: is dropped before any byte/line/progress accounting and produces no SSE
#: event; it exists only to keep the 300 s socket read alive while the
#: orchestrator stream is idle.
HEARTBEAT_KEY = "adt_heartbeat"
#: The botocore socket read timeout for the orchestrator runtime stream. The
#: runtime's idle-heartbeat ceiling (runtime_app._HEARTBEAT_CEILING_SECONDS)
#: MUST stay safely BELOW this, or an idle turn could time out before the next
#: heartbeat and lose the answer — the exact bug this keepalive fixes. A test
#: pins ceiling < this value (test_runtime_stream_heartbeat). Do not raise one
#: without re-checking the other.
RUNTIME_STREAM_READ_TIMEOUT_S = 300
#: The EXACT heartbeat frame (mirrors runtime_app.HEARTBEAT_FRAME — separate
#: processes). A heartbeat is matched by exact equality, never by mere presence
#: of the reserved key, so a future additive runtime frame that reuses the key
#: alongside real content (e.g. ``{"adt_heartbeat": 1, "data": "answer"}``) is
#: NOT silently discarded.
HEARTBEAT_FRAME = {HEARTBEAT_KEY: 1}


def _is_heartbeat(data: object) -> bool:
    """True iff *data* is EXACTLY the heartbeat frame ``{HEARTBEAT_KEY: 1}``.

    Exact match (not ``KEY in data``): a mixed-key frame or the reserved key
    with any other value is a real frame and must flow through unchanged. The
    value must be the int ``1`` itself: Python equality would also accept
    ``True`` and ``1.0``."""
    if not (isinstance(data, dict) and data.keys() == HEARTBEAT_FRAME.keys()):
        return False
    value = data[HEARTBEAT_KEY]
    return type(value) is int and value == HEARTBEAT_FRAME[HEARTBEAT_KEY]


def _is_heartbeat_line(line: str) -> bool:
    """True iff a raw stream line is EXACTLY a heartbeat ``data:`` frame.

    Cheap substring pre-check, then an exact parse + exact-frame match so a
    real frame that merely mentions the token in free text, or carries the
    reserved key alongside real content, is never mistaken for a heartbeat."""
    if HEARTBEAT_KEY not in line or not line.startswith("data: "):
        return False
    try:
        return _is_heartbeat(json.loads(line[6:]))
    except (json.JSONDecodeError, ValueError):
        return False


def _log_generic_error_dict(data: dict, category: str = "unknown") -> None:
    """Fingerprint the runtime's raw error dict before it is scrubbed.

    The LAST uninstrumented origin of the generic "Query failed.": an 8-cell
    interleaved A/B produced 6 failures with the other three candidate sites
    instrumented and SILENT (2026-07-30), so the dict arrives here. The KEY SET
    is the useful part — it names the emitting path, which the scrubbed message
    cannot. Values are digit-masked (claim-check policy covers logs too).
    """
    logging.getLogger("agentcore_client").error(
        "GENERIC_ERROR_DICT keys=%s err_type=%s category=%s head=%s",
        sorted(data.keys())[:12], type(data.get("error")).__name__,
        category, _redact_digits(data.get("error")))


def _redact_digits(text: object, limit: int = 160) -> str:
    """First *limit* chars with credentials redacted and digit runs masked.

    Keeps WHICH error; drops measurements (an error string can quote telemetry
    values) and secrets (it can quote a DSN or a token)."""
    import re as _re
    return _re.sub(r"\d", "#", redact_secrets(str(text), limit))[:limit]


def _safe_cloud_error_category(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and isinstance(error.get("Code"), str):
            code = error["Code"]
    if code in {"AccessDeniedException", "UnauthorizedException"}:
        return "AUTHORIZATION"
    if code in {"ThrottlingException", "ServiceUnavailableException"}:
        return "TRANSIENT_SERVICE"
    if isinstance(exc, TimeoutError) or _is_botocore_timeout(exc):
        return "TIMEOUT"
    return "RUNTIME_UNAVAILABLE"


def _is_botocore_timeout(exc: Exception) -> bool:
    """A botocore read or connect timeout. Neither is a built-in
    ``TimeoutError`` (their bases are botocore's vendored requests/urllib3
    classes), so a read timeout in the middle of the runtime stream used to
    show the generic stream-error text instead of "timed out"."""
    try:
        from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
    except ImportError:  # pragma: no cover - botocore ships with boto3
        return False
    return isinstance(exc, (ReadTimeoutError, ConnectTimeoutError))


# ── Public error classification (closed taxonomy — events.ERROR_CATEGORIES) ──
# WORD-BOUNDARY regexes only (error-taxonomy invariant: never bare
# substrings — a data value quoted in an error string must not classify; e.g.
# an embedded token like ``CONNECTION_TIMEOUT`` has no word boundary before
# ``TIMEOUT`` because ``_`` is a word character). The classifier only ever runs
# on ERROR payloads — never on answer text. ORDER MATTERS: auth markers win
# (an MCP-client init failure often also mentions retries/timeouts), then the
# explicit MaxTokens→runtime_error pin, then timeout words.
_ERROR_TEXT_CLASSIFIERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Protocol/exception SHAPES, not bare words: an HTTP status phrase
    # ("403 Forbidden"), the strands MCP-client start failure, or a JWT
    # rejection — a quoted row value "Forbidden" alone never classifies.
    ("gateway_auth", re.compile(
        r"\b40[13]\s+(?:Forbidden|Unauthorized)\b"
        r"|\bFailed to start MCP client\b"
        r"|\bMCP client\b[^.]{0,60}\binitializ\w*\b"
        r"|\bUnauthorizedException\b|\bAccessDeniedException\b",
        re.IGNORECASE)),
    # The runtime's FIXED output-limit code (agents/output_cap_recovery.py):
    # an exact, case-sensitive token only the runtime emits.
    ("output_limit", re.compile(r"\bORCHESTRATOR_OUTPUT_LIMIT\b")),
    ("runtime_error", re.compile(r"\bMaxTokens\w*\b")),
    ("timeout", re.compile(
        r"\btimed[\s-]?out\b|\b(?:Read|Connect|Write|Pool)?Timeout(?:Error|Exception)\b"
        r"|\bquery_timeout\b|\berror_type\W{1,4}timeout\b",
        re.IGNORECASE)),
)


def classify_error_text(text: object) -> str:
    """Map an ERROR payload's text to the closed public taxonomy.

    Returns one of ``events.ERROR_CATEGORIES``; anything unmatched is
    ``runtime_error`` (the payload IS an error — 'unknown' is reserved for
    frames that never carried a classification at all).
    """
    haystack = str(text or "")
    for category, pattern in _ERROR_TEXT_CLASSIFIERS:
        if pattern.search(haystack):
            return category
    return "runtime_error"


#: _safe_cloud_error_category → public taxonomy. RUNTIME_UNAVAILABLE is the
#: internal catch-all, so its public mapping is site-specific (the *default*):
#: invoke/setup failures are 'runtime_unavailable', mid-stream read failures
#: are 'stream_error'.
_CLOUD_TO_PUBLIC_CATEGORY = {
    "AUTHORIZATION": "gateway_auth",
    "TRANSIENT_SERVICE": "runtime_unavailable",
    "TIMEOUT": "timeout",
}


def _public_cloud_category(exc: Exception, *, default: str) -> str:
    return _CLOUD_TO_PUBLIC_CATEGORY.get(_safe_cloud_error_category(exc), default)
# Configure the agentcore_client logger to output to stdout
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
logging.getLogger("agentcore_client").setLevel(logging.INFO)


# ── Tool result helpers ──────────────────────────────────────────────────────
# Summarization lives in the shared ``tool_result_summary`` module (single source
# of truth). Every render site goes through ``_summarize_tool_result`` below, which
# wraps the pure summarizer with a GENERIC, FAIL-SOFT id→human-name enrichment:
# any graph-known identifier (e.g. a SiteWise ``asset_id`` UUID / an ``event_id``)
# in the summary is annotated as ``Name (idprefix…)`` so the activity trace shows
# a human name instead of an opaque UUID. Fail-soft: if the graph is unreachable
# or no name is known, the summary is returned unchanged (see id_name_resolver).
def _summarize_tool_result(tool_name: str, result) -> str:
    summary = _summarize_tool_result_raw(tool_name, result)
    try:
        from .id_name_resolver import annotate_ids_in_text
        return annotate_ids_in_text(summary)
    except Exception:  # nosec B110 - enrichment is best-effort, never break a render
        return summary


# NOTE: chart delivery moved server-side. ChartRedactionHook
# (agents/orchestrator.py) redacts <chart-block .../> tags from the model's
# copy of tool results and queues them on the agent; the runtime stream loop
# (runtime_app) / the local drain below inject them as ordinary data tokens.
# The former client-side extraction/injection seam on the cloud stream state
# is RETIRED — tags now arrive as data tokens this client already forwards
# as MessageEvents, so dual injection is impossible by construction.


# ── SSE callback / hook (mirrored from server.py) ─────────────────────────────



def _local_queue_maxsize() -> int:
    """Bound of the LocalChatClient internal event queue
    (``LOCAL_STREAM_QUEUE_MAX``, default 512).

    Ordinary mode used to build this queue with ``maxsize=0`` — UNBOUNDED — and
    write it with a blocking ``put``, so an abandoned turn accumulated its whole
    remaining event stream in the app task's memory. Integrity mode
    keeps its own tighter bound (the progress-event budget plus terminal
    headroom). Malformed values fall back to 512."""
    try:
        return max(2, int(os.environ.get("LOCAL_STREAM_QUEUE_MAX", "512")))
    except (TypeError, ValueError):
        return 512


def _cancel_agent_quietly(agent) -> None:
    """Request cooperative cancellation on a strands ``Agent``; never fatal.

    ``Agent.cancel()`` is the REAL seam in the pinned ``strands-agents==1.42.0``:
    a thread-safe ``threading.Event`` the event loop checks at the
    model-streaming and pre-tool-execution checkpoints. An object without it is a
    WIRING bug, so it is logged rather than silently tolerated."""
    if agent is None:
        return
    log = logging.getLogger("agentcore_client")
    cancel = getattr(agent, "cancel", None)
    if cancel is None:
        log.warning("LOCAL_CANCEL_SEAM_MISSING type=%s — cannot request "
                    "cooperative cancellation", type(agent).__name__)
        return
    try:
        cancel()
    except Exception:  # nosec B110 - best-effort teardown; failure is non-fatal
        log.warning("local agent cancel request failed", exc_info=True)


class _LocalCancelGate:
    """Cancellation seam for ONE local turn — a closeable the shared
    :class:`StreamCancellation` arms, so both clients keep one handle type.

    The local path holds no HTTP body to close. What it does hold is a producer
    thread, a bounded queue and a strands ``Agent``. Closing this gate stops
    every local enqueue and requests cancellation on the bound agent.

    Honest guarantee: work already inside a tool call or a provider read is NOT
    interrupted — that thread finishes out-of-band. What is immediate and
    guaranteed is local: callback/hook production stops, the queue stays bounded,
    and the consumer returns without waiting the model out.
    """

    __slots__ = ("_event", "_lock", "_agent")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._agent = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def bind_agent(self, agent) -> None:
        """Publish this turn's agent — cancelling it NOW if the gate already
        closed (the build/first-checkpoint race)."""
        with self._lock:
            if not self._event.is_set():
                self._agent = agent
                return
        _cancel_agent_quietly(agent)

    def close(self) -> None:
        """Stop local production and request cooperative cancellation.

        Idempotent: the agent is POPPED under the lock, so its ``cancel()`` is
        requested at most once however many times this is called."""
        with self._lock:
            self._event.set()
            agent, self._agent = self._agent, None
        _cancel_agent_quietly(agent)


#: Poll slice for cancellation-aware local consumption.
_LOCAL_STOP_TICK = 0.25


def _put_bounded_local(target: queue.Queue, item: dict,
                       gate: '_LocalCancelGate | None', *,
                       terminal: bool = False) -> bool:
    """Apply bounded back-pressure until the item lands or cancellation wins."""
    while gate is None or not gate.cancelled:
        try:
            target.put(item, timeout=_LOCAL_STOP_TICK)
            return True
        except queue.Full:
            continue
    return False


def _put_local_event(
    target: queue.Queue, item: dict, integrity_context: IntegrityContext | None = None,
    *, gate: '_LocalCancelGate | None' = None, terminal: bool = False,
) -> bool:
    if gate is not None and gate.cancelled:
        return False
    if integrity_context is None:
        return _put_bounded_local(target, item, gate, terminal=terminal)
    if integrity_context.progress_events >= MAX_PROGRESS_EVENTS:
        integrity_context.mark_budget_exceeded()
        return False
    integrity_context.progress_events += 1
    try:
        target.put_nowait(item)
        return True
    except queue.Full:
        integrity_context.mark_budget_exceeded()
        return False


class _SSECallbackHandler:
    def __init__(
        self, q: queue.Queue, *, emit_tokens: bool = True,
        integrity_context: IntegrityContext | None = None,
        gate: '_LocalCancelGate | None' = None,
    ):
        self._q = q
        self._emit_tokens = emit_tokens
        self._integrity_context = integrity_context
        self._gate = gate
        self.tool_count = 0
        self._seen_tool_ids: set = set()
        self._active_tool_id: str | None = None
        self._active_tool_name: str | None = None

    def _emit(self, item: dict) -> bool:
        return _put_local_event(self._q, item, self._integrity_context,
                                gate=self._gate)

    def __call__(self, **kwargs: Any) -> None:
        data = kwargs.get("data", "")
        current_tool_use = kwargs.get("current_tool_use", {})

        # Reasoning delta (Task 1I): Strands invokes the callback with a
        # reasoningText kwarg when the model streams reasoningContent. Forward
        # it on the SEPARATE `reasoning` channel — never the token stream
        # (would pollute the answer). Additive; older frontends drop it.
        reasoning_text = kwargs.get("reasoningText")
        if self._emit_tokens and isinstance(reasoning_text, str) and reasoning_text:
            self._emit({"event": "reasoning", "data": {"text": reasoning_text}})

        if current_tool_use:
            tool_id = current_tool_use.get("toolUseId")
            tool_name = current_tool_use.get("name")
            if tool_id and tool_id not in self._seen_tool_ids:
                if self._active_tool_id:
                    self._emit({"event": "tool_end", "data": {"tool": self._active_tool_name}})
                self._seen_tool_ids.add(tool_id)
                self.tool_count += 1
                self._active_tool_id = tool_id
                self._active_tool_name = tool_name
                self._emit({"event": "tool_start", "data": {
                    "tool": tool_name, "tool_id": tool_id,
                    "n": self.tool_count,
                    "input": current_tool_use.get("input", {}) if self._emit_tokens else {},
                    "ts": now_ms(),  # server-truth emit timestamp (epoch ms)
                }})

        if data:
            if self._active_tool_id and not current_tool_use:
                self._emit({"event": "tool_end", "data": {"tool": self._active_tool_name}})
                self._active_tool_id = None
                self._active_tool_name = None
            if self._emit_tokens:
                self._emit({"event": "token", "data": {"text": data}})


class _ToolResultHook(HookProvider):
    #: Bound on the raw-result text scanned for rule ids / failure markers —
    #: tool results can be huge; rule ids appear inline, so a generous prefix
    #: is enough and keeps the per-call scan cheap.
    _SCAN_BOUND = 50_000

    def __init__(self, q: queue.Queue, *, integrity_mode: bool = False, prompt: str = "",
                 session_id: str = "", gate: '_LocalCancelGate | None' = None):
        self._q = q
        self.integrity_mode = integrity_mode
        self._gate = gate
        # Threaded through for the local provenance emit (plan T1.3, D3):
        # ``mint_query_ref`` derives from (session_id, tool_id).
        self.session_id = session_id
        # Per-query intelligence state, read by the stream() done branch. The
        # RAW result is only visible here — the queue events carry summaries,
        # which may drop the rule node ids.
        self.rule_ids: set[str] = set()
        self.wasted_count = 0
        # Determinism trace: call order + bounded inputs/results, fed
        # to determinism.score_tool_calls by _build_local_summary at done.
        self.tool_calls: list[dict] = []
        self.tool_inputs: dict[str, str] = {}
        self.tool_results: dict[str, str] = {}
        # Provenance (T1.3): ADDITIVE per-call ``safe_tool_result_metadata``
        # capture (result_shape/verification source for the D1 records). The
        # 2 KB ``tool_results`` strings above stay byte-identical.
        self.tool_metadata: dict[str, dict] = {}
        self.integrity_context = IntegrityContext(prompt)

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_tool_done)

    def _emit(self, item: dict) -> bool:
        context = self.integrity_context if self.integrity_mode else None
        return _put_local_event(self._q, item, context, gate=self._gate)

    def _on_tool_done(self, event: AfterToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "")
        tool_id = event.tool_use.get("toolUseId", "")
        # Accumulate intelligence-summary inputs (pure scans). Swallow-guarded:
        # a scan failure must never break the tool_result event below.
        try:
            raw = str(event.result)[:self._SCAN_BOUND]
            self.rule_ids |= extract_rule_node_ids([raw])
            if is_failed_result(raw):
                self.wasted_count += 1
            if tool_id:
                # Determinism trace — same 2KB caps as the runtime's
                # _tool_inputs/_tool_results capture.
                self.tool_calls.append({"name": tool_name, "toolUseId": tool_id})
                self.tool_inputs[tool_id] = str(event.tool_use.get("input", ""))[:2000]
                self.tool_results[tool_id] = raw[:2000]
                # Provenance (T1.3): bounded result-shape metadata per call —
                # the ONLY result surface the D1 records may read (D6).
                from .tool_result_capture import safe_tool_result_metadata
                self.tool_metadata[tool_id] = safe_tool_result_metadata(
                    event.result, tool_name=tool_name, max_chars=1024,
                )
        except Exception:  # nosec B110 - intelligence scan is best-effort
            pass
        capture_orchestrator_tool_result(
            tool_name, event.result, self.integrity_context,
        )
        if self.integrity_mode:
            from .tool_result_capture import safe_tool_result_metadata
            summary = json.dumps(safe_tool_result_metadata(
                event.result, tool_name=tool_name, max_chars=1024,
            ), separators=(",", ":"))
            public_input, sample = {}, None
        else:
            summary, sample = _with_stored_sample_line(
                self.session_id, tool_id, tool_name,
                _summarize_tool_result(tool_name, event.result), event.result)
            public_input = event.tool_use.get("input", {})
            log_tool_result_envelope(
                logging.getLogger("agentcore_client"), tool_id,
                (event.result or {}).get("status", "") if isinstance(event.result, dict) else "",
                event.result,
            )
        self._emit({"event": "tool_result", "data": {
            "tool": tool_name, "tool_id": tool_id,
            "summary": summary, "input": public_input,
            "ts": now_ms(), **({"sample_line": sample} if sample else {}),
        }})


def _dict_to_sse_event(item: dict, correlation_id: str = "") -> SSEEvent:
    event = item["event"]
    data = item["data"]
    if event == "token":
        return MessageEvent(content=data["text"])
    if event == "tool_start":
        return ToolUseEvent(
            tool_name=data["tool"], tool_use_id=data.get("tool_id", ""),
            tool_input=data.get("input"), status="started",
            ts=data.get("ts"),
        )
    if event == "tool_result":
        return ToolResultEvent(
            tool_name=data["tool"], tool_use_id=data.get("tool_id", ""),
            summary=data.get("summary", ""), tool_result=data.get("input"),
            ts=data.get("ts"), elapsed_ms=data.get("elapsed_ms"),
            sample_line=data.get("sample_line"),
        )
    if event == "tool_end":
        return ToolEndEvent(tool_name=data["tool"])
    if event == "reasoning":
        # Explicit mapping REQUIRED: the fallback below returns MessageEvent
        # (token stream) — reasoning text must never pollute the answer.
        return ReasoningEvent(text=data.get("text", ""))
    if event == "done":
        return DoneEvent(text=data.get("text", ""))
    if event == "error":
        # This is the LAST place the runtime's original error text exists: the
        # server-side frame filter replaces it with a generic "Query failed."
        # before the client, and the runtime already scrubbed its own copy. With
        # both ends opaque, a reproducible 7-of-10 failure could not be named at
        # all (2026-07-29). Log a payload-safe fingerprint — digits masked,
        # because an error can quote row values and the claim-check policy
        # covers logs too.
        _err_text = data.get("text", "")
        _category = classify_error_text(_err_text)
        logging.getLogger("agentcore_client").error(
            "RUNTIME_ERROR_FRAME category=%s head=%s",
            _category, _redact_digits(_err_text))
        # The digit-masked log line above is the ONLY place the runtime's text
        # survives; the event carries the fixed per-category public text so no
        # consumer of this client (the /api/ask filter or otherwise) can relay
        # upstream error text.
        return ErrorEvent(message=public_error_message(_category),
                          category=_category, correlation_id=correlation_id)
    if event == "metadata":
        return MetadataEvent(data=data)
    return MessageEvent(content=str(data))


# ── LocalChatClient ───────────────────────────────────────────────────────────

def _build_local_summary(tool_names: list[str], hook: _ToolResultHook) -> dict:
    """Intelligence-summary payload for the local path (Decision-2 shape via
    ``query_intelligence.build_intelligence_summary`` — the ONE builder both
    emitters use).

    Label resolution is best-effort in this process (Decision 3): pure-local
    dev has no DynamoDB, so a failed/empty lookup degrades to ``label == id``
    — never an error. NO usage bump here (Decision 4): recording stays in the
    runtime finally-block hook; the local path is display-only extraction.
    """
    rows: list[dict] = []
    if hook.rule_ids:
        try:
            from .self_learning import get_accepted_rules_by_node_ids
            rows = get_accepted_rules_by_node_ids(hook.rule_ids) or []
        except Exception:  # nosec B110 - label lookup is best-effort (Decision 3)
            rows = []
    labels_by_id: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        node_id = row.get("neptune_node_id")
        if isinstance(node_id, str) and node_id:
            labels_by_id[node_id] = format_rule_label(row)
    rules_applied = [{"id": rid, "label": labels_by_id.get(rid) or rid}
                     for rid in sorted(hook.rule_ids)]
    # Determinism: score the hook's trace mechanically when it captured
    # one (inputs/results present); omit otherwise so the payload stays
    # byte-identical to the legacy shape (pre-trace hooks, direct callers).
    determinism = None
    if hook.tool_calls and hook.tool_results:
        from .determinism import score_tool_calls
        determinism = score_tool_calls(
            hook.tool_calls, hook.tool_inputs, hook.tool_results)
    return build_intelligence_summary(
        tool_names=tool_names,
        wasted_calls=hook.wasted_count,
        rules_applied=rules_applied,
        new_proposed_mappings=0,
        determinism=determinism,
    )


def _build_local_provenance(hook: _ToolResultHook,
                            rules_applied: list[dict] | None = None,
                            verdict: str | None = None) -> dict | None:
    """Additive D1 ``provenance`` payload for the local path — the SAME
    builder the runtime emitter uses (``agents.provenance``, single source).

    Returns ``None`` (frame ABSENT, never empty) when the hook captured no
    tool calls, or when the builder raises — provenance is additive and must
    never break the stream (swallow-guarded like the runtime emit).

    ``rules_applied`` reuses the ``{id, label}`` rows the intelligence
    summary already resolved for this turn (no second label lookup —
    Decision-3 parity with the runtime's "do NOT re-query" rule); when the
    caller has none (integrity path), labels degrade honestly to ids.
    """
    if not hook.tool_calls:
        return None
    if rules_applied is None:
        rules_applied = [{"id": rid, "label": rid} for rid in sorted(hook.rule_ids)]
    try:
        from datetime import datetime, timezone
        payload = build_provenance_records(
            session_id=hook.session_id,
            tool_calls=hook.tool_calls,
            tool_inputs=hook.tool_inputs,
            tool_results_metadata=hook.tool_metadata,
            evidence_by_tool={},
            rules_applied=rules_applied,
            verdict=verdict,
            captured_at=format_captured_at(datetime.now(timezone.utc)),
            graph_query_refs=uncited_query_refs(
                session_id=hook.session_id, tool_calls=hook.tool_calls,
                tool_inputs=hook.tool_inputs, literal_eval_fallback=True),
        )
        # Assembly-time sidecar merge through the ONE merge helper, fail-soft (a
        # store failure keeps the payload as built); the reader logs capability misses.
        try:
            from . import provenance_store
            reader = provenance_store.FrameReader()
            payload = enrich_records_from_sidecar(
                payload, session_id=hook.session_id, get_entry=reader, verdict=verdict)
            reader.log_misses(hook.session_id)
        except Exception:  # nosec B110 - additive enrichment; base frame survives
            logging.getLogger("agentcore_client").debug(
                "local provenance sidecar enrichment failed — frame unenriched")
        return payload
    except Exception:  # nosec B110 - additive frame; never break the stream
        logging.getLogger("agentcore_client").warning(
            "local provenance build failed — frame omitted")
        return None


def _store_provenance_queries(session_id: str, entries: object) -> None:
    """Persist Wave-3 sidecar entries server-side (T3.3) — swallow-guarded.

    The store is fail-soft already (``provenance_store``), but a failure of
    the import or the call itself must ALSO never break the answer stream:
    Tier 3 degrades to an honest 404 at the redemption route, the turn's
    answer is untouched.
    """
    if not isinstance(entries, dict) or not entries:
        return
    try:
        from . import provenance_store
        provenance_store.put_turn_queries(session_id, entries)
    except Exception:  # nosec B110 - sidecar persistence is best-effort by contract
        logging.getLogger("agentcore_client").warning(
            "provenance query store write failed — sidecar entries dropped")


def _intercept_provenance_queries(data: dict, session_id: str) -> bool:
    """True when the frame is the Wave-3 ``provenance_queries`` sidecar.

    D6 never-broadcast: the sidecar is server-only — it is persisted here
    (where session context exists) and the caller drops the frame, so it
    NEVER becomes an SSE event. ``_parse_metadata_event`` strips the key
    too (defense in depth for parse paths without session context).
    """
    if "provenance_queries" not in data:
        return False
    _store_provenance_queries(session_id, data.get("provenance_queries"))
    return True


def _local_provenance_query_entries(hook: _ToolResultHook) -> dict:
    """Sidecar entries from the hook's captured inputs (local path, T3.3).

    A thin adapter over the ONE sidecar builder
    (``agents.provenance.build_sidecar_entries`` — mirrors the runtime
    emitters): ONLY calls whose input carries a query-bearing field (Mode-B
    SQL / explore_graph openCypher) produce an entry — Mode-A NL
    sub-questions are omitted (D6). ``literal_eval_fallback=True`` is the
    local tolerance: ``hook.tool_inputs`` retains a ``str()`` of the input
    dict (python literal, sometimes JSON), unlike the runtime's JSON capture.
    """
    from datetime import datetime, timezone
    return build_sidecar_entries(
        session_id=hook.session_id,
        tool_calls=hook.tool_calls,
        tool_inputs=hook.tool_inputs,
        rule_ids=hook.rule_ids,
        captured_at=format_captured_at(datetime.now(timezone.utc)),
        literal_eval_fallback=True,
    )


def _persist_local_provenance_queries(hook: _ToolResultHook) -> None:
    """Local done-branch store write (T3.3) — direct, no frame needed
    in-process. ONE swallow guard covers entry build AND store write (the
    cloud intercept path keeps its own guard in
    ``_store_provenance_queries``); a failure drops the sidecar entries —
    Tier 3 degrades to an honest 404 — and never touches the answer stream.
    """
    try:
        entries = _local_provenance_query_entries(hook)
        if not entries:
            return
        from . import provenance_store
        provenance_store.put_turn_queries(hook.session_id, entries)
    except Exception:  # nosec B110 - sidecar persistence is best-effort by contract
        logging.getLogger("agentcore_client").warning(
            "local provenance query persistence failed — entries dropped")


def _emit_local_integrity_result(q, result, hook, handler,
                                 gate: '_LocalCancelGate | None' = None) -> None:
    gated_text, integrity = gate_final_result(result, hook.integrity_context)
    _put_local_event(q, {"event": "token", "data": {"text": gated_text}}, gate=gate)
    from .runtime_integrity import integrity_metadata, persist_typed_proposals
    _put_local_event(q, {"event": "metadata", "data": integrity_metadata(integrity)},
                     gate=gate)
    # Provenance (T1.3): the enforce path emits the frame WITH the verdict
    # (D4) before done; the done branch skips integrity mode, so exactly one
    # frame per turn.
    provenance = _build_local_provenance(
        hook, verdict=integrity.quality_metadata().get("verdict"))
    if provenance is not None:
        _put_local_event(q, {"event": "metadata", "data": {"provenance": provenance}},
                         gate=gate)
    try:
        persist_typed_proposals(integrity, tool_calls_count=handler.tool_count)
    except Exception as exc:
        logging.getLogger("agentcore_client").warning(
            "Typed proposal persistence failed type=%s", type(exc).__name__,
        )
    _put_local_event(q, {"event": "done", "data": {"text": gated_text}},
                     gate=gate, terminal=True)


def _record_local_invocation(result, started: float) -> None:
    try:
        from .metrics_registry import get_metrics_registry, extract_strands_usage
        input_tokens, output_tokens = extract_strands_usage(result)
        get_metrics_registry().record_invocation(
            "orchestrator", input_tokens=input_tokens, output_tokens=output_tokens,
            latency_s=time.time() - started,
        )
    except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
        pass


def _drain_local_pending_charts(q: queue.Queue, agent,
                                gate: '_LocalCancelGate | None' = None) -> None:
    """Emit pending local charts unless this turn has been cancelled."""
    try:
        fresh = drain_pending_charts(agent)
        if fresh:
            _put_local_event(q, {"event": "token", "data": {
                "text": "\n\n" + "\n\n".join(fresh) + "\n\n",
            }}, gate=gate)
    except Exception:  # nosec B110 - chart injection is best-effort by contract
        logging.getLogger("agentcore_client").exception("local chart injection failed")


def _local_metric_layer_prompt(prompt, question, session_id: str):
    """ADR-0078 in LOCAL mode: the same structured metric-layer result,
    rendered by the ONE renderer into a seed-independent preamble. Computed in
    the local producer thread (never the event loop); ``question`` (the raw
    intent) stays what memory/provenance see. No match ⇒ ``prompt`` itself."""
    try:
        from .metric_turn_notice import local_turn_prompt
        return local_turn_prompt(prompt, str(question or ""), session_id)
    except Exception as exc:  # noqa: BLE001 - only an import/matcher fault lands here
        logging.getLogger("agentcore_client").warning(
            "local metric-layer notice skipped category=%s", type(exc).__name__)
        return prompt


def _run_local_agent(q, hook, prompt: str, session_id: str, user_id: str,
                     write_memory: bool, user_access_token: str | None,
                     integrity_mode: bool, gate: _LocalCancelGate,
                     question: str | None = None) -> None:
    started = time.time()
    from .identifier_provenance import begin_unanswered_record
    unanswered = begin_unanswered_record()  # before the build; logged once at turn end
    try:
        prompt = _local_metric_layer_prompt(
            prompt, prompt if question is None else question, session_id)
        handler = _SSECallbackHandler(
            q, emit_tokens=not integrity_mode, gate=gate,
            integrity_context=hook.integrity_context if integrity_mode else None,
        )
        orchestrator = build_orchestrator(
            session_id, user_id, write_memory, user_access_token=user_access_token,
        )
        gate.bind_agent(orchestrator)
        orchestrator.callback_handler = handler
        orchestrator.hooks.add_hook(hook)
        if gate.cancelled:
            return
        kwargs = {"structured_output_model": FinalAnswerDraft} if integrity_mode else {}
        result = orchestrator(prompt, **kwargs)
        if gate.cancelled:
            return
        if integrity_mode:
            _emit_local_integrity_result(q, result, hook, handler, gate)
        else:
            _drain_local_pending_charts(q, orchestrator, gate)
            _put_local_event(q, {"event": "done", "data": {"text": str(result)}},
                             gate=gate, terminal=True)
        _record_local_invocation(result, started)
    except Exception:
        _put_local_event(q, {"event": "error", "data": {
            "text": "Query failed unexpectedly. Please try again.",
        }}, gate=gate, terminal=True)
    finally:
        unanswered.log()


def _record_local_tool_event(item: dict, starts: dict[str, int], names: list[str]) -> None:
    data = item.get("data", {}) if isinstance(item.get("data"), dict) else {}
    if item.get("event") == "tool_start":
        tool_id = data.get("tool_id", "")
        if tool_id and isinstance(data.get("ts"), int):
            starts[tool_id] = data["ts"]
        return
    if item.get("event") != "tool_result":
        return
    started = starts.pop(data.get("tool_id", ""), None)
    if started is not None and isinstance(data.get("ts"), int):
        data["elapsed_ms"] = data["ts"] - started
    name = data.get("tool", "")
    names.append(name)
    try:
        from .metrics_registry import get_metrics_registry, tool_to_agent_key
        registry = get_metrics_registry()
        registry.record_tool_call("orchestrator")
        key = tool_to_agent_key(name)
        if key != "orchestrator":
            registry.record_tool_call(key)
            registry.record_invocation(key)
    except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
        pass


_LOCAL_CANCELLED = object()


def _next_local_event(q: queue.Queue, gate: _LocalCancelGate,
                      timeout: float = 300.0):
    """Return the next item, or a cancellation sentinel, preserving timeout."""
    deadline = time.monotonic() + timeout
    while not gate.cancelled:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            return q.get(timeout=min(_LOCAL_STOP_TICK, remaining))
        except queue.Empty:
            continue
    return _LOCAL_CANCELLED


def _consume_local_events(q, hook, gate: _LocalCancelGate | None = None
                          ) -> Generator[SSEEvent, None, None]:
    gate = gate or _LocalCancelGate()
    tool_names: list[str] = []
    starts: dict[str, int] = {}
    while True:
        try:
            item = _next_local_event(q, gate)
        except queue.Empty:
            yield ErrorEvent(message="Timeout", category="timeout",
                             correlation_id=hook.session_id)
            return
        if item is _LOCAL_CANCELLED:
            return
        _record_local_tool_event(item, starts, tool_names)
        event_name = item.get("event")
        if event_name == "done":
            _persist_local_provenance_queries(hook)
            summary = _build_local_summary(tool_names, hook)
            yield MetadataEvent(data={"intelligence_summary": summary})
            if not hook.integrity_mode:
                provenance = _build_local_provenance(
                    hook, rules_applied=summary.get("rules_applied") or [])
                if provenance is not None:
                    yield MetadataEvent(data={"provenance": provenance})
        yield _dict_to_sse_event(item, correlation_id=hook.session_id)
        if event_name in ("done", "error"):
            return


def _local_integrity_mode() -> bool:
    """Return the shared local query-mode decision for stream and sync paths.

    True only when Mode A is active (``SYSTEM_QUERY_MODE`` != ``orchestrator``)
    AND the ADR-0046 gate is opted in via ``RESPONSE_INTEGRITY_MODE=enforce``
    (ADR-0047; the default ``report-only`` keeps the legacy answer flow).
    """
    return mode_a_integrity_enforced()


class LocalChatClient:
    def stream(self, prompt: str, session_id: str, user_id: str, plant: str = "",
               write_memory: bool = False, user_access_token: str | None = None,
               raw_question: str | None = None,
               cancel: 'StreamCancellation | None' = None,
               extra_payload: dict | None = None,
               ) -> Generator[SSEEvent, None, None]:
        """Stream a local turn with bounded, cancellation-aware production.

        ``extra_payload`` is accepted for signature parity with
        :class:`AgentCoreChatClient` and ignored (no runtime consumes reserved
        keys here); the ADR-0078 metric-layer notice is instead computed and
        rendered in the local producer thread (``_local_metric_layer_prompt``)."""
        yield from self._stream_impl(
            prompt, session_id, user_id, plant, write_memory, user_access_token,
            raw_question, cancel,
        )

    def _stream_impl(self, prompt: str, session_id: str, user_id: str,
                     plant: str = "", write_memory: bool = False,
                     user_access_token: str | None = None,
                     raw_question: str | None = None,
                     cancel: 'StreamCancellation | None' = None
                     ) -> Generator[SSEEvent, None, None]:
        integrity_mode = _local_integrity_mode()
        size = MAX_PROGRESS_EVENTS + 16 if integrity_mode else _local_queue_maxsize()
        target: queue.Queue = queue.Queue(maxsize=size)
        gate = _LocalCancelGate()
        if cancel is not None:
            cancel.arm(gate)
        intent = raw_question if raw_question is not None else getattr(
            prompt, "raw_question", prompt)
        hook = _ToolResultHook(
            target, integrity_mode=integrity_mode, prompt=intent,
            session_id=session_id, gate=gate,
        )
        args = (target, hook, prompt, session_id, user_id, write_memory,
                user_access_token, integrity_mode, gate, intent)
        try:
            if gate.cancelled:
                return
            threading.Thread(target=_run_local_agent, args=args, daemon=True).start()
            yield from _consume_local_events(target, hook, gate)
        finally:
            if cancel is not None:
                cancel.release()
            else:
                gate.close()


# ── ThinkingFilter ────────────────────────────────────────────────────────────


def _tool_result_from_payload(payload: dict) -> ToolResultEvent:
    content = payload.get("content", [])
    raw_text = ""
    if isinstance(content, list) and content and isinstance(content[0], dict):
        raw_text = str(content[0].get("text", ""))
    summary = _summarize_tool_result(
        "", {"content": [{"text": raw_text}]},
    ) if raw_text else ""
    # The runtime forwards the message the ChartRedactionHook already
    # redacted: the result preview drops the hook's model-only chart note
    # the same way the summary does, and the orchestrator-only engine lines.
    from .chart_note import strip_chart_notes
    from .internal_lines import public_text
    preview, rendered, withheld = strip_chart_notes(public_text(raw_text))
    if rendered or withheld:
        preview = preview.strip()
    return ToolResultEvent(
        tool_name="", tool_use_id=payload.get("toolUseId", ""), summary=summary,
        tool_result=preview[:500] if preview else None,
    )


#: Display bound on a forwarded route context (the trace caps its own copy too).
_ROUTE_CONTEXT_MAX_CHARS = 4000


def _received_route_context(payload: dict) -> "str | None":
    """The route context the runtime forwarded with a tool result
    (``route_facts.RECEIVED_KEY``): what the gateway sent the system, for the
    trace's Routing context; ``None`` when there is none."""
    from .route_facts import RECEIVED_KEY
    value = payload.get(RECEIVED_KEY) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:_ROUTE_CONTEXT_MAX_CHARS]


def _keep_route_context(owner_id: str, session_id: str, tool_id: str,
                        route_context: "str | None") -> None:
    """Keep a call's forwarded route context for the Recent sessions reload
    (``route_context_store``). Never raises."""
    if not route_context:
        return
    try:
        from . import route_context_store
        route_context_store.put(owner_id, session_id, tool_id, route_context)
    except Exception:  # noqa: BLE001 - display metadata never breaks the stream
        logging.getLogger("agentcore_client").debug("route context not kept", exc_info=True)


def _is_system_query_tool(tool_name: str) -> bool:
    """A Mode A ``query_<id>`` tool (gateway ``<target>___`` prefix or bare):
    the one read hint the reload uses too (``telemetry_coverage.
    may_hold_trace_telemetry``)."""
    from .telemetry_coverage import may_hold_trace_telemetry
    return may_hold_trace_telemetry(tool_name)


def _trace_telemetry(session_id: str, tool_id: str, tool_name: str,
                     summary: str) -> "tuple[str, list[str] | None, str | None]":
    """``(summary, coverage lines, sample line)`` of a ``query_<id>`` trace
    step: the telemetry-coverage lines and the telemetry sample line the
    system runtime stored for the call, each as the step's own field
    (``ToolResultEvent.telemetry_coverage`` / ``sample_line``; ``None`` when
    none), shown above and below the summary.

    OUT-OF-BAND read: the system runtime stored both in the W6-F2
    investigation-trace entry under the D3 ``(session_id, query_ref)`` join
    (``query_ref`` minted from THIS call's ``toolUseId``, the same pure
    derivation the orchestrator forwarded), written under the call's
    capability. Trace text only: both ride the ``tool_result`` SSE to the
    Reasoning pill and never reach the orchestrator model, whose copy of the
    tool result is untouched upstream. Any other tool performs ZERO store
    reads; the name only skips the read, and a call the runtime wrote no
    entry for gets nothing, whatever its name. Lines carry names/counts/
    timestamps only; graph-known ids are named through the SAME fail-soft
    resolver as the summary. Neither enters the summary, which the outcome
    classifiers read from its head: a system query call's summary is the
    structural one less any copy of the sample line, on a hit, a miss or a
    failed read alike (``telemetry_coverage.system_call_summary``), and the
    sample line comes only from the entry, never from the result's text.
    """
    if not _is_system_query_tool(tool_name):
        return summary, None, None
    from .telemetry_coverage import system_call_summary
    summary = system_call_summary(tool_name, summary)
    if not (session_id and tool_id):
        return summary, None, None
    try:
        from . import investigation_trace_store
        from .provenance import mint_query_ref
        lines, sample = investigation_trace_store.get_trace_telemetry(
            session_id, mint_query_ref(session_id, tool_id))
        from .id_name_resolver import annotate_ids_in_lines
        named = annotate_ids_in_lines([*lines, *([sample] if sample else [])])
        coverage = named[:len(lines)] or None
        return summary, coverage, named[len(lines)] if sample else None
    except Exception as exc:  # noqa: BLE001 - trace enrichment never breaks the stream
        logging.getLogger("agentcore_client").debug(
            "telemetry coverage read skipped (%s)", type(exc).__name__)
        return summary, None, None


def _with_stored_sample_line(session_id: str, tool_id: str, tool_name: str,
                             summary: str, result) -> "tuple[str, str | None]":
    """``(summary, sample line)`` of a local chat trace step: the telemetry
    sample line the system runtime stored for this call as its own field,
    and the summary without any copy of it, whether or not the line is found
    (``telemetry_coverage.system_call_summary``); ``(summary, None)`` when
    there is none. The cloud stream reads the line with the coverage lines
    (:func:`_trace_telemetry`). Read only for a system query tool whose
    result holds the line's opening words, which only decides whether to look
    (``telemetry_coverage.names_sample_line``); the line shown is the stored
    one. Never raises. The local path carries no coverage lines."""
    from .telemetry_coverage import names_sample_line, system_call_summary
    if not _is_system_query_tool(tool_name):
        return summary, None
    summary = system_call_summary(tool_name, summary)
    if not (session_id and tool_id and names_sample_line(str(result))):
        return summary, None
    try:
        from . import investigation_trace_store
        from .id_name_resolver import annotate_ids_in_text
        line = investigation_trace_store.sample_lines_for(session_id, [tool_id]).get(tool_id)
        return summary, annotate_ids_in_text(line) if line else None
    except Exception as exc:  # noqa: BLE001 - trace enrichment never breaks the stream
        logging.getLogger("agentcore_client").debug(
            "sample line read skipped (%s)", type(exc).__name__)
        return summary, None


def _parse_metadata_event(data: dict) -> SSEEvent | None:
    # D6 never-broadcast (T3.3): the Wave-3 ``provenance_queries`` sidecar is
    # server-only and must NEVER become an SSE frame. The store write happens
    # upstream where session context exists (_CloudStreamState.events_for);
    # this strip is defense in depth for parse paths without it.
    if "provenance_queries" in data:
        return None
    # Allowlist posture: only these keys pass through as metadata frames —
    # "provenance" (T1.3) is the additive per-claim records key (D1); an
    # unknown sibling key is still silently dropped.
    for key in ("response_integrity", "intelligence_summary", "agent_analysis",
                "provenance"):
        if key in data:
            return MetadataEvent(data={key: data[key]})
    result = data.get("tool_result_metadata")
    if not isinstance(result, dict):
        return None
    metadata = result.get("metadata")
    return ToolResultEvent(
        tool_name=str(result.get("name") or ""),
        tool_use_id=str(result.get("toolUseId") or ""),
        summary=json.dumps(metadata if isinstance(metadata, dict) else {}, separators=(",", ":")),
        tool_result=None,
    )


def _parse_direct_stream_event(data: dict, text_filter: ThinkingFilter) -> SSEEvent | None:
    reasoning = data.get("reasoningText")
    if isinstance(reasoning, str) and reasoning:
        return ReasoningEvent(text=reasoning)
    text = data.get("data")
    if isinstance(text, str) and text:
        filtered = text_filter.filter(text)
        return MessageEvent(content=filtered) if filtered else None
    current = data.get("current_tool_use")
    if isinstance(current, dict) and current.get("name"):
        return ToolUseEvent(
            tool_name=current.get("name", ""), tool_use_id=current.get("toolUseId", ""),
            tool_input=current.get("input"),
        )
    return None


def _parse_callback_event(data: dict, text_filter: ThinkingFilter) -> SSEEvent | None:
    callback = data.get("callback")
    if not isinstance(callback, dict):
        return None
    text = callback.get("data", "")
    if isinstance(text, str) and text:
        filtered = text_filter.filter(text)
        return MessageEvent(content=filtered) if filtered else None
    current = callback.get("current_tool_use", {})
    if isinstance(current, dict) and current.get("name"):
        return ToolUseEvent(
            tool_name=current.get("name", ""), tool_use_id=current.get("toolUseId", ""),
            tool_input=current.get("input"),
        )
    return None


def _parse_nested_event(data: dict, text_filter: ThinkingFilter) -> SSEEvent | None:
    nested = data.get("event", {})
    delta = nested.get("contentBlockDelta", {}).get("delta", {})
    if delta.get("text"):
        filtered = text_filter.filter(delta["text"])
        return MessageEvent(content=filtered) if filtered else None
    start = nested.get("contentBlockStart", {}).get("start", {})
    if isinstance(start.get("toolUse"), dict):
        tool_use = start["toolUse"]
        return ToolUseEvent(
            tool_name=tool_use.get("name", ""), tool_use_id=tool_use.get("toolUseId", ""),
            tool_input=tool_use.get("input"),
        )
    result = nested.get("toolResult")
    return _tool_result_from_payload(result) if isinstance(result, dict) and result else None


def _parse_message_event(data: dict) -> SSEEvent | None:
    message = data.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("toolUse"), dict):
            tool_use = block["toolUse"]
            return ToolUseEvent(
                tool_name=tool_use.get("name", ""), tool_use_id=tool_use.get("toolUseId", ""),
                tool_input=tool_use.get("input"),
            )
        if isinstance(block.get("toolResult"), dict):
            return _tool_result_from_payload(block["toolResult"])
    return None


def _parse_legacy_event(data: dict) -> SSEEvent | None:
    if data.get("type") == "tool_use":
        return ToolUseEvent(
            tool_name=data.get("tool_name") or data.get("name", ""),
            tool_use_id=data.get("tool_use_id") or data.get("id", ""),
            tool_input=data.get("tool_input") or data.get("input"),
        )
    if data.get("type") == "tool_result":
        return ToolResultEvent(
            tool_name=data.get("tool_name") or data.get("name", ""),
            tool_use_id=data.get("tool_use_id") or data.get("id", ""),
            summary=str(data.get("tool_result") or data.get("result", ""))[:200],
        )
    usage = data.get("usage") or data.get("event", {}).get("metadata", {}).get("usage")
    if usage:
        return MetadataEvent(data={
            "inputTokens": usage.get("inputTokens", 0),
            "outputTokens": usage.get("outputTokens", 0),
        })
    return None

class ThinkingFilter:
    """Strips <thinking>...</thinking> and Nova XML tool tags from streamed text."""

    _THINKING_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)
    _FUNCTION_RE = re.compile(r"<__function=[^>]*>.*?</__function[^>]*>", re.DOTALL)

    def filter(self, text: str) -> str:
        text = self._THINKING_RE.sub("", text)
        text = self._FUNCTION_RE.sub("", text)
        return text


# ── AgentCoreChatClient ───────────────────────────────────────────────────────



def _cloud_mode_a() -> bool:
    """Return the cloud-path integrity-stream decision.

    True only when Mode A is active (``SYSTEM_QUERY_MODE`` != ``orchestrator``)
    AND the ADR-0046 gate is opted in via ``RESPONSE_INTEGRITY_MODE=enforce``
    (ADR-0047; the default ``report-only`` keeps the legacy answer flow).
    """
    return mode_a_integrity_enforced()


#: The runtime session id this client mints: "session-" + 28 hex (36 chars,
#: inside AgentCore's 33-256 bound). Only ids of this exact shape are invoked
#: with or stopped, so a corrupt record can never name another session shape.
_RUNTIME_SESSION_ID_RE = re.compile(r"session-[0-9a-f]{28}")


def new_runtime_session_id() -> str:
    """A fresh AgentCore runtime session id in the shape this client uses."""
    import uuid
    return "session-" + uuid.uuid4().hex[:28]


def is_runtime_session_id(value: object) -> bool:
    """True when ``value`` has the shape :func:`new_runtime_session_id` mints."""
    return isinstance(value, str) and bool(_RUNTIME_SESSION_ID_RE.fullmatch(value))


def stop_runtime_session(runtime_session_id: object, *, runtime_arn: "str | None" = None,
                         region: "str | None" = None) -> str:
    """Stop one AgentCore runtime session; a closed outcome token, never raised.

    ``"stopped"``, ``"not_found"`` (it already ended), ``"failed:<code>"``,
    ``"invalid"`` (a malformed id, nothing sent) or ``"no_runtime"`` (no
    runtime ARN to stop it on). Defaults to the app's configured orchestrator
    runtime and region. Used to end an async ask the app has given up on."""
    if not is_runtime_session_id(runtime_session_id):
        return "invalid"
    if runtime_arn is None or region is None:
        cfg = get_config()
        runtime_arn = runtime_arn if runtime_arn is not None else getattr(
            cfg, "agentcore_runtime_arn", None)
        region = region if region is not None else getattr(cfg, "aws_region", None)
    if not runtime_arn or runtime_arn == "placeholder":
        return "no_runtime"
    from botocore.config import Config
    from botocore.exceptions import ClientError
    try:
        client = boto3.client("bedrock-agentcore", region_name=region,
                              config=Config(connect_timeout=5, read_timeout=15,
                                            retries={"max_attempts": 3, "mode": "standard"}))
        client.stop_runtime_session(agentRuntimeArn=runtime_arn,
                                    runtimeSessionId=runtime_session_id)
        return "stopped"
    except ClientError as exc:
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = _token_or_dash(error.get("Code"))
        return "not_found" if code == "ResourceNotFoundException" else f"failed:{code}"
    except Exception as exc:  # noqa: BLE001 - reported as a closed token, the caller logs it
        return f"failed:{_token_or_dash(type(exc).__name__)}"


def _invoke_cloud_runtime(client, runtime_arn: str, session_id: str, payload: bytes, log):
    from botocore.exceptions import ClientError
    retryable = {"ServiceUnavailableException", "ThrottlingException", "ModelTimeoutException"}
    for attempt in range(4):
        try:
            return client.invoke_agent_runtime(
                runtimeSessionId=session_id, agentRuntimeArn=runtime_arn,
                payload=BytesIO(payload),
            )
        except ClientError as exc:
            error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
            code = error.get("Code", "RUNTIME_UNAVAILABLE")
            if code not in retryable or attempt >= 3:
                raise
            wait = 2 ** attempt
            log.warning("AgentCore category=%s attempt=%d retry_seconds=%d", code, attempt + 1, wait)
            time.sleep(wait)
    raise RuntimeError("runtime invocation retry exhausted")


class _CloudStreamState:
    # Class-level default so test doubles that bypass __init__ still mint a
    # payload-safe ErrorEvent (an empty correlation id is omitted from the
    # SSE payload).
    session_id: str = ""
    # Failure-fingerprint defaults (same reason): where the read was, what the
    # last parsed frame looked like, and when the last line arrived. ``phase``
    # starts at "invoke": until iter_stream runs, a failure is about what the
    # invocation returned (e.g. a response with no stream), not the read.
    runtime_session_id: str = ""
    phase: str = "invoke"
    last_frame: str = "none"
    skipped_lines: int = 0
    started: float = 0.0
    last_line_at: float = 0.0

    def __init__(self, text_filter, *, integrity_mode: bool, log, owner_id: str,
                 session_id: str, runtime_session_id: str):
        self.text_filter = text_filter
        self.integrity_mode = integrity_mode
        self.log = log
        self.owner_id = owner_id
        self.runtime_session_id = runtime_session_id
        # App session id (NOT the runtime session id): the Wave-3 sidecar
        # store keys by the SAME session_id the D3 query_ref derivation and
        # the redemption route use (T3.3).
        self.session_id = session_id
        self.started = time.monotonic()
        self.last_line_at = self.started
        self.line_count = 0
        self.total_bytes = 0
        self.progress_events = 0
        self.progress_exceeded = False
        self.runtime_error_seen = False
        self.pending: dict[str, str] = {}
        self.seen: set[str] = set()
        self.deferred: dict[str, str] = {}
        self.starts: dict[str, int] = {}
        from .stream_capture import open_stream_capture
        self.capture = open_stream_capture(owner_id=owner_id, session_id=session_id)

    def _line_data(self, raw_line) -> dict | None:
        self.last_line_at = time.monotonic()
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        # Keepalive heartbeat: dropped BEFORE any byte/line/progress accounting
        # or capture write, so a long idle turn's heartbeats never trip the
        # integrity byte cap, the progress limit, or pollute the capture log.
        # It still stamps last_line_at above, so a failure fingerprint's idle_s
        # measures how long the SOCKET was silent, heartbeats included.
        if _is_heartbeat_line(line):
            return None
        self.line_count += 1
        self.total_bytes += len(raw_line) if isinstance(raw_line, bytes) else len(line.encode())
        from .runtime_integrity import MAX_PUBLIC_STREAM_BYTES
        if self.integrity_mode and self.total_bytes > MAX_PUBLIC_STREAM_BYTES:
            raise RuntimeError("public response stream exceeded bounded size")
        if self.capture is not None:
            self.capture.write(line)
        if self.line_count % 1000 == 0:
            self.log.info("QUERY_STREAM_PROGRESS lines=%d bytes=%d", self.line_count, self.total_bytes)
        if not line.startswith("data: "):
            self.skipped_lines += 1 if line else 0
            return None
        try:
            data = json.loads(line[6:])
        except (json.JSONDecodeError, ValueError):
            self.skipped_lines += 1
            return None
        self.last_frame = frame_label(data)
        return data if isinstance(data, dict) else None

    def _start_event(self, tool_use: dict) -> list[SSEEvent]:
        tool_id = str(tool_use.get("toolUseId") or "")
        name = str(tool_use.get("name") or "")
        if not tool_id or tool_id in self.seen:
            return []
        self.seen.add(tool_id)
        self.deferred.pop(tool_id, None)
        self.pending[tool_id] = name
        timestamp = now_ms()
        self.starts[tool_id] = timestamp
        tool_input = None if self.integrity_mode else tool_use.get("input")
        return [ToolUseEvent(
            tool_name=name, tool_use_id=tool_id, tool_input=tool_input, ts=timestamp,
        )]

    def _result_events(self, payload: dict) -> list[SSEEvent]:
        tool_id = str(payload.get("toolUseId") or "")
        name = self.pending.get(tool_id) or self.deferred.get(tool_id, "")
        events: list[SSEEvent] = []
        if name and tool_id not in self.pending:
            events.extend(self._start_event({"toolUseId": tool_id, "name": name}))
        if self.integrity_mode:
            from .tool_result_capture import safe_tool_result_metadata
            metadata = safe_tool_result_metadata(payload, tool_name=name, max_chars=1024)
            result = ToolResultEvent(
                tool_name=name, tool_use_id=tool_id,
                summary=json.dumps(metadata, separators=(",", ":")), tool_result=None,
            )
        else:
            result = _tool_result_from_payload(payload)
            result.tool_name = name
            result.summary, result.telemetry_coverage, result.sample_line = _trace_telemetry(
                self.session_id, tool_id, name, result.summary)
            result.route_context = _received_route_context(payload)
            _keep_route_context(self.owner_id, self.session_id, tool_id, result.route_context)
            log_tool_result_envelope(self.log, tool_id, payload.get("status", ""), payload.get("content"))
        result.ts = now_ms()
        started = self.starts.pop(tool_id, None)
        result.elapsed_ms = result.ts - started if started is not None else None
        events.extend((result, ToolEndEvent(tool_name=name)))
        self.pending.pop(tool_id, None)
        return events

    def _message_events(self, data: dict) -> tuple[list[SSEEvent], bool]:
        message = data.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return [], False
        uses = [item["toolUse"] for item in content
                if isinstance(item, dict) and isinstance(item.get("toolUse"), dict)]
        results = [item["toolResult"] for item in content
                   if isinstance(item, dict) and isinstance(item.get("toolResult"), dict)]
        if uses and not results:
            return [event for item in uses for event in self._start_event(item)], True
        if len(results) == 1:
            return self._result_events(results[0]), True
        if not results:
            return [], False
        events: list[SSEEvent] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("toolResult"), dict):
                events.extend(self._result_events(item["toolResult"]))
            elif isinstance(item, dict) and isinstance(item.get("toolUse"), dict):
                events.extend(self._start_event(item["toolUse"]))
        return events, True

    def _runtime_error_events(self, data: dict) -> list[SSEEvent]:
        """One public ErrorEvent per stream for the runtime's raw error dict.

        Fixed per-category public text — the raw error text NEVER rides in
        the event (claim-check policy: it can quote row values).
        """
        if self.runtime_error_seen:
            return []
        self.runtime_error_seen = True
        category = classify_error_text(data.get("error"))
        _log_generic_error_dict(data, category)
        return [ErrorEvent(
            message=public_error_message(category), details="",
            category=category, correlation_id=self.session_id,
        )]

    def _generic_events(self, data: dict) -> list[SSEEvent]:
        if data.get("error") not in (None, "", False, [], {}):
            return self._runtime_error_events(data)
        metadata_result = _parse_metadata_event(data)
        if "tool_result_metadata" in data and isinstance(metadata_result, ToolResultEvent):
            metadata_result.ts = now_ms()
            name = metadata_result.tool_name or self.pending.get(metadata_result.tool_use_id, "")
            metadata_result.tool_name = name
            started = self.starts.pop(metadata_result.tool_use_id, None)
            metadata_result.elapsed_ms = metadata_result.ts - started if started is not None else None
            self.pending.pop(metadata_result.tool_use_id, None)
            return [metadata_result, ToolEndEvent(tool_name=name)]
        event = _parse_direct_stream_event(data, self.text_filter)
        event = event or _parse_callback_event(data, self.text_filter)
        event = event or _parse_nested_event(data, self.text_filter)
        event = event or _parse_message_event(data)
        event = event or _parse_metadata_event(data) or _parse_legacy_event(data)
        if self.integrity_mode and isinstance(event, ReasoningEvent):
            return []
        if isinstance(event, ToolUseEvent):
            if event.tool_use_id in self.seen:
                return []
            if isinstance(event.tool_input, str):
                first = event.tool_use_id not in self.deferred
                self.deferred[event.tool_use_id] = event.tool_name
                return [ReasoningEvent(text=f"preparing {event.tool_name} query…")] if first and event.tool_name else []
            return self._start_event({
                "toolUseId": event.tool_use_id, "name": event.tool_name,
                "input": event.tool_input,
            })
        if isinstance(event, ToolResultEvent):
            # Prefer the FULL nested payload when present: the reconstructed
            # fallback carries only the event's 500-char preview, which would
            # truncate long tool results before summarization. (Chart tags no
            # longer ride in tool results on the client side — the runtime
            # redacts and injects them as data tokens; see ChartRedactionHook.)
            nested = data.get("event") if isinstance(data.get("event"), dict) else {}
            full = nested.get("toolResult")
            payload = full if isinstance(full, dict) and full.get("toolUseId") else {
                "toolUseId": event.tool_use_id, "content": [
                    {"text": event.tool_result or ""},
                ],
            }
            return self._result_events(payload)
        return [event] if event is not None else []

    def _bounded(self, events: list[SSEEvent]) -> list[SSEEvent]:
        output: list[SSEEvent] = []
        progress_types = (ToolUseEvent, ToolResultEvent, ToolEndEvent, ReasoningEvent)
        for event in events:
            if self.integrity_mode and isinstance(event, progress_types):
                self.progress_events += 1
                if self.progress_events > MAX_PROGRESS_EVENTS:
                    self.progress_exceeded = True
                    continue
            output.append(event)
        return output

    def events_for(self, data: dict) -> list[SSEEvent]:
        # Keepalive heartbeat → no SSE event (defense in depth; _line_data
        # already drops it before this is reached on the wire path).
        if _is_heartbeat(data):
            return []
        # Wave-3 (T3.3): the sidecar frame is persisted and DROPPED here —
        # it never reaches a parser, so it can never become an SSE event.
        if _intercept_provenance_queries(data, self.session_id):
            return []
        message_events, handled = self._message_events(data)
        return self._bounded(message_events if handled else self._generic_events(data))

    def iter_stream(self, stream) -> Generator[SSEEvent, None, None]:
        # ``phase`` tells a failure apart: "read" = the body's line iterator
        # raised (network/service side), "parse" = OUR handling of a line did.
        self.phase = "read"
        try:
            lines = stream.iter_lines(chunk_size=1)
        except TypeError:
            lines = stream.iter_lines()
        for raw_line in lines:
            self.phase = "parse"
            data = self._line_data(raw_line)
            if data is not None:
                yield from self.events_for(data)
            self.phase = "read"

    def completion_events(self) -> list[SSEEvent]:
        if self.runtime_error_seen:
            return [DoneEvent()]
        elapsed = time.monotonic() - self.started
        events: list[SSEEvent] = []
        if self.progress_exceeded:
            events.append(MetadataEvent(data={"response_integrity": {
                "completeness": "partial", "learning_allowed": False,
                "reason_codes": ["TRAVERSAL_BUDGET_EXCEEDED"],
            }}))
        metadata = {
            "streamBytes": self.total_bytes, "streamMB": round(self.total_bytes / (1024 * 1024), 2),
            "streamLines": self.line_count, "elapsedSeconds": round(elapsed, 1),
        }
        if self.capture is not None:
            metadata["captureId"] = self.capture.capture_id
        events.extend((MetadataEvent(data=metadata), DoneEvent()))
        return events

    def close(self) -> None:
        if self.capture is not None:
            self.capture.close()


def _close_quietly(closeable) -> bool:
    """Close ``closeable`` if it can be closed; a failure is never fatal.
    ``True`` when it is closed (or there was nothing to close), ``False``
    when its ``close()`` raised, so a caller can try again.

    Cancellation runs on the CONSUMER's thread while the producer may be parked
    inside the body's read — a raise here would surface as a disconnect handler
    blowing up, which tells nobody anything useful.

    A socket body is shut for reading first (:func:`_shutdown_reads`): its
    ``close()`` takes the lock the parked read holds, so without the shutdown
    it waits for the runtime's next bytes, and on the disconnect path that wait
    is on the event loop (a rehearsal account, 2026-10-17: every request stalled 15.5 s)."""
    close = getattr(closeable, "close", None)
    if close is None:
        return True
    _shutdown_reads(closeable)
    try:
        close()
        return True
    except Exception:  # nosec B110 - best-effort teardown; failure is non-fatal
        logging.getLogger("agentcore_client").debug(
            "stream body close failed", exc_info=True)
        return False


def _shutdown_reads(body) -> None:
    """Wake a read parked in ``body``'s socket so a close does not wait for it.

    ``body`` is botocore's ``StreamingBody`` (its urllib3 response is
    ``_raw_stream``) or a urllib3 response itself; anything else (the local
    cancel gate, test doubles) is left alone. urllib3's ``shutdown()`` shuts
    the socket's read side: the parked read returns at once and the close that
    follows does not block. It raises when the connection was already returned
    to the pool (a finished body), which is fine: nothing is parked then."""
    from urllib3.response import BaseHTTPResponse
    raw = body if isinstance(body, BaseHTTPResponse) else getattr(body, "_raw_stream", None)
    if not isinstance(raw, BaseHTTPResponse):
        return
    try:
        raw.shutdown()
    except Exception:  # nosec B110 - nothing parked to wake; the close still runs
        logging.getLogger("agentcore_client").debug(
            "stream body read shutdown skipped", exc_info=True)


class StreamCancellation:
    """Idempotent cancel handle for ONE invocation's response body.

    A stop FLAG cannot cancel a producer that is already blocked inside
    ``response["response"].iter_lines()``: the check between events is never
    reached and the iterator's ``finally`` cannot run, so the abandoned
    AgentCore turn stays alive until the 300 s read timeout (2026-08-24: one
    task at cpu=512 pinned ~99.9-100% CPU for ~45 min). Closing the
    PER-INVOCATION body from outside that read is what unblocks it — no chat
    client object is closed.

    :meth:`arm` publishes the body once the invocation returns; :meth:`cancel`
    (consumer disconnect) and :meth:`release` (normal/error teardown) both close
    it, in any order, from any thread, and a body that closed is never closed
    again. A close that raised keeps the body, so the next :meth:`cancel` or
    :meth:`release` tries again (an abandoned async ask's worker is otherwise
    left blocked on a body nobody can reach).
    """

    __slots__ = ("_lock", "_body", "_cancelled", "_invoking", "_armed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._body = None
        self._cancelled = False
        self._invoking = False
        self._armed = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def armed(self) -> bool:
        """True once a response body was published (the runtime answered)."""
        return self._armed

    def begin_invoke(self) -> bool:
        """The invoke is about to be sent: ``False`` (send nothing) when the
        handle was already cancelled. Atomic with :meth:`prevent_invoke`, so
        a run is either refused before it starts or known to have started."""
        with self._lock:
            if self._cancelled:
                return False
            self._invoking = True
            return True

    def prevent_invoke(self) -> bool:
        """Cancel ONLY if no invoke has begun; ``True`` when it was prevented
        (a later :meth:`begin_invoke` refuses). ``False`` once the invoke was
        sent: the runtime session may then exist, so a caller that wanted no
        run at all must stop it instead."""
        with self._lock:
            if self._invoking:
                return False
            self._cancelled = True
            return True

    def arm(self, body) -> None:
        """Publish this invocation's response body — closing it NOW if the
        disconnect already landed (the invoke/first-read race)."""
        if body is None:
            return
        with self._lock:
            self._armed = True
            if not self._cancelled:
                self._body = body
                return
        self._close_or_keep(body)

    def cancel(self) -> None:
        """Consumer is gone: mark cancelled and close the body (idempotent)."""
        with self._lock:
            self._cancelled = True
            body, self._body = self._body, None
        self._close_or_keep(body)

    def release(self) -> None:
        """Normal/error teardown: close the body WITHOUT claiming cancellation."""
        with self._lock:
            body, self._body = self._body, None
        self._close_or_keep(body)

    def _close_or_keep(self, body) -> None:
        """Close ``body``; if its close raised, keep it for the next attempt."""
        if body is None or _close_quietly(body):
            return
        with self._lock:
            if self._body is None:
                self._body = body


def _log_stream_failure(state: _CloudStreamState, exc: Exception, public_category: str,
                        cancel: 'StreamCancellation | None') -> None:
    """One payload-safe QUERY_FAILURE line that NAMES the failure.

    The line used to carry only the internal catch-all category, so a read
    timeout, a connection the service closed and a bug in our own frame parser
    all looked the same. It now adds where the stream was (``phase``), the
    exception's qualified type, cause chain and innermost frames, the AWS
    code/HTTP status/request id when present, how long the stream ran and how
    long since its last line, the allowlisted shape of the last parsed frame,
    whether the consumer had already cancelled, and the runtime session id (the
    runtime's own request log carries the same id). The message head is
    digit-masked; no exc_info (it would re-render the unmasked message).
    """
    now = time.monotonic()
    try:
        started = getattr(state, "started", 0.0) or now
        last_line = getattr(state, "last_line_at", 0.0) or started
        fields = {
            "phase": getattr(state, "phase", "invoke"),
            "cancelled": "yes" if cancel is not None and cancel.cancelled else "no",
            "elapsed_s": f"{now - started:.1f}", "idle_s": f"{now - last_line:.1f}",
            "skipped": getattr(state, "skipped_lines", 0),
            "last_frame": getattr(state, "last_frame", "none"),
            "runtime_session": _token_or_dash(getattr(state, "runtime_session_id", "")),
        }
    except Exception:  # noqa: BLE001 - the fingerprint never masks the failure
        fields = {}
    state.log.error(
        "QUERY_FAILURE category=%s public_category=%s lines=%d bytes=%d %s",
        _safe_cloud_error_category(exc), public_category,
        state.line_count, state.total_bytes, _fingerprint_text(exc, fields),
    )


def _fingerprint_text(exc: Exception, fields: dict) -> str:
    """``fields`` plus :func:`exception_fingerprint` as one ``k=v`` line, or
    ``fingerprint=unavailable``: TOTAL, because it runs inside the ``except``
    that turns a failure into the public ErrorEvent/DoneEvent pair."""
    try:
        merged = dict(fields)
        merged.update(exception_fingerprint(exc))
        return format_fields(merged)
    except Exception:  # noqa: BLE001 - the fingerprint never masks the failure
        return "fingerprint=unavailable"


def _token_or_dash(value: object) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text) else "-"


def _stream_cloud_response(response: dict, state: _CloudStreamState,
                           cancel: 'StreamCancellation | None' = None
                           ) -> Generator[SSEEvent, None, None]:
    try:
        if "response" not in response:
            raise RuntimeError("runtime response missing stream")
        if cancel is not None:
            cancel.arm(response["response"])
        state.log.info("QUERY_STREAM_START runtime_session=%s",
                       _token_or_dash(state.runtime_session_id))
        yield from state.iter_stream(response["response"])
        state.phase = "completion"
        yield from state.completion_events()
        state.log.info(
            "QUERY_SUCCESS lines=%d bytes=%d elapsed_s=%.1f", state.line_count,
            state.total_bytes, time.monotonic() - (state.started or time.monotonic()),
        )
    except Exception as exc:
        # Mid-stream read failure — RUNTIME_UNAVAILABLE (the internal
        # catch-all) maps to 'stream_error' here; auth/timeout/throttle codes
        # keep their specific public categories.
        public_category = _public_cloud_category(exc, default="stream_error")
        _log_stream_failure(state, exc, public_category, cancel)
        yield ErrorEvent(message=public_error_message(public_category), details="",
                         category=public_category, correlation_id=state.session_id)
        yield DoneEvent()
    finally:
        if cancel is not None:
            cancel.release()
        try:
            state.close()
        except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
            pass
def _compose_runtime_payload(base_payload: dict, extra_payload: "dict | None") -> dict:
    """Merge caller-supplied extra payload keys into the runtime payload.

    ADDITIVE ONLY: existing base keys always win, so no caller can override
    ``prompt``/``sessionId``/… through this seam. ``None``/empty (every
    ordinary turn) returns the base payload unchanged — byte-identical to the
    pre-seam behavior. Used by the B2 try-it worker to ride the RESERVED
    ``tryItCandidate`` key to the runtime (popped there unconditionally —
    the ``_adt_identity`` posture, ADR-0049).
    """
    if not extra_payload:
        return base_payload
    merged = dict(base_payload)
    merged.update({key: value for key, value in extra_payload.items()
                   if key not in base_payload})
    return merged


def _with_metric_layer_results(question, session_id: str,
                               extra_payload: "dict | None") -> "dict | None":
    """ADR-0078: add the RESERVED ``metricLayerResults`` key when the question
    names a DEFINED metric (the metric layer's computed result or its verbatim
    withhold, rendered by the runtime before the model runs).

    Runs HERE because this is the ONE place every cloud turn's payload is
    built (web SSE, Evals tab, try-it, async-ask, ask_sync) and because only
    the app process holds the source connections. Always inside a producer
    thread (a sync generator body), never on the event loop. The key is
    HOOK-OWNED: a caller-supplied one is stripped FIRST on every path (hook
    success, no match, disabled, hook fault). Only an import/matcher fault
    leaves no result — no match ⇒ byte-identical.
    """
    extra_payload = without_reserved_results(extra_payload)
    try:
        from .metric_turn_notice import extra_payload_with_results
        return extra_payload_with_results(str(question or ""), session_id, extra_payload)
    except Exception as exc:  # noqa: BLE001 - a notice never breaks a turn
        logging.getLogger("agentcore_client").warning(
            "metric-layer turn notice skipped category=%s", type(exc).__name__)
        return extra_payload


class AgentCoreChatClient:
    def __init__(self, runtime_arn: str | None = None, region: str | None = None):
        cfg = get_config()
        self._runtime_arn = runtime_arn or cfg.agentcore_runtime_arn
        self._region = region or cfg.aws_region
        self._filter = ThinkingFilter()

    def stream(self, prompt: str, session_id: str, user_id: str, plant: str = "",
               write_memory: bool = False, user_access_token: str | None = None,
               raw_question: str | None = None,
               extra_payload: dict | None = None,
               cancel: 'StreamCancellation | None' = None,
               runtime_session_id: str | None = None,
               ) -> Generator[SSEEvent, None, None]:
        """Stream one runtime turn. ``runtime_session_id`` is the AgentCore
        runtime session to invoke with: a caller that must stop the run later
        (the async ask path) mints it with :func:`new_runtime_session_id` and
        keeps it; ``None`` mints a fresh one here, as before. A malformed id
        is refused before any invoke."""
        try:
            yield from self._stream_impl(
                prompt, session_id, user_id, plant, write_memory, user_access_token,
                raw_question, extra_payload, cancel, runtime_session_id,
            )
        except Exception as exc:
            # Setup/invoke failure (before or outside the stream read) — the
            # internal catch-all maps to 'runtime_unavailable' here. Same
            # payload-safe fingerprint as a mid-stream failure (phase=invoke).
            public_category = _public_cloud_category(exc, default="runtime_unavailable")
            logging.getLogger("agentcore_client").error(
                "QUERY_FAILURE category=%s public_category=%s %s",
                _safe_cloud_error_category(exc), public_category,
                _fingerprint_text(exc, {"phase": "invoke"}),
            )
            yield ErrorEvent(message=public_error_message(public_category), details="",
                             category=public_category, correlation_id=session_id)
            yield DoneEvent()

    def _stream_impl(self, prompt: str, session_id: str, user_id: str, plant: str = "",
                     write_memory: bool = False, user_access_token: str | None = None,
                     raw_question: str | None = None,
                     extra_payload: dict | None = None,
                     cancel: 'StreamCancellation | None' = None,
                     runtime_session_id: str | None = None,
                     ) -> Generator[SSEEvent, None, None]:
        from botocore.config import Config

        if runtime_session_id is None:
            runtime_session_id = new_runtime_session_id()
        elif not is_runtime_session_id(runtime_session_id):
            raise ValueError("malformed runtime session id")
        log = logging.getLogger("agentcore_client")
        integrity_mode = _cloud_mode_a()
        client = boto3.client(
            "bedrock-agentcore", region_name=self._region,
            config=Config(read_timeout=RUNTIME_STREAM_READ_TIMEOUT_S, connect_timeout=10),
        )
        raw_question = raw_question if raw_question is not None else getattr(prompt, "raw_question", prompt)
        base_payload = {
            "prompt": prompt, "userId": user_id, "sessionId": session_id,
            "plant": plant, "writeMemory": write_memory,
            "rawPrompt": raw_question,
        }
        extra_payload = _with_metric_layer_results(raw_question, session_id, extra_payload)
        base_payload = _compose_runtime_payload(base_payload, extra_payload)
        payload = json.dumps(with_user_access_token(base_payload, user_access_token)).encode()
        if cancel is not None and not cancel.begin_invoke():
            # Cancelled before the runtime was invoked (the consumer left, or
            # an abandoned async ask was given up during the pre-invoke work):
            # start no run, since closing our side later would not stop it.
            log.info("QUERY_NOT_INVOKED reason=cancelled_before_invoke")
            return
        response = _invoke_cloud_runtime(
            client, self._runtime_arn, runtime_session_id, payload, log,
        )
        state = _CloudStreamState(
            self._filter, integrity_mode=integrity_mode, log=log,
            owner_id=user_id, session_id=session_id,
            runtime_session_id=runtime_session_id,
        )
        yield from _stream_cloud_response(response, state, cancel)

    def _parse_event(self, data: dict) -> SSEEvent | None:
        parsers = (
            lambda: _parse_metadata_event(data),
            lambda: _parse_direct_stream_event(data, self._filter),
            lambda: _parse_callback_event(data, self._filter),
            lambda: _parse_nested_event(data, self._filter),
            lambda: _parse_message_event(data),
            lambda: _parse_legacy_event(data),
        )
        for parse in parsers:
            event = parse()
            if event is not None:
                return event
        return None


# ── Factory ───────────────────────────────────────────────────────────────────

def get_chat_client() -> LocalChatClient | AgentCoreChatClient:
    if get_config().has_runtime:
        return AgentCoreChatClient()
    return LocalChatClient()
