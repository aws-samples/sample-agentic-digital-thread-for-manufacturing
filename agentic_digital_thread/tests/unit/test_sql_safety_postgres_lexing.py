# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SQL guard reads a statement the way PostgreSQL does, or refuses it.

``ensure_select_only`` finds denied function calls with the shared tokenizer
(``athena_scope.tokenize``). Wherever that tokenizer and the PostgreSQL lexer
can read the same text differently, a denied call could sit in text the
tokenizer treats as a comment or a string while PostgreSQL runs it. The
reader login still has PUBLIC EXECUTE on ``pg_notify``, ``pg_sleep`` and the
advisory-lock functions, so for those the guard is the boundary. Each
difference is closed by refusing the statement, never by modelling one
engine's rules (Amazon Athena does not nest block comments, PostgreSQL does):

* a ``/*`` inside a block comment (PostgreSQL nests block comments);
* a carriage return inside a ``--`` comment that is not part of CRLF
  (PostgreSQL ends the comment there);
* ``--`` inside a number token (``1--x`` starts a comment);
* a dollar-quoted string or ``$n`` parameter;
* an escape string (``E'...'``) that contains a backslash; an ordinary
  string ending in a backslash (``'C:\\'``) reads the same in both, because
  Mode A and Mode B set ``standard_conforming_strings = on``;
* a Unicode-escape name or string (``U&"..."``, ``u&'...'``) anywhere outside
  a string literal, quoted name or comment;
* a denied name after ``:`` (an array slice ``[1:f(x)]``): the tokenizer
  reads the ``:`` on its own, so the name is a word like any other.

Backtick-quoted names pass: PostgreSQL rejects backtick text itself, so
nothing hidden there runs, and Amazon Athena DDL (``DESCRIBE``) uses them.
A denied call written inside backticks is still refused, because the
position-based check reads backtick text as code.

Every case is refused before Mode A or Mode B connects. Ordinary comments,
casts, slices and quoted names keep passing, and the Athena path refuses the
same statements before any Athena call (strictly safer: it never accepts SQL
it used to refuse). ``test_sql_safety_compositions.py`` combines every
spelling with every denied name. ``tests/integration/test_sql_guard_matches_postgres_pg.py``
shows on a real PostgreSQL server that each refused spelling calls the
function.
"""
from __future__ import annotations

import pytest

from agents import athena_tools, system_agent
from agents.athena_scope import tokenize
from agents.sql_safety import DENIED_FUNCTION_PREFIXES, ensure_select_only
from tests.unit._sql_guard_compositions import DENIED_NAMES, PREFIX_EXAMPLES

#: Ways to put a nested block comment between a function name and its "(".
NESTED = (
    "{f} /* outer /* inner */ still outer */ ('a', 'b')",
    "{f}/*a/*b*/c*/('a')",
    "{f} /* a /*/ b */ */ ('a', 'b')",
    "pg_catalog.{f} /* x /* y */ z */ (1)",
)

#: Other PostgreSQL lexing the tokenizer used to read differently. Each puts
#: the call where the tokenizer saw a string or a comment.
OTHER_SPELLINGS = {
    "dollar_quote": "SELECT $$'$$ AS a, {f}('a', 'b') AS b, $$'$$ AS c",
    "tagged_dollar_quote": "SELECT $q$'$q$ AS a, {f}('a', 'b') AS b, $q$'$q$ AS c",
    "escape_string": "SELECT E'\\'' AS a, {f}('a', 'b') AS b, E'\\'' AS c",
    "carriage_return_in_line_comment": "SELECT 1 -- note\r, {f}('a', 'b')",
    "comment_inside_a_number": "SELECT 1--'\n, {f}('a', 'b') --'",
    "array_slice": "SELECT (ARRAY[1, 2])[1:{f}(7)::int]",
    "escape_string_with_any_backslash": "SELECT E'a\\nb' AS a, {f}('a', 'b') AS b",
    "backtick_hidden_call": "SELECT 1 ` {f}('a', 'b') `",
}
SPELLING_FUNCTIONS = ("pg_notify", "pg_sleep", "pg_try_advisory_xact_lock", "dblink", "pg_read_file")


def test_every_denied_prefix_has_an_example():
    assert set(PREFIX_EXAMPLES) == set(DENIED_FUNCTION_PREFIXES)
    for prefix, names in PREFIX_EXAMPLES.items():
        assert all(name.startswith(prefix) for name in names), prefix


def _nested_cases():
    return [pytest.param(f"SELECT {form.format(f=name)}", id=f"{name}-{i}")
            for name in DENIED_NAMES for i, form in enumerate(NESTED)]


def _other_cases():
    return [pytest.param(form.format(f=name), id=f"{kind}-{name}")
            for kind, form in OTHER_SPELLINGS.items() for name in SPELLING_FUNCTIONS]


@pytest.fixture
def no_connection(monkeypatch):
    """Mode A and Mode B fail the test if they connect at all."""
    monkeypatch.setattr(system_agent.pgdriver, "connect",
                        lambda *a, **k: pytest.fail("Mode A connected for a refused statement"))
    monkeypatch.setattr(system_agent, "_pg_readonly_conn",
                        lambda params: pytest.fail("Mode B connected for a refused statement"))


def _refused_everywhere(sql):
    with pytest.raises(ValueError):
        ensure_select_only(sql)
    with pytest.raises(ValueError):
        system_agent._execute_direct_query("mes", {"host": "db.example.com"}, sql)
    with pytest.raises(ValueError):
        system_agent._mode_b_postgres_query(sql, "mes", {"host": "db.example.com"}, None)


