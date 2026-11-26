# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 — a question naming a DEFINED metric carries the metric layer's
result BEFORE the model runs.

Two halves, one module:

* **App side** (:func:`results_for_turn`, :func:`extra_payload_with_results`):
  the served metrics a question names (``metric_term_resolution.
  defined_metric_terms``) resolve through the ONE computation home,
  ``metric_resolver.resolve_metric``, inside an attributed execution trace
  (``:metric-turn``) — bounded by a cap, a TTL cache, a hard timebox and a
  2-thread pool. The outcome is classified in the dashboard's honest-data
  register (never a new one) and rides the runtime payload as STRUCTURED data
  under the RESERVED key :data:`METRIC_LAYER_RESULTS_KEY`.
* **Runtime side** (:func:`sanitize_results`, :func:`render_metric_layer_notice`):
  the orchestrator runtime has no source connections, so it never resolves; it
  re-sanitizes what arrived and renders the notice text itself — no caller can
  put prose into the model's token path.

Failure posture (D3): the MATCHER fails open to no notice (we do not know a
defined metric was named); every RESOLUTION failure becomes a
``result_unavailable`` block that still forbids substitution — silence would
reproduce the fabricated-value defect. Nothing here is LLM-judged. Import-pure:
stdlib + ``metric_term_resolution``; resolver, trace and layer load lazily.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import metric_term_resolution as mtr
from .metric_turn_lineage import definition_lineage as _definition_lineage
from .metric_turn_lineage import grouped_lineage as _grouped_lineage
from .metric_turn_lineage import lineage_summary as _lineage_summary
from .metric_turn_render import (  # noqa: F401 - the ONE public surface
    INPUT_OVERFLOW_LINE, MAX_DETAIL_CHARS, MAX_LIST_ITEMS, MAX_NOTICE_CHARS, METRIC_LAYER_HEADER,
    METRIC_LAYER_RESULTS_KEY, NO_DATA_DETAIL, NOT_AVAILABLE_LINE, NOT_IN_TIME_OUTCOME, PERMITTED_LINE,
    ONE_QUERY_COMPUTATION,
    REQUIRED_RESPONSE, WINDOW_SCOPE_SENTENCE, WITHHELD_SCOPE_LINE, _bounded_detail, _field,
    input_overflow_result,
    render_metric_layer_notice, render_or_fallback, sanitize_result,
    sanitize_results, without_reserved_results,
)

logger = logging.getLogger(__name__)

METRIC_TURN_MAX_WORKERS = 2
# the bounded number of grouped rows a turn result carries.
MAX_GROUPED_ENTRIES = 50
FAILURE_COOLDOWN_SECONDS = 30.0
_DEFAULT_TIMEBOX_SECONDS = 12.0
_DEFAULT_TTL_SECONDS = 180.0

_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

#: A derived metric that DECLARES a window: the resolver applies no window to a
#: formula (``DerivedMetric.window`` is a resolver-deferred seam), so a value
#: would be reported for a period it does not cover — withheld instead.
DERIVED_WINDOW_DETAIL = (
    "derived metric declares window '{token}', but the metric layer does not "
    "apply a window to a derived formula; no value is reported for that window")

_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None
_GENERATION = 0                  # bumped by invalidate(); guards stale stores
_clock = time.monotonic          # monkeypatch point (fake clock in tests)


