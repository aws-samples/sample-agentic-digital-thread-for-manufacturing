# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ROOT FIX: onboarding must persist the REAL physical Athena columns.

Diagnosed defect: ``onboard_discover`` captures the real physical columns from
``information_schema``, but ``_do_provision`` built ``SchemaEntry``/``Field`` nodes
SOLELY from the LLM map step's column→concept mappings. When the map step emitted
flat business-ish names (or a subset), ``get_system_schema`` advertised columns that
DO NOT EXIST in the live table (COLUMN_NOT_FOUND), and nested-struct columns / partition
keys were never surfaced.

These tests cover, GENERICALLY (no dataset literals):
  1. the pure ``flatten_athena_columns`` helper — nested ``row(...)`` structs flatten to
     dotted leaf paths, partition keys flagged from ``extra_info``;
  2. ``describe_physical_columns`` over a mocked Athena client;
  3. the REAL ``_do_provision`` schema-write — physical columns (incl. partition flags)
     ARE persisted, and a phantom (non-physical) mapped column is DROPPED;
  4. the AFFIRMATIVE introspection-completeness signal
     (``describe_physical_columns_bounded`` / the ``_discover_athena_physical_columns``
     sink), which is what gates the baked "no partition keys" claim;
  5. the renderer emits ``(no partition keys)`` ONLY for a table that was
     completely introspected AND has zero partition keys, and that rendering
     round-trips through the agent-side parser.
