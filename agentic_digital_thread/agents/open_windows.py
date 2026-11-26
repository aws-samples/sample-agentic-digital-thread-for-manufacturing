# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A zero over a time window that had not ended: rows for it may not be in the table yet.

Measured in a dry run on an analytics deployment: a site's counts for its
working day, each windowed from that day's start to the next day's, came back
0 for every state about nine hours into the day. The table's newest row for
that site was from before the window started (the table ran about ten hours
behind), so the zeros meant that no rows for the window had landed yet. The
agent's one query for a newest time was bounded by the same window, so it had
nothing to read, and the answer reported no records and no newest time. A
steward requirement to give the newest row time for a window that runs up to
now reached only the final answer, which had no such time to give.

* :func:`found` reads one result of one query. The query is one SELECT of one
  table: no join, expansion, subquery, CTE, set operation, HAVING, sample or
  snapshot. The result is complete (not cut by rows, pages, the engine's row
  limit or the query's own LIMIT or OFFSET) and shows that no row matched the
  query's WHERE: it has no rows, or it is one ungrouped row with a
  ``COUNT(*)`` (or ``COUNT(1)``) output of 0 (a zero), or one whose ``MIN`` or
  ``MAX`` of a windowed column is NULL (an earliest or newest time that is
  not there). Each column a top-level WHERE conjunct bounds from below with a
  time literal is a window (``>``, ``>=``, ``BETWEEN``; the upper bound from
  ``<``, ``<=``, ``BETWEEN``). One result is one observation, however many
  columns its query windows (at most :data:`MAX_COLUMNS`): its zero is one
  zero over all of them. It is read only when every window had not ended when
  the query was evaluated: each has no upper bound, or an upper bound after
  the time the result was in hand (an inclusive one: not before it), which is
  on or after the time the engine evaluated the query; and each is not empty
  by its own bounds and started at most :data:`MAX_ELAPSED` before the query
  was sent (the result's ``queried_at``). In the recorded dry runs the table
  ran hours behind, and every zero over a window that had started about two
  weeks earlier came from a filter that matched nothing, not from rows still
  to land. A query with any other top-level conjunct that names a windowed
  column (a bound under an OR, on a wrapped column, by a clock function, or
  one not read below) is not read.
* :func:`note` is that result's note; :func:`engine_line` and
  :func:`requirement_block` are the answer's engine line and FINAL-ANSWER
  REQUIREMENTS block, for the zeros only: a missing time is a figure no answer
  reports, and the note already sends the agent to the newest time without
  the window. Results are never merged: several single-window queries of one
  table over one window share an entry that counts them. Each text has a
  character bound (:data:`MAX_NOTE_CHARS`, ``executed_filters.MAX_LINE_CHARS``,
  :data:`MAX_BLOCK_CHARS`): the line and the block drop whole entries and
  count the queries left out, and a note past its bound names no column.
  :func:`neutralize` makes a model-written copy of the line inert.

A time literal is ``TIMESTAMP '...'``, ``DATE '...'``, ``from_iso8601_timestamp``,
``from_iso8601_date`` or ``date`` of one string, ``CAST('...' AS TIMESTAMP|DATE)``,
or an ISO 8601 string. A literal with no zone is read as UTC, the engine's
session zone. A bound is read as written, never moved. An inclusive upper
bound (``<=``, ``BETWEEN``) at day precision (a DATE literal, or a text with no
time of day) is not read: on a DATE column it covers that whole day and on a
TIMESTAMP column it stops at its midnight, and the column's type is not known
here. A table or column name is printed only when ``engine_names.printable``
passes. Generic: no column, table or system names. Reads SQL with the shared
tokenizer; issues no query and does no I/O.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] Open window"
#: Words the orchestrator prompt keys the requirement block on.
MARKER = "a zero over a window that had not ended"
#: The most entries the engine line and the block consider.
_MAX_SHOWN = 6
#: The most time columns one query may window for its result to be read, and
#: the most queries one entry of the engine line lists.
MAX_COLUMNS = 3
#: The longest a window may have run before the query for its zero to be marked.
MAX_ELAPSED = timedelta(days=1)
#: The longest note one result carries; past it the note names no column.
MAX_NOTE_CHARS = 1_200
#: The longest FINAL-ANSWER REQUIREMENTS block, fences included: twice the
#: engine line's bound (``executed_filters.MAX_LINE_CHARS``).
MAX_BLOCK_CHARS = 2_400
_COLUMN_PLACEHOLDER = "col"
_DAY_PARSERS = frozenset({"from_iso8601_date", "date"})
_TIME_PARSERS = frozenset({"from_iso8601_timestamp", "timestamp"}) | _DAY_PARSERS
_NOT_ONE_TABLE = frozenset({"JOIN", "UNNEST", "LATERAL"})
_ISO = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<hm>\d{2}:\d{2})(?::(?P<s>\d{2})"
                  r"(?:\.(?P<f>\d{1,9}))?)?)?(?: ?(?P<zone>UTC|Z|[+-]\d{2}:?\d{2}))?")
