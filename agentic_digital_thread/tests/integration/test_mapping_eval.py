# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the ADR-0012 mapping-quality eval.

Two lanes:

  * ``test_cmms_mapping_meets_thresholds`` (marker ``integration``, fixture
    ``live_graph``) — scores the LIVE cmms mappings against the authored key and
    asserts the thresholds. **Skips cleanly** when no graph is reachable (the
    ``live_graph`` fixture skips), so the suite stays green credential-free.

  * synthetic unit tests for ``load_key`` / ``score`` / ``format_report`` that run
    WITHOUT a graph against hand-built live-mapping dicts (all-correct pass, a
    must-map miss fail, and nothing_to_eval). These prove the scorer logic.
"""

from __future__ import annotations

import json

import pytest

from tests.integration import mapping_eval
from tests.integration.mapping_eval import (
    ScoreReport,
    format_report,
    key_path_for,
    load_key,
    read_live_mappings,
    score,
)


# ---------------------------------------------------------------------------
# Integration lane (live graph; skips without creds)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_cmms_mapping_meets_thresholds(live_graph: str) -> None:
    """LIVE cmms mappings must clear the ADR-0012 thresholds.

    ``live_graph`` skips when no reachable graph has an onboarded system, so this
    only runs against a real dev graph. If cmms specifically is not onboarded,
    ``read_live_mappings`` is empty -> ``nothing_to_eval`` -> we skip (never error).
    """
    key = load_key(key_path_for("cmms"))
    live = read_live_mappings("cmms")
    report = score(key, live)
    if report.status == "nothing_to_eval":
        pytest.skip("cmms has no live MAPS_TO mappings — onboard cmms first")

    msg = format_report(report)
    assert report.must_map_pass == report.must_map_total, msg
    assert report.overall_accuracy >= report.overall_floor, msg


@pytest.mark.integration
def test_cmms2_mapping_meets_thresholds(live_graph: str) -> None:
    """LIVE cmms2 (synonym schema) must clear the SAME ADR-0012 thresholds.

    This is the NAME-INDEPENDENCE generalization assertion: cmms2_maintenance has
    the SAME real-world meaning as cmms but DIFFERENT table/column names, scored
    with the SAME acceptable sets + thresholds — proving the mapper keys on
    MEANING, not memorized field names. The scorer (``score``/``read_live_mappings``/
    ``load_key``) is reused VERBATIM — no cmms2-specific logic.

    cmms2 onboarding requires a COLD redeploy+reseed (the synonym schema must be in
    the deployed demo RDS that ``discover`` introspects — see the W5 plan). When cmms2
    is not onboarded, ``read_live_mappings`` is empty -> ``nothing_to_eval`` -> we
    skip (never error), pointing at ``harness.sh warm-cmms2``.
    """
    key = load_key(key_path_for("cmms2"))
    live = read_live_mappings("cmms2")
    report = score(key, live)
    if report.status == "nothing_to_eval":
        pytest.skip(
            "cmms2 has no live MAPS_TO mappings — onboard cmms2 first "
            "(requires COLD redeploy+reseed, then `harness.sh warm-cmms2`)"
        )

    msg = format_report(report)
    assert report.must_map_pass == report.must_map_total, msg
    assert report.overall_accuracy >= report.overall_floor, msg


# ---------------------------------------------------------------------------
# load_key validation (no graph)
# ---------------------------------------------------------------------------

def test_load_key_validates_concept_ids() -> None:
    """The shipped cmms key loads and every concept id is a real core concept."""
    key = load_key(key_path_for("cmms"))
    assert key["system"] == "cmms"
    assert len(key["fields"]) >= 9
    must_map = [n for n, s in key["fields"].items() if s.get("must_map")]
    assert len(must_map) == 9


def test_load_key_cmms2_synonym_key_loads_with_nine_must_map() -> None:
    """The synonym cmms2 key loads (no concept-id error) with exactly 9 must-map,
    and shares ZERO must-map column names with cmms (name-independence)."""
    key = load_key(key_path_for("cmms2"))
    assert key["system"] == "cmms2"
    assert key["overall_floor"] == 0.90
    assert key["must_map_min_confidence"] == 0.60
    must_map = [n for n, s in key["fields"].items() if s.get("must_map")]
    assert len(must_map) == 9
    cmms_must_map_cols = {
        n.split(".", 1)[1]
        for n, s in load_key(key_path_for("cmms"))["fields"].items()
        if s.get("must_map")
    }
    cmms2_must_map_cols = {n.split(".", 1)[1] for n in must_map}
    assert cmms_must_map_cols.isdisjoint(cmms2_must_map_cols)


def test_load_key_rejects_unknown_concept(tmp_path) -> None:
    """An authored key referencing a non-core concept id fails fast."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "system: bad\nschema: x\nfields:\n"
        "  t.c: { acceptable: [not-a-real-concept], must_map: true }\n"
    )
    with pytest.raises(ValueError, match="not in catalog.CORE_CONCEPTS"):
        load_key(str(bad))


# ---------------------------------------------------------------------------
# Synthetic scoring fixtures (no graph)
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_key() -> dict:
    """A tiny authored-style key: 2 must-map + 1 normal + 1 ignore."""
    return {
        "system": "synth",
        "schema": "synth",
        "overall_floor": 0.90,
        "must_map_min_confidence": 0.60,
        "fields": {
            "assets.asset_tag": {"acceptable": ["equipment-id"], "must_map": True},
            "spare_parts.quantity_on_hand": {"acceptable": ["inventory-level"], "must_map": True},
            "assets.facility": {"acceptable": ["facility", "site"]},
        },
        "ignore": ["assets.created_at"],
    }


