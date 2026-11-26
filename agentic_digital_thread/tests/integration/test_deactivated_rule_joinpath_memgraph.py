# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A deactivated IdentityRule's JoinPath is not served, on a real openCypher
engine (the local Memgraph backend, ``GRAPH_BACKEND=local``).

The live shape: onboarding wrote ``jp-<system>-<digest>`` and its rule
``ir-<system>-<digest>``; a steward deactivated the rule, the JoinPath kept no
status of its own and no ``rule_id``. These cases build that shape beside an
active rule's JoinPath, a JoinPath that came from no rule, a JoinPath that
names a retired rule by ``rule_id`` and an intra-system rule JoinPath, then
run each reader's Cypher unchanged: the route seed (and the model's own
fallback traversal, the same Cypher), the established-join gate, the resolve
hop, the dimension join, the self-map covering read, the impact and paths
views, the system Connections read, the provision read-back, the graph
summary and ``explore_graph``.
The deactivate/reactivate cascade runs on the same engine, including the
action run again on the live shape (it marks the JoinPath and changes nothing
else). ``explore_graph`` withholds by exact identity only: an active join
sharing a switched-off one's field names is returned, and a count is what the
query computed (the documented WHERE clause is what leaves rules out).

Skipped unless ``ADT_TEST_MEMGRAPH_URI`` names a THROWAWAY Memgraph (every
node in it is deleted) and the Bolt driver (``pip install .[local]``) is
importable. Run (from ``agentic_digital_thread/``)::

    docker run -d --rm --name adt-mg-test -p 127.0.0.1:57687:7687 \\
      memgraph/memgraph:3.12.0 --telemetry-enabled=false
    ADT_TEST_MEMGRAPH_URI=bolt://127.0.0.1:57687 \\
      PYTHONPATH=. python -m pytest tests/integration/test_deactivated_rule_joinpath_memgraph.py -q

GENERIC: synthetic systems only.
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")

A, B, C, D, E, F = "sys-a", "sys-b", "sys-c", "sys-d", "sys-e", "sys-f"
QUESTION = "How many records did site S1 complete?"

#: the live shape: rule deactivated, JoinPath with no status and no rule_id
DEAD_JP, DEAD_IR = f"jp-{A}-0123456789abcdef", f"ir-{A}-0123456789abcdef"
#: an active rule's JoinPath
LIVE_JP, LIVE_IR = f"jp-{A}-1111111111111111", f"ir-{A}-1111111111111111"
#: a JoinPath no rule made (an accepted proposal's subject id)
FREE_JP = "subj-2222222222222222"
#: a JoinPath naming a retired rule by rule_id (its id suffix names no rule)
RETIRED_JP, RETIRED_IR = f"jp-{A}-3333333333333333", "ir-elsewhere-retired"
#: an intra-system rule JoinPath whose rule is deactivated, and an internal one
INTRA_DEAD_JP, INTRA_DEAD_IR = f"jp-{F}-4444444444444444", f"ir-{F}-4444444444444444"
INTRA_FREE_JP = f"jp-internal-{F}-orders-item-items"


@pytest.fixture
def graph():
    if not _URI:
        pytest.skip("set ADT_TEST_MEMGRAPH_URI to a throwaway Memgraph to run these")
    neo4j = pytest.importorskip("neo4j")
    from agents.db import _to_plain
    driver = neo4j.GraphDatabase.driver(_URI, auth=("", ""))

    def run(cypher, params=None):
        with driver.session() as session:
            return [{k: _to_plain(v) for k, v in record.items()}
                    for record in session.run(cypher, **(params or {}))]

    run("MATCH (n) DETACH DELETE n")
    for sid in (A, B, C, D, E, F):
        run("CREATE (:System {id: $sid, name: toUpper($sid)})", {"sid": sid})
    run("CREATE (:Concept {id: 'line'})")
    _rule(run, DEAD_IR, A, "site_code", B, "line_name", status="deactivated")
    _rule(run, LIVE_IR, A, "lot", C, "lot_no", status=None)
    _rule(run, RETIRED_IR, A, "serial", E, "serial_no", status="retired")
    _rule(run, INTRA_DEAD_IR, F, "item", F, "item_ref", status="deactivated")
    _join(run, DEAD_JP, A, "schema_a.records", "site_code", B, ".hierarchy", "line_name")
    _join(run, LIVE_JP, A, "schema_a.records", "lot", C, "schema_c.lots", "lot_no",
          rule_id=LIVE_IR)
    _join(run, FREE_JP, A, "schema_a.records", "batch", D, "schema_d.batches", "batch_no",
          method="user_confirmed")
    _join(run, RETIRED_JP, A, "schema_a.records", "serial", E, "schema_e.units", "serial_no",
          rule_id=RETIRED_IR)
    _join(run, INTRA_DEAD_JP, F, "schema_f.orders", "item", F, "schema_f.items", "item_ref")
    _join(run, INTRA_FREE_JP, F, "schema_f.orders", "item", F, "schema_f.items", "item_id")
    yield run
    run("MATCH (n) DETACH DELETE n")
    driver.close()


