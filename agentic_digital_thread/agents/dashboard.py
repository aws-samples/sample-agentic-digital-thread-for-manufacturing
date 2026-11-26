# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dashboard data endpoints and IoT feed for the Agentic Digital Thread sample."""
import json
import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from typing import Annotated
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from .catalog import catalog
from . import metric_resolver as mr
from . import metric_multisite
from . import mqtt_subscriber
from . import card_events
from . import connection_types
from . import coverage
from . import entity_render
from . import active_issues
from . import live_settings
from .card_layer import get_card_layer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])

_agent_manager = None

def set_agent_manager(mgr):
    global _agent_manager
    _agent_manager = mgr


def _resolved_card_metric_entry(
    rm, *, status: str, value, detail: str | None = None, percentiles=None
) -> dict:
    """Render one successful resolver result with conditional evidence keys."""
    entry = {
        "metric_id": rm.metric_id,
        "label": rm.label,
        "value": value,
        "unit": rm.unit,
        "system_id": rm.system_id,
        "status": status,
        "lineage": mr._lineage_to_dict(rm.lineage),
    }
    if percentiles is not None:  # a duration-percentile card entry
        entry["percentiles"] = [{"p": p, "value": v} for p, v in percentiles]
    if detail is not None:
        entry["detail"] = detail
    if rm.sample_count is not None:
        entry["sample_count"] = rm.sample_count
    return entry


def card_entry_from_resolved(rm, drill_sink: dict | None = None) -> dict:
    """ONE resolved metric → its card metric entry (the honest-data register).

    Declaration-withheld values remain distinct invariant violations; an
    unbound predicate concept (ADR-0075) is a distinct ``unresolved-predicate``
    a concept bound to 2+ columns with no steward choice (ADR-0076) a distinct
    ``ambiguous-binding``, and a preview input gap (ADR-0080 D3) a distinct
    ``unresolved-input`` — each NAMES what is missing, never no-data. Successful empty reads are
    no-data. Only an ``ok`` direct metric can stamp the optional
    physical-table drill target. Shared by the dashboard and the dry-run
    preview so the two surfaces cannot drift.
    """
    withheld = {
        "invariant_violation": "invariant-violation",
        mr.UNRESOLVED_PREDICATE_STATUS: "unresolved-predicate",
        mr.AMBIGUOUS_BINDING_STATUS: "ambiguous-binding",
        mr.UNRESOLVED_INPUT_STATUS: "unresolved-input",
    }
    if rm.status in withheld:
        return _resolved_card_metric_entry(
            rm, status=withheld[rm.status], value=None, detail=rm.detail,
        )
    percentiles = getattr(rm, "percentiles", None)
    if percentiles is not None:  # a duration-percentile result
        usable = [(p, v) for p, v in percentiles if v is not None]
        if not usable or rm.sample_count == 0:
            return _resolved_card_metric_entry(rm, status="no-data", value=None)
        return _resolved_card_metric_entry(
            rm, status="ok", value=None, percentiles=usable)
    if rm.value is None or rm.sample_count == 0:
        return _resolved_card_metric_entry(rm, status="no-data", value=None)
    if drill_sink is not None and not drill_sink and rm.query_plan is not None:
        drill_sink["system_id"] = rm.system_id
        drill_sink["table"] = rm.query_plan.table
    return _resolved_card_metric_entry(rm, status="ok", value=rm.value)


def unavailable_card_entry(ref: str, error: BaseException) -> dict:
    """The boundary-failure card metric entry (``unavailable`` + the reason)."""
    return {
        "metric_id": ref,
        "status": "unavailable",
        "value": None,
        "error": str(error),
    }


def _build_card_metric(ref: str, drill_sink: dict | None = None) -> dict:
    """Resolve one card metric without composing SQL.

    Boundary failures are unavailable; every resolved outcome renders through
    :func:`card_entry_from_resolved`.
    """
    try:
        rm = mr.resolve_metric(ref)
        return card_entry_from_resolved(rm, drill_sink)
    except (mr.MetricResolverError, NotImplementedError, KeyError) as e:
        logger.warning("Card metric %r: resolve_metric failed", ref, exc_info=True)
        return unavailable_card_entry(ref, e)


# Cards whose multi-site coverage gate already logged a warning this process
# (warn-ONCE per card: the gate runs on every TTL cards rebuild, and a repeating
# failure would otherwise spam a warning per rebuild).
_MULTI_SITE_WARNED: set[str] = set()


def _multi_site_covering(card) -> list[dict] | None:
    """The covering systems for a scalar card's PRIMARY metric ref — but ONLY
    when the fan-out applies: >1 onboarded system covers it (2A).

    Cheap gate first: with fewer than two onboarded systems there is nothing to
    fan out over, so NO graph read happens at all (a single-system deployment
    pays zero extra cost and its payload stays byte-identical). Fail-safe →
    ``None`` (the single-authority path) on ANY error: multi-site is an
    enrichment, never a reason a card fails to build — but the swallow is
    OBSERVABLE: a ``logger.warning`` fires once per card per process (fix-pass
    escalation from debug; a silently-degraded gate hid real coverage-read
    failures)."""
    if not card.metric_refs:
        return None
    try:
        onboarded = catalog.get_onboarded() or []
        if len(onboarded) < 2:
            return None
        covering = metric_multisite.systems_covering(
            card.metric_refs[0], systems=onboarded, readable_only=False)
    except Exception:  # nosec B110 - enrichment gate is best-effort; never break card build
        if card.id not in _MULTI_SITE_WARNED:
            _MULTI_SITE_WARNED.add(card.id)
            logger.warning(
                "multi-site coverage gate failed for card %r — serving the "
                "single-authority path (warn-once per process)",
                card.id, exc_info=True,
            )
        return None
    return covering if len(covering) > 1 else None


