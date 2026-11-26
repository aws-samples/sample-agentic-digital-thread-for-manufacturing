# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.metamodel`` — the self-describing graph vocabulary
— and its orchestrator cold-start prompt seam.

Offline / no AWS / no Neptune: ``neptune_query`` / ``neptune_write`` are
monkeypatched in the module namespace; the orchestrator ``Agent`` construction
is captured (same pattern as ``tests/test_orchestrator_memory.py``).

Pinned contracts:
  * ``write_metamodel`` emits ONE parameterized MERGE per META_NODES +
    META_EDGES entry (idempotent — MERGE, never CREATE; no entry values
    interpolated into the cypher text).
  * The vocabulary describes THIS repo's graph shape: ``HAS_PARENT`` is the
    ONLY hierarchy edge — the inverse-edge name appears NOWHERE in the module
    (contract §3).
  * ``fetch_metamodel_text`` renders label + edge lines and the
    undocumented-edge drift suffix from live query results; returns '' when
    the graph holds no metamodel and '' on any exception.
  * MAPS_TO_ASSET guidance present, parameter-placeholder style, free of demo
    literals (denylist over the whole module source).
  * ``build_orchestrator`` appends the fetched text AFTER the static prompt;
    a fetch failure leaves the static prompt byte-identical.
  * Set-equality cross-pin: META_EDGES names and META_NODES labels
    equal the documented vocabulary sets below — the graph-shape contract's
    third copy cannot drift silently.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.metamodel as metamodel  # noqa: E402

_MODULE_SOURCE = Path(metamodel.__file__).read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# The documented vocabulary (set-equality cross-pin with the graph-shape
# contract, docs/graph-shape-contract.md). Code that introduces a new label or
# edge type MUST extend BOTH the metamodel and these pinned sets (and the
# contract doc) — that is the module's ownership rule.
# ---------------------------------------------------------------------------
_DOCUMENTED_EDGE_NAMES = {
    "HAS_SCHEMA",
    "HAS_FIELD",
    "MAPS_TO",
    "HAS_PARENT",       # the ONLY hierarchy edge (contract §3, F6)
    "HAS_PROPERTY",
    "INSTANCE_OF",
    "MODELED_BY",
    "LOCATED_AT",
    "FROM_SYSTEM",
    "TO_SYSTEM",
    "LINKS_CONCEPT",
    "APPLIES_TO",
    "MAPS_TO_ASSET",
    "DIFFERENT_IDENTIFIER_FROM",  # measured different-identifier column pairs (ADR-0098)
}

_DOCUMENTED_NODE_LABELS = {
    "MetricDefinition",   # ADR-0066 — graph-resident deterministic metric definitions
    "Plant",
    "System",
    "SchemaEntry",
    "Field",
    "Concept",
    "Asset",
    "AssetModelRole",      # asset-model role per (system, model): agents/asset_role_pass.py
    "Property",
    "AssetModel",
    "ValueMapping",
    "JoinPath",
    "IdentityRule",
    "AuthorityRule",
    "ConceptOverlay",
    "MetaNode",
    "MetaEdge",
}

# Demo-literal denylist (project constraint: no demo content in engine code).
_DEMO_DENYLIST = [
    "Milwaukee",
    "Detroit",
    "Cedar Rapids",
    "Indianapolis",
    "Line1Filler",
    "AnyCompany",
    "WasherPump",
    "BottlingMachine",
    "FillerPump",
    "maint_jones",
    "MKE-FILL",
]


