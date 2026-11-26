# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse a SELECT that onboarding measured cannot match (identifier shapes).

Onboarding measures same-named identifier columns of one concept that hold
different identifiers (``distinct_identifiers.measure``): each column's
values have their own shape and none occurs in the other column. Each such
pair is stored in the knowledge graph with both columns' shape signatures
(ADR-0098), and the system agent is handed the stored records with its
schema (``distinct_identifiers.record_from_row``); the baked schema's note
is a rendering of the same records (``schema_format.with_distinct_identifiers``),
with the shapes left out where an example cannot be printed there. Workshop
rehearsal, 2026-10-18: the system agent was told so in its schema and still,
in 8 of 10 asks of one question, looked values of one column up in the other
or joined the two columns to each other.

Before the PostgreSQL query tool runs a SELECT, :func:`check` refuses only
what cannot match:

1. ``wrong_form``: a string literal compared with ``=`` or ``IN`` to a
   measured column whose value has the paired column's shape and not this
   column's (it can only find no rows);
2. ``paired_join``: an equality between the two columns of a measured pair
   (they never share a value).

The refusal is an ordinary tool result: a plain note and, for 1, one
IDENTIFIER-MISS block per value (token, looked_in, my_format, context), the form the
agent's own protocol and the orchestrator's recovery already read, so the
recovery asks the system that pairs the two forms.

Everything else runs exactly as before: a schema with no measured shapes, a
column with no pair, LIKE, ranges, functions, casts, negations, a literal
the tokenizer marks uncertain, a column the guard cannot resolve to one
table, and any error inside the guard (logged, never blocking). So does a
value in the column's own shape, whoever wrote it: it can match, and an
own-shape value the agent edited from the paired form is often right (a
live check, 2026-10-18), so refusing it would also refuse the right rows.
The guard keeps no state between queries. Shapes are the system agent's
IDENTIFIER-MISS signature, the one the measurement used. The SQL is read
with the safety layer's tokenizer (``athena_scope``).

Generic: no dataset, system, table, column or concept literals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Most values named in one refusal.
MAX_LISTED = 10

#: Words that may stand right before a required conjunct (or its group).
_CONJUNCT_BEFORE = frozenset({"AND", "WHERE", "ON", "HAVING"})
#: Words that open a filter clause whose conjuncts the guard reads.
_FILTER_CLAUSES = frozenset({"WHERE", "HAVING", "ON"})
#: Words that end a WHERE, HAVING or ON clause at its own depth.
_CLAUSE_ENDS = frozenset({
    "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING", "WINDOW", "FETCH", "FOR", "WHERE",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "UNION", "INTERSECT",
    "EXCEPT", "RETURNING",
})
#: Words that keep every row of the other side of a join.
_OUTER_JOIN_WORDS = frozenset({"LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL"})
#: Set operations: a statement holding one is never refused (fail open).
_SET_OPERATIONS = frozenset({"UNION", "INTERSECT", "EXCEPT"})
#: Words that may stand right after one.
_RIGHT_OK = frozenset({
    "AND", "OR", "THEN", "GROUP", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT",
    "EXCEPT", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
    "WHERE", "FETCH", "WINDOW",
})
#: Unquoted words that are never a column reference.
_KEYWORDS = _CONJUNCT_BEFORE | _RIGHT_OK | frozenset({
    "OR", "WHEN", "NOT", "IN", "IS", "NULL", "TRUE", "FALSE", "SELECT", "FROM", "ELSE", "END", "CASE",
    "LIKE", "ILIKE", "BETWEEN", "EXISTS", "ANY", "ALL", "SOME", "AS", "DISTINCT", "WITH",
    "DATE", "TIME", "TIMESTAMP", "INTERVAL", "USING", "SIMILAR",
})


@dataclass(frozen=True)
class Column:
    """One measured column and its shape. ``example`` is a value of that
    shape, for the refusal text."""
    table: str
    column: str
    concept: str
    signature: tuple
    example: str

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class ShapeContext:
    """The measured columns of one system: ``measured`` and ``partners`` are
    keyed by ``(table, column)``; ``tables`` maps a table to its columns."""
    measured: dict
    partners: dict
    tables: dict


def _sig(value: str) -> tuple:
    from .system_agent import _shape_signature
    return _shape_signature(value)


