# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Remove model-authored internal counters from the public answer stream.

The orchestrator still emits the bounded candidate-pair observation for post-turn
telemetry.  This module keeps that record out of assistant prose even when its
literal is split across streamed text chunks, and when the model wraps the line in
one Markdown emphasis marker (``CANDIDATE_PAIR_WRAPPERS``).  Only the integer is
retained; answer text is never logged or copied into metadata.

The same line filter drops an engine line meant for the orchestrator only
(``internal_lines.MARKERS``) when the orchestrator copies one into its answer: a
line that starts with a marker is held until it ends and then left out. One
longer than :data:`_MAX_ORCHESTRATOR_LINE_CHARS` stops being held and is
dropped to its line break (nothing of it is kept or shown), so memory stays
bounded and no part of the line is shown.
"""
from __future__ import annotations

import re

from .internal_lines import MARKERS as _ORCHESTRATOR_LINE_MARKERS
from .internal_lines import is_marker_line as _is_orchestrator_line

_CANDIDATE_PAIR_MARKER = "additional_candidate_pairs_observed:"
#: The Markdown wrappers the model has put around the record line (a saved Daily
#: Briefing, 2026-10-17: ``*additional_candidate_pairs_observed: 0*``). Exactly one
#: matching wrapper, or none, is the record; the web parser
#: (frontend/src/shared/thread/answer-blocks.tsx CANDIDATE_COUNTER_WRAPPERS) uses the
#: same list, pinned by tests/unit/test_internal_telemetry_redaction.py. Longest first.
CANDIDATE_PAIR_WRAPPERS = ("**", "__", "*", "_")
_WRAPPER_ALTERNATION = "|".join(re.escape(w) for w in CANDIDATE_PAIR_WRAPPERS)
_CANDIDATE_PAIR_RECORD = re.compile(
    rf"^[ \t]*({_WRAPPER_ALTERNATION}|){re.escape(_CANDIDATE_PAIR_MARKER)}"
    rf"[ \t]*(\d+)[ \t]*\1[ \t]*\r?$",
    re.IGNORECASE,
)
_MAX_CANDIDATE_RECORD_CHARS = 160
#: Longest orchestrator-only engine line the stream holds; past it the rest of
#: the line is dropped unheld.
_MAX_ORCHESTRATOR_LINE_CHARS = 4000
#: First characters that can open a record line: indentation, a wrapper, the
#: marker, and the bracket that opens an engine line.
_RECORD_STARTS = frozenset(" \t[" + "".join(w[0] for w in CANDIDATE_PAIR_WRAPPERS))
_INDENT = re.compile(r"[ \t]*")


def _orchestrator_line_possible(candidate: str) -> bool:
    """Is ``candidate`` (a line so far) an orchestrator-only engine line, or
    still a prefix of one, within :data:`_MAX_ORCHESTRATOR_LINE_CHARS`?"""
    if len(candidate) > _MAX_ORCHESTRATOR_LINE_CHARS:
        return False
    start = _INDENT.match(candidate).end()
    rest = len(candidate) - start
    return any(candidate.startswith(marker, start)
               or (rest <= len(marker) and marker.startswith(candidate[start:]))
               for marker in _ORCHESTRATOR_LINE_MARKERS)


def _record_suffix_possible(suffix: str, wrapper: str) -> bool:
    """Can ``suffix`` (the text after the marker) still grow into a record closed by
    ``wrapper``? Digits, then optional spaces, then the closing wrapper (or a prefix of
    it), then optional spaces and a carriage return."""
    m = re.fullmatch(r"[ \t]*(\d*)[ \t]*(.*)", suffix, re.S)
    digits, tail = m.group(1), m.group(2)
    if not digits:
        return tail == ""
    if len(tail) < len(wrapper):
        return wrapper.startswith(tail)
    if not tail.startswith(wrapper):
        return False
    return re.fullmatch(r"[ \t]*\r?", tail[len(wrapper):]) is not None


class InternalTelemetryFilter:
    """Stateful, bounded filter for one streamed assistant answer."""

    def __init__(self) -> None:
        self._candidate: str | None = None
        self._line_start = True
        self._skip_blank = False
        self._discarding = False
        self.candidate_pair_values: list[int] = []

    def _release_or_record(self, *, newline: bool, complete: bool = True) -> str:
        """Emit the held text, or keep it out: a candidate-pair record (its
        value kept), or a whole orchestrator-only engine line with its line
        break. ``complete`` is False when the held text stopped being a
        possible record before its line ended; it is then emitted."""
        candidate, self._candidate = self._candidate or "", None
        body = candidate[:-1] if newline else candidate
        match = _CANDIDATE_PAIR_RECORD.fullmatch(body)
        self._line_start = newline
        if match:
            self.candidate_pair_values.append(int(match.group(2)))
            return "\n" if newline else ""
        if complete and _is_orchestrator_line(body):
            # The blank line that separated it from what follows goes too.
            self._skip_blank = newline
            return ""
        return candidate

    def _candidate_is_possible(self) -> bool:
        """Is the held text still a prefix of a record line (bare or inside
        exactly one matching wrapper) or of an orchestrator-only engine line?"""
        candidate = self._candidate or ""
        return _counter_possible(candidate) or _orchestrator_line_possible(candidate)

    def feed(self, text: str) -> str:
        """Return the public portion of one chunk, retaining only a possible record."""
        output: list[str] = []
        for char in str(text or ""):
            if self._discarding:
                self._discard(char)
                continue
            if self._candidate is not None:
                self._candidate += char
                if char == "\n":
                    output.append(self._release_or_record(newline=True))
                elif not self._candidate_is_possible():
                    if self._starts_orchestrator_line():
                        self._candidate, self._discarding = None, True
                        continue
                    output.append(self._release_or_record(newline=False, complete=False))
                    self._line_start = False
                continue
            if self._line_start and (char in _RECORD_STARTS or char.lower() == "a"):
                self._skip_blank = False
                self._candidate = char
                continue
            if self._skip_blank:
                self._skip_blank = False
                if char == "\n":
                    continue
            output.append(char)
            self._line_start = char == "\n"
        return "".join(output)

    def _starts_orchestrator_line(self) -> bool:
        """Does the held text open with an orchestrator-only engine marker?
        Then a line past the hold bound is dropped to its end, never shown."""
        return _is_orchestrator_line(self._candidate or "")

    def _discard(self, char: str) -> None:
        """Drop one character of an orchestrator-only engine line that grew
        past the hold bound; its line break ends it like a held line's."""
        if char == "\n":
            self._discarding = False
            self._line_start = True
            self._skip_blank = True

    def flush(self) -> str:
        """Finish a text run, dropping a complete unterminated record only."""
        if self._discarding:
            self._discarding = False
            return ""
        if self._candidate is None:
            return ""
        return self._release_or_record(newline=False)


