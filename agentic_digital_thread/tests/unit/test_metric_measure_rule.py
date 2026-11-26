# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

""": the ONE closed rule for the duration measure, field refs, and the
percentile aggregation (``agents/metric_measure_rule.py``).

Mirrors ``metric_predicate_rule``: import-pure, TOTAL over hostile input (a
problem string, never a raw ``TypeError``/``OverflowError``), no domain
literals. Neutral fixture vocabulary only."""
from __future__ import annotations

import pytest

import agents.metric_measure_rule as mmr
from agents.metric_measure_rule import (
    DURATION_UNITS,
    MAX_PERCENTILES,
    DurationMeasure,
    FieldRef,
    check_field_ref,
    check_measure,
    check_percentiles,
)


# --- FieldRef --------------------------------------------------------------- #

def test_field_ref_accepts_concept_and_column():
    assert check_field_ref(FieldRef(concept="event-time", column="start_ts")) is None


@pytest.mark.parametrize("ref", [
    None, {"concept": "event-time"}, "event-time", 7,
    FieldRef(concept="", column="start_ts"),
    FieldRef(concept="event-time", column=""),
    FieldRef(concept="event-time", column="bad col"),
    FieldRef(concept="has space", column="start_ts"),
])
def test_field_ref_rejects_malformed(ref):
    assert isinstance(check_field_ref(ref), str)


def test_field_ref_allows_dotted_column():
    # A Glue-flattened struct member is ONE identifier (ADR-0076 D4).
    assert check_field_ref(FieldRef(concept="event-time",
                                    column="payload.start_ts")) is None


# --- DurationMeasure -------------------------------------------------------- #

def _measure(**over) -> DurationMeasure:
    base = dict(kind="duration",
                start=FieldRef("event-time", "start_ts"),
                end=FieldRef("event-time", "complete_ts"),
                unit="hours", clamp_negative=True)
    base.update(over)
    return DurationMeasure(**base)


def test_valid_measure_passes():
    assert check_measure(_measure()) is None


def test_every_unit_is_accepted():
    assert DURATION_UNITS == ("seconds", "minutes", "hours")
    for unit in DURATION_UNITS:
        assert check_measure(_measure(unit=unit)) is None


@pytest.mark.parametrize("over,needle", [
    (dict(kind="elapsed"), "kind"),
    (dict(unit="days"), "unit"),
    (dict(clamp_negative="yes"), "clamp_negative"),
    (dict(start="start_ts"), "start"),
    (dict(end=None), "end"),
    (dict(start=FieldRef("event-time", "bad col")), "start"),
])
def test_measure_rejects_malformed(over, needle):
    problem = check_measure(_measure(**over))
    assert isinstance(problem, str) and needle in problem


def test_check_measure_total_over_non_measure():
    for bad in (None, {"kind": "duration"}, "duration", 7):
        assert isinstance(check_measure(bad), str)


# --- percentiles ------------------------------------------------------------ #

def test_valid_percentiles_pass():
    assert check_percentiles((0.9, 0.99)) is None


def test_single_percentile_passes():
    assert check_percentiles((0.5,)) is None


def test_five_percentiles_pass():
    assert MAX_PERCENTILES == 5
    assert check_percentiles((0.1, 0.25, 0.5, 0.9, 0.99)) is None


@pytest.mark.parametrize("values", [
    (), (0.1,) * 6, (0.0,), (1.0,), (-0.1,), (1.5,),
    (0.9, 0.9), ("0.9",), (True,), (float("nan"),), (float("inf"),),
    None, 0.9, "0.9",
])
def test_percentiles_reject_malformed(values):
    assert isinstance(check_percentiles(values), str)


def test_percentiles_accept_a_json_list():
    assert check_percentiles([0.9]) is None
    assert check_percentiles([0.9, 0.99]) is None


def test_percentiles_total_over_hostile():
    # never a raw TypeError/OverflowError
    assert isinstance(check_percentiles(({"p": 1},)), str)
    assert isinstance(check_percentiles(([0.9],)), str)


# --- the exprs are pure identifier + closed-vocab composition --------------- #

def test_athena_duration_expr_clamped_and_unit_scaled():
    expr = mmr.render_duration_expr(
        _measure(), dialect="athena", qualifier="")
    assert expr == (
        'greatest(date_diff(\'second\', "start_ts", "complete_ts"), 0) / 3600.0')


def test_athena_duration_expr_no_clamp_minutes():
    expr = mmr.render_duration_expr(
        _measure(clamp_negative=False, unit="minutes"), dialect="athena", qualifier="")
    assert expr == 'date_diff(\'second\', "start_ts", "complete_ts") / 60.0'


def test_athena_duration_expr_seconds_has_no_scale():
    expr = mmr.render_duration_expr(_measure(unit="seconds"), dialect="athena",
                                    qualifier="")
    assert expr == 'greatest(date_diff(\'second\', "start_ts", "complete_ts"), 0)'


def test_postgres_duration_expr_clamped_and_unit_scaled():
    expr = mmr.render_duration_expr(_measure(), dialect="postgres", qualifier="")
    assert expr == (
        'greatest(extract(epoch from ("complete_ts" - "start_ts")), 0) / 3600.0')


def test_duration_expr_qualified_columns():
    expr = mmr.render_duration_expr(_measure(unit="seconds"), dialect="athena",
                                    qualifier="m.")
    assert expr == 'greatest(date_diff(\'second\', m."start_ts", m."complete_ts"), 0)'


def test_dotted_columns_render_as_one_identifier():
    expr = mmr.render_duration_expr(
        _measure(unit="seconds", clamp_negative=False,
                 start=FieldRef("event-time", "p.start_ts"),
                 end=FieldRef("event-time", "p.complete_ts")),
        dialect="athena", qualifier="")
    assert expr == 'date_diff(\'second\', "p.start_ts", "p.complete_ts")'


def test_render_duration_expr_rejects_unknown_dialect():
    with pytest.raises(ValueError):
        mmr.render_duration_expr(_measure(), dialect="mysql", qualifier="")


def test_render_percentile_athena_is_approx():
    sql = mmr.render_percentile_select("DUR", (0.9, 0.99), dialect="athena")
    assert sql == "approx_percentile(DUR, 0.9), approx_percentile(DUR, 0.99)"


def test_render_percentile_postgres_is_disc():
    sql = mmr.render_percentile_select("DUR", (0.9,), dialect="postgres")
    assert sql == "percentile_disc(0.9) WITHIN GROUP (ORDER BY DUR)"
