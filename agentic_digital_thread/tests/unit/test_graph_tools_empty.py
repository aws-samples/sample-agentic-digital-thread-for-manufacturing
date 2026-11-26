# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data-level anti-thrash: an EMPTY ``explore_graph`` result must be informative.

When a Cypher query matches nothing, the bare ``{"results": []}`` gave the model
zero context, so it broadened/retried the search several times before concluding
"that node type doesn't exist" (observed in a live trace: 4 escalating empty
explore_graph calls). The canonical ``graph_tools.explore_graph`` now, on an empty
result, runs ONE inventory query and returns the graph's actual node-type census
plus an explicit "do NOT broaden" note so the model can conclude from evidence
after a single call. These tests pin that behaviour and confirm the non-empty
path is unchanged (no extra inventory query).

Fully offline: ``graph_tools.neptune_query`` is monkeypatched.
"""
import json

import agents.graph_tools as graph_tools

# Inventory the graph would return: System + Concept exist, NO Asset/equipment.
_INVENTORY = [
    {"label": "System", "count": 6},
    {"label": "Concept", "count": 40},
    {"label": "Field", "count": 120},
]


def _fake_neptune(primary_result):
    """Return a neptune_query double: primary query -> primary_result, the
    inventory query (``UNWIND labels(n)``) -> the fixed census. Records calls."""
    calls = []

    def _nq(cypher, *args, **kwargs):
        calls.append(cypher)
        if "UNWIND labels(n)" in cypher:
            return list(_INVENTORY)
        return primary_result

    return _nq, calls


def test_explore_graph_empty_returns_node_inventory_and_stop_signal(monkeypatch):
    nq, calls = _fake_neptune(primary_result=[])
    monkeypatch.setattr(graph_tools, "neptune_query", nq)
    monkeypatch.setattr(graph_tools, "neptune_query_checked", nq)

    out = json.loads(graph_tools.explore_graph("MATCH (a:Asset) RETURN a"))

    # Empty results, but now flagged + explained.
    assert out["results"] == []
    assert out["empty"] is True
    # The actual node-type census is surfaced (data-driven, not hardcoded labels).
    assert out["graph_node_types"] == _INVENTORY
    present = {row["label"] for row in out["graph_node_types"]}
    assert "System" in present
    # The searched-for type is provably absent from the census.
    assert "Asset" not in present
    # An explicit stop-broadening signal is present.
    assert "do NOT broaden" in out["note"]
    # Exactly two queries ran: the primary + one inventory query.
    assert len(calls) == 2
    assert "UNWIND labels(n)" in calls[1]


def test_explore_graph_non_empty_path_unchanged(monkeypatch):
    nq, calls = _fake_neptune(primary_result=[{"x": 1}])
    monkeypatch.setattr(graph_tools, "neptune_query", nq)
    monkeypatch.setattr(graph_tools, "neptune_query_checked", nq)

    out = json.loads(graph_tools.explore_graph("MATCH (s:System) RETURN s"))

    # Non-empty path is exactly the old shape — no empty flag, no inventory.
    assert out == {"results": [{"x": 1}]}
    assert "empty" not in out
    assert "graph_node_types" not in out
    # Only the primary query ran; the inventory query was NOT triggered.
    assert len(calls) == 1
