# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse a TIMESTAMP column compared to a date at midnight, before it runs.

``ts = DATE 'd'`` matches only rows stamped exactly ``d 00:00:00``;
``ts <= DATE 'd'`` and ``ts BETWEEN ... AND DATE 'd'`` drop every row later
than midnight on the last day; ``ts > DATE 'd'`` keeps day ``d`` itself. The
engine accepts all of them (a DATE coerces to a TIMESTAMP at midnight), so the
answer still names the asked day while its count is short. Measured on a
rehearsal account: per-day pieces written as ``col = DATE 'd'`` matched almost
nothing of the true count. The prompt rule against it was not enough.

:func:`find_midnight_comparisons` reads one statement with the shared
tokenizer (``athena_scope.tokenize``) and reports each comparison of a bare
column reference whose type is ``timestamp...`` with a DATE literal or a
date-only string literal where the operator compares at midnight:

* ``col = | <= | > <date>`` and the mirrored ``<date> = | >= | < col``;
* ``col [NOT] BETWEEN <low> AND <date>`` (the upper bound is the midnight).

``>=`` and ``<`` with a date are the correct half-open bounds and pass, as do
a wrapped column (``date(col)``, ``CAST(col AS DATE)``), a date with
arithmetic (``DATE 'd' + INTERVAL '1' DAY``), a TIMESTAMP literal, and any
statement the tokenizer cannot read with certainty.

A reference is refused only when it is tied, by SQL scope, to a base-table
column whose catalogued type is ``timestamp...``. Its scope is the query block
whose SELECT it sits in (climbing only through grouping parentheses), and in
that block's FROM clause:

* a qualified ``q.col`` must name exactly one relation by its alias (or, when
  it has none, by its table name), and that relation must be a base table;
* an unqualified ``col`` is tied only when every relation in the block is a
  base table whose columns are known and exactly one of them has ``col``.

A derived table, a CTE (any CTE name, which also shadows a base table of the
same name), a table function, a table alias with a column list, a table in
another database, a qualifier from an outer scope and a reference in GROUP BY,
HAVING or ORDER BY are all unknown, and unknown passes: this check only ever
refuses what it is sure of. The Athena connector raises
:class:`TimestampDateComparisonError` from it; its :meth:`to_dict` is the
tool result the agent reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from .athena_scope import Token, _cte_bindings, _matching_close, tokenize

ERROR_NAME = "TIMESTAMP_COMPARED_TO_DATE"
_DATE_ONLY = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_ARITHMETIC = {"+", "-", "*", "/", "%", "|"}
#: Operators that compare at midnight when the column is on the left.
_LEFT_REFUSED = {"=", "<=", ">"}
#: The same comparisons written with the date on the left.
_MIRROR = {"=": "=", ">=": "<=", "<": ">"}

#: ``table_columns(name parts)``: ``{column: type}`` for a base table, or
#: ``None`` when its columns are not known.
TableColumns = Callable[[tuple], "dict | None"]


@dataclass(frozen=True)
class MidnightComparison:
    """One refused comparison: ``operator`` as if the column were on the left."""

    column: str
    operator: str
    day: date
    literal: str     # "DATE literal" or "date-only string"


class TimestampDateComparisonError(ValueError):
    """A query refused before it ran (see the module docstring)."""

    def __init__(self, finding: MidnightComparison):
        super().__init__(f"{ERROR_NAME}: {finding.column} {finding.operator}")
        self.finding = finding

    def to_dict(self) -> dict:
        from .engine_names import Withheld, printable
        f, names = self.finding, Withheld()
        column = names.show(f.column)
        day, after = f.day.isoformat(), (f.day + timedelta(days=1)).isoformat()
        return {
            "error": "invalid_request",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "column": f.column if printable(f.column) else "",
            "operator": f.operator,
            "guidance": (
                f"Refused before running: {column} is a TIMESTAMP column and comparing it "
                f"with {f.operator} to a {f.literal} compares at midnight, so it misses or "
                f"wrongly includes the rest of that day. For the whole day {day} use "
                f"{column} >= DATE '{day}' AND {column} < DATE '{after}'; for a span, "
                f"end with < the day after its last day. When the question carries a "
                f"RESOLVED TIME WINDOWS block, use its UTC start and end instead."
                f"{names.sentence()}"),
        }


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _is_name(token: Token) -> bool:
    return token.kind in ("word", "identifier")