def _rule(run, rid, src, src_field, tgt, tgt_field, *, status):
    run("MATCH (c:Concept {id: 'line'}) "
        "CREATE (ir:IdentityRule {id: $id, concept: 'line', source_system: $src, "
        "source_field: $sf, target_system: $tgt, target_field: $tf, source_pattern: '*', "
        "target_pattern: '*', method: 'onboarding-llm', confidence: 0.7})-[:LINKS_CONCEPT]->(c) "
        "WITH ir WHERE $status IS NOT NULL SET ir.status = $status, ir.deactivated_by = 'ADT'",
        {"id": rid, "src": src, "sf": src_field, "tgt": tgt, "tf": tgt_field, "status": status})


def _join(run, jid, frm, ftbl, ffld, to, ttbl, tfld, *, rule_id=None, method="foreign_key_match"):
    run("MATCH (f:System {id: $frm}), (t:System {id: $to}) "
        "CREATE (j:JoinPath {id: $id, from_system: $frm, from_table: $ftbl, from_field: $ffld, "
        "to_system: $to, to_table: $ttbl, to_field: $tfld, method: $m, transform: 'direct', "
        "description: $id}), (j)-[:FROM_SYSTEM]->(f), (j)-[:TO_SYSTEM]->(t) "
        "WITH j WHERE $rule_id IS NOT NULL SET j.rule_id = $rule_id",
        {"id": jid, "frm": frm, "ftbl": ftbl, "ffld": ffld, "to": to, "ttbl": ttbl,
         "tfld": tfld, "m": method, "rule_id": rule_id})


SERVED_FROM_A = {LIVE_JP, FREE_JP}
OFF = {DEAD_JP, RETIRED_JP, INTRA_DEAD_JP}


# --------------------------------------------------------------------------- #
# Route seed (and the model's fallback traversal: the same Cypher)
# --------------------------------------------------------------------------- #

def _seed_rows(run):
    import agents.orchestrator as orch
    return {row["system_id"]: row for row in run(orch._ROUTE_SEED_CYPHER)}


def test_route_seed_serves_no_joinpath_of_an_inactive_rule(graph):
    rows = _seed_rows(graph)
    assert set(rows) == {A, B, C, D, E, F}  # every system row survives
    assert {jp["id"] for jp in rows[A]["join_paths"]} == SERVED_FROM_A
    assert rows[B]["join_paths"] == []
    assert rows[E]["join_paths"] == []
    assert {jp["id"] for jp in rows[F]["join_paths"]} == {INTRA_FREE_JP}
    # The deactivated rule itself was already left out; nothing names it now.
    assert DEAD_IR not in {ir["id"] for ir in rows[A]["identity_rules"]}


def test_rendered_route_seed_never_names_the_switched_off_join(graph):
    import agents.orchestrator as orch
    seed = orch.build_route_seed(query=graph, question=QUESTION)
    assert seed
    assert DEAD_JP not in seed and RETIRED_JP not in seed
    assert "site_code" not in seed
    assert LIVE_JP in seed
    # The only steward-confirmed artifact in this graph is FREE_JP: nothing
    # the switched-off rule made is labelled confirmed.
    assert seed.count('"user_confirmed"') == 1, seed


