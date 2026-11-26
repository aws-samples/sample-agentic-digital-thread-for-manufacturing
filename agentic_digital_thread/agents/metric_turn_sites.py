# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0094 — a defined metric whose authority is held PER SITE is answered
per site, not withheld.

Onboarding mints one ``AuthorityRule`` per system for the concepts its class
owns, scoped same-plant (ADR-0045), so a four-site deployment carries one
quality/OEE authority per plant's MES. The scalar resolve
(``GraphAuthorityResolver``) still fails loud on that state — a deployment-wide
scalar does not exist — and raises :class:`metric_resolver.AmbiguousAuthorityError`
naming the systems. This module tells the two cases apart:

* a **site partition** — every named system is onboarded, SITED
  (``metric_multisite.site_of`` non-empty) and at a DIFFERENT site, at most
  :data:`MAX_SITES` of them ⇒ resolve the metric once per site with that
  site's authority pinned (``metric_resolver.PinnedAuthority``, the
  ``metric_multisite`` pin) and carry every site's own computed/withheld
  outcome in the dashboard's register;
* anything else (an unsited system, two systems at one site, an unknown
  system) is a genuine conflict ⇒ ``None`` and the caller keeps the existing
  ``unavailable`` withhold, reason verbatim.

No cross-site value is combined here: the block reports each site's value and
forbids the model from totalling or averaging them.

The other direction, a deployment-wide result asked about at a NAMED site
(:func:`named_site_scope`): the result covers every site, so its block says
that it is not the named site's figure and how that figure may be computed.

Two halves like ``metric_turn_notice`` / ``metric_turn_render``: the app half
(:func:`per_site_result`) imports catalog, resolver and trace lazily; the
runtime half (:func:`sanitize_sites`, :func:`render_sites_block`,
:func:`site_window_classes`) is pure — stdlib plus ``metric_turn_render``.
"""
from __future__ import annotations

from . import metric_turn_render as _r

#: The outcome spelling on the reserved payload key.
SITES_OUTCOME = "computed_by_site"
#: Upper bound on sites in one block (the notice's list cap).
MAX_SITES = _r.MAX_LIST_ITEMS
#: Fields copied per site through ``sanitize_result`` (everything else dropped).
_SITE_RESULT_KEYS = ("outcome", "value", "unit", "kind", "shape", "lineage",
                     "window_requested", "window_applied", "input_windows",
                     "input_windows_truncated", "sample_count", "status",
                     "detail", "reason", "scan", "scan_narrowed", "scan_inexact", "system_id",
                     "window_applied_range", "computation", "lineage_incomplete",
                     "percentiles", "approximate")

SITES_RULE_LINE = (
    "  These per-site values ARE the metric layer's result: authority for this "
    "metric is held per site. Report each site's value as that site's figure; a "
    "WITHHELD or UNAVAILABLE site gets NO number. The metric layer computes no "
    "cross-site total or average — do not compute one.")


# --------------------------------------------------------------------------- #
# App half
# --------------------------------------------------------------------------- #
def ambiguous_authority_in(exc: BaseException):
    """The :class:`AmbiguousAuthorityError` in ``exc``'s cause chain (a
    derived metric wraps its input's error), or ``None``."""
    from . import metric_resolver as mr
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, mr.AmbiguousAuthorityError):
            return exc
        exc, seen = exc.__cause__, seen + 1
    return None


def site_partition(system_ids, systems: list[dict]) -> list[dict] | None:
    """``[{"site", "system_id"}]`` sorted by site when ``system_ids`` is a
    per-site partition of ``systems`` (catalog entries), else ``None``."""
    from .metric_multisite import site_of, site_sort_key
    by_id = {str(s.get("id")): s for s in systems if s.get("id")}
    picked = []
    for sid in sorted({str(s) for s in system_ids}):
        entry = by_id.get(sid)
        site = site_of(entry) if entry is not None else ""
        if not site:
            return None
        picked.append({"site": site, "system_id": sid})
    if not 2 <= len(picked) <= MAX_SITES:
        return None
    if len({p["site"].casefold() for p in picked}) != len(picked):
        return None
    return sorted(picked, key=lambda p: site_sort_key(p["site"], p["system_id"]))


