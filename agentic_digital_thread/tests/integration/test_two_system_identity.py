# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two-system cross-system identity guard — ADR-0011 F1 (S0.2 Wave 4).

A lone onboarded system (e.g. ``cmms``) creates **zero** IdentityRules, so the
cross-system identity machinery is only exercisable on a **>=2-system** graph
(see ADR-0011 F1). This module is the
live regression guard the F1 grill asked for: it confirms that once a second
system is onboarded (via ``harness.sh warm2`` — reset -> onboard cmms -> onboard
erp -> verify), the graph actually carries the canonical
``(:IdentityRule)-[:LINKS_CONCEPT]->(:Concept)`` edges and that >=2 systems map
concepts.

Two tests, both gated by the W2 ``live_graph`` fixture and an explicit
"<2 systems onboarded" skip so the suite stays green in the credential-free /
single-system CI lane:

  (1) ``test_cross_system_identity_links`` — the **graph-level** guard, live now:
      asserts >=1 ``LINKS_CONCEPT`` edge AND >=2 systems with ``MAPS_TO`` once a
      2-system graph exists; skips cleanly otherwise.

  (2) ``test_f1_resolver_cross_system_resolve`` — the **resolver-level** guard,
      live now (S1.2 landed the ADR-0011 F1 read-side fix). ``_fetch_identity_rules``
      reads the canonical ``(:IdentityRule)-[:LINKS_CONCEPT]->(:Concept)`` edge that
      onboarding writes, so an IdentityRule-driven cross-system join is produced; the
      test asserts it on a >=2-system graph and skips cleanly otherwise.

