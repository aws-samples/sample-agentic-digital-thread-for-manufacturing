# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Replica-coherent investigation-trace store (Wave 6, W6-F2).

Thin wrapper over :class:`agents.job_store.JobStore` with
``kind="investigation-trace"`` — the SAME onboarding-sessions table the
Wave-5 provenance-query store rides (write-through DynamoDB, fail-soft to
in-memory, TTL'd; ZERO new infrastructure — Wave-5 Task A2 already granted
the runtime role and plumbed ``ONBOARDING_SESSIONS_TABLE``). The system-agent
writer (``system_agent._write_investigation_trace``) puts ONE entry per
cited Mode-A call; the admin-gated D5-family sibling route
(``agents_trace_routes.get_investigation_trace``) reads it back by
``(session_id, query_ref)`` — the byte-identical Wave-5 F1 join key.

Contracts (frozen — W6-F2):

- Entry shape is the W6-F2 dict built by
  ``investigation_trace.build_trace_entry``; ALL caps are imported from that
  ONE home and re-enforced here at write (defense-in-depth) — including the
  64 KB whole-entry oldest-dropped ratchet.
- ``tool_input``/``tool_output`` carry bounded RAW payload content — the
  DELIBERATE, documented W6-F2 posture change (same sink class as
  ``executed_query``): out-of-band store only, admin-gated redemption, 24 h
  TTL. Nothing here ever re-enters a tool result, the SSE stream, or any
  LLM token path.
- TTL, key regexes, and the digest helper are IMPORTED from
  ``provenance_store`` (one home each — no drift).
- Logging NEVER carries reasoning, input, output, question, or answer text:
  digests (``sha256[:12]``) and counts only.
- ``None``/``False`` are the only miss/failure signals — malformed anything
  is dropped, never raised (a trace bug must never break the answer path).
"""
from __future__ import annotations

import logging

from agents import investigation_trace as _trace
from agents import telemetry_coverage as _coverage
from agents.engine_names import printable_id as _printable_id
from agents.job_store import JobStore
from agents.provenance_store import (
    PROVENANCE_QUERY_TTL_SECONDS,
    QUERY_REF_RE,
    SESSION_ID_RE,
    capability_suffix,
    digest12,
    read_entry,
    valid_write_token,
)

logger = logging.getLogger(__name__)

#: JobStore namespace for this family (shared-table key prefix). DISTINCT
#: from the Wave-5 ``provenance-query`` kind: a query entry under the same
#: ``(sid, ref)`` never satisfies a trace read (kind-isolation pinned).
_KIND = "investigation-trace"

# Lazily-constructed module singleton; tests inject a JobStore built on a
# fake table by assigning ``investigation_trace_store._job_store`` directly.
_job_store: JobStore | None = None


def _jobs() -> JobStore:
    """The one ``JobStore(kind="investigation-trace")`` for this process —
    TTL imported from the Wave-5 store (24 h, one home; see the
    ``PROVENANCE_QUERY_TTL_SECONDS`` rationale there)."""
    global _job_store
    if _job_store is None:
        _job_store = JobStore(kind=_KIND,
                              ttl_seconds=PROVENANCE_QUERY_TTL_SECONDS)
    return _job_store


def _valid_key(session_id: object, query_ref: object) -> bool:
    return (isinstance(session_id, str) and bool(SESSION_ID_RE.match(session_id))
            and isinstance(query_ref, str) and bool(QUERY_REF_RE.match(query_ref)))


def _bounded(value: object, limit: int, carried_flag: object) -> tuple[str, bool, int]:
    """Re-bound one text field at write: ``(text, truncated, total_bytes)``
    with the WRITER's honest flag preserved (an already-bounded field keeps
    its ``*_truncated`` even though re-bounding is a no-op)."""
    text, truncated, total = _trace._bounded_utf8(
        value if isinstance(value, str) else "", limit)
    return text, truncated or carried_flag is True, total


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _normalize_step(raw: object) -> dict | None:
    """One W6-F2 step, bounds re-enforced from the A1 caps home; non-dict ⇒
    dropped (``None``). ``*_bytes`` keep the writer's honest full size when
    valid, else fall back to the stored text's size (never negative)."""
    if not isinstance(raw, dict):
        return None
    reasoning, r_trunc, _ = _bounded(raw.get("reasoning"),
                                     _trace.MAX_REASONING_BYTES,
                                     raw.get("reasoning_truncated"))
    tool_input, in_trunc, in_size = _bounded(raw.get("tool_input"),
                                             _trace.MAX_STEP_INPUT_BYTES,
                                             raw.get("input_truncated"))
    tool_output, out_trunc, out_size = _bounded(raw.get("tool_output"),
                                                _trace.MAX_STEP_OUTPUT_BYTES,
                                                raw.get("output_truncated"))
    status = raw.get("status")
    duration = raw.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        duration = None
    step = {
        "seq": _int_or(raw.get("seq"), 0),
        "reasoning": reasoning,
        "tool_name": (raw.get("tool_name") if isinstance(raw.get("tool_name"), str)
                      else "")[:_trace.MAX_TRACE_TOOL_NAME_LEN],
        "tool_input": tool_input,
        "input_bytes": _int_or(raw.get("input_bytes"), in_size),
        "tool_output": tool_output,
        "output_bytes": _int_or(raw.get("output_bytes"), out_size),
        "status": status if status in _trace.TRACE_STATUSES else "unknown",
        "duration_ms": duration,
        "started_at": (raw.get("started_at") if isinstance(raw.get("started_at"), str)
                       else "")[:120],
    }
    for flag, value in (("reasoning_truncated", r_trunc),
                        ("input_truncated", in_trunc),
                        ("output_truncated", out_trunc)):
        if value:
            step[flag] = True
    step.update(_trace._records_read_fact(None, raw.get("records_read")))
    return step


