# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime correlation extractor routes narrative patterns to `insight` (Task 2.2).

The post-run CORRELATION-PROPOSAL parser in ``runtime_app`` used to log
sentence-shaped ``identity_rule`` proposals (``from_field=pattern[:80]``) with
systems guessed from a hardcoded display-token list. After the insight rework:

- narrative patterns become ``proposed_type="insight"`` with EMPTY
  ``from_field``/``to_field`` — the narrative lives in ``query_context``
  (``Auto-analysis: <pattern>``, which ingestion strips to form the
  annotation digest);
- system detection is catalog-derived (ids + names from the live catalog),
  canonicalized via ``system_normalize.normalize_system_id``;
- the extraction is a module-level helper
  (``_extract_correlation_insights``) so it is testable without the SSE
  generator.

Includes the source-grep regression tripwire asserting the old
``from_field=pattern`` emit is gone from ``runtime_app.py`` (mirrors the
source-inspection pattern of ``test_workflow_guidance_no_drift.py``).

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from agents.runtime_app import _extract_correlation_insights

# Minimal catalog fixture — the shape catalog.get_all() returns.
_CATALOG = [
    {"id": "erp", "name": "ERP", "plant": "All Sites", "system_type": "ERP"},
    {"id": "mes-mil", "name": "Milwaukee MES", "plant": "Milwaukee", "system_type": "MES"},
    {"id": "mes-det", "name": "Detroit MES", "plant": "Detroit", "system_type": "MES"},
    {"id": "cmms", "name": "CMMS", "plant": "All Sites", "system_type": "CMMS"},
]


def _block(body: str) -> str:
    return f"answer text\n---CORRELATION-PROPOSAL---\n{body}\n---END-PROPOSAL---\nmore text"


# --------------------------------------------------------------------------- #
# Narrative pattern → insight with EMPTY fields, systems canonical.
# --------------------------------------------------------------------------- #
def test_sentence_pattern_routes_to_insight_with_empty_fields():
    text = _block(
        "Pattern: Operator IDs in Milwaukee MES correlate with ERP work orders\n"
        "Confidence: HIGH"
    )
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    ins = out[0]
    assert ins["proposed_type"] == "insight"
    assert ins["from_field"] == ""
    assert ins["to_field"] == ""
    assert ins["concept"] == "correlation"
    assert ins["confidence"] == 0.9
    # Narrative preserved in query_context with the Auto-analysis prefix
    # (ingestion strips it to derive the annotation digest).
    assert ins["query_context"] == (
        "Auto-analysis: Operator IDs in Milwaukee MES correlate with ERP work orders"
    )


def test_systems_are_canonical_catalog_ids_first_mention_wins():
    text = _block(
        "Pattern: Milwaukee MES lot numbers match ERP order refs\nConfidence: high"
    )
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    # Display name "Milwaukee MES" (first mention) → canonical id mes-mil;
    # "ERP" (second mention) → erp. NOT lowercased display tokens like "mes".
    assert out[0]["from_system"] == "mes-mil"
    assert out[0]["to_system"] == "erp"


