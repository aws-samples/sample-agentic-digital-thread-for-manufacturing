# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the DETERMINISTIC Mode-B graph-first routing seed (#5).

In Mode B (SYSTEM_QUERY_MODE=orchestrator) explore_graph is described as the
"mandatory first routing call", but the model can SKIP it — leaving LEVER 1
(routed_system_schemas + concept_to_column) out of context, so it guesses columns
(COLUMN_NOT_FOUND) and the graph is never consulted (neptune_queries=0). The
graph-first seed makes routing a DETERMINISTIC pre-req: before the model composes
any SQL, ``orchestrator.build_graph_first_seed`` fetches the routed schema (via the
SAME ``_explore_graph_with_schema`` path) and ``runtime_app._graph_first_prelude``
MERGES it into the initial user turn and emits a VISIBLE explore_graph step.

DRY: reuses ``_explore_graph_with_schema`` (canonical traversal + cached schema
path) — these tests mock the same seams as ``test_orchestrator_modeb_schema_routing``
and use only SYNTHETIC data (no dataset literals). Offline — no AWS / LLM / gateway /
Neptune.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
import agents.runtime_app as runtime_app
import agents.schema_cache as schema_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    schema_cache.reset()
    orch._mcp_clients.clear()
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=False, agentcore_gateway_url=None),
    )
    yield
    schema_cache.reset()


def _install_seams(monkeypatch, *, systems, traversal_result=None, build=None):
    """Patch the seams _explore_graph_with_schema reuses (same as the routing test)."""
    traversal_result = traversal_result if traversal_result is not None else json.dumps(
        {"results": [{"note": "routing overview"}]}
    )
    monkeypatch.setattr(orch, "_explore_graph", lambda cypher: traversal_result)
    monkeypatch.setattr("agents.db.neptune_query", lambda *_a, **_k: list(systems))
    monkeypatch.setattr(schema_cache, "cheap_schema_version", lambda _sid: "v1")

    def _default_build(system_id, include_samples=True):
        assert include_samples is False  # bounded: metadata only
        return {
            "system_id": system_id,
            "tables": {
                "t": {
                    "columns": ["c", "p"],
                    "column_types": {"c": "varchar", "p": "date"},
                    "partition_keys": ["p"],
                    "concept_map": {"c": {"concept": "concept.c"}},
                }
            },
            "rendered": "ignored",
        }

    monkeypatch.setattr(
        "agents.schema_bundle.build_schema_bundle", build or _default_build
    )


# --------------------------------------------------------------------------- #
# build_graph_first_seed — the pure seed builder
# --------------------------------------------------------------------------- #
def test_seed_injects_routed_schemas_and_concept_to_column(monkeypatch):
    """The seed carries routed_system_schemas AND the compact concept_to_column
    binding for every onboarded system — the LEVER-1 payload the model needs to bind
    columns instead of guessing."""
    _install_seams(
        monkeypatch,
        systems=[
            {"id": "sys-a", "name": "Alpha", "connection_type": "Athena"},
            {"id": "sys-b", "name": "Beta", "connection_type": "PostgreSQL"},
        ],
    )
    seed = orch.build_graph_first_seed()
    assert seed is not None
    # Clearly-labeled block that instructs binding and forbids guessing.
    assert seed.startswith(orch._GRAPH_FIRST_SEED_HEADER[:40])
    assert "concept_to_column" in seed
    assert "NEVER guess" in seed
    # The embedded routing JSON has both systems' schema + the concept binding.
    payload = json.loads(seed[len(orch._GRAPH_FIRST_SEED_HEADER):])
    assert set(payload["routed_system_schemas"]) == {"sys-a", "sys-b"}
    tbl = payload["routed_system_schemas"]["sys-a"]["tables"]["t"]
    assert tbl["concept_to_column"] == {"concept.c": "c"}


def test_seed_none_when_no_onboarded_systems(monkeypatch):
    """No onboarded systems ⇒ nothing deterministic to inject ⇒ None (fail-safe)."""
    _install_seams(monkeypatch, systems=[])
    assert orch.build_graph_first_seed() is None


def test_seed_none_when_graph_read_fails(monkeypatch):
    """A graph read failure is SWALLOWED — build_graph_first_seed returns None so the
    caller proceeds without the seed (a query must never break on a graph failure)."""
    def _boom(*_a, **_k):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(orch, "_explore_graph", lambda cypher: json.dumps({"results": []}))
    monkeypatch.setattr("agents.db.neptune_query", _boom)
    assert orch.build_graph_first_seed() is None