_RESULTS = itertools.count(1)


@dataclass(frozen=True)
class OpenWindow:
    """One result of ``table`` (as written) that matched no rows over the
    windows of its query, none of which had ended when it ran at ``ran_at``.
    ``windows`` holds ``(column as written, start, end, end inclusive)`` per
    windowed column, in the order the query bounds them (``end`` ``""``: no
    end); each time is ``YYYY-MM-DD HH:MM:SS UTC``. ``zero``: the result is a
    zero (a count of 0 or no rows), not only a missing earliest or newest
    time. ``source`` (the full relation, case-folded) and ``result`` (this
    result's identity) are never printed: two results are never one."""

    table: str
    windows: tuple
    ran_at: str
    zero: bool = True
    source: str = ""
    result: str = ""

    def to_dict(self, names=None) -> dict:
        """The record the result carries (``open_window``); a name that is not
        a plain identifier is a placeholder. At most :data:`MAX_COLUMNS`
        windows, each name at most ``engine_names.MAX_CHARS``."""
        from .engine_names import Withheld
        names = names if names is not None else Withheld()
        out = {"windows": [{"column": names.show(column), "start": start, "end": end or None,
                            "end_inclusive": bool(end) and inclusive}
                           for column, start, end, inclusive in self.windows],
               "ran_at": self.ran_at, "zero": self.zero}
        if names.count:
            out["names_withheld"] = names.count
        return out


# --------------------------------------------------------------------------- #
# Reading the SQL
# --------------------------------------------------------------------------- #
def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _string(token) -> str | None:
    if token.kind != "literal" or len(token.text) < 2:
        return None
    return token.text[1:-1].replace("''", "'")


def _moment(text: str) -> datetime | None:
    """The UTC instant an ISO 8601 date or timestamp text names (no zone: UTC),
    else ``None``."""
    match = _ISO.fullmatch(text.strip())
    if match is None:
        return None
    zone = match.group("zone") or "UTC"
    offset = "+00:00" if zone in {"UTC", "Z"} else zone[:3] + ":" + zone[-2:]
    fraction = (match.group("f") or "").ljust(6, "0")[:6]
    clock = f"{match.group('hm') or '00:00'}:{match.group('s') or '00'}.{fraction}"
    try:
        moment = datetime.fromisoformat(f"{match.group('date')}T{clock}{offset}")
    except ValueError:
        return None
    return moment.astimezone(timezone.utc)


def _literal(tokens):
    """``(instant, at day precision)`` of a time literal (see the module
    docstring), else ``None``. Day precision: a DATE literal, or a text with no
    time of day."""
    from .athena_scope import _matching_close, _strip_outer
    tokens = _strip_outer(tokens)
    text, day = None, False
    if len(tokens) == 1:
        text = _string(tokens[0])
    elif len(tokens) == 2 and _upper(tokens[0]) in {"DATE", "TIMESTAMP"}:
        text, day = _string(tokens[1]), _upper(tokens[0]) == "DATE"
    elif (len(tokens) == 4 and tokens[0].kind == "word" and tokens[1].text == "("
          and tokens[0].value.lower() in _TIME_PARSERS and tokens[3].text == ")"):
        text, day = _string(tokens[2]), tokens[0].value.lower() in _DAY_PARSERS
    elif (len(tokens) >= 6 and _upper(tokens[0]) == "CAST" and tokens[1].text == "("
          and _matching_close(tokens, 1) == len(tokens) - 1 and _upper(tokens[3]) == "AS"
          and _upper(tokens[4]) in {"DATE", "TIMESTAMP"}
          and all(t.kind == "word" for t in tokens[4:-1])):
        text, day = _string(tokens[2]), _upper(tokens[4]) == "DATE" and len(tokens) == 6
    moment = _moment(text) if text is not None else None
    if moment is None:
        return None
    return moment, day or _ISO.fullmatch(text.strip()).group("hm") is None


