# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Records an element expansion drops: rows whose serialized JSON list is empty or NULL.

Measured in dry runs on an analytics deployment: a grouped query by a code and
a serialized JSON list column was withheld because one list value was stored in
several spellings (``json_spellings``), and the refusal sent the agent to one
row per element (``CROSS JOIN UNNEST``). One group of the withheld rows held an
empty list for 15 records. The expansion has no row for a record with no
element, so its per-element counts for that code summed to 15 fewer than the
code's records. The expanded result carried no note, the totals line added up
its element rows as "every returned row", and the answer reported the smaller
figure and said nothing had been left out. Before the refusal existed, the
empty-list row was in the result and the answers reported it. In a later run
the refused query had no ``COUNT(*)`` output, so the number of those records
was not known; the agent counted them itself after the expansion, with the
same filters, and the answer still gave the per-element sum as a record count.

* :func:`observe` keeps, for one Mode A call, what a ``json_spellings`` finding
  on a grouped list column held: the table, the column, the WHERE conjuncts
  and other grouped expressions of its query, whether it had a HAVING, and the
  records whose list is empty or NULL, when that count is exact
  (``JsonSpellings.records_known``: a complete result with one ``COUNT(*)``
  output, or none of those rows).
* :func:`expansion` reads an inner element expansion of a serialized list
  column: one SELECT over one table with one top-level ``CROSS JOIN UNNEST(``
  or ``, UNNEST(`` whose argument is ``CAST`` or ``TRY_CAST`` of
  ``json_parse(<column>)`` to an ``ARRAY``. ``LEFT JOIN UNNEST ... ON TRUE``
  keeps those rows and is not read; nor is a CTE, a subquery, a join, a second
  expansion, or an expression in place of the column.
* :func:`dropped` decides what such a result carries. An observation of the
  same column of the same source (the full table name as written, catalog and
  schema included, case-folded; not a ``TABLESAMPLE`` or a ``FOR ... AS OF``
  snapshot), with the same top-level WHERE conjunct set (each conjunct as
  the shared tokenizer reads it, names case-folded; a query with an
  aggregate's ``FILTER (WHERE ...)`` is not read) and no HAVING on either
  query, whose count is exact, gives the count, and the observed rows when the
  expansion's other grouped expressions are the observation's. A count of
  zero gives nothing. Any other expansion, one inside a CTE or a derived
  table included, gives a :class:`Dropped` with no count: a note that such
  records are not in the rows and how to count them. A WHERE with a top-level
  OR is not a set of conjuncts, so it is not read.
* :func:`counted` reads the count that note asks for, run in the same call:
  one SELECT of one ``COUNT(*)`` over one table, no GROUP BY, HAVING or
  join, whose top-level WHERE is a set of conjuncts with exactly one test
  that selects every record whose list is empty or NULL and no other
  (:data:`json_spellings.EMPTY_COUNT`, alone or in an OR with ``<column> IS
  NULL`` and ``<column> = '<an empty list>'``, both inside it), and whose
  result is one whole count. It is an observation like :func:`observe`'s,
  with the test's conjunct left out, so a later expansion with the same
  source and the other conjuncts takes its count. :func:`awaiting` keeps an
  expansion that got no count, and :func:`reached` gives each such earlier
  expansion the count a later one holds, as a :class:`Dropped` for the call's
  engine line and REQUIRED sentence. The coalesced test also counts a value
  that is not a list (its length is NULL): ``CAST`` to an ``ARRAY`` fails the
  query on such a value, but ``TRY_CAST`` makes it NULL, and a LIMIT or FETCH
  with no ORDER BY can end the expansion before it has read every row, so
  such a count reaches no ``TRY_CAST`` expansion and no such limited one.
  :func:`pair` is what one result adds to the call, run under the call's
  lock so two results processed at once meet.
* A count and an expansion match on the field each reads (:func:`_field`):
  ``r.f`` is field ``f`` of row column ``r``, kept whole, unless ``r`` is the
  query's own relation (its alias, else its table name), which only
  qualifies column ``f``; the table's name under another alias is not read.
* :func:`note` is the result's note; :func:`engine_line` and
  :func:`requirement_block` are the answer's engine line and FINAL-ANSWER
  REQUIREMENTS block for a counted result; :func:`neutralize` makes a
  model-written copy of the line inert.
* :func:`complete_result` says whether a result holds every row of its query.

A table or column name is printed only when ``engine_names.printable`` passes.
Generic: no column, table or system names. Reads SQL with the shared tokenizer
and the exact split's and executed-filters' readers; no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] Empty lists"
#: Words the orchestrator prompt keys the requirement block on.
MARKER = "records with no element"
_OPENINGS = (("CROSS", "JOIN", "UNNEST"), (",", "UNNEST"))
_CASTS = {"CAST", "TRY_CAST"}
_CLAUSE_WORDS = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH"}
_COLUMN_PLACEHOLDER = "col"
_MAX_SHOWN = 6


# --------------------------------------------------------------------------- #
# The records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ListObservation:
    """What an earlier grouped result of this call held about one list column.
    ``table`` and ``column`` are lower case; ``where`` and ``others`` are
    ``None`` when not read; ``records`` is the exact count of records whose
    list is empty or NULL, else ``None``; ``rows`` is ``(labels, rows)`` of
    those rows merged by the other grouped values, or ``()``; ``source`` is
    the full name of the table the query read (``_source``), ``""`` when not
    one plain table; ``field`` the field the column reference reads
    (:func:`_field`), ``()`` when not proved; ``coalesced`` that ``records``
    also counts values that are not lists (:func:`counted`)."""

    table: str
    column: str
    where: frozenset | None
    having: bool
    others: frozenset | None
    records: int | None
    rows: tuple = ()
    source: str = ""
    field: tuple = ()
    coalesced: bool = False


@dataclass(frozen=True)
class Expansion:
    """An inner element expansion of ``column`` (as written; ``row.field`` for
    a field of a row column) of ``table``; ``source`` and ``field`` as on
    :class:`ListObservation`; ``cast`` is ``CAST`` or ``TRY_CAST``;
    ``limited`` that a top-level LIMIT or FETCH can end it before it has read
    every row (:func:`_limited`)."""

    table: str
    column: str
    where: frozenset | None
    having: bool
    others: frozenset | None
    source: str = ""
    field: tuple = ()
    cast: str = ""
    limited: bool = False


@dataclass(frozen=True)
class Dropped:
    """A result that expanded ``column`` of ``table`` into one row per element:
    ``records`` records under its filters whose list is empty or NULL are not
    in it (``None``: not counted), ``rows`` the earlier result's rows of them
    (``(labels, rows)``), or ``()``; ``parts`` the row column and field when
    ``column`` is a field of a row column, else ``()``."""

    table: str
    column: str
    records: int | None = None
    rows: tuple = ()
    parts: tuple = ()

    def to_dict(self, names=None) -> dict:
        """The record the result carries (``empty_lists_dropped``); a name that
        is not a plain identifier is a placeholder."""
        from .engine_names import Withheld
        from .json_spellings import MAX_EMPTY_ROWS
        names = names if names is not None else Withheld()
        out = {"column": names.show(self.column), "records": self.records}
        if self.rows:
            labels, rows = self.rows
            shown = {"columns": [names.show(str(label)) for label in labels],
                     "rows": [list(row) for row in rows[:MAX_EMPTY_ROWS]]}
            if len(rows) > MAX_EMPTY_ROWS:
                shown["rows_not_shown"] = len(rows) - MAX_EMPTY_ROWS
            out["rows"] = shown
        if names.count:
            out["names_withheld"] = names.count
        return out


# --------------------------------------------------------------------------- #
# Reading the SQL
# --------------------------------------------------------------------------- #
def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _tokens(sql):
    """The tokens of one statement read with certainty, else ``None``."""
    from .athena_scope import tokenize
    if not isinstance(sql, str):
        return None
    text = sql.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens or any(t.kind == "semicolon" for t in tokens):
        return None
    return tokens


def _select(sql):
    """The tokens of ``sql`` when it is one SELECT with no CTE, subquery or
    set operation, read with certainty, else ``None``."""
    tokens = _tokens(sql)
    if tokens is None:
        return None
    if _upper(tokens[0]) != "SELECT" or sum(_upper(t) == "SELECT" for t in tokens) != 1:
        return None
    if any(_upper(t) in {"UNION", "INTERSECT", "EXCEPT"} for t in tokens):
        return None
    return tokens


def _clauses(tokens) -> dict:
    found: dict = {}
    for index, token in enumerate(tokens):
        word = _upper(token)
        if token.depth == 0 and word in _CLAUSE_WORDS and word not in found:
            found[word] = index
    return found


def _clause_span(tokens, clauses: dict, word: str, skip: int = 1):
    """``[start, end)`` of one top-level clause's body, else ``None``."""
    if word not in clauses:
        return None
    start = clauses[word] + skip
    later = [i for i in clauses.values() if i > clauses[word]]
    return start, min([len(tokens), *later])


def _outputs(tokens, clauses: dict) -> list:
    from .athena_exact_shape import _split_alias, _split_commas
    if "FROM" not in clauses:
        return []
    first = 2 if len(tokens) > 1 and _upper(tokens[1]) == "DISTINCT" else 1
    return [_split_alias(item)[0] for item in _split_commas(tokens[first:clauses["FROM"]])]


def _conjuncts(tokens):
    """The query's own row filter: its top-level WHERE conjuncts, each as its
    tokens (``[]`` for no WHERE), else ``None``. A WHERE inside parentheses
    (an aggregate's ``FILTER (WHERE ...)``) filters one output, not the rows,
    so a query that has one is not read; nor is a WHERE with a top-level OR,
    whose AND-separated parts are not conjuncts of the whole."""
    from .executed_filters import _clause_spans, _conjunct_spans
    wheres = [t for t in tokens if _upper(t) == "WHERE"]
    if any(t.depth != 0 for t in wheres):
        return None
    if not wheres:
        return []
    spans = _clause_spans(tokens)
    if len(wheres) != 1 or len(spans) != 1:
        return None
    start, end = spans[0]
    if any(t.depth == 0 and _upper(t) == "OR" for t in tokens[start:end]):
        return None
    return [tokens[s:e] for s, e in _conjunct_spans(tokens, start, end)]


def _where(tokens):
    """:func:`_conjuncts` as a set of normalized texts (names case-folded),
    else ``None``."""
    from .athena_exact_shape import _norm
    found = _conjuncts(tokens)
    return None if found is None else frozenset(_norm(c) for c in found)


def _group_items(tokens, clauses: dict):
    """Each GROUP BY item's expression (a position resolved to its output),
    ``[]`` for no GROUP BY, else ``None``."""
    from .athena_exact_shape import _split_commas
    span = _clause_span(tokens, clauses, "GROUP", skip=2)
    if span is None:
        return None if len(tokens) > 1 and _upper(tokens[1]) == "DISTINCT" else []
    start, end = span
    if start > end or _upper(tokens[start - 1]) != "BY":
        return None
    outputs = _outputs(tokens, clauses)
    items = []
    for item in _split_commas(tokens[start:end]):
        if not item:
            return None
        if len(item) == 1 and item[0].kind == "number" and item[0].text.isdigit():
            position = int(item[0].text) - 1
            if not 0 <= position < len(outputs):
                return None
            item = outputs[position]
        items.append(item)
    return items


def _others(tokens, clauses: dict, skip) -> frozenset | None:
    from .athena_exact_shape import _norm
    items = _group_items(tokens, clauses)
    if items is None:
        return None
    return frozenset(_norm(item) for item in items if not skip(item))


def _relation(tokens):
    """The one relation the query reads, else ``None``."""
    from .athena_scope import relations
    scans, uncertain = relations(tokens)
    return scans[0] if not uncertain and len(scans) == 1 else None


def _table(tokens) -> str:
    found = _relation(tokens)
    return str(found.parts[-1]) if found is not None else ""


def _sampled(tokens) -> bool:
    """True when the query reads a sample or a snapshot of its table
    (``TABLESAMPLE``, ``FOR VERSION|TIMESTAMP|SYSTEM_TIME AS OF``): another
    population than the table's rows, even read twice the same way."""
    words = [_upper(t) for t in tokens]
    if "TABLESAMPLE" in words:
        return True
    return any(word == "FOR" and index + 1 < len(words)
               and words[index + 1] in {"VERSION", "TIMESTAMP", "SYSTEM_TIME", "SYSTEM_VERSION"}
               for index, word in enumerate(words))


def _source(tokens) -> str:
    """The full name of the one table the query reads, as written (catalog,
    schema and table, case-folded), else ``""``: no single relation, or a
    sample or snapshot of it. Two sources match only when equal, so a
    qualified name is not taken for the unqualified one."""
    found = _relation(tokens)
    if found is None or _sampled(tokens):
        return ""
    return ".".join(str(part).lower() for part in found.parts)


def _names(ref) -> tuple:
    """The names of a bare, quoted or two-part column reference as written,
    else ``()``."""
    from .athena_exact_shape import _column_ref
    from .athena_scope import _strip_outer
    ref = _strip_outer(list(ref or ()))
    if _column_ref(ref) is None:
        return ()
    return tuple(t.value for t in ref if t.text != ".")


def _field(ref, relation) -> tuple:
    """The field the column reference ``ref`` reads in a query of the one
    ``relation``, as its names in lower case, else ``()``: a bare or quoted
    name is the column; in ``r.f``, ``r`` the relation's alias (its table name
    when it has none) only qualifies column ``f``, the table's name under
    another alias is not read, and any other ``r`` is a row column whose field
    ``f`` is kept whole, so two fields of the same name stay apart."""
    names = tuple(name.lower() for name in _names(ref))
    if len(names) <= 1:
        return names
    if relation is None:
        return ()
    alias, table = str(relation.alias).lower(), str(relation.table).lower()
    if names[0] == alias:
        return names[1:]
    return () if names[0] == table else names


def _row_parts(field: tuple) -> tuple:
    """``field``'s row column and field when it is one, else ``()``."""
    return field if len(field) == 2 else ()


def _opening(tokens, index: int, any_depth: bool = False) -> int:
    """How many tokens open an inner expansion at ``index`` (``CROSS JOIN
    UNNEST`` or ``, UNNEST``, then ``(``), top level unless ``any_depth``,
    else 0."""
    if tokens[index].depth != 0 and not any_depth:
        return 0
    for words in _OPENINGS:
        span = tokens[index:index + len(words) + 1]
        if (len(span) == len(words) + 1 and span[-1].text == "("
                and [t.text if w == "," else _upper(t) for t, w in zip(span, words)]
                == list(words)):
            return len(words)
    return 0


def _parsed_ref(argument):
    """``(cast, reference)`` for ``CAST|TRY_CAST(json_parse(<column>) AS
    ARRAY(...))``: the cast word in upper case and the column reference's
    tokens, else ``None``."""
    from .athena_exact_shape import _column_ref
    from .athena_scope import _matching_close
    if (len(argument) < 9 or _upper(argument[0]) not in _CASTS or argument[1].text != "("
            or _matching_close(argument, 1) != len(argument) - 1
            or _upper(argument[2]) != "JSON_PARSE" or argument[3].text != "("):
        return None
    close = _matching_close(argument, 3)
    if close is None or close + 3 >= len(argument):
        return None
    ref = argument[4:close]
    if _column_ref(ref) is None or _upper(argument[close + 1]) != "AS" or \
            _upper(argument[close + 2]) != "ARRAY" or argument[close + 3].text != "(":
        return None
    if _matching_close(argument, close + 3) != len(argument) - 2:
        return None
    return _upper(argument[0]), ref


def _parsed_column(argument) -> str | None:
    """The column :func:`_parsed_ref` reads, by its last name, else ``None``."""
    from .athena_exact_shape import _column_ref
    parsed = _parsed_ref(argument)
    return _column_ref(parsed[1]) if parsed is not None else None


def _alias_names(tokens, close: int) -> frozenset | None:
    """The column names ``[AS] name(c, ...)`` after an UNNEST's ``)`` gives."""
    from .athena_scope import _matching_close
    index = close + 2 if close + 1 < len(tokens) and _upper(tokens[close + 1]) == "AS" \
        else close + 1
    if (index + 1 >= len(tokens) or tokens[index].kind not in {"word", "identifier"}
            or tokens[index + 1].text != "("):
        return None
    end = _matching_close(tokens, index + 1)
    names = [t for t in tokens[index + 2:end or index + 2] if t.text != ","]
    if not names or any(t.kind not in {"word", "identifier"} for t in names):
        return None
    return frozenset(t.value.lower() for t in names)


def expansion(sql) -> Expansion | None:
    """The inner element expansion of a serialized list column ``sql`` holds
    (see the module docstring), else ``None``."""
    from .athena_scope import _matching_close
    tokens = _select(sql)
    if tokens is None:
        return None
    unnests = [i for i, t in enumerate(tokens) if _upper(t) == "UNNEST"]
    starts = [(i, size) for i in range(len(tokens)) if (size := _opening(tokens, i))]
    if len(unnests) != 1 or len(starts) != 1:
        return None
    start, size = starts[0]
    close = _matching_close(tokens, start + size)
    if close is None:
        return None
    parsed = _parsed_ref(tokens[start + size + 1:close])
    relation = _relation(tokens)
    if parsed is None or relation is None:
        return None
    cast, ref = parsed
    field, written = _field(ref, relation), _names(ref)
    table = str(relation.parts[-1])
    if not table:
        return None
    column = ".".join(written) if _row_parts(field) else written[-1]
    clauses = _clauses(tokens)
    aliases = _alias_names(tokens, close)
    others = None
    if aliases is not None:
        others = _others(tokens, clauses, lambda item: any(
            t.kind in {"word", "identifier"} and t.value.lower() in aliases for t in item))
    return Expansion(table=table, column=column, where=_where(tokens),
                     having="HAVING" in clauses, others=others, source=_source(tokens),
                     field=field, cast=cast, limited=_limited(clauses))


def _limited(clauses: dict) -> bool:
    """True when a top-level LIMIT or FETCH can end the query before it has
    read every row: one with no ORDER BY, which has to read them all first."""
    return bool({"LIMIT", "FETCH"} & set(clauses)) and "ORDER" not in clauses


def expansion_column(sql) -> str | None:
    """The list column ``sql`` expands into one row per element, else ``None``."""
    try:
        found = expansion(sql)
    except Exception:  # noqa: BLE001 - an unread query is not an expansion
        return None
    return found.column if found is not None else None


def _nested_column(sql) -> str | None:
    """The column of the first inner expansion of a serialized list column at
    any depth of ``sql`` (a CTE, a derived table), else ``None``; ``None``
    too when ``sql`` has an outer join or a set operation, which could keep
    or add back the records the expansion drops."""
    from .athena_scope import _matching_close
    tokens = _tokens(sql)
    if tokens is None:
        return None
    words = [_upper(t) for t in tokens]
    if {"LEFT", "RIGHT", "FULL", "OUTER", "UNION", "INTERSECT", "EXCEPT"} & set(words):
        return None
    starts = [(i, size) for i in range(len(tokens))
              if (size := _opening(tokens, i, any_depth=True))]
    if words.count("JOIN") != sum(size == 3 for _i, size in starts):
        return None
    for index, size in starts:
        close = _matching_close(tokens, index + size)
        column = _parsed_column(tokens[index + size + 1:close]) if close else None
        if column is not None:
            return column
    return None


def _named(column: str):
    """A test that an expression is a reference to ``column`` by its last name."""
    from .athena_exact_shape import _column_ref

    def named(item) -> bool:
        return (_column_ref(item) or "").lower() == column
    return named


def observe(sql, found) -> ListObservation | None:
    """What a ``json_spellings`` finding on a list column of ``sql``'s rows
    holds for a later expansion of that column, else ``None``. Its field is
    the one grouped reference to that column (:func:`_field`), ``()`` when
    there is none or more than one."""
    if (found is None or getattr(found, "container", "") != "list"
            or not found.plain or not found.table):
        return None
    tokens = _select(sql)
    if tokens is None:
        return None
    clauses = _clauses(tokens)
    column = found.column.lower()
    named = _named(column)
    others = _others(tokens, clauses, named)
    relation = _relation(tokens)
    keys = _group_items(tokens, clauses) or _outputs(tokens, clauses)
    fields = {_field(item, relation) for item in keys if named(item)}
    field = next(iter(fields)) if len(fields) == 1 else ()
    known = found.records_known
    records = (found.empty_records if found.empty_rows else 0) if known else None
    source = _source(tokens)
    if source.rsplit(".", 1)[-1] != found.table.lower():
        source = ""
    return ListObservation(table=found.table.lower(), column=column, where=_where(tokens),
                           having="HAVING" in clauses, others=others, records=records,
                           rows=found.empty_table if known else (), source=source,
                           field=field)


def _counts(observation, found: Expansion) -> bool:
    """True when ``observation`` holds the exact count of the records the
    expansion ``found`` leaves out: the same field of the same full source,
    the same top-level WHERE conjunct set, and no HAVING on either query. A
    coalesced count also counts values that are not lists, so it holds only
    for a ``CAST`` expansion not :func:`_limited`, which fails on such a
    value once it has read every row; a ``TRY_CAST`` makes it NULL."""
    return (isinstance(observation, ListObservation)
            and observation.table == found.table.lower()
            and bool(found.field) and observation.field == found.field
            and bool(found.source) and observation.source == found.source
            and observation.records is not None
            and not (observation.coalesced and (found.cast != "CAST" or found.limited))
            and not observation.having and not found.having
            and observation.where is not None and observation.where == found.where)


def dropped(sql, observations) -> Dropped | None:
    """What an expansion result of ``sql`` carries (see the module docstring),
    else ``None``: not an expansion, or a count of zero. An expansion inside a
    CTE or a derived table gets no count, only the note; so does one whose
    source is not the observation's full table name, or a sample or snapshot."""
    found = expansion(sql)
    if found is None:
        nested = _nested_column(sql)
        return Dropped(table="", column=nested) if nested is not None else None
    matching = [o for o in observations or () if _counts(o, found)]
    parts = _row_parts(found.field)
    if not matching:
        return Dropped(table=found.table, column=found.column, parts=parts)
    same = [o for o in matching if o.others is not None and o.others == found.others]
    chosen = (same or matching)[-1]
    if chosen.records == 0:
        return None
    return Dropped(table=found.table, column=found.column, records=chosen.records,
                   rows=chosen.rows if same else (), parts=parts)


def awaiting(sql, found) -> Expansion | None:
    """The expansion ``sql`` reads when its result ``found`` (from
    :func:`dropped`) got no count, so a later count in the call can reach it
    (:func:`reached`), else ``None``."""
    if not isinstance(found, Dropped) or found.records is not None or not found.table:
        return None
    return expansion(sql)


def reached(observation, observations) -> tuple:
    """A :class:`Dropped` with the count ``observation`` holds (from
    :func:`counted`) for each earlier expansion among ``observations`` (kept by
    :func:`awaiting`) whose left-out records it counts, else ``()``; a count
    of zero reaches none."""
    if not isinstance(observation, ListObservation) or not observation.records:
        return ()
    out: list = []
    for item in observations or ():
        if isinstance(item, Expansion) and _counts(observation, item):
            hit = Dropped(table=item.table, column=item.column, records=observation.records,
                          parts=_row_parts(item.field))
            if hit not in out:
                out.append(hit)
    return tuple(out)


def pair(sql, result, kept) -> tuple:
    """What one ``result`` of ``sql`` adds to a call that keeps the list
    records ``kept``: ``(found, keep, disclosures)``. ``found`` is
    :func:`dropped`'s record for an expansion, else ``None``; ``keep`` the
    record a later result pairs with (an expansion that got no count, from
    :func:`awaiting`, or a count, from :func:`counted`), else ``None``;
    ``disclosures`` the counted expansions for the call's engine lines (this
    expansion, or each earlier one a count reaches). Pure: the call runs it
    under one lock with the keeping and the disclosing, so two results
    processed at once still meet."""
    found = dropped(sql, kept)
    if found is None:
        observation = counted(sql, result)
        if observation is None:
            return None, None, ()
        return None, observation, reached(observation, kept)
    if found.records:
        return found, None, (found,)
    return found, awaiting(sql, found), ()


# --------------------------------------------------------------------------- #
# A count of the records whose list is empty or NULL
# --------------------------------------------------------------------------- #
def _empty_literal(token) -> bool:
    """True for a string literal whose text is an empty JSON list."""
    import json
    text = token.text if token.kind == "literal" else ""
    if len(text) < 2 or text[0] != "'" or text[-1] != "'":
        return False
    try:
        return json.loads(text[1:-1].replace("''", "'")) == []
    except ValueError:
        return False


def _coalesced_length(tokens):
    """The column reference ``COALESCE(json_array_length(<column>), 0) = 0``
    tests, as its tokens, else ``None``: true for a NULL cell, an empty list
    in any spelling, and a value that is not a list."""
    from .athena_exact_shape import _column_ref
    from .athena_scope import _matching_close
    if (len(tokens) < 11 or _upper(tokens[0]) != "COALESCE" or tokens[1].text != "("
            or _upper(tokens[2]) != "JSON_ARRAY_LENGTH" or tokens[3].text != "("):
        return None
    inner = _matching_close(tokens, 3)
    outer = _matching_close(tokens, 1)
    if inner is None or outer is None or outer != len(tokens) - 3:
        return None
    if [t.text for t in tokens[inner + 1:outer]] != [",", "0"]:
        return None
    if tokens[-2].text != "=" or tokens[-1].kind != "number" or tokens[-1].text != "0":
        return None
    ref = tokens[4:inner]
    return ref if _column_ref(ref) is not None else None


def _narrower(tokens):
    """The column reference ``<column> IS NULL`` or ``<column> = '<an empty
    list>'`` tests, as its tokens, else ``None``: each selects only rows the
    coalesced test selects."""
    from .athena_exact_shape import _column_ref
    ref = None
    if len(tokens) >= 3 and [_upper(t) for t in tokens[-2:]] == ["IS", "NULL"]:
        ref = tokens[:-2]
    elif len(tokens) >= 3 and tokens[-2].text == "=" and _empty_literal(tokens[-1]):
        ref = tokens[:-2]
    return ref if ref is not None and _column_ref(ref) is not None else None


def _empty_test(tokens, relation) -> tuple:
    """The field (:func:`_field`) a WHERE conjunct tests for an empty or NULL
    list, selecting every such row and no other, else ``()``: the coalesced
    test, alone or in an OR with :func:`_narrower` tests of the same field."""
    from .athena_scope import _strip_outer
    inner = _strip_outer(tokens)
    if not inner:
        return ()
    depth = min(t.depth for t in inner)
    if any(t.depth == depth and _upper(t) in {"CASE", "AND", "NOT"} for t in inner):
        return ()
    parts, begin = [], 0
    for index, token in enumerate(inner):
        if token.depth == depth and _upper(token) == "OR":
            parts.append(inner[begin:index])
            begin = index + 1
    parts.append(inner[begin:])
    anchors, fields = 0, set()
    for part in (_strip_outer(p) for p in parts):
        ref = _coalesced_length(part)
        anchors += ref is not None
        ref = ref if ref is not None else _narrower(part)
        field = _field(ref, relation) if ref is not None else ()
        if not field:
            return ()
        fields.add(field)
    return next(iter(fields)) if anchors and len(fields) == 1 else ()


def _whole_count(result) -> int | None:
    """The one count a complete one-row, one-column result holds, else ``None``."""
    if not isinstance(result, dict) or "error" in result:
        return None
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        return None
    row = rows[0]
    if not isinstance(row, (list, tuple)) or len(row) != 1:
        return None
    columns = result.get("columns")
    if isinstance(columns, list) and len(columns) != 1:
        return None
    cell = row[0]
    text = str(cell).strip() if isinstance(cell, (str, int)) and not isinstance(cell, bool) else ""
    return int(text) if text.isdigit() else None


def counted(sql, result) -> ListObservation | None:
    """The count of the records whose list is empty or NULL that ``sql`` and
    its ``result`` hold (see the module docstring), as an observation for an
    expansion of that column, else ``None``."""
    from .athena_exact_shape import _norm
    tokens = _select(sql)
    if tokens is None or not complete_result(result, sql):
        return None
    words = {_upper(t) for t in tokens}
    if words & {"JOIN", "UNNEST", "DISTINCT", "OVER"}:
        return None
    clauses = _clauses(tokens)
    if "GROUP" in clauses or "HAVING" in clauses:
        return None
    outputs = _outputs(tokens, clauses)
    if len(outputs) != 1 or [_upper(t) or t.text for t in outputs[0]] != ["COUNT", "(", "*", ")"]:
        return None
    table, source = _table(tokens), _source(tokens)
    conjuncts = _conjuncts(tokens)
    records = _whole_count(result)
    if not table or not source or conjuncts is None or records is None:
        return None
    relation = _relation(tokens)
    tests = [(index, field) for index, part in enumerate(conjuncts)
             if (field := _empty_test(part, relation))]
    if len(tests) != 1:
        return None
    (at, field), = tests
    where = frozenset(_norm(part) for index, part in enumerate(conjuncts) if index != at)
    return ListObservation(table=table.lower(), column=".".join(field), where=where,
                           having=False, others=None, records=records, source=source,
                           field=field, coalesced=True)


def complete_result(result, sql) -> bool:
    """True when ``result`` holds every row of its query: not cut by rows,
    pages, windows or the engine's row limit, and fewer rows than the query's
    own LIMIT, with no OFFSET."""
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        return False
    rows = result["rows"]
    if (result.get("truncated") is True or result.get("capped_at_limit")
            or result.get("partial_reason") or result.get("partial_note")):
        return False
    count = result.get("row_count")
    if count is not None and count != len(rows):
        return False
    from .answer_figures import _cut_by_query
    return not _cut_by_query(sql, rows)


# --------------------------------------------------------------------------- #
# The note, the engine line and the requirement block: facts, names only when
# printable
# --------------------------------------------------------------------------- #
def _sql_column(names, column: str, parts: tuple = ()) -> tuple[str, str]:
    """``(the column in SQL, a sentence saying what stands in for it)``;
    ``parts`` the row column and field of a field of a row column."""
    from .engine_names import printable
    if parts and all(printable(part) for part in parts):
        return ".".join(f'"{part}"' for part in parts), ""
    if not parts and printable(column):
        return f'"{column}"', ""
    names.show(column)
    return _COLUMN_PLACEHOLDER, (f" Here {_COLUMN_PLACEHOLDER} is the list column, whose name "
                                 "is withheld.")


def _records(n: int) -> str:
    return f"{n:,} record{'' if n == 1 else 's'}"


def note(found: Dropped) -> str:
    """The note on a result that expanded a list column into one row per element."""
    from .engine_names import Withheld
    from .json_spellings import EMPTY_COUNT
    names = Withheld()
    column = names.show(found.column)
    head = f"this result has one row per element of the list in {column}"
    if found.records:
        verb = "is" if found.records == 1 else "are"
        return (f"{head}, so the {_records(found.records)} under the same filters whose list "
                f"is empty or NULL {verb} not in it: an earlier result in this call counted "
                "them, and empty_lists_dropped holds them; report them beside these "
                f"counts.{names.sentence()}")
    sql_column, stand_in = _sql_column(names, found.column, found.parts)
    return (f"{head}, so a record whose list is empty or NULL has no row here; count those "
            f"records under the same filters with {EMPTY_COUNT.format(column=sql_column)} and "
            f"report them beside these counts.{stand_in}{names.sentence()}")


def _distinct(found) -> list:
    out = []
    for item in found or ():
        if isinstance(item, Dropped) and item.records and item not in out:
            out.append(item)
    return out[:_MAX_SHOWN]


def engine_line(found) -> str:
    """One bounded engine line, as facts, for this call's expansion results
    whose dropped records were counted, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(found)
    if not shown:
        return ""
    names = Withheld()
    parts = []
    for item in shown:
        where = f"in {names.show(item.table, 'table')} " if item.table else ""
        verb = "is" if item.records == 1 else "are"
        parts.append(f"a result {where}expanded the list in {names.show(item.column)} into one "
                     f"row per element, so {_records(item.records)} under its filters whose "
                     f"list is empty or NULL {verb} not in its rows")
    return (f"{ENGINE_PREFIX} check, in this turn's queries: {'; '.join(parts)}. Each such "
            f"result leaves out {MARKER}.{names.sentence()}")


def requirement_block(found) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for these results, as facts, ``""``
    for none."""
    from .engine_names import Withheld
    shown = _distinct(found)
    if not shown:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = Withheld()
    notes = []
    for item in shown:
        one = item.records == 1
        notes.append(f"REQUIRED: the answer must say that {_records(item.records)} under the "
                     f"filters of its figures by {names.show(item.column)} "
                     f"{'holds' if one else 'hold'} no element of it and "
                     f"{'is' if one else 'are'} not in those figures.")
    if names.count:
        notes[-1] += names.sentence()
    document = empty_document()
    for text in notes:
        document = append_note(document, text, GuidanceScope.ANSWER_REQUIREMENT)
    return render_orchestrator_sections(document)["answer_requirements"]


def neutralize(text: str) -> str:
    """``text`` with a copy of the engine line's prefix made inert."""
    if not isinstance(text, str) or ENGINE_PREFIX not in text:
        return text
    return text.replace(ENGINE_PREFIX, ENGINE_PREFIX[len("[engine] "):])
