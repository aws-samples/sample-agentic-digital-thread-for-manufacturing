# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T1.2 — the PERMANENT core-concept neutrality gate (V3 guard).

``CORE_CONCEPT_DEFINITIONS`` feeds BOTH map-prompt surfaces (the 140-char
``concepts_prompt_block`` and the UNTRUNCATED ``concept_hints`` string), so any
demo/dataset literal in a description biases every customer onboarding. The
denylist below is PINNED — it names the literal families once found
contaminating 11 core descriptions. Demo examples live in
``agents/demo/concept_overlays.py`` (ADR-0044) and compose back in as T1.3
overlays; they must never return to the engine vocabulary.

Offline, pure — no AWS, no graph.
"""
from __future__ import annotations

import re

import pytest

from agents.catalog import CORE_CONCEPT_DEFINITIONS

# PINNED denylist (do not weaken; extend only). Families:
#   - demo site codes (word-bounded MKE / DET / CR / CRR),
#   - demo plant/city names,
#   - schema-qualified demo table references (mes_<site>.<table>),
#   - demo identifier prefixes (MKE- / DET- / CRR-),
#   - the demo equipment-class word lists (bottling line stations).
CORE_CONCEPT_DENYLIST: tuple[str, ...] = (
    r"\bMKE\b",
    r"\bDET\b",
    r"\bCRR?\b",
    r"(?i)milwaukee|detroit|cedar rapids",
    r"mes_\w+\.\w+",
    r"MKE-|DET-|CRR-",
    r"(?i)\b(washer|filler|capper|labeler|checkweigher|case_packer|palletizer)\b",
)


@pytest.mark.parametrize("concept_id", sorted(CORE_CONCEPT_DEFINITIONS))
def test_core_concept_description_is_domain_neutral(concept_id):
    """Every CORE_CONCEPT_DEFINITIONS value is free of the pinned demo-literal
    denylist — per concept, so a failure names the offending id and match."""
    description = CORE_CONCEPT_DEFINITIONS[concept_id]
    for pattern in CORE_CONCEPT_DENYLIST:
        match = re.search(pattern, description)
        assert match is None, (
            f"core concept {concept_id!r} description contains demo literal "
            f"{match.group(0)!r} (denylist pattern {pattern!r}) — demo examples "
            f"belong in agents/demo/concept_overlays.py (ADR-0044), never in "
            f"the engine vocabulary"
        )


def test_core_concept_descriptions_are_whitespace_normalized():
    """Descriptions are single-line, single-spaced — the invariant that keeps
    the two map-prompt surfaces byte-identical through the shared composition
    point (whitespace normalization there must be a no-op for core text)."""
    bad = {cid: d for cid, d in CORE_CONCEPT_DEFINITIONS.items()
           if " ".join(d.split()) != d}
    assert not bad, f"non-normalized core descriptions: {sorted(bad)}"


def test_demo_overlay_content_carries_the_moved_examples():
    """The examples removed by the neutral rewrite live in the demo overlay
    data (spot-pin a representative literal per family so the move is a MOVE,
    not a deletion)."""
    from agents.demo.concept_overlays import DEMO_CONCEPT_OVERLAYS

    blob = " ".join(
        " ".join(fields.values()) for fields in DEMO_CONCEPT_OVERLAYS.values()
    )
    for literal in ("MKE-WASH-001", "DET-CNC1-001", "CRR-MIXR-001",
                    "KF2000-MKE-01", "WO-MKE-2026-0041", "LOT-DET-ALU-2026-001",
                    "DET-RUN-0001", "MKE-BATCH-001", "B-2847",
                    "MILWAUKEE_BOTTLING", "production_runs.run_number",
                    "lot_mes_tracking", "Cedar Rapids", "checkweigher"):
        assert literal in blob, f"moved demo literal {literal!r} missing from overlays"
