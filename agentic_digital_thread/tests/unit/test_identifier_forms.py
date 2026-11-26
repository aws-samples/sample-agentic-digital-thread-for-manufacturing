# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic IDENTIFIER FORMS in the route seed.

Live check 2026-10-15 (44 runs): one target system kept a single
concept under two identifiers. Its shipment line held one source system's run
numbers and its lot-tracking row held the same source system's work-order
numbers, and the graph knew both as measured exact joins. The seed listed the
two IdentityRules as ``source.run_number -> target.batch_id`` and
``source.work_order_number -> target.batch_id`` (rules store leaf columns), so
they read as one target field that takes either form, and the prompt asked for
ONE ``join_rule`` per hop. When a question needed both target tables (which
lot a batch drew, then who received the batches that drew it), the
orchestrator delegated the whole chain with the work-order form; the target's
own list of further batches came back in that form and its shipment lookup
returned no rows (23 of 30 runs). In 5 of those the target then widened the
scope to a whole product, supplier or date range and reported a total above
$1M.

The seed now states, from the graph's rules alone, which field holds which
form and which system pairs the forms per item, and the standing prompt says
how to carry and recover forms. Nothing here names a system, table or concept
in engine code: every line is built from the graph's own rules and join paths.

Per-item evidence: two exact joins alone do not show that one item
carries both forms, so a line needs a pairing JoinPath (a key join, a same-row
set or a steward-confirmed join) or one shared table on one side. On the
recorded rehearsal graph the ERP has no such path between its two tables; the
MES pairs them (``work_orders.production_run_id`` is a foreign key to
``production_runs``), so the line says to ask the source. Sources are merged
before the line cap, every omission is stated, and a forms-only tail has the
same bounded rescue as a deterministic translation.
"""
import json
import logging
import re

import pytest

from agents import identifier_translation as it
from agents import identity_affinity as ia
from agents import orchestrator as orch

_RUN_SHAPE, _WO_SHAPE = "AAA-99999999-99999", "AA-99999999-99999"


def _reasoning(source, target_system, target, shape, concept):
    return (f"75 exact values of {source} exist in {target_system} {target} "
            f"(membership-probed); both columns shape {shape}; concept {concept}; "
            f"84 and 200 distinct values sampled")


def _rule(rid, src, sf, tgt, tf, shape, *, concept="batch", src_table="t_src",
          tgt_table="t_tgt", **over):
    rule = {"id": rid, "concept": concept, "source_system": src, "source_field": sf,
            "target_system": tgt, "target_field": tf, "source_pattern": "",
            "target_pattern": "", "method": ia.EXACT_METHOD, "confidence": 0.85,
            "provenance": _reasoning(f"{src_table}.{sf}", tgt, f"{tgt_table}.{tf}", shape,
                                     concept)}
    rule.update(over)
    return rule


def _jp(rule, from_field, to_field, method=ia.EXACT_METHOD):
    return {"id": "jp-" + rule["id"], "from_system": rule["source_system"],
            "from_field": from_field, "to_system": rule["target_system"],
            "to_field": to_field, "transform": "identity", "method": method,
            "confidence": 0.85, "provenance": rule["provenance"]}


# The live pair, with neutral system ids: two exact rules from ONE source system
# into ONE target system for ONE concept, landing on two target tables whose
# column has the same name.
RUN_RULE = _rule("ir-mes-a-run", "mes-a", "run_number", "erp-a", "batch_id", _RUN_SHAPE,
                 src_table="production_runs", tgt_table="shipment_items")
WO_RULE = _rule("ir-mes-a-wo", "mes-a", "work_order_number", "erp-a", "batch_id", _WO_SHAPE,
                src_table="work_orders", tgt_table="lot_mes_tracking")
RUN_JP = _jp(RUN_RULE, "sch_m.production_runs.run_number", "sch_e.shipment_items.batch_id")
WO_JP = _jp(WO_RULE, "sch_m.work_orders.work_order_number", "sch_e.lot_mes_tracking.batch_id")


def _internal(system, rid, from_field, to_field, method="foreign_key_match"):
    """An internal JoinPath as the seed reads it (table-qualified endpoints)."""
    return {"id": rid, "from_system": system, "from_field": from_field, "to_system": system,
            "to_field": to_field, "transform": "direct", "method": method}


# The source side's per-item pairing: a work order's foreign key to its run.
PAIR_JP = _internal("mes-a", "jp-internal-mes-a-work_orders-production_run_id-production_runs",
                    "sch_m.work_orders.production_run_id",
                    "sch_m.production_runs.production_run_id")
PAIRING = "production_runs.production_run_id = work_orders.production_run_id"

# The recorded rehearsal graph (2026-10-15; ``erp-joinpaths-after.json`` and
# ``restore/mes-ind-joinpaths-after.json``, schemas renamed): every internal
# ERP join touching the two batch tables, and the plant MES's internal joins.
RECORDED_TARGET_JOINS = [
    _internal("erp-a", f"jp-e{i}", f"sch_e.{a}", f"sch_e.{b}", m) for i, (a, b, m) in enumerate([
        ("lot_mes_tracking.lot_id", "material_lots.lot_id", "foreign_key_match"),
        ("invoices.shipment_id", "shipment_items.shipment_id", "value_overlap"),
        ("bill_of_materials.product_id", "shipment_items.product_id", "value_overlap"),
        ("lot_mes_tracking.lot_id", "lot_production_usage.lot_id", "value_overlap"),
        ("inventory_items.product_id", "shipment_items.product_id", "value_overlap"),
        ("invoice_line_items.product_id", "shipment_items.product_id", "value_overlap"),
        ("shipment_items.product_id", "products.product_id", "foreign_key_match"),
        ("order_line_items.product_id", "shipment_items.product_id", "value_overlap"),
        ("shipment_items.shipment_id", "shipments.shipment_id", "foreign_key_match"),
        ("shipment_items.line_item_id", "order_line_items.line_item_id", "foreign_key_match"),
        ("inventory_transactions.reference_id", "shipment_items.shipment_id", "value_overlap")])]
RECORDED_SOURCE_JOINS = [
    _internal("mes-a", f"jp-m{i}", f"sch_m.{a}", f"sch_m.{b}", m) for i, (a, b, m) in enumerate([
        ("work_orders.work_order_id", "work_orders.work_order_number", "same_row_identifier_set"),
        ("material_consumption.work_order_id", "quality_checks.work_order_id", "value_overlap"),
        ("production_events.work_order_id", "work_orders.work_order_id", "foreign_key_match"),
        ("production_lines.line_id", "production_lines.line_name", "same_row_identifier_set"),
        ("downtime_events.line_id", "production_lines.line_id", "foreign_key_match"),
        ("material_consumption.work_order_id", "work_orders.work_order_id", "foreign_key_match"),
        ("quality_checks.work_order_id", "work_orders.work_order_id", "foreign_key_match"),
        ("downtime_events.work_order_id", "work_orders.work_order_id", "foreign_key_match")])
] + [PAIR_JP]
JPS = [RUN_JP, WO_JP, PAIR_JP]

# The module question that failed live: no identifier in it, two target tables.
QUESTION = ("Which customers received parts from any batch that drew the same supplier lot "
            "as the batch that failed bore inspection? For each customer give the units "
            "received and the value of the parts, then the total units and the total value.")


def _seed_query(rules, join_paths, systems=("erp-a", "mes-a")):
    inventory = [{"system_id": s, "system_name": s.upper(), "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": None} for s in systems]
    rows = [{**inv, "mapped_concepts": [], "shared_concepts": [],
             "join_paths": [jp for jp in join_paths
                            if inv["system_id"] in (jp["from_system"], jp["to_system"])],
             "value_mappings": [],
             "identity_rules": [r for r in rules
                                if inv["system_id"] in (r["source_system"], r["target_system"])]}
            for inv in inventory]

    def fake_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return fake_query


def _form_lines(text: str) -> list[str]:
    if it.FORMS_HEADER not in text:
        return []
    out = []
    for line in text.split(it.FORMS_HEADER, 1)[1].splitlines():
        if not line.startswith("- "):
            break
        out.append(line)
    return out


# --------------------------------------------------------------------------- #
# The reproduction: the seed the live runs got
# --------------------------------------------------------------------------- #
def test_the_rule_summaries_alone_read_as_one_target_field_taking_either_form():
    """What the orchestrator had before: both rule summaries end on the same
    bare target field, so nothing paired a form with a target table except the
    join paths, one hop at a time."""
    seed = orch.build_route_seed(query=_seed_query([RUN_RULE, WO_RULE], JPS), question=QUESTION)
    payload = json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]
    targets = {(r["source_field"], r["target_system"], r["target_field"])
               for s in payload["systems"]
               for r in (s.get("rules") or {}).get("identity_rules") or []}
    assert targets == {("run_number", "erp-a", "batch_id"),
                       ("work_order_number", "erp-a", "batch_id")}


LINE = ("- erp-a batch, from mes-a: "
        f"sch_e.lot_mes_tracking.batch_id holds work_orders.work_order_number values ({_WO_SHAPE}); "
        f"sch_e.shipment_items.batch_id holds production_runs.run_number values ({_RUN_SHAPE}); "
        f"per item the source pairs them by {PAIRING}, so ask it for the other form")


def test_the_seed_says_which_target_field_holds_which_form_and_who_pairs_them():
    """One deterministic line: the field that holds each form, the source field
    it comes from, its measured shape, and the source's key join that pairs
    the forms per item."""
    seed = orch.build_route_seed(query=_seed_query([RUN_RULE, WO_RULE], JPS), question=QUESTION)
    assert _form_lines(seed) == [LINE]
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET


def test_the_header_is_generic_and_points_at_the_standing_rule():
    assert it.FORMS_HEADER.startswith("IDENTIFIER FORMS (deterministic")
    assert "the key join that pairs the forms per item" in it.FORMS_HEADER
    assert "follow the IDENTIFIER FORMS rule" in it.FORMS_HEADER
    # Generic text: no system id, table, concept or identifier literal.
    assert not re.search(r"\b[A-Z]{2,4}-\d", it.FORMS_HEADER)
    for literal in ("batch", "run_number", "work_order", "shipment", "lot_"):
        assert literal not in it.FORMS_HEADER


# --------------------------------------------------------------------------- #
# A line needs per-item pairing evidence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("extra", ["none", "value overlap", "target joins elsewhere",
                                   "llm-proposed join", "other system's key join"])
def test_two_exact_joins_over_unpaired_tables_give_no_line(extra):
    """Two exact rules into two target tables prove only that each target
    field holds some source values; with no key join pairing the source rows
    (or the target rows) per item there is no line."""
    paths = {
        "none": [],
        "value overlap": [{**PAIR_JP, "method": "value_overlap"}],
        "target joins elsewhere": RECORDED_TARGET_JOINS,
        "llm-proposed join": [{**PAIR_JP, "method": "onboarding-llm"}],
        "other system's key join": [{**PAIR_JP, "from_system": "mes-b", "to_system": "mes-b"}],
    }[extra]
    assert it.find_field_forms([RUN_RULE, WO_RULE], [RUN_JP, WO_JP, *paths], QUESTION) == []


def test_the_recorded_graph_pairs_the_forms_on_the_source_side_only():
    """The recorded rehearsal graph: no ERP join links its lot-tracking row to
    its shipment line per item (they meet only through product-level
    overlaps), while the MES work order's key join to its run pairs them."""
    rules, joins = [RUN_RULE, WO_RULE], [RUN_JP, WO_JP, *RECORDED_TARGET_JOINS]
    assert it.find_field_forms(rules, joins, QUESTION) == []
    lines = it.find_field_forms(rules, joins + RECORDED_SOURCE_JOINS, QUESTION)
    assert [(line["pairer"], line["pairing"]) for line in lines] == [("source", (PAIRING,))]
    assert _form_lines(it.render_forms(lines)) == [LINE]
    assert lines[0]["rule_ids"] == {RUN_RULE["id"], WO_RULE["id"], RUN_JP["id"], WO_JP["id"],
                                    PAIR_JP["id"]}


