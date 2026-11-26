# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mode-A deterministic route seed and routing prompt contracts.

Offline: graph reads use an injected fake query; no AWS, Neptune, gateway, or LLM.
All fixtures are synthetic and generic.
"""
from __future__ import annotations

import pytest

import json

import agents.orchestrator as orch
from agents.catalog import REMOVING_SYSTEMS_READ


def _payload(seed: str) -> dict:
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        if seed.startswith(header):
            return json.loads(seed[len(header):])
    raise AssertionError("route seed has no recognized header")


def test_route_seed_is_one_bounded_relationship_traversal_and_schema_free():
    calls: list[str] = []

    def fake_query(cypher: str):
        calls.append(cypher)
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [
                {
                    "system_id": "sys-a", "system_name": "System A",
                    "plants": ["site-1"], "capabilities": "maintenance events",
                    "connection_type": "PostgreSQL",
                    "query_guidance": "Ask for bounded current-state facts.",
                },
                {
                    "system_id": "sys-b", "system_name": "System B",
                    "plants": ["site-2"], "capabilities": "production events",
                    "connection_type": "MCP Server",
                    "query_guidance": "Ask intent-level questions.",
                },
            ]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return [{
                "id": "ar-sys-a-machine-state", "concept": "machine-state",
                "system_id": "sys-a", "method": "user_confirmed",
                "confidence": 1.0, "status": "must-not-leak-status",
            }]
        return [{
            "system_id": "sys-a",
            "shared_concepts": [{"concept": "asset.identity", "peer_system": "sys-b"}],
            "join_paths": [{
                "id": "join-a-b", "from_system": "sys-a", "from_field": "asset_ref",
                "to_system": "sys-b", "to_field": "equipment_ref",
                "transform": "normalize", "confidence": 0.9,
            }],
            "value_mappings": [{
                "from_system": "sys-a", "from_field": "asset_ref",
                "to_system": "sys-b", "to_field": "equipment_ref",
                "concept": "asset.identity", "method": "accepted_crosswalk",
                "confidence": 0.8, "from_value": "must-not-leak",
                "to_value": "must-not-leak",
                "value_pairs": [{"from": "A-1", "to": "E-1"}], "value_pair_count": 1,
            }],
            "identity_rules": [{
                "id": "identity-a-b", "concept": "asset.identity",
                "source_system": "sys-a", "source_field": "asset_ref",
                "target_system": "sys-b", "target_field": "equipment_ref",
                "transform": "normalize", "confidence": 0.85,
            }],
            "routed_system_schemas": {"must": "not leak"},
        }]

    seed = orch.build_route_seed(query=fake_query)
    assert seed is not None
    # The last read re-checks the removal marker after the seed's own reads.
    assert calls == [orch._ROUTE_SEED_INVENTORY_CYPHER, orch._ROUTE_SEED_CYPHER,
                     orch._ROUTE_SEED_AUTHORITY_CYPHER, REMOVING_SYSTEMS_READ]
    cypher = calls[1]
    assert "[:HAS_SCHEMA]" in cypher and "[:MAPS_TO]" in cypher
    assert "[:FROM_SYSTEM|TO_SYSTEM]" in cypher
    assert "[:LINKS_CONCEPT]" in cypher
    assert f"[0..{orch._ROUTE_SEED_RULE_CAP}]" in cypher
    assert "LIMIT" not in cypher
    # Row-level values enter only as a value mapping's own bounded value
    # pairs (a value mapping IS its values); no other row value is read.
    assert cypher.count("from_value") == 3 and cypher.count("to_value") == 3
    assert f"vpairs[0..{orch._ROUTE_SEED_VALUE_PAIR_CAP}]" in cypher
    # T1.6 authority read: canonical PROPERTY read (ADR-0011 F3), excluding
    # soft-deactivated rules (T1.4 lifecycle) — never row-level values.
    authority_cypher = calls[2]
    assert "AuthorityRule" in authority_cypher
    assert "coalesce(ar.status, '') <> 'deactivated'" in authority_cypher
    assert "APPLIES_TO" not in authority_cypher  # property read, not edges
    assert "from_value" not in authority_cypher and "to_value" not in authority_cypher

    payload = _payload(seed)
    assert payload["rule_grain_cap_per_kind"] == 50
    systems = {system["id"]: system for system in payload["systems"]}
    assert set(systems) == {"sys-a", "sys-b"}
    assert systems["sys-a"]["plants"] == ["site-1"]
    assert systems["sys-a"]["capabilities"] == "maintenance events"
    assert systems["sys-a"]["shared_concepts"] == [
        {"concept": "asset.identity", "peer_system": "sys-b"}
    ]
    rules = systems["sys-a"]["rules"]
    assert rules["join_paths"][0]["transform"] == "normalize"
    assert rules["value_mappings"][0]["concept"] == "asset.identity"
    assert rules["identity_rules"][0]["target_system"] == "sys-b"
    # T1.6: authority rules fold under their owning system through the
    # explicit allowlist (id/concept/method/confidence — no status, no
    # system_id echo); every OTHER system carries the key EMPTY (the
    # consulted marker planned_with counts from).
    assert rules["authority_rules"] == [{
        "id": "ar-sys-a-machine-state", "concept": "machine-state",
        "method": "user_confirmed", "confidence": 1.0,
    }]
    assert systems["sys-b"]["rules"]["authority_rules"] == []

    serialized = json.dumps(payload)
    for forbidden in (
        "from_value",
        "to_value",
        "routed_system_schemas",
        "tables",
        "columns",
        "must-not-leak",
        "must_not_leak",
    ):
        assert forbidden not in serialized


def test_route_seed_ranks_and_caps_rule_grains():
    rules = [{
        "id": f"join-{index}",
        "from_system": "sys-a",
        "to_system": "sys-b",
        "confidence": index / 100,
    } for index in range(60)]

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "System A"}]
        return [{"system_id": "sys-a", "join_paths": rules}]

    seed = orch.build_route_seed(query=fake_query)
    capped = _payload(seed)["systems"][0]["rules"]["join_paths"]
    assert len(capped) == 50
    assert capped[0]["id"] == "join-59"
    assert capped[-1]["id"] == "join-10"


def test_rule_heavy_system_cannot_evict_another_system_from_inventory():
    rules = [{"id": f"join-{index}", "confidence": 1 - index / 100}
             for index in range(60)]

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [
                {"system_id": "rule-heavy", "system_name": "Rule Heavy"},
                {"system_id": "ruleless", "system_name": "Ruleless"},
            ]
        return [{"system_id": "rule-heavy", "join_paths": rules}]

    payload = _payload(orch.build_route_seed(query=fake_query))
    assert {system["id"] for system in payload["systems"]} == {
        "rule-heavy", "ruleless",
    }
    heavy = next(system for system in payload["systems"]
                 if system["id"] == "rule-heavy")
    assert len(heavy["rules"]["join_paths"]) == 50


def test_route_seed_truncates_oversized_guidance_with_disclosure_and_budget():
    oversized = "route using bounded facts " * 1000

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{
                "system_id": "sys-a",
                "capabilities": oversized,
                "query_guidance": oversized,
            }]
        return [{"system_id": "sys-a"}]

    seed = orch.build_route_seed(query=fake_query)
    payload = _payload(seed)
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert payload["truncated"] is True
    system = payload["systems"][0]
    assert system["capabilities"].endswith("… [truncated]")
    # Guidance is a FENCED section, so the disclosure marker sits inside the
    # body: the closing fence must stay last (a head truncation severed it).
    assert "… [truncated]" in system["query_guidance"]
    assert system["query_guidance"].rstrip().endswith("----- END QUERY HINTS -----")


def test_route_seed_large_inventory_compacts_without_losing_system_ids():
    expected_ids = [f"system-{index:02d}" for index in range(80)]
    inventory = [
        {
            "system_id": system_id,
            "system_name": f"System {index} " + "N" * 1000,
            "plants": [f"Plant {index} " + "P" * 1000],
            "connection_type": "PostgreSQL",
        }
        for index, system_id in enumerate(expected_ids)
    ]

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        return []

    seed = orch.build_route_seed(query=fake_query)
    payload = _payload(seed)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert payload["truncated"] is True
    assert [system["id"] for system in payload["systems"]] == expected_ids
    assert all(set(system) == {"id"} for system in payload["systems"])


def test_route_seed_is_fail_safe_for_empty_or_failed_graph():
    assert orch.build_route_seed(query=lambda _cypher: []) is None

    def fail(_cypher: str):
        raise RuntimeError("graph unavailable")

    assert orch.build_route_seed(query=fail) is None


# --------------------------------------------------------------------------- #
# R3 — provenance-tier ordering: user_confirmed > accepted proposal >
# onboarding-LLM, applied BEFORE confidence in both the Cypher and the
# Python ranking/budget trim. Steward assertions can never be displaced by
# LLM guesses.
# --------------------------------------------------------------------------- #
def test_user_confirmed_rules_survive_cap():
    # The audited displacement defect: CAP+ LLM rules, every one of them with
    # higher confidence than the single user_confirmed row — the user row
    # must still be present (and ranked first) after the per-kind cap.
    llm_rules = [{
        "id": f"join-llm-{index:02d}", "from_system": "sys-a",
        "to_system": "sys-b", "confidence": 0.99 - index / 1000,
    } for index in range(60)]
    user_rule = {
        "id": "join-user", "from_system": "sys-a", "to_system": "sys-b",
        "confidence": 0.01, "method": "user_confirmed",
    }

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "System A"}]
        return [{"system_id": "sys-a", "join_paths": llm_rules + [user_rule]}]

    seed = orch.build_route_seed(query=fake_query)
    assert "_tier" not in seed  # internal trim key never serialized
    capped = _payload(seed)["systems"][0]["rules"]["join_paths"]
    assert len(capped) == 50
    assert capped[0]["id"] == "join-user"  # tier 0 outranks every LLM row
    # Within the LLM tier, confidence ordering is preserved.
    assert capped[1]["id"] == "join-llm-00"


def test_accepted_proposal_tier_outranks_onboarding_llm():
    rules = [
        {"id": "rule-llm", "from_system": "sys-a", "to_system": "sys-b",
         "confidence": 0.99},
        {"id": "rule-accepted", "from_system": "sys-a", "to_system": "sys-b",
         "confidence": 0.10, "provenance": "orchestrator"},
    ]

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a"}]
        return [{"system_id": "sys-a", "identity_rules": rules}]

    capped = _payload(orch.build_route_seed(query=fake_query))[
        "systems"][0]["rules"]["identity_rules"]
    assert [rule["id"] for rule in capped] == ["rule-accepted", "rule-llm"]


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_budget_trim_drops_llm_tier_first():
    # Under budget pressure the trim must pop tier-2 (onboarding-LLM) rows
    # first — the low-confidence user_confirmed rule survives truncation.
    user_rule = {"id": "user-rule", "confidence": 0.05, "_tier": 0}
    llm_rules = [
        {"id": f"llm-{index:03d}-" + "x" * 120, "confidence": 0.9, "_tier": 2}
        for index in range(80)
    ]
    payload = {
        "systems": [{
            "id": "sys-a", "name": "System A", "plants": [],
            "capabilities": None, "connection_type": None,
            "query_guidance": None, "shared_concepts": [],
            "rules": {"join_paths": [user_rule] + llm_rules,
                      "value_mappings": [], "identity_rules": []},
        }],
        "rule_grain_cap_per_kind": 50,
    }
    serialized = orch._serialize_route_seed(payload)
    assert len(orch._ROUTE_SEED_HEADER) + len(serialized) <= (
        orch._ROUTE_SEED_SERIALIZED_BUDGET)
    assert "user-rule" in serialized  # tier 0 survived the trim
    assert "_tier" not in serialized
    kept = payload["systems"][0]["rules"]["join_paths"]
    assert any(rule.get("id") == "user-rule" for rule in kept)
    assert len(kept) < 81  # LLM-tier rows were the ones dropped


def test_route_seed_cypher_orders_by_provenance_tier_before_confidence():
    import re

    cypher = orch._ROUTE_SEED_CYPHER
    # Value mappings rank AFTER they are grouped per field pair, on the grouped
    # columns: vmethod / vconf, and vprov (the group's provenance or origin).
    column = {"vmethod": ("vmethod", "vconf", "vprov IS NOT NULL")}
    tier_orderings = list(re.finditer(r"ORDER BY CASE WHEN (\w+?)(?:\.method| IN)", cypher))
    assert [match.group(1) for match in tier_orderings] == ["jp", "vmethod", "ir"]
    for match in tier_orderings:
        var = match.group(1)
        method, conf, provenance_test = column.get(var, (
            f"{var}.method", f"{var}.confidence",
            f"{var}.provenance IS NOT NULL OR {var}.origin_context IS NOT NULL"))
        clause = cypher[match.start():match.start() + 340]
        assert f"{method} IN ['user_confirmed', 'crosswalk_confirmed']" in clause
        # The onboarding stamp sets method AND origin_context —
        # the method branch must classify tier 2 BEFORE the provenance branch,
        # or onboarding-LLM rules tie tier 1 with accepted proposals.
        onboarding = clause.index(f"WHEN {method} = 'onboarding-llm' THEN 2")
        provenance = clause.index(provenance_test)
        assert onboarding < provenance
        # Confidence stays the SECOND sort key, after the tier CASE.
        assert f"coalesce({conf}, 0) DESC" in clause
    # The grouped provenance is the first of the group's provenance or origin.
    assert "head(collect(coalesce(vm.provenance, vm.origin_context))) AS vprov" in cypher


def test_stamped_onboarding_rule_ranks_below_accepted_proposal():
    """Python mirror: _stamp_identity_rule_provenance stamps
    method='onboarding-llm' AND origin_context on every provisioned rule; the
    origin_context must not lift it into tier 1 — an accepted agent proposal
    (provenance-carrying) outranks it even at lower confidence."""
    onboarding_rule = {
        "id": "ir-sys-abc123", "method": "onboarding-llm",
        "origin_context": "approved at the onboarding Identity Rules checkpoint",
        "confidence": 0.95,
    }
    accepted_proposal = {"id": "vm-1", "provenance": "agent-proposal",
                         "confidence": 0.5}
    assert orch._provenance_tier(onboarding_rule) == 2
    assert orch._provenance_tier(accepted_proposal) == 1
    ranked = sorted([onboarding_rule, accepted_proposal],
                    key=orch._rule_sort_key)
    assert ranked[0] is accepted_proposal


def test_route_seed_omits_overflowing_inventory_with_partial_disclosure(caplog):
    inventory = [
        {"system_id": f"system-{index:04d}-" + "x" * 24}
        for index in range(1000)
    ]

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        return []

    with caplog.at_level("WARNING", logger=orch.__name__):
        seed = orch.build_route_seed(query=fake_query)
    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert seed.startswith(orch._ROUTE_SEED_PARTIAL_HEADER)
    header = orch._ROUTE_SEED_PARTIAL_HEADER.lower()
    assert "partial" in header
    assert "explore_graph" in header
    # A real compact prefix, not the empty emergency seed: the header is the
    # one the compacted payload needs, chosen after it was compacted.
    payload = _payload(seed)
    ids = [system["id"] for system in payload["systems"]]
    assert ids and ids == [row["system_id"] for row in inventory[:len(ids)]]
    assert payload["systems_omitted"] == len(inventory) - len(ids)
    assert "size invariant failed" not in caplog.text

    payload = _payload(seed)
    assert payload["truncated"] is True
    assert payload["systems_omitted"] > 0
    assert len(payload["systems"]) + payload["systems_omitted"] == 1000


def test_route_seed_truncates_pathological_system_id_with_marker():
    pathological_id = "system-" + "x" * 9000

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": pathological_id}]
        return []

    seed = orch.build_route_seed(query=fake_query)
    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    payload = _payload(seed)
    assert payload["truncated"] is True
    bounded_id = payload["systems"][0]["id"]
    assert len(bounded_id) <= orch._ROUTE_SEED_ID_FIELD_CAP
    assert bounded_id.endswith(orch._ROUTE_SEED_ID_TRUNCATION_MARKER)


def test_route_seed_complete_inventory_keeps_skip_inventory_instruction():
    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "System A"}]
        return []

    seed = orch.build_route_seed(query=fake_query)
    assert seed is not None
    assert seed.startswith(orch._ROUTE_SEED_HEADER)
    assert "Do NOT re-run the systems inventory" in orch._ROUTE_SEED_HEADER
    payload = _payload(seed)
    assert payload["truncated"] is False
    assert "systems_omitted" not in payload


def test_route_seed_omits_emergency_seed_when_budget_is_below_its_floor(
    monkeypatch, caplog,
):
    monkeypatch.setattr(
        orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(orch._ROUTE_SEED_PARTIAL_HEADER) - 1
    )

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a"}]
        return []

    assert orch.build_route_seed(query=fake_query) is None
    assert "Emergency route seed cannot fit configured budget" in caplog.text


def test_step2_uses_seed_or_one_batched_relationship_fallback():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        lower = prompt.lower()
        assert "do not re-run the inventory" in lower
        assert "one batched" in lower
        assert "relationship-bearing traversal" in lower
        assert "MATCH (s:System) RETURN s" not in prompt
        assert orch._ROUTE_SEED_CYPHER in prompt
        assert "[:MAPS_TO]" in prompt
        assert "[:FROM_SYSTEM|TO_SYSTEM]" in prompt
        assert "[:LINKS_CONCEPT]" in prompt


def test_mode_a_delegation_requires_structured_route_block():
    prompt = orch._build_orchestrator_prompt("agent")
    assert "```ROUTE:" in prompt
    assert "target_system: <exact system id>" in prompt
    assert "translated_identifiers:" in prompt
    assert "canonical_concept:" in prompt
    assert "scope:" in prompt
    assert "one-string `question` contract" in prompt
    assert "EVERY per-system question string" in prompt


def test_value_mapping_proposals_are_capped_but_not_removed():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        lower = prompt.lower()
        assert "value-mapping-proposal" in lower
        assert "at most 5" in lower
        assert "highest-value" in lower
        assert "novel, reusable, and evidence-bearing" in lower
        assert "additional_candidate_pairs_observed: n" in lower
        assert "emit 45 blocks" not in lower
        assert "one block per mapping" not in lower


# --------------------------------------------------------------------------- #
# T1.6 — authority rules in the route seed: the count planned_with reports is
# what was REALLY loaded (never a re-query); the read fails soft on its own
# (authority unknown ⇒ key absent everywhere — honest null, never 0).
# --------------------------------------------------------------------------- #
def _inventory_only(system_ids: "list[str]"):
    return [{"system_id": sid, "system_name": sid} for sid in system_ids]


def test_authority_read_failure_leaves_seed_without_the_key():
    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _inventory_only(["sys-a"])
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            raise RuntimeError("authority read unavailable")
        return []

    seed = orch.build_route_seed(query=fake_query)
    assert seed is not None  # the seed itself survives (fail-soft, not fail-safe)
    # Absent == empty: with no authority marker and no rules the empty
    # ``rules`` block itself is omitted from the serialized seed.
    rules = _payload(seed)["systems"][0].get("rules", {})
    assert "authority_rules" not in rules
    # End-to-end honest null: the runtime parser reports authority UNKNOWN.
    from agents import runtime_app
    assert "authority_rules" not in runtime_app._planned_with_from_seed(seed)


def test_authority_rules_capped_at_the_existing_rule_cap():
    rows = [{"id": f"ar-sys-a-c{index:03d}", "concept": f"concept-{index:03d}",
             "system_id": "sys-a", "confidence": index / 100}
            for index in range(60)]

    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _inventory_only(["sys-a"])
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return rows
        return []

    payload = _payload(orch.build_route_seed(query=fake_query))
    capped = payload["systems"][0]["rules"]["authority_rules"]
    assert len(capped) == orch._ROUTE_SEED_RULE_CAP == 50


def test_built_seed_flows_authority_count_into_planned_with():
    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _inventory_only(["sys-a", "sys-b"])
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return [
                {"id": "ar-sys-a-state", "concept": "machine-state",
                 "system_id": "sys-a"},
                {"id": "ar-sys-b-downtime", "concept": "downtime",
                 "system_id": "sys-b"},
                {"id": "ar-orphan", "concept": "orphan",
                 "system_id": "unknown-system"},  # dropped: not in the seed
            ]
        return []

    from agents import runtime_app
    planned = runtime_app._planned_with_from_seed(
        orch.build_route_seed(query=fake_query))
    # Loaded-at-planning semantics: only rules that made it INTO the seed
    # count — the orphan never loaded, so it is honestly not counted.
    assert planned["authority_rules"] == 2


def test_confirmed_rule_survives_when_system_prose_alone_exceeds_the_budget():
    """Event-15 shape: ten connected systems whose guidance prose alone blows
    the seed budget. The old pass order (all rules, then prose) dropped the
    steward-confirmed identity rule while keeping generic connection-type
    boilerplate — the orchestrator then reported 'no cross-system identity
    mapping exists' for a rule that was in the graph. Prose shrinks first."""
    confirmed = {"id": "ir-learned-batch", "confidence": 0.99, "_tier": 0,
                 "source_system": "mes-ind", "target_system": "sys-b",
                 "source_pattern": "B-{id}", "target_pattern": "LOT-2026-0{id}"}
    evidence = {"id": "ir-affinity-lot", "confidence": 0.85, "_tier": 1,
                "source_system": "sys-b", "target_system": "mes-ind",
                "source_pattern": "LOT-2026-0{id}", "target_pattern": "B-{id}"}
    llm = {"id": "ir-llm-guess", "confidence": 0.9, "_tier": 2}
    systems = []
    for index in range(10):
        systems.append({
            "id": f"sys-{index}", "name": f"System {index}", "plants": [],
            "capabilities": "capabilities " * 40, "connection_type": None,
            "query_guidance": f"generic guidance {index} " * 60,
            "answer_requirements": None, "legacy_guidance": None,
            "shared_concepts": [{"id": f"concept-{index}"}],
            "rules": {"join_paths": [], "value_mappings": [],
                      "identity_rules": ([dict(confirmed), dict(evidence), dict(llm)]
                                         if index in (1, 2) else [dict(llm)])},
        })
    payload = {"systems": systems, "rule_grain_cap_per_kind": 50}

    serialized = orch._serialize_route_seed(payload)

    assert len(orch._ROUTE_SEED_HEADER) + len(serialized) <= (
        orch._ROUTE_SEED_SERIALIZED_BUDGET)
    assert payload["truncated"] is True
    assert serialized.count("ir-learned-batch") == 2  # both endpoints kept it
    assert serialized.count("ir-affinity-lot") == 2   # evidence-backed rows too
    assert "ir-llm-guess" not in serialized            # LLM-tier detail went first
    assert "LOT-2026-0{id}" in serialized
    for system in payload["systems"]:
        assert system["shared_concepts"] == []  # tier-2 detail went first
        assert len(system["query_guidance"]) < 60 * len("generic guidance 0 ")


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_confirmed_rules_are_still_dropped_when_prose_cannot_give_enough():
    """Last resort: with prose already at its floor, confirmed rows are
    trimmed so the seed always lands under budget (the size invariant)."""
    rows = [{"id": f"ir-{index:03d}-" + "x" * 150, "confidence": 0.9, "_tier": 0}
            for index in range(50)]
    payload = {"systems": [{
        "id": "sys-a", "name": "System A", "plants": [],
        "capabilities": None, "connection_type": None,
        "query_guidance": None, "shared_concepts": [],
        "rules": {"join_paths": [], "value_mappings": [],
                  "identity_rules": rows},
    }], "rule_grain_cap_per_kind": 50}

    serialized = orch._serialize_route_seed(payload)

    assert len(orch._ROUTE_SEED_HEADER) + len(serialized) <= (
        orch._ROUTE_SEED_SERIALIZED_BUDGET)
    assert 0 < len(payload["systems"][0]["rules"]["identity_rules"]) < 50


