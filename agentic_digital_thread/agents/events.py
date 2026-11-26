# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SSE event model shared between local and cloud chat paths."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Union

# U+2010 HYPHEN and U+2011 NON-BREAKING HYPHEN render as "-" and are only ever
# hyphens; dashes (U+2013/U+2014) are punctuation and are left alone.
_HYPHEN_VARIANTS = str.maketrans({"\u2010": "-", "\u2011": "-"})


def now_ms() -> int:
    """Epoch milliseconds — the server-truth timestamp stamped on tool events."""
    return int(time.time() * 1000)


#: Compatibility cap for bounded envelope metadata (raw previews are prohibited).
ENVELOPE_PREVIEW_CHARS = 200


def log_tool_result_envelope(log, tool_use_id: str, status: str, content: Any) -> None:
    """Log only tool-result ID, status, and bounded structural shape."""
    try:
        if isinstance(content, (list, tuple)):
            shape = f"{type(content).__name__}[{len(content)}]"
        elif isinstance(content, dict):
            shape = f"dict(keys={sorted(str(k) for k in content)[:5]})"
        elif isinstance(content, str):
            shape = f"str({len(content)})"
        elif content is None:
            shape = "none"
        else:
            shape = type(content).__name__
        shape = shape[:ENVELOPE_PREVIEW_CHARS]
        identifier = hashlib.sha256(str(tool_use_id).encode()).hexdigest()[:12]
        safe_status = str(status).lower() if str(status).lower() in {
            "success", "succeeded", "error", "failed", "partial",
        } else "unknown"
        log.info("TOOL_RESULT_ENVELOPE id_hash=%s status=%s shape=%s",
                 identifier, safe_status, shape)
    except Exception:  # nosec B110 - forensics only, never break the stream
        pass


@dataclass
class MessageEvent:
    content: str

    def __post_init__(self) -> None:
        # Sonnet 5 writes identifiers with U+2011 NON-BREAKING HYPHEN
        # ("RUN‑20260929‑90001"): they look identical, but a copied ID no longer
        # matches the record, and the golden regexes miss it (live rehearsal
        # 2026-10-01). Each is one code point, so a per-chunk replace is exact.
        if isinstance(self.content, str):
            self.content = self.content.translate(_HYPHEN_VARIANTS)

    def to_sse_format(self) -> str:
        return f"event: token\ndata: {json.dumps({'text': self.content})}\n\n"


@dataclass
class ToolUseEvent:
    tool_name: str
    tool_input: Any = None
    tool_use_id: str = ""
    status: str = "started"
    #: Server-side emit timestamp (epoch ms), stamped by the emitter at
    #: composition-emit (chat_client). None = not stamped (older producers);
    #: the field is OMITTED from the SSE payload when None so existing
    #: consumers/tests see byte-identical frames.
    ts: int | None = None

    def to_sse_format(self) -> str:
        data = {"tool": self.tool_name, "tool_id": self.tool_use_id, "n": 0, "input": self.tool_input}
        if self.ts is not None:
            data["ts"] = self.ts
        return f"event: tool_start\ndata: {json.dumps(data)}\n\n"


