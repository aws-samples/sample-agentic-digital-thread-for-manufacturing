# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ERP dashboard's Advanced controls on a simulation_config row with NULLs.

A row ``scenarios.apply_scenario`` inserted (``active_scenario``, or a key the
deployed seed has not added yet) has no description and no bounds. pandas
reads those NULLs as NaN, and ``NaN or key`` is NaN, so Streamlit got a float
label and the whole Advanced panel failed ("bad argument type for built-in
operation", ADT-DEMO 2026-10-28). Reproduced against the old expressions.

    python -m pytest erp_simulation/tests/test_config_widgets.py -q
"""
from __future__ import annotations

import math
from decimal import Decimal

from erp_simulation.config_widgets import config_bound, config_label


def test_old_label_expression_passes_nan_through():
    nan = float("nan")
    assert isinstance(nan or "active_scenario", float)  # the bug


def test_label_falls_back_to_the_key_for_nan_none_and_blank():
    for missing in (float("nan"), None, "", "   "):
        assert config_label(missing, "active_scenario") == "active_scenario"


def test_label_keeps_a_real_description():
    assert config_label("Refresh interval", "dashboard_refresh_interval") == "Refresh interval"


def test_label_is_always_a_string():
    assert isinstance(config_label(12, "k"), str)


def test_bound_falls_back_for_nan_and_none():
    assert config_bound(float("nan"), 7.0) == 7.0
    assert config_bound(None, 7.0) == 7.0
    assert config_bound(Decimal("NaN"), 7.0) == 7.0


def test_bound_keeps_zero_and_real_values():
    # The old ``if min_val`` treated 0 as missing; 0 is a real bound.
    assert config_bound(0, 7.0) == 0.0
    assert config_bound(Decimal("1440.0"), 7.0) == 1440.0
    assert config_bound("2.5", 7.0) == 2.5


def test_bound_falls_back_for_garbage():
    assert config_bound("abc", 3.0) == 3.0
    assert math.isfinite(config_bound(float("inf"), 3.0))
