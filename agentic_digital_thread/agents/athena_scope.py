# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Conservative Athena partition-scope proof.

This is intentionally a small recognizer, not a general SQL parser. It executes
complex SQL unchanged only when every discovered physical partitioned scan has a
provably restricting WHERE predicate. Any uncertain parse or metadata lookup
fails closed; only a narrow one-table SELECT is eligible for auto-scoping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    value: str
    start: int
    end: int
    depth: int


@dataclass
class Relation:
    parts: tuple[str, ...]
    quoted: bool
    alias: str
    depth: int
    select_index: int
    table_token_index: int

    @property
    def table(self) -> str:
        return self.parts[-1]

    @property
    def identity(self) -> str:
        return ".".join(self.parts)


@dataclass(frozen=True)
class CteBinding:
    name: str
    quoted: bool
    declaration_index: int
    body_open: int
    body_close: int
    scope_depth: int
    scope_end: int
    recursive: bool


_RESERVED_AFTER_RELATION = {
    "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "ON", "USING",
    "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT",
    "WINDOW", "QUALIFY", "FETCH", "TABLESAMPLE", "LATERAL", "FOR",
}
_SCOPE_END = {"GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT", "FETCH"}


def _skip_layout(sql: str, index: int) -> tuple[int, bool]:
    """Skip whitespace or one comment; malformed or ambiguous comments are uncertain.

    A comment PostgreSQL and Amazon Athena (Trino) could end in different
    places is marked uncertain rather than read by either engine's rules:
    a ``/*`` inside a block comment (PostgreSQL nests block comments, Trino
    does not) and a carriage return inside a ``--`` comment that is not
    part of a CRLF line ending (both engines end the comment there).
    Callers fail closed on uncertain SQL, so this only ever refuses more."""
    n = len(sql)
    if sql[index].isspace():
        index += 1
        while index < n and sql[index].isspace():
            index += 1
        return index, False
    pair = sql[index:index + 2]
    if pair == "--":
        newline = sql.find("\n", index + 2)
        end = n if newline < 0 else newline
        bare_cr = any(sql[i] == "\r" and i + 1 < n and sql[i + 1] != "\n"
                      for i in range(index + 2, end))
        return (n if newline < 0 else newline + 1), bare_cr
    if pair == "/*":
        close = sql.find("*/", index + 2)
        nested = close >= 0 and sql.find("/*", index + 2, close + 1) >= 0
        return (n if close < 0 else close + 2), close < 0 or nested
    return index, False


def _quoted_token(sql: str, index: int, depth: int) -> tuple[Token, int, bool]:
    quote = sql[index]
    start = index
    index += 1
    value_parts: list[str] = []
    while index < len(sql):
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                value_parts.append(quote)
                index += 2
                continue
            index += 1
            kind = "literal" if quote == "'" else "identifier"
            value = "literal" if kind == "literal" else "".join(value_parts)
            return Token(kind, sql[start:index], value, start, index, depth), index, False
        value_parts.append(sql[index])
        index += 1
    kind = "literal" if quote == "'" else "identifier"
    value = "literal" if kind == "literal" else "".join(value_parts)
    return Token(kind, sql[start:index], value, start, index, depth), index, True


def _bind_or_number_token(sql: str, index: int, depth: int) -> tuple[Token | None, int]:
    # A ":" is always its own token (an array slice "[1:f(x)]", a "::" cast);
    # reading ":name" as one bind token hid the name from every check that
    # looks at words. Model-written SQL has no named parameters.
    ch = sql[index]
    nxt = sql[index + 1] if index + 1 < len(sql) else ""
    if ch == "?":
        return Token("bind", ch, ch, index, index + 1, depth), index + 1
    if ch.isdigit() or (ch in "+-" and nxt.isdigit()):
        end = index + 1
        while end < len(sql) and (sql[end].isdigit() or sql[end] in ".eE+-"):
            end += 1
        return Token("number", sql[index:end], sql[index:end], index, end, depth), end
    return None, index


def _word_or_symbol_token(sql: str, index: int, depth: int) -> tuple[Token, int]:
    ch = sql[index]
    if ch.isalpha() or ch in "_$":
        end = index + 1
        while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
            end += 1
        text = sql[index:end]
        return Token("word", text, text, index, end, depth), end
    pair = sql[index:index + 2]
    if pair in {"<=", ">=", "!=", "<>"}:
        return Token("operator", pair, pair, index, index + 2, depth), index + 2
    kind = "operator" if ch in "=<>" else "punct"
    return Token(kind, ch, ch, index, index + 1, depth), index + 1


def tokenize(sql: str) -> tuple[list[Token], bool]:
    tokens: list[Token] = []
    depth = index = 0
    uncertain = False
    while index < len(sql):
        skipped, malformed = _skip_layout(sql, index)
        if skipped != index:
            index = skipped
            uncertain = uncertain or malformed
            continue
        ch = sql[index]
        if ch in {"'", '"', '`'}:
            token, index, malformed = _quoted_token(sql, index, depth)
            tokens.append(token)
            uncertain = uncertain or malformed
            continue
        if ch in "()":
            if ch == ")":
                depth -= 1
                if depth < 0:
                    depth, uncertain = 0, True
            tokens.append(Token("punct", ch, ch, index, index + 1,
                                depth if ch == ")" else depth))
            depth += 1 if ch == "(" else 0
            index += 1
            continue
        if ch == ";":
            tokens.append(Token("semicolon", ch, ch, index, index + 1, depth))
            index += 1
            continue
        token, next_index = _bind_or_number_token(sql, index, depth)
        if token is None:
            token, next_index = _word_or_symbol_token(sql, index, depth)
        # "1--x" is the number 1 and then a line comment to both engines; the
        # number scan would read on past the "--", so the statement is uncertain.
        uncertain = uncertain or (token.kind == "number" and "--" in token.text)
        tokens.append(token)
        index = next_index
    return tokens, uncertain or depth != 0


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else token.value