def _bound(part):
    """``(column, low, high, high inclusive)`` a WHERE conjunct gives one
    column with time literals, each end an instant or ``None``, else ``None``
    (also for an inclusive upper bound at day precision)."""
    from .athena_exact_shape import _column_ref
    base = min(t.depth for t in part)
    words = [(i, _upper(t)) for i, t in enumerate(part) if t.depth == base]
    if any(word == "NOT" for _i, word in words):
        return None
    between = next((i for i, word in words if word == "BETWEEN"), None)
    if between is not None:
        and_at = next((i for i, word in words if i > between and word == "AND"), None)
        low = _literal(part[between + 1:and_at]) if and_at else None
        high = _literal(part[and_at + 1:]) if and_at else None
        column = _column_ref(part[:between])
        if column is None or low is None or high is None or high[1]:
            return None
        return column, low[0], high[0], True
    ops = [i for i, t in enumerate(part) if t.depth == base and t.kind == "operator"]
    if len(ops) != 1 or part[ops[0]].text not in {"<", "<=", ">", ">="}:
        return None
    at, op = ops[0], part[ops[0]].text
    column, value = _column_ref(part[:at]), _literal(part[at + 1:])
    if column is None or value is None:
        column, value = _column_ref(part[at + 1:]), _literal(part[:at])
        op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[op]
    if column is None or value is None:
        return None
    if op in {">", ">="}:
        return column, value[0], None, False
    if op == "<=" and value[1]:
        return None
    return column, None, value[0], op == "<="


def _names_column(part, column: str) -> bool:
    """True when a conjunct names ``column`` (lower case) anywhere."""
    return any(t.kind in {"word", "identifier"} and str(t.value).lower() == column
               for t in part)


def _windows(tokens, relation) -> dict:
    """``{column (lower case): [as written, start, end, end inclusive]}`` for
    each column the query's top-level WHERE bounds from below with a time
    literal (``end`` ``None``: no upper bound), else ``{}``: none, or another
    top-level conjunct names a windowed column (a bound not read)."""
    from .athena_scope import _split_boolean, _strip_outer, _where_expression
    where = _where_expression(tokens, relation)
    out: dict = {}
    unread = []
    for part in (_split_boolean(where, "AND") if where else []):
        part = _strip_outer(part)
        if not part:
            continue
        found = _bound(part) if len(_split_boolean(part, "OR")) == 1 else None
        if found is None:
            unread.append(part)
            continue
        column, low, high, inclusive = found
        entry = out.setdefault(column.lower(), [column, None, None, False])
        if low is not None:
            entry[1] = low if entry[1] is None else max(entry[1], low)
        if high is not None and (entry[2] is None or high < entry[2]):
            entry[2], entry[3] = high, inclusive
        elif high is not None and high == entry[2]:
            entry[3] = entry[3] and inclusive
    windows = {key: entry for key, entry in out.items() if entry[1] is not None}
    if any(_names_column(part, key) for part in unread for key in windows):
        return {}
    return windows


def _one_table(sql):
    """``(tokens, the one relation)`` when ``sql`` is one SELECT of one table
    with no join, expansion, HAVING, sample or snapshot, else ``None``."""
    from .empty_lists import _clauses, _relation, _sampled, _select
    tokens = _select(sql)
    if tokens is None or any(_upper(t) in _NOT_ONE_TABLE for t in tokens):
        return None
    relation = _relation(tokens)
    if relation is None or _sampled(tokens) or "HAVING" in _clauses(tokens):
        return None
    return tokens, relation


