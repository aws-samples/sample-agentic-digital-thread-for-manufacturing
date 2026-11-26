# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-request temporal context for the orchestrator.

The orchestrator system prompt is composed ONCE at module import
(``orchestrator.ORCHESTRATOR_PROMPT``) and, in cloud mode, lives on the remote
AgentCore runtime — so "now" can never go into the system prompt without going
stale. Instead, ``render_time_context`` renders a bracketed context line that
``server._prepare_question`` prefixes onto the PER-TURN user question (the same
mechanism as the existing ``[Plant context: ...]`` bracket), guaranteeing a
fresh timestamp on every request.

The line carries the current UTC timestamp (ISO 8601) + weekday and — when the
client supplied them — the user's IANA timezone (with the current local time in
that zone) and locale, so relative phrases like "today", "last 24 hours", or
"this month" resolve against the user's clock rather than the server's. The
zone is used for nothing else: every time in the answer is written in UTC in
one form (``YYYY-MM-DD HH:MM UTC``) and the browser shows it in the viewer's
own zone (frontend ``lib/answer-times.ts``). The model used to convert answer
times itself and sometimes got the arithmetic wrong (2026-10-15: a window end
six hours off), so it no longer converts any time for display.

Client input is untrusted: the timezone is validated against ``zoneinfo`` and
the locale against a strict character allowlist. Anything invalid degrades to
the UTC-only line — never an exception, and never a raw client string composed
into the prompt.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# IANA zone keys ("America/New_York", "Etc/GMT+5") — path-like, no whitespace.
_TZ_PATTERN = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")
# BCP-47-ish locale tags ("en-US", "ja", "zh-Hans-CN").
_LOCALE_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")


def _resolve_zone(tz_name: str | None) -> "ZoneInfo | None":
    """Return a ZoneInfo for a client-supplied IANA name, or None when the
    value is missing, malformed, or unknown (UTC-only fallback — never raise)."""
    if not tz_name or not isinstance(tz_name, str):
        return None
    candidate = tz_name.strip()
    if not _TZ_PATTERN.match(candidate):
        return None
    try:
        return ZoneInfo(candidate)
    except Exception:
        return None


def _sanitize_locale(locale: str | None) -> str:
    """Return the locale tag when it looks like one, else '' (dropped)."""
    if not locale or not isinstance(locale, str):
        return ""
    candidate = locale.strip()
    return candidate if _LOCALE_PATTERN.match(candidate) else ""


def _offset_sentence(now_utc: datetime, local: datetime) -> str:
    """The zone's offset in words, with the arithmetic applied to *now*.

    Event 27: given ``local time ...-0600`` the model still subtracted seven
    hours (MST's offset) and wrote "15:02 MDT" for 22:02Z. An ISO offset is
    easy to misread next to an abbreviation; "add 6 hours" applied to the
    current instant is not.

    It runs local -> UTC: answers no longer convert, so the one conversion the
    model still makes is turning a local time into a UTC time for a query. It
    holds for the current instant only, so it says so and points at the
    period starts ``_local_bounds_sentence`` lists, each converted at its own
    instant (a day that starts before a daylight-saving change has the old
    offset).
    """
    abbr = local.strftime("%Z") or "local time"
    offset = local.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    iso = f"UTC{sign}{hours:02d}:{minutes:02d} at this instant"
    if total == 0:
        rule = f"{abbr} is {iso}: a local time equals UTC"
    else:
        span = f"{hours} hour{'s' if hours != 1 else ''}"
        if minutes:
            span += f" {minutes} minutes"
        verb = "subtract" if total > 0 else "add"
        rule = f"{abbr} is {iso}: {verb} {span} {'from' if total > 0 else 'to'} a local time to get UTC"
    return (f"{rule} ({local.strftime('%H:%M')} {abbr} is {now_utc.strftime('%H:%M')} UTC). "
            "That offset holds for the current instant only: a local time across a "
            "daylight-saving change has its own offset, so use the period starts below.")


def _utc_of_local_midnight(local_now: datetime, day) -> datetime:
    """The UTC instant a local calendar day starts, at that day's own offset.

    ZoneInfo reads the offset from the wall time, so replacing the fields of
    an aware local time gives that day's offset. ``fold=0`` picks the first of
    a repeated midnight, and for a midnight that a spring-forward gap skips it
    gives the instant the gap starts, which is when the day begins."""
    midnight = local_now.replace(year=day.year, month=day.month, day=day.day, hour=0,
                                 minute=0, second=0, microsecond=0, fold=0)
    return midnight.astimezone(timezone.utc)


def _bound_text(label: str, at_utc: datetime, zone: ZoneInfo) -> str:
    local = at_utc.astimezone(zone)
    return (f"{label} {local.strftime('%Y-%m-%d %H:%M')} {local.strftime('%Z')} is "
            f"{at_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")


