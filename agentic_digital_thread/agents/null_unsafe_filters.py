# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A negative filter that also drops the rows whose column is NULL.

Measured in a dry run on an analytics deployment: three per-state counts each
kept a filter ``<status column> <> '<value>'`` beside their own state filter
and came back with no rows. That status column was NULL on every row of the
window, and ``NULL <> '<value>'`` is unknown, not true, so the filter dropped
every row. The results carried nothing about it, and the agent reported that
there were no such records.

* :func:`find` reads the top-level WHERE of one statement (the WHERE at depth
  0; a statement with a set operation at depth 0, or more than one such WHERE,
  is not read) and returns each AND conjunct that is a negative test of one
  column against literals: ``<column> <> <literal>``, ``<column> !=
  <literal>`` (either side), ``<column> NOT IN (<literal>, ...)`` and
  ``<column> NOT LIKE <literal>``. The column is a bare, quoted or qualified
  reference; a literal is one string or number. A conjunct under OR (so the
  null-safe ``(c <> v OR c IS NULL)``), a ``NOT (...)``, an expression in
  place of the column (``COALESCE``), another column or a subquery on the
  other side, and ``IS DISTINCT FROM`` are not read. A test is left out when
  another top-level conjunct already needs that same column to have a value
  (``IS NOT NULL``, ``=``, ``<``, ``>``, ``<=``, ``>=`` on either side, ``IN``,
  ``LIKE``, ``BETWEEN``): a NULL row fails that conjunct anyway. The same
  column means the same qualifier and column name (case-folded), or both
  bare; ``a.c`` and ``b.c``, or ``a.c`` and a bare ``c``, are not known to be
  one column, so the test is kept.
* :func:`outcome` says whether a result is ``empty`` (no rows) or ``zero``
  (one row whose every COUNT output is 0), else ``""``. A COUNT or COUNT_IF
  call, optionally followed by ``FILTER (WHERE ...)``, is a COUNT output;
  anything else after the call (``OVER``, arithmetic) is not read. An error,
  a cut or partial result, or a SELECT list it cannot read gives ``""``.
* :func:`note` is the result's note: which tests the WHERE holds, that a row
  whose column is NULL fails such a test and is not counted, that the engine
  did not check whether such rows exist, and the null-safe form of each test.
  It is never longer than :data:`MAX_CHARS`: a NOT IN list longer than
  :data:`MAX_VALUES_SHOWN` values is printed as ``...`` with its length said in
  prose (a printed form is never a cut list), and when the tests do
  not all fit, the last ones are counted instead of shown.

The note adds no query and no figure. A column reference is printed only when
``engine_names.printable_sql`` passes it and it is at most
:data:`MAX_REFERENCE_CHARS` long, a value only when it is a plain literal of
at most :data:`MAX_LITERAL_CHARS` characters; otherwise a placeholder stands
in its place and the note says so. Generic: no column, table or value names.
Reads SQL with the shared tokenizer; no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: Most tests one note names.
MAX_TESTS = 4
#: Longest note, the bound engine lines keep (``executed_filters.MAX_LINE_CHARS``).
MAX_CHARS = 1200
#: Most values of one NOT IN list the note prints; the rest are counted.
MAX_VALUES_SHOWN = 3
#: Longest column reference and literal (inside its quotes) the note prints.
MAX_REFERENCE_CHARS = 64
MAX_LITERAL_CHARS = 64
_NEGATIVE_OPERATORS = {"<>", "!="}
_VALUE_OPERATORS = {"=", "<", ">", "<=", ">="}
_SET_OPERATIONS = {"UNION", "INTERSECT", "EXCEPT"}
_AFTER_WHERE = {"GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH", "WINDOW", "QUALIFY"}
_COUNTS = {"COUNT", "COUNT_IF"}
_NAME_KINDS = {"word", "identifier"}
_COLUMN_PLACEHOLDER = "col"
_VALUE_PLACEHOLDER = "v"
#: A string value the note may print between quotes: short, plain characters.
_PLAIN_VALUE_RE = re.compile(rf"[A-Za-z0-9 _.,&/%:#+\-]{{0,{MAX_LITERAL_CHARS}}}")
_NUMBER_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


@dataclass(frozen=True)
class NegativeTest:
    """One top-level conjunct ``reference operator values``: ``column`` is the
    column it names (lower case, the last part of the reference),
    ``reference`` the reference as written, ``operator`` one of ``<>``,
    ``!=``, ``NOT IN``, ``NOT LIKE``, ``values`` the literals as written, and
    ``qualifier`` the reference's qualifier (lower case, ``""`` for a bare
    column)."""

    column: str
    reference: str
    operator: str
    values: tuple
    qualifier: str = ""


