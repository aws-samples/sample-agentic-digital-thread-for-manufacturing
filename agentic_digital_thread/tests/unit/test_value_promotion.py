# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the per-value → rule promotion sweep (ADR-0043, T1).

No AWS, no Neptune, no network. Covers:

  * the PURE grain helpers — ``group_value_mappings`` (field-pair grain,
    ``table.column`` shape gate, ADR-0041 same-row exclusion) and
    ``independent_confirmations`` (distinct normalized ``from_value``s — the
    threshold input, NOT confidence);
  * the sweep — 40 rows on one field pair with a ladder-clearing transform →
    exactly ONE ``join_path`` proposal (transform carried) and all 40 rows
    ``superseded`` with ``subsumed_by``; 2 distinct values (below threshold) →
    nothing emitted, rows untouched; disjoint vocabularies → ONE ``crosswalk``;
  * NEVER-AUTO-ACCEPTED (ADR-0043 D5) — end-to-end through the REAL
    ``log_proposed_mapping`` with ``SELF_LEARNING_AUTO_ACCEPT=1``: the promoted
    proposal enters ``pending`` and ``accept_mapping`` is never called (the
    sweep contains no accept; ``join_path`` is outside ``_AUTO_ACCEPT_TYPES``
    and ``crosswalk`` is in the deny-tuple);
  * the ``POST /api/proposed-mappings/promote-values`` route via ``TestClient``
    (mirrors ``test_proposed_mappings_bulk_routes.py``);
  * import purity — the module imports with boto3/pg8000 blocked.

