# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Graph maintenance routes are steward-only POSTs — plus a repo-wide
"no GET handler writes the graph" tripwire.

``/api/neptune/graph/repair`` (MERGE edges, DETACH DELETE orphans) and
``/api/neptune/graph/backfill-concepts`` (create Concept nodes) used to be
ungated ``@app.get`` routes: any authenticated session (or a browser prefetch
carrying its cookie) could delete graph nodes. They are now:

* ``POST`` only (GET ⇒ 405),
* gated at registration with ``Depends(_check_steward)`` — FAILS CLOSED
  (absent ``is_admin`` verdict ⇒ 403, exactly like an explicit non-admin),
* offloaded (the sync Neptune body runs through ``offload_resolver``),
* honest about failure (a swallowed graph error ⇒ 500, not a 200 with an
  ``error`` key).

The AST tripwire at the bottom scans EVERY ``get``/``head`` handler in
``agents/`` for graph-write helpers so the class of bug cannot return.

Harness mirrors ``test_steward_gate.py`` (real ``server.app`` route objects
mounted on a bare app; a tiny middleware stamps ``is_admin``). Offline: the
Neptune helpers are monkeypatched; no AWS, no network.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import agents.db as db
import agents.server as server

_AGENTS = Path(__file__).resolve().parents[2] / "agents"

ROUTES = [
    "/api/neptune/graph/repair",
    "/api/neptune/graph/backfill-concepts",
    "/api/neptune/graph/repair-schema",   # ADR-0077 D2
]


def _real_route(path: str, method: str) -> APIRoute:
    for r in server.app.routes:
        if isinstance(r, APIRoute) and r.path == path and method in r.methods:
            return r
    raise AssertionError(f"route not registered on server.app: {method} {path}")


def _client(is_admin: "bool | None" = None) -> TestClient:
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    for path in ROUTES:
        app.router.routes.append(_real_route(path, "POST"))
    return TestClient(app)


@pytest.fixture
def graph_spy(monkeypatch):
    """Record every Neptune write; reads return empty result sets."""
    writes: list[str] = []
    monkeypatch.setattr(db, "neptune_query", lambda cypher, parameters=None: [])
    monkeypatch.setattr(db, "neptune_write",
                       lambda cypher, parameters=None: writes.append(cypher))
    # backfill seeds core concepts through catalog.core_concept_merges
    import agents.catalog as catalog
    monkeypatch.setattr(catalog, "core_concept_merges",
                        lambda: [("MERGE (c:Concept {id: $id})", {"id": "core-1"})])
    return writes


# --------------------------------------------------------------------------- #
# Method + gate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ROUTES)
def test_routes_are_post_only(path):
    """No GET registration exists for either path anywhere on server.app."""
    methods = {m for r in server.app.routes
               if isinstance(r, APIRoute) and r.path == path for m in r.methods}
    assert methods == {"POST"}, methods


@pytest.mark.parametrize("path", ROUTES)
def test_get_is_405(path, graph_spy):
    resp = _client(is_admin=True).get(path)
    assert resp.status_code == 405
    assert graph_spy == []


@pytest.mark.parametrize("is_admin", [False, None], ids=["non-admin", "absent"])
@pytest.mark.parametrize("path", ROUTES)
def test_non_admin_is_403_and_writes_nothing(path, is_admin, graph_spy):
    resp = _client(is_admin=is_admin).post(path)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Steward (admin) role required"
    assert graph_spy == [], "the graph write seam must never be reached"


def test_admin_repair_runs_and_reports_counts(graph_spy):
    resp = _client(is_admin=True).post("/api/neptune/graph/repair")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"repairs", "deletions", "repair_detail", "deletion_detail",
                         "value_mappings", "dry_run"}
    assert body["repairs"] == 0 and body["deletions"] == 0  # empty graph
    assert body["dry_run"] is False
    vm = body["value_mappings"]
    assert (vm["malformed_learned_retired"], vm["duplicate_generated_retired"],
            vm["kept"]) == (0, 0, 0)
    # every read answers [] here, like a failing neptune_query: the count
    # control makes that a FAILED scan, not a clean graph
    assert vm["scan_failed"] is True and vm["verified"] is False


