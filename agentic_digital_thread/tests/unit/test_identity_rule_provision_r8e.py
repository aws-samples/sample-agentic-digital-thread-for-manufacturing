# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 8e: what 211ff791's start mark, landing and query precheck got wrong.

* The start mark became best-effort: a refused mark write was logged and b0
  went on to delete the schema, so the whole b0..b3b window ran unmarked and
  a peer could again measure a field with no concepts. b0 now deletes
  nothing unless the mark write returned this run's token; otherwise the
  provision fails before the delete and leaves no marker (nothing was
  touched).
* The emergent landing ran with best-effort writes and reported nothing, so
  a refused write left a concept missing and the window was still declared
  settled. The landing now says whether every write landed; it is retried
  inside the window, and a landing that still did not land leaves the
  failed marker and fails the provision with a retryable error.
* The computed-subscript refusal blocked ordinary list queries (``ns[i]``,
  ``collect(x)[i]``, ``names[0..n]``). It is gone; the internal-name check
  and the output filter stay.

The world is ``_schema_rewrite_world`` (neutral systems, fake graph and clock).
"""
from __future__ import annotations

import json

import pytest

import agents.onboard as onboard
from agents import graph_tools
from agents.connection_types import MCP_SERVER
from agents.identity_affinity import AFFINITY_CONFIDENCE, EXACT_METHOD
from agents.models import ProvisionRequest
from tests.unit._schema_rewrite_world import (  # noqa: F401 - world is a fixture
    SRC_MAPPINGS, SRC_SYS, SYSTEMS, TARGET_FIELD, TGT_CONCEPT, TGT_MAPPINGS, TGT_SYS,
    resolve_rules, schema_of, target_rewrite_starts, the_node, world)

UNSETTLED = f"the schema of '{TGT_SYS}' was left unsettled by its own provision"
#: A non-core concept the graph's vocabulary does not list: b2 leaves it out,
#: only the emergent landing of a telemetry-rich system writes it.
EMERGENT = "trace-key"
EMERGENT_FIELD = (TGT_SYS, "lot_lines", "trace_code")
TELEMETRY_MAPPINGS = [*TGT_MAPPINGS, {
    "table": "lot_lines", "field": "lot_lines.trace_code", "concept": EMERGENT, "confidence": 0.9,
    "mappings": [{"concept": EMERGENT, "role": "identifier", "confidence": 0.9}]}]
_MARK_PREFIX = "MATCH (s:System {id: $sid}) SET s.schema_rewrite_started"

#: Every coordinator phase but b0, b2, b3b, b3c: the target's re-provision
#: writes its schema for real; ``_dp_valueoverlap_joinpaths`` (b4, the first
#: step after the window) is where a peer's provision reads.
_STUBBED = ("_dp_connection_phase", "_dp_discover_athena_physical", "_dp_athena_physical_fields",
            "_dp_discovery_completeness", "_dp_reapply_user_corrections", "_dp_identity_key_edges",
            "_dp_mint_unmapped", "_dp_internal_joinpaths", "_dp_mirrored_measure_tags",
            "_dp_samerow_joinpaths", "_dp_identity_rules", "_dp_rule_joinpaths",
            "_dp_cross_system_vms", "_dp_equipment_vms_legacy", "_dp_authority_rules",
            "_dp_onboarding_log", "_dp_finalize", "_dp_start_pg_agent",
            "_provision_i3x_post_pass", "_provision_knowledge_post_pass")
_REAL_IDENTITY_RULES = onboard._dp_identity_rules


def _peer_provision(rules):
    """The source system's provision (the reader): its identity-rule phase."""
    ctx = onboard._make_provision_state(ProvisionRequest(
        system_id=SRC_SYS, system_name=SRC_SYS, schema=schema_of(SRC_SYS),
        plant=SYSTEMS[SRC_SYS]["plant"], rules=[dict(r) for r in rules],
        mappings=[dict(m) for m in SRC_MAPPINGS], connection_type="PostgreSQL"))
    ctx.tables_seen = {"runs"}
    _REAL_IDENTITY_RULES(ctx)
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    return ctx


