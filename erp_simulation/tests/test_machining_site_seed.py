# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One-ERP site data in the db-init seed (lambda-db-init/handler.py).

The handler is loaded by path with a stand-in boto3 module (the same
way erp_simulation/seed_data.py loads it) and exercised with a recording
cursor, so these tests run offline.

The characterization tests pin the view SQL that HEAD produced before
``get_views_sql`` was decomposed: the legacy three sites and the ERP data views
must stay byte-identical; the only allowed differences in ``get_views_sql`` are
the added MACHINING branch of ``customer_product_categories`` and the ADDITIVE
per-site ``production_runs`` view (live-story traceability, 2026-09-29:
``_RUNS_VIEW`` statements are stripped before hashing, so every pre-existing
statement is still pinned byte-for-byte).
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import re
import sys
import types
import uuid
from pathlib import Path

import pytest

from erp_simulation.tests._pg_sql import stmt

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"

# sha256 of the pinned get_views_sql() / get_machining_views_sql() output.
_HEAD_VIEWS_SHA = "7ca9a1b44f525e5698c025652730c21eb4b493d3158cfec22fc60e82964802e1"
_HEAD_MACHINING_VIEWS_SHA = "9fdd7bb4223d87e4418a623e69c3345e8f9461a91f4c2135cb8fa082bfcb6890"
# The one line the one-ERP change adds to get_views_sql().
_MACHINING_WHEN_LINE = "        WHEN 'MACHINING' THEN ARRAY['Machined Components']\n"
# The additive per-site production_runs view statement (one line each).
_RUNS_VIEW = re.compile(r"CREATE OR REPLACE VIEW \w+\.production_runs AS [^\n]*;\n")
# The additive per-site equipment registry view statement (one line each).
_EQUIPMENT_VIEW = re.compile(r"CREATE OR REPLACE VIEW \w+\.equipment AS [^\n]*;\n")


def _additive(sql: str) -> str:
    """The view SQL without the statements added after the pinned HEAD."""
    return _EQUIPMENT_VIEW.sub("", _RUNS_VIEW.sub("", sql))


_LEGACY_SITES = (
    ("mes_milwaukee", "MILWAUKEE_BOTTLING", "MKE"),
    ("mes_detroit", "DETROIT_AUTOPARTS", "DET"),
    ("mes_cedarrapids", "CEDARRAPIDS_FOOD", "CR"),
)
_SITE_VIEWS = {
    "work_orders", "production_events", "quality_checks", "downtime_events",
    "material_consumption", "production_lines", "line_status", "production_runs",
    "equipment",
}


