# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Canonical system-id normalization against the catalog (pure, offline).

LLM-emitted proposals name systems in free text — a display name
("Milwaukee MES"), a bare type ("ERP"), or the canonical id itself ("mes-mil").
The proposal store keys and supersede logic require the CANONICAL catalog id,
so ingestion must normalize before computing canonical keys.

``normalize_system_id(value, known)`` resolves a free-text system reference to
a canonical catalog id using the caller-supplied catalog entries. Resolution
ladder (first hit wins; never guesses):

  1. knowledge-graph identity tokens (via
     ``proposal_scoring.is_knowledge_graph_system``) → ``None`` — the sentinel
     meaning "self-referential; caller drops the proposal";
  2. exact ``id`` match → that id (canonical ids pass through unchanged);
  3. UNIQUE normalized match (casefold, strip non-alphanumerics) against each
     entry's ``name`` and ``id`` ("Milwaukee MES" → ``mes-mil``) — compared as
     BOTH the concatenated normal form AND the order-insensitive token SET
     (casefold, non-alnum-split: "MES Milwaukee" also → ``mes-mil``); an
     ambiguous match via either form returns the input unchanged;
  4. UNIQUE ``system_type`` match (casefold) — "ERP" → ``erp`` only when
     exactly one catalog entry has that type; ambiguous ("MES" with three MES
     systems) returns the input unchanged — never guess;
  5. UNIQUE ``plant`` match (same normal form) — a reference naming the PLANT
     a system lives at resolves when exactly one catalog entry has that plant;
     a plant hosting several systems is ambiguous (input unchanged, candidates
     exposed) — never guess;
  6. unknown system → input unchanged (the normalizer itself never drops
     unknowns; whether an unknown reference is stored or rejected is the
     CALLER's ingestion policy — ``resolve_system_id`` exposes the
     ambiguous-vs-no-match distinction so ingestion can drop agent-sourced
     junk while keeping legitimate ambiguous passthroughs).

