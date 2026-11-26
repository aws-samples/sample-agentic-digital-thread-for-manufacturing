# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-physical-read evaluation support for the data rules, with an ALLOWLIST
prover.

The record-key and the three catalog window rules (storage, state-time,
record-time) read ONE single-table statement; they return nothing for a join,
CTE-plus-join or subquery, so a multi-scan statement used to bypass them. This
module proves a statement is built ONLY from shapes whose every physical read
can be evaluated on its own single-table block, then splits it into those
reads so the guard can run the single-table rules on EACH read's own query
block.

The prover (:func:`prove`) is a closed classification, NOT a denylist. A
statement that reads a table carrying a single-table-only rule runs ONLY when
the prover returns ``PROVEN``; every other verdict -- ``COMBINED`` (a shape the
rules cannot span) or ``UNPROVEN`` (a shape the prover does not recognise, or
any parse uncertainty) -- hard-refuses. The allowlist permits exactly three
proven families:

1. a single base-table scan;
2. a scalar / ``EXISTS`` / ``IN`` subquery (in WHERE, HAVING or the SELECT
   list) that is itself proven;
3. a chain of single-table CTEs / derived tables with NO join, where every
   SELECT block at every depth has exactly one FROM item.

Anything else -- a set operator (``UNION`` / ``INTERSECT`` / ``EXCEPT``)
anywhere, ``LATERAL`` / ``VALUES`` / ``UNNEST`` / a table function in FROM, a
positional join, or any ``JOIN`` keyword / comma / ``USING`` / ``NATURAL`` in
any FROM clause at any depth -- is refused.

For each proven physical scan it reconstructs a standalone single-table
statement from that scan's own query block, with every NESTED subquery removed,
so the per-read rules see the outer filters/windows/aggregates traced along the
single chain down to the one physical read.

Pure / offline: it only tokenizes and rebuilds text; it executes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass

from .athena_scope import Token, relations, tokenize

#: The prover's closed verdicts.
PROVEN = "PROVEN"
COMBINED = "COMBINED"
UNPROVEN = "UNPROVEN"

_SET_OPS = {"UNION", "INTERSECT", "EXCEPT"}
_JOIN_WORDS = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
               "OUTER", "POSITIONAL", "ASOF", "ANTI", "SEMI"}
_FROM_SOURCE_FORMS = {"LATERAL", "VALUES", "UNNEST"}
#: Clause keywords that end a FROM list at the FROM's own depth.
_FROM_LIST_END = {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
                  "QUALIFY", "WINDOW", "UNION", "INTERSECT", "EXCEPT", "FETCH"}


@dataclass(frozen=True)
class Read:
    """One physical read of a base table: the reconstructed single-table
    statement for its query block (``block_sql``), the terminal table name and
    whether it was quoted, and ``multi_scan`` -- True when the block cannot be
    evaluated on its own.

    ``combines_relations`` marks the ONE synthetic read a non-``PROVEN``
    statement returns, so the caller refuses the whole statement once;
    ``combined_terminals`` are the rule-relevant base tables recovered from it
    (possibly empty for an ``UNPROVEN`` shape whose terminals could not be
    recovered); ``unproven`` is True when the verdict was ``UNPROVEN`` (a shape
    the prover does not recognise, or parse uncertainty), so the caller refuses
    even if no terminal was recovered, whenever the system has any
    single-table-only rule."""

    block_sql: str
    terminal: str
    quoted: bool
    multi_scan: bool
    combines_relations: bool = False
    combined_terminals: tuple = ()
    unproven: bool = False
    #: On the synthetic non-PROVEN read: whether the statement reads a relation
    #: (has a clause FROM). A no-data statement (``SELECT 1`` / ``SHOW`` /
    #: ``DESCRIBE``) is UNPROVEN under the per-relation contract but reads
    #: nothing, so the caller does not refuse it.
    reads_data: bool = True


