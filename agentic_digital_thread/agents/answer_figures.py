# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Totals the engine computes, and a stated total that no result holds.

Measured on an analytics deployment, over two runs: a system agent wrote a
day's total that was not the sum of its own per-group rows (which summed
exactly to the direct count it also ran), the whole-column sum of an 18-row
result wrong (one column a per-group distinct count), a total under a list
of 14 daily counts it had copied correctly that was not their sum, a "Total
X: N" whose N was in no result, and subtotals of one grouping column across
another that matched nothing. The orchestrator relayed the wrong figures and
called the right ones unverified. The prompt already says a total comes from
SQL, never from adding rows by hand; this carries the facts instead.

Both halves are engine lines through ``system_agent._append_engine_facts``
(report-only and annotate only; under enforce the answer is the server-built
envelope):

1. **Result totals.** Each executed query's sidecar row
   (``tool_result_capture.record_sidecar_statement``) keeps the totals of a
   result that is row-complete (not cut by rows, pages, windows or bytes, and
   fewer rows than its own LIMIT), has two or more rows, and whose statement
   the exact split's parser admits (``athena_exact_shape.parse_shape``: one
   relation, GROUP BY keys and COUNT/SUM/MIN/MAX of a row expression; no
   JOIN, HAVING, subquery or set operation), read with GROUP BY spelled to
   match two key forms that leave every sum unchanged: a constant SELECT
   item (a string or typed literal) and ``COALESCE(k, <literal>)`` over
   ``GROUP BY k`` (:func:`_group_by_matched`). Only COUNT and SUM columns are
   added up (CASE-filtered too); MIN and MAX never are, and AVG makes the
   parser refuse the query. A ``COUNT(DISTINCT x)`` column is never added up
   (:func:`_additive_columns`): one x can sit in several groups, and the
   engine cannot tell when it does, so the per-group counts may add up to no
   count at all. Measured: the line once printed such a sum with a label
   saying it was not a distinct count, and the orchestrator stated it as one.
   A result whose only count columns are COUNT(DISTINCT) ones gets no totals;
   any other DISTINCT is refused. :func:`totals_line`
   states them, numbered like the executed-filters line, each with what its
   rows count (:func:`_counted`): the physical GROUP BY columns (a COALESCE
   key by the column it reads), the relation (list elements, one row per
   element, for a list expansion, so an empty list adds no row), the time
   column with its bounds and the other top-level WHERE conjuncts as
   executed, and the aggregate expression beside each total. A list
   expansion whose result carries the records an empty or NULL list leaves
   out (``empty_lists_dropped``, ``empty_lists``) says how many of them add
   no row, the first label detail to give way, and a list expansion keeps its
   list-elements phrase beside the result's own key names before it falls to
   them alone, so a long label still says its rows are elements. A result alias
   alone once let the orchestrator relabel a total as a count of something
   else. The line is never longer than the executed-filters line may be: a
   total over the same FROM and WHERE as an earlier one says so instead of
   repeating them, and past the budget an entry's labels give way (its
   aggregates, then what its rows count) before whole entries are counted
   instead of shown (:func:`_fitted_entries`); one entry past its own cap
   with no label left to give shows its leading totals and counts the rest;
   a figure is never cut.
2. **A stated total nothing accounts for.** :func:`stated_figures` reads the
   integer figures the answer presents as a total or count (``<label with
   total, sum, count, number of or overall>: N``, ``<label>: N total``,
   ``total of N``, ``total is N``, ``<label> count was N``), and, with NO
   delimiter, three prose shapes a system agent writes in
   running text: a cardinal beside a count noun under a total word (``324
   total defects``, ``324 defects total``, ``324 defects across all lines``); a
   verb phrase asserting a sum
   (``sums to N``, ``adds up to N``, ``totals N``); and a lower bound (``at
   least N <count noun>``, ``N+ <count noun>``). The count-noun vocabulary is
   the ONE table-header set (:data:`_COUNT_NOUNS`), so ``at least 6,733 defect
   records`` reads but ``at least 3 sites reporting`` does not. A lower bound
   is held when it is at most the returned rows' sum (:func:`unfound_figures`'s
   ``floor``), so a true ``at least N, the total is unknown`` passes and one
   that exceeds everything the call holds is named. Percentages (``%`` or in
   words after the number), decimals, signed
   figures, dates, times, durations, approximations, labels about an
   average, rate, share or change, and labels whose head names an
   identifier, date or time (``order ID``, ``date key``, ``timestamp``)
   unless they ask for a count, are not read (the prose shapes reuse the same
   ``average/rate/share/change`` skip filter, so they never read a ratio or a
   percentage). A year, compact date or Unix
   time written without separators is read like any figure, and left alone
   only when it is one of this call's own dates or times in that form
   (:func:`date_moments` over its results, question and conversation,
   checked by :func:`date_held`), so no word next to it decides; more dates
   than a call keeps leaves only its date-shaped figures unchecked. :func:`unfound_figures` keeps a figure only when it
   is none of: a number in any of this call's tool results; a whole-column
   sum or per-key subtotal of a complete result (each grouping column and
   combination of them summed across the others, and, for a column with at
   most :data:`MAX_SUBSET_VALUES` values, any set of them); a sum or
   difference of two returned values or column sums; the sum of the figures
   the answer lists next to it; at most the rows returned; a number in the
   delegated question or in the inner model's conversation (its prompts and
   every tool result in it, earlier calls' too, as a Mode A agent is
   long-lived; never its own replies; read only when needed), written out
   or abbreviated (``8.3M`` allows what it rounds from, :func:`question_ranges`). The figure
   line names it, what its listed figures add up to (when one sum), the
   engine totals, and a FINAL-ANSWER REQUIREMENTS block, in place of the
   totals line. A prose total (never a table count cell, a lower bound, a
   figure scoped to part of a result, or a running, partial or filtered
   total in its own clause, :func:`_part_spellings`) written beside a
   listing that reproduces one complete grouped result's count or sum column
   (at least :data:`_MIN_TIED_PREFIX` of its leading rows in its row order,
   or all its rows in any order, each listed row writing that row's grouping
   keys; :func:`_tied_totals`) is stated as that result's total: when it is
   not that total, it is checked against that result alone
   (:func:`_own_checks`: its own cells, its row count, the engine totals and
   the question's own figures), so a misadded day's total that happens to be
   a value or a sum of two values from another result is named. Of its own
   values, only a sum of two of its cells holds it, so its total less or plus
   one of its rows is named too. When the
   listed rows match several results, one of them alone must hold it
   (:func:`_tied_checks`). A figure named only for this gets a line opening
   with :data:`TIED_PREFIX` that says which query's rows it is written beside
   and that query's engine total, and a requirement not to state it as that
   result's total; neither says no result holds it.
   A result's cells and row keys are kept on its totals record for this only
   up to :data:`_MAX_LIST_LINES` rows, the most a listing beside a figure holds.
3. **A count in a table cell.** A cell that is exactly one integer under a
   column header the label test admits, or a count-shaped header word
   (occurrences / count / number / qty / quantity / records / rows / units /
   events; never a rate, ``%``, identifier or date header), is a stated count,
   with NO key attributed to it. A period word (``Daily Total``, ``Weekly
   Count``) qualifies such a header; alone it is none. It goes through the SAME support test as a
   labelled total, pair arithmetic included (:func:`unfound_figures`), and is
   named the same way when nothing holds it. Table count cells get their own
   larger budget (:data:`MAX_TABLE_FIGURES`), kept ahead of prose figures, so a
   wide table is checked whole. So a CORRECT per-key figure summed across
   results is not named, :func:`cross_result_vouch_sums` adds, as held values,
   the sum of each key's subtotals over a set of complete grouped results that
   share the key column and the same additive aggregate AND are a PROVEN
   PARTITION (same relation, every pair disjoint on a ``=``/``IN`` or typed
   range column); an identical, retried, unfiltered or overlapping set is not a
   partition and is not vouched. Attributing a table cell to a particular row
   key is deliberately not attempted: naming an unsupported cell as in no
   result is enough for the orchestrator to stop it.

Conservative, because naming a correct figure is worse than missing a wrong
one: a call with a result or conversation the engine could not read whole,
rows of the wrong width, or more numbers than the record holds gets no figure
line: its figures are unverifiable, not wrong. A call with a cut result (any
connector's cut shape, rows filling the query's own LIMIT, an OFFSET) checks
only its PROSE TOTALS (a labelled or phrased total, or a prose total or sum
shape; never a lower bound, a ``+`` figure or a table count cell), and a cut
result shields only the totals it could have produced: such a total is named
when no COMPLETE result of the call supports it (the same support tests, over
the complete results alone), every cut result rules it out (it is strictly
below each count or sum column's returned sum, which that result's true total
is at least, and below the row count of an ungrouped one; and it is none of
the result's numbers, prefix sums in returned or descending order, column sums
or per-key subtotals), and no segment it is written in scopes it to a subset
(:data:`_SUBSET_SCOPE`) or to leading rows (:data:`_RANK_SCOPE`). A cut result
the engine cannot read (no rows, no count or sum column, a negative cell in
one) keeps that call silent.

A physical name reaches a line only through ``engine_names``. Figures are the
answer's own spellings and the engine's own sums. Stdlib only.

The support tests are exact. A sum or difference of two values holds a figure
only when the subtraction is exact (an engine query id once carried ``7e641``
inside a hex group; read as a 642-digit number, it made every figure pass at
default precision). A number is read from a tool result's or the
conversation's text only when it stands alone, never inside an id or a hash;
a short time unit or a k/M magnitude glued to its digits (``12h``, ``3.5k``)
still reads as the number written, unconverted. A tool call a hook cancelled
returned nothing, so its message adds no values (``tool_result_capture``).
"""
from __future__ import annotations

import json
import re
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from itertools import combinations

TOTALS_PREFIX = "[engine] Result totals: "
FIGURE_PREFIX = "[engine] Figures in no result: "
#: The figure line's prefix when it names a figure written as a total beside
#: a result's rows that is not that result's total (:func:`_figure_head`): a
#: value another result returns may equal it, so it is in no result only as
#: the answer states it.
TIED_PREFIX = "[engine] Figures in no result as stated: "
#: Words the orchestrator prompt keys the requirement block on.
MARKER = "in no result"
#: Invocation-state key of every number this call's tool results returned.
VALUES_STATE_KEY = "_adt_result_values"
MAX_VALUES = 50_000
MAX_SUBSET_VALUES = 8
MAX_FIGURES = 16
#: Table count cells get their own larger budget, kept ahead of prose figures,
#: so a wide supported table is checked whole.
MAX_TABLE_FIGURES = 64
MAX_FIGURES_SHOWN = 6
MAX_QUERIES_SHOWN = 6
MAX_COLUMNS_SHOWN = 4
MAX_KEYS_SHOWN = 3
_MAX_KEYS_ALL_SUBSETS = 4
_MAX_NAME_CHARS = 64
#: A SELECT item's aggregate text as kept on the totals record (whole, or not
#: kept past this), and as shown: cut at a token, with :data:`SHORTENED`.
_MAX_EXPR_CHARS = 4_000
_MAX_SHOWN_EXPR_CHARS = 96
SHORTENED = " ... (shortened)"
#: The other WHERE conjuncts beside one total (past it they are cut whole and
#: counted, :func:`_filters_text`), and one time bound's text.
_MAX_FILTER_CHARS = 240
#: One totals entry. The whole totals line is never longer than the
#: executed-filters line may be; past either, an entry's labels give way
#: first (:func:`_fitted_entries`), and an entry still past its own cap with
#: none left shows its leading totals and counts the rest. The figure line
#: keeps to the same budget (:func:`figure_line`); :data:`_TOTALS_CUT` is
#: what it says when no whole total fits beside its figures.
_MAX_ENTRY_CHARS = 600
_LINE_CUT = " (totals line cut: entries incomplete)"
_TOTALS_CUT = (" The engine computed totals from this call's results, but this line has no "
               "room to show them.")
_TOTALS_HEAD = ("the engine added up every returned row of each complete grouped result, "
                "in this turn's queries numbered as above: ")
_MAX_TIME_CHARS = 40
_MAX_ROWS = 50_000
_MAX_CELL_CHARS = 40
#: A grouped KEY value is held up to this many characters -- the printable-name
#: bound, large enough for an ordinary identifier (the real motivating codes
#: reach 48); separate from the numeric-cell bound :data:`_MAX_CELL_CHARS`.
_MAX_KEY_CHARS = 128
#: At most this many complete results feed one key's cross-result vouch sum.
_MAX_VOUCH_RESULTS = 4
#: At most this many cut results are summarised per call; past it the call's
#: prose totals stay unchecked, as every figure beside a cut result once was.
_MAX_CUTS = 16
_MAX_JSON_NODES = 100_000
_MAX_CONTEXT_CHARS = 4_000_000
_MAX_LIST_LINES = 200
#: A listing of at least this many of a result's leading rows, in its row
#: order and naming their keys, reproduces that result (:func:`_reproduces`);
#: fewer prove nothing.
_MIN_TIED_PREFIX = 3
_METADATA_MARKERS = ("information_schema", "$partitions", "$files", "$snapshots")
_CELL_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
#: A number in free text stands alone: not inside a longer word token (an id,
#: a hash), so ``7e641`` inside a hex query id is not read. A sentence's
#: closing full stop still ends a number; a dot followed by a digit (a version
#: string) does not.
_ALONE_BEFORE = r"(?<![\w.])"
_ALONE_AFTER = r"(?!\w|\.\d)"
#: A short unit a number may carry glued to its digits: a time unit or a k/M
#: magnitude. ``12h``, ``40ms`` and ``3.5k`` read as 12, 40 and 3.5; the unit
#: is not converted. A lone ``d`` is not one: a hex id's run can end in it.
_UNIT = r"(?:ms|secs?|s|mins?|m|hrs?|h|k|K|M)"
#: A grouped number (``40,425``, ``-1,234.5``) is one number: its sign and its
#: comma-separated pieces are not read again as plain numbers
#: (:func:`_numbers_in`).
_GROUPED_DIGITS = re.compile(
    rf"{_ALONE_BEFORE}(?P<n>-?\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?){_UNIT}?{_ALONE_AFTER}")
_PLAIN_DIGITS = re.compile(
    rf"{_ALONE_BEFORE}(?P<n>-?\d+(?:\.\d+)?)(?:(?P<e>[eE][+-]?\d+)|{_UNIT})?{_ALONE_AFTER}")
#: The pair test's arithmetic: a difference that is not exact at this
#: precision (or overflows) proves nothing, so the pair is skipped
#: (:func:`_exact_difference`). Wide enough for any count a result returns.
_EXACT = Context(prec=64, traps=[Inexact, Overflow, InvalidOperation])
_OWN_LIMIT = re.compile(r"\blimit\s+(\d+)\s*;?\s*$|\bfetch\s+(?:first|next)\s+(\d+)\s+rows?\b",
                        re.I)
_OFFSET = re.compile(r"\boffset\s+0*[1-9]", re.I)


# --------------------------------------------------------------------------- #
# 1. The totals of a complete grouped result
# --------------------------------------------------------------------------- #
def _cell(value) -> Decimal | None:
    """A result cell as a number; ``None`` for a null; raises for anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("not a number")
    text = str(value).strip()
    if len(text) > _MAX_CELL_CHARS or not _CELL_RE.fullmatch(text):
        raise ValueError("not a number")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError("not a number")
    return number


def _column(rows: list, index: int) -> list | None:
    """One column's cells as numbers (``None`` for a null), ``None`` when a
    cell is not a number or every cell is null."""
    try:
        cells = [_cell(row[index]) for row in rows]
    except (ValueError, IndexError, InvalidOperation):
        return None
    return cells if any(cell is not None for cell in cells) else None


def _column_total(rows: list, index: int) -> Decimal | None:
    cells = _column(rows, index)
    return None if cells is None else sum((c for c in cells if c is not None), Decimal(0))


def _cut_by_query(sql, rows: list) -> bool:
    """Rows fill the query's own LIMIT (or FETCH), or it skips rows (OFFSET)."""
    if not isinstance(sql, str):
        return False
    if _OFFSET.search(sql):
        return True
    match = _OWN_LIMIT.search(sql.strip())
    limit = next((int(g) for g in match.groups() if g), None) if match else None
    return limit is not None and len(rows) >= limit


def _complete(payload: dict, rows: list) -> bool:
    count = payload.get("row_count")
    return (payload.get("truncated") is not True and not payload.get("partial_reason")
            and not payload.get("partial_note") and len(rows) <= _MAX_ROWS
            and (count is None or count == len(rows))
            and all(isinstance(row, list) for row in rows)
            and not _cut_by_query(payload.get("_adt_executed_query"), rows))


def _without_count_distinct(sql: str):
    """``(sql without DISTINCT inside each COUNT(DISTINCT ...), the SELECT
    item indexes it was in)``; ``None`` when the text has another DISTINCT or
    does not tokenize with certainty."""
    from .athena_exact_shape import _split_commas, _top_level
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql)
    clauses = _top_level(tokens) if not uncertain else {}
    if uncertain or "FROM" not in clauses:
        return None
    spans, items = [], set()
    for item, part in enumerate(_split_commas(tokens[1:clauses["FROM"]])):
        for k, token in enumerate(part[:-2]):
            if (token.kind == "word" and token.value.upper() == "COUNT"
                    and part[k + 1].text == "(" and part[k + 2].kind == "word"
                    and part[k + 2].value.upper() == "DISTINCT"):
                spans.append((part[k + 2].start, part[k + 2].end))
                items.add(item)
    distinct = sum(t.kind == "word" and t.value.upper() == "DISTINCT" for t in tokens)
    if not spans or distinct != len(spans):
        return None
    for start, end in sorted(spans, reverse=True):
        sql = sql[:start] + " " + sql[end:]
    return sql, items


def _shape(sql: str):
    """``(shape, COUNT(DISTINCT) item indexes)`` the split's parser admits,
    else ``(None, set())``. A statement it refuses is read again with its
    GROUP BY spelled to match its keys (:func:`_group_by_matched`): that
    changes no COUNT or SUM column's sum, and the split's own parser and its
    contract stay as they are."""
    from .athena_exact_shape import parse_shape
    texts = [sql]
    matched = _group_by_matched(sql)
    if matched is not None:
        texts.append(matched)
    for text in texts:
        try:
            return parse_shape(text), set()
        except Exception:  # noqa: BLE001 - retried below without COUNT(DISTINCT)
            pass
        stripped = _without_count_distinct(text)
        if stripped is None:
            continue
        try:
            return parse_shape(stripped[0]), stripped[1]
        except Exception:  # noqa: BLE001 - a shape the parser refuses gets no total
            continue
    return None, set()


def _additive_columns(shape, distinct: set) -> list[int]:
    """The output indexes of ``shape`` whose rows add up to a total: its COUNT
    and SUM columns, never a COUNT(DISTINCT) one (``distinct``). That column
    counts each value once per group, and one value can sit in several
    groups, so its rows add up to a count only when no value sits in more
    than one group, which the result cannot show."""
    return [i for i, o in enumerate(shape.outputs)
            if o.kind == "agg" and o.func in ("count", "sum") and i not in distinct]


def _constant_item(expr: list) -> bool:
    """A SELECT expression that is one string or typed (``DATE``/``TIMESTAMP``)
    literal. A bare number is not: in GROUP BY it would read as a position."""
    if len(expr) == 1:
        return expr[0].kind == "literal"
    return (len(expr) == 2 and expr[0].kind == "word"
            and expr[0].value.upper() in ("DATE", "TIMESTAMP") and expr[1].kind == "literal")


def _coalesced_column(expr: list):
    """The column tokens of ``COALESCE(<column>, <literal>)``, else ``None``."""
    from .athena_exact_shape import _column_ref, _split_commas
    from .athena_scope import _matching_close
    if not (len(expr) >= 6 and expr[0].kind == "word" and expr[0].value.upper() == "COALESCE"
            and expr[1].text == "(" and _matching_close(expr, 1) == len(expr) - 1):
        return None
    parts = _split_commas(expr[2:-1])
    if (len(parts) != 2 or _column_ref(parts[0]) is None or len(parts[1]) != 1
            or parts[1][0].kind not in ("literal", "number")):
        return None
    return parts[0]


def _group_by_matched(sql: str) -> str | None:
    """``sql`` with GROUP BY spelled to match two harmless key spellings, else
    ``None`` (nothing to respell, or the text does not tokenize with
    certainty):

    * a constant SELECT item (:func:`_constant_item`) not in GROUP BY is added
      to it;
    * ``COALESCE(k, <literal>)`` selected over ``GROUP BY k`` is grouped as
      written: the COALESCE is added, and ``k`` is dropped when no SELECT item
      is ``k`` itself.

    The rows the result returned are the same either way, so every COUNT and
    SUM column adds up to the same total. Only the totals parser reads the
    respelled text; it is never run."""
    from .athena_exact_shape import _norm, _split_alias, _split_commas, _top_level
    from .athena_scope import _strip_outer, tokenize
    tokens, uncertain = tokenize(sql)
    clauses = _top_level(tokens) if tokens and not uncertain else {}
    if uncertain or "FROM" not in clauses or "GROUP" not in clauses:
        return None
    start = clauses["GROUP"] + 2
    end = min([i for i in clauses.values() if i > start] or [len(tokens)])
    group = _split_commas(tokens[start:end])
    if start >= end or any(not item for item in group):
        return None
    exprs = [_strip_outer(_split_alias(item)[0])
             for item in _split_commas(tokens[1:clauses["FROM"]]) if item]
    grouped = {_norm(item) for item in group}
    selected = {_norm(expr) for expr in exprs}
    added, dropped = [], set()
    for expr in exprs:
        text = _norm(expr)
        if text in grouped:
            continue
        column = _coalesced_column(expr)
        if _constant_item(expr) or (column is not None and _norm(column) in grouped):
            added.append(sql[expr[0].start:expr[-1].end])
            if column is not None and _norm(column) not in selected:
                dropped.add(_norm(column))
    if not added:
        return None
    kept = [sql[item[0].start:item[-1].end] for item in group if _norm(item) not in dropped]
    return (sql[:tokens[start].start] + ", ".join(kept + added)
            + sql[tokens[end - 1].end:])


