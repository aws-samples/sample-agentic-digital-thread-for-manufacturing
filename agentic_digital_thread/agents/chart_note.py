# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The note ``ChartRedactionHook`` leaves where it took a tool result's charts.

The hook (``agents/orchestrator.py``) replaces every ``<chart-block .../>``
tag in a tool result's text with one bracketed note saying how many charts
were rendered (or, in integrity-enforce mode, withheld), so the model knows
the charts are on screen without copying their data. A rendered note tells
the model what the orchestrator's chart rule tells it: author no chart for
those series, and never mention the note, tool results or how charts are
shown. A notice-only tag (``kind="notice"``, sent when no chart could be
drawn) gets its own note instead, which tells the model no chart was drawn
and to say so in plain words.
The note is written for the model only. The hook edits the live tool-result
message in place, and that message is what the runtime streams to the client
(the model's restored history on a later turn gets a neutral note instead,
:func:`earlier_note`), so the activity-trace summarizer
(``tool_result_summary``) and the cloud stream's result preview
(``chat_client``) remove it again with ``strip_chart_notes``.
Memory keeps the original tags, not the note: the session manager stores the
message before the hook runs, and a Recent sessions reload rebuilds the
charts from that stored copy.

The note is text the system writes, so it carries no chart title. Titles hold
external display names and channel labels, and any of those can read like an
instruction; inside system-authored text the model has no way to tell them
apart from the system's own words. The titles still reach the user: the hook
queues the original tags and the page draws them. A notice note quotes the
notice only when it is exactly the fixed reason a tool writes
(:func:`unnamed_series_notice`), never other notice text.

What a tool left out reaches the model as numbers only. A chart tool that
drew fewer charts than it was asked for, did not chart some series because no
name was recorded, or charted fewer assets than a read returned, puts those
counts on its first chart's tag as digit-only attributes
(:func:`omission_attributes`); the page may also say so in that chart's
title. The note reads them strictly (:func:`omission_counts`) and states them
in fixed words, never with an omitted chart's or asset's name, then tells
the model to say how many were not charted, from those counts alone, and to
chart none of them (:data:`_COUNTED_CLAUSE`). The hook
counts each distinct tag once, as delivery does, so the note describes the
charts the page gets.