def test_reactivating_the_rule_serves_the_joinpath_again(graph):
    graph("MATCH (ir:IdentityRule {id: $id}) SET ir.status = 'active'", {"id": DEAD_IR})
    assert DEAD_JP in {jp["id"] for jp in _seed_rows(graph)[A]["join_paths"]}


# --------------------------------------------------------------------------- #
# The direct readers
# --------------------------------------------------------------------------- #

def test_established_join_gate(graph, monkeypatch):
    from agents import join_discovery as jd
    monkeypatch.setattr(jd, "_neptune_query", graph)
    assert jd._has_established_join(A, B) is False
    assert jd._has_established_join(A, E) is False
    assert jd._has_established_join(A, C) is True
    assert jd._has_established_join(A, D) is True


def test_resolve_follows_only_served_joins(graph, monkeypatch):
    import agents.resolve as resolve
    monkeypatch.setattr(resolve, "neptune_query", graph)
    monkeypatch.setattr(resolve, "get_schema", lambda sid: f"schema_{sid[-1]}")
    monkeypatch.setattr(resolve, "get_name", lambda sid: sid)
    monkeypatch.setattr(resolve, "pg", lambda schema, sql, params=None:
                        {"columns": ["k"], "rows": [["v"]]})
    raw: list = []
    row = {"site_code": "S1", "lot": "L1", "batch": "B1", "serial": "N1"}
    resolve._follow_joinpaths(A, row, {A}, raw, [], [])
    assert {r["system"] for r in raw} == {C, D}


def test_dimension_join_read(graph):
    from agents import metric_dimension
    rows = graph(metric_dimension._JOINPATH_CYPHER, {"sid": F})
    assert {r["join_id"] for r in rows} == {INTRA_FREE_JP}


def test_self_map_covering_read(graph, monkeypatch):
    from agents import self_learning
    monkeypatch.setattr(self_learning, "_neptune_query", graph)
    assert self_learning._direct_joinpath_rows(A, B) == []
    assert len(self_learning._direct_joinpath_rows(A, C)) == 1


def test_impact_and_paths(graph, monkeypatch):
    from agents import graph_analytics
    monkeypatch.setattr(graph_analytics, "neptune_query", graph)
    hop2 = graph_analytics._impact_hop2([A], None)
    assert {r["to_sys"] for r in hop2} == {C, D}
    assert graph_analytics._path_queries(A, B)[2] == []
    assert len(graph_analytics._path_queries(A, C)[2]) == 1


def test_system_connections(graph, monkeypatch):
    import agents.connection_evidence as ce
    monkeypatch.setattr(ce, "neptune_query_checked", graph)
    out = ce.system_connections(A)
    assert {jp["id"] for jp in out["join_paths"]} == SERVED_FROM_A
    assert out["join_paths_total"] == len(SERVED_FROM_A)