def _literal_day(token: Token) -> "date | None":
    if token.kind != "literal":
        return None
    match = _DATE_ONLY.match(token.text[1:-1])
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _date_at(tokens: list, i: int) -> "tuple | None":
    """``(day, kind, end index)`` for a date literal starting at ``i``."""
    if i < len(tokens) and _upper(tokens[i]) == "DATE" and i + 1 < len(tokens):
        day = _literal_day(tokens[i + 1])
        if day is not None:
            return day, "DATE literal", i + 2
    if i < len(tokens):
        day = _literal_day(tokens[i])
        if day is not None:
            return day, "date-only string", i + 1
    return None


def _date_ending_at(tokens: list, k: int) -> "tuple | None":
    """``(day, kind, start index)`` for a date literal ending at ``k``."""
    day = _literal_day(tokens[k]) if 0 <= k < len(tokens) else None
    if day is None:
        return None
    if k >= 1 and _upper(tokens[k - 1]) == "DATE":
        return day, "DATE literal", k - 1
    return day, "date-only string", k


def _column_ending_at(tokens: list, k: int) -> "tuple | None":
    """``(name parts, start index)`` for a bare column reference ending at ``k``."""
    if not (0 <= k < len(tokens)) or not _is_name(tokens[k]):
        return None
    start = k
    while start >= 2 and tokens[start - 1].text == "." and _is_name(tokens[start - 2]):
        start -= 2
    return tuple(tokens[j].value for j in range(start, k + 1, 2)), start


def _column_starting_at(tokens: list, i: int) -> "tuple | None":
    """``(name parts, end index)`` for a bare column reference starting at ``i``."""
    if not (0 <= i < len(tokens)) or not _is_name(tokens[i]):
        return None
    end = i
    while end + 2 < len(tokens) and tokens[end + 1].text == "." and _is_name(tokens[end + 2]):
        end += 2
    if end + 1 < len(tokens) and tokens[end + 1].text in ("(", "."):
        return None                      # a function call, not a column
    return tuple(tokens[j].value for j in range(i, end + 1, 2)), end + 1


def _bare_before(tokens: list, start: int) -> bool:
    """Nothing binds tighter on the left of the operand starting at ``start``."""
    return start == 0 or tokens[start - 1].text not in _ARITHMETIC | {"."}


def _bare_after(tokens: list, end: int) -> bool:
    """Nothing binds tighter on the right of the operand ending before ``end``."""
    return end >= len(tokens) or tokens[end].text not in _ARITHMETIC | {"(", "."}


def _candidates(tokens: list) -> list:
    """Every syntactic midnight comparison:
    ``(name parts, operator, day, kind, column token index)``."""
    found = []
    for i, token in enumerate(tokens):
        if token.kind == "operator" and token.text in _LEFT_REFUSED:
            column = _column_ending_at(tokens, i - 1)
            literal = _date_at(tokens, i + 1)
            if column and literal and _bare_before(tokens, column[1]) \
                    and _bare_after(tokens, literal[2]):
                found.append((column[0], token.text, literal[0], literal[1], column[1]))
        if token.kind == "operator" and token.text in _MIRROR:
            literal = _date_ending_at(tokens, i - 1)
            column = _column_starting_at(tokens, i + 1)
            if column and literal and _bare_before(tokens, literal[2]) \
                    and _bare_after(tokens, column[1]):
                found.append((column[0], _MIRROR[token.text], literal[0], literal[1], i + 1))
        if _upper(token) == "BETWEEN":
            found.extend(_between(tokens, i))
    return found


def _between(tokens: list, i: int) -> list:
    k = i - 1
    if k >= 0 and _upper(tokens[k]) == "NOT":
        k -= 1
    column = _column_ending_at(tokens, k)
    if not column or not _bare_before(tokens, column[1]):
        return []
    low_end = _simple_operand_end(tokens, i + 1)
    if low_end is None or low_end >= len(tokens) or _upper(tokens[low_end]) != "AND":
        return []
    high = _date_at(tokens, low_end + 1)
    if not high or not _bare_after(tokens, high[2]):
        return []
    return [(column[0], "BETWEEN", high[0], high[1], column[1])]


def _simple_operand_end(tokens: list, i: int) -> "int | None":
    """The index after a one-literal operand (a typed or plain literal, a
    number, or a bare column), or ``None`` for anything more complex."""
    if i >= len(tokens):
        return None
    if _upper(tokens[i]) in ("DATE", "TIMESTAMP") and i + 1 < len(tokens) \
            and tokens[i + 1].kind == "literal":
        end = i + 2
    elif tokens[i].kind in ("literal", "number"):
        end = i + 1
    else:
        column = _column_starting_at(tokens, i)
        if column is None:
            return None
        end = column[1]
    return end if _bare_after(tokens, end) else None