def test_join_path_endpoints_are_table_qualified_in_the_seed_cypher():
    """A bare field is ambiguous inside one system (ERP has ``batch_id`` on two
    tables), so JoinPath endpoints ride as ``table.field``; an already-dotted
    field or a missing table falls back to the stored field unchanged."""
    cypher = orch._ROUTE_SEED_CYPHER
    for side in ("from", "to"):
        assert (f"{side}_field: CASE WHEN jp.{side}_table IS NULL OR jp.{side}_table = '' "
                f"OR jp.{side}_field CONTAINS '.' THEN jp.{side}_field "
                f"ELSE jp.{side}_table + '.' + jp.{side}_field END") in cypher


def test_qualified_join_endpoints_survive_serialization():
    def fake_query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "A"},
                    {"system_id": "sys-b", "system_name": "B"}]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return []
        return [{"system_id": "sys-a", "join_paths": [{
            "id": "jp-1", "from_system": "sys-a", "from_field": "work_orders.work_order_number",
            "to_system": "sys-b", "to_field": "lot_tracking.batch_id",
            "method": "user_confirmed",
        }]}]

    seed = orch.build_route_seed(query=fake_query)
    rule = _payload(seed)["systems"][0]["rules"]["join_paths"][0]
    assert rule["from_field"] == "work_orders.work_order_number"
    assert rule["to_field"] == "lot_tracking.batch_id"


def test_join_and_identity_rules_carry_their_confirmation_method():
    assert "method" in orch._ROUTE_SEED_RULE_FIELDS["join_paths"]
    assert "method" in orch._ROUTE_SEED_RULE_FIELDS["identity_rules"]


def test_mode_a_delegate_block_prefers_confirmed_joins_and_names_join_rule():
    block = orch._MODE_A_DELEGATE_BLOCK
    assert "join_rule:" in block
    assert "CONFIRMED JOINS BEAT IDENTIFIER GUESSES" in block
    assert "user_confirmed" in block and "SEQUENTIALLY" in block
