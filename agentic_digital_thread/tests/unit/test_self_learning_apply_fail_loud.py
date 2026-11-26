# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail-loud graph apply for accepted proposals (offline).

Live incident (2026-07-14): the analyzer emitted a ``field_annotation`` whose
``from_system`` was a PLANT name (not a graph ``System`` id) with a hallucinated
table. ACCEPTING it flipped the DynamoDB ledger to ``accepted`` while the
graph-side ``MATCH (s:System {id: …})… SET …`` matched ZERO rows — a SILENT
no-op: the user believed a rule was learned, the graph was unchanged.

Contract pinned here (``self_learning._apply_to_neptune`` + the count-probe
helpers ``_graph_rows_matched`` / ``_require_graph_systems``):

  * ``field_annotation`` accept probes the exact target MATCH first; a
    DEFINITIVE zero-row match raises ``ValueError`` (actionable), the ledger
    stays ``pending``, and NO disambiguation write is attempted;
  * ``value_mapping`` / ``crosswalk`` / ``join_path`` accepts pre-check both
    endpoint ``System`` nodes the same way BEFORE any node MERGE (no partial
    graph writes);
  * an INDETERMINATE probe (the read seam yields ``[]`` — backend
    unreachable / the offline lane) PROCEEDS: never block on uncertainty, and
    a real outage still fails loud at ``_neptune_write_loud``;
  * a matched probe (n ≥ 1) proceeds and the accept completes as before;
  * the accept ROUTE surfaces the ``ValueError`` as a 400 ``{"error": …}``
    (the UI's accept path renders ``result.error``), and the ledger item is
    still ``pending`` afterwards.

No AWS / Neptune / LLM / network — in-memory ``FakeTable`` (lifecycle-test
conventions) + monkeypatched ``_neptune_query`` / ``_neptune_write`` seams.
All system/table literals are synthetic.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)



class FakeTable:
    """Minimal DynamoDB-shaped store (lifecycle-test conventions) whose
    ``update_item`` actually applies SET clauses so ledger status is assertable."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def by_id(self, mapping_id: str) -> dict:
        for (mid, _), item in self._items.items():
            if mid == mapping_id:
                return item
        raise KeyError(mapping_id)

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **_kw) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        expr = UpdateExpression.strip()
        set_part, remove_part = expr, None
        if " REMOVE " in expr:
            set_part, remove_part = expr.split(" REMOVE ", 1)
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                lhs, rhs = (s.strip() for s in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]
        if remove_part:
            for tok in remove_part.split(","):
                item.pop(names.get(tok.strip(), tok.strip()), None)


class WriteSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, cypher: str, parameters: dict | None = None) -> bool:
        self.calls.append((cypher, parameters))
        return True


@pytest.fixture
def fake_table(monkeypatch) -> FakeTable:
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


@pytest.fixture
def write_spy(monkeypatch) -> WriteSpy:
    spy = WriteSpy()
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    # The one-statement ValueMapping / JoinPath / Correlation write returns
    # the id it wrote; route it through the same spy (a refused write answers None).
    route_returning_writes(monkeypatch, spy)
    return spy


def _count_probe(n: "int | None"):
    """A ``_neptune_query`` stub answering every count probe with ``n``
    (``None`` → the seam's empty/indeterminate ``[]``)."""
    def _q(cypher: str, parameters: dict | None = None) -> list:
        if n is None:
            return []
        return [{"n": n}]
    return _q


def _log_annotation() -> str:
    """A stored, non-degenerate field_annotation (synthetic literals)."""
    return self_learning.log_proposed_mapping(
        from_system="hist-a", from_field="ghost_table.status", from_value="",
        to_system="hist-a", to_field="ghost_table.status", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: Enum values OPEN/CLOSED — a type gotcha",
        proposed_type="field_annotation",
    )


def _log(proposed_type: str) -> str:
    """A stored join-like proposal of the given type (synthetic literals)."""
    return self_learning.log_proposed_mapping(
        from_system="erp-a", from_field="orders.line", from_value="A",
        to_system="cmms-b", to_field="events.line_code", to_value="B",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type=proposed_type,
    )


# ===========================================================================
# field_annotation: zero-row target → ValueError, ledger pending, no write
# ===========================================================================
def test_field_annotation_zero_row_apply_raises_and_ledger_stays_pending(
        fake_table, write_spy, monkeypatch):
    mid = _log_annotation()
    assert mid is not None
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(0))

    with pytest.raises(ValueError, match="does not exist in the knowledge graph"):
        self_learning.accept_mapping(mid)

    assert fake_table.by_id(mid)["status"] == "pending"  # NOT flipped
    # No disambiguation SET was attempted after the definitive zero-row probe.
    assert not any("disambiguation" in c for c, _p in write_spy.calls)


def test_field_annotation_matched_target_applies_and_accepts(
        fake_table, write_spy, monkeypatch):
    mid = _log_annotation()
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(1))

    result = self_learning.accept_mapping(mid)

    assert result["status"] == "accepted"
    assert fake_table.by_id(mid)["status"] == "accepted"
    assert any("disambiguation" in c for c, _p in write_spy.calls)


