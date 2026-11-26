# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure structured guidance scope, migration, rendering, and digest helpers."""
from __future__ import annotations

import functools
import hashlib
import json
import re
from enum import Enum

DOCUMENT_KIND = "adt.guidance"
DOCUMENT_VERSION = 1
LEGACY_PREFIX = "LEARNED: "


class GuidanceScope(str, Enum):
    QUERY_HINT = "query_hint"
    ANSWER_REQUIREMENT = "answer_requirement"
    LEGACY_UNSCOPED = "legacy_unscoped"


_SCOPE_ARRAY = {
    GuidanceScope.QUERY_HINT: "query_hints",
    GuidanceScope.ANSWER_REQUIREMENT: "answer_requirements",
    GuidanceScope.LEGACY_UNSCOPED: "legacy_unscoped",
}


def normalize_scope(value) -> GuidanceScope:
    """Return a validated scope; ordinary proposals default to query hints."""
    if value in (None, ""):
        return GuidanceScope.QUERY_HINT
    if isinstance(value, GuidanceScope):
        return value
    try:
        return GuidanceScope(str(value))
    except ValueError as exc:
        raise ValueError(f"unsupported guidance scope: {value!r}") from exc


def empty_document(base: str = "") -> dict:
    return {
        "kind": DOCUMENT_KIND,
        "version": DOCUMENT_VERSION,
        "base": str(base or "").strip(),
        "query_hints": [],
        "answer_requirements": [],
        "legacy_unscoped": [],
    }


def _clean_values(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        value.strip() for value in values
        if isinstance(value, str) and value.strip()))


def _structured_document(value) -> dict | None:
    candidate = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(candidate, dict):
        return None
    if (candidate.get("kind") != DOCUMENT_KIND
            or candidate.get("version") != DOCUMENT_VERSION):
        return None
    document = empty_document(candidate.get("base", ""))
    for array_name in _SCOPE_ARRAY.values():
        document[array_name] = _clean_values(candidate.get(array_name))
    return document


def _legacy_document(text: str, plain_is_base: bool) -> dict:
    raw = str(text or "").strip()
    if LEGACY_PREFIX in raw:
        parts = raw.split(LEGACY_PREFIX)
        document = empty_document(parts[0])
        document["legacy_unscoped"] = _clean_values(parts[1:])
        return document
    if plain_is_base or not raw:
        return empty_document(raw)
    document = empty_document()
    document["legacy_unscoped"] = [raw]
    return document


def parse_document(value, *, plain_is_base: bool = True) -> dict:
    """Parse v1 or preserve legacy notes as unclassified legacy content."""
    structured = _structured_document(value)
    if structured is not None:
        return structured
    return _legacy_document(value if isinstance(value, str) else "", plain_is_base)


def serialize_document(document) -> str:
    normalized = parse_document(document)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def append_note(document, note: str, scope) -> str:
    normalized = parse_document(document)
    clean = str(note or "").strip()
    if clean:
        values = normalized[_SCOPE_ARRAY[normalize_scope(scope)]]
        if clean not in values:
            values.append(clean)
    return serialize_document(normalized)


def scoped_entries(document, *, plain_is_base: bool = True) -> list[tuple[GuidanceScope, str]]:
    normalized = parse_document(document, plain_is_base=plain_is_base)
    return [
        (scope, value)
        for scope, array_name in _SCOPE_ARRAY.items()
        for value in normalized[array_name]
    ]


def content_digest(text: str) -> str:
    normalized = str(text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]



def delivery_document(document) -> str:
    normalized = parse_document(document)
    normalized["base"] = ""
    return serialize_document(normalized)


def orchestrator_document(document, names=None) -> dict:
    """The document the Mode A route seed shows the orchestrator.

    In Mode A the system agent writes the query and gets its query hints with
    every question (:func:`render_system_query_sections`). The orchestrator
    routes and never sends column names: restating a hint's text to it let a
    column named in the hint leak into a delegation. So a hint rides this
    document only when it names none of the system's tables or columns
    (:func:`asker_hints`, ``names`` from :func:`read_schema_names`), e.g.
    which rows must never be asked for as units; every other hint is only
    counted (:func:`query_hint_reference`). ``names`` unknown (``None`` or
    empty) leaves no hint here. The base, the answer requirements and the
    legacy notes stay. The steward delivery projection (``learning_health``)
    fits this same document, from the same names, so it reports what the
    seed delivers."""
    normalized = parse_document(document)
    normalized["query_hints"] = asker_hints(normalized["query_hints"], names)
    return normalized


