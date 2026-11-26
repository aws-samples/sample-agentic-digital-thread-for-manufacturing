# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Knowledge Graph overview says how many nodes it leaves out, or that it
could not tell.

The overview never draws :Field or :Property nodes (leaf schema and telemetry
metadata, too many to render) or :OnboardingLog records (one per provision,
with no edge). The legend used to count only drawn nodes, so it read "Field 0"
on a graph holding thousands. ``not_drawn`` now gives each left-out label's
count from one static aggregate read through the checked graph reader. A label
whose count could not be read is ``None``, never 0, and a failed count never
costs the graph that was read.

The cases are shared with the frontend
(``frontend/src/test/fixtures/graph-view-not-drawn-cases.json``), whose half
renders the same ``not_drawn`` in the legend.
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
from agents.db import GraphQueryError  # noqa: E402

_FIXTURE = (_ADT_ROOT / "frontend" / "src" / "test" / "fixtures"
            / "graph-view-not-drawn-cases.json")
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]

_SYSTEM_ROW = {"n": {"~id": "sys-1", "~labels": ["System"],
                     "~properties": {"name": "alpha-system"}}}


class _Graph:
    """The two graph read seams: ``view`` answers the view's node and edge
    reads (one System node, nothing else); ``checked`` answers the count read
    with ``count_rows`` or raises the checked reader's error."""

    def __init__(self, count_rows=None, count_error=False):
        self.count_rows = count_rows or []
        self.count_error = count_error
        self.checked_queries = []

    def view(self, query, *args, **kwargs):
        return [_SYSTEM_ROW] if "(n:System OR n:Plant)" in query else []

    def checked(self, query, *args, **kwargs):
        self.checked_queries.append(query)
        if self.count_error:
            raise GraphQueryError("ValidationException", "count failed")
        return self.count_rows


def _view(graph):
    with patch.object(server, "neptune_query", graph.view), \
            patch.object(server, "neptune_query_checked", graph.checked):
        return asyncio.run(server.neptune_full_graph_route())


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_not_drawn_matches_the_shared_cases(case):
    out = _view(_Graph(case.get("count_rows"), case.get("count_error", False)))
    assert out["not_drawn"] == case["not_drawn"]
    # The graph that was read is kept whatever the count read did.
    assert [n["id"] for n in out["nodes"]] == ["sys-1"]
    assert "error" not in out


def test_one_static_aggregate_count_read_returns_numbers_only():
    first, second = _Graph(), _Graph()
    _view(first)
    _view(second)
    assert first.checked_queries == second.checked_queries
    (query,) = first.checked_queries
    assert query == server._GRAPH_VIEW_NOT_DRAWN_CYPHER
    returned = query.split("RETURN", 1)[1]
    assert "count(o) AS onboarding_log_count" in returned
    assert set(returned.replace(",", " ").split()) == {
        "field_count", "property_count", "count(o)", "AS", "onboarding_log_count"}


@pytest.mark.parametrize("value", [True, "1922", 19.5, -1, None])
def test_a_count_that_is_not_a_whole_number_is_unavailable(value):
    out = _view(_Graph([{"field_count": value, "property_count": 3,
                         "onboarding_log_count": 2}]))
    assert out["not_drawn"] == {"Field": None, "Property": 3, "OnboardingLog": 2}


def test_a_failed_view_read_still_answers_without_counts(monkeypatch):
    def _boom():
        raise RuntimeError("graph down")

    monkeypatch.setattr(server, "_graph_view_fetch_nodes", _boom)
    out = server.neptune_full_graph()
    assert out["nodes"] == [] and out["edges"] == []
    assert "not_drawn" not in out
