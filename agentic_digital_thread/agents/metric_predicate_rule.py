# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Row-predicate TYPES + the ONE shape/literal rule for DIRECT metrics (ADR-0075).

A closed, AND-only predicate vocabulary over CONCEPTS (never column names):
the resolver binds each clause's concept to a column through the metric
system's MAPS_TO binding. ``metric_layer`` re-exports every public name here
(it is the metric-type surface callers import from); the document parser, the
layer validator and the SQL renderer all call THIS rule, and the renderer
re-runs it before it emits a byte.

Every check is TOTAL over arbitrary JSON-ish input: a malformed literal (a
nested list/object, a non-finite or non-representable number) yields a
problem string, never a raw ``TypeError``/``OverflowError``.

Import-pure (ADR 0001): no I/O, no LLM, no ``eval``/``exec``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Union


class PredicateOp(str, Enum):
    EQ = "eq"
    IN = "in"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"


PredicateLiteral = Union[str, int, float, bool]


@dataclass(frozen=True)
class Predicate:
    concept: str
    op: PredicateOp
    values: tuple[PredicateLiteral, ...] = ()
    # an optional exact ``{concept, column}`` pin, the column the
    # predicate concept binds to on the measure table, verified against live
    # MAPS_TO at resolve time (fail-closed). None keeps today's single/authority
    # binding behaviour byte-identical.
    column: str | None = None


MAX_FILTER_CLAUSES = 8
MAX_IN_VALUES = 32
MAX_PREDICATE_LITERAL_LEN = 64
#: Integers beyond ±2^53 are not exactly representable as the canonical float
#: form (and ``float(10**400)`` overflows) — REJECTED, never rounded.
MAX_EXACT_INTEGER = 2 ** 53
# The ``athena_tools._safe_sql_literal`` rejection register: REJECTED, never
# escaped or truncated.
_FORBIDDEN_LITERAL_CHARS = ("'", '"', ";", "\\", "\n", "\r")


def check_predicate_literal(value: object) -> str | None:
    """Return a problem string for an unacceptable literal, ``None`` if ok.

    Strings: 1..64 printable-ASCII chars with none of the forbidden SQL chars.
    Numbers: finite floats and integers within ±2^53 (a bool is NOT a number).
    Booleans are shape-checked by ``check_predicate`` (``eq`` only). Anything
    else (list, dict, None, ...) is rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if abs(value) <= MAX_EXACT_INTEGER:
            return None
        return "must be a number within +/-2^53"
    if isinstance(value, float):
        return None if math.isfinite(value) else "must be a finite number"
    if not isinstance(value, str):
        return "must be a string, number, or boolean"
    if not 1 <= len(value) <= MAX_PREDICATE_LITERAL_LEN:
        return f"must be 1..{MAX_PREDICATE_LITERAL_LEN} characters"
    if any(ch in value for ch in _FORBIDDEN_LITERAL_CHARS):
        return "must not contain quotes, semicolons, backslashes or line breaks"
    if any(not 0x20 <= ord(ch) <= 0x7E for ch in value):
        return "must be printable ASCII"
    return None


def _literal_kind(value: PredicateLiteral) -> str:
    if isinstance(value, bool):
        return "bool"
    return "str" if isinstance(value, str) else "number"


def _check_in_homogeneous(values: tuple) -> str | None:
    """Runs ONLY after every value passed the literal rule (so all hashable)."""
    kinds = {_literal_kind(v) for v in values}
    if "bool" in kinds:
        return "'in' values must not be booleans"
    if len(kinds) != 1:
        return "'in' values must be all strings or all numbers"
    if len(set(values)) != len(values):
        return "'in' values must not repeat"
    return None


def _check_arity(op: PredicateOp, values: tuple) -> str | None:
    if op in (PredicateOp.IS_NULL, PredicateOp.NOT_NULL):
        return f"{op.value!r} takes no value" if values else None
    if op is PredicateOp.EQ and len(values) != 1:
        return "'eq' takes exactly one value"
    if op is PredicateOp.IN and not 1 <= len(values) <= MAX_IN_VALUES:
        return f"'in' needs 1..{MAX_IN_VALUES} values"
    return None


def check_predicate(pred: object) -> str | None:
    """Shape + literal rule for ONE clause; ``None`` when acceptable. Order:
    arity → per-literal type/content → ``in`` homogeneity/uniqueness."""
    if not isinstance(pred, Predicate):
        return "must be a Predicate"
    if not isinstance(pred.concept, str) or not pred.concept.strip():
        return "concept must be a non-empty concept id"
    if not isinstance(pred.op, PredicateOp):
        return "op must be one of " + ", ".join(o.value for o in PredicateOp)
    if not isinstance(pred.values, tuple):
        return "values must be a tuple"
    problem = _check_arity(pred.op, pred.values)
    if problem:
        return problem
    for value in pred.values:
        problem = check_predicate_literal(value)
        if problem:
            return problem
    if pred.op is PredicateOp.IN:
        return _check_in_homogeneous(pred.values)
    return None


def check_filter(clauses: object) -> str | None:
    """Rule for a whole ``filter``: ``None`` (absent) or a non-empty tuple of
    ≤ ``MAX_FILTER_CLAUSES`` valid clauses with DISTINCT concepts."""
    if clauses is None:
        return None
    if not isinstance(clauses, tuple) or not clauses:
        return "filter must be a non-empty tuple of predicates"
    if len(clauses) > MAX_FILTER_CLAUSES:
        return f"filter allows at most {MAX_FILTER_CLAUSES} clauses"
    for index, clause in enumerate(clauses):
        problem = check_predicate(clause)
        if problem:
            return f"filter[{index}]: {problem}"
    concepts = [c.concept for c in clauses]
    if len(set(concepts)) != len(concepts):
        return "filter allows at most one clause per concept"
    return None