def has_candidates(sql: str) -> bool:
    """Cheap pre-check (no types): could this statement hold a midnight
    comparison at all? ``False`` lets the caller skip the type lookup."""
    if not isinstance(sql, str) or "'" not in sql:
        return False
    tokens, uncertain = tokenize(sql)
    return not uncertain and bool(_candidates(tokens))


# --------------------------------------------------------------------------- #
# Scope: which relation a column reference belongs to
# --------------------------------------------------------------------------- #
#: Words a grouping parenthesis follows (anything else before ``(`` is a call).
_GROUPING_BEFORE = {"WHERE", "AND", "OR", "NOT", "ON", "HAVING", "WHEN", "THEN", "ELSE"}
_CLAUSES = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH", "WINDOW"}
#: Clauses whose column references resolve against the block's FROM relations.
_RESOLVED_CLAUSES = {"SELECT", "FROM", "WHERE"}
_SET_OPERATIONS = {"UNION", "INTERSECT", "EXCEPT"}
_JOIN_WORDS = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL"}
_NOT_AN_ALIAS = _JOIN_WORDS | {"ON", "USING", "AS", "TABLESAMPLE", "FOR", "LATERAL",
                               "UNNEST", "WITH", "ORDINALITY", "TABLE"}
_DERIVED_STARTS = {"LATERAL", "UNNEST", "TABLE"}


@dataclass(frozen=True)
class _Relation:
    """One FROM item. ``parts`` is the base table name (``()`` when derived);
    ``alias`` is lowercased, ``""`` when there is none."""

    parts: tuple
    alias: str
    derived: bool

    def answers_to(self, qualifier: tuple) -> bool:
        wanted = tuple(part.lower() for part in qualifier)
        if self.alias:
            return wanted == (self.alias,)
        names = tuple(part.lower() for part in self.parts)
        return bool(names) and len(wanted) <= len(names) and names[-len(wanted):] == wanted


def _select_for(tokens: list, index: int) -> "int | None":
    """The SELECT that opens the query block ``tokens[index]`` sits in,
    climbing only through grouping parentheses; ``None`` when there is none
    (a function call's argument, a VALUES list)."""
    depth, i = tokens[index].depth, index
    while True:
        j = i - 1
        while j >= 0 and tokens[j].depth >= depth:
            if tokens[j].depth == depth and _upper(tokens[j]) == "SELECT":
                return j
            j -= 1
        if j < 1 or tokens[j].text != "(":
            return None
        before = tokens[j - 1]
        if before.text != "(" and _upper(before) not in _GROUPING_BEFORE:
            return None
        i, depth = j, tokens[j].depth


def _block_end(tokens: list, select: int) -> int:
    depth, end = tokens[select].depth, select + 1
    while end < len(tokens) and tokens[end].depth >= depth:
        if tokens[end].kind == "semicolon" or (
                tokens[end].depth == depth and _upper(tokens[end]) in _SET_OPERATIONS):
            break
        end += 1
    return end


def _clause_of(tokens: list, select: int, index: int) -> str:
    depth, clause = tokens[select].depth, "SELECT"
    for k in range(select + 1, index):
        if tokens[k].depth == depth and _upper(tokens[k]) in _CLAUSES:
            clause = _upper(tokens[k])
    return clause


def _from_span(tokens: list, select: int, end: int) -> "tuple | None":
    depth, start = tokens[select].depth, None
    for k in range(select + 1, end):
        if tokens[k].depth != depth:
            continue
        if start is None and _upper(tokens[k]) == "FROM":
            start = k + 1
        elif start is not None and _upper(tokens[k]) in _CLAUSES:
            return start, k
    return (start, end) if start is not None else None


def _relation_name(tokens: list, i: int, end: int) -> "tuple | None":
    """``(name parts, next index, is a call)`` for a FROM item starting at ``i``."""
    parts, j = [tokens[i].value], i
    while j + 2 < end and tokens[j + 1].text == "." and _is_name(tokens[j + 2]):
        parts.append(tokens[j + 2].value)
        j += 2
    return tuple(parts), j + 1, j + 1 < end and tokens[j + 1].text == "("


