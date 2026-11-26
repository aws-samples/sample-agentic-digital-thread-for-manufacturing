# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-site metric resolution engine — generic, concept-driven fan-out.

Resolves ONE metric against EVERY onboarded system that covers it (one system
at a time, authority pinned per system) and combines the surviving per-system
values into a single aggregate, carrying a per-site breakdown alongside.

Design (mirrors the existing engine seams — ADR 0001 / ADR 0016 / ADR 0021):
  * **Concept-driven, zero literals.** Which systems participate is derived
    ONLY from graph metadata (``Field-[:MAPS_TO]->Concept`` edges, the same
    pattern as ``coverage.covered_concepts``) intersected with the onboarded
    catalog. No dataset, system, plant, or schema names appear here.
  * **Site = catalog ``plant``.** Each contributing system's site label is its
    catalog ``plant`` field; the wildcard ``'all'`` (or empty) means "no site"
    and is carried as ``""`` — the same rule as ``dashboard._system_context``.
  * **Injected impure seams.** ``neptune_query`` (graph read) and
    ``resolve_metric`` (per-system resolve) are default args, swappable in
    tests (the ``coverage.py`` convention). Everything else is pure.
  * **Fail soft per system, honest overall.** A per-system boundary error
    marks that site ``"error"`` and skips it (the ``dashboard_line_state``
    pattern). If at least one covering system was READ SUCCESSFULLY and
    reported no rows, the aggregate is an honest no-data answer (``value
    None``, ``contributing 0``) — ADR-0045's honest-data rule.
    ``MetricResolverError`` is raised only when nothing could be read at all:
    no coverage, or EVERY covering system errored. Callers fail soft.

Output shape of :func:`resolve_metric_multi` (the Wave-2A contract)::

    {"aggregate": {"value": <float|int|None>, "unit": <str>, "label": <str>,
                   "method": <"sum"|"mean"|"max"|"min"|"latest-not-combined">},
     "sites": [{"site": <plant|"">, "system_id": ..., "system_name": ...,
                "value": <float|int|None>,
                "status": "ok"|"no-data"|"invariant-violation"
                          |"unresolved-predicate"|"error"},
               ...],                       # sorted by (site, system_name)
     "contributing": <n>}                  # systems whose value entered the aggregate

Combination semantics (documented simplification):
  * SUM / COUNT → arithmetic sum of per-system values.
  * AVG → **simple arithmetic mean** of per-system values (NOT row-weighted;
    weighting needs per-site sample counts — the 2A ``sample_count`` seam
    enables that later without a payload change).
  * MAX / MIN → max / min of per-system values.
  * LATEST → the aggregate is SKIPPED with reason (``value: None``, method
    ``"latest-not-combined"``): there is no cross-system event ordering to
    pick a single "latest" honestly. Per-site values still ride in ``sites``.
  * Derived metrics → the WHOLE formula is evaluated per pinned system, then
    per-site results combine with the simple mean (per-site OEE → overall
    OEE = mean of site OEE).

