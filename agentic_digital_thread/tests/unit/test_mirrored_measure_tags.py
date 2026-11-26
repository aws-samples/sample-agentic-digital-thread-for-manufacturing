# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A measure column takes the measure concept of the column it mirrors across
a measured join.

Workshop rehearsals, 2026-10-14: the ERP's ``order_line_items.line_total``
came back from the onboarding mapper tagged with the money concept (``cost``,
role ``measure``) on some runs and only as an ``invoice`` measure on others,
while the order and invoice header totals got the money tag every time. The
tag comes from one LLM call per batch of tables, and three vocabulary entries
describe an order line's amount columns (the money concept, the sales order
and the invoice), so which one the mapper returns varies from run to run.
Nothing after the mapper added a tag the column's own evidence supports.

Provision now adds it from measured evidence once the internal JoinPaths are
measured (b3a). Column ``y`` takes concept ``C`` from column ``x`` when:

* a measured JoinPath links their two tables (a declared foreign key, or a
  name-pattern join the data bore out), and neither column is the join's own;
* both have exactly the same declared fractional exact-numeric type;
* their names share a word, and when every shared word only says what kind
  of number a column holds (``amount``, ``total``, ``value``, ``qty``...),
  ``y``'s own generation expression names the corroborating column below
  (on the ERP an order's shipping charge and an invoice's amount paid share
  only ``amount``, and without this the payment was tagged as a cost);
* the mapper tagged ``C``, a field-layer concept, on ``x`` in the ``measure``
  role; and
* ``y``'s own table already carries ``C`` in the ``measure`` role on another
  column of that same type, so a concept never enters a table through a join
  alone (the ERP's lot tables hold quantities in the money type, and a lot's
  received quantity must not tag every movement of it).

Entity concepts never spread, integer columns never mirror, an added tag is
never a source (nothing chains), and a row the steward edited at the review
is left alone. Duplicate entries of one concept in a reviewed row count once,
at their highest confidence, whatever their order.

