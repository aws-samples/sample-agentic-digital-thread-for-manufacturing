# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounded query-result helpers shared by direct connector paths."""
from __future__ import annotations

from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
import json
import logging
import math
import re
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

MAX_QUERY_COLUMNS = 128
MAX_QUERY_CELL_BYTES = 16_384
MAX_QUERY_RESULT_BYTES = 200_000
MAX_QUERY_COLUMN_NAME_BYTES = 256


def _json_cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite numeric cell")
        return format(value, "f")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numeric cell")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def strict_json_dumps(value: Any) -> str:
    """Serialize deterministically and reject every NaN/Infinity spelling."""
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )


def _encoded_size(value: Any) -> int:
    return len(strict_json_dumps(value).encode("utf-8"))


def _bounded_columns(columns: Iterable[Any]) -> tuple[list[str], bool]:
    raw_columns = list(columns)
    output: list[str] = []
    truncated = len(raw_columns) > MAX_QUERY_COLUMNS
    for raw in raw_columns[:MAX_QUERY_COLUMNS]:
        name = str(raw)
        if len(name.encode("utf-8", "replace")) > MAX_QUERY_COLUMN_NAME_BYTES:
            truncated = True
            break
        output.append(name)
    return output, truncated


PARTIAL_NOTE_KEY = "partial_note"

# What each partial reason cut, for the plain-language note. ``column_limit``
# alone keeps every row and drops columns instead (cut ""). ``page_limit`` is
# one full page of a paginated source; ``response_limit`` is a response body
# or text cut by bytes (no row semantics); ``source_partial`` is an upstream
# tool's own partial flag whose meaning the engine does not know.
_CUT_KIND = {
    "row_limit": "rows", "byte_limit": "rows", "cell_limit": "rows",
    "invalid_row": "rows", "invalid_numeric": "rows",
    "page_limit": "page", "response_limit": "response",
    "source_partial": "source", "column_limit": "",
}
_ROW_CUT_REASONS = frozenset(
    reason for reason, cut in _CUT_KIND.items() if cut in {"rows", "page"})

_INCOMPLETE_FROM_ROWS = (
    "A list of distinct values, a count, or an earliest or latest value read "
    "off these rows is not the full result.")
_INCOMPLETE_FROM_CONTENT = (
    "A list, a count, or an earliest or latest value read off it may be "
    "incomplete.")

#: Public spellings for the Mode A engine line that carries a row cut across
#: the query boundary (``tool_result_capture.result_cut_line``): the same
#: reasons count as a row/page cut and the same sentence says what that means,
#: so the note inside the tool result and the line outside it cannot drift.
ROW_CUT_REASONS = _ROW_CUT_REASONS
INCOMPLETE_FROM_ROWS = _INCOMPLETE_FROM_ROWS


def partial_note(row_count: int, column_count: int, *, cut: str,
                 columns_cut: bool = False) -> str:
    """Plain-language statement of what a partial result is missing.

    Rides beside the machine-readable ``truncated``/``partial_reason`` keys so
    the model reading the tool result is told, in words, what was left out.
    ``cut`` is ``rows``, ``page``, ``response``, ``source`` or ``""`` (only
    columns were capped). Generic: names no dataset.
    """
    parts: list[str] = []
    if cut == "page":
        parts.append(
            f"This is one full page of {row_count} records, so more records may "
            "exist after it; request the next page (a higher offset) until a "
            "page comes back short.")
    elif cut == "rows":
        parts.append(
            f"Only the first {row_count} rows are included; every row after "
            "them was not returned. The rows kept are the first ones in the "
            "query's ORDER BY, and in no defined order without one.")
    elif cut == "response" and row_count == 0:
        parts.append(
            "The response was larger than the limit and none of it could be "
            "included, so the content is missing.")
    elif cut == "response":
        parts.append(
            "Only the first part of the response was returned; the rest was "
            "cut off, so the content may be incomplete.")
    elif cut == "source":
        parts.append(
            "The source marked this result as partial, so it may not include "
            "everything that matched.")
    if columns_cut:
        parts.append(
            f"Only the first {column_count} columns are included; the other "
            "columns were omitted.")
        if not cut:
            parts.append("Every row is included.")
    if cut in {"rows", "page"}:
        parts.append(_INCOMPLETE_FROM_ROWS)
    elif cut:
        parts.append(_INCOMPLETE_FROM_CONTENT)
    return " ".join(parts)


