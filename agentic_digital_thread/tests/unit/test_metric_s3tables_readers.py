# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where S3 Tables counts as a SQL backend for metric reads.

The metric fan-out and the live cards read an S3 Tables system through the
metric layer's SQL path (its DuckDB dialect). The dashboard SECTION renderer
has no S3 Tables reader, so it keeps leaving that type out rather than render
a failed row per table.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.card_live as card_live
import agents.connection_types as ct
import agents.entity_render as er
import agents.metric_multisite as mms
from agents.metric_layer import Aggregation, DirectMetric, MetricLayer

CONCEPT = "concept-a"
METRIC = DirectMetric(id="m-avg", concept=CONCEPT, aggregation=Aggregation.AVG,
                      label="Avg Metric", unit="percent")
S3T = {"id": "sys-s3t", "name": "T", "plant": "site-x",
       "connection_type": ct.S3TABLES_DUCKDB}
PG = {"id": "sys-pg", "name": "P", "plant": "site-x",
      "connection_type": ct.POSTGRESQL}
VENDOR = {"id": "sys-v", "name": "V", "plant": "site-x", "connection_type": ct.TULIP}


class _Layer(MetricLayer):
    def get_metric(self, metric_id):
        if metric_id != METRIC.id:
            raise KeyError(metric_id)
        return METRIC

    def list_metrics(self):
        return [METRIC]


def test_s3_tables_is_a_metric_sql_backend():
    assert ct.S3TABLES_DUCKDB in ct.METRIC_SQL_CONNECTION_TYPES
    assert set(ct.METRIC_SQL_CONNECTION_TYPES) == {
        ct.POSTGRESQL, ct.ATHENA, ct.S3TABLES_DUCKDB}


@pytest.mark.parametrize("system,metric_ok,section_ok", [
    (S3T, True, False), (PG, True, True), (VENDOR, False, False),
    ({"connection_type": ct.ATHENA}, True, True)])
def test_reader_support(system, metric_ok, section_ok):
    assert ct.structured_read_supported(system) is metric_ok
    assert ct.section_read_supported(system) is section_ok


def test_the_metric_fan_out_includes_an_s3_tables_system():
    covered = [{"system_id": s["id"], "concept_id": CONCEPT} for s in (S3T, PG, VENDOR)]
    out = mms.systems_covering(METRIC.id, query=lambda *a, **k: covered,
                               metric_layer=_Layer(), systems=[S3T, PG, VENDOR])
    assert [e["system_id"] for e in out] == ["sys-s3t", "sys-pg"]


def test_the_section_renderer_still_skips_an_s3_tables_system(monkeypatch):
    rendered = []
    monkeypatch.setattr(er, "_render_system_sections_safe",
                        lambda system, **_k: rendered.append(system["id"]) or [])
    er.render_all_sections([S3T, PG])
    assert rendered == ["sys-pg"]


def test_an_s3_tables_card_is_a_polled_query_card(monkeypatch):
    assert ct.S3TABLES_DUCKDB in card_live.QUERY_CONNECTION_TYPES
    card = SimpleNamespace(id="c", group_by=None, metric_refs=("m",))
    monkeypatch.setattr(card_live, "_card_connection_types",
                        lambda c: {ct.S3TABLES_DUCKDB})
    assert card_live._bucket(card) == "query"