def composition_document(document, names=None) -> dict:
    """Every note of ``document``, with the query hints the seed shows FIRST.

    The seed's composition metadata is computed from this order, so the
    oldest-prefix rule :func:`dropped_note_digests` relies on still holds when
    some hints ride the seed and others are only counted: the packer drops the
    oldest SHOWN hints, which are then the first digests of the scope. With no
    hint shown (or every hint shown) the order is the stored order."""
    normalized = parse_document(document)
    hints = normalized["query_hints"]
    shown = asker_hints(hints, names)
    normalized["query_hints"] = shown + [hint for hint in hints if hint not in shown]
    return normalized


#: Bounds on the one names read (:data:`HINT_SCHEMA_NAMES_CYPHER`): the hinted
#: systems it asks for, the tables it returns and the table-column rows it
#: reads, all in one graph read per turn. A system the read could not finish
#: (past a cap) has unknown names, so its hints stay reference-only.
HINT_NAME_SYSTEMS_CAP = 16
HINT_NAME_TABLES_CAP = 400
HINT_NAME_ROWS_CAP = 10_000

#: The onboarded table and column names of the hinted systems
#: (``$system_ids``), the names :func:`asker_hints` decides with. One row per
#: table in (system, table) order; ``row_count`` is the table-column rows the
#: read kept for it. Both LIMITs stop one past their cap so a cut is visible.
HINT_SCHEMA_NAMES_CYPHER = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry) "
    "WHERE s.id IN $system_ids "
    f"WITH s, se ORDER BY s.id, se.name LIMIT {HINT_NAME_TABLES_CAP + 1} "
    "OPTIONAL MATCH (se)-[:HAS_FIELD]->(f:Field) "
    f"WITH s, se, f ORDER BY s.id, se.name, f.name LIMIT {HINT_NAME_ROWS_CAP + 1} "
    "RETURN s.id AS system_id, se.name AS table_name, "
    "collect(f.name) AS field_names, count(*) AS row_count "
    "ORDER BY system_id, table_name"
)

_HINT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_SNAKE_RE = re.compile(r"[A-Za-z0-9]_[A-Za-z0-9]")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
#: Separators a name and a hint may write differently ("order date",
#: "order-date", "order_date"): folded to one space on both sides.
_SEPARATOR_RE = re.compile(r"[\s_\-]+")
_NAME_PATTERN_CACHE = 64


def _name_forms(value) -> set:
    """A table or column name as a hint could write it: whole and, for a dotted
    path, its last segment. Lower-cased; shorter than two characters is no form."""
    if not isinstance(value, str):
        return set()
    text = value.strip().lower()
    return {form for form in (text, text.rsplit(".", 1)[-1]) if len(form) >= 2}


