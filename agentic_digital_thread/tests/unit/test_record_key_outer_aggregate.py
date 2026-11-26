# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The record-key check reads only a de-duplication whose rows are then counted.

Measured in a replay of logged queries, with a declared record key live on the
table: a question asked for the current status of each line "based on the most
recent records per line", and the system agent wrote

    WITH recent AS (SELECT line, status, ..., ROW_NUMBER() OVER (PARTITION BY
      line ORDER BY updated_at DESC) AS rn FROM records WHERE ...)
    SELECT line, status, ... FROM recent WHERE rn = 1 ORDER BY line

It keeps the newest row of each line to read its values; it counts nothing. The
check refused it, and both of its texts were false there: that rows sharing
``line`` are separate records the query collapsed, and that the answer must
say it counted distinct values of ``line``.

So :func:`record_key.decide` now decides only when the outer query's result
depends on how many de-duplicated rows there are: an aggregate whose value
changes with row multiplicity (``COUNT`` in any form, ``SUM``, ``AVG``, a
percentile, ``array_agg`` and the like) in the outer query's SELECT list or
HAVING, at its own level (not in a nested query block, not a window under
``OVER``). A plain selection of the ``rn = 1`` rows, and an outer query whose
only aggregates are ``MIN`` / ``MAX``, give no decision. A refusal of a query
that aggregates without counting says so, instead of "counted".

Two schemas, neutral names: one shaped like the logged table (a five-column
key with a coalesced column, partitioned by one key column), one named
differently. Fully offline.
"""
from __future__ import annotations

import importlib

import pytest

import agents.system_agent as sa

_TS = "TIMESTAMP '2026-03-02 08:00:00'"
_TS_END = "TIMESTAMP '2026-03-03 08:00:00'"


def _rk():
    return importlib.import_module("agents.record_key")


# Schema A: shaped like the logged table. Key id, kind, revision, line,
# COALESCE(attempt_ref, ''); the latest-row read partitions by line.
_A = {
    "table": "records",
    "key": [{"column": "id", "coalesced": False}, {"column": "kind", "coalesced": False},
            {"column": "revision", "coalesced": False}, {"column": "line", "coalesced": False},
            {"column": "attempt_ref", "coalesced": True}],
    "group": "line",
    "value": "qty",
    "status": "status",
    "latest": (
        "WITH recent AS ( SELECT line, status, begun_at, done_at, failed_at, kind, "
        "ROW_NUMBER() OVER (PARTITION BY line ORDER BY updated_at DESC) as rn "
        f"FROM records WHERE updated_at >= {_TS} AND updated_at <= {_TS_END} "
        "AND line IS NOT NULL AND line NOT IN ('L7', 'L9') ) "
        "SELECT line, status as current_status, kind, begun_at, done_at, failed_at "
        "FROM recent WHERE rn = 1 ORDER BY line LIMIT 500"),
    "dedupe_by": "id",
    "cte": ("WITH latest AS (SELECT id, line, status, qty, revision, "
            "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
            f"FROM records WHERE line = 'L1' AND done_at >= {_TS} AND done_at < {_TS_END}) "),
}

# Schema B: named differently. Key run_ref, step, attempt, station; the
# latest-row read partitions by station.
_B = {
    "table": "job_events",
    "key": ["run_ref", "step", "attempt", "station"],
    "group": "station",
    "value": "units",
    "status": "phase",
    "latest": (
        "WITH newest AS (SELECT station, phase, opened_ts, closed_ts, "
        "ROW_NUMBER() OVER (PARTITION BY station ORDER BY changed_ts DESC) AS pos "
        f"FROM job_events WHERE changed_ts >= {_TS}) "
        "SELECT station, phase, opened_ts, closed_ts FROM newest WHERE pos = 1"),
    "dedupe_by": "run_ref",
    "cte": ("WITH latest AS (SELECT run_ref, station, phase, units, attempt, "
            "ROW_NUMBER() OVER (PARTITION BY run_ref ORDER BY attempt DESC) AS rn "
            f"FROM job_events WHERE station = 'S1' AND closed_ts >= {_TS}) "),
}

_SCHEMAS = pytest.mark.parametrize("schema", [_A, _B], ids=["logged-shape", "renamed"])


def _key_names(schema) -> list:
    return [t["column"] if isinstance(t, dict) else t for t in schema["key"]]


def _decide(schema, sql):
    return _rk().decide(sql, schema["table"], _key_names(schema))


def _outer(schema, select_tail: str) -> str:
    """The schema's de-duplicating CTE read by ``SELECT <select_tail>``."""
    return schema["cte"] + f"SELECT {select_tail}"


@pytest.fixture(autouse=True)
def _fresh():
    _rk().reset()
    yield
    _rk().reset()


# --------------------------------------------------------------------------- #
# The latest row per group, read for its values: no decision.
# --------------------------------------------------------------------------- #
@_SCHEMAS
def test_the_latest_row_per_group_read_for_its_values_is_not_decided(schema):
    assert _decide(schema, schema["latest"]) is None


@_SCHEMAS
def test_the_latest_row_per_group_read_runs_through_the_system_agent_rule(schema):
    _rk().remember("system-a", {schema["table"]: schema["key"]})
    found, fired = sa._record_key_rule(schema["latest"], "system-a")
    assert found is None and fired == []


