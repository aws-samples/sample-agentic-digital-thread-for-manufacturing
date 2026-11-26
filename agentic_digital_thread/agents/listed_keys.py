# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Keep a figure per listed record key when a total spans several keys.

Measured on a rehearsal account (2026-10-15, five fresh chats asking the
same four questions): the orchestrator asked a system agent for a total per
group across five listed record keys, one of which an earlier turn had
singled out. The system agent filtered on the five keys
(``col IN ('k1', ..., 'k5')``), grouped by the other column only and answered
with one total per group. Nothing in that answer said which key a total came
from, so the orchestrator filled that in from an earlier turn whose rows had
been filtered to a single group, and stated a subset the data did not
support. On the runs whose delegated question asked for per-key rows, the
answer was right.

Two deterministic halves, both on the system agent's side of the query
boundary (``system_agent._run_mode_a_query``):

* :func:`with_listed_keys_note` appends ONE fixed engine line to a delegated
  question that asks for an aggregate (a total, sum, count or value) and
  lists two or more record keys of one namespace, asking that every total
  across them also be given per key. Any other question is returned
  unchanged. Only the question the caller wrote is read, never the guidance
  or graph context appended after it.
* :func:`grain_line` returns ONE fixed engine line when a query this call ran
  aggregated rows across two or more listed record keys without grouping by
  their column (:func:`collapsed_key_filters`), unless the answer gives a
  figure for EVERY one of those keys that this call's own rows prove
  (:func:`proven_keys`). Fail closed: a key whose figure is not proven counts
  as missing. The wording follows the operator: SUM and COUNT add up, AVG
  averages, MIN and MAX pick a value.

A figure is proven for key K from ONE of this call's tool results, for one
of the collapsed aggregates (the same function over the same argument):

* the key's own aggregate: one answer line that names K and no other listed
  key carries the value that aggregate takes over K's rows in that result
  (SUM and COUNT added up, AVG as the mean, MIN or MAX); or
* the key's full breakdown: every one of K's rows in that result has its own
  such line carrying that row's value of the aggregate's per-row measure.

A row's measure is the aggregate's argument evaluated on the row (a column,
a product of two columns, or one per row for ``COUNT(*)``); an argument the
engine cannot evaluate proves nothing. In a result that is itself an
aggregate query, only an output column made by the SAME aggregate counts,
so a per-key unit price never proves a per-key ``SUM(quantity * price)``,
and one row's value out of several never stands for the key. The rows and
their statement are recorded by the capture hook (:func:`record_key_rows`)
from this call's tool results only, so a figure an agent restates from an
earlier call proves nothing.

When the collapsed query also grouped by other columns (totals per group
across the listed keys), a per-key figure is not enough: the answer must
pair each key with its groups. A key is proven only from a result whose rows
carry both the key and a value of those group columns, and only when, for
every group the key has rows for there, a line naming the key and that
group carries the pair's own figure (measured on a rehearsal account,
2026-10-15: a table per group plus a separate table per key passed the
per-key proof while saying nothing about which group a key went to, and the
orchestrator filled the pairing in from an earlier turn). The line then
names the group column, so it applies to exactly that pairing claim.

A record key is an identifier-shaped token (letters and digits joined by
``-``, ``_``, ``/`` or ``.``) with at least one letter and two digits, which
leaves out dates, plain words, column names and short codes; UUID row ids are
left out too. Keys count as a list only when two or more distinct ones share
a namespace: the same letters in the same places with digit runs of any
length, so two ``RUN-`` numbers are one list and a customer id next to an
order id is not.

