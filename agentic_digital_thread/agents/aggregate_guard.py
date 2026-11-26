# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Give every SUM, COUNT or AVG computed over a JOIN a machine status.

A JOIN can match one row of the table being totaled to many rows of another
(two tables joined on a column they both carry instead of the row's own key),
and every total over it then counts those rows more than once. The result
looks normal: a few grouped rows, nothing cut. Workshop rehearsal,
2026-10-14: shipped units joined to order lines on a shared product key came
back hundreds of times too high.

Scope: a direct SQL query whose main SELECT computes SUM, COUNT or AVG (not
as a window, not inside a subquery) over a FROM with a JOIN or a comma join,
or over one derived table or CTE that joins inside. Single-table queries are
not checked and carry no status.

Each such aggregate gets one status in ``aggregate_check``:

- ``verified``: over the rows the result's figures come from, a table it
  reads has one joined row per identity, so the rows it totals are each
  counted once; COUNT(*) (and a constant argument) is checked at the
  FROM-root table's grain; a distinct count is verified by construction
  (repeated rows cannot change it);
- ``repeated``: the rows it totals repeat, so it counts some of them more
  than once; the result also carries a plain ``aggregate_note``;
- ``weighted``: an AVG of a lookup-side column whose rows repeat once per
  child row while the child rows are each counted once (an intended per-child
  weighting); informational, never withheld;
- ``unchecked``: the engine could not tell: an unreadable shape, SUM or AVG
  of DISTINCT values, a table with no measurable identity, an unqualified
  column, or a check that failed or ran past its own timeout.

The check is ONE more query on the same cursor, under its own statement
timeout (``CHECK_TIMEOUT_MS``), over the same FROM, JOIN and WHERE and, when
the query has HAVING, the same GROUP BY and HAVING (only the kept groups
count): ``COUNT(*)`` and, per FROM item, the joined rows with an identity
against the distinct identities (``verification_sql``). A table's identity is its
single-column primary key read through pass-through views (``pg_keys``); a
derived table or CTE carries its base table's key when it is a provable
pass-through, its group column when it groups one table by one column, or
its whole row when it is SELECT DISTINCT; any other relation of the schema
is identified by its whole row. Each aggregate also carries a signature
built here from its function, the columns it reads, and the query's filters,
grouping and HAVING (aliases replaced by relation names), so a later result
can verify the SAME total (``tool_result_capture``). Every status is logged
at INFO (``AGGREGATE_CHECK ...``). Never blocks: the user's rows always come
back. Uses the repo's own SQL tokenizer; no new dependency. Generic.

The same parser also tells which select-list items of a grouped query are a
COUNT(DISTINCT ...) (:func:`grouped_distinct_counts`, with
:func:`select_item_count`), for the note a result with several such rows
carries (``response_integrity_query.with_distinct_note``), and which are a
plain SUM and which a GROUP BY column of a grouped JOIN query
(:func:`plain_sum_shape`), for ``response_integrity_query.with_row_sums``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import threading
import time
from typing import Any

from .athena_scope import tokenize

logger = logging.getLogger(__name__)

AGGREGATE_CHECK_KEY = "aggregate_check"
AGGREGATE_NOTE_KEY = "aggregate_note"
VERIFIED, REPEATED, WEIGHTED, UNCHECKED = "verified", "repeated", "weighted", "unchecked"
STATUSES = (VERIFIED, REPEATED, WEIGHTED, UNCHECKED)
#: A result's overall status is its strongest aggregate status, in this order.
_PRECEDENCE = (REPEATED, UNCHECKED, WEIGHTED, VERIFIED)
#: The check's own statement timeout: a slow check cannot double the wait.
CHECK_TIMEOUT_MS = 15_000
#: Sets the session's statement_timeout (ms, a bound string); SET cannot bind.
_SET_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, false)"
#: FROM items measured in one check; aggregates reported per result.
MAX_CHECKED_SOURCES = 12
MAX_AGGREGATES = 16
_AGGREGATES = frozenset({"SUM", "COUNT", "AVG"})
_NOT_ALIAS = frozenset({
    "ON", "USING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS",
    "NATURAL", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "FETCH",
    "WINDOW", "UNION", "INTERSECT", "EXCEPT", "LATERAL", "AS", "FOR", "TABLESAMPLE",
})
_CLAUSES = frozenset({"WHERE", "GROUP", "HAVING", "WINDOW", "ORDER", "LIMIT", "OFFSET",
                      "FETCH", "FOR"})
_SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT"})
#: Words an aggregate argument may hold that are not column names.
_ARG_WORDS = frozenset({
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL", "IS", "IN",
    "BETWEEN", "LIKE", "ILIKE", "SIMILAR", "TO", "TRUE", "FALSE", "AS", "DISTINCT",
    "ALL", "INTERVAL", "FROM", "EPOCH", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE",
    "SECOND", "DOW", "DOY", "WEEK", "QUARTER", "ISODOW", "MILLISECONDS",
    "MICROSECONDS", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME",
    "LOCALTIMESTAMP", "ANY", "SOME", "ARRAY", "ESCAPE", "AT", "TIME", "ZONE",
    "WITHOUT", "WITH", "COLLATE", "UNKNOWN",
})
_LABEL_RE = re.compile(r"^[A-Za-z0-9_$.,*() ]{1,160}$")
_PLAIN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
#: How long a schema's keys are reused before they are read again.
KEY_CACHE_SECONDS = 600
_KEY_CACHE: dict = {}
_KEY_CACHE_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Source:
    """One FROM item. ``relation`` is a relation of the schema (table or
    view); ``body`` the SQL of a derived table or CTE; neither for a function
    or another schema's relation. ``sig`` names it in signatures without its
    alias (the relation name, or a hash of a derived table's or CTE's body);
    ``join`` is how it is joined (``inner``, ``left``, ``right``, ``full``);
    ``label`` names it in messages."""
    text: str
    relation: str | None
    body: str | None
    sig: str
    label: str
    join: str = "inner"


@dataclasses.dataclass(frozen=True)
class Aggregate:
    """One top-level SUM, COUNT or AVG. ``rows``: COUNT(*) or a constant
    argument. ``aliases``: FROM items its qualified columns read. ``bare``:
    it names a column it does not tie to a FROM item."""
    function: str
    distinct: bool
    rows: bool
    aliases: tuple
    bare: bool
    signature: str
    label: str
    clause: str = "select"


@dataclasses.dataclass(frozen=True)
class Plan:
    prefix: str
    from_where: str
    group_by: str
    having: str
    sources: dict
    root: str
    aggregates: tuple
    trace: tuple = ()


