# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pre-submit checks for openCypher shapes Neptune Analytics always rejects.

``union_column_mismatch``: Neptune rejects a ``UNION`` whose branches return
different column names ("All sub queries in an UNION must have the same column
names"). The model writes that shape repeatedly, and the engine's message does
not say WHICH names differ. The check reads each top-level branch's final
``RETURN`` list and reports the per-branch names only when the read is
CONFIDENT and the names differ.

``uses_regex_match``: the regular-expression operator ``=~`` ("RegexMatch is
not supported in this release"), found outside string literals.

``dotted_alias``: ``AS md.id`` in a ``RETURN`` / ``WITH`` projection ("Invalid
input '.': expected an identifier character"); an alias is a plain name.

Fail-open by design: an unterminated string, unbalanced brackets or a comment
(and, for the UNION check, ``CALL``, a ``UNION`` nested inside brackets,
``RETURN *``, a branch with no top-level ``RETURN``, or an un-aliased item
that is not a plain variable / property) makes a check report nothing, so the
query is submitted unchanged and the engine decides. All three share
``_mask``, so text inside quotes never matches. The query is never rewritten
or auto-aliased. A UNION difference in ORDER only is left to the engine too
(the names match, so the check is not confident the engine would refuse it).

``names_property``: the query names a given property (an internal one the
caller never serves). Unlike the checks above it FAILS CLOSED: it reads the
whole text, quotes and comments included, because a backtick-quoted name
(``s.`p` ``), a subscript string (``s['p']``) and an escaped one
(``s['\\u0070']``) all read the property, and a mention it cannot place is
still a mention.
"""
from __future__ import annotations

import re

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}
_QUOTES = {"'", '"', "`"}
_UNION_RE = re.compile(r"\bUNION(?:\s+ALL)?\b", re.IGNORECASE)
_RETURN_RE = re.compile(r"\bRETURN\b", re.IGNORECASE)
_TAIL_RE = re.compile(r"\b(?:ORDER\s+BY|SKIP|LIMIT)\b", re.IGNORECASE)
_AS_RE = re.compile(r"\bAS\b", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"^\s*DISTINCT\b", re.IGNORECASE)
_UNCERTAIN_RE = re.compile(r"//|/\*|\bCALL\b", re.IGNORECASE)
_PLAIN_ITEM_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?$")
_ALIAS_RE = re.compile(r"^(?:[A-Za-z_]\w*|`[^`]+`)$")
_COMMENT_RE = re.compile(r"//|/\*")
# ``AS md.id`` (spaces around the dot allowed). Matched on MASKED text, so a
# backtick-quoted alias such as AS `md.id` (legal) never matches.
_DOTTED_ALIAS_RE = re.compile(r"\bAS\s+[A-Za-z_]\w*\s*\.\s*[A-Za-z_]\w*", re.IGNORECASE)
# Clause keywords that can own an ``AS``. Only a RETURN / WITH projection is
# checked; UNWIND, YIELD and LOAD CSV aliases are left to the engine.
_CLAUSE_RE = re.compile(
    r"\b(?:RETURN|WITH|UNWIND|YIELD|LOAD|MATCH|WHERE|ORDER|SKIP|LIMIT|CREATE|"
    r"MERGE|SET|DELETE|REMOVE|CALL|UNION|FOREACH)\b",
    re.IGNORECASE,
)


# openCypher's escapes in strings (and the ones a model may write in a
# quoted name): ``\\uXXXX`` and ``\\UXXXXXXXX``.
_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _unescaped(text: str) -> str:
    def char(m: re.Match) -> str:
        try:
            return chr(int(m.group(1) or m.group(2), 16))
        except ValueError:                                   # past the last code point
            return m.group(0)
    return _ESCAPE_RE.sub(char, text)


def names_property(cypher: str, names) -> str | None:
    """The first of ``names`` (sorted) that ``cypher`` names as a whole word,
    in any spelling (bare, backtick-quoted, in a string, ``\\u``-escaped),
    else ``None``. Fails closed: see the module docstring."""
    if not isinstance(cypher, str):
        return None
    text = _unescaped(cypher)
    for name in sorted(names):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text):
            return name
    return None


def _mask(cypher: str) -> tuple[str, list[int]] | None:
    """``cypher`` with every quoted span's CONTENT replaced by ``_`` (same
    length, quotes kept) plus the bracket depth of each character; ``None``
    for an unterminated quote or unbalanced brackets."""
    out: list[str] = []
    depths: list[int] = []
    stack: list[str] = []
    quote = ""
    i = 0
    while i < len(cypher):
        ch = cypher[i]
        if quote:
            if ch == "\\" and quote != "`" and i + 1 < len(cypher):
                out.append("__")
                depths.extend([len(stack)] * 2)
                i += 2
                continue
            out.append(ch if ch == quote else "_")
            quote = "" if ch == quote else quote
        elif ch in _QUOTES:
            quote = ch
            out.append(ch)
        elif ch in _OPEN:
            stack.append(_OPEN[ch])
            out.append(ch)
        elif ch in _CLOSE:
            if not stack or stack.pop() != ch:
                return None
            out.append(ch)
        else:
            out.append(ch)
        depths.append(len(stack))
        i += 1
    if quote or stack:
        return None
    return "".join(out), depths


def _top_level(pattern: re.Pattern, masked: str, depths: list[int]) -> list[re.Match]:
    return [m for m in pattern.finditer(masked) if depths[m.start()] == 0]


def _split_top(text: str, depths: list[int], sep: str) -> list[tuple[int, int]]:
    """Spans of ``text`` split on ``sep`` characters at bracket depth 0."""
    spans, start = [], 0
    for i, ch in enumerate(text):
        if ch == sep and depths[i] == 0:
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(text)))
    return spans


def _item_name(original: str, masked: str, depths: list[int]) -> str | None:
    """The column name one RETURN item produces, or ``None`` when unsure."""
    aliases = _top_level(_AS_RE, masked, depths)
    if aliases:
        alias = original[aliases[-1].end():].strip()
        if not _ALIAS_RE.match(alias):
            return None
        return alias.strip("`")
    item = original.strip()
    return item if _PLAIN_ITEM_RE.match(item) else None


def _branch_columns(original: str, masked: str, depths: list[int]) -> list[str] | None:
    """The final top-level RETURN list of one UNION branch, or ``None``."""
    returns = _top_level(_RETURN_RE, masked, depths)
    if not returns:
        return None
    start = returns[-1].end()
    tail = [m for m in _top_level(_TAIL_RE, masked, depths) if m.start() > start]
    end = tail[0].start() if tail else len(masked)
    proj_masked, proj_orig = masked[start:end], original[start:end]
    distinct = _DISTINCT_RE.match(proj_masked)
    cut = distinct.end() if distinct else 0
    proj_masked, proj_orig = proj_masked[cut:], proj_orig[cut:]
    proj_depths = depths[start + cut:end]
    if proj_masked.strip() == "*":
        return None
    names = []
    for lo, hi in _split_top(proj_masked, proj_depths, ","):
        name = _item_name(proj_orig[lo:hi], proj_masked[lo:hi], proj_depths[lo:hi])
        if name is None:
            return None
        names.append(name)
    return names


def _scan(cypher: str) -> tuple[str, list[int]] | None:
    """``_mask`` output, or ``None`` when ``cypher`` is not a string, a quote
    or bracket is unbalanced, or it carries a comment (comment text is not
    masked, so a match could sit inside one: fail open)."""
    if not isinstance(cypher, str):
        return None
    scanned = _mask(cypher)
    if scanned is None or _COMMENT_RE.search(scanned[0]):
        return None
    return scanned


def uses_regex_match(cypher: str) -> bool:
    """True when ``cypher`` CONFIDENTLY uses the ``=~`` operator outside
    string literals; False otherwise (including any uncertain parse)."""
    scanned = _scan(cypher)
    return scanned is not None and "=~" in scanned[0]


def _after_dot(masked: str, pos: int) -> bool:
    """True when the token at ``pos`` follows a ``.``: a property such as
    ``x.as``, not a keyword."""
    return masked[:pos].rstrip().endswith(".")


def _owning_clause(masked: str, depths: list[int], pos: int) -> str | None:
    """The last clause keyword before ``pos`` in the same bracket group."""
    depth = depths[pos]
    start = pos
    while start > 0 and depths[start - 1] >= depth:
        start -= 1
    owner = None
    for m in _CLAUSE_RE.finditer(masked, start, pos):
        if depths[m.start()] == depth and not _after_dot(masked, m.start()):
            owner = m.group(0).upper()
    return owner


def dotted_alias(cypher: str) -> str | None:
    """The first ``AS a.b`` (original text) in a RETURN / WITH projection when
    the read is confident; ``None`` otherwise."""
    scanned = _scan(cypher)
    if scanned is None:
        return None
    masked, depths = scanned
    for m in _DOTTED_ALIAS_RE.finditer(masked):
        if _after_dot(masked, m.start()):
            continue
        if _owning_clause(masked, depths, m.start()) in ("RETURN", "WITH"):
            return cypher[m.start():m.end()]
    return None


def union_column_mismatch(cypher: str) -> list[list[str]] | None:
    """Per-branch RETURN column names when a top-level UNION's branches
    CONFIDENTLY return different names; ``None`` otherwise (no UNION, the
    names agree, or the parse is uncertain — submit unchanged)."""
    if not isinstance(cypher, str) or not _UNION_RE.search(cypher):
        return None
    scanned = _mask(cypher)
    if scanned is None:
        return None
    masked, depths = scanned
    if _UNCERTAIN_RE.search(masked):
        return None
    unions = list(_UNION_RE.finditer(masked))
    # A nested UNION, or ``x.union`` (a property, not the keyword): unsure.
    if any(depths[m.start()] != 0 or masked[m.start() - 1:m.start()] == "."
           for m in unions):
        return None
    bounds = [0] + [p for m in unions for p in (m.start(), m.end())] + [len(masked)]
    columns = []
    for lo, hi in zip(bounds[::2], bounds[1::2]):
        names = _branch_columns(cypher[lo:hi], masked[lo:hi], depths[lo:hi])
        if names is None:
            return None
        columns.append(names)
    if all(sorted(c) == sorted(columns[0]) for c in columns[1:]):
        return None
    return columns