def totals_record(payload) -> dict | None:
    """``{"rows", "keys", "totals"}`` for one complete grouped result (see the
    module docstring), else ``None``. ``totals`` holds ``(column, total,
    False, aggregate)`` for each additive output column
    (:func:`_additive_columns`: COUNT or SUM, never COUNT(DISTINCT), so a
    result whose only counts are distinct ones gets ``None``); ``aggregate``
    is its SELECT expression as written, whitespace collapsed, ``""`` when it
    cannot be read. The third field once marked a COUNT(DISTINCT) total; a
    record that still marks one is read as having no such total
    (:func:`_stated_totals`). ``keys`` holds the GROUP BY output columns.
    Columns and keys are named as the result names them. The aggregate is
    raw query text: :func:`_total_entry` prints it only through
    ``engine_names``. ``dropped``, when present, is the count of records an
    empty or NULL list leaves out of a list expansion (:func:`_dropped_of`).
    ``cells``, when present, holds each total's column cells in row order
    (:func:`_total_cells`), and ``row_keys`` each row's grouping keys
    (:func:`_row_keys`), for :func:`_tied_totals`; no line prints them."""
    if not isinstance(payload, dict):
        return None
    columns, rows, sql = (payload.get("columns"), payload.get("rows"),
                          payload.get("_adt_executed_query"))
    if not (isinstance(columns, list) and isinstance(rows, list) and isinstance(sql, str)):
        return None
    if len(rows) < 2 or not _complete(payload, rows):
        return None
    shape, distinct = _shape(sql)
    if shape is None or not shape.grouped or len(shape.outputs) != len(columns) or (
            shape.limit is not None and len(rows) >= shape.limit):
        return None
    texts = _select_texts(sql, len(shape.outputs)) or [""] * len(shape.outputs)
    totals, indexes = [], []
    for index in _additive_columns(shape, distinct):
        total = _column_total(rows, index)
        if total is not None:
            totals.append((str(columns[index]), format(total, "f"), False, texts[index]))
            indexes.append(index)
    if not totals:
        return None
    key_indexes = [i for i, o in enumerate(shape.outputs) if o.kind == "key"]
    keys = [str(columns[i]) for i in key_indexes]
    record = {"rows": len(rows), "keys": keys, "totals": totals[:MAX_COLUMNS_SHOWN]}
    record.update(_dropped_of(payload))
    cells = _total_cells(rows, indexes[:MAX_COLUMNS_SHOWN])
    if cells:
        record["cells"] = cells
        row_keys = _row_keys(rows, key_indexes)
        if row_keys:
            record["row_keys"] = row_keys
    return record


def _total_cells(rows: list, indexes: list[int]) -> list | None:
    """Each total column's cells in row order, as strings, one list per total
    (``None`` for a column with a null cell); ``None`` when the result has
    more rows than a listing beside a figure can show
    (:data:`_MAX_LIST_LINES`) or no column qualifies."""
    if len(rows) > _MAX_LIST_LINES:
        return None
    out = []
    for index in indexes:
        cells = _column(rows, index)
        out.append(None if cells is None or None in cells
                   else [format(cell, "f") for cell in cells])
    return out if any(out) else None


def _row_keys(rows: list, indexes: list[int]) -> list | None:
    """Each row's grouping-key cells as text, in row order, kept beside
    ``cells`` for :func:`_keys_listed`: ``None`` for a row with a null, empty
    or longer than :data:`_MAX_KEY_CHARS` key cell; ``None`` overall for a
    result with no key column or more than :data:`_MAX_KEYS_ALL_SUBSETS`.
    No line prints them."""
    if not indexes or len(indexes) > _MAX_KEYS_ALL_SUBSETS:
        return None
    out = []
    for row in rows:
        texts = [_key_text(row[i]) if isinstance(row, list) and i < len(row) else None
                 for i in indexes]
        out.append(None if None in texts else texts)
    return out if any(out) else None


def _key_text(cell) -> str | None:
    """A key cell as the text an answer would write, ``None`` when unusable."""
    if isinstance(cell, bool) or not isinstance(cell, (str, int, float, Decimal)):
        return None
    text = str(cell).strip()
    return text if text and len(text) <= _MAX_KEY_CHARS else None


def _dropped_of(payload: dict) -> dict:
    """``{"dropped": N}``, the records whose list is empty or NULL that a list
    expansion leaves out, when the result carries that count
    (``empty_lists_dropped``); ``{}`` otherwise. Only an entry that reads as
    a list expansion states it (:func:`_counted`)."""
    dropped = payload.get("empty_lists_dropped")
    records = dropped.get("records") if isinstance(dropped, dict) else None
    if isinstance(records, int) and not isinstance(records, bool) and records > 0:
        return {"dropped": records}
    return {}


def _select_texts(sql: str, count: int) -> list[str] | None:
    """Each SELECT item's whole expression (alias dropped), rebuilt from its
    tokens: one space where the source had layout or a comment, every token
    as written. An expression over :data:`_MAX_EXPR_CHARS` is not kept
    (``""``): a cut one would not tokenize again. ``None`` when the list does
    not split into ``count`` items with certainty."""
    from .athena_exact_shape import _split_alias, _split_commas, _top_level
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql)
    clauses = _top_level(tokens) if tokens and not uncertain else {}
    if uncertain or "FROM" not in clauses:
        return None
    items = _split_commas(tokens[1:clauses["FROM"]])
    if len(items) != count or any(not item for item in items):
        return None
    texts = []
    for item in items:
        expr = _split_alias(item)[0] or item
        text = "".join((" " if i and token.start > expr[i - 1].end else "") + token.text
                       for i, token in enumerate(expr))
        texts.append(text if len(text) <= _MAX_EXPR_CHARS else "")
    return texts


def _stated_totals(record) -> list:
    """The totals of one sidecar totals record a line may state: every one
    but a total marked as a COUNT(DISTINCT) column's (third field set), which
    only a record built before such columns got none would hold
    (:func:`totals_record`)."""
    items = record.get("totals") if isinstance(record, dict) else None
    return [item for item in items or ()
            if not (isinstance(item, (tuple, list)) and len(item) > 2 and item[2])]


def _ordered_totals(statements) -> list[tuple[int, dict, str]]:
    """``(query ordinal, totals record, executed SQL)`` for each sidecar row
    with totals (:func:`_stated_totals`), numbered like the executed-filters
    line (every data query in run order), an identical record kept once (its
    ``cells`` and ``row_keys`` aside, which no line prints)."""
    out, seen, ordinal = [], [], 0
    for statement in statements or ():
        sql = statement.get("query_text") if isinstance(statement, dict) else None
        if not isinstance(sql, str) or not sql.strip():
            continue
        if any(marker in sql.lower() for marker in _METADATA_MARKERS):
            continue
        ordinal += 1
        record = statement.get("totals")
        totals = _stated_totals(record)
        if not totals:
            continue
        record = {**record, "totals": totals}
        shown = {key: value for key, value in record.items()
                 if key not in ("cells", "row_keys")}
        if shown not in seen:
            seen.append(shown)
            out.append((ordinal, record, sql))
    return out


def _figure(text: str) -> str:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    return f"{value:,}"