def test_graph_summary_status(graph, monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    body = server.thread_graph_summary()
    detail = {jp["id"]: jp for jp in body["join_paths_detail"]}
    for jid in OFF:
        assert detail[jid]["status"] == "deactivated"
    assert detail[LIVE_JP]["status"] is None
    assert detail[FREE_JP]["status"] is None
    # Counts are active rules; the switched-off ones are counted apart.
    assert body["identity_rules"] == 1 and body["identity_rules_inactive"] == 3
    assert body["join_paths"] == 3 and body["join_paths_inactive"] == len(OFF)


def test_provision_read_back(graph, monkeypatch):
    import agents.onboard as onboard
    monkeypatch.setattr(onboard, "neptune_query", graph)
    joinpaths, _vms = onboard._dp_written_artifacts(A)
    assert {jp["id"] for jp in joinpaths} == SERVED_FROM_A
    assert all(set(jp) >= {"id", "from_system", "to_field"} for jp in joinpaths)


def test_inactive_joinpaths_read(graph):
    from agents import joinpath_rule_link as link
    rows = graph(link.INACTIVE_JOINPATHS_CYPHER)
    assert {r["id"] for r in rows} == OFF


def test_explore_graph(graph, monkeypatch):
    import agents.graph_tools as graph_tools
    monkeypatch.setattr(graph_tools, "neptune_query_checked", graph)
    monkeypatch.setattr(graph_tools, "neptune_query", graph)
    out = json.loads(graph_tools.explore_graph(
        "MATCH (jp:JoinPath) WHERE jp.from_system = 'sys-a' RETURN jp"))
    served = {r["jp"]["~properties"]["id"] for r in out["results"]}
    assert served == SERVED_FROM_A
    # Scoped to what the result touched: the sys-f JoinPath is not named.
    assert {w["id"] for w in out["inactive_join_paths"]} == {DEAD_JP, RETIRED_JP}
    out = json.loads(graph_tools.explore_graph(
        "MATCH (jp:JoinPath) WHERE jp.from_system = 'sys-a' "
        "RETURN jp.id AS id, jp.from_field AS from_field"))
    assert {r["id"] for r in out["results"]} == SERVED_FROM_A


# --------------------------------------------------------------------------- #
# Deactivate / reactivate keep the JoinPath in step (never a delete)
# --------------------------------------------------------------------------- #

def _jp(run, jid):
    return run("MATCH (jp:JoinPath {id: $id}) RETURN jp.status AS status, "
               "jp.deactivated_with_rule AS mark", {"id": jid})[0]


def test_cascade_round_trip_and_idempotence(graph, monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=True)
    assert _jp(graph, LIVE_JP) == {"status": "deactivated", "mark": LIVE_IR}
    first = graph("MATCH (jp:JoinPath {id: $id}) RETURN jp.deactivated_at AS at", {"id": LIVE_JP})
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=True)
    again = graph("MATCH (jp:JoinPath {id: $id}) RETURN jp.deactivated_at AS at", {"id": LIVE_JP})
    assert first == again  # a second deactivation re-stamps nothing
    assert LIVE_JP not in {jp["id"] for jp in _seed_rows(graph)[A]["join_paths"]}

    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=False)
    assert _jp(graph, LIVE_JP) == {"status": "active", "mark": None}
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=False)
    assert _jp(graph, LIVE_JP) == {"status": "active", "mark": None}
    assert LIVE_JP in {jp["id"] for jp in _seed_rows(graph)[A]["join_paths"]}
    assert graph("MATCH (jp:JoinPath {id: $id}) RETURN count(jp) AS n", {"id": LIVE_JP})[0]["n"] == 1


def test_a_steward_switched_off_joinpath_stays_off_when_its_rule_returns(graph, monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    server._set_rule_lifecycle(LIVE_JP, "steward", deactivate=True)  # the JoinPath itself
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=True)
    assert _jp(graph, LIVE_JP) == {"status": "deactivated", "mark": None}
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=False)
    assert _jp(graph, LIVE_JP)["status"] == "deactivated"


def test_live_shape_reactivation_needs_no_data_change(graph, monkeypatch):
    """The live rule was deactivated before the cascade existed: its JoinPath
    carries no mark. Reading already leaves it out; reactivating the rule
    serves it again with no repair write."""
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    assert _jp(graph, DEAD_JP) == {"status": None, "mark": None}
    server._set_rule_lifecycle(DEAD_IR, "steward", deactivate=False)
    assert _jp(graph, DEAD_JP) == {"status": None, "mark": None}
    assert DEAD_JP in {jp["id"] for jp in _seed_rows(graph)[A]["join_paths"]}


def test_requeue_cascade(graph, monkeypatch):
    from agents import self_learning
    monkeypatch.setattr(self_learning, "_neptune_write_loud", graph)
    self_learning._undo_graph_artifact(
        {"proposed_type": "identity_rule", "neptune_node_id": LIVE_IR, "requeued_by": "s"})
    assert _jp(graph, LIVE_JP) == {"status": "deactivated", "mark": LIVE_IR}


def _snapshot(run) -> dict:
    return {row["id"]: row["props"] for row in run(
        "MATCH (n) WHERE n:JoinPath OR n:IdentityRule OR n:System OR n:Concept "
        "RETURN coalesce(n.id, '') AS id, properties(n) AS props")}