def _build_card_metric_multi(ref: str, attach_sites: bool) -> dict:
    """Resolve one ``metric_ref`` through the multi-site fan-out
    (``metric_multisite.resolve_metric_multi``, Task 1B) → a card metric entry.

    The entry mirrors the single-authority ``ok`` shape (same base key set) with
    ``system_id``/``lineage`` ``None`` (no SINGLE system/plan exists behind a
    cross-site aggregate). ``sites`` — the per-site breakdown
    ``[{site, system_id, system_name, value, status}]`` — is attached ONLY for
    the card's PRIMARY metric (``attach_sites``); sub-readings stay
    aggregate-level. An aggregate the combiner honestly cannot produce (LATEST
    across systems, or an all-no-data fan-out → ``value None``) is classified
    ``"no-data"`` and consumed DIRECTLY — no second resolve pass and no
    traceback. That state is EXPECTED; treating it as a failure would cost
    a full extra single-authority resolve per card per TTL rebuild plus an
    ``exc_info`` traceback each time (a measured contributor to single-task
    CPU saturation).

    FAIL-SOFT: if the fan-out raises, fall back to the single-authority
    ``_build_card_metric`` path — never a dead card because the enrichment
    failed. ``MetricResolverError`` is the fan-out's NAMED boundary class (its
    message carries the reason: unknown metric, no coverage, or every covering
    system errored) and logs CONCISELY at info; the code-defect classes keep
    their traceback."""
    try:
        multi = metric_multisite.resolve_metric_multi(ref)
    except mr.MetricResolverError as e:
        logger.info("multi-site fan-out unavailable for %r (%s); serving the "
                    "single-authority path", ref, e)
        return _build_card_metric(ref)
    except (NotImplementedError, KeyError):
        logger.warning("multi-site resolve failed for %r; falling back to "
                       "single-authority", ref, exc_info=True)
        return _build_card_metric(ref)
    return multi_card_entry(ref, multi, attach_sites)


#: Withheld site registers in the order the aggregate adopts one when no site
#: contributed: a declared invariant first (the value exists and is rejected),
#: then the two "a steward must choose" states.
_WITHHELD_ORDER = ("invariant-violation", "ambiguous-binding", "unresolved-predicate")

_WITHHELD_LEAD = {
    "invariant-violation": "declared invariant withheld the value on ",
    "ambiguous-binding": "the concept binds to several columns on ",
    "unresolved-predicate": "a filter concept has no bound field on ",
}


def _withheld_aggregate(value, sites: list[dict]) -> tuple[str, str] | None:
    """``(status, detail)`` when no site contributed and at least one withheld
    its value (the aggregate is WITHHELD, never relabelled no-data; ADR-0045 /
    0073 / 0076). The status is the first register in ``_WITHHELD_ORDER`` any
    site reported; the detail names every withholding system, each with its
    own reason when the site carried one. ``None`` otherwise."""
    if value is not None or any(s.get("status") == "ok" for s in sites):
        return None
    withheld = [s for s in sites if s.get("status") in _WITHHELD_LEAD]
    if not withheld:
        return None
    status = next(st for st in _WITHHELD_ORDER if any(s["status"] == st for s in withheld))
    detail = _WITHHELD_LEAD[status] + ", ".join(str(s.get("system_id")) for s in withheld)
    reasons = {s.get("detail") for s in withheld if s["status"] == status and s.get("detail")}
    if len(reasons) == 1:
        detail += f": {reasons.pop()}"
    return status, detail


def multi_card_entry(ref: str, multi: dict, attach_sites: bool) -> dict:
    """A ``resolve_metric_multi`` result → the card metric entry (shared with
    the dry-run preview so the fan-out rendering cannot drift)."""
    agg = multi["aggregate"]
    entry = {
        "metric_id": ref,
        "label": agg["label"],
        "value": agg["value"],
        "unit": agg["unit"],
        "system_id": None,
        "status": "ok" if agg["value"] is not None else "no-data",
        "lineage": None,
    }
    withheld = _withheld_aggregate(agg["value"], multi["sites"])
    if withheld is not None:
        entry["status"], entry["detail"] = withheld
    if attach_sites:
        entry["sites"] = [dict(s) for s in multi["sites"]]
    return entry


def _build_card(card_id: str, covered_set=None) -> dict:
    """Resolve a single card by id into its payload dict (the shared per-card
    resolution path — also reused by the re-derivation worker, S3.2/W2.2).

    Looks the card up via the card layer and resolves each ``metric_ref`` via
    ``_build_card_metric`` (ADR 0001). Returns the same
    ``{card_id,title,viz_type,group_by,metrics:[...]}`` shape as before.

    PROACTIVE coverage gating (S4.2, behind ``RESOLVER_COVERAGE``): when
    ``covered_set`` is provided (not ``None``) and the card is NOT satisfiable
    against the onboarded covered-concept set, short-circuit BEFORE any
    ``_build_card_metric``/``resolve_metric`` call and return a distinct
    ``uncovered`` payload (structural gap, ADR 0006) — never a fake value. When
    ``covered_set is None`` (flag off) the body is byte-identical to before, and
    a satisfiable card falls through to the existing reactive resolve path.
    """
    card = get_card_layer().get_card(card_id)
    if covered_set is not None:
        cc = coverage.card_coverage(card, covered_set)
        if not cc["satisfiable"]:
            missing = cc["missing_concepts"]
            return {
                "card_id": card.id,
                "title": card.title,
                "viz_type": card.viz_type.value,
                "group_by": card.group_by,
                "status": "uncovered",
                "missing_concepts": missing,
                "suggestion": coverage.suggestion_for(missing[0]) if missing else None,
                "suggestions": [coverage.suggestion_for(c) for c in missing],
                "metrics": [],
            }
    if card.group_by is not None:
        # By-dimension card (S5.3 §6.3): a DECLARATIVE descriptor resolved
        # client-side via POST /api/metrics/resolve-by-dimension — NEVER scalar-
        # resolved here (a single whole-table AGG would be wrong). The shared
        # per-card path (this fn, also the re-derivation worker) therefore skips
        # the scalar resolve cleanly and emits the descriptor (metrics=[]).
        return {
            "card_id": card.id,
            "title": card.title,
            "viz_type": card.viz_type.value,
            "group_by": card.group_by,
            "metric_refs": list(card.metric_refs),
            "resolver": "by-dimension",
            "metrics": [],
        }
    drill_sink: dict = {}
    # 2A multi-site fan-out: ONLY for scalar (kpi/gauge) cards whose PRIMARY
    # concept is covered by >1 onboarded system, and only on this shared
    # per-card path — which runs inside the TTL-cached ``_build_cards``/warmer
    # flow, never per-viewer. Query budget: ~(covering systems × metric_refs)
    # backend resolves per card per TTL rebuild (documented cost, ADR 0021-style
    # containment via the cache). Single-system deployments skip the gate with
    # zero graph reads and stay byte-identical.
    covering = _multi_site_covering(card)
    if covering is not None:
        metrics = [_build_card_metric_multi(ref, attach_sites=(i == 0))
                   for i, ref in enumerate(card.metric_refs)]
    else:
        metrics = [_build_card_metric(ref, drill_sink) for ref in card.metric_refs]
    payload = {
        "card_id": card.id,
        "title": card.title,
        "viz_type": card.viz_type.value,
        "group_by": card.group_by,
        "metrics": metrics,
        **_card_presentation(card),
    }
    # Wave 1 drill hint: present ONLY when a direct metric resolved to a physical
    # (system_id, table) — mirrors the conditional thresholds/target pattern in
    # ``_card_presentation`` so a card with no drillable source stays byte-identical.
    # The client matches this against the (system_id, table) key of an entity
    # section and opens it in a click-to-drill modal. ``entity_label`` carries the
    # card title for the modal header. No SQL composed — the table came from the
    # resolver's query_plan.
    if drill_sink.get("system_id") and drill_sink.get("table"):
        payload["drill"] = {
            "system_id": drill_sink["system_id"],
            "table": drill_sink["table"],
            "entity_label": card.title,
        }
    # S5.4 context (site/cell/system): auto-derive system + site from the resolving system,
    # then let any card-declared context override (and supply `cell`, which has no auto source).
    context = _system_context(drill_sink.get("system_id"))
    if card.context is not None:
        if card.context.system:
            context["system"] = card.context.system
        if card.context.site:
            context["site"] = card.context.site
        if card.context.cell:
            context["cell"] = card.context.cell
    if context:
        payload["context"] = context
    return payload


