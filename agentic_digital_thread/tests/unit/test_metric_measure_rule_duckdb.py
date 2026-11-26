# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``duckdb`` dialect of the duration-percentile measure rule.

Each fragment the rule renders for DuckDB (S3 Tables) is EXECUTED on an
in-memory DuckDB, so a fragment that merely looks right but is not valid
DuckDB fails here. The duration matches Athena's ``date_diff('second', ...)``
(whole seconds, truncated), not DuckDB's own boundary-counting ``date_diff``.
"""
from __future__ import annotations

import duckdb
import pytest

import agents.metric_measure_rule as mmr
from agents.metric_measure_rule import DurationMeasure, FieldRef

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef("t", "start_ts"),
    end=FieldRef("t", "end_ts"), unit="seconds", clamp_negative=True)


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone='UTC'")
    c.execute("CREATE TABLE seg(start_ts TIMESTAMP, end_ts TIMESTAMP, "
              "start_z TIMESTAMPTZ, end_z TIMESTAMPTZ, start_d DATE)")
    c.execute("INSERT INTO seg VALUES "
              "('2026-01-01 10:00:00.900', '2026-01-01 10:00:01.100', "
              "'2026-01-01 10:00:00.900+00', '2026-01-01 10:00:01.100+00', "
              "'2026-01-01'), "
              "('2026-01-01 10:00:00', '2026-01-01 10:01:00', "
              "'2026-01-01 10:00:00+00', '2026-01-01 10:01:00+00', "
              "'2026-01-01'), "
              "('2026-01-01 10:00:10', '2026-01-01 10:00:00', "
              "'2026-01-01 10:00:10+00', '2026-01-01 10:00:00+00', "
              "'2026-01-01')")
    yield c
    c.close()


def _durations(con, expr):
    return [r[0] for r in con.execute(f"SELECT {expr} FROM seg").fetchall()]


def test_duckdb_is_a_supported_dialect():
    assert "duckdb" in mmr._DIALECTS


def test_duration_truncates_to_whole_seconds_like_athena(con):
    expr = mmr.render_duration_expr(_MEASURE, dialect="duckdb")
    # 0.2s -> 0 (Athena date_diff truncates; DuckDB's own date_diff would say 1),
    # 60s -> 60, negative -> clamped at 0.
    assert _durations(con, expr) == [0, 60, 0]


def test_duration_without_clamp_keeps_the_negative(con):
    measure = DurationMeasure(kind="duration", start=_MEASURE.start,
                              end=_MEASURE.end, unit="seconds",
                              clamp_negative=False)
    expr = mmr.render_duration_expr(measure, dialect="duckdb")
    assert _durations(con, expr) == [0, 60, -10]


def test_duration_scaled_to_minutes(con):
    measure = DurationMeasure(kind="duration", start=_MEASURE.start,
                              end=_MEASURE.end, unit="minutes",
                              clamp_negative=True)
    expr = mmr.render_duration_expr(measure, dialect="duckdb")
    assert _durations(con, expr) == [0.0, 1.0, 0.0]


def test_duration_over_timestamptz_columns(con):
    measure = DurationMeasure(kind="duration", start=FieldRef("t", "start_z"),
                              end=FieldRef("t", "end_z"), unit="seconds",
                              clamp_negative=True)
    expr = mmr.render_duration_expr(measure, dialect="duckdb")
    assert _durations(con, expr) == [0, 60, 0]


def test_duration_casts_a_date_column_to_timestamp(con):
    measure = DurationMeasure(kind="duration", start=FieldRef("t", "start_d"),
                              end=FieldRef("t", "end_ts"), unit="hours",
                              clamp_negative=True)
    expr = mmr.render_duration_expr(measure, dialect="duckdb",
                                    casts={"start": "date"})
    assert _durations(con, expr) == [
        pytest.approx(36001 / 3600), pytest.approx(36060 / 3600), 10.0]
    assert "::timestamp" in expr


def test_percentile_is_exact_quantile_disc(con):
    expr = mmr.render_duration_expr(_MEASURE, dialect="duckdb")
    select = mmr.render_percentile_select(expr, (0.5, 0.9), dialect="duckdb")
    assert "quantile_disc" in select
    assert "approx" not in select
    row = con.execute(f"SELECT {select}, COUNT({expr}) FROM seg").fetchone()
    assert row == (0, 60, 3)


def test_duckdb_percentile_is_not_approximate():
    assert mmr.dialect_is_approximate("duckdb") is False
    assert mmr.dialect_is_approximate("athena") is True


def test_null_exclusion_where_runs_on_duckdb(con):
    con.execute("INSERT INTO seg (start_ts) VALUES ('2026-01-01 00:00:00')")
    where = mmr.null_exclusion_where(_MEASURE)
    assert con.execute(f"SELECT COUNT(*) FROM seg WHERE {where}").fetchone()[0] == 3


@pytest.mark.parametrize("col_type,column", [
    ("unzoned", "end_ts"), ("zoned", "end_z"), ("date", "start_d")])
def test_absolute_window_literals_run_on_duckdb(con, col_type, column):
    where = mmr.absolute_window_where(
        column, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
        dialect="duckdb", col_type=col_type)
    got = con.execute(f"SELECT COUNT(*) FROM seg WHERE {where}").fetchone()[0]
    assert got == 3


def test_absolute_window_excludes_rows_outside(con):
    where = mmr.absolute_window_where(
        "end_ts", "2026-01-01T10:00:30Z", "2026-01-01T10:02:00Z",
        dialect="duckdb", col_type="unzoned")
    assert con.execute(f"SELECT COUNT(*) FROM seg WHERE {where}").fetchone()[0] == 1


def test_qualified_expression_runs_in_a_joined_shape(con):
    expr = mmr.render_duration_expr(_MEASURE, dialect="duckdb", qualifier="m.")
    sql = f"SELECT {expr} FROM seg m JOIN seg d ON m.start_ts = d.start_ts"
    assert sorted(r[0] for r in con.execute(sql).fetchall()) == [0, 0, 60]