# --------------------------------------------------------------------------- #
# Reading the SQL
# --------------------------------------------------------------------------- #
def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _tokens(sql):
    """The tokens of one statement read with certainty, else ``None``."""
    from .athena_scope import tokenize
    if not isinstance(sql, str):
        return None
    text = sql.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens or any(t.kind == "semicolon" for t in tokens):
        return None
    return tokens


def _top_where(tokens):
    """The body of the statement's one top-level WHERE, ``[]`` for none, else
    ``None`` (a set operation at depth 0, or several such WHEREs)."""
    top = [(i, _upper(t)) for i, t in enumerate(tokens) if t.depth == 0]
    if any(word in _SET_OPERATIONS for _i, word in top):
        return None
    wheres = [i for i, word in top if word == "WHERE"]
    if not wheres:
        return []
    if len(wheres) != 1:
        return None
    start = wheres[0] + 1
    end = next((i for i, word in top if i >= start and word in _AFTER_WHERE), len(tokens))
    return tokens[start:end]


def _reference(tokens):
    """``((qualifier, column), reference as written)`` of a bare, quoted or
    qualified column reference, names lower case and ``""`` for no
    qualifier, else ``None``."""
    from .athena_scope import _strip_outer
    tokens = _strip_outer(tokens)
    if len(tokens) == 1 and tokens[0].kind in _NAME_KINDS:
        key = ("", str(tokens[0].value).lower())
    elif (len(tokens) == 3 and tokens[1].text == "."
            and tokens[0].kind in _NAME_KINDS and tokens[2].kind in _NAME_KINDS):
        key = (str(tokens[0].value).lower(), str(tokens[2].value).lower())
    else:
        return None
    return key, "".join(t.text for t in tokens)


def _literal(tokens):
    """The text of one string or number literal, else ``None``."""
    from .athena_scope import _strip_outer
    tokens = _strip_outer(tokens)
    if len(tokens) == 1 and tokens[0].kind in {"literal", "number"}:
        return tokens[0].text
    return None


def _base(part):
    depth = min(t.depth for t in part)
    return [(i, t) for i, t in enumerate(part) if t.depth == depth]


def _keyword_after_not(top, word: str):
    """The index in the part of ``NOT <word>`` at its base depth, else ``None``."""
    for (i, token), (_j, after) in zip(top, top[1:]):
        if _upper(token) == "NOT" and _upper(after) == word:
            return i
    return None


def _test(found, operator: str, values: tuple) -> NegativeTest:
    (qualifier, column), reference = found
    return NegativeTest(column, reference, operator, values, qualifier)


def _negative(part) -> NegativeTest | None:
    """The negative test ``part`` is (see the module docstring), else ``None``."""
    from .athena_exact_shape import _split_commas
    from .athena_scope import _matching_close
    top = _base(part)
    words = {_upper(t) for _i, t in top}
    if words & {"OR", "AND", "IS", "BETWEEN", "CASE", "EXISTS", "SELECT", "DISTINCT"}:
        return None
    operators = [(i, t) for i, t in top if t.kind == "operator"]
    if "NOT" not in words:
        if len(operators) != 1 or operators[0][1].text not in _NEGATIVE_OPERATORS:
            return None
        i, token = operators[0]
        for side, other in ((part[:i], part[i + 1:]), (part[i + 1:], part[:i])):
            found, value = _reference(side), _literal(other)
            if found is not None and value is not None:
                return _test(found, token.text, (value,))
        return None
    if operators or [_upper(t) for _i, t in top].count("NOT") != 1:
        return None
    at = _keyword_after_not(top, "IN")
    if at is not None:
        found, rest = _reference(part[:at]), part[at + 2:]
        if found is None or not rest or rest[0].text != "(" or \
                _matching_close(rest, 0) != len(rest) - 1:
            return None
        values = tuple(_literal(item) for item in _split_commas(rest[1:-1]))
        if not values or any(v is None for v in values):
            return None
        return _test(found, "NOT IN", values)
    at = _keyword_after_not(top, "LIKE")
    if at is not None:
        found, value = _reference(part[:at]), _literal(part[at + 2:])
        if found is not None and value is not None:
            return _test(found, "NOT LIKE", (value,))
    return None


def _needs_value(part) -> set:
    """The columns ``part`` cannot be true for when they are NULL (see the
    module docstring), each as ``(qualifier, column)`` lower case; empty for
    anything else."""
    top = _base(part)
    words = [_upper(t) for _i, t in top]
    if "OR" in words or (words and words[0] == "NOT"):
        return set()
    for position, (i, token) in enumerate(top):
        word = _upper(token)
        before = words[position - 1] if position else ""
        if word == "IS" and words[position + 1:] == ["NOT", "NULL"]:
            found = _reference(part[:i])
            return {found[0]} if found else set()
        if word in {"IN", "LIKE", "BETWEEN"} and before != "NOT":
            found = _reference(part[:i])
            return {found[0]} if found else set()
        if token.kind == "operator" and token.text in _VALUE_OPERATORS:
            sides = (_reference(part[:i]), _reference(part[i + 1:]))
            return {found[0] for found in sides if found}
    return set()


