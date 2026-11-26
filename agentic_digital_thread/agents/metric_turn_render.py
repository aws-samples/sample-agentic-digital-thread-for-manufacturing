# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 runtime half — re-sanitize the structured metric-layer results that
arrived on the RESERVED payload key and render the notice text.

Split from ``metric_turn_notice`` (the app half: resolution, cache, timebox)
for the file-size cap; ``metric_turn_notice`` re-exports every public name
here, so callers import from ONE place. Pure and import-pure: stdlib +
``metric_term_resolution``; ``WINDOW_VOCAB`` is read lazily.
"""
from __future__ import annotations

import math

from . import metric_term_resolution as mtr

#: RESERVED runtime-payload key (ADR-0049 posture: popped unconditionally).
METRIC_LAYER_RESULTS_KEY = "metricLayerResults"
METRIC_LAYER_HEADER = "[METRIC LAYER — DETERMINISTIC RESULT]"
WINDOW_SCOPE_SENTENCE = (
    "The metric layer provides exactly the value above for its declared window "
    "token; arbitrary date ranges and per-dimension breakdowns are not available "
    "from it this turn.")
LIFETIME_SCOPE_SENTENCE = (
    "The metric layer provides exactly the value above over all recorded data "
    "(lifetime — no window token applies); windowed figures, arbitrary date "
    "ranges and per-dimension breakdowns are not available from it this turn.")
MIXED_SCOPE_SENTENCE = (
    "The metric layer provides exactly the value(s) above for the window stated "
    "on each block (a declared window token, or lifetime where none applies); "
    "other windows, arbitrary date ranges and per-dimension breakdowns are not "
    "available from it this turn.")
#: Used when a computed result is grouped and/or approximate (any Athena
#: percentile). It must NOT claim exactness and must NOT say per-dimension
#: breakdowns are unavailable, because the block just supplied them.
GROUPED_SCOPE_SENTENCE = (
    "The metric layer provides the value(s) above for the scope and window "
    "stated on each block, including the per-dimension breakdown it computed "
    "for that scope.")
GROUPED_APPROX_SCOPE_SENTENCE = (
    "The metric layer provides the figure(s) above for the scope and window "
    "stated on each block, including the per-dimension breakdown it computed "
    "for that scope; a figure marked approximate is the engine's estimate for "
    "that scope and must be reported as approximate, never as exact.")
APPROXIMATE_SCOPE_SENTENCE = (
    "The metric layer provides the value(s) above for the window stated on each "
    "block; a figure marked approximate is the engine's estimate for that scope "
    "and must be reported as approximate, never as exact. Other windows and "
    "arbitrary date ranges are not available from it this turn.")
#: Appended to the closing sentence when a computed value is NOT EXACT.
INEXACT_SENTENCE = (
    " A value marked NOT EXACT was added from partition pieces and can differ from one "
    "query's result in its last digits: report it as approximate, never as exact.")
NO_VALUE_SCOPE_SENTENCE = (
    "The metric layer provides no value for the metric(s) above this turn; no "
    "window, arbitrary date range or per-dimension breakdown of them is "
    "available from it either.")
INVALID_LINE = (
    "- Metric-layer results for this question did not arrive intact: "
    "METRIC-LAYER RESULT UNAVAILABLE THIS TURN for every defined metric the "
    "question names that is not listed above. Give NO number for such a metric "
    "— do not compute, estimate or illustrate a substitute.")
PERMITTED_LINE = (
    "  Scoped/other-window figures: PERMITTED only by applying this exact "
    "lineage through a system agent, labelled as computed outside the metric "
    "layer at that scope.")
NOT_AVAILABLE_LINE = (
    "  Scoped/other-window figures: NOT AVAILABLE — the formula is applied only "
    "by the metric layer; do not compute one.")
#: The key a deployment-wide computed result carries the sites its question
#: names under (``metric_turn_sites.named_site_scope``), and the one line it
#: adds to the block.
NAMED_SITES_KEY = "named_sites"
NAMED_SITE_LINE = (
    "  Site(s) named in the question: {sites}. This result covers every site and is "
    "NOT a named site's figure; a named site's figure is PERMITTED only by applying "
    "this exact lineage through the system agent at that site.")
#: The named-site line when the lineage is NOT the whole definition
#: (:data:`LINEAGE_INCOMPLETE_KEY`): no permission rests on it.
NAMED_SITE_UNSCOPED_LINE = (
    "  Site(s) named in the question: {sites}. This result covers every site and must "
    "NOT be reported as a named site's figure.")
#: Set on a computed result whose lineage is not the whole definition (cut at
#: the notice bound, unreadable, or plan text that omits a declared filter).
LINEAGE_INCOMPLETE_KEY = "lineage_incomplete"
#: Ends a definition cut at the notice's lineage bound. A lineage carrying it
#: is never whole, whether or not the flag above arrived.
LINEAGE_CUT_MARKER = " ... (definition cut at the notice bound)"
#: The words every computed block's scope line opens with.
DEPLOYMENT_WIDE = "deployment-wide"
NO_DATA_DETAIL = "the metric resolved with no samples (no value to report)"
INPUT_OVERFLOW_LINE = (
    f"- The question exceeds the metric layer's matching bound "
    f"({mtr.MAX_MATCH_INPUT_CHARS} characters): the defined metrics it may name "
    "were NOT checked or resolved this turn. Give NO number for any defined "
    "metric it names — do not compute, estimate or illustrate a substitute; ask "
    "the user to shorten or split the request.")

MAX_DETAIL_CHARS = 240
# 3 maximal withheld blocks + required-response rules + the withheld-scope line.
MAX_NOTICE_CHARS = 3400
MAX_FIELD_CHARS = 80
MAX_SYSTEMS_CHARS = 160
MAX_LIST_ITEMS = 6

#: App-side marker: the question exceeded the matcher bound (fail CLOSED).
INPUT_OVERFLOW_OUTCOME = "input_overflow"
#: ADR-0094 per-site result (``metric_turn_sites.SITES_OUTCOME``; spelled here
#: too because that module imports this one).
SITES_OUTCOME = "computed_by_site"
#: Over the per-query scan limit, and its exact computation from partition
#: pieces did not finish in the metric layer's time budget: the system agent
#: (with the full split budget) can compute it.
NOT_IN_TIME_OUTCOME = "not_computed_in_time"
OUTCOMES = frozenset({"computed", "withheld", "result_unavailable", "ambiguous_term",
                      "overflow", INPUT_OVERFLOW_OUTCOME, SITES_OUTCOME, NOT_IN_TIME_OUTCOME})
NOT_IN_TIME_LINE = (
    "  PERMITTED: delegate ONE query to that system agent for this metric as defined "
    "(lineage above); it computes it exactly from partition pieces. Report its result "
    "as computed by that system agent, not by the metric layer. Give no number for it "
    "before that answer.")
#: A not-computed-in-time result whose ONE query inside an explicit turn
#: window was refused (a duration metric: no split computes it). Its block
#: states the window and the definition, and asks for exactly that query.
ONE_QUERY_COMPUTATION = "one_query"
#: A per-site result whose definition is grouped: the per-site path gives one
#: value per site, never per-group rows, so it is handed over instead.
GROUPED_COMPUTATION = "one_query_grouped"
ONE_QUERY_KINDS = frozenset({ONE_QUERY_COMPUTATION, GROUPED_COMPUTATION})
NOT_IN_TIME_ONE_QUERY_LINE = (
    "  PERMITTED: delegate ONE query inside the window above to that system agent, "
    "applying this definition (lineage above) and nothing else. Report its result as "
    "computed by that system agent, not by the metric layer. Give no number for it "
    "before that answer.")
#: The same result when its definition could not be carried whole.
NOT_IN_TIME_NO_PERMISSION_LINE = (
    "  The metric layer could not compute it, and no whole definition is stated here to "
    "apply elsewhere: give NO number for this metric.")
#: Runtime-only marker: the reserved key was PRESENT but (part of) it did not
#: survive sanitation. Never accepted from transport (not in :data:`OUTCOMES`).
INVALID_OUTCOME = "invalid"
_PINNED_TAIL_OUTCOMES = frozenset({"overflow", INVALID_OUTCOME, INPUT_OVERFLOW_OUTCOME})


def invalid_result() -> dict:
    return {"outcome": INVALID_OUTCOME}


def input_overflow_result() -> dict:
    return {"outcome": INPUT_OVERFLOW_OUTCOME, "term": ""}


def without_reserved_results(extra):
    """``extra`` minus the RESERVED key (``None`` when nothing else is left):
    the key is hook-owned, so a caller can never supply a result. Pure."""
    if not isinstance(extra, dict) or METRIC_LAYER_RESULTS_KEY not in extra:
        return extra
    rest = {k: v for k, v in extra.items() if k != METRIC_LAYER_RESULTS_KEY}
    return rest or None


# --------------------------------------------------------------------------- #
# Bounded text
# --------------------------------------------------------------------------- #
def _field(value: object, cap: int = MAX_FIELD_CHARS) -> str:
    """Single-line, printable, capped. Pure."""
    single = " ".join(str(value if value is not None else "").split())
    return "".join(ch for ch in single if ch.isprintable())[:cap]


def _bounded_detail(text: object, fallback: str) -> str:
    """A resolver detail VERBATIM — single line, capped — or ``fallback`` when
    it is empty or carries non-printable characters. Pure."""
    single = " ".join(str(text or "").split())
    if not single or not single.isprintable():
        return fallback
    return single[:MAX_DETAIL_CHARS]


# --------------------------------------------------------------------------- #
# Runtime-side sanitation (structured data in, bounded data out)
# --------------------------------------------------------------------------- #
def _window_token(value: object) -> str | None:
    try:
        from .metric_dimension import WINDOW_VOCAB
    except Exception:  # noqa: BLE001 - unknown vocabulary ⇒ no token
        return None
    return value if isinstance(value, str) and value in WINDOW_VOCAB else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _sanitize_input_windows(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [{"input": _field(w.get("input")) or "input",
             "requested": _window_token(w.get("requested")),
             "applied": _window_token(w.get("applied"))}
            for w in raw[:MAX_LIST_ITEMS] if isinstance(w, dict)]


def _sanitize_percentiles(raw: object) -> list:
    """The (p, value) pairs of a duration-percentile computed block,
    bounded to finite numbers and the list cap. ``[]`` when none/malformed."""
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw[:MAX_LIST_ITEMS]:
        if not isinstance(entry, dict):
            continue
        p, v = entry.get("p"), entry.get("value")
        if (isinstance(p, (int, float)) and not isinstance(p, bool) and 0 < p < 1
                and isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v)):
            out.append({"p": float(p), "value": v})
    return out


#: the bounded number of grouped rows a notice carries.
_MAX_GROUPED = 50
#: Charset for an ISO-8601 instant in a window-range field (digits + ``-:T.+Z``).
_ISO_RANGE_CHARS = frozenset("0123456789-:T.Z+ ")


def _sanitize_iso_instant(value: object) -> str | None:
    """A bounded, charset-gated ISO-8601 instant, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()[:40]
    return text if all(ch in _ISO_RANGE_CHARS for ch in text) else None


