# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the metric time-window seam in the grouped resolver
(Task 2D — decision-useful windowed Output-by-Line).

Covers the acceptance contract:
  * the windowed WHERE is composed ONLY when an ``event-time`` field-concept is
    mapped on the measure table (mocked mappings) — no mapping ⇒ the HONEST
    lifetime fallback (byte-identical SQL, ``window: null`` in lineage);
  * interval parameterization is SAFE: the window token comes from the CLOSED
    ``WINDOW_VOCAB`` (an unknown token fails loud before any SQL is composed)
    and the predicate column goes through ``validate_identifier`` — no
    string-interpolated user input;
  * Athena reuses the existing ``_athena_time_window_where`` seam with the
    metric's own interval, AND-combined with the partition prune;
  * the seed ``actual-quantity-windowed`` metric is additive (the existing
    ``actual-quantity`` id/semantics untouched).

All connectors/graph reads are mocked; synthetic names only.
"""
from __future__ import annotations

import pytest

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation,
    DirectMetric,
    get_metric_layer,
    validate_metric_layer,
)
from agents.metric_resolver import MetricResolverError


# --- Wiring (pattern: test_metric_dimension_athena._wire_by_dimension) -------

def _wire(
    monkeypatch,
    *,
    conn_type="PostgreSQL",
    window="24h",
    order_col="event_ts",
    joined=False,
    aggregation=Aggregation.SUM,
):
    """Wire ``resolve_metric_by_dimension`` with a synthetic windowed metric and
    mocked graph/connector seams; return a dict capturing the executed SQL."""
    metric = DirectMetric(
        id="m-output", label="Output", unit="units",
        concept="widget-count", aggregation=aggregation, window=window,
    )

    class _Layer:
        def get_metric(self, mid):
            if mid != "m-output":
                raise KeyError(mid)
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())

    auth = type("A", (), {"system_id": "sys-1", "concept": "widget-count",
                          "source": None})()
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    mapping = type("M", (), {"table": "orders", "column": "qty",
                             "role": "measure", "confidence": 0.9})()
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mapping)
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "factory")

    if joined:
        dim = type("D", (), {"table": "lines", "column": "line",
                             "role": "identifier", "confidence": 0.9})()
        join = md.JoinPathProvenance(
            join_id="j1", from_table="orders", from_field="line_id",
            to_table="lines", to_field="id",
        )
    else:
        dim = type("D", (), {"table": "orders", "column": "line",
                             "role": "identifier", "confidence": 0.9})()
        join = None
    monkeypatch.setattr(md, "_resolve_dimension_mappings", lambda *a, **k: [dim])
    monkeypatch.setattr(md, "_select_dimension_mapping", lambda *a, **k: (dim, join))

    # The event-time mapping (window column) — the SAME graph read LATEST uses.
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: order_col)
    # Keep the Athena partition-prune seam hermetic unless a test re-patches it.
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)

    captured = {}

    def fake_exec(plan, sql):
        captured["plan"], captured["sql"] = plan, sql
        return [("L1", 7.0), ("L2", 3.0)]

    monkeypatch.setattr(md, "_execute_grouped", fake_exec)
    return captured


_LIFETIME_SQL = ('SELECT "line", SUM("qty") FROM "factory"."orders" '
                 'GROUP BY "line" ORDER BY "line" LIMIT 200')


# --- Postgres windowed WHERE --------------------------------------------------

def test_windowed_postgres_composes_event_time_where(monkeypatch):
    captured = _wire(monkeypatch)
    out = md.resolve_metric_by_dimension("m-output", "widget-line")
    assert captured["sql"] == (
        'SELECT "line", SUM("qty") FROM "factory"."orders" '
        'WHERE "event_ts" >= (now() - INTERVAL \'24 hours\') '
        'GROUP BY "line" ORDER BY "line" LIMIT 200'
    )
    assert captured["plan"].window == "24h"
    assert captured["plan"].window_column == "event_ts"
    assert out.rows[0].value == 7.0


def test_windowed_postgres_joined_shape_qualifies_measure_alias(monkeypatch):
    captured = _wire(monkeypatch, joined=True)
    md.resolve_metric_by_dimension("m-output", "widget-line")
    assert (
        'WHERE m."event_ts" >= (now() - INTERVAL \'24 hours\') GROUP BY'
        in captured["sql"]
    )


def test_no_event_time_mapping_falls_back_to_lifetime_shape(monkeypatch):
    # HONEST fallback: no event-time mapped ⇒ lifetime SQL byte-identical to a
    # non-windowed resolve, window null in the plan AND in per-row lineage.
    captured = _wire(monkeypatch, order_col=None)
    out = md.resolve_metric_by_dimension("m-output", "widget-line")
    assert captured["sql"] == _LIFETIME_SQL
    assert captured["plan"].window is None
    assert captured["plan"].window_column is None
    lineage = md._row_lineage_to_dict(out.rows[0].lineage)
    assert lineage["query_plan"]["window"] is None


def test_non_windowed_metric_sql_unchanged(monkeypatch):
    # Regression: a metric WITHOUT a window (the existing registry) composes
    # exactly the pre-2D SQL — no WHERE, no behavior churn.
    captured = _wire(monkeypatch, window=None)
    md.resolve_metric_by_dimension("m-output", "widget-line")
    assert captured["sql"] == _LIFETIME_SQL
    assert captured["plan"].window is None


def test_window_rides_into_row_lineage(monkeypatch):
    _wire(monkeypatch)
    out = md.resolve_metric_by_dimension("m-output", "widget-line")
    lineage = md._row_lineage_to_dict(out.rows[0].lineage)
    assert lineage["query_plan"]["window"] == "24h"
    assert lineage["query_plan"]["window_column"] == "event_ts"


# --- Closed vocabulary / injection safety --------------------------------------

def test_unknown_window_token_fails_loud_before_sql(monkeypatch):
    captured = _wire(monkeypatch, window="24 hours'); DROP TABLE orders;--")
    with pytest.raises(MetricResolverError, match="unsupported window"):
        md.resolve_metric_by_dimension("m-output", "widget-line")
    assert "sql" not in captured  # nothing executed


def test_hostile_window_column_rejected_by_identifier_validation():
    plan = md.GroupedQueryPlan(
        system_id="sys-1", schema="factory",
        measure_table="orders", measure_column="qty",
        aggregation=Aggregation.SUM,
        dimension_table="orders", dimension_column="line",
        window="24h", window_column='evil"; DROP TABLE x;--',
    )
    with pytest.raises(ValueError, match="Invalid identifier"):
        md._pg_time_window_where(plan)


def test_window_vocab_is_closed_and_static():
    # Every interval literal is a compile-time constant — no request/user field
    # feeds the vocabulary. (Belt-and-braces pin for the nosec justification.)
    for token, (pg_literal, (qty, unit)) in md.WINDOW_VOCAB.items():
        assert isinstance(token, str) and isinstance(pg_literal, str)
        assert qty.isdigit()
        assert unit in ("HOUR", "DAY")


# --- Non-SQL backends: honest lifetime fallback --------------------------------

def test_non_sql_connection_type_skips_window(monkeypatch):
    # The predicate dialects cover Postgres/Athena only; anything else resolves
    # the lifetime shape with window null (never a wrong-dialect WHERE).
    captured = _wire(monkeypatch, conn_type="MCP Server")
    md.resolve_metric_by_dimension("m-output", "widget-line")
    assert captured["sql"] == _LIFETIME_SQL
    assert captured["plan"].window is None


# --- Athena: reuse of the existing time-window seam -----------------------------

def test_windowed_athena_uses_interval_syntax_and_ands_partition_prune(monkeypatch):
    # A non-decomposable aggregate keeps the up-front partition prune.
    captured = _wire(monkeypatch, conn_type="Athena", aggregation=Aggregation.AVG)
    monkeypatch.setattr(
        mr, "_athena_recent_partition_where",
        lambda sid, table, qualifier="": f"{qualifier}\"dt\" >= '2026-07'",
    )
    md.resolve_metric_by_dimension("m-output", "widget-line")
    assert captured["sql"] == (
        'SELECT "line", AVG("qty") FROM "factory"."orders" '
        'WHERE "dt" >= \'2026-07\' '
        'AND "event_ts" >= (current_timestamp - INTERVAL \'24\' HOUR) '
        'GROUP BY "line" ORDER BY "line" LIMIT 200'
    )
    assert captured["plan"].window == "24h"


def test_windowed_athena_decomposable_metric_runs_its_window_only(monkeypatch):
    # COUNT/SUM/MIN/MAX run as defined; the connector computes them exactly
    # past the scan limit, so no partition prune narrows them up front.
    captured = _wire(monkeypatch, conn_type="Athena")
    monkeypatch.setattr(
        mr, "_athena_recent_partition_where",
        lambda sid, table, qualifier="": f"{qualifier}\"dt\" >= '2026-07'",
    )
    md.resolve_metric_by_dimension("m-output", "widget-line")
    assert captured["sql"] == (
        'SELECT "line", SUM("qty") FROM "factory"."orders" '
        'WHERE "event_ts" >= (current_timestamp - INTERVAL \'24\' HOUR) '
        'GROUP BY "line" ORDER BY "line" LIMIT 200'
    )


def test_windowed_athena_without_partition_prune_still_windows(monkeypatch):
    captured = _wire(monkeypatch, conn_type="Athena", aggregation=Aggregation.AVG)
    md.resolve_metric_by_dimension("m-output", "widget-line")
    # No partition predicate: _prune_athena_grouped_sql falls back to the
    # 30-day second-tier bound, then the metric's 24h window ANDs on. Both
    # predicates come from the same seam (column resolved via the mocked
    # event-time mapping).
    assert (
        'WHERE "event_ts" >= (current_timestamp - INTERVAL \'30\' DAY) '
        'AND "event_ts" >= (current_timestamp - INTERVAL \'24\' HOUR) '
        "GROUP BY" in captured["sql"]
    )


def test_athena_time_window_where_default_interval_unchanged(monkeypatch):
    # The pre-2D callers (metric_resolver scalar path) pass no interval — the
    # default 30-day bound is byte-identical to before the parameter existed.
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: "event_ts")
    where = md._athena_time_window_where("sys-1", "orders")
    assert where == '"event_ts" >= (current_timestamp - INTERVAL \'30\' DAY)'


# --- _insert_measure_where ------------------------------------------------------

def test_insert_measure_where_adds_and_extends():
    base = 'SELECT "a", SUM("b") FROM "s"."t" GROUP BY "a" ORDER BY "a" LIMIT 200'
    once = md._insert_measure_where(base, '"ts" >= now()')
    assert once == ('SELECT "a", SUM("b") FROM "s"."t" WHERE "ts" >= now() '
                    'GROUP BY "a" ORDER BY "a" LIMIT 200')
    twice = md._insert_measure_where(once, '"x" = 1')
    assert twice == ('SELECT "a", SUM("b") FROM "s"."t" WHERE "ts" >= now() '
                     'AND "x" = 1 GROUP BY "a" ORDER BY "a" LIMIT 200')


def test_insert_measure_where_no_group_by_marker_unchanged():
    latest = ('SELECT DISTINCT ON ("d") "d", "m" FROM "s"."t" '
              'ORDER BY "d", "ts" DESC LIMIT 200')
    assert md._insert_measure_where(latest, '"ts" >= now()') == latest


# --- Seed registry (metric_layer) ------------------------------------------------

def test_seed_actual_quantity_windowed_is_additive():
    layer = get_metric_layer()
    windowed = layer.get_metric("actual-quantity-windowed")
    assert isinstance(windowed, DirectMetric)
    assert windowed.concept == "actual-quantity"
    assert windowed.aggregation is Aggregation.SUM
    assert windowed.window == "24h"
    assert windowed.window in md.WINDOW_VOCAB  # token from the closed vocabulary
    # The existing lifetime metric is untouched (no id churn).
    lifetime = layer.get_metric("actual-quantity")
    assert lifetime.window is None
    assert lifetime.aggregation is Aggregation.SUM
    assert validate_metric_layer() is None


def test_windowed_metric_satisfiable_iff_concept_covered():
    layer = get_metric_layer()
    m = layer.get_metric("actual-quantity-windowed")
    assert layer.is_satisfiable(m, frozenset({"actual-quantity"})) is True
    assert layer.is_satisfiable(m, frozenset()) is False
