# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the operational graph snapshot (agents/graph_snapshot.py +
agents/graph_snapshot_routes.py).

Fully offline: the graph is a FakeGraph double that understands EXACTLY the
bounded openCypher shapes the module emits (pagination pages, batched wipe,
UNWIND creates, key cleanup), so the tests pin both the query text and the
phase ordering. Route tests follow the repo's admin-route pattern
(FastAPI + inject-middleware + TestClient) and the async-job tests call the
handlers directly under asyncio.run (same approach as
test_onboard_reset_async.py — TestClient's per-request loop cannot host a
background task across requests).
"""
import asyncio
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.graph_snapshot as gs  # noqa: E402
import agents.graph_snapshot_routes as gsr  # noqa: E402


# ── FakeGraph: a double for the exact cypher shapes the module emits ─────────

_NODE_PAGE_RE = re.compile(
    r"^MATCH \(n\) RETURN n ORDER BY id\(n\) SKIP (\d+) LIMIT (\d+)$")
_EDGE_PAGE_RE = re.compile(
    r"^MATCH \(a\)-\[r\]->\(b\) RETURN id\(a\) AS from_gid, id\(b\) AS to_gid, "
    r"r ORDER BY id\(r\) SKIP (\d+) LIMIT (\d+)$")
_COUNT_NODES = "MATCH (n) RETURN count(n) AS n"
_KEY_COUNT = "MATCH (n) WHERE n._import_key IS NOT NULL RETURN count(n) AS n"
_WIPE_RE = re.compile(r"^MATCH \(n\) WITH n LIMIT (\d+) DETACH DELETE n$")
_NODE_CREATE_RE = re.compile(
    r"^UNWIND \$rows AS row CREATE \(n((?::\w+)*)\) "
    r"SET n \+= row\.props, n\._import_key = row\.key$")
_EDGE_CREATE_RE = re.compile(
    r"^UNWIND \$rows AS row MATCH \(a \{_import_key: row\.from_key\}\) "
    r"MATCH \(b \{_import_key: row\.to_key\}\) "
    r"CREATE \(a\)-\[r:(\w+)\]->\(b\) SET r \+= row\.props$")
_KEY_REMOVE_RE = re.compile(
    r"^MATCH \(n\) WHERE n\._import_key IS NOT NULL "
    r"WITH n LIMIT (\d+) REMOVE n\._import_key$")


class FakeGraph:
    """In-memory graph honouring the module's bounded query/write shapes."""

    def __init__(self):
        self.nodes: list[dict] = []   # {gid, labels, props}
        self.edges: list[dict] = []   # {gid, type, from_gid, to_gid, props}
        self._next = 0
        self.queries: list[str] = []
        self.writes: list[tuple[str, dict | None]] = []

    def add_node(self, labels, props) -> str:
        gid = f"n{self._next:06d}"
        self._next += 1
        self.nodes.append({"gid": gid, "labels": list(labels),
                           "props": dict(props)})
        return gid

    def add_edge(self, etype, from_index, to_index, props=None) -> None:
        gid = f"e{self._next:06d}"
        self._next += 1
        self.edges.append({
            "gid": gid, "type": etype,
            "from_gid": self.nodes[from_index]["gid"],
            "to_gid": self.nodes[to_index]["gid"],
            "props": dict(props or {})})

    # -- reader ------------------------------------------------------------ #
    def query(self, cypher, parameters=None):
        self.queries.append(cypher)
        if cypher == _COUNT_NODES:
            return [{"n": len(self.nodes)}]
        if cypher == _KEY_COUNT:
            keyed = sum(1 for n in self.nodes if "_import_key" in n["props"])
            return [{"n": keyed}]
        match = _NODE_PAGE_RE.match(cypher)
        if match:
            skip, limit = int(match.group(1)), int(match.group(2))
            page = sorted(self.nodes, key=lambda n: n["gid"])[skip:skip + limit]
            return [{"n": {"~id": n["gid"], "~labels": list(n["labels"]),
                           "~properties": dict(n["props"])}} for n in page]
        match = _EDGE_PAGE_RE.match(cypher)
        if match:
            skip, limit = int(match.group(1)), int(match.group(2))
            page = sorted(self.edges, key=lambda e: e["gid"])[skip:skip + limit]
            return [{"from_gid": e["from_gid"], "to_gid": e["to_gid"],
                     "r": {"~id": e["gid"], "~type": e["type"],
                           "~properties": dict(e["props"])}} for e in page]
        raise AssertionError(f"FakeGraph: unexpected read: {cypher!r}")

    # -- writer ------------------------------------------------------------ #
    def write(self, cypher, parameters=None):
        self.writes.append((cypher, parameters))
        match = _WIPE_RE.match(cypher)
        if match:
            keep = sorted(self.nodes, key=lambda n: n["gid"])[int(match.group(1)):]
            kept_gids = {n["gid"] for n in keep}
            self.nodes = [n for n in self.nodes if n["gid"] in kept_gids]
            self.edges = [e for e in self.edges
                          if e["from_gid"] in kept_gids and e["to_gid"] in kept_gids]
            return True
        match = _NODE_CREATE_RE.match(cypher)
        if match:
            labels = [l for l in match.group(1).split(":") if l]
            for row in parameters["rows"]:
                self.add_node(labels,
                              {**row["props"], "_import_key": row["key"]})
            return True
        match = _EDGE_CREATE_RE.match(cypher)
        if match:
            by_key = {n["props"].get("_import_key"): n for n in self.nodes}
            for row in parameters["rows"]:
                a, b = by_key[row["from_key"]], by_key[row["to_key"]]
                gid = f"e{self._next:06d}"
                self._next += 1
                self.edges.append({"gid": gid, "type": match.group(1),
                                   "from_gid": a["gid"], "to_gid": b["gid"],
                                   "props": dict(row["props"])})
            return True
        match = _KEY_REMOVE_RE.match(cypher)
        if match:
            budget = int(match.group(1))
            for node in sorted(self.nodes, key=lambda n: n["gid"]):
                if budget and "_import_key" in node["props"]:
                    del node["props"]["_import_key"]
                    budget -= 1
            return True
        raise AssertionError(f"FakeGraph: unexpected write: {cypher!r}")


