# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0077 D1/D2 — ``schema_repair`` materialisation core + bulk repair (offline).

A local ``GraphFake`` records writes and answers the ``schema_fields``
probes from what was written; ``_source_columns`` is stubbed (the Athena and
non-Athena source branches are covered separately). No AWS, no network.
"""
from __future__ import annotations

import pytest

import agents.schema_drift as schema_drift
import agents.schema_fields as sf
import agents.schema_repair as sr
import agents.self_learning as self_learning

try:
    from unit.test_self_learning_lifecycle import FakeTable
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable


class GraphFake:
    """Records writes; answers the probes/readbacks from what was written.
    ``tamper`` overrides read-back Field properties (``entry_tamper`` the
    SchemaEntry's) so every exact-readback mismatch is reproducible."""

    def __init__(self, *, schema_entries=("erp.work_orders",), system=True,
                 readback=True, existing_fields=(), tamper=None, entry_tamper=None):
        self.writes: list = []
        self.schema_entries = set(schema_entries)
        self.system = system
        self.readback = readback
        self.tamper = dict(tamper or {})
        self.entry_tamper = dict(entry_tamper or {})
        self.fields = {fid: fid.rsplit(".", 1)[-1] for fid in existing_fields}
        self.props: dict = {}
        self.entry_props: dict = {}

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        if cypher == sf.FIELD_MERGE:
            self.fields[params["fid"]] = params["name"]
            self.props[params["fid"]] = {
                "name": params["name"], "schema_id": params["schema_id"],
                "system_id": params["sid"], "data_type": params["dtype"],
                "is_partition": params["isp"]}
        if cypher == sf.SCHEMA_ENTRY_MERGE:
            self.schema_entries.add(params["seid"])
            self.entry_props[params["seid"]] = {"name": params["name"],
                                                "system_id": params["sid"]}
        return True

    def query(self, cypher, params=None):
        if cypher == sf.SCHEMA_ENTRY_EXISTS:
            return [{"n": int(params["seid"] in self.schema_entries)}]
        if "MATCH (s:System {id: $sid}) RETURN count(s)" in cypher:
            return [{"n": int(self.system)}]
        if cypher == sf.TABLE_FIELD_NAMES:
            prefix = params["seid"] + "."
            return [{"name": n} for f, n in self.fields.items() if f.startswith(prefix)]
        if cypher == sf.FIELD_READBACK:
            if not self.readback or params["fid"] not in self.props:
                return []
            return [{**self.props[params["fid"]], **self.tamper}]
        if cypher == sf.SCHEMA_ENTRY_READBACK:
            if not self.readback or params["seid"] not in self.entry_props:
                return []
            return [{**self.entry_props[params["seid"]], **self.entry_tamper}]
        return []

    def cyphers(self):
        return [c for c, _p in self.writes]


def _item(reason="column_added", from_field="work_orders.priority", system="erp"):
    return {"mapping_id": "m-1", "reason": reason, "from_field": from_field,
            "from_system": system, "proposed_type": "schema_drift"}


def _source(monkeypatch, cols, complete=True):
    calls: list = []
    monkeypatch.setattr(sr, "_source_columns",
                        lambda sid, table: calls.append((sid, table)) or (list(cols), complete))
    return calls


_WO = [{"name": "id", "data_type": "integer"}, {"name": "priority", "data_type": "text"}]


def test_column_added_writes_field_edge_and_completeness(monkeypatch):
    _source(monkeypatch, _WO)
    graph = GraphFake(existing_fields=("erp.work_orders.id",))
    out = sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes == [
        (sf.FIELD_MERGE, {"fid": "erp.work_orders.priority", "name": "priority",
                          "schema_id": "erp.work_orders", "sid": "erp",
                          "isp": False, "dtype": "text"}),
        (sf.HAS_FIELD_MERGE, {"seid": "erp.work_orders", "fid": "erp.work_orders.priority"}),
        (sf.DISCOVERY_COMPLETENESS_SET, {"seid": "erp.work_orders", "complete": True,
                                         "count": 2}),
    ]
    assert out == {"node_id": "erp.work_orders.priority", "schema_entry": "erp.work_orders",
                   "fields": ["priority"], "field_count": 1, "table_created": False}


def test_completeness_stays_false_while_columns_are_missing(monkeypatch):
    _source(monkeypatch, _WO + [{"name": "status", "data_type": "text"}])
    graph = GraphFake()
    sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes[-1] == (sf.DISCOVERY_COMPLETENESS_SET,
                                {"seid": "erp.work_orders", "complete": False, "count": 1})


def test_struct_parent_materialises_every_leaf(monkeypatch):
    _source(monkeypatch, [{"name": "oee.performance", "data_type": "double"},
                          {"name": "oee.availability", "data_type": "double"},
                          {"name": "oeex", "data_type": "text"}])
    graph = GraphFake()
    out = sr.materialise_drift(_item(from_field="work_orders.oee"),
                               write=graph.write, query=graph.query)
    assert out["fields"] == ["oee.availability", "oee.performance"]
    assert graph.cyphers().count(sf.FIELD_MERGE) == 2
    assert out["node_id"] == "erp.work_orders"  # several leaves → the owning entry


def test_missing_schema_entry_is_value_error_with_zero_writes(monkeypatch):
    _source(monkeypatch, _WO)
    graph = GraphFake(schema_entries=())
    with pytest.raises(ValueError, match="not in the onboarded schema"):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes == []


def test_graph_unreachable_is_runtime_error(monkeypatch):
    _source(monkeypatch, _WO)
    graph = GraphFake(schema_entries=(), system=False)
    with pytest.raises(RuntimeError, match="graph unavailable"):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes == []


def test_column_not_in_source_is_value_error_with_zero_writes(monkeypatch):
    _source(monkeypatch, [{"name": "id"}])
    graph = GraphFake()
    with pytest.raises(ValueError, match="not visible in the source"):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes == []


def test_readback_miss_is_runtime_error(monkeypatch):
    _source(monkeypatch, _WO)
    graph = GraphFake(readback=False)
    with pytest.raises(RuntimeError, match="readback mismatch for erp.work_orders.priority"):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)