def per_site_result(metric_id: str, exc: BaseException, classify, classify_error,
                    finish, *, turn_window=None) -> dict | None:
    """The ``computed_by_site`` result for ``metric_id``, or ``None`` when the
    authority ambiguity in ``exc`` is not a per-site partition. ``classify`` /
    ``classify_error`` / ``finish`` are the notice's own classifiers, so every
    site lands in the SAME register as a scalar result. ``turn_window`` (the
    turn's explicit ``[start, end)``) bounds every site's read exactly as it
    bounds the unsited one."""
    ambiguity = ambiguous_authority_in(exc)
    if ambiguity is None:
        return None
    from .catalog import catalog
    partition = site_partition(ambiguity.system_ids, catalog.get_onboarded())
    if partition is None:
        return None
    from .metric_execution_trace import TURN_SUFFIX, execution_trace
    sites = []
    with execution_trace(TURN_SUFFIX) as trace:
        for part in partition:
            try:
                site_result = _site_result(metric_id, part["system_id"], turn_window,
                                           classify, finish)
            except Exception as site_exc:  # noqa: BLE001 - classified per site
                site_result = classify_error(site_exc)
            sites.append({**site_result, **part})
    cost = trace.cost()
    return {"outcome": SITES_OUTCOME, "concept": ambiguity.concept, "sites": sites,
            "query_count": cost["query_count"],
            "systems": [s["system_id"] for s in cost["systems"]]}


def _site_result(metric_id: str, system_id: str, turn_window, classify, finish) -> dict:
    """One site's classified result. A grouped duration definition is not
    resolved here: this path gives one value per site, never per-group rows,
    so the site gives no number and no hand-over (:func:`_grouped_hand_over`)."""
    grouped = _grouped_definition(metric_id)
    if grouped is not None:
        return _grouped_hand_over(metric_id, system_id, grouped, turn_window)
    return finish(metric_id, classify(_resolve_site(metric_id, system_id, turn_window)))


def _grouped_definition(metric_id: str):
    """The served metric when it is a grouped duration definition (the shape
    ``metric_resolver.resolve_turn_metric`` resolves grouped), else ``None``."""
    try:
        from . import metric_resolver as mr
        metric = mr.get_metric_layer().get_metric(metric_id)
    except Exception:  # noqa: BLE001 - unreadable: the scalar resolve decides
        return None
    grouped = (getattr(metric, "group_by", None) is not None
               and getattr(metric, "measure", None) is not None)
    return metric if grouped else None


def _grouped_hand_over(metric_id: str, system_id: str, metric, turn_window) -> dict:
    """A grouped definition at one site: NOT computed and NOT delegated. This
    path does not validate the definition's measure, filter and group-by
    bindings at each site, so it cannot say the site's system agent may apply
    it: the result is always marked incomplete, which renders no number and no
    one-query permission. The definition and window stay on the payload."""
    from . import metric_resolver as mr
    from .metric_turn_lineage import definition_lineage
    _auth, mapping = mr._resolve_measure_target(metric, mr.PinnedAuthority(system_id))
    result = {"outcome": _r.NOT_IN_TIME_OUTCOME, "system_id": system_id,
              "computation": _r.GROUPED_COMPUTATION,
              **definition_lineage(metric_id, system_id, mapping.table,
                                   f"grouped by {metric.group_by.concept}"),
              _r.LINEAGE_INCOMPLETE_KEY: True}
    if turn_window is None:
        return result
    return {**result, "window_applied_range": {"start": turn_window.start_utc,
                                               "end": turn_window.end_utc}}


def _resolve_site(metric_id: str, system_id: str, turn_window):
    """One site's resolve with that site's authority pinned. Without a turn
    window the call is exactly the one this path always made."""
    from . import metric_resolver as mr
    window = {"turn_window": turn_window} if turn_window is not None else {}
    return mr.resolve_metric(metric_id, authority=mr.PinnedAuthority(system_id), **window)


