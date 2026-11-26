# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The closed set of Trino/Athena parser and analyzer diagnostics the Athena
connector reads from a query StartQueryExecution refuses.

Measured on an analytics deployment: the system agent wrote
``EXTRACT(EPOCH FROM ...)``, which Athena refuses when the query is
submitted ("line 5:10: Invalid EXTRACT field: EPOCH"). The tool said only
``query_unavailable``, so the agent retried the same construct. The agent
needs to know what the parser rejected, but the engine's message is not
safe to hand over as text: a parser echoes tokens of the model's own SQL, so
a message can carry an instruction, and a permission or workgroup refusal
can carry a ``line L:C:`` position too.

So nothing of the message is passed on as text. :func:`read` matches the
message (after an optional leading engine NAME and ``line L:C:`` position)
against :data:`DIAGNOSTICS`, each a whole-message pattern; a message that
matches none is not a SQL diagnostic and the connector keeps its old mapping.
A match gives the diagnostic id, the captured tokens, each checked for its
kind (an identifier or keyword in ``[A-Za-z0-9_.]``, at most
:data:`MAX_TOKEN_CHARS`, or :data:`MAX_COLUMN_CHARS` for a column name; a
type name reduced to its base name; an operator
from a fixed set) and dropped when it does not pass, a fixed sentence built
from the tokens that passed, and the position as two integers.

One FAILED-state reason is read the same way: a column the engine could not
resolve (``column_not_resolved``). The connector then works out what that
name was from the agent's own SQL and the table's column list (an alias the
same query defines, a dotted column referenced in parts, an underscored
spelling of a dotted column), and :func:`cause` gives the sentence for that
cause from checked tokens, in :data:`COLUMN_CAUSES`.