Seams: ``self_learning``'s module globals (``get_pending_mappings``,
``log_proposed_mapping``, ``_get_table``, ``_update_status``) are monkeypatched
for the sweep-shape tests; the end-to-end tests use an in-memory ``FakeTable``
(the ``test_self_learning_lifecycle.py`` double) behind the REAL
``log_proposed_mapping``.
"""
from __future__ import annotations

import os
import pathlib
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.self_learning as self_learning
import agents.value_promotion as value_promotion
from agents.value_promotion import (
    group_value_mappings, independent_confirmations, sweep_value_promotion,
)


def _vm_row(i: int, from_value: str, to_value: str, *,
            from_system: str = "cmms", from_field: str = "assets.serial_number",
            to_system: str = "uns", to_field: str = "tags.asset_tag",
            **extra) -> dict:
    row = {
        "mapping_id": f"vm-{i:03d}",
        "discovered_at": f"2026-07-01T00:{i:02d}:00+00:00",
        "proposed_type": "value_mapping",
        "status": "pending",
        "from_system": from_system, "from_field": from_field,
        "from_value": from_value,
        "to_system": to_system, "to_field": to_field, "to_value": to_value,
        "concept": "equipment-id", "confidence": "0.8",
        "subject_key": f"vm|{from_system}|{from_field}|{from_value}",
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# PURE helpers — grouping grain + threshold input
# --------------------------------------------------------------------------- #
def test_groups_by_field_pair_grain():
    rows = [
        _vm_row(1, "SN-1", "TAG-1"),
        _vm_row(2, "SN-2", "TAG-2"),
        _vm_row(3, "LINE1", "DET-L1", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
    ]
    groups = group_value_mappings(rows)
    assert set(groups) == {
        ("cmms", "assets.serial_number", "uns", "tags.asset_tag"),
        ("erp", "work_orders.line_id", "mes-det", "production_lines.line_id"),
    }
    assert len(groups[("cmms", "assets.serial_number", "uns", "tags.asset_tag")]) == 2


def test_grouping_keeps_only_value_mappings_and_shaped_fields():
    rows = [
        _vm_row(1, "SN-1", "TAG-1", proposed_type="join_path"),   # wrong type
        _vm_row(2, "SN-2", "TAG-2", from_field="serial number"),  # narrative (whitespace)
        _vm_row(3, "SN-3", "TAG-3", to_field="tags"),             # bare token — ELIGIBLE
        _vm_row(4, "SN-4", "TAG-4"),                              # dotted — eligible
    ]
    groups = group_value_mappings(rows)
    kept = {r["mapping_id"] for g in groups.values() for r in g}
    assert kept == {"vm-003", "vm-004"}


def test_bare_token_fields_group_at_field_pair_grain():
    """Non-SQL sources register BARE property names (no table qualifier). The
    live regression this pins: 19 historian ``asset_name`` → MES
    ``machine_identifier`` per-value rows were invisible to the sweep because
    neither side carries a dot — the exact rows promotion exists to collapse.
    Same-system same-FIELD bare pairs stay excluded (a field mapped to itself
    is never a promotable pair)."""
    rows = [
        _vm_row(1, "Granulator_1", "MOD_X_GRANULATOR_01",
                from_system="historian", from_field="asset_name",
                to_system="mes", to_field="machine_identifier"),
        _vm_row(2, "Granulator_2", "MOD_X_GRANULATOR_02",
                from_system="historian", from_field="asset_name",
                to_system="mes", to_field="machine_identifier"),
        _vm_row(3, "Granulator_3", "MOD_X_GRANULATOR_03",
                from_system="historian", from_field="asset_name",
                to_system="mes", to_field="machine_identifier"),
        # same-system, same bare field on both sides → never promotable
        _vm_row(4, "A", "A", from_system="historian", from_field="asset_name",
                to_system="historian", to_field="asset_name"),
    ]
    groups = group_value_mappings(rows)
    assert set(groups) == {("historian", "asset_name", "mes", "machine_identifier")}
    assert len(next(iter(groups.values()))) == 3


def test_grouping_excludes_same_system_same_table_pairs():
    """ADR-0041 turf: same-system same-table pairs are same-row sets captured
    at provision — never promoted from the queue. Same-system DIFFERENT-table
    pairs stay eligible."""
    same_table = _vm_row(1, "A", "B", from_system="cmms",
                         from_field="assets.serial_number",
                         to_system="cmms", to_field="assets.asset_tag")
    diff_table = _vm_row(2, "A", "B", from_system="mes-det",
                         from_field="work_orders.line_id",
                         to_system="mes-det", to_field="production_lines.line_id")
    groups = group_value_mappings([same_table, diff_table])
    assert set(groups) == {
        ("mes-det", "work_orders.line_id", "mes-det", "production_lines.line_id"),
    }


def test_independent_confirmations_counts_distinct_normalized_from_values():
    rows = [
        _vm_row(1, "LINE1", "DET-L1"),
        _vm_row(2, " line1 ", "DET-L1"),   # same after strip+casefold
        _vm_row(3, "LINE2", "DET-L2"),
        _vm_row(4, "LINE3", "DET-L3"),
        _vm_row(5, "", "DET-L4"),          # empty never counts
    ]
    assert independent_confirmations(rows) == 3
    assert independent_confirmations([]) == 0


# --------------------------------------------------------------------------- #
# Sweep — seam-level (log_proposed_mapping / _update_status spies)
# --------------------------------------------------------------------------- #
def _counts(**over) -> dict:
    """The sweep's full result dict (all keys zero) with overrides."""
    base = {"promoted": 0, "subsumed": 0, "skipped_below_threshold": 0,
            "already_promoted": 0, "gated": 0}
    base.update(over)
    return base


class _Spies:
    def __init__(self, monkeypatch, pending, *, emit_id="rule-001"):
        self.log_calls: list[dict] = []
        self.status_calls: list[tuple] = []
        self.emit_id = emit_id

        def _log(**kwargs):
            self.log_calls.append(kwargs)
            return self.emit_id

        def _update(table, mapping_id, discovered_at, status, extra=None,
                    expect_status=None):
            self.status_calls.append(
                (mapping_id, status, dict(extra or {}), expect_status))

        monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
        monkeypatch.setattr(self_learning, "log_proposed_mapping", _log)
        monkeypatch.setattr(self_learning, "_get_table", lambda: object())
        monkeypatch.setattr(self_learning, "_update_status", _update)


