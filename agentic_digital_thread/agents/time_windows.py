# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolved time windows: the date expressions of a question as UTC facts.

Across three identical rehearsal runs a bare date ("on 8/20") was read as the
UTC day in some runs and as the site's working day in others, a site's 06:30
local start was written into a zone-less timestamp (so the engine read it as
06:30 UTC), and the calendar rule that should have settled it was a steward
note the model followed about half the time. This module settles it before
the model runs, from the deployment's calendar policy
(:mod:`agents.calendar_policy`):

* :func:`resolve` takes the date expressions in the question
  (:func:`date_expressions.find_expressions`: M/D, M/D/YY, M/D/YYYY,
  YYYY-MM-DD, month-name forms, inclusive ranges, ``since``, today,
  yesterday, this week, last week, the past N days or weeks) and
  resolves each to a half-open ``[start, end)`` UTC window per the policy:
  the UTC calendar day, or the working day of each site the question names
  (that site's zone and day start). A site's working day applies only to a
  policy site the question names: with none named, the phrase is read as the
  UTC day and the block says so. A phrase the question itself writes in UTC
  (:func:`utc_cues`, a strict grammar: "UTC", "(UTC)" or "in UTC" right after
  it and then the end of its clause, "the UTC day(s)" just before it or the
  list it begins, or a sentence that starts "In UTC,") is the UTC day whatever
  the policy says, and the block says why; any other UTC, and one on a time of
  day, changes nothing. A date with no year is its most recent
  occurrence on or before
  today, and a range with no year is the latest whole range whose last day is
  on or before today (a future range needs a written year). A two-digit year
  ``YY`` is ``20YY`` when that is not after this year; otherwise the phrase is
  named as unresolved, with no window. Anything the parser cannot read with
  certainty (a fraction, a date with a time of day, ``before``/``after``/
  ``until`` a date, an invalid day) is left alone: no window, no guess.
* Nothing is dropped silently. Every expression and every in-scope site gets
  its window; past :data:`MAX_EXPRESSIONS` expressions, :data:`MAX_WINDOWS`
  windows or :data:`MAX_BLOCK_CHARS` characters the block gives NO window and
  says to narrow the question instead of a partial list.
* A working-day boundary is round-tripped through UTC. A local start that
  does not exist that day (inside the spring clock change) leaves that site's
  window unresolved, said so in the block; a local start that happens twice
  (the fall clock change) uses the earlier one (``fold=0``), said so too.
* :func:`render_block` states each one as facts: the phrase, the rule
  applied, ``[start, end)`` as ``YYYY-MM-DD HH:MM:SS UTC`` and the local
  equivalent.
* :func:`turn_block` is the per-turn entry point the orchestrator runtime
  calls before the model runs; it also records the block for the turn's
  delegations, which :func:`with_kg_context_windows` adds to each system
  query's ``kg_context`` (within the system agent's bound).

No policy, or no date expression, gives ``""`` and the turn stays
byte-identical. Pure apart from the policy read and one log line; never
calls a model.

