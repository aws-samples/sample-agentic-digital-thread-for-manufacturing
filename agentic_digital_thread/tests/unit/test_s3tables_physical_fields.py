# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables provision must persist EVERY physical column as a Field, the
way Athena does, so record-key validation and get_system_schema see the
unmapped columns too (not only the mapped ones).

Live gap on an S3 Tables system: the Athena physical-column persistence
(``_dp_discover_athena_physical`` / ``_dp_athena_physical_fields`` /
``_dp_discovery_completeness``) was gated on ``connection_type == ATHENA``, so an
S3 Tables provision wrote only the mapped fields. A record-key PUT naming
unmapped columns was then rejected unknown_column.

These tests run the real persistence code with the S3 Tables connector's own
metadata discovery faked (DESCRIBE with flattened struct leaves + partition
fields), and the full steward chain on top. Athena is the regression guard.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import duckdb
import pytest

import agents.admin_routes as admin_routes
import agents.onboard as onboard
import agents.record_key as record_key
import agents.row_exclusion as row_exclusion
import agents.schema_fields as schema_fields
import agents.state_times as state_times
import agents.storage_bounds as storage_bounds
import agents.system_agent as sa
from agents.connection_types import ATHENA, S3TABLES_DUCKDB
from agents.duckdb_tools import make_duckdb_tools

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYS = "s3t-physical"
TERMINAL = "segment_record_details_raw"
QUALIFIED = f"process_data_global.{TERMINAL}"
# The live 7-column key; id/segment_type/entity_type/revision/cell are UNMAPPED
# physical columns, so they only validate once every physical column is a Field.
PHYSICAL_COLUMNS = [
    {"name": "id", "data_type": "varchar"},
    {"name": "segment_type", "data_type": "varchar"},
    {"name": "entity_type", "data_type": "varchar"},
    {"name": "revision", "data_type": "integer"},
    {"name": "cell", "data_type": "varchar"},
    {"name": "manufacturing_attempt_id", "data_type": "varchar"},
    {"name": "lifecycle_id", "data_type": "varchar"},
    {"name": "observed_at", "data_type": "timestamp"},
    {"name": "source_region", "data_type": "varchar"},     # a partition field
    {"name": "state.started.code", "data_type": "integer"},  # a flattened struct leaf
]
PARTITION_FIELDS = ["source_region"]
RECORD_KEY = ["id", "segment_type", "entity_type", "revision", "cell",
              {"column": "manufacturing_attempt_id", "coalesced": True},
              {"column": "lifecycle_id", "coalesced": True}]


def _discovered():
    return {"system_id": SYS, "schema_name": "s3tablescatalog",
            "tables": [{"name": QUALIFIED, "columns": list(PHYSICAL_COLUMNS)}],
            "partial": False}


# --------------------------------------------------------------------------- #
# A graph that captures the provision writes AND answers the steward reads.     #
# --------------------------------------------------------------------------- #
class _Graph:
    def __init__(self):
        self.entries: dict = {}     # seid -> {"name", "fields": {col: {...}}, "props": {}}
        self.writes: list = []

    def write(self, cypher, params=None):
        p = dict(params or {})
        self.writes.append((cypher, p))
        if "MERGE (se:SchemaEntry {id: $seid})" in cypher and "SET se.name" in cypher:
            self.entries.setdefault(p["seid"], {"name": p.get("name"), "fields": {},
                                                "props": {}})
            self.entries[p["seid"]]["name"] = p.get("name")
            return [{"id": p["seid"]}]
        if "MERGE (f:Field {id: $fid})" in cypher:
            seid = p.get("schema_id")
            if seid in self.entries:
                self.entries[seid]["fields"][p["name"]] = {
                    "is_partition": bool(p.get("isp")), "data_type": p.get("dtype", "")}
            return [{"id": p["fid"]}]
        if "SET se.record_key = $record_key" in cypher:
            e = self.entries.get(p["seid"])
            if e is None:
                return []
            e["props"].update(record_key=p.get("record_key"), record_key_by=p.get("by"),
                              record_key_at=p.get("at"), record_key_source=p.get("source"))
            return [{"seid": p["seid"]}]
        return []            # SchemaEntry/Concept/completeness/rewrite writes: accept

    def query(self, cypher, params=None):
        p = dict(params or {})
        if "RETURN se.name AS name" in cypher and "seid" not in p:
            return [{"name": e["name"]} for e in self.entries.values()]
        if "RETURN count(se) AS n" in cypher:
            return [{"n": 1 if p.get("seid") in self.entries else 0}]
        if "RETURN f.name AS name" in cypher and "HAS_FIELD" in cypher:
            e = self.entries.get(p.get("seid"))
            return [{"name": n} for n in (e or {}).get("fields", {})]
        if "RETURN se.record_key AS record_key" in cypher:
            e = self.entries.get(p.get("seid"))
            props = (e or {}).get("props", {})
            return [{"record_key": props.get("record_key"),
                     "record_key_by": props.get("record_key_by"),
                     "record_key_at": props.get("record_key_at"),
                     "record_key_source": props.get("record_key_source")}]
        return []

    def field_names(self, seid: str) -> set:
        return set(self.entries.get(seid, {}).get("fields", {}))