Cost note: the fan-out multiplies backend queries by the number of covering
systems. It is intended to run ONLY inside the TTL-cached ``_build_cards``
path (wired in Task 2A) — never per-viewer / per-SSE-tick.
"""
from __future__ import annotations

import logging

from .catalog import catalog
from . import connection_types
from .db import neptune_query
from .metric_layer import Aggregation, DirectMetric, get_metric_layer
from . import metric_resolver as mr

logger = logging.getLogger(__name__)


# ONE graph read → every (system, concept) coverage pair. Same traversal as
# coverage._COVERED_CYPHER, additionally returning the system id so per-system
# covered sets (and hence derived-metric satisfiability per system) come from
# a single query instead of N scoped reads.
_COVERING_PAIRS_CYPHER = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->"
    "(:Field)-[:MAPS_TO]->(c:Concept) "
    "RETURN DISTINCT s.id AS system_id, c.id AS concept_id"
)


# Canonical pin shape lives in ``metric_resolver.PinnedAuthority`` (fix-pass:
# the former local copy here — and the one in ``dashboard`` — collapsed into
# that ONE class; this alias keeps this module's internal call sites stable).
_PinnedAuthority = mr.PinnedAuthority


def site_of(system: dict) -> str:
    """THE plant→site rule (single source, fix-pass consolidation): a catalog
    entry's site label is its ``plant`` field, with the wildcard ``'all'``
    (case-insensitive) or empty meaning "no site" → ``""`` (a wildcard system
    still contributes to aggregates, just unsited). Shared by
    ``dashboard._system_context`` / ``dashboard_line_state``,
    ``dashboard_drill``, and this module."""
    plant = system.get("plant")
    normalized = str(plant or "").strip().lower()
    # "all" / "all sites" / "all plants" are wildcard spellings seen in real
    # catalog entries — an unsited system, never a site named "All Sites".
    if not normalized or normalized in ("all", "all sites", "all plants"):
        return ""
    return str(plant)


# Back-compat internal alias (pre-consolidation name).
_site_of = site_of


def site_sort_key(site: str | None, name: str | None) -> tuple[str, str]:
    """THE site sort rule (single source, fix-pass consolidation): order by
    ``(site, name)`` case-insensitively, with the empty/unsited site (``""``)
    FIRST — unsited (wildcard-plant) systems lead, then sites alphabetically.
    Shared by :func:`resolve_metric_multi` and ``dashboard_drill``."""
    return ((site or "").lower(), (name or "").lower())


def _covered_by_system(query) -> dict[str, frozenset[str]]:
    """ONE graph read → ``{system_id: frozenset(concept ids)}``.

    ``neptune_query`` swallows errors → ``[]`` → ``{}`` (no-graph safe, same
    posture as ``coverage.covered_concepts``)."""
    covered: dict[str, set[str]] = {}
    for row in query(_COVERING_PAIRS_CYPHER) or []:
        sid, cid = row.get("system_id"), row.get("concept_id")
        if sid and cid:
            covered.setdefault(sid, set()).add(cid)
    return {sid: frozenset(cs) for sid, cs in covered.items()}


def systems_covering(target: str, *, query=neptune_query, metric_layer=None,
                     systems: list[dict] | None = None,
                     readable_only: bool = True) -> list[dict]:
    """Onboarded systems that cover ``target`` (a metric id OR a concept id).

    * ``target`` names a known metric → a system covers it iff the metric is
      satisfiable (``MetricLayer.is_satisfiable``, the resolver-mirroring rule
      incl. the ``prefer_stored`` OR-branch) against THAT system's covered
      concept set alone.
    * otherwise ``target`` is treated as a concept id → simple membership.

    One graph read, intersected with the onboarded catalog. Each entry carries
    ``{"system_id", "name", "site"}`` where ``site`` follows :func:`site_of`.
    ``systems`` overrides the catalog read (tests / pre-filtered callers).

    ``readable_only`` (default) drops systems the deterministic readers cannot
    read (``connection_types.structured_read_supported``). The fan-out GATES
    pass ``False``: they must count every covering system, because the
    single-authority path they fall back to still sees every mapping, so one
    readable plus one REST system would otherwise fail there as ambiguous."""
    covered = _covered_by_system(query)
    ml = metric_layer or get_metric_layer()
    try:
        metric = ml.get_metric(target)
    except KeyError:
        metric = None  # not a metric id → concept-membership semantics

    if metric is not None:
        def covers(concepts: frozenset[str]) -> bool:
            return ml.is_satisfiable(metric, concepts)
    else:
        def covers(concepts: frozenset[str]) -> bool:
            return target in concepts

    out: list[dict] = []
    for system in (systems if systems is not None else catalog.get_onboarded()):
        sid = system.get("id")
        if not sid or sid not in covered:
            continue
        if readable_only and not connection_types.structured_read_supported(system):
            # A vendor REST system can map the concept yet never answer the
            # composed SQL; listing it made every card show it as a failed site.
            continue
        if covers(covered[sid]):
            out.append({"system_id": sid,
                        "name": system.get("name") or sid,
                        "site": site_of(system)})
    return out


def _method_for(aggregation: Aggregation | None) -> str:
    """THE ``method`` label for an aggregation — the whole vocabulary lives here.

    Split out of :func:`_combine` so a no-data aggregate (zero contributors,
    nothing to combine) can still carry the SAME method token the frontend and
    the card layer already understand, instead of inventing a new one.
    ``aggregation is None`` ⇒ derived metric ⇒ simple mean (see module doc)."""
    if aggregation is None or aggregation is Aggregation.AVG:
        return "mean"
    if aggregation in (Aggregation.SUM, Aggregation.COUNT):
        return "sum"
    if aggregation is Aggregation.MAX:
        return "max"
    if aggregation is Aggregation.MIN:
        return "min"
    # LATEST: no cross-system event ordering exists → skip the aggregate with
    # reason rather than invent one (honest-by-construction).
    return "latest-not-combined"


def _combine(aggregation: Aggregation | None, values: list) -> tuple[float | int | None, str]:
    """Combine per-system values by the metric's aggregation → ``(value, method)``.

    ``values`` is non-empty when called (callers gate on ``contributing``)."""
    method = _method_for(aggregation)
    if method == "mean":
        return sum(values) / len(values), method
    if method == "sum":
        return sum(values), method
    if method == "max":
        return max(values), method
    if method == "min":
        return min(values), method
    return None, method  # latest-not-combined


def _no_data(resolved) -> bool:
    """THE honest-data predicate (ADR-0045), shared by every caller here.

    ``value is None`` **or** ``sample_count == 0`` ⇒ no-data. Checking only the
    first half would publish a degenerate ``0`` over ZERO rows as a real
    measurement; checking ``sample_count`` truthiness instead would hide a
    genuine measured zero. ``sample_count is None`` means the resolver did not
    report one — absence is NOT zero samples. Mirrors
    ``dashboard._metric_entry``'s rule so the two surfaces cannot drift."""
    return resolved.value is None or resolved.sample_count == 0


