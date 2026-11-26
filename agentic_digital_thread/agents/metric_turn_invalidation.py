# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 D3 — the ONE home for dropping the per-turn metric-layer result
cache after a resolver-affecting graph mutation.

A cached ``computed`` / ``withheld`` result is only as fresh as the graph it
was resolved against: an ``AuthorityRule`` write or lifecycle change, or a
``Field-[:MAPS_TO]->Concept`` repoint / mint / remap / provision, can change
what ``metric_resolver.resolve_metric`` would answer. Every such writer is
decorated with :func:`invalidates_metric_turn_results`, which invalidates in
a ``finally`` — success, readback mismatch or a raise part-way (a partial
write may already have changed the graph). ``metric_turn_notice.invalidate``
is generation-guarded, so a resolve in flight during the mutation can never
store its pre-mutation result afterwards.

Stdlib-only and import-pure: ``metric_turn_notice`` is imported lazily, and a
cache drop never fails the write it follows.
"""
from __future__ import annotations

import functools

#: Set on every decorated writer so a test can pin the full writer set.
MARKER_ATTR = "__invalidates_metric_turn_results__"


def invalidate_metric_turn_results() -> None:
    """Drop the per-turn result cache (lazy import, never raises)."""
    try:
        from . import metric_turn_notice
        metric_turn_notice.invalidate()
    except Exception:  # noqa: BLE001 - a cache drop never fails a write  # nosec B110
        pass


def invalidates_metric_turn_results(fn):
    """Decorate a resolver-affecting graph writer: invalidate on EVERY exit."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            invalidate_metric_turn_results()
    setattr(_wrapper, MARKER_ATTR, True)
    return _wrapper