def test_rerunning_deactivate_marks_the_live_joinpath_and_changes_nothing_else(graph, monkeypatch):
    """The live shape: the rule was deactivated before the cascade existed,
    its JoinPath carries no status. Running the deactivate action again
    marks that JoinPath and changes nothing else, then a third run changes
    nothing at all."""
    import agents.server as server
    from agents import rule_status
    monkeypatch.setattr(server, "neptune_query", graph)
    graph("MATCH (ir:IdentityRule {id: $id}) SET ir.deactivated_at = '2026-01-01T00:00:00Z'",
          {"id": DEAD_IR})
    before = _snapshot(graph)
    result = server._set_rule_lifecycle(DEAD_IR, "operator", deactivate=True)
    after = _snapshot(graph)
    assert result["join_paths_changed"] == 1
    assert result["deactivated_by"] == "ADT"
    assert result["deactivated_at"] == "2026-01-01T00:00:00Z"
    changed = {nid for nid in before if before[nid] != after[nid]}
    assert changed == {DEAD_JP}
    assert after[DEAD_JP]["status"] == "deactivated"
    assert after[DEAD_JP]["deactivated_with_rule"] == DEAD_IR
    assert after[DEAD_JP]["deactivated_by"] == "operator"
    assert {k: v for k, v in after[DEAD_JP].items()
            if k not in ("status", "deactivated_with_rule", "deactivated_by", "deactivated_at")
            } == before[DEAD_JP]

    again = server._set_rule_lifecycle(DEAD_IR, "someone-else", deactivate=True)
    assert again["join_paths_changed"] == 0
    assert _snapshot(graph) == after

    # The data now carries the status: the documented clause leaves it out.
    active = {r["id"] for r in graph(
        f"MATCH (jp:JoinPath) {rule_status.active_where('jp')} AND jp.from_system = 'sys-a' "
        "RETURN jp.id AS id")}
    assert DEAD_JP not in active and LIVE_JP in active
    # Reactivating the rule restores the JoinPath it marked.
    server._set_rule_lifecycle(DEAD_IR, "operator", deactivate=False)
    assert _jp(graph, DEAD_JP) == {"status": "active", "mark": None}


def test_a_marked_joinpath_left_active_is_switched_off_again(graph, monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=True)
    graph("MATCH (jp:JoinPath {id: $id}) SET jp.status = 'active', jp.rule_id = 'ir-moved'",
          {"id": LIVE_JP})
    result = server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=True)
    assert result["join_paths_changed"] == 1
    assert _jp(graph, LIVE_JP) == {"status": "deactivated", "mark": LIVE_IR}


def test_a_retired_joinpath_is_off_and_its_rule_never_marks_it(graph, monkeypatch):
    import agents.server as server
    from agents import join_discovery as jd
    graph("MATCH (jp:JoinPath {id: $id}) SET jp.status = 'retired'", {"id": LIVE_JP})
    assert LIVE_JP not in {jp["id"] for jp in _seed_rows(graph)[A]["join_paths"]}
    monkeypatch.setattr(jd, "_neptune_query", graph)
    assert jd._has_established_join(A, C) is False
    monkeypatch.setattr(server, "neptune_query", graph)
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=True)
    assert _jp(graph, LIVE_JP) == {"status": "retired", "mark": None}
    server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=False)
    assert _jp(graph, LIVE_JP) == {"status": "retired", "mark": None}


# --------------------------------------------------------------------------- #
# Round 30: retired rules, nested explore_graph values, value-level mappings,
# the KG view and the lifecycle results, on the same engine
# --------------------------------------------------------------------------- #

def test_switched_off_rules_are_off_on_every_rule_reader(graph, monkeypatch):
    served = {ir["id"] for row in _seed_rows(graph).values() for ir in row["identity_rules"]}
    assert LIVE_IR in served
    assert not served & {DEAD_IR, RETIRED_IR, INTRA_DEAD_IR}

    import agents.resolve as resolve
    answered: list = []

    def recording(cypher, params=None):
        rows = graph(cypher, params)
        answered.append(rows)
        return rows

    monkeypatch.setattr(resolve, "neptune_query", recording)
    assert {r["id"] for r in resolve._fetch_identity_rules("line")} == {LIVE_IR}
    resolve._join_transform_value(A, E, "N1")
    assert answered[-1] == []  # the retired A -> E rule is not read

    from agents import graph_analytics
    monkeypatch.setattr(graph_analytics, "neptune_query", graph)
    direct, indirect, _joins = graph_analytics._path_queries(A, E)
    assert direct == [] and indirect == []
    assert len(graph_analytics._path_queries(A, C)[0]) == 1

    import agents.connection_evidence as ce
    monkeypatch.setattr(ce, "neptune_query_checked", graph)
    assert {ir["id"] for ir in ce.system_connections(A)["identity_rules"]} == {LIVE_IR}


