# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engine's own line that the shape guard refused a wrong-form lookup in a call.

Measured on a test event (2026-11-11): a system kept one concept under a
different identifier in each of two tables, and its PostgreSQL query tool's
shape guard (``identifier_shape_guard``) refused a lookup of one form in the
other form's column. The system's model then ended its answer with a typed
``not_connected`` decline about the concept (``delegation_decline``), and the
orchestrator's decline guard refused every later delegation about it that
turn, the right-form re-ask included. The decline's kind and concept are the
model's words: they cannot tell a decline that a wrong-form lookup explains
from one about records that need a source that is not connected. This line
cannot either, but it is evidence only the runtime writes:

    [engine] Wrong-form lookup refused: concept=<concept>[,<concept>...]

* :func:`line` reads the call's own record of the guard's refusals
  (``unresolved_miss.STATE_KEY``, kept by the capture hook from the query
  tool's own results) and names the concept of every value the guard refused
  as a wrong-form lookup in THIS call (the measured pair's concept) that the
  call did not resolve: a value a later row paired with exactly one
  counterpart, or whose counterpart the delegated question supplies
  (``unresolved_miss.resolved``), is left out, since a decline after it has
  another cause. A join refusal names no value and counts for nothing; a
  call with no record, a record the call could not read whole, or more than
  :data:`MAX_CONCEPTS` concepts gets no line. ``system_agent`` appends it
  right before the decline line.
* :func:`neutralize` makes every copy of the prefix in the model's answer
  inert (``system_agent._engine_line_copies_inert_once``), so only the
  engine's line carries it.
* :func:`concepts_before_decline` reads the concepts of the line only where
  the engine puts it: right before the result's last decline line. A copy
  anywhere else (an executed filter's literal quoted in another engine line,
  say) is not read.

The orchestrator stores the concepts the line names on the decline's record,
whatever concept the model named in its decline (measured 2026-11-12: the
guard refused under ``batch`` and the model declined about ``lot-number``),
and reopens the decline only for the other form, on the turn's IDENTIFIER
FORMS line for a refused concept, that a pairing system's own tool results
returned (``delegation_decline.brings_paired_form``,
``result_identifiers``); the line never unlatches a decline on its own. It is
for the orchestrator only (``internal_lines.MARKERS``). Generic: the concepts
are the measured pairs' own; no dataset literal. Pure.
"""
from __future__ import annotations

import re

#: Opening words of the line.
MARKER = "[engine] Wrong-form lookup refused:"
#: Most concepts one line names; a call with more gets no line.
MAX_CONCEPTS = 8
_CONCEPTS = r"[a-z0-9_.:-]+(?:,[a-z0-9_.:-]+)*"
#: The line, then the blank line the engine joins it to the decline line with,
#: as prose or inside a JSON answer's reserved member.
_BEFORE_DECLINE_RE = re.compile(re.escape(MARKER) + r" concept=(?P<concepts>" + _CONCEPTS
                                + r")(?:\n\n|\\n\\n)")


def _concepts(invocation_state, question: str) -> list:
    """The cleaned concepts of this call's wrong-form refusals that the call
    did not resolve (``unresolved_miss.resolved``), sorted; none for a record
    the call could not read whole."""
    from .delegation_decline import _clean
    from .unresolved_miss import STATE_KEY, resolved
    record = invocation_state.get(STATE_KEY) if isinstance(invocation_state, dict) else None
    pending = record.get("pending") if isinstance(record, dict) else None
    if not isinstance(pending, dict) or record.get("unread"):
        return []
    found = {_clean(entry.get("concept", "")) for entry in pending.values()
             if isinstance(entry, dict) and not resolved(entry, question)}
    return sorted(found - {""})


def line(invocation_state, question: str = "") -> str:
    """The line for this call's unresolved wrong-form refusals, ``""`` when
    it had none. ``question`` is the delegated question alone."""
    concepts = _concepts(invocation_state, question)
    if not concepts or len(concepts) > MAX_CONCEPTS:
        return ""
    return f"{MARKER} concept={','.join(concepts)}"


def neutralize(text: str) -> str:
    """``text`` with every copy of the prefix made inert."""
    if not isinstance(text, str) or MARKER not in text:
        return text
    return text.replace(MARKER, "Wrong-form lookup refused:")


def concepts_before_decline(text: str) -> frozenset:
    """The concepts the line names when it stands right before the last
    decline line of ``text`` (``delegation_decline.ENGINE_PREFIX``), else an
    empty set."""
    from .delegation_decline import ENGINE_PREFIX
    if not isinstance(text, str) or MARKER not in text:
        return frozenset()
    end = text.rfind(ENGINE_PREFIX)
    start = text.rfind(MARKER, 0, end) if end > 0 else -1
    match = _BEFORE_DECLINE_RE.fullmatch(text, start, end) if start >= 0 else None
    return frozenset(match.group("concepts").split(",")) if match else frozenset()
