# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A link a steward retired is no evidence for an AuthorityRule.

c4 (``_dp_authority_rules``) mints an AuthorityRule for each concept the
system's fields map. b2 may write a retired concept link back before the
retired-link sweep removes it, and c4's concept set was read before the
sweep, so a concept whose ONLY field edge was a retired link got (or kept) an
active AuthorityRule. An active AuthorityRule wins routing, so a later metric
for that concept went to a system whose binding was retired.

The rule now: c4 counts only field edges that are not retired links. A
concept that a live column still maps keeps its rule. A concept supported by
retired links alone gets none, and an earlier rule for it is deactivated as
an evidence orphan, whether the sweep succeeded or not. When the read that
would show other evidence fails, the concept is left out (fail closed).

Neutral fixture: one telemetry-rich hub at one plant (MCP server), columns
on a ``signals`` table, concepts no system class owns.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.catalog import CONCEPT_AUTHORITY, CORE_CONCEPTS  # noqa: E402
from agents.connection_types import MCP_SERVER  # noqa: E402
from agents.db import GraphQueryError  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402

SID, PLANT = "signal-hub", "Plant North"
#: Three core concepts no system class owns (c4 mints them for any mapping system).
KEPT, RETIRED, SHARED = [c for c in sorted(CORE_CONCEPTS) if CONCEPT_AUTHORITY.get(c) is None][:3]


def _row(column, concept) -> dict:
    return {"table": "signals", "field": f"signals.{column}", "concept": concept,
            "confidence": 0.9, "mappings": [{"concept": concept, "role": "identifier",
                                             "confidence": 0.9}]}


#: ``SHARED`` is mapped on two columns; a steward retired one of them.
MAPPINGS = [_row("kept_tag", KEPT), _row("retired_tag", RETIRED),
            _row("shared_old", SHARED), _row("shared_new", SHARED)]
RETIRED_LINKS = [{"concept": RETIRED, "field": "signals.retired_tag"},
                 {"concept": SHARED, "field": "signals.shared_old"}]


class _Graph:
    """Fields and their MAPS_TO edges, AuthorityRule MERGEs, the evidence-orphan
    deactivation c4 sends, and the reads c4 and the retired-link sweep make.
    Every other read answers no rows and every other write succeeds."""

    def __init__(self):
        self.edges: dict = {}           # field id -> {concept}
        self.rules: set = set()         # AuthorityRule ids MERGEd
        self.orphan_scope: list = []    # $concepts of each evidence-orphan deactivation
        self.refuse_sweep = False       # the retired-link delete is refused
        self.fail_evidence_read = False

    def write(self, cypher, params=None):
        params = params or {}
        if cypher.startswith("MERGE (f:Field {id: $fid})"):
            self.edges.setdefault(params["fid"], set())
        elif "MERGE (f)-[r:MAPS_TO]->(c)" in cypher:
            self.edges.setdefault(params["fid"], set()).add(params["cid"])
        elif cypher == onboard._RETIRED_LINK_DELETE_CYPHER:
            if self.refuse_sweep:
                return False
            self.edges.get(params["fid"], set()).discard(params["concept"])
        elif cypher == onboard._SCHEMA_SUBGRAPH_DELETE_CYPHER:
            self.edges.clear()
        elif cypher.startswith("MERGE (ar:AuthorityRule {id: $arid})"):
            self.rules.add(params["arid"])
        elif ("ar.integrity_flag = 'evidence-orphan'" in cypher
              and "ar.status = 'deactivated'" in cypher):
            self.orphan_scope.append(sorted(params["concepts"]))
        return True

    def query(self, cypher, params=None):
        params = params or {}
        if cypher == onboard._RETIRED_LINK_COUNT_CYPHER:
            return [{"n": int(params["concept"] in self.edges.get(params["fid"], set()))}]
        if "RETURN DISTINCT c.id AS concept" in cypher and "$sid" in cypher:
            return [{"concept": c} for c in sorted(set().union(*self.edges.values()))]
        if "AS fields" in cypher and "concepts" in params:   # the fields carrying each concept
            if self.fail_evidence_read:
                raise GraphQueryError("ServiceUnavailableException", "graph unavailable")
            return [{"concept": c, "fields": sorted(f for f, cs in self.edges.items() if c in cs)}
                    for c in params["concepts"]
                    if any(c in cs for cs in self.edges.values())]
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
                        lambda sid: [dict(r) for r in RETIRED_LINKS] if sid == SID else [])
    monkeypatch.setattr(onboard.self_learning, "accepted_user_concept_mappings", lambda _sid: {})
    monkeypatch.setattr(onboard, "_classify_table_entity", lambda *_a, **_k: {"entity": None})
    monkeypatch.setattr(onboard, "_delete_stale_onboarding_rule_artifacts", lambda _sid: None)
    monkeypatch.setattr(onboard, "_settle_retry_sleep", lambda _s: None, raising=False)
    # Every coordinator phase but b0, b2, b3b and c4: nothing else writes
    # concepts or authority.
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
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [KEPT, RETIRED, SHARED])
    return g


def _provision():
    return onboard._do_provision_writes(ProvisionRequest(
        system_id=SID, system_name=SID, plant=PLANT, mappings=[dict(m) for m in MAPPINGS],
        connection_type=MCP_SERVER))


def _rule(concept) -> str:
    return f"ar-{SID}-{concept}"


def test_a_concept_mapped_only_by_a_retired_link_gets_no_authority_rule(graph):
    """b2 writes the retired links back and the sweep removes them; c4 mints
    for the live concepts only, and the evidence-orphan deactivation covers
    the retired one (an earlier rule for it is switched off). Retiring one of
    two links of a concept leaves the other as its evidence, so that concept
    keeps its rule."""
    assert _provision().get("status") == "provisioned"
    assert graph.rules == {_rule(KEPT), _rule(SHARED)}
    assert graph.orphan_scope == [sorted([KEPT, SHARED])]
    assert graph.edges[f"{SID}.signals.retired_tag"] == set()
    assert graph.edges[f"{SID}.signals.shared_new"] == {SHARED}


def test_a_failed_sweep_still_mints_nothing_from_the_retired_link(graph):
    """The sweep cannot remove the retired link, so the provision fails
    (retired_link_sweep_failed), and c4, which runs before that verdict, still
    counts the link for nothing."""
    graph.refuse_sweep = True
    result = _provision()
    assert result.get("reason") == "retired_link_sweep_failed"
    assert graph.edges[f"{SID}.signals.retired_tag"] == {RETIRED}     # still in the graph
    assert _rule(RETIRED) not in graph.rules
    assert graph.rules == {_rule(KEPT), _rule(SHARED)}


def test_an_unreadable_evidence_check_leaves_the_concepts_out(graph):
    """The read that would show other evidence fails: every concept with a
    retired link is left out (no rule, and an earlier one is deactivated);
    a concept with no retired link is unaffected."""
    graph.fail_evidence_read = True
    _provision()
    assert graph.rules == {_rule(KEPT)}
    assert graph.orphan_scope == [[KEPT]]
