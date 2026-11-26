# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0081 vocabulary drift — offline unit tests.

A concept imported AFTER onboarding files PENDING ``concept_mapping``
proposals for the unbound columns of every onboarded system — computed via
``vocab_fit`` token/synonym matching, filed ONLY through
``self_learning.log_proposed_mapping``, never a MAPS_TO (or any graph) write.

Pins:
  * the pass files exactly the expected proposals and NOTHING else
    (``agents.db.neptune_write`` monkeypatched to raise — no write path);
  * bound columns are skipped (never re-litigated); generic bookkeeping
    columns are skipped; dotted struct columns are SCORED and filed as
    ``table.a.b`` (ADR-0076 D5 — the accept writer's Field id is a plain
    concatenation, so the split point is irrelevant);
  * ONLY the given concept ids are scored (the graph read is parameterized
    with exactly the input ids);
  * a ``log_proposed_mapping`` ``None`` return (gated/tombstoned) counts
    under ``skipped.tombstoned_or_gated``;
  * idempotent re-run relies on the STORE's subject-key supersede — the same
    ``(to_system, concept)`` subject key both runs, no dedupe state here;
  * assignment is ONE-TO-ONE across BOTH (system, concept) and
    (system, column) — the ``_repoint_maps_to`` accept writer makes the
    chosen column the SOLE authoritative edge for the pair AND its R5
    column-keyed cleanup makes the most recently applied edge the only
    live one for a column, so coexisting proposals on EITHER axis would
    make bulk accept destructive; runners-up and column conflicts are
    VISIBLY counted (ADR-0081 amendment 2026-09-25);
  * graph outage (``neptune_query`` ⇒ ``[]``) fails SAFE: zero proposals,
    never "everything unbound";
  * the ``_run_import`` hook isolates drift failure (import stays ok) and
    passes ``provenance="import:<slug>"``;
  * the manual steward route offloads (event-loop offload invariant) and
    validates its body; the module source contains no graph-write helper
    (the ``test_graph_maintenance_routes.py`` AST-scan pattern);
  * ``assess_vocabulary_fit`` reports ``pending_bindings`` READ-ONLY.

Fully offline: every impure seam monkeypatched. Placeholder data only
(``system-a`` / ``widget`` style — neutrality ratchet).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

import agents.db as db  # noqa: E402
import agents.schema_check as schema_check  # noqa: E402
import agents.schema_drift as schema_drift  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
import agents.vocabulary_drift as vd  # noqa: E402
import agents.vocabulary_routes as vr  # noqa: E402
from agents.proposal_key import canonical_keys  # noqa: E402

_AGENTS = _ADT_ROOT / "agents"

_BASELINE = {
    "widget_orders": {
        "columns": ["work_state_code", "widget_count", "created_at",
                    "oee.availability"],
        "column_types": {},
    },
}

_CONCEPT_ROWS = [
    {"id": "work_state", "name": "Work state", "description": ""},
    {"id": "widget_count", "name": "Widget count", "description": ""},
    {"id": "availability", "name": "Availability", "description": ""},
    {"id": "other_concept", "name": "Widget count", "description": ""},
    {"id": "another_state", "name": "Work state", "description": ""},
]


def _row(column: str, system_id: str = "system-a",
         table: str = "widget_orders") -> dict:
    return {"id": f"{system_id}.{table}.{column}", "name": column,
            "schema_id": f"{system_id}.{table}"}


@pytest.fixture
def no_graph_write(monkeypatch):
    """The pass must NEVER reach a graph write — raise if it tries."""
    def _forbidden(*a, **k):
        raise AssertionError("vocabulary_drift must NEVER write the graph")
    monkeypatch.setattr(db, "neptune_write", _forbidden)


@pytest.fixture
def graph(monkeypatch, no_graph_write):
    """Dispatch ``agents.db.neptune_query`` on the Cypher text; record calls."""
    state = {"unbound": [], "calls": []}

    def fake_query(cypher, parameters=None):
        state["calls"].append((cypher, parameters))
        if "Concept" in cypher and "$ids" in cypher:
            ids = (parameters or {}).get("ids") or []
            return [dict(r) for r in _CONCEPT_ROWS if r["id"] in ids]
        if "HAS_FIELD" in cypher:
            return [dict(r) for r in state["unbound"]]
        raise AssertionError(f"unexpected cypher: {cypher}")

    monkeypatch.setattr(db, "neptune_query", fake_query)
    return state