def _matching_close(tokens: list[Token], open_index: int) -> int | None:
    if open_index >= len(tokens) or tokens[open_index].text != "(":
        return None
    depth = tokens[open_index].depth
    for i in range(open_index + 1, len(tokens)):
        if tokens[i].text == ")" and tokens[i].depth == depth:
            return i
    return None


def _query_scope_end(tokens: list[Token], with_index: int) -> int:
    depth = tokens[with_index].depth
    return next(
        (i for i in range(with_index + 1, len(tokens)) if tokens[i].depth < depth),
        len(tokens),
    )


def _parse_with_bindings(tokens: list[Token], with_index: int) -> tuple[list[CteBinding], bool]:
    """Parse one WITH chain; binding visibility begins after each CTE body."""
    depth = tokens[with_index].depth
    scope_end = _query_scope_end(tokens, with_index)
    i = with_index + 1
    recursive = i < len(tokens) and _upper(tokens[i]) == "RECURSIVE"
    if recursive:
        i += 1
    bindings: list[CteBinding] = []
    while i < scope_end:
        declaration = i
        if tokens[i].depth != depth or tokens[i].kind not in {"word", "identifier"}:
            return bindings, True
        name_token = tokens[i]
        i += 1
        # Optional CTE output-column list: alias(col_a, col_b) AS (...).
        if i < scope_end and tokens[i].text == "(" and tokens[i].depth == depth:
            close = _matching_close(tokens, i)
            if close is None:
                return bindings, True
            i = close + 1
        if i >= scope_end or _upper(tokens[i]) != "AS":
            return bindings, True
        i += 1
        if i >= scope_end or tokens[i].text != "(" or tokens[i].depth != depth:
            return bindings, True
        body_open = i
        body_close = _matching_close(tokens, body_open)
        if body_close is None:
            return bindings, True
        bindings.append(CteBinding(
            name=name_token.value,
            quoted=name_token.kind == "identifier",
            declaration_index=declaration,
            body_open=body_open,
            body_close=body_close,
            scope_depth=depth,
            scope_end=scope_end,
            recursive=recursive,
        ))
        i = body_close + 1
        if i < scope_end and tokens[i].text == "," and tokens[i].depth == depth:
            i += 1
            continue
        break
    return bindings, False


#: Words after which a query, and so a WITH clause, can begin: ``AS`` of
#: ``CREATE TABLE ... AS`` / ``CREATE VIEW ... AS`` and ``EXPLAIN [ANALYZE]
#: [VERBOSE]``. (A CTE body, a derived table and an IN / EXISTS / scalar
#: subquery open with ``(``.)
_QUERY_FOLLOWS = frozenset({"AS", "EXPLAIN", "ANALYZE", "VERBOSE"})


def _is_time_zone_type(tokens: list[Token], index: int) -> bool:
    """True when the WITH at ``index`` opens ``WITH [LOCAL] TIME ZONE``: the
    tail of a type such as ``timestamp(6) with time zone``, never a clause."""
    words = [_upper(t) if t.kind == "word" else "" for t in tokens[index + 1:index + 4]]
    return words[:2] == ["TIME", "ZONE"] or words[:3] == ["LOCAL", "TIME", "ZONE"]


def _opens_with_clause(tokens: list[Token], index: int) -> bool:
    """True when the word WITH at ``index`` can open a WITH clause: where a
    statement or a parenthesised query can begin (the first token, after
    ``(`` or ``;``, or after a word in :data:`_QUERY_FOLLOWS`), and not the
    ``WITH [LOCAL] TIME ZONE`` of a type. Any other WITH is part of what
    precedes it (a type's time zone, ``UNNEST(...) WITH ORDINALITY``), so it
    neither binds a name nor makes the chain unreadable."""
    token = tokens[index]
    if token.kind != "word" or token.value.upper() != "WITH" or _is_time_zone_type(tokens, index):
        return False
    if index == 0:
        return True
    before = tokens[index - 1]
    return (before.text == "(" or before.kind == "semicolon"
            or (before.kind == "word" and before.value.upper() in _QUERY_FOLLOWS))


def _cte_bindings(tokens: list[Token]) -> tuple[list[CteBinding], bool]:
    """Every WITH chain's bindings (:func:`_parse_with_bindings`), and whether
    any chain could not be read. Only a WITH that can open a clause
    (:func:`_opens_with_clause`) is read as one."""
    bindings: list[CteBinding] = []
    uncertain = False
    for index, token in enumerate(tokens):
        if not _opens_with_clause(tokens, index):
            continue
        parsed, malformed = _parse_with_bindings(tokens, index)
        bindings.extend(parsed)
        uncertain = uncertain or malformed
    return bindings, uncertain


def _binding_name_matches(binding: CteBinding, table_token: Token) -> bool:
    if binding.quoted or table_token.kind == "identifier":
        return binding.quoted == (table_token.kind == "identifier") and binding.name == table_token.value
    return binding.name.lower() == table_token.value.lower()


def _cte_reference_status(
    parts: list[str], table_token: Token, table_index: int, bindings: list[CteBinding]
) -> str:
    """Return physical, logical, or ambiguous for an unqualified table reference."""
    if len(parts) != 1:
        return "physical"
    candidates = [binding for binding in bindings if _binding_name_matches(binding, table_token)]
    visible: list[CteBinding] = []
    for binding in candidates:
        if table_index >= binding.scope_end:
            continue
        if binding.body_open < table_index < binding.body_close:
            if binding.recursive:
                # Recursive self-reference is logical by SQL semantics, but a
                # same-named physical relation cannot be distinguished safely by
                # this recognizer. Fail closed and retain it as a candidate scan.
                return "ambiguous"
            continue
        if table_index > binding.body_close:
            visible.append(binding)
    return "logical" if visible else "physical"


def _nearest_select(tokens: list[Token], relation_index: int, depth: int) -> int | None:
    for i in range(relation_index - 1, -1, -1):
        if tokens[i].depth == depth and _upper(tokens[i]) == "SELECT":
            return i
        if tokens[i].depth < depth:
            break
    return None