def _system_context(system_id: str | None) -> dict:
    """Auto-derive ``{system, site}`` from the onboarded system record (fail-safe → ``{}``).
    ``site`` follows the shared plant→site rule (``metric_multisite.site_of``:
    the wildcard ``all``/empty plant means no site)."""
    if not system_id:
        return {}
    try:
        for s in (catalog.get_all() or []):
            if s.get("id") == system_id:
                out: dict = {}
                if s.get("name"):
                    out["system"] = s["name"]
                site = metric_multisite.site_of(s)
                if site:
                    out["site"] = site
                return out
    except Exception:  # nosec B110 - context is best-effort; never break card build
        return {}
    return {}


def _card_presentation(card) -> dict:
    """Optional declarative presentation extras (``thresholds``/``target``) for
    the card payload, included ONLY when set — a card without them stays
    byte-identical to before this seam (the many exact-key-set snapshots hold).

    ``thresholds`` is rendered as an ordered list of ``{value, color}`` dicts
    (JSON-safe, no dataclass/enum leftovers); the frontend picks the band the
    metric value falls in and colors the value accordingly. ``target`` is a bare
    number rendered as a small 'target: N' reference (actual-vs-target)."""
    extra: dict = {}
    if card.thresholds is not None:
        extra["thresholds"] = [{"value": b.value, "color": b.color}
                               for b in card.thresholds]
    if card.target is not None:
        extra["target"] = card.target
    return extra


def _build_cards() -> list[dict]:
    """Declarative card payload (S2.3): iterate the card layer and resolve each
    SCALAR card via the shared per-card path ``_build_card`` (ADR 0001). Errors
    are caught per metric_ref so one bad ref yields an unavailable entry without
    killing its siblings. Generalizes the former flag-gated tracer card.

    By-dimension cards (``group_by`` set, e.g. the ``quality-by-line`` bar) are
    EXCLUDED from this scalar snapshot (S5.3 §6.3): they carry per-row grouped
    data resolved client-side via POST ``/api/metrics/resolve-by-dimension``, so
    scalar-resolving them on every 30s SSE snapshot would be both wrong (a single
    whole-table AGG) and wasteful. The snapshot therefore stays scalar-only —
    ``kpi`` + ``gauge`` (composite + sub-readings) cards.

    PROACTIVE coverage gating (S4.2): when ``RESOLVER_COVERAGE`` is set, compute
    the onboarded covered-concept set ONCE here (one graph read) and pass it to
    each ``_build_card`` so genuinely-uncovered cards short-circuit to the
    distinct ``uncovered`` payload. Flag off ⇒ ``covered_set=None`` ⇒ today's
    byte-identical fall-through path.
    """
    covered_set = (coverage.covered_concepts()
                   if os.environ.get('RESOLVER_COVERAGE', '').strip() else None)
    return [_build_card(card.id, covered_set)
            for card in get_card_layer().list_cards()
            if card.group_by is None]


# --- Cost control (S5.4): TTL cache for resolved card values ------------------
# Each scalar card resolve is a backend query — for an Athena-backed MES that is a
# billed data scan. Without a cache, ``dashboard_stream`` re-resolves EVERY card on
# EVERY 30s tick for EVERY open tab, which is both slow and expensive. Cache the built
# card list for a short TTL so repeated snapshots (and concurrent viewers) serve from
# memory instead of re-scanning. Cards are plant-independent (``_build_cards`` takes no
# plant), so one process-wide cache suffices; ``connected_types`` stays computed fresh
# (a cheap catalog read). Staleness is bounded by the TTL; the on-change ``card_update``
# ripple still updates individual cards live on the client. Thread-safe enough under the
# GIL — a rare race just resolves twice, never returns corrupt data.
_CARDS_CACHE: dict = {"ts": 0.0, "cards": None}


def _cards_ttl_seconds() -> float:
    """Card-value cache TTL (seconds). Override via ``DASHBOARD_CARDS_TTL``; 0 disables."""
    try:
        return float(os.environ.get("DASHBOARD_CARDS_TTL", "90"))
    except ValueError:
        return 90.0


def _snapshot_interval_seconds() -> float:
    """Seconds between periodic SSE dashboard snapshots.

    Delegates to :func:`live_settings.snapshot_interval_seconds` — the runtime
    admin override (if set) wins, then the ``DASHBOARD_SNAPSHOT_INTERVAL`` env
    value, then the 120s default (relaxed from the old 30s to cut the recurring
    per-viewer Athena scan cost)."""
    return live_settings.snapshot_interval_seconds()


def invalidate_cards_cache() -> None:
    """Drop the cached cards so the next snapshot re-resolves (e.g. after onboarding or a
    correction changes what the cards would resolve to)."""
    _CARDS_CACHE["cards"] = None
    _CARDS_CACHE["ts"] = 0.0