def named_site_scope(question: str, results: list[dict]) -> list[dict]:
    """``results`` with :data:`metric_turn_render.NAMED_SITES_KEY` set on each
    deployment-wide computed result: the calendar policy's sites the question
    names (``time_windows.named_sites``) other than the authority system's own
    site (``metric_multisite.site_of``). A per-site result, a derived formula
    and every other outcome are unchanged. Fails open: any failure detecting
    sites marks nothing."""
    try:
        from . import calendar_policy as cp
        from . import time_windows as tw
        policy = cp.load_policy()
        names = [s.site for s in tw.named_sites(question, policy)] if policy else []
        return with_named_sites(results, names) if names else results
    except Exception:  # noqa: BLE001 - a site read never breaks a turn
        return results


def with_named_sites(results: list[dict], names: list[str]) -> list[dict]:
    """Mark ``names`` on each computed direct result whose authority system
    is not sited at that name. Raises on a catalog fault (the caller fails
    open)."""
    from .catalog import catalog
    from .metric_multisite import site_of
    out = []
    for result in results:
        if (not isinstance(result, dict) or result.get("outcome") != "computed"
                or result.get("shape") == "formula"):
            out.append(result)
            continue
        entry = catalog.get(result.get("system_id") or "")
        own = site_of(entry).casefold() if entry else None
        other = [n for n in names if n.casefold() != own][:MAX_SITES]
        if not (entry and other):
            out.append(result)
            continue
        marked = {**result, _r.NAMED_SITES_KEY: other}
        if _plan_text_not_definition(result):
            marked[_r.LINEAGE_INCOMPLETE_KEY] = True
        out.append(marked)
    return out


def _plan_text_not_definition(result: dict) -> bool:
    """The lineage is plan text that does not state the whole definition: no
    plan was read, or a non-duration metric declares a filter its plan text
    omits (a duration lineage is already the rendered definition, flagged
    itself when cut). An unreadable metric counts as not whole."""
    if result.get("lineage") == "direct read":
        return True
    try:
        from . import metric_resolver as mr
        metric = mr.get_metric_layer().get_metric(result.get("metric_id"))
    except Exception:  # noqa: BLE001 - unreadable: never treated as a whole definition
        return True
    return getattr(metric, "measure", None) is None and bool(getattr(metric, "filter", None))


# --------------------------------------------------------------------------- #
# Runtime half (pure)
# --------------------------------------------------------------------------- #
def _sanitize_site(raw: object, parent: dict) -> dict | None:
    if not isinstance(raw, dict) or raw.get("outcome") == SITES_OUTCOME:
        return None
    site = _r._field(raw.get("site"))
    system_id = _r._field(raw.get("system_id"))
    if not site or not system_id:
        return None
    copied = {k: raw[k] for k in _SITE_RESULT_KEYS if k in raw}
    clean = _r.sanitize_result({**copied, "metric_id": parent["metric_id"],
                                "term": "", "label": "", "source": ""})
    if clean is None or clean["outcome"] not in ("computed", "withheld", "result_unavailable",
                                                 _r.NOT_IN_TIME_OUTCOME):
        return None
    for key in ("term", "label", "source", "metric_id", "system_id"):
        clean.pop(key, None)
    return {"site": site, "system_id": system_id, **clean}


def sanitize_sites(item: dict, clean: dict) -> dict | None:
    """Re-bound a ``computed_by_site`` result; ``None`` (⇒ the invalid
    marker) when any site is malformed or the list is empty/oversize."""
    raw_sites = item.get("sites")
    if not isinstance(raw_sites, list) or not 1 <= len(raw_sites) <= MAX_SITES:
        return None
    sites = [_sanitize_site(s, clean) for s in raw_sites]
    if any(s is None for s in sites):
        return None
    clean.update(concept=_r._field(item.get("concept")), sites=sites,
                 query_count=_r._count(item.get("query_count")) or 0,
                 systems=[_r._field(s) for s in (item.get("systems") or [])
                          [:MAX_SITES] if isinstance(s, str)])
    return clean


def site_window_classes(result: dict) -> set[str]:
    """Window classes of the COMPUTED sites (feeds the closing scope sentence)."""
    return {_r.window_class_and_line(s)[0] for s in result.get("sites") or []
            if s.get("outcome") == "computed"}


def _common(values: list[str]) -> str | None:
    return values[0] if values and len(set(values)) == 1 else None


def _shared_lineage(computed: list[dict]) -> str | None:
    """The lineage with each site's own system id stripped, when identical."""
    tails = []
    for s in computed:
        lineage, prefix = s.get("lineage", ""), f"{s['system_id']}."
        tails.append(lineage[len(prefix):] if lineage.startswith(prefix) else lineage)
    return _common(tails)