def _sanitize_group_entry(entry: object) -> dict | None:
    """One grouped row for the notice: a printable dimension value, its
    percentile pairs and sample count. ``None`` when malformed."""
    if not isinstance(entry, dict):
        return None
    pct = _sanitize_percentiles(entry.get("percentiles"))
    if not pct:
        return None
    group = entry.get("group")
    if isinstance(group, str) and group.strip():
        group = _field(group, 60)
    elif isinstance(group, (int, float)) and not isinstance(group, bool):
        pass  # a numeric group value renders as-is
    else:
        # a SQL NULL (or empty) grouping key keeps the row but is labelled, so
        # it is identifiable and not confused with the explicit overall row.
        group = "(no value)"
    return {"group": group, "percentiles": pct,
            "sample_count": _count(entry.get("sample_count"))}


def _sanitize_overall(raw: object) -> dict | None:
    """The OVERALL row: percentile pairs + sample count, or a not-computed
    marker."""
    if isinstance(raw, dict) and raw.get("not_computed") is True:
        return {"not_computed": True}
    pct = _sanitize_percentiles(raw.get("percentiles")) if isinstance(raw, dict) else []
    if not pct:
        return {"not_computed": True}
    return {"percentiles": pct, "sample_count": _count(raw.get("sample_count"))}


