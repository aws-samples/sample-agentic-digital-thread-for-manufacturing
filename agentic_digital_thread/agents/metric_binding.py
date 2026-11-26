# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ONE concept-to-column binding rule + ONE column-identifier home (ADR-0076).

Selection (D1) is TOTAL with three outcomes, for the metric's OWN concept and
for every predicate concept alike:

1. exactly one DISTINCT ``(table, column)`` candidate → use it
   (``binding_basis = "single"``);
2. two or more → an ACTIVE ``AuthorityRule`` for ``(system, concept)`` whose
   steward-set ``field`` (and optional ``table``) names EXACTLY one candidate
   decides (``binding_basis = "authority_rule"``);
3. otherwise → WITHHOLD as :data:`AMBIGUOUS_BINDING_STATUS`, every candidate
   NAMED in sorted order.

No role / confidence / first-row heuristic — a deterministic-but-unjustified
pick is a proxy. Rendering (D4): a dotted physical column (a Glue-flattened
struct member) is ONE identifier — never segment-split, never underscored —
with every segment validated fail-closed; a flat name renders exactly as the
engine always has.

Pure: no graph client (the resolver executes these Cypher templates through
its own ``neptune_query`` seam), no LLM, no deployment literal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

#: ADR-0076 D3 — the honest-data status for a concept bound to 2+ columns on
#: one system with no steward choice. The ONE spelling; consumers import it.
AMBIGUOUS_BINDING_STATUS = "ambiguous_binding"

#: Candidate PAIRS considered — the cap applies at the ``(table, column)``
#: grain (one grouped row per pair, provenance collected inside it), so
#: duplicate provenance edges can never fill the page and hide a second pair.
BINDING_CANDIDATE_CAP = 25
#: The read asks for ONE sentinel pair beyond the cap: a page that returns it
#: is NOT exhaustive (:func:`is_truncated`), so neither selection nor the
#: steward write door may validate anything against it — withhold / reject.
BINDING_CANDIDATE_READ_LIMIT = BINDING_CANDIDATE_CAP + 1

BASIS_SINGLE = "single"
BASIS_AUTHORITY_RULE = "authority_rule"

_CANDIDATE_RETURN = (
    "RETURN se.name AS table, f.name AS column, "
    "collect({role: r.role, confidence: r.confidence, source: r.method, "
    "version: r.version}) AS edges "
    "ORDER BY table, column "
    f"LIMIT {BINDING_CANDIDATE_READ_LIMIT}"
)

#: The metric's OWN concept: every SchemaEntry of the system (the binding
#: DEFINES the measure table, so it cannot be pre-scoped).
MEASURE_BINDING_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)"
    "-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) "
    + _CANDIDATE_RETURN
)

#: A predicate concept: the EXACT measure SchemaEntry (ADR-0075 D2 scope).
PREDICATE_BINDING_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {name: $table})"
    "-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) "
    + _CANDIDATE_RETURN
)

#: The steward's choice lives on the ONE authority home. Two DISTINCT active
#: field-carrying pairs is itself an ambiguity (fail-loud, never a pick).
BINDING_RULE_CYPHER = (
    "MATCH (ar:AuthorityRule {system_id: $sid, concept: $concept}) "
    "WHERE coalesce(ar.status, '') <> 'deactivated' AND ar.field IS NOT NULL "
    "RETURN DISTINCT ar.table AS table, ar.field AS field "
    "ORDER BY table, field LIMIT 2"
)

_SEGMENT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


@dataclass(frozen=True)
class BindingCandidate:
    """One DISTINCT ``(SchemaEntry.name, Field.name)`` binding, EXACT as stored."""

    table: str
    column: str
    role: str | None = None
    confidence: float | None = None
    source: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class BindingChoice:
    candidate: BindingCandidate
    basis: str                      # BASIS_SINGLE | BASIS_AUTHORITY_RULE


@dataclass(frozen=True)
class BindingAmbiguity:
    concept: str
    system_id: str
    candidates: tuple[BindingCandidate, ...]
    detail: str


def _text(value) -> str:
    return "" if value is None else str(value)


