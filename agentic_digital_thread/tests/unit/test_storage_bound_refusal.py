# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A bound on an Iceberg table's storage column next to another time window
is refused before the query runs.

Measured on an analytics deployment: the SQL system agent bounded the
table's partition column (a creation date) as well as the question's own
event-time window, rows created outside the extra bound were dropped, and
counts 5x to 50x too small were stated as fact. ``storage_bound_rule.decide``
refuses that query with a structured, retryable error; it does not refuse a
query that bounds only the time column, only the storage column, the column
the storage is partitioned on itself, a Hive-partitioned table (whose
partition predicate the scan guard requires) or the connector's own split
pieces, and it never rewrites the SQL.

Fully offline; synthetic names.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
from agents.chat_client import classify_error_text
from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS
from agents.runtime_integrity import _TOOL_ENUMS

from ._athena_split_fakes import FakeLake

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_COLUMNS = {"id": "varchar", "state": "varchar", "created": "timestamp(6)",
            "done_at": "timestamp(6)", "region": "varchar", "dt": "varchar"}
_WINDOW = ("done_at >= TIMESTAMP '2026-01-05 00:00:00' "
           "AND done_at < TIMESTAMP '2026-01-12 00:00:00'")
_P_AND_E = f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} AND created >= DATE '2026-01-01'"


@pytest.fixture(autouse=True)
def _fresh():
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()


class _Lake:
    """One table behind GetTableMetadata plus a query engine that answers the
    ``$partitions`` metadata read and gives every data query one row."""

    def __init__(self, *, iceberg=True, fields=("created_day",), keys=()):
        self.iceberg, self.fields, self.keys = iceberg, fields, keys
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        columns = [{"Name": n, "Type": t} for n, t in _COLUMNS.items() if n not in self.keys]
        return {"TableMetadata": {
            "Name": "events", "Columns": columns,
            "PartitionKeys": [{"Name": k, "Type": _COLUMNS[k]} for k in self.keys],
            "Parameters": {"table_type": "ICEBERG"} if self.iceberg else {}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "$partitions" in sql:
            names = ["partition"]
            rows = [["{" + ", ".join(f"{f}=1" for f in self.fields) + "}"]]
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, "partition key" if n in self.keys else ""]
                    for n, t in _COLUMNS.items()]
        else:
            names, rows = ["n"], [["42"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None

    def data_queries(self) -> list[str]:
        return [q for q in self.queries
                if "$partitions" not in q and "information_schema" not in q]


def _run(lake: _Lake, sql: str):
    return at.run_athena_query_exact(sql, _CFG, source_system="system-a", client=lake)


def _refusal(lake: _Lake, sql: str) -> dict:
    with pytest.raises(Exception) as info:
        _run(lake, sql)
    return at._tool_error(info.value)


def test_a_storage_bound_next_to_another_time_bound_is_refused_before_running():
    lake = _Lake()
    payload = _refusal(lake, _P_AND_E)
    assert payload["error"] == "storage_bound"
    assert payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "STORAGE_BOUND"
    assert payload["retryable"] is True
    assert payload["storage_columns"] == ["created"]
    assert payload["time_columns"] == ["done_at"]
    assert "Remove the bound on created" in payload["guidance"]
    # Nothing ran, and nothing was rewritten.
    assert lake.data_queries() == []


@pytest.mark.parametrize("where", [
    "created BETWEEN TIMESTAMP '2026-01-01 00:00:00' AND TIMESTAMP '2026-01-31 00:00:00' "
    "AND done_at >= DATE '2026-01-05'",
    "e.created >= TIMESTAMP '2026-01-01 00:00:00' AND e.done_at >= TIMESTAMP '2026-01-05 00:00:00'",
    "(created < DATE '2026-02-01') AND state = 'done' AND (done_at >= DATE '2026-01-05')",
])
def test_every_bound_form_the_planner_reads_is_refused(where):
    lake = _Lake()
    payload = _refusal(lake, f"SELECT COUNT(*) FROM events e WHERE {where}")
    assert payload["error_name"] == "STORAGE_BOUND" and lake.data_queries() == []


@pytest.mark.parametrize("sql", [
    # the event-time window alone
    f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} AND state = 'done'",
    # the storage column alone (no other time column)
    "SELECT COUNT(*) AS n FROM events WHERE created >= DATE '2026-01-01' AND state = 'done'",
    # a bound the planner does not read (computed, wrapped, under OR)
    f"SELECT COUNT(*) FROM events WHERE {_WINDOW} AND created >= current_date - INTERVAL '7' DAY",
    f"SELECT COUNT(*) FROM events WHERE {_WINDOW} AND date(created) = DATE '2026-01-05'",
    "SELECT COUNT(*) FROM events WHERE created >= DATE '2026-01-01' OR done_at >= DATE '2026-01-05'",
    # more than one table
    f"SELECT COUNT(*) FROM events a JOIN events b ON a.id = b.id WHERE a.{_WINDOW} "
    "AND b.created >= DATE '2026-01-01'",
])
def test_only_the_refused_shape_is_refused(sql):
    refused, lake = _Lake(), _Lake()
    assert _refusal(refused, _P_AND_E)["error_name"] == "STORAGE_BOUND"
    assert _run(lake, sql)["rows"] == [["42"]]
    assert lake.data_queries()


def test_the_storage_column_itself_as_the_time_window_is_not_refused():
    """Partitioned by the day of the event column: bounding it is the window."""
    partitioned_on_e = _Lake(fields=("done_at_day",))
    refused = _Lake()
    sql = f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} AND region = 'r1'"
    assert _refusal(refused, _P_AND_E)["error_name"] == "STORAGE_BOUND"
    assert _run(partitioned_on_e, sql)["rows"] == [["42"]]


