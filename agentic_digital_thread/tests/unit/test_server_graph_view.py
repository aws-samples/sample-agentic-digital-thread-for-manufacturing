# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the `/api/neptune/graph` KG-view endpoint.

The view must NEVER surface :Property nodes (onboarding adds a per-asset
:Property spine — an onboarded i3X-style system carries hundreds-to-thousands
of them, which would flood the view exactly like :Field would). The endpoint
fetches nodes in four budget-separated buckets (core System/Plant uncapped;
connected Concept/IdentityRule/JoinPath LIMIT 1000; the intelligence bucket
incl. MetaNode/MetaEdge LIMIT 600; Asset/AssetModel hierarchy LIMIT 300) and
excludes Property from the edge query the same way Field is excluded.

Neptune Analytics is VPC-private (ADR-0024), so `agents.server.neptune_query`
is patched with a recording fake; assertions run against the captured cypher
and the shaped response.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402


def _node_row(nid, label, props=None):
    return {"n": {"~id": nid, "~labels": [label], "~properties": props or {}}}


def _edge_row(src, tgt, rel):
    return {"src_node": {"~id": src}, "tgt_node": {"~id": tgt}, "rel": rel}


class _RecordingQuery:
    """neptune_query stand-in: records every cypher string, dispatches fixture
    rows by recognizable query fragments (one entry per endpoint bucket)."""

    def __init__(self, responses=None):
        self.queries = []
        self.responses = responses or {}

    def __call__(self, query, *args, **kwargs):
        self.queries.append(query)
        for fragment, rows in self.responses.items():
            if fragment in query:
                return rows
        return []


def _call_endpoint(fake):
    with patch.object(server, "neptune_query", fake), \
            patch.object(server, "neptune_query_checked", fake):
        return asyncio.run(server.neptune_full_graph_route())


# Fragments unique to each of the six queries the endpoint issues.
_CORE_FRAG = "(n:System OR n:Plant)"
_PRIORITY_FRAG = "(n:Concept AND (n)-[]-())"
_INTEL_FRAG = "n:ValueMapping"
_HIERARCHY_FRAG = "n:Asset OR n:AssetModel"
_EDGE_FRAG = "MATCH (a)-[r]->(b)"
_CONCEPT_EDGE_FRAG = "MAPS_TO_CONCEPT"


#: The one aggregate read that counts the left-out labels for the legend
#: (``server._graph_view_not_drawn``): it returns numbers, never a node.
_COUNT_QUERY = server._GRAPH_VIEW_NOT_DRAWN_CYPHER


def test_no_query_matches_bare_property_nodes():
    """:Property may appear ONLY inside NOT-clauses, and in the count query
    that returns how many there are: never matched/returned as a node."""
    fake = _RecordingQuery()
    _call_endpoint(fake)
    assert fake.queries, "endpoint issued no queries"
    for q in fake.queries:
        if q == _COUNT_QUERY:
            continue
        stripped = q.replace("NOT a:Property", "").replace("NOT b:Property", "")
        assert ":Property" not in stripped, f"bare :Property match in: {q}"


def test_no_query_matches_bare_field_nodes():
    """Same invariant for :Field (pre-existing exclusion, must survive the
    restructure). The collapsed MAPS_TO_CONCEPT traversal legitimately walks
    THROUGH Field but returns only System/Concept endpoints, and the count
    query returns only how many there are."""
    fake = _RecordingQuery()
    _call_endpoint(fake)
    for q in fake.queries:
        if q == _COUNT_QUERY:
            continue
        stripped = (
            q.replace("NOT a:Field", "").replace("NOT b:Field", "")
            .replace("(f:Field)-[:MAPS_TO]->", "")
        )
        assert ":Field" not in stripped, f"bare :Field match in: {q}"


def test_four_node_buckets_carry_documented_budgets():
    fake = _RecordingQuery()
    _call_endpoint(fake)
    node_queries = [q for q in fake.queries if "RETURN n" in q]
    assert len(node_queries) == 4, node_queries
    core = next(q for q in node_queries if _CORE_FRAG in q)
    priority = next(q for q in node_queries if _PRIORITY_FRAG in q)
    intel = next(q for q in node_queries if _INTEL_FRAG in q)
    hierarchy = next(q for q in node_queries if _HIERARCHY_FRAG in q)
    # Core backbone is UNCAPPED so System/Plant are always present.
    assert "LIMIT" not in core
    assert priority.rstrip().endswith("LIMIT 1000")
    assert intel.rstrip().endswith("LIMIT 600")
    assert hierarchy.rstrip().endswith("LIMIT 300")


def test_priority_bucket_requires_connected_concepts():
    fake = _RecordingQuery()
    _call_endpoint(fake)
    priority = next(q for q in fake.queries if "n:IdentityRule" in q)
    assert _PRIORITY_FRAG in priority
    assert "n:JoinPath" in priority


def test_catch_all_other_bucket_is_gone():
    """The old 'everything else' query (`NOT n:Field AND NOT n:System ...`)
    is what floods after i3X onboarding — it must not exist in any form."""
    fake = _RecordingQuery()
    _call_endpoint(fake)
    for q in fake.queries:
        assert "NOT n:Field" not in q, f"catch-all bucket survived: {q}"


def test_meta_nodes_included_in_intel_bucket():
    fake = _RecordingQuery()
    _call_endpoint(fake)
    intel = next(q for q in fake.queries if _INTEL_FRAG in q)
    for label in ("n:AuthorityRule", "n:SchemaEntry", "n:Correlation",
                  "n:MetaNode", "n:MetaEdge"):
        assert label in intel, f"{label} missing from intel bucket: {intel}"