def _seed_graph() -> FakeGraph:
    """Variety: multi-label, label-free, id-free nodes; edge + float props."""
    g = FakeGraph()
    g.add_node(["System"], {"id": "erp", "name": "ERP", "status": "onboarded"})
    g.add_node(["SchemaEntry"], {"id": "erp:orders", "name": "orders"})
    g.add_node(["Asset", "Leaf"], {"source_id": "a-1", "property_count": 0})
    g.add_node([], {"note": "no labels, no id property", "weight": 1.25})
    g.add_node(["Field"], {"name": "col_a", "schema_id": "erp:orders"})
    g.add_node(["Field"], {"name": "col_b", "schema_id": "erp:orders"})
    g.add_node(["Concept"], {"id": "work_order"})
    g.add_edge("HAS_SCHEMA", 0, 1)
    g.add_edge("HAS_FIELD", 1, 4)
    g.add_edge("MAPS_TO", 4, 6, {"confidence": 0.87, "role": "identifier"})
    g.add_edge("HAS_PARENT", 3, 2)
    return g


# ── Export: pagination + positional edge wiring ──────────────────────────────

def test_export_pages_are_bounded_skip_limit():
    g = _seed_graph()
    snapshot = gs.export_snapshot(query=g.query, page_size=3)
    node_pages = [q for q in g.queries if _NODE_PAGE_RE.match(q)]
    edge_pages = [q for q in g.queries if _EDGE_PAGE_RE.match(q)]
    assert [_NODE_PAGE_RE.match(q).groups() for q in node_pages] == [
        ("0", "3"), ("3", "3"), ("6", "3")]
    assert [_EDGE_PAGE_RE.match(q).groups() for q in edge_pages] == [
        ("0", "3"), ("3", "3")]
    assert snapshot["node_count"] == 7 and snapshot["edge_count"] == 4


def test_export_wires_edges_by_positional_index_not_id_property():
    g = _seed_graph()
    snapshot = gs.export_snapshot(query=g.query, page_size=3)
    nodes = snapshot["nodes"]
    # The HAS_PARENT edge joins the label-free id-less node to the Asset.
    parent = next(e for e in snapshot["edges"] if e["type"] == "HAS_PARENT")
    assert nodes[parent["from"]]["props"]["note"].startswith("no labels")
    assert nodes[parent["to"]]["props"]["source_id"] == "a-1"
    # And no edge carries a gid or assumes an id property.
    assert all(set(e) == {"type", "from", "to", "props"}
               for e in snapshot["edges"])


def test_export_format_envelope():
    g = _seed_graph()
    snapshot = gs.export_snapshot(query=g.query, page_size=100)
    assert snapshot["format"] == "adt-graph-snapshot/1"
    assert snapshot["exported_at"]
    assert len(snapshot["nodes"]) == snapshot["node_count"]
    assert len(snapshot["edges"]) == snapshot["edge_count"]


