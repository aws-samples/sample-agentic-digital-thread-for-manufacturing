# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only guard for the ERP dashboard's SQL console.

Operator-typed SQL never runs as the dashboard's admin login: it runs on its
own short-lived connection as the console login, a role with SELECT grants
and nothing else (``erp_simulation/console_login.py``). That role is the
security boundary. It stops what a READ ONLY transaction cannot, such as
``pg_terminate_backend``, ``pg_cancel_backend`` or ``set_config('role', ...)``.

This module adds three layers on top:

1. ``exceeds_size_limit`` refuses text over ``MAX_QUERY_BYTES`` (64 KiB of
   UTF-8) before any other check scans or copies it.
2. ``is_single_statement`` accepts exactly one statement. Trailing semicolons,
   whitespace and comments are allowed; anything after a separator is not.
   Without this, ``SELECT 1; COMMIT; ...`` would end the read-only transaction.
3. ``run_readonly_query`` runs that statement in a READ ONLY transaction with
   a statement timeout and ALWAYS rolls back. Cleanup is best effort: it never
   replaces the query's own error, and a connection it cannot prove clean is
   closed.

The dashboard's keyword check only gives an obvious write a clearer first
message.

Stdlib only, no streamlit: the pure functions are unit tested directly.
"""

from __future__ import annotations

import logging
from typing import Any, List, Sequence, Tuple

logger = logging.getLogger(__name__)

# How long one console query may run before PostgreSQL cancels it.
STATEMENT_TIMEOUT_SECONDS = 15

# Longest console text accepted, in bytes of UTF-8 (64 KiB). Longer text is
# refused before it is upper-cased, matched or lexed, so an oversized paste
# costs one length check instead of several full scans and copies.
MAX_QUERY_BYTES = 64 * 1024

# First keyword a console statement may start with. WITH is safe here because
# the READ ONLY transaction refuses a data-modifying CTE (WITH ... DELETE) the
# same way it refuses the bare statement.
ALLOWED_LEADING_KEYWORDS = frozenset({"SELECT", "WITH"})

READ_ONLY_MESSAGE = "Only read-only queries are allowed."
TIMEOUT_MESSAGE = (
    f"The query took longer than {STATEMENT_TIMEOUT_SECONDS} seconds and was stopped."
)
CHECK_SQL_MESSAGE = "Check the SQL: syntax error, or a table or column that does not exist."
PERMISSION_MESSAGE = "The console's read-only login is not allowed to do that."
TOO_LONG_MESSAGE = f"The query is longer than {MAX_QUERY_BYTES // 1024} KiB."
GENERIC_MESSAGE = "Query failed."

# SQLSTATE -> the only text the console shows. Details stay in the server log.
_PGCODE_MESSAGES = {
    "25006": READ_ONLY_MESSAGE,   # read_only_sql_transaction
    "42501": PERMISSION_MESSAGE,  # insufficient_privilege
    "57014": TIMEOUT_MESSAGE,     # query_canceled (statement_timeout)
    "42601": CHECK_SQL_MESSAGE,   # syntax_error
    "42P01": CHECK_SQL_MESSAGE,   # undefined_table
    "42703": CHECK_SQL_MESSAGE,   # undefined_column
}

# ---------------------------------------------------------------- scanner ---
# Character classes from PostgreSQL's lexer (src/backend/parser/scan.l).
# Bytes >= 0x80 are identifier characters there, so any non-ASCII character
# is one here.
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DIGITS = frozenset("0123456789")
_SPACE = frozenset(" \t\n\r\f\v")
_HORIZ_SPACE = frozenset(" \t\f\v")
_NEWLINE = frozenset("\n\r")


class _Unterminated(Exception):
    """A string, identifier, dollar quote or comment never closes."""


def _ident_start(ch: str) -> bool:
    return ch in _ASCII_LETTERS or ch == "_" or ord(ch) >= 0x80


def _ident_cont(ch: str) -> bool:
    return _ident_start(ch) or ch in _DIGITS or ch == "$"


def _dolq_cont(ch: str) -> bool:
    return _ident_start(ch) or ch in _DIGITS


def _skip_line_comment(sql: str, i: int) -> int:
    """``sql[i:i+2] == '--'``; return the index of the ending newline (or len)."""
    n = len(sql)
    while i < n and sql[i] not in _NEWLINE:
        i += 1
    return i


def _skip_block_comment(sql: str, i: int) -> int:
    """``sql[i:i+2] == '/*'``; block comments nest. Return the index after it."""
    depth, i, n = 1, i + 2, len(sql)
    while i < n:
        if sql.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif sql.startswith("*/", i):
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i
        else:
            i += 1
    raise _Unterminated


def _string_continues(sql: str, i: int):
    """After a closing quote at ``i - 1``: PostgreSQL joins ``'a'`` and ``'b'``
    into one string when only whitespace containing a newline (and ``--``
    comments) separates them. Return the index after the next opening quote,
    or None when the string really ended."""
    n = len(sql)
    while i < n:
        if sql[i] in _HORIZ_SPACE:
            i += 1
        elif sql.startswith("--", i):
            i = _skip_line_comment(sql, i)
        else:
            break
    if i >= n or sql[i] not in _NEWLINE:
        return None
    i += 1
    while i < n:
        if sql[i] in _SPACE:
            i += 1
        elif sql.startswith("--", i):
            end = _skip_line_comment(sql, i)
            if end >= n:
                return None
            i = end + 1
        else:
            break
    if i < n and sql[i] == "'":
        return i + 1
    return None


def _skip_string(sql: str, i: int, *, backslash: bool, doubling: bool) -> int:
    """``i`` is just past the opening quote. Return the index after the string.

    ``backslash``: a backslash escapes the next character (E'...' strings, and
    plain strings when standard_conforming_strings is off). ``doubling``: ``''``
    is a quote inside the string (every kind except B'...' and X'...')."""
    n = len(sql)
    while True:
        while i < n:
            ch = sql[i]
            if backslash and ch == "\\":
                i += 2
                continue
            if ch == "'":
                if doubling and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                i += 1
                break
            i += 1
        else:
            raise _Unterminated
        resumed = _string_continues(sql, i)
        if resumed is None:
            return i
        i = resumed


def _skip_quoted_identifier(sql: str, i: int) -> int:
    """``i`` is just past the opening ``"``; ``""`` is a quote inside it."""
    n = len(sql)
    while i < n:
        if sql[i] == '"':
            if i + 1 < n and sql[i + 1] == '"':
                i += 2
                continue
            return i + 1
        i += 1
    raise _Unterminated


