# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Crosswalk quality gates: similarity floor + direction dedup.

Live evidence: 6 questionable crosswalks reached the review queue, including a
zero-overlap BNR5↔NRD2 pair (incidental difflib ratio exactly 0.5 cleared the
old 0.5 floor) and digit-only coincidences ('9'↔'9'). And because the discovery
sweep runs BOTH directions (A→B and B→A), each bridge appeared TWICE as
mirrored proposals.

Fixes pinned here:
  * ALPHABETIC ANCHOR — a side with no alphabetic character never anchors a
    crosswalk; digit-only shared tokens never count toward the token-Jaccard.
  * FLOOR raised to 0.6 — the BNR5↔NRD2 fuzzy score (0.5) no longer clears.
  * DIRECTION-CANONICAL EMIT — ``_emit_crosswalk`` orders sides by system id,
    so A→B and B→A sweep passes produce ONE identical proposal (collapsed by
    the lookup-before-write dedup).

Pure/offline — proposal writes behind a spy. GENERIC synthetic values only.
"""
from __future__ import annotations

import agents.join_discovery as jd


# --------------------------------------------------------------------------- #
# Similarity floor — the evidence cases
# --------------------------------------------------------------------------- #

def test_bnr5_nrd2_zero_overlap_pair_is_rejected():
    """A nonsense pair: no shared token, incidental fuzzy 0.5."""
    assert jd._pair_similarity("BNR5", "NRD2") < jd.CROSSWALK_MIN_SIMILARITY
    assert jd.crosswalk_candidates(["BNR5"], ["NRD2"]) == []


def test_digit_only_values_never_anchor():
    """'9'↔'9' is identical text (fuzzy 1.0) but a bare number is coincidence."""
    assert jd._pair_similarity("9", "9") == 0.0
    assert jd.crosswalk_candidates(["9"], ["9"]) == []
    assert jd.crosswalk_candidates(["104", "205"], ["104", "301"]) == []


def test_digit_only_shared_token_does_not_clear_alone():
    """'Line 9' vs 'Bay 9' share only the digit token '9' — no word overlap."""
    assert jd._pair_similarity("Line 9", "Bay 9") < jd.CROSSWALK_MIN_SIMILARITY
    assert jd.crosswalk_candidates(["Line 9"], ["Bay 9"]) == []


def test_alpha_anchored_pairs_still_clear():
    """Positive controls: real token overlap / near-spellings keep working."""
    assert jd._pair_similarity("Press Line 1", "press line one") >= jd.CROSSWALK_MIN_SIMILARITY
    cands = jd.crosswalk_candidates(["Press Line 1"], ["press line one"])
    assert cands and cands[0]["to_value"] == "press line one"
    # Near-spelling with only a digit token shared: the FUZZY signal carries it.
    assert jd._pair_similarity("PUMP-01", "PMP-01") >= jd.CROSSWALK_MIN_SIMILARITY


def test_floor_is_raised_above_the_evidence_score():
    """Pin the constant: the BNR5↔NRD2 incidental ratio (0.5) must sit below it."""
    assert jd.CROSSWALK_MIN_SIMILARITY > 0.5


# --------------------------------------------------------------------------- #
# Bidirectional dedup — A→B and B→A collapse to ONE canonical proposal
# --------------------------------------------------------------------------- #

def test_emit_crosswalk_is_direction_canonical(monkeypatch):
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return "mid-1"

    monkeypatch.setattr(jd, "_log_proposed_mapping", spy)

    # Forward sweep pass (sys-a → sys-b) …
    jd._emit_crosswalk(
        from_system="sys-a", from_field="wo.machine_name",
        to_system="sys-b", to_field="asset.machine",
        concept="machine",
        candidate={"from_value": "Press Line 1", "to_value": "press line one",
                   "similarity": 0.72},
    )
    # … and the mirrored reverse pass (sys-b → sys-a) the sweep also runs.
    jd._emit_crosswalk(
        from_system="sys-b", from_field="asset.machine",
        to_system="sys-a", to_field="wo.machine_name",
        concept="machine",
        candidate={"from_value": "press line one", "to_value": "Press Line 1",
                   "similarity": 0.72},
    )

    assert len(calls) == 2
    assert calls[0] == calls[1], (
        "mirrored sweep passes must produce the IDENTICAL proposal so the "
        "lookup-before-write dedup collapses them to one queue entry"
    )
    # The canonical direction orders sides by system id.
    assert calls[0]["from_system"] == "sys-a"
    assert calls[0]["to_system"] == "sys-b"
    assert calls[0]["from_value"] == "Press Line 1"
    assert calls[0]["to_value"] == "press line one"
    assert calls[0]["provenance"] == "adr-0058-machine-miner"
    assert calls[0]["method"] == "cross-system-miner"
    assert calls[0]["source"] == "agent"
