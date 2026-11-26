# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The role pass records each system's per-site asset coverage on its System node."""
from __future__ import annotations

import json
import logging

import pytest

from agents import asset_role_pass as arp
from agents import onboard


def _asset(aid, model, parent=None, level="equipment", path=None):
    return {"id": aid, "name": aid.replace("-", " ").title(), "asset_type": model,
            "path": path or aid, "parent_id": parent, "parent_path": None,
            "level": level, "walker_level": level}


def _site_graph():
    """Enterprise > Site A > Area 1 > Line 1 > two machines."""
    return [
        _asset("ent", "Enterprise", level="enterprise", path="ent"),
        _asset("site-a", "Site", "ent", level="site", path="ent/site-a"),
        _asset("area-1", "Area", "site-a", level="area", path="ent/site-a/area-1"),
        _asset("line-1", "Line", "area-1", level="line", path="ent/site-a/area-1/line-1"),
        _asset("m-1", "Machine", "line-1", path="ent/site-a/area-1/line-1/m-1"),
        _asset("m-2", "Machine", "line-1", path="ent/site-a/area-1/line-1/m-2"),
    ]


class _Graph:
    def __init__(self, assets, fail_write=None, raise_write=None, on_write=None):
        self.assets, self.fail_write, self.raise_write = assets, fail_write, raise_write
        self.on_write = on_write
        self.writes: list[tuple[str, dict]] = []

    def query(self, cypher, params=None):
        return {arp._READ_ASSETS: self.assets, arp._READ_MAPPINGS: [],
                arp._READ_OVERRIDES: [],
                arp._READ_SYSTEMS: [{"system": "hist", "has_system": True,
                                     "removal_incomplete": False}]}[cypher]

    def write(self, cypher, params=None):
        if self.on_write:
            self.on_write(cypher)
        self.writes.append((cypher, params))
        if cypher == self.raise_write:
            raise RuntimeError("boom")
        return cypher != self.fail_write


def test_coverage_is_the_last_write_of_the_pass_and_counts_by_level():
    g = _Graph(_site_graph())
    s = arp.classify_system("hist", query=g.query, write=g.write)
    cypher, params = g.writes[-1]
    assert cypher == arp._WRITE_COVERAGE
    assert "MATCH (s:System {id: $sys})" in cypher and "MERGE" not in cypher
    assert "s.asset_coverage = $coverage" in cypher and "s.asset_coverage_at = $now" in cypher
    assert params["sys"] == "hist" and params["now"]
    cov = json.loads(params["coverage"])
    assert cov["sites"] == [{"name": "Site A", "total": 4,
                             "levels": {"area": 1, "line": 1, "equipment": 2}}]
    assert s["written"] and s["coverage_written"] is True and s["coverage"] == cov


def test_the_coverage_write_is_held_under_the_role_pass_lock():
    seen = []
    g = _Graph(_site_graph(), on_write=lambda c: seen.append(
        (c, arp.ROLE_PASS_LOCK.locked())))
    arp.classify_system("hist", query=g.query, write=g.write)
    assert (arp._WRITE_COVERAGE, True) in seen


def test_a_system_with_no_assets_left_removes_both_properties():
    g = _Graph([])
    s = arp.classify_system("hist", query=g.query, write=g.write)
    assert [c for c, _ in g.writes] == [arp._CLEAR_COVERAGE]
    assert "REMOVE s.asset_coverage, s.asset_coverage_at" in arp._CLEAR_COVERAGE
    assert "MATCH (s:System {id: $sys})" in arp._CLEAR_COVERAGE
    assert s["coverage"] is None and s["coverage_written"] is True


def test_restore_removes_both_properties():
    g = _Graph(_site_graph())
    s = arp.restore_system("hist", write=g.write)
    assert [c for c, _ in g.writes] == [arp._RESTORE_ASSETS, arp._RESTORE_MODELS,
                                        arp._CLEAR_COVERAGE]
    assert s == {"system": "hist", "restored": True}


def test_dry_run_returns_the_coverage_and_writes_nothing():
    g = _Graph(_site_graph())
    s = arp.classify_system("hist", dry_run=True, query=g.query, write=g.write)
    assert g.writes == []
    assert s["coverage"]["sites"][0]["levels"] == {"area": 1, "line": 1, "equipment": 2}
    assert "coverage_written" not in s


def test_dry_run_of_an_empty_system_writes_nothing():
    g = _Graph([])
    s = arp.classify_system("hist", dry_run=True, query=g.query, write=g.write)
    assert g.writes == [] and s["coverage"] is None


