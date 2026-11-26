# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import boto3

from agents.config import _MEMORY_NAME_PREFIX, get_config
from agents.memory_labels import (
    _created_sort_key,
    _fallback_label,
    label_from_turns,
)
from agents.memory_turns import message_blocks

logger = logging.getLogger(__name__)

#: Memory ids confirmed, in this process, to be the app's own memory: status
#: ACTIVE and a name under the prefix the app's memory discovery binds
#: (``config._MEMORY_NAME_PREFIX``). Only on a confirmed memory may "Actor
#: <id> not found" mean an empty history (``list_session_page``); a wrong but
#: existing memory answers the same for every user. Successes only are kept.
_CONFIRMED_MEMORIES: set[str] = set()
_CONFIRMED_LOCK = threading.Lock()


def reset_confirmed_memories() -> None:
    """Forget every confirmed memory (test seam only)."""
    with _CONFIRMED_LOCK:
        _CONFIRMED_MEMORIES.clear()


class MemoryUnavailableError(Exception):
    """Raised when a memory read fails for any reason OTHER than memory being
    unconfigured (an API error, a permission denial, a transient outage).

    Project rule: errors must propagate, never swallow into empty results. The
    read path used to ``return []`` on any exception, so an API/permission
    failure rendered identically to a genuinely-empty memory ('No events yet').
    Callers (memory_routes) translate this into a 503 so the UI can show an
    explicit error state instead of a misleading empty state.
    """


class MemoryRecordNotFoundError(Exception):
    """Raised when a memory record targeted for deletion does not exist OR is
    not scoped to the requesting user's namespaces. Both cases surface
    identically (memory_routes → 404) so the API never leaks the existence of
    another actor's records."""


def _is_not_found(exc: Exception) -> bool:
    """True when a bedrock-agentcore exception is a ResourceNotFound — matched
    by botocore error code or by class name so stubbed clients in unit tests
    behave the same. Never raises."""
    try:
        resp = getattr(exc, "response", None)
        if isinstance(resp, dict):
            code = (resp.get("Error") or {}).get("Code") or ""
            if "ResourceNotFound" in code:
                return True
    except Exception:  # nosec B110 - classification only
        pass
    return "ResourceNotFound" in exc.__class__.__name__


def _is_actor_not_found(exc: Exception, actor_id: str) -> bool:
    """True when ``exc`` is the ResourceNotFound AgentCore returns for an actor
    with no events yet ("Actor <id> not found", measured on a deployed memory),
    as opposed to a missing memory. Never raises."""
    if not _is_not_found(exc):
        return False
    resp = getattr(exc, "response", None)
    message = ((resp.get("Error") or {}).get("Message") or "") if isinstance(resp, dict) else ""
    return message.strip() == f"Actor {actor_id} not found"


def _render_content_blocks(blocks: object) -> str:
    """Render a Strands-style content block list into human-readable text.

    Handles the three block shapes the agent loop emits:
      * ``{"text": "..."}``       -> the prose, concatenated verbatim.
      * ``{"toolUse": {"name"}}`` -> a concise ``Tool call: <name>`` label.
      * ``{"toolResult": {...}}`` -> a concise ``Tool result`` label.

    Without this, tool turns (which carry no ``.text``) rendered as the raw
    AgentCore envelope JSON in the Memory Events tab. Prose blocks are joined
    with no separator (preserving multi-part text like ``"Hello " + "world"``);
    tool labels are separated onto their own line so a mixed text+tool turn
    stays readable. Returns "" if nothing renders. Never raises.
    """
    if not isinstance(blocks, list):
        return ""
    out = ""

    def _append_label(label: str) -> None:
        nonlocal out
        out += ("\n" + label) if (out and not out.endswith("\n")) else label

    for b in blocks:
        if not isinstance(b, dict):
            continue
        text = b.get("text")
        if isinstance(text, str) and text:
            out += text
        elif "toolUse" in b:
            tu = b.get("toolUse")
            name = tu.get("name") if isinstance(tu, dict) else None
            _append_label(f"Tool call: {name}" if isinstance(name, str) and name else "Tool call")
        elif "toolResult" in b:
            _append_label("Tool result")
    return out