@dataclass(frozen=True)
class Proof:
    """A closed classification of a statement.

    * ``PROVEN``: ``reads`` is the per-read list, one entry per physical scan,
      each evaluable on its own single-table block.
    * ``COMBINED``: a recognised shape the single-table rules cannot span (a
      join, set operation, or a FROM item that combines relations);
      ``terminals`` are the rule-relevant base tables it reaches.
    * ``UNPROVEN``: an unrecognised shape, or parse uncertainty; ``terminals``
      is best-effort and may be empty. The caller refuses whenever the system
      has any single-table-only rule, even with no terminal recovered."""

    verdict: str
    reads: tuple = ()
    terminals: tuple = ()


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _is_query_open(tokens, index: int) -> bool:
    """A ``(`` at ``index`` whose body opens a query (``SELECT`` / ``WITH`` /
    ``VALUES``): a nested subquery, dropped from the owning block."""
    if index >= len(tokens) or tokens[index].text != "(":
        return False
    nxt = tokens[index + 1] if index + 1 < len(tokens) else None
    return nxt is not None and _upper(nxt) in {"SELECT", "WITH", "VALUES"}


def _matching_close(tokens, open_index: int) -> int | None:
    depth = tokens[open_index].depth
    for i in range(open_index + 1, len(tokens)):
        if tokens[i].text == ")" and tokens[i].depth == depth:
            return i
    return None


def _block_end(tokens, select_index: int, depth: int) -> int:
    """The index one past the query block opened at ``select_index``: the first
    token whose depth drops below ``depth``, a sibling ``SELECT`` or a set
    operator at ``depth``, else the end. Keeps GROUP BY / HAVING / QUALIFY in
    the block, so the reconstructed statement carries the clauses the rules
    read."""
    from .athena_scope import _upper as _u
    for i in range(select_index + 1, len(tokens)):
        t = tokens[i]
        if t.depth < depth:
            return i
        if t.depth == depth and _u(t) in ({"SELECT"} | _SET_OPS):
            return i
    return len(tokens)


def _terminal_of(tokens, relation) -> tuple:
    """``(terminal name, quoted)`` of a relation: its last dotted identifier
    and whether that terminal token was quoted."""
    i = relation.table_token_index
    terminal = tokens[i] if i < len(tokens) else None
    j = i + 1
    while (j + 1 < len(tokens) and tokens[j].text == "."
           and tokens[j + 1].kind in {"word", "identifier"}):
        terminal = tokens[j + 1]
        j += 2
    if terminal is None:
        return relation.table, relation.quoted
    return str(terminal.value), terminal.kind == "identifier"


def _block_tokens_without_subqueries(tokens, start: int, end: int, depth: int) -> list:
    """The tokens of ``tokens[start:end]`` with every nested subquery removed:
    a ``(`` that opens a query is skipped to its matching close. Value-parens
    (an ``IN (...)`` list, a function call, a ``COALESCE(...)``) are kept whole,
    so the reconstructed statement stays a valid single-table read."""
    out: list = []
    i = start
    while i < end:
        if _is_query_open(tokens, i):
            close = _matching_close(tokens, i)
            if close is None or close >= end:
                break
            i = close + 1
            continue
        out.append(tokens[i])
        i += 1
    return out


def _rebuild(tokens: list) -> str:
    """A SQL string from a token list, one space between tokens, no space
    before ``.`` / ``,`` / ``)`` or after ``.`` / ``(``. Rebuilt text is only
    re-tokenized by the rules, never executed, so spacing need only parse."""
    parts: list[str] = []
    for i, t in enumerate(tokens):
        text = t.text
        if i and _no_space_between(tokens[i - 1], t):
            parts[-1] = parts[-1] + text
        else:
            parts.append(text)
    return " ".join(parts)


def _no_space_between(prev: Token, cur: Token) -> bool:
    if cur.text in {".", ",", ")"}:
        return True
    if prev.text in {".", "("}:
        return True
    return False


# --- the allowlist prover ---------------------------------------------------


