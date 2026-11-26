# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A COUNT/SUM/MIN/MAX across several states, each on its own time column.

A model writes a count across several lifecycle states as ONE query whose
WHERE is ``<common conjuncts> AND (B1 OR B2 OR ...)``: every branch ``Bi``
pins one value of the SAME column (``state = 'X'`` -- the GROUP BY key, or a
column the result does not otherwise need) and bounds a time column with
literals, each branch windowing that state on ITS OWN time column. The whole
query is over the per-query scan cap, and the single-query exact split cannot
plan it: with the time bounds under the OR there is no top-level bound to clip
a box to, so every partition box must read each state column's whole range.

Each branch ALONE, though, is an ordinary single-window COUNT/SUM/MIN/MAX the
splitter plans. So this module:

1. recognises the shape (``parse_or_branches``): exactly one top-level OR group
   among the WHERE's AND conjuncts, every branch an AND of a pin on one shared
   column (``=`` a literal) plus literal-only bounds and filters, the pins
   DISJOINT (no value pinned by two branches), and the select a decomposable
   COUNT/SUM/MIN/MAX shape (``athena_exact_shape``);
2. runs each branch as its own query ``<common> AND (Bi)`` through the exact
   split (``athena_exact_split``), pinned to ONE shared snapshot, and
3. combines the pieces of every branch with the same merge the single split
   uses (``athena_exact_merge``): the branches select disjoint rows, so a
   COUNT adds, a SUM adds, MIN/MAX keep the extreme, and a GROUP BY key that
   appears in two branches (possible only when the pinned column is not the
   key) combines exactly. The query's ORDER BY and LIMIT are applied once over
   the combined groups.

Anything else under an OR -- a branch pinning a different column, an
overlapping pin, a branch with a non-literal bound, a NOT, a nested OR -- is
not recognised, and the caller keeps the single-split behaviour (which refuses
it). No total is returned when any branch cannot be computed exactly: every
existing guarantee (one snapshot, no partial sum, the caps, the refusal with a
reason) holds across the combine.

Generic: no table, column or state names in code. Pure apart from the Athena
and Glue clients it threads to the per-branch splits.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import athena_exact_merge as merge
from . import athena_exact_shape as shape_mod
from .athena_scope import (Token, _matching_close, _split_boolean, _strip_outer,
                           _where_expression, relations, tokenize)
from .athena_exact_shape import _column_ref, _literal_day


@dataclass(frozen=True)
class OrBranches:
    """The recognised OR-of-branches query: the whole decomposable shape, the
    shared pinned column, and one rewritten SQL per branch."""
    shape: shape_mod.Shape
    pinned_column: str
    branch_sqls: tuple[str, ...]
    pins: tuple[str, ...]


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else token.value


def _pin_value(part: list[Token], column: str | None) -> tuple[str, str] | None:
    """``(column, string literal text)`` when ``part`` is ``<col> = '<string>'``
    at its base depth, else ``None``. Only a single-quoted STRING literal is a
    pin (a numeric/date/boolean literal does not give exact, type-safe
    equality). ``column`` restricts which column the pin must name (``None``
    accepts any)."""
    part = _strip_outer(part)
    base = min((t.depth for t in part), default=0)
    if any(t.depth == base and _upper(t) == "NOT" for t in part):
        return None
    eqs = [i for i, t in enumerate(part) if t.depth == base and t.kind == "operator"]
    if len(eqs) != 1 or part[eqs[0]].text != "=":
        return None
    i = eqs[0]
    for side, other in ((part[:i], part[i + 1:]), (part[i + 1:], part[:i])):
        ref = _column_ref(side)
        value = _string_literal(other)
        if ref is not None and value is not None:
            if column is not None and ref.lower() != column.lower():
                return None
            return ref.lower(), value
    return None


def _string_literal(tokens: list[Token]) -> str | None:
    """The text of a single single-quoted string literal (outer parens
    stripped), else ``None`` (a number, a column, DATE/TIMESTAMP '...' and the
    like are not accepted as a pin value)."""
    tokens = _strip_outer(tokens)
    if len(tokens) == 1 and tokens[0].kind == "literal":
        return tokens[0].text
    return None


