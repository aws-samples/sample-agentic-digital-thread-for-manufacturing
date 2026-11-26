# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Server half of the tool-result outcome parity pin.

``query_intelligence.is_failed_result`` (wasted-call scoring, the determinism
score, the ADR-0059 empty-join signal) and the frontend's
``classifyToolResult`` (HUD / trace / graph outcome) read the SAME fixture:
``frontend/src/test/fixtures/tool-result-outcome-cases.json``. The frontend
half lives in ``frontend/src/test/tool-result-outcome-parity.test.ts``.

Evidence driving it: a live ERP answer (eight successful queries, an honest
"no suppliers linked" conclusion) classified as failed because the phrases
"no results" / "does not exist" appeared deep in its prose.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents import query_intelligence as qi

_FIXTURE = (Path(__file__).resolve().parents[2]
            / "frontend" / "src" / "test" / "fixtures"
            / "tool-result-outcome-cases.json")
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_server_verdict_matches_shared_fixture(case):
    assert qi.is_failed_result(case["text"]) is case["server"]


def test_every_ui_server_disagreement_is_declared():
    """A case where the UI shows failed/empty but the server does not count
    it (or vice versa) must say so explicitly — silent drift is the bug."""
    undeclared = [c["id"] for c in _CASES
                  if (c["ui"] in ("failed", "empty")) != c["server"]
                  and not c.get("divergent")]
    assert undeclared == []


def test_fixture_pins_the_live_erp_case():
    ids = {c["id"] for c in _CASES}
    assert "erp-honest-absence-verbatim" in ids


def test_failure_marker_names_the_matched_needle():
    assert qi.failure_marker("Error: connection refused") == ("error", "error:")
    assert qi.failure_marker("No records found.") == ("empty", "no records")
    assert qi.failure_marker("42 rows returned") is None
    assert qi.failure_marker(None) is None  # type: ignore[arg-type]


def test_envelope_head_is_the_first_240_chars():
    long_text = "first line\n" + "x" * 500
    assert qi.envelope_head(long_text) == long_text[:qi.ENVELOPE_HEAD_CHARS]
    assert qi.envelope_head("  short\ntext  ") == "short\ntext"


def test_empty_join_does_not_fire_on_an_honest_prose_answer():
    erp = next(c["text"] for c in _CASES if c["id"] == "erp-honest-absence-verbatim")
    rows = "| wo | defect |\n|---|---|\n| WO-1 | CAP_TORQUE |"
    assert qi.classify_empty_join({"erp": erp, "mes-a": rows, "mes-b": rows}) is None
    assert qi.classify_empty_join(
        {"erp": "No records found.", "mes-a": rows}) == "erp"
