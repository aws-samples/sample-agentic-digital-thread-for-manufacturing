# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Entity resolution engine and Digital Thread API routes."""

import logging, re, datetime
from fastapi import APIRouter, Depends
from .steward_gate import check_steward
from . import joinpath_rule_link as _jp_rule
from .rule_status import rule_active as _rule_active
from .db import pg, rows_to_fields, neptune_query, neptune_write, validate_identifier
from .offload import offload
from .query_router import query_system
from .catalog import get_schema, get_name, catalog, ENTITY_CONCEPT
from .models import ResolveRequest, LearnRequest
from . import identifier_translation as _translation
from .identifier_translation import (PATTERN_GRAMMAR_LITERAL, RESOLVER_PLACEHOLDER, WILDCARD,
                                     WILDCARD_REGEX, note_legacy_reading, pattern_body, reads_literally)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/thread", tags=["thread"])


def _query_for_system(sys_id: str, schema: str, sql: str, params=None) -> dict:
    """Query a system using the appropriate protocol. Postgres uses pg() directly, others use query_system()."""
    # Revised Task 2.3: identity-only read (connection_type) — routing metadata that
    # stays on the catalog identity registry; NOT a CONNECTION read
    # (no connection_string/connection_config/secret_arn), so it is not rerouted
    # through resolve_connection().
    entry = catalog.get(sys_id)
    conn_type = entry.get('connection_type', 'PostgreSQL') if entry else 'PostgreSQL'
    if conn_type == 'PostgreSQL':
        return pg(schema, sql, params)
    return query_system(sys_id, sql, params)


def apply_pattern(pattern: str, value: str, reverse: bool = False) -> str | None:
    """Apply an IdentityRule pattern transform.
    
    If reverse=False: extract {id} from value using source_pattern, apply to target_pattern.
    If reverse=True: extract {id} from value using target_pattern, apply to source_pattern.

    Unused by the app (kept for callers outside it); it reads ``pattern``
    with the one pattern compiler (``identifier_translation.pattern_body``),
    so its literal text is literal as in every other reader.
    """
    try:
        m = re.match(pattern_body(pattern, RESOLVER_PLACEHOLDER), value)
    except re.error:
        return None
    if m:
        return m.groupdict()
    return None


def source_pattern_regex(source_pattern: str) -> str | None:
    """The regular expression :func:`transform_id` matches a value against
    for ``source_pattern``, or None when the pattern names no ``{name}``
    placeholder (it transforms nothing). Built by the ONE pattern compiler
    the route seed's translation uses too
    (``identifier_translation.pattern_body``): each ``{name}`` captures
    ``.+`` here (``RESOLVER_PLACEHOLDER``; the route seed's is one word),
    ``{*}`` is one alphanumeric token, and every other character is literal.
    Pure; a placeholder named twice gives an expression that does not
    compile (:func:`source_pattern_compiles`)."""
    if not re.search(r'\{(\w+)\}', source_pattern or ""):
        return None
    return '^' + pattern_body(source_pattern, RESOLVER_PLACEHOLDER) + '$'


def _legacy_source_regex(source_pattern: str) -> str | None:
    """The expression :func:`transform_id` matched a value against before
    2026-10-21, for a stored row without the grammar marker: each ``{name}``
    is ``.+``, ``{*}`` one alphanumeric token, and the rest of the text is
    used as a regular expression, as written."""
    parts = re.findall(r'\{(\w+)\}', source_pattern or "")
    if not parts:
        return None
    regex = source_pattern
    for p in parts:
        regex = regex.replace('{' + p + '}', '(?P<' + p + '>.+)')
    return '^' + regex.replace(WILDCARD, WILDCARD_REGEX) + '$'


def source_pattern_compiles(source_pattern: str) -> bool:
    """False when the regular expression :func:`transform_id` builds for
    ``source_pattern`` does not compile (a malformed pattern, which then
    transforms nothing); True otherwise, a pattern with no placeholder
    included. Pure."""
    regex = source_pattern_regex(source_pattern)
    if regex is None:
        return True
    try:
        re.compile(regex)
    except re.error:
        return False
    return True


#: A pattern slot: a ``{name}`` placeholder or the ``{*}`` wildcard (ADR-0086).
PATTERN_SLOT = re.compile(r"\{(?:\w+|\*)\}")
_PLACEHOLDER_NAME = re.compile(r"\{(\w+)\}")


def _pattern_side_problem(side: str, pattern: str) -> str | None:
    """Why one side of a pattern pair cannot be read (in each direction each
    side is read as a source), or None. Literal text, punctuation included,
    is literal to both readers (``identifier_translation.pattern_body``)."""
    names = _PLACEHOLDER_NAME.findall(pattern)
    if len(names) != len(set(names)):
        return f"the {side} pattern names the same placeholder twice"
    if not source_pattern_compiles(pattern):
        return f"the {side} pattern does not compile as a pattern"
    return None


def identity_pattern_problem(source_pattern, target_pattern) -> str | None:
    """Why an IdentityRule's (source_pattern, target_pattern) pair is not
    valid to store, as a fixed sentence, or None when it is. ONE check for
    every writer of an IdentityRule, so both runtime readers (the resolver's
    :func:`transform_id`, which also tries the reverse direction, and the
    route seed's ``identifier_translation.apply``, which tries both) read
    every stored pair alike: both compile a pattern with the one compiler
    ``identifier_translation.pattern_body``, which reads literal text,
    punctuation included, as literal.

    Valid: no pattern (an exact-agreement rule); a direct match (the same
    text on both sides with no slot: the bare ``*`` or the values both
    columns share, which neither reader transforms); a transform whose two
    sides name the same ``{placeholder}`` set, each once, and that compiles.
    Pure."""
    src, tgt = str(source_pattern or "").strip(), str(target_pattern or "").strip()
    if (not src and not tgt) or (src == tgt and not PATTERN_SLOT.search(src)):
        return None
    if not _PLACEHOLDER_NAME.search(src):
        return "the source pattern names no {placeholder} and the two sides differ, so it transforms nothing"
    for side, pattern in (("source", src), ("target", tgt)):
        problem = _pattern_side_problem(side, pattern)
        if problem:
            return problem
    captured, named = set(_PLACEHOLDER_NAME.findall(src)), set(_PLACEHOLDER_NAME.findall(tgt))
    if named - captured:
        return "the target pattern names a placeholder the source pattern does not capture"
    if captured - named:
        return "the source pattern captures a placeholder the target pattern does not name"
    return None