def prove(sql: str) -> Proof:
    """Classify ``sql`` as :data:`PROVEN`, :data:`COMBINED` or :data:`UNPROVEN`
    The verdict is closed: only the three proven families run;
    everything else -- including anything the prover cannot recognise -- is
    refused by the caller whenever a single-table-only rule applies."""
    text = sql if isinstance(sql, str) else ""
    tokens, uncertain = tokenize(text)
    if uncertain or not tokens or any(t.kind == "semicolon" for t in tokens):
        return Proof(UNPROVEN)
    token_verdict = _token_level_verdict(tokens)
    if token_verdict is not None:
        return token_verdict
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain:
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    # A CTE whose name shadows a rule-bearing base-table terminal is refused:
    # the guard cannot tell a reference to the CTE from one to the base table

    shadowed = _cte_shadowed_terminals(tokens, scans)
    if shadowed:
        return Proof(COMBINED, terminals=tuple(shadowed))
    reads = _build_reads(tokens, scans)
    # A PROVEN verdict must carry at least one read; a zero-read PROVEN would
    # apply no per-table rule. A statement that genuinely reads
    # nothing (``SELECT 1``, ``SHOW TABLES``, ``DESCRIBE t``) is UNPROVEN under
    # this per-relation contract -- the guard's caller allows a no-data read
    # separately (it refuses only when the statement reads a relation).
    if not reads:
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    if any(r.multi_scan for r in reads):
        # A reconstructed block still scanning two base tables is a shape the
        # earlier checks did not catch: refuse it rather than evaluate it.
        return Proof(COMBINED, terminals=tuple(dict.fromkeys(
            r.terminal for r in reads if r.multi_scan)))
    # Lineage: a rule must see the outer filters traced to the one read. The
    # single-table readers trace at most ONE wrapper level; a filtering clause
    # (WHERE / HAVING / QUALIFY) two or more levels above a physical read is not
    # attributed, so the statement is "cannot tell" (spec 4).
    if _lineage_untraced(tokens, scans):
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    return Proof(PROVEN, reads=tuple(reads))


def _token_level_verdict(tokens):
    """The non-PROVEN verdict a TOKEN-level check settles, else ``None`` (the
    statement passes the token-level allowlist and ``prove`` continues to the
    scan-level checks). Each rejected shape is UNPROVEN unless it is a
    resolvable combination (COMBINED). Covers, in order: WITH RECURSIVE, a set
    operator anywhere, a join / LATERAL / VALUES / UNNEST / table-function FROM,
    an unmodeled FROM source, an empty clause body, and a SELECT block with more
    than one FROM item (reviews R6-F1..R9-F1)."""
    if _has_recursive_with(tokens):            # a cycle the readers do not model
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    if any(_upper(t) in _SET_OPS for t in tokens):   # UNION/INTERSECT/EXCEPT anywhere
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    bad_from = _from_combination(tokens)       # join / LATERAL / VALUES / UNNEST / table fn
    if bad_from is not None:
        if bad_from == UNPROVEN:
            return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
        return Proof(COMBINED, terminals=tuple(_best_effort_terminals(tokens)))
    if _from_source_unmodeled(tokens):         # closed source grammar (R8-F2)
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    if _empty_clause_body(tokens):             # incomplete SQL (R9-F1)
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    if not _every_block_single_from(tokens):   # exactly one FROM item per block
        return Proof(UNPROVEN, terminals=_best_effort_terminals(tokens))
    return None


#: Clause introducers that MUST be followed by a clause body. A trailing one
#: (nothing after it, or only its own keyword tail) is incomplete SQL.
_CLAUSE_INTRODUCERS = {"WHERE", "HAVING", "QUALIFY", "ON", "GROUP", "ORDER"}


def _empty_clause_body(tokens) -> bool:
    """True when a clause introducer (:data:`_CLAUSE_INTRODUCERS`) has no clause
    body after it: the end of the statement, or only the ``BY`` of ``GROUP BY``
    / ``ORDER BY`` and then the end. Incomplete SQL is not a
    proven shape."""
    for i, t in enumerate(tokens):
        word = _upper(t)
        if word not in _CLAUSE_INTRODUCERS:
            continue
        j = i + 1
        if word in {"GROUP", "ORDER"} and j < len(tokens) and _upper(tokens[j]) == "BY":
            j += 1                           # step over the mandatory BY
        if j >= len(tokens):
            return True                      # nothing follows the introducer
    return False


def _has_recursive_with(tokens) -> bool:
    """True when a ``WITH RECURSIVE`` opens a CTE chain: a ``RECURSIVE`` word
    right after a ``WITH`` at the same depth."""
    for i, t in enumerate(tokens):
        if _upper(t) == "WITH":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and _upper(nxt) == "RECURSIVE":
                return True
    return False


def _has_clause_from(tokens) -> bool:
    """True when the statement has any clause ``FROM`` (it reads a relation)."""
    return any(_is_clause_from(tokens, i) for i in range(len(tokens)))


#: Keywords (and ``)``) that legitimately END a FROM source. Reaching one of
#: these right after a base name + optional alias means the source is a plain
#: single table; any OTHER token there is an unmodeled suffix.
_SOURCE_END = (_FROM_LIST_END | _JOIN_WORDS | _SET_OPS
               | {"WHERE", "GROUP", "HAVING", "QUALIFY", "WINDOW", "ORDER",
                  "LIMIT", "OFFSET", "ON", "USING", "SELECT"})


