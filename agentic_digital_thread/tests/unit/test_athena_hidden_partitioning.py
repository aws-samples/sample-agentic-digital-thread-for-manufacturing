# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An Iceberg table has hidden partitioning; it is never "known unpartitioned".

Measured on an analytics deployment: the catalog reported the table with
``Parameters.table_type = ICEBERG`` and no declared ``PartitionKeys``. Iceberg
partitions by transforms of ordinary columns (a day of a timestamp, a region
value), so the catalog lists no partition key while the storage is partitioned.
The app read "zero partition keys" as "unpartitioned": onboarding baked the
``(no partition keys)`` marker into the schema line, and the scan guard refused
an uncertain-parse query with "the table is known to be unpartitioned".

The fix, pinned here (fully offline, fake Athena clients, neutral names):

1. The catalog read that already describes a table (GetTableMetadata) records
   whether it is an Iceberg table; no extra call.
2. The scan guard reports such a table as ``hidden_partitioned`` on the live
   and the baked path, runs a plain query unchanged and refuses an uncertain
   parse with wording that never claims the table is unpartitioned.
3. Onboarding renders ``(hidden partitioning by <fields>)`` instead of
   ``(no partition keys)``, reading the fields from the ``$partitions``
   metadata table at the point the schema is described (fail soft), and the
   agent-side parser and prompt read that marker back.
"""
from __future__ import annotations

import types

import pytest

import agents.athena_tools as at
import agents.onboard as onboard
import agents.schema_format as schema_format
import agents.system_agent as sa
from agents.connection_types import ATHENA
from agents.models import ProvisionRequest

_CFG = {"workgroup": "wg_a", "database": "db_a", "region": "us-east-1"}
_NO_PART = schema_format.PARTITION_KEYS_ABSENT_MARKER


@pytest.fixture(autouse=True)
def _clear_caches():
    at._PARTITION_COLUMNS_CACHE.clear()
    at.clear_schema_cache()
    yield
    at._PARTITION_COLUMNS_CACHE.clear()
    at.clear_schema_cache()


def _iceberg_meta(name: str) -> dict:
    return {
        "Name": name, "TableType": "EXTERNAL_TABLE",
        "Columns": [{"Name": "id", "Type": "string"},
                    {"Name": "event_time", "Type": "timestamp"},
                    {"Name": "region_key", "Type": "string"}],
        "PartitionKeys": [],
        "Parameters": {"table_type": "ICEBERG",
                       "metadata_location": "s3://amzn-s3-demo-bucket-a/metadata/v1.json"},
    }


def _hive_meta(name: str) -> dict:
    return {
        "Name": name, "TableType": "EXTERNAL_TABLE",
        "Columns": [{"Name": "id", "Type": "string"}],
        "Parameters": {"classification": "parquet"},
    }


class _FakeAthena:
    """GetTableMetadata from a table map; any query answers the ``$partitions`` probe."""

    def __init__(self, tables: dict, partition_value: str | None = None,
                 probe_fails: bool = False):
        self.tables = tables
        self.partition_value = partition_value
        self.probe_fails = probe_fails
        self.queries: list[str] = []

    def get_table_metadata(self, CatalogName, DatabaseName, TableName):
        if TableName not in self.tables:
            raise RuntimeError("EntityNotFoundException: Entity Not Found")
        return {"TableMetadata": self.tables[TableName]}

    def start_query_execution(self, **kwargs):
        self.queries.append(kwargs.get("QueryString", ""))
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        if self.probe_fails:
            return {"QueryExecution": {"Status": {
                "State": "FAILED", "StateChangeReason": "TABLE_NOT_FOUND: no such table"}}}
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def stop_query_execution(self, QueryExecutionId):
        pass

    def get_query_results(self, **_kwargs):
        rows = [{"Data": [{"VarCharValue": "partition"}]}]
        if self.partition_value is not None:
            rows.append({"Data": [{"VarCharValue": self.partition_value}]})
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "partition"}]}, "Rows": rows}}


def _live_discovery_through(monkeypatch, fake: _FakeAthena) -> None:
    """The guard's live lookup runs the REAL introspection against ``fake``."""
    real = at.list_partition_columns
    monkeypatch.setattr(at, "list_partition_columns",
                        lambda table, cfg, **_kw: real(table, cfg, client=fake))