# --------------------------------------------------------------------------- #
# Configuration (finite, positive, fail-loud — the metric_definition_store rule)
# --------------------------------------------------------------------------- #
def _positive_finite_env(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of seconds, got {raw!r}")
    if not value > 0:
        raise ValueError(f"{name} must be positive")
    return value


def _timebox_seconds() -> float:
    return _positive_finite_env("METRIC_TURN_TIMEBOX_SECONDS", _DEFAULT_TIMEBOX_SECONDS)


def _cache_ttl_seconds() -> float:
    return _positive_finite_env("METRIC_TURN_CACHE_TTL_SECONDS", _DEFAULT_TTL_SECONDS)


def enabled() -> bool:
    """The operational kill switch ``METRIC_TURN_NOTICE`` (default on)."""
    return os.environ.get("METRIC_TURN_NOTICE", "on").strip().lower() not in _OFF_VALUES


# --------------------------------------------------------------------------- #
# Classification — the dashboard's register, never a new one
# --------------------------------------------------------------------------- #
def _classify(resolved) -> dict:
    from . import metric_resolver as mr
    withhold = {"invariant_violation", mr.UNRESOLVED_PREDICATE_STATUS,
                mr.AMBIGUOUS_BINDING_STATUS, mr.UNRESOLVED_INPUT_STATUS}
    if resolved.status in withhold:
        return {"outcome": "withheld", "status": resolved.status,
                "detail": _bounded_detail(resolved.detail, resolved.status)}
    percentiles = getattr(resolved, "percentiles", None)
    if percentiles is not None:  # a duration-percentile computed result
        return _classify_percentiles(resolved, percentiles)
    value = resolved.value
    if (value is None or resolved.sample_count == 0 or isinstance(value, bool)
            or not math.isfinite(float(value))):
        return {"outcome": "withheld", "status": "no-data",
                "detail": _bounded_detail(resolved.detail, NO_DATA_DETAIL)}
    result = {"outcome": "computed", "value": value, "unit": resolved.unit,
              "system_id": resolved.system_id, **_lineage_summary(resolved),
              **_applied_range(resolved)}
    if resolved.sample_count is not None:
        result["sample_count"] = resolved.sample_count
    return result


def _applied_range(resolved) -> dict:
    """``{"window_applied_range": {start, end}}`` for a read bounded by an
    explicit turn window (``QueryPlan.absolute_window``), the same field a
    grouped result carries; ``{}`` otherwise."""
    window = getattr(getattr(resolved, "query_plan", None), "absolute_window", None)
    if window is None:
        return {}
    return {"window_applied_range": {"start": window.start_utc, "end": window.end_utc}}


def _classify_percentiles(resolved, percentiles) -> dict:
    """A duration-PERCENTILE computed block: the (p, value) pairs plus
    the sample count. No usable pair, or zero samples ⇒ ``no-data``. The
    highest percentile's value is carried as the representative ``value`` so
    the existing single-value render/sanitize path works; the full list rides
    ``percentiles``."""
    usable = [(p, v) for (p, v) in percentiles
              if isinstance(v, (int, float)) and not isinstance(v, bool)
              and math.isfinite(float(v))]
    if not usable or resolved.sample_count == 0:
        return {"outcome": "withheld", "status": "no-data",
                "detail": _bounded_detail(resolved.detail, NO_DATA_DETAIL)}
    headline = max(usable, key=lambda pv: pv[0])[1]
    result = {"outcome": "computed", "value": headline, "unit": resolved.unit,
              "system_id": resolved.system_id,
              "percentiles": [{"p": p, "value": v} for p, v in usable],
              **_lineage_summary(resolved), **_applied_range(resolved)}
    if getattr(getattr(resolved, "lineage", None), "approximate", None):
        result["approximate"] = True  # Athena approx_percentile
    if resolved.sample_count is not None:
        result["sample_count"] = resolved.sample_count
    return result


def not_in_time_result(system_id: str, lineage: str) -> dict:
    """A value the metric layer did not compute within its time budget, that
    the system agent can compute (``metric_resolver.ExactValueNotInTime``)."""
    return {"outcome": NOT_IN_TIME_OUTCOME, "system_id": _field(system_id),
            "lineage": _field(lineage, MAX_DETAIL_CHARS)}


def _classify_error(exc: BaseException, metric_id: str | None = None) -> dict:
    """Resolver boundary errors are the dashboard's ``unavailable`` WITH the
    verbatim reason; anything else (connection, SDK) names only its class —
    its message may quote hosts or row values. A value whose exact
    computation did not fit the time budget is its own outcome; one refused
    inside an explicit turn window carries ``metric_id``'s definition."""
    from . import metric_resolver as mr
    if isinstance(exc, mr.ExactValueNotInTime):
        if exc.window is not None:
            return windowed_not_in_time_result(exc, metric_id)
        return not_in_time_result(exc.system_id, exc.lineage)
    if isinstance(exc, (mr.MetricResolverError, NotImplementedError, KeyError)):
        return {"outcome": "withheld", "status": "unavailable",
                "detail": _bounded_detail(str(exc), type(exc).__name__)}
    return {"outcome": "result_unavailable", "reason": type(exc).__name__}


def windowed_not_in_time_result(exc, metric_id: str | None) -> dict:
    """A read inside an explicit turn window that the engine refused: the
    system agent can compute it with ONE query inside that window, applying
    the definition the computed blocks carry (``definition_lineage``; a cut
    or unreadable definition is marked incomplete and then gives no
    permission). Never an exact split: no split computes a percentile."""
    lineage = _definition_lineage(metric_id, exc.system_id, exc.table or "", exc.lineage)
    return {**not_in_time_result(exc.system_id, lineage.pop("lineage")),
            "computation": ONE_QUERY_COMPUTATION, **lineage,
            "window_applied_range": {"start": exc.window.start_utc,
                                     "end": exc.window.end_utc}}


def _declared_derived_window(metric_id: str) -> str | None:
    """The declared window token of a DERIVED metric, else ``None``."""
    try:
        from . import metric_layer
        metric = metric_layer.get_metric_layer().get_metric(metric_id)
    except Exception:  # noqa: BLE001 - resolve_metric already read this metric
        return None
    if not isinstance(metric, metric_layer.DerivedMetric):
        return None
    return metric.window if isinstance(metric.window, str) and metric.window else None


def _withhold_unapplied_window(metric_id: str, result: dict) -> dict:
    """A COMPUTED derived value whose declared window was not applied is
    WITHHELD (``unavailable``) — never reported under the wrong period."""
    if result.get("outcome") != "computed" or result.get("kind") != "derived":
        return result
    token = _declared_derived_window(metric_id)
    if token is None:
        return result
    return {"outcome": "withheld", "status": "unavailable",
            "detail": _bounded_detail(DERIVED_WINDOW_DETAIL.format(token=_field(token, 20)),
                                      "unavailable")}


def _source_of(metric_id: str) -> str:
    try:
        from . import metric_layer
        is_graph = getattr(metric_layer.get_metric_layer(), "is_graph_defined", None)
        return "graph" if callable(is_graph) and is_graph(metric_id) else "code"
    except Exception:  # noqa: BLE001 - provenance label only
        return ""


def _resolve_one(metric_id: str, deadline: float | None = None,
                 signal: dict | None = None, turn_window=None) -> dict:
    """Resolve ONE metric through the ONE computation home, traced. An exact
    split it needs must finish by ``deadline`` (``time.monotonic()``).

    ``turn_window`` is the turn's resolved absolute window; a metric
    that declares a ``group_by`` resolves to a GROUPED result through
    ``resolve_turn_metric``, otherwise scalar. Both are classified here."""
    from . import metric_resolver as mr
    from .metric_athena_scope import turn_budget
    from .metric_execution_trace import TURN_SUFFIX, execution_trace
    try:
        with execution_trace(TURN_SUFFIX) as trace, turn_budget(deadline, signal):
            resolved = mr.resolve_turn_metric(metric_id, turn_window=turn_window)
            if hasattr(resolved, "rows"):  # a GroupedResolvedMetric (group_by)
                # the overall read shares the SAME trace and turn budget as the
                # grouped read; it is skipped when no budget remains.
                overall = _overall_row(mr, metric_id, turn_window, deadline)
                result = _classify_grouped(resolved, turn_window, overall)
            else:
                result = _withhold_unapplied_window(metric_id, _classify(resolved))
            if result["outcome"] == "computed":
                cost = trace.cost()
                result["query_count"] = cost["query_count"]
                result["systems"] = [s["system_id"] for s in cost["systems"]]
    except Exception as exc:  # noqa: BLE001 - classified, never raised
        result = _per_site_or_error(metric_id, exc, turn_window)
    result["source"] = _source_of(metric_id)
    return result


def _overall_row(mr, metric_id: str, turn_window, deadline=None) -> dict | None:
    """The OVERALL (ungrouped) percentile row for a grouped turn: the SAME
    metric resolved WITHOUT its group_by, over the same window and filters,
    inside the caller's active trace and turn budget. ``None`` when it could not
    be computed OR when the turn budget is already spent (the groups are still
    delivered; the notice then says the overall row was not computed). Never
    raises."""
    import time
    if deadline is not None and time.monotonic() >= deadline:
        return None  # no budget left after the grouped read, skip the extra read
    try:
        scalar = mr.resolve_metric(metric_id, turn_window=turn_window)
        pct = getattr(scalar, "percentiles", None)
        usable = [{"p": p, "value": v} for p, v in (pct or ())
                  if isinstance(v, (int, float)) and not isinstance(v, bool)
                  and math.isfinite(float(v))]
        if not usable or scalar.sample_count == 0:
            return None
        return {"percentiles": usable, "sample_count": scalar.sample_count}
    except Exception:  # noqa: BLE001 - the groups still deliver
        return None


def _classify_grouped(grm, turn_window=None, overall=None) -> dict:
    """A grouped duration-percentile turn result: EVERY
    group with its (p, value) pairs and sample count, the applied absolute
    ``[start, end)`` window, an OVERALL row, the pinned dimension as scope, and
    the dialect approximation flag. No usable group ⇒ ``no-data``."""
    groups = [g for g in (_grouped_entry(row) for row in getattr(grm, "rows", ()) or ())
              if g is not None]
    if not groups:
        return {"outcome": "withheld", "status": "no-data",
                "detail": _bounded_detail(None, NO_DATA_DETAIL)}
    headline = max(groups[0]["percentiles"], key=lambda e: e["p"])["value"]
    plan = getattr(grm, "query_plan", None)
    result = {"outcome": "computed", "value": headline, "unit": grm.unit,
              "system_id": grm.system_id, "kind": "direct", "shape": "direct",
              **_grouped_lineage(grm),
              "dimension_concept": grm.dimension_concept,
              "window_requested": None, "window_applied": None,
              "grouped": groups[:MAX_GROUPED_ENTRIES],
              "percentiles": groups[0]["percentiles"]}
    if getattr(plan, "approximate", None):  # Athena approx_percentile
        result["approximate"] = True
    window = turn_window or getattr(plan, "absolute_window", None)
    if window is not None:  # the exact applied day boundary
        result["window_applied_range"] = {"start": window.start_utc,
                                           "end": window.end_utc}
    result["overall"] = overall if overall is not None else {"not_computed": True}
    return result


def _grouped_entry(row) -> dict | None:
    """One grouped row as a classified entry: its dimension value,
    usable (p, value) pairs and sample count. ``None`` when no usable pair or
    zero samples."""
    pct = getattr(row, "percentiles", None)
    if not pct:
        return None
    usable = [{"p": p, "value": v} for p, v in pct
              if isinstance(v, (int, float)) and not isinstance(v, bool)
              and math.isfinite(float(v))]
    if not usable or row.sample_count == 0:
        return None
    return {"group": row.dimension_value, "percentiles": usable,
            "sample_count": row.sample_count}


def _per_site_or_error(metric_id: str, exc: BaseException, turn_window=None) -> dict:
    """ADR-0094: an authority ambiguity that is a per-site partition is
    answered per site (each site over the same ``turn_window``); everything
    else keeps its dashboard classification."""
    from functools import partial
    from .metric_turn_sites import per_site_result
    classify_error = partial(_classify_error, metric_id=metric_id)
    try:
        per_site = per_site_result(metric_id, exc, _classify, classify_error,
                                   _withhold_unapplied_window, turn_window=turn_window)
    except Exception as fan_exc:  # noqa: BLE001 - the original withhold stands
        logger.info("metric-layer per-site resolve failed category=%s",
                    type(fan_exc).__name__)
        per_site = None
    return per_site if per_site is not None else classify_error(exc)


def _named_site_scope(question: str, results: list[dict]) -> list[dict]:
    """``results`` with the sites ``question`` names marked on each
    deployment-wide computed result (``metric_turn_sites``). Fails open."""
    from .metric_turn_sites import named_site_scope
    return named_site_scope(question, results)


# --------------------------------------------------------------------------- #
# Cache + pool + timebox
# --------------------------------------------------------------------------- #
def _cache_key(metric_id: str, turn_window=None) -> tuple[str, str]:
    # authority context: default graph authority (D9). the turn window
    # (if any) joins the key, so a windowed result never serves a plain turn.
    window = ""
    if turn_window is not None:
        window = f"{turn_window.start_utc}|{turn_window.end_utc}"
    return (metric_id, window)


def _cached(key: tuple[str, str]) -> dict | None:
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if entry[0] > _clock():
            return dict(entry[1])
        _CACHE.pop(key, None)
        return None


def _generation() -> int:
    with _LOCK:
        return _GENERATION


def _store(key: tuple[str, str], result: dict, ttl: float, generation: int, *,
           if_absent: bool = False) -> bool:
    """Cache ``result`` ONLY if no :func:`invalidate` ran since ``generation``
    was captured (at submission) — a resolve that started before a steward
    accept can never resurrect its pre-accept result. Returns stored?"""
    if result.get("outcome") == "result_unavailable":
        ttl = min(ttl, FAILURE_COOLDOWN_SECONDS)
    with _LOCK:
        if generation != _GENERATION:
            return False
        current = _CACHE.get(key)
        if if_absent and current is not None and current[0] > _clock():
            return False
        _CACHE[key] = (_clock() + ttl, dict(result))
        return True


def invalidate() -> None:
    """Drop every cached result AND bump the generation, so an in-flight
    resolve submitted before this call cannot store its (stale) result."""
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        _CACHE.clear()


def _pool() -> ThreadPoolExecutor:
    global _POOL
    with _LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=METRIC_TURN_MAX_WORKERS,
                                       thread_name_prefix="adt-metric-turn")
        return _POOL


