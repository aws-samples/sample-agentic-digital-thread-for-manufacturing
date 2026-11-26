# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A constrained column that is itself a time is never a population column.

The record-own-time rule defines a population by a VALUE field ``C`` (e.g. a
defect code) whose record carries its own time ``T``, then windows on another
time. A time column used as a ``IS NOT NULL`` filter is not such a field: it is
a time, and a struct holding two times has no single own time anyway. So
``decide`` must not treat a temporal constrained column as a population column,
and ``own_time_column`` must return ``None`` for a temporal column.

These tests read ``decide`` and ``own_time_column`` directly, with the catalog
types built the way the live catalog carries them (``{name: type}``). Neutral
names (the repo's domain-neutrality ratchet): a flattened struct ``rec.a.code``
+ ``rec.a.detected_at`` (one sibling time) and ``rec.b.start_at`` +
``rec.b.need_at`` (two times), plus a top-level ``done_at``.
"""
from __future__ import annotations

import importlib

import pytest

from agents.storage_bound_rule import bounded_columns

_TS = "timestamp(6) with time zone"
#: A flattened catalog: struct ``rec.a`` has one sibling time (``detected_at``);
#: struct ``rec.b`` has two (``start_at``, ``need_at``); ``done_at`` is a
#: top-level time.
_COLUMNS = {
    "id": "varchar",
    "rec.a.code": "varchar",
    "rec.a.detected_at": _TS,
    "rec.b.label": "varchar",
    "rec.b.start_at": _TS,
    "rec.b.need_at": _TS,
    "done_at": _TS,
}
_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _rt():
    return importlib.import_module("agents.record_time")


def _window(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


def _decide(sql: str):
    """``decide`` over ``sql`` with the catalog ``_COLUMNS``, building
    ``bounded`` the way the production path does."""
    read = bounded_columns(sql)
    bounded = read[1] if read else {}
    return _rt().decide(sql, "events", bounded, _COLUMNS)


# --------------------------------------------------------------------------- #
# own_time_column: a temporal column is never a population, and the two-time
# struct is already ambiguous.
# --------------------------------------------------------------------------- #
def test_own_time_column_of_a_temporal_column_is_none():
    """A time column has no own time: it IS a time, not a field whose record
    carries one. ``rec.b.start_at`` is temporal and its parent struct carries a
    sibling time (``rec.b.need_at``), so the old rule wrongly paired it; it
    must now return None."""
    assert _rt().own_time_column("rec.b.start_at", _COLUMNS) is None


def test_own_time_column_of_a_single_time_sibling_column_is_none():
    """``rec.a.detected_at`` is temporal with no sibling time: None. An
    unchanged invariant (no other time exists to pair it with); the guard on a
    temporal column is proven by the two-time and type-matrix tests."""
    assert _rt().own_time_column("rec.a.detected_at", _COLUMNS) is None


def test_own_time_column_of_a_non_temporal_field_is_its_single_sibling_time():
    assert _rt().own_time_column("rec.a.code", _COLUMNS) == "rec.a.detected_at"


def test_own_time_column_of_a_two_time_struct_field_is_none():
    """A non-temporal field whose parent struct carries two times is
    ambiguous: no single own time. Unchanged behavior, pinned."""
    assert _rt().own_time_column("rec.b.label", _COLUMNS) is None


# --------------------------------------------------------------------------- #
# decide: the misfire shape and the temporal-filter shape make no decision;
# the real rule still fires.
# --------------------------------------------------------------------------- #
def test_the_misfire_shape_a_time_filter_windowed_on_another_time_makes_no_decision():
    """``rec.b.start_at IS NOT NULL`` (a time used as a filter) + a window on
    ``done_at``: the time filter is not a population, so no decision. This is
    the live misfire shape (a ``start_at IS NOT NULL`` filter read as a field
    whose own time is its sibling ``need_at``)."""
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND rec.b.start_at IS NOT NULL "
           "AND done_at IS NOT NULL")
    assert _decide(sql) is None


def test_a_single_time_sibling_used_as_a_filter_makes_no_decision():
    """A temporal column with exactly one sibling time under its parent
    (``rec.a.detected_at IS NOT NULL``) windowed on ``done_at``: a time is
    never a population, so no decision."""
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND rec.a.detected_at IS NOT NULL")
    assert _decide(sql) is None


def test_the_real_rule_still_fires_on_a_value_field():
    """``rec.a.code = 'X'`` windowed on ``done_at`` still refuses, own column
    ``rec.a.detected_at``."""
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND rec.a.code = 'X'")
    found = _decide(sql)
    assert found is not None
    assert found.population_column == "rec.a.code"
    assert found.own_column == "rec.a.detected_at"
    assert found.windows == ("done_at",)


def test_a_non_temporal_field_in_a_two_time_struct_makes_no_decision():
    """``rec.b.label = 'X'`` windowed on ``done_at``: the parent struct has two
    times, so no own time, so no decision."""
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND rec.b.label = 'X'")
    assert _decide(sql) is None


# --------------------------------------------------------------------------- #
# Every catalog time family is a time, whatever its precision or zone; value
# types are not. Each temporal type sits beside one sibling timestamp, so only
# the temporal-self guard keeps it from being paired.
# --------------------------------------------------------------------------- #
_TIME_TYPES = ("date", "datetime", "time", "time(3)", "time with time zone",
               "time(6) with time zone", "timestamp", "timestamp(3)", _TS,
               "timestamptz", "TIMESTAMP(6) WITH TIME ZONE", "Time")
_VALUE_TYPES = ("varchar", "varchar(16)", "bigint", "integer", "boolean",
                "decimal(10,2)", "double", "array(varchar)")


def _pair(kind: str) -> dict:
    """A struct ``rec.c`` with ``x`` of type ``kind`` beside one sibling
    timestamp ``other_at``, and a top-level ``done_at``."""
    return {"rec.c.x": kind, "rec.c.other_at": _TS, "done_at": _TS}


@pytest.mark.parametrize("kind", _TIME_TYPES)
def test_a_column_of_any_time_type_has_no_own_time(kind):
    assert _rt().own_time_column("rec.c.x", _pair(kind)) is None


@pytest.mark.parametrize("kind", _VALUE_TYPES)
def test_a_column_of_a_value_type_keeps_its_single_sibling_time(kind):
    assert _rt().own_time_column("rec.c.x", _pair(kind)) == "rec.c.other_at"


@pytest.mark.parametrize("kind", _TIME_TYPES)
def test_a_time_typed_filter_windowed_on_another_time_makes_no_decision(kind):
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND rec.c.x IS NOT NULL")
    read = bounded_columns(sql)
    assert _rt().decide(sql, "events", read[1] if read else {}, _pair(kind)) is None


# --------------------------------------------------------------------------- #
# The column's own type is found whatever the catalog map's case, the same way
# the sibling scan already compares names.
# --------------------------------------------------------------------------- #
def test_the_own_type_lookup_ignores_the_catalog_map_case():
    columns = {"Rec.B.Start_At": _TS, "Rec.B.Need_At": _TS, "Rec.A.Code": "varchar",
               "Rec.A.Detected_At": _TS}
    assert _rt().own_time_column("rec.b.start_at", columns) is None
    assert _rt().own_time_column("REC.B.START_AT", columns) is None
    assert _rt().own_time_column("rec.a.code", columns) == "Rec.A.Detected_At"


def test_a_column_with_no_type_in_the_map_is_still_read_as_a_value_field():
    """An unknown or empty type keeps today's reading (a value field): a
    refused first send costs one round trip, a missed refusal can leave a wrong
    window unsaid."""
    columns = {"rec.a.code": "", "rec.a.detected_at": _TS}
    assert _rt().own_time_column("rec.a.code", columns) == "rec.a.detected_at"


# --------------------------------------------------------------------------- #
# Every population form the rule reads: a value field refuses through each, a
# time field (with a sibling time) through none.
# --------------------------------------------------------------------------- #
_FORMS = ("{c} = 'X'", "{c} IN ('X', 'Y')", "{c} LIKE 'X%'", "{c} IS NOT NULL",
          "{c} <> ''", "{c} <> '[]'", "{c} != ''")


@pytest.mark.parametrize("form", _FORMS)
def test_each_population_form_on_a_value_field_refuses(form):
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND {form.format(c='rec.a.code')}")
    found = _decide(sql)
    assert found is not None and found.population_column == "rec.a.code"


@pytest.mark.parametrize("form", _FORMS)
def test_each_population_form_on_a_time_field_with_a_sibling_time_makes_no_decision(form):
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_window('done_at')} AND {form.format(c='rec.b.start_at')}")
    assert _decide(sql) is None