def mark_rows_cut(result: dict) -> dict:
    """Return *result* marked as a row cut, keeping any column-cap fact.

    For a caller that learns AFTER shaping that rows may be missing (an
    engine-injected LIMIT exactly reached). A note that already describes a
    row cut is kept; a columns-only or absent note is regenerated so it names
    the row cut and, when the columns were capped too, both.
    """
    reason = result.get("partial_reason") or ""
    out = {**result, "truncated": True, "partial_reason": reason or "row_limit"}
    if _CUT_KIND.get(reason) == "rows" and PARTIAL_NOTE_KEY in result:
        return out
    out[PARTIAL_NOTE_KEY] = partial_note(
        int(result.get("row_count") or 0), len(result.get("columns") or []),
        cut="rows", columns_cut=reason == "column_limit")
    return out


class BoundedQueryAccumulator:
    """Incrementally retain only a strictly serializable bounded row prefix."""

    def __init__(self, columns: Iterable[Any]):
        self.columns, limited = _bounded_columns(columns)
        self.columns_limited = limited
        self.rows: list[list[Any]] = []
        self.partial_reason = "column_limit" if limited else ""

    def add(self, raw_row: Any) -> bool:
        if self.partial_reason and self.partial_reason != "column_limit":
            return False
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) < len(self.columns):
            self.partial_reason = self.partial_reason or "invalid_row"
            return False
        row: list[Any] = []
        try:
            for raw_cell in raw_row[:len(self.columns)]:
                cell = _json_cell(raw_cell)
                if _encoded_size(cell) > MAX_QUERY_CELL_BYTES:
                    self.partial_reason = self.partial_reason or "cell_limit"
                    return False
                row.append(cell)
            candidate = self._shape(self.rows + [row], True, "byte_limit")
            if _encoded_size(candidate) > MAX_QUERY_RESULT_BYTES:
                self.partial_reason = self.partial_reason or "byte_limit"
                return False
        except (TypeError, ValueError, OverflowError):
            self.partial_reason = self.partial_reason or "invalid_numeric"
            return False
        self.rows.append(row)
        return True

    def _shape(self, rows: list[list[Any]], partial: bool, reason: str,
               cut: str = "rows") -> dict:
        result = {"columns": self.columns, "rows": rows, "row_count": len(rows)}
        if partial:
            reason = reason or "row_limit"
            result.update(truncated=True, partial_reason=reason)
            result[PARTIAL_NOTE_KEY] = partial_note(
                len(rows), len(self.columns), cut=cut,
                columns_cut=self.columns_limited)
        return result

    def result(self, source_truncated: bool = False, source_reason: str = "") -> dict:
        own = self.partial_reason
        partial = bool(source_truncated or own)
        reason = own or ((source_reason or "row_limit") if source_truncated else "")
        # What was cut, for the note. A response cut by bytes has no rows to
        # speak of, so it stays a response cut even when its one oversized
        # cell was then dropped; otherwise the accumulator's own row cut wins,
        # then the source's reason (a column cap alone keeps every row).
        if source_truncated and source_reason == "response_limit":
            cut, reason = "response", "response_limit"
        elif own and own != "column_limit":
            cut = "rows"
        elif source_truncated:
            cut = _CUT_KIND.get(source_reason or "row_limit", "rows") or "rows"
        else:
            cut = ""
        result = self._shape(self.rows, partial, reason, cut)
        while self.rows and _encoded_size(result) > MAX_QUERY_RESULT_BYTES:
            self.rows.pop()
            self.partial_reason = "byte_limit"
            result = self._shape(self.rows, True, self.partial_reason)
        if _encoded_size(result) > MAX_QUERY_RESULT_BYTES:
            raise ValueError("query result metadata exceeds byte limit")
        return result