def _counter_possible(candidate: str) -> bool:
    """Is ``candidate`` still a prefix of a candidate-pair record line, bare
    or inside exactly one matching wrapper?"""
    if len(candidate) > _MAX_CANDIDATE_RECORD_CHARS:
        return False
    stripped = candidate.lstrip(" \t")
    for wrapper in CANDIDATE_PAIR_WRAPPERS + ("",):
        if len(stripped) <= len(wrapper):
            if wrapper.startswith(stripped):
                return True
            continue
        if not stripped.startswith(wrapper):
            continue
        after = stripped[len(wrapper):]
        lowered = after.lower()
        if len(lowered) <= len(_CANDIDATE_PAIR_MARKER):
            if _CANDIDATE_PAIR_MARKER.startswith(lowered):
                return True
            continue
        if lowered.startswith(_CANDIDATE_PAIR_MARKER) and _record_suffix_possible(
                after[len(_CANDIDATE_PAIR_MARKER):], wrapper):
            return True
    return False


def redact_internal_telemetry(text: str) -> tuple[str, list[int]]:
    """Redact complete records from a non-streamed text value."""
    filter_ = InternalTelemetryFilter()
    public = filter_.feed(text) + filter_.flush()
    return public, list(filter_.candidate_pair_values)


# Self-learning control blocks the orchestrator appends for the web UI's
# accept/reject cards (frontend shared/thread/answer-blocks.tsx strips the same
# shapes). The runtime parses and logs them post-turn, so removing them from a
# delivered answer loses nothing. Matched tolerantly (two or more dashes,
# optional spaces, hyphen or space between the words): the model writes
# ``--VALUE-MAPPING-PROPOSAL--`` and ``--- END-PROPOSAL ---`` often enough that
# an exact match let the block through, and on event 26 (2026-10-04, module 3)
# the closing sentinel arrived with no opener and rendered as a literal line.
# The opener also arrives with its dashes on the line above, as a horizontal
# rule (``---`` then ``CORRELATION-PROPOSAL`` on its own line; module 5 walk,
# 2026-10-27), so a marker alone on its line opens the block too, and the rule
# line before it goes with the block.
_PROPOSAL_NAME_SRC = r"(?:VALUE[- ]MAPPING|CORRELATION)[- ]PROPOSAL"
_PROPOSAL_OPEN_SRC = (
    r"(?:-{2,}[ \t]*" + _PROPOSAL_NAME_SRC + r"[ \t]*-{2,}"
    r"|(?:^|\n)(?:[ \t]*-{3,}[ \t]*\n)?[ \t]*" + _PROPOSAL_NAME_SRC + r"[ \t]*(?=\n|$))"
)
_PROPOSAL_END_SRC = r"-{2,}[ \t]*END[- ]PROPOSAL[ \t]*-{2,}"
_PROPOSAL_BLOCK = re.compile(_PROPOSAL_OPEN_SRC + r".*?" + _PROPOSAL_END_SRC, re.S)
_PROPOSAL_ORPHAN_END = re.compile(r"^[ \t]*" + _PROPOSAL_END_SRC + r"[ \t]*$\n?", re.M)
_PROPOSAL_OPENED = re.compile(_PROPOSAL_OPEN_SRC)
_SUGGESTION_LINE = re.compile(r"^[ \t]*([QA])\d+:[ \t]*(.+?)[ \t]*$", re.M)
# The model's "nothing here" placeholders (mirror of answer-blocks.tsx
# PLACEHOLDER_RE): never a suggestion.
_PLACEHOLDER = re.compile(r"^\s*(?:none|n/?a|nothing|null|-+|\.+)?\s*[.!]?\s*$",
                          re.I)


