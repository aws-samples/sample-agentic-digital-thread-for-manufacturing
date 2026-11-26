# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Display-policy helpers for AgentCore Memory reads: recovering the genuine
question from a composed interactive user turn, deriving session-picker
labels, and the created-at parsing that labels and the session sort share.

Extracted from ``agents.memory_client`` (which keeps transport, pagination and
namespace policy) so each file stays within the project size cap. The import
direction is ONE-WAY: ``memory_client`` imports from here, never the reverse —
these helpers operate on already-parsed conversational turns (objects with
``.role`` / ``.content`` / ``.timestamp``), so they need nothing from the
client module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

# Turn prefixes that can never BE a label by themselves — tool-turn labels and
# the seeded/preamble compositions. Tool turns are skipped outright; the ROUTE
# SEED and bracketed preambles are STRIPPED to recover the question they carry
# (the persisted "user turn" of an interactive chat is the composed
# ``{seed}\n\n---\n\n[Temporal context: ...] {question}`` blob — see
# runtime_app._graph_first_prelude and server._prepare_question — so skipping
# those turns wholesale would discard the only place the question exists).
_LABEL_TOOL_PREFIXES = ("Tool call", "Tool result")
_ROUTE_SEED_PREFIX = "[ROUTE SEED"
#: The Mode-B graph-first seed (``orchestrator._GRAPH_FIRST_SEED_HEADER``'s
#: opening bracket): composed before the question like the route seed, and it
#: carries every routed system's schema, so it is never shown as the question.
#: Cross-pinned in tests/unit/test_ltm_search_query.py.
_GRAPH_FIRST_PREFIX = "[GRAPH-FIRST ROUTING"
#: ADR-0078: the seed-independent metric-layer preamble (route seed failed, or
#: local mode) — ``metric_turn_render.METRIC_LAYER_HEADER``'s opening bracket.
_METRIC_LAYER_PREFIX = "[METRIC LAYER"
#: The seed-independent resolved-time-windows preamble (route seed failed or
#: empty) — ``time_windows.HEADER``'s opening bracket.
_TIME_WINDOWS_PREFIX = "[RESOLVED TIME WINDOWS"
#: The orchestrator runtime's output-limit notice turn
#: (``output_cap_recovery.RECOVERY_NOTICE_PREFIX``): runtime-authored, never
#: something the user typed, so the display drops it like a seed-only turn.
_OUTPUT_LIMIT_NOTICE_PREFIX = "[OUTPUT LIMIT NOTICE]"
_ROUTE_SEED_SEPARATOR = "\n\n---\n\n"

# The bracketed preambles ``server._prepare_question`` composes ahead of the
# question, and the peel anchors derived from its EXACT output shapes:
#   * "[Temporal context: ...] "  — body fully server-rendered
#     (agents.time_context), never contains ']', so the first ']' closes it;
#   * "[Plant context: {plant} — ... search other plants.] " — ends with a
#     FIXED sentence, so the peel anchors THERE and an unvalidated plant name
#     containing ']' can never truncate the peel mid-bracket. Cross-pinned
#     against the real ``_prepare_question`` in
#     tests/unit/test_memory_session_list.py.
_TEMPORAL_PREAMBLE_PREFIX = "[Temporal context:"
_PLANT_PREAMBLE_PREFIX = "[Plant context:"
_PLANT_PREAMBLE_TERMINATOR = "search other plants.]"
# The server composes AT MOST two bracketed preambles (temporal, then plant) —
# the peel is bounded to that shape so bracketed material belonging to the
# question itself is never eaten by an unbounded loop.
_MAX_BRACKET_PREAMBLES = 2