def test_single_system_block_skipped():
    # P11: a correlation relates TWO DISTINCT systems — one mentioned system
    # is an observation about a single system, not a correlation. (The
    # pre-P11 extractor accepted these; the pilot-env audit showed they flooded
    # the pending queue as unfalsifiable single-subject findings.)
    text = _block("Pattern: ERP order dates cluster around month-end\nConfidence: low")
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_same_system_both_slots_skipped():
    # P11: mentions dedupe on canonical id, so a block whose every mention
    # resolves to ONE system can never fill a distinct to_system → skipped.
    text = _block(
        "Pattern: mes-mil shift codes drift against Milwaukee MES lot numbers\n"
        "Confidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_zero_matched_systems_skips_block():
    text = _block("Pattern: Widget colors correlate with shipping delays\nConfidence: HIGH")
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_id_and_name_mentions_dedupe_to_one_system():
    text = _block(
        "Pattern: mes-mil (Milwaukee MES) shift codes correlate with ERP work orders\n"
        "Confidence: HIGH"
    )
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    # Both the id and the display name of the SAME system collapse to one
    # canonical mention (from_system) — ERP fills to_system, never mes-mil twice.
    assert out[0]["from_system"] == "mes-mil"
    assert out[0]["to_system"] == "erp"


def test_knowledge_graph_mention_is_dropped():
    # A catalog entry naming the graph itself normalizes to None (self-
    # referential) — never becomes an insight subject.
    kg_catalog = _CATALOG + [
        {"id": "digital-thread", "name": "Knowledge Graph", "plant": "", "system_type": "Graph"},
    ]
    text = _block("Pattern: Knowledge Graph nodes align with digital-thread edges\nConfidence: HIGH")
    assert _extract_correlation_insights(text, kg_catalog) == []


def test_empty_catalog_yields_no_insights():
    text = _block("Pattern: ERP and Milwaukee MES agree on lot ids\nConfidence: HIGH")
    assert _extract_correlation_insights(text, []) == []


# --------------------------------------------------------------------------- #
# Correlational markers exempt from the coverage-meta rejection.
# --------------------------------------------------------------------------- #
def test_correlational_narrative_with_coverage_verbs_still_extracts():
    """'When ERP holds a rush
    order, MES records show higher scrap rates' is a genuine correlation —
    the coverage verb+noun pair ('holds' … 'records') must not reject it."""
    catalog = _CATALOG + [
        {"id": "mes", "name": "MES", "plant": "", "system_type": "MES"},
    ]
    text = _block(
        "Pattern: when ERP holds a rush order, MES records show higher scrap rates\n"
        "Confidence: HIGH"
    )
    out = _extract_correlation_insights(text, catalog)
    assert len(out) == 1
    assert out[0]["query_context"] == (
        "Auto-analysis: when ERP holds a rush order, MES records show higher scrap rates"
    )


def test_coverage_meta_without_correlational_marker_still_rejected():
    from agents.runtime_app import _reject_insight_pattern

    # Pure inventory fact — still a coverage rejection.
    assert _reject_insight_pattern(
        "ERP holds records for every Milwaukee MES work order"
    ) == "coverage meta-observation (inventory fact, not a correlation)"
    # The correlational shape passes the gate outright.
    assert _reject_insight_pattern(
        "when ERP holds a rush order, MES records show higher scrap rates"
    ) is None


def test_correlational_marker_never_lifts_the_entailment_rejection():
    from agents.runtime_app import _reject_insight_pattern

    # A self-derivation stays rejected even when it name-drops a rate.
    assert _reject_insight_pattern(
        "scrap rate is the ratio of scrap quantity to total scrap output"
    ) == "mathematically entailed (quantity related to its own derivation)"


def test_max_three_blocks_per_query():
    blocks = "".join(
        f"---CORRELATION-PROPOSAL---\nPattern: ERP finding number {i} tracks with CMMS work order timing\nConfidence: HIGH\n---END-PROPOSAL---\n"
        for i in range(5)
    )
    out = _extract_correlation_insights(blocks, _CATALOG)
    assert len(out) == 3


def test_missing_pattern_line_falls_back_to_block_prefix():
    text = _block("ERP totals look correlated with CMMS downtime windows")
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    assert out[0]["query_context"].startswith("Auto-analysis: ERP totals look correlated")


def test_query_context_capped_at_500_chars():
    long_pattern = "ERP and CMMS " + ("x" * 600)
    text = _block(f"Pattern: {long_pattern}\nConfidence: HIGH")
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    assert len(out[0]["query_context"]) == 500
    assert out[0]["query_context"].startswith("Auto-analysis: ERP and CMMS ")


def test_no_correlation_blocks_returns_empty():
    assert _extract_correlation_insights("plain answer, no proposals", _CATALOG) == []


# --------------------------------------------------------------------------- #
# P11 narrative-shape gate: entailed derivations and coverage meta-
# observations are not correlations. Generic SHAPE fixtures only.
# --------------------------------------------------------------------------- #
def test_entailed_ratio_pattern_skipped():
    # The narrative relates a quantity to its own derivation (shared stem
    # "scrap" on both sides of "ratio of") — mathematically entailed, not a
    # discovered cross-system relationship. This is the pilot-env-audited shape.
    text = _block(
        "Pattern: ERP scrap percentage is the ratio of scrap count to output "
        "count in CMMS\nConfidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_entailed_derived_from_pattern_skipped():
    text = _block(
        "Pattern: Downtime rate in ERP is derived from downtime minutes "
        "recorded in CMMS\nConfidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_coverage_meta_observation_skipped():
    # "system X has data for Y" is an inventory fact, not a correlation.
    text = _block(
        "Pattern: CMMS has data for equipment downtime that ERP also references\n"
        "Confidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_coverage_tracks_records_shape_skipped():
    text = _block(
        "Pattern: ERP stores maintenance records while CMMS is queried live\n"
        "Confidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_genuine_two_system_correlation_still_extracted():
    # Regression guard: a real cross-system correlation without derivation or
    # coverage phrasing keeps flowing.
    text = _block(
        "Pattern: Machine faults in CMMS correlate with scrap spikes in ERP\n"
        "Confidence: HIGH"
    )
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    assert out[0]["from_system"] == "cmms"
    assert out[0]["to_system"] == "erp"
    assert out[0]["to_system"] != ""


# --------------------------------------------------------------------------- #
# Word-boundary token matching (F1 regression): a short system id must never
# match INSIDE an unrelated word — matching is \b-anchored per token.
# --------------------------------------------------------------------------- #
def test_erp_does_not_match_inside_enterprise():
    # Substring find would hit "erp" inside "ent-erp-rise" → false ERP insight.
    text = _block(
        "Pattern: Enterprise scheduling drifts around month-end\nConfidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_erp_does_not_match_inside_interpret():
    text = _block(
        "Pattern: Operators interpret shift codes inconsistently\nConfidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


def test_erp_still_matches_as_standalone_word():
    # \b-anchored matching keeps the legitimate standalone mention alive
    # (case-insensitive), including when punctuation-adjacent.
    text = _block(
        "Pattern: erp order totals spike alongside cmms fault bursts on Fridays.\n"
        "Confidence: HIGH"
    )
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    assert out[0]["from_system"] == "erp"
    assert out[0]["to_system"] == "cmms"


def test_hyphenated_id_matches_at_word_boundaries():
    # \b anchors on the alphanumeric edges of "mes-mil" — the hyphen inside the
    # id is fine, and boundary punctuation around it still matches.
    text = _block(
        "Pattern: (mes-mil) shift codes drift alongside ERP order timing\n"
        "Confidence: HIGH"
    )
    out = _extract_correlation_insights(text, _CATALOG)
    assert len(out) == 1
    assert out[0]["from_system"] == "mes-mil"
    assert out[0]["to_system"] == "erp"


def test_hyphenated_id_does_not_match_inside_longer_token():
    # "mes-mil" must NOT match inside "mes-milling" (trailing \b fails on the
    # following word character) — and the display name doesn't match either.
    text = _block(
        "Pattern: The mes-milling station logs duplicate codes\nConfidence: HIGH"
    )
    assert _extract_correlation_insights(text, _CATALOG) == []


# --------------------------------------------------------------------------- #
# Source-grep regression tripwires (the old emit shape must be GONE).
# --------------------------------------------------------------------------- #
def _runtime_app_source() -> str:
    import agents.runtime_app as runtime_app

    return Path(inspect.getsourcefile(runtime_app)).read_text(encoding="utf-8")


def test_source_has_no_from_field_pattern_emit():
    """Tripwire: the sentence-shaped identity_rule emit (`from_field=pattern`)
    must never return to runtime_app.py."""
    src = _runtime_app_source()
    assert "from_field=pattern" not in src, (
        "runtime_app.py must not truncate a narrative pattern into from_field — "
        "narrative correlations are `insight` proposals with EMPTY fields"
    )
    assert "to_field=pattern" not in src


def test_source_has_no_hardcoded_display_token_system_scan():
    """Tripwire: system detection is catalog-derived, not a hardcoded
    display-token list (which produced non-canonical ids like bare `mes`)."""
    src = _runtime_app_source()
    assert "'SiteWise', 'PLM', 'HighByte'" not in src