def _match(sig_a: tuple, sig_b: tuple) -> bool:
    from .system_agent import _signatures_match
    return _signatures_match(sig_a, sig_b)


# --------------------------------------------------------------------------- #
# Context: the measured pairs the graph holds
# --------------------------------------------------------------------------- #

def _record_sides(pair: dict) -> list | None:
    """``[(signature, example)]`` of side a and b of a stored pair
    (``distinct_identifiers.shaped_pairs``), or None."""
    from .distinct_identifiers import shape_text
    sides = []
    for side in ("a", "b"):
        signature = pair[f"signature_{side}"]
        example = shape_text(signature)
        if example is None:
            return None
        sides.append((signature, example))
    return sides


def _note_sides(pair: dict) -> list:
    """The same for a pair read back from a baked note: its examples."""
    return [(_sig(pair[f"shape_{side}"]), pair[f"shape_{side}"]) for side in ("a", "b")]


def _known_pairs(tables_desc: str, records) -> list:
    """``[(pair, sides)]`` of the stored records, or (``records`` None, or
    not all usable records) of the pairs baked into ``tables_desc``;
    ``sides`` None when unusable."""
    from .distinct_identifiers import shaped_pairs, signed_records
    from .schema_format import distinct_identifier_pairs
    if records is not None and signed_records(records) is not None:
        return [(pair, _record_sides(pair)) for pair in shaped_pairs(records)]
    return [(pair, _note_sides(pair)) for pair in distinct_identifier_pairs(tables_desc)]


def context_from(tables_desc: str, records=None) -> ShapeContext | None:
    """The guard's context, or None (no guard) when no pair with both shapes
    is known. A column measured with two different shapes, or a pair whose
    two shapes match, is left out. Pure.

    ``records`` are the measured pairs onboarding stored in the knowledge
    graph and handed to the agent (``distinct_identifiers.record_from_row``):
    the source of truth, so ``[]`` means no guard whatever the schema text
    says. Their stored shape signatures are used as they are, whether or not
    the note could print an example of them. ``None`` means the agent was
    configured without them (it was provisioned before the graph held the
    pairs): only then are the pairs read back from the note baked into
    ``tables_desc`` (``schema_format.distinct_identifier_pairs``), the
    fallback for a graph with no record. Records that are not all ones the
    guard can use (``distinct_identifiers.signed_records``: a
    shape-only record, a signature missing or invalid) count as none, so they
    never switch that fallback off. ``tables_desc`` always supplies the
    table columns a bare column name is resolved against."""
    from .response_integrity_sql import parse_physical_schema
    columns: dict = {}
    links: list = []
    for pair, shapes in _known_pairs(tables_desc, records):
        if shapes is None:
            continue
        sides = []
        for side, (signature, example) in zip(("a", "b"), shapes):
            key = (pair[f"table_{side}"].lower(), pair["column"].lower())
            sides.append(Column(key[0], key[1], pair["concept"], signature, example))
        if _match(sides[0].signature, sides[1].signature):
            continue
        for col in sides:
            known = columns.setdefault((col.table, col.column), col)
            if known is not None and not _match(known.signature, col.signature):
                columns[(col.table, col.column)] = None
        links.append(tuple(sides))
    measured = {key: col for key, col in columns.items() if col is not None}
    partners: dict = {}
    for side_a, side_b in links:
        a, b = (side_a.table, side_a.column), (side_b.table, side_b.column)
        if a in measured and b in measured:
            partners.setdefault(a, []).append(measured[b])
            partners.setdefault(b, []).append(measured[a])
    if not partners:
        return None
    tables = {table: frozenset(entry.get("columns") or ())
              for table, entry in parse_physical_schema(tables_desc).items()}
    return ShapeContext({k: measured[k] for k in partners},
                        {k: tuple(v) for k, v in partners.items()}, tables)


# --------------------------------------------------------------------------- #
# Reading the SQL (the safety layer's tokenizer)
# --------------------------------------------------------------------------- #

def _upper(tok) -> str:
    return tok.value.upper() if tok.kind == "word" else ""


def _name(tok) -> str:
    return tok.value.lower() if tok.kind == "word" else tok.value


def _is_name(tok) -> bool:
    return tok.kind == "identifier" or (tok.kind == "word" and _upper(tok) not in _KEYWORDS)


