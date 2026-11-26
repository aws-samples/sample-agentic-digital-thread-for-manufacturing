# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the impact analytics route (agents/graph_analytics.py impact()).

Covers the impact-drawer data-quality fixes, all offline (mocked neptune_query,
NO live Neptune — the graph is VPC-private):

  1. DEDUPE — hop1 returns one row per (field, site-table); the drawer must show
     one entry per (field, role), first table wins (no 'I:x, I:x, I:x').
  2. SELF-JOINS — degenerate JoinPaths (same system AND same field) are noise and
     dropped; same-system CROSS-field joins are real intra-system links and kept.
  3. STALE-SYSTEM FILTER — JoinPath rows referencing systems absent from the graph
     or explicitly not onboarded are excluded (and their concepts never reach
     reachable_concepts). Short-name refs ('erp' for System id 'erp-xyz') match.
     Fail OPEN on legacy graphs (no status property) and on System-query failure —
     availability-first for a read-only analytics view.
  4. SHAPE — the /api/thread/analytics/impact/{id} response shape is unchanged.

All fixtures are synthetic (generic sys_a/sys_b/erp-style ids, no dataset content).
"""
from unittest.mock import patch

from agents import graph_analytics

_CONCEPT = "concept-x"

# Distinguishing cypher substrings for each neptune_query call in impact().
_K_HOP1 = "f.name AS field"
_K_SYSTEMS = "s.status AS status"
_K_HOP2 = "JoinPath"
_K_HOP3 = "$connected_system_ids"
_K_CORR = "Correlation"
_K_BOUND = "DecisionBoundary"
_K_NAME = "c.name AS name"


def _dispatch(responses):
    """Build a neptune_query side_effect that dispatches on cypher substrings.
    A value that is an Exception is raised; missing keys return []."""
    keys = (_K_SYSTEMS, _K_HOP2, _K_HOP3, _K_CORR, _K_BOUND, _K_NAME, _K_HOP1)

    def q(cypher, params=None):
        for key in keys:
            if key in cypher:
                val = responses.get(key, [])
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unmatched cypher in test dispatch: {cypher}")

    return q


def _hop1_row(system_id="sys_a", field="asset_id", role="identifier", table="t1"):
    return {"system_id": system_id, "system_name": system_id.upper(),
            "field": field, "role": role, "table_name": table}


def _jp(from_sys, to_sys, from_field, to_field):
    return {"from_sys": from_sys, "to_sys": to_sys,
            "from_field": from_field, "to_field": to_field, "description": ""}


def _impact(responses):
    with patch.object(graph_analytics, "neptune_query", side_effect=_dispatch(responses)):
        return graph_analytics.impact(_CONCEPT)


# ── 1. Lives-in field dedupe ─────────────────────────────────────────────────
def test_direct_system_fields_deduped_across_site_tables():
    """Three per-site rows of the SAME (field, role) collapse to ONE entry
    (first table wins) — no more 'I:asset_id, I:asset_id, I:asset_id'."""
    out = _impact({
        _K_HOP1: [_hop1_row(table="t_site1"), _hop1_row(table="t_site2"), _hop1_row(table="t_site3")],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"}],
    })
    fields = out["direct_systems"][0]["fields"]
    assert fields == [{"name": "asset_id", "table": "t_site1", "role": "identifier"}]


def test_direct_system_keeps_distinct_roles_and_fields():
    """Dedupe is on (name, role): the same field in a different ROLE, and a
    different field, both stay."""
    out = _impact({
        _K_HOP1: [_hop1_row(role="identifier"), _hop1_row(role="attribute", table="t2"),
                  _hop1_row(field="serial_no", table="t3")],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"}],
    })
    fields = out["direct_systems"][0]["fields"]
    assert len(fields) == 3
    assert {(f["name"], f["role"]) for f in fields} == {
        ("asset_id", "identifier"), ("asset_id", "attribute"), ("serial_no", "identifier")}


# ── 2. Degenerate self-joins ─────────────────────────────────────────────────
def test_degenerate_self_join_dropped_cross_field_join_kept():
    """sys_a→sys_a asset_id→asset_id is noise and dropped; sys_a→sys_a
    wo_number→work_order_id is a real intra-system link and kept."""
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"}],
        _K_HOP2: [_jp("sys_a", "sys_a", "asset_id", "asset_id"),
                  _jp("sys_a", "sys_a", "wo_number", "work_order_id")],
    })
    assert out["join_paths"] == [_jp("sys_a", "sys_a", "wo_number", "work_order_id")]


# ── 3. Stale/offboarded system filter ────────────────────────────────────────
def test_stale_system_ref_filtered_from_joins_and_reachable():
    """A JoinPath to a system with NO System node is excluded — and because
    connected_system_ids inherits the filter, its concepts never reach
    reachable_concepts (hop3 sees no connected systems)."""
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"}],
        _K_HOP2: [_jp("sys_a", "sys_zz", "asset_id", "equip_id")],
        _K_HOP3: [{"concept": "leaked", "concept_name": "Leaked",
                   "system_id": "sys_zz", "role": "identifier"}],
    })
    assert out["join_paths"] == []
    assert out["reachable_concepts"] == []
    assert out["hop_summary"]["hop_2_systems"] == 0


def test_short_name_join_ref_matches_onboarded_system_id():
    """JoinPath refs may use SHORT names: 'erp' must match onboarded System id
    'erp-xyz' — legitimate cross-system unlocks are NOT hidden."""
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"},
                     {"id": "erp-xyz", "status": "onboarded"}],
        _K_HOP2: [_jp("erp", "sys_a", "equipment_id", "asset_id")],
        _K_HOP3: [{"concept": "concept-y", "concept_name": "Y",
                   "system_id": "erp", "role": "identifier"}],
    })
    assert out["join_paths"] == [_jp("erp", "sys_a", "equipment_id", "asset_id")]
    assert out["reachable_concepts"] == [
        {"concept": "concept-y", "concept_name": "Y", "via_system": "erp", "role": "identifier"}]
    assert out["hop_summary"]["hop_2_systems"] == 1


def test_not_onboarded_system_filtered_when_statuses_present():
    """When System nodes carry statuses, refs to a non-'onboarded' system are
    excluded."""
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"},
                     {"id": "sys_b", "status": "registered"}],
        _K_HOP2: [_jp("sys_a", "sys_b", "asset_id", "equip_id")],
    })
    assert out["join_paths"] == []


def test_fail_open_when_no_status_properties():
    """Legacy graphs: NO System node carries a status → no status restriction
    (existing systems all allowed)."""
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: [{"id": "sys_a", "status": None}, {"id": "sys_b", "status": None}],
        _K_HOP2: [_jp("sys_a", "sys_b", "asset_id", "equip_id")],
    })
    assert out["join_paths"] == [_jp("sys_a", "sys_b", "asset_id", "equip_id")]


def test_fail_open_when_system_query_raises():
    """A failing System fetch disables the filter entirely (availability-first
    for a read-only analytics view) — joins still flow."""
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: RuntimeError("neptune unavailable"),
        _K_HOP2: [_jp("sys_a", "sys_zz", "asset_id", "equip_id")],
    })
    assert out["join_paths"] == [_jp("sys_a", "sys_zz", "asset_id", "equip_id")]


# ── 4. Response shape (frontend contract) ────────────────────────────────────
def test_response_shape_unchanged():
    out = _impact({
        _K_HOP1: [_hop1_row()],
        _K_SYSTEMS: [{"id": "sys_a", "status": "onboarded"}],
        _K_NAME: [{"name": "Concept X"}],
    })
    assert set(out.keys()) == {"concept", "direct_systems", "join_paths",
                               "reachable_concepts", "correlations", "boundaries",
                               "hop_summary"}
    assert out["concept"] == {"id": _CONCEPT, "name": "Concept X"}
    ds = out["direct_systems"][0]
    assert set(ds.keys()) == {"system_id", "system_name", "fields"}
    assert set(ds["fields"][0].keys()) == {"name", "table", "role"}
    assert set(out["hop_summary"].keys()) == {"hop_1_systems", "hop_2_systems", "hop_3_concepts"}


def test_hop1_failure_returns_empty_payload():
    out = _impact({_K_HOP1: RuntimeError("neptune down")})
    assert out["direct_systems"] == [] and out["join_paths"] == []
    assert out["hop_summary"] == {"hop_1_systems": 0, "hop_2_systems": 0, "hop_3_concepts": 0}