def _relation_name(tokens: list[Token], start: int, depth: int):
    if start >= len(tokens):
        return None, start, True
    if tokens[start].text == "(":
        return None, start, False  # derived table; inner scans are found separately
    if tokens[start].kind not in {"word", "identifier"}:
        return None, start, True
    parts = [tokens[start].value]
    quoted = tokens[start].kind == "identifier"
    index = start + 1
    while (index + 1 < len(tokens) and tokens[index].text == "."
           and tokens[index + 1].kind in {"word", "identifier"}):
        parts.append(tokens[index + 1].value)
        quoted = quoted or tokens[index + 1].kind == "identifier"
        index += 2
    invalid = len(parts) > 3 or (
        index < len(tokens) and tokens[index].text == "("
        and tokens[index].depth == depth
    )
    return (parts, quoted) if not invalid else None, index, invalid


#: Words that are a whole time value on their own in a snapshot expression.
_TIME_WORDS = frozenset({"CURRENT_TIMESTAMP", "CURRENT_DATE", "LOCALTIMESTAMP", "NOW"})
#: Type words that, followed by a string literal, make a typed literal.
_TYPED_LITERAL_WORDS = frozenset({"TIMESTAMP", "DATE", "TIME", "INTERVAL"})


def _past_primary(tokens: list[Token], index: int) -> int | None:
    """The index after one primary of a snapshot expression at ``index``: a
    number, a string literal, a typed literal (``TIMESTAMP '...'``,
    ``INTERVAL '1' DAY``), a parenthesised expression, a function call or a
    time word (:data:`_TIME_WORDS`); else ``None``."""
    if index >= len(tokens):
        return None
    token = tokens[index]
    if token.kind in {"number", "literal"}:
        return index + 1
    if token.text == "(":
        close = _matching_close(tokens, index)
        return close + 1 if close is not None else None
    if token.kind != "word":
        return None
    word = _upper(token)
    nxt = tokens[index + 1] if index + 1 < len(tokens) else None
    if word in _TYPED_LITERAL_WORDS and nxt is not None and nxt.kind == "literal":
        end = index + 2
        if word == "INTERVAL" and end < len(tokens) and tokens[end].kind == "word":
            end += 1
        return end
    if nxt is not None and nxt.text == "(" and nxt.depth == token.depth:
        close = _matching_close(tokens, index + 1)
        return close + 1 if close is not None else None
    return index + 1 if word in _TIME_WORDS else None


def _past_snapshot(tokens: list[Token], index: int) -> tuple[int, bool]:
    """``(index after it, malformed)`` for an Iceberg snapshot clause at
    ``index``: ``FOR VERSION AS OF <expr>`` or ``FOR TIMESTAMP AS OF <expr>``,
    the expression one primary (:func:`_past_primary`) or primaries joined by
    ``+`` / ``-``. No ``FOR`` there: ``(index, False)``. A ``FOR`` that does
    not open a readable clause is malformed (never an alias: ``FOR`` is
    reserved)."""
    if index >= len(tokens) or tokens[index].kind != "word" or _upper(tokens[index]) != "FOR":
        return index, False
    words = [_upper(t) if t.kind == "word" else "" for t in tokens[index + 1:index + 4]]
    if words[:1] not in (["VERSION"], ["TIMESTAMP"]) or words[1:3] != ["AS", "OF"]:
        return index, True
    end = _past_primary(tokens, index + 4)
    while end is not None and end < len(tokens) and tokens[end].text in {"+", "-"}:
        end = _past_primary(tokens, end + 1)
    if end is None or not _snapshot_ends_at(tokens, end):
        return index, True
    return end, False


def _relation_boundary(tokens: list[Token], index: int) -> bool:
    """True when ``index`` ends a relation: the end of the statement, a
    closing parenthesis, a comma, a semicolon, or an unquoted word of
    :data:`_RESERVED_AFTER_RELATION` (``WHERE``, ``JOIN``, ``ON``, ...)."""
    if index >= len(tokens):
        return True
    token = tokens[index]
    if token.text in {")", ","} or token.kind == "semicolon":
        return True
    return token.kind == "word" and _upper(token) in _RESERVED_AFTER_RELATION


def _alias_token(token: Token) -> bool:
    """A token that can be a relation's alias: a quoted identifier, or an
    unquoted word that is not in :data:`_RESERVED_AFTER_RELATION`."""
    return token.kind == "identifier" or (
        token.kind == "word" and _upper(token) not in _RESERVED_AFTER_RELATION)


def _snapshot_ends_at(tokens: list[Token], index: int) -> bool:
    """True when the snapshot expression provably ends at ``index``: a
    relation boundary follows (:func:`_relation_boundary`), or ``AS`` and one
    identifier, or one alias (:func:`_alias_token`), and then a boundary.
    Anything else (``AT TIME ZONE``, ``* 3``, ``DAY TO SECOND``, two words)
    may still be the expression, so its end is not proven."""
    if _relation_boundary(tokens, index):
        return True
    if _upper(tokens[index]) == "AS" and tokens[index].kind == "word":
        index += 1
        if index >= len(tokens) or tokens[index].kind not in {"word", "identifier"}:
            return False
    elif not _alias_token(tokens[index]):
        return False
    return _relation_boundary(tokens, index + 1)


def _relation_alias(tokens: list[Token], index: int, depth: int,
                    default: str) -> tuple[str, bool]:
    if index < len(tokens) and _upper(tokens[index]) == "AS":
        index += 1
        if index >= len(tokens) or tokens[index].kind not in {"word", "identifier"}:
            return default, True
        return tokens[index].value, False
    if (index < len(tokens) and tokens[index].depth == depth
            and _alias_token(tokens[index])):
        return tokens[index].value, False
    return default, False


