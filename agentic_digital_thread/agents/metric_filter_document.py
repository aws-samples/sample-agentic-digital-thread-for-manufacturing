# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``filter`` block of an ADR-0066 metric-definition document (ADR-0075).

``filter`` (direct only) is a CLOSED, AND-only list of row predicates
``{concept, op: eq|in|is_null|not_null, value|values}`` over CONCEPT ids —
never column names or free text. The shape + literal rule is
``metric_predicate_rule.check_predicate`` (the ONE home); this module
canonicalizes the block (clauses sorted by concept, ``in`` values sorted,
numbers as floats), converts it to/from ``Predicate`` tuples and renders it
for the steward's formula string.

It carries no error type of its own: ``metric_definition`` passes its
field-naming ``fail(field, problem)`` and id-shape ``ref(field, raw)``
callbacks, so every rejection surfaces as the SAME ``MetricDefinitionError``
naming ``filter[i].<field>``. Import-pure: no I/O, no LLM.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, NoReturn

from .metric_predicate_rule import (
    MAX_EXACT_INTEGER, MAX_FILTER_CLAUSES, Predicate, PredicateOp, check_predicate,
)

Fail = Callable[[str, str], NoReturn]
Ref = Callable[[str, Any], str]

PREDICATE_OPS: frozenset[str] = frozenset(o.value for o in PredicateOp)
_CLAUSE_KEYS: frozenset[str] = frozenset({"concept", "op", "value", "values", "column"})
_NULL_OPS: frozenset[str] = frozenset(
    {PredicateOp.IS_NULL.value, PredicateOp.NOT_NULL.value})
_LATEST = "latest"


def normalize_filter(raw: Any, aggregation: str, *, fail: Fail, ref: Ref,
                     ) -> list[dict]:
    """Canonicalize a ``filter`` block (ADR-0075 D1): clauses sorted by concept,
    at most one clause per concept, never with the ``latest`` aggregation."""
    if not isinstance(raw, (list, tuple)) or not raw:
        fail("filter", "must be a non-empty list of predicate clauses")
    if len(raw) > MAX_FILTER_CLAUSES:
        fail("filter", f"carries {len(raw)} clauses, over the "
             f"{MAX_FILTER_CLAUSES} cap")
    if aggregation == _LATEST:
        fail("filter", "is not valid with the 'latest' aggregation")
    clauses: list[dict] = []
    for index, clause in enumerate(raw):
        norm = _normalize_clause(clause, path=f"filter[{index}]", fail=fail, ref=ref)
        if any(c["concept"] == norm["concept"] for c in clauses):
            fail(f"filter[{index}].concept",
                 f"repeats concept {norm['concept']!r}; at most one clause "
                 "per concept")
        clauses.append(norm)
    return sorted(clauses, key=lambda c: c["concept"])


def _normalize_clause(clause: Any, *, path: str, fail: Fail, ref: Ref) -> dict:
    """Shape-check ONE clause and return its canonical form."""
    if not isinstance(clause, Mapping):
        fail(path, "must be a {concept, op, value|values} object")
    extra = sorted(str(k) for k in set(clause) - _CLAUSE_KEYS)
    if extra:
        fail(f"{path}.{extra[0]}", "is not a predicate-clause field (only "
             "'concept', 'op', 'value', 'values')")
    concept = ref(f"{path}.concept", clause.get("concept"))
    op = clause.get("op")
    if not isinstance(op, str) or op not in PREDICATE_OPS:
        fail(f"{path}.op", "must be one of the closed predicate operations "
             f"{sorted(PREDICATE_OPS)}")
    field, values = _clause_values(clause, op, path=path, fail=fail)
    problem = check_predicate(Predicate(concept, PredicateOp(op), values))
    if problem:
        fail(f"{path}.{field}", problem)
    out: dict[str, Any] = {"concept": concept, "op": op}
    column = clause.get("column")
    if column is not None:  # an optional exact column pin on the clause
        if not isinstance(column, str) or not column.strip():
            fail(f"{path}.column", "must be a non-empty column name")
        out["column"] = column.strip()
    if op == PredicateOp.EQ.value:
        out["value"] = values[0]
    elif op == PredicateOp.IN.value:
        out["values"] = sorted(values)
    return out


