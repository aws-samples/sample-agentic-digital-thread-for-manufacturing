# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Measure tags a column takes from the column it mirrors across a measured join.

The onboarding mapper is a model call per batch of tables. When two or three
vocabulary entries describe the same column (an amount on a line row reads
as the money concept, as the order it belongs to and as the invoice it is
billed on), which tag comes back varies from run to run, and provision
committed exactly what came back. This rule adds, after the internal joins
are measured, the tag a column's own measured evidence supports.

Column ``y`` takes concept ``C`` from column ``x`` when ALL of these hold:

1. a measured join links the two tables (the caller passes only declared
   foreign keys and name-pattern joins the data bore out, never a guess), the
   tables differ, and neither column is one of the join's own columns;
2. both columns have exactly the same declared type, and it is a fractional
   exact numeric (``numeric``/``decimal`` with a scale above zero, or
   ``money``). Integer columns are counts and keys: an ordered and a shipped
   quantity share a word and a type and still count different things;
3. their names share a word (split on non-letters and case changes,
   lowercased, words of three letters or more). When every shared word is
   one of ``GENERIC_WORDS`` (words that say what kind of number a column
   holds, not what it measures), the names are no evidence on their own:
   ``y``'s own generation expression must then name the column of condition
   5 that corroborates ``C``. A column computed from a priced column is
   that price's kind of money; a column that merely shares ``amount`` with
   a charge may be a payment;
4. ``C`` is a field-layer core concept the mapper tagged on ``x`` in the
   ``measure`` role. Entity concepts say which record a value belongs to and
   never spread; a concept outside the core vocabulary has no layer this
   module can read, so it never spreads either;
5. ``y``'s own table already carries ``C`` in the ``measure`` role on another
   column of that same declared type. A concept never enters a table through
   a join alone: a schema can keep quantities in its money type, and a stock
   level on one table must not tag every movement that references it;
6. ``y`` does not carry ``C`` in any role yet, and ``y`` is a target (the
   caller leaves out rows a steward edited at the review).

Only ``y``'s OWN expression counts. Another column computed from ``y`` (a
balance computed as a total minus ``y``) says ``y`` is money of some kind,
not that it is the concept that column carries. A view column's own
expression is its base column's, and only when the view's rewrite rule shows
it passes that column and the columns its expression reads straight through
(:func:`view_generated_expressions`); a matching name proves nothing.

Only the mapper's tags are sources or corroboration: an added tag is never
read back, so nothing chains. When several sources give ``y`` the same
concept it is added once, at the highest source confidence (ties: the first
source in sorted order). The result is sorted, so it never depends on input
order.

Pure: no I/O, no model, no graph. No concept, table or column name decides
anything; the concept layer comes from ``catalog.FIELD_CONCEPT_IDS``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .catalog import FIELD_CONCEPT_IDS

Column = tuple[str, str]
Join = tuple[str, str, str, str]

#: The MAPS_TO ``method`` (and the map-row ``basis``) of an added tag.
METHOD = "measured_mirror"

#: Name words that say what kind of number a column holds (how much, how
#: many, which one), not what it measures. Two columns that share only these
#: share no meaning: a charge and a payment both hold an amount, and a line's
#: total and an order's total are totals of different things. Kept small on
#: purpose: a word belongs here only when it says nothing about the subject.
GENERIC_WORDS = frozenset({"amount", "amt", "value", "total", "qty", "quantity",
                           "number", "num"})

_FRACTIONAL_EXACT = re.compile(r"^(numeric|decimal)\((\d+),(\d+)\)$")
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
_MIN_WORD = 3
#: A string literal, a quoted identifier, or an unquoted identifier, in the
#: text ``pg_get_expr`` renders for an expression.
_EXPR_TOKENS = re.compile(r"'(?:[^']|'')*'|\"((?:[^\"]|\"\")*)\"|([A-Za-z_][A-Za-z0-9_$]*)")
#: A target-list entry of a stored rule (PostgreSQL's node-tree text) whose
#: expression is a bare column reference: ``{TARGETENTRY :expr {VAR ...}
#: :resno .. :resname .. :resorigtbl .. :resorigcol .. :resjunk ..}``. A
#: backslash escapes the next character in a token; braces never appear
#: unescaped inside either node. Group 1: the VAR's fields; group 2: the
#: entry's own fields.
_RULE_PLAIN_OUTPUT = re.compile(
    r"\{TARGETENTRY :expr \{VAR ((?:\\.|[^{}\\])*)\}((?:\\.|[^{}\\])*)\}")
