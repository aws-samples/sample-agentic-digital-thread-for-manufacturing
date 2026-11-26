# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic telemetry-coverage lines for the chat trace (pure home).

One short line per history channel the system agent read, e.g.::

    Telemetry read: <asset> / <property>, <start> to <end>, 24 points
    (samples <first> to <last>)

Computed from the tool call's INPUT (the requested window) and its
post-claim-check RESULT (``_cap_history_payload`` stats / short VQT lists /
the SiteWise (Direct) outcome map) — the same bytes the inner agent already
received, so nothing new is read and no value is derived. The lines carry
names, counts and timestamps ONLY (claim-check policy): stats min/max/mean,
landmark values and raw point values never reach a line.

OUT-OF-BAND ONLY: the capture hook (``tool_result_capture``) records lines
into ``invocation_state``; ``system_agent._write_investigation_trace`` stores
them in the W6-F2 investigation-trace entry; the ECS cloud stream
(``chat_client``) reads them back by the D3 ``(session_id, query_ref)`` join
and prefixes the ``query_<id>`` trace summary. They never enter the Mode A
answer, a tool result, or any LLM token path.

Samples: the same walk also records, per series, when a read returned fewer
values than it read (the SiteWise (Direct) reader's ``_sampling`` counts, or
an i3X claim-check's landmark points against its statistics count).
:func:`sample_line` renders those as ONE engine line that
``system_agent._append_engine_facts`` appends to the Mode A answer, so the
orchestrator learns a list of readings is a sample. It carries series names
and the two counts only, never a value, and names that are not plain ids are
withheld (the caller passes the name test). A series read again over the same
fixed window is one record, the latest read's: inside a fixed window the
stored count can only grow, so a later complete read counts at least every
value an earlier one did. A partial read counts only the values read before
its cut, so it never replaces a complete read of that window. A trailing
window moves with the clock, so each read of it stays its own record.
Records are admitted one at a time, so two reads of one series and window in
one call merge too. Reads past ``MAX_SAMPLE_RECORDS`` are counted, a
fixed-window read once however often it repeats, and the line says what it
counts.
The system runtime also stores the line in the call's investigation-trace
entry, and the trace steps show it from there, as their own field below the
summary (:func:`without_sample_copy` drops the summary's own copy), never
from a tool result's text; :func:`is_sample_line` is the line's one parser,
which the writer and the store both check it with. :func:`neutralize` makes
a copy of the line in model text inert before the engine appends its own.

Honest data: zero points ⇒ ``0 points (no data)`` (never omitted), or
``0 points (data starts <ts>)`` when the Direct reader proved the window
predates the property's first stored value; an unknown/unparseable window is
OMITTED (never guessed). Stdlib-pure (the ``investigation_trace`` posture —
import purity pinned); never raises.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

#: Reserved ``invocation_state`` key (``_adt_`` family — ADR-0049 spelling).
TELEMETRY_COVERAGE_STATE_KEY = "_adt_telemetry_coverage"
#: Reserved ``invocation_state`` keys for the sampled-series records and for
#: how many sampled series did not fit in them.
TELEMETRY_SAMPLE_STATE_KEY = "_adt_telemetry_sample"
TELEMETRY_SAMPLE_OVERFLOW_KEY = "_adt_telemetry_sample_overflow"
#: Reserved ``invocation_state`` keys for the identities of the fixed-window
#: reads counted past the record bound (so a repeat is counted once), and for
#: the flag set when that set is full and the count became a lower bound.
TELEMETRY_SAMPLE_OVERFLOW_IDS_KEY = "_adt_telemetry_sample_overflow_ids"
TELEMETRY_SAMPLE_OVERFLOW_FULL_KEY = "_adt_telemetry_sample_overflow_full"
#: The Direct reader's sampling summary key (``sitewise_read.
#: HISTORY_SAMPLING_KEY``, pinned equal by a test; not imported, to keep this
#: module stdlib-pure).
DIRECT_SAMPLING_KEY = "_sampling"
#: The most values the Direct history tool returns per series per read
#: (``sitewise_read.HISTORY_DOWNSAMPLE_POINTS``, pinned equal by a test; not
#: imported, for the same reason).
DIRECT_HISTORY_RETURN_CAP = 24

#: Bounds — per investigation and per line (store re-enforces both).
MAX_COVERAGE_LINES = 16
MAX_COVERAGE_LINE_CHARS = 300
_MAX_NAME_CHARS = 120
#: Sample bounds: records kept per investigation, series named in the line,
#: and the line's length. A name longer than ``_MAX_SAMPLE_NAME_CHARS`` is
#: kept one character over it, so a name test with that ceiling refuses it
#: instead of printing a cut id.
MAX_SAMPLE_RECORDS = 64
MAX_SAMPLE_SERIES = 8
MAX_SAMPLE_LINE_CHARS = 2000
_MAX_SAMPLE_NAME_CHARS = 128
#: Most fixed-window reads past ``MAX_SAMPLE_RECORDS`` remembered by identity.
MAX_SAMPLE_OVERFLOW_IDS = 256
SAMPLE_LINE_PREFIX = "[engine] Telemetry samples, not every value: "
#: What the line's count of unlisted reads counts. A read is one series over
#: one window; a fixed window read again counts once.
_UNLISTED_RULE = "a series read again over the same window counts once"
#: The words an entry is written with (``_printed``, ``_sample_entry``). The
#: line's parser (:data:`_SAMPLE_LINE_RE`) is built from the same strings, so
#: the writer and the parser cannot drift apart.
_WITHHELD = " whose name is withheld"
_STORED_RETURNED = " stored values returned"
_READ_RETURNED = " values read returned"
_STOPPED_EARLY = ", the read stopped early"
#: How the tail opens when its count is a lower bound (``_unlisted``).
_FLOOR = "at least "
#: A name the engine-text name test lets the line print: one id token
#: (``engine_names._ID_RE`` within ``engine_names.MAX_CHARS``, pinned equal by
#: a test; not imported, to keep this module stdlib-pure).
_ID_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,127}"  # nosec B105 - a regex for an identifier, not a password
#: What each reader guarantees about the values it kept, by record kind. The
#: Direct clause also says the cap is the tool's own and holds for any window.
_SAMPLE_CLAUSES = {
    "direct": f"this app's history tool returns at most {DIRECT_HISTORY_RETURN_CAP} values "
              "per series per read, whatever the window or max_results; only a window "
              f"holding no more than {DIRECT_HISTORY_RETURN_CAP} stored values returns every "
              "one; first and last value read included, the rest picked toward evenly spaced "
              "times between them, so spacing is uneven where the data has gaps",
    "i3x": "first, last, lowest and highest value read included, the rest at evenly "
           "spaced positions in the series",
    "none": "no value returned, only a summary of the values read",
}
_SAMPLE_KINDS = tuple(_SAMPLE_CLAUSES)

#: The system agent's telemetry HISTORY tools (engine tool names).
_I3X_HISTORY_TOOLS = frozenset({"sitewise_get_asset_history",
                                "sitewise_get_assets_history"})
_I3X_COMPOSITE_TOOL = "sitewise_get_asset_telemetry"
_DIRECT_HISTORY_TOOL = "sitewise_get_asset_property_history"


def _moment(value: Any) -> "datetime | None":
    """The UTC moment of an ISO string (naive = UTC) or epoch seconds;
    ``None`` when unparseable."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        else:
            return None
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _fmt_ts(value: Any) -> str:
    """``YYYY-MM-DDTHH:MMZ`` (UTC) for an ISO string (naive = UTC) or epoch
    seconds; ``""`` when unparseable, never a guess."""
    moment = _moment(value)
    return moment.strftime("%Y-%m-%dT%H:%MZ") if moment else ""


def _window(start: Any, end: Any) -> str:
    """``"<start> to <end>"`` only when BOTH ends parse; else ``""``."""
    lo, hi = _fmt_ts(start), _fmt_ts(end)
    return f"{lo} to {hi}" if lo and hi else ""


def _fixed_window(start: Any, end: Any) -> str:
    """Both ends of an absolute window at full precision, the key that tells
    two reads of one window apart from reads of windows that print as the
    same minute; ``""`` unless both ends parse (a trailing window, or one
    ending at now, is not fixed)."""
    lo, hi = _moment(start), _moment(end)
    return f"{lo.isoformat()}/{hi.isoformat()}" if lo and hi else ""


def _result_payload(result: Any) -> Any:
    """The first JSON value of a ToolResult (``json`` part, or the leading
    JSON of a ``text`` part — a chart-tag suffix may follow); ``None`` when
    there is none."""
    content = result.get("content") if isinstance(result, dict) else None
    for part in content if isinstance(content, list) else ():
        if not isinstance(part, dict):
            continue
        if "json" in part:
            return part["json"]
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            try:
                return json.JSONDecoder().raw_decode(text.lstrip())[0]
            except ValueError:
                return None
    return None


def _stamp(point: dict) -> Any:
    return point.get("timestamp") or point.get("time")


def _series(node: Any) -> "tuple[int, Any, Any, int | None] | None":
    """``(points, first_ts, last_ts, returned)`` for a VQT list or a
    claim-check dict. ``returned`` is the number of points a claim-check
    handed back (its landmark sample), ``None`` for a list (every point)."""
    if isinstance(node, list):
        if not all(isinstance(point, dict) for point in node):
            return None
        if not node:
            return 0, None, None, None
        return len(node), _stamp(node[0]), _stamp(node[-1]), None
    stats = node.get("_stats") if isinstance(node, dict) else None
    count = stats.get("count") if isinstance(stats, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    landmarks = node.get("landmark_values")
    returned = len(landmarks) if isinstance(landmarks, list) else None
    return count, stats.get("first_ts"), stats.get("last_ts"), returned


def _row(asset: str, prop: str, window: str, series: tuple,
         partial: bool = False, starts: str = "", span: str = "") -> dict:
    """One read of one series. ``window`` is the printed window, ``span``
    its fixed key (:func:`_fixed_window`, ``""`` when the window is not
    fixed)."""
    points, first, last = series[:3]
    return {"asset": str(asset or "")[:_MAX_NAME_CHARS],
            "property": str(prop or "")[:_MAX_NAME_CHARS], "window": window,
            "points": points, "first": _fmt_ts(first), "last": _fmt_ts(last),
            "partial": partial, "starts": starts, "span": span}


def _sample_name(value: Any) -> str:
    return str(value or "")[:_MAX_SAMPLE_NAME_CHARS + 1]


def _with_sample(row: dict, kind: str, returned: Any, read: Any,
                 names: tuple) -> dict:
    """``row`` carrying a sample record when ``returned`` is fewer than
    ``read`` (both non-negative ints). ``names`` is ``(asset, property,
    asset id, property id)`` untruncated: the display names and the ids the
    result keys the series by. The record keeps the reader (``kind``) and
    the row's fixed window, which identify a read of the same series again."""
    valid = all(isinstance(n, int) and not isinstance(n, bool) and n >= 0
                for n in (returned, read))
    if valid and returned < read:
        asset, prop, asset_id, prop_id = (_sample_name(name) for name in names)
        row["sample"] = {"asset": asset, "property": prop, "asset_id": asset_id,
                         "property_id": prop_id, "returned": returned, "read": read,
                         "partial": bool(row.get("partial")),
                         "kind": kind if returned else "none",
                         "reader": kind, "window": str(row.get("span") or "")}
    return row


def _split_channel(channel_id: str, parent_id: str) -> tuple[str, str]:
    """``(asset, property)`` of an i3X channel id: ``<asset>:<property>``
    splits; an opaque component key sits under its parent element."""
    if ":" in channel_id:
        asset, prop = channel_id.split(":", 1)
        return asset, prop
    if parent_id and channel_id != parent_id:
        return parent_id, channel_id
    return channel_id, ""


def _i3x_channels(element_id: str, inner: Any, parent: str = "") -> list:
    """``(channel_id, parent_id, series)`` for one i3X element, recursing
    into ``components`` (maxDepth=0 composition)."""
    if not isinstance(inner, dict):
        return []
    found = []
    own = _series(inner.get("values")) if "values" in inner else _series(inner)
    if own is not None:
        found.append((element_id, parent, own))
    components = inner.get("components")
    for key, comp in (components.items() if isinstance(components, dict) else ()):
        found.extend(_i3x_channels(str(key), comp, element_id))
    return found


def _i3x_rows(wrappers: Any, window: str, labels: Any = None,
              asset_name: str = "", span: str = "") -> list[dict]:
    labels = labels if isinstance(labels, dict) else {}
    rows = []
    for wrapper in wrappers if isinstance(wrappers, list) else ():
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("elementId"), str):
            continue
        for cid, parent, series in _i3x_channels(wrapper["elementId"],
                                                 wrapper.get("result")):
            asset, prop = _split_channel(cid, parent)
            label = labels.get(cid)
            label = label if isinstance(label, str) and label else prop
            row = _row(asset_name or asset, label, window, series, span=span)
            rows.append(_with_sample(row, "i3x", series[3], series[0],
                                     (asset_name or asset, label, asset, prop)))
    return rows


