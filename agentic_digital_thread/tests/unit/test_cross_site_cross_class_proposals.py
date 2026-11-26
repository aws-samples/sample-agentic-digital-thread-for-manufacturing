# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0051 P8 for learning proposals between systems of ANY class.

Walk finding (2026-10-19): after deploy the dashboard's Recent learning card
offered "Proposed (with evidence): Translate <field> in <plant A>'s MES to
<field> in <telemetry system> at <plant B>", with hundreds of pending
proposals behind it. The cross-system miner filed a value crosswalk between
an execution system serving one plant and a telemetry system serving
another. P8 refused such links only between two systems of the SAME class,
so a plant MES and another plant's historian (or maintenance system) passed.
The onboarding Resolve step already withholds identity rules between
different plants of any class (``resolve_site_gate``); this pins the same
site scope for every learning proposal (crosswalk, value mapping, join path,
identity rule) that ``log_proposed_mapping`` files.

Unchanged and pinned here: an unsited / All Sites system has no plant, so
its links to any plant pass; a steward-confirmed All Sites authority for the
concept lets a cross-plant link through and an unconfirmed one only flags it
(ADR-0052 B4); user source bypasses (ADR-0003 C); missing site metadata, an
absent concept and backend failures fail open (a proposal-quality gate).

Offline: FakeTable, monkeypatched ``known_systems`` catalog and graph seam,
and (for the miner fixture) monkeypatched join-discovery reads. Neutral
fixture names only: no workshop plant, system, table or column.
"""
from __future__ import annotations

import itertools

import pytest

import agents.join_discovery as jd
import agents.self_learning as self_learning

_SYSTEMS = [
    {"id": "exec-north", "name": "North Execution", "system_type": "MES",
     "plant": "North Works"},
    {"id": "exec-south", "name": "South Execution", "system_type": "MES",
     "plant": "South Works"},
    {"id": "telemetry-north", "name": "North Telemetry",
     "system_type": "Historian", "plant": "North Works"},
    {"id": "telemetry-south", "name": "South Telemetry",
     "system_type": "Historian", "plant": "South Works"},
    {"id": "upkeep-south", "name": "South Upkeep", "system_type": "CMMS",
     "plant": "south works"},  # same plant as exec-south, spelled in lower case
    {"id": "ledger", "name": "Ledger", "system_type": "ERP",
     "plant": "All Sites"},
]


class FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def query(self, **kwargs):
        idx = kwargs.get("IndexName")
        vals = kwargs.get("ExpressionAttributeValues", {})
        if idx == "subject-key-index":
            return {"Items": [i for i in self.items
                              if i.get("subject_key") == vals.get(":sk")]}
        if idx == "status-index":
            return {"Items": [i for i in self.items
                              if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self.items
                          if i.get("mapping_id") == vals.get(":mid")]}

    def put_item(self, Item):
        self.items.append(dict(Item))

    def delete_item(self, Key):
        self.items = [i for i in self.items
                      if not (i.get("mapping_id") == Key["mapping_id"]
                              and i.get("discovered_at") == Key["discovered_at"])]

    def pending(self) -> list[dict]:
        return [i for i in self.items if i.get("status") == "pending"]


@pytest.fixture()
def wired(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "known_systems", lambda: list(_SYSTEMS))
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    self_learning._reset_accepted_coverage_cache()
    return fake


def _authority(monkeypatch, sid, concept="cell-ref", **attrs):
    monkeypatch.setattr(
        self_learning, "_neptune_query",
        lambda c, p=None: ([{"id": f"ar-{sid}", "sid": sid, "concept": concept,
                             **attrs}] if "AuthorityRule" in c else []))


def _log(from_system, to_system, *, proposed_type="crosswalk", source="agent",
         concept="cell-ref"):
    values = ("CELL-ALPHA", "ALPHA") if proposed_type in (
        "crosswalk", "value_mapping") else ("", "")
    return self_learning.log_proposed_mapping(
        from_system, "cells.cell_ref", values[0],
        to_system, "units.unit_ref", values[1],
        concept, 0.8, "name similarity",
        proposed_type=proposed_type, source=source,
    )


# --------------------------------------------------------------------------- #
# The gap: different classes at different plants
# --------------------------------------------------------------------------- #
def test_cross_class_cross_plant_crosswalk_dropped(wired):
    # The walk's shape: a plant's execution system to another plant's
    # telemetry system.
    assert _log("exec-north", "telemetry-south") is None
    assert wired.items == []


@pytest.mark.parametrize("ptype", ["value_mapping", "join_path",
                                   "identity_rule", "crosswalk"])
def test_every_guarded_type_is_site_scoped(wired, ptype):
    assert _log("exec-north", "upkeep-south", proposed_type=ptype) is None
    assert wired.pending() == []


def test_reason_names_both_classes_and_plants(wired):
    verdict, reason = self_learning._gate_cross_site_namespace(
        "crosswalk", "exec-north", "telemetry-south", "cell-ref", "agent")
    assert verdict == "drop"
    assert "site-local identifier namespaces collide on format" in reason
    assert "'cell-ref'" in reason
    assert "between one MES system and one Historian system" in reason
    assert "at North Works and South Works" in reason


def test_same_class_reason_unchanged(wired):
    # The pinned same-class wording (test_cross_site_proposal_guard) keeps
    # its "two <class> systems" form.
    verdict, reason = self_learning._gate_cross_site_namespace(
        "value_mapping", "exec-north", "exec-south", "cell-ref", "agent")
    assert verdict == "drop"
    assert "between two MES systems at North Works and South Works" in reason


# --------------------------------------------------------------------------- #
# What still passes
# --------------------------------------------------------------------------- #
def test_same_plant_cross_class_kept(wired):
    assert _log("exec-north", "telemetry-north") is not None
    assert len(wired.pending()) == 1


def test_plant_names_compare_without_case(wired):
    # "South Works" and "south works" are one plant.
    assert _log("exec-south", "upkeep-south") is not None


def test_all_sites_system_links_to_any_plant(wired):
    assert _log("exec-north", "ledger") is not None
    assert _log("telemetry-south", "ledger") is not None


def test_steward_confirmed_all_sites_authority_lets_it_through(monkeypatch, wired):
    _authority(monkeypatch, "ledger", approved_by="steward")
    assert _log("exec-north", "telemetry-south") is not None
    assert not wired.pending()[0].get("review_reason")


def test_unconfirmed_all_sites_authority_flags_not_drops(monkeypatch, wired):
    _authority(monkeypatch, "ledger")
    assert _log("exec-north", "telemetry-south") is not None
    (row,) = wired.pending()
    assert row["review_reason"] == (
        "cross-site link vouched only by unverified authority ar-ledger")


def test_sited_authority_does_not_vouch(monkeypatch, wired):
    _authority(monkeypatch, "exec-south", approved_by="steward")
    assert _log("exec-north", "telemetry-south") is None


def test_user_source_bypasses(wired):
    assert _log("exec-north", "telemetry-south", source="user") is not None


def test_missing_class_on_either_side_still_site_scoped(monkeypatch, wired):
    # The plant is what scopes a namespace; the class only names it.
    systems = [dict(s) for s in _SYSTEMS]
    for s in systems:
        if s["id"] == "telemetry-south":
            s["system_type"] = ""
    monkeypatch.setattr(self_learning, "known_systems", lambda: systems)
    assert _log("exec-north", "telemetry-south") is None


def test_missing_site_metadata_fails_open(monkeypatch, wired):
    systems = [dict(s) for s in _SYSTEMS]
    for s in systems:
        if s["id"] == "telemetry-south":
            s["plant"] = ""
    monkeypatch.setattr(self_learning, "known_systems", lambda: systems)
    assert _log("exec-north", "telemetry-south") is not None


@pytest.mark.parametrize("concept", ["", "unknown"])
def test_absent_concept_fails_open(wired, concept):
    assert self_learning._gate_cross_site_namespace(
        "crosswalk", "exec-north", "telemetry-south", concept, "agent") == ("", "")


def test_unknown_system_fails_open(wired):
    assert _log("exec-north", "not-in-catalog") is not None


# --------------------------------------------------------------------------- #
# Fixture count through the real producer (the cross-system miner)
# --------------------------------------------------------------------------- #
# Every system exposes one identifier field on the shared concept. Each
# execution system's values align by name with two values of EACH telemetry
# system (one at its own plant, one at the other plant) and one value of the
# All Sites ledger, and no from-value aligns with two targets, so every
# pair the miner measures files its own crosswalks. Before the fix the miner
# filed 10 pending crosswalks here, 4 of them across plants; after it, 6,
# none across plants.
_MINER_SYSTEMS = ("exec-north", "exec-south", "telemetry-north",
                  "telemetry-south", "ledger")
_FIELD_VALUES = {
    "exec-north": ["CELL-ALPHA", "CELL-BRAVO", "CELL-CHARLIE", "CELL-DELTA",
                   "CELL-JULIET"],
    "exec-south": ["CELL-ECHO", "CELL-FOXTROT", "CELL-GOLF", "CELL-HOTEL",
                   "CELL-KILO"],
    "telemetry-north": ["ALPHA", "BRAVO", "ECHO", "FOXTROT"],
    "telemetry-south": ["CHARLIE", "DELTA", "GOLF", "HOTEL"],
    "ledger": ["JULIET", "KILO"],
}


@pytest.fixture()
def miner(monkeypatch, wired):
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: [
        {"table": "items", "field": "item_ref", "concept": "cell-ref",
         "role": "identifier", "data_type": "text"}])
    monkeypatch.setattr(jd, "_sample",
                        lambda sid, tbl, fld: list(_FIELD_VALUES[sid]))
    monkeypatch.setattr(jd, "_neptune_query", lambda c, p=None: [])
    monkeypatch.setattr(jd, "_bump_usage", lambda refs: None)
    return wired


def _plant(sid: str) -> str:
    from agents.metric_multisite import site_of
    entry = next(s for s in _SYSTEMS if s["id"] == sid)
    return site_of(entry).casefold()


def test_miner_files_no_cross_plant_crosswalks(miner):
    for a, b in itertools.combinations(_MINER_SYSTEMS, 2):
        jd.mine_value_crosswalks(a, b)
    pending = miner.pending()
    cross_plant = [r for r in pending
                   if _plant(r["from_system"]) and _plant(r["to_system"])
                   and _plant(r["from_system"]) != _plant(r["to_system"])]
    assert cross_plant == []
    # Same plant across classes, and links to the All Sites ledger, remain.
    pairs = sorted(tuple(sorted((r["from_system"], r["to_system"])))
                   for r in pending)
    assert pairs == [("exec-north", "ledger"),
                     ("exec-north", "telemetry-north"),
                     ("exec-north", "telemetry-north"),
                     ("exec-south", "ledger"),
                     ("exec-south", "telemetry-south"),
                     ("exec-south", "telemetry-south")]