def _sanitize_grouped_into(item: dict, clean: dict) -> None:
    """Carry a grouped duration-percentile shape through sanitation:
    every group (bounded), the overall row, the applied absolute
    window range and the pinned dimension. No grouped data ⇒ nothing added."""
    raw_groups = item.get("grouped")
    if not isinstance(raw_groups, list):
        return
    groups = [g for g in (_sanitize_group_entry(e) for e in raw_groups[:_MAX_GROUPED])
              if g is not None]
    if not groups:
        return
    clean["grouped"] = groups
    clean["overall"] = _sanitize_overall(item.get("overall"))
    dim = item.get("dimension_concept")
    if isinstance(dim, str) and dim:
        clean["dimension_concept"] = _field(dim, 60)


def _sanitize_range(raw: object) -> dict | None:
    """An applied ``{start, end}`` window range, both ends charset-gated ISO
    instants, or ``None``."""
    if not isinstance(raw, dict):
        return None
    start = _sanitize_iso_instant(raw.get("start"))
    end = _sanitize_iso_instant(raw.get("end"))
    return {"start": start, "end": end} if start and end else None


def _lineage_whole(item: dict) -> bool:
    """The lineage is the whole definition: not marked incomplete, not longer
    than the bound this sanitizer cuts it to, and not carrying the cut marker."""
    text = _field(item.get("lineage"), MAX_DETAIL_CHARS + 1)
    return (item.get(LINEAGE_INCOMPLETE_KEY) is not True and len(text) <= MAX_DETAIL_CHARS
            and LINEAGE_CUT_MARKER.strip() not in text)