def _clip(text: str, limit: int = _MAX_NAME_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _total_entry(ordinal: int, record: dict, sql=None) -> dict:
    """The parts of one entry ``Qn (r rows, <what they count>): column =
    aggregate total, ...``, each ``(text, names)`` with an
    ``engine_names.Withheld`` of its own, so only the parts shown are counted
    (a name that is not a plain identifier prints as a placeholder,
    ``names.show``, or the executed-filters placeholder inside a WHERE
    conjunct): ``counted``, what each total counts (:func:`_counted`, from
    the executed ``sql`` and the record's ``dropped``; ``None`` when it does
    not read); ``by``, the
    result's own key names (`` by k1, k2``); and each total with (``full``)
    and without (``bare``) its aggregate, one ``(text, names)`` per total.
    :func:`_entry_text` puts them together at one level of detail."""
    from .engine_names import Withheld
    rows = record.get("rows") or 0
    key_names = Withheld()
    keys = [_clip(key_names.show(k)) for k in record.get("keys") or ()][:MAX_KEYS_SHOWN]
    entry = {"ordinal": ordinal, "head": f"Q{ordinal} ({rows:,} row{'' if rows == 1 else 's'}",
             "by": (f" by {', '.join(keys)}" if keys else "", key_names),
             "counted": (_counted(sql, record.get("dropped")) if isinstance(sql, str)
                         else None)}
    for kind, aggregates in (("full", True), ("bare", False)):
        entry[kind] = []
        for item in record["totals"][:MAX_COLUMNS_SHOWN]:
            names = Withheld()
            entry[kind].append((_total_text(item, names, aggregates), names))
    return entry


def _labelled_levels(counted) -> int:
    """How many levels of detail apart from the left-out count state what an
    entry's rows count: two, and one more for a list expansion, which keeps
    its list-elements label beside the result's own key names before it gives
    way (:func:`_entry_text`)."""
    if counted is None:
        return 0
    return 3 if counted.get("expanded") else 2


def _entry_levels(entry: dict) -> int:
    """How many levels of detail :func:`_entry_text` has for ``entry``: the
    labelled ones and the key names, and one more for a list expansion that
    counts the records its empty lists leave out (:func:`_detail`)."""
    counted = entry["counted"]
    if counted is None:
        return 2
    return _labelled_levels(counted) + 1 + bool(counted.get("of_counted"))


def _detail(entry: dict, level: int) -> tuple[int, bool]:
    """``(level of detail apart from the left-out count, whether that count is
    shown)`` for ``entry`` at ``level``. The count, where there is one
    (``counted["of_counted"]``), is shown at level 0 only: it is the first
    detail to give way, so it never costs the entry the rest of its label."""
    counted = entry["counted"]
    if counted is None or not counted.get("of_counted"):
        return level, False
    return max(level - 1, 0), level == 0


def _scope_key(entry: dict, level: int):
    """The scope an entry states at ``level`` (its FROM and WHERE, and the
    left-out count it shows), else ``None`` when it states none."""
    counted = entry["counted"]
    detail, left_out = _detail(entry, level)
    if counted is None or detail >= _labelled_levels(counted):
        return None
    return counted["scope"], counted["left_out"] if left_out else ""


def _entry_text(entry: dict, level: int, same_as, kept=None) -> tuple[str, list]:
    """``(text, the Withheld of each part it shows)`` for one entry at one
    level of detail: 0, what its rows count and each total's aggregate; 1,
    what its rows count; last, the result's own key names. Where what its rows
    count does not read, 0 is the key names with the aggregates. A list
    expansion has one more level before the last, the result's own key names
    in place of the GROUP BY expressions beside its list-elements label, so a
    long label keeps saying its rows are list elements; one that counts the
    records its empty lists leave out has one more level first, which also
    states that count (:func:`_detail`). ``same_as``
    is the ordinal of an earlier entry that states this entry's scope (its
    FROM and WHERE), said as ``of the same ... as Qn``, else ``None``. Only
    the first ``kept`` totals are shown (all for ``None``), each whole, and
    the rest are counted as ``and N more totals``."""
    counted = entry["counted"]
    level, left_out = _detail(entry, level)
    labelled = level < _labelled_levels(counted)
    items = entry["full" if level == 0 else "bare"]
    shown = items if kept is None else items[:kept]
    more = len(items) - len(shown)
    totals = ", ".join(text for text, _names in shown) + (
        f", and {more} more total{'s' * (more > 1)}" if more else "")
    if labelled:
        scope = counted["of_counted" if left_out else "of"] if same_as is None else (
            f"{counted['same'][0]} as Q{same_as}", counted["same"][1])
        if level < 2:
            parts = [part for part in (counted["one_per"], scope) if part[0]]
            label = ", " + ", ".join(text for text, _names in parts)
        else:
            parts = [entry["by"], scope]
            label = f"{entry['by'][0]}, {scope[0]}"
    else:
        parts = [entry["by"]]
        label = entry["by"][0]
    return f"{entry['head']}{label}): {totals}", [names for _t, names in (*parts, *shown)]


def _fitted_entries(entries: list[dict], budget: int, hidden: int = 0) -> tuple[str, str]:
    """``(entries text, withheld sentence)``, together at most ``budget``
    characters. Each entry starts at its fullest level of detail within
    :data:`_MAX_ENTRY_CHARS`; one still past it at its last level shows its
    leading totals whole and counts the rest (:func:`_entry_text`), keeping
    at least one. While the whole is longer, the longest entry
    (the latest of equals, as later queries are the ones counted, not shown)
    gives up one level; once every entry is at its last level, the last
    entry is counted in ``and N more`` instead (``hidden`` more are counted
    already). One entry that still does not fit is cut between two items
    with :data:`_LINE_CUT`, so a figure is never cut. An entry whose scope an
    earlier shown one states says so (:func:`_entry_text`). Only the names
    the shown text withholds are counted."""
    from .engine_names import Withheld
    levels, shown = [0] * len(entries), len(entries)
    kept = [len(entry["bare"]) for entry in entries]
    while True:
        texts, stated = [], {}
        for entry, level, count in zip(entries[:shown], levels, kept):
            scope = _scope_key(entry, level)
            texts.append(_entry_text(entry, level, stated.get(scope), count))
            if scope is not None:
                stated.setdefault(scope, entry["ordinal"])
        over = [i for i, (text, _parts) in enumerate(texts)
                if len(text) > _MAX_ENTRY_CHARS
                and (levels[i] < _entry_levels(entries[i]) - 1 or kept[i] > 1)]
        if over:
            if levels[over[0]] < _entry_levels(entries[over[0]]) - 1:
                levels[over[0]] += 1
            else:
                kept[over[0]] -= 1
            continue
        names = Withheld()
        for _text, parts in texts:
            for part in parts:
                names.merge(part)
        more = hidden + len(entries) - shown
        body = "; ".join(text for text, _parts in texts) + (f"; and {more} more" if more else "")
        sentence = names.sentence()
        if len(body) + len(sentence) <= budget:
            return body, sentence
        raisable = [i for i in range(shown) if levels[i] < _entry_levels(entries[i]) - 1]
        if raisable:
            levels[max(raisable, key=lambda i: (len(texts[i][0]), i))] += 1
        elif shown > 1:
            shown -= 1
        else:
            return _cut_between_items(body, budget - len(sentence)), sentence


def _cut_between_items(text: str, limit: int) -> str:
    """``text`` cut after its last whole item (``, `` or ``; `` separated)
    within ``limit`` characters, with :data:`_LINE_CUT`."""
    room = max(limit - len(_LINE_CUT), 0)
    cut = max(text.rfind(", ", 0, room + 1), text.rfind("; ", 0, room + 1))
    return (text[:cut] if cut > 0 else "") + _LINE_CUT


def _total_text(item, names, aggregates: bool = True) -> str:
    """``column = aggregate total`` for one total (``column total`` when the
    aggregate was not kept or ``aggregates`` is false)."""
    aggregate = (_shown_sql(item[3], names) if aggregates and len(item) > 3 and item[3]
                 else "")
    shown = _clip(names.show(item[0]))
    text = f"{shown} = {aggregate} " if aggregate else f"{shown} "
    return text + _figure(item[1])


def _shown_sql(text: str, names) -> str:
    """A query fragment as written (:func:`_tokens_text`): each name that is
    not a plain identifier becomes its placeholder (``names.show``, counted),
    a long literal is clipped, and a long fragment is cut at a token. A
    fragment that does not tokenize with certainty is not shown (``""``):
    it is never passed off as a withheld name."""
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens:
        return ""
    return _tokens_text(tokens, names)


def _tokens_text(tokens: list, names) -> str:
    """Tokens joined as the source spaced them, gated like :func:`_shown_sql`,
    within :data:`_MAX_SHOWN_EXPR_CHARS`: past it the text ends at the last
    whole token that fits, followed by :data:`SHORTENED`. Only a name that is
    shown is counted."""
    from .engine_names import Withheld, printable
    from .executed_filters import _clean, _clip_literal
    pieces, previous = [], None
    for token in tokens:
        hidden = token.kind in ("word", "identifier") and not printable(str(token.value))
        if hidden:
            piece = Withheld().show(str(token.value))
        elif token.kind == "literal":
            piece = _clip_literal(token.text)
        else:
            piece = token.text
        spaced = previous is not None and token.start > previous
        pieces.append(((" " if spaced and pieces else "") + _clean(piece),
                       str(token.value) if hidden else None))
        previous = token.end
    whole = sum(len(piece) for piece, _name in pieces) <= _MAX_SHOWN_EXPR_CHARS
    text = ""
    for piece, name in pieces:
        if text and not whole and len(text) + len(piece) > (
                _MAX_SHOWN_EXPR_CHARS - len(SHORTENED)):
            return text + SHORTENED
        if name is not None:
            names.show(name)
        text += piece
    return text


def _counted(sql: str, dropped=None) -> dict | None:
    """What the rows of one complete grouped result count, in parts, each
    ``(text, names)`` with the ``engine_names.Withheld`` of the names it
    withholds: ``one_per``, ``one per <physical GROUP BY columns>``; ``of``,
    ``of <relation> rows [with <time column> in <bounds>] [where <other
    top-level conjuncts>]``, for a list expansion ``of list elements: one row
    per element of UNNEST(...) in each <relation> row, so a record whose list
    is empty adds no row``; ``of_counted``, only when ``dropped`` counts the
    records whose list is empty or NULL, ``of`` with ``so N records with an
    empty or NULL list add no row`` (``left_out``, :func:`_dropped_text`) in
    place of that last clause; ``same``, ``of the same <relation> rows``
    (``of the same list elements``), said instead of ``of`` when an earlier
    entry states the same ``scope``: the FROM and WHERE clauses as written,
    names case-folded (:func:`_scope_key` adds the count it shows);
    ``expanded``, whether the statement is a list expansion. ``None`` when the
    statement does not read with certainty (the entry keeps the result's own
    key names, and no count is stated)."""
    from .athena_exact_shape import _norm, _top_level
    from .athena_scope import tokenize
    from .engine_names import Withheld
    tokens, uncertain = tokenize(sql)
    clauses = _top_level(tokens) if tokens and not uncertain else {}
    shape, _distinct = _shape(sql)
    if uncertain or shape is None or "FROM" not in clauses:
        return None
    key_names, names, same_names = Withheld(), Withheld(), Withheld()
    keys = _group_by_labels(tokens, clauses, key_names)
    if keys is None:
        return None
    relation = ".".join(shape.table_parts)
    table = _clip(names.show(relation, "table"))
    of, joint, left_out, of_counted = f"{table} rows", " ", "", ""
    same = f"the same {_clip(same_names.show(relation, 'table'))} rows"
    if shape.expanded:
        listed = _expansion_text(tokens, names)
        if listed is None:
            return None
        elements = f"list elements: one row per element of {listed} in each {table} row, so "
        of = f"{elements}a record whose list is empty adds no row"
        left_out = _dropped_text(dropped)
        of_counted = f"{elements}{left_out}" if left_out else ""
        joint, same, same_names = ", ", "the same list elements", Withheld()
    windows, filters = _where_parts(tokens, clauses, names)
    scope = (f"{joint}with {' and '.join(windows)}" if windows else "") + (
        f"{joint}where {filters}" if filters else "")
    begin = clauses["FROM"]
    end = min([i for word, i in clauses.items() if i > begin and word != "WHERE"]
              or [len(tokens)])
    out = {"one_per": (_one_per(keys), key_names), "of": (f"of {of}{scope}", names),
           "same": (f"of {same}", same_names), "scope": _norm(tokens[begin:end]),
           "left_out": left_out, "expanded": bool(shape.expanded)}
    if of_counted:
        out["of_counted"] = (f"of {of_counted}{scope}", names)
    return out


def _dropped_text(dropped) -> str:
    """``N records with an empty or NULL list add no row`` for a positive
    count of the records a list expansion leaves out, else ``""``."""
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped <= 0:
        return ""
    if dropped == 1:
        return "1 record with an empty or NULL list adds no row"
    return f"{dropped:,} records with an empty or NULL list add no row"


def _one_per(keys: list[str]) -> str:
    if not keys:
        return ""
    if len(keys) == 1:
        return f"one per value of {keys[0]}"
    shown = keys[:MAX_KEYS_SHOWN]
    if len(keys) > len(shown):
        more = len(keys) - len(shown)
        return f"one per combination of {', '.join(shown)} and {more} more column{'s' * (more > 1)}"
    return f"one per combination of {', '.join(shown[:-1])} and {shown[-1]}"


def _group_by_labels(tokens: list, clauses: dict, names) -> list[str] | None:
    """The physical GROUP BY columns of a statement, in its order: a column
    reference by its column, ``COALESCE(k, <literal>)`` by ``k``, a position
    by the SELECT item it points at, any other expression as written.
    ``None`` for a position past the SELECT list."""
    from .athena_exact_shape import _column_ref, _split_alias, _split_commas
    from .athena_scope import _strip_outer
    if "GROUP" not in clauses:
        return []
    start = clauses["GROUP"] + 2
    end = min([i for i in clauses.values() if i > start] or [len(tokens)])
    items = [_strip_outer(_split_alias(item)[0]) for item in
             _split_commas(tokens[1:clauses["FROM"]])]
    labels: list[str] = []
    for item in _split_commas(tokens[start:end]):
        expr = _strip_outer(item)
        if len(expr) == 1 and expr[0].kind == "number" and expr[0].text.isdigit():
            position = int(expr[0].text) - 1
            if not 0 <= position < len(items):
                return None
            expr = items[position]
        if not expr:
            return None
        inner = _coalesced_column(expr)
        column = _column_ref(inner if inner is not None else expr)
        label = _clip(names.show(column)) if column is not None else _tokens_text(expr, names)
        if label not in labels:
            labels.append(label)
    return labels


def _expansion_text(tokens: list, names) -> str | None:
    """``UNNEST(<row expression>)`` of the statement's one list expansion, as
    written and gated; ``None`` when it does not read."""
    from .athena_exact_shape import _expansion
    from .athena_scope import _matching_close
    try:
        expansion = _expansion(tokens)
    except Exception:  # noqa: BLE001 - an unreadable expansion keeps the key names
        return None
    if expansion is None:
        return None
    opening = expansion.start + expansion.opening
    close = _matching_close(tokens, opening)
    if close is None:
        return None
    return f"UNNEST({_tokens_text(tokens[opening + 1:close], names)})"


def _where_parts(tokens: list, clauses: dict, names) -> tuple[list[str], str]:
    """``(time windows, other filters)`` of the statement's top-level WHERE:
    a column bounded once from below and once from above by a date or time
    literal (or by one BETWEEN) is a window ``column in [low, high)``,
    bracketed as written; every other top-level conjunct is a filter, as
    executed (``executed_filters``' forms: a long IN list collapsed, a long
    literal clipped, a name that is not a plain identifier withheld), within
    :data:`_MAX_FILTER_CHARS` (:func:`_filters_text`). Only the names of the
    windows and conjuncts shown are counted."""
    from .engine_names import printable
    from .executed_filters import _conjunct_spans
    if "WHERE" not in clauses:
        return [], ""
    start = clauses["WHERE"] + 1
    end = min([i for i in clauses.values() if i > start] or [len(tokens)])
    spans = _conjunct_spans(tokens, start, end)
    bounds: dict[str, list] = {}
    for span in spans:
        found = _time_bound(tokens[span[0]:span[1]])
        if found is not None:
            bounds.setdefault(found[0].lower(), []).append((span, found))
    windows, used = [], set()
    for entries in bounds.values():
        sides = sorted(entries, key=lambda entry: entry[1][1] != "low")
        kinds = [found[1] for _span, found in sides]
        if kinds == ["between"]:
            low = high = sides[0][1]
            brackets = ("[", "]")
            low_text, high_text = low[2], low[3]
        elif kinds == ["low", "high"]:
            low, high = sides[0][1], sides[1][1]
            brackets = ("[" if low[2] else "(", "]" if high[2] else ")")
            low_text, high_text = low[3], high[3]
        else:
            continue
        column = _clip(names.show(low[0]))
        windows.append((min(span[0] for span, _f in sides),
                        f"{column} in {brackets[0]}{low_text}, {high_text}{brackets[1]}"))
        used.update(span for span, _f in sides)
    filters, kept = _filters_text(tokens, [span for span in spans if span not in used])
    for s, e in kept:
        for token in tokens[s:e]:
            if token.kind in ("word", "identifier") and not printable(str(token.value)):
                names.show(str(token.value))
    return [text for _at, text in sorted(windows)], filters


def _filters_text(tokens: list, spans: list) -> tuple[str, list]:
    """``(text, spans shown)``: the conjuncts at ``spans`` joined by AND within
    :data:`_MAX_FILTER_CHARS`, the longest shortened first, one form at a
    time (the same forms and order as the executed-filters line). When every
    conjunct is at its shortest and the text is still longer, it keeps the
    leading conjuncts that fit and says how many it did not show
    (:func:`_filters_cut`)."""
    from .executed_filters import _forms
    forms = [_forms(tokens, s, e) for s, e in spans]
    levels = [0] * len(forms)

    def text() -> str:
        return " AND ".join(form[level] for form, level in zip(forms, levels))

    while len(text()) > _MAX_FILTER_CHARS:
        candidates = [i for i, level in enumerate(levels) if level < 2]
        if not candidates:
            break
        longest = max(candidates, key=lambda i: (len(forms[i][levels[i]]), -i))
        levels[longest] += 1
    if len(text()) <= _MAX_FILTER_CHARS:
        return text(), list(spans)
    shortest = [form[2] for form in forms]
    for count in range(len(shortest) - 1, 0, -1):
        shown = " AND ".join(shortest[:count]) + _filters_cut(len(shortest) - count, True)
        if len(shown) <= _MAX_FILTER_CHARS:
            return shown, list(spans[:count])
    return _filters_cut(len(shortest), False).strip(), []


def _filters_cut(count: int, after: bool) -> str:
    """The marker for ``count`` conjuncts a filter text does not show."""
    plural = "s" if count > 1 else ""
    return f" (filters incomplete: {count}{' more' if after else ''} predicate{plural} not shown)"


def _time_bound(part: list):
    """``(column, "low"|"high", inclusive, literal text)`` for one conjunct that
    bounds a column by a date or time literal, ``(column, "between", low,
    high)`` for a BETWEEN of two, else ``None``."""
    from .athena_exact_shape import _column_ref
    from .athena_scope import _strip_outer
    part = _strip_outer(part)
    if not part:
        return None
    base = min(token.depth for token in part)
    words = [(i, t.value.upper()) for i, t in enumerate(part)
             if t.depth == base and t.kind == "word"]
    between = next((i for i, word in words if word == "BETWEEN"), None)
    if between is not None:
        ands = [i for i, word in words if word == "AND" and i > between]
        column = _column_ref(part[:between])
        if column is None or len(ands) != 1:
            return None
        low, high = _time_text(part[between + 1:ands[0]]), _time_text(part[ands[0] + 1:])
        return (column, "between", low, high) if low and high else None
    ops = [i for i, t in enumerate(part) if t.depth == base and t.kind == "operator"
           and t.text in (">", ">=", "<", "<=")]
    if len(ops) != 1:
        return None
    column, text = _column_ref(part[:ops[0]]), _time_text(part[ops[0] + 1:])
    if column is None or text is None:
        return None
    op = part[ops[0]].text
    return (column, "low" if op in (">", ">=") else "high", op in (">=", "<="), text)


def _time_text(tokens: list) -> str | None:
    """The text of a date or time literal (``DATE '...'``, ``TIMESTAMP '...'``,
    a date function or cast of a string, or an ISO-dated string), quotes
    dropped, else ``None``."""
    from .athena_exact_shape import _literal_day
    from .executed_filters import _clean
    if _literal_day(tokens) is None:
        return None
    literal = next((t for t in tokens if t.kind == "literal"), None)
    return None if literal is None else _clip(_clean(literal.text[1:-1]), _MAX_TIME_CHARS)


def _totals_text(statements, budget: int) -> tuple[str, str]:
    """``(entries, withheld sentence)`` for this call's engine totals,
    together at most ``budget`` characters (:func:`_fitted_entries`)."""
    ordered = _ordered_totals(statements)
    if not ordered:
        return "", ""
    entries = [_total_entry(n, record, sql) for n, record, sql in ordered[:MAX_QUERIES_SHOWN]]
    return _fitted_entries(entries, budget, len(ordered) - len(entries))


def totals_line(statements) -> str:
    """The engine line stating this call's result totals, ``""`` for none;
    never longer than the executed-filters line may be
    (``executed_filters.MAX_LINE_CHARS``)."""
    from .executed_filters import MAX_LINE_CHARS
    head = f"{TOTALS_PREFIX}{_TOTALS_HEAD}"
    entries, withheld = _totals_text(statements, MAX_LINE_CHARS - len(head) - 1)
    if not entries:
        return ""
    return f"{head}{entries}.{withheld}"


def engine_totals(statements) -> list[Decimal]:
    """Every total :func:`totals_line` states, so never a sum of a
    COUNT(DISTINCT) column's rows (:func:`_additive_columns`)."""
    out = []
    for _ordinal, record, _sql in _ordered_totals(statements):
        for item in record["totals"]:
            try:
                out.append(Decimal(item[1]))
            except (InvalidOperation, ValueError, IndexError):
                continue
    return out


# --------------------------------------------------------------------------- #
# 2. Every number a tool result returned, and its sums (capture side)
# --------------------------------------------------------------------------- #
def _result_texts(result, max_chars: int, max_fragments: int) -> list[str] | None:
    """The raw text of one tool result's content, ``None`` when it cannot be
    read whole (too many fragments, one too long, or an unknown shape)."""
    content = result.get("content") if isinstance(result, dict) else getattr(
        result, "content", None)
    if content is None and isinstance(result, dict):
        content = [{"json": result}]
    if isinstance(content, (str, dict)):
        content = [{"text": content} if isinstance(content, str) else {"json": content}]
    if not isinstance(content, list) or len(content) > max_fragments:
        return None
    texts = []
    for item in content:
        if not isinstance(item, dict):
            return None
        text = item.get("text")
        if not isinstance(text, str) and "json" in item:
            try:
                text = json.dumps(item["json"], default=str)
            except (TypeError, ValueError):
                return None
        if isinstance(text, str):
            if len(text) > max_chars:
                return None
            texts.append(text)
    return texts


def _numbers_in(text: str):
    """Every number written in ``text``. A grouped number is read once, whole:
    a plain match overlapping it (its sign, ``40`` or ``425`` in ``40,425``)
    is not read, so its pieces never pair into a figure. A grouped run right
    after ``[`` or ``,`` is a compact list's numbers as strict JSON writes
    them (``[40,425]``), so there its pieces are read as well."""
    spans = []
    for match in _GROUPED_DIGITS.finditer(text):
        if match.start() == 0 or text[match.start() - 1] not in "[,":
            spans.append(match.span())
        value = _spelled_number(match.group("n"))
        if value is not None:
            yield value
    at = 0
    for match in _PLAIN_DIGITS.finditer(text):
        while at < len(spans) and spans[at][1] <= match.start():
            at += 1
        if at < len(spans) and spans[at][0] < match.end():
            continue
        value = _spelled_number(match.group("n") + (match.group("e") or ""))
        if value is not None:
            yield value


def _spelled_number(spelled: str) -> Decimal | None:
    try:
        value = Decimal(spelled.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _leaves(node, out: set, budget: list) -> None:
    """The numeric leaves of a JSON value (numbers and numeric strings)."""
    stack = [node]
    while stack and budget[0] > 0:
        budget[0] -= 1
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(v for k, v in value.items() if not str(k).startswith("_adt_"))
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                number = _cell(value)
            except (ValueError, InvalidOperation):
                continue
            if number is not None:
                out.add(number)


def _keys_of(columns: list, rows: list, sql) -> list[int]:
    """The grouping columns of a result: the parser's GROUP BY keys, else the
    columns that are not all numbers."""
    shape, _distinct = _shape(sql) if isinstance(sql, str) else (None, set())
    if shape is not None and len(shape.outputs) == len(columns):
        return [i for i, o in enumerate(shape.outputs) if o.kind == "key"]
    return [i for i in range(len(columns)) if _column(rows, i) is None]


def _subsets(keys: list[int]) -> list[tuple[int, ...]]:
    """The proper, non-empty sets of grouping columns to sum across the rest."""
    if len(keys) < 2:
        return []
    sizes = range(1, len(keys)) if len(keys) <= _MAX_KEYS_ALL_SUBSETS else (1, 2)
    return [combo for size in sizes for combo in combinations(keys, size)]


def _value_sets(marginals: dict) -> list[Decimal]:
    """Every sum over a set of a few values of one grouping column."""
    figures = [v for v in marginals.values() if v is not None]
    if not 2 < len(figures) <= MAX_SUBSET_VALUES:
        return []
    return [sum(combo, Decimal(0)) for size in range(2, len(figures))
            for combo in combinations(figures, size)]


def derived_values(payload) -> tuple[list[Decimal], list[Decimal]] | None:
    """``(column sums, subtotals)`` a correct answer could take from one
    complete tabular result (module docstring): each numeric column's total;
    its per-key subtotals for each set of grouping columns, and, for a
    grouping column with few values, its sum over any set of them. ``None``
    past the bound."""
    columns, rows = payload.get("columns"), payload.get("rows")
    if not (isinstance(columns, list) and isinstance(rows, list) and len(rows) >= 2):
        return [], []
    keys = _keys_of(columns, rows, payload.get("_adt_executed_query"))
    measures = {i: cells for i in range(len(columns))
                if i not in keys and (cells := _column(rows, i)) is not None}
    sums = [sum((c for c in cells if c is not None), Decimal(0)) for cells in measures.values()]
    subtotals: list[Decimal] = []
    for subset in _subsets(keys):
        for cells in measures.values():
            groups: dict = {}
            for row, cell in zip(rows, cells):
                key = tuple(str(row[k]) for k in subset)
                groups[key] = groups.get(key, Decimal(0)) + (cell or Decimal(0))
            subtotals.extend(groups.values())
            if len(subset) == 1:
                subtotals.extend(_value_sets(groups))
            if len(subtotals) > MAX_VALUES:
                return None
    if len(keys) == 1:
        for cells in measures.values():
            subtotals.extend(_value_sets(dict(enumerate(cells))))
    return (sums, subtotals) if len(subtotals) <= MAX_VALUES else None


def keyed_subtotals(payload) -> dict | None:
    """Per-key subtotals of ONE complete grouped result (module docstring, the
    cross-result vouch sums of item 3).

    ``{"keys": {key column (normalised expression): {key value: subtotal}},
    "measure": <normalised aggregate expression>}`` -- the subtotal is that key
    column's one additive output summed across the other grouping columns.
    Returned only when the result is complete and grouped and has EXACTLY ONE
    additive (COUNT or SUM, not COUNT(DISTINCT)) output column; ``None``
    otherwise, or past the bound. The key-column text is
    :func:`athena_exact_shape`'s normalised form, and ``measure`` the
    aggregate's text, so two results contribute to one key's cross-result sum
    only when they group by the same column with the same aggregate.
    """
    if not isinstance(payload, dict):
        return None
    columns, rows, sql = (payload.get("columns"), payload.get("rows"),
                          payload.get("_adt_executed_query"))
    if not (isinstance(columns, list) and isinstance(rows, list) and isinstance(sql, str)):
        return None
    if len(rows) < 2 or not _complete(payload, rows):
        return None
    shape, distinct = _shape(sql)
    if shape is None or not shape.grouped or len(shape.outputs) != len(columns) or (
            shape.limit is not None and len(rows) >= shape.limit):
        return None
    measures = _additive_columns(shape, distinct)
    if len(measures) != 1:
        return None
    cells = _column(rows, measures[0])
    if cells is None or any(not isinstance(row, list) for row in rows):
        return None
    keys = _keyed_groups(shape, rows, cells)
    if keys is None or not keys:
        return None
    return {"keys": keys, "measure": shape.outputs[measures[0]].text}


def _keyed_groups(shape, rows: list, cells: list) -> dict | None:
    """``{key column (normalised): {value: subtotal}}`` for each single GROUP
    BY column, the subtotal summed across the others; ``None`` past the bound.
    A key value is held up to :data:`_MAX_KEY_CHARS` (an ordinary identifier
    can be long -- the real motivating codes reach 48 characters), a bound
    separate from the numeric-cell bound; past it that key column is dropped."""
    out: dict[str, dict[str, Decimal]] = {}
    for index, output in enumerate(shape.outputs):
        if output.kind != "key":
            continue
        groups: dict[str, Decimal] = {}
        for row, cell in zip(rows, cells):
            value = str(row[index])
            if len(value) > _MAX_KEY_CHARS:
                groups = {}
                break
            groups[value] = groups.get(value, Decimal(0)) + (cell or Decimal(0))
            if len(groups) > MAX_VALUES:
                return None
        if groups:
            out[output.text] = groups
    return out


def _ordered_keyed(statements) -> list[tuple[dict, str]]:
    """``(keyed-subtotals map, executed SQL)`` of each sidecar row that kept a
    keyed map, in run order. The SQL rides along so the cross-result vouch sum
    can prove the contributing results disjoint. Unlike :func:`_ordered_totals`
    this does NOT drop a result whose totals match an earlier one: two results
    that happen to share a column total are still distinct populations."""
    out = []
    for statement in statements or ():
        sql = statement.get("query_text") if isinstance(statement, dict) else None
        if not isinstance(sql, str) or not sql.strip():
            continue
        if any(marker in sql.lower() for marker in _METADATA_MARKERS):
            continue
        keyed = statement.get("keyed")
        if isinstance(keyed, dict) and keyed:
            out.append((keyed, sql))
    return out


def _normalise_keyed(keyed) -> dict | None:
    """One sidecar row's stored keyed record back as ``{"keys": {key column:
    {value: Decimal}}, "measure": str}``, or ``None`` when it is not well
    formed (the store carries the subtotals as strings)."""
    if not isinstance(keyed, dict):
        return None
    raw_keys, measure = keyed.get("keys"), keyed.get("measure")
    if not (isinstance(raw_keys, dict) and isinstance(measure, str)):
        return None
    keys: dict[str, dict[str, Decimal]] = {}
    for column, groups in raw_keys.items():
        if not (isinstance(column, str) and isinstance(groups, dict)):
            return None
        values: dict[str, Decimal] = {}
        for value, subtotal in groups.items():
            try:
                values[str(value)] = Decimal(str(subtotal))
            except (InvalidOperation, ValueError):
                return None
        keys[column] = values
    return {"keys": keys, "measure": measure}


def _partition_meta(sql: str) -> dict | None:
    """``{"relation": canonical FROM, "eq": {column: [literals]}, "range":
    {column: [(lo, lo_incl, hi, hi_incl)]}}`` for ``sql``, the facts the
    disjointness proof needs, or ``None`` when the text does not tokenise with
    certainty. The relation is the table alone for a single-table FROM (a bare
    alias compares equal); a list expansion keeps its full FROM text. ``eq`` is
    the top-level ``=``/``IN`` conjuncts, ``range`` the top-level literal range
    bounds (:func:`_eq_predicates`, :func:`_range_predicates`)."""
    from .athena_exact_shape import _norm, _top_level, _where_tokens
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain or not tokens:
        return None
    clauses = _top_level(tokens)
    if "FROM" not in clauses:
        return None
    shape, _distinct = _shape(sql)
    if shape is not None and not shape.expanded:
        relation = ".".join(part.lower() for part in shape.table_parts)
    else:
        stop = min([i for k, i in clauses.items() if k != "FROM" and i > clauses["FROM"]]
                   or [len(tokens)])
        relation = _norm(tokens[clauses["FROM"] + 1:stop])
    where = _where_tokens(tokens, clauses)
    return {"relation": relation, "eq": _eq_predicates(where),
            "range": _range_predicates(where)}


def _canon_literal(token) -> str | None:
    """A predicate literal canonicalised for the disjointness test: a number by
    VALUE (``1``, ``1.0``, ``01``, ``1e0`` all become one form), a quoted
    string by its text. ``None`` for anything else (a function, a cast, a bound
    parameter): the column is then unusable for the proof."""
    if token.kind == "number":
        try:
            value = Decimal(token.text)
        except (InvalidOperation, ValueError):
            return None
        if not value.is_finite():
            return None
        normalised = value.normalize()
        return f"n:{normalised if normalised != 0 else Decimal(0):f}"
    if token.kind == "literal":
        return f"s:{token.text.lower()}"
    return None


def _eq_predicates(where: list) -> dict:
    """``{column (lower): [canonical literals]}`` for each top-level AND conjunct
    that is ``column = literal`` or ``column IN (literal, ...)``. A column whose
    predicate carries a literal that cannot be canonicalised is dropped (fail
    closed: it must not prove disjointness)."""
    from .athena_exact_shape import _column_ref, _split_commas
    from .athena_scope import _matching_close, _split_boolean, _strip_outer, _upper
    out: dict[str, set] = {}
    unusable: set[str] = set()
    for part in _split_boolean(where, "AND"):
        part = _strip_outer(part)
        if not part or len(_split_boolean(part, "OR")) > 1:
            continue
        base = min(t.depth for t in part)
        in_at = next((i for i, t in enumerate(part)
                      if t.depth == base and _upper(t) == "IN"), None)
        if in_at is not None:
            column = _column_ref(part[:in_at])
            tail = part[in_at + 1:]
            close = _matching_close(tail, 0) if tail and tail[0].text == "(" else None
            items = _split_commas(tail[1:close]) if close is not None else []
            tokens = [i[0] for i in items if len(i) == 1]
            if column is None or not tokens or len(tokens) != len(items):
                continue
        else:
            eq = next((i for i, t in enumerate(part)
                       if t.depth == base and t.kind == "operator" and t.text == "="), None)
            if eq is None:
                continue
            column = _column_ref(part[:eq])
            value = _strip_outer(part[eq + 1:])
            if column is None or len(value) != 1:
                continue
            tokens = value
        canon = [_canon_literal(t) for t in tokens]
        if any(c is None for c in canon):
            unusable.add(column.lower())
            continue
        out.setdefault(column.lower(), set()).update(canon)
    for key in unusable:
        out.pop(key, None)
    return {column: sorted(values) for column, values in out.items()}


def _range_literal(tokens: list):
    """``(value, type tag)`` for a single range literal, parsed to a comparable
    value: a number (``Decimal``, tag ``n``), a ``DATE`` (``date``, tag ``d``),
    or a zone-free ``TIMESTAMP`` (naive ``datetime``, tag ``t``). ``None`` for
    anything else -- a zone-bearing timestamp, a cast, a function, an
    unparseable value -- so an unreadable or ambiguous bound never proves a
    range disjoint. ``DATE`` and ``TIMESTAMP`` carry different tags, so a
    ``DATE`` bound and a ``TIMESTAMP`` bound never combine or compare."""
    from datetime import date, datetime
    from .athena_scope import _strip_outer, _upper
    tokens = _strip_outer(tokens)
    if len(tokens) == 1 and tokens[0].kind == "number":
        try:
            value = Decimal(tokens[0].text)
        except (InvalidOperation, ValueError):
            return None
        return (value, "n") if value.is_finite() else None
    if (len(tokens) == 2 and tokens[0].kind == "word"
            and _upper(tokens[0]) in {"DATE", "TIMESTAMP"} and tokens[1].kind == "literal"):
        text = tokens[1].text.strip("'\" ")
        if _upper(tokens[0]) == "DATE":
            try:
                return (date.fromisoformat(text), "d")
            except ValueError:
                return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        return (moment, "t") if moment.tzinfo is None else None
    return None


def _range_predicates(where: list) -> dict:
    """``{column (lower): [(low, low_inclusive, high, high_inclusive)]}`` for
    each top-level AND conjunct that bounds a column against one range literal
    (``>=``, ``>``, ``<``, ``<=``, or ``BETWEEN a AND b``). Open ends are
    ``None``. Numbers, dates and naive timestamps are kept in separate type
    spaces; a bound whose literal is unreadable is skipped."""
    from .athena_exact_shape import _column_ref
    from .athena_scope import _split_boolean, _strip_outer, _upper
    out: dict[str, list] = {}
    for part in _split_boolean(where, "AND"):
        part = _strip_outer(part)
        if not part or len(_split_boolean(part, "OR")) > 1:
            continue
        base = min(t.depth for t in part)
        between = next((i for i, t in enumerate(part)
                        if t.depth == base and _upper(t) == "BETWEEN"), None)
        if between is not None:
            column = _column_ref(part[:between])
            halves = _split_boolean(part[between + 1:], "AND")
            if column is None or len(halves) != 2:
                continue
            lo, hi = _range_literal(halves[0]), _range_literal(halves[1])
            if lo is None or hi is None or lo[1] != hi[1]:
                continue
            out.setdefault(column.lower(), []).append((lo, True, hi, True))
            continue
        op = next((i for i, t in enumerate(part) if t.depth == base
                   and t.kind == "operator" and t.text in {">", ">=", "<", "<="}), None)
        if op is None:
            continue
        column = _column_ref(part[:op])
        literal = _range_literal(part[op + 1:])
        if column is None or literal is None:
            continue
        text = part[op].text
        bound = ((literal, text == ">=", None, False) if text in {">", ">="}
                 else (None, False, literal, text == "<="))
        out.setdefault(column.lower(), []).append(bound)
    return out


def _interval(bounds: list):
    """Combine a column's range bounds into one ``(type tag, low, low_incl,
    high, high_incl)`` interval of ONE type, or ``None`` when the bounds mix
    types or do not combine (so a mixed or unreadable set never proves
    disjoint). ``low``/``high`` are the parsed values (or ``None`` for an open
    end)."""
    tags = {b[0][1] if b[0] else b[2][1] for b in bounds}
    if len(tags) != 1:
        return None
    tag = tags.pop()
    low = low_incl = high = high_incl = None
    for lo, lo_incl, hi, hi_incl in bounds:
        if lo is not None and (low is None or lo[0] > low):
            low, low_incl = lo[0], lo_incl
        if hi is not None and (high is None or hi[0] < high):
            high, high_incl = hi[0], hi_incl
    return (tag, low, low_incl, high, high_incl)


def _ranges_disjoint(a, b) -> bool:
    """Two intervals of the SAME type are provably disjoint: one's high end is
    at or below the other's low end, and not both inclusive at a touching
    point. Different types (a ``DATE`` interval against a ``TIMESTAMP`` one)
    are never proven disjoint."""
    if a[0] != b[0]:
        return False
    for first, second in ((a, b), (b, a)):
        hi, hi_incl = first[3], first[4]
        lo, lo_incl = second[1], second[2]
        if hi is None or lo is None:
            continue
        if hi < lo or (hi == lo and not (hi_incl and lo_incl)):
            return True
    return False


def _disjoint_on_column(a: dict, b: dict) -> bool:
    """Two results provably count disjoint slices: a shared ``=``/``IN`` column
    with no common literal, or a shared range column with disjoint intervals of
    one literal type."""
    for column in set(a["eq"]) & set(b["eq"]):
        if not (set(a["eq"][column]) & set(b["eq"][column])):
            return True
    for column in set(a["range"]) & set(b["range"]):
        ia, ib = _interval(a["range"][column]), _interval(b["range"][column])
        if ia is not None and ib is not None and _ranges_disjoint(ia, ib):
            return True
    return False


def _proven_partition(metas: list[dict]) -> bool:
    """The results carrying ``metas`` partition one population: all read the
    same relation, and every PAIR is provably disjoint on some column
    (:func:`_disjoint_on_column`). One result is trivially a partition of
    itself; an empty or unreadable meta is never a partition."""
    if any(m is None for m in metas):
        return False
    if len(metas) <= 1:
        return True
    if len({m["relation"] for m in metas}) != 1:
        return False
    return all(_disjoint_on_column(a, b)
               for i, a in enumerate(metas) for b in metas[i + 1:])


def cross_result_vouch_sums(statements) -> set:
    """Held values that VOUCH for a correct per-key figure summed across
    results (module docstring item 3): for each key value held by two or more
    complete grouped results that share the key column and the same additive
    aggregate AND are a PROVEN PARTITION (:func:`_proven_partition`: same
    relation, every pair disjoint on a ``=``/``IN`` or range column), the sum
    of that key's subtotals over every such set of results (sets of
    2..:data:`_MAX_VOUCH_RESULTS`). Identical or retried queries, an unfiltered
    query beside a filtered one, and overlapping windows are never a partition,
    so their doubled sum is not vouched. Bounded by :data:`MAX_VALUES`."""
    by_column: dict[tuple[str, str], dict[str, list[tuple[Decimal, int]]]] = {}
    metas: list[dict | None] = []
    for index, (keyed, sql) in enumerate(_ordered_keyed(statements)):
        normal = _normalise_keyed(keyed)
        if normal is None:
            continue
        metas.append(_partition_meta(sql))
        measure = normal["measure"]
        for column, groups in normal["keys"].items():
            seen = by_column.setdefault((column, measure), {})
            for value, subtotal in groups.items():
                seen.setdefault(value, []).append((subtotal, len(metas) - 1))
    sums: set = set()
    for groups in by_column.values():
        for entries in groups.values():
            if len(entries) < 2:
                continue
            cap = min(len(entries), _MAX_VOUCH_RESULTS)
            for size in range(2, cap + 1):
                for combo in combinations(entries, size):
                    if not _proven_partition([metas[i] for _s, i in combo]):
                        continue
                    sums.add(sum((s for s, _i in combo), Decimal(0)))
                    if len(sums) > MAX_VALUES:
                        return set()
    return sums


def _view() -> dict:
    """An empty value view: the numbers, data values and derived sums read
    from some tool results, their rows, and whether one was cut or unread."""
    return {"raw": set(), "data": set(), "derived": set(), "rows": 0, "unread": False,
            "cut": False}


def _record(invocation_state) -> dict:
    record = invocation_state.get(VALUES_STATE_KEY)
    if not (isinstance(record, dict) and all(
            isinstance(record.get(key), set) for key in ("raw", "data", "derived"))):
        record = {**_view(), "results": 0, "complete": _view(), "cuts": []}
        invocation_state[VALUES_STATE_KEY] = record
    return record


def _add_payload(record: dict, payload) -> None:
    """One parsed payload into the record: a tabular result's cells, rows and
    sums, any other JSON value's numeric leaves and list items (each counted
    as a row, so a count read off a list is never named)."""
    data = record["data"]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list) and isinstance(
            payload.get("columns"), list):
        rows = payload["rows"]
        record["rows"] += len(rows)
        _leaves(rows, data, [_MAX_JSON_NODES])
        if any(not isinstance(row, list) or len(row) != len(payload["columns"])
               for row in rows):
            record["unread"] = True
            return
        if not _complete(payload, rows):
            record["cut"] = True
            return
        derived = derived_values(payload)
        if derived is None:
            record["unread"] = True
            return
        data.update(derived[0])
        record["derived"].update(derived[1])
        return
    _leaves(payload, data, [_MAX_JSON_NODES])
    record["rows"] += _list_items(payload)


def _list_items(node) -> int:
    """How many list elements a JSON value holds, at any depth (bounded)."""
    count, stack, budget = 0, [node], _MAX_JSON_NODES
    while stack and budget > 0:
        budget, value = budget - 1, stack.pop()
        if isinstance(value, (list, dict)):
            count += len(value) if isinstance(value, list) else 0
            stack.extend(value if isinstance(value, list) else value.values())
    return count


def record_result_values(invocation_state, result) -> None:
    """Add one completed tool result to this call's record: every number its
    text holds (``raw``), its data values and sums (``data``), the rows it
    returned, and whether it was cut or could not be read whole. A complete
    result is also added to the record's ``complete`` view, and a cut one
    keeps a summary in ``cuts`` (:func:`_cut_summary`), for the prose totals
    :func:`unfound_figures` checks past a cut. Never reset between the first
    pass and the elicitation retry, like the other capture records."""
    from .tool_result_capture import (_CUT_SCAN_CHARS, _MAX_CONTENT_FRAGMENTS,
                                      _payload_candidates, result_row_cut)
    if not isinstance(invocation_state, dict):
        return
    record = _record(invocation_state)
    record["results"] += 1
    texts = _result_texts(result, _CUT_SCAN_CHARS, _MAX_CONTENT_FRAGMENTS)
    if texts is None:
        record["unread"] = True
        return
    own = _view()
    own["cut"] = bool(result_row_cut(result)[0])
    payloads = list(_payload_candidates(result, _CUT_SCAN_CHARS))
    dates = record.setdefault("dates", [])
    for text in texts:
        for value in _numbers_in(text):
            own["raw"].update((value, abs(value)))
        if not record.get("dates_cut") and date_moments(text, dates)[1]:
            record["dates_cut"] = True
    for payload in payloads:
        _add_payload(own, payload)
    _merge_view(record, own, bool(payloads))
    if own["cut"]:
        _keep_cut(record, payloads, own["raw"])
    elif isinstance(record.get("complete"), dict):
        _merge_view(record["complete"], own, bool(payloads))
    if sum(len(record[key]) for key in ("raw", "data", "derived")) > 2 * MAX_VALUES:
        record["unread"] = True


def _merge_view(into: dict, own: dict, has_payload: bool) -> None:
    """Add one result's view to a call-wide one. A result with no JSON payload
    makes every number the view has read so far a data value."""
    for key in ("raw", "data", "derived"):
        into[key] |= own[key]
    if not has_payload:
        into["data"].update(into["raw"])
    into["rows"] += own["rows"]
    into["unread"] = bool(into["unread"] or own["unread"])
    into["cut"] = bool(into["cut"] or own["cut"])


def _keep_cut(record: dict, payloads: list, raw: set) -> None:
    """Keep one cut result's summary on the record; ``None`` past the bound."""
    cuts = record.get("cuts")
    if isinstance(cuts, list):
        cuts.append(_cut_summary(payloads, raw) if len(cuts) < _MAX_CUTS else None)


def _cut_summary(payloads: list, raw: set) -> dict | None:
    """``{"floors": [...], "held": {...}}`` for one cut result: the sums its
    true total is at least (each count or sum column's returned sum, and the
    row count of an ungrouped result) and the figures it could stand for (every
    number its text holds, each numeric column's prefix sums in returned and in
    descending order, and its column sums and per-key subtotals). ``None`` when
    it cannot be read: no tabular payload, or one :func:`_cut_table` refuses."""
    tables = [p for p in payloads if isinstance(p, dict) and isinstance(p.get("rows"), list)
              and isinstance(p.get("columns"), list)]
    if not tables:
        return None
    floors, held = [], set(raw)
    for payload in tables:
        part = _cut_table(payload)
        if part is None:
            return None
        floors.extend(part[0])
        held |= part[1]
        if len(held) > MAX_VALUES:
            return None
    return {"floors": floors, "held": held}


def _prefix_sums(values: list) -> list[Decimal]:
    out, total = [], Decimal(0)
    for value in values:
        total += value
        out.append(total)
    return out


def _cut_table(payload: dict) -> tuple[list, set] | None:
    """``(floors, held)`` of one cut tabular payload (:func:`_cut_summary`);
    ``None`` for rows of the wrong width, a statement that does not tokenize,
    no floor, a floor column that is not all numbers or holds a negative cell
    (its returned sum is then no lower bound), or past the bound."""
    columns, rows = payload["columns"], payload["rows"]
    if len(rows) > _MAX_ROWS or any(
            not isinstance(row, list) or len(row) != len(columns) for row in rows):
        return None
    floor_at, grouped = _cut_floor_columns(columns, payload.get("_adt_executed_query"))
    if floor_at is None:
        return None
    floors, held = [], set()
    for index in range(len(columns)):
        cells = _column(rows, index)
        if cells is None:
            if index in floor_at:
                return None
            continue
        values = [cell if cell is not None else Decimal(0) for cell in cells]
        held.update(_prefix_sums(values))
        held.update(_prefix_sums(sorted(values, reverse=True)))
        if index in floor_at:
            if any(value < 0 for value in values):
                return None
            floors.append(sum(values, Decimal(0)))
        if len(held) > MAX_VALUES:
            return None
    if not grouped:
        floors.append(Decimal(len(rows)))
    derived = derived_values(payload) if floors else None
    if derived is None:
        return None
    return floors, held | set(derived[0]) | set(derived[1])


def _cut_floor_columns(columns: list, sql) -> tuple[set | None, bool]:
    """``(floor column indexes, grouped)`` for a cut result's statement: each
    SELECT item that holds a COUNT or SUM call (every column when the items
    cannot be matched to the columns), and whether it has a top-level GROUP
    BY. ``(None, False)`` when there is no statement or it does not tokenize
    with certainty."""
    from .athena_exact_shape import _split_commas, _top_level
    from .athena_scope import tokenize
    if not isinstance(sql, str):
        return None, False
    tokens, uncertain = tokenize(sql)
    clauses = _top_level(tokens) if tokens and not uncertain else {}
    if uncertain or "FROM" not in clauses:
        return None, False
    grouped = "GROUP" in clauses
    items = _split_commas(tokens[1:clauses["FROM"]])
    if tokens[0].value.upper() != "SELECT" or len(items) != len(columns):
        return set(range(len(columns))), grouped
    return {i for i, item in enumerate(items) if any(
        token.kind == "word" and token.value.upper() in ("COUNT", "SUM")
        and k + 1 < len(item) and item[k + 1].text == "("
        for k, token in enumerate(item))}, grouped


def _context_texts(message) -> list[str] | None:
    """The text the inner model was given in one conversation message: a
    prompt's text and each tool result's text. Its own replies are left out,
    so a figure it wrote earlier never vouches for itself. ``None`` when a
    tool result cannot be read whole."""
    from .tool_result_capture import _CUT_SCAN_CHARS, _MAX_CONTENT_FRAGMENTS
    if not isinstance(message, dict) or message.get("role") != "user":
        return []
    texts: list[str] = []
    for block in message.get("content") or ():
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            texts.append(block["text"])
        if "toolResult" in block:
            found = _result_texts(block["toolResult"], _CUT_SCAN_CHARS, _MAX_CONTENT_FRAGMENTS)
            if found is None:
                return None
            texts.extend(found)
    return texts


def keep_context(invocation_state, messages, system_prompt=None) -> None:
    """Keep a shallow copy of the inner model's conversation (``agent.messages``)
    and system prompt on this call's record, for :func:`unfound_figures` to
    read only when a figure would otherwise be named. No conversation keeps
    nothing."""
    if not isinstance(invocation_state, dict) or messages is None:
        return
    record = _record(invocation_state)
    if not isinstance(messages, list):
        record["unread"] = True
        return
    prompt = system_prompt if isinstance(system_prompt, str) else ""
    record["context"] = (list(messages), prompt)


def _context_all_texts(record: dict) -> list[str] | None:
    """The kept conversation's prompts and tool results (not the model's own
    replies) and its system prompt; ``None`` when they cannot be read whole."""
    messages, prompt = record.get("context") or ((), "")
    texts, size = [prompt], len(prompt)
    for message in messages:
        found = _context_texts(message)
        if found is None:
            return None
        texts.extend(found)
        size += sum(len(t) for t in found)
        if size > _MAX_CONTEXT_CHARS:
            return None
    return texts


def _context_numbers(record: dict) -> set | None:
    """The numbers written out in the kept conversation (:func:`_context_all_texts`,
    so a figure the model wrote never vouches for itself); ``None`` when it
    cannot be read whole.
    Only exact numbers count here: an abbreviation such as ``5m`` in a schema
    note or business context is too loose to vouch for a stated total (the
    delegated question's abbreviations are read by :func:`question_ranges`).
    Its dates are read separately, only when needed (:func:`date_moments`)."""
    texts = _context_all_texts(record)
    if texts is None:
        return None
    numbers: set = set()
    for text in texts:
        for value in _numbers_in(text):
            numbers.update((value, abs(value)))
        if len(numbers) > 2 * MAX_VALUES:
            return None
    return numbers


def _context_moments(record: dict) -> tuple[list, bool]:
    """The kept conversation's dates and times, and whether they overflowed
    (or could not be read whole), for the date-shaped figures left."""
    texts = _context_all_texts(record)
    moments: list = []
    if texts is None:
        return moments, True
    for text in texts:
        if date_moments(text, moments)[1]:
            return moments, True
    return moments, False


def mark_unread(invocation_state) -> None:
    """Mark this call's record unread, so no figure is named this call."""
    if isinstance(invocation_state, dict):
        _record(invocation_state)["unread"] = True


def result_values(invocation_state) -> dict | None:
    """This call's value record, ``None`` when absent or malformed."""
    record = invocation_state.get(VALUES_STATE_KEY) if isinstance(invocation_state, dict) else None
    if not (isinstance(record, dict) and all(
            isinstance(record.get(key), set) for key in ("raw", "data", "derived"))):
        return None
    return record


# --------------------------------------------------------------------------- #
# 3. Figures the answer presents as a total or count, and the figures it lists
# --------------------------------------------------------------------------- #
_TOTAL_WORD = re.compile(r"\b(?:totals?|grand total|sum|overall|count|number of)\b", re.I)
_SKIP_WORD = re.compile(
    r"\b(?:average|avg|mean|median|rate|ratio|percent|percentage|share|change|difference"
    r"|delta|increase|decrease|growth|net|per cent)\b", re.I)
#: Words that scope a prose total to part of the result; the prose shapes
#: (:func:`_total_phrase_figures`) leave such a segment to the labelled reader.
_SUBSET_SCOPE = re.compile(
    r"\b(?:from|for|among|within|excluding|except|only|group|groups|subset)\b", re.I)
#: Words that scope a total to the leading or trailing rows of a ranked result
#: ("top 3", "the first 10", "largest", "bottom 4", "lowest"). Read only past a
#: cut (:func:`_scoped_spellings`). Not "last": "last 7 days" names a window,
#: and a total over it must stay checked.
_RANK_SCOPE = re.compile(
    r"\b(?:top|first|largest|leading|biggest|bottom|lowest|smallest|fewest|least)\b",
    re.I)
#: Words that scope a total to hand-picked rows, read only past a cut beside
#: :data:`_SUBSET_SCOPE` (which the no-cut readers also use, unchanged). Not
#: "selected": "selected period" names the window, whose total stays checked.
_PICKED_SCOPE = re.compile(r"\b(?:chosen|picked)\b", re.I)
#: A number as the readers spell it, never the tail of a longer one.
_ANY_NUMBER = re.compile(r"(?<![\d,.])(\d{1,3}(?:,\d{3})+|\d+)")
_NUMBER = r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)"
_DURATION = (r"(?:ms|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?|days?|weeks?|months?"
             r"|years?)\b")