def _month_start(day, months_back: int):
    """The first day of the month ``months_back`` before ``day``'s month."""
    index = day.year * 12 + (day.month - 1) - months_back
    return day.replace(year=index // 12, month=index % 12 + 1, day=1)


def _local_bounds_sentence(now_utc: datetime, zone: ZoneInfo) -> str:
    """The local period starts the model needs most, each as its own UTC
    instant (computed here with ZoneInfo, never with the current offset), and
    now. A week starts on Monday (ISO 8601)."""
    local_now = now_utc.astimezone(zone)
    today = local_now.date()
    starts = (("start of today", today), ("start of yesterday", today - timedelta(days=1)),
              ("start of this week", today - timedelta(days=today.weekday())),
              ("start of this month", _month_start(today, 0)),
              ("start of last month", _month_start(today, 1)))
    parts = [_bound_text(label, _utc_of_local_midnight(local_now, day), zone)
             for label, day in starts]
    parts.append(_bound_text("now", now_utc, zone))
    return "Local period starts in UTC (a week starts on Monday): " + "; ".join(parts) + "."


# The line with no user zone is the one a system agent gets on every delegated
# question (and Quick, which sends no zone). The orchestrator now sends every
# time to a system as UTC with Z; this keeps a system agent from shifting a
# time it was handed (test event, 2026-10-15: a window end came out exactly
# one UTC offset off). The zoned line says the same thing its own way: it
# tells the model never to convert an answer time.
_ABSOLUTE_AS_GIVEN = ("A time given with Z or an offset is already absolute: use it as "
                      "given and never convert it to another zone.")

# No server bound exists for a shift: its local start can fall on either side
# of a daylight-saving change, and only a system's records know when a shift
# started, so the window comes from the data (both lines).
_SHIFT_WINDOW = ("A shift's start and end come from a system's records as absolute times; "
                 "when no system gives them, say the shift window could not be resolved "
                 "instead of guessing it.")

# The zoned line (the orchestrator, from a browser that sent its zone). The
# zone only resolves relative phrases; answer times stay UTC in one form and
# the page shows them in the viewer's zone. The standing orchestrator times
# rule carries the same form, so a turn with no zone (Quick) writes it too.
_ANSWER_TIMES_IN_UTC = (
    "Use the user's zone only to turn those phrases into UTC bounds. Data timestamps from "
    "tools are UTC unless stated otherwise. Write every time in the answer in UTC as "
    "YYYY-MM-DD HH:MM UTC, never converted to the user's zone: the user's screen shows "
    "each one in their own zone."
)


def render_time_context(
    tz_name: str | None = None,
    locale: str | None = None,
    now: datetime | None = None,
) -> str:
    """Render the bracketed temporal-context line for one request.

    Args:
        tz_name: Optional client-supplied IANA timezone (e.g. "America/Denver").
            Invalid/unknown values fall back to the UTC-only line.
        locale: Optional client-supplied locale tag (e.g. "en-US"). Invalid
            values are dropped.
        now: Injectable clock for tests; defaults to ``datetime.now(UTC)``.
    """
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    parts = [
        f"Current UTC time: {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
        f"({now_utc.strftime('%A')})."
    ]
    zone = _resolve_zone(tz_name)
    local = now_utc.astimezone(zone) if zone is not None else None
    if local is not None:
        parts.append(
            f"User timezone: {zone.key} — local time "
            f"{local.strftime('%Y-%m-%dT%H:%M:%S%z')} ({local.strftime('%A')}). "
            + _offset_sentence(now_utc, local)
        )
        parts.append(_local_bounds_sentence(now_utc, zone))
    loc = _sanitize_locale(locale)
    if loc:
        parts.append(f"User locale: {loc}.")
    parts.append(
        "Resolve relative dates and times ('today', 'yesterday', 'last 24 hours', "
        "'this month') against the user's local time when known, otherwise UTC."
    )
    parts.append(_SHIFT_WINDOW)
    parts.append(_ANSWER_TIMES_IN_UTC if zone is not None else _ABSOLUTE_AS_GIVEN)
    return "[Temporal context: " + " ".join(parts) + "]"


class PreparedQuestion(str):
    """Enriched model prompt carrying separate server-owned raw intent text."""

    def __new__(cls, enriched: str, raw_question: str):
        value = super().__new__(cls, enriched)
        value.raw_question = raw_question
        return value

    def __reduce__(self):
        # copy, deepcopy and pickle rebuild the object with both arguments.
        return (PreparedQuestion, (str(self), self.raw_question))


def with_time_context(text: str, raw: str, tz_name: str | None = None,
                      locale: str | None = None) -> PreparedQuestion:
    """``text`` with a fresh per-turn time line in front, carrying ``raw`` as
    the turn's intent (what the metric layer and memory read).

    The one enrichment for every path that runs a user question or a saved
    prompt: ``/api/ask`` (``server._prepare_question``), running a saved
    prompt, refreshing a pinned card, and a scheduled run (no zone: the
    UTC-only line, with the clock read now)."""
    return PreparedQuestion(f"{render_time_context(tz_name, locale)} {text}", raw)

