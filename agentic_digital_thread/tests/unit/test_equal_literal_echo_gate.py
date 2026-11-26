# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Equal-literal echo gate (3g) + shared degeneracy classifier.

Pins the equal-literal echo gate:
  - ``log_proposed_mapping`` DROPS (returns ``None``, stores nothing, logs ONE
    info line) a SAME-SYSTEM ``value_mapping`` that echoes one literal across
    two DIFFERENT columns — the equal-literal cross-field echo. All sources
    (agent AND user) are gated. PROPHYLACTIC: this same-system shape is not
    currently present live — the live degenerate rows are all CROSS-system.
  - A different-value mapping over the same field pair still stores.
  - Existing gate 3b behavior is UNCHANGED (same-system same-field same-value
    still dropped by 3b; same-column different-table intra-system pair still
    preserved).
  - Generation-side crosswalk/cross-system storage is NOT broken: a
    ``crosswalk`` with equal from/to values stores normally, and a
    CROSS-system equal-value ``value_mapping`` still reaches the ADR-0051 P7
    collapse/store path — an ingestion drop would starve the value-promotion
    trigger and P7 corroboration. Review-side, the live CROSS-system
    equal-literal shape is surfaced by the ADVISORY
    ``cross_system_equal_literal`` badge flag (live-topology pins below) —
    the concrete shape the 2026-07-27 Phase-A curation rejected was
    ``cell HIO2 → line_name HIO2`` (equal-literal cell→line pairs; contrast
    a legitimate name→UUID crosswalk, whose literals DIFFER).
  - ``proposal_scoring.degeneracy_flags`` verdicts are unit-pinned.
  - Normalization: whitespace is trimmed (``' X ' vs 'X'`` IS an echo); case
    is NOT folded (``'X7' vs 'x7'`` is NOT an echo — a real transform).

