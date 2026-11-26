# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The WHERE filters a SQL system agent's queries ran with, as ONE engine line.

Only a system agent's answer crosses the ``query_<id>`` boundary, so a
condition the agent added on its own (a bound on another date column after a
scan-limit refusal, an auto-scoped recent-partition predicate) is invisible to
the orchestrator unless the prose names it, and measured answers did not. The
trusted executed SQL of every query that returned results is already retained
for the turn (``tool_result_capture`` sidecar statements, each with its row
count). This module parses the WHERE clauses out of that SQL and renders them
as a bounded engine line that ``system_agent._append_engine_facts`` appends to
the answer, so the orchestrator can compare them with the question and
disclose an added bound.

Each query is numbered in run order with its row count, and each WHERE clause
is rendered conjunct by conjunct (split at its top-level ``AND``), so a length
cap shortens predicates but never removes one: a long ``IN`` list collapses to
``IN (<N values>)``, a long literal is clipped, and a predicate that still does
not fit keeps its column and operator. A refused or failed query carries no
executed SQL, so it is never listed.

Generic across SQL engines: it reuses the conservative tokenizer the Athena
scope guard uses (quotes, comments and nesting as PostgreSQL and Trino read
them), keeps the conditions as executed, and adds no interpretation. Pure and
offline.
"""
from __future__ import annotations

from .athena_scope import tokenize
from .engine_names import count_sentence, printable

#: Stands in for a name in a WHERE clause that is not a plain identifier
#: (``engine_names``): the predicate stays, the name is never printed.
WITHHELD_NAME = "<name withheld>"
#: Words that end a WHERE clause at its own nesting level.
_CLAUSE_END = frozenset({
    "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT",
    "EXCEPT", "FETCH", "WINDOW", "QUALIFY",
})
#: Tokens that make a conjunct a comparison: what follows is its operand.
_COMPARISON_WORDS = frozenset({"IN", "LIKE", "ILIKE", "BETWEEN", "IS", "EXISTS", "SIMILAR"})
#: A statement that only reads catalog metadata is not a data query.
_METADATA_MARKERS = ("information_schema", "$partitions", "$files", "$snapshots")
#: Bounds. A WHERE clause is shortened to MAX_CLAUSE_CHARS unless the column
#: and operator of every predicate alone are longer (they always stay); the
#: whole line is never longer than MAX_LINE_CHARS.
MAX_CLAUSE_CHARS = 300
MAX_STATEMENTS = 6
MAX_LINE_CHARS = 1200
#: Appended when one query alone is over MAX_LINE_CHARS and had to be cut.
INCOMPLETE_MARK = " (filters incomplete: predicates cut)"
_MAX_IN_LIST_CHARS = 60
_MAX_LITERAL_CHARS = 40
_SHORT_SIDE_CHARS = 28
_SHORT_OPERAND_CHARS = 32
_SHORT_PLAIN_CHARS = 40
_MIN_PLAIN_CHARS = 24
LINE_PREFIX = ("[engine] WHERE filters of this turn's queries that returned results, "
               "in run order, as executed (a query with no WHERE is not listed): ")


def _clause_spans(tokens) -> list[tuple[int, int]]:
    """Token index ranges ``[start, end)`` of every WHERE clause body."""
    spans: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.kind != "word" or token.value.upper() != "WHERE":
            continue
        depth = token.depth
        end = index + 1
        while end < len(tokens):
            current = tokens[end]
            if current.kind == "semicolon":
                break
            if current.text == ")" and current.depth < depth:
                break
            if (current.depth == depth and current.kind == "word"
                    and current.value.upper() in _CLAUSE_END):
                break
            end += 1
        if end > index + 1:
            spans.append((index + 1, end))
    # A WHERE inside another WHERE's body (an IN or EXISTS subquery) is already
    # part of that body's text: keep the outer clause only.
    return [span for span in spans
            if not any(other != span and other[0] <= span[0] and span[1] <= other[1]
                       for other in spans)]


def _conjunct_spans(tokens, start: int, end: int) -> list[tuple[int, int]]:
    """Split one clause body at its top-level ``AND``: not inside parentheses,
    not inside a ``CASE ... END``, and not the ``AND`` of a ``BETWEEN``."""
    if start >= end:
        return []
    depth = tokens[start].depth
    spans, begin, cases, between = [], start, 0, False
    for index in range(start, end):
        token = tokens[index]
        if token.depth != depth or token.kind != "word":
            continue
        word = token.value.upper()
        if word == "CASE":
            cases += 1
        elif word == "END" and cases:
            cases -= 1
        elif word == "BETWEEN" and not cases:
            between = True
        elif word == "AND" and not cases:
            if between:
                between = False
            else:
                spans.append((begin, index))
                begin = index + 1
    spans.append((begin, end))
    return [(s, e) for s, e in spans if e > s]


def _clean(text: str) -> str:
    # Only a literal can still hold a line break or tab here.
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _clip_literal(text: str) -> str:
    if len(text) <= _MAX_LITERAL_CHARS or text[:1] != "'":
        return text
    return text[:_MAX_LITERAL_CHARS - 4] + "...'"


def _value_list_close(tokens, open_index: int, end: int) -> int | None:
    """The index of the ``)`` closing a pure value list (literals and numbers
    separated by commas) opened at ``open_index``, else ``None``."""
    depth = tokens[open_index].depth
    expect_value = True
    for index in range(open_index + 1, end):
        token = tokens[index]
        if token.text == ")" and token.depth == depth:
            return index if not expect_value else None
        if expect_value and token.kind in {"literal", "number"}:
            expect_value = False
        elif not expect_value and token.text == ",":
            expect_value = True
        else:
            return None
    return None


def _pieces(tokens, start: int, end: int) -> list[tuple[str, bool]]:
    """``(text, space_before)`` pieces of one conjunct as executed, with a long
    ``IN`` value list collapsed to ``(<N values>)`` and long literals clipped."""
    pieces: list[tuple[str, bool]] = []
    index, previous_end = start, None
    while index < end:
        token = tokens[index]
        spaced = previous_end is not None and token.start > previous_end
        close = None
        before = tokens[index - 1] if index > 0 else None
        if (token.text == "(" and before is not None and before.kind == "word"
                and before.value.upper() == "IN"):
            close = _value_list_close(tokens, index, end)
        if close is not None:
            text = _clean(" ".join(t.text for t in tokens[index:close + 1]))
            if len(text) > _MAX_IN_LIST_CHARS:
                count = sum(1 for t in tokens[index + 1:close] if t.kind in {"literal", "number"})
                pieces.append((f"(<{count} values>)", spaced))
                previous_end, index = tokens[close].end, close + 1
                continue
        text = _clip_literal(token.text) if token.kind == "literal" else token.text
        if token.kind in {"word", "identifier"} and not printable(str(token.value)):
            text = WITHHELD_NAME
        pieces.append((_clean(text), spaced))
        previous_end, index = token.end, index + 1
    return pieces


def _join(pieces) -> str:
    return "".join((" " if spaced and i else "") + text
                   for i, (text, spaced) in enumerate(pieces)).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _comparison_at(tokens, start: int, end: int) -> int | None:
    """Piece offset of the conjunct's first top-level comparison, else ``None``."""
    depth = tokens[start].depth
    for index in range(start, end):
        token = tokens[index]
        if token.depth != depth:
            continue
        if token.kind == "operator" or (
                token.kind == "word" and token.value.upper() in _COMPARISON_WORDS):
            return index
    return None