@pytest.mark.parametrize("prop, bad", [
    ("name", "wrong"), ("schema_id", "erp.other"), ("system_id", "mes"),
    ("data_type", "varchar"), ("is_partition", True), ("is_partition", "false"),
])
def test_readback_property_mismatch_is_runtime_error(monkeypatch, prop, bad):
    """Exact readback compares the FULL MERGEd Field shape, never existence."""
    _source(monkeypatch, [{"name": "priority", "data_type": "integer"}])
    graph = GraphFake(tamper={prop: bad})
    with pytest.raises(RuntimeError, match=rf"readback mismatch .*\['{prop}'\]"):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)


@pytest.mark.parametrize("tamper", [{"name": "other"}, {"system_id": "mes"}])
def test_table_added_schema_entry_mismatch_is_runtime_error(monkeypatch, tamper):
    _source(monkeypatch, [{"name": "id", "data_type": "integer"}])
    graph = GraphFake(schema_entries=(), entry_tamper=tamper)
    with pytest.raises(RuntimeError, match="readback mismatch for erp.segment_events:"):
        sr.materialise_drift(_item("table_added", "segment_events."),
                             write=graph.write, query=graph.query)


def test_table_added_schema_entry_missing_is_runtime_error(monkeypatch):
    _source(monkeypatch, [{"name": "id", "data_type": "integer"}])
    graph = GraphFake(schema_entries=())
    graph.query_orig = graph.query
    graph.query = lambda c, p=None: [] if c == sf.SCHEMA_ENTRY_READBACK else graph.query_orig(c, p)
    with pytest.raises(RuntimeError, match="readback mismatch for erp.segment_events"):
        sr.materialise_drift(_item("table_added", "segment_events."),
                             write=graph.write, query=graph.query)