def _sanitize_not_in_time(item: dict, clean: dict) -> dict | None:
    """A not-computed-in-time result. The exact-split shape keeps only its
    system and lineage (unchanged); a refused windowed query also keeps its
    window and whether its definition is whole, and without a valid window
    it is incomplete (no window to state, so nothing to permit)."""
    clean.update(system_id=_field(item.get("system_id")),
                 lineage=_field(item.get("lineage"), MAX_DETAIL_CHARS))
    if item.get("computation") in ONE_QUERY_KINDS:
        clean["computation"] = item["computation"]
        rng = _sanitize_range(item.get("window_applied_range"))
        if rng:
            clean["window_applied_range"] = rng
        if not (rng and _lineage_whole(item)):
            clean[LINEAGE_INCOMPLETE_KEY] = True
    return clean if clean["system_id"] else None


def _sanitize_scope_into(item: dict, clean: dict) -> None:
    """Carry what bounds a computed block's scope: the applied absolute
    ``[start, end)`` window (scalar or grouped), the sites the question
    named, and whether the lineage is the whole definition. A lineage longer
    than the bound is cut here, and then it is not the whole definition."""
    rng = _sanitize_range(item.get("window_applied_range"))
    if rng:
        clean["window_applied_range"] = rng
    sites = _sanitize_named_sites(item.get(NAMED_SITES_KEY))
    if sites:  # only when the question named a site the value is not for
        clean[NAMED_SITES_KEY] = sites
    if not _lineage_whole(item):
        clean[LINEAGE_INCOMPLETE_KEY] = True


