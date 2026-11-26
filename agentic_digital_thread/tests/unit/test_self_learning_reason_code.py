# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reason-code → failure-mode auto-learning (generic, offline).

Proves ``self_learning.propose_reason_code_mappings`` (and the ``runtime_app``
wiring that feeds it parsed rows) turns an opaque machine-stop reason/fault CODE
column into a ``value_mapping`` proposal through the SAME pipeline as every other
discovery — deriving the human label from a co-located description column, and
NEVER fabricating one when absent.

No AWS / Neptune / LLM / network. The DynamoDB table is an in-memory ``FakeTable``
wired via ``monkeypatch.setattr(self_learning, "_get_table", ...)``; auto-accept is
OFF by default so no Neptune seam is touched. GENERIC — synthetic column names and
codes only; no dataset/system/model literals.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
import agents.runtime_app as runtime_app


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
def fake_table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    # Auto-accept defaults OFF; assert it explicitly so no Neptune seam is exercised.
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


# ===========================================================================
# Pure classifier / opaqueness predicates (generic, no dataset literals)
# ===========================================================================
class TestPredicates:
    @pytest.mark.parametrize("name", ["reason_code", "faultCode", "STOP_CODE",
                                      "alarm_code", "trip_code", "downtime_code"])
    def test_reason_code_columns_detected(self, name):
        assert self_learning._is_reason_code_column(name) is True

    @pytest.mark.parametrize("name", ["asset_id", "reason", "description",
                                      "quantity", "state_name", "code"])
    def test_non_reason_code_columns_rejected(self, name):
        # "code" alone (no stop concept) and prose/id columns are NOT code columns.
        assert self_learning._is_reason_code_column(name) is False

    @pytest.mark.parametrize("value", ["131607000", "0", "E4012", "FLT-22", "3.14"])
    def test_opaque_codes(self, value):
        assert self_learning._is_opaque_code(value) is True

    @pytest.mark.parametrize("value", ["Jam detected", "Overheat", "", "   ", "N/A word"])
    def test_readable_values_not_opaque(self, value):
        assert self_learning._is_opaque_code(value) is False

    def test_numeric_opaque_from_non_string(self):
        assert self_learning._is_opaque_code(131607000) is True

    def test_human_label_predicate(self):
        assert self_learning._is_human_label("Jam detected") is True
        assert self_learning._is_human_label("Overheat") is True
        assert self_learning._is_human_label("131607000") is False
        assert self_learning._is_human_label("") is False


# ===========================================================================
# propose_reason_code_mappings — the required contract
# ===========================================================================
def test_opaque_code_with_colocated_description_yields_labelled_proposal(fake_table):
    """A reason-code column + a co-located description column → one value_mapping
    proposal whose to_value is the DERIVED description (not fabricated)."""
    rows = [
        {"reason_code": "131607000", "reason_description": "Feeder jam", "count": 4},
        {"reason_code": "220118001", "reason_description": "Overtemp trip", "count": 1},
    ]
    ids = self_learning.propose_reason_code_mappings(rows, "sys-a")
    assert len(ids) == 2

    items = {i["from_value"]: i for i in fake_table.all_items()}
    assert set(items) == {"131607000", "220118001"}
    jam = items["131607000"]
    assert jam["proposed_type"] == "value_mapping"
    assert jam["concept"] == self_learning._FAILURE_MODE_CONCEPT
    assert jam["from_system"] == "sys-a" and jam["to_system"] == "sys-a"
    assert jam["from_field"] == "reason_code"
    assert jam["to_field"] == "reason_description"
    assert jam["to_value"] == "Feeder jam"        # derived, not fabricated
    assert float(jam["confidence"]) == self_learning._REASON_CODE_LABELLED_CONFIDENCE
    assert jam["status"] == "pending"


def test_opaque_code_without_description_is_low_confidence_pending_no_label(fake_table):
    """Opaque codes with NO usable description → a low-confidence/pending proposal
    with an EMPTY label — never a fabricated one."""
    rows = [
        {"fault_code": "131607000", "occurrences": 9},
        {"fault_code": "131607000", "occurrences": 3},  # duplicate code → one proposal
    ]
    ids = self_learning.propose_reason_code_mappings(rows, "sys-b")
    assert len(ids) == 1

    item = fake_table.all_items()[0]
    assert item["from_value"] == "131607000"
    assert item["to_value"] == ""                 # no fabricated label
    assert float(item["confidence"]) == self_learning._REASON_CODE_PENDING_CONFIDENCE
    assert item["proposed_type"] == "value_mapping"


def test_unrelated_name_column_is_not_used_as_label(fake_table):
    """A co-located non-descriptive column (e.g. an asset name) must NOT become the
    label — that would be a fabricated mapping. With no real description present, the
    proposal stays pending with an empty label."""
    rows = [{"reason_code": "131607000", "asset_name": "Press-01", "line": 2}]
    ids = self_learning.propose_reason_code_mappings(rows, "sys-a")
    assert len(ids) == 1
    item = fake_table.all_items()[0]
    assert item["to_value"] == ""
    assert float(item["confidence"]) == self_learning._REASON_CODE_PENDING_CONFIDENCE