@pytest.fixture
def one_system(monkeypatch):
    monkeypatch.setattr(schema_drift, "onboarded_system_ids",
                        lambda: ["system-a"])
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: dict(_BASELINE) if sid == "system-a"
                        else None)


@pytest.fixture
def log_spy(monkeypatch):
    """Record every ``log_proposed_mapping`` call; return a mapping id."""
    calls: list[dict] = []

    def fake(from_system, from_field, from_value, to_system, to_field,
             to_value, concept, confidence, query_context, **kwargs):
        calls.append({
            "from_system": from_system, "from_field": from_field,
            "from_value": from_value, "to_system": to_system,
            "to_field": to_field, "to_value": to_value, "concept": concept,
            "confidence": confidence, "query_context": query_context,
            **kwargs})
        return f"m-{len(calls)}"

    monkeypatch.setattr(self_learning, "log_proposed_mapping", fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. Exact proposals and NOTHING else
# --------------------------------------------------------------------------- #
def test_files_exactly_the_expected_proposals_and_nothing_else(
        graph, one_system, log_spy):
    graph["unbound"] = [_row("work_state_code")]
    report = vd.propose_bindings(["work_state"], provenance="import:pack-a")

    assert len(log_spy) == 1
    call = log_spy[0]
    assert call["proposed_type"] == "concept_mapping"
    assert call["source"] == "vocabulary_drift"
    assert call["method"] == "vocab_fit"
    assert call["provenance"] == "import:pack-a"
    assert call["to_system"] == "system-a"
    assert call["to_field"] == "widget_orders.work_state_code"
    assert call["concept"] == "work_state"
    assert 0 < call["confidence"] <= 1
    assert (call["from_system"], call["from_field"], call["from_value"],
            call["to_value"]) == ("", "", "", "")
    assert report["systems_scanned"] == 1
    assert report["proposals_filed"] == 1
    assert report["columns_scored"] == 1
    assert set(report["skipped"]) == {"bound", "generic",
                                      "tombstoned_or_gated"}
    assert report["per_system"][0]["system_id"] == "system-a"
    assert report["per_system"][0]["status"] == "ok"


# --------------------------------------------------------------------------- #
# 2. Bound columns skipped
# --------------------------------------------------------------------------- #
def test_bound_columns_are_never_scored(graph, one_system, log_spy):
    # widget_count is in the baseline but NOT in the unbound result — bound.
    graph["unbound"] = [_row("work_state_code")]
    report = vd.propose_bindings(["work_state", "widget_count"],
                                 provenance="manual")

    assert all(c["to_field"] != "widget_orders.widget_count"
               for c in log_spy)
    # 4 baseline columns − 1 unbound ⇒ 3 counted bound.
    assert report["skipped"]["bound"] == 3
    assert report["proposals_filed"] == 1


# --------------------------------------------------------------------------- #
# 3. Generic columns skipped
# --------------------------------------------------------------------------- #
def test_generic_columns_are_skipped(graph, one_system, log_spy):
    graph["unbound"] = [_row("created_at")]
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert log_spy == []
    assert report["skipped"]["generic"] == 1
    assert report["columns_scored"] == 0
    assert report["proposals_filed"] == 0


# --------------------------------------------------------------------------- #
# 4. Only the given concept ids are scored
# --------------------------------------------------------------------------- #
def test_only_the_given_concept_ids_are_scored(graph, one_system, log_spy):
    # widget_count column would match "other_concept" (same name tokens) —
    # but other_concept is NOT in the input, so nothing may be filed for it.
    graph["unbound"] = [_row("widget_count")]
    vd.propose_bindings(["work_state"], provenance="manual")

    concept_reads = [(c, p) for c, p in graph["calls"] if "$ids" in c]
    assert len(concept_reads) == 1
    assert concept_reads[0][1] == {"ids": ["work_state"]}
    assert all(c["concept"] == "work_state" for c in log_spy)
    assert log_spy == []  # widget_count does not match work_state


# --------------------------------------------------------------------------- #
# 5. A gated/tombstoned filing (None return) counts under skipped
# --------------------------------------------------------------------------- #
def test_gated_filing_counts_as_tombstoned_or_gated(
        graph, one_system, monkeypatch):
    graph["unbound"] = [_row("work_state_code")]
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda *a, **k: None)
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert report["proposals_filed"] == 0
    assert report["skipped"]["tombstoned_or_gated"] == 1