def _hygiene_graph(monkeypatch):
    """A graph holding one replayed guidance note, one legacy duplicate of a
    current-key equipment row, and one steward row with the same identity."""
    from agents.onboard import _equipment_vm_id
    current = {"from_system": "reg", "from_value": "P-1", "to_system": "hist",
               "to_value": "Pump 1", "concept": "equipment"}
    current["id"] = _equipment_vm_id(current)
    rows = [current, {**current, "id": "vm-equip-P-1-0"},
            {**current, "id": "subj-0123456789abcdef"},
            {"id": "guidance-subj-1", "from_system": "reg", "from_value": "",
             "to_system": "", "to_value": "A sentence of guidance.", "concept": ""}]
    present = {r["id"] for r in rows}
    writes: list = []

    def _query(cypher, parameters=None):
        if cypher == "MATCH (vm:ValueMapping) RETURN count(vm) AS n":
            return [{"n": len(rows)}]
        if cypher.startswith("MATCH (vm:ValueMapping) RETURN vm.id AS id, "):
            return rows
        if cypher.startswith("UNWIND $ids AS i OPTIONAL MATCH (vm:ValueMapping"):
            return [{"id": i, "found": i if i in present else None} for i in parameters["ids"]]
        if "MATCH (se:SchemaEntry) RETURN" in cypher:
            return [{"nid": "se-1", "sid": "erp"}]
        return []

    def _write(cypher, parameters=None):
        writes.append((cypher, parameters))
        if "DETACH DELETE vm" in cypher:
            present.discard(parameters["vmid"])
        return True
    monkeypatch.setattr(db, "neptune_query", _query)
    monkeypatch.setattr(db, "neptune_write", _write)
    return current["id"], writes


def test_admin_repair_retires_bad_value_mappings(monkeypatch):
    kept_id, writes = _hygiene_graph(monkeypatch)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair")
    assert resp.status_code == 200
    vm = resp.json()["value_mappings"]
    assert vm["malformed_learned_retired"] == 1
    assert vm["duplicate_generated_retired"] == 1
    assert vm["kept"] == 1 and vm["kept_ids"] == [kept_id]
    assert vm["readback"]["verified"] is True
    deleted = [p["vmid"] for c, p in writes if "DETACH DELETE vm" in c]
    assert deleted == ["guidance-subj-1", "vm-equip-P-1-0"]
    assert "subj-0123456789abcdef" not in deleted


def test_admin_repair_dry_run_plans_and_writes_nothing(monkeypatch):
    kept_id, writes = _hygiene_graph(monkeypatch)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair",
                                       json={"dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert writes == [], "dry_run must never reach the write seam"
    vm = body["value_mappings"]
    assert vm["dry_run"] is True
    assert vm["malformed_learned_ids"] == ["guidance-subj-1"]
    assert vm["duplicate_generated_ids"] == ["vm-equip-P-1-0"]
    assert vm["kept_ids"] == [kept_id] and "readback" not in vm
    # the other repair steps still REPORT what they would do — through a
    # no-op writer, so nothing above reached the graph
    assert body["repair_detail"] == ["System(erp) --HAS_SCHEMA--> SchemaEntry(se-1)"]
    note = body["dry_run_note"]
    assert "only value_mappings is exact" in note
    assert "repairs" in note and "MERGE" in note and "orphans" in note and "over-report" in note


def test_applied_repair_carries_no_dry_run_note(monkeypatch):
    _hygiene_graph(monkeypatch)
    body = _client(is_admin=True).post("/api/neptune/graph/repair").json()
    assert "dry_run_note" not in body


def test_admin_repair_dry_run_false_body_applies(monkeypatch):
    _kept, writes = _hygiene_graph(monkeypatch)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair",
                                       json={"dry_run": False})
    assert resp.status_code == 200 and resp.json()["dry_run"] is False
    assert any("DETACH DELETE vm" in c for c, _p in writes)


def test_admin_repair_dry_run_must_be_a_json_boolean(graph_spy):
    resp = _client(is_admin=True).post("/api/neptune/graph/repair",
                                       json={"dry_run": "true"})
    assert resp.status_code == 422
    assert resp.json() == {"error": "graph repair rejected: 'dry_run' must be a JSON boolean"}
    assert graph_spy == []


def test_non_admin_dry_run_is_403(graph_spy):
    resp = _client(is_admin=False).post("/api/neptune/graph/repair",
                                        json={"dry_run": True})
    assert resp.status_code == 403 and graph_spy == []


def test_admin_repair_deletes_true_orphans_and_recreates_edges(monkeypatch, graph_spy):
    """Behavioural pin of the hoisted sync body: an orphan Field is DETACH
    DELETEd, a SchemaEntry naming a system gets its System + HAS_SCHEMA edge."""
    def _fake_query(cypher, parameters=None):
        if "MATCH (n:Field) WHERE NOT ()-[:HAS_FIELD]->(n)" in cypher:
            return [{"nid": "field-orphan"}]
        if "MATCH (se:SchemaEntry) RETURN" in cypher:
            return [{"nid": "se-1", "sid": "erp"}]
        return []
    monkeypatch.setattr(db, "neptune_query", _fake_query)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deletions"] == 1
    assert body["deletion_detail"] == ["Deleted orphan Field(field-orphan)"]
    assert body["repairs"] == 1
    assert body["repair_detail"] == ["System(erp) --HAS_SCHEMA--> SchemaEntry(se-1)"]
    assert any("DETACH DELETE" in w for w in graph_spy)
    assert any("MERGE (s)-[:HAS_SCHEMA]->(n)" in w for w in graph_spy)


