# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Write a top-level ``col <> ''`` WHERE conjunct so the engine skips NULL pages.

Athena engine v3 folds ``col IS NOT NULL AND col <> ''`` into the one
``col <> ''`` filter, and with that filter it no longer skips the Parquet pages
where ``col`` is NULL: every other column the query reads is then read for
every row of the matching files. A query on a mostly-NULL column that also
reads wide columns scans many times what it needs. Measured on an analytics
lake (Iceberg, 2026-10-27): a week-long top-defect count with a source owner's
row exclusion (three wide string columns) scanned past the 5 GB per-query
limit, and 1.08 GB with ``col <> ''`` written as ``length(col) > 0``, returning
the same rows; ``col > ''`` and every other spelling of the exclusion scanned
past the limit too.

:func:`rewrite` writes each ``col <> ''`` (or ``col != ''``, ``'' <> col``,
``'' != col``) that is a whole top-level conjunct of a WHERE clause as
``(col IS NOT NULL AND length(col) > 0)``. In a WHERE clause's top-level AND
chain the two are the same filter: a row is kept only when every conjunct is
TRUE, and both are TRUE exactly when ``col`` is neither NULL nor ``''``. The
``IS NOT NULL`` is the part the engine pushes into the scan; ``length`` keeps
the empty string out without becoming a range filter the engine folds it into.