def bounded_query_result(columns: list[str], rows: Iterable[Any], truncated: bool,
                         reason: str = "") -> dict:
    """Return a finite-JSON row prefix bounded before each row is accumulated.

    ``reason`` names WHY the source marked the result truncated (default
    ``row_limit``): ``page_limit`` is one full page of a paginated source,
    ``response_limit`` a body/text cut by bytes, ``source_partial`` an
    upstream tool's own partial flag. A cut found while accumulating
    (cell/byte limits) wins over it.
    """
    accumulator = BoundedQueryAccumulator(columns)
    source_truncated = bool(truncated)
    source_reason = reason if source_truncated else ""
    for row in rows:
        if not accumulator.add(row):
            source_truncated = True
            if source_reason != "response_limit":
                source_reason = ""
            break
    return accumulator.result(source_truncated, source_reason)


#: A cut SQL result gets a repeat note when the query has a JOIN, at least
#: ``REPEAT_MIN_ROWS`` rows were kept, and at least half of them are exact
#: copies of another kept row. Measured on the rows kept only; nothing is read
#: from the source. Rehearsal, 2026-10-14: a shipment-line query joined a line
#: table on a shared product key came back cut as 500 identical rows, and the
#: GROUP BY re-run the truncated-results rule asks for kept the same JOIN, so
#: its totals came back multiplied. A low count of distinct ids is NOT a
#: repeat (500 distinct line items under 50 orders is a correct
#: one-to-many result), so the note rests on whole-row copies, which a result
#: listing distinct records never has, and says that repeats can be correct.
REPEAT_NOTE_KEY = "repeat_note"
REPEATED_ROWS_KEY = "repeated_rows"
REPEAT_MIN_ROWS = 50


def _sql_has_join(sql: str) -> bool:
    """Whether *sql* has a JOIN keyword outside strings and comments.
    Uncertain tokenization reads as no JOIN (no note)."""
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql or "")
    return not uncertain and any(
        t.kind == "word" and t.value.upper() == "JOIN" for t in tokens)


def repeated_rows(columns: list, rows: list, sql: str) -> dict | None:
    """``{"rows", "distinct_rows"}`` when at least half of *rows* are exact
    copies of another row in *rows*, else None.

    None unless *sql* has a JOIN and there are at least ``REPEAT_MIN_ROWS``
    rows, each a list or tuple of scalar cells. ``columns`` is not read: the
    whole row is compared, so a result that carries any column telling its
    records apart (each record's own key) is never a repeat. Pure.
    """
    if not _sql_has_join(sql) or len(rows) < REPEAT_MIN_ROWS:
        return None
    seen: set = set()
    for row in rows:
        if not isinstance(row, (list, tuple)):
            return None
        try:
            seen.add(tuple(row))
        except TypeError:
            return None
    distinct = len(seen)
    if distinct * 2 > len(rows):
        return None
    return {"rows": len(rows), "distinct_rows": distinct}


def repeat_note(fact: dict) -> str:
    """Plain-language note for a :func:`repeated_rows` fact, for the model
    reading the tool result. States what was measured and how to tell a
    correct repeat from a counting error; it claims neither."""
    rows, distinct = fact["rows"], fact["distinct_rows"]
    leaving = "row" if distinct == 1 else "rows"
    return (
        f"{rows - distinct} of the {rows} rows kept are exact copies of another row "
        f"kept, leaving {distinct} distinct {leaving}. That can be correct: a JOIN "
        "repeats a row of one table on each matching row of another, and rows also "
        "repeat when no selected column tells them apart. It matters when the rows "
        "a SUM or COUNT adds up should each appear once: then a total over the same "
        "JOINs counts some of them more than once, also in a separate GROUP BY "
        "query. To tell which, compare COUNT(*) with COUNT(DISTINCT the summed "
        "table's own key) under the same FROM, JOIN and WHERE.")


def with_repeat_note(result: dict, sql: str) -> dict:
    """*result* plus ``repeated_rows`` and ``repeat_note`` when it was cut by
    rows and :func:`repeated_rows` finds copies; otherwise *result* itself.
    An uncut result is never annotated: its rows are all there to count."""
    if not isinstance(result, dict) or result.get("truncated") is not True:
        return result
    if result.get("partial_reason") not in _ROW_CUT_REASONS:
        return result
    try:
        fact = repeated_rows(result.get("columns") or [], result.get("rows") or [], sql)
    except Exception:  # noqa: BLE001 - the note is an addition, never required
        return result
    if fact is None:
        return result
    return {**result, REPEATED_ROWS_KEY: fact, REPEAT_NOTE_KEY: repeat_note(fact)}