def _background_warming_active() -> bool:
    """True when a background warmer is ACTUALLY keeping the card cache fresh.

    The ``CARD_LIVE`` live-card poll (``card_live._poll_covered_cards``, every
    ``CARD_LIVE_POLL_SECONDS``) re-resolves the covered cards and — via the
    unified single-resolve path (``rederive._publish_batch`` →
    :func:`update_cards_cache`) — refreshes this cache on the SAME resolve that
    drives the SSE ripple. When that warmer is active, :func:`_cached_cards`
    serves a slightly-stale but PRESENT value rather than blocking a request on a
    full cold resolve (a cold ``_build_cards`` can be tens of seconds against an
    Athena-backed MES).

    BUT the poll no-ops while live updates are PAUSED
    (``live_settings.live_updates_enabled()`` is False — the runtime cost toggle):
    the warmer is wired but not running, so it is NOT keeping the cache fresh.
    Reporting "warming active" in that state would make :func:`_cached_cards`
    serve a possibly-bad startup-warmed value FOREVER (an expired entry is never
    re-resolved). So we require BOTH the ``CARD_LIVE`` flag AND live updates
    enabled — when paused, an on-demand ``GET /api/dashboard`` falls through to a
    fresh inline resolve on TTL expiry (bounded to real page loads; the recurring
    timer still serves cached while paused, so paused cost stays controlled).
    Flag off ⇒ unchanged inline-on-expiry TTL behavior."""
    return (bool(os.environ.get("CARD_LIVE", "").strip())
            and live_settings.live_updates_enabled())


def refresh_cards_cache() -> list[dict]:
    """FORCE-rebuild the TTL card cache and return the freshly-resolved cards.

    The background warmer entry point (startup warm + a fallback for callers that
    want a full refresh). FAIL-SOFT: never raises into a caller/scheduler — on any
    resolve error it logs and leaves the PRIOR cache intact (returning the prior
    cards, or ``[]`` if the cache was never warmed), so one bad resolve can neither
    crash startup nor blank a good cache. Respects the ``DASHBOARD_CARDS_TTL=0``
    opt-out (caching disabled ⇒ resolve fresh, do not populate the cache)."""
    if _cards_ttl_seconds() <= 0:
        try:
            return _build_cards()
        except Exception:
            logger.warning("refresh_cards_cache: rebuild failed (cache disabled)", exc_info=True)
            return []
    try:
        cards = _build_cards()
    except Exception:
        logger.warning("refresh_cards_cache: rebuild failed; leaving prior cache", exc_info=True)
        return _CARDS_CACHE.get("cards") or []
    _CARDS_CACHE["cards"] = cards
    _CARDS_CACHE["ts"] = time.monotonic()
    return cards


def update_cards_cache(cards: list[dict] | None) -> None:
    """Patch freshly-resolved card payloads into the TTL cache BY ``card_id`` and
    refresh the cache timestamp (fail-soft).

    The DRY, single-resolve seam: the live poll / re-derivation worker resolves a
    batch of cards ONCE (to drive the SSE ``card_update`` ripple) and feeds that
    SAME result here, so the ``/api/dashboard`` cache stays warm WITHOUT a second
    full ``_build_cards`` scan (no doubling of backend queries). Only entries
    ALREADY present in the cache are replaced (so the scalar-only cache never
    grows a by-dimension/uncovered card the poll happened to re-resolve). NO-OP
    when the cache has not been warmed yet (nothing to patch onto — a full
    :func:`refresh_cards_cache`/startup warm populates it) or when caching is
    disabled (``DASHBOARD_CARDS_TTL=0``)."""
    if _cards_ttl_seconds() <= 0:
        return
    try:
        existing = _CARDS_CACHE.get("cards")
        if existing is None:
            return  # not warmed yet — a full refresh/startup warm populates it
        by_id = {c.get("card_id"): c for c in (cards or [])
                 if isinstance(c, dict) and c.get("card_id")}
        if not by_id:
            return
        _CARDS_CACHE["cards"] = [by_id.get(c.get("card_id"), c) for c in existing]
        _CARDS_CACHE["ts"] = time.monotonic()
    except Exception:
        logger.warning("update_cards_cache: patch failed; leaving prior cache", exc_info=True)


def _cached_cards() -> list[dict]:
    """Resolved cards, served from a short TTL cache to avoid re-scanning the backend on
    every snapshot. TTL<=0 disables caching (always fresh).

    When a background warmer is active (:func:`_background_warming_active`), a
    PRESENT-but-expired cache is served as-is rather than blocking the request on a
    full cold resolve — the warmer's next tick refreshes it, and serving a
    slightly-stale value beats a multi-second hang (the SSE ripple keeps the client
    live in the meantime). Only the very first cold start (nothing cached yet)
    resolves inline. Flag off ⇒ byte-identical inline-on-expiry behavior."""
    ttl = _cards_ttl_seconds()
    if ttl <= 0:
        return _build_cards()
    now = time.monotonic()
    cache = _CARDS_CACHE
    cards = cache["cards"]
    if cards is not None and (now - cache["ts"]) < ttl:
        return cards
    if cards is not None and _background_warming_active():
        # Serve slightly-stale rather than block on a full cold resolve — the
        # background poll refreshes it on its next tick (never a cold hang).
        return cards
    cards = _build_cards()
    cache["cards"] = cards
    cache["ts"] = now
    return cards


def _matches_plant(system: dict, plant: str) -> bool:
    """Replicates the in-loop plant predicate so ``connected_types``, the lazy
    ``/api/dashboard/sections`` route, and the legacy per-system loop all filter
    onboarded systems identically. ``plant='all'`` (the default) matches every
    system; otherwise a system matches if its own plant is the wildcard or equals
    the requested plant (case-insensitive)."""
    if plant == 'all':
        return True
    sys_plant = (system.get('plant') or 'all').lower()
    return sys_plant == 'all' or sys_plant == plant.lower()


def _onboarded_for_plant(plant: str = 'all') -> list[dict]:
    """Onboarded systems honoring the ``plant`` query-filter convention."""
    return [s for s in catalog.get_onboarded() if _matches_plant(s, plant)]


# Canonical pin shape lives in ``metric_resolver.PinnedAuthority`` (fix-pass:
# the former local class here collapsed into that ONE class). The private name
# is kept as an alias because it is this module's sanctioned sharing seam —
# ``dashboard_drill`` imports it (used by the Line State fan-outs to query EACH
# covering system in turn, bypassing single-authority-per-concept). Generic —
# carries no concept/system literals; the caller supplies the system id.
_PinnedAuthority = mr.PinnedAuthority


