# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A wrong-form lookup the shape guard refused that no result of the call resolved.

The PostgreSQL query tool's shape guard (``identifier_shape_guard``) refuses a
lookup whose value has the form of the measured column's pair and hands the
agent one IDENTIFIER-MISS block per value. The guard keeps no state between
queries and lets LIKE and own-form values through (its docstring says why),
so the agent can go on to guess the other form and state a pairing of the
two forms that no result row showed. The orchestrator learns of the miss
only if the answer carries the block, and its IDENTIFIER-MISS recovery (ask
the system that pairs the two forms) never starts otherwise.

The answer is never changed. After it, the engine carries the guard's own
blocks, the same way ``answer_ids`` carries identifiers no result returned.
Everything is judged per refused value, from what the call recorded AFTER
that value's first refusal:

* :func:`record_result` reads each of THIS call's tool results as it arrives
  (the capture hook calls it, with the guard context the agent keeps under
  :data:`SHAPES_ATTR`). A wrong-form refusal adds each refused value with
  its looked-in column, the paired column (where the value's form lives),
  both measured shapes and the guard's block. A later result row that holds
  the refused value itself (a whole cell or a token in one) records, for
  that value, the cells of the looked-in column's shape in the same row: the
  value's counterparts. When the result names its columns, the value must be
  under a column that stands for the paired column and the counterpart under
  one that stands for the looked-in column: the column whose source the
  SELECT list proves (``identifier_provenance._projected_sources``), else a
  result column of the measured column's name. The row's other identifier
  cells are kept as the value's link keys.
* Two-query recovery: a later query whose only string literal is one of a
  refused value's link keys (the call's own trusted executed SQL), records
  the looked-in-shape cells of its result (under the looked-in column when
  named) as that value's counterparts too. So "query A returns a linking key
  for the value, query B returns the other form by that key" counts.
* A refused value is resolved when exactly one distinct counterpart was
  recorded for it. None means the call never paired it; more than one means
  the rows paired it through a key several records share, which is how a
  guessed pairing reads, so it is not proven either.
* :func:`engine_fact` gives one fixed line (:data:`MARKER`) and, fenced as
  the IDENTIFIER-MISS protocol writes it, the guard's block for each refused
  value that is not resolved, that the delegated question does not supply a
  counterpart for, and that the answer has no IDENTIFIER-MISS block or
  no-record line of its own for. The question supplies a counterpart when a
  token of the looked-in shape stands next to the value on the same line,
  at most :data:`MAX_GAP` characters away, and the text between them says
  the two name one record: an opening parenthesis or bracket, a slash or an
  ``=`` (``x (y)``, ``x / y``, ``x = y``), a pairing phrase (``aka``,
  ``a.k.a.``, ``also known as``, ``same as``), or, in a table row, one cell
  border. A comma pairs only when a pairing phrase or a label follows it
  (``x, label y``). After the mark or phrase there may be a label naming the
  form of the value after it: the leading letters of that form's measured
  shape, as one word or as a word for each letter, or the words of its
  table name, of the measured column's name or of the pair's concept. Any
  other word, a contrast word (``against``, ``versus``, ``not``,
  ``unrelated``...), a list word (``and``, ``or``...), a digit or a sentence
  mark means no pair. A counterpart that the rule pairs with another value
  of the paired form on its other side too could be either one's, and counts
  for neither. Only the delegated question counts, not the time line,
  concept mappings, guidance or route context the runtime adds around it.

Every input the record cannot read resolves toward no fact: a result cut
short or not read whole after a refusal, one whose rows cannot be read, more
than :data:`MAX_VALUES` refused values, a refusal the record cannot read or
one whose value is not a plain identifier (no value can close the fence), a
shape with a space (it cannot be found as one token), and any fault while
recording. Proof of a pairing is the other way round: what the record cannot
prove fires the line, because the cost is at most one bounded follow-up the
orchestrator did not need. Trade-offs, accepted:

* A correct recovery through more than two queries, through an integer key,
  through one query keyed by several values' link keys, or through a second
  query that also filters on anything else, is not proven, and the line
  fires. So does a pairing through a table whose columns are named neither
  as the measured columns nor proven to be them.
* A key that several records share but that reaches exactly one record of
  the other form when queried (the others not written yet) proves a pairing
  the data does not hold, and the line is not given.