#: A SQL result with at least two rows whose query computes a select-list
#: item with COUNT(DISTINCT ...) under a GROUP BY carries a distinct note.
#: Rehearsal dry runs, 2026-10-22/23: such a result came back with a few
#: dozen grouped rows, and the system agent added rows up, or merged them
#: into a coarser grouping, in its own answer, once while the result at the
#: coarser grouping was already returned. Each row counts distinct values
#: inside its own group and one value can be counted in several groups, so
#: those sums were not distinct counts. When the grouping splits the values
#: (each in one group only) the rows do add up, and no passive reading of the
#: SQL can tell the two apart; an overlap-check query would cost an engine
#: query per grouped result under a time budget the measured answers already
#: spent. So the note asks the question the model can answer from what the
#: columns mean, whether one counted value can belong to several of the
#: returned groups, and gives its remedy only for "yes" or "cannot tell". It
#: carries no figure, keeps every row and refuses nothing. The column is read
#: from the parsed SQL (``aggregate_guard.grouped_distinct_counts``).
DISTINCT_NOTE_KEY = "distinct_note"
DISTINCT_NOTE_MIN_ROWS = 2
#: Past this many columns the note names none of them.
DISTINCT_NOTE_MAX_NAMES = 4
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def distinct_note(names: list[str]) -> str:
    """Plain-language note for a grouped result with a COUNT(DISTINCT ...)
    column. ``names`` are the columns it names (none: "a column"). States
    when the rows add up (each counted value in one group) and when they do
    not (one value under several grouping values), with the remedy for that
    case and for "cannot tell"; it carries no figure and withholds nothing.
    The EFFICIENCY sentence in ``system_agent._SQL_TOTALS_CLAUSE`` asks the
    same question."""
    subject = (f"{_listed(names)} {'is' if len(names) == 1 else 'are'}" if names
               else "a column is")
    return (
        f"In this result, {subject} computed with COUNT(DISTINCT ...) inside each row's "
        "own group. Whether these rows add up depends on whether one counted value can "
        "belong to several of the returned groups. When each counted value has only one "
        "value of the grouping columns (a property it has once, such as one category per "
        "item), it sits in one group and the rows add up. When one counted value can carry "
        "several values of the grouping columns (several codes, reasons or states per "
        "item), it is counted in each of those groups, so these figures do not add up "
        "across rows or merge into a coarser grouping: take a figure over several groups "
        "from its own query, or say it was not computed. When you cannot tell, do not add "
        "the rows: take that figure from its own query, or say it was not computed. Each "
        "row's figure stands as returned.")


def _distinct_names(columns: Any, items: tuple[int, ...], sql: str) -> list[str]:
    """The result's names for ``items`` when its columns line up with the
    select list and every name prints as a plain identifier, else ``[]``."""
    from .aggregate_guard import select_item_count
    if (not isinstance(columns, list) or len(items) > DISTINCT_NOTE_MAX_NAMES
            or select_item_count(sql) != len(columns)):
        return []
    names = [str(columns[i]) for i in items if i < len(columns)]
    if len(names) != len(items) or not all(_PLAIN_NAME.match(n) for n in names):
        return []
    return names


def with_distinct_note(result: dict, sql: str) -> dict:
    """*result* plus ``distinct_note`` when it has at least
    ``DISTINCT_NOTE_MIN_ROWS`` rows and *sql* is a grouped query with a
    COUNT(DISTINCT ...) select-list item; otherwise *result* itself. Cut
    results included: the rows kept are grouped the same way. Never raises."""
    if not isinstance(result, dict) or "error" in result:
        return result
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) < DISTINCT_NOTE_MIN_ROWS:
        return result
    try:
        from . import aggregate_guard
        items = aggregate_guard.grouped_distinct_counts(sql)
        if not items:
            return result
        names = _distinct_names(result.get("columns"), items, sql)
    except Exception:  # noqa: BLE001 - the note is an addition, never required
        return result
    logger.info("DISTINCT_NOTE rows=%d columns=%d named=%d", len(rows), len(items),
                len(names))
    return {**result, DISTINCT_NOTE_KEY: distinct_note(names)}