# --- Panel-read cache: bounded TTL + single-flight ---------------------------
# The expensive dashboard PANEL reads (the line-state fan-out sweep, the entity
# sections, the active-issues composition, and the grouped by-dimension/heatmap
# resolves) were each recomputed PER REQUEST. One browser wave — or a handful of
# viewers — therefore multiplied the same backend scans, which is what pinned a
# single app task at ~100% CPU on 2026-08-24.
#
# This is the ONE home for that algorithm: a short TTL plus single-flight so a
# convoy of identical reads collapses to one compute. Three properties are
# deliberate and must not be copied away:
#   • BOUNDED. Completed entries live in an LRU capped at _READ_CACHE_MAX, and
#     the in-flight registry holds ONE entry per key with a LIVE compute and
#     retires it on every outcome — so coordination memory is bounded by
#     CONCURRENCY, never by plant/metric/dimension cardinality (the original
#     per-key lock dict grew without limit).
#   • ERRORS ARE NEVER CACHED, BUT THEY ARE STILL SINGLE-FLIGHT. A raising
#     compute propagates and leaves nothing behind, and the friendly HTTP-200
#     ``status='error'`` envelopes these routes return are served but not stored
#     (see _cacheable_payload) — otherwise a transient backend fault would be
#     pinned in front of every viewer for a whole window. Callers already joined
#     to a doomed attempt SHARE its outcome instead of each re-running the same
#     doomed sweep: the failing convoy is the one that most needs collapsing.
#     Retiring the flight as it settles is what keeps the NEXT request re-probing.
#   • AN INVALIDATION IS NEVER UNDONE. Dropping the entries alone is not enough:
#     a caller that started computing BEFORE a steward correction still holds a
#     PRE-correction answer, and storing it afterwards resurrects exactly the
#     state that was just corrected. Admission is therefore gated on a cache
#     GENERATION captured before the compute starts, and a flight from an older
#     generation is never joined.
# Callers run in worker threads via the EVENT-LOOP OFFLOAD pattern
# (metric_resolver.py), so thread primitives are the right choice here.
_READ_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()  # key -> {"ts", "value"}
_READ_CACHE_MAX = 128
_READ_META_LOCK = threading.Lock()
# key -> _ReadFlight, guarded by _READ_META_LOCK. Empty whenever nothing is
# computing; see _claim_read_flight / _retire_read_flight.
_READ_FLIGHTS: dict = {}
# Monotonic counter bumped by every invalidation — the admission fence.
_READ_META = {"generation": 0}
# The honest fault statuses a fan-out/composition can report. Neither is cached:
# ``error`` means nothing usable was produced, ``partial-error`` means part of
# the answer is missing because a source failed UNEXPECTEDLY (distinct from a
# mapping coverage gap, which is an honest ``ok``/``uncovered`` and IS cached).
_UNCACHEABLE_STATUSES = frozenset({"error", "partial-error"})


def _panel_read_ttl_seconds() -> float:
    """Panel-read cache TTL (seconds). Override via ``DASHBOARD_PANEL_READ_TTL``;
    0 disables (always fresh) and is the rollback lever for this cache."""
    try:
        return float(os.environ.get("DASHBOARD_PANEL_READ_TTL", "60"))
    except ValueError:
        return 60.0


def _line_state_ttl_seconds() -> float:
    """Line-state sweep cache TTL (seconds). Override via
    ``DASHBOARD_LINE_STATE_TTL``; 0 disables (always fresh)."""
    try:
        return float(os.environ.get("DASHBOARD_LINE_STATE_TTL", "20"))
    except ValueError:
        return 20.0


def _cacheable_payload(value) -> bool:
    """False for the friendly ``status='error'``/``'partial-error'`` envelopes
    (top level, or any nested section) these routes return instead of raising.
    Honest stable outcomes — ``ok`` (incl. empty), ``uncovered`` and ``no-data``
    — ARE cached: they are just as expensive to recompute and, per the
    honest-data rule, ``no-data`` is a real answer rather than a fault.
    Non-dict values (the generic helper is used by tests and by simple sweeps)
    are cacheable."""
    if not isinstance(value, dict):
        return True
    if value.get("status") in _UNCACHEABLE_STATUSES:
        return False
    sections = value.get("sections")
    return not (isinstance(sections, list) and any(
        isinstance(s, dict) and s.get("status") in _UNCACHEABLE_STATUSES
        for s in sections))


def invalidate_dashboard_read_cache() -> None:
    """Drop every cached panel read AND fence the in-flight ones.

    Bumping the generation is what stops a compute that began BEFORE this call
    from storing its pre-invalidation answer afterwards (production paths use
    :func:`invalidate_dashboard_caches`; TTL handles ordinary ageing)."""
    with _READ_META_LOCK:
        _READ_CACHE.clear()
        _READ_META["generation"] += 1


def invalidate_line_state_cache() -> None:
    """Compatibility/test seam — the line-state sweeps live in the shared cache."""
    invalidate_dashboard_read_cache()


def invalidate_dashboard_caches() -> None:
    """Drop BOTH dashboard caches — the ONE invalidator the production paths
    call. A provisioning change or a mapping correction changes what the cards
    AND the panel reads would resolve to, so neither may outlive it."""
    invalidate_cards_cache()
    invalidate_dashboard_read_cache()


def _read_cache_hit(key: tuple, ttl: float):
    """Return the live entry for ``key`` (refreshing its LRU position) or None."""
    with _READ_META_LOCK:
        entry = _READ_CACHE.get(key)
        if entry is None or (time.monotonic() - entry["ts"]) >= ttl:
            return None
        _READ_CACHE.move_to_end(key)
        return entry


def _read_cache_store(key: tuple, value, generation: int) -> None:
    """Store ``value`` as the newest entry, evicting least-recently-used keys.

    A no-op when an invalidation landed while ``compute`` was running: the value
    predates the correction and must not be resurrected."""
    with _READ_META_LOCK:
        if generation != _READ_META["generation"]:
            return
        _READ_CACHE[key] = {"ts": time.monotonic(), "value": value}
        _READ_CACHE.move_to_end(key)
        while len(_READ_CACHE) > _READ_CACHE_MAX:
            _READ_CACHE.popitem(last=False)


