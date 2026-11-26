# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""GET /api/onboard/connections/{system_id}: one system's connections.

The system detail page lists the join paths (both directions), value mappings
and identity rules that touch one system, each with its evidence. The route
is a bounded read: every list carries a LIMIT and the total it was cut from,
so a system with hundreds of value mappings answers "first N of M". A failed
graph read answers the fixed public error, never an empty list that would
read as "no connections". Offline: the graph is a fake.
"""
from __future__ import annotations

import asyncio
import re

import pytest

import agents.connection_evidence as ce
import agents.onboard as onboard

SID = "sys-a"


def _fake_graph(monkeypatch, *, totals=None, rows=None, raises=None):
    """Answer each read by node label: ``totals[label]`` for a count read,
    ``rows[label]`` (a callable taking the bound limit) for a row read."""
    seen: list[tuple[str, dict]] = []
    totals = totals or {}
    rows = rows or {}

    def query(cypher, params=None):
        seen.append((cypher, dict(params or {})))
        if raises is not None:
            raise raises
        label = re.search(r"MATCH \(\w+:(\w+)\)", cypher).group(1)
        if "count(" in cypher:
            return [{"total": totals.get(label, 0)}]
        make = rows.get(label)
        limit = int(re.search(r"LIMIT (\d+)", cypher).group(1))
        return make(limit) if make else []

    monkeypatch.setattr(ce, "neptune_query_checked", query)
    return seen


def _jp_rows(n):
    return [{"id": f"jp-{i}", "from_system": SID, "to_system": "sys-b",
             "basis": "value_overlap", "overlap_count": i} for i in range(n)]


def _vm_rows(n):
    return [{"id": f"vm-{i}", "from_system": SID, "to_system": "sys-b",
             "from_value": str(i), "to_value": str(i)} for i in range(n)]


def test_each_list_is_limited_and_reports_the_total_it_was_cut_from(monkeypatch):
    seen = _fake_graph(
        monkeypatch,
        totals={"JoinPath": 7, "ValueMapping": 450, "IdentityRule": 2},
        rows={"JoinPath": lambda limit: _jp_rows(min(7, limit)),
              "ValueMapping": lambda limit: _vm_rows(limit),
              "IdentityRule": lambda limit: [{"id": "ir-1"}, {"id": "ir-2"}]})
    out = ce.system_connections(SID)
    assert out["system_id"] == SID
    assert out["join_paths_total"] == 7 and len(out["join_paths"]) == 7
    assert out["value_mappings_total"] == 450
    assert len(out["value_mappings"]) == ce.VALUE_MAPPING_LIMIT
    assert out["identity_rules_total"] == 2 and len(out["identity_rules"]) == 2
    assert out["limits"] == {"join_paths": ce.JOIN_PATH_LIMIT,
                             "value_mappings": ce.VALUE_MAPPING_LIMIT,
                             "identity_rules": ce.IDENTITY_RULE_LIMIT}
    row_reads = [c for c, _p in seen if "count(" not in c]
    assert len(row_reads) == 3 and all(re.search(r"LIMIT \d+$", c) for c in row_reads)
    assert all(p == {"sid": SID} for _c, p in seen)


def test_a_graph_that_returns_more_than_the_limit_is_still_cut(monkeypatch):
    _fake_graph(monkeypatch, totals={"ValueMapping": 999},
                rows={"ValueMapping": lambda limit: _vm_rows(limit + 25)})
    out = ce.system_connections(SID)
    assert len(out["value_mappings"]) == ce.VALUE_MAPPING_LIMIT


def test_both_directions_are_read_for_every_kind(monkeypatch):
    seen = _fake_graph(monkeypatch)
    ce.system_connections(SID)
    by_label = {}
    for cypher, _p in seen:
        by_label.setdefault(re.search(r"MATCH \(\w+:(\w+)\)", cypher).group(1), []).append(cypher)
    for cypher in by_label["JoinPath"] + by_label["ValueMapping"]:
        assert ".from_system = $sid OR " in cypher and ".to_system = $sid" in cypher
    for cypher in by_label["IdentityRule"]:
        assert "ir.source_system = $sid OR ir.target_system = $sid" in cypher


def test_order_by_stays_within_the_engine_sort_key_limit(monkeypatch):
    seen = _fake_graph(monkeypatch)
    ce.system_connections(SID)
    for cypher, _p in seen:
        if "ORDER BY" in cypher:
            keys = cypher.split("ORDER BY", 1)[1].split("LIMIT", 1)[0].split(",")
            assert 1 <= len(keys) <= 5, cypher


def test_join_paths_return_their_evidence_fields(monkeypatch):
    seen = _fake_graph(monkeypatch)
    ce.system_connections(SID)
    jp_rows = next(c for c, _p in seen if ":JoinPath)" in c and "count(" not in c)
    for field in ("basis", "constraint_name", "overlap_count", "overlap_ratio",
                  "pair_count", "unique_constraints", "rule_id", "rule_method",
                  "method", "transform"):
        assert f"jp.{field} AS {field}" in jp_rows, field


def test_deactivated_identity_rules_are_excluded_from_the_count_and_the_rows(monkeypatch):
    """A deactivated IdentityRule is a soft disable, not a current connection.
    Both the count and the row read must carry the standard filter so a retired
    rule is never counted in the headline or shown as a live connection."""
    seen = _fake_graph(monkeypatch)
    ce.system_connections(SID)
    ir_reads = [c for c, _p in seen if c.startswith("MATCH (ir:IdentityRule)")]
    assert ir_reads, "IdentityRule was not read"
    for cypher in ir_reads:
        assert "coalesce(ir.status, '') <> 'deactivated'" in cypher, cypher


def test_an_active_rule_is_counted_and_a_deactivated_one_is_not(monkeypatch):
    """One active and one deactivated IdentityRule touch the system. Only the
    active one is counted and returned; the deactivated row is filtered out by
    the status predicate the reads now carry."""
    active = {"id": "ir-active", "concept": "batch", "source_system": SID,
              "status": "active", "method": "value_exact_match"}
    deactivated = {"id": "ir-dead", "concept": "batch", "source_system": SID,
                   "status": "deactivated", "method": "value_exact_match"}
    pool = [active, deactivated]

    def _rows(cypher):
        # Honour the status filter the production cypher carries.
        keep = pool if "<> 'deactivated'" not in cypher else \
            [r for r in pool if r.get("status") != "deactivated"]
        return keep

    seen: list = []

    def query(cypher, params=None):
        seen.append(cypher)
        label = re.search(r"MATCH \(\w+:(\w+)\)", cypher).group(1)
        if label != "IdentityRule":
            return [{"total": 0}] if "count(" in cypher else []
        kept = _rows(cypher)
        return [{"total": len(kept)}] if "count(" in cypher else kept

    monkeypatch.setattr(ce, "neptune_query_checked", query)
    out = ce.system_connections(SID)
    assert out["identity_rules_total"] == 1
    assert [r["id"] for r in out["identity_rules"]] == ["ir-active"]


def test_a_failed_read_answers_the_public_error_not_an_empty_list(monkeypatch):
    _fake_graph(monkeypatch, raises=RuntimeError("bolt://graph.internal:8182 refused"))
    out = ce.system_connections(SID)
    assert out["system_id"] == SID
    assert out["error_category"] == "graph_query_failed"
    assert "graph.internal" not in out["error"] and "8182" not in out["error"]
    assert "join_paths" not in out and "value_mappings" not in out


@pytest.mark.parametrize("bad", ["", "x" * 300])
def test_an_unusable_system_id_reads_nothing(monkeypatch, bad):
    seen = _fake_graph(monkeypatch)
    out = ce.system_connections(bad)
    assert out["error"] and seen == []


def test_the_route_offloads_the_same_read(monkeypatch):
    _fake_graph(monkeypatch, totals={"JoinPath": 1},
                rows={"JoinPath": lambda limit: _jp_rows(1)})
    out = asyncio.run(onboard.onboard_connections_route(SID))
    assert out["join_paths_total"] == 1 and out["join_paths"][0]["id"] == "jp-0"


def test_the_route_is_a_get_under_the_onboard_router():
    paths = {(r.path, tuple(sorted(r.methods))) for r in onboard.router.routes
             if hasattr(r, "methods")}
    assert (f"{onboard.router.prefix}/connections/{{system_id}}", ("GET",)) in paths
