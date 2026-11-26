# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Base-mode catalog purity (ADR-0030 + ADR-0035): the LIVE catalog starts EMPTY.

In base mode the app ships ZERO demo systems in the live catalog — real systems
are onboarded at runtime via ``/api/onboard/connect``. The former dormant demo-seed
machinery has been DELETED entirely from production ``catalog.py`` (ADR-0035,
query-ownership redesign): there is no demo/seed system definition in the core tree
anymore, so the only invariant left to pin is that the module-level ``catalog`` is
empty at import.

Fully offline: no AWS, no Neptune, no DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.catalog as catalog  # noqa: E402


def test_live_catalog_is_empty_at_import():
    assert catalog.catalog.get_all() == []
    assert catalog.catalog.get_onboarded_ids() == set()