def _resolve_and_store(metric_id: str, ttl: float, generation: int,
                       deadline: float | None = None, signal: dict | None = None,
                       turn_window=None) -> dict:
    result = _resolve_one(metric_id, deadline, signal, turn_window)
    _store(_cache_key(metric_id, turn_window), result, ttl, generation)
    return result


def _missed(signal: dict) -> dict:
    """The result of a resolve that missed the timebox: the not-in-time
    outcome when it was inside an exact split, else ``timebox``."""
    if signal.get("split"):
        return not_in_time_result(signal.get("system_id", ""), signal.get("lineage", ""))
    return {"outcome": "result_unavailable", "reason": "timebox"}


def _submit_all(metric_ids: list[str], timebox: float, ttl: float,
                turn_window=None) -> dict[str, dict]:
    """Resolve on the bounded pool; whatever misses the timebox is
    ``result_unavailable (timebox)``, or not computed in time when it was in
    an exact split — a straggler still stores its own result when it lands
    (unless an invalidation intervened). An exact split must end
    ``TURN_MARGIN_SECONDS`` before the timebox does."""
    from .metric_athena_scope import TURN_MARGIN_SECONDS
    generation = _generation()
    deadline = time.monotonic() + timebox - TURN_MARGIN_SECONDS
    signals = {mid: {} for mid in metric_ids}
    futures = {mid: _pool().submit(_resolve_and_store, mid, ttl, generation,
                                   deadline, signals[mid], turn_window)
               for mid in metric_ids}
    done, _pending = wait(list(futures.values()), timeout=timebox)
    out: dict[str, dict] = {}
    for metric_id, future in futures.items():
        if future in done:
            try:
                out[metric_id] = future.result()
            except Exception as exc:  # noqa: BLE001 - classified, never raised
                out[metric_id] = {"outcome": "result_unavailable",
                                  "reason": type(exc).__name__}
            continue
        future.cancel()
        out[metric_id] = _missed(signals[metric_id])
        _store(_cache_key(metric_id, turn_window), out[metric_id], ttl,
               generation, if_absent=True)
    return out