The note text is built here and nowhere else, so the stripper always matches
what the hook wrote. Notes stored before titles were left out quoted each
title in double quotes (a double quote inside one written as ``&quot;``), and
memory still holds them, so the stripper also matches that form: it reads
the note as a whole, from its opening phrase through the quoted titles to its
fixed closing sentence, so nothing a stored title holds can end the match
early or start one inside it. Rendered and notice notes stored before the
current closing sentences (:data:`_RENDERED_BODIES_BEFORE`,
:data:`_NOTICE_BODIES_BEFORE`), and counted notes stored without
:data:`_COUNTED_CLAUSE`, are matched the same way.
"""
from __future__ import annotations

import re

_RENDERED_HEAD = "chart(s) ALREADY RENDERED to the user"
_RENDERED_BODY = (". Author no chart for their series, and never mention this note, a tool "
                  "result, how charts are shown or a rule about charts; at most say 'the "
                  "chart above shows'. Never reproduce or re-emit chart markup.]")
#: The closing sentences rendered notes carried before :data:`_RENDERED_BODY`.
#: Memory still holds notes in that wording, so the stripper matches it too.
_RENDERED_BODIES_BEFORE = (
    ". Chart data is displayed client-side and has been withheld from you intentionally "
    "\u2014 reference the charts in prose, never attempt to reproduce or re-emit chart markup.]",
)
_WITHHELD_HEAD = "chart(s) withheld and NOT delivered to the user (integrity-enforce mode)"
_WITHHELD_BODY = (". Do not reference these charts in your answer and never "
                  "attempt to reproduce or re-emit chart markup.]")
#: The note for a notice-only tag (``kind="notice"``): the tool drew no chart
#: and says why. The same in both integrity modes, and it carries no count:
#: nothing was rendered. It tells the model what the orchestrator's telemetry
#: chart rule does: say the readings are not charted, and chart none.
_NOTICE_HEAD = "No chart was drawn for these readings"
_NOTICE_BODY = (". Give these readings in prose, say in plain words they are not charted "
                "(with the count above when there is one) and build no chart; never "
                "reproduce or re-emit chart markup.]")
#: The closing sentences notice notes carried before :data:`_NOTICE_BODY`.
_NOTICE_BODIES_BEFORE = (
    ". Do not describe a chart or refer to one for these readings; give the readings in "
    "prose and never attempt to reproduce or re-emit chart markup.]",
)
#: The note an EARLIER turn's tool result carries in the model's restored
#: history (``orchestrator._wrap_list_messages``). The stored message does not
#: say whether those charts reached the page (an integrity-enforce turn sent
#: none, and a reopened chat may not show them), so the note claims neither
#: rendered nor withheld and tells the model not to say they are on screen.
_EARLIER_HEAD = "chart(s) were in this earlier tool result and are not reproduced here"
_EARLIER_BODY = (". Do not say or imply they are on screen, author no chart for their "
                 "series, and never reproduce or re-emit chart markup.]")

#: The fixed reason a tool writes into a notice when series had no recorded
#: name. The notice note quotes a notice only when it is exactly this text.
_UNNAMED_SERIES_NOTICE = "{count} series not charted: no name recorded."
_FIXED_NOTICE_RE = re.compile(r"[0-9]{1,9} series not charted: no name recorded\.")

#: The omission counts a chart tool may put on a drawn chart's tag, in the
#: order the note states them: the charts it left out at its chart limit, the
#: series it did not chart because no name was recorded, and the assets it
#: did not chart at its asset limit. A value is 1 to 6 ASCII digits.
CHARTS_NOT_DRAWN_ATTR = "charts_not_drawn"
SERIES_NOT_NAMED_ATTR = "series_not_named"
ASSETS_NOT_CHARTED_ATTR = "assets_not_charted"
_COUNT_LIMIT = 999_999
_COUNT_VALUE_RE = re.compile(r"[0-9]{1,6}")
#: Each count's attribute and how the note states it, between the note's
#: head and its closing sentences.
_OMISSIONS = (
    (CHARTS_NOT_DRAWN_ATTR, "; {count} more chart(s) were not drawn (chart limit)"),
    (SERIES_NOT_NAMED_ATTR, "; {count} series were not charted (no name recorded)"),
    (ASSETS_NOT_CHARTED_ATTR, "; {count} more asset(s) were not charted (asset limit)"),
)
_COUNT_ATTR_RE = re.compile(
    r"\s(" + "|".join(name for name, _ in _OMISSIONS) + r')="([^"]*)"')
#: What a rendered note that states counts tells the model to do with them:
#: say how many were left out, from the counts alone (the note carries no
#: names by design), and chart none of them. Placed after the counts.
_COUNTED_CLAUSE = (". Say in plain words how many were not charted, from these counts, "
                   "never by name, and chart none of them")

#: After a head, the titles a stored note quoted: ": " then one quoted title,
#: then any more separated by "; ". A title holds no double quote, so the
#: closing quote of each one is unambiguous. A current note has none.
_QUOTED_TITLES = r'(?:: (?:"[^"]*"(?:; "[^"]*")*)?)?'


def _clause_pattern(clause: str) -> str:
    before, after = clause.split("{count}")
    return "(?:" + re.escape(before) + "[0-9]{1,9}" + re.escape(after) + ")?"


#: After a head, the omission counts a current note may state.
_COUNT_CLAUSES = "".join(_clause_pattern(clause) for _, clause in _OMISSIONS)

#: A rendered note's closing sentences, in the current wording or an earlier one.
_RENDERED_BODY_PATTERN = (
    "(?:" + "|".join(re.escape(body) for body in (_RENDERED_BODY, *_RENDERED_BODIES_BEFORE))
    + ")")
#: A notice note's closing sentences, in the current wording or an earlier one.
_NOTICE_BODY_PATTERN = (
    "(?:" + "|".join(re.escape(body) for body in (_NOTICE_BODY, *_NOTICE_BODIES_BEFORE))
    + ")")

_NOTE_RE = re.compile(
    r"\[(?:(\d+) (?:"
    + "(?P<rendered>" + re.escape(_RENDERED_HEAD) + ")" + _QUOTED_TITLES + _COUNT_CLAUSES
    + "(?:" + re.escape(_COUNTED_CLAUSE) + ")?" + _RENDERED_BODY_PATTERN
    + "|(?P<withheld>" + re.escape(_WITHHELD_HEAD) + ")" + _QUOTED_TITLES + _COUNT_CLAUSES
    + re.escape(_WITHHELD_BODY)
    + "|(?P<earlier>" + re.escape(_EARLIER_HEAD) + ")" + re.escape(_EARLIER_BODY)
    + ")"
    + "|(?P<notice>" + re.escape(_NOTICE_HEAD) + ")" + _QUOTED_TITLES
    + _NOTICE_BODY_PATTERN
    + ")"
)


def unnamed_series_notice(count: int) -> str:
    """The fixed notice reason for ``count`` series that had no name."""
    return _UNNAMED_SERIES_NOTICE.format(count=int(count))


def _bounded(value) -> int:
    """``value`` as a count the note may state, ``0`` outside 1 to 999999."""
    value = int(value)
    return value if 0 < value <= _COUNT_LIMIT else 0


def omission_attributes(charts_not_drawn: int = 0, series_not_named: int = 0,
                        assets_not_charted: int = 0) -> str:
    """The attribute text a chart tool appends to a drawn chart's tag for what
    it left out, ``""`` when it left nothing out. Counts outside 1 to
    999999 are left off."""
    values = (charts_not_drawn, series_not_named, assets_not_charted)
    return "".join(f' {name}="{_bounded(value)}"'
                   for (name, _), value in zip(_OMISSIONS, values) if _bounded(value))


def omission_counts(tags: "list[str]") -> "tuple[int, int, int]":
    """``(charts_not_drawn, series_not_named, assets_not_charted)`` summed
    over ``tags``.

    A value counts only when it is 1 to 6 ASCII digits in double quotes, and
    an attribute given more than once in one tag is ignored for that tag, so
    nothing but a bounded number can come out of a tag. Each sum is capped at
    999999."""
    totals = {name: 0 for name, _ in _OMISSIONS}
    for tag in tags:
        found: "dict[str, list[str]]" = {}
        for name, value in _COUNT_ATTR_RE.findall(str(tag)):
            found.setdefault(name, []).append(value)
        for name, values in found.items():
            if len(values) == 1 and _COUNT_VALUE_RE.fullmatch(values[0]):
                totals[name] += int(values[0])
    return tuple(min(totals[name], _COUNT_LIMIT) for name, _ in _OMISSIONS)


def _count_clauses(counts) -> str:
    return "".join(clause.format(count=_bounded(value))
                   for (_, clause), value in zip(_OMISSIONS, counts) if _bounded(value))


def rendered_note(count: int, *omitted: int) -> str:
    """The note for ``count`` charts the runtime streams to the user itself,
    with the omission counts (in ``omission_counts`` order) when there are
    any, followed by :data:`_COUNTED_CLAUSE`."""
    counts = _count_clauses(omitted)
    counted = _COUNTED_CLAUSE if counts else ""
    return f"[{int(count)} {_RENDERED_HEAD}{counts}{counted}{_RENDERED_BODY}"


def withheld_note(count: int, *omitted: int) -> str:
    """The note for ``count`` charts integrity-enforce mode drops undelivered,
    with the omission counts (in ``omission_counts`` order) when there are
    any."""
    return f"[{int(count)} {_WITHHELD_HEAD}{_count_clauses(omitted)}{_WITHHELD_BODY}"


def earlier_note(count: int) -> str:
    """The note for ``count`` charts an earlier turn's tool result held, in
    the model's restored history: neither rendered nor withheld."""
    return f"[{int(count)} {_EARLIER_HEAD}{_EARLIER_BODY}"