def _bound_is_literal(part: list[Token]) -> bool:
    """Whether ``part`` is a literal-only comparison or BETWEEN on one column
    (the kind the single split can clip a box to): ``_literal_day`` reads the
    literal side, so a non-literal bound (a column, ``current_timestamp``, an
    arithmetic expression) is not accepted."""
    part = _strip_outer(part)
    base = min((t.depth for t in part), default=0)
    if any(t.depth == base and _upper(t) == "NOT" for t in part):
        return False
    # BETWEEN <lit> AND <lit>
    for i, t in enumerate(part):
        if t.depth == base and _upper(t) == "BETWEEN":
            and_at = next((j for j in range(i + 1, len(part))
                           if part[j].depth == base and _upper(part[j]) == "AND"), None)
            if and_at is None:
                return False
            return (_column_ref(part[:i]) is not None
                    and _literal_day(part[i + 1:and_at]) is not None
                    and _literal_day(part[and_at + 1:]) is not None)
    # <col> <op> <lit>  (or the reverse)
    for i, t in enumerate(part):
        if t.depth == base and t.kind == "operator":
            if t.text in {"!=", "<>"}:
                return False
            left, right = part[:i], part[i + 1:]
            return ((_column_ref(left) is not None and _literal_day(right) is not None)
                    or (_literal_day(left) is not None and _column_ref(right) is not None))
    # <col> IS [NOT] NULL is a filter, not a window: a literal-only filter.
    words = [_upper(t) for t in part if t.depth == base]
    if "IS" in words and _column_ref(part[:next(i for i, t in enumerate(part)
                                                if _upper(t) == "IS")]) is not None:
        return set(words) <= {"IS", "NOT", "NULL"} or words[words.index("IS"):] in (
            ["IS", "NULL"], ["IS", "NOT", "NULL"])
    return False


def _branch_pins(branch: list[Token], column: str | None) -> tuple[str, str] | None:
    """The disjoint-pin value of one OR branch: every top-level AND conjunct is
    either the ONE pin on the shared column or a literal-only bound/filter. A
    branch with a nested OR, a NOT, a non-literal bound, or a pin on a second
    column is rejected (``None``)."""
    conjuncts = _split_boolean(branch, "AND")
    pin = None
    for part in conjuncts:
        part = _strip_outer(part)
        if not part:
            return None
        if len(_split_boolean(part, "OR")) > 1:        # a nested OR
            return None
        value = _pin_value(part, column)
        if value is not None:
            if pin is not None and value[0] != pin[0]:  # a second pinned column
                return None
            if pin is not None:                         # two pins on the same column in one branch
                return None
            pin = value
            continue
        if not _bound_is_literal(part):
            return None
    return pin


def _single_top_or(where: list[Token]) -> tuple[list[list[Token]], list[list[Token]]] | None:
    """``(common conjuncts, OR branches)`` when the WHERE's top-level AND has
    exactly ONE conjunct that is a top-level OR group and no other conjunct
    hides an OR, else ``None``."""
    if not where:
        return None
    conjuncts = _split_boolean(where, "AND")
    or_groups, common = [], []
    for part in conjuncts:
        stripped = _strip_outer(part)
        branches = _split_boolean(stripped, "OR")
        if len(branches) > 1:
            or_groups.append(branches)
        else:
            if any(_upper(t) == "OR" for t in stripped):   # an OR this reader cannot split
                return None
            common.append(stripped)
    if len(or_groups) != 1:
        return None
    return common, or_groups[0]


#: Glue/Trino string column types a pinned column may have for the OR combine.
#: String equality in Athena is exact, so distinct string literals are disjoint
#: groups. A numeric, date, boolean or any other type is NOT accepted (1 and 01
#: are one integer; 1.5 and 1.50 one decimal), so the OR stays unsplit.
_STRING_TYPES = ("varchar", "char", "string")


def _is_string_type(col_type) -> bool:
    text = str(col_type or "").strip().lower()
    return text.startswith(_STRING_TYPES)


