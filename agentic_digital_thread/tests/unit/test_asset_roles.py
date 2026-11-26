# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Asset-model role classification (agents/asset_roles.py) on the tree shapes
sources actually expose. Names here are generic on purpose."""
from __future__ import annotations

from agents.asset_roles import (
    AREA, DATA_STREAM, ENTERPRISE, EQUIPMENT, EQUIPMENT_PART, SITE,
    IDENTIFIER_GAP, UNCLASSIFIED, WORK_CENTER, WORK_UNIT,
    AssetRow, asset_depths, classify_models, equipment_hits_from_mappings,
    matched_assets_from_mappings,
)
from agents.onboard_i3x import _level_for


class _Tree:
    """Tiny builder: add(model, name, parent_name) -> AssetRow list."""

    def __init__(self):
        self.rows: list[AssetRow] = []

    def add(self, model: str, name: str, parent: str | None = None) -> str:
        self.rows.append(AssetRow(id=f"id-{name}", name=name, model=model,
                                  parent_id=f"id-{parent}" if parent else None))
        return name


def _roles(rows, **kw):
    return {m: r.role for m, r in classify_models(rows, **kw).items()}


def _station_tier_tree() -> _Tree:
    """Site > Area > Process > {Machine, Station > Machine} > event/part leaves."""
    t = _Tree()
    for s in ("S1", "S2"):
        t.add("Site", s)
        t.add("Area", f"{s}-A", s)
        t.add("Process", f"{s}-P", f"{s}-A")
        for i in range(3):
            m = t.add("Machine", f"{s}-M{i}", f"{s}-P")
            t.add("State_Event", f"{m}-state", m)
            t.add("MetricEvent", f"{m}-metric", m)
            t.add("Counter", f"{m}-counter", m)
        st = t.add("Station", f"{s}-ST", f"{s}-P")
        for i in range(2):
            m = t.add("Machine", f"{s}-SM{i}", st)
            t.add("State_Event", f"{m}-state", m)
            t.add("Alarm_Event", f"{m}-alarm", m)
    return t


def test_station_tier_machines_are_equipment_at_every_depth():
    t = _station_tier_tree()
    roles = _roles(t.rows)
    assert roles == {
        "Site": SITE, "Area": AREA, "Process": WORK_CENTER, "Station": WORK_UNIT,
        "Machine": EQUIPMENT, "State_Event": DATA_STREAM, "MetricEvent": DATA_STREAM,
        "Alarm_Event": DATA_STREAM, "Counter": EQUIPMENT_PART,
    }
    classified = classify_models(t.rows)
    depths = asset_depths(t.rows)
    machine_depths = {depths[a.id] for a in t.rows if a.model == "Machine"}
    assert machine_depths == {3, 4}           # same model, two depths ...
    assert classified["Machine"].level == "equipment"   # ... one level
    assert classified["Station"].level == "work_unit"
    assert classified["Process"].level == "line"
    assert not any(r.needs_review for r in classified.values())


def _highbyte_tree(with_enterprise: bool) -> _Tree:
    """Enterprise > Site > Area > Line > one asset per process model."""
    t = _Tree()
    top = t.add("Enterprise", "E") if with_enterprise else None
    t.add("Site", "Plant-1", top)
    t.add("Area", "Area-1", "Plant-1")
    for line in ("Line-1", "Line-2"):
        t.add("Line", line, "Area-1")
        for proc in ("Mixing", "Filling", "Capping"):
            t.add(proc, f"{line}-{proc}", line)
    return t


def _walker_level(row: AssetRow, depth: int, rooted_at_enterprise: bool) -> str:
    """The level the walkers write today: i3X roots are 'enterprise' and its
    children use the parent's depth; SiteWise uses the child's own depth."""
    if rooted_at_enterprise:
        return "enterprise" if depth == 0 else _level_for(row.model, depth - 1)
    return _level_for(row.model, depth)


def test_highbyte_shape_levels_unchanged():
    for with_enterprise in (False, True):
        t = _highbyte_tree(with_enterprise)
        classified = classify_models(t.rows)
        depths = asset_depths(t.rows)
        for row in t.rows:
            assert classified[row.model].level == _walker_level(row, depths[row.id], with_enterprise), row
        assert classified["Mixing"].role == EQUIPMENT
        assert classified["Line"].role == WORK_CENTER
        if with_enterprise:
            assert classified["Enterprise"].role == ENTERPRISE


def test_single_top_level_asset_is_equipment_and_level_unchanged():
    t = _Tree()
    t.add("CNC", "Mill-1")
    classified = classify_models(t.rows)
    assert classified["CNC"].role == EQUIPMENT
    assert classified["CNC"].level == _level_for("CNC", 0) == "equipment"


def test_batch_hierarchy():
    """Site > Area > ProcessCell > Unit > EquipmentModule > control modules."""
    t = _Tree()
    t.add("Site", "Plant")
    t.add("Area", "Area-1", "Plant")
    t.add("ProcessCell", "Cell-1", "Area-1")
    for u in ("Reactor-1", "Reactor-2"):
        t.add("Unit", u, "Cell-1")
        for em in ("Dosing", "Heating"):
            e = t.add("EquipmentModule", f"{u}-{em}", u)
            t.add("Valve", f"{e}-valve", e)
            t.add("Pump", f"{e}-pump", e)
    assert _roles(t.rows) == {
        "Site": SITE, "Area": AREA, "ProcessCell": WORK_CENTER, "Unit": WORK_UNIT,
        "EquipmentModule": EQUIPMENT, "Valve": EQUIPMENT_PART, "Pump": EQUIPMENT_PART,
    }


def test_one_generic_type_stays_unclassified_for_review():
    t = _Tree()
    t.add("Node", "root")
    t.add("Node", "a", "root")
    t.add("Node", "a1", "a")
    t.add("Node", "a2", "a")
    classified = classify_models(t.rows)
    assert classified["Node"].role == UNCLASSIFIED
    assert classified["Node"].level is None        # the walker's label is kept
    assert classified["Node"].needs_review


def test_container_of_many_leaves_holds_equipment():
    """A cell holding several assets of one model: they are its equipment."""
    t = _Tree()
    t.add("Site", "Plant")
    t.add("Cell", "C1", "Plant")
    for i in range(4):
        t.add("Press", f"P{i}", "C1")
    assert _roles(t.rows) == {"Site": SITE, "Cell": WORK_CENTER, "Press": EQUIPMENT}


def test_value_evidence_decides_when_structure_is_ambiguous():
    """Area > Cell > one Machine per cell. Structure alone can't tell whether
    the cell is the equipment or holds it, so both stay unclassified;
    equipment identifiers in another system say the machines are the
    equipment."""
    t = _Tree()
    t.add("Area", "A")
    for i in range(3):
        t.add("Cell", f"C{i}", "A")
        t.add("Machine", f"M{i}", f"C{i}")
    alone = classify_models(t.rows)
    assert alone["Cell"].role == alone["Machine"].role == UNCLASSIFIED
    assert alone["Cell"].needs_review and alone["Cell"].level is None
    roles = _roles(t.rows, equipment_hits={"Machine": 2})
    assert roles == {"Area": AREA, "Cell": WORK_CENTER, "Machine": EQUIPMENT}


def test_value_evidence_keeps_the_topmost_model_on_a_branch():
    t = _station_tier_tree()
    classified = classify_models(t.rows, equipment_hits={"Machine": 4, "Alarm_Event": 1})
    assert classified["Machine"].role == EQUIPMENT
    assert classified["Machine"].source == "value_evidence"
    assert not classified["Machine"].needs_review
    assert classified["Alarm_Event"].role == DATA_STREAM
    flagged = classify_models(t.rows, equipment_hits={"Machine": 1, "Alarm_Event": 3})
    assert flagged["Machine"].role == EQUIPMENT and flagged["Machine"].needs_review


def test_exact_structural_names_beat_value_evidence():
    t = _station_tier_tree()
    assert _roles(t.rows, equipment_hits={"Site": 2})["Site"] == SITE


def test_steward_override_wins():
    t = _station_tier_tree()
    classified = classify_models(t.rows, overrides={"Station": EQUIPMENT, "Bogus": SITE,
                                                    "Area": "not-a-role"})
    assert classified["Station"].role == EQUIPMENT
    assert classified["Station"].source == "steward"
    assert classified["Machine"].role == EQUIPMENT_PART      # now below equipment
    assert classified["Area"].role == AREA
    assert "Bogus" not in classified


def test_top_level_holder_of_lines_is_a_site_flagged_for_review():
    t = _Tree()
    t.add("Plant", "P")
    t.add("Line", "L1", "P")
    t.add("Robot", "R1", "L1")
    classified = classify_models(t.rows)
    assert classified["Plant"].role == SITE and classified["Plant"].needs_review
    assert classified["Robot"].role == EQUIPMENT


def test_unnamed_containers_are_placed_from_the_equipment_up():
    """No tier names at all: Region > Zone > several Folders under one zone."""
    t = _Tree()
    t.add("Region", "R")
    t.add("Zone", "Z", "R")
    t.add("Zone", "Z2", "R")
    t.add("Folder", "F", "Z")
    t.add("Folder", "F2", "Z")
    classified = classify_models(t.rows)
    assert {m: r.role for m, r in classified.items()} == {
        "Folder": EQUIPMENT, "Zone": WORK_CENTER, "Region": SITE}
    assert classified["Region"].needs_review


def test_unnamed_chain_with_a_unit_tier():
    t = _Tree()
    t.add("Hall", "H")
    t.add("Bay", "B", "H")
    t.add("Rig", "R1", "B")
    t.add("Rig", "R2", "B")
    t.add("RigLog", "R1-log", "R1")
    t.add("RigLog", "R2-log", "R2")
    t.add("Bay", "B2", "H")
    t.add("Rig", "R3", "B2")
    t.add("RigLog", "R3-log", "R3")
    classified = classify_models(t.rows)
    assert {m: r.role for m, r in classified.items()} == {
        "Hall": SITE, "Bay": WORK_CENTER, "Rig": EQUIPMENT, "RigLog": DATA_STREAM}
    assert classified["Hall"].needs_review and classified["Bay"].needs_review
    assert not classified["Rig"].needs_review


def test_equipment_hits_from_mappings_matches_this_systems_side_only():
    t = _station_tier_tree()
    vms = [
        {"concept": "equipment-id", "from_system": "mes", "from_value": "X1",
         "to_system": "hist", "to_value": "id-S1-M0"},          # by source id
        {"concept": "equipment-id", "from_system": "hist", "from_value": "S1-SM0",
         "to_system": "mes", "to_value": "X2"},                  # by name, from side
        {"concept": "equipment", "from_system": "mes", "from_value": "X3",
         "to_system": "hist", "to_value": "S1-M0"},              # same asset again
        {"concept": "production-line", "from_system": "mes", "from_value": "S1",
         "to_system": "hist", "to_value": "S1"},                 # wrong concept
        {"concept": "equipment-id", "from_system": "mes", "from_value": "X4",
         "to_system": "other", "to_value": "S1-M1"},             # other system
        {"concept": "equipment-id", "from_system": "hist", "from_value": "S1-SM0-alarm",
         "to_system": "mes", "to_value": "X5"},
    ]
    assert equipment_hits_from_mappings(t.rows, vms, "hist") == {"Machine": 2, "Alarm_Event": 1}


def test_asset_depths_is_cycle_safe_and_ignores_unknown_parents():
    rows = [AssetRow("a", "a", "M", None), AssetRow("b", "b", "M", "a"),
            AssetRow("c", "c", "M", "b"), AssetRow("x", "x", "M", "y"),
            AssetRow("y", "y", "M", "x"), AssetRow("o", "o", "M", "missing")]
    d = asset_depths(rows)
    assert (d["a"], d["b"], d["c"], d["o"]) == (0, 1, 2, 0)
    assert set(d) == {"a", "b", "c", "x", "y", "o"}


def test_model_level_cycle_terminates():
    """Two models that contain each other (A > B > A > leaf) must not hang."""
    t = _Tree()
    t.add("Alpha", "a1")
    t.add("Beta", "b1", "a1")
    t.add("Alpha", "a2", "b1")
    t.add("Gauge", "g1", "a2")
    t.add("Gauge", "g2", "a2")
    classified = classify_models(t.rows)
    # Nothing in a cyclic model tree is placed by guessing.
    assert {m: r.role for m, r in classified.items()} == {
        "Alpha": UNCLASSIFIED, "Beta": UNCLASSIFIED, "Gauge": UNCLASSIFIED}
    assert all(r.needs_review for r in classified.values())


def test_unmatched_leaf_beside_matched_equipment_is_left_for_review():
    """Line > {Mixer, Oven, Metrics}: equipment identifiers land on the mixers
    and ovens, not on the metrics asset, which may be the line's own data. It
    stays equipment (as the walker labelled it) and is flagged; a missing
    identifier never demotes, since the other system may not track it."""
    t = _Tree()
    t.add("Site", "P")
    t.add("Area", "A", "P")
    for line in ("L1", "L2"):
        t.add("Line", line, "A")
        for model in ("Mixer", "Oven", "Metrics"):
            t.add(model, f"{line}-{model}", line)
    classified = classify_models(t.rows, equipment_hits={"Mixer": 2, "Oven": 1},
                                 matched_assets={"id-L1-Mixer", "id-L2-Mixer", "id-L1-Oven"})
    assert classified["Mixer"].role == classified["Oven"].role == EQUIPMENT
    assert classified["Metrics"].role == EQUIPMENT and classified["Metrics"].needs_review
    assert "identifiers" in classified["Metrics"].evidence
    assert classified["Metrics"].level == "equipment"
    # With no evidence at all, every leaf of the line is equipment, unflagged.
    plain = classify_models(t.rows)["Metrics"]
    assert plain.role == EQUIPMENT and not plain.needs_review
    # Counts alone (no matched asset ids) never flag a sibling.
    assert not classify_models(t.rows, equipment_hits={"Mixer": 2})["Metrics"].needs_review


def _plant_lines_tree(lines: int = 4) -> _Tree:
    """Site > Area > Line x N, each line holding repeated machines of several
    process models (the shape the workshop's SFC plant tree has)."""
    t = _Tree()
    t.add("Site", "P")
    t.add("Area", "A", "P")
    for n in range(lines):
        line = t.add("Line", f"L{n}", "A")
        for model in ("Lathe", "Lathe", "Rinser", "Deburr", "Gauge"):
            t.add(model, f"{line}-{model}-{len(t.rows)}", line)
    return t


def _one_lathe_on(t: _Tree, line: str) -> str:
    return next(a.id for a in t.rows if a.model == "Lathe" and a.parent_id == f"id-{line}")


def _identifier_flagged(role) -> bool:
    return role.needs_review and role.source == IDENTIFIER_GAP


def test_one_matched_machine_never_changes_its_siblings():
    """One cross-system identifier on one machine (module 4's single CMMS
    mapping) leaves every other model on every line exactly as it was."""
    t = _plant_lines_tree()
    before = classify_models(t.rows)
    after = classify_models(t.rows, equipment_hits={"Lathe": 1},
                            matched_assets={_one_lathe_on(t, "L1")})
    assert {m: r for m, r in after.items() if m != "Lathe"} == \
        {m: r for m, r in before.items() if m != "Lathe"}
    assert after["Lathe"].role == EQUIPMENT


def test_matches_on_some_lines_do_not_flag_models_on_others():
    """Matches on lines 0-2 but not line 3: Rinser sits in a parent with no
    matched sibling, so the identifiers say nothing about it."""
    t = _plant_lines_tree()
    matched = {_one_lathe_on(t, f"L{n}") for n in range(3)}
    roles = classify_models(t.rows, equipment_hits={"Lathe": 3}, matched_assets=matched)
    assert roles["Rinser"].role == EQUIPMENT and not _identifier_flagged(roles["Rinser"])


def test_a_match_cannot_touch_a_model_with_no_asset_beside_it():
    """Property: matched assets only count in the parent asset that holds
    them. A model with no asset in those parents is unchanged."""
    t = _plant_lines_tree(lines=3)
    t.add("Line", "L-solo", "A")
    t.add("Oven", "L-solo-oven-a", "L-solo")
    t.add("Oven", "L-solo-oven-b", "L-solo")
    baseline = classify_models(t.rows)["Oven"]
    every_lathe = {a.id for a in t.rows if a.model == "Lathe"}
    roles = classify_models(t.rows, equipment_hits={"Lathe": len(every_lathe)},
                            matched_assets=every_lathe)
    assert roles["Oven"] == baseline
    # Models beside a matched lathe in every line they sit in are flagged,
    # and stay equipment: a missing identifier never demotes.
    for m in ("Rinser", "Deburr", "Gauge"):
        assert roles[m].role == EQUIPMENT and _identifier_flagged(roles[m]), m
    assert UNCLASSIFIED not in {r.role for r in roles.values()}


def test_one_parent_is_enough_to_flag_but_never_to_demote():
    """r4-1: one line, its presses all matched, one unmatched Metrics leaf.
    Flagged (it may be the line's own data), still equipment."""
    t = _Tree()
    t.add("Site", "P")
    t.add("Line", "L", "P")
    for i in range(3):
        t.add("Press", f"Press-{i}", "L")
    t.add("Metrics", "L-metrics", "L")
    roles = classify_models(t.rows, equipment_hits={"Press": 3},
                            matched_assets={f"id-Press-{i}" for i in range(3)})
    assert roles["Metrics"].role == EQUIPMENT and _identifier_flagged(roles["Metrics"])
    assert roles["Metrics"].source == IDENTIFIER_GAP
    assert roles["Press"].source == "value_evidence"


# ── edge cases: one-off machines, mixed populations, ambiguous models ──

def test_station_holding_one_bare_machine_is_left_unclassified():
    """Site > Area > Process > Station > Machine (one leaf machine per station,
    no streams): structure alone cannot say which is the equipment."""
    t = _Tree()
    t.add("Site", "S")
    t.add("Area", "A", "S")
    t.add("Process", "P", "A")
    for i in range(3):
        t.add("Station", f"ST{i}", "P")
        t.add("Machine", f"M{i}", f"ST{i}")
    classified = classify_models(t.rows)
    assert classified["Station"].role == UNCLASSIFIED and classified["Station"].needs_review
    assert classified["Machine"].role == UNCLASSIFIED
    # Equipment identifiers on the machines settle it.
    settled = classify_models(t.rows, equipment_hits={"Machine": 2})
    assert (settled["Machine"].role, settled["Station"].role, settled["Process"].role) == \
        (EQUIPMENT, WORK_UNIT, WORK_CENTER)


def test_several_kinds_of_one_off_leaf_is_equipment_flagged():
    t = _Tree()
    t.add("Line", "L")
    for i in range(2):
        t.add("Press", f"P{i}", "L")
        t.add("Motor", f"P{i}-motor", f"P{i}")
        t.add("Die", f"P{i}-die", f"P{i}")
    classified = classify_models(t.rows)
    assert classified["Press"].role == EQUIPMENT and classified["Press"].needs_review
    assert classified["Motor"].role == classified["Die"].role == EQUIPMENT_PART


def test_population_marks_only_the_repeating_model():
    """Site > Cell > {Machine, Machine, Metadata}: the one-off Metadata is not
    swept into equipment by its repeating sibling."""
    t = _Tree()
    t.add("Site", "S")
    t.add("Cell", "C", "S")
    t.add("Machine", "M1", "C")
    t.add("Machine", "M2", "C")
    t.add("Metadata", "C-meta", "C")
    classified = classify_models(t.rows)
    assert classified["Machine"].role == EQUIPMENT and not classified["Machine"].needs_review
    assert classified["Cell"].role == WORK_CENTER
    # Repetition beside it is weak evidence: kept as equipment, flagged.
    assert classified["Metadata"].role == EQUIPMENT and classified["Metadata"].needs_review


def test_one_off_machines_beside_a_repeating_one_stay_equipment():
    """Line > {Press, Press, Oven, Grinder} with no identifiers anywhere: an
    ordinary line layout, so Oven and Grinder stay equipment (flagged)."""
    t = _Tree()
    t.add("Site", "S")
    t.add("Area", "A", "S")
    for line in ("L1", "L2"):
        t.add("Line", line, "A")
        for model in ("Press", "Press", "Oven", "Grinder"):
            t.add(model, f"{line}-{model}-{len(t.rows)}", line)
    classified = classify_models(t.rows)
    assert {m: classified[m].role for m in ("Press", "Oven", "Grinder")} == \
        {"Press": EQUIPMENT, "Oven": EQUIPMENT, "Grinder": EQUIPMENT}
    assert classified["Oven"].needs_review and not classified["Press"].needs_review


def test_model_in_two_parent_positions_is_left_for_review():
    """A Unit holding machines sits directly under a site in one place and
    inside a line in another: one role cannot cover both."""
    t = _Tree()
    t.add("Site", "S")
    t.add("Area", "A", "S")
    t.add("Line", "L", "A")
    for parent, name in (("S", "U1"), ("L", "U2")):
        t.add("Unit", name, parent)
        t.add("Machine", f"{name}-m1", name)
        t.add("Machine", f"{name}-m2", name)
    classified = classify_models(t.rows)
    assert classified["Unit"].role == UNCLASSIFIED and classified["Unit"].needs_review
    assert classified["Machine"].role == EQUIPMENT


def test_mapping_values_never_fan_out_to_ambiguous_assets():
    rows = [AssetRow("id-1", "Pump-1", "Pump", None), AssetRow("id-2", "Pump-1", "Pump", None),
            AssetRow("id-3", "id-4", "Meta", None), AssetRow("id-4", "Valve-1", "Valve", None),
            AssetRow("id-5", "Fan-1", "Fan", None)]
    vms = [
        {"concept": "equipment-id", "to_system": "h", "to_value": "Pump-1"},   # shared name
        {"concept": "equipment-id", "to_system": "h", "to_value": "id-4"},     # id AND a name
        {"concept": "equipment-id", "to_system": "h", "to_value": "Fan-1"},    # unique name
        {"concept": "equipment-id", "to_system": "h", "to_value": "Pump-1",
         "to_element_id": "id-2"},                                              # exact element id
    ]
    assert equipment_hits_from_mappings(rows, vms, "h") == {"Fan": 1, "Pump": 1}
    assert matched_assets_from_mappings(rows, vms, "h") == {"id-2", "id-5"}