# --------------------------------------------------------------------------- #
# 6. Idempotent re-run — the STORE owns supersede; no dedupe state here
# --------------------------------------------------------------------------- #
def test_rerun_produces_the_identical_subject_key_and_files_again(
        graph, one_system, log_spy):
    graph["unbound"] = [_row("work_state_code")]
    vd.propose_bindings(["work_state"], provenance="manual")
    first_run = list(log_spy)
    vd.propose_bindings(["work_state"], provenance="manual")

    assert len(log_spy) == 2 * len(first_run)  # second run files again
    keys = [canonical_keys("concept_mapping", to_system=c["to_system"],
                           to_field=c["to_field"],
                           concept=c["concept"]).subject_key
            for c in log_spy]
    assert keys[0] == keys[1]  # the store's supersede grain, both runs


# --------------------------------------------------------------------------- #
# 7. One-to-one across BOTH axes — best score, lexicographic tie-break,
#    runners-up and column conflicts VISIBLY counted (ADR-0081 amendment
#    2026-09-25): the _repoint_maps_to accept writer deletes every prior
#    MAPS_TO edge for the (system, concept) pair AND (R5 column-keyed
#    cleanup) every other edge leaving the chosen column, so coexisting
#    proposals on either axis would make accept-all silently destroy an
#    already-accepted edge — never filed, never silently dropped either.
# --------------------------------------------------------------------------- #
def test_one_winner_per_system_concept_with_visible_runner_up(
        graph, one_system, log_spy):
    # work_state (2 tokens, score 1.0) beats work_state_code (score 2/3).
    graph["unbound"] = [_row("work_state_code"), _row("work_state")]
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert len(log_spy) == 1
    assert log_spy[0]["to_field"] == "widget_orders.work_state"
    # The outscored candidate is REPORTED, not silently dropped.
    assert report["per_system"][0]["skipped"]["runner_up"] == 1
    # top-level skipped keeps EXACTLY the three spec-pinned keys.
    assert set(report["skipped"]) == {"bound", "generic",
                                      "tombstoned_or_gated"}


def test_two_concepts_never_compete_for_the_same_column(
        graph, one_system, log_spy):
    # widget_count and other_concept share the SAME name tokens and only ONE
    # unbound column matches. Two coexisting proposals would let accept-all
    # accept both while the R5 column-keyed cleanup deletes the first
    # accepted edge (row stays `accepted` — silent partial state). Exactly
    # ONE is filed; the losing concept is a VISIBLE column_conflict.
    graph["unbound"] = [_row("widget_count")]
    report = vd.propose_bindings(["widget_count", "other_concept"],
                                 provenance="manual")

    assert len(log_spy) == 1
    assert log_spy[0]["to_field"] == "widget_orders.widget_count"
    # Deterministic: equal score, lexicographically first concept id wins.
    assert log_spy[0]["concept"] == "other_concept"
    assert report["proposals_filed"] == 1
    assert report["per_system"][0]["skipped"]["column_conflict"] == 1
    assert report["per_system"][0]["skipped"]["runner_up"] == 0
    # top-level skipped keeps EXACTLY the three spec-pinned keys.
    assert set(report["skipped"]) == {"bound", "generic",
                                      "tombstoned_or_gated"}


