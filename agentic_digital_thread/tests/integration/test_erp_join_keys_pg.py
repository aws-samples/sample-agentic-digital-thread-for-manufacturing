# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding measures the ERP view schema's real join keys (PG).

Workshop rehearsal, 2026-10-14: asked for a lot's shipped quantity and value,
the ERP system agent sometimes joined ``order_line_items`` (or
``invoice_line_items``) to ``shipment_items`` on the ``product_id`` both
carry instead of on the shipment line's own ``line_item_id``. Every shipment
line then repeated once per order line of that product, and a SUM over the
join came back hundreds of times too high. The baked schema listed columns
and samples but no join keys, so the agent chose its own.

The ERP view schema is pass-through views (``erp_data.<t>`` over
``public.<t>``), and views carry no foreign keys of their own, so the
onboarding FK read found none and fell back to a name heuristic that maps
``shipment_items.line_item_id`` to the alphabetically first
``*_line_items`` table: ``invoice_line_items``. These tests run onboarding's
own detection and schema render on the real db-init DDL and pin that the
foreign keys are read through the views to their base tables, that the
heuristic no longer adds a wrong guess beside a measured key, and that the
measured keys reach the agent's baked schema.

Also pinned: a view column counts as a key only when the view's
own definition proves it copies the base column (hostile aliased, swapped,
computed, cast and doubled views on this server), the baked keys are not
presented as the only joins (the PLM second reference and the ERP batch
number stay usable), and a raw table keeps its hop to the view that serves
it (the CMMS schema, unchanged from main).

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_story_traceability_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_erp_join_keys_pg.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.demo import workshop  # noqa: E402
from tests.integration import story_pg_support as support  # noqa: E402
from tests.integration._pg_sql import stmt  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_ERP = support.ERP_VIEW_SCHEMA
_SHIP_LINE = {"from_table": "shipment_items", "from_field": "line_item_id",
              "to_table": "order_line_items", "to_field": "line_item_id"}


#: Base tables and hostile views for the lineage proof.
#: Neither schema name is ERP-family, so ``routed_pg`` sends them to the seed
#: database; it is dropped with the module's databases.
_HOSTILE_DDL = """
CREATE SCHEMA w7h_base;
CREATE TABLE w7h_base.parent (pid INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE w7h_base.child (cid INTEGER PRIMARY KEY,
                             pid INTEGER REFERENCES w7h_base.parent (pid), other INTEGER);
CREATE SCHEMA w7h;
CREATE VIEW w7h.parent AS SELECT * FROM w7h_base.parent;
CREATE VIEW w7h.child_ok AS SELECT * FROM w7h_base.child WHERE cid > 0;
CREATE VIEW w7h.child_alias AS SELECT cid, other AS pid FROM w7h_base.child;
CREATE VIEW w7h.child_swap AS SELECT cid, other AS pid, pid AS other FROM w7h_base.child;
CREATE VIEW w7h.child_comp AS SELECT cid, pid + 0 AS pid FROM w7h_base.child;
CREATE VIEW w7h.child_cast AS SELECT cid, pid::bigint AS pid FROM w7h_base.child;
CREATE VIEW w7h.child_union AS
    SELECT cid, pid FROM w7h_base.child UNION ALL SELECT cid, pid FROM w7h_base.child;
CREATE FUNCTION w7h_base.child_rows(integer) RETURNS SETOF integer
    LANGUAGE sql IMMUTABLE AS 'SELECT generate_series(1, 2)';
CREATE VIEW w7h.parent_srf AS SELECT pid, generate_series(1, 3) AS n FROM w7h_base.parent;
CREATE VIEW w7h.child_srf AS SELECT cid, pid, w7h_base.child_rows(cid) AS n FROM w7h_base.child;
CREATE TABLE w7h_base.comp_parent (x INTEGER, y INTEGER, PRIMARY KEY (x, y));
CREATE SCHEMA w7h_ext;
CREATE TABLE w7h_ext.comp_parent (z INTEGER PRIMARY KEY);
CREATE TABLE w7h_base.comp_child (cid INTEGER PRIMARY KEY, a INTEGER, b INTEGER, e INTEGER,
    CONSTRAINT fk_comp FOREIGN KEY (a, b) REFERENCES w7h_base.comp_parent (x, y),
    CONSTRAINT fk_ext FOREIGN KEY (e) REFERENCES w7h_ext.comp_parent (z));
"""
_HOSTILE = "w7h"

