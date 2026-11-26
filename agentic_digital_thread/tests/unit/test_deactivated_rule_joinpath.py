# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A JoinPath made from an IdentityRule is not a join once that rule is off.

Onboarding writes one cross-system JoinPath per accepted IdentityRule. The two
nodes share no edge: the JoinPath names its rule by ``rule_id`` (newer writes)
or by its id alone (``jp-<system>-<digest>`` beside ``ir-<system>-<digest>``,
the same content digest). Observed live: the rule was deactivated, its
JoinPath was not, and the route seed kept serving it as a join, so the
planner treated one system's site-code column as the other system's line
names and sent line names that match no row.

Pinned here, offline:

* the link: ``rule_id`` first, then the shared id suffix, nothing for any
  other id;
* every reader that serves JoinPaths to an agent, uses them to plan or
  translate, or shows them as connections reads the linked rule through the
  ONE shared fragment and leaves the JoinPath out when the rule is off;
* deactivating a rule marks its JoinPaths, reactivating restores only the
  ones the rule switched off, and a direct JoinPath decision drops the mark;
* ``explore_graph`` withholds rows naming a switched-off JoinPath and says
  why, without calling it confirmed.

The real openCypher behaviour (the live shape run on an engine) is pinned in
``tests/integration/test_deactivated_rule_joinpath_memgraph.py``.