def _count_star(expr) -> bool:
    return (len(expr) == 4 and _upper(expr[0]) == "COUNT" and expr[1].text == "("
            and (expr[2].text == "*" or expr[2].kind == "number") and expr[3].text == ")")


def _extreme_of(expr) -> str | None:
    """The column of ``MIN(<column>)`` or ``MAX(<column>)``, else ``None``."""
    from .athena_exact_shape import _column_ref
    from .athena_scope import _matching_close
    if (len(expr) < 4 or _upper(expr[0]) not in {"MIN", "MAX"} or expr[1].text != "("
            or _matching_close(expr, 1) != len(expr) - 1):
        return None
    column = _column_ref(expr[2:-1])
    return column.lower() if column else None


def _is_zero(cell) -> bool:
    return str(cell).strip() in {"0", "0.0"} if cell is not None else False


def _no_row_matched(tokens, result, windowed) -> str:
    """How the complete ``result`` shows that no row matched the WHERE:
    ``"zero"`` (no rows, or one ungrouped row whose ``COUNT(*)`` is 0),
    ``"missing"`` (one ungrouped row whose ``MIN`` or ``MAX`` of a windowed
    column is NULL, with no ``COUNT(*)``), else ``""`` (any other such output
    says a row matched)."""
    from .athena_scope import _strip_outer
    from .empty_lists import _clauses, _outputs
    rows = result["rows"]
    if not rows:
        return "zero"
    clauses = _clauses(tokens)
    if len(rows) != 1 or "GROUP" in clauses:
        return ""
    outputs = [_strip_outer(expr) for expr in _outputs(tokens, clauses)]
    row = rows[0]
    if not isinstance(row, list) or len(outputs) != len(row) \
            or len(outputs) != len(result.get("columns") or []):
        return ""
    counts, extremes = [], []
    for expr, cell in zip(outputs, row):
        if _count_star(expr):
            counts.append(_is_zero(cell))
        elif _extreme_of(expr) in windowed:
            extremes.append(cell is None)
    if not (counts or extremes) or not all(counts + extremes):
        return ""
    return "zero" if counts else "missing"


def _clock(now) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _ran_at(result, now) -> datetime:
    """When the query was sent: the result's ``queried_at``, else ``now``,
    else the clock."""
    source = result.get("_adt_source")
    text = source.get("queried_at") if isinstance(source, dict) else None
    moment = _moment(text) if isinstance(text, str) else None
    return moment or _clock(now)


def _identity(result) -> str:
    """This result's identity: its evidence id, else a new one."""
    source = result.get("_adt_source")
    evidence = source.get("evidence_id") if isinstance(source, dict) else None
    return evidence if isinstance(evidence, str) and evidence else f"result-{next(_RESULTS)}"


def _not_ended(start, end, inclusive, ran, held) -> bool:
    """True when a window had not ended by ``held`` (the time the result was
    in hand, on or after the engine evaluated the query), is not empty by its
    own bounds and started at most :data:`MAX_ELAPSED` before ``ran``."""
    if ran - start > MAX_ELAPSED:
        return False
    if end is None:
        return True
    return end > start and (end >= held if inclusive else end > held)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def found(sql, result, *, now: datetime | None = None) -> tuple:
    """The one :class:`OpenWindow` of ``sql``'s complete ``result`` when it
    matched no rows over windows none of which had ended (see the module
    docstring), else ``()``. ``now`` is when the result was in hand (default:
    the clock), so the engine evaluated the query no later."""
    from .empty_lists import complete_result
    if not isinstance(result, dict) or "error" in result or not complete_result(result, sql):
        return ()
    read = _one_table(sql)
    if read is None:
        return ()
    tokens, relation = read
    windows = _windows(tokens, relation)
    if not windows or len(windows) > MAX_COLUMNS:
        return ()
    shown_by = _no_row_matched(tokens, result, windows)
    if not shown_by:
        return ()
    ran = _ran_at(result, now)
    held = max(ran, _clock(now))
    if not all(_not_ended(start, end, inclusive, ran, held)
               for _column, start, end, inclusive in windows.values()):
        return ()
    parts = [str(part) for part in relation.parts]
    return (OpenWindow(table=".".join(parts),
                       windows=tuple((column, _utc(start), _utc(end) if end else "",
                                      bool(end) and inclusive)
                                     for column, start, end, inclusive in windows.values()),
                       ran_at=_utc(ran), zero=shown_by == "zero",
                       source=".".join(parts).lower(), result=_identity(result)),)