#: Same-named identifier column pairs for the distinct-identifier measurement
#: (synthetic forms; a non-ERP schema name, so it lives in the seed database):
#: ``held_*`` differ in shape but share ONE value that sits in the middle of
#: ``held_b``'s order, where neither sampled end of ``held_b`` reaches it, so
#: only the lookup into the other column finds it; ``same_*`` share a shape
#: and no value; ``diff_*`` differ in shape and declared type and share no
#: value; ``slow_b`` reads each row through a function that sleeps.
_FORMS_DDL = """
CREATE SCHEMA w7i;
CREATE TABLE w7i.held_a (ref TEXT);
INSERT INTO w7i.held_a SELECT 'BP-' || lpad(g::text, 4, '0') || '-XQ' FROM generate_series(145, 154) g;
CREATE TABLE w7i.held_b (ref TEXT);
INSERT INTO w7i.held_b SELECT 'BP-' || lpad(g::text, 4, '0') FROM generate_series(1, 300) g;
INSERT INTO w7i.held_b VALUES ('BP-0150-XQ');
CREATE TABLE w7i.same_a (ref TEXT);
INSERT INTO w7i.same_a SELECT 'KX-' || lpad(g::text, 4, '0') FROM generate_series(1, 10) g;
CREATE TABLE w7i.same_b (ref TEXT);
INSERT INTO w7i.same_b SELECT 'KX-' || lpad(g::text, 4, '0') FROM generate_series(101, 110) g;
CREATE TABLE w7i.diff_a (ref TEXT);
INSERT INTO w7i.diff_a SELECT 'KX-' || lpad(g::text, 4, '0') FROM generate_series(1, 10) g;
CREATE TABLE w7i.diff_b (ref VARCHAR(20));
INSERT INTO w7i.diff_b SELECT 'QZ-' || lpad(g::text, 4, '0') FROM generate_series(1, 12) g;
CREATE TABLE w7i.spaced_a (ref TEXT);
INSERT INTO w7i.spaced_a SELECT 'KX ' || lpad(g::text, 4, '0') FROM generate_series(1, 10) g;
CREATE TABLE w7i.slow_base (ref TEXT);
INSERT INTO w7i.slow_base SELECT 'QZ-' || lpad(g::text, 4, '0') FROM generate_series(1, 60) g;
CREATE FUNCTION w7i.slow(t TEXT) RETURNS TEXT LANGUAGE plpgsql AS
    'BEGIN PERFORM pg_sleep(0.05); RETURN t; END';
CREATE VIEW w7i.slow_b AS SELECT w7i.slow(ref) AS ref FROM w7i.slow_base;
"""
_FORMS = "w7i"