def _merge(match, by_id: dict[str, dict]) -> dict:
    if match.ambiguous:
        return {"outcome": "ambiguous_term", "term": match.term,
                "candidates": list(match.metric_ids)}
    metric_id = match.metric_ids[0]
    return {**by_id[metric_id], "term": match.term, "metric_id": metric_id,
            "label": match.label}


def _overflow_result(overflow) -> list[dict]:
    return [{"outcome": "overflow", "term": overflow.term}] if overflow else []


def _matches_for_turn(question: str):
    """``(matches, overflow)`` — at most the cap, plus the first over-cap match
    (scanned only far enough to DETECT overflow). The MATCHER is the one step
    that fails OPEN to no notice: an exception means we do not know that a
    defined metric was named at all."""
    try:
        found = mtr.defined_metric_terms(question, limit=mtr.MAX_METRIC_TERMS + 1)
    except mtr.MatcherInputTooLong:
        raise                    # over the bound ⇒ fail CLOSED (the caller)
    except Exception as exc:  # noqa: BLE001 - matcher fails OPEN (D3)
        logger.info("metric-layer turn matcher unavailable category=%s",
                    type(exc).__name__)
        return [], None
    overflow = found[mtr.MAX_METRIC_TERMS] if len(found) > mtr.MAX_METRIC_TERMS else None
    return found[:mtr.MAX_METRIC_TERMS], overflow