def _compact_keys(tool_input: dict) -> dict:
    """The same key map for the compact ``asset_ids_json`` x
    ``property_ids_json`` form (the tool reads every pair); ``{}`` otherwise."""
    try:
        assets = json.loads(tool_input.get("asset_ids_json") or "[]")
        props = json.loads(tool_input.get("property_ids_json") or "[]")
    except (TypeError, ValueError):
        return {}
    if not (isinstance(assets, list) and isinstance(props, list)):
        return {}
    return {f"{a}_{p}": (a, p) for a in assets if isinstance(a, str)
            for p in props if isinstance(p, str)}


def _direct_keys(tool_input: dict) -> dict:
    """``"<assetId>_<propertyId>"`` → ``(assetId, propertyId)`` from the
    call's own ``entries_json`` or compact id arrays (exact — never a split
    heuristic)."""
    compact = _compact_keys(tool_input)
    if compact:
        return compact
    try:
        entries = json.loads(tool_input.get("entries_json") or "[]")
    except (TypeError, ValueError):
        return {}
    return {f"{e['assetId']}_{e['propertyId']}": (str(e["assetId"]), str(e["propertyId"]))
            for e in entries if isinstance(e, dict)
            and isinstance(e.get("assetId"), str) and isinstance(e.get("propertyId"), str)}