def test_related_stop_token_column_supplies_label(fake_table):
    """A ``reason`` prose column (sharing the ``reason_code`` stop concept) supplies
    the label even without a 'description'-style name."""
    rows = [{"reason_code": "77012", "reason": "Guard door open"}]
    ids = self_learning.propose_reason_code_mappings(rows, "sys-a")
    assert len(ids) == 1
    item = fake_table.all_items()[0]
    assert item["to_value"] == "Guard door open"
    assert item["to_field"] == "reason"


def test_already_readable_code_value_yields_nothing(fake_table):
    """When the code column already holds human-readable values (no opaque codes),
    there is nothing to learn — no proposal."""
    rows = [{"reason_code": "Feeder jam"}, {"reason_code": "Overtemp trip"}]
    assert self_learning.propose_reason_code_mappings(rows, "sys-a") == []
    assert fake_table.all_items() == []


def test_non_reason_code_result_yields_nothing(fake_table):
    """A result with no reason/fault-code column proposes nothing."""
    rows = [
        {"order_id": "abc", "quantity": 10, "status_code": 200},  # http-style, no stop concept
        {"order_id": "def", "quantity": 20, "status_code": 200},
    ]
    assert self_learning.propose_reason_code_mappings(rows, "sys-a") == []
    assert fake_table.all_items() == []


def test_empty_and_malformed_inputs_are_tolerated(fake_table):
    assert self_learning.propose_reason_code_mappings([], "sys-a") == []
    assert self_learning.propose_reason_code_mappings(None, "sys-a") == []
    assert self_learning.propose_reason_code_mappings([{"reason_code": "1"}], "") == []
    assert self_learning.propose_reason_code_mappings(["not-a-dict"], "sys-a") == []


def test_errors_propagate_from_storage(monkeypatch):
    """log_proposed_mapping is fail-loud — a storage error propagates (not swallowed)."""
    class Boom:
        def query(self, **kwargs):
            raise RuntimeError("dynamo down")
    monkeypatch.setattr(self_learning, "_get_table", lambda: Boom())
    with pytest.raises(RuntimeError, match="dynamo down"):
        self_learning.propose_reason_code_mappings(
            [{"reason_code": "131607000", "reason_description": "Jam"}], "sys-a"
        )


# ===========================================================================
# runtime_app wiring — row extraction + delegation (reuse, no duplication)
# ===========================================================================
class TestRuntimeWiring:
    def test_extract_rows_from_top_level_array(self):
        assert runtime_app._extract_result_rows('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_extract_rows_from_results_envelope(self):
        assert runtime_app._extract_result_rows('{"rows": [{"a": 1}]}') == [{"a": 1}]

    def test_extract_rows_from_non_json_or_truncated(self):
        assert runtime_app._extract_result_rows("no results") == []
        assert runtime_app._extract_result_rows('[{"a": 1}, {trunc') == []
        assert runtime_app._extract_result_rows("") == []

    def test_check_delegates_with_parsed_rows_and_system_id(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            "agents.self_learning.propose_reason_code_mappings",
            lambda rows, system_id: captured.append((system_id, rows)) or ["m1"],
        )
        tool_calls_log = [{"name": "sys-a___run_query", "toolUseId": "u1"}]
        tool_results = {"u1": '[{"reason_code": "131607000", "reason_description": "Jam"}]'}
        n = runtime_app._check_for_reason_code_mappings(tool_calls_log, {}, tool_results, "sess")
        assert n == 1
        assert captured == [("sys-a", [{"reason_code": "131607000", "reason_description": "Jam"}])]

    def test_non_prefixed_tool_is_skipped(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "agents.self_learning.propose_reason_code_mappings",
            lambda rows, system_id: called.append(system_id) or [],
        )
        tool_calls_log = [{"name": "explore_graph", "toolUseId": "u1"}]
        tool_results = {"u1": '[{"reason_code": "1"}]'}
        assert runtime_app._check_for_reason_code_mappings(tool_calls_log, {}, tool_results, "s") == 0
        assert called == []

    def test_per_system_failure_is_isolated(self, monkeypatch):
        def _boom(rows, system_id):
            raise RuntimeError("propose down")
        monkeypatch.setattr("agents.self_learning.propose_reason_code_mappings", _boom)
        tool_calls_log = [{"name": "sys-a___run_query", "toolUseId": "u1"}]
        tool_results = {"u1": '[{"reason_code": "1"}]'}
        # Best-effort at the wiring layer — does not raise, counts zero.
        assert runtime_app._check_for_reason_code_mappings(tool_calls_log, {}, tool_results, "s") == 0