@pytest.fixture(scope="module")
def handler():
    stand_ins = {}
    for name in ("boto3",):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            stand_ins[name] = True
    try:
        spec = importlib.util.spec_from_file_location("db_init_handler_t3", _HANDLER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in stand_ins:
            del sys.modules[name]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _views(sql: str) -> dict:
    """``schema.view`` -> full CREATE OR REPLACE VIEW statement."""
    return {m.group(1): m.group(0) for m in re.finditer(
        r"CREATE OR REPLACE VIEW ([\w.]+) AS\s.*?;", sql, re.S)}


def test_views_sql_characterization(handler):
    sql = handler.get_views_sql()
    assert _sha(_additive(sql).replace(_MACHINING_WHEN_LINE, "")) == _HEAD_VIEWS_SHA
    views = _views(sql)
    for schema, site, prefix in _LEGACY_SITES:
        assert {v.split(".", 1)[1] for v in views if v.startswith(schema + ".")} == _SITE_VIEWS
        assert f"WHERE w.site = '{site}';" in views[f"{schema}.quality_checks"]
        assert views[f"{schema}.work_orders"].endswith(f"WHERE site = '{site}';")
        assert views[f"{schema}.production_lines"].endswith(f"WHERE site_id = '{prefix}';")
        assert views[f"{schema}.production_runs"].endswith(f"WHERE w.site = '{site}');")
    assert len([v for v in views if v.startswith("erp_data.")]) == 25
    assert "customer_product_categories" in views


def test_machining_views_sql_characterization(handler):
    sql = handler.get_machining_views_sql()
    assert _sha(_additive(sql)) == _HEAD_MACHINING_VIEWS_SHA
    assert len(_RUNS_VIEW.findall(sql)) == 1
    assert len(_EQUIPMENT_VIEW.findall(sql)) == 1


def _sql_text(sql) -> str:
    """The statement text of a str or a ``pgdriver.sql`` composition (a quoted
    name as the server receives it)."""
    from erp_simulation.pgdriver import sql as psql

    if isinstance(sql, psql.Composed):
        return "".join(_sql_text(part) for part in sql.seq)
    if isinstance(sql, psql.SQL):
        return sql.string
    if isinstance(sql, psql.Identifier):
        return ".".join('"' + part.replace('"', '""') + '"' for part in sql.strings)
    return sql


class _RecordingCursor:
    """Records every execute(sql, params); fetchone answers the lot count."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(_sql_text(sql).split()), params))

    def fetchone(self):
        return (1,)


@pytest.fixture
def seeded(handler):
    cursor = _RecordingCursor()
    handler.seed_machining_site(cursor)
    return cursor.calls


def _index(calls, predicate):
    return [i for i, (sql, params) in enumerate(calls) if predicate(sql, params)]


def test_machining_customers_inserted(handler, seeded):
    inserts = [p for sql, p in seeded if sql.startswith("INSERT INTO customers")]
    assert [p[0] for p in inserts] == [f"CUST-MCH-{n:04d}" for n in range(1, 5)]
    assert all("'MACHINING'" in sql for sql, _ in seeded if sql.startswith("INSERT INTO customers"))
    assert all("ON CONFLICT (customer_code) DO NOTHING" in sql
               for sql, _ in seeded if sql.startswith("INSERT INTO customers"))
    for _code, company, _contact, email, phone in inserts:
        assert company.split()[0] in {"AnyCompany", "Example"}
        assert email.endswith("@example.com")
        assert re.fullmatch(r"\(202\) 555-01\d\d", phone)
    # the segment column is ensured before the first insert (older DDL home)
    alter = _index(seeded, lambda s, _: "ADD COLUMN IF NOT EXISTS customer_segment" in s)
    first = _index(seeded, lambda s, _: s.startswith("INSERT INTO customers"))[0]
    assert alter and alter[0] < first


def test_plm_xref_applied_after_machined_products(handler, seeded):
    product_inserts = _index(seeded, lambda s, p: s.startswith("INSERT INTO products")
                             and p and str(p[0]).startswith("MACH-"))
    xref_updates = _index(seeded, lambda s, p: s.startswith("UPDATE products SET engineering_part_number")
                          and p and p[1].startswith("MACH-"))
    assert len(product_inserts) == 4 and len(xref_updates) == 4
    assert min(xref_updates) > max(product_inserts)
    assert {seeded[i][1] for i in xref_updates} == {
        (pn, sku) for sku, pn in handler.PLM_PART_XREF.items() if sku.startswith("MACH-")}


def test_dimensional_check_type_allowed(handler, seeded):
    alters = [s for s, _ in seeded if "mes_quality_checks_check_type_check" in s]
    assert len(alters) == 1
    sql = alters[0]
    assert "DROP CONSTRAINT IF EXISTS mes_quality_checks_check_type_check" in sql
    for check_type in ("VISUAL", "WEIGHT", "FILL_LEVEL", "CAP_TORQUE", "LABEL_PLACEMENT",
                       "LEAK_TEST", "DIMENSIONAL"):
        assert f"'{check_type}'" in sql


class _ConstraintCursor(_RecordingCursor):
    """fetchone answers the constraint-definition probe with ``definition``."""

    def __init__(self, definition):
        super().__init__()
        self.definition = definition

    def fetchone(self):
        return None if self.definition is None else (self.definition,)


def _check_alters(calls):
    return [s for s, _ in calls if s.startswith("ALTER TABLE mes_quality_checks")]


def test_dimensional_widen_is_skipped_when_already_allowed(handler):
    cursor = _ConstraintCursor("CHECK (check_type IN ('VISUAL', 'DIMENSIONAL'))")
    handler._allow_machining_quality_checks(cursor)
    assert _check_alters(cursor.calls) == []
    probe_sql, probe_params = cursor.calls[0]
    assert "pg_get_constraintdef" in probe_sql and "mes_quality_checks" in probe_sql
    assert probe_params == ("mes_quality_checks_check_type_check",)


@pytest.mark.parametrize("definition", [None, "CHECK (check_type IN ('VISUAL'))"])
def test_dimensional_widen_runs_when_missing(handler, definition):
    cursor = _ConstraintCursor(definition)
    handler._allow_machining_quality_checks(cursor)
    assert len(_check_alters(cursor.calls)) == 1


def test_site_event_columns_added(seeded):
    added = [s for s, _ in seeded if s.startswith("ALTER TABLE simulation_events")]
    assert added == [f"ALTER TABLE simulation_events ADD COLUMN IF NOT EXISTS \"mes_ind_{k}\" INTEGER DEFAULT 0"
                     for k in ("scheduled", "running", "completed")]


def test_customer_categories_view_has_machining(handler):
    view = _views(handler.get_views_sql())["customer_product_categories"]
    assert "WHEN 'MACHINING' THEN ARRAY['Machined Components']" in view
    assert handler.SEGMENT_CATEGORIES["MACHINING"] == [handler.INDIANAPOLIS_CATEGORY]
    assert list(handler.SEGMENT_CATEGORIES) == ["BEVERAGE", "AUTOMOTIVE", "FOOD", "MACHINING"]


def test_machining_views_match_legacy_site_view_set(handler):
    views = _views(handler.get_machining_views_sql())
    assert {v.split(".", 1)[1] for v in views if v.startswith("mes_indianapolis.")} == _SITE_VIEWS
    assert all(f"'{handler.INDIANAPOLIS_SITE_CODE}'" in v or f"'{handler.INDIANAPOLIS_LINE_PREFIX}'" in v
               for v in views.values())


# --------------------------------------------------------------------------
# MES equipment registry (mes_equipment + one equipment view per site schema)
# --------------------------------------------------------------------------
_EQUIPMENT_COLUMNS = ["line", "equipment_id", "equipment_name", "equipment_type", "status"]
_ALL_SITE_SCHEMAS = _LEGACY_SITES + (("mes_indianapolis", "INDIANAPOLIS_MACHINING", "IND"),)


@pytest.mark.parametrize("schema, site, _prefix", _ALL_SITE_SCHEMAS)
def test_every_site_mes_schema_exposes_its_equipment_registry(handler, schema, site, _prefix):
    """The site's machines, filtered by site, with no site column (the schema
    is the site)."""
    views = _views(handler.get_views_sql() + handler.get_machining_views_sql())
    assert views[f"{schema}.equipment"] == (
        f"CREATE OR REPLACE VIEW {schema}.equipment AS SELECT "
        f"line, equipment_id, equipment_name, equipment_type, status "
        f"FROM public.mes_equipment WHERE site = '{site}';")


def _seeded_line_status(handler) -> list:
    """(site, line) of every mes_line_status row the seed writes."""
    cursor = _RecordingCursor()
    handler.seed_mes_line_status(cursor)
    handler._seed_indianapolis_lines(cursor)
    return [p[:2] for sql, p in cursor.calls if sql.startswith("INSERT INTO mes_line_status")]


def test_mes_equipment_registry_has_one_work_center_per_seeded_line(handler):
    rows = handler.MES_EQUIPMENT
    lines = [(site, line) for site, line, *_rest in rows]
    assert sorted(lines) == sorted(_seeded_line_status(handler))
    assert len(set(lines)) == len(lines)
    ids = [equipment_id for _s, _l, equipment_id, *_rest in rows]
    assert len(set(ids)) == len(ids), "an equipment id is used at two sites"
    for site in {site for site, *_rest in rows}:
        names = [name.casefold() for s, _l, _i, name, _t in rows if s == site]
        assert len(set(names)) == len(names), site


def test_story_line_machine_carries_the_story_ids_and_the_cmms_name(handler):
    """The story line's machine: the story's MES equipment id, and as its name
    the site CMMS's assets.description of the story asset, character for
    character."""
    from erp_simulation import condition_monitor as cm
    from erp_simulation import story_history

    (row,) = [r for r in handler.MES_EQUIPMENT
              if r[0] == handler.INDIANAPOLIS_SITE_CODE and r[1] == cm.STORY_MES_LINE]
    assert row[2] == cm.STORY_MES_EQUIPMENT_ID
    cmms = (_HANDLER_PATH.parents[1] / "lambda-adt-seed" / "011_cmms_indianapolis.sql").read_text()
    match = re.search(r"^\('" + re.escape(cm.STORY_CMMS_ASSET_NUM) + r"',\s*'((?:[^']|'')*)'", cmms, re.M)
    assert match, "story asset row not found in the site CMMS seed"
    assert row[3] == match.group(1).replace("''", "'")
    ind_lines = {line for site, line, *_rest in handler.MES_EQUIPMENT if site == handler.INDIANAPOLIS_SITE_CODE}
    assert set(story_history.LINES) <= ind_lines


def test_seed_mes_equipment_creates_the_table_then_inserts_every_row_idempotently(handler):
    cursor = _RecordingCursor()
    handler.seed_mes_equipment(cursor)
    sqls = [sql for sql, _ in cursor.calls]
    assert sqls[0].startswith("CREATE TABLE IF NOT EXISTS mes_equipment (")
    assert "PRIMARY KEY (site, equipment_id)" in sqls[0] and "UNIQUE (site, equipment_name)" in sqls[0]
    inserts = [(sql, p) for sql, p in cursor.calls if sql.startswith("INSERT INTO mes_equipment")]
    assert [p for _, p in inserts] == [tuple(r) for r in handler.MES_EQUIPMENT]
    assert all(sql.endswith("ON CONFLICT DO NOTHING") for sql, _ in inserts)
    assert not [sql for sql in sqls if _DESTRUCTIVE.search(sql)]


def test_machined_product_names_follow_plm_families(seeded):
    names = {p[0]: p[1] for s, p in seeded if s.startswith("INSERT INTO products") and p}
    assert names == {
        "MACH-HSG-7742": "Precision Shaft 7742",
        "MACH-SHFT-8810": "Drive Coupling 8810",
        "MACH-BRKT-9901": "Aerospace Bracket 9901",
        "MACH-MNFD-5520": "Hydraulic Manifold Block 5520",
    }


def test_second_ddl_home_mirrors_the_changes():
    schema = _HANDLER_PATH.parents[1] / "erp_simulation" / "schema"
    assert "customer_segment VARCHAR(50)" in (schema / "01_customers_suppliers.sql").read_text()
    assert "'DIMENSIONAL'" in (schema / "08_mes_tracking.sql").read_text()
    events = (schema / "11_simulation_events.sql").read_text()
    for column in ("mes_ind_scheduled", "mes_ind_running", "mes_ind_completed"):
        assert f"{column} INTEGER DEFAULT 0" in events


# --------------------------------------------------------------------------
# Real PostgreSQL (throwaway container): ERP_SIM_TEST_PG_DSN; skipped otherwise.
# --------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def pg_seeded(handler):
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    # A throwaway database per run (the handler DDL targets public and
    # creates the view schemas), dropped afterwards.
    db_name = "t3_" + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))
    conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": db_name}))
    try:
        handler.create_schema(conn)
        handler.seed_data(conn)
        _reseed_machining_site(handler, conn)  # a re-run must stay idempotent
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db}", db=db_name))
        admin.close()


def _reseed_machining_site(handler, conn):
    """Run the machining seed and both view builders a SECOND time."""
    with conn.cursor() as cur:
        handler.seed_machining_site(cur)
        cur.execute(handler.get_views_sql())
        cur.execute(handler.get_machining_views_sql())
    conn.commit()


def test_pg_machining_seed_rerun_keeps_one_dimensional_constraint(pg_seeded):
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'mes_quality_checks'::regclass AND contype = 'c'")
        defs = [r[0] for r in cur.fetchall()]
        assert len([d for d in defs if "DIMENSIONAL" in d]) == 1, defs


def test_pg_machining_seed(pg_seeded):
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT count(*) FROM customers WHERE customer_segment = 'MACHINING'")
        assert cur.fetchone()[0] == 4
        cur.execute("SELECT sku, engineering_part_number FROM products WHERE sku LIKE 'MACH-%' ORDER BY sku")
        rows = cur.fetchall()
        assert len(rows) == 4 and all(pn for _, pn in rows)
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'mes_quality_checks_check_type_check'")
        assert "DIMENSIONAL" in cur.fetchone()[0]
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'simulation_events' AND column_name LIKE 'mes_ind_%'")
        assert {r[0] for r in cur.fetchall()} == {"mes_ind_scheduled", "mes_ind_running", "mes_ind_completed"}
        cur.execute("SELECT allowed_categories FROM customer_product_categories "
                    "WHERE customer_segment = 'MACHINING' LIMIT 1")
        assert cur.fetchone()[0] == ["Machined Components"]
        cur.execute("SELECT count(*) FROM mes_indianapolis.line_status")
        assert cur.fetchone()[0] == 3


def test_pg_manifold_product_fg_and_bom_seed_once(pg_seeded):
    """The fourth machined SKU (frozen PLM margin, D17) lands exactly once
    after the idempotent reseed: product with its PLM cross-reference, the
    finished-good item linked to it, its four BOM lines and its FG balance."""
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT count(*), max(engineering_part_number), max(category) FROM products "
                    "WHERE sku = 'MACH-MNFD-5520'")
        assert cur.fetchone() == (1, "PN-5520-REV-B", "Machined Components")
        cur.execute("SELECT count(*) FROM inventory_items i JOIN products p ON p.product_id = i.product_id "
                    "WHERE i.item_code = 'FG-MNFD-5520' AND p.sku = 'MACH-MNFD-5520' "
                    "AND i.item_type = 'FINISHED_GOOD'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT i.item_code FROM bill_of_materials b "
                    "JOIN products p ON p.product_id = b.product_id "
                    "JOIN inventory_items i ON i.item_id = b.component_item_id "
                    "WHERE p.sku = 'MACH-MNFD-5520' ORDER BY i.item_code")
        assert [r[0] for r in cur.fetchall()] == [
            "BAR-AL7075-150", "COOL-SYN-IND", "INS-CARB-IND", "PKG-CRATE-SM"]
        cur.execute("SELECT b.quantity_on_hand FROM inventory_balances b "
                    "JOIN inventory_items i ON i.item_id = b.item_id "
                    "JOIN inventory_locations l ON l.location_id = b.location_id "
                    "WHERE i.item_code = 'FG-MNFD-5520' AND l.location_code = 'IND-FG'")
        assert [float(r[0]) for r in cur.fetchall()] == [80.0]


def _equipment_state(conn) -> dict:
    """Each site schema's equipment view: its columns and its rows."""
    state = {}
    with conn.cursor() as cur:
        for schema, _site, _prefix in _ALL_SITE_SCHEMAS:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = 'equipment' ORDER BY ordinal_position",
                        (schema,))
            columns = [r[0] for r in cur.fetchall()]
            cur.execute(stmt("SELECT * FROM {rel} ORDER BY line, equipment_id", rel=(schema, "equipment")))
            state[schema] = (columns, cur.fetchall())
    conn.rollback()
    return state