def _s3t_ctx():
    body = SimpleNamespace(connection_config={"table_bucket_arn": ARN},
                           connection_type=S3TABLES_DUCKDB, table_entities=[])
    return onboard._ProvisionState(
        body=body, system_id=SYS, system_name="S3T", plant="", schema="s3tables",
        rules=[], mappings=[], connection_type=S3TABLES_DUCKDB, cm_items=[],
        tables_seen={QUALIFIED}, table_columns={QUALIFIED: ["id"]})


def _wire(monkeypatch, graph):
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe", graph.write)
    monkeypatch.setattr(onboard, "neptune_query", graph.query)
    monkeypatch.setattr(onboard, "discover_schema", lambda *a, **k: _discovered(), raising=False)
    import agents.duckdb_tools as dt
    monkeypatch.setattr(dt, "discover_schema", lambda *a, **k: _discovered())
    monkeypatch.setattr(onboard, "_s3tables_partition_fields",
                        lambda name, cfg, factory: list(PARTITION_FIELDS))


def setup_function(_):
    for mod in (row_exclusion, state_times, storage_bounds, record_key):
        mod.reset()


def teardown_function(_):
    for mod in (row_exclusion, state_times, storage_bounds, record_key):
        mod.reset()


# --------------------------------------------------------------------------- #
# 1. Every physical column becomes a Field (the core gap)                      #
# --------------------------------------------------------------------------- #
def test_s3tables_provision_persists_every_physical_column(monkeypatch):
    graph = _Graph()
    _wire(monkeypatch, graph)
    ctx = _s3t_ctx()
    onboard._dp_discover_athena_physical(ctx)
    # The SchemaEntry is written under the real (qualified) name, then every
    # physical column as a Field.
    schema_fields.write_schema_entry(SYS, QUALIFIED, write=graph.write)
    onboard._dp_athena_physical_fields(ctx)
    onboard._dp_discovery_completeness(ctx)
    seid = schema_fields.schema_entry_id(SYS, QUALIFIED)
    names = graph.field_names(seid)
    assert names == {c["name"] for c in PHYSICAL_COLUMNS}      # ALL columns, not just mapped
    # The partition field is flagged, the struct leaf is present.
    assert graph.entries[seid]["fields"]["source_region"]["is_partition"] is True
    assert "state.started.code" in names
    # Discovery completeness was recorded for S3 Tables (no longer Athena-gated).
    assert any("se.discovery_complete" in c for c, _ in graph.writes)


# --------------------------------------------------------------------------- #
# 2. Full chain: provision fields -> record-key PUT -> bake -> guard refuses    #
# --------------------------------------------------------------------------- #
def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.process_data_global")
        con.execute(f"CREATE TABLE s3t.process_data_global.{TERMINAL}"
                    "(id BIGINT, observed_at TIMESTAMP, segment_type VARCHAR)")
        con.execute(f"INSERT INTO s3t.process_data_global.{TERMINAL} VALUES "
                    "(1, TIMESTAMP '2026-10-06', 'a'),(2, TIMESTAMP '2026-10-06', 'b')")
        return con
    return factory