def _extract_event_text(raw: str) -> tuple[Optional[str], str]:
    """Unwrap a Strands message envelope that the SessionManager serialized into
    the event payload text.

    Returns (role_or_None, text). If ``raw`` is not JSON or holds no extractable
    message text, the original string is returned unchanged with role ``None`` so
    plain-text turns pass through untouched. Tool turns surface as concise
    ``Tool call: <name>`` / ``Tool result`` labels rather than raw JSON.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None, raw
    # {"message": {"role":..., "content": [...]}} -> unwrap the inner message
    msg = obj.get("message") if isinstance(obj, dict) else None
    inner = msg if isinstance(msg, dict) else obj
    role = inner.get("role") if isinstance(inner, dict) else None
    if isinstance(inner, dict):
        text = _render_content_blocks(inner.get("content"))
    elif isinstance(inner, list):  # bare list of content blocks
        text = _render_content_blocks(inner)
    else:
        text = ""
    if not text:
        return None, raw  # no extractable text -> keep original
    return (role if isinstance(role, str) else None), text


# ── AgentCore SUMMARY / FACT / PREFERENCE parsing ───────────────────────────
# AgentCore's managed strategies emit MODEL-GENERATED markup that is NOT
# guaranteed to be well-formed XML, so every parser below is regex-lenient and
# never raises — the worst case is that a record renders as cleaned plain text.

_GLOBAL_SUMMARY_RE = re.compile(r"<global_summary[^>]*>(.*?)</global_summary>", re.IGNORECASE | re.DOTALL)
_TOPIC_RE = re.compile(r"<topic\b([^>]*)>(.*?)</topic>", re.IGNORECASE | re.DOTALL)
_NAME_ATTR_RE = re.compile(r"""\bname\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# A leaf element wrapping a short scalar value, e.g. ``<WO_number>1234</WO_number>``.
_LEAF_TAG_RE = re.compile(r"<([a-zA-Z][\w.-]*)\b[^>]*>\s*([^<>]{1,300}?)\s*</\1>")


