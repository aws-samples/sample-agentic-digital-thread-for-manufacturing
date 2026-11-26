# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The REAL steward chain for a declared table fact, end to end, with the graph
seam faked: admin PUT (``set_record_key_sync`` / ``set_row_exclusion_sync``),
then the bake from the graph (``remember_from_graph``), then the guarded
``s3tables_run_query``. No test here sets the registries directly.

The live bug this guards: an S3 Tables system's SchemaEntry is stored under the
namespace-qualified name (``process_data_global.segment_record_details_raw``),
but the admin PUT and the bake use the terminal name the baked table list
carries. The PUT must resolve either form to the real entry, and the bake must
read under the real name while remembering under the terminal name the guard
looks up. Athena (single-part names) is the regression guard: it must behave
exactly as before.
"""
import json
from types import SimpleNamespace

import duckdb
import pytest

import agents.admin_routes as admin_routes
import agents.record_key as record_key
import agents.row_exclusion as row_exclusion
import agents.row_exclusion_manifest as manifest
import agents.state_times as state_times
import agents.storage_bounds as storage_bounds
import agents.system_agent as sa
from agents.connection_types import ATHENA, S3TABLES_DUCKDB
from agents.duckdb_tools import make_duckdb_tools

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYS = "s3t-steward"
TERMINAL = "segment_record_details_raw"
QUALIFIED = f"process_data_global.{TERMINAL}"
LABEL = "non-record rows"
CONDITIONS = [{"column": "route_name", "op": "not_empty"}]
PRED = row_exclusion.predicate_sql(row_exclusion.exclusion_from(LABEL, CONDITIONS))
TABLES_DESC = f"{QUALIFIED}(id: bigint, observed_at: timestamp, route_name: varchar)"
COLUMNS = {"id": "bigint", "observed_at": "timestamp", "route_name": "varchar"}


class _FakeGraph:
    """A tiny in-memory graph honouring exactly the Cypher the steward store and
    the bake issue. One System node with a manifest property, and SchemaEntry
    nodes keyed by their real id (``<system>.<entry_name>``) carrying Fields and
    the two stored facts. ``entry_name`` is the real SchemaEntry name (qualified
    for S3 Tables), so a lookup by the terminal id simply misses, exactly as the
    real graph behaves."""

    def __init__(self, system_id, entry_name, columns):
        self.sid = system_id
        self.entry_name = entry_name
        self.seid = f"{system_id}.{entry_name}"
        self.fields = dict(columns)
        self.props = {}            # SchemaEntry props (record_key, row_exclusion, ...)
        self.manifest = None       # System.row_exclusion_manifest

    # -- read seam ---------------------------------------------------------
    def query(self, cypher, params=None):
        p = params or {}
        if "RETURN se.name AS name, se.system_id AS system_id" not in cypher \
                and "se.name AS name" in cypher and "HAS_SCHEMA" in cypher \
                and "HAS_FIELD" not in cypher and "WHERE" not in cypher:
            # SYSTEM_SCHEMA_ENTRY_NAMES
            return [{"name": self.entry_name}]
        if "RETURN count(se) AS n" in cypher:
            return [{"n": 1 if p.get("seid") == self.seid else 0}]
        if "f.name AS name, f.data_type AS data_type" in cypher and "se.id IN $seids" in cypher:
            # FIELD_TYPES_BATCH (seid-scoped, several tables at once).
            if self.seid not in (p.get("seids") or []):
                return []
            return [{"seid": self.seid, "name": n, "data_type": t}
                    for n, t in self.fields.items()]
        if "f.name AS name, f.data_type AS data_type" in cypher:
            # FIELD_TYPES (one table by seid).
            if p.get("seid") != self.seid:
                return []
            return [{"name": n, "data_type": t} for n, t in self.fields.items()]
        if "RETURN f.name AS name" in cypher and "HAS_FIELD" in cypher:
            if p.get("seid") != self.seid:
                return []
            return [{"name": n} for n in self.fields]
        if "s.row_exclusion_manifest AS row_exclusion_manifest" in cypher:
            return [{"row_exclusion_manifest": self.manifest}]
        if "RETURN se.record_key AS record_key" in cypher:
            if p.get("seid") != self.seid:
                return []
            return [{"record_key": self.props.get("record_key"),
                     "record_key_by": self.props.get("record_key_by"),
                     "record_key_at": self.props.get("record_key_at"),
                     "record_key_source": self.props.get("record_key_source")}]
        if "se.id AS seid" in cypher and "se.id IN $seids" in cypher:
            # READ_ROW_EXCLUSIONS
            if self.seid not in (p.get("seids") or []):
                return []
            if "row_exclusion" not in self.props:
                return []
            return [{"seid": self.seid, "row_exclusion": self.props.get("row_exclusion"),
                     "set_at": self.props.get("row_exclusion_at"),
                     "set_source": self.props.get("row_exclusion_source")}]
        if "RETURN se.row_exclusion AS row_exclusion" in cypher:
            if p.get("seid") != self.seid:
                return []
            return [{"row_exclusion": self.props.get("row_exclusion"),
                     "row_exclusion_by": self.props.get("row_exclusion_by"),
                     "row_exclusion_at": self.props.get("row_exclusion_at"),
                     "row_exclusion_source": self.props.get("row_exclusion_source")}]
        raise AssertionError(f"unexpected read cypher: {cypher!r}")

    # -- write seam --------------------------------------------------------
    def write(self, cypher, params=None):
        p = params or {}
        if "SET s.row_exclusion_manifest = $manifest" in cypher:
            if (self.manifest or "") != (p.get("expected") or ""):
                return []                         # CAS miss
            self.manifest = p.get("manifest")
            return [{"id": self.sid}]
        if "SET se.record_key = $record_key" in cypher:
            if p.get("seid") != self.seid:
                return []
            self.props.update(record_key=p.get("record_key"), record_key_by=p.get("by"),
                              record_key_at=p.get("at"), record_key_source=p.get("source"))
            return [{"seid": self.seid}]
        if "SET se.row_exclusion = $row_exclusion" in cypher:
            if (self.manifest or "") != (p.get("expected") or "") or p.get("seid") != self.seid:
                return []
            self.props.update(row_exclusion=p.get("row_exclusion"),
                              row_exclusion_by=p.get("by"), row_exclusion_at=p.get("at"),
                              row_exclusion_source=p.get("source"))
            return [{"seid": self.seid}]
        raise AssertionError(f"unexpected write cypher: {cypher!r}")


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.process_data_global")
        con.execute(f"CREATE TABLE s3t.process_data_global.{TERMINAL}"
                    "(id BIGINT, observed_at TIMESTAMP, route_name VARCHAR)")
        con.execute(f"INSERT INTO s3t.process_data_global.{TERMINAL} VALUES "
                    "(1, TIMESTAMP '2026-10-06', ''),(2, TIMESTAMP '2026-10-06', 'r1')")
        return con
    return factory


def _guarded_run(cfg):
    raw = make_duckdb_tools(cfg, source_system=SYS, connection_factory=_factory())
    for t in sa._guard_duckdb_tools(raw, cfg, SYS):
        if getattr(t, "tool_name", "") == "s3tables_run_query":
            return t.__wrapped__
    raise AssertionError("no guarded tool")


def _ask(run, sql, windowed=False):
    call = sa._ModeACall("how many records", windowed=windowed)
    token = sa._MODE_A_CALL.set(call)
    try:
        return json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)


def _wire_bake_graph(monkeypatch, graph):
    """Point both bakes' graph read seams at the fake."""
    monkeypatch.setattr(record_key, "_graph_query", lambda: graph.query)
    monkeypatch.setattr(row_exclusion, "_graph_query", lambda: graph.query)


