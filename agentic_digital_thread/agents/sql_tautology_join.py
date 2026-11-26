# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse a JOIN whose ON condition is true for every pair of rows.

Workshop rehearsal, 2026-11-12: a system agent built a set of records with
``JOIN t2 b ON a.k = a.k``. Both sides of the equality are the same column
of the same alias, so the condition links nothing: every row of the joined
table matched every row before it, the set held every record instead of the
one the request named, and the agent reported a total over all of them as
the answer. The aggregate check rightly passed it (no row repeats in the
outer SUM); the set itself was wrong.

Before the PostgreSQL query tool runs a SELECT, :func:`check` refuses a JOIN
whose ON condition is ALWAYS TRUE as written and links no row of one side to
a row of the other:

* every conjunct (joined by AND at the condition's own level, a fully
  parenthesised group read as its own conjunction) either is always true
  (``TRUE``, or ``X = X`` where both sides are the same column reference,
  number or string) or references columns of at most one alias, and
* at least one conjunct is always true.

So ``ON a.k = a.k``, ``ON 1 = 1``, ``ON TRUE`` and ``ON a.k = a.k AND
b.s = 'x'`` are refused; ``ON a.parent_id = b.id`` (a self-join of two
aliases of one table), ``ON a.k = b.k AND a.k = a.k`` and ``ON b.s = 'x'``
run as before. Parentheses wrapping an operand are peeled first, so
``(a.k) = ((a.k))`` is ``a.k = a.k``. The refusal holds fixed fields only
(:data:`REFUSAL`): never the condition's text or a value from it, so a
literal of a query that never ran cannot pass for a value the system
returned. The log names only the kind of always-true condition.
A cross product the agent really means is written ``CROSS JOIN``, which the
note says and this check never reads.

Everything it cannot read for certain runs as before (fail open): SQL the
tokenizer marks uncertain (one warning is logged, without the SQL), an OR,
BETWEEN or CASE in the condition, a
subquery anywhere in the condition, a joined subquery, function or LATERAL
item (``JOIN LATERAL (...) x ON TRUE`` links inside its subquery), an
unqualified column name next to anything but itself, ``USING`` and
``NATURAL`` joins, and any error inside the check (logged, never blocking).

Generic: no dataset, system, table, column or concept literals. Pure apart
from logging; the SQL is read with the safety layer's tokenizer
(``athena_scope``).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Words that end an ON condition at its own depth.
_CONDITION_ENDS = frozenset({
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "WHERE", "GROUP",
    "ORDER", "LIMIT", "OFFSET", "HAVING", "WINDOW", "FETCH", "UNION", "INTERSECT",
    "EXCEPT", "FOR", "ON", "USING", "RETURNING",
})
#: Words that, at the condition's level, make its truth unreadable here.
_UNREADABLE = frozenset({"OR", "BETWEEN", "CASE", "SELECT"})
#: The words walked back to when deciding what an ON belongs to.
_ON_OWNERS = frozenset({"JOIN", "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "HAVING",
                        "ON", "DISTINCT", "USING", "LATERAL"})
#: Unquoted words that are never a column name.
_KEYWORDS = frozenset({
    "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE", "IN", "LIKE", "ILIKE", "SIMILAR",
    "TO", "ESCAPE", "DISTINCT", "FROM", "AS", "INTERVAL", "DATE", "TIME", "TIMESTAMP",
    "ANY", "ALL", "SOME", "CAST", "AT", "ZONE", "COLLATE",
})
#: Words that are join keywords but also text functions (``left(x, 2)``).
_FUNCTION_WORDS = frozenset({"LEFT", "RIGHT"})

#: The refusal note: fixed, it carries no data.
NOTE = (
    "Not run: this JOIN's ON condition is true for every pair of rows. It compares a value "
    "with itself (or is always true) and links no row of one table to a row of the other, "
    "so every row of the joined table would match every row before it, and any list, "
    "count or total from it would cover records the request did not name. Join the two "
    "tables on a column of EACH side that holds the same identifier, such as a declared "
    "join key or a value the request supplies. If every row of one side really should "
    "pair with every row of the other (for example a one-row total), write CROSS JOIN.")


