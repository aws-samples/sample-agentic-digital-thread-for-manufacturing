# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dashboard drill-in endpoints (Wave 2 Task 2C) — a SEPARATE router so
``dashboard.py`` (owned by the multi-site card work in the same wave) stays
untouched. The generic fan-out helpers (``_PinnedAuthority``,
``_onboarded_for_plant``) are IMPORTED from ``dashboard`` — importing is the
sanctioned sharing seam; only editing the same file conflicts.

``GET /api/dashboard/line-state/machines`` replaces the single-authority stub
behind the Line State click-to-drill: it fans out over EVERY onboarded system
(honoring the ``plant`` filter), pins authority to each in turn, and resolves
per-machine LATEST state where the system maps an ``event-time`` ordering
column — falling back to that system's per-state distribution (the same data
the panel shows) where it does not. Generic throughout: no system_type /
plant / dataset literals decide who contributes — only whether the concepts
actually resolve for that system (the ``dashboard_line_state`` contributor
filter). Auth is enforced by the app-wide ``AuthMiddleware`` like every other
``/api/*`` route.

Cost note: resolved ONLY on click (the drill), never on the periodic SSE
snapshot — the fan-out adds no recurring per-viewer scan cost.
"""
import logging

from fastapi import APIRouter

from . import metric_resolver as mr
from .dashboard import (
    _onboarded_for_plant,
    _PinnedAuthority,
    fanout_status,
    line_state_cached,
)
from .metric_multisite import site_of as _site_of, site_sort_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])

# Concept-driven metric/dimension ids (metric_layer seed registry — metadata,
# not dataset literals): the preferred per-machine LATEST view and the honest
# per-state distribution fallback.
_MACHINES_METRIC = "machine-state-latest"
_MACHINES_DIMENSION = "asset"
_DISTRIBUTION_METRIC = "machine-state-distribution"
_DISTRIBUTION_DIMENSION = "machine-state"


def _rows_of(grm) -> list[list]:
    """JSON-safe ``[dimension_value, value]`` rows — the exact row shape of
    ``resolve_metric_by_dimension_route`` (machines: [asset, state]; distribution:
    [state, count])."""
    return [[r.dimension_value, r.value] for r in grm.rows]


def _resolve_view(md, sid: str, metric: str, dimension: str) -> tuple[list | None, bool]:
    """Resolve ONE view for ONE system as ``(rows | None, source_failed)``.

    ``rows is None`` covers both "this system does not map the view" (the
    EXPECTED resolver boundary error — the generic contributor filter, no
    system_type test) and "resolved but empty". ``source_failed`` is True ONLY
    for an UNEXPECTED error, which is what the caller needs to report the sweep
    honestly instead of publishing a fabricated success."""
    try:
        grm = md.resolve_metric_by_dimension(
            metric, dimension, authority=_PinnedAuthority(sid),
        )
    except (mr.MetricResolverError, NotImplementedError, KeyError):
        return None, False
    except Exception:  # per-system boundary: log + report, never a 500
        logger.warning(
            "line-state %s resolve failed for system %r", metric, sid, exc_info=True
        )
        return None, True
    return (_rows_of(grm) if grm.rows else None), False


def _resolve_system_group(md, system: dict) -> tuple[dict | None, bool]:
    """Resolve ONE system's drill contribution as ``(group, source_failed)``.

    ``group`` is ``None`` when the system does not contribute (maps neither view
    — the generic contributor filter). ``source_failed`` is True when an
    UNEXPECTED error was hit, which is what lets the caller report the sweep
    honestly (``dashboard.fanout_status``) instead of publishing — and caching —
    a fabricated success. An expected mapping gap is NOT a failure.

    Preferred: ``machine-state-latest`` grouped by ``asset`` (needs that system's
    ``event-time`` field-concept mapping — the ``_compose_latest_grouped_sql``
    path). On any resolver boundary error — or an empty result, so the drill is
    never emptier than the card — fall back to the per-state distribution with
    ``kind="distribution"`` (the frontend renders the honest 'needs an event-time
    mapping' caption). Per-system fail-soft: never a 500."""
    sid = system.get("id")
    rows, failed = _resolve_view(md, sid, _MACHINES_METRIC, _MACHINES_DIMENSION)
    kind = "machines" if rows is not None else None
    if kind is None:
        rows, fallback_failed = _resolve_view(
            md, sid, _DISTRIBUTION_METRIC, _DISTRIBUTION_DIMENSION)
        failed = failed or fallback_failed
        if rows is None:
            return None, failed
        kind = "distribution"
    return {
        "system_id": sid,
        "name": system.get("name") or sid,
        "site": _site_of(system),
        "kind": kind,
        "rows": rows,
    }, failed


def _machines_payload(plant: str) -> dict:
    """Sync body of the drill route — the full per-system fan-out (worker
    thread — EVENT-LOOP OFFLOAD pattern in ``metric_resolver``; cached via
    ``dashboard.line_state_cached``).

    Per-system fail-soft, but NOT silently: UNEXPECTED source errors are counted
    and reported through the SAME honest rule as the Line State card
    (``dashboard.fanout_status``), so a transient fault is never published — or
    cached — as empty success."""
    from . import metric_dimension as md  # lazy — keeps the module graph acyclic
    systems: list[dict] = []
    resolved = failed = 0
    for system in _onboarded_for_plant(plant):
        if not system.get("id"):
            continue
        group, source_failed = _resolve_system_group(md, system)
        if source_failed:
            failed += 1
        if group is not None:
            resolved += 1
            systems.append(group)
    # THE unified site sort rule (metric_multisite.site_sort_key): (site, name)
    # case-insensitive with the empty/unsited site FIRST — the same order the
    # multi-site card breakdown uses (an empty site must NOT fall back to the
    # name, or the drill diverges from the cards).
    systems.sort(key=lambda s: site_sort_key(s["site"], s["name"]))
    status, error = fanout_status(resolved, failed)
    return {"status": status, "systems": systems, "error": error}


@router.get("/dashboard/line-state/machines")
async def dashboard_line_state_machines(plant: str = "all"):
    """Per-machine current state across ALL covering systems — the Line State
    click-to-drill (Task 2C), mirroring the ``dashboard_line_state`` fan-out.

    FAIL-SOFT: always HTTP 200 with the envelope; per-system boundary errors are
    contained in ``_resolve_system_group`` (that system falls back or is
    skipped). An UNEXPECTED per-system error is REPORTED, not hidden: ``status``
    becomes ``partial-error`` (some systems contributed) or ``error`` (none did),
    with a message in ``error`` — and neither is cached, so the next request
    re-probes instead of serving a fabricated empty drill for a whole TTL window.
    A system that simply does not map either view is an expected coverage gap and
    keeps ``status: "ok"``. Envelope::

        {"status": "ok" | "partial-error" | "error",
         "systems": [{"system_id": ..., "name": ..., "site": <plant|"">,
                      "kind": "machines" | "distribution",
                      "rows": [[asset, state], ...] | [[state, count], ...]},
                     ...],
         "error": None | <message>}

    EVENT-LOOP OFFLOAD + TTL CACHE: the sweep runs in a worker thread via the
    shared ``line_state_cached`` (~20s TTL, single-flight; its own cache key —
    the drill payload differs from the panel's) so repeated drill clicks and
    the panel don't stampede the resolver.
    """
    return await mr.offload_resolver(
        line_state_cached, ("line-state-machines", plant), _machines_payload, plant)
