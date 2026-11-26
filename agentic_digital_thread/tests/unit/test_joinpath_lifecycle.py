# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""JoinPath lifecycle contracts.

A JoinPath gets the same soft deactivate/reactivate lifecycle as IdentityRule
and AuthorityRule: ``_set_rule_lifecycle`` falls through to the JoinPath label
after the two rule labels (their contracts stay byte-identical), and every
chat-path reader that DECIDES with a JoinPath (route seed, the crosswalk
"already joined" gate, the covering-rule check, intra-system dimension joins)
excludes ``status='deactivated'`` in the query itself. A wrong
onboarding-proposed join can then be switched off without deleting it. The
identifier-resolution hop is pinned end to end in
test_resolve_follows_active_joinpaths.py.

Offline: monkeypatched query seams. GENERIC: synthetic systems only.
"""
from __future__ import annotations

_JP_FILTER = "coalesce(jp.status, '') <> 'deactivated'"


def test_lifecycle_falls_through_to_joinpath(monkeypatch):
    import agents.server as server
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        calls.append((cypher, params or {}))
        if cypher.startswith(("MATCH (ir:IdentityRule", "MATCH (ar:AuthorityRule")):
            return []
        return [{"id": (params or {}).get("rid")}]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    result = server._set_rule_lifecycle(
        "jp-sys-a-0001", "reviewer@example.com", deactivate=True)
    assert result["status"] == "deactivated"
    assert result["id"] == "jp-sys-a-0001"
    assert result["deactivated_by"] == "reviewer@example.com"
    cypher, params = calls[-1]
    assert "MATCH (jp:JoinPath {id: $rid})" in cypher
    assert "SET jp.status = $status" in cypher
    assert params["status"] == "deactivated"
    assert len(calls) == 3


def test_lifecycle_reactivates_joinpath(monkeypatch):
    import agents.server as server

    def fake_query(cypher, params=None):
        if "JoinPath" not in cypher:
            return []
        return [{"id": (params or {}).get("rid")}]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    result = server._set_rule_lifecycle(
        "jp-sys-a-0001", "reviewer@example.com", deactivate=False)
    assert result["status"] == "active"
    assert result["reactivated_by"] == "reviewer@example.com"


def test_unknown_id_names_all_three_labels(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", lambda c, p=None: [])
    result = server._set_rule_lifecycle("nope", "reviewer@example.com", deactivate=True)
    assert "JoinPath" in result["error"]


def test_route_seed_excludes_deactivated_joinpaths():
    from agents import orchestrator
    seed = orchestrator._ROUTE_SEED_CYPHER
    jp_clause = seed.split("OPTIONAL MATCH (jp:JoinPath)")[1].split("WITH s,")[0]
    assert _JP_FILTER in jp_clause


def test_established_join_gate_excludes_deactivated(monkeypatch):
    from agents import join_discovery as jd
    captured: list[str] = []
    monkeypatch.setattr(jd, "_neptune_query", lambda c, p=None: captured.append(c) or [])
    assert jd._has_established_join("sys-a", "sys-b") is False
    assert _JP_FILTER in captured[0]


def test_covering_rule_read_excludes_deactivated(monkeypatch):
    from agents import self_learning as sl
    captured: list[str] = []
    monkeypatch.setattr(sl, "_neptune_query", lambda c, p=None: captured.append(c) or [])
    assert sl._direct_joinpath_rows("sys-a", "sys-b") == []
    assert _JP_FILTER in captured[0].split("RETURN")[0]


def test_dimension_joinpath_read_excludes_deactivated():
    from agents import metric_dimension
    assert _JP_FILTER in metric_dimension._JOINPATH_CYPHER.split("RETURN")[0]
