# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Where the chat client links a citation marker in an answer.

The chat renders a final answer with ``buildAnswerModel``
(``frontend/src/chat/answer-sections.ts``): it splits the answer at its chart
tags (``chat/charts.ts`` ``splitChartBlocks``) and turns markers into Sources
chips in each markdown part (``chat/citations.ts`` ``injectCitationLinks``).
This module is the server's copy of that grammar (``citation_ordinals``
finds the citation markers of earlier answers by it). The rules, on both
sides:

* A marker group is ``[`` one or more members separated by commas (spaces
  around a comma allowed) ``]``, not followed by ``(`` (a markdown link). A
  member is a number (``1``) or an earlier label (``E1``, one or two digits).
  ``[1]``, ``[E2]``, ``[1, 2]``, ``[1,2]``, ``[E1, E2]`` and ``[1, E1]`` are
  groups; ``[1-3]``, ``[1; 2]`` and ``[E123]`` are not.
* Groups written side by side (``[1][2]``) form a run, and two shapes are
  never a citation, whatever the members: a run right after a letter, a
  digit, ``_``, ``]`` or ``)`` (an index such as ``sensor[1]``, ``a[1, 2]``
  or ``f(x)[1]``, an index chain ``m[1][2]``, the label of a reference link
  ``[text][1]``), and a footnote definition, a run that starts its line
  (after spaces or tabs) and is followed by ``:``. Letters and digits are
  ASCII, so a marker right after a letter of another script still links.
* The client links a group, as one chip per member in order, when EVERY
  member names a citation of the answer: a number the turn gave out, written
  as the client prints it (``[01]`` is not ``[1]``), or an earlier label the
  turn has (``E01`` reads as ``E1``). A group with any other member stays
  exactly as written.
* Nothing inside a chart tag, a fenced code block or an inline code span
  (backtick runs closed by an equal run) is linked. A fence opens on a line
  starting with three or more backticks or tildes and closes only on a line
  holding a run of the same character at least as long and nothing else
  (CommonMark), so a four-backtick block may contain a three-backtick line.
  Fence state starts afresh in each part between chart tags, as it does in
  the client, and a line or a stretch after a code span starts afresh too:
  the character before it and the line start are read within it.
* Digits are ASCII on both sides.