#: ``row_sums``: a grouped PostgreSQL JOIN result's plain SUM columns summed by
#: the engine over the rows that share each value of a repeated id column.
#: Workshop walk, 2026-11-02: a result grouped by (group, recipient id, code,
#: name) over three JOINs came back right and verified; the per-recipient
#: figures the agent added up from its rows were not (one took in another
#: recipient's row). So the regrouping comes from code:
#:
#: * only for a complete result whose aggregates the aggregate check verified
#:   (no row of the JOIN counted twice), one verified entry for each SUM call
#:   of the select list (the check keeps at most 16 entries), and whose
#:   statement is a grouped JOIN with no HAVING, LIMIT, OFFSET or FETCH
#:   (``aggregate_guard.plain_sum_shape``);
#: * only plain ``SUM(expr)`` columns, summed exactly (an int, or a decimal's
#:   text; a float is never summed). Each group's value is the exact SUM over
#:   its own base rows and the groups are disjoint, so a regrouped figure is
#:   exactly what the same query grouped by that id alone returns;
#: * only by a GROUP BY column whose every value is an integer or a canonical
#:   lowercase UUID and repeats across rows: equality there is the same in
#:   PostgreSQL and here, whatever the collation;
#: * a GROUP BY text column rides along as a label only when it maps one to
#:   one to the id in these rows and passes the canonical untrusted-text check;
#:   it is a JSON value under ``labels``, never part of any engine sentence.
#:
#: The caller adds the block last, and it is left out when the payload with it
#: would pass :data:`MAX_QUERY_RESULT_BYTES` (measured with the serializer
#: both PostgreSQL tools send, :func:`strict_json_dumps`) or would newly fail
#: the capture's own budget (``response_integrity_capture._budget_check``).
ROW_SUMS_KEY = "row_sums"
ROW_SUMS_MAX_BYTES = 8_192
ROW_SUMS_MAX_VALUES = 50
ROW_SUMS_MAX_LABEL_CHARS = 128
_DECIMAL_TEXT = re.compile(r"[+-]?\d{1,60}(?:\.\d{1,40})?")
_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Exact for any sum this block takes; a sum it could not hold exactly raises.
_EXACT_SUMS = Context(prec=200, traps=[Inexact, InvalidOperation, Overflow])


def _exact_cell(cell: Any) -> Decimal | None:
    """An exact number for a bounded result's cell (an int, or a decimal's
    text), ``None`` for NULL; raises for anything else (a float included)."""
    if cell is None:
        return None
    if isinstance(cell, int) and not isinstance(cell, bool):
        return Decimal(cell)
    if isinstance(cell, str) and _DECIMAL_TEXT.fullmatch(cell):
        return Decimal(cell)
    raise ValueError("not an exact number")


def _exact_sum(cells: list) -> Any:
    """The exact sum of ``cells`` as the result writes numbers (an int, or a
    decimal's text); ``None`` when every cell is NULL."""
    total, seen, integer = Decimal(0), False, True
    for cell in cells:
        value = _exact_cell(cell)
        if value is not None:
            total, seen = _EXACT_SUMS.add(total, value), True
            integer = integer and isinstance(cell, int)
    if not seen:
        return None
    return int(total) if integer else format(total, "f")


def _stable_id(value: Any) -> bool:
    return ((isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, str) and _UUID_TEXT.fullmatch(value) is not None))


def _data_text(text: Any) -> bool:
    """Text the block may repeat: passes the canonical untrusted-text check."""
    from .untrusted_text import sanitize_untrusted, validate_untrusted
    return (isinstance(text, str) and sanitize_untrusted(text) == text
            and validate_untrusted(text, max_len=ROW_SUMS_MAX_LABEL_CHARS, field="value") is None)


def _one_to_one(groups: dict, column: int) -> bool:
    """Every id value has one value of ``column`` in its rows, a different
    one for each id, and each passes :func:`_data_text`."""
    labels = []
    for members in groups.values():
        values = {row[column] for row in members if isinstance(row[column], str)}
        if len(values) != 1 or any(not isinstance(row[column], str) for row in members):
            return False
        labels.append(next(iter(values)))
    return len(set(labels)) == len(labels) and all(_data_text(label) for label in labels)