def _target_reprovision(monkeypatch, *, mappings=TGT_MAPPINGS, connection_type="PostgreSQL",
                        peer_rules=None):
    """The target system's re-provision through ``_do_provision_writes``; with
    ``peer_rules`` the source's provision reads at b4. Returns ``(result,
    peer ctx)``; a raised error is returned as ``{"raised": message}``."""
    for name in _STUBBED:
        monkeypatch.setattr(onboard, name, lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_dp_collect_tables",
                        lambda ctx: ctx.tables_seen.update(m["table"] for m in mappings))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    monkeypatch.setattr("agents.asset_role_pass.run_after_provision", lambda: None)
    seen: dict = {}

    def at_b4(_ctx):
        if peer_rules is not None:
            seen["peer"] = _peer_provision(peer_rules)
    monkeypatch.setattr(onboard, "_dp_valueoverlap_joinpaths", at_b4)
    body = ProvisionRequest(system_id=TGT_SYS, system_name=TGT_SYS, schema=schema_of(TGT_SYS),
                            plant=SYSTEMS[TGT_SYS]["plant"], mappings=[dict(m) for m in mappings],
                            connection_type=connection_type)
    try:
        result = onboard._do_provision_writes(body)
    except RuntimeError as exc:
        result = {"raised": str(exc)}
    return result, seen.get("peer")


# ── 1. b0 deletes nothing unless its mark is confirmed ─────────────────────

def test_a_refused_mark_stops_the_provision_before_the_schema_delete(world, monkeypatch):
    """REPRODUCTION. The mark write is refused. 211ff791 logged it and b0
    deleted the schema anyway, so the rewrite ran unmarked. Now the provision
    fails before the delete: the target's field is still there with its
    concept, and no marker is left (nothing was touched)."""
    world.graph.fail_writes = lambda cypher: cypher.startswith(_MARK_PREFIX)
    result, _peer = _target_reprovision(monkeypatch)
    assert "schema-rewrite mark" in result.get("raised", ""), result
    assert world.graph.fields[TARGET_FIELD] == [(TGT_CONCEPT, "identifier", 0.95)]
    assert TGT_SYS not in world.graph.marks and TGT_SYS not in world.graph.failed


@pytest.mark.parametrize("answer", [[], [{"started": 1}], None],
                         ids=["no-system-node", "another-value", "refused"])
def test_a_mark_the_write_does_not_return_stops_b0_before_the_delete(world, monkeypatch, answer):
    """REPRODUCTION: the mark counts only when the write returns this run's
    token (it matched the System node and set the value). 211ff791 never
    checked; b0 deleted the schema."""
    real = onboard.neptune_write_returning
    monkeypatch.setattr(onboard, "neptune_write_returning",
                        lambda c, p=None: answer if c.startswith(_MARK_PREFIX) else real(c, p))
    with pytest.raises(RuntimeError, match="schema-rewrite mark"):
        target_rewrite_starts()
    assert TARGET_FIELD in world.graph.fields


def test_a_confirmed_mark_still_comes_before_the_delete(world, monkeypatch):
    """Guard: the mark is confirmed first, then the schema is deleted."""
    order: list = []
    real_delete = onboard._delete_stale_schema_subgraph
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph",
                        lambda sid: order.append(("delete", dict(world.graph.marks))) or
                        real_delete(sid))
    target_rewrite_starts()
    ((step, marks),) = order
    assert step == "delete" and TGT_SYS in marks


# ── 2. a landing that did not land is never settled ────────────────────────

def _landing_refused(monkeypatch, world, times: int) -> dict:
    """The landing's MAPS_TO write of the emergent concept is refused
    ``times`` times, then lands. Returns the attempt count."""
    seen = {"attempts": 0}
    write = world.graph.write

    def refusing(cypher, params=None):
        if "MERGE (f)-[r:MAPS_TO]->(c)" in cypher and (params or {}).get("cid") == EMERGENT:
            seen["attempts"] += 1
            if seen["attempts"] <= times:
                return False
        return write(cypher, params)
    monkeypatch.setattr(onboard, "neptune_write", refusing)
    return seen


