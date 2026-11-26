# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Requeue lifecycle — POST /api/proposed-mappings/{id}/requeue (offline).

An ACCEPTED proposal moves back to ``pending`` for steward re-review, and the
graph artifact its accept created is honestly UNDONE first:

  - value_mapping/crosswalk/join_path/correlation → the applied node is
    DETACH-DELETEd (no reader soft-filters these labels);
  - identity_rule → the EXISTING soft-deactivation machinery (readers filter
    status='deactivated'); the re-accept path restores ``ir.status='active'``;
  - insight → accept wrote nothing → nothing to undo;
  - vocab/guidance types are non-invertible → ValueError → route 400.

The row keeps its history (method/discovered_at/accepted_by untouched);
``accepted_at``/``neptune_node_id``/``verified`` are cleared and
``review_reason`` states why the row is back in the queue.

Harness style mirrors ``test_self_learning_lifecycle.py`` (in-memory FakeTable
+ ``_neptune_write_loud`` spy on the exact module-global seams) and
``test_steward_gate.py`` (real server.app route object mounted on a bare app
with an is_admin-stamping middleware). No AWS, no Neptune, no network.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import agents.self_learning as self_learning
import agents.server as server


# --------------------------------------------------------------------------- #
# In-memory doubles (test_self_learning_lifecycle.py pattern)
# --------------------------------------------------------------------------- #
class FakeTable:
    """Dict-backed stand-in for the proposed-mappings table (PK/SK keyed)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def seed(self, item: dict) -> None:
        self._items[(item["mapping_id"], item["discovered_at"])] = dict(item)

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)

    def query(self, **kwargs) -> dict:
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if kwargs.get("IndexName") == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:  # main-table query by mapping_id
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **_kw) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_part, remove_part = UpdateExpression.strip(), None
        if " REMOVE " in set_part:
            set_part, remove_part = set_part.split(" REMOVE ", 1)
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                lhs, rhs = (s.strip() for s in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]
        if remove_part:
            for tok in remove_part.split(","):
                item.pop(names.get(tok.strip(), tok.strip()), None)


class NeptuneSpy:
    """Spy for the fail-loud ``_neptune_write_loud`` seam."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, cypher: str, parameters: dict | None = None):
        self.calls.append((cypher, parameters or {}))
        return True


def _accepted_row(mapping_id: str = "m-1", *, proposed_type: str = "value_mapping",
                  node_id: str = "subj-aaaaaaaaaaaaaaaa") -> dict:
    return {
        "mapping_id": mapping_id, "discovered_at": "2026-07-01T00:00:00Z",
        "status": "accepted", "proposed_type": proposed_type,
        "from_system": "sys-a", "from_field": "tbl.col", "from_value": "V1",
        "to_system": "sys-b", "to_field": "tbl2.col2", "to_value": "V2",
        "concept": "part", "method": "foreign_key_match",
        "accepted_at": "2026-07-02T00:00:00Z", "accepted_by": "steward@example.com",
        "neptune_node_id": node_id, "verified": "true",
        "verified_at": "2026-07-02T00:00:00Z",
    }


@pytest.fixture()
def fake(monkeypatch):
    table = FakeTable()
    spy = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write_loud", spy)
    return table, spy


# --------------------------------------------------------------------------- #
# Lifecycle: accepted → requeue → pending + graph undo + review_reason
# --------------------------------------------------------------------------- #
def test_requeue_accepted_value_mapping_deletes_node_and_flips_pending(fake):
    table, spy = fake
    table.seed(_accepted_row())

    result = self_learning.requeue_mapping("m-1", actor="steward@example.com")

    assert result["status"] == "requeued"
    assert result["graph_artifact"] == "removed"
    # Graph-removal called: the applied ValueMapping node is DETACH-DELETEd.
    assert len(spy.calls) == 1
    cypher, params = spy.calls[0]
    assert "MATCH (n:ValueMapping {id: $nid}) DETACH DELETE n" in cypher
    assert params["nid"] == "subj-aaaaaaaaaaaaaaaa"

    row = table.by_id("m-1")
    assert row["status"] == "pending"
    assert row["review_reason"] == ("requeued for steward review: accepted "
                                    "without target verification (pre-P4)")
    # Cleared: the accept stamps and the P4 verified mark.
    for gone in ("accepted_at", "neptune_node_id", "verified", "verified_at"):
        assert gone not in row
    # History untouched.
    assert row["method"] == "foreign_key_match"
    assert row["discovered_at"] == "2026-07-01T00:00:00Z"
    assert row["accepted_by"] == "steward@example.com"
    assert row["requeued_by"] == "steward@example.com"


def test_requeue_identity_rule_uses_soft_deactivation_not_delete(fake):
    table, spy = fake
    table.seed(_accepted_row(proposed_type="identity_rule",
                             node_id="ir-sys-a-0123456789abcdef"))

    result = self_learning.requeue_mapping("m-1", actor="steward@example.com")

    assert result["graph_artifact"] == "deactivated"
    cypher, params = spy.calls[0]
    assert "SET ir.status = 'deactivated'" in cypher
    assert "DETACH DELETE" not in cypher
    assert params["rid"] == "ir-sys-a-0123456789abcdef"
    assert params["actor"] == "steward@example.com"
    assert table.by_id("m-1")["status"] == "pending"


