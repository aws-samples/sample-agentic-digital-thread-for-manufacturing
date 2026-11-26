# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One reading of a column a rule spells, when the column may be dotted.

A flattened struct column (``billing.customer.id``) or a tag path
(``AreaA.Line1.Line1_Tag``) is ONE physical column whose name has dots. A
rule can spell it as the column, ``table.column``, ``db.table.column`` or a
trailing part of it (``customer.id``). The resolve step's rule merge
(``identity_affinity``) and provision's rule binding (``onboard``) both read
a spelling with :func:`read_column`, so they find the same column, or both
find none or more than one. Pure, stdlib only.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

#: No dotted column is among the columns a spelling names: the caller keeps
#: its own reading of the spelling (the last dotted segment).
NONE = "none"
#: The spelling names exactly one column, and it is dotted.
ONE = "one"
#: The spelling names a dotted column and at least one other column.
AMBIGUOUS = "ambiguous"


class Reading(NamedTuple):
    """What :func:`read_column` made of a spelling."""

    status: str
    table: str = ""
    column: str = ""
    #: Every ``(table, column)`` the spelling names.
    candidates: frozenset = frozenset()

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


def _named_by(spelling: str, table: str, column: str) -> bool:
    """Whether ``spelling`` names ``column`` of ``table``: the column itself,
    ``table.column``, ``<qualifier>.table.column``, or a trailing part of the
    column on a dotted boundary (``customer.id`` of ``billing.customer.id``),
    each with or without the table in front."""
    spellings = {spelling}
    if spelling.startswith(table + "."):
        spellings.add(spelling[len(table) + 1:])
    if f".{table}." in spelling:
        spellings.add(spelling.rsplit(f".{table}.", 1)[1])
    return any(column == s or column.endswith("." + s) for s in spellings)


def candidates(spelling: str, columns: Iterable[tuple[str, str]],
               table_hint: str = "") -> frozenset:
    """Every ``(table, column)`` of ``columns`` that ``spelling`` names
    (:func:`_named_by`). A ``table_hint`` keeps only that table's columns (a
    qualified hint ``db.table`` keeps ``table``'s)."""
    spelling, hint = str(spelling or "").strip(), str(table_hint or "").strip()
    if not spelling:
        return frozenset()
    return frozenset(
        (table, column) for table, column in columns
        if table and column
        and (not hint or hint == table or hint.endswith("." + table))
        and _named_by(spelling, table, column))


def read_column(spelling: str, columns: Iterable[tuple[str, str]],
                table_hint: str = "") -> Reading:
    """The dotted column ``spelling`` names among ``columns``
    (``(table, column)`` pairs, dotted or not): :data:`ONE` with it when it
    is the only column the spelling names; :data:`AMBIGUOUS` when the
    spelling names a dotted column and any other; else :data:`NONE`."""
    found = candidates(spelling, columns, table_hint)
    if not any("." in column for _table, column in found):
        return Reading(NONE, candidates=found)
    if len(found) > 1:
        return Reading(AMBIGUOUS, candidates=found)
    ((table, column),) = found
    return Reading(ONE, table, column, found)
