# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Merge the rows of an exact-total split's pieces into one result.

Each piece is the same decomposable query (``athena_exact_shape``) over a
disjoint part of the table, so every source row is counted by exactly one
piece. Rows are merged per GROUP BY key, compared as the engine's GROUP BY
compares them (by value of the column's declared type: ``1.50`` and ``1.5``
are one DECIMAL key, ``0.0`` and ``-0.0`` one DOUBLE key, every NaN one key, a
NULL key one key); a key column whose type the merge cannot group exactly is
refused:

* COUNT adds; SUM adds the non-NULL piece sums and is NULL only when every
  piece's sum was NULL (SQL: a SUM over no non-NULL value is NULL);
* MIN and MAX keep the extreme of the non-NULL piece values;
* integers and decimals stay exact, within the result column's declared
  range: a merged integer outside it, or a decimal past its declared
  precision, is refused (one query raises a numeric overflow there); decimal
  arithmetic runs in a local context wide enough for ``decimal(38, s)`` that
  refuses any rounding; a floating-point SUM is added but reported as not
  exact;
* where the engine's floating-point ordering is not certain, the merge
  refuses: MIN or MAX over a NaN, MIN or MAX tied on ``0.0`` and ``-0.0``,
  ORDER BY a column holding NaN;
* the query's ORDER BY and LIMIT are applied after the merge, once over
  every group (NULLS LAST unless the query says otherwise, as the engine
  orders); ``groups`` says how many there were before them.

Values come back as the engine's own text (Athena returns every cell as a
string; a merged key is the text its first piece printed), so a merged row
reads like a row of one query. Pure and offline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, Inexact, InvalidOperation, Overflow, Rounded

from .athena_exact_shape import Shape

_INTEGER_TYPES = ("tinyint", "smallint", "integer", "int", "bigint")
_FLOAT_TYPES = ("double", "real", "float")
_TEXT_ORDERED = ("varchar", "char", "date", "timestamp", "boolean")
#: Bits of each integer result type (one query raises outside its range).
_INTEGER_BITS = {"tinyint": 8, "smallint": 16, "integer": 32, "int": 32, "bigint": 64}
#: The engine's largest decimal precision (a decimal SUM is ``decimal(38, s)``).
DECIMAL_MAX_PRECISION = 38
#: Decimal arithmetic: room for any sum of ``decimal(38, s)`` pieces, and any
#: rounding raises rather than changing the total.
_DECIMAL = Context(prec=80, rounding=ROUND_HALF_EVEN,
                   traps=[InvalidOperation, Overflow, Inexact, Rounded])
#: The one GROUP BY key every floating-point NaN shares.
_NAN_KEY = ("NaN",)