def _from_source_unmodeled(tokens) -> bool:
    """True when any clause-FROM's source is NOT one of the two closed forms
    a table name (bare / qualified / quoted) with an optional
    ``[AS] alias`` and nothing else, or a ``(SELECT`` / ``(WITH`` subquery with
    an optional alias. Anything else before the next clause keyword / ``)`` /
    end -- an alias column list, TABLESAMPLE, USING SAMPLE, an AT / FOR / ``@``
    version clause, a non-query ``(`` (nested parens, PIVOT / UNPIVOT), or any
    unrecognised token -- is unmodeled and refused."""
    for index, token in enumerate(tokens):
        if not _is_clause_from(tokens, index):
            continue
        if _source_span_unmodeled(tokens, index, token.depth):
            return True
    return False


def _source_span_unmodeled(tokens, from_index: int, depth: int) -> bool:
    """True when the single FROM source starting after ``from_index`` has an
    unmodeled suffix. Walks past the source (a ``(...)`` subquery or a dotted
    name) and its optional ``[AS] alias`` (one identifier), then requires the
    next token at ``depth`` to be a FROM-source end (:data:`_SOURCE_END`),
    ``,`` (another FROM item, handled elsewhere), ``)`` or end."""
    i = from_index + 1
    first = tokens[i] if i < len(tokens) else None
    if first is None:
        return False
    if first.text == "(" and first.depth == depth:
        inner = tokens[i + 1] if i + 1 < len(tokens) else None
        if inner is None or _upper(inner) not in {"SELECT", "WITH"}:
            return True                     # a non-query parenthesised source
        close = _matching_close(tokens, i)
        if close is None:
            return True
        i = close + 1                        # past the subquery
    else:
        i = _past_dotted_name(tokens, i, depth)
        if i is None:
            return True                      # not a readable name
    return _suffix_unmodeled(tokens, i, depth)


def _past_dotted_name(tokens, index: int, depth: int) -> int | None:
    """The index just past a dotted/quoted table name starting at ``index``
    (``a`` / ``a.b.c`` / ``"a"."b"``), or ``None`` when ``index`` is not a
    name token at ``depth``."""
    t = tokens[index] if index < len(tokens) else None
    if t is None or t.depth != depth or t.kind not in {"word", "identifier"}:
        return None
    i = index + 1
    while (i + 1 < len(tokens) and tokens[i].text == "."
           and tokens[i + 1].kind in {"word", "identifier"}):
        i += 2
    return i


def _suffix_unmodeled(tokens, i: int, depth: int) -> bool:
    """True when what follows the source (and its optional ``[AS] alias``) at
    ``depth`` is anything other than a FROM-source end, ``,``, ``)`` or end.
    Consumes one ``AS <id>`` or bare ``<id>`` alias first."""
    nxt = tokens[i] if i < len(tokens) else None
    if nxt is None:
        return False
    if nxt.depth != depth:
        # Deeper token (e.g. inside a following clause's parens) is not part of
        # the source; a shallower one ends the block. Either way, not unmodeled.
        return False
    if _upper(nxt) == "AS":
        alias = tokens[i + 1] if i + 1 < len(tokens) else None
        if alias is None or alias.kind not in {"word", "identifier"}:
            return True
        i += 2
    elif nxt.kind in {"word", "identifier"} and _upper(nxt) not in _SOURCE_END:
        i += 1                               # a bare alias
    end = tokens[i] if i < len(tokens) else None
    if end is None or end.text in {",", ")"} or end.depth < depth:
        return False
    if end.depth == depth and _upper(end) in _SOURCE_END:
        return False
    return True                              # an unmodeled trailing token


def _cte_shadowed_terminals(tokens, scans) -> list:
    """The base terminals a CTE name shadows: a CTE whose name equals a scanned
    base-table terminal, so a reference cannot be told from the base table
    Returns the shadowed terminal names, else ``[]``."""
    from .athena_scope import _cte_bindings
    bindings, uncertain = _cte_bindings(tokens)
    if uncertain or not bindings:
        return []
    cte_names = {str(b.name).lower() for b in bindings}
    out: list = []
    for scan in scans:
        term, _q = _terminal_of(tokens, scan)
        low = term.lower()
        if low in cte_names and low not in out:
            out.append(term)
    return out