GENERIC: synthetic systems only.
"""
from __future__ import annotations

import json

import pytest

SYS_A, SYS_B = "sys-a", "sys-b"
DIGEST = "0123456789abcdef"
JP_ID = f"jp-{SYS_A}-{DIGEST}"
IR_ID = f"ir-{SYS_A}-{DIGEST}"


def _link():
    from agents import joinpath_rule_link
    return joinpath_rule_link


# --------------------------------------------------------------------------- #
# The link between a JoinPath and the rule it came from
# --------------------------------------------------------------------------- #

def test_rule_id_property_names_the_rule():
    assert _link().linked_rule_id(JP_ID, "ir-other-1") == "ir-other-1"


def test_shared_suffix_names_the_rule_when_rule_id_is_absent():
    link = _link()
    assert link.linked_rule_id(JP_ID) == IR_ID
    assert link.linked_rule_id(JP_ID, "") == IR_ID
    assert link.linked_rule_id(JP_ID, None) == IR_ID


def test_an_id_without_the_prefix_names_no_rule():
    link = _link()
    assert link.linked_rule_id("subj-0123") is None
    assert link.linked_rule_id("") is None
    assert link.linked_rule_id(None) is None


def test_cypher_link_matches_the_python_link():
    """The Cypher expression spells the same rule: rule_id first (a blank
    one does not count), then the id suffix, else nothing."""
    expr = _link().linked_rule_id_expr("jp")
    assert "jp.rule_id" in expr
    assert "STARTS WITH 'jp-'" in expr
    assert "'ir-' + substring(jp.id, 3)" in expr
    assert "coalesce(jp.rule_id, '') <> ''" in expr


def test_inactive_statuses_cover_deactivated_and_retired():
    link = _link()
    assert set(link.INACTIVE_RULE_STATUSES) == {"deactivated", "retired"}
    pred = link.rule_inactive("jp_rule")
    assert "'deactivated'" in pred and "'retired'" in pred
    assert "coalesce(jp_rule.status, '')" in pred


# --------------------------------------------------------------------------- #
# Every reader uses the shared fragment
# --------------------------------------------------------------------------- #

def _uses_rule_check(cypher: str) -> bool:
    link = _link()
    return (link.linked_rule_match("jp", "jp_rule") in cypher
            and link.rule_inactive("jp_rule") in cypher)


def test_route_seed_reads_the_linked_rule_inside_the_join_clause():
    from agents import orchestrator
    clause = orchestrator._ROUTE_SEED_CYPHER.split(
        "OPTIONAL MATCH (jp:JoinPath)")[1].split("AS join_paths")[0]
    assert _uses_rule_check(clause)
    # An inactive rule's JoinPath collects as null (dropped), so it never
    # takes one of the capped slots and the system row survives.
    assert "CASE WHEN jp IS NULL OR " + _link().rule_inactive("jp_rule") in clause


def test_established_join_gate_reads_the_linked_rule(monkeypatch):
    from agents import join_discovery as jd
    seen: list[str] = []
    monkeypatch.setattr(jd, "_neptune_query", lambda c, p=None: seen.append(c) or [])
    jd._has_established_join(SYS_A, SYS_B)
    assert _uses_rule_check(seen[0])


def test_resolve_join_hop_reads_the_linked_rule(monkeypatch):
    import agents.resolve as resolve
    seen: list[str] = []
    monkeypatch.setattr(resolve, "neptune_query", lambda c, p=None: seen.append(c) or [])
    resolve._follow_joinpaths(SYS_A, {}, set(), [], [], [])
    assert _uses_rule_check(seen[0])


def test_dimension_join_reads_the_linked_rule():
    from agents import metric_dimension
    assert _uses_rule_check(metric_dimension._JOINPATH_CYPHER)


def test_self_map_covering_join_reads_the_linked_rule(monkeypatch):
    from agents import self_learning
    seen: list[str] = []
    monkeypatch.setattr(self_learning, "_neptune_query",
                        lambda c, p=None: seen.append(c) or [])
    self_learning._direct_joinpath_rows(SYS_A, SYS_B)
    assert _uses_rule_check(seen[0])


def test_impact_and_paths_read_the_linked_rule(monkeypatch):
    from agents import graph_analytics
    seen: list[str] = []
    monkeypatch.setattr(graph_analytics, "neptune_query",
                        lambda c, p=None: seen.append(c) or [])
    graph_analytics._impact_hop2([SYS_A], None)
    graph_analytics._path_queries(SYS_A, SYS_B)
    jp_reads = [c for c in seen if "MATCH (jp:JoinPath)" in c]
    assert len(jp_reads) == 2
    for cypher in jp_reads:
        assert _uses_rule_check(cypher)
        assert "coalesce(jp.status, '') <> 'deactivated'" in cypher


def test_system_connections_reads_the_linked_rule(monkeypatch):
    import agents.connection_evidence as ce
    seen: list[str] = []

    def query(cypher, params=None):
        seen.append(cypher)
        return [{"total": 0}] if "count(" in cypher else []

    monkeypatch.setattr(ce, "neptune_query_checked", query)
    ce.system_connections(SYS_A)
    jp_reads = [c for c in seen if "MATCH (jp:JoinPath)" in c]
    assert len(jp_reads) == 2  # the count and the rows
    for cypher in jp_reads:
        assert _uses_rule_check(cypher)
        assert "coalesce(jp.status, '') <> 'deactivated'" in cypher


def test_graph_summary_reports_a_switched_off_joinpath_as_deactivated(monkeypatch):
    import agents.server as server
    seen: list[str] = []
    monkeypatch.setattr(server, "neptune_query", lambda c, p=None: seen.append(c) or [])
    server.thread_graph_summary()
    jp_read = next(c for c in seen if "MATCH (jp:JoinPath)" in c)
    assert _link().linked_rule_match("jp", "jp_rule") in jp_read
    assert "AS status" in jp_read


def test_provision_read_back_reads_the_linked_rule(monkeypatch):
    import agents.onboard as onboard
    seen: list[str] = []
    monkeypatch.setattr(onboard, "neptune_query", lambda c, p=None: seen.append(c) or [])
    onboard._dp_written_artifacts(SYS_A)
    jp_read = next(c for c in seen if "MATCH (jp:JoinPath)" in c)
    assert _uses_rule_check(jp_read)
    assert "coalesce(jp.status, '') <> 'deactivated'" in jp_read


# --------------------------------------------------------------------------- #
# Deactivation and reactivation keep the JoinPath in step with its rule
# --------------------------------------------------------------------------- #

class _Recorder:
    def __init__(self, found_labels=("IdentityRule",)):
        self.calls: list[tuple[str, dict]] = []
        self.found_labels = found_labels

    def __call__(self, cypher, params=None):
        self.calls.append((cypher, dict(params or {})))
        if cypher.startswith("MATCH (jp:JoinPath) WHERE"):
            return [{"id": JP_ID}]
        head = cypher.split(" SET ", 1)[0]
        if any(f":{label} {{id: $rid}})" in head for label in self.found_labels):
            return [{"id": (params or {}).get("rid")}]
        return []


def _cascade_calls(recorder):
    return [(c, p) for c, p in recorder.calls if c.startswith("MATCH (jp:JoinPath) WHERE")]


def test_deactivating_a_rule_marks_its_joinpaths(monkeypatch):
    import agents.server as server
    rec = _Recorder()
    monkeypatch.setattr(server, "neptune_query", rec)
    result = server._set_rule_lifecycle(IR_ID, "reviewer@example.com", deactivate=True)
    assert result["status"] == "deactivated"
    # The rule's own write is first.
    assert rec.calls[0][0].startswith("MATCH (ir:IdentityRule {id: $rid}) ")
    assert "SET ir.status = $status" in rec.calls[0][0]
    cascade = _cascade_calls(rec)
    assert len(cascade) == 1
    cypher, params = cascade[0]
    link = _link()
    assert link.linked_rule_id_expr("jp") + " = $rid" in cypher
    assert "coalesce(jp.status, '') <> 'deactivated'" in cypher
    assert "jp.status = 'deactivated'" in cypher
    assert f"jp.{link.RULE_MARKER} = $rid" in cypher
    assert "DELETE" not in cypher
    assert params["rid"] == IR_ID and params["actor"] == "reviewer@example.com"


def test_reactivating_a_rule_restores_only_the_joinpaths_it_switched_off(monkeypatch):
    import agents.server as server
    rec = _Recorder()
    monkeypatch.setattr(server, "neptune_query", rec)
    result = server._set_rule_lifecycle(IR_ID, "reviewer@example.com", deactivate=False)
    assert result["status"] == "active"
    cascade = _cascade_calls(rec)
    assert len(cascade) == 1
    cypher, params = cascade[0]
    marker = _link().RULE_MARKER
    assert cypher.startswith(f"MATCH (jp:JoinPath) WHERE jp.{marker} = $rid ")
    assert "jp.status = 'active'" in cypher
    assert f"REMOVE jp.{marker}" in cypher
    assert params["rid"] == IR_ID


def test_unknown_rule_touches_no_joinpath(monkeypatch):
    import agents.server as server
    rec = _Recorder(found_labels=())
    monkeypatch.setattr(server, "neptune_query", rec)
    result = server._set_rule_lifecycle("nope", "x", deactivate=True)
    assert "error" in result
    assert _cascade_calls(rec) == []


def test_authority_rule_lifecycle_touches_no_joinpath(monkeypatch):
    import agents.server as server
    rec = _Recorder(found_labels=("AuthorityRule",))
    monkeypatch.setattr(server, "neptune_query", rec)
    server._set_rule_lifecycle("ar-x", "x", deactivate=True)
    assert _cascade_calls(rec) == []


def test_a_direct_joinpath_decision_drops_the_rule_mark(monkeypatch):
    """A steward who switches the JoinPath itself on or off owns it from then
    on: reactivating the rule later must not undo that decision."""
    import agents.server as server
    rec = _Recorder(found_labels=("JoinPath",))
    monkeypatch.setattr(server, "neptune_query", rec)
    for deactivate in (True, False):
        server._set_rule_lifecycle(JP_ID, "x", deactivate=deactivate)
        jp_write = next(c for c, _p in rec.calls if c.startswith("MATCH (jp:JoinPath {id: $rid})"))
        assert f"REMOVE jp.{_link().RULE_MARKER}" in jp_write
        rec.calls.clear()


def test_requeue_of_an_identity_rule_marks_its_joinpaths(monkeypatch):
    from agents import self_learning
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(self_learning, "_neptune_write_loud",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or [])
    out = self_learning._undo_graph_artifact(
        {"proposed_type": "identity_rule", "neptune_node_id": IR_ID,
         "requeued_by": "steward@example.com"})
    assert out == "deactivated"
    assert "SET ir.status = 'deactivated'" in writes[0][0]
    cascade = [w for w in writes if w[0].startswith("MATCH (jp:JoinPath) WHERE")]
    assert len(cascade) == 1
    assert f"jp.{_link().RULE_MARKER} = $rid" in cascade[0][0]
    assert cascade[0][1]["rid"] == IR_ID


def test_reaccepting_an_identity_rule_restores_its_joinpaths(monkeypatch):
    from agents import self_learning
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(self_learning, "_neptune_write_loud",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or [])
    self_learning._apply_identity_rule(
        {"from_system": SYS_A, "to_system": SYS_B, "from_field": "t.code_a",
         "to_field": "t.code_b", "accepted_by": "steward@example.com"},
        IR_ID, 1, "")
    restore = [w for w in writes
               if w[0].startswith(f"MATCH (jp:JoinPath) WHERE jp.{_link().RULE_MARKER} = $rid ")]
    assert len(restore) == 1
    assert restore[0][1]["rid"] == IR_ID



# --------------------------------------------------------------------------- #
# explore_graph: an agent's own Cypher
# --------------------------------------------------------------------------- #

_LIVE_JP = {"id": JP_ID, "from_system": SYS_A, "from_table": "schema_a.records",
            "from_field": "site_code", "to_system": SYS_B, "to_table": ".hierarchy",
            "to_field": "line_name", "method": "foreign_key_match", "transform": "direct"}
_ACTIVE_JP = {**_LIVE_JP, "id": f"jp-{SYS_A}-fedcba9876543210", "from_field": "lot"}
_JP_QUERY = "MATCH (jp:JoinPath) RETURN jp"


def _node(props):
    return {"~id": "n-" + props["id"], "~labels": ["JoinPath"], "~properties": dict(props)}


def _inactive_row(jp):
    return {"id": jp["id"], "from_system": jp["from_system"], "from_table": jp["from_table"],
            "from_field": jp["from_field"], "to_system": jp["to_system"],
            "to_table": jp["to_table"], "to_field": jp["to_field"], "rule_id": IR_ID,
            "reason": "rule_inactive"}


def _explore(monkeypatch, primary, inactive=(_LIVE_JP,), cypher=_JP_QUERY):
    """Run explore_graph on ``primary`` rows. The inactive lookup answers the
    switched-off JoinPaths among ``inactive`` that its parameters scope to,
    the way the graph would."""
    import agents.graph_tools as graph_tools
    calls: list[tuple[str, dict]] = []
    link = _link()

    def checked(query, params=None):
        calls.append((query, dict(params or {})))
        if query == link.INACTIVE_JOINPATHS_SCOPED_CYPHER:
            values, tails = set(params["values"]), set(params.get("tails") or [])
            by_fields = "$tails" in query  # what the query text itself matches on
            return [_inactive_row(jp) for jp in inactive
                    if jp["id"] in values
                    or (by_fields and (jp["from_field"] in values or jp["from_field"] in tails)
                        and (jp["to_field"] in values or jp["to_field"] in tails))]
        if query == link.INACTIVE_JOINPATHS_CYPHER:
            return [_inactive_row(jp) for jp in inactive]
        if query.startswith("MATCH (ir:IdentityRule)"):
            return []  # no switched-off identity rule in these cases
        return primary

    monkeypatch.setattr(graph_tools, "neptune_query_checked", checked)
    monkeypatch.setattr(graph_tools, "neptune_query", lambda c, p=None: [])
    return json.loads(graph_tools.explore_graph(cypher)), calls


def test_explore_graph_withholds_a_switched_off_joinpath_node(monkeypatch):
    out, _calls = _explore(monkeypatch, [{"jp": _node(_LIVE_JP)}, {"jp": _node(_ACTIVE_JP)}])
    ids = [r["jp"]["~properties"]["id"] for r in out["results"]]
    assert ids == [_ACTIVE_JP["id"]]
    withheld = out["inactive_join_paths"]
    assert [w["id"] for w in withheld] == [JP_ID]
    assert out["withheld_rows"] == 1
    note = out["inactive_join_paths_note"]
    assert "not joins" in note.lower()
    assert "confirmed" not in note.lower()


def test_explore_graph_withholds_a_row_naming_it_by_id(monkeypatch):
    rows = [{"id": JP_ID, "from_field": "site_code"},
            {"id": _ACTIVE_JP["id"], "from_field": "lot"}]
    out, _calls = _explore(monkeypatch, rows)
    assert [r["id"] for r in out["results"]] == [_ACTIVE_JP["id"]]


@pytest.mark.parametrize("row", [
    {"wrapped": {"join": "NODE"}},
    {"wrapped": {"join": {"deeper": {"still": "NODE"}}}},
    {"wrapped": {"id": JP_ID, "from": "site_code"}},
])
def test_explore_graph_withholds_a_row_carrying_it_inside_a_map(monkeypatch, row):
    """A map wrapper (RETURN {join: jp}) is a row value like any other: a
    switched-off JoinPath anywhere inside it withholds the row."""
    def fill(value):
        if value == "NODE":
            return _node(_LIVE_JP)
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value
    out, _calls = _explore(monkeypatch, [fill(row), {"keep": "me"}])
    assert out["results"] == [{"keep": "me"}]
    assert out["withheld_rows"] == 1
    assert JP_ID not in json.dumps(out["results"])


def test_explore_graph_drops_list_items_at_any_depth(monkeypatch):
    rows = [{"system": SYS_A,
             "groups": [{"joins": [_node(_LIVE_JP), _node(_ACTIVE_JP)]},
                        [{"join": _node(_LIVE_JP)}, "other"]]}]
    out, _calls = _explore(monkeypatch, rows)
    assert JP_ID not in json.dumps(out["results"])
    kept = out["results"][0]
    assert kept["system"] == SYS_A
    assert kept["groups"][0]["joins"][0]["~properties"]["id"] == _ACTIVE_JP["id"]
    assert kept["groups"][1] == ["other"]
    assert out["dropped_items"] == 2
    assert "withheld_rows" not in out


def test_explore_graph_keeps_one_field_alone(monkeypatch):
    rows = [{"from_field": "site_code", "to_field": "lot"}, {"name": "line_name"}]
    out, _calls = _explore(monkeypatch, rows)
    assert out["results"] == rows


def test_lookup_is_scoped_to_the_values_in_the_result(monkeypatch):
    rows = [{"jp": _node(_ACTIVE_JP)}, {"f": "schema_a.records.site_code"}]
    _out, calls = _explore(monkeypatch, rows)
    link = _link()
    lookups = [(c, p) for c, p in calls if c in (link.INACTIVE_JOINPATHS_SCOPED_CYPHER,
                                                 link.INACTIVE_JOINPATHS_CYPHER)]
    assert len(lookups) == 1
    cypher, params = lookups[0]
    assert cypher == link.INACTIVE_JOINPATHS_SCOPED_CYPHER
    assert _ACTIVE_JP["id"] in params["values"]
    assert "schema_a.records.site_code" in params["values"]
    assert set(params) == {"values"}  # by exact id: no field names, no tails
    assert "LIMIT" not in cypher


def test_too_many_values_fall_back_to_the_bounded_global_lookup(monkeypatch):
    from agents import explore_inactive_joins
    many = [{"v": f"value-{i}"} for i in range(explore_inactive_joins.SCOPE_VALUE_CAP + 1)]
    _out, calls = _explore(monkeypatch, many + [{"jp": _node(_LIVE_JP)}])
    link = _link()
    assert [c for c, _p in calls if c == link.INACTIVE_JOINPATHS_CYPHER]
    assert not [c for c, _p in calls if c == link.INACTIVE_JOINPATHS_SCOPED_CYPHER]


def test_an_unlabelled_match_is_checked(monkeypatch):
    rows = [{"n": _node(_LIVE_JP)}, {"n": _node(_ACTIVE_JP)}]
    out, _calls = _explore(monkeypatch, rows,
                           cypher="MATCH (n) WHERE n.from_field IS NOT NULL RETURN n")
    assert [r["n"]["~properties"]["id"] for r in out["results"]] == [_ACTIVE_JP["id"]]


def test_field_names_alone_name_no_joinpath(monkeypatch):
    """Endpoint names are not an identity: a result holding a switched-off
    JoinPath's field names, and no JoinPath, is returned with no envelope."""
    rows = [{"f": "site_code", "t": "line_name"}, {"f": "line_name"}]
    out, _calls = _explore(monkeypatch, rows)
    assert out == {"results": rows}


