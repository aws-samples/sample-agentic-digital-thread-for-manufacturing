# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An exact split reads ONE current time: the clock is frozen once.

A WHERE using the current time (``current_timestamp``, ``now()``,
``current_date``, ``localtimestamp``) ran in every piece on its own, so each
piece saw a different instant; across a boundary such as midnight for
``current_date`` the merged rows matched no single query time, yet the result
was marked exact (only the connector note said "each piece on its own", and
the metric layer dropped even that).

Now the split takes one UTC instant when it starts and, in every piece's SQL,
replaces each clock form with a literal of the type the engine gives it
(``current_timestamp``/``now()``: timestamp(3) with time zone;
``localtimestamp``: timestamp(3); a precision ``(p)`` keeps p digits;
``current_date``: a DATE in the session zone, UTC). Only real tokens are
replaced, never text inside a string literal or a quoted name. A clock form
it cannot type exactly (a time of day, or an argument it does not read) is
refused with ``clock_unfrozen``. The instant is in the lineage
(``clock_frozen_at``), and the metric layer calls the result exact only then.

Fully offline (``FakeLake`` with a moving clock); synthetic names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agents.athena_tools as at
from agents import athena_exact_split as sp
from agents.athena_exact_shape import ShapeRefusal, parse_shape

from . import test_athena_exact_split as base
from ._athena_split_fakes import FakeLake

UTC = timezone.utc
#: The split starts two seconds before midnight; the engine's clock then
#: moves an hour per query, so every piece would see another day.
FROZEN = datetime(2026, 9, 14, 23, 59, 58, tzinfo=UTC)
_BY_DAY = ("SELECT site, COUNT(*), SUM(qty) FROM events WHERE created >= TIMESTAMP '2026-09-02 00:00:00' "
           "AND created < current_date GROUP BY site")


@pytest.fixture
def lake(monkeypatch):
    lake = FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created", cutoff=base._CUTOFF)
    ticks = iter(range(10_000))
    lake.clock = lambda: FROZEN + timedelta(hours=next(ticks))
    monkeypatch.setattr(sp, "_clock_now", lambda: FROZEN, raising=False)
    return lake


def _exact(lake: FakeLake, sql: str) -> dict:
    return at.run_athena_query_exact(sql, dict(base._CFG), source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _same(got, want) -> bool:
    return base._same_rows(got, want)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def test_pieces_across_midnight_give_one_querys_rows_at_the_frozen_instant(lake):
    later = lake.reference(_BY_DAY, at=FROZEN + timedelta(hours=1))
    assert not _same(lake.reference(_BY_DAY, at=FROZEN), later)       # the boundary matters
    result = _exact(lake, _BY_DAY)
    split = result["exact_split"]
    assert split["pieces"] > 1 and split["exact"] is True
    assert _same(result["rows"], lake.reference(_BY_DAY, at=FROZEN))
    assert all("DATE '2026-09-14'" in p and "current_date" not in p for p in _pieces(lake))


def test_the_lineage_records_the_frozen_instant_and_drops_the_per_piece_note(lake):
    result = _exact(lake, _BY_DAY)
    split = result["exact_split"]
    assert split["clock_frozen_at"] == "2026-09-14T23:59:58Z"
    assert "clock_per_piece" not in split
    assert "each piece" not in result["note"] and "2026-09-14T23:59:58Z" in result["note"]
    plain = _exact(lake, base._P1)
    assert "clock_frozen_at" not in plain["exact_split"] and "current time" not in plain["note"]


def test_a_clock_word_inside_a_string_literal_is_untouched(lake):
    sql = ("SELECT COUNT(*) FROM events WHERE state <> 'current_date' AND batch <> 'now() current_timestamp' "
           "AND created >= TIMESTAMP '2026-09-02 00:00:00' AND completed < current_timestamp")
    result = _exact(lake, sql)
    assert result["rows"] == lake.reference(sql, at=FROZEN)
    for piece in _pieces(lake):
        assert "'current_date'" in piece and "'now() current_timestamp'" in piece
        assert "completed < TIMESTAMP '2026-09-14 23:59:58.000 UTC'" in piece


def test_the_frozen_instant_keeps_its_fraction_of_a_second(lake, monkeypatch):
    """The captured instant keeps its microseconds: rounding it down to a whole
    second would freeze a split starting at .987654 as .000 for
    ``current_timestamp`` and .000000 for ``current_timestamp(6)``."""
    captured = FROZEN.replace(microsecond=987654)
    monkeypatch.setattr(sp, "_clock_now", lambda: captured, raising=False)
    sql = ("SELECT COUNT(*) FROM events WHERE created >= TIMESTAMP '2026-09-02 00:00:00' "
           "AND completed < current_timestamp AND completed <> current_timestamp(6)")
    result = _exact(lake, sql)
    for piece in _pieces(lake):
        assert "completed < TIMESTAMP '2026-09-14 23:59:58.987 UTC'" in piece
        assert "completed <> TIMESTAMP '2026-09-14 23:59:58.987654 UTC'" in piece
    assert result["exact_split"]["clock_frozen_at"] == "2026-09-14T23:59:58.987654Z"


def test_a_clock_form_that_cannot_be_frozen_exactly_is_refused(lake):
    sql = ("SELECT COUNT(*) FROM events WHERE created >= TIMESTAMP '2026-09-02 00:00:00' "
           "AND CAST(completed AS time) < current_time")
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql)
    assert info.value.exact_split["reason"] == "clock_unfrozen"
    assert _pieces(lake) == []