def test_a_column_name_guess_does_not_pair_the_forms_per_item():
    """A foreign_key_match pairing JoinPath whose basis is column_name_pattern
    is a bare naming guess: it never proves two tables' rows pair per item, so
    the source cannot be named the pairer and no line is produced. A legacy row
    without a basis keeps pairing (the recorded-graph test above)."""
    guess_pair = {**PAIR_JP, "basis": "column_name_pattern"}
    rules = [RUN_RULE, WO_RULE]
    joins = [RUN_JP, WO_JP, *RECORDED_TARGET_JOINS,
             *[jp for jp in RECORDED_SOURCE_JOINS if jp is not PAIR_JP], guess_pair]
    assert it.find_field_forms(rules, joins, QUESTION) == []
    # The same join with a declared-FK basis still pairs.
    declared_pair = {**PAIR_JP, "basis": "declared_foreign_key"}
    joins_ok = [RUN_JP, WO_JP, *RECORDED_TARGET_JOINS,
                *[jp for jp in RECORDED_SOURCE_JOINS if jp is not PAIR_JP], declared_pair]
    lines = it.find_field_forms(rules, joins_ok, QUESTION)
    assert [(line["pairer"], line["pairing"]) for line in lines] == [("source", (PAIRING,))]


def test_a_target_side_key_join_names_the_target_as_the_pairer():
    target_pair = _internal("erp-a", "jp-e-pair", "sch_e.shipment_items.tracking_id",
                            "sch_e.lot_mes_tracking.tracking_id")
    lines = it.find_field_forms([RUN_RULE, WO_RULE], [RUN_JP, WO_JP, target_pair], QUESTION)
    assert [(line["pairer"], line["pairing"]) for line in lines] == [
        ("holder", ("lot_mes_tracking.tracking_id = shipment_items.tracking_id",))]
    assert _form_lines(it.render_forms(lines))[0].endswith(
        "; erp-a pairs them per item by "
        "lot_mes_tracking.tracking_id = shipment_items.tracking_id")


