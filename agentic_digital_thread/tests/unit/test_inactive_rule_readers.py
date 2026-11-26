# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A switched-off IdentityRule is switched off on every reader, through ONE
shared predicate; the lifecycle routes tell the truth about what they did;
the KG view reports a switched-off JoinPath as such.

* ``retired`` is handled exactly like ``deactivated``: the route seed's
  identity rules (and so the identifier translations computed from them),
  resolve's rule fetch, join transform and fuzzy-prefix read, the /paths
  view and the system Connections section all use
  :func:`agents.rule_status.rule_active`; the JoinPath link uses the same
  module.
* Reactivating an IdentityRule whose JoinPath restore did not run reports a
  failure (the rule is active, its JoinPaths are not). Directly reactivating
  a JoinPath whose IdentityRule is still switched off reports the status
  readers see: still deactivated.
* ``/api/neptune/graph`` returns each JoinPath's effective status.

Offline: monkeypatched graph seams. GENERIC: synthetic systems only.
"""
from __future__ import annotations

SYS_A, SYS_B = "sys-a", "sys-b"
IR_ID = "ir-sys-a-0123456789abcdef"
JP_ID = "jp-sys-a-0123456789abcdef"


def _status():
    from agents import rule_status
    return rule_status


# --------------------------------------------------------------------------- #
# The shared predicate
# --------------------------------------------------------------------------- #

def test_inactive_statuses_and_python_twin():
    rs = _status()
    assert rs.INACTIVE_RULE_STATUSES == ("deactivated", "retired")
    for status in ("deactivated", "retired"):
        assert rs.is_inactive_status(status)
    for status in ("", None, "active", "Deactivated "):
        assert not rs.is_inactive_status(status)


def test_cypher_predicate_names_both_statuses():
    rs = _status()
    active = rs.rule_active("ir")
    assert "coalesce(ir.status, '') <> 'deactivated'" in active
    assert "coalesce(ir.status, '') <> 'retired'" in active
    assert active.startswith("(") and active.endswith(")")
    assert rs.rule_inactive("ir") == f"NOT {active}"


def test_joinpath_link_shares_the_predicate():
    from agents import joinpath_rule_link as link
    rs = _status()
    assert link.INACTIVE_RULE_STATUSES is rs.INACTIVE_RULE_STATUSES
    assert link.rule_inactive("jp_rule") == rs.rule_inactive("jp_rule")


# --------------------------------------------------------------------------- #
# Every IdentityRule reader
# --------------------------------------------------------------------------- #

def test_route_seed_identity_rules_use_the_shared_predicate():
    from agents import orchestrator
    clause = orchestrator._ROUTE_SEED_CYPHER.split(
        "OPTIONAL MATCH (ir:IdentityRule)")[1].split("RETURN s.id")[0]
    assert _status().rule_active("ir") in clause


def test_resolve_rule_readers_use_the_shared_predicate(monkeypatch):
    import agents.resolve as resolve
    seen: list[str] = []
    monkeypatch.setattr(resolve, "neptune_query", lambda c, p=None: seen.append(c) or [])
    resolve._fetch_identity_rules("line")
    resolve._join_transform_value(SYS_A, SYS_B, "V-1")
    resolve._known_pattern_prefixes()
    active = _status().rule_active("ir")
    assert len(seen) == 3
    assert active in seen[0] and active in seen[1]
    assert seen[2].count(active) == 2  # both UNION branches


def test_paths_view_uses_the_shared_predicate(monkeypatch):
    from agents import graph_analytics
    seen: list[str] = []
    monkeypatch.setattr(graph_analytics, "neptune_query",
                        lambda c, p=None: seen.append(c) or [])
    graph_analytics._path_queries(SYS_A, SYS_B)
    rs = _status()
    direct, indirect = seen[0], seen[1]
    assert rs.rule_active("ir") in direct
    assert rs.rule_active("ir1") in indirect and rs.rule_active("ir2") in indirect


def test_system_connections_identity_rules_use_the_shared_predicate(monkeypatch):
    import agents.connection_evidence as ce
    seen: list[str] = []

    def query(cypher, params=None):
        seen.append(cypher)
        return [{"total": 0}] if "count(" in cypher else []

    monkeypatch.setattr(ce, "neptune_query_checked", query)
    ce.system_connections(SYS_A)
    ir_reads = [c for c in seen if c.startswith("MATCH (ir:IdentityRule)")]
    assert len(ir_reads) == 2
    for cypher in ir_reads:
        assert _status().rule_active("ir") in cypher


# --------------------------------------------------------------------------- #
# Lifecycle results tell the truth
# --------------------------------------------------------------------------- #

def _lifecycle_graph(*, cascade_rows, jp_row=None):
    calls: list[tuple[str, dict]] = []

    def query(cypher, params=None):
        calls.append((cypher, dict(params or {})))
        if cypher.startswith("MATCH (ir:IdentityRule {id: $rid})"):
            rid = str((params or {}).get("rid"))
            return [{"id": rid}] if rid.startswith("ir-") else []
        if cypher.startswith("MATCH (jp:JoinPath) WHERE"):
            return cascade_rows
        if cypher.startswith("MATCH (jp:JoinPath {id: $rid})") and jp_row is not None:
            return [jp_row]
        return []

    return query, calls


def test_reactivation_reports_a_restore_that_did_not_run(monkeypatch):
    """The restore always answers one row (a count); no row means the graph
    read failed, and the steward must not be told the joins are back."""
    import agents.server as server
    query, _calls = _lifecycle_graph(cascade_rows=[])
    monkeypatch.setattr(server, "neptune_query", query)
    result = server._set_rule_lifecycle(IR_ID, "steward@example.com", deactivate=False)
    assert "error" in result
    assert result["id"] == IR_ID
    assert result["http_status"] == 502
    assert "join" in result["error"].lower()


def test_reactivation_succeeds_when_the_restore_answers(monkeypatch):
    import agents.server as server
    query, _calls = _lifecycle_graph(cascade_rows=[{"changed": 0}])
    monkeypatch.setattr(server, "neptune_query", query)
    result = server._set_rule_lifecycle(IR_ID, "steward@example.com", deactivate=False)
    assert result["status"] == "active" and "error" not in result


def test_deactivation_does_not_fail_on_a_cascade_miss(monkeypatch):
    """Readers already leave a switched-off rule's JoinPaths out through the
    link, so a failed mark changes nothing a reader sees."""
    import agents.server as server
    query, _calls = _lifecycle_graph(cascade_rows=[])
    monkeypatch.setattr(server, "neptune_query", query)
    result = server._set_rule_lifecycle(IR_ID, "steward@example.com", deactivate=True)
    assert result["status"] == "deactivated" and "error" not in result


def test_cascade_queries_always_answer_one_row():
    from agents import joinpath_rule_link as link
    for cypher in (link._DEACTIVATE_WITH_RULE_CYPHER, link._RESTORE_WITH_RULE_CYPHER):
        assert cypher.rstrip().endswith("WITH count(jp) AS changed RETURN changed")


def test_reactivate_route_maps_the_failure_status(monkeypatch):
    import agents.server as server
    query, _calls = _lifecycle_graph(cascade_rows=[])
    monkeypatch.setattr(server, "neptune_query", query)
    monkeypatch.setattr(server, "_check_steward", lambda request: None)
    monkeypatch.setattr(server, "_request_actor", lambda request: "steward@example.com")
    response = server.reactivate_identity_rule(IR_ID, object())
    assert response.status_code == 502


def test_direct_joinpath_reactivation_reports_the_effective_status(monkeypatch):
    import agents.server as server
    query, calls = _lifecycle_graph(
        cascade_rows=[], jp_row={"id": JP_ID, "rule_id": IR_ID, "rule_inactive": True})
    monkeypatch.setattr(server, "neptune_query", query)
    result = server._set_rule_lifecycle(JP_ID, "steward@example.com", deactivate=False)
    assert result["status"] == "deactivated"
    assert result["own_status"] == "active"
    assert result["inactive_rule_id"] == IR_ID
    jp_write = next(c for c, _p in calls if c.startswith("MATCH (jp:JoinPath {id: $rid})"))
    from agents import joinpath_rule_link as link
    assert link.linked_rule_match("jp", "jp_rule") in jp_write


def test_direct_joinpath_reactivation_with_an_active_rule_is_active(monkeypatch):
    import agents.server as server
    query, _calls = _lifecycle_graph(
        cascade_rows=[], jp_row={"id": JP_ID, "rule_id": IR_ID, "rule_inactive": False})
    monkeypatch.setattr(server, "neptune_query", query)
    result = server._set_rule_lifecycle(JP_ID, "steward@example.com", deactivate=False)
    assert result["status"] == "active"
    assert "inactive_rule_id" not in result


# --------------------------------------------------------------------------- #
# /api/neptune/graph
# --------------------------------------------------------------------------- #

def _jp_node(nid, jp_id):
    return {"~id": nid, "~labels": ["JoinPath"],
            "~properties": {"id": jp_id, "from_system": SYS_A, "to_system": SYS_B,
                            "from_field": "site_code", "to_field": "line_name"}}


def test_graph_view_reports_the_effective_joinpath_status(monkeypatch):
    import agents.server as server
    from agents import joinpath_rule_link as link
    seen: list[str] = []

    def query(cypher, params=None):
        seen.append(cypher)
        if "n:JoinPath" in cypher and "RETURN n" in cypher:
            return [{"n": _jp_node("g-1", JP_ID), "effective_status": "deactivated"},
                    {"n": _jp_node("g-2", "jp-sys-a-1111111111111111"), "effective_status": None}]
        return []

    monkeypatch.setattr(server, "neptune_query", query)
    monkeypatch.setattr(server, "neptune_query_checked", query)
    out = server.neptune_full_graph()
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["g-1"]["props"]["effective_status"] == "deactivated"
    assert "effective_status" not in by_id["g-2"]["props"]
    priority = next(c for c in seen if "n:JoinPath" in c and "RETURN n" in c)
    assert link.linked_rule_match("n", "jp_rule").replace(
        "WHERE", "WHERE n:JoinPath AND") in priority
    assert priority.rstrip().endswith("LIMIT 1000")