@pytest.fixture(scope="module")
def dbs():
    """{erp|seed: connection}: db-init (incl. the views), the seed SQL the
    story support loads plus the CMMS and historian files, the live story
    history, and the hostile lineage views."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "jk") as (conns, _plan):
        with conns["seed"].cursor() as cur:
            for number in ("004", "009"):
                (path,) = (support.REPO / "lambda-adt-seed").glob(f"{number}_*.sql")
                cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(_HOSTILE_DDL)
            cur.execute(_FORMS_DDL)
        conns["seed"].commit()
        yield conns


@pytest.fixture(scope="module")
def erp_conn(dbs):
    return dbs["erp"]


@pytest.fixture
def graph(monkeypatch):
    """The graph onboarding writes the measured pairs to and renders the note
    from, holding the Field nodes b2 writes for every mapped column
    (``tests/unit/_field_graph_fake.py``; no Neptune)."""
    from tests.unit._field_graph_fake import FieldGraph
    return FieldGraph(auto_fields=True).install(monkeypatch)


@pytest.fixture
def onboard_pg(dbs, graph, monkeypatch):
    """Route onboarding's own ``pg`` (and the engine's sampler) at the test DBs
    by the production ``is_erp_schema`` rule."""
    import agents.db as db
    import agents.onboard as onboard
    routed = support.routed_pg(dbs)
    monkeypatch.setattr(onboard, "pg", routed)
    monkeypatch.setattr(db, "pg", routed)
    return onboard


def _endpoints(joins, table, field):
    return sorted((j["to_table"], j["to_field"]) for j in joins
                  if (j["from_table"], j["from_field"]) == (table, field))


def _ends(joins):
    """Each join's four endpoints. Detection also records what proved a join
    (``basis``, ``constraint_name``); these tests pin which joins exist."""
    return [{k: j[k] for k in ("from_table", "from_field", "to_table", "to_field")}
            for j in joins]


def _touching(joins, table, field):
    return [j for j in joins if (table, field) in {
        (j["from_table"], j["from_field"]), (j["to_table"], j["to_field"])}]


def test_the_view_schema_has_no_foreign_keys_of_its_own(erp_conn):
    """Characterizes why the old FK read found nothing: the constraints live
    on the base tables, never on the pass-through views."""
    with erp_conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM information_schema.table_constraints
                       WHERE table_schema = %s AND constraint_type = 'FOREIGN KEY'""", (_ERP,))
        assert cur.fetchone() == (0,)
        cur.execute("""SELECT count(*) FROM information_schema.table_constraints
                       WHERE table_schema = 'public' AND table_name = 'shipment_items'
                         AND constraint_type = 'FOREIGN KEY'""")
        assert cur.fetchone()[0] >= 3
    erp_conn.rollback()


def test_shipment_lines_join_order_lines_on_their_own_line_key(onboard_pg):
    joins = _ends(onboard_pg._detect_internal_joinpaths(_ERP, "erp"))
    assert _SHIP_LINE in joins
    assert _endpoints(joins, "shipment_items", "line_item_id") == [
        ("order_line_items", "line_item_id")], joins


def test_the_foreign_keys_behind_the_views_are_all_read(onboard_pg):
    """Single-column FKs between two pass-through views, oriented FK side to
    key side (the metric layer's contract for a JoinPath), including the ones
    no name match can find (a key named after an abbreviation, or a column
    named differently from the key it references)."""
    joins = _ends(onboard_pg._detect_internal_joinpaths(_ERP, "erp"))
    for expected in (
            ("shipment_items", "shipment_id", "shipments", "shipment_id"),
            ("shipment_items", "product_id", "products", "product_id"),
            ("order_line_items", "order_id", "customer_orders", "order_id"),
            ("invoice_line_items", "invoice_id", "invoices", "invoice_id"),
            ("shipments", "customer_id", "customers", "customer_id"),
            ("po_line_items", "po_id", "purchase_orders", "po_id"),
            ("po_receipts", "po_id", "purchase_orders", "po_id"),
            ("bill_of_materials", "component_item_id", "inventory_items", "item_id")):
        assert dict(zip(("from_table", "from_field", "to_table", "to_field"),
                        expected)) in joins, expected


def test_no_primary_key_is_guessed_as_a_join_to_another_tables_primary_key(onboard_pg):
    """``invoice_line_items.line_item_id`` and ``order_line_items.line_item_id``
    are each their own table's primary key; the name heuristic used to join
    them to each other (and ``po_line_items``) because the names match."""
    joins = _ends(onboard_pg._detect_internal_joinpaths(_ERP, "erp"))
    assert _touching(joins, "invoice_line_items", "line_item_id") == [], joins
    assert _touching(joins, "po_line_items", "line_item_id") == [], joins
    assert _touching(joins, "order_line_items", "line_item_id") == [_SHIP_LINE], joins


