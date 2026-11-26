# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Coverage layer (S4.2): proactive, read-only thread-level gap report.

This module composes the metric-layer satisfiability helpers (ADR 0016) with a
single graph read of the onboarded *covered concept* set to answer, per card and
for the whole thread, "what is satisfiable today and what is missing?".

Design (ADR 0001 / ADR 0006):
  * Import-pure core — deterministic, no LLM/eval. The ONLY impure seam is the
    injected ``neptune_query`` graph read (default arg, swappable in tests).
  * Read-only/downstream — coverage NEVER blocks onboarding; the route is GET and
    the gating only annotates cards (ADR 0006).
  * One satisfiability source — ``card_coverage``/``thread_coverage`` feed BOTH the
    dashboard gating and the gap report (one graph read, one composition).

``suggestion_for`` is a deterministic seam for the richer S4.3 affinity
suggestions (ADR 0007); it returns plain text today.
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from .card_layer import get_card_layer
from .catalog import CONCEPT_AFFINITY, catalog, missing_expected, system_types_for
from .db import neptune_query
from .metric_layer import get_metric_layer


_COVERED_CYPHER = ("MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->"
                   "(:Field)-[:MAPS_TO]->(c:Concept) RETURN DISTINCT c.id AS id")


def covered_concepts(query=neptune_query) -> frozenset[str]:
    """ONE graph read → set of concept ids mapped by any onboarded system.

    Offboarding deletes a system's edges, so ≥1 ``MAPS_TO`` ≡ covered.
    ``neptune_query`` swallows errors → ``[]`` → ``frozenset()`` (no-graph safe).
    """
    return frozenset(r["id"] for r in query(_COVERED_CYPHER) if r.get("id"))


def suggestion_for(concept: str) -> str:
    """Plain/deterministic now; S4.3 affinity (ADR 0007) enriches later (clean seam)."""
    return f"Onboard a system providing {concept}"


def affinity_suggestion_for(concept: str) -> str:
    """Affinity-enriched suggestion (ADR 0007). Falls back to the generic string when the
    concept has no affinity types, so it is always a safe drop-in for ``suggestion_for``."""
    types = system_types_for(concept)
    if not types:
        return suggestion_for(concept)
    if len(types) == 1:
        lst = f"a {types[0]}"
    elif len(types) == 2:
        lst = f"a {types[0]} or {types[1]}"
    else:
        lst = "a " + ", ".join(types[:-1]) + f", or {types[-1]}"
    return f"Onboard {lst} — typically provides {concept}"


def card_coverage(card, covered, *, metric_layer=None) -> dict:
    """Per-card status fed to BOTH dashboard gating and the gap report (one source).

    ``satisfiable`` ⟺ every ``metric_ref`` satisfiable; ``missing_concepts`` is the
    union of each unsatisfiable metric's cheapest unmet path (ADR 0016).
    """
    ml = metric_layer or get_metric_layer()
    missing: set[str] = set()
    sat = True
    for ref in card.metric_refs:
        m = ml.get_metric(ref)
        if not ml.is_satisfiable(m, covered):
            sat = False
            missing |= ml.missing_concepts(m, covered)
    return {"card_id": card.id, "satisfiable": sat,
            "missing_concepts": sorted(missing)}


def thread_coverage(covered, *, card_layer=None, metric_layer=None,
                    suggester=suggestion_for) -> dict:
    """Thread-level report: per-card + inverted gaps + summary roll-up. Pure.

    ``suggester`` defaults to ``suggestion_for`` so the composition is byte-identical
    to S4.2; the route injects ``affinity_suggestion_for`` only when gated (ADR 0007).
    """
    cl = card_layer or get_card_layer()
    cards = [card_coverage(c, covered, metric_layer=metric_layer) for c in cl.list_cards()]
    blocks: dict[str, list[str]] = {}
    for c in cards:
        for concept in c["missing_concepts"]:
            blocks.setdefault(concept, []).append(c["card_id"])
    gaps = [{"concept": k, "blocks_cards": v, "suggestion": suggester(k)}
            for k, v in sorted(blocks.items())]
    return {"cards": cards, "gaps": gaps,
            "summary": {"total": len(cards),
                        "satisfiable": sum(1 for c in cards if c["satisfiable"]),
                        "uncovered": sum(1 for c in cards if not c["satisfiable"])}}


_SYS_COVERED_CYPHER = ("MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->"
                       "(:Field)-[:MAPS_TO]->(c:Concept) RETURN DISTINCT c.id AS id")


def system_covered_concepts(system_id, query=neptune_query) -> frozenset[str]:
    """ONE scoped graph read → set of concept ids mapped by a SINGLE system.

    Scoped variant of ``covered_concepts`` (``{id: $sid}``). ``neptune_query``
    swallows errors → ``[]`` → ``frozenset()`` (no-graph safe).
    """
    return frozenset(r["id"] for r in query(_SYS_COVERED_CYPHER, {"sid": system_id})
                     if r.get("id"))


router = APIRouter(prefix="/api", tags=["coverage"])


@router.get("/coverage")
def coverage():
    """Thread-level gap report (session-authed via app-wide AuthMiddleware; NOT admin).

    One graph read → pure composition. Read-only; never blocks onboarding (ADR 0006).
    ``RESOLVER_AFFINITY`` (DEFAULT OFF) selects the suggester at this impure boundary
    only; unset ⇒ byte-identical S4.2 output.
    """
    suggester = (affinity_suggestion_for
                 if os.environ.get("RESOLVER_AFFINITY", "").strip() else suggestion_for)
    return thread_coverage(covered_concepts(), suggester=suggester)


@router.get("/affinity/{system_id}")
def affinity_for_system(system_id: str):
    """Per-system expected/mapped/missing affinity (session-authed; gated by RESOLVER_AFFINITY).

    Read-only; one scoped ``MAPS_TO`` graph read. Flag OFF ⇒ ``{"enabled": False}``.
    """
    if not os.environ.get("RESOLVER_AFFINITY", "").strip():
        return {"enabled": False}
    entry = catalog.get(system_id)
    stype = entry.get("system_type") if entry else None
    mapped = system_covered_concepts(system_id)
    expected = sorted(CONCEPT_AFFINITY.get(stype, frozenset())) if stype else []
    return {"enabled": True, "system_id": system_id, "system_type": stype,
            "expected": expected, "mapped": sorted(mapped),
            "missing": missing_expected(stype, mapped) if stype else []}