def _word(tokens, i) -> str:
    """The upper-cased word at ``i``, or "" (not a word, out of range, or a
    join word used as a function call)."""
    if i < 0 or i >= len(tokens) or tokens[i].kind != "word":
        return ""
    word = tokens[i].value.upper()
    if word in _FUNCTION_WORDS and i + 1 < len(tokens) and tokens[i + 1].text == "(":
        return ""
    return word


def _name(tok) -> str:
    return tok.value.lower() if tok.kind == "word" else tok.value


def _is_name(tok) -> bool:
    return tok.kind == "identifier" or (tok.kind == "word" and tok.value.upper() not in _KEYWORDS)


def _joined_plain_relation(tokens, on_index) -> bool:
    """True when the ON at ``on_index`` belongs to a JOIN whose joined item
    is a plain table or CTE name (no parenthesis, no LATERAL between)."""
    depth = tokens[on_index].depth
    for i in range(on_index - 1, -1, -1):
        tok = tokens[i]
        if tok.depth < depth:
            return False
        if tok.depth > depth or tok.text in ("(", ")"):
            if tok.text == "(" and tok.depth == depth:
                return False
            continue
        word = _word(tokens, i)
        if word in _ON_OWNERS:
            return word == "JOIN"
    return False


def _condition_end(tokens, on_index) -> int:
    """The index just past the ON condition that starts after ``on_index``."""
    depth = tokens[on_index].depth
    for i in range(on_index + 1, len(tokens)):
        tok = tokens[i]
        if tok.depth < depth or tok.kind == "semicolon":
            return i
        if tok.depth == depth and _word(tokens, i) in _CONDITION_ENDS:
            return i
    return len(tokens)


def _matching_close(tokens, open_index):
    from .athena_scope import _matching_close as close
    return close(tokens, open_index)


def _conjuncts(tokens, first, end):
    """``[(first, end)]`` of the AND-joined conjuncts of ``tokens[first:end]``,
    or None when the span holds anything this check does not read."""
    if first >= end:
        return None
    depth = tokens[first].depth
    spans, start = [], first
    for i in range(first, end):
        tok = tokens[i]
        if tok.depth < depth:
            return None
        word = _word(tokens, i)
        if word in _UNREADABLE:
            return None
        if tok.depth == depth and word == "AND":
            spans.append((start, i))
            start = i + 1
    spans.append((start, end))
    return None if any(a >= b for a, b in spans) else spans


def _peel(tokens, first, end) -> tuple[int, int]:
    """``(first, end)`` with every complete balanced parenthesis pair that
    wraps the whole span removed, nested ones too: ``((a.k))`` reads as
    ``a.k``. ``(a) = (b)`` is not one wrapper and is left as it is."""
    while end - first >= 2 and tokens[first].text == "(" \
            and _matching_close(tokens, first) == end - 1:
        first, end = first + 1, end - 1
    return first, end


def _operand(tokens, first, end):
    """A comparable key for a simple operand (a column reference, a number or
    a string, in any number of wrapping parentheses), or None."""
    first, end = _peel(tokens, first, end)
    span = tokens[first:end]
    if len(span) == 1 and span[0].kind in ("number", "literal"):
        return (span[0].kind, span[0].text)
    if not span or len(span) % 2 == 0:
        return None
    names = span[0::2]
    if not all(_is_name(t) for t in names) or any(t.text != "." for t in span[1::2]):
        return None
    return ("ref",) + tuple(_name(t) for t in names)


def _always_true(tokens, first, end) -> str | None:
    """The kind of an always-true conjunct, else None: ``true_literal``
    (``TRUE``), ``self_comparison`` (one column reference on both sides of
    ``=``) or ``constant_comparison`` (one number or string on both sides)."""
    if end - first == 1 and _word(tokens, first) == "TRUE":
        return "true_literal"
    depth = tokens[first].depth
    equals = [i for i in range(first, end)
              if tokens[i].kind == "operator" and tokens[i].text == "="]
    if len(equals) != 1 or tokens[equals[0]].depth != depth:
        return None
    left, right = _operand(tokens, first, equals[0]), _operand(tokens, equals[0] + 1, end)
    if left is None or left != right:
        return None
    return "self_comparison" if left[0] == "ref" else "constant_comparison"