#: Resolver statuses that WITHHOLD a site's value, each keeping its OWN
#: card-register spelling — never collapsed into ``no-data`` (a declared
#: invariant violation, an unbound predicate concept and an ambiguous binding
#: NAME why the value is absent; ADR-0045 / ADR-0075 / ADR-0080 / ADR-0076).
_WITHHELD_SITE_STATUS = {
    mr.UNRESOLVED_PREDICATE_STATUS: "unresolved-predicate",
    mr.AMBIGUOUS_BINDING_STATUS: "ambiguous-binding",
    mr._INVARIANT_STATUS: "invariant-violation",
}

#: Site statuses that prove the system was READ (an answer, not a failure):
#: an honest empty read and every withheld register. A plant whose value is
#: withheld (its concept bound to two columns, an unbound filter, a declared
#: invariant) was queried and answered; counting it as "nothing read" made the
#: all-withheld fan-out raise, and on a multi-plant deployment the caller's
#: single-authority fallback then died on the per-plant authority partition
#: and rendered the WRONG reason (event 26, 2026-10-04: ``machine-speed``).
_READ_SITE_STATUSES = frozenset({"no-data", *_WITHHELD_SITE_STATUS.values()})


def _resolve_per_system(metric_id: str, covering: list[dict], resolve
                        ) -> tuple[list[dict], list]:
    """Resolve ``metric_id`` once per covering system → ``(sites, values)``.

    Per-system behavior (pure code motion out of :func:`resolve_metric_multi`):
    resolve OK with real data → ``"ok"`` and contributes; resolve OK but
    :func:`_no_data` (no rows / zero samples) → ``"no-data"`` with ``value
    None``, does not contribute; a boundary class or any unexpected exception
    (logged) → ``"error"``, does not contribute; an unbound predicate concept
    (ADR-0075) → ``"unresolved-predicate"`` and a declared invariant violation
    → ``"invariant-violation"`` — NOT no-data, neither contributes.
    ``values`` holds only the contributing values."""
    sites: list[dict] = []
    values: list = []
    for entry in covering:
        sid = entry["system_id"]
        try:
            resolved = resolve(metric_id, authority=_PinnedAuthority(sid))
        except (mr.MetricResolverError, NotImplementedError, KeyError):
            # This system cannot satisfy the metric right now → skip it, the
            # remaining sites still aggregate (fail-soft, dashboard_line_state).
            sites.append({**_site_entry(entry), "value": None, "status": "error"})
            continue
        except Exception:  # nosec B112 - per-system boundary: log + skip, never propagate
            logger.warning("multi-site resolve failed for metric %r on system %r",
                           metric_id, sid, exc_info=True)
            sites.append({**_site_entry(entry), "value": None, "status": "error"})
            continue
        withheld = _WITHHELD_SITE_STATUS.get(resolved.status)
        if withheld is not None or _no_data(resolved):
            entry_out = {**_site_entry(entry), "value": None,
                         "status": withheld or "no-data"}
            if withheld is not None and resolved.detail:
                entry_out["detail"] = resolved.detail
            sites.append(entry_out)
            continue
        sites.append({**_site_entry(entry), "value": resolved.value,
                      "status": "ok"})
        values.append(resolved.value)
    return sites, values