"""
from __future__ import annotations

import json

import agents.athena_tools as onboard_athena_tools
import agents.onboard as onboard
import agents.schema_format as schema_format
from agents.athena_tools import (
    describe_physical_columns,
    describe_physical_columns_bounded,
    flatten_athena_columns,
)
from agents.connection_types import ATHENA
from agents.models import ProvisionRequest
from agents.sql_safety import MAX_ROWS


# --------------------------------------------------------------------------- #
# 1. Pure helper: flatten_athena_columns
# --------------------------------------------------------------------------- #
def test_flatten_scalar_columns_passthrough():
    rows = [["id", "varchar", ""], ["revision", "integer", ""]]
    out = flatten_athena_columns(rows)
    assert out == [
        {"name": "id", "data_type": "varchar", "is_partition": False},
        {"name": "revision", "data_type": "integer", "is_partition": False},
    ]


def test_flatten_marks_partition_keys_from_extra_info():
    rows = [
        ["id", "varchar", ""],
        ["ingest_date", "varchar", "partition key"],
    ]
    out = flatten_athena_columns(rows)
    names = {c["name"]: c["is_partition"] for c in out}
    assert names == {"id": False, "ingest_date": True}


def test_flatten_nested_struct_to_dotted_paths():
    """A nested ``row(...)`` column flattens into the dotted leaf paths a query must
    actually reference — the exact form the agent needs (e.g. col.sub.leaf)."""
    rows = [
        ["id", "varchar", ""],
        [
            "state_context_map",
            "row(started row(code integer, at varchar), completed row(code integer))",
            "",
        ],
    ]
    out = flatten_athena_columns(rows)
    names = [c["name"] for c in out]
    assert "id" in names
    assert "state_context_map.started.code" in names
    assert "state_context_map.started.at" in names
    assert "state_context_map.completed.code" in names
    # The bare struct parent is NOT surfaced as a queryable leaf.
    assert "state_context_map" not in names


def test_flatten_array_type_left_as_leaf():
    """A non-``row`` complex type (array/map) cannot be dotted into without UNNEST,
    so it is surfaced as-is (a single leaf) — never dropped."""
    rows = [["tags", "array(varchar)", ""]]
    out = flatten_athena_columns(rows)
    assert out == [{"name": "tags", "data_type": "array(varchar)", "is_partition": False}]


def test_flatten_dedupes_and_skips_blank_names():
    rows = [["id", "varchar", ""], ["id", "varchar", ""], ["", "varchar", ""]]
    out = flatten_athena_columns(rows)
    assert [c["name"] for c in out] == ["id"]


# --------------------------------------------------------------------------- #
# 2. describe_physical_columns over a mocked Athena client
# --------------------------------------------------------------------------- #
class _FakeAthena:
    """Minimal Athena double: a single SUCCEEDED information_schema.columns query."""

    def __init__(self, rows):
        self._rows = rows

    def start_query_execution(self, **_kw):
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def stop_query_execution(self, QueryExecutionId):
        pass

    def get_query_results(self, **_kw):
        cols = ["column_name", "data_type", "extra_info"]
        rows = [{"Data": [{"VarCharValue": c} for c in cols]}]
        for r in self._rows:
            rows.append({"Data": [{"VarCharValue": v} for v in r]})
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": c} for c in cols]},
                "Rows": rows,
            }
        }


def test_describe_physical_columns_flattens_and_flags(monkeypatch):
    fake = _FakeAthena(
        [
            ["id", "varchar", ""],
            ["ingest_date", "varchar", "partition key"],
            ["state_context_map", "row(started row(code integer))", ""],
        ]
    )
    cfg = {"workgroup": "wg", "database": "db", "region": "us-east-1"}
    out = describe_physical_columns("segments", cfg, client=fake)
    by_name = {c["name"]: c for c in out}
    assert set(by_name) == {"id", "ingest_date", "state_context_map.started.code"}
    assert by_name["ingest_date"]["is_partition"] is True
    assert by_name["id"]["is_partition"] is False


# --------------------------------------------------------------------------- #
# 3. REAL _do_provision schema-write for Athena (physical columns are the truth)
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    return writes


def _field_ids(writes):
    return [p["fid"] for c, p in writes if "MERGE (f:Field {id: $fid})" in c and "fid" in p]


def _partition_field_ids(writes):
    return [
        p["fid"]
        for c, p in writes
        if "MERGE (f:Field {id: $fid})" in c and p.get("isp") is True
    ]


def _maps_to_fids(writes):
    return [p.get("fid") for c, p in writes if "MERGE (f)-[r:MAPS_TO]->(c)" in c]


def _provision_athena(monkeypatch, mappings, physical):
    writes = _install_seams(monkeypatch)
    monkeypatch.setattr(
        onboard, "_discover_athena_physical_columns", lambda *_a, **_k: physical
    )
    body = ProvisionRequest(
        system_id="example-athena-x",
        system_name="Example Athena",
        schema="example_athena_db",
        connection_type=ATHENA,
        connection_config={"workgroup": "wg", "database": "example_athena_db", "region": "us-east-1"},
        mappings=mappings,
        rules=[],
    )
    onboard._do_provision(body)
    return writes


def test_provision_persists_physical_columns_and_flags_partitions(monkeypatch):
    """Physical columns (incl. a flattened struct dotted path) are persisted as Field
    nodes, and a partition column is flagged (is_partition=True)."""
    physical = {
        "example_segments": [
            {"name": "id", "data_type": "varchar", "is_partition": False},
            {"name": "ingest_date", "data_type": "varchar", "is_partition": True},
            {"name": "state_context_map.started.code", "data_type": "integer", "is_partition": False},
        ]
    }
    writes = _provision_athena(
        monkeypatch,
        [
            {"table": "example_segments", "field": "example_segments.id", "concept": "batch",
             "mappings": [{"concept": "batch", "role": "identifier", "confidence": 0.9}]},
        ],
        physical,
    )
    fids = set(_field_ids(writes))
    # All REAL physical columns are persisted (incl. the dotted struct leaf).
    assert "example-athena-x.example_segments.id" in fids
    assert "example-athena-x.example_segments.ingest_date" in fids
    assert "example-athena-x.example_segments.state_context_map.started.code" in fids
    # The partition column is flagged.
    assert "example-athena-x.example_segments.ingest_date" in set(_partition_field_ids(writes))


def test_provision_drops_phantom_non_physical_mapped_column(monkeypatch):
    """A mapped column that is NOT a real physical column (a business-ish name the map
    step invented) is DROPPED — get_system_schema will never advertise it. A mapped
    column that IS physical keeps its MAPS_TO concept edge."""
    physical = {
        "example_segments": [
            {"name": "id", "data_type": "varchar", "is_partition": False},
            {"name": "entity_type", "data_type": "varchar", "is_partition": False},
        ]
    }
    writes = _provision_athena(
        monkeypatch,
        [
            # Physical column — kept, with its concept edge.
            {"table": "example_segments", "field": "example_segments.id", "concept": "batch",
             "mappings": [{"concept": "batch", "role": "identifier", "confidence": 0.9}]},
            # Phantom business name — does NOT exist physically → dropped.
            {"table": "example_segments", "field": "example_segments.defect_type", "concept": "defect",
             "mappings": [{"concept": "defect", "role": "attribute", "confidence": 0.8}]},
        ],
        physical,
    )
    fids = set(_field_ids(writes))
    assert "example-athena-x.example_segments.id" in fids
    # Phantom column is never persisted as a Field.
    assert "example-athena-x.example_segments.defect_type" not in fids
    # The physical mapped column retains its concept edge.
    assert "example-athena-x.example_segments.id" in _maps_to_fids(writes)


def test_provision_falls_back_when_physical_discovery_empty(monkeypatch):
    """If physical discovery fails/returns empty (e.g. no live creds), provision falls
    back to the map-derived Field write — NO regression, no lost onboarding."""
    writes = _provision_athena(
        monkeypatch,
        [
            {"table": "example_segments", "field": "example_segments.id", "concept": "batch",
             "mappings": [{"concept": "batch", "role": "identifier", "confidence": 0.9}]},
        ],
        {},  # empty physical schema → fallback
    )
    assert "example-athena-x.example_segments.id" in set(_field_ids(writes))


def test_provision_multi_dot_mapped_column_survives_physical_gate(monkeypatch):
    """M5: a mapped multi-dot flattened column (explicit table + table-prefixed
    field) SURVIVES the Athena physical gate — Field node + MAPS_TO edge. The old
    last-segment split reduced ``group_a.sub.code`` to the non-physical ``code``,
    so the gate dropped every mapped multi-dot column."""
    physical = {
        "wide_table": [
            {"name": "id", "data_type": "varchar", "is_partition": False},
            {"name": "group_a.sub.code", "data_type": "integer", "is_partition": False},
        ]
    }
    writes = _provision_athena(
        monkeypatch,
        [
            {"table": "wide_table", "field": "wide_table.group_a.sub.code",
             "concept": "batch",
             "mappings": [{"concept": "batch", "role": "attribute", "confidence": 0.85}]},
        ],
        physical,
    )
    fid = "example-athena-x.wide_table.group_a.sub.code"
    assert fid in set(_field_ids(writes))
    # The mapped multi-dot column keeps its concept edge end-to-end.
    assert fid in _maps_to_fids(writes)


def test_provision_table_less_multi_dot_resolves_by_physical_suffix(monkeypatch):
    """M5 target: a TABLE-LESS multi-dot locator (degraded connector shape)
    resolves by longest-suffix match against the physical columns — Field lands
    under the REAL owning table with the full flattened path and keeps its
    MAPS_TO edge."""
    physical = {
        "wide_table": [
            {"name": "group_a.sub.code", "data_type": "integer", "is_partition": False},
        ]
    }
    writes = _provision_athena(
        monkeypatch,
        [
            {"field": "group_a.sub.code", "concept": "batch",
             "mappings": [{"concept": "batch", "role": "attribute", "confidence": 0.8}]},
        ],
        physical,
    )
    fid = "example-athena-x.wide_table.group_a.sub.code"
    assert fid in set(_field_ids(writes))
    assert fid in _maps_to_fids(writes)


# --------------------------------------------------------------------------- #
# 4. AFFIRMATIVE introspection-completeness signal
# --------------------------------------------------------------------------- #
# "This table has no partition keys" may only be baked from a read that provably
# saw EVERY column. The information_schema.columns query runs under the shared
# MAX_ROWS row cap, so a result AT the cap may have been truncated — and the
# truncated tail is exactly where an unseen partition key would hide.
_BOUNDED_CFG = {"workgroup": "wg", "database": "db", "region": "us-east-1"}


def test_describe_physical_columns_bounded_reports_complete_under_the_cap():
    fake = _FakeAthena([["id", "varchar", ""], ["qty", "integer", ""]])
    cols, complete = describe_physical_columns_bounded(
        "segments", _BOUNDED_CFG, client=fake)
    assert [c["name"] for c in cols] == ["id", "qty"]
    assert complete is True


def test_describe_physical_columns_bounded_reports_truncated_at_the_cap():
    """A read that comes back AT the row cap is POSSIBLY TRUNCATED => not complete.
    Note every row here is non-partition, so the naive reading would be the unsafe
    'no partition keys'."""
    rows = [[f"c{i}", "varchar", ""] for i in range(MAX_ROWS + 25)]
    fake = _FakeAthena(rows)
    cols, complete = describe_physical_columns_bounded(
        "wide", _BOUNDED_CFG, client=fake)
    assert len(cols) == MAX_ROWS
    assert complete is False


def test_describe_physical_columns_keeps_its_legacy_single_value_shape():
    """The pre-existing entry point is unchanged — it delegates and drops the flag."""
    fake = _FakeAthena([["id", "varchar", ""]])
    assert describe_physical_columns("segments", _BOUNDED_CFG, client=fake) == [
        {"name": "id", "data_type": "varchar", "is_partition": False},
    ]


def test_discover_athena_physical_columns_fills_the_completeness_sink(monkeypatch):
    """The onboarding discovery helper records ONLY the provably-whole tables in
    the sink; the sink stays optional (callers that pass nothing are unaffected)."""
    calls = {"complete": ("whole",)}

    def fake_bounded(table, cfg, **_kw):
        cols = [{"name": "id", "data_type": "varchar", "is_partition": False}]
        return cols, table in calls["complete"]

    monkeypatch.setattr(
        onboard_athena_tools, "describe_physical_columns_bounded", fake_bounded)
    monkeypatch.setattr(onboard_athena_tools, "_resolve_config", lambda cfg: dict(cfg))

    sink: set = set()
    out = onboard._discover_athena_physical_columns(
        _BOUNDED_CFG, ["whole", "capped"], complete_tables=sink)
    assert set(out) == {"whole", "capped"}
    assert sink == {"whole"}
    # No sink => same physical result, no crash.
    assert set(onboard._discover_athena_physical_columns(
        _BOUNDED_CFG, ["whole", "capped"])) == {"whole", "capped"}


# --------------------------------------------------------------------------- #
# 5. the renderer emits the marker ONLY under the completeness condition
# --------------------------------------------------------------------------- #
_NO_PART = schema_format.PARTITION_KEYS_ABSENT_MARKER


def test_renderer_omits_marker_by_default():
    """Byte-identical to the pre-marker rendering when no completeness set is
    passed — every existing caller is unaffected."""
    assert schema_format.format_tables_desc({"t": [("a", "int")]}) == "t(a: int)"


def test_renderer_emits_marker_for_complete_unpartitioned_table():
    out = schema_format.format_tables_desc(
        {"t": [("a", "int"), ("b", "varchar")]}, unpartitioned_tables={"t"})
    assert out == f"t(a: int, b: varchar) {_NO_PART}"


def test_renderer_never_marks_a_partitioned_table():
    """Self-consistency guard: even if a caller wrongly lists a table whose
    rendered columns carry a "(partition key)" suffix, the marker is suppressed —
    the two markers can never co-occur."""
    out = schema_format.format_tables_desc(
        {"t": [("a", "int"), ("dt", "date (partition key)")]},
        unpartitioned_tables={"t"})
    assert out == "t(a: int, dt: date (partition key))"
    assert _NO_PART not in out


def test_renderer_marks_only_the_listed_tables():
    out = schema_format.format_tables_desc(
        {"plain": [("a", "int")],
         "events": [("dt", "date (partition key)")],
         "unknown": [("b", "int")]},
        unpartitioned_tables={"plain"})
    assert out == (
        f"plain(a: int) {_NO_PART}. events(dt: date (partition key)). unknown(b: int)"
    )


def test_partitioned_rendering_is_unchanged_with_and_without_the_flag():
    table_columns = {"events": [("dt", "date (partition key)"), ("qty", "int")]}
    baseline = schema_format.format_tables_desc(table_columns)
    assert baseline == "events(dt: date (partition key), qty: int)"
    assert schema_format.format_tables_desc(
        table_columns, unpartitioned_tables=set()) == baseline
    assert schema_format.format_tables_desc(
        table_columns, unpartitioned_tables={"events"}) == baseline


def test_samples_renderer_emits_marker_after_the_semicolon_body():
    out = schema_format.format_schema_with_samples(
        {"t": [("a", "int"), ("b", "varchar")]},
        {"t": {"a": [1, 2]}},
        unpartitioned_tables={"t"},
    )
    assert out == f"t(a: int e.g. 1, 2; b: varchar) {_NO_PART}"


def test_samples_renderer_suppresses_marker_for_a_partitioned_table():
    out = schema_format.format_schema_with_samples(
        {"t": [("dt", "date (partition key)")]}, {}, unpartitioned_tables={"t"})
    assert _NO_PART not in out


def test_renderer_marker_round_trips_through_the_agent_side_parser():
    """End-to-end contract: what onboarding RENDERS is what the guardrail PARSES —
    marked table => baked [] (discovery skipped); unmarked => absent (live
    discovery); partitioned => named columns."""
    import agents.system_agent as sa

    rendered = schema_format.format_tables_desc(
        {"plain": [("a", "int")],
         "events": [("dt", "date (partition key)"), ("qty", "int")],
         "capped": [("b", "int")]},
        unpartitioned_tables={"plain"},
    )
    assert sa._athena_partition_columns_from_desc(rendered) == {
        "plain": [], "events": ["dt"],
    }

    with_samples = schema_format.format_schema_with_samples(
        {"plain": [("a", "int")], "capped": [("b", "int")]},
        {"plain": {"a": ["x"]}},
        unpartitioned_tables={"plain"},
    )
    assert sa._athena_partition_columns_from_desc(with_samples) == {"plain": []}


# --------------------------------------------------------------------------- #
# ADR-0077 D1/D3: the ONE writer + per-table discovery completeness
# --------------------------------------------------------------------------- #
import logging  # noqa: E402
import types  # noqa: E402

import agents.schema_fields as schema_fields  # noqa: E402

_ONE_MAPPED = [{"table": "example_segments", "field": "example_segments.id",
                "concept": "batch",
                "mappings": [{"concept": "batch", "role": "identifier", "confidence": 0.9}]}]
_THREE_PHYSICAL = {"example_segments": [
    {"name": "id", "data_type": "varchar", "is_partition": False},
    {"name": "ingest_date", "data_type": "varchar", "is_partition": True},
    {"name": "oee.availability", "data_type": "double", "is_partition": False}]}


def _completeness_writes(writes):
    return [p for c, p in writes if c == schema_fields.DISCOVERY_COMPLETENESS_SET]


def _provision_with_sink(monkeypatch, mappings, physical, complete):
    writes = _install_seams(monkeypatch)

    def fake_discover(_cfg, _tables, complete_tables=None):
        if complete_tables is not None:
            complete_tables.update(complete)
        return physical
    monkeypatch.setattr(onboard, "_discover_athena_physical_columns", fake_discover)
    body = ProvisionRequest(
        system_id="example-athena-x", system_name="Example Athena",
        schema="example_athena_db", connection_type=ATHENA,
        connection_config={"workgroup": "wg", "database": "example_athena_db",
                           "region": "us-east-1"},
        mappings=mappings, rules=[])
    onboard._do_provision(body)
    return writes


def test_complete_discovery_is_recorded(monkeypatch):
    writes = _provision_with_sink(monkeypatch, _ONE_MAPPED, _THREE_PHYSICAL,
                                  {"example_segments"})
    assert _completeness_writes(writes) == [
        {"seid": "example-athena-x.example_segments", "complete": True, "count": 3}]


def test_fallback_table_is_recorded_partial_and_warned(monkeypatch, caplog):
    mapped = _ONE_MAPPED + [{"table": "work_orders", "field": "work_orders.priority",
                             "concept": "batch", "mappings": []}]
    with caplog.at_level(logging.WARNING, logger="agents.onboard"):
        writes = _provision_with_sink(monkeypatch, mapped, _THREE_PHYSICAL,
                                      {"example_segments"})
    by_seid = {p["seid"]: p for p in _completeness_writes(writes)}
    assert by_seid["example-athena-x.work_orders"] == {
        "seid": "example-athena-x.work_orders", "complete": False, "count": 1}
    assert by_seid["example-athena-x.example_segments"]["complete"] is True
    warned = [r.getMessage() for r in caplog.records
              if "discovery incomplete" in r.getMessage()]
    assert len(warned) == 1 and "work_orders" in warned[0]
    assert "priority" not in warned[0]  # counts only — never column names


def test_capped_discovery_is_recorded_partial(monkeypatch):
    writes = _provision_with_sink(monkeypatch, _ONE_MAPPED, _THREE_PHYSICAL, set())
    assert _completeness_writes(writes) == [
        {"seid": "example-athena-x.example_segments", "complete": False, "count": 3}]


def test_non_athena_records_nothing():
    writes: list = []
    ctx = types.SimpleNamespace(connection_type="PostgreSQL", tables_seen={"t"})
    original = onboard.neptune_write
    try:
        onboard.neptune_write = lambda c, p=None: writes.append(c)
        onboard._dp_discovery_completeness(ctx)
    finally:
        onboard.neptune_write = original
    assert writes == []


def test_b2a_uses_the_one_writer_cypher(monkeypatch):
    writes = _provision_with_sink(monkeypatch, _ONE_MAPPED, _THREE_PHYSICAL,
                                  {"example_segments"})
    field_writes = [(c, p) for c, p in writes
                    if p.get("fid") == "example-athena-x.example_segments.oee.availability"]
    assert (schema_fields.FIELD_MERGE,
            {"fid": "example-athena-x.example_segments.oee.availability",
             "name": "oee.availability", "schema_id": "example-athena-x.example_segments",
             "sid": "example-athena-x", "isp": False, "dtype": "double"}) in field_writes
    assert any(c == schema_fields.HAS_FIELD_MERGE for c, _p in field_writes)
    with open(onboard.__file__, encoding="utf-8") as handle:
        assert "f.is_partition = $isp" not in handle.read()
