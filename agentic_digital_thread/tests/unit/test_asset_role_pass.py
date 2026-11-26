# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The asset-role graph pass (agents/asset_role_pass.py) against fake seams."""
from __future__ import annotations

import threading

import pytest

from agents import asset_role_pass as arp

_PATHS: dict[str, str] = {}


def _asset(aid, model, parent=None, level="equipment", **extra):
    """One read row (one per parent edge; ``parent=None`` is the OPTIONAL MATCH miss)."""
    path = extra.pop("path", None) or (f"{_PATHS[parent]}/{aid}" if parent else aid)
    _PATHS.setdefault(aid, path)
    return {"id": aid, "name": aid, "asset_type": model, "path": path,
            "parent_id": parent, "parent_path": _PATHS.get(parent) if parent else None,
            "level": level, "walker_level": level, **extra}


def _station_graph():
    """Site > Area > Process > {Machine, Station > Machine} > events, with the
    walker's depth labels (the machines under the station are 'component')."""
    return [
        _asset("s", "Site", level="site"),
        _asset("a", "Area", "s", level="area"),
        _asset("p", "Process", "a", level="line"),
        _asset("m1", "Machine", "p"),
        _asset("m1-e", "State_Event", "m1", level="component"),
        _asset("st", "Station", "p"),
        _asset("m2", "Machine", "st", level="component"),
        _asset("m3", "Machine", "st", level="component"),
        _asset("m2-e", "State_Event", "m2", level="component"),
    ]


class _Graph:
    def __init__(self, assets, vms=(), overrides=(), systems=("hist",), fail_read=None,
                 fail_write=None):
        self.assets, self.vms, self.overrides, self.systems = assets, list(vms), list(overrides), systems
        self.fail_read, self.fail_write = fail_read, fail_write
        self.writes: list[tuple[str, dict]] = []

    def _system_rows(self):
        """``_READ_SYSTEMS`` rows. A plain id means a healthy owning :System;
        a 2-tuple ``(id, kind)`` with kind 'marked' (removal in progress) or
        'orphan' (no owning :System node) models the rows the new query emits."""
        rows = []
        for s in self.systems:
            sid, kind = (s if isinstance(s, tuple) else (s, "ok"))
            rows.append({"system": sid, "has_system": kind != "orphan",
                         "removal_incomplete": kind == "marked"})
        return rows

    def query(self, cypher, params=None):
        if cypher == self.fail_read:
            raise RuntimeError("transient")
        return {arp._READ_ASSETS: self.assets, arp._READ_MAPPINGS: self.vms,
                arp._READ_OVERRIDES: self.overrides,
                arp._READ_SYSTEMS: self._system_rows()}[cypher]

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        return cypher != self.fail_write

    def rows(self, cypher):
        return {r.get("id") or r.get("model"): r for c, p in self.writes if c == cypher
                for r in p["rows"]}


def test_pass_writes_roles_levels_depth_and_keeps_the_walker_label():
    g = _Graph(_station_graph())
    s = arp.classify_system("hist", query=g.query, write=g.write)
    rows = g.rows(arp._WRITE_ASSETS)
    assert (rows["st"]["role"], rows["st"]["level"], rows["st"]["walker_level"]) == \
        ("work_unit", "work_unit", "equipment")
    assert (rows["m2"]["role"], rows["m2"]["level"], rows["m2"]["depth"], rows["m2"]["walker_level"]) == \
        ("equipment", "equipment", 4, "component")
    assert rows["m1-e"]["role"] == "data_stream" and rows["m1-e"]["level"] == "component"
    assert rows["s"]["level"] == "site" and rows["p"]["level"] == "line"
    assert s["level_changes"] == 3 and s["written"]
    assert set(g.rows(arp._WRITE_MODELS)) == {"Site", "Area", "Process", "Machine", "Station",
                                              "State_Event"}
    sweep = [p for c, p in g.writes if c == arp._SWEEP_MODELS]
    assert sweep and set(sweep[0]["models"]) == set(g.rows(arp._WRITE_MODELS))
    assert arp._LINK_MODELS in [c for c, _ in g.writes]


