# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding records a PostgreSQL schema's real join keys (offline).

Workshop rehearsal, 2026-10-14: the ERP system agent joined two line tables
on a ``product_id`` both carry instead of the shipment line's own key, so
every shipment line repeated once per order line of that product and a SUM
over the join came back hundreds of times too high. The baked schema named
no join keys, so the agent chose its own.

The ERP schema is pass-through views, which carry no foreign keys; the FK
read found none and the ``*_id`` name heuristic mapped ``line_item_id`` to
the alphabetically first ``*_line_items`` table. Pinned here, with a fake
``pg``:

1. Foreign keys are also read through one-table views to their base tables
   (``_view_foreign_keys``), only for view columns whose lineage the view's
   own definition proves (``view_lineage.pass_through_columns``; a
   same-named column is not proof); an ambiguous target is left out.
2. A measured key outranks the heuristic: no guessed second target beside
   it. A primary-key column is never guessed onto a table whose same-named
   column is the primary key of a DIFFERENT base table (two sibling keys);
   the exact-name hop and a view over the same table are kept.
3. The measured keys (never the heuristic guesses) are baked after the
   table entries (``schema_format.with_join_keys``) as a non-exhaustive
   list (other valid joins, such as a natural key or a join given in the
   question, are not contradicted), and every table parser
   still reads the entries as before.

The same behavior on the real db-init DDL: tests/integration/test_erp_join_keys_pg.py.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents import schema_format
from agents.context_placement import parse_cached_samples
from agents.response_integrity_sql import parse_physical_schema

_LINE_TABLES = ["invoice_line_items", "order_line_items", "shipment_items"]


def _join(ft, ff, tt, tf):
    return {"from_table": ft, "from_field": ff, "to_table": tt, "to_field": tf}


def _declared(ft, ff, tt, tf):
    """A detected join found by a declared foreign key."""
    return {**_join(ft, ff, tt, tf), "basis": "declared_foreign_key"}


def _guessed(ft, ff, tt, tf):
    """A detected join found by the ``*_id`` naming guess."""
    return {**_join(ft, ff, tt, tf), "basis": "column_name_pattern"}


def _pass_through(view, cols, base=None):
    """A lineage row for a view that copies ``cols`` from ``public.<base>``."""
    base = base or view
    select_list = ",\n    ".join(cols)
    return (view, "public", base, f" SELECT {select_list}\n   FROM public.{base};", list(cols))


def _auto_lineage(view_fks, keys):
    """Pass-through lineage for every view column the fake's rows name."""
    cols: dict[str, list[str]] = {}
    for ft, ff, tt, tf in [r for r in view_fks if isinstance(r, tuple) and len(r) == 4]:
        cols.setdefault(ft, []).append(ff)
        cols.setdefault(tt, []).append(tf)
    for row in keys:
        if isinstance(row, tuple) and len(row) == 4 and row[0] == "view":
            cols.setdefault(row[1], []).append(row[2])
    return [_pass_through(v, list(dict.fromkeys(c))) for v, c in sorted(cols.items())]


def _fake_pg(*, view_fks=(), keys=(), tables=(), id_cols=(), has_column=lambda t, c: True,
             schema_fks=(), fail=(), views=None):
    """A ``pg`` stand-in answering the detector's catalog reads by shape.
    ``keys`` rows are ``(kind, table, column, base)``; ``views`` are lineage
    rows (default: every view the other rows name copies its columns)."""
    calls: list[str] = []
    lineage = _auto_lineage(view_fks, keys) if views is None else views

    def pg(_schema, sql, params=None):
        calls.append(sql)
        for marker in fail:
            if marker in sql:
                raise RuntimeError("catalog read failed")
        if "pg_get_viewdef" in sql:
            return {"rows": [list(r) for r in lineage]}
        if "contype = 'f'" in sql and "one_table_views" in sql:
            return {"rows": [list(r) for r in view_fks]}
        if "contype = 'p'" in sql:
            return {"rows": [list(r) for r in keys]}
        if "referential_constraints" in sql:
            return {"rows": [list(r) for r in schema_fks]}
        if "information_schema.tables" in sql:
            return {"rows": [[t] for t in tables]}
        if "column_name LIKE" in sql:
            return {"rows": [list(r) for r in id_cols]}
        if "SELECT 1" in sql:
            return {"rows": [[1]] if has_column(params[1], params[2]) else []}
        return {"rows": []}
    pg.calls = calls
    return pg