def test_both_forms_in_one_source_table_pair_on_one_row():
    run_jp = _jp(RUN_RULE, "sch_m.work_orders.run_number", "sch_e.shipment_items.batch_id")
    lines = it.find_field_forms([RUN_RULE, WO_RULE], [run_jp, WO_JP], QUESTION)
    assert [(line["pairer"], line["pairing"]) for line in lines] == [("source", ())]
    assert _form_lines(it.render_forms(lines))[0].endswith(
        "; per item the source holds them on one row, so ask it for the other form")


# --------------------------------------------------------------------------- #
# find_field_forms: built from the rules, for any system pair
# --------------------------------------------------------------------------- #
def test_any_system_pair_and_concept_get_the_same_treatment():
    a = _rule("ir-plm-x-1", "plm-x", "item_code", "erp-y", "part_ref", "AAAA-9999",
              concept="product", src_table="items", tgt_table="order_lines")
    b = _rule("ir-plm-x-2", "plm-x", "drawing_no", "erp-y", "part_ref", "AA9999",
              concept="product", src_table="drawings", tgt_table="stock_moves")
    jps = [_jp(a, "p.items.item_code", "e.order_lines.part_ref"),
           _jp(b, "p.drawings.drawing_no", "e.stock_moves.part_ref"),
           _internal("plm-x", "jp-p", "p.drawings.item_code", "p.items.item_code")]

    lines = it.find_field_forms([a, b], jps, "which product lines moved last week?")
    assert [(line["system"], line["concept"], line["peers"]) for line in lines] == [
        ("erp-y", "product", ["plm-x"])]
    assert lines[0]["fields"] == [
        ("e.order_lines.part_ref", "items.item_code", "AAAA-9999"),
        ("e.stock_moves.part_ref", "drawings.drawing_no", "AA9999")]


