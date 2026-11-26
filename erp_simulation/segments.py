# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Customer segments: the ONE segment -> product-category map for the simulator.

The order simulator only assigns products whose category matches the
customer's segment. Only sub-categories that exist in product_site_routing are
valid; parent-level categories (Automotive, Beverages, Food) are excluded so no
order is created for an unroutable product.
"""

from typing import Dict, FrozenSet, Tuple

SEGMENT_CATEGORIES: Dict[str, FrozenSet[str]] = {
    'BEVERAGE': frozenset({'Carbonated Soft Drinks', 'Juices', 'Water'}),
    'AUTOMOTIVE': frozenset({
        'Engine Components', 'Transmission Components', 'Brake Components', 'Electrical Systems',
    }),
    'FOOD': frozenset({'Cereals', 'Snack Foods', 'Grain Products'}),
    'MACHINING': frozenset({'Machined Components'}),
}

# Segment used when a customer's segment is not in SEGMENT_CATEGORIES.
FALLBACK_SEGMENT = 'BEVERAGE'

# Machined parts are ordered in tens of units, not cases of bottles.
MACHINED_SKU_PREFIX = 'MACH-'


def allowed_categories(segment: str) -> FrozenSet[str]:
    """Product categories a customer of ``segment`` may order (unknown -> BEVERAGE)."""
    return SEGMENT_CATEGORIES.get(segment, SEGMENT_CATEGORIES[FALLBACK_SEGMENT])


def quantity_range(sku: str, qmin: int, qmax: int) -> Tuple[int, int]:
    """Inclusive order-quantity range for one line of ``sku``.

    500ML bottles use the full configured range, machined SKUs a tenth of it
    (at least 1..2), everything else half of it. The rule keys off the SKU,
    not the customer's segment, so a machined SKU picked through the legacy
    fallback-to-all-products path (a segment with no matching products) also
    gets the machined range.
    """
    if '500ML' in sku or '500ml' in sku:
        return qmin, qmax
    if sku.upper().startswith(MACHINED_SKU_PREFIX):
        return max(1, qmin // 10), max(2, qmax // 10)
    return qmin // 2, qmax // 2