def setup_function(_):
    for mod in (row_exclusion, state_times, storage_bounds, record_key):
        mod.reset()


def teardown_function(_):
    for mod in (row_exclusion, state_times, storage_bounds, record_key):
        mod.reset()


# --------------------------------------------------------------------------- #
# S3 Tables: the qualified SchemaEntry name                                   #
# --------------------------------------------------------------------------- #
def test_record_key_full_chain_s3tables_qualified_name(monkeypatch):
    graph = _FakeGraph(SYS, QUALIFIED, COLUMNS)
    seams = (graph.write, graph.query)
    # Admin PUT with the TERMINAL name (what a steward sees in the baked list);
    # it must resolve to the qualified SchemaEntry and store the key.
    out = admin_routes.set_record_key_sync(
        SYS, {"table": TERMINAL, "record_key": ["id", "route_name"]}, "steward",
        connection_type=S3TABLES_DUCKDB, seams=seams)
    assert out.get("status") == "ok"
    assert graph.props.get("record_key")            # written under the qualified entry
    # Bake reads the graph (terminal from the desc, resolved to the qualified
    # entry) and remembers under the terminal the guard looks up.
    _wire_bake_graph(monkeypatch, graph)
    record_key.remember_from_graph(SYS, TABLES_DESC)
    assert record_key.columns_for(SYS, TERMINAL) == ["id", "route_name"]
    # Guard: a subset de-dup of the declared key is refused before running.
    run = _guarded_run({"table_bucket_arn": ARN})
    sql = (f"SELECT COUNT(*) AS n FROM (SELECT * FROM s3t.process_data_global.{TERMINAL} "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY observed_at DESC) = 1) d")
    res = _ask(run, sql)
    assert "error" in res
    assert res.get("error_name") == "RECORD_KEY_DEDUPE" or "record key" in json.dumps(res).lower()