def _turn_window(question: str):
    """The turn's resolved ABSOLUTE ``[start, end)`` UTC window, or
    ``None``. Exactly ONE resolved window (one date, one scope) becomes an
    ``AbsoluteWindow`` applied to a measure's END column; zero, several, an
    overflow, or ANY failure yields ``None`` (the metric then keeps its
    declared-token / lifetime behaviour, never a guessed window). Fail-soft."""
    try:
        from . import calendar_policy as cp
        from . import metric_resolver as mr
        from . import time_windows as tw
        resolved = tw.resolve(question, cp.load_policy())
        windows = [w for item in resolved for w in getattr(item, "windows", ())]
        if len(windows) != 1:
            return None
        w = windows[0]
        return mr.AbsoluteWindow(start_utc=w.start_utc.isoformat(),
                                 end_utc=w.end_utc.isoformat())
    except Exception:  # noqa: BLE001 - a window read never breaks a turn
        return None


def _unavailable_results(matches, overflow, reason: str) -> list[dict]:
    """Post-match failure ⇒ a FORBIDDING block per named metric, never silence."""
    by_id = {mid: {"outcome": "result_unavailable", "reason": reason}
             for m in matches for mid in m.metric_ids}
    return [_merge(m, by_id) for m in matches] + _overflow_result(overflow)


