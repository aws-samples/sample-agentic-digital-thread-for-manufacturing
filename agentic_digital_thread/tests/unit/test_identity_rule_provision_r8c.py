# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 8c: what 1a1fb7ca's schema-rewrite mark and query precheck still missed.

* The mark was cleared after b3a, but two later steps still change a field's
  concepts: the telemetry-rich emergent-concept landing adds non-core
  concepts b2 leaves out, and the retired-link sweep removes links b2 wrote
  back. A reader between the clear and those steps saw a concept set that was
  about to change: it downgraded a measured rule whose concept was about to
  be landed, or earned a method from a link the steward had retired. Both
  steps now run inside the marked window (b3b), and the mark is cleared after
  them.
* ``explore_graph`` stripped the mark from whole nodes and property maps, but
  a scalar projection (``RETURN s.schema_rewrite_started AS started``) served
  it under another name. A query that names an internal property, in any
  spelling, is now refused before it runs.

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
    resolve_rules, schema_of, the_node, world)

#: A non-core concept both columns share: b2 writes it only when the graph's
#: vocabulary already lists it, the emergent landing of a telemetry-rich
#: system writes it either way.
EMERGENT = "trace-key"


def _row(table, column, concept) -> dict:
    return {"table": table, "field": f"{table}.{column}", "concept": concept, "confidence": 0.95,
            "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.95}]}


EMERGENT_SRC = [_row("runs", "run_code", EMERGENT)]
EMERGENT_TGT = [_row("lot_lines", "lot_ref", EMERGENT)]

#: Every coordinator phase but the schema ones (b0, b2) and the steps this
#: round is about: the target's re-provision writes its schema for real and
#: nothing else. ``_dp_settle_field_concepts`` is absent on 1a1fb7ca.
_STUBBED = ("_dp_connection_phase", "_dp_discover_athena_physical", "_dp_athena_physical_fields",
            "_dp_discovery_completeness", "_dp_reapply_user_corrections", "_dp_identity_key_edges",
            "_dp_mint_unmapped", "_dp_internal_joinpaths", "_dp_mirrored_measure_tags",
            "_dp_valueoverlap_joinpaths", "_dp_samerow_joinpaths", "_dp_identity_rules",
            "_dp_rule_joinpaths", "_dp_cross_system_vms", "_dp_equipment_vms_legacy",
            "_dp_authority_rules", "_dp_onboarding_log", "_dp_finalize", "_dp_start_pg_agent",
            "_provision_i3x_post_pass", "_provision_knowledge_post_pass")


def _target_reprovision(monkeypatch, mappings, connection_type="PostgreSQL"):
    """The target system's re-provision through ``_do_provision_writes``."""
    for name in _STUBBED:
        monkeypatch.setattr(onboard, name, lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_dp_collect_tables",
                        lambda ctx: ctx.tables_seen.update(m["table"] for m in mappings))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    monkeypatch.setattr("agents.asset_role_pass.run_after_provision", lambda: None)
    body = ProvisionRequest(system_id=TGT_SYS, system_name=TGT_SYS, schema=schema_of(TGT_SYS),
                            plant=SYSTEMS[TGT_SYS]["plant"], mappings=[dict(m) for m in mappings],
                            connection_type=connection_type)
    return onboard._do_provision_writes(body)


def _source_reads_during(monkeypatch, world, step: str, rules, mappings) -> dict:
    """Run the source system's provision (the reader) at the moment the
    target's re-provision reaches ``step``, the reader's first wait letting
    the target finish that step and clear its mark. Returns the reader's
    state under ``ctx``. Call it before :func:`_target_reprovision` stubs the
    target's other phases (the reader runs the real identity-rule phase)."""
    real = getattr(onboard, step)
    identity_rules = onboard._dp_identity_rules          # the target's own run stubs it
    seen: dict = {}

    def target_finishes(_n):
        if "result" not in seen:
            seen["result"] = real(*seen["args"])
            world.graph.marks.pop(TGT_SYS, None)             # the target's run settles

    def wrapped(*args):
        if args and args[0] == TGT_SYS and "ctx" not in seen:
            seen["args"] = args
            world.on_sleep = target_finishes
            ctx = onboard._make_provision_state(ProvisionRequest(
                system_id=SRC_SYS, system_name=SRC_SYS, schema=schema_of(SRC_SYS),
                plant=SYSTEMS[SRC_SYS]["plant"], rules=[dict(r) for r in rules],
                mappings=[dict(m) for m in mappings], connection_type="PostgreSQL"))
            ctx.tables_seen = {m["table"] for m in mappings}
            identity_rules(ctx)
            onboard._stamp_identity_rule_provenance(SRC_SYS)
            seen["ctx"], world.on_sleep = ctx, None
        if "result" not in seen:
            seen["result"] = real(*args)
            return seen["result"]
        return real(*args) if step == "_sweep_retired_concept_links" else None
    monkeypatch.setattr(onboard, step, wrapped)
    return seen


# ── 1. the mark covers the last concept writes ──────────────────────────────

def test_a_concept_landed_after_the_old_clear_point_is_waited_for(world, monkeypatch):
    """REPRODUCTION (add after clear). The target is telemetry-rich, its field
    maps a non-core concept the graph's vocabulary read does not list (a
    concept read that fell back to the core set), so b2 leaves it out and
    only the emergent landing writes it back. 1a1fb7ca landed it after the
    mark was cleared: a reader in between saw the field with no concept and
    stored the measured rule as provision-request. Now the landing runs
    inside the marked window, so the reader waits and keeps the method."""
    world.graph.add_field(*TARGET_FIELD, (EMERGENT, "identifier", 0.95))
    rules = _resolve(EMERGENT_SRC)
    seen = _source_reads_during(monkeypatch, world, "_land_emergent_concepts", rules, EMERGENT_SRC)
    _target_reprovision(monkeypatch, EMERGENT_TGT, connection_type=MCP_SERVER)
    assert seen["ctx"].rules_unchecked == []
    assert (the_node(world)["method"], the_node(world)["confidence"]) == (EXACT_METHOD,
                                                                           AFFINITY_CONFIDENCE)
    assert world.sleeps, "the reader waited for the target's rewrite"
    assert world.graph.fields[TARGET_FIELD] == [(EMERGENT, "identifier", 0.95)]


def test_a_link_the_steward_retired_is_not_earned_from_before_the_sweep(world, monkeypatch):
    """REPRODUCTION (remove after clear). The steward retired the target
    field's concept link after Resolve measured the rule; b2 writes the link
    back and the sweep removes it. 1a1fb7ca swept after the mark was cleared:
    a reader in between earned value_exact_match from the retired link. Now
    the sweep runs inside the marked window: the reader waits, finds no
    concept, and the method is not earned."""
    rules = resolve_rules()
    world.graph.fields[TARGET_FIELD] = []                   # the earlier sweep removed it
    retired = [{"concept": TGT_CONCEPT, "field": "lot_lines.lot_ref"}]
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links",
                        lambda sid: list(retired) if sid == TGT_SYS else [])
    seen = _source_reads_during(monkeypatch, world, "_sweep_retired_concept_links", rules,
                                SRC_MAPPINGS)
    assert _target_reprovision(monkeypatch, TGT_MAPPINGS)["status"] == "provisioned"
    node = the_node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = seen["ctx"].rules_unchecked
    assert "found no concept both fields map to" in entry["reason"]
    assert world.graph.fields[TARGET_FIELD] == []