def _is_list_expansion(tokens: list[Token], index: int) -> bool:
    """``UNNEST (`` at ``index``: a list expanded into rows of the relation it
    is joined to. It reads no table (a subquery inside it is a scan of its own,
    found separately), so it is no relation and nothing uncertain."""
    return (index < len(tokens) and tokens[index].kind == "word"
            and _upper(tokens[index]) == "UNNEST" and index + 1 < len(tokens)
            and tokens[index + 1].text == "(")


#: Trino's special forms whose argument list holds a FROM of their own:
#: ``EXTRACT(field FROM x)``, ``SUBSTRING(s FROM start [FOR length])`` and
#: ``TRIM([spec] [chars] FROM s)``.
_FROM_ARGUMENT_FORMS = {"EXTRACT", "SUBSTRING", "TRIM"}


def _is_expression_from(tokens: list[Token], index: int) -> bool:
    """Whether the ``FROM`` at ``index`` is part of an expression, not a FROM
    clause: the first FROM directly inside the argument list of an unquoted
    ``EXTRACT(``, ``SUBSTRING(`` or ``TRIM(`` with no SELECT before it in that
    list. Any other FROM is a clause, so a scan this cannot place stays
    uncertain.

    That FROM sits one level deeper than its query block, so the other SQL
    readers that look for a block's FROM at the block's own depth (the
    state-time and storage-window checks among them) already pass over it.
    ``a IS [NOT] DISTINCT FROM b`` is left a clause on purpose: its FROM is at
    the block's own depth, where those readers would take it for the block's
    FROM clause, so admitting it needs them changed too."""
    token = tokens[index]
    for at in range(index - 1, -1, -1):
        earlier = tokens[at]
        if earlier.depth < token.depth:
            return (earlier.text == "(" and at > 0 and tokens[at - 1].kind == "word"
                    and _upper(tokens[at - 1]) in _FROM_ARGUMENT_FORMS
                    and tokens[at - 1].depth == earlier.depth)
        if earlier.depth == token.depth and _upper(earlier) in {"SELECT", "FROM"}:
            return False
    return False


def _is_clause_from(tokens: list[Token], index: int) -> bool:
    return _upper(tokens[index]) == "FROM" and not _is_expression_from(tokens, index)


def _relation_at(tokens: list[Token], keyword_index: int,
                 bindings: list[CteBinding]) -> tuple[Relation | None, bool]:
    keyword = tokens[keyword_index]
    table_index = keyword_index + 1
    if (_upper(keyword) == "JOIN" and table_index < len(tokens)
            and _is_list_expansion(tokens, table_index)):
        return None, False
    parsed, next_index, invalid = _relation_name(tokens, table_index, keyword.depth)
    if parsed is None:
        return None, invalid
    parts, quoted = parsed
    binding_status = _cte_reference_status(parts, tokens[table_index], table_index, bindings)
    if binding_status == "logical":
        return None, False
    next_index, bad_snapshot = _past_snapshot(tokens, next_index)
    alias, bad_alias = _relation_alias(tokens, next_index, keyword.depth, parts[-1])
    bad_alias = bad_alias or bad_snapshot
    select_index = _nearest_select(tokens, keyword_index, keyword.depth)
    if select_index is None:
        return None, True
    relation = Relation(
        tuple(parts), quoted, alias, keyword.depth, select_index, table_index
    )
    return relation, bad_alias or binding_status == "ambiguous"


_FROM_LIST_END = {
    "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
    "UNION", "INTERSECT", "EXCEPT",
}


def _has_comma_join(tokens: list[Token]) -> bool:
    """Whether a FROM list holds a comma the guard cannot read: any comma at
    the FROM's depth before its next clause, except one right before
    ``UNNEST (`` (Trino's list expansion of the relation before it, read like
    ``CROSS JOIN UNNEST``). An expression FROM (:func:`_is_expression_from`)
    starts no FROM list."""
    for index, token in enumerate(tokens):
        if not _is_clause_from(tokens, index):
            continue
        for at in range(index + 1, len(tokens)):
            following = tokens[at]
            if following.depth < token.depth:
                break
            if following.depth == token.depth and _upper(following) in _FROM_LIST_END:
                break
            if (following.depth == token.depth and following.text == ","
                    and not _is_list_expansion(tokens, at + 1)):
                return True
    return False


def _read_relations(tokens: list[Token]) -> tuple[list[Relation], bool]:
    """The relations, and whether any CTE or relation could not be read (a
    FROM-list comma is not counted here: :func:`relations` adds it). An
    expression FROM (:func:`_is_expression_from`) names no relation."""
    bindings, uncertain = _cte_bindings(tokens)
    found: list[Relation] = []
    for index, token in enumerate(tokens):
        if _upper(token) != "JOIN" and not _is_clause_from(tokens, index):
            continue
        relation, invalid = _relation_at(tokens, index, bindings)
        uncertain = uncertain or invalid
        if relation is not None:
            found.append(relation)
    return found, uncertain


def relations(tokens: list[Token]) -> tuple[list[Relation], bool]:
    found, uncertain = _read_relations(tokens)
    return found, uncertain or _has_comma_join(tokens)


def _past_relation(tokens: list[Token], start: int, depth: int) -> int | None:
    """The index after a plain table name and its optional ``[AS] alias``
    starting at ``start``, else ``None``."""
    parsed, index, invalid = _relation_name(tokens, start, depth)
    if parsed is None or invalid:
        return None
    index, bad_snapshot = _past_snapshot(tokens, index)
    if bad_snapshot:
        return None
    if index < len(tokens) and _upper(tokens[index]) == "AS":
        index += 1
        if index >= len(tokens) or tokens[index].kind not in {"word", "identifier"}:
            return None
        return index + 1
    if (index < len(tokens) and tokens[index].depth == depth
            and _alias_token(tokens[index])):
        return index + 1
    return index