# --------------------------------------------------------------------------- #
# 1. Foreign keys through one-table views, proven lineage only
# --------------------------------------------------------------------------- #
def test_view_foreign_keys_become_joins_between_the_views(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id"),
        ("bill_of_materials", "component_item_id", "inventory_items", "item_id")]))
    assert onboard._view_foreign_keys("erp_data") == [
        _join("bill_of_materials", "component_item_id", "inventory_items", "item_id"),
        _join("shipment_items", "line_item_id", "order_line_items", "line_item_id")]


def test_the_view_reads_are_parameterised_on_the_schema_and_read_only_pg_catalog(monkeypatch):
    seen = []

    def pg(schema, sql, params=None):
        seen.append((schema, sql, params))
        return {"rows": []}
    monkeypatch.setattr(onboard, "pg", pg)
    onboard._view_foreign_keys("erp_data")
    assert seen and all(params == ("erp_data",) and schema == "erp_data"
                        for schema, _sql, params in seen)
    for _schema, sql, _params in seen:
        assert "erp_data" not in sql and "information_schema" not in sql
        # A view over a join or subquery reads two relations and is left out.
        assert "count(DISTINCT d.refobjid) = 1" in sql
    assert any("pg_get_viewdef" in sql for _s, sql, _p in seen)


@pytest.mark.parametrize("hostile", [
    # aliased: the view's line_item_id is the base table's other column
    " SELECT shipment_item_id,\n    other_id AS line_item_id\n   FROM public.shipment_items;",
    # swapped
    " SELECT other_id AS line_item_id,\n    line_item_id AS other_id\n   FROM public.shipment_items;",
    # computed / cast
    " SELECT shipment_item_id,\n    (line_item_id)::text AS line_item_id\n   FROM public.shipment_items;",
    # the base table twice
    " SELECT line_item_id\n   FROM public.shipment_items\nUNION ALL\n SELECT line_item_id\n"
    "   FROM public.shipment_items;",
])
def test_a_foreign_key_through_an_unproven_view_column_is_left_out(monkeypatch, hostile):
    cols = (["other_id", "line_item_id"] if "other_id AS line_item_id,\n    line_item_id AS"
            in hostile else ["line_item_id"] if "UNION" in hostile
            else ["shipment_item_id", "line_item_id"])
    views = [("shipment_items", "public", "shipment_items", hostile, cols),
             _pass_through("order_line_items", ["line_item_id"]),
             _pass_through("shipments", ["shipment_id"])]
    monkeypatch.setattr(onboard, "pg", _fake_pg(views=views, view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id")]))
    assert onboard._view_foreign_keys("erp_data") == []


def test_both_ends_of_a_view_key_must_be_proven(monkeypatch):
    views = [_pass_through("shipment_items", ["line_item_id"]),
             ("order_line_items", "public", "order_line_items",
              " SELECT (line_item_id)::text AS line_item_id\n   FROM public.order_line_items;",
              ["line_item_id"])]
    monkeypatch.setattr(onboard, "pg", _fake_pg(views=views, view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id")]))
    assert onboard._view_foreign_keys("erp_data") == []


def test_a_view_with_no_lineage_row_proves_nothing(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(views=[], view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id")]))
    assert onboard._view_foreign_keys("erp_data") == []


def test_a_failed_lineage_read_emits_no_view_key(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(fail=("pg_get_viewdef",), view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id")],
        schema_fks=[("orders", "customer_id", "customers", "customer_id")]))
    assert onboard._measured_foreign_keys("erp_data") == [
        _join("orders", "customer_id", "customers", "customer_id")]


def test_a_column_with_two_view_targets_is_ambiguous_and_left_out(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id"),
        ("shipment_items", "line_item_id", "open_order_lines", "line_item_id"),
        ("shipment_items", "shipment_id", "shipments", "shipment_id")]))
    assert onboard._view_foreign_keys("erp_data") == [
        _join("shipment_items", "shipment_id", "shipments", "shipment_id")]


@pytest.mark.parametrize("row", [("a", "b", "c"), ("a", "", "c", "d"), (None, "b", "c", "d"),
                                 "abcd", 7])
def test_malformed_catalog_rows_are_skipped(monkeypatch, row):
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": [row]})
    assert onboard._view_foreign_keys("s") == []
    assert onboard._primary_key_columns("s") == {}


def test_schema_constraints_come_first_and_the_view_read_adds_no_duplicate(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        schema_fks=[("orders", "customer_id", "customers", "customer_id")],
        view_fks=[("customers", "customer_id", "orders", "customer_id"),
                  ("lines", "order_id", "orders", "order_id")]))
    assert onboard._measured_foreign_keys("s") == [
        _join("orders", "customer_id", "customers", "customer_id"),
        _join("lines", "order_id", "orders", "order_id")]


def test_a_composite_foreign_key_yields_only_its_ordinal_pairs(monkeypatch):
    """A two-column FK ``(a, b) REFERENCES parent (x, y)`` must become the two
    ordinal pairs ``a->x`` and ``b->y`` only. A join of key_column_usage to
    constraint_column_usage on the constraint name alone returns the CARTESIAN
    product (``a->x, a->y, b->x, b->y``), presenting ``a->y`` and ``b->x`` as
    declared constraints. The FK read's SQL (checked below) pairs by ordinal
    through referential_constraints + position_in_unique_constraint, so the
    rows reaching the detector are already the real pairs. These are the rows
    PostgreSQL returns for that query (confirmed on a live two-column FK)."""
    seen = []

    def pg(_schema, sql, params=None):
        seen.append(sql)
        if "contype = 'f'" in sql and "one_table_views" in sql:
            return {"rows": []}
        if "referential_constraints" in sql:
            # The ordinal-paired query returns one row per FK column, each
            # paired with the referenced column at the same ordinal position.
            return {"rows": [["child", "a", "parent", "x", "fk_cp"],
                             ["child", "b", "parent", "y", "fk_cp"]]}
        return {"rows": []}

    monkeypatch.setattr(onboard, "pg", pg)
    assert onboard._measured_foreign_keys("s") == [
        {**_join("child", "a", "parent", "x"), "constraint_name": "fk_cp"},
        {**_join("child", "b", "parent", "y"), "constraint_name": "fk_cp"}]
    # The query must pair by ordinal, never join the two sides by constraint
    # name alone (which Cartesian-expands a composite key).
    fk_sql = next(s for s in seen if "referential_constraints" in s)
    assert "position_in_unique_constraint" in fk_sql
    assert "constraint_column_usage" not in fk_sql
    # A key in another schema must not come back: a join keeps table names
    # only, so it would read as this schema's own same-named table.
    assert "pk.table_schema = fk.table_schema" in fk_sql


def test_a_failed_view_read_keeps_the_schema_constraints(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        schema_fks=[("orders", "customer_id", "customers", "customer_id")],
        fail=("one_table_views",)))
    assert onboard._measured_foreign_keys("s") == [
        _join("orders", "customer_id", "customers", "customer_id")]


def test_primary_keys_carry_their_base_table_and_need_proven_lineage(monkeypatch):
    views = [_pass_through("order_line_items", ["line_item_id"]),
             ("invoice_line_items", "public", "invoice_line_items",
              " SELECT (line_item_id)::text AS line_item_id\n   FROM public.invoice_line_items;",
              ["line_item_id"])]
    monkeypatch.setattr(onboard, "pg", _fake_pg(views=views, keys=[
        ("view", "order_line_items", "line_item_id", "101"),
        ("view", "invoice_line_items", "line_item_id", "102"),
        ("table", "assets", "asset_id", "103")]))
    assert onboard._primary_key_columns("s") == {
        ("order_line_items", "line_item_id"): "101", ("assets", "asset_id"): "103"}


# --------------------------------------------------------------------------- #
# 2. The heuristic never guesses beside a measured key, nor between siblings
# --------------------------------------------------------------------------- #
_LINE_ID_COLS = [(t, "line_item_id") for t in _LINE_TABLES]
_LINE_KEYS = [("view", "invoice_line_items", "line_item_id", "201"),
              ("view", "order_line_items", "line_item_id", "202")]


def test_before_the_fix_the_heuristic_picked_the_alphabetically_first_sibling(monkeypatch):
    """Characterizes the guess the measured key now replaces."""
    monkeypatch.setattr(onboard, "pg", _fake_pg(tables=_LINE_TABLES,
                                                id_cols=[("shipment_items", "line_item_id")]))
    assert onboard._detect_internal_joinpaths("erp_data", "erp") == [
        _guessed("shipment_items", "line_item_id", "invoice_line_items", "line_item_id")]


def test_a_measured_key_suppresses_the_name_guess_for_that_column(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        tables=_LINE_TABLES, id_cols=[("shipment_items", "line_item_id")],
        view_fks=[("shipment_items", "line_item_id", "order_line_items", "line_item_id")]))
    assert onboard._detect_internal_joinpaths("erp_data", "erp") == [
        _declared("shipment_items", "line_item_id", "order_line_items", "line_item_id")]


def test_a_primary_key_is_never_guessed_onto_a_sibling_table(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        tables=_LINE_TABLES, id_cols=_LINE_ID_COLS, keys=_LINE_KEYS,
        view_fks=[("shipment_items", "line_item_id", "order_line_items", "line_item_id")]))
    assert onboard._detect_internal_joinpaths("erp_data", "erp") == [
        _declared("shipment_items", "line_item_id", "order_line_items", "line_item_id")]


