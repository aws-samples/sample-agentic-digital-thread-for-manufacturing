# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for SAME-SYSTEM (intra-system) join discovery — the G1 engine.

``discover_intra_system`` runs the transform ladder across DIFFERENT tables of
ONE system and RETURNS candidates — it never writes (the provision caller
decides direct-write vs proposal). ``discover_pair`` keeps its cross-system-only
guard.

No AWS / Neptune / LLM / network — graph reads and value sampling are
monkeypatched module seams; the proposal seam is wired only to prove it is
NEVER called. GENERIC — synthetic systems / fields / values only (fixtures
mirror the live-evidence SHAPE: a plant-prefixed line id shared between a
transactional table and its dimension table)."""
from __future__ import annotations

import pytest

import agents.join_discovery as jd


# --------------------------------------------------------------------------- #
# Fixture: ONE system, three tables.
#   - work_orders.line_id and production_lines.line_id share IDENTICAL values
#     (the live-evidence shape: plant-prefixed line ids, e.g. DET-L1) → a real
#     intra-system join via the identity rung.
#   - measurements.reject_quantity carries a stale role='identifier' AND values
#     overlapping work_orders.line_id → the defensive measure gate must exclude it.
#   - work_orders has a SECOND identifier column (wo_ref) sharing values with
#     work_orders.line_id → same-table pairs must be skipped.
# --------------------------------------------------------------------------- #
LINE_IDS = ["DET-L1", "DET-L2", "DET-L3", "DET-L4", "DET-L5", "DET-L6"]

FIELDS = [
    {"table": "work_orders", "field": "line_id", "concept": "line-id",
     "role": "identifier", "data_type": "varchar"},
    {"table": "work_orders", "field": "wo_ref", "concept": "",
     "role": "identifier", "data_type": "varchar"},
    {"table": "production_lines", "field": "line_id", "concept": "line-id",
     "role": "identifier", "data_type": "varchar"},
    # Stale graph role: a measure column marked identifier — must never pair.
    {"table": "measurements", "field": "reject_quantity", "concept": "",
     "role": "identifier", "data_type": "varchar"},
]

SAMPLES = {
    ("sys-x", "work_orders", "line_id"): list(LINE_IDS),
    ("sys-x", "work_orders", "wo_ref"): list(LINE_IDS),          # same-table twin
    ("sys-x", "production_lines", "line_id"): list(LINE_IDS),
    ("sys-x", "measurements", "reject_quantity"): list(LINE_IDS),  # overlapping bait
}


@pytest.fixture
def wired(monkeypatch):
    """Wire the synthetic system; capture any (forbidden) proposal writes."""
    proposals: list[dict] = []
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: [dict(f) for f in FIELDS])
    monkeypatch.setattr(jd, "_sample",
                        lambda sid, tbl, fld: list(SAMPLES.get((sid, tbl, fld), [])))

    def _fake_log(**kwargs):
        proposals.append(kwargs)
        return f"m{len(proposals)}"

    monkeypatch.setattr(jd, "_log_proposed_mapping", _fake_log)
    return proposals


def _pair_tables(pair):
    return {(pair["from_table"], pair["from_field"]),
            (pair["to_table"], pair["to_field"])}


# =========================================================================== #
# The engine finds the cross-table pair (live-evidence shape)
# =========================================================================== #
def test_cross_table_identity_pair_is_returned(wired):
    r = jd.discover_intra_system("sys-x")
    line_pairs = [p for p in r["pairs"]
                  if _pair_tables(p) == {("work_orders", "line_id"),
                                         ("production_lines", "line_id")}]
    assert len(line_pairs) == 1
    p = line_pairs[0]
    assert p["transform"] == jd.TRANSFORM_IDENTITY
    assert p["overlap_count"] == len(LINE_IDS)
    assert p["overlap_ratio"] == 1.0
    assert p["examples"]  # measured evidence carried for provenance


def test_transform_ladder_applies_across_tables(monkeypatch, wired):
    # One table carries a systematic plant prefix the other omits — the
    # strip_prefix rung must still discover the pair (LINE1 → DET-L1 style).
    monkeypatch.setattr(jd, "_sample", lambda sid, tbl, fld: {
        ("sys-x", "work_orders", "line_id"):
            ["DET-L1", "DET-L2", "DET-L3", "DET-L4", "DET-L5", "DET-L6"],
        ("sys-x", "production_lines", "line_id"):
            ["L1", "L2", "L3", "L4", "L5", "L6"],
    }.get((sid, tbl, fld), []))
    r = jd.discover_intra_system("sys-x")
    line_pairs = [p for p in r["pairs"]
                  if _pair_tables(p) == {("work_orders", "line_id"),
                                         ("production_lines", "line_id")}]
    assert len(line_pairs) == 1
    assert line_pairs[0]["transform"] == "strip_prefix:DET-"


# =========================================================================== #
# Exclusions: measure gate, same-table skip, no-overlap decline
# =========================================================================== #
def test_measure_like_endpoint_is_excluded(wired):
    # reject_quantity overlaps work_orders.line_id values AND carries a stale
    # role='identifier' — the defensive gate must still exclude it.
    r = jd.discover_intra_system("sys-x")
    for p in r["pairs"]:
        assert "reject_quantity" not in (p["from_field"], p["to_field"])


def test_same_table_pair_is_never_returned(wired):
    # work_orders.line_id and work_orders.wo_ref share ALL values — but a
    # same-table pair is a same-row identifier set (provision's turf), not a join.
    r = jd.discover_intra_system("sys-x")
    for p in r["pairs"]:
        assert p["from_table"] != p["to_table"]


def test_disjoint_values_decline(monkeypatch, wired):
    monkeypatch.setattr(jd, "_sample", lambda sid, tbl, fld: {
        ("sys-x", "work_orders", "line_id"): ["A1", "A2", "A3", "A4", "A5"],
        ("sys-x", "production_lines", "line_id"): ["Z6", "Z7", "Z8", "Z9", "Z10"],
    }.get((sid, tbl, fld), []))
    assert jd.discover_intra_system("sys-x") == {"pairs": []}


# =========================================================================== #
# Contract: returns candidates, NEVER writes; best-effort; guard rails
# =========================================================================== #
def test_returns_candidates_without_writing(wired):
    r = jd.discover_intra_system("sys-x")
    assert r["pairs"]  # candidates found …
    assert wired == []  # … and NOT ONE proposal was written


def test_sampling_error_is_isolated_per_field(monkeypatch, wired):
    # A raising sample on ONE field must not abort the system — the remaining
    # cross-table pair is still found.
    def _sample(sid, tbl, fld):
        if tbl == "measurements":
            raise RuntimeError("connection reset")
        return list(SAMPLES.get((sid, tbl, fld), []))

    monkeypatch.setattr(jd, "_sample", _sample)
    r = jd.discover_intra_system("sys-x")
    assert any(_pair_tables(p) == {("work_orders", "line_id"),
                                   ("production_lines", "line_id")}
               for p in r["pairs"])


def test_empty_system_id_returns_empty(wired):
    assert jd.discover_intra_system("") == {"pairs": []}


def test_single_field_system_returns_empty(monkeypatch, wired):
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: [dict(FIELDS[0])])
    assert jd.discover_intra_system("sys-x") == {"pairs": []}


def test_discover_pair_keeps_cross_system_only_guard(wired):
    # The same-system guard on discover_pair is DELIBERATE and must stay:
    # intra-system discovery goes through discover_intra_system only.
    assert jd.discover_pair("sys-x", "sys-x") == {"joins": [], "crosswalks": []}
    assert wired == []