@dataclass
class ToolResultEvent:
    tool_name: str
    tool_result: Any = None
    tool_use_id: str = ""
    status: str = "completed"
    summary: str = ""
    #: Server-side receipt timestamp (epoch ms), stamped by the emitter when
    #: the result arrives. Omitted from the SSE payload when None (back-compat).
    ts: int | None = None
    #: Server-known call duration (ms), paired by toolUseId at the emitter
    #: (tool_start emit → tool_result receipt). The client's own wall-clock
    #: pill timing was skewed by wire buffering — this is the server truth.
    elapsed_ms: int | None = None
    #: The ROUTE context the gateway sent this system delegation when it
    #: differs from the model's own arguments (``route_facts``), for the
    #: trace's Routing context. Omitted from the SSE payload when None.
    route_context: str | None = None
    #: The telemetry sample line the system runtime stored for this call
    #: (``investigation_trace_store``), for the trace step to show as its own
    #: line below ``summary``. Display only: ``summary`` stays the summary the
    #: outcome classifiers read. Omitted from the SSE payload when empty.
    sample_line: str | None = None
    #: The telemetry-coverage lines the system runtime stored for this call,
    #: for the trace step to show above ``summary``. Display only, like
    #: ``sample_line``. Omitted from the SSE payload when empty.
    telemetry_coverage: list[str] | None = None

    def to_sse_format(self) -> str:
        data = {"tool": self.tool_name, "tool_id": self.tool_use_id, "summary": self.summary, "input": self.tool_result}
        if self.ts is not None:
            data["ts"] = self.ts
        if self.elapsed_ms is not None:
            data["elapsed_ms"] = self.elapsed_ms
        if self.route_context is not None:
            data["route_context"] = self.route_context
        if self.sample_line:
            data["sample_line"] = self.sample_line
        if self.telemetry_coverage:
            data["telemetry_coverage"] = list(self.telemetry_coverage)
        return f"event: tool_result\ndata: {json.dumps(data)}\n\n"


# ── Public error taxonomy ─────────────────────────────────────────────────────
# CLOSED vocabulary for the `category` field on `event: error` frames. The
# claim-check policy forbids raw upstream error TEXT from reaching the client
# (it can quote row values), so the category — not the text — is the ONLY
# machine-readable signal of WHICH failure fired. The public text is always a
# fixed per-category message from PUBLIC_ERROR_MESSAGES below; producers
# (chat_client) and the server-side frame filter (server._fixed_public_sse)
# both read this ONE table.
ERROR_CATEGORIES = frozenset({
    "gateway_auth",         # the forwarded bearer was rejected by the query gateway
    "runtime_unavailable",  # the analysis runtime could not be reached/invoked
    "timeout",              # the query timed out
    "runtime_error",        # the runtime reported an error frame/dict
    "stream_error",         # the app-side stream read failed mid-turn
    "output_limit",         # the answer outgrew the model's output limit twice
    "unknown",              # unclassified / legacy producers
})

#: Fixed public replacement text per category — the ONE message table used by
#: both the chat client (event producers) and server._fixed_public_sse (the
#: public frame filter). NEVER interpolate upstream text into these.
_GENERIC_QUERY_FAILED = (
    "Query failed. Retry in a moment; if this keeps happening, report it."
)
PUBLIC_ERROR_MESSAGES = {
    "gateway_auth": ("Query failed: the session could not be authorized to the "
                     "query gateway — sign out and back in if this persists."),
    "runtime_unavailable": ("Query failed: the analysis runtime was unavailable. "
                            "Retry in a moment."),
    # Says what to do next. Keep the word-boundary "timed out" token: the
    # smoke/eval error taxonomy (scripts/smoke_prompts.py ERROR_PATTERNS)
    # labels this frame TIMEOUT from its text.
    "timeout": "The question timed out. Ask again, or ask a narrower question.",
    "runtime_error": _GENERIC_QUERY_FAILED,
    "stream_error": _GENERIC_QUERY_FAILED,
    # The orchestrator's reply hit its output limit, and so did the one
    # shorter attempt (agents/output_cap_recovery.py). Not an outage: asking
    # the same question again would most likely stop the same way, so this
    # says what to change instead.
    "output_limit": ("The answer was too long to finish, even after one shorter "
                     "attempt. Ask a narrower question: fewer systems, a shorter "
                     "period, or a count or summary instead of a full list."),
    "unknown": _GENERIC_QUERY_FAILED,
}

#: Bound on the correlation id echoed back in a public error frame — the id is
#: app-minted (session/turn id), never upstream text; the cap is defensive.
MAX_CORRELATION_ID_CHARS = 128


def public_error_message(category: str) -> str:
    """The fixed public text for *category* (unknown categories degrade)."""
    return PUBLIC_ERROR_MESSAGES.get(category, PUBLIC_ERROR_MESSAGES["unknown"])


