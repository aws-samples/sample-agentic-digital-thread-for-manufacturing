# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delivery-time geometry for purchase-order receipts (OTD support).

WHY THIS EXISTS
---------------
Out of the box the procurement simulator receives a PO and stamps
``actual_delivery_date`` with the receipt time (``NOW()``). A PO is received only
once it is due, so that time lands at or after the ``expected_delivery_date``
and every receipt reads as late against a strict ``actual <= expected`` test: an
on-time-delivery (OTD) view shows the same number for every supplier. That is
honest but useless for a demo that wants to *show* OTD and supplier variation.

This module computes a realistic delivery-time offset to add to the expected
date on receipt — most POs on or before the expected date, some late — as a pure
function of the PO's own identity and its supplier (deterministic, no global RNG
state) and ONE ``simulation_config`` lever:

    procurement_delivery_variability_minutes   (float, default 0.0)

DEFAULT IS ZERO ON PURPOSE. With the lever unset or 0, ``spread_minutes`` is 0.0
and the receipt code records ``actual_delivery_date`` as the real receipt time
(``NOW()``) exactly as before — so a deployment that does not opt in (the
workshop, every pre-existing env) is BYTE-IDENTICAL. Only a positive spread
makes the receipt label ``actual_delivery_date`` as ``expected + offset``. A demo
scenario sets the lever to a positive spread to make OTD believable. The lever
is data, not a code gate: there is no ``ADT_WORKSHOP`` branch here and no
deployment-stack change — isolation is by config default, not by a runtime flag
(see docs/workshop-mode.md and the demo-upgrades contract).

DETERMINISM
-----------
Each supplier gets its own on-time share from a stable hash of its key (a few
poor suppliers, most reliable), and each PO's offset comes from a stable hash of
the PO number, not ``random`` module state, so:
  * the same PO always receives the same offset (idempotent re-receipt),
  * the spread is reproducible in tests and across cycles, and
  * the frozen Indianapolis seed lots are never touched (they are created by the
    seed / story_history, not by ``receive_pos``; this helper only shapes NEW
    replenishment POs created by the running simulator).
"""
from __future__ import annotations

import hashlib
import math

#: The ``simulation_config`` key that opts a deployment into delivery-time
#: variability. Default 0.0 ⇒ zero offset ⇒ the receipt records NOW() (current behavior).
DELIVERY_VARIABILITY_KEY = "procurement_delivery_variability_minutes"

#: Per-supplier on-time share: ``_ON_TIME_MAX - _ON_TIME_RANGE * u**2`` for a
#: stable ``u`` in [0, 1) from the supplier key. Shares run from 97% down to
#: 60%, skewed toward reliable (mean ~85%) with a tail of poor suppliers, so an
#: OTD-by-supplier view has something to show. A PO with no supplier key uses
#: ``_DEFAULT_ON_TIME_SHARE``.
_ON_TIME_MAX = 0.97
_ON_TIME_RANGE = 0.37
_DEFAULT_ON_TIME_SHARE = 0.85

#: How far before the expected date an on-time PO can land, as a fraction of
#: the window. A late PO lands in (0, window] after it.
_EARLY_SPAN = 0.5

#: Upper bound on the configured spread, matching the ``simulation_config`` row's
#: declared maximum (lambda-db-init). A larger finite value is CLAMPED to this
#: rather than passed through — an unbounded minutes value multiplied by 60 and
#: handed to ``make_interval(secs => …)`` could overflow and abort PO receipt.
_MAX_VARIABILITY_MINUTES = 1440.0


def _unit_interval(key: str) -> float:
    """A stable, uniform-ish value in [0, 1) derived from ``key``.

    Deterministic (same key ⇒ same value), dependency-free, and independent of
    any global RNG — so the spread is reproducible and re-receipt is idempotent.
    """
    digest = hashlib.sha256((key or "").encode("utf-8")).digest()
    # First 8 bytes as an unsigned int, scaled into [0, 1).
    n = int.from_bytes(digest[:8], "big")
    return n / float(1 << 64)


def spread_minutes(variability_minutes) -> float:
    """The configured spread as a bounded window.

    0.0 when unset, non-numeric, non-finite or <= 0 (fail-safe: never widen the
    window on bad config); a finite value above ``_MAX_VARIABILITY_MINUTES`` is
    clamped to it so the resulting seconds cannot overflow ``make_interval``.
    """
    try:
        window = float(variability_minutes)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(window) or window <= 0:  # NaN, ±inf, or <=0
        return 0.0
    return min(window, _MAX_VARIABILITY_MINUTES)


def supplier_on_time_share(supplier_key) -> float:
    """The share of this supplier's POs that land on or before the expected date."""
    if supplier_key is None or str(supplier_key) == "":
        return _DEFAULT_ON_TIME_SHARE
    u = _unit_interval(f"supplier:{supplier_key}")
    return _ON_TIME_MAX - _ON_TIME_RANGE * u * u


def delivery_offset_minutes(po_number: str, variability_minutes: float,
                            supplier_key=None) -> float:
    """Signed minutes to add to ``expected_delivery_date`` for this PO.

    With ``p = supplier_on_time_share(supplier_key)`` and a stable ``u`` in
    [0, 1) from the PO number: ``u < p`` lands on time, in
    ``[-window * _EARLY_SPAN, 0]``; otherwise it lands late, in ``(0, window]``.
    So about ``p`` of a supplier's POs are on time, and a late PO is strictly
    after the expected date.

    A spread of 0 (the default, or bad config) returns ``0.0``; the receipt then
    records ``actual_delivery_date`` as NOW() (byte-identical to the pre-feature
    behavior).
    """
    window = spread_minutes(variability_minutes)
    if window == 0.0:
        return 0.0
    p = supplier_on_time_share(supplier_key)
    u = _unit_interval(po_number)
    if u < p:
        # On or before the expected date, never below the window's early end.
        return max(round(-window * _EARLY_SPAN * (1.0 - u / p), 3), -window * _EARLY_SPAN)
    # Strictly late, but never past the window (a sub-0.001 window keeps its
    # unrounded value rather than the rounding floor).
    late = window * (u - p) / (1.0 - p)
    rounded = round(late, 3)
    return rounded if 0.0 < rounded <= window else min(max(late, 1e-9), window)
