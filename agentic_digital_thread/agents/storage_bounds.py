# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounds on a table's storage columns that the question never asked for.

Measured on an analytics deployment: asked for an exact count over a
completion window, a SQL system agent hit the scan limit and added bounds on
the table's storage partition columns (a creation-date column, a region
column) to get under it. The executed-filters engine line showed both to the
orchestrator, but the final answer disclosed only one, so the reader could
not tell the total left rows out.

This module marks such a bound and requires its disclosure:

* :func:`columns_from_tables_desc` reads a table's storage columns from the
  baked schema the system agent already has: Hive partition keys (the
  ``(partition key)`` suffix, from the Glue catalog) and Iceberg partition
  sources (the ``(hidden partitioning by ...)`` marker, read from the
  table's partition metadata; a field named for its source column, or
  ``<column>_<transform>`` for a year/month/day/hour/bucket/truncate
  transform of it);
* :func:`with_result_columns` adds the storage columns the Athena
  connector's own result records name (``storage_bound``), which it reads
  from the table's partition fields: an Iceberg table's partition sources are
  not catalog keys, so the baked schema may not carry them;
* :func:`storage_bound_line` lists, for this call's queries numbered like the
  executed-filters line, each storage column a WHERE clause bounds on a table
  the query reads, unless the delegated question names that column. Each is
  marked "storage bound, not asked for";
* :func:`requirement_block` renders the matching FINAL-ANSWER REQUIREMENTS
  block (the answer-requirement scope, ``guidance_scope``): the answer must
  name each such bound and say the total may exclude rows outside it.

Generic: no column, table or system names. Pure apart from the per-system
registry the system agent fills at construction.
"""
from __future__ import annotations

import re
import threading

from .athena_scope import tokenize

#: Iceberg partition transforms whose default field name is ``<column>_<name>``.
_TRANSFORMS = frozenset({"year", "month", "day", "hour", "bucket", "trunc"})
#: A statement that only reads catalog metadata is not a data query (the same
#: rule the executed-filters line numbers by).
_METADATA_MARKERS = ("information_schema", "$partitions")
_MAX_COLUMNS = 6
_MAX_QUERIES_SHOWN = 8
_NAME_CAP = 64

_lock = threading.Lock()

#: Per-system list of the baked ``tables_desc``'s FULLY QUALIFIED table names
#: (``s3t.<ns>.<table>``), kept so the S3 Tables guard's unknown_table refusal
#: can list the system's real relations even when the scope is open or
#: namespace-only. Separate from the terminal-keyed rule maps in ``_maps``.
_names: dict[str, list] = {}
_maps: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# Storage columns
# --------------------------------------------------------------------------- #
def columns_from_tables_desc(tables_desc: str) -> dict:
    """``{table: frozenset(storage columns)}`` from a baked ``tables_desc``.

    A table with no partition key and no readable hidden-partition field is
    left out (unpartitioned or unknown: nothing to mark)."""
    from .system_agent import _desc_entries
    out: dict = {}
    for table, fragments, _marked, hidden in _desc_entries(tables_desc or ""):
        names = [str(f).split(":", 1)[0].strip() for f in fragments]
        keys = {name for fragment, name in zip(fragments, names)
                if name and "(partition key)" in str(fragment).lower()}
        known = {name for name in names if name}
        for field in hidden or ():
            if field in known:
                keys.add(field)
                continue
            base, sep, suffix = str(field).rpartition("_")
            if sep and suffix.lower() in _TRANSFORMS and base in known:
                keys.add(base)
        # Key by the TERMINAL name: the guard looks these facts up by the last
        # name part, so a qualified S3 Tables render (s3t.<ns>.<table>) and
        # Athena's database.table both resolve the same way.
        terminal = str(table or "").rpartition(".")[2]
        if terminal and keys:
            out[terminal] = frozenset(keys)
    return out


def table_columns_from_tables_desc(tables_desc: str) -> dict:
    """``{table: frozenset(every column)}`` from a baked ``tables_desc``: what
    an unqualified name in a join could belong to."""
    from .system_agent import _desc_entries
    out: dict = {}
    for table, fragments, _marked, _hidden in _desc_entries(tables_desc or ""):
        names = {str(f).split(":", 1)[0].strip() for f in fragments}
        terminal = str(table or "").rpartition(".")[2]
        if terminal:
            out[terminal] = frozenset(n for n in names if n)
    return out


def remember(system_id: str, storage: dict, columns: dict | None = None) -> None:
    """Keep ``system_id``'s storage columns (and every table's columns) for
    its per-question engine line."""
    if not system_id:
        return
    with _lock:
        if storage:
            _maps[system_id] = (dict(storage), dict(columns or {}))
        else:
            _maps.pop(system_id, None)


def remember_tables_desc(system_id: str, tables_desc: str) -> None:
    """:func:`remember` from the system's baked ``tables_desc``."""
    remember(system_id, columns_from_tables_desc(tables_desc),
             table_columns_from_tables_desc(tables_desc))