class MergeError(ValueError):
    """The pieces cannot be merged into one exact result (``reason`` is a code)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PieceRows:
    columns: list[str]
    types: list[str]
    rows: list[list]


@dataclass(frozen=True)
class Merged:
    columns: list[str]
    types: list[str]
    rows: list[list]
    exact: bool
    cut: bool
    #: Every group the pieces hold, before the query's LIMIT and the row cap.
    groups: int = 0


def _family(data_type: str) -> str:
    text = (data_type or "").strip().lower()
    if text.startswith(_INTEGER_TYPES):
        return "integer"
    if text.startswith("decimal"):
        return "decimal"
    if text.startswith(_FLOAT_TYPES):
        return "float"
    if text.startswith("timestamp") and "with time zone" in text:
        return "zoned"
    if text.startswith(_TEXT_ORDERED):
        return "text"
    return "other"


def _number(text, family: str):
    try:
        if family == "integer":
            return int(text)
        if family == "decimal":
            value = Decimal(text)
            if not value.is_finite():
                raise ValueError("not a finite decimal")
            return value
        return float(text)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise MergeError("unmergeable_value", "a piece returned a value that is not a number") from exc


def _sort_key(text, family: str):
    """A comparable key for MIN/MAX/ORDER BY on one non-NULL cell."""
    if family in {"integer", "decimal", "float"}:
        return _number(text, family)
    if family == "text":
        return str(text)
    if family == "zoned":
        stamp, _, zone = str(text).rpartition(" ")
        return (zone, stamp)
    raise MergeError("unmergeable_type", "a selected column has a type the split cannot order")


def _group_key(text, family: str):
    """The value one GROUP BY cell is grouped by, as the engine compares it."""
    if text is None:
        return None
    if family in {"integer", "decimal"}:
        return _number(text, family)
    if family == "float":
        value = _number(text, family)
        return _NAN_KEY if math.isnan(value) else (0.0 if value == 0 else value)
    if family == "text":
        return str(text)
    raise MergeError("unmergeable_type", "a GROUP BY column has a type the split cannot group exactly")


def _decimal_type(data_type: str) -> tuple[int, int | None]:
    """``(precision, scale)`` of ``decimal(p, s)``; an undeclared scale is None."""
    inside = data_type[data_type.find("(") + 1:data_type.rfind(")")] if "(" in data_type else ""
    parts = [p.strip() for p in inside.split(",")]
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0]), int(parts[1])
    return DECIMAL_MAX_PRECISION, None


def _overflow() -> MergeError:
    return MergeError("numeric_overflow", "the merged total is outside its column's type, so one "
                      "query would have failed with a numeric overflow")


def _render_decimal(total: Decimal, data_type: str) -> str:
    precision, scale = _decimal_type(data_type)
    try:
        if scale is not None:
            total = total.quantize(Decimal(1).scaleb(-scale), context=_DECIMAL)
    except DecimalException as exc:
        raise MergeError("unmergeable_value", "a piece returned a decimal off its column's scale") from exc
    digits = scale if scale is not None else max(0, -total.as_tuple().exponent)
    if total.copy_abs() >= Decimal(1).scaleb(precision - digits, context=_DECIMAL):
        raise _overflow()
    return format(total, "f")


def _render_integer(total: int, data_type: str) -> str:
    bits = _INTEGER_BITS.get((data_type or "").strip().lower().split("(")[0], 64)
    if not -2 ** (bits - 1) <= total < 2 ** (bits - 1):
        raise _overflow()
    return str(total)


def _render_sum(total, family: str, data_type: str) -> str:
    if family == "decimal":
        return _render_decimal(total, data_type)
    return _render_integer(total, data_type) if family == "integer" else repr(float(total))


class _Accumulator:
    """One output column's running value across pieces."""

    def __init__(self, func: str, data_type: str):
        self.func, self.type, self.family = func, data_type, _family(data_type)
        if func == "sum" and self.family not in {"integer", "decimal", "float"}:
            raise MergeError("unmergeable_type", "a SUM column is not numeric")
        if func == "count" and self.family != "integer":
            raise MergeError("unmergeable_type", "a COUNT column is not an integer")
        self.value = None
        self.text = None
        self.tied = False

    def add(self, cell) -> None:
        if cell is None:
            return
        if self.func in {"count", "sum"}:
            number = _number(cell, "integer" if self.func == "count" else self.family)
            if self.value is None:
                self.value = number
            elif self.family == "decimal" and self.func == "sum":
                try:
                    self.value = _DECIMAL.add(self.value, number)
                except DecimalException as exc:
                    raise _overflow() from exc
            else:
                self.value = self.value + number
            return
        self._extreme(cell)

    def _extreme(self, cell) -> None:
        key = _sort_key(cell, self.family)
        if self.family == "float" and math.isnan(key):
            raise MergeError("float_nan", "a MIN or MAX over floating point met NaN, whose order "
                             "the split does not reproduce")
        if self.family == "zoned" and self.value is not None and key[0] != self.value[0]:
            raise MergeError("unmergeable_value", "MIN or MAX values carry different time zones")
        better = self.value is None or (key < self.value if self.func == "min" else key > self.value)
        if better:
            self.value, self.text, self.tied = key, cell, False
        elif self.family == "float" and key == self.value and cell != self.text:
            self.tied = True                    # 0.0 and -0.0: which one prints is not certain

    def result(self):
        if self.func == "count":
            return _render_integer(self.value or 0, "bigint")
        if self.func == "sum":
            return None if self.value is None else _render_sum(self.value, self.family, self.type)
        if self.tied:
            raise MergeError("float_signed_zero", "MIN or MAX is tied on 0.0 and -0.0, and which "
                             "one the engine prints is not certain")
        return self.text


