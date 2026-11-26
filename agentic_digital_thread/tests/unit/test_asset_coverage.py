# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The per-site asset coverage summary (agents/asset_coverage.py): pure."""
from __future__ import annotations

import json

from agents import asset_coverage as ac
from agents.asset_roles import AssetRow


def _tree(*nodes):
    """``(id, name, parent, level)`` tuples -> (placed rows, level by id)."""
    rows = [AssetRow(id=i, name=n, model="M", parent_id=p) for i, n, p, _lvl in nodes]
    return rows, {i: lvl for i, _n, _p, lvl in nodes}


def _two_sites():
    return _tree(
        ("e", "Enterprise 1", None, "enterprise"),
        ("sa", "Site A", "e", "site"),
        ("a1", "Area 1", "sa", "area"),
        ("l1", "Line 1", "a1", "line"),
        ("l2", "Line 2", "a1", "line"),
        ("m1", "Machine 1", "l1", "equipment"),
        ("m2", "Machine 2", "l1", "equipment"),
        ("m3", "Machine 3", "l2", "equipment"),
        ("sb", "Site B", "e", "site"),
        ("a2", "Area 2", "sb", "area"),
        ("l3", "Line 3", "a2", "line"),
        ("m4", "Machine 4", "l3", "equipment"),
        ("c4", "Part 4", "m4", "component"),
    )


def test_counts_every_descendant_of_each_site_by_stored_level():
    rows, levels = _two_sites()
    cov = ac.compute_coverage(rows, levels, unplaced=0)
    assert cov["anchor"] == "site"
    assert cov["sites"] == [
        {"name": "Site A", "total": 6, "levels": {"area": 1, "line": 2, "equipment": 3}},
        {"name": "Site B", "total": 4,
         "levels": {"area": 1, "line": 1, "equipment": 1, "component": 1}},
    ]
    assert "more_sites" not in cov and "unplaced" not in cov


def test_without_a_site_level_the_roots_are_the_anchors():
    rows, levels = _tree(
        ("r1", "Line 1", None, "line"),
        ("m1", "Machine 1", "r1", "equipment"),
        ("m2", "Machine 2", "r1", "equipment"),
        ("r2", "Line 2", None, "line"),
    )
    cov = ac.compute_coverage(rows, levels, unplaced=0)
    assert cov["anchor"] == "root"
    # An anchor with nothing below it is kept with total 0, nothing invented.
    assert cov["sites"] == [{"name": "Line 1", "total": 2, "levels": {"equipment": 2}},
                            {"name": "Line 2", "total": 0, "levels": {}}]


def test_a_blank_level_counts_as_other():
    rows, levels = _tree(("s", "Site A", None, "site"), ("x", "Thing", "s", ""),
                         ("y", "Thing 2", "s", None))
    cov = ac.compute_coverage(rows, levels, unplaced=0)
    assert cov["sites"][0]["levels"] == {"other": 2}


def test_duplicate_anchor_names_are_told_apart_by_path_and_ordered():
    rows, levels = _tree(
        ("e2", "Enterprise 2", None, "enterprise"),
        ("s2", "Site A", "e2", "site"),
        ("e1", "Enterprise 1", None, "enterprise"),
        ("s1", "Site A", "e1", "site"),
        ("m1", "Machine 1", "s1", "equipment"),
        ("s3", "Site C", "e1", "site"),
    )
    cov = ac.compute_coverage(rows, levels, unplaced=0)
    assert [s["name"] for s in cov["sites"]] == [
        "Enterprise 1/Site A", "Enterprise 2/Site A", "Site C"]
    assert cov["sites"][0]["total"] == 1


def test_anchor_cap_keeps_eight_in_name_order_and_discloses_the_rest():
    nodes = [("e", "Enterprise 1", None, "enterprise")]
    nodes += [(f"s{i}", f"Site {i:02d}", "e", "site") for i in range(10, 0, -1)]
    rows, levels = _tree(*nodes)
    cov = ac.compute_coverage(rows, levels, unplaced=0)
    assert ac.ANCHOR_CAP == 8
    assert [s["name"] for s in cov["sites"]] == [f"Site {i:02d}" for i in range(1, 9)]
    assert cov["more_sites"] == 2


def test_unplaced_assets_are_disclosed_not_counted():
    rows, levels = _two_sites()
    cov = ac.compute_coverage(rows, levels, unplaced=3)
    assert cov["unplaced"] == 3
    assert sum(s["total"] for s in cov["sites"]) == 10


def test_nothing_at_all_is_none():
    assert ac.compute_coverage([], {}, unplaced=0) is None


def test_only_unplaced_assets_still_say_so():
    cov = ac.compute_coverage([], {}, unplaced=4)
    assert cov == {"anchor": "root", "sites": [], "unplaced": 4}


def test_serialized_form_is_one_compact_json_string():
    rows, levels = _two_sites()
    raw = ac.coverage_json(rows, levels, unplaced=0)
    assert isinstance(raw, str) and " " not in raw.replace("Site A", "").replace(
        "Site B", "")
    assert json.loads(raw) == ac.compute_coverage(rows, levels, unplaced=0)


def test_length_cap_drops_anchors_from_the_end_never_cuts_mid_json():
    nodes = [("e", "Enterprise 1", None, "enterprise")]
    for i in range(8):
        nodes.append((f"s{i}", f"Site {i} " + "x" * 80, "e", "site"))
        for lvl in ("area", "line", "work_unit", "equipment", "component", "lvl_a",
                    "lvl_b", "lvl_c", "lvl_d", "lvl_e"):
            nodes.append((f"s{i}-{lvl}", f"{lvl} {i}", f"s{i}", lvl))
    rows, levels = _tree(*nodes)
    raw = ac.coverage_json(rows, levels, unplaced=0)
    assert len(raw) <= ac.JSON_CAP
    cov = json.loads(raw)
    assert len(cov["sites"]) < 8
    assert len(cov["sites"]) + cov["more_sites"] == 8
    # The kept anchors are the FIRST ones in order.
    assert [s["name"][:6] for s in cov["sites"]] == [f"Site {i}" for i in range(len(cov["sites"]))]