def _clause_values(clause: Mapping[str, Any], op: str, *, path: str,
                   fail: Fail) -> tuple[str, tuple]:
    """The op's value slot (``value`` for eq, ``values`` for in, none for the
    null ops) as a canonical tuple; the OTHER slot must be absent."""
    has_value, has_values = "value" in clause, "values" in clause
    if op in _NULL_OPS:
        if has_value or has_values:
            fail(f"{path}.{'value' if has_value else 'values'}",
                 f"is not valid on the {op!r} operation")
        return "op", ()
    if op == PredicateOp.EQ.value:
        if has_values or not has_value:
            fail(f"{path}.value",
                 "is required (and 'values' forbidden) on the 'eq' operation")
        return "value", (canonical_literal(clause["value"]),)
    if has_value or not isinstance(clause.get("values"), (list, tuple)):
        fail(f"{path}.values",
             "must be a list (and 'value' forbidden) on the 'in' operation")
    return "values", tuple(canonical_literal(v) for v in clause["values"])


def canonical_literal(value: Any) -> Any:
    """Exactly-representable integers canonicalize as floats (booleans are NOT
    numbers); everything else — including an out-of-range integer — passes
    through UNCHANGED for ``check_predicate`` to reject (total: never raises)."""
    if isinstance(value, int) and not isinstance(value, bool) \
            and abs(value) <= MAX_EXACT_INTEGER:
        return float(value)
    return value


def filter_from_document(clauses: Iterable[Mapping[str, Any]] | None,
                         ) -> tuple[Predicate, ...] | None:
    """Canonical ``filter`` clauses → ``Predicate`` tuple (``None`` if absent)."""
    if not clauses:
        return None
    out = []
    for clause in clauses:
        if "values" in clause:
            values = tuple(clause["values"])
        else:
            values = (clause["value"],) if "value" in clause else ()
        out.append(Predicate(clause["concept"], PredicateOp(clause["op"]), values,
                             column=clause.get("column")))
    return tuple(out)


def filter_to_document(clauses: Iterable[Predicate] | None) -> list[dict] | None:
    """``Predicate`` tuple → canonical ``filter`` clauses (``None`` if absent);
    the inverse of :func:`filter_from_document` over canonical input."""
    if not clauses:
        return None
    out: list[dict] = []
    for pred in clauses:
        clause: dict[str, Any] = {"concept": pred.concept, "op": pred.op.value}
        if getattr(pred, "column", None):
            clause["column"] = pred.column
        if pred.op is PredicateOp.EQ:
            clause["value"] = canonical_literal(pred.values[0])
        elif pred.op is PredicateOp.IN:
            clause["values"] = sorted(canonical_literal(v) for v in pred.values)
        out.append(clause)
    return sorted(out, key=lambda c: c["concept"])


def render_filter(clauses: Iterable[Mapping[str, Any]] | None) -> str:
    """`` where <concept> eq 'X' and <concept> in (...)`` or ``""``."""
    rendered = [_render_clause(clause) for clause in clauses or ()]
    return f" where {' and '.join(rendered)}" if rendered else ""


def _render_clause(clause: Mapping[str, Any]) -> str:
    head = f"{clause['concept']} {clause['op']}"
    if "value" in clause:
        return f"{head} {_render_literal(clause['value'])}"
    if "values" in clause:
        return f"{head} ({', '.join(_render_literal(v) for v in clause['values'])})"
    return head


def _render_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{value}'"
    return render_number(value)


def render_number(value: float) -> str:
    """Trimmed numeric literal (``2.0`` → ``2``) — shared with formula render."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)