def test_the_mes_view_heuristic_hop_survives(onboard_pg):
    """A site MES schema's two-table views keep the name heuristic: the
    ``work_orders`` -> ``production_runs`` hop (renamed run key) is unchanged."""
    detected = onboard_pg._detect_internal_joinpaths(workshop.LIVE_MES_SYSTEM["schema"],
                                                     workshop.LIVE_MES_SYSTEM["id"])
    hop = {"from_table": "work_orders", "from_field": "production_run_id",
           "to_table": "production_runs", "to_field": "production_run_id"}
    assert hop in _ends(detected)
    # Nothing declares this key, so it is recorded as a guess from the names.
    (found,) = [j for j in detected if _ends([j]) == [hop]]
    assert found["basis"] == "column_name_pattern", found


def test_a_composite_foreign_key_becomes_only_its_ordinal_pairs(onboard_pg, dbs):
    """A two-column FK ``(a, b) REFERENCES comp_parent (x, y)`` must read as the
    two ordinal pairs ``a->x`` and ``b->y`` only, never the Cartesian product a
    constraint-name-only join of key_column_usage to constraint_column_usage
    returns (which would also present ``a->y`` and ``b->x`` as declared keys)."""
    joins = onboard_pg._measured_foreign_keys("w7h_base")
    comp = sorted((j["from_field"], j["to_field"]) for j in joins
                  if j["from_table"] == "comp_child" and j["to_table"] == "comp_parent")
    assert comp == [("a", "x"), ("b", "y")], joins
    for j in joins:
        if j["from_table"] == "comp_child" and j["to_table"] == "comp_parent":
            assert j.get("constraint_name") == "fk_comp", j


def test_a_foreign_key_into_another_schema_is_not_an_internal_join(onboard_pg, dbs):
    """``comp_child.e REFERENCES w7h_ext.comp_parent (z)`` points outside the
    onboarded schema. A join keeps only table names, so returning it would
    read as a join to this schema's own ``comp_parent``, a different table."""
    joins = onboard_pg._measured_foreign_keys("w7h_base")
    assert [j for j in joins if j["from_field"] == "e"] == [], joins
    assert [j for j in joins if j.get("constraint_name") == "fk_ext"] == [], joins


def _ctx(onboard, schema: str, tables):
    return onboard._ProvisionState(
        body=None, system_id="erp", system_name="ERP", plant="", schema=schema,
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[],
        tables_seen=set(tables))


def _views(conn, schema):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.views WHERE table_schema = %s",
                    (schema,))
        names = sorted(r[0] for r in cur.fetchall())
    conn.rollback()
    return names


def test_the_baked_schema_names_the_measured_join_keys(onboard_pg, erp_conn, monkeypatch):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    desc = onboard_pg._dp_pg_tables_desc(_ctx(onboard_pg, _ERP, _views(erp_conn, _ERP)))
    assert "shipment_items.line_item_id = order_line_items.line_item_id" in desc
    from agents.schema_format import JOIN_KEYS_PREFIX
    assert "invoice_line_items.line_item_id" not in desc.split(JOIN_KEYS_PREFIX, 1)[-1]
    # The table entries still parse exactly as before (the last one included).
    from agents.response_integrity_sql import parse_physical_schema
    parsed = parse_physical_schema(desc)
    assert set(parsed) == set(_views(erp_conn, _ERP))
    assert parsed["shipment_items"]["columns"]["line_item_id"] == "uuid"


def test_the_baked_schema_already_shows_each_columns_value_format(onboard_pg, erp_conn,
                                                                    monkeypatch):
    """The first module 7b answer queried ``lot_mes_tracking.batch_id`` with a
    production-run number; that column holds work-order numbers. Onboarding
    already bakes up to three sampled values per column, so the format is in
    the prompt; this pins it (no new mechanism needed)."""
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "on")
    desc = onboard_pg._dp_pg_tables_desc(
        _ctx(onboard_pg, _ERP, ["lot_mes_tracking", "shipment_items"]))
    lot_entry = desc[desc.index("lot_mes_tracking("):]
    lot_entry = lot_entry[:lot_entry.index(")")]
    batch = next(part for part in lot_entry.split(";") if "batch_id" in part)
    assert " e.g. " in batch, lot_entry



