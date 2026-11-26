# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only check for openCypher the model writes.

``explore_graph`` runs a query the model wrote, and the runtime that serves it
can write the graph. :func:`write_clause` finds any keyword that writes the
graph or reaches outside a plain read, so the tool refuses the query before
it is submitted: ``CREATE``, ``MERGE``, ``SET``, ``REMOVE``, ``DELETE``,
``DETACH``, ``DROP``, ``CALL`` (procedures and ``CALL { }`` subqueries),
``LOAD`` (``LOAD CSV``) and ``FOREACH``.

The scan blanks string literals (both quote kinds, backslash escapes), backtick
names and ``//`` / ``/* */`` comments first, so a value or a comment that
contains one of those words still runs. It then reads whole words only, so a
name that merely contains one (``createdAt``, ``n.set_id``, a ``:Reset``
label) is not a keyword. A keyword is skipped only where openCypher allows
nothing but a name: after ``.`` (a property key), after ``:`` (a label or
relationship type), or as a map key (``{set: 1}``). Every other occurrence
is refused wherever it sits, so a write inside a ``UNION`` branch, an
``EXISTS { }`` or ``CALL { }`` subquery, or after ``WITH`` is caught too.

Fail closed: a query that is not a string, or whose string, backtick name or
block comment never closes, cannot be read with certainty and is reported as
:data:`UNREADABLE`.
"""
from __future__ import annotations

import re

#: Keywords that write the graph or reach outside a read.
WRITE_KEYWORDS = frozenset({
    "CREATE", "MERGE", "SET", "REMOVE", "DELETE", "DETACH", "DROP",
    "CALL", "LOAD", "FOREACH",
})

#: What :func:`write_clause` returns for a query it cannot scan.
UNREADABLE = "UNREADABLE"

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Every character a line comment can end at. Ending one early only means
# more text is scanned, which can refuse a read but never pass a write.
_LINE_ENDS = frozenset("\n\r\x0b\x0c\x85\u2028\u2029")


def _blank(cypher: str) -> str | None:
    """``cypher`` with string literals, backtick names and comments blanked
    (``''``, ``_q_`` and a space), or ``None`` when one never closes."""
    out: list[str] = []
    i, n = 0, len(cypher)
    while i < n:
        ch = cypher[i]
        if ch in ("'", '"'):
            i += 1
            while i < n and cypher[i] != ch:
                i += 2 if cypher[i] == "\\" else 1
            if i >= n:
                return None
            out.append("''")
            i += 1
        elif ch == "`":
            i += 1
            while True:
                if i >= n:
                    return None
                if cypher[i] == "`":
                    if i + 1 < n and cypher[i + 1] == "`":
                        i += 2
                        continue
                    break
                i += 1
            out.append("_q_")
            i += 1
        elif cypher.startswith("//", i):
            while i < n and cypher[i] not in _LINE_ENDS:
                i += 1
            out.append(" ")
        elif cypher.startswith("/*", i):
            end = cypher.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" ")
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _prev_char(text: str, pos: int) -> str:
    """The nearest character before ``pos`` that is not whitespace."""
    head = text[:pos].rstrip()
    return head[-1] if head else ""


def _next_char(text: str, pos: int) -> str:
    """The nearest character at or after ``pos`` that is not whitespace."""
    tail = text[pos:].lstrip()
    return tail[0] if tail else ""


def _name_position(text: str, start: int, end: int) -> bool:
    """True when the word at ``start:end`` sits where openCypher allows only
    a name: a property key, a label or relationship type, or a map key."""
    before = _prev_char(text, start)
    if before in (".", ":"):
        return True
    return before in ("{", ",") and _next_char(text, end) == ":"


def write_clause(cypher: object) -> str | None:
    """The first keyword in ``cypher`` that writes the graph or reaches
    outside a read (upper case), :data:`UNREADABLE` when the query cannot be
    scanned, or ``None`` for a read."""
    if not isinstance(cypher, str):
        return UNREADABLE
    text = _blank(cypher)
    if text is None:
        return UNREADABLE
    for match in _WORD.finditer(text):
        word = match.group(0).upper()
        if word not in WRITE_KEYWORDS:
            continue
        if match.start() and text[match.start() - 1] == "$":
            continue  # a query parameter such as $set
        if _name_position(text, match.start(), match.end()):
            continue
        return word
    return None
