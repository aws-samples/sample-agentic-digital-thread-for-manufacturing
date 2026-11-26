# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``metric_dimension``'s DETERMINISTIC co-located
dimension pick (``_prefer_colocated_mapping`` / ``_select_dimension_mapping``).

When two+ columns on the measure table map the same dimension concept, the
resolver used to fail loud ("ambiguous co-located dimension mappings"). It now
picks ONE candidate by a TOTAL order (never ambiguous):

  1. MAPS_TO ``role == 'identifier'`` beats any other role;
  2. then the highest mapping ``confidence`` (``None`` ranks below any number);
  3. then the lexicographically smallest column name (final tiebreak).

All synthetic names; no graph / DB / AWS is touched.
"""
from __future__ import annotations

import agents.metric_dimension as md
from agents.metric_resolver import MappingProvenance


def _mp(column, role="dimension", confidence=1.0, table="readings_log"):
    return MappingProvenance(table=table, column=column, role=role,
                             confidence=confidence, source="user_confirmed")


_MEASURE = _mp("reading_value", role="measure", confidence=0.9)


# --- level 1: identifier role beats any other role ---------------------------

def test_identifier_role_beats_higher_confidence_and_smaller_name():
    # 'zone_ref' is role=identifier with LOW confidence and a LARGER name —
    # it must still win over a high-confidence, lexically-smaller 'area_code'.
    cands = [
        _mp("area_code", role="dimension", confidence=1.0),
        _mp("zone_ref", role="identifier", confidence=0.2),
    ]
    assert md._prefer_colocated_mapping(cands).column == "zone_ref"


def test_identifier_tie_falls_through_to_confidence():
    # BOTH are identifiers → level 1 ties, level 2 (confidence) decides.
    cands = [
        _mp("zone_ref", role="identifier", confidence=0.4),
        _mp("area_code", role="identifier", confidence=0.9),
    ]
    assert md._prefer_colocated_mapping(cands).column == "area_code"


# --- level 2: highest confidence among equal roles ---------------------------

def test_highest_confidence_wins_among_equal_roles():
    cands = [
        _mp("area_code", confidence=0.7),
        _mp("zone_ref", confidence=0.95),
    ]
    assert md._prefer_colocated_mapping(cands).column == "zone_ref"


def test_none_confidence_ranks_below_any_number():
    cands = [
        _mp("area_code", confidence=None),
        _mp("zone_ref", confidence=0.1),
    ]
    assert md._prefer_colocated_mapping(cands).column == "zone_ref"


# --- level 3: lexicographically smallest column name (total order) -----------

def test_lexicographic_column_final_tiebreak():
    cands = [
        _mp("zone_ref", confidence=1.0),
        _mp("area_code", confidence=1.0),
    ]
    assert md._prefer_colocated_mapping(cands).column == "area_code"


def test_all_none_confidence_lexicographic():
    cands = [
        _mp("zone_ref", confidence=None),
        _mp("area_code", confidence=None),
    ]
    assert md._prefer_colocated_mapping(cands).column == "area_code"


# --- through _select_dimension_mapping: co-located pick, no join -------------

def test_select_dimension_mapping_colocated_tie_returns_pick_no_join():
    cands = [
        _mp("area_code", role="dimension", confidence=1.0),
        _mp("zone_ref", role="identifier", confidence=0.5),
    ]
    chosen, join = md._select_dimension_mapping(
        _MEASURE, cands, "sys-a",
        query=lambda c, p=None: (_ for _ in ()).throw(
            AssertionError("no graph read needed for a co-located pick")),
    )
    assert chosen.column == "zone_ref"
    assert join is None


def test_select_dimension_mapping_single_colocated_unchanged():
    # ONE co-located candidate → returned as-is, no join (pre-existing behavior).
    chosen, join = md._select_dimension_mapping(
        _MEASURE, [_mp("area_code")], "sys-a",
        query=lambda c, p=None: [],
    )
    assert chosen.column == "area_code"
    assert join is None
