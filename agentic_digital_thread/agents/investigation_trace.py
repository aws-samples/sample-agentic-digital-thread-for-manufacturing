# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Investigation-trace capture — the pure bounding home (Wave 6, W6-F2/F3).

Stdlib-pure (the ``provenance.py`` posture — import-purity pinned): no
boto3, no strands, no intra-package imports. The capture hook
(``tool_result_capture.InvocationToolResultCapture``) delegates every
trace-step record here; the store (``investigation_trace_store``) imports
the SAME caps for its write-side defense-in-depth — one caps home, no drift.

DELIBERATE posture change (W6-F2, documented not hidden): ``tool_input`` /
``tool_output`` carry bounded RAW payload content — a scoped relaxation of
``tool_result_capture``'s metadata-only allowlist, justified because the
ONLY sink is the out-of-band, admin-gated, TTL'd investigation-trace store
(the same sink class as ``executed_query``). Nothing built here ever enters
a tool result, the SSE stream, or any LLM token path, and log lines carry
digests/counts only.

State model (the Wave-5 concurrency posture): everything invocation-local
lives in ``invocation_state`` under ``_adt_*`` reserved keys — never a
contextvar read inside strands executor threads. Reasoning attribution is
anchored on the globally-unique ``toolUseId``; any miss is an honest ``""``,
never a guess.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

#: Hard cap on retained steps per investigation (W6-F2) — over-cap drops the
#: OLDEST step and counts it into ``steps_dropped`` (honest, never silent).
MAX_TRACE_STEPS = 40

#: Per-field byte caps (W6-F2) — UTF-8 bytes, clean codepoint boundaries.
MAX_STEP_INPUT_BYTES = 2048
MAX_STEP_OUTPUT_BYTES = 4096
MAX_REASONING_BYTES = 2048
MAX_QUESTION_BYTES = 2048
MAX_FINAL_ANSWER_BYTES = 4096

#: Whole-entry serialized cap (W6-F2): enforced by the oldest-dropped
#: ratchet in ``build_trace_entry`` and re-enforced at store-normalize.
MAX_TRACE_ENTRY_BYTES = 65536

#: Metadata bounds — spelled locally (the import-purity pin allows stdlib
#: only); MUST mirror ``agents.provenance.MAX_SYSTEM_ID_LEN`` /
#: ``MAX_TOOL_NAME_LEN`` (equality pinned in test_investigation_trace.py).
MAX_TRACE_SYSTEM_ID_LEN = 100
MAX_TRACE_TOOL_NAME_LEN = 160
_MAX_TIMESTAMP_LEN = 120

#: Reserved ``invocation_state`` keys (``_adt_`` family — ADR-0049 spelling;
#: the serializer's defensive key-strip below covers them structurally).
TRACE_STATE_KEY = "_adt_trace_steps"
_TRACE_PENDING_KEY = "_adt_trace_pending"
_TRACE_ANCHORS_KEY = "_adt_trace_anchors"
_TRACE_DROPPED_KEY = "_adt_trace_dropped"

#: Bound on the Before→After pending-start map (unmatched Befores can only
#: accumulate when After never fires — cap keeps the state dict small).
_MAX_PENDING_STARTS = 64

#: The frozen W6-F2 status vocabulary.
TRACE_STATUSES = ("success", "error", "exception", "cancelled", "unknown")


def _now_utc_str() -> str:
    """The D1 ``captured_at`` wire spelling (whole seconds, ``Z`` suffix) —
    same format as ``provenance.format_captured_at``, spelled locally for
    the import-purity pin."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded_utf8(text: Any, limit: int) -> tuple[str, bool, int]:
    """``(bounded_text, truncated, total_bytes)`` — first ``limit`` UTF-8
    bytes on a clean codepoint boundary (a split codepoint is dropped, never
    emitted broken). Non-str coerces defensively (None ⇒ '')."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    raw = text.encode("utf-8")
    total = len(raw)
    if total <= limit:
        return text, False, total
    cut = raw[: max(0, limit)].decode("utf-8", errors="ignore")
    return cut, True, total