* A counterpart the question pairs with the value by that rule is taken as
  given, whether or not it is the right one. A pairing the rule does not
  read (a bare label with no mark, ``x: y``, a sentence that says it in
  other words) fires the line.
* An answer that never rests on translating the refused value still gets
  the block.

The line is for the orchestrator only (``internal_lines.MARKERS``); the blocks
stay wherever the answer is shown, as a system agent's own blocks do. Each
refusal kept also carries the measured pair's concept, which
``wrong_form_refused`` names in its own line for each refused value the call
did not resolve (:func:`resolved`).

Generic: the columns and shapes are the guard's measured pairs; no dataset,
system, table, column or concept literals.
"""
from __future__ import annotations

import re
from typing import Any

#: Attribute the system agent factory sets on the agent (and the query tool
#: carries): the guard context (``identifier_shape_guard.ShapeContext``).
SHAPES_ATTR = "_adt_identifier_shapes"
#: Invocation-state key of this call's refused values and what paired them.
STATE_KEY = "_adt_wrong_form_refusals"
#: Opening words of the engine line.
MARKER = "[engine] Identifier forms not paired:"
#: Most refused values one call may have for the fact to be given.
MAX_VALUES = 5
#: Refused values kept per call; past it the call counts as unread.
MAX_REFUSALS = 32
#: Link keys kept per refused value; past it no more are kept.
MAX_KEYS = 16
#: Distinct counterparts kept per refused value (two already mean not proven).
_MAX_COUNTERPARTS = 4
#: Most characters between a value and its counterpart in the question.
MAX_GAP = 48
#: Content fragments read per result; a result with more is unread.
MAX_FRAGMENTS = 16
#: A text cell longer than this is not read token by token: the call is unread.
MAX_CELL_CHARS = 4096
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_/.:-]*")
_TOKEN_END = "_/.:-"  # nosec B105 - the characters an identifier may end on, not a password
#: A refused value the fact may quote: letters and digits joined by the shape
#: separators, so no value can close the fence it is quoted in.
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_/.: -]{0,199}")
#: A link key: one identifier token holding a digit.
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{2,127}")
#: Marks that may stand between a value and its counterpart in the question.
_MARKS = frozenset("([/=,|#")
#: Marks that pair the value after them with the one before (``x (y)``,
#: ``x / y``, ``x = y``).
_OPENERS = frozenset("([/=")
#: Words that say two identifiers name one record.
_AKA_PHRASES = (("also", "known", "as"), ("known", "as"), ("aka",), ("same", "as"))
_AKA_DOTTED = re.compile(r"\ba\.k\.a\b\.?")
#: Words around a label that change nothing it names.
_ARTICLES = frozenset({"the", "a", "an", "its", "their"})
_QUALIFIERS = frozenset({"number", "no", "num", "id", "code", "ref", "reference", "#"})
#: Words that set two values apart: contrast, a list, another record.
_BREAK_WORDS = frozenset({
    "against", "versus", "vs", "unrelated", "compare", "compared", "comparing", "different",
    "differs", "not", "unlike", "except", "other", "another", "and", "or", "nor", "but",
    "plus", "with", "without", "from", "to", "than", "then"})
_NO_RECORD = re.compile(r"No record of (\S+)")
_LINE = (
    "this call looked up {what} in a column that holds another form of identifier, "
    "and the engine did not run that lookup. Figures that rest on translating {that} "
    "into the other form were not paired with one value of the other form by any "
    "result in this call; the IDENTIFIER-MISS {block} below {names} the value, the "
    "column it was looked up in and that column's form."
)


class _Unreadable(Exception):
    """A result, row or cell the record cannot read whole."""


def _sig(value: str) -> tuple:
    from .system_agent import _shape_signature
    return _shape_signature(value)


def _match(sig_a: tuple, sig_b: tuple) -> bool:
    from .system_agent import _signatures_match
    return _signatures_match(sig_a, sig_b)


def _record(invocation_state: dict) -> dict:
    record = invocation_state.get(STATE_KEY)
    if not isinstance(record, dict):
        record = {"pending": {}, "unread": False}
        invocation_state[STATE_KEY] = record
    return record


def mark_unread(invocation_state: Any) -> None:
    """Mark this call's record unread: no fact is given for it."""
    if isinstance(invocation_state, dict):
        _record(invocation_state)["unread"] = True