def _direct_window(tool_input: dict) -> str:
    """Absolute ``start_iso``/``end_iso``, else the stated trailing window;
    ``""`` when neither was passed (the default is not re-derived here)."""
    start, end = tool_input.get("start_iso"), tool_input.get("end_iso")
    if start:
        return _window(start, end) if end else (
            f"{_fmt_ts(start)} to now" if _fmt_ts(start) else "")
    hours = tool_input.get("hours_back")
    if isinstance(hours, int) and not isinstance(hours, bool) and hours > 0:
        return f"last {hours} h"
    return ""


#: The ONLY status wrappers the Direct reader emits around a point list
#: (``sitewise_read``). Any other status dict is not a read we understand and
#: is never counted as a clean zero-point read (2026-10-05).
_DIRECT_WRAPPER_STATUSES = frozenset({"partial", "before_first_value"})


def _direct_sampling(payload: dict) -> dict:
    """The Direct reader's per-series ``{points_returned, values_read}`` map,
    ``{}`` when the result carries none."""
    summary = payload.get(DIRECT_SAMPLING_KEY)
    series = summary.get("series") if isinstance(summary, dict) else None
    return series if isinstance(series, dict) else {}


def _direct_sample(row: dict, counts: Any, names: tuple) -> dict:
    """``row`` with the reader's sample counts for its series, kept only when
    the returned count is the number of points the result holds."""
    if not isinstance(counts, dict) or counts.get("points_returned") != row["points"]:
        return row
    return _with_sample(row, "direct", counts.get("points_returned"),
                        counts.get("values_read"), names)