def test_hierarchy_bucket_covers_asset_and_model():
    fake = _RecordingQuery()
    _call_endpoint(fake)
    hierarchy = next(q for q in fake.queries if _HIERARCHY_FRAG in q)
    assert "n:Asset" in hierarchy and "n:AssetModel" in hierarchy


def test_edge_query_excludes_field_and_property_with_raised_cap():
    fake = _RecordingQuery()
    _call_endpoint(fake)
    edge_q = next(q for q in fake.queries if _EDGE_FRAG in q)
    for clause in ("NOT a:Field", "NOT b:Field",
                   "NOT a:Property", "NOT b:Property"):
        assert clause in edge_q, f"{clause} missing from edge query: {edge_q}"
    assert "LIMIT 2000" in edge_q


def test_collapsed_maps_to_concept_edge_query_unchanged():
    fake = _RecordingQuery()
    _call_endpoint(fake)
    concept_q = next(q for q in fake.queries if _CONCEPT_EDGE_FRAG in q)
    assert ("MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)"
            "-[:HAS_FIELD]->(f:Field)-[:MAPS_TO]->(c:Concept)") in concept_q
    assert "'MAPS_TO_CONCEPT' AS rel" in concept_q
    assert "LIMIT" not in concept_q


def test_queries_are_static_and_deterministic():
    """All seven cypher strings (six view reads, one left-out count read) are
    static (the endpoint takes no user input): two invocations issue
    byte-identical query sets, so no runtime value can ever be interpolated
    into the cypher."""
    fake_a, fake_b = _RecordingQuery(), _RecordingQuery()
    _call_endpoint(fake_a)
    _call_endpoint(fake_b)
    assert len(fake_a.queries) == 7
    assert fake_a.queries == fake_b.queries


def test_response_shape_and_node_dedupe_unchanged():
    """Existing-fixture behavior: nodes/edges keys, dedupe by `~id`, edges to
    undisplayed nodes dropped, duplicate edge triples collapsed."""
    fake = _RecordingQuery(responses={
        _CORE_FRAG: [
            _node_row("sys-1", "System", {"name": "alpha-system"}),
            _node_row("plant-1", "Plant", {"name": "plant-north"}),
        ],
        _PRIORITY_FRAG: [
            _node_row("con-1", "Concept", {"id": "equipment-id"}),
            # Duplicate ~id across buckets must dedupe to one node.
            _node_row("sys-1", "System", {"name": "alpha-system"}),
        ],
        _INTEL_FRAG: [
            _node_row("vm-1", "ValueMapping", {"concept": "equipment"}),
            _node_row("meta-1", "MetaNode", {"name": "Asset"}),
        ],
        _HIERARCHY_FRAG: [
            _node_row("asset-1", "Asset", {"name": "pump-01"}),
        ],
        _EDGE_FRAG: [
            _edge_row("vm-1", "asset-1", "MAPS_TO_ASSET"),
            _edge_row("vm-1", "asset-1", "MAPS_TO_ASSET"),   # duplicate triple
            _edge_row("asset-1", "ghost-9", "HAS_PROPERTY"),  # undisplayed target
        ],
        _CONCEPT_EDGE_FRAG: [
            _edge_row("sys-1", "con-1", "MAPS_TO_CONCEPT"),
        ],
    })
    result = _call_endpoint(fake)
    assert set(result.keys()) == {"nodes", "edges", "not_drawn"}
    ids = [n["id"] for n in result["nodes"]]
    assert ids == ["sys-1", "plant-1", "con-1", "vm-1", "meta-1", "asset-1"]
    node = result["nodes"][0]
    assert set(node.keys()) == {"id", "label", "type", "props"}
    assert node["label"] == "alpha-system" and node["type"] == "System"
    assert result["edges"] == [
        {"source": "vm-1", "target": "asset-1", "label": "MAPS_TO_ASSET"},
        {"source": "sys-1", "target": "con-1", "label": "MAPS_TO_CONCEPT"},
    ]


def test_meta_node_renders_with_label_and_type():
    fake = _RecordingQuery(responses={
        _INTEL_FRAG: [
            _node_row("meta-e-1", "MetaEdge", {"name": "MAPS_TO_ASSET"}),
        ],
    })
    result = _call_endpoint(fake)
    (node,) = result["nodes"]
    assert node["type"] == "MetaEdge"
    assert node["label"] == "MAPS_TO_ASSET"


def test_query_failure_returns_empty_graph_with_error():
    def _boom(query, *args, **kwargs):
        raise RuntimeError("graph unavailable")

    with patch.object(server, "neptune_query", _boom):
        result = asyncio.run(server.neptune_full_graph_route())
    assert result["nodes"] == [] and result["edges"] == []
    assert result["error_category"] == "graph_query_failed"
    assert "Reference: " in result["error"] and "graph unavailable" not in result["error"]


def test_graph_view_helpers_stay_under_the_50_line_gate():
    """The endpoint is decomposed; pin every graph-view function <=50 lines."""
    import ast
    src = (Path(server.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    targets = {
        "neptune_full_graph", "_graph_view_fetch_nodes",
        "_graph_view_fetch_edges", "_graph_view_shape_nodes",
        "_graph_view_shape_edges",
    }
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            found[node.name] = int(node.end_lineno or node.lineno) - node.lineno + 1
    assert set(found) == targets
    oversized = {k: v for k, v in found.items() if v > 50}
    assert not oversized, oversized
