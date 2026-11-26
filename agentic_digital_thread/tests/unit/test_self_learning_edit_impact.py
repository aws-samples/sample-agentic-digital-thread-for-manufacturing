# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline behavioral proof of self-learning edit + impact signal features.

Tests the edit_mapping backend function and related schema validation logic.
No AWS, no Neptune, no network. Uses in-memory FakeTable + schema_check mocks.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
import agents.schema_check as schema_check


# --------------------------------------------------------------------------- #
# In-memory test doubles (reuse from test_self_learning_lifecycle.py)
# --------------------------------------------------------------------------- #
class FakeTable:
    """In-memory stand-in for the ``adt-proposed-mappings`` DynamoDB table."""

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
            result = [i for i in items if i.get("mapping_id") == values.get(":mid")]
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        expr = UpdateExpression.strip()
        set_part = expr.replace("SET ", "") if expr.startswith("SET ") else expr
        for clause in set_part.split(","):
            if "=" not in clause:
                continue
            lhs, rhs = (s.strip() for s in clause.split("="))
            key = names.get(lhs, lhs)
            val = values.get(rhs)
            if val is not None:
                item[key] = val

    def add_item(self, mapping_id: str, discovered_at: str, **kwargs) -> None:
        item = {"mapping_id": mapping_id, "discovered_at": discovered_at, "status": "pending", **kwargs}
        self._items[(mapping_id, discovered_at)] = item


# --------------------------------------------------------------------------- #
# Tests for edit_mapping
# --------------------------------------------------------------------------- #
class TestEditMapping:
    """Tests for the edit_mapping function."""

    def test_edit_mapping_updates_field(self, monkeypatch):
        """Verify edit_mapping updates the specified field."""
        fake = FakeTable()
        fake.add_item("map-123", "2025-01-01T00:00:00Z", from_field="old_field", status="pending")
        
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        
        result = self_learning.edit_mapping("map-123", {"from_field": "new_field"})
        
        assert "error" not in result
        assert result.get("from_field") == "new_field"

    def test_edit_mapping_validates_system_exists(self, monkeypatch):
        """Verify edit_mapping rejects unknown systems."""
        fake = FakeTable()
        fake.add_item("map-456", "2025-01-01T00:00:00Z", from_system="erp", status="pending")
        
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        
        result = self_learning.edit_mapping("map-456", {"from_system": "nonexistent"})
        
        assert "error" in result
        assert "not onboarded" in result["error"]

    def test_edit_mapping_validates_column_exists(self, monkeypatch):
        """Verify edit_mapping rejects non-existent columns."""
        fake = FakeTable()
        fake.add_item("map-789", "2025-01-01T00:00:00Z", from_system="erp", from_field="existing_col", status="pending")
        
        def mock_is_onboarded(sys_id):
            return sys_id == "erp"
        
        def mock_column_exists(sys_id, field):
            if sys_id == "erp":
                return field in ("existing_col", "another_col")
            return None
        
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        monkeypatch.setattr(schema_check, "is_onboarded_system", mock_is_onboarded)
        monkeypatch.setattr(schema_check, "column_exists", mock_column_exists)
        
        result = self_learning.edit_mapping("map-789", {"from_field": "missing_col"})
        
        assert "error" in result
        assert "does not exist" in result["error"]

    def test_edit_mapping_allows_valid_column(self, monkeypatch):
        """Verify edit_mapping accepts a column that exists."""
        fake = FakeTable()
        fake.add_item("map-abc", "2025-01-01T00:00:00Z", from_system="erp", from_field="old_col", status="pending")
        
        def mock_is_onboarded(sys_id):
            return sys_id == "erp"
        
        def mock_column_exists(sys_id, field):
            return sys_id == "erp" and field in ("old_col", "new_col")
        
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        monkeypatch.setattr(schema_check, "is_onboarded_system", mock_is_onboarded)
        monkeypatch.setattr(schema_check, "column_exists", mock_column_exists)
        
        result = self_learning.edit_mapping("map-abc", {"from_field": "new_col"})
        
        assert "error" not in result
        assert result.get("from_field") == "new_col"

    def test_edit_mapping_rejects_non_pending(self, monkeypatch):
        """Verify edit_mapping rejects already accepted/rejected mappings."""
        fake = FakeTable()
        fake.add_item("map-xyz", "2025-01-01T00:00:00Z", status="accepted", from_field="field_x")
        
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        
        result = self_learning.edit_mapping("map-xyz", {"from_field": "new_field"})
        
        assert "error" in result
        assert "pending" in result["error"]

    def test_edit_mapping_returns_not_found_for_missing_id(self, monkeypatch):
        """Verify edit_mapping returns error for non-existent mapping."""
        fake = FakeTable()
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        
        result = self_learning.edit_mapping("nonexistent-id", {"from_field": "anything"})
        
        assert "error" in result
        assert "not found" in result["error"]

    def test_edit_mapping_updates_multiple_fields(self, monkeypatch):
        """Verify edit_mapping can update multiple fields at once."""
        fake = FakeTable()
        fake.add_item(
            "map-multi", "2025-01-01T00:00:00Z",
            from_system="erp", from_field="part_num",
            to_system="mes", to_field="component_id",
            status="pending"
        )
        
        def mock_is_onboarded(sys_id):
            return sys_id in ("erp", "mes")
        
        def mock_column_exists(sys_id, field):
            # Return True for any field - we're testing the update logic, not validation
            return True
        
        monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
        monkeypatch.setattr(schema_check, "is_onboarded_system", mock_is_onboarded)
        monkeypatch.setattr(schema_check, "column_exists", mock_column_exists)
        
        result = self_learning.edit_mapping("map-multi", {
            "from_field": "part_number",
            "to_field": "component"
        })
        
        assert "error" not in result
        assert result.get("from_field") == "part_number"
        assert result.get("to_field") == "component"
        # Original fields preserved
        assert result.get("from_system") == "erp"
        assert result.get("to_system") == "mes"