def notice_note(notices: "list[str]") -> str:
    """The note for notice-only tags: no chart was drawn. It quotes each
    notice that is exactly a fixed reason a tool writes (for example how many
    series had no name) and leaves any other notice text out."""
    fixed = [n for n in notices if isinstance(n, str) and _FIXED_NOTICE_RE.fullmatch(n)]
    quoted = "; ".join(f'"{n}"' for n in fixed)
    return f"[{_NOTICE_HEAD}{': ' + quoted if quoted else ''}{_NOTICE_BODY}"


def strip_chart_notes(text: str) -> "tuple[str, int, int]":
    """``text`` without the hook's notes, current or stored in the older form.

    Returns ``(text, rendered, withheld)``: the text with every note removed
    (and, when one was, the blank space it leaves at either end), and how
    many charts the removed notes said were rendered and withheld. A notice
    note counts as neither: it says no chart was drawn; nor does the note an
    earlier turn's result carries in restored history (:func:`earlier_note`).
    Text with no note comes back unchanged with ``(text, 0, 0)``.
    """
    if not isinstance(text, str) or "chart markup.]" not in text:
        return text, 0, 0
    counts = {"rendered": 0, "withheld": 0, "notice": 0}

    def _drop(match: "re.Match") -> str:
        if match.group("notice") or match.group("earlier"):
            counts["notice"] += 1
        else:
            counts["rendered" if match.group("rendered") else "withheld"] += int(match.group(1))
        return ""

    stripped = _NOTE_RE.sub(_drop, text)
    if counts["notice"]:
        stripped = stripped.strip()
    return stripped, counts["rendered"], counts["withheld"]