def _rejected_comma_sites(tokens: list[Token]) -> list[tuple[int, int]]:
    """``(FROM index, comma index)`` of each comma in the one shape ``FROM
    <table> [[AS] alias], CROSS JOIN UNNEST (``, at any depth."""
    found = []
    for index, token in enumerate(tokens):
        if token.kind != "word" or not _is_clause_from(tokens, index):
            continue
        comma = _past_relation(tokens, index + 1, token.depth)
        if comma is None or comma + 3 >= len(tokens) or tokens[comma].text != ",":
            continue
        words = tokens[comma:comma + 4]
        if (all(t.depth == token.depth for t in words)
                and [t.kind for t in words[1:3]] == ["word", "word"]
                and [_upper(t) for t in words[1:3]] == ["CROSS", "JOIN"]
                and _is_list_expansion(tokens, comma + 3)):
            found.append((index, comma))
    return found


def _without(text: str, start: int, end: int) -> str:
    """``text`` without ``text[start:end]`` (one comma), a space kept between
    the words it separated."""
    before, after = text[:start], text[end:]
    glue = "" if before[-1:].isspace() or after[:1].isspace() else " "
    return before + glue + after


def drop_comma_before_cross_join(sql: str) -> tuple[str, int]:
    """``sql`` without each comma in the one shape ``FROM <table> [[AS]
    alias], CROSS JOIN UNNEST(...)``, and how many were removed.

    The engine rejects that comma at submit ("mismatched input 'CROSS'"), and
    the query without it is the ``CROSS JOIN UNNEST`` form the guard reads,
    so the caller can run that and say so. Any other comma (``FROM a, b``,
    ``FROM a, b CROSS JOIN UNNEST(...)``), and SQL the tokenizer reads as
    uncertain, is left as it is."""
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return sql, 0
    sites = _rejected_comma_sites(tokens)
    out = sql
    for _from, comma in reversed(sites):
        out = _without(out, tokens[comma].start, tokens[comma].end)
    return out, len(sites)


def drop_trailing_semicolons(sql: str) -> tuple[str, int]:
    """``sql`` as the one statement it holds, without the semicolons that end
    it, and how many were removed.

    A query that is one statement followed only by one or more semicolons,
    with whitespace or comments around them, is that statement. The guard
    reads any semicolon as uncertain (:func:`scope_sql`), so the caller can
    run the statement and say the semicolons were removed. The statement ends
    at its last token: the semicolons and any whitespace or comment around
    them are what is removed. A semicolon anywhere else (two statements, one
    inside a statement, one followed by more SQL), a query with no statement
    before its semicolons, and SQL the tokenizer reads as uncertain are left
    as they are. A semicolon in a string literal, quoted name or comment is
    not a token, so it neither ends the statement nor stops this."""
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return sql, 0
    end = len(tokens)
    while end and tokens[end - 1].kind == "semicolon":
        end -= 1
    removed = len(tokens) - end
    if not removed or not end or any(token.kind == "semicolon" for token in tokens[:end]):
        return sql, 0
    return sql[:tokens[end - 1].end], removed


def comma_before_join_clause(sql: str) -> tuple[str, str] | None:
    """The first FROM clause with a comma right before ``CROSS JOIN UNNEST``
    (the shape :func:`drop_comma_before_cross_join` reads): ``(the clause as
    written, the clause without that comma)``, from ``FROM`` to the next
    clause at its depth (or the end of its scope), else ``None``. For a
    caller that refuses the query and names the fix rather than rewriting."""
    tokens, uncertain = tokenize(sql)
    sites = [] if uncertain else _rejected_comma_sites(tokens)
    if not sites:
        return None
    first, comma = sites[0]
    depth = tokens[first].depth
    stop = next((i for i in range(first + 1, len(tokens))
                 if tokens[i].depth < depth
                 or (tokens[i].depth == depth and _upper(tokens[i]) in _FROM_LIST_END)), None)
    begin = tokens[first].start
    end = tokens[stop].start if stop is not None else len(sql)
    clause = sql[begin:end].rstrip()
    corrected = _without(clause, tokens[comma].start - begin, tokens[comma].end - begin)
    return clause, corrected


def _scope_end(tokens: list[Token], select_index: int, depth: int) -> int:
    for i in range(select_index + 1, len(tokens)):
        token = tokens[i]
        if token.depth < depth:
            return i
        if token.depth == depth and i != select_index and _upper(token) == "SELECT":
            return i
        if token.depth == depth and _upper(token) in _SCOPE_END:
            return i
    return len(tokens)


def _where_expression(tokens: list[Token], relation: Relation) -> list[Token]:
    end = _scope_end(tokens, relation.select_index, relation.depth)
    where = None
    for i in range(relation.select_index + 1, end):
        if tokens[i].depth == relation.depth and _upper(tokens[i]) == "WHERE":
            where = i + 1
            break
    return tokens[where:end] if where is not None else []


def _strip_outer(tokens: list[Token]) -> list[Token]:
    current = tokens
    while len(current) >= 2 and current[0].text == "(" and current[-1].text == ")":
        close = _matching_close(current, 0)
        if close != len(current) - 1:
            break
        current = current[1:-1]
    return current


def _split_boolean(tokens: list[Token], keyword: str) -> list[list[Token]]:
    tokens = _strip_outer(tokens)
    if not tokens:
        return [tokens]
    base = min(token.depth for token in tokens)
    parts: list[list[Token]] = []
    start = 0
    between_pending = False
    for i, token in enumerate(tokens):
        if token.depth != base:
            continue
        upper = _upper(token)
        if upper == "BETWEEN":
            between_pending = True
            continue
        if keyword == "AND" and upper == "AND" and between_pending:
            between_pending = False
            continue
        if upper == keyword:
            parts.append(tokens[start:i])
            start = i + 1
    if parts:
        parts.append(tokens[start:])
        return parts
    return [tokens]


def _column_ref(tokens: list[Token], relation: Relation, partition_columns: set[str], multi_scan: bool) -> bool:
    tokens = _strip_outer(tokens)
    if not tokens or len(tokens) > 3:
        return False
    if len(tokens) == 1 and tokens[0].kind in {"word", "identifier"}:
        return not multi_scan and tokens[0].value.lower() in partition_columns
    if (len(tokens) == 3 and tokens[0].kind in {"word", "identifier"}
            and tokens[1].text == "." and tokens[2].kind in {"word", "identifier"}):
        qualifier = tokens[0].value.lower()
        allowed_qualifiers = (
            {relation.alias.lower()}
            if relation.alias.lower() != relation.table.lower()
            else {relation.table.lower()}
        )
        return (
            qualifier in allowed_qualifiers
            and tokens[2].value.lower() in partition_columns
        )
    return False