# --------------------------------------------------------------------------- #
# The note, the engine line and the requirement block: facts, names only when
# printable, each within its bound
# --------------------------------------------------------------------------- #
def _span(start: str, end: str, inclusive: bool) -> str:
    if end:
        return f"from {start} {'through' if inclusive else 'to'} {end}"
    return f"from {start} with no end"


def _listed(shown) -> str:
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def _windowed_on(names, windows) -> str:
    """``on <column> <span>`` for each window; one span said once when all
    windows share it."""
    spans = {_span(start, end, inclusive) for _c, start, end, inclusive in windows}
    if len(windows) > 1 and len(spans) == 1:
        listed = _listed([names.show(column) for column, *_rest in windows])
        return f"on {listed} (each {spans.pop()})"
    return " and ".join(f"on {names.show(column)} {_span(start, end, inclusive)}"
                        for column, start, end, inclusive in windows)


def _sql_column(names, column: str) -> tuple[str, str]:
    """``(the column in SQL, a sentence saying what stands in for it)``."""
    from .engine_names import printable
    if printable(column):
        return f'"{column}"', ""
    names.show(column)
    return _COLUMN_PLACEHOLDER, (f" Here {_COLUMN_PLACEHOLDER} is the windowed column, whose "
                                 "name is withheld.")


def _named_note(window: OpenWindow) -> str:
    """The note naming the table and columns (names gated)."""
    from .engine_names import Withheld
    names = Withheld()
    table = names.show(window.table, "table")
    if len(window.windows) == 1:
        column, start, _end, _inclusive = window.windows[0]
        shown = names.show(column)
        sql_column, stand_in = _sql_column(names, column)
        return (f"no row of {table} matched this query, and its window "
                f"{_windowed_on(names, window.windows)} had not ended when it ran at "
                f"{window.ran_at}: rows for that window may not be in the table yet, so this "
                f"result does not show that nothing happened in it. Give the newest {shown} "
                f"under this query's other filters in your answer (MAX({sql_column}) with "
                f"those filters and no bound on {shown}); when it is before {start}, say the "
                f"table has no rows under those filters for the window yet.{stand_in}"
                f"{names.sentence()}")
    shown = [names.show(column) for column, *_rest in window.windows]
    in_sql = [_sql_column(names, column) for column, *_rest in window.windows]
    stand_in = next((sentence for _sql, sentence in in_sql if sentence), "")
    maxima = _listed([f"MAX({sql})" for sql, _sentence in in_sql])
    return (f"no row of {table} matched this query, and its windows "
            f"{_windowed_on(names, window.windows)} had not ended when it ran at "
            f"{window.ran_at}: rows for them may not be in the table yet, so this result does "
            "not show that nothing happened in them. Give "
            f"{_listed([f'the newest {name}' for name in shown])} under this query's other "
            f"filters in your answer ({maxima} with those filters and no bound on any of these "
            "columns); when one is before the start of its window, say the table has no rows "
            f"under those filters for this query's windows yet.{stand_in}{names.sentence()}")


def _plain_note(window: OpenWindow) -> str:
    """The note when the named one is past :data:`MAX_NOTE_CHARS`: no name."""
    many = len(window.windows) > 1
    which = (f"each of its {len(window.windows)} windows" if many else "its window")
    return (f"no row matched this query, and {which} on a column its WHERE bounds from "
            f"below with a time had not ended when it ran at {window.ran_at}: rows for "
            f"{'them' if many else 'it'} may not be in the table yet, so this result does not "
            "show that nothing happened. Give the newest value of each windowed column under "
            "this query's other filters in your answer (MAX of it with those filters and no "
            "bound on any windowed column); when one is before the start of its window, say "
            "the table has no rows under those filters for this query's windows yet.")


