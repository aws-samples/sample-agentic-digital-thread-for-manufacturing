# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The frozen story's MES -> PLM wildcard rule holds with a MARGIN (ADR-0086).

The frozen MES seed (lambda-adt-seed 003) keys parts by a bare product code
(``7742``); the PLM seed (005) keys the same parts by revisioned item ids
(``PN-7742-REV-C``). ``agents.identity_affinity`` derives the one-to-many rule
``{id}`` -> ``PN-{id}-REV-{*}`` from the VALUES. A wildcard needs
``required_one_to_many(agreeing)`` cores that carry several revisions; with
only two multi-revision parts the frozen rule sat exactly on that floor, so
one part losing a revision would silently drop the rule.

The Hydraulic Manifold Block (``5520``, revisions A and B) adds a third
multi-revision core: 4 agreeing cores, 3 of them one-to-many, against a
required 2 -- a margin of one core. These pins run the SAME pure functions
onboarding runs over every seeded value (the SQL inserts are parsed, never
executed), so a seed edit that erodes the margin fails here, offline.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from agents import identity_affinity as aff

_SEED_DIR = Path(__file__).resolve().parents[3] / "lambda-adt-seed"
_MES_SQL = (_SEED_DIR / "003_mes_line3.sql").read_text(encoding="utf-8")
_PLM_SQL = (_SEED_DIR / "005_plm_engineering.sql").read_text(encoding="utf-8")

#: One SQL literal: a quoted string ('' escapes), NULL, or a bare number.
_FIELD = re.compile(r"'((?:[^']|'')*)'|(NULL)|(-?[\d.]+)")
_NEW_CORE = "5520"


def _unquote(match: re.Match) -> str | None:
    quoted, null, number = match.groups()
    if null:
        return None
    return number if number is not None else quoted.replace("''", "'")


def _rows(sql: str, table: str) -> list[list]:
    """Every VALUES row of every ``INSERT INTO <table>`` statement (one row
    per line in these seeds), each as a list of unquoted fields."""
    rows = []
    for block in re.findall(rf"INSERT INTO {table}\b.*?VALUES\s*(.*?);[ \t]*$", sql, re.S | re.M):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("("):
                rows.append([_unquote(m) for m in _FIELD.finditer(line)])
    assert rows, f"no {table} rows parsed -- seed layout changed?"
    return rows


def _mes_runs() -> list[dict]:
    keys = ("batch", "work_order", "line", "equipment", "product_code")
    return [dict(zip(keys, row)) for row in _rows(_MES_SQL, "production_runs")]


def _plm_items() -> list[dict]:
    keys = ("item_id", "name", "description", "item_type", "revision", "lifecycle")
    return [dict(zip(keys, row)) for row in _rows(_PLM_SQL, "products")]


def _fields() -> tuple[aff.SampledField, aff.SampledField]:
    codes = tuple(sorted({r["product_code"] for r in _mes_runs()}))
    item_ids = tuple(sorted({i["item_id"] for i in _plm_items()}))
    return (aff.SampledField("mes", "production_runs", "product_code", "part", codes),
            aff.SampledField("plm", "products", "item_id", "part", item_ids))


def _revisions_by_core() -> dict[str, list[dict]]:
    mes, _plm = _fields()
    return {code: [i for i in _plm_items() if code in aff.digit_cores(i["item_id"])]
            for code in mes.values}


def test_the_frozen_mes_makes_four_parts():
    assert set(_fields()[0].values) == {"7742", "8810", "9901", _NEW_CORE}


def test_each_core_carries_its_revision_count():
    counts = {core: len(items) for core, items in _revisions_by_core().items()}
    assert counts == {"7742": 3, "8810": 2, "9901": 1, _NEW_CORE: 2}


def test_no_digit_core_collides_with_another_plm_item():
    """A core names ONE part family: every PLM item carrying it is a revision
    of ``PN-<core>-REV-*`` (a sub-component sharing the digits would be a
    false sibling)."""
    for core, items in _revisions_by_core().items():
        assert all(re.fullmatch(rf"PN-{core}-REV-[A-Z]", i["item_id"]) for i in items), items


def test_each_multi_revision_core_has_exactly_one_released_revision():
    for core, items in _revisions_by_core().items():
        states = Counter(i["lifecycle"] for i in items)
        assert states["Released"] == 1, (core, states)
        assert set(states) <= {"Released", "Obsolete"}, (core, states)


def test_the_new_part_is_one_family_across_its_revisions():
    items = _revisions_by_core()[_NEW_CORE]
    assert {i["item_id"]: i["lifecycle"] for i in items} == {
        "PN-5520-REV-A": "Obsolete", "PN-5520-REV-B": "Released"}
    assert {i["name"] for i in items} == {"Hydraulic Manifold Block"}
    assert {i["item_type"] for i in items} == {"Part"}


def test_the_wildcard_rule_holds_with_a_margin_of_one_core():
    mes, plm = _fields()
    groups = aff.matched_groups(mes, plm)
    assert [core for core, _s, _t in groups] == sorted(mes.values)
    agreeing = len(groups)
    one_to_many = sum(1 for _core, sources, targets in groups
                      if len(sources) > 1 or len(targets) > 1)
    assert (agreeing, one_to_many) == (4, 3)
    assert aff.required_one_to_many(agreeing) == 2
    assert one_to_many - aff.required_one_to_many(agreeing) == 1

    rule = aff.affinity_rule(mes, plm)
    assert rule is not None
    assert (rule["source_pattern"], rule["target_pattern"]) == ("{id}", "PN-{id}-REV-{*}")
    assert rule["method"] == aff.AFFINITY_METHOD
    assert rule["confidence"] == aff.AFFINITY_CONFIDENCE
    assert rule["reasoning"].startswith(
        "4 of 4 sampled identifier cores share one transform; "
        "3 of 4 cores match several target values (up to 3)")


def test_the_rule_survives_losing_any_one_multi_revision_part():
    """The margin, stated as behaviour: drop the extra revisions of any ONE
    multi-revision part and the wildcard rule is still derived."""
    mes, plm = _fields()
    for core, items in _revisions_by_core().items():
        if len(items) < 2:
            continue
        extra = {i["item_id"] for i in items if i["lifecycle"] != "Released"}
        thinned = aff.SampledField(plm.system_id, plm.table, plm.field, plm.concept,
                                   tuple(v for v in plm.values if v not in extra))
        rule = aff.affinity_rule(mes, thinned)
        assert rule is not None and rule["target_pattern"] == "PN-{id}-REV-{*}", core


def test_the_new_runs_leave_the_defect_chain_rows_alone():
    """The manifold runs sit on their own machine, never on the story mill
    or the story batch/work order (the B-2847 chain is test-pinned)."""
    new = [r for r in _mes_runs() if r["product_code"] == _NEW_CORE]
    assert len(new) == 8
    assert {r["equipment"] for r in new} == {"L7-M2"}
    assert {r["line"] for r in new} == {"L7"}
    assert not {r["batch"] for r in new} & {"B-2847"}
    assert not {r["work_order"] for r in new} & {"WO-9921"}
    others = [r for r in _mes_runs() if r["product_code"] != _NEW_CORE]
    assert not {r["batch"] for r in new} & {r["batch"] for r in others}
    assert not {r["work_order"] for r in new} & {r["work_order"] for r in others}
    assert all(r["equipment"] != "L7-M2" for r in others)