def _constant(tokens: list[Token]) -> bool:
    tokens = _strip_outer(tokens)
    if len(tokens) == 1:
        return tokens[0].kind in {"literal", "number", "bind"}
    if (len(tokens) == 2 and tokens[0].kind == "word"
            and _upper(tokens[0]) in {"DATE", "TIME", "TIMESTAMP"}
            and tokens[1].kind == "literal"):
        return True
    return False


def _atom_restricts(tokens: list[Token], relation: Relation,
                    partition_columns: set[str], multi_scan: bool) -> bool:
    tokens = _strip_outer(tokens)
    if not tokens or _upper(tokens[0]) == "NOT":
        return False
    base = min(token.depth for token in tokens)
    # BETWEEN must be checked before generic AND handling.
    for i, token in enumerate(tokens):
        if token.depth == base and _upper(token) == "BETWEEN":
            and_index = next((j for j in range(i + 1, len(tokens))
                              if tokens[j].depth == base and _upper(tokens[j]) == "AND"), None)
            if and_index is None:
                return False
            return (_column_ref(tokens[:i], relation, partition_columns, multi_scan)
                    and _constant(tokens[i + 1:and_index])
                    and _constant(tokens[and_index + 1:]))
    for i, token in enumerate(tokens):
        if token.depth == base and _upper(token) == "IN":
            if not _column_ref(tokens[:i], relation, partition_columns, multi_scan):
                return False
            values = _strip_outer(tokens[i + 1:])
            if any(_upper(item) == "SELECT" for item in values):
                return False
            segments: list[list[Token]] = []
            start = 0
            value_base = min((item.depth for item in values), default=0)
            for j, item in enumerate(values):
                if item.depth == value_base and item.text == ",":
                    segments.append(values[start:j])
                    start = j + 1
            segments.append(values[start:])
            return bool(segments) and all(_constant(segment) for segment in segments)
    for i, token in enumerate(tokens):
        if token.depth != base or token.kind != "operator":
            continue
        if token.text in {"!=", "<>"}:
            return False
        if token.text not in {"=", "<", ">", "<=", ">="}:
            return False
        left, right = tokens[:i], tokens[i + 1:]
        return (
            _column_ref(left, relation, partition_columns, multi_scan) and _constant(right)
        ) or (
            _constant(left) and _column_ref(right, relation, partition_columns, multi_scan)
        )
    return False


def predicate_guards(tokens: list[Token], relation: Relation,
                     partition_columns: list[str], multi_scan: bool) -> bool:
    tokens = _strip_outer(tokens)
    or_parts = _split_boolean(tokens, "OR")
    if len(or_parts) > 1:
        return all(predicate_guards(part, relation, partition_columns, multi_scan) for part in or_parts)
    and_parts = _split_boolean(tokens, "AND")
    if len(and_parts) > 1:
        return any(predicate_guards(part, relation, partition_columns, multi_scan) for part in and_parts)
    return _atom_restricts(tokens, relation, {name.lower() for name in partition_columns}, multi_scan)


def _relation_config(relation: Relation, configured: dict) -> tuple[dict, bool]:
    cfg = dict(configured)
    if len(relation.parts) == 1:
        return cfg, True
    if len(relation.parts) == 2:
        cfg["database"] = relation.parts[0]
        return cfg, True
    if len(relation.parts) == 3:
        cfg["catalog"] = relation.parts[0]
        cfg["database"] = relation.parts[1]
        return cfg, True
    return cfg, False


def _normalized_baked_key(key: str) -> tuple[str, ...]:
    return tuple(part.strip().strip('"`').lower() for part in str(key).split(".") if part.strip())


def _baked_partition_metadata(relation: Relation, baked: dict | None,
                              configured: dict) -> tuple[list[str], bool]:
    relation_key = tuple(part.lower() for part in relation.parts)
    configured_db = str(configured.get("database") or "").lower()
    matches: list[str] = []
    metadata_known = False
    for key, values in (baked or {}).items():
        baked_key = _normalized_baked_key(key)
        applies = baked_key == relation_key
        if len(relation_key) == 1:
            applies = applies or baked_key == (configured_db, relation_key[0])
        if applies:
            metadata_known = True
            matches.extend(str(value) for value in values if str(value).strip())
    return list(dict.fromkeys(matches)), metadata_known


def baked_partition_names(relation: Relation, baked: dict | None,
                          configured: dict) -> list[str]:
    return _baked_partition_metadata(relation, baked, configured)[0]


#: Partition-knowledge detail (and the ``known`` slot of a
#: :data:`PartitionMetadata` item) for a table with HIDDEN partitioning: its
#: storage is partitioned by fields the catalog lists no key for (an Iceberg
#: table partitions by transforms of ordinary columns). Metadata is known, the
#: table has no partition column to check, and it is NOT unpartitioned.
HIDDEN_PARTITIONED = "hidden_partitioned"


def _baked_hidden_fields(relation: Relation, baked: dict | None,
                         configured: dict) -> tuple | None:
    """The partition fields when the baked map marks ``relation`` as hidden
    partitioning (a ``schema_format.HiddenPartitioning`` value), else ``None``."""
    relation_key = tuple(part.lower() for part in relation.parts)
    configured_db = str(configured.get("database") or "").lower()
    for key, values in (baked or {}).items():
        baked_key = _normalized_baked_key(key)
        applies = baked_key == relation_key or (
            len(relation_key) == 1 and baked_key == (configured_db, relation_key[0]))
        fields = getattr(values, "fields", None)
        if applies and isinstance(values, tuple) and isinstance(fields, tuple):
            return fields
    return None