def transform_id(source_pattern: str, target_pattern: str, value: str, *,
                 literal: bool = True) -> str | None:
    """Transform an identifier from source pattern to target pattern (SQL side).

    Example: source='X-{id}', target='PLANT-2026-0{id}', value='X-1234' -> 'PLANT-2026-01234'

    A ``{*}`` wildcard (ADR-0086) matches ONE alphanumeric token in the source
    and renders as SQL LIKE ``%`` in the result, consumed by
    ``_try_pattern_transform``'s existing LIKE branch and by the onboarding
    provision sample check. When the target carries ``{*}``, ``%``, ``_`` and
    ``\\`` in the captured values are escaped (PostgreSQL's default LIKE
    escape), so only the wildcard slot is a live LIKE metacharacter.

    The source pattern is read by :func:`source_pattern_regex`: its literal
    text is literal. A pattern whose expression does not compile transforms
    nothing (None), so a malformed stored pattern can never raise here.

    This is the resolver's reader: it transforms ONE whole stored value (a
    row's field, an identifier a person typed into the Thread panel), so a
    ``{name}`` matches any characters (``.+``) and case is the value's own.
    The route seed's reader (``identifier_translation.pattern_regex``) finds
    identifiers inside a question's free text instead: one word, any case.
    Same compiler, same literal text; the placeholder width and case differ
    by design (ADR-0086, amendment 2026-10-21). Neither hands the other a
    value it captured, and a rule is written only when the two read its
    values alike (:func:`reader_disagreement`).

    ``literal=False`` reads a stored row written before the grammar marker
    (``pattern_grammar``) with the old grammar: its literal text as a regular
    expression (:func:`_legacy_source_regex`). Only the stored-row readers
    (:func:`_rule_target`, :func:`_join_transform_value`) pass it, from the
    row's marker.
    """
    regex = source_pattern_regex(source_pattern) if literal else _legacy_source_regex(source_pattern)
    if regex is None:
        return None
    if not literal:
        note_legacy_reading(source_pattern)
    parts = re.findall(r'\{(\w+)\}', source_pattern)
    try:
        m = re.match(regex, value)
    except re.error:
        return None
    if not m:
        return None
    like = WILDCARD in target_pattern
    result = target_pattern
    for p in parts:
        result = result.replace('{' + p + '}', _like_escape(m.group(p)) if like else m.group(p))
    return result.replace(WILDCARD, '%')


def reader_disagreement(source_pattern: str, target_pattern: str, values) -> str | None:
    """The first of ``values`` the two runtime readers would read
    differently, or None. The route seed reads the value as a whole question
    (``identifier_translation.pattern_regex``, one word per placeholder, any
    case), the resolver as one stored value (:func:`transform_id`, any
    characters, exact case). They agree when neither translates it, or both
    do from the same captured core(s): both render the same target pattern
    from them. A source pattern the route seed never reads from (no literal
    text) has nothing to disagree on. Pure."""
    seed = _translation.pattern_regex(source_pattern)
    resolver = source_pattern_regex(source_pattern)
    if seed is None or resolver is None:
        return None
    for value in values or ():
        text = str(value)
        by_seed = seed.fullmatch(text)
        try:
            by_resolver = re.match(resolver, text)
        except re.error:
            return None
        if (by_seed.groupdict() if by_seed else None) != \
                (by_resolver.groupdict() if by_resolver else None):
            return text
    return None


def synthetic_identifier(pattern: str) -> str:
    """A value ``pattern`` describes: each ``{name}`` filled with a short
    alphanumeric token, each ``{*}`` with one letter. Pure."""
    return re.sub(r'\{\w+\}', 'A1', str(pattern or "").replace(WILDCARD, 'X'))


def _like_escape(value: str) -> str:
    """``value`` with LIKE metacharacters escaped by backslash (PostgreSQL default)."""
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def infer_pattern(input_val: str, matched_val: str) -> dict | None:
    """Infer a pattern transform between two identifier values.
    Returns {source_pattern, target_pattern, shared_id} or None.

    The shared core (``{id}``) is the longest run of DIGITS both values hold,
    never a separator at its edge: ``WO-9921`` / ``MFG-WO-9921`` give
    ``WO-{id}`` / ``MFG-WO-{id}``. A core that took the hyphen
    (``WO{id}``, core ``-9921``) was read by the resolver but could never be
    found by the route seed, whose core is one word
    (``identifier_translation.TRANSLATION_PLACEHOLDER``)."""
    def lcs(a, b):
        best = ''
        for i in range(len(a)):
            for j in range(i+1, len(a)+1):
                sub = a[i:j]
                if sub in b and len(sub) > len(best) and sub.isdigit():
                    best = sub
        return best

    shared = lcs(input_val, matched_val)
    if not shared or len(shared) < 2:
        return None

    src_pattern = input_val.replace(shared, '{id}')
    tgt_pattern = matched_val.replace(shared, '{id}')

    if src_pattern == tgt_pattern:
        return None

    return {'source_pattern': src_pattern, 'target_pattern': tgt_pattern, 'shared_id': shared}


def _fetch_identity_rules(concept: str) -> list:
    """Query Neptune for IdentityRules matching this concept."""
    rules = neptune_query(
        "MATCH (ir:IdentityRule)-[:LINKS_CONCEPT]->(c:Concept {id: $concept}) "
        f"WHERE {_rule_active('ir')} "
        "RETURN ir.id as id, ir.source_system as src_sys, ir.source_field as src_field, "
        "ir.source_pattern as src_pat, ir.target_system as tgt_sys, ir.target_field as tgt_field, "
        "ir.target_pattern as tgt_pat, ir.confidence as conf, ir.rule_type as rule_type, "
        "ir.pattern_grammar AS grammar",
        {"concept": concept}
    )
    return rules if rules else []