# --------------------------------------------------------------------------- #
# 1. The catalog read records the table format
# --------------------------------------------------------------------------- #
def test_the_catalog_read_records_an_iceberg_table_as_hidden_partitioned():
    fake = _FakeAthena({"events_a": _iceberg_meta("events_a"),
                        "plain_a": _hive_meta("plain_a")})
    cfg = at._resolve_config(_CFG)
    at._table_columns_via_metadata_api(fake, cfg, "events_a")
    at._table_columns_via_metadata_api(fake, cfg, "plain_a")
    assert at.known_hidden_partitioning("events_a", _CFG) is True
    assert at.known_hidden_partitioning("plain_a", _CFG) is False
    # Never read in this process: not known, so never claimed.
    assert at.known_hidden_partitioning("never_read", _CFG) is False
    assert fake.queries == []  # the format rides the describe; no query ran


def test_the_table_format_is_read_case_insensitively():
    meta = _iceberg_meta("events_b")
    meta["Parameters"] = {"TABLE_TYPE": "iceberg"}
    fake = _FakeAthena({"events_b": meta})
    at._table_columns_via_metadata_api(fake, at._resolve_config(_CFG), "events_b")
    assert at.known_hidden_partitioning("events_b", _CFG) is True


def test_clearing_the_schema_cache_forgets_the_table_format():
    fake = _FakeAthena({"events_c": _iceberg_meta("events_c")})
    at._table_columns_via_metadata_api(fake, at._resolve_config(_CFG), "events_c")
    at.clear_schema_cache()
    assert at.known_hidden_partitioning("events_c", _CFG) is False


# --------------------------------------------------------------------------- #
# 2. The scan guard
# --------------------------------------------------------------------------- #
def test_live_guard_never_calls_an_iceberg_table_known_unpartitioned(monkeypatch):
    _live_discovery_through(monkeypatch, _FakeAthena({"events_d": _iceberg_meta("events_d")}))
    sql, decision = sa._scope_athena_sql("SELECT count(*) FROM events_d;", _CFG)
    assert sql == "SELECT count(*) FROM events_d;"
    assert decision["reason"] == "scope_required"
    assert decision["partition_metadata"] == "hidden_partitioned"


def test_a_plain_query_on_an_iceberg_table_runs_unchanged_and_says_why(monkeypatch):
    _live_discovery_through(monkeypatch, _FakeAthena({"events_e": _iceberg_meta("events_e")}))
    original = "SELECT count(*) FROM events_e WHERE event_time >= TIMESTAMP '2026-01-01 00:00:00'"
    sql, decision = sa._scope_athena_sql(original, _CFG)
    assert sql == original
    assert decision["applied"] is False
    assert decision["reason"] == "hidden_partitioning"
    assert decision["partition_metadata"] == "hidden_partitioned"
    assert decision["partition_columns"] == []  # no column to filter on


def test_a_hive_table_with_no_partition_keys_stays_known_unpartitioned(monkeypatch):
    _live_discovery_through(monkeypatch, _FakeAthena({"plain_b": _hive_meta("plain_b")}))
    _sql, decision = sa._scope_athena_sql("SELECT count(*) FROM plain_b;", _CFG)
    assert decision["partition_metadata"] == "known_unpartitioned"
    _sql, decision = sa._scope_athena_sql("SELECT count(*) FROM plain_b", _CFG)
    assert decision["reason"] == "unpartitioned"
    assert "partition_metadata" not in decision  # byte-identical decision


def test_the_hidden_partitioning_block_never_claims_the_table_is_unpartitioned():
    error = sa._athena_scope_required_error(
        {"applied": False, "reason": "scope_required", "table": "events_f",
         "partition_columns": [], "partition_metadata": "hidden_partitioned",
         "partition_fields": ["region_key", "event_time_day"]},
        "system-a")
    text = " ".join([error["error"], *error["guidance"]])
    assert error["error_type"] == "scope_required"
    assert "unpartitioned" not in text.lower()
    assert "hidden partitioning" in text.lower()
    assert "region_key, event_time_day" in text
    # Retrying identical SQL cannot change a format fact.
    assert "retryable" not in error
    # Plain prompt text: no em or en dashes.
    assert "\u2014" not in text and "\u2013" not in text