def _aggregate_or_raise(metric_id: str, aggregation: Aggregation | None,
                        sites: list[dict], values: list
                        ) -> tuple[float | int | None, str]:
    """``(value, method)`` for the aggregate — or raise when nothing was READ.

    Zero contributors is NOT automatically a failure. If at least one covering
    system was read successfully and honestly reported no rows (``"no-data"``)
    or computed a value its declared invariant withheld
    (``"invariant-violation"``), the answer IS a withheld value — the honest-data vocabulary (ADR-0045: ``value
    None`` / no samples ⇒ ``no-data``, distinct from ``unavailable``). Raising
    for that state made ``dashboard._build_card_metric_multi`` re-run the WHOLE
    single-authority resolve for every card on every TTL rebuild, plus an
    ``exc_info`` traceback per card — measured as a contributor to the
    2026-08-24 single-task CPU saturation.

    ``MetricResolverError`` stays reserved for the genuinely unanswerable case:
    EVERY covering system errored, so nothing was read at all."""
    if values:
        return _combine(aggregation, values)
    if any(s["status"] in _READ_SITE_STATUSES for s in sites):
        return None, _method_for(aggregation)
    raise mr.MetricResolverError(
        f"metric {metric_id!r} resolved no value and errored on all "
        f"{len(sites)} covering system(s)")


def resolve_metric_multi(metric_id: str, *, resolver=None, query=neptune_query,
                         metric_layer=None, systems: list[dict] | None = None) -> dict:
    """Resolve ``metric_id`` per covering system (authority pinned) + combine.

    See the module docstring for the exact output shape (the Wave-2A contract)
    and the combination semantics; :func:`_resolve_per_system` documents the
    per-system statuses and :func:`_aggregate_or_raise` the honest-no-data rule.

    Fail loud (``MetricResolverError``) when: the metric id is unknown, ZERO
    onboarded systems cover it, or every covering system ERRORED. An all-no-data
    fan-out is an ANSWER (``value None`` / ``contributing 0``), not a failure —
    callers render it as ``no-data``, never fabricate."""
    ml = metric_layer or get_metric_layer()
    try:
        metric = ml.get_metric(metric_id)
    except KeyError as e:
        raise mr.MetricResolverError(f"unknown metric id: {metric_id!r}") from e

    covering = systems_covering(metric_id, query=query, metric_layer=ml,
                                systems=systems)
    if not covering:
        raise mr.MetricResolverError(
            f"no onboarded system covers metric {metric_id!r}")

    resolve = resolver if resolver is not None else mr.resolve_metric
    sites, values = _resolve_per_system(metric_id, covering, resolve)
    aggregation = metric.aggregation if isinstance(metric, DirectMetric) else None
    agg_value, method = _aggregate_or_raise(metric_id, aggregation, sites, values)
    sites.sort(key=lambda s: site_sort_key(s["site"], s["system_name"]))
    return {
        "aggregate": {"value": agg_value, "unit": metric.unit,
                      "label": metric.label, "method": method},
        "sites": sites,
        "contributing": len(values),
    }


def _site_entry(covering_entry: dict) -> dict:
    """Base keys of one ``sites`` element from a :func:`systems_covering` entry."""
    return {"site": covering_entry["site"],
            "system_id": covering_entry["system_id"],
            "system_name": covering_entry["name"]}