def parse_or_branches(sql: str, column_type=None) -> OrBranches | None:
    """The OR-of-branches form of ``sql``, or ``None`` when it is not that
    shape. A recognised shape is a decomposable COUNT/SUM/MIN/MAX (NOT a
    percentile -- digests are not kept across branches yet) whose WHERE is
    ``<common> AND (B1 OR B2 OR ...)`` with every branch pinning one shared
    column to a DISTINCT single-quoted STRING literal and bounding/filtering
    only with literals. ``column_type(name)`` returns the pinned column's Glue
    type; the combine is accepted only when it is a string type (string
    equality is exact, so distinct literals are disjoint). When no oracle is
    given, a string-type pinned column still cannot be proved, so the shape is
    declined (the caller provides the oracle)."""
    text = (sql or "").strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens:
        return None
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1:
        return None
    where = _where_expression(tokens, scans[0])
    split = _single_top_or(where)
    if split is None:
        return None
    common, branches = split
    parsed = _parse_branches(text, tokens, where, common, branches)
    if parsed is None:
        return None
    column, pins, branch_wheres = parsed
    # Disjointness is by the pinned column's TYPE, not source text: only a
    # string column with string-literal pins has exact equality.
    if column_type is None or not _is_string_type(column_type(column)):
        return None
    # The whole query must be a decomposable shape once an OR branch replaces
    # the OR group: parse one branch's rewrite to prove the SELECT/GROUP BY/
    # ORDER BY/LIMIT shape, and reuse it for every branch. A percentile is not
    # combinable across branches here, so refuse it.
    try:
        shape = shape_mod.parse_shape(branch_wheres[0])
    except shape_mod.ShapeRefusal:
        return None
    if shape.has_percentile or not any(o.kind == "agg" for o in shape.outputs):
        return None
    return OrBranches(shape, column, tuple(branch_wheres), tuple(pins))


def _parse_branches(text: str, tokens, where, common, branches):
    """``(pinned column, pins, per-branch rewritten SQL)`` when every branch
    pins ONE shared column to a DISTINCT string literal and bounds/filters only
    with literals, else ``None``."""
    column = None
    pins: list[str] = []
    branch_wheres: list[str] = []
    seen: set[str] = set()
    for branch in branches:
        pin = _branch_pins(branch, column)
        if pin is None:
            return None
        if column is None:
            column = pin[0]
        if pin[1] in seen:                              # an overlapping (non-disjoint) pin
            return None
        seen.add(pin[1])
        pins.append(pin[1])
        branch_wheres.append(_where_sql(text, tokens, where, common, branch))
    return column, pins, branch_wheres


def _where_sql(text: str, tokens: list[Token], where: list[Token],
               common: list[list[Token]], branch: list[Token]) -> str:
    """``text`` with its whole WHERE expression replaced by
    ``<common conjuncts> AND (<branch>)`` (or just the branch when there is no
    common conjunct). The SELECT, GROUP BY, ORDER BY and LIMIT are unchanged."""
    start = where[0].start
    end = where[-1].end
    common_text = " AND ".join(f"({_span(text, part)})" for part in common)
    branch_text = f"({_span(text, branch)})"
    predicate = f"{common_text} AND {branch_text}" if common_text else branch_text
    return f"{text[:start]}{predicate}{text[end:]}"


def _span(text: str, tokens: list[Token]) -> str:
    """The exact source of a contiguous token run, outer parens stripped."""
    tokens = _strip_outer(tokens)
    if not tokens:
        return ""
    return text[tokens[0].start:tokens[-1].end]


# --------------------------------------------------------------------------- #
# Combine
# --------------------------------------------------------------------------- #
def combine(branch_merges: list[merge.Merged], shape: shape_mod.Shape,
            *, max_rows: int) -> merge.Merged:
    """One result from the branches' merged rows. The branches select disjoint
    rows, so the branch rows are merged with the same per-group accumulation
    the single split uses (COUNT/SUM add, MIN/MAX keep the extreme), and the
    query's ORDER BY and LIMIT are applied once over the combined groups."""
    pieces = [merge.PieceRows(m.columns, m.types, m.rows) for m in branch_merges]
    return merge.merge_pieces(shape, pieces, max_rows=max_rows)