The SQL reading is a bounded scan of one flat SELECT (no subquery, CTE or set
operation) that ignores string literals and comments; anything it cannot
read is treated as no collapse. Generic: no dataset word. Stdlib only.
"""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, NamedTuple

#: The identifier-shaped token, as ``identifier_translation`` reads one.
#: Alphanumeric runs joined by single separators: after the first separator,
#: each character is alphanumeric or a separator that an alphanumeric follows.
#: One character per repeat, so no input can be split two ways.
_KEY_TOKEN = re.compile(
    r"(?<![\w-])[A-Za-z0-9]+[-_/.][A-Za-z0-9](?:[A-Za-z0-9]|[-_/.](?=[A-Za-z0-9]))*(?![\w-])")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_MIN_KEY_CHARS = 5
_MAX_KEY_CHARS = 64
_MAX_TEXT_CHARS = 20_000
_MAX_SQL_CHARS = 200_000
_MAX_ANSWER_LINES = 2_000
_MAX_JSON_DEPTH = 8
_MAX_KEY_ROWS = 4_000
_MAX_KEY_RESULTS = 64
_MAX_BREAKDOWN_ROWS = 50
_MAX_KEY_LINES = 200
_MAX_GROUP_VALUE_CHARS = 120

#: Words that mark a question as asking for an aggregate figure.
_AGGREGATE_INTENT = re.compile(
    r"\b(?:totals?|totall?ed|sums?|summed|how\s+many|how\s+much|counts?|number\s+of"
    r"|values?)\b", re.I)

#: Appended to a delegated question that lists several record keys of one
#: kind. Fixed text: no data value, no dataset word, no current-state word
#: (the integrity layer reads the question's intent).
LISTED_KEYS_NOTE = (
    "[engine] This question lists several record keys of one kind. Wherever your "
    "answer gives a total, sum or count across them, also give that figure for "
    "each listed key. When the question also groups the figures (for example per "
    "recipient or per site), give one row for each pair of a listed key and a group "
    "in a single table, never one table per key and another per group, so the "
    "reader can see which group each key's figure belongs to.")

#: Invocation-state key of the rows (from this call's tool results) that hold
#: a record key: ``[{"result": i, "keys": [...], "numbers": {col: Decimal},
#: "present": [non-null columns]}]``.
KEY_ROWS_STATE_KEY = "_adt_listed_key_rows"
#: The executed statement behind each recorded result, by result index.
KEY_RESULTS_STATE_KEY = "_adt_listed_key_results"


def is_record_key(token: Any) -> bool:
    """True for an identifier-shaped token with a letter and two digits."""
    if not isinstance(token, str) or not _MIN_KEY_CHARS <= len(token) <= _MAX_KEY_CHARS:
        return False
    if _UUID.fullmatch(token) or not _KEY_TOKEN.fullmatch(token):
        return False
    return sum(ch.isdigit() for ch in token) >= 2 and any(ch.isalpha() for ch in token)


def key_namespace(token: str) -> str:
    """The token lower-cased with each digit run as ``9``: two keys are of one
    kind when their letters and separators agree."""
    return re.sub(r"[0-9]+", "9", token.casefold())


def _namespace_groups(tokens: Iterable[str]) -> list[str]:
    """Distinct record keys (first spelling, case-insensitive) in namespaces
    that hold two or more of them, in first-seen order."""
    by_kind: dict[str, dict[str, str]] = {}
    for token in tokens:
        if is_record_key(token):
            by_kind.setdefault(key_namespace(token), {}).setdefault(token.casefold(), token)
    return [key for group in by_kind.values() if len(group) >= 2
            for key in group.values()]


def listed_keys(text: Any) -> list[str]:
    """The record keys ``text`` lists: two or more distinct keys of one
    namespace."""
    if not isinstance(text, str) or not text:
        return []
    return _namespace_groups(_KEY_TOKEN.findall(text[:_MAX_TEXT_CHARS]))


def asks_for_aggregate(text: Any) -> bool:
    """True when ``text`` asks for a total, sum, count or value."""
    return isinstance(text, str) and bool(_AGGREGATE_INTENT.search(text[:_MAX_TEXT_CHARS]))


def with_listed_keys_note(question: Any) -> Any:
    """``question`` plus :data:`LISTED_KEYS_NOTE` when it asks for an aggregate
    and lists record keys of one namespace; anything else unchanged."""
    if not asks_for_aggregate(question) or not listed_keys(question):
        return question
    return f"{question.rstrip()}\n\n{LISTED_KEYS_NOTE}"


# --------------------------------------------------------------------------- #
# SQL: a filter on listed keys that an aggregate runs across
# --------------------------------------------------------------------------- #
_SELECT = re.compile(r"\bselect\b", re.I)
_FROM = re.compile(r"\bfrom\b", re.I)
_WHERE = re.compile(r"\bwhere\b", re.I)
_GROUP_BY = re.compile(r"\bgroup\s+by\b", re.I)
_AFTER_WHERE = re.compile(r"\b(?:group\s+by|having|order\s+by|limit|offset|window|fetch)\b", re.I)
_AFTER_GROUP = re.compile(r"\b(?:having|order\s+by|limit|offset|window|fetch)\b", re.I)
_AGGREGATE_CALL = re.compile(r"\b(sum|count|avg|min|max)\s*\(", re.I)
#: Aggregates whose value lists the grouped-away values themselves.
_LIST_AGGREGATE = re.compile(
    r"\b(?:string_agg|array_agg|json_agg|jsonb_agg|json_object_agg|jsonb_object_agg)\s*\(", re.I)
_IN_LIST = re.compile(
    r"(?P<col>(?:\"?[A-Za-z_][A-Za-z0-9_$]*\"?\s*\.\s*)?\"?[A-Za-z_][A-Za-z0-9_$]*\"?)\s+"
    r"(?P<neg>not\s+)?in\s*\(\s*"
    r"(?P<items>'(?:[^']|'')*'(?:\s*,\s*'(?:[^']|'')*')*)\s*\)", re.I)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_MASKED_LITERAL = re.compile(r"'[^']*'")
_CAST = re.compile(r"::\s*[A-Za-z_]\w*(?:\s*\([^)]*\))?")
_ARG_COLUMN = re.compile(
    r"(?<![\w$])(?:\"?[A-Za-z_][A-Za-z0-9_$]*\"?\s*\.\s*)*\"?([A-Za-z_][A-Za-z0-9_$]*)\"?"
    r"(?!\s*\()")
_ALIAS = re.compile(
    r"\s*(?:::\s*[A-Za-z_]\w*(?:\s*\([^)]*\))?)?\s+(?:as\s+)?(\"?)([A-Za-z_][A-Za-z0-9_]*)\1"
    r"(?=\s*(?:,|$))", re.I)
_NOT_A_COLUMN = frozenset({"not", "and", "or", "where", "when", "then", "else", "on"})
_SQL_WORDS = frozenset({
    "distinct", "all", "case", "when", "then", "else", "end", "null", "and", "or",
    "not", "is", "in", "as", "true", "false", "cast", "like", "between", "any",
    "numeric", "int", "integer", "bigint", "smallint", "decimal", "float", "real",
    "double", "precision", "text", "varchar", "char", "date", "timestamp", "interval",
    "over", "filter", "from"})
_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
#: Operator families, in the order the wording prefers them.
_FAMILY = {"sum": "total", "count": "total", "avg": "average", "min": "extreme",
           "max": "extreme"}


class Collapse(NamedTuple):
    """One WHERE filter whose listed keys an aggregate runs across."""
    column: str
    keys: tuple[str, ...]
    operator: str               # "total", "average" or "extreme"
    measures: frozenset[str]    # bare columns inside the aggregate calls
    counts_rows: bool           # a COUNT is among them
    #: ``(function, argument spec)`` per aggregate call (:func:`_aggregate_spec`).
    aggregates: tuple[tuple[str, tuple], ...] = ()
    #: One entry per GROUP BY item: the result column names it can appear
    #: under (its bare column, then select-list aliases of it); an
    #: expression the engine cannot name is an empty entry.
    groups: tuple[tuple[str, ...], ...] = ()


def _scan(sql: str) -> tuple[str, str, list[tuple[int, int]]]:
    """``(code, masked, string spans)`` for ``sql``, all the same length.

    ``code`` has comments blanked; ``masked`` also blanks every string
    literal's body. Double-quoted identifiers are skipped as one unit so a
    comment marker inside one is not read as a comment.
    """
    code, masked, spans = list(sql), list(sql), []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            for k in range(i, end):
                code[k] = masked[k] = " "
            i = end
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for k in range(i, end):
                code[k] = masked[k] = " " if sql[k] != "\n" else "\n"
            i = end
        elif ch == "'":
            k = i + 1
            while k < n:
                if sql[k] == "'":
                    if k + 1 < n and sql[k + 1] == "'":
                        k += 2
                        continue
                    break
                k += 1
            end = min(k + 1, n)
            for j in range(i + 1, end - 1):
                masked[j] = "x"
            spans.append((i, end))
            i = end
        elif ch == '"':
            end = sql.find('"', i + 1)
            i = n if end < 0 else end + 1
        else:
            i += 1
    return "".join(code), "".join(masked), spans


def _clause(masked: str, start_re: re.Pattern, end_re: re.Pattern) -> tuple[int, int] | None:
    match = start_re.search(masked)
    if not match:
        return None
    end = end_re.search(masked, match.end())
    return match.end(), (end.start() if end else len(masked))


def _bare_column(ref: str) -> str:
    return ref.split(".")[-1].strip().strip('"').lower()


def _call_end(masked: str, open_at: int, end: int) -> int:
    """Index just past the parenthesis that closes the one before ``open_at``."""
    depth, k = 1, open_at
    while k < end and depth:
        depth += {"(": 1, ")": -1}.get(masked[k], 0)
        k += 1
    return k


def _list_aggregate_spans(masked: str, start: int, end: int) -> list[str]:
    """The argument text of every list aggregate call in ``masked[start:end]``."""
    out: list[str] = []
    for match in _LIST_AGGREGATE.finditer(masked, start, end):
        out.append(masked[match.end():_call_end(masked, match.end(), end)])
    return out


class _Select(NamedTuple):
    code: str
    masked: str
    spans: list[tuple[int, int]]
    select_list: tuple[int, int]


def _names_blanked(masked: str) -> str:
    """``masked`` with the text of each double-quoted name blanked (its quotes
    kept, a doubled quote inside one read as part of it), same length: a
    parenthesis or the word ``from`` in a quoted alias or column is data."""
    out, inside, i = list(masked), False, 0
    while i < len(masked):
        if masked[i] == '"':
            if inside and masked[i + 1:i + 2] == '"':
                out[i] = out[i + 1] = " "
                i += 2
                continue
            inside = not inside
        elif inside:
            out[i] = " "
        i += 1
    return "".join(out)


def _list_from(masked: str, start: int) -> re.Match | None:
    """The SELECT list's own FROM: the first ``from`` after ``start`` outside
    any parenthesis opened after it and outside any double-quoted name, so
    not the FROM inside ``EXTRACT(field FROM x)``, ``SUBSTRING(s FROM i)`` or
    ``TRIM(c FROM s)``.

    The mask does not blank every literal form (a PostgreSQL dollar-quoted or
    ``E''`` string, for one), so a parenthesis in one can throw the count off.
    When no ``from`` is found at the list's own depth (or the count goes
    below it), the reading is the plain one: the first ``from`` after ``start``.
    So this only ever moves the list end past a FROM that sits inside
    parentheses before a FROM at the list's depth."""
    bare = _names_blanked(masked)
    depth, at = 0, start
    for match in _FROM.finditer(bare, start):
        for ch in bare[at:match.start()]:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth < 0:
                return _FROM.search(masked, start)
        at = match.start()
        if depth == 0:
            return match
    return _FROM.search(masked, start)