def _ctx_for(onboard, system_id: str, schema: str, conn):
    ctx = _ctx(onboard, schema, _relations(conn, schema))
    ctx.system_id = system_id
    return ctx


def _relations(conn, schema):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    (schema,))
        names = sorted(r[0] for r in cur.fetchall())
    conn.rollback()
    return names


# --------------------------------------------------------------------------- #
# Lineage is proven, not inferred from a name
# --------------------------------------------------------------------------- #
def test_only_the_proven_view_column_becomes_a_key(onboard_pg, dbs):
    """Aliased, swapped, computed, cast, doubled and set-returning views keep
    the base column's NAME; only the plain filtered copy keeps its values and
    one row per base row."""
    joins = _ends(onboard_pg._detect_internal_joinpaths(_HOSTILE, "hostile"))
    assert joins == [{"from_table": "child_ok", "from_field": "pid",
                      "to_table": "parent", "to_field": "pid"}], joins
    keys = onboard_pg._primary_key_columns(_HOSTILE)
    assert ("parent", "pid") in keys and ("child_ok", "cid") in keys
    for view in ("child_alias", "child_swap"):
        assert (view, "pid") not in keys, view
        assert (view, "cid") in keys, view          # its cid IS a plain copy
    # An expression anywhere in the select list proves no column of the view.
    for view in ("child_comp", "child_cast"):
        assert not any(table == view for table, _col in keys), view
    assert not any(table == "child_union" for table, _col in keys)


def test_a_set_returning_projection_carries_no_key(onboard_pg, dbs):
    """A set-returning function in a view's select list repeats every base
    row, so the base table's primary key copied unchanged beside it is no
    longer unique: neither generate_series nor a user-defined RETURNS SETOF
    function lets the view carry a key or a join."""
    with dbs["seed"].cursor() as cur:
        cur.execute("INSERT INTO w7h_base.parent VALUES (1, 'a'), (2, 'b')")
        cur.execute("INSERT INTO w7h_base.child VALUES (10, 1, 1), (20, 2, 2)")
        for view, key in (("parent_srf", "pid"), ("child_srf", "cid")):
            cur.execute(stmt("SELECT count(*), count(DISTINCT {key}) FROM {view}",
                             key=key, view=(_HOSTILE, view)))
            rows, distinct = cur.fetchone()
            assert rows > distinct, view            # the projection really repeats rows
    dbs["seed"].rollback()
    keys = onboard_pg._primary_key_columns(_HOSTILE)
    assert not any(table in ("parent_srf", "child_srf") for table, _col in keys), keys
    joins = _ends(onboard_pg._detect_internal_joinpaths(_HOSTILE, "hostile"))
    assert not any({"parent_srf", "child_srf"} & {j["from_table"], j["to_table"]}
                   for j in joins), joins


def test_every_emitted_view_key_is_backed_by_view_column_usage(onboard_pg, dbs):
    """Cross-check with PostgreSQL's own lineage view (readable here because
    the test login owns the tables): each emitted key's view column is a
    column the view reads from its base table."""
    for schema, conn in ((_ERP, dbs["erp"]), (_HOSTILE, dbs["seed"])):
        joins = onboard_pg._view_foreign_keys(schema)
        assert joins, schema
        with conn.cursor() as cur:
            cur.execute("""SELECT view_name, column_name FROM information_schema.view_column_usage
                           WHERE view_schema = %s""", (schema,))
            used = {tuple(r) for r in cur.fetchall()}
        conn.rollback()
        for join in joins:
            assert (join["from_table"], join["from_field"]) in used, (schema, join)
            assert (join["to_table"], join["to_field"]) in used, (schema, join)