def test_the_model_write_cannot_touch_a_steward_node():
    assert "WHERE coalesce(mr.status, '') <> 'steward'" in arp._WRITE_MODELS
    assert "<> 'steward'" in arp._SWEEP_MODELS and "<> 'steward'" in arp._RESTORE_MODELS


def test_dry_run_writes_nothing():
    g = _Graph(_station_graph())
    s = arp.classify_system("hist", dry_run=True, query=g.query, write=g.write)
    assert g.writes == [] and s["level_changes"] == 3 and not s["written"]


@pytest.mark.parametrize("failing", ["_READ_ASSETS", "_READ_MAPPINGS", "_READ_OVERRIDES"])
def test_a_failed_read_stops_the_system_before_any_write(failing):
    g = _Graph(_station_graph(), fail_read=getattr(arp, failing))
    with pytest.raises(arp.RolePassError):
        arp.classify_system("hist", query=g.query, write=g.write)
    assert g.writes == []


def test_a_failed_write_stops_the_later_writes(monkeypatch):
    g = _Graph(_station_graph(), fail_write=arp._WRITE_MODELS)
    with pytest.raises(arp.RolePassError):
        arp.classify_system("hist", query=g.query, write=g.write)
    assert [c for c, _ in g.writes] == [arp._WRITE_MODELS]   # assets untouched


def test_classify_all_reports_a_failed_system_and_runs_the_rest():
    g = _Graph(_station_graph(), systems=("a", "b"), fail_write=arp._LINK_MODELS)
    out = arp.classify_all(query=g.query, write=g.write)["systems"]
    assert out["a"]["error"] == "graph write failed" and not out["a"]["written"]
    assert set(out) == {"a", "b"}


def test_asset_systems_skips_marked_and_orphan_systems(caplog):
    g = _Graph(_station_graph(),
               systems=("ok", ("gone", "marked"), ("orphan", "orphan")))
    import logging
    with caplog.at_level(logging.INFO):
        systems = arp.asset_systems(query=g.query)
    assert systems == ["ok"]   # the marked and the orphan are both left out
    assert "gone" in caplog.text and "orphan" in caplog.text


def test_classify_all_only_runs_the_healthy_systems():
    g = _Graph(_station_graph(),
               systems=(("gone", "marked"), "live", ("stray", "orphan")))
    out = arp.classify_all(query=g.query, write=g.write)["systems"]
    assert set(out) == {"live"}   # marked system and orphan assets are not classified
    assert all(c != arp._WRITE_MODELS or p["sys"] == "live" for c, p in g.writes)


def test_a_coverage_only_system_mid_removal_is_left_alone():
    """A System with stored coverage and no Assets is visited so its stale counts
    are cleared, but not while its removal is unfinished."""
    g = _Graph([], systems=(("cov-gone", "marked"), "cov-live"))
    out = arp.classify_all(query=g.query, write=g.write)["systems"]
    assert set(out) == {"cov-live"}
    assert (arp._CLEAR_COVERAGE, {"sys": "cov-live"}) in g.writes
    assert all((p or {}).get("sys") != "cov-gone" for _, p in g.writes)


def test_steward_override_is_read_not_rewritten():
    g = _Graph(_station_graph(), overrides=[{"model": "Station", "role": "equipment"}])
    arp.classify_system("hist", query=g.query, write=g.write)
    assert "Station" not in g.rows(arp._WRITE_MODELS)
    assert g.rows(arp._WRITE_ASSETS)["m2"]["role"] == "equipment_part"


def test_value_evidence_and_element_ids_are_read():
    assets = [_asset("a", "Area", level="area"), _asset("c", "Cell", "a"),
              _asset("x", "Machine", "c", level="component")]
    vms = [{"concept": "equipment-id", "from_system": "mes", "from_value": "X",
            "to_system": "hist", "to_value": "MACHINE-X", "to_element_id": "x"}]
    s = arp.classify_system("hist", query=_Graph(assets, vms=vms).query, write=lambda *a: True)
    assert s["roles"]["Cell"]["role"] == "work_center"
    assert s["roles"]["Machine"]["source"] == "value_evidence"
    assert "to_element_id" in arp._READ_MAPPINGS