def _resolve_matches(matches, overflow, turn_window=None) -> list[dict]:
    """Cache → pool → timebox for the admitted matches. Bounds are read HERE,
    so a malformed bound raises into the caller's forbidding fallback."""
    timebox, ttl = _timebox_seconds(), _cache_ttl_seconds()
    started = _clock()
    wanted = list(dict.fromkeys(m.metric_ids[0] for m in matches if not m.ambiguous))
    by_id = {mid: hit for mid in wanted
             if (hit := _cached(_cache_key(mid, turn_window))) is not None}
    pending = [mid for mid in wanted if mid not in by_id]
    if pending:
        by_id.update(_submit_all(pending, timebox, ttl, turn_window))
    results = [_merge(match, by_id) for match in matches]
    logger.info(
        "metric-layer turn notice metrics=%s outcomes=%s elapsed_ms=%d queries=%s",
        [r.get("metric_id", "") for r in results], [r["outcome"] for r in results],
        int((_clock() - started) * 1000), [r.get("query_count") for r in results])
    return results + _overflow_result(overflow)


def _fallback_reason(exc: BaseException) -> str:
    if isinstance(exc, ValueError) and "METRIC_TURN_" in str(exc):
        logger.error("metric-layer turn notice misconfigured: %s", exc)
        return "invalid-config"
    logger.error("metric-layer turn resolution failed category=%s", type(exc).__name__)
    return type(exc).__name__


