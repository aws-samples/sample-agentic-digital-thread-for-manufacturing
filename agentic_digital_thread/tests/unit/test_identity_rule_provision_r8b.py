# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 8b: what 7d0e6784's wait for a target's schema rewrite still got wrong.

* The mark and the field's concepts were two reads. A rewrite that began
  between them (no mark yet, then the deleted schema) still downgraded a
  measured rule, and both reads degraded a graph failure to ``[]``: an
  unreadable graph read as "no rewrite" and as an empty concept set. They
  are now one checked read of one snapshot, which tells "field not in the
  graph", "no concepts" and "being rewritten" apart, and a failed read is
  "could not run".
* The bound counted sleeps, not time: the reads did not count, and the
  synchronous ``/api/onboard/provision/confirm`` route (behind a 120 s edge
  timeout) could wait the whole 300 s. The wait is now one monotonic deadline
  per provision run that the reads count against, the confirm route waits
  for nothing, and the seed harness polls a provision for the wait too.
* A stale mark was ignored but never removed, and every whole-node read
  showed it. A reader now removes the exact stale value it saw, and the
  graph view, node details, ``explore_graph`` and graph snapshots never carry
  the property.

The world is ``_schema_rewrite_world`` (neutral systems, fake graph and clock).
"""
from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import time
from pathlib import Path

import pytest

import agents.onboard as onboard
from agents.identity_affinity import AFFINITY_CONFIDENCE, EXACT_METHOD
from tests.unit._schema_rewrite_world import (  # noqa: F401 - world is a fixture
    MARK, SRC_MAPPINGS, SRC_SYS, TARGET_FIELD, TGT_SYS, provision_body, provision_rules,
    resolve_rules, target_field_reads, target_rewrite_starts, the_node, world)


def _downgraded_with(state, ctx, reason: str) -> None:
    node = the_node(state)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = ctx.rules_unchecked
    assert reason in entry["reason"], entry["reason"]


# ── 1. one snapshot, read checked ───────────────────────────────────────────

def test_a_rewrite_that_begins_between_the_mark_and_the_concepts_cannot_downgrade(world):
    """REPRODUCTION (the reviewer's interleaving). The target is not marked
    when the mark is read; its re-provision then marks it and deletes its
    schema before the concepts are read. 7d0e6784 read the two separately and
    stored provision-request. One snapshot holds the field as the target's
    earlier provision left it, so the rule keeps its measured method."""
    rules = resolve_rules()
    world.graph.after_mark_read = target_rewrite_starts
    ctx = provision_rules(rules)
    assert world.graph.marks.get(TGT_SYS) is not None       # the rewrite did begin
    assert ctx.rules_unchecked == []
    assert (the_node(world)["method"], the_node(world)["confidence"]) == (EXACT_METHOD,
                                                                           AFFINITY_CONFIDENCE)


def test_a_graph_read_that_fails_is_could_not_run_not_an_empty_concept_set(world):
    """REPRODUCTION. The target-field read fails. 7d0e6784 read it through
    ``neptune_query``, which answers ``[]``: no rewrite, no concept, "found no
    concept both fields map to". It is now a checked read, so the reason says
    the graph could not be read; nothing waits."""
    rules = resolve_rules()
    world.graph.fail_reads = target_field_reads
    ctx = provision_rules(rules)
    _downgraded_with(world, ctx, f"could not run: the graph could not be read for {TGT_SYS} "
                                 "lot_lines.lot_ref")
    assert world.sleeps == []


def test_a_target_field_missing_from_the_graph_is_told_apart_from_no_concepts(world):
    """REPRODUCTION. The target field is not in the graph at all (and its
    system is not rewriting). 7d0e6784 read that as an empty concept set."""
    rules = resolve_rules()
    world.graph.fields.pop(TARGET_FIELD)
    ctx = provision_rules(rules)
    _downgraded_with(world, ctx, f"could not run: {TGT_SYS} lot_lines.lot_ref is not in the graph")
    assert world.sleeps == []


def test_the_snapshot_is_one_read_that_returns_a_row_without_edges():
    """The read is one statement that keeps the System row when the field has
    no MAPS_TO edge (both are OPTIONAL) and returns the mark beside them."""
    cypher = onboard._TARGET_FIELD_SNAPSHOT_CYPHER
    assert cypher.startswith("MATCH (s:System {id: $sid}) OPTIONAL MATCH (s)-[:HAS_SCHEMA]->")
    assert "OPTIONAL MATCH (f)-[r:MAPS_TO]->(c:Concept)" in cypher
    assert "s.schema_rewrite_started AS started" in cypher and "count(DISTINCT f) AS fields" in cypher


# ── 2. one wall-clock deadline; the synchronous route waits for nothing ─────

def test_the_wait_is_a_wall_clock_deadline_that_counts_read_time(world, monkeypatch):
    """REPRODUCTION. Each read of the target takes 20 s and the budget is
    60 s. 7d0e6784 counted only its 3 s sleeps (and read its 300 s budget once,
    at import), so it slept a hundred times and waited far past the bound.
    The deadline is on the clock: the wait ends within one read of it."""
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_WAIT_S", 60)
    rules = resolve_rules()
    target_rewrite_starts()
    world.graph.read_cost = 20.0
    began = world.now
    ctx = provision_rules(rules)
    assert world.now - began <= 60 + 20
    _downgraded_with(world, ctx, f"the schema of '{TGT_SYS}' is being rewritten by its own provision")


def test_one_deadline_for_every_rule_of_a_provision_run(world, monkeypatch):
    """REPRODUCTION. Two measured rules to a target that stays marked: both
    stop at the run's one deadline, not one bound each."""
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_WAIT_S", 60)
    rule = resolve_rules()[0]
    target_rewrite_starts()
    began = world.now
    ctx = provision_rules([rule, {**rule, "reasoning": rule["reasoning"] + " "}])
    assert world.now - began <= 60
    assert [("being rewritten" in e["reason"]) for e in ctx.rules_unchecked] == [True, True]


@pytest.fixture
def confirm_route(monkeypatch):
    """The confirm route around a stand-in ``_do_provision`` (its own
    provenance check and claim are not what this is about)."""
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_claim_provision", lambda _sid: contextlib.nullcontext())
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", lambda *_a: None)

    def run(do_provision, rules=()):
        monkeypatch.setattr(onboard, "_do_provision", do_provision)
        body = provision_body(SRC_SYS, SRC_MAPPINGS, rules).model_dump(by_alias=True)
        return onboard.onboard_provision_confirm({"provision_body": body})
    return run


def _budget_seen(body) -> dict:
    return {"budget": getattr(onboard._make_provision_state(body), "schema_wait_budget_s", None)}


def test_the_confirm_route_has_no_wait_budget_and_the_job_has_the_full_one(world, confirm_route,
                                                                          monkeypatch):
    """REPRODUCTION. The synchronous confirm route answers within one request
    behind 120 s edge timeouts: it gets a zero budget, and only for that
    request. The provision job (the wizard's ``POST /provision``, the chat's
    job and the seed's) gets the full one."""
    assert confirm_route(_budget_seen)["budget"] == 0
    assert _budget_seen(provision_body(SRC_SYS, SRC_MAPPINGS))["budget"] == \
        onboard._SCHEMA_REWRITE_WAIT_S                       # the same thread, after the confirm
    from agents import coverage_gap_analyzer, schema_drift
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift", lambda *_a: None)
    monkeypatch.setattr(coverage_gap_analyzer, "run_coverage_gap_check", lambda *_a: None)
    monkeypatch.setattr(onboard, "_post_provision_mining", lambda _sid: None)
    monkeypatch.setattr(onboard, "_post_provision_schema_shape", lambda *_a: None)
    seen = {}
    monkeypatch.setattr(onboard, "_do_provision", lambda body: seen.update(_budget_seen(body)) or {})
    onboard._run_provision_job_steps("job-1", provision_body(SRC_SYS, SRC_MAPPINGS))
    assert seen["budget"] == onboard._SCHEMA_REWRITE_WAIT_S


def test_the_confirm_route_reads_a_marked_target_once_and_does_not_wait(world, confirm_route):
    """REPRODUCTION. Through the confirm route a marked target is read once:
    no sleep, the method not earned with the rewrite as the reason (before
    7d0e6784 the same rule was refused as "found no concept"; 7d0e6784 waited
    up to 300 s inside the request)."""
    rules = resolve_rules()
    target_rewrite_starts()
    seen = {}

    def identity_rules_only(body):
        ctx = onboard._make_provision_state(body)
        ctx.tables_seen = {"runs"}
        onboard._dp_identity_rules(ctx)
        seen["ctx"] = ctx
        return {}
    confirm_route(identity_rules_only, rules)
    assert world.sleeps == []
    (entry,) = seen["ctx"].rules_unchecked
    assert f"the schema of '{TGT_SYS}' is being rewritten by its own provision" in entry["reason"]


def test_the_seed_harness_polls_a_provision_for_the_wait_too():
    """REPRODUCTION. The seed's harness waits for a provision by polling the
    catalog (``onboarding_smoketest.provision``, 720 s before this change).
    In the seed's first lap that flip comes after the identity-rule stage, so
    the poll now covers the wait on top of the 720 s it had."""
    path = Path(__file__).resolve().parents[3] / "scripts" / "onboarding_smoketest.py"
    spec = importlib.util.spec_from_file_location("onboarding_smoketest_r8b", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    default = inspect.signature(smoke.provision).parameters["max_s"].default
    assert default >= 720 + onboard._SCHEMA_REWRITE_WAIT_S


# ── 3. a stale mark is removed, and never shown ─────────────────────────────

def test_a_stale_mark_is_removed_with_a_compare_and_clear_on_its_value(world):
    """REPRODUCTION. A mark older than the staleness bound (a provision that
    stopped between b0 and the clear). 7d0e6784 ignored it and left it on the
    node for good. The reader removes exactly the value it saw."""
    rules = resolve_rules()
    world.graph.marks[TGT_SYS] = stale = 1_000                 # epoch ms, long ago
    provision_rules(rules)
    assert TGT_SYS not in world.graph.marks
    (clear,) = [(c, p) for c, p in world.graph.mark_writes if "REMOVE" in c]
    assert "WHERE s.schema_rewrite_started = $started" in clear[0]
    assert clear[1] == {"sid": TGT_SYS, "started": stale}
    assert the_node(world)["method"] == EXACT_METHOD


def test_a_newer_mark_written_after_the_stale_read_is_kept(world):
    """Guard: the target starts a new rewrite after the reader saw the stale
    value; the compare-and-clear names the stale value, so the new mark stays."""
    rules = resolve_rules()
    world.graph.marks[TGT_SYS] = 1_000
    fresh = int(time.time() * 1000)
    world.graph.after_mark_read = lambda: world.graph.marks.__setitem__(TGT_SYS, fresh)
    provision_rules(rules)
    assert world.graph.marks[TGT_SYS] == fresh


_SYSTEM_NODE = {"~id": "n1", "~labels": ["System"],
                "~properties": {"id": TGT_SYS, "name": "Ledger", MARK: 1_792_577_321_000}}


def test_the_graph_view_never_shows_the_mark():
    """REPRODUCTION: ``/api/neptune/graph`` rendered every System property."""
    from agents import server
    nodes, _ids = server._graph_view_shape_nodes([{"n": dict(_SYSTEM_NODE)}])
    assert MARK not in nodes[0]["props"] and nodes[0]["props"]["id"] == TGT_SYS


def test_node_details_never_show_the_mark(monkeypatch):
    """REPRODUCTION: the node-detail panel (``GET /api/neptune/node/{id}/neighbors``)
    returned every property."""
    import asyncio
    from agents import server
    monkeypatch.setattr(server, "neptune_query", lambda cypher, params=None: (
        [{"n": dict(_SYSTEM_NODE)}] if "RETURN n" in cypher else
        [{"rel": "LOCATED_AT", "m": dict(_SYSTEM_NODE)}]))
    details = asyncio.run(server.neptune_node_neighbors_route("n1"))
    assert MARK not in details["node"]["properties"]
    assert MARK not in json.dumps(details)


def test_explore_graph_never_returns_the_mark(monkeypatch):
    """REPRODUCTION: the chat's ``explore_graph`` returned whole System nodes
    and ``properties(s)`` maps as the graph held them."""
    from agents import graph_tools
    rows = [{"s": dict(_SYSTEM_NODE)}, {"p": dict(_SYSTEM_NODE["~properties"])}]
    monkeypatch.setattr(graph_tools, "neptune_query_checked",
                        lambda cypher, params=None: rows if "MATCH (s:System)" in cypher else [])
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: [])
    out = graph_tools.explore_graph("MATCH (s:System) RETURN s, properties(s) AS p")
    assert MARK not in out and TGT_SYS in out


def test_a_graph_snapshot_neither_exports_nor_restores_the_mark():
    """REPRODUCTION: snapshot export copied every property, and a restore
    wrote back whatever the snapshot held."""
    from agents import graph_snapshot
    (item,) = graph_snapshot._node_page(lambda _cypher: [{"n": dict(_SYSTEM_NODE)}], 0, 10)
    assert MARK not in item["props"]
    written = []
    graph_snapshot._create_nodes(lambda cypher, params=None: written.append(params) or True,
                                 [{"labels": ["System"], "props": dict(_SYSTEM_NODE["~properties"])}],
                                 10)
    assert written and all(MARK not in row["props"] for p in written for row in p["rows"])