def test_current_parent_wins_over_edges_a_rewalk_left():
    """m3 moved from Station st to Process p; the old HAS_PARENT edge is still there."""
    assets = [a for a in _station_graph() if a["id"] != "m3"]
    moved_path = f"{_PATHS['p']}/m3"
    edges = [_asset("m3", "Machine", "st", path=moved_path), _asset("m3", "Machine", "p", path=moved_path)]
    placed, unplaced, _ = arp._read_assets(_Graph(assets + edges).query, "hist")
    assert next(r for r in placed if r.id == "m3").parent_id == "p" and unplaced == []
    lost = [dict(e, path="elsewhere/m3") for e in edges]
    placed, unplaced, _ = arp._read_assets(_Graph(assets + lost).query, "hist")
    assert [r.id for r in unplaced] == ["m3"] and "m3" not in {r.id for r in placed}


def test_inserting_a_tier_keeps_moved_assets_under_the_new_tier():
    """Site > Area > Process > Machine, then a Station is inserted between
    Process and the machines and the system is re-walked: each moved machine
    has its old parent (Process) and the new one (Station); both prefix its
    path. The longest wins, so the station keeps its machines."""
    base = [_asset("S", "Site", level="site"), _asset("A", "Area", "S", level="area"),
            _asset("P", "Process", "A", level="line"), _asset("ST", "Station", "P")]
    machines = []
    for m in ("M7", "M8"):
        path = f"{_PATHS['ST']}/{m}"
        machines += [_asset(m, "Machine", "P", level="component", path=path),
                     _asset(m, "Machine", "ST", level="component", path=path),
                     _asset(f"{m}-e", "State_Event", m, level="component", path=f"{path}/{m}-e")]
    g = _Graph(base + machines)
    s = arp.classify_system("hist", query=g.query, write=g.write)
    rows = g.rows(arp._WRITE_ASSETS)
    assert s["ambiguous_parents"] == 0
    assert (rows["ST"]["role"], rows["M7"]["role"], rows["M7"]["depth"]) == ("work_unit", "equipment", 4)


def test_an_unplaced_asset_takes_its_models_role_and_keeps_its_depth():
    assets = _station_graph()
    twin = [_asset("m4", "Machine", "st", level="component", path="x/m4"),
            _asset("m4", "Machine", "p", level="component", path="x/m4")]
    g = _Graph(assets + twin)
    s = arp.classify_system("hist", query=g.query, write=g.write)
    m4 = g.rows(arp._WRITE_ASSETS)["m4"]
    assert (m4["role"], m4["level"], m4["depth"]) == ("equipment", "equipment", None)
    assert s["ambiguous_parents"] == 1
    assert "coalesce(r.depth, a.depth)" in arp._WRITE_ASSETS


def test_an_unplaced_assets_subtree_is_unplaced_too():
    """m5 has two parent edges and neither prefixes its path: m5, its event
    and the event's child are all kept out of the tree (no depth 0 roots)."""
    assets = _station_graph()
    tied = [_asset("m5", "Machine", "st", level="component", path="y/m5"),
            _asset("m5", "Machine", "p", level="component", path="y/m5"),
            _asset("m5-e", "State_Event", "m5", level="component", path="y/m5/m5-e"),
            _asset("m5-e-x", "Extra", "m5-e", level="component", path="y/m5/m5-e/x")]
    g = _Graph(assets + tied)
    s = arp.classify_system("hist", query=g.query, write=g.write)
    rows = g.rows(arp._WRITE_ASSETS)
    assert s["ambiguous_parents"] == 3
    assert [rows[i]["depth"] for i in ("m5", "m5-e", "m5-e-x")] == [None, None, None]
    assert rows["m5-e"]["role"] == "data_stream" and rows["m5-e"]["level"] == "component"
    # A model seen only under the unplaced subtree is unclassified, walker level kept.
    assert rows["m5-e-x"]["role"] == "unclassified" and rows["m5-e-x"]["level"] == "component"


def test_walker_level_comes_from_the_walker_stamp():
    rewalked = _asset("m9", "Machine", level="equipment", walker_level="equipment")
    legacy = dict(_asset("m8", "Machine", level="component"), walker_level=None)
    _, _, walker = arp._read_assets(_Graph([rewalked, legacy]).query, "hist")
    assert walker == {"m9": "equipment", "m8": "component"}
    assert "a.walker_level = coalesce(a.walker_level, r.walker_level)" in arp._WRITE_ASSETS


