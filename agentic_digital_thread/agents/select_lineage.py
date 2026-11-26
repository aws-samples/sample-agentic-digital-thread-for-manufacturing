# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which ``table.column`` each output column of a plain SELECT comes from.

A result column's name says nothing about where it came from: two joined
tables can both carry ``item_id``, and ``SELECT x.other_id AS item_id`` hands
back a column named like a key it is not. The row-id note
(``row_id_names``) states a fact about one specific relation's column, so it
needs that column's proven source, read from the SELECT list.

Readable shape: ONE top-level ``SELECT [DISTINCT|ALL] items FROM ...`` (no
WITH, no set operation), its FROM items read by the aggregate check's own
reader (``aggregate_guard._sources``: relations of the agent's schema with
their aliases; derived tables, CTEs, functions and other schemas' relations
carry no relation). A select item is a source only when it is a bare column
reference, ``col`` or ``q.col`` with an optional ``AS`` alias:

- ``q.col`` resolves through the FROM item named ``q`` (its alias, or the
  relation's own name when it has none);
- ``col`` resolves to the ONE FROM item whose known columns include it, and
  only when every FROM item is a relation whose columns are known (a derived
  table or an unknown relation could carry it too).

Anything else (an expression, a cast, a function, a literal) is an output with
no source. A ``*`` anywhere in the list, a WITH, a set operation, unreadable
SQL, or a statement with no FROM gives None: the whole projection is
unsupported. Pure, never raises; generic, no dataset knowledge.
"""
from __future__ import annotations

from typing import Mapping


def _column_ref(tokens) -> tuple[str, str] | None:
    """``(qualifier or "", column)`` for a bare ``col`` / ``q.col`` written
    with plain words, else None (a quoted identifier is not read)."""
    from .aggregate_guard import _norm
    if len(tokens) == 1 and tokens[0].kind == "word":
        return "", _norm(tokens[0])
    if (len(tokens) == 3 and tokens[1].text == "."
            and tokens[0].kind == "word" and tokens[2].kind == "word"):
        return _norm(tokens[0]), _norm(tokens[2])
    return None


def _is_star(part) -> bool:
    """``*`` or ``q.*``: its output columns cannot be lined up with the result."""
    return ([t.text for t in part] == ["*"]
            or (len(part) == 3 and part[1].text == "." and part[2].text == "*"))


def _split_alias(part) -> tuple[list, str | None]:
    """``(expression tokens, output alias or None)`` for one select item."""
    from .aggregate_guard import _is_name, _norm, _word
    if len(part) >= 3 and _word(part[-2]) == "AS" and _is_name(part[-1]):
        return part[:-2], _norm(part[-1])
    if (len(part) in (2, 4) and part[-1].kind == "word" and part[-2].text != "."
            and _column_ref(part[:-1]) is not None):
        return part[:-1], _norm(part[-1])
    return part, None


def _resolve(ref: tuple[str, str], sources: Mapping, columns: Mapping[str, set]):
    """The ``(relation, column)`` a column reference reads, or None."""
    qualifier, column = ref
    if qualifier:
        source = sources.get(qualifier)
        relation = getattr(source, "relation", None)
        return (relation, column) if relation and column in columns.get(relation, ()) else None
    found = []
    for source in sources.values():
        relation = getattr(source, "relation", None)
        if not relation or relation not in columns:
            return None                    # could carry the column: unprovable
        if column in columns[relation]:
            found.append(relation)
    return (found[0], column) if len(found) == 1 else None


def _select_and_sources(sql: str, schema: str):
    """``(select-list tokens, {alias: Source})`` for the readable shape, or None."""
    from .aggregate_guard import (_clause, _clause_spans, _sources, _tokens,
                                  _without_quantifier, _word)
    tokens = _tokens(sql)
    if not tokens or _word(tokens[0]) != "SELECT":
        return None
    spans = _clause_spans(tokens, 0)
    if spans is None:
        return None
    sources = _sources(_clause(tokens, spans, "from")[1:], sql, schema, {})
    if not sources:
        return None
    return _without_quantifier(tokens[1:spans["from"][0]]), sources


def output_sources(sql: str, columns: Mapping[str, set],
                   schema: str = "") -> list[tuple[str | None, tuple[str, str] | None]] | None:
    """``[(output name or None, (relation, column) or None)]`` per select
    item in order, or None when the projection cannot be read.

    ``columns`` is ``{relation: {column, ...}}`` (lowercased) of the relations
    the agent was given; ``schema`` the agent's own schema, so a relation
    qualified with it still counts as one of them."""
    try:
        from .aggregate_guard import _split
        found = _select_and_sources(sql, schema)
        if found is None:
            return None
        select_tokens, sources = found
        out: list = []
        for part in _split(select_tokens):
            if not part or _is_star(part):
                return None
            expression, alias = _split_alias(part)
            ref = _column_ref(expression)
            source = _resolve(ref, sources, columns) if ref else None
            out.append((alias or (ref[1] if ref else None), source))
        return out
    except Exception:  # noqa: BLE001 - an unreadable projection proves nothing
        return None