def test_a_raw_table_keeps_its_hop_to_the_view_that_serves_it(onboard_pg):
    """The CMMS schema serves fresh dates through views over its raw tables;
    the raw-to-view hops main detected are unchanged."""
    joins = _ends(onboard_pg._detect_internal_joinpaths("cmms_maintenance", "cmms"))
    for expected in (
            ("maintenance_schedules_raw", "schedule_id", "maintenance_schedules", "schedule_id"),
            ("work_orders_raw", "work_order_id", "work_orders", "work_order_id"),
            ("work_orders_raw", "asset_id", "assets", "asset_id")):
        assert dict(zip(("from_table", "from_field", "to_table", "to_field"),
                        expected)) in joins, expected


# --------------------------------------------------------------------------- #
# The baked keys never read as the only joins
# --------------------------------------------------------------------------- #
def test_the_plm_second_reference_stays_usable(onboard_pg, dbs, monkeypatch):
    """PLM declares ``bom_items.parent_item`` as a key to ``products``; the
    child item refers to the same table with no declared key and is a valid
    join on the seeded data. The baked text names the declared key, keeps the
    child item in the table entry, and says other joins remain valid."""
    from agents.response_integrity_sql import parse_physical_schema
    from agents.schema_format import JOIN_KEYS_PREFIX
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    desc = onboard_pg._dp_pg_tables_desc(
        _ctx_for(onboard_pg, "plm", "plm_engineering", dbs["seed"]))
    keys = desc.split(JOIN_KEYS_PREFIX, 1)[1]
    assert "bom_items.parent_item = products.item_id" in keys
    assert "child_item" not in keys
    assert "not the only valid joins" in JOIN_KEYS_PREFIX
    assert "child_item" in parse_physical_schema(desc)["bom_items"]["columns"]
    with dbs["seed"].cursor() as cur:
        cur.execute("""SELECT count(*) FROM plm_engineering.bom_items b
                       JOIN plm_engineering.products p ON p.item_id = b.child_item""")
        assert cur.fetchone()[0] > 0
    dbs["seed"].rollback()


def test_the_erp_batch_number_stays_usable(onboard_pg, erp_conn, monkeypatch):
    """The shipment line's batch id joins the MES run number (derived by the
    exact-agreement pass, test_story_traceability_pg.py) with no declared key;
    the baked ERP keys do not name it and do not rule it out."""
    from agents.response_integrity_sql import parse_physical_schema
    from agents.schema_format import JOIN_KEYS_PREFIX
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    desc = onboard_pg._dp_pg_tables_desc(_ctx(onboard_pg, _ERP, _views(erp_conn, _ERP)))
    keys = desc.split(JOIN_KEYS_PREFIX, 1)[1]
    assert "batch_id" not in keys
    assert "batch_id" in parse_physical_schema(desc)["shipment_items"]["columns"]
    assert "a shared business number or code" in JOIN_KEYS_PREFIX
    assert "a join given in the question" in JOIN_KEYS_PREFIX


#: The KNOWN CONCEPT MAPPINGS a rehearsal account's ERP agent carried for the
#: batch concept (rehearsal 2026-10-17, Quick exact-6): both batch_id columns
#: mapped as the batch identifier.
_BATCH_ITEMS = [{"concept": "batch", "field": "lot_mes_tracking.batch_id", "role": "identifier"},
                {"concept": "batch", "field": "shipment_items.batch_id", "role": "identifier"},
                {"concept": "customer", "field": "shipments.customer_id", "role": "identifier"},
                {"concept": "customer", "field": "customer_orders.customer_id",
                 "role": "identifier"}]