def test_unclassified_models_keep_the_walker_label():
    assets = [_asset("r", "Node", level="enterprise"), _asset("n1", "Node", "r"),
              _asset("n2", "Node", "n1", level="component")]
    g = _Graph(assets)
    s = arp.classify_system("hist", query=g.query, write=g.write)
    rows = g.rows(arp._WRITE_ASSETS)
    assert [rows[i]["level"] for i in ("r", "n1", "n2")] == ["enterprise", "equipment", "component"]
    assert s["level_changes"] == 0 and s["needs_review"] == ["Node"]


def test_no_assets_writes_no_roles_and_only_clears_the_coverage():
    g = _Graph([])
    assert arp.classify_system("hist", query=g.query, write=g.write)["assets"] == 0
    assert [c for c, _ in g.writes] == [arp._CLEAR_COVERAGE]


def test_writes_are_chunked(monkeypatch):
    monkeypatch.setattr(arp, "_WRITE_CHUNK", 4)
    g = _Graph(_station_graph())
    arp.classify_system("hist", query=g.query, write=g.write)
    assert [len(p["rows"]) for c, p in g.writes if c == arp._WRITE_ASSETS] == [4, 4, 1]
    order = [c for c, _ in g.writes]
    assert order.index(arp._WRITE_MODELS) < order.index(arp._WRITE_ASSETS)


def test_restore_puts_walker_levels_back_and_drops_derived_roles():
    g = _Graph(_station_graph(), systems=("a", "b"))
    out = arp.restore_all(query=g.query, write=g.write)
    assert set(out["systems"]) == {"a", "b"}
    assert [c for c, _ in g.writes] == [arp._RESTORE_ASSETS, arp._RESTORE_MODELS,
                                        arp._CLEAR_COVERAGE] * 2
    assert "SET a.level = a.walker_level REMOVE a.role, a.depth" in arp._RESTORE_ASSETS