def _regrouped(columns: list, rows: list, key: int, keys: tuple, sums: list) -> list | None:
    """One entry per value of id column ``key``, first-row order, or ``None``
    when ``key`` is no repeated stable id (:func:`_stable_id`)."""
    if not all(_stable_id(row[key]) for row in rows):
        return None
    groups: dict = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    if not 0 < len(groups) < len(rows) or len(groups) > ROW_SUMS_MAX_VALUES:
        return None
    labels = [j for j in keys if j != key and _one_to_one(groups, j)]
    out = []
    for value, members in groups.items():
        entry: dict = {str(columns[key]): value}
        if labels:
            entry["labels"] = {str(columns[j]): members[0][j] for j in labels}
        entry["sums"] = {str(columns[i]): _exact_sum([row[i] for row in members]) for i in sums}
        out.append(entry)
    return out


def row_sums(columns: list, rows: list, sql: Any, verified_sums: int) -> dict | None:
    """``{"by_value": {id column: [entries]}}`` for one complete, verified
    result's ``columns`` and ``rows`` and the statement the model wrote
    (before the engine's row cap), else ``None`` (the module note above).

    ``verified_sums`` is how many SUM entries of the select list the check
    verified. Its entries are one per distinct SUM call and stop at
    ``aggregate_guard.MAX_AGGREGATES``, so fewer of them than the select
    list's SUM calls means some SUM, possibly one summed here, was never
    checked: then there is no block."""
    from .aggregate_guard import plain_sum_shape
    shape = plain_sum_shape(sql)
    if (shape is None or shape[0] != len(columns) or shape[3] > verified_sums
            or len(set(map(str, columns))) != len(columns)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
        return None
    _items, sum_items, keys, _calls = shape
    sums = []
    for i in sum_items:
        try:
            cells = [_exact_cell(row[i]) for row in rows]
        except ValueError:
            continue
        if any(cell is not None for cell in cells) and _data_text(str(columns[i])):
            sums.append(i)
    by_value = {}
    for key in keys if sums else ():
        entries = _regrouped(columns, rows, key, keys, sums)
        if entries is not None and _data_text(str(columns[key])):
            by_value[str(columns[key])] = entries
    block = {"by_value": by_value}
    return block if by_value and _encoded_size(block) <= ROW_SUMS_MAX_BYTES else None


def _verified_sums(result: dict) -> int:
    """How many SUM entries of the select list the aggregate check verified,
    when it verified every entry it recorded for the main query; 0 when it
    did not, or there is no check."""
    from .aggregate_guard import AGGREGATE_CHECK_KEY, VERIFIED
    check = result.get(AGGREGATE_CHECK_KEY)
    if not isinstance(check, dict) or check.get("status") != VERIFIED:
        return 0
    entries = [entry for entry in check.get("aggregates") or ()
               if isinstance(entry, dict) and entry.get("clause") != "subquery"]
    if not entries or any(entry.get("status") != VERIFIED for entry in entries):
        return 0
    return sum(entry.get("function") == "SUM" and entry.get("clause") == "select"
               for entry in entries)


def _inside_capture_budget(payload: dict) -> bool:
    from .response_integrity_capture import _BudgetExceeded, _budget_check
    try:
        _budget_check(payload)
    except (RecursionError, _BudgetExceeded):
        return False
    return True


def with_row_sums(result: dict, sql: str) -> dict:
    """*result* plus ``row_sums`` as its last key, when it is complete (not
    cut, nothing partial, every row counted, two or more rows), verified
    (:func:`_verified_sums`), :func:`row_sums` builds a block from the model's
    statement ``sql``, and the payload with it stays within
    :data:`MAX_QUERY_RESULT_BYTES` and does not newly fail the capture budget;
    otherwise *result* itself. Call it after every other key is on the
    payload. Never raises."""
    from .aggregate_guard import AGGREGATE_NOTE_KEY
    if (not isinstance(result, dict) or "error" in result or result.get("truncated") is True
            or result.get("partial_reason") or result.get(PARTIAL_NOTE_KEY)
            or result.get(AGGREGATE_NOTE_KEY) or result.get(REPEAT_NOTE_KEY)
            or ROW_SUMS_KEY in result):
        return result
    verified_sums = _verified_sums(result)
    columns, rows = result.get("columns"), result.get("rows")
    if (not verified_sums or not (isinstance(columns, list) and isinstance(rows, list))
            or len(rows) < 2 or result.get("row_count", len(rows)) != len(rows)):
        return result
    try:
        block = row_sums(columns, rows, sql, verified_sums)
        out = {**result, ROW_SUMS_KEY: block}
        if (block is None or _encoded_size(out) > MAX_QUERY_RESULT_BYTES
                or (_inside_capture_budget(result) and not _inside_capture_budget(out))):
            return result
    except Exception:  # noqa: BLE001 - the sums are an addition, never required
        return result
    logger.info("ROW_SUMS rows=%d id_columns=%d", len(rows), len(block["by_value"]))
    return out


def _cursor_rows(cursor: Any, limit: int) -> Iterator[Any]:
    fetchone = getattr(cursor, "fetchone", None)
    if callable(fetchone):
        for _ in range(limit):
            row = fetchone()
            if row is None:
                return
            yield row
        return
    fetchmany = getattr(cursor, "fetchmany", None)
    if callable(fetchmany):
        remaining = limit
        while remaining:
            chunk = list(fetchmany(min(32, remaining)))
            if not chunk:
                return
            for row in chunk[:remaining]:
                yield row
            remaining -= len(chunk[:remaining])
        return
    for index, row in enumerate(cursor):
        if index >= limit:
            return
        yield row


def fetch_max_plus_one(
    cursor: Any, max_rows: int, columns: Iterable[Any] | None = None,
) -> tuple[list[Any], bool]:
    """Read row-wise/bounded-chunk and enforce cell/byte/column limits in flight."""
    inferred = columns
    if inferred is None:
        inferred = [item[0] for item in (getattr(cursor, "description", None) or [])]
    accumulator = BoundedQueryAccumulator(inferred or [])
    exceeded = False
    for index, row in enumerate(_cursor_rows(cursor, max_rows + 1)):
        if index >= max_rows:
            exceeded = True
            break
        if not accumulator.add(row):
            exceeded = True
            break
    # A column cap alone reads every row; the result still carries the
    # column_limit marker through bounded_query_result's own accumulator.
    return accumulator.rows, exceeded or accumulator.partial_reason in _ROW_CUT_REASONS


def read_http_body_capped(response: Any, limit: int = MAX_QUERY_RESULT_BYTES) -> tuple[bytes, bool]:
    """Read a synchronous HTTP response stream without materializing beyond limit."""
    chunks: list[bytes] = []
    used = 0
    iterator = response.iter_bytes() if callable(getattr(response, "iter_bytes", None)) else [response.content]
    for chunk in iterator:
        raw = bytes(chunk)
        remaining = limit - used
        if len(raw) > remaining:
            if remaining:
                chunks.append(raw[:remaining])
            return b"".join(chunks), True
        chunks.append(raw)
        used += len(raw)
    return b"".join(chunks), False


def capped_mcp_http_client_factory(headers=None, timeout=None, auth=None):
    """Create the MCP SDK client: a byte-capped response stream, every request
    sent to the address the SSRF guard validates for it, and no redirects
    (``pinned_http``). The MCP SDK's own default client follows redirects."""
    import httpx
    from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

    from . import pinned_http

    class CappedStream(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            used = 0
            async for chunk in self.stream:
                used += len(chunk)
                if used > MAX_QUERY_RESULT_BYTES:
                    raise ValueError("MCP response body exceeds connector limit")
                yield chunk

        async def aclose(self):
            await self.stream.aclose()

    async def cap_response(response):
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_QUERY_RESULT_BYTES:
            raise ValueError("MCP response body exceeds connector limit")
        response.stream = CappedStream(response.stream)

    if timeout is None:  # the MCP SDK's own default
        timeout = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    client = pinned_http.async_client(connection_type="MCP Server", timeout=timeout,
                                      headers=headers, auth=auth)
    client.event_hooks.setdefault("response", []).append(cap_response)
    return client
