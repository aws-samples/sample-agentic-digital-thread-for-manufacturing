# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for Athena partition pruning (scan-cost guard).

LIVE-CONFIRMED problem: an unpruned full-table GROUP BY over a large partitioned
Athena table is CANCELLED by the workgroup's bytes-scanned limit. The fix
restricts the composed metric SQL (grouped AND scalar) to the table's N most
recent partitions, discovered GENERICALLY:

* partition COLUMNS via ``information_schema.columns`` (``extra_info='partition
  key'`` — the Hive connector marker), cached per physical table for the process
  lifetime;
* recent partition VALUES via the metadata-only ``"<table>$partitions"`` view
  (never scans table data).

Strictly best-effort: an unpartitioned table or any discovery failure leaves the
SQL byte-identical to today (the limit error stays an honest env-side error).

The metric cases here use AVG: narrowing up front now applies only to an
aggregate the connector cannot compute exactly from partition pieces. A
COUNT/SUM/MIN/MAX metric runs as defined (``test_metric_exact_split.py``).
Fully offline — ``athena_tools.run_athena_query`` is stubbed (NO AWS); synthetic
names only.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents import connection_types as ct
from agents.metric_layer import Aggregation, CodeMetricLayer, DirectMetric


_CFG = {"workgroup": "wg", "database": "lake_db"}


@pytest.fixture(autouse=True)
def _clear_partition_cache():
    """Partition-column discovery is cached per process — isolate every test."""
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at._PARTITION_COLUMNS_CACHE.clear()


class _FakeAthena:
    """Router stub for ``run_athena_query``: answers the two metadata queries
    (information_schema.columns / ``$partitions``) and records+answers the final
    data query. Call log exposed for cache/no-metadata assertions."""

    def __init__(self, *, partition_cols=None, partition_rows=None,
                 data_result=None, fail_metadata=False, fail_partitions=False):
        # information_schema rows: [column_name, data_type, extra_info]
        self.partition_cols = partition_cols or []
        # $partitions rows: one list per recent partition (VarChar strings)
        self.partition_rows = partition_rows or []
        self.data_result = data_result or {"columns": [], "rows": [], "row_count": 0}
        self.fail_metadata = fail_metadata
        self.fail_partitions = fail_partitions
        self.metadata_calls: list[str] = []
        self.partitions_calls: list[str] = []
        self.data_calls: list[str] = []

    def __call__(self, sql, connection_config, **kwargs):
        if "information_schema.columns" in sql:
            self.metadata_calls.append(sql)
            if self.fail_metadata:
                raise RuntimeError("information_schema access denied")
            return {"columns": ["column_name", "data_type", "extra_info"],
                    "rows": self.partition_cols,
                    "row_count": len(self.partition_cols)}
        if "$partitions" in sql:
            self.partitions_calls.append(sql)
            if self.fail_partitions:
                raise RuntimeError("$partitions unavailable")
            return {"columns": ["p"], "rows": self.partition_rows,
                    "row_count": len(self.partition_rows)}
        self.data_calls.append(sql)
        return self.data_result


# ===========================================================================
# (A) build_recent_partition_predicate — the shared generic helper
# ===========================================================================

def test_single_string_partition_column_in_predicate(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"], ["qty", "bigint", ""]],
        partition_rows=[["2026-07-14"], ["2026-07-13"], ["2026-07-12"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    pred = at.build_recent_partition_predicate("orders", _CFG)
    assert pred == '"dt" IN (\'2026-07-14\', \'2026-07-13\', \'2026-07-12\')'
    # The values query hits the metadata-only $partitions view, most-recent-first,
    # bounded by the module constant.
    assert len(fake.partitions_calls) == 1
    values_sql = fake.partitions_calls[0]
    assert '"lake_db"."orders$partitions"' in values_sql
    assert '"dt" DESC' in values_sql
    assert f"LIMIT {at.RECENT_PARTITIONS_LIMIT}" in values_sql


def test_numeric_partition_values_emitted_bare(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["year", "int", "partition key"]],
        partition_rows=[["2026"], ["2025"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("orders", _CFG) == '"year" IN (2026, 2025)'


def test_date_partition_values_emitted_as_typed_literals(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["day", "date", "partition key"]],
        partition_rows=[["2026-07-14"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("orders", _CFG) == \
        "\"day\" IN (DATE '2026-07-14')"


def test_multi_column_partitions_or_of_conjunctions(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["year", "int", "partition key"],
                        ["month", "int", "partition key"]],
        partition_rows=[["2026", "7"], ["2026", "6"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    pred = at.build_recent_partition_predicate("orders", _CFG)
    assert pred == ('(("year" = 2026 AND "month" = 7) OR '
                    '("year" = 2026 AND "month" = 6))')


def test_partition_values_with_quotes_are_escaped(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["site", "string", "partition key"]],
        partition_rows=[["O'Brien"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("t", _CFG) == "\"site\" IN ('O''Brien')"


def test_qualifier_prefixes_column_references(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    pred = at.build_recent_partition_predicate("orders", _CFG, qualifier="m.")
    assert pred == 'm."dt" IN (\'2026-07-14\')'


def test_unpartitioned_table_yields_none_and_skips_partitions_view(monkeypatch):
    fake = _FakeAthena(partition_cols=[["qty", "bigint", ""], ["id", "string", ""]])
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("orders", _CFG) is None
    assert fake.partitions_calls == []      # no $partitions read for unpartitioned


def test_metadata_failure_yields_none_and_is_not_cached(monkeypatch):
    fake = _FakeAthena(fail_metadata=True)
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("orders", _CFG) is None
    assert at.build_recent_partition_predicate("orders", _CFG) is None
    # a transient failure retries (NOT cached) and never raises
    assert len(fake.metadata_calls) == 2


def test_partitions_view_failure_yields_none(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]], fail_partitions=True,
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("orders", _CFG) is None


def test_non_numeric_value_for_numeric_type_fails_open(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["year", "int", "partition key"]],
        partition_rows=[["__HIVE_DEFAULT_PARTITION__"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    assert at.build_recent_partition_predicate("orders", _CFG) is None


def test_partition_columns_cached_once_per_table(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    at.build_recent_partition_predicate("orders", _CFG)
    at.build_recent_partition_predicate("orders", _CFG)
    assert len(fake.metadata_calls) == 1     # column discovery cached per table
    assert len(fake.partitions_calls) == 2   # values are data — re-read each time


def test_cache_key_distinguishes_tables(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    at.build_recent_partition_predicate("orders", _CFG)
    at.build_recent_partition_predicate("shipments", _CFG)
    assert len(fake.metadata_calls) == 2     # one discovery per distinct table


# ===========================================================================
# (B) Grouped path (metric_dimension) — pruned WHERE in executed SQL + lineage
# ===========================================================================

_GROUPED_RESULT = {"columns": ["status", "sum"],
                   "rows": [["CLOSED", 5], ["OPEN", 7]], "row_count": 2}


def _wire_grouped(monkeypatch, fake, *, conn_type=ct.ATHENA, join=None,
                  dim_table="orders", time_column=None):
    """Wire resolve_metric_by_dimension end-to-end with a stubbed connector."""
    metric = DirectMetric(id="m-widgets", label="Widgets", unit="count",
                          concept="widget-count", aggregation=Aggregation.AVG)
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    auth = type("A", (), {"system_id": "sys-lake", "concept": "widget-count",
                          "source": None})()
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    mapping = type("M", (), {"table": "orders", "column": "qty",
                             "role": "measure", "confidence": 0.9})()
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mapping)
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": conn_type})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))
    dim = type("D", (), {"table": dim_table, "column": "status",
                         "role": "identifier", "confidence": 0.9})()
    monkeypatch.setattr(md, "_resolve_dimension_mappings", lambda *a, **k: [dim])
    monkeypatch.setattr(md, "_select_dimension_mapping", lambda *a, **k: (dim, join))
    # Hermetic graph seam for the time-window fallback's _resolve_order_column:
    # event-time maps to `time_column` on the measure table (or nothing).
    monkeypatch.setattr(
        mr, "neptune_query",
        lambda cypher, params=None: (
            [{"table": "orders", "column": time_column}]
            if time_column and (params or {}).get("concept") == md.TIME_CONCEPT
            else []),
    )
    monkeypatch.setattr(mr.athena_tools, "run_athena_query", fake)


def test_grouped_athena_partitioned_table_gets_pruned_where(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"], ["2026-07-13"]],
        data_result=_GROUPED_RESULT,
    )
    _wire_grouped(monkeypatch, fake)
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    assert len(fake.data_calls) == 1
    executed = fake.data_calls[0]
    assert executed == (
        'SELECT "status", AVG("qty") FROM "lake_db"."orders" '
        'WHERE "dt" IN (\'2026-07-14\', \'2026-07-13\') '
        'GROUP BY "status" ORDER BY "status" LIMIT 200'
    )
    # Lineage records the FINAL executed SQL (the pruned one), per row.
    assert out.rows[0].lineage.sql == executed
    assert out.rows[1].lineage.sql == executed
    assert [(r.dimension_value, r.value) for r in out.rows] == \
        [("CLOSED", 5), ("OPEN", 7)]


def test_grouped_athena_joined_shape_qualifies_measure_alias(monkeypatch):
    join = md.JoinPathProvenance(
        join_id="jp-1", from_table="orders", from_field="status_id",
        to_table="statuses", to_field="id",
    )
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"]],
        data_result=_GROUPED_RESULT,
    )
    _wire_grouped(monkeypatch, fake, join=join, dim_table="statuses")
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    executed = fake.data_calls[0]
    assert 'WHERE m."dt" IN (\'2026-07-14\') GROUP BY' in executed
    assert executed.index("JOIN") < executed.index("WHERE")   # WHERE after the JOIN
    assert out.rows[0].lineage.sql == executed


def test_grouped_athena_unpartitioned_table_sql_unchanged(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["qty", "bigint", ""]],   # no partition keys
        data_result=_GROUPED_RESULT,
    )
    _wire_grouped(monkeypatch, fake)
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    executed = fake.data_calls[0]
    assert executed == ('SELECT "status", AVG("qty") FROM "lake_db"."orders" '
                        'GROUP BY "status" ORDER BY "status" LIMIT 200')
    assert "WHERE" not in executed
    assert out.rows[0].lineage.sql == executed


def test_grouped_athena_discovery_failure_sql_unchanged_no_crash(monkeypatch):
    fake = _FakeAthena(fail_metadata=True, data_result=_GROUPED_RESULT)
    _wire_grouped(monkeypatch, fake)
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    executed = fake.data_calls[0]
    assert "WHERE" not in executed              # honest unpruned query
    assert out.rows[0].lineage.sql == executed  # lineage still the executed SQL


def test_grouped_postgres_path_never_consults_pruning(monkeypatch):
    fake = _FakeAthena()   # must never be called at all
    _wire_grouped(monkeypatch, fake, conn_type=ct.POSTGRESQL)
    monkeypatch.setattr(
        mr, "_athena_recent_partition_where",
        lambda *a, **k: pytest.fail("pruning must NOT run for PostgreSQL"),
    )
    import json as _json
    captured = {}

    def fake_pg(schema, sql):
        captured["sql"] = sql
        return _json.dumps({"rows": [["OPEN", 3]], "row_count": 1})

    monkeypatch.setattr(mr, "query_pg", fake_pg)
    md.resolve_metric_by_dimension("m-widgets", "widget-state")
    assert captured["sql"] == ('SELECT "status", AVG("qty") FROM "lake_db"."orders" '
                               'GROUP BY "status" ORDER BY "status" LIMIT 200')


# ===========================================================================
# (C) Scalar path (metric_resolver) — same guard via the SHARED helper
# ===========================================================================

def _wire_scalar(monkeypatch, fake, *, conn_type=ct.ATHENA, time_column=None):
    metric = DirectMetric(id="m-widgets", label="Widgets", unit="count",
                          concept="widget-count", aggregation=Aggregation.AVG)
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))

    def _nq(cypher, params=None):
        # Concept-aware: the event-time concept drives the time-window fallback;
        # everything else is the measure MAPS_TO row.
        if (params or {}).get("concept") == md.TIME_CONCEPT:
            return ([{"table": "orders", "column": time_column}]
                    if time_column else [])
        return [{"table": "orders", "column": "qty"}]

    monkeypatch.setattr(mr, "neptune_query", _nq)
    auth = type("A", (), {"system_id": "sys-lake", "concept": "widget-count",
                          "source": None})()
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": conn_type})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))
    monkeypatch.setattr(mr.athena_tools, "run_athena_query", fake)


def test_scalar_athena_partitioned_table_gets_pruned_where(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"], ["2026-07-13"]],
        data_result={"columns": ["_col0"], "rows": [["42"]], "row_count": 1},
    )
    _wire_scalar(monkeypatch, fake)
    resolved = mr.resolve_metric("m-widgets")
    executed = fake.data_calls[0]
    assert executed == ("SELECT AVG(qty), COUNT(qty) FROM lake_db.orders "
                        "WHERE \"dt\" IN ('2026-07-14', '2026-07-13')")
    assert resolved.value == 42
    assert resolved.lineage.sql == executed     # lineage carries the PRUNED SQL


def test_scalar_athena_unpartitioned_sql_unchanged(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["qty", "bigint", ""]],
        data_result={"columns": ["_col0"], "rows": [["42"]], "row_count": 1},
    )
    _wire_scalar(monkeypatch, fake)
    resolved = mr.resolve_metric("m-widgets")
    assert fake.data_calls[0] == "SELECT AVG(qty), COUNT(qty) FROM lake_db.orders"
    assert resolved.lineage.sql == "SELECT AVG(qty), COUNT(qty) FROM lake_db.orders"


def test_scalar_athena_discovery_failure_sql_unchanged_no_crash(monkeypatch):
    fake = _FakeAthena(
        fail_metadata=True,
        data_result={"columns": ["_col0"], "rows": [["42"]], "row_count": 1},
    )
    _wire_scalar(monkeypatch, fake)
    resolved = mr.resolve_metric("m-widgets")
    assert fake.data_calls[0] == "SELECT AVG(qty), COUNT(qty) FROM lake_db.orders"
    assert resolved.value == 42


def test_shared_helper_swallows_config_resolution_failure(monkeypatch):
    def boom(sid):
        raise RuntimeError("catalog unavailable")
    monkeypatch.setattr(mr, "_full_connection_config", boom)
    assert mr._athena_recent_partition_where("sys-lake", "orders") is None


# ===========================================================================
# (D) Second-tier fallback — time-window WHERE on the mapped event-time column
# LIVE-CONFIRMED: a table with NO partition predicate (unpartitioned / discovery
# failed) still runs the unbounded aggregate and is CANCELLED by the workgroup's
# bytes-scanned limit. When the graph maps the event-time concept on the measure
# table, the composed SQL is bounded to the last ATHENA_TIME_WINDOW_DAYS days;
# with no time column mapped either, behavior is UNCHANGED (honest limit error —
# covered by the *_sql_unchanged tests above).
# ===========================================================================

_TIME_WINDOW = (f'"event_ts" >= (current_timestamp - '
                f"INTERVAL '{md.ATHENA_TIME_WINDOW_DAYS}' DAY)")


def test_time_window_constant_is_days_bound():
    assert md.ATHENA_TIME_WINDOW_DAYS == 30


def test_scalar_no_partition_predicate_falls_back_to_time_window(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["qty", "bigint", ""]],     # unpartitioned
        data_result={"columns": ["_col0"], "rows": [["42"]], "row_count": 1},
    )
    _wire_scalar(monkeypatch, fake, time_column="event_ts")
    resolved = mr.resolve_metric("m-widgets")
    executed = fake.data_calls[0]
    assert executed == (f"SELECT AVG(qty), COUNT(qty) FROM lake_db.orders "  # nosec B608 - test fixture composing expected SQL from module constants
                        f"WHERE {_TIME_WINDOW}")
    assert resolved.value == 42
    assert resolved.lineage.sql == executed     # lineage carries the FINAL SQL


def test_scalar_partition_predicate_wins_over_time_window(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"]],
        data_result={"columns": ["_col0"], "rows": [["42"]], "row_count": 1},
    )
    _wire_scalar(monkeypatch, fake, time_column="event_ts")
    mr.resolve_metric("m-widgets")
    executed = fake.data_calls[0]
    assert '"dt" IN' in executed                # first tier applied
    assert "event_ts" not in executed           # second tier NOT stacked on top


def test_grouped_no_partition_predicate_falls_back_to_time_window(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["qty", "bigint", ""]],     # unpartitioned
        data_result=_GROUPED_RESULT,
    )
    _wire_grouped(monkeypatch, fake, time_column="event_ts")
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    executed = fake.data_calls[0]
    assert executed == (
        f'SELECT "status", AVG("qty") FROM "lake_db"."orders" '  # nosec B608 - test fixture composing expected SQL from module constants
        f'WHERE {_TIME_WINDOW} '
        f'GROUP BY "status" ORDER BY "status" LIMIT 200'
    )
    assert out.rows[0].lineage.sql == executed  # per-row lineage = executed SQL


def test_grouped_joined_time_window_qualifies_measure_alias(monkeypatch):
    join = md.JoinPathProvenance(
        join_id="jp-1", from_table="orders", from_field="status_id",
        to_table="statuses", to_field="id",
    )
    fake = _FakeAthena(
        partition_cols=[["qty", "bigint", ""]],
        data_result=_GROUPED_RESULT,
    )
    _wire_grouped(monkeypatch, fake, join=join, dim_table="statuses",
                  time_column="event_ts")
    out = md.resolve_metric_by_dimension("m-widgets", "widget-state")
    executed = fake.data_calls[0]
    assert f'WHERE m.{_TIME_WINDOW} GROUP BY' in executed
    assert executed.index("JOIN") < executed.index("WHERE")
    assert out.rows[0].lineage.sql == executed


def test_grouped_partition_predicate_wins_over_time_window(monkeypatch):
    fake = _FakeAthena(
        partition_cols=[["dt", "string", "partition key"]],
        partition_rows=[["2026-07-14"]],
        data_result=_GROUPED_RESULT,
    )
    _wire_grouped(monkeypatch, fake, time_column="event_ts")
    md.resolve_metric_by_dimension("m-widgets", "widget-state")
    executed = fake.data_calls[0]
    assert '"dt" IN' in executed
    assert "event_ts" not in executed


def test_time_window_helper_none_when_no_time_mapping():
    assert md._athena_time_window_where(
        "sys-lake", "orders", query=lambda c, p=None: []) is None


def test_time_window_helper_none_on_invalid_identifier():
    # A malformed mapped column (would not validate) fails open, never raises.
    bad = lambda c, p=None: [{"table": "orders", "column": 'evil"col'}]  # noqa: E731
    assert md._athena_time_window_where("sys-lake", "orders", query=bad) is None


def test_time_window_helper_qualifier_and_shape():
    q = lambda c, p=None: [{"table": "orders", "column": "event_ts"}]  # noqa: E731
    assert md._athena_time_window_where("sys-lake", "orders", query=q) == _TIME_WINDOW
    assert md._athena_time_window_where(
        "sys-lake", "orders", qualifier="m.", query=q) == f"m.{_TIME_WINDOW}"


# --------------------------------------------------------------------------- #
# Date-literal dialect rule. Measured on a live deployment: 8 of 10 failed
# Athena queries were "TYPE_MISMATCH: Cannot apply operator: timestamp(6) <=
# varchar(10)" — a temporal column compared to a bare string. The equivalent
# rule existed only in connection_types._ATHENA_GUIDANCE, which is a
# provision-time FALLBACK that explicit registration guidance displaces, and is
# read by the orchestrator rather than the agent writing the SQL.
# --------------------------------------------------------------------------- #
import agents.system_agent as _sa


def test_sql_engines_get_the_date_literal_rule():
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._temporal_literal_prompt(ct)
        assert "DATE FILTERS (required)" in rule
        assert "TIMESTAMP '" in rule and "DATE '" in rule
        assert "half-open" in rule
        assert "defeats partition pruning" in rule


def test_athena_states_the_failure_is_hard_not_merely_fragile():
    assert "FAILS on this engine with TYPE_MISMATCH" in _sa._temporal_literal_prompt("Athena")


def test_non_sql_connection_types_get_no_sql_rule():
    for ct in ("MCP Server", "SiteWise (Direct)", "SiteWise (i3X)", ""):
        assert _sa._temporal_literal_prompt(ct) == ""


def test_policy_tail_carries_the_rule_for_athena_only():
    assert "DATE FILTERS (required)" in _sa._policy_tail("Athena")
    assert "DATE FILTERS (required)" not in _sa._policy_tail("SiteWise (Direct)")
    # the pre-existing policy text must survive alongside it
    assert "SELF-RECOVERY (required)" in _sa._policy_tail("Athena")


def test_sql_engines_get_the_subquery_projection_rule():
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._subquery_projection_prompt(ct)
        assert "SUBQUERY PROJECTION (required)" in rule
        assert "COLUMN_NOT_FOUND" in rule
    for ct in ("MCP Server", "SiteWise (Direct)", ""):
        assert _sa._subquery_projection_prompt(ct) == ""


def test_policy_tail_carries_both_sql_rules_for_athena():
    tail = _sa._policy_tail("Athena")
    assert "DATE FILTERS (required)" in tail
    assert "SUBQUERY PROJECTION (required)" in tail
    assert "SELF-RECOVERY (required)" in tail
    sw = _sa._policy_tail("SiteWise (Direct)")
    assert "SUBQUERY PROJECTION" not in sw and "DATE FILTERS" not in sw


def test_sql_engines_get_the_fanout_total_rule():
    """SUM(DISTINCT)/MAX over a join fan-out silently drops records; the
    rule tells the SQL-writing agent to sum at the value's own grain
    (rehearsal 2026-10-02: one question gave $33,625.40, $24,480 and the
    correct total across three runs)."""
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._fanout_total_prompt(ct)
        assert "TOTALS ACROSS JOINS (required)" in rule
        assert "SUM(DISTINCT" in rule and "MAX(" in rule
        assert "TOTALS ACROSS JOINS (required)" in _sa._policy_tail(ct)
    for ct in ("MCP Server", "SiteWise (Direct)", ""):
        assert _sa._fanout_total_prompt(ct) == ""
        assert "TOTALS ACROSS JOINS" not in _sa._policy_tail(ct)


def test_athena_gets_the_dotted_column_rule():
    """Dotted flattened physical columns must be referenced as ONE double-quoted
    identifier — observed live as a repeated COLUMN_NOT_FOUND the agent could
    not self-recover from (the error names the very column the schema lists).
    Athena-only: PostgreSQL schemas here never carry dotted physical columns."""
    rule = _sa._dotted_column_prompt("Athena")
    assert "DOTTED COLUMN NAMES (required)" in rule
    assert 'ONE double-quoted identifier' in rule
    assert "COLUMN_NOT_FOUND" in rule
    # the serialized-empty-collection trap rides along: '[]' is neither NULL nor ''
    assert "'[]'" in rule
    for ct in ("PostgreSQL", "MCP Server", "SiteWise (Direct)", ""):
        assert _sa._dotted_column_prompt(ct) == ""


def test_the_dotted_column_rule_groups_json_text_by_a_field_inside_it():
    """A serialized JSON value can be stored with its keys in several orders,
    so grouping by its text counts one value once per spelling. The rule says
    so and gives the element expansion; it no longer forbids UNNEST, which the
    IDENTIFIER FORMAT rule prescribes for the same values."""
    rule = _sa._dotted_column_prompt("Athena")
    assert "never UNNEST it" not in rule
    assert "struct path" in rule
    assert "never GROUP BY, SELECT DISTINCT or COUNT(DISTINCT) the text itself" in rule
    expansion, element = _sa._LIST_EXPANSION["Athena"]
    assert expansion in rule and f"GROUP BY {element}" in rule
    assert "once per element" in rule
    assert "json_format" not in rule


def test_policy_tail_carries_the_dotted_column_rule_for_athena_only():
    assert "DOTTED COLUMN NAMES (required)" in _sa._policy_tail("Athena")
    assert "DOTTED COLUMN NAMES" not in _sa._policy_tail("PostgreSQL")
    assert "DOTTED COLUMN NAMES" not in _sa._policy_tail("SiteWise (Direct)")


# --------------------------------------------------------------------------- #
# Inclusive END DATE -> exclusive next-day bound.
#
# The existing rule covers a SINGLE calendar day. A multi-day "through <end>"
# window written as `col <= DATE 'e'` silently drops every row timestamped
# after midnight on the end day, so a count comes back short while the answer
# still claims the asked window. `col < DATE 'e'` drops the end day whole.
# One generic expression, pinned on BOTH SQL engines.
# --------------------------------------------------------------------------- #
_INCLUSIVE_END_EXPR = "col < DATE 'e' + INTERVAL '1' DAY"


def test_sql_engines_get_the_inclusive_end_date_expression():
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._temporal_literal_prompt(ct)
        assert _INCLUSIVE_END_EXPR in rule, ct
        assert "INCLUSIVE" in rule, ct


def test_both_wrong_inclusive_end_forms_are_explicitly_rejected():
    # Naming ONLY the correct form leaves the agent free to keep writing the
    # wrong one; both failure modes are named as failures.
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._temporal_literal_prompt(ct)
        assert "NEVER write col <= DATE 'e'" in rule, ct
        assert "NEVER write col < DATE 'e' " in rule, ct


def test_policy_tail_carries_the_inclusive_end_expression_for_sql_only():
    for ct in ("Athena", "PostgreSQL"):
        assert _INCLUSIVE_END_EXPR in _sa._policy_tail(ct), ct
    for ct in ("MCP Server", "SiteWise (Direct)", "SiteWise (i3X)"):
        assert _INCLUSIVE_END_EXPR not in _sa._policy_tail(ct), ct


# --------------------------------------------------------------------------- #
# OBSERVED VALUE SHAPE — "what do the values look like" must be READ.
#
# Value format/shape/family answers are only true if the values were queried.
# Names, declared types, schema example values, steward business context and
# concept/value mappings are METADATA: they can locate the field, never
# establish what is stored in it. Inferring shape from them produces a fluent,
# confident, ungrounded answer.
# --------------------------------------------------------------------------- #
def test_sql_engines_get_the_observed_value_shape_rule():
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._observed_value_shape_prompt(ct)
        assert "OBSERVED VALUE SHAPE (required)" in rule, ct
        # Filtered aggregation over the TARGET column, NULLs excluded.
        assert "GROUP BY target_column" in rule, ct
        assert "target_column IS NOT NULL" in rule, ct
        assert "COUNT(*)" in rule, ct
        # Representative observed values quoted exactly.
        assert "EXACTLY" in rule and "'status_a'" in rule, ct


def test_observed_value_shape_rule_denies_metadata_as_evidence():
    for ct in ("Athena", "PostgreSQL"):
        rule = _sa._observed_value_shape_prompt(ct)
        assert "CANNOT establish value shape" in rule, ct
        for source in ("declared type", "schema example", "business context",
                       "concept mapping", "value mapping"):
            assert source in rule, (ct, source)
        # They may still LOCATE the field — the rule narrows, never blinds.
        assert "LOCATE" in rule, ct


def test_non_sql_connection_types_get_no_observed_value_shape_rule():
    for ct in ("MCP Server", "SiteWise (Direct)", "SiteWise (i3X)",
               "REST API", ""):
        assert _sa._observed_value_shape_prompt(ct) == "", ct
        assert "OBSERVED VALUE SHAPE" not in _sa._policy_tail(ct), ct


def test_policy_tail_carries_the_observed_value_shape_rule_for_sql():
    for ct in ("Athena", "PostgreSQL"):
        tail = _sa._policy_tail(ct)
        assert "OBSERVED VALUE SHAPE (required)" in tail, ct
        # The pre-existing policy text survives alongside it.
        assert "SELF-RECOVERY (required)" in tail, ct
        assert "DATE FILTERS (required)" in tail, ct
        assert "SUBQUERY PROJECTION (required)" in tail, ct
