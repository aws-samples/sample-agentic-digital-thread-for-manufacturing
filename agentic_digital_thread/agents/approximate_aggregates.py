# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Approximate aggregates in model-written Athena SQL, and the percentile forms
this engine does not have.

Measured on an analytics deployment: asked for a discrete percentile (an
actual observed value), the system agent wrote ``PERCENTILE_DISC(p) WITHIN
GROUP (ORDER BY x)`` and ``APPROX_PERCENTILE_DISC(x, p)``, which the engine
(Trino) does not have, then fell back to ``approx_percentile`` and reported
its estimates as observed values. Nothing in the result said they were
estimates.

Two readers, both on ``athena_scope.tokenize`` (a call is a word token right
before ``(``, never a string literal, a comment, a quoted identifier, a
longer word or a qualified name; an ``uncertain`` tokenization reads nothing):

* :func:`label` gives the result label for a query that calls an approximate
  aggregate (:data:`APPROXIMATE_FUNCTIONS`): the functions it calls and, when
  the outermost SELECT list can be read (:func:`outputs`), the result columns
  that use one, directly or through one CTE or subquery it selects from.
  :func:`note` is the result's fixed note.
* :func:`refusal` gives the ``invalid_request`` for an ordered-set percentile
  (:data:`MISSING_PERCENTILES`, or ``WITHIN GROUP`` after anything but
  ``LISTAGG``), with fixed guidance naming the exact discrete form that runs
  here, per group and over all rows. A call to a function the statement
  declares itself (``USING EXTERNAL FUNCTION <name>``) is never refused;
  other missing built-ins in that statement still are.

Stdlib only, pure; no name from the SQL reaches any text.
"""
from __future__ import annotations

from .athena_scope import Token, tokenize

#: The engine's approximate aggregates and the digest functions an estimate
#: is read from. A figure one of them computes is an estimate.
APPROXIMATE_FUNCTIONS = frozenset({
    "approx_distinct", "approx_most_frequent", "approx_percentile", "approx_set",
    "numeric_histogram", "qdigest_agg", "tdigest_agg", "value_at_quantile",
    "values_at_quantiles",
})
#: Percentile functions other engines have and this one does not.
MISSING_PERCENTILES = frozenset({
    "percentile_disc", "percentile_cont", "approx_percentile_disc", "approx_percentile_cont",
    "median",
})
#: The refusal's fixed diagnostic id and error name.
DIAGNOSTIC = "percentile_function_not_in_engine"
ERROR_NAME = "FUNCTION_NOT_FOUND"
#: The exact discrete percentile ``<p>`` (a decimal fraction, 0 to 1, written
#: inside the DECIMAL literal) per group and over all rows: the row at rank
#: CEIL(n * p) in value order, clamped to the first row so p = 0 gives the
#: smallest value. The form that has run on this engine; the refusal offers
#: both verbatim.
EXACT_GROUPED_FORM = (
    "WITH ranked AS (SELECT <group>, <value>, ROW_NUMBER() OVER (PARTITION BY <group> "
    "ORDER BY <value>) AS rn, COUNT(*) OVER (PARTITION BY <group>) AS n FROM <table> "
    "WHERE <filters> AND <value> IS NOT NULL) SELECT <group>, n, MAX(CASE WHEN rn = "
    "GREATEST(1, CAST(CEIL(n * DECIMAL '<p>') AS BIGINT)) THEN <value> END) AS pct FROM "
    "ranked GROUP BY <group>, n")
EXACT_UNGROUPED_FORM = (
    "WITH ranked AS (SELECT <value>, ROW_NUMBER() OVER (ORDER BY <value>) AS rn, "
    "COUNT(*) OVER () AS n FROM <table> WHERE <filters> AND <value> IS NOT NULL) SELECT "
    "MAX(CASE WHEN rn = GREATEST(1, CAST(CEIL(n * DECIMAL '<p>') AS BIGINT)) THEN <value> "
    "END) AS pct FROM ranked")
#: The fixed guidance: the exact discrete forms, and approx_percentile as a
#: disclosed estimate only. No SQL from the query is echoed.
GUIDANCE = (
    "Refused before running: this engine has no PERCENTILE_DISC, PERCENTILE_CONT, "
    "APPROX_PERCENTILE_DISC or MEDIAN, and WITHIN GROUP is accepted only after LISTAGG. "
    "For an exact discrete percentile (the observed value: the smallest value with at "
    "least that share of the values at or below it), number the rows in value order and "
    "take the row at rank GREATEST(1, CEIL(n * p)). Write p as a decimal literal from 0 "
    "to 1, DECIMAL '0.90' for p90 (<p> below is 0.90); never write it as integer "
    "arithmetic such as 90 / 100, which truncates. Per group: "
    f"{EXACT_GROUPED_FORM}. Over all rows, drop <group>, the PARTITION BY and the GROUP BY: "
    f"{EXACT_UNGROUPED_FORM}. approx_percentile is an estimate, not an observed value: "
    "use it only when the exact form cannot run, and then say in the answer that those "
    "figures are approximate.")
#: Words that end a query's SELECT list or FROM clause at its own depth.
_FROM_END = {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH", "WINDOW",
             "QUALIFY"}
_SET_OPERATIONS = {"UNION", "INTERSECT", "EXCEPT"}
_JOIN_WORDS = {"JOIN", "CROSS", "INNER", "LEFT", "RIGHT", "FULL", "NATURAL", "LATERAL",
               "UNNEST", "TABLESAMPLE"}
#: Words that end an expression without being an alias of it.
_NOT_AN_ALIAS = {"END", "NULL", "TRUE", "FALSE", "YEAR", "MONTH", "WEEK", "DAY", "HOUR",
                 "MINUTE", "SECOND", "DESC", "ASC"}
#: How many CTE or subquery levels :func:`outputs` follows.
_MAX_LEVELS = 8


def _tokens(sql) -> list[Token] | None:
    if not isinstance(sql, str) or not sql.strip():
        return None
    tokens, uncertain = tokenize(sql)
    return None if uncertain or not tokens else tokens


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _called(tokens: list[Token], index: int) -> str:
    """The lower-cased name of the unqualified function called at ``index``,
    else ``""``."""
    token = tokens[index]
    if token.kind != "word" or index + 1 >= len(tokens) or tokens[index + 1].text != "(":
        return ""
    if index and tokens[index - 1].text == ".":
        return ""
    return token.value.lower()


def _calls(tokens: list[Token], names: frozenset) -> list[str]:
    found: list[str] = []
    for index in range(len(tokens)):
        name = _called(tokens, index)
        if name in names and name not in found:
            found.append(name)
    return found


def functions(sql) -> tuple[str, ...]:
    """The approximate aggregates ``sql`` calls, lower-cased, in order of first
    call; ``()`` for none or an uncertain tokenization."""
    tokens = _tokens(sql)
    return tuple(_calls(tokens, APPROXIMATE_FUNCTIONS)) if tokens else ()


# --------------------------------------------------------------------------- #
# Which result columns are approximate
# --------------------------------------------------------------------------- #
class _Item:
    __slots__ = ("name", "approximate")

    def __init__(self, name: str, approximate: bool):
        self.name = name
        self.approximate = approximate


def _scope_end(tokens: list[Token], start: int, depth: int) -> int:
    """The index after the last token of the query that starts at ``start``
    at ``depth``: the first token outside that depth, or a statement end."""
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.depth < depth or (token.kind == "semicolon" and token.depth == depth):
            return index
    return len(tokens)


def _first_select(tokens: list[Token], start: int, end: int, depth: int) -> int | None:
    for index in range(start, end):
        if tokens[index].depth == depth and _upper(tokens[index]) == "SELECT":
            return index
    return None


def _ctes(tokens: list[Token]) -> dict | None:
    """``{name: (body start, column names or None)}`` for the statement's own
    WITH clause; ``{}`` without one; ``None`` when it cannot be read (a
    recursive WITH, or a shape other than ``name [(cols)] AS (body)``)."""
    if not tokens or _upper(tokens[0]) != "WITH":
        return {}
    index, out = 1, {}
    if index < len(tokens) and _upper(tokens[index]) == "RECURSIVE":
        return None
    while index < len(tokens):
        name = tokens[index]
        if name.kind not in {"word", "identifier"} or name.depth != 0:
            return None
        index += 1
        columns = None
        if index < len(tokens) and tokens[index].text == "(":
            close = _close(tokens, index)
            if close is None:
                return None
            columns = [t.value.lower() for t in tokens[index + 1:close]
                       if t.kind in {"word", "identifier"}]
            index = close + 1
        if index >= len(tokens) or _upper(tokens[index]) != "AS":
            return None
        index += 1
        if index >= len(tokens) or tokens[index].text != "(":
            return None
        close = _close(tokens, index)
        if close is None:
            return None
        out[name.value.lower()] = (index + 1, columns)
        index = close + 1
        if index < len(tokens) and tokens[index].text == ",":
            index += 1
            continue
        return out
    return None


def _close(tokens: list[Token], open_index: int) -> int | None:
    depth = tokens[open_index].depth
    for index in range(open_index + 1, len(tokens)):
        if tokens[index].text == ")" and tokens[index].depth == depth:
            return index
    return None


def _split_items(tokens: list[Token], start: int, end: int, depth: int) -> list[list[Token]]:
    items, current = [], []
    for token in tokens[start:end]:
        if token.text == "," and token.depth == depth:
            items.append(current)
            current = []
        else:
            current.append(token)
    items.append(current)
    return items


def _alias(item: list[Token]) -> tuple[str, int]:
    """``(name, alias length)``: the item's alias (``AS name`` or a trailing
    implicit alias) and how many trailing tokens it takes, else the bare or
    qualified column it selects with length 0, else ``("", 0)``."""
    if len(item) >= 3 and _upper(item[-2]) == "AS" and item[-1].kind in {"word", "identifier"}:
        return item[-1].value.lower(), 2
    last = item[-1] if item else None
    if (last is not None and len(item) >= 2 and last.kind in {"word", "identifier"}
            and _upper(last) not in _NOT_AN_ALIAS
            and (item[-2].text == ")" or item[-2].kind in {"word", "identifier", "number",
                                                             "literal"})):
        return last.value.lower(), 1
    if len(item) == 1 and item[0].kind in {"word", "identifier"}:
        return item[0].value.lower(), 0
    if (len(item) == 3 and item[1].text == "." and item[2].kind in {"word", "identifier"}):
        return item[2].value.lower(), 0
    return "", 0


def _references(expression: list[Token], names: set) -> bool:
    """Whether ``expression`` reads a column named in ``names``: a word or
    quoted name that is neither a call nor a qualifier (``name.``)."""
    for index, token in enumerate(expression):
        if token.kind not in {"word", "identifier"} or token.value.lower() not in names:
            continue
        if index + 1 < len(expression) and expression[index + 1].text in {"(", "."}:
            continue
        return True
    return False


def _is_star(item: list[Token]) -> bool:
    return bool(item) and item[-1].text == "*" and (
        len(item) == 1 or (len(item) == 3 and item[1].text == "."))


def _source(tokens: list[Token], start: int, end: int, depth: int, ctes: dict,
            level: int) -> list[_Item] | None:
    """The columns of the one relation a FROM clause reads, when it is a CTE
    of the statement or a parenthesised subquery; ``None`` for a physical
    table, a join, a list or anything else."""
    clause = [t for t in tokens[start:end]]
    if not clause or any(t.depth == depth and (t.text == "," or _upper(t) in _JOIN_WORDS)
                         for t in clause):
        return None
    first = clause[0]
    if first.text == "(":
        close = _close(tokens, start)
        if close is None or close >= end:
            return None
        rest = tokens[close + 1:end]
        if len(rest) > 2 or (len(rest) == 2 and _upper(rest[0]) != "AS"):
            return None
        select = _first_select(tokens, start + 1, close, depth + 1)
        return None if select is None else _read(tokens, select, depth + 1, ctes, level + 1)
    if first.kind not in {"word", "identifier"} or len(clause) > 3:
        return None
    if len(clause) >= 2 and not (
            (len(clause) == 2 and clause[1].kind in {"word", "identifier"})
            or (len(clause) == 3 and _upper(clause[1]) == "AS")):
        return None
    found = ctes.get(first.value.lower())
    if found is None:
        return None
    body, columns = found
    select = _first_select(tokens, body, _scope_end(tokens, body, 1), 1)
    items = None if select is None else _read(tokens, select, 1, ctes, level + 1)
    if items is not None and columns:
        if len(columns) != len(items):
            return None
        items = [_Item(name, item.approximate) for name, item in zip(columns, items)]
    return items


def _read(tokens: list[Token], select: int, depth: int, ctes: dict,
          level: int) -> list[_Item] | None:
    """The output columns of the query whose SELECT is at ``select``, or
    ``None`` when its list cannot be indexed."""
    if level > _MAX_LEVELS:
        return None
    end = _scope_end(tokens, select + 1, depth)
    own = [i for i in range(select + 1, end) if tokens[i].depth == depth]
    if any(_upper(tokens[i]) in _SET_OPERATIONS for i in own):
        return None
    start = select + 1
    if start < end and _upper(tokens[start]) in {"DISTINCT", "ALL"}:
        start += 1
    from_at = next((i for i in own if _upper(tokens[i]) == "FROM"), None)
    list_end = from_at if from_at is not None else next(
        (i for i in own if _upper(tokens[i]) in _FROM_END), end)
    source = None
    if from_at is not None:
        from_end = next((i for i in own if i > from_at and _upper(tokens[i]) in _FROM_END), end)
        source = _source(tokens, from_at + 1, from_end, depth, ctes, level)
    approximate_names = {i.name for i in source or () if i.approximate and i.name}
    out: list[_Item] = []
    for item in _split_items(tokens, start, list_end, depth):
        if not item:
            return None
        if _is_star(item):
            if source is None:
                return None
            out.extend(source)
            continue
        name, alias_length = _alias(item)
        expression = item[:len(item) - alias_length]
        approximate = bool(_calls(expression, APPROXIMATE_FUNCTIONS)) or _references(
            expression, approximate_names)
        out.append(_Item(name, approximate))
    return out


def outputs(sql) -> tuple[int, ...] | None:
    """The indexes of ``sql``'s result columns whose value an approximate
    aggregate computes: an outermost SELECT item that calls one, or that
    reads such a column of the one CTE or subquery it selects from (``*``
    over it included). ``None`` when the list cannot be indexed (``*`` over a
    table or a join, a set operation, an unreadable WITH, an uncertain
    tokenization)."""
    tokens = _tokens(sql)
    if not tokens:
        return None
    ctes = _ctes(tokens)
    if ctes is None:
        return None
    select = _first_select(tokens, 0, len(tokens), 0)
    items = None if select is None else _read(tokens, select, 0, ctes, 0)
    if items is None:
        return None
    return tuple(i for i, item in enumerate(items) if item.approximate)


def label(sql) -> dict | None:
    """``{"functions": [...], "column_indexes": [...]}`` for a query that
    calls an approximate aggregate (``column_indexes`` only when
    :func:`outputs` can read the SELECT list), else ``None``."""
    found = functions(sql)
    if not found:
        return None
    out: dict = {"functions": list(found)}
    indexes = outputs(sql)
    if indexes is not None:
        out["column_indexes"] = list(indexes)
    return out


def note(found) -> str:
    """The result's fixed note for the approximate aggregates ``found`` (names
    from :data:`APPROXIMATE_FUNCTIONS` only)."""
    names = [n for n in found or () if n in APPROXIMATE_FUNCTIONS]
    return ("approximate: the figures this result computes with "
            f"{', '.join(names) or 'an approximate aggregate'} are the engine's estimates of "
            "the same measure, not exact or observed values; say they are approximate "
            "wherever the answer states them")


def joined(names) -> str:
    """Function names for prose: ``a``, ``a and b``, ``a, b and c``."""
    names = [n for n in names or () if n in APPROXIMATE_FUNCTIONS]
    if len(names) <= 1:
        return names[0] if names else "an approximate aggregate"
    return f"{', '.join(names[:-1])} and {names[-1]}"


# --------------------------------------------------------------------------- #
# Ordered-set percentiles this engine does not have
# --------------------------------------------------------------------------- #
def _declared_functions(tokens: list[Token]) -> set:
    """The lower-cased names the statement declares with ``[USING] EXTERNAL
    FUNCTION <name>(...)`` at its top level."""
    names = set()
    for index in range(len(tokens) - 2):
        if (tokens[index].depth == 0 and _upper(tokens[index]) == "EXTERNAL"
                and _upper(tokens[index + 1]) == "FUNCTION"
                and tokens[index + 2].kind == "word"):
            names.add(tokens[index + 2].value.lower())
    return names


def _within_group_after(tokens: list[Token], index: int) -> str:
    """For ``WITHIN`` at ``index`` followed by ``GROUP (``: the lower-cased
    name of the call it follows, ``""`` when it follows no call."""
    before = index - 1
    if before < 0 or tokens[before].text != ")":
        return ""
    depth = tokens[before].depth
    for open_index in range(before - 1, -1, -1):
        if tokens[open_index].text == "(" and tokens[open_index].depth == depth:
            return _called(tokens, open_index - 1) if open_index else ""
    return ""


def refused_form(sql) -> str:
    """The percentile form this engine does not have that ``sql`` uses (a
    :data:`MISSING_PERCENTILES` name the statement does not declare as its
    own external function, or ``"WITHIN GROUP"`` after anything but
    ``LISTAGG``), else ``""``. An uncertain tokenization gives ``""``."""
    tokens = _tokens(sql)
    if not tokens:
        return ""
    declared = _declared_functions(tokens)
    for index, token in enumerate(tokens):
        name = _called(tokens, index)
        if name in MISSING_PERCENTILES and name not in declared:
            return name
        if (_upper(token) == "WITHIN" and index + 2 < len(tokens)
                and _upper(tokens[index + 1]) == "GROUP" and tokens[index + 2].text == "("
                and _within_group_after(tokens, index) != "listagg"):
            return "WITHIN GROUP"
    return ""


def refusal(sql) -> dict | None:
    """The ``invalid_request`` for a query using a percentile form this engine
    does not have (:func:`refused_form`), else ``None``."""
    form = refused_form(sql)
    if not form:
        return None
    return {"error": "invalid_request", "error_type": "invalid_request",
            "error_name": ERROR_NAME, "diagnostic": DIAGNOSTIC, "refused_form": form,
            "guidance": GUIDANCE}