def test_a_hive_partition_key_next_to_a_time_window_still_runs():
    """The scan guard requires a Hive table's partition predicate, so the
    rule refuses only on Iceberg tables; the bound is disclosed instead."""
    hive = _Lake(iceberg=False, keys=("dt",))
    sql = f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} AND dt >= '2026-01-01'"
    assert _refusal(_Lake(), _P_AND_E)["error_name"] == "STORAGE_BOUND"
    assert _run(hive, sql)["rows"] == [["42"]]


def test_the_connectors_own_split_pieces_are_never_refused(monkeypatch):
    """The rule reads the model's query once; the split's pieces (which add
    predicates on the split columns) never pass through it, and they run."""
    rows = []
    for day in range(20):
        for i in range(12):
            rows.append((f"id-{day}-{i}", "done", f"2026-01-{day + 1:02d} {i:02d}:10:00",
                         f"2026-01-{day + 1:02d} {i + 5:02d}:20:00", "r1", ""))
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=700_000)
    monkeypatch.setattr(at, "known_hidden_partitioning", lambda *_a, **_k: True)
    monkeypatch.setattr(at, "_cached_partition_fields", lambda *_a, **_k: ["created_day"])
    checked: list[str] = []
    real_check = at._storage_bound_check

    def counting(athena, cfg, sql, source_system):
        checked.append(sql)
        return real_check(athena, cfg, sql, source_system)

    monkeypatch.setattr(at, "_storage_bound_check", counting)
    sql = "SELECT COUNT(*) AS n FROM events WHERE done_at >= TIMESTAMP '2026-01-03 00:00:00'"
    payload = at.run_athena_query_exact(sql, _CFG, source_system="system-a", client=lake,
                                        glue_client=lake.glue)
    assert payload["exact_split"]["applied"] is True and payload["exact_split"]["pieces"] > 1
    assert checked and set(checked) == {sql}
    with pytest.raises(Exception) as info:
        at.run_athena_query_exact(_P_AND_E, _CFG, source_system="system-a", client=lake,
                                  glue_client=lake.glue)
    assert at._tool_error(info.value)["error_name"] == "STORAGE_BOUND"


