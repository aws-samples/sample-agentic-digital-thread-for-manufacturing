# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Computed concept-authority boundaries reach the ORCHESTRATOR synthesis layer.

ROOT CAUSE under test: steward business context is delivered to SYSTEM AGENTS
only, so the orchestrator — the layer that writes the final prose — never
learned that a system is NOT authoritative for the requested concept, and
relabelled a proxy table as the requested quantity. The fix delivers that
boundary as a bounded COMPUTED block on the Mode-A route seed instead of the
prose.

Offline and pure: graph reads use an injected fake query; no AWS, Neptune,
gateway, DB or LLM. Every fixture id is synthetic and domain-free.
"""
from __future__ import annotations

import json

import agents.concept_authority as ca
import agents.orchestrator as orch


# --- seed harness ----------------------------------------------------------

def _payload(seed: str) -> dict:
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        if seed.startswith(header):
            return json.loads(seed[len(header):])
    raise AssertionError("route seed has no recognized header")


def _fake_query(rule_rows, authority_rows=(), *, authority_fails=False):
    """Injected graph seam: inventory from the rule rows' system ids."""
    inventory = [
        {"system_id": row["system_id"], "system_name": row["system_id"],
         "plants": [], "capabilities": None, "connection_type": None,
         "query_guidance": None}
        for row in rule_rows
    ]

    def query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return list(inventory)
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            if authority_fails:
                raise RuntimeError("authority read unavailable")
            return [dict(row) for row in authority_rows]
        return [dict(row) for row in rule_rows]

    return query


def _rule_row(system_id: str, mapped_concepts=None) -> dict:
    row = {"system_id": system_id, "shared_concepts": [], "join_paths": [],
           "value_mappings": [], "identity_rules": []}
    if mapped_concepts is not None:
        row["mapped_concepts"] = list(mapped_concepts)
    return row


def _block(rule_rows, authority_rows=(), **kwargs):
    seed = orch.build_route_seed(
        query=_fake_query(rule_rows, authority_rows, **kwargs))
    assert seed is not None
    return _payload(seed).get("concept_authority")