@dataclasses.dataclass(frozen=True)
class Unplanned:
    """An aggregate over a JOIN this module cannot read: ``unchecked``."""
    reason: str
    signature: str
    trace: tuple = ()


@dataclasses.dataclass(frozen=True)
class Grain:
    """One FROM item's measurement over the result's rows: ``own`` its rows
    with an identity are distinct identities; ``root`` every joined row is a
    distinct identity of it (the COUNT(*) grain)."""
    own: bool
    root: bool
    rows: int
    distinct: int


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def _is_name(token) -> bool:
    return token.kind in ("word", "identifier")


def _word(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _norm(token) -> str:
    return token.value.lower() if token.kind == "word" else token.value


def _close(tokens, index: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``index``."""
    depth = tokens[index].depth
    for i in range(index + 1, len(tokens)):
        if tokens[i].text == ")" and tokens[i].depth == depth:
            return i
    return None


def _tokens(sql: Any):
    if not isinstance(sql, str) or not sql.strip():
        return None
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return None
    while tokens and tokens[-1].kind == "semicolon":
        tokens = tokens[:-1]
    if not tokens or any(t.kind == "semicolon" for t in tokens):
        return None
    return tokens


def _text(sql: str, tokens) -> str:
    return sql[tokens[0].start:tokens[-1].end] if tokens else ""


def _split(tokens) -> list[list]:
    """``tokens`` split at commas of their own depth."""
    if not tokens:
        return []
    depth, parts = tokens[0].depth, [[]]
    for token in tokens:
        if token.text == "," and token.depth == depth:
            parts.append([])
        else:
            parts[-1].append(token)
    return parts


def _looks_in_scope(sql: Any) -> bool:
    """For SQL this module cannot read: an aggregate call and a JOIN appear."""
    return (isinstance(sql, str) and re.search(r"(?i)\b(sum|count|avg)\s*\(", sql) is not None
            and re.search(r"(?i)\bjoin\b", sql) is not None)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _unplanned(sql: Any, reason: str) -> Unplanned | None:
    if not _looks_in_scope(sql):
        return None
    return Unplanned(reason, _hash("unplanned", " ".join(str(sql).lower().split())))


# --------------------------------------------------------------------------- #
# The main query's shape
# --------------------------------------------------------------------------- #
def _cte_body(tokens, j: int, sql: str):
    """``(body or None, index after it)`` for the CTE definition from ``j``
    (after its name), or None when it cannot be read. A column list renames
    the body's columns, so such a CTE has no readable body."""
    renamed = j < len(tokens) and tokens[j].text == "("
    if renamed:
        end = _close(tokens, j)
        if end is None:
            return None
        j = end + 1
    if j < len(tokens) and _word(tokens[j]) == "AS":
        j += 1
    while j < len(tokens) and _word(tokens[j]) in ("MATERIALIZED", "NOT"):
        j += 1
    if j >= len(tokens) or tokens[j].text != "(":
        return None
    end = _close(tokens, j)
    if end is None:
        return None
    body = None if renamed or end == j + 1 else _text(sql, tokens[j + 1:end])
    return body, end + 1


def _with_clause(tokens, sql: str):
    """``(index of the main SELECT, {cte name: body or None})`` or None."""
    if _word(tokens[0]) == "SELECT":
        return 0, {}
    if _word(tokens[0]) != "WITH":
        return None
    ctes: dict = {}
    i = 2 if len(tokens) > 1 and _word(tokens[1]) == "RECURSIVE" else 1
    while i < len(tokens):
        token = tokens[i]
        if token.depth == 0 and _word(token) == "SELECT":
            return i, ctes
        if token.depth == 0 and _is_name(token):
            found = _cte_body(tokens, i + 1, sql)
            if found is None:
                return None
            ctes[_norm(token)], i = found
            continue
        i += 1
    return None


def _clause_spans(tokens, start: int) -> dict | None:
    """``{clause: (first, end)}`` token spans of the main query from its
    SELECT: ``select``, ``from`` and, when present, ``where``, ``group`` and
    ``having``. None for a set operation or a query with no FROM."""
    marks: list[tuple[str, int]] = [("select", start)]
    for i in range(start + 1, len(tokens)):
        token = tokens[i]
        if token.depth != 0:
            continue
        word = _word(token)
        if word in _SET_OPERATORS:
            return None
        if word == "FROM" and not any(name == "from" for name, _ in marks):
            marks.append(("from", i))
        elif word in _CLAUSES and len(marks) > 1:
            marks.append((word.lower(), i))
    if len(marks) < 2 or marks[1][0] != "from":
        return None
    spans = {}
    for index, (name, first) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(tokens)
        spans.setdefault(name, (first, end))
    return spans


def _body_sig(body: str | None) -> str:
    """A derived table's or CTE's signature name: a hash of its text with
    words lowercased and spacing normalized, so its alias does not count."""
    tokens = _tokens(body)
    text = (" ".join(_norm(t) if _is_name(t) else t.text for t in tokens) if tokens
            else " ".join(str(body or "").lower().split()))
    return "~" + _hash("body", text)


def _source_item(tokens, i: int, sql: str, schema: str, ctes: dict):
    """``((relation, body, signature name, display name or None), index after
    it, default name token)`` for the FROM item at ``i``, or None."""
    token = tokens[i]
    if token.text == "(":
        end = _close(tokens, i)
        if end is None:
            return None
        body = _text(sql, tokens[i + 1:end]) or None
        return (None, body, _body_sig(body), None), end + 1, None
    if not _is_name(token) or _word(token) in _NOT_ALIAS:
        return None
    parts, j = [token], i + 1
    if j + 1 < len(tokens) and tokens[j].text == "." and _is_name(tokens[j + 1]):
        parts.append(tokens[j + 1])
        j += 2
    qualified = ".".join(_norm(p) for p in parts)
    if j < len(tokens) and tokens[j].text == "(":            # a function in FROM
        end = _close(tokens, j)
        if end is None:
            return None
        return (None, None, "fn:" + qualified, parts[-1].value), end + 1, parts[-1]
    name = _norm(parts[-1])
    if len(parts) == 2 and _norm(parts[0]) != (schema or "").lower():
        return (None, None, qualified, parts[-1].value), j, parts[-1]
    if len(parts) == 1 and name in ctes:
        body = ctes[name]
        sig = _body_sig(body) if body else "cte:" + name
        return (None, body, sig, parts[-1].value), j, parts[-1]
    return (name, None, name, name), j, parts[-1]


#: Words before JOIN that say how a FROM item is joined.
_JOIN_WORDS = frozenset({"INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL"})


def _join_kind(tokens, i: int) -> str:
    """How the JOIN at ``tokens[i]`` joins: ``left``, ``right`` or ``full``
    (``natural`` prefixed when NATURAL), else ``inner``. A comma or CROSS
    JOIN is an inner join filtered by WHERE, which the signature carries."""
    words: set[str] = set()
    j = i - 1
    while j >= 0 and tokens[j].depth == tokens[i].depth and _word(tokens[j]) in _JOIN_WORDS:
        words.add(_word(tokens[j]))
        j -= 1
    kind = next((w.lower() for w in ("LEFT", "RIGHT", "FULL") if w in words), "inner")
    return f"natural {kind}" if "NATURAL" in words else kind


def _sources(tokens, sql: str, schema: str, ctes: dict) -> dict | None:
    """``{alias: Source}`` in FROM order from the FROM clause's tokens."""
    out: dict = {}
    expect, i, kind = True, 0, "inner"
    while i < len(tokens):
        token, word = tokens[i], _word(tokens[i])
        if token.depth != tokens[0].depth and token.text not in "()":
            i += 1
        elif word == "JOIN" or token.text == ",":
            kind = _join_kind(tokens, i) if word == "JOIN" else "inner"
            expect, i = True, i + 1
        elif not expect or word in ("LATERAL", "ONLY"):
            i += 1
        else:
            item = _source_item(tokens, i, sql, schema, ctes)
            if item is None:
                return None
            (relation, body, sig, display), at, name = item
            if at < len(tokens) and _word(tokens[at]) == "AS":
                at += 1
            if at < len(tokens) and _is_name(tokens[at]) and _word(tokens[at]) not in _NOT_ALIAS:
                name, at = tokens[at], at + 1
            if name is None:
                return None                                   # a derived table needs an alias
            out[_norm(name)] = Source(name.text, relation, body, sig, display or name.value,
                                      kind)
            expect, i = False, at
    return out or None


# --------------------------------------------------------------------------- #
# Aggregates and their signatures
# --------------------------------------------------------------------------- #
def _normalized(tokens, sources: dict) -> str:
    """Tokens as one string, words lowercased, ``alias.column`` written with
    the FROM item's signature name, so a renamed alias reads the same."""
    out, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if (_is_name(token) and i + 2 < len(tokens) and tokens[i + 1].text == "."
                and _is_name(tokens[i + 2]) and (i == 0 or tokens[i - 1].text != ".")
                and _norm(token) in sources):
            out.append(f"{sources[_norm(token)].sig}.{_norm(tokens[i + 2])}")
            i += 3
            continue
        out.append(_norm(token) if _is_name(token) else token.text)
        i += 1
    return " ".join(out)


def _select_items(select_tokens, sql: str) -> list[tuple[str, str | None]]:
    """``(expression text, output name or None)`` per select-list entry."""
    items = []
    for part in _split(select_tokens):
        if not part:
            continue
        if len(part) >= 3 and _word(part[-2]) == "AS" and _is_name(part[-1]):
            items.append((_text(sql, part[:-2]), part[-1].value))
        elif (len(part) >= 2 and _is_name(part[-1]) and part[-2].text not in (".", ":")
              and _word(part[-1]) not in ("END", "NULL", "TRUE", "FALSE")):
            items.append((_text(sql, part[:-1]), part[-1].value))
        else:
            items.append((_text(sql, part), None))
    return items


def _group_terms(tokens, items, sql: str) -> list[str] | None:
    """GROUP BY terms as text, with select-list ordinals and output names
    replaced by their expressions (the check has its own select list)."""
    if len(tokens) < 2 or _word(tokens[1]) != "BY":
        return None
    terms = []
    for part in _split(tokens[2:]):
        if not part:
            return None
        text = _text(sql, part)
        if len(part) == 1 and part[0].kind == "number" and part[0].text.isdigit():
            index = int(part[0].text) - 1
            if not 0 <= index < len(items):
                return None
            text = items[index][0]
        elif len(part) == 1 and _is_name(part[0]):
            named = [expr for expr, out in items if out == part[0].value and expr != text]
            text = named[0] if len(named) == 1 else text
        terms.append(text)
    return terms


def _subquery_spans(tokens) -> list[tuple[int, int]]:
    spans = []
    for i, token in enumerate(tokens):
        if token.text == "(" and i + 1 < len(tokens) and _word(tokens[i + 1]) in ("SELECT", "WITH"):
            end = _close(tokens, i)
            spans.append((i, end if end is not None else len(tokens)))
    return spans


def _aggregate_calls(select_tokens) -> list[tuple[str, list, list]] | None:
    """``(function, argument tokens, FILTER tokens)`` per top-level
    SUM/COUNT/AVG call of a clause (not a window, not in a subquery)."""
    spans = _subquery_spans(select_tokens)
    calls = []
    for i, token in enumerate(select_tokens):
        word = _word(token)
        if word not in _AGGREGATES or i + 1 >= len(select_tokens):
            continue
        if select_tokens[i + 1].text != "(" or any(s < i <= e for s, e in spans):
            continue
        end = _close(select_tokens, i + 1)
        if end is None:
            return None
        after = select_tokens[end + 1:]
        if after and _word(after[0]) == "OVER":
            continue
        filtered: list = []
        if len(after) >= 2 and _word(after[0]) == "FILTER" and after[1].text == "(":
            close = _close(select_tokens, end + 2)
            filtered = select_tokens[end + 1:(close or end + 2) + 1]
        calls.append((word, select_tokens[i + 2:end], filtered))
    return calls


def _subquery_aggregates(tokens, spans: dict) -> list[tuple[str, str]]:
    """``(function, normalized subquery text)`` per SUM/COUNT/AVG call inside
    a scalar subquery of the select list, WHERE, HAVING or ORDER BY. Their
    totals are recorded as ``unchecked`` for the trace only."""
    found = []
    for name in ("select", "where", "having", "order"):
        if name not in spans:
            continue
        part = _clause(tokens, spans, name)
        for start, end in _subquery_spans(part):
            inner = part[start:end + 1]
            text = " ".join(_norm(t) if _is_name(t) else t.text for t in inner)
            for k, token in enumerate(inner[:-1]):
                if _word(token) in _AGGREGATES and inner[k + 1].text == "(":
                    found.append((_word(token), f"{k}:{text}"))
    return list(dict.fromkeys(found))


def _argument(arg, sources: dict):
    """``(distinct, rows, aliases, refs, bare)`` for an aggregate argument;
    ``refs`` are ``(alias, column)`` pairs of its qualified columns."""
    distinct = bool(arg) and _word(arg[0]) == "DISTINCT"
    if arg and _word(arg[0]) in ("DISTINCT", "ALL"):
        arg = arg[1:]
    refs, bare, skip_type = [], False, False
    for k, token in enumerate(arg):
        if token.text == ":":
            skip_type = True
            continue
        if not _is_name(token):
            skip_type = False if token.text not in ("(", ")", ",") else skip_type
            continue
        if skip_type or (k > 0 and arg[k - 1].text == "."):
            continue
        if k + 2 < len(arg) and arg[k + 1].text == "." and _is_name(arg[k + 2]):
            if _norm(token) in sources:
                refs.append((_norm(token), arg[k + 2].value))
            else:
                bare = True
            continue
        if (k + 1 < len(arg) and arg[k + 1].text == "(") or _word(token) in _ARG_WORDS:
            continue
        if k > 0 and _word(arg[k - 1]) == "AS":
            continue
        bare = True
    rows = (not refs and not bare and all(t.kind in ("number", "literal") or t.text in "*()"
                                          for t in arg))
    if any(_word(t) == "SELECT" for t in arg):
        bare = True
    aliases = tuple(dict.fromkeys(alias for alias, _col in refs))
    return distinct, rows, aliases, refs, bare


def _aggregate_label(function: str, distinct: bool, rows: bool, refs, root: Source) -> str:
    if rows:
        return f"{function}(*) over {root.label} rows" if _PLAIN_RE.match(root.label) else (
            f"{function}(*) over the root rows")
    head = f"{function}(DISTINCT)" if distinct else function
    shown = sorted({f"{label}.{column}" for label, column in refs
                    if _PLAIN_RE.match(label) and _PLAIN_RE.match(column)})
    label = f"{head} over {', '.join(shown)}" if shown else f"{head} over an expression"
    return label if _LABEL_RE.match(label) else f"{head} over an expression"


def _aggregates(calls, sources: dict, root: str, scope: str) -> tuple:
    """One Aggregate per distinct signature, the projected one first: the
    same total in the select list and in ORDER BY or HAVING is one entry."""
    out, seen = [], set()
    for function, arg, filtered, clause in calls:
        distinct, rows, aliases, refs, bare = _argument(arg, sources)
        refs_labels = [(sources[a].label, col) for a, col in refs]
        signature = _hash(function, "distinct" if distinct else "",
                          _normalized(arg, sources), _normalized(filtered, sources), scope)
        if signature in seen or len(out) >= MAX_AGGREGATES:
            continue
        seen.add(signature)
        label = _aggregate_label(function, distinct, rows, refs_labels, sources[root])
        if clause != "select":
            label = f"{label} in {clause.upper()}"
        out.append(Aggregate(function, distinct, rows, aliases, bare, signature,
                             label if _LABEL_RE.match(label) else f"{function} in a clause",
                             clause))
    return tuple(out)


def _trace(subqueries: list[tuple[str, str]]) -> tuple:
    """``(function, signature, label)`` per aggregate in a scalar subquery."""
    return tuple((function, _hash("subquery", function, text), f"{function} in a subquery")
                 for function, text in subqueries[:MAX_AGGREGATES])


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
def _has_join(sql: str | None) -> bool:
    tokens = _tokens(sql) if sql else None
    if tokens is None:
        return bool(sql) and re.search(r"(?i)\bjoin\b", sql) is not None
    return any(_word(t) == "JOIN" or (t.text == "," and t.depth == tokens[0].depth)
               for t in tokens)


def _without_quantifier(select_tokens):
    """The select list without a leading ALL, DISTINCT or DISTINCT ON (...)."""
    if select_tokens and _word(select_tokens[0]) == "ALL":
        return select_tokens[1:]
    if not select_tokens or _word(select_tokens[0]) != "DISTINCT":
        return select_tokens
    if len(select_tokens) > 2 and _word(select_tokens[1]) == "ON" and select_tokens[2].text == "(":
        end = _close(select_tokens, 2)
        return select_tokens[(end or 2) + 1:]
    return select_tokens[1:]


def _clause(tokens, spans: dict, name: str):
    first, end = spans.get(name, (0, 0))
    return tokens[first:end]


def parse(sql: Any, schema: str = "") -> Plan | Unplanned | None:
    """The check a direct SQL query needs: a Plan, an Unplanned (an aggregate
    over a JOIN that cannot be read), or None (nothing to check)."""
    tokens = _tokens(sql)
    head = _with_clause(tokens, sql) if tokens else None
    spans = _clause_spans(tokens, head[0]) if head else None
    if spans is None:
        return _unplanned(sql, "shape")
    start, ctes = head
    select_tokens = _without_quantifier(tokens[start + 1:spans["from"][0]])
    from_tokens = _clause(tokens, spans, "from")[1:]
    sources = _sources(from_tokens, sql, schema, ctes)
    calls = _clause_calls(tokens, spans, select_tokens)
    if sources is None or calls is None:
        return _unplanned(sql, "shape")
    trace = _trace(_subquery_aggregates(tokens, spans)) if _looks_in_scope(sql) else ()
    if not calls and not trace:
        return None
    joined = any((_word(t) == "JOIN" or t.text == ",") and t.depth == 0 for t in from_tokens)
    if not joined:
        only = next(iter(sources.values()))
        if calls and _has_join(only.body):
            found = _unplanned(sql, "join inside a derived table")
            return dataclasses.replace(found, trace=trace) if found else None
        return Plan("", "", "", "", {}, "", (), trace) if trace else None
    return _plan(sql, tokens, spans, select_tokens, sources, calls, trace)


def _main_select(sql: Any):
    """``(tokens, clause spans, select-list tokens, {cte name: body})`` of the
    main query, or None for SQL :func:`parse` cannot read or a set operation."""
    tokens = _tokens(sql)
    head = _with_clause(tokens, sql) if tokens else None
    spans = _clause_spans(tokens, head[0]) if head else None
    if spans is None:
        return None
    return (tokens, spans, _without_quantifier(tokens[head[0] + 1:spans["from"][0]]),
            head[1])


def select_item_count(sql: Any) -> int | None:
    """How many items the main query's select list has, or None when it
    cannot be read. Pure."""
    found = _main_select(sql)
    return None if found is None else len(_split(found[2]))


def _unwrapped(tokens):
    """``tokens`` without the parentheses that only wrap all of them."""
    while len(tokens) > 2 and tokens[0].text == "(" and _close(tokens, 0) == len(tokens) - 1:
        tokens = tokens[1:-1]
    return tokens


def _output_name(part) -> tuple[list, str | None]:
    """``(expression tokens, output name or None)`` of one select-list entry
    (read as :func:`_select_items` reads it), the name as SQL compares it:
    unquoted folded to lower case, quoted exact (:func:`_norm`)."""
    if len(part) >= 3 and _word(part[-2]) == "AS" and _is_name(part[-1]):
        return part[:-2], _norm(part[-1])
    if (len(part) >= 2 and _is_name(part[-1]) and part[-2].text not in (".", ":")
            and _word(part[-1]) not in ("END", "NULL", "TRUE", "FALSE")):
        return part[:-1], _norm(part[-1])
    return part, None


def _comparable(tokens, sources: dict) -> str:
    """An expression as SQL resolves its names, for telling whether two are
    the same: parentheses around all of it dropped, unquoted names folded and
    quoted ones exact (:func:`_norm`), and each column reference written
    through its FROM item: ``alias.column`` by the alias, a bare column by
    the one FROM item when there is only one. With several FROM items a bare
    column stays bare: which item holds it needs the schema."""
    tokens = _unwrapped(tokens)
    only = next(iter(sources)) if len(sources) == 1 else None
    out, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        after_dot = i > 0 and tokens[i - 1].text == "."
        if (_is_name(token) and not after_dot and i + 2 < len(tokens)
                and tokens[i + 1].text == "." and _is_name(tokens[i + 2])
                and _norm(token) in sources):
            out.append(f"{_norm(token)}.{_norm(tokens[i + 2])}")
            i += 3
            continue
        if (_is_name(token) and only is not None and not after_dot
                and not (i + 1 < len(tokens) and tokens[i + 1].text in (".", "("))
                and _word(token) not in _ARG_WORDS):
            out.append(f"{only}.{_norm(token)}")
        else:
            out.append(_norm(token) if _is_name(token) else token.text)
        i += 1
    return " ".join(out)


def _sole_group_term(group, parts: list, sources: dict) -> str | None:
    """The one GROUP BY term as :func:`_comparable` writes it, an ordinal
    replaced by its select-list expression; None when there are several
    terms, a parenthesized list of them included, or none. A name is read as
    an input column, never as an output name: an input column of that name
    cannot be ruled out without the schema, and PostgreSQL reads the input
    column first, so taking the output name's expression could leave out a
    note the rows need. An output name that means the same column reads the
    same either way."""
    terms = _split(group[2:])
    term = _unwrapped(terms[0]) if len(terms) == 1 else []
    if not term or any(t.text == "," and t.depth == term[0].depth for t in term):
        return None
    if len(term) == 1 and term[0].kind == "number" and term[0].text.isdigit():
        index = int(term[0].text) - 1
        if not 0 <= index < len(parts):
            return None
        term = _output_name(parts[index])[0]
    return _comparable(term, sources) if term else None


def grouped_distinct_counts(sql: Any) -> tuple[int, ...]:
    """Indexes in the main query's select list of the items computed with a
    top-level COUNT(DISTINCT ...) (not a window, not inside a subquery) when
    the main query has a GROUP BY; ``()`` for anything else, SQL this module
    cannot read and a set operation included. Each such item counts distinct
    values inside its own group, and one value can be counted in several
    groups, so its rows need not add up
    (``response_integrity_query.with_distinct_note``). A distinct count of
    the one GROUP BY term itself is left out: each value is its own group,
    so those rows do add up. The two are compared as SQL resolves them
    (:func:`_sole_group_term`, :func:`_comparable`): aliases, qualified and
    bare columns of the main query's FROM items, folded unquoted names and
    redundant parentheses. Pure; read with the same parser as :func:`parse`,
    JOIN or not."""
    found = _main_select(sql)
    if found is None:
        return ()
    tokens, spans, select_tokens, ctes = found
    if "group" not in spans:
        return ()
    group = _clause(tokens, spans, "group")
    if len(group) < 3 or _word(group[1]) != "BY":
        return ()
    parts = _split(select_tokens)
    try:
        sources = _sources(_clause(tokens, spans, "from")[1:], sql, "", ctes) or {}
    except Exception:  # noqa: BLE001 - an unreadable FROM resolves no column
        sources = {}
    sole = _sole_group_term(group, parts, sources)
    items = []
    for index, part in enumerate(parts):
        calls = _aggregate_calls(part) if part else []
        if calls is None:
            return ()
        if any(function == "COUNT" and arg and _word(arg[0]) == "DISTINCT"
               and _comparable(arg[1:], sources) != sole
               for function, arg, _filtered in calls):
            items.append(index)
    return tuple(items)


#: Words in a GROUP BY after which the rows are not disjoint groups (grouping
#: sets add subtotal rows) or groups may be merged (GROUP BY DISTINCT / ALL).
_NOT_PLAIN_GROUPING = frozenset({"ROLLUP", "CUBE", "GROUPING", "DISTINCT", "ALL"})


def _plain_sum(tokens) -> bool:
    """The whole item is ``SUM(expr)``: no DISTINCT or ALL, no FILTER, no
    window, nothing around it."""
    return (len(tokens) >= 4 and _word(tokens[0]) == "SUM" and tokens[1].text == "("
            and _close(tokens, 1) == len(tokens) - 1
            and _word(tokens[2]) not in ("DISTINCT", "ALL"))


def _column_reference(tokens) -> bool:
    """``c``, ``t.c`` or ``s.t.c``."""
    return (bool(tokens) and len(tokens) % 2 == 1 and len(tokens) <= 5
            and all(_is_name(t) if i % 2 == 0 else t.text == "." for i, t in enumerate(tokens)))


def plain_sum_shape(sql: Any) -> tuple[int, tuple[int, ...], tuple[int, ...], int] | None:
    """``(select items, plain SUM item indexes, GROUP BY column item indexes,
    SUM calls in the select list)`` of a grouped JOIN query whose groups are
    disjoint and all returned, else ``None``
    (``response_integrity_query.with_row_sums``).

    A plain SUM item is exactly ``SUM(expr)`` (:func:`_plain_sum`). A GROUP BY
    column item is a column reference that is one of the GROUP BY terms, an
    ordinal resolved. The SUM calls are counted as :func:`check` reads them
    (:func:`_aggregate_calls`, windows left out), so a caller can tell whether
    the check recorded one entry for each (it keeps at most
    :data:`MAX_AGGREGATES`). ``None`` for SQL this module cannot read, a set
    operation, no JOIN in the main FROM, no GROUP BY, HAVING, LIMIT, OFFSET,
    FETCH, SELECT DISTINCT, ROLLUP, CUBE, GROUPING SETS, GROUP BY DISTINCT or
    ALL, or a subquery in the select list. Pure."""
    found = _main_select(sql)
    if found is None:
        return None
    tokens, spans, select_tokens, ctes = found
    from_tokens = _clause(tokens, spans, "from")[1:]
    group = _clause(tokens, spans, "group")
    head = spans["select"][0] + 1
    if (not any(_word(t) == "JOIN" or (t.text == "," and t.depth == 0) for t in from_tokens)
            or any(name in spans for name in ("having", "limit", "offset", "fetch"))
            or len(group) < 3 or _word(group[1]) != "BY"
            or any(_word(t) in _NOT_PLAIN_GROUPING for t in group[2:])
            or (head < len(tokens) and _word(tokens[head]) == "DISTINCT")
            or _subquery_spans(select_tokens)):
        return None
    parts, calls = _split(select_tokens), _aggregate_calls(select_tokens)
    if not parts or not all(parts) or calls is None:
        return None
    try:
        sources = _sources(from_tokens, sql, "", ctes) or {}
    except Exception:  # noqa: BLE001 - an unreadable FROM resolves no column
        sources = {}
    exprs = [_unwrapped(_output_name(part)[0]) for part in parts]
    terms = set()
    for term in _split(group[2:]):
        term = _unwrapped(term)
        if len(term) == 1 and term[0].kind == "number" and term[0].text.isdigit():
            if not 0 < int(term[0].text) <= len(exprs):
                return None
            term = exprs[int(term[0].text) - 1]
        terms.add(_comparable(term, sources))
    sums = tuple(i for i, expr in enumerate(exprs) if _plain_sum(expr))
    keys = tuple(i for i, expr in enumerate(exprs)
                 if _column_reference(expr) and _comparable(expr, sources) in terms)
    return len(parts), sums, keys, sum(function == "SUM" for function, _arg, _filter in calls)


def _clause_calls(tokens, spans: dict, select_tokens) -> list | None:
    """``(function, argument, FILTER, clause)`` per top-level aggregate of
    the select list, HAVING and ORDER BY (the same FROM, so the same check)."""
    out = []
    for clause, part in (("select", select_tokens),
                         ("having", _clause(tokens, spans, "having")),
                         ("order by", _clause(tokens, spans, "order"))):
        calls = _aggregate_calls(part) if part else []
        if calls is None:
            return None
        out += [(function, arg, filtered, clause) for function, arg, filtered in calls]
    return out


def _plan(sql, tokens, spans, select_tokens, sources, calls, trace) -> Plan | Unplanned:
    items = _select_items(select_tokens, sql)
    group = _clause(tokens, spans, "group")
    terms = _group_terms(group, items, sql) if group else []
    if terms is None:
        return _unplanned(sql, "shape")
    where = _clause(tokens, spans, "where")
    having = _clause(tokens, spans, "having")
    from_end = spans["where"][1] if "where" in spans else spans["from"][1]
    from_where = _text(sql, tokens[spans["from"][0]:from_end])
    group_norm = sorted(_normalized(tokenize(term)[0], sources) for term in terms)
    # The source set and join types, not the ON predicates: correcting the
    # join key on the same tables is the same total; another table is not.
    source_set = sorted(f"{source.join}:{source.sig}" for source in sources.values())
    scope = "|".join([",".join(source_set), _normalized(where, sources),
                      ",".join(group_norm), _normalized(having, sources)])
    root = next(iter(sources))
    return Plan(prefix=sql[:tokens[0].start] + _text(sql, tokens[:spans["select"][0]])
                + (" " if spans["select"][0] else ""),
                from_where=from_where,
                group_by=f"GROUP BY {', '.join(terms)}" if terms else "",
                having=_text(sql, having), sources=sources, root=root,
                aggregates=_aggregates(calls, sources, root, scope), trace=trace)


# --------------------------------------------------------------------------- #
# Identities
# --------------------------------------------------------------------------- #
def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _body_relation(tokens) -> tuple | None:
    """``(schema or None, table)`` names of a body's one FROM relation, with
    nothing else in its FROM clause, or None."""
    froms = [i for i, t in enumerate(tokens) if t.depth == 0 and _word(t) == "FROM"]
    if len(froms) != 1:
        return None
    end = next((i for i in range(froms[0] + 1, len(tokens))
                if tokens[i].depth == 0 and _word(tokens[i]) in _CLAUSES), len(tokens))
    part = tokens[froms[0] + 1:end]
    if part and _word(part[0]) == "ONLY":
        part = part[1:]
    if any(_word(t) == "JOIN" or t.text in (",", "(") for t in part) or not part:
        return None
    if not _is_name(part[0]):
        return None
    if len(part) >= 3 and part[1].text == ".":
        return part[0].value, part[2].value
    return None, part[0].value


def _grouped_identity(tokens, sql: str) -> str | None:
    """The output name of a body's one GROUP BY column, when it selects it."""
    groups = [i for i, t in enumerate(tokens) if t.depth == 0 and _word(t) == "GROUP"]
    froms = [i for i, t in enumerate(tokens) if t.depth == 0 and _word(t) == "FROM"]
    if len(groups) != 1 or len(froms) != 1:
        return None
    end = next((i for i in range(groups[0] + 1, len(tokens))
                if tokens[i].depth == 0 and _word(tokens[i]) in _CLAUSES - {"GROUP"}),
               len(tokens))
    terms = _split(tokens[groups[0] + 2:end])
    if len(terms) != 1 or not 1 <= len(terms[0]) <= 3 or not _is_name(terms[0][-1]):
        return None
    column = terms[0][-1].value
    for part in _split(tokens[1:froms[0]]):
        core, out = part, None
        if len(part) >= 3 and _word(part[-2]) == "AS" and _is_name(part[-1]):
            core, out = part[:-2], part[-1].value
        if core and _is_name(core[-1]) and core[-1].value == column and len(core) in (1, 3):
            return out or column
    return None


def derived_identity(body: str | None, schema: str, keys: dict) -> tuple | None:
    """``("key", column)`` or ``("row", "")`` for a derived table or CTE body
    whose rows are provably one per identity, else None (see the module
    docstring for the shapes)."""
    from .view_lineage import pass_through_columns
    tokens = _tokens(body)
    if not tokens or _word(tokens[0]) != "SELECT":
        return None
    if any(t.depth == 0 and _word(t) in _SET_OPERATORS for t in tokens):
        return None
    if len(tokens) > 2 and _word(tokens[1]) == "DISTINCT" and _word(tokens[2]) != "ON":
        return "row", ""
    relation = _body_relation(tokens)
    if relation is None or (relation[0] and relation[0].lower() != (schema or "").lower()):
        return None
    table = relation[1].lower()
    if any(t.depth == 0 and _word(t) == "GROUP" for t in tokens):
        column = _grouped_identity(tokens, body)
        return ("key", column) if column else None
    froms = [i for i, t in enumerate(tokens) if t.depth == 0 and _word(t) == "FROM"]
    select = [p for p in _split(tokens[1:froms[0]])]
    key = keys.get(table)
    if len(select) == 1 and select[0] and select[0][-1].text == "*":
        return ("key", key) if key else ("row", "")
    names = [out or "?column?" for _expr, out in _select_items(tokens[1:froms[0]], body)]
    names = [n if n != "?column?" else _bare(part) for n, part in zip(names, select)]
    proven = pass_through_columns(body, names, relation[0] or schema, relation[1])
    return ("key", key) if key and key in proven else None


def _bare(part) -> str:
    return part[-1].value if part and _is_name(part[-1]) else "?column?"


def identities(plan: Plan, keys: dict, schema: str) -> dict[str, tuple[str, str]]:
    """``{alias: (identity SQL, key name or "" for a whole row)}`` for the
    FROM items the check can measure, at most ``MAX_CHECKED_SOURCES``."""
    out: dict = {}
    for alias, source in plan.sources.items():
        if len(out) >= MAX_CHECKED_SOURCES:
            break
        if source.relation:
            key = keys.get(source.relation)
            out[alias] = (f"{source.text}.{_quote(key)}", key) if key else (source.text, "")
        elif source.body:
            found = derived_identity(source.body, schema, keys)
            if found:
                kind, column = found
                out[alias] = (f"{source.text}.{_quote(column)}", column) if kind == "key" else (
                    source.text, "")
    return out


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #
def verification_sql(plan: Plan, idents: dict) -> tuple[str, list[str]] | None:
    """The one check query for ``plan`` and the aliases it measures, in order.

    Its one row is ``(groups, joined rows, then per alias: rows with an
    identity, distinct identities)`` over exactly the rows the result's
    figures come from. Without HAVING every group is returned, so that is
    every row of the FROM, JOIN and WHERE (grouping does not change which rows
    count). With HAVING only the groups it keeps count: the check groups and
    filters the same way, collects each kept group's distinct identities and
    counts them across the kept groups, so a row counted in two returned
    groups is a repeat too."""
    aliases = [alias for alias in plan.sources if alias in idents]
    if not aliases:
        return None
    exprs = [idents[alias][0] for alias in aliases]
    if not plan.having:
        cols = ["1 AS adt_groups", "COUNT(*) AS adt_n"]
        for i, expr in enumerate(exprs):
            cols += [f"COUNT({expr}) AS adt_c{i}", f"COUNT(DISTINCT {expr}) AS adt_d{i}"]
        return f"{plan.prefix}SELECT {', '.join(cols)} {plan.from_where}", aliases
    inner = ["COUNT(*) AS adt_n"]
    outer = ["(SELECT COUNT(*) FROM adt_kept)",
             "COALESCE((SELECT SUM(adt_n) FROM adt_kept), 0)::bigint"]
    for i, expr in enumerate(exprs):
        inner += [f"COUNT({expr}) AS adt_c{i}", f"array_agg(DISTINCT {expr}) AS adt_k{i}"]
        outer += [f"COALESCE((SELECT SUM(adt_c{i}) FROM adt_kept), 0)::bigint",  # nosec B608 - i is an enumerate() int; the check SQL passes ensure_select_only and runs on a read-only session
                  f"(SELECT COUNT(DISTINCT k) FROM adt_kept, unnest(adt_k{i}) AS k)"]  # nosec B608 - same: only the int i is interpolated
    kept = " ".join(part for part in (f"SELECT {', '.join(inner)}", plan.from_where,
                                      plan.group_by, plan.having) if part)
    head = f"{plan.prefix.rstrip()}, " if plan.prefix.strip() else "WITH "
    return f"{head}adt_kept AS ({kept}) SELECT {', '.join(outer)}", aliases


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _grains(row: Any, aliases: list[str]) -> tuple[int, dict] | None:
    """``(joined rows, {alias: Grain})`` from the check's one row, or None."""
    if not isinstance(row, (list, tuple)) or len(row) != 2 + 2 * len(aliases):
        return None
    counts = [_count(value) for value in row]
    if any(value is None for value in counts):
        return None
    total, out = counts[1], {}
    for i, alias in enumerate(aliases):
        rows, distinct = counts[2 + 2 * i], counts[3 + 2 * i]
        if distinct > rows or rows > total:
            return None
        out[alias] = Grain(own=rows == distinct, root=total == distinct, rows=rows,
                           distinct=distinct)
    return total, out


def _fact(plan: Plan, idents: dict, alias: str, rows: int, distinct: int) -> dict | None:
    table, key = plan.sources[alias].label, idents[alias][1]
    if not (_PLAIN_RE.match(table) and (key == "" or _PLAIN_RE.match(key))):
        return None
    if not 1 <= distinct < rows:
        return None
    return {"table": table, "key": key, "rows": rows, "distinct": distinct}


def _static_status(agg: Aggregate) -> str | None:
    """The status an aggregate has from its SQL alone, or None."""
    if agg.function == "COUNT" and agg.distinct:
        return VERIFIED                        # repeated rows cannot change a distinct count
    if agg.distinct:
        return UNCHECKED                       # SUM/AVG of DISTINCT values is no fan-out fix
    if agg.rows and agg.function == "AVG":
        return VERIFIED                        # the average of a constant is that constant
    return None


def _status(agg: Aggregate, plan: Plan, idents: dict, total: int, grains: dict):
    """``(status, repeats)`` for one aggregate from the check's measurements."""
    if agg.rows:
        # COUNT(*) counts joined rows: verified when they are exactly the
        # distinct rows of ONE relation of the join (the counted grain).
        if any(grain.root for grain in grains.values()):
            return VERIFIED, []
        if len(grains) < len(plan.sources):
            return UNCHECKED, []            # an unmeasured relation could be the grain
        root = grains.get(plan.root)
        fact = _fact(plan, idents, plan.root, total, root.distinct) if root else None
        return REPEATED, [fact] if fact else []
    read = [a for a in agg.aliases if a in grains]
    if any(grains[a].own for a in read):
        return VERIFIED, []
    nothing_once = len(grains) == len(plan.sources) and not any(g.own for g in grains.values())
    complete = bool(agg.aliases) and len(read) == len(agg.aliases) and not agg.bare
    if not (nothing_once or complete):
        return UNCHECKED, []
    if not nothing_once and agg.function == "AVG":
        return WEIGHTED, []                    # a lookup-side average over counted child rows
    shown = read or list(grains)
    facts = [_fact(plan, idents, a, grains[a].rows, grains[a].distinct) for a in shown]
    return REPEATED, [f for f in facts if f]


def _is_timeout(exc: Exception) -> bool:
    return getattr(exc, "pgcode", None) == "57014" or type(exc).__name__ == "QueryCanceled"


def clear_key_cache() -> None:
    with _KEY_CACHE_LOCK:
        _KEY_CACHE.clear()


def _relation_keys(cursor, schema: str, cache_key: Any) -> dict[str, str]:
    """``{relation: key}`` for *schema*, read once per ``cache_key`` and
    ``KEY_CACHE_SECONDS`` through ``pg_keys`` on the caller's cursor."""
    from . import pg_keys
    marker = (cache_key, schema)
    now = time.monotonic()
    with _KEY_CACHE_LOCK:
        cached = _KEY_CACHE.get(marker)
        if cached and cached[0] > now:
            return cached[1]
    cursor.execute(pg_keys.PRIMARY_KEY_COLUMNS_SQL, (schema, schema))
    rows = list(cursor.fetchall() or [])
    lineage: dict = {}
    if any(isinstance(r, (list, tuple)) and r and r[0] == "view" for r in rows):
        cursor.execute(pg_keys.ONE_TABLE_VIEW_DEFS_SQL, (schema,))
        lineage = pg_keys.lineage_from_rows(list(cursor.fetchall() or []))
    keys = pg_keys.relation_keys(pg_keys.primary_keys_from_rows(rows, lineage))
    with _KEY_CACHE_LOCK:
        _KEY_CACHE[marker] = (now + KEY_CACHE_SECONDS, keys)
    return keys


def _measure(cursor, schema: str, plan: Plan, cache_key: Any,
             restore_timeout_ms: int | None) -> tuple:
    """``(identities, joined rows, grains, reason)``; grains None when the
    check did not produce measurements."""
    idents: dict = {}
    try:
        cursor.execute(_SET_TIMEOUT_SQL, (str(CHECK_TIMEOUT_MS),))
        idents = identities(plan, _relation_keys(cursor, schema, cache_key), schema)
        built = verification_sql(plan, idents)
        if built is None:
            return idents, 0, None, "no identity"
        check_sql, aliases = built
        from .sql_safety import ensure_select_only
        ensure_select_only(check_sql)
        cursor.execute(check_sql)
        measured = _grains(cursor.fetchone(), aliases)
        if measured is None:
            return idents, 0, None, "error"
        return idents, measured[0], measured[1], "-"
    except Exception as exc:  # noqa: BLE001 - the check never blocks the answer
        return idents, 0, None, "timeout" if _is_timeout(exc) else "error"
    finally:
        if restore_timeout_ms is not None:
            try:
                cursor.execute(_SET_TIMEOUT_SQL, (str(restore_timeout_ms),))
            except Exception:  # noqa: BLE001,S110 - the connection closes right after  # nosec B110
                pass


def _trace_entries(plan: Plan | Unplanned) -> list[dict]:
    return [{"function": function, "label": label, "status": UNCHECKED,
             "signature": signature, "clause": "subquery"}
            for function, signature, label in plan.trace]


def _entries(cursor, schema: str, plan: Plan | Unplanned, cache_key: Any,
             restore_timeout_ms: int | None) -> tuple[list[dict], str]:
    if isinstance(plan, Unplanned):
        return [{"function": "AGGREGATE", "label": "SUM, COUNT or AVG over a JOIN",
                 "status": UNCHECKED, "signature": plan.signature, "clause": "select"}
                ] + _trace_entries(plan), plan.reason
    static = [_static_status(agg) for agg in plan.aggregates]
    reason, idents, total, grains = "-", {}, 0, {}
    if any(s is None for s in static):
        idents, total, grains, reason = _measure(cursor, schema, plan, cache_key,
                                                 restore_timeout_ms)
    entries = []
    for agg, fixed in zip(plan.aggregates, static):
        if fixed is not None:
            status, repeats = fixed, []
        elif grains is None:
            status, repeats = UNCHECKED, []
        else:
            status, repeats = _status(agg, plan, idents, total, grains)
        entry = {"function": agg.function, "label": agg.label, "status": status,
                 "signature": agg.signature, "clause": agg.clause}
        if status == REPEATED and repeats:
            entry["repeats"] = repeats
        entries.append(entry)
    if reason == "-" and any(e["status"] == UNCHECKED for e in entries):
        reason = "distinct" if UNCHECKED in static else "unresolved"
    return entries + _trace_entries(plan), reason


def aggregate_note(repeats: list[dict]) -> str:
    """Plain-language note for the system agent reading a repeated result."""
    facts = "; ".join(_fact_text(r) for r in repeats)
    return (
        "A SUM, COUNT or AVG in this result is unverified: it was computed over a JOIN "
        f"in which the rows being totaled repeat ({facts}), so it counts some of them more "
        "than once. Do not report it, or a ranking or selection made by it. Run the query "
        "again with that table totaled before the join, or joined so that each of its rows "
        "matches once.")


def _fact_text(fact: dict) -> str:
    if fact.get("key"):
        return (f"in {fact['table']}, {fact['rows']} joined rows carry only "
                f"{fact['distinct']} distinct {fact['key']} values")
    return (f"in {fact['table']}, {fact['rows']} joined rows are only {fact['distinct']} "
            "distinct rows")


def check(cursor, schema: str, sql: str, result: dict, *, cache_key: Any = None,
          restore_timeout_ms: int | None = None) -> dict:
    """``result`` with ``aggregate_check`` (and ``aggregate_note`` when a
    total repeats) when ``sql`` computes an aggregate over a JOIN; otherwise
    ``result`` itself. Never raises; the rows are never changed."""
    if not isinstance(result, dict) or "error" in result or "rows" not in result:
        return result
    try:
        plan = parse(sql, schema)
    except Exception:  # noqa: BLE001 - an unreadable query is unchecked, never fatal
        plan = _unplanned(sql, "shape")
    if plan is None:
        return result
    entries, reason = _entries(cursor, schema, plan, cache_key, restore_timeout_ms)
    main = [e for e in entries if e.get("clause") != "subquery"]
    subqueries = len(entries) - len(main)
    counts = {s: sum(e["status"] == s for e in main) for s in STATUSES}
    if main:
        overall = next(s for s in _PRECEDENCE if counts[s] or s == VERIFIED)
    else:
        overall, reason = UNCHECKED, "subquery"     # recorded for the trace only
    logger.info("AGGREGATE_CHECK status=%s aggregates=%d verified=%d repeated=%d weighted=%d "
                "unchecked=%d subquery=%d reason=%s", overall, len(main), counts[VERIFIED],
                counts[REPEATED], counts[WEIGHTED], counts[UNCHECKED], subqueries, reason)
    marker: dict = {"status": overall, "aggregates": entries}
    if counts[UNCHECKED] or not main:
        marker["reason"] = reason
    out = {**result, AGGREGATE_CHECK_KEY: marker}
    if counts[REPEATED]:
        out[AGGREGATE_NOTE_KEY] = aggregate_note(
            [f for e in main for f in e.get("repeats", [])])
    return out