def test_rules_stored_in_both_directions_are_one_line_from_the_shared_name_side():
    reverse_wo = _rule("ir-erp-a-wo", "erp-a", "batch_id", "mes-a", "work_order_number",
                       _WO_SHAPE, src_table="lot_mes_tracking", tgt_table="work_orders")
    reverse_jp = _jp(reverse_wo, "sch_e.lot_mes_tracking.batch_id",
                     "sch_m.work_orders.work_order_number")
    lines = it.find_field_forms([RUN_RULE, WO_RULE, reverse_wo], [*JPS, reverse_jp], QUESTION)
    assert [(line["system"], line["peers"]) for line in lines] == [("erp-a", ["mes-a"])]
    assert len(lines[0]["fields"]) == 2


def _sources(names, holder="erp-a"):
    """Exact rules, exact joins and pairing joins for each source in ``names``."""
    rules, jps = [], []
    for peer in names:
        schema = "sch_" + peer.replace("-", "_")
        run = {**RUN_RULE, "id": f"ir-{peer}-{holder}-run", "source_system": peer,
               "target_system": holder}
        wo = {**WO_RULE, "id": f"ir-{peer}-{holder}-wo", "source_system": peer,
              "target_system": holder}
        rules += [run, wo]
        jps += [_jp(run, f"{schema}.production_runs.run_number", "sch_e.shipment_items.batch_id"),
                _jp(wo, f"{schema}.work_orders.work_order_number",
                    "sch_e.lot_mes_tracking.batch_id"),
                _internal(peer, f"jp-pair-{peer}", f"{schema}.work_orders.production_run_id",
                          f"{schema}.production_runs.production_run_id")]
    return rules, jps


