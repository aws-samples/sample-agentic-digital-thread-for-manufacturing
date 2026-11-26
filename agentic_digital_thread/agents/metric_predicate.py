# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Row-predicate SQL rendering for DIRECT metrics (ADR-0075 D2).

Pure: no graph read, no execution. The resolver resolves each clause's
concept to a column through the metric system's ``MAPS_TO`` binding and hands
the bound clauses here; this module composes the WHERE fragment EXCLUSIVELY
from validated identifiers and literals that re-pass the ONE literal rule in
``metric_layer`` (``check_predicate``) — so a hand-built clause carrying a
hostile literal fails HERE, before any SQL string exists (fail-closed twice).
A bound column renders through ``metric_binding.quote_column_identifier`` —
a dotted physical column is ONE quoted identifier (ADR-0076 D4). The fragment
is dialect-neutral across the two SQL backends (Postgres, Athena): ``"col" = 'X'``, ``"col" IN ('A', 'B')``, ``IS NULL``,
``IS NOT NULL``, joined by `` AND ``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .metric_binding import quote_column_identifier
from .metric_layer import (
    Predicate,
    PredicateLiteral,
    PredicateOp,
    check_predicate,
    check_predicate_literal,
)

# The only qualifiers a caller may prefix: none (scalar / co-located grouped)
# or the grouped joined shape's measure alias.
_QUALIFIERS = frozenset({"", "m."})


@dataclass(frozen=True)
class PredicateBinding:
    """One predicate clause with its concept bound to a measure-table column."""

    concept: str
    column: str
    op: PredicateOp
    values: tuple[PredicateLiteral, ...]


def render_literal(value: PredicateLiteral) -> str:
    """Render ONE literal; ``ValueError`` when it fails the literal rule.

    Strings are quoted verbatim — the rule already REJECTED every quote,
    backslash, semicolon and line break, so no escaping is needed or done."""
    problem = check_predicate_literal(value)
    if problem:
        raise ValueError(f"predicate literal {problem}")
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    raise ValueError("predicate literal must be a string, number, or boolean")


def _render_one(binding: PredicateBinding, qualifier: str) -> str:
    problem = check_predicate(Predicate(binding.concept, binding.op, binding.values))
    if problem:
        raise ValueError(f"predicate for concept {binding.concept!r}: {problem}")
    ident = f"{qualifier}{quote_column_identifier(binding.column)}"
    if binding.op is PredicateOp.IS_NULL:
        return f"{ident} IS NULL"
    if binding.op is PredicateOp.NOT_NULL:
        return f"{ident} IS NOT NULL"
    rendered = [render_literal(v) for v in binding.values]
    if binding.op is PredicateOp.EQ:
        return f"{ident} = {rendered[0]}"
    return f"{ident} IN ({', '.join(rendered)})"


def render_predicate_sql(
    bindings: Sequence[PredicateBinding], *, qualifier: str = ""
) -> str:
    """AND-join the bound clauses into ONE WHERE fragment (no ``WHERE``).

    Raises ``ValueError`` on an empty list, an unknown qualifier, an invalid
    column identifier, or any clause that fails the shape/literal rule."""
    if qualifier not in _QUALIFIERS:
        raise ValueError(f"unsupported predicate qualifier: {qualifier!r}")
    if not bindings:
        raise ValueError("no predicate bindings to render")
    return " AND ".join(_render_one(b, qualifier) for b in bindings)