def with_result_columns(storage: dict, statements) -> dict:
    """``storage`` plus the storage columns this call's own connector results
    named (a sidecar row's ``storage_bound`` record: the table, by its last
    name part, and the storage columns its query bounded).

    An Iceberg table's partition sources are not catalog keys, so the baked
    map can be empty for it while the connector, which reads the table's
    partition fields, knows the column. Tables are merged case-insensitively;
    a table keeps the name it first appears under. Names are kept as the
    record gives them, to match the query's SQL; they reach engine text only
    through :func:`storage_bound_line` and :func:`requirement_block`, which
    print a name only when ``engine_names.printable`` passes."""
    merged: dict = {}
    for table, cols in (storage or {}).items():
        merged.setdefault(str(table).lower(), [str(table), set()])[1].update(cols)
    for statement in statements or ():
        bound = statement.get("storage_bound") if isinstance(statement, dict) else None
        if not isinstance(bound, dict):
            continue
        table = str(bound.get("table") or "").rpartition(".")[2]
        cols = {str(c) for c in bound.get("columns") or () if str(c).strip()}
        if table and cols:
            merged.setdefault(table.lower(), [table, set()])[1].update(cols)
    return {name: frozenset(cols) for name, cols in merged.values() if cols}


def storage_map_for(system_id: str) -> dict:
    with _lock:
        return dict(_maps.get(system_id, ({}, {}))[0])


def columns_map_for(system_id: str) -> dict:
    with _lock:
        return dict(_maps.get(system_id, ({}, {}))[1])


def remember_qualified_names(system_id: str, names) -> None:
    """Keep ``system_id``'s baked qualified table names for the unknown_table
    refusal. An empty/absent list forgets them."""
    if not system_id:
        return
    with _lock:
        cleaned = [str(n) for n in (names or []) if str(n).strip()]
        if cleaned:
            _names[system_id] = cleaned
        else:
            _names.pop(system_id, None)


def qualified_names_for(system_id: str) -> list:
    with _lock:
        return list(_names.get(system_id, ()))


def reset() -> None:
    """Forget every system's storage columns and qualified names (tests)."""
    with _lock:
        _maps.clear()
        _names.clear()


# --------------------------------------------------------------------------- #
# What a statement reads and bounds, scope by scope
# --------------------------------------------------------------------------- #
#: Words that follow a relation reference instead of an alias.
_CLAUSE_WORDS = frozenset({
    "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT",
    "FULL", "OUTER", "CROSS", "ON", "USING", "UNION", "INTERSECT", "EXCEPT", "OFFSET",
    "FETCH", "WINDOW", "QUALIFY", "TABLESAMPLE", "FOR", "NATURAL", "LATERAL",
})
_JOIN_WORDS = frozenset({"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS",
                         "NATURAL"})
_SUBQUERY_WORDS = frozenset({"SELECT", "WITH", "VALUES"})
#: A relation the statement reads that is not one of the system's tables (a
#: subquery, a CTE, a table function): its columns are unknown.
_UNRESOLVED = None


def _name(token) -> str:
    if token.kind == "identifier":
        return str(token.value)
    return token.text if token.kind == "word" else ""