def _literal(tokens, i):
    """The text of a plain ``'...'`` literal at ``i``, else None (``E'..'``
    and other prefixed strings are not plain)."""
    if i < 0 or i >= len(tokens):
        return None
    tok = tokens[i]
    if tok.kind != "literal" or not tok.text.startswith("'") or len(tok.text) < 2:
        return None
    if i > 0 and tokens[i - 1].end == tok.start and tokens[i - 1].kind == "word":
        return None
    return tok.text[1:-1].replace("''", "'")


def _ref_back(tokens, end):
    """``(parts, start)`` of a column reference ending at ``end``, else None."""
    if end < 0 or not _is_name(tokens[end]):
        return None
    start = end
    while start >= 2 and tokens[start - 1].text == "." and _is_name(tokens[start - 2]):
        start -= 2
    parts = [_name(tokens[i]) for i in range(start, end + 1, 2)]
    return (parts, start) if len(parts) <= 3 else None


def _ref_forward(tokens, start):
    """``(parts, end)`` of a column reference starting at ``start``, else None."""
    if start >= len(tokens) or not _is_name(tokens[start]):
        return None
    end = start
    while end + 2 < len(tokens) and tokens[end + 1].text == "." and _is_name(tokens[end + 2]):
        end += 2
    if end + 1 < len(tokens) and tokens[end + 1].text in ("(", "."):
        return None
    parts = [_name(tokens[i]) for i in range(start, end + 1, 2)]
    return (parts, end) if len(parts) <= 3 else None


def _right_ok(tokens, end) -> bool:
    nxt = end + 1
    if nxt >= len(tokens):
        return True
    tok = tokens[nxt]
    return tok.text in (")", ";") or _upper(tok) in _RIGHT_OK


#: Join words that are also text functions (``left(x, 2)``).
_FUNCTION_WORDS = frozenset({"LEFT", "RIGHT"})


def _word(tokens, i) -> str:
    """The upper-cased word at ``i``, or "" (not a word, out of range, or
    ``left(`` / ``right(``, which are function calls, not joins)."""
    if i < 0 or i >= len(tokens) or tokens[i].kind != "word":
        return ""
    word = tokens[i].value.upper()
    if word in _FUNCTION_WORDS and i + 1 < len(tokens) and tokens[i + 1].text == "(":
        return ""
    return word


def _matching_close(tokens, open_index):
    from .athena_scope import _matching_close as close
    return close(tokens, open_index)


def _case_start(tokens, end_index):
    """Index of the CASE that the END at ``end_index`` closes (same depth),
    or None when there is none."""
    depth = tokens[end_index].depth
    nested = 0
    for i in range(end_index - 1, -1, -1):
        tok = tokens[i]
        if tok.depth < depth:
            return None
        if tok.depth > depth:
            continue
        word = _word(tokens, i)
        if word == "END":
            nested += 1
        elif word == "CASE":
            if not nested:
                return i
            nested -= 1
    return None


def _closes_between(tokens, and_index) -> bool:
    """True when the AND at ``and_index`` is the one in ``x BETWEEN a AND b``
    (a range delimiter, not a boolean AND), or when that can't be told (an
    END with no CASE), so the caller fails open."""
    depth = tokens[and_index].depth
    i = and_index - 1
    while i >= 0:
        tok = tokens[i]
        if tok.depth > depth:
            i -= 1
            continue
        if tok.depth < depth:
            return False
        word = _word(tokens, i)
        if word == "END":
            case = _case_start(tokens, i)
            if case is None:
                return True
            i = case - 1
            continue
        if word == "BETWEEN":
            return True
        if word in ("AND", "OR", "SELECT", "FROM", "CASE", "WHEN", "THEN", "ELSE") \
                or word in _FILTER_CLAUSES:
            return False
        i -= 1
    return False


def _has_or(tokens, first, depth) -> bool:
    """An OR at ``depth`` from ``first`` up to the end of its clause or group.
    An OR inside a CASE ... END at that depth belongs to the CASE's own
    condition, not to the clause, and is skipped."""
    cases = 0
    for i in range(first, len(tokens)):
        tok = tokens[i]
        if tok.depth < depth or tok.text == ";":
            return False
        if tok.depth == depth:
            word = _word(tokens, i)
            if word == "CASE":
                cases += 1
            elif word == "END" and cases:
                cases -= 1
            elif cases:
                continue
            elif word == "OR":
                return True
            elif word in _CLAUSE_ENDS or (tok.text == ")" and tok.depth < depth):
                return False
    return False