def serialize_tool_payload(value: Any) -> str:
    """Compact serialized form of a tool input/output payload.

    Strings pass through verbatim; dicts first drop every ``_adt_*``
    reserved key (defense-in-depth — the ASGI seam already popped them
    before the inner agent existed; the leak pin makes it structural);
    everything else renders as compact JSON (``default=str`` — never
    raises on exotic types)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = {k: v for k, v in value.items()
                 if not (isinstance(k, str) and k.startswith("_adt_"))}
    try:
        return json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def record_step_start(invocation_state: dict, tool_use: Any) -> None:
    """Stamp the monotonic start + wall-clock ``started_at`` for one tool
    call, keyed by its ``toolUseId`` (W6-F3 timing). Missing/bad id or an
    over-cap pending map ⇒ no stamp (the step then records an honest
    ``duration_ms: null``)."""
    tool_use_id = tool_use.get("toolUseId") if isinstance(tool_use, dict) else None
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return
    pending = invocation_state.setdefault(_TRACE_PENDING_KEY, {})
    if not isinstance(pending, dict) or len(pending) >= _MAX_PENDING_STARTS:
        return
    pending[tool_use_id] = (time.monotonic(), _now_utc_str())


def extract_reasoning(messages: Any, tool_use_id: Any, seen_anchors: set) -> str:
    """The assistant's OWN text segments from the message that issued this
    tool call (W6-F3) — already-emitted output, NOT chain-of-thought.

    End-scan of ``messages`` for the assistant message whose content holds a
    ``toolUse`` block with THIS ``toolUseId`` (globally unique — interleaved
    concurrent histories cannot mis-attribute). Dedup: the message's FIRST
    toolUseId is the anchor; a second toolUse of the same message — or an
    anchor already attributed — returns ``""``. Any miss ⇒ ``""`` (honest-
    empty, never guessed). Returns UNBOUNDED text; the caller bounds it.
    """
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return ""
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        ids = [block["toolUse"].get("toolUseId") for block in content
               if isinstance(block, dict) and isinstance(block.get("toolUse"), dict)]
        if tool_use_id not in ids:
            continue
        anchor = ids[0]
        if anchor != tool_use_id or anchor in seen_anchors:
            return ""
        seen_anchors.add(anchor)
        texts = [block["text"] for block in content
                 if isinstance(block, dict) and isinstance(block.get("text"), str)
                 and block["text"].strip()]
        return "\n\n".join(texts)
    return ""


def _result_payload(result: Any) -> Any:
    """The ToolResult content for serialization (dict ``content`` first,
    attribute ``content`` second, the raw result last)."""
    if isinstance(result, dict) and "content" in result:
        return result.get("content")
    content = getattr(result, "content", None)
    return content if content is not None else result


def _result_status(result: Any) -> str:
    """Transport status normalized to the frozen W6-F2 vocabulary."""
    raw = (result.get("status") if isinstance(result, dict)
           else getattr(result, "status", None))
    normalized = str(raw or "").lower()
    if normalized in ("success", "succeeded", "ok"):
        return "success"
    if normalized in ("error", "failed", "failure"):
        return "error"
    if normalized in ("cancelled", "canceled"):
        return "cancelled"
    return "unknown"


def _pop_pending(invocation_state: dict, tool_use_id: Any) -> tuple:
    """``(duration_ms | None, started_at)`` — the Before-stamped pair, or
    honest ``(None, now)`` when Before never fired (W6-F3)."""
    pending = invocation_state.get(_TRACE_PENDING_KEY)
    stamp = (pending.pop(tool_use_id, None)
             if isinstance(pending, dict) and isinstance(tool_use_id, str)
             else None)
    if not (isinstance(stamp, tuple) and len(stamp) == 2):
        return None, _now_utc_str()
    return int((time.monotonic() - stamp[0]) * 1000), stamp[1]


def _records_read_fact(exception: Any, records_read: Any) -> dict:
    """``{"records_read": n}`` for a call that returned, when its tool
    reported a whole non-negative count of the records it parsed from the
    source's response (``tool_result_capture.report_read_count``), else
    ``{}``. The onboarding agent test reads it (``entity_reads``)."""
    if (exception is None and isinstance(records_read, int)
            and not isinstance(records_read, bool) and records_read >= 0):
        return {"records_read": records_read}
    return {}


def record_step(invocation_state: dict, *, tool_use: Any, result: Any,
                exception: Any, retry: Any, messages: Any,
                records_read: Any = None) -> None:
    """Append ONE bounded step dict for a completed tool call (W6-F3).

    ``retry`` truthy ⇒ no record (only the FINAL attempt's result is emitted
    — strands 1.42.0 retry semantics). Over ``MAX_TRACE_STEPS`` drops the
    OLDEST step and counts it (``seq`` preserves the original position, so
    drops stay visible). ``records_read`` is kept by
    :func:`_records_read_fact`."""
    if retry:
        return
    tool_use = tool_use if isinstance(tool_use, dict) else {}
    steps = invocation_state.setdefault(TRACE_STATE_KEY, [])
    anchors = invocation_state.setdefault(_TRACE_ANCHORS_KEY, set())
    if not isinstance(steps, list) or not isinstance(anchors, set):
        return
    dropped = invocation_state.get(_TRACE_DROPPED_KEY, 0)
    dropped = dropped if isinstance(dropped, int) and dropped >= 0 else 0
    tool_use_id = tool_use.get("toolUseId")
    duration_ms, started_at = _pop_pending(invocation_state, tool_use_id)
    reasoning, r_trunc, _ = _bounded_utf8(
        extract_reasoning(messages, tool_use_id, anchors), MAX_REASONING_BYTES)
    tool_input, in_trunc, in_bytes = _bounded_utf8(
        serialize_tool_payload(tool_use.get("input")), MAX_STEP_INPUT_BYTES)
    if exception is not None:
        tool_output, out_trunc, out_bytes, status = "", False, 0, "exception"
    else:
        tool_output, out_trunc, out_bytes = _bounded_utf8(
            serialize_tool_payload(_result_payload(result)), MAX_STEP_OUTPUT_BYTES)
        status = _result_status(result)
    step = {"seq": dropped + len(steps) + 1, "reasoning": reasoning,
            "tool_name": str(tool_use.get("name") or "")[:MAX_TRACE_TOOL_NAME_LEN],
            "tool_input": tool_input, "input_bytes": in_bytes,
            "tool_output": tool_output, "output_bytes": out_bytes,
            "status": status, "duration_ms": duration_ms,
            "started_at": started_at, **_records_read_fact(exception, records_read)}
    for flag, value in (("reasoning_truncated", r_trunc),
                        ("input_truncated", in_trunc),
                        ("output_truncated", out_trunc)):
        if value:
            step[flag] = True
    steps.append(step)
    if len(steps) > MAX_TRACE_STEPS:
        steps.pop(0)
        invocation_state[_TRACE_DROPPED_KEY] = dropped + 1


def collect_trace_steps(invocation_state: Any) -> tuple[list[dict], int]:
    """PURE read-only ``(bounded step copies, dropped_count)`` — read in the
    outer ``_run_mode_a_query`` frame only (the Wave-5 posture)."""
    state = invocation_state if isinstance(invocation_state, dict) else {}
    dropped = state.get(_TRACE_DROPPED_KEY, 0)
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
        dropped = 0
    raw = state.get(TRACE_STATE_KEY)
    if not isinstance(raw, list):
        return [], dropped
    return ([dict(step) for step in raw[:MAX_TRACE_STEPS]
             if isinstance(step, dict)], dropped)


def _entry_bytes(entry: dict) -> int:
    """Serialized size the 64 KB ratchet measures (compact JSON — the same
    spelling the store persists)."""
    return len(json.dumps(entry, default=str,
                          separators=(",", ":")).encode("utf-8"))


def _enforce_entry_cap(entry: dict) -> dict:
    """The W6-F2 whole-entry ratchet: while over ``MAX_TRACE_ENTRY_BYTES``
    pop the OLDEST step (counted into ``steps_dropped`` — never silent);
    a pathological zero-step over-cap entry hard-truncates ``final_answer``
    then ``question`` with flags. Never raises, never fails the write."""
    while _entry_bytes(entry) > MAX_TRACE_ENTRY_BYTES and entry["steps"]:
        entry["steps"].pop(0)
        entry["steps_dropped"] += 1
    for field, flag in (("final_answer", "final_answer_truncated"),
                        ("question", "question_truncated")):
        while _entry_bytes(entry) > MAX_TRACE_ENTRY_BYTES and entry[field]:
            halved = len(entry[field].encode("utf-8")) // 2
            entry[field], _, _ = _bounded_utf8(entry[field], halved)
            entry[flag] = True
    return entry


def build_trace_entry(*, session_id: str, query_ref: str, system_id: str,
                      question: str, kg_context_attached: bool,
                      steps: list[dict], steps_dropped: int,
                      final_answer: str,
                      telemetry_coverage: "list[str] | None" = None,
                      telemetry_sample: str = "") -> dict:
    """The W6-F2 store entry. ``question`` is the raw DELEGATED sub-question
    (never the kg-augmented text — the caller passes the original param and
    only the ``kg_context_attached`` bool); ``final_answer`` is exactly what
    ``_run_mode_a_query`` returns. Enforces the 64 KB oldest-dropped
    ratchet; ``*_truncated`` flags present only when true (honest).
    ``telemetry_coverage`` (already-bounded lines from
    ``telemetry_coverage.collect_coverage``; the store re-bounds them) is
    present ONLY when non-empty, so coverage-free entries stay byte-identical.
    ``telemetry_sample`` (the one sample line ``telemetry_coverage.
    sample_line`` rendered for the call; the store re-bounds it) follows the
    same rule."""
    bounded_q, q_trunc, _ = _bounded_utf8(question, MAX_QUESTION_BYTES)
    bounded_a, a_trunc, a_bytes = _bounded_utf8(final_answer,
                                                MAX_FINAL_ANSWER_BYTES)
    dropped = steps_dropped if (isinstance(steps_dropped, int)
                                and not isinstance(steps_dropped, bool)
                                and steps_dropped >= 0) else 0
    entry = {
        "query_ref": str(query_ref or ""),
        "session_id": str(session_id or ""),
        "system_id": str(system_id or "")[:MAX_TRACE_SYSTEM_ID_LEN],
        "origin": "system-agent",
        "question": bounded_q,
        "kg_context_attached": bool(kg_context_attached),
        "steps": [dict(step) for step in (steps or [])
                  if isinstance(step, dict)][:MAX_TRACE_STEPS],
        "steps_dropped": dropped,
        "final_answer": bounded_a,
        "final_answer_bytes": a_bytes,
        "captured_at": _now_utc_str(),
    }
    if q_trunc:
        entry["question_truncated"] = True
    if a_trunc:
        entry["final_answer_truncated"] = True
    coverage = [line for line in (telemetry_coverage or [])
                if isinstance(line, str) and line]
    if coverage:
        entry["telemetry_coverage"] = coverage
    if isinstance(telemetry_sample, str) and telemetry_sample:
        entry["telemetry_sample"] = telemetry_sample
    return _enforce_entry_cap(entry)