def _word(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _name_chain(tokens, j: int, end: int) -> tuple:
    """``(name parts, next index)`` of a dotted name starting at ``j``."""
    parts: list = []
    while j < end and _name(tokens[j]):
        parts.append(_name(tokens[j]))
        j += 1
        if j < end and tokens[j].text == ".":
            j += 1
            continue
        break
    return parts, j


def _close(tokens, open_index: int):
    """Index of the ``)`` closing the ``(`` at ``open_index``, else ``None``."""
    depth = tokens[open_index].depth
    for i in range(open_index + 1, len(tokens)):
        if tokens[i].text == ")" and tokens[i].depth == depth:
            return i
    return None


def _is_alias(token) -> bool:
    if token.kind == "identifier":
        return True
    return token.kind == "word" and _word(token) not in _CLAUSE_WORDS


def _cte_names(tokens) -> set:
    """Names a WITH clause defines (``name AS (``): never base tables."""
    out = set()
    for i in range(len(tokens) - 2):
        if (_name(tokens[i]) and _word(tokens[i + 1]) == "AS" and tokens[i + 2].text == "("
                and _word(tokens[i]) not in _CLAUSE_WORDS):
            out.add(_name(tokens[i]).lower())
    return out


def _where_spans(tokens) -> list:
    """``(where index, end)`` of every WHERE clause, at any nesting level."""
    from .executed_filters import _CLAUSE_END
    spans = []
    for w, token in enumerate(tokens):
        if _word(token) != "WHERE":
            continue
        depth, end = token.depth, w + 1
        while end < len(tokens):
            current = tokens[end]
            if current.kind == "semicolon" or (current.text == ")" and current.depth < depth):
                break
            if current.depth == depth and _word(current) in _CLAUSE_END:
                break
            end += 1
        spans.append((w, end))
    return spans


def _from_start(tokens, w: int):
    """Index of the FROM of the query block the WHERE at ``w`` belongs to."""
    depth = tokens[w].depth
    for i in range(w - 1, -1, -1):
        token = tokens[i]
        if token.depth < depth or (token.depth == depth and _word(token) == "SELECT"):
            return None
        if token.depth == depth and _word(token) == "FROM":
            return i
    return None


def _skip_condition(tokens, i: int, end: int, depth: int) -> int:
    """Past an ON/USING condition to the next relation boundary."""
    while i < end and not (tokens[i].depth == depth
                           and (tokens[i].text == "," or _word(tokens[i]) in _JOIN_WORDS)):
        i += 1
    return i


def _relation(tokens, i: int, end: int, ctes: set):
    """``(table or _UNRESOLVED, next index)`` of the relation starting at
    ``i``, or ``None`` when it cannot be read."""
    if tokens[i].text == "(":
        close = _close(tokens, i)
        return (None if close is None else (_UNRESOLVED, close + 1))
    parts, j = _name_chain(tokens, i, end)
    if not parts:
        return None
    if j < end and tokens[j].text == "(":                 # a table function
        close = _close(tokens, j)
        return (None if close is None else (_UNRESOLVED, close + 1))
    table = parts[-1].lower()
    return (_UNRESOLVED if table in ctes else table), j


def _relations(tokens, start: int, end: int, ctes: set):
    """``[(alias or None, table or _UNRESOLVED)]`` the FROM clause at
    ``start`` reads, or ``None`` when it cannot be read with certainty."""
    depth, rels, i = tokens[start].depth, [], start + 1
    while i < end:
        token = tokens[i]
        if token.depth != depth or token.text == "," or _word(token) in _JOIN_WORDS:
            i += 1
            continue
        if _word(token) in ("ON", "USING"):
            i = _skip_condition(tokens, i + 1, end, depth)
            continue
        if _word(token) == "LATERAL":
            return None
        read = _relation(tokens, i, end, ctes)
        if read is None:
            return None
        table, i = read
        if i < end and _word(tokens[i]) == "AS":
            i += 1
        alias = None
        if i < end and tokens[i].depth == depth and _is_alias(tokens[i]):
            alias = _name(tokens[i]).lower()
            i += 1
            if i < end and tokens[i].text == "(":           # alias(column, ...)
                close = _close(tokens, i)
                if close is None:
                    return None
                i, table = close + 1, _UNRESOLVED
        rels.append((alias, table))
    return rels


def _column_refs(tokens, start: int, end: int) -> list:
    """Name chains referenced in a WHERE clause body, leaving out nested
    subqueries (their own WHERE is read in their own scope), function names
    and typed literals (``DATE '...'``)."""
    refs, k = [], start
    while k < end:
        token = tokens[k]
        if token.text == "(" and k + 1 < end and _word(tokens[k + 1]) in _SUBQUERY_WORDS:
            close = _close(tokens, k)
            k = end if close is None else close + 1
            continue
        if not _name(token) or (k > start and tokens[k - 1].text == "."):
            k += 1
            continue
        parts, j = _name_chain(tokens, k, end)
        followed_by = tokens[j] if j < end else None
        if followed_by is not None and followed_by.text == "(":
            k = j
            continue
        if (len(parts) == 1 and token.kind == "word" and followed_by is not None
                and followed_by.kind == "literal"):
            k = j
            continue
        refs.append([p.lower() for p in parts])
        k = j
    return refs


def _owner(parts: list, rels: list, columns: dict):
    """``(table, column)`` a WHERE reference belongs to, or ``None`` when its
    owner cannot be named with certainty."""
    column = parts[-1]
    if len(parts) >= 2:
        qualifier = parts[-2]
        by_alias = [t for alias, t in rels if alias == qualifier]
        by_name = [t for alias, t in rels if alias is None and t == qualifier]
        owners = by_alias or by_name
        return (owners[0], column) if len(owners) == 1 and owners[0] else None
    if any(t is _UNRESOLVED for _alias, t in rels):
        return None
    if len(rels) == 1:
        return rels[0][1], column
    if any(t not in columns for _alias, t in rels):
        return None
    owners = [t for _alias, t in rels if column in columns[t]]
    return (owners[0], column) if len(owners) == 1 else None


def _statement_bounds(sql: str, storage: dict, columns: dict) -> set:
    """``{(table, column)}`` of storage columns this statement's WHERE
    clauses bound, each attributed to the relation that owns it."""
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return set()
    ctes = _cte_names(tokens)
    found = set()
    for w, end in _where_spans(tokens):
        start = _from_start(tokens, w)
        rels = _relations(tokens, start, w, ctes) if start is not None else None
        if not rels:
            continue
        for parts in _column_refs(tokens, w + 1, end):
            owner = _owner(parts, rels, columns)
            if owner and owner[0] in storage and owner[1] in storage[owner[0]]:
                found.add(owner)
    return found


def _asked(question: str, column: str) -> bool:
    """True when the question names ``column`` (as written, or its words)."""
    text = str(question or "").lower()
    forms = {column.lower(), column.lower().replace("_", " ")}
    return any(re.search(r"(?<![a-z0-9_])" + re.escape(form) + r"(?![a-z0-9_])", text)
               for form in forms if form)


def _lowered(mapping: dict) -> tuple:
    """``({table: {column}}, {table: name as written}, {(table, column): name})``
    with lowercased keys."""
    lowered, tables, names = {}, {}, {}
    for table, cols in (mapping or {}).items():
        key = str(table).lower()
        tables[key] = str(table)
        lowered[key] = {str(c).lower() for c in cols}
        names.update({(key, str(c).lower()): str(c) for c in cols})
    return lowered, tables, names


def _bounds(statements, storage: dict, question: str, columns: dict | None = None) -> dict:
    """``{(column, table): [ordinals]}`` of storage bounds not asked for."""
    storage_l, table_names, column_names = _lowered(storage)
    columns_l = _lowered(columns or {})[0]
    found: dict = {}
    ordinal = 0
    for statement in statements or []:
        sql = statement.get("query_text") if isinstance(statement, dict) else statement
        if not isinstance(sql, str) or not sql.strip():
            continue
        if any(marker in sql.lower() for marker in _METADATA_MARKERS):
            continue
        ordinal += 1
        if not storage_l:
            continue
        for table, column in sorted(_statement_bounds(sql, storage_l, columns_l)):
            name = column_names[(table, column)]
            if not _asked(question, name):
                found.setdefault((name, table_names[table]), []).append(ordinal)
    return found


# --------------------------------------------------------------------------- #
# The engine line and the requirement
# --------------------------------------------------------------------------- #
def _clip(text: str) -> str:
    return text if len(text) <= _NAME_CAP else text[:_NAME_CAP - 3] + "..."


def _ordinals(numbers: list) -> str:
    shown = [f"Q{n}" for n in numbers[:_MAX_QUERIES_SHOWN]]
    more = len(numbers) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {more} more" if more else text


def storage_bound_line(statements, storage: dict, question: str,
                       columns: dict | None = None) -> tuple:
    """``(engine line, [column, ...])`` for this call's storage bounds the
    question did not name, or ``("", [])``. One line, bounded. A bound is
    attributed only to the relation that owns the column (qualified by its
    alias or name, or unqualified with exactly one candidate in ``columns``,
    every table's columns); an unresolved scope is never marked. A table or
    column name that is not a plain identifier (``engine_names``) is never
    printed: the line puts a placeholder in its place and says how many it
    withheld. The returned columns are as the storage map holds them."""
    from .engine_names import Withheld
    found = _bounds(statements, storage, question, columns)
    if not found:
        return "", []
    entries = sorted(found.items(), key=lambda kv: (kv[1][0], kv[0][0], kv[0][1]))
    shown = entries[:_MAX_COLUMNS]
    names = Withheld()
    parts = [f"{_clip(names.show(column))} in {_ordinals(numbers)} (a storage partition column "
             f"of {_clip(names.show(table, 'table'))})" for (column, table), numbers in shown]
    more = len(entries) - len(shown)
    tail = f"; and {more} more" if more else ""
    columns = sorted({column for (column, _table), _n in entries})
    line = ("[engine] Storage bounds the question did not name, in this turn's queries "
            f"numbered as above: {'; '.join(parts)}{tail}. Each is a storage bound, not "
            "asked for: a total from those queries leaves out rows outside it."
            f"{names.sentence()}")
    return line, columns


def ingestion_basis_columns(statements) -> list:
    """The storage columns this call's connector results ran on under a
    declared write-time basis (a sidecar row's ``storage_basis``, from a
    ``storage_bound`` record whose ``basis`` is ``"ingestion"``,
    ``tool_result_capture.ingestion_basis_record``), whatever the query's shape: a grouping, projection or
    ordering on the storage column has no WHERE bound for
    :func:`storage_bound_line` to read, so these come from the record."""
    found: list = []
    for statement in statements or ():
        record = statement.get("storage_basis") if isinstance(statement, dict) else None
        if isinstance(record, dict):
            found.extend(str(c) for c in record.get("columns") or () if str(c).strip())
    return list(dict.fromkeys(found))[:_MAX_COLUMNS]


def ingestion_basis_facts(columns) -> tuple:
    """``(engine line, requirement block)`` for figures dated by when rows
    were written, ``()`` for none. A column name that is not a plain
    identifier (``engine_names``) is never printed."""
    from .engine_names import printable
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = [_clip(c) for c in columns or () if printable(c)]
    if not columns:
        return ()
    listed = ", ".join(names) if names else "a storage column whose name is withheld"
    line = (f"[engine] Write-time basis: figures in this turn's results are filtered, "
            f"grouped or ordered by {listed}, the time rows were written to the table, "
            "not a business time such as completion.")
    note = (f"REQUIRED: the answer must say its figures are dated by when rows were "
            f"written ({listed}), not by completion or another business time.")
    document = append_note(empty_document(), note, GuidanceScope.ANSWER_REQUIREMENT)
    return line, render_orchestrator_sections(document)["answer_requirements"]


#: A fence line. A match starts only at a dash run's first dash (the
#: lookbehind refuses a dash right after another), so a long run of dashes
#: is read once, not once per dash: linear in the text.
_FENCE_RE = re.compile(r"(?<!-)-{3,}\s*(BEGIN|END)\s+FINAL-ANSWER REQUIREMENTS\s*-{3,}",
                       re.IGNORECASE)


def neutralize(text: str) -> str:
    """``text`` with any FINAL-ANSWER REQUIREMENTS fence line made inert, so
    only the block the engine appends after a system agent's answer is one."""
    if not isinstance(text, str) or "FINAL-ANSWER" not in text.upper():
        return text
    return _FENCE_RE.sub("[GUIDANCE_FENCE_REMOVED]", text)


def requirement_block(columns) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for these storage bounds (rendered
    by ``guidance_scope`` in the answer-requirement scope), ``""`` for none.
    A column name that is not a plain identifier (``engine_names``) is never
    printed; the requirement counts it instead."""
    from .engine_names import printable
    raw = list(dict.fromkeys(str(c) for c in columns or () if str(c).strip()))[:_MAX_COLUMNS]
    if not raw:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = [_clip(c) for c in raw if printable(c)]
    hidden = len(raw) - len(names)
    if names:
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        more = (f" (and one on {hidden} more column{'s' if hidden != 1 else ''} whose "
                f"name{'s are' if hidden != 1 else ' is'} withheld)") if hidden else ""
        note = (f"REQUIRED: the answer must name the storage bound on {listed} that the "
                f"question did not ask for{more}, and say the total may exclude rows outside it.")
    else:
        note = (f"REQUIRED: the answer must say that the result ran with a storage bound the "
                f"question did not ask for, on {hidden} column{'s' if hidden != 1 else ''} "
                f"whose name{'s are' if hidden != 1 else ' is'} withheld, and that the total "
                "may exclude rows outside it.")
    document = append_note(empty_document(), note, GuidanceScope.ANSWER_REQUIREMENT)
    return render_orchestrator_sections(document)["answer_requirements"]