def test_concept_outranked_on_one_column_still_wins_another(
        graph, one_system, log_spy):
    # Both concepts match both columns; the one-to-one assignment gives each
    # its own column (no starvation), and the outranked candidates count as
    # runner_up — NOT column_conflict, because both concepts got bound.
    graph["unbound"] = [_row("work_state"), _row("work_state_code")]
    report = vd.propose_bindings(["work_state", "another_state"],
                                 provenance="manual")

    filed = {(c["concept"], c["to_field"]) for c in log_spy}
    assert filed == {("another_state", "widget_orders.work_state"),
                     ("work_state", "widget_orders.work_state_code")}
    assert report["proposals_filed"] == 2
    assert report["per_system"][0]["skipped"]["runner_up"] == 2
    assert report["per_system"][0]["skipped"]["column_conflict"] == 0


def test_no_runner_up_counts_zero(graph, one_system, log_spy):
    graph["unbound"] = [_row("work_state_code")]
    report = vd.propose_bindings(["work_state"], provenance="manual")
    assert report["per_system"][0]["skipped"]["runner_up"] == 0
    assert report["per_system"][0]["skipped"]["column_conflict"] == 0


def test_tie_breaks_lexicographically(graph, one_system, log_spy):
    # Equal token counts ⇒ equal score; the lexicographically first field wins.
    graph["unbound"] = [_row("work_state_flag"), _row("work_state_code")]
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert len(log_spy) == 1
    assert log_spy[0]["to_field"] == "widget_orders.work_state_code"
    assert report["per_system"][0]["skipped"]["runner_up"] == 1


# --------------------------------------------------------------------------- #
# 8. Dotted struct columns are SCORED and filed (ADR-0076 D5)
# --------------------------------------------------------------------------- #
def test_dotted_columns_are_scored_and_filed(graph, one_system, log_spy):
    graph["unbound"] = [_row("oee.availability")]
    report = vd.propose_bindings(["availability"], provenance="manual")

    assert [c["to_field"] for c in log_spy] == ["widget_orders.oee.availability"]
    assert report["columns_scored"] == 1 and report["proposals_filed"] == 1
    assert "dotted" not in report["per_system"][0]["skipped"]
    # top-level skipped keeps EXACTLY the three spec-pinned keys
    assert set(report["skipped"]) == {"bound", "generic",
                                      "tombstoned_or_gated"}


# --------------------------------------------------------------------------- #
# 9. Graph outage fails SAFE — [] degrade means ZERO proposals
# --------------------------------------------------------------------------- #
def test_graph_outage_yields_zero_proposals(graph, one_system, log_spy):
    graph["unbound"] = []  # the neptune_query failure degrade
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert log_spy == []
    assert report["proposals_filed"] == 0
    assert report["columns_scored"] == 0


# --------------------------------------------------------------------------- #
# 10. No baseline — honest per-system skip
# --------------------------------------------------------------------------- #
def test_no_baseline_is_an_honest_skip(graph, monkeypatch, log_spy):
    monkeypatch.setattr(schema_drift, "onboarded_system_ids",
                        lambda: ["system-a"])
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert log_spy == []
    assert report["systems_scanned"] == 0
    assert report["per_system"] == [
        {"system_id": "system-a", "status": "skipped:no-baseline"}]


def test_per_system_failure_is_an_honest_skip_not_a_pass_kill(
        graph, monkeypatch, log_spy):
    monkeypatch.setattr(schema_drift, "onboarded_system_ids",
                        lambda: ["system-a", "system-b"])

    def _baseline(sid):
        if sid == "system-a":
            raise RuntimeError("boom")
        return dict(_BASELINE)

    monkeypatch.setattr(schema_check, "load_system_schema", _baseline)
    graph["unbound"] = [_row("work_state_code", system_id="system-b")]
    report = vd.propose_bindings(["work_state"], provenance="manual")

    assert report["per_system"][0] == {"system_id": "system-a",
                                       "status": "skipped:RuntimeError"}
    assert report["per_system"][1]["status"] == "ok"
    assert report["proposals_filed"] == 1


def test_empty_concept_ids_returns_zero_report_without_graph_read(
        graph, one_system, log_spy):
    report = vd.propose_bindings([], provenance="import:empty")
    assert graph["calls"] == []
    assert log_spy == []
    assert report["systems_scanned"] == 0
    assert report["proposals_filed"] == 0


