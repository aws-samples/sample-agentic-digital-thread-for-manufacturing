# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Refuse a bound on a table's storage column next to another time window.

Measured on an analytics deployment: in 4 of 14 answers the SQL system agent
bounded the table's partition column (a record-creation date) as well as the
question's event-time window (a completion or start time). Rows whose
creation date fell outside the extra bound were dropped, the counts shrank
5x to 50x, and the answers stated them as fact. The agent added the bound
because its instructions said to always filter on partition columns; an
over-limit COUNT, SUM, MIN or MAX no longer needs one (the connector splits
it exactly, ``athena_exact_split``).

:func:`decide` is the one decision. The Athena connector calls it, through
``athena_tools._storage_bound_check``, before a model-written query runs (the
same place as the midnight-comparison refusal); the connector's own split
pieces never pass that way, and the SQL is never rewritten.

How it reads a query (:func:`bounded_columns`, shared by the refusal and the
disclosure): only a statement the shared tokenizer reads with certainty that
scans exactly ONE table. Each top-level AND conjunct of that table's WHERE
that compares the column itself (bare, quoted or qualified) with a literal of
any type (a string, a number, ``TRUE``/``FALSE``, a typed literal such as
``DATE '...'``, a literal-parsing function of one string, or
``CAST(<literal> AS <type>)``) by ``=``, ``<``, ``<=``, ``>``, ``>=``,
``BETWEEN`` or ``IN (...)`` bounds that column. A conjunct under a top-level
OR, a negated one, a wrapped column (``date(col)``) or a computed bound
(``current_date - INTERVAL ...``) is not read, and what is not read is never
refused.

What counts as what:

* a STORAGE column is one the table's storage is partitioned on: a Hive
  partition key, or the source column of an Iceberg partition field (a field
  named ``<column>`` or ``<column>_<year|month|day|hour>``, see
  :func:`field_sources`);
* a TIME column is a bounded column whose catalogued type (the describe the
  connector already reads) is ``date...`` or ``timestamp...``.

