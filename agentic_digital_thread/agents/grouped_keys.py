# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which outputs of a query's rows hold its grouped values, read for detection.

A check that reads a result's rows (``json_spellings``) needs the output
positions whose values are unique per row: the GROUP BY keys, or every output
of a SELECT DISTINCT. The exact split's shape parser (``athena_exact_shape``)
admits only queries it can add up from pieces, so it refuses ordinary grouped
queries (AVG, HAVING, DISTINCT, a CTE). This reader decides nothing about the
figures, so it admits more:

* one SELECT with GROUP BY: an output is a grouped value when a GROUP BY item
  is its position or its expression, and every GROUP BY item must be one of
  the outputs (a grouping key that is not projected would let the projected
  values repeat). Any aggregates, HAVING, ORDER BY, LIMIT, joins, and CTEs
  before it are allowed: none of them changes which outputs are unique.
* SELECT DISTINCT with no GROUP BY: every output is a grouped value.
* one hop: an outer SELECT with neither, whose FROM is only one CTE (or one
  derived table) that is one of the shapes above, and that projects every
  grouped value of it unchanged, by name or with ``*``.

Anything else gives ``None``, and the caller passes the result through: a
window function, grouping sets, a set operation, a star at the grouping level,
a CTE with its own column list, more than one statement, or any statement the
shared tokenizer reads as uncertain. Pure.
"""
from __future__ import annotations

from dataclasses import dataclass

from .athena_exact_shape import _column_ref, _norm, _split_alias, _split_commas
from .athena_scope import _matching_close, _parse_with_bindings, relations, tokenize

_CLAUSES = ("FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH")
_SET_OPERATIONS = {"UNION", "INTERSECT", "EXCEPT"}
_GROUPING_SETS = {"GROUPING", "ROLLUP", "CUBE", "ALL", "DISTINCT"}
_AFTER_FROM = {"WHERE", "ORDER", "LIMIT", "OFFSET", "FETCH"}


@dataclass(frozen=True)
class GroupedKeys:
    """``width`` outputs per row; ``keys`` the ``(position, column)`` of each
    grouped value, ``column`` the physical column it projects (``None`` for an
    expression); ``table`` the one table the query scans, else ``""``."""

    width: int
    keys: tuple
    table: str


@dataclass(frozen=True)
class _Level:
    outputs: tuple           # (expression tokens, alias) per output
    keys: tuple | None       # grouped positions, None when neither grouped nor DISTINCT
    star: bool
    clauses: dict            # clause word -> token index, at this SELECT's depth


def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _clauses(tokens, start: int):
    """``{clause: index}`` at the depth of the SELECT at ``start``, else
    ``None`` for a set operation."""
    depth, found = tokens[start].depth, {}
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token.depth < depth:
            break
        word = _upper(token)
        if token.depth != depth or not word:
            continue
        if word in _SET_OPERATIONS:
            return None
        if word in _CLAUSES and word not in found:
            found[word] = index
    return found


def _clause_end(tokens, clauses: dict, keyword: int) -> int:
    """The end of the clause whose keyword is at ``keyword``: the next clause
    of the same SELECT, or the close of its scope."""
    later = [i for i in clauses.values() if i > keyword]
    depth = tokens[keyword].depth
    stop = next((i for i in range(keyword + 1, len(tokens)) if tokens[i].depth < depth),
                len(tokens))
    return min([stop, *later])


def _group_positions(tokens, clauses: dict, outputs) -> tuple | None:
    """The output positions the GROUP BY items name, else ``None`` when one
    of them names no output or the clause uses grouping sets."""
    start = clauses["GROUP"] + 2
    if start > len(tokens) or _upper(tokens[start - 1]) != "BY":
        return None
    items = _split_commas(tokens[start:_clause_end(tokens, clauses, start - 1)])
    if not items:
        return None
    positions = set()
    for item in items:
        if not item or _upper(item[0]) in _GROUPING_SETS:
            return None
        position = _output_position(item, outputs)
        if position is None:
            return None
        positions.add(position)
    return tuple(sorted(positions))


def _output_position(item, outputs) -> int | None:
    if len(item) == 1 and item[0].kind == "number" and item[0].text.isdigit():
        position = int(item[0].text) - 1
        return position if 0 <= position < len(outputs) else None
    text, ref = _norm(item), _column_ref(item)
    for position, (expression, _alias) in enumerate(outputs):
        if _norm(expression) == text:
            return position
    if ref is not None:
        same = [p for p, (expression, _alias) in enumerate(outputs)
                if (_column_ref(expression) or "").lower() == ref.lower()]
        return same[0] if len(same) == 1 else None
    return None


def _read_level(tokens, start: int) -> _Level | None:
    """The outputs and grouped positions of the SELECT at ``start``."""
    clauses = _clauses(tokens, start)
    if clauses is None or "FROM" not in clauses or ("HAVING" in clauses and "GROUP" not in clauses):
        return None
    distinct = start + 1 < len(tokens) and _upper(tokens[start + 1]) == "DISTINCT"
    first = start + (2 if distinct else 1)
    outputs = tuple(_split_alias(item) for item in _split_commas(tokens[first:clauses["FROM"]]))
    if not outputs or any(not expression for expression, _alias in outputs):
        return None
    star = any(expression[-1].text == "*" for expression, _alias in outputs)
    keys = None
    if "GROUP" in clauses:
        keys = None if star else _group_positions(tokens, clauses, outputs)
        if keys is None:
            return None
    elif distinct:
        if star:
            return None
        keys = tuple(range(len(outputs)))
    return _Level(outputs, keys, star, clauses)


def _name(expression, alias) -> str | None:
    name = alias if alias is not None else _column_ref(expression)
    return name.lower() if name is not None else None


def _hop_body(text: str, tokens, level: _Level, bindings) -> str | None:
    """The SQL of the one CTE or derived table the outer FROM reads, else ``None``."""
    start = level.clauses["FROM"] + 1
    span = tokens[start:_clause_end(tokens, level.clauses, start - 1)]
    if not span:
        return None
    if span[0].text == "(":
        close = _matching_close(span, 0)
        rest = span[close + 1:] if close is not None else None
        if rest is None or not (len(rest) <= 1 or (len(rest) == 2 and _upper(rest[0]) == "AS")):
            return None
        return text[span[0].end:span[close].start]
    if span[0].kind not in {"word", "identifier"} or len(span) > 3:
        return None
    if (len(span) == 3 and _upper(span[1]) != "AS") or (len(span) == 2 and span[1].kind != "word"):
        return None
    for binding in bindings:
        if binding.name.lower() == span[0].value.lower():
            if tokens[binding.declaration_index + 1].text == "(":
                return None
            return text[tokens[binding.body_open].end:tokens[binding.body_close].start]
    return None


def _projected(level: _Level, inner: _Level) -> tuple | None:
    """``(width, ((outer position, inner position), ...))`` for an outer
    SELECT that projects every grouped value of ``inner`` unchanged."""
    if level.star:
        if len(level.outputs) != 1 or len(level.outputs[0][0]) != 1:
            return None
        return len(inner.outputs), tuple((p, p) for p in inner.keys)
    names = [_name(expression, alias) for expression, alias in inner.outputs]
    named = [name for name in names if name is not None]
    if any(names[p] is None for p in inner.keys) or len(set(named)) != len(named):
        return None
    pairs = []
    for position, (expression, _alias) in enumerate(level.outputs):
        ref = _column_ref(expression)
        if ref is not None and ref.lower() in names and names.index(ref.lower()) in inner.keys:
            pairs.append((position, names.index(ref.lower())))
    if {inner_position for _p, inner_position in pairs} != set(inner.keys):
        return None
    return len(level.outputs), tuple(pairs)


def _outer_select(tokens):
    """``(index of the outer SELECT, CTE bindings)``, else ``None``."""
    if _upper(tokens[0]) == "SELECT":
        return 0, []
    if _upper(tokens[0]) != "WITH":
        return None
    bindings, malformed = _parse_with_bindings(tokens, 0)
    if malformed or not bindings or bindings[0].recursive:
        return None
    index = bindings[-1].body_close + 1
    if index >= len(tokens) or _upper(tokens[index]) != "SELECT" or tokens[index].depth != 0:
        return None
    return index, bindings


def _statement(sql):
    """``(text, tokens)`` of one certain statement with no window function."""
    if not isinstance(sql, str):
        return None
    text = sql.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens or any(t.kind == "semicolon" for t in tokens):
        return None
    if any(_upper(t) == "OVER" for t in tokens):
        return None
    return text, tokens


def _table(tokens) -> str:
    scans, uncertain = relations(tokens)
    return str(scans[0].parts[-1]) if not uncertain and len(scans) == 1 else ""


def read(sql) -> GroupedKeys | None:
    """The grouped values of ``sql``'s rows (see the module docstring)."""
    statement = _statement(sql)
    if statement is None:
        return None
    text, tokens = statement
    outer = _outer_select(tokens)
    level = _read_level(tokens, outer[0]) if outer else None
    if level is None:
        return None
    if level.keys is not None:
        keys = tuple((p, _column_ref(level.outputs[p][0])) for p in level.keys)
        return GroupedKeys(len(level.outputs), keys, _table(tokens))
    body = _hop_body(text, tokens, level, outer[1])
    inner_statement = _statement(body) if body else None
    if inner_statement is None or _upper(inner_statement[1][0]) != "SELECT":
        return None
    inner = _read_level(inner_statement[1], 0)
    projected = _projected(level, inner) if inner is not None and inner.keys else None
    if projected is None:
        return None
    width, pairs = projected
    keys = tuple((p, _column_ref(inner.outputs[i][0])) for p, i in pairs)
    return GroupedKeys(width, keys, _table(tokens))


def _counts_rows(expression) -> bool:
    """True for ``COUNT(*)`` or ``COUNT(<number>)``: a count of the group's rows."""
    return (len(expression) == 4 and _upper(expression[0]) == "COUNT"
            and expression[1].text == "(" and expression[3].text == ")"
            and (expression[2].text == "*" or expression[2].kind == "number"))


def row_counts(sql) -> tuple:
    """The output positions of ``sql`` that count the rows of their group
    (``COUNT(*)`` or ``COUNT(<number>)``), for a query that :func:`read` reads
    as one grouped SELECT; ``()`` for any other query, a hop included."""
    statement = _statement(sql)
    if statement is None:
        return ()
    _text, tokens = statement
    outer = _outer_select(tokens)
    level = _read_level(tokens, outer[0]) if outer else None
    if level is None or level.keys is None:
        return ()
    return tuple(p for p, (expression, _alias) in enumerate(level.outputs)
                 if p not in level.keys and _counts_rows(expression))