def _guarded_run():
    cfg = {"table_bucket_arn": ARN}
    raw = make_duckdb_tools(cfg, source_system=SYS, connection_factory=_factory())
    for t in sa._guard_duckdb_tools(raw, cfg, SYS):
        if getattr(t, "tool_name", "") == "s3tables_run_query":
            return t.__wrapped__
    raise AssertionError("no guarded tool")


def _ask(run, sql):
    call = sa._ModeACall("how many records")
    token = sa._MODE_A_CALL.set(call)
    try:
        return json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)


def test_s3tables_full_chain_unmapped_record_key_is_accepted_and_enforced(monkeypatch):
    graph = _Graph()
    _wire(monkeypatch, graph)
    # Provision persists every physical column (as test 1).
    ctx = _s3t_ctx()
    onboard._dp_discover_athena_physical(ctx)
    schema_fields.write_schema_entry(SYS, QUALIFIED, write=graph.write)
    onboard._dp_athena_physical_fields(ctx)

    # Record-key PUT naming UNMAPPED physical columns must now succeed.
    out = admin_routes.set_record_key_sync(
        SYS, {"table": TERMINAL, "record_key": RECORD_KEY}, "steward",
        connection_type=S3TABLES_DUCKDB, seams=(graph.write, graph.query))
    assert out.get("status") == "ok"

    # Bake reads the graph (resolves terminal -> qualified entry) and remembers
    # under the terminal the guard looks up.
    monkeypatch.setattr(record_key, "_graph_query", lambda: graph.query)
    record_key.remember_from_graph(
        SYS, f"{QUALIFIED}(id: bigint, observed_at: timestamp, segment_type: varchar)")
    assert record_key.columns_for(SYS, TERMINAL)      # a key is remembered

    # Guard: a subset de-dup of the declared key is refused before running.
    run = _guarded_run()
    sql = (f"SELECT COUNT(*) AS n FROM (SELECT * FROM s3t.process_data_global.{TERMINAL} "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY observed_at DESC) = 1) d")
    res = _ask(run, sql)
    assert "error" in res
    assert res.get("error_name") == "RECORD_KEY_DEDUPE" or "record key" in json.dumps(res).lower()


# --------------------------------------------------------------------------- #
# 3. Athena regression: behaviour unchanged                                    #
# --------------------------------------------------------------------------- #
def test_athena_still_persists_physical_columns_and_records_completeness(monkeypatch):
    graph = _Graph()
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe", graph.write)
    monkeypatch.setattr(onboard, "neptune_query", graph.query)
    physical = {"segments": [
        {"name": "id", "data_type": "varchar", "is_partition": False},
        {"name": "ingest_date", "data_type": "varchar", "is_partition": True}]}
    monkeypatch.setattr(onboard, "_discover_athena_physical_columns",
                        lambda *a, **k: physical)
    body = SimpleNamespace(connection_config={"workgroup": "wg", "database": "db",
                                              "region": "us-east-1"},
                           connection_type=ATHENA, table_entities=[])
    ctx = onboard._ProvisionState(
        body=body, system_id="ath-x", system_name="A", plant="", schema="db",
        rules=[], mappings=[], connection_type=ATHENA, cm_items=[],
        tables_seen={"segments"}, table_columns={"segments": ["id"]},
        athena_complete_tables={"segments"})
    onboard._dp_discover_athena_physical(ctx)
    schema_fields.write_schema_entry("ath-x", "segments", write=graph.write)
    onboard._dp_athena_physical_fields(ctx)
    onboard._dp_discovery_completeness(ctx)
    seid = schema_fields.schema_entry_id("ath-x", "segments")
    assert graph.field_names(seid) == {"id", "ingest_date"}
    assert graph.entries[seid]["fields"]["ingest_date"]["is_partition"] is True
    assert any("se.discovery_complete" in c for c, _ in graph.writes)