# --------------------------------------------------------------------------- #
# 11. Import hook — failure isolation + provenance passthrough
# --------------------------------------------------------------------------- #
@pytest.fixture
def import_seams(monkeypatch):
    monkeypatch.setattr(vr, "_read_existing_ids", lambda ids: set())
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: None)


def test_import_hook_failure_never_fails_the_import(
        import_seams, monkeypatch):
    import agents.vocabulary_drift as drift_mod

    def _boom(*a, **k):
        raise RuntimeError("drift pass exploded")

    monkeypatch.setattr(drift_mod, "propose_bindings", _boom)
    result = vr._run_import("pack-a", [
        {"id": "work_state", "name": "Work state", "description": ""}])

    assert result["status"] == "ok"
    assert result["inserted"] == 1
    assert result["vocabulary_drift"] == {"error": "RuntimeError"}


def test_import_hook_passes_inserted_ids_and_import_provenance(
        import_seams, monkeypatch):
    import agents.vocabulary_drift as drift_mod
    calls: list[tuple] = []

    def spy(concept_ids, *, provenance):
        calls.append((list(concept_ids), provenance))
        return {"systems_scanned": 0, "columns_scored": 0,
                "proposals_filed": 0,
                "skipped": {"bound": 0, "generic": 0,
                            "tombstoned_or_gated": 0},
                "per_system": []}

    monkeypatch.setattr(drift_mod, "propose_bindings", spy)
    result = vr._run_import("pack-a", [
        {"id": "work_state", "name": "Work state", "description": ""}])

    assert calls == [(["work_state"], "import:pack-a")]
    assert result["vocabulary_drift"]["proposals_filed"] == 0
    assert result["inserted"] == 1


def test_import_hook_excludes_skipped_and_collision_ids(
        import_seams, monkeypatch):
    import agents.vocabulary_drift as drift_mod
    monkeypatch.setattr(vr, "_read_existing_ids",
                        lambda ids: {"work_state"})
    calls: list[list] = []
    monkeypatch.setattr(
        drift_mod, "propose_bindings",
        lambda ids, *, provenance: calls.append(list(ids)) or {"stub": True})
    vr._run_import("pack-a", [
        {"id": "work_state", "name": "Work state", "description": ""},
        {"id": "widget_count", "name": "Widget count", "description": ""}])

    assert calls == [["widget_count"]]  # the exact-skip id never rides along


# --------------------------------------------------------------------------- #
# 12. Manual steward route — gate shape, validation, offload
# --------------------------------------------------------------------------- #
class _AdminMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(vr.router)
    app.add_middleware(_AdminMiddleware)
    return TestClient(app)


def test_route_runs_the_pass_and_returns_its_report(monkeypatch):
    import agents.vocabulary_drift as drift_mod
    calls: list[tuple] = []

    def spy(concept_ids, *, provenance):
        calls.append((list(concept_ids), provenance))
        return {"systems_scanned": 1, "columns_scored": 2,
                "proposals_filed": 1,
                "skipped": {"bound": 0, "generic": 1,
                            "tombstoned_or_gated": 0},
                "per_system": []}

    monkeypatch.setattr(drift_mod, "propose_bindings", spy)
    resp = _client().post("/api/admin/vocabulary/drift/run",
                          json={"concept_ids": [" work_state "]})
    assert resp.status_code == 200
    assert resp.json()["proposals_filed"] == 1
    assert calls == [(["work_state"], "manual")]


@pytest.mark.parametrize("body", [
    {},
    {"concept_ids": []},
    {"concept_ids": "work_state"},
    {"concept_ids": [1, 2]},
    {"concept_ids": ["  "]},
    {"concept_ids": [f"c{i}" for i in range(501)]},
])
def test_route_rejects_bad_bodies_400(monkeypatch, body):
    import agents.vocabulary_drift as drift_mod

    def _forbidden(*a, **k):
        raise AssertionError("the pass must not run on a rejected body")

    monkeypatch.setattr(drift_mod, "propose_bindings", _forbidden)
    resp = _client().post("/api/admin/vocabulary/drift/run", json=body)
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_route_get_is_405():
    resp = _client().get("/api/admin/vocabulary/drift/run")
    assert resp.status_code == 405