def _try_direct_match(rules, identifier, queried_systems, raw, resolution, unified):
    """Try to find the identifier directly in source systems. (Section 2a)"""
    for rule in rules:
        src_sys = rule.get("src_sys", "")
        src_field = rule.get("src_field", "")
        if src_sys in queried_systems or not src_field or "." in src_field:  # tag path: no SQL
            continue
        schema = get_schema(src_sys)
        if not schema:
            continue
        # Revised Task 2.3: identity-only read (connection_type) — routing metadata,
        # not a CONNECTION read; stays on the catalog identity registry.
        entry = catalog.get(src_sys)
        conn_type = entry.get('connection_type', 'PostgreSQL') if entry else 'PostgreSQL'
        if conn_type != 'PostgreSQL':
            # Non-Postgres: use query_system for direct field search
            validate_identifier(src_field)
            r = query_system(src_sys, f"SELECT * FROM {src_field} LIMIT 1")  # nosec B608 - src_field validated by validate_identifier(); query_system routes to non-Postgres adapters
            if r["rows"]:
                queried_systems.add(src_sys)
                sys_name = get_name(src_sys)
                raw.append({"system": sys_name, "fields": rows_to_fields(r)})
                resolution.append({"rule": f"Direct match in {sys_name}", "transform": f"{src_field} = {identifier}", "check": f"{src_sys} ✓"})
                row = dict(zip(r["columns"], r["rows"][0]))
                unified.append({"title": sys_name, "fields": [{"label": c, "value": row.get(c)} for c in r["columns"] if row.get(c) is not None][:8]})
            continue
        validate_identifier(schema)
        validate_identifier(src_field)
        schema_tables = pg(schema,
            "SELECT table_name FROM information_schema.columns WHERE column_name = %s AND table_schema = %s LIMIT 1",
            (src_field, schema))
        if not schema_tables["rows"]:
            continue
        tbl = schema_tables["rows"][0][0]
        validate_identifier(tbl)
        validate_identifier(src_field)
        r = pg(schema, f"SELECT * FROM {tbl} WHERE CAST({src_field} AS TEXT) = %s LIMIT 1", (identifier,))  # nosec B608 - tbl and src_field validated by validate_identifier(); identifier passed as parameter
        if r["rows"]:
            queried_systems.add(src_sys)
            sys_name = get_name(src_sys)
            raw.append({"system": sys_name, "fields": rows_to_fields(r)})
            resolution.append({
                "rule": f"Direct match in {sys_name}",
                "transform": f"{src_field} = {identifier}",
                "check": f"{schema}.{tbl}.{src_field} ✓"
            })
            row = dict(zip(r["columns"], r["rows"][0]))
            unified.append({"title": sys_name, "fields": [
                {"label": c, "value": row.get(c)} for c in r["columns"] if row.get(c) is not None
            ][:8]})


def _rule_target(rule: dict, identifier: str):
    """Apply one IdentityRule to ``identifier``. Returns ``(transformed, tgt_sys,
    tgt_field, src_pat, tgt_pat)``, or ``None`` when the rule applies in neither
    direction. A row without the grammar marker is read with the old grammar
    (:func:`transform_id`, ``literal``)."""
    literal = reads_literally(rule.get("grammar"))
    src_sys, tgt_sys = rule.get("src_sys", ""), rule.get("tgt_sys", "")
    src_pat, tgt_pat = rule.get("src_pat", ""), rule.get("tgt_pat", "")
    src_field, tgt_field = rule.get("src_field", ""), rule.get("tgt_field", "")
    if rule.get("rule_type", "pattern_transform") == "lookup" and src_pat and "=" in src_pat:
        lookup = dict(pair.split("=") for pair in src_pat.split(",") if "=" in pair)
        transformed = lookup.get(identifier)
        if not transformed:
            transformed = {v: k for k, v in lookup.items()}.get(identifier)
            if transformed:
                src_sys, tgt_sys = tgt_sys, src_sys
                src_field, tgt_field = tgt_field, src_field
        return (transformed, tgt_sys, tgt_field, src_pat, tgt_pat) if transformed else None
    transformed = transform_id(src_pat, tgt_pat, identifier, literal=literal) if src_pat and tgt_pat else None
    if transformed is None and src_pat and tgt_pat:
        transformed = transform_id(tgt_pat, src_pat, identifier, literal=literal)
        if transformed is not None:
            src_sys, tgt_sys = tgt_sys, src_sys
            src_field, tgt_field = tgt_field, src_field
            src_pat, tgt_pat = tgt_pat, src_pat
    if transformed is None:
        return None
    return transformed, tgt_sys, tgt_field, src_pat, tgt_pat


def _target_table(tgt_sys: str, schema: str, tgt_field: str) -> str:
    """The table holding ``tgt_field`` in ``tgt_sys``: graph first, then the
    database catalog; ``""`` when neither knows it."""
    tables = neptune_query(
        "MATCH (s:System {id: $tgt_sys})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field {name: $tgt_field}) "
        "RETURN se.name as table_name",
        {"tgt_sys": tgt_sys, "tgt_field": tgt_field}
    )
    if not tables:
        validate_identifier(schema)
        validate_identifier(tgt_field)
        schema_tables = pg(schema,
            "SELECT table_name FROM information_schema.columns WHERE column_name = %s AND table_schema = %s LIMIT 1",
            (tgt_field, schema))
        if schema_tables["rows"]:
            tables = [{"table_name": f"{schema}.{schema_tables['rows'][0][0]}"}]
    return tables[0].get("table_name", "") if tables else ""


def _target_row(schema: str, table_short: str, tgt_field: str, transformed: str) -> dict:
    """Exact, then LIKE, then substring match of ``transformed`` in the target column."""
    validate_identifier(table_short)
    validate_identifier(tgt_field)
    r = pg(schema, f"SELECT * FROM {table_short} WHERE {tgt_field} = %s LIMIT 1", (transformed,))  # nosec B608 - table_short and tgt_field validated by validate_identifier(); transformed passed as parameter
    if not r["rows"] and "%" in transformed:
        r = pg(schema, f"SELECT * FROM {table_short} WHERE {tgt_field} LIKE %s LIMIT 1", (transformed,))  # nosec B608 - table_short and tgt_field validated by validate_identifier()
    if not r["rows"]:
        r = pg(schema, f"SELECT * FROM {table_short} WHERE CAST({tgt_field} AS TEXT) LIKE %s LIMIT 1",  # nosec B608 - table_short and tgt_field validated by validate_identifier()
               (f"%{transformed.replace('%', '')}%",))
    return r


def _try_pattern_transform(rules, identifier, queried_systems, raw, resolution, unified,
                           concept: str = ""):
    """Apply pattern transforms to find identifier in target systems + follow JoinPaths. (Section 2b)"""
    for rule in rules:
        hit = _rule_target(rule, identifier)
        if hit is None:
            continue
        transformed, tgt_sys, tgt_field, src_pat, tgt_pat = hit
        schema = get_schema(tgt_sys)
        if not schema or tgt_sys in queried_systems:
            continue
        table_name = _target_table(tgt_sys, schema, tgt_field)
        if not table_name:
            continue
        table_short = table_name.split(".")[-1] if "." in table_name else table_name
        r = _target_row(schema, table_short, tgt_field, transformed)
        if not r["rows"]:
            continue
        sys_name, conf = get_name(tgt_sys), rule.get("conf", 0)
        queried_systems.add(tgt_sys)
        raw.append({"system": sys_name, "fields": rows_to_fields(r)})
        resolution.append({
            "rule": f"IdentityRule: {concept} ({int(conf * 100 if conf <= 1 else conf)}% confidence)",
            "transform": f"{identifier} → {src_pat}→{tgt_pat} → {transformed}",
            "check": f"{table_name}.{tgt_field} = {transformed} ✓"
        })
        row = dict(zip(r["columns"], r["rows"][0]))
        unified.append({"title": f"{sys_name}", "fields": [
            {"label": c, "value": row.get(c)} for c in r["columns"] if row.get(c) is not None
        ][:8]})
        _follow_joinpaths(tgt_sys, row, queried_systems, raw, resolution, unified)


