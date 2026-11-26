# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``_repoint_maps_to`` fail-loud Field-existence pre-check (lifecycle bug 2).

Live incident (2026-07-14, pilot env): accepting a concept_mapping correction whose
corrected column was NEVER discovered into the graph DELETEd every prior MAPS_TO
edge for (system, concept), then the ``MATCH (f:Field {id: $fid}) ... MERGE``
silently matched zero rows — the concept lost ALL mappings (covered → UNCOVERED)
with no error. The fix verifies the Field node exists BEFORE any delete and raises
a clear ``ValueError`` otherwise, so the accept route returns an actionable error
and the prior edges stay intact.

Offline — table/graph seams replaced per the ``test_self_learning_lifecycle``
pattern (``FakeTable`` / ``NeptuneGraphFake`` are reused from that module, the
same way ``test_rejection_damping`` does).
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneGraphFake
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneGraphFake


def _install(monkeypatch, graph: NeptuneGraphFake, table: FakeTable | None = None):
    if table is not None:
        monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", graph.write)
    monkeypatch.setattr(self_learning, "_neptune_query", graph.query)


_ITEM = {
    "to_system": "sys-a", "concept": "machine-speed",
    "to_field": "counter_tbl.act_speed", "confidence": "0.95",
}


def test_missing_field_raises_valueerror_before_any_delete(monkeypatch):
    graph = NeptuneGraphFake(field_exists=False)
    _install(monkeypatch, graph)

    with pytest.raises(ValueError) as exc:
        self_learning._repoint_maps_to(dict(_ITEM))

    msg = str(exc.value)
    # Actionable error naming the column and system, per the fix contract.
    assert "corrected column 'counter_tbl.act_speed'" in msg
    assert "not in the discovered schema" in msg
    assert "'sys-a'" in msg
    assert "correction not applied" in msg
    # NO delete (or any other write) executed — prior edges stay intact.
    assert not any("DELETE" in c for c in graph.cyphers())
    assert graph.writes == []


def test_present_field_keeps_delete_then_merge_behavior(monkeypatch):
    graph = NeptuneGraphFake(field_exists=True)
    _install(monkeypatch, graph)

    node_id = self_learning._repoint_maps_to(dict(_ITEM))

    assert node_id == "mapsto-sys-a-machine-speed"
    cyphers = graph.cyphers()
    # Old behavior preserved: delete(s) then the corrected-edge MERGE.
    delete_idx = next(i for i, c in enumerate(cyphers) if "DELETE r" in c)
    edge_idx = next(i for i, c in enumerate(cyphers) if "MERGE (f)-[r:MAPS_TO]->(c)" in c)
    assert delete_idx < edge_idx
    _cypher, params = graph.edge_write()
    assert params["fid"] == "sys-a.counter_tbl.act_speed"
    assert params["ver"] == 2  # no prior edge (query → None) ⇒ 1 + 1


def test_existence_check_runs_before_version_read(monkeypatch):
    """The pre-check is the FIRST graph interaction — nothing (not even the
    max-version read) happens for an undiscovered column. The only follow-up
    read is the honesty guard's System cross-check (outage vs missing column)."""
    graph = NeptuneGraphFake(field_exists=False)
    _install(monkeypatch, graph)

    with pytest.raises(ValueError):
        self_learning._repoint_maps_to(dict(_ITEM))

    assert len(graph.queries) == 2
    assert "count(f)" in graph.queries[0][0]
    assert "count(s)" in graph.queries[1][0]
    # No max-version read happened.
    assert not any("max(r.version)" in q for q, _ in graph.queries)


# --------------------------------------------------------------------------- #
# M2 honesty guard: a graph OUTAGE (the read seam returns [] on backend errors)
# must NOT masquerade as "column not in schema" — it raises RuntimeError.
# --------------------------------------------------------------------------- #
def test_graph_outage_raises_runtimeerror_not_schema_error(monkeypatch):
    graph = NeptuneGraphFake(graph_down=True)
    _install(monkeypatch, graph)

    with pytest.raises(RuntimeError, match="graph unavailable — correction not applied"):
        self_learning._repoint_maps_to(dict(_ITEM))

    # Both the existence read and the System cross-check ran; nothing was written.
    assert len(graph.queries) == 2
    assert graph.writes == []


def test_missing_field_with_live_graph_still_raises_valueerror(monkeypatch):
    """Graph reachable (System node readable) + Field genuinely absent → the
    actionable schema ValueError, NOT the outage RuntimeError."""
    graph = NeptuneGraphFake(field_exists=False, system_exists=True)
    _install(monkeypatch, graph)

    with pytest.raises(ValueError, match="not in the discovered schema"):
        self_learning._repoint_maps_to(dict(_ITEM))

    assert graph.writes == []


def test_accept_route_propagates_error_and_prior_edges_intact(monkeypatch):
    """End-to-end through the lifecycle: a user correction whose column is not in
    the graph RAISES out of record_user_correction (the accept route surfaces it)
    and executes no graph write at all."""
    table = FakeTable()
    graph = NeptuneGraphFake(field_exists=False)
    _install(monkeypatch, graph, table)

    with pytest.raises(ValueError, match="correction not applied"):
        self_learning.record_user_correction(
            from_system="", from_field="", from_value="",
            to_system="sys-a", to_field="counter_tbl.act_speed", to_value="",
            concept="machine-speed", proposed_type="concept_mapping",
        )

    assert graph.writes == []