def _site_line(s: dict, window_shared: bool, lineage_shared: bool) -> str:
    head = f"  {s['site']} ({s['system_id']}): "
    if s["outcome"] == "withheld":
        return f"{head}WITHHELD — status '{s.get('status', '')}': {s.get('detail', '')}"
    if s["outcome"] == "result_unavailable":
        return f"{head}UNAVAILABLE ({s.get('reason', 'unknown')})"
    if s["outcome"] == _r.NOT_IN_TIME_OUTCOME:
        return head + _not_in_time_text(s)
    unit, pct = s.get("unit", ""), s.get("percentiles")
    if pct:  # each percentile labelled, Athena marked approximate, as the scalar block
        approx = " (approximate: Athena approx_percentile)" if s.get("approximate") else ""
        value = f"percentiles: {_r._grouped_percentiles_text(pct, unit)}{approx}"
    else:
        value = f"{_r._fmt_value(s['value'])} {unit}".rstrip()
    parts = [value + (" (NOT EXACT)" if s.get("scan_inexact") else "")]
    if not window_shared:
        parts.append(f"window: {_r.window_class_and_line(s)[1]}")
    if s.get("sample_count") is not None:
        parts.append(f"samples: {s['sample_count']}")
    if not lineage_shared:
        parts.append(f"lineage: {s.get('lineage', '')}")
    return head + " | ".join(parts)


def _not_in_time_text(s: dict) -> str:
    """A site the metric layer did not compute: over its time budget in an
    exact split (unchanged), a refused query inside an explicit window, or a
    grouped definition this path gives no per-group rows for; that site's
    system agent may run one query applying the stated definition, or
    (definition not whole, or no window) no number at all."""
    kind = s.get("computation")
    if kind not in _r.ONE_QUERY_KINDS:
        return ("NOT COMPUTED IN TIME (over the per-query scan limit; that system's "
                f"agent can compute it exactly: {s.get('lineage', '')})")
    rng = s.get("window_applied_range")
    span = f" inside window [{rng['start']}, {rng['end']})" if rng else ""
    grouped = kind == _r.GROUPED_COMPUTATION
    why = (" is grouped, and the metric layer gives no per-group figures per site" if grouped
           else " is over the per-query scan limit")
    where = f"its query{span}{why}"
    if s.get(_r.LINEAGE_INCOMPLETE_KEY):
        return (f"NOT COMPUTED ({where}; the metric layer could not compute it): "
                "NO number for this site")
    label = "NOT COMPUTED" if grouped else "NOT COMPUTED IN TIME"
    return (f"{label} ({where}; that system's agent can compute it with one "
            f"query inside that window applying: {s.get('lineage', '')})")


def render_sites_block(r: dict, head: str) -> str:
    """The block for one ``computed_by_site`` result."""
    sites = r.get("sites") or []
    computed = [s for s in sites if s.get("outcome") == "computed"]
    window = _common([_r.window_class_and_line(s)[1] for s in computed])
    lineage = _shared_lineage(computed)
    source = f"{r['source']}-defined" if r.get("source") else "defined"
    kind = computed[0].get("kind", "direct") if computed else "direct"
    shared = [f"authority: per site ({len(sites)} sites, concept "
              f"'{r.get('concept', '')}')"]
    if window is not None:
        shared.append(f"window: {window}")
    if lineage is not None:
        shared.append(f"lineage: <site system>.{lineage}")
    shared.append(f"queries: {r.get('query_count', 0)}")
    formula = any(s.get("shape") == "formula" for s in computed)
    lines = [f"{head} (\"{r.get('label', '')}\", {kind}, {source}): COMPUTED PER SITE.",
             "  " + " | ".join(shared),
             *[_site_line(s, window is not None, lineage is not None) for s in sites],
             SITES_RULE_LINE]
    if computed:
        incomplete = any(s.get(_r.LINEAGE_INCOMPLETE_KEY) for s in computed)
        lines.append(_r.NOT_AVAILABLE_LINE if formula or incomplete else _r.PERMITTED_LINE)
    return "\n".join(lines)
