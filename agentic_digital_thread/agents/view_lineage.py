# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Prove that a view column is a direct copy of one base-table column. Pure.

Onboarding reads foreign and primary keys through views to the table each
view reads (a view schema carries no constraints of its own). A view column
that merely keeps a base column's NAME proves nothing: ``other AS pid``,
``pid + 0 AS pid`` or ``pid::bigint AS pid`` keep the name with other or
changed values, and a key read through one would be a false key in the
agent's prompt and in the graph.

:func:`pass_through_columns` reads the view's own definition as PostgreSQL
renders it (``pg_get_viewdef``) and proves a column only when its select-list
entry is a bare reference to the same-named column of the one base table, in
a query of the shape ``SELECT <list> FROM [ONLY] <table> [[AS] <alias>]
[WHERE ...]``. A view of any other shape, or text it cannot read, proves
nothing (fail closed). Generic; no dataset knowledge.

A projection can also change the NUMBER of rows: a set-returning function in
the select list (``generate_series``, ``unnest``, any user-defined ``RETURNS
SETOF`` function, or an operator backed by one) repeats each base row, so a
primary key copied unchanged beside it is no longer unique. PostgreSQL keeps
no catalog record of the built-in functions a view calls, so the reader does
not try to tell such calls apart: a view whose select list holds anything but
column references, renames (``col AS other``) and constants (``'x'::text``,
``NULL``) proves NO column, which keeps every proven view at one row per base
row.

PostgreSQL 16 renders a single-table view's columns bare (``pid``); earlier
releases qualify them with the relation name (``c.pid``). Both are read.
"""
from __future__ import annotations

from typing import Any

from .athena_scope import tokenize

#: Words that, at the top level of a view's query, mean its rows are not one
#: plain copy of each base row (or a shape this reader does not read).
_UNPROVEN_WORDS = frozenset({
    "DISTINCT", "UNION", "INTERSECT", "EXCEPT", "GROUP", "HAVING", "WINDOW",
    "LIMIT", "OFFSET", "FETCH", "JOIN", "ON", "USING", "LATERAL", "VALUES",
    "WITH", "ORDER", "TABLESAMPLE", "INTO", "FOR", "CROSS", "NATURAL",
})

#: Constant words a select-list entry may be (with an optional cast).
_CONSTANT_WORDS = frozenset({"NULL", "TRUE", "FALSE"})


def _is_name(token) -> bool:
    return token.kind in ("word", "identifier") and token.depth == 0


def _word(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _statement_tokens(view_def: str):
    """The view query's tokens without a trailing semicolon, or None when the
    text is uncertain, is not one SELECT statement, or uses a top-level word
    that changes its rows."""
    tokens, uncertain = tokenize(view_def)
    if uncertain:
        return None
    while tokens and tokens[-1].kind == "semicolon":
        tokens = tokens[:-1]
    if not tokens or _word(tokens[0]) != "SELECT":
        return None
    for token in tokens:
        if token.kind in ("semicolon", "bind"):
            return None
        if token.depth == 0 and _word(token) in _UNPROVEN_WORDS:
            return None
    return tokens


def _from_relation(tokens, base_schema: str, base_table: str) -> str | None:
    """The qualifier the select list may use (the alias, else the table
    name) when the FROM clause names exactly the base table; else None."""
    names = list(tokens)
    if names and _word(names[0]) == "ONLY":
        names = names[1:]
    if not names or not _is_name(names[0]):
        return None
    relation = [names[0].value]
    rest = names[1:]
    if len(rest) >= 2 and rest[0].text == "." and _is_name(rest[1]):
        relation.append(rest[1].value)
        rest = rest[2:]
    if relation[-1] != base_table or (len(relation) == 2 and relation[0] != base_schema):
        return None
    if rest and _word(rest[0]) == "AS":
        rest = rest[1:]
    if not rest:
        return base_table
    if len(rest) == 1 and _is_name(rest[0]) and _word(rest[0]) not in _UNPROVEN_WORDS:
        return rest[0].value
    return None


def _select_items(tokens) -> list[list] | None:
    """The select list split at top-level commas, or None when one is empty."""
    items: list[list] = [[]]
    for token in tokens:
        if token.depth == 0 and token.text == ",":
            items.append([])
        else:
            items[-1].append(token)
    return None if any(not item for item in items) else items


def _is_constant(core) -> bool:
    """A string or number constant, optionally cast to a type named by words
    (``'x'::text``, ``1``, ``NULL::integer``, ``'2026-01-01'::timestamp without
    time zone``)."""
    if not core or core[0].depth != 0:
        return False
    if core[0].kind not in ("literal", "number") and _word(core[0]) not in _CONSTANT_WORDS:
        return False
    rest = core[1:]
    if not rest:
        return True
    return (len(rest) >= 3 and rest[0].text == ":" and rest[1].text == ":"
            and all(t.kind == "word" and t.depth == 0 for t in rest[2:]))


def _row_preserving(item) -> bool:
    """True when a select-list entry can only yield one value per base row: a
    column reference or a constant, optionally renamed with ``AS``. Any call,
    operator, parenthesis or keyword expression is refused (it could hide a
    set-returning function)."""
    core = list(item)
    if len(core) >= 3 and _word(core[-2]) == "AS" and _is_name(core[-1]):
        core = core[:-2]
    if len(core) == 1 and _is_name(core[0]):
        return True
    if len(core) == 3 and _is_name(core[0]) and core[1].text == "." and _is_name(core[2]):
        return True
    return _is_constant(core)


def _copied_name(item, qualifier: str) -> str | None:
    """The column a select-list entry copies unchanged, or None: the entry is
    exactly ``name`` or ``<qualifier>.name`` (no expression, no ``AS``)."""
    if len(item) == 1 and _is_name(item[0]):
        return item[0].value
    if (len(item) == 3 and _is_name(item[0]) and item[1].text == "."
            and _is_name(item[2]) and item[0].value == qualifier):
        return item[2].value
    return None


def pass_through_columns(view_def: Any, view_columns: Any, base_schema: str,
                         base_table: str) -> set[str]:
    """The view columns proven to be direct copies of the same-named column of
    ``base_schema.base_table``.

    ``view_def`` is the view's ``pg_get_viewdef`` text and ``view_columns``
    its output column names in order. The select list must line up with
    ``view_columns`` one for one; a column is proven only when its entry is a
    bare reference to the base column of the same name, and only when every
    entry is a column reference, a rename or a constant (one row per base
    row). Anything else, or any view shape other than a plain filtered select
    from the one base table, proves nothing.
    """
    if not isinstance(view_def, str) or not isinstance(view_columns, list):
        return set()
    if not view_columns or not all(isinstance(c, str) and c for c in view_columns):
        return set()
    tokens = _statement_tokens(view_def)
    if tokens is None:
        return set()
    froms = [i for i, t in enumerate(tokens) if t.depth == 0 and _word(t) == "FROM"]
    if len(froms) != 1:
        return set()
    wheres = [i for i, t in enumerate(tokens)
              if t.depth == 0 and _word(t) == "WHERE" and i > froms[0]]
    end = wheres[0] if wheres else len(tokens)
    qualifier = _from_relation(tokens[froms[0] + 1:end], base_schema, base_table)
    items = _select_items(tokens[1:froms[0]])
    if qualifier is None or items is None or len(items) != len(view_columns):
        return set()
    if not all(_row_preserving(item) for item in items):
        return set()            # the view may not be one row per base row
    proven: set[str] = set()
    for item, column in zip(items, view_columns):
        if _copied_name(item, qualifier) == column:
            proven.add(column)
    return proven
