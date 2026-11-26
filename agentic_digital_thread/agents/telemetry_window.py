# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The window a telemetry history read asked for, as one engine line.

A SiteWise (Direct) history result states the window it read under its
top-level ``_window`` member (``sitewise_read.HISTORY_WINDOW_KEY``: the
resolved ``requested_start`` and ``requested_end``). Only the system agent's
answer crosses the ``query_<id>`` boundary, so the orchestrator learned that
window only when the answer said so, and a rehearsal answer printed a
24-hour window for a one-hour read.

* :func:`record_window` is called by the capture hook on every successful
  history call and keeps each distinct window once, in order, bounded by
  :data:`MAX_WINDOWS` (more are counted).
* :func:`window_line` renders ONE engine line naming them in the answer's
  time form (``YYYY-MM-DD HH:MM:SS UTC``). It prints times only, never a
  reading value, asset or channel name.
* :func:`neutralize` makes a copy of the line in model text inert before the
  engine appends its own, so only the engine's line names a window. The
  system agent applies it in every integrity mode, enforce included, and to a
  JSON answer's parsed strings (``system_agent._rule_decline_of``).

Stdlib-pure; never raises.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

#: The reader's window key (``sitewise_read.HISTORY_WINDOW_KEY``, pinned equal
#: by a test; not imported, to keep this module stdlib-pure).
DIRECT_WINDOW_KEY = "_window"
#: Reserved ``invocation_state`` keys: the windows read, and how many distinct
#: windows did not fit.
STATE_KEY = "_adt_telemetry_windows"
OVERFLOW_KEY = "_adt_telemetry_windows_overflow"
#: Most distinct windows the line names.
MAX_WINDOWS = 8
_TOOLS = frozenset({"sitewise_get_asset_property_history"})
_ENGINE_TAG = "[engine] "
WINDOW_LINE_PREFIX = f"{_ENGINE_TAG}Telemetry history windows read (as requested): "
WINDOW_LINE_TAIL = (". Each series of a read was requested over that read's window; a cut or "
                    "sample line says when a series returned less of it, and a reading's own "
                    "time is when that value was stored.")
_COPY_RE = re.compile(
    rf"(?<!{re.escape(_ENGINE_TAG)})(?:{re.escape(_ENGINE_TAG)})+"
    rf"(?={re.escape(WINDOW_LINE_PREFIX[len(_ENGINE_TAG):])})")


def _moment(value: Any) -> "datetime | None":
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _payload(result: Any) -> Any:
    """The first JSON value of a ToolResult's first ``json`` or ``text`` part."""
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


def _window_of(payload: Any) -> "tuple[str, str] | None":
    """``(start, end)`` in the line's form when the payload states a window
    whose ends both parse and are in order; else ``None``."""
    window = payload.get(DIRECT_WINDOW_KEY) if isinstance(payload, dict) else None
    if not isinstance(window, dict):
        return None
    start, end = _moment(window.get("requested_start")), _moment(window.get("requested_end"))
    if start is None or end is None or start >= end:
        return None
    form = "%Y-%m-%d %H:%M:%S UTC"
    return start.strftime(form), end.strftime(form)


def record_window(invocation_state: dict, tool_use: Any, result: Any) -> None:
    """Keep the window of one successful history call (distinct, in order)."""
    try:
        name = tool_use.get("name") if isinstance(tool_use, dict) else None
        if name not in _TOOLS or not isinstance(invocation_state, dict):
            return
        window = _window_of(_payload(result))
        if window is None:
            return
        stored = invocation_state.setdefault(STATE_KEY, [])
        if not isinstance(stored, list) or list(window) in stored:
            return
        if len(stored) < MAX_WINDOWS:
            stored.append(list(window))
            return
        before = invocation_state.get(OVERFLOW_KEY)
        invocation_state[OVERFLOW_KEY] = (before if isinstance(before, int)
                                          and not isinstance(before, bool) else 0) + 1
    except Exception:  # noqa: BLE001 - a window record is best-effort
        return


def window_line(invocation_state: Any) -> str:
    """ONE engine line naming the windows this call's history reads asked
    for, ``""`` when none was recorded. Never raises."""
    try:
        stored = invocation_state.get(STATE_KEY) if isinstance(invocation_state, dict) else None
        windows = [w for w in stored if isinstance(w, list) and len(w) == 2
                   and all(isinstance(t, str) for t in w)] if isinstance(stored, list) else []
        if not windows:
            return ""
        body = "; ".join(f"{start} to {end}" for start, end in windows[:MAX_WINDOWS])
        more = invocation_state.get(OVERFLOW_KEY)
        if isinstance(more, int) and not isinstance(more, bool) and more > 0:
            body += f"; and {more} more window{'s' if more > 1 else ''}"
        return f"{WINDOW_LINE_PREFIX}{body}{WINDOW_LINE_TAIL}"
    except Exception:  # noqa: BLE001 - a window-line bug must never surface
        return ""


def neutralize(text: Any) -> Any:
    """``text`` with each copy of the line's prefix made inert: every engine
    tag in front of its words is removed, however many are stacked, in one
    linear pass, and the words stay."""
    if not isinstance(text, str) or WINDOW_LINE_PREFIX[len(_ENGINE_TAG):] not in text:
        return text
    return _COPY_RE.sub("", text)