def test_accept_reread_bypasses_a_warmed_schema_cache(monkeypatch):
    """A describe warmed with OLD metadata never answers the accept-time read:
    the second (current) GetTableMetadata response wins, and a gone target
    fails closed (EntityNotFound → no columns → not visible)."""
    import agents.athena_tools as at
    responses = [
        {"TableMetadata": {"Columns": [{"Name": "priority", "Type": "string"}]}},
        {"TableMetadata": {"Columns": [{"Name": "priority", "Type": "bigint"}],
                           "PartitionKeys": [{"Name": "dt", "Type": "string"}]}},
    ]

    class Client:
        def get_table_metadata(self, **_kw):
            if not responses:
                raise RuntimeError("EntityNotFoundException: gone")
            return responses.pop(0)

    monkeypatch.setattr(at, "_SCHEMA_CACHE", {})
    monkeypatch.delenv("ADT_ATHENA_SCHEMA_CACHE_TTL_SECONDS", raising=False)
    cfg = {"workgroup": "wg", "database": "db", "region": "us-east-1"}
    client = Client()
    warm, _ = at.describe_physical_columns_bounded("work_orders", cfg, client=client)
    assert warm == [{"name": "priority", "data_type": "string", "is_partition": False}]
    cached, _ = at.describe_physical_columns_bounded("work_orders", cfg, client=client)
    assert cached == warm  # the normal path still serves the cache
    fresh, complete = at.describe_physical_columns_bounded(
        "work_orders", cfg, client=client, refresh=True)
    assert fresh == [{"name": "priority", "data_type": "bigint", "is_partition": False},
                     {"name": "dt", "data_type": "string", "is_partition": True}]
    assert complete is True
    gone, _ = at.describe_physical_columns_bounded(
        "work_orders", cfg, client=client, refresh=True)
    assert gone == []
    graph = GraphFake()
    monkeypatch.setattr(sr, "_source_columns", lambda sid, table: (gone, True))
    with pytest.raises(ValueError, match="not visible"):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes == []


@pytest.mark.parametrize("item", [
    _item(from_field="a b.x"), _item(from_field="work_orders.1x"),
    _item(from_field="work_orders.a..b"), _item(from_field="nodot"),
    _item(from_field=".x"), _item(system="bad/system"), _item(system=""),
])
def test_bad_identifiers_fail_closed_before_any_read(monkeypatch, item):
    calls = _source(monkeypatch, _WO)
    graph = GraphFake()
    with pytest.raises(ValueError):
        sr.materialise_drift(item, write=graph.write, query=graph.query)
    assert graph.writes == [] and calls == []


def test_invalid_source_column_name_fails_closed(monkeypatch):
    _source(monkeypatch, [{"name": "priority"}, {"name": "priority.bad-leaf"}])
    graph = GraphFake()
    with pytest.raises(ValueError):
        sr.materialise_drift(_item(), write=graph.write, query=graph.query)
    assert graph.writes == []


def test_table_added_writes_entry_then_fields_then_completeness(monkeypatch):
    _source(monkeypatch, [{"name": "id", "data_type": "integer"},
                          {"name": "ts", "data_type": "timestamp", "is_partition": True}])
    graph = GraphFake(schema_entries=())
    out = sr.materialise_drift(_item("table_added", "segment_events."),
                               write=graph.write, query=graph.query)
    assert graph.cyphers() == [sf.SCHEMA_ENTRY_MERGE, sf.FIELD_MERGE, sf.HAS_FIELD_MERGE,
                               sf.FIELD_MERGE, sf.HAS_FIELD_MERGE,
                               sf.DISCOVERY_COMPLETENESS_SET]
    assert graph.writes[-1][1] == {"seid": "erp.segment_events", "complete": True, "count": 2}
    assert graph.writes[3][1]["isp"] is True
    assert out["node_id"] == "erp.segment_events" and out["table_created"] is True


def test_table_added_over_cap_fails_closed(monkeypatch):
    monkeypatch.setattr(sr, "MATERIALISE_COLUMN_CAP", 1)
    _source(monkeypatch, _WO)
    graph = GraphFake(schema_entries=())
    with pytest.raises(ValueError, match="cap"):
        sr.materialise_drift(_item("table_added", "segment_events."),
                             write=graph.write, query=graph.query)
    assert graph.writes == []


@pytest.mark.parametrize("reason", ["column_removed", "column_retyped", "table_removed"])
def test_destructive_kinds_are_not_materialisable(monkeypatch, reason):
    calls = _source(monkeypatch, _WO)
    graph = GraphFake()
    with pytest.raises(ValueError, match="not materialisable"):
        sr.materialise_drift(_item(reason), write=graph.write, query=graph.query)
    assert graph.writes == [] and calls == []


# --------------------------------------------------------------------------- #
# Source branches
# --------------------------------------------------------------------------- #