No AWS / Neptune / LLM / network — DynamoDB behind an in-memory ``FakeTable``
via ``monkeypatch`` (the idiom of ``test_self_learning_join_degeneracy.py``).
Fixtures use demo-shaped field/value literals (``work_orders.cell``, ``FTW7``)
in TESTS ONLY, per the plan's acceptance pins; system names stay synthetic
(``sysA``/``sysB``). No demo literal enters ``agents/*`` (ADR-0044).
"""
from __future__ import annotations

import logging

import pytest

import agents.self_learning as self_learning
from agents.proposal_scoring import (
    degeneracy_flags,
    is_cross_system_equal_literal,
    is_equal_literal_echo,
)


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


# --------------------------------------------------------------------------- #
# In-memory table double (put + subject-key query is all log_proposed_mapping needs)
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

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
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "_neptune_write", lambda *a, **k: True)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


def _log_vm(from_field: str, from_value: str, to_field: str, to_value: str,
            *, from_system: str = "sysA", to_system: str = "sysA",
            proposed_type: str = "value_mapping", **kw):
    return self_learning.log_proposed_mapping(
        from_system, from_field, from_value,
        to_system, to_field, to_value,
        "production-line", 0.85, "which line ran the batch?",
        proposed_type=proposed_type, **kw,
    )


# ===========================================================================
# Pin 1 — echo suppressed: None returned, no row, ONE info line
# ===========================================================================
def test_echo_suppressed_returns_none_no_row_one_info_line(table, caplog):
    with caplog.at_level(logging.INFO, logger="agents.self_learning"):
        mid = _log_vm("work_orders.cell", "FTW7",
                      "production_lines.line_name", "FTW7")
    assert mid is None
    assert table.all_items() == []
    echo_lines = [r for r in caplog.records
                  if "equal-literal" in r.getMessage()]
    assert len(echo_lines) == 1
    assert echo_lines[0].levelno == logging.INFO


def test_echo_dropped_for_user_source_too(table):
    """ALL sources are gated (agent AND user) — a user recording 'these two
    fields carry the same literal' is describing a join_path, not a VM."""
    mid = _log_vm("work_orders.cell", "FTW7",
                  "production_lines.line_name", "FTW7", source="user")
    assert mid is None
    assert table.all_items() == []


def test_raising_predicate_falls_through_to_storage(table, monkeypatch):
    """Best-effort sibling contract: a raising predicate must not break the
    fail-loud propose path — the row falls through to storage."""
    import agents.proposal_scoring as proposal_scoring

    def _boom(*a, **k):
        raise RuntimeError("predicate exploded")

    monkeypatch.setattr(proposal_scoring, "is_equal_literal_echo", _boom)
    mid = _log_vm("work_orders.cell", "FTW7",
                  "production_lines.line_name", "FTW7")
    assert mid is not None
    assert len(table.all_items()) == 1


# ===========================================================================
# Pin 2 — a different-value mapping over the same field pair still stores
# ===========================================================================
def test_different_value_mapping_stores(table):
    mid = _log_vm("work_orders.cell", "FTW7",
                  "production_lines.line_name", "Fort Worth Line 7")
    assert mid is not None
    items = table.all_items()
    assert len(items) == 1
    assert items[0]["status"] == "pending"


# ===========================================================================
# Pin 3 — existing 3b behavior unchanged
# ===========================================================================
def test_same_system_same_field_same_value_still_dropped_by_3b(table, caplog):
    """3b runs BEFORE 3g in the pinned order — the same-field self-map is
    still ITS drop (the 3g line never fires for it)."""
    with caplog.at_level(logging.INFO, logger="agents.self_learning"):
        mid = _log_vm("orders.state", "OPEN", "orders.state", "OPEN")
    assert mid is None
    assert table.all_items() == []
    assert any("dropped degenerate" in r.getMessage() for r in caplog.records)
    assert not any("equal-literal" in r.getMessage() for r in caplog.records)


def test_same_column_different_table_intra_system_pair_preserved(table):
    """Same COLUMN across two DIFFERENT tables of one system is intra-system
    join evidence — neither 3b (table-aware) nor 3g (columns equal) drops it."""
    mid = _log_vm("work_orders.line_id", "L-1",
                  "production_lines.line_id", "L-1")
    assert mid is not None
    assert len(table.all_items()) == 1


def test_same_system_code_to_label_empty_target_still_stores(table):
    """The reason-code path's pending VM with an EMPTY label survives — empty
    values are never an echo."""
    mid = _log_vm("events.reason_code", "131607000",
                  "events.reason_code", "")
    assert mid is not None
    assert len(table.all_items()) == 1


# ===========================================================================
# Pin 4 — crosswalk evidence NOT broken; cross-system VM reaches P7
# ===========================================================================
def test_crosswalk_with_equal_values_stores(table):
    """The echo gate is value_mapping-only: an equal-value crosswalk still
    STORES (no ingestion drop — the review-side cross_system_equal_literal
    badge is the advisory surface for the degenerate equal-literal shape;
    equal values are evidence only for name→UUID bridges whose literals
    differ)."""
    mid = _log_vm("work_orders.cell", "FTW7",
                  "production_lines.line_name", "FTW7",
                  proposed_type="crosswalk")
    assert mid is not None
    items = table.all_items()
    assert len(items) == 1
    assert items[0]["proposed_type"] == "crosswalk"


def test_cross_system_crosswalk_equal_values_stores(table):
    mid = _log_vm("records.external_id", "A-100",
                  "entities.canonical_id", "A-100",
                  to_system="sysB", proposed_type="crosswalk")
    assert mid is not None
    assert len(table.all_items()) == 1


def test_cross_system_equal_value_vm_reaches_p7_path(table, monkeypatch):
    """A CROSS-system equal-value value_mapping is NOT an echo — it must
    still reach the ADR-0051 P7 collapse/store path (spied here; with no
    covering JoinPath it stores normally)."""
    calls: list[tuple] = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return False  # no covering JoinPath → store normally

    monkeypatch.setattr(self_learning, "_lpm_self_map_collapse", _spy)
    mid = _log_vm("work_orders.cell", "FTW7",
                  "production_lines.line_name", "FTW7", to_system="sysB")
    assert calls, "P7 self-map collapse never saw the cross-system pair"
    assert mid is not None
    assert len(table.all_items()) == 1


# ===========================================================================
# Pin 6 — normalization at the gate: trim yes, casefold no
# ===========================================================================
def test_gate_trims_whitespace_before_comparing(table):
    mid = _log_vm("work_orders.cell", " FTW7 ",
                  "production_lines.line_name", "FTW7")
    assert mid is None
    assert table.all_items() == []


def test_gate_does_not_casefold(table):
    """A case-changing pair encodes a REAL transform and must survive."""
    mid = _log_vm("work_orders.cell", "FTW7",
                  "production_lines.line_name", "ftw7")
    assert mid is not None
    assert len(table.all_items()) == 1


# ===========================================================================
# Pure predicate — is_equal_literal_echo
# ===========================================================================
class TestEqualLiteralEchoPredicate:
    def test_echo_shape_true(self):
        assert is_equal_literal_echo(
            "sysA", "work_orders.cell", "sysA",
            "production_lines.line_name", "FTW7", "FTW7") is True

    def test_whitespace_trimmed_is_echo(self):
        assert is_equal_literal_echo(
            "sysA", "t1.a", "sysA", "t2.b", " FTW7 ", "FTW7") is True

    def test_case_pair_is_not_echo(self):
        assert is_equal_literal_echo(
            "sysA", "t1.a", "sysA", "t2.b", "FTW7", "ftw7") is False

    def test_cross_system_is_not_echo(self):
        assert is_equal_literal_echo(
            "sysA", "t1.a", "sysB", "t2.b", "FTW7", "FTW7") is False

    def test_same_column_is_not_echo(self):
        # Same column across two tables = intra-system join evidence.
        assert is_equal_literal_echo(
            "sysA", "t1.line_id", "sysA", "t2.line_id", "L-1", "L-1") is False

    def test_empty_values_not_echo(self):
        assert is_equal_literal_echo(
            "sysA", "t1.a", "sysA", "t2.b", "", "") is False
        assert is_equal_literal_echo(
            "sysA", "t1.a", "sysA", "t2.b", "X", "") is False

    def test_empty_field_not_echo(self):
        assert is_equal_literal_echo(
            "sysA", "", "sysA", "t2.b", "X", "X") is False
        assert is_equal_literal_echo(
            "sysA", "t1.a", "sysA", "", "X", "X") is False

    def test_empty_systems_not_echo(self):
        assert is_equal_literal_echo(
            "", "t1.a", "", "t2.b", "X", "X") is False

    def test_system_spelling_variants_normalize(self):
        # "Sys-A" and "sys a" normalize to the same token → same system.
        assert is_equal_literal_echo(
            "Sys-A", "t1.a", "sys a", "t2.b", "X", "X") is True


# ===========================================================================
# Pin 5 — degeneracy_flags classifier
# ===========================================================================
class TestDegeneracyFlags:
    def test_echo_row_flags_equal_literal_echo_only(self):
        row = {"proposed_type": "value_mapping",
               "from_system": "sysA", "from_field": "work_orders.cell",
               "from_value": "FTW7",
               "to_system": "sysA", "to_field": "production_lines.line_name",
               "to_value": "FTW7"}
        assert degeneracy_flags(row) == ["equal_literal_echo"]

    def test_same_system_same_field_row_flagged(self):
        row = {"proposed_type": "join_path",
               "from_system": "sysA", "from_field": "orders.state",
               "from_value": "",
               "to_system": "sysA", "to_field": "orders.state",
               "to_value": ""}
        assert "same_system_same_field" in degeneracy_flags(row)

    def test_identity_self_mapping_flagged(self):
        row = {"proposed_type": "value_mapping",
               "from_system": "sysA", "from_field": "orders.state",
               "from_value": "OPEN",
               "to_system": "sysA", "to_field": "orders.state",
               "to_value": "OPEN"}
        flags = degeneracy_flags(row)
        assert "same_system_same_field" in flags
        assert "identity_self_mapping" in flags

    def test_clean_cross_system_row_has_no_flags(self):
        row = {"proposed_type": "value_mapping",
               "from_system": "sysA", "from_field": "records.external_id",
               "from_value": "A-100",
               "to_system": "sysB", "to_field": "entities.canonical_id",
               "to_value": "B-200"}
        assert degeneracy_flags(row) == []

    @pytest.mark.parametrize("ptype", ["insight", "field_annotation",
                                       "schema_drift"])
    def test_exempt_types_have_no_flags(self, ptype):
        row = {"proposed_type": ptype,
               "from_system": "sysA", "from_field": "t1.a",
               "from_value": "X",
               "to_system": "sysA", "to_field": "t2.b", "to_value": "X"}
        assert degeneracy_flags(row) == []

    def test_knowledge_graph_endpoint_flagged(self):
        row = {"proposed_type": "join_path",
               "from_system": "digital-thread", "from_field": "Asset.path",
               "from_value": "p1",
               "to_system": "sysB", "to_field": "t2.b", "to_value": "p1"}
        assert "knowledge_graph_endpoint" in degeneracy_flags(row)

    def test_crosswalk_equal_values_not_flagged_as_echo(self):
        row = {"proposed_type": "crosswalk",
               "from_system": "sysA", "from_field": "t1.a",
               "from_value": "X",
               "to_system": "sysA", "to_field": "t2.b", "to_value": "X"}
        assert "equal_literal_echo" not in degeneracy_flags(row)

    def test_same_column_different_tables_not_flagged(self):
        row = {"proposed_type": "join_path",
               "from_system": "sysA", "from_field": "work_orders.line_id",
               "from_value": "",
               "to_system": "sysA", "to_field": "production_lines.line_id",
               "to_value": ""}
        assert degeneracy_flags(row) == []


# ===========================================================================
# Pin 7 — cross_system_equal_literal: the LIVE degenerate topology.
# Values genericized; the system/field topology
# mirrors the verified pilot-env export replay — 5 vm asset_name→machine_identifier
# + 1 vm + 1 crosswalk with TABLE-QUALIFIED fields ('tbl.cell'→'hier.line_name').
# ===========================================================================
def _live_shaped_rows() -> list[dict]:
    """The 7 live-shaped degenerate rows (topology preserved, values generic)."""
    vm_names = ["Machine_A1", "Machine_B2", "Machine_C3", "Machine_D4",
                "Machine_E5"]
    rows = [
        {"proposed_type": "value_mapping",
         "from_system": "sysX", "from_field": "asset_name", "from_value": v,
         "to_system": "sysY", "to_field": "machine_identifier", "to_value": v}
        for v in vm_names
    ]
    rows.append(
        {"proposed_type": "value_mapping",
         "from_system": "sysY",
         "from_field": "segment_record_details_raw.cell", "from_value": "FW7",
         "to_system": "sysX",
         "to_field": "asset_hierarchy.line_name", "to_value": "FW7"})
    rows.append(
        {"proposed_type": "crosswalk",
         "from_system": "sysY",
         "from_field": "segment_record_details_raw.cell", "from_value": "LS8",
         "to_system": "sysX",
         "to_field": "asset_hierarchy.line_name", "to_value": "LS8"})
    return rows


class TestCrossSystemEqualLiteral:
    def test_all_seven_live_shaped_rows_flagged(self):
        """Every live-shaped degenerate row carries the advisory flag —
        including the table-qualified pair (column-token compare, not raw
        string compare)."""
        for row in _live_shaped_rows():
            assert "cross_system_equal_literal" in degeneracy_flags(row), row

    def test_live_shaped_rows_flag_advisory_only_no_echo(self):
        """The live rows are CROSS-system — the same-system echo flag must
        NOT co-fire on them."""
        for row in _live_shaped_rows():
            assert "equal_literal_echo" not in degeneracy_flags(row), row

    def test_name_to_uuid_crosswalk_stays_unflagged(self):
        """A name→UUID bridge (values DIFFER) is the legitimate crosswalk
        shape — equal values are evidence only there, never when byte-equal."""
        row = {"proposed_type": "crosswalk",
               "from_system": "sysX", "from_field": "asset_name",
               "from_value": "BNR5_Dryer7",
               "to_system": "sysY", "to_field": "asset_id",
               "to_value": "fb44e1eb-8e2d-45df-ae7a-000000000000"}
        assert degeneracy_flags(row) == []

    def test_cross_system_same_column_token_equal_values_not_flagged(self):
        """SAME column token on both sides with equal values is legitimate
        join corroboration — never cross_system_equal_literal."""
        row = {"proposed_type": "value_mapping",
               "from_system": "sysX", "from_field": "work_orders.work_order_id",
               "from_value": "WO-100",
               "to_system": "sysY", "to_field": "wo_events.work_order_id",
               "to_value": "WO-100"}
        assert "cross_system_equal_literal" not in degeneracy_flags(row)

    def test_predicate_requires_cross_system(self):
        assert is_cross_system_equal_literal(
            "sysX", "t1.a", "sysX", "t2.b", "V", "V") is False
        assert is_cross_system_equal_literal(
            "sysX", "t1.a", "", "t2.b", "V", "V") is False

    def test_predicate_no_casefold_and_trims(self):
        assert is_cross_system_equal_literal(
            "sysX", "t1.a", "sysY", "t2.b", " V7 ", "V7") is True
        assert is_cross_system_equal_literal(
            "sysX", "t1.a", "sysY", "t2.b", "V7", "v7") is False

    def test_predicate_empty_values_or_fields_not_flagged(self):
        assert is_cross_system_equal_literal(
            "sysX", "t1.a", "sysY", "t2.b", "", "") is False
        assert is_cross_system_equal_literal(
            "sysX", "", "sysY", "t2.b", "V", "V") is False

    def test_flag_never_fires_on_field_grain_types(self):
        """join_path/identity_rule are field-grain — the classifier applies
        the flag to value_mapping AND crosswalk only."""
        row = {"proposed_type": "join_path",
               "from_system": "sysX", "from_field": "t1.a", "from_value": "V",
               "to_system": "sysY", "to_field": "t2.b", "to_value": "V"}
        assert "cross_system_equal_literal" not in degeneracy_flags(row)

    def test_cross_system_rows_still_store_at_ingestion(self, table):
        """ADVISORY ONLY — the live cross-system shape is never dropped at
        the source (an ingestion drop would starve the value-promotion
        trigger and ADR-0051 P7 corroboration)."""
        mid = _log_vm("asset_name", "Machine_A1",
                      "machine_identifier", "Machine_A1", to_system="sysY",
                      from_system="sysX")
        assert mid is not None
        assert len(table.all_items()) == 1