Pure and offline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Longest token carried to the agent.
MAX_TOKEN_CHARS = 64
#: Longest column name carried to the agent: a dotted lake column is often
#: longer than :data:`MAX_TOKEN_CHARS`, and a column token is also held to
#: ``engine_names.printable`` (same cap) by its caller.
MAX_COLUMN_CHARS = 128
_NAME_RE = re.compile(r"[A-Za-z0-9_.]{1,%d}" % MAX_TOKEN_CHARS)
_COLUMN_RE = re.compile(r"[A-Za-z0-9_.]{1,%d}" % MAX_COLUMN_CHARS)
_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,%d}" % (MAX_TOKEN_CHARS - 1))
_OPERATORS = frozenset({"=", "<>", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", "||"})
_POSITION_RE = re.compile(r"line (\d{1,6}):(\d{1,6}): ")
_LEADING_NAME_RE = re.compile(r"([A-Z][A-Z0-9_]*[A-Z0-9]): ")


@dataclass(frozen=True)
class Diagnostic:
    """One known diagnostic: whole-message ``patterns``, the kind of each
    token they capture, and the sentence (``text``, or ``fallback`` when a
    token the sentence names was dropped)."""

    id: str
    patterns: tuple[re.Pattern, ...]
    kinds: dict
    text: str
    fallback: str


def _diag(id_: str, patterns, kinds: dict, text: str, fallback: str) -> Diagnostic:
    return Diagnostic(id_, tuple(re.compile(p) for p in patterns), kinds, text, fallback)


DIAGNOSTICS: tuple[Diagnostic, ...] = (
    _diag("invalid_extract_field", [r"Invalid EXTRACT field: (?P<field>\S+)"],
          {"field": "name"}, "EXTRACT does not accept the field {field}.",
          "EXTRACT does not accept that field."),
    _diag("function_not_registered", [r"Function '(?P<function>[^'\s]+)' not registered",
                                      r"Function (?P<function>[^'\s]+) not registered"],
          {"function": "name"}, "The function {function} is not registered in this engine.",
          "That function is not registered in this engine."),
    _diag("unexpected_parameters",
          [r"Unexpected parameters \(.*\) for function (?P<function>[^\s()]+?)\. Expected: .*"],
          {"function": "name"}, "The function {function} does not accept these argument types.",
          "A function does not accept these argument types."),
    _diag("mismatched_input", [r"mismatched input '(?P<input>.*?)'\.?(?: Expecting: .*)?"],
          {"input": "name"}, "The parser did not expect the input {input} here.",
          "The parser did not expect an input token here."),
    _diag("no_viable_alternative", [r"no viable alternative at input '(?P<input>.*?)'"],
          {"input": "name"}, "The parser could not read the input {input}.",
          "The parser could not read an input token."),
    _diag("extraneous_input", [r"extraneous input '(?P<input>.*?)' expecting .*"],
          {"input": "name"}, "The parser found an extra input {input}.",
          "The parser found an extra input token."),
    _diag("operator_types", [r"Cannot apply operator: (?P<left>\S+) (?P<operator>\S+) (?P<right>\S+)",
                             r"'(?P<operator>[^']+)' cannot be applied to (?P<left>.+?), (?P<right>.+)"],
          {"operator": "operator", "left": "type", "right": "type"},
          "The operator {operator} cannot compare {left} with {right}: cast one side to the "
          "other's type.",
          "An operator was applied to two types it cannot compare: cast one side."),
    _diag("cannot_cast", [r"Cannot cast (?P<from>.+?) to (?P<to>.+)"],
          {"from": "type", "to": "type"}, "The engine cannot cast {from} to {to}.",
          "The engine cannot make that cast."),
    _diag("must_be_aggregate",
          [r"'(?P<expression>.+)' must be an aggregate expression or appear in GROUP BY clause"],
          {"expression": "name"}, "{expression} must be aggregated or listed in GROUP BY.",
          "An expression must be aggregated or listed in GROUP BY."),
    _diag("column_ambiguous", [r"Column '(?P<column>[^']+)' is ambiguous"],
          {"column": "name"}, "The column {column} is ambiguous: qualify it with its table alias.",
          "A column is ambiguous: qualify it with its table alias."),
    _diag("where_not_boolean", [r"WHERE clause must evaluate to a boolean: actual type (?P<actual>.+)"],
          {"actual": "type"}, "The WHERE clause must be a boolean condition, not {actual}.",
          "The WHERE clause must be a boolean condition."),
    _diag("table_not_found", [r"Table '(?P<table>[^']+)' does not exist"],
          {"table": "name"},
          "The table {table} does not exist: call athena_list_tables for the real names.",
          "That table does not exist: call athena_list_tables for the real names."),
    _diag("column_not_resolved",
          [r"Column '(?P<column>[^']+)' cannot be resolved"
           r"(?: or requester is not authorized to access requested resources)?"],
          {"column": "column"},
          "The column {column} could not be resolved in the tables this query reads: check its "
          "exact name with athena_describe_table.",
          "A column could not be resolved in the tables this query reads: check its exact name "
          "with athena_describe_table."),
)

#: What a ``column_not_resolved`` name turned out to be, worked out by the
#: connector from the agent's own SQL and the table's column list. Each has
#: no message pattern: :func:`cause` builds its sentence from tokens alone.
COLUMN_CAUSES: tuple[Diagnostic, ...] = (
    _diag("select_alias", [], {"column": "column"},
          "{column} is an alias this query defines in its SELECT list, not a column. This "
          "engine does not resolve a SELECT alias in WHERE, GROUP BY, HAVING or another "
          "expression of the same SELECT list. Group by position (GROUP BY 1, 2, 3) or repeat "
          "the full expression, and check every other GROUP BY name the same way. The table's "
          "column names are not the cause; keep them.",
          "A name is an alias this query defines in its SELECT list, not a column."),
    _diag("dotted_name_split", [], {"column": "column"},
          "{column} is a real column of this table: the dots are part of its name. Reference it "
          "as one double-quoted identifier, \"{column}\", never as separate quoted or unquoted "
          "parts.",
          "A real column has dots in its name: reference it as one double-quoted identifier."),
    _diag("underscored_variant", [], {"column": "column", "real_column": "column"},
          "No column named {column} exists. The real column is {real_column}, with dots in its "
          "name: reference it as one double-quoted identifier, \"{real_column}\".",
          "That column does not exist; the real column has dots in its name."),
)


@dataclass(frozen=True)
class Reading:
    """What the agent is given for one recognised diagnostic."""

    diagnostic: str
    tokens: dict
    text: str
    position: dict | None
    name: str


def _token(raw: str, kind: str) -> str | None:
    if kind == "operator":
        return raw if raw in _OPERATORS else None
    if kind == "type":
        match = _TYPE_RE.match(raw)
        end = match.end() if match else 0
        if not match or (end < len(raw) and raw[end] not in " (,"):
            return None
        return match.group(0)
    if kind == "column":
        return raw if _COLUMN_RE.fullmatch(raw) else None
    return raw if _NAME_RE.fullmatch(raw) else None


def _sentence(diagnostic: Diagnostic, tokens: dict) -> str:
    named = re.findall(r"\{(\w+)\}", diagnostic.text)
    if all(name in tokens for name in named):
        return diagnostic.text.format(**tokens)
    return diagnostic.fallback


def read(message, names=()) -> Reading | None:
    """The :class:`Reading` of one engine message, or ``None`` when it is not
    one of :data:`DIAGNOSTICS`. ``names`` are the engine error NAMEs that may
    lead the message (``NAME: ...``)."""
    if not isinstance(message, str):
        return None
    rest, name = message.strip(), ""
    lead = _LEADING_NAME_RE.match(rest)
    if lead and lead.group(1) in set(names):
        name, rest = lead.group(1), rest[lead.end():]
    position = None
    at = _POSITION_RE.match(rest)
    if at:
        position = {"line": int(at.group(1)), "column": int(at.group(2))}
        rest = rest[at.end():]
    for diagnostic in DIAGNOSTICS:
        for pattern in diagnostic.patterns:
            match = pattern.fullmatch(rest)
            if not match:
                continue
            tokens = {}
            for key, kind in diagnostic.kinds.items():
                value = _token(match.group(key) or "", kind)
                if value is not None:
                    tokens[key] = value
            return Reading(diagnostic.id, tokens, _sentence(diagnostic, tokens), position, name)
    return None


def cause(diagnostic_id: str, tokens: dict) -> Reading | None:
    """The :class:`Reading` of one :data:`COLUMN_CAUSES` entry, or ``None``
    when the id is unknown or any token it names fails its check (the caller
    then keeps the plain diagnostic)."""
    entry = next((c for c in COLUMN_CAUSES if c.id == diagnostic_id), None)
    if entry is None:
        return None
    checked = {}
    for key, kind in entry.kinds.items():
        value = _token(str(tokens.get(key) or ""), kind)
        if value is None:
            return None
        checked[key] = value
    return Reading(entry.id, checked, _sentence(entry, checked), None, "")