``ambiguous_candidates(value, known)`` exposes WHICH canonical ids an
ambiguous reference matched (sorted; empty for every other verdict), so review
surfaces can state the candidates ("'MES' is ambiguous across mes-cr, mes-det,
mes-mil") — same matching internals as the ladder, never a re-implementation.

Per ADR 0001/0020/0021: import-pure — stdlib only (plus the equally-pure
``proposal_scoring``), NO boto3, NO pg8000, NO catalog import at module
level. The caller passes ``known`` (the impure ``catalog.get_all()`` read
lives in the ingestion layer), keeping this module unit-testable with zero AWS.
"""
from __future__ import annotations

import re

from .proposal_scoring import is_knowledge_graph_system

_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _norm(value: str | None) -> str:
    """Casefold + strip non-alphanumerics — the name/id matching normal form."""
    return _NON_ALNUM.sub("", (value or "").strip().casefold())


def _token_set(value: str | None) -> frozenset:
    """Casefolded, non-alnum-split token SET — the order-insensitive form.

    ``'MES Milwaukee'`` and ``'Milwaukee MES'`` both become
    ``{'mes', 'milwaukee'}``, so rung (3) matches a name regardless of token
    order (F7 — the concatenated normal form alone is order-sensitive, which
    silently dropped agent proposals as ``no_match``)."""
    return frozenset(
        t for t in _TOKEN_SPLIT.split((value or "").strip().casefold()) if t
    )


# --------------------------------------------------------------------------- #
# Resolution verdicts returned by ``resolve_system_id`` — they expose WHY a
# value resolved the way it did, so callers can distinguish an AMBIGUOUS
# passthrough ("MES" matching several MES systems — legitimate, never guess)
# from a NO-MATCH passthrough (a reference naming NO catalog id/name/type —
# junk the ingestion layer may choose to drop for agent-sourced proposals).
# --------------------------------------------------------------------------- #
VERDICT_EMPTY = "empty"                      # no value supplied — nothing to resolve
VERDICT_KNOWLEDGE_GRAPH = "knowledge_graph"  # names the graph itself (None sentinel)
VERDICT_MATCHED = "matched"                  # resolved to a canonical catalog id
VERDICT_AMBIGUOUS = "ambiguous"              # multiple candidates — passthrough, never guess
VERDICT_NO_MATCH = "no_match"                # matched NOTHING — passthrough


def _resolve(value: str | None, known: list[dict]) -> tuple[str | None, str, list[str]]:
    """The resolution ladder — the single implementation both public entry
    points wrap. Returns ``(resolved, verdict, candidates)`` where
    ``candidates`` is the SORTED list of canonical ids an AMBIGUOUS reference
    matched (rung 3 name/id, rung 4 system_type, or rung 4b plant — whichever
    rung produced the ambiguity) and ``[]`` for every other verdict. Factored out so
    ``ambiguous_candidates`` reuses the exact matching internals instead of
    duplicating them (R1)."""
    if not value:
        return value, VERDICT_EMPTY, []

    # (1) The knowledge graph is the rules engine, not a data system — a
    # proposal naming it is self-referential; the caller drops it.
    if is_knowledge_graph_system(value):
        return None, VERDICT_KNOWLEDGE_GRAPH, []

    # (2) Exact canonical id — pass through unchanged.
    for entry in known:
        if value == entry.get("id"):
            return entry["id"], VERDICT_MATCHED, []

    # (3) Unique normalized name/id match — the concatenated normal form OR the
    # order-insensitive token SET (F7: "MES Milwaukee" must match a system
    # named "Milwaukee MES"). Ambiguity semantics unchanged: 2+ candidates via
    # EITHER form → passthrough, never guess.
    norm = _norm(value)
    tokens = _token_set(value)
    if norm:
        matches = {
            entry["id"]
            for entry in known
            if entry.get("id") and (
                norm == _norm(entry.get("name")) or norm == _norm(entry.get("id"))
                or (tokens and (tokens == _token_set(entry.get("name"))
                                or tokens == _token_set(entry.get("id"))))
            )
        }
        if len(matches) == 1:
            return next(iter(matches)), VERDICT_MATCHED, []
        if len(matches) > 1:
            return value, VERDICT_AMBIGUOUS, sorted(matches)  # never guess

    # (4) Unique system_type match ("ERP" → erp only when exactly one ERP).
    type_norm = (value or "").strip().casefold()
    type_matches = {
        entry["id"]
        for entry in known
        if entry.get("id") and (entry.get("system_type") or "").strip().casefold() == type_norm
    }
    if len(type_matches) == 1:
        return next(iter(type_matches)), VERDICT_MATCHED, []
    if len(type_matches) > 1:
        # Ambiguous type ("MES" ×3) — never guess.
        return value, VERDICT_AMBIGUOUS, sorted(type_matches)

    # (4b) Unique PLANT match — the analyzer sometimes names the PLANT a system
    # lives at rather than the system itself (live incident: a field_annotation
    # whose from_system was a plant/site code, which no earlier rung matches).
    # A plant hosting exactly ONE system resolves to that system's id; a plant
    # hosting SEVERAL systems is a legitimate AMBIGUOUS reference (candidates
    # surfaced so review can state which systems to confirm) — never guess.
    if norm:
        plant_matches = {
            entry["id"]
            for entry in known
            if entry.get("id") and norm == _norm(entry.get("plant"))
        }
        if len(plant_matches) == 1:
            return next(iter(plant_matches)), VERDICT_MATCHED, []
        if len(plant_matches) > 1:
            return value, VERDICT_AMBIGUOUS, sorted(plant_matches)

    # (5) Unknown — pass through unchanged; the VERDICT tells the caller.
    return value, VERDICT_NO_MATCH, []


def resolve_system_id(value: str | None, known: list[dict]) -> tuple[str | None, str]:
    """Resolve a free-text system reference and report HOW it resolved.

    Returns ``(resolved, verdict)`` where ``resolved`` is byte-identical to
    what ``normalize_system_id`` returns for the same inputs (that function is
    a thin wrapper over this one) and ``verdict`` is one of the ``VERDICT_*``
    constants above. The resolution ladder is unchanged: knowledge-graph
    sentinel → exact id → unique name/id → unique system_type → passthrough.
    """
    resolved, verdict, _ = _resolve(value, known)
    return resolved, verdict


def ambiguous_candidates(value: str | None, known: list[dict]) -> list[str]:
    """The sorted canonical ids an AMBIGUOUS reference matches (R1 primitive).

    When ``resolve_system_id(value, known)`` verdicts ``VERDICT_AMBIGUOUS``
    this returns the canonical catalog ids the reference matched — e.g.
    ``"MES"`` against a catalog with three MES systems →
    ``["mes-cr", "mes-det", "mes-mil"]`` — so ingestion can state a review
    reason naming the candidates. For every OTHER verdict (matched, no-match,
    empty, knowledge-graph) it returns ``[]``. Same matching internals as the
    resolution ladder (shared ``_resolve``), so the candidate set can never
    drift from what the ladder considered ambiguous.
    """
    return _resolve(value, known)[2]


def normalize_system_id(value: str | None, known: list[dict]) -> str | None:
    """Resolve a free-text system reference to a canonical catalog id.

    ``known`` is a list of catalog entries (``{"id", "name", "plant",
    "system_type"}`` — extra keys ignored). Returns the canonical ``id`` when
    the reference resolves unambiguously, ``None`` when the reference names the
    knowledge graph itself (self-referential — caller drops), and the input
    unchanged otherwise (ambiguous or unknown — never guess, never drop).
    Callers that need to know WHY a value passed through unchanged use
    ``resolve_system_id`` (same resolution, plus a verdict).
    """
    return resolve_system_id(value, known)[0]
