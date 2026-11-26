# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Safe widget inputs for a ``simulation_config`` row on the ERP dashboard.

pandas reads a NULL description or bound as NaN (or None), which is truthy, so
``description or key`` and ``float(min_val) if min_val else 0`` both let it
through. Streamlit then rejects a float label and the Advanced panel fails.
Pure; no Streamlit import, so it is testable on its own.
"""
from __future__ import annotations

import math


def config_label(description, key: str) -> str:
    """The row's description when it is a non-blank string, otherwise its key."""
    if isinstance(description, str) and description.strip():
        return description
    return str(key)


def config_bound(value, fallback: float) -> float:
    """``value`` as a finite float, or ``fallback`` when it is NULL, NaN or not a number.

    Zero is a real bound and is kept.
    """
    if value is None:
        return float(fallback)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if math.isfinite(number) else float(fallback)