def _live(concept_conf: dict) -> dict:
    """Build a live-mapping dict: {field: (concept, confidence)} -> scorer shape."""
    return {
        fld: {"concept": c, "confidence": conf, "role": "identifier"}
        for fld, (c, conf) in concept_conf.items()
    }


def test_score_all_correct_passes(synthetic_key) -> None:
    """Every keyed field hits + must-map meets confidence -> pass at 100%."""
    live = _live({
        "assets.asset_tag": ("equipment-id", 0.95),
        "spare_parts.quantity_on_hand": ("inventory-level", 0.88),
        "assets.facility": ("facility", 0.91),
        "assets.created_at": ("schedule", 0.40),  # ignored — excluded
        "work_orders_raw.status_hint": ("order-status", 0.5),  # out-of-key — excluded
    })
    report = score(synthetic_key, live)
    assert report.status == "pass"
    assert report.passed is True
    assert report.must_map_pass == report.must_map_total == 2
    assert report.hits == report.present == 3
    assert report.overall_accuracy == 1.0
    assert report.ignored_present == ["assets.created_at"]
    assert [o["field"] for o in report.out_of_key] == ["work_orders_raw.status_hint"]


def test_score_must_map_miss_fails(synthetic_key) -> None:
    """A must-map field mapped outside its acceptable set fails the eval."""
    live = _live({
        "assets.asset_tag": ("asset-class", 0.95),  # WRONG concept (must-map miss)
        "spare_parts.quantity_on_hand": ("inventory-level", 0.88),
        "assets.facility": ("facility", 0.91),
    })
    report = score(synthetic_key, live)
    assert report.status == "fail"
    assert report.passed is False
    assert report.must_map_pass == 1 and report.must_map_total == 2
    assert any(f["field"] == "assets.asset_tag" for f in report.must_map_failures)


def test_score_must_map_low_confidence_fails(synthetic_key) -> None:
    """A must-map field below the confidence gate fails even if concept is right."""
    live = _live({
        "assets.asset_tag": ("equipment-id", 0.55),  # below 0.60 gate
        "spare_parts.quantity_on_hand": ("inventory-level", 0.88),
        "assets.facility": ("facility", 0.91),
    })
    report = score(synthetic_key, live)
    assert report.passed is False
    assert any("confidence" in f["reason"] for f in report.must_map_failures)


def test_score_overall_floor_miss_fails(synthetic_key) -> None:
    """The OVERALL-FLOOR threshold gates independently of the must-map gate.

    Both must-map fields are correct AND meet the confidence gate, but the one
    non-must-map keyed field misses, dropping overall accuracy to 2/3 = 0.67 —
    below the 0.90 floor. The eval must fail *on the floor* (ADR-0012's second
    threshold), not on must-map. Without this, the floor clause in
    ``ScoreReport.passed`` is never exercised in isolation offline.
    """
    live = _live({
        "assets.asset_tag": ("equipment-id", 0.95),                # must-map OK
        "spare_parts.quantity_on_hand": ("inventory-level", 0.88),  # must-map OK
        "assets.facility": ("asset-class", 0.91),                  # non-must-map MISS
    })
    report = score(synthetic_key, live)
    # must-map fully passes — the only reason for failure is the overall floor.
    assert report.must_map_pass == report.must_map_total == 2
    assert not report.must_map_failures
    assert report.hits == 2 and report.present == 3
    assert report.overall_accuracy < report.overall_floor  # 0.667 < 0.90
    assert report.passed is False
    assert report.status == "fail"


def test_score_nothing_to_eval(synthetic_key) -> None:
    """Empty live mappings -> nothing_to_eval (skip-clean), not a failure."""
    report = score(synthetic_key, {})
    assert report.status == "nothing_to_eval"
    assert report.passed is False
    assert report.present == 0


# ---------------------------------------------------------------------------
# format_report (writes the JSON demo artifact)
# ---------------------------------------------------------------------------

def test_format_report_pass_line_and_json(synthetic_key, monkeypatch, tmp_path) -> None:
    """format_report emits the demo line and writes the JSON artifact."""
    out = tmp_path / "mapping-eval-synth.json"
    monkeypatch.setattr(mapping_eval, "_report_path", lambda system: str(out))
    live = _live({
        "assets.asset_tag": ("equipment-id", 0.95),
        "spare_parts.quantity_on_hand": ("inventory-level", 0.88),
        "assets.facility": ("facility", 0.91),
    })
    line = format_report(score(synthetic_key, live))
    assert "mapped 3/3 fields correctly (100%)" in line
    assert "all 2 critical fields exact" in line
    assert json.loads(out.read_text())["status"] == "pass"


def test_format_report_names_failing_critical(synthetic_key, monkeypatch, tmp_path) -> None:
    """On a must-map miss the demo line names the offending critical field."""
    monkeypatch.setattr(mapping_eval, "_report_path",
                        lambda system: str(tmp_path / "r.json"))
    live = _live({
        "assets.asset_tag": ("asset-class", 0.95),
        "spare_parts.quantity_on_hand": ("inventory-level", 0.88),
        "assets.facility": ("facility", 0.91),
    })
    line = format_report(score(synthetic_key, live))
    assert "FAILED: assets.asset_tag" in line