def test_the_tool_returns_the_hidden_block_without_running_data_io(monkeypatch):
    _live_discovery_through(monkeypatch, _FakeAthena({"events_g": _iceberg_meta("events_g")}))
    ran: list[str] = []
    monkeypatch.setattr(at, "run_athena_query",
                        lambda sql, *_a, **_k: ran.append(sql) or {"rows": []})
    # A second statement keeps the query unreadable to the guard (a lone
    # trailing semicolon is removed before the guard reads it).
    out = sa._scoped_athena_attempt("SELECT count(*) FROM events_g; SELECT 1",
                                    at._resolve_config(_CFG), None, "system-a")
    assert out["error_type"] == "scope_required"
    assert out["scope"]["partition_metadata"] == "hidden_partitioned"
    assert ran == []


# --------------------------------------------------------------------------- #
# 3. Bake, parse and prompt
# --------------------------------------------------------------------------- #
def test_the_renderer_marks_hidden_partitioning_with_its_fields():
    cols = {"events_h": [("id", "varchar"), ("event_time", "timestamp")]}
    out = schema_format.format_tables_desc(
        cols, unpartitioned_tables={"events_h"},
        hidden_partitioning={"events_h": ["region_key", "event_time_day"]})
    assert out == ("events_h(id: varchar, event_time: timestamp) "
                   "(hidden partitioning by region_key, event_time_day)")
    assert _NO_PART not in out


def test_the_renderer_marks_hidden_partitioning_without_fields():
    out = schema_format.format_tables_desc(
        {"events_i": [("id", "varchar")]}, hidden_partitioning={"events_i": []})
    assert out == "events_i(id: varchar) (hidden partitioning)"


def test_the_samples_renderer_marks_hidden_partitioning():
    out = schema_format.format_schema_with_samples(
        {"events_j": [("id", "varchar"), ("region_key", "varchar")]},
        {"events_j": {"region_key": ["r1", "r2"]}},
        hidden_partitioning={"events_j": ["region_key"]})
    assert out == ("events_j(id: varchar; region_key: varchar e.g. r1, r2) "
                   "(hidden partitioning by region_key)")


def test_the_renderer_is_byte_identical_without_hidden_partitioning():
    cols = {"plain_c": [("a", "int")]}
    assert schema_format.format_tables_desc(cols, unpartitioned_tables={"plain_c"}) == (
        schema_format.format_tables_desc(cols, unpartitioned_tables={"plain_c"},
                                         hidden_partitioning={}))


def test_the_hidden_marker_round_trips_through_the_agent_side_parser():
    rendered = schema_format.format_tables_desc(
        {"events_k": [("id", "varchar")], "plain_d": [("a", "int")],
         "parts_a": [("dt", "date (partition key)")]},
        unpartitioned_tables={"plain_d", "events_k"},
        hidden_partitioning={"events_k": ["region_key", "event_time_day"]})
    baked = sa._athena_partition_columns_from_desc(rendered)
    assert set(baked) == {"events_k", "plain_d", "parts_a"}
    assert isinstance(baked["events_k"], schema_format.HiddenPartitioning)
    assert baked["events_k"].fields == ("region_key", "event_time_day")
    assert len(baked["events_k"]) == 0  # no partition COLUMN to filter on
    assert baked["plain_d"] == [] and baked["parts_a"] == ["dt"]


@pytest.mark.parametrize("desc", [
    "events_l(id: varchar) (hidden partitioning by a, b)EVIL",
    "events_l(id: varchar) (hidden partitioning by a b)",
    "events_l(id: varchar) (hidden partitioning by a, b",
])
def test_a_damaged_hidden_marker_is_never_read_as_proof(desc):
    baked = sa._athena_partition_columns_from_desc(desc)
    assert "events_l" not in baked or not isinstance(
        baked["events_l"], schema_format.HiddenPartitioning)
    assert baked.get("events_l") != []


def test_the_baked_hidden_marker_needs_no_live_lookup(monkeypatch):
    def unexpected(*_a, **_k):
        raise AssertionError("a baked hidden marker must skip live partition discovery")

    monkeypatch.setattr(at, "list_partition_columns", unexpected)
    baked = sa._athena_partition_columns_from_desc(
        "events_m(id: varchar, event_time: timestamp) "
        "(hidden partitioning by region_key, event_time_day)")
    _sql, decision = sa._scope_athena_sql(
        "SELECT count(*) FROM events_m;", _CFG, baked_partition_columns=baked)
    assert decision["partition_metadata"] == "hidden_partitioned"
    assert decision["partition_fields"] == ["region_key", "event_time_day"]


