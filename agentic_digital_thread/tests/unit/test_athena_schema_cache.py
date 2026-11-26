# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Per-process TTL cache of Athena ``information_schema.columns`` reads.

Measured 2026-09-24 on a live golden campaign: 84 of 300 Athena executions were
introspection, and ONE describe statement ran 69 times — each exposed to the
on-demand queue exactly like a data query. The cache is keyed by physical-table
identity (same key shape as the partition-column cache), TTL-bounded, shared by
the agent-facing ``athena_describe_table`` tool and the internal
``_introspect_columns_bounded`` path, never caches failures, and is disabled
by ``ADT_ATHENA_SCHEMA_CACHE_TTL_SECONDS=0``.
"""
from __future__ import annotations

import json

import pytest

import agents.athena_tools as at

_CFG = {"region": "us-east-1", "catalog": "AwsDataCatalog", "database": "db",
        "role_arn": "arn:aws:iam::111122223333:role/r", "workgroup": "wg"}


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    at.clear_schema_cache()
    monkeypatch.delenv(at._ENV_SCHEMA_CACHE_TTL_SECONDS, raising=False)
    yield
    at.clear_schema_cache()


def _stub_queries(monkeypatch, rows):
    calls = []

    def fake_run(sql, cfg, **kwargs):
        calls.append(sql)
        return {"rows": rows, "columns": ["column_name", "data_type", "extra_info"]}

    monkeypatch.setattr(at, "run_athena_query", fake_run)
    return calls


def test_introspection_is_served_from_cache_within_ttl(monkeypatch):
    calls = _stub_queries(monkeypatch, [["id", "varchar", ""], ["dt", "date", "partition key"]])
    first = at._introspect_columns_bounded(object(), _CFG, "t1")
    second = at._introspect_columns_bounded(object(), _CFG, "t1")
    assert first == second
    assert first[1] is True  # complete: under the row cap
    assert len(calls) == 1  # ONE information_schema read for two calls


def test_cache_key_is_the_physical_table_identity(monkeypatch):
    calls = _stub_queries(monkeypatch, [["id", "varchar", ""]])
    at._introspect_columns_bounded(object(), _CFG, "t1")
    at._introspect_columns_bounded(object(), _CFG, "t2")               # other table
    at._introspect_columns_bounded(object(), dict(_CFG, database="other"), "t1")  # other db
    assert len(calls) == 3


def test_expired_entries_are_re_read(monkeypatch):
    calls = _stub_queries(monkeypatch, [["id", "varchar", ""]])
    now = [1000.0]
    monkeypatch.setattr(at.time, "monotonic", lambda: now[0])
    monkeypatch.setenv(at._ENV_SCHEMA_CACHE_TTL_SECONDS, "30")
    at._introspect_columns_bounded(object(), _CFG, "t1")
    now[0] += 29
    at._introspect_columns_bounded(object(), _CFG, "t1")
    assert len(calls) == 1
    now[0] += 2  # past the TTL
    at._introspect_columns_bounded(object(), _CFG, "t1")
    assert len(calls) == 2


def test_zero_ttl_disables_the_cache(monkeypatch):
    calls = _stub_queries(monkeypatch, [["id", "varchar", ""]])
    monkeypatch.setenv(at._ENV_SCHEMA_CACHE_TTL_SECONDS, "0")
    at._introspect_columns_bounded(object(), _CFG, "t1")
    at._introspect_columns_bounded(object(), _CFG, "t1")
    assert len(calls) == 2
    assert at._SCHEMA_CACHE == {}


def test_failures_are_never_cached(monkeypatch):
    attempts = []

    def flaky(sql, cfg, **kwargs):
        attempts.append(sql)
        if len(attempts) == 1:
            raise RuntimeError("Athena query did not finish within 10s (last state QUEUED)")
        return {"rows": [["id", "varchar", ""]]}

    monkeypatch.setattr(at, "run_athena_query", flaky)
    with pytest.raises(RuntimeError):
        at._introspect_columns_bounded(object(), _CFG, "t1")
    assert at._SCHEMA_CACHE == {}
    at._introspect_columns_bounded(object(), _CFG, "t1")
    assert len(attempts) == 2


@pytest.mark.parametrize("raw", ["abc", "inf", "nan", "-5"])
def test_malformed_ttl_fails_loud(monkeypatch, raw):
    monkeypatch.setenv(at._ENV_SCHEMA_CACHE_TTL_SECONDS, raw)
    with pytest.raises(ValueError):
        at._schema_cache_ttl()


def test_describe_tool_shares_the_cache_and_returns_identical_payload(monkeypatch):
    calls = _stub_queries(monkeypatch, [["id", "varchar"], ["cell", "varchar"]])
    monkeypatch.setattr(at, "_resolve_config", lambda cfg: dict(_CFG))
    monkeypatch.setattr(at, "boto3_client_for", lambda *a, **k: object())
    tools = at.make_athena_tools(dict(_CFG), source_system="sys")
    describe = next(t for t in tools
                    if getattr(t, "tool_name", getattr(t, "__name__", "")).endswith("athena_describe_table"))
    first = describe("t1")
    second = describe("t1")
    assert first == second
    assert json.loads(first)["rows"][0][0] == "id"
    assert len(calls) == 1


# --- Catalog metadata API first, information_schema fallback -----------------

class _MetaAthena:
    """Athena double exposing ONLY the metadata API (no query execution)."""
    def __init__(self):
        self.calls = []

    def get_table_metadata(self, **kw):
        self.calls.append(("get_table_metadata", kw["TableName"]))
        return {"TableMetadata": {
            "Name": kw["TableName"],
            "Columns": [{"Name": "id", "Type": "varchar"}, {"Name": "qty", "Type": "bigint"}],
            "PartitionKeys": [{"Name": "dt", "Type": "date"}],
        }}

    def list_table_metadata(self, **kw):
        self.calls.append(("list_table_metadata", kw.get("NextToken")))
        if kw.get("NextToken") is None:
            return {"TableMetadataList": [{"Name": "t_b"}], "NextToken": "p2"}
        return {"TableMetadataList": [{"Name": "t_a"}]}


def test_introspection_uses_metadata_api_and_never_starts_a_query(monkeypatch):
    def must_not_run(*a, **k):
        raise AssertionError("information_schema query executed despite metadata API")
    monkeypatch.setattr(at, "run_athena_query", must_not_run)
    rows, complete = at._introspect_columns_bounded(_MetaAthena(), _CFG, "t1")
    # SAME shape + marker as the information_schema path: partition keys LAST,
    # extra_info == 'partition key' — so flatten_athena_columns is unchanged.
    assert rows == [("id", "varchar", ""), ("qty", "bigint", ""), ("dt", "date", "partition key")]
    assert complete is True
    flat = at.flatten_athena_columns(rows)
    assert [c["name"] for c in flat if c["is_partition"]] == ["dt"]


def test_metadata_api_failure_falls_back_to_information_schema(monkeypatch):
    class _Denied:
        def get_table_metadata(self, **kw):
            raise RuntimeError("AccessDeniedException: athena:GetTableMetadata")
    calls = _stub_queries(monkeypatch, [["id", "varchar", ""]])
    rows, complete = at._introspect_columns_bounded(_Denied(), _CFG, "t1")
    assert rows == [("id", "varchar", "")] and complete is True
    assert len(calls) == 1 and "information_schema.columns" in calls[0]


def test_double_without_metadata_method_falls_back(monkeypatch):
    calls = _stub_queries(monkeypatch, [["id", "varchar", ""]])
    at._introspect_columns_bounded(object(), _CFG, "t1")  # no get_table_metadata attr
    assert len(calls) == 1


def test_list_tables_tool_uses_metadata_api_paginated_and_caches(monkeypatch):
    meta = _MetaAthena()
    monkeypatch.setattr(at, "_resolve_config", lambda cfg: dict(_CFG))
    monkeypatch.setattr(at, "boto3_client_for", lambda *a, **k: meta)
    monkeypatch.setattr(at, "run_athena_query",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SQL ran")))
    tools = at.make_athena_tools(dict(_CFG), source_system="sys")
    list_tables = next(t for t in tools
                       if getattr(t, "tool_name", getattr(t, "__name__", "")).endswith("athena_list_tables"))
    first = json.loads(list_tables())
    second = json.loads(list_tables())
    assert first["rows"] == [["t_a"], ["t_b"]] and first == second
    assert [c[0] for c in meta.calls].count("list_table_metadata") == 2  # 2 pages, ONE listing


def test_describe_tool_metadata_api_payload_shape(monkeypatch):
    meta = _MetaAthena()
    monkeypatch.setattr(at, "_resolve_config", lambda cfg: dict(_CFG))
    monkeypatch.setattr(at, "boto3_client_for", lambda *a, **k: meta)
    tools = at.make_athena_tools(dict(_CFG), source_system="sys")
    describe = next(t for t in tools
                    if getattr(t, "tool_name", getattr(t, "__name__", "")).endswith("athena_describe_table"))
    payload = json.loads(describe("t1"))
    assert payload["columns"] == ["column_name", "data_type"]
    assert payload["rows"] == [["id", "varchar"], ["qty", "bigint"], ["dt", "date"]]
    assert payload["row_count"] == 3 and payload.get("truncated") in (False, None)


def test_entity_not_found_answers_empty_without_sql_fallback(monkeypatch):
    class _Missing:
        def get_table_metadata(self, **kw):
            raise RuntimeError("MetadataException: Entity Not Found (EntityNotFoundException)")
    monkeypatch.setattr(at, "run_athena_query",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SQL ran for a missing table")))
    rows, complete = at._introspect_columns_bounded(_Missing(), _CFG, "nope")
    assert rows == [] and complete is True


def test_column_not_found_enrichment_uses_shared_introspection(monkeypatch):
    """The enrichment's column list rides the cached metadata path — never its
    own information_schema query (the 2026-09-24 single-column stragglers)."""
    seen = []
    monkeypatch.setattr(at, "_introspect_columns",
                        lambda athena, cfg, table: seen.append(table) or
                        [("order_id", "varchar", ""), ("created_at", "timestamp", "")])
    monkeypatch.setattr(at, "_first_column_values",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("own SQL ran")))
    out = at._enrich_column_not_found(
        object(), dict(_CFG), "SELECT guessed FROM orders", "COLUMN_NOT_FOUND: guessed", "db")
    assert seen == ["orders"] and "order_id" in out