def _join_transform_value(tgt_sys: str, join_sys: str, join_val: str) -> str:
    """Apply the first ACTIVE IdentityRule from ``tgt_sys`` to ``join_sys`` that
    transforms ``join_val``; the value unchanged when none does."""
    join_rules = neptune_query(
        "MATCH (ir:IdentityRule) WHERE ir.source_system = $src AND ir.target_system = $tgt "
        f"AND {_rule_active('ir')} "
        "RETURN ir.source_pattern AS src_pat, ir.target_pattern AS tgt_pat, "
        "ir.pattern_grammar AS grammar",
        {"src": tgt_sys, "tgt": join_sys}
    )
    for jr_rule in join_rules or []:
        t = transform_id(jr_rule.get('src_pat', ''), jr_rule.get('tgt_pat', ''), str(join_val),
                         literal=reads_literally(jr_rule.get('grammar')))
        if t:
            return t
    return join_val


def _join_row(join_schema: str, join_tbl: str, join_fld: str, join_val: str, raw_val: str) -> dict:
    """Exact, then LIKE, then zero-stripped substring match in the joined column."""
    validate_identifier(join_tbl)
    validate_identifier(join_fld)
    jr = pg(join_schema, f"SELECT * FROM {join_tbl} WHERE CAST({join_fld} AS TEXT) = %s LIMIT 1", (join_val,))  # nosec B608 - join_tbl and join_fld validated by validate_identifier()
    if not jr["rows"] and "%" in join_val:
        jr = pg(join_schema, f"SELECT * FROM {join_tbl} WHERE CAST({join_fld} AS TEXT) LIKE %s LIMIT 1", (join_val,))  # nosec B608 - join_tbl and join_fld validated by validate_identifier()
    if not jr["rows"]:
        stripped = raw_val.lstrip('0') or '0'
        jr = pg(join_schema, f"SELECT * FROM {join_tbl} WHERE CAST({join_fld} AS TEXT) LIKE %s LIMIT 1",  # nosec B608 - join_tbl and join_fld validated by validate_identifier()
                (f"%{stripped}%",))
    return jr


def _follow_joinpaths(tgt_sys: str, row: dict, queried_systems, raw, resolution, unified):
    """Follow the target system's ACTIVE JoinPaths from the matched row (a
    JoinPath whose IdentityRule is switched off is not a join)."""
    joins = neptune_query(
        "MATCH (jp:JoinPath) WHERE jp.from_system = $tgt_sys "
        "AND " + _jp_rule.rule_active("jp") + " "
        + _jp_rule.drop_if_rule_inactive() +
        "RETURN jp.to_system as to_sys, jp.to_table as to_tbl, jp.to_field as to_fld, "
        "jp.from_field as from_fld, jp.transform as xform, jp.description as descr",
        {"tgt_sys": tgt_sys}
    )
    for jp in joins:
        join_sys = jp.get("to_sys", "")
        join_schema = get_schema(join_sys)
        if not join_schema or join_sys in queried_systems:
            continue
        join_tbl = (jp.get("to_tbl") or "").split(".")[-1]
        join_fld, from_fld = jp.get("to_fld", ""), jp.get("from_fld", "")
        join_val = row.get(from_fld)
        if not join_val or not join_tbl:
            continue
        join_val = str(join_val)
        if jp.get("xform", ""):
            join_val = _join_transform_value(tgt_sys, join_sys, join_val)
        jr = _join_row(join_schema, join_tbl, join_fld, join_val, str(row.get(from_fld, "")))
        if not jr["rows"]:
            continue
        join_name = get_name(join_sys)
        queried_systems.add(join_sys)
        raw.append({"system": join_name, "fields": rows_to_fields(jr)})
        resolution.append({
            "rule": f"JoinPath: {jp.get('descr', f'{from_fld}→{join_fld}')}",
            "transform": f"{from_fld}={join_val} → {join_tbl}.{join_fld}",
            "check": f"{join_schema}.{join_tbl} ✓"
        })
        jrow = dict(zip(jr["columns"], jr["rows"][0]))
        unified.append({"title": join_name, "fields": [
            {"label": c, "value": jrow.get(c)} for c in jr["columns"] if jrow.get(c) is not None
        ][:8]})


def _known_pattern_prefixes() -> set[str]:
    """Literal prefixes of every ACTIVE IdentityRule pattern in the graph
    (``B-{id}`` → ``B-``). Learned from the graph, never from a built-in list;
    a switched-off rule (``rule_status``) no longer shapes the fuzzy lookup."""
    known_patterns = neptune_query(
        f"MATCH (ir:IdentityRule) WHERE {_rule_active('ir')} "
        "RETURN DISTINCT ir.source_pattern AS pat "
        f"UNION MATCH (ir:IdentityRule) WHERE {_rule_active('ir')} "
        "RETURN DISTINCT ir.target_pattern AS pat"
    ) or []
    prefixes = set()
    for p in known_patterns:
        pat = p.get('pat', '') or ''
        if '{' in pat and pat[:pat.index('{')]:
            prefixes.add(pat[:pat.index('{')])
    return prefixes