def test_pg_equipment_registry_reads_one_machine_per_line_in_every_site_schema(handler, pg_seeded):
    from erp_simulation import condition_monitor as cm

    state = _equipment_state(pg_seeded)
    for schema, site, _prefix in _ALL_SITE_SCHEMAS:
        columns, rows = state[schema]
        assert columns == _EQUIPMENT_COLUMNS, schema
        expected = sorted((line, eid, name, etype, "ACTIVE")
                          for s, line, eid, name, etype in handler.MES_EQUIPMENT if s == site)
        assert sorted(rows) == expected and len(rows) == 3, schema
    ind = {line: eid for line, eid, *_rest in state["mes_indianapolis"][1]}
    assert ind[cm.STORY_MES_LINE] == cm.STORY_MES_EQUIPMENT_ID
    with pg_seeded.cursor() as cur:                    # every line a work order may run on
        cur.execute("SELECT s.site, s.line, count(e.equipment_id) FROM mes_line_status s "
                    "LEFT JOIN mes_equipment e ON e.site = s.site AND e.line = s.line "
                    "GROUP BY s.site, s.line")
        assert {count for _s, _l, count in cur.fetchall()} == {1}
    pg_seeded.rollback()


def _as_account_seeded_before_the_equipment_registry(conn) -> None:
    """Give a fresh database the shape an account initialised before the
    equipment registry has: no site equipment views, no mes_equipment."""
    with conn.cursor() as cur:
        for schema, _site, _prefix in _ALL_SITE_SCHEMAS:
            cur.execute(stmt("DROP VIEW IF EXISTS {rel}", rel=(schema, "equipment")))
        cur.execute("DROP TABLE IF EXISTS public.mes_equipment")
    conn.commit()


def test_pg_seed_catalogue_upsert_adds_the_equipment_registry_to_an_existing_account(handler, pg_seeded):
    """An account seeded before the equipment registry: the seed-only Update
    creates the table, its rows and every site's view, changes no other table,
    and a second run changes nothing."""
    fresh_state, fresh_counts = _equipment_state(pg_seeded), _table_counts(pg_seeded)
    _as_account_seeded_before_the_equipment_registry(pg_seeded)
    without = _table_counts(pg_seeded)
    assert "mes_equipment" not in without
    handler.seed_catalogue_upsert(pg_seeded)
    assert _equipment_state(pg_seeded) == fresh_state
    assert _table_counts(pg_seeded) == {**without, "mes_equipment": len(handler.MES_EQUIPMENT)} == fresh_counts
    with pg_seeded.cursor() as cur:                    # a row the account already holds is kept
        cur.execute("UPDATE mes_equipment SET status = 'DOWN' WHERE equipment_id = 'L1-M1'")
    pg_seeded.commit()
    handler.seed_catalogue_upsert(pg_seeded)
    assert _table_counts(pg_seeded) == fresh_counts
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT status FROM mes_equipment WHERE equipment_id = 'L1-M1'")
        assert cur.fetchone() == ("DOWN",)
    pg_seeded.rollback()


# --------------------------------------------------------------------------
# Update path: a SeedVersion-only Update is an ADDITIVE catalogue upsert
# (stacks/erp_database_stack.py::db_init_seed_version); Create and every
# other Update keep the drop-and-reseed.
# --------------------------------------------------------------------------
_PROPS = {"ServiceToken": "arn:provider", "ClusterEndpoint": "db.example", "SecretArn": "arn:secret",
          "DatabaseName": "erp_simulation", "Timestamp": "2026-05-09-supplier-qualification"}
_DESTRUCTIVE = re.compile(r"(?i)\b(DROP\s+(TABLE|SCHEMA)|TRUNCATE|DELETE\s+FROM|pg_terminate_backend)\b")


def _update(old: dict, new: dict) -> dict:
    return {"RequestType": "Update", "OldResourceProperties": old, "ResourceProperties": new}


@pytest.mark.parametrize("old, new, expected", [
    ({**_PROPS, "SeedVersion": "a"}, {**_PROPS, "SeedVersion": "b"}, True),
    (_PROPS, {**_PROPS, "SeedVersion": "b"}, True),  # first deploy that carries it
    ({**_PROPS, "SeedVersion": "a"}, {**_PROPS, "SeedVersion": "b", "Timestamp": "bump"}, False),
    (_PROPS, {**_PROPS, "Timestamp": "bump"}, False),
    (_PROPS, {**_PROPS, "DatabaseName": "other"}, False),
    (_PROPS, {**_PROPS, "ClusterEndpoint": "new.example"}, False),
    (_PROPS, {**_PROPS, "ServiceToken": "arn:other"}, False),
    ({**_PROPS, "SeedVersion": "a"}, {**_PROPS, "SeedVersion": "a"}, False),
])
def test_is_seed_only_update(handler, old, new, expected):
    assert handler.is_seed_only_update(_update(old, new)) is expected


def test_create_and_delete_are_never_seed_only(handler):
    new = {**_PROPS, "SeedVersion": "b"}
    assert not handler.is_seed_only_update({"RequestType": "Create", "ResourceProperties": new})
    assert not handler.is_seed_only_update({"RequestType": "Delete", "ResourceProperties": new,
                                            "OldResourceProperties": _PROPS})


class _FakeConn:
    def __init__(self, fail_on=None):
        self.cursor_obj = _RecordingCursor()
        self.commits = 0
        self.closed = False
        self.fail_on = fail_on

    def cursor(self):
        conn = self

        class _Ctx:
            def __enter__(self):
                return conn.cursor_obj

            def __exit__(self, *exc):
                return False
        if self.fail_on:
            def execute(sql, params=None):
                if self.fail_on in _sql_text(sql):
                    raise RuntimeError("boom")
                self.cursor_obj.calls.append((" ".join(_sql_text(sql).split()), params))
            self.cursor_obj.execute = execute
        return _Ctx()

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@pytest.fixture
def lifecycle(handler, monkeypatch):
    """handler() wired to a fake connection; the reset helpers record calls.
    The function's own SeedVersion is unset unless a test sets it."""
    state = {"conn": _FakeConn(), "reset": []}
    monkeypatch.delenv("DB_INIT_SEED_VERSION", raising=False)
    monkeypatch.setattr(handler, "get_db_credentials", lambda: {"host": "h"})
    monkeypatch.setattr(handler, "get_connection", lambda creds: state["conn"])
    for name in ("drop_all_tables", "create_schema", "seed_data"):
        monkeypatch.setattr(handler, name, lambda conn, _n=name: state["reset"].append(_n))
    monkeypatch.setattr(handler, "verify_schema", lambda conn: state["reset"].append("verify_schema") or True)
    return state


def test_seed_only_update_upserts_the_catalogue_and_never_resets(handler, lifecycle):
    result = handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)
    assert result["Status"] == "SUCCESS"
    assert lifecycle["reset"] == []
    calls = lifecycle["conn"].cursor_obj.calls
    assert not [sql for sql, _ in calls if _DESTRUCTIVE.search(sql)]
    products = [p[0] for sql, p in calls if sql.startswith("INSERT INTO products")]
    assert "MACH-MNFD-5520" in products
    assert all("ON CONFLICT" in sql for sql, _ in calls if sql.startswith("INSERT"))
    assert any("CREATE OR REPLACE VIEW mes_indianapolis." in sql for sql, _ in calls)
    assert lifecycle["conn"].commits == 2 and lifecycle["conn"].closed


@pytest.mark.parametrize("event", [
    {"RequestType": "Create", "ResourceProperties": {**_PROPS, "SeedVersion": "b"}},
    _update(_PROPS, {**_PROPS, "Timestamp": "bump", "SeedVersion": "b"}),
])
def test_create_and_reset_updates_keep_the_drop_and_reseed(handler, lifecycle, event):
    assert handler.handler(event, None)["Status"] == "SUCCESS"
    assert lifecycle["reset"] == ["drop_all_tables", "create_schema", "seed_data", "verify_schema"]


def test_seed_upsert_failure_raises_and_commits_nothing_after_it(handler, lifecycle):
    """The CDK Provider behind db-init (stacks/erp_database_stack.py) reports
    the custom resource FAILED only when the handler raises; a returned
    ``{'Status': 'FAILED'}`` would read as success."""
    lifecycle["conn"] = _FakeConn(fail_on="INSERT INTO products")
    with pytest.raises(RuntimeError, match="boom"):
        handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)
    assert lifecycle["conn"].commits == 0 and lifecycle["conn"].closed
    assert lifecycle["reset"] == []


