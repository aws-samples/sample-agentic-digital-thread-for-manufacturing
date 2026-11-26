# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ValueMapping graph hygiene — the pure planner and the applier.

Accounts onboarded before 2026-09-28 carry two classes of bad ValueMapping row:

(a) a delivered guidance note (ADR-0063 — never a graph rule) replayed as a
    ValueMapping by the old provision §i loop: id ``guidance-subj-*`` (the
    item's ``neptune_node_id``) or ``vm-learned-*`` (the fallback), empty
    concept, one empty side, the note sentence on the other;
(b) generated ``vm-equip-*`` rows sharing the equipment identity with another
    ``vm-equip-*`` row (older index-/field-keyed ids).

Offline: no graph, no network — the applier takes fake read/write seams.
"""
from __future__ import annotations

import pytest

from agents import graph_hygiene as gh
from agents.onboard import _equipment_vm_id

_NOTE = "Always prefer the maintenance order number when the asset tag is ambiguous."


def _equip(vid: str | None = None, *, value: str = "PUMP-101", to_value: str = "Pump 101",
           **extra) -> dict:
    row = {"from_system": "reg", "from_value": value, "to_system": "hist",
           "to_value": to_value, "concept": "equipment", **extra}
    row["id"] = vid if vid is not None else _equipment_vm_id(row)
    return row


def _learned(vid: str, **fields) -> dict:
    base = {"id": vid, "from_system": "reg", "from_value": "", "to_system": "",
            "to_value": _NOTE, "concept": ""}
    return {**base, **fields}


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #

def test_malformed_learned_rows_are_planned_for_retirement():
    rows = [
        _learned("guidance-subj-0123456789abcdef"),
        _learned("vm-learned-deadbeef", from_value=_NOTE, to_value=""),
    ]
    plan = gh.plan_value_mapping_hygiene(rows)
    assert plan["malformed_learned"] == ["guidance-subj-0123456789abcdef",
                                         "vm-learned-deadbeef"]
    assert plan["duplicate_generated"] == [] and plan["kept"] == []


def test_valid_learned_row_is_untouched():
    valid = _learned("vm-learned-cafebabe", from_value="A-1", to_system="hist",
                     to_value="Asset 1", concept="equipment")
    plan = gh.plan_value_mapping_hygiene([valid])
    assert plan == {"malformed_learned": [], "duplicate_generated": [], "kept": []}


def test_learned_row_with_concept_is_untouched_even_with_an_empty_side():
    """Only the replayed-note SHAPE is malformed: blank concept AND a blank side."""
    row = _learned("vm-learned-00000001", concept="equipment")
    assert gh.plan_value_mapping_hygiene([row])["malformed_learned"] == []


def test_equipment_duplicates_keep_the_current_key_row():
    current = _equip()
    legacy_idx = _equip("vm-equip-PUMP-101-3")
    legacy_field = _equip("vm-equip-0000aaaa1111bbbb", updated_at="2099-01-01T00:00:00Z")
    plan = gh.plan_value_mapping_hygiene([legacy_idx, current, legacy_field])
    assert plan["kept"] == [current["id"]]
    assert plan["duplicate_generated"] == sorted([legacy_idx["id"], legacy_field["id"]])
    assert plan["malformed_learned"] == []


def test_equipment_duplicates_without_current_key_keep_the_newest():
    old = _equip("vm-equip-a", created_at="2026-01-01T00:00:00Z")
    new = _equip("vm-equip-b", updated_at="2026-06-01T00:00:00Z")
    untimed = _equip("vm-equip-c")
    plan = gh.plan_value_mapping_hygiene([old, new, untimed])
    assert plan["kept"] == ["vm-equip-b"]
    assert plan["duplicate_generated"] == ["vm-equip-a", "vm-equip-c"]


def test_equipment_duplicates_without_timestamps_keep_lexically_first():
    plan = gh.plan_value_mapping_hygiene([_equip("vm-equip-z"), _equip("vm-equip-m")])
    assert plan["kept"] == ["vm-equip-m"]
    assert plan["duplicate_generated"] == ["vm-equip-z"]


def test_generated_single_rows_are_untouched():
    rows = [_equip(), _equip("vm-equip-legacy-0", value="VALVE-7", to_value="Valve 7")]
    assert gh.plan_value_mapping_hygiene(rows) == {
        "malformed_learned": [], "duplicate_generated": [], "kept": []}


def test_steward_and_unclassified_rows_are_never_touched():
    """``subj-*`` steward rows survive even in the malformed learned shape and
    even when they share an equipment identity; other prefixes are not ours."""
    current = _equip()
    steward_same_identity = {**current, "id": "subj-0123456789abcdef"}
    steward_malformed = _learned("subj-fedcba9876543210")
    cross = {**_learned("vm-xsys-a-b-0"), "concept": ""}
    plan = gh.plan_value_mapping_hygiene(
        [current, steward_same_identity, steward_malformed, cross])
    assert plan == {"malformed_learned": [], "duplicate_generated": [], "kept": []}


def test_rows_without_id_are_ignored():
    assert gh.plan_value_mapping_hygiene([{"id": ""}, {}, None]) == {
        "malformed_learned": [], "duplicate_generated": [], "kept": []}


# --------------------------------------------------------------------------- #
# Applier
# --------------------------------------------------------------------------- #

class _FakeGraph:
    """Records every write (Cypher + params); the readback answers from the
    ids still 'present' after the deletes it saw."""

    def __init__(self, present: set[str], fail: set[str] = frozenset()):
        self.present = set(present)
        self.fail = set(fail)
        self.writes: list[tuple[str, dict]] = []
        self.reads: list[tuple[str, dict]] = []

    def write(self, cypher, params=None):
        self.writes.append((cypher, dict(params or {})))
        if params["vmid"] in self.fail:
            return False
        self.present.discard(params["vmid"])
        return True

    def read(self, cypher, params=None):
        """The readback: ONE row per requested id (``found`` None when gone)."""
        self.reads.append((cypher, dict(params or {})))
        return [{"id": i, "found": i if i in self.present else None} for i in params["ids"]]


_NOT_TRUNCATED = {"malformed_learned_ids_truncated": False,
                  "duplicate_generated_ids_truncated": False, "kept_ids_truncated": False}
_NOT_TRUNCATED_READBACK = {"still_present_truncated": False, "kept_missing_truncated": False}

_PLAN = {"malformed_learned": ["guidance-subj-1"],
         "duplicate_generated": ["vm-equip-old"], "kept": ["vm-equip-new"]}


def test_applier_deletes_each_id_parameterised_and_reads_back():
    g = _FakeGraph({"guidance-subj-1", "vm-equip-old", "vm-equip-new"})
    report = gh.apply_value_mapping_hygiene(_PLAN, write=g.write, read=g.read)
    assert g.writes == [
        (gh.RETIRE_VALUE_MAPPING_CYPHER, {"vmid": "guidance-subj-1"}),
        (gh.RETIRE_VALUE_MAPPING_CYPHER, {"vmid": "vm-equip-old"}),
    ]
    assert gh.RETIRE_VALUE_MAPPING_CYPHER == (
        "MATCH (vm:ValueMapping {id: $vmid}) DETACH DELETE vm")
    assert g.reads[0][1] == {"ids": ["guidance-subj-1", "vm-equip-old", "vm-equip-new"]}
    assert report == {
        "dry_run": False,
        "malformed_learned_retired": 1, "duplicate_generated_retired": 1, "kept": 1,
        "malformed_learned_ids": ["guidance-subj-1"],
        "duplicate_generated_ids": ["vm-equip-old"], "kept_ids": ["vm-equip-new"],
        "readback": {"verified": True, "still_present": [], "kept_missing": [],
                     "readback_failed": False,
                     **_NOT_TRUNCATED_READBACK},
        "scan_failed": False, "verified": True, **_NOT_TRUNCATED,
    }


def test_applier_counts_only_readback_confirmed_retirements():
    g = _FakeGraph({"guidance-subj-1", "vm-equip-old", "vm-equip-new"},
                   fail={"vm-equip-old"})
    report = gh.apply_value_mapping_hygiene(_PLAN, write=g.write, read=g.read)
    assert report["duplicate_generated_retired"] == 0
    assert report["malformed_learned_retired"] == 1
    assert report["readback"] == {"verified": False, "still_present": ["vm-equip-old"],
                                  "kept_missing": [], "readback_failed": False,
                     **_NOT_TRUNCATED_READBACK}
    assert report["verified"] is False


def test_applier_flags_a_missing_kept_row():
    g = _FakeGraph({"guidance-subj-1", "vm-equip-old"})
    report = gh.apply_value_mapping_hygiene(_PLAN, write=g.write, read=g.read)
    assert report["readback"]["kept_missing"] == ["vm-equip-new"]
    assert report["readback"]["verified"] is False


def test_applier_refuses_a_steward_id_even_if_planned():
    g = _FakeGraph({"subj-0123456789abcdef"})
    plan = {"malformed_learned": ["subj-0123456789abcdef"],
            "duplicate_generated": [], "kept": []}
    with pytest.raises(ValueError, match="steward"):
        gh.apply_value_mapping_hygiene(plan, write=g.write, read=g.read)
    assert g.writes == []


def test_empty_plan_writes_and_reads_nothing():
    g = _FakeGraph(set())
    report = gh.apply_value_mapping_hygiene(
        {"malformed_learned": [], "duplicate_generated": [], "kept": []},
        write=g.write, read=g.read)
    assert g.writes == [] and g.reads == []
    assert report["readback"] == {"verified": True, "still_present": [], "kept_missing": [],
                                  "readback_failed": False,
                     **_NOT_TRUNCATED_READBACK}
    assert report["verified"] is True


# --------------------------------------------------------------------------- #
# run_value_mapping_hygiene: scan → plan → (apply | dry run)
# --------------------------------------------------------------------------- #

def _scan_reader(rows, count=None):
    """A read seam answering the positive-control count and the scan."""
    def read(cypher, params=None):
        assert params is None or params == {}
        if cypher == gh.COUNT_VALUE_MAPPINGS_CYPHER:
            return [{"n": len(rows) if count is None else count}]
        assert cypher == gh.SCAN_VALUE_MAPPINGS_CYPHER
        return rows
    return read

def test_run_dry_run_plans_and_never_writes():
    current, legacy = _equip(), _equip("vm-equip-PUMP-101-0")
    rows = [current, legacy, _learned("guidance-subj-1")]
    writes: list = []
    report = gh.run_value_mapping_hygiene(_scan_reader(rows),
                                          lambda c, p=None: writes.append(c), dry_run=True)
    assert writes == []
    assert report == {
        "dry_run": True,
        "malformed_learned_retired": 1, "duplicate_generated_retired": 1, "kept": 1,
        "malformed_learned_ids": ["guidance-subj-1"],
        "duplicate_generated_ids": ["vm-equip-PUMP-101-0"], "kept_ids": [current["id"]],
        "scan_failed": False, "verified": True, **_NOT_TRUNCATED,
    }


def test_scan_query_is_label_scoped_and_projects_identity_and_timestamps():
    q = gh.SCAN_VALUE_MAPPINGS_CYPHER
    assert q.startswith("MATCH (vm:ValueMapping) RETURN ")
    for f in ("id", "from_system", "from_value", "to_system", "to_value", "concept",
              "created_at", "updated_at"):
        assert f"vm.{f} AS {f}" in q


# --------------------------------------------------------------------------- #
# A failed read is not an empty result (positive controls)
# --------------------------------------------------------------------------- #

def test_count_query_always_returns_one_row():
    assert gh.COUNT_VALUE_MAPPINGS_CYPHER == "MATCH (vm:ValueMapping) RETURN count(vm) AS n"


def test_empty_graph_is_a_verified_empty_scan():
    report = gh.run_value_mapping_hygiene(_scan_reader([]), _no_write, dry_run=False)
    assert report["scan_failed"] is False and report["verified"] is True
    assert report["malformed_learned_retired"] == 0


def _no_write(*_a, **_k):
    raise AssertionError("no write after a failed scan")


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("control", [[], None, [{"n": "x"}], [{"n": True}], [{}], [{"n": 1}, {"n": 1}]])
def test_failed_count_control_marks_the_scan_failed_and_writes_nothing(dry_run, control):
    """``neptune_query`` degrades to ``[]`` on failure: without the control a
    failed scan would read as a clean graph."""
    def read(cypher, params=None):
        return control if cypher == gh.COUNT_VALUE_MAPPINGS_CYPHER else []
    report = gh.run_value_mapping_hygiene(read, _no_write, dry_run=dry_run)
    assert report["scan_failed"] is True and report["verified"] is False
    assert report["malformed_learned_ids"] == [] and report["kept"] == 0


@pytest.mark.parametrize("dry_run", [True, False])
def test_scan_short_of_the_count_is_a_failed_scan(dry_run):
    rows = [_learned("guidance-subj-1")]
    report = gh.run_value_mapping_hygiene(_scan_reader([], count=len(rows)), _no_write,
                                          dry_run=dry_run)
    assert report["scan_failed"] is True and report["verified"] is False


def test_failed_readback_is_unverified_and_counts_stay_as_attempted():
    """A readback that returns fewer rows than ids asked for FAILED (the
    UNWIND query answers one row per id): verified false, retirements
    counted as attempted writes, no invented still-present ids."""
    g = _FakeGraph({"guidance-subj-1", "vm-equip-old", "vm-equip-new"})
    report = gh.apply_value_mapping_hygiene(_PLAN, write=g.write, read=lambda *a, **k: [])
    assert report["readback"] == {"verified": False, "still_present": [], "kept_missing": [],
                                  "readback_failed": True,
                                  **_NOT_TRUNCATED_READBACK}
    assert report["verified"] is False
    assert report["malformed_learned_retired"] == 1 and report["duplicate_generated_retired"] == 1


def test_readback_query_answers_one_row_per_id():
    q = gh.READBACK_VALUE_MAPPINGS_CYPHER
    assert q.startswith("UNWIND $ids AS i OPTIONAL MATCH (vm:ValueMapping {id: i})")
    assert "RETURN i AS id, vm.id AS found" in q


def test_readback_with_a_foreign_id_row_fails():
    g = _FakeGraph({"vm-equip-new"})

    def read(cypher, params=None):
        return [{"id": "other", "found": None}] * len(params["ids"])
    report = gh.apply_value_mapping_hygiene(_PLAN, write=g.write, read=read)
    assert report["readback"]["readback_failed"] is True and report["verified"] is False


def test_id_lists_are_capped_with_truncation_flags():
    many = [_learned(f"guidance-subj-{i:04d}") for i in range(gh.REPORT_ID_CAP + 5)]
    report = gh.run_value_mapping_hygiene(_scan_reader(many), _no_write, dry_run=True)
    assert gh.REPORT_ID_CAP == 200
    assert report["malformed_learned_retired"] == gh.REPORT_ID_CAP + 5
    assert len(report["malformed_learned_ids"]) == gh.REPORT_ID_CAP
    assert report["malformed_learned_ids_truncated"] is True
    assert report["duplicate_generated_ids_truncated"] is False
    assert report["kept_ids_truncated"] is False


def test_small_reports_carry_false_truncation_flags_only_when_capped():
    """Under the cap the flags are present and False (a stable shape)."""
    report = gh.run_value_mapping_hygiene(_scan_reader([_learned("guidance-subj-1")]),
                                          _no_write, dry_run=True)
    assert report["malformed_learned_ids_truncated"] is False


def test_readback_lists_are_capped():
    ids = [f"guidance-subj-{i:04d}" for i in range(gh.REPORT_ID_CAP + 3)]
    plan = {"malformed_learned": ids, "duplicate_generated": [], "kept": []}
    g = _FakeGraph(set(ids), fail=set(ids))
    report = gh.apply_value_mapping_hygiene(plan, write=g.write, read=g.read)
    assert len(report["readback"]["still_present"]) == gh.REPORT_ID_CAP
    assert report["readback"]["still_present_truncated"] is True
