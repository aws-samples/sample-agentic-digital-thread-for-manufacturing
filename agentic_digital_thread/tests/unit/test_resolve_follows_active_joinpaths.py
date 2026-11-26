# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identifier resolution through a pattern transform and its JoinPath hop.

``resolve_entity`` applies a pattern IdentityRule to find the identifier in a
target system, then follows the target system's JoinPaths to a joined system.
Two contracts are pinned here:

* a pattern-transform hit records its resolution step (the step used to raise
  ``NameError`` on the undefined ``concept`` name, so no hit ever got past it);
* a deactivated JoinPath, and a deactivated IdentityRule used for the join's
  transform, are never followed: the lifecycle readers skip them.

Offline: the graph and SQL seams are monkeypatched. GENERIC: synthetic
systems only.
"""
from __future__ import annotations

import pytest

_JP_FILTER = "coalesce(jp.status, '') <> 'deactivated'"
_IR_FILTER = "coalesce(ir.status, '') <> 'deactivated'"


class _Graph:
    def __init__(self, joinpath_status: str = ""):
        self.calls: list[str] = []
        self.joinpath_status = joinpath_status

    def __call__(self, cypher, params=None):
        self.calls.append(cypher)
        if "MATCH (ir:IdentityRule)-[:LINKS_CONCEPT]" in cypher:
            return [{"id": "ir-1", "src_sys": "sys-a", "src_field": "code_a",
                     "src_pat": "A-{id}", "tgt_sys": "sys-b", "tgt_field": "code_b",
                     "tgt_pat": "B-{id}", "conf": 0.9, "rule_type": "pattern_transform"}]
        if "HAS_FIELD]->(f:Field {name: $tgt_field})" in cypher:
            return [{"table_name": "schema_b.table_b"}]
        if "MATCH (jp:JoinPath)" in cypher:
            if self.joinpath_status == "deactivated" and _JP_FILTER in cypher:
                return []
            return [{"to_sys": "sys-c", "to_tbl": "schema_c.table_c", "to_fld": "ref_c",
                     "from_fld": "link_b", "xform": "", "descr": "table_b.link_b -> table_c.ref_c"}]
        return []


@pytest.fixture
def wired(monkeypatch):
    import agents.resolve as resolve

    def fake_pg(schema, sql, params=None):
        if "table_b" in sql:
            return {"columns": ["code_b", "link_b"], "rows": [["B-42", "L-7"]]}
        if "table_c" in sql:
            return {"columns": ["ref_c", "name_c"], "rows": [["L-7", "joined row"]]}
        return {"columns": [], "rows": []}

    monkeypatch.setattr(resolve, "pg", fake_pg)
    monkeypatch.setattr(resolve, "get_schema", lambda sid: {"sys-b": "schema_b", "sys-c": "schema_c"}.get(sid, ""))
    monkeypatch.setattr(resolve, "get_name", lambda sid: sid.upper())
    monkeypatch.setattr(resolve, "_try_brute_force", lambda *a, **k: None)
    monkeypatch.setattr(resolve, "_try_entity_anchored", lambda *a, **k: None)
    monkeypatch.setattr(resolve, "_try_direct_match", lambda *a, **k: None)
    return resolve


def test_pattern_transform_hit_records_its_step(monkeypatch, wired):
    graph = _Graph()
    monkeypatch.setattr(wired, "neptune_query", graph)
    raw, resolution, _unified, _c, _b = wired.resolve_entity("thing-id", "A-42")
    assert resolution[0]["rule"].startswith("IdentityRule: thing-id")
    assert "B-42" in resolution[0]["transform"]
    assert [r["system"] for r in raw] == ["SYS-B", "SYS-C"]


def test_active_joinpath_is_followed(monkeypatch, wired):
    graph = _Graph()
    monkeypatch.setattr(wired, "neptune_query", graph)
    _raw, resolution, _u, _c, _b = wired.resolve_entity("thing-id", "A-42")
    assert any(step["rule"].startswith("JoinPath:") for step in resolution)


def test_deactivated_joinpath_is_not_followed(monkeypatch, wired):
    graph = _Graph(joinpath_status="deactivated")
    monkeypatch.setattr(wired, "neptune_query", graph)
    raw, resolution, _u, _c, _b = wired.resolve_entity("thing-id", "A-42")
    assert not any(step["rule"].startswith("JoinPath:") for step in resolution)
    assert [r["system"] for r in raw] == ["SYS-B"]
    jp_query = next(c for c in graph.calls if "MATCH (jp:JoinPath)" in c)
    assert _JP_FILTER in jp_query


def test_join_transform_rules_skip_deactivated(monkeypatch):
    import agents.resolve as resolve
    seen: list[str] = []
    monkeypatch.setattr(resolve, "neptune_query", lambda c, p=None: seen.append(c) or [])
    resolve._join_transform_value("sys-b", "sys-c", "L-7")
    assert _IR_FILTER in seen[0]


def test_fuzzy_prefixes_come_only_from_active_rules(monkeypatch):
    """A deactivated rule's pattern prefix must not strip the user's
    identifier before the fuzzy LIKE lookup."""
    import agents.resolve as resolve
    rules = [{"pat": "ASSET-{id}", "status": "deactivated"}, {"pat": "LOT-{id}", "status": ""}]

    def fake_query(cypher, params=None):
        active_only = _IR_FILTER in cypher
        return [{"pat": r["pat"]} for r in rules if not active_only or r["status"] != "deactivated"]

    monkeypatch.setattr(resolve, "neptune_query", fake_query)
    prefixes = resolve._known_pattern_prefixes()
    assert prefixes == {"LOT-"}
    assert resolve._identifier_core("ASSET-ALPHA", prefixes) == resolve._identifier_core("ASSET-ALPHA", set())