def _parse_created_at(created: object) -> Optional[datetime]:
    """Parse a ``createdAt`` value as the API actually returns it (a datetime
    from boto3, an ISO-ish string, or missing/garbage) into a datetime.
    Returns ``None`` instead of raising."""
    if isinstance(created, datetime):
        return created
    if isinstance(created, str) and created:
        try:
            return datetime.fromisoformat(created.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _created_sort_key(created: object) -> float:
    """Epoch seconds for newest-first sorting. A missing or unparseable
    timestamp sorts to the BOTTOM (``-inf``) — it never crashes the endpoint
    and never silently floats above real timestamps."""
    dt = _parse_created_at(created)
    if dt is None:
        return float("-inf")
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return float("-inf")


def _trim_label(text: str, limit: int = 80) -> str:
    """Trim to ``limit`` characters, on a word boundary where practical (only
    when a space exists in the back half — never degrade to a stub)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut + "…"


def _peel_one_preamble(text: str) -> Optional[str]:
    """Peel ONE leading server-composed bracketed preamble; ``None`` when the
    text does not start with one. The plant bracket anchors on its FIXED
    closing sentence (falling back to the first ']' only for a non-canonical
    bracket); an UNTERMINATED bracket is treated as genuine user text — the
    server never composes one — and is never peeled or dropped."""
    if text.startswith(_PLANT_PREAMBLE_PREFIX):
        idx = text.find(_PLANT_PREAMBLE_TERMINATOR)
        end = idx + len(_PLANT_PREAMBLE_TERMINATOR) - 1 if idx >= 0 else text.find("]")
    elif text.startswith(_TEMPORAL_PREAMBLE_PREFIX):
        end = text.find("]")
    else:
        return None
    if end < 0:
        return None  # unterminated: not server-composed — keep the text whole
    return text[end + 1:].lstrip()


def strip_turn_preambles(content: str) -> str:
    """Recover the genuine question from a composed user turn.

    The persisted interactive user turn is
    ``{route seed}\\n\\n---\\n\\n[Temporal context: ...] [Plant context: ...] {question}``
    (runtime_app._graph_first_prelude + server._prepare_question); in Mode B
    the seed is the graph-first seed. Strip the seed at its documented
    separator, then peel the leading bracketed
    preambles — ANCHORED to the exact server-composed shapes and bounded to
    ``_MAX_BRACKET_PREAMBLES`` — so bracketed text belonging to the question
    itself survives. Returns '' for a seed-only turn (caller falls back) and
    the original text unchanged when no preamble applies.

    Shared by the session-label derivation AND the ``/api/memory/events``
    display path (rehydrated history must show the question the user typed,
    never the seed blob). Display-only: stored memory stays raw. The
    runtime's output-limit notice turn holds no question and returns ''.
    The separator is located with ``str.find`` and only the text after it is
    copied, so a seed of any size costs no copy of its own.
    The two steps are :func:`turn_after_seed` and
    :func:`peel_question_preambles`."""
    return peel_question_preambles(turn_after_seed(content))


def turn_after_seed(content: str) -> str:
    """The first step of :func:`strip_turn_preambles`: the composed turn
    without its server seed (route seed, graph-first seed, metric-layer or
    resolved-time-windows notice) and the separator after it, left-stripped.
    The text unchanged when it carries no seed; '' for a seed-only turn or the
    runtime's output-limit notice. The separator is located with ``str.find``
    and only the text after it is copied."""
    text = content
    if text.startswith(_OUTPUT_LIMIT_NOTICE_PREFIX):
        return ""
    if text.startswith((_ROUTE_SEED_PREFIX, _GRAPH_FIRST_PREFIX, _METRIC_LAYER_PREFIX,
                        _TIME_WINDOWS_PREFIX)):
        cut = text.find(_ROUTE_SEED_SEPARATOR)
        if cut < 0:
            return ""  # seed-only turn (no separator): nothing usable
        text = text[cut + len(_ROUTE_SEED_SEPARATOR):].lstrip()
    return text


def peel_question_preambles(text: str) -> str:
    """The second step of :func:`strip_turn_preambles`: peel the leading
    server-composed bracketed preambles, at most ``_MAX_BRACKET_PREAMBLES``."""
    for _ in range(_MAX_BRACKET_PREAMBLES):
        peeled = _peel_one_preamble(text)
        if peeled is None:
            break
        text = peeled
    return text


def label_from_turns(turns: list) -> Optional[str]:
    """Derive a session label from the FIRST genuine user question among
    already-parsed conversational turns — skipping tool turns, and STRIPPING
    the ROUTE SEED / bracketed preambles that the composed interactive turn
    wraps around the question — or ``None`` when no usable question exists.
    Never raises (labeling is best-effort)."""
    try:
        user_turns = [
            t for t in turns
            if t.role == "user" and t.content
            and not t.content.startswith(_LABEL_TOOL_PREFIXES)
        ]
        user_turns.sort(key=lambda t: t.timestamp)
        for turn in user_turns:
            question = strip_turn_preambles(turn.content).strip()
            if question:
                return _trim_label(question.replace("\n", " "))
        return None
    except Exception:  # noqa: BLE001 - labeling is best-effort, never fatal
        return None


def _fallback_label(created: object, session_id: str) -> str:
    """Readable fallback when a session has no usable question: its formatted
    date/time (the raw session id is meaningless to a human; the id still
    travels in the payload for the client). Last resort: the session id."""
    dt = _parse_created_at(created)
    if dt is not None:
        try:
            return "Chat from " + dt.strftime("%b %d, %Y %H:%M")
        except ValueError:  # pragma: no cover - strftime edge (year < 1000 etc.)
            pass
    return session_id