Offline: the database and the graph are fakes. The catalog read and the rule
are proven on the real ERP schema in
``tests/integration/test_mirrored_measure_tags_pg.py``.
"""
from __future__ import annotations

import random
import types

import pytest

import agents.onboard as onboard
from agents.models import ProvisionRequest

SID = "sys-a"
SCHEMA = "schema_a"
MONEY = "numeric(15,2)"
FIELD = "cost"              # a field-layer core concept
OTHER_FIELD = "throughput"  # another field-layer core concept
ENTITY = "invoice"          # an entity-layer core concept

#: Header ``hdr`` with amount columns, line ``ln`` referencing it, and a
#: catalogue ``cat`` the line references for its price.
_TYPES = {
    ("hdr", "hdr_id"): "uuid",
    ("hdr", "subtotal"): MONEY,
    ("hdr", "total_amount"): MONEY,
    ("ln", "ln_id"): "uuid",
    ("ln", "hdr_id"): "uuid",
    ("ln", "cat_id"): "uuid",
    ("ln", "quantity"): "integer",
    ("ln", "unit_price"): MONEY,
    ("ln", "line_total"): MONEY,
    ("cat", "cat_id"): "uuid",
    ("cat", "list_price"): MONEY,
    ("cat", "weight_kg"): "numeric(10,2)",
}
_JOINS = [("ln", "hdr_id", "hdr", "hdr_id"), ("ln", "cat_id", "cat", "cat_id")]
#: The line total is a generated column computed from the line's price.
_GENERATED = {("ln", "line_total"): "(quantity * unit_price)"}


def _rule():
    from agents import measure_equivalence
    return measure_equivalence


def _run(types_=None, joins=None, measure=None, carried=None, targets=None, generated=None):
    me = _rule()
    types_ = dict(_TYPES if types_ is None else types_)
    measure = dict(measure or {})
    carried = dict(carried) if carried is not None else {
        k: set(v) for k, v in measure.items()}
    targets = set(types_) if targets is None else set(targets)
    extra = {} if generated is None else {"generated": dict(generated)}
    return me.mirrored_measure_tags(types_, list(_JOINS if joins is None else joins),
                                    measure, carried, targets, **extra)


def _added(result):
    return sorted((t.table, t.column, t.concept) for t in result)


def _pair(left_name, left_type, right_name, right_type, *, corroborate=True):
    """Tables ``a`` and ``b`` joined on ``a_id``; ``b`` also has a
    ``zz_corr`` column of the right-hand type the mapper tagged ``FIELD``."""
    types_ = {("a", "a_id"): "uuid", ("a", left_name): left_type,
              ("b", "a_id"): "uuid", ("b", right_name): right_type}
    measure = {("a", left_name): {FIELD: 0.9}}
    if corroborate:
        types_[("b", "zz_corr")] = right_type
        measure[("b", "zz_corr")] = {FIELD: 0.7}
    return types_, [("b", "a_id", "a", "a_id")], measure


# --------------------------------------------------------------------------- #
# The pure rule
# --------------------------------------------------------------------------- #
def test_an_order_line_total_takes_the_header_totals_money_concept():
    """The failing run's tags: header total and line price carry the money
    concept, the line total only an entity measure. The two totals share
    only ``total``; the line total's own expression over the line price is
    what admits it."""
    result = _run(measure={("hdr", "total_amount"): {FIELD: 0.9},
                           ("ln", "unit_price"): {FIELD: 0.85},
                           ("ln", "line_total"): {ENTITY: 0.8}},
                  generated=_GENERATED)
    assert _added(result) == [("ln", "line_total", FIELD)]
    (tag,) = result
    assert (tag.source_table, tag.source_column) == ("hdr", "total_amount")
    assert tag.join == ("ln", "hdr_id", "hdr", "hdr_id")
    assert tag.confidence == 0.9


def test_without_its_generation_expression_the_line_total_takes_nothing():
    """``total`` is a generic word: on its own it is no evidence."""
    measure = {("hdr", "total_amount"): {FIELD: 0.9}, ("ln", "unit_price"): {FIELD: 0.85}}
    assert _run(measure=measure) == []
    assert _run(measure=measure, generated={}) == []


def test_the_generation_expression_must_name_the_corroborating_column():
    """An expression over columns that do not carry the concept proves
    nothing about it."""
    measure = {("hdr", "total_amount"): {FIELD: 0.9}, ("ln", "unit_price"): {FIELD: 0.85}}
    assert _run(measure=measure, generated={("ln", "line_total"): "(quantity * 2)"}) == []
    types_ = {**_TYPES, ("ln", "discount"): MONEY}
    assert _run(types_, measure=measure,
                generated={("ln", "line_total"): "(quantity * discount)"}) == []


def test_only_the_receiving_columns_own_expression_counts():
    """A remaining balance computed as ``total - paid`` names the paid
    column; that says the paid column is money of some kind, not that it is
    the concept the balance carries. The payment must not become a cost."""
    types_ = {("ord", "ord_id"): "uuid", ("ord", "shipping_amount"): MONEY,
              ("inv", "inv_id"): "uuid", ("inv", "ord_id"): "uuid",
              ("inv", "total_amount"): MONEY, ("inv", "amount_paid"): MONEY,
              ("inv", "balance_due"): MONEY}
    measure = {("ord", "shipping_amount"): {FIELD: 0.9},
               ("inv", "total_amount"): {FIELD: 0.9}, ("inv", "balance_due"): {FIELD: 0.9}}
    generated = {("inv", "balance_due"): "(total_amount - amount_paid)"}
    assert _run(types_, [("inv", "ord_id", "ord", "ord_id")], measure=measure,
                generated=generated) == []


@pytest.mark.parametrize("left, right", [
    ("shipping_amount", "amount_paid"),
    ("total_amount", "line_total"),
    ("TotalAmount", "lineTotal"),
    ("net_value", "gross_value"),
    ("qty_open", "qty_held"),
    ("quantity_open", "quantity_held"),
    ("number_a1", "number_b2"),
    ("total_amount", "amount_total_due"),
], ids=["amount", "total", "camel case total", "value", "qty", "quantity", "number",
        "two generic words"])
def test_generic_number_words_alone_are_not_a_shared_meaning(left, right):
    types_, joins, measure = _pair(left, MONEY, right, MONEY)
    assert _run(types_, joins, measure=measure, generated={}) == []


def test_a_meaningful_word_beside_a_generic_one_is_enough():
    types_, joins, measure = _pair("net_price_total", MONEY, "line_price_total", MONEY)
    assert _added(_run(types_, joins, measure=measure, generated={})) == [
        ("b", "line_price_total", FIELD)]


@pytest.mark.parametrize("expression, columns", [
    ("(quantity * unit_price)", {"quantity", "unit_price"}),
    ('("Unit Price" * qty)', {"Unit Price", "qty"}),
    ("round((unit_price * (1)::numeric), 2)", {"unit_price"}),
    ("COALESCE(unit_price, 'quantity'::text)", {"unit_price"}),
    ('("say ""hi""" || note)', {'say "hi"'}),
    ("", set()),
], ids=["product", "quoted names", "function and cast", "string literal", "doubled quotes",
        "empty"])
def test_an_expression_names_only_its_tables_columns(expression, columns):
    table_columns = {"quantity", "unit_price", "Unit Price", "qty", 'say "hi"', "line_total"}
    assert _rule().expression_columns(expression, table_columns) == columns


# --------------------------------------------------------------------------- #
# A view column takes its base column's generation expression only when the
# view passes that column (and every column the expression reads) through
# --------------------------------------------------------------------------- #
# A view has no generated columns of its own. Matching a view column to a
# base column by NAME gave a view that selects ``paid_amount AS line_total``
# the base table's unused ``line_total = quantity * unit_price``, and b3a
# then tagged the payment as the line's money. The view's own rewrite rule
# says which base attribute each output reads.

#: Base columns each generated column of the fixture table reads (what
#: pg_depend records for its generation expression).
_FIXTURE_INPUTS = {"line_total": [(2, "quantity"), (3, "unit_price")],
                   "Odd Total": [(5, "Odd Name")]}

#: Which generated columns each fixture view passes through.
_FIXTURE_EXPECTED = {
    "pass": {"line_total", "Odd Total"},       # SELECT * FROM ln
    "alias_qualified": {"line_total"},         # SELECT t.line_total, t.quantity, ... FROM ln t
    "renamed": set(),                          # paid_amount AS line_total
    "computed": set(),                         # (line_total + 0) AS line_total
    "selfjoin": set(),                         # line_total of one row, its inputs of another
    "joined": set(),                           # any JOIN syntax is withheld
    "hidden_input": set(),                     # quantity is not in the view
    "input_renamed": set(),                    # paid_amount AS unit_price
    "subquery": set(),                         # FROM (SELECT * FROM ln) s
    # SELECT lt AS line_total FROM (SELECT line_total AS lt, quantity, ...) s:
    # the inner query's quantity and unit_price are not columns of the view.
    "subquery_hidden": set(),
}


