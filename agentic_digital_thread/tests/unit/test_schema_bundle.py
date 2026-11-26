# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 1 (Task 1.1) — shared schema formatter, PII helpers, and bundle builder.

Pure/offline assertions. Uses ONLY synthetic data — no dataset literals. The graph
read (``get_system_schema``) is mocked; no strands / MCP / DB / boto3 is constructed.
"""
import json
from unittest.mock import patch

import pytest

import agents.schema_format as sf
import agents.pii as pii
import agents.schema_bundle as sb


# --------------------------------------------------------------------------- #
# format_schema_with_samples — columns + types + capped/redacted samples
# --------------------------------------------------------------------------- #
def test_format_schema_with_samples_dedups_and_caps():
    out = sf.format_schema_with_samples({"t": [("c", "int")]}, {"t": {"c": [1, 2, 2, 3, 4]}})
    # deduped + capped at 3 example values
    assert "t(c: int e.g. 1, 2, 3)" in out
    assert "4" not in out


def test_format_schema_with_samples_multi_column_uses_semicolon_separator():
    out = sf.format_schema_with_samples(
        {"t": [("c1", "int"), ("c2", "text")]},
        {"t": {"c1": [1, 2]}},
    )
    # columns separated by "; " so the comma-joined sample list is unambiguous
    assert "t(c1: int e.g. 1, 2; c2: text)" == out


def test_format_schema_with_samples_pii_named_column_is_redacted():
    out = sf.format_schema_with_samples(
        {"people": [("email", "varchar"), ("widget_code", "varchar")]},
        {"people": {"email": ["user1@example.com", "user2@example.com"], "widget_code": ["W-1", "W-2"]}},
    )
    # PII-named column: no raw values, [redacted] instead
    assert "email: varchar [redacted]" in out
    assert "user1@example.com" not in out
    # non-PII column keeps its samples
    assert "widget_code: varchar e.g. W-1, W-2" in out


def test_format_schema_with_samples_value_level_pii_in_non_pii_column():
    # A structured identifier in a NON-PII-named column is value-redacted.
    out = sf.format_schema_with_samples(
        {"logs": [("note", "text")]},
        {"logs": {"note": ["ref 123-45-6789 here", "call 415-555-0100"]}},
    )
    assert "[REDACTED_SSN]" in out
    assert "[REDACTED_PHONE]" in out
    assert "123-45-6789" not in out


def test_format_schema_with_samples_no_samples_falls_back_to_columns():
    out = sf.format_schema_with_samples({"t": [("c", "int")]}, None)
    assert out == "t(c: int)"


# --------------------------------------------------------------------------- #
# pii — patterns + column_looks_pii heuristic
# --------------------------------------------------------------------------- #
def test_redact_pii_covers_structured_identifiers():
    assert pii.redact_pii("ssn 123-45-6789") == "ssn [REDACTED_SSN]"
    assert pii.redact_pii("user3@example.com") == "[REDACTED_EMAIL]"
    assert pii.redact_pii("415-555-0100") == "[REDACTED_PHONE]"


@pytest.mark.parametrize("name", ["email", "user_email", "ssn", "phone_number",
                                   "date_of_birth", "first_name", "home_address", "cc_num"])
def test_column_looks_pii_true(name):
    assert pii.column_looks_pii(name) is True


@pytest.mark.parametrize("name", ["dashboard_id", "widget_code", "qty", "run_id",
                                   "wildcard", "status", "created_at"])
def test_column_looks_pii_false_no_overmatch(name):
    # "dashboard"/"wildcard" must NOT match on a "card" substring.
    assert pii.column_looks_pii(name) is False


# --------------------------------------------------------------------------- #
# build_schema_bundle — graph metadata only; samples merged separately
# --------------------------------------------------------------------------- #
_FAKE_GRAPH = json.dumps({
    "orders": {
        "columns": ["order_id", "qty", "email"],
        "column_types": {"order_id": "varchar", "qty": "integer", "email": "varchar"},
        "concept_map": {"order_id": {"concept": "order", "role": "identifier"}},
        "partition_keys": ["order_id"],
    }
})


def test_build_schema_bundle_metadata_verbatim_from_graph():
    with patch("agents.graph_tools.get_system_schema", return_value=_FAKE_GRAPH):
        bundle = sb.build_schema_bundle("sys-x", include_samples=False)
    t = bundle["tables"]["orders"]
    assert t["columns"] == ["order_id", "qty", "email"]
    assert t["column_types"] == {"order_id": "varchar", "qty": "integer", "email": "varchar"}
    assert t["partition_keys"] == ["order_id"]
    # include_samples=False -> no samples key emitted
    assert "samples" not in t
    # rendered carries columns+types (no example values)
    assert "order_id: varchar" in bundle["rendered"]
    assert "e.g." not in bundle["rendered"]


def test_build_schema_bundle_merges_samples_and_redacts():
    samples = {"orders": {"order_id": ["O-1", "O-2", "O-1"], "email": ["user1@example.com"]}}
    with patch("agents.graph_tools.get_system_schema", return_value=_FAKE_GRAPH):
        bundle = sb.build_schema_bundle("sys-x", samples, include_samples=True)
    t = bundle["tables"]["orders"]
    assert "samples" in t
    # non-PII column: deduped samples surfaced
    assert t["samples"]["order_id"] == ["O-1", "O-2"]
    # PII-named column dropped from samples entirely
    assert "email" not in t["samples"]
    assert "e.g. O-1, O-2" in bundle["rendered"]
    assert "user1@example.com" not in bundle["rendered"]


def test_build_schema_bundle_error_shape_returns_empty():
    with patch("agents.graph_tools.get_system_schema",
               return_value=json.dumps({"error": "No schema found"})):
        bundle = sb.build_schema_bundle("sys-x")
    assert bundle["tables"] == {}
    assert bundle["rendered"] == ""


def test_render_bundle_returns_rendered_string():
    assert sb.render_bundle({"rendered": "abc"}) == "abc"
    assert sb.render_bundle({}) == ""
    assert sb.render_bundle(None) == ""