def _sanitize_named_sites(raw: object) -> list[str]:
    """The named sites of a computed block, each a bounded printable name.
    ``[]`` (no line) when the list is not a non-empty list of at most
    :data:`MAX_LIST_ITEMS` non-blank strings: a malformed list is dropped
    whole, never trimmed to a partial one."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LIST_ITEMS:
        return []
    names = [_field(s, 60) if isinstance(s, str) else "" for s in raw]
    return names if all(names) else []


def _sanitize_computed(item: dict, clean: dict) -> dict | None:
    value = item.get("value")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        return None
    clean.update(
        value=value, unit=_field(item.get("unit"), 40),
        kind="derived" if item.get("kind") == "derived" else "direct",
        shape="formula" if item.get("shape") == "formula" else "direct",
        system_id=_field(item.get("system_id")),
        lineage=_field(item.get("lineage"), MAX_DETAIL_CHARS),
        window_requested=_window_token(item.get("window_requested")),
        window_applied=_window_token(item.get("window_applied")),
        query_count=_count(item.get("query_count")) or 0,
        systems=[_field(s) for s in (item.get("systems") or [])[:MAX_LIST_ITEMS]
                 if isinstance(s, str)],
        sample_count=_count(item.get("sample_count")))
    pct = _sanitize_percentiles(item.get("percentiles"))
    if pct:  # a duration-percentile computed block carries its (p, value)s
        clean["percentiles"] = pct
        if item.get("approximate") is True:  # Athena is approximate
            clean["approximate"] = True
    _sanitize_grouped_into(item, clean)  # grouped shape + overall
    _sanitize_scope_into(item, clean)  # applied range, named sites, lineage whole?
    scan = _field(item.get("scan"), MAX_DETAIL_CHARS)
    if scan:  # only when the read did not run as defined in one query
        clean.update(scan=scan, scan_narrowed=item.get("scan_narrowed") is True)
        if item.get("scan_inexact") is True:
            clean["scan_inexact"] = True
    if clean["shape"] == "formula":
        windows = item.get("input_windows")
        clean["input_windows"] = _sanitize_input_windows(windows)
        clean["input_windows_truncated"] = bool(
            item.get("input_windows_truncated") is True or not isinstance(windows, list)
            or len(windows) > MAX_LIST_ITEMS
            or len(clean["input_windows"]) != len(windows))
    return clean


def sanitize_result(item: object) -> dict | None:
    """One result dict re-bounded field by field, or ``None`` when malformed."""
    if not isinstance(item, dict) or item.get("outcome") not in OUTCOMES:
        return None
    outcome = item["outcome"]
    clean = {"outcome": outcome, "term": _field(item.get("term"), mtr.MAX_TERM_CHARS)}
    if outcome in ("overflow", INPUT_OVERFLOW_OUTCOME):
        return clean if outcome == "overflow" else input_overflow_result()
    if outcome == "ambiguous_term":
        clean["candidates"] = [_field(c) for c in (item.get("candidates") or [])
                               [:MAX_LIST_ITEMS] if isinstance(c, str)]
        return clean if clean["candidates"] else None
    clean.update(metric_id=_field(item.get("metric_id")),
                 label=_field(item.get("label")),
                 source=_field(item.get("source"), 10))
    if not clean["metric_id"]:
        return None
    if outcome == "withheld":
        clean["status"] = _field(item.get("status"), 40) or "unavailable"
        clean["detail"] = _bounded_detail(item.get("detail"), clean["status"])
        return clean
    if outcome == "result_unavailable":
        clean["reason"] = _field(item.get("reason"), 60) or "unknown"
        return clean
    if outcome == NOT_IN_TIME_OUTCOME:
        return _sanitize_not_in_time(item, clean)
    if outcome == SITES_OUTCOME:
        from .metric_turn_sites import sanitize_sites
        return sanitize_sites(item, clean)
    return _sanitize_computed(item, clean)


def sanitize_results(raw: object) -> list[dict]:
    """The runtime receipt check for a PRESENT reserved key (the caller maps
    an ABSENT key to ``[]``). At most :data:`mtr.MAX_METRIC_TERMS` metric
    results plus ONE trailing ``overflow`` marker survive, re-bounded.

    Fails CLOSED: presence proves app-side matching named a defined metric,
    so a non-list, empty, oversize or cap+1-without-marker value becomes the
    generic :func:`invalid_result` alone, and any dropped item (malformed, or
    a non-trailing marker) adds it — never silence."""
    cap = mtr.MAX_METRIC_TERMS
    if not isinstance(raw, list) or not raw or len(raw) > cap + 1:
        return [invalid_result()]
    body, tail = raw, []
    if len(raw) == cap + 1 or (isinstance(raw[-1], dict)
                               and raw[-1].get("outcome") == "overflow"):
        body, tail = raw[:-1], raw[-1:]
        if not (isinstance(tail[0], dict) and tail[0].get("outcome") == "overflow"):
            return [invalid_result()]
    clean = [c for item in body if (c := sanitize_result(item)) is not None
             and c["outcome"] != "overflow"]
    marker = [c for item in tail if (c := sanitize_result(item)) is not None]
    dropped = len(body) + len(tail) - len(clean) - len(marker)
    return clean + ([invalid_result()] if dropped else []) + marker


# --------------------------------------------------------------------------- #
# Rendering (pure)
# --------------------------------------------------------------------------- #
def _fmt_value(value) -> str:
    return str(value) if isinstance(value, int) else format(value, ".6g")


def _input_token(window: dict) -> str:
    requested, applied = window.get("requested"), window.get("applied")
    return applied if requested and applied == requested else "lifetime"


def _formula_window(result: dict) -> tuple[str, str]:
    windows = result.get("input_windows") or []
    if not windows:
        return "mixed", "not reported for the inputs (derived formula)"
    tokens = [_input_token(w) for w in windows]
    listed = ", ".join(f"{w.get('input', 'input')}={t}" for w, t in zip(windows, tokens))
    if result.get("input_windows_truncated"):
        return "mixed", f"per input (derived formula): {listed}, and further inputs"
    if set(tokens) == {"lifetime"}:
        return "lifetime", "lifetime on every input (derived formula)"
    if len(set(tokens)) == 1:
        return "declared", f"{tokens[0]} on every input (declared; derived formula)"
    return "mixed", f"mixed across inputs (derived formula): {listed}"


def window_class_and_line(result: dict) -> tuple[str, str]:
    """``(class, text)`` — class ``declared`` / ``lifetime`` / ``mixed``
    drives the closing scope sentence; text is the block's window field."""
    if result.get("shape") == "formula":
        return _formula_window(result)
    rng = result.get("window_applied_range")
    if isinstance(rng, dict) and rng.get("start") and rng.get("end"):
        # an applied absolute [start, end) window is NOT lifetime, the
        # closing scope sentence must not call it lifetime.
        found = "declared", f"applied window [{rng['start']}, {rng['end']})"
        if result.get("scan_narrowed"):
            return "mixed", f"{found[1]}, but the value is NARROWED (see scan)"
        return found
    requested, applied = result.get("window_requested"), result.get("window_applied")
    if not requested:
        found = "lifetime", "lifetime (no window declared)"
    elif applied == requested:
        found = "declared", f"{requested} (declared)"
    else:
        found = "lifetime", (f"{requested} declared, applied as lifetime "
                             "(no event-time mapping on the source)")
    if result.get("scan_narrowed"):
        # The value covers only part of that window: say so in the window
        # field and let the closing sentence speak per block.
        return "mixed", f"{found[1]}, but the value is NARROWED (see scan)"
    return found


def _window_line(result: dict) -> str:
    return window_class_and_line(result)[1]


def _any_inexact(results) -> bool:
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("outcome") == "computed" and r.get("scan_inexact"):
            return True
        if r.get("outcome") == SITES_OUTCOME and any(
                isinstance(s, dict) and s.get("scan_inexact") for s in r.get("sites") or []):
            return True
    return False