Calendar conventions: a week runs Monday to Sunday; "last week" is the
previous Monday to Sunday and "this week" the current one; "the past N days"
is N whole days ending with today (today included); "since X" runs from X
through today. "Today" is each site's own working day; the year of a date
written without one is picked once per expression, against the latest
working day among the sites in scope, so every site gets the same dates.
"""
from __future__ import annotations

import calendar
import contextvars
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import calendar_policy as cp
from .date_expressions import MAX_SCAN_CHARS, MAX_SPAN_DAYS, DateExpression, find_expressions

logger = logging.getLogger(__name__)

#: Most date expressions resolved for one question; more resolves none.
MAX_EXPRESSIONS = 12
#: Most windows (one per site in scope, for each expression) for one question:
#: every site a policy can hold, for one expression. More resolves none.
MAX_WINDOWS = cp.MAX_SITES
#: Most policy sites whose local equivalent an unscoped UTC-day window lists
#: (the rest are counted), so a wide policy cannot crowd out the windows.
MAX_LOCAL_EQUIVALENTS = 4
#: Longest block the orchestrator turn carries; past it the compact form,
#: then the narrow-the-question block.
MAX_BLOCK_CHARS = 8000
#: A range written end before start in month terms ("12/28-1/3") rolls into
#: the next year only when the rolled span stays this short.
_MAX_ROLLED_SPAN_DAYS = 120
#: The system agent's bound on ``kg_context`` (``system_agent._KG_CONTEXT_MAX_CHARS``).
KG_CONTEXT_MAX_CHARS = 2000

HEADER = "[RESOLVED TIME WINDOWS]"
_INTRO = ("The engine resolved the date expressions in the user's question with this "
          "deployment's calendar rules, before this turn:")


_PLANT_RE = re.compile(r"\[Plant context: (?P<plant>[^\]\n]{1,128}?) \u2014 scope")


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    """One resolved window. ``site`` is ``""`` for the UTC calendar day."""

    site: str
    timezone: str
    day_start: str
    first_day: date
    last_day: date
    start_utc: datetime
    end_utc: datetime
    #: Facts about a boundary at a clock change (a local time that happens twice).
    notes: tuple = ()


@dataclass(frozen=True)
class ResolvedExpression:
    """An expression with its rule and windows. ``category`` is ``date`` (a
    date with no time, the ``bare_date`` rule), ``relative`` (the
    ``relative_days`` rule) or ``overflow`` (nothing resolved; ``unresolved``
    says why). ``local_sites`` are the sites whose local equivalent a UTC-day
    window shows; ``unresolved`` holds ``(site, reason)`` for each site in
    scope that has no window."""

    phrase: str
    category: str
    rule: str
    windows: tuple
    site_named: bool
    local_sites: tuple = ()
    #: ``date``, ``range``, ``since`` or ``relative`` (as found).
    kind: str = "date"
    unresolved: tuple = ()
    #: Site names in the order their windows and unresolved entries are listed.
    order: tuple = ()
    #: Policy sites left out of ``local_sites`` (an unscoped question).
    more_local: int = 0
    #: The policy reads this phrase as a site's working day, but the question
    #: named no policy site, so it was read as the UTC day.
    site_day_unnamed: bool = False
    #: The policy's working-day start, for that note.
    day_start: str = ""
    #: The policy reads this phrase as a site's working day, but the question
    #: names the UTC frame for it; this says where (``'"UTC" right after it'``).
    utc_cue: str = ""


@dataclass(frozen=True)
class _Frame:
    site: str
    zone: ZoneInfo
    day_start: time


_UTC_FRAME = _Frame("", ZoneInfo("UTC"), time(0, 0))


def _today(frame: _Frame, now: datetime) -> date:
    """The day that contains ``now`` in the frame: a working day that has not
    started yet locally is still the previous day."""
    local = now.astimezone(frame.zone)
    day = local.date()
    if local.time().replace(tzinfo=None) < frame.day_start:
        day -= timedelta(days=1)
    return day


def _boundary(frame: _Frame, day: date) -> tuple:
    """``(UTC instant, "" | "ambiguous" | "nonexistent")`` for the local start
    of the working day ``day``, round-tripped through UTC. A local time inside
    the spring gap does not exist; one inside the fall overlap is ambiguous and
    the earlier (``fold=0``) one is used."""
    naive = datetime.combine(day, frame.day_start)
    earlier = naive.replace(tzinfo=frame.zone)
    moment = earlier.astimezone(timezone.utc)
    if moment.astimezone(frame.zone).replace(tzinfo=None) != naive:
        return None, "nonexistent"
    later = naive.replace(tzinfo=frame.zone, fold=1)
    return moment, ("ambiguous" if later.utcoffset() != earlier.utcoffset() else "")


def _valid(year: int, month: int, day: int) -> "date | None":
    if not (1 <= month <= 12 and 1 <= year <= 9999):
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return date(year, month, day)


def _latest_on_or_before(month: int, day: int, today: date) -> "date | None":
    """The most recent ``month``/``day`` on or before ``today`` (a leap day
    looks back to the latest year that has one)."""
    for year in range(today.year, today.year - 9, -1):
        found = _valid(year, month, day)
        if found is not None and found <= today:
            return found
    return None


def _latest_interval(first: tuple, last: tuple, today: date) -> "tuple | None":
    """The latest whole ``first``-to-``last`` range written with no year whose
    last day is on or before ``today``. A range whose end comes earlier in the
    year than its start crosses New Year, within :data:`_MAX_ROLLED_SPAN_DAYS`."""
    (_y1, m1, d1, _yy1), (_y2, m2, d2, _yy2) = first, last
    rolls = (m2, d2) < (m1, d1)
    for year in range(today.year, today.year - 9, -1):
        start, end = _valid(year, m1, d1), _valid(year + int(rolls), m2, d2)
        if start is None or end is None or end > today:
            continue
        if rolls and (end - start).days > _MAX_ROLLED_SPAN_DAYS:
            return None
        return start, end
    return None


def _written_range(first: tuple, last: tuple) -> "tuple | None":
    """A range with a written year on at least one end."""
    (y1, m1, d1, _yy1), (y2, m2, d2, _yy2) = first, last
    if y1 and y2:
        return _valid(y1, m1, d1), _valid(y2, m2, d2)
    year = y1 or y2
    start, end = _valid(year, m1, d1), _valid(year, m2, d2)
    if start and end and end < start:
        start, end = ((start, _valid(year + 1, m2, d2)) if y1
                      else (_valid(year - 1, m1, d1), end))
    return start, end


def _absolute_days(expression: DateExpression, reference: date,
                   today: date) -> "tuple | None":
    """The days of a written date or range. ``reference`` picks a missing
    year (the latest working day among the sites in scope); ``today`` is this
    site's own working day, where a ``since`` range ends."""
    (y1, m1, d1, _yy1) = expression.first
    if expression.kind in ("date", "since"):
        first = _valid(y1, m1, d1) if y1 else _latest_on_or_before(m1, d1, reference)
        if first is None or (expression.kind == "since" and first > reference):
            return None
        return (first, max(first, today)) if expression.kind == "since" else (first, first)
    if expression.last[0] or y1:
        first, last = _written_range(expression.first, expression.last)
    else:
        found = _latest_interval(expression.first, expression.last, reference)
        first, last = found if found else (None, None)
    if first is None or last is None or last < first:
        return None
    if (last - first).days >= MAX_SPAN_DAYS:
        return None
    return first, last