def _value_mapping(run, vid, frm, ffld, fval, to, tfld, tval, method="user_confirmed",
                   confidence=1.0):
    run("MATCH (f:System {id: $frm}), (t:System {id: $to}), (c:Concept {id: 'line'}) "
        "CREATE (vm:ValueMapping {id: $id, from_system: $frm, from_field: $ffld, "
        "to_system: $to, to_field: $tfld, concept: 'line', method: $m, confidence: $conf}), "
        "(vm)-[:FROM_SYSTEM]->(f), (vm)-[:TO_SYSTEM]->(t), (vm)-[:LINKS_CONCEPT]->(c) "
        "WITH vm WHERE $fval IS NOT NULL SET vm.from_value = $fval, vm.to_value = $tval",
        {"id": vid, "frm": frm, "ffld": ffld, "fval": fval, "to": to, "tfld": tfld,
         "tval": tval, "m": method, "conf": confidence})


def test_value_mappings_reach_the_seed_as_their_value_pairs(graph):
    import agents.orchestrator as orch
    for value, confidence in (("S1", 1.0), ("S2", 0.6)):
        _value_mapping(graph, f"subj-{value}", A, "records.site_code", value,
                       B, "hierarchy.line_name", value, confidence=confidence)
    _value_mapping(graph, "subj-novalue", A, "records.unmapped_code", None, D, "batches.unmapped_ref", None)
    # Every raw row the traversal returns for a system (an engine may split
    # one system's row): only value-level entries, never a value-less one.
    raw = [vm for row in graph(orch._ROUTE_SEED_CYPHER) for vm in row["value_mappings"]]
    assert raw and all(vm["value_pairs"] for vm in raw)
    assert {vm["from_field"] for vm in raw} == {"records.site_code"}

    seed = orch.build_route_seed(query=graph, question=QUESTION)
    payload = json.loads(seed.split("ROUTE SEED RESULT:\n", 1)[1].split("\n", 1)[0])
    served = [entry for system in payload["systems"]
              for entry in system.get("rules", {}).get("value_mappings", [])]
    [vm] = served  # one entry for the field pair, kept under one of its systems
    assert sorted(tuple(p) for p in vm["value_pairs"]) == [("S1", "S1"), ("S2", "S2")]
    assert "value_pair_count" not in vm  # every pair is listed
    assert vm["confidence"] == 0.6  # the entry stands for its weakest pair
    assert "unmapped_code" not in seed  # a value mapping with no value is not a relationship


def test_explore_graph_withholds_nested_rows_and_returns_projections(graph, monkeypatch):
    import agents.graph_tools as graph_tools
    monkeypatch.setattr(graph_tools, "neptune_query_checked", graph)
    monkeypatch.setattr(graph_tools, "neptune_query", graph)
    out = json.loads(graph_tools.explore_graph(
        "MATCH (jp:JoinPath) WHERE jp.from_system = 'sys-a' RETURN {join: jp} AS wrapped"))
    assert not any(DEAD_JP in json.dumps(r) for r in out["results"])
    assert DEAD_JP in {w["id"] for w in out["inactive_join_paths"]}
    assert len(out["results"]) == len(SERVED_FROM_A)
    # A projection that carries no JoinPath id is the query's own result:
    # the status contract (agents/metamodel.py) is how a reader filters it.
    out = json.loads(graph_tools.explore_graph(
        "MATCH (jp:JoinPath) WHERE jp.from_system = 'sys-a' "
        "RETURN jp.from_field AS f, jp.to_field AS t"))
    assert {(r["f"], r["t"]) for r in out["results"]} == {
        ("lot", "lot_no"), ("batch", "batch_no"), ("site_code", "line_name"), ("serial", "serial_no")}
    assert "inactive_join_paths" not in out