def test_the_guarded_tool_returns_the_refusal_to_the_agent(monkeypatch):
    lake = _Lake()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payload = sa._scoped_athena_attempt(_P_AND_E, at._resolve_config(_CFG), {}, "system-a")
    assert payload["error_name"] == "STORAGE_BOUND"
    assert lake.data_queries() == []


def test_the_baked_partition_fields_are_used_without_a_metadata_read(monkeypatch):
    from agents import storage_bounds
    lake = _Lake(fields=())
    storage_bounds.remember("system-a", {"events": frozenset({"created"})})
    try:
        payload = _refusal(lake, _P_AND_E)
    finally:
        storage_bounds.reset()
    assert payload["storage_columns"] == ["created"]
    assert not [q for q in lake.queries if "$partitions" in q]


def test_the_refusal_text_reads_as_a_fixable_query_not_an_outage():
    payload = _refusal(_Lake(), _P_AND_E)
    text = payload["guidance"].lower()
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in text
    assert classify_error_text(payload["guidance"]) != "timeout"
    assert payload["error_type"] in _TOOL_ENUMS["safe_error_category"]
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]


# --------------------------------------------------------------------------- #
# Every predicate on a storage column, of any type
# --------------------------------------------------------------------------- #
_REGION = ("created_day", "region")


@pytest.mark.parametrize("bound", [
    "region = 'r1'",
    "region IN ('r1', 'r2')",
    "region BETWEEN 'r1' AND 'r5'",
    "region = CAST('r1' AS varchar)",
    "'r1' = e.region",
])
def test_a_bound_on_a_non_temporal_storage_column_runs_and_is_labelled(bound):
    """A user can ask for one value of a non-temporal storage column, and the
    connector cannot see the question, so it runs; the bound is labelled for
    disclosure."""
    lake = _Lake(fields=_REGION)
    result = _run(lake, f"SELECT COUNT(*) AS n FROM events e WHERE {_WINDOW} AND {bound}")
    assert result["rows"] == [["42"]]
    assert result["storage_bound"]["columns"] == ["region"]
    assert result["storage_bound"]["conjuncts"] == [bound]


def test_a_hive_key_bound_of_any_type_is_labelled():
    lake = _Lake(iceberg=False, keys=("region",))
    result = _run(lake, f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} AND region = 'r1'")
    assert result["storage_bound"]["columns"] == ["region"]


def test_two_bounded_temporal_storage_columns_are_refused():
    """Both time columns are partition sources: each bound drops the other's
    rows, so one of them is not the question's window."""
    lake = _Lake(fields=("created_day", "done_at_day"))
    payload = _refusal(lake, _P_AND_E)
    assert payload["error_name"] == "STORAGE_BOUND"
    assert payload["storage_columns"] == ["created", "done_at"]
    assert payload["time_columns"] == ["created", "done_at"]
    assert "Keep the bound on the one time column the question asks about" in payload["guidance"]
    assert lake.data_queries() == []


def test_one_bounded_temporal_storage_column_of_two_runs_and_is_labelled():
    lake = _Lake(fields=("created_day", "done_at_day"))
    result = _run(lake, f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW}")
    assert result["storage_bound"]["columns"] == ["done_at"]


def test_a_non_temporal_storage_bound_does_not_hide_a_temporal_one():
    lake = _Lake(fields=_REGION)
    payload = _refusal(lake, f"{_P_AND_E} AND region = 'r1'")
    assert payload["storage_columns"] == ["created"]
    assert payload["time_columns"] == ["done_at"]


def test_the_check_reads_each_table_from_the_catalog_once():
    class _Counting(_Lake):
        reads = 0

        def get_table_metadata(self, **kwargs):
            type(self).reads += 1
            return super().get_table_metadata(**kwargs)

    lake = _Counting(fields=_REGION)
    for _ in range(3):
        _run(lake, f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} AND region = 'r1'")
    assert _Counting.reads == 1
    assert len([q for q in lake.queries if "$partitions" in q]) == 1
