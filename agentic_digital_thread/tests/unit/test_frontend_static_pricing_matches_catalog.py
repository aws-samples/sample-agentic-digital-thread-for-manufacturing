# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The frontend's static pricing fallback lists exactly the curated catalog.

``buildStaticPricing`` (frontend/src/shared/agents/pricing.ts) is what the
Agents page prices with while ``/api/admin/models`` is loading or unavailable.
It must name only models ``agents/model_catalog.py`` offers, keyed the way
``resolve_id`` keys them (inference-profile prefix, none for Bedrock Mantle
``openai.*`` ids), with the catalog's label and prices. Offline: the
TypeScript source is read as text.
"""
from __future__ import annotations

import re
from pathlib import Path

from agents import model_catalog

_PRICING_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "shared" / "agents" / "pricing.ts"

#: One fallback entry: ``[`${prefix}.<family>`]``, ``[`global.<family>`]`` or
#: ``['<family>']``, then the entry object.
_ENTRY = re.compile(
    r"\[(?:`(?:\$\{prefix\}|global)\.(?P<prefixed>[^`]+)`|'(?P<bare>[^']+)')\]:\s*"
    r"\{\s*label:\s*'(?P<label>[^']+)',\s*input:\s*(?P<input>[0-9.]+),\s*output:\s*(?P<output>[0-9.]+)\s*\}")


def _fallback_body() -> str:
    text = _PRICING_TS.read_text(encoding="utf-8")
    start = text.index("export function buildStaticPricing(")
    end = text.index("\n}\n", start)
    return text[start:end]


def _fallback_entries() -> dict:
    entries = {}
    for m in _ENTRY.finditer(_fallback_body()):
        family = m.group("prefixed") or m.group("bare")
        assert family not in entries, f"duplicate fallback entry {family}"
        entries[family] = {"prefixed": m.group("prefixed") is not None, "label": m.group("label"),
                           "input": float(m.group("input")), "output": float(m.group("output"))}
    return entries


def _catalog_entries() -> dict:
    return {m.family: {"prefixed": not m.family.startswith("openai."), "label": m.label,
                       "input": m.input_per_1k, "output": m.output_per_1k}
            for m in model_catalog.MODELS}


def test_parser_reads_every_entry_line():
    """Guard the guard: every entry line in the function body is parsed."""
    lines = [line for line in _fallback_body().splitlines() if "label:" in line]
    assert len(_fallback_entries()) == len(lines) > 0


def test_fallback_lists_exactly_the_catalog_models():
    assert set(_fallback_entries()) == set(_catalog_entries())


def test_fallback_keys_labels_and_prices_match_the_catalog():
    assert _fallback_entries() == _catalog_entries()


def test_the_regional_premium_matches_the_catalog():
    """``priceFor`` prices a geo id of a global-profile model like
    ``model_catalog.pricing`` does: the same premium."""
    text = _PRICING_TS.read_text(encoding="utf-8")
    (value,) = re.findall(r"export const REGIONAL_PREMIUM = ([0-9.]+);", text)
    assert float(value) == model_catalog.REGIONAL_PREMIUM


def test_fallback_keys_follow_resolve_id():
    """Prefixed exactly where ``resolve_id`` adds the inference-profile prefix."""
    for family, entry in _fallback_entries().items():
        resolved = model_catalog.resolve_id(family, "eu-central-1")
        assert entry["prefixed"] == (resolved != family), family


#: One generated key per model, ``global.`` literal or the geo ``${prefix}``.
_KEY = re.compile(r"\[(?:`(?P<tmpl>[^`]+)`|'(?P<bare>[^']+)')\]:\s*\{\s*label:")


def _fallback_keys(prefix: str) -> set:
    keys = set()
    for m in _KEY.finditer(_fallback_body()):
        key = m.group("tmpl") or m.group("bare")
        keys.add(key.replace("${prefix}", prefix))
    return keys


def test_fallback_keys_are_exactly_the_resolved_ids():
    """Each key is the id the catalog resolves the model to: ``global.`` for
    every model with a global profile (in every Region), the Region's geo
    prefix only for the rest, none for Mantle. The frontend's region prefix
    for an Asia Pacific host is the catalog's (``apac``)."""
    pricing_ts = _PRICING_TS.read_text(encoding="utf-8")
    for prefix, region in (("us", "us-east-1"), ("eu", "eu-west-1"), ("apac", "ap-southeast-2")):
        assert prefix == model_catalog.region_prefix(region)
        assert _fallback_keys(prefix) == set(model_catalog.all_resolved_ids(region)), region
    assert "return 'apac';" in pricing_ts


def test_geo_only_families_are_the_catalog_models_without_a_global_profile():
    """``geoPrefixForFleet`` reads the Region only from these ids; they must
    be exactly the catalog models that resolve to a geo profile."""
    text = _PRICING_TS.read_text(encoding="utf-8")
    match = re.search(r"const GEO_ONLY_FAMILIES = \[([^\]]*)\]", text)
    assert match, "GEO_ONLY_FAMILIES not found in pricing.ts"
    listed = set(re.findall(r"'([^']+)'", match.group(1)))
    expected = {m.family for m in model_catalog.MODELS
                if not m.mantle and not m.global_profile}
    assert listed == expected