def test_the_two_batch_id_columns_are_measured_to_hold_different_identifiers(
        onboard_pg, erp_conn, monkeypatch):
    """Rehearsal 2026-10-17: the ERP agent sent work-order-form batch ids to
    ``shipment_items.batch_id`` (which holds run numbers), got no rows, and
    rewrote the prefix by hand. On the real DDL and the live-story history
    the two columns share no value, so the baked schema names the pair; the
    customer_id columns, tied to one key by the foreign keys, are not read."""
    from agents.response_integrity_sql import parse_physical_schema
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX, declared_join_keys
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    ctx = _ctx(onboard_pg, _ERP, _views(erp_conn, _ERP))
    ctx.cm_items = list(_BATCH_ITEMS)
    desc = onboard_pg._dp_pg_tables_desc(ctx)
    found = desc.split(DISTINCT_IDENTIFIERS_PREFIX, 1)[1]
    assert found.startswith("batch: lot_mes_tracking.batch_id and shipment_items.batch_id, ")
    assert [(p["column"], p["table_a"], p["table_b"]) for p in ctx.distinct_identifiers] == [
        ("batch_id", "lot_mes_tracking", "shipment_items")]
    assert min(ctx.distinct_identifiers[0]["read_a"], ctx.distinct_identifiers[0]["read_b"]) >= 5
    assert ctx.distinct_identifiers_not_measured == []
    assert "customer_id" not in found
    assert set(parse_physical_schema(desc)) == set(_views(erp_conn, _ERP))
    assert ("shipment_items", "line_item_id", "order_line_items", "line_item_id") in (
        declared_join_keys(desc))


def test_the_measured_batch_id_pair_is_stored_in_the_graph_and_the_note_is_its_rendering(
        onboard_pg, erp_conn, graph, monkeypatch):
    """The pair is written as one edge between the two batch_id Field nodes,
    with each side's stored signature the one the values stored there have,
    and the baked note is rendered from that record."""
    import json
    from agents.schema_format import with_distinct_identifiers
    from agents.system_agent import _shape_signature
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    ctx = _ctx(onboard_pg, _ERP, _views(erp_conn, _ERP))
    ctx.cm_items = list(_BATCH_ITEMS)
    desc = onboard_pg._dp_pg_tables_desc(ctx)
    (edge,) = graph.edges
    assert (edge["from"], edge["to"]) == ("erp.lot_mes_tracking.batch_id",
                                          "erp.shipment_items.batch_id")
    assert edge["props"]["basis"] == "measured" and edge["props"]["concept"] == "batch"
    with erp_conn.cursor() as cur:
        cur.execute(f"SELECT batch_id FROM {_ERP}.lot_mes_tracking LIMIT 1")
        lot = cur.fetchone()[0]
        cur.execute(f"SELECT batch_id FROM {_ERP}.shipment_items LIMIT 1")
        ship = cur.fetchone()[0]
    erp_conn.rollback()
    assert json.loads(edge["props"]["signature_a"]) == json.loads(json.dumps(_shape_signature(lot)))
    assert json.loads(edge["props"]["signature_b"]) == json.loads(
        json.dumps(_shape_signature(ship)))
    assert "shape_a" not in edge["props"] and "shape_b" not in edge["props"]
    (record,) = ctx.distinct_identifier_records
    assert (record["read_a"], record["read_b"]) == (edge["props"]["read_a"],
                                                    edge["props"]["read_b"])
    base = desc.split(" Same-named columns", 1)[0]
    assert desc == with_distinct_identifiers(base, [record])


def _forms_desc(onboard, tables, monkeypatch):
    """The baked schema of ``tables`` in the synthetic forms schema, their
    same-named ``ref`` columns mapped as identifiers of one concept."""
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    ctx = _ctx(onboard, _FORMS, tables)
    ctx.cm_items = [{"concept": "unit", "field": f"{t}.ref", "role": "identifier"}
                    for t in tables]
    return ctx, onboard._dp_pg_tables_desc(ctx)


def test_a_value_found_only_by_the_lookup_into_the_other_column_names_nothing(
        onboard_pg, dbs, monkeypatch):
    """The two shapes differ and neither column's sampled ends hold the
    shared value, so only the EXISTS lookup into the other column sees it:
    the pair is read and not named."""
    from agents import distinct_identifiers as di
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX
    ctx, desc = _forms_desc(onboard_pg, ["held_a", "held_b"], monkeypatch)
    assert DISTINCT_IDENTIFIERS_PREFIX not in desc
    assert ctx.distinct_identifiers == [] and ctx.distinct_identifiers_not_measured == []
    # The setup is what the test claims: the shared value is read on side a
    # only, and the lookup reports it there.
    pair = {"concept": "unit", "column": "ref", "table_a": "held_a", "table_b": "held_b"}
    rows = support.routed_pg(dbs)(_FORMS, di.measure_sql(_FORMS, pair, same_type=True),
                                  di.measure_params())["rows"]
    assert [r for r in rows if r[1] == "BP-0150-XQ"] == [["a", "BP-0150-XQ", True]]
    assert not any(r[2] for r in rows if r[1] != "BP-0150-XQ")