def _equipment_before_views(calls) -> None:
    """The equipment registry's table and rows come before the first view
    statement, so the view the site schemas gain finds its table."""
    table = _index(calls, lambda s, _: s.startswith("CREATE TABLE IF NOT EXISTS mes_equipment"))
    rows = _index(calls, lambda s, _: s.startswith("INSERT INTO mes_equipment"))
    views = _index(calls, lambda s, _: "CREATE OR REPLACE VIEW" in s)
    assert len(table) == 1 and rows and views, (table, rows, views)
    assert table[0] < min(rows) and max(rows) < views[0]
    assert any("CREATE OR REPLACE VIEW mes_indianapolis.equipment AS" in s for s, _ in calls)


def _handler_rows(calls) -> list:
    return [p for s, p in calls if s.startswith("INSERT INTO mes_equipment")]


def test_seed_only_update_seeds_the_equipment_registry_before_the_views(handler, lifecycle):
    handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)
    calls = lifecycle["conn"].cursor_obj.calls
    _equipment_before_views(calls)
    assert _handler_rows(calls) == [tuple(r) for r in handler.MES_EQUIPMENT]


def test_create_seed_seeds_the_equipment_registry_before_the_views(handler, monkeypatch):
    for name in ("_seed_reference_and_sites", "_seed_boms_inventory_and_lots", "seed_machining_site"):
        monkeypatch.setattr(handler, name, lambda cursor: None)
    conn = _FakeConn()
    handler.seed_data(conn)
    _equipment_before_views(conn.cursor_obj.calls)
    assert _handler_rows(conn.cursor_obj.calls) == [tuple(r) for r in handler.MES_EQUIPMENT]
    assert conn.commits == 2


def test_seed_only_update_raises_when_the_downtime_length_migration_refuses(handler, lifecycle):
    """The migration refusing (a dependent it cannot recreate) fails the
    seed-only Update: raised to the Provider, nothing committed, no view
    builder run after it, the connection closed, no reset."""
    lifecycle["conn"] = _FakeConn(fail_on="DO $migrate$")
    with pytest.raises(RuntimeError, match="boom"):
        handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)
    calls = [sql for sql, _p in lifecycle["conn"].cursor_obj.calls]
    assert any(sql.startswith("INSERT INTO products") for sql in calls)        # the catalogue ran first
    assert not [sql for sql in calls if "CREATE OR REPLACE VIEW" in sql]
    assert lifecycle["conn"].commits == 0 and lifecycle["conn"].closed
    assert lifecycle["reset"] == []


# --------------------------------------------------------------------------
# Update rollback: when a seed-only Update fails, CloudFormation rolls the
# stack back and sends the custom resource another Update with the PREVIOUS
# properties (SeedVersion = the old hash; OldResourceProperties = the failed
# one). If the function still runs the new code at that moment, running its
# upsert again would fail the same way and leave the stack in
# UPDATE_ROLLBACK_FAILED. The function knows its own code's SeedVersion from
# DB_INIT_SEED_VERSION (the stack sets it to the value it gives the custom
# resource, tests/test_seed_cr_versions.py) and runs the upsert only for that
# version; any other requested version is logged, skipped and succeeds.
# Without the variable (a function deployed before the stack set it) the
# upsert runs as before.
# --------------------------------------------------------------------------
_SEED_VERSION_ENV = "DB_INIT_SEED_VERSION"


def _rollback(failed: str, previous: str | None) -> dict:
    """The Update sent while rolling back a failed seed-only Update from
    ``previous`` (None: the stack before SeedVersion existed) to ``failed``."""
    restored = dict(_PROPS) if previous is None else {**_PROPS, "SeedVersion": previous}
    return _update({**_PROPS, "SeedVersion": failed}, restored)


def _record_connections(handler, monkeypatch, conn) -> list:
    opened = []
    monkeypatch.setattr(handler, "get_connection", lambda creds: opened.append(conn) or conn)
    return opened


def test_seed_only_update_runs_and_raises_when_it_asks_for_the_running_code(handler, lifecycle, monkeypatch):
    """A forward seed-only Update asks for the version of the code that is
    running (the stack computes both from the same file): the upsert runs,
    and a failure still raises to the Provider."""
    monkeypatch.setenv(_SEED_VERSION_ENV, "b")
    forward = _update({**_PROPS, "SeedVersion": "a"}, {**_PROPS, "SeedVersion": "b"})
    assert handler.handler(forward, None)["Status"] == "SUCCESS"
    assert lifecycle["conn"].commits == 2 and lifecycle["conn"].closed
    lifecycle["conn"] = _FakeConn(fail_on="DO $migrate$")
    with pytest.raises(RuntimeError, match="boom"):
        handler.handler(forward, None)
    assert lifecycle["conn"].commits == 0 and lifecycle["conn"].closed
    assert lifecycle["reset"] == []


@pytest.mark.parametrize("previous", ["a", None], ids=["previous-version", "before-seed-version"])
def test_seed_only_update_rollback_on_the_new_code_skips_and_succeeds(handler, lifecycle, monkeypatch,
                                                                     caplog, previous):
    """The rollback Update reaching the function while it still runs the new
    code ("b") asks for the previous version: nothing is opened, run or
    reset, the skip is logged with both versions, and the Update succeeds."""
    monkeypatch.setenv(_SEED_VERSION_ENV, "b")
    opened = _record_connections(handler, monkeypatch, lifecycle["conn"])
    caplog.set_level(logging.INFO)
    result = handler.handler(_rollback("b", previous), None)
    assert result["Status"] == "SUCCESS"
    assert "skipped" in result["Data"]["Message"]
    assert opened == [] and lifecycle["conn"].cursor_obj.calls == [] and lifecycle["conn"].commits == 0
    assert lifecycle["reset"] == []
    (skip,) = [r for r in caplog.records if "skipped" in r.getMessage()]
    assert repr(previous) in skip.getMessage() and "'b'" in skip.getMessage()


@pytest.mark.parametrize("env", [None, ""], ids=["unset", "empty"])
def test_seed_only_update_without_a_running_version_runs_as_before(handler, lifecycle, monkeypatch, env):
    """A function deployed before the stack passed its version: every
    seed-only Update, a rollback-shaped one included, runs the upsert and
    still raises on failure, as before the guard."""
    if env is None:
        monkeypatch.delenv(_SEED_VERSION_ENV, raising=False)
    else:
        monkeypatch.setenv(_SEED_VERSION_ENV, env)
    assert handler.handler(_rollback("b", "a"), None)["Status"] == "SUCCESS"
    assert lifecycle["conn"].commits == 2 and lifecycle["reset"] == []
    lifecycle["conn"] = _FakeConn(fail_on="DO $migrate$")
    with pytest.raises(RuntimeError, match="boom"):
        handler.handler(_rollback("b", "a"), None)


@pytest.mark.parametrize("event", [
    {"RequestType": "Create", "ResourceProperties": {**_PROPS, "SeedVersion": "a"}},
    _update({**_PROPS, "SeedVersion": "b"}, {**_PROPS, "Timestamp": "bump", "SeedVersion": "a"}),
], ids=["create", "reset-update"])
def test_create_and_reset_ignore_the_running_version(handler, lifecycle, monkeypatch, event):
    """The guard covers the seed-only Update only: a Create or a reset Update
    asking for another version still drops and reseeds."""
    monkeypatch.setenv(_SEED_VERSION_ENV, "b")
    assert handler.handler(event, None)["Status"] == "SUCCESS"
    assert lifecycle["reset"] == ["drop_all_tables", "create_schema", "seed_data", "verify_schema"]


_MANIFOLD_DELETES = (
    "DELETE FROM inventory_balances WHERE item_id IN (SELECT item_id FROM inventory_items WHERE item_code = 'FG-MNFD-5520')",
    "DELETE FROM bill_of_materials WHERE product_id IN (SELECT product_id FROM products WHERE sku = 'MACH-MNFD-5520')",
    "DELETE FROM inventory_items WHERE item_code = 'FG-MNFD-5520'",
    "DELETE FROM products WHERE sku = 'MACH-MNFD-5520'",
)


