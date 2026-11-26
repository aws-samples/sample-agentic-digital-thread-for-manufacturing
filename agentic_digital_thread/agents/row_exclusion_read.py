# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The certain reader of a query against a table's row exclusion.

``row_exclusion`` owns the fact (the steward's conditions, label, store,
registry, the one canonical predicate and the texts); this module only reads
SQL. :func:`classify` says how one statement reads one table against that
table's conditions: ``applied`` (EVERY read of the table carries the
predicate as a top-level AND conjunct of that read's own WHERE), ``missing``
(it certainly reads the table and at least one read does not), ``undecided``
with the reason this reader could not prove it, or ``not_read`` (it certainly
does not read the table, or its text never names it). ``row_exclusion``'s
module docstring is the specification; in short, each physical read of the
table (a set-operation branch, each side of a join or self-join, a CTE body,
a derived table, an expression subquery) is decided on its own WHERE, bound
to its own alias when its query block joins anything but a list expansion;
the reads that lack it are named (:class:`ReadSite`). ``undecided`` is not
"never refused": ``row_exclusion.decide`` refuses it once like ``missing``.

A condition is compared as its key, ``(column, op, frozenset(values))``, so
this module needs nothing from ``row_exclusion``. Pure.
"""
from __future__ import annotations

from dataclasses import dataclass

from .athena_scope import (_cte_bindings, _matching_close, _scope_end, _split_boolean,
                           _strip_outer, _where_expression, relations, tokenize)

_SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT"})


@dataclass(frozen=True)
class ReadSite:
    """One physical read of the table, in the order the statement names it
    (``ordinal``, 1-based, of ``total`` reads): its explicit ``alias`` (``""``
    when none), the ``qualifier`` its predicate must be bound to (``""`` when
    the read is alone in its query block), the set-operation ``branch`` it is
    in (0 for none) and that ``set_operator``, the innermost ``cte`` holding
    it, the ``subquery`` kind of an expression subquery holding it (``in``,
    ``exists``, ``scalar``, else ``""``) and whether its own WHERE
    ``carried`` the predicate."""

    ordinal: int
    total: int
    alias: str = ""
    qualifier: str = ""
    branch: int = 0
    set_operator: str = ""
    cte: str = ""
    subquery: str = ""
    carried: bool = False


@dataclass(frozen=True)
class Reading:
    """What :func:`classify` read of one statement for one table: ``kind`` is
    ``applied``, ``missing``, ``undecided`` or ``not_read`` (the statement
    does not read the table), ``reason`` why it was undecided, and ``sites``
    the reads that lack the predicate (``missing`` / ``undecided`` with the
    reads known; empty when the statement could not be read)."""

    kind: str
    reason: str = ""
    sites: tuple = ()


def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _one_statement(sql) -> str:
    """``sql`` without one trailing semicolon; any other stays (unreadable)."""
    text = sql.strip() if isinstance(sql, str) else ""
    return text[:-1] if text.endswith(";") else text


def _terminal(tokens, relation):
    """The last identifier token of the relation name starting at its first
    table token (its own quoting decides how it is compared)."""
    i = relation.table_token_index
    terminal = tokens[i] if i < len(tokens) else None
    j = i + 1
    while (j + 1 < len(tokens) and tokens[j].text == "."
           and tokens[j + 1].kind in {"word", "identifier"}):
        terminal = tokens[j + 1]
        j += 2
    return terminal


def _is_table(tokens, relation, table: str) -> bool:
    terminal = _terminal(tokens, relation)
    if terminal is None:
        return False
    if terminal.kind == "identifier":
        return str(terminal.value) == str(table)
    return str(terminal.value).lower() == str(table).lower()


def _relation_chains(relation) -> frozenset:
    """The lowercased qualifier CHAINS (tuples) a column of this read may carry:
    its alias (one token), and every suffix of its dotted table name that ENDS
    at the terminal table name (``(table,)``, ``(namespace, table)``,
    ``(catalog, namespace, table)``). A lone catalog or namespace component is
    NOT a chain, so ``flow`` or ``s3t`` alone never qualifies ``segments`` even
    though they are parts of ``s3t.flow.segments`` -- the review's correlated
    alias gap (the terminal name is the last part, so a suffix ending at it can
    never be a lone upstream component). An alias equal to an upstream
    component is still only the alias, bound as one token."""
    parts = [str(p).lower() for p in relation.parts if str(p)]
    chains = {tuple(parts[i:]) for i in range(len(parts))} if parts else set()
    if relation.alias:
        chains.add((str(relation.alias).lower(),))
    chains.discard(())
    return frozenset(chains)


def _list_expansion_at(tokens, index: int) -> bool:
    """``UNNEST (`` or ``( VALUES`` at ``index``: a list expanded into rows,
    which reads no table (a subquery inside it is a read of its own)."""
    nxt = tokens[index] if index < len(tokens) else None
    after = tokens[index + 1] if index + 1 < len(tokens) else None
    if nxt is None or after is None:
        return False
    if _upper(nxt) == "UNNEST" and after.text == "(":
        return True
    return nxt.text == "(" and _upper(after) == "VALUES"


def _joined(tokens, scan) -> bool:
    """True when the query block of ``scan`` JOINs anything but a list
    expansion (:func:`_list_expansion_at`): another table, a CTE, a derived
    table. An unqualified column then binds to no read this reader can name."""
    end = _scope_end(tokens, scan.select_index, scan.depth)
    return any(tokens[i].depth == scan.depth and _upper(tokens[i]) == "JOIN"
               and not _list_expansion_at(tokens, i + 1)
               for i in range(scan.select_index + 1, end))


def _enclosing_open(tokens, index: int) -> int | None:
    """The index of the ``(`` that encloses ``tokens[index]``, else ``None``
    at the top level."""
    depth = tokens[index].depth
    if depth == 0:
        return None
    for j in range(index - 1, -1, -1):
        if tokens[j].text == "(" and tokens[j].depth == depth - 1:
            return j
    return None


def _subquery_kind(tokens, select_index: int, bindings) -> str:
    """``in``, ``exists`` or ``scalar`` when the SELECT at ``select_index``
    is an expression subquery (its parenthesis neither a CTE body, a derived
    table nor a parenthesised set-operation branch), else ``""``."""
    opened = _enclosing_open(tokens, select_index)
    if opened is None or opened in {b.body_open for b in bindings}:
        return ""
    before = tokens[opened - 1] if opened > 0 else None
    word = _upper(before) if before is not None else ""
    if before is None or before.text == "(" or word in {"FROM", "JOIN", "ALL", "DISTINCT"} \
            or word in _SET_OPERATORS:
        return ""
    return {"IN": "in", "EXISTS": "exists"}.get(word, "scalar")


def _branch(tokens, scan) -> tuple:
    """``(branch, operator)``: the 1-based branch of the set operation the
    read's SELECT is in, at its own depth inside its own parenthesis, and the
    first operator of it (``UNION ALL``); ``(0, "")`` for none."""
    opened = _enclosing_open(tokens, scan.select_index)
    lo = opened + 1 if opened is not None else 0
    close = _matching_close(tokens, opened) if opened is not None else None
    hi = close if close is not None else len(tokens)
    ops = [i for i in range(lo, hi)
           if tokens[i].depth == scan.depth and _upper(tokens[i]) in _SET_OPERATORS]
    if not ops:
        return 0, ""
    nxt = tokens[ops[0] + 1] if ops[0] + 1 < len(tokens) else None
    name = _upper(tokens[ops[0]]) + (
        f" {_upper(nxt)}" if nxt is not None and _upper(nxt) in {"ALL", "DISTINCT"} else "")
    return 1 + sum(1 for i in ops if i < scan.select_index), name


def _cte_of(scan, bindings) -> str:
    """The name of the innermost CTE whose body holds the read, else ``""``."""
    holding = [b for b in bindings if b.body_open < scan.table_token_index < b.body_close]
    return max(holding, key=lambda b: b.body_open).name if holding else ""


def _conjuncts(tokens) -> list:
    """The AND conjuncts of a boolean expression, nested ANDs flattened and
    balanced outer parentheses stripped. An expression with an OR at its own
    level is ONE conjunct (``a AND b OR c`` is ``(a AND b) OR c``: AND binds
    tighter), so nothing under an OR is ever read as a conjunct of the whole."""
    tokens = _strip_outer(tokens or [])
    if not tokens:
        return []
    if len(_split_boolean(tokens, "OR")) > 1:
        return [tokens]
    parts = _split_boolean(tokens, "AND")
    if len(parts) == 1:
        return [tokens]
    out: list = []
    for part in parts:
        out.extend(_conjuncts(part))
    return out


def _literal_value(token) -> str | None:
    """The string a single-quoted literal token spells, else ``None``."""
    text = str(token.text)
    if token.kind != "literal" or len(text) < 2 or text[0] != "'" or text[-1] != "'":
        return None
    return text[1:-1].replace("''", "'")


def _is_empty_literal(tokens) -> bool:
    return len(tokens) == 1 and _literal_value(tokens[0]) == ""


class Qualified(frozenset):
    """The qualifier CHAINS a joined read's columns must carry (tuples): a bare
    column binds to no read this reader can name, so it is not that read's."""


def _column(tokens, chains) -> str | None:
    """The column a bare, quoted or qualified reference names (lowercased),
    when its qualifier CHAIN (every segment before the column) is one of
    ``chains`` (tuples of lowercased components); any qualifier when ``chains``
    is ``None``; a bare column only when ``chains`` is not :class:`Qualified`.
    A lone catalog or namespace component is never a chain, so it never
    qualifies the column (review F1). A quoted part keeps its own dots."""
    tokens = _strip_outer(tokens)
    if not tokens or len(tokens) % 2 == 0:
        return None
    parts = []
    for i, token in enumerate(tokens):
        if i % 2 == 0:
            if token.kind not in {"word", "identifier"}:
                return None
            parts.append(str(token.value))
        elif token.text != ".":
            return None
    if len(parts) > 1 and chains is not None:
        qualifier = tuple(p.lower() for p in parts[:-1])
        if qualifier not in chains:
            return None
    if len(parts) == 1 and isinstance(chains, Qualified):
        return None
    return parts[-1].lower()


def condition_key(tokens, names) -> tuple | None:
    """The key ``(column, op, frozenset(values))`` of the condition one
    conjunct spells in the canonical form (``COALESCE(<column>, '') = ''``,
    ``<> ''`` / ``!= ''``, or ``[NOT] IN (<string literals>)``), else
    ``None``. ``names`` are the qualifiers that bind (``None``: any)."""
    from .athena_exact_shape import _split_commas
    t = _strip_outer(tokens)
    if len(t) < 6 or _upper(t[0]) != "COALESCE" or t[1].text != "(":
        return None
    close = _matching_close(t, 1)
    if close is None:
        return None
    args = _split_commas(t[2:close])
    if len(args) != 2 or not _is_empty_literal(args[1]):
        return None
    column = _column(args[0], names)
    if column is None:
        return None
    rest = t[close + 1:]
    if len(rest) == 2 and rest[0].kind == "operator" and _is_empty_literal(rest[1:]):
        op = {"=": "empty", "<>": "not_empty", "!=": "not_empty"}.get(rest[0].text)
        return (column, op, frozenset()) if op else None
    negated = bool(rest) and _upper(rest[0]) == "NOT"
    listed = rest[1:] if negated else rest
    if (len(listed) < 3 or _upper(listed[0]) != "IN" or listed[1].text != "("
            or _matching_close(listed, 1) != len(listed) - 1):
        return None
    values = []
    for item in _split_commas(listed[2:-1]):
        value = _literal_value(item[0]) if len(item) == 1 else None
        if value is None:
            return None
        values.append(value)
    return (column, "not_in" if negated else "in", frozenset(values)) if values else None


def _carries(conjunct, wanted: list, names) -> bool:
    """True when ``conjunct`` is ``NOT (<the conditions whose keys are
    wanted>)``, normalized (see ``row_exclusion``'s docstring)."""
    part = _strip_outer(conjunct)
    if len(part) < 4 or _upper(part[0]) != "NOT" or part[1].text != "(":
        return False
    if _matching_close(part, 1) != len(part) - 1:
        return False
    found = []
    for item in _conjuncts(part[2:-1]):
        key = condition_key(item, names)
        if key is None:
            return False
        found.append(key)
    return len(found) == len(wanted) and set(found) == set(wanted)


def _condition_at(tokens, index: int) -> tuple | None:
    """The key of the canonical condition (any qualifier) starting at the
    ``COALESCE`` at ``index``, else ``None``."""
    if index + 1 >= len(tokens) or tokens[index + 1].text != "(":
        return None
    close = _matching_close(tokens, index + 1)
    if close is None:
        return None
    end = close + 1
    if end < len(tokens) and tokens[end].kind == "operator":
        end = min(end + 2, len(tokens))
    else:
        if end < len(tokens) and _upper(tokens[end]) == "NOT":
            end += 1
        if end + 1 < len(tokens) and _upper(tokens[end]) == "IN" and tokens[end + 1].text == "(":
            list_close = _matching_close(tokens, end + 1)
            end = list_close + 1 if list_close is not None else end
    return condition_key(tokens[index:end], None)


def _conditions_outside(tokens, spans, wanted: list) -> bool:
    """True when one of the wanted conditions (by column and op) is written
    outside every read's WHERE (each ``tokens[lo:hi]`` of ``spans``)."""
    column_ops = {(key[0], key[1]) for key in wanted}
    for i, token in enumerate(tokens):
        if any(lo <= i < hi for lo, hi in spans) or _upper(token) != "COALESCE":
            continue
        found = _condition_at(tokens, i)
        if found is not None and (found[0], found[1]) in column_ops:
            return True
    return False


def _undecided_unless_named(text: str, table: str, reason: str) -> Reading:
    """``undecided`` when the statement text names the table, else ``not_read``
    (the tokens cannot be trusted, so the text is searched)."""
    if str(table).lower() in text.lower():
        return Reading("undecided", reason)
    return Reading("not_read")


def may_read(sql: str, table: str) -> bool:
    """True when ``sql`` reads or MAY read ``table``: a certain scan of it, or
    a statement this reader cannot read with certainty (uncertain tokens,
    several statements, unreadable relations) whose text names it. For a
    table whose row exclusion is unknown, where every possible read is held."""
    text = _one_statement(sql)
    tokens, uncertain = tokenize(text)
    if not tokens:
        return False
    named = str(table).lower() in text.lower()
    if uncertain or any(t.kind == "semicolon" for t in tokens):
        return named
    scans, scan_uncertain = relations(tokens)
    if any(_is_table(tokens, s, table) for s in scans):
        return True
    return scan_uncertain and named


def _metadata_relation(relation) -> bool:
    """A relation of the engine's own catalog (``information_schema``), read
    for structural discovery, not a data table."""
    return any(str(part).lower() == "information_schema" for part in relation.parts[:-1])


def may_read_data(sql: str) -> bool:
    """True when ``sql`` reads or MAY read any data table: a scan of a
    relation outside ``information_schema``, or a statement this reader
    cannot read with certainty (uncertain tokens, several statements,
    unreadable relations). A read of ``information_schema`` only, or of no
    relation at all, is False. For a system whose row-exclusion manifest
    could not be read, where every data read is held and structural
    discovery is not."""
    text = _one_statement(sql)
    tokens, uncertain = tokenize(text)
    if not tokens:
        return False
    if uncertain or any(t.kind == "semicolon" for t in tokens):
        return True
    scans, scan_uncertain = relations(tokens)
    return scan_uncertain or any(not _metadata_relation(s) for s in scans)


def classify(sql: str, table: str, wanted) -> Reading:
    """How ``sql`` reads ``table`` against the condition keys ``wanted``
    (:func:`condition_key`'s shape): ``applied``, ``missing``, ``undecided``
    (with why) or ``not_read``."""
    wanted = list(wanted or ())
    if not wanted:
        return Reading("not_read", "no_exclusion")
    text = _one_statement(sql)
    tokens, uncertain = tokenize(text)
    if not tokens:
        return Reading("not_read")
    if uncertain:
        return _undecided_unless_named(text, table, "uncertain_tokens")
    if any(t.kind == "semicolon" for t in tokens):
        return _undecided_unless_named(text, table, "several_statements")
    scans, scan_uncertain = relations(tokens)
    ours = [s for s in scans if _is_table(tokens, s, table)]
    if scan_uncertain:
        return (Reading("undecided", "uncertain_relations") if ours
                else _undecided_unless_named(text, table, "uncertain_relations"))
    if not ours:
        return Reading("not_read")
    return _classify_reads(tokens, ours, wanted)


def _site(tokens, scan, ordinal: int, total: int, bindings, wanted: list) -> tuple:
    """``(ReadSite, WHERE token range)`` for one read of the table: decided
    on its own WHERE, its columns bound to its own alias when its block is
    joined (:func:`_joined`), else to its alias or a valid qualifier chain of
    its name (:func:`_relation_chains`; never a lone catalog/namespace part)."""
    joined = _joined(tokens, scan)
    alias = str(scan.alias) if str(scan.alias).lower() != str(scan.parts[-1]).lower() else ""
    chains = (Qualified({(str(scan.alias).lower(),)}) if joined
              else _relation_chains(scan))
    where = _where_expression(tokens, scan)
    carried = any(_carries(part, wanted, chains) for part in _conjuncts(where))
    branch, operator = _branch(tokens, scan)
    site = ReadSite(ordinal, total, alias=alias, qualifier=str(scan.alias) if joined else "",
                    branch=branch, set_operator=operator, cte=_cte_of(scan, bindings),
                    subquery=_subquery_kind(tokens, scan.select_index, bindings),
                    carried=carried)
    index = {t.start: i for i, t in enumerate(tokens)}
    span = (index[where[0].start], index[where[-1].start] + 1) if where else (0, 0)
    return site, span


def _classify_reads(tokens, ours: list, wanted: list) -> Reading:
    """The decision for a statement that certainly reads the table: every
    read decided on its own WHERE (:func:`_site`)."""
    bindings, cte_uncertain = _cte_bindings(tokens)
    if cte_uncertain or any(b.recursive for b in bindings):
        return Reading("undecided", "uncertain_with")
    decided = [_site(tokens, scan, n, len(ours), bindings, wanted)
               for n, scan in enumerate(ours, start=1)]
    lacking = tuple(site for site, _span in decided if not site.carried)
    if not lacking:
        return Reading("applied")
    if _conditions_outside(tokens, [span for _site, span in decided], wanted):
        return Reading("undecided", "conditions_elsewhere", lacking)
    return Reading("missing", "", lacking)
