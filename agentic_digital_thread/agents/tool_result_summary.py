# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Single source of truth for tool-result summarization.

Renders a short, human-readable one-line summary of a tool result (Athena /
SiteWise / Neptune / schema / generic tabular payloads) for the activity trace.
Every consumer (``chat_client.py`` included) imports this ONE implementation so
the error/shape logic never drifts.

Two rules the implementation holds:

* **No substring "error" false positives.** Scanning the RAW result text for
  the substrings ``ValidationException`` / ``Exception`` / ``Error`` /
  ``error`` and labeling ANY match a "Query error" mislabels valid Athena/SiteWise
  payloads whose *data* legitimately contains those words (a ``segment_state``
  value of ``"error"``, an ``error_code`` column, ``"error": null`` in a status
  envelope, a failure-code row value) even though the rows are present and used
  in the answer. Error detection is therefore STRUCTURAL: a payload is
  an error only when it is structurally an error envelope (a truthy top-level
  ``error`` / ``__error__`` key, an i3X ``{"success": false, "responseDetail":
  ...}`` envelope, or the tool result is flagged failed) — never because some
  data value contains the text "error". A successful result set (columns/rows,
  or any list/dict of data) is NEVER an error.

* **No opaque ``"Error processing results."`` fallback.** A bare ``except`` that
  swallows every unexpected shape into a useless "Error processing results."
  string hides real data. The fallback is best-effort: recognized shapes (including
  ``sitewise_list_associated_assets``' list-of-assets) summarize normally, and any
  genuinely unexpected shape yields a short key/row-count summary of the parsed
  data instead of a fake error.

i3X / chart / claim-check shapes (all VALID payloads — never labeled errors):

* **i3X envelope-list** — a list of per-elementId ``{"elementId", "result"}``
  wrappers (``objects/history`` / ``objects/value`` responses after the client
  strips the outer success envelope) summarizes as element/channel/point
  counts instead of generic truncation.
* **i3X error envelope** — ``{"success": false, "responseDetail": {...}}`` IS
  a structural error; its title/status/detail are surfaced in the summary.
* **Claim-check** — a capped history payload (``_stats`` + ``dataset_ref`` +
  ``landmark_values``, see ``system_agent._cap_history_payload``) summarizes
  as its stats, noting the full series was withheld.
* **Chart-bearing results** — a result carrying pre-rendered
  ``<chart-block .../>`` tags (a chart tool, the i3X guaranteed-chart suffix,
  or a system answer with relayed charts on the local pre-redaction path)
  summarizes by its other text: a leading JSON value by its own shape, prose
  as itself. Only a result that is nothing but charts summarizes as
  "Rendered N chart(s).". The tags themselves (and any cut-off tag fragment)
  are NEVER echoed into the activity trace. The same holds for the note the
  ChartRedactionHook leaves in their place for the model (``chart_note``):
  the cloud stream summarizes the redacted message, so the note is removed
  like the tags and counted the same way. A reload summarizes the stored
  copy, which keeps the tags (memory stores the message before the hook).
"""
from __future__ import annotations

import json
import re
from typing import Any

from .chart_note import strip_chart_notes
from .internal_lines import public_text

# Sentinel so a present-but-``None`` key (e.g. ``"error": null`` in a status
# envelope) is distinguishable from an absent key.
_MISSING = object()

# Top-level keys that mark a payload as an ERROR envelope when their value is
# truthy. A ``None``/empty value (``"error": null``) is data, not a failure.
_ERROR_ENVELOPE_KEYS = ("error", "__error__")

# Metadata a SiteWise history result carries beside its series: the source
# block, the sampling summary and the requested window. Only these keys are
# skipped when counting series; any other key is part of the shape check.
_HISTORY_METADATA_KEYS = frozenset({"_adt_source", "_sampling", "_window"})

# Complete self-closing chart tags only — a truncated ``<chart-block ...`` (no
# ``/>``) never matches. Kept local (chat_client defines its own copy for the
# stream-injection seam; importing it from there would be circular).
_CHART_TAG_RE = re.compile(r"<chart-block\b[^>]*/>")


def _extract_raw_text(result: Any) -> str:
    """Pull the first text block out of a tool result's ``content`` list."""
    if not isinstance(result, dict):
        return ""
    content = result.get("content", []) or []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            return block["text"]
    return ""


def _looks_like_result_set(parsed: Any) -> bool:
    """True if the parsed payload is a successful DATA result set — which is NEVER
    an error, even if a data value or column happens to contain the word 'error'."""
    if isinstance(parsed, list):
        return True
    if isinstance(parsed, dict):
        if parsed.get("columns") or parsed.get("rows") or parsed.get("row_count"):
            return True
        if "results" in parsed:  # Neptune ``{"results": [...]}``
            return True
    return False


def _is_error_payload(parsed: Any, result: Any) -> bool:
    """STRUCTURAL error detection (no substring scan of data text).

    A payload is an error only when:
      * the tool result itself is flagged failed (``isError`` / ``status ==
        'error'``), OR
      * the parsed payload is an i3X error envelope (``success: false`` WITH a
        ``responseDetail`` — the exact wire shape; ``success: false`` alone is
        left to the best-effort summary, never guessed into an error), OR
      * the parsed payload is a dict that is NOT a data result set and carries a
        TRUTHY top-level ``error`` / ``__error__`` key.
    """
    if isinstance(result, dict):
        if result.get("isError") is True or result.get("status") == "error":
            return True
    if isinstance(parsed, dict):
        # A genuine result set is never an error, even if it also carries an
        # 'error' column/field whose value is a data value or null.
        if _looks_like_result_set(parsed):
            return False
        if parsed.get("success") is False and "responseDetail" in parsed:
            return True
        for k in _ERROR_ENVELOPE_KEYS:
            v = parsed.get(k, _MISSING)
            if v is not _MISSING and v not in (None, "", [], {}, False):
                return True
    return False


def _i3x_error_reason(parsed: dict) -> str:
    """Surface an i3X error envelope's ``responseDetail`` (title/status/detail)."""
    if parsed.get("success") is not False or "responseDetail" not in parsed:
        return ""
    detail = parsed.get("responseDetail")
    if not isinstance(detail, dict):
        return str(detail) if detail else "i3X request failed"
    title = str(detail.get("title") or "Error")
    status = detail.get("status")
    message = str(detail.get("detail") or detail.get("message") or "")
    head = f"{title} ({status})" if status not in (None, "") else title
    return f"{head}: {message}" if message else head


def _error_reason(parsed: Any) -> str:
    """Best-effort human reason from a structural error payload."""
    if isinstance(parsed, dict):
        i3x_reason = _i3x_error_reason(parsed)
        if i3x_reason:
            return i3x_reason
        for k in _ERROR_ENVELOPE_KEYS:
            err = parsed.get(k)
            if err in (None, "", [], {}, False):
                continue
            if isinstance(err, dict):
                return str(
                    err.get("message") or err.get("error")
                    or err.get("detail") or err
                )
            return str(err)
        reason = parsed.get("message") or parsed.get("detail") or ""
        if reason:
            return str(reason)
    return ""


def _best_effort_summary(parsed: Any, raw: str) -> str:
    """Short summary of an otherwise-unrecognized parsed shape — key/row counts
    rather than a fake error. Never raises."""
    try:
        if isinstance(parsed, list):
            return f"{len(parsed)} item{'s' if len(parsed) != 1 else ''}"
        if isinstance(parsed, dict):
            keys = list(parsed.keys())
            if not keys:
                return "Empty result."
            preview = ", ".join(str(k) for k in keys[:5])
            suffix = f" +{len(keys) - 5} more" if len(keys) > 5 else ""
            return f"Result with {len(keys)} field{'s' if len(keys) != 1 else ''}: {preview}{suffix}"
        if parsed is not None:
            return str(parsed)[:200]
    except Exception:  # nosec B110 - best-effort formatting, fall through to raw
        pass
    return raw[:200] if raw else "Result received."


# ── i3X / chart / claim-check shapes ─────────────────────────────────────────


def _chart_count(raw: str) -> int:
    """Number of COMPLETE self-closing ``<chart-block .../>`` tags in the raw text."""
    if not raw or "<chart-block" not in raw:
        return 0
    return len(_CHART_TAG_RE.findall(raw))


def _charts_only_summary(rendered: int, withheld: int) -> str:
    """The trace line for a result that was nothing but charts: rendered
    ones as a count, and charts integrity-enforce mode withheld never as
    rendered."""
    parts = []
    if rendered:
        parts.append(f"Rendered {rendered} chart(s).")
    if withheld:
        parts.append(f"{withheld} chart(s) withheld.")
    return " ".join(parts)


#: A complete chart tag, or an unterminated one cut off at the end.
_CHART_FRAGMENT_RE = re.compile(r"<chart-block\b[^>]*>?")


def _text_beside_charts(raw: str) -> str:
    """What a chart-bearing result says besides its charts: every chart tag
    and tag fragment removed (never echoed), and, when the rest opens with a
    JSON value, that value alone (the i3X guaranteed-chart suffix rides after
    claim-checked JSON). A lead-in colon left before the removed tags goes
    too. ``""`` when the result is nothing but charts."""
    rest = _CHART_FRAGMENT_RE.sub("", raw).strip()
    if rest[:1] in ("{", "["):
        try:
            return rest[:json.JSONDecoder().raw_decode(rest)[1]]
        except ValueError:
            pass
    return rest[:-1].rstrip() if rest.endswith(":") else rest


def _is_claim_check(parsed: Any) -> bool:
    """True for the capped-history claim-check shape (``_cap_history_payload``):
    a dict carrying computed ``_stats`` plus a ``dataset_ref`` handle."""
    return (
        isinstance(parsed, dict)
        and isinstance(parsed.get("_stats"), dict)
        and isinstance(parsed.get("dataset_ref"), str)
        and bool(parsed.get("dataset_ref"))
    )


def _summarize_claim_check(parsed: dict) -> str:
    """Stats summary for a claim-checked series — the full series was withheld
    from the model on purpose; never echo landmark points back out."""
    stats = parsed.get("_stats") or {}
    bits = []
    if stats.get("count") is not None:
        bits.append(f"{stats['count']} point(s)")
    for key in ("min", "max", "mean"):
        if stats.get(key) is not None:
            bits.append(f"{key} {stats[key]}")
    head = ", ".join(bits) if bits else "stats available"
    return f"Series stats: {head} — full series withheld ({parsed['dataset_ref']})"


def _claim_check_points(payload: dict) -> int:
    """Point count recorded in a claim-check's ``_stats`` (0 when malformed)."""
    try:
        return int((payload.get("_stats") or {}).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _count_i3x_channels(inner: Any) -> tuple[int, int]:
    """``(channels, points)`` for one wrapper's inner ``result`` payload.

    Telemetry shapes counted (``_cap_history_payload`` replaces a long VQT
    list IN PLACE UNDER ITS KEY, so the capped wire shapes keep ``values`` /
    component entries as claim-check DICTS — they must count the same as the
    live lists they replaced):

    * ``values`` as a VQT list (uncapped history) — points = list length;
    * ``values`` as a claim-check dict (capped history) — points = ``_stats.count``;
    * a bare claim-check dict (server returned the VQT list directly);
    * a single live VQT (``value``/``quality``);
    * ``components`` (maxDepth=0 composition) — each component counted the
      same way, recursively; may coexist with the element's own ``values``.
    """
    if not isinstance(inner, dict):
        return 0, 0
    channels = points = 0
    values = inner.get("values")
    if isinstance(values, list):
        channels, points = 1, len(values)
    elif isinstance(values, dict) and isinstance(values.get("_stats"), dict):
        channels, points = 1, _claim_check_points(values)
    elif isinstance(inner.get("_stats"), dict):
        channels, points = 1, _claim_check_points(inner)
    elif "value" in inner or "quality" in inner:
        channels, points = 1, 1
    components = inner.get("components")
    if isinstance(components, dict):
        for comp in components.values():
            c, p = _count_i3x_channels(comp)
            channels += c
            points += p
    return channels, points


def _summarize_i3x_envelope_list(parsed: list) -> str:
    """Element/channel/point counts for a list of per-elementId i3X wrappers.

    A channel is a wrapper whose inner ``result`` carries telemetry (see
    ``_count_i3x_channels`` for the live AND claim-check-capped shapes).
    Relationship wrappers (inner ``result`` is a list) count as elements only.
    """
    channels = 0
    points = 0
    for entry in parsed:
        inner = entry.get("result") if isinstance(entry, dict) else None
        c, p = _count_i3x_channels(inner)
        channels += c
        points += p
    return f"{len(parsed)} element(s), {channels} channel(s), {points} point(s)"


def _summarize_i3x_shape(parsed: Any) -> str | None:
    """i3X wrapper-list and claim-check shapes; ``None`` when not i3X-shaped."""
    if (
        isinstance(parsed, list) and parsed and isinstance(parsed[0], dict)
        and "elementId" in parsed[0] and "result" in parsed[0]
    ):
        return _summarize_i3x_envelope_list(parsed)
    if _is_claim_check(parsed):
        return _summarize_claim_check(parsed)
    return None


# ── Recognized result shapes (extracted verbatim from the one summarizer) ────

_NEPTUNE_SCHEMA_FIELDS = {"batch_number", "material_lot", "id", "name", "type", "label",
                          "domain", "description", "status", "field", "property"}


def _neptune_is_schema_row(r: dict) -> bool:
    vals = [v for v in r.values() if v is not None]
    return all(isinstance(v, str) and v.lower() in _NEPTUNE_SCHEMA_FIELDS for v in vals)


def _neptune_row_parts(r: dict) -> list:
    parts = []
    for v in r.values():
        if isinstance(v, dict):
            props = v.get("~properties", v)
            label = v.get("~labels", [])
            if isinstance(label, list):
                label = label[0] if label else ""
            # Show meaningful properties for each node type
            name = props.get("name") or props.get("id") or v.get("name") or v.get("id") or ""
            # For IdentityRules/JoinPaths, show the relationship info
            from_sys = props.get("from_system") or props.get("source_system") or ""
            to_sys = props.get("to_system") or props.get("target_system") or ""
            concept = props.get("concept") or ""
            if from_sys and to_sys:
                parts.append(f"{label}: {from_sys} → {to_sys}" + (f" ({concept})" if concept else ""))
            else:
                domain = props.get("domain") or props.get("type") or ""
                parts.append(f"{label}: {name}" + (f" ({domain})" if domain else ""))
        elif v is not None:
            parts.append(str(v)[:80])
    return parts


def _summarize_neptune(parsed: Any) -> str:
    # A failure envelope (explore_graph's ``query_failed`` marker or an
    # ``error``) is a FAILED call: never summarize its empty ``results`` as
    # "No matching entities found."
    if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("query_failed") is True):
        return f"Error: {str(parsed.get('error') or 'Graph query failed')[:240]}"
    results = parsed.get("results", []) if isinstance(parsed, dict) else []
    if not results:
        return "No matching entities found."
    results = [r for r in results if not _neptune_is_schema_row(r)]
    if not results:
        return "No matching entities found."
    lines = []
    for r in results[:8]:  # show up to 8 entities
        parts = _neptune_row_parts(r)
        lines.append(" — ".join(p for p in parts if p))
    suffix = f" (+{len(results) - 8} more)" if len(results) > 8 else ""
    return "\n".join(lines) + suffix


def _is_schema_result(tool_name: str, parsed: Any) -> bool:
    """get_system_schema returns a dict of table_name -> {columns, concept_map}.
    Only consulted AFTER the Neptune check (preserves the original ordering)."""
    if tool_name == "get_system_schema":
        return True
    return (
        isinstance(parsed, dict)
        and len(parsed) > 0
        and all(
            isinstance(v, dict) and "columns" in v
            for v in list(parsed.values())[:2]
        )
    )


def _summarize_schema(parsed: dict) -> str:
    tables = list(parsed.keys())
    col_counts = [len(parsed[t].get("columns", [])) for t in tables[:3]]
    summary = ", ".join(f"{t}({c} cols)" for t, c in zip(tables[:3], col_counts))
    suffix = f" +{len(tables)-3} more" if len(tables) > 3 else ""
    return f"Schema: {len(tables)} tables — {summary}{suffix}"


def _summarize_sitewise_shape(parsed: Any) -> str | None:
    """SiteWise tool results — detect by shape; ``None`` when not recognized."""
    # sitewise_list_associated_assets returns
    # {assets:[{id,name,...,depth,path}], truncated, reason, direction, root}
    # plus status/partial_note/skipped_count when a cap or failed read cut it.
    if isinstance(parsed, dict) and isinstance(parsed.get("assets"), list) \
            and "truncated" in parsed:
        assets = parsed["assets"]
        names = [str(a.get("name", a.get("id", "")))[:30]
                 for a in assets[:3] if isinstance(a, dict)]
        more = f" +{len(assets) - 3} more" if len(assets) > 3 else ""
        trunc = ""
        if parsed.get("truncated"):
            reason = parsed.get("reason") or "cap"
            skipped = parsed.get("skipped_count") or 0
            unread = f", {skipped} branch(es) unread" if skipped else ""
            trunc = f" (partial: {reason}{unread})"
        return (f"{len(assets)} associated assets: "
                f"{', '.join(n for n in names if n)}{more}{trunc}")
    # sitewise_get_site_processes returns {"site":..., "area":..., "line":..., "processes":{...}}
    if isinstance(parsed, dict) and "processes" in parsed and isinstance(parsed.get("processes"), dict):
        procs = parsed["processes"]
        names = list(procs.keys())[:4]
        suffix = f" +{len(procs)-4} more" if len(procs) > 4 else ""
        return f"{len(procs)} process assets: {', '.join(names)}{suffix}"
    # sitewise_get_asset_property_history returns {assetId_propId: [{value, timestamp}...]};
    # the source block and sampling summary beside the series are metadata.
    series = ({k: v for k, v in parsed.items() if k not in _HISTORY_METADATA_KEYS}
              if isinstance(parsed, dict) else {})
    if series and all(
        isinstance(v, (list, dict)) for v in list(series.values())[:3]
    ) and not any(k in parsed for k in ("columns", "rows", "results", "error")):
        series_count = len(series)
        total_pts = sum(len(v) if isinstance(v, list) else 0 for v in series.values())
        return f"{series_count} time-series: {total_pts} data points total"
    # sitewise_describe_asset / sitewise_list_assets / sitewise_list_asset_models /
    # sitewise_list_associated_assets — all return a JSON list of asset dicts.
    if isinstance(parsed, list):
        if parsed and isinstance(parsed[0], dict):
            names = [str(item.get("name", item.get("id", "")))[:30] for item in parsed[:3]]
            suffix = f" +{len(parsed)-3} more" if len(parsed) > 3 else ""
            return f"{len(parsed)} items: {', '.join(n for n in names if n)}{suffix}"
        return f"{len(parsed)} items"
    # Single asset describe result
    if isinstance(parsed, dict) and ("assetId" in parsed or "id" in parsed):
        name = parsed.get("assetName") or parsed.get("name") or parsed.get("id", "")
        props = parsed.get("properties", [])
        return f"Asset: {name} ({len(props)} properties)"
    return None


_TABULAR_ID_PREFERRED = {"batch_number", "order_id", "wo_id", "name", "product_id",
                         "material_id", "asset_id", "supplier_id", "alert_id"}
_TABULAR_DETAIL_PREFERRED = {"status", "result", "description", "wo_type", "type",
                             "due_date", "grade", "rating", "priority", "site", "line",
                             "product_name", "company_name", "supplier_code", "item_code",
                             "order_number", "work_order_number", "run_number", "lot_number"}


def _summarize_tabular(parsed: dict) -> str:
    """Tabular result set (Athena / SQL): columns + rows."""
    cols = parsed.get("columns", [])
    rows = parsed.get("rows", [])
    row_count = parsed.get("row_count", len(rows))
    if not cols or not rows:
        return "No records found."
    cols_lower = [c.lower() for c in cols]
    id_col = next((i for i, c in enumerate(cols_lower) if c in _TABULAR_ID_PREFERRED), 0)
    # If the ID column value looks like a UUID, try to find a more readable column
    if rows and id_col < len(rows[0]):
        sample_val = str(rows[0][id_col])
        if len(sample_val) == 36 and sample_val.count('-') == 4:
            # UUID detected — find a better display column
            alt_col = next((i for i, c in enumerate(cols_lower)
                           if c in _TABULAR_DETAIL_PREFERRED and i < len(rows[0])
                           and str(rows[0][i]) and len(str(rows[0][i])) < 36), None)
            if alt_col is not None:
                id_col = alt_col
    detail_cols = [i for i, c in enumerate(cols_lower) if c in _TABULAR_DETAIL_PREFERRED and i != id_col][:2]

    def _fmt_row(row: list) -> str:
        ident = str(row[id_col])[:40] if row else ""
        details = ", ".join(str(row[i])[:30] for i in detail_cols if i < len(row))
        return f"{ident} ({details})" if details else ident

    previews = ", ".join(_fmt_row(r) for r in rows[:4])
    suffix = "..." if row_count > 4 else ""
    s = "s" if row_count != 1 else ""
    return f"{row_count} record{s} found: {previews}{suffix}"


def _dispatch_shape(tool_name: str, parsed: Any, raw: str) -> str:
    """Route a non-error parsed payload to its shape-specific summary."""
    # i3X wrapper lists and claim-checked series (valid data, never errors).
    i3x_summary = _summarize_i3x_shape(parsed)
    if i3x_summary is not None:
        return i3x_summary
    # Detect Neptune result by shape (tool_name may be empty for AgentCore stream events)
    if tool_name == "explore_graph" or (
        "results" in parsed and "columns" not in parsed and "rows" not in parsed
    ):
        return _summarize_neptune(parsed)
    if _is_schema_result(tool_name, parsed):
        return _summarize_schema(parsed)
    sitewise_summary = _summarize_sitewise_shape(parsed)
    if sitewise_summary is not None:
        return sitewise_summary
    if isinstance(parsed, dict) and any(
        k in parsed for k in ("columns", "rows", "row_count")
    ):
        return _summarize_tabular(parsed)
    # Unrecognized shape — best-effort short summary (never a fake error).
    return _best_effort_summary(parsed, raw)


def _without_engine_note(parsed: Any) -> Any:
    """The answer's own shape, without the engine's cut-note annotation.

    A Mode A answer that is one JSON object carries the engine line as the
    reserved member ``RESULT_CUT_NOTE_KEY``; one JSON array is wrapped as
    ``{RESULT_CUT_ITEMS_KEY: [...], RESULT_CUT_NOTE_KEY: line}``
    (``system_agent._with_result_cut_fact``). The note is not data, so shape
    dispatch ignores it and the wrapper is unwrapped: the summary of an
    annotated answer is the summary of the answer itself.
    """
    from .tool_result_capture import RESULT_CUT_ITEMS_KEY, RESULT_CUT_NOTE_KEY
    if not isinstance(parsed, dict) or RESULT_CUT_NOTE_KEY not in parsed:
        return parsed
    rest = {key: value for key, value in parsed.items() if key != RESULT_CUT_NOTE_KEY}
    if set(rest) == {RESULT_CUT_ITEMS_KEY} and isinstance(rest[RESULT_CUT_ITEMS_KEY], list):
        return rest[RESULT_CUT_ITEMS_KEY]
    return rest


def _keep_no_data_line(summary: str, raw: str) -> str:
    """The summary, with the engine no-data line appended when the result
    carries one the summary lost (``query_intelligence.no_data_line_in``).

    The trace summary is what the UI classifies a call's outcome from. A long
    prose answer is cut at 2,000 characters and a JSON answer's engine note
    is dropped from its shape summary, and either can lose the line that says
    no query the call ran returned data; the call then shows as answered.
    """
    from .query_intelligence import no_data_line_in
    line = no_data_line_in(raw)
    if not line or line in summary:
        return summary
    return f"{summary}\n\n{line}" if summary else line


def summarize_tool_result(tool_name: str, result: Any) -> str:
    """Render a short one-line summary of a tool result for the activity trace.

    The structural summary (``_structural_summary``), keeping the engine
    no-data line when the result carries one (``_keep_no_data_line``).
    """
    summary = _structural_summary(tool_name, result)
    try:
        return _keep_no_data_line(summary, _extract_raw_text(result))
    except Exception:  # nosec B110 - never break a render over the engine line
        return summary


def _structural_summary(tool_name: str, result: Any) -> str:
    """Render a short one-line summary of a tool result for the activity trace.

    Structural (not substring) error detection; robust best-effort fallback for
    unexpected shapes. See module docstring for the defects this fixes and the
    i3X / chart / claim-check shapes handled.
    """
    raw = ""
    parsed: Any = None
    try:
        # The orchestrator-only engine lines are not for the activity trace.
        raw = public_text(_extract_raw_text(result))

        # Chart-bearing results (a chart tool, the i3X guaranteed-chart suffix,
        # a system answer with relayed charts on the local pre-redaction path)
        # are summarized by their other text; the bare chart count only when
        # nothing else remains. NEVER echo the tags. A result the
        # ChartRedactionHook already redacted carries its note to the model
        # instead of the tags; the note is removed the same way.
        raw, noted, withheld = strip_chart_notes(raw)
        charts = _chart_count(raw) + noted
        if charts or withheld:
            raw = _text_beside_charts(raw)
            if not raw:
                return _charts_only_summary(charts, withheld)

        try:
            parsed = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            # Not JSON — return the raw text (never a fake error).
            return raw[:2000] if raw else "No results."
        parsed = _without_engine_note(parsed)

        # STRUCTURAL error detection — a data value containing 'error' is NOT a
        # failure; only a structural error envelope / failed flag is.
        if _is_error_payload(parsed, result):
            reason = _error_reason(parsed)
            return f"Query error — {reason[:120]}" if reason else "Query error."

        return _dispatch_shape(tool_name, parsed, raw)

    except Exception:  # nosec B110 - never surface a fake error; degrade to best-effort
        return _best_effort_summary(parsed, raw)


# Back-compat alias: the historical private name used across the codebase.
_summarize_tool_result = summarize_tool_result