def _table_counts(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        tables = [r[0] for r in cur.fetchall()]
        counts = {}
        for table in tables:
            cur.execute(stmt("SELECT count(*) FROM {rel}", rel=("public", table)))
            counts[table] = cur.fetchone()[0]
    return counts


def test_pg_seed_catalogue_upsert_lands_the_manifold_on_an_existing_account(handler, pg_seeded):
    """An account initialised before MACH-MNFD-5520 existed: the upsert adds
    exactly its product, FG item, four BOM lines and FG balance, and keeps
    every other row, including one the simulator wrote since."""
    expected = _table_counts(pg_seeded)
    with pg_seeded.cursor() as cur:
        for sql in _MANIFOLD_DELETES:
            cur.execute(sql)
        cur.execute("INSERT INTO customers (customer_code, company_name, customer_segment, is_active) "
                    "VALUES ('CUST-SIM-9999', 'Written by the simulator', 'MACHINING', TRUE)")
    pg_seeded.commit()
    handler.seed_catalogue_upsert(pg_seeded)
    assert _table_counts(pg_seeded) == {**expected, "customers": expected["customers"] + 1}
    test_pg_manifold_product_fg_and_bom_seed_once(pg_seeded)
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT count(*) FROM customers WHERE customer_code = 'CUST-SIM-9999'")
        assert cur.fetchone()[0] == 1


# --------------------------------------------------------------------------
# Quality pass-rate defaults (2026-10-04): a fresh seed carries 0.998-1.0, and
# an existing account's seed-only update moves the row only while it is still
# the untouched old default (0.90-0.99).
# --------------------------------------------------------------------------
_PASS_RATE_KEYS = ("mes_quality_pass_rate_min", "mes_quality_pass_rate_max")


def test_seed_config_rows_carry_the_new_pass_rate_defaults(handler):
    rows = {row[0]: row for row in handler.SIMULATION_CONFIG_ROWS}
    assert (rows["mes_quality_pass_rate_min"][1], rows["mes_quality_pass_rate_min"][7]) == ("0.998", "0.998")
    assert (rows["mes_quality_pass_rate_max"][1], rows["mes_quality_pass_rate_max"][7]) == ("1.0", "1.0")
    cursor = _RecordingCursor()
    handler.seed_simulation_config(cursor)
    assert len(cursor.calls) == len(handler.SIMULATION_CONFIG_ROWS)
    assert all("ON CONFLICT (config_key) DO NOTHING" in sql for sql, _ in cursor.calls)


def _config_moves(calls) -> tuple:
    """The migration's simulation_config statements: (untouched-row moves,
    scenario-stamped moves, default-only moves); nothing else touches the
    table."""
    updates = [(sql, p) for sql, p in calls if sql.startswith("UPDATE simulation_config")]
    untouched = [(sql, p) for sql, p in updates if sql.endswith("AND updated_by IS NULL")]
    stamped = [(sql, p) for sql, p in updates if sql.endswith("AND updated_by = ANY(%s)")]
    defaults = [(sql, p) for sql, p in updates if sql.startswith("UPDATE simulation_config SET default_value")]
    assert len(untouched) + len(stamped) + len(defaults) == len(updates), updates
    return untouched, stamped, defaults


def test_migration_updates_only_untouched_rows(handler):
    cursor = _RecordingCursor()
    handler.migrate_moved_config_defaults(cursor)
    moved_keys = [key for key, _old, _new in handler.MOVED_CONFIG_DEFAULTS]
    assert set(_PASS_RATE_KEYS) <= set(moved_keys)
    untouched, stamped, defaults = _config_moves(cursor.calls)
    assert [p[2] for _, p in untouched] == [p[1] for _, p in defaults] == moved_keys
    for sql, (value, default, _key, old_values, old_defaults) in untouched:
        assert "config_value = ANY(%s) AND default_value = ANY(%s) AND updated_by IS NULL" in sql
        assert value == default and old_values == old_defaults and value not in old_values
    for sql, (default, _key, old_defaults) in defaults:
        assert "config_value" not in sql and default not in old_defaults
    # A scenario stamp moves only for the keys that name it, never a pass rate.
    stamps = handler.MOVED_CONFIG_SCENARIO_STAMPS
    assert [p[1] for _, p in stamped] == [key for key in moved_keys if key in stamps]
    assert not set(stamps) & set(_PASS_RATE_KEYS)
    for sql, (value, key, old_values, writers) in stamped:
        assert "SET config_value = %s, updated_at = NOW()" in sql and "default_value" not in sql
        assert "WHERE config_key = %s AND config_value = ANY(%s)" in sql
        assert writers == list(stamps[key]) and value not in old_values


def test_seed_only_update_moves_untouched_config_defaults(handler, lifecycle):
    assert handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)["Status"] == "SUCCESS"
    untouched, stamped, _defaults = _config_moves(lifecycle["conn"].cursor_obj.calls)
    keys = [p[2] for _sql, p in untouched]
    assert keys == [key for key, _old, _new in handler.MOVED_CONFIG_DEFAULTS]
    assert set(_PASS_RATE_KEYS) <= set(keys)
    assert [p[1] for _sql, p in stamped] == [_CAP_KEY]
    assert lifecycle["conn"].commits == 2 and lifecycle["reset"] == []


def _pass_rate_rows(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT config_key, config_value, default_value FROM simulation_config "
                    "WHERE config_key = ANY(%s)", (list(_PASS_RATE_KEYS),))
        return {key: (value, default) for key, value, default in cur.fetchall()}


def _set_pass_rate_row(conn, key, value, default, updated_by):
    with conn.cursor() as cur:
        cur.execute("UPDATE simulation_config SET config_value = %s, default_value = %s, updated_by = %s "
                    "WHERE config_key = %s", (value, default, updated_by, key))
    conn.commit()


def test_pg_existing_account_moves_untouched_pass_rate_defaults_only(handler, pg_seeded):
    assert _pass_rate_rows(pg_seeded) == {
        "mes_quality_pass_rate_min": ("0.998", "0.998"), "mes_quality_pass_rate_max": ("1.0", "1.0")}
    # An account seeded before the change: min is still the seed's old default
    # (updated_by NULL); max holds the old value, but a scenario wrote it. The
    # scenario's value stays, its stale default moves, so a dashboard reset
    # (config_value = default_value) cannot bring the old rate back.
    _set_pass_rate_row(pg_seeded, "mes_quality_pass_rate_min", "0.90", "0.90", None)
    _set_pass_rate_row(pg_seeded, "mes_quality_pass_rate_max", "0.99", "0.99", "scenario:normal")
    handler.seed_catalogue_upsert(pg_seeded)
    expected = {"mes_quality_pass_rate_min": ("0.998", "0.998"),
                "mes_quality_pass_rate_max": ("0.99", "1.0")}
    assert _pass_rate_rows(pg_seeded) == expected
    handler.seed_catalogue_upsert(pg_seeded)                    # a re-run changes nothing
    assert _pass_rate_rows(pg_seeded) == expected
    # A value someone set (not the old default) stays even without provenance.
    _set_pass_rate_row(pg_seeded, "mes_quality_pass_rate_max", "0.95", "0.99", None)
    handler.seed_catalogue_upsert(pg_seeded)
    assert _pass_rate_rows(pg_seeded)["mes_quality_pass_rate_max"] == ("0.95", "1.0")


# --------------------------------------------------------------------------
# Downtime cap (2026-10-17): a fresh seed caps random downtime at 60 minutes,
# inside the dashboard slider's existing 1..120 bounds, and an existing
# account's seed-only update moves the old 30-minute cap while it is still the
# untouched default or the value Normal Operations wrote.
# --------------------------------------------------------------------------
_CAP_KEY = "mes_downtime_duration_max"


def test_seed_config_rows_carry_the_new_downtime_cap_inside_their_slider_bounds(handler):
    rows = {row[0]: row for row in handler.SIMULATION_CONFIG_ROWS}
    assert (rows[_CAP_KEY][1], rows[_CAP_KEY][7]) == ("60", "60")
    assert (rows[_CAP_KEY][5], rows[_CAP_KEY][6]) == (1, 120)
    # The dashboard draws every int and float row as a slider over
    # min_value..max_value; a value outside that range breaks the panel.
    for key, value, dtype, _cat, _desc, low, high, default in handler.SIMULATION_CONFIG_ROWS:
        if dtype in ("int", "float"):
            assert low <= float(value) <= high and low <= float(default) <= high, key


def test_migration_moves_the_old_downtime_cap_untouched_or_written_by_normal_operations(handler):
    cursor = _RecordingCursor()
    handler.migrate_moved_config_defaults(cursor)
    untouched, stamped, defaults = _config_moves(cursor.calls)
    assert [p for _sql, p in untouched if p[2] == _CAP_KEY] == [("60", "60", _CAP_KEY, ["30"], ["30"])]
    assert [p for _sql, p in stamped] == [("60", _CAP_KEY, ["30"], ["scenario:normal"])]
    assert [p for _sql, p in defaults if p[1] == _CAP_KEY] == [("60", _CAP_KEY, ["30"])]
    # The slider bounds are not migrated: 60 sits inside the seeded 1..120.
    assert not [sql for sql, _p in cursor.calls if "max_value" in sql]
    assert not hasattr(handler, "MOVED_CONFIG_MAX_BOUNDS")


def _cap_row(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT config_value, default_value, max_value::int, updated_by FROM simulation_config "
                    "WHERE config_key = %s", (_CAP_KEY,))
        return cur.fetchone()


def _set_cap_row(conn, value, default, updated_by):
    with conn.cursor() as cur:
        cur.execute("UPDATE simulation_config SET config_value = %s, default_value = %s, updated_by = %s "
                    "WHERE config_key = %s", (value, default, updated_by, _CAP_KEY))
    conn.commit()


def test_pg_existing_account_moves_the_old_downtime_cap(handler, pg_seeded):
    assert _cap_row(pg_seeded) == ("60", "60", 120, None)
    moved = ("60", "60", 120, "db-init: moved default")
    cases = [
        # (value, default, updated_by) on an account seeded before the change -> after the update
        (("30", "30", None), moved),                                        # the untouched seed
        (("30", "30", "scenario:normal"), moved),                           # Normal Operations' old cap
        (("120", "30", "scenario:equipment_down"), ("120", "60", 120, "scenario:equipment_down")),
        (("45", "30", None), ("45", "60", 120, None)),                      # a value someone set
        (("30", "30", "scenario:demand_surge"), ("30", "60", 120, "scenario:demand_surge")),
    ]
    for before, after in cases:
        _set_cap_row(pg_seeded, *before)
        handler.seed_catalogue_upsert(pg_seeded)
        assert _cap_row(pg_seeded) == after, before
        handler.seed_catalogue_upsert(pg_seeded)                # a re-run changes nothing
        assert _cap_row(pg_seeded) == after, before