def test_explore_graph_all_rows_withheld_is_not_reported_as_absent(monkeypatch):
    out, _calls = _explore(monkeypatch, [{"jp": _node(_LIVE_JP)}])
    assert out["results"] == []
    assert out["withheld_rows"] == 1
    assert "empty" not in out and "graph_node_types" not in out


def test_explore_graph_without_inactive_joinpaths_is_unchanged(monkeypatch):
    rows = [{"jp": _node(_ACTIVE_JP)}]
    out, _calls = _explore(monkeypatch, rows, inactive=())
    assert out == {"results": rows}


def test_explore_graph_skips_the_check_when_no_joinpath_is_involved(monkeypatch):
    import agents.graph_tools as graph_tools
    calls: list[str] = []
    rows = [{"name": "System A"}]
    monkeypatch.setattr(graph_tools, "neptune_query_checked",
                        lambda c, p=None: calls.append(c) or rows)
    monkeypatch.setattr(graph_tools, "neptune_query", lambda c, p=None: [])
    out = json.loads(graph_tools.explore_graph("MATCH (s:System) RETURN s.name AS name"))
    assert out == {"results": rows}
    assert len(calls) == 1


def test_explore_graph_keeps_the_rows_when_the_check_fails(monkeypatch):
    import agents.graph_tools as graph_tools
    from agents.db import GraphQueryError
    rows = [{"jp": _node(_LIVE_JP)}]
    link = _link()

    def checked(cypher, params=None):
        if cypher in (link.INACTIVE_JOINPATHS_CYPHER, link.INACTIVE_JOINPATHS_SCOPED_CYPHER):
            raise GraphQueryError("unavailable", "graph down")
        return rows

    monkeypatch.setattr(graph_tools, "neptune_query_checked", checked)
    monkeypatch.setattr(graph_tools, "neptune_query", lambda c, p=None: [])
    out = json.loads(graph_tools.explore_graph("MATCH (jp:JoinPath) RETURN jp"))
    assert out["results"] == rows


@pytest.mark.parametrize("value", [JP_ID, [JP_ID, _ACTIVE_JP["id"]]])
def test_explore_graph_drops_the_id_from_a_list_and_keeps_the_row(monkeypatch, value):
    rows = [{"system": SYS_A, "joins": value if isinstance(value, list) else [value]}]
    out, _calls = _explore(monkeypatch, rows)
    assert out["results"][0]["system"] == SYS_A
    assert JP_ID not in out["results"][0]["joins"]