class _ReadFlight:
    """One in-flight compute for a key, tagged with the generation it began in.

    Its outcome — value OR exception — is reproduced for every caller that
    joined it, so a failing sweep is attempted once rather than once per viewer.
    ``owner`` lets a re-entrant read on the LEADER'S OWN thread compute instead
    of waiting on itself (a hung request is worse than a duplicate compute)."""

    __slots__ = ("generation", "owner", "done", "value", "error")

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.owner = threading.get_ident()
        self.done = threading.Event()
        self.value = None
        self.error: BaseException | None = None

    def settle(self, value, error: BaseException | None) -> None:
        self.value, self.error = value, error
        self.done.set()

    def outcome(self):
        """Block for the leader's outcome and reproduce it. A shared FAILURE is
        re-raised — never swallowed into a fallback and never cached."""
        self.done.wait()
        if self.error is not None:
            raise self.error
        return self.value


def _claim_read_flight(key: tuple):
    """Return ``(flight, is_leader)`` for ``key``.

    Joins the CURRENT-generation flight when one exists and was started by
    another thread; otherwise registers a new flight and leads it. A flight from
    an older generation is replaced, never joined: its answer predates an
    invalidation."""
    with _READ_META_LOCK:
        generation = _READ_META["generation"]
        flight = _READ_FLIGHTS.get(key)
        if (flight is not None and flight.generation == generation
                and flight.owner != threading.get_ident()):
            return flight, False
        flight = _ReadFlight(generation)
        _READ_FLIGHTS[key] = flight
        return flight, True


def _retire_read_flight(key: tuple, flight: _ReadFlight) -> None:
    """Remove ``flight`` — and ONLY it, so a post-invalidation replacement flight
    survives its predecessor's cleanup and keeps its own joiners."""
    with _READ_META_LOCK:
        if _READ_FLIGHTS.get(key) is flight:
            del _READ_FLIGHTS[key]


def _lead_read_flight(key: tuple, flight: _ReadFlight, compute, args):
    """Compute once on behalf of every caller joined to ``flight``.

    Admission is fenced on the generation captured when the flight was claimed;
    the flight is retired before it settles so the NEXT request re-probes rather
    than joining a finished attempt."""
    value = None
    error: BaseException | None = None
    try:
        value = compute(*args)
        if _cacheable_payload(value):
            _read_cache_store(key, value, flight.generation)
        return value
    except BaseException as exc:                    # noqa: BLE001 — re-raised below
        error = exc
        raise
    finally:
        _retire_read_flight(key, flight)
        flight.settle(value, error)


def dashboard_read_cached(key: tuple, compute, *args, ttl_seconds: float | None = None):
    """Serve ``compute(*args)`` from the shared bounded panel-read cache.

    ``key`` identifies the read (e.g. ``("sections", plant)``) and MUST carry
    every request discriminator, so distinct plants/metrics/dimensions/authority
    pins can never be served each other's answer. Errors are never cached but
    ARE shared with the callers already in flight, and an answer computed before
    an invalidation is served to its own caller but never admitted."""
    ttl = _panel_read_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        return compute(*args)
    entry = _read_cache_hit(key, ttl)
    if entry is not None:
        return entry["value"]
    flight, is_leader = _claim_read_flight(key)
    if not is_leader:
        return flight.outcome()
    return _lead_read_flight(key, flight, compute, args)


def line_state_cached(key: tuple, compute, *args):
    """Line-state sweeps through the shared cache, keeping their OWN shorter TTL
    (machine state moves faster than the mapping-driven panels)."""
    return dashboard_read_cached(key, compute, *args,
                                 ttl_seconds=_line_state_ttl_seconds())


def fanout_status(resolved: int, failed: int) -> tuple[str, str | None]:
    """Honest ``(status, error)`` for a per-system fan-out that skipped systems.

    ``failed`` counts UNEXPECTED per-system errors — a real source fault. It
    NEVER counts the expected coverage-gap skip (a system that simply does not
    map the concept), which stays an honest ``ok`` with ``error=None`` so the
    mid-onboarding empty state keeps its sharing.

    Nothing resolved and something broke ⇒ ``error`` — distinct from "no system
    maps the concept" (``ok`` + ``sites: []``), which the cache would otherwise
    pin in front of every viewer for a whole window.
    Some systems resolved ⇒ ``partial-error``. Both fault statuses are refused
    by :func:`_cacheable_payload`, so the next request re-probes."""
    if failed <= 0:
        return "ok", None
    plural = "s" if failed != 1 else ""
    if resolved > 0:
        return "partial-error", (
            f"Partial data — {failed} source system{plural} reported errors.")
    return "error", (
        f"{failed} source system{plural} reported errors; no data was read.")


def fetch_dashboard_data(plant='all') -> dict:
    """Cheap dashboard snapshot (S5.3 / ADR 0021): honest ``connected_types``
    plus the declarative, mapping-driven ``cards`` array — and NOTHING else.

    Entity sections are served lazily by ``/api/dashboard/sections``
    (loaded after first paint), so they never run on the 30s SSE snapshot.

    ``connected_types`` is derived ONLY from the actually-onboarded systems
    (respecting the plant filter); zero onboarded ⇒ ``[]`` and a truthful
    frontend empty-state. ``cards`` is built unconditionally via ``_build_cards``
    (mapping-driven, ADR 0001) — ``RESOLVER_COVERAGE`` still gates the proactive
    ``uncovered`` state inside ``_build_cards``."""
    return {
        'connected_types': sorted({s['system_type'] for s in _onboarded_for_plant(plant)}),
        'cards': _cached_cards(),
    }


def _snapshot_dashboard_data(plant='all') -> dict:
    """Timer-driven SSE snapshot payload — the recurring ``event: dashboard`` push.

    Runtime pause gate (cost control): when live updates are PAUSED
    (:func:`live_settings.live_updates_enabled` is False) this serves the LAST
    cached cards WITHOUT triggering a fresh ``_build_cards`` resolve, so the
    recurring per-viewer timer NEVER re-scans the Athena/SiteWise backend while
    paused. ``connected_types`` (a cheap catalog read, no backend query) stays
    truthful. When live updates are ON this is byte-identical to
    :func:`fetch_dashboard_data` (unchanged cadence + TTL-cache behavior).

    This gates ONLY the recurring TIMER snapshot — an on-demand ``GET
    /api/dashboard`` (a user loading the page) still goes through
    :func:`fetch_dashboard_data` and is allowed to resolve (served from the warm
    TTL cache when available)."""
    if not live_settings.live_updates_enabled():
        return {
            'connected_types': sorted(
                {s['system_type'] for s in _onboarded_for_plant(plant)}
            ),
            'cards': _CARDS_CACHE.get('cards') or [],
        }
    return fetch_dashboard_data(plant=plant)