def test_a_stale_no_partition_keys_bake_is_corrected_by_the_catalog_read(monkeypatch):
    """An existing deployment baked ``(no partition keys)`` for an Iceberg table:
    once this process has read the table's catalog entry, the guard says hidden."""
    def unexpected(*_a, **_k):
        raise AssertionError("baked [] must skip live partition discovery")

    monkeypatch.setattr(at, "list_partition_columns", unexpected)
    at._table_columns_via_metadata_api(
        _FakeAthena({"events_n": _iceberg_meta("events_n")}),
        at._resolve_config(_CFG), "events_n")
    _sql, decision = sa._scope_athena_sql(
        "SELECT count(*) FROM events_n;", _CFG,
        baked_partition_columns={"events_n": []})
    assert decision["partition_metadata"] == "hidden_partitioned"


def test_the_athena_prompt_names_hidden_partitioning_and_its_fields():
    desc = ("events_o(id: varchar, event_time: timestamp) "
            "(hidden partitioning by region_key, event_time_day)")
    prompt = sa._compose_system_prompt(
        system_id="system-a", system_name="System A", schema="db_a",
        system_prompt="You are the Athena agent.", tables_desc=desc,
        connection_type="Athena")
    assert "(hidden partitioning by region_key, event_time_day)" in prompt
    assert _NO_PART not in prompt
    efficiency = sa._athena_query_efficiency_prompt(desc)
    assert "Hidden partitioning" in efficiency
    assert "events_o: region_key, event_time_day" in efficiency
    new_text = efficiency[efficiency.index("Hidden partitioning"):]
    new_text = new_text[:new_text.index("Bound the time column the question asks about.")]
    assert "\u2014" not in new_text and "\u2013" not in new_text


# --------------------------------------------------------------------------- #
# 4. Onboarding reads the fields at the point the schema is described
# --------------------------------------------------------------------------- #
def test_partition_fields_are_read_from_the_partition_row_value():
    value = "{region_key=r1, event_time_day=2026-01-05, event_time_hour=null}"
    assert at._partition_fields_from_value(value) == [
        "region_key", "event_time_day", "event_time_hour"]
    assert at._partition_fields_from_value("not a row value") == []
    assert at._partition_fields_from_value(None) == []


def test_hidden_partition_fields_probe_only_iceberg_tables():
    fake = _FakeAthena({"events_p": _iceberg_meta("events_p"), "plain_e": _hive_meta("plain_e")},
                       partition_value="{region_key=r1, event_time_day=2026-01-05}")
    cfg = at._resolve_config(_CFG)
    at._table_columns_via_metadata_api(fake, cfg, "events_p")
    at._table_columns_via_metadata_api(fake, cfg, "plain_e")
    assert at.hidden_partition_fields("plain_e", _CFG, client=fake) is None
    assert fake.queries == []
    assert at.hidden_partition_fields("events_p", _CFG, client=fake) == [
        "region_key", "event_time_day"]
    assert len(fake.queries) == 1 and '"events_p$partitions"' in fake.queries[0]


def test_a_failed_probe_still_reports_hidden_partitioning_without_fields():
    fake = _FakeAthena({"events_q": _iceberg_meta("events_q")}, probe_fails=True)
    at._table_columns_via_metadata_api(fake, at._resolve_config(_CFG), "events_q")
    assert at.hidden_partition_fields("events_q", _CFG, client=fake) == []


def test_onboarding_bakes_hidden_partitioning_not_no_partition_keys(monkeypatch):
    fake = _FakeAthena({"events_r": _iceberg_meta("events_r")},
                       partition_value="{region_key=r1, event_time_day=2026-01-05}")
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: fake)
    monkeypatch.setattr(onboard, "_collect_provision_samples", lambda **_k: {})
    body = ProvisionRequest(
        system_id="system-a", system_name="System A", schema="db_a",
        connection_type=ATHENA, connection_config=dict(_CFG),
        mappings=[{"table": "events_r", "field": "events_r.id", "concept": "record",
                   "mappings": []}],
        rules=[])
    ctx = types.SimpleNamespace(body=body, system_id="system-a", schema="db_a")
    desc = onboard._dp_athena_tables_desc(ctx, dict(_CFG))
    assert desc.endswith("(hidden partitioning by region_key, event_time_day)")
    assert _NO_PART not in desc
    baked = sa._athena_partition_columns_from_desc(desc)
    assert baked["events_r"].fields == ("region_key", "event_time_day")