def test_a_landing_write_refused_once_is_retried_inside_the_window(world, monkeypatch):
    """REPRODUCTION. One landing write is refused. 211ff791 ignored it and
    declared the window settled with the emergent concept missing. Now the
    landing is retried inside the window: the concept is there when the
    window ends, the provision succeeds, and a peer measuring at b4 sees the
    settled fields."""
    rules = resolve_rules()
    seen = _landing_refused(monkeypatch, world, 1)
    result, peer = _target_reprovision(monkeypatch, mappings=TELEMETRY_MAPPINGS,
                                       connection_type=MCP_SERVER, peer_rules=rules)
    assert result.get("status") == "provisioned", result
    assert world.graph.fields[EMERGENT_FIELD] == [(EMERGENT, "identifier", 0.9)]
    assert seen["attempts"] == 2
    assert TGT_SYS not in world.graph.marks and TGT_SYS not in world.graph.failed
    assert peer.rules_unchecked == []
    assert (the_node(world)["method"], the_node(world)["confidence"]) == (EXACT_METHOD,
                                                                           AFFINITY_CONFIDENCE)


def test_a_landing_that_keeps_failing_leaves_the_failed_marker_and_a_retryable_error(
        world, monkeypatch):
    """REPRODUCTION. Every landing attempt is refused. 211ff791 reported the
    provision as provisioned with the concept missing. Now the window ends
    with the failed marker (a peer's re-measurement could not run) and the
    provision fails with a retryable error."""
    rules = resolve_rules()
    _landing_refused(monkeypatch, world, 10_000)
    result, peer = _target_reprovision(monkeypatch, mappings=TELEMETRY_MAPPINGS,
                                       connection_type=MCP_SERVER, peer_rules=rules)
    assert result["reason"] == "emergent_concepts_not_landed"
    record = onboard._provision_error_record(result)
    assert record["retryable"] is True and record["status"] == "error"
    assert TGT_SYS not in world.graph.marks and TGT_SYS in world.graph.failed
    (entry,) = peer.rules_unchecked
    assert f"could not run: {UNSETTLED}" in entry["reason"]


def test_the_landing_reports_whether_every_write_landed(monkeypatch):
    """REPRODUCTION: ``_land_emergent_concepts`` returned nothing; it now
    answers True only when every Concept and MAPS_TO write landed."""
    monkeypatch.setattr(onboard.self_learning, "accepted_user_concept_mappings", lambda _sid: {})
    rows = [{"table": "t", "field": "t.c", "concept": EMERGENT,
             "mappings": [{"concept": EMERGENT, "role": "identifier", "confidence": 0.9}]}]
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: True)
    assert onboard._land_emergent_concepts("sys-x", "", rows) is True
    for refused in ("MERGE (c:Concept", "MERGE (f)-[r:MAPS_TO]->(c)"):
        monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None, _r=refused: _r not in c)
        assert onboard._land_emergent_concepts("sys-x", "", rows) is False


# ── 3. list subscripts in model queries run ────────────────────────────────

@pytest.mark.parametrize("cypher", [
    "MATCH p = (a:Asset)-[:HAS_PARENT*1..3]->(b) WITH nodes(p) AS ns "
    "UNWIND range(0, size(ns) - 2) AS i RETURN ns[i].name AS child, ns[i + 1].name AS parent",
    "MATCH (a:Asset) WITH collect(a.name) AS names, count(a) AS n RETURN names[n - 1] AS last",
    "MATCH (a:Asset) WITH collect(a.name) AS names, count(a) AS n RETURN names[0..n] AS head",
    "MATCH (a:Asset) RETURN labels(a)[0] AS label",
], ids=["path-pairs", "collect-index", "variable-slice", "literal-index"])
def test_explore_graph_runs_list_queries_with_computed_indexes(cypher, monkeypatch):
    """REPRODUCTION: 211ff791 refused the first three as computed subscripts."""
    ran = []
    monkeypatch.setattr(graph_tools, "neptune_query_checked",
                        lambda c, p=None: ran.append(c) or [{"child": "x"}])
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: [])
    out = json.loads(graph_tools.explore_graph(cypher))
    assert ran[:1] == [cypher] and not out.get("query_failed")


def test_explore_graph_still_refuses_a_query_naming_the_internal_mark(monkeypatch):
    """Guard: the internal-name check stays."""
    ran = []
    monkeypatch.setattr(graph_tools, "neptune_query_checked", lambda c, p=None: ran.append(c) or [])
    out = json.loads(graph_tools.explore_graph(
        "MATCH (s:System) RETURN s.schema_rewrite_failed AS failed"))
    assert ran == [] and out["query_failed"] is True