@router.get("/dashboard")
async def dashboard_data():
    """Return all dashboard data grouped by plant.

    EVENT-LOOP OFFLOAD (pattern note in ``metric_resolver``): a cold/expired
    card cache makes ``fetch_dashboard_data`` run the full resolver sweep (sync
    boto3/DB) — run it in a worker thread, never inline on the event loop."""
    return await mr.offload_resolver(fetch_dashboard_data)


def _sections_payload(plant: str, system_id: str | None,
                      table: str | None) -> dict:
    """Sync sections body; optional target fields are an all-or-nothing pair."""
    systems = _onboarded_for_plant(plant)
    if system_id is None and table is None:
        return {"sections": entity_render.render_all_sections(systems)}
    if not system_id or not table:
        return {"sections": []}
    system = next(
        (candidate for candidate in systems if candidate.get("id") == system_id),
        None,
    )
    if system is None or not connection_types.section_read_supported(system):
        return {"sections": []}
    sections = entity_render._render_system_sections_safe(system, table=table)
    return {"sections": sections}


def _cached_sections_payload(plant: str, system_id: str | None = None,
                             table: str | None = None) -> dict:
    """Shared body keyed by every verbatim request discriminator."""
    key = ("sections", plant, system_id, table)
    return dashboard_read_cached(
        key, _sections_payload, plant, system_id, table)


@router.get("/dashboard/sections")
async def dashboard_sections(
    plant: Annotated[str, Query(max_length=128)] = 'all',
    system_id: Annotated[str | None, Query(max_length=128)] = None,
    table: Annotated[str | None, Query(max_length=128)] = None,
):
    """Return full sections for compatibility or one exact targeted drill.

    Target fields are used together. Unknown, plant-mismatched, partial, or
    unmapped targets return HTTP 200 with an empty list and never query a
    sibling system/table. Per-section source failures remain honest error
    envelopes. Sync graph/source reads stay off the event loop and identical
    requests share the bounded, no-error-caching read cache.
    """
    return await mr.offload_resolver(
        _cached_sections_payload, plant, system_id, table)


def _active_issues_payload(plant: str) -> dict:
    """Sync body of ``GET /dashboard/active-issues`` (worker thread — offload
    pattern). The composer is fail-loud; any DB/mapping boundary error is wrapped
    here into the friendly ``status='error'`` envelope (empty issues) and logged
    server-side — NEVER a raw 500 (mirrors ``metric_resolver._grouped_error``)."""
    try:
        issues = active_issues.compose_active_issues(_onboarded_for_plant(plant))
    except Exception as e:
        # Traceback goes INTO the message: the orchestrator runtime's OTel
        # handler discards exc_info/tracebacks, which made this
        # exact failure invisible for hours on a live env.
        import traceback

        logger.warning(
            "active-issues composition failed: "
            + "".join(traceback.format_exception(e)).replace("\n", " | ")
        )
        return {"kind": "active-issues", "issues": [], "status": "error",
                "error": "Active issues are temporarily unavailable."}
    return {"kind": "active-issues", "issues": issues, "status": "ok"}


def _cached_active_issues_payload(plant: str) -> dict:
    """Shared body of ``GET /dashboard/active-issues`` — same verbatim-plant key
    rule as the sections read; the ``status='error'`` envelope is never cached,
    so a transient source failure recovers on the next request."""
    return dashboard_read_cached(
        ("active-issues", plant), _active_issues_payload, plant)


@router.get("/dashboard/active-issues")
async def dashboard_active_issues(plant: str = 'all'):
    """Lazy, mapping-driven cross-entity active-issues list (S5.3 §6.2).

    Loaded by the frontend AFTER first paint (fire-and-forget), like the sections and
    quality-by-line panels — deliberately NOT part of ``fetch_dashboard_data`` / the 30s SSE
    snapshot, so the LIMIT-ed filtered per-source queries do not run on every snapshot. Auth
    is enforced by ``AuthMiddleware`` like every other ``/api/*`` route.

    Returns a friendly HTTP-200 envelope ``{kind:'active-issues', issues:[...],
    status:'ok'|'error', error?}``.

    EVENT-LOOP OFFLOAD: the composer runs sync per-source queries — offloaded.
    SHARED READ: identical concurrent/repeat requests collapse to one
    composition via the bounded panel-read cache (errors are never cached)."""
    return await mr.offload_resolver(_cached_active_issues_payload, plant)


def _line_state_site(system: dict, grm) -> dict:
    """One site entry for the Line State fan-out. RAW state strings are carried
    through (the frontend buckets them); ``site`` uses the shared plant→site
    rule so the card and the multi-site breakdown agree."""
    sid = system.get('id')
    return {
        "site": metric_multisite.site_of(system),
        "system_id": sid,
        "system_type": system.get('system_type') or '',
        "name": system.get('name') or sid,
        "states": [{"state": str(r.dimension_value), "count": r.value}
                   for r in grm.rows],
    }


def _line_state_payload(plant: str) -> dict:
    """Sync body of ``GET /dashboard/line-state`` — the full fan-out sweep
    (worker thread — offload pattern; cached via ``line_state_cached``).

    Per-system fail-soft, but NOT silently: an UNEXPECTED per-system error is
    counted so the envelope can report it honestly (``fanout_status``) and the
    result stays out of the cache. An expected coverage gap is still a plain
    skip."""
    from . import metric_dimension as md
    sites: list[dict] = []
    contributing_types: set[str] = set()
    resolved = failed = 0
    for system in _onboarded_for_plant(plant):
        sid = system.get('id')
        if not sid:
            continue
        try:
            grm = md.resolve_metric_by_dimension(
                'machine-state-distribution', 'machine-state',
                authority=_PinnedAuthority(sid),
            )
        except (mr.MetricResolverError, NotImplementedError, KeyError):
            # System does not map machine-state (or the concept is uncovered here)
            # → skip. This is the generic contributor filter (no system_type test).
            continue
        except Exception:  # nosec B112 - per-system boundary: count + skip, never 500
            failed += 1
            logger.warning("line-state resolve failed for system %r", sid, exc_info=True)
            continue
        resolved += 1
        if not grm.rows:
            continue
        entry = _line_state_site(system, grm)
        if entry["system_type"]:
            contributing_types.add(entry["system_type"])
        sites.append(entry)
    sites.sort(key=lambda s: (s["site"] or s["name"] or "").lower())
    status, error = fanout_status(resolved, failed)
    return {
        "status": status,
        "source_system_types": sorted(contributing_types),
        "sites": sites,
        "error": error,
    }