def _view_fixture():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures" / "view_rule_lineage.json").read_text())
    generated = [{**g, "inputs": [{"attnum": a, "name": n} for a, n in _FIXTURE_INPUTS[g["name"]]]}
                 for g in data["generated"]]
    return data["views"], generated


def _fixture_view(pg_major, name):
    views, generated = _view_fixture()
    (view,) = [v for v in views if v["pg_major"] == pg_major and v["view"] == name]
    return view, generated


@pytest.mark.parametrize("pg_major", [15, 16])
@pytest.mark.parametrize("name", sorted(_FIXTURE_EXPECTED))
def test_a_view_column_takes_a_generation_expression_only_through_a_plain_reference(
        pg_major, name):
    view, generated = _fixture_view(pg_major, name)
    got = _rule().view_generated_expressions(view["rule"], view["base_oid"], generated)
    assert set(got) == _FIXTURE_EXPECTED[name]
    exprs = {g["name"]: g["expr"] for g in generated}
    assert all(got[column] == exprs[column] for column in got)


def test_a_view_over_another_relation_takes_nothing():
    view, generated = _fixture_view(16, "pass")
    assert _rule().view_generated_expressions(view["rule"], "1", generated) == {}


@pytest.mark.parametrize("rule", ["", None, "junk", "({QUERY :targetList <>})"],
                         ids=["empty", "none", "junk", "no target list"])
def test_an_unreadable_rule_passes_nothing_through(rule):
    _view, generated = _fixture_view(16, "pass")
    assert _rule().view_generated_expressions(rule, "1", generated) == {}


def test_a_malformed_generated_entry_is_skipped():
    view, generated = _fixture_view(16, "pass")
    broken = [{"name": "line_total"}, None, "junk", {**generated[0], "inputs": "junk"}]
    assert _rule().view_generated_expressions(view["rule"], view["base_oid"],
                                              broken + generated[1:]) == {
        "Odd Total": generated[1]["expr"]}


def test_an_order_line_price_takes_the_catalogue_prices_money_concept():
    """The other way round: the line total carries it, the price does not."""
    result = _run(measure={("cat", "list_price"): {FIELD: 0.7},
                           ("ln", "line_total"): {FIELD: 0.8}})
    assert _added(result) == [("ln", "unit_price", FIELD)]
    (tag,) = result
    assert (tag.source_table, tag.source_column) == ("cat", "list_price")
    assert tag.join == ("ln", "cat_id", "cat", "cat_id")


def test_a_concept_never_enters_a_table_through_a_join_alone():
    """With neither line column tagged, nothing on the line says it holds
    the concept, so nothing is added: the rule repairs, it never invents."""
    assert _run(measure={("hdr", "total_amount"): {FIELD: 0.9},
                         ("cat", "list_price"): {FIELD: 0.7}}) == []


def test_the_corroborating_column_must_have_the_same_declared_type():
    types_ = dict(_TYPES)
    types_[("ln", "unit_price")] = "numeric(10,2)"
    assert _run(types_, measure={("hdr", "total_amount"): {FIELD: 0.9},
                                 ("ln", "unit_price"): {FIELD: 0.85}},
                generated=_GENERATED) == []


def test_the_corroborating_tag_must_be_the_same_concept_in_the_measure_role():
    assert _run(measure={("hdr", "total_amount"): {FIELD: 0.9},
                         ("ln", "unit_price"): {OTHER_FIELD: 0.85}},
                generated=_GENERATED) == []
    assert _run(measure={("hdr", "total_amount"): {FIELD: 0.9}},
                carried={("hdr", "total_amount"): {FIELD},
                         ("ln", "unit_price"): {FIELD}},
                generated=_GENERATED) == []


def test_the_rule_runs_in_both_directions_of_a_join():
    """The line price gives the catalogue's other price the concept the
    catalogue already uses for its sale price."""
    types_ = {**_TYPES, ("cat", "sale_price"): MONEY}
    result = _run(types_, measure={("ln", "unit_price"): {FIELD: 0.8},
                                   ("cat", "sale_price"): {FIELD: 0.8}})
    assert _added(result) == [("cat", "list_price", FIELD)]


def test_an_entity_concept_never_spreads():
    """``invoice [measure]`` on the line total says which record the amount
    belongs to; copying it onto the line price would be wrong."""
    assert _run(measure={("cat", "list_price"): {ENTITY: 0.8},
                         ("ln", "line_total"): {ENTITY: 0.8}}) == []


def test_every_corroborated_field_concept_the_source_carries_spreads():
    result = _run(measure={("hdr", "total_amount"): {FIELD: 0.9, OTHER_FIELD: 0.6},
                           ("ln", "unit_price"): {FIELD: 0.85, OTHER_FIELD: 0.5}},
                  targets={("ln", "line_total")}, generated=_GENERATED)
    assert _added(result) == [("ln", "line_total", FIELD), ("ln", "line_total", OTHER_FIELD)]


def test_integer_columns_never_mirror():
    """Integer columns are counts and keys: an ordered and a shipped count
    share a word and a type and still mean different counts."""
    types_, joins, measure = _pair("units_ordered", "integer", "units_shipped", "integer")
    assert _run(types_, joins, measure=measure) == []