def test_onboarding_still_bakes_no_partition_keys_for_a_hive_table(monkeypatch):
    fake = _FakeAthena({"plain_f": _hive_meta("plain_f")})
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: fake)
    monkeypatch.setattr(onboard, "_collect_provision_samples", lambda **_k: {})
    body = ProvisionRequest(
        system_id="system-a", system_name="System A", schema="db_a",
        connection_type=ATHENA, connection_config=dict(_CFG),
        mappings=[{"table": "plain_f", "field": "plain_f.id", "concept": "record",
                   "mappings": []}],
        rules=[])
    ctx = types.SimpleNamespace(body=body, system_id="system-a", schema="db_a")
    desc = onboard._dp_athena_tables_desc(ctx, dict(_CFG))
    assert desc == f"plain_f(id: string) {_NO_PART}"
    assert fake.queries == []


# --------------------------------------------------------------------------- #
# 5. Split-window recovery never asks the model to add figures itself
# --------------------------------------------------------------------------- #
# The SQL totals rule says a total comes from SQL, never by adding rows by
# hand. The hidden-partitioning recovery (split the window when the scan limit
# refuses) must say the same: report each range, give a combined total only
# when a tool computed it, else say the total was not computed. A query that
# only adds copied literals is not a tool total, so the scan guard keeps
# refusing a relation-free SELECT as before.
_HIDDEN_DESC = ("events_s(id: varchar, event_time: timestamp) "
                "(hidden partitioning by region_key, event_time_day)")


def _hidden_error_text() -> str:
    error = sa._athena_scope_required_error(
        {"applied": False, "reason": "scope_required", "table": "events_s",
         "partition_columns": [], "partition_metadata": "hidden_partitioned"},
        "system-a")
    return " ".join(" ".join([error["error"], *error["guidance"]]).split())


def _hidden_hint_text() -> str:
    efficiency = sa._athena_query_efficiency_prompt(_HIDDEN_DESC)
    hint = efficiency[efficiency.index("Hidden partitioning"):]
    return " ".join(hint[:hint.index("Bound the time column the question asks about.")].split())


@pytest.mark.parametrize("text_of", [_hidden_error_text, _hidden_hint_text],
                         ids=["scope-error", "query-efficiency"])
def test_split_window_recovery_never_hands_the_total_to_the_model(text_of):
    text = text_of()
    low = text.lower()
    assert "add the results" not in low
    # A refused COUNT/SUM/MIN/MAX over one table is the tool's job now: it is
    # computed exactly from partition pieces and comes back as one result.
    assert "computed exactly by the tool from partition pieces" in low
    assert "use it as one result" in low
    assert "if a refusal says the exact total could not be computed" in low
    # Other shapes may still be split, each range reported, never added up.
    assert "smaller ranges" in low and "report each range's result" in low
    assert "with no combined total" in low
    assert "never add numbers yourself" in low
    assert "only adds numbers you copied" in low
    # No facility that exists only in enforce mode is offered.
    assert "typed-output contract" not in low and "server recomputes" not in low
    assert "select 1204" not in low and "arithmetic query" not in low
    assert "\u2014" not in text and "\u2013" not in text


def test_the_composed_prompt_never_tells_the_agent_to_add_range_results():
    prompt = sa._compose_system_prompt(
        system_id="system-a", system_name="System A", schema="db_a",
        system_prompt="You are the Athena agent.", tables_desc=_HIDDEN_DESC,
        connection_type="Athena")
    assert "never by adding rows yourself" in prompt  # the totals rule
    assert "add the results" not in prompt.lower()


@pytest.mark.parametrize("sql", ["SELECT 1204 + 980 + 1311",
                                 "SELECT 3495 AS exact_customer_total",
                                 "SELECT random()"])
def test_a_relation_free_select_is_not_a_grounded_result(monkeypatch, sql):
    """A SELECT that reads no table proves nothing about the data, so the guard
    keeps refusing it (honest unknown) rather than running it as a result."""
    monkeypatch.setattr(at, "list_partition_columns",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no relation => no metadata lookup")))
    _out, decision = sa._scope_athena_sql(sql, _CFG)
    assert decision["reason"] == "scope_required"
    assert decision["partition_metadata"] == "unknown"