def _relation_item(tokens: list, i: int, end: int, ctes: set) -> "tuple | None":
    """``(relation, next index)`` for the FROM item at ``i``, or ``None``."""
    first = tokens[i]
    if first.text == "(":
        close = _matching_close(tokens, i)
        if close is None or close >= end:
            return None
        parts, i, derived = (), close + 1, True
    elif _upper(first) in _DERIVED_STARTS:
        return _Relation((), "", True), i + 1
    elif _is_name(first) and _upper(first) not in _NOT_AN_ALIAS:
        parts, i, call = _relation_name(tokens, i, end)
        if call:
            return _Relation((), "", True), i
        derived = len(parts) == 1 and parts[0].lower() in ctes
    else:
        return None
    if i < end and _upper(tokens[i]) == "AS":
        i += 1
        if i >= end or not _is_name(tokens[i]):
            return None
    alias = ""
    if i < end and _is_name(tokens[i]) and _upper(tokens[i]) not in _NOT_AN_ALIAS:
        alias, i = tokens[i].value.lower(), i + 1
        if i < end and tokens[i].text == "(":    # a column list renames the columns
            close = _matching_close(tokens, i)
            if close is None:
                return None
            derived, i = True, close + 1
    return _Relation(() if derived else parts, alias, derived), i


def _relations(tokens: list, start: int, end: int, ctes: set) -> "list | None":
    """The FROM items between ``start`` and ``end``; ``None`` when unreadable."""
    depth = tokens[start].depth if start < end else 0
    relations, i = [], start
    while i < end:
        item = _relation_item(tokens, i, end, ctes)
        if item is None:
            return None
        relations.append(item[0])
        i = item[1]
        while i < end and not (tokens[i].depth == depth and (
                tokens[i].text == "," or _upper(tokens[i]) == "JOIN")):
            i += 1                               # an ON / USING condition, join words
        i += 1
    return relations or None


def _reference_type(parts: tuple, relations: list, columns) -> "str | None":
    """The catalogued type a reference is tied to, ``None`` when it is not tied
    unambiguously to one base-table column."""
    column, qualifier = parts[-1].lower(), parts[:-1]
    if qualifier:
        matches = [r for r in relations if r.answers_to(qualifier)]
        if len(matches) != 1 or matches[0].derived:
            return None
        known = columns(matches[0].parts)
        return known.get(column) if known else None
    if any(r.derived for r in relations):
        return None
    found = []
    for relation in relations:
        known = columns(relation.parts)
        if known is None:
            return None
        if column in known:
            found.append(known[column])
    return found[0] if len(found) == 1 else None


def _resolved_type(tokens: list, index: int, parts: tuple, ctes: set, columns) -> "str | None":
    select = _select_for(tokens, index)
    if select is None or _clause_of(tokens, select, index) not in _RESOLVED_CLAUSES:
        return None
    span = _from_span(tokens, select, _block_end(tokens, select))
    relations = _relations(tokens, span[0], span[1], ctes) if span else None
    return _reference_type(parts, relations, columns) if relations else None


def _columns_lookup(table_columns: TableColumns):
    cache: dict = {}

    def columns(parts: tuple) -> "dict | None":
        key = tuple(part.lower() for part in parts)
        if key not in cache:
            known = table_columns(key)
            cache[key] = ({str(k).lower(): str(v).lower() for k, v in known.items()}
                          if isinstance(known, dict) and known else None)
        return cache[key]
    return columns


def find_midnight_comparisons(sql: str, table_columns: TableColumns) -> list:
    """The refused comparisons in ``sql``. ``table_columns(name parts)``
    answers a base table's ``{column: type}`` or ``None`` when unknown; only a
    reference tied by scope to a known ``timestamp...`` column is refused.
    Uncertain SQL (or an unreadable WITH list) gives ``[]``."""
    if not isinstance(sql, str):
        return []
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return []
    bindings, cte_uncertain = _cte_bindings(tokens)
    if cte_uncertain:
        return []
    ctes = {binding.name.lower() for binding in bindings}
    columns = _columns_lookup(table_columns)
    findings = []
    for parts, operator, day, kind, index in _candidates(tokens):
        dtype = _resolved_type(tokens, index, parts, ctes, columns) or ""
        if dtype.strip().startswith("timestamp"):
            findings.append(MidnightComparison(".".join(parts), operator, day, kind))
    return findings


def check(sql: str, table_columns: TableColumns) -> None:
    """Raise :class:`TimestampDateComparisonError` for the first refused
    comparison in ``sql``; return quietly when there is none."""
    findings = find_midnight_comparisons(sql, table_columns)
    if findings:
        raise TimestampDateComparisonError(findings[0])