def test_a_failed_coverage_write_is_reported_and_the_roles_result_stands(caplog):
    g = _Graph(_site_graph(), fail_write=arp._WRITE_COVERAGE)
    with caplog.at_level(logging.WARNING, logger=arp.__name__):
        s = arp.classify_system("hist", query=g.query, write=g.write)
    assert s["written"] is True and s["assets"] == 6
    assert s["coverage_written"] is False and s["coverage_error"]
    # One best-effort clear so stale counts are not served.
    assert [c for c, _ in g.writes][-2:] == [arp._WRITE_COVERAGE, arp._CLEAR_COVERAGE]
    assert s["coverage_cleared"] is True
    assert any("asset coverage" in r.getMessage() and "category=" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_a_raising_coverage_write_is_reported_not_raised(caplog):
    g = _Graph(_site_graph(), raise_write=arp._WRITE_COVERAGE)
    with caplog.at_level(logging.WARNING, logger=arp.__name__):
        s = arp.classify_system("hist", query=g.query, write=g.write)
    assert s["written"] is True and s["coverage_written"] is False
    assert "RuntimeError" in s["coverage_error"]
    assert any("category=RuntimeError" in r.getMessage() for r in caplog.records)


def test_a_failed_write_and_a_failed_clear_says_the_clear_failed():
    class _Both(_Graph):
        def write(self, cypher, params=None):
            self.writes.append((cypher, params))
            return cypher not in (arp._WRITE_COVERAGE, arp._CLEAR_COVERAGE)
    g = _Both(_site_graph())
    s = arp.classify_system("hist", query=g.query, write=g.write)
    assert s["written"] is True and s["coverage_written"] is False
    assert s["coverage_cleared"] is False and s["coverage_error"]


def test_a_failed_clear_on_restore_makes_the_restore_incomplete():
    g = _Graph([], fail_write=arp._CLEAR_COVERAGE)
    with pytest.raises(arp.RolePassError):
        arp.restore_system("hist", write=g.write)
    # The role writes ran first; the system is reported as an error.
    assert [c for c, _ in g.writes] == [arp._RESTORE_ASSETS, arp._RESTORE_MODELS,
                                        arp._CLEAR_COVERAGE]
    out = arp.restore_all(query=g.query, write=g.write)
    assert out["systems"]["hist"]["error"]


def test_roles_off_post_provision_logs_each_failed_restore(monkeypatch, caplog):
    monkeypatch.setenv("ADT_ASSET_ROLES", "off")
    monkeypatch.setattr(arp, "restore_all", lambda: {"systems": {
        "a": {"system": "a", "restored": True},
        "b": {"system": "b", "error": "graph write failed", "written": False}}})
    with caplog.at_level(logging.WARNING, logger=arp.__name__):
        arp.run_after_provision()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "b" in warnings[0] and "graph write failed" in warnings[0]


class _StatefulGraph(_Graph):
    """System node properties held in memory, so the seed reads what was written."""

    def __init__(self, assets, old_coverage, fail):
        super().__init__(assets)
        self.node = {"asset_coverage": old_coverage, "asset_coverage_at": "t0"}
        self.fail = fail

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        if cypher in self.fail:
            return False
        if cypher == arp._WRITE_COVERAGE:
            self.node.update(asset_coverage=params["coverage"], asset_coverage_at=params["now"])
        elif cypher == arp._CLEAR_COVERAGE:
            self.node.pop("asset_coverage", None)
            self.node.pop("asset_coverage_at", None)
        return True


def test_after_a_failed_refresh_the_seed_does_not_serve_the_old_counts():
    from agents import orchestrator as orch
    old = json.dumps({"anchor": "site", "sites": [
        {"name": "Site Old", "total": 99, "levels": {"equipment": 99}}]})
    g = _StatefulGraph(_site_graph(), old, fail={arp._WRITE_COVERAGE})
    s = arp.classify_system("hist", query=g.query, write=g.write)
    assert s["coverage_cleared"] is True and "asset_coverage" not in g.node

    def seed_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "hist", "system_name": "Hist", "plants": [],
                     "capabilities": None, "connection_type": None, "query_guidance": None,
                     "asset_coverage": g.node.get("asset_coverage")}]
        return []
    seed = orch.build_route_seed(query=seed_query)
    assert "Site Old" not in seed and "asset_coverage" not in seed


def test_a_failed_clear_on_an_empty_system_is_reported():
    g = _Graph([], fail_write=arp._CLEAR_COVERAGE)
    s = arp.classify_system("hist", query=g.query, write=g.write)
    assert s["coverage_written"] is False and s["coverage_error"]


def test_the_pass_also_visits_systems_that_only_carry_old_coverage():
    """A re-walk that dropped every asset leaves no :Asset to find the system
    by; its System node still carries counts the pass must clear."""
    assert "UNION" in arp._READ_SYSTEMS
    assert "s.asset_coverage IS NOT NULL" in arp._READ_SYSTEMS


def test_offboarding_deletes_the_system_node_so_the_coverage_goes_with_it(monkeypatch):
    # Coverage lives on the :System node, so a removal that deletes that node
    # takes the coverage with it. Checked by what the removal writes, not by
    # the source text of the delete.
    writes = []
    monkeypatch.setattr(onboard, "neptune_query", lambda q, p=None: [{"id": "sys-a"}])
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda q, p=None: writes.append((q, dict(p or {}))) or True)
    onboard._reset_delete_graph_artifacts("sys-a")
    system_deletes = [p for q, p in writes
                      if q.startswith("MATCH (s:System {id: $sid}) DETACH DELETE s")]
    assert system_deletes == [{"sid": "sys-a"}]
    assert "System" in onboard._RESET_NODE_LABELS
