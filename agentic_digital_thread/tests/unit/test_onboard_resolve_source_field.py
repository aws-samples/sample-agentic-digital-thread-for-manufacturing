# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guard for the resolve display field label (``_qualified_field``).

The mapper emits a mapping's ``field`` already table-qualified (``"table.column"``),
so joining ``f"{table}.{field}"`` for the human-readable ``source_field`` produced a
DOUBLED table segment (``table.table.column``). Value sampling stripped it correctly
so evidence was still produced, but the displayed/returned label was wrong. These
assert the label is joined exactly once, generically, with SYNTHETIC identifiers.

Pure string assembly — no AWS, no graph, no DB, no LLM.
"""
from __future__ import annotations

import agents.onboard as onboard


def test_qualified_field_does_not_double_already_qualified_field():
    # field already carries the "table." prefix (mapper convention) → not doubled.
    assert onboard._qualified_field("records", "records.cell") == "records.cell"


def test_qualified_field_preserves_nested_dotted_path():
    # A flattened struct column keeps its full dotted path after the single prefix.
    assert (onboard._qualified_field("records", "records.entity.request.cell")
            == "records.entity.request.cell")


def test_qualified_field_qualifies_a_bare_column():
    # A bare column (target-side convention) gets the table prefix added once.
    assert onboard._qualified_field("assets", "line_name") == "assets.line_name"


def test_qualified_field_handles_missing_parts():
    assert onboard._qualified_field("", "records.cell") == "records.cell"
    assert onboard._qualified_field("records", "") == "records"
    assert onboard._qualified_field("records", "records") == "records"


def test_qualified_field_no_doubled_signature():
    # The doubled-segment signature must never appear for a qualified field.
    out = onboard._qualified_field("segment_records", "segment_records.a.b.c")
    assert "segment_records.segment_records" not in out
    assert out == "segment_records.a.b.c"