def _forms(tokens, start: int, end: int) -> list[str]:
    """The conjunct rendered at three lengths: as executed (compacted), short
    (column side and operand clipped) and minimal (column side and operator).
    Every form keeps the column and the operator of a comparison."""
    full = _join(_pieces(tokens, start, end))
    at = _comparison_at(tokens, start, end)
    if at is None:
        return [full, _clip(full, _SHORT_PLAIN_CHARS), _clip(full, _MIN_PLAIN_CHARS)]
    # A leading NOT stays with its operator (NOT IN, NOT LIKE, IS NOT NULL).
    split = at - 1 if at - 1 > start and tokens[at - 1].value.upper() == "NOT" else at
    side = _clip(_join(_pieces(tokens, start, split)), _SHORT_SIDE_CHARS) if split > start else ""
    word_end = at + 1
    while word_end < end and tokens[word_end].kind == "word" and \
            tokens[word_end].value.upper() in {"NOT", "NULL", "DISTINCT", "FROM"}:
        word_end += 1
    operator = _join(_pieces(tokens, split, word_end))
    operand = _join(_pieces(tokens, word_end, end))
    head = f"{side} {operator}".strip()
    short = f"{head} {_clip(operand, _SHORT_OPERAND_CHARS)}".strip() if operand else head
    minimal = f"{head} ..." if operand else head
    return [full, short, minimal]


def _render_clause(tokens, start: int, end: int) -> str:
    """One WHERE clause within :data:`MAX_CLAUSE_CHARS`: the longest conjunct
    is shortened first, one form at a time, until the clause fits or every
    conjunct is at its minimal form (column and operator)."""
    forms = [_forms(tokens, s, e) for s, e in _conjunct_spans(tokens, start, end)]
    levels = [0] * len(forms)

    def text() -> str:
        return " AND ".join(f[level] for f, level in zip(forms, levels))

    while len(text()) > MAX_CLAUSE_CHARS:
        candidates = [i for i, level in enumerate(levels) if level < 2]
        if not candidates:
            break
        longest = max(candidates, key=lambda i: (len(forms[i][levels[i]]), -i))
        levels[longest] += 1
    return text()


def where_clauses(sql: str) -> list[str]:
    """The WHERE clause bodies of one SQL statement as executed, outermost
    first per scope, in source order. ``[]`` when there is none or the text is
    not SQL."""
    if not isinstance(sql, str) or not sql.strip():
        return []
    tokens, _uncertain = tokenize(sql)
    clauses = []
    for start, end in _clause_spans(tokens):
        clause = _join(_pieces_raw(tokens, start, end))
        if clause:
            clauses.append(clause)
    return clauses