def _normalize_trace(session_id: str, query_ref: str, entry: dict) -> dict:
    """The W6-F2 entry with every bound re-enforced (defense-in-depth even
    though ``build_trace_entry`` already did it), the writer's honest
    ``*_truncated`` flags preserved, and the 64 KB oldest-dropped ratchet
    re-applied via the ONE enforcement home."""
    question, q_trunc, _ = _bounded(entry.get("question"),
                                    _trace.MAX_QUESTION_BYTES,
                                    entry.get("question_truncated"))
    answer, a_trunc, a_size = _bounded(entry.get("final_answer"),
                                       _trace.MAX_FINAL_ANSWER_BYTES,
                                       entry.get("final_answer_truncated"))
    raw_steps = entry.get("steps")
    steps = [step for step in ((_normalize_step(raw) for raw in raw_steps)
                               if isinstance(raw_steps, list) else ())
             if step is not None][:_trace.MAX_TRACE_STEPS]
    system_id = entry.get("system_id")
    normalized = {
        "query_ref": query_ref,
        "session_id": session_id,
        "system_id": (system_id if isinstance(system_id, str)
                      else "")[:_trace.MAX_TRACE_SYSTEM_ID_LEN],
        "origin": "system-agent",
        "question": question,
        "kg_context_attached": entry.get("kg_context_attached") is True,
        "steps": steps,
        "steps_dropped": _int_or(entry.get("steps_dropped"), 0),
        "final_answer": answer,
        "final_answer_bytes": _int_or(entry.get("final_answer_bytes"), a_size),
        "captured_at": (entry.get("captured_at")
                        if isinstance(entry.get("captured_at"), str) else "")[:120],
    }
    if q_trunc:
        normalized["question_truncated"] = True
    if a_trunc:
        normalized["final_answer_truncated"] = True
    coverage = _coverage.bounded_lines(entry.get("telemetry_coverage"))
    if coverage:
        normalized["telemetry_coverage"] = coverage
    sample = _coverage.bounded_sample_line(entry.get("telemetry_sample"), _printable_id)
    if sample:
        normalized["telemetry_sample"] = sample
    return _trace._enforce_entry_cap(normalized)


def put_trace(session_id: str, query_ref: str, entry: dict,
              write_token: "str | None" = None) -> bool:
    """Persist ONE investigation-trace entry.

    With a valid ``write_token`` (the per-call capability the orchestrator
    forwarded) the entry is stored under the opaque capability suffix
    ``cap:<sha256(token|sid|ref)>`` so a caller without the token cannot
    overwrite it; without a token the legacy ``<sid>:<ref>`` key is used
    (local mode and pre-capability entries). Bounds enforced at write;
    malformed key or non-dict entry ⇒ dropped (``False``); it never raises, so
    a bad trace cannot break the Mode-A answer (the writer swallow-guards
    its call too). ``JobStore`` degrades to in-memory on any backend error
    (honest 404 after restart/expiry).
    """
    if not _valid_key(session_id, query_ref) or not isinstance(entry, dict):
        logger.debug("investigation_trace_store: put rejected sid_sha=%s",
                     digest12(str(session_id)))
        return False
    normalized = _normalize_trace(session_id, query_ref, entry)
    token = write_token if valid_write_token(write_token) else None
    job_id = (f"cap:{capability_suffix(token, session_id, query_ref)}"
              if token else f"{session_id}:{query_ref}")
    _jobs()[job_id] = normalized
    logger.info(
        "investigation_trace_store: put sid_sha=%s ref_sha=%s steps=%d dropped=%d capability=%s",
        digest12(session_id), digest12(query_ref),
        len(normalized["steps"]), normalized["steps_dropped"], bool(token))
    return True