def _payloads(result: Any) -> list:
    """The parsed JSON payload of each content fragment of ``result``."""
    from .tool_result_capture import _CUT_SCAN_CHARS, _bounded_json_text
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or not content or len(content) > MAX_FRAGMENTS:
        raise _Unreadable
    payloads = []
    for item in content:
        if not isinstance(item, dict):
            raise _Unreadable
        payload = item.get("json")
        if not isinstance(payload, (dict, list)):
            payload = _bounded_json_text(item.get("text"), _CUT_SCAN_CHARS)
        if not isinstance(payload, dict):
            raise _Unreadable
        payloads.append(payload)
    return payloads


def _token(match) -> str:
    return match.group(0).rstrip(_TOKEN_END)


def _cell_tokens(cell: Any, depth: int = 0):
    """The identifier candidates of one cell: a whole text value and each
    token in it, an integer as text, and those of a list or object's items."""
    if isinstance(cell, str):
        if len(cell) > MAX_CELL_CHARS:
            raise _Unreadable
        if cell.strip():
            yield cell.strip()
        for match in _TOKEN.finditer(cell):
            yield _token(match)
    elif isinstance(cell, int) and not isinstance(cell, bool):
        yield str(cell)
    elif isinstance(cell, (list, tuple, dict)) and depth < 2:
        for item in cell.values() if isinstance(cell, dict) else cell:
            yield from _cell_tokens(item, depth + 1)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def _measured(context, name: str):
    return next((c for c in context.measured.values() if c.name == name), None)


def _keep_refusal(record: dict, check: Any, blocks: Any, context) -> None:
    """Keep each value of one guard refusal not refused before in the call;
    a join refusal names no value and is left out."""
    if isinstance(check, dict) and check.get("kind") == "paired_join":
        return
    if not isinstance(check, dict) or check.get("kind") != "wrong_form":
        raise _Unreadable
    column, paired, values = check.get("column"), check.get("paired_column"), check.get("values")
    if not (isinstance(column, str) and isinstance(paired, str) and _strings(values)
            and _strings(blocks) and len(blocks) == len(values)
            and all(_PLAIN_VALUE.fullmatch(v) for v in values)):
        raise _Unreadable
    looked_in, other = _measured(context, column), _measured(context, paired)
    if looked_in is None or other is None:
        raise _Unreadable
    concept = check.get("concept") if isinstance(check.get("concept"), str) else ""
    forms = {"value_form": _form_labels(other, concept),
             "counterpart_form": _form_labels(looked_in, concept)}
    pending = record["pending"]
    for value, block in zip(values, blocks):
        key = (value.casefold(), column)
        if key in pending:
            continue
        if len(pending) >= MAX_REFUSALS:
            raise _Unreadable
        pending[key] = {"value": value, "folded": value.casefold(), "column": column,
                        "paired_column": paired, "block": block, "concept": concept,
                        "signature": looked_in.signature, "partner": other.signature,
                        "counterparts": set(), "keys": set(), **forms}


# --------------------------------------------------------------------------- #
# A result's rows, and which measured column each of its columns stands for
# --------------------------------------------------------------------------- #

def _row_cells(row: Any, names) -> tuple:
    """``(cells, names or None)`` of one row: a list under the result's
    column list, or an object under its own keys."""
    if isinstance(row, dict):
        return list(row.values()), [str(k) for k in row]
    if isinstance(row, (list, tuple)):
        named = names if names is not None and len(names) == len(row) else None
        if names is not None and named is None:
            raise _Unreadable
        return list(row), named
    raise _Unreadable


def _proven_sources(payload: dict, names, physical_schema) -> dict:
    """``{result column index: "table.column"}`` the SELECT list proves."""
    sql = payload.get("_adt_executed_query")
    if not (names and isinstance(sql, str) and sql.strip()):
        return {}
    from .identifier_provenance import _projected_sources
    found = _projected_sources(sql, names, physical_schema) or []
    return {index: f"{relation}.{column}".lower() for index, (relation, column) in found}


def _stands_for(index: int, names, proven: dict, measured_name: str) -> bool:
    """Whether result column ``index`` may hold ``measured_name``'s values:
    any column of an unnamed result; else its proven source, or, unproven, a
    result column of the measured column's own name."""
    if names is None:
        return True
    if index in proven:
        return proven[index] == measured_name
    own = str(names[index]).rsplit(".", 1)[-1].strip('"').lower()
    return own == measured_name.rsplit(".", 1)[-1]