# ---------------------------------------------------------------------------
# (1) write_metamodel — one parameterized MERGE per entry
# ---------------------------------------------------------------------------
def test_write_metamodel_one_parameterized_merge_per_entry(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def _capture(cypher, parameters=None):
        captured.append((cypher, parameters))
        return True

    monkeypatch.setattr(metamodel, "neptune_write", _capture)
    count = metamodel.write_metamodel()

    expected = len(metamodel.META_NODES) + len(metamodel.META_EDGES)
    assert count == expected
    assert len(captured) == expected

    for cypher, params in captured:
        assert "MERGE" in cypher, f"idempotent write must MERGE: {cypher}"
        assert "CREATE" not in cypher, f"never CREATE (not idempotent): {cypher}"
        assert params, f"every write must be parameterized: {cypher}"
        # No entry VALUE may be interpolated into the cypher text. The
        # MetaNode/MetaEdge SELF-entries' label values legitimately equal the
        # structural label tokens in the MERGE pattern — exempt those two.
        for value in params.values():
            if value and value not in ("MetaNode", "MetaEdge"):
                assert value not in cypher, (
                    f"parameter value leaked into cypher text: {value!r} in {cypher}"
                )

    node_writes = [c for c, _ in captured if ":MetaNode" in c]
    edge_writes = [c for c, _ in captured if ":MetaEdge" in c]
    assert len(node_writes) == len(metamodel.META_NODES)
    assert len(edge_writes) == len(metamodel.META_EDGES)
    assert all("$label" in c for c in node_writes)
    assert all("$name" in c for c in edge_writes)


def test_write_metamodel_counts_only_successful_writes(monkeypatch):
    monkeypatch.setattr(metamodel, "neptune_write", lambda *a, **k: False)
    assert metamodel.write_metamodel() == 0


# ---------------------------------------------------------------------------
# (2) HAS_PARENT is the ONLY hierarchy edge — inverse name appears NOWHERE
# ---------------------------------------------------------------------------
def test_inverse_hierarchy_edge_absent_from_module():
    """Pin the target's HAS_PARENT-only decision (contract §3, F6): the
    metamodel must not document — or even mention — the removed inverse edge."""
    banned = "HAS_" + "CHILD"  # composed so THIS file doesn't trip source greps
    assert banned not in _MODULE_SOURCE, (
        f"{banned} found in agents/metamodel.py — HAS_PARENT is the ONLY "
        "hierarchy edge in this repo (graph-shape contract §3, F6)"
    )
    assert any(e["name"] == "HAS_PARENT" for e in metamodel.META_EDGES)


def test_has_parent_guidance_traverses_incoming_edge():
    """The descendant traversal must use the written direction: <-[:HAS_PARENT*]-."""
    entry = next(e for e in metamodel.META_EDGES if e["name"] == "HAS_PARENT")
    assert "<-[:HAS_PARENT*]-" in entry["guidance"]
    assert "$site" in entry["guidance"]  # parameter placeholder, not a literal


# ---------------------------------------------------------------------------
# (3) fetch_metamodel_text — rendering, empty, exception
# ---------------------------------------------------------------------------
_FIXTURE_NODES = [
    {"label": "Asset", "description": "Hierarchy node.", "key_properties": "source_id, name"},
    {"label": "Property", "description": "Telemetry channel.", "key_properties": "source_id"},
]
_FIXTURE_EDGES = [
    {"name": "HAS_PROPERTY", "from_label": "Asset", "to_label": "Property",
     "description": "Asset owns this channel.", "guidance": "Resolve channels first."},
    {"name": "HAS_PARENT", "from_label": "Asset", "to_label": "Asset",
     "description": "Child to parent.", "guidance": ""},
]


def _fixture_query(live_edges):
    def _query(cypher, parameters=None):
        if ":MetaNode" in cypher:
            return _FIXTURE_NODES
        if ":MetaEdge" in cypher:
            return _FIXTURE_EDGES
        if "type(r)" in cypher:
            return [{"rel": r} for r in live_edges]
        raise AssertionError(f"unexpected query: {cypher}")
    return _query


def test_fetch_renders_labels_edges_and_drift_suffix(monkeypatch):
    monkeypatch.setattr(
        metamodel, "neptune_query",
        _fixture_query(["HAS_PROPERTY", "HAS_PARENT", "SOME_NEW_EDGE"]),
    )
    text = metamodel.fetch_metamodel_text()

    assert "LIVE GRAPH METAMODEL" in text
    assert "- Asset: Hierarchy node. [props: source_id, name]" in text
    assert "- Property: Telemetry channel. [props: source_id]" in text
    assert "- (Asset)-[:HAS_PROPERTY]->(Property): Asset owns this channel." in text
    assert "USE: Resolve channels first." in text
    # Drift suffix: only the undocumented live edge is listed.
    assert "NOT documented above" in text
    assert "SOME_NEW_EDGE" in text
    assert text.count("USE:") == 1  # empty guidance renders no USE line


def test_fetch_no_drift_suffix_when_all_edges_documented(monkeypatch):
    monkeypatch.setattr(
        metamodel, "neptune_query", _fixture_query(["HAS_PROPERTY", "HAS_PARENT"])
    )
    text = metamodel.fetch_metamodel_text()
    assert text
    assert "NOT documented above" not in text


def test_fetch_returns_empty_when_graph_has_no_metamodel(monkeypatch):
    monkeypatch.setattr(metamodel, "neptune_query", lambda *a, **k: [])
    assert metamodel.fetch_metamodel_text() == ""


def test_fetch_returns_empty_on_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simulated graph failure")

    monkeypatch.setattr(metamodel, "neptune_query", _boom)
    assert metamodel.fetch_metamodel_text() == ""


def test_fetch_drift_check_failure_is_non_fatal(monkeypatch):
    """A failure INSIDE the drift check must not blank the whole section."""
    def _query(cypher, parameters=None):
        if ":MetaNode" in cypher:
            return _FIXTURE_NODES
        if ":MetaEdge" in cypher:
            return _FIXTURE_EDGES
        raise RuntimeError("live-edge introspection failed")

    monkeypatch.setattr(metamodel, "neptune_query", _query)
    text = metamodel.fetch_metamodel_text()
    assert "LIVE GRAPH METAMODEL" in text
    assert "NOT documented above" not in text


# ---------------------------------------------------------------------------
# (4) MAPS_TO_ASSET guidance + demo-literal denylist
# ---------------------------------------------------------------------------
def test_maps_to_asset_guidance_present_and_parameterized():
    entry = next(e for e in metamodel.META_EDGES if e["name"] == "MAPS_TO_ASSET")
    assert entry["from_label"] == "ValueMapping"
    assert entry["to_label"] == "Asset"
    guidance = entry["guidance"]
    assert "MAPS_TO_ASSET" in guidance
    assert "$site" in guidance and "$n" in guidance  # placeholders, not literals
    assert "NEVER free-text search" in guidance


def test_module_source_has_no_demo_literals():
    lower = _MODULE_SOURCE.lower()
    for banned in _DEMO_DENYLIST:
        assert banned.lower() not in lower, (
            f"demo literal {banned!r} found in agents/metamodel.py — the "
            "metamodel is engine code (no demo content outside agents/demo/)"
        )


# ---------------------------------------------------------------------------
# (5) build_orchestrator appends the fetched text after the static prompt
# ---------------------------------------------------------------------------
def _fake_config():
    return SimpleNamespace(
        has_memory=False,
        has_gateway=False,
        agentcore_gateway_url=None,
        memory_id=None,
        aws_region="us-east-1",
    )


def _build_and_capture_prompt(fetch_impl):
    import agents.orchestrator as orch

    captured: list[dict] = []

    def _fake_agent(**kwargs):
        captured.append(kwargs)
        return MagicMock(name="Agent")

    orch._mcp_clients.clear()
    orch._metamodel_cache_clear()  # M3 TTL cache: patched fetch must apply
    with patch.object(orch, "get_config", return_value=_fake_config()), \
         patch.object(orch, "Agent", side_effect=_fake_agent), \
         patch.object(metamodel, "fetch_metamodel_text", side_effect=fetch_impl):
        orch.build_orchestrator("sess-1", "user-1")
    orch._metamodel_cache_clear()  # never leak fixture text into other tests
    return orch._build_orchestrator_prompt(), captured[0]["system_prompt"]


def test_build_orchestrator_appends_metamodel_after_static_prompt():
    sentinel = "\nLIVE GRAPH METAMODEL (test fixture)\n- (A)-[:X]->(B): test edge\n"
    static, built = _build_and_capture_prompt(lambda: sentinel)
    assert built == static + sentinel
    assert built.startswith(static)


def test_build_orchestrator_fetch_failure_keeps_static_prompt_unchanged():
    def _boom():
        raise RuntimeError("simulated metamodel fetch failure")

    static, built = _build_and_capture_prompt(_boom)
    assert built == static


def test_metamodel_prompt_suffix_is_ttl_cached(monkeypatch):
    """build_orchestrator is per-request — without the TTL cache
    every chat turn paid 3 Neptune queries (incl. a full DISTINCT edge-type
    scan). One fetch serves subsequent builds within the TTL; failures/empty
    results are NOT cached (the next build retries)."""
    import agents.orchestrator as orch

    calls: list[int] = []

    def _fetch():
        calls.append(1)
        return "\nMETA fixture\n"

    orch._metamodel_cache_clear()
    monkeypatch.setattr(metamodel, "fetch_metamodel_text", _fetch)
    assert orch._metamodel_prompt_suffix() == "\nMETA fixture\n"
    assert orch._metamodel_prompt_suffix() == "\nMETA fixture\n"
    assert len(calls) == 1
    orch._metamodel_cache_clear()
    # Empty result is not cached — the next call fetches again.
    monkeypatch.setattr(metamodel, "fetch_metamodel_text", lambda: "")
    assert orch._metamodel_prompt_suffix() == ""
    monkeypatch.setattr(metamodel, "fetch_metamodel_text", _fetch)
    assert orch._metamodel_prompt_suffix() == "\nMETA fixture\n"
    orch._metamodel_cache_clear()


def test_static_prompt_builder_never_fetches(monkeypatch):
    """The live fetch stays OUT of _build_orchestrator_prompt (offline prompt-pin
    tests and ORCHESTRATOR_PROMPT readers must see the static prompt only)."""
    import agents.orchestrator as orch

    def _boom():
        raise AssertionError("_build_orchestrator_prompt must not fetch the metamodel")

    monkeypatch.setattr(metamodel, "fetch_metamodel_text", _boom)
    assert orch._build_orchestrator_prompt()  # renders offline, no fetch


# ---------------------------------------------------------------------------
# (6) Set-equality cross-pin: third copy cannot drift silently
# ---------------------------------------------------------------------------
def test_meta_edges_names_equal_documented_set():
    names = {e["name"] for e in metamodel.META_EDGES}
    assert names == _DOCUMENTED_EDGE_NAMES, (
        "META_EDGES drifted from the documented vocabulary — update "  # nosec B608 - test fixture composing expected SQL from module constants
        "docs/graph-shape-contract.md AND this pinned set together.\n"
        f"missing from metamodel: {sorted(_DOCUMENTED_EDGE_NAMES - names)}\n"
        f"undocumented in contract pin: {sorted(names - _DOCUMENTED_EDGE_NAMES)}"
    )


def test_meta_nodes_labels_equal_documented_set():
    labels = {m["label"] for m in metamodel.META_NODES}
    assert labels == _DOCUMENTED_NODE_LABELS, (
        "META_NODES drifted from the documented vocabulary — update "  # nosec B608 - test fixture composing expected SQL from module constants
        "docs/graph-shape-contract.md AND this pinned set together.\n"
        f"missing from metamodel: {sorted(_DOCUMENTED_NODE_LABELS - labels)}\n"
        f"undocumented in contract pin: {sorted(labels - _DOCUMENTED_NODE_LABELS)}"
    )


def test_meta_entries_have_no_duplicates_and_required_keys():
    edge_names = [e["name"] for e in metamodel.META_EDGES]
    node_labels = [m["label"] for m in metamodel.META_NODES]
    assert len(edge_names) == len(set(edge_names))
    assert len(node_labels) == len(set(node_labels))
    for m in metamodel.META_NODES:
        assert m["description"] and m["key_properties"]
    for e in metamodel.META_EDGES:
        assert e["from_label"] and e["to_label"] and e["description"]
