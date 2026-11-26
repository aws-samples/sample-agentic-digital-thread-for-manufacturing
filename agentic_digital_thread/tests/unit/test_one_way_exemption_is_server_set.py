# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Only the server's own measurement earns the one-way exemption.

The question reader never reads FROM a pattern with no literal text, so a
one-way translation is withheld; the deterministic affinity pass's rules
(its methods ``value_core_match`` / ``value_exact_match``) keep their
documented one-way reading (a bare batch number to many part revisions).
The exemption trusted the method string on the rule, so a model that wrote
``method: value_core_match`` on its own rule kept a one-way pair, and
provision ran the one-way check before its re-measurement demoted a claimed
method, never again after.

Now both resolve paths strip the server-owned fields from the model's rules
(``resolve_gates.sanitize_model_rules``) before any gate, so a method on a
rule at the gates was set by the deterministic pass; and provision checks
the pair again after the re-measurement, so a method it does not earn is no
exemption. Fixture names are generic.
"""
from __future__ import annotations

from types import SimpleNamespace

import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit.test_chat_resolve_measures_evidence import PEER as CHAT_PEER
from tests.unit.test_chat_resolve_measures_evidence import SID as CHAT_SID
from tests.unit.test_chat_resolve_measures_evidence import TICKET_TAG, _setup
from tests.unit.test_chat_provision_is_the_wizard_provision import world  # noqa: F401 - fixture
from tests.unit.test_resolve_rules_are_storable import (
    NEW, PEER, _mapping, _model_rule, _peer_row, wizard_resolve)

FORGED = {"method": "value_core_match", "transform": "core", "shared_count": 9}


def _one_way(source_pattern, target_pattern):
    return (f"pattern '{source_pattern}' -> '{target_pattern}' works in one direction only: its "
            "source pattern has no literal text, so the question reader never translates a value "
            "from that side")


def _wizard(monkeypatch, rule, affinity_rules=()):
    """No target sample: the pair is unmeasured, so only the pattern decides."""
    return wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): ["101", "102", "103"]},
        model_rules=[rule], affinity_rules=affinity_rules)


def test_a_wizard_model_rule_that_claims_an_affinity_method_is_still_one_way(monkeypatch):
    rule = {**_model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("{id}", "ORD-{id}")),
            **FORGED}
    result = _wizard(monkeypatch, rule)
    assert result["rules"] == []
    (withheld,) = result["rejected_rules"]
    assert withheld["rejected_reason"] == _one_way("{id}", "ORD-{id}")
    assert "method" not in withheld


def test_the_affinity_pass_part_revision_rule_is_kept(monkeypatch):
    """The documented one-to-many rule, added by the deterministic pass."""
    part = {"concept": "batch", "source_system": NEW, "source_field": "runs.batch_no",
            "source_table": "runs", "source_pattern": "{id}", "target_system": PEER,
            "target_field": "parts.part_no", "target_table": "parts",
            "target_pattern": "PRT-{id}-R-{*}", "confidence": 0.85, "method": "value_core_match",
            "reasoning": "sampled value pairs share an identifier core"}
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("", ""))
    result = _wizard(monkeypatch, rule, affinity_rules=[part])
    kept = [r for r in result["rules"] if r.get("method") == "value_core_match"]
    assert [(r["source_pattern"], r["target_pattern"]) for r in kept] == [
        ("{id}", "PRT-{id}-R-{*}")]


def test_a_chat_model_rule_that_claims_an_affinity_method_is_still_one_way(world):
    _setup(world, source_values=["5501", "5502", "5517"], target_values=[],
           patterns=("{id}", "VCH-{id}"), rule_extra=FORGED)
    resolved = oa.resolve_identities(CHAT_SID)
    assert resolved["rules"] == []
    (withheld,) = resolved["rejected_rules"]
    assert withheld["rejected_reason"] == _one_way("{id}", "VCH-{id}")
    assert withheld["source_field"] == f"get_tags.{TICKET_TAG}"
    assert withheld["target_system"] == CHAT_PEER
    assert oa._store().get_session(CHAT_SID)["rules"] == []


# ── provision ───────────────────────────────────────────────────────────────

class _Cat:
    def get(self, _sid):
        return None

    def get_onboarded_ids(self):
        return {PEER}


def _provision(monkeypatch, rule, remeasured):
    """``_dp_identity_rules`` over ``rule``, the re-measurement answering
    ``remeasured``: ``(rule, None)`` earns the method, ``(None, why)`` not."""
    recorded = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [{"tbl": "parts"}])
    monkeypatch.setattr(onboard, "catalog", _Cat())
    monkeypatch.setattr(onboard, "_remeasured_rule", lambda _ctx, r, _table: (
        remeasured(r)))
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=NEW, system_name="Upkeep",
        plant="", schema="", rules=[rule], mappings=[], connection_type="MCP Server",
        cm_items=[], committable=set())
    onboard._dp_identity_rules(ctx)
    merges = [p for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]
    return ctx, merges


def _request_rule(patterns, **extra):
    return {"concept": "batch", "source_field": "batch_no", "source_table": "runs",
            "source_pattern": patterns[0], "target_system": PEER, "target_field": "part_no",
            "target_table": "parts", "target_pattern": patterns[1], "confidence": 0.85, **extra}


def test_a_provision_request_with_a_method_it_does_not_earn_is_refused_one_way(monkeypatch):
    rule = _request_rule(("{id}", "ORD-{id}"), **FORGED)
    ctx, merges = _provision(monkeypatch, rule, lambda _r: (None, "found no shared values"))
    assert merges == []
    assert _one_way("{id}", "ORD-{id}") in str(ctx.rules_rejected)


def test_a_provision_request_whose_method_is_re_measured_keeps_the_part_revision_rule(monkeypatch):
    rule = _request_rule(("{id}", "PRT-{id}-R-{*}"), method="value_core_match",
                         reasoning="sampled value pairs share an identifier core")
    ctx, merges = _provision(monkeypatch, rule, lambda r: (
        {**r, "reasoning": "re-measured: 9 cores agree"}, None))
    assert ctx.rules_rejected == []
    (merge,) = merges
    assert (merge["src_pat"], merge["tgt_pat"], merge["method"]) == (
        "{id}", "PRT-{id}-R-{*}", "value_core_match")