def _identifier_core(identifier: str, prefixes: set[str]) -> str:
    """The searchable core of an identifier for a fuzzy cross-system lookup:
    the value with a graph-known pattern prefix removed, else its longest
    digit run with leading zeros stripped (``PLANT-2026-01234`` → ``1234``,
    ``X-1234`` → ``1234``), else the identifier itself."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if identifier.upper().startswith(prefix.upper()):
            return identifier[len(prefix):]
    runs = [run.lstrip('0') for run in re.findall(r'\d{3,}', identifier)]
    runs = [run for run in runs if run]
    # Longest run wins; on a tie the TRAILING run (the serial part) does.
    return max(reversed(runs), key=len) if runs else identifier


def _fuzzy_lookup(schema: str, tbl: str, field_name: str, identifier: str, prefixes: set[str]) -> dict:
    """Exact match first, then the identifier core (LIKE), then the zero-padded
    numeric form some ERPs store. Returns the pg result (possibly empty)."""
    r = pg(schema, f"SELECT * FROM {tbl} WHERE CAST({field_name} AS TEXT) = %s LIMIT 1", (identifier,))  # nosec B608 - tbl and field_name validated by validate_identifier(); identifier passed as parameter
    if r["rows"]:
        return r
    stripped = _identifier_core(identifier, prefixes)
    if stripped != identifier:
        r = pg(schema, f"SELECT * FROM {tbl} WHERE CAST({field_name} AS TEXT) LIKE %s LIMIT 1",  # nosec B608 - tbl and field_name validated by validate_identifier()
               (f"%{stripped}%",))
    if not r["rows"] and stripped.isdigit():
        padded = stripped.zfill(18)
        r = pg(schema,
            f"SELECT * FROM {tbl} WHERE CAST({field_name} AS TEXT) LIKE %s OR CAST({field_name} AS TEXT) = %s LIMIT 1",  # nosec B608 - tbl and field_name validated by validate_identifier()
            (f"%{stripped}", padded))
    return r


def _learn_proposal(concept: str, identifier: str, matched_val: str,
                    source_sys: str, target_sys: str, target_field: str) -> dict | None:
    """A 'Learn this pattern?' proposal for the Threads panel, or None.

    The source field is the source system's identifier-role field for this
    concept, read from the graph — never assumed. No such field, no proposal."""
    inferred = infer_pattern(identifier, matched_val)
    if not inferred or source_sys == target_sys:
        return None
    rows = neptune_query(
        "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) "
        "WHERE f.system_id = $sid RETURN f.name AS field_name, r.role AS role",
        {"concept": concept, "sid": source_sys}) or []
    rows.sort(key=lambda row: 0 if row.get("role") == "identifier" else 1)
    source_field = (rows[0].get("field_name") or "").split(".")[-1] if rows else ""
    if not source_field:
        return None
    return {
        'source_pattern': inferred['source_pattern'], 'target_pattern': inferred['target_pattern'],
        'source_system': source_sys, 'source_field': source_field,
        'target_system': target_sys, 'target_field': target_field,
        'concept': concept, 'confidence': 0.7,
        # The values the pattern was inferred from: the Thread panel sends
        # them back with the learn request, which checks them.
        'example_identifier': identifier, 'example_matched_value': matched_val,
    }


def _brute_force_non_pg(concept, identifier, sys_id, sys_name, field_name,
                        queried_systems, raw, resolution, unified):
    """Non-Postgres system: exact lookup through the query router."""
    validate_identifier(field_name)
    r = query_system(sys_id, f"SELECT * FROM {field_name} WHERE {field_name} = %s LIMIT 1", (identifier,))  # nosec B608 - field_name validated by validate_identifier(); identifier passed as parameter
    if not r["rows"]:
        return
    queried_systems.add(sys_id)
    raw.append({"system": sys_name, "fields": rows_to_fields(r)})
    resolution.append({"rule": f"Concept search: {concept} in {sys_name} (query_system)", "transform": f"{field_name} = {identifier}", "check": f"{sys_id} ✓"})
    row = dict(zip(r["columns"], r["rows"][0]))
    unified.append({"title": sys_name, "fields": [{"label": c, "value": row.get(c)} for c in r["columns"] if row.get(c) is not None][:8]})


def _record_fuzzy_hit(concept, identifier, r, sys_id, sys_name, schema, tbl, field_name,
                      queried_systems, raw, resolution, unified):
    """Record a brute-force hit and, when the two values suggest a pattern,
    attach a learn proposal (source field read from the graph)."""
    queried_systems.add(sys_id)
    raw.append({"system": sys_name, "fields": rows_to_fields(r)})
    resolution.append({
        "rule": f"Concept search: {concept} in {sys_name} (no IdentityRule — brute-force)",
        "transform": f"{field_name} LIKE '%{identifier}%'",
        "check": f"{schema}.{tbl}.{field_name} ✓ (fuzzy match)"
    })
    matched_val = str(r['rows'][0][r['columns'].index(field_name)]) if field_name in r['columns'] else str(r['rows'][0][0])
    source_sys = list(queried_systems - {sys_id})[0] if len(queried_systems) > 1 else sys_id
    proposal = _learn_proposal(concept, identifier, matched_val, source_sys, sys_id, field_name)
    if proposal:
        resolution[-1]['proposal'] = proposal
    row = dict(zip(r["columns"], r["rows"][0]))
    unified.append({"title": sys_name, "fields": [
        {"label": c, "value": row.get(c)} for c in r["columns"] if row.get(c) is not None
    ][:8]})


def _try_brute_force(concept, identifier, queried_systems, raw, resolution, unified):
    """Fallback: query ALL onboarded systems with fields mapped to this concept. (Section 3)"""
    concept_fields = neptune_query(
        "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) WHERE r.role = 'identifier' "
        "RETURN f.system_id AS sys_id, f.name AS field_name, f.schema_id AS schema_entry",
        {"concept": concept}
    )
    if not concept_fields:
        concept_fields = neptune_query(
            "MATCH (f:Field)-[:MAPS_TO]->(c:Concept {id: $concept}) "
            "RETURN f.system_id AS sys_id, f.name AS field_name, f.schema_id AS schema_entry",
            {"concept": concept}
        )
    prefixes = None
    for cf in concept_fields:
        sys_id = cf.get("sys_id", "")
        field_name = cf.get("field_name", "").split(".")[-1]
        if sys_id in queried_systems or not field_name:
            continue
        schema = get_schema(sys_id)
        sys_name = get_name(sys_id)
        if not schema:
            continue
        # Revised Task 2.3: identity-only read (connection_type) — routing metadata,
        # not a CONNECTION read; stays on the catalog identity registry.
        entry = catalog.get(sys_id)
        conn_type = entry.get('connection_type', 'PostgreSQL') if entry else 'PostgreSQL'
        if conn_type != 'PostgreSQL':
            _brute_force_non_pg(concept, identifier, sys_id, sys_name, field_name,
                                queried_systems, raw, resolution, unified)
            continue
        validate_identifier(schema)
        validate_identifier(field_name)
        tbl_result = pg(schema,
            "SELECT table_name FROM information_schema.columns WHERE table_schema = %s AND column_name = %s LIMIT 1",
            (schema, field_name))
        if not tbl_result["rows"]:
            continue
        tbl = tbl_result["rows"][0][0]
        validate_identifier(tbl)
        if prefixes is None:
            prefixes = _known_pattern_prefixes()
        r = _fuzzy_lookup(schema, tbl, field_name, identifier, prefixes)
        if r["rows"]:
            _record_fuzzy_hit(concept, identifier, r, sys_id, sys_name, schema, tbl, field_name,
                              queried_systems, raw, resolution, unified)


def _try_entity_anchored(concept, identifier, queried_systems, raw, resolution, unified):
    """Entity-anchored fallback (ADR-0040, D4). When no ``role='identifier'`` field
    matched this concept, locate systems whose ``SchemaEntry-[:MAPS_TO]->Concept`` entity
    edge equals this concept and search their ``SchemaEntry-[:IDENTIFIED_BY]->Field``
    identity-key fields. This links a novel system on the ENTITY match even when its
    identity field carries no field-concept identifier mapping. (Section 3b)"""
    entity_fields = neptune_query(
        "MATCH (se:SchemaEntry)-[:MAPS_TO]->(c:Concept {id: $concept}) "
        "MATCH (se)-[:IDENTIFIED_BY]->(f:Field) "
        "RETURN f.system_id AS sys_id, f.name AS field_name, se.name AS table_name",
        {"concept": concept}
    )
    for cf in entity_fields or []:
        sys_id = cf.get("sys_id", "")
        field_name = (cf.get("field_name", "") or "").split(".")[-1]
        table_name = (cf.get("table_name", "") or "").split(".")[-1]
        if sys_id in queried_systems or not field_name or not table_name:
            continue
        schema = get_schema(sys_id)
        sys_name = get_name(sys_id)
        if not schema:
            continue
        # Revised Task 2.3: identity-only read (connection_type) — routing metadata,
        # not a CONNECTION read; stays on the catalog identity registry.
        entry = catalog.get(sys_id)
        conn_type = entry.get('connection_type', 'PostgreSQL') if entry else 'PostgreSQL'
        if conn_type != 'PostgreSQL':
            validate_identifier(table_name)
            validate_identifier(field_name)
            r = query_system(sys_id, f"SELECT * FROM {table_name} WHERE {field_name} = %s LIMIT 1", (identifier,))  # nosec B608 - table_name and field_name validated by validate_identifier(); identifier passed as parameter
        else:
            validate_identifier(schema)
            validate_identifier(table_name)
            validate_identifier(field_name)
            r = pg(schema, f"SELECT * FROM {table_name} WHERE CAST({field_name} AS TEXT) = %s LIMIT 1", (identifier,))  # nosec B608 - table_name and field_name validated by validate_identifier(); identifier passed as parameter
        if r["rows"]:
            queried_systems.add(sys_id)
            raw.append({"system": sys_name, "fields": rows_to_fields(r)})
            resolution.append({
                "rule": f"Entity match: {concept} in {sys_name} (identity key — no IdentityRule)",
                "transform": f"{field_name} = {identifier}",
                "check": f"{schema}.{table_name}.{field_name} ✓"
            })
            row = dict(zip(r["columns"], r["rows"][0]))
            unified.append({"title": sys_name, "fields": [
                {"label": c, "value": row.get(c)} for c in r["columns"] if row.get(c) is not None
            ][:8]})


def resolve_entity(concept: str, identifier: str) -> tuple[list, list, list, list, list]:
    """Resolve an entity across systems using Neptune IdentityRules."""
    raw, resolution, unified = [], [], []
    queried_systems = set()

    rules = _fetch_identity_rules(concept)
    _try_direct_match(rules, identifier, queried_systems, raw, resolution, unified)
    _try_pattern_transform(rules, identifier, queried_systems, raw, resolution, unified,
                           concept=concept)
    _try_brute_force(concept, identifier, queried_systems, raw, resolution, unified)
    # ADR-0040 (D4): entity-edge fallback — only meaningful when the field-concept passes
    # above found no identifier field for a system that nonetheless shares this entity.
    _try_entity_anchored(concept, identifier, queried_systems, raw, resolution, unified)

    correlations = neptune_query(
        "MATCH (c:Correlation) WHERE c.concept_a = $concept OR c.concept_b = $concept "
        "RETURN c.id AS id, c.pattern AS pattern, c.confidence AS confidence",
        {"concept": concept}
    )
    boundaries = neptune_query(
        "MATCH (d:DecisionBoundary) WHERE d.concept = $concept "
        "RETURN d.id AS id, d.rule AS rule, d.threshold AS threshold, d.action AS action",
        {"concept": concept}
    )

    return raw, resolution, unified, correlations or [], boundaries or []


def thread_resolve(body: ResolveRequest):
    """Resolve one entity across every system. Sync body (graph and source
    queries); the route offloads it."""
    entity_type = body.entity_type.lower()
    identifier = body.identifier.strip()

    concept = ENTITY_CONCEPT.get(entity_type)
    if not concept:
        return {"error": f"Unknown entity_type '{entity_type}'. Use: {', '.join(ENTITY_CONCEPT)}"}

    raw, resolution, unified, correlations, boundaries = resolve_entity(concept, identifier)
    for section in (raw, unified):
        for item in section:
            item["fields"] = [f for f in item["fields"] if f.get("value") is not None]

    return {"entity_type": entity_type, "identifier": identifier, "raw": raw, "resolution": resolution, "unified": unified, "correlations": correlations, "boundaries": boundaries}


@router.post("/resolve")
async def thread_resolve_route(body: ResolveRequest):
    """Resolve one entity across every system. Sync body: ``thread_resolve``."""
    return await offload(thread_resolve, body)


def _validate_learn_target(tgt_sys: str, tgt_field: str) -> "dict | None":
    """The taught rule's target must be a provisioned System whose schema (when
    SQL) really has the target column. Returns an error dict or None."""
    systems = neptune_query("MATCH (s:System {id: $tgt_sys}) RETURN s.id", {"tgt_sys": tgt_sys})
    if not systems:
        return {'error': f"target_system '{tgt_sys}' not found in Neptune"}
    tgt_schema = get_schema(tgt_sys)
    if tgt_schema:
        validate_identifier(tgt_schema)
        col_check = pg(tgt_schema,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND column_name = %s LIMIT 1",
            (tgt_schema, tgt_field.split('.')[-1]))
        if not col_check['rows']:
            return {'error': f"target_field '{tgt_field}' not found in {tgt_schema}"}
    return None


def _learn_reader_problem(src_pattern: str, tgt_pattern: str, identifier: str = "",
                          matched_value: str = "") -> str | None:
    """Why the two runtime readers would read a taught pair differently, or
    None (:func:`reader_disagreement`): on the identifier the rule was learned
    from (read through the source pattern) and the value it matched (read
    through the target pattern, the direction a reader takes for it), when
    the request carries them, and on a value the source pattern describes
    (:func:`synthetic_identifier`), which alone cannot show a case or
    placeholder-width difference."""
    checks = [(src_pattern, tgt_pattern, [v for v in (identifier, synthetic_identifier(src_pattern)) if v])]
    if matched_value:
        checks.append((tgt_pattern, src_pattern, [matched_value]))
    for source, target, values in checks:
        odd = reader_disagreement(source, target, values)
        if odd:
            return f"the two identifier readers would read value '{odd}' differently"
    return None


#: The method of a rule taught through /api/thread/learn WITHOUT the
#: identifier it was learned from and the value it matched (a direct API
#: call; the Thread panel always sends both). Its reader check ran only on a
#: value built from the pattern, so it is not stamped ``user_confirmed``
#: (tier 0): it ranks as an unconfirmed rule with recorded provenance (tier 1
#: in ``orchestrator._provenance_tier``, ``identifier_translation._tier`` and
#: the route seed's ORDER BY), and re-provision keeps it like a confirmed one
#: (``onboard._ONBOARDING_RULE_WHERE``).
USER_UNVERIFIED_METHOD = "user_unverified"
#: What a taught rule's origin_context says when that check could not run.
_UNVERIFIED_LEARN_ORIGIN = ("Taught through /api/thread/learn without the identifier it was "
                            "learned from and the value it matched, so the two identifier "
                            "readers were compared only on a value built from the pattern")


def _learn_identity_rule(body: LearnRequest) -> dict:
    """``thread_learn`` identity_rule branch: validate the pattern pair (the
    one check every IdentityRule writer uses, :func:`identity_pattern_problem`)
    and the target, CREATE the steward-stamped IdentityRule and link its
    Concept. A request that carries both examples is stamped
    ``user_confirmed``; one without them :data:`USER_UNVERIFIED_METHOD`."""
    concept, src_pattern, tgt_pattern = body.concept, body.source_pattern, body.target_pattern
    tgt_sys, tgt_field = body.target_system, body.target_field
    identifier = str(body.example_identifier or "").strip()
    matched_value = str(body.example_matched_value or "").strip()

    problem = identity_pattern_problem(src_pattern, tgt_pattern) or _learn_reader_problem(
        src_pattern, tgt_pattern, identifier, matched_value)
    if problem:
        return {'error': f"Identity rule not created: {problem}."}
    err = _validate_learn_target(tgt_sys, tgt_field)
    if err:
        return err

    checked = bool(identifier and matched_value)
    rule_id = _create_learned_rule(body, checked)
    if not rule_id:
        return {'error': 'Failed to write rule to Neptune'}

    neptune_write(
        "MERGE (c:Concept {id: $concept}) SET c.name = $concept",
        {"concept": concept}
    )
    neptune_write(
        "MATCH (ir:IdentityRule {id: $id}), (c:Concept {id: $concept}) MERGE (ir)-[:LINKS_CONCEPT]->(c)",
        {"id": rule_id, "concept": concept}
    )

    answer = {'status': 'created', 'rule_id': rule_id, 'message': f'Identity rule created: {src_pattern} ↔ {tgt_pattern}'}
    if not checked:
        answer['unverified'] = (f"{_UNVERIFIED_LEARN_ORIGIN}; written as {USER_UNVERIFIED_METHOD}, "
                                "not user_confirmed. Send example_identifier and "
                                "example_matched_value to have it confirmed.")
    return answer


def _create_learned_rule(body: LearnRequest, checked: bool) -> str:
    """CREATE the IdentityRule ``body`` teaches; its id, or ``""`` when the
    write failed. A rule taught through this route is a HUMAN assertion.
    Stamp it with the steward provenance the readers rank on (route seed R3
    tiering: ``method IN ('user_confirmed', 'crosswalk_confirmed')`` is tier
    0 and is trimmed LAST under prompt budget). Without the stamp the rule
    ranked as an anonymous tier-2 row and, in a graph with ~180
    onboarding-LLM rules, never reached the orchestrator (workshop test
    event 13, 2026-09-14). Tier 0 needs ``checked`` (both examples): only
    they let the reader check expose a case or placeholder-width
    difference; otherwise :data:`USER_UNVERIFIED_METHOD` and an
    origin_context saying why."""
    now = datetime.datetime.utcnow()
    rule_id = f"ir-learned-{body.concept}-{now.strftime('%Y%m%d%H%M%S')}"
    origin = "" if checked else "origin_context: $octx, "
    ok = neptune_write(
        "CREATE (ir:IdentityRule {id: $id, concept: $concept, "
        "source_system: $src_sys, source_field: $src_field, source_pattern: $src_pat, "
        "target_system: $tgt_sys, target_field: $tgt_field, target_pattern: $tgt_pat, "
        "confidence: $confidence, learned: true, created_by: 'user_feedback', pattern_grammar: $grammar, "
        "method: $method, provenance: 'human-asserted', " + origin + "created_at: $created_at})",
        {"id": rule_id, "concept": body.concept, "src_sys": body.source_system,
         "src_field": body.source_field, "src_pat": body.source_pattern,
         "tgt_sys": body.target_system, "tgt_field": body.target_field,
         "tgt_pat": body.target_pattern, "confidence": body.confidence,
         "grammar": PATTERN_GRAMMAR_LITERAL,
         "method": "user_confirmed" if checked else USER_UNVERIFIED_METHOD,
         **({} if checked else {"octx": _UNVERIFIED_LEARN_ORIGIN}),
         "created_at": now.isoformat()}
    )
    return rule_id if ok else ""


def thread_learn(body: LearnRequest):
    if body.type == 'correlation':
        corr_id = f"corr-{body.concept_a}-{body.concept_b}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        neptune_write(
            "CREATE (c:Correlation {id: $id, pattern: $pattern, confidence: $conf, "
            "discovered: $disc, validated_count: 1, source_metric: $src_m, target_metric: $tgt_m, "
            "concept_a: $ca, concept_b: $cb, plant_id: $plant})",
            {"id": corr_id, "pattern": body.pattern, "conf": body.confidence,
             "disc": datetime.datetime.utcnow().isoformat(), "src_m": body.source_metric,
             "tgt_m": body.target_metric, "ca": body.concept_a, "cb": body.concept_b,
             "plant": body.plant_id}
        )
        if body.system_a:
            neptune_write("MATCH (c:Correlation {id: $id}), (s:System {id: $sid}) CREATE (c)-[:SOURCE_SYSTEM]->(s)",
                          {"id": corr_id, "sid": body.system_a})
        if body.system_b:
            neptune_write("MATCH (c:Correlation {id: $id}), (s:System {id: $sid}) CREATE (c)-[:TARGET_SYSTEM]->(s)",
                          {"id": corr_id, "sid": body.system_b})
        return {'status': 'created', 'type': 'correlation', 'id': corr_id}

    if body.type == 'decision_boundary':
        db_id = f"db-{body.concept}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        neptune_write(
            "CREATE (d:DecisionBoundary {id: $id, rule: $rule, threshold: $thresh, "
            "action: $action, applies_to: $applies, concept: $concept, plant_id: $plant})",
            {"id": db_id, "rule": body.pattern, "thresh": body.threshold,
             "action": body.action, "applies": body.applies_to or body.concept,
             "concept": body.concept, "plant": body.plant_id}
        )
        neptune_write(
            "MATCH (d:DecisionBoundary {id: $id}), (c:Concept {id: $cid}) MERGE (d)-[:GOVERNS]->(c)",
            {"id": db_id, "cid": body.concept}
        )
        return {'status': 'created', 'type': 'decision_boundary', 'id': db_id}

    # default: identity_rule
    return _learn_identity_rule(body)


@router.post('/learn', dependencies=[Depends(check_steward)])
async def thread_learn_route(body: LearnRequest):
    """thread_learn: offloaded (EVENT-LOOP OFFLOAD PATTERN). Sync body: ``thread_learn``.
    Steward-gated: every branch writes a steward-grade rule straight into the
    shared graph (an IdentityRule stamped ``user_confirmed`` when the request
    carries both examples, else ``user_unverified``; a Correlation; or a
    DecisionBoundary), so anyone else gets 403 before any graph I/O."""
    return await offload(thread_learn, body=body)


def thread_correlations(concept: str = '', plant: str = ''):
    """Get learned correlations, optionally filtered by concept or plant."""
    if concept:
        results = neptune_query(
            "MATCH (c:Correlation) WHERE c.concept_a = $concept OR c.concept_b = $concept RETURN c",
            {"concept": concept}
        )
    elif plant:
        results = neptune_query(
            "MATCH (c:Correlation) WHERE c.plant_id = $plant RETURN c",
            {"plant": plant}
        )
    else:
        results = neptune_query("MATCH (c:Correlation) RETURN c")
    return {"correlations": results}


@router.get('/correlations')
async def thread_correlations_route(concept: str = '', plant: str = ''):
    """Get learned correlations, optionally filtered by concept or plant. Sync body: ``thread_correlations``."""
    return await offload(thread_correlations, concept=concept, plant=plant)


def thread_boundaries(concept: str = '', plant: str = ''):
    """Get decision boundaries, optionally filtered by concept or plant."""
    if concept:
        results = neptune_query(
            "MATCH (d:DecisionBoundary) WHERE d.concept = $concept RETURN d",
            {"concept": concept}
        )
    elif plant:
        results = neptune_query(
            "MATCH (d:DecisionBoundary) WHERE d.plant_id = $plant RETURN d",
            {"plant": plant}
        )
    else:
        results = neptune_query("MATCH (d:DecisionBoundary) RETURN d")
    return {"boundaries": results}


@router.get('/boundaries')
async def thread_boundaries_route(concept: str = '', plant: str = ''):
    """Get decision boundaries, optionally filtered by concept or plant. Sync body: ``thread_boundaries``."""
    return await offload(thread_boundaries, concept=concept, plant=plant)


#: Entity-picker concepts: picker key -> graph Concept id whose ``identifier``
#: fields are sampled from onboarded systems.
_THREAD_ENTITY_CONCEPTS = {
    "batch": "batch",
    "equipment": "equipment-id",
    "product": "product",
    "order": "work-order",
}
_THREAD_ENTITY_SAMPLE_LIMIT = 20


def _sample_identifier_values(sys_id: str, field_name: str, table_name: str) -> list[str]:
    """Distinct identifier values for one graph-mapped field on an ONBOARDED
    catalog system — identity-only catalog reads (status, connection_type,
    schema), never connection material. Returns ``[]`` for a system that is
    unknown or not onboarded."""
    entry = catalog.get(sys_id)
    if not entry or entry.get("status") != "onboarded":
        return []
    validate_identifier(field_name)
    validate_identifier(table_name)
    sql = (f"SELECT DISTINCT CAST({field_name} AS TEXT) AS val FROM {table_name} "
           f"WHERE {field_name} IS NOT NULL")  # nosec B608 - identifiers validated above
    if entry.get("connection_type", "PostgreSQL") == "PostgreSQL":
        result = pg(entry.get("schema", ""), f"{sql} ORDER BY 1 DESC LIMIT {_THREAD_ENTITY_SAMPLE_LIMIT}")  # nosec B608
    else:
        result = query_system(sys_id, f"{sql} LIMIT {_THREAD_ENTITY_SAMPLE_LIMIT}")  # nosec B608 - routes to non-Postgres adapters
    return [str(row[0]) for row in result.get("rows", []) if row and row[0] is not None and str(row[0])]


def thread_entities():
    """Sample entities for Digital Thread exploration, drawn ONLY from graph
    ``identifier`` mappings on onboarded systems.

    Generic by construction (ADR-0065, 2026-09-18): an engine whose graph has
    no identifier mappings yet answers with a STRUCTURED EMPTY result (every
    picker key present, empty lists) — there is no fixed-schema fallback, so
    nothing here presumes what the seed created."""
    entities: dict[str, list[dict]] = {key: [] for key in _THREAD_ENTITY_CONCEPTS}
    for entity_type, concept_id in _THREAD_ENTITY_CONCEPTS.items():
        fields = neptune_query(
            "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept {id: $cid}) WHERE r.role = 'identifier' "
            "RETURN f.system_id AS sys_id, f.name AS field, f.schema_id AS schema_entry",
            {"cid": concept_id}
        )
        seen: set[str] = set()
        for finfo in fields[:3]:  # Limit to 3 systems per concept
            sys_id = finfo.get("sys_id", "")
            field_name = finfo.get("field", "")
            schema_entry = finfo.get("schema_entry", "")
            table_name = schema_entry.split(".")[-1] if "." in schema_entry else ""
            if not sys_id or not field_name or not table_name:
                continue
            for val in _sample_identifier_values(sys_id, field_name, table_name):
                if val not in seen:
                    seen.add(val)
                    entities[entity_type].append({"id": val, "label": val})
            if len(entities[entity_type]) >= _THREAD_ENTITY_SAMPLE_LIMIT:
                break
    return entities


@router.get("/entities")
async def thread_entities_route():
    """Return sample entities available for Digital Thread exploration, dynamically from onboarded systems. Sync body: ``thread_entities``."""
    return await offload(thread_entities)