def test_seed_none_when_enrichment_helper_raises(monkeypatch):
    """If the enrichment helper itself raises, the seed builder swallows and returns
    None (fail-safe over the whole path, not just the inner best-effort loop)."""
    def _explode(_cypher):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch, "_explore_graph_with_schema", _explode)
    assert orch.build_graph_first_seed() is None


# --------------------------------------------------------------------------- #
# _graph_first_prelude — the Mode-gated runtime injection + visible step
# --------------------------------------------------------------------------- #
def test_prelude_mode_b_injects_and_emits_visible_step(monkeypatch):
    """Mode B: the prelude merges the seed into the user turn (prompt) and returns a
    VISIBLE explore_graph tool_use event so neptune_queries>=1 and the UI trace shows
    the graph-first step."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    _install_seams(
        monkeypatch,
        systems=[{"id": "sys-a", "name": "Alpha", "connection_type": "Athena"}],
    )
    augmented, events, planned_with = runtime_app._graph_first_prelude("How many open work orders?")
    # Seed merged into the user turn; the original question is preserved AFTER it.
    assert "routed_system_schemas" in augmented
    assert augmented.rstrip().endswith("How many open work orders?")
    assert planned_with is None  # planned_with is Mode-A-only (D3)
    # One VISIBLE explore_graph step (registers as a neptune query) plus its
    # immediate completion event so the UI trace never shows it in-flight.
    assert len(events) == 2
    ctu = events[0]["current_tool_use"]
    assert ctu["name"] == "explore_graph"
    assert ctu["toolUseId"] == orch.GRAPH_FIRST_SEED_TOOL_ID
    done = events[1]["message"]["content"][0]["toolResult"]
    assert done["toolUseId"] == orch.GRAPH_FIRST_SEED_TOOL_ID
    assert done["status"] == "success"


def test_prelude_mode_a_injects_route_seed_and_emits_visible_step(monkeypatch):
    """Mode A: the prelude merges the schema-free route seed into the user turn and
    emits the same visible graph-routing step used by Mode B."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    seen: dict = {}

    def fake_seed(candidate=None, question=None):
        seen["question"] = question
        return orch._ROUTE_SEED_HEADER + '{"systems": [{"id": "sys-a"}]}'
    monkeypatch.setattr(orch, "build_route_seed", fake_seed)
    prompt = "How many open work orders?"
    augmented, events, _planned = runtime_app._graph_first_prelude(prompt)
    assert seen["question"] == prompt  # the seed translates identifiers in THIS question
    assert augmented.startswith(orch._ROUTE_SEED_HEADER)
    assert augmented.rstrip().endswith(prompt)
    assert "routed_system_schemas" not in augmented
    assert len(events) == 2
    ctu = events[0]["current_tool_use"]
    assert ctu["name"] == "explore_graph"
    assert ctu["toolUseId"] == orch.ROUTE_SEED_TOOL_ID
    done = events[1]["message"]["content"][0]["toolResult"]
    assert done["toolUseId"] == orch.ROUTE_SEED_TOOL_ID
    assert done["status"] == "success"


def test_prelude_mode_unset_defaults_to_mode_a_route_seed(monkeypatch):
    """Unset SYSTEM_QUERY_MODE defaults to Mode A and injects its route seed."""
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    monkeypatch.setattr(orch, "build_route_seed", lambda candidate=None, question=None: "ROUTE-SEED")
    prompt = "hello"
    augmented, events, _planned = runtime_app._graph_first_prelude(prompt)
    assert augmented == "ROUTE-SEED\n\n---\n\nhello"
    assert events[0]["current_tool_use"]["toolUseId"] == orch.ROUTE_SEED_TOOL_ID


def test_prelude_mode_b_no_seed_returns_prompt_unchanged(monkeypatch):
    """Mode B but the builder returns None (no systems / failure) ⇒ prompt unchanged,
    no visible step — the query proceeds without the seed (fail-safe)."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setattr(orch, "build_graph_first_seed", lambda: None)
    prompt = "hello"
    assert runtime_app._graph_first_prelude(prompt) == (prompt, [], None)


def test_prelude_swallows_builder_exception(monkeypatch):
    """Mode B and the builder RAISES ⇒ the prelude swallows and returns the prompt
    unchanged (a graph failure never breaks the query)."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")

    def _boom():
        raise RuntimeError("neptune down")

    monkeypatch.setattr(orch, "build_graph_first_seed", _boom)
    prompt = "hello"
    assert runtime_app._graph_first_prelude(prompt) == (prompt, [], None)