def test_a_primary_key_still_matches_the_table_named_after_it(monkeypatch):
    # A 1:1 extension table keyed by its parent's key keeps the exact-name hop.
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        tables=["orders", "order_details"], id_cols=[("order_details", "order_id")],
        keys=[("table", "order_details", "order_id", "301"),
              ("table", "orders", "order_id", "302")]))
    assert onboard._detect_internal_joinpaths("s", "sys") == [
        _guessed("order_details", "order_id", "orders", "order_id")]


def test_a_view_over_the_same_table_keeps_its_suffix_hop(monkeypatch):
    """A raw table and the view that serves it (same base table, same key):
    the hop main detected between them stays."""
    views = [_pass_through("maintenance_schedules", ["schedule_id"],
                           base="maintenance_schedules_raw")]
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        views=views, tables=["maintenance_schedules_raw", "maintenance_schedules"],
        id_cols=[("maintenance_schedules_raw", "schedule_id")],
        keys=[("table", "maintenance_schedules_raw", "schedule_id", "401"),
              ("view", "maintenance_schedules", "schedule_id", "401")]))
    assert onboard._detect_internal_joinpaths("cmms", "cmms") == [
        _guessed("maintenance_schedules_raw", "schedule_id", "maintenance_schedules",
                 "schedule_id")]