def _relative_days(expression: DateExpression, today: date) -> tuple:
    name = expression.relative
    if expression.kind == "since":
        start = today - timedelta(days=1) if name == "yesterday" else today
        return start, today
    if name == "today":
        return today, today
    if name == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    monday = today - timedelta(days=today.weekday())
    if name == "this_week":
        return monday, monday + timedelta(days=6)
    if name == "last_week":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    return today - timedelta(days=expression.count - 1), today


def _window(frame: _Frame, days: tuple) -> tuple:
    """``(Window, "")``, or ``(None, reason)`` when a boundary does not exist."""
    first, last = days
    start_text = frame.day_start.strftime("%H:%M")
    bounds = []
    for day in (first, last + timedelta(days=1)):
        moment, flag = _boundary(frame, day)
        if flag == "nonexistent":
            return None, (f"its working day start {start_text} does not exist on "
                          f"{day.isoformat()} (the clocks skip that time)")
        bounds.append((moment, flag, day))
    notes = tuple(f"{start_text} happens twice on {day.isoformat()}; the earlier one is used"
                  for _moment, flag, day in bounds if flag == "ambiguous")
    return Window(site=frame.site, timezone=frame.zone.key, day_start=start_text,
                  first_day=first, last_day=last, start_utc=bounds[0][0],
                  end_utc=bounds[1][0], notes=notes), ""


def _site_pattern(site: str):
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(site)}(?![A-Za-z0-9])", re.IGNORECASE)


def named_sites(text: str, policy: cp.CalendarPolicy) -> tuple:
    """The policy's sites the text names (whole words, any case), in policy order."""
    return tuple(site for site in policy.sites if _site_pattern(site.site).search(text or ""))


def _frame(policy: cp.CalendarPolicy, site: cp.SiteZone) -> _Frame:
    return _Frame(site.site, ZoneInfo(site.timezone), policy.day_start(site))


def _question_text(prompt: str) -> tuple:
    """``(question, scope text)``: the user's question with the composed
    preambles peeled, and the question plus any selected plant name (which
    names a site as much as the question does)."""
    from .memory_labels import strip_turn_preambles
    question = strip_turn_preambles(prompt or "")
    plant = _PLANT_RE.search(prompt or "")
    scope = f"{question} {plant.group('plant')}" if plant else question
    return question, scope


def _overflow(reason: str) -> ResolvedExpression:
    """The one result when the question cannot be resolved whole."""
    return ResolvedExpression(phrase="", category="overflow", rule="", windows=(),
                              site_named=False, kind="overflow", unresolved=(("", reason),))


_TWO_DIGIT_REASON = ("its two-digit year would be after this year, and the engine does not "
                     "guess the century; ask the user for the four-digit year")


def _future_two_digit_year(expression: DateExpression, reference: date) -> bool:
    """A two-digit year is ``20YY`` only when that year is not after this one."""
    ends = [expression.first] + ([expression.last] if expression.last else [])
    return any(two_digit and year > reference.year for year, _m, _d, two_digit in ends)


# --------------------------------------------------------------------------- #
# A question that names the UTC frame for a date expression
# --------------------------------------------------------------------------- #
# A small, strict grammar (:func:`utc_cues`): a cue moves an expression to the
# UTC day only in one of three forms. Anything else keeps the policy's rule.
_UTC_WORD = r"(?:UTC|GMT)"
#: Blank space within one line: a line break ends a clause, so no cue crosses it.
_SP = r"[^\S\n]"
_BOUND = rf"{_SP}+(?:inclusive|exclusive)\b"
#: Form 1, right after an expression: "UTC" or "GMT", "(UTC)", ", UTC" or
#: "in UTC", after the range's own "inclusive" or "exclusive" if it has one,
#: and optionally followed by one.
_AFTER = re.compile(
    rf"(?:{_BOUND})?(?:{_SP}*(?P<paren>\({_UTC_WORD}\))"
    rf"|{_SP}*,{_SP}*(?P<comma>{_UTC_WORD})\b"
    rf"|{_SP}+(?P<word>(?:in{_SP}+)?{_UTC_WORD})\b)(?:{_BOUND})?", re.IGNORECASE)
#: What must come right after a form-1 cue: the end of the text, clause
#: punctuation, a line break, "and", "or" or "at" before a word that is not
#: UTC or GMT, or "for" and a policy site's name (masked) that ends the clause.
#: Any other "for" or "in" ("for display", "for site-a display", "in the
#: header") says what the cue is for.
_AFTER_END = re.compile(
    rf"{_SP}*(?:\Z|[.,;:?!)\n])"
    rf"|{_SP}+(?:and|or|at){_SP}+(?!{_UTC_WORD}\b)\S"
    rf"|{_SP}+for{_SP}+(?:the{_SP}+)?\x00+{_SP}*(?:\Z|[.,;:?!)\n])", re.IGNORECASE)
#: A negation or contrast earlier in the cue's sentence ("not the UTC day",
#: "rather than 10/5 UTC", "do not use, for this report, the UTC day"), or
#: "non-" right before the frame word: the question may reject that frame, so
#: the cue does not count (the safe side: the date keeps the policy's rule).
_NEGATION = re.compile(
    r"\b(?:not|never|without|except|excluding|rather\s+than|instead\s+of|other\s+than"
    r"|do\s+not|does\s+not|don['\u2019]t|doesn['\u2019]t)\b", re.IGNORECASE)