def find(sql) -> tuple:
    """The negative tests of ``sql``'s top-level WHERE (see the module
    docstring), in written order, at most :data:`MAX_TESTS`; ``()`` for none
    or an unread statement."""
    from .state_times import _conjuncts
    tokens = _tokens(sql)
    if tokens is None:
        return ()
    where = _top_where(tokens)
    if not where:
        return ()
    parts = [p for p in _conjuncts(where) if p]
    required: set = set()
    for part in parts:
        required |= _needs_value(part)
    out: list = []
    for part in parts:
        test = _negative(part)
        if test is not None and (test.qualifier, test.column) not in required \
                and test not in out:
            out.append(test)
    return tuple(out[:MAX_TESTS])


# --------------------------------------------------------------------------- #
# The result
# --------------------------------------------------------------------------- #
def _count_positions(sql):
    """``(indexes of the COUNT outputs, number of outputs)`` of the statement's
    top-level SELECT list, else ``None``."""
    from .athena_exact_shape import _split_alias, _split_commas
    from .athena_scope import _strip_outer
    tokens = _tokens(sql)
    if tokens is None:
        return None
    top = [(i, _upper(t)) for i, t in enumerate(tokens) if t.depth == 0]
    select = next((i for i, word in top if word == "SELECT"), None)
    if select is None:
        return None
    end = next((i for i, word in top if i > select and word == "FROM"), None)
    start = select + 1
    if start < len(tokens) and _upper(tokens[start]) in {"DISTINCT", "ALL"}:
        start += 1
    if end is None or end <= start:
        return None
    items = _split_commas(tokens[start:end])
    if any(not item for item in items):
        return None
    positions = []
    for index, item in enumerate(items):
        if _is_count(_strip_outer(_split_alias(item)[0])):
            positions.append(index)
    return positions, len(items)


def _is_count(expression) -> bool:
    """True for ``COUNT(...)`` or ``COUNT_IF(...)``, alone or followed by one
    ``FILTER (WHERE ...)``; anything else after the call is not read."""
    from .athena_scope import _matching_close
    if (len(expression) < 3 or _upper(expression[0]) not in _COUNTS
            or expression[1].text != "("):
        return False
    close = _matching_close(expression, 1)
    if close is None:
        return False
    rest = expression[close + 1:]
    return not rest or (len(rest) >= 4 and _upper(rest[0]) == "FILTER"
                        and rest[1].text == "(" and _upper(rest[2]) == "WHERE"
                        and _matching_close(rest, 1) == len(rest) - 1)


def _zero(cell) -> bool:
    if isinstance(cell, bool):
        return False
    try:
        return Decimal(str(cell).strip()) == 0
    except (InvalidOperation, ValueError):
        return False


def outcome(result, sql) -> str:
    """``"empty"`` for a result with no rows, ``"zero"`` for one row whose
    every COUNT output is 0, else ``""``."""
    if not isinstance(result, dict) or "error" in result:
        return ""
    rows = result.get("rows")
    if not isinstance(rows, list):
        return ""
    if (result.get("truncated") is True or result.get("capped_at_limit")
            or result.get("partial_reason") or result.get("partial_note")):
        return ""
    if not rows:
        return "empty" if result.get("row_count") in (None, 0) else ""
    columns = result.get("columns")
    read = _count_positions(sql)
    if len(rows) != 1 or read is None or not isinstance(columns, list) or \
            len(columns) != read[1] or not isinstance(rows[0], list) or \
            len(rows[0]) != read[1] or not read[0]:
        return ""
    return "zero" if all(_zero(rows[0][i]) for i in read[0]) else ""


# --------------------------------------------------------------------------- #
# The note: facts, names and values only when printable
# --------------------------------------------------------------------------- #
def _value_shown(text: str) -> bool:
    from .untrusted_text import sanitize_untrusted, validate_untrusted
    if len(text) > MAX_LITERAL_CHARS + 2:
        return False
    if _NUMBER_RE.fullmatch(text):
        return len(text) <= MAX_LITERAL_CHARS
    if len(text) < 2 or text[0] != "'" or text[-1] != "'":
        return False
    value = text[1:-1]
    if "'" in value or not _PLAIN_VALUE_RE.fullmatch(value):
        return False
    return value == "" or (sanitize_untrusted(value) == value
                           and validate_untrusted(value, max_len=MAX_LITERAL_CHARS,
                                                  field="value") is None)


