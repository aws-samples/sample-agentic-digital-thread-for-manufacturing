# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Verifier contracts for complete opaque three-source reconciliation."""
from __future__ import annotations

from agents.guidance_acceptance import opaque_digest
from agents.guidance_reconciliation import build_reconciliation
from agents.guidance_scope import append_note


def _document(note, scope="query_hint"):
    return append_note("", note, scope)


def _row(system_id, note, scope="query_hint"):
    return {"status": "accepted", "proposed_type": "guidance_note",
            "from_system": system_id, "to_value": note,
            "guidance_scope": scope}


def test_reconciliation_reports_every_directional_pair_and_delivery_orphan():
    accepted = [_row("accepted-only", "accepted note")]
    graph = {"graph-only": _document("graph note")}
    delivery = {"delivery-only": _document("delivery note")}

    result = build_reconciliation(accepted, graph, delivery)

    assert result["status"] == "mismatch"
    assert result["counts"] == {"accepted": 1, "graph": 1, "delivered": 1}
    assert {entry["kind"] for entry in result["mismatches"]} == {
        "accepted_not_graph", "accepted_not_delivered",
        "graph_not_accepted", "graph_not_delivery",
        "delivery_not_accepted", "delivery_not_graph",
        "accepted_only_system", "graph_only_system", "delivery_only_system",
    }
    orphan = next(entry for entry in result["mismatches"]
                  if entry["kind"] == "delivery_only_system")
    assert orphan["system_digest"] == opaque_digest("delivery-only")
    assert orphan["note_digest"] is None
    rendered = repr(result)
    for raw in ("accepted-only", "accepted note", "graph-only", "graph note",
                "delivery-only", "delivery note"):
        assert raw not in rendered


def test_each_unavailable_source_is_none_without_zeroing_other_sources():
    accepted_unavailable = build_reconciliation(
        None, {"system-a": _document("graph")},
        {"system-a": _document("delivered")})
    assert accepted_unavailable["counts"] == {
        "accepted": None, "graph": 1, "delivered": 1}
    assert accepted_unavailable["sources"] == {
        "accepted": "unavailable", "graph": "ok", "delivery": "ok"}

    graph_unavailable = build_reconciliation(
        [_row("system-a", "accepted")], None,
        {"system-a": _document("delivered")})
    assert graph_unavailable["counts"] == {
        "accepted": 1, "graph": None, "delivered": 1}

    delivery_unavailable = build_reconciliation(
        [_row("system-a", "accepted")],
        {"system-a": _document("graph")}, None)
    assert delivery_unavailable["counts"] == {
        "accepted": 1, "graph": 1, "delivered": None}


def test_collection_lists_delivery_independently_including_orphan(monkeypatch):
    import agents.guidance_reconciliation as reconciliation
    import agents.self_learning as learning
    import agents.system_guidance as system_guidance

    monkeypatch.setattr(learning, "_query_by_status", lambda _status: [
        _row("accepted-system", "accepted")])
    monkeypatch.setattr(learning, "_neptune_query", lambda *_a, **_k: [
        {"id": "graph-system", "qg": _document("graph")}])
    monkeypatch.setattr(system_guidance, "list_snapshots", lambda **_kwargs: {
        "delivery-orphan": system_guidance.GuidanceSnapshot.present(
            _document("delivery"), 1)})

    result = reconciliation.collect_reconciliation()

    assert result["counts"] == {"accepted": 1, "graph": 1, "delivered": 1}
    assert "delivery_only_system" in {
        item["kind"] for item in result["mismatches"]}
