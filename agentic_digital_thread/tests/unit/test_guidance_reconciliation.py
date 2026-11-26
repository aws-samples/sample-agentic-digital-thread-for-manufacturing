# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only guidance reconciliation API and opaque mismatch reporting."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _accepted_rows(count=8):
    return [
        {"mapping_id": f"mapping-{index}", "status": "accepted",
         "proposed_type": "guidance_note", "from_system": "system-a",
         "to_value": f"requirement number {index}"}
        for index in range(count)
    ]


def _graph_text(count=8):
    return "Base. " + " ".join(
        f"LEARNED: requirement number {index}" for index in range(count))


def test_live_equivalent_eight_accepted_three_delivered_is_opaque():
    from agents.guidance_reconciliation import build_reconciliation
    rows = _accepted_rows()
    graph = {"system-a": _graph_text()}
    delivered = {"system-a": " ".join(
        f"LEARNED: requirement number {index}" for index in range(3))}

    result = build_reconciliation(rows, graph, delivered)

    assert result["status"] == "mismatch"
    assert result["counts"] == {"accepted": 8, "graph": 8, "delivered": 3}
    assert result["mismatch_count"] == 10
    assert {item["kind"] for item in result["mismatches"]} == {
        "accepted_not_delivered", "graph_not_delivery"}
    rendered = repr(result)
    assert "system-a" not in rendered
    assert all(row["to_value"] not in rendered for row in rows)


def test_duplicate_accepted_proposals_preserve_row_count_semantics():
    from agents.guidance_reconciliation import build_reconciliation
    row = _accepted_rows(1)[0]
    result = build_reconciliation(
        [row, dict(row, mapping_id="duplicate")],
        {"system-a": _graph_text(1)},
        {"system-a": "LEARNED: requirement number 0"},
    )
    assert result["counts"] == {"accepted": 2, "graph": 1, "delivered": 1}
    assert result["status"] == "mismatch"


def test_reconciliation_preserves_unavailable_states_without_fake_zeros():
    from agents.guidance_reconciliation import build_reconciliation
    result = build_reconciliation(_accepted_rows(2), None, None)
    assert result["status"] == "unavailable"
    assert result["counts"] == {"accepted": 2, "graph": None, "delivered": None}
    assert result["sources"]["graph"] == "unavailable"
    assert result["sources"]["delivery"] == "unavailable"


def test_reconciliation_route_is_get_only_and_performs_no_mutation(monkeypatch):
    import agents.guidance_reconciliation as reconciliation
    from agents.server import guidance_reconciliation_endpoint

    expected = {"status": "mismatch", "counts": {"accepted": 8, "graph": 8,
                "delivered": 3}, "sources": {"accepted": "ok", "graph": "ok",
                "delivery": "ok"}, "mismatches": [], "mismatch_count": 5,
                "truncated": False}
    calls = []
    monkeypatch.setattr(
        reconciliation, "collect_reconciliation",
        lambda: calls.append("read") or expected)
    app = FastAPI()
    app.add_api_route("/api/query-guidance/reconciliation",
                      guidance_reconciliation_endpoint, methods=["GET"])
    client = TestClient(app)

    assert client.get("/api/query-guidance/reconciliation").json() == expected
    assert client.post("/api/query-guidance/reconciliation").status_code == 405
    assert calls == ["read"]


def test_real_reconciliation_route_is_steward_gated():
    import inspect
    import agents.server as server
    source = inspect.getsource(server)
    marker = '"/api/query-guidance/reconciliation"'
    window = source[source.index(marker) - 100:source.index(marker) + 500]
    assert "_check_steward" in window