def parse_public_error_frame(frame: str) -> tuple[str, str]:
    """Extract ``(category, correlation_id)`` from an ``event: error`` frame.

    Used by the server-side public frame filter to rebuild the frame with the
    fixed per-category text while PRESERVING the classification. NEVER returns
    upstream text: only a closed-vocabulary category and the app-minted
    correlation id survive; anything malformed degrades to ``("unknown", "")``.
    """
    category, correlation_id = "unknown", ""
    for line in frame.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except (json.JSONDecodeError, ValueError):
            break
        if isinstance(data, dict):
            raw_category = data.get("category")
            if isinstance(raw_category, str) and raw_category in ERROR_CATEGORIES:
                category = raw_category
            raw_id = data.get("correlation_id")
            if isinstance(raw_id, str):
                correlation_id = raw_id[:MAX_CORRELATION_ID_CHARS]
        break
    return category, correlation_id


@dataclass
class ErrorEvent:
    message: str
    details: str = ""
    #: Closed-taxonomy failure class (ERROR_CATEGORIES). Values outside the
    #: taxonomy serialize as "unknown" — the vocabulary never widens silently.
    category: str = "unknown"
    #: App-minted per-turn/session id (the /api/ask ``session_id``) so a user
    #: can quote a reference. NEVER an upstream-provided value; omitted from
    #: the payload when empty.
    correlation_id: str = ""

    def to_sse_format(self) -> str:
        category = self.category if self.category in ERROR_CATEGORIES else "unknown"
        data: dict[str, Any] = {"text": self.message, "category": category}
        if self.correlation_id:
            data["correlation_id"] = str(self.correlation_id)[:MAX_CORRELATION_ID_CHARS]
        return f"event: error\ndata: {json.dumps(data)}\n\n"


@dataclass
class MetadataEvent:
    data: dict

    def to_sse_format(self) -> str:
        return f"event: metadata\ndata: {json.dumps(self.data)}\n\n"


@dataclass
class DoneEvent:
    text: str = ""

    def to_sse_format(self) -> str:
        return f"event: done\ndata: {json.dumps({'text': self.text})}\n\n"


@dataclass
class GuardrailEvent:
    source: str = "INPUT"
    action: str = "NONE"
    assessments: list = field(default_factory=list)

    def to_sse_format(self) -> str:
        data = {"source": self.source, "action": self.action, "assessments": self.assessments}
        return f"event: guardrail\ndata: {json.dumps(data)}\n\n"


@dataclass
class ToolEndEvent:
    tool_name: str

    def to_sse_format(self) -> str:
        return f"event: tool_end\ndata: {json.dumps({'tool': self.tool_name})}\n\n"


@dataclass
class ReasoningEvent:
    """Model reasoning/progress surfaced between tool batches (Task 1I).

    ADDITIVE event type: older frontends drop unknown SSE event names, so
    emitting this never changes tool_start/token semantics. Two producers:

    - Cloud/local reasoning deltas — when the model streams ``reasoningContent``
      (Strands ``ReasoningTextStreamEvent``: ``{"reasoningText": str, ...}``),
      the text is forwarded here. NEVER routed through the token stream (would
      pollute the answer).
    - Deferral-seam heartbeat — while ``chat_client`` defers streaming
      ``current_tool_use`` events (partial JSON input) until the assistant
      message block, the first delta per toolUseId emits a coarse
      ``preparing <tool> query…`` heartbeat (tool NAME only, never the partial
      JSON) so the input-composition window is visible instead of silent.
    """

    text: str

    def to_sse_format(self) -> str:
        return f"event: reasoning\ndata: {json.dumps({'text': self.text})}\n\n"


@dataclass
class CredentialRequestEvent:
    system_id: str
    connection_type: str
    fields: list = field(default_factory=list)

    def to_sse_format(self) -> str:
        data = {"system_id": self.system_id, "connection_type": self.connection_type, "fields": self.fields}
        return f"event: credential_request\ndata: {json.dumps(data)}\n\n"


SSEEvent = Union[
    MessageEvent,
    ToolUseEvent,
    ToolResultEvent,
    ErrorEvent,
    MetadataEvent,
    DoneEvent,
    GuardrailEvent,
    ToolEndEvent,
    ReasoningEvent,
    CredentialRequestEvent,
]
