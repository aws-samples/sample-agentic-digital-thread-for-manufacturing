# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

""": the GROUPED resolver for a steward-defined duration-PERCENTILE metric:
one row per group with each percentile and the sample count (decision 5).
Offline, graph/DB seams monkeypatched on ``metric_resolver``/``metric_dimension``."""
from __future__ import annotations

import json

import pytest

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric
from agents.metric_measure_rule import DurationMeasure, FieldRef

CONCEPT = "cycle-time"
TIME = "event-time"
INK = "ink-colour"
TABLE = "segments"

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, "start_ts"),
    end=FieldRef(TIME, "complete_ts"), unit="hours", clamp_negative=True)
_EXPR = ('greatest(extract(epoch from ("complete_ts" - "start_ts")), 0) / 3600.0')
_NOTNULL = '"start_ts" IS NOT NULL AND "complete_ts" IS NOT NULL'

_DIM = {"table": TABLE, "column": "ink_code", "role": "dimension",
        "confidence": 0.9, "source": "user", "version": "1"}
_GROUPED_RESULT = json.dumps({
    "columns": ["ink", "p", "p", "c"],
    "rows": [["alpha", 61.8, 121.4, 1000], ["beta", 215.5, 238.8, 500]],
    "row_count": 2})


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-a"


def _metric():
    return DirectMetric(id="cycle-tp", concept=CONCEPT, aggregation=Aggregation.COUNT,
                        label="Cycle Time", unit="hours",
                        measure=_MEASURE, percentiles=(0.9, 0.99))


def _wire(monkeypatch):
    registry = {"cycle-tp": _metric()}
    bound = {
        CONCEPT: [{"table": TABLE, "column": "segment_id"}],
        TIME: [{"table": TABLE, "column": "start_ts"},
               {"table": TABLE, "column": "complete_ts"}],
    }
    captured = {"sql": []}

    def _neptune(cypher, params=None):
        concept = (params or {}).get("concept")
        if cypher == mr._FIELD_TYPE_CYPHER:  # measure columns are temporal
            return [{"data_type": "timestamp(6) with time zone"}]
        if cypher == md._DIM_MAPS_TO_CYPHER and concept == INK:
            return [_DIM]
        if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER,
                      md._DIM_MAPS_TO_CYPHER):
            table = (params or {}).get("table")
            rows = bound.get(concept, [])
            pairs = sorted({(r["table"], r["column"]) for r in rows
                            if table is None or r["table"] == table})
            return [{"table": t, "column": c} for t, c in pairs]
        return []

    def _pg(schema, sql, params=None):
        captured["sql"].append(sql)
        return _GROUPED_RESULT

    monkeypatch.setattr(mr, "get_metric_layer", lambda: type("L", (), {
        "get_metric": staticmethod(lambda mid: registry[mid])})())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: "PostgreSQL")
    return captured


def test_grouped_percentile_sql_and_rows(monkeypatch):
    captured = _wire(monkeypatch)
    grm = md.resolve_metric_by_dimension("cycle-tp", INK, authority=_Auth())
    assert captured["sql"] == [
        f'SELECT "ink_code", '
        f'percentile_disc(0.9) WITHIN GROUP (ORDER BY {_EXPR}), '
        f'percentile_disc(0.99) WITHIN GROUP (ORDER BY {_EXPR}), '
        f'COUNT({_EXPR}) '
        f'FROM "plant"."{TABLE}" WHERE {_NOTNULL} '
        f'GROUP BY "ink_code" ORDER BY "ink_code" LIMIT {md.MAX_GROUPS}'
    ]
    by_group = {r.dimension_value: r for r in grm.rows}
    assert by_group["alpha"].percentiles == ((0.9, 61.8), (0.99, 121.4))
    assert by_group["alpha"].sample_count == 1000
    assert by_group["beta"].percentiles == ((0.9, 215.5), (0.99, 238.8))
    assert by_group["beta"].sample_count == 500