# --------------------------------------------------------------------------
# Downtime length to two decimals (2026-10-17): the simulator records random
# stops to the second, and mes_downtime_events.duration_minutes carries two
# decimal places on a fresh account (both DDL homes) and on an existing one
# after its seed-only update, with the views that read it recreated as they
# were.
# --------------------------------------------------------------------------
_DURATION_EXPRESSION = ("GENERATED ALWAYS AS ( CASE WHEN end_time IS NOT NULL "
                        "THEN EXTRACT(EPOCH FROM (end_time - start_time)) / 60 ELSE NULL END ) STORED")


def _duration_column_ddl(sql_text: str) -> str:
    match = re.search(r"\bduration_minutes\s+(.*?STORED)", sql_text, re.S)
    assert match, "no duration_minutes column"
    return " ".join(match.group(1).replace("(", "( ").replace(")", " )").split())


def test_downtime_length_is_two_decimal_in_both_ddl_homes(handler):
    schema = _HANDLER_PATH.parents[1] / "erp_simulation" / "schema" / "08_mes_tracking.sql"
    expression = " ".join(_DURATION_EXPRESSION.replace("(", "( ").replace(")", " )").split())
    for home in (handler.get_schema_sql_part3(), schema.read_text()):
        assert _duration_column_ddl(home) == f"NUMERIC( 8,2 ) {expression}"
    assert handler.DOWNTIME_DURATION_TYPE == "numeric(8,2)"
    assert f"= '{handler.DOWNTIME_DURATION_TYPE}' THEN" in " ".join(handler.MIGRATE_DOWNTIME_DURATION_SQL.split())


def test_seed_only_update_moves_the_downtime_length_type_before_rebuilding_the_views(handler, lifecycle):
    assert handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)["Status"] == "SUCCESS"
    calls = [sql for sql, _p in lifecycle["conn"].cursor_obj.calls]
    migration = " ".join(handler.MIGRATE_DOWNTIME_DURATION_SQL.split())
    assert calls.count(migration) == 1
    views = [i for i, sql in enumerate(calls) if "CREATE OR REPLACE VIEW" in sql]
    assert views and calls.index(migration) < min(views)
    assert lifecycle["conn"].commits == 2


def _duration_column(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.attnum, format_type(a.atttypid, a.atttypmod), a.attgenerated::text,
                   pg_get_expr(d.adbin, d.adrelid)
            FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = 'public.mes_downtime_events'::regclass AND a.attname = 'duration_minutes'
        """)
        return cur.fetchone()


def _views_over_downtime(conn) -> dict:
    """``schema.view`` -> (definition, duration_minutes type or None, comment,
    reloptions) of every view in the database, for the ones that read
    mes_downtime_events directly or through another view."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.nspname || '.' || c.relname, pg_get_viewdef(c.oid),
                   (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                     WHERE a.attrelid = c.oid AND a.attname = 'duration_minutes'),
                   obj_description(c.oid, 'pg_class'), c.reloptions
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'v' AND pg_get_viewdef(c.oid) LIKE '%downtime_events%'
        """)
        return {name: rest for name, *rest in cur.fetchall()}


def _downtime_rows(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT downtime_reason, start_time, end_time, duration_minutes::text "
                    "FROM mes_downtime_events ORDER BY downtime_reason")
        return {reason: rest for reason, *rest in cur.fetchall()}


def _as_account_seeded_before_the_change(handler, conn) -> list:
    """Give a fresh database the shape an account initialised before the
    change has: an INTEGER duration_minutes, the db-init views over it. The
    site views have to go first: PostgreSQL will not retype a column a view
    reads. Returns the view schemas that had a downtime_events view."""
    with conn.cursor() as cur:
        cur.execute("SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'downtime_events' AND c.relkind = 'v' ORDER BY 1")
        schemas = [r[0] for r in cur.fetchall()]
        for schema in schemas:
            cur.execute(stmt("DROP VIEW {rel}", rel=(schema, "downtime_events")))
        cur.execute("ALTER TABLE mes_downtime_events ALTER COLUMN duration_minutes TYPE INTEGER")
        cur.execute(handler.get_views_sql())
        cur.execute(handler.get_machining_views_sql())
    conn.commit()
    return schemas


@pytest.fixture
def pg_reader_role(pg_seeded):
    """A throwaway login-less role (roles are cluster-wide): dropped again,
    with every privilege it holds in the test database, afterwards."""
    role = "t3r_" + uuid.uuid4().hex[:8]
    with pg_seeded.cursor() as cur:
        cur.execute(stmt("CREATE ROLE {role}", role=role))
    pg_seeded.commit()
    try:
        yield role
    finally:
        pg_seeded.rollback()
        with pg_seeded.cursor() as cur:
            cur.execute(stmt("DROP OWNED BY {role}", role=role))
            cur.execute(stmt("DROP ROLE {role}", role=role))
        pg_seeded.commit()


def test_pg_existing_account_gets_two_decimal_downtime_lengths(handler, pg_seeded, pg_reader_role):
    fresh = _duration_column(pg_seeded)
    assert fresh[1:3] == ("numeric(8,2)", "s")
    schemas = _as_account_seeded_before_the_change(handler, pg_seeded)
    assert {"mes_milwaukee", "mes_detroit", "mes_cedarrapids", "mes_indianapolis"} <= set(schemas)
    assert _duration_column(pg_seeded) == (fresh[0], "integer", "s", fresh[3])
    with pg_seeded.cursor() as cur:
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity)
            SELECT 'RUN-T3-DT', product_id, 10 FROM products ORDER BY sku LIMIT 1;
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                                         planned_quantity)
            SELECT 'WO-T3-DT', run_id, product_id, 'INDIANAPOLIS_MACHINING', 'LINE3', 10
            FROM production_runs WHERE run_number = 'RUN-T3-DT';
        """)
        rows = [
            # (reason, start, end): a whole-minute stop as the old draw wrote it,
            # a repair of 7 min 30 s (a 3-hour story loop's bearing repair, stored
            # as 8 while the column was INTEGER), one written to the second while
            # the column was still INTEGER, and a stop with no end yet.
            ("A-WHOLE", "2026-10-01 08:00:00", "2026-10-01 08:30:00"),
            ("B-HALF", "2026-10-01 09:00:00", "2026-10-01 09:07:30"),
            ("C-SECONDS", "2026-10-01 10:00:00", "2026-10-01 10:37:25"),
            ("D-OPEN", "2026-10-01 11:00:00", None),
        ]
        for reason, start, end in rows:
            cur.execute("INSERT INTO mes_downtime_events (work_order_id, line_id, downtime_category, "
                        "downtime_reason, start_time, end_time) SELECT work_order_id, 'LINE3', 'MECHANICAL', "
                        "%s, %s, %s FROM mes_work_orders WHERE work_order_number = 'WO-T3-DT'",
                        (reason, start, end))
        # A view over a site view, with options and comments, and grants on both.
        cur.execute("CREATE VIEW mes_indianapolis.long_stops WITH (security_barrier=true) AS "
                    "SELECT downtime_id, duration_minutes FROM mes_indianapolis.downtime_events "
                    "WHERE duration_minutes > 20")
        cur.execute("COMMENT ON VIEW mes_indianapolis.long_stops IS 'Stops over twenty minutes'")
        cur.execute("COMMENT ON COLUMN mes_indianapolis.long_stops.duration_minutes IS 'Length in minutes'")
        for view in ("downtime_events", "long_stops"):
            cur.execute(stmt("GRANT SELECT ON {rel} TO {role}", rel=("mes_indianapolis", view),
                             role=pg_reader_role))
    pg_seeded.commit()
    before_rows, before_views = _downtime_rows(pg_seeded), _views_over_downtime(pg_seeded)
    assert [before_rows[r][2] for r in ("A-WHOLE", "B-HALF", "C-SECONDS", "D-OPEN")] == ["30", "8", "37", None]
    assert {v[1] for v in before_views.values()} == {"integer"}

    handler.seed_catalogue_upsert(pg_seeded)

    assert _duration_column(pg_seeded) == fresh
    after_rows = _downtime_rows(pg_seeded)
    # Same timestamps; each length now its exact minutes to two places.
    assert {r: v[:2] for r, v in after_rows.items()} == {r: v[:2] for r, v in before_rows.items()}
    assert [after_rows[r][2] for r in ("A-WHOLE", "B-HALF", "C-SECONDS", "D-OPEN")] == [
        "30.00", "7.50", "37.42", None]
    after_views = _views_over_downtime(pg_seeded)
    assert set(after_views) == set(before_views) and len(after_views) >= 5
    for name, (definition, column_type, comment, options) in after_views.items():
        assert column_type == "numeric(8,2)", name
        assert (comment, options) == (before_views[name][2], before_views[name][3]), name
        if name.endswith(".downtime_events"):               # the db-init views: SELECT d.*
            assert definition == before_views[name][0], name
    assert tuple(after_views["mes_indianapolis.long_stops"][2:]) == ("Stops over twenty minutes",
                                                                    ["security_barrier=true"])
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT col_description('mes_indianapolis.long_stops'::regclass, 2)")
        assert cur.fetchone()[0] == "Length in minutes"
        for view in ("downtime_events", "long_stops"):
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')",
                        (pg_reader_role, f"mes_indianapolis.{view}"))
            assert cur.fetchone()[0] is True, view
        cur.execute("SELECT duration_minutes::text FROM mes_indianapolis.long_stops ORDER BY 1")
        assert [r[0] for r in cur.fetchall()] == ["30.00", "37.42"]
        cur.execute("SELECT 'mes_indianapolis.long_stops'::regclass::oid")
        oid = cur.fetchone()[0]
    pg_seeded.commit()

    handler.seed_catalogue_upsert(pg_seeded)                    # a re-run changes nothing
    assert _duration_column(pg_seeded) == fresh and _downtime_rows(pg_seeded) == after_rows
    assert _views_over_downtime(pg_seeded) == after_views
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT 'mes_indianapolis.long_stops'::regclass::oid")
        assert cur.fetchone()[0] == oid                         # not dropped and recreated again


