# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No-drift guard for the single concept seed source (ADR 0020).

Pure in-process unit tests — no AWS, no graph, no DB, no LLM, no fixtures, no
markers — runs offline in the default (unmarked) pytest lane. This is the
regression lock for the onboard.py:180 core-vs-emergent delete risk: every core
concept the shared seeder emits MUST carry non-null name + isa95_level + source='core'.
"""
from __future__ import annotations

from agents.catalog import (
    CONCEPT_AFFINITY,
    CONCEPT_AUTHORITY,
    CONCEPT_ISA95_LEVEL,
    CONCEPT_LAYER,
    CONCEPT_NAME,
    CORE_CONCEPT_DEFINITIONS,
    CORE_CONCEPTS,
    ENTITY_CONCEPT_IDS,
    TABLE_ENTITY_MAP,
    core_concept_merges,
)


def test_seed_idset_equals_core_definitions():
    """Emitted concept id-set == CORE_CONCEPT_DEFINITIONS exactly (no missing, no extras)."""
    seeded_ids = {p["id"] for _stmt, p in core_concept_merges()}
    assert seeded_ids == set(CORE_CONCEPT_DEFINITIONS)
    assert seeded_ids == set(CORE_CONCEPTS)
    # one emitted statement per concept (no dupes, no drops)
    # 65 = 61 original + actual-quantity + quality-yield-rate
    #    + target-machine-speed + inspection-result (new-metric-cards wave)
    assert len(core_concept_merges()) == len(CORE_CONCEPT_DEFINITIONS) == 65


def test_metadata_keysets_match_definitions():
    """The adjacent maps cover exactly the core concept id-set."""
    assert set(CONCEPT_NAME) == set(CORE_CONCEPT_DEFINITIONS)
    assert set(CONCEPT_ISA95_LEVEL) == set(CORE_CONCEPT_DEFINITIONS)


def test_seed_carries_required_metadata():
    """Every emitted Concept carries truthy name, integer isa95_level in 0-4, and the
    statement sets source='core' + name + isa95_level + description."""
    for stmt, p in core_concept_merges():
        assert isinstance(p["name"], str) and p["name"], f"empty name for {p['id']}"
        assert isinstance(p["isa95_level"], int), f"non-int isa95_level for {p['id']}"
        assert 0 <= p["isa95_level"] <= 4, f"isa95_level out of range for {p['id']}"
        assert isinstance(p["description"], str) and p["description"], f"empty desc for {p['id']}"
        assert "c.source = 'core'" in stmt
        assert "c.name =" in stmt
        assert "c.isa95_level =" in stmt
        assert "c.description =" in stmt


def test_seed_statement_is_single_merge():
    """One MERGE per concept, all using the same canonical template."""
    statements = {stmt for stmt, _ in core_concept_merges()}
    assert len(statements) == 1
    only = next(iter(statements))
    assert only.startswith("MERGE (c:Concept {id: $id})")


def test_catalog_has_no_module_level_runtime_dep_import():
    """The core-vocabulary seed path (``deploy.sh`` [2/6]) imports ``agents.catalog`` in the
    CDK-only venv, which lacks app runtime deps (pg8000). Guard against re-adding a
    module-level ``from .db import ...`` (or ``import pg8000`` / ``from . import pgdriver``) that re-breaks the one-script
    deploy seed — regression fixed 2026-06-16 (ADR 0020). neptune_query must be imported lazily
    inside the functions that use it.
    """
    import ast
    import pathlib

    catalog_src = pathlib.Path(__file__).resolve().parents[2] / "agents" / "catalog.py"
    tree = ast.parse(catalog_src.read_text())
    offenders = []
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.ImportFrom) and node.module == "db":
            offenders.append(f"from .db import ... (line {node.lineno})")
        if isinstance(node, ast.ImportFrom) and (
                node.module == "pgdriver" or any(a.name == "pgdriver" for a in node.names)):
            offenders.append(f"pgdriver import (line {node.lineno})")
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in ("pg8000", "pgdriver")
                                                for a in node.names):
            offenders.append(f"import pg8000/pgdriver (line {node.lineno})")
    assert not offenders, (
        "catalog.py must not import .db / pgdriver / pg8000 at MODULE level (breaks the deploy-time "
        f"seed in the CDK-only venv); import lazily inside functions instead. Found: {offenders}"
    )


# ── S5.2 Wave 1: two-layer concept tagging (ADR 0021) ───────────────────────

_NEW_ENTITIES = ("asset", "sales-order", "shipment")


def test_every_core_concept_is_layer_tagged():
    """CONCEPT_LAYER tags exactly the core concept id-set, each value entity|field."""
    assert set(CONCEPT_LAYER) == set(CORE_CONCEPT_DEFINITIONS)
    assert all(v in ("entity", "field") for v in CONCEPT_LAYER.values())


def test_new_entities_present_with_metadata():
    """asset/sales-order/shipment exist with name+isa95+authority+affinity; authority ⊆ affinity."""
    for cid in _NEW_ENTITIES:
        assert cid in CORE_CONCEPT_DEFINITIONS, cid
        assert CONCEPT_NAME.get(cid), cid
        assert isinstance(CONCEPT_ISA95_LEVEL.get(cid), int), cid
        auth = CONCEPT_AUTHORITY.get(cid)
        assert auth, f"{cid} has no authority"
        assert cid in CONCEPT_AFFINITY[auth], f"{cid} authority {auth} not in its affinity set"
        assert CONCEPT_LAYER[cid] == "entity", cid


def test_table_entity_map_targets_are_entities():
    """Every TABLE_ENTITY_MAP target is an entity-capable concept (incl. dimension dual-roles)."""
    assert TABLE_ENTITY_MAP, "TABLE_ENTITY_MAP must be non-empty"
    for (schema, table), entity in TABLE_ENTITY_MAP.items():
        assert entity in ENTITY_CONCEPT_IDS, f"{schema}.{table} -> {entity} not entity-capable"


def test_dimension_concepts_entity_capable():
    """production-line/location/site stay field-tagged (rendering) but are entity-capable."""
    for cid in ("production-line", "location", "site"):
        assert CONCEPT_LAYER[cid] == "field", cid
        assert cid in ENTITY_CONCEPT_IDS, cid
