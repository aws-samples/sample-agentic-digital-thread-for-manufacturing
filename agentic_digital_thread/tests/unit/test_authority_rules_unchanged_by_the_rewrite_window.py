# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A telemetry-rich system's AuthorityRules do not depend on where its
emergent concepts are landed.

c4 (``_dp_authority_rules``) mints an AuthorityRule for each concept the
system's fields map when it runs. Before 2026-10-21 the emergent landing and
the retired-link sweep ran after the base provision, so c4 saw the concepts
b2 and b3a wrote: not an emergent concept b2 leaves out (one the graph's
vocabulary does not list yet), and still a link a steward retired (b2 writes
it back; the sweep removes it later). Landing and sweeping inside the
schema-rewrite window, before c4, must not change the emergent half of that.

The retired half changed on purpose on 2026-10-27: a concept whose only
field edge is a retired link no longer gets an AuthorityRule (an active one
wins routing, so it sent the concept's metrics to a system whose binding a
steward retired; ``test_authority_rule_retired_link.py``). Before that change
this file pinned the retired concept's rule as minted.

Self-contained (only names every version of ``agents.onboard`` since
2026-10-20 defines). Neutral fixture: one telemetry-rich system at one
plant, three mapped columns: a core concept, an emergent concept, and a core
concept whose link a steward retired.
"""
from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.catalog import CONCEPT_AUTHORITY, CORE_CONCEPTS  # noqa: E402
from agents.connection_types import MCP_SERVER  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402

SID, PLANT = "probe-hub", "Plant A"
#: Two core concepts no system class owns (c4 mints them for any mapping system).
KEPT, RETIRED = [c for c in sorted(CORE_CONCEPTS) if CONCEPT_AUTHORITY.get(c) is None][:2]
EMERGENT = "trace-key"
assert EMERGENT not in CORE_CONCEPTS


def _row(column, concept) -> dict:
    return {"table": "signals", "field": f"signals.{column}", "concept": concept,
            "confidence": 0.9, "mappings": [{"concept": concept, "role": "identifier",
                                             "confidence": 0.9}]}


MAPPINGS = [_row("kept_tag", KEPT), _row("emergent_tag", EMERGENT), _row("retired_tag", RETIRED)]
RETIRED_LINK = {"concept": RETIRED, "field": "signals.retired_tag"}


class _Graph:
    """Fields and their MAPS_TO edges, AuthorityRule MERGEs, and the reads c4
    and the retired-link sweep make. Every other read answers no rows and
    every other write succeeds."""

    def __init__(self):
        self.edges: dict = {}           # field id -> {concept}
        self.rules: set = set()         # AuthorityRule ids MERGEd

    def write(self, cypher, params=None):
        params = params or {}
        if cypher.startswith("MERGE (f:Field {id: $fid})"):
            self.edges.setdefault(params["fid"], set())
        elif "MERGE (f)-[r:MAPS_TO]->(c)" in cypher:
            self.edges.setdefault(params["fid"], set()).add(params["cid"])
        elif cypher == onboard._RETIRED_LINK_DELETE_CYPHER:
            self.edges.get(params["fid"], set()).discard(params["concept"])
        elif cypher == onboard._SCHEMA_SUBGRAPH_DELETE_CYPHER:
            self.edges.clear()
        elif cypher.startswith("MERGE (ar:AuthorityRule {id: $arid})"):
            self.rules.add(params["arid"])
        return True

    def query(self, cypher, params=None):
        params = params or {}
        if cypher == onboard._RETIRED_LINK_COUNT_CYPHER:
            return [{"n": int(params["concept"] in self.edges.get(params["fid"], set()))}]
        if re.search(r"RETURN DISTINCT c\.id AS concept", cypher) and "$sid" in cypher:
            return [{"concept": c} for c in sorted(set().union(*self.edges.values()))]
        if "AS fields" in cypher and "concepts" in params:   # c4's retired-link check
            return [{"concept": c, "fields": sorted(f for f, cs in self.edges.items() if c in cs)}
                    for c in params["concepts"]]
        return []


@pytest.fixture
def graph(monkeypatch):
    g = _Graph()
    for name in ("neptune_write", "_db_neptune_write"):
        monkeypatch.setattr(onboard, name, g.write, raising=False)
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)
    entry = {"id": SID, "name": SID, "schema": "", "connection_type": MCP_SERVER,
             "system_type": "Telemetry Hub", "plant": PLANT}
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: entry if sid == SID else None)
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links",
                        lambda sid: [dict(RETIRED_LINK)] if sid == SID else [])
    monkeypatch.setattr(onboard.self_learning, "accepted_user_concept_mappings", lambda _sid: {})
    monkeypatch.setattr(onboard, "_classify_table_entity", lambda *_a, **_k: {"entity": None})
    monkeypatch.setattr(onboard, "_delete_stale_onboarding_rule_artifacts", lambda _sid: None)
    # Every coordinator phase but b0, b2, c4 (and the window's own steps, when
    # the version has them): nothing else writes concepts or authority.
    for name in ("_dp_connection_phase", "_dp_discover_athena_physical",
                 "_dp_athena_physical_fields", "_dp_discovery_completeness",
                 "_dp_reapply_user_corrections", "_dp_identity_key_edges", "_dp_mint_unmapped",
                 "_dp_internal_joinpaths", "_dp_mirrored_measure_tags",
                 "_dp_valueoverlap_joinpaths", "_dp_samerow_joinpaths", "_dp_identity_rules",
                 "_dp_rule_joinpaths", "_dp_cross_system_vms", "_dp_equipment_vms_legacy",
                 "_dp_onboarding_log", "_dp_start_pg_agent", "_dp_finalize",
                 "_provision_knowledge_post_pass"):
        monkeypatch.setattr(onboard, name, lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(onboard, "_dp_collect_tables",
                        lambda ctx: ctx.tables_seen.update({"signals"}))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    monkeypatch.setattr("agents.asset_role_pass.run_after_provision", lambda: None)
    return g


def _provision(monkeypatch, vocabulary):
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: list(vocabulary))
    return onboard._do_provision_writes(ProvisionRequest(
        system_id=SID, system_name=SID, plant=PLANT, mappings=[dict(m) for m in MAPPINGS],
        connection_type=MCP_SERVER))


def _field_concepts(graph) -> dict:
    return {fid.split(".")[-1]: sorted(c) for fid, c in graph.edges.items()}


def test_a_first_provision_mints_authority_from_the_base_concepts_only(graph, monkeypatch):
    """The emergent concept is not in the graph's vocabulary yet: b2 leaves
    it out and the landing writes it. c4 mints for the core concept only: not
    for the emergent one, and not for the retired link b2 wrote back (changed
    2026-10-27, see the module docstring). The provision ends with the
    emergent link landed and the retired link swept."""
    result = _provision(monkeypatch, [KEPT, RETIRED])
    assert result.get("status") == "provisioned"
    assert graph.rules == {f"ar-{SID}-{KEPT}"}
    assert _field_concepts(graph) == {"kept_tag": [KEPT], "emergent_tag": [EMERGENT],
                                      "retired_tag": []}


def test_a_reprovision_mints_authority_for_an_emergent_concept_the_vocabulary_lists(
        graph, monkeypatch):
    """The emergent concept is in the graph's vocabulary (an earlier landing
    created it): b2 writes it, so c4 mints for it too, as it always did. The
    retired link still earns no rule."""
    result = _provision(monkeypatch, [KEPT, RETIRED, EMERGENT])
    assert result.get("status") == "provisioned"
    assert graph.rules == {f"ar-{SID}-{KEPT}", f"ar-{SID}-{EMERGENT}"}
    assert _field_concepts(graph) == {"kept_tag": [KEPT], "emergent_tag": [EMERGENT],
                                      "retired_tag": []}