def note(found_windows) -> str:
    """The note on the one result :func:`found` read, at most
    :data:`MAX_NOTE_CHARS` characters, ``""`` for none. A note on several
    windows with a withheld column names none (one stand-in would stand for
    two columns)."""
    from .engine_names import printable
    windows = [w for w in found_windows or () if isinstance(w, OpenWindow)]
    if not windows:
        return ""
    window = windows[0]
    if len(window.windows) > 1 and not all(printable(c) for c, *_rest in window.windows):
        return _plain_note(window)
    text = _named_note(window)
    return text if len(text) <= MAX_NOTE_CHARS else _plain_note(window)


@dataclass
class _Entry:
    """One entry of the line and the block: one query (``windows``), or
    several single-window queries of one table over one window (``span``,
    one column per query in ``columns``)."""

    table: str
    windows: tuple
    span: tuple
    columns: list
    ran_at: str

    @property
    def queries(self) -> int:
        return len(self.columns) or 1


def _entries(found_windows) -> tuple[list, int]:
    """``(entries, queries)``: at most :data:`_MAX_SHOWN` entries in the order
    the zeros ran, and how many zeros there are in all. A single-window zero
    joins the open entry of its source and window while that entry lists fewer
    than :data:`MAX_COLUMNS` queries."""
    # The same result disclosed twice is one zero; two results never are.
    zeros = list(dict.fromkeys(w for w in found_windows or () if isinstance(w, OpenWindow)
                               and w.zero and 1 <= len(w.windows) <= MAX_COLUMNS))
    entries: list = []
    joinable: dict = {}
    for window in zeros:
        if len(window.windows) == 1:
            column, start, end, inclusive = window.windows[0]
            key = (window.source, start, end, inclusive)
            entry = joinable.get(key)
            if entry is not None and len(entry.columns) < MAX_COLUMNS:
                entry.columns.append(column)
                entry.ran_at = max(entry.ran_at, window.ran_at)
                continue
            if len(entries) >= _MAX_SHOWN:
                continue
            entry = _Entry(window.table, (), (start, end, inclusive), [column], window.ran_at)
            joinable[key] = entry
        elif len(entries) >= _MAX_SHOWN:
            continue
        else:
            entry = _Entry(window.table, window.windows, (), [], window.ran_at)
        entries.append(entry)
    return entries, len(zeros)


def _queries_on(names, columns) -> str:
    """The columns of several single-window queries: each once."""
    shown, seen = [], set()
    for column in columns:
        if column.lower() not in seen:
            seen.add(column.lower())
            shown.append(names.show(column))
    if len(shown) == len(columns):
        return f"{_listed(shown)} (one query each)"
    return shown[0] if len(shown) == 1 else f"{_listed(shown)} (each on one of them)"


def _line_part(names, entry: _Entry) -> str:
    table = names.show(entry.table, "table")
    if entry.windows:
        one = len(entry.windows) == 1
        return (f"in {table}, a query windowed {_windowed_on(names, entry.windows)} matched "
                f"no rows, and {'that window' if one else 'those windows'} had not ended when "
                f"it ran at {entry.ran_at}")
    span = _span(*entry.span)
    if len(entry.columns) == 1:
        return (f"in {table}, a query windowed on {names.show(entry.columns[0])} {span} "
                f"matched no rows, and that window had not ended when it ran at {entry.ran_at}")
    return (f"in {table}, {len(entry.columns)} queries windowed on "
            f"{_queries_on(names, entry.columns)} {span} matched no rows, and that window "
            f"had not ended when they ran (the last at {entry.ran_at})")


def _line(entries, omitted: int) -> str:
    from .engine_names import Withheld
    names = Withheld()
    parts = "; ".join(_line_part(names, entry) for entry in entries)
    tail = (f" (and {omitted} more such {'query' if omitted == 1 else 'queries'} not shown)"
            if omitted else "")
    return (f"{ENGINE_PREFIX} check, in this turn's queries: {parts}{tail}. Rows for such a "
            f"window may not be in the table yet, so each is {MARKER}.{names.sentence()}")