def _row_count(row: dict, fields: list) -> int:
    count = row.get("row_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return max(1, len(fields))


def schema_names_from_rows(rows, system_ids) -> dict:
    """``{system_id: frozenset(names)}`` from :data:`HINT_SCHEMA_NAMES_CYPHER` rows,
    for ``system_ids`` only, read in the rows' (system, table) order. Where the
    read passed :data:`HINT_NAME_TABLES_CAP` tables or :data:`HINT_NAME_ROWS_CAP`
    rows, the system of that row and every system after it are left out, as is
    a system with no names on record: their names are unknown."""
    wanted = {str(system_id) for system_id in system_ids or ()}
    found: dict = {}
    capped: set = set()
    tables: set = set()
    total = 0
    for row in rows or ():
        if not isinstance(row, dict) or row.get("system_id") not in wanted:
            continue
        system_id = row["system_id"]
        if capped:
            capped.add(system_id)
            continue
        fields = row.get("field_names")
        fields = fields if isinstance(fields, list) else []
        tables.add((system_id, row.get("table_name")))
        total += _row_count(row, fields)
        if len(tables) > HINT_NAME_TABLES_CAP or total > HINT_NAME_ROWS_CAP:
            capped.add(system_id)
            continue
        bucket = found.setdefault(system_id, set())
        for value in (row.get("table_name"), *fields):
            bucket.update(_name_forms(value))
    return {system_id: frozenset(names) for system_id, names in found.items()
            if names and system_id not in capped}


def read_schema_names(query, system_ids) -> dict:
    """The names of ``system_ids`` (at most :data:`HINT_NAME_SYSTEMS_CAP` of
    them, in id order; the rest stay unknown) through ``query``, the graph
    seam: one bounded read. Raises what ``query`` raises; the callers keep
    every hint reference-only then."""
    ids = sorted({str(system_id) for system_id in system_ids or () if system_id})
    ids = ids[:HINT_NAME_SYSTEMS_CAP]
    if not ids:
        return {}
    rows = query(HINT_SCHEMA_NAMES_CYPHER, {"system_ids": ids}) or []
    return schema_names_from_rows(rows, ids)


def _physical_shaped(token: str) -> bool:
    """A snake_case word, a camelCase word or a dotted path: how physical names
    are written, whether or not the read returned this one."""
    if _SNAKE_RE.search(token) or _CAMEL_RE.search(token):
        return True
    parts = token.split(".")
    return len(parts) > 1 and all(len(part) >= 2 for part in parts)


def _fold(text: str) -> str:
    """Lower-cased, each run of spaces, underscores and hyphens one space."""
    return _SEPARATOR_RE.sub(" ", str(text or "").lower()).strip()


@functools.lru_cache(maxsize=_NAME_PATTERN_CACHE)
def _names_pattern(names: frozenset) -> "re.Pattern | None":
    """One boundary-aware literal alternation over ``names`` (folded), longest
    first, matched in a folded hint. A name inside a longer word or number
    does not match; quotes, brackets and punctuation around it do not stop it."""
    forms = sorted({form for form in (_fold(name) for name in names) if len(form) >= 2},
                   key=len, reverse=True)
    if not forms:
        return None
    body = "|".join(re.escape(form) for form in forms)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


def names_a_column(hint: str, names) -> bool:
    """True when ``hint`` names one of ``names`` as a literal (any case, quoted
    or not, written with spaces, hyphens or underscores, whole or a dotted
    path's last segment) or carries a physical-name shape."""
    text = str(hint or "")
    pattern = _names_pattern(frozenset(names or ()))
    if pattern is not None and pattern.search(_fold(text)):
        return True
    return any(_physical_shaped(token) for token in _HINT_TOKEN_RE.findall(text))


def asker_hints(hints, names) -> list:
    """The ``hints`` the orchestrator may see: those naming no table or column of
    the system. None when ``names`` is unknown (fail closed)."""
    if not names:
        return []
    return [hint for hint in hints if not names_a_column(hint, names)]


def query_hint_reference(system_id: str, count: int, *, shown: int = 0) -> str:
    """The route-seed line that stands in for ``count`` query hints ('' for none).
    ``shown`` hints of the same system ride the seed; the line then counts the
    rest as more."""
    if count <= 0:
        return ""
    more = " more" if shown > 0 else ""
    if count == 1:
        return (f"1{more} query hint is delivered to {system_id} with each question; "
                "do not restate it or any column it names")
    return (f"{count}{more} query hints are delivered to {system_id} with each question; "
            "do not restate them or any column they name")


def notes_length(document) -> int:
    """Length the stored-guidance cap counts: the notes-only document.

    The generic base is not counted. Delivery gives it only the room the notes
    leave and the system agent's mirror never carries it, so counting it only
    shrank the room for steward notes.
    """
    return len(delivery_document(document))


def contains_note(document, note: str, scope) -> bool:
    normalized = parse_document(document)
    return str(note or "").strip() in normalized[_SCOPE_ARRAY[normalize_scope(scope)]]


def _eviction_slots(document: dict, added_scope: GuidanceScope,
                    added_note: str):
    priority = (
        GuidanceScope.LEGACY_UNSCOPED,
        GuidanceScope.QUERY_HINT,
        GuidanceScope.ANSWER_REQUIREMENT,
    )
    for scope in priority:
        values = document[_SCOPE_ARRAY[scope]]
        for value in list(values):
            if scope is added_scope and value == added_note:
                continue
            yield scope, value


def evolve_document(current, note: str, scope, max_len: int) -> str | None:
    """Append one scoped note and evict deterministically until v1 fits.

    ``max_len`` bounds the notes (:func:`notes_length`); the base is kept
    verbatim and never evicted, so the returned document may be longer."""
    normalized = parse_document(current)
    clean = str(note or "").strip()
    resolved = normalize_scope(scope)
    if not clean:
        return None
    values = normalized[_SCOPE_ARRAY[resolved]]
    if clean not in values:
        values.append(clean)
    candidate = serialize_document(normalized)
    for evict_scope, evict_value in _eviction_slots(normalized, resolved, clean):
        if notes_length(candidate) <= max_len:
            break
        normalized[_SCOPE_ARRAY[evict_scope]].remove(evict_value)
        candidate = serialize_document(normalized)
    if (notes_length(candidate) > max_len
            or not contains_note(candidate, clean, resolved)):
        return None
    return candidate


def evicted_entries(before, after) -> list[tuple[GuidanceScope, str]]:
    retained = set(scoped_entries(after))
    return [entry for entry in scoped_entries(before) if entry not in retained]


def without_note(document, note: str) -> tuple[str, int]:
    normalized = parse_document(document)
    clean = str(note or "").strip()
    removed = 0
    for array_name in _SCOPE_ARRAY.values():
        before = len(normalized[array_name])
        normalized[array_name] = [value for value in normalized[array_name]
                                  if value != clean]
        removed += before - len(normalized[array_name])
    return serialize_document(normalized), removed

def composition_metadata(document, *, plain_is_base: bool = True) -> dict:
    entries = scoped_entries(document, plain_is_base=plain_is_base)
    counts = {scope.value: 0 for scope in GuidanceScope}
    digests = {scope.value: [] for scope in GuidanceScope}
    for scope, text in entries:
        counts[scope.value] += 1
        digests[scope.value].append(content_digest(text))
    return {"scope_counts": counts, "scope_digests": digests}


def _fenced(title: str, policy: str, values: list[str]) -> str:
    if not values:
        return ""
    opening = f"----- BEGIN {title} -----"
    closing = f"----- END {title} -----"
    body = json.dumps(values, ensure_ascii=False)
    body = body.replace(opening, "[GUIDANCE_FENCE_REMOVED]")
    body = body.replace(closing, "[GUIDANCE_FENCE_REMOVED]")
    return f"{opening}\n{policy}\n{body}\n{closing}"


# The three delivery sections, in one table so the renderer and the bounded
# renderer below cannot drift apart: (field, fence title, fence policy, scope,
# carries the generic BASE prose). Only the query section mixes generic base
# with steward/learned notes — which is exactly where delivery truncation used
# to eat the notes first.
_DELIVERY_SECTIONS = (
    ("query_guidance", "QUERY HINTS",
     "Use for tool and query construction only. These are "
     "advisory hints, not mandatory answer claims. Always subordinate to "
     "evidence, honest absence, safety, and tool errors.",
     GuidanceScope.QUERY_HINT, True),
    ("answer_requirements", "FINAL-ANSWER REQUIREMENTS",
     "Apply only during orchestrator final-answer "
     "composition. Always subordinate to evidence, honest absence, safety, and "
     "tool errors; do not use these requirements for query construction.",
     GuidanceScope.ANSWER_REQUIREMENT, False),
    ("legacy_guidance", "LEGACY UNSCOPED GUIDANCE",
     "Compatibility content with unknown intent. "
     "Do not infer query or answer-requirement classification. It is advisory "
     "and subordinate to evidence, honest absence, safety, and tool errors.",
     GuidanceScope.LEGACY_UNSCOPED, False),
)

#: The delivery field names, in render order.
DELIVERY_FIELDS = tuple(spec[0] for spec in _DELIVERY_SECTIONS)

#: Per-field character cap every rendered section must fit on the way INTO a
#: prompt. SINGLE HOME: the route-seed delivery path (``orchestrator``) and the
#: steward-facing projection (``learning_health``) must agree on this number,
#: or the surface reports a truncation the prompt never suffered — or misses
#: one it did.
DELIVERY_FIELD_CAP = 1_024

#: Disclosure marker appended to any value the delivery cap shortened.
TRUNCATION_MARKER = "… [truncated]"


def _section_values(document: dict, scope: GuidanceScope,
                    with_base: bool) -> list[str]:
    """Ordered values of one delivery section: generic base first, notes after."""
    values = [document["base"]] if with_base and document["base"] else []
    values.extend(document[_SCOPE_ARRAY[scope]])
    return values


def render_orchestrator_sections(document) -> dict[str, str]:
    normalized = parse_document(document)
    return {
        field: _fenced(title, policy,
                       _section_values(normalized, scope, with_base))
        for field, title, policy, scope, with_base in _DELIVERY_SECTIONS
    }


def _largest_head(title: str, policy: str, others: list[str], text: str,
                  cap: int, marker: str) -> "str | None":
    """Longest head of ``text`` that still fits beside ``others``.

    ``None`` when nothing beyond the disclosure marker fits — a field must
    never deliver a bare marker.
    """
    low, high, best = 0, len(text), None
    while low <= high:
        mid = (low + high) // 2
        head = text if mid >= len(text) else text[:mid].rstrip() + marker
        if len(_fenced(title, policy, [head, *others])) <= cap:
            best, low = head, mid + 1
        else:
            high = mid - 1
    if best is None or (best != text and len(best) <= len(marker)):
        return None
    return best


def _fit_section(title: str, policy: str, base: str, notes: list[str],
                 cap: int, marker: str) -> list[tuple[str, str]]:
    """``(original, delivered)`` pairs that fit ``cap`` as a WELL-FORMED fence.

    Notes are packed NEWEST-first (the most recent learning survives a tight
    cap) and emitted in append order; the generic base prose gets only the room
    that remains. Plain head truncation did the opposite — base is long, so it
    consumed the whole cap and the notes never reached the prompt — and it also
    severed the closing fence.
    """
    kept: list[str] = []
    for note in reversed(notes):
        if len(_fenced(title, policy, [note, *kept])) > cap:
            break
        kept.insert(0, note)
    if kept:
        head = _largest_head(title, policy, kept, base, cap, marker) if base else None
        pairs = [(note, note) for note in kept]
        return [(base, head), *pairs] if head else pairs
    remainder = notes[-1] if notes else base
    if not remainder:
        return []
    head = _largest_head(title, policy, [], remainder, cap, marker)
    return [(remainder, head)] if head else []


def empty_delivery_report() -> dict:
    """The zero (nothing dropped) delivery-truncation report shape."""
    return {"truncated": False, "fields": [], "field_cap": DELIVERY_FIELD_CAP,
            "notes_total": 0, "notes_dropped": 0, "notes_truncated": 0,
            "chars_dropped": 0, "base_chars_dropped": 0,
            "dropped_digests": [],
            "scope_notes_dropped": {scope.value: 0 for scope in GuidanceScope}}


#: Hard cap on the dropped-note digests one report carries (payload-bounded).
DROPPED_DIGEST_CAP = 10


def _record_drop(report: dict, field: str, cap: int, scope: GuidanceScope,
                 base: str, pairs: list[tuple[str, str]],
                 values: list[str]) -> None:
    """Accumulate payload-safe counts for one shortened field.

    Counts and content digests only — never note text, and never a claim that
    the model read what WAS delivered.
    """
    delivered = dict(pairs)
    for value in values:
        text = delivered.get(value)
        if text == value:
            continue
        lost = len(value) - len(text or "")
        report["chars_dropped"] += lost
        if value == base:
            report["base_chars_dropped"] += lost
            continue
        if text:
            report["notes_truncated"] += 1
            continue
        report["notes_dropped"] += 1
        report["scope_notes_dropped"][scope.value] += 1
        if len(report["dropped_digests"]) < DROPPED_DIGEST_CAP:
            report["dropped_digests"].append(content_digest(value))
    report["truncated"] = True
    report["fields"].append(field)
    report["field_cap"] = min(report["field_cap"], cap)


def fit_delivery_sections(document, caps: "dict[str, int] | None" = None, *,
                          marker: str = TRUNCATION_MARKER,
                          ) -> tuple[dict[str, str], dict]:
    """Render the delivery sections under a per-field cap, PRESERVING notes.

    PURE. Truncates CONTENT, not text: steward/learned notes are kept and the
    generic base prose yields the room, and every emitted section is a
    well-formed fence. ``caps`` overrides :data:`DELIVERY_FIELD_CAP` per field.
    Returns ``(sections, report)``; a section that fits is byte-identical to
    :func:`render_orchestrator_sections`.
    """
    normalized = parse_document(document)
    sections: dict[str, str] = {}
    report = empty_delivery_report()
    for field, title, policy, scope, with_base in _DELIVERY_SECTIONS:
        values = _section_values(normalized, scope, with_base)
        report["notes_total"] += len(normalized[_SCOPE_ARRAY[scope]])
        cap = (caps or {}).get(field, DELIVERY_FIELD_CAP)
        text = _fenced(title, policy, values)
        if len(text) <= cap:
            sections[field] = text
            continue
        base = normalized["base"] if with_base else ""
        notes = normalized[_SCOPE_ARRAY[scope]]
        pairs = _fit_section(title, policy, base, notes, cap, marker)
        sections[field] = _fenced(title, policy, [text for _v, text in pairs])
        _record_drop(report, field, cap, scope, base, pairs, values)
    return sections, report


# --------------------------------------------------------------------------- #
# Route-seed delivery helpers: shared sections and the bounded report aggregate
# --------------------------------------------------------------------------- #
#: Field name a SHARED guidance section is reported under. Not a per-system
#: delivery field: one section is delivered once for every system referencing it.
SHARED_GUIDANCE_FIELD = "shared_guidance"

#: Prefix of the short reference a system's ``query_guidance`` carries when its
#: base is delivered through a shared section. Engine-generated, trusted text.
SHARED_REFERENCE_PREFIX = "see shared guidance: "


def shared_reference(key: str) -> str:
    """The reference line pointing a system at shared section ``key``."""
    return f"{SHARED_REFERENCE_PREFIX}{key}"


def shared_reference_key(text) -> "str | None":
    """The shared-section key a delivered ``query_guidance`` references, else None."""
    if not isinstance(text, str) or not text.startswith(SHARED_REFERENCE_PREFIX):
        return None
    key = text[len(SHARED_REFERENCE_PREFIX):].split("\n", 1)[0].strip()
    return key or None


def merge_delivery_reports(reports: list) -> "dict | None":
    """Combine one system's delivery reports (its own fields + a shared section).

    ``None`` when nothing was dropped; a single report is returned unchanged.
    """
    reports = [r for r in reports if isinstance(r, dict) and r.get("truncated")]
    if not reports:
        return None
    if len(reports) == 1:
        return reports[0]
    merged = empty_delivery_report()
    merged["truncated"] = True
    for report in reports:
        merged["fields"].extend(f for f in report.get("fields") or []
                                if f not in merged["fields"])
        merged["field_cap"] = min(merged["field_cap"],
                                  report.get("field_cap", DELIVERY_FIELD_CAP))
        for key in ("notes_total", "notes_dropped", "notes_truncated",
                    "chars_dropped", "base_chars_dropped"):
            merged[key] += int(report.get(key) or 0)
        for scope, count in (report.get("scope_notes_dropped") or {}).items():
            merged["scope_notes_dropped"][scope] = (
                merged["scope_notes_dropped"].get(scope, 0) + int(count or 0))
        room = DROPPED_DIGEST_CAP - len(merged["dropped_digests"])
        merged["dropped_digests"].extend((report.get("dropped_digests") or [])[:max(0, room)])
    return merged


#: One-letter codes the prompt aggregate uses for the reported fields.
DELIVERY_FIELD_CODES = {"query_guidance": "q", "answer_requirements": "a",
                        "legacy_guidance": "l", SHARED_GUIDANCE_FIELD: "s"}
_FIELD_BY_CODE = {code: field for field, code in DELIVERY_FIELD_CODES.items()}

#: One-letter codes for the per-scope dropped-note counts in the aggregate.
SCOPE_CODES = {GuidanceScope.QUERY_HINT.value: "q",
               GuidanceScope.ANSWER_REQUIREMENT.value: "a",
               GuidanceScope.LEGACY_UNSCOPED.value: "l"}
_SCOPE_BY_CODE = {code: scope for scope, code in SCOPE_CODES.items()}

#: Bound on the per-system rows the prompt aggregate carries. A system past it
#: is COUNTED (``systems_omitted``) and its drops cannot be verified, so the
#: composed-context reader withholds its notes (``dropped_note_digests``).
AGGREGATE_SYSTEM_CAP = 20


def dropped_note_digests(scope_digests, report) -> "set | None":
    """The EXACT digests of the notes a delivery report says were dropped.

    Exact because :func:`fit_delivery_sections` drops an OLDEST PREFIX per
    scope: notes are packed newest-first and the packer stops at the first
    note that does not fit, so the kept notes are always the newest contiguous
    run (when none fits, only the newest survives, as a head). The first
    ``scope_notes_dropped[scope]`` digests of ``scope_digests[scope]`` (in
    document order) are therefore exactly the dropped ones, for any number of
    drops, at zero digest cost in the prompt. This prefix property is pinned
    by ``test_route_seed_routing_guidance``.

    Returns ``set()`` when nothing was dropped, and ``None`` when the drop
    cannot be verified: a count is malformed or exceeds the digests on record,
    the per-scope counts do not add up to ``notes_dropped``, or a digest the
    report lists as dropped is not in the computed prefix. ``None`` means the
    caller must NOT emit this system's notes as delivered.
    """
    report = report if isinstance(report, dict) else {}
    try:
        total = int(report.get("notes_dropped") or 0)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    if total == 0:
        counts = report.get("scope_notes_dropped")
        clean = not isinstance(counts, dict) or not any(counts.values())
        return set() if clean and not report.get("dropped_digests") else None
    counts = report.get("scope_notes_dropped")
    listed = set(report.get("dropped_digests") or [])
    digests = scope_digests if isinstance(scope_digests, dict) else {}
    if not isinstance(counts, dict) or not any(counts.values()):
        return listed if len(listed) >= total else None
    dropped: set = set()
    counted = 0
    for scope, count in counts.items():
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            return None
        values = digests.get(scope) or []
        if count < 0 or count > len(values):
            return None
        dropped.update(values[:count])
        counted += count
    if counted != total or not listed <= dropped:
        return None
    return dropped


def aggregate_delivery_reports(entries: list) -> dict:
    """ONE bounded prompt-side summary of every system's delivery report.

    ``entries`` is ``[(system_id, report), ...]``. Per system the row is
    ``[chars_dropped, notes_dropped, notes_truncated, field_codes]`` (codes from
    :data:`DELIVERY_FIELD_CODES`), plus a fifth element ``{scope_code: count}``
    (:data:`SCOPE_CODES`) when notes were dropped. Those per-scope counts are
    COMPLETE accounting: with the scope digests already in the seed they
    identify every dropped note exactly (:func:`dropped_note_digests`), so no
    digest list rides the prompt. Bounded by :data:`AGGREGATE_SYSTEM_CAP` rows;
    systems beyond it are COUNTED (``systems_omitted``), never silently lost.
    The full per-system report stays in the ``GUIDANCE_DELIVERY_TRUNCATION``
    log line and the ``learning_health.guidance_delivery`` projection.
    """
    out: dict = {"chars_dropped": 0, "notes_dropped": 0, "systems": {}}
    systems_omitted = 0
    for system_id, report in entries:
        chars = int(report.get("chars_dropped") or 0)
        dropped = int(report.get("notes_dropped") or 0)
        cut = int(report.get("notes_truncated") or 0)
        out["chars_dropped"] += chars
        out["notes_dropped"] += dropped
        codes = "".join(DELIVERY_FIELD_CODES.get(f, "?")
                        for f in report.get("fields") or [])
        row = [chars, dropped, cut, codes]
        scopes = {SCOPE_CODES.get(scope, scope): int(count)
                  for scope, count in (report.get("scope_notes_dropped") or {}).items()
                  if count}
        if dropped:
            row.append(scopes)
        if len(out["systems"]) < AGGREGATE_SYSTEM_CAP:
            out["systems"][str(system_id)] = row
        else:
            systems_omitted += 1
    if systems_omitted:
        out["systems_omitted"] = systems_omitted
    return out


def decode_delivery_aggregate(aggregate) -> dict:
    """Decode :func:`aggregate_delivery_reports` for the composed-context reader.

    Returns ``{"entries": [report-shaped dicts], "unverified": {system ids},
    "systems_omitted": int}``. A malformed row cannot be trusted either way,
    so its system lands in ``unverified`` (its notes must not be reported as
    delivered); ``systems_omitted`` > 0 means systems outside ``entries`` are
    unverifiable too. A missing or non-dict aggregate decodes to nothing.
    """
    out: dict = {"entries": [], "unverified": set(), "systems_omitted": 0}
    if not isinstance(aggregate, dict):
        return out
    rows = aggregate.get("systems")
    if not isinstance(rows, dict):
        out["systems_omitted"] = 1 if aggregate else 0
        return out
    try:
        out["systems_omitted"] = max(0, int(aggregate.get("systems_omitted") or 0))
    except (TypeError, ValueError):
        out["systems_omitted"] = 1
    for system_id, row in rows.items():
        entry = _decode_aggregate_row(str(system_id), row)
        if entry is None:
            out["unverified"].add(str(system_id))
        else:
            out["entries"].append(entry)
    return out


def _count(value) -> "int | None":
    """A non-negative int count, else None (bool, float, str and negative refused)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _decode_aggregate_row(system_id: str, row) -> "dict | None":
    """One aggregate row as a report, or ``None`` when it cannot be trusted.

    A row is VERIFIED only when every total and per-scope count is a
    non-negative integer, every scope code is known, and the per-scope counts
    add up to ``notes_dropped`` (zero included). Anything else is a
    contradiction the reader must not resolve in favour of delivery, so the
    caller withholds the system's notes and reports ``accounting: "incomplete"``.
    """
    if not isinstance(row, list) or len(row) not in (4, 5) or not isinstance(row[3], str):
        return None
    chars, dropped, cut = (_count(value) for value in row[:3])
    scopes = row[4] if len(row) == 5 else {}
    if None in (chars, dropped, cut) or not isinstance(scopes, dict):
        return None
    scope_counts = {}
    for code, value in scopes.items():
        count = _count(value)
        if code not in _SCOPE_BY_CODE or count is None:
            return None
        scope_counts[_SCOPE_BY_CODE[code]] = count
    if sum(scope_counts.values()) != dropped:
        return None
    return {"system": system_id,
            "fields": [_FIELD_BY_CODE.get(code, code) for code in row[3]],
            "chars_dropped": chars, "notes_dropped": dropped,
            "notes_truncated": cut, "scope_notes_dropped": scope_counts}


def fit_system_query_sections(document) -> tuple[str, dict]:
    """The system agent's per-question guidance under the delivery cap.

    Only query-time and legacy sections; final requirements are withheld
    before fitting, so the report covers only what this block delivers.
    Same fit as the route seed (:func:`fit_delivery_sections`): every section
    is a well-formed fence at or below :data:`DELIVERY_FIELD_CAP`, and a
    section that fits is unchanged. Returns ``(text, report)``."""
    normalized = parse_document(document, plain_is_base=False)
    normalized["answer_requirements"] = []
    sections, report = fit_delivery_sections(normalized)
    text = "\n\n".join(filter(None, (
        sections["query_guidance"], sections["legacy_guidance"])))
    return text, report


def render_system_query_sections(document) -> str:
    """Render only query-time and legacy sections; withhold final requirements."""
    return fit_system_query_sections(document)[0]


def delivery_truncation_log(system_id, report: dict) -> tuple:
    """``(format, *args)`` of the one ``GUIDANCE_DELIVERY_TRUNCATION`` line a
    delivery drop is logged with, wherever it is delivered. Counts only: never
    note text, and never a claim that the model read what was delivered."""
    message = ("GUIDANCE_DELIVERY_TRUNCATION system_id=%s field_cap=%d fields=%s "
               "notes_total=%d notes_dropped=%d notes_truncated=%d chars_dropped=%d "
               "action=report-only")
    return (message, str(system_id or "")[:96], report["field_cap"],
            ",".join(report["fields"]), report["notes_total"],
            report["notes_dropped"], report["notes_truncated"],
            report["chars_dropped"])