def scope_sentence(results) -> str:
    """The closing sentence, conditional on the COMPUTED results' windows
    (a per-site result contributes each computed site's window), and on
    whether any of them is NOT EXACT."""
    sentence = _window_scope_sentence(results)
    return sentence + INEXACT_SENTENCE if _any_inexact(results) else sentence


def _any_site_approximate(results) -> bool:
    """A per-site result with an Athena (approximate) percentile site."""
    return any(isinstance(r, dict) and r.get("outcome") == SITES_OUTCOME
               and any(isinstance(s, dict) and s.get("approximate") for s in r.get("sites") or [])
               for r in results)


def _window_scope_sentence(results) -> str:
    from .metric_turn_sites import site_window_classes
    computed = [r for r in results
                if isinstance(r, dict) and r.get("outcome") == "computed"]
    # A grouped block already supplied the breakdown, and an Athena percentile
    # is approximate: neither may be closed with "exactly" / "breakdowns are not
    # available". Grouped wins (its text also covers approximation).
    if any(r.get("grouped") for r in computed):
        if any(r.get("approximate") for r in computed):
            return GROUPED_APPROX_SCOPE_SENTENCE
        return GROUPED_SCOPE_SENTENCE
    if any(r.get("approximate") for r in computed) or _any_site_approximate(results):
        return APPROXIMATE_SCOPE_SENTENCE
    classes = {window_class_and_line(r)[0] for r in computed}
    for r in results:
        if isinstance(r, dict) and r.get("outcome") == SITES_OUTCOME:
            classes |= site_window_classes(r)
    if not classes:
        return NO_VALUE_SCOPE_SENTENCE
    if classes == {"declared"}:
        return WINDOW_SCOPE_SENTENCE
    if classes == {"lifetime"}:
        return LIFETIME_SCOPE_SENTENCE
    return MIXED_SCOPE_SENTENCE


def _render_computed(r: dict, head: str) -> str:
    source = f"{r['source']}-defined" if r.get("source") else "defined"
    formula = r.get("shape") == "formula"
    scope = ("inputs resolved by the metric layer" if formula
             else f"authority system '{r.get('system_id', '')}'")
    exactness = " (NOT EXACT: see scan)" if r.get("scan_inexact") else ""
    unit = r.get("unit", "")
    grouped = r.get("grouped")
    if grouped:  # a grouped, day-windowed result
        head_part, scope_line = _grouped_head_and_scope(r, unit, exactness, scope)
    else:
        pct = r.get("percentiles")
        if pct:  # a duration-percentile block lists each p-value
            listed = ", ".join(f"p{_fmt_value(e['p'] * 100)}={_fmt_value(e['value'])} "
                               f"{unit}".rstrip() for e in pct)
            # Athena approx_percentile is an estimate, labelled here and
            # never described as exact; PostgreSQL percentile_disc carries no label.
            approx = " (approximate: Athena approx_percentile)" if r.get("approximate") else ""
            head_part = f"percentiles: {listed}{approx}{exactness}"
        else:
            head_part = f"value: {_fmt_value(r['value'])} {unit}".rstrip() + exactness
        scope_line = f"scope: {DEPLOYMENT_WIDE} scalar; {scope}"
    parts = [head_part,
             f"window: {_computed_window_line(r)}",
             scope_line,
             f"lineage: {r.get('lineage', '')}",
             f"queries: {r.get('query_count', 0)} across "
             f"{_field(', '.join(r.get('systems') or []), MAX_SYSTEMS_CHARS) or 'no system'}"]
    if not formula and not grouped and r.get("sample_count") is not None:
        parts.append(f"samples: {r['sample_count']}")
    if r.get("scan"):
        parts.append(f"scan: {r['scan']}")
    sites = r.get(NAMED_SITES_KEY) if not formula else None
    site_text = (NAMED_SITE_UNSCOPED_LINE if r.get(LINEAGE_INCOMPLETE_KEY)
                 else NAMED_SITE_LINE)
    site_line = ("\n" + site_text.format(sites=", ".join(sites))) if sites else ""
    permission = (NOT_AVAILABLE_LINE if formula or r.get(LINEAGE_INCOMPLETE_KEY)
                  else PERMITTED_LINE)
    return (f"{head} (\"{r.get('label', '')}\", {r.get('kind', 'direct')}, "
            f"{source}): COMPUTED.\n  " + " | ".join(parts) + "\n"
            + permission + site_line)


def _grouped_percentiles_text(pct, unit: str) -> str:
    return ", ".join(f"p{_fmt_value(e['p'] * 100)}={_fmt_value(e['value'])} "
                     f"{unit}".rstrip() for e in pct)