def _clean_ws(text: str) -> str:
    """Collapse the ragged whitespace that tag removal leaves behind, without
    touching intentional line breaks. Never raises."""
    if not text:
        return ""
    out = re.sub(r"[ \t]+", " ", text)
    out = re.sub(r" *\n *", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _strip_all_tags(text: str) -> str:
    """Remove every ``<...>`` tag, keeping inner text, and normalise whitespace.
    Used as the graceful fallback for records with no recognised structure."""
    if not text:
        return ""
    try:
        return _clean_ws(_ANY_TAG_RE.sub(" ", text))
    except Exception:  # nosec B110 - display-only cleanup, never break a read
        return text


def _humanize_tag(name: str) -> str:
    """``WO_number`` -> ``WO number`` — a readable label for a leaf element."""
    return re.sub(r"[_\-.]+", " ", name).strip()


def _flatten_topic_body(body: str) -> str:
    """Flatten a topic body's model-generated nesting into readable lines.

    Topic bodies may contain further ``<subtopic name=...>`` / ``<subsubtopic>``
    sections, ``<content>`` / ``<item>`` / ``<timestamp>`` elements, and ad-hoc
    leaf elements (``<WO_number>``, etc.). This turns them into a clean,
    multi-line string: subtopic names become sub-headings, items become bullet
    lines, leaf elements become ``label: value`` lines, and every remaining tag
    is stripped. Never raises.
    """
    if not body:
        return ""
    try:
        s = body
        # (sub)topic headings: keep the name as a sub-heading line.
        s = re.sub(
            r"<sub(?:sub)?topic\b([^>]*)>",
            lambda m: (lambda nm: f"\n{nm.group(1).strip()}:\n" if nm else "\n")(_NAME_ATTR_RE.search(m.group(1) or "")),
            s, flags=re.IGNORECASE,
        )
        s = re.sub(r"</sub(?:sub)?topic\s*>", "\n", s, flags=re.IGNORECASE)
        # items -> bullet lines.
        s = re.sub(r"<item\b[^>]*>", "\n• ", s, flags=re.IGNORECASE)
        s = re.sub(r"</item\s*>", "", s, flags=re.IGNORECASE)
        # content blocks -> their own line; timestamps stay inline.
        s = re.sub(r"<content\b[^>]*>", "\n", s, flags=re.IGNORECASE)
        s = re.sub(r"</content\s*>", "", s, flags=re.IGNORECASE)
        s = re.sub(r"</?timestamp\b[^>]*>", " ", s, flags=re.IGNORECASE)
        # remaining leaf elements (e.g. <WO_number>1234</WO_number>) -> label: value.
        s = _LEAF_TAG_RE.sub(lambda m: f"\n{_humanize_tag(m.group(1))}: {m.group(2).strip()}", s)
        # strip any tags still standing (unclosed / unknown containers).
        s = _ANY_TAG_RE.sub(" ", s)
        return _clean_ws(s)
    except Exception:  # nosec B110 - display-only cleanup, never break a read
        return _strip_all_tags(body)


def _parse_summary_record(text: str) -> dict:
    """Parse an AgentCore SUMMARY record into ``{overview, topics:[{name, body}]}``.

    ``overview`` is the ``<global_summary>`` text (a concise session overview),
    and each ``<topic name=...>`` block becomes a structured topic with a
    flattened, readable ``body``. A record with no ``<global_summary>``/``<topic>``
    markup falls back to a single untitled topic holding the whole cleaned text,
    so the UI never sees raw tags and never renders empty. Never raises.
    """
    text = text or ""
    try:
        overview = ""
        gm = _GLOBAL_SUMMARY_RE.search(text)
        if gm:
            overview = _strip_all_tags(gm.group(1))
        topics = []
        for tm in _TOPIC_RE.finditer(text):
            attrs, body = tm.group(1), tm.group(2)
            nm = _NAME_ATTR_RE.search(attrs or "")
            name = nm.group(1).strip() if nm else ""
            topics.append({"name": name, "body": _flatten_topic_body(body)})
        if not topics and not overview:
            cleaned = _strip_all_tags(text)
            topics = [{"name": "", "body": cleaned}] if cleaned else []
            return {"overview": "", "topics": topics}
        return {"overview": overview, "topics": topics}
    except Exception:  # nosec B110 - display-only parse, never break a read
        cleaned = _strip_all_tags(text)
        return {"overview": "", "topics": ([{"name": "", "body": cleaned}] if cleaned else [])}


def _parse_preference_record(raw: str) -> dict:
    """Parse a userPreference record into ``{preference, context, categories[]}``.

    The record's ``content.text`` is JSON, e.g.::

        {"context": "The user explicitly stated...",
         "preference": "Prefers metric units for measurements",
         "categories": ["work", "units"]}

    Categories are surfaced so the UI can render them as chips. If the value is
    not the expected JSON shape, the cleaned text becomes the preference with no
    context/categories. Never raises.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("preference"), str) and obj["preference"]:
        cats = obj.get("categories")
        categories = [c for c in cats if isinstance(c, str) and c] if isinstance(cats, list) else []
        context = obj.get("context")
        return {
            "preference": obj["preference"].strip(),
            "context": context.strip() if isinstance(context, str) else "",
            "categories": categories,
        }
    return {"preference": _strip_all_tags(raw), "context": "", "categories": []}


def _parse_fact_text(raw: str) -> str:
    """Return a semantic FACT record as clean text.

    Keeps the existing JSON-envelope unwrapping (``{"text": "..."}``) and strips
    any stray markup tags. Never raises.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        text = obj.get("text")
        if isinstance(text, str) and text:
            return _strip_all_tags(text)
        pref = obj.get("preference")
        if isinstance(pref, str) and pref:
            return _strip_all_tags(pref)
    return _strip_all_tags(raw)


@dataclass
class MemoryEvent:
    event_id: str
    role: str
    content: str
    timestamp: str
    # The stored message's ordered text / tool_use / tool_result blocks
    # (agents.memory_turns.message_blocks); None for a plain-text turn.
    blocks: Optional[list] = None


@dataclass
class SemanticFact:
    fact_id: str
    content: str
    confidence: Optional[float]
    timestamp: str
    # Structured, type-specific payload for the UI. ``None`` for plain facts;
    # for summaries a ``{overview, topics:[{name, body}]}`` object; for
    # preferences a ``{preference, context, categories[]}`` object.
    structured: Optional[dict] = None


@dataclass
class SessionSummary:
    session_id: str
    label: str
    created_at: str


# ── Session-list policy (user-facing picker) ────────────────────────────────
# Session-id prefixes written by AUTOMATED lanes (smoke_prompts, eval_answers,
# fx eval variants, diagnostics, try-it probes, warmup, briefings, the
# scheduler, feedback exploration, and ad-hoc validate/repro/raw/adhoc
# captures) that authenticate as the SAME Cognito user as the interactive UI
# and would otherwise pollute the session picker. Matching is ANCHORED to the
# START of the session id (see ``_is_automated_session``) so a user's real
# ``session-<epoch>-<rand>`` chat can never be excluded by an accidental
# substring hit. This filter applies to the user-facing LISTING only —
# explicitly rehydrating a session id by hand is unaffected.
# CROSS-PINNED against the actual minting sites in
# tests/unit/test_memory_session_list.py: every prefix in
# ``runtime_app._SYNTHETIC_SESSION_PREFIXES`` (a SEPARATE policy — the
# no-learning-writes gate), ``feedback_exploration.SESSION_PREFIX``, and the
# ``scripts/smoke_prompts.create_session_namespace`` smoke/eval namespaces
# must be excluded HERE; extending any of those without covering it in this
# tuple fails that lane.
AUTOMATED_SESSION_PREFIXES: tuple[str, ...] = (
    "smoke",
    "eval",
    # Server-side golden-eval runs (runtime_app._SYNTHETIC_SESSION_PREFIXES).
    # Anchored matching means "eval" already covers this — pinned EXPLICITLY
    # so narrowing "eval" can never silently re-admit eval-run traffic.
    "eval-run-",
    "fx-eval",
    "diag",
    "tryit",
    "warmup",
    "briefing",
    "scheduler",
    "feedback-explore",
    "validate",
    "repro",
    "raw",
    "adhoc",
)

# Pagination bounds for AgentCore ``list_sessions``: per-call page size, a hard
# page-count cap, and a hard total-collection cap so a huge actor can never
# hang the request or blow memory (worst case 20 pages / 1000 summaries).
_SESSION_PAGE_SIZE = 100
_SESSION_MAX_PAGES = 20
_SESSION_MAX_TOTAL = 1000

# Event existence is now checked SERVER-SIDE by ``_collect_session_pages``
# via ``filter={"eventFilter": "HAS_EVENTS"}`` — no client-side probe is
# needed to find out whether a session's events aged out. The remaining
# per-candidate call is labeling only, bounded to exactly the display page
# (``max_results``), never an over-fetch multiplier.

# Labels within one page are fetched CONCURRENTLY (bounded thread pool, one
# shared boto3 client — clients are thread-safe; creating them is not).
# Sequential fetching measured a worst case of ~20 serial list_events calls
# per GET /api/memory/sessions; batching collapses the latency to
# ~ceil(page/_PROBE_MAX_WORKERS) round-trips at the same call count.
_PROBE_MAX_WORKERS = 8

# The label read pages exactly like the reload (``_EVENTS_PAGE_SIZE`` per call),
# so no label call asks for more events than this.
_LABEL_SCAN_EVENTS = 100

# ``get_events`` (the Recent sessions reload) reads up to this many events,
# in pages of the ListEvents ceiling (100). A turn writes two events per tool
# call, so one investigation can pass 50 events; a single page of 50 cut off
# the oldest events, which is where the turn's question is.
_EVENTS_PAGE_SIZE = 100
_EVENTS_MAX_PAGES = 4
_EVENTS_MAX_TOTAL = _EVENTS_PAGE_SIZE * _EVENTS_MAX_PAGES

# Label DERIVATION (tool-turn skipping, ROUTE SEED / bracketed-preamble
# stripping, word-boundary trim, date fallback) lives in
# ``agents.memory_labels`` — display policy extracted from this transport
# module; import direction is one-way (this module imports from there).


def _is_automated_session(session_id: str) -> bool:
    """True for sessions written by automated lanes — matched ANCHORED to the
    start of the id so a real chat can never be excluded by a substring hit."""
    return session_id.startswith(AUTOMATED_SESSION_PREFIXES)


def _build_semantic_fact(memory_type: str, record: dict, raw_text: str) -> SemanticFact:
    """Map one raw ``list_memory_records`` entry to a display-ready
    ``SemanticFact`` for the given strategy, parsing type-specific structure:

      * ``summaries``   -> ``structured`` = ``{overview, topics[]}``; ``content``
        is a plain-text fallback (overview, else first topic body).
      * ``preferences`` -> ``structured`` = ``{preference, context, categories[]}``;
        ``content`` is the preference statement.
      * ``facts``       -> ``content`` is clean fact text; ``structured`` is None.

    Never raises — parsing helpers all degrade to cleaned text.
    """
    fact_id = record.get("memoryRecordId", "")
    timestamp = str(record.get("createdAt", ""))
    if memory_type == "summaries":
        parsed = _parse_summary_record(raw_text)
        fallback = parsed.get("overview") or (
            parsed["topics"][0]["body"] if parsed.get("topics") else ""
        )
        return SemanticFact(fact_id=fact_id, content=fallback, confidence=None,
                            timestamp=timestamp, structured=parsed)
    if memory_type == "preferences":
        parsed = _parse_preference_record(raw_text)
        return SemanticFact(fact_id=fact_id, content=parsed["preference"], confidence=None,
                            timestamp=timestamp, structured=parsed)
    return SemanticFact(fact_id=fact_id, content=_parse_fact_text(raw_text), confidence=None,
                        timestamp=timestamp, structured=None)


def _conv_from_blob(blob: object) -> dict:
    """Decode a BLOB event payload into the conversational-envelope shape.

    AgentCore Memory stores oversized turns as ``{"blob": <json string>}``
    instead of ``{"conversational": ...}``: the string decodes to a LIST of
    JSON-serialized Strands message envelopes
    (``{"message": {"role": ..., "content": [...]}, ...}``). Returns a
    ``{"role", "content"}`` dict matching the conversational shape for the
    FIRST envelope carrying a message, or ``{}`` when the blob is absent or
    undecodable (never raises — the caller degrades to a blank event exactly
    as before)."""
    if not blob:
        return {}
    try:
        outer = json.loads(blob) if isinstance(blob, str) else blob
        if isinstance(outer, dict):
            outer = [outer]
        if not isinstance(outer, list):
            return {}
        for entry in outer:
            inner = json.loads(entry) if isinstance(entry, str) else entry
            if not isinstance(inner, dict):
                continue
            message = inner.get("message")
            if isinstance(message, dict) and message.get("role"):
                # The downstream unwrapper (_extract_event_text) expects the
                # JSON-envelope TEXT form, so re-serialize just the envelope.
                return {"role": str(message["role"]).upper(),
                        "content": {"text": json.dumps({"message": message})}}
        return {}
    except Exception:  # noqa: BLE001 - parsing is best-effort, never fatal
        return {}


#: Reserved metadata key bedrock-agentcore's Strands session manager (1.3+)
#: puts on its own session/agent STATE events, which it stores in the user's
#: session stream next to the turns.
_SDK_STATE_TYPE_KEY = "stateType"

#: ListEvents ``filter`` that asks the service for conversation events only,
#: so the SDK's state events never use up a bounded read (the reload pages,
#: the label scan). Same request shape as the SDK's own EventMetadataFilter
#: (bedrock_agentcore.memory.models.filters; NOT_EXISTS takes no right side).
_TURN_EVENTS_FILTER = {"eventMetadata": [
    {"left": {"metadataKey": _SDK_STATE_TYPE_KEY}, "operator": "NOT_EXISTS"}]}


def _turn_events(events: list) -> list:
    """``events`` without the SDK's state events: they are bookkeeping blobs,
    not turns, and would parse as blank events with no role. A defence behind
    ``_TURN_EVENTS_FILTER``, which already keeps them out server-side."""
    return [e for e in events
            if not (isinstance(e, dict) and _SDK_STATE_TYPE_KEY in (e.get("metadata") or {}))]


def _parse_conversational_event(e: object) -> MemoryEvent:
    """Turn one raw AgentCore ``list_events`` entry into a display-ready
    ``MemoryEvent`` (human-readable role + text). Kept module-level so both
    ``get_events`` and the session-label lookup share one parser. Never raises.
    """
    if not isinstance(e, dict):
        e = {}
    # list_events returns payload as a LIST of entries, e.g.
    #   [{"conversational": {"role": "USER", "content": {"text": ...}}}]
    # (a dict form is handled defensively in case the API shape varies).
    payload = e.get("payload") or []
    conv = {}
    if isinstance(payload, list) and payload:
        conv = (payload[0] or {}).get("conversational", {}) or {}
        if not conv:
            # Oversized turns (measured live 2026-08-06: entries over ~9KB —
            # notably the composed route-seed user turn) arrive as a BLOB
            # payload instead of a conversational one: a JSON string holding a
            # list of JSON-serialized message envelopes. Read as a conversational
            # event they parse as blank, which reads as "the user turn was never
            # persisted" (it was — as a blob). Decode to the same envelope
            # shape the conversational path unwraps below.
            conv = _conv_from_blob((payload[0] or {}).get("blob"))
    elif isinstance(payload, dict):  # defensive
        conv = payload.get("conversational", {}) or {}
    # The API returns role as 'USER'/'ASSISTANT' but the frontend uses it
    # directly as a CSS class (.memory-role--user/--assistant), so normalize
    # to lowercase here to match.
    outer_role = conv.get("role") or ""
    # 'content' is normally a dict ({"text": ...}), but tolerate str and list
    # shapes so API drift can't raise and silently empty the Events tab.
    content_obj = conv.get("content")
    if isinstance(content_obj, dict):
        raw_content = content_obj.get("text", "") or ""
    elif isinstance(content_obj, str):
        raw_content = content_obj
    elif isinstance(content_obj, list):
        raw_content = "".join(b.get("text", "") for b in content_obj if isinstance(b, dict))
    else:
        raw_content = ""
    # Assistant/user turns written by the Strands AgentCoreMemorySessionManager
    # arrive as a JSON-serialized message envelope
    # (e.g. {"message": {"role":.., "content":[{"text":..}]}}). Unwrap to the
    # human-readable text; plain-text turns pass through; tool turns become
    # concise labels.
    env_role, content = _extract_event_text(raw_content)
    role = (env_role or outer_role).lower()
    return MemoryEvent(
        event_id=e.get("eventId", ""),
        role=role,
        content=content,
        timestamp=_event_time_text(_event_time(e)),
        blocks=message_blocks(raw_content),
    )


def _event_time(e: object) -> object:
    """The event's time as ListEvents returns it: ``eventTimestamp`` (a
    datetime from boto3). ``createdAt`` is not an event field; it is read
    only as a fallback for older fixtures."""
    if not isinstance(e, dict):
        return None
    value = e.get("eventTimestamp")
    return value if value is not None else e.get("createdAt")


def _event_time_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class MemoryClient:
    def __init__(self, memory_id: str = None, region: str = None):
        cfg = get_config()
        self._memory_id = memory_id or cfg.memory_id
        self._region = region or cfg.aws_region

    def _client(self):
        return boto3.client("bedrock-agentcore", region_name=self._region)

    def _control_client(self):
        return boto3.client("bedrock-agentcore-control", region_name=self._region)

    def confirm_app_memory(self) -> None:
        """Confirm the configured memory is the app's own: ONE ``GetMemory``
        per process (cached on success; a failure is not cached), requiring
        status ``ACTIVE`` and a name under the prefix the app's memory
        discovery binds. Until this passes, ``list_session_page`` reads
        "Actor <id> not found" as a failure, never an empty history. Raises
        ``MemoryUnavailableError`` when memory is unconfigured, the call
        fails, or the memory is not the app's."""
        if not get_config().has_memory:
            raise MemoryUnavailableError("memory is not configured")
        with _CONFIRMED_LOCK:
            if self._memory_id in _CONFIRMED_MEMORIES:
                return
            try:
                resp = self._control_client().get_memory(memoryId=self._memory_id)
            except Exception as exc:
                logger.error(  # deliberate: logged for ops visibility then re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                    "get_memory failed (memory_id=%s): %s", self._memory_id, exc)
                raise MemoryUnavailableError(
                    f"failed to read memory_id={self._memory_id}") from exc
            memory = (resp or {}).get("memory") or {}
            name, status = str(memory.get("name") or ""), str(memory.get("status") or "")
            if status.upper() != "ACTIVE" or not name.startswith(_MEMORY_NAME_PREFIX):
                logger.error(
                    "memory_not_the_apps: memory_id=%s status=%s name_has_app_prefix=%s",
                    self._memory_id, status or "-", name.startswith(_MEMORY_NAME_PREFIX))
                raise MemoryUnavailableError(
                    f"memory_id={self._memory_id} is not the app's ACTIVE memory")
            _CONFIRMED_MEMORIES.add(self._memory_id)

    def get_events(self, session_id: str, user_id: str,
                   max_results: int = _EVENTS_MAX_TOTAL) -> list[MemoryEvent]:
        """The session's events, OLDEST FIRST.

        ListEvents returns the newest event first (the SDK's own readers
        reverse it: ``AgentCoreMemoryConverter.events_to_messages``, and
        ``read_agent`` takes ``max_results=1`` as the latest state), so the
        pages are collected newest-first and then ordered by
        ``eventTimestamp``, which the Strands session manager writes strictly
        increasing. Passing the API order through put a reopened session's
        answer above its question. Reads at most ``_EVENTS_MAX_PAGES`` pages."""
        if not get_config().has_memory:
            return []
        return self.read_session_events(session_id, user_id, max_results)[0]

    def read_session_events(self, session_id: str, user_id: str,
                            max_results: int = _EVENTS_MAX_TOTAL,
                            client=None) -> tuple[list[MemoryEvent], bool]:
        """``get_events``' read, plus whether events were left unread.

        The pages arrive newest first, so a cut drops the OLDEST events (the
        session's first turns); the flag lets a caller say so instead of
        showing a shorter session as if it were whole. ``client`` is a shared
        boto3 client for concurrent callers (creating clients concurrently is
        not thread-safe). Raises ``MemoryUnavailableError`` when memory is
        unconfigured or the API fails."""
        if not get_config().has_memory:
            raise MemoryUnavailableError("memory is not configured")
        try:
            client = client or self._client()
            raw: list = []
            token = None
            for _ in range(_EVENTS_MAX_PAGES):
                kwargs = {
                    "memoryId": self._memory_id,
                    "actorId": user_id,
                    "sessionId": session_id,
                    "maxResults": min(_EVENTS_PAGE_SIZE, max_results - len(raw)),
                    "includePayloads": True,
                    "filter": _TURN_EVENTS_FILTER,
                }
                if token:
                    kwargs["nextToken"] = token
                resp = client.list_events(**kwargs)
                raw.extend(resp.get("events", []) or [])
                token = resp.get("nextToken")
                if not token or len(raw) >= max_results:
                    break
            raw = _turn_events(raw[:max_results])
            # Stable sort: events without a readable time keep the API order.
            raw.sort(key=lambda e: _created_sort_key(_event_time(e)))
            return [_parse_conversational_event(e) for e in raw], bool(token)
        except Exception as exc:
            logger.error(  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                "get_events failed (memory_id=%s, session_id=%s, user_id=%s): %s",
                self._memory_id, session_id, user_id, exc, exc_info=True,
            )
            raise MemoryUnavailableError(
                f"failed to list events for memory_id={self._memory_id} "
                f"session_id={session_id} user_id={user_id}"
            ) from exc

    def _label_for(self, client, session_id: str, user_id: str) -> Optional[str]:
        """The session's first genuine user question, as its label.

        Reads the session the same way the reload does
        (``read_session_events``): pages of ``_EVENTS_PAGE_SIZE`` chained by
        ``nextToken``, at most ``_EVENTS_MAX_PAGES`` pages, the conversation
        filter, then oldest first. The service applies ``maxResults`` before
        the stateType filter, so one page of the newest raw events often
        holds only the last turn or two; a single call labelled sessions with
        their latest question. A session longer than the page bound is
        labelled from its oldest readable turn.

        Cost: labels are fetched concurrently for the displayed page only, and
        a typical session needs one page. Existence is not this call's job:
        ``_collect_session_pages`` already asked AgentCore Memory for
        ``HAS_EVENTS`` sessions only. Any failure returns ``None`` (fail
        open: the caller falls back to a readable label and never hides the
        session). ``client`` is the SHARED boto3 client (boto3 clients are
        thread-safe, creating them concurrently is not)."""
        try:
            events, _cut = self.read_session_events(session_id, user_id, client=client)
        except Exception:  # noqa: BLE001 - labeling is best-effort
            return None
        if not events:
            return None
        return label_from_turns(events)

    def _collect_session_pages(self, user_id: str) -> list[dict]:
        """Collect the actor's session summaries across ALL API pages, bounded
        by ``_SESSION_MAX_PAGES`` / ``_SESSION_MAX_TOTAL`` so a huge actor can
        never hang the request or blow memory. The API does NOT return pages
        newest-first, so the FULL set must be collected before sorting —
        sorting a single page and truncating loses the user's recent chats.

        ``filter={"eventFilter": "HAS_EVENTS"}`` asks AgentCore Memory itself
        to exclude sessions with no retrievable events (aged out under the
        7-day event TTL), server-side, in the SAME paginated call. This
        replaces a prior version that reconstructed the same check with a
        client-side ``list_events`` probe per candidate — up to
        ``max_results * 3`` extra sequential/concurrent AWS calls to answer a
        question the API can already answer for free."""
        client = self._client()
        collected: list[dict] = []
        token = None
        for _ in range(_SESSION_MAX_PAGES):
            kwargs = {
                "memoryId": self._memory_id,
                "actorId": user_id,
                "maxResults": _SESSION_PAGE_SIZE,
                "filter": {"eventFilter": "HAS_EVENTS"},
            }
            if token:
                kwargs["nextToken"] = token
            resp = client.list_sessions(**kwargs)
            collected.extend(resp.get("sessionSummaries", []) or [])
            token = resp.get("nextToken")
            if not token or len(collected) >= _SESSION_MAX_TOTAL:
                break
        return collected[:_SESSION_MAX_TOTAL]

    def list_session_page(self, user_id: str, max_results: int, token: Optional[str] = None,
                          client=None) -> tuple[list[dict], Optional[str]]:
        """ONE ``ListSessions`` page of the actor's sessions that still hold
        events (``HAS_EVENTS``, as ``_collect_session_pages``), from ``token``
        (None = the first page), and the token of the next page (None when
        this was the last). The API does not return sessions newest first, so
        a caller that orders sessions must read every page or say it did not.
        On a memory ``confirm_app_memory`` confirmed, "Actor <id> not found"
        (a user who has never chatted) is an empty history; on any other it is
        a failure, because a wrong but existing memory answers the same for
        every user. Raises ``MemoryUnavailableError`` when memory is
        unconfigured or the API fails."""
        if not get_config().has_memory:
            raise MemoryUnavailableError("memory is not configured")
        kwargs = {"memoryId": self._memory_id, "actorId": user_id, "maxResults": max_results,
                  "filter": {"eventFilter": "HAS_EVENTS"}}
        if token:
            kwargs["nextToken"] = token
        try:
            resp = (client or self._client()).list_sessions(**kwargs)
        except Exception as exc:
            if _is_actor_not_found(exc, user_id) and self._memory_id in _CONFIRMED_MEMORIES:
                # A pool user who has never chatted has no actor in the app's
                # memory yet: an empty history, not a failure. Any other
                # not-found (the memory itself, or an unconfirmed memory)
                # still raises below.
                return [], None
            logger.error(  # deliberate: logged for ops visibility then re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                "list_sessions failed (memory_id=%s, user_id=%s): %s",
                self._memory_id, user_id, exc)
            raise MemoryUnavailableError(
                f"failed to list sessions for memory_id={self._memory_id} "
                f"user_id={user_id}") from exc
        sessions = [s for s in resp.get("sessionSummaries", []) or [] if isinstance(s, dict)]
        next_token = resp.get("nextToken")
        return sessions[:max_results], next_token if isinstance(next_token, str) and next_token else None

    def _label_batch(self, client, batch: list[dict], user_id: str) -> list[Optional[str]]:
        """Fetch labels for one batch of already-live candidates CONCURRENTLY
        (bounded pool, shared client), returning labels in BATCH ORDER —
        ``ThreadPoolExecutor.map`` preserves input order, so newest-first
        candidate order survives the fan-out."""
        if not batch:
            return []
        if len(batch) == 1:  # no pool for the trivial case
            sid = batch[0].get("sessionId", "") or ""
            return [self._label_for(client, sid, user_id)]
        with ThreadPoolExecutor(max_workers=min(_PROBE_MAX_WORKERS, len(batch))) as pool:
            return list(pool.map(
                lambda s: self._label_for(
                    client, s.get("sessionId", "") or "", user_id),
                batch,
            ))

    def _select_live_sessions(self, candidates: list[dict], user_id: str,
                              max_results: int, with_labels: bool) -> list[SessionSummary]:
        """Assemble the display page from newest-first ``candidates`` — all
        already known-live (``_collect_session_pages`` filtered on
        ``HAS_EVENTS`` server-side). Truncate to ``max_results`` first, THEN
        fetch labels only for that page (bounded, issued CONCURRENTLY in one
        batch) — no probe-multiplier over-fetch is needed since there is no
        liveness check left to fail."""
        page = candidates[:max_results]
        if not with_labels:
            return [
                SessionSummary(
                    session_id=s.get("sessionId", "") or "",
                    label=s.get("sessionId", "") or "",
                    created_at=str(s["createdAt"]) if s.get("createdAt") is not None else "",
                )
                for s in page
            ]
        client = self._client()
        labels = self._label_batch(client, page, user_id)
        out: list[SessionSummary] = []
        for s, label in zip(page, labels):
            sid = s.get("sessionId", "") or ""
            created = s.get("createdAt")
            out.append(SessionSummary(
                session_id=sid,
                label=label or _fallback_label(created, sid),
                created_at=str(created) if created is not None else "",
            ))
        return out

    def list_sessions(self, user_id: str, max_results: int = 20, with_labels: bool = True) -> list[SessionSummary]:
        """List a user's (actor's) recent conversation sessions, newest first.

        AgentCore Memory enumerates sessions natively via ``list_sessions``
        (keyed on ``memoryId`` + ``actorId``, filtered server-side to
        ``HAS_EVENTS`` — see ``_collect_session_pages``) — but the API does
        NOT return pages newest-first, so this collects EVERY page (bounded),
        filters out automated-lane sessions (``AUTOMATED_SESSION_PREFIXES`` —
        user-facing listing only; explicit rehydration by id is unaffected),
        sorts the FULL set newest-first (missing/garbage ``createdAt`` sorts
        last and never crashes), and only THEN truncates to ``max_results`` —
        attaching a best-effort first-user-question label for the picker.
        """
        if not get_config().has_memory:
            return []
        try:
            raw = self._collect_session_pages(user_id)
        except Exception as exc:
            logger.error(  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                "list_sessions failed (memory_id=%s, user_id=%s): %s",
                self._memory_id, user_id, exc, exc_info=True,
            )
            raise MemoryUnavailableError(
                f"failed to list sessions for memory_id={self._memory_id} user_id={user_id}"
            ) from exc
        candidates = [
            s for s in raw
            if isinstance(s, dict) and s.get("sessionId")
            and not _is_automated_session(s["sessionId"])
        ]
        candidates.sort(key=lambda s: _created_sort_key(s.get("createdAt")), reverse=True)
        return self._select_live_sessions(candidates, user_id, max_results, with_labels)

    def get_semantic(self, session_id: str, user_id: str, memory_type: str = "facts", max_results: int = 50) -> list[SemanticFact]:
        if not get_config().has_memory:
            return []
        # These read namespaces MUST be byte-identical to the CfnMemory strategy
        # namespaceTemplates (leading AND trailing slash), with {actorId}=user_id
        # and {sessionId}=session_id substituted in. If they diverge, the read
        # never matches and no records are returned.
        #   facts:       /facts/{actorId}/
        #   summaries:   /summaries/{actorId}/{sessionId}/
        #   preferences: /preferences/{actorId}/
        namespace_map = {
            "facts": f"/facts/{user_id}/",
            "summaries": f"/summaries/{user_id}/{session_id}/",
            "preferences": f"/preferences/{user_id}/",
        }
        namespace = namespace_map.get(memory_type, f"/facts/{user_id}/")
        try:
            resp = self._client().list_memory_records(
                memoryId=self._memory_id,
                namespace=namespace,
                maxResults=max_results,
            )
            facts = []
            for r in resp.get("memoryRecordSummaries", []):
                raw_text = r.get("content", {}).get("text", "") or ""
                facts.append(_build_semantic_fact(memory_type, r, raw_text))
            facts.sort(key=lambda f: f.timestamp, reverse=True)
            return facts
        except Exception as exc:
            logger.error(  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                "get_semantic failed (memory_id=%s, namespace=%s, type=%s): %s",
                self._memory_id, namespace, memory_type, exc, exc_info=True,
            )
            raise MemoryUnavailableError(
                f"failed to list memory records for memory_id={self._memory_id} "
                f"namespace={namespace} type={memory_type}"
            ) from exc

    def _user_namespaces(self, user_id: str) -> tuple[str, ...]:
        """The namespace prefixes a user's semantic records may live under.
        MUST stay aligned with the ``get_semantic`` namespace_map (the CfnMemory
        strategy namespaceTemplates with {actorId}=user_id substituted)."""
        return (
            f"/facts/{user_id}/",
            f"/summaries/{user_id}/",
            f"/preferences/{user_id}/",
        )

    def delete_record(self, record_id: str, user_id: str) -> None:
        """Delete ONE semantic memory record (fact / summary / preference) —
        but only after proving, via ``get_memory_record``, that EVERY namespace
        the record lives in belongs to the requesting user (actor). A record
        outside the user's namespaces raises ``MemoryRecordNotFoundError``
        (indistinguishable from absent — no cross-actor existence leak) and the
        data-plane delete is never attempted.

        Raises ``MemoryUnavailableError`` when memory is unconfigured or the
        API fails; ``MemoryRecordNotFoundError`` when absent/foreign.
        """
        if not get_config().has_memory:
            raise MemoryUnavailableError("memory is not configured")
        client = self._client()
        try:
            resp = client.get_memory_record(
                memoryId=self._memory_id, memoryRecordId=record_id
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise MemoryRecordNotFoundError(record_id) from exc
            logger.error(  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                "delete_record lookup failed (memory_id=%s, record_id=%s): %s",
                self._memory_id, record_id, exc, exc_info=True,
            )
            raise MemoryUnavailableError(
                f"failed to look up memory record record_id={record_id}"
            ) from exc
        record = resp.get("memoryRecord") if isinstance(resp, dict) else None
        namespaces = (record or {}).get("namespaces") or []
        allowed = self._user_namespaces(user_id)
        if not namespaces or not all(
            isinstance(ns, str) and ns.startswith(allowed) for ns in namespaces
        ):
            # Foreign (another actor's) or unattributable record → same as absent.
            raise MemoryRecordNotFoundError(record_id)
        try:
            client.delete_memory_record(
                memoryId=self._memory_id, memoryRecordId=record_id
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise MemoryRecordNotFoundError(record_id) from exc
            logger.error(  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                "delete_record failed (memory_id=%s, record_id=%s): %s",
                self._memory_id, record_id, exc, exc_info=True,
            )
            raise MemoryUnavailableError(
                f"failed to delete memory record record_id={record_id}"
            ) from exc
