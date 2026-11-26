# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AuthorityRule evidence-orphan prevention during onboarding."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import agents.concept_authority as concept_authority
import agents.onboard as onboard


class _Catalog:
    def get(self, system_id):
        if system_id == "system-a":
            return {"system_type": "MES"}
        return None


def _ctx(mappings):
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]),
        system_id="system-a",
        system_name="System A",
        plant="site-a",
        schema="schema_a",
        rules=[],
        mappings=mappings,
        connection_type="MCP Server",
        cm_items=[],
    )


def _run(monkeypatch, mappings, evidenced=()):
    """Run the c4 step offline; return (writes, queries)."""
    writes: list[tuple[str, dict]] = []
    queries: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "catalog", _Catalog())
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: writes.append(
                            (cypher, params or {})))

    def query(cypher, params=None):
        queries.append((cypher, params or {}))
        if "RETURN DISTINCT c.id AS concept" in cypher:
            return [{"concept": concept} for concept in evidenced]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    ctx = _ctx(mappings)
    onboard._dp_authority_rules(ctx)
    return ctx, writes, queries


def _deactivating(writes):
    return [(cypher, params) for cypher, params in writes
            if "ar.status = 'deactivated'" in cypher]


def _flagging(writes):
    return [(cypher, params) for cypher, params in writes
            if "steward-confirmed'" in cypher]


def test_reonboard_soft_deactivates_rule_whose_field_evidence_disappeared(
        monkeypatch):
    _ctx_out, writes, _q = _run(monkeypatch, [])

    orphan = _deactivating(writes)
    assert len(orphan) == 1
    cypher, params = orphan[0]
    assert "MATCH (ar:AuthorityRule {system_id: $sid})" in cypher
    assert "NOT (ar.concept IN $concepts)" in cypher
    assert "ar.integrity_flag = 'evidence-orphan'" in cypher
    assert params["concepts"] == []
    assert params["actor"] == "onboarding-integrity"


def test_machine_deactivation_excludes_steward_confirmed_claims(monkeypatch):
    """The machine never unwinds a steward verdict — it FLAGS it (ADR-0052)."""
    _ctx_out, writes, _q = _run(monkeypatch, [])

    for cypher, params in _deactivating(writes):
        assert "NOT (coalesce(ar.method, '') IN $steward_methods" in cypher
        assert "OR coalesce(ar.approved_by, '') <> '')" in cypher
        assert params["steward_methods"] == sorted(
            concept_authority.STEWARD_CONFIRMED_METHODS)
    flagged = _flagging(writes)
    assert len(flagged) == 1
    cypher, _params = flagged[0]
    assert "ar.integrity_flag = 'evidence-orphan-steward-confirmed'" in cypher
    assert "SET ar.status" not in cypher
    assert "(coalesce(ar.method, '') IN $steward_methods" in cypher


def test_superseded_branch_is_scoped_to_the_same_plant(monkeypatch):
    """ADR-0045: re-onboarding at one site never retires another site's rule."""
    _ctx_out, writes, _q = _run(monkeypatch, [{"concept": "machine-state"}],
                                evidenced=("machine-state",))

    superseded = [(cypher, params) for cypher, params in writes
                  if "'superseded'" in cypher]
    assert len(superseded) == 1
    cypher, params = superseded[0]
    # AuthorityRule carries no plant property: the scope comes from its System.
    assert "(other:System)" in cypher
    assert "other.id = ar.system_id AND other.plant = $plant" in cypher
    assert params["plant"] == "site-a"
    # The steward-confirmed competing claim is flagged, never deactivated.
    steward_superseded = [c for c, _p in writes
                          if "'superseded-steward-confirmed'" in c]
    assert len(steward_superseded) == 1
    assert "SET ar.status" not in steward_superseded[0]


def test_reonboard_reverses_only_the_machines_own_deactivation(monkeypatch):
    """Returning field evidence must be able to revive a machine husk.

    Pinned as DISCRIMINATED behaviour, not as the blanket absence of an
    ``ar.status = 'active'`` write: the machine may reverse a deactivation it
    stamped itself (``deactivated_by = 'onboarding-integrity'``) and MUST NOT
    touch any other actor's — a steward deactivation survives re-onboard.
    """
    ctx, writes, _q = _run(monkeypatch, [{"concept": "machine-state"}],
                           evidenced=("machine-state",))

    revive = [(cypher, params) for cypher, params in writes
              if "ar.status = 'active'" in cypher]
    assert len(revive) == 1
    cypher, params = revive[0]
    assert "MATCH (ar:AuthorityRule {id: $arid})" in cypher
    assert "coalesce(ar.status, '') = 'deactivated'" in cypher
    # The discriminator: only the machine's OWN stamp qualifies.
    assert "coalesce(ar.deactivated_by, '') = $actor" in cypher
    assert params["actor"] == "onboarding-integrity"
    assert params["arid"] == "ar-system-a-machine-state"
    # No husk left wearing a deactivation while active.
    assert ("REMOVE ar.deactivated_by, ar.deactivated_at, ar.integrity_flag"
            in cypher)
    assert "ar.reactivated_by = $actor" in cypher
    assert ctx.authority_rules_written == 1


def test_reversal_is_never_minted_for_an_unevidenced_concept(monkeypatch):
    """No field evidence ⇒ nothing is minted, so nothing can be revived."""
    ctx, writes, _q = _run(monkeypatch, [])

    assert not [c for c, _p in writes if "ar.status = 'active'" in c]
    assert not [c for c, _p in writes if "MERGE (ar:AuthorityRule" in c]
    assert ctx.authority_rules_written == 0


def test_authority_reconciliation_never_deletes_or_rejects_rules(monkeypatch):
    ctx, writes, queries = _run(monkeypatch, [{"concept": "machine-state"}],
                                evidenced=("machine-state",))

    assert concept_authority.FIELD_EVIDENCE_TRAVERSAL in queries[0][0]
    text = "\n".join(cypher for cypher, _ in writes)
    assert "DELETE" not in text.upper()
    assert "rejected" not in text.lower()
    assert "ar.integrity_flag = 'superseded'" in text
    assert "MERGE (ar:AuthorityRule {id: $arid})" in text
    assert ctx.authority_rules_written == 1


def test_field_evidence_predicate_has_one_home(monkeypatch):
    """The traversal is composed from the single exported constant."""
    source = inspect.getsource(onboard._dp_field_evidenced_concepts)
    assert "concept_authority.FIELD_EVIDENCE_TRAVERSAL" in source
    assert "HAS_FIELD" not in source
    # An entity-level SchemaEntry mapping must never satisfy the predicate.
    assert "(:SchemaEntry)-[:HAS_FIELD]->" \
        in concept_authority.FIELD_EVIDENCE_TRAVERSAL


def test_authority_writer_source_contains_no_auto_delete():
    source = inspect.getsource(onboard._dp_deactivate_stale_authority_rules)
    source += inspect.getsource(onboard._dp_authority_integrity_writes)
    source += inspect.getsource(onboard._dp_reactivate_machine_deactivated_rule)
    source += inspect.getsource(onboard._dp_authority_rules)
    assert "DETACH DELETE" not in source.upper()
    assert " DELETE " not in source.upper()
    assert "status = 'rejected'" not in source
