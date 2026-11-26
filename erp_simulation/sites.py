# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MES site constants shared by the simulation orchestrator and the dashboard.

A leaf module (no imports) so the dashboard need not import the orchestrator.
"""

# MES site code -> simulation_events per-site column prefix
# (mes_<prefix>_scheduled/_running/_completed; the fourth site is ADR-0072).
# A plant whose columns the live table lacks is skipped by the INSERT, so
# listing it here is always safe.
SITE_EVENT_PREFIX = {
    'MILWAUKEE_BOTTLING': 'mke',
    'DETROIT_AUTOPARTS': 'det',
    'CEDARRAPIDS_FOOD': 'cr',
    'INDIANAPOLIS_MACHINING': 'ind',
}
SITE_EVENT_STATES = ('scheduled', 'running', 'completed')

#: Prefixes of plants only some accounts enable (none ship in this sample).
OPTIONAL_SITE_EVENT_PREFIXES = frozenset()


def optional_event_column(column: str) -> bool:
    """True for a per-site column of an optional plant (``mes_tul_*``)."""
    return any(column.startswith(f"mes_{prefix}_") for prefix in OPTIONAL_SITE_EVENT_PREFIXES)