def _catalog_says_hidden(athena_tools: Any, relation: Relation, cfg: dict) -> bool:
    """True when this process's catalog read of the table said hidden
    partitioning (``athena_tools.known_hidden_partitioning``: a cache lookup,
    never I/O). Missing or failing lookups claim nothing."""
    check = getattr(athena_tools, "known_hidden_partitioning", None)
    if not callable(check):
        return False
    try:
        return check(relation.table, cfg) is True
    except Exception:  # noqa: BLE001 - a failed format lookup claims nothing
        return False


def _decision(*, applied: bool, reason: str, relation: Relation | None = None,
              partition_columns: list[str] | None = None,
              partition_metadata: str = "",
              partition_fields: list[str] | None = None,
              uncertain_cause: str = "") -> dict:
    out = {
        "applied": applied,
        "reason": reason,
        "table": relation.identity if relation else "",
        "partition_columns": partition_columns or [],
    }
    # Partition-knowledge detail — serialized ONLY when set so every decision
    # that predates this field stays byte-identical (pinned exact-dict tests).
    # "known_partitioned": the guard KNOWS the partition columns (actionable
    # partition-predicate guidance applies); "known_unpartitioned": metadata
    # WAS read and the table has zero partition columns (guidance must bound
    # the query shape, NEVER advise partition predicates that do not exist);
    # "hidden_partitioned": the table's storage is partitioned by fields that
    # are not columns (no partition predicate to advise, and never called
    # unpartitioned); "unknown": partition metadata could not be read
    # (discovery failed/absent, or no relation could even be identified) — a
    # transient condition the caller may retry, NEVER an instruction to add
    # partition predicates.
    if partition_metadata:
        out["partition_metadata"] = partition_metadata
    if partition_fields:
        out["partition_fields"] = list(partition_fields)
    # Why an uncertain parse was uncertain, when that is one thing the
    # refusal can name (COMMA_JOIN); serialized only when set, as above.
    if uncertain_cause:
        out["uncertain_cause"] = uncertain_cause
    return out


#: ``uncertain_cause`` of a decision whose only uncertainty is a comma in a
#: FROM list (``FROM a, b``): the refusal names it instead of quoting.
COMMA_JOIN = "comma_join"


PartitionMetadata = tuple[Relation, list[str], dict, "bool | str"]


def _partition_knowledge(item: PartitionMetadata) -> str:
    """Honest partition-knowledge detail for an uncertain-parse decision.

    Metadata read + zero partition columns => "known_unpartitioned" (never
    blank: blank routes to partition-predicate guidance for columns that are
    known NOT to exist), unless the table has hidden partitioning."""
    if item[1]:
        return "known_partitioned"
    if item[3] == HIDDEN_PARTITIONED:
        return HIDDEN_PARTITIONED
    return "known_unpartitioned" if item[3] else "unknown"


def _hidden_fields(item: PartitionMetadata, knowledge: str, baked: dict | None,
                   configured: dict) -> list[str]:
    """The baked partition fields for a hidden-partitioned item, else ``[]``."""
    if knowledge != HIDDEN_PARTITIONED:
        return []
    return list(_baked_hidden_fields(item[0], baked, configured) or ())


def _no_partition_column_decision(metadata: list[PartitionMetadata], scans: list[Relation],
                                  baked: dict | None, configured: dict) -> dict:
    """The run-unchanged decision when no scan has a partition column: an honest
    ``hidden_partitioning`` label when one has hidden partitioning (nothing the
    guard can check, as for an unpartitioned table), else ``unpartitioned``."""
    hidden = next((item for item in metadata if item[3] == HIDDEN_PARTITIONED), None)
    if hidden:
        return _decision(applied=False, reason="hidden_partitioning", relation=hidden[0],
                         partition_metadata=HIDDEN_PARTITIONED,
                         partition_fields=_hidden_fields(
                             hidden, HIDDEN_PARTITIONED, baked, configured))
    return _decision(applied=False, reason="unpartitioned", relation=scans[0])


def _load_partition_metadata(scans: list[Relation], configured: dict,
                             baked: dict | None, athena_tools: Any) -> list[PartitionMetadata]:
    metadata: list[PartitionMetadata] = []
    for relation in scans:
        target_cfg, identity_known = _relation_config(relation, configured)
        baked_names, baked_known = _baked_partition_metadata(
            relation, baked, configured
        )
        if baked_known and not baked_names:
            # AFFIRMATIVE "known unpartitioned": the baked schema carried the
            # explicit "(no partition keys)" marker
            # (schema_format.PARTITION_KEYS_ABSENT_MARKER), which onboarding
            # appends ONLY after a COMPLETE (uncapped) physical introspection
            # found zero partition keys. A live information_schema round-trip
            # cannot change that answer — skip it (metadata_known=True) rather
            # than paying it on every query.
            #
            # Because the signal is marker-gated, an UNFLAGGED baked schema (one
            # persisted without physical partition discovery, or truncated at the
            # row cap) never reaches this branch at all: it parses as ABSENT,
            # so live discovery still runs.
            #
            # Residual risk, deliberately accepted (ONE path):
            #   STALE BAKED — the table was repartitioned after onboarding.
            # That degrades to an unbounded scan, exactly as for any genuinely
            # unpartitioned table; the scan budget (resolve_scan_budget) remains
            # the cost guard and QUERY CORRECTNESS is unaffected — no predicate is
            # invented and no rows are hidden. Re-onboarding is the steward's fix
            # (the same contract as schema drift, ADR-0053).
            metadata.append((relation, [], target_cfg, _known_without_columns(
                relation, baked, configured, athena_tools, target_cfg)))
            continue
        metadata_known = identity_known
        try:
            pcols = athena_tools.list_partition_columns(relation.table, target_cfg)
        except Exception:
            pcols, metadata_known = [], False
        runtime_names = [
            str(item.get("name")) for item in pcols
            if isinstance(item, dict) and item.get("name")
        ]
        names = list(dict.fromkeys(runtime_names + baked_names))
        if not names and metadata_known:
            metadata_known = _known_without_columns(
                relation, baked, configured, athena_tools, target_cfg)
        metadata.append((relation, names, target_cfg, metadata_known))
    return metadata