def test_the_landing_and_the_sweep_run_inside_the_marked_window(world, monkeypatch):
    """REPRODUCTION (the order itself): the System node is still marked when
    the emergent landing and the retired-link sweep run, and is not marked
    once the provision returns. On 1a1fb7ca both ran after the clear."""
    marks: dict = {}
    for step in ("_land_emergent_concepts", "_sweep_retired_concept_links"):
        real = getattr(onboard, step)

        def spy(*args, _real=real, _step=step):
            marks.setdefault(_step, world.graph.marks.get(TGT_SYS))
            return _real(*args)
        monkeypatch.setattr(onboard, step, spy)
    _target_reprovision(monkeypatch, EMERGENT_TGT, connection_type=MCP_SERVER)
    assert marks["_land_emergent_concepts"] is not None
    assert marks["_sweep_retired_concept_links"] is not None
    assert TGT_SYS not in world.graph.marks


def test_a_failed_sweep_inside_the_window_still_fails_the_provision(world, monkeypatch):
    """Guard: a retired link whose removal cannot be read back fails the
    provision as before. The ordinary mark is gone (since round 8d the
    System node carries the failed marker instead)."""
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links",
                        lambda sid: [{"concept": TGT_CONCEPT, "field": "lot_lines.lot_ref"}]
                        if sid == TGT_SYS else [])
    world.graph.fail_reads = lambda cypher: cypher == onboard._RETIRED_LINK_COUNT_CYPHER
    result = _target_reprovision(monkeypatch, TGT_MAPPINGS)
    assert result["reason"] == "retired_link_sweep_failed"
    assert TGT_SYS not in world.graph.marks


def _resolve(mappings) -> list:
    from types import SimpleNamespace
    body = SimpleNamespace(system_id=SRC_SYS, mappings=[dict(m) for m in mappings])
    rules = onboard._augment_with_affinity_rules(body, {"rules": []})["rules"]
    assert [(r["method"], r["concept"]) for r in rules] == [(EXACT_METHOD, EMERGENT)]
    return rules


# ── 2. a model-written query never reads an internal property ──────────────

@pytest.mark.parametrize("cypher", [
    "MATCH (s:System) RETURN s.schema_rewrite_started AS started",
    "MATCH (s:System) WITH s AS sys RETURN sys.`schema_rewrite_started` AS since",
    "MATCH (s:System) WHERE s.schema_rewrite_started IS NOT NULL RETURN s.id AS id",
    "MATCH (s:System) RETURN s['schema_rewrite_started'] AS since",
    "MATCH (s:System) RETURN s['schema\\u005frewrite_started'] AS since",
], ids=["scalar-alias", "backtick-alias", "where", "subscript", "escaped-subscript"])
def test_explore_graph_refuses_a_query_that_names_an_internal_property(cypher, monkeypatch):
    """REPRODUCTION: 1a1fb7ca ran these and served the mark's epoch value
    under the query's own column name. The precheck refuses them before
    they run, with a plain error."""
    ran = []
    monkeypatch.setattr(graph_tools, "neptune_query_checked",
                        lambda c, p=None: ran.append(c) or [{"since": 1792577321000}])
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: [])
    out = json.loads(graph_tools.explore_graph(cypher))
    assert ran == []
    assert out["query_failed"] is True
    assert "schema_rewrite_started" in out["error"] and "not run" in out["error"]
    assert "1792577321000" not in json.dumps(out)


def test_explore_graph_runs_a_query_that_names_no_internal_property(monkeypatch):
    """Guard: a System query that names only data properties runs as before,
    including one whose names merely share words with the mark."""
    ran = []
    monkeypatch.setattr(graph_tools, "neptune_query_checked",
                        lambda c, p=None: ran.append(c) or [{"schema": "s1", "started": "x"}])
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_a, **_k: [])
    out = json.loads(graph_tools.explore_graph(
        "MATCH (s:System) RETURN s.schema AS schema, s.schema_version AS started"))
    assert len(ran) == 1 and out["results"][0]["schema"] == "s1"