def test_run_after_provision_is_non_fatal(monkeypatch, caplog):
    monkeypatch.setattr(arp, "classify_all",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    arp.run_after_provision()
    assert "asset role pass failed" in caplog.text


def test_run_after_provision_rolls_back_when_switched_off(monkeypatch):
    calls = []
    monkeypatch.setattr(arp, "classify_all", lambda **kw: calls.append("classify"))
    monkeypatch.setattr(arp, "restore_all", lambda **kw: calls.append("restore"))
    monkeypatch.setenv("ADT_ASSET_ROLES", "off")
    arp.run_after_provision()
    assert calls == ["restore"]


@pytest.mark.parametrize("value,expected", [("on", True), ("", True), ("OFF", False),
                                            ("0", False), ("false", False)])
def test_enabled(monkeypatch, value, expected):
    monkeypatch.setenv("ADT_ASSET_ROLES", value)
    assert arp.enabled() is expected


class _BlockingGraph(_Graph):
    """A graph whose FIRST write blocks until released, logging every read and
    write with the pass that made it."""

    def __init__(self, name, log, **kw):
        super().__init__(_station_graph(), **kw)
        self.name, self.log = name, log
        self.in_write, self.release = threading.Event(), threading.Event()

    def query(self, cypher, params=None):
        self.log.append((self.name, "read"))
        return super().query(cypher, params)

    def write(self, cypher, params=None):
        if not self.in_write.is_set():
            self.in_write.set()
            assert self.release.wait(5), "test stuck"
        self.log.append((self.name, "write"))
        return super().write(cypher, params)


def _overlap(second):
    """Pass A blocks inside its first write; ``second(log)`` runs meanwhile.
    Returns the combined log once both finish."""
    log: list = []
    a = _BlockingGraph("A", log)
    t1 = threading.Thread(target=lambda: arp.classify_system("hist", query=a.query,
                                                             write=a.write))
    t1.start()
    assert a.in_write.wait(5)
    t2 = threading.Thread(target=lambda: second(log))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive(), "the second pass ran while the first held its write"
    a.release.set()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    return log


def _after_a(log, name):
    last_a = max(i for i, (who, _) in enumerate(log) if who == "A")
    return all(i > last_a for i, (who, _) in enumerate(log) if who == name)


def test_two_passes_on_one_system_never_interleave():
    """r7-1: a pass that read the graph before a new mapping must finish its
    writes before a later pass reads, or the stale asset rows land last."""
    def second(log):
        b = _Graph(_station_graph())
        arp.classify_system("hist", query=lambda c, p=None: (log.append(("B", "read")),
                                                             b.query(c, p))[1],
                            write=lambda c, p=None: (log.append(("B", "write")),
                                                     b.write(c, p))[1])
    log = _overlap(second)
    assert ("B", "read") in log and _after_a(log, "B")


def test_a_restore_waits_for_a_running_pass():
    def second(log):
        b = _Graph(_station_graph())
        arp.restore_system("hist", write=lambda c, p=None: (log.append(("R", "write")),
                                                            b.write(c, p))[1])
    log = _overlap(second)
    assert ("R", "write") in log and _after_a(log, "R")


def test_passes_on_different_systems_are_serialised_too():
    """A provision's post-pass classifies every system, so another system's
    provision post-pass must not interleave with it either."""
    def second(log):
        b = _Graph(_station_graph(), systems=("other",))
        arp.classify_all(query=lambda c, p=None: (log.append(("C", "read")), b.query(c, p))[1],
                         write=lambda c, p=None: (log.append(("C", "write")), b.write(c, p))[1])
    log = _overlap(second)
    # classify_all lists systems before taking the lock; the pass itself waits.
    first_c_write = next(i for i, e in enumerate(log) if e == ("C", "write"))
    last_a = max(i for i, (who, _) in enumerate(log) if who == "A")
    assert first_c_write > last_a


def test_the_lock_is_free_after_a_failed_pass():
    g = _Graph(_station_graph(), fail_write=arp._WRITE_MODELS)
    with pytest.raises(arp.RolePassError):
        arp.classify_system("hist", query=g.query, write=g.write)
    assert arp.ROLE_PASS_LOCK.acquire(blocking=False)
    arp.ROLE_PASS_LOCK.release()


def _reset_race(monkeypatch, reset):
    """A pass holds the lock inside its model write while ``reset()`` runs.
    Returns the AssetModelRole nodes left once both finish."""
    from agents import onboard
    assets = {r["id"]: dict(r) for r in _station_graph()}
    model_roles: dict = {}
    in_write, release = threading.Event(), threading.Event()

    def query(c, p=None):
        return {arp._READ_ASSETS: [dict(x) for x in assets.values()],
                arp._READ_MAPPINGS: [], arp._READ_OVERRIDES: []}[c]

    def write(c, p=None):
        if c == arp._WRITE_MODELS:
            if not in_write.is_set():
                in_write.set()
                assert release.wait(5), "test stuck"
            model_roles.update({r["model"]: r["role"] for r in p["rows"]})   # MERGE
        return True

    def fake_write(cypher, params=None):
        if "(a:Asset {system: $sid}) DETACH DELETE" in cypher or "MATCH (n:Asset)" in cypher:
            assets.clear()
        if "AssetModelRole" in cypher and "DETACH DELETE" in cypher:
            model_roles.clear()
        return True

    monkeypatch.setattr(onboard, "neptune_write", fake_write)
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(onboard, "core_concept_merges", lambda: [])
    tp = threading.Thread(target=lambda: arp.classify_system("hist", query=query, write=write))
    tp.start()
    assert in_write.wait(5)
    tr = threading.Thread(target=lambda: reset(onboard))
    tr.start()
    tr.join(0.3)
    assert tr.is_alive(), "the reset deleted while a pass held its write"
    release.set()
    tp.join(5)
    tr.join(5)
    assert not tp.is_alive() and not tr.is_alive()
    return model_roles


def test_a_system_reset_waits_for_a_running_pass(monkeypatch):
    """r8-1: otherwise the pass MERGEs AssetModelRole nodes back for a system
    the reset just removed, and no later pass sweeps them."""
    left = _reset_race(monkeypatch, lambda ob: ob._reset_delete_graph_artifacts("hist"))
    assert left == {}


def test_a_global_wipe_waits_for_a_running_pass(monkeypatch):
    left = _reset_race(monkeypatch, lambda ob: ob._global_reset_wipe_graph())
    assert left == {}