def test_sources_that_differ_only_in_name_share_one_line():
    rules, jps = _sources(["mes-c", "mes-a", "mes-b"])
    lines = it.find_field_forms(rules, jps, QUESTION)
    assert [line["peers"] for line in lines] == [["mes-a", "mes-b", "mes-c"]]
    assert _form_lines(it.render_forms(lines)) == [
        LINE.replace("from mes-a:", "from mes-a, mes-b, mes-c:")]
    smallest = it.forms_block_sizes(lines)[1]
    assert _form_lines(it.render_forms(lines, max_chars=smallest)) == [
        "- erp-a batch, from mes-a, mes-b, mes-c: "
        f"sch_e.lot_mes_tracking.batch_id holds work_order_number ({_WO_SHAPE}); "
        f"sch_e.shipment_items.batch_id holds run_number ({_RUN_SHAPE}); "
        "ask the source for the other form"]


# --------------------------------------------------------------------------- #
# Merge before the cap, never drop a source silently
# --------------------------------------------------------------------------- #
TEN = [f"mes-{i:02d}" for i in range(10)]


def test_more_sources_than_the_cap_all_stay_on_their_one_line():
    rules, jps = _sources(TEN)
    lines = it.find_field_forms(rules, jps, QUESTION)
    rendered = _form_lines(it.render_forms(lines))
    assert len(rendered) == 1 and [peer for peer in TEN if peer not in rendered[0]] == []
    assert len(lines) == 1 and lines[0]["peers"] == TEN and lines.omitted == 0
    smallest = it.render_forms(lines, max_chars=it.forms_block_sizes(lines)[1])
    assert all(peer in smallest for peer in TEN)


def test_lines_past_the_cap_are_counted_in_one_omission_line():
    rules, jps = [], []
    for i in range(it.FORMS_CAP + 2):
        more_rules, more_jps = _sources([f"mes-{i:02d}"], holder=f"erp-{i:02d}")
        rules, jps = rules + more_rules, jps + more_jps
    lines = it.find_field_forms(rules, jps, QUESTION)
    rendered = _form_lines(it.render_forms(lines))
    assert rendered[-1] == "- 2 more identifier-form line(s) omitted"
    assert len(rendered) == it.FORMS_CAP + 1
    assert len(lines) == it.FORMS_CAP and lines.omitted == 2


def test_lines_the_room_cannot_hold_are_counted_too():
    lines = []
    for i in range(3):
        rules, jps = _sources([f"mes-{i:02d}"], holder=f"erp-{i:02d}")
        lines += it.find_field_forms(rules, jps, QUESTION)
    full = it.render_forms(lines)
    # One character short of every full line: every line stays, in compact form.
    all_compact = it.render_forms(lines, max_chars=len(full) - 1)
    assert len(_form_lines(all_compact)) == 3 and "omitted" not in all_compact
    # Short of every compact line: two lines stay and the third is counted.
    two = _form_lines(it.render_forms(lines, max_chars=len(all_compact) - 1))
    assert [line for line in two if "omitted" in line] == [
        it.FORMS_OMITTED.format(n=1).rstrip("\n")]
    assert len([line for line in two if "omitted" not in line]) == 2


@pytest.mark.parametrize("case", [
    "one rule", "two peers", "same target field", "pattern rule", "not exact",
    "two concepts", "one target field takes two forms"])