#: One node-tree token (backslash escapes kept).
_NODE_TOKEN = re.compile(r"(?:\\.|[^\s\\])+")


@dataclass(frozen=True, order=True)
class MirroredTag:
    """One tag to add: ``concept`` on ``table.column`` in the measure role,
    taken from ``source_table.source_column`` across ``join``."""

    table: str
    column: str
    concept: str
    confidence: float
    source_table: str
    source_column: str
    join: Join


def normalized_type(declared: str) -> str:
    """The declared type, lowercased with its spaces removed."""
    return re.sub(r"\s+", "", str(declared or "")).lower()


def is_fractional_exact(declared: str) -> bool:
    """``numeric(p,s)`` / ``decimal(p,s)`` with ``s > 0``, or ``money``."""
    text = normalized_type(declared)
    if text == "money":
        return True
    match = _FRACTIONAL_EXACT.match(text)
    return bool(match) and int(match.group(3)) > 0


def name_words(name: str) -> frozenset[str]:
    """The words of a column name, lowercased, three letters or more."""
    return frozenset(w.lower() for w in _WORDS.findall(str(name or ""))
                     if len(w) >= _MIN_WORD)


def expression_columns(expression: str, table_columns: Iterable[str]) -> frozenset[str]:
    """The columns of ``table_columns`` an expression names (as
    ``pg_get_expr`` renders it). Quoted identifiers keep their case and
    unescape ``""``; unquoted ones fold to lower case as PostgreSQL reads
    them; string literals name nothing. A function or type name counts only
    when the table has a column of that name."""
    names = set(table_columns)
    found: set[str] = set()
    for match in _EXPR_TOKENS.finditer(str(expression or "")):
        quoted, bare = match.group(1), match.group(2)
        name = quoted.replace('""', '"') if quoted is not None else (
            bare.lower() if bare else None)
        if name in names:
            found.add(name)
    return frozenset(found)


def _rule_plain_outputs(rule_text: str) -> dict[str, tuple[str, str, str]]:
    """``{output name: (range table index, origin relation oid, origin
    attribute number)}`` for each output of a view's rewrite rule that is a
    bare column reference. Outputs that compute anything, junk entries, and
    names that occur twice are left out."""
    seen: dict[str, tuple[str, str, str] | None] = {}
    for match in _RULE_PLAIN_OUTPUT.finditer(rule_text):
        var, rest = match.group(1), match.group(2)
        varno = re.search(r"(?:^|\s):varno (\d+)(?:\s|$)", var)
        level = re.search(r"(?:^|\s):varlevelsup (\d+)(?:\s|$)", var)
        tokens = _NODE_TOKEN.findall(rest)
        if len(tokens) % 2 or not all(t.startswith(":") for t in tokens[::2]):
            continue
        fields = dict(zip(tokens[::2], tokens[1::2]))
        name = re.sub(r"\\(.)", r"\1", fields.get(":resname", ""))
        if (not varno or not level or level.group(1) != "0" or not name or name == "<>"
                or fields.get(":resjunk") != "false"
                or not fields.get(":resorigtbl", "").isdigit()
                or not fields.get(":resorigcol", "").isdigit()):
            continue
        origin = (varno.group(1), fields[":resorigtbl"], fields[":resorigcol"])
        seen[name] = None if name in seen else origin
    return {name: origin for name, origin in seen.items() if origin}


def view_generated_expressions(rule_text: str, base_oid: str,
                               generated: Iterable[Mapping]) -> dict[str, str]:
    """The generation expressions a view's columns inherit from its base
    relation, ``{view column: expression}``.

    A view has no generated columns of its own, and a column name says
    nothing about where a view output comes from (a view can show one base
    column under another base column's name). The view's ``_RETURN`` rewrite
    rule (``rule_text``, its
    ``pg_rewrite.ev_action`` as text) records, for each output that is a bare
    column reference, the base attribute it reads. A view column ``c``
    inherits base column ``g``'s expression only when ALL of these hold:

    - the rule is one query with no JOIN syntax (a subquery, CTE, set
      operation or JOIN withholds everything);
    - ``c`` is a bare reference to ``g`` itself on relation ``base_oid``,
      under ``g``'s own name;
    - every base column the expression reads (``g["inputs"]``, from the
      expression's own catalog dependencies) is also a bare reference under
      its own name, through the SAME range-table entry as ``c`` (in a
      self-join the inputs could come from another row).

    ``generated``: the base relation's generated columns as ``{"attnum",
    "name", "expr", "inputs": [{"attnum", "name"}]}``; a malformed entry is
    skipped. Anything else withholds the expression: a missing expression
    only means a generic-only name match takes nothing."""
    text = str(rule_text or "")
    if text.count("{QUERY ") != 1 or "{JOINEXPR " in text:
        return {}
    outputs = _rule_plain_outputs(text)
    base = str(base_oid)
    found: dict[str, str] = {}
    for entry in generated or ():
        try:
            name, attnum, expr = str(entry["name"]), int(entry["attnum"]), str(entry["expr"])
            inputs = [(str(i["name"]), int(i["attnum"])) for i in entry.get("inputs") or []]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        own = outputs.get(name)
        if own is None or own[1:] != (base, str(attnum)):
            continue
        if all(outputs.get(n) == (own[0], base, str(a)) for n, a in inputs):
            found[name] = expr
    return found