@pytest.mark.parametrize("left, right, mirrored", [
    (MONEY, MONEY, True),
    ("numeric(15,2)", "numeric(10,2)", False),
    ("numeric(15,2)", "numeric(15,4)", False),
    ("numeric(12,0)", "numeric(12,0)", False),
    ("numeric", "numeric", False),
    ("money", "money", True),
    ("NUMERIC(15, 2)", "numeric(15,2)", True),
    ("decimal(9,3)", "decimal(9,3)", True),
    ("double precision", "double precision", False),
    ("text", "text", False),
], ids=["same money type", "other precision", "other scale", "zero scale",
        "unconstrained numeric", "money", "spelling and spaces", "decimal",
        "floating point", "text"])
def test_the_declared_types_must_be_one_fractional_exact_numeric(left, right, mirrored):
    types_, joins, measure = _pair("list_price", left, "unit_price", right)
    assert bool(_run(types_, joins, measure=measure)) is mirrored


@pytest.mark.parametrize("left, right, mirrored", [
    ("list_price", "unit_price", True),
    ("ListPrice", "unitPrice", True),
    ("list_price", "UnitPrice", True),
    ("inlet_temperature", "outlet_pressure", False),
    ("listprice", "unit_price", False),
    ("fee_x", "fee_y", True),
    ("ab_x", "ab_y", False),
], ids=["shared word", "camel case", "mixed styles", "no shared word",
        "a word inside another is not shared", "three letters", "two letters"])
def test_the_names_must_share_a_word(left, right, mirrored):
    types_, joins, measure = _pair(left, MONEY, right, MONEY)
    assert bool(_run(types_, joins, measure=measure)) is mirrored


def test_without_a_corroborating_column_a_shared_word_and_type_are_not_enough():
    types_, joins, measure = _pair("list_price", MONEY, "unit_price", MONEY,
                                   corroborate=False)
    assert _run(types_, joins, measure=measure) == []


def test_a_tag_outside_the_measure_role_is_not_a_source():
    result = _run(measure={("ln", "unit_price"): {FIELD: 0.85}},
                  carried={("hdr", "total_amount"): {FIELD}, ("ln", "unit_price"): {FIELD}})
    assert result == []


def test_a_column_already_carrying_the_concept_in_any_role_gets_nothing():
    result = _run(measure={("hdr", "total_amount"): {FIELD: 0.9},
                           ("ln", "unit_price"): {FIELD: 0.85}},
                  carried={("hdr", "total_amount"): {FIELD}, ("ln", "unit_price"): {FIELD},
                           ("ln", "line_total"): {FIELD}})
    assert result == []


def test_a_column_that_is_not_a_target_gets_nothing_but_still_counts_as_a_source():
    """A row the steward edited at the review keeps exactly what the steward
    confirmed; its confirmed tags still describe its column."""
    types_ = {**_TYPES, ("cat", "sale_price"): MONEY}
    measure = {("ln", "unit_price"): {FIELD: 0.8}, ("cat", "sale_price"): {FIELD: 0.8}}
    assert _run(types_, measure=measure, targets={("ln", "line_total")}) == []
    assert _added(_run(types_, measure=measure, targets={("cat", "list_price")})) == [
        ("cat", "list_price", FIELD)]


def test_a_joins_own_columns_never_mirror():
    """``b.ref_amount`` would otherwise take the concept (same type, same
    word, ``b.zz_corr`` corroborates); it is the join's own column."""
    types_ = {("a", "ref_amount"): MONEY,
              ("b", "ref_amount"): MONEY, ("b", "zz_corr"): MONEY}
    measure = {("a", "ref_amount"): {FIELD: 0.9}, ("b", "zz_corr"): {FIELD: 0.7}}
    assert _run(types_, [("b", "ref_amount", "a", "ref_amount")], measure=measure) == []


def test_a_self_join_is_not_used():
    """``a.net_price`` would otherwise take the concept from ``a.list_price``
    (same type and word, ``a.list_price`` itself corroborates)."""
    types_ = {("a", "a_id"): "uuid", ("a", "parent_id"): "uuid",
              ("a", "list_price"): MONEY, ("a", "net_price"): MONEY}
    assert _run(types_, [("a", "parent_id", "a", "a_id")],
                measure={("a", "list_price"): {FIELD: 0.9}}) == []


def test_an_added_tag_is_never_a_source_so_nothing_chains():
    """``c`` mirrors ``b`` and ``b`` mirrors ``a``; only ``b`` is added. Each
    added tag rests on a tag the mapper (and the review) actually gave."""
    types_ = {("a", "a_id"): "uuid", ("a", "total_amount"): MONEY,
              ("b", "b_id"): "uuid", ("b", "a_id"): "uuid", ("b", "line_total"): MONEY,
              ("b", "b_price"): MONEY,
              ("c", "b_id"): "uuid", ("c", "line_total"): MONEY, ("c", "c_price"): MONEY}
    joins = [("b", "a_id", "a", "a_id"), ("c", "b_id", "b", "b_id")]
    measure = {("a", "total_amount"): {FIELD: 0.9}, ("b", "b_price"): {FIELD: 0.7},
               ("c", "c_price"): {FIELD: 0.7}}
    generated = {("b", "line_total"): "(2 * b_price)", ("c", "line_total"): "(2 * c_price)"}
    assert _added(_run(types_, joins, measure=measure, generated=generated)) == [
        ("b", "line_total", FIELD)]


