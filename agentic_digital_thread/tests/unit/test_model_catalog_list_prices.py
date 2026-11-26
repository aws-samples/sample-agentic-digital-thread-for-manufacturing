# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Catalog prices are the Amazon Bedrock list prices the deployment is billed.

Read from the AWS Price List API (us-east-1, on-demand, 2026-10-22): the
global cross-Region standard rate for a model the catalog runs on its
``global.`` profile, the Region's standard rate for the two Nova models that
have none. Three entries were wrong: Nova Lite v2 carried Nova Lite v1's price
(the global profile bills $0.30/$2.50 per MTok, not $0.06/$0.24); Opus 4.7
and 4.8 carried Opus 4.0's $15/$75 (they bill $5/$25); and Sonnet 5 kept the
$3/$15 its launch price was expected to rise to after 2026-08-31, but the
Price List still bills $2/$10. GPT-5.6 (Bedrock Mantle) is not on the Price
List, so it is not pinned here.
"""
import pytest

from agents import model_catalog

# family -> ($ per MTok input, $ per MTok output), from the Price List.
LIST_PRICES_PER_MTOK = {
    "anthropic.claude-sonnet-5-5": (2.0, 10.0),
    "anthropic.claude-opus-5-5": (4.0, 20.0),
    "anthropic.claude-sonnet-5": (2.0, 10.0),
    "anthropic.claude-sonnet-4-6": (3.0, 15.0),
    "anthropic.claude-sonnet-4-5-20250929-v1:0": (3.0, 15.0),
    "anthropic.claude-opus-4-7": (5.0, 25.0),
    "anthropic.claude-opus-4-8": (5.0, 25.0),
    "anthropic.claude-haiku-5-5": (0.10, 0.50),
    "anthropic.claude-haiku-4-5-20251001-v1:0": (1.0, 5.0),
    "amazon.nova-2-lite-v1:0": (0.30, 2.50),
    "amazon.nova-lite-v1:0": (0.06, 0.24),
    "amazon.nova-pro-v1:0": (0.80, 3.20),
}


@pytest.mark.parametrize("family", sorted(LIST_PRICES_PER_MTOK))
def test_catalog_price_is_the_list_price(family):
    entry = next(m for m in model_catalog.MODELS if m.family == family)
    per_mtok = (round(entry.input_per_1k * 1000, 6), round(entry.output_per_1k * 1000, 6))
    assert per_mtok == LIST_PRICES_PER_MTOK[family], family


# A model with a global profile, invoked under a geo profile (a saved older
# ``us.`` id the deployment keeps), bills the Price List's regional standard
# rate: 1.1x the global one for every such model.
REGIONAL_PRICES_PER_MTOK = {
    "anthropic.claude-sonnet-5-5": (2.2, 11.0),
    "anthropic.claude-opus-5-5": (4.4, 22.0),
    "anthropic.claude-sonnet-5": (2.2, 11.0),
    "anthropic.claude-sonnet-4-6": (3.3, 16.5),
    "anthropic.claude-sonnet-4-5-20250929-v1:0": (3.3, 16.5),
    "anthropic.claude-opus-4-7": (5.5, 27.5),
    "anthropic.claude-opus-4-8": (5.5, 27.5),
    "anthropic.claude-haiku-5-5": (0.11, 0.55),
    "anthropic.claude-haiku-4-5-20251001-v1:0": (1.1, 5.5),
    "amazon.nova-2-lite-v1:0": (0.33, 2.75),
}


def _per_mtok(price):
    return (round(price["input"] * 1000, 6), round(price["output"] * 1000, 6))


@pytest.mark.parametrize("prefix", ["us", "eu", "apac"])
@pytest.mark.parametrize("family", sorted(REGIONAL_PRICES_PER_MTOK))
def test_a_regional_id_of_a_global_profile_model_is_priced_at_the_regional_rate(family, prefix):
    assert _per_mtok(model_catalog.pricing(f"{prefix}.{family}")) == REGIONAL_PRICES_PER_MTOK[family]
    assert _per_mtok(model_catalog.pricing(f"global.{family}")) == LIST_PRICES_PER_MTOK[family]


def test_the_regional_rates_cover_every_global_profile_model():
    assert set(REGIONAL_PRICES_PER_MTOK) == {m.family for m in model_catalog.MODELS if m.global_profile}


@pytest.mark.parametrize("family", ["amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"])
def test_a_model_without_a_global_profile_keeps_its_regional_list_price(family):
    assert _per_mtok(model_catalog.pricing(f"us.{family}")) == LIST_PRICES_PER_MTOK[family]


def test_the_admin_catalog_lists_the_rate_of_the_id_it_offers():
    """The Admin list offers each model's resolve_id, so its price is that id's."""
    for entry in model_catalog.catalog("us-east-1")["orchestrator"] + model_catalog.catalog("us-east-1")["agent"]:
        price = model_catalog.pricing(entry["id"])
        assert (entry["input_per_1k"], entry["output_per_1k"]) == (price["input"], price["output"]), entry["id"]


def test_every_non_mantle_model_is_pinned():
    """A new catalog entry gets its list price checked here too."""
    unpinned = {m.family for m in model_catalog.MODELS
                if not m.mantle and m.family not in LIST_PRICES_PER_MTOK}
    assert unpinned == set()
