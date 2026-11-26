# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Owner rules have a total deterministic order and the smallest owners form
names EVERY candidate system.

``find_owners`` sorted only by confidence and token, and the route-seed Cypher
and ``_rule_sort_key`` had no stable rule-id/endpoint tie-break, so equal-ranked
exact rules inherited database collection order. Under budget trimming that
order decided which named owners survived, and the smallest form named a few
arbitrary systems first — on the workshop module-8-plus-hub state it could drop
the question's own-plant MES behind unrelated equal-ranked owners. The order is
now total (tier, question overlap, confidence, then stable rule id and endpoint
ids) and the smallest form names every candidate system compactly.
"""
from __future__ import annotations

import random

from agents import identifier_translation as it
from agents import identity_affinity as ia
from agents import orchestrator as orch
from tests.unit import _route_seed_workshop_fixture as wf

#: One run number whose literal shape matches the exact joins built below.
RUN_NUMBER = "RUN-20261008-90001"
RUN_SHAPE = ia.literal_shape(RUN_NUMBER)
_QUESTION = f"owners for production run {RUN_NUMBER}?"


def _exact_rule(src_sys, tgt_sys, *, confidence=0.85, src_field="run_number",
                tgt_field="batch_id", shape=RUN_SHAPE):
    """One exact IdentityRule in the exact pass's own reasoning shape."""
    return {"id": f"ir-{src_sys}-{tgt_sys}-{confidence}", "concept": "batch",
            "source_system": src_sys, "source_field": src_field,
            "target_system": tgt_sys, "target_field": tgt_field,
            "source_pattern": "", "target_pattern": "", "method": ia.EXACT_METHOD,
            "confidence": confidence,
            "provenance": (f"19 exact values of a.{src_field} exist in {tgt_sys} b.{tgt_field} "
                           f"(membership-probed); both columns shape {shape}; concept batch; "
                           f"40 and 400 distinct values sampled")}


def test_every_matching_candidate_system_is_named_beyond_the_owner_line_cap():
    """More than TRANSLATION_CAP matching owner pairs: the compact smallest form
    must still name every system that owns a run-number-shaped identifier, not
    only the systems behind the capped lines. Expected set comes from the RAW
    rules, not from find_owners output (which is capped)."""
    n = it.TRANSLATION_CAP + 4  # 12 distinct ERP endpoints, one shared MES source
    rules = [_exact_rule("mes-a", f"erp-{i:02d}") for i in range(n)]
    expected = {"mes-a", *(f"erp-{i:02d}" for i in range(n))}

    owners = it.find_owners(_QUESTION, rules)
    assert len(owners) == it.TRANSLATION_CAP  # the per-line list is still capped
    _whole, smallest = it.owners_block_sizes(owners)
    compact = it.render_owners(owners, max_chars=smallest)
    assert compact
    for sid in sorted(expected):
        assert sid in compact, f"{sid} missing from the smallest owners form"


def test_reversed_directional_duplicates_give_one_stable_owner_either_order():
    """The same logical join supplied in both directions with different
    confidence must fold to ONE owner whose rendered direction and confidence
    do not depend on collection order. Expected best rule (higher confidence)
    comes from the RAW rules.

    A real exact join writes two rules — one per side's resolve — that name the
    SAME two columns with source and target swapped: ``sys-00.run_number =
    erp.batch_id`` and ``erp.batch_id = sys-00.run_number``."""
    forward = _exact_rule("sys-00", "erp", confidence=0.80,
                          src_field="run_number", tgt_field="batch_id")
    reverse = _exact_rule("erp", "sys-00", confidence=0.95,
                          src_field="batch_id", tgt_field="run_number")

    out_a = it.find_owners(_QUESTION, [forward, reverse])
    out_b = it.find_owners(_QUESTION, [reverse, forward])
    assert len(out_a) == 1 and len(out_b) == 1
    rendered_a = it.render_owners(out_a)
    rendered_b = it.render_owners(out_b)
    assert rendered_a == rendered_b, "owner line depends on collection order"
    # The higher-confidence rule wins deterministically (0.95), not whichever came first.
    assert "confidence 0.95" in rendered_a and "confidence 0.80" not in rendered_a


def test_reversed_equal_confidence_directional_copies_render_identically():
    """The two directional copies of one join with the SAME
    tier and confidence (the natural output of one measurement) must render the
    same direction in either collection order."""
    forward = _exact_rule("sys-00", "erp", confidence=0.85,
                          src_field="run_number", tgt_field="batch_id")
    reverse = _exact_rule("erp", "sys-00", confidence=0.85,
                          src_field="batch_id", tgt_field="run_number")
    out_a = it.render_owners(it.find_owners(_QUESTION, [forward, reverse]))
    out_b = it.render_owners(it.find_owners(_QUESTION, [reverse, forward]))
    assert out_a == out_b and out_a.count("\n- ") == 1


def _shuffled_graph(ids, seed_value: int):
    """The workshop graph seam with every rule collection shuffled in place."""
    rows = [dict(row) for row in wf._rule_rows(ids)]
    rng = random.Random(seed_value)
    for row in rows:
        for kind in ("identity_rules", "join_paths", "value_mappings", "shared_concepts"):
            lst = list(row.get(kind) or [])
            rng.shuffle(lst)
            row[kind] = lst
    inventory, authority = wf._inventory(ids), wf._authority_rows(ids)

    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return authority
        return []
    return query


def test_seed_is_byte_identical_across_shuffled_equal_ranked_rule_collections():
    """A run-number question on the module-8-plus-hub state: equal-ranked exact
    rules in any collection order must produce the same seed."""
    ids = wf.STATES["module 8 + hub"]
    seeds = {orch.build_route_seed(query=_shuffled_graph(ids, s), question=wf.IDENTIFIER_QUESTION)
             for s in range(8)}
    assert len(seeds) == 1, f"{len(seeds)} distinct seeds across shuffles"


def test_find_owners_order_is_stable_under_rule_permutation():
    ids = wf.STATES["module 8 + hub"]
    orders = set()
    for s in range(8):
        rows = _shuffled_graph(ids, s)(orch._ROUTE_SEED_CYPHER)
        owners = it.find_owners(wf.IDENTIFIER_QUESTION, orch._seed_identity_rules(rows),
                                orch._seed_rule_dicts(rows, "join_paths"))
        orders.add(tuple((o["from_system"], o["from_field"], o["to_system"], o["to_field"])
                         for o in owners))
    assert len(orders) == 1, f"{len(orders)} distinct owner orders across shuffles"


def test_the_smallest_owners_form_names_every_candidate_system():
    """On the module-8-plus-hub state the smallest form must name every MES that
    owns a run-number-shaped identifier, so the question's own-plant MES is never
    dropped behind unrelated equal-ranked owners."""
    ids = wf.STATES["module 8 + hub"]
    rows = wf.graph(ids)(orch._ROUTE_SEED_CYPHER)
    owners = it.find_owners(wf.IDENTIFIER_QUESTION, orch._seed_identity_rules(rows),
                            orch._seed_rule_dicts(rows, "join_paths"))
    candidate_systems = {o["from_system"] for o in owners} | {o["to_system"] for o in owners}
    _whole, smallest = it.owners_block_sizes(owners)
    compact = it.render_owners(owners, max_chars=smallest)

    assert compact
    for sid in candidate_systems:
        assert sid in compact, f"{sid} missing from the smallest owners form"