def _required(names, entry: _Entry) -> str:
    table = names.show(entry.table, "table")
    if entry.windows and len(entry.windows) > 1:
        newest = _listed([f"the newest {names.show(column)}"
                          for column, *_rest in entry.windows])
        return (f"REQUIRED: the answer must not present the zero of the query windowed "
                f"{_windowed_on(names, entry.windows)} in {table} as no activity in those "
                "windows: it was counted before they ended, so rows for them may not be in "
                f"that table yet. It must give {newest} in that table under that query's "
                "other filters, with no bound on any of these columns, as a system returned "
                "them, or say they were not returned; when one is before the start of its "
                "window, it must say that table has no rows under those filters for the "
                "query's windows yet.")
    column, start, end, inclusive = (entry.windows[0] if entry.windows
                                     else (entry.columns[0], *entry.span))
    span = _span(start, end, inclusive)
    if entry.windows or len(entry.columns) == 1:
        shown = names.show(column)
        return (f"REQUIRED: the answer must not present the zero for {shown} in {table} "
                f"{span} as no activity in that window: it was counted before the window "
                f"ended, so rows for it may not be in that table yet. It must give the newest "
                f"{shown} in that table under the same other filters as a system returned "
                f"it, or say it was not returned; when that time is before {start}, it must "
                "say that table has no rows under those filters for the window yet.")
    return (f"REQUIRED: the answer must not present the zeros of the {len(entry.columns)} "
            f"queries windowed on {_queries_on(names, entry.columns)} in {table} {span} as "
            "no activity in that window: they were counted before the window ended, so rows "
            "for it may not be in that table yet. For each of those queries it must give the "
            "newest value of its windowed column in that table under that query's other "
            "filters as a system returned it, or say it was not returned; when that time is "
            f"before {start}, it must say that table has no rows under that query's filters "
            "for the window yet.")


def _block(entries, omitted: int) -> str:
    from .engine_names import Withheld
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = Withheld()
    notes: list = []
    for index, entry in enumerate(entries, 1):
        text = _required(names, entry)
        # Two tables whose names are both withheld read alike; a block keeps
        # one copy of a repeated text, so each still gets its own.
        notes.append(text if text not in notes else
                     f"{text} This is entry {index}, another query than the one the same "
                     "text above describes.")
    if names.count:
        notes[-1] += names.sentence()
    if omitted:
        notes.append(f"REQUIRED: the same holds for the {omitted} other "
                     f"{'zero' if omitted == 1 else 'zeros'} over a window that had not ended "
                     "that this turn's queries returned and this block does not list: the "
                     "answer must not present any of them as no activity in its window.")
    document = empty_document()
    for text in notes:
        document = append_note(document, text, GuidanceScope.ANSWER_REQUIREMENT)
    return render_orchestrator_sections(document)["answer_requirements"]


def _fitted(found_windows) -> tuple[str, str]:
    """``(engine line, block)`` for the zeros, the line within
    ``executed_filters.MAX_LINE_CHARS`` and the block within
    :data:`MAX_BLOCK_CHARS`: whole entries dropped from the end and counted,
    ``("", "")`` when none fits or there are none."""
    from .executed_filters import MAX_LINE_CHARS
    entries, queries = _entries(found_windows)
    for shown in range(len(entries), 0, -1):
        kept = entries[:shown]
        omitted = queries - sum(entry.queries for entry in kept)
        line, block = _line(kept, omitted), _block(kept, omitted)
        if len(line) <= MAX_LINE_CHARS and len(block) <= MAX_BLOCK_CHARS:
            return line, block
    return "", ""


def engine_line(found_windows) -> str:
    """One bounded engine line, as facts, for this call's results that matched
    no rows over a window that had not ended, ``""`` for none."""
    return _fitted(found_windows)[0]


def requirement_block(found_windows) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for the zeros :func:`engine_line`
    lists, as facts, ``""`` for none."""
    return _fitted(found_windows)[1]


def neutralize(text: str) -> str:
    """``text`` with a copy of the engine line's prefix made inert."""
    if not isinstance(text, str) or ENGINE_PREFIX not in text:
        return text
    return text.replace(ENGINE_PREFIX, ENGINE_PREFIX[len("[engine] "):])