def test_admin_backfill_runs_and_writes_core_concepts(graph_spy):
    resp = _client(is_admin=True).post("/api/neptune/graph/backfill-concepts")
    assert resp.status_code == 200
    assert resp.json() == {"concepts_created": 1, "detail": ["core:core-1"]}
    assert graph_spy == ["MERGE (c:Concept {id: $id})"]


def test_graph_error_is_500_not_200(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("neptune down")
    monkeypatch.setattr(db, "neptune_query", _boom)
    monkeypatch.setattr(db, "neptune_write", _boom)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_category"] == "graph_query_failed"
    assert body["error"].endswith(f"Reference: {body['error_ref']}")
    assert "neptune down" not in body["error"]


# --------------------------------------------------------------------------- #
# Offload: the async handlers contain no direct graph call
# --------------------------------------------------------------------------- #

def _handler_calls(name: str) -> list[str]:
    tree = ast.parse((_AGENTS / "server.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    names = []
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                f = node.func
                names.append(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return names


@pytest.mark.parametrize("handler", ["neptune_repair_graph", "neptune_backfill_concepts",
                                     "neptune_repair_schema"])
def test_handlers_only_offload(handler):
    calls = set(_handler_calls(handler))
    assert calls & {"offload_resolver", "offload"}
    assert not calls & {"neptune_query", "neptune_write", "_nq", "_nw",
                        "repair_schema", "run_drift_check", "accept_mapping"}, calls


# --------------------------------------------------------------------------- #
# ADR-0077 D2: POST /api/neptune/graph/repair-schema
# --------------------------------------------------------------------------- #

def test_admin_repair_schema_reports_counts(monkeypatch, graph_spy):
    import agents.schema_repair as schema_repair
    calls: list = []
    report = {"system_id": "erp", "detection": None, "candidates": 2, "materialised": 2,
              "failed": [], "failed_count": 0, "remaining": 0, "pending_unreviewed": 1}

    def fake_repair(system_id, actor="", detect=True):
        calls.append((system_id, actor, detect))
        return dict(report)
    monkeypatch.setattr(schema_repair, "repair_schema", fake_repair)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair-schema",
                                       json={"system_id": "erp"})
    assert resp.status_code == 200
    assert resp.json() == report
    assert calls == [("erp", "", True)]


def test_repair_schema_forwards_detect_false(monkeypatch, graph_spy):
    import agents.schema_repair as schema_repair
    calls: list = []
    monkeypatch.setattr(schema_repair, "repair_schema",
                        lambda system_id, actor="", detect=True:
                        calls.append(detect) or {"system_id": system_id})
    resp = _client(is_admin=True).post("/api/neptune/graph/repair-schema",
                                       json={"system_id": "erp", "detect": False})
    assert resp.status_code == 200 and calls == [False]


def test_repair_schema_missing_system_is_422(graph_spy):
    resp = _client(is_admin=True).post("/api/neptune/graph/repair-schema", json={})
    assert resp.status_code == 422
    assert resp.json()["error"].startswith("schema repair rejected: ")
    assert "http_status" not in resp.json()
    assert graph_spy == []


def test_repair_schema_failure_is_500(monkeypatch, graph_spy):
    import agents.schema_repair as schema_repair

    def boom(*_a, **_k):
        raise RuntimeError("neptune down at host-x")
    monkeypatch.setattr(schema_repair, "repair_schema", boom)
    resp = _client(is_admin=True).post("/api/neptune/graph/repair-schema",
                                       json={"system_id": "erp"})
    assert resp.status_code == 500
    assert resp.json() == {"error": "schema repair failed: RuntimeError"}


# --------------------------------------------------------------------------- #
# Repo-wide tripwire: a GET/HEAD handler never writes the graph
# --------------------------------------------------------------------------- #

_GRAPH_WRITE = re.compile(
    r"\b(neptune_write|_nw)\s*\(|DETACH DELETE|\bDELETE\s+\w+\s*$|\bCREATE\s*\(|\bMERGE\s*\("
)


def _get_head_handlers():
    for path in sorted(_AGENTS.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ("get", "head")):
                    body = "\n".join(lines[node.lineno - 1:node.end_lineno])
                    yield path.relative_to(_AGENTS), node.name, body


def test_no_get_or_head_handler_writes_the_graph():
    offenders = [f"{rel}:{name}" for rel, name, body in _get_head_handlers()
                 if _GRAPH_WRITE.search(body)]
    assert offenders == [], (
        "GET/HEAD handlers must be read-only — move the write to a steward-"
        f"gated POST: {offenders}"
    )


def test_repair_schema_detect_must_be_a_json_boolean(monkeypatch):
    """'detect' is type-validated, never truthiness-coerced ("false" is not False)."""
    from agents import server, schema_repair
    called: list = []
    monkeypatch.setattr(schema_repair, "repair_schema",
                        lambda *a, **k: called.append(k) or {"ok": True})
    out = server._repair_schema_sync({"system_id": "erp", "detect": "false"})
    assert out.get("http_status") == 422 and called == []
    out = server._repair_schema_sync({"system_id": "erp", "detect": False})
    assert called and called[0]["detect"] is False