@_SCHEMAS
@pytest.mark.parametrize("tail", [
    "* FROM latest WHERE rn = 1",
    "{group}, {status} FROM latest WHERE rn = 1 ORDER BY {group}",
    "DISTINCT {group} FROM latest WHERE rn = 1",
    "{group} FROM latest WHERE rn = 1 GROUP BY {group}",
])
def test_a_plain_selection_of_the_kept_rows_is_not_decided(schema, tail):
    sql = _outer(schema, tail.format(**schema))
    assert _decide(schema, sql) is None


@_SCHEMAS
@pytest.mark.parametrize("tail", [
    "MAX({value}) AS top FROM latest WHERE rn = 1",
    "MIN({value}) AS low, MAX({value}) AS top FROM latest WHERE rn = 1",
    "{group}, MAX({status}) AS last_status FROM latest WHERE rn = 1 GROUP BY {group}",
])
def test_an_outer_query_with_only_min_and_max_is_not_decided(schema, tail):
    sql = _outer(schema, tail.format(**schema))
    assert _decide(schema, sql) is None


@_SCHEMAS
@pytest.mark.parametrize("tail", [
    # a window over the kept rows, not an aggregate of the outer query
    "{group}, COUNT(*) OVER (PARTITION BY {group}) AS n FROM latest WHERE rn = 1",
    "{group}, SUM({value}) OVER () AS total FROM latest WHERE rn = 1",
    # a count in a nested query block counts another relation's rows
    "{group}, (SELECT COUNT(*) FROM other_table) AS n FROM latest WHERE rn = 1",
])
def test_a_window_or_a_nested_block_count_is_not_an_outer_count(schema, tail):
    sql = _outer(schema, tail.format(**schema))
    assert _decide(schema, sql) is None


# --------------------------------------------------------------------------- #
# The kept rows counted or aggregated by multiplicity: decided, as today.
# --------------------------------------------------------------------------- #
@_SCHEMAS
@pytest.mark.parametrize("tail", [
    # the original measured shape
    "COUNT(DISTINCT {dedupe_by}) AS n FROM latest WHERE rn = 1 AND {status} = 'DONE'",
    "COUNT(*) AS n FROM latest WHERE rn = 1",
    "SUM({value}) AS total FROM latest WHERE rn = 1",
    "AVG({value}) AS mean FROM latest WHERE rn = 1",
    "ROUND(AVG({value}), 1) AS mean FROM latest WHERE rn = 1",
    "approx_percentile({value}, 0.5) AS p50 FROM latest WHERE rn = 1",
    "array_agg({dedupe_by}) AS ids FROM latest WHERE rn = 1",
    "count_if({status} = 'DONE') AS n FROM latest WHERE rn = 1",
    "{group}, COUNT(*) AS n FROM latest WHERE rn = 1 GROUP BY {group}",
    "{group} FROM latest WHERE rn = 1 GROUP BY {group} HAVING COUNT(*) > 1",
    "MAX({value}) AS top, COUNT(*) AS n FROM latest WHERE rn = 1",
    "COUNT(*) FILTER (WHERE {status} = 'DONE') AS n FROM latest WHERE rn = 1",
])
def test_an_outer_count_or_multiplicity_aggregate_is_decided(schema, tail):
    sql = _outer(schema, tail.format(**schema))
    found = _decide(schema, sql)
    assert found is not None and list(found.subset) == [schema["dedupe_by"]]


@_SCHEMAS
def test_the_measured_shape_is_refused_through_the_system_agent_rule(schema):
    _rk().remember("system-a", {schema["table"]: schema["key"]})
    sql = _outer(schema, "COUNT(DISTINCT {dedupe_by}) AS n FROM latest "
                         "WHERE rn = 1 AND {status} = 'DONE'".format(**schema))
    found, fired = sa._record_key_rule(sql, "system-a")
    assert found is not None and [rule for rule, _exc in fired] == ["record_key"]


# --------------------------------------------------------------------------- #
# The texts: a count keeps today's words; an aggregate that counts nothing
# does not say "counted".
# --------------------------------------------------------------------------- #
@_SCHEMAS
def test_a_count_keeps_the_counted_texts(schema):
    rk = _rk()
    found = _decide(schema, _outer(schema, "COUNT(*) AS n FROM latest WHERE rn = 1"))
    guidance = rk.RecordKeyError(found).to_dict()["guidance"]
    block = rk.requirement_block([found])
    assert "Count the rows as they are" in guidance
    assert f"counted distinct values of {schema['dedupe_by']}" in block


@_SCHEMAS
@pytest.mark.parametrize("tail", [
    "SUM({value}) AS total FROM latest WHERE rn = 1",
    "AVG({value}) AS mean FROM latest WHERE rn = 1",
    "approx_percentile({value}, 0.5) AS p50 FROM latest WHERE rn = 1",
])
def test_an_aggregate_that_counts_nothing_is_not_called_a_count(schema, tail):
    rk = _rk()
    found = _decide(schema, _outer(schema, tail.format(**schema)))
    payload = rk.RecordKeyError(found).to_dict()
    guidance, block = payload["guidance"], rk.requirement_block([found])
    assert "Count the rows" not in guidance and "count distinct values" not in guidance
    assert "counted distinct values" not in block
    assert f"one row per distinct value of {schema['dedupe_by']}" in block
    assert "record key" in guidance.lower() and "separate records" in guidance
    for text in (guidance, block, rk.engine_line([found])):
        assert "\u2014" not in text and "\u2013" not in text