_PERCENT = r"(?i:\s*\(?\s*(?:percent(?:age)?s?|per\s+cent|pct)\b)"
#: A scale word after a figure (``109 million``, ``12 bn``): the figure rounds
#: a larger count, so no reader reads it as that bare number. One-letter
#: ``K`` / ``M`` / ``B`` are also units (metres, Kelvin, bytes); only a count
#: shape, which names what it counts, takes one as a scale (:data:`_SCALE_AFTER`).
#: ``bn`` / ``mn`` are scales in lower case only: ``mN`` and ``MN`` are newtons.
#: A capitalised word after the scale word makes it a name (``13 Thousand
#: Pines locations``), not a scale.
_SCALED = (r"[ \t]+(?:(?i:thousands?|millions?|billions?|trillions?)|(?-i:bn|mn))(?![\w&-])"
           r"(?-i:(?![ \t]+[A-Z][a-z]))")
_LETTER_SCALE = r"[ \t]+(?i:[kmb])(?![\w&-])"
_AFTER = rf"(?![\d\w%/:\-]|[.,]\d|\s*%|{_PERCENT}|\s+{_DURATION}|{_SCALED})"
#: A label naming an identifier, a date or a time: its figure is not a count,
#: unless the label asks for one (:data:`_COUNT_ASKED`).
_NOT_A_COUNT = re.compile(
    r"\b(?:ids?|identifiers?|keys?|codes?|uuids?|numbers?|dates?|\w*times?|timestamps?|ts"
    r"|epochs?|years?)\b", re.I)
_COUNT_ASKED = re.compile(r"\b(?:number of|counts?|counted|how many)\b", re.I)
#: The label's head ends at its first preposition: in ``Total for code c4``
#: the identifier word qualifies the total, it is not what the figure is.
_PREPOSITION = re.compile(
    r"\b(?:for|of|in|by|per|at|on|under|with|across|from|within|during)\b", re.I)
_LABELLED = re.compile(rf"(?P<label>[^:=\n]{{1,100}})[:=][ \t]*{_NUMBER}{_AFTER}")
_PHRASED = re.compile(
    rf"(?P<label>(?:\b[\w-]+[ \t]+){{0,3}}\b(?:grand[ \t]+)?totals?)[ \t]+"
    rf"(?:of|is|was|are|were)[ \t]+{_NUMBER}{_AFTER}", re.I)
#: ``<label> count is/was N`` (``the highest daily count was N``), read like
#: ``<label> total was N`` and through the same label test. Not ``count of N``,
#: which says what is counted more often than it states a count.
_COUNT_PHRASED = re.compile(
    rf"(?P<label>(?:\b[\w-]+[ \t]+){{0,3}}\bcounts?)[ \t]+(?:is|was|are|were)[ \t]+"
    rf"{_NUMBER}{_AFTER}", re.I)
#: A total word right after a labelled figure (``<label>: N total``, ``N in
#: total``, ``N overall``): the figure is read as ``<label> total: N`` would be.
_TOTAL_AFTER = re.compile(r"[ \t]+(?:in[ \t]+)?(?P<word>total|overall)\b", re.I)
_MARKUP = re.compile(r"\*\*|__|`|\"|^\s*#+\s*", re.M)
_LISTED = re.compile(rf"(?<![\w.,/:\-]){_NUMBER}{_AFTER}")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")
_GAP = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})?\s*$")


