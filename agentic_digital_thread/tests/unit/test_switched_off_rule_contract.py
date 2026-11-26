# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A switched-off IdentityRule or JoinPath: the status the data carries, and
the contract a free-form graph reader follows.

An IdentityRule or JoinPath whose status is ``deactivated`` or ``retired``
is soft-disabled: kept for audit, never a rule or a join. Pinned here:

* the data carries the status. Deactivating an IdentityRule sets
  ``status = 'deactivated'`` on every JoinPath made from it (and on any
  JoinPath it already marked), and running the same action again on a rule
  that is already deactivated marks a JoinPath it missed and changes nothing
  else: not the rule's own audit fields, not a JoinPath already switched off;
* the metamodel and the ``explore_graph`` description state that contract
  with the exact WHERE clause, the way they do for AuthorityRule;
* ``explore_graph`` withholds by exact identity only: a row or list item
  carrying a switched-off JoinPath or IdentityRule node, or its exact id.
  Endpoint names are never compared, so an active join that shares field
  names with a switched-off one is returned; counts and other projections
  come back as the query computed them (the contract above says how to
  filter them);
* the JoinPath readers that filter a JoinPath's own status use the shared
  predicate, so a ``retired`` JoinPath is off too;
* the graph summary counts active rules, with the inactive ones counted
  apart.