Strictly READ-ONLY against the graph: it imports the audit cypher constants
verbatim (``CYPHER_IDENTITY_LINKS_CONCEPT`` / ``CYPHER_SYSTEMS_WITH_MAPPINGS``)
and only ever calls ``neptune_query`` (via the audit ``_count`` helper). It NEVER
calls ``neptune_write``. ``no_llm_guard`` keeps the resolver deterministic
(ADR-0001).
"""

from __future__ import annotations

from typing import Callable

import pytest

from agents.db import neptune_query
from tests.audit.audit_onboarding_graph import (
    CYPHER_IDENTITY_LINKS_CONCEPT,
    CYPHER_SYSTEMS_WITH_MAPPINGS,
    _count,
    _row_get,
)

pytestmark = pytest.mark.integration

# A shared cross-system identifier concept: both a CMMS (cmms) and an ERP-class
# system are expected to map `equipment-id`, so it is the natural anchor for a
# cross-system identity join. Validated against catalog.CORE_CONCEPTS by onboarding.
SHARED_CONCEPT = "equipment-id"

# A real, stable asset identifier seeded by lambda-adt-seed/004_cmms_maintenance.sql
# (reused from the resolver integration suite). Used only to drive a real resolve.
SHARED_IDENTIFIER = "MKE-FILL-001"

# The cross-system machinery (IdentityRules / LINKS_CONCEPT) requires a >=2-system
# onboard; a lone system creates none (ADR-0011 F1).
SINGLE_SYSTEM_SKIP = (
    "fewer than 2 systems onboarded — a lone system creates zero IdentityRules / "
    "LINKS_CONCEPT edges (ADR-0011 F1). Onboard a 2nd system (e.g. via "
    "'tests/harness/harness.sh warm2 --yes': reset -> onboard cmms -> onboard erp) "
    "to arm this cross-system identity guard."
)


# ---------------------------------------------------------------------------
# Helper (read-only — reuses the audit's reachability traversal verbatim)
# ---------------------------------------------------------------------------

def _systems_with_mappings() -> list[str]:
    """Return the distinct system ids that have >=1 MAPS_TO edge (read-only).

    Reuses the audit ``CYPHER_SYSTEMS_WITH_MAPPINGS`` traversal verbatim (never
    re-declares it). Returns an empty list when no graph is reachable / nothing
    is onboarded — callers turn that into a clean skip.
    """
    rows = neptune_query(CYPHER_SYSTEMS_WITH_MAPPINGS) or []
    return [sid for sid in (_row_get(r, "s.id", "id") for r in rows) if sid]


# ---------------------------------------------------------------------------
# (1) Graph-level cross-system identity guard (live now)
# ---------------------------------------------------------------------------

def test_cross_system_identity_links(live_graph: str) -> None:
    """>=1 LINKS_CONCEPT edge AND >=2 systems map concepts, on a 2-system graph.

    ``live_graph`` first gates on a reachable, onboarded graph (skips otherwise).
    We then require >=2 systems with MAPS_TO — a single system (cmms alone)
    creates no IdentityRules, so we **skip cleanly** rather than fail. With >=2
    systems the canonical ``(:IdentityRule)-[:LINKS_CONCEPT]->(:Concept)`` edges
    must be present (ADR-0011 F1: LINKS_CONCEPT is canonical).
    """
    systems = _systems_with_mappings()
    if len(systems) < 2:
        pytest.skip(f"{SINGLE_SYSTEM_SKIP} (onboarded systems: {sorted(systems)})")

    links_concept = _count(CYPHER_IDENTITY_LINKS_CONCEPT)
    assert links_concept >= 1, (
        "a >=2-system graph must carry the canonical IdentityRule-[:LINKS_CONCEPT]->"
        "Concept edges, but found 0 (ADR-0011 F1). Onboarded systems: "
        f"{sorted(systems)}"
    )
    assert len(systems) >= 2, (
        f"expected >=2 systems with MAPS_TO edges, found {sorted(systems)}"
    )


# ---------------------------------------------------------------------------
# (2) Resolver-level cross-system guard (live since S1.2 ADR-0011 F1 read-side fix)
# ---------------------------------------------------------------------------

def test_f1_resolver_cross_system_resolve(
    live_graph: str,
    local_postgres: dict,
    resolve_fn: Callable[[str, str], tuple],
    no_llm_guard: None,
) -> None:
    """Cross-system resolve on a shared identifier concept (live since S1.2 F1 fix).

    Drives the real ``resolve_entity('equipment-id', <shared id>)`` over a
    >=2-system graph. The IdentityRule-driven sections (``_try_direct_match`` /
    ``_try_pattern_transform``) depend on ``_fetch_identity_rules``, which now reads
    the canonical ``(:IdentityRule)-[:LINKS_CONCEPT]->(:Concept)`` edge that
    onboarding writes (ADR-0011 F1, fixed in S1.2). We assert an IdentityRule-driven
    entry exists AND the resolve spans >=2 systems; the test skips cleanly on a
    single-system / credential-free lane.
    """
    systems = _systems_with_mappings()
    if len(systems) < 2:
        pytest.skip(f"{SINGLE_SYSTEM_SKIP} (onboarded systems: {sorted(systems)})")

    raw, resolution, unified, _correlations, _boundaries = resolve_fn(
        SHARED_CONCEPT, SHARED_IDENTIFIER
    )

    # IdentityRule-driven entries are the cross-system join under test; brute-force
    # entries are per-system fallbacks (tagged "brute-force") and do NOT prove an
    # IdentityRule cross-system link.
    identity_driven = [
        entry for entry in resolution if "brute-force" not in (entry.get("rule") or "")
    ]
    systems_touched = {item.get("title") for item in unified if item.get("title")}

    assert identity_driven, (
        "no IdentityRule-driven resolution entry: the resolver found no "
        "cross-system identity link for the shared concept "
        f"'{SHARED_CONCEPT}' — expected an "
        f"(:IdentityRule)-[:LINKS_CONCEPT]->(:Concept) join (ADR-0011 F1). raw={raw}"
    )
    assert len(systems_touched) >= 2, (
        f"cross-system resolve of '{SHARED_CONCEPT}'='{SHARED_IDENTIFIER}' did not "
        f"span >=2 systems: {sorted(systems_touched)}"
    )