def _label_ok(label: str) -> bool:
    """A total or count label, not about an average, rate, share or change,
    and whose head (before its first preposition) names no identifier, date
    or time unless the label asks for a count."""
    words = re.sub(r"[_\W]+", " ", label)
    if not _TOTAL_WORD.search(words) or _SKIP_WORD.search(words):
        return False
    head = _PREPOSITION.split(words, maxsplit=1)[0]
    return not _NOT_A_COUNT.search(head) or bool(_COUNT_ASKED.search(words))


def _labelled_spellings(segment: str) -> list[str]:
    """The figures one segment states with a label (:data:`_LABELLED`,
    :data:`_PHRASED`, :data:`_COUNT_PHRASED`) that passes :func:`_label_ok`,
    in pattern order. A ``<label>: N`` figure with a total word right after
    it (:data:`_TOTAL_AFTER`) is tested as ``<label> <word>``. A ``count was
    N`` figure is not read when its sentence, up to the figure, has a skip
    word (``the difference from the earlier count is N``)."""
    return [spelling for spelling, _span in _labelled_matches(segment)]


def _labelled_matches(segment: str) -> list[tuple[str, tuple[int, int]]]:
    """:func:`_labelled_spellings` with each figure's span in ``segment``."""
    out: list[tuple[str, tuple[int, int]]] = []
    for pattern in (_LABELLED, _PHRASED, _COUNT_PHRASED):
        for match in pattern.finditer(segment):
            label = match.group("label")
            if pattern is _COUNT_PHRASED:
                starts = [end.end() for end in _SENTENCE_END.finditer(segment, 0, match.start())]
                before = segment[starts[-1] if starts else 0:match.start("num")]
                if _SKIP_WORD.search(re.sub(r"[_\W]+", " ", before)):
                    continue
            after = _TOTAL_AFTER.match(segment, match.end()) if pattern is _LABELLED else None
            if _label_ok(label) or (after and _label_ok(f"{label} {after.group('word')}")):
                out.append((match.group("num"), match.span("num")))
    return out


#: A total/sum/lower-bound figure the answer writes in prose with NO ``:``/``=``
#: delimiter, so :data:`_LABELLED` and :data:`_PHRASED` miss it. Three shapes,
#: each read then run through the UNCHANGED :func:`unfound_figures` support
#: test (no new grounding logic): a cardinal beside a count noun under a total
#: word ("324 total defects", "324 defects total"); a verb phrase asserting a
#: sum ("sums to N", "adds up to N", "totals N"); and a lower bound ("at least
#: N", "N+"). The count-noun vocabulary is the ONE set :data:`_COUNT_NOUNS`
#: (compiled lazily, since it is defined below). ``_AFTER`` still rejects a
#: percentage, decimal, duration or further digits, and ``_SKIP_WORD`` still
#: rejects an average, rate, share or change, so these shapes never read a
#: ratio or a percentage.
#: The number token without the ``num`` capture group, so a pattern can carry
#: it in more than one alternation branch (a named group may appear once).
_NUM_BARE = r"(\d{1,3}(?:,\d{3})+|\d+)"
#: A bounded set of SINGULAR modifier words permitted BETWEEN the number and
#: the count noun (so "at least 6,733 distinct defect records" reads). It is an
#: adjective / singular-noun-adjunct allowlist ONLY -- it deliberately excludes
#: any plural noun (``sites``, ``machines``, ``lines``), because a plural noun
#: in the gap means the number counts THAT plural, not the count noun ("at least
#: 3 sites reported defect records" counts sites). The count-noun singular stems
#: join it so a noun-adjunct like ``defect`` in "defect records" is admitted.
_BOUND_MODIFIER_WORDS = (
    "distinct", "unique", "total", "overall", "combined", "separate",
    "individual", "valid", "matching", "logged", "recorded", "returned",
    "defect", "failure", "quality", "segment", "reprint", "completed", "failed")
_SUM_VERB = re.compile(
    rf"\b(?:sums?|adds?)[ \t]+(?:up[ \t]+)?to[ \t]+{_NUM_BARE}{_AFTER}"
    rf"|\btotals[ \t]+{_NUM_BARE}{_AFTER}", re.I)


def _modifier_alt() -> str:
    """The bounded modifier allowlist plus the count-noun singular stems, as a
    regex alternation (so ``defect``/``segment`` noun-adjuncts are allowed in
    the gap). Cached."""
    cached = globals().get("_MODIFIER_ALT_CACHE")
    if cached is None:
        stems = {noun.rstrip("s") for noun in _COUNT_NOUNS} | set(_BOUND_MODIFIER_WORDS)
        alt = "|".join(re.escape(s) for s in sorted(stems, key=len, reverse=True))
        # A whole token only: "case-sensitive" is not the stem "case".
        cached = rf"(?:(?:{alt})(?![\w-]))"
        globals()["_MODIFIER_ALT_CACHE"] = cached
    return cached


def _count_noun_alt() -> str:
    """The count-noun vocabulary (:data:`_COUNT_NOUNS`) as a regex alternation,
    each noun matched with an optional trailing ``s``, as a whole token (never
    the head of a hyphenated modifier). Built once and cached so the ONE
    vocabulary set stays the source of truth."""
    cached = globals().get("_COUNT_NOUN_ALT_CACHE")
    if cached is None:
        stems = sorted({noun.rstrip("s") for noun in _COUNT_NOUNS}, key=len, reverse=True)
        alt = "|".join(re.escape(s) + "s?" for s in stems)
        cached = rf"(?:(?:{alt})(?![\w-]))"
        globals()["_COUNT_NOUN_ALT_CACHE"] = cached
    return cached


def _count_phrase_re():
    """The prose total/count shapes: ``N <mods> X`` where a total word is among
    the modifiers ("324 total defects"); ``N X total`` / ``N X overall`` /
    ``N X in total``; ``N X across all / every ...`` (a count over the whole
    result; "across 5 lines" is not read, it may be a subset);
    ``combined N X``; and plural-subject ``the X total N`` /
    ``X totalling N``. ``<mods>`` is the bounded allowlist (:func:`_modifier_alt`)
    so the count noun stays in the number's own noun phrase -- the reader never
    crosses a plural noun into a later phrase. Compiled once."""
    cached = globals().get("_COUNT_PHRASE_RE_CACHE")
    if cached is None:
        noun = _count_noun_alt()
        mod = _modifier_alt()
        gap = rf"(?:[ \t]+{mod}){{0,3}}"
        total_word = r"(?:total|totals|grand[ \t]+total|overall|combined)"
        cached = re.compile(
            # N <mods incl. a total word> <count noun>: "324 total defects"
            rf"(?<![\w.,/:\-]){_NUM_BARE}{_AFTER}(?:[ \t]+{mod})*[ \t]+{total_word}"
            rf"(?:[ \t]+{mod})*[ \t]+{noun}\b"
            # N <count noun> total / overall / in total: "324 defects total"
            rf"|(?<![\w.,/:\-]){_NUM_BARE}{_AFTER}{gap}[ \t]+{noun}[ \t]+(?:in[ \t]+)?{total_word}\b"
            # N <count noun> across all / every ...: "324 defects across all lines".
            # Not "across 5 lines": that may be a subset of the returned lines,
            # and past a few groups no subset sum is derived, so a right
            # subtotal would be named.
            rf"|(?<![\w.,/:\-]){_NUM_BARE}{_AFTER}{gap}[ \t]+{noun}[ \t]+across[ \t]+"
            rf"(?:all|every|the[ \t]+(?:whole|entire))\b"
            # combined N <count noun>: "combined 324 defects"
            rf"|\bcombined[ \t]+{_NUM_BARE}{_AFTER}{gap}[ \t]+{noun}\b"
            # plural-subject "the <count noun> total N" / "<count noun> totalling N"
            rf"|\b{noun}[ \t]+(?:total|totalling|totaling|add[ \t]+up[ \t]+to|sum[ \t]+to)[ \t]+"
            rf"{_NUM_BARE}{_AFTER}",
            re.I)
        globals()["_COUNT_PHRASE_RE_CACHE"] = cached
    return cached


def _lower_bound_re():
    """A lower bound of a listed or returned set: ``at least N <mods> <count
    noun>`` / ``N+ <mods> <count noun>``, where ``<mods>`` is the bounded
    allowlist (:func:`_modifier_alt`) only. Because the gap admits no plural
    noun, "at least 6,733 distinct defect records" reads while "at least 3 sites
    reported defect records" does not (``sites`` counts sites, not records), and
    "at least 2026 was the year" does not (``year`` is not a count noun).
    Compiled once. A bound naming no count noun is not read."""
    cached = globals().get("_LOWER_BOUND_RE_CACHE")
    if cached is None:
        noun = _count_noun_alt()
        mod = _modifier_alt()
        gap = rf"(?:[ \t]+{mod}){{0,3}}"
        cached = re.compile(
            rf"\b(?:at[ \t]+least|no[ \t]+fewer[ \t]+than|no[ \t]+less[ \t]+than)[ \t]+"
            rf"{_NUM_BARE}{_AFTER}{gap}[ \t]+{noun}\b"
            rf"|(?<![\w.,/:\-]){_NUM_BARE}\+{gap}[ \t]+{noun}\b",
            re.I)
        globals()["_LOWER_BOUND_RE_CACHE"] = cached
    return cached


def _total_phrase_figures(segment: str) -> list[tuple[str, bool]]:
    """``(spelling, is_lower_bound)`` for each prose total/sum/lower-bound shape
    in one segment (:func:`_count_phrase_re`, :data:`_SUM_VERB`,
    :func:`_lower_bound_re`), in text order, each spelling once. A segment
    carrying a skip word (average, rate, share, change) near a shape is left to
    the stricter labelled reader -- these shapes never read a ratio or
    percentage figure. ``is_lower_bound`` is True for an ``at least N`` / ``N+``
    reading: such a figure is held when at most the returned rows' sum. A
    segment that scopes its figure to part of the result (:data:`_SUBSET_SCOPE`:
    "from groups A and C", "for region B", "Group A ... total") is not read
    either: the checker derives no arbitrary subset sum, so a correct subtotal
    would be named."""
    out: list[tuple[str, bool]] = []
    seen: set = set()
    for number, is_bound, _span in _total_phrase_matches(segment):
        if number not in seen:
            seen.add(number)
            out.append((number, is_bound))
    return out


def _total_phrase_matches(segment: str) -> list[tuple[str, bool, tuple[int, int]]]:
    """Every figure :func:`_total_phrase_figures` reads, with its span in
    ``segment``, a figure written twice listed twice."""
    plain = re.sub(r"[_\W]+", " ", segment)
    if _SKIP_WORD.search(plain) or _SUBSET_SCOPE.search(plain):
        return []
    out: list[tuple[str, bool, tuple[int, int]]] = []
    for pattern, is_bound in ((_count_phrase_re(), False), (_SUM_VERB, False),
                              (_lower_bound_re(), True)):
        for match in pattern.finditer(segment):
            group = next((i for i, g in enumerate(match.groups(), 1) if g), None)
            if group is not None:
                out.append((match.group(group), is_bound, match.span(group)))
    return out


def _date_shaped(spelling: str) -> bool:
    """A year (1900-2100), or digits written with no separator that read as a
    compact date (``YYYYMMDD``) or a Unix time in seconds or milliseconds
    (2000-2100). Such a figure is left alone only when it IS a date or time
    this call holds (:func:`date_held`); otherwise it is checked like any
    other figure, whatever words surround it."""
    if "," in spelling or not spelling.isdigit():
        return False
    value = int(spelling)
    if len(spelling) == 4:
        return 1900 <= value <= 2100
    if len(spelling) == 8:
        year, month, day = value // 10_000, value // 100 % 100, value % 100
        return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31
    return (len(spelling) == 10 and 946_684_800 <= value < 4_102_444_800) or (
        len(spelling) == 13 and 946_684_800_000 <= value < 4_102_444_800_000)


_ISO_MOMENT = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})"
    r"(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?(?!\d)"
    r"(?:[ ]?((?:UTC|GMT)[+-]\d{1,2}(?::?\d{2})?(?!\d)|Z|UTC|GMT"
    r"|[+-]\d{2}(?::?\d{2})?(?!\d)|[A-Za-z]+(?:/[\w+\-]+)+|[A-Z]{1,4}T\b|[AP]M\b))?"
    r")?(?!\d)")
#: Text right after a time, or after its zone, that is part of a zone the
#: pattern did not read: a sign and digit (after at most one space), or any
#: letter, digit, sign, colon or slash directly after it.
_UNREAD_ZONE = re.compile(r"[ ]?[+\-]\d|[+\-:/]?[A-Za-z0-9]")
#: The UTC offsets in use span -12:00 to +14:00, in quarter hours.
_MIN_OFFSET, _MAX_OFFSET, _OFFSET_STEP = -12 * 3600, 14 * 3600, 900
#: Dates and times one call keeps; past this, date-shaped figures go unverified.
_MAX_MOMENTS = MAX_VALUES


def _offsets(zone, wall) -> tuple | None:
    """The UTC offsets (seconds) the zone written after a time can mean at
    that wall time: ``Z``/``UTC``/``GMT`` is 0, a numeric offset is itself,
    an IANA name is its offset(s) then (both, in a repeated hour). ``None``
    when no zone is written beside the time (it may be given elsewhere: a
    sibling field, a column note) or the engine cannot resolve it (an
    abbreviation such as ``PDT``, an unknown name, no zone database): any
    offset in use, so a correct reading of it is never named. Only an
    explicit zone pins the offset."""
    if not zone:
        return None
    if zone in ("Z", "UTC", "GMT"):
        return (0,)
    if zone[:3] in ("UTC", "GMT"):
        zone = zone[3:]
    numeric = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", zone)
    if numeric:
        hours, minutes = int(numeric.group(2)), int(numeric.group(3) or 0)
        if hours <= 14 and minutes < 60:
            sign = -1 if numeric.group(1) == "-" else 1
            return (sign * (hours * 3600 + minutes * 60),)
        return None
    if "/" not in zone:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone)
        return tuple({int(wall.replace(tzinfo=tz, fold=fold).utcoffset().total_seconds())
                      for fold in (0, 1)})
    except Exception:  # unknown or malformed zone name, or no zone database
        return None


def date_moments(text, into: list | None = None) -> tuple[list, bool]:
    """Each ISO date or time in ``text`` as one moment (appended to ``into``
    when given): its year, compact date as written, wall time in seconds,
    millisecond fraction floored and rounded, whether its seconds round up,
    and its zone's UTC offsets (``None`` when unresolved, including when
    zone text right after the time was not read whole, such as ``+5`` or
    ``Zulu``: a zone prefix never stands for the whole zone). A bare date or
    a time with no zone beside it is unresolved too. ``True`` second when the moments reached
    :data:`_MAX_MOMENTS`, so the date-shaped figures of the call cannot be
    checked; nothing else is affected."""
    import calendar
    from datetime import datetime
    moments = [] if into is None else into
    if not isinstance(text, str):
        return moments, False
    text = text[:_MAX_CONTEXT_CHARS]
    for match in _ISO_MOMENT.finditer(text):
        if len(moments) >= _MAX_MOMENTS:
            return moments, True
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        hour, minute, second = (int(match.group(i) or 0) for i in (4, 5, 6))
        zone = match.group(8)
        if zone in ("AM", "PM") and 1 <= hour <= 12:
            hour = hour % 12 + (12 if zone == "PM" else 0)
        try:
            wall = datetime(year, month, day, hour, minute, min(second, 59))
        except ValueError:
            continue
        tail = text[match.end():match.end() + 3]
        unread = match.group(4) is not None and bool(_UNREAD_ZONE.match(tail))
        fraction = Decimal("0." + (match.group(7) or "0")[:9]) * 1000
        moments.append((year, year * 10_000 + month * 100 + day,
                        calendar.timegm(wall.timetuple()) + (second == 60),
                        int(fraction.to_integral_value(rounding="ROUND_FLOOR")),
                        int(fraction.to_integral_value(rounding="ROUND_HALF_UP")),
                        int(fraction >= 500), None if unread else _offsets(zone, wall)))
    return moments, False


def _offset_ok(offset: int, offsets) -> bool:
    if offsets is None:
        return _MIN_OFFSET <= offset <= _MAX_OFFSET and offset % _OFFSET_STEP == 0
    return offset in offsets


def _utc_day(seconds: int) -> int:
    day = time.gmtime(seconds)
    return day.tm_year * 10_000 + day.tm_mon * 100 + day.tm_mday