def _pieces_raw(tokens, start: int, end: int) -> list[tuple[str, bool]]:
    pieces, previous_end = [], None
    for token in tokens[start:end]:
        pieces.append((_clean(token.text), previous_end is not None and token.start > previous_end))
        previous_end = token.end
    return pieces


def where_conjuncts(sql: str) -> list[list[str]]:
    """Per WHERE clause of ``sql``, its top-level ``AND`` conjuncts as executed."""
    if not isinstance(sql, str) or not sql.strip():
        return []
    tokens, _uncertain = tokenize(sql)
    return [[_join(_pieces_raw(tokens, s, e)) for s, e in _conjunct_spans(tokens, start, end)]
            for start, end in _clause_spans(tokens)]


def _statement_filter(sql: str) -> str:
    tokens, _uncertain = tokenize(sql)
    return " / ".join(f"WHERE {_render_clause(tokens, start, end)}"
                      for start, end in _clause_spans(tokens))


def _row_label(count) -> str:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return "row count not known"
    return f"{count:,} row{'' if count == 1 else 's'}"


def _pieces_label(pieces) -> str:
    """``, computed from N partition pieces`` for one exact-total split result:
    the query is ONE logical query whose WHERE is the model's own."""
    if not isinstance(pieces, int) or isinstance(pieces, bool) or pieces < 1:
        return ""
    return f", computed from {pieces:,} partition pieces"


def _withheld_names(sql: str) -> set:
    """The names in ``sql``'s WHERE clauses that the line does not print."""
    tokens, _uncertain = tokenize(sql)
    return {str(t.value).lower() for start, end in _clause_spans(tokens)
            for t in tokens[start:end]
            if t.kind in {"word", "identifier"} and not printable(str(t.value))}


def _is_catalog_probe(sql: str) -> bool:
    """A catalog probe (``information_schema``, ``pg_catalog``, ``pg_*``), by the
    same test the no-data line uses (``provenance.is_discovery_statement``);
    fail-soft."""
    try:
        from .provenance import is_discovery_statement
        return bool(is_discovery_statement(sql))
    except Exception:  # noqa: BLE001 - a probe test must never drop the line
        return False


def _numbered(statements) -> list[tuple[str, set]]:
    """``(Qn (rows): WHERE ..., withheld names)`` per data query that has a
    WHERE clause, ``n`` counting every data query this turn in run order. A
    result an exact-total split computed is listed once, with its piece
    count."""
    out: list[tuple[str, set]] = []
    ordinal = 0
    for statement in statements or []:
        if isinstance(statement, dict):
            sql, count = statement.get("query_text"), statement.get("row_count")
            pieces = statement.get("pieces")
        else:
            sql, count, pieces = statement, None, None
        if not isinstance(sql, str) or not sql.strip():
            continue
        if any(marker in sql.lower() for marker in _METADATA_MARKERS) or _is_catalog_probe(sql):
            continue
        ordinal += 1
        rendered = _statement_filter(sql)
        if rendered:
            out.append((f"Q{ordinal} ({_row_label(count)}{_pieces_label(pieces)}): {rendered}",
                        _withheld_names(sql)))
    return out


def executed_filters_line(statements) -> str:
    """The engine line for this turn's executed queries, or ``""``.

    ``statements`` are the turn's sidecar rows (``{"query_text", "row_count"}``)
    in run order; a bare SQL string is accepted with no count. Each data query
    that has a WHERE clause is listed with its ordinal and row count. When more
    queries ran than fit, the most recent are kept and the earlier ones are
    counted. A name that is not a plain identifier (``engine_names``) is shown
    as :data:`WITHHELD_NAME`, and the line says how many it withheld. Never
    longer than :data:`MAX_LINE_CHARS`; no line breaks.
    """
    numbered = _numbered(statements)
    if not numbered:
        return ""
    entries = [entry for entry, _withheld in numbered]
    shown = entries[-MAX_STATEMENTS:]
    while True:
        omitted = len(entries) - len(shown)
        withheld = set().union(*(names for _entry, names in numbered[omitted:]))
        tail = (f" (and {omitted} earlier {'query' if omitted == 1 else 'queries'} "
                "not shown)") if omitted else ""
        note = f".{count_sentence(len(withheld))}" if withheld else ""
        line = f"{LINE_PREFIX}{' | '.join(shown)}{tail}{note}"
        if len(line) <= MAX_LINE_CHARS or len(shown) == 1:
            break
        shown = shown[1:]
    if len(line) <= MAX_LINE_CHARS:
        return line
    # One query alone is over the cap: cut it, and say the filters are incomplete
    # so nothing downstream reads the shown predicates as the whole scope.
    return _clip(line, MAX_LINE_CHARS - len(INCOMPLETE_MARK)) + INCOMPLETE_MARK