def test_two_sources_of_one_concept_add_it_once_at_the_higher_confidence():
    types_ = {("a", "a_id"): "uuid", ("a", "total_amount"): MONEY,
              ("z", "z_id"): "uuid", ("z", "total_due"): MONEY,
              ("b", "a_id"): "uuid", ("b", "z_id"): "uuid", ("b", "line_total"): MONEY,
              ("b", "b_price"): MONEY}
    joins = [("b", "z_id", "z", "z_id"), ("b", "a_id", "a", "a_id")]
    result = _run(types_, joins, measure={("a", "total_amount"): {FIELD: 0.6},
                                          ("z", "total_due"): {FIELD: 0.8},
                                          ("b", "b_price"): {FIELD: 0.7}},
                  targets={("b", "line_total")},
                  generated={("b", "line_total"): "(2 * b_price)"})
    (tag,) = result
    assert tag.confidence == 0.8
    assert (tag.source_table, tag.source_column) == ("z", "total_due")


def test_the_result_does_not_depend_on_input_order():
    measure = {("hdr", "total_amount"): {FIELD: 0.9}, ("cat", "list_price"): {FIELD: 0.7},
               ("ln", "unit_price"): {FIELD: 0.8}, ("hdr", "subtotal"): {FIELD: 0.9}}
    first = _run(measure=measure, generated=_GENERATED)
    assert first
    for seed in range(5):
        rng = random.Random(seed)
        items = list(_TYPES.items())
        rng.shuffle(items)
        joins = list(_JOINS)
        rng.shuffle(joins)
        assert _run(dict(items), joins, measure=dict(reversed(list(measure.items()))),
                    generated=_GENERATED) == first


def test_a_join_naming_an_unknown_table_is_ignored():
    assert _run(joins=[("ln", "hdr_id", "nope", "hdr_id")],
                measure={("hdr", "total_amount"): {FIELD: 0.9},
                         ("ln", "unit_price"): {FIELD: 0.85}},
                generated=_GENERATED) == []


# --------------------------------------------------------------------------- #
# b3 records which joins were measured
# --------------------------------------------------------------------------- #
def _ctx(**overrides) -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=SID, system_name="System A", schema=SCHEMA,
                            connection_type="PostgreSQL")
    ctx = onboard._ProvisionState(
        body=body, system_id=SID, system_name="System A", plant="", schema=SCHEMA,
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[],
        committable={FIELD, OTHER_FIELD, ENTITY})
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_b3_keeps_declared_and_measured_joins_and_never_a_guess(monkeypatch):
    joins = [
        {"from_table": "ln", "from_field": "hdr_id", "to_table": "hdr", "to_field": "hdr_id",
         "basis": "declared_foreign_key"},
        {"from_table": "ln", "from_field": "cat_id", "to_table": "cat", "to_field": "cat_id",
         "basis": "column_name_pattern"},
        {"from_table": "ln", "from_field": "zz_id", "to_table": "zz", "to_field": "zz_id",
         "basis": "column_name_pattern"},
    ]
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: True)
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [dict(j) for j in joins])

    def pg(_schema, sql, params=None, **_kw):
        # The data bears out cat_id; zz_id has a value the target lacks.
        missing = 0 if '."cat"' in sql else 1
        return {"rows": [[40, 12, 12, missing, 12, 12]]}
    monkeypatch.setattr(onboard, "pg", pg)
    ctx = _ctx()
    onboard._dp_internal_joinpaths(ctx)
    assert ctx.measured_joins == [("ln", "hdr_id", "hdr", "hdr_id"),
                                  ("ln", "cat_id", "cat", "cat_id")]


# --------------------------------------------------------------------------- #
# b3a: the provision step
# --------------------------------------------------------------------------- #
def _type_rows(types_=None):
    return [[t, c, ty] for (t, c), ty in sorted((types_ or _TYPES).items())]


def _generated_rows(generated=None):
    """The generation expression read's rows for plain tables: one row per
    table, ``[table, relkind, rule text, base oid, generated columns JSON]``
    (a table has no rewrite rule and is its own base)."""
    import json
    by_table: dict = {}
    for (t, c), expr in sorted((_GENERATED if generated is None else generated).items()):
        by_table.setdefault(t, []).append({"attnum": len(by_table.get(t, [])) + 1,
                                           "name": c, "expr": expr, "inputs": []})
    return [[t, "r", "", "0", json.dumps(cols)] for t, cols in sorted(by_table.items())]


def _view_generation_row(table, rule, base_oid, generated):
    import json
    return [table, "v", rule, base_oid, json.dumps(generated)]


def _row(table, column, *tags):
    """A reviewed map row as the mapping checkpoint sends it, untouched:
    ``concepts`` derived from the mapper's role list."""
    mappings = [{"concept": c, "role": r, "confidence": conf} for c, r, conf in tags]
    return {"table": table, "field": f"{table}.{column}", "mappings": mappings,
            "concepts": [{"concept": c, "confidence": conf} for c, _r, conf in tags],
            "concept": tags[0][0] if tags else "needs_review"}


def _rows():
    return [
        _row("hdr", "total_amount", (FIELD, "measure", 0.9)),
        _row("ln", "unit_price", (FIELD, "measure", 0.85)),
        _row("ln", "line_total", (ENTITY, "measure", 0.8)),
        _row("ln", "quantity", (ENTITY, "measure", 0.8)),
    ]


