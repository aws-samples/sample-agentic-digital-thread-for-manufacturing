# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guard for the generic ISA-95 concept-synonym enrichment.

Broadens name-based mapping by naming correct, GENERIC ISA-95 synonyms in the
concept definitions:
  * site           ~ plant / facility / location (site level)
  * production-line ~ line / work cell / cell / station / work center (below site)
  * equipment-id   ~ machine / asset / serial

Critically, a 'cell' is a WORK-CENTER-level location (below site), so it must be
associated with 'production-line', NEVER encoded as a site synonym. These
assertions are on the shared vocabulary text only — no AWS, no graph, no LLM.
"""
from __future__ import annotations

from agents.catalog import CORE_CONCEPT_DEFINITIONS, MAPPING_DISAMBIGUATION


def _defn(concept: str) -> str:
    return CORE_CONCEPT_DEFINITIONS[concept].lower()


def test_site_synonyms_present():
    d = _defn("site")
    for syn in ("plant", "facility", "location"):
        assert syn in d, f"site synonym '{syn}' missing"


def test_production_line_synonyms_present():
    d = _defn("production-line")
    for syn in ("work cell", "cell", "station", "work center", "line"):
        assert syn in d, f"production-line synonym '{syn}' missing"


def test_equipment_id_synonyms_present():
    d = _defn("equipment-id")
    for syn in ("machine", "asset", "serial"):
        assert syn in d, f"equipment-id synonym '{syn}' missing"


def test_cell_is_not_encoded_as_a_site_synonym():
    """A cell is a work-center level (below site). It must NOT be presented as a
    site synonym anywhere in the vocabulary."""
    site = _defn("site")
    # 'cell' as a bare token must not appear in the site definition.
    assert "cell" not in site, "cell wrongly associated with site"
    # The production-line definition owns 'cell' and explicitly places it below site.
    line = _defn("production-line")
    assert "cell" in line
    assert "below site" in line or "not to 'site'" in line or "not to site" in line


def test_disambiguation_places_cell_under_production_line():
    d = MAPPING_DISAMBIGUATION.lower()
    assert "work cell" in d or "'cell'" in d
    assert "production-line" in d
    # cell/work-center routed to production-line, NOT site.
    assert "not 'site'" in d or "not site" in d


def _listed_fields(concept: str) -> set[str]:
    """The ``Fields:`` names a concept definition lists (up to its first full stop)."""
    text = CORE_CONCEPT_DEFINITIONS[concept]
    listed = text.split("Fields:", 1)[1].split(".", 1)[0]
    return {f.strip() for f in listed.split(",") if f.strip()}


def test_pass_rate_columns_belong_to_quality_yield_rate_only():
    """A 0-100 pass/yield rate is ``quality-yield-rate``: the metric and card
    layers read it there (``oee-quality`` prefer_stored, ``quality-by-line``).
    If ``quality-metric`` also lists the rate columns, onboarding maps the rate
    beside the counts and the quality metrics withhold as ambiguous_binding."""
    rate_columns = {"pass_rate", "first_pass_yield"}
    assert rate_columns <= _listed_fields("quality-yield-rate")
    assert not rate_columns & _listed_fields("quality-metric")


def test_disambiguation_routes_pass_rate_to_quality_yield_rate():
    d = MAPPING_DISAMBIGUATION
    hint = next(line for line in d.splitlines() if "pass/yield RATE" in line)
    assert "'quality-yield-rate'" in hint
    assert "NOT 'quality-metric'" in hint
    assert "quality_checks.pass_rate" in hint
    assert "never map a quality concept to check_id or any *_id" in hint