def test_field_annotation_indeterminate_probe_proceeds(
        fake_table, write_spy, monkeypatch):
    # The read seam swallows backend errors into [] — indistinguishable from
    # "unreachable". Never block on uncertainty: proceed (a real outage fails
    # loud at the write itself). This is also the offline lane's historical path.
    mid = _log_annotation()
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(None))

    result = self_learning.accept_mapping(mid)

    assert result["status"] == "accepted"
    assert any("disambiguation" in c for c, _p in write_spy.calls)


# ===========================================================================
# value_mapping / crosswalk / join_path: unknown System → ValueError,
# ledger pending, NO node MERGE (pre-check runs before any write)
# ===========================================================================
@pytest.mark.parametrize("ptype,node_token", [
    ("value_mapping", "ValueMapping"),
    ("crosswalk", "ValueMapping"),
    ("join_path", "JoinPath"),
])
def test_join_like_zero_row_system_raises_before_any_write(
        fake_table, write_spy, monkeypatch, ptype, node_token):
    mid = _log(ptype)
    assert mid is not None
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(0))

    with pytest.raises(ValueError, match="does not exist in the knowledge graph"):
        self_learning.accept_mapping(mid)

    assert fake_table.by_id(mid)["status"] == "pending"
    # Pre-check fired BEFORE the rule-node MERGE — no partial graph state.
    assert not any(node_token in c for c, _p in write_spy.calls)


@pytest.mark.parametrize("ptype", ["value_mapping", "crosswalk", "join_path"])
def test_join_like_matched_systems_accepts(fake_table, write_spy, monkeypatch, ptype):
    mid = _log(ptype)
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(1))

    result = self_learning.accept_mapping(mid)

    assert result["status"] == "accepted"
    assert fake_table.by_id(mid)["status"] == "accepted"


# ===========================================================================
# value_mapping / crosswalk: ONE statement writes the node and both edges and
# returns its id; the ledger flips to accepted only after that id came back
# ===========================================================================
@pytest.mark.parametrize("ptype", ["value_mapping", "crosswalk"])
def test_a_refused_mapping_write_keeps_the_ledger_pending(
        fake_table, write_spy, monkeypatch, ptype):
    mid = _log(ptype)
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(1))
    monkeypatch.setattr(self_learning, "_neptune_write_returning",
                        lambda c, p=None: None, raising=False)

    with pytest.raises(RuntimeError, match="ValueMapping"):
        self_learning.accept_mapping(mid)

    assert fake_table.by_id(mid)["status"] == "pending"
    assert not any("ValueMapping" in c for c, _p in write_spy.calls)


@pytest.mark.parametrize("ptype", ["value_mapping", "crosswalk"])
def test_a_mapping_write_that_matched_no_system_pair_keeps_the_ledger_pending(
        fake_table, write_spy, monkeypatch, ptype):
    """The pre-check probe was indeterminate or a System went away before the
    write: the statement matched no System pair, wrote nothing and returned
    no row. The accept fails with the actionable message, ledger pending."""
    mid = _log(ptype)
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(None))
    monkeypatch.setattr(self_learning, "_neptune_write_returning",
                        lambda c, p=None: [], raising=False)

    with pytest.raises(ValueError, match="does not exist in the knowledge graph"):
        self_learning.accept_mapping(mid)

    assert fake_table.by_id(mid)["status"] == "pending"


# ===========================================================================
# _graph_rows_matched / _require_graph_systems primitives
# ===========================================================================
def test_graph_rows_matched_semantics(monkeypatch):
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(3))
    assert self_learning._graph_rows_matched("RETURN 1 AS n", {}) == 3
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(0))
    assert self_learning._graph_rows_matched("RETURN 1 AS n", {}) == 0
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(None))
    assert self_learning._graph_rows_matched("RETURN 1 AS n", {}) is None


def test_require_graph_systems_names_the_missing_system(monkeypatch):
    def _q(cypher: str, parameters: dict | None = None) -> list:
        return [{"n": 1 if (parameters or {}).get("sid") == "erp-a" else 0}]
    monkeypatch.setattr(self_learning, "_neptune_query", _q)

    self_learning._require_graph_systems("erp-a")  # exists → no raise
    with pytest.raises(ValueError, match="'plant-x'"):
        self_learning._require_graph_systems("erp-a", "plant-x")


# ===========================================================================
# Accept ROUTE: the apply ValueError surfaces as a 400 {"error": …} and the
# ledger item is still pending (the UI toast renders result.error)
# ===========================================================================
def test_accept_route_returns_400_error_on_zero_row_apply(
        fake_table, write_spy, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agents.server import accept_proposed_mapping

    mid = _log_annotation()
    monkeypatch.setattr(self_learning, "_neptune_query", _count_probe(0))

    app = FastAPI()
    app.add_api_route("/api/proposed-mappings/{mapping_id}/accept",
                      accept_proposed_mapping, methods=["POST"])
    resp = TestClient(app).post(f"/api/proposed-mappings/{mid}/accept")

    assert resp.status_code == 400
    assert "does not exist in the knowledge graph" in resp.json()["error"]
    assert fake_table.by_id(mid)["status"] == "pending"