def _is_generation_read(sql: str) -> bool:
    return "attgenerated" in sql


def _step(monkeypatch, ctx, answer=None, generated_answer=None):
    """Run b3a against a fake catalog. ``answer`` answers the column type
    read (and, unless ``generated_answer`` is given, the generation
    expression read too); a returned exception is raised."""
    writes: list = []
    reads: list = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)

    def pg(schema, sql, params=None, **kwargs):
        reads.append({"schema": schema, "sql": sql, "params": params, "kwargs": kwargs})
        if _is_generation_read(sql):
            chosen = generated_answer or answer or (lambda: {"rows": _generated_rows()})
        else:
            chosen = answer or (lambda: {"rows": _type_rows()})
        result = chosen()
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(onboard, "pg", pg)
    onboard._dp_mirrored_measure_tags(ctx)
    edges = [p for c, p in writes if "MERGE (f)-[r:MAPS_TO]->(c)" in c]
    return edges, writes, reads


def test_b3a_writes_the_mirrored_tag_as_a_measure_edge_with_its_method(monkeypatch):
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS))
    edges, writes, reads = _step(monkeypatch, ctx)
    assert [(e["fid"], e["cid"], e["role"], e["conf"]) for e in edges] == [
        (f"{SID}.ln.line_total", FIELD, "measure", 0.9)]
    (cypher,) = [c for c, p in writes if "MERGE (f)-[r:MAPS_TO]->(c)" in c]
    assert "r.method = $method" in cypher
    assert edges[0]["method"] == onboard.MIRRORED_MEASURE_METHOD
    assert len(reads) == 2
    assert [_is_generation_read(r["sql"]) for r in reads] == [False, True]
    assert all(r["schema"] == SCHEMA and r["params"] == (SCHEMA,) for r in reads)


def test_b3a_puts_the_tag_on_the_row_and_in_the_agents_concept_mappings(monkeypatch):
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS),
               cm_items=onboard._concept_mapping_items(_rows(), default_table=SCHEMA,
                                                       committable={FIELD, OTHER_FIELD, ENTITY}))
    assert {"concept": FIELD, "field": "ln.line_total", "role": "measure"} not in ctx.cm_items
    _step(monkeypatch, ctx)
    assert {"concept": FIELD, "field": "ln.line_total", "role": "measure"} in ctx.cm_items
    line = next(m for m in ctx.mappings if m["field"] == "ln.line_total")
    assert {"concept": FIELD, "role": "measure", "confidence": 0.9,
            "basis": onboard.MIRRORED_MEASURE_METHOD} in line["mappings"]
    assert {"concept": FIELD, "confidence": 0.9} in line["concepts"]
    assert ctx.measure_tags_mirrored == [{
        "field": "ln.line_total", "concept": FIELD, "from_field": "hdr.total_amount",
        "join": "ln.hdr_id = hdr.hdr_id"}]
    assert onboard._dp_mirrored_markers(ctx) == {"measure_tags_mirrored": ctx.measure_tags_mirrored}


def test_b3a_leaves_a_row_the_steward_edited(monkeypatch):
    rows = _rows()
    line = next(m for m in rows if m["field"] == "ln.line_total")
    line["concepts"] = []            # the steward removed the line total's tags
    ctx = _ctx(mappings=rows, measured_joins=list(_JOINS))
    edges, _w, _r = _step(monkeypatch, ctx)
    assert edges == []
    assert line["mappings"] == [{"concept": ENTITY, "role": "measure", "confidence": 0.8}]


def test_b3a_adds_no_concept_a_steward_correction_owns(monkeypatch):
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS),
               user_corrections={FIELD: "hdr.total_amount"})
    edges, _w, _r = _step(monkeypatch, ctx)
    assert edges == [] and ctx.deferred_llm_edges == {}


def test_b3a_adds_no_concept_outside_the_committable_vocabulary(monkeypatch):
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS), committable={ENTITY})
    edges, _w, _r = _step(monkeypatch, ctx)
    assert edges == []


@pytest.mark.parametrize("answer", [
    lambda: {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
             "error_category": "OperationalError"},
    lambda: RuntimeError("boom"),
    lambda: {"rows": [["ln", "line_total"], None, "junk", [1, 2, 3]]},
], ids=["unavailable", "raises", "malformed rows"])
def test_b3a_adds_nothing_and_never_raises_when_the_catalog_read_fails(monkeypatch, answer):
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS))
    edges, _w, _r = _step(monkeypatch, ctx, answer)
    assert edges == [] and ctx.measure_tags_mirrored == []
    assert onboard._dp_mirrored_markers(ctx) == {}


@pytest.mark.parametrize("overrides", [
    {"measured_joins": []},
    {"connection_type": "Athena"},
    {"schema": ""},
], ids=["no measured join", "not PostgreSQL", "no schema"])
def test_b3a_reads_nothing_without_a_measured_join_on_a_postgresql_schema(monkeypatch, overrides):
    ctx = _ctx(mappings=_rows(), **{"measured_joins": list(_JOINS), **overrides})
    edges, _w, reads = _step(monkeypatch, ctx)
    assert reads == [] and edges == []


def test_b3a_reads_the_catalog_with_a_deadline(monkeypatch):
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS))
    _e, _w, reads = _step(monkeypatch, ctx)
    assert reads and all(r["kwargs"].get("statement_timeout_ms")
                         == onboard.REFERENCE_READ_TIMEOUT_MS for r in reads)


