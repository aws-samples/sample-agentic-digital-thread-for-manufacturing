# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compositions of every spelling the SQL guard handles, around any function
name (shared by the unit test and the PostgreSQL-lane test).

A composition is one way to write a call: a name spelling (casing, schema
qualification, double quotes, Unicode escapes), a gap before ``(``
(whitespace, comments), and a position in a statement (select list, array
slice bounds, nested parentheses, casts, subquery, CTE, CASE, a parenthesised
statement). Lexing wrappers put the call next to text PostgreSQL reads
differently from the tokenizer (dollar quotes, escape strings, a bare
carriage return in a line comment, ``--`` after a number, a nested block
comment). Every spelling here is valid PostgreSQL for a lower-case function
name, so on PostgreSQL each composition calls the function.
"""
from __future__ import annotations

from agents.sql_safety import DENIED_FUNCTION_PREFIXES, DENIED_FUNCTIONS

#: One or more real PostgreSQL function names per denied prefix.
PREFIX_EXAMPLES = {
    "dblink": ("dblink", "dblink_exec", "dblink_connect"),
    "pg_advisory": ("pg_advisory_lock", "pg_advisory_xact_lock", "pg_advisory_unlock_all"),
    "pg_try_advisory": ("pg_try_advisory_lock", "pg_try_advisory_xact_lock"),
    "pg_ls_": ("pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir"),
    "lo_": ("lo_import", "lo_export"),
    "query_to_xml": ("query_to_xml", "query_to_xml_and_xmlschema"),
    "cursor_to_xml": ("cursor_to_xml",),
}
DENIED_NAMES = sorted(DENIED_FUNCTIONS | {n for names in PREFIX_EXAMPLES.values() for n in names})
#: Names the guard must accept in every composition that is not a Unicode
#: escape or a lexing wrapper (the second and third start like denied names).
ALLOWED_NAMES = ("lower", "pg_size_pretty", "ts_rank")


def _escape_first(name: str) -> str:
    return f"\\{ord(name[0]):04x}{name[1:]}"


def _escape_all(name: str) -> str:
    return "".join(f"\\{ord(ch):04x}" for ch in name)


def _mixed(name: str) -> str:
    return "".join(ch.upper() if i % 2 else ch for i, ch in enumerate(name))


#: (id, spelling, is a Unicode escape). Quoted spellings keep the exact
#: lower-case name, so PostgreSQL resolves every one to the function.
SPELLINGS = (
    ("plain", lambda n: n, False),
    ("upper", lambda n: n.upper(), False),
    ("mixed_case", _mixed, False),
    ("schema", lambda n: f"pg_catalog.{n}", False),
    ("schema_spaced", lambda n: f"pg_catalog . {n}", False),
    ("quoted", lambda n: f'"{n}"', False),
    ("schema_quoted", lambda n: f'pg_catalog."{n}"', False),
    ("quoted_schema_quoted", lambda n: f'"pg_catalog"."{n}"', False),
    ("unicode", lambda n: f'U&"{_escape_first(n)}"', True),
    ("unicode_lower", lambda n: f'u&"{_escape_first(n)}"', True),
    ("unicode_all", lambda n: f'U&"{_escape_all(n)}"', True),
    ("schema_unicode", lambda n: f'pg_catalog.U&"{_escape_first(n)}"', True),
    ("unicode_uescape", lambda n: "U&\"!" + f"{ord(n[0]):04x}{n[1:]}" + "\" UESCAPE '!'", True),
)

#: (id, text between the name and its "(", is a lexing refusal).
GAPS = (
    ("none", "", False),
    ("space", " ", False),
    ("block_comment", " /* note */ ", False),
    ("line_comment", " -- note\n", False),
    ("newline_tab", "\n\t", False),
    ("nested_comment", " /* a /* b */ c */ ", True),
)

#: (id, statement around ``{call}``). Each is valid PostgreSQL when ``{call}``
#: returns a boolean (``pg_try_advisory_lock`` on the PostgreSQL lane).
POSITIONS = (
    ("select_list", "SELECT {call}"),
    ("slice_upper", "SELECT (ARRAY[1, 2])[1:{call}::int]"),
    ("slice_lower", "SELECT (ARRAY[1, 2])[{call}::int:2]"),
    ("nested_parentheses", "SELECT ((({call})))"),
    ("cast_operator", "SELECT ({call})::text"),
    ("cast_function", "SELECT CAST({call} AS text)"),
    ("where_subquery", "SELECT a FROM (VALUES (true)) AS t(a) WHERE a = (SELECT {call})"),
    ("cte", "WITH x AS (SELECT {call} AS v) SELECT v FROM x"),
    ("case", "SELECT CASE WHEN true THEN {call} END"),
    ("parenthesised_statement", "(SELECT {call})"),
)

#: (id, statement around ``{call}``) next to text PostgreSQL reads as code.
LEXING_WRAPPERS = (
    ("dollar_quote", "SELECT $$'$$ AS a, {call} AS b, $$'$$ AS c"),
    ("tagged_dollar_quote", "SELECT $q$'$q$ AS a, {call} AS b, $q$'$q$ AS c"),
    ("escape_string", "SELECT E'\\'' AS a, {call} AS b, E'\\'' AS c"),
    ("carriage_return_in_line_comment", "SELECT 1 -- note\r, {call}"),
    ("comment_inside_a_number", "SELECT 1--'\n, {call} --'"),
)


def compositions(name: str, args: str = "(1)"):
    """Every ``(id, sql, refusal_by_form)`` for a call of ``name``. A
    ``refusal_by_form`` composition is refused whatever the name (a Unicode
    escape, a nested comment, a lexing wrapper)."""
    for spelling_id, spell, unicode in SPELLINGS:
        for gap_id, gap, lexing_gap in GAPS:
            call = f"{spell(name)}{gap}{args}"
            for position_id, template in POSITIONS:
                yield (f"{spelling_id}/{gap_id}/{position_id}", template.format(call=call),
                       unicode or lexing_gap)
        for wrapper_id, template in LEXING_WRAPPERS:
            yield (f"{spelling_id}/{wrapper_id}", template.format(call=f"{spell(name)}{args}"), True)


def count_per_name() -> int:
    return len(SPELLINGS) * (len(GAPS) * len(POSITIONS) + len(LEXING_WRAPPERS))