def date_held(figure, moments) -> bool:
    """``figure`` is, written without separators, the year, compact date
    (as written, or the UTC day of its instant) or Unix time (seconds
    floored or rounded, milliseconds floored or rounded, of its true
    instant, fraction and zone included) of one of ``moments``. An
    unresolved zone allows any offset in use, checked here only for this
    figure rather than spelled out per moment."""
    if not isinstance(figure, Decimal) or figure != figure.to_integral_value():
        return False
    value = int(figure)
    for year, written, local, ms_floor, ms_round, up, offsets in moments:
        if value in (year, written):
            return True
        if any(_offset_ok(local - instant, offsets) for instant in {value, value - up}):
            return True
        for millis in {ms_floor, ms_round}:
            instant, rest = divmod(value - millis, 1000)
            if rest == 0 and _offset_ok(local - instant, offsets):
                return True
        if len(str(value)) == 8:
            if offsets is None:
                days = range((local - _MAX_OFFSET) // 86_400, (local - _MIN_OFFSET) // 86_400 + 1)
                if value in {_utc_day(d * 86_400) for d in days}:
                    return True
            elif value in {_utc_day(local - offset) for offset in offsets}:
                return True
    return False


def _lines(answer) -> list[str]:
    if not isinstance(answer, str):
        return []
    return _MARKUP.sub("", answer[:200_000]).splitlines()[:5_000]


#: The bounded set of count nouns a table header may reduce to. A header is a
#: count header when, after qualifiers are removed, what remains is empty (it
#: was all qualifiers, e.g. "Total") or exactly one of these; or it is "#".
_COUNT_NOUNS = frozenset({
    "count", "occurrences", "qty", "quantity", "number", "records", "rows",
    "units", "events", "defects", "failures", "cases", "items", "orders",
    "segments", "requests", "jobs", "books"})
#: Qualifier words stripped from a header before the single-noun test: they
#: qualify a count without naming a different measure.
_HEADER_QUALIFIER = frozenset({"total", "grand", "overall", "of", "the", "all"})
#: Words naming the period one table row covers ("Daily Total", "Weekly
#: Count"). Dropped before the single-noun test like the qualifiers, but a
#: header left with nothing else is a count header only when it has a total
#: word: "Daily" alone is not one.
_PERIOD_QUALIFIER = frozenset({"hourly", "daily", "weekly", "monthly", "quarterly", "yearly",
                               "annual"})
_TOTAL_QUALIFIER = frozenset({"total", "grand", "overall"})
#: A rank/index column: its cell is a position, never the row's key.
_RANK_HEADER = frozenset({"rank", "no", "num", "index", "position", "pos", "row", "line"})
#: Markdown link ``[visible](target)`` -- only the visible text is read. The
#: destination may hold one level of balanced parentheses (``a_(1)``); the
#: target is dropped, never printed or trusted.
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
_EMPHASIS = re.compile(r"\*\*|__|\*|`|~~")


def _cell_text(cell: str) -> str:
    """One table cell's visible text: markdown link targets dropped (kept as
    their visible text), emphasis and inline-code markers removed, trimmed.
    A link target is never printed or trusted."""
    if not isinstance(cell, str):
        return ""
    return _EMPHASIS.sub("", _MD_LINK.sub(r"\1", cell)).strip()


def _header_words(header: str) -> list[str]:
    """A header's count-bearing words, lowercased: markup stripped, a leading
    ``<count word> of`` phrase (``number of``, ``count of``, ``no of``) and the
    plain qualifier words removed, trailing plural ``s`` dropped. So ``Number
    of Defects`` reduces to ``defects`` and ``Total Occurrences`` to
    ``occurrences``."""
    text = re.sub(r"[_\W]+", " ", _cell_text(header)).lower()
    words = text.split()
    while len(words) >= 2 and words[0] in {"number", "count", "no", "qty"} and words[1] == "of":
        words = words[2:]
    return [word for word in words if word not in _HEADER_QUALIFIER]


def _rank_header(header: str) -> bool:
    """A rank/index column header (its single word, qualifiers removed, is a
    rank word), or the bare ``#``/``No.`` marker."""
    if _cell_text(header).strip() in {"#", "No.", "no.", "No", "#."}:
        return True
    words = _header_words(header)
    return len(words) == 1 and words[0].rstrip("s") in _RANK_HEADER


def _header_counts(header: str) -> bool:
    """A table column header under which a lone integer cell is a stated count:
    after qualifiers are removed it is empty or exactly ONE word from the
    bounded count-noun set (:data:`_COUNT_NOUNS`), or it is exactly ``#``. A
    ``%`` or any other word (a price, cost, size, time, rate, id, ...) is never
    a count header, so admission stays lexical and separate from the subtotal
    proof. A period word (:data:`_PERIOD_QUALIFIER`) qualifies a total word
    or that one count noun, so ``Daily Total`` and ``Weekly Count`` read; with
    neither, or beside any other word, it does not."""
    if not isinstance(header, str) or "%" in header:
        return False
    if _cell_text(header).strip() == "#":
        return True
    words = _header_words(header)
    if any(word in _PERIOD_QUALIFIER for word in words):
        words = [word for word in words if word not in _PERIOD_QUALIFIER]
        if not words:
            text = re.sub(r"[_\W]+", " ", _cell_text(header)).lower()
            return bool(_TOTAL_QUALIFIER & set(text.split()))
    if not words:
        return bool(re.sub(r"[_\W]+", " ", _cell_text(header)).strip())
    return len(words) == 1 and words[0].rstrip("s") in {n.rstrip("s") for n in _COUNT_NOUNS}


def _split_pipes(line: str) -> list[str]:
    """Split a markdown table row on ``|`` separators, honouring ``\\|`` escapes
    and pipes inside a backtick inline-code span. A ``\\|`` keeps its pipe as a
    literal in the cell."""
    cells, buf, in_code, i = [], [], False, 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
            buf.append(ch)
        elif ch == "|" and not in_code:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return cells


def _row_cells(line: str) -> list[str] | None:
    """A markdown table row's raw cells (trimmed), or ``None`` when the line is
    not a table row or is a rule row. Pipes escaped with ``\\`` or inside an
    inline-code span do not split the row (:func:`_split_pipes`). Visible-text
    stripping is applied where a cell is read (:func:`_cell_text`), never to the
    count figure itself."""
    if "|" not in line or _TABLE_RULE.match(line):
        return None
    inner = _split_pipes(line.strip())
    if inner and inner[0].strip() == "":
        inner = inner[1:]
    if inner and inner[-1].strip() == "":
        inner = inner[:-1]
    return [cell.strip() for cell in inner]


def _column_kinds(header: list[str]) -> tuple[set[int], set[int]]:
    """``(rank columns, count columns)`` for a header, position-aware: a rank
    header (including ``#``) in the FIRST column is a rank/index column (never a
    count); a ``#`` in any other column is a count column; a column is never
    both. A count column is one under which a lone integer cell is a stated
    count (:func:`_header_counts`)."""
    rank: set[int] = set()
    counts: set[int] = set()
    for index, head in enumerate(header):
        is_hash = _cell_text(head).strip() == "#"
        if _rank_header(head) and (index == 0 or not is_hash):
            rank.add(index)
        elif _header_counts(head):
            counts.add(index)
    return rank, counts


def _table_figures(lines: list[str]) -> list[tuple[str, int]]:
    """``(figure spelling, line index)`` for each table cell that is exactly one
    integer under a count column, with NO key attributed. A table is a header
    row, a rule row, then data rows; count columns are classified position-aware
    (:func:`_column_kinds`), and the rank column and every non-count column are
    never read. A table count cell is checked for support exactly like a
    labelled figure (:func:`unfound_figures`)."""
    out: list[tuple[str, int]] = []
    index = 0
    while index < len(lines):
        header = _row_cells(lines[index])
        if (header is None or index + 1 >= len(lines)
                or not _TABLE_RULE.match(lines[index + 1])):
            index += 1
            continue
        row = index + 2
        data = []
        while row < len(lines) and (cells := _row_cells(lines[row])) is not None:
            data.append((row, cells))
            row += 1
        _rank, counts = _column_kinds(header)
        for line_no, cells in data:
            if len(cells) != len(header):
                continue
            for i in counts:
                match = _LISTED.fullmatch(_cell_text(cells[i]))
                if match and "." not in match.group("num"):
                    out.append((match.group("num"), line_no))
        index = row if row > index + 2 else index + 1
    return out


# --------------------------------------------------------------------------- #
# 3b. Counts stated without a total word
# --------------------------------------------------------------------------- #
# Measured on real answers, the system agent (and the orchestrator) write most
# counts with NO total word: ``N <count noun>`` (``13,656 segment records``),
# ``<label naming a count noun>: N`` (``FAILED segments: 13,656``), or a cell
# under a multi-word count header (``Record Count``). The labelled, phrased and
# table-header readers above miss them (the label test needs a TOTAL word, the
# header test needs a ONE-word count header). :func:`_count_shapes` reads those
# shapes, with its own pieces (built on the same count-noun vocabulary, number
# spelling, skip words and identifier/date/time words), so the in-app check and
# the evaluator read one and the same set. A figure it reads goes through the
# SAME support tests as a labelled total (:func:`unfound_figures`): nothing new
# grounds a figure here, only what is READ widens.
_STEMS = frozenset(noun.rstrip("s") for noun in _COUNT_NOUNS)
#: Table measure words, not things a prose figure counts ("Line 2 count").
_MEASURES = frozenset({"count", "number", "qty", "quantity"})
#: The plural forms ``N <count noun>`` needs: a singular after a figure is a
#: noun adjunct ("75 defect codes" counts codes), never the head. A count of
#: one is read apart (:data:`_ONE_THEN_NOUN`).
_PLURAL = "|".join(sorted((re.escape(s) + "s" for s in _STEMS - _MEASURES),
                          key=len, reverse=True))
_SINGULAR = "|".join(sorted((re.escape(s) for s in _STEMS - _MEASURES), key=len, reverse=True))
#: The figure as the reader spells it, never after a sign, an approximation
#: sign, ``#`` or another number's pieces.
_FIGURE_BEFORE = r"(?<![\w.,/:\-+#~$<>\u2248\u00b1])"
#: ``N <up to 3 words> <count noun>``.
_FIGURE_THEN_NOUN = re.compile(
    rf"{_FIGURE_BEFORE}{_NUM_BARE}{_AFTER}"
    rf"(?P<gap>(?:[ \t]+[A-Za-z][\w-]*){{0,3}}?)[ \t]+(?P<noun>{_PLURAL})(?![\w-])", re.I)
#: ``1 <up to 3 words> <singular count noun>`` ("Exactly 1 record matched"):
#: after exactly one, a singular count noun is the head, so the plural rule
#: for any other figure is unchanged.
_ONE_THEN_NOUN = re.compile(
    rf"{_FIGURE_BEFORE}(1){_AFTER}"
    rf"(?P<gap>(?:[ \t]+[A-Za-z][\w-]*){{0,3}}?)[ \t]+(?P<noun>{_SINGULAR})(?![\w-])", re.I)
_FIGURE_SIGN = re.compile(r"[~\u2248<>\u2264\u2265]\s*$")
_APPROXIMATE = re.compile(
    r"\b(?:about|approximately|approx|around|roughly|nearly|almost|circa|ca|some|over|under"
    r"|above|below|beyond|upwards of|close to|more than|less than|fewer than|up to|at most"
    r"|at least|no more than|no fewer than|no less than|estimated|est)\W*$", re.I)
_RATE_AFTER = re.compile(r"\s*(?:/|per\b|an?\s+(?:second|minute|hour|day|week|month|shift)\b)",
                         re.I)
_NOT_IN_GAP = re.compile(r"(?:the|an?|and|or|than|to|vs|versus|of|from|for|in|by|per|at|on"
                         r"|under|with|across|within|during)", re.I)
#: A plural-looking gap word: one there means N counts that, not the noun
#: ("3 sites reported item records"). Not ``-ss``/``-is``/``-us`` words.
_PLURAL_WORD = re.compile(r".*[^siu]s", re.I)
_PARENTHESES = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
#: A label's colon that is a clock time's (``2031-03-04 00:00``): the
#: "figure" after it is the minutes. Searched up to the end of the match.
_CLOCK = re.compile(r"(?<![\w.])\d{1,2}:\d{2}\Z")
#: Where a count label's head ends besides its first preposition ("Records
#: matching defect name X": the label counts records).
_POSTMODIFIER = re.compile(r"\b(?:matching|where|that|which|whose|having|containing|named)\b",
                           re.I)
#: A per-unit rate in a label: ``per <unit>``, or a count noun then ``/``.
_RATE_LABEL = re.compile(
    rf"\bper\b|\b(?:{'|'.join(sorted(map(re.escape, _STEMS), key=len, reverse=True))})s?"
    rf"\s*/\s*[A-Za-z]", re.I)
#: A scale word after a figure (``109 million records``, ``95 M rows``,
#: ``12 bn records``): the figure is a rounding of a larger count, never that
#: count, so a count shape does not read it (an approximation, like ``about N``).
_SCALE_WORD = re.compile(r"(?:thousands?|millions?|billions?|trillions?|k|m|b|bn|mn)", re.I)
#: A count shape already has count context (its label or its noun names a
#: count), so a one-letter ``K`` / ``M`` / ``B`` after its figure is a scale.
_SCALE_AFTER = re.compile(rf"{_SCALED}|{_LETTER_SCALE}", re.I)


def _count_shapes(lines: list[str], known: set, limit: int | None = MAX_FIGURES,
                  *, cells: bool = True) -> list[tuple[str, tuple[int, ...]]]:
    """``(spelling, line indexes)`` of each figure stated as ``N <count noun>``,
    ``<label naming a count noun>: N`` or, with ``cells``, a cell under a
    multi-word count header (:func:`_count_header_cells`) that is not in
    ``known``: at most the reader's prose budget (``MAX_FIGURES``) of the prose
    shapes and its table budget (``MAX_TABLE_FIGURES``) of the cells, or all
    with ``limit`` None. The in-app reader passes ``cells=False``: which rows a
    table count covers (a picked set of groups, a rank, a code) is not decidable
    from the row text, so those cells stay the evaluator's. Markdown emphasis
    and link targets are dropped first (``_cell_text``)."""
    prose: dict = {}
    for index, line in enumerate(lines):
        for segment in _cell_text(line).split("|"):
            for spelling in _segment_counts(segment):
                if spelling not in known:
                    prose.setdefault(spelling, []).append(index)
    table: dict = {}
    for spelling, index in _count_header_cells(lines) if cells else ():
        if spelling not in known and spelling not in prose:
            table.setdefault(spelling, []).append(index)
    cut = (None, None) if limit is None else (limit, MAX_TABLE_FIGURES)
    found = [(s, tuple(dict.fromkeys(i))) for s, i in list(prose.items())[:cut[0]]]
    found += [(s, tuple(dict.fromkeys(i))) for s, i in list(table.items())[:cut[1]]]
    return sorted(found, key=lambda item: item[1][0])


def _count_header_cells(lines: list[str]) -> list[tuple[str, int]]:
    """``(figure, line index)`` for each table cell that is exactly one integer
    under a column the reader does not read as a count column
    (``_column_kinds``) but whose header names a count noun the way a count
    label does (:func:`_count_label`: ``Record Count``, ``Production record
    count``); never a ``%`` header or the rank column."""
    out, index = [], 0
    while index + 1 < len(lines):
        header = _row_cells(lines[index])
        if header is None or not _TABLE_RULE.match(lines[index + 1]):
            index += 1
            continue
        rank, counted = _column_kinds(header)
        columns = [i for i, head in enumerate(header) if i not in rank | counted
                   and "%" not in head and _count_label(_cell_text(head))]
        row = index + 2
        while row < len(lines) and (cells := _row_cells(lines[row])) is not None:
            for i in columns if len(cells) == len(header) else ():
                match = _LISTED.fullmatch(_cell_text(cells[i]))
                if match and "." not in match.group("num"):
                    out.append((match.group("num"), row))
            row += 1
        index = max(row, index + 1)
    return out


def _segment_counts(segment: str) -> list[str]:
    out = [m.group(1) for pattern in (_FIGURE_THEN_NOUN, _ONE_THEN_NOUN)
           for m in pattern.finditer(segment) if _counts_noun(segment, m)]
    out += [m.group("num") for m in _LABELLED.finditer(segment)
            if segment[m.end():m.end() + 1] != "+" and not _CLOCK.search(segment, 0, m.end())
            and not _SCALE_AFTER.match(segment, m.end()) and _count_label(m.group("label"))]
    return out


def _counts_noun(segment: str, match) -> bool:
    """``N <gap> <noun>`` states a count of that noun: no gap word is plural,
    a preposition, a scale word (:data:`_SCALE_WORD`) or an identifier, date
    or time word; the word before N is no such word and no approximation; its
    sentence has no average/rate/share/change word; no per-unit rate follows
    the noun."""
    gap = match.group("gap").split()
    if any(_PLURAL_WORD.fullmatch(w) or _NOT_IN_GAP.fullmatch(w) or _NOT_A_COUNT.search(w)
           or _SCALE_WORD.fullmatch(w) for w in gap):
        return False
    before = segment[:match.start()]
    sentence = _count_sentence(segment, match.start(), match.end())
    previous = re.sub(r"[_\W]+", " ", before).split()[-1:]
    return not (_FIGURE_SIGN.search(before) or _APPROXIMATE.search(before)
                or (previous and _NOT_A_COUNT.fullmatch(previous[0]))
                or _SKIP_WORD.search(re.sub(r"[_\W]+", " ", sentence))
                or _RATE_AFTER.match(segment, match.end()))


def _count_sentence(segment: str, start: int, end: int) -> str:
    """The sentence of ``segment`` holding ``[start, end)``."""
    begin = max((m.end() for m in _SENTENCE_END.finditer(segment, 0, start)), default=0)
    after = _SENTENCE_END.search(segment, end)
    return segment[begin:after.start() if after else len(segment)]


def _count_label(label: str) -> bool:
    """A label naming a count noun: in its own sentence, parentheses and a
    list marker dropped, no average/rate/share/change word and no per-unit
    rate (``Records per hour``, ``Records/hour``, parentheses included), and
    its head (its first clause, before its first preposition or
    post-modifier) ends on a count noun and names no identifier, date or time
    unless it asks for one."""
    sentence = _SENTENCE_END.split(label)[-1]
    if _RATE_LABEL.search(sentence):
        return False
    text = _LIST_ITEM.sub("", _PARENTHESES.sub(" ", sentence).strip())
    words = re.sub(r"[_\W]+", " ", text).strip()
    if not words or _SKIP_WORD.search(words):
        return False
    head = re.sub(r"[_\W]+", " ", _CLAUSE_END.split(text, maxsplit=1)[0])
    head = _POSTMODIFIER.split(_PREPOSITION.split(head, maxsplit=1)[0], maxsplit=1)[0]
    if not head.split() or head.split()[-1].lower().rstrip("s") not in _STEMS:
        return False
    return not _NOT_A_COUNT.search(head) or bool(_COUNT_ASKED.search(words))


def unfound_count_shapes(answer: str, values: dict, question: str) -> list[str]:
    """The count-shape figures (:func:`_count_shapes`) nothing accounts for, by
    the reader's own support tests (:func:`_figure_held`: a value, a pair, the
    question or prompt, a date this call holds; :func:`_occurrence_sums`: the
    figures listed beside it). Past a cut, as the reader treats a prose total:
    checked against the complete results, held when a cut result could reach it
    (:func:`_past_cuts`), and not read when scoped to part of a result
    (:func:`_scoped_spellings`).

    The evaluator calls this with its own value view (pair operands restricted
    to the counts the results state) so a count shape is checked exactly as a
    labelled total is under that view; the in-app path reads these same shapes
    through :func:`unfound_figures` instead. The ``known`` set is the labelled,
    phrased and table figures (:func:`_stated`), so the count shapes read here
    are only the ones those readers miss."""
    lines = _lines(answer)
    counts = _count_shapes(lines, {spelling for spelling, _i in _stated(lines)})
    view, cuts = _count_view(values)
    if not counts or view is None:
        return []
    if cuts is not None:
        scoped = _scoped_spellings(lines)
        counts = [(s, i) for s, i in counts if s not in scoped]
    checks = _figure_checks(view, [], question)
    if checks is None:
        return []
    out, cache = [], {}
    for spelling, indexes in counts:
        figure = Decimal(spelling.replace(",", ""))
        if _figure_held(figure, spelling, (), checks) or (
                cuts is not None and not _past_cuts(figure, cuts)):
            continue
        sums, whole = _occurrence_sums(lines, indexes, cache, figure)
        if whole and figure not in sums:
            out.append(spelling)
    return out


def _count_view(values: dict) -> tuple[dict | None, list | None]:
    """``(values to check against, cut summaries or None)``: past a cut, the
    complete results' view (``None`` when a cut or that view is unreadable, as
    the reader's :func:`_unfound_past_cut` decides)."""
    if not values.get("cut"):
        return values, None
    cuts, complete = values.get("cuts"), values.get("complete")
    if not (isinstance(cuts, list) and cuts and all(isinstance(c, dict) for c in cuts)
            and isinstance(complete, dict)
            and all(isinstance(complete.get(k), set) for k in ("raw", "data", "derived"))):
        return None, None
    return {**values, **{k: complete[k] for k in ("raw", "data", "derived")}}, cuts


def _stated(lines: list[str]) -> list[tuple[str, tuple[int, ...]]]:
    """``(spelling, line indexes)`` of each figure stated as a total or count,
    each spelling once, in order of its first occurrence: a labelled or phrased
    figure, and a count read from a table cell (:func:`_table_figures`, no key
    attributed). ``line indexes`` are every line it occurs on, in text order,
    so the listing beside any occurrence is consulted, however often the
    figure recurs before the one that sits next to its listing. A spelling is
    a table count cell when ANY occurrence is one. Table count cells get their
    own budget (:data:`MAX_TABLE_FIGURES`), kept ahead of prose figures (which
    keep :data:`MAX_FIGURES`), so a wide table is checked whole even behind
    many prose figures, and a prose repeat cannot move a table cell onto the
    smaller prose budget.

    A figure is checked as one spelling: a listing that supports it beside any
    occurrence supports it everywhere in the answer. So a wrong claim that
    happens to equal a correct total stated elsewhere is not named. That is
    the chosen trade-off, since naming a correct total would be the costlier
    mistake."""
    seen: dict[str, tuple[list[int], list[int]]] = {}

    def _note(spelling: str, index: int, table: bool) -> None:
        kind = seen.setdefault(spelling, ([], []))[1 if table else 0]
        if index not in kind:
            kind.append(index)

    for index, line in enumerate(lines):
        for segment in line.split("|"):
            for spelling in _labelled_spellings(segment):
                _note(spelling, index, False)
            for spelling, _is_bound in _total_phrase_figures(segment):
                _note(spelling, index, False)
    for spelling, index in _table_figures(lines):
        _note(spelling, index, True)
    indexes = {s: tuple(sorted(set(p) | set(t))) for s, (p, t) in seen.items()}
    order = sorted(seen, key=lambda s: indexes[s][0])
    table = [s for s in order if seen[s][1]][:MAX_TABLE_FIGURES]
    prose = [s for s in order if not seen[s][1]][:MAX_FIGURES]
    keep = set(table) | set(prose)
    return [(s, indexes[s]) for s in order if s in keep]


def _lower_bound_spellings(lines: list[str]) -> set:
    """The spellings any ``at least N <count noun>`` / ``N+ <count noun>`` lower
    bound states, EXCLUDING any spelling that also occurs as a table count cell
    (a cell carries no ``at least`` qualifier, so it is checked as an exact
    count, not a bound). These are held when at most the returned rows' sum."""
    bounds: set = set()
    for line in lines:
        for segment in line.split("|"):
            # Not through _total_phrase_figures: its subset-scope rule decides
            # which prose figures are READ, never whether a figure another
            # reader admitted is a bound ("For this result, total: 150+").
            if _SKIP_WORD.search(re.sub(r"[_\W]+", " ", segment)):
                continue
            for match in _lower_bound_re().finditer(segment):
                number = next((g for g in match.groups() if g), None)
                if number is not None:
                    bounds.add(number)
    return bounds - {s for s, _i in _table_figures(lines)}


def _stated_counts(lines: list[str]) -> list[tuple[str, tuple[int, ...]]]:
    """``_stated`` plus the count shapes (:func:`_count_shapes`) the labelled,
    phrased and table-header readers miss, each spelling once, in order of its
    first occurrence. The count shapes exclude any spelling ``_stated`` already
    reads; for the IN-APP reader they also exclude a figure scoped to part of a
    result (:func:`_count_shape_scoped`), the same conservatism the prose total
    reader applies, so a correct subtotal written ``across 5 sites`` or ``from
    groups A, C and E`` is not newly named, and they leave out the cells under a
    multi-word count header (:func:`_count_shapes` ``cells=False``). The
    evaluator reads the shapes directly through :func:`_count_shapes` (its
    support view decides naming), so its behaviour is unchanged."""
    stated = _stated(lines)
    known = {spelling for spelling, _i in stated}
    scoped = _count_shape_scoped(lines)
    counts = [(s, i) for s, i in _count_shapes(lines, known, cells=False) if s not in scoped]
    return sorted(stated + counts, key=lambda item: item[1][0])


#: A count noun immediately followed by a subset ``across`` phrase: ``across``
#: and then anything other than all / every / the whole / the entire. ``across
#: all lines`` is the whole result and is read; ``across 5 sites`` or ``across
#: multiple cells`` is a subset and is not.
_ACROSS_SUBSET = re.compile(
    r"\s+across\s+(?!all\b|every\b|each\b|the\s+(?:whole|entire)\b)", re.I)


def _count_shape_scoped(lines: list[str]) -> set:
    """The count-shape spellings the IN-APP reader leaves alone because they
    are scoped to part of a result: a figure the prose reader's subset, rank or
    picked scope catches (:func:`_scoped_spellings`), a count noun followed by
    a subset ``across`` phrase (:data:`_ACROSS_SUBSET`), or a count whose label
    names two or more groups in a list (:func:`_picked_label`). The evaluator
    does not apply this; it reads every shape and lets its support view
    decide."""
    scoped = set(_scoped_spellings(lines))
    for line in lines:
        for segment in _cell_text(line).split("|"):
            scoped |= _scoped_in_segment(segment)
    return scoped


def _scoped_in_segment(segment: str) -> set:
    """The count-shape spellings one segment scopes (:func:`_count_shape_scoped`)."""
    out = set()
    for pattern in (_FIGURE_THEN_NOUN, _ONE_THEN_NOUN):
        for match in pattern.finditer(segment):
            if not _counts_noun(segment, match):
                continue
            if (_ACROSS_SUBSET.match(segment, match.end("noun"))
                    or _picked_before(segment, match.start())
                    or _picked_after(segment, match.end("noun"))
                    or _unsure_count(segment, match.start(), match.end())):
                out.add(match.group(1))
    for match in _LABELLED.finditer(segment):
        label = _LABEL_START.split(match.group("label"))[-1]
        if _count_label(match.group("label")) and (
                _picked_label(label, True) or _unsure_count(segment, match.start("num"),
                                                            match.end("num"))):
            out.add(match.group("num"))
    return out


#: The word before a figure that makes it a classifier, not a count
#: (``Version 2 records``, ``Line 2 production records``).
_CLASSIFIER = re.compile(
    r"(?:versions?|v|types?|lines?|models?|class(?:es)?|levels?|phases?|sections?|categor(?:y|ies)"
    r"|stages?|steps?|rev(?:ision)?s?|releases?|tiers?|grades?|lanes?|zones?|parts?|pages?)", re.I)
#: Just before a figure: a range or a bound (``between 100 and 200``, ``a
#: minimum of``, ``an estimate of``).
_BOUND_BEFORE = re.compile(
    r"\b(?:between(?:\s+[\d,]+\s+(?:and|to))?|(?:minimum|maximum|min|max)\s+of|minimum|maximum"
    r"|an?\s+estimate\s+of|estimate\s+of)\W*$", re.I)
#: In a count's sentence: rounding, an estimate, a calculation or a rate, so its
#: figure may be a derived or approximate value, not a count a result returns.
_UNSURE_SENTENCE = re.compile(
    r"\b(?:rounded|rounding|nearest|estimates?|estimated|averaged|averages|averaging|each"
    r"|every|times|multiplied|multiply|divided|dividing|divide|daily|hourly|weekly|monthly"
    r"|quarterly|yearly|annually)\b", re.I)


def _unsure_count(segment: str, start: int, end: int) -> bool:
    """The IN-APP reader skips a count shape whose figure (``[start, end)``)
    may not be an exact count of its noun: a classifier word right before it
    (:data:`_CLASSIFIER`), a range or bound before it (:data:`_BOUND_BEFORE`),
    or a rounding, estimate, calculation or rate word in its sentence
    (:data:`_UNSURE_SENTENCE`). A miss here is the cost of never naming a right
    figure; the evaluator still reads these shapes."""
    before = segment[:start]
    previous = re.sub(r"[_\W]+", " ", before).split()[-1:]
    return bool((previous and _CLASSIFIER.fullmatch(previous[0])) or _BOUND_BEFORE.search(before)
                or _UNSURE_SENTENCE.search(_count_sentence(segment, start, end)))


#: Where a count shape's label starts: the end of the sentence before it. Not
#: ``;``, which also separates the items of a list.
_LABEL_START = re.compile(r"[.!?](?=\s|$)")
_PAREN_GROUP = re.compile(r"\(([^()]*)\)")
_LIST_JOIN = re.compile(r"\s*(?:[,;&]|\band\b|\bor\b)\s*", re.I)
#: Two items joined by and / or / & (``code_a and code_b``); a name joined by
#: underscores (``A_AND_B``) is one item.
_CONJOINED = re.compile(r"\S\s+(?:and|or|&)\s+\S", re.I)
#: A serial list in running text: ``a, b and c`` or ``a, b, or c``. Anchored on
#: the last comma (the item before it, the item after it, then the joiner) so a
#: long label is scanned once, not once per start position.
_SERIAL_LIST = re.compile(r"[^,;:()],[^,;:()]+(?:\s|,\s+)(?:and|or|&)\s+\S", re.I)  # linear, no nested quantifier: 20,000-char worst cases take under 10 ms (tests/unit/test_answer_figures_serial_list_time.py)  # nosemgrep: regex_dos
_DIGIT_GROUP = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _picked_before(segment: str, start: int) -> bool:
    """The label before the figure at ``start`` names a picked set of groups:
    the text from its sentence's start, up to its last colon when it has one."""
    begin = max((m.end() for m in _LABEL_START.finditer(segment, 0, start)), default=0)
    text = segment[begin:start]
    if ":" in text:
        return _picked_label(text[:text.rfind(":")], True)
    return _picked_label(text, False) or _short_list(text)


def _picked_after(segment: str, end: int) -> bool:
    """The rest of the count noun's sentence, from ``end``, names a picked set
    of groups (:func:`_short_list`: ``1,110 records in a, b and c``,
    ``(a, b, c)``, ``a; b; and c``). One group, or a long clause after a comma,
    is not a list."""
    after = _LABEL_START.search(segment, end)
    return _short_list(segment[end:after.start() if after else len(segment)])


#: A list item is short: a picked group is named in at most three words.
_LIST_ITEM_WORDS = 3


def _short_list(text: str) -> bool:
    """``text`` is a list of three or more short items (each at most
    :data:`_LIST_ITEM_WORDS` words) joined by commas, semicolons, and / or /
    ``&``, brackets dropped and digit-group commas kept inside their number:
    ``in a, b, c``, ``(a, b, and c)``, ``a and b and c yield``."""
    text = _DIGIT_GROUP.sub("", re.sub(r"[()\[\]]", " ", text))
    items = [item.split() for item in _LIST_JOIN.split(text) if item.strip()]
    return len(items) >= 3 and all(len(item) <= _LIST_ITEM_WORDS for item in items)


def _picked_label(label: str, colon: bool) -> bool:
    """``label`` names two or more groups in a list: a parenthesized list of
    two or more items (``(a, b, c)``, ``(a; b or c)``); outside parentheses,
    before a colon, items joined by and / or / & or by two or more commas or
    semicolons; in running text, a serial list (``a, b and c``). Read from the
    line alone, never from a result. A digit-group comma (``1,770,923``) is
    part of a number, not a list."""
    label = _DIGIT_GROUP.sub("", label)
    if any(len([i for i in _LIST_JOIN.split(inner) if i.strip()]) >= 2
           for inner in _PAREN_GROUP.findall(label)):
        return True
    bare = _PAREN_GROUP.sub(" ", label)
    if not colon:
        return bool(_SERIAL_LIST.search(bare))
    return bool(_CONJOINED.search(bare)) or len(re.findall(r"[,;]", bare)) >= 2


def stated_figures(answer) -> list[str]:
    """The spellings of the integer figures ``answer`` presents as a total or
    count (module docstring), in text order, each once. Includes the count
    shapes a system agent writes without a total word (:func:`_count_shapes`)."""
    return [spelling for spelling, _indexes in _stated_counts(_lines(answer))]


def _listing(lines: list[str], start: int, step: int) -> list[str]:
    """The list or table lines next to ``start`` going ``step`` (-1 up, +1
    down), past at most a few blank lines, rules and headings."""
    index, skipped, block = start + step, 0, []
    while 0 <= index < len(lines) and not block and skipped < 4:
        line = lines[index]
        if _LIST_ITEM.match(line) or line.lstrip().startswith("|"):
            break
        if not (_GAP.match(line) or (step > 0 and line.strip() and not _LISTED.search(line))):
            return []
        index, skipped = index + step, skipped + 1
    while 0 <= index < len(lines) and len(block) < _MAX_LIST_LINES:
        line = lines[index]
        if not (_LIST_ITEM.match(line) or line.lstrip().startswith("|")):
            break
        if not _TABLE_RULE.match(line):
            block.append(line)
        index += step
    return block


def _listed_rows(block: list[str]) -> list[list[tuple[str, Decimal]]]:
    """Each figure column of a list or table, in line order, as ``(rest of
    the row, figure)``: a list item's last figure, a table cell that is one
    figure; only columns of two or more. The rest of the row is the item
    without its marker and that figure, or the row's other cells."""
    columns: dict = {}
    for line in block:
        if line.lstrip().startswith("|"):
            cells = line.strip().strip("|").split("|")
            found = [(i, m, " | ".join(c for j, c in enumerate(cells) if j != i))
                     for i, cell in enumerate(cells) if (m := _LISTED.fullmatch(cell.strip()))]
        else:
            item = _LIST_ITEM.sub("", line)
            matches = list(_LISTED.finditer(item))
            found = ([("item", matches[-1],
                       item[:matches[-1].start()] + item[matches[-1].end():])]
                     if matches else [])
        for key, match, rest in found:
            columns.setdefault(key, []).append(
                (rest, Decimal(match.group("num").replace(",", ""))))
    return [values for values in columns.values() if len(values) >= 2]


def _column_sums(block: list[str]) -> list[Decimal]:
    """The sum of each figure column of a list or table (:func:`_listed_rows`)."""
    return [sum((value for _rest, value in values), Decimal(0))
            for values in _listed_rows(block)]


def listed_sums(lines: list[str], index: int) -> list[Decimal]:
    """What the figures the answer lists just above and just below line
    ``index`` add up to, one sum per figure column."""
    sums: list[Decimal] = []
    for step in (-1, 1):
        for value in _column_sums(_listing(lines, index, step)):
            if value not in sums:
                sums.append(value)
    return sums


#: How many distinct answer lines have their neighbouring listings read per
#: answer. A figure whose occurrences run past this before one supports it is
#: not named, so a very long answer costs bounded work and never a false alarm.
MAX_LISTING_READS = 512


def _occurrence_sums(lines: list[str], indexes, cache: dict, figure=None):
    """``(sums, whole)``: :func:`listed_sums` beside each of one figure's
    occurrences, merged in order, so the same figure written in a sentence and
    in a table is supported by the listing beside either. Reading stops at the
    first occurrence whose listing adds up to ``figure``. ``cache`` holds each
    line's sums for one answer (at most :data:`MAX_LISTING_READS` lines);
    ``whole`` is False when it filled before every occurrence was read."""
    sums: list[Decimal] = []
    for index in indexes:
        if index not in cache:
            if len(cache) >= MAX_LISTING_READS:
                return sums, False
            cache[index] = listed_sums(lines, index)
        for value in cache[index]:
            if value not in sums:
                sums.append(value)
        if figure is not None and figure in sums:
            break
    return sums, True


def _total_columns(statements) -> list[tuple[list[Decimal], Decimal, list, int]]:
    """``(cells, total, row keys, query ordinal)`` for each total column of
    each complete grouped result whose cells and row keys its totals record
    kept (``cells``, ``row_keys``, :func:`totals_record`); the ordinal numbers
    its query as :func:`_ordered_totals` does."""
    out, ordinal = [], 0
    for statement in statements or ():
        sql = statement.get("query_text") if isinstance(statement, dict) else None
        if not isinstance(sql, str) or not sql.strip() or any(
                marker in sql.lower() for marker in _METADATA_MARKERS):
            continue
        ordinal += 1
        record = statement.get("totals")
        if not isinstance(record, dict):
            continue
        cells, totals, keys = record.get("cells"), record.get("totals"), record.get("row_keys")
        if not (isinstance(cells, list) and isinstance(totals, list) and isinstance(keys, list)):
            continue
        for item, column in zip(totals, cells):
            if not (isinstance(column, list) and 2 <= len(column) == len(keys)
                    and isinstance(item, (tuple, list)) and len(item) >= 2):
                continue
            try:
                out.append(([Decimal(str(cell)) for cell in column], Decimal(str(item[1])),
                            keys, ordinal))
            except (InvalidOperation, ValueError):
                continue
    return out


def _reproduces(listed: list[tuple[str, Decimal]], cells: list[Decimal], keys: list) -> bool:
    """``listed`` is a result column's leading rows in its row order (at least
    :data:`_MIN_TIED_PREFIX` of them) or all its rows in any order, each
    listed row naming that row's keys (:func:`_keys_listed`)."""
    values = [value for _rest, value in listed]
    if len(values) == len(cells):
        if sorted(values) != sorted(cells):
            return False
    elif not (_MIN_TIED_PREFIX <= len(values) < len(cells) and values == cells[:len(values)]):
        return False
    return _keys_listed(listed, cells, keys)


def _keys_listed(listed: list[tuple[str, Decimal]], cells: list[Decimal], keys: list) -> bool:
    """Each listed row writes, beside its figure, every grouping key of a
    distinct result row with that figure (rows that share a figure in any
    order among themselves); a row whose keys were not kept matches none."""
    used: set = set()
    for rest, value in listed:
        folded, best = rest.casefold(), None
        for row, (cell, row_keys) in enumerate(zip(cells, keys)):
            if row in used or cell != value or not isinstance(row_keys, list) or not row_keys:
                continue
            if all(isinstance(key, str) and _names_key(folded, key) for key in row_keys):
                size = sum(len(key) for key in row_keys)
                if best is None or size > best[0]:
                    best = (size, row)
        if best is None:
            return False
        used.add(best[1])
    return True


def _names_key(folded: str, key: str) -> bool:
    """``folded`` (casefolded) writes ``key`` as a whole token: not inside a
    longer word."""
    key = key.casefold()
    start = folded.find(key) if key else -1
    while start >= 0:
        end = start + len(key)
        before = folded[start - 1] if start else " "
        after = folded[end] if end < len(folded) else " "
        if not (before.isalnum() or before == "_" or after.isalnum() or after == "_"):
            return True
        start = folded.find(key, start + 1)
    return False


def _tied_totals(lines: list[str], indexes, columns: list, cache: dict) -> list | None:
    """``(cells, total, row keys, query ordinal)`` of each result column whose
    rows the answer lists beside one of a figure's occurrences (the listing
    :func:`listed_sums` reads, in line order; :func:`_reproduces`). ``cache``
    holds each line's listings for one answer, at most
    :data:`MAX_LISTING_READS` lines; ``None`` when it fills first, so the
    figure is checked as if written beside none."""
    tied: list = []
    for index in indexes:
        if index not in cache:
            if len(cache) >= MAX_LISTING_READS:
                return None
            above, below = _listing(lines, index, -1), _listing(lines, index, 1)
            cache[index] = _listed_rows(above[::-1]) + _listed_rows(below)
        for listed in cache[index]:
            for column in columns:
                if column not in tied and _reproduces(listed, column[0], column[2]):
                    tied.append(column)
    return tied


def _tied_checks(checks: dict, figure: Decimal, tied) -> list[dict] | None:
    """The checks for a figure written beside the rows of the result columns
    in ``tied``: ``None`` (checked with ``checks``, as any figure) when it is
    tied to none or is one of their totals; else one record per tied column
    (:func:`_own_checks`), and the figure is held only when one of them alone
    holds it. So neither another result's values nor two tied results' cells
    together hold it."""
    if not tied or any(figure == total for _cells, total, _keys, _ordinal in tied):
        return None
    return [_own_checks(checks, cells, total) for cells, total, _keys, _ordinal in tied]


def _own_checks(checks: dict, cells: list[Decimal], total: Decimal) -> dict:
    """``checks`` read against one result column only: its own cells, its row
    count and the engine totals (``exact``: another result's total written
    beside these rows is that result's figure, and a sum of two totals is a
    total). The question's own figures still hold it (``asked``); the
    call-wide values, rounding, row count, question ranges and dates do not.

    Of the column's own values (``own``: its ``total`` and its cells), a pair
    holds the figure only as a sum of two of its cells (``cells``), which can
    be the correct total of two rows the prose names. Its total with one of
    its own values, and a difference of two of its cells, do not: a total
    less or plus one of its rows is a misadd, not its total. Another query's
    engine total still pairs with any of them (both days together), and a
    cell equal to one is read as that total, as a figure equal to it is.
    When two results share its total, that total twice holds it too
    (``exact_twice``), and nothing else of the pair rule changes.

    Another query's engine total stays in, as on the base: the check reads
    figures, not which result a label names, so dropping it would name a
    correct next-section total ("Day two: 1,113") along with a wrong one
    ("Day one total: 1,113") whose figure is exactly another query's total."""
    own = set(checks["exact"]) | set(cells)
    others = set(checks["exact"]) - {total}
    mine = {total} | {cell for cell in cells if cell not in others}
    return {**checks, "ranges": [], "moments": [], "dates_cut": False, "rows": len(cells),
            "single": own | checks.get("exact_twice", set()),
            "pairs": own | {value.copy_negate() for value in own},
            "own": mine | {value.copy_negate() for value in mine}, "cells": set(cells),
            "rough": [value for value in own if value != value.to_integral_value()]}


def _held_as_stated(figure: Decimal, spelling: str, checks: dict, owns) -> bool:
    """``figure`` is held by ``checks``, or, for a tied figure (``owns``,
    :func:`_tied_checks`), by one tied result's own checks."""
    if owns is None:
        return _figure_held(figure, spelling, (), checks)
    return any(_figure_held(figure, spelling, (), own) for own in owns)


def _beside(tied, owns, held: bool) -> tuple:
    """``(query ordinal, total)`` of each result a named figure is tied to,
    sorted, when only that tie names it (``held``: the call-wide checks would
    hold it); else ``()``."""
    if owns is None or not held:
        return ()
    return tuple(sorted({(ordinal, total) for _cells, total, _keys, ordinal in tied}))


#: A number with a magnitude suffix or word (``8.3M``, ``12 k``, ``1.2bn``,
#: ``3 thousand``), as a question or context may abbreviate a figure.
_MAGNITUDE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,6}))?[ \t]?"
    r"(k|m|bn|b|thousand|million|billion)(?!\w)", re.I)
