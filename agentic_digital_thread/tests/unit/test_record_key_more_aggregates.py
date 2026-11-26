# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The record-key check also reads the engine's other aggregates whose value
depends on which records were kept.

``record_key.decide`` decides a de-duplication by a strict subset of the
record key only when the outer query aggregates the kept rows by their
multiplicity, and that list was closed: ``checksum``, ``map_agg``,
``set_agg``, ``reduce_agg`` and the statistical and regression aggregates
were missing, so a query that collapsed separate records and then computed
one of them over the rows it kept got no decision, though its figure changes
when rows are removed. One positive case per family below; a windowed call
and a call in a nested query block still give no decision.

Fully offline; the two neutral schemas of ``test_record_key_outer_aggregate``.
"""
from __future__ import annotations

import pytest

from tests.unit.test_record_key_outer_aggregate import _SCHEMAS, _decide, _outer, _rk

_FAMILIES = {
    "general": ["checksum({value}) AS c"],
    "bitwise": ["bitwise_xor_agg({value}) AS x"],
    "map": ["map_agg({dedupe_by}, {value}) AS m"],
    "set": ["set_agg({dedupe_by}) AS s"],
    "lambda": ["reduce_agg({value}, 0, (s, x) -> s + x, (s, t) -> s + t) AS r"],
    "statistical": ["corr({value}, {value}) AS c", "covar_pop({value}, {value}) AS c",
                    "covar_samp({value}, {value}) AS c", "kurtosis({value}) AS k",
                    "skewness({value}) AS s", "entropy({value}) AS e"],
    "regression": ["regr_slope({value}, {value}) AS b", "regr_intercept({value}, {value}) AS a"],
    "approximate": ["approx_set({dedupe_by}) AS h", "merge({value}) AS d"],
}
_CASES = [(family, call) for family, calls in _FAMILIES.items() for call in calls]


@pytest.fixture(autouse=True)
def _fresh():
    _rk().reset()
    yield
    _rk().reset()


@_SCHEMAS
@pytest.mark.parametrize("family,call", _CASES, ids=[f"{f}:{c.split('(')[0]}" for f, c in _CASES])
def test_each_family_aggregating_the_kept_rows_is_decided(schema, family, call):
    sql = _outer(schema, f"{call.format(**schema)} FROM latest WHERE rn = 1")
    found = _decide(schema, sql)
    assert found is not None and list(found.subset) == [schema["dedupe_by"]], family


@_SCHEMAS
@pytest.mark.parametrize("tail", [
    "{group}, checksum({value}) OVER (PARTITION BY {group}) AS c FROM latest WHERE rn = 1",
    "{group}, regr_slope({value}, {value}) OVER () AS b FROM latest WHERE rn = 1",
    "{group}, (SELECT map_agg(k, v) FROM other_table) AS m FROM latest WHERE rn = 1",
    "{group}, (SELECT set_agg(k) FROM other_table) AS s FROM latest WHERE rn = 1",
])
def test_a_windowed_or_nested_call_of_them_is_still_not_decided(schema, tail):
    assert _decide(schema, _outer(schema, tail.format(**schema))) is None


#: The rest of the engine's regression family: each pairs (y, x) over the
#: rows it aggregates, so its value moves when the de-duplication removes one.
_REGRESSION_REST = ("regr_count", "regr_avgx", "regr_avgy", "regr_r2", "regr_sxx",
                    "regr_sxy", "regr_syy")


@_SCHEMAS
@pytest.mark.parametrize("name", _REGRESSION_REST)
def test_the_rest_of_the_regression_family_aggregating_the_kept_rows_is_decided(schema, name):
    sql = _outer(schema, f"{name}({{value}}, {{value}}) AS r FROM latest WHERE rn = 1".format(
        **schema))
    found = _decide(schema, sql)
    assert found is not None and list(found.subset) == [schema["dedupe_by"]], name


@_SCHEMAS
@pytest.mark.parametrize("tail", [
    "{group}, regr_count({value}, {value}) OVER () AS c FROM latest WHERE rn = 1",
    "{group}, (SELECT regr_r2(k, v) FROM other_table) AS r FROM latest WHERE rn = 1",
])
def test_a_windowed_or_nested_regression_call_is_still_not_decided(schema, tail):
    assert _decide(schema, _outer(schema, tail.format(**schema))) is None
