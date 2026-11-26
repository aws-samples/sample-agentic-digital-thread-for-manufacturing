# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Grounding guardrail (§7) for the onboarding agent.

Observed live behavior: the onboarding agent hallucinated a fixed menu of five
vendors (SAP / Plex / Maximo / Windchill / Ignition) when no systems were
connected. Task 1D grounds the agent so this is impossible:

- ``get_catalog()`` cleanly returns ``{'systems': []}`` for an empty catalog, so
  the agent has an unambiguous "nothing connected" signal.
- ``ONBOARD_AGENT_PROMPT`` carries an explicit rule to report ONLY the systems
  the tool returns, say "no systems connected yet" on empty, and never
  example-list vendors — and the prompt text itself names none of the observed
  hallucinated vendors (naming them would prime the very output we forbid).
"""
from unittest.mock import MagicMock, patch

from agents.onboard_agent import get_catalog, ONBOARD_AGENT_PROMPT

# The exact vendor names observed in the live hallucination (guardrail §7).
_HALLUCINATED_VENDORS = ("SAP", "Plex", "Maximo", "Windchill", "Ignition")


def test_get_catalog_empty_returns_empty_systems():
    """An empty catalog yields the clean, unambiguous ``{'systems': []}`` shape."""
    cat = MagicMock()
    cat.get_all.return_value = []
    with patch("agents.onboard_agent.catalog", cat):
        result = get_catalog()
    assert result == {"systems": []}


def test_prompt_has_empty_catalog_guidance():
    """The prompt tells the agent what to say when nothing is connected."""
    assert "no systems connected yet" in ONBOARD_AGENT_PROMPT


def test_prompt_names_no_hallucinated_vendors():
    """The prompt must not seed any of the observed hallucinated vendor names."""
    for vendor in _HALLUCINATED_VENDORS:
        assert vendor not in ONBOARD_AGENT_PROMPT, (
            f"ONBOARD_AGENT_PROMPT must not name vendor {vendor!r} — naming it "
            "primes the hallucination guardrail §7 forbids."
        )


# ── Stateless-runtime catalog hydration (2026-07-22) ─────────────────────────
# The onboarding tools also run in the stateless adt_onboarding AgentCore
# runtime, which never executes the ECS app's startup restore — get_catalog
# reported "empty" while six systems were onboarded. An empty catalog now
# lazily hydrates from the graph's :System nodes (best-effort).

def test_get_catalog_hydrates_from_graph_when_empty():
    import agents.onboard_agent as oa
    from agents.catalog import SystemCatalog
    cat = SystemCatalog([])
    rows = [
        {"id": "erp", "name": "ERP", "type": "ERP", "plant": "All Sites",
         "connection_type": "PostgreSQL", "schema": "erp", "status": "onboarded"},
        {"id": "cmms", "name": "CMMS", "type": "CMMS", "plant": "All Sites",
         "connection_type": "PostgreSQL", "schema": "cmms_maintenance",
         "status": "onboarded"},
    ]
    import agents.catalog as cat_mod
    with patch.object(oa, "catalog", cat), \
            patch.object(cat_mod, "catalog", cat), \
            patch.object(cat, "_graph_rows", return_value=rows):
        result = oa.get_catalog()
    ids = {s["id"] for s in result["systems"]}
    assert ids == {"erp", "cmms"}
    assert all(s["status"] == "onboarded" for s in result["systems"])


def test_get_catalog_hydration_failure_stays_empty():
    """No graph access (e.g. unit env) → the empty contract is preserved."""
    import agents.onboard_agent as oa
    from agents.catalog import SystemCatalog
    cat = SystemCatalog([])
    import agents.catalog as cat_mod
    with patch.object(oa, "catalog", cat), \
            patch.object(cat_mod, "catalog", cat), \
            patch.object(cat, "_graph_rows", side_effect=RuntimeError("no vpc")):
        result = oa.get_catalog()
    assert result == {"systems": []}


def test_get_catalog_does_not_rehydrate_populated_catalog():
    """A hydrated/app-process catalog never re-queries the graph."""
    import agents.onboard_agent as oa
    from agents.catalog import SystemCatalog
    cat = SystemCatalog([{"id": "erp", "name": "ERP", "type": "ERP",
                          "plant": "", "status": "onboarded",
                          "connection_type": "PostgreSQL"}])
    nq = MagicMock()
    with patch.object(oa, "catalog", cat), patch.object(oa, "neptune_query", nq):
        result = oa.get_catalog()
    nq.assert_not_called()
    assert [s["id"] for s in result["systems"]] == ["erp"]