def _grouped_head_and_scope(r: dict, unit: str, exactness: str,
                            scope: str) -> tuple[str, str]:
    """The grouped head (every group + its samples + the overall row) and the
    grouped scope line. Athena is labelled approximate."""
    approx = " (approximate: Athena approx_percentile)" if r.get("approximate") else ""
    lines = []
    for g in r["grouped"]:
        lines.append(f"{g['group']}: {_grouped_percentiles_text(g['percentiles'], unit)} "
                     f"(samples: {g.get('sample_count')})")
    overall = r.get("overall") or {}
    if overall.get("percentiles"):
        lines.append(f"overall: {_grouped_percentiles_text(overall['percentiles'], unit)} "
                     f"(samples: {overall.get('sample_count')})")
    else:
        lines.append("overall: not computed this turn")
    head_part = f"percentiles by group{approx}{exactness}: " + "; ".join(lines)
    dim = r.get("dimension_concept", "the pinned dimension")
    return head_part, f"scope: {DEPLOYMENT_WIDE}, grouped by {dim}; {scope}"


def _computed_window_line(r: dict) -> str:
    """The window field for a computed block. A grouped/scalar result with an
    applied absolute range prints the exact ``[start, end)`` boundary;
    otherwise the declared-token / lifetime line as before."""
    rng = r.get("window_applied_range")
    if isinstance(rng, dict) and rng.get("start") and rng.get("end"):
        return f"applied window [{rng['start']}, {rng['end']})"
    return _window_line(r)


def _render_overflow(r: dict) -> str:
    cap = mtr.MAX_METRIC_TERMS
    return (f"- The question names more than {cap} defined metrics: '{r.get('term', '')}' "
            "and any further defined metric are NOT resolved by the metric layer this "
            "turn. Give NO number for any defined metric not listed above — ask the "
            f"user to narrow the request to at most {cap} metrics.")


def _render_block(r: dict) -> str:
    outcome = r.get("outcome")
    if outcome == "overflow":
        return _render_overflow(r)
    if outcome == "ambiguous_term":
        listed = ", ".join(f"'{c}'" for c in r.get("candidates") or [])
        return (f"- '{r.get('term', '')}' matches more than one defined metric: "
                f"{listed}. Not resolved — ask which is meant.")
    head = f"- '{r.get('term', '')}' → metric '{r.get('metric_id', '')}'"
    if outcome == "computed":
        return _render_computed(r, head)
    if outcome == SITES_OUTCOME:
        from .metric_turn_sites import render_sites_block
        return render_sites_block(r, head)
    if outcome == "withheld":
        return (f"{head} (\"{r.get('label', '')}\"): WITHHELD — status "
                f"'{r.get('status', '')}'.\n  reason: {r.get('detail', '')}")
    if outcome == NOT_IN_TIME_OUTCOME:
        return _render_not_in_time(r, head)
    return (f"{head} (\"{r.get('label', '')}\"): METRIC-LAYER RESULT UNAVAILABLE "
            f"THIS TURN ({r.get('reason', 'unknown')}).")


def _render_not_in_time(r: dict, head: str) -> str:
    """A value the metric layer did not compute: over its time budget in an
    exact split (unchanged), or a refused query inside an explicit window,
    for which the system agent may run ONE query in that window applying the
    stated definition, or (definition not whole) nothing at all."""
    label, system = r.get("label", ""), r.get("system_id", "")
    lineage = f"  lineage: {r.get('lineage', '')}"
    if r.get("computation") not in ONE_QUERY_KINDS:
        return (f"{head} (\"{label}\"): NOT COMPUTED IN THE METRIC LAYER'S "
                f"TIME BUDGET THIS TURN: it is over the per-query scan limit, and the system "
                f"agent of '{system}' can compute it exactly.\n{lineage}\n{NOT_IN_TIME_LINE}")
    rng = r.get("window_applied_range") or {}
    window = f"  window: applied window [{rng.get('start', '')}, {rng.get('end', '')})"
    lead = (f"{head} (\"{label}\"): NOT COMPUTED BY THE METRIC LAYER THIS TURN: its query "
            "inside the window below is over the per-query scan limit")
    if r.get(LINEAGE_INCOMPLETE_KEY):
        return (f"{lead}, and its whole definition could not be carried.\n{window}\n"
                f"{lineage}\n{NOT_IN_TIME_NO_PERMISSION_LINE}")
    return (f"{lead}; the system agent of '{system}' can compute it with one query.\n"
            f"{window}\n{lineage}\n{NOT_IN_TIME_ONE_QUERY_LINE}")