def test_source_columns_athena_uses_the_metadata_describe(monkeypatch):
    import agents.athena_tools as athena_tools
    import agents.catalog as catalog_mod
    from agents.connection_types import ATHENA
    monkeypatch.setattr(catalog_mod.catalog, "get", lambda sid: {
        "connection_type": ATHENA, "connection_config": {"database": "db", "workgroup": "wg"}})
    monkeypatch.setattr(sr, "_athena_config", lambda sid, entry: {"cfg": sid})
    seen: list = []
    monkeypatch.setattr(athena_tools, "describe_physical_columns_bounded",
                        lambda table, cfg, **kw: seen.append((table, cfg, kw)) or (
                            [{"name": "id", "data_type": "int", "is_partition": False}], True))
    cols, complete = sr._source_columns("erp", "work_orders")
    # refresh=True: the accept never reads a describe the drift check warmed.
    assert seen == [("work_orders", {"cfg": "erp"}, {"refresh": True})]
    assert cols == [{"name": "id", "data_type": "int", "is_partition": False}]
    assert complete is True


def test_source_columns_non_athena_uses_the_drift_discover(monkeypatch):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod.catalog, "get",
                        lambda sid: {"connection_type": "PostgreSQL"})
    monkeypatch.setattr(schema_drift, "_discover_fresh", lambda sid: {"tables": [
        {"name": "Work_Orders", "columns": [{"name": "priority", "data_type": "text"}]}]})
    cols, complete = sr._source_columns("erp", "work_orders")
    assert cols == [{"name": "priority", "data_type": "text", "is_partition": False}]
    assert complete is True
    with pytest.raises(ValueError, match="not visible"):
        sr._source_columns("erp", "segment_events")
    monkeypatch.setattr(schema_drift, "_discover_fresh", lambda sid: {"error": "x"})
    with pytest.raises(RuntimeError, match="DiscoveryError"):
        sr._source_columns("erp", "work_orders")


# --------------------------------------------------------------------------- #
# repair_schema (the route body)
# --------------------------------------------------------------------------- #

def _pending(mid, from_field, *, marked=True, reason="column_added", system="erp",
             source="agent", method="schema-drift"):
    return {"mapping_id": mid, "discovered_at": "2026-09-01T00:00:00+00:00",
            "status": "pending", "proposed_type": "schema_drift", "reason": reason,
            "from_system": system, "from_field": from_field, "source": source,
            "method": method,
            "to_description": schema_drift.REDETECTED_DESCRIPTION if marked else ""}


def _accepted(mid, from_field, *, reason="column_added", system="erp"):
    """An earlier steward accept of the SAME additive reason on the subject —
    the stored lineage that makes a marked re-file repair-eligible."""
    return {"mapping_id": mid, "discovered_at": "2026-08-01T00:00:00+00:00",
            "status": "accepted", "accepted_at": "2026-08-02T00:00:00+00:00",
            "proposed_type": "schema_drift", "reason": reason,
            "from_system": system, "from_field": from_field,
            "source": "agent", "method": "schema-drift", "to_description": ""}


@pytest.fixture
def repair_env(monkeypatch):
    fake = FakeTable()
    for row in (_pending("m-c", "work_orders.c"), _pending("m-a", "work_orders.a"),
                _pending("m-b", "work_orders.b"),
                _pending("m-u", "work_orders.u", marked=False),
                _pending("m-other", "work_orders.x", system="mes"),
                # lineage: each marked row has an accepted same-reason predecessor
                _accepted("p-c", "work_orders.c"), _accepted("p-a", "work_orders.a"),
                _accepted("p-b", "work_orders.b"), _accepted("p-x", "work_orders.x", system="mes")):
        fake.put_item(Item=row)
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    accepted_rows = {r["from_field"]: r for r in fake.scan()["Items"] if r.get("accepted_at")}

    def accept_generation(system_id, from_field, reason):
        row = accepted_rows.get(from_field)
        return 1 if row and row["from_system"] == system_id and row["reason"] == reason else 0
    monkeypatch.setattr(schema_drift, "_accept_generation", accept_generation)
    checks: list = []
    monkeypatch.setattr(schema_drift, "run_drift_check",
                        lambda sid: checks.append(sid) or {"system_id": sid, "status": "ok"})
    accepted: list = []

    def accept(mapping_id, actor=""):
        accepted.append((mapping_id, actor))
        return {"status": "accepted", "materialised": True}
    monkeypatch.setattr(self_learning, "accept_mapping", accept)
    return {"checks": checks, "accepted": accepted}