@router.get("/dashboard/line-state")
async def dashboard_line_state(plant: str = 'all'):
    """Aggregate the CURRENT machine-state across ALL onboarded systems that map
    the ``machine-state`` concept — the Line State panel's cross-site view.

    Fans out over every onboarded system (honoring the ``plant`` filter) and, for
    EACH, pins authority to that system (``_PinnedAuthority``) so the whole concept
    is not collapsed to a single plant. For each system it resolves the current
    machine-state DISTRIBUTION grouped by ``machine-state`` (the working
    metric-by-dimension path; ``machine_state`` is one row per line, so the per-state
    COUNT equals the number of lines in each state). A system whose ``machine-state``
    mapping is absent simply raises and is SKIPPED — that is what keeps this generic:
    only systems that actually map the concept contribute (no system_type literals).

    FAIL-SOFT: always HTTP 200 with the envelope; per-system boundary errors are
    caught and that system is skipped (mirrors ``dashboard_active_issues``). An
    UNEXPECTED per-system error is REPORTED, not hidden: ``status`` becomes
    ``partial-error`` (some systems contributed) or ``error`` (none did), with a
    message in ``error`` — and neither is cached, so the next request re-probes
    instead of serving a fabricated empty panel for a whole TTL window. A system
    whose mapping is simply absent is an expected coverage gap and keeps
    ``status: "ok"`` with ``error: None``. Loaded
    on demand by the frontend (after first paint), NOT part of the SSE snapshot, so
    it adds no recurring per-viewer snapshot cost.

    Envelope::

        {"status": "ok" | "partial-error" | "error",
         "source_system_types": ["MES", ...],   # sorted distinct contributing types → badge
         "sites": [{"site": <plant|"">, "system_id": ..., "system_type": ...,
                    "name": ..., "states": [{"state": <raw>, "count": <n>}, ...]}],
         "error": None | <message>}

    RAW state strings are returned; the frontend buckets them (single-sourced JS
    ``_bucketMachineState``) and computes Running/Idle/Down totals client-side.

    EVENT-LOOP OFFLOAD + TTL CACHE: the sweep runs in a worker thread via
    ``line_state_cached`` (~20s TTL, single-flight) so the panel/drill/reloads
    share ONE sweep per window instead of stampeding the resolver."""
    return await mr.offload_resolver(
        line_state_cached, ("line-state", plant), _line_state_payload, plant)


@router.get("/dashboard/stream")
async def dashboard_stream(plant: str = 'all'):
    """SSE endpoint — streams dashboard snapshots and live MQTT events."""
    def generator():
        q = mqtt_subscriber.subscribe()
        # S3.2/FLAG (d): subscribe to / drain the card broker when EITHER the
        # corrections loop (RESOLVER_REDERIVE) OR the live-data drivers (CARD_LIVE)
        # are on. Both publish the SAME event:card_update through card_events, so
        # one drain serves both. Flag-off on BOTH ⇒ cq stays None, no card_update
        # is ever emitted, and the SSE byte-stream is byte-identical to today
        # (hard invariant).
        cq = (
            card_events.subscribe()
            if (os.environ.get('RESOLVER_REDERIVE', '').strip()
                or os.environ.get('CARD_LIVE', '').strip())
            else None
        )
        try:
            yield f"event: dashboard\ndata: {json.dumps(fetch_dashboard_data(plant=plant))}\n\n"
            last_dashboard = time.time()
            last_stats = time.time()
            snapshot_interval = _snapshot_interval_seconds()
            while True:
                now = time.time()
                if now - last_dashboard >= snapshot_interval:
                    yield f"event: dashboard\ndata: {json.dumps(_snapshot_dashboard_data(plant=plant))}\n\n"
                    last_dashboard = now
                if now - last_stats >= 10:
                    yield f"event: stats\ndata: {json.dumps(mqtt_subscriber.get_stats())}\n\n"
                    last_stats = now
                try:
                    entry = q.get(timeout=1)
                    yield f"event: mqtt\ndata: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    pass
                # Non-blocking drain of the corrections broker (flag-gated).
                if cq is not None:
                    try:
                        while True:
                            ce = cq.get_nowait()
                            yield f"event: card_update\ndata: {json.dumps(ce)}\n\n"
                    except queue.Empty:
                        pass
        finally:
            mqtt_subscriber.unsubscribe(q)
            if cq is not None:
                card_events.unsubscribe(cq)

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.get("/systems")
async def systems():
    """Return the list of manufacturing systems with dynamic state from SYSTEM_CATALOG."""
    result = []
    for s in catalog.get_all():
        entry = {
            'id': s['id'],
            'name': s['name'],
            'plant': s['plant'],
            'type': s['system_type'],
            'connection_type': s['connection_type'],
            'status': 'connected' if s['status'] == 'onboarded' else 'registered',
        }
        if s['status'] == 'onboarded':
            inst = _agent_manager._agents.get(s['id'])
            if inst is None:
                entry['healthy'] = False
            elif inst.server is not None:
                # Local mode: check the agent's server is still running
                entry['healthy'] = inst.server.is_running()
            else:
                # Cloud mode (AgentCore runtime or MCP Server): healthy if registered in agent manager
                # process is None for cloud agents — use runtime_arn or mcp_url as health indicator
                entry['healthy'] = bool(inst.mcp_url)
        result.append(entry)
    return {'systems': result}


@router.get("/iot/stats")
async def iot_stats():
    """Return MQTT subscriber statistics."""
    return mqtt_subscriber.get_stats()


@router.get("/iot/topics")
async def iot_topics():
    """Return hierarchical MQTT topic tree with message counts."""
    return mqtt_subscriber.get_topic_tree()


@router.get("/iot/recent")
async def iot_recent(limit: int = 20):
    """Return recent MQTT messages for the live feed."""
    return {"messages": mqtt_subscriber.get_recent_messages(limit=limit)}
