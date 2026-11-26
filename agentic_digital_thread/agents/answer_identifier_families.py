# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Id-format families grounded in the element result, not the agent's prose.

Measured on an eval (trace G, golden n9): asked which id formats a serialized
equipment-list field holds, the system agent's own ``CROSS JOIN UNNEST`` query
returned every element id, including a ROUTING family, in four of five runs,
but its natural-language summary to the orchestrator dropped that family (it
hand-picked a few "id formats" and left the rest out), or ordered the raw ids
alphabetically with a ``LIMIT`` that cut them off before the family. The
IDENTIFIER FORMAT prompt (``system_agent._identifier_shape_prompt``) already
says to group by shape with no ``LIMIT``, so more prompt text is not the fix.
This carries the shapes the result HELD, the same engine-side, result-grounded
pattern as the figure check (``answer_figures``).

A value's shape is computed with :func:`normalize_shape`, the SAME ordered
substitutions the IDENTIFIER FORMAT prompt prescribes: one spec
(:data:`SHAPE_STEPS`) is read here through ``re`` and by the prompt renderer,
so the prompt example and the code cannot drift.

Delivered through ``system_agent._append_engine_facts`` (report-only and
annotate only; byte-identical under enforce):

1. **Trigger (structural).** A captured result whose SQL expands a serialized
   list with the engine's own expansion (``UNNEST`` / ``jsonb_array_elements``)
   and groups by an element id. The element binding is read FROM THE QUERY: the
   alias the expansion declares, then a SELECT output that extracts the id from
   that alias (``json_extract_scalar(<alias>, '$.id')``, ``<alias>->>'id'``,
   and the same through ``CAST(... AS VARCHAR)`` / ``json_format``), following
   one level of CTE or derived-table projection.
2. **Engine fact, no customer text in trusted output.** The trusted ``[engine]``
   line carries only COUNTS and a pointer to a fenced block; it never carries a
   shape or an example id. The shapes (customer-derived: letters survive
   normalisation) ride only inside the repo's untrusted fence
   (``untrusted_text.render_fenced``), each row a shape, its summed count, and
   an engine-written ``in answer`` / ``not in answer`` marker. A shape whose
   source value or rendered shape looks like a credential is dropped before
   fencing and only counted. No example id is printed or stored on the sidecar.
3. **Cut results.** A cut whose first sort key is a descending count keeps the
   most frequent ids (neutral sentence). A cut in key order (first key the id
   column) means ids past the cut were never returned: the line says so and a
   FINAL-ANSWER REQUIREMENTS block (naming NO shape) requires the answer to run
   the format-grouped count first. No SQL is rewritten and nothing is refused.
4. **Omission check.** A shape the answer does not mention is marked
   ``not in answer``. Mention is by typed segments, not substring: the answer
   contains the exact rendered shape, a held example value (in-process only),
   or an id-like notation token that matches the shape's segments under
   wildcard rules. A shape that is one literal word (``PUMP``) is never
   counted as mentioned, since prose, query keywords and quoted terms spell
   it the same way; it is always marked ``not in answer``.