def test_export_chunk_cursor_walk_and_malformed_cursor():
    g = _seed_graph()
    chunk = gs.export_chunk("", query=g.query, page_size=4)
    assert chunk["kind"] == "nodes" and chunk["next_cursor"] == "nodes:4"
    chunk = gs.export_chunk("nodes:4", query=g.query, page_size=4)
    assert chunk["kind"] == "nodes" and chunk["next_cursor"] == "edges:0"
    chunk = gs.export_chunk("edges:0", query=g.query, page_size=4)
    assert chunk["kind"] == "edges" and chunk["next_cursor"] == "edges:4"
    chunk = gs.export_chunk("edges:4", query=g.query, page_size=4)
    assert chunk["items"] == [] and chunk["next_cursor"] is None
    for bad in ("bogus", "nodes:x", "edges", "nodes:-1"):
        with pytest.raises(ValueError):
            gs.export_chunk(bad, query=g.query)


def test_assemble_rejects_edge_to_unknown_gid():
    with pytest.raises(ValueError, match="graph changed during export"):
        gs.assemble_snapshot(
            [{"gid": "n1", "labels": [], "props": {}}],
            [{"type": "X", "from_gid": "n1", "to_gid": "nMISSING", "props": {}}])


# ── Validation: unknown format + injection guard ─────────────────────────────

def test_unknown_format_version_refused():
    for fmt in ("adt-graph-snapshot/2", "", None, 7):
        with pytest.raises(ValueError, match="unsupported snapshot format"):
            gs.validate_snapshot({"format": fmt, "nodes": [], "edges": []})
    with pytest.raises(ValueError):
        gs.validate_snapshot("not-a-dict")


def test_unsafe_labels_and_edge_types_refused():
    base = {"format": gs.FORMAT_VERSION, "edges": []}
    for label in ("Bad Label", "x`) DETACH DELETE (m", "1Numeric", ""):
        with pytest.raises(ValueError, match="unsafe label"):
            gs.validate_snapshot(
                {**base, "nodes": [{"labels": [label], "props": {}}]})
    snap = {"format": gs.FORMAT_VERSION,
            "nodes": [{"labels": ["A"], "props": {}}] * 2,
            "edges": [{"type": "e]->(x) MATCH (y", "from": 0, "to": 1,
                       "props": {}}]}
    with pytest.raises(ValueError, match="unsafe type"):
        gs.validate_snapshot(snap)


def test_edge_index_out_of_range_refused():
    snap = {"format": gs.FORMAT_VERSION,
            "nodes": [{"labels": ["A"], "props": {}}],
            "edges": [{"type": "REL", "from": 0, "to": 1, "props": {}}]}
    with pytest.raises(ValueError, match="node index"):
        gs.validate_snapshot(snap)


# ── Import: two-phase order + batch bounds ───────────────────────────────────

def _write_kind(cypher: str) -> str:
    for regex, kind in ((_WIPE_RE, "wipe"), (_NODE_CREATE_RE, "node"),
                        (_EDGE_CREATE_RE, "edge"), (_KEY_REMOVE_RE, "remove")):
        if regex.match(cypher):
            return kind
    raise AssertionError(f"unclassified write: {cypher!r}")


def test_import_phase_order_and_batch_bounds():
    source = _seed_graph()
    snapshot = gs.export_snapshot(query=source.query, page_size=3)
    target = FakeGraph()
    for i in range(5):  # stale content that MUST be wiped first
        target.add_node(["Stale"], {"id": f"stale-{i}"})
    result = gs.import_snapshot(snapshot, query=target.query,
                                write=target.write, wipe_batch=2,
                                import_batch=2)
    kinds = [_write_kind(c) for c, _ in target.writes]
    assert kinds == sorted(kinds, key=["wipe", "node", "edge", "remove"].index)
    assert kinds.count("wipe") == 3          # 5 stale nodes / batch 2
    for cypher, params in target.writes:
        if _WIPE_RE.match(cypher):
            assert _WIPE_RE.match(cypher).group(1) == "2"
        if params is not None:
            assert len(params["rows"]) <= 2  # bounded UNWIND chunks
    assert result["nodes_wiped"] == 5
    assert result["nodes_created"] == 7 and result["edges_created"] == 4
    assert result["import_keys_removed"] == 7
    assert all("_import_key" not in n["props"] for n in target.nodes)


def test_import_refuses_unknown_format_before_wiping():
    target = FakeGraph()
    target.add_node(["Precious"], {"id": "keep-me"})
    with pytest.raises(ValueError, match="unsupported snapshot format"):
        gs.import_snapshot({"format": "adt-graph-snapshot/999",
                            "nodes": [], "edges": []},
                           query=target.query, write=target.write)
    assert target.writes == [] and len(target.nodes) == 1