def _qualifiers(tokens, first, end):
    """The aliases the column references of a conjunct name, or None when
    one is unqualified (it could be either side)."""
    found = set()
    i = first
    while i < end:
        tok = tokens[i]
        prev = tokens[i - 1].text if i > first else ""
        nxt = tokens[i + 1].text if i + 1 < end else ""
        if not _is_name(tok) or prev in (".", ":") or nxt == "(":
            i += 1
            continue
        parts = [_name(tok)]
        while i + 2 < end and tokens[i + 1].text == "." and _is_name(tokens[i + 2]):
            parts.append(_name(tokens[i + 2]))
            i += 2
        if len(parts) == 1:
            return None
        found.add(tuple(parts[:-1]))
        i += 1
    return found


def _classify(tokens, first, end):
    """``(kinds, links)`` of the condition ``tokens[first:end]``, or None when
    it cannot be read: ``kinds`` lists the kind of each always-true conjunct
    (:func:`_always_true`), ``links`` is True when a conjunct may tie two
    aliases."""
    spans = _conjuncts(tokens, first, end)
    if spans is None:
        return None
    kinds, links = [], False
    for a, b in spans:
        if tokens[a].text == "(" and _matching_close(tokens, a) == b - 1:
            inner = _classify(tokens, a + 1, b - 1)
            if inner is None:
                return None
            kinds, links = kinds + inner[0], links or inner[1]
            continue
        kind = _always_true(tokens, a, b)
        if kind:
            kinds.append(kind)
        else:
            names = _qualifiers(tokens, a, b)
            links = links or names is None or len(names) > 1
    return kinds, links


#: The kind of every refusal, the one field of it besides the fixed note.
KIND = "always_true_join"
#: The refusal the tool returns: fixed fields only, never the SQL's text or
#: values (a literal of a query that never ran is not a value the system read).
REFUSAL = {"status": "not_run", "join_check": {"kind": KIND}, "note": NOTE}


def _refused_kinds(tokens) -> list | None:
    """The always-true conjunct kinds of the first JOIN condition that links
    nothing, or None when none does."""
    for i in range(len(tokens)):
        if _word(tokens, i) != "ON" or not _joined_plain_relation(tokens, i):
            continue
        end = _condition_end(tokens, i)
        found = _classify(tokens, i + 1, end) if end > i + 1 else None
        if found is not None and found[0] and not found[1]:
            return found[0]
    return None


def check(sql: str) -> dict | None:
    """The refusal result for ``sql`` (a copy of :data:`REFUSAL`), or None to
    run it as today. Logs, without the SQL or any value in it, which kind of
    always-true condition it refused, and one warning when the tokenizer
    could not read the SQL for certain (the query then runs unchecked)."""
    if not isinstance(sql, str) or not sql.strip():
        return None
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain:
        logger.warning("SQL_TAUTOLOGY_JOIN: SQL not read for certain by the tokenizer; "
                       "the query runs unchecked")
        return None
    kinds = _refused_kinds(tokens) if tokens else None
    if not kinds:
        return None
    logger.info("SQL_TAUTOLOGY_JOIN: always-true join condition (%s); query not run",
                ",".join(sorted(set(kinds))))
    return {"status": REFUSAL["status"], "join_check": dict(REFUSAL["join_check"]),
            "note": NOTE}


def guard_query(sql: str) -> dict | None:
    """:func:`check` for the query tool: fail open on any error (logged)."""
    try:
        return check(sql)
    except Exception as exc:  # noqa: BLE001 - the guard is an addition, never required
        logger.warning("SQL_TAUTOLOGY_JOIN guard failed (%s); the query runs", type(exc).__name__)
        return None