def test_no_line_when_the_rules_do_not_show_two_forms_in_two_fields(case):
    rules, jps = [RUN_RULE, WO_RULE], list(JPS)
    if case == "one rule":
        rules, jps = [RUN_RULE], [RUN_JP, PAIR_JP]
    elif case == "two peers":
        other = {**WO_RULE, "source_system": "mes-b"}
        rules, jps = [RUN_RULE, other], [RUN_JP, {**WO_JP, "from_system": "mes-b"}, PAIR_JP]
    elif case == "same target field":  # no join path qualifies the leaf: unknown table
        jps = [PAIR_JP]
    elif case == "pattern rule":
        rules = [RUN_RULE, {**WO_RULE, "source_pattern": "WO-{id}", "target_pattern": "B{id}"}]
    elif case == "not exact":
        rules = [RUN_RULE, {**WO_RULE, "method": "onboarding-llm"}]
    elif case == "two concepts":
        rules = [RUN_RULE, {**WO_RULE, "concept": "work-order"}]
    elif case == "one target field takes two forms":
        jps = [RUN_JP, {**WO_JP, "to_field": "sch_e.shipment_items.batch_id"}, PAIR_JP]
    assert it.find_field_forms(rules, jps, QUESTION) == []


def test_only_a_question_naming_the_concept_gets_the_block():
    rules = [RUN_RULE, WO_RULE]
    assert it.find_field_forms(rules, JPS, "Which batches shipped yesterday?")
    assert it.find_field_forms(rules, JPS, "Who received batch RUN-20261011-90001?")
    # An identifier alone is the owners block's job, not this one's.
    assert it.find_field_forms(rules, JPS, "Who received RUN-20261011-90001?") == []
    assert it.find_field_forms(rules, JPS, "Is the spindle vibration trending up?") == []
    assert it.find_field_forms(rules, JPS, None) == []
    assert it.find_field_forms([], JPS, QUESTION) == []


def test_render_forms_degrades_line_by_line_then_to_the_compact_form():
    lines = []
    for i in range(3):
        rules, jps = _sources(["mes-a"], holder=f"erp-{i}")
        lines += it.find_field_forms(rules, jps, QUESTION)
    full = it.render_forms(lines)
    assert full.startswith(it.FORMS_HEADER) and len(_form_lines(full)) == 3
    assert it.render_forms(lines, max_chars=len(full)) == full
    whole, smallest = it.forms_block_sizes(lines)
    assert whole == len(full)
    compact = it.render_forms(lines, max_chars=smallest)
    assert len(compact) == smallest
    assert _form_lines(compact) == [
        f"- erp-0 batch, from mes-a: sch_e.lot_mes_tracking.batch_id holds work_order_number "
        f"({_WO_SHAPE}); sch_e.shipment_items.batch_id holds run_number ({_RUN_SHAPE}); "
        "ask the source for the other form",
        it.FORMS_OMITTED.format(n=2).rstrip("\n")]
    assert it.render_forms(lines, max_chars=smallest - 1) == ""
    assert it.render_forms([]) == "" and it.forms_block_sizes([]) == (0, 0)


# --------------------------------------------------------------------------- #
# The tail: priority and budget
# --------------------------------------------------------------------------- #
def test_the_tail_keeps_forms_after_owners_and_inside_the_room():
    lines = it.find_field_forms([RUN_RULE, WO_RULE], JPS, QUESTION)
    forms = it.render_forms(lines)
    assert orch._compose_identifier_tail("", [], "", None, forms=lines) == forms
    smallest = it.forms_block_sizes(lines)[1]
    assert orch._compose_identifier_tail("", [], "", smallest, forms=lines) == it.render_forms(
        lines, max_chars=smallest)
    assert orch._compose_identifier_tail("", [], "", smallest - 1, forms=lines) == ""
    owners = it.find_owners("RUN-20261011-90001", [RUN_RULE], [RUN_JP])
    rendered_owners = it.render_owners(owners)
    tail = orch._compose_identifier_tail("", owners, "", len(rendered_owners) + len(forms),
                                         forms=lines)
    assert tail == rendered_owners + forms
    # Room for both smallest forms: the forms leave owners their smallest form.
    owners_small = it.owners_block_sizes(owners)[1]
    both = orch._compose_identifier_tail("", owners, "", owners_small + smallest, forms=lines)
    assert both == (it.render_owners(owners, max_chars=owners_small)
                    + it.render_forms(lines, max_chars=smallest))
    # Room for less: the forms outrank the advisory owners.
    tight = orch._compose_identifier_tail("", owners, "", len(forms) + 10, forms=lines)
    assert tight == forms