_MULTIPLIER = {"k": 10 ** 3, "thousand": 10 ** 3, "m": 10 ** 6, "million": 10 ** 6,
               "b": 10 ** 9, "bn": 10 ** 9, "billion": 10 ** 9}
MAX_QUESTION_RANGES = 32
_MAX_QUESTION_CHARS = 20_000


def question_ranges(text, limit: int = MAX_QUESTION_RANGES,
                    max_chars: int | None = _MAX_QUESTION_CHARS):
    """``[(low, high)]`` for each abbreviated figure ``text`` writes: the
    values the abbreviation stands for at its own precision (``8.3M`` is
    ``8,250,000 <= v < 8,350,000``). A lone ``m`` or ``b`` may be another
    unit; reading it as a magnitude only allows more figures. ``None`` past
    ``limit``, so the caller names nothing rather than miss one."""
    if not isinstance(text, str):
        return []
    out = []
    for match in _MAGNITUDE.finditer(text if max_chars is None else text[:max_chars]):
        whole, fraction, word = match.group(1), match.group(2) or "", match.group(3)
        step = Decimal(_MULTIPLIER[word.lower()]).scaleb(-len(fraction))
        value = Decimal(whole.replace(",", "") + (f".{fraction}" if fraction else "")) * \
            _MULTIPLIER[word.lower()]
        out.append((value - step / 2, value + step / 2))
        if len(out) > limit:
            return None
    return out


def _in_ranges(figure: Decimal, ranges) -> bool:
    return any(low <= figure < high for low, high in ranges or ())


def _exact_difference(figure: Decimal, value: Decimal) -> Decimal | None:
    """``figure - value`` when it is exact (:data:`_EXACT`), else ``None``. At
    default precision a figure minus a value of hundreds of digits rounds to
    the negated value, which the symmetric pair set holds, so every figure
    would pass."""
    try:
        return _EXACT.subtract(figure, value)
    except (Inexact, Overflow, InvalidOperation):
        return None


def _held(figure: Decimal, single: set, pairs: set, rough: list, own=frozenset(),
          cells=frozenset()) -> bool:
    """``figure`` is in ``single`` (a non-integer one when it rounds to it),
    or a sum or difference of two ``pairs`` values (subtotals are never
    paired: they are dense enough to account for almost any figure). The
    pair test holds a figure only through an exact difference. Two ``own``
    values pair only as a sum of two ``cells`` (:func:`_own_checks`)."""
    if figure in single or any(abs(v - figure) * 2 <= 1 for v in rough):
        return True
    for value in pairs:
        difference = _exact_difference(figure, value)
        if difference is None or difference not in pairs:
            continue
        if value in own and difference in own and not (value in cells and difference in cells):
            continue
        return True
    return False


def _figure_held(figure: Decimal, spelling: str, sums, checks: dict) -> bool:
    """``figure`` is accounted for: asked, in the question's abbreviated range,
    no larger than the rows returned, one of its listed sums, a held value or
    sum/difference, or a date this call holds. A table count cell is checked by
    exactly this test, like a labelled figure."""
    return (figure in checks["asked"] or _in_ranges(figure, checks["ranges"])
            or figure <= checks["rows"] or figure in sums
            or _held(figure, checks["single"], checks["pairs"], checks["rough"],
                     checks.get("own", frozenset()), checks.get("cells", frozenset()))
            or (_date_shaped(spelling)
                and (checks["dates_cut"] or date_held(figure, checks["moments"]))))


def _figure_checks(values: dict, totals, question: str, statements=None) -> dict | None:
    """The held-value sets and date moments :func:`_figure_held` needs, or
    ``None`` when the question's abbreviations overflow. The cross-result vouch
    sums (:func:`cross_result_vouch_sums`) join the held set, so a correct
    per-key figure summed across proven-disjoint results is not named."""
    from .answer_provenance import numbers_in
    ranges = question_ranges(question or "")
    if ranges is None:
        return None
    moments, dates_cut = date_moments(question or "", list(values.get("dates") or ()))
    data = set(values["data"]) | set(totals or ())
    vouch = cross_result_vouch_sums(statements) if statements is not None else set()
    #: The lower-bound floor is built ONLY from typed additive engine totals
    #: (``totals`` = each complete grouped result's COUNT/SUM measure-column sum,
    #: from ``engine_totals``) and the cross-result vouch sums -- NEVER raw
    #: result cells (which include numeric grouping KEYS like a year) nor a
    #: call-wide max of ``data``/``derived``. A lower bound is held when it is at
    #: most the MAX of these typed totals. Keeping the max is deliberate: when
    #: several results/measure totals could apply the floor is ambiguous, and an
    #: ambiguous floor must cause a MISS (hold, stay silent), never a false
    #: alarm. A call with no engine total has floor 0, so a bare lower bound
    #: over an ungrouped/cut call is not held by the floor (other support tests
    #: still apply). The adjacent listed sum is folded in per figure in
    #: :func:`unfound_figures`, not here.
    bound_floor = set(totals or ()) | vouch
    return {
        "asked": {value for _s, value, _p in numbers_in(question or "")},
        "ranges": ranges, "moments": moments,
        "dates_cut": dates_cut or bool(values.get("dates_cut")),
        "rows": values.get("rows", 0), "pairs": data | {v.copy_negate() for v in data},
        "single": data | set(values["derived"]) | set(values["raw"]) | vouch,
        "rough": [v for v in data | set(values["derived"]) if v != v.to_integral_value()],
        "floor": max(bound_floor) if bound_floor else Decimal(0),
        #: The engine totals, the only values besides a tied result's own cells
        #: that hold a figure written beside that result's rows, alone or in a
        #: pair (:func:`_own_checks`). Not the vouch sums: one key's sum across
        #: results plus one of the tied result's cells is no total of it.
        "exact": set(totals or ()),
        #: A total two results share, twice: both together (:func:`_own_checks`).
        "exact_twice": {total + total for total, count in Counter(totals or ()).items()
                        if count >= 2},
    }