_NON = re.compile(r"\bnon[-\u2010\u2011]?$", re.IGNORECASE)
#: Form 2, a frame noun right before an expression: "UTC day(s)", "UTC
#: date(s)", "UTC calendar day(s)" or "GMT day(s)" ("of" may come between:
#: "the UTC day of 10/5").
_FRAME_NOUN = re.compile(
    rf"\b(?P<cue>UTC{_SP}+(?:calendar{_SP}+)?days?|UTC{_SP}+dates?|GMT{_SP}+days?)"
    rf"(?:{_SP}+of)?{_SP}+", re.IGNORECASE)
#: Between the members of the list a frame noun begins: a comma, "and", ", and".
_AND_LIST = re.compile(rf"{_SP}*,{_SP}*(?:and{_SP}+)?|{_SP}+and{_SP}+", re.IGNORECASE)
#: Form 3, a sentence-leading frame: "In UTC,", "Using UTC days," or "Using UTC
#: dates," (the comma is required).
_LEAD = re.compile(rf"\s*(?P<cue>In{_SP}+UTC|Using{_SP}+UTC{_SP}+(?:days|dates)){_SP}*,",
                   re.IGNORECASE)
_TIME_OF_DAY = rf"\d{{1,2}}(?::\d{{2}}(?::\d{{2}})?|{_SP}*[ap]\.?m\b\.?)"
#: A time of day just before or just after a cue: the cue is part of that time.
_TIME_BEFORE = re.compile(rf"{_TIME_OF_DAY}{_SP}*,?{_SP}*$", re.IGNORECASE)
_TIME_AFTER = re.compile(rf"{_SP}*,?{_SP}*(?:at{_SP}+)?{_TIME_OF_DAY}", re.IGNORECASE)
#: A time of day just before an expression ("06:00 on 10/5"): a cue right
#: after that expression may be the time's.
_TIME_LEAD = re.compile(rf"{_TIME_OF_DAY}{_SP}*,?{_SP}*(?:(?:on|of){_SP}+)?(?:the{_SP}+)?$",
                        re.IGNORECASE)
#: Between two expressions of one list: a comma, "and", "or", "vs".
_JOIN = re.compile(
    rf"{_SP}*(?:,{_SP}*(?:(?:and|or){_SP}+)?|(?:and|or|vs\.?|versus|&){_SP}+)"
    rf"(?:(?:on|of|for){_SP}+)?(?:the{_SP}+)?", re.IGNORECASE)