def test_reserves_include_the_forms_block():
    rows = _seed_query([RUN_RULE, WO_RULE], JPS)(orch._ROUTE_SEED_CYPHER)
    lines = it.find_field_forms([RUN_RULE, WO_RULE], JPS, QUESTION)
    whole, smallest = it.forms_block_sizes(lines)
    assert whole > smallest
    assert orch._tail_room_reserves(QUESTION, rows) == (whole, smallest)
    assert orch._tail_room_reserves("Is the spindle vibration trending up?", rows) == ()


def test_the_forms_block_stays_inside_a_tight_budget(monkeypatch):
    q = _seed_query([RUN_RULE, WO_RULE], JPS)
    plain = orch.build_route_seed(query=q)
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(plain) + 40)
    seed = orch.build_route_seed(query=q, question=QUESTION)
    assert len(seed) <= len(plain) + 40 and it.FORMS_HEADER not in seed


def _workshop_graph(wf, ids):
    """The workshop fixture's graph plus each plant MES's own work-order to run
    key join, as onboarding writes it from the site run view."""
    base = wf.graph(ids)
    rows = base(orch._ROUTE_SEED_CYPHER)
    for row in rows:
        entry = wf.MANIFEST[row["system_id"]]
        if entry["system_type"] == "MES":
            schema = entry.get("schema") or row["system_id"].replace("-", "_")
            row["join_paths"] = [*row["join_paths"], _internal(
                row["system_id"], f"jp-internal-{row['system_id']}-work_orders-production_runs",
                f"{schema}.work_orders.production_run_id",
                f"{schema}.production_runs.production_run_id")]

    def fake_query(cypher, *args, **kwargs):
        return rows if cypher == orch._ROUTE_SEED_CYPHER else base(cypher, *args, **kwargs)
    return fake_query


def test_workshop_scale_seed_delivers_the_forms_without_losing_a_rule_or_a_system():
    from tests.unit import _route_seed_workshop_fixture as wf
    for state in sorted(wf.STATES):
        ids = wf.STATES[state]
        seed = orch.build_route_seed(query=_workshop_graph(wf, ids), question=QUESTION)
        payload, tail = wf.decode(seed)
        kept = {rule.get("id") for s in payload["systems"]
                for rules in (s.get("rules") or {}).values() for rule in rules}
        tier1 = {row["id"] for pair in wf.tier1_pairs(ids) for row in pair}
        assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET, state
        assert tier1 <= kept, state
        assert len(payload["systems"]) == len(ids), state
        lines = _form_lines(tail)
        assert len(lines) == 1 and "lot_mes_tracking.batch_id holds" in lines[0], state
        assert "shipment_items.batch_id holds" in lines[0] and "ask" in lines[0], state


def test_the_workshop_fixture_without_the_source_key_join_gets_no_line():
    from tests.unit import _route_seed_workshop_fixture as wf
    assert it.FORMS_HEADER not in wf.build_seed(wf.STATES["module 6"], QUESTION)


# --------------------------------------------------------------------------- #
# A forms-only tier-1 wall has a bounded rescue
# --------------------------------------------------------------------------- #
_GUIDANCE = ("Plant execution records: work orders, runs, quality checks and downtime. "
             "Ask in plain words; the agent knows its own tables. ") * 4


def _wall_query(unrelated_per_source: int):
    """Eleven systems: one target holding both forms from ten sources, each
    with its pairing join and long guidance, plus ``unrelated_per_source``
    tier-1 exact rules on another concept between neighbouring sources."""
    rules, jps = _sources(TEN)
    for i, peer in enumerate(TEN):
        nxt = TEN[(i + 1) % len(TEN)]
        for k in range(unrelated_per_source):
            rule = _rule(f"ir-{peer}-tag-{k}", peer, f"tag_ref_{k}", nxt, f"asset_code_{k}",
                         "AA-999", concept="equipment-id", src_table="machines",
                         tgt_table="assets")
            rules.append(rule)
            jps.append(_jp(rule, f"m.machines.tag_ref_{k}", f"m.assets.asset_code_{k}"))
    systems = ("erp-a", *TEN)
    inventory = [{"system_id": s, "system_name": s.upper(), "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": _GUIDANCE} for s in systems]
    rows = [{**inv, "mapped_concepts": [], "shared_concepts": [], "value_mappings": [],
             "join_paths": [jp for jp in jps
                            if inv["system_id"] in (jp["from_system"], jp["to_system"])],
             "identity_rules": [r for r in rules
                                if inv["system_id"] in (r["source_system"], r["target_system"])]}
            for inv in inventory]

    def fake_query(cypher, *_args, **_kwargs):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    producing = {r["id"] for r in rules if r["concept"] == "batch"}
    producing |= {jp["id"] for jp in jps if "-tag-" not in jp["id"]}
    return fake_query, producing


