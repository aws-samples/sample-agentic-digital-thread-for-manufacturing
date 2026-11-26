# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQL-side identity transform (``resolve.transform_id``) and the ``{*}`` slot.

ADR-0086: a wildcard target renders as SQL LIKE ``%`` so the existing LIKE
branch in ``_try_pattern_transform`` (and the onboarding provision sample
check) consume it unchanged; a wildcard in the source only has to match.
"""
from agents.resolve import transform_id


def test_characterization_non_wildcard_transforms_are_unchanged():
    assert transform_id("X-{id}", "PLANT-2026-0{id}", "X-1234") == "PLANT-2026-01234"
    assert transform_id("PLANT-2026-0{id}", "X-{id}", "PLANT-2026-01234") == "X-1234"
    assert transform_id("X-{id}", "PLANT-2026-0{id}", "Y-1234") is None
    assert transform_id("X-{id}", "{id}", "X-12-34") == "12-34"


def test_no_placeholder_source_is_none():
    assert transform_id("X-ONLY", "PRT-{id}", "X-ONLY") is None
    assert transform_id("X-{*}", "PRT-{id}", "X-A") is None


def test_wildcard_target_renders_sql_like_percent():
    assert transform_id("{id}", "PRT-{id}-R-{*}", "5101") == "PRT-5101-R-%"


def test_wildcard_source_only_has_to_match():
    assert transform_id("PRT-{id}-R-{*}", "{id}", "PRT-5101-R-C") == "5101"
    assert transform_id("PRT-{id}-R-{*}", "{id}", "PRT-5101-R-") is None


def test_a_source_wildcard_matches_one_alphanumeric_token_only():
    for value in ("PRT-5101-R-C-D", "PRT-5101-R-C_x", "PRT-5101-R--", "PRT-5101-R-%"):
        assert transform_id("PRT-{id}-R-{*}", "{id}", value) is None


def test_a_captured_core_cannot_become_a_live_like_metacharacter():
    # Only the {*} slot may be a LIKE wildcard: %, _ and \ in the caller's
    # identifier are escaped (PostgreSQL's default LIKE escape is backslash).
    assert transform_id("X-{id}", "PRT-{id}-R-{*}", "X-51%01") == r"PRT-51\%01-R-%"
    assert transform_id("X-{id}", "PRT-{id}-R-{*}", "X-51_01") == r"PRT-51\_01-R-%"
    assert transform_id("X-{id}", "PRT-{id}-R-{*}", "X-51\\01") == "PRT-51\\\\01-R-%"


def test_escaping_only_applies_when_the_target_has_a_wildcard():
    # A non-wildcard target is byte-identical to the pre-change output.
    assert transform_id("X-{id}", "PRT-{id}", "X-51_01") == "PRT-51_01"
    assert transform_id("PRT-{id}-R-{*}", "ITM-{id}", "PRT-51_01-R-A") == "ITM-51_01"