def _flat_select(sql: Any) -> _Select | None:
    """The scanned statement when it is one flat SELECT, else None."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > _MAX_SQL_CHARS:
        return None
    code, masked, spans = _scan(sql)
    selects = list(_SELECT.finditer(masked))
    if len(selects) != 1:
        return None
    from_match = _list_from(masked, selects[0].end())
    if not from_match:
        return None
    return _Select(code, masked, spans, (selects[0].end(), from_match.start()))


def _aggregates(stmt: _Select) -> list[tuple[str, str, int]]:
    """``(function, argument text, end index)`` for every aggregate call in
    the SELECT list."""
    start, end = stmt.select_list
    out = []
    for match in _AGGREGATE_CALL.finditer(stmt.masked, start, end):
        close = _call_end(stmt.masked, match.end(), end)
        out.append((match.group(1).lower(), stmt.masked[match.end():close - 1], close))
    return out


# Possessive repeats: every qualifier segment ends in a literal ".", which the
# last segment cannot hold, so giving one back never helps a match.
_BARE_REF = re.compile(
    r"^(?:\"?[A-Za-z_][A-Za-z0-9_$]*+\"?\s*+\.\s*+)*+\"?([A-Za-z_][A-Za-z0-9_$]*)\"?$")


def _strip_parens(text: str) -> str:
    """``text`` without parentheses that wrap all of it."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for index, ch in enumerate(text):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0 and index < len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def _aggregate_spec(func: str, argument: str) -> tuple:
    """What one aggregate call adds up, in a form the rows can evaluate:
    ``("star",)`` for ``COUNT(*)``/``COUNT(1)``, ``("col", c)``, ``("product",
    a, b)`` for ``a * b``, else ``("opaque",)`` (DISTINCT, CASE, functions,
    literals or anything else)."""
    arg = _strip_parens(_CAST.sub(" ", argument))
    if re.match(r"(?i)distinct\b", arg) or "'" in arg:
        return ("opaque",)
    if func == "count" and arg in ("*", "1"):
        return ("star",)
    match = _BARE_REF.match(arg)
    if match and match.group(1).lower() not in _SQL_WORDS:
        return ("col", match.group(1).lower())
    parts = [_strip_parens(part) for part in arg.split("*")]
    if len(parts) == 2:
        refs = [_BARE_REF.match(part) for part in parts]
        if all(refs) and not any(ref.group(1).lower() in _SQL_WORDS for ref in refs):
            return ("product", refs[0].group(1).lower(), refs[1].group(1).lower())
    return ("opaque",)