Refused, on an Iceberg table: a bounded temporal storage column next to any
other bounded time column, even one that is a storage source too (each bound
drops the other's rows). Not refused: the storage column is the time column
itself (an Iceberg ``day()`` partition of the column the question bounds), the
query bounds no other time column, a non-temporal storage column (a region: a
question may ask for that value, and the connector cannot see the question),
and a Hive-partitioned table, whose partition predicate the scan guard
requires (``athena_scope``), so refusing it would leave no query that can run.
A storage bound that runs is described instead (:func:`decide` returns it) so
the answer can disclose it.

:func:`decide_window` is the second decision, for a turn whose question has
resolved time windows (``time_windows``). Measured on the same deployment:
with a one-day window resolved for the question, the agent applied it only to
the table's partition column (a record-creation time) and to no event time,
so it counted the rows created that day. The connector cannot see the
question, so the system agent's guarded Athena tool calls this rule before
the query runs. Refused, on an Iceberg table only: the query's only bounded
date or timestamp column is a temporal storage column, the question does not
name it, the table has another date or timestamp column that is not a storage
source, and no such column appears anywhere in the WHERE clause. A table whose
time columns are all storage sources has nothing else to window on, so it is
never refused.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .athena_scope import _split_boolean, _strip_outer, _where_expression, relations, tokenize
from .connection_types import ATHENA_PARTITION_RULE

ERROR_NAME = "STORAGE_BOUND"
#: Why a query was refused (``StorageBoundError.reason``): a storage bound
#: next to another time bound (:func:`decide`), or a resolved time window
#: applied only to a storage column (:func:`decide_window`).
REASON_COMPETING = "competing_time_bound"
REASON_WINDOW_ON_STORAGE = "window_on_storage_column"
#: A query whose only bounded time column is a storage/write column, on a table
#: whose BUSINESS time (a state's own time) the rules know, and the question
#: does not name the storage column (:func:`decide_business_window`). Fires
#: whether or not the turn carries a resolved time window.
REASON_BUSINESS_TIME_ON_STORAGE = "business_time_on_storage_column"
#: Iceberg time transforms whose default field name is ``<column>_<name>``.
TIME_TRANSFORMS = ("year", "month", "day", "hour")
_MAX_CONJUNCT_CHARS = 160
_MAX_CONJUNCTS = 4

#: The accurate rule an Athena agent is told about partition columns (the
#: provision prompt, the raw run tool's docstring, the discovery summary, the
#: QUERY EFFICIENCY directive and the Mode-B text). The one copy lives in the
#: stdlib-only leaf ``connection_types``, so import-pure seams can use it.
PARTITION_RULE = ATHENA_PARTITION_RULE
#: The sentence onboarding baked into every Athena agent's stored registration
#: prompt before :data:`PARTITION_RULE` replaced it.
BAKED_PARTITION_SENTENCE = (
    "ALWAYS include partition predicates in WHERE clauses to keep scans small.")


def correct_baked_prompt(prompt):
    """``prompt`` with the baked :data:`BAKED_PARTITION_SENTENCE` replaced by
    :data:`PARTITION_RULE`. A registration prompt is stored when the agent is
    provisioned, so an agent provisioned before the rule changed still has the
    old sentence; the system agent corrects it each time it composes its
    prompt, with no re-provision (as the scan guard corrects a stale
    "(no partition keys)" bake at query time). Anything else is unchanged."""
    if not isinstance(prompt, str) or BAKED_PARTITION_SENTENCE not in prompt:
        return prompt
    return prompt.replace(BAKED_PARTITION_SENTENCE, PARTITION_RULE)


def _temporal(data_type: str) -> bool:
    return str(data_type or "").lower().startswith(("date", "timestamp"))


def _conjunct_text(sql: str, tokens) -> str:
    """The conjunct as written, whitespace collapsed, bounded."""
    text = " ".join(sql[tokens[0].start:tokens[-1].end].split())
    return text if len(text) <= _MAX_CONJUNCT_CHARS else text[:_MAX_CONJUNCT_CHARS - 3] + "..."


def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


#: Functions whose one string argument makes a typed literal.
_LITERAL_FUNCS = frozenset({"date", "timestamp", "from_iso8601_timestamp", "from_iso8601_date"})


def _strip_postfix_cast(tokens):
    """``tokens`` without a trailing DuckDB ``:: <type>`` postfix cast at the
    outer level (``'2026-01-01'::TIMESTAMP``, ``x::TIMESTAMPTZ``), else
    ``tokens`` unchanged. ``::`` tokenises as two ``:`` punct tokens; the type
    is one or more trailing word/number/punct tokens (no literal, no operator),
    so only a cast of a value to a type is stripped, never a comparison."""
    base = min((t.depth for t in tokens), default=0)
    colons = [i for i in range(len(tokens) - 1)
              if tokens[i].text == ":" and tokens[i + 1].text == ":"
              and tokens[i].depth == base and tokens[i + 1].depth == base]
    if not colons:
        return tokens
    at = colons[-1]
    tail = tokens[at + 2:]
    if not tail or any(t.kind in {"literal", "operator"} or t.text == ";" for t in tail):
        return tokens
    return tokens[:at]


def _constant(tokens) -> bool:
    """A literal of any type: a string or number, ``TRUE``/``FALSE``, a typed
    literal (``DATE '...'``, ``TIMESTAMP '...'``, ``DECIMAL '...'``), a
    literal-parsing function of one string, ``CAST(<literal> AS <type>)``, or a
    DuckDB postfix cast of any of those (``'2026-01-01'::TIMESTAMP``)."""
    tokens = _strip_outer(tokens)
    stripped = _strip_postfix_cast(tokens)
    if stripped is not tokens and len(stripped) < len(tokens):
        return _constant(stripped)
    if len(tokens) == 1:
        return tokens[0].kind in {"literal", "number"} or _upper(tokens[0]) in {"TRUE", "FALSE"}
    if len(tokens) == 2:
        return tokens[0].kind == "word" and tokens[1].kind == "literal" \
            and _upper(tokens[0]) not in {"NOT", "AND", "OR"}
    if len(tokens) == 4 and tokens[0].kind == "word" and tokens[1].text == "(":
        return (tokens[0].value.lower() in _LITERAL_FUNCS and tokens[2].kind == "literal"
                and tokens[3].text == ")")
    if (len(tokens) >= 6 and _upper(tokens[0]) == "CAST" and tokens[1].text == "("
            and tokens[-1].text == ")"):
        depth = tokens[1].depth + 1
        at = next((i for i in range(2, len(tokens) - 1)
                   if tokens[i].depth == depth and _upper(tokens[i]) == "AS"), None)
        return at is not None and _constant(tokens[2:at]) and all(
            t.kind in {"word", "number", "punct"} and t.text != ";" for t in tokens[at + 1:-1])
    return False


def _bounded_column(tokens) -> str | None:
    """The column one conjunct bounds with literals: ``col <op> literal``
    (either side, ``=``, ``<``, ``<=``, ``>``, ``>=``), ``col BETWEEN literal
    AND literal`` or ``col IN (literal, ...)``; ``None`` for anything else
    (``NOT``, ``!=``, ``IS NULL``, ``LIKE``, a wrapped column, a computed
    value)."""
    from .athena_exact_shape import _column_ref
    base = min(t.depth for t in tokens)
    words = [(i, _upper(t)) for i, t in enumerate(tokens) if t.depth == base]
    if any(word == "NOT" for _i, word in words):
        return None
    for i, word in words:
        if word == "BETWEEN":
            and_at = next((j for j, w in words if j > i and w == "AND"), None)
            if and_at is None or not (_constant(tokens[i + 1:and_at]) and _constant(tokens[and_at + 1:])):
                return None
            return _column_ref(tokens[:i])
        if word == "IN":
            values = _strip_outer(tokens[i + 1:])
            if not values or tokens[i + 1].text != "(" or any(_upper(t) == "SELECT" for t in values):
                return None
            depth = min(t.depth for t in values)
            cuts = [-1] + [j for j, t in enumerate(values) if t.depth == depth and t.text == ","]
            parts = [values[a + 1:b] for a, b in zip(cuts, cuts[1:] + [len(values)])]
            return _column_ref(tokens[:i]) if all(p and _constant(p) for p in parts) else None
    for i, token in enumerate(tokens):
        if token.depth == base and token.kind == "operator":
            if token.text not in {"=", "<", "<=", ">", ">="}:
                return None
            left, right = tokens[:i], tokens[i + 1:]
            if _constant(right):
                return _column_ref(left)
            return _column_ref(right) if _constant(left) else None
    return None


def _same_column_or(part) -> str | None:
    """The one column a top-level AND conjunct that is an OR of branches bounds,
    when EVERY branch (outer parentheses stripped) is a non-empty AND of
    conjuncts that each bound the SAME one column with literals
    (:func:`_bounded_column`); ``None`` otherwise. So a date window written as a
    union of ranges on one column (``(col range1) OR (col range2)``) reads as a
    bound on that column. Anything else stays unread: fewer than two branches,
    a branch that is empty or holds a non-bound conjunct (``NOT``, a wrapped or
    computed bound, a nested OR), or branches that bound different columns."""
    branches = _split_boolean(part, "OR")
    if len(branches) < 2:
        return None
    column: str | None = None
    for branch in branches:
        conjuncts = _split_boolean(_strip_outer(branch), "AND")
        if not conjuncts:
            return None
        for conjunct in conjuncts:
            conjunct = _strip_outer(conjunct)
            if not conjunct or len(_split_boolean(conjunct, "OR")) > 1:
                return None
            bound = _bounded_column(conjunct)
            if bound is None:
                return None
            bound = bound.lower()
            if column is None:
                column = bound
            elif bound != column:
                return None
    return column


def bounded_columns(sql: str, *, include_same_column_or: bool = False):
    """``(table name parts, {column: [conjunct text, ...]})`` for the columns
    the one table a statement scans is bounded on, or ``None`` when the
    statement is not one certain single-table read (see the module
    docstring). Column names are lowercased; name parts stay as written.
    The refusal and the disclosure both read bounds through this.

    With ``include_same_column_or`` (the window rules' call sites only, never
    the storage consumers), a top-level AND conjunct that is a same-column OR
    of ranges (:func:`_same_column_or`) also counts as a bound on that column,
    the whole OR conjunct recorded as its text. The default is byte-identical:
    a conjunct under an OR is skipped, as before."""
    sql = sql if isinstance(sql, str) else ""
    tokens, uncertain = tokenize(sql)
    if uncertain or not tokens:
        return None
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1:
        return None
    found: dict[str, list[str]] = {}
    where = _where_expression(tokens, scans[0])
    for part in (_split_boolean(where, "AND") if where else []):
        part = _strip_outer(part)
        if not part:
            continue
        if len(_split_boolean(part, "OR")) > 1:
            column = _same_column_or(part) if include_same_column_or else None
            if column:
                found.setdefault(column, []).append(_conjunct_text(sql, part))
            continue
        column = _bounded_column(part)
        if column:
            found.setdefault(column.lower(), []).append(_conjunct_text(sql, part))
    return tuple(scans[0].parts), found


def field_sources(fields, column_types: dict) -> set[str]:
    """The source columns of Iceberg partition fields: a field named for a
    column of the table, or ``<column>_<time transform>`` of a column whose
    type is a date or timestamp."""
    out: set[str] = set()
    for field in fields or ():
        name = str(field).lower()
        if name in column_types:
            out.add(name)
            continue
        base, sep, suffix = name.rpartition("_")
        if sep and suffix in TIME_TRANSFORMS and _temporal(column_types.get(base, "")):
            out.add(base)
    return out


@dataclass(frozen=True)
class StorageBound:
    """The storage bound one query runs with (or is refused for)."""

    table: str
    storage: tuple[str, ...]
    other: tuple[str, ...]
    conjuncts: tuple[str, ...]
    #: "ingestion" when the caller declared a write-time basis and the query
    #: ran on it; "" otherwise (the record shape is then unchanged).
    basis: str = ""

    def to_dict(self) -> dict:
        record = {"table": self.table, "columns": list(self.storage),
                  "conjuncts": list(self.conjuncts)}
        if self.basis:
            record["basis"] = self.basis
        return record


class StorageBoundError(ValueError):
    """A query refused before it ran (see the module docstring). ``refused``
    are the temporal storage columns, ``others`` the bounded time columns
    each competes with (none for a window applied only to a storage column),
    ``reason`` which rule refused it."""

    def __init__(self, bound: StorageBound, refused: tuple, others: tuple, *,
                 reason: str = REASON_COMPETING):
        super().__init__(f"{ERROR_NAME}: {', '.join(refused)}")
        self.bound, self.refused, self.others = bound, refused, others
        self.reason = reason

    def _window_guidance(self, table: str, storage: str) -> str:
        return (f"Refused before running: this turn's question has resolved time windows, and "
                f"this query applies its window only to {storage}, a storage column of "
                f"{table} (its data is partitioned on it). Rows are stored by {storage}, which "
                "need not be when the counted state happened, so the result can count the wrong "
                "rows. Apply the window to the time column that records the state being "
                "counted, each state on its own time. The tool computes a COUNT, SUM, MIN or MAX "
                "that is over the scan limit exactly from partition pieces, so no partition bound "
                f"is needed. If {storage} is itself the time of the state being counted, send the "
                "same query again unchanged: it runs, and the answer carries that bound.")

    def _business_time_guidance(self, table: str, storage: str, names) -> str:
        """``names`` is :meth:`to_dict`'s ``engine_names.Withheld``, so a
        business column withheld here is counted once in its payload."""
        business = " or ".join(names.show(c) for c in self.others) if self.others else "a time column"
        return (f"Refused before running: this query's only time bound is on {storage}, a storage "
                f"column of {table} (its data is partitioned on it, so it records when each row "
                f"was WRITTEN, not when the counted state happened). Rows are usually written on "
                f"earlier days than they complete, so windowing on {storage} drops most of the "
                f"day's output and the count is wrong. Filter on the business time column the "
                f"question is about instead ({business}), each state on its own time, and remove "
                f"the {storage} bound. The tool computes a COUNT, SUM, MIN or MAX that is over the "
                f"scan limit exactly, so no partition bound is needed. Only if the question is "
                f"genuinely about when rows were written ({storage}) is a bound on it correct; "
                f"if the question does not say which time it means (e.g. start vs completion), ask "
                f"the user which once rather than guess.")

    def to_dict(self) -> dict:
        """The refusal payload. A table or column name that is not a plain
        identifier (``engine_names``) is never printed: the guidance puts a
        placeholder in its place and says how many it withheld, the name
        fields leave it out, and ``names_withheld`` counts it."""
        from .engine_names import Withheld, printable
        b, names = self.bound, Withheld()
        table = names.show(b.table, "table")
        storage = " and ".join(names.show(c) for c in self.refused)
        other = " and ".join(names.show(c) for c in self.others)
        if set(self.refused) & set(self.others):
            what = (f"Refused before running: {storage} are storage columns of {table} (its "
                    "data is partitioned on them), and this query bounds more than one of them. "
                    "A bound on a storage column the question does not ask about drops every row "
                    "outside it and changes the result. Keep the bound on the one time column "
                    "the question asks about and remove the others.")
        else:
            what = (f"Refused before running: {storage} is a storage column of {table} (its "
                    f"data is partitioned on it), and this query bounds it as well as {other}. "
                    f"That drops every row whose {storage} falls outside the bound and changes "
                    f"the result. Remove the bound on {storage} and keep the one on {other} (if "
                    f"the question asks for {storage}'s range, remove the {other} bound instead).")
        guidance = (
            f"{what} The tool computes a COUNT, SUM, MIN or MAX that is over the scan limit "
            "exactly from partition pieces; for any other query that is too large, narrow "
            "the question's own time window, or report that it could not run.")
        if self.reason == REASON_WINDOW_ON_STORAGE:
            guidance = self._window_guidance(table, storage)
        elif self.reason == REASON_BUSINESS_TIME_ON_STORAGE:
            guidance = self._business_time_guidance(table, storage, names)
        out = {
            "error": "storage_bound",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "reason": self.reason,
            "retryable": True,
            "table": b.table if printable(b.table) else "",
            "storage_columns": [c for c in self.refused if printable(c)],
            "time_columns": [c for c in self.others if printable(c)],
            "guidance": guidance + names.sentence(),
        }
        if names.count:
            out["names_withheld"] = names.count
        return out


class WindowRefusals(StorageBoundError):
    """Several window rules refusing ONE query on the same send
    (``system_agent._enforce_window_refusals``): one refusal carrying every
    remedy, so the identical resend runs after one round trip. The payload is
    the first rule's, with each other rule's guidance appended and its error
    name and reason under ``also_refused``. Every field comes from the rules'
    own :meth:`to_dict`, which already withholds non-printable names."""

    def __init__(self, errors):
        first = errors[0]
        super().__init__(first.bound, first.refused, first.others, reason=first.reason)
        self.errors = tuple(errors)

    def to_dict(self) -> dict:
        out = dict(self.errors[0].to_dict())
        rest = [error.to_dict() for error in self.errors[1:]]
        out["guidance"] = " ".join(
            [str(out.get("guidance") or "")] + [str(r.get("guidance") or "") for r in rest]).strip()
        out["also_refused"] = [{"error_name": r.get("error_name"), "reason": r.get("reason")}
                               for r in rest]
        withheld = sum(int(r.get("names_withheld") or 0) for r in rest)
        if withheld:
            out["names_withheld"] = int(out.get("names_withheld") or 0) + withheld
        return out


def decide(table: str, bounded: dict, storage_columns, column_types: dict, *,
           iceberg: bool) -> StorageBound | None:
    """The one decision.

    * Refused (:class:`StorageBoundError`), on an Iceberg table only: a
      bounded TEMPORAL storage column next to any other bounded date or
      timestamp column, including one that is a storage source itself.
    * Otherwise, when a storage column of any type is bounded, the
      :class:`StorageBound` the query runs with (all bounded storage columns,
      for disclosure). A non-temporal storage column (a region) is never
      refused: the question may ask for that value, and the connector cannot
      see the question.
    * ``None`` when no storage column is bounded."""
    storage_set = {str(c).lower() for c in storage_columns or ()}
    storage = tuple(sorted(c for c in bounded if c in storage_set))
    if not storage:
        return None
    temporal = {c for c in bounded if _temporal(column_types.get(c, ""))}
    refused = tuple(c for c in storage if c in temporal)
    others = tuple(sorted({c for p in refused for c in temporal - {p}}))
    conjuncts = tuple(text for c in storage for text in bounded[c])[:_MAX_CONJUNCTS]
    bound = StorageBound(table, storage, others, conjuncts)
    if others and iceberg:
        raise StorageBoundError(bound, tuple(c for c in refused if temporal - {c}), others)
    return bound


def where_names(sql: str) -> set[str] | None:
    """Every name the WHERE clause of a certain single-table read mentions
    (lowercased; each part of a dotted name, and a quoted identifier as
    written), in any form: wrapped, computed, under OR or NOT. A function
    name and a typed literal's type word (``DATE '...'``) are not names.
    ``None`` when the statement is not one certain single-table read."""
    tokens, uncertain = tokenize(sql if isinstance(sql, str) else "")
    if uncertain or not tokens:
        return None
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1:
        return None
    where = _where_expression(tokens, scans[0]) or []
    names = set()
    for i, token in enumerate(where):
        after = where[i + 1] if i + 1 < len(where) else None
        if token.kind == "identifier" or (
                token.kind == "word" and not (after is not None and (
                    after.kind == "literal" or after.text == "("))):
            names.add(str(token.value).lower())
    return names


def _column_tokens(sql: str):
    """``(tokens, scan)`` of one certain single-table read, else ``None``: the
    shared parse the storage-day scanner reuses."""
    tokens, uncertain = tokenize(sql if isinstance(sql, str) else "")
    if uncertain or not tokens:
        return None
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1:
        return None
    return tokens, scans[0]


#: Aggregate functions whose single argument is a freshness read of one
#: column: ``MIN(col)`` / ``MAX(col)`` are the earliest/latest value, not a
#: window that drops rows (Exemption A, the structural freshness read).
_FRESHNESS_AGGREGATES = frozenset({"min", "max"})


def _is_column_ref_token(tokens, i: int) -> bool:
    """Whether ``tokens[i]`` is a reference to a column value (not a function
    name, a dotted-name qualifier, or a typed literal's type word). A column
    word is a bare name or the terminal of a qualified ``a.b`` reference; it is
    not followed by ``(`` (a call) nor by a string literal (``DATE '...'``),
    and not followed by ``.`` (it is the qualifier, not the column)."""
    token = tokens[i]
    if token.kind not in {"word", "identifier"}:
        return False
    after = tokens[i + 1] if i + 1 < len(tokens) else None
    if after is not None and (after.text in {"(", "."} or after.kind == "literal"):
        return False
    return True


def _relation_decl_tokens(tokens, scan) -> set:
    """The token indices the FROM relation declaration occupies: the dotted
    table-name tokens, an optional ``AS``, and the alias. A relation alias that
    happens to equal a storage column name (``FROM events AS created``) sits
    here, so excluding these positions stops it being read as a column
    reference (it never reads the storage column). Walks the same name and
    ``[AS] alias`` shape the tokenizer parsed the relation from
    (``athena_scope``)."""
    start = getattr(scan, "table_token_index", None)
    if start is None or start >= len(tokens):
        return set()
    out = {start}
    j = start + 1
    while (j + 1 < len(tokens) and tokens[j].text == "."
           and tokens[j + 1].kind in {"word", "identifier"}):
        out.update({j, j + 1})
        j += 2
    if j < len(tokens) and tokens[j].kind == "word" and tokens[j].value.upper() == "AS":
        out.add(j)
        j += 1
    if (j < len(tokens) and tokens[j].depth == scan.depth
            and tokens[j].kind in {"word", "identifier"}
            and str(tokens[j].value).lower() == str(scan.alias).lower()):
        out.add(j)
    return out


def _storage_refs(tokens, storage_set, scan) -> list:
    """Indices of every reference to a storage column anywhere in ``tokens``
    (:func:`_is_column_ref_token`), lowercased match, EXCLUDING the FROM
    relation's own declaration tokens (:func:`_relation_decl_tokens`) so an
    alias named for a storage column is never read as a column. Any wrapping
    function -- ``CAST``, ``date_trunc``, ``strftime``, ``extract``, an
    arithmetic expression -- leaves the inner column as its own token, so this
    reads the storage use whatever wraps it, with no function allowlist."""
    lowered = {str(c).lower() for c in storage_set or ()}
    declared = _relation_decl_tokens(tokens, scan)
    return [i for i, t in enumerate(tokens)
            if i not in declared and _is_column_ref_token(tokens, i)
            and str(t.value).lower() in lowered]


def _enclosing_aggregate(tokens, i: int) -> str | None:
    """The lowercased aggregate function whose argument list directly encloses
    ``tokens[i]``, else ``None``: the nearest ``<word>(`` opened at one depth
    below ``tokens[i]`` whose word is an aggregate. Reads only the token
    structure, so ``MAX(col)`` and ``MAX(date_trunc('day', col))`` both report
    ``max`` for the inner ``col``."""
    target = tokens[i].depth
    for j in range(i - 1, -1, -1):
        token = tokens[j]
        if token.text == "(" and token.depth == target - 1:
            word = tokens[j - 1] if j >= 1 else None
            if word is not None and word.kind == "word":
                return word.value.lower()
            return None
        if token.depth < target - 1:
            return None
    return None


def _clause_of(tokens, i: int, scan) -> str:
    """The top-level clause of the scan ``tokens[i]`` belongs to -- ``select``,
    ``where``, ``group``, ``having``, ``order`` or ``other``. The reference may
    be nested in a function call (``CAST(col ...)``, ``MAX(col)``), so this
    walks back to the nearest clause keyword written at the SCAN's own depth,
    not the reference's. A reference whose walk-back crosses below the scan
    depth (its own subquery or derived table) reads as ``other``: that scope
    has its own clauses, so it is left to its own read."""
    depth = scan.depth
    keywords = {"SELECT": "select", "WHERE": "where", "GROUP": "group",
                "HAVING": "having", "ORDER": "order"}
    for j in range(i - 1, scan.select_index - 1, -1):
        token = tokens[j]
        if token.depth < depth:
            return "other"
        if token.depth != depth:
            continue
        word = token.value.upper() if token.kind == "word" else ""
        if word in keywords:
            return keywords[word]
    return "other"


def _single_top_level_select(tokens, scan) -> bool:
    """Whether the statement is ONE top-level SELECT with no CTE, no subquery
    and no join: the scan sits at the outer depth (0), the statement opens no
    ``WITH``, and it holds exactly one ``SELECT``. A derived table or CTE
    pushes the scan below depth 0 and adds a second ``SELECT`` or a ``WITH``,
    so a MIN/MAX read nested in one (whose value can then filter the outer
    result) is never read as a pure freshness read."""
    if scan.depth != 0:
        return False
    if any(t.kind == "word" and t.value.upper() == "WITH" for t in tokens):
        return False
    selects = sum(1 for t in tokens if t.kind == "word" and t.value.upper() == "SELECT")
    return selects == 1


def _storage_use(sql: str, storage_set, business_set):
    """Read how a single-table statement uses storage columns, for the
    business-time rule. Returns ``None`` when the statement is not one certain
    single-table read or references no storage column; otherwise
    ``(used columns, exempt)`` where ``used`` are the lowercased storage
    columns referenced anywhere and ``exempt`` is True when the one structural
    freshness read (Exemption A) applies.

    Exemption A, structural (narrow): every storage reference is the direct
    argument of ``MIN``/``MAX`` in the SELECT list of ONE top-level SELECT with
    no CTE, no subquery and no join (:func:`_single_top_level_select`), and the
    storage column is referenced nowhere else. That is the earliest/latest
    stored value as an output, which drops no rows. A MIN/MAX inside a CTE or
    derived table whose value then filters the outer result is NOT this read
    (the aggregate controls which rows survive), and a bare ``ORDER BY`` of the
    storage column is no longer exempt (a 'latest rows' listing uses
    ``time_basis='ingestion'``)."""
    parsed = _column_tokens(sql)
    if parsed is None:
        return None
    tokens, scan = parsed
    refs = _storage_refs(tokens, storage_set, scan)
    if not refs:
        return None
    used = sorted({str(tokens[i].value).lower() for i in refs})
    clauses = [_clause_of(tokens, i, scan) for i in refs]
    aggs = [_enclosing_aggregate(tokens, i) for i in refs]
    only_minmax_select = (_single_top_level_select(tokens, scan) and all(
        clause == "select" and agg in _FRESHNESS_AGGREGATES
        for clause, agg in zip(clauses, aggs)))
    return used, bool(only_minmax_select)


def decide_window(table: str, bounded: dict, storage_columns, column_types: dict, *,
                  iceberg: bool, sql: str, question: str) -> None:
    """The resolved-window decision (see the module docstring), for a turn
    whose question has resolved time windows; the caller knows that, the
    connector does not.

    Raises :class:`StorageBoundError` (reason
    :data:`REASON_WINDOW_ON_STORAGE`) when, on an Iceberg table, the one
    bounded date or timestamp column is a storage column the question does
    not name, the table has another date or timestamp column that is not a
    storage source, and no such column is mentioned in the WHERE clause.
    Returns ``None`` otherwise; a query with two or more bounded time columns
    is :func:`decide`'s."""
    if not iceberg:
        return None
    storage_set = {str(c).lower() for c in storage_columns or ()}
    temporal = sorted(c for c in bounded if _temporal(column_types.get(c, "")))
    if len(temporal) != 1 or temporal[0] not in storage_set:
        return None
    column = temporal[0]
    from .storage_bounds import _asked
    if _asked(question, column):
        return None
    others = {c for c, t in column_types.items() if _temporal(t) and c not in storage_set}
    named = where_names(sql)
    if not others or named is None or named & others:
        return None
    bound = StorageBound(table, (column,), (), tuple(bounded[column])[:_MAX_CONJUNCTS])
    raise StorageBoundError(bound, (column,), (), reason=REASON_WINDOW_ON_STORAGE)


def decide_business_window(table: str, bounded: dict, storage_columns, column_types: dict, *,
                           iceberg: bool, sql: str, question: str, business_columns,
                           time_basis: str = "business"):
    """Refuse a query that uses a table's STORAGE column for its time basis,
    on a table whose BUSINESS time the rules know (``business_columns``).

    STORAGE USE is ANY reference to a storage column anywhere in the single-
    table statement (:func:`_storage_use`), whatever function wraps it: there
    is no wrapper allowlist, so ``CAST``, ``date_trunc``, ``strftime`` and
    ``extract`` all read as storage use. Rows are written before the counted
    state happens, so a storage-time basis drops the day's output and the count
    is wrong. Raises :class:`StorageBoundError`
    (:data:`REASON_BUSINESS_TIME_ON_STORAGE`) on an Iceberg table with a known
    business column when the statement uses a storage column, bounds no
    business column, and neither exemption applies. A business column mentioned
    elsewhere does not disable the rule.

    Exemption A, structural (:func:`_storage_use`): a ``MIN``/``MAX`` freshness
    read in a plain top-level SELECT, which drops no rows, runs with no
    disclosure. Exemption B, explicit (``time_basis='ingestion'``): the caller
    declared a write-time basis, so non-exempt storage use RUNS but the rule
    returns a :class:`StorageBound` disclosure record (the storage columns and
    their WHERE conjuncts) so every ingestion run, whatever its shape, labels
    the result and carries the storage-basis disclosure to the answer. Returns
    a :class:`StorageBound` for a disclosed ingestion run, or ``None``."""
    if not iceberg:
        return None
    business = {str(c).lower() for c in business_columns or ()}
    if not business:
        return None
    if any(str(c).lower() in business for c in bounded):
        return None                      # bounds a business column: the competing rule's case
    use = _storage_use(sql, storage_columns, business)
    if use is None:
        return None                      # no storage column is referenced
    used, exempt = use
    if exempt:
        return None                      # Exemption A: a freshness read, which drops no rows
    from .storage_bounds import _asked
    bound = _business_storage_bound(table, tuple(used), business, bounded)
    if str(time_basis or "").lower() == "ingestion":
        # Exemption B: run, carrying a record marked with its write-time basis.
        return replace(bound, basis="ingestion")
    if any(_asked(question, c) for c in used):
        return None                      # the question is about the storage column
    raise StorageBoundError(bound, bound.storage, bound.other,
                            reason=REASON_BUSINESS_TIME_ON_STORAGE)


def _business_storage_bound(table, used, business, bounded) -> StorageBound:
    """The :class:`StorageBound` the business-time rule refuses with (or
    discloses under an ingestion basis): the storage columns the statement
    uses, the known business columns they compete with, and the storage
    columns' own WHERE conjuncts (empty for a projection/grouping)."""
    others = tuple(sorted(business))
    conjuncts = tuple(t for c in used for t in bounded.get(c, ()))[:_MAX_CONJUNCTS]
    return StorageBound(table, used, others, conjuncts)