def test_a_suffix_table_whose_column_is_no_key_is_still_guessed(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        tables=["events", "production_lines"], id_cols=[("events", "line_id")],
        keys=[("table", "events", "line_id", "501")]))
    assert onboard._detect_internal_joinpaths("mes", "mes") == [
        _guessed("events", "line_id", "production_lines", "line_id")]


def test_without_catalog_keys_the_heuristic_is_unchanged(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        tables=["downtime_events", "production_lines"],
        id_cols=[("downtime_events", "line_id")]))
    assert onboard._detect_internal_joinpaths("mes", "mes") == [
        _guessed("downtime_events", "line_id", "production_lines", "line_id")]


def test_a_failed_key_read_keeps_the_heuristic(monkeypatch):
    monkeypatch.setattr(onboard, "pg", _fake_pg(
        tables=_LINE_TABLES, id_cols=[("shipment_items", "line_item_id")],
        fail=("contype = 'p'",)))
    assert onboard._detect_internal_joinpaths("erp_data", "erp") == [
        _guessed("shipment_items", "line_item_id", "invoice_line_items", "line_item_id")]


# --------------------------------------------------------------------------- #
# 3. The measured keys in the baked schema
# --------------------------------------------------------------------------- #
_DETAILS = {
    "order_line_items": [("line_item_id", "uuid"), ("product_id", "uuid")],
    "shipment_items": [("shipment_item_id", "uuid"), ("line_item_id", "uuid"),
                       ("product_id", "uuid")],
}
_SHIP_TO_ORDER = _join("shipment_items", "line_item_id", "order_line_items", "line_item_id")


def test_the_join_keys_follow_the_table_entries():
    desc = schema_format.format_tables_desc(_DETAILS)
    out = schema_format.with_join_keys(desc, [_SHIP_TO_ORDER], _DETAILS)
    assert out == (desc + ". " + schema_format.JOIN_KEYS_PREFIX
                   + "shipment_items.line_item_id = order_line_items.line_item_id.")


@pytest.mark.parametrize("render", ["plain", "samples"])
def test_every_table_parser_reads_the_entries_as_before(render):
    if render == "plain":
        desc = schema_format.format_tables_desc(_DETAILS)
    else:
        desc = schema_format.format_schema_with_samples(
            _DETAILS, {"shipment_items": {"line_item_id": ["a1", "a2"]},
                       "order_line_items": {"product_id": ["p1"]}})
    out = schema_format.with_join_keys(desc, [_SHIP_TO_ORDER], _DETAILS)
    assert parse_physical_schema(out) == parse_physical_schema(desc)
    assert set(parse_physical_schema(out)) == set(_DETAILS)  # the last entry included
    assert parse_cached_samples(out) == parse_cached_samples(desc)
    added = out[len(desc):]
    assert not set(added) & set("()[]") and "e.g." not in added