def _async_defs(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)}


def _call_names(fn: ast.AsyncFunctionDef) -> list[str]:
    names = []
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                f = node.func
                names.append(f.id if isinstance(f, ast.Name)
                             else f.attr if isinstance(f, ast.Attribute)
                             else ast.dump(f))
    return names


def test_route_handler_only_calls_offload():
    """Event-loop offload invariant: the async handler's ONLY call is
    ``offload`` (agents/offload.py) — sync work never runs inline."""
    defs = _async_defs(_AGENTS / "vocabulary_routes.py")
    assert "run_vocabulary_drift_route" in defs
    calls = _call_names(defs["run_vocabulary_drift_route"])
    assert calls == ["offload"], calls


_GRAPH_WRITE_HELPERS = {
    "neptune_write", "_neptune_write", "_neptune_write_loud", "neptune_batch_write",
}


def test_module_source_contains_no_graph_write_helper():
    """The ADR-0081 'proposals only' contract, pinned structurally (the
    ``test_graph_maintenance_routes.py`` scan pattern extended to the new
    module): no graph-write helper name appears in ANY call or import."""
    tree = ast.parse((_AGENTS / "vocabulary_drift.py")
                     .read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = (f.id if isinstance(f, ast.Name)
                    else f.attr if isinstance(f, ast.Attribute) else "")
            if name in _GRAPH_WRITE_HELPERS:
                offenders.append(name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] in _GRAPH_WRITE_HELPERS:
                    offenders.append(alias.name)
    assert offenders == [], offenders


# --------------------------------------------------------------------------- #
# 13. assess_vocabulary_fit — read-only pending_bindings count (D6)
# --------------------------------------------------------------------------- #
_PENDING_ROWS = [
    {"proposed_type": "concept_mapping", "source": "vocabulary_drift",
     "to_system": "system-a"},
    {"proposed_type": "concept_mapping", "source": "vocabulary_drift",
     "to_system": "system-a"},
    {"proposed_type": "concept_mapping", "source": "vocabulary_drift",
     "to_system": "system-b"},          # wrong system
    {"proposed_type": "concept_mapping", "source": "agent",
     "to_system": "system-a"},          # wrong source
    {"proposed_type": "schema_drift", "source": "vocabulary_drift",
     "to_system": "system-a"},          # wrong type
]


def test_pending_binding_count_filters_exactly(monkeypatch, no_graph_write):
    import agents.onboard_agent as oa
    monkeypatch.setattr(self_learning, "_query_by_status",
                        lambda status, **kw: list(_PENDING_ROWS)
                        if status == "pending" else [])
    monkeypatch.setattr(
        oa, "neptune_write",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("read-only")), raising=False)
    assert oa._pending_binding_count("system-a") == 2
    assert oa._pending_binding_count("system-b") == 1
    assert oa._pending_binding_count("system-c") == 0


def test_pending_binding_count_degrades_to_zero(monkeypatch):
    import agents.onboard_agent as oa

    def _boom(status, **kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(self_learning, "_query_by_status", _boom)
    assert oa._pending_binding_count("system-a") == 0


def test_assess_vocabulary_fit_carries_pending_bindings(monkeypatch):
    import agents.onboard_agent as oa
    from agents.onboarding_session import SessionNotFoundError

    tables = [{"name": "widget_orders",
               "columns": [{"name": "work_state", "data_type": "text"}],
               "row_count": 1, "sample_values": {}}]

    class _FakeStore:
        def get_session(self, system_id):
            if system_id != "system-a":
                raise SessionNotFoundError(system_id)
            return {"system_id": "system-a", "status": "discovered",
                    "discovered_tables": tables}

    monkeypatch.setattr(oa, "_store", lambda: _FakeStore())
    monkeypatch.setattr(oa, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(self_learning, "_query_by_status",
                        lambda status, **kw: list(_PENDING_ROWS))
    result = oa.assess_vocabulary_fit("system-a")
    assert result["pending_bindings"] == 2