def test_an_active_join_with_the_same_field_names_is_returned(graph, monkeypatch):
    """The switched-off sys-a.site_code -> sys-b.line_name says nothing about
    an active sys-c.site_code -> sys-d.line_name."""
    import agents.graph_tools as graph_tools
    _join(graph, "subj-same-names", C, "schema_c.records", "site_code", D, ".hierarchy",
          "line_name", method="user_confirmed")
    monkeypatch.setattr(graph_tools, "neptune_query_checked", graph)
    monkeypatch.setattr(graph_tools, "neptune_query", graph)
    out = json.loads(graph_tools.explore_graph(
        "MATCH (jp:JoinPath) WHERE jp.from_system = 'sys-c' RETURN jp.from_system AS fs, "
        "jp.from_field AS ff, jp.to_system AS ts, jp.to_field AS tf"))
    assert out == {"results": [{"fs": C, "ff": "site_code", "ts": D, "tf": "line_name"}]}


def test_explore_graph_withholds_a_switched_off_identity_rule(graph, monkeypatch):
    import agents.graph_tools as graph_tools
    monkeypatch.setattr(graph_tools, "neptune_query_checked", graph)
    monkeypatch.setattr(graph_tools, "neptune_query", graph)
    off = {DEAD_IR, RETIRED_IR, INTRA_DEAD_IR}
    out = json.loads(graph_tools.explore_graph("MATCH (ir:IdentityRule) RETURN ir"))
    assert {r["ir"]["~properties"]["id"] for r in out["results"]} == {LIVE_IR}
    assert {w["id"] for w in out["inactive_identity_rules"]} == off
    assert out["withheld_rows"] == len(off)
    out = json.loads(graph_tools.explore_graph(
        "MATCH (ir:IdentityRule) RETURN ir.id AS id, ir.source_field AS f"))
    assert [r["id"] for r in out["results"]] == [LIVE_IR]
    out = json.loads(graph_tools.explore_graph(
        "MATCH (ir:IdentityRule) RETURN collect(ir.id) AS ids"))
    assert out["results"] == [{"ids": [LIVE_IR]}]


def test_the_status_contract_counts_only_active_rules(graph, monkeypatch):
    """Counts are not rewritten after the fact; the documented WHERE clause
    is what leaves the switched-off rules out of them."""
    import agents.graph_tools as graph_tools
    from agents import rule_status
    monkeypatch.setattr(graph_tools, "neptune_query_checked", graph)
    monkeypatch.setattr(graph_tools, "neptune_query", graph)
    out = json.loads(graph_tools.explore_graph("MATCH (ir:IdentityRule) RETURN count(ir) AS n"))
    assert out == {"results": [{"n": 4}]}
    out = json.loads(graph_tools.explore_graph(
        f"MATCH (ir:IdentityRule) {rule_status.active_where('ir')} RETURN count(ir) AS n"))
    assert out == {"results": [{"n": 1}]}


def test_graph_view_marks_switched_off_joinpaths(graph, monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    out = server.neptune_full_graph()
    jps = {n["props"]["id"]: n["props"] for n in out["nodes"] if n["type"] == "JoinPath"}
    for jid in OFF:
        assert jps[jid].get("effective_status") == "deactivated", jid
    for jid in (LIVE_JP, FREE_JP, INTRA_FREE_JP):
        assert "effective_status" not in jps[jid], jid


def test_lifecycle_results_on_the_engine(graph, monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", graph)
    result = server._set_rule_lifecycle(LIVE_IR, "steward", deactivate=False)
    assert result["status"] == "active" and "error" not in result
    result = server._set_rule_lifecycle(DEAD_JP, "steward", deactivate=False)
    assert result["status"] == "deactivated"
    assert result["own_status"] == "active" and result["inactive_rule_id"] == DEAD_IR
    assert DEAD_JP not in {jp["id"] for jp in _seed_rows(graph)[A]["join_paths"]}
    result = server._set_rule_lifecycle(LIVE_JP, "steward", deactivate=False)
    assert result["status"] == "active"