#: A site whose whole name is the frame word.
_FRAME_NAMES = ("utc", "gmt")
#: A sentence ends at ". ! ?" or ";" before a space or the end ("6 p.m." does
#: not), or at a line break.
_SENTENCE_END = re.compile(r"(?<![AaPp]\.[Mm])[.!?;](?=\s|$)|\n")
#: Quoted text: double, curly and back quotes, and single quotes that are not
#: an apostrophe (a word character on neither outer side).
_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d|`[^`\n]*`'
                     r"|(?<!\w)'[^'\n]*'(?!\w)|(?<!\w)\u2018[^\u2018\u2019\n]*\u2019(?!\w)")
#: What masked text is filled with: not a space, a word or punctuation, so it is
#: never read as a cue, the end of a clause or a list join.
_MASK = "\x00"


def _negated(masked: str, start: int) -> bool:
    """A negation or contrast between the start of the sentence holding
    ``start`` and ``start`` (commas and parentheses do not end its reach), or
    "non-" right before it."""
    low = max((m.end() for m in _SENTENCE_END.finditer(masked, 0, start)), default=0)
    return bool(_NEGATION.search(masked, low, start) or _NON.search(masked[:start]))


def _blank(text: str, spans) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = _MASK * (end - start)
    return "".join(chars)


def _cue_text(raw: str) -> str:
    return " ".join(raw.split())


def _sentences(masked: str, expressions: tuple) -> list:
    """``[(start, end)]`` of each sentence; a dot inside an expression
    ("Oct. 5") does not end one."""
    ends = [m.end() for m in _SENTENCE_END.finditer(masked)
            if not any(e.start < m.end() <= e.end for e in expressions)]
    return list(zip([0] + ends, ends + [len(masked)]))


def _form_one(masked: str, expressions: tuple) -> list:
    """``[(index, span, written)]``: a cue right after an expression, then the
    clause ends. A cue on a time of day ("10/5 UTC at 06:00"), or after an
    expression a time of day leads ("06:00 on 10/5 in UTC"), does not count."""
    found = []
    for index, expression in enumerate(expressions):
        match = _AFTER.match(masked, expression.end)
        if (match is None or _AFTER_END.match(masked, match.end()) is None
                or _TIME_AFTER.match(masked, match.end())
                or _TIME_LEAD.search(masked, max(0, expression.start - 24), expression.start)
                or _negated(masked, expression.start)):
            continue
        group = next(name for name in ("paren", "comma", "word") if match.group(name))
        found.append((index, match.span(group), _cue_text(match.group(group))))
    return found


def _form_two(masked: str, expressions: tuple) -> list:
    """``[(indices, span, written)]``: a frame noun and the expression right
    after it, with every member of a list that expression begins."""
    starts = {expression.start: index for index, expression in enumerate(expressions)}
    found = []
    for match in _FRAME_NOUN.finditer(masked):
        first = starts.get(match.end())
        if first is None or _TIME_BEFORE.search(masked, max(0, match.start() - 24),
                                                match.start()) \
                or _negated(masked, match.start("cue")):
            continue
        members = [first]
        while (members[-1] + 1 < len(expressions)
               and _AND_LIST.fullmatch(masked, expressions[members[-1]].end,
                                       expressions[members[-1] + 1].start)):
            members.append(members[-1] + 1)
        found.append((tuple(members), match.span("cue"), _cue_text(match.group("cue"))))
    return found


def _form_three(masked: str, expressions: tuple) -> list:
    """``[(indices, span, written)]``: a sentence-leading frame and every
    expression in its sentence. "In UTC," that a time of day follows ("In UTC,
    06:00 to 08:00 ...") belongs to that time and does not count; "Using UTC
    days," names days, so a time after it does not change it."""
    found = []
    for low, high in _sentences(masked, expressions):
        match = _LEAD.match(masked, low, high)
        if match is None or (match.group("cue").lower().startswith("in")
                             and _TIME_AFTER.match(masked, match.end("cue"))):
            continue
        members = tuple(index for index, expression in enumerate(expressions)
                        if low <= expression.start < high)
        found.append((members, match.span("cue"), _cue_text(match.group("cue"))))
    return found


def _ambiguous_lists(masked: str, expressions: tuple, one: dict) -> set:
    """The members of every list (expressions joined by a comma, "and", "or"
    or "vs", a member's own form-1 cue allowed before the join) in which a
    member with no form-1 cue in ``one`` comes right before one with a cue."""
    def gap_start(index: int) -> int:
        match = _AFTER.match(masked, expressions[index].end) if index in one else None
        return match.end() if match else expressions[index].end

    lists, ambiguous = [], set()
    for index, expression in enumerate(expressions):
        if index and _JOIN.fullmatch(masked, gap_start(index - 1), expression.start):
            lists[-1].append(index)
        else:
            lists.append([index])
    for members in lists:
        if any(index in one and index - 1 not in one for index in members[1:]):
            ambiguous.update(members)
    return ambiguous


def _cue_spans(masked: str, expressions: tuple) -> list:
    """Every span the grammar reads as a cue, in any of its three forms."""
    return [span for form in (_form_one, _form_two, _form_three)
            for _which, span, _written in form(masked, expressions)]


def _mask(text: str, expressions: tuple, policy: cp.CalendarPolicy) -> str:
    """``text`` with quoted strings and the policy's site names masked. A site
    whose whole name is the frame word ("UTC") cannot be told from the cue by
    its spelling: when every mention of it has a cue's shape, the mentions may
    all be the site and are masked (the site's working day stays); when the
    question also names it outside a cue's shape, a cue-shaped "in UTC" is
    the frame and is kept."""
    masked = _blank(text, [m.span() for m in _QUOTED.finditer(text)])
    framed = [s for s in policy.sites if s.site.casefold() in _FRAME_NAMES]
    masked = _blank(masked, [m.span() for site in policy.sites if site not in framed
                             for m in _site_pattern(site.site).finditer(masked)])
    for site in framed:
        mentions = [m.span() for m in _site_pattern(site.site).finditer(masked)]
        shaped = _cue_spans(masked, expressions)
        if mentions and all(any(a <= s and e <= b for a, b in shaped) for s, e in mentions):
            masked = _blank(masked, mentions)
    return masked


def utc_cues(text: str, expressions: tuple, policy: cp.CalendarPolicy) -> tuple:
    """For each expression in ``text`` (positions from :func:`find_expressions`),
    where the question names the UTC frame for it, or ``""``. A cue counts in
    one of three forms only:

    1. Right after the expression, then the clause ends: "UTC" or "GMT",
       "(UTC)", ", UTC" or "in UTC" (after the range's own "inclusive" or
       "exclusive", and optionally followed by one), then the end of the
       text, ``. , ; : ? !``, ``)``, a line break, or "and", "or", "at",
       "for" or "in" before a word that is not UTC or GMT ("10/5 UTC, at
       site-a"; not "10/5 UTC format" or "10/5 in UTC timestamps").
    2. A frame noun right before it: "UTC day(s)", "UTC date(s)", "UTC
       calendar day(s)" or "GMT day(s)", with at most "of" between ("the UTC
       day of 10/5"). It frames that expression and every member of a list
       it begins whose members are joined by commas or "and" ("the UTC days
       10/5 and 10/6").
    3. A sentence that starts with "In UTC,", "Using UTC days," or "Using UTC
       dates," (the comma is required): every expression in that sentence. A
       sentence ends at ``. ! ?`` or ``;`` before a space, or at a line break.

    A cue that is part of a time of day ("06:00 UTC", "10/5 UTC at 06:00",
    "06:00 on 10/5 in UTC") is not a date cue. In a list where a member with
    no form-1 cue comes right before one with a cue ("10/5 and 10/6 UTC"),
    that cue may be meant for the whole list or for one date, so no member of
    the list moves by form 1. Quoted strings and the policy's site names are
    masked first (see :func:`_mask`). Anything else keeps the policy's rule.
    Pure."""
    masked = _mask(text, expressions, policy)
    cues = ["" for _ in expressions]
    one = {index: written for index, _span, written in _form_one(masked, expressions)}
    for index in _ambiguous_lists(masked, expressions, one):
        one.pop(index, None)
    for index, written in one.items():
        cues[index] = f'"{written}" right after it'
    for members, _span, written in _form_two(masked, expressions):
        for position, index in enumerate(members):
            cues[index] = cues[index] or (f'"{written}" just before it' if position == 0
                                          else f'"{written}" just before the list it is in')
    for members, _span, written in _form_three(masked, expressions):
        for index in members:
            cues[index] = cues[index] or f'"{written}" leading its sentence'
    return tuple(cues)


def _scope(item: ResolvedExpression, policy: cp.CalendarPolicy,
           named: tuple, utc_cue: str = "") -> ResolvedExpression:
    """Which day the phrase is read as. A site's working day applies only to
    the policy sites the question names; with none named it is the UTC day,
    and the block says so. A phrase the question writes in UTC (``utc_cue``)
    is the UTC day whatever the policy says, and the block says why. A UTC-day
    window lists the named sites' local equivalents, or for an unscoped
    question the first :data:`MAX_LOCAL_EQUIVALENTS` policy sites' (the rest
    counted)."""
    if utc_cue and item.rule == cp.SITE_DAY:
        item = replace(item, rule=cp.UTC_DAY, utc_cue=utc_cue)
        if named:
            return replace(item, local_sites=tuple(named))
        return replace(item, local_sites=tuple(policy.sites[:MAX_LOCAL_EQUIVALENTS]),
                       more_local=max(0, len(policy.sites) - MAX_LOCAL_EQUIVALENTS))
    if item.rule == cp.SITE_DAY and named:
        return item
    if named:
        return replace(item, rule=cp.UTC_DAY, local_sites=tuple(named))
    return replace(item, rule=cp.UTC_DAY,
                   local_sites=tuple(policy.sites[:MAX_LOCAL_EQUIVALENTS]),
                   more_local=max(0, len(policy.sites) - MAX_LOCAL_EQUIVALENTS),
                   site_day_unnamed=item.rule == cp.SITE_DAY,
                   day_start=policy.site_day_start)


def _resolve_one(expression: DateExpression, policy: cp.CalendarPolicy, named: tuple,
                 now: datetime, utc_cue: str = "") -> "ResolvedExpression | None":
    category = "date" if expression.first is not None else "relative"
    rule = policy.bare_date if category == "date" else policy.relative_days
    item = _scope(ResolvedExpression(expression.phrase, category, rule, (), bool(named),
                                     kind=expression.kind), policy, named, utc_cue)
    frames = ([_frame(policy, site) for site in named] if item.rule == cp.SITE_DAY
              else [_UTC_FRAME])
    reference = max(_today(frame, now) for frame in frames)
    if category == "date" and _future_two_digit_year(expression, reference):
        return replace(item, unresolved=(("", _TWO_DIGIT_REASON),))
    windows, unresolved, order = [], [], []
    for frame in frames:
        today = _today(frame, now)
        days = (_absolute_days(expression, reference, today) if category == "date"
                else _relative_days(expression, today))
        if days is None:
            return None              # not a readable date at all: left alone
        window, reason = _window(frame, days)
        if window is None:
            unresolved.append((frame.site, reason))
        else:
            windows.append(window)
        order.append(frame.site)
    return replace(item, windows=tuple(windows), unresolved=tuple(unresolved),
                   order=tuple(order))


def resolve(question: object, policy: "cp.CalendarPolicy | None", *,
            now: "datetime | None" = None) -> tuple:
    """Resolve every date expression in ``question`` under ``policy``.
    ``question`` may be a composed prompt; its temporal and plant preambles
    are peeled first (a plant name still counts as a named site). ``()`` with
    no policy or nothing to resolve; one ``overflow`` result (no window at
    all) when the question has more than :data:`MAX_EXPRESSIONS` expressions
    or needs more than :data:`MAX_WINDOWS` windows. Pure given ``now``."""
    if policy is None or not isinstance(question, str):
        return ()
    now = (now or _utc_now()).astimezone(timezone.utc)
    text, scope = _question_text(question)
    if len(text) > MAX_SCAN_CHARS:
        return (_overflow(f"the question is longer than {MAX_SCAN_CHARS} characters"),)
    expressions = find_expressions(text)
    if not expressions:
        return ()
    if len(expressions) > MAX_EXPRESSIONS:
        return (_overflow(f"it has {len(expressions)} date expressions and the engine "
                          f"resolves at most {MAX_EXPRESSIONS} for one question"),)
    named = named_sites(scope, policy)
    cues = utc_cues(text, expressions, policy)
    resolved = tuple(item for item in (_resolve_one(e, policy, named, now, cue)
                                       for e, cue in zip(expressions, cues))
                     if item is not None)
    count = sum(len(item.windows) + len(item.unresolved) for item in resolved)
    if count > MAX_WINDOWS:
        return (_overflow(f"it needs {count} windows (one per site in scope for each date) "
                          f"and the engine resolves at most {MAX_WINDOWS} for one question"),)
    return resolved


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _offset(local: datetime) -> str:
    total = int((local.utcoffset() or timedelta(0)).total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _local_span(window: Window, zone: str) -> str:
    tz = ZoneInfo(zone)
    start, end = window.start_utc.astimezone(tz), window.end_utc.astimezone(tz)
    first, last = start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")
    if _offset(start) == _offset(end):
        return f"{first} to {last} ({_offset(start)})"
    return f"{first} ({_offset(start)}) to {last} ({_offset(end)})"


def _days_text(window: Window) -> str:
    if window.first_day == window.last_day:
        return window.first_day.isoformat()
    return f"{window.first_day.isoformat()} through {window.last_day.isoformat()}"


def _span(window: Window) -> str:
    return f"[{_utc_text(window.start_utc)}, {_utc_text(window.end_utc)})"


def _what(item: ResolvedExpression) -> str:
    if item.kind == "since":
        return "a start day, counted through today"
    if item.category == "date":
        return "a date range with no time" if item.kind == "range" else "a date with no time"
    return "a relative day"


def _site_line(window: Window, compact: bool) -> str:
    line = (f"  {window.site} ({window.timezone}, day starts {window.day_start}): "
            f"{_days_text(window)} = {_span(window)}")
    if not compact:
        line += f", local {_local_span(window, window.timezone)}"
    if window.notes:
        line += f" ({'; '.join(window.notes)})"
    return line + "."


def _utc_line(item: ResolvedExpression, compact: bool) -> str:
    (window,) = item.windows
    reason = (f" because the question names the UTC frame for it ({item.utc_cue}), not as "
              "a site's working day") if item.utc_cue else ""
    line = (f'- "{item.phrase}" is {_what(item)}, read as the UTC calendar day{reason}: '
            f"{_days_text(window)} = {_span(window)}.")
    if item.site_day_unnamed:
        # The question may name a site the policy lacks; it is not "no site named".
        line += (" No calendar-policy site was recognized in the question, so these are UTC "
                 f"days; a site's working day (starting {item.day_start} local) applies to a "
                 "calendar-policy site the question names, and a site the question names that "
                 "is not in the calendar policy has no configured working day: say so.")
    if item.local_sites and not compact:
        locals_text = "; ".join(f"{site.site} {_local_span(window, site.timezone)}"
                                for site in item.local_sites)
        more = f"; and {item.more_local} more sites" if item.more_local else ""
        line += f" Local equivalent: {locals_text}{more}."
    return line


def _lines(item: ResolvedExpression, compact: bool) -> list:
    if not item.windows and any(site == "" for site, _reason in item.unresolved):
        (_site, reason), = item.unresolved
        return [f'- "{item.phrase}" could not be resolved: {reason}. No window is given, so '
                "do not derive one."]
    if item.rule == cp.UTC_DAY:
        return [_utc_line(item, compact)]
    head = (f'- "{item.phrase}" is {_what(item)}, read as the working day of each site named'
            " (a site not listed here has no configured working day: say so rather than use"
            " another site's window):")
    by_site = {window.site: _site_line(window, compact) for window in item.windows}
    by_site.update({site: (f"  {site}: could not be resolved: {reason}. No window is given, "
                           "so do not derive one; a day start outside the clock change hour "
                           "in the calendar policy fixes this.")
                    for site, reason in item.unresolved})
    return [head] + [by_site[site] for site in item.order or tuple(by_site)]


_UNRESOLVED_RULE = ("A phrase or site marked could not be resolved has no window: do not "
                    "derive one; say it could not be resolved and why.")


def _footer(resolved: tuple) -> str:
    windows = [window for item in resolved for window in item.windows]
    unresolved = any(item.unresolved for item in resolved)
    if not windows:
        return _UNRESOLVED_RULE
    start = windows[0].start_utc
    example, iso = start.strftime("%Y-%m-%d %H:%M:%S"), start.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ("Use exactly these windows for these dates and periods; do not re-derive them. "
            "Filter each as column >= start AND column < end, with start and end written as "
            "these UTC instants: with an explicit UTC zone when the column type carries a time "
            f"zone (on Athena: TIMESTAMP '{example} UTC'), as the plain UTC instant when it "
            f"does not (TIMESTAMP '{example}'), and as ISO 8601 UTC ({iso}) for a telemetry "
            "history tool." + (f" {_UNRESOLVED_RULE}" if unresolved else ""))


def _overflow_block(item: ResolvedExpression) -> str:
    (_site, reason), = item.unresolved
    return (f"{HEADER}\nThe engine could not resolve every date in the user's question: "
            f"{reason}. No window is given. Do not derive windows yourself: ask the user to "
            "narrow the question (fewer dates, periods or sites) before querying.")


def render_block(resolved, *, compact: bool = False) -> str:
    """The RESOLVED TIME WINDOWS block for ``resolved``, ``""`` when empty.
    ``compact`` leaves out the local equivalents (for a tight ``kg_context``).
    An ``overflow`` result renders the narrow-the-question block alone."""
    resolved = tuple(resolved or ())
    if not resolved:
        return ""
    overflow = [item for item in resolved if item.category == "overflow"]
    if overflow:
        return _overflow_block(overflow[0])
    lines = [HEADER, _INTRO]
    for item in resolved:
        lines.extend(_lines(item, compact))
    lines.append(_footer(resolved))
    return "\n".join(lines)


def render_for_turn(resolved) -> tuple:
    """``(turn block, compact block)`` within :data:`MAX_BLOCK_CHARS`: the
    full block, else the compact one, else the narrow-the-question block
    (never a cut list). ``("", "")`` when there is nothing to resolve."""
    full, compact = render_block(resolved), render_block(resolved, compact=True)
    if len(full) > MAX_BLOCK_CHARS:
        full = compact
    if len(full) > MAX_BLOCK_CHARS:
        full = compact = render_block((_overflow(
            f"its windows take more than {MAX_BLOCK_CHARS} characters to state"),))
    return full, compact


# --------------------------------------------------------------------------- #
# The per-turn hand-off: the runtime resolves once, the delegations read it
# --------------------------------------------------------------------------- #
_TURN_WINDOWS: "contextvars.ContextVar[tuple]" = contextvars.ContextVar(
    "adt_turn_time_windows", default=("", ""))


def remember_turn_windows(full: str, compact: str = "") -> None:
    """Record this turn's block (full and compact forms; ``""`` for none)."""
    _TURN_WINDOWS.set((full or "", compact or full or ""))


def turn_windows() -> tuple:
    """``(full, compact)`` block for the current turn, ``("", "")`` for none."""
    return _TURN_WINDOWS.get()


def reset_turn_windows() -> None:
    """Clear the current turn's windows (start of every invocation)."""
    _TURN_WINDOWS.set(("", ""))


def turn_block(prompt: str, *, policy=None, now: "datetime | None" = None) -> str:
    """The block for one composed user turn, remembered for its delegations.

    A question with no date expression reads no policy and gives ``""``. The
    policy is the TTL snapshot (:func:`calendar_policy.load_policy`) unless
    one is passed. Fail-soft: any failure gives ``""`` and remembers nothing."""
    remember_turn_windows("")
    try:
        text, _scope = _question_text(prompt)
        if len(text) <= MAX_SCAN_CHARS and not find_expressions(text):
            return ""
        if policy is None:
            policy = cp.load_policy()
        resolved = resolve(prompt, policy, now=now)
        full, compact = render_for_turn(resolved)
        if full:
            remember_turn_windows(full, compact)
            logger.info("RESOLVED_TIME_WINDOWS expressions=%d windows=%d unresolved=%d "
                        "overflow=%s", len(resolved),
                        sum(len(item.windows) for item in resolved),
                        sum(len(item.unresolved) for item in resolved),
                        any(item.category == "overflow" for item in resolved))
        return full
    except Exception as exc:  # noqa: BLE001 - additive; never break a turn
        logger.warning("resolved time windows skipped category=%s", type(exc).__name__)
        remember_turn_windows("")
        return ""


def _is_query_tool(tool_name: str) -> bool:
    tool = tool_name.split("___", 1)[1] if "___" in tool_name else tool_name
    return tool.startswith("query_") or "_query_" in tool


_HEADER_LINE_RE = re.compile(r"(?m)^" + re.escape(HEADER) + r"$")


def carries_block(text: object) -> bool:
    """True when ``text`` (a delegation's ``kg_context``) carries this turn's
    block, or the note sent in its place, as a line of its own."""
    return isinstance(text, str) and _HEADER_LINE_RE.search(text) is not None


#: Sent to a delegation whose ``kg_context`` has no room for the windows:
#: never a cut block, and never silence that would let the agent derive them.
NO_ROOM_NOTE = (f"{HEADER}\nThe engine resolved the dates in the user's question, but the "
                "windows do not fit in this context. Use the UTC start and end the question "
                "gives; do not derive a window from a date phrase yourself. If the question "
                "names a date or period without its UTC start and end, say so.")
#: Ends a model-written ``kg_context`` cut short to make room for the note.
TRIM_MARKER = "[context trimmed to fit the time-window note]"


def _with_note(base: str) -> "tuple[str, bool]":
    """``(kg_context, trimmed)``: ``base`` and :data:`NO_ROOM_NOTE` within the
    system agent's bound. The note is never cut: when it does not fit, the
    model-written ``base`` is cut from the end just enough to fit the note
    after a :data:`TRIM_MARKER` line."""
    tail = "\n\n" + NO_ROOM_NOTE
    if len(base) + len(tail) <= KG_CONTEXT_MAX_CHARS:
        return base + tail, False
    keep = KG_CONTEXT_MAX_CHARS - len(tail) - len(TRIM_MARKER) - 1
    return base[:max(0, keep)] + "\n" + TRIM_MARKER + tail, True


def with_kg_context_windows(tool_name: object, arguments, windows: tuple):
    """``arguments`` with this turn's block added to a system query's
    ``kg_context`` (after what the model passed, or as the whole argument when
    it passed none): the full block, else the compact one, whichever fits the
    system agent's bound. When neither fits, :data:`NO_ROOM_NOTE` always lands
    whole, and the model-written context is trimmed from the end to make room
    for it (:func:`_with_note`, logged). ``arguments`` itself when there is
    nothing to add or the tool is not a system query tool. The caller's dict
    is never mutated."""
    full, compact = windows if isinstance(windows, tuple) and len(windows) == 2 else ("", "")
    if not full or not isinstance(arguments, dict) or not isinstance(tool_name, str):
        return arguments
    if not _is_query_tool(tool_name):
        return arguments
    existing = arguments.get("kg_context")
    base = existing if isinstance(existing, str) and existing.strip() else ""
    separator = "\n\n" if base else ""
    merged = dict(arguments)
    for block in (full, compact):
        if len(base) + len(separator) + len(block) <= KG_CONTEXT_MAX_CHARS:
            merged["kg_context"] = base + separator + block
            return merged
    if not base:
        merged["kg_context"] = NO_ROOM_NOTE
        return merged
    merged["kg_context"], trimmed = _with_note(base)
    logger.info("RESOLVED_TIME_WINDOWS_NO_ROOM kg_context=%d block=%d note=sent trimmed=%s",
                len(base), len(compact), trimmed)
    return merged
