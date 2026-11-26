# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic identifier OWNERS in the route seed (ADR-0087 amendment).

Test event 28 (2026-10-06): a question naming a run number in the
exact-join shape ("... the same supplier lot as production run
RUN-20261006-90001") fanned out to every MES that shares the production-run
concept and once returned a different table built from other plants'
numbering. The graph already held a measured exact join (same values, same
shape) between one MES's run number and the ERP batch id; the seed now says
which systems own an identifier of that shape, so the model asks them first.
"""
import pytest

from agents import identifier_translation as it
from agents import identity_affinity as ia
from agents import orchestrator as orch


def _exact_rule(**over):
    base = {"id": "ir-exact", "concept": "production-run", "source_system": "mes-a",
            "source_field": "run_number", "source_pattern": "", "target_system": "erp-a",
            "target_field": "batch_id", "target_pattern": "", "confidence": 0.95,
            "method": ia.EXACT_METHOD, "transform": ia.EXACT_TRANSFORM,
            "provenance": ("12 exact values of production_runs.run_number exist in erp-a "
                           "shipment_items.batch_id (membership-probed); both columns shape "
                           "AAA-99999999-99999; concept production-run; 40 and 40 distinct "
                           "values sampled")}
    base.update(over)
    return base


def test_exact_shape_round_trips_through_the_reasoning_text():
    """The reasoning writer and the shape reader live together and only drift together."""
    src = ia.SampledField("mes-a", "production_runs", "run_number", "production-run",
                          ("RUN-20260921-91005", "RUN-20260921-91006"))
    tgt = ia.SampledField("erp-a", "shipment_items", "batch_id", "batch", ("RUN-20260921-91005",))
    text = ia._exact_reasoning(src, tgt, "AAA-99999999-99999", {"RUN-20260921-91005"}, "production-run")
    assert ia.exact_shape_from_reasoning(text) == "AAA-99999999-99999"
    assert ia.exact_shape_from_reasoning("50 of 50 sampled value pairs") is None
    assert ia.exact_shape_from_reasoning(None) is None


def test_find_owners_matches_tokens_of_the_exact_join_shape_only():
    out = it.find_owners("Which customers received parts from any batch that drew the same "
                         "supplier lot as production run RUN-20261006-90001? And lot "
                         "LOT-IND-BARAL7075150-H2240, order WO-20261006-90001.", [_exact_rule()])
    assert [(o["token"], o["from_system"], o["from_field"], o["to_system"], o["to_field"]) for o in out] == [
        ("RUN-20261006-90001", "mes-a", "run_number", "erp-a", "batch_id")]
    assert out[0]["method"] == ia.EXACT_METHOD and out[0]["confidence"] == 0.95


def test_find_owners_ignores_pattern_rules_and_rules_without_a_shape():
    pattern_rule = _exact_rule(id="ir-p", method="value_core_match", source_pattern="X-{id}",
                               target_pattern="PLANT-2026-0{id}", provenance="50 of 50 pairs")
    shapeless = _exact_rule(id="ir-s", provenance=None)
    assert it.find_owners("run RUN-20261006-90001 and X-2847", [pattern_rule, shapeless]) == []
    assert it.find_owners("", [_exact_rule()]) == []
    assert it.find_owners("run RUN-20261006-90001", []) == []


def test_find_owners_dedupes_a_token_across_rules_and_caps():
    rules = [_exact_rule(), _exact_rule(id="ir-2", target_field="batch_number")]
    out = it.find_owners("RUN-20261006-90001 RUN-20261006-90001", rules)
    assert len(out) == 2 and {o["to_field"] for o in out} == {"batch_id", "batch_number"}
    many = " ".join(f"RUN-2026100{i}-9000{i}" for i in range(1, 10)) + " RUN-20261010-90010"
    assert len(it.find_owners(many, [_exact_rule()])) == it.TRANSLATION_CAP


def test_render_owners_names_both_systems_and_the_first_then_widen_rule():
    assert it.render_owners([]) == ""
    text = it.render_owners(it.find_owners("production run RUN-20261006-90001", [_exact_rule()]))
    assert text.startswith(it.OWNERS_HEADER)
    assert ("- RUN-20261006-90001 → mes-a.run_number = erp-a.batch_id "
            "[value_exact_match, confidence 0.95]") in text
    assert "first" in it.OWNERS_HEADER and "no match" in it.OWNERS_HEADER


def _rows(rule):
    return [{"system_id": "mes-a", "identity_rules": [rule]},
            {"system_id": "erp-a", "identity_rules": [rule]}]


# Both ERP tables carry a ``batch_id`` column: the shipment line holds MES run
# numbers, the lot-tracking row holds MES work-order numbers (ADR-0087
# amendment 2026-10-07). IdentityRules store leaf column names only.
_RUN_SHAPE, _WO_SHAPE = "AAA-99999999-99999", "AA-99999999-99999"


def _wo_rule(**over):
    return _exact_rule(**{"id": "ir-wo", "concept": "batch", "source_field": "work_order_number",
                          "provenance": ("12 exact values of work_orders.work_order_number exist in "
                                         f"erp-a lot_mes_tracking.batch_id (membership-probed); both "
                                         f"columns shape {_WO_SHAPE}; concept batch"), **over})


def _jp(jid, fs, ff, ts, tf, method=ia.EXACT_METHOD):
    return {"id": jid, "from_system": fs, "from_field": ff, "to_system": ts, "to_field": tf,
            "method": method}


_RUN_JP = _jp("jp-run", "mes-a", "sch_m.production_runs.run_number",
              "erp-a", "sch_e.shipment_items.batch_id")
_WO_JP = _jp("jp-wo", "mes-a", "sch_m.work_orders.work_order_number",
             "erp-a", "sch_e.lot_mes_tracking.batch_id")
_WO_JP_REV = _jp("jp-wo-r", "erp-a", "sch_e.lot_mes_tracking.batch_id",
                 "mes-a", "sch_m.work_orders.work_order_number")


def test_owners_take_the_table_from_the_exact_join_path_of_the_same_columns():
    """A work-order token names the lot-tracking table, a run token the shipment line."""
    wo_reverse = _wo_rule(id="ir-wo-r", source_system="erp-a", source_field="batch_id",
                          target_system="mes-a", target_field="work_order_number")
    rows = [{"system_id": "mes-a", "identity_rules": [_exact_rule(), _wo_rule()],
             "join_paths": [_RUN_JP, _WO_JP]},
            {"system_id": "erp-a", "identity_rules": [wo_reverse, _exact_rule()],
             "join_paths": [_WO_JP_REV, _RUN_JP]}]
    block = orch._identifier_translation_block(
        "which lots went into WO-20261001-10001, and run RUN-20261001-90001?", rows)
    owner_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert owner_lines == [
        "- RUN-20261001-90001 → mes-a.sch_m.production_runs.run_number = "
        "erp-a.sch_e.shipment_items.batch_id [value_exact_match, confidence 0.95]",
        "- WO-20261001-10001 → erp-a.sch_e.lot_mes_tracking.batch_id = "
        "mes-a.sch_m.work_orders.work_order_number [value_exact_match, confidence 0.95]",
    ]  # the two directions of the work-order join are ONE owner; with equal tier and
    # confidence the total order picks the smaller source direction,
    # not whichever copy the graph returned first


def test_owners_fall_back_to_the_rule_leaves_without_a_matching_exact_join_path():
    pattern_jp = _jp("jp-fk", "mes-a", "sch_m.work_orders.work_order_number",
                     "erp-a", "sch_e.lot_mes_tracking.batch_id", method="foreign_key_match")
    other_column = _jp("jp-x", "mes-a", "sch_m.work_orders.wo_ref",
                       "erp-a", "sch_e.lot_mes_tracking.batch_id")
    out = it.find_owners("order WO-20261001-10001", [_wo_rule()], [pattern_jp, other_column])
    assert [(o["from_field"], o["to_field"]) for o in out] == [("work_order_number", "batch_id")]
    assert it.find_owners("order WO-20261001-10001", [_wo_rule()]) == out


def test_owners_header_states_the_shape_never_that_the_value_exists():
    """A shape match is not a membership test (ADR-0087), so an
    unrelated identifier of the same shape must not be presented as a key in both."""
    assert "key in BOTH" not in it.OWNERS_HEADER
    assert "does not show the value exists" in it.OWNERS_HEADER
    stranger = it.render_owners(it.find_owners("what about ZZZ-11111111-22222?", [_exact_rule()]))
    assert stranger.startswith(it.OWNERS_HEADER) and "- ZZZ-11111111-22222 → mes-a.run_number" in stranger


def _many_owners(n=5):
    return it.find_owners(" ".join(f"RUN-2026100{i}-9000{i}" for i in range(1, n + 1)), [_exact_rule()])


def test_render_owners_falls_back_to_a_compact_line_naming_every_candidate_system():
    owners = _many_owners()
    full = it.render_owners(owners)
    assert it.render_owners(owners, max_chars=len(full)) == full
    compact = it.render_owners(owners, max_chars=len(full) - 1)
    # The smallest form is the header plus ONE compact line, no per-run lines.
    assert len(compact) <= len(full) - 1 and compact.startswith(it.OWNERS_HEADER)
    assert compact.count("- RUN-") == 0
    body = [ln for ln in compact.splitlines() if ln.startswith("- candidate owner systems")]
    assert len(body) == 1
    for sid in {o["from_system"] for o in owners} | {o["to_system"] for o in owners}:
        assert sid in body[0]
    assert it.render_owners(owners, max_chars=len(it.OWNERS_HEADER) + 1) == ""
    assert it.render_owners(owners, max_chars=-5) == ""


def test_owners_block_sizes_are_the_whole_and_the_compact_smallest_block():
    """The route seed reserves room from these, so they must be exactly what
    render_owners can return: every full line, and the header plus the one
    compact candidate-systems line."""
    owners = _many_owners()
    whole, smallest = it.owners_block_sizes(owners)
    assert whole == len(it.render_owners(owners))
    small = it.render_owners(owners, max_chars=smallest)
    assert small and small.count("- RUN-") == 0 and len(small) == smallest
    assert it.render_owners(owners, max_chars=smallest - 1) == ""
    one = owners[:1]
    assert it.owners_block_sizes(one) == (len(it.render_owners(one)),) * 2
    assert it.owners_block_sizes([]) == (0, 0)


def _seed_query(rule):
    inventory = [{"system_id": s, "system_name": s.upper(), "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": None} for s in ("mes-a", "erp-a")]
    rows = [{**inv, "mapped_concepts": [], "shared_concepts": [], "join_paths": [],
             "value_mappings": [], "identity_rules": [rule]} for inv in inventory]

    def fake_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return fake_query


def test_route_seed_with_owners_stays_inside_the_seed_budget(monkeypatch):
    """The owners block used to be appended after the budget check, so a
    near-budget seed plus several owners could cross it. The composed return
    must stay within the budget at every pressure, trimming the owners block to
    its compact smallest form and then omitting it."""
    q = _seed_query(_exact_rule())
    question = " ".join(f"RUN-2026100{i}-9000{i}" for i in range(1, 6))
    plain = orch.build_route_seed(query=q)
    full = orch.build_route_seed(query=q, question=question)
    assert full.startswith(plain) and it.OWNERS_HEADER in full and it.OWNERS_OMITTED[:8] not in full
    smallest = it.owners_block_sizes(it.find_owners(
        question, orch._seed_identity_rules(q(orch._ROUTE_SEED_CYPHER))))[1]
    # Room for the compact smallest form but not the full per-run lines: the
    # block renders compact (every candidate system, no per-run line), ≤ budget.
    budget = len(plain) + smallest + 40
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = orch.build_route_seed(query=q, question=question)
    assert len(seed) <= budget and it.OWNERS_HEADER in seed
    assert "candidate owner systems" in seed and "- RUN-" not in seed.split(it.OWNERS_HEADER, 1)[1]
    # No room for even the compact form: the whole owners block is left out and
    # the composed return still fits.
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(plain) + 40)
    seed = orch.build_route_seed(query=q, question=question)
    assert len(seed) <= len(plain) + 40 and it.OWNERS_HEADER not in seed


def test_route_seed_trims_table_qualified_owners_to_the_budget(monkeypatch):
    """The two 2026-10-07 changes together: owner endpoints come from the exact
    JoinPath (table-qualified, longer lines) AND the block stays inside the
    budget, keeping the first lines that fit plus one omitted line.

    The join paths carry provenance, as the exact pass writes them (tier 1):
    since 2026-10-08 the fit keeps room for the owners block by trimming
    tier-2 detail first, and here nothing below tier 1 is left to trim, so the
    block itself is what gives way."""
    q = _seed_query(_exact_rule())
    rows = q(orch._ROUTE_SEED_CYPHER)
    rows[0]["identity_rules"] = [_exact_rule(), _wo_rule()]
    rows[0]["join_paths"] = [{**_RUN_JP, "provenance": _exact_rule()["provenance"]},
                             {**_WO_JP, "provenance": _wo_rule()["provenance"]}]
    question = "lots for WO-20261001-10001, and run RUN-20261001-90001?"
    full = orch.build_route_seed(query=q, question=question)
    run_line = ("- RUN-20261001-90001 → mes-a.sch_m.production_runs.run_number = "
                "erp-a.sch_e.shipment_items.batch_id")
    wo_line = ("- WO-20261001-10001 → mes-a.sch_m.work_orders.work_order_number = "
               "erp-a.sch_e.lot_mes_tracking.batch_id")
    assert run_line in full and wo_line in full and it.OWNERS_OMITTED[:8] not in full
    smallest = it.owners_block_sizes(it.find_owners(
        question, orch._seed_identity_rules(rows), orch._seed_rule_dicts(rows, "join_paths")))[1]
    plain = orch.build_route_seed(query=q)  # same rows, no question → no tail
    # Budget below the whole table-qualified block but above the compact form:
    # the smallest compact line, naming every candidate system, replaces
    # the full per-identifier lines, and the composed return stays within budget.
    budget = len(plain) + smallest + 40
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = orch.build_route_seed(query=q, question=question)
    assert len(seed) <= budget
    assert run_line not in seed and wo_line not in seed
    compact = [ln for ln in seed.splitlines() if ln.startswith("- candidate owner systems")]
    assert len(compact) == 1 and "mes-a" in compact[0] and "erp-a" in compact[0]


def test_seed_block_appends_owners_after_translations_and_stays_empty_otherwise():
    block = orch._identifier_translation_block("production run RUN-20261006-90001", _rows(_exact_rule()))
    assert it.OWNERS_HEADER in block and it.HEADER not in block  # an exact join translates to itself
    assert orch._identifier_translation_block("no identifiers here", _rows(_exact_rule())) == ""
    assert orch._identifier_translation_block(None, _rows(_exact_rule())) == ""


def test_seed_block_carries_both_blocks_when_both_apply():
    pattern = {"id": "ir-1", "concept": "batch", "source_system": "mes-x", "source_field": "batch_number",
               "source_pattern": "X-{id}", "target_system": "erp-x", "target_field": "material_lot",
               "target_pattern": "PLANT-2026-0{id}", "confidence": 0.28, "method": "value_core_match",
               "provenance": "50 of 50 sampled value pairs"}
    rows = [{"system_id": "mes-x", "identity_rules": [pattern, _exact_rule()]}]
    block = orch._identifier_translation_block("batch X-2847 and run RUN-20261006-90001", rows)
    assert block.index(it.HEADER) < block.index(it.OWNERS_HEADER)
    assert "PLANT-2026-02847" in block and "mes-a.run_number = erp-a.batch_id" in block


@pytest.mark.parametrize("token,shape", [
    ("RUN-20261006-90001", "AAA-99999999-99999"),
    ("LOT-IND-BARAL7075150-H2240", "AAA-AAA-AAAAA9999999-A9999"),
    ("CMX-441", "AAA-999"),
])
def test_identifier_tokens_and_their_shapes(token, shape):
    assert it.identifier_tokens(f"about {token} today") == [token]
    assert ia.literal_shape(token) == shape
    assert it.identifier_tokens("plain words 2026 and 90001") == []  # one segment is not an identifier
