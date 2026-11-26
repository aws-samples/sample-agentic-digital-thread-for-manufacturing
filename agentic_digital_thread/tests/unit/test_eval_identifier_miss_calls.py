# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""identifier_miss_calls: computed from public tool_result summaries only."""
from __future__ import annotations

from agents.eval_core import _with_identifier_miss_calls

MISS = "```\nIDENTIFIER-MISS:\ntoken: LOT-20260926-AZ8\nlooked_in: material_lots.lot_number\n```"


def test_counts_only_fenced_miss_blocks():
    result = {"tool_summaries": [MISS, "Found 5 work orders", "IDENTIFIER-MISS mentioned in prose",
                                 "prefix\n" + MISS]}
    assert _with_identifier_miss_calls({"total_tool_calls": 4}, result) == {
        "total_tool_calls": 4, "identifier_miss_calls": 2}


def test_zero_is_reported_when_summaries_were_measured():
    assert _with_identifier_miss_calls({}, {"tool_summaries": []}) == {"identifier_miss_calls": 0}


def test_absent_when_not_measured_or_no_metrics():
    assert _with_identifier_miss_calls({"a": 1}, {}) == {"a": 1}
    assert _with_identifier_miss_calls(None, {"tool_summaries": [MISS]}) is None