def _lineage_untraced(tokens, scans) -> bool:
    """True when an outer WHERE / HAVING / QUALIFY clause cannot be attributed
    to a physical read because the read is more than one wrapper level below
    it. The single-table rule readers trace one CTE/derived level; a deeper
    chain with an outer filtering clause is not traced, so the prover cannot
    prove it (spec 4). A chain whose only outer clauses are
    non-filtering (a bare aggregate / GROUP BY / ORDER BY) is still proven: the
    aggregate over the whole single read is the same."""
    filter_depths = [t.depth for i, t in enumerate(tokens)
                     if _upper(t) in {"WHERE", "HAVING", "QUALIFY"}]
    if not filter_depths:
        return False
    for scan in scans:
        for wd in filter_depths:
            if scan.depth - wd >= 2:
                return True
    return False


def _build_reads(tokens, scans) -> list:
    """One :class:`Read` per physical scan, each a reconstructed single-table
    block with nested subqueries removed (``multi_scan`` when it still scans
    more than one base table)."""
    reads: list = []
    for scan in scans:
        end = _block_end(tokens, scan.select_index, scan.depth)
        block = _block_tokens_without_subqueries(tokens, scan.select_index, end, scan.depth)
        inner_scans, inner_uncertain = relations(block)
        terminal, quoted = _terminal_of(tokens, scan)
        multi = inner_uncertain or len(inner_scans) != 1
        reads.append(Read(_rebuild(block), terminal, quoted, multi))
    return reads


def _from_combination(tokens) -> str | None:
    """``None`` when no FROM clause holds a join/combination; else ``COMBINED``
    (a join/comma we can attribute) or ``UNPROVEN`` (LATERAL / VALUES / UNNEST
    / table function -- a source the rules do not model). Scans every FROM
    clause at every depth."""
    verdict: str | None = None
    for index, token in enumerate(tokens):
        if not _is_clause_from(tokens, index):
            continue
        from_depth = token.depth
        end = _from_clause_end(tokens, index, from_depth)
        # A JOIN keyword, a comma, or USING/NATURAL at the FROM's depth: a join.
        for at in range(index + 1, end):
            t = tokens[at]
            if t.depth != from_depth:
                continue
            if t.text == "," or _upper(t) in _JOIN_WORDS or _upper(t) == "USING":
                verdict = COMBINED if verdict != UNPROVEN else UNPROVEN
        # LATERAL / VALUES / UNNEST / a table function as the FROM source.
        if _from_source_is_unmodeled(tokens, index, end, from_depth):
            verdict = UNPROVEN
    return verdict


def _from_clause_end(tokens, from_index: int, depth: int) -> int:
    """The index that ends the FROM clause opened at ``from_index``: the first
    token at ``depth`` that begins another clause / set op, or a depth drop,
    else the end."""
    for i in range(from_index + 1, len(tokens)):
        t = tokens[i]
        if t.depth < depth:
            return i
        if t.depth == depth and _upper(t) in _FROM_LIST_END:
            return i
        if t.depth == depth and _upper(t) == "SELECT":
            return i
    return len(tokens)


def _from_source_is_unmodeled(tokens, from_index: int, end: int, depth: int) -> bool:
    """True when the FROM source is ``LATERAL`` / ``VALUES`` / ``UNNEST`` or a
    table function (a name directly followed by ``(``) at the FROM's depth."""
    i = from_index + 1
    # Skip a leading open paren of a derived table; a query-opening paren is a
    # derived table (allowed), a value-opening paren here would be VALUES.
    first = tokens[i] if i < len(tokens) else None
    if first is not None and _upper(first) in _FROM_SOURCE_FORMS:
        return True
    # A table function: FROM name ( ... ) at the FROM's depth (not a derived
    # table, whose body opens with SELECT/WITH/VALUES).
    if (first is not None and first.kind in {"word", "identifier"}
            and i + 1 < end and tokens[i + 1].text == "(" and tokens[i + 1].depth == depth):
        return True
    return False


def _every_block_single_from(tokens) -> bool:
    """True when every SELECT block at every depth has AT MOST one clause-FROM
    at the block's own depth. A block with no FROM (``SELECT 1``) is allowed;
    two FROM clauses at one block's depth is not (a shape the prover does not
    model)."""
    counts: dict = {}
    for index, token in enumerate(tokens):
        if _upper(token) != "SELECT":
            continue
        select_depth = token.depth
        end = _block_end(tokens, index, select_depth)
        froms = sum(1 for i in range(index + 1, end)
                    if tokens[i].depth == select_depth and _is_clause_from(tokens, i))
        if froms > 1:
            return False
        counts[index] = froms
    return True