Conservative: a query the parser cannot read, a result with no shaped id rows,
one whose id key is not derivable from the query, gets nothing. Stdlib only.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: The ordered shape substitutions, read by :func:`normalize_shape` (through
#: ``re``) and rendered into the IDENTIFIER FORMAT prompt's ``regexp_replace``
#: chain (``system_agent._identifier_shape_prompt``), so the two cannot drift:
#: each UUID to UUID, each run of 16+ hex characters holding a letter to HEX
#: (an all-digit number stays digits), then each digit to N.
SHAPE_STEPS: tuple[tuple[str, str], ...] = (
    (r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", "UUID"),
    (r"(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{16,}", "HEX"),
    (r"[0-9]", "N"),
)
_SHAPE_RES = tuple((re.compile(pattern), token) for pattern, token in SHAPE_STEPS)
#: The placeholder tokens a normalized shape can carry.
_PLACEHOLDERS = ("UUID", "HEX", "N")

LINE_PREFIX = "[engine] Id formats in the result: "
FENCE_HEADING = "ID FORMATS IN THIS CALL'S RESULTS (untrusted data, descriptive only):"
FENCE_SUBJECT = "the id formats this system's result held, with their row counts"
#: Words the orchestrator's engine-fact rule keys the omission requirement on.
MARKER = "not in answer"

SHAPE_FLOOR_FRACTION = Decimal("0.01")
SHAPE_FLOOR_ROWS = 2
MAX_SHAPES = 25
MAX_EXAMPLES = 4
MAX_ANSWER_TOKENS = 4_000
_MAX_ROWS = 50_000
_MAX_VALUE_CHARS = 128


def normalize_shape(value: str) -> str:
    """The normalized shape of one id value: the ordered :data:`SHAPE_STEPS`
    substitutions, every match replaced (Trino semantics). ``""`` for a
    non-string or one past :data:`_MAX_VALUE_CHARS`."""
    if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_CHARS:
        return ""
    for pattern, token in _SHAPE_RES:
        value = pattern.sub(token, value)
    return value


def normalize_segments(value: str) -> tuple:
    """``value`` as SOURCE-accurate typed segments: ``("lit", text)`` for a
    literal run and ``("ph", token)`` for a placeholder run (UUID, HEX, or a
    run of digits as ``N``). Segmenting during normalization keeps a literal
    ``N`` in the source (e.g. the ``N`` of ``WN5``) distinct from a digit
    placeholder, which ``normalize_shape`` alone cannot. Rendering the segments
    reproduces ``normalize_shape(value)`` exactly (pinned by test)."""
    if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_CHARS:
        return ()
    spans = []  # (start, end, token) placeholder spans, highest priority first
    taken = [False] * len(value)
    for pattern, token in _SHAPE_RES:
        for match in pattern.finditer(value):
            if not any(taken[match.start():match.end()]):
                spans.append((match.start(), match.end(), token))
                for i in range(match.start(), match.end()):
                    taken[i] = True
    spans.sort()
    segments, pos = [], 0
    for start, end, token in spans:
        if start > pos:
            segments.append(("lit", value[pos:start]))
        segments.append(("ph", token))
        pos = end
    if pos < len(value):
        segments.append(("lit", value[pos:]))
    return _coalesce(segments)


def _coalesce(segments) -> tuple:
    """Merge adjacent ``lit`` runs, and adjacent digit (``N``) placeholders into
    one ``N``-run token (so two digits render as ``NN`` like
    ``normalize_shape``); ``UUID``/``HEX`` stay their own segments."""
    out: list = []
    for kind, text in segments:
        if out and kind == "lit" and out[-1][0] == "lit":
            out[-1] = ("lit", out[-1][1] + text)
        elif (out and kind == "ph" and out[-1][0] == "ph"
              and set(text) <= {"N"} and set(out[-1][1]) <= {"N"}):
            out[-1] = ("ph", out[-1][1] + text)
        else:
            out.append((kind, text))
    return tuple(out)


def render_segments(segments) -> str:
    """The rendered shape of typed ``segments`` (``ph`` tokens as themselves,
    ``lit`` runs verbatim): equals ``normalize_shape`` of the source."""
    return "".join(text for _kind, text in segments)



def shape_sql_example(postgres: bool, *, re2: bool = False) -> str:
    """The ``regexp_replace`` chain the IDENTIFIER FORMAT prompt shows, built
    from :data:`SHAPE_STEPS` (the single source, so the prompt example and
    :func:`normalize_shape` cannot drift). Nested innermost-first = the step
    order; PostgreSQL needs the ``'g'`` flag to replace every match, Trino
    replaces every match by default. ``re2`` (DuckDB) has no lookahead, so it
    gets :func:`shape_sql_example_duckdb`."""
    if re2:
        return shape_sql_example_duckdb()
    flag = ", 'g'" if postgres else ""
    example = "col"
    for pattern, token in SHAPE_STEPS:
        example = f"regexp_replace({example}, '{pattern}', '{token}'{flag})"
    return example


def _hex_pattern_re2() -> str:
    """The HEX step of :data:`SHAPE_STEPS` without lookahead, for RE2 engines
    (DuckDB). A run of 16 or more hex characters holding a letter is one of:
    ``k`` digits (``k`` = 0..14), a letter, then at least ``15 - k`` more hex
    characters; or 15 or more digits, a letter, then any hex characters. Every
    branch starts at the same place and runs greedily to the end of the hex
    run, so it matches exactly what the lookahead form matches (pinned by a
    test that runs both over a corpus)."""
    hexc, letter = "[0-9a-fA-F]", "[a-fA-F]"
    branches = [f"{letter}{hexc}{{15,}}"]
    branches += [f"[0-9]{{{k}}}{letter}{hexc}{{{15 - k},}}" for k in range(1, 15)]
    branches.append(f"[0-9]{{15,}}{letter}{hexc}*")
    return "|".join(branches)


def shape_sql_example_duckdb() -> str:
    """:func:`shape_sql_example` for DuckDB: every step with the ``'g'`` flag,
    and the HEX step in its lookahead-free RE2 form."""
    example = "col"
    for pattern, placeholder in SHAPE_STEPS:
        if placeholder == "HEX":
            pattern = _hex_pattern_re2()
        example = f"regexp_replace({example}, '{pattern}', '{placeholder}', 'g')"
    return example


# --------------------------------------------------------------------------- #
# 1. Reading the element id binding from the query
# --------------------------------------------------------------------------- #
_EXPANSION_WORDS = ("unnest", "jsonb_array_elements")


def _norm_text(text: str) -> str | None:
    """A fragment normalized the way a SELECT output is, ``None`` when it does
    not tokenize with certainty."""
    from .athena_exact_shape import _norm, tokenize
    tokens, uncertain = tokenize(text)
    return None if uncertain or not tokens else _norm(tokens)


def _expansion_aliases(tokens) -> set:
    """The element column names every list expansion declares (lower case):
    ``UNNEST(...) [AS] [t](alias[, ...])`` / ``jsonb_array_elements(...) [AS]
    name``. Found at any depth, so a CTE or derived-table expansion counts."""
    from .athena_exact_shape import _matching_close
    names: set = set()
    for i, tok in enumerate(tokens):
        word = tok.value.lower() if tok.kind == "word" else ""
        if word not in _EXPANSION_WORDS or i + 1 >= len(tokens) or tokens[i + 1].text != "(":
            continue
        close = _matching_close(tokens, i + 1)
        if close is None:
            continue
        k = close + 1
        if k < len(tokens) and tokens[k].kind == "word" and tokens[k].value.upper() == "AS":
            k += 1
        if k < len(tokens) and tokens[k].kind in ("word", "identifier"):
            if k + 1 < len(tokens) and tokens[k + 1].text == "(":
                m = k + 2
                while m < len(tokens) and tokens[m].text != ")":
                    if tokens[m].kind in ("word", "identifier"):
                        names.add(tokens[m].value.lower())
                    m += 1
            else:
                names.add(tokens[k].value.lower())
    return names


def _id_forms(aliases) -> set:
    """The normalized id-extraction expressions for the element ``aliases``:
    the Athena and PostgreSQL forms, plain and through ``CAST(... AS VARCHAR)``
    / ``json_format``."""
    forms: set = set()
    for alias in aliases:
        for expr in (f"json_extract_scalar({alias}, '$.id')", f"{alias}->>'id'",
                     f"cast(json_extract_scalar({alias}, '$.id') as varchar)",
                     f"json_format(json_extract_scalar({alias}, '$.id'))",
                     f"cast({alias}->>'id' as varchar)"):
            norm = _norm_text(expr)
            if norm:
                forms.add(norm)
    return forms


def _select_outputs(text: str):
    """``[(normalized expression, aggregate func or None, alias lower or
    None)]`` for one SELECT's items, ``None`` when it does not read cleanly or
    holds a ``*``."""
    from .athena_exact_shape import _aggregate, _norm, _split_alias, _split_commas, _top_level, tokenize
    tokens, uncertain = tokenize(text)
    if uncertain:
        return None
    clauses = _top_level(tokens)
    if "FROM" not in clauses or tokens[:1] == [] or tokens[0].value.upper() != "SELECT":
        return None
    out = []
    for raw in _split_commas(tokens[1:clauses["FROM"]]):
        expr, alias = _split_alias(raw)
        if not expr or (len(expr) == 1 and expr[0].text == "*"):
            return None
        out.append((_norm(expr), _aggregate(expr), alias.lower() if alias else None))
    return out


def _cte_bodies(sql: str, tokens):
    """``({cte output alias: (expr, func)}, outer SQL text)`` for a leading
    ``WITH`` of simple ``name AS ( SELECT ... )`` parts, else ``({}, sql)``.
    One level only; the outer query's plain references resolve through it."""
    from .athena_exact_shape import _matching_close
    if not (tokens and tokens[0].kind == "word" and tokens[0].value.upper() == "WITH"):
        return {}, sql
    projections: dict = {}
    i = 1
    while i + 2 < len(tokens):
        if not (tokens[i].kind in ("word", "identifier")
                and tokens[i + 1].value.upper() == "AS" and tokens[i + 2].text == "("):
            return {}, sql
        close = _matching_close(tokens, i + 2)
        if close is None:
            return {}, sql
        body = _select_outputs(sql[tokens[i + 2].end:tokens[close].start])
        for expr, func, alias in body or ():
            if alias:
                projections[alias] = (expr, func)
        i = close + 1
        if i < len(tokens) and tokens[i].text == ",":
            i += 1
            continue
        return projections, sql[tokens[close].end:]
    return {}, sql


def _derived_table(sql: str, tokens):
    """``({subquery output alias: (expr, func)}, outer SQL text)`` for a
    top-level ``FROM ( <SELECT ...> ) [AS] alias``, else ``({}, sql)``. One
    level only; the outer query's plain references resolve through it. The
    outer SQL is the whole query (its SELECT list sits before the subquery)."""
    from .athena_exact_shape import _matching_close, _top_level
    clauses = _top_level(tokens)
    from_i = clauses.get("FROM")
    if from_i is None or from_i + 1 >= len(tokens) or tokens[from_i + 1].text != "(":
        return {}, sql
    close = _matching_close(tokens, from_i + 1)
    if close is None:
        return {}, sql
    projections: dict = {}
    for expr, func, alias in _select_outputs(sql[tokens[from_i + 1].end:tokens[close].start]) or ():
        if alias:
            projections[alias] = (expr, func)
    return projections, sql


def _projections_and_outer(sql: str, tokens):
    """``(projections, outer SQL)`` from a leading CTE or a top-level derived
    table (one level, CTE first); ``({}, sql)`` when neither applies."""
    projections, outer_sql = _cte_bodies(sql, tokens)
    if projections:
        return projections, outer_sql
    return _derived_table(sql, tokens)


def _resolve_outputs(outer, projections):
    """Outer outputs with each plain reference to a CTE projection replaced by
    that projection's ``(expr, func)``; other outputs kept as ``(expr, func)``."""
    resolved = []
    for expr, func, _alias in outer or ():
        if func is None and expr in projections:
            resolved.append(projections[expr])
        else:
            resolved.append((expr, func))
    return resolved


def _element_binding(sql: str, columns):
    """``(id index, count index)`` when the query expands a list and projects
    exactly one element-id extraction (a key) and one COUNT/SUM, matching the
    result columns, else ``None``."""
    if not any(word in sql.lower() for word in _EXPANSION_WORDS):
        return None
    from .athena_exact_shape import tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return None
    projections, outer_sql = _projections_and_outer(sql, tokens)
    outer = _select_outputs(outer_sql)
    if outer is None or len(outer) != len(columns):
        return None
    resolved = _resolve_outputs(outer, projections)
    forms = _id_forms(_expansion_aliases(tokens))
    ids = [i for i, (expr, func) in enumerate(resolved) if func is None and expr in forms]
    counts = [i for i, (_e, func) in enumerate(resolved) if func in ("count", "sum")]
    if len(ids) != 1 or len(counts) != 1:
        return None
    return ids[0], counts[0]


# --------------------------------------------------------------------------- #
# 2. The sort order of a cut result
# --------------------------------------------------------------------------- #
def _limit_of(tokens, clauses) -> int | None:
    if "LIMIT" not in clauses:
        return None
    after = tokens[clauses["LIMIT"] + 1:]
    if after and after[0].kind == "number":
        try:
            return int(after[0].value)
        except (ValueError, TypeError):
            return None
    return None


def _first_sort_key(tokens, clauses):
    """``(core tokens, descending)`` of the FIRST ORDER BY key, else ``None``."""
    if "ORDER" not in clauses:
        return None
    from .athena_exact_shape import _split_commas
    end = min((clauses[c] for c in ("LIMIT",) if c in clauses), default=len(tokens))
    items = _split_commas(tokens[clauses["ORDER"] + 2:end])
    if not items or not items[0]:
        return None
    body = items[0]
    descending = any(t.kind == "word" and t.value.upper() == "DESC" for t in body)
    core = [t for t in body if not (t.kind == "word" and t.value.upper() in ("ASC", "DESC"))]
    return (core, descending) if core else None


def _matches_column(core, index: int, outer, resolved) -> bool:
    """The ORDER BY key ``core`` names output ``index`` by ordinal, by its own
    (outer-scope) expression or alias, or by the CTE-resolved expression."""
    from .athena_exact_shape import _norm
    if len(core) == 1 and core[0].kind == "number":
        try:
            return int(core[0].value) - 1 == index
        except (ValueError, TypeError):
            return False
    ordered = _norm(core)
    candidates = {resolved[index][0]}
    if index < len(outer):
        candidates.add(outer[index][0])
        if outer[index][2]:
            candidates.add(outer[index][2])
    return ordered in candidates


def _order_kind(sql: str, columns, id_index: int, count_index: int) -> str:
    """``"count"`` when the first sort key is the count column descending,
    ``"key"`` when it is the id column (any direction), else ``"other"``."""
    from .athena_exact_shape import _top_level, tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return "other"
    projections, outer_sql = _projections_and_outer(sql, tokens)
    outer = _select_outputs(outer_sql) or []
    resolved = _resolve_outputs(outer, projections)
    otokens, _u = tokenize(outer_sql)
    first = _first_sort_key(otokens, _top_level(otokens))
    if first is None:
        return "other"
    core, descending = first
    if descending and _matches_column(core, count_index, outer, resolved):
        return "count"
    if _matches_column(core, id_index, outer, resolved):
        return "key"
    return "other"


# --------------------------------------------------------------------------- #
# 3. Typed shape segments and the per-shape record
# --------------------------------------------------------------------------- #
def _count_of(cell) -> Decimal | None:
    try:
        number = Decimal(str(cell).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return number


def _looks_secret(value: str) -> bool:
    """``value`` is credential-shaped, by the repo's value detector plus the
    token prefixes it does not carry (OpenAI ``sk-``, GitLab ``glpat-``,
    GitHub fine-grained ``github_pat_``)."""
    from .response_integrity_security import sensitive_text
    if sensitive_text(value):
        return True
    return bool(_EXTRA_SECRET_RE.search(value))


_EXTRA_SECRET_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}|\bglpat-[A-Za-z0-9_-]{16,}|\bgithub_pat_[A-Za-z0-9_]{20,})")


def families_record(payload, columns_hint=None) -> dict | None:
    """``{"shapes", "total", "cut", "order"}`` for one element result, else
    ``None``. ``shapes`` is ``[{"shape", "count", "segments", "examples"}]``
    above the floor, descending by count; ``examples`` stay in-process (never
    printed, never on the sidecar). ``order`` is ``count``/``key``/``other``.
    A shape whose source value or rendered shape is credential-shaped is
    dropped and counted as ``dropped``."""
    if not isinstance(payload, dict):
        return None
    columns, rows, sql = (payload.get("columns"), payload.get("rows"),
                          payload.get("_adt_executed_query"))
    if not (isinstance(columns, list) and isinstance(rows, list) and isinstance(sql, str)):
        return None
    if not rows or len(rows) > _MAX_ROWS or any(
            not isinstance(row, list) or len(row) != len(columns) for row in rows):
        return None
    binding = _element_binding(sql, columns)
    if binding is None:
        return None
    id_index, count_index = binding
    buckets, total, drops = _bucket_rows(rows, id_index, count_index)
    if buckets is None:
        return None
    floor = max(Decimal(SHAPE_FLOOR_ROWS), total * SHAPE_FLOOR_FRACTION)
    kept = _kept_shapes(buckets, floor)
    if not kept or total <= 0:
        return None
    return {"shapes": kept, "more": 0, "total": total, "dropped": drops.get("secret", 0),
            "cut": _is_cut(payload, rows, sql),
            "order": _order_kind(sql, columns, id_index, count_index)}


def _bucket_rows(rows, id_index, count_index):
    """``(buckets, total, drops)`` or ``(None, 0, {})``: per-shape count and
    bounded examples. ``drops`` counts values not listed as a format:
    ``secret`` (credential-shaped). An all-letter value is a valid id format
    and is listed like any other; injection safety is the fenced block's
    structural encoding, not a digit requirement."""
    buckets: dict = {}
    total = Decimal(0)
    drops = {"secret": 0}
    for row in rows:
        raw, count = str(row[id_index]).strip(), _count_of(row[count_index])
        if count is None:
            return None, 0, {}
        shape = normalize_shape(raw)
        if not shape:
            return None, 0, {}
        if _looks_secret(raw) or _looks_secret(shape):
            drops["secret"] += 1
            continue
        total += count
        bucket = buckets.setdefault(shape, {"count": Decimal(0), "examples": [],
                                            "segments": normalize_segments(raw)})
        bucket["count"] += count
        if len(bucket["examples"]) < MAX_EXAMPLES and raw not in bucket["examples"]:
            bucket["examples"].append(raw)
    return buckets, total, drops


def _kept_shapes(buckets, floor):
    kept = [{"shape": s, "count": b["count"], "segments": b["segments"],
             "examples": list(b["examples"])}
            for s, b in buckets.items() if b["count"] >= floor]
    kept.sort(key=lambda item: (-item["count"], item["shape"]))
    return kept[:MAX_SHAPES]


def _is_cut(payload, rows, sql) -> bool:
    if (payload.get("truncated") is True or payload.get("partial_reason")
            or payload.get("partial_note")):
        return True
    from .athena_exact_shape import _top_level, tokenize
    tokens, uncertain = tokenize(sql)
    if uncertain:
        return False
    _p, outer_sql = _projections_and_outer(sql, tokens)
    otokens, _u = tokenize(outer_sql)
    limit = _limit_of(otokens, _top_level(otokens))
    return limit is not None and len(rows) >= limit


# --------------------------------------------------------------------------- #
# 4. Which shapes the answer mentions
# --------------------------------------------------------------------------- #
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9_.*#?\\+-]+")
#: Inside an id-like notation token, one position of a digit placeholder: a
#: literal digit, a wildcard (``*`` any run), or a placeholder letter/symbol
#: (``N``/``X``/``#``/``?``) an answer uses for "a digit".
_PH_CLASS = r"(?:[0-9NXnx#?*]|\\d\+?)+"


def _shape_regex(segments) -> "re.Pattern | None":
    """A regex matching an id-like token of these SOURCE-accurate segments: a
    literal run matches itself (case-insensitively, including a literal ``N``),
    a placeholder run matches a digit/wildcard run of any width. Anchored."""
    parts = []
    for kind, text in segments or ():
        parts.append(re.escape(text) if kind == "lit" else _PH_CLASS)
    try:
        return re.compile("^" + "".join(parts) + "$", re.I) if parts else None
    except re.error:
        return None


#: Sentence punctuation to trim from a candidate token's EDGES before matching
#: (``BINDER_X_ABCN_NN.`` -> ``BINDER_X_ABCN_NN``); interior dots are kept, so a
#: dotted id like ``A.B12.C`` still matches its own shape.
_EDGE_PUNCT = ".,;:!?)('\"`[]{}"


def _strip_edges(token: str) -> str:
    return token.strip(_EDGE_PUNCT)


def _token_matches_shape(token: str, record_shape: dict, pattern) -> bool:
    """An answer token matches a shape: the exact rendered shape, a held
    example, or an id-like token whose placeholders line up with the shape's
    segments (``pattern``). Sentence-edge punctuation is trimmed first."""
    token = _strip_edges(token)
    if not token:
        return False
    low = token.lower()
    if low == record_shape["shape"].lower() or any(low == e.lower()
                                                   for e in record_shape["examples"]):
        return True
    if pattern is None or not re.search(r"[_.\-]", token):
        return False
    return pattern.match(token) is not None


#: A quoted or backticked run of letters, for an all-letter format's mention.
_QUOTED_WORD_RE = re.compile(r"[`'\"]([A-Za-z]+)[`'\"]")


def _literal_word(record_shape: dict) -> bool:
    """The shape is one literal word: letters only and no placeholder segment
    (``PUMP``), as opposed to a shape spelled in letters because its
    placeholders are letters (``UUID``, ``HEX``, ``NNNN``, ``ABCN``)."""
    segments = record_shape.get("segments") or ()
    return bool(segments) and all(kind == "lit" for kind, _text in segments)


def _alpha_mentioned(answer: str, record_shape: dict) -> bool:
    """A letter-only shape with no separator to tell it from a word.

    A literal word (:func:`_literal_word`) is never a mention: the same
    spelling is ordinary prose, a query keyword or a quoted term, so no form
    of it proves the answer named it as an id format, and it is always marked
    not in answer (naming a format the answer may cover is the safe mistake).
    A shape with a placeholder is mentioned when the answer quotes or
    backticks it (any case), or writes it bare in its own all-capitals
    spelling."""
    if _literal_word(record_shape):
        return False
    forms = {record_shape["shape"], *record_shape["examples"]}
    lowered = {f.lower() for f in forms}
    if any(m.group(1).lower() in lowered for m in _QUOTED_WORD_RE.finditer(answer)):
        return True
    caps = {f for f in forms if len(f) > 1 and f.isupper()}
    tokens = _ID_TOKEN_RE.findall(answer)[:MAX_ANSWER_TOKENS]
    return any(_strip_edges(tok) in caps for tok in tokens)


def mentions(answer: str, record_shape: dict) -> bool:
    """The answer mentions this shape (exact shape, held example, or an id-like
    token matching its segments; a letter-only shape by
    :func:`_alpha_mentioned`, which never counts a literal word).
    Bounded token scan, no substring search."""
    if not isinstance(answer, str):
        return False
    if record_shape["shape"].isalpha():
        return _alpha_mentioned(answer, record_shape)
    pattern = _shape_regex(record_shape.get("segments") or ())
    tokens = _ID_TOKEN_RE.findall(answer)[:MAX_ANSWER_TOKENS]
    return any(_token_matches_shape(tok, record_shape, pattern) for tok in tokens)


def omitted(answer, record: dict) -> list[dict]:
    """The record's shapes the answer does not mention, in record order."""
    return [s for s in record.get("shapes") or () if not mentions(answer, s)]


# --------------------------------------------------------------------------- #
# 5. The engine line, the fenced block and the requirement block
# --------------------------------------------------------------------------- #
def _fenced_block(record: dict, answer) -> str:
    """The shapes and counts inside the repo's untrusted fence, one JSON object
    per line so no record structure or marker comes from customer text; ``""``
    when no shape survives."""
    import json
    from .untrusted_text import render_fenced
    rows = []
    for shape in record["shapes"]:
        text = str(shape["shape"])[:_MAX_VALUE_CHARS]
        rows.append(json.dumps({"format": text, "rows": int(shape["count"]),
                                "in_answer": mentions(answer, shape)}, ensure_ascii=True))
    if not rows:
        return ""
    body = ("One JSON object per line; the engine wrote each line. 'format' is a normalized id "
            "shape, 'rows' its element-row count, 'in_answer' whether the answer mentions it.\n"
            + "\n".join(rows))
    return render_fenced(body, "engine:id-formats", heading=FENCE_HEADING, subject=FENCE_SUBJECT)


def _cut_sentence(record: dict) -> str:
    if not record.get("cut"):
        return ""
    if record.get("order") == "count":
        return " The result was cut and holds the most frequent ids only."
    if record.get("order") == "key":
        return (" The result was cut in key order, so id formats past the cut were never "
                "returned; run the shape-grouped count before naming which formats the field holds.")
    return " The result was cut, so rarer id formats may be missing."


def family_line(record: dict, answer) -> str:
    """The trusted engine line: counts and a pointer to the fenced block. No
    shape or example id. ``""`` when no shape survives."""
    if not record or not record.get("shapes"):
        return ""
    total_shapes = len(record["shapes"])
    missing = len(omitted(answer, record))
    dropped = int(record.get("dropped") or 0)
    withheld = (f" {dropped} credential-shaped id{'s' if dropped != 1 else ''} "
                f"{'were' if dropped != 1 else 'was'} withheld." if dropped else "")
    miss = (f" {missing} of them {'is' if missing == 1 else 'are'} {MARKER} (see the block)."
            if missing else " The answer mentions each of them.")
    return (f"{LINE_PREFIX}the engine grouped every returned element id of this result by its "
            f"normalized shape (each UUID to UUID, each run of 16+ hex characters with a letter "
            f"to HEX, each digit to N) and summed the row counts: {total_shapes} id "
            f"format{'s' if total_shapes != 1 else ''} in the result.{miss}{withheld}"
            f" The fenced block below lists them with their counts."
            f"{_cut_sentence(record)}")


def requirement_block(record: dict, answer) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for a key-ordered cut with an
    omission: it names no shape. ``""`` otherwise."""
    if not (record.get("cut") and record.get("order") == "key" and omitted(answer, record)):
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    note = ("REQUIRED: this system's result was cut in key order, so id formats past the cut "
            "were never returned. The answer must state that it does not list every format, cover "
            "every format the fenced id-formats block marks 'not in answer', and run the "
            "shape-grouped count (no LIMIT) before claiming which formats the field holds.")
    document = append_note(empty_document(), note, GuidanceScope.ANSWER_REQUIREMENT)
    return render_orchestrator_sections(document)["answer_requirements"]


def _record_of(statement) -> dict | None:
    """The families record a sidecar row carries, restored for rendering.
    ``count`` and ``total`` ride as text; examples and segments survive in
    process (never printed, never written to the provenance store)."""
    if not (isinstance(statement, dict) and isinstance(statement.get("families"), dict)):
        return None
    record = dict(statement["families"])
    shapes = record.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        return None
    try:
        record["total"] = Decimal(str(record.get("total", 0)))
        record["shapes"] = [{"shape": str(s["shape"]), "count": Decimal(str(s["count"])),
                             "segments": tuple(tuple(seg) for seg in s.get("segments") or ()),
                             "examples": list(s.get("examples") or ())} for s in shapes]
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return None
    return record


def engine_lines(statements, answer) -> list[str]:
    """This call's id-family output: for each admitted element result, the
    trusted line, its fenced block, and (for a key-ordered cut with an
    omission) a FINAL-ANSWER REQUIREMENTS block, in that order."""
    lines: list[str] = []
    for statement in statements or ():
        record = _record_of(statement)
        if not record:
            continue
        line = family_line(record, answer)
        if not line:
            continue
        lines.append(line)
        block = _fenced_block(record, answer)
        if block:
            lines.append(block)
        requirement = requirement_block(record, answer)
        if requirement:
            lines.append(requirement)
    return lines


def record_for_sidecar(record: dict | None) -> dict | None:
    """A families record made JSON-safe for the in-process sidecar row:
    ``Decimal`` counts to text; examples and segments kept (in-process only,
    never rendered, never written to the provenance store)."""
    if not record:
        return None
    return {**record, "total": format(record["total"], "f"),
            "shapes": [{"shape": s["shape"], "count": format(s["count"], "f"),
                        "segments": [list(seg) for seg in s["segments"]],
                        "examples": list(s["examples"])} for s in record["shapes"]]}


def neutralize(text):
    """``text`` with a copy of the line's prefix made inert, so a model-written
    or data-carried line never reads as the engine's."""
    if not isinstance(text, str) or LINE_PREFIX not in text:
        return text
    return text.replace(LINE_PREFIX, LINE_PREFIX[len("[engine] "):])