def _dollar_tag_end(sql: str, i: int):
    """``sql[i] == '$'``. Return the index after a ``$$`` or ``$tag$`` opener,
    or None (``$1`` is a parameter, a lone ``$`` is just a character)."""
    n = len(sql)
    j = i + 1
    if j < n and sql[j] == "$":
        return j + 1
    if j < n and _ident_start(sql[j]):
        j += 1
        while j < n and _dolq_cont(sql[j]):
            j += 1
        if j < n and sql[j] == "$":
            return j + 1
    return None


def _one_statement(sql: str, plain_backslash: bool) -> bool:
    """One reading of ``sql``: True when nothing but whitespace, comments and
    semicolons follows the first separator. ``plain_backslash`` says whether a
    backslash escapes inside a plain '...' string."""
    i, n = 0, len(sql)
    after_separator = False
    while i < n:
        ch = sql[i]
        if ch in _SPACE:
            i += 1
            continue
        if sql.startswith("--", i):
            i = _skip_line_comment(sql, i)
            continue
        if sql.startswith("/*", i):
            i = _skip_block_comment(sql, i)
            continue
        if ch == ";":
            after_separator = True
            i += 1
            continue
        if after_separator:
            return False  # a second statement starts here
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "'":
            i = _skip_string(sql, i + 1, backslash=plain_backslash, doubling=True)
        elif ch == '"':
            i = _skip_quoted_identifier(sql, i + 1)
        elif ch == "$":
            opener_end = _dollar_tag_end(sql, i)
            if opener_end is None:
                i += 1
                continue
            tag = sql[i:opener_end]
            close = sql.find(tag, opener_end)
            if close < 0:
                raise _Unterminated
            i = close + len(tag)
        elif _ident_start(ch):
            if ch in "eE" and nxt == "'":
                i = _skip_string(sql, i + 2, backslash=True, doubling=True)
            elif ch in "bBxX" and nxt == "'":
                i = _skip_string(sql, i + 2, backslash=False, doubling=False)
            elif ch in "uU" and nxt == "&" and sql[i + 2:i + 3] == "'":
                i = _skip_string(sql, i + 3, backslash=False, doubling=True)
            elif ch in "uU" and nxt == "&" and sql[i + 2:i + 3] == '"':
                i = _skip_quoted_identifier(sql, i + 3)
            else:
                i += 1
                while i < n and _ident_cont(sql[i]):
                    i += 1
        else:
            i += 1
    return True


def exceeds_size_limit(sql: str) -> bool:
    """True when ``sql`` is longer than ``MAX_QUERY_BYTES`` bytes of UTF-8.

    A text with more characters than the cap is over it whatever its encoding,
    so that case is decided without encoding (or otherwise copying) the text.
    """
    if len(sql) > MAX_QUERY_BYTES:
        return True
    return len(sql.encode("utf-8", "surrogatepass")) > MAX_QUERY_BYTES