def test_import_raises_when_write_fails():
    snapshot = {"format": gs.FORMAT_VERSION,
                "nodes": [{"labels": ["A"], "props": {}}], "edges": []}
    g = FakeGraph()
    g.add_node(["Old"], {})
    with pytest.raises(RuntimeError):
        gs.import_snapshot(snapshot, query=g.query,
                           write=lambda *a, **k: False)


# ── Import: onboarded systems must carry the onboarding marker ──────────────

def _systems_snapshot(*, erp_status=None) -> dict:
    """``erp`` has onboarding data (a SchemaEntry); ``pending`` is a
    connection-only node an Edit before onboarding leaves (no edges, no
    marker); ``mes`` is onboarded and marked."""
    erp_props = {"id": "erp", "name": "ERP", **({"status": erp_status} if erp_status else {})}
    return {"format": gs.FORMAT_VERSION, "edges": [
        {"type": "HAS_SCHEMA", "from": 0, "to": 1},
        {"type": "HAS_SCHEMA", "from": 3, "to": 4},
    ], "nodes": [
        {"labels": ["System"], "props": erp_props},
        {"labels": ["SchemaEntry"], "props": {"id": "erp.orders"}},
        {"labels": ["System"], "props": {"id": "pending", "connection_type": "MCP Server"}},
        {"labels": ["System"], "props": {"id": "mes", "status": "onboarded"}},
        {"labels": ["SchemaEntry"], "props": {"id": "mes.runs"}},
    ]}


def test_import_refuses_onboarding_data_without_the_marker_before_wiping():
    target = FakeGraph()
    target.add_node(["Precious"], {"id": "keep-me"})
    with pytest.raises(ValueError) as err:
        gs.import_snapshot(_systems_snapshot(), query=target.query, write=target.write)
    message = str(err.value)
    assert "erp" in message and "status" in message, message
    assert "pending" not in message and "mes" not in message, message
    assert target.writes == [] and len(target.nodes) == 1


def test_guard_a_marked_snapshot_with_a_connection_only_node_imports():
    target = FakeGraph()
    result = gs.import_snapshot(_systems_snapshot(erp_status="onboarded"),
                                query=target.query, write=target.write)
    assert result["nodes_created"] == 5 and result["edges_created"] == 2


def test_guard_a_current_export_of_onboarded_systems_imports():
    """An export copies every property, so an onboarded System keeps its
    marker through export and import."""
    snapshot = gs.export_snapshot(query=_seed_graph().query, page_size=3)
    gs.validate_snapshot(snapshot)
    assert any(n["props"].get("status") == "onboarded" for n in snapshot["nodes"]
               if "System" in n["labels"])


# ── Round-trip: export → import → re-export is semantically equal ────────────