def test_row_exclusion_full_chain_s3tables_qualified_name(monkeypatch):
    graph = _FakeGraph(SYS, QUALIFIED, COLUMNS)
    seams = (graph.write, graph.query)
    out = admin_routes.set_row_exclusion_sync(
        SYS, {"table": TERMINAL, "label": LABEL, "conditions": CONDITIONS}, "steward",
        connection_type=S3TABLES_DUCKDB, seams=seams)
    assert out.get("status") == "ok"
    assert graph.props.get("row_exclusion")         # written under the qualified entry
    assert QUALIFIED in (manifest.parse(graph.manifest) or {})   # manifest lists the real name
    _wire_bake_graph(monkeypatch, graph)
    row_exclusion.remember_from_graph(SYS, TABLES_DESC)
    assert row_exclusion.exclusion_for(SYS, TERMINAL) is not None   # under the terminal
    # Guard: a read of the rule-bearing table WITHOUT the predicate is refused.
    run = _guarded_run({"table_bucket_arn": ARN})
    res = _ask(run, f"SELECT COUNT(*) FROM s3t.process_data_global.{TERMINAL}")
    assert "error" in res
    assert "row exclusion" in json.dumps(res).lower() or res.get("error_name")


def test_row_exclusion_put_refused_on_non_sql_backend():
    # The allowance is Athena + S3 Tables only; every other backend still 400s.
    resp = admin_routes.set_row_exclusion_sync(
        SYS, {"table": TERMINAL, "label": LABEL, "conditions": CONDITIONS}, "steward",
        connection_type="PostgreSQL", seams=(lambda *a, **k: [], lambda *a, **k: []))
    assert getattr(resp, "status_code", None) == 400
    assert json.loads(bytes(resp.body))["reason"] == "unsupported_backend"


# --------------------------------------------------------------------------- #
# Athena regression: single-part names behave exactly as before               #
# --------------------------------------------------------------------------- #
ATH_SYS = "ath-steward"
ATH_TABLE = "segments"
ATH_COLUMNS = {"id": "bigint", "observed_at": "timestamp", "route_name": "varchar"}
ATH_DESC = f"{ATH_TABLE}(id: bigint, observed_at: timestamp, route_name: varchar)"


def test_record_key_full_chain_athena_single_part_name(monkeypatch):
    graph = _FakeGraph(ATH_SYS, ATH_TABLE, ATH_COLUMNS)
    monkeypatch.setattr(record_key, "_graph_query", lambda: graph.query)
    out = admin_routes.set_record_key_sync(
        ATH_SYS, {"table": ATH_TABLE, "record_key": ["id", "route_name"]}, "steward",
        connection_type=ATHENA, seams=(graph.write, graph.query))
    assert out.get("status") == "ok"
    record_key.remember_from_graph(ATH_SYS, ATH_DESC)
    assert record_key.columns_for(ATH_SYS, ATH_TABLE) == ["id", "route_name"]


def test_row_exclusion_full_chain_athena_single_part_name(monkeypatch):
    graph = _FakeGraph(ATH_SYS, ATH_TABLE, ATH_COLUMNS)
    monkeypatch.setattr(row_exclusion, "_graph_query", lambda: graph.query)
    out = admin_routes.set_row_exclusion_sync(
        ATH_SYS, {"table": ATH_TABLE, "label": LABEL, "conditions": CONDITIONS}, "steward",
        connection_type=ATHENA, seams=(graph.write, graph.query))
    assert out.get("status") == "ok"
    assert ATH_TABLE in (manifest.parse(graph.manifest) or {})
    row_exclusion.remember_from_graph(ATH_SYS, ATH_DESC)
    assert row_exclusion.exclusion_for(ATH_SYS, ATH_TABLE) is not None


def test_wrong_qualified_name_does_not_resolve_by_terminal():
    # A dotted name must match a SchemaEntry exactly: a wrong namespace is an
    # unknown table, never the same-terminal entry of another namespace.
    graph = _FakeGraph(SYS, QUALIFIED, COLUMNS)
    out = admin_routes.set_row_exclusion_sync(
        SYS, {"table": f"wrong_namespace.{QUALIFIED.rpartition('.')[2]}",
              "label": LABEL, "conditions": CONDITIONS},
        "steward", connection_type=S3TABLES_DUCKDB, seams=(graph.write, graph.query))
    assert getattr(out, "status_code", None) == 422
    assert not graph.props.get("row_exclusion")