def get_trace(session_id: str, query_ref: str, *, consistent: bool = False) -> dict | None:
    """The stored W6-F2 entry, or ``None`` when unknown/expired/invalid.

    Resolves the entry through the per-call capability record when one exists
    (so a forged write under a replayed pair is never returned), else the
    legacy key. ``None`` is the ONLY miss signal: the redemption route maps
    it to the structured 404 ("Unknown or expired investigation trace");
    never fabricated, never raises on malformed ids. A Wave-5
    ``provenance-query`` entry under the same key does NOT satisfy this read
    (kind isolation: the stores are separate JobStore instances).

    ``consistent`` reads the shared table with ``ConsistentRead``, for a
    reader that must see a write another process has just acknowledged (the
    onboarding agent test reads the trace its call's system runtime wrote a
    moment before it answered); a failed consistent read is a miss.
    """
    if not _valid_key(session_id, query_ref):
        return None
    return read_entry(_jobs(), session_id, query_ref, consistent=consistent)


def stored_durably(session_id: str, query_ref: str, suffix: "str | None" = None) -> bool:
    """Whether the shared table holds a trace entry for the pair: under the
    capability ``suffix`` when given, else under the legacy key. Existence
    only: ``JobStore.get_durable`` never caches, and the entry is not
    returned. Never raises."""
    key = f"cap:{suffix}" if suffix else f"{session_id}:{query_ref}"
    return _jobs().get_durable(key) is not None


def get_telemetry_coverage(session_id: str, query_ref: str) -> list[str]:
    """ONLY the bounded ``telemetry_coverage`` lines of one stored entry —
    the ECS trace-summary read (names/counts/timestamps; never the entry's
    raw ``tool_input``/``tool_output``). ``[]`` on any miss/invalid key;
    never raises past the store's own fail-soft reads."""
    entry = get_trace(session_id, query_ref)
    return _coverage.bounded_lines(entry.get("telemetry_coverage")) if entry else []


def get_trace_telemetry(session_id: str, query_ref: str) -> "tuple[list[str], str]":
    """``(coverage lines, sample line)`` of one stored entry, in one read:
    the bounded ``telemetry_coverage`` lines and the bounded
    ``telemetry_sample`` line the system runtime wrote under the call's
    capability (``([], "")`` on any miss). The ECS trace summary's read;
    never the entry's raw ``tool_input``/``tool_output``."""
    entry = get_trace(session_id, query_ref)
    if not entry:
        return [], ""
    return (_coverage.bounded_lines(entry.get("telemetry_coverage")),
            _coverage.bounded_sample_line(entry.get("telemetry_sample"), _printable_id))


#: Most calls one read of :func:`telemetry_for` looks an entry up for
#: (one entry read each), the same bound as the route-context reload.
MAX_SAMPLE_READS = 64


def telemetry_for(session_id: str, tool_use_ids) -> dict:
    """``{tool use id: (coverage lines, sample line)}`` for the given calls of
    the session whose entry holds either, at most :data:`MAX_SAMPLE_READS` of
    them read in the given order; ``{}`` when any read fails. The bounded
    lines the system runtime wrote under the call's capability
    (:func:`get_trace_telemetry`). All or nothing: the lines are collected
    first and returned only once every read has completed, so a read that
    fails after earlier hits returns ``{}`` and no call is shown a stored
    line. Each call's ``query_ref`` is minted from ``(session_id,
    tool_use_id)`` as the orchestrator minted it for the call. Never raises."""
    from agents.provenance import mint_query_ref
    try:
        found: dict = {}
        for tool_use_id in list(dict.fromkeys(tool_use_ids or ()))[:MAX_SAMPLE_READS]:
            if not (isinstance(tool_use_id, str) and tool_use_id):
                continue
            lines, line = get_trace_telemetry(session_id, mint_query_ref(session_id, tool_use_id))
            if lines or line:
                found[tool_use_id] = (lines, line)
    except Exception:  # noqa: BLE001 - a trace without the lines is the fallback
        logger.debug("investigation_trace_store: trace telemetry read skipped sid_sha=%s",
                     digest12(str(session_id)))
        return {}
    return found


def sample_lines_for(session_id: str, tool_use_ids) -> dict:
    """``{tool use id: stored sample line}`` for the given calls whose entry
    holds one (:func:`telemetry_for`, all or nothing). Never raises."""
    return {tool_use_id: line for tool_use_id, (_lines, line)
            in telemetry_for(session_id, tool_use_ids).items() if line}