def _is_clause_from(tokens, index: int) -> bool:
    from .athena_scope import _is_clause_from as _cf
    return _cf(tokens, index)


def _best_effort_terminals(tokens) -> tuple:
    """The rule-relevant base terminals reachable in ``tokens``, best-effort
    for a COMBINED/UNPROVEN statement. Combines two sources so recovery does
    not depend on the Athena scan list parsing the whole statement cleanly:

    * every physical scan :func:`relations` can read; and
    * every table-looking name that directly follows ``FROM``, ``JOIN`` or a
      FROM-list comma at that FROM's depth -- which recovers a comma-join's
      LATER input even when the scan list went uncertain.

    May still be empty when nothing is recoverable; the caller then refuses on
    the system having any single-table-only rule (an UNPROVEN verdict)."""
    out: list = []
    scans, _uncertain = relations(tokens)
    for scan in scans:
        term, _q = _terminal_of(tokens, scan)
        if term not in out:
            out.append(term)
    for term in _from_item_terminals(tokens):
        if term not in out:
            out.append(term)
    return tuple(out)


def _from_item_terminals(tokens) -> list:
    """Every terminal name that begins a FROM item: the token right after a
    ``FROM`` or ``JOIN`` keyword, or after a comma at a FROM's depth. Reads the
    dotted name's terminal. Comma-aware, so a comma-join's second base table is
    recovered even when the scan reader went uncertain."""
    out: list = []
    for index, token in enumerate(tokens):
        up = _upper(token)
        is_from = _is_clause_from(tokens, index)
        if up != "JOIN" and not is_from:
            continue
        depth = token.depth
        end = _from_clause_end(tokens, index, depth)
        _collect_from_list_terminals(tokens, index + 1, end, depth, out)
    return out


def _collect_from_list_terminals(tokens, start: int, end: int, depth: int, out: list) -> None:
    """Append the terminal of each comma-separated base-table name in the FROM
    list span ``[start, end)`` at ``depth`` to ``out``. A derived table (a
    ``(`` at ``depth``) contributes nothing here; its inner scans come from
    :func:`relations`."""
    expect_item = True
    i = start
    while i < end:
        t = tokens[i]
        if t.depth == depth and t.text == ",":
            expect_item = True
            i += 1
            continue
        if t.depth == depth and _upper(t) in ({"ON", "USING"} | _JOIN_WORDS):
            expect_item = False
        if expect_item and t.depth == depth and t.kind in {"word", "identifier"} \
                and _upper(t) not in _JOIN_WORDS and _upper(t) not in {"ON", "USING", "AS"}:
            term = _dotted_terminal(tokens, i)
            if term and term not in out:
                out.append(term)
            expect_item = False
        i += 1


def _dotted_terminal(tokens, index: int) -> str:
    """The last identifier of a possibly-dotted name starting at ``index``."""
    terminal = tokens[index]
    j = index + 1
    while (j + 1 < len(tokens) and tokens[j].text == "."
           and tokens[j + 1].kind in {"word", "identifier"}):
        terminal = tokens[j + 1]
        j += 2
    return str(terminal.value)


def single_table_reads(sql: str):
    """Yield one :class:`Read` per physical base-table scan in ``sql`` when the
    allowlist prover returns :data:`PROVEN`; otherwise return ONE synthetic
    combining read (``combines_relations`` True, ``multi_scan`` True) carrying
    the recovered rule-relevant terminals and an ``unproven`` flag, so the
    caller refuses the whole statement. ``None`` is reserved for nothing the
    prover can even classify as touching a read (an empty parse), which the
    caller treats as no reads."""
    proof = prove(sql)
    if proof.verdict == PROVEN:
        return list(proof.reads)
    # COMBINED or UNPROVEN: one synthetic read the caller refuses on, hard.
    terminals = proof.terminals
    first = terminals[0] if terminals else ""
    tokens, _unc = tokenize(sql if isinstance(sql, str) else "")
    reads_data = _has_clause_from(tokens)
    return [Read("", first, False, True, combines_relations=True,
                 combined_terminals=tuple(terminals), unproven=proof.verdict == UNPROVEN,
                 reads_data=reads_data)]