def _suggestion_section(block: str) -> str:
    """The FOLLOWUPS block's Q/A lines as plain markdown lists, or ''."""
    groups: dict[str, list[str]] = {"Q": [], "A": []}
    for kind, value in _SUGGESTION_LINE.findall(block):
        if not _PLACEHOLDER.fullmatch(value):
            groups[kind].append(value)
    parts = []
    for kind, label in (("Q", "Follow-up questions"), ("A", "Suggested actions")):
        if groups[kind]:
            parts.append(f"{label}:\n" + "\n".join(f"- {v}" for v in groups[kind]))
    return "\n\n".join(parts)


def external_answer_text(text: str) -> str:
    """The answer as a client WITHOUT the web UI's block parser should see it.

    For the async ask path (``/api/ask/result``), which the Quick query bridge
    relays verbatim to an external MCP client. Proposal blocks (complete, or
    opened and never closed) and the candidate-pair counter are removed; the
    ``---FOLLOWUPS---`` block becomes plain "Follow-up questions" / "Suggested
    actions" lists. Text with none of these comes back unchanged apart from
    trailing whitespace. Mirrors parseAnswerBlocks in
    frontend/src/shared/thread/answer-blocks.tsx.
    """
    from .eval_core import split_followups
    public, _ = redact_internal_telemetry(
        _PROPOSAL_ORPHAN_END.sub("", _PROPOSAL_BLOCK.sub("", text or "")))
    body, block = split_followups(public)
    opened = _PROPOSAL_OPENED.search(body)
    if opened:
        body = body[:opened.start()]
    body = body.rstrip()
    section = _suggestion_section(block) if block is not None else ""
    return f"{body}\n\n{section}" if section else body