def test_forty_rows_collapse_to_one_join_path(monkeypatch):
    """The live acceptance shape: 40 per-value rows on ONE field pair with a
    ladder-clearing transform (casefold) → exactly ONE join_path proposal
    (transform carried) and all 40 rows superseded with subsumed_by."""
    rows = [_vm_row(i, f"SN-A{i}", f"sn-a{i}") for i in range(1, 41)]
    spies = _Spies(monkeypatch, rows)

    result = sweep_value_promotion()

    assert result == _counts(promoted=1, subsumed=40)
    assert len(spies.log_calls) == 1
    call = spies.log_calls[0]
    assert call["proposed_type"] == "join_path"
    assert call["transform"] == "casefold"
    assert call["source"] == "agent"
    assert call["from_system"] == "cmms"
    assert call["from_field"] == "assets.serial_number"
    assert call["to_system"] == "uns"
    assert call["to_field"] == "tags.asset_tag"
    # query_context names the N confirmations (the threshold evidence).
    assert "40" in call["query_context"]
    assert "casefold" in call["query_context"]
    # All 40 rows superseded with subsumed_by=<new id> AND a superseded_at
    # stamp (subsumed rows are superseded rows — same timeline attribute).
    assert len(spies.status_calls) == 40
    assert {c[0] for c in spies.status_calls} == {r["mapping_id"] for r in rows}
    assert all(c[1] == "superseded" for c in spies.status_calls)
    assert all(c[2]["subsumed_by"] == "rule-001" for c in spies.status_calls)
    assert all(c[2]["superseded_at"] for c in spies.status_calls)
    # Every subsume is CONDITIONAL on the row still being pending (S-B: an
    # accept/reject between snapshot and subsume wins).
    assert all(c[3] == "pending" for c in spies.status_calls)


def test_below_threshold_nothing_emitted_rows_untouched(monkeypatch):
    """2 distinct from_values < the default threshold (3) → no emit, no
    supersede — the per-value rows simply wait (ADR-0043: no data lost)."""
    rows = [
        _vm_row(1, "SN-1", "TAG-1"),
        _vm_row(2, "SN-2", "TAG-2"),
        _vm_row(3, "sn-1", "TAG-1"),  # duplicate of SN-1 after normalization
    ]
    spies = _Spies(monkeypatch, rows)

    result = sweep_value_promotion()

    assert result == _counts(skipped_below_threshold=1)
    assert spies.log_calls == []
    assert spies.status_calls == []