def _back_to_clause(tokens, start):
    """``("group", i)`` for the ``(`` that opens the boolean group holding the
    token at ``start``, ``("clause", i)`` for the WHERE / HAVING / ON it is a
    conjunct of, or None (a select list, CASE, or anything else). A complete
    CASE ... END before it at its depth is a sibling conjunct and is passed
    over whole."""
    depth = tokens[start].depth
    i = start - 1
    while i >= 0:
        tok = tokens[i]
        if tok.depth > depth:
            i -= 1
            continue
        if tok.depth < depth:
            return ("group", i) if tok.text == "(" else None
        word = _word(tokens, i)
        if word == "END":
            case = _case_start(tokens, i)
            if case is None:
                return None
            i = case - 1
            continue
        if word in _FILTER_CLAUSES:
            return ("clause", i)
        if word in ("SELECT", "FROM", "JOIN", "WHEN", "THEN", "ELSE", "CASE", "BY", "USING"):
            return None
        i -= 1
    return None


def _inner_join_on(tokens, on_index) -> bool:
    """True when the ON at ``on_index`` belongs to a plain or INNER JOIN and
    no later RIGHT or FULL join of the same FROM keeps the other side's rows."""
    depth = tokens[on_index].depth
    join = next((i for i in range(on_index - 1, -1, -1)
                 if tokens[i].depth == depth and _word(tokens, i) in ("JOIN", "SELECT", "FROM")),
                None)
    if join is None or _word(tokens, join) != "JOIN" or _word(tokens, join - 1) in _OUTER_JOIN_WORDS:
        return False
    for i in range(on_index + 1, len(tokens)):
        if tokens[i].depth < depth:
            break
        if tokens[i].depth == depth:
            word = _word(tokens, i)
            if word in ("RIGHT", "FULL"):
                return False
            if word in ("WHERE", "GROUP", "ORDER", "LIMIT", "HAVING"):
                break
    return True


def _subquery_use(tokens, open_index):
    """``(start, end)`` of the ``x IN (subquery)`` or ``EXISTS (subquery)``
    predicate whose subquery opens at ``open_index``, or None for any other
    use (NOT IN, ANY / ALL, a scalar, a derived table, a CTE body)."""
    close = _matching_close(tokens, open_index)
    if close is None or _word(tokens, open_index + 1) != "SELECT":
        return None
    before = _word(tokens, open_index - 1) if open_index > 0 else ""
    if before == "EXISTS":
        return open_index - 1, close
    if before == "IN":
        ref = _ref_back(tokens, open_index - 2)
        return (ref[1], close) if ref else None
    return None


def _required(tokens, start, end) -> bool:
    """True when the predicate spanning ``tokens[start..end]`` is a REQUIRED
    CONJUNCT of the statement: if it is false, no row can come back.

    That holds when it sits in a WHERE, a HAVING or the ON of a plain or
    INNER JOIN, joined to the clause's root by AND alone at every grouping
    level (no OR, not under NOT, not in CASE or a select list), and its query
    is the statement itself or a subquery used as ``x IN (subquery)`` or
    ``EXISTS (subquery)`` that is in turn a required conjunct. The caller
    leaves set operations out first. Anything else is not required."""
    for _ in range(len(tokens)):
        if not _right_ok(tokens, end):
            return False
        prev = tokens[start - 1] if start > 0 else None
        if prev is None or not (prev.text == "(" or _word(tokens, start - 1) in _CONJUNCT_BEFORE):
            return False
        if _word(tokens, start - 1) == "AND" and _closes_between(tokens, start - 1):
            return False
        found = _back_to_clause(tokens, start)
        if found is None:
            return False
        kind, at = found
        depth = tokens[start].depth
        if kind == "group":
            close = _matching_close(tokens, at)
            if close is None or _has_or(tokens, at + 1, depth):
                return False
            start, end = at, close
            continue
        if _has_or(tokens, at + 1, depth):
            return False
        if _word(tokens, at) == "ON" and not _inner_join_on(tokens, at):
            return False
        if depth == 0:
            return _word(tokens, 0) in ("SELECT", "WITH")
        opener = next((i for i in range(at - 1, -1, -1) if tokens[i].depth < depth), None)
        if opener is None or tokens[opener].text != "(":
            return False
        use = _subquery_use(tokens, opener)
        if use is None:
            return False
        start, end = use
    return False