def _column_shown(names, test: NegativeTest) -> tuple[str, bool]:
    """``(the test's column reference, False)``, else ``(the placeholder,
    True)``: for a reference that is not printable (counted in ``names``) or
    is longer than :data:`MAX_REFERENCE_CHARS`."""
    from .engine_names import printable_sql
    if printable_sql(test.reference):
        if len(test.reference) <= MAX_REFERENCE_CHARS:
            return test.reference, False
        return _COLUMN_PLACEHOLDER, True
    names.show(test.reference)
    return _COLUMN_PLACEHOLDER, True


def _values_shown(test: NegativeTest) -> tuple[list, bool, int]:
    """``(the test's values as the note prints them, whether they are
    withheld, the length of a NOT IN list printed as ``...``)``. A NOT IN list
    past :data:`MAX_VALUES_SHOWN` values is printed as ``...`` and its length
    is said in prose, so every form the note prints stays whole SQL."""
    values = list(test.values)
    if not all(_value_shown(v) for v in values):
        return ([_VALUE_PLACEHOLDER] if test.operator != "NOT IN" else ["..."]), True, 0
    if len(values) > MAX_VALUES_SHOWN:
        return ["..."], False, len(values)
    return values, False, 0


def _forms(column: str, test: NegativeTest, values) -> tuple[str, str]:
    """``(the test, its null-safe form)`` with ``column`` and ``values`` as
    :func:`_text` already shows them."""
    if test.operator == "NOT IN":
        written = f"{column} NOT IN ({', '.join(values)})"
        return written, f"({written} OR {column} IS NULL)"
    written = f"{column} {test.operator} {values[0]}"
    safe = f"({written} OR {column} IS NULL)"
    if test.operator in _NEGATIVE_OPERATORS:
        safe += f", or equally {column} IS DISTINCT FROM {values[0]}"
    return written, safe


def note(tests, result_outcome: str) -> str:
    """The note on a result :func:`outcome` calls ``empty`` or ``zero`` whose
    WHERE holds ``tests``; ``""`` for no test or another outcome. At most
    :data:`MAX_CHARS` long: when the tests do not all fit, the last ones are
    counted instead of shown."""
    tests = [t for t in tests or () if isinstance(t, NegativeTest)][:MAX_TESTS]
    if not tests or result_outcome not in {"empty", "zero"}:
        return ""
    for shown in range(len(tests), 0, -1):
        text = _text(tests[:shown], len(tests) - shown, result_outcome)
        if len(text) <= MAX_CHARS:
            return text
    # Not reached: with the caps above one test's note is well under
    # MAX_CHARS. A longer note is never returned.
    return ""


def _text(tests: list, hidden: int, result_outcome: str) -> str:
    """The note naming ``tests`` and counting ``hidden`` more."""
    from .engine_names import Withheld
    names = Withheld()
    written, safe, columns = [], [], []
    withheld_column = withheld_value = False
    lists = []
    for test in tests:
        column, column_withheld = _column_shown(names, test)
        values, values_withheld, listed = _values_shown(test)
        withheld_column |= column_withheld
        withheld_value |= values_withheld
        if listed:
            lists.append(listed)
        one, null_safe = _forms(column, test, values)
        written.append(one)
        safe.append(null_safe)
        columns.append(column)
    head = ("this result is empty" if result_outcome == "empty"
            else "this result's count is 0")
    if len(tests) == 1 and not hidden:
        text = (f"{head}, and its WHERE keeps a row only when {written[0]} is true. A row "
                f"whose {columns[0]} is NULL fails that test (a comparison with NULL is "
                "unknown, not true) and is not counted; the engine did not check whether "
                f"such rows exist. The null-safe form is {safe[0]}.")
    else:
        more = (f"; and {hidden} more such test{'' if hidden == 1 else 's'}, not shown here"
                if hidden else "")
        forms = "forms" if len(tests) > 1 else "form"
        shown = (f" of the test{'s' if len(tests) > 1 else ''} shown" if hidden else "")
        verb = "are" if len(tests) > 1 else "is"
        text = (f"{head}, and its WHERE keeps a row only when each of these is true: "
                f"{'; '.join(written)}{more}. A row whose column in one of these tests is NULL "
                "fails that test (a comparison with NULL is unknown, not true) and is not "
                "counted; the engine did not check whether such rows exist. The null-safe "
                f"{forms}{shown} {verb} {'; '.join(safe)}.")
    if withheld_column:
        text += f" Here {_COLUMN_PLACEHOLDER} is the filtered column, whose name is withheld."
    if withheld_value:
        text += f" Here {_VALUE_PLACEHOLDER} or ... is the query's own value, not repeated here."
    if lists:
        sizes = " and ".join(f"{n:,}" for n in lists)
        text += (f" Here ... is the query's own list of {sizes} values, not repeated here; "
                 "keep that list whole.")
    return text + names.sentence()