``tests/fixtures/citation_marker_cases.json`` is one case list both sides run
(``tests/unit/test_citation_marker_parity.py`` and
``frontend/src/test/citation-marker-parity.test.ts``). Stdlib only; no text
is logged.
"""
from __future__ import annotations

import re
import string
from collections.abc import Collection, Iterator
from typing import NamedTuple

#: A chart tag, quote-aware (``chat/charts.ts`` ``CHART_TAG_RE``).
_CHART_TAG_RE = re.compile(r"""<chart-block((?:[^>'"]|'[^']*'|"[^"]*")*)/?>""")
#: A fence line: optional indent, then a run of three or more of one character.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
#: An inline code span (``chat/citations.ts`` ``CODE_SPAN_RE``).
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
#: A marker group (``chat/citations.ts`` ``CITATION_GROUP_RE``); group 1 is
#: the members with their separators.
_MEMBER = r"(?:E[0-9]{1,2}|[0-9]+)"
_GROUP_RE = re.compile(rf"\[({_MEMBER}(?: *, *{_MEMBER})*)\](?!\()", re.ASCII)
#: Groups side by side, ``[1][2]`` (``chat/citations.ts`` ``CITATION_RUN_RE``).
_RUN_RE = re.compile(rf"(?:\[{_MEMBER}(?: *, *{_MEMBER})*\](?!\())+", re.ASCII)
#: A run right after one of these belongs to the text before it.
_ATTACHED = frozenset(string.ascii_letters + string.digits + "_])")
#: What may come before a footnote definition on its line.
_INDENT_RE = re.compile(r"[ \t]*")


class Member(NamedTuple):
    """One member of a marker group: ``[<number>]`` or ``[E<number>]``.
    ``canonical`` is False for a number the client never prints that way
    (a leading zero); an earlier label is always canonical."""

    number: int
    earlier: bool
    canonical: bool


class MarkerGroup(NamedTuple):
    """One marker group outside code and chart tags: ``text[start:end]``."""

    start: int
    end: int
    members: tuple[Member, ...]


def marker_groups(text: str) -> list[MarkerGroup]:
    """Every marker group of ``text`` outside chart tags, fenced blocks and
    inline code spans, in text order, whether or not the client links it."""
    if not isinstance(text, str) or "[" not in text:
        return []
    found: list[MarkerGroup] = []
    for start, end in _markdown_parts(text):
        for seg_start, seg_end, line_start in _linkable_segments(text, start, end):
            found.extend(_groups(text, seg_start, seg_end, line_start))
    return found


def names_citation(member: Member, current: Collection[int], earlier: Collection[int]) -> bool:
    """True when ``member`` names one of the turn's citations: a canonical
    number in ``current`` or an earlier label in ``earlier``."""
    if member.earlier:
        return member.number in earlier
    return member.canonical and member.number in current


def linked_markers(text: str, current: Collection[int],
                   earlier: Collection[int] = ()) -> list[MarkerGroup]:
    """The groups of ``text`` the client links for a turn whose citations are
    numbered ``current`` and whose earlier labels are numbered ``earlier``:
    those whose every member names one of them, in text order."""
    if not (current or earlier):
        return []
    return [group for group in marker_groups(text)
            if all(names_citation(m, current, earlier) for m in group.members)]


def _markdown_parts(text: str) -> Iterator[tuple[int, int]]:
    """``(start, end)`` of each stretch of ``text`` between chart tags."""
    last = 0
    for match in _CHART_TAG_RE.finditer(text):
        yield last, match.start()
        last = match.end()
    yield last, len(text)


def _closes(line: str, fence: str) -> bool:
    """True when ``line`` closes a block opened by the run ``fence``."""
    match = _FENCE_RE.match(line)
    return (match is not None and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence) and not line[match.end():].strip())


def _linkable_segments(text: str, start: int,
                       end: int) -> Iterator[tuple[int, int, bool]]:
    """``(start, end, starts its line)`` of each stretch of one markdown part
    outside fenced blocks, fence lines and inline code spans."""
    fence = None
    pos = start
    for line in text[start:end].split("\n"):
        line_start, pos = pos, pos + len(line) + 1
        if fence is not None:
            if _closes(line, fence):
                fence = None
            continue
        opened = _FENCE_RE.match(line)
        if opened:
            fence = opened.group(1)
            continue
        last = 0
        for span in _CODE_SPAN_RE.finditer(line):
            yield line_start + last, line_start + span.start(), last == 0
            last = span.end()
        yield line_start + last, line_start + len(line), last == 0


def _member(raw: str) -> Member:
    """One member as written (``"1"``, ``"01"``, ``"E2"``). A number too long
    to be any citation is read as -1 (never one), so a long digit run is
    never converted."""
    raw = raw.strip()
    if raw.startswith("E"):
        return Member(int(raw[1:]), True, True)
    number = int(raw) if len(raw) <= 9 else -1
    return Member(number, False, raw == str(number))


def _refused(text: str, start: int, end: int, run: re.Match, line_start: bool) -> bool:
    """True when a run in the stretch ``text[start:end]`` is never a
    citation: it follows an ``_ATTACHED`` character, or it is a footnote
    definition (it starts its line and ``:`` follows it)."""
    if run.start() > start and text[run.start() - 1] in _ATTACHED:
        return True
    return (line_start and run.end() < end and text[run.end()] == ":"
            and _INDENT_RE.fullmatch(text, start, run.start()) is not None)


def _groups(text: str, start: int, end: int, line_start: bool) -> Iterator[MarkerGroup]:
    """The marker groups in the stretch ``text[start:end]`` outside refused
    runs (``_refused``); the lookahead stops at ``end``, as the client's
    does at the end of its stretch."""
    for run in _RUN_RE.finditer(text, start, end):
        if _refused(text, start, end, run, line_start):
            continue
        for match in _GROUP_RE.finditer(text, run.start(), run.end()):
            members = tuple(_member(raw) for raw in match.group(1).split(","))
            yield MarkerGroup(match.start(), match.end(), members)