def _direct_rows(payload: Any, tool_input: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    keys, window, rows = _direct_keys(tool_input), _direct_window(tool_input), []
    sampling = _direct_sampling(payload)
    for key, outcome in payload.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        status = outcome.get("status") if isinstance(outcome, dict) else None
        if status is not None and status not in _DIRECT_WRAPPER_STATUSES:
            continue  # unknown wrapper: not a read
        wrapped = status is not None
        partial = status == "partial"
        series = _series((outcome.get("points") or []) if wrapped else outcome)
        if series is None:
            continue  # per-key error outcome: not a read
        asset, prop = keys.get(key) or _split_channel(key.replace("_", ":", 1), "")
        printed, fixed = _direct_outcome_window(outcome, wrapped, tool_input, window)
        starts = _fmt_ts(outcome.get("data_starts_at")) if status == "before_first_value" else ""
        row = _row(asset, prop, printed, series, partial, starts, fixed)
        rows.append(_direct_sample(row, sampling.get(key), (asset, prop, asset, prop)))
    return rows


def _direct_outcome_window(outcome: Any, wrapped: bool, tool_input: dict,
                           window: str) -> tuple[str, str]:
    """``(printed window, fixed window key)`` of one Direct outcome: a status
    wrapper's own requested window when it carries one, else the call's."""
    if wrapped:
        start, end = outcome.get("requested_start"), outcome.get("requested_end")
        printed = _window(start, end)
        if printed:
            return printed, _fixed_window(start, end)
    return window, _fixed_window(tool_input.get("start_iso"), tool_input.get("end_iso"))


def _rows(tool_name: str, tool_input: dict, payload: Any) -> list[dict]:
    if isinstance(payload, dict) and payload.get("error"):
        return []
    start, end = tool_input.get("start_time"), tool_input.get("end_time")
    if tool_name in _I3X_HISTORY_TOOLS:
        return _i3x_rows(payload, _window(start, end), span=_fixed_window(start, end))
    if tool_name == _I3X_COMPOSITE_TOOL and isinstance(payload, dict):
        return _i3x_rows(payload.get("history"), _window(start, end), payload.get("labels"),
                         str(payload.get("asset_name") or ""), _fixed_window(start, end))
    if tool_name == _DIRECT_HISTORY_TOOL:
        return _direct_rows(payload, tool_input)
    return []


def format_coverage_line(row: dict) -> str:
    """The ONE line spelling (bounded to ``MAX_COVERAGE_LINE_CHARS``)."""
    head = f"Telemetry read: {row['asset']}"
    parts = [f"{head} / {row['property']}" if row["property"] else head]
    if row["window"]:
        parts.append(row["window"])
    points = row["points"]
    starts = row.get("starts") or ""
    if points == 0:
        parts.append(f"0 points (data starts {starts})" if starts else "0 points (no data)")
    else:
        parts.append(f"{points} point{'s' if points != 1 else ''}")
    line = ", ".join(parts)
    if points and row["first"] and row["last"]:
        line += f" (samples {row['first']} to {row['last']})"
    if row["partial"]:
        line += ", partial read"
    return line[:MAX_COVERAGE_LINE_CHARS]


def _call_rows(tool_name: Any, tool_input: Any, result: Any) -> list[dict]:
    """The rows of ONE completed tool call; ``[]`` for non-history tools,
    error envelopes and malformed shapes. Never raises."""
    try:
        return _rows(str(tool_name or ""),
                     tool_input if isinstance(tool_input, dict) else {},
                     _result_payload(result))
    except Exception:  # noqa: BLE001 - a coverage bug must never surface
        return []


def _format_rows(rows: list) -> list[str]:
    try:
        return [format_coverage_line(row) for row in rows][:MAX_COVERAGE_LINES]
    except Exception:  # noqa: BLE001 - a coverage bug must never surface
        return []


def coverage_lines(tool_name: Any, tool_input: Any, result: Any) -> list[str]:
    """Coverage lines for ONE completed tool call; ``[]`` for non-history
    tools, error envelopes and malformed shapes. Never raises."""
    return _format_rows(_call_rows(tool_name, tool_input, result))


#: What identifies a read of the same series again: the reader, the ids the
#: result keys the series by, and the fixed window.
_SAME_READ_FIELDS = ("reader", "asset_id", "property_id", "window")


def _merged(stored: list, sample: dict) -> bool:
    """Fold ``sample`` into the stored record of the same series and fixed
    window, when there is one, and say whether there was. The later read
    replaces the earlier one in place (the stored count of a fixed window can
    only grow), except that a partial read, which counts only the values read
    before its cut, never replaces a complete one."""
    if not sample.get("window"):
        return False
    for index, record in enumerate(stored):
        if isinstance(record, dict) and all(record.get(field) == sample.get(field)
                                            for field in _SAME_READ_FIELDS):
            if not sample.get("partial") or record.get("partial"):
                stored[index] = sample
            return True
    return False


def _record_samples(invocation_state: dict, rows: list, result: Any) -> None:
    """Admit this call's sample records one at a time, in order
    (:func:`_admit`); a result the tool marked as an error records none."""
    if isinstance(result, dict) and result.get("status") == "error":
        return
    samples = [row["sample"] for row in rows if isinstance(row.get("sample"), dict)]
    if not samples:
        return
    stored = invocation_state.setdefault(TELEMETRY_SAMPLE_STATE_KEY, [])
    if not isinstance(stored, list):
        return
    for sample in samples:
        _admit(invocation_state, stored, sample)


def _admit(invocation_state: dict, stored: list, sample: dict) -> None:
    """Admit one sample record: a read of a series and fixed window already
    stored updates that record (:func:`_merged`), also when both came back
    in one call; a new one is stored while there is room
    (``MAX_SAMPLE_RECORDS``); past that it is counted (:func:`_count_overflow`)."""
    if _merged(stored, sample):
        return
    if len(stored) < MAX_SAMPLE_RECORDS:
        stored.append(sample)
        return
    _count_overflow(invocation_state, sample)


def _count_overflow(invocation_state: dict, sample: dict) -> None:
    """Count one read past the record bound under
    ``TELEMETRY_SAMPLE_OVERFLOW_KEY``. A fixed-window read counts once
    however often it is repeated: its identity is kept, at most
    ``MAX_SAMPLE_OVERFLOW_IDS`` of them. Past that a new identity is not
    counted, and ``TELEMETRY_SAMPLE_OVERFLOW_FULL_KEY`` marks the count as a
    lower bound. A trailing-window read always counts: its window moves."""
    if sample.get("window"):
        seen = invocation_state.setdefault(TELEMETRY_SAMPLE_OVERFLOW_IDS_KEY, [])
        if not isinstance(seen, list):
            return
        identity = json.dumps([sample.get(field) for field in _SAME_READ_FIELDS])
        if identity in seen:
            return
        if len(seen) >= MAX_SAMPLE_OVERFLOW_IDS:
            invocation_state[TELEMETRY_SAMPLE_OVERFLOW_FULL_KEY] = True
            return
        seen.append(identity)
    before = invocation_state.get(TELEMETRY_SAMPLE_OVERFLOW_KEY)
    before = before if isinstance(before, int) and not isinstance(before, bool) else 0
    invocation_state[TELEMETRY_SAMPLE_OVERFLOW_KEY] = before + 1


def record_coverage(invocation_state: dict, tool_use: Any, result: Any) -> None:
    """Append this call's lines and sample records to ``invocation_state``
    (bounded, in order)."""
    tool_use = tool_use if isinstance(tool_use, dict) else {}
    rows = _call_rows(tool_use.get("name"), tool_use.get("input"), result)
    lines = _format_rows(rows)
    if lines:
        stored = invocation_state.setdefault(TELEMETRY_COVERAGE_STATE_KEY, [])
        if isinstance(stored, list):
            stored.extend(lines[:max(0, MAX_COVERAGE_LINES - len(stored))])
    _record_samples(invocation_state, rows, result)


def bounded_lines(value: Any) -> list[str]:
    """Re-bound an untrusted line list: non-empty strings only, each capped,
    at most ``MAX_COVERAGE_LINES``."""
    if not isinstance(value, list):
        return []
    return [line[:MAX_COVERAGE_LINE_CHARS] for line in value
            if isinstance(line, str) and line][:MAX_COVERAGE_LINES]


def collect_coverage(invocation_state: Any) -> list[str]:
    """PURE read of the recorded lines (copies; ``[]`` on foreign state)."""
    state = invocation_state if isinstance(invocation_state, dict) else {}
    return bounded_lines(state.get(TELEMETRY_COVERAGE_STATE_KEY))


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_sample(record: Any) -> bool:
    return (isinstance(record, dict)
            and all(isinstance(record.get(k), str)
                    for k in ("asset", "property", "asset_id", "property_id"))
            and _count(record.get("returned")) and _count(record.get("read"))
            and record["returned"] < record["read"]
            and record.get("kind") in _SAMPLE_KINDS)


def collect_samples(invocation_state: Any) -> list[dict]:
    """PURE read of the recorded sample records (copies of the well-formed
    ones, at most ``MAX_SAMPLE_RECORDS``; ``[]`` on foreign state)."""
    state = invocation_state if isinstance(invocation_state, dict) else {}
    stored = state.get(TELEMETRY_SAMPLE_STATE_KEY)
    if not isinstance(stored, list):
        return []
    return [dict(record) for record in stored if _valid_sample(record)][:MAX_SAMPLE_RECORDS]


def _printed(display: str, ident: str, printable: Callable, kind: str) -> str:
    """``display`` when the name test passes it, else ``ident``, else a
    placeholder naming only the kind."""
    for name in (display, ident):
        if name and printable(name):
            return name
    return f"{kind}{_WITHHELD}"


def _sample_entry(record: dict, printable: Callable) -> str:
    """``<asset> / <property>: N of M ... returned`` for one record."""
    name = _printed(record["asset"], record["asset_id"], printable, "an asset")
    if record["property"] or record["property_id"]:
        name += " / " + _printed(record["property"], record["property_id"],
                                 printable, "a property")
    counts = f"{record['returned']} of {record['read']}"
    if record.get("partial") is True:
        return f"{name}: {counts}{_READ_RETURNED}{_STOPPED_EARLY}"
    if record["kind"] == "direct":
        return f"{name}: {counts}{_STORED_RETURNED}"
    return f"{name}: {counts}{_READ_RETURNED}"


def _sample_groups(records: list, printable: Callable, budget: int) -> tuple[dict, int]:
    """Entries by record kind, in record order, within ``budget`` characters
    (each kind's clause counted once), and how many records they list."""
    groups: dict = {}
    used = listed = 0
    for record in records[:MAX_SAMPLE_SERIES]:
        entry = _sample_entry(record, printable)
        kind = record["kind"]
        cost = len(entry) + 2 + (0 if kind in groups else len(_SAMPLE_CLAUSES[kind]) + 5)
        if used + cost > budget:
            break
        groups.setdefault(kind, []).append(entry)
        used += cost
        listed += 1
    return groups, listed


def _unlisted(invocation_state: dict, records: list, listed: int) -> str:
    """How many sampled reads the line does not list, worded with what the
    count counts (``_UNLISTED_RULE``), ``""`` when it lists every one: the
    stored records it leaves out plus the reads counted past the record
    bound, ``at least`` when that count is a lower bound."""
    overflow = invocation_state.get(TELEMETRY_SAMPLE_OVERFLOW_KEY)
    hidden = len(records) - listed + (overflow if _count(overflow) else 0)
    if not hidden:
        return ""
    floor = _FLOOR if invocation_state.get(TELEMETRY_SAMPLE_OVERFLOW_FULL_KEY) is True else ""
    reads, verb = ("read", "is") if hidden == 1 else ("reads", "are")
    more = "more " if listed else ""
    return f"{floor}{hidden} {more}sampled {reads} {verb} not listed; {_UNLISTED_RULE}."


def sample_line(invocation_state: Any, printable: Callable) -> str:
    """ONE engine line naming each series a history read in this call
    returned a sample of, with both counts and what the reader kept, or
    ``""`` when none did. ``printable`` decides which names may be printed
    (the engine-text name test); a refused name falls back to the id the
    result keys the series by, then to a placeholder. Reads past
    ``MAX_SAMPLE_SERIES`` or the line budget are counted, not named
    (:func:`_unlisted`). Never raises."""
    try:
        records = collect_samples(invocation_state)
        if not records:
            return ""
        tail_room = len(f" at least 99999 more sampled reads are not listed; {_UNLISTED_RULE}.")
        budget = MAX_SAMPLE_LINE_CHARS - len(SAMPLE_LINE_PREFIX) - tail_room - 1
        groups, listed = _sample_groups(records, printable, budget)
        body = "; ".join(f"{'; '.join(groups[kind])} ({_SAMPLE_CLAUSES[kind]})"
                         for kind in _SAMPLE_KINDS if kind in groups)
        tail = _unlisted(invocation_state, records, listed)
        line = (f"{SAMPLE_LINE_PREFIX}{body}.{' ' + tail if tail else ''}" if body
                else f"{SAMPLE_LINE_PREFIX}{tail}")
        return line if is_sample_line(line, printable) else ""
    except Exception:  # noqa: BLE001 - a sample-line bug must never surface
        return ""


def _sample_line_pattern() -> "re.Pattern[str]":
    """The full grammar of the line :func:`sample_line` writes, built from the
    strings it writes the line with. A group lists entries of one record kind
    (at most ``MAX_SAMPLE_SERIES``) and ends with that kind's clause; the
    groups come in ``_SAMPLE_KINDS`` order, each at most once; the tail counts
    the reads not listed. Each entry is matched atomically: a name has no
    space, so an entry has one reading, and a failed line costs one pass."""
    head = (rf"(?:{_ID_TOKEN}|an asset{re.escape(_WITHHELD)})"
            rf"(?: / (?:{_ID_TOKEN}|a property{re.escape(_WITHHELD)}))?"
            r": (?:0|[1-9][0-9]*) of [1-9][0-9]*")
    stopped = re.escape(_READ_RETURNED + _STOPPED_EARLY)
    entries = {"direct": rf"{head}(?:{re.escape(_STORED_RETURNED)}|{stopped})",
               "other": rf"{head}{re.escape(_READ_RETURNED)}(?:{re.escape(_STOPPED_EARLY)})?"}
    groups = []
    for kind in _SAMPLE_KINDS:
        entry = entries["direct" if kind == "direct" else "other"]
        groups.append(rf"(?>{entry})(?:; (?>{entry})){{0,{MAX_SAMPLE_SERIES - 1}}}"
                      rf" \({re.escape(_SAMPLE_CLAUSES[kind])}\)")
    body = "|".join(group + "".join(f"(?:; {later})?" for later in groups[index + 1:])
                    for index, group in enumerate(groups))

    def tail(more: str) -> str:
        return (rf"(?:{re.escape(_FLOOR)})?(?:1 {more}sampled read is"
                rf"|(?:[2-9]|[1-9][0-9]+) {more}sampled reads are)"
                rf" not listed; {re.escape(_UNLISTED_RULE)}\.")
    return re.compile(rf"{re.escape(SAMPLE_LINE_PREFIX)}"
                      rf"(?:(?:{body})\.(?: {tail('more ')})?|{tail('')})")


_SAMPLE_LINE_RE = _sample_line_pattern()
#: One entry's counts: values returned of values read.
_ENTRY_COUNTS_RE = re.compile(r": ([0-9]+) of ([0-9]+) (?:stored|values) ")
#: One entry's names in a line that already matched the grammar: the asset
#: and, when present, the property, each a name or its withheld placeholder.
#: An entry opens the line or follows ``"; "``; a name has no space, so the
#: clause words after ``"; "`` never read as one.
_ENTRY_NAMES_RE = re.compile(
    rf"(?:\A{re.escape(SAMPLE_LINE_PREFIX)}|; )"
    rf"({_ID_TOKEN}|an asset{re.escape(_WITHHELD)})"
    rf"(?: / ({_ID_TOKEN}|a property{re.escape(_WITHHELD)}))?: [0-9]+ of ")


def _names_pass(value: str, printable: Callable) -> bool:
    """Whether every name the line prints passes ``printable``, the name test
    the writer prints names through (:func:`_printed`); a withheld
    placeholder is the writer's own text and needs none."""
    for asset, prop in _ENTRY_NAMES_RE.findall(value):
        for name in (asset, prop):
            if name and not name.endswith(_WITHHELD) and not printable(name):
                return False
    return True


def is_sample_line(value: Any, printable: Callable) -> bool:
    """Whether ``value`` is a line :func:`sample_line` can write: at most
    ``MAX_SAMPLE_LINE_CHARS``, a full match of its grammar
    (:data:`_SAMPLE_LINE_RE`), at most ``MAX_SAMPLE_SERIES`` entries, each
    entry returning fewer values than it read, and every name it prints
    passing ``printable``, the name test the writer is given (the engine
    passes ``engine_names.printable_id``). The one parser both the writer and
    the trace store check the line with. Never raises."""
    if not (isinstance(value, str) and len(value) <= MAX_SAMPLE_LINE_CHARS
            and _SAMPLE_LINE_RE.fullmatch(value)):
        return False
    counts = _ENTRY_COUNTS_RE.findall(value)
    if not (len(counts) <= MAX_SAMPLE_SERIES and all(int(a) < int(b) for a, b in counts)):
        return False
    try:
        return _names_pass(value, printable)
    except Exception:  # noqa: BLE001 - a failing name test keeps nothing
        return False


def bounded_sample_line(value: Any, printable: Callable) -> str:
    """``value`` when it is a line :func:`sample_line` can write
    (:func:`is_sample_line` with the same name test), else ``""``: the trace
    store's re-bound of an untrusted entry field."""
    return value if is_sample_line(value, printable) else ""


def may_hold_trace_telemetry(tool_name: Any) -> bool:
    """Whether a call of ``tool_name`` can have stored trace telemetry: a
    Mode A ``query_<id>`` system tool (gateway ``<target>___`` prefix or
    bare), the calls whose system runtime writes an investigation-trace
    entry. Only decides whether to look an entry up; the lines shown are the
    entry's, written under the call's capability, so a call of another tool
    that matches the name finds nothing."""
    return (isinstance(tool_name, str) and bool(tool_name)
            and tool_name.split("___", 1)[-1].startswith("query_"))


def system_call_summary(tool_name: Any, summary: str) -> str:
    """The trace summary of a call of ``tool_name``, as every surface (the
    cloud stream, the local chat, a reopened chat) shows it and the outcome
    classifiers read it. A system query call's (:func:`may_hold_trace_telemetry`)
    loses any copy of the sample line (:func:`without_sample_copy`) whatever
    the trace read returns: a missing, expired, unread or failed entry leaves
    the same structural summary as a read one, and the line is shown only
    from the call's stored entry, as its own field. Any other tool's summary
    is its own text. Removing text grants nothing."""
    return without_sample_copy(summary) if may_hold_trace_telemetry(tool_name) else summary


def names_sample_line(text: Any) -> bool:
    """Whether ``text`` holds the sample line's opening words. Only a hint
    of which calls to look a stored line up for (a reload reads nothing for
    the rest); it never yields a line, and a text that only claims one costs
    a read that finds nothing."""
    return isinstance(text, str) and SAMPLE_LINE_PREFIX in text


def without_sample_copy(summary: str) -> str:
    """``summary`` without the copy of the sample line it ends with, whole or
    cut off by the summary's own cut: the last line that holds the line's
    prefix or a start of it, when only engine lines (the no-data line a cut
    summary keeps) follow it, together with the blank line before it. A trace
    step that shows the stored line as its own field drops this copy, so the
    line appears once and whole, and the summary the outcome classifiers read
    holds no part of it. The copy is found by the prefix, which holds no id,
    so a summary whose ids were named after the cut (``id_name_resolver``)
    still loses it. Removing text grants nothing."""
    lines = summary.split("\n")
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if line.startswith(SAMPLE_LINE_PREFIX) or (line and SAMPLE_LINE_PREFIX.startswith(line)):
            before = index - 1 if index and not lines[index - 1] else index
            return "\n".join(lines[:before] + lines[index + 1:]).rstrip()
        if line and not line.startswith(_ENGINE_TAG):
            return summary
    return summary


_ENGINE_TAG = "[engine] "
#: The engine tags in front of a copy of the sample line's words. A match
#: starts only at the first tag of a run (the lookbehind refuses a tag right
#: after another), so each run is read once whatever its length: one linear
#: pass however many tags are stacked.
_SAMPLE_COPY_RE = re.compile(
    rf"(?<!{re.escape(_ENGINE_TAG)})(?:{re.escape(_ENGINE_TAG)})+"
    rf"(?={re.escape(SAMPLE_LINE_PREFIX[len(_ENGINE_TAG):])})")


def neutralize(text: Any) -> Any:
    """``text`` with each copy of the sample line's prefix made inert: every
    engine tag in front of its words is removed, however many are stacked
    (``[engine] [engine] ...``), in one linear pass, and the words stay."""
    if not isinstance(text, str) or SAMPLE_LINE_PREFIX not in text:
        return text
    return _SAMPLE_COPY_RE.sub("", text)