def unfound_figures(answer, values: dict | None, totals, question: str = "",
                    statements=None, *, read_counts: bool = True) -> list[tuple]:
    """``(spelling, listed sums)`` for each stated figure nothing in this call
    accounts for (module docstring); ``[]`` when the record is missing or
    unread, or no result was read. In a call with a cut result only prose
    totals are checked (:func:`_unfound_past_cut`). A table count cell is one
    such stated figure with no key attributed, checked exactly like a labelled
    figure; a correct per-key figure summed across proven-disjoint results is
    held by :func:`cross_result_vouch_sums` (through ``statements``). A prose
    total written beside a complete result's rows is checked as that result's
    total (:func:`_tie_reader`).

    ``read_counts`` adds the count shapes a system agent writes without a total
    word (:func:`_count_shapes`), checked by the same support tests as a
    labelled total; the in-app check reads them. The evaluator passes
    ``read_counts=False`` and reads those shapes against its own value view
    (:func:`unfound_count_shapes`), so its behaviour is unchanged."""
    return [(spelling, sums) for spelling, sums, _tied in
            _unfound(answer, values, totals, question, statements, read_counts)]


def _unfound(answer, values, totals, question: str, statements,
             read_counts: bool = True) -> list[tuple]:
    """:func:`_unfound_in_order`, with the figures the labelled, phrased and
    table readers (:func:`_stated`) name ahead of the count shapes, so the
    count shapes never push one of those out of the figures the engine line
    and the correction contract show by name."""
    out = _unfound_in_order(answer, values, totals, question, statements, read_counts)
    if not read_counts or len(out) < 2:
        return out
    old = {spelling for spelling, _i in _stated(_lines(answer))}
    return sorted(out, key=lambda row: row[0] not in old)


def _unfound_in_order(answer, values, totals, question: str, statements,
                      read_counts: bool = True) -> list[tuple]:
    """``(spelling, listed sums, beside)`` for :func:`unfound_figures`;
    ``beside`` (:func:`_beside`) names the results a figure is written beside
    when only that tie names it (:func:`_tie_reader`): the checks over every
    returned value would have held it."""
    if not values or values.get("unread") or not values.get("results"):
        return []
    if values.get("cut"):
        return _unfound_past_cut(answer, values, totals, question, statements, read_counts)
    checks = _figure_checks(values, totals, question, statements)
    if checks is None:
        return []
    lines = _lines(answer)
    bounds = _lower_bound_spellings(lines)
    tie = _tie_reader(lines, statements)
    out, cache = [], {}
    for spelling, indexes in (_stated_counts(lines) if read_counts else _stated(lines)):
        figure = Decimal(spelling.replace(",", ""))
        tied = tie(spelling, indexes)
        owns = _tied_checks(checks, figure, tied)
        if _held_as_stated(figure, spelling, checks, owns):
            continue
        sums, whole = _occurrence_sums(lines, indexes, cache, figure)
        if not whole or figure in sums:
            continue
        if spelling in bounds and (figure <= checks["floor"]
                                   or any(figure <= s for s in sums)):
            # A lower bound is held when it is at most a typed additive engine
            # total (the floor) OR at most an adjacent listed sum -- a true
            # bound of a returned/listed set. An ambiguous floor (several
            # applicable totals) keeps the max, so the bound is held, a
            # deliberate miss rather than a false alarm.
            continue
        out.append((spelling, sums, figure, _beside(
            tied, owns, owns is not None and _figure_held(figure, spelling, (), checks))))
    return _without_context_numbers(out, values)


def _tie_reader(lines: list[str], statements):
    """``(spelling, indexes) -> tied`` for one answer: the result columns
    (:func:`_tied_totals`) whose rows a prose total (:func:`_cut_candidates`:
    never a table count cell, a lower bound or a figure scoped to part of a
    result; nor a running, partial or filtered one, :func:`_part_spellings`)
    is written beside, so it is stated as their total and checked with
    :func:`_tied_checks`; ``[]`` or ``None`` for any other figure."""
    columns = _total_columns(statements)
    prose = _cut_candidates(lines) - _part_spellings(lines) if columns else set()
    cache: dict = {}

    def read(spelling: str, indexes) -> list | None:
        if spelling not in prose:
            return []
        return _tied_totals(lines, indexes, columns, cache)
    return read


def _without_context_numbers(out: list, values: dict) -> list[tuple]:
    """``out`` as ``(spelling, listed sums, beside)``, without the figures the
    inner model's conversation wrote (:func:`_drop_context_numbers`); ``[]``
    when the conversation cannot be read whole."""
    if out and values.get("context") is not None:
        out = _drop_context_numbers(out, values)
        if out is None:
            return []
    return [(spelling, sums, beside) for spelling, sums, _figure, beside in out]


def _unfound_past_cut(answer, values: dict, totals, question: str, statements,
                      read_counts: bool = True) -> list[tuple]:
    """``(spelling, listed sums, beside)`` for each prose total
    (:func:`_cut_candidates`) in a call with a cut result that the call cannot
    account for: none of its COMPLETE results supports it (the support tests
    of :func:`unfound_figures`, over the ``complete`` view) and every cut
    result rules it out (:func:`_past_cuts`). ``[]`` when a cut result or the
    complete view could not be read."""
    cuts, complete = values.get("cuts"), values.get("complete")
    if not (isinstance(cuts, list) and cuts and all(isinstance(c, dict) for c in cuts)
            and isinstance(complete, dict)
            and all(isinstance(complete.get(k), set) for k in ("raw", "data", "derived"))):
        return []
    view = {**values, "raw": complete["raw"], "data": complete["data"],
            "derived": complete["derived"]}
    checks = _figure_checks(view, totals, question, statements)
    if checks is None:
        return []
    lines = _lines(answer)
    candidates = _cut_candidates(lines)
    # Count shapes past a cut are checked against the complete results too, as
    # the evaluator does, dropping any scoped to part of a result.
    count_shapes = ({s for s, _i in _count_shapes(lines, {s for s, _i in _stated(lines)},
                                                  cells=False)}
                    - _count_shape_scoped(lines)) if read_counts else set()
    reader = _stated_counts if read_counts else _stated
    tie = _tie_reader(lines, statements)
    out, cache = [], {}
    for spelling, indexes in reader(lines):
        if spelling not in candidates and spelling not in count_shapes:
            continue
        figure = Decimal(spelling.replace(",", ""))
        tied = tie(spelling, indexes)
        owns = _tied_checks(checks, figure, tied)
        if _held_as_stated(figure, spelling, checks, owns) or not _past_cuts(figure, cuts):
            continue
        sums, whole = _occurrence_sums(lines, indexes, cache, figure)
        if not whole or figure in sums:
            continue
        out.append((spelling, sums, figure, _beside(
            tied, owns, owns is not None and _figure_held(figure, spelling, (), checks))))
    return _without_context_numbers(out, values)


def _past_cuts(figure: Decimal, cuts: list) -> bool:
    """``figure`` is no cut result's total: strictly below every floor of every
    cut result (its true total is at least each), and none of the figures it
    could stand for (:func:`_cut_summary`)."""
    for cut in cuts:
        floors, held = cut.get("floors"), cut.get("held")
        if not (isinstance(floors, list) and floors and isinstance(held, set)):
            return False
        if figure in held or any(figure >= floor for floor in floors):
            return False
    return True


def _cut_candidates(lines: list[str]) -> set:
    """The spellings stated as a prose total (a labelled or phrased total, or a
    prose total or sum shape), never as a lower bound or a table count cell,
    and never written in a scoped segment (:func:`_scoped_spellings`)."""
    read: set = set()
    for line in lines:
        for segment in line.split("|"):
            read.update(_labelled_spellings(segment))
            read.update(spelling for spelling, bound in _total_phrase_figures(segment)
                        if not bound)
    table = {spelling for spelling, _index in _table_figures(lines)}
    return read - table - _lower_bound_spellings(lines) - _scoped_spellings(lines)


def _scoped_spellings(lines: list[str]) -> set:
    """Every number written in a segment that scopes a total to part of a
    result: one with a subset word anywhere in it (:data:`_SUBSET_SCOPE`,
    :data:`_PICKED_SCOPE`), a number with a ranking word (:data:`_RANK_SCOPE`)
    before it or before the count noun that follows it, and a number written
    with ``+``."""
    noun = re.compile(rf"\b{_count_noun_alt()}", re.I)
    out: set = set()
    for line in lines:
        for segment in line.split("|"):
            plain = re.sub(r"[_\W]+", " ", segment)
            subset = bool(_SUBSET_SCOPE.search(plain) or _PICKED_SCOPE.search(plain))
            for match in _ANY_NUMBER.finditer(segment):
                follow = noun.search(segment, match.end())
                end = follow.end() if follow else match.end()
                if (subset or _RANK_SCOPE.search(segment[:end])
                        or segment[match.end():match.end() + 1] == "+"):
                    out.add(match.group(1))
    return out


#: Words that make a prose total a running, partial or filtered one ("running
#: total after the fourth row", "sub-total", "with more than 5 each", "four
#: codes"): a correct total of some of a result's rows, so it is never stated
#: as the result's total (:func:`_part_spellings`). Read for that only, and
#: only in the figure's own clause.
_PART_SCOPE = re.compile(
    r"\b(?:running|cumulative(?:ly)?|accumulated|partial(?:ly)?|sub|subtotals?|so far|to date"
    r"|coded|uncoded"
    r"|through|thru|after|before|until|till|up to|remaining|remainder|rest|others?|besides"
    r"|without|minus|plus|less|more|fewer|over|under|beyond|where|which|whose"
    r"|that|with|having|when|while|if|unless|not|non|filtered|matching|these"
    r"|those|each|per|rows?|ranks?|ranked|second|third|fourth|fifth|sixth|seventh|eighth"
    r"|ninth|tenth|eleventh|twelfth)\b"
    r"|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve) [a-z]", re.I)
#: Words that make a total partial only in its label, before the figure ("the
#: codes shown above: 80"); after it they say where its rows are ("118 (shown
#: below)").
_PART_LABEL = re.compile(r"\b(?:listed|shown|above|below)\b", re.I)
#: The end of a sentence before a figure, and the end of the figure's clause
#: after it (a comma, semicolon, colon, sentence end or spaced dash).
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)")
_CLAUSE_END = re.compile(r"[,;:!?]|\.(?=\s|$)|\s[-\u2013\u2014]+\s")
#: A clause end after a figure that a qualifier may continue past: a comma or
#: a spaced dash ("80, after the fourth row"; "80 - through row four").
_CONTINUES = re.compile(r",|\s[-\u2013\u2014]+\s")
#: A running or partial word that, opening the clause right after a comma or
#: dash, still scopes the figure before it ("80, after the fourth row shown
#: below"). Presentational openers ("with") are not here, so "118, with the full
#: breakdown shown below" stays a full total; "only" is not here either (a
#: segment holding it is already scoped by :func:`_scoped_spellings`).
_PART_NEXT = re.compile(
    r"\s*(?:running|cumulative(?:ly)?|accumulated|partial(?:ly)?|sub ?totals?|so far|to date"
    r"|through|thru|after|before|until|till|up to|excluding|minus|less|filtered|matching"
    r"|not counting)\b", re.I)


def _part_spellings(lines: list[str]) -> set:
    """Every number whose own clause has a running, partial or filtered word
    (:data:`_PART_SCOPE`): its label, from the start of its sentence, or its
    clause after it, up to :data:`_CLAUSE_END`; or a :data:`_PART_LABEL` word
    in its label; or, past a comma or spaced dash, a next clause that opens
    with a :data:`_PART_NEXT` word ("80, after the fourth row"). So a clause
    past a comma that only presents the listing ("118, with the full
    breakdown shown below") leaves it a full total."""
    out: set = set()
    for line in lines:
        for segment in line.split("|"):
            numbers = list(_ANY_NUMBER.finditer(segment))
            if not numbers:
                continue
            starts = [end.end() for end in _SENTENCE_END.finditer(segment)]
            ends = list(_CLAUSE_END.finditer(segment))
            stops = [end.start() for end in ends]
            for match in numbers:
                before = bisect_right(starts, match.start())
                start = starts[before - 1] if before else 0
                after = bisect_left(stops, match.end())
                stop = stops[after] if after < len(stops) else len(segment)
                label = re.sub(r"[_\W]+", " ", segment[start:match.start()])
                rest = re.sub(r"[_\W]+", " ", segment[match.end():stop])
                continued = (after < len(ends) and _CONTINUES.fullmatch(ends[after].group())
                             and _PART_NEXT.match(segment, ends[after].end()))
                if (_PART_SCOPE.search(label) or _PART_LABEL.search(label)
                        or _PART_SCOPE.search(rest) or continued):
                    out.add(match.group(1))
    return out


def _drop_context_numbers(out: list, values: dict) -> list | None:
    """Drop figures the inner model's conversation wrote (so one it quoted
    never vouches for itself), ``None`` when the conversation cannot be read
    whole. Date-shaped figures also drop when a conversation date holds them."""
    numbers = _context_numbers(values)
    if numbers is None:
        return None
    out = [item for item in out if item[2] not in numbers]
    if any(_date_shaped(item[0]) for item in out):
        held_moments, cut = _context_moments(values)
        out = [item for item in out if not _date_shaped(item[0])
               or not (cut or date_held(item[2], held_moments))]
    return out


# --------------------------------------------------------------------------- #
# 4. The engine lines
# --------------------------------------------------------------------------- #
def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _listed_sentence(found: list[tuple]) -> str:
    """What each named figure's listed figures add up to, when one sum."""
    parts = [(spelling, sums[0]) for spelling, sums in found[:MAX_FIGURES_SHOWN]
             if len(sums) == 1]
    if not parts:
        return ""
    if len(found) == 1:
        return f" The figures this answer lists next to it add up to {parts[0][1]:,}."
    listed = "; ".join(f"next to {spelling}, {total:,}" for spelling, total in parts)
    return f" The figures this answer lists add up to: {listed}."


def _figure_head(figures: list[str], count: int, tied=None) -> str:
    """The figure line's opening sentence naming the first ``count`` of
    ``figures`` (the rest counted as ``and N more``); for ``count`` 0 only
    how many there are. ``tied`` maps a figure named only because it is
    written beside a result's rows (:func:`_beside`) to the ``(query ordinal,
    total)`` of each such result: another result may hold its value, so the
    line then opens with :data:`TIED_PREFIX`, and such a figure gets its own
    sentence (:func:`_beside_sentence`) instead of the one saying no result
    holds it."""
    tied = tied or {}
    if not any(figure in tied for figure in figures):
        return FIGURE_PREFIX + _loose_sentence(figures[:count], len(figures))
    shown = figures[:count]
    loose = [figure for figure in figures if figure not in tied]
    beside = [figure for figure in shown if figure in tied]
    parts = [_loose_sentence([f for f in shown if f not in tied], len(loose))] if loose else []
    parts += [_beside_sentence(figure, tied[figure]) for figure in beside]
    more = len(figures) - len(loose) - len(beside)
    if more:
        one = more == 1
        noun = f"{more:,} {'more ' if beside else ''}figure{'' if one else 's'}"
        parts.append(f"{noun} in this answer {'is' if one else 'are'} written as "
                     f"{'a total' if one else 'totals'} beside rows a result returns, and "
                     f"{'it is not' if one else 'none is'} that result's engine total.")
    return TIED_PREFIX + " ".join(parts)


def _loose_sentence(shown: list[str], total: int) -> str:
    """The figure line's sentence for figures no result holds: ``shown`` of
    ``total`` named, the rest counted."""
    more = total - len(shown)
    one = total == 1
    named = (_listed(shown) + (f" and {more} more" if more else "") if shown
             else f"{total:,} figure{'' if one else 's'}")
    return (f"{named} in this answer {'is' if one else 'are'} stated as "
            f"{'a total or count' if one else 'totals or counts'}, but none of this call's "
            f"results holds {'it' if one else 'them'}, and {'it is not an' if one else 'none is an'}"
            f" engine total or a sum or difference of two returned values.")


def _beside_sentence(figure: str, results) -> str:
    """``figure`` is written as a total beside rows the queries in
    ``results`` (``(query ordinal, total)``, :func:`_beside`) return, and
    their engine totals."""
    ordinals = list(dict.fromkeys(ordinal for ordinal, _total in results))
    queries = _listed([f"Q{ordinal}" for ordinal in ordinals])
    returns = "returns" if len(ordinals) == 1 else "return"
    if len(results) == 1:
        totals = f"{queries}'s engine total is {results[0][1]:,}"
    elif len(ordinals) == 1:
        totals = f"{queries}'s engine totals are {_listed([f'{t:,}' for _o, t in results])}"
    else:
        totals = "their engine totals are " + _listed(
            [f"{total:,} (Q{ordinal})" for ordinal, total in results])
    return (f"{figure} in this answer is written as a total beside rows {queries} {returns}, "
            f"and {totals}.")


def figure_line(found: list[tuple], statements, tied=None) -> str:
    """The engine line naming stated figures no result holds, ``""`` for none;
    never longer than the executed-filters line may be
    (``executed_filters.MAX_LINE_CHARS``). Past it, the listed-sum sentence
    goes first, then the totals give way as on the totals line
    (:func:`_fitted_entries`: their labels, then whole entries); past that,
    the last figures are counted instead of named. A named figure is never
    cut, and totals that exist are never said to be missing
    (:data:`_TOTALS_CUT` when no whole total fits). ``tied`` maps the
    figures named as written beside a result's rows to those results
    (:func:`_figure_head`)."""
    if not found:
        return ""
    from .executed_filters import MAX_LINE_CHARS
    figures = [spelling for spelling, _sums in found]
    lead = " Engine totals, in this turn's queries numbered as above: "

    def assembled(head: str, budget: int) -> str:
        entries, withheld = _totals_text(statements, budget)
        if not entries:
            return f"{head} The engine computed no total from this call's results."
        return "" if entries == _LINE_CUT else f"{head}{lead}{entries}.{withheld}"

    count = min(len(figures), MAX_FIGURES_SHOWN)
    whole = assembled(_figure_head(figures, count, tied) + _listed_sentence(found), 10 ** 9)
    if whole and len(whole) <= MAX_LINE_CHARS:
        return whole
    for count in range(count, -1, -1):
        head = _figure_head(figures, count, tied)
        line = assembled(head, MAX_LINE_CHARS - len(head) - len(lead) - 1)
        if line and len(line) <= MAX_LINE_CHARS:
            return line
    return f"{head}{_TOTALS_CUT}" if _ordered_totals(statements) else line


def requirement_block(figures: list[str], has_totals: bool, tied=None) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for :func:`figure_line`; a figure
    in ``tied`` (:func:`_figure_head`) gets its own requirement, which does
    not say no result holds it."""
    if not figures:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    tied = tied or {}
    loose = [figure for figure in figures if figure not in tied]
    beside = [figure for figure in figures if figure in tied][:MAX_FIGURES_SHOWN]
    document = empty_document()
    if loose:
        shown = loose[:MAX_FIGURES_SHOWN]
        one = len(loose) == 1
        instead = ("Use a figure its results give or an engine total instead." if has_totals
                   else "Use a figure its results give instead, or say that total was not "
                        "computed.")
        note = (f"REQUIRED: the answer must not state {_listed(shown)} as data from this "
                f"system, because none of its results this turn holds "
                f"{'it' if one else 'them'}. {instead}")
        document = append_note(document, note, GuidanceScope.ANSWER_REQUIREMENT)
    if beside:
        one = len(beside) == 1
        note = (f"REQUIRED: the answer must not state {_listed(beside)} as the total of the "
                f"result rows {'it is' if one else 'they are'} written beside, because "
                f"{'it is not' if one else 'none is'} that result's engine total. Use that "
                f"result's engine total instead.")
        document = append_note(document, note, GuidanceScope.ANSWER_REQUIREMENT)
    return render_orchestrator_sections(document)["answer_requirements"]


def _statement_counts(lines: list[str]) -> Counter:
    """How many times the answer states each figure as a total or count, by
    the same readers as :func:`_stated`: every distinct place in a line
    segment that a labelled or phrased reading reads (two sentences on one
    line are two; one statement both readers read is one, since both read the
    same digits) and every table count cell."""
    counts: Counter = Counter()
    for line in lines:
        for segment in line.split("|"):
            places = {(spelling, span) for spelling, span in _labelled_matches(segment)}
            places |= {(spelling, span) for spelling, _b, span in _total_phrase_matches(segment)}
            counts.update(spelling for spelling, _span in places)
    for spelling, _index in _table_figures(lines):
        counts[spelling] += 1
    return counts


def _shown_first(found: list[tuple], answer) -> list[tuple]:
    """``found`` with each figure the answer states more than once moved to the
    front, in order, and the rest after it in order. Past
    :data:`MAX_FIGURES_SHOWN` the line names a few figures and counts the rest;
    a figure written twice (a table cell the answer then quotes in its prose)
    is the one most likely to be carried into the final answer, so it is the
    one shown and required. A statement is a place the figure reader reads
    the number as a total or count (:func:`_statement_counts`), so an
    identifier column, a date or a time holding the same digits is not one.
    Only the order changes, never which are named."""
    if not isinstance(answer, str) or len(found) < 2:
        return found
    statements = _statement_counts(_lines(answer))
    repeated = [item for item in found if statements[item[0]] > 1]
    if not repeated:
        return found
    return repeated + [item for item in found if item not in repeated]


def engine_lines(statements, values: dict | None, answer, question: str = "") -> list[str]:
    """This call's figure lines: the figure line and its requirement block when
    the answer states a total or count nothing accounts for, else the totals
    line (or nothing)."""
    found = _unfound(answer, values, engine_totals(statements), question, statements)
    if found:
        tied = {spelling: beside for spelling, _sums, beside in found if beside}
        found = _shown_first([(spelling, sums) for spelling, sums, _beside in found], answer)
        return [figure_line(found, statements, tied),
                requirement_block([s for s, _sums in found], bool(_ordered_totals(statements)),
                                  tied)]
    line = totals_line(statements)
    return [line] if line else []


def neutralize(text):
    """``text`` with a copy of any of the lines' prefixes made inert, so a
    model-written or data-carried line never reads as the engine's."""
    if not isinstance(text, str):
        return text
    for prefix in (TOTALS_PREFIX, FIGURE_PREFIX, TIED_PREFIX):
        if prefix in text:
            text = text.replace(prefix, prefix[len("[engine] "):])
    return text