REQUIRED_RESPONSE = (
    "REQUIRED RESPONSE:\n"
    "- COMPUTED ⇒ report THAT value, its unit, its window and its scope as the "
    "metric's value; never recompute it from raw columns or another metric. A "
    "narrower scope or another period follows the block's own PERMITTED / NOT "
    "AVAILABLE line.\n"
    "- WITHHELD or UNAVAILABLE ⇒ state the status and the reason verbatim and "
    "give NO number for that metric: do not compute, estimate, or illustrate a "
    "substitute from raw columns, a related ratio, or a neighbouring metric — "
    "not even labelled as approximate or \"for illustration\"; write NO formula, "
    "scale factor, example or placeholder number for it either. When the reason "
    "names an unmapped concept, say the gap is an onboarding/mapping gap in the "
    "connected systems.\n"
    "- AMBIGUOUS ⇒ ask which of the listed metric ids is meant.\n"
    "If the question's use of a term is clearly not this metric, disregard that "
    "term's block and do not mention it in the answer — but never compute a "
    "quantity by that name outside the metric layer.")

#: Rides the notice whenever a block gives no number. Measured: a WITHHELD
#: block read as the answer to the whole question, so the part asking how the
#: term is decided was never asked of anyone. The number stays withheld.
WITHHELD_SCOPE_LINE = (
    "A WITHHELD or UNAVAILABLE block withholds only that metric's number, not the "
    "rest of the question: a part that asks how the term is defined, decided or "
    "measured is still answered, from the concept notes or by asking the system "
    "that holds the definition. Its number stays withheld.")
_NO_NUMBER_OUTCOMES = frozenset({"withheld", "result_unavailable", INVALID_OUTCOME})


def _no_number_not_in_time(r: dict) -> bool:
    """A refused windowed query whose definition is not whole: no number."""
    return (r.get("outcome") == NOT_IN_TIME_OUTCOME
            and r.get("computation") in ONE_QUERY_KINDS
            and r.get(LINEAGE_INCOMPLETE_KEY) is True)

_INTRO = ("The metric layer resolved the defined metric(s) this question names "
          "BEFORE this turn. These results are computed, not suggestions:")


def render_metric_layer_notice(results) -> str:
    """The notice for ``results`` (``""`` when empty), ≤ :data:`MAX_NOTICE_CHARS`.
    Blocks drop from the TAIL, never mid-block; the header, the invalid,
    input-overflow and overflow markers (when present), the REQUIRED RESPONSE section, the
    never-substitute sentence and the (window-conditional) scope sentence
    always survive. Pure."""
    items = [r for r in (results or []) if isinstance(r, dict)]
    if not items:
        return ""
    outcomes = {r.get("outcome") for r in items}
    pinned = [INVALID_LINE] if INVALID_OUTCOME in outcomes else []
    pinned += [INPUT_OVERFLOW_LINE] if INPUT_OVERFLOW_OUTCOME in outcomes else []
    pinned += [_render_overflow(r) for r in items if r.get("outcome") == "overflow"][:1]
    scope = [WITHHELD_SCOPE_LINE] if (outcomes & _NO_NUMBER_OUTCOMES
                                      or any(map(_no_number_not_in_time, items))) else []
    tail = "\n".join([*pinned, REQUIRED_RESPONSE, *scope, mtr.NEVER_SUBSTITUTE_SENTENCE,
                      scope_sentence(items)])
    blocks = [_render_block(item) for item in items
              if item.get("outcome") not in _PINNED_TAIL_OUTCOMES]
    kept = list(blocks)
    while kept and len(_join(kept, len(blocks) - len(kept), tail)) > MAX_NOTICE_CHARS:
        kept.pop()
    return _join(kept, len(blocks) - len(kept), tail)


def _join(blocks: list[str], dropped: int, tail: str) -> str:
    extra = ([f"- {dropped} further result(s) omitted by the notice bound — give "
              "NO number for those metrics."] if dropped else [])
    return "\n".join([METRIC_LAYER_HEADER, _INTRO, *blocks, *extra, tail])


#: The static notice for a renderer fault: still the header and the
#: forbidding rules — a render failure never degrades to silence.
FALLBACK_NOTICE = "\n".join([METRIC_LAYER_HEADER, INVALID_LINE, REQUIRED_RESPONSE,
                             WITHHELD_SCOPE_LINE, mtr.NEVER_SUBSTITUTE_SENTENCE,
                             NO_VALUE_SCOPE_SENTENCE])


def render_or_fallback(results) -> str:
    """:func:`render_metric_layer_notice`, or :data:`FALLBACK_NOTICE` when it
    raises (``""`` only for no results at all)."""
    if not results:
        return ""
    try:
        return render_metric_layer_notice(results) or FALLBACK_NOTICE
    except Exception:  # noqa: BLE001 - render fault fails CLOSED to the static notice
        return FALLBACK_NOTICE