def test_only_joins_between_rendered_tables_are_named_each_once():
    desc = schema_format.format_tables_desc(_DETAILS)
    out = schema_format.with_join_keys(desc, [
        _SHIP_TO_ORDER, dict(_SHIP_TO_ORDER),
        _join("shipment_items", "shipment_id", "shipments", "shipment_id"),
        _join("order_line_items", "bad name", "x", "y"), "not a join", {}], _DETAILS)
    assert out.count(" = ") == 1


@pytest.mark.parametrize("desc,joins", [("", [_SHIP_TO_ORDER]), ("t(a: int)", []),
                                        ("t(a: int)", None)])
def test_nothing_to_name_leaves_the_desc_byte_identical(desc, joins):
    assert schema_format.with_join_keys(desc, joins, _DETAILS) == desc


def test_the_join_keys_text_names_no_dataset():
    low = schema_format.JOIN_KEYS_PREFIX.lower()
    for word in ("shipment", "order", "invoice", "product", "line_item"):
        assert word not in low


def test_the_join_keys_are_not_presented_as_the_only_joins():
    """An earlier text, "join on exactly these columns", read as the only
    joins, so an obedient agent could refuse a valid join that has no declared
    key (a run number another system records as a batch id, a second column
    that refers to the same table). The text says the list is not complete
    and names those kinds of joins, and a join the question gives, as valid."""
    text = schema_format.JOIN_KEYS_PREFIX
    assert "not the only valid joins" in text
    assert "another column that holds the same values" in text
    assert "a second column that refers to the same table" in text
    assert "a shared business number or code" in text
    assert "a join given in the question" in text
    low = text.lower()
    for word in ("exactly", "rather than", "never", "only these", "do not", "must"):
        assert word not in low, word


_PARTS = {
    "bom_items": [("bom_id", "integer"), ("parent_item", "character varying"),
                  ("child_item", "character varying")],
    "products": [("item_id", "character varying"), ("name", "text")],
}


def test_a_second_reference_to_the_same_table_stays_usable():
    """The declared key of a pair is named, and the pair's other link (a
    column with no declared key that holds the same values) keeps its column
    in the entries and is not ruled out by the text after them."""
    desc = schema_format.format_tables_desc(_PARTS)
    out = schema_format.with_join_keys(
        desc, [_join("bom_items", "parent_item", "products", "item_id")], _PARTS)
    keys = out[len(desc):]
    assert keys.endswith("bom_items.parent_item = products.item_id.")
    assert "child_item" not in keys
    assert parse_physical_schema(out)["bom_items"]["columns"]["child_item"]
    assert "not the only valid joins" in keys


def _ctx(tables):
    return onboard._ProvisionState(
        body=None, system_id="erp", system_name="ERP", plant="", schema="erp_data",
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[],
        tables_seen=set(tables))


def _desc_pg(view_fks=(), fail=()):
    base = _fake_pg(view_fks=view_fks, tables=_LINE_TABLES,
                    id_cols=[("shipment_items", "line_item_id")], fail=fail)

    def pg(schema, sql, params=None):
        if "information_schema.columns WHERE table_schema = %s AND table_name" in sql:
            return {"rows": [list(c) for c in _DETAILS.get(params[1], [])]}
        return base(schema, sql, params)
    return pg


def test_the_provision_schema_bakes_only_measured_keys(monkeypatch):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    monkeypatch.setattr(onboard, "pg", _desc_pg(view_fks=[
        ("shipment_items", "line_item_id", "order_line_items", "line_item_id")]))
    desc = onboard._dp_pg_tables_desc(_ctx(_DETAILS))
    assert desc.endswith(schema_format.JOIN_KEYS_PREFIX
                         + "shipment_items.line_item_id = order_line_items.line_item_id.")


def test_a_name_guess_is_never_baked_as_a_join_key(monkeypatch):
    # No measured key: the heuristic would guess a sibling, but the baked
    # schema names nothing rather than a guess.
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    monkeypatch.setattr(onboard, "pg", _desc_pg())
    desc = onboard._dp_pg_tables_desc(_ctx(_DETAILS))
    assert schema_format.JOIN_KEYS_PREFIX not in desc
    assert set(parse_physical_schema(desc)) == set(_DETAILS)


def test_a_failed_key_read_keeps_the_baked_schema(monkeypatch):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    monkeypatch.setattr(onboard, "_measured_foreign_keys",
                        lambda _s: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(onboard, "pg", _desc_pg())
    desc = onboard._dp_pg_tables_desc(_ctx(_DETAILS))
    assert schema_format.JOIN_KEYS_PREFIX not in desc
    assert set(parse_physical_schema(desc)) == set(_DETAILS)