Offline: monkeypatched graph seams. The engine twins live in
``tests/integration/test_deactivated_rule_joinpath_memgraph.py``.
GENERIC: synthetic systems only.
"""
from __future__ import annotations

import json

import pytest

SYS_A, SYS_B, SYS_C, SYS_D = "sys-a", "sys-b", "sys-c", "sys-d"
DEAD_JP = "jp-sys-a-0123456789abcdef"
DEAD_IR = "ir-sys-a-0123456789abcdef"
LIVE_JP = "jp-sys-c-fedcba9876543210"
LIVE_IR = "ir-sys-c-fedcba9876543210"


def _status():
    from agents import rule_status
    return rule_status


def _where(alias: str) -> str:
    return ("WHERE " + " AND ".join(f"coalesce({alias}.status, '') <> '{status}'"
                                    for status in ("deactivated", "retired")))


# --------------------------------------------------------------------------- #
# The contract, as the model reads it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label,alias", [("IdentityRule", "ir"), ("JoinPath", "jp")])
def test_metamodel_states_the_status_contract(label, alias):
    from agents.metamodel import META_NODES
    entry = next(n for n in META_NODES if n["label"] == label)
    assert "status" in [p.strip() for p in entry["key_properties"].split(",")]
    text = entry["description"]
    assert "'deactivated' or 'retired'" in text
    assert "soft-disabled" in text and "kept for audit" in text
    assert _where(alias) in text


def test_metamodel_clause_is_the_shared_predicate():
    from agents.metamodel import META_NODES
    rs = _status()
    for label, alias in (("IdentityRule", "ir"), ("JoinPath", "jp")):
        entry = next(n for n in META_NODES if n["label"] == label)
        assert rs.active_where(alias) in entry["description"]
    assert rs.active_where("ir") == _where("ir")
    assert rs.rule_active("ir") == "(" + _where("ir")[len("WHERE "):] + ")"


def test_explore_graph_description_states_the_status_contract():
    import agents.graph_tools as graph_tools
    doc = " ".join((graph_tools.explore_graph.__doc__ or "").split())
    assert "{" + "rule_lifecycle}" not in doc
    assert "'deactivated' or 'retired'" in doc
    assert "coalesce(x.status, '') <> 'deactivated' AND coalesce(x.status, '') <> 'retired'" in doc
    assert "inactive_join_paths" in doc and "inactive_identity_rules" in doc
    # The documented limit of the withholding: aggregates are not rewritten.
    assert "Counts, aggregates and other projections are returned as the query computes them" in doc


# --------------------------------------------------------------------------- #
# explore_graph: exact identity, nothing else
# --------------------------------------------------------------------------- #

_DEAD_JP_ROW = {"id": DEAD_JP, "from_system": SYS_A, "from_table": "schema_a.records",
                "from_field": "cell", "to_system": SYS_B, "to_table": ".hierarchy",
                "to_field": "line_name", "rule_id": DEAD_IR, "reason": "rule_inactive"}
_LIVE_JP_PROPS = {"id": LIVE_JP, "from_system": SYS_C, "from_table": "schema_c.records",
                  "from_field": "cell", "to_system": SYS_D, "to_table": ".hierarchy",
                  "to_field": "line_name"}
_DEAD_IR_ROW = {"id": DEAD_IR, "concept": "line", "source_system": SYS_A,
                "source_field": "cell", "target_system": SYS_B, "target_field": "line_name",
                "status": "deactivated"}


def _graph_answer(query, params, jps, irs):
    """Answer the tool's own lookups the way the graph would: by id, and
    (when the query passes ``$tails``) by both fields, as its Cypher says."""
    values = set((params or {}).get("values") or [])
    tails = set((params or {}).get("tails") or [])
    if query.startswith("MATCH (jp:JoinPath)"):
        if "$values" not in query:
            return list(jps)
        return [jp for jp in jps if jp["id"] in values
                or ("$tails" in query
                    and (jp["from_field"] in values or jp["from_field"] in tails)
                    and (jp["to_field"] in values or jp["to_field"] in tails))]
    if query.startswith("MATCH (ir:IdentityRule)"):
        if "$values" not in query:
            return list(irs)
        return [ir for ir in irs if ir["id"] in values]
    return None


def _explore(monkeypatch, cypher, primary, *, jps=(_DEAD_JP_ROW,), irs=(_DEAD_IR_ROW,)):
    import agents.graph_tools as graph_tools
    calls: list[tuple[str, dict]] = []

    def checked(query, params=None):
        calls.append((query, dict(params or {})))
        if query == cypher:
            return primary
        answer = _graph_answer(query, params, jps, irs)
        return [] if answer is None else answer

    monkeypatch.setattr(graph_tools, "neptune_query_checked", checked)
    monkeypatch.setattr(graph_tools, "neptune_query", lambda c, p=None: [])
    return json.loads(graph_tools.explore_graph(cypher)), calls


def _jp_node(props):
    return {"~id": "n-" + props["id"], "~labels": ["JoinPath"], "~properties": dict(props)}


def _ir_node(props):
    return {"~id": "n-" + props["id"], "~labels": ["IdentityRule"], "~properties": dict(props)}


def test_an_active_join_sharing_endpoint_names_with_a_switched_off_one_is_returned(monkeypatch):
    """Generic field names recur across systems: a switched-off
    sys-a.cell -> sys-b.line_name says nothing about an active
    sys-c.cell -> sys-d.line_name."""
    cypher = ("MATCH (jp:JoinPath {from_system: 'sys-c'}) RETURN jp.from_system AS fs, "
              "jp.from_field AS ff, jp.to_system AS ts, jp.to_field AS tf")
    rows = [{"fs": SYS_C, "ff": "cell", "ts": SYS_D, "tf": "line_name"}]
    out, _calls = _explore(monkeypatch, cypher, rows)
    assert out == {"results": rows}


def test_field_projections_are_returned_as_computed(monkeypatch):
    """A projection that names no JoinPath id is the query's own result:
    the status contract is how a reader filters it, not post-hoc matching."""
    cypher = "MATCH (jp:JoinPath) RETURN jp.from_field AS f, jp.to_field AS t"
    rows = [{"f": "cell", "t": "line_name"}, {"f": "lot", "t": "lot_no"}]
    out, _calls = _explore(monkeypatch, cypher, rows)
    assert out == {"results": rows}
    pairs = [{"pair": ["cell", "line_name"]}]
    out, _calls = _explore(monkeypatch, "MATCH (jp:JoinPath) RETURN [jp.from_field, jp.to_field] AS pair",
                           pairs)
    assert out == {"results": pairs}


@pytest.mark.parametrize("cypher,rows", [
    ("MATCH (jp:JoinPath) RETURN count(jp) AS joins", [{"joins": 2}]),
    ("MATCH (jp:JoinPath) RETURN jp.from_system AS s, count(*) AS n", [{"s": SYS_A, "n": 1}]),
    ("MATCH (ir:IdentityRule) RETURN count(ir) AS rules", [{"rules": 2}]),
    ("MATCH (ir:IdentityRule) RETURN exists((ir)-[:LINKS_CONCEPT]->()) AS linked", [{"linked": True}]),
])
def test_count_queries_are_not_post_filtered(monkeypatch, cypher, rows):
    out, _calls = _explore(monkeypatch, cypher, rows)
    assert out == {"results": rows}


def test_a_switched_off_identity_rule_node_is_withheld(monkeypatch):
    live = {**_DEAD_IR_ROW, "id": LIVE_IR, "status": None}
    cypher = "MATCH (ir:IdentityRule) RETURN ir"
    out, _calls = _explore(monkeypatch, cypher,
                           [{"ir": _ir_node(_DEAD_IR_ROW)}, {"ir": _ir_node(live)}])
    assert [r["ir"]["~properties"]["id"] for r in out["results"]] == [LIVE_IR]
    assert out["withheld_rows"] == 1
    [entry] = out["inactive_identity_rules"]
    assert entry["id"] == DEAD_IR and entry["status"] == "deactivated"
    assert entry["from"] == f"{SYS_A}.cell" and entry["to"] == f"{SYS_B}.line_name"
    assert "inactive_join_paths" not in out
    note = out["inactive_join_paths_note"]
    assert "inactive_identity_rules" in note and "confirmed" not in note.lower()


@pytest.mark.parametrize("rows,kept", [
    ([{"id": DEAD_IR, "concept": "line"}, {"id": LIVE_IR, "concept": "line"}],
     [{"id": LIVE_IR, "concept": "line"}]),
    ([{"rule": {"id": DEAD_IR}}, {"rule": {"id": LIVE_IR}}], [{"rule": {"id": LIVE_IR}}]),
    ([{"rules": [DEAD_IR, LIVE_IR]}], [{"rules": [LIVE_IR]}]),
])
def test_a_switched_off_identity_rule_id_is_withheld(monkeypatch, rows, kept):
    out, _calls = _explore(monkeypatch, "MATCH (ir:IdentityRule) RETURN ir.id AS id", rows)
    assert out["results"] == kept
    assert [e["id"] for e in out["inactive_identity_rules"]] == [DEAD_IR]


def test_a_retired_identity_rule_is_withheld():
    from agents import explore_inactive_joins as eij
    assert _status().rule_inactive("ir") in eij.INACTIVE_RULES_SCOPED_CYPHER
    assert "ir.id IN $values" in eij.INACTIVE_RULES_SCOPED_CYPHER


def test_a_switched_off_joinpath_node_and_id_are_still_withheld(monkeypatch):
    cypher = "MATCH (jp:JoinPath) RETURN jp"
    dead = {k: v for k, v in _DEAD_JP_ROW.items() if k not in ("rule_id", "reason")}
    out, _calls = _explore(monkeypatch, cypher,
                           [{"jp": _jp_node(dead)}, {"jp": _jp_node(_LIVE_JP_PROPS)}])
    assert [r["jp"]["~properties"]["id"] for r in out["results"]] == [LIVE_JP]
    assert [e["id"] for e in out["inactive_join_paths"]] == [DEAD_JP]
    out, _calls = _explore(monkeypatch, "MATCH (jp:JoinPath) RETURN collect(jp.id) AS ids",
                           [{"ids": [DEAD_JP, LIVE_JP]}])
    assert out["results"] == [{"ids": [LIVE_JP]}]
    assert out["dropped_items"] == 1


def test_the_lookup_is_by_exact_id_only(monkeypatch):
    from agents import joinpath_rule_link as link
    cypher = "MATCH (jp:JoinPath) RETURN jp"
    _out, calls = _explore(monkeypatch, cypher, [{"jp": _jp_node(_LIVE_JP_PROPS)}])
    lookups = [(c, p) for c, p in calls if c == link.INACTIVE_JOINPATHS_SCOPED_CYPHER]
    assert len(lookups) == 1
    query, params = lookups[0]
    assert set(params) == {"values"}
    assert "$tails" not in query and "from_field IN" not in query
    assert "jp.id IN $values" in query


def test_a_joinpath_query_does_not_read_identity_rules(monkeypatch):
    from agents import explore_inactive_joins as eij
    _out, calls = _explore(monkeypatch, "MATCH (jp:JoinPath) RETURN jp",
                           [{"jp": _jp_node(_LIVE_JP_PROPS)}])
    assert not [c for c, _p in calls if c in (eij.INACTIVE_RULES_SCOPED_CYPHER,
                                               eij.INACTIVE_RULES_CYPHER)]


def test_an_unlabelled_match_checks_both_labels(monkeypatch):
    from agents import explore_inactive_joins as eij
    from agents import joinpath_rule_link as link
    cypher = "MATCH (n) WHERE n.id IN ['a', 'b'] RETURN n.id AS id"
    out, calls = _explore(monkeypatch, cypher, [{"id": DEAD_IR}, {"id": DEAD_JP}, {"id": "other"}])
    assert out["results"] == [{"id": "other"}]
    assert out["withheld_rows"] == 2
    queried = {c for c, _p in calls}
    assert link.INACTIVE_JOINPATHS_SCOPED_CYPHER in queried
    assert eij.INACTIVE_RULES_SCOPED_CYPHER in queried


def test_a_value_deeper_than_the_walk_is_dropped_when_nothing_is_switched_off(monkeypatch):
    """Fail closed: a value nested past the walk depth is never checked, so
    it is dropped whether or not the lookup found anything."""
    from agents import explore_inactive_joins as eij
    deep: object = DEAD_JP
    for _ in range(eij.WALK_DEPTH + 2):
        deep = {"inner": deep}
    out, _calls = _explore(monkeypatch, "MATCH (jp:JoinPath) RETURN jp.id AS x",
                           [{"x": deep}, {"x": "kept"}], jps=())
    assert out["results"] == [{"x": "kept"}]
    assert DEAD_JP not in json.dumps(out)


# --------------------------------------------------------------------------- #
# The data carries the status: the deactivate action, run again
# --------------------------------------------------------------------------- #

class _Graph:
    """The lifecycle seam: the rule write answers the rule's stored audit
    fields; the cascade answers how many JoinPaths it changed."""

    def __init__(self, *, stored_by="ADT", stored_at="2026-01-01T00:00:00Z", changed=1):
        self.calls: list[tuple[str, dict]] = []
        self.stored_by, self.stored_at, self.changed = stored_by, stored_at, changed

    def __call__(self, cypher, params=None):
        self.calls.append((cypher, dict(params or {})))
        if cypher.startswith("MATCH (ir:IdentityRule {id: $rid})"):
            return [{"id": (params or {}).get("rid"), "deactivated_by": self.stored_by,
                     "deactivated_at": self.stored_at, "reactivated_by": self.stored_by,
                     "reactivated_at": self.stored_at}]
        if cypher.startswith("MATCH (jp:JoinPath) WHERE"):
            return [{"changed": self.changed}]
        return []


def _rule_write(graph):
    return next(c for c, _p in graph.calls if c.startswith("MATCH (ir:IdentityRule {id: $rid})"))


def test_a_repeated_deactivation_keeps_the_rules_audit_fields(monkeypatch):
    import agents.server as server
    graph = _Graph()
    monkeypatch.setattr(server, "neptune_query", graph)
    result = server._set_rule_lifecycle(DEAD_IR, "operator@example.com", deactivate=True)
    write = _rule_write(graph)
    # The decision already recorded on the rule is kept, not re-stamped.
    assert "coalesce(ir.status, '') = $status AS same_decision" in write
    assert ("ir.deactivated_by = CASE WHEN same_decision THEN ir.deactivated_by "
            "ELSE $actor END") in write
    assert ("ir.deactivated_at = CASE WHEN same_decision THEN ir.deactivated_at "
            "ELSE $now END") in write
    # The response reports what the rule stores.
    assert result["deactivated_by"] == "ADT"
    assert result["deactivated_at"] == "2026-01-01T00:00:00Z"
    assert result["join_paths_changed"] == 1


def test_the_deactivate_cascade_marks_any_unmarked_joinpath(monkeypatch):
    import agents.server as server
    from agents import joinpath_rule_link as link
    graph = _Graph(changed=0)
    monkeypatch.setattr(server, "neptune_query", graph)
    result = server._set_rule_lifecycle(DEAD_IR, "operator@example.com", deactivate=True)
    [(cascade, params)] = [(c, p) for c, p in graph.calls
                           if c.startswith("MATCH (jp:JoinPath) WHERE")]
    # Made from the rule, or already marked by it ...
    assert f"({link.linked_rule_id_expr()} = $rid OR jp.{link.RULE_MARKER} = $rid)" in cascade
    # ... and not already switched off (deactivated or retired).
    assert _status().rule_active("jp") in cascade
    assert "jp.status = 'deactivated'" in cascade
    assert params["rid"] == DEAD_IR
    assert result["join_paths_changed"] == 0


def test_reactivation_reports_how_many_joinpaths_it_restored(monkeypatch):
    import agents.server as server
    graph = _Graph(changed=2)
    monkeypatch.setattr(server, "neptune_query", graph)
    result = server._set_rule_lifecycle(DEAD_IR, "operator@example.com", deactivate=False)
    assert result["status"] == "active"
    assert result["join_paths_changed"] == 2
    assert ("ir.reactivated_by = CASE WHEN same_decision THEN ir.reactivated_by "
            "ELSE $actor END") in _rule_write(graph)


def test_authority_rule_lifecycle_write_is_unchanged(monkeypatch):
    import agents.server as server
    calls: list[str] = []

    def query(cypher, params=None):
        calls.append(cypher)
        return [{"id": "ar-x"}] if cypher.startswith("MATCH (ar:AuthorityRule") else []

    monkeypatch.setattr(server, "neptune_query", query)
    result = server._set_rule_lifecycle("ar-x", "operator@example.com", deactivate=True)
    assert result["deactivated_by"] == "operator@example.com"
    assert "join_paths_changed" not in result
    write = next(c for c in calls if c.startswith("MATCH (ar:AuthorityRule"))
    assert write == ("MATCH (ar:AuthorityRule {id: $rid}) SET ar.status = $status, "
                     "ar.deactivated_by = $actor, ar.deactivated_at = $now RETURN ar.id AS id")


# --------------------------------------------------------------------------- #
# JoinPath readers: the JoinPath's own status, through the shared predicate
# --------------------------------------------------------------------------- #

def _own_status_readers(monkeypatch) -> dict:
    """The Cypher each JoinPath reader runs."""
    import agents.connection_evidence as ce
    import agents.resolve as resolve
    from agents import graph_analytics, join_discovery, metric_dimension, onboard
    from agents import orchestrator, self_learning
    out: dict = {"route_seed": orchestrator._ROUTE_SEED_CYPHER.split(
        "OPTIONAL MATCH (jp:JoinPath)")[1].split("OPTIONAL MATCH (vm:ValueMapping)")[0],
        "metric_dimension": metric_dimension._JOINPATH_CYPHER}

    def recorder(name):
        def query(cypher, params=None, *a, **k):
            if "JoinPath" in cypher:
                out.setdefault(name, cypher)
            return [{"total": 0}] if "count(" in cypher else []
        return query

    monkeypatch.setattr(join_discovery, "_neptune_query", recorder("join_discovery"))
    join_discovery._has_established_join(SYS_A, SYS_B)
    monkeypatch.setattr(resolve, "neptune_query", recorder("resolve"))
    resolve._follow_joinpaths(SYS_A, {"cell": "S1"}, {SYS_A}, [], [], [])
    monkeypatch.setattr(self_learning, "_neptune_query", recorder("self_learning"))
    self_learning._direct_joinpath_rows(SYS_A, SYS_B)
    monkeypatch.setattr(graph_analytics, "neptune_query", recorder("impact"))
    graph_analytics._impact_hop2([SYS_A], None)
    monkeypatch.setattr(graph_analytics, "neptune_query", recorder("paths"))
    graph_analytics._path_queries(SYS_A, SYS_B)
    monkeypatch.setattr(ce, "neptune_query_checked", recorder("connections"))
    ce.system_connections(SYS_A)
    monkeypatch.setattr(onboard, "neptune_query", recorder("provision"))
    onboard._dp_written_artifacts(SYS_A)
    return out


def test_joinpath_readers_filter_their_own_status_with_the_shared_predicate(monkeypatch):
    readers = _own_status_readers(monkeypatch)
    assert set(readers) == {"route_seed", "metric_dimension", "join_discovery", "resolve",
                            "self_learning", "impact", "paths", "connections", "provision"}
    active = _status().rule_active("jp")
    for name, cypher in readers.items():
        assert active in cypher, name


def test_the_inactive_joinpath_read_counts_a_retired_joinpath():
    from agents import joinpath_rule_link as link
    for cypher in (link.INACTIVE_JOINPATHS_SCOPED_CYPHER, link.INACTIVE_JOINPATHS_CYPHER):
        assert _status().rule_inactive("jp") in cypher


# --------------------------------------------------------------------------- #
# The graph summary counts active rules
# --------------------------------------------------------------------------- #

def test_graph_summary_counts_active_rules_and_the_inactive_ones_apart(monkeypatch):
    import agents.server as server

    def query(cypher, params=None):
        if cypher.startswith("MATCH (ir:IdentityRule)"):
            return [{"id": LIVE_IR, "status": None}, {"id": "ir-2", "status": "active"},
                    {"id": DEAD_IR, "status": "deactivated"}, {"id": "ir-3", "status": "retired"}]
        if cypher.startswith("MATCH (jp:JoinPath)"):
            return [{"id": LIVE_JP, "status": None}, {"id": DEAD_JP, "status": "deactivated"}]
        return []

    monkeypatch.setattr(server, "neptune_query", query)
    body = server.thread_graph_summary()
    assert body["identity_rules"] == 2 and body["identity_rules_inactive"] == 2
    assert body["join_paths"] == 1 and body["join_paths_inactive"] == 1
    # The audit lists stay whole.
    assert len(body["rules"]) == 4 and len(body["join_paths_detail"]) == 2


def test_graph_summary_failure_shape_carries_the_inactive_counts(monkeypatch):
    import agents.server as server

    def boom(cypher, params=None):
        raise RuntimeError("graph down")

    monkeypatch.setattr(server, "neptune_query", boom)
    body = server.thread_graph_summary()
    assert body["identity_rules"] == 0 and body["identity_rules_inactive"] == 0
    assert body["join_paths"] == 0 and body["join_paths_inactive"] == 0
