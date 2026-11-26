# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Date expressions in a question, found deterministically (no model).

:func:`find_expressions` returns every date expression in the text, in
question order: M/D, M/D/YY, M/D/YYYY, YYYY-MM-DD, month-name forms
(``Aug 20``, ``August 20th``, ``20 August 2025``, ``July 17-28``), inclusive
ranges (``A-B``, ``A to/through/until B``, ``between A and B``, ``from A to
B``), ``since A``, today, yesterday, this week, last week, the past week and
the past N days or weeks. A written date keeps its year as written (a
two-digit year is stored as 20YY and flagged); turning an expression into
days and UTC windows is :mod:`agents.time_windows`.

Anything not read with certainty is not an expression: a fraction (``1/2 of``),
a date with a time of day, ``before``/``after``/``until``/``as of`` a date,
``the day before yesterday``, a bare month, the verb "may", and record ids.
Pure; stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Longest question scanned (the caller treats a longer one as unresolvable).
MAX_SCAN_CHARS = 20000
#: Longest range accepted, in days (a wider one is left alone).
MAX_SPAN_DAYS = 400
_PHRASE_CAP = 60


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
          r"aug(?:ust)?|sept(?:ember)?|sep|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_ORD = r"(?:st|nd|rd|th)?"
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}

_DATE_RE = re.compile(
    r"(?<![\w/.\-])(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})(?![\w/\-]|[.:]\d)"
    r"|(?<![\w/.])(?<![A-Za-z_]-)(?P<sm>\d{1,2})/(?P<sd>\d{1,2})(?:/(?P<sy>\d{4}|\d{2}))?"
    r"(?![\w/]|[.:]\d)"
    rf"|\b(?P<mm>{_MONTH})\.?\s+(?P<md>\d{{1,2}}){_ORD}"
    rf"(?:\s*-\s*(?P<md2>\d{{1,2}}){_ORD}(?![\w/]))?(?:,?\s+(?P<my>\d{{4}}))?(?![\w/])"
    rf"|\b(?P<dd>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<dm>{_MONTH})\.?(?:,?\s+(?P<dy>\d{{4}}))?\b",
    re.IGNORECASE)
_TIME_AFTER = re.compile(r"\s*,?\s*(?:at\s+)?\d{1,2}(?::\d{2}|\s*[ap]\.?m\b)", re.IGNORECASE)
_T_TIME_AFTER = re.compile(r"T\d")
_FRACTION_AFTER = re.compile(r"\s*(?:of\b|%)", re.IGNORECASE)
_RANGE_JOIN = re.compile(r"^\s*(?:-|\u2013|\u2014|to|through|thru|until|till)\s*$",
                         re.IGNORECASE)
_AND_JOIN = re.compile(r"^\s+and\s+$", re.IGNORECASE)
_BETWEEN_BEFORE = re.compile(r"\bbetween\s+$", re.IGNORECASE)
_FROM_BEFORE = re.compile(r"\bfrom\s+$", re.IGNORECASE)
_SINCE_BEFORE = re.compile(r"\b(?:since|starting(?:\s+on|\s+from)?|beginning)\s+(?:on\s+)?$",
                           re.IGNORECASE)
#: A date these words lead into is a bound the parser does not resolve.
_OPEN_BOUND_BEFORE = re.compile(
    r"\b(?:before|after|until|till|by|through|thru|prior\s+to|as\s+of|ending|"
    r"day\s+before|day\s+after|later\s+than|earlier\s+than)\s+(?:on\s+)?(?:the\s+)?$",
    re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"\b(?P<today>today)\b"
    r"|\b(?P<yesterday>yesterday)\b"
    r"|\b(?P<thisweek>this\s+week)\b"
    r"|\b(?P<lastweek>(?:last|previous)\s+week)\b"
    r"|\b(?P<pastweek>past\s+week)\b"
    r"|\b(?:past|last|previous)\s+(?P<n>\d{1,3}|one|two|three|four|five|six|seven|eight|"
    r"nine|ten|eleven|twelve)\s+(?P<unit>days?|weeks?)\b",
    re.IGNORECASE)


@dataclass(frozen=True)
class _DateToken:
    start: int
    end: int
    year: "int | None"
    month: int
    day: int
    #: Same-month shorthand end day ("July 17-28").
    day2: "int | None" = None
    #: The year was written with two digits (stored as 20YY).
    two_digit: bool = False


@dataclass(frozen=True)
class DateExpression:
    """One expression found in a question. ``kind`` is ``date``, ``range``,
    ``since`` or ``relative``; positions index the scanned text."""

    phrase: str
    start: int
    end: int
    kind: str
    first: "tuple | None" = None     # (year or None, month, day, two-digit year)
    last: "tuple | None" = None
    relative: str = ""               # today | yesterday | this_week | last_week | days
    count: int = 0                   # days, for ``relative == "days"``


def _date_tokens(text: str) -> list:
    tokens = []
    for match in _DATE_RE.finditer(text):
        token = _token_from_match(text, match)
        if token is not None:
            tokens.append(token)
    return tokens