def _check_pieces(shape: Shape, pieces: list[PieceRows]) -> tuple[list[str], list[str]]:
    if not pieces:
        raise MergeError("no_pieces", "no piece returned a result")
    columns, types = list(pieces[0].columns), [str(t or "") for t in pieces[0].types]
    width = len(shape.outputs)
    if len(columns) != width or len(types) != width:
        raise MergeError("unmergeable_shape", "a piece returned a different number of columns")
    for piece in pieces[1:]:
        if list(piece.columns) != columns or [str(t or "") for t in piece.types] != types:
            raise MergeError("unmergeable_shape", "pieces returned different columns or types")
    return columns, types


def _merge_groups(shape: Shape, pieces: list[PieceRows], types: list[str]) -> dict:
    """``{key value: (key texts, accumulators)}`` over every piece's rows."""
    keys = [i for i, o in enumerate(shape.outputs) if o.kind == "key"]
    families = [_family(types[i]) for i in keys]
    aggs = [(i, o.func) for i, o in enumerate(shape.outputs) if o.kind == "agg"]
    groups: dict[tuple, tuple[tuple, dict[int, _Accumulator]]] = {}
    for piece in pieces:
        for row in piece.rows:
            if not isinstance(row, (list, tuple)) or len(row) != len(shape.outputs):
                raise MergeError("unmergeable_shape", "a piece returned a malformed row")
            key = tuple(_group_key(row[i], f) for i, f in zip(keys, families))
            found = groups.get(key)
            if found is None:
                found = groups[key] = (tuple(row[i] for i in keys),
                                       {i: _Accumulator(func, types[i]) for i, func in aggs})
            for i, _func in aggs:
                found[1][i].add(row[i])
    if not shape.grouped and not groups:
        groups[()] = ((), {i: _Accumulator(func, types[i]) for i, func in aggs})
    return groups


def _ordered(shape: Shape, rows: list[list], types: list[str]) -> list[list]:
    families = [_family(t) for t in types]
    for item in reversed(shape.order):
        nulls = [r for r in rows if r[item.column] is None]
        present = [r for r in rows if r[item.column] is not None]
        if families[item.column] == "float" and any(
                math.isnan(_sort_key(r[item.column], "float")) for r in present):
            raise MergeError("float_nan", "an ORDER BY column holds NaN, whose order the split "
                             "does not reproduce")
        present.sort(key=lambda r: _sort_key(r[item.column], families[item.column]),
                     reverse=item.descending)
        rows = nulls + present if item.nulls_first else present + nulls
    return rows


def merge_pieces(shape: Shape, pieces: list[PieceRows], *, max_rows: int) -> Merged:
    """One result from the pieces' rows, ordered and limited like the query."""
    columns, types = _check_pieces(shape, pieces)
    groups = _merge_groups(shape, pieces, types)
    keys = [i for i, o in enumerate(shape.outputs) if o.kind == "key"]
    rows: list[list] = []
    for texts, accs in groups.values():
        row: list = [None] * len(shape.outputs)
        for position, index in enumerate(keys):
            row[index] = texts[position]
        for index, acc in accs.items():
            row[index] = acc.result()
        rows.append(row)
    rows = _ordered(shape, rows, types)
    groups = len(rows)
    if shape.limit is not None:
        rows = rows[:shape.limit]
    cut = len(rows) > max_rows
    exact = not any(o.func == "sum" and _family(types[i]) == "float"
                    for i, o in enumerate(shape.outputs))
    return Merged(columns, types, rows[:max_rows], exact, cut, groups)