def _split_top(text: str) -> list[str]:
    """``text`` split at commas outside parentheses."""
    parts, depth, start = [], 0, 0
    for index, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


_ITEM_ALIAS = re.compile(
    r"^(?P<expr>.*?\S)\s+(?:as\s+)?\"?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\"?$", re.I | re.S)


def _select_items(stmt: "_Select") -> list[tuple[str, str]]:
    """``(bare column of the expression or "", output alias or "")`` per
    select-list item."""
    items = []
    for part in _split_top(stmt.masked[stmt.select_list[0]:stmt.select_list[1]]):
        text = re.sub(r"(?i)^distinct\s+", "", part)
        expr, alias = text, ""
        match = _ITEM_ALIAS.match(text)
        if (match and not _BARE_REF.match(text)
                and match.group("alias").lower() not in _SQL_WORDS):
            expr, alias = match.group("expr"), match.group("alias").lower()
        bare = _BARE_REF.match(_strip_parens(expr))
        name = bare.group(1).lower() if bare and bare.group(1).lower() not in _SQL_WORDS else ""
        items.append((name, alias))
    return items


def _group_entries(stmt: "_Select", group_text: str) -> tuple[tuple[str, ...], ...]:
    """The result column names each GROUP BY item can appear under: its bare
    column and the aliases a select item gives that column; for an ordinal,
    the select item's alias or column. An expression gives an empty entry."""
    items = _select_items(stmt)
    entries = []
    for part in _split_top(group_text):
        text = _strip_parens(part)
        if text.isdigit():
            index = int(text) - 1
            name, alias = items[index] if 0 <= index < len(items) else ("", "")
            entries.append(tuple(dict.fromkeys(n for n in (alias, name) if n)))
            continue
        bare = _BARE_REF.match(text)
        if not bare or bare.group(1).lower() in _SQL_WORDS:
            entries.append(())
            continue
        name = bare.group(1).lower()
        aliases = [alias for item, alias in items if item == name and alias]
        entries.append(tuple(dict.fromkeys([name, *aliases])))
    return tuple(entries)


def _argument_columns(argument: str) -> set[str]:
    text = _CAST.sub(" ", _MASKED_LITERAL.sub(" ", argument))
    return {name.lower() for name in _ARG_COLUMN.findall(text)
            if name.lower() not in _SQL_WORDS}


def statement_shape(sql: Any) -> "tuple[bool, dict[str, tuple[str, tuple]]] | None":
    """``(aggregates, outputs)`` for one flat SELECT, None otherwise:
    whether its list has an aggregate call, and the output name of each
    aliased one with its ``(function, argument spec)`` (``SUM(x) AS units``
    gives ``{"units": ("sum", ("col", "x"))}``)."""
    stmt = _flat_select(sql)
    if stmt is None:
        return None
    calls = _aggregates(stmt)
    outputs: dict[str, tuple[str, tuple]] = {}
    end = stmt.select_list[1]
    for func, arg, close in calls:
        match = _ALIAS.match(stmt.masked[close:end].split(",", 1)[0] + ",")
        if match and match.group(2).lower() not in _SQL_WORDS:
            outputs.setdefault(match.group(2).lower(), (func, _aggregate_spec(func, arg)))
    return bool(calls), outputs


def collapsed_key_filters(sql: Any) -> list[Collapse]:
    """One :class:`Collapse` for each WHERE filter of an aggregate query that
    keeps two or more record keys of one namespace (``col IN ('k1', ...)``)
    while the query neither groups by that column nor lists its values in a
    list aggregate. ``column`` is the bare, lower-cased column name.

    One flat SELECT only: a subquery, CTE or set operation, a negated list,
    a list of numbers, a list outside WHERE, or SQL that does not scan
    returns ``[]``.
    """
    stmt = _flat_select(sql)
    if stmt is None:
        return []
    aggregates = _aggregates(stmt)
    if not aggregates:
        return []
    where = _clause(stmt.masked, _WHERE, _AFTER_WHERE)
    if where is None:
        return []
    group = _clause(stmt.masked, _GROUP_BY, _AFTER_GROUP)
    group_text = stmt.masked[group[0]:group[1]] if group else ""
    listed_aggregates = _list_aggregate_spans(stmt.masked, *stmt.select_list)
    families = [_FAMILY[func] for func, _arg, _end in aggregates]
    operator = next(f for f in ("total", "average", "extreme") if f in families)
    measures = frozenset().union(*(_argument_columns(arg) for _f, arg, _e in aggregates))
    counts_rows = any(func == "count" for func, _arg, _end in aggregates)
    specs = tuple(dict.fromkeys((func, _aggregate_spec(func, arg))
                                for func, arg, _end in aggregates))
    groups = _group_entries(stmt, group_text) if group else ()
    out: list[Collapse] = []
    for match in _IN_LIST.finditer(stmt.code, *where):
        if match.group("neg") or any(a <= match.start() < b for a, b in stmt.spans):
            continue
        column = _bare_column(match.group("col"))
        if column in _NOT_A_COLUMN:
            continue
        values = [value.replace("''", "'") for value in _LITERAL.findall(match.group("items"))]
        keys = _namespace_groups(values)
        if not keys:
            continue
        shown = re.compile(rf"(?<![\w$]){re.escape(column)}(?![\w$])", re.I)
        if shown.search(group_text) or any(shown.search(arg) for arg in listed_aggregates):
            continue
        out.append(Collapse(column, tuple(keys), operator, measures - {column},
                            counts_rows, specs, groups))
    return out


# --------------------------------------------------------------------------- #
# The rows behind a proof: this call's own tool results
# --------------------------------------------------------------------------- #
_NUMERIC_TEXT = re.compile(r"-?\d+(?:\.\d+)?")


def _number(cell: Any) -> Decimal | None:
    if isinstance(cell, bool):
        return None
    if isinstance(cell, int):
        return Decimal(cell)
    if isinstance(cell, float):
        return Decimal(repr(cell)) if cell == cell and abs(cell) != float("inf") else None
    if isinstance(cell, str) and len(cell) <= 40 and _NUMERIC_TEXT.fullmatch(cell.strip()):
        return Decimal(cell.strip())
    return None


def record_key_rows(invocation_state: Any, payload: Any) -> None:
    """Keep, from one tool result's rows (``{"columns": [...], "rows": [[...]]}``),
    each row that holds a record key: its keys, its numeric cells and its
    non-null columns, plus the result's executed statement
    (``_adt_executed_query``) for the proof. Bounded; anything else is
    ignored."""
    if not isinstance(invocation_state, dict) or not isinstance(payload, dict):
        return
    columns, rows = payload.get("columns"), payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return
    results = invocation_state.get(KEY_RESULTS_STATE_KEY)
    if not isinstance(results, list):
        results = []
        invocation_state[KEY_RESULTS_STATE_KEY] = results
    if len(results) >= _MAX_KEY_RESULTS:
        return
    kept = invocation_state.get(KEY_ROWS_STATE_KEY)
    if not isinstance(kept, list):
        kept = []
        invocation_state[KEY_ROWS_STATE_KEY] = kept
    sql = payload.get("_adt_executed_query")
    result = len(results)
    results.append(sql if isinstance(sql, str) and len(sql) <= _MAX_SQL_CHARS else "")
    names = [_bare_column(str(name)) for name in columns]
    for row in rows:
        if len(kept) >= _MAX_KEY_ROWS:
            return
        if not isinstance(row, list):
            continue
        keys = [cell.casefold() for cell in row if is_record_key(cell)]
        if not keys:
            continue
        numbers, present, texts = {}, [], {}
        for name, cell in zip(names, row):
            if cell is not None:
                present.append(name)
            value = _number(cell)
            if value is not None:
                numbers.setdefault(name, value)
            if (isinstance(cell, (str, int)) and not isinstance(cell, bool)
                    and 0 < len(str(cell)) <= _MAX_GROUP_VALUE_CHARS):
                texts.setdefault(name, str(cell))
        kept.append({"result": result, "keys": keys, "numbers": numbers,
                     "present": present, "values": texts})


def key_rows(invocation_state: Any) -> list[dict]:
    """The rows :func:`record_key_rows` kept for this call (well-formed only)."""
    rows = invocation_state.get(KEY_ROWS_STATE_KEY) if isinstance(invocation_state, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:_MAX_KEY_ROWS]
            if isinstance(row, dict) and isinstance(row.get("keys"), list)
            and isinstance(row.get("numbers"), dict)]


def key_results(invocation_state: Any) -> list[str]:
    """The executed statement of each recorded result, by result index."""
    results = (invocation_state.get(KEY_RESULTS_STATE_KEY)
               if isinstance(invocation_state, dict) else None)
    if not isinstance(results, list):
        return []
    return [sql if isinstance(sql, str) else "" for sql in results[:_MAX_KEY_RESULTS]]


def _row_measure(row: dict, func: str, spec: tuple) -> Decimal | None:
    """One raw row's value of an aggregate's per-row measure, None when the
    row cannot give it."""
    numbers, kind = row["numbers"], spec[0]
    if kind == "star":
        return Decimal(1)
    if kind == "col" and func == "count":
        present = row.get("present")
        return Decimal(1) if isinstance(present, list) and spec[1] in present else None
    if kind == "col":
        value = numbers.get(spec[1])
        return value if isinstance(value, Decimal) else None
    if kind == "product":
        a, b = numbers.get(spec[1]), numbers.get(spec[2])
        return a * b if isinstance(a, Decimal) and isinstance(b, Decimal) else None
    return None


def _combine(func: str, values: list[Decimal], *, from_outputs: bool) -> Decimal | None:
    """The key's own aggregate from its per-row values: SUM and COUNT add up,
    MIN and MAX pick, AVG is the mean of raw rows (an average of averages is
    not the key's average, so from aggregate outputs only a single row is)."""
    if func in ("sum", "count"):
        return sum(values, Decimal(0))
    if func == "min":
        return min(values)
    if func == "max":
        return max(values)
    if from_outputs:
        return values[0] if len(values) == 1 else None
    return sum(values, Decimal(0)) / len(values)


def _rows_evidence(own: list[dict], shape: tuple, func: str,
                   spec: tuple) -> tuple[Decimal | None, list[Decimal]] | None:
    """``(aggregate, per-row values)`` that one aggregate takes over ``own``
    rows of one result, None when the result cannot give it: an aggregate
    query only through an output column made by the same aggregate, a raw
    result through the aggregate's per-row measure on every row."""
    has_aggregate, outputs = shape
    if has_aggregate:
        for name, made_by in outputs.items():
            values = [row["numbers"].get(name) for row in own]
            if made_by == (func, spec) and all(isinstance(v, Decimal) for v in values):
                return _combine(func, values, from_outputs=True), values
        return None
    values = [_row_measure(row, func, spec) for row in own]
    if values and all(isinstance(v, Decimal) for v in values):
        return _combine(func, values, from_outputs=False), values
    return None


def _rows_by_result(rows: list[dict], key: str) -> dict[int, list[dict]]:
    by_result: dict[int, list[dict]] = {}
    for row in rows:
        index = row.get("result")
        if key in row["keys"] and isinstance(index, int) and not isinstance(index, bool):
            by_result.setdefault(index, []).append(row)
    return by_result


def _shape(results: list[str], index: int, shapes: dict):
    sql = results[index] if 0 <= index < len(results) else ""
    if sql not in shapes:
        shapes[sql] = statement_shape(sql)
    return shapes[sql]


def _key_evidence(rows: list[dict], results: list[str], key: str, collapse: Collapse,
                  shapes: dict) -> list[tuple[Decimal | None, list[Decimal]]]:
    """``(the key's aggregate, its per-row values)`` for each of this call's
    results and collapsed aggregates the key's rows can give.

    A result whose statement is not one flat SELECT gives nothing. A result
    of an aggregate query gives only an output column made by the same
    aggregate (function and argument); a raw result gives the aggregate's
    per-row measure evaluated on each of the key's rows. Every one of the
    key's rows must give a value, or the result gives nothing."""
    out: list[tuple[Decimal | None, list[Decimal]]] = []
    for index, own in _rows_by_result(rows, key).items():
        shape = _shape(results, index, shapes)
        if shape is None:
            continue
        for func, spec in collapse.aggregates:
            evidence = _rows_evidence(own, shape, func, spec)
            if evidence is not None:
                out.append(evidence)
    return out


def _group_part(row: dict, groups: tuple[tuple[str, ...], ...]) -> tuple[str, ...] | None:
    """The row's values of the collapse's group columns it carries (any name
    a group entry can appear under), None when it carries none."""
    values = row.get("values")
    if not isinstance(values, dict):
        return None
    part = []
    for names in groups:
        hit = next((values[name] for name in names if name in values), None)
        if isinstance(hit, str):
            part.append(hit)
    return tuple(part) or None


def _pair_evidence(rows: list[dict], results: list[str], key: str, collapse: Collapse,
                   shapes: dict) -> list[dict[tuple[str, ...], tuple[Decimal | None, list[Decimal]]]]:
    """For each of this call's results whose rows pair ``key`` with the
    collapse's group columns, and each collapsed aggregate: the key's figure
    per group (``{group values: (aggregate, per-row values)}``). Every one of
    the key's rows in that result must carry a group value and a value of
    the aggregate, or the result gives nothing for that aggregate."""
    out = []
    for index, own in _rows_by_result(rows, key).items():
        shape = _shape(results, index, shapes)
        if shape is None:
            continue
        parts: dict[tuple[str, ...], list[dict]] = {}
        for row in own:
            part = _group_part(row, collapse.groups)
            if part is None:
                parts = {}
                break
            parts.setdefault(part, []).append(row)
        if not parts:
            continue
        for func, spec in collapse.aggregates:
            figures = {part: _rows_evidence(group_rows, shape, func, spec)
                       for part, group_rows in parts.items()}
            if all(figure is not None for figure in figures.values()):
                out.append(figures)
    return out


# --------------------------------------------------------------------------- #
# The answer: which listed keys get a figure the rows prove
# --------------------------------------------------------------------------- #
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_ANSWER_NUMBER = re.compile(
    r"(?<![\w.$/:-])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w/:-]|\.\d|,\d{3})")


def _json_lines(node: Any, depth: int = 0) -> Iterator[str]:
    """One line per JSON object or array (its scalar members joined), with a
    multi-line string member split into its own lines."""
    if depth > _MAX_JSON_DEPTH:
        return
    members = list(node.values()) if isinstance(node, dict) else node
    scalars: list[str] = []
    for value in members:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str) and "\n" in value:
            yield from value.splitlines()
        elif isinstance(value, (str, int, float)):
            scalars.append(str(value))
    if scalars:
        yield " | ".join(scalars)
    for value in members:
        if isinstance(value, (dict, list)):
            yield from _json_lines(value, depth + 1)


def _answer_lines(answer: str) -> list[str]:
    stripped = answer.strip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return list(_json_lines(parsed))[:_MAX_ANSWER_LINES]
    return answer.splitlines()[:_MAX_ANSWER_LINES]


def _line_numbers(line: str) -> list[tuple[Decimal, int]]:
    """``(value, decimal places)`` for each number written in ``line`` once
    record keys and a leading list marker are removed (a decimal such as
    ``340.00`` is identifier-shaped but no key, so it stays)."""
    text = _LIST_MARKER.sub("", _KEY_TOKEN.sub(
        lambda match: " " if is_record_key(match.group(0)) else match.group(0), line))
    out = []
    for whole, fraction in _ANSWER_NUMBER.findall(text):
        try:
            out.append((Decimal(whole.replace(",", "") + (fraction or "")),
                        len(fraction) - 1 if fraction else 0))
        except InvalidOperation:
            continue
    return out


def _matches(number: Decimal, places: int, values: set[Decimal]) -> bool:
    step = Decimal(1).scaleb(-places)
    for value in values:
        try:
            if value.quantize(step, rounding=ROUND_HALF_UP) == number:
                return True
        except InvalidOperation:
            continue
    return False


def _carried(line_numbers: list[tuple[Decimal, int]], value: Decimal) -> bool:
    return any(_matches(number, places, {value}) for number, places in line_numbers)


def _breakdown_shown(per_row: list[Decimal], lines: list[list[tuple[Decimal, int]]]) -> bool:
    """Every per-row value carried by a line of its own (a matching of rows to
    distinct lines)."""
    if not per_row or len(per_row) > _MAX_BREAKDOWN_ROWS or len(per_row) > len(lines):
        return False
    owner: dict[int, int] = {}

    def place(row: int, seen: set[int]) -> bool:
        for line, numbers in enumerate(lines):
            if line in seen or not _carried(numbers, per_row[row]):
                continue
            seen.add(line)
            if line not in owner or place(owner[line], seen):
                owner[line] = row
                return True
        return False

    return all(place(row, set()) for row in range(len(per_row)))


def _names_value(line: str, part: tuple[str, ...]) -> bool:
    """True when ``line`` names one of a group's values (word-bounded,
    case-insensitive)."""
    for value in part:
        text = value.strip()
        if text and re.search(rf"(?<![\w-]){re.escape(text)}(?![\w-])", line, re.I):
            return True
    return False


def _pairs_shown(figures: dict, lines: list[tuple[str, list[tuple[Decimal, int]]]]) -> bool:
    """Every group's figure is on a line that also names that group: the
    group's own aggregate, or each of its rows on a line of its own."""
    for part, (aggregate, per_row) in figures.items():
        candidates = [numbers for text, numbers in lines if _names_value(text, part)]
        if not candidates:
            return False
        if not ((aggregate is not None and any(_carried(n, aggregate) for n in candidates))
                or _breakdown_shown(per_row, candidates)):
            return False
    return True


def proven_keys(answer: Any, collapse: Collapse, rows: list[dict],
                results: Iterable[str] = ()) -> set[str]:
    """The listed keys (lower-cased) whose figure this call's rows prove (see
    the module docstring). Without other grouping: one line naming only that
    key among the listed ones carries the key's own aggregate, or each of the
    key's rows in one result has its own such line carrying that row's value.
    When the collapsed query also grouped by other columns, the key's figure
    must be given per group instead: for every group the key has rows for in
    one result, a line naming the key and that group carries the pair's
    figure."""
    if not isinstance(answer, str) or not answer:
        return set()
    keys = list(dict.fromkeys(key.casefold() for key in collapse.keys))
    patterns = {key: re.compile(rf"(?<![\w-]){re.escape(key)}(?![\w-])", re.I) for key in keys}
    lines: dict[str, list[tuple[str, list[tuple[Decimal, int]]]]] = {key: [] for key in keys}
    for line in _answer_lines(answer):
        named = [key for key, pattern in patterns.items() if pattern.search(line)]
        if len(named) == 1 and len(lines[named[0]]) < _MAX_KEY_LINES:
            numbers = _line_numbers(line)
            if numbers:
                lines[named[0]].append((line, numbers))
    statements = list(results or [])
    shapes: dict = {}
    proven: set[str] = set()
    for key in keys:
        if not lines[key]:
            continue
        if collapse.groups:
            if any(_pairs_shown(figures, lines[key])
                   for figures in _pair_evidence(rows, statements, key, collapse, shapes)):
                proven.add(key)
            continue
        key_lines = [numbers for _text, numbers in lines[key]]
        for aggregate, per_row in _key_evidence(rows, statements, key, collapse, shapes):
            if ((aggregate is not None
                 and any(_carried(numbers, aggregate) for numbers in key_lines))
                    or _breakdown_shown(per_row, key_lines)):
                proven.add(key)
                break
    return proven


_HEAD = {
    "total": "[engine] A total in this answer adds up rows for",
    "average": "[engine] An average in this answer is taken over rows for",
    "extreme": "[engine] A smallest or largest value in this answer is picked from rows for",
}


def _group_display(groups: tuple[tuple[str, ...], ...]) -> str:
    """The group column the line names: the first named GROUP BY item that is
    not an id column, else the first named one, else ``group``."""
    named = [names[0] for names in groups if names]
    for name in named:
        if not re.search(r"(?:^|_)id$", name):
            return name
    return named[0] if named else "group"


_GROUPED = {
    "total": ("totals", "add up rows for"),
    "average": ("averages", "are taken over rows for"),
    "extreme": ("smallest or largest values", "are picked from rows for"),
}


def grain_line_text(column: str, count: int, operator: str = "total", proven: int = 0,
                    group: str = "") -> str:
    """The fixed engine line for ``count`` listed values of ``column`` of
    which ``proven`` have a figure of their own; with ``group``, the figures
    the answer gives are per that group column and the line is about the
    key and group pairs. An unsafe column name is replaced with ``key``, an
    unsafe group name with ``group``."""
    name = column if isinstance(column, str) and _COLUMN_NAME.fullmatch(column) else "key"
    family = operator if operator in _HEAD else "total"
    count, proven = int(count), max(0, int(proven))
    if group:
        by = group if isinstance(group, str) and _COLUMN_NAME.fullmatch(group) else "group"
        noun, verb = _GROUPED[family]
        if proven:
            return (f"[engine] The {by} {noun} in this answer {verb} {count} listed {name} "
                    f"values, and the answer pairs only {proven} of them with their {by} "
                    f"values, so it does not show which {by} values the other "
                    f"{count - proven} have rows for.")
        return (f"[engine] The {by} {noun} in this answer {verb} {count} listed {name} "
                f"values without pairing any {name} with its {by} values, so it does not "
                f"show which {by} values a single {name} has rows for, or which {name} a "
                f"{by} figure came from.")
    if proven:
        rest = {"total": f"what the other {count - proven} contributed",
                "average": f"the average for the other {count - proven}",
                "extreme": f"the value for the other {count - proven}"}[family]
        return (f"{_HEAD[family]} {count} listed {name} values, but the answer gives a "
                f"figure of its own for only {proven} of them, so it does not show {rest}.")
    rest = {"total": f"which {name} a total came from",
            "average": f"the average for any one {name}",
            "extreme": f"which {name} that value came from"}[family]
    return (f"{_HEAD[family]} {count} listed {name} values with no figure for any "
            f"single one of them, so it does not show {rest}.")


def grain_line(query_texts: Iterable[Any], answer: Any, rows: Iterable[dict] = (),
               results: Iterable[str] = ()) -> str:
    """The engine line for the first listed-key filter that a query this call
    aggregated across and the answer does not give a proven figure for every
    key of (per group, when that query also grouped by other columns); ``""``
    when there is none. ``rows`` and ``results`` are this call's
    :func:`key_rows` and :func:`key_results`."""
    if not isinstance(answer, str):
        return ""
    texts = [text for text in (query_texts or []) if isinstance(text, str)]
    kept = [row for row in (rows or []) if isinstance(row, dict)
            and isinstance(row.get("keys"), list) and isinstance(row.get("numbers"), dict)]
    statements = [sql if isinstance(sql, str) else "" for sql in (results or [])]
    seen: set[tuple] = set()
    for sql in texts:
        for collapse in collapsed_key_filters(sql):
            keys = frozenset(key.casefold() for key in collapse.keys)
            fact = (collapse.column, keys, collapse.groups)
            if fact in seen:
                continue
            seen.add(fact)
            proven = proven_keys(answer, collapse, kept, statements)
            if len(proven) < len(keys):
                group = _group_display(collapse.groups) if collapse.groups else ""
                return grain_line_text(collapse.column, len(keys), collapse.operator,
                                       len(proven), group)
    return ""