def _scope_of(tokens, index):
    """``(depth, select index)`` of the query the token at ``index`` is in."""
    depth = tokens[index].depth
    for j in range(index - 1, -1, -1):
        if tokens[j].depth < depth:
            return None
        if tokens[j].depth == depth and _upper(tokens[j]) == "SELECT":
            return depth, j
    return None


def _scope_relation_count(tokens, depth, select_index) -> int:
    count = 0
    for tok in tokens[select_index + 1:]:
        if tok.depth < depth or (tok.depth == depth and _upper(tok) == "SELECT"):
            break
        if tok.depth == depth and _upper(tok) in ("FROM", "JOIN"):
            count += 1
    return count


class _Resolver:
    """Resolve a column reference to one measured column, or None."""

    def __init__(self, tokens, context: ShapeContext):
        from .athena_scope import relations
        self.tokens, self.context = tokens, context
        found, self.uncertain = relations(tokens)
        self.aliases: dict = {}
        self.relations: list = []          # (relation, table name)
        for rel in found:
            table = _name(tokens[rel.table_token_index + 2 * (len(rel.parts) - 1)])
            self.aliases.setdefault(str(rel.alias).lower(), set()).add(table)
            self.relations.append((rel, table))

    def resolve(self, parts, index):
        column = parts[-1]
        if len(parts) == 1:
            table = self._bare_table(column, index)
        else:
            tables = self.aliases.get(parts[-2], set())
            table = next(iter(tables)) if len(tables) == 1 else None
        return self.context.measured.get((table, column)) if table else None

    def _bare_table(self, column, index):
        if self.uncertain:
            return None
        scope = _scope_of(self.tokens, index)
        if scope is None:
            return None
        depth, select_index = scope
        in_scope = [table for rel, table in self.relations
                    if rel.depth == depth and rel.select_index == select_index]
        if not in_scope or len(in_scope) != _scope_relation_count(self.tokens, *scope):
            return None
        holders = []
        for table in in_scope:
            known = self.context.tables.get(table)
            if known is None:
                return None
            if column in known:
                holders.append(table)
        return holders[0] if len(holders) == 1 else None


# --------------------------------------------------------------------------- #
# Finding the comparisons
# --------------------------------------------------------------------------- #

def _comparisons(tokens, resolver):
    """Yield ``("values", Column, [text, ...])`` for each judged ``=`` / ``IN``
    literal comparison and ``("join", Column, Column)`` for each judged
    column-to-column equality between measured columns."""
    for i, tok in enumerate(tokens):
        if tok.kind == "operator" and tok.text == "=":
            yield from _equality(tokens, resolver, i)
        elif _upper(tok) == "IN":
            found = _in_list(tokens, resolver, i)
            if found:
                yield found


def _equality(tokens, resolver, i):
    left_lit, right_lit = _literal(tokens, i - 1), _literal(tokens, i + 1)
    left = None if left_lit is not None else _ref_back(tokens, i - 1)
    right = None if right_lit is not None else _ref_forward(tokens, i + 1)
    start = i - 1 if left_lit is not None else (left[1] if left else None)
    end = i + 1 if right_lit is not None else (right[1] if right else None)
    if start is None or end is None or not _required(tokens, start, end):
        return
    if left and right:
        col_a, col_b = resolver.resolve(left[0], start), resolver.resolve(right[0], i + 1)
        if col_a and col_b:
            yield ("join", col_a, col_b)
    elif left and right_lit is not None:
        col = resolver.resolve(left[0], start)
        if col:
            yield ("values", col, [right_lit])
    elif right and left_lit is not None:
        col = resolver.resolve(right[0], i + 1)
        if col:
            yield ("values", col, [left_lit])


def _in_list(tokens, resolver, i):
    ref = _ref_back(tokens, i - 1)
    if not ref or i + 1 >= len(tokens) or tokens[i + 1].text != "(":
        return None
    values, j = [], i + 2
    while j < len(tokens):
        value = _literal(tokens, j)
        if value is None:
            return None
        values.append(value)
        if j + 1 < len(tokens) and tokens[j + 1].text == ",":
            j += 2
            continue
        break
    close = j + 1
    if close >= len(tokens) or tokens[close].text != ")" or not values:
        return None
    if not _required(tokens, ref[1], close):
        return None
    col = resolver.resolve(ref[0], ref[1])
    return ("values", col, values) if col else None


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #

def _wrong_form(col: Column, partners, values):
    """``(partner, [values])`` of the values that have a partner's shape and
    not ``col``'s own, else None."""
    for partner in partners:
        wrong = [v for v in values
                 if _match(_sig(v), partner.signature) and not _match(_sig(v), col.signature)]
        if wrong:
            return partner, wrong
    return None


def check(sql: str, context: ShapeContext | None) -> dict | None:
    """The refusal result for ``sql``, or None to run it as today. Pure apart
    from the shape functions it shares with the agent."""
    if context is None or not isinstance(sql, str) or not sql.strip():
        return None
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain or not tokens or any(_upper(t) in _SET_OPERATIONS for t in tokens):
        return None
    resolver = _Resolver(tokens, context)
    lookups = []
    for kind, col, other in _comparisons(tokens, resolver):
        if kind == "join":
            if other in context.partners.get((col.table, col.column), ()):
                return _refusal("paired_join", col, other, [])
            continue
        lookups.append((col, other))
    for col, values in lookups:
        found = _wrong_form(col, context.partners[(col.table, col.column)], values)
        if found:
            return _refusal("wrong_form", col, found[0], found[1])
    return None


def guard_query(sql: str, context: ShapeContext | None) -> dict | None:
    """:func:`check` for the query tool: fail open on any error (logged)."""
    if context is None:
        return None
    try:
        found = check(sql, context)
    except Exception as exc:  # noqa: BLE001 - the guard is an addition, never required
        logger.warning("IDENTIFIER_SHAPE_GUARD failed (%s); the query runs", type(exc).__name__)
        return None
    if found:
        info = found["identifier_check"]
        logger.info("IDENTIFIER_SHAPE_GUARD: %s on %s (%d values); query not run",
                    info["kind"], info["column"], len(info["values"]))
    return found


# --------------------------------------------------------------------------- #
# The refusal result
# --------------------------------------------------------------------------- #

def _listed(values) -> list:
    return list(dict.fromkeys(values))[:MAX_LISTED]


def _miss_block(token: str, col: Column, partner: Column) -> str:
    return (f"IDENTIFIER-MISS:\ntoken: {token}\nlooked_in: {col.name}\n"
            f"my_format: {col.name} values are shaped like {col.example} (the shape onboarding "
            f"measured); {partner.name} holds a different identifier for {col.concept}\n"
            f"context: {token} has the form of {partner.name}, not of {col.name}.")


def _join_note(col: Column, partner: Column) -> str:
    return (f"Not run: the query compares {col.name} with {partner.name}. Onboarding measured "
            f"that these two columns hold different identifiers for {col.concept} (shaped like "
            f"{col.example} and {partner.example}) and never share a value, so the comparison "
            "matches no rows. Link rows of the two tables only through another link that says "
            "they belong together, such as a declared join key, or through identifiers the "
            "request supplies from the system that pairs the two forms.")


def _wrong_note(col: Column, partner: Column, values) -> str:
    shown = ", ".join(values)
    return (f"Not run: {shown} has the form of {partner.name} (shaped like {partner.example}), "
            f"not of {col.name} (shaped like {col.example}). Onboarding measured that these two "
            f"columns hold different identifiers for {col.concept} and never share a value, so "
            f"this lookup can only find no rows. Values of this form belong in {partner.name}. "
            f"For {col.name} the request must supply its own identifiers, from the system that "
            "pairs the two forms; do not rewrite a value into the other form yourself. If the "
            "request gives only values of the other form, say so with the IDENTIFIER-MISS "
            "block below.")


def _refusal(kind: str, col: Column, partner: Column, values) -> dict:
    values = _listed(values)
    info = {"kind": kind, "concept": col.concept, "column": col.name,
            "paired_column": partner.name, "values": values}
    if kind == "paired_join":
        note, blocks = _join_note(col, partner), []
    else:
        note = _wrong_note(col, partner, values)
        blocks = [_miss_block(v, col, partner) for v in values]
    return {"status": "not_run", "identifier_check": info, "note": note,
            "identifier_miss": blocks}