@pytest.mark.parametrize("sql", _nested_cases())
def test_a_nested_block_comment_hides_no_denied_call(no_connection, sql):
    _refused_everywhere(sql)


@pytest.mark.parametrize("sql", _other_cases())
def test_other_postgres_lexing_hides_no_denied_call(no_connection, sql):
    _refused_everywhere(sql)


@pytest.mark.parametrize("sql", [
    "SELECT a /* note */ FROM t",
    "SELECT /* one */ /* two */ a FROM t",
    "SELECT /**/ a FROM t",
    "SELECT a FROM t -- trailing /* not a block comment",
    "SELECT a FROM t -- note\r\n WHERE b = 1",
    "SELECT '/* in a string */' AS a FROM t",
    "SELECT a::int, b::text FROM t",
    "SELECT (ARRAY[1, 2, 3])[1:n] FROM t",
    'SELECT "$path", "a b" FROM t',
    "SELECT a FROM t WHERE a ~ '\\d+'",
    "SELECT 1 - 2, 1e-5, 2.5e+3 FROM t",
    "SELECT data ? 'key' FROM t",
    "SELECT 'C:\\' AS path FROM t",
    "SELECT a FROM t WHERE p LIKE '%\\' OR p LIKE 'x\\%'",
    "SELECT E'plain' AS a FROM t",
    "SELECT 'U&\"x\"', 'u&''y''' AS a FROM t /* U&\"z\" */",
    'SELECT "menu&x", menu & 1, menu&1 FROM t',
    "DESCRIBE `order`",
    "DESCRIBE `my table`",
    "SELECT `a b` FROM t",
])
def test_ordinary_sql_still_passes(sql):
    assert ensure_select_only(sql) == sql


def test_the_tokenizer_marks_a_nested_block_comment_uncertain():
    assert tokenize("SELECT 1 /* a /* b */ c */")[1] is True
    assert tokenize("SELECT 1 /* a /*/ b */ */")[1] is True
    assert tokenize("SELECT 1 -- a\rb")[1] is True
    assert tokenize("SELECT 1--2")[1] is True


@pytest.mark.parametrize("sql", [
    'SELECT U&"\\0061" FROM t',
    "SELECT u&'\\0061' FROM t",
    'SELECT (ARRAY[1])[1:U&"\\0061"::int]',
    'SELECT (ARRAY[1])[U&"\\0061"::int:1]',
    'SELECT pg_catalog.U&"\\0061"(1)',
    'SELECT x::U&"\\0061"',
    "SELECT `U&\"\\0061\"`",
])
def test_a_unicode_escape_outside_literals_and_comments_is_refused(sql):
    with pytest.raises(ValueError, match="Unicode"):
        ensure_select_only(sql)


def test_an_array_slice_colon_is_its_own_token():
    tokens, uncertain = tokenize("SELECT (ARRAY[1, 2])[1:lower(x)] FROM t")
    assert not uncertain
    texts = [t.text for t in tokens]
    i = texts.index(":")
    assert texts[i + 1] == "lower" and tokens[i + 1].kind == "word"
    assert not any(t.kind == "bind" and t.text.startswith(":") for t in tokens)


class _RecordingCursor:
    def __init__(self, log):
        self.log, self.description = log, [("a",)]

    def execute(self, sql, params=None):
        self.log.append(sql if isinstance(sql, str) else str(sql))

    def fetchmany(self, n):
        return []

    def fetchall(self):
        return []

    def close(self):
        pass


class _RecordingConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _RecordingCursor(self.log)

    def set_session(self, **kwargs):
        pass

    def close(self):
        pass


def test_both_modes_pin_standard_conforming_strings(monkeypatch):
    """An ordinary string ending in a backslash reads the same to the guard
    and to PostgreSQL only while standard_conforming_strings is on, so the
    system-agent session sets it next to its statement timeout."""
    log: list = []
    monkeypatch.setattr(system_agent.pgdriver, "connect", lambda **kw: _RecordingConn(log))
    system_agent._execute_direct_query("mes", {"host": "db.example.com"}, "SELECT 'C:\\' AS p")
    assert "SET standard_conforming_strings = on" in log
    assert log.index("SET standard_conforming_strings = on") < len(log) - 1
    log.clear()
    monkeypatch.setattr(system_agent, "_pg_readonly_conn", lambda params: _RecordingConn(log))
    system_agent._mode_b_postgres_query("SELECT 'C:\\' AS p", "mes", {"host": "db.example.com"}, None)
    assert "SET standard_conforming_strings = on" in log
    assert log[-1].startswith("SELECT 'C:")


def test_ordinary_comments_leave_the_tokens_unchanged():
    plain, uncertain = tokenize("SELECT a FROM t WHERE b = 1")
    for sql in ("SELECT a /* c */ FROM t -- x\r\nWHERE b = 1",
                "SELECT /**/ a FROM /*/ c */ t WHERE b = 1"):
        tokens, flagged = tokenize(sql)
        assert flagged is uncertain is False
        assert [t.text for t in tokens] == [t.text for t in plain]


def test_the_athena_path_refuses_before_any_athena_call(monkeypatch):
    class _NoAthena:
        def __getattr__(self, name):
            pytest.fail(f"Athena was called ({name}) for a refused statement")

    monkeypatch.setattr(athena_tools, "_resolve_config",
                        lambda cfg: {"region": "us-east-1", "workgroup": "wg", "database": "db"})
    monkeypatch.setattr(athena_tools, "resolve_poll_timeout", lambda cfg: 5.0)
    for sql in ("SELECT a /* x /* y */ z */ FROM t", "SELECT 1 -- a\rb FROM t"):
        with pytest.raises(ValueError):
            athena_tools._run_athena_query_impl(sql, {}, client=_NoAthena())