def _known_without_columns(relation: Relation, baked: dict | None, configured: dict,
                           athena_tools: Any, cfg: dict) -> bool | str:
    """The ``known`` slot for a table whose metadata shows no partition column:
    :data:`HIDDEN_PARTITIONED` when the baked map marks hidden partitioning or
    this process's catalog read said so (that also corrects a
    "(no partition keys)" bake made before hidden partitioning was
    recognized; no new I/O), else ``True`` (known unpartitioned)."""
    hidden = (_baked_hidden_fields(relation, baked, configured) is not None
              or _catalog_says_hidden(athena_tools, relation, cfg))
    return HIDDEN_PARTITIONED if hidden else True


def _unguarded_scans(tokens: list[Token], scans: list[Relation],
                     partitioned: list[PartitionMetadata]) -> list[PartitionMetadata]:
    out: list[PartitionMetadata] = []
    for item in partitioned:
        relation, names, _cfg, _known = item
        expression = _where_expression(tokens, relation)
        same_scope_count = sum(
            scan.select_index == relation.select_index and scan.depth == relation.depth
            for scan in scans
        )
        if not predicate_guards(expression, relation, names, same_scope_count > 1):
            out.append(item)
    return out


def _can_auto_scope(tokens: list[Token], scans: list[Relation],
                    unguarded: list[PartitionMetadata]) -> bool:
    # UNNEST is refused like JOIN: a comma list expansion (``t, UNNEST(...)``)
    # gets the decision ``t CROSS JOIN UNNEST(...)`` gets.
    return (
        len(scans) == len(unguarded) == 1
        and not scans[0].quoted
        and scans[0].depth == 0
        and sum(_upper(token) == "SELECT" for token in tokens) == 1
        and not any(_upper(token) in {"UNION", "INTERSECT", "EXCEPT", "JOIN", "UNNEST"}
                    for token in tokens)
        and bool(tokens) and _upper(tokens[0]) == "SELECT"
        and bool(scans[0].alias)
        and all(ch.isalnum() or ch == "_" for ch in scans[0].alias)
    )


def _rewrite_recent_partition(sql: str, item: PartitionMetadata, *, athena_tools: Any,
                              inject_predicate: Callable[[str, str], str | None]):
    relation, names, target_cfg, _known = item
    qualifier = f'"{relation.alias}".' if relation.alias.lower() != relation.table.lower() else ""
    predicate = athena_tools.build_recent_partition_predicate(
        relation.table, target_cfg, qualifier=qualifier
    )
    scoped = inject_predicate(sql, predicate) if predicate else None
    if not scoped:
        return None
    return scoped, _decision(
        applied=True, reason="recent_partition_injected",
        relation=relation, partition_columns=names,
    )


def _uncertain_decision(tokens: list[Token], lexical: bool, metadata: list[PartitionMetadata],
                        baked: dict | None, configured: dict) -> dict:
    """The refusal of a query the guard could not read with certainty: what is
    known of the first (partitioned, else any) scan's partitioning, and
    :data:`COMMA_JOIN` when a FROM-list comma is the only thing it could not
    read (``lexical``: the tokenizer's own uncertainty)."""
    partitioned = [item for item in metadata if item[1]]
    first = partitioned[0] if partitioned else metadata[0]
    knowledge = _partition_knowledge(first)
    comma_only = (not lexical and not any(token.kind == "semicolon" for token in tokens)
                  and _has_comma_join(tokens) and not _read_relations(tokens)[1])
    return _decision(applied=False, reason="scope_required",
                     relation=first[0], partition_columns=first[1],
                     partition_metadata=knowledge,
                     partition_fields=_hidden_fields(first, knowledge, baked, configured),
                     uncertain_cause=COMMA_JOIN if comma_only else "")


def scope_sql(sql: str, connection_config: dict, *, athena_tools: Any,
              inject_predicate: Callable[[str, str], str | None],
              baked_partition_columns: dict | None = None) -> tuple[str, dict]:
    tokens, lexical = tokenize(sql)
    scans, relation_uncertain = relations(tokens)
    uncertain = lexical or relation_uncertain or any(token.kind == "semicolon" for token in tokens)
    if not scans:
        query_like = any(_upper(token) in {"SELECT", "WITH"} for token in tokens)
        if uncertain or query_like:  # no relation => NO partition knowledge
            return sql, _decision(applied=False, reason="scope_required",
                                  partition_metadata="unknown")
        return sql, _decision(applied=False, reason="unpartitioned")

    metadata = _load_partition_metadata(
        scans, connection_config, baked_partition_columns, athena_tools
    )
    partitioned = [item for item in metadata if item[1]]
    if uncertain:
        return sql, _uncertain_decision(tokens, lexical, metadata, baked_partition_columns,
                                        connection_config)
    unknown = next((item for item in metadata if not item[3] and not item[1]), None)
    if unknown:
        return sql, _decision(applied=False, reason="scope_required",
                              relation=unknown[0], partition_metadata="unknown")
    if not partitioned:
        return sql, _no_partition_column_decision(
            metadata, scans, baked_partition_columns, connection_config)

    unguarded = _unguarded_scans(tokens, scans, partitioned)
    if not unguarded:
        first = partitioned[0]
        return sql, _decision(applied=False, reason="explicit_partition_predicate",
                              relation=first[0], partition_columns=first[1])
    if _can_auto_scope(tokens, scans, unguarded):
        rewritten = _rewrite_recent_partition(
            sql, unguarded[0], athena_tools=athena_tools,
            inject_predicate=inject_predicate,
        )
        if rewritten:
            return rewritten
    first = unguarded[0]
    return sql, _decision(applied=False, reason="scope_required",
                          relation=first[0], partition_columns=first[1],
                          partition_metadata="known_partitioned")
