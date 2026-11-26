# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pinned semantic contract of the duration measure across dialects.

Athena (Trino) ``date_diff('second', a, b)`` is the whole-second difference
truncated toward zero, and the measure clamps negatives at 0. The DuckDB
dialect must give those same numbers, so each case below states the contract
value (taken from the Trino definition, not from DuckDB) and runs the DuckDB
rendering against it. The Athena rendering is checked for shape only, since
Trino cannot run here; ``approx_percentile`` is an estimate and the S3 Tables
``quantile_disc`` is exact, which the disclosure flag must say.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.metric_measure_rule as mmr
from agents.metric_measure_rule import DurationMeasure, FieldRef

_MEASURE = DurationMeasure(kind="duration", start=FieldRef("t", "s"),
                           end=FieldRef("t", "e"), unit="seconds",
                           clamp_negative=True)
_UNCLAMPED = DurationMeasure(kind="duration", start=FieldRef("t", "s"),
                             end=FieldRef("t", "e"), unit="seconds",
                             clamp_negative=False)

# (start, end, whole seconds truncated toward zero)
_SPANS = [
    ("2026-01-01 10:00:00", "2026-01-01 10:01:00", 60),
    ("2026-01-01 10:00:00.900", "2026-01-01 10:00:01.100", 0),
    ("2026-01-01 10:00:00.100", "2026-01-01 10:00:01.900", 1),
    ("2026-01-01 10:00:01.100", "2026-01-01 10:00:00.900", 0),    # -0.2 truncates to 0
    ("2026-01-01 10:00:02.500", "2026-01-01 10:00:00.000", -2),   # -2.5 truncates to -2
    ("2026-01-01 10:00:00", "2026-01-01 10:00:00", 0),
    ("2026-01-01 23:59:59", "2026-01-02 00:00:01", 2),            # across midnight
]


def _run(expr, start, end, start_type="TIMESTAMP", end_type="TIMESTAMP"):
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"CREATE TABLE t(s {start_type}, e {end_type})")
    con.execute("INSERT INTO t VALUES (?, ?)", [start, end])
    try:
        return con.execute(f"SELECT {expr} FROM t").fetchone()[0]
    finally:
        con.close()


@pytest.mark.parametrize("start,end,seconds", _SPANS)
def test_unclamped_duration_truncates_toward_zero(start, end, seconds):
    expr = mmr.render_duration_expr(_UNCLAMPED, dialect="duckdb")
    assert _run(expr, start, end) == seconds


@pytest.mark.parametrize("start,end,seconds", _SPANS)
def test_clamped_duration_never_goes_below_zero(start, end, seconds):
    expr = mmr.render_duration_expr(_MEASURE, dialect="duckdb")
    assert _run(expr, start, end) == max(seconds, 0)


@pytest.mark.parametrize("start,end,seconds", _SPANS)
def test_zoned_and_unzoned_columns_give_the_same_duration(start, end, seconds):
    expr = mmr.render_duration_expr(_UNCLAMPED, dialect="duckdb")
    assert _run(expr, start, end, "TIMESTAMPTZ", "TIMESTAMPTZ") == seconds


def test_a_date_column_is_cast_to_midnight_timestamp():
    expr = mmr.render_duration_expr(_UNCLAMPED, dialect="duckdb",
                                    casts={"start": "date"})
    assert _run(expr, "2026-01-01", "2026-01-01 00:01:30", "DATE", "TIMESTAMP") == 90


def test_both_date_columns_are_cast():
    expr = mmr.render_duration_expr(_UNCLAMPED, dialect="duckdb",
                                    casts={"start": "date", "end": "date"})
    assert _run(expr, "2026-01-01", "2026-01-03", "DATE", "DATE") == 172800


def test_a_null_timestamp_is_excluded_not_counted_as_zero():
    con = duckdb.connect()
    con.execute("CREATE TABLE t(s TIMESTAMP, e TIMESTAMP)")
    con.execute("INSERT INTO t VALUES ('2026-01-01 10:00:00', '2026-01-01 10:00:10'), "
                "(NULL, '2026-01-01 10:00:10'), ('2026-01-01 10:00:00', NULL)")
    expr = mmr.render_duration_expr(_MEASURE, dialect="duckdb")
    where = mmr.null_exclusion_where(_MEASURE)
    n, p50 = con.execute(
        f"SELECT COUNT({expr}), quantile_disc({expr}, 0.5) FROM t WHERE {where}").fetchone()
    assert (n, p50) == (1, 10)


def test_the_unit_scaling_is_the_same_in_every_dialect():
    minutes = DurationMeasure(kind="duration", start=_MEASURE.start, end=_MEASURE.end,
                              unit="minutes", clamp_negative=True)
    for dialect in ("athena", "postgres", "duckdb"):
        assert mmr.render_duration_expr(minutes, dialect=dialect).endswith(" / 60.0")


def test_the_athena_rendering_keeps_trino_date_diff():
    expr = mmr.render_duration_expr(_MEASURE, dialect="athena")
    assert expr == 'greatest(date_diff(\'second\', "s", "e"), 0)'


def test_the_percentile_disclosure_flag_per_dialect():
    assert mmr.dialect_is_approximate("athena") is True
    assert mmr.dialect_is_approximate("postgres") is False
    assert mmr.dialect_is_approximate("duckdb") is False
    athena = mmr.render_percentile_select("x", (0.5,), dialect="athena")
    duck = mmr.render_percentile_select("x", (0.5,), dialect="duckdb")
    assert athena.startswith("approx_percentile(") and duck.startswith("quantile_disc(")


def test_an_unknown_dialect_is_still_refused():
    with pytest.raises(ValueError, match="unsupported dialect"):
        mmr.render_duration_expr(_MEASURE, dialect="mysql")
