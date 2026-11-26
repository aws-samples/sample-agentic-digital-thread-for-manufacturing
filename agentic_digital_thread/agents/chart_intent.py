# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Chart intent - computed from the USER's question, read by the delegation.

A SiteWise (Direct) history read draws a chart only when the user asked for
one. The signal must be the USER's own question, not the model-rewritten
delegated sub-question: the orchestrator decomposes a turn into intent-level
sub-questions, so the model can drop the word "chart" while rewriting an
explicit request, or add "trend" while decomposing one that asked for no
visual.

So the runtime's turn prelude computes one bounded, word-boundary bit from the
composed user turn and remembers it in a context variable
(:func:`turn_block`). The bit then travels to each system delegation in a
RESERVED, gateway-owned tool-call argument (:data:`RESERVED_CHART_ARG`), the
same ``_adt_identity`` / ``_adt_provenance`` discipline: the orchestrator's
gateway-client wrapper stamps it, the system-agent runtime pops it at the ASGI
seam into a context variable BEFORE any model sees the payload, and the
SiteWise history path reads that context variable
(:func:`system_agent._sitewise_runtime_wants_chart`). It is NOT a declared
tool parameter, so neither the orchestrator model nor the inner system model
can forge it, and it never touches ``kg_context`` - every delegation's
``kg_context`` is byte-identical to what it was before chart intent existed
(no marker, no reserve, no slicing). A false negative only means no chart,
never a wrong one, so the vocabulary is kept small on purpose.

Pure and stdlib-only.
"""
from __future__ import annotations

import contextvars
import logging
import re

logger = logging.getLogger(__name__)

#: Words in a question that ask for a chart or a trend. Single words match at
#: word boundaries; the phrases match as substrings (already multi-word).
#: ``trending`` is deliberately NOT here - "trending behind schedule" is
#: ordinary business wording, not a request to draw a trend.
_WORDS = frozenset({
    "chart", "charts", "plot", "plots", "plotted", "plotting",
    "graph", "graphs", "graphed",
    "trend", "trends", "history", "historical",
})
_PHRASES = ("over time", "time series")
_WORD_RE = re.compile(r"\b(?:" + "|".join(sorted(_WORDS)) + r")\b", re.IGNORECASE)

#: A question longer than this is not scanned (the bit stays False).
MAX_SCAN_CHARS = 20_000


def wants_chart(question) -> bool:
    """True when ``question`` asks for a chart or a trend. Pure: a bounded,
    word-boundary match over :data:`_WORDS` plus the multi-word phrases,
    case-insensitive. A non-string, empty or over-long question is False."""
    if not isinstance(question, str) or not question or len(question) > MAX_SCAN_CHARS:
        return False
    low = question.lower()
    if any(phrase in low for phrase in _PHRASES):
        return True
    return _WORD_RE.search(question) is not None


# --------------------------------------------------------------------------- #
# The per-turn hand-off: the runtime computes once, the delegations read it
# --------------------------------------------------------------------------- #
_TURN_INTENT: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "adt_turn_chart_intent", default=False)


def remember_turn_intent(wants: bool) -> None:
    """Record this turn's chart-intent bit."""
    _TURN_INTENT.set(bool(wants))


def turn_intent() -> bool:
    """This turn's chart-intent bit (``False`` when none was computed)."""
    return _TURN_INTENT.get()


def reset_turn_intent() -> None:
    """Clear the current turn's bit (start of every invocation)."""
    _TURN_INTENT.set(False)


def turn_block(prompt: str) -> bool:
    """Compute this turn's bit from the composed USER turn and remember it for
    the turn's delegations. Returns the bit. Fail-soft: any failure remembers
    and returns ``False`` (no chart, never a wrong one)."""
    try:
        from .memory_labels import strip_turn_preambles
    except Exception:  # noqa: BLE001 - fall back to the raw prompt
        strip_turn_preambles = None
    try:
        text = strip_turn_preambles(prompt) if strip_turn_preambles else prompt
        wants = wants_chart(text if isinstance(text, str) else prompt)
        remember_turn_intent(wants)
        return wants
    except Exception as exc:  # noqa: BLE001 - additive; never break a turn
        logger.warning("chart intent skipped (%s)", type(exc).__name__)
        remember_turn_intent(False)
        return False


def _is_query_tool(tool_name: object) -> bool:
    if not isinstance(tool_name, str):
        return False
    tool = tool_name.split("___", 1)[1] if "___" in tool_name else tool_name
    return tool.startswith("query_") or "_query_" in tool


# --------------------------------------------------------------------------- #
# The transport: a RESERVED tool-call argument, never ``kg_context``
# --------------------------------------------------------------------------- #
#: Reserved tool-call argument carrying this turn's chart-intent bit. The
#: orchestrator's gateway-client wrapper stamps it onto every system query's
#: ``arguments``; the system-agent runtime pops it UNCONDITIONALLY at the ASGI
#: seam into a context variable BEFORE any model sees the payload (the exact
#: ``_adt_identity`` / ``_adt_provenance`` posture). Underscore-prefixed and
#: NOT a declared tool parameter, so neither model can forge it. ONE
#: definition home; every consumer imports it.
RESERVED_CHART_ARG = "_adt_chart_intent"
_REQUESTED = "requested"
_NONE = "none"


def chart_intent_value(wants: bool) -> str:
    """The reserved-arg wire value: ``"requested"`` or ``"none"``. The marker
    ALWAYS states one, so the system agent can tell "the user did not ask"
    (``none``) from "no turn context at all" (the arg absent)."""
    return _REQUESTED if wants else _NONE


def parse_chart_intent(value: object):
    """``True``/``False`` from a reserved-arg value, or ``None`` when it is
    absent or malformed (a direct/background call with no turn context - the
    SiteWise path then falls back to the delegated question). Never raises."""
    if value == _REQUESTED:
        return True
    if value == _NONE:
        return False
    return None


def with_chart_intent_arg(tool_name: object, arguments, wants: bool):
    """``arguments`` with the reserved chart-intent arg added for a system
    query tool, else ``arguments`` unchanged. ``kg_context`` is NEVER touched.
    The caller's dict is never mutated."""
    if not isinstance(arguments, dict) or not _is_query_tool(tool_name):
        return arguments
    merged = dict(arguments)
    merged[RESERVED_CHART_ARG] = chart_intent_value(wants)
    return merged