@pytest.mark.parametrize("generated_answer", [
    lambda: RuntimeError("no attgenerated before PostgreSQL 12"),
    lambda: {"columns": [], "rows": [], "row_count": 0, "unavailable": True},
    lambda: {"rows": [["ln", "line_total"], None, [1, 2, 3]]},
], ids=["raises", "unavailable", "malformed rows"])
def test_b3a_without_generation_expressions_adds_only_what_a_meaningful_word_supports(
        monkeypatch, generated_answer):
    """The line total shares only a generic word with the header total, so
    without its expression it takes nothing; the line price shares ``price``
    with the catalogue price and still takes the concept."""
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS))
    edges, _w, _r = _step(monkeypatch, ctx, generated_answer=generated_answer)
    assert edges == [] and ctx.measure_tags_mirrored == []
    rows = [_row("cat", "list_price", (FIELD, "measure", 0.7)),
            _row("ln", "line_total", (FIELD, "measure", 0.8)),
            _row("ln", "unit_price", (ENTITY, "measure", 0.8))]
    ctx = _ctx(mappings=rows, measured_joins=list(_JOINS))
    edges, _w, _r = _step(monkeypatch, ctx, generated_answer=generated_answer)
    assert [(e["fid"], e["cid"]) for e in edges] == [(f"{SID}.ln.unit_price", FIELD)]


@pytest.mark.parametrize("pg_major", [15, 16])
@pytest.mark.parametrize("name, tagged", [("pass", True), ("renamed", False)],
                         ids=["pass-through view", "view renames a payment to the line total"])
def test_b3a_on_a_view_reads_the_line_totals_expression_only_when_the_view_passes_it_through(
        monkeypatch, pg_major, name, tagged):
    """``ln`` is a view. Passed through, its line total keeps the base
    expression over its price and takes the header total's money concept.
    When the view's ``line_total`` is the base table's ``paid_amount`` under
    another name, the base table's unused generated ``line_total`` says
    nothing about it, and it takes nothing."""
    view, generated = _fixture_view(pg_major, name)
    line_total = [g for g in generated if g["name"] == "line_total"]
    ctx = _ctx(mappings=_rows(), measured_joins=list(_JOINS))
    edges, _w, _r = _step(monkeypatch, ctx, generated_answer=lambda: {"rows": [
        _view_generation_row("ln", view["rule"], view["base_oid"], line_total)]})
    expected = [(f"{SID}.ln.line_total", FIELD)] if tagged else []
    assert [(e["fid"], e["cid"]) for e in edges] == expected


# --------------------------------------------------------------------------- #
# Duplicate concepts inside one reviewed row
# --------------------------------------------------------------------------- #
def _duplicate_rows(reverse: bool):
    """The header total carries the money concept twice (0.9 and 0.4); a
    second header ``zz`` offers it at 0.6 across its own join."""
    inner = [(FIELD, "measure", 0.9), (FIELD, "measure", 0.4)]
    if reverse:
        inner.reverse()
    return [_row("hdr", "total_amount", *inner),
            _row("zz", "total_due", (FIELD, "measure", 0.6)),
            _row("ln", "unit_price", (FIELD, "measure", 0.85)),
            _row("ln", "line_total", (ENTITY, "measure", 0.8))]


_DUP_TYPES = {**_TYPES, ("zz", "zz_id"): "uuid", ("zz", "total_due"): MONEY,
              ("ln", "zz_id"): "uuid"}
_DUP_JOINS = [*_JOINS, ("ln", "zz_id", "zz", "zz_id")]


@pytest.mark.parametrize("reverse", [False, True], ids=["high first", "low first"])
def test_duplicate_entries_of_a_concept_count_at_their_highest_confidence(reverse):
    ctx = _ctx(mappings=_duplicate_rows(reverse))
    _rows_, measure_tags, _carried, _targets = onboard._mirror_inputs(ctx)
    assert measure_tags[("hdr", "total_amount")] == {FIELD: 0.9}


@pytest.mark.parametrize("reverse", [False, True], ids=["high first", "low first"])
def test_duplicate_entries_do_not_change_the_chosen_source(monkeypatch, reverse):
    ctx = _ctx(mappings=_duplicate_rows(reverse), measured_joins=list(_DUP_JOINS))
    edges, _w, _r = _step(monkeypatch, ctx, answer=lambda: {"rows": _type_rows(_DUP_TYPES)},
                          generated_answer=lambda: {"rows": _generated_rows()})
    assert [(e["fid"], e["cid"], e["conf"]) for e in edges] == [
        (f"{SID}.ln.line_total", FIELD, 0.9)]
    assert [t["from_field"] for t in ctx.measure_tags_mirrored] == ["hdr.total_amount"]


# --------------------------------------------------------------------------- #
# The new tag reaches KNOWN CONCEPT MAPPINGS without pushing a concept out
# (one line per concept, every field listed under the renderer's cap)
# --------------------------------------------------------------------------- #
_NEW_ENTRY = ("cost", "order_line_items.line_total", "measure")


def _failing_run_items() -> list:
    import json
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "erp_concept_mappings_failing_run.json"
    return json.loads(path.read_text())["items"]