def test_a_very_long_anchor_name_is_shortened_not_dropped():
    rows, levels = _tree(("s", "S" * 500, None, "site"))
    cov = json.loads(ac.coverage_json(rows, levels, unplaced=0))
    assert len(cov["sites"]) == 1 and len(cov["sites"][0]["name"]) <= ac.NAME_CAP


def test_render_is_one_compact_line_in_hierarchy_order():
    rows, levels = _two_sites()
    raw = ac.coverage_json(rows, levels, unplaced=0)
    assert ac.render_coverage(raw) == (
        "Site A: 1 area, 2 lines, 3 equipment; "
        "Site B: 1 area, 1 line, 1 equipment, 1 component")


def test_render_discloses_more_sites_unplaced_and_empty_anchors():
    raw = json.dumps({"anchor": "site", "sites": [
        {"name": "Site A", "total": 0, "levels": {}},
        {"name": "Site B", "total": 3, "levels": {"work_unit": 2, "facility": 1}}],
        "more_sites": 2, "unplaced": 5})
    assert ac.render_coverage(raw) == (
        "Site A: none; Site B: 2 work units, 1 facility; +2 more sites; "
        "5 assets not placed in the hierarchy (not counted)")


def test_render_plural_rules_are_generic():
    raw = json.dumps({"anchor": "site", "sites": [
        {"name": "Site A", "total": 9,
         "levels": {"facility": 2, "box": 3, "other": 4}}]})
    assert ac.render_coverage(raw) == "Site A: 3 boxes, 2 facilities, 4 other"


def test_render_says_top_level_assets_when_there_are_no_sites():
    raw = json.dumps({"anchor": "root", "sites": [
        {"name": "Line 1", "total": 2, "levels": {"equipment": 2}}], "more_sites": 1})
    assert ac.render_coverage(raw) == (
        "top-level assets (no site level): Line 1: 2 equipment; +1 more top-level asset")


def test_malformed_or_missing_coverage_renders_nothing():
    for raw in (None, "", "{not json", "[]", "3", json.dumps({"sites": "x"}),
                json.dumps({"sites": [{"name": 3}]}),
                json.dumps({"sites": [{"name": "A", "total": "x", "levels": {}}]}),
                json.dumps({"sites": [{"name": "A", "total": 1, "levels": {"a": "1"}}]}),
                json.dumps({"sites": [], "more_sites": "2"}),
                json.dumps({"sites": []})):
        assert ac.render_coverage(raw) is None, raw


def test_same_parent_duplicate_names_get_an_id_suffix():
    rows, levels = _tree(
        ("e", "Enterprise 1", None, "enterprise"),
        ("site-0000aaaa", "Site A", "e", "site"),
        ("m1", "Machine 1", "site-0000aaaa", "equipment"),
        ("site-0000bbbb", "Site A", "e", "site"),
        ("m2", "Machine 2", "site-0000bbbb", "equipment"),
    )
    names = [s["name"] for s in ac.compute_coverage(rows, levels, unplaced=0)["sites"]]
    assert names == ["Enterprise 1/Site A [0000aaaa]", "Enterprise 1/Site A [0000bbbb]"]
    rendered = ac.render_coverage(ac.coverage_json(rows, levels, unplaced=0))
    assert rendered.count("[0000aaaa]: 1 equipment") == 1
    assert rendered.count("[0000bbbb]: 1 equipment") == 1


def test_names_that_collide_only_after_truncation_get_a_bounded_suffix():
    stem = "S" * (ac.NAME_CAP + 10)
    rows, levels = _tree(("id-11111111", stem + "1", None, "site"),
                         ("id-22222222", stem + "2", None, "site"))
    names = [s["name"] for s in ac.compute_coverage(rows, levels, unplaced=0)["sites"]]
    assert len(set(names)) == 2
    assert all(len(n) <= ac.NAME_CAP for n in names)
    assert names[0].endswith(" [11111111]") and names[1].endswith(" [22222222]")


def test_ids_sharing_the_last_eight_chars_still_get_unique_labels():
    rows, levels = _tree(
        ("e", "Enterprise 1", None, "enterprise"),
        ("a-12345678", "Site A", "e", "site"),
        ("b-12345678", "Site A", "e", "site"),
    )
    names = [s["name"] for s in ac.compute_coverage(rows, levels, unplaced=0)["sites"]]
    assert len(set(names)) == 2
    assert all(len(n) <= ac.NAME_CAP for n in names)
    assert sorted(names) == ["Enterprise 1/Site A [a-12345678]", "Enterprise 1/Site A [b-12345678]"]


def test_ids_sharing_a_long_tail_fall_back_to_a_unique_bounded_label():
    tail = "x" * 60
    rows, levels = _tree(
        ("e", "Enterprise 1", None, "enterprise"),
        ("a" + tail, "Site A", "e", "site"),
        ("b" + tail, "Site A", "e", "site"),
        ("c" + tail, "Site A", "e", "site"),
    )
    names = [s["name"] for s in ac.compute_coverage(rows, levels, unplaced=0)["sites"]]
    assert len(set(names)) == 3
    assert all(len(n) <= ac.NAME_CAP for n in names)