def test_repair_accepts_only_marked_rows(repair_env):
    report = sr.repair_schema("erp", actor="steward@example.com")
    assert repair_env["checks"] == ["erp"]
    assert [m for m, _a in repair_env["accepted"]] == ["m-a", "m-b", "m-c"]
    assert {a for _m, a in repair_env["accepted"]} == {"steward@example.com"}
    assert report == {"system_id": "erp", "detection": {"system_id": "erp", "status": "ok"},
                      "candidates": 3, "materialised": 3, "failed": [], "failed_count": 0,
                      "remaining": 0, "pending_unreviewed": 1}


def test_repair_is_bounded(repair_env, monkeypatch):
    monkeypatch.setattr(sr, "REPAIR_ACCEPT_CAP", 2)
    report = sr.repair_schema("erp")
    assert report["materialised"] == 2 and report["remaining"] == 1


def test_repair_reports_per_row_failures_and_continues(repair_env, monkeypatch):
    def accept(mapping_id, actor=""):
        if mapping_id == "m-b":
            raise RuntimeError("readback failed")
        if mapping_id == "m-c":
            return {"error": "schema drift not materialised: gone", "http_status": 422}
        return {"status": "accepted", "materialised": True}
    monkeypatch.setattr(self_learning, "accept_mapping", accept)
    report = sr.repair_schema("erp")
    assert report["materialised"] == 1
    assert report["failed"] == [
        {"mapping_id": "m-b", "error": "RuntimeError"},
        {"mapping_id": "m-c", "error": "schema drift not materialised: gone"}]
    assert report["failed_count"] == 2


def test_repair_without_detect_skips_the_check(repair_env):
    report = sr.repair_schema("erp", detect=False)
    assert repair_env["checks"] == [] and report["detection"] is None
    assert report["materialised"] == 3


def test_skipped_detection_accepts_nothing(repair_env, monkeypatch):
    monkeypatch.setattr(schema_drift, "run_drift_check",
                        lambda sid: {"system_id": sid, "status": "skipped:no-baseline"})
    report = sr.repair_schema("erp")
    assert repair_env["accepted"] == [] and report["materialised"] == 0
    assert report["detection"]["status"] == "skipped:no-baseline"


@pytest.mark.parametrize("bad", ["", "a/b", "..", "a b"])
def test_repair_rejects_bad_system_ids(repair_env, bad):
    with pytest.raises(ValueError):
        sr.repair_schema(bad)
    assert repair_env["checks"] == [] and repair_env["accepted"] == []


def test_split_from_field():
    assert sr.split_from_field("work_orders.oee.availability") == ("work_orders",
                                                                   "oee.availability")
    assert sr.split_from_field("segment_events.") == ("segment_events", "")
    for bad in ("nodot", ".x", "", None):
        with pytest.raises(ValueError):
            sr.split_from_field(bad)


def test_repair_ignores_a_marker_without_an_accepted_predecessor(repair_env, monkeypatch):
    """ADR-0077 D2: eligibility is PROVEN from stored lineage — a marked pending
    row with no accepted same-reason predecessor (misfiled / hand-crafted)
    stays in the ordinary review queue."""
    fake = self_learning._get_table()
    fake.put_item(Item=_pending("m-orphan", "work_orders.orphan"))          # marked, no lineage
    fake.put_item(Item=_pending("m-hand", "work_orders.hand", source="user"))  # wrong provenance
    fake.put_item(Item=_accepted("p-hand", "work_orders.hand"))
    report = sr.repair_schema("erp")
    assert [m for m, _a in repair_env["accepted"]] == ["m-a", "m-b", "m-c"]
    assert report["pending_unreviewed"] == 3  # m-u, m-orphan, m-hand


def test_repair_ignores_a_marker_whose_lineage_is_a_different_reason(repair_env):
    fake = self_learning._get_table()
    fake.put_item(Item=_pending("m-rm", "work_orders.rm"))
    fake.put_item(Item=_accepted("p-rm", "work_orders.rm", reason="column_removed"))
    sr.repair_schema("erp")
    assert "m-rm" not in [m for m, _a in repair_env["accepted"]]