Only what is sure is written. The conjunct must follow the WHERE (or a
conjunct-separating AND, not a BETWEEN's) and end at an AND, at the end of the
clause or at the end of the query block; the WHERE must have no top-level OR;
the operand on each side must be bare (no operator binding tighter); and the
reference must be tied by SQL scope to one base-table column whose catalogued
type is a string (``varchar`` or ``string``; ``char`` pads, so it is left as
written), the way ``sql_time_hygiene`` ties a reference
(:func:`sql_time_hygiene._resolved_type`). Anything else (an OR, NOT,
parentheses around the comparison, a function of the column, HAVING, ON,
CASE, a derived table or CTE, another database, an unknown or ambiguous
column, uncertain SQL) is left exactly as written. Writing is idempotent: the
written conjunct is parenthesized, so it is never a candidate again. A
comment inside a written comparison (``col /* c */ <> ''``) is dropped with
it; comments anywhere else stay.

The written form is for the engine only. The connector's result keeps
``_adt_executed_query`` as the query it would have run without writing
anything (the query it was given, with its row cap), so the payload, every
reader of that field (the NULL-unsafe note, the executed-filters line, the
figure and key checks) and every byte budget see what they saw before. The
statement the engine received goes out of band (``athena_tools.
engine_statements``), kept under its own tool call's id, to that call's
evidence card, which shows it as the executed query. No reader has to tell a
written conjunct from one the agent wrote in the same form.
"""
from __future__ import annotations

import re
from typing import Callable

from .athena_scope import _cte_bindings, tokenize
from .sql_time_hygiene import (
    _bare_after,
    _bare_before,
    _clause_of,
    _column_ending_at,
    _column_starting_at,
    _columns_lookup,
    _resolved_type,
    _select_for,
    _upper,
)

#: ``table_columns(name parts) -> {column: type} | None`` (as in ``sql_time_hygiene``).
TableColumns = Callable[[tuple], "dict | None"]
_NOT_EQUAL = {"<>", "!="}
_EMPTY = "''"
#: Words that end a WHERE clause at its own nesting level.
_CLAUSE_END = {"GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH", "WINDOW", "QUALIFY",
               "UNION", "INTERSECT", "EXCEPT"}
#: A cheap pre-check: an empty string literal beside a not-equal operator.
_MAYBE = re.compile(r"(?:<>|!=)\s*''|''\s*(?:<>|!=)")


def has_candidates(sql: str) -> bool:
    """Could ``sql`` hold a conjunct to write at all? ``False`` lets the caller
    skip the catalog read."""
    return isinstance(sql, str) and bool(_MAYBE.search(sql))


def _is_string_type(dtype: str) -> bool:
    dtype = (dtype or "").strip().lower()
    return dtype == "string" or dtype == "varchar" or dtype.startswith("varchar(")


def _comparisons(tokens: list) -> list:
    """Every syntactic ``col <> ''`` / ``'' <> col``: ``(name parts, column
    token index, first column token, last column token, span start, span end)``
    where the span is the whole comparison, end exclusive."""
    found = []
    for i, token in enumerate(tokens):
        if token.kind != "operator" or token.text not in _NOT_EQUAL:
            continue
        if i + 1 < len(tokens) and tokens[i + 1].kind == "literal" and tokens[i + 1].text == _EMPTY:
            column = _column_ending_at(tokens, i - 1)
            if column and _bare_before(tokens, column[1]) and _bare_after(tokens, i + 2):
                found.append((column[0], column[1], column[1], i - 1, column[1], i + 2))
        if i >= 1 and tokens[i - 1].kind == "literal" and tokens[i - 1].text == _EMPTY:
            column = _column_starting_at(tokens, i + 1)
            if column and _bare_before(tokens, i - 1) and _bare_after(tokens, column[1]):
                found.append((column[0], i + 1, i + 1, column[1] - 1, i - 1, column[1]))
    return found


def _follows_conjunct_boundary(tokens: list, where: int, start: int, depth: int) -> bool:
    """The token before ``tokens[start]`` is the WHERE itself, or an AND that
    separates conjuncts (a BETWEEN's AND belongs to its BETWEEN), with no
    top-level OR between the WHERE and ``start``."""
    pending_between, separator = 0, False
    for k in range(where + 1, start):
        if tokens[k].depth != depth:
            continue
        word = _upper(tokens[k])
        if word == "OR":
            return False
        if word == "BETWEEN":
            pending_between += 1
        if word == "AND":
            separator = not pending_between
            pending_between = max(0, pending_between - 1)
        else:
            separator = False
    return start - 1 == where or separator


def _top_level_where_conjunct(tokens: list, start: int, end: int) -> bool:
    """``tokens[start:end]`` is a whole conjunct of its block's WHERE clause,
    in an AND chain with no top-level OR."""
    depth = tokens[start].depth
    if any(tokens[k].depth != depth for k in range(start, end)):
        return False
    select = _select_for(tokens, start)
    if select is None or tokens[select].depth != depth \
            or _clause_of(tokens, select, start) != "WHERE":
        return False
    where = next((k for k in range(start - 1, select, -1)
                  if tokens[k].depth == depth and _upper(tokens[k]) == "WHERE"), None)
    if where is None or not _follows_conjunct_boundary(tokens, where, start, depth):
        return False
    # The token after: an AND, the clause's end, or the block's end.
    if end < len(tokens):
        nxt = tokens[end]
        if nxt.depth == depth and nxt.kind != "semicolon" \
                and _upper(nxt) not in ({"AND"} | _CLAUSE_END):
            return False
    # No top-level OR after it in the same WHERE.
    for k in range(end, len(tokens)):
        if tokens[k].depth < depth or tokens[k].kind == "semicolon":
            break
        if tokens[k].depth == depth and _upper(tokens[k]) in _CLAUSE_END:
            break
        if tokens[k].depth == depth and _upper(tokens[k]) == "OR":
            return False
    return True


def rewrite(sql: str, table_columns: TableColumns) -> str:
    """``sql`` with each sure top-level ``<string column> <> ''`` WHERE
    conjunct written as ``(col IS NOT NULL AND length(col) > 0)``; ``sql``
    itself when there is none or anything is uncertain."""
    if not has_candidates(sql):
        return sql
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return sql
    bindings, cte_uncertain = _cte_bindings(tokens)
    if cte_uncertain:
        return sql
    ctes = {binding.name.lower() for binding in bindings}
    columns = _columns_lookup(table_columns)
    edits = []
    for parts, index, first, last, start, end in _comparisons(tokens):
        if not _top_level_where_conjunct(tokens, start, end):
            continue
        if not _is_string_type(_resolved_type(tokens, index, parts, ctes, columns) or ""):
            continue
        column = sql[tokens[first].start:tokens[last].end]
        edits.append((tokens[start].start, tokens[end - 1].end,
                      f"({column} IS NOT NULL AND length({column}) > 0)"))
    for begin, finish, text in sorted(edits, reverse=True):
        sql = sql[:begin] + text + sql[finish:]
    return sql