def test_same_shaped_disjoint_columns_are_not_named(onboard_pg, monkeypatch):
    """Two populations of one identifier share no value but have one shape:
    that is not evidence of two identifiers."""
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX
    ctx, desc = _forms_desc(onboard_pg, ["same_a", "same_b"], monkeypatch)
    assert DISTINCT_IDENTIFIERS_PREFIX not in desc
    assert ctx.distinct_identifiers == [] and ctx.distinct_identifiers_not_measured == []


def test_different_shapes_and_declared_types_with_no_shared_value_are_named(
        onboard_pg, monkeypatch):
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX
    ctx, desc = _forms_desc(onboard_pg, ["diff_a", "diff_b"], monkeypatch)
    assert desc.endswith(DISTINCT_IDENTIFIERS_PREFIX
                         + "unit: diff_a.ref and diff_b.ref, 10 and 12 values read, "
                         + "shaped like KX-9999 and QZ-9999.")
    assert ctx.distinct_identifiers_not_measured == []


def test_a_shape_the_note_cannot_print_is_stored_and_guards(onboard_pg, graph, dbs,
                                                            monkeypatch):
    """``spaced_a`` holds ``KX 0001``-shaped values: a space, which a baked
    list entry cannot hold. The note names the pair without shapes, the graph
    keeps both signatures, and the guard built from the record refuses the
    lookup that finds no rows on PostgreSQL."""
    import json
    from agents import identifier_shape_guard as g
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX
    from agents.system_agent import _shape_signature
    ctx, desc = _forms_desc(onboard_pg, ["diff_b", "spaced_a"], monkeypatch)
    assert desc.endswith(DISTINCT_IDENTIFIERS_PREFIX
                         + "unit: diff_b.ref and spaced_a.ref, 12 and 10 values read.")
    (edge,) = graph.edges
    assert json.loads(edge["props"]["signature_a"]) == json.loads(
        json.dumps(_shape_signature("QZ-0001")))
    assert json.loads(edge["props"]["signature_b"]) == json.loads(
        json.dumps(_shape_signature("KX 0001")))
    context = g.context_from(desc, records=ctx.distinct_identifier_records)
    routed = support.routed_pg(dbs)
    for table, value in (("spaced_a", "QZ-0003"), ("diff_b", "KX 0003")):
        found = routed(_FORMS, f'SELECT t.ref FROM "{_FORMS}"."{table}" t WHERE t.ref = %s',
                       (value,))
        assert found["rows"] == [], (table, value)
        out = g.check(f"SELECT t.ref FROM {table} t WHERE t.ref = '{value}'", context)
        assert out["identifier_check"]["kind"] == "wrong_form", (table, value)
    assert g.check("SELECT t.ref FROM spaced_a t WHERE t.ref = 'KX 0003'", context) is None


def test_a_read_past_its_deadline_names_nothing_and_does_not_stall_the_provision(
        onboard_pg, monkeypatch):
    import time
    from agents import distinct_identifiers as di
    from agents.response_integrity_sql import parse_physical_schema
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX
    monkeypatch.setattr(di, "QUERY_TIMEOUT_MS", 300)
    started = time.monotonic()
    ctx, desc = _forms_desc(onboard_pg, ["diff_a", "slow_b"], monkeypatch)
    assert time.monotonic() - started < 2.5          # the full read sleeps for 3 s
    assert DISTINCT_IDENTIFIERS_PREFIX not in desc
    assert [p["reason"] for p in ctx.distinct_identifiers_not_measured] == ["timeout"]
    assert set(parse_physical_schema(desc)) == {"diff_a", "slow_b"}