def test_pg_downtime_length_migration_refuses_a_dependent_it_cannot_recreate(handler, pg_seeded):
    """A materialized view over the column: the migration stops with an
    error naming it, before anything has changed."""
    _as_account_seeded_before_the_change(handler, pg_seeded)
    with pg_seeded.cursor() as cur:
        cur.execute("CREATE MATERIALIZED VIEW public.t3_downtime_totals AS "
                    "SELECT line_id, sum(duration_minutes) AS minutes FROM mes_downtime_events GROUP BY line_id")
    pg_seeded.commit()
    from erp_simulation import pgdriver
    with pytest.raises(pgdriver.Error, match="t3_downtime_totals"):
        with pg_seeded.cursor() as cur:
            handler.migrate_downtime_duration_type(cur)
    pg_seeded.rollback()
    assert _duration_column(pg_seeded)[1] == "integer"
    assert set(_views_over_downtime(pg_seeded)) >= {"mes_indianapolis.downtime_events"}


def test_pg_seed_only_update_fails_and_changes_nothing_when_the_migration_refuses(handler, pg_seeded, monkeypatch):
    """An existing account whose column is read by a materialized view: the
    seed-only Update RAISES to the Provider (so CloudFormation fails and rolls
    back the update), its connection is closed, and the first transaction
    commits nothing: the column stays INTEGER and the old cap stays put."""
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    _as_account_seeded_before_the_change(handler, pg_seeded)
    _set_cap_row(pg_seeded, "30", "30", None)
    with pg_seeded.cursor() as cur:
        cur.execute("CREATE MATERIALIZED VIEW public.t3_downtime_totals AS "
                    "SELECT line_id, sum(duration_minutes) AS minutes FROM mes_downtime_events GROUP BY line_id")
    pg_seeded.commit()
    opened = []
    db_name = pg_seeded.get_dsn_parameters()["dbname"]

    def connect(_credentials):
        opened.append(pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": db_name})))
        return opened[-1]
    monkeypatch.setattr(handler, "get_db_credentials", lambda: {"host": "h"})
    monkeypatch.setattr(handler, "get_connection", connect)

    with pytest.raises(pgdriver.Error, match="t3_downtime_totals.*is a materialized view"):
        handler.handler(_update(_PROPS, {**_PROPS, "SeedVersion": "b"}), None)

    assert len(opened) == 1 and opened[0].closed
    assert _duration_column(pg_seeded)[1] == "integer"
    assert _cap_row(pg_seeded) == ("30", "30", 120, None)


def test_pg_rollback_of_a_refused_seed_only_update_is_a_no_op_on_the_new_code(handler, pg_seeded, monkeypatch):
    """The deterministic refusal end to end: the forward seed-only Update
    ("a" -> "b") on code "b" fails on a materialized view over the column.
    CloudFormation's rollback Update ("b" -> "a") reaching the same code
    succeeds without opening a connection, and the account is left exactly as
    the failed update left it: INTEGER column, old cap, the view in place."""
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    _as_account_seeded_before_the_change(handler, pg_seeded)
    _set_cap_row(pg_seeded, "30", "30", None)
    with pg_seeded.cursor() as cur:
        cur.execute("CREATE MATERIALIZED VIEW public.t3_downtime_totals AS "
                    "SELECT line_id, sum(duration_minutes) AS minutes FROM mes_downtime_events GROUP BY line_id")
    pg_seeded.commit()
    opened = []
    db_name = pg_seeded.get_dsn_parameters()["dbname"]

    def connect(_credentials):
        opened.append(pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": db_name})))
        return opened[-1]
    monkeypatch.setattr(handler, "get_db_credentials", lambda: {"host": "h"})
    monkeypatch.setattr(handler, "get_connection", connect)
    monkeypatch.setenv(_SEED_VERSION_ENV, "b")

    with pytest.raises(pgdriver.Error, match="t3_downtime_totals"):
        handler.handler(_update({**_PROPS, "SeedVersion": "a"}, {**_PROPS, "SeedVersion": "b"}), None)
    assert len(opened) == 1 and opened[0].closed

    result = handler.handler(_rollback("b", "a"), None)

    assert result["Status"] == "SUCCESS"
    assert len(opened) == 1
    assert _duration_column(pg_seeded)[1] == "integer"
    assert _cap_row(pg_seeded) == ("30", "30", 120, None)
    with pg_seeded.cursor() as cur:
        cur.execute("SELECT to_regclass('public.t3_downtime_totals') IS NOT NULL")
        assert cur.fetchone()[0] is True
    pg_seeded.commit()


# A view's effective access list: (grantor, grantee, privilege, grant option)
# for every entry of its ACL, or of the owner's default one when it has none.
_VIEW_ACLS_SQL = """
    SELECT n.nspname || '.' || c.relname, pg_get_userbyid(c.relowner),
           ARRAY(SELECT pg_get_userbyid(a.grantor) || ' ' ||
                        CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END || ' ' ||
                        a.privilege_type || CASE WHEN a.is_grantable THEN ' grantable' ELSE '' END
                   FROM aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'v' AND pg_get_viewdef(c.oid) LIKE '%downtime_events%'
"""


def _view_acls(conn) -> dict:
    """``schema.view`` -> (owner, effective access list) for every view over
    mes_downtime_events."""
    with conn.cursor() as cur:
        cur.execute(_VIEW_ACLS_SQL)
        return {name: (owner, sorted(acl)) for name, owner, acl in cur.fetchall()}


@pytest.fixture
def pg_roles(pg_seeded):
    """Make throwaway login-less roles on demand (``pg_roles("label")``).
    Roles are cluster-wide: afterwards whatever they own in the test database
    goes back to the connecting user and each role is dropped."""
    made = []

    def make(label):
        role = f"t3_{label}_" + uuid.uuid4().hex[:8]
        with pg_seeded.cursor() as cur:
            cur.execute(stmt("CREATE ROLE {role}", role=role))
        pg_seeded.commit()
        made.append(role)
        return role
    try:
        yield make
    finally:
        pg_seeded.rollback()
        with pg_seeded.cursor() as cur:
            cur.execute("RESET ROLE")
            for role in reversed(made):
                cur.execute(stmt("REASSIGN OWNED BY {role} TO CURRENT_USER", role=role))
                cur.execute(stmt("DROP OWNED BY {role}", role=role))
            for role in reversed(made):
                cur.execute(stmt("DROP ROLE {role}", role=role))
        pg_seeded.commit()


def _hand_over(cur, role, schemas):
    """Make *role* own what Aurora's administrator login owns on a deployed account:
    the downtime table, the view schemas and their downtime views (db-init
    creates all three as the administrator login, which is not a superuser there)."""
    cur.execute(stmt("ALTER TABLE public.mes_downtime_events OWNER TO {role}", role=role))
    cur.execute(stmt("GRANT SELECT ON public.mes_work_orders TO {role}", role=role))
    for schema in schemas:
        cur.execute(stmt("ALTER SCHEMA {schema} OWNER TO {role}", schema=schema, role=role))
        cur.execute(stmt("ALTER VIEW {rel} OWNER TO {role}", rel=(schema, "downtime_events"), role=role))


@pytest.mark.parametrize("connect_as", ["superuser", "administrator login"])
def test_pg_downtime_length_migration_gives_each_view_back_exactly_its_acl(handler, pg_seeded, pg_roles,
                                                                           connect_as):
    """CREATE VIEW applies the creating role's default privileges (here to a
    role and to PUBLIC the views never had, and without one of the owner's
    own); every recreated view still ends with exactly its old effective
    access list: explicit grants, a grant option, an owner's self-revoke, an
    emptied ACL and the owner's default one."""
    schemas = _as_account_seeded_before_the_change(handler, pg_seeded)
    prior, opt, future = pg_roles("prior"), pg_roles("opt"), pg_roles("future")
    admin = pg_roles("admin") if connect_as == "administrator login" else None
    with pg_seeded.cursor() as cur:
        if admin:
            _hand_over(cur, admin, schemas)
        cur.execute("SELECT current_user")
        creator = admin or cur.fetchone()[0]
        ind, det = ("mes_indianapolis", "downtime_events"), ("mes_detroit", "downtime_events")
        cur.execute(stmt("GRANT SELECT ON {rel} TO {role}", rel=ind, role=prior))
        cur.execute(stmt("GRANT SELECT, REFERENCES ON {rel} TO {role} WITH GRANT OPTION", rel=ind, role=opt))
        cur.execute(stmt("REVOKE TRUNCATE ON {rel} FROM {role}", rel=ind, role=creator))
        cur.execute(stmt("REVOKE ALL ON {rel} FROM {role}", rel=det, role=creator))
        # Milwaukee and Cedar Rapids keep no ACL of their own (the owner's default).
        cur.execute(stmt("ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA {schema} "
                         "GRANT SELECT ON TABLES TO {future}", role=creator, schema="mes_indianapolis",
                         future=future))
        cur.execute(stmt("ALTER DEFAULT PRIVILEGES FOR ROLE {role} GRANT INSERT, SELECT ON TABLES TO PUBLIC",
                         role=creator))
        cur.execute(stmt("ALTER DEFAULT PRIVILEGES FOR ROLE {role} REVOKE DELETE ON TABLES FROM {role}",
                         role=creator))
    pg_seeded.commit()
    before = _view_acls(pg_seeded)
    assert {owner for owner, _acl in before.values()} == {creator}
    assert not [e for _o, acl in before.values() for e in acl if future in e or " PUBLIC " in e]

    with pg_seeded.cursor() as cur:
        if admin:
            cur.execute(stmt("SET ROLE {role}", role=admin))
        handler.migrate_downtime_duration_type(cur)
        cur.execute("RESET ROLE")
    pg_seeded.commit()

    assert _duration_column(pg_seeded)[1] == "numeric(8,2)"
    assert _view_acls(pg_seeded) == before
    with pg_seeded.cursor() as cur:
        for role in (future, "public"):
            cur.execute("SELECT has_table_privilege(%s, 'mes_indianapolis.downtime_events', 'SELECT')", (role,))
            assert cur.fetchone()[0] is False, role


def test_pg_downtime_length_migration_refuses_a_view_whose_owner_it_cannot_give_back(handler, pg_seeded,
                                                                                     pg_roles):
    """Connected as an administrator login that is not a superuser (Aurora's), a view
    over the column owned by a role the administrator login is not a member of: it
    could drop the view (it owns the schema) but not make that role its owner
    again, so the migration refuses, naming the view and both roles, before
    anything has changed."""
    from erp_simulation import pgdriver
    schemas = _as_account_seeded_before_the_change(handler, pg_seeded)
    admin, other = pg_roles("admin"), pg_roles("other")
    with pg_seeded.cursor() as cur:
        _hand_over(cur, admin, schemas)
        cur.execute(stmt("GRANT USAGE, CREATE ON SCHEMA {schema} TO {role}", schema="mes_indianapolis", role=other))
        cur.execute(stmt("GRANT SELECT ON {rel} TO {role}", rel=("mes_indianapolis", "downtime_events"), role=other))
        cur.execute("CREATE VIEW mes_indianapolis.t3_others_stops AS "
                    "SELECT downtime_id, duration_minutes FROM mes_indianapolis.downtime_events")
        cur.execute(stmt("ALTER VIEW {rel} OWNER TO {role}", rel=("mes_indianapolis", "t3_others_stops"), role=other))
    pg_seeded.commit()
    before = _view_acls(pg_seeded)

    with pytest.raises(pgdriver.Error,
                       match=rf"t3_others_stops, which .* it is owned by {other}, which {admin} cannot"):
        with pg_seeded.cursor() as cur:
            cur.execute(stmt("SET ROLE {role}", role=admin))
            handler.migrate_downtime_duration_type(cur)
    pg_seeded.rollback()

    assert _duration_column(pg_seeded)[1] == "integer"
    assert _view_acls(pg_seeded) == before


def test_pg_downtime_length_migration_gives_another_role_its_view_back(handler, pg_seeded, pg_roles):
    """Connected as a non-superuser administrator login that IS a member of the
    owning role: the view goes back to that owner with exactly its access
    list, including the grant to the administrator login itself (which a plain
    ownership change would fold into the owner's entry)."""
    schemas = _as_account_seeded_before_the_change(handler, pg_seeded)
    admin, other = pg_roles("admin"), pg_roles("other")
    rel = ("mes_indianapolis", "t3_others_stops")
    with pg_seeded.cursor() as cur:
        _hand_over(cur, admin, schemas)
        cur.execute(stmt("GRANT {other} TO {admin}", other=other, admin=admin))
        cur.execute(stmt("GRANT USAGE, CREATE ON SCHEMA {schema} TO {role}", schema="mes_indianapolis", role=other))
        cur.execute(stmt("GRANT SELECT ON {rel} TO {role}", rel=("mes_indianapolis", "downtime_events"), role=other))
        cur.execute("CREATE VIEW mes_indianapolis.t3_others_stops AS "
                    "SELECT downtime_id, duration_minutes FROM mes_indianapolis.downtime_events")
        cur.execute(stmt("ALTER VIEW {rel} OWNER TO {role}", rel=rel, role=other))
        cur.execute(stmt("GRANT SELECT ON {rel} TO {role}", rel=rel, role=admin))
    pg_seeded.commit()
    before = _view_acls(pg_seeded)
    assert before["mes_indianapolis.t3_others_stops"] == (other, sorted([
        *(f"{other} {other} {p}" for p in
          ("DELETE", "INSERT", "REFERENCES", "SELECT", "TRIGGER", "TRUNCATE", "UPDATE")),
        f"{other} {admin} SELECT"]))

    with pg_seeded.cursor() as cur:
        cur.execute(stmt("SET ROLE {role}", role=admin))
        handler.migrate_downtime_duration_type(cur)
        cur.execute("RESET ROLE")
    pg_seeded.commit()

    assert _duration_column(pg_seeded)[1] == "numeric(8,2)"
    assert _view_acls(pg_seeded) == before


# --- Config rows added after an account was seeded ---------------------------
# An in-place deploy runs only the additive upsert, which never re-ran
# seed_simulation_config, so a key added later (the delivery spread) did not
# exist until a scenario wrote it -- and scenarios.apply_scenario files a new
# key under category 'scenario' with no description or bounds, which the
# dashboard's Advanced panel never lists (ADT-DEMO, 2026-10-28).

_SPREAD_KEY = "procurement_delivery_variability_minutes"


def test_added_config_keys_are_seed_rows(handler):
    seeded = {row[0] for row in handler.SIMULATION_CONFIG_ROWS}
    assert _SPREAD_KEY in handler.ADDED_CONFIG_KEYS
    assert set(handler.ADDED_CONFIG_KEYS) <= seeded


def test_add_new_config_rows_inserts_and_repairs_scenario_placeholders_only(handler):
    cursor = _RecordingCursor()
    handler.add_new_config_rows(cursor)
    inserts = [(sql, p) for sql, p in cursor.calls if sql.startswith("INSERT INTO simulation_config")]
    assert [p[0] for _, p in inserts] == list(handler.ADDED_CONFIG_KEYS)
    rows = {row[0]: row for row in handler.SIMULATION_CONFIG_ROWS}
    for sql, params in inserts:
        assert tuple(params) == rows[params[0]]
        update = sql.split("DO UPDATE", 1)[1]
        # The live value is never touched; only a scenario placeholder is repaired.
        assert "config_value" not in update
        assert update.endswith("WHERE simulation_config.category = 'scenario'")


def test_seed_catalogue_upsert_adds_new_config_rows(handler, monkeypatch):
    called = []
    for name in ("seed_machining_site", "seed_mes_equipment", "_seed_anchor_suppliers",
                 "migrate_moved_config_defaults", "migrate_downtime_duration_type",
                 "get_views_sql", "get_machining_views_sql"):
        monkeypatch.setattr(handler, name, lambda *a, _n=name, **k: called.append(_n) or "")
    monkeypatch.setattr(handler, "add_new_config_rows", lambda c: called.append("add_new_config_rows"))

    class _Conn:
        def cursor(self):
            class _C(_RecordingCursor):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return _C()

        def commit(self):
            pass

    handler.seed_catalogue_upsert(_Conn())
    assert "add_new_config_rows" in called


def _spread_row(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT config_value, data_type, category, description, min_value, max_value, "
                    "default_value FROM simulation_config WHERE config_key = %s", (_SPREAD_KEY,))
        return cur.fetchone()


def test_pg_existing_account_gets_the_spread_row_and_keeps_a_scenario_value(handler, pg_seeded):
    # An account seeded before the key existed, where Normal then wrote it.
    with pg_seeded.cursor() as cur:
        cur.execute("DELETE FROM simulation_config WHERE config_key = %s", (_SPREAD_KEY,))
        cur.execute("INSERT INTO simulation_config (config_key, config_value, data_type, category, "
                    "default_value, updated_by) VALUES (%s, '120', 'float', 'scenario', '120', "
                    "'scenario:normal')", (_SPREAD_KEY,))
    pg_seeded.commit()
    handler.seed_catalogue_upsert(pg_seeded)
    value, dtype, category, desc, minv, maxv, default = _spread_row(pg_seeded)
    assert (value, dtype, category, default) == ("120", "float", "procurement", "0")
    assert desc and float(minv) == 0.0 and float(maxv) == 1440.0
    # An account that never had it gets the seed row at the default.
    with pg_seeded.cursor() as cur:
        cur.execute("DELETE FROM simulation_config WHERE config_key = %s", (_SPREAD_KEY,))
    pg_seeded.commit()
    handler.seed_catalogue_upsert(pg_seeded)
    assert _spread_row(pg_seeded)[0] == "0"
    # A steward's own row (not a scenario placeholder) is left alone.
    with pg_seeded.cursor() as cur:
        cur.execute("UPDATE simulation_config SET config_value = '30', description = 'mine' "
                    "WHERE config_key = %s", (_SPREAD_KEY,))
    pg_seeded.commit()
    handler.seed_catalogue_upsert(pg_seeded)
    row = _spread_row(pg_seeded)
    assert (row[0], row[3]) == ("30", "mine")