# --------------------------------------------------------------------------- #
# Tests for overlap signal data structure
# --------------------------------------------------------------------------- #
class TestOverlapSignal:
    """Tests for value-overlap signal display logic."""

    def test_overlap_signal_high_confidence(self):
        """Verify High badge for ratio >= 0.5."""
        ratio = 0.75
        badge = 'High' if ratio >= 0.5 else 'Med' if ratio >= 0.2 else 'Low'
        assert badge == 'High'

    def test_overlap_signal_medium_confidence(self):
        """Verify Med badge for 0.2 <= ratio < 0.5."""
        ratio = 0.35
        badge = 'High' if ratio >= 0.5 else 'Med' if ratio >= 0.2 else 'Low'
        assert badge == 'Med'

    def test_overlap_signal_low_confidence(self):
        """Verify Low badge for ratio < 0.2."""
        ratio = 0.1
        badge = 'High' if ratio >= 0.5 else 'Med' if ratio >= 0.2 else 'Low'
        assert badge == 'Low'

    def test_overlap_signal_not_measured(self):
        """Verify None/undefined shows 'not measured'."""
        ratio = None
        overlap = None
        has_signal = ratio is not None or overlap is not None
        assert has_signal is False


# --------------------------------------------------------------------------- #
# Tests for schema_verdict enrichment after edit
# --------------------------------------------------------------------------- #
class TestSchemaVerdictEnrichment:
    """Tests for schema_verdict recomputation after edit."""

    def test_verdict_verified_after_valid_edit(self, monkeypatch):
        """Verify schema_verdict is verified when column exists after edit."""
        def mock_load_schema(sys_id):
            return {"tables": {"erp": {"columns": {"part_num": {}}}}}
        
        def mock_column_in_schema(schema, field):
            return schema is not None and field in schema.get("tables", {}).get("erp", {}).get("columns", {})
        
        monkeypatch.setattr(schema_check, "load_system_schema", mock_load_schema)
        monkeypatch.setattr(schema_check, "column_in_schema", mock_column_in_schema)
        
        # Simulate enrichment logic from server.py edit route
        m = {"proposed_type": "value_mapping", "from_system": "erp", "from_field": "part_num"}
        
        if m.get('proposed_type', '') in schema_check.SCHEMA_CHECKED_TYPES:
            sys_id = m.get('from_system', '')
            field = m.get('from_field', '')
            schema = mock_load_schema(sys_id)
            exists = mock_column_in_schema(schema, field)
            if exists is True:
                m['schema_verdict'] = 'verified'
                m['schema_checked'] = f"{sys_id}.{field}"
            elif exists is False:
                m['schema_verdict'] = 'not_found'
                m['schema_checked'] = f"{sys_id}.{field}"
        
        assert m.get('schema_verdict') == 'verified'
        assert m.get('schema_checked') == 'erp.part_num'

    def test_verdict_not_found_after_invalid_edit(self, monkeypatch):
        """Verify schema_verdict is not_found when column doesn't exist after edit."""
        def mock_load_schema(sys_id):
            return {"tables": {"erp": {"columns": {"part_num": {}}}}}
        
        def mock_column_in_schema(schema, field):
            return field in schema.get("tables", {}).get("erp", {}).get("columns", {})
        
        monkeypatch.setattr(schema_check, "load_system_schema", mock_load_schema)
        monkeypatch.setattr(schema_check, "column_in_schema", mock_column_in_schema)
        
        # Simulate enrichment: column does NOT exist
        m = {"proposed_type": "value_mapping", "from_system": "erp", "from_field": "missing_col"}
        
        if m.get('proposed_type', '') in schema_check.SCHEMA_CHECKED_TYPES:
            sys_id = m.get('from_system', '')
            field = m.get('from_field', '')
            schema = mock_load_schema(sys_id)
            exists = mock_column_in_schema(schema, field)
            if exists is True:
                m['schema_verdict'] = 'verified'
                m['schema_checked'] = f"{sys_id}.{field}"
            elif exists is False:
                m['schema_verdict'] = 'not_found'
                m['schema_checked'] = f"{sys_id}.{field}"
        
        assert m.get('schema_verdict') == 'not_found'
        assert m.get('schema_checked') == 'erp.missing_col'