def test_reaccept_after_requeue_restores_active_status():
    # The identity-rule apply path must flip the soft-deactivated node back to
    # routing — otherwise requeue→re-accept leaves a dead rule.
    import inspect
    src = inspect.getsource(self_learning._apply_identity_rule)
    assert "ir.status = 'active'" in src


def test_requeue_insight_undoes_nothing(fake):
    table, spy = fake
    table.seed(_accepted_row(proposed_type="insight", node_id="insight-subj-x"))
    result = self_learning.requeue_mapping("m-1")
    assert result["graph_artifact"] == "none"
    assert spy.calls == []
    assert table.by_id("m-1")["status"] == "pending"


def test_requeue_join_path_deletes_joinpath_node(fake):
    table, spy = fake
    table.seed(_accepted_row(proposed_type="join_path",
                             node_id="jp-sys-a-0123456789abcdef"))
    result = self_learning.requeue_mapping("m-1")
    assert result["graph_artifact"] == "removed"
    assert "MATCH (n:JoinPath {id: $nid}) DETACH DELETE n" in spy.calls[0][0]


def test_requeue_non_invertible_type_raises_and_keeps_row_accepted(fake):
    table, spy = fake
    table.seed(_accepted_row(proposed_type="concept_vocab", node_id="concept-x"))
    with pytest.raises(ValueError, match="not supported for proposed_type=concept_vocab"):
        self_learning.requeue_mapping("m-1")
    assert spy.calls == []
    assert table.by_id("m-1")["status"] == "accepted"  # nothing mutated


def test_requeue_not_found_and_not_accepted(fake):
    table, spy = fake
    assert "not found" in self_learning.requeue_mapping("nope")["error"]

    pending = _accepted_row("m-2")
    pending["status"] = "pending"
    table.seed(pending)
    err = self_learning.requeue_mapping("m-2")["error"]
    assert "not accepted" in err and "status=pending" in err
    assert spy.calls == []  # the graph is never touched on either error


# --------------------------------------------------------------------------- #
# Route: steward-gated, 404 unknown, 400 non-accepted / non-invertible
# --------------------------------------------------------------------------- #
def _client(is_admin: "bool | None" = True) -> TestClient:
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    for r in server.app.routes:
        if isinstance(r, APIRoute) and r.path == "/api/proposed-mappings/{mapping_id}/requeue":
            app.router.routes.append(r)
            return TestClient(app)
    raise AssertionError("requeue route not registered on server.app")


def test_route_200_maps_lifecycle_result(monkeypatch):
    seen = {}

    def fake_requeue(mapping_id, actor=""):
        seen["args"] = (mapping_id, actor)
        return {"status": "requeued", "mapping_id": mapping_id,
                "graph_artifact": "removed",
                "review_reason": self_learning._REQUEUE_REVIEW_REASON}

    monkeypatch.setattr(self_learning, "requeue_mapping", fake_requeue)
    resp = _client().post("/api/proposed-mappings/m-1/requeue")
    assert resp.status_code == 200
    assert resp.json()["status"] == "requeued"
    assert seen["args"][0] == "m-1"


def test_route_404_unknown_and_400_non_accepted(monkeypatch):
    monkeypatch.setattr(self_learning, "requeue_mapping",
                        lambda mid, actor="": {"error": f"Mapping {mid} not found"})
    assert _client().post("/api/proposed-mappings/zz/requeue").status_code == 404

    monkeypatch.setattr(
        self_learning, "requeue_mapping",
        lambda mid, actor="": {"error": f"Mapping {mid} is not accepted "
                               "(status=pending) — only accepted proposals "
                               "can be requeued"})
    resp = _client().post("/api/proposed-mappings/m-1/requeue")
    assert resp.status_code == 400
    assert "not accepted" in resp.json()["error"]


def test_route_400_on_non_invertible_value_error(monkeypatch):
    def boom(mid, actor=""):
        raise ValueError("requeue is not supported for proposed_type=concept_vocab"
                         " — accepting it mutated shared graph state that cannot"
                         " be automatically undone")

    monkeypatch.setattr(self_learning, "requeue_mapping", boom)
    resp = _client().post("/api/proposed-mappings/m-1/requeue")
    assert resp.status_code == 400
    assert "not supported" in resp.json()["error"]


def test_route_is_steward_gated(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("requeue_mapping must not run behind a failed gate")

    monkeypatch.setattr(self_learning, "requeue_mapping", boom)
    resp = _client(is_admin=False).post("/api/proposed-mappings/m-1/requeue")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Steward (admin) role required"
    # Absent verdict fails closed too.
    resp = _client(is_admin=None).post("/api/proposed-mappings/m-1/requeue")
    assert resp.status_code == 403
