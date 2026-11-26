# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scoring-boundary ↔ frontend parity for the tolerant FOLLOWUPS marker.

ONE shared case list — ``tests/fixtures/followups_marker_cases.json`` — is
read here (``eval_core._FOLLOWUPS_RE`` / ``strip_followups``) AND by
``frontend/src/test/followups-marker-parity.test.ts`` (``findFollowupsMarker``
/ ``splitFollowups``). A regex change on either side that the other does not
mirror fails one of the two lanes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents import eval_core

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "followups_marker_cases.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_fixture_is_non_trivial():
    assert len(_CASES) > 20
    assert any(c["match"] for c in _CASES) and any(not c["match"] for c in _CASES)
    assert len({c["name"] for c in _CASES}) == len(_CASES)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_python_marker_matches_the_shared_fixture(case):
    match = eval_core._FOLLOWUPS_RE.search(case["text"])
    assert bool(match) is case["match"]
    assert eval_core.strip_followups(case["text"]) == case["body"]
    # splitFollowups' block is the text after the marker line
    assert (case["text"][match.end():] if match else None) == case["block"]