def _shaped(cell: Any, signature: tuple) -> set:
    return {t.casefold() for t in _cell_tokens(cell) if t and _match(_sig(t), signature)}


def _link_keys(cell: Any, entry: dict) -> set:
    """A cell that is one identifier token of neither measured shape: a key
    the value's row returned that a later query may look the other form up by."""
    if not isinstance(cell, str):
        return set()
    text = cell.strip()
    if not _KEY.fullmatch(text) or not any(ch.isdigit() for ch in text):
        return set()
    sig = _sig(text)
    if _match(sig, entry["signature"]) or _match(sig, entry["partner"]):
        return set()
    return {text.casefold()}


def _add_counterparts(entry: dict, found: set) -> None:
    counterparts = entry["counterparts"]
    for token in sorted(found):
        if len(counterparts) < _MAX_COUNTERPARTS:
            counterparts.add(token)


def _read_row(entry: dict, cells: list, names, proven: dict, keys: set) -> None:
    """Record, for ``entry``, the counterparts and link keys of one row that
    holds its value under the paired column."""
    at = [i for i, cell in enumerate(cells)
          if _stands_for(i, names, proven, entry["paired_column"])
          and entry["folded"] in {t.casefold() for t in _cell_tokens(cell)}]
    if not at:
        return
    for j, cell in enumerate(cells):
        if j in at:
            continue
        if _stands_for(j, names, proven, entry["column"]):
            _add_counterparts(entry, _shaped(cell, entry["signature"]))
        keys.update(_link_keys(cell, entry))


def _literals(sql: Any) -> "set | None":
    """The plain string literals of ``sql`` (folded), or None when the
    safety layer's tokenizer is not sure of the statement."""
    if not isinstance(sql, str) or not sql.strip():
        return None
    from .athena_scope import tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return None
    found = set()
    for i, tok in enumerate(tokens):
        if tok.kind != "literal" or not tok.text.startswith("'") or len(tok.text) < 2:
            continue
        if i > 0 and tokens[i - 1].end == tok.start and tokens[i - 1].kind == "word":
            return None
        found.add(tok.text[1:-1].replace("''", "'").casefold())
    return found


def _read_chain(entry: dict, payload: dict, rows: list, names, proven: dict) -> None:
    """The second query of a two-query recovery: its only string literal is
    a link key an earlier row returned for ``entry``'s value."""
    literals = _literals(payload.get("_adt_executed_query"))
    if not literals or len(literals) != 1 or not literals <= entry["keys"]:
        return
    for row in rows:
        cells, row_names = _row_cells(row, names)
        row_proven = proven if row_names is names else {}
        for j, cell in enumerate(cells):
            if _stands_for(j, row_names, row_proven, entry["column"]):
                _add_counterparts(entry, _shaped(cell, entry["signature"]))


def _read_rows(record: dict, payload: dict, physical_schema) -> None:
    pending = list(record["pending"].values())
    if not pending:
        return
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("truncated") is True:
        raise _Unreadable
    names = payload.get("columns")
    names = [str(n) for n in names] if isinstance(names, list) else None
    proven = _proven_sources(payload, names, physical_schema)
    new_keys: dict = {id(entry): set() for entry in pending}
    for entry in pending:
        _read_chain(entry, payload, rows, names, proven)
    for row in rows:
        cells, row_names = _row_cells(row, names)
        row_proven = proven if row_names is names else {}
        for entry in pending:
            _read_row(entry, cells, row_names, row_proven, new_keys[id(entry)])
    for entry in pending:
        for key in sorted(new_keys[id(entry)]):
            if len(entry["keys"]) < MAX_KEYS:
                entry["keys"].add(key)


def _read_payload(record: dict, payload: dict, context, physical_schema) -> None:
    check = payload.get("identifier_check")
    if check is not None and payload.get("status") == "not_run":
        _keep_refusal(record, check, payload.get("identifier_miss"), context)
        return
    if "error" in payload:
        return
    _read_rows(record, payload, physical_schema)