def _token_from_match(text: str, match) -> "_DateToken | None":
    groups = match.groupdict()
    after = text[match.end():]
    if _TIME_AFTER.match(after) or _T_TIME_AFTER.match(after):
        return None                     # an explicit time of day: not a day window
    if groups["iy"]:
        return _DateToken(match.start(), match.end(), int(groups["iy"]),
                          int(groups["im"]), int(groups["id"]))
    if groups["sm"]:
        if _FRACTION_AFTER.match(after):
            return None                 # "1/2 of", "3/4%": a fraction
        year = groups["sy"]
        two_digit = bool(year) and len(year) == 2
        if two_digit:
            year = "20" + year
        return _DateToken(match.start(), match.end(), int(year) if year else None,
                          int(groups["sm"]), int(groups["sd"]), two_digit=two_digit)
    word = groups["mm"] or groups["dm"]
    has_year = bool(groups["my"] or groups["dy"])
    has_ordinal = bool(re.search(r"\d(?:st|nd|rd|th)", match.group(0), re.IGNORECASE))
    if word.lower() == "may" and word != "May" and not (has_year or has_ordinal):
        return None                     # the verb "may", not the month
    month = _MONTHS[word.lower().rstrip(".")]
    if groups["mm"]:
        year = groups["my"]
        return _DateToken(match.start(), match.end(), int(year) if year else None,
                          month, int(groups["md"]),
                          int(groups["md2"]) if groups["md2"] else None)
    year = groups["dy"]
    return _DateToken(match.start(), match.end(), int(year) if year else None,
                      month, int(groups["dd"]))


def _phrase(text: str, start: int, end: int) -> str:
    raw = " ".join(text[start:end].split())
    raw = raw.replace("\u2013", "-").replace("\u2014", "-").replace('"', "'")
    return raw[:_PHRASE_CAP]


def _ymd(token: _DateToken) -> tuple:
    return (token.year, token.month, token.day, token.two_digit)


def _range_expressions(text: str, tokens: list, taken: set) -> list:
    out = []
    for left, right in zip(tokens, tokens[1:]):
        if left.start in taken or right.start in taken or left.day2 is not None:
            continue
        between = text[left.end:right.start]
        before = text[:left.start]
        start = None
        if _AND_JOIN.match(between) and _BETWEEN_BEFORE.search(before):
            start = _BETWEEN_BEFORE.search(before).start()
        elif _RANGE_JOIN.match(between):
            found = _FROM_BEFORE.search(before)
            start = found.start() if found else left.start
            if not found and _OPEN_BOUND_BEFORE.search(before):
                continue
        if start is None:
            continue
        taken.update({left.start, right.start})
        out.append(DateExpression(_phrase(text, start, right.end), start, right.end,
                                  "range", _ymd(left), _ymd(right)))
    return out


def _single_expression(text: str, token: _DateToken) -> "DateExpression | None":
    before = text[:token.start]
    if token.day2 is not None:
        return DateExpression(_phrase(text, token.start, token.end), token.start,
                              token.end, "range", _ymd(token),
                              (token.year, token.month, token.day2, False))
    since = _SINCE_BEFORE.search(before)
    if since:
        return DateExpression(_phrase(text, since.start(), token.end), since.start(),
                              token.end, "since", _ymd(token))
    if _OPEN_BOUND_BEFORE.search(before):
        return None
    return DateExpression(_phrase(text, token.start, token.end), token.start,
                          token.end, "date", _ymd(token))


def _relative_expressions(text: str, spans: list) -> list:
    out = []
    for match in _RELATIVE_RE.finditer(text):
        if any(s <= match.start() < e or s < match.end() <= e for s, e in spans):
            continue
        before = text[:match.start()]
        if re.search(r"\bday\s+(?:before|after)\s+(?:the\s+)?$", before, re.IGNORECASE):
            continue
        groups = match.groupdict()
        if groups["today"]:
            relative, count = "today", 0
        elif groups["yesterday"]:
            relative, count = "yesterday", 0
        elif groups["thisweek"]:
            relative, count = "this_week", 0
        elif groups["lastweek"]:
            relative, count = "last_week", 0
        elif groups["pastweek"]:
            relative, count = "days", 7
        else:
            raw = groups["n"].lower()
            number = _NUMBER_WORDS.get(raw) or int(raw)
            count = number * (7 if groups["unit"].lower().startswith("week") else 1)
            relative = "days"
            if not 1 <= count <= MAX_SPAN_DAYS:
                continue
        start = match.start()
        kind = "relative"
        since = _SINCE_BEFORE.search(before)
        if since and relative in ("today", "yesterday"):
            start, kind = since.start(), "since"
        elif since or _OPEN_BOUND_BEFORE.search(before):
            continue                    # "since last week", "until today": left alone
        out.append(DateExpression(_phrase(text, start, match.end()), start, match.end(),
                                  kind, relative=relative, count=count))
    return out


def find_expressions(text: object) -> tuple:
    """Every date expression in ``text``, in question order (the first
    :data:`MAX_SCAN_CHARS` characters are scanned). Pure."""
    if not isinstance(text, str) or not text.strip():
        return ()
    text = text[:MAX_SCAN_CHARS]
    tokens = _date_tokens(text)
    taken: set = set()
    found = _range_expressions(text, tokens, taken)
    for token in tokens:
        if token.start in taken:
            continue
        expression = _single_expression(text, token)
        if expression is not None:
            found.append(expression)
    found.extend(_relative_expressions(text, [(e.start, e.end) for e in found]))
    found.sort(key=lambda e: e.start)
    return tuple(found)