def _payload(seed: str) -> dict:
    return json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]


def _kept_ids(seed: str) -> set:
    return {rule.get("id") for s in _payload(seed)["systems"]
            for rules in (s.get("rules") or {}).values() for rule in rules}


def test_a_forms_only_tier1_wall_yields_unrelated_rules_for_the_block(monkeypatch, caplog):
    query, producing = _wall_query(unrelated_per_source=3)
    monkeypatch.setattr(orch, "_yield_rules_for_forms", lambda *a, **k: None, raising=False)
    walled = orch.build_route_seed(query=query, question=QUESTION)
    monkeypatch.undo()
    assert it.FORMS_HEADER not in walled  # the plain fit left no room and no reserve

    with caplog.at_level(logging.WARNING, logger=orch.logger.name):
        seed = orch.build_route_seed(query=query, question=QUESTION)
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    lines = _form_lines(seed)
    assert lines and all(peer in lines[0] for peer in TEN)
    assert "ROUTE_SEED_FORMS_YIELD" in caplog.text
    removed = _kept_ids(walled) - _kept_ids(seed)
    assert removed and all("-tag-" in rid for rid in removed)  # only unrelated rules gave way
    assert producing & _kept_ids(walled) <= _kept_ids(seed)  # no producing rule removed
    assert len(_payload(seed)["systems"]) == 11


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_a_forms_only_wall_with_nothing_to_yield_is_logged_and_stays_in_budget(caplog):
    query, _producing = _wall_query(unrelated_per_source=0)
    with caplog.at_level(logging.WARNING, logger=orch.logger.name):
        seed = orch.build_route_seed(query=query, question=QUESTION)
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert it.FORMS_HEADER not in seed
    assert "ROUTE_SEED_FORMS_DROPPED" in caplog.text


# --------------------------------------------------------------------------- #
# The standing prompt: several join rules per hop, and the forms recovery
# --------------------------------------------------------------------------- #
def _prompts():
    from agents.workflow_guidance import compose_quick_system_prompt
    return {"full-agent": " ".join(orch._build_orchestrator_prompt("agent").split()),
            "quick-agent": " ".join(compose_quick_system_prompt("agent").split())}


def test_a_hop_may_cite_one_join_rule_per_target_field():
    block = " ".join(orch._MODE_A_DELEGATE_BLOCK.split())
    assert "one per target field the hop uses" in block


@pytest.mark.parametrize("name", ["full-agent", "quick-agent"])
def test_every_mode_a_prompt_carries_the_forms_rule_once(name):
    prompt = _prompts()[name]
    rule = "IDENTIFIER FORMS: when the route seed's IDENTIFIER FORMS block lists the target system"
    assert prompt.count(rule) == 1
    assert "send each listed field only the form it holds" in prompt
    assert ("must carry every form paired per item (get the missing forms first from the "
            "system its line says pairs them)") in prompt
    assert "or be split into one request per field" in prompt
    assert "Never make one form by editing another" in prompt
    assert "never to widen the scope (a product, supplier, customer or date range)" in prompt
    assert "wrong-form miss even without an IDENTIFIER-MISS block" in prompt
    window = prompt[prompt.index(rule):prompt.index(rule) + 1200]
    assert not re.search(r"\b(?:RUN|LOT|WO|SHIP)-\d", window)


def test_mode_b_prompts_do_not_carry_the_forms_rule():
    from agents.workflow_guidance import compose_quick_system_prompt
    for prompt in (orch._build_orchestrator_prompt("orchestrator"),
                   compose_quick_system_prompt("orchestrator")):
        assert "IDENTIFIER FORMS:" not in prompt


def test_the_miss_recovery_uses_the_forms_block_before_a_graph_lookup():
    block = " ".join(orch._MODE_A_DELEGATE_BLOCK.split())
    assert ("When the seed's IDENTIFIER FORMS block shows the token is the form another "
            "field holds, that block IS the lookup") in block
    assert ("ask the system its line says pairs the forms for the form the looked_in "
            "field holds") in block