def record_result(invocation_state: Any, result: Any, context, physical_schema=None) -> None:
    """Read one of this call's tool results into the call's record (see the
    module docstring). ``physical_schema`` (``response_integrity_sql.
    parse_physical_schema``) lets the SELECT list prove a column's source. A
    result with an error status returned no rows and is passed over; one the
    record cannot read marks the call unread."""
    if not isinstance(invocation_state, dict) or context is None:
        return
    record = _record(invocation_state)
    if record["unread"]:
        return
    if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
        return
    try:
        for payload in _payloads(result):
            _read_payload(record, payload, context, physical_schema or {})
    except _Unreadable:
        record["unread"] = True


# --------------------------------------------------------------------------- #
# The fact
# --------------------------------------------------------------------------- #

def _reported(answer: str) -> tuple:
    """``(tokens, unparsed)``: the folded values the answer's own
    IDENTIFIER-MISS blocks and no-record lines name, and whether the answer
    mentions IDENTIFIER-MISS without a token line the record can read."""
    from .system_agent import _MISS_TOKEN_RE
    tokens = {m.group(1).strip().strip("`'\"").casefold() for m in _NO_RECORD.finditer(answer)}
    mentioned = "IDENTIFIER-MISS" in answer.upper()
    named = {m.group(1).strip().strip("`'\"").casefold()
             for m in _MISS_TOKEN_RE.finditer(answer)} if mentioned else set()
    return tokens | named, mentioned and not named


def _line_tokens(line: str, split_slash: bool) -> list:
    """``(start, end, token)`` of the identifier tokens (holding a digit) of
    one line; with ``split_slash``, a token is also split at each ``/``."""
    out = []
    for match in _TOKEN.finditer(line):
        token, start = _token(match), match.start()
        parts = token.split("/") if split_slash and "/" in token else [token]
        for part in parts:
            if len(part) >= 3 and any(ch.isdigit() for ch in part):
                out.append((start, start + len(part), part))
            start += len(part) + 1
    return out


def _gap_symbols(gap: str) -> "list | None":
    """The words and pairing marks of the text between two values, quotes
    and emphasis left out; None when it holds anything else (a digit, a
    sentence mark, another identifier)."""
    text = _AKA_DOTTED.sub(" aka ", gap.lower())
    text = re.sub(r"[_-]", " ", re.sub(r"[`'\"*]", "", text))
    symbols = re.findall(r"[a-z]+|\S", text)
    return symbols if all(s.isalpha() or s in _MARKS for s in symbols) else None


def _names_form(words: list, form: tuple) -> bool:
    """Whether ``words`` are a label for ``form`` (``(prefix, phrases)``, see
    :func:`_form_labels`): an optional article and one modifier word, then
    the form's prefix (or a word for each of its letters) or one of its
    phrases, then an optional qualifier such as ``number``. A contrast word
    anywhere means no label."""
    prefix, phrases = form
    if not words or any(w in _BREAK_WORDS or not w.isalpha() for w in words if w != "#"):
        return False
    if words[0] in _ARTICLES:
        words = words[1:]
    if len(words) > 1 and words[-1] in _QUALIFIERS:
        words = words[:-1]
    for core in (words, words[1:]):
        if not core or len(core) > 4:
            continue
        if tuple(core) in phrases or (prefix and core in ([prefix], [prefix + "s"])):
            return True
        if prefix and len(prefix) >= 2 and len(core) == len(prefix) \
                and "".join(w[0] for w in core) == prefix:
            return True
    return False


def _after_pairing_word(symbols: list) -> "tuple[bool, list]":
    for phrase in _AKA_PHRASES:
        if symbols[:len(phrase)] == list(phrase):
            return True, symbols[len(phrase):]
    return False, symbols


def _pairs_across(gap: str, table_row: bool, later: tuple) -> bool:
    """Whether ``gap``, the text between two values, pairs them (see the
    module docstring). ``later`` is the form of the value after the gap."""
    if len(gap) > MAX_GAP:
        return False
    symbols = _gap_symbols(gap)
    if not symbols:
        return False
    if table_row and symbols == ["|"]:
        return True
    head = symbols[0] if symbols[0] in _OPENERS or symbols[0] == "," else ""
    said, rest = _after_pairing_word(symbols[1:] if head else symbols)
    if not (head or said) or (head == "," and not (said or rest)):
        return False
    return not rest or _names_form(rest, later)