def _confidence_key(value) -> tuple:
    """Total order over confidence: numbers (ascending), then text, then None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
        return (0, float(value), "")
    return (2, 0.0, "") if value is None else (1, 0.0, str(value))


def _edge_key(edge: dict) -> tuple:
    """Provenance tie-break over EVERY lineage field (confidence included) so
    the edge kept for a duplicated pair never depends on graph row order."""
    return (_text(edge.get("role")), _confidence_key(edge.get("confidence")),
            _text(edge.get("source")), _text(edge.get("version")))


def _flat_rows(rows: Iterable[dict] | None) -> Iterable[dict]:
    """Expand grouped rows (``edges`` collected per pair) into per-edge rows;
    an already-flat row passes through unchanged."""
    for row in rows or ():
        edges = row.get("edges")
        if not isinstance(edges, list):
            yield row
            continue
        for edge in edges or [{}]:
            yield {**(edge if isinstance(edge, dict) else {}),
                   "table": row.get("table"), "column": row.get("column")}


def candidates_from_rows(rows: Iterable[dict] | None) -> tuple[BindingCandidate, ...]:
    """SORTED distinct candidates, names EXACT (no tail split of either).

    Order-independent: per pair, the provenance kept is the edge with the
    smallest :func:`_edge_key` — a real edge, never a field-wise blend."""
    best: dict[tuple[str, str], dict] = {}
    for row in _flat_rows(rows):
        pair = (_text(row.get("table")), _text(row.get("column")))
        if pair not in best or _edge_key(row) < _edge_key(best[pair]):
            best[pair] = row
    return tuple(
        BindingCandidate(table=t, column=c, role=best[(t, c)].get("role"),
                         confidence=best[(t, c)].get("confidence"),
                         source=best[(t, c)].get("source"),
                         version=best[(t, c)].get("version"))
        for t, c in sorted(best))


def is_truncated(candidates: Sequence[BindingCandidate]) -> bool:
    """True when the read returned the sentinel pair: candidates beyond the
    cap may exist, so the set cannot back a pick or validate a write."""
    return len(candidates) > BINDING_CANDIDATE_CAP


def match_candidate(
    candidates: Sequence[BindingCandidate], field: str, table: str | None = None
) -> list[BindingCandidate]:
    """Candidates whose column is EXACTLY ``field`` (and table, when given)."""
    return [c for c in candidates
            if c.column == field and (table is None or c.table == table)]


def _rule_pairs(rows: Iterable[dict] | None) -> list[tuple[str | None, str]]:
    pairs = {(_text(r.get("table")) or None, _text(r.get("field")))
             for r in rows or () if _text(r.get("field"))}
    return sorted(pairs, key=lambda p: (p[0] or "", p[1]))


def describe_candidates(candidates: Sequence[BindingCandidate]) -> str:
    """``table: 'a', 'b'; other: 'c'`` — sorted, every candidate named."""
    by_table: dict[str, list[str]] = {}
    for c in candidates:
        by_table.setdefault(c.table, []).append(repr(c.column))
    return "; ".join(f"{t}: {', '.join(cols)}" for t, cols in sorted(by_table.items()))


def _ambiguity(concept: str, system_id: str,
               candidates: tuple[BindingCandidate, ...], why: str) -> BindingAmbiguity:
    # No count in the text: the metric layer tells the model to state this
    # reason verbatim, and an absent-metric sentence must carry no digit.
    more = ""
    if is_truncated(candidates):
        candidates = candidates[:BINDING_CANDIDATE_CAP]
        more = "; and more"
    detail = (
        f"concept {concept!r} is bound to more than one column on system "
        f"{system_id!r} ({describe_candidates(candidates)}{more}){why}; a "
        "steward must choose one via POST /api/metrics/set-authority {field}"
    )
    return BindingAmbiguity(concept, system_id, candidates, detail)


def _rule_verdict(candidates: tuple[BindingCandidate, ...],
                  pairs: list[tuple[str | None, str]]) -> tuple[BindingCandidate | None, str]:
    """``(chosen, why-not)`` for the active field-carrying rule pairs."""
    if not pairs:
        return None, ""
    if len(pairs) > 1:
        shown = [f"{t}.{f}" if t else f for t, f in pairs]
        return None, ("; more than one active AuthorityRule carries a "
                      f"different field ({shown})")
    table, field = pairs[0]
    matches = match_candidate(candidates, field, table)
    if len(matches) == 1:
        return matches[0], ""
    where = f" (table {table!r})" if table else ""
    return None, (f"; the AuthorityRule field {field!r}{where} names "
                  f"{'none' if not matches else 'more than one'} of them")


def select_binding(
    concept: str, system_id: str, candidates: tuple[BindingCandidate, ...],
    read_rules: Callable[[], Iterable[dict] | None],
) -> BindingChoice | BindingAmbiguity:
    """The ONE selection (D1). ``read_rules`` runs ONLY for 2+ candidates
    on an EXHAUSTIVE page — a truncated page withholds without reading rules
    (a rule validated against a partial set could match a hidden duplicate).

    Raises ``ValueError`` on zero candidates — "unbound" is the caller's
    distinct gap (coverage / unresolved predicate), never an ambiguity."""
    if not candidates:
        raise ValueError(f"no binding candidates for concept {concept!r}")
    if len(candidates) == 1:
        return BindingChoice(candidates[0], BASIS_SINGLE)
    if is_truncated(candidates):
        return _ambiguity(concept, system_id, candidates,
                          "; the candidate set is not exhaustive, so no "
                          "AuthorityRule field can be validated against it")
    chosen, why = _rule_verdict(candidates, _rule_pairs(read_rules()))
    if chosen is not None:
        return BindingChoice(chosen, BASIS_AUTHORITY_RULE)
    return _ambiguity(concept, system_id, candidates, why)


# --- ONE column-identifier home (D4) --------------------------------------- #

def validate_column_name(name: str) -> str:
    """Return ``name`` unchanged when EVERY ``.``-segment is a SQL identifier.

    Empty segments (leading / trailing / double dots) and any character
    outside ``[A-Za-z0-9_]`` fail closed with ``ValueError``."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid identifier: {name!r}")
    for segment in name.split("."):
        if not _SEGMENT.fullmatch(segment):
            raise ValueError(f"Invalid identifier: {name!r}")
    return name


def render_column_identifier(name: str) -> str:
    """SCALAR register: flat → bare (byte-identical); dotted → ONE ``"a.b"``."""
    column = validate_column_name(name)
    return f'"{column}"' if "." in column else column


def quote_column_identifier(name: str) -> str:
    """ALREADY-QUOTED register: always ONE ``"<name>"`` (dots inside)."""
    return f'"{validate_column_name(name)}"'