def is_single_statement(sql: str) -> bool:
    """True when ``sql`` holds exactly one SQL statement for PostgreSQL.

    Understands single-quoted strings ('' escapes, E'...' backslash escapes,
    B/X/U& strings, strings continued across a newline), double-quoted
    identifiers, dollar quotes ($$...$$, $tag$...$tag$), ``--`` comments and
    nested ``/* */`` comments. Trailing semicolons, whitespace and comments are
    allowed. Fails closed: an unterminated string or comment is rejected, and
    so is text over ``MAX_QUERY_BYTES`` (it is not scanned at all).

    A backslash inside a plain '...' string escapes only when the server's
    standard_conforming_strings is off, so the text must be one statement
    under BOTH readings. The check then does not depend on that setting.
    """
    if exceeds_size_limit(sql):
        return False
    try:
        return _one_statement(sql, False) and _one_statement(sql, True)
    except _Unterminated:
        return False


def leading_keyword(sql: str) -> str:
    """The first word of ``sql`` in upper case, after whitespace and comments.

    Returns "" when the text starts with anything other than a word (or a
    comment never closes)."""
    i, n = 0, len(sql)
    try:
        while i < n:
            if sql[i] in _SPACE:
                i += 1
            elif sql.startswith("--", i):
                i = _skip_line_comment(sql, i)
            elif sql.startswith("/*", i):
                i = _skip_block_comment(sql, i)
            else:
                break
    except _Unterminated:
        return ""
    start = i
    while i < n and sql[i] in _ASCII_LETTERS:
        i += 1
    if i < n and _ident_cont(sql[i]):
        return ""  # SELECT_x, SELECT1: not the keyword
    return sql[start:i].upper()


# ---------------------------------------------------------------- execute ---

def run_readonly_query(conn: Any, sql: str) -> Tuple[List[str], Sequence[tuple]]:
    """Run one console statement in a READ ONLY transaction and roll it back.

    ``conn`` is a ``pgdriver`` connection. Returns ``(columns, rows)``. A database
    error propagates unchanged: show the user ``console_error_message(exc)``,
    never ``str(exc)``.

    The rollback runs on success too, so a caller's later commit has nothing
    to commit. It also reverts SET LOCAL and any ``set_config`` the query made.
    Session-level advisory locks survive a rollback, so they are released
    explicitly.

    Cleanup is best effort and never replaces the query's own exception
    (``KeyboardInterrupt`` included). When a cleanup step fails, the
    connection is closed, so a pool discards it instead of reusing it.
    """
    conn.rollback()  # start at a transaction boundary so SET TRANSACTION comes first
    autocommit = conn.autocommit
    if autocommit:
        conn.autocommit = False
    try:
        result = _execute_readonly(conn, sql)
    except BaseException:
        _restore_connection(conn, autocommit)
        raise
    _restore_connection(conn, autocommit)
    return result


def _execute_readonly(conn: Any, sql: str) -> Tuple[List[str], Sequence[tuple]]:
    with conn.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        # SET cannot take a bound parameter; set_config(..., true) is SET LOCAL.
        cursor.execute("SELECT set_config('statement_timeout', %s, true)",
                       (f"{STATEMENT_TIMEOUT_SECONDS}s",))
        cursor.execute(sql)
        if cursor.description is None:
            return [], []
        columns = [desc[0] for desc in cursor.description]
        return columns, cursor.fetchall()


def _unlock_advisory(conn: Any) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock_all()")


def _set_autocommit(conn: Any, value: bool) -> None:
    conn.autocommit = value


def _restore_connection(conn: Any, autocommit: bool) -> None:
    """Roll back, drop advisory locks, restore autocommit. Never raises an
    ``Exception``: the first failed step stops the cleanup and closes ``conn``."""
    steps = [lambda: conn.rollback(), lambda: _unlock_advisory(conn), lambda: conn.rollback()]
    if autocommit:
        steps.append(lambda: _set_autocommit(conn, True))
    for step in steps:
        try:
            step()
        except Exception as exc:  # cleanup must not mask the query's own error
            logger.warning("SQL console: cleanup failed (%s); closing the connection",
                           type(exc).__name__)
            close_quietly(conn)
            return


def close_quietly(conn: Any) -> None:
    """Close ``conn``, ignoring any error (a broken connection may refuse)."""
    try:
        conn.close()
    except Exception:  # nosec B110 - nothing left to clean up on a failed close
        pass


def console_error_message(exc: BaseException) -> str:
    """The text the console shows for a failed query (never the raw error)."""
    return _PGCODE_MESSAGES.get(getattr(exc, "pgcode", None), GENERIC_MESSAGE)