def _field_measures(measure_tags: Mapping[Column, Mapping[str, float]], column: Column) -> dict:
    return {c: float(conf) for c, conf in (measure_tags.get(column) or {}).items()
            if c in FIELD_CONCEPT_IDS}


def _corroborators(table: str, column: str, declared: str, concept: str,
                   by_table: Mapping[str, list], measure_tags) -> frozenset[str]:
    """The other columns of ``table`` with the same declared type that carry
    ``concept`` in the measure role (condition 5)."""
    return frozenset(other for other, kind in by_table.get(table, ())
                     if other != column and normalized_type(kind) == declared
                     and concept in _field_measures(measure_tags, (table, other)))


def _candidates(join: Join, by_table, measure_tags, carried, targets, expression_refs):
    from_table, from_field, to_table, to_field = join
    if from_table == to_table or from_table not in by_table or to_table not in by_table:
        return
    own = {(from_table, from_field), (to_table, to_field)}
    for src_table, dst_table in ((from_table, to_table), (to_table, from_table)):
        for src_col, src_kind in by_table[src_table]:
            sources = _field_measures(measure_tags, (src_table, src_col))
            declared = normalized_type(src_kind)
            if (src_table, src_col) in own or not sources or not is_fractional_exact(declared):
                continue
            for dst_col, dst_kind in by_table[dst_table]:
                target = (dst_table, dst_col)
                shared = name_words(src_col) & name_words(dst_col)
                if (target in own or target not in targets
                        or normalized_type(dst_kind) != declared or not shared):
                    continue
                # Condition 3: generic words alone need the target's own expression.
                named = expression_refs.get(target, frozenset()) \
                    if shared <= GENERIC_WORDS else None
                for concept, conf in sources.items():
                    if concept in set(carried.get(target) or ()):
                        continue
                    support = _corroborators(dst_table, dst_col, declared, concept,
                                             by_table, measure_tags)
                    if not support or (named is not None and not support & named):
                        continue
                    yield MirroredTag(dst_table, dst_col, concept, conf, src_table, src_col,
                                      tuple(join))


def mirrored_measure_tags(column_types: Mapping[Column, str], joins: Iterable[Join],
                          measure_tags: Mapping[Column, Mapping[str, float]],
                          carried: Mapping[Column, Iterable[str]],
                          targets: Iterable[Column],
                          generated: Mapping[Column, str] | None = None) -> list[MirroredTag]:
    """The tags to add (see the module docstring), sorted.

    ``column_types``: ``{(table, column): declared type}`` for the schema.
    ``joins``: measured joins as ``(from_table, from_field, to_table, to_field)``.
    ``measure_tags``: ``{(table, column): {concept: confidence}}``, the
    reviewed tags in the ``measure`` role. ``carried``: every concept each
    column carries in any role. ``targets``: columns allowed to take a tag.
    ``generated``: ``{(table, column): generation expression}`` for the
    schema's generated columns; without it a generic-only name match never
    qualifies.
    """
    by_table: dict[str, list] = {}
    for (table, column), declared in sorted(column_types.items()):
        by_table.setdefault(table, []).append((column, declared))
    expression_refs = {
        (table, column): expression_columns(expr, (c for c, _k in by_table.get(table, ())))
        for (table, column), expr in (generated or {}).items()}
    target_set = set(targets)
    best: dict[tuple[str, str, str], MirroredTag] = {}
    for join in sorted(set(tuple(j) for j in joins)):
        for tag in _candidates(join, by_table, measure_tags, carried, target_set,
                               expression_refs):
            key = (tag.table, tag.column, tag.concept)
            held = best.get(key)
            if held is None or (-tag.confidence, tag) < (-held.confidence, held):
                best[key] = tag
    return sorted(best.values())