def _load_cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "graph_snapshot.py"
    spec = importlib.util.spec_from_file_location("adt_graph_snapshot_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_round_trip_export_import_reexport_semantically_equal():
    source = _seed_graph()
    first = gs.export_snapshot(query=source.query, page_size=3)
    target = FakeGraph()
    target.add_node(["Stale"], {"id": "wiped-away"})
    gs.import_snapshot(first, query=target.query, write=target.write,
                       wipe_batch=2, import_batch=2)
    second = gs.export_snapshot(query=target.query, page_size=4)
    cli = _load_cli()
    diff = cli.diff_snapshots(first, second)
    assert diff["identical"], json.dumps(diff, indent=1, default=str)
    assert second["node_count"] == first["node_count"]
    assert second["edge_count"] == first["edge_count"]


# ── Routes: admin gating, confirm token, background job ─────────────────────

class _AdminInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


def _build_app(is_admin: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(gsr.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin)
    return app


@pytest.fixture(autouse=True)
def _fresh_import_jobs(monkeypatch):
    """Fresh in-memory job registry per test (JobStore is dict-like)."""
    monkeypatch.setattr(gsr, "_IMPORT_JOBS", {})


_MINIMAL_SNAPSHOT = {"format": gs.FORMAT_VERSION,
                     "nodes": [{"labels": ["A"], "props": {"id": "x"}}],
                     "edges": []}


def test_routes_admin_gated():
    client = TestClient(_build_app(is_admin=False))
    assert client.get("/api/admin/graph/snapshot").status_code == 403
    assert client.post("/api/admin/graph/snapshot/import",
                       json={"confirm": "wipe-and-restore",
                             "snapshot": _MINIMAL_SNAPSHOT}).status_code == 403
    assert client.get("/api/admin/graph/snapshot/import/status").status_code == 403


def test_get_chunk_delegates_and_maps_bad_cursor_to_400(monkeypatch):
    seen = {}

    def fake_chunk(cursor):
        seen["cursor"] = cursor
        return {"format": gs.FORMAT_VERSION, "kind": "nodes", "items": [],
                "next_cursor": None}

    monkeypatch.setattr(gsr.graph_snapshot, "export_chunk", fake_chunk)
    client = TestClient(_build_app(is_admin=True))
    resp = client.get("/api/admin/graph/snapshot?cursor=nodes:500")
    assert resp.status_code == 200 and seen["cursor"] == "nodes:500"

    def bad_chunk(cursor):
        raise ValueError("malformed snapshot cursor")

    monkeypatch.setattr(gsr.graph_snapshot, "export_chunk", bad_chunk)
    assert client.get("/api/admin/graph/snapshot?cursor=zzz").status_code == 400


def test_import_requires_exact_confirm_token():
    client = TestClient(_build_app(is_admin=True))
    for body in ({}, {"confirm": ""}, {"confirm": "yes"},
                 {"confirm": "WIPE-AND-RESTORE"},
                 {"confirm": "wipe-and-restore "}):
        resp = client.post("/api/admin/graph/snapshot/import",
                           json={**body, "snapshot": _MINIMAL_SNAPSHOT})
        assert resp.status_code == 400, body
        assert "confirm" in resp.json()["detail"]


def test_import_refuses_invalid_snapshot_with_400():
    client = TestClient(_build_app(is_admin=True))
    resp = client.post("/api/admin/graph/snapshot/import",
                       json={"confirm": "wipe-and-restore",
                             "snapshot": {"format": "adt-graph-snapshot/9",
                                          "nodes": [], "edges": []}})
    assert resp.status_code == 400
    assert "unsupported snapshot format" in resp.json()["detail"]
    resp = client.post("/api/admin/graph/snapshot/import",
                       content=b"not-json",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


class _FakeRequest:
    """Direct-handler double: admin verdict + JSON body."""

    def __init__(self, body, is_admin=True):
        self._body = body
        self.state = SimpleNamespace(is_admin=is_admin)

    async def json(self):
        return self._body


async def _drive_import(monkeypatch, import_result=None, error=None):
    calls = []

    def fake_import(payload):
        calls.append(payload)
        if error:
            raise error
        return import_result or {"nodes_created": 1}

    monkeypatch.setattr(gsr.graph_snapshot, "import_snapshot", fake_import)
    request = _FakeRequest({"confirm": "wipe-and-restore",
                            "snapshot": _MINIMAL_SNAPSHOT})
    started = await gsr.start_snapshot_import(request)
    status = gsr.snapshot_import_status(_FakeRequest(None))
    deadline = time.monotonic() + 10.0
    while status["state"] == "running" and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        status = gsr.snapshot_import_status(_FakeRequest(None))
    return started, status, calls


def test_import_runs_as_background_job_and_reports_done(monkeypatch):
    started, status, calls = asyncio.run(
        _drive_import(monkeypatch, import_result={"nodes_created": 42}))
    assert started == {"status": "started"}
    assert status["state"] == "done"
    assert status["result"]["nodes_created"] == 42
    assert calls == [_MINIMAL_SNAPSHOT]


def test_import_error_surfaces_in_status(monkeypatch):
    started, status, _ = asyncio.run(
        _drive_import(monkeypatch, error=RuntimeError("graph wipe failed")))
    assert started == {"status": "started"}
    assert status["state"] == "error"
    assert status["error_category"] == "graph_query_failed"
    assert "Reference: " in status["error"] and "graph wipe failed" not in status["error"]


def test_second_import_while_running_is_rejected(monkeypatch):
    async def scenario():
        gsr._IMPORT_JOBS["global"] = {"state": "running"}
        calls = []
        monkeypatch.setattr(gsr.graph_snapshot, "import_snapshot",
                            lambda payload: calls.append(payload))
        request = _FakeRequest({"confirm": "wipe-and-restore",
                                "snapshot": _MINIMAL_SNAPSHOT})
        response = await gsr.start_snapshot_import(request)
        return response, calls

    response, calls = asyncio.run(scenario())
    assert response == {"status": "already-running"}
    assert calls == []


def test_status_none_when_no_job_recorded():
    assert gsr.snapshot_import_status(_FakeRequest(None)) == {"state": "none"}


# ── Reconcile (husk detection) + repair ──────────────────────────────────────


class _FakeManager:
    gateway_id = "gw-1"
    mode = "cloud"

    @staticmethod
    def _runtime_name(system_id):
        return f"adt_{system_id}".replace("-", "_")[:48]


class _FakeControl:
    """Two pages of BOTH listings to pin pagination (a live env exceeded one
    runtimes page and a READY system was misreported as a husk, 2026-08-06)."""

    def __init__(self, runtimes, target_names):
        self._runtimes = list(runtimes.items())
        self._targets = list(target_names)

    def list_agent_runtimes(self, **kwargs):
        items = self._runtimes[1:] if kwargs.get("nextToken") else self._runtimes[:1]
        page = {"agentRuntimes": [
            {"agentRuntimeName": n, "status": s} for n, s in items]}
        if not kwargs.get("nextToken") and len(self._runtimes) > 1:
            page["nextToken"] = "r2"
        return page

    def list_gateway_targets(self, **kwargs):
        if kwargs.get("nextToken"):
            return {"items": [{"name": n} for n in self._targets[1:]]}
        page = {"items": [{"name": n} for n in self._targets[:1]]}
        if len(self._targets) > 1:
            page["nextToken"] = "t2"
        return page


def _fake_query_onboarded(ids):
    def query(cypher, params=None):
        assert "status = 'onboarded'" in cypher
        return [{"id": i} for i in ids]
    return query


def test_reconcile_healthy_husk_and_missing_target():
    """Three shapes in one report: healthy, full husk (runtime+target gone),
    and runtime-READY-but-target-missing. Names via the single-source
    convention (hyphens folded) — sitewise-i3x maps to adt_sitewise_i3x."""
    control = _FakeControl(
        runtimes={"adt_erp": "READY", "adt_mes_cr": "READY"},
        target_names=["erp"],  # mes-cr target missing; sitewise-i3x all gone
    )
    report = gs.reconcile_systems(
        manager=_FakeManager(), control=control,
        query=_fake_query_onboarded(["erp", "mes-cr", "sitewise-i3x"]))
    by_id = {r["system_id"]: r for r in report}
    assert by_id["erp"] == {"system_id": "erp", "runtime_ok": True,
                            "target_ok": True, "healthy": True}
    assert by_id["mes-cr"]["runtime_ok"] and not by_id["mes-cr"]["target_ok"]
    assert not by_id["mes-cr"]["healthy"]
    assert by_id["sitewise-i3x"] == {
        "system_id": "sitewise-i3x", "runtime_ok": False,
        "target_ok": False, "healthy": False}


def test_reconcile_non_ready_runtime_is_unhealthy():
    control = _FakeControl(runtimes={"adt_erp": "CREATING"},
                           target_names=["erp"])
    report = gs.reconcile_systems(
        manager=_FakeManager(), control=control,
        query=_fake_query_onboarded(["erp"]))
    assert report[0]["runtime_ok"] is False and report[0]["healthy"] is False


def test_repair_runs_restore_only_when_unhealthy_exists():
    calls = []
    healthy = [{"system_id": "erp", "runtime_ok": True, "target_ok": True,
                "healthy": True}]
    out = gs.repair_systems(healthy, restore_fn=lambda: calls.append(1))
    assert out == {"repaired": [], "healthy": ["erp"]} and calls == []
    husk = healthy + [{"system_id": "sitewise-i3x", "runtime_ok": False,
                       "target_ok": False, "healthy": False}]
    out = gs.repair_systems(husk, restore_fn=lambda: calls.append(1))
    assert out["repaired"] == ["sitewise-i3x"] and calls == [1]


def test_repair_restore_failure_propagates():
    husk = [{"system_id": "x", "runtime_ok": False, "target_ok": False,
             "healthy": False}]

    def boom():
        raise RuntimeError("restore blew up")

    with pytest.raises(RuntimeError, match="restore blew up"):
        gs.repair_systems(husk, restore_fn=boom)


def _repair_store(monkeypatch):
    """A fresh memory-only repair store (claim_slot needs a real JobStore)."""
    from agents.job_store import JobStore
    store = JobStore("test-graph-snapshot-repair", table=None)
    monkeypatch.setattr(gsr, "_REPAIR_JOBS", store)
    return store


async def _until_repair_leaves_running(store, timeout=5.0):
    deadline = time.monotonic() + timeout
    while ((store.get("global") or {}).get("state") == "running"
           and time.monotonic() < deadline):
        await asyncio.sleep(0.01)
    return dict(store.get("global") or {})


def _record_releases(monkeypatch) -> list:
    """Wrap the real exact-owner release so a test can wait for the job's
    ``finally`` to have run (the run ids it released, in order)."""
    released: list = []
    real = gsr._release_repair_slot

    def recording(run_id):
        real(run_id)
        released.append(run_id)

    monkeypatch.setattr(gsr, "_release_repair_slot", recording)
    return released


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def test_reconcile_route_checks_off_the_loop_and_starts_repair_on_it(monkeypatch):
    """The husk check runs on the general offload lane; a requested repair job
    is handed back to the request's loop from that worker thread."""
    import threading
    from fastapi import HTTPException

    store = _repair_store(monkeypatch)
    report = [{"system_id": "erp", "healthy": False}]
    check_threads = []

    def fake_report():
        check_threads.append(threading.current_thread().name)
        return report

    async def fake_repair_job(*_args):
        store["global"] = {"state": "done", "result": {}}

    monkeypatch.setattr(gsr, "_reconcile_report", fake_report)
    monkeypatch.setattr(gsr, "_run_repair_job", fake_repair_job)

    async def scenario():
        check = await gsr.reconcile_route(_FakeRequest({}))
        started = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        return check, started, await _until_repair_leaves_running(store)

    check, started, job = asyncio.run(scenario())
    assert check == {"report": report}
    assert started == {"status": "started", "report": report}
    assert job["state"] == "done"
    assert check_threads and all(
        name.startswith("adt-offload-general") for name in check_threads), check_threads
    with pytest.raises(HTTPException) as denied:
        asyncio.run(gsr.reconcile_route(_FakeRequest({}, is_admin=False)))
    assert denied.value.status_code == 403


def test_concurrent_repair_requests_schedule_exactly_one_job(monkeypatch):
    """Two repair requests whose husk checks overlap in two worker threads:
    exactly one starts a job, the other answers already-running. On the event
    loop this check-then-start was atomic for free; off it, only the slot
    claim makes it so. Every read of the ``global`` status record is held
    until both requests have made it, which widens the check-then-write
    window to make the race deterministic."""
    import threading

    store = _repair_store(monkeypatch)
    report = [{"system_id": "erp", "healthy": False}]
    both_read = threading.Barrier(2, timeout=0.5)
    plain_get = store.get

    def held_get(job_id, default=None):
        value = plain_get(job_id, default)
        if job_id == gsr._REPAIR_JOB_KEY:
            try:
                both_read.wait()
            except threading.BrokenBarrierError:
                pass
        return value

    monkeypatch.setattr(store, "get", held_get)
    scheduled = []

    async def fake_repair_job(*args):
        scheduled.append(args)

    monkeypatch.setattr(gsr, "_reconcile_report", lambda: report)
    monkeypatch.setattr(gsr, "_run_repair_job", fake_repair_job)

    async def scenario():
        responses = await asyncio.gather(
            *(gsr.reconcile_route(_FakeRequest({"repair": True})) for _ in range(2)))
        await _until(lambda: scheduled)
        await asyncio.sleep(0.05)  # room for a second job, if one was scheduled
        return responses

    responses = asyncio.run(scenario())
    assert sorted(r["status"] for r in responses) == ["already-running", "started"], responses
    assert len(scheduled) == 1, scheduled


@pytest.mark.parametrize("outcome", ["done", "error"])
def test_repair_job_releases_its_slot_when_it_ends(monkeypatch, outcome):
    """While the real repair job runs a second request is refused; once it
    ends, done or error, the slot is free and a new repair starts."""
    import threading

    store = _repair_store(monkeypatch)
    monkeypatch.setattr(gsr, "_reconcile_report",
                        lambda: [{"system_id": "erp", "healthy": False}])
    release = threading.Event()

    def fake_repair(report, restore_fn):
        release.wait(5.0)
        if outcome == "error":
            raise RuntimeError("restore blew up")
        return {"repaired": ["erp"], "healthy": []}

    monkeypatch.setattr(gsr.graph_snapshot, "repair_systems", fake_repair)
    released = _record_releases(monkeypatch)

    async def scenario():
        first = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        during = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        release.set()
        job = await _until_repair_leaves_running(store)
        await _until(lambda: len(released) == 1)  # the job's finally ran
        after = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        await _until(lambda: len(released) == 2)
        return first, during, job, after

    first, during, job, after = asyncio.run(scenario())
    assert first["status"] == "started"
    assert during["status"] == "already-running"
    assert job["state"] == outcome
    assert after["status"] == "started"
    assert len(released) == 2 and released[0] != released[1], released
    slot = store.get(gsr._REPAIR_ACTIVE_KEY)
    assert slot["status"] == "idle" and slot["run_id"] == released[1], slot


def test_stale_repair_finishing_late_never_frees_a_newer_claim(monkeypatch):
    """Exact-owner release: a repair whose claim went stale and was taken over
    must not free the newer run's slot when it finally finishes."""
    import threading

    store = _repair_store(monkeypatch)
    monkeypatch.setattr(gsr, "_reconcile_report", lambda: [])
    release = threading.Event()

    def fake_repair(report, restore_fn):
        release.wait(5.0)
        return {"repaired": [], "healthy": []}

    monkeypatch.setattr(gsr.graph_snapshot, "repair_systems", fake_repair)
    released = _record_releases(monkeypatch)
    later = time.time() + gsr._REPAIR_STALE_SECONDS + 60

    async def scenario():
        first = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        assert store.claim_slot(
            gsr._REPAIR_ACTIVE_KEY,
            {"status": "running", "run_id": "newer", "started_at": later},
            stale_seconds=gsr._REPAIR_STALE_SECONDS, now=later)
        release.set()
        await _until(lambda: len(released) == 1)  # the stale job's finally ran
        refused = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        return first, refused

    first, refused = asyncio.run(scenario())
    assert first["status"] == "started"
    assert len(released) == 1, released
    assert store.get(gsr._REPAIR_ACTIVE_KEY)["run_id"] == "newer"
    assert refused["status"] == "already-running"


def test_late_first_repair_never_overwrites_the_takeover_runs_status(monkeypatch):
    """Run 1 goes stale (its claim is backdated past the bound), run 2 takes
    the slot over, then run 1 finishes late. ``global`` must keep showing run
    2: running while it runs, then run 2's own result. Only the slot's
    current owner writes the status record."""
    import threading

    store = _repair_store(monkeypatch)
    monkeypatch.setattr(gsr, "_reconcile_report", lambda: [])
    released = _record_releases(monkeypatch)
    gates = [threading.Event(), threading.Event()]
    calls = []

    def fake_repair(report, restore_fn):
        n = len(calls)
        calls.append(n)
        gates[n].wait(5.0)
        return {"run": n + 1}

    monkeypatch.setattr(gsr.graph_snapshot, "repair_systems", fake_repair)

    async def scenario():
        first = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        await _until(lambda: len(calls) == 1)
        claim = dict(store.get(gsr._REPAIR_ACTIVE_KEY))
        claim["started_at"] = time.time() - gsr._REPAIR_STALE_SECONDS - 60
        store._mem[gsr._REPAIR_ACTIVE_KEY] = claim  # run 1's claim went stale
        second = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        await _until(lambda: len(calls) == 2)
        gates[0].set()  # run 1 finishes late
        await _until(lambda: len(released) == 1)
        while_second_runs = dict(store.get("global"))
        gates[1].set()
        await _until(lambda: len(released) == 2)
        return first, second, while_second_runs, dict(store.get("global"))

    first, second, during, final = asyncio.run(scenario())
    assert first["status"] == "started" and second["status"] == "started"
    assert during["state"] == "running", during
    assert final["state"] == "done" and final["result"]["run"] == 2, final


def test_long_repair_heartbeat_keeps_its_claim(monkeypatch):
    """A repair that runs past the stale bound keeps the slot while it
    heartbeats: a request made after the bound still gets already-running
    instead of starting a second restore."""
    import threading

    store = _repair_store(monkeypatch)
    monkeypatch.setattr(gsr, "_reconcile_report", lambda: [])
    monkeypatch.setattr(gsr, "_REPAIR_STALE_SECONDS", 1)
    monkeypatch.setattr(gsr, "_REPAIR_HEARTBEAT_SECONDS", 0.1, raising=False)
    released = _record_releases(monkeypatch)
    finish = threading.Event()
    restores = []

    def fake_repair(report, restore_fn):
        restores.append(1)
        finish.wait(5.0)
        return {"repaired": [], "healthy": []}

    monkeypatch.setattr(gsr.graph_snapshot, "repair_systems", fake_repair)

    async def scenario():
        first = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        await asyncio.sleep(2.2)  # well past the 1 s stale bound
        later = await gsr.reconcile_route(_FakeRequest({"repair": True}))
        finish.set()
        await _until(lambda: len(released) >= 1)
        return first, later

    first, later = asyncio.run(scenario())
    assert first["status"] == "started"
    assert later["status"] == "already-running", later
    assert len(restores) == 1, restores
    assert store.get(gsr._REPAIR_ACTIVE_KEY)["status"] == "idle"