def test_disjoint_vocabularies_emit_one_crosswalk(monkeypatch):
    """No transform clears the ladder (disjoint vocabularies — the line_id
    shape LINE1↔DET-L1) → ONE crosswalk proposal with the FIRST aligned pair
    as the exemplar; rows subsumed."""
    rows = [
        _vm_row(1, "LINE1", "DET-L1", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
        _vm_row(2, "LINE2", "DET-L2", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
        _vm_row(3, "LINE3", "DET-L3", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
    ]
    spies = _Spies(monkeypatch, rows, emit_id="rule-xw-1")

    result = sweep_value_promotion()

    assert result == _counts(promoted=1, subsumed=3)
    assert len(spies.log_calls) == 1
    call = spies.log_calls[0]
    assert call["proposed_type"] == "crosswalk"
    assert "transform" not in call  # crosswalk carries NO transform
    # Exemplar = first aligned pair in deterministic (oldest-first) order.
    assert call["from_value"] == "LINE1"
    assert call["to_value"] == "DET-L1"
    assert "3" in call["query_context"]
    assert all(c[2]["subsumed_by"] == "rule-xw-1" for c in spies.status_calls)
    assert all(c[2]["superseded_at"] for c in spies.status_calls)


def test_quarantined_rows_are_skipped(monkeypatch):
    """ADR-0042 D2: quarantined rows are frozen until a HUMAN accept/reject —
    the promotion sweep never counts or subsumes them (mirrors
    sweep_auto_accept's quarantine skip)."""
    rows = [_vm_row(i, f"SN-{i}", f"sn-{i}", quarantined="true")
            for i in range(1, 7)]
    spies = _Spies(monkeypatch, rows)

    result = sweep_value_promotion()

    assert result == _counts()
    assert spies.log_calls == []
    assert spies.status_calls == []


def test_emit_returning_none_leaves_rows_pending(monkeypatch):
    """A dropped/held emit (ingestion gate or ADR-0042 churn control →
    log_proposed_mapping returns None) with NO existing equivalent rule never
    subsumes — promotion is evidence, not privilege. The group is now COUNTED
    under ``gated`` so the operator sees the stuck group instead of silence."""
    rows = [_vm_row(i, f"SN-B{i}", f"sn-b{i}") for i in range(1, 7)]
    spies = _Spies(monkeypatch, rows, emit_id=None)
    monkeypatch.setattr(self_learning, "find_existing_equivalent_rule",
                        lambda *a, **k: None)

    result = sweep_value_promotion()

    assert result == _counts(gated=1)
    assert len(spies.log_calls) == 1  # the emit was attempted…
    assert spies.status_calls == []   # …but nothing was superseded


def test_emit_dedup_against_existing_rule_subsumes_as_already_promoted(monkeypatch):
    """The live-observed defect shape: the emit returns None because an
    EQUIVALENT rule already exists (lookup-before-write dedup / rule
    absorption). The group is ALREADY SATISFIED — its per-value rows are
    superseded with ``subsumed_by=<the EXISTING rule id>`` (the same
    conditional subsume the normal path uses) and counted under the additive
    ``already_promoted`` key, never under ``promoted``."""
    rows = [_vm_row(i, f"SN-G{i}", f"sn-g{i}") for i in range(1, 7)]
    spies = _Spies(monkeypatch, rows, emit_id=None)
    monkeypatch.setattr(self_learning, "find_existing_equivalent_rule",
                        lambda *a, **k: "rule-existing")

    result = sweep_value_promotion()

    assert result == _counts(already_promoted=1, subsumed=6)
    assert len(spies.log_calls) == 1              # the emit was attempted
    assert len(spies.status_calls) == 6           # …and the group subsumed
    assert all(c[1] == "superseded" for c in spies.status_calls)
    # Referencing the EXISTING rule row id, conditional on still-pending.
    assert all(c[2]["subsumed_by"] == "rule-existing" for c in spies.status_calls)
    assert all(c[2]["superseded_at"] for c in spies.status_calls)
    assert all(c[3] == "pending" for c in spies.status_calls)


def test_one_bad_group_never_aborts_the_sweep(monkeypatch):
    """Per-group best-effort (the sweep_auto_accept contract): a raising emit
    on one group leaves the other group promoted."""
    bad = [_vm_row(i, f"SN-C{i}", f"sn-c{i}") for i in range(1, 7)]
    good = [_vm_row(i + 10, f"LN{i}", f"ln{i}", from_system="erp",
                    from_field="work_orders.line_id",
                    to_system="mes-mil", to_field="lines.line_id")
            for i in range(1, 7)]
    calls: list[dict] = []

    def _log(**kwargs):
        calls.append(kwargs)
        if kwargs["from_system"] == "cmms":
            raise RuntimeError("boom")
        return "rule-good"

    status_calls: list[tuple] = []
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: bad + good)
    monkeypatch.setattr(self_learning, "log_proposed_mapping", _log)
    monkeypatch.setattr(self_learning, "_get_table", lambda: object())
    monkeypatch.setattr(
        self_learning, "_update_status",
        lambda table, mid, dat, status, extra=None, expect_status=None:
            status_calls.append(mid))

    result = sweep_value_promotion()

    assert result["promoted"] == 1
    assert result["subsumed"] == 6
    assert len(status_calls) == 6


def test_third_distinct_logged_value_auto_promotes_to_pending_rule(monkeypatch):
    """The third distinct pending value triggers one sweep automatically; the
    generated rule stays pending and all three observations are subsumed."""
    fake = FakeTable()
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    # ADR-0051 P5: staging off — this test pins the observe→sweep trigger, not
    # the co-reference buffer (covered by test_self_learning_lifecycle).
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    import agents.schema_check as schema_check
    monkeypatch.setattr(schema_check, "proposal_missing_field",
                        lambda *a, **k: "", raising=False)
    self_learning._reset_accepted_coverage_cache()
    value_promotion._reset_auto_promotion_state()

    mapping_ids = []
    for suffix in ("A", "B", "C"):
        mapping_ids.append(self_learning.log_proposed_mapping(
            from_system="sys-a", from_field="records.external_id",
            from_value=f"ENTITY-{suffix}",
            to_system="sys-b", to_field="entities.canonical_id",
            to_value=f"entity-{suffix.lower()}",
            concept="entity-id", confidence=0.8,
            query_context="resolved identifier",
            proposed_type="value_mapping", source="agent",
        ))

    assert all(mapping_ids)
    # Three observations meet the promotion threshold but not the transform
    # ladder's independent five-key confidence floor, so the existing sweep
    # emits its human-gated crosswalk rule rather than claiming a transform.
    (rule,) = fake.items_of_type("crosswalk")
    assert rule["status"] == "pending"
    for mapping_id in mapping_ids:
        row = fake.by_id(mapping_id)
        assert row["status"] == "superseded"
        assert row["subsumed_by"] == rule["mapping_id"]
    assert len(value_promotion._triggered_field_pairs) == 1


# --------------------------------------------------------------------------- #
# NEVER auto-accepted (ADR-0043 D5) — end-to-end through the REAL
# log_proposed_mapping with the flag ON and accept_mapping spied.
# --------------------------------------------------------------------------- #
class ConditionalCheckFailedException(Exception):
    """Fake of the DynamoDB conditional-failure error — production detection is
    by class name (boto3's ClientError subclass carries the same name)."""


class FakeTable:
    """In-memory stand-in for the proposals table (the
    test_self_learning_lifecycle.py double, slimmed to what this suite uses)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def seed(self, rows: list[dict]) -> None:
        for r in rows:
            self._items[(r["mapping_id"], r["discovered_at"])] = dict(r)

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)

    def items_of_type(self, proposed_type: str) -> list[dict]:
        return [dict(v) for v in self._items.values()
                if v.get("proposed_type") == proposed_type]

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        if ConditionExpression is not None:
            # Only the '#s = :expected' shape is emitted by _update_status.
            assert ConditionExpression == "#s = :expected"
            if item.get(names.get("#s", "status")) != values[":expected"]:
                raise ConditionalCheckFailedException(Key["mapping_id"])
        set_part = UpdateExpression.strip()
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                lhs, rhs = (s.strip() for s in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]


def _wire_end_to_end(monkeypatch, fake: FakeTable) -> list[str]:
    """Real log_proposed_mapping over FakeTable; auto-accept flag ON; every
    accept path spied. Returns the list accept calls land in."""
    accepted: list[str] = []
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: accepted.append(mid) or {"status": "accepted"})
    # Hermetic gates: empty catalog → gate 3d stores raw (its fail-safe);
    # schema gate reports nothing missing (no graph read).
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    import agents.schema_check as schema_check
    monkeypatch.setattr(schema_check, "proposal_missing_field",
                        lambda *a, **k: "", raising=False)
    return accepted


def test_promoted_join_path_is_never_auto_accepted_even_with_flag_on(monkeypatch):
    """End-to-end: flag ON, ladder-clearing transform → the promoted join_path
    row sits PENDING (join_path is outside _AUTO_ACCEPT_TYPES) and the sweep
    itself never calls accept."""
    fake = FakeTable()
    fake.seed([_vm_row(i, f"SN-D{i}", f"sn-d{i}") for i in range(1, 7)])
    accepted = _wire_end_to_end(monkeypatch, fake)

    result = sweep_value_promotion()

    assert result["promoted"] == 1
    assert result["subsumed"] == 6
    assert accepted == []  # the sweep NEVER accepts — full stop
    (jp,) = fake.items_of_type("join_path")
    assert jp["status"] == "pending"
    assert jp["transform"] == "casefold"
    assert jp["source"] == "agent"
    # Every per-value row superseded with subsumed_by=<the promoted id>.
    for i in range(1, 7):
        row = fake.by_id(f"vm-{i:03d}")
        assert row["status"] == "superseded"
        assert row["subsumed_by"] == jp["mapping_id"]
        assert row["superseded_at"]


def test_promoted_crosswalk_is_never_auto_accepted_even_with_flag_on(monkeypatch):
    """End-to-end: flag ON, disjoint vocabularies → the promoted crosswalk row
    sits PENDING (crosswalk is in the _NEVER_AUTO_ACCEPT_TYPES deny-tuple) and
    accept is never called."""
    fake = FakeTable()
    fake.seed([
        _vm_row(1, "LINE1", "DET-L1", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
        _vm_row(2, "LINE2", "DET-L2", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
        _vm_row(3, "LINE3", "DET-L3", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
    ])
    accepted = _wire_end_to_end(monkeypatch, fake)

    result = sweep_value_promotion()

    assert result["promoted"] == 1
    assert result["subsumed"] == 3
    assert accepted == []
    (xw,) = fake.items_of_type("crosswalk")
    assert xw["status"] == "pending"
    assert "crosswalk" in self_learning._NEVER_AUTO_ACCEPT_TYPES  # by construction
    assert "join_path" not in self_learning._AUTO_ACCEPT_TYPES    # by construction
    for i in (1, 2, 3):
        row = fake.by_id(f"vm-{i:03d}")
        assert row["status"] == "superseded"
        assert row["subsumed_by"] == xw["mapping_id"]


def test_two_run_idempotency_second_sweep_promotes_zero(monkeypatch):
    """End-to-end two-run idempotency: the first sweep collapses the group;
    the second sweep finds no promotable pending value_mappings (the subsumed
    rows are superseded, the promoted rule is a join_path) and promotes 0 —
    no duplicate rule row, nothing re-subsumed."""
    fake = FakeTable()
    fake.seed([_vm_row(i, f"SN-E{i}", f"sn-e{i}") for i in range(1, 7)])
    accepted = _wire_end_to_end(monkeypatch, fake)

    first = sweep_value_promotion()
    assert first["promoted"] == 1
    assert first["subsumed"] == 6

    second = sweep_value_promotion()
    assert second == _counts()
    assert accepted == []
    assert len(fake.items_of_type("join_path")) == 1  # exactly ONE rule row


def test_covered_by_accepted_rule_subsumes_against_existing_rule(monkeypatch, request):
    """End-to-end through the REAL log_proposed_mapping — the live defect
    path: the group's field pair is already covered by an ACCEPTED equivalent
    rule (from an earlier promotion or ordinary agent activity), so the
    crosswalk emit is absorbed as coverage evidence
    (``_lpm_covered_by_rule`` → ``log_proposed_mapping`` returns None). The
    sweep must treat the promotion as ALREADY SATISFIED: subsume the
    per-value rows referencing the EXISTING rule id, count
    ``already_promoted``, write NO new rule row — and the SECOND sweep is a
    true no-op (previously these groups re-ran the same dead emission on
    every sweep, forever, counted nowhere)."""
    common = dict(from_system="erp", from_field="work_orders.cell_ref",
                  to_system="mes-det", to_field="work_cells.cell_code")
    fake = FakeTable()
    fake.seed([
        _vm_row(1, "CELL1", "DET-C1", **common),
        _vm_row(2, "CELL2", "DET-C2", **common),
        _vm_row(3, "CELL3", "DET-C3", **common),
    ])
    # The EXISTING equivalent rule: an accepted crosswalk on the same field
    # pair whose exemplar equals the group's oldest aligned pair.
    fake.seed([{
        "mapping_id": "rule-accepted-1",
        "discovered_at": "2026-06-01T00:00:00+00:00",
        "proposed_type": "crosswalk", "status": "accepted",
        "from_value": "CELL1", "to_value": "DET-C1", **common,
    }])
    accepted = _wire_end_to_end(monkeypatch, fake)
    self_learning._reset_accepted_coverage_cache()
    request.addfinalizer(self_learning._reset_accepted_coverage_cache)

    first = sweep_value_promotion()

    assert first == _counts(already_promoted=1, subsumed=3)
    for i in (1, 2, 3):
        row = fake.by_id(f"vm-{i:03d}")
        assert row["status"] == "superseded"
        assert row["subsumed_by"] == "rule-accepted-1"  # the EXISTING rule
        assert row["superseded_at"]
    assert accepted == []  # the sweep still NEVER accepts (ADR-0043 D5)
    # No new rule row was written — the accepted rule stands alone.
    assert [r["mapping_id"] for r in fake.items_of_type("crosswalk")] == [
        "rule-accepted-1"]

    second = sweep_value_promotion()
    assert second == _counts()  # true no-op: nothing pending, nothing gated


def test_row_accepted_between_snapshot_and_subsume_keeps_accepted(monkeypatch):
    """S-B race: an auto-accept (the post-/api/ask sweep_auto_accept) lands on
    one row AFTER the sweep snapshots pending but BEFORE the subsume. The
    conditional update raises ConditionalCheckFailedException on that row →
    the accept WINS (status stays accepted, no subsumed_by/superseded_at), the
    'subsumed' count excludes it, and the sweep continues over the rest."""
    fake = FakeTable()
    rows = [_vm_row(i, f"SN-F{i}", f"sn-f{i}") for i in range(1, 7)]
    fake.seed(rows)
    accepted = _wire_end_to_end(monkeypatch, fake)
    # Stale snapshot: the sweep sees all 6 rows as pending…
    snapshot = [dict(r) for r in rows]
    monkeypatch.setattr(self_learning, "get_pending_mappings",
                        lambda: snapshot)
    # …but vm-003 was accepted between snapshot and subsume.
    fake._items[("vm-003", rows[2]["discovered_at"])]["status"] = "accepted"

    result = sweep_value_promotion()

    assert result["promoted"] == 1
    assert result["subsumed"] == 5  # the raced row is NOT counted
    raced = fake.by_id("vm-003")
    assert raced["status"] == "accepted"       # the decision wins — never flipped
    assert "subsumed_by" not in raced
    assert "superseded_at" not in raced
    for i in (1, 2, 4, 5, 6):                  # the sweep continued
        row = fake.by_id(f"vm-{i:03d}")
        assert row["status"] == "superseded"
        assert row["subsumed_by"]
    assert accepted == []  # the sweep itself still never accepts


def test_recent_rejections_damp_the_promoted_emit_rows_stay_pending(monkeypatch):
    """Damping × promotion end-to-end through the REAL _classify_collision
    (ADR-0042 D3): the exact rule the sweep would emit has 2 recent human
    rejections → the emit is rejection_damped (log_proposed_mapping returns
    None) and the per-value rows stay pending — promotion is evidence, not
    privilege."""
    fake = FakeTable()
    fake.seed([
        _vm_row(1, "LINE1", "DET-L1", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
        _vm_row(2, "LINE2", "DET-L2", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
        _vm_row(3, "LINE3", "DET-L3", from_system="erp",
                from_field="work_orders.line_id",
                to_system="mes-det", to_field="production_lines.line_id"),
    ])
    _wire_end_to_end(monkeypatch, fake)

    # Two recent rejections of the EXACT crosswalk the sweep would emit —
    # disjoint vocabularies → crosswalk, keyed on the full value pair, so the
    # exemplar (oldest aligned pair: LINE1 → DET-L1) pins the grain.
    def _emit_rule():
        return self_learning.log_proposed_mapping(
            "erp", "work_orders.line_id", "LINE1",
            "mes-det", "production_lines.line_id", "DET-L1",
            "equipment-id", 0.8, "ctx",
            proposed_type="crosswalk", source="agent",
        )

    first = _emit_rule()
    assert first is not None
    assert self_learning.reject_mapping(first) == {"status": "rejected"}
    second = _emit_rule()  # 1 rejection → still inserts (with history)
    assert second is not None
    assert self_learning.reject_mapping(second) == {"status": "rejected"}

    result = sweep_value_promotion()

    # The emit was damped → nothing promoted, nothing subsumed. A steward
    # rejection is NOT an equivalent rule (ADR-0053: rejected rows never
    # satisfy the dedup probe) — the group reports GATED, visibly…
    assert result == _counts(gated=1)
    # …the per-value rows stay pending…
    for i in (1, 2, 3):
        assert fake.by_id(f"vm-{i:03d}")["status"] == "pending"
    # …and no new crosswalk row appeared (both prior ones remain rejected).
    assert sorted(i["status"] for i in fake.items_of_type("crosswalk")) == [
        "rejected", "rejected"]


# --------------------------------------------------------------------------- #
# Route — POST /api/proposed-mappings/promote-values (TestClient)
# --------------------------------------------------------------------------- #
def _route_client():
    from agents.server import promote_value_mappings
    app = FastAPI()
    app.add_api_route("/api/proposed-mappings/promote-values",
                      promote_value_mappings, methods=["POST"])
    return TestClient(app)


def test_promote_values_route_returns_sweep_counts(monkeypatch):
    counts = {"promoted": 2, "subsumed": 43, "skipped_below_threshold": 1}
    monkeypatch.setattr(value_promotion, "sweep_value_promotion", lambda: counts)
    resp = _route_client().post("/api/proposed-mappings/promote-values")
    assert resp.status_code == 200
    assert resp.json() == counts


# --------------------------------------------------------------------------- #
# Import purity (ADR 0020/0021): module level is stdlib-only.
# --------------------------------------------------------------------------- #
def test_module_is_import_pure_symbols():
    assert not hasattr(value_promotion, "boto3")
    assert not hasattr(value_promotion, "pgdriver")
    assert not hasattr(value_promotion, "self_learning")
    assert not hasattr(value_promotion, "join_discovery")


def test_imports_with_no_pg8000_or_aws():
    """Fresh interpreter with pg8000/agents.pgdriver/boto3/botocore BLOCKED still
    imports the module and runs the pure helpers — zero AWS/DB at import time."""
    code = (
        "import sys\n"
        "for _m in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[_m] = None\n"
        "from agents.value_promotion import (\n"
        "    group_value_mappings, independent_confirmations)\n"
        "rows = [{'proposed_type': 'value_mapping', 'from_system': 'a',\n"
        "         'from_field': 't.c', 'from_value': 'V1',\n"
        "         'to_system': 'b', 'to_field': 'u.d', 'to_value': 'W1'}]\n"
        "assert list(group_value_mappings(rows)) == [('a', 't.c', 'b', 'u.d')]\n"
        "assert independent_confirmations(rows) == 1\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout
