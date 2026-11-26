# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which model-written queries can be added up from pieces, read exactly.

The Athena exact-total split (``athena_exact_split``) runs a refused query as
several pieces, each the SAME text plus one conjunct on a partition column, and
merges their rows. That is exact only for a shape where every output is a
GROUP BY key or an aggregate whose per-piece values combine: COUNT and SUM add,
MIN and MAX keep the extreme. This module recognises that shape and refuses
everything else with a named reason (the caller keeps the honest scan-limit
error). It reads the text with the conservative tokenizer the scope guard uses.

Admitted: ONE ``SELECT`` over ONE relation (no JOIN, set operation, subquery or
CTE), items that are a GROUP BY key or ``COUNT(*)``, ``COUNT(<number>)``, or
COUNT / SUM / MIN / MAX of one row-level expression (a column, or say
``CASE WHEN ... THEN 1 ELSE 0 END``: no aggregate inside), an optional WHERE,
GROUP BY over exactly the key items, ORDER BY over output columns, and a
numeric LIMIT. One list expansion right after the table is admitted too,
``CROSS JOIN UNNEST(<row expression>) [AS] name(column, ...)``, or the same
written with a comma, ``, UNNEST(...)`` (Trino reads both alike): it turns each
row's list into rows of that row, so a row's elements are all in the piece
that holds the row, and per-piece aggregates over them still combine. A piece
keeps the form the model wrote.
Refused: DISTINCT, HAVING, window functions, AVG and every other
aggregate, arithmetic over aggregates, grouping sets, OFFSET, a time-travel
clause the model wrote, a volatile function anywhere (``rand``, ``uuid`` and
the like: each piece would draw its own values) or the current time in the
SELECT list, a current time it cannot freeze exactly (below), any other join or
table function, a second expansion, ``WITH ORDINALITY``, an expansion with no
column names or with an aggregate inside, and anything the tokenizer reads as
uncertain.

The current time in WHERE is admitted and FROZEN: the split takes one UTC
instant and :func:`frozen_sql` replaces each clock form (real tokens only;
never text in a string literal or a quoted name) with a literal of the type
the engine gives it, so every piece reads the same instant:
``current_timestamp`` and ``now()`` a ``timestamp(3) with time zone``,
``localtimestamp`` a ``timestamp(3)`` (``(p)`` keeps p digits), and
``current_date`` a DATE, all in the session zone, UTC (the connector sets no
other). A time of day (``current_time``, ``localtime``) or an argument it
does not read is refused with ``clock_unfrozen``.

It also reads, for the cost planner only, the day range a WHERE conjunct gives
a column (``bounds``). A missed bound only makes the plan more cautious; it
never changes which rows a piece counts.

Pure and offline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .athena_scope import Token, _matching_close, _split_boolean, _strip_outer, relations, tokenize

AGGREGATES = ("count", "sum", "min", "max")

#: Words that make a statement a shape the split cannot add up (or cannot
#: read safely). Checked on word tokens only: a quoted name or a string never
#: matches.
_REFUSED_WORDS = {
    "WITH": "a common table expression",
    "JOIN": "a join",
    "UNION": "a set operation", "INTERSECT": "a set operation", "EXCEPT": "a set operation",
    "HAVING": "a HAVING clause",
    "OVER": "a window function", "WINDOW": "a window function",
    "QUALIFY": "a QUALIFY clause",
    "DISTINCT": "DISTINCT",
    "OFFSET": "an OFFSET", "FETCH": "a FETCH clause",
    "TABLESAMPLE": "TABLESAMPLE",
    "GROUPING": "grouping sets", "ROLLUP": "grouping sets", "CUBE": "grouping sets",
    "FILTER": "an aggregate FILTER clause",
    "WITHIN": "an ordered-set aggregate",
    "LATERAL": "a lateral relation", "UNNEST": "UNNEST", "VALUES": "VALUES",
    "FOR": "a time-travel clause",
    "ALL": "ALL",
}
_CLAUSES = ("WHERE", "GROUP", "ORDER", "LIMIT")
#: SQL words that are never a column name (kept out of ``Shape.referenced``).
_SQL_WORDS = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "IS", "IN", "AS", "BY",
    "GROUP", "ORDER", "LIMIT", "CASE", "WHEN", "THEN", "ELSE", "END", "BETWEEN",
    "LIKE", "ASC", "DESC", "NULLS", "FIRST", "LAST", "TRUE", "FALSE", "DATE",
    "TIMESTAMP", "INTERVAL", "CAST", "COUNT", "SUM", "MIN", "MAX", "UTC",
    "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "LOCALTIMESTAMP",
    "LOCALTIME", "NOW",
})
#: Functions whose value is the time the engine evaluates them: each piece
#: evaluates them on its own, seconds apart (the result says so).
_CLOCK_WORDS = frozenset({"current_timestamp", "current_date", "current_time",
                          "localtimestamp", "localtime", "now"})
#: Functions whose value differs on every evaluation: pieces would each draw
#: their own, so the merged rows would not be one query's.
_VOLATILE_FUNCS = frozenset({"rand", "random", "secure_rand", "secure_random", "uuid", "shuffle"})
#: Clock forms that freeze exactly: ``(literal form, default precision)``.
_FREEZABLE = {"current_timestamp": ("timestamp_tz", 3), "localtimestamp": ("timestamp", 3),
              "current_date": ("date", None)}
#: The most fractional digits a timestamp literal may carry.
MAX_CLOCK_PRECISION = 12
_ISO_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_TEMPORAL_FUNCS = {"from_iso8601_timestamp", "from_iso8601_date", "date", "timestamp"}


class ShapeRefusal(ValueError):
    """The query cannot be computed exactly from pieces; ``reason`` is a code."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Output:
    """One SELECT item: a GROUP BY ``key`` or an ``agg`` with its function."""
    kind: str
    func: str | None
    text: str
    alias: str | None


@dataclass(frozen=True)
class OrderItem:
    column: int
    descending: bool
    nulls_first: bool


@dataclass(frozen=True)
class ClockUse:
    """One clock form in the query text: ``sql[start:end]``, frozen as a
    ``form`` literal (``timestamp_tz``, ``timestamp`` or ``date``) with
    ``precision`` fractional digits."""
    start: int
    end: int
    form: str
    precision: int | None


@dataclass(frozen=True)
class PercentileUse:
    """One ``approx_percentile(expr, q|ARRAY[...])`` in the SELECT list.
    ``start``/``end`` span the whole call in the query text; ``arg_start``/
    ``arg_end`` span the inner value expression and ``q_start``/``q_end`` the
    probability spec, so both are sliced verbatim from the SQL. ``array`` is
    True for the ``ARRAY[...]`` (several quantiles) form. ``value_type`` is the
    value expression's proved Athena type (``bigint``/``double``/``real``),
    empty until resolved against the schema. A piece replaces the call with a
    serialized ``qdigest_agg(CAST(arg AS <value_type>), 1, <accuracy>)`` and a
    later query merges the digests and reads the quantiles. The result is
    approximate, like ``approx_percentile`` itself, keeping the value's own
    type (so two BIGINTs beyond 2^53 stay distinct)."""
    start: int
    end: int
    arg_start: int
    arg_end: int
    q_start: int
    q_end: int
    array: bool
    value_type: str = ""


@dataclass(frozen=True)
class Shape:
    sql: str
    table_parts: tuple[str, ...]
    name_end: int
    outputs: tuple[Output, ...]
    grouped: bool
    order: tuple[OrderItem, ...]
    limit: int | None
    referenced: frozenset[str]
    bounds: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    clock: tuple[ClockUse, ...] = ()
    #: The column names a list expansion gives (lower case), or empty.
    expanded: frozenset[str] = frozenset()
    #: Where the query's own LIMIT starts in ``sql``, when it has one.
    limit_at: int | None = None
    #: The approx_percentile calls in the SELECT list, with their spans, or
    #: empty for an ordinary COUNT/SUM/MIN/MAX shape (byte-identical when so).
    percentiles: tuple[PercentileUse, ...] = ()

    @property
    def uses_clock(self) -> bool:
        return bool(self.clock)

    @property
    def has_percentile(self) -> bool:
        return bool(self.percentiles)

    @property
    def table(self) -> str:
        return self.table_parts[-1]


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _refuse(reason: str, detail: str):
    raise ShapeRefusal(reason, detail)


def _top_level(tokens: list[Token]) -> dict[str, int]:
    """Index of each depth-0 clause keyword (first occurrence)."""
    found: dict[str, int] = {}
    for index, token in enumerate(tokens):
        word = _upper(token)
        if token.depth == 0 and word in (*_CLAUSES, "FROM") and word not in found:
            found[word] = index
    return found


def _split_commas(tokens: list[Token]) -> list[list[Token]]:
    if not tokens:
        return []
    base = tokens[0].depth
    parts, start = [], 0
    for index, token in enumerate(tokens):
        if token.text == "," and token.depth == base:
            parts.append(tokens[start:index])
            start = index + 1
    parts.append(tokens[start:])
    return parts


def _norm(tokens: list[Token]) -> str:
    """Comparable text of an expression: names case-folded (the engine reads
    names case-insensitively), literals and symbols as written."""
    return " ".join(t.value.lower() if t.kind in {"word", "identifier"} else t.text
                    for t in _strip_outer(tokens))


def _column_ref(tokens: list[Token]) -> str | None:
    """The column a bare, quoted or qualified reference names, else ``None``."""
    tokens = _strip_outer(tokens)
    if len(tokens) == 1 and tokens[0].kind in {"word", "identifier"}:
        return tokens[0].value
    if (len(tokens) == 3 and tokens[1].text == "."
            and all(t.kind in {"word", "identifier"} for t in (tokens[0], tokens[2]))):
        return tokens[2].value
    return None


def _percentile(expr: list[Token]) -> PercentileUse | None:
    """``approx_percentile(<value expr>, <number | ARRAY[...]>)`` as a
    :class:`PercentileUse`, else ``None``. The value expression holds no
    aggregate (a per-row value the digest summarises); a weight argument (the
    three-argument form) or a non-literal/complex probability is not read."""
    if len(expr) < 6 or expr[0].kind != "word" or expr[0].value.lower() != "approx_percentile":
        return None
    if expr[1].text != "(" or _matching_close(expr, 1) != len(expr) - 1:
        return None
    inner = expr[2:-1]
    args = _top_commas(inner)
    if len(args) != 2:                          # the weighted 3-arg form is not read
        return None
    value, quant = args
    if not value or not quant or _named_aggregate(value):
        return None
    array = _upper(quant[0]) == "ARRAY"
    if not _literal_quantiles(quant, array):
        return None
    return PercentileUse(expr[0].start, expr[-1].end, value[0].start, value[-1].end,
                         quant[0].start, quant[-1].end, array)


def _top_commas(tokens: list[Token]) -> list[list[Token]]:
    """Split ``tokens`` on commas at their own base paren depth AND outside any
    square brackets (so the commas inside ``ARRAY[0.9, 0.99]`` do not split the
    argument list)."""
    if not tokens:
        return []
    base = min(t.depth for t in tokens)
    parts, start, brackets = [], 0, 0
    for i, t in enumerate(tokens):
        if t.text == "[":
            brackets += 1
        elif t.text == "]":
            brackets = max(0, brackets - 1)
        elif t.depth == base and brackets == 0 and t.text == ",":
            parts.append(tokens[start:i])
            start = i + 1
    parts.append(tokens[start:])
    return parts


def _literal_quantiles(quant: list[Token], array: bool) -> bool:
    """Whether the probability argument is a plain number, or ``ARRAY[`` of
    plain numbers ``]`` (so a piece and the merge read the same quantiles)."""
    if not array:
        return len(quant) == 1 and quant[0].kind == "number"
    if len(quant) < 3 or quant[1].text != "[" or quant[-1].text != "]":
        return False
    items = _top_commas(quant[2:-1])
    return bool(items) and all(len(it) == 1 and it[0].kind == "number" for it in items)


def _aggregate(tokens: list[Token]) -> str | None:
    """``count``/``sum``/``min``/``max`` when the WHOLE item is one admitted
    aggregate of a row-level expression (no aggregate inside), else ``None``."""
    if len(tokens) < 4 or tokens[0].kind != "word" or tokens[1].text != "(":
        return None
    func = tokens[0].value.lower()
    if func not in AGGREGATES or _matching_close(tokens, 1) != len(tokens) - 1:
        return None
    inner = tokens[2:-1]
    if func == "count" and len(inner) == 1 and (inner[0].text == "*" or inner[0].kind == "number"):
        return func
    if len(inner) == 1 and inner[0].text == "*":
        return None
    return None if _named_aggregate(inner) else func


_NOT_ALIAS = {"END", "NULL", "TRUE", "FALSE", "DESC", "ASC"}
#: Aggregate names a refusal names (for a clear reason); correctness does not
#: depend on this list, because an item that is not an admitted aggregate must
#: be a GROUP BY key, and the engine refuses an aggregate there.
_OTHER_AGGREGATES = {
    "avg", "approx_distinct", "approx_percentile", "approx_most_frequent",
    "arbitrary", "any_value", "array_agg", "bool_and", "bool_or", "count_if",
    "every", "geometric_mean", "listagg", "map_agg", "max_by", "min_by",
    "stddev", "stddev_pop", "stddev_samp", "string_agg", "variance",
    "var_pop", "var_samp", "checksum", "histogram",
}


def _split_alias(item: list[Token]) -> tuple[list[Token], str | None]:
    """``(expression, alias)`` of one SELECT item (``AS name`` or a trailing name)."""
    if len(item) >= 3 and _upper(item[-2]) == "AS" and item[-1].kind in {"word", "identifier"}:
        return item[:-2], item[-1].value
    if len(item) < 2:
        return item, None
    last, before = item[-1], item[-2]
    trailing_name = (last.kind in {"word", "identifier"} and last.depth == item[0].depth
                     and _upper(last) not in _NOT_ALIAS)
    ends_expression = (before.kind in {"word", "identifier", "literal", "number"}
                       or before.text in {")", "*"})
    if trailing_name and ends_expression and before.text != ".":
        return item[:-1], last.value
    return item, None


def _named_aggregate(expr: list[Token]) -> str | None:
    for index, token in enumerate(expr[:-1]):
        name = token.value.lower() if token.kind == "word" else ""
        if expr[index + 1].text == "(" and (name in _OTHER_AGGREGATES or name in AGGREGATES):
            return name
    return None


def _outputs(items: list[list[Token]]) -> tuple[list[Output], list[PercentileUse]]:
    out = []
    pctls: list[PercentileUse] = []
    for raw in items:
        if not raw:
            _refuse("unparsable", "an empty SELECT item")
        expr, alias = _split_alias(raw)
        if len(expr) == 1 and expr[0].text == "*":
            _refuse("not_decomposable", "SELECT * returns rows, not totals")
        pctl = _percentile(expr)
        if pctl is not None:
            pctls.append(pctl)
            out.append(Output("percentile", "approx_percentile", _norm(expr), alias))
            continue
        func = _aggregate(expr)
        named = None if func else _named_aggregate(expr)
        if named:
            _refuse("not_decomposable",
                    f"{named.upper()}(...) here is not COUNT, SUM, MIN or MAX of one row expression")
        out.append(Output("agg" if func else "key", func, _norm(expr), alias))
    return out, pctls


def _check_words(tokens: list[Token], skip: frozenset[int] = frozenset()) -> None:
    for index, token in enumerate(tokens):
        word = _upper(token)
        if word in _REFUSED_WORDS and index not in skip:
            _refuse("not_decomposable", f"the query uses {_REFUSED_WORDS[word]}")
        if token.kind == "semicolon":
            _refuse("unparsable", "more than one statement")
    if sum(_upper(t) == "SELECT" for t in tokens) != 1:
        _refuse("not_decomposable", "the query has a subquery")


@dataclass(frozen=True)
class _Expansion:
    """``CROSS JOIN UNNEST(...) [AS] name(columns)``: tokens ``[start, end)``."""
    start: int
    end: int
    columns: frozenset[str]
    #: How many tokens open it before ``UNNEST``'s ``(``: 3 for ``CROSS JOIN
    #: UNNEST``, 2 for ``, UNNEST`` (the comma form; the same in Trino).
    opening: int = 3

    @property
    def keywords(self) -> frozenset[int]:
        """The ``CROSS JOIN UNNEST`` (or ``, UNNEST``) words, which the refusal
        list must not see."""
        return frozenset(range(self.start, self.start + self.opening))


def _not_named(what: str):
    _refuse("not_decomposable", f"the list expansion {what}")


def _expansion_opening(tokens: list[Token], index: int) -> int:
    """How many tokens open a depth-0 list expansion at ``index`` (``CROSS
    JOIN UNNEST (`` or ``, UNNEST (``, the UNNEST a word), else 0."""
    if tokens[index].depth != 0:
        return 0
    for words in (("CROSS", "JOIN", "UNNEST"), (",", "UNNEST")):
        size = len(words)
        span = tokens[index:index + size + 1]
        if (len(span) == size + 1 and span[-1].text == "(" and span[-2].kind == "word"
                and [t.text if w == "," else _upper(t) for t, w in zip(span, words)] == list(words)):
            return size
    return 0


def _expansion(tokens: list[Token]) -> _Expansion | None:
    """The one top-level list expansion of the query (``CROSS JOIN UNNEST``,
    or ``, UNNEST`` right after the table), ``None`` when there is none; a
    second one, or one that names no columns or holds an aggregate, is
    refused. Where it stands is checked by :func:`_after_relation_ok`."""
    starts = [(i, size) for i in range(len(tokens)) if (size := _expansion_opening(tokens, i))]
    if not starts:
        return None
    if len(starts) > 1:
        _not_named("is not the only one")
    start, opening = starts[0]
    close = _matching_close(tokens, start + opening)
    if close is None or close == start + opening + 1 or _named_aggregate(
            tokens[start + opening + 1:close]):
        _not_named("holds no row expression, or an aggregate")
    index = close + 2 if close + 1 < len(tokens) and _upper(tokens[close + 1]) == "AS" else close + 1
    if (index + 1 >= len(tokens) or tokens[index].kind not in {"word", "identifier"}
            or tokens[index + 1].text != "("):
        _not_named("does not name its columns")
    names_close = _matching_close(tokens, index + 1)
    names = _split_commas(tokens[index + 2:names_close]) if names_close else []
    if not names or any(len(n) != 1 or n[0].kind not in {"word", "identifier"} for n in names):
        _not_named("does not name its columns")
    return _Expansion(start, names_close + 1, frozenset(n[0].value.lower() for n in names), opening)


def _check_volatile(tokens: list[Token], from_index: int) -> None:
    """Refuse a volatile function anywhere, and the clock in the SELECT list."""
    for index, token in enumerate(tokens):
        name = token.value.lower() if token.kind == "word" else ""
        if name in _VOLATILE_FUNCS and index + 1 < len(tokens) and tokens[index + 1].text == "(":
            _refuse("volatile", f"the query uses {name}(), whose value differs between pieces")
        if name in _CLOCK_WORDS and index < from_index:
            _refuse("volatile", f"the query selects {name}, which each piece would read on its own")


def _unfrozen(name: str):
    _refuse("clock_unfrozen", f"the query reads the current time as {name}, which the split "
            "cannot fix to one instant exactly")


def _clock_use(tokens: list[Token], index: int) -> ClockUse | None:
    """The clock form starting at ``tokens[index]``, ``None`` when it is not
    one, or :func:`_unfrozen`."""
    token = tokens[index]
    name = token.value.lower() if token.kind == "word" else ""
    if name not in _CLOCK_WORDS:
        return None
    following = [t.text for t in tokens[index + 1:index + 4]]
    if name == "now":
        if following[:1] != ["("]:
            return None                         # a column named now
        if following[1:2] != [")"]:
            _unfrozen("now(...)")
        return ClockUse(token.start, tokens[index + 2].end, "timestamp_tz", 3)
    if name not in _FREEZABLE:
        _unfrozen(name)
    form, precision = _FREEZABLE[name]
    if following[:1] != ["("]:
        return ClockUse(token.start, token.end, form, precision)
    digits = tokens[index + 2] if index + 2 < len(tokens) else None
    if (form == "date" or digits is None or digits.kind != "number" or not digits.text.isdigit()
            or following[2:3] != [")"] or int(digits.text) > MAX_CLOCK_PRECISION):
        _unfrozen(f"{name}(...)")
    return ClockUse(token.start, tokens[index + 3].end, form, int(digits.text))


def _clock_uses(tokens: list[Token], name_end: int) -> tuple[ClockUse, ...]:
    uses = tuple(u for u in (_clock_use(tokens, i) for i in range(len(tokens))) if u is not None)
    if any(u.start < name_end for u in uses):
        _unfrozen("a clock before the table name")
    return uses


def _group_items(tokens, start, end, outputs) -> None:
    keys = {i for i, o in enumerate(outputs) if o.kind == "key"}
    matched: set[int] = set()
    for item in _split_commas(tokens[start:end]):
        index = _output_index(item, outputs, by_alias=False)
        if index is None or index not in keys:
            _refuse("not_decomposable", "a GROUP BY item is not one of the selected keys")
        matched.add(index)
    if matched != keys:
        _refuse("not_decomposable", "a selected key is not in GROUP BY")


def _output_index(item: list[Token], outputs: list[Output], *, by_alias: bool) -> int | None:
    item = _strip_outer(item)
    if len(item) == 1 and item[0].kind == "number" and item[0].text.isdigit():
        position = int(item[0].text) - 1
        return position if 0 <= position < len(outputs) else None
    text = _norm(item)
    if by_alias and len(item) == 1 and item[0].kind in {"word", "identifier"}:
        for index, output in enumerate(outputs):
            if output.alias is not None and (
                    output.alias == item[0].value
                    or (item[0].kind == "word" and output.alias.lower() == item[0].value.lower())):
                return index
    return next((i for i, o in enumerate(outputs) if o.text == text), None)


def _order_items(tokens, start, end, outputs) -> tuple[OrderItem, ...]:
    out = []
    for item in _split_commas(tokens[start:end]):
        words = [_upper(t) for t in item]
        nulls_first = None
        if len(item) >= 2 and words[-2] == "NULLS" and words[-1] in {"FIRST", "LAST"}:
            nulls_first, item, words = words[-1] == "FIRST", item[:-2], words[:-2]
        descending = bool(item) and words[-1] == "DESC"
        if item and words[-1] in {"ASC", "DESC"}:
            item = item[:-1]
        index = _output_index(item, outputs, by_alias=True)
        if index is None:
            _refuse("not_decomposable", "an ORDER BY item is not a selected column")
        out.append(OrderItem(index, descending, bool(nulls_first)))
    return tuple(out)


def _limit(tokens, start) -> int | None:
    rest = tokens[start + 1:]
    if len(rest) != 1 or rest[0].kind != "number" or not rest[0].text.isdigit():
        _refuse("not_decomposable", "the LIMIT is not a plain number")
    return int(rest[0].text)


def _relation(tokens: list[Token]):
    scans, uncertain = relations(tokens)
    if uncertain or len(scans) != 1 or scans[0].depth != 0:
        _refuse("not_decomposable", "the query does not read exactly one table")
    relation = scans[0]
    last = relation.table_token_index + 2 * (len(relation.parts) - 1)
    return relation, tokens[last].end, last


def _after_relation_ok(tokens: list[Token], last: int, clauses: dict[str, int],
                       expansion: _Expansion | None = None) -> None:
    """Only an alias, and then the one list expansion, may sit between the
    table name and the next clause."""
    stop = min([i for k, i in clauses.items() if k != "FROM" and i > last] or [len(tokens)])
    if expansion is not None and not last < expansion.start < expansion.end <= stop:
        _refuse("not_decomposable", "the list expansion does not follow the table")
    span = range(expansion.start, expansion.end) if expansion is not None else range(0)
    between = [t for i, t in enumerate(tokens[last + 1:stop], last + 1) if i not in span]
    if between and _upper(between[0]) == "AS":
        between = between[1:]
    if len(between) > 1 or (between and between[0].kind not in {"word", "identifier"}):
        _refuse("not_decomposable", "the table reference is not a plain name")


def _check_clause_order(clauses: dict[str, int], tokens: list[Token]) -> None:
    positions = [clauses[k] for k in ("FROM", *_CLAUSES) if k in clauses]
    if positions != sorted(positions) or "FROM" not in clauses:
        _refuse("unparsable", "the clauses are not in a recognised order")
    for keyword in ("GROUP", "ORDER"):
        index = clauses.get(keyword)
        if index is not None and (index + 1 >= len(tokens) or _upper(tokens[index + 1]) != "BY"):
            _refuse("unparsable", f"{keyword} without BY")


def _clause_end(clauses: dict[str, int], start: int, total: int) -> int:
    return min([i for i in clauses.values() if i > start] or [total])


def parse_shape(sql: str) -> Shape:
    """The decomposable shape of ``sql``, or :class:`ShapeRefusal`."""
    text = (sql or "").strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens:
        _refuse("unparsable", "the query is not one plain SELECT")
    expansion = _expansion(tokens)
    _check_words(tokens, expansion.keywords if expansion else frozenset())
    if _upper(tokens[0]) != "SELECT":
        _refuse("unparsable", "the query is not one plain SELECT")
    clauses = _top_level(tokens)
    _check_clause_order(clauses, tokens)
    _check_volatile(tokens, clauses["FROM"])
    relation, name_end, last = _relation(tokens)
    _after_relation_ok(tokens, last, clauses, expansion)
    outputs, pctls = _outputs(_split_commas(tokens[1:clauses["FROM"]]))
    aggs = [o for o in outputs if o.kind in {"agg", "percentile"}]
    if not aggs:
        _refuse("not_decomposable", "the query has no COUNT, SUM, MIN, MAX or approx_percentile")
    if len(pctls) > 1:
        _refuse("not_decomposable", "the query has more than one approx_percentile")
    grouped = "GROUP" in clauses
    if grouped:
        start = clauses["GROUP"] + 2
        _group_items(tokens, start, _clause_end(clauses, start, len(tokens)), outputs)
    elif any(o.kind == "key" for o in outputs):
        _refuse("not_decomposable", "a selected column is neither a key nor an aggregate")
    order: tuple[OrderItem, ...] = ()
    if "ORDER" in clauses:
        start = clauses["ORDER"] + 2
        order = _order_items(tokens, start, _clause_end(clauses, start, len(tokens)), outputs)
        if any(outputs[item.column].kind == "percentile" for item in order):
            # Ordering by an approximate array/scalar percentile would need the
            # engine's own typed ordering; refuse rather than sort rendered text.
            _refuse("not_decomposable", "ORDER BY an approx_percentile output is not supported")
    limit = _limit(tokens, clauses["LIMIT"]) if "LIMIT" in clauses else None
    where = _where_tokens(tokens, clauses)
    return Shape(
        sql=text, table_parts=tuple(relation.parts), name_end=name_end,
        outputs=tuple(outputs), grouped=grouped, order=order, limit=limit,
        referenced=_referenced(tokens),
        bounds=day_bounds(where),
        clock=_clock_uses(tokens, name_end),
        expanded=expansion.columns if expansion else frozenset(),
        limit_at=tokens[clauses["LIMIT"]].start if "LIMIT" in clauses else None,
        percentiles=tuple(pctls),
    )


def unlimited(shape: Shape) -> Shape:
    """``shape`` without its own LIMIT (its text stops before it), for the
    pieces of a grouped query: each reads every group, and the merge applies
    the ORDER BY and LIMIT once over all of them."""
    if shape.limit_at is None:
        return shape
    return replace(shape, sql=shape.sql[:shape.limit_at].rstrip(), limit=None, limit_at=None)


def all_groups(shape: Shape) -> Shape:
    """``shape`` with no ORDER BY and no LIMIT, for an OR branch's intermediate
    merge: every branch returns ALL of its groups, and the original ORDER BY
    and LIMIT are applied ONCE in the final combine over all branches."""
    return replace(shape, order=(), limit=None)


def _referenced(tokens: list[Token]) -> frozenset[str]:
    """Names the query may read: quoted names, and words that are not SQL words."""
    return frozenset(t.value.lower() for t in tokens
                     if t.kind == "identifier"
                     or (t.kind == "word" and t.value.upper() not in _SQL_WORDS))


def _where_tokens(tokens: list[Token], clauses: dict[str, int]) -> list[Token]:
    if "WHERE" not in clauses:
        return []
    start = clauses["WHERE"] + 1
    return tokens[start:_clause_end(clauses, start, len(tokens))]


# --------------------------------------------------------------------------- #
# Day bounds a WHERE gives its columns (planner input only)
# --------------------------------------------------------------------------- #
def _literal_day(tokens: list[Token]) -> str | None:
    """The ``YYYY-MM-DD`` a temporal or ISO-string literal starts with."""
    tokens = _strip_outer(tokens)
    literal = None
    if len(tokens) == 1:
        literal = tokens[0]
    elif len(tokens) == 2 and _upper(tokens[0]) in {"DATE", "TIMESTAMP"}:
        literal = tokens[1]
    elif (len(tokens) == 4 and tokens[0].kind == "word"
          and tokens[0].value.lower() in _TEMPORAL_FUNCS and tokens[1].text == "("):
        literal = tokens[2]
    elif len(tokens) == 6 and _upper(tokens[0]) == "CAST" and _upper(tokens[3]) == "AS":
        literal = tokens[2]
    if literal is None or literal.kind != "literal":
        return None
    match = _ISO_DAY.match(literal.text[1:-1])
    return match.group(1) if match else None


def _comparison(tokens: list[Token]):
    base = min(t.depth for t in tokens)
    for index, token in enumerate(tokens):
        if token.depth == base and _upper(token) == "BETWEEN":
            and_at = next((j for j in range(index + 1, len(tokens))
                           if tokens[j].depth == base and _upper(tokens[j]) == "AND"), None)
            if and_at is None:
                return None
            return (_column_ref(tokens[:index]), _literal_day(tokens[index + 1:and_at]),
                    _literal_day(tokens[and_at + 1:]))
    for index, token in enumerate(tokens):
        if token.depth != base or token.kind != "operator" or token.text in {"!=", "<>"}:
            continue
        left, right, op = tokens[:index], tokens[index + 1:], token.text
        column, day = _column_ref(left), _literal_day(right)
        if column is None:
            column, day = _column_ref(right), _literal_day(left)
            op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
        if column is None or day is None:
            return None
        return (column, day if op in {">", ">=", "="} else None,
                day if op in {"<", "<=", "="} else None)
    return None


def day_bounds(where: list[Token]) -> dict[str, tuple[str | None, str | None]]:
    """``{column: (lowest day, highest day)}`` over the WHERE's top-level AND
    conjuncts. Days are inclusive and unadjusted (the planner widens them)."""
    out: dict[str, tuple[str | None, str | None]] = {}
    if not where:
        return out
    for part in _split_boolean(where, "AND"):
        part = _strip_outer(part)
        if not part or len(_split_boolean(part, "OR")) > 1:
            continue
        found = _comparison(part)
        if not found or found[0] is None:
            continue
        column, lo, hi = found[0].lower(), found[1], found[2]
        old_lo, old_hi = out.get(column, (None, None))
        out[column] = (max(filter(None, (old_lo, lo)), default=None),
                       min(filter(None, (old_hi, hi)), default=None))
    return out


# --------------------------------------------------------------------------- #
# Piece text: the model's SQL plus one conjunct (and the pinned snapshot)
# --------------------------------------------------------------------------- #
def clock_literal(use: ClockUse, instant: datetime) -> str:
    """The literal ``use`` reads at ``instant`` (taken in UTC, the session zone)."""
    moment = instant.astimezone(timezone.utc)
    if use.form == "date":
        return f"DATE '{moment:%Y-%m-%d}'"
    stamp = f"{moment:%Y-%m-%d %H:%M:%S}"
    if use.precision:
        stamp += "." + f"{moment.microsecond:06d}".ljust(MAX_CLOCK_PRECISION, "0")[:use.precision]
    return f"TIMESTAMP '{stamp} UTC'" if use.form == "timestamp_tz" else f"TIMESTAMP '{stamp}'"


#: The qdigest accuracy (max relative rank error) the split asks for on every
#: piece, stated in the lineage and the approximate-result note. 0.01 is the
#: Athena engine 3 default for ``qdigest_agg``; naming it makes the accuracy a
#: fixed, declared property of the result rather than an engine default left
#: implicit. Smaller is more accurate (more memory).
QDIGEST_ACCURACY = 0.01

#: Athena ``qdigest`` supports these value types; the digest is built over the
#: value expression's OWN type (not forced to double), so two BIGINTs beyond
#: 2**53 stay distinct.
_QDIGEST_VALUE_TYPES = ("bigint", "double", "real")


def resolve_value_type(arg: str, column_types: dict) -> str:
    """The proved qdigest value type of the percentile's value expression, or
    :class:`ShapeRefusal`. ``column_types`` maps a lower-case column name to its
    Athena type. Support is deliberately NARROW (no general expression type
    resolver): exactly

    (a) a BARE column reference whose Glue type is bigint, double or real, or
    (b) a plain ``date_diff('<unit>', <column>, <column>)`` -- a quoted string
        unit literal and two bare column references -- which is BIGINT.

    Anything else -- arithmetic on either form, a cast, any other function
    call, a column of any other type -- refuses, so the split never silently
    changes the value family."""
    tokens, uncertain = tokenize((arg or "").strip())
    if uncertain or not tokens:
        _refuse("not_decomposable", "the percentile value expression could not be read")
    bare = _bare_column(tokens)
    if bare is not None:
        base = str(column_types.get(bare.lower()) or "").strip().lower().split("(")[0]
        if base in _QDIGEST_VALUE_TYPES:
            return base
        _refuse("not_decomposable",
                "the percentile value column is not bigint, double or real, or is not a column")
    if _is_plain_date_diff(tokens):
        return "bigint"
    _refuse("not_decomposable",
            "the percentile value expression is not a bare bigint/double/real column "
            "or a plain date_diff(unit, column, column)")


def _bare_column(tokens) -> str | None:
    """The column name when ``tokens`` is exactly ONE bare or quoted column
    reference, else ``None``. A dotted ``a.b`` is refused: its qualifier may be
    an UNNEST alias or a row value, not the base table, so the Glue type of a
    column named ``b`` would not prove the value's type."""
    if len(tokens) == 1 and tokens[0].kind in {"word", "identifier"}:
        return tokens[0].value
    return None


def _is_plain_date_diff(tokens) -> bool:
    """``True`` when ``tokens`` is exactly ``date_diff('unit', col, col)``: the
    function word, a string-literal unit, and two BARE column references, no
    arithmetic, cast or nesting."""
    if (len(tokens) < 8 or tokens[0].kind != "word" or tokens[0].value.lower() != "date_diff"
            or tokens[1].text != "(" or tokens[-1].text != ")"):
        return False
    if _matching_close(tokens, 1) != len(tokens) - 1:
        return False
    args = _split_commas(tokens[2:-1])
    if len(args) != 3:
        return False
    unit, left, right = args
    return (len(unit) == 1 and unit[0].kind == "literal"
            and _bare_column(left) is not None and _bare_column(right) is not None)


def with_percentile_types(shape: "Shape", column_types: dict) -> "Shape":
    """``shape`` with each percentile's ``value_type`` resolved against
    ``column_types`` (refuses when a type cannot be proved). A shape with no
    percentile is returned unchanged."""
    if not shape.percentiles:
        return shape
    if shape.expanded:
        # An UNNEST expansion adds names the Glue schema does not describe, so
        # the value's type cannot be proved from it.
        _refuse("not_decomposable", "an approx_percentile over an UNNEST expansion is not supported")
    resolved = tuple(replace(u, value_type=resolve_value_type(shape.sql[u.arg_start:u.arg_end],
                                                              column_types))
                     for u in shape.percentiles)
    return replace(shape, percentiles=resolved)


def digest_piece_expr_typed(arg: str, value_type: str) -> str:
    """A serialized ``qdigest_agg`` over ``arg`` cast to ``value_type``
    (bigint/double/real), with unit weight and the fixed accuracy, as hex of
    its varbinary (so it survives as a result cell and a later query merges
    it). Refuses an unsupported ``value_type``."""
    base = str(value_type or "").strip().lower()
    if base not in _QDIGEST_VALUE_TYPES:
        _refuse("not_decomposable", "a percentile value type is not bigint, double or real")
    return (f"to_hex(CAST(qdigest_agg(CAST({arg} AS {base}), 1, {QDIGEST_ACCURACY}) "
            "AS varbinary))")


def digest_piece_expr(shape: Shape, use: PercentileUse) -> str:
    """The piece's replacement for one ``approx_percentile`` call: a serialized
    ``qdigest_agg`` over the value expression cast to its proved type
    (``use.value_type``), with unit weight and the fixed accuracy. An alias the
    SELECT item had is applied by the caller through the span."""
    arg = shape.sql[use.arg_start:use.arg_end]
    return digest_piece_expr_typed(arg, use.value_type)


def _span_subs(shape: Shape, instant: datetime | None, digest: bool) -> list[tuple[int, int, str]]:
    """Every ``(start, end, replacement)`` to apply to ``shape.sql``: the clock
    forms (always), and the percentile calls (only in a digest piece)."""
    subs = [(u.start, u.end, clock_literal(u, instant)) for u in shape.clock]
    if digest:
        subs += [(u.start, u.end, digest_piece_expr(shape, u)) for u in shape.percentiles]
    return subs


def _apply_subs(text: str, subs: list[tuple[int, int, str]]) -> str:
    """Apply ``(start, end, replacement)`` edits to ``text``, right to left so
    earlier offsets stay valid (the spans are into the original text and must
    not overlap)."""
    for start, end, replacement in sorted(subs, key=lambda s: -s[0]):
        text = f"{text[:start]}{replacement}{text[end:]}"
    return text


def frozen_sql(shape: Shape, instant: datetime | None, *, digest: bool = False) -> str:
    """``shape.sql`` with every clock form read at ``instant`` (and, in a
    digest piece, each ``approx_percentile`` replaced by a serialized
    ``qdigest_agg``); a shape with a clock and no instant raises (a piece never
    reads its own clock)."""
    if shape.clock and instant is None:
        raise ValueError("a query reading the current time needs one frozen instant")
    return _apply_subs(shape.sql, _span_subs(shape, instant, digest))


def pinned_sql(shape: Shape, snapshot_id: int, instant: datetime | None = None,
               *, digest: bool = False) -> str:
    """``shape.sql`` reading one Iceberg snapshot: ``FOR VERSION AS OF <id>``
    right after the table name, and the current time read at ``instant``
    (every clock form follows the table name); a digest piece also replaces
    each ``approx_percentile`` with a serialized ``qdigest_agg``; nothing else
    changed. The snapshot clause and the rewrites are applied in ONE ordered
    pass so a rewrite in the SELECT list does not shift the table-name offset."""
    if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int):
        raise ValueError("snapshot id must be an integer")
    if shape.clock and instant is None:
        raise ValueError("a query reading the current time needs one frozen instant")
    subs = _span_subs(shape, instant, digest)
    subs.append((shape.name_end, shape.name_end, f" FOR VERSION AS OF {snapshot_id}"))
    return _apply_subs(shape.sql, subs)


def piece_sql(shape: Shape, predicate: str, snapshot_id: int | None = None,
              instant: datetime | None = None, *, digest: bool = False) -> str | None:
    """The text one piece runs: the model's SQL (pinned when ``snapshot_id``
    is set, its clock read at ``instant``, each ``approx_percentile`` replaced
    by a serialized ``qdigest_agg`` when ``digest``) with ``predicate`` ANDed
    onto its WHERE, or ``None`` when the injection cannot be placed."""
    from .sql_where_inject import _inject_where_predicate
    text = (frozen_sql(shape, instant, digest=digest) if snapshot_id is None
            else pinned_sql(shape, snapshot_id, instant, digest=digest))
    return _inject_where_predicate(text, predicate)