def _form_labels(column, concept: str = "") -> tuple:
    """``(prefix, phrases)`` that name one measured column's form: the
    leading letters of its measured shape, and the words of its table name,
    its column name and its concept (the last two the pair shares)."""
    segments = column.signature[1] if len(column.signature) > 1 else ()
    first = segments[0] if segments else None
    prefix = first[1] if first and first[0] == "prefix" and str(first[1]).isalpha() else ""
    phrases = set()
    for name in (column.table, column.column, concept or column.concept):
        words = tuple(w for w in re.split(r"[^a-z]+", str(name or "").lower()) if w)
        if words:
            phrases.add(words)
    return str(prefix), frozenset(phrases)


def _owns(line: str, found: list, k: int, n: int, entry: dict) -> bool:
    """Whether the looked-in-shape token ``found[n]`` next to the value at
    ``found[k]`` is that value's: the text between them pairs them, and no
    value of the paired form is paired with it on its other side as well
    (then it could be either one's)."""
    if not _match(_sig(found[n][2]), entry["signature"]):
        return False
    table_row = line.lstrip().startswith("|")
    forms = {k: entry["value_form"], n: entry["counterpart_form"]}

    def paired(i: int, j: int) -> bool:
        a, b = (i, j) if found[i][0] < found[j][0] else (j, i)
        return _pairs_across(line[found[a][1]:found[b][0]], table_row,
                             forms.get(b, entry["value_form"]))

    if not paired(k, n):
        return False
    m = n + (n - k)
    if not 0 <= m < len(found) or not _match(_sig(found[m][2]), entry["partner"]):
        return True
    return not paired(n, m)


def _question_pairs(question: str, entry: dict) -> bool:
    """Whether the delegated question pairs the refused value with a
    counterpart of the looked-in column's shape (see the module docstring),
    or that cannot be told (a shape with a space)."""
    if " " in entry["signature"][0] or " " in entry["partner"][0]:
        return True
    split_slash = "/" not in entry["signature"][0] + entry["partner"][0]
    for line in str(question or "").splitlines():
        found = _line_tokens(line, split_slash)
        for k, (_start, _end, token) in enumerate(found):
            if token.casefold() == entry["folded"] and any(
                    _owns(line, found, k, n, entry) for n in (k - 1, k + 1)
                    if 0 <= n < len(found)):
                return True
    return False


def resolved(entry: dict, question: str) -> bool:
    """Whether the call resolved one refused value (an entry of the record's
    ``pending``): exactly one distinct counterpart was recorded for it, or the
    delegated ``question`` supplies its counterpart (:func:`_question_pairs`)."""
    return len(entry["counterparts"]) == 1 or _question_pairs(question, entry)


def _unreported_blocks(record: dict, answer: str, question: str) -> list:
    tokens, unparsed = _reported(answer)
    folded_answer = answer.casefold()
    blocks: list = []
    for entry in record["pending"].values():
        if resolved(entry, question):
            continue
        if entry["folded"] in tokens or (unparsed and entry["folded"] in folded_answer):
            continue
        blocks.append(entry["block"])
    return blocks


def engine_fact(answer: str, invocation_state: Any, question: str = "") -> str:
    """The fixed line and the guard's fenced IDENTIFIER-MISS blocks for this
    call's unresolved wrong-form refusals, ``""`` when none applies (see the
    module docstring). ``question`` is the delegated question alone."""
    record = invocation_state.get(STATE_KEY) if isinstance(invocation_state, dict) else None
    if not isinstance(record, dict) or record.get("unread") or not record.get("pending") \
            or not isinstance(answer, str):
        return ""
    if len({entry["folded"] for entry in record["pending"].values()}) > MAX_VALUES:
        return ""
    blocks = _unreported_blocks(record, answer, question)
    if not blocks:
        return ""
    one = len(blocks) == 1
    line = _LINE.format(what="a value" if one else "values",
                        that="that value" if one else "those values",
                        block="block" if one else "blocks", names="names" if one else "name")
    fenced = "\n\n".join(f"```\n{block}\n```" for block in blocks)
    return f"{MARKER} {line}\n\n{fenced}"


def context_of(tools) -> Any:
    """The guard context the first of ``tools`` carries under
    :data:`SHAPES_ATTR`, or None."""
    return next((getattr(t, SHAPES_ATTR) for t in tools or ()
                 if getattr(t, SHAPES_ATTR, None) is not None), None)