def results_for_turn(question: str, session_id: str = "") -> list[dict]:
    """The metric layer's structured results for the defined metrics
    ``question`` names (``[]`` when none or disabled). EVERY turn kind —
    briefings and scheduled prompts included — is treated alike.

    Only the matcher fails open, and never over the matching bound (a
    FORBIDDING ``input_overflow`` block). Once a defined metric is named,
    NOTHING is silent: a malformed bound (ERROR log; rejected at boot by
    :func:`validate_config`), any post-match failure, or a result failing its
    own sanitation yields a forbidding ``result_unavailable`` block."""
    del session_id  # composition is session-independent
    if not enabled():
        return []
    try:
        matches, overflow = _matches_for_turn(question)
    except mtr.MatcherInputTooLong as exc:
        logger.info("metric-layer turn input over bound chars=%d", exc.length)
        return [input_overflow_result()]
    if not matches:
        return []
    turn_window = _turn_window(question)
    try:
        results = _resolve_matches(matches, overflow, turn_window)
    except Exception as exc:  # noqa: BLE001 - degrade to FORBIDDING, never silence
        results = _unavailable_results(matches, overflow, _fallback_reason(exc))
    results = _named_site_scope(question, results)  # per question, never cached
    fallback = _unavailable_results(matches, overflow, "sanitize")
    cleaned = [sanitize_result(r) or sanitize_result(fallback[i])
               for i, r in enumerate(results)]
    return [c for c in cleaned if c is not None]


def computed_concepts(results) -> set:
    """The authority/identity concepts the COMPUTED metrics in ``results`` are
    bound to. Maps each ``computed`` result's ``metric_id``
    through the served metric layer to its ``concept``. Fail-soft: any layer
    failure yields an empty set, so an unreadable layer never drops an engine
    fact it cannot justify. Never resolves anything."""
    ids = [r.get("metric_id") for r in (results or [])
           if isinstance(r, dict) and r.get("outcome") == "computed"
           and r.get("metric_id")]
    if not ids:
        return set()
    try:
        from . import metric_layer
        layer = metric_layer.get_metric_layer()
    except Exception:  # noqa: BLE001 - fail-soft: no drop without the layer
        return set()
    concepts: set = set()
    for metric_id in ids:
        try:
            metric = layer.get_metric(metric_id)
        except Exception:  # noqa: BLE001 - a missing id contributes nothing
            continue
        concept = getattr(metric, "concept", None)
        if isinstance(concept, str) and concept:
            concepts.add(concept)
    return concepts


def validate_config() -> None:
    """Boot-time fail-loud check of the finite bounds (``server.lifespan``):
    a malformed ``METRIC_TURN_*`` value refuses to start the app."""
    if enabled():
        _timebox_seconds()
        _cache_ttl_seconds()


def extra_payload_with_results(question: str, session_id: str,
                               extra: dict | None) -> dict | None:
    """``extra`` plus the RESERVED results key when a defined metric was named;
    ``extra`` itself (byte-identical payload) otherwise.

    The key is HOOK-OWNED: a caller-supplied ``metricLayerResults`` is
    stripped FIRST, on every path (matched, unmatched, disabled), so only
    :func:`results_for_turn` can ever put a result on the wire."""
    extra = without_reserved_results(extra)
    results = results_for_turn(question, session_id)
    if not results:
        return extra
    merged = dict(extra or {})
    merged[METRIC_LAYER_RESULTS_KEY] = results
    return merged


def local_turn_prompt(prompt, question: str, session_id: str):
    """LOCAL mode (``LocalChatClient``, no runtime prelude): ``prompt`` with the
    SAME rendered notice as a seed-independent preamble before the documented
    ``\\n\\n---\\n\\n`` separator (``memory_labels.strip_turn_preambles``
    recovers the question). No defined metric named ⇒ ``prompt`` unchanged
    (identity). Runs in the local producer thread, never the event loop."""
    notice = render_or_fallback(results_for_turn(question, session_id))
    if not notice:
        return prompt
    return f"{notice}\n\n---\n\n{prompt}"