def re_strings(node) -> list:
    """Every string key and value in a nested structure."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.append(key)
            out.extend(re_strings(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(re_strings(value))
    elif isinstance(node, str):
        out.append(node)
    return out


# --- the signal appears where the boundary is decidable --------------------

def test_signal_names_the_system_with_no_field_mapping_for_the_concept():
    """sys-b lacks any field mapped to concept-alpha while sys-a has one."""
    block = _block([_rule_row("sys-a", ["concept-alpha", "concept-shared"]),
                    _rule_row("sys-b", ["concept-beta", "concept-shared"])])
    boundaries = {entry["concept"]: entry for entry in block["boundaries"]}
    assert boundaries["concept-alpha"]["authority"] == ["sys-a"]
    assert boundaries["concept-alpha"]["not_authoritative"] == ["sys-b"]
    assert boundaries["concept-alpha"]["authority_basis"] == ca.BASIS_FIELD_MAPPING
    # Symmetric, and a concept BOTH systems map is no boundary at all.
    assert boundaries["concept-beta"]["not_authoritative"] == ["sys-a"]
    assert "concept-shared" not in boundaries
    assert block["evidence_orphans"] == []
    assert block["truncated"] is False


def test_note_tells_the_synthesis_layer_to_keep_the_proxy_label():
    """The delivered instruction is the anti-relabelling rule, not prose."""
    block = _block([_rule_row("sys-a", ["concept-alpha"]),
                    _rule_row("sys-b", ["concept-beta"])])
    note = block["note"]
    assert "proxy" in note.lower() and "retitle" in note.lower()
    assert "not_authoritative" in note
    # The note is FIXED engine text: it never names a system or a concept.
    for identifier in ("sys-a", "sys-b", "concept-alpha", "concept-beta"):
        assert identifier not in note


def test_authority_rule_with_field_evidence_sets_the_authority_rule_basis():
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"])],
        [{"id": "ar-1", "concept": "concept-alpha", "system_id": "sys-a",
          "method": "user_confirmed", "confidence": 1.0}])
    entry = next(e for e in block["boundaries"] if e["concept"] == "concept-alpha")
    assert entry["authority"] == ["sys-a"]
    assert entry["authority_basis"] == ca.BASIS_AUTHORITY_RULE
    assert block["evidence_orphans"] == []


# --- honest absence --------------------------------------------------------

def test_no_claim_when_coverage_is_absent():
    """A read without the coverage column yields unknown coverage, not absence."""
    assert _block([_rule_row("sys-a"), _rule_row("sys-b")]) is None


def test_no_claim_when_the_authority_read_failed():
    """Unknown assertions ⇒ no boundary can be claimed at all."""
    rows = [_rule_row("sys-a", ["concept-alpha"]),
            _rule_row("sys-b", ["concept-beta"])]
    assert _block(rows, authority_fails=True) is None
    # Direct contract: None (read failed) is NOT the same as [] (none found).
    coverage = {"sys-a": ["concept-alpha"], "sys-b": ["concept-beta"]}
    assert ca.build_concept_authority(coverage, None) is None
    assert ca.build_concept_authority(coverage, []) is not None


def test_a_system_with_zero_mapped_concepts_is_never_named():
    """Unknown coverage (structural-only / telemetry-spine) is not 'no coverage'."""
    block = _block([_rule_row("sys-a", ["concept-alpha"]),
                    _rule_row("sys-b", ["concept-beta"]),
                    _rule_row("sys-c", [])])
    for entry in block["boundaries"]:
        assert "sys-c" not in entry["not_authoritative"]


def test_coverage_that_reached_the_read_cap_is_unknown_not_uncovered():
    capped = [f"concept-{index}" for index in range(ca.COVERAGE_LIST_CAP)]
    block = _block([_rule_row("sys-a", ["concept-alpha"]),
                    _rule_row("sys-b", ["concept-beta"]),
                    _rule_row("sys-c", capped)])
    for entry in block["boundaries"]:
        assert "sys-c" not in entry["not_authoritative"]


def test_single_known_system_yields_no_claim():
    assert ca.build_concept_authority({"sys-a": ["concept-alpha"]}, []) is None


# --- evidence orphans: detection AND prevention ----------------------------

def test_machine_originated_evidence_orphan_is_detected_and_prevented():
    """An unvouched machine authority claim cannot establish authority, and
    cannot hand it to whichever system merely carries a field mapping."""
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"])],
        [{"id": "ar-orphan", "concept": "concept-alpha", "system_id": "sys-b",
          "method": "onboarding-llm", "confidence": 0.6}])
    assert block["evidence_orphans"] == [
        {"concept": "concept-alpha", "system": "sys-b", "prevented": True,
         "rule_id": "ar-orphan"}]
    concepts = {entry["concept"] for entry in block["boundaries"]}
    assert "concept-alpha" not in concepts  # undecidable, so nothing claimed
    assert "concept-beta" in concepts       # unaffected concepts still decidable


def test_steward_confirmed_evidence_orphan_keeps_authority_but_is_labelled():
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"])],
        [{"id": "ar-steward", "concept": "concept-gamma", "system_id": "sys-b",
          "method": "user_confirmed", "confidence": 1.0}])
    entry = next(e for e in block["boundaries"] if e["concept"] == "concept-gamma")
    assert entry["authority"] == ["sys-b"]
    assert entry["authority_basis"] == ca.BASIS_STEWARD_ASSERTED
    assert entry["not_authoritative"] == ["sys-a"]
    assert block["evidence_orphans"] == [
        {"concept": "concept-gamma", "system": "sys-b", "prevented": False,
         "rule_id": "ar-steward"}]


def test_authority_rows_for_unknown_systems_are_ignored():
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"])],
        [{"id": "ar-x", "concept": "concept-alpha", "system_id": "sys-missing",
          "method": "onboarding-llm"}])
    assert block["evidence_orphans"] == []
    assert {e["concept"] for e in block["boundaries"]} == {
        "concept-alpha", "concept-beta"}


def test_orphan_is_never_asserted_against_a_capped_coverage_list():
    """A claiming system whose coverage list REACHED the read cap has
    UNKNOWN coverage, so ``prevented: true`` would be a positive FALSE claim.

    The module already refuses to name such a system non-authoritative; the
    orphan verdict has to obey the same rule. Failing open lands on the
    boundary, never on the orphan claim: the rule keeps its authority.
    """
    capped = [f"concept-{index}" for index in range(ca.COVERAGE_LIST_CAP)]
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"]),
         _rule_row("sys-c", capped)],
        [{"id": "ar-capped", "concept": "concept-alpha", "system_id": "sys-c",
          "method": "onboarding-llm"}])
    assert block["evidence_orphans"] == []

    # The resolution itself: the capped system stays an authority holder and the
    # concept stays DECIDABLE — the machine-orphan "blocked" path never fires.
    holders, basis, orphans = ca._resolve_authority(
        "concept-alpha", {"sys-a"},
        [{"system": "sys-c", "rule_id": "ar-capped", "steward": False}],
        judgeable={"sys-a", "sys-b"})
    assert orphans == []
    assert holders == ["sys-c"]
    assert basis == ca.BASIS_AUTHORITY_RULE


def test_orphan_is_never_asserted_against_a_system_with_unknown_coverage():
    """The zero-mapped-concept (structural-only) half of the same rule."""
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"]),
         _rule_row("sys-c", [])],
        [{"id": "ar-structural", "concept": "concept-alpha",
          "system_id": "sys-c", "method": "onboarding-llm"}])
    assert block["evidence_orphans"] == []
    entry = next(e for e in block["boundaries"]
                 if e["concept"] == "concept-alpha")
    assert entry["authority"] == ["sys-c"]


def test_orphan_is_still_asserted_when_coverage_is_genuinely_judgeable():
    """The capped-coverage carve-out must not swallow the real orphan case."""
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"])],
        [{"id": "ar-real", "concept": "concept-alpha", "system_id": "sys-b",
          "method": "onboarding-llm"}])
    assert block["evidence_orphans"] == [
        {"concept": "concept-alpha", "system": "sys-b", "prevented": True,
         "rule_id": "ar-real"}]


# --- block-level disclosure under budget pressure ------------------------

def _pressure_payload(orphans):
    """A payload whose prose alone blows the seed budget, with one boundary."""
    block = {
        "note": ca.NOTE,
        "boundaries": [{"concept": "concept-alpha", "authority": ["sys-a"],
                        "authority_basis": ca.BASIS_FIELD_MAPPING,
                        "not_authoritative": ["sys-b"]}],
        "evidence_orphans": list(orphans),
        "boundary_cap": ca.MAX_BOUNDARIES,
        "truncated": False,
    }
    systems = [{"id": "sys-a", "shared_concepts": [], "rules": {},
                "capabilities": "x" * orch._ROUTE_SEED_SERIALIZED_BUDGET}]
    payload = {"systems": systems, "concept_authority": block}
    return payload, systems


def test_trimming_the_last_boundary_deletes_the_block_entirely():
    """A claim-free block must not survive as an empty, complete-looking one."""
    payload, systems = _pressure_payload([])
    orch._trim_route_seed_rules(payload, systems,
                                orch._encode_route_seed(payload))
    assert "concept_authority" not in payload


def test_trimming_a_boundary_discloses_truncation_on_the_block():
    """With orphans still present the block survives — but never as complete."""
    orphan = {"concept": "concept-gamma", "system": "sys-b", "prevented": True}
    payload, systems = _pressure_payload([orphan])
    orch._trim_route_seed_rules(payload, systems,
                                orch._encode_route_seed(payload))
    block = payload["concept_authority"]
    assert block["boundaries"] == []
    assert block["truncated"] is True
    assert block["evidence_orphans"] == [orphan]


def test_untouched_block_keeps_its_truncated_flag_false():
    """No budget pressure ⇒ the disclosure flag is not spuriously raised."""
    payload = {"systems": [], "concept_authority": {
        "note": ca.NOTE, "boundaries": [{"concept": "concept-alpha",
                                         "authority": ["sys-a"],
                                         "authority_basis": ca.BASIS_FIELD_MAPPING,
                                         "not_authoritative": ["sys-b"]}],
        "evidence_orphans": [], "boundary_cap": ca.MAX_BOUNDARIES,
        "truncated": False}}
    orch._trim_route_seed_rules(payload, [], orch._encode_route_seed(payload))
    assert payload["concept_authority"]["truncated"] is False
    assert payload["concept_authority"]["boundaries"]


# --- the field-evidence predicate has ONE home ----------------------------

def test_route_seed_coverage_composes_the_shared_field_evidence_predicate():
    assert (f"(s){ca.FIELD_EVIDENCE_TRAVERSAL}(oc:Concept)"
            in orch._ROUTE_SEED_CYPHER)
    # An ENTITY-level SchemaEntry mapping is not field evidence.
    assert "(:SchemaEntry)-[:MAPS_TO]->" not in ca.FIELD_EVIDENCE_TRAVERSAL
    assert "-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->" in ca.FIELD_EVIDENCE_TRAVERSAL


# --- bounded ---------------------------------------------------------------

def test_block_honors_every_documented_cap():
    """Pure builder: 40 systems each covering one distinct concept."""
    coverage = {f"sys-{index:03d}": [f"concept-{index:03d}"] for index in range(40)}
    block = ca.build_concept_authority(coverage, [])
    assert len(block["boundaries"]) <= ca.MAX_BOUNDARIES
    assert block["boundary_cap"] == ca.MAX_BOUNDARIES
    for entry in block["boundaries"]:
        assert len(entry["authority"]) <= ca.MAX_SYSTEMS_PER_SIDE
        assert len(entry["not_authoritative"]) <= ca.MAX_SYSTEMS_PER_SIDE
    assert ca.serialized_length(block) <= ca.MAX_BLOCK_CHARS
    assert block["truncated"] is True


def test_orphans_are_capped_and_disclosed():
    coverage = {f"sys-{index}": [f"concept-{index}"] for index in range(3)}
    orphans = [{"id": f"ar-{index}", "concept": f"orphan-concept-{index}",
                "system_id": "sys-0", "method": "onboarding-llm"}
               for index in range(ca.MAX_ORPHANS + 3)]
    block = ca.build_concept_authority(coverage, orphans)
    assert len(block["evidence_orphans"]) == ca.MAX_ORPHANS
    assert block["truncated"] is True


def test_pathological_identifiers_are_truncated():
    long_id = "s" * (ca.MAX_TEXT_LEN * 4)
    block = ca.build_concept_authority(
        {long_id: ["c" * (ca.MAX_TEXT_LEN * 4)], "sys-b": ["concept-beta"]}, [])
    for value in re_strings(block["boundaries"]):
        assert len(value) <= max(ca.MAX_TEXT_LEN, len(ca.BASIS_FIELD_MAPPING))
    assert ca.serialized_length(block) <= ca.MAX_BLOCK_CHARS


def test_seed_stays_inside_its_own_budget_with_the_block():
    rows = [_rule_row(f"sys-{index:03d}", [f"concept-{index:03d}"])
            for index in range(12)]
    seed = orch.build_route_seed(query=_fake_query(rows))
    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert _payload(seed)["concept_authority"]["boundaries"]


# --- byte identity when nothing applies ------------------------------------

def test_ordinary_turn_seed_is_byte_identical_when_nothing_applies():
    """Same graph content, coverage column present but no decidable boundary:
    the composed seed must equal the pre-change seed byte for byte."""
    shared = ["concept-shared", "concept-other"]
    with_coverage = orch.build_route_seed(
        query=_fake_query([_rule_row("sys-a", shared), _rule_row("sys-b", shared)]))
    legacy = orch.build_route_seed(
        query=_fake_query([_rule_row("sys-a"), _rule_row("sys-b")]))
    assert with_coverage == legacy
    assert "concept_authority" not in with_coverage
    # The coverage input itself never becomes seed content.
    assert "mapped_concepts" not in with_coverage


def test_coverage_detail_is_never_echoed_as_seed_content():
    seed = orch.build_route_seed(query=_fake_query(
        [_rule_row("sys-a", ["concept-alpha"]), _rule_row("sys-b", ["concept-beta"])]))
    payload = _payload(seed)
    assert "mapped_concepts" not in json.dumps(payload)
    for system in payload["systems"]:
        assert "mapped_concepts" not in system


# --- neutrality ------------------------------------------------------------

def test_block_vocabulary_is_derived_only_from_the_graph_input():
    """Every string the block emits is either an input id, a pinned basis
    label, a structural key, or the fixed note — the engine invents no domain
    vocabulary of its own (ADR-0044 neutrality)."""
    inputs = {"sys-a", "sys-b", "concept-alpha", "concept-beta", "ar-steward"}
    block = _block(
        [_rule_row("sys-a", ["concept-alpha"]),
         _rule_row("sys-b", ["concept-beta"])],
        [{"id": "ar-steward", "concept": "concept-alpha", "system_id": "sys-a",
          "method": "user_confirmed"}])
    allowed = inputs | {ca.BASIS_FIELD_MAPPING, ca.BASIS_AUTHORITY_RULE,
                        ca.BASIS_STEWARD_ASSERTED, ca.NOTE,
                        "note", "boundaries", "evidence_orphans",
                        "boundary_cap", "truncated", "concept", "authority",
                        "authority_basis", "not_authoritative", "system",
                        "prevented", "rule_id"}
    for token in re_strings(block):
        assert token in allowed, token


def test_business_context_prose_is_not_a_dependency_of_the_signal():
    """The orchestrator still never touches business context (ADR-0054): the
    boundary is computed from graph rows only."""
    source = (orch.__file__, ca.__file__)
    for path in source:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "read_business_context" not in text
        assert "render_prompt_section" not in text