# --------------------------------------------------------------------------- #
# The frozen text, form by form
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("clock, literal", [
    ("current_timestamp", "TIMESTAMP '2026-09-14 23:59:58.000 UTC'"),
    ("CURRENT_TIMESTAMP", "TIMESTAMP '2026-09-14 23:59:58.000 UTC'"),
    ("now()", "TIMESTAMP '2026-09-14 23:59:58.000 UTC'"),
    ("current_timestamp(6)", "TIMESTAMP '2026-09-14 23:59:58.000000 UTC'"),
    ("current_timestamp(0)", "TIMESTAMP '2026-09-14 23:59:58 UTC'"),
    ("localtimestamp", "TIMESTAMP '2026-09-14 23:59:58.000'"),
    ("localtimestamp(1)", "TIMESTAMP '2026-09-14 23:59:58.0'"),
    ("current_date", "DATE '2026-09-14'"),
])
def test_each_clock_form_becomes_a_literal_of_its_type(clock, literal):
    shape = parse_shape(f"SELECT COUNT(*) FROM t WHERE ts < {clock} - INTERVAL '1' DAY")
    from agents.athena_exact_shape import frozen_sql
    assert frozen_sql(shape, FROZEN) == f"SELECT COUNT(*) FROM t WHERE ts < {literal} - INTERVAL '1' DAY"


def test_the_instant_is_read_in_utc():
    from agents.athena_exact_shape import frozen_sql
    shape = parse_shape("SELECT COUNT(*) FROM t WHERE d < current_date AND ts < now()")
    mountain = FROZEN.astimezone(timezone(timedelta(hours=-6)))            # 17:59:58 the same day
    assert frozen_sql(shape, mountain + timedelta(hours=1)) == (
        "SELECT COUNT(*) FROM t WHERE d < DATE '2026-09-15' AND ts < TIMESTAMP '2026-09-15 00:59:58.000 UTC'")


def test_quoted_names_strings_and_a_column_named_now_are_not_clocks():
    from agents.athena_exact_shape import frozen_sql
    sql = ("SELECT COUNT(*) FROM t WHERE \"current_date\" > 1 AND note = 'current_timestamp' "
           "AND now > 3 AND ts < current_timestamp")
    shape = parse_shape(sql)
    assert frozen_sql(shape, FROZEN) == sql.replace(
        "ts < current_timestamp", "ts < TIMESTAMP '2026-09-14 23:59:58.000 UTC'")
    assert parse_shape("SELECT COUNT(*) FROM t WHERE now > 3").uses_clock is False


@pytest.mark.parametrize("clock", ["current_time", "localtime", "current_time(3)", "current_date()",
                                   "now(1)", "current_timestamp(x)", "current_timestamp(13)"])
def test_an_unfreezable_clock_form_is_refused(clock):
    with pytest.raises(ShapeRefusal) as info:
        parse_shape(f"SELECT COUNT(*) FROM t WHERE ts < {clock}")
    assert info.value.reason == "clock_unfrozen"


# --------------------------------------------------------------------------- #
# The metric layer
# --------------------------------------------------------------------------- #
def test_metric_text_says_exactly_only_for_a_frozen_clock():
    from agents.metric_athena_scope import from_result, inexact, scan_text
    split = {"applied": True, "pieces": 5, "exact": True, "atomic": True}
    frozen = from_result({"exact_split": {**split, "clock_frozen_at": "2026-09-14T23:59:58Z"}})
    assert "computed exactly" in scan_text(frozen) and "2026-09-14T23:59:58Z" in scan_text(frozen)
    assert not inexact(frozen)
    moving = from_result({"exact_split": {**split, "clock_per_piece": True}})
    assert "exactly" not in scan_text(moving) and "NOT EXACT" in scan_text(moving)
    assert inexact(moving)
    assert len(scan_text(moving)) <= 240 and len(scan_text(frozen)) <= 240
