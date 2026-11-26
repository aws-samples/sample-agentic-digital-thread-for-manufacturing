# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared, engine-agnostic SQL-safety helpers.

Extracted so every SQL-issuing tool (the PostgreSQL ``_make_query_tool`` and the
Athena tools) enforces the SAME read-only + row-cap guarantees from ONE place,
rather than each connector re-implementing them.

Two guarantees:
  * :func:`apply_row_cap` — never return more than ``max_rows`` rows (injects or
    lowers a trailing ``LIMIT``). Keeps result payloads bounded regardless of
    engine.
  * :func:`ensure_select_only` — reject anything that is not a read-only
    statement, and calls to functions with an effect outside the query
    (:data:`DENIED_FUNCTIONS`). PostgreSQL also enforces read-only at the
    driver via ``conn.set_session(readonly=True)``, and a system agent's
    login can read its own schema only; Athena has no read-only session, so
    the guard is the ONLY thing standing between the LLM-generated SQL and a
    mutating statement. Fail-closed: unknown/compound statements are rejected.
"""
from __future__ import annotations

import re

from .athena_scope import _matching_close, tokenize

MAX_ROWS = 500

# Leading keywords that denote a read-only statement. Anything else
# (INSERT/UPDATE/DELETE/MERGE/CREATE/DROP/ALTER/GRANT/…​ and multi-statement
# batches) is rejected fail-closed.
#
# NOTE: EXPLAIN is deliberately EXCLUDED. On Trino/Athena engine v3,
# ``EXPLAIN ANALYZE <statement>`` EXECUTES the wrapped statement to gather
# runtime stats — so allowing an EXPLAIN prefix would let
# ``EXPLAIN ANALYZE DELETE/INSERT ...`` slip past this guard and mutate data.
# The agent does not need EXPLAIN for data retrieval, so it is simply not allowed.
_READ_ONLY_PREFIXES = ("select", "with", "show", "describe", "desc")

# Keywords that begin a read once a leading WITH chain has been consumed: the
# governing statement (and every CTE body) must start with one of these, or
# with a nested WITH that itself satisfies this rule, or an opening
# parenthesis that leads to one. Anything else (INSERT/UPDATE/DELETE/MERGE,
# any DDL, or an unknown word) is a write or unreadable and is rejected,
# because PostgreSQL runs a data-modifying CTE body and a write after a chain.
_READ_BODY_KEYWORDS = frozenset({"select", "values", "table", "with"})


def _word(token) -> str:
    return token.value.lower() if token.kind == "word" else ""


def _first_code_index(tokens: list, start: int, end: int):
    """Index of the first token in ``tokens[start:end]`` that is not an opening
    parenthesis, or None when the slice holds only ``(`` / is empty. Comments
    and whitespace are not tokens, so they are already skipped."""
    return next((i for i in range(start, end) if tokens[i].text != "("), None)


def _require_read_at(tokens: list, start: int, end: int, where: str) -> None:
    """The slice ``tokens[start:end]`` must begin (after opening parentheses)
    with a read keyword, or with a nested WITH that satisfies the same rule."""
    index = _first_code_index(tokens, start, end)
    keyword = _word(tokens[index]) if index is not None else ""
    if keyword == "with":
        _ensure_chain_is_read(tokens, index, end)
    elif keyword not in _READ_BODY_KEYWORDS:
        raise ValueError(
            f"only read statements are allowed {where} "
            f"(SELECT, VALUES, TABLE); got {keyword.upper() or '<none>'}"
        )


_UNREADABLE_CHAIN = "WITH chain could not be read with certainty"

# Punctuation a CYCLE mark constant may carry (a cast, a qualified type name,
# a signed number); parentheses and every other symbol are refused.
_MARK_PUNCT = frozenset({":", ".", "-", "+"})

# Words that begin a statement (or a statement-shaped clause) in PostgreSQL or
# Trino. None can appear in a CYCLE mark constant, which is a literal value.
_STATEMENT_WORDS = frozenset({
    "select", "with", "values", "table", "insert", "update", "delete", "merge",
    "upsert", "create", "drop", "alter", "truncate", "copy", "call", "execute",
    "exec", "do", "grant", "revoke", "lock", "set", "reset", "refresh", "vacuum",
    "analyze", "comment", "prepare", "deallocate", "listen", "notify", "load",
    "import", "discard", "begin", "commit", "rollback", "returning", "from", "into",
})


class _Cursor:
    """A position in ``tokens[:end]`` at one parenthesis depth, for reading the
    fixed words and names of a recursive CTE's SEARCH / CYCLE clause."""

    def __init__(self, tokens: list, index: int, end: int, depth: int):
        self.tokens, self.index, self.end, self.depth = tokens, index, end, depth

    def peek_word(self) -> str:
        if self.index < self.end and self.tokens[self.index].depth == self.depth:
            return _word(self.tokens[self.index])
        return ""

    def take(self, *kinds: str) -> None:
        """Consume one token at this depth of one of ``kinds``, else reject."""
        if not (self.index < self.end and self.tokens[self.index].depth == self.depth
                and self.tokens[self.index].kind in kinds):
            raise ValueError(_UNREADABLE_CHAIN)
        self.index += 1

    def word(self, *words: str) -> None:
        if self.peek_word() not in words:
            raise ValueError(_UNREADABLE_CHAIN)
        self.index += 1

    def names(self) -> None:
        """One or more column names separated by commas (a SEARCH or CYCLE
        list is always followed by SET, so a comma here never ends the CTE)."""
        self.take("word", "identifier")
        while self.index < self.end and self.tokens[self.index].text == "," \
                and self.tokens[self.index].depth == self.depth:
            self.index += 1
            self.take("word", "identifier")


    def constant(self, stop: str) -> None:
        """A CYCLE mark value, up to the ``stop`` word: any run of words,
        literals, numbers, quoted names and ``:`` ``.`` ``-`` ``+`` (so a typed
        constant such as ``INTERVAL '1-2' YEAR TO MONTH`` or a cast such as
        ``'Y'::pg_catalog.text``), but never a parenthesis or a statement
        keyword, so no statement can sit inside it. Rather than recognise
        every constant form, it refuses anything a statement would need."""
        start = self.index
        while self.index < self.end and self.peek_word() != stop:
            token = self.tokens[self.index]
            if token.depth != self.depth or _word(token) in _STATEMENT_WORDS or not (
                    token.kind in ("literal", "number", "word", "identifier")
                    or token.text in _MARK_PUNCT):
                raise ValueError(_UNREADABLE_CHAIN)
            self.index += 1
        if self.index == start or self.peek_word() != stop:
            raise ValueError(_UNREADABLE_CHAIN)


def _skip_search_cycle(tokens: list, index: int, end: int, depth: int) -> int:
    """Index just past a recursive CTE's optional SQL-standard clauses (both
    read only, PostgreSQL 14+), read word by word so nothing else is skipped:
    ``SEARCH {DEPTH|BREADTH} FIRST BY col[, ...] SET col`` then
    ``CYCLE col[, ...] SET col [TO value DEFAULT value] USING col``."""
    cur = _Cursor(tokens, index, end, depth)
    if cur.peek_word() == "search":
        cur.word("search")
        cur.word("depth", "breadth")
        cur.word("first")
        cur.word("by")
        cur.names()
        cur.word("set")
        cur.take("word", "identifier")
    if cur.peek_word() == "cycle":
        cur.word("cycle")
        cur.names()
        cur.word("set")
        cur.take("word", "identifier")
        if cur.peek_word() == "to":
            cur.word("to")
            cur.constant("default")
            cur.word("default")
            cur.constant("using")
        cur.word("using")
        cur.take("word", "identifier")
    return cur.index


def _chain_bodies(tokens: list, with_index: int, scope_end: int):
    """Walk one WITH chain starting at ``with_index`` (depth = that token's).
    Yield each CTE body as ``(body_open, body_close)`` and return the index at
    which the governing statement begins (just past the last body). Reject a
    chain that cannot be read (reuses the tokenizer; no second SQL reader)."""
    depth = tokens[with_index].depth
    index = with_index + 1
    recursive = index < scope_end and _word(tokens[index]) == "recursive"
    if recursive:
        index += 1
    bodies: list[tuple[int, int]] = []
    while True:
        if index >= scope_end or tokens[index].depth != depth \
                or tokens[index].kind not in ("word", "identifier"):
            raise ValueError("WITH chain could not be read with certainty")
        index += 1  # CTE name
        if index < scope_end and tokens[index].text == "(" and tokens[index].depth == depth:
            close = _matching_close(tokens, index)  # optional column list
            if close is None:
                raise ValueError("WITH chain could not be read with certainty")
            index = close + 1
        if index >= scope_end or _word(tokens[index]) != "as":
            raise ValueError("WITH chain could not be read with certainty")
        index += 1
        if index < scope_end and _word(tokens[index]) == "not":
            index += 1
        if index < scope_end and _word(tokens[index]) == "materialized":
            index += 1
        if index >= scope_end or tokens[index].text != "(" or tokens[index].depth != depth:
            raise ValueError("WITH chain could not be read with certainty")
        body_close = _matching_close(tokens, index)
        if body_close is None:
            raise ValueError("WITH chain could not be read with certainty")
        bodies.append((index, body_close))
        index = body_close + 1
        if recursive:  # SEARCH / CYCLE apply only to a recursive chain
            index = _skip_search_cycle(tokens, index, scope_end, depth)
        if index < scope_end and tokens[index].text == "," and tokens[index].depth == depth:
            index += 1
            continue
        return bodies, index


def _ensure_chain_is_read(tokens: list, with_index: int, scope_end: int) -> None:
    """Validate a leading WITH chain: the statement after the chain, and every
    CTE body, must be a read (SELECT / VALUES / TABLE, or a nested WITH that
    itself satisfies this rule). Fail-closed on any chain that cannot be read."""
    bodies, tail_start = _chain_bodies(tokens, with_index, scope_end)
    _require_read_at(tokens, tail_start, scope_end, "after a WITH chain")
    for body_open, body_close in bodies:
        _require_read_at(tokens, body_open + 1, body_close, "in a CTE body")


def _require_read_leading(tokens: list) -> None:
    """The statement's first keyword (after leading ``(``) must be a read-only
    prefix; a leading WITH additionally has its chain and governed statement
    validated so a write hidden behind a CTE chain cannot slip through."""
    first_index = next((i for i, t in enumerate(tokens) if t.text != "("), None)
    first = tokens[first_index] if first_index is not None else None
    keyword = first.value.lower() if first is not None and first.kind == "word" else ""
    if keyword not in _READ_ONLY_PREFIXES:
        raise ValueError(
            f"only read-only statements are allowed ({', '.join(_READ_ONLY_PREFIXES)}); "
            f"got {keyword.upper() or '<none>'}"
        )
    if keyword == "with":
        _ensure_chain_is_read(tokens, first_index, len(tokens))


def _outer_limit_token(sql: str):
    tokens, uncertain = tokenize(sql)
    if uncertain:
        raise ValueError("uncertain SQL tokenization")
    candidates = [
        (index, token) for index, token in enumerate(tokens)
        if token.depth == 0 and token.kind == "word" and token.value.upper() == "LIMIT"
    ]
    if not candidates:
        return tokens, None, None
    index, keyword = candidates[-1]
    value = tokens[index + 1] if index + 1 < len(tokens) else None
    if value is None or value.depth != 0 or value.kind != "number" or not value.text.isdigit():
        raise ValueError("outer LIMIT must be a non-negative integer")
    return tokens, keyword, value


def apply_row_cap(sql: str, max_rows: int = MAX_ROWS) -> tuple[str, bool]:
    """Inject or lower the outermost query LIMIT using structural token offsets."""
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    text = (sql or "").strip()
    if not text:
        raise ValueError("empty SQL statement")
    while text.endswith(";"):
        text = text[:-1].rstrip()
    tokens, _keyword, value = _outer_limit_token(text)
    if value is not None:
        current = int(value.text)
        if current <= max_rows:
            return text, False
        return text[:value.start] + str(max_rows) + text[value.end:], True
    insertion = tokens[-1].end if tokens else len(text)
    return text[:insertion].rstrip() + f" LIMIT {max_rows}" + text[insertion:], True


def ensure_select_only(sql: str) -> str:
    """Return ``sql`` unchanged if it is a single read-only statement, else raise.

    Fail-closed guard, run before every model-written statement (Athena, and
    PostgreSQL in both system-agent modes):
      * the first keyword (after leading comments and opening parentheses)
        must be one of :data:`_READ_ONLY_PREFIXES`;
      * multiple statements are rejected (a ``;`` outside a quoted string or
        comment, followed by more SQL) to block ``SELECT 1; DROP TABLE t``;
      * a call to a function in :data:`DENIED_FUNCTIONS` (or starting with one
        of :data:`DENIED_FUNCTION_PREFIXES`) is rejected however it is
        written (casing, schema qualification, quotes, comments before the
        ``(``, inside an array slice), found by position in the statement's
        code text (:func:`_code_text`), not by how it splits into tokens;
      * a Unicode escape (``U&"..."``, ``u&'...'``) anywhere outside a string,
        quoted name or comment is rejected: it can spell any name;
      * text PostgreSQL could read differently from the tokenizer is
        rejected: a nested block comment, a bare carriage return in a line
        comment, ``--`` inside a number, a dollar-quoted string or ``$n``
        parameter, and an escape string (``E'...'``) with a backslash. An
        ordinary string ending in a backslash reads the same in both, as the
        system-agent session sets ``standard_conforming_strings`` on.
        Backtick names pass: PostgreSQL rejects them, Athena DDL uses them;
      * the ``SELECT ... INTO`` clause is rejected: it creates a table from a
        statement that starts with SELECT (INTO as a name after ``AS`` or
        ``.`` is allowed, see :func:`_refuse_select_into`);
      * SQL whose comments, quoting or parentheses cannot be read with
        certainty is rejected (the row cap rejects it too).

    Raises :class:`ValueError` (never silently rewrites). The database login
    is the real boundary (a system agent's login reads its own schema only);
    this guard is defence in depth.
    """
    if not sql or not sql.strip():
        raise ValueError("empty SQL statement")
    tokens, uncertain = tokenize(sql)
    if not tokens:
        raise ValueError("SQL statement is only comments")
    if uncertain:
        raise ValueError("SQL comments, quoting or parentheses could not be read with certainty")
    _refuse_postgres_lexing(sql, tokens)
    semicolons = [i for i, token in enumerate(tokens) if token.kind == "semicolon"]
    if semicolons and any(t.kind != "semicolon" for t in tokens[semicolons[0] + 1:]):
        raise ValueError("multiple SQL statements are not allowed")
    _require_read_leading(tokens)
    _refuse_select_into(tokens)
    _refuse_denied_functions(sql, tokens)
    return sql


def _refuse_select_into(tokens: list) -> None:
    """``SELECT ... INTO [TEMP|TEMPORARY|UNLOGGED] [TABLE] target`` creates a
    table, so it is a write even though the statement starts with SELECT.

    An INTO word token is that clause unless it is a name: right after ``AS``
    (a column label, ``SELECT 1 AS into``) or right after ``.`` (a field,
    ``t.into``). Every other INTO word is treated as the clause, fail-closed,
    at any depth, so INTO inside a CTE body or subquery is refused too. A
    string, quoted name or comment holding the word is not a word token."""
    for index, token in enumerate(tokens):
        if _word(token) != "into":
            continue
        before = tokens[index - 1] if index else None
        if before is not None and (_word(before) == "as" or before.text == "."):
            continue
        raise ValueError("SELECT ... INTO creates a table and is not allowed")


#: Functions model-written SQL is never allowed to call. None is needed to
#: read data; each has an effect outside the query or reads outside the
#: tables: ending or cancelling other sessions, reloading or rotating server
#: state, reading or listing server files, moving large objects in or out of
#: the server, reaching another database, holding advisory locks other
#: sessions wait on, changing session settings, running SQL held in a text
#: value, or sending notifications. ``pg_sleep`` is refused too: it has no
#: analytical use, holds a connection and a server process until the statement
#: timeout, and is the usual timing probe in SQL injection.
DENIED_FUNCTIONS = frozenset({
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "pg_read_file", "pg_read_binary_file", "pg_stat_file", "set_config",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_notify", "pg_switch_wal", "pg_create_restore_point", "pg_promote",
    "pg_log_backend_memory_contexts", "pg_file_write",
    "ts_stat", "ts_rewrite",
})
#: Name prefixes refused the same way: dblink (another database), advisory
#: locks, server directory listings, large-object server I/O, and the XML
#: functions that run a query held in a text value.
DENIED_FUNCTION_PREFIXES = (
    "dblink", "pg_advisory", "pg_try_advisory", "pg_ls_", "lo_",
    "query_to_xml", "cursor_to_xml",
)


_IDENT_CHAR = "A-Za-z0-9_$"
#: A call of a denied name in code text: not preceded by a name character,
#: optionally quoted, then only whitespace (comments are blanked) before "(".
_DENIED_CALL = re.compile(
    rf'(?<![{_IDENT_CHAR}])["`]?(?:'
    rf'(?:{"|".join(re.escape(n) for n in sorted(DENIED_FUNCTIONS, key=len, reverse=True))})'
    rf'(?![{_IDENT_CHAR}])'
    rf'|(?:{"|".join(re.escape(p) for p in DENIED_FUNCTION_PREFIXES)})[{_IDENT_CHAR}]*'
    r')["`]?\s*\(',
    re.IGNORECASE,
)
#: PostgreSQL's Unicode-escape prefix: U or u, "&", then a quote at once.
_UNICODE_ESCAPE = re.compile(r"""[uU]&["'`]""")


def _code_text(sql: str, tokens, *, hide_quoted_names: bool) -> str:
    """``sql`` with comments, whitespace and the inside of every string
    literal turned into spaces, character positions kept. With
    ``hide_quoted_names`` the inside of double-quoted names is hidden too.
    Backtick text always stays visible: PostgreSQL reads it as code. The
    checks that read this text match by position, so how the tokenizer
    splits code into tokens cannot hide a name from them."""
    chars = [" "] * len(sql)
    for token in tokens:
        hide = token.kind == "literal" or (
            hide_quoted_names and token.kind == "identifier" and token.text.startswith('"'))
        if hide and token.end - token.start >= 2:
            chars[token.start] = sql[token.start]
            chars[token.end - 1] = sql[token.end - 1]
        else:
            chars[token.start:token.end] = sql[token.start:token.end]
    return "".join(chars)


def _refuse_postgres_lexing(sql: str, tokens) -> None:
    """Refuse tokens where PostgreSQL reads a string that the tokenizer reads
    otherwise, so a call could hide in what the tokenizer takes for a string:
    a dollar-quoted string or ``$n`` parameter, and an escape string
    (``E'...'``, the ``E`` right before the quote) that contains a backslash.
    An ordinary string reads the same in both while
    ``standard_conforming_strings`` is on, which the system-agent session
    sets. Amazon Athena accepts neither refused form."""
    for token in tokens:
        if token.kind == "word" and token.value.startswith("$"):
            raise ValueError("dollar-quoted strings and $n parameters are not allowed")
        if (token.kind == "literal" and token.start > 0 and sql[token.start - 1] in "eE"
                and "\\" in token.text):
            raise ValueError("escape strings (E'...') with a backslash are not allowed")


def _refuse_denied_functions(sql: str, tokens) -> None:
    """Refuse a Unicode escape outside strings, quoted names and comments,
    then a denied call found by position in the code text, then a denied
    word or quoted name the tokenizer reads right before a ``(``."""
    if _UNICODE_ESCAPE.search(_code_text(sql, tokens, hide_quoted_names=True)):
        raise ValueError("Unicode-escape identifiers and strings are not allowed")
    match = _DENIED_CALL.search(_code_text(sql, tokens, hide_quoted_names=False))
    if match:
        name = match.group(0).rstrip("(").strip().strip('"`').lower()
        raise ValueError(f"function {name} is not allowed in a read-only query")
    for index, token in enumerate(tokens):
        if token.kind not in ("word", "identifier") or index + 1 >= len(tokens):
            continue
        if tokens[index + 1].text != "(":
            continue
        name = token.value.lower()
        if name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES):
            raise ValueError(f"function {name} is not allowed in a read-only query")