def _failing_run_after_b3a(monkeypatch):
    """The failing run's 192 KNOWN CONCEPT MAPPINGS items as reviewed map
    rows, then b3a with the order line's measured join, column types and
    generated line total. Returns ``(items_before, ctx)``."""
    items = _failing_run_items()
    by_field: dict = {}
    for it in items:
        by_field.setdefault(it["field"], []).append((it["concept"], it["role"], 0.9))
    rows = [_row(field.split(".", 1)[0], field.split(".", 1)[1], *tags)
            for field, tags in sorted(by_field.items())]
    committable = {it["concept"] for it in items}
    assert onboard._concept_mapping_items(rows, default_table=SCHEMA,
                                          committable=committable) == sorted(
        items, key=lambda it: (it["concept"], it["field"]))
    types_ = {("order_line_items", "order_id"): "uuid",
              ("order_line_items", "quantity"): "integer",
              ("order_line_items", "unit_price"): MONEY,
              ("order_line_items", "line_total"): MONEY,
              ("customer_orders", "order_id"): "uuid",
              ("customer_orders", "subtotal"): MONEY,
              ("customer_orders", "tax_amount"): MONEY,
              ("customer_orders", "shipping_amount"): MONEY,
              ("customer_orders", "total_amount"): MONEY}
    generated = {("order_line_items", "line_total"): "(quantity * unit_price)"}
    ctx = _ctx(mappings=rows, committable=committable,
               measured_joins=[("order_line_items", "order_id", "customer_orders", "order_id")],
               cm_items=list(items))
    _step(monkeypatch, ctx, answer=lambda: {"rows": _type_rows(types_)},
          generated_answer=lambda: {"rows": _generated_rows(generated)})
    return items, ctx


def _rendered_concepts(out: str) -> set:
    return {ln[2:].split(" -> ", 1)[0] for ln in out.splitlines() if ln.startswith("- ")}


def _rendered_entries(out: str) -> set:
    """``(concept, field, role)`` for every field the section shows. A line is
    ``- concept -> f1, f2 [role]; f3 [role2]``."""
    got = set()
    for ln in out.splitlines():
        if not ln.startswith("- "):
            continue
        concept, rest = ln[2:].split(" -> ", 1)
        for group in rest.split("; "):
            role = ""
            if group.endswith("]") and " [" in group:
                group, role = group[:-1].rsplit(" [", 1)
            got.update((concept, field, role) for field in group.split(", "))
    return got


def test_the_failing_runs_mapping_list_keeps_every_concept_with_the_new_tag(monkeypatch):
    import agents.system_agent as sa
    items, ctx = _failing_run_after_b3a(monkeypatch)
    assert [(t["field"], t["concept"], t["from_field"]) for t in ctx.measure_tags_mirrored] == [
        ("order_line_items.line_total", FIELD, "customer_orders.total_amount")]
    assert len(items) == 192 and len(ctx.cm_items) == 193
    assert {"concept": FIELD, "field": "order_line_items.line_total",
            "role": "measure"} in ctx.cm_items
    before = sa.format_concept_mappings(items)
    after = sa.format_concept_mappings(ctx.cm_items)
    assert len(after) <= sa._CONCEPT_MAPPINGS_CAP
    assert _rendered_concepts(before) == {it["concept"] for it in items}
    assert _rendered_concepts(after) == _rendered_concepts(before)
    assert "truncated" not in after
    assert _rendered_entries(after) == {(it["concept"], it["field"], it["role"])
                                        for it in ctx.cm_items}


def test_the_failing_runs_mapping_list_shows_the_new_tag(monkeypatch):
    import agents.system_agent as sa
    _items, ctx = _failing_run_after_b3a(monkeypatch)
    out = sa.format_concept_mappings(ctx.cm_items)
    assert _NEW_ENTRY in _rendered_entries(out)
    cost_lines = [ln for ln in out.splitlines() if ln.startswith(f"- {FIELD} -> ")]
    assert len(cost_lines) == 1 and "order_line_items.line_total" in cost_lines[0]


def test_the_coordinator_runs_b3a_after_b3_and_before_the_agent_starts(monkeypatch):
    ran: list = []
    monkeypatch.setattr(onboard, "_make_provision_state",
                        lambda body: types.SimpleNamespace(system_id=SID, body=body))
    names = ("_dp_collect_tables", "_dp_discover_athena_physical", "_dp_schema_entries",
             "_dp_field_mappings", "_dp_athena_physical_fields", "_dp_discovery_completeness",
             "_dp_reapply_user_corrections", "_dp_identity_key_edges", "_dp_mint_unmapped",
             "_dp_internal_joinpaths", "_dp_mirrored_measure_tags", "_dp_settle_field_concepts",
             "_dp_valueoverlap_joinpaths", "_dp_samerow_joinpaths", "_dp_identity_rules",
             "_dp_rule_joinpaths", "_dp_cross_system_vms", "_dp_equipment_vms_legacy",
             "_dp_authority_rules", "_dp_onboarding_log", "_dp_finalize")
    for name in names:
        monkeypatch.setattr(onboard, name, lambda ctx, _n=name: ran.append(_n), raising=False)
    monkeypatch.setattr(onboard, "_dp_connection_phase", lambda ctx: None)
    monkeypatch.setattr(onboard, "_dp_start_pg_agent",
                        lambda ctx: ran.append("_dp_start_pg_agent"))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    onboard._do_provision_base(types.SimpleNamespace(system_id=SID))
    assert "_dp_mirrored_measure_tags" in ran
    assert ran.index("_dp_internal_joinpaths") < ran.index("_dp_mirrored_measure_tags") \
        < ran.index("_dp_start_pg_agent")
