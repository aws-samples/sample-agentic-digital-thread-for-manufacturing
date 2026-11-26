# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pin the production route-seed budget DECISION and the full composed-return
ceilings independent of the mutable constant.

The historical lever fixture stays at ``LEVER_BUDGET = 8_000`` (the budget those
numbers were measured under; see test_route_seed_routing_guidance.py). The
production tests used to compare ``seed_chars`` to ``len(seed)`` and only assert
``len(seed) <= _ROUTE_SEED_SERIALIZED_BUDGET`` — so raising the constant, or
growing output alongside it, stayed green. These pins instead:

* assert the ADR-0035 decision as the literal 16,000, so a change to the
  constant is a deliberate, test-breaking edit;
* assert representative FULL composed-return lengths (seed + the post-JSON
  IDENTIFIER TRANSLATIONS / OWNERS / wildcard tail) against the literal budget;
* cover the mixed exact-owner + wildcard/translation reserve-fallback case
  (the composed return must stay within the literal budget even
  when the owners reserve is unaffordable).
"""
from __future__ import annotations

import hashlib

import pytest

from agents import identifier_translation as it
from agents import identity_affinity as ia
from agents import orchestrator as orch
from agents.connection_types import POSTGRESQL, default_capabilities
from tests.unit import _route_seed_workshop_fixture as wf

#: The ADR-0035 production decision (2026-10-17, was 10,000 from 2026-10-08).
#: Pinned as a literal so a
#: change to orchestrator._ROUTE_SEED_SERIALIZED_BUDGET breaks this test.
PRODUCTION_BUDGET = 16_000


def test_production_budget_decision_is_pinned_to_the_adr_literal():
    assert orch._ROUTE_SEED_SERIALIZED_BUDGET == PRODUCTION_BUDGET


@pytest.mark.parametrize("state", sorted(wf.STATES))
def test_full_composed_return_stays_within_the_literal_budget_with_the_tail(state):
    """The whole composed return — seed PLUS every post-JSON block — is within
    the literal budget, and the owners block is actually present in the tail (so
    the pin covers the tail, not only the fitted seed)."""
    ids = wf.STATES[state]
    composed = wf.build_seed(ids, wf.IDENTIFIER_QUESTION)
    _payload, tail = wf.decode(composed)

    assert len(composed) <= PRODUCTION_BUDGET
    assert it.OWNERS_HEADER in tail  # a post-JSON block is included in the pinned length


# The over-budget tail shape, reused as a ceiling pin: exact owners + a wildcard
# note whose reserve the tier-0/1 floor cannot afford.
RUN_NUMBER = "RUN-20261008-90001"
MIXED_QUESTION = (f"Was machine L3-M1 maintained on schedule before production run "
                  f"{RUN_NUMBER} ran?")
_A, _B = "system-a", "system-b"


def _mixed_graph(n_exact: int):
    def _exact(idx):
        src = ia.SampledField(_A, f"production_runs_{idx}", "run_number", "batch",
                              (RUN_NUMBER,) * 40)
        tgt = ia.SampledField(_B, f"shipment_items_{idx}", "batch_id", "batch",
                              (RUN_NUMBER,) * 400)
        reasoning = ia._exact_reasoning(src, tgt, ia.literal_shape(RUN_NUMBER),
                                        set(range(19)), "batch")
        d = hashlib.sha256(str(idx).encode()).hexdigest()[:16]
        rule = {"id": f"ir-{_A}-{d}", "concept": "batch", "source_system": _A,
                "source_field": f"production_runs_{idx}.run_number", "target_system": _B,
                "target_field": f"shipment_items_{idx}.batch_id", "source_pattern": "",
                "target_pattern": "", "method": ia.EXACT_METHOD, "confidence": 0.85,
                "provenance": reasoning}
        join = {"id": f"jp-{_A}-{d}", "from_system": _A,
                "from_field": f"a.production_runs_{idx}.run_number", "to_system": _B,
                "to_field": f"b.shipment_items_{idx}.batch_id", "transform": "identity",
                "method": ia.EXACT_METHOD, "confidence": 0.85, "provenance": reasoning}
        return rule, join

    wild_rule = {"id": f"ir-{_A}-wild", "concept": "product", "source_system": _A,
                 "source_field": "products.item_id", "target_system": _B,
                 "target_field": "products.part_number", "source_pattern": "PN-{id}",
                 "target_pattern": "PART-{id}-{*}", "method": "value_format_match",
                 "confidence": 0.8, "provenance": "pattern probed"}
    exact = [_exact(i) for i in range(n_exact)]
    identity_rules = [r for r, _ in exact] + [wild_rule]
    join_paths = [j for _, j in exact]
    rows = {sid: {"system_id": sid, "system_name": sid.upper(), "connection_type": POSTGRESQL,
                  "identity_rules": list(identity_rules), "join_paths": list(join_paths),
                  "shared_concepts": [], "value_mappings": []} for sid in (_A, _B)}
    inventory = [{"system_id": sid, "system_name": sid.upper(), "plants": ["p"],
                  "capabilities": default_capabilities(POSTGRESQL),
                  "connection_type": POSTGRESQL,
                  "query_guidance": default_capabilities(POSTGRESQL)} for sid in (_A, _B)]

    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [rows[_A], rows[_B]]
        return []
    return query


def test_mixed_exact_owner_and_wildcard_reserve_fallback_stays_within_the_literal_budget():
    query = _mixed_graph(n_exact=29)
    rows = query(orch._ROUTE_SEED_CYPHER)
    _t, owners, note = orch._identifier_block_parts(MIXED_QUESTION, rows)
    assert note == it.WILDCARD_SEED_NOTE and owners  # the reserve-fallback path is exercised

    composed = orch.build_route_seed(query=query, question=MIXED_QUESTION)
    assert composed is not None and len(composed) <= PRODUCTION_BUDGET
