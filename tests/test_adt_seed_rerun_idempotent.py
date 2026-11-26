# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The frozen seed (``lambda-adt-seed/*.sql``) re-runs on an existing schema.

``AdtSeedResource`` re-runs every seed file on a stack Update whenever the SQL
changes (``stacks/adt_seed_data_stack.py::adt_seed_version``), against a
database that already holds the previous seed. So every file must be
re-runnable WITHOUT data loss:

* DDL is ``CREATE ... IF NOT EXISTS`` (a plain ``CREATE TABLE`` aborts the
  whole file on a re-run, before any new row lands);
* no ``DROP TABLE`` (it destroys whatever the steward's systems added since);
* every INSERT is idempotent: ``ON CONFLICT (<key>) DO NOTHING`` where the
  table has a primary/unique key, else ``WHERE NOT EXISTS`` on the natural key
  (or, for fact rows without one, on the full inserted row).

Offline checks parse the SQL text; the PostgreSQL checks
(``ERP_SIM_TEST_PG_DSN``, throwaway database; skipped otherwise) apply every
file twice exactly like the seed Lambda (each file whole, autocommit) and
prove the second run changes nothing, and that an account seeded BEFORE the
Hydraulic Manifold 5520 rows gets them on the re-run with every other row
(the B-2847 defect chain, the planted-mapping asset rows) untouched.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import pytest

from _pg_sql import pgdriver, stmt

_REPO = Path(__file__).resolve().parents[1]
_SEED_DIR = _REPO / "lambda-adt-seed"
_SEED_FILES = sorted(_SEED_DIR.glob("*.sql"))
_OVERRIDES = _REPO / "agentic_digital_thread" / "agents" / "demo" / "fixtures" / "workshop_mapping_overrides.json"

# One statement = from ``INSERT INTO`` to the first ``;`` that ends a line
# (quoted descriptions may carry mid-line semicolons).
_INSERT = re.compile(r"\bINSERT INTO\b.*?;[ \t]*$", re.S | re.M)
_IDEMPOTENT = re.compile(r"\bON CONFLICT\b|\bNOT EXISTS\b")


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    """The SQL with ``--`` comments removed (comments may say anything)."""
    return re.sub(r"--[^\n]*", "", sql)


def test_seed_files_exist():
    assert len(_SEED_FILES) >= 10, _SEED_FILES


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_every_create_is_if_not_exists(path):
    code = _code(_sql(path))
    plain = re.findall(r"(?i)\bCREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|SCHEMA)\s+(?!IF\s+NOT\s+EXISTS)(\S+)", code)
    assert plain == [], f"{path.name}: re-run aborts on {plain}"


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_no_seed_file_drops_a_table(path):
    assert not re.search(r"(?i)\bDROP\s+TABLE\b", _code(_sql(path))), path.name


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_every_insert_is_idempotent(path):
    loose = [stmt.splitlines()[0][:120] for stmt in _INSERT.findall(_code(_sql(path)))
             if not _IDEMPOTENT.search(stmt)]
    assert loose == [], f"{path.name}: a re-run duplicates rows for {loose}"


def test_idempotence_check_bites():
    """The INSERT check flags a bare INSERT and accepts both guarded forms."""
    bare = "INSERT INTO t (a) VALUES\n(1),\n(2);\n"
    guarded = ("INSERT INTO t (a) VALUES\n(1)\nON CONFLICT (a) DO NOTHING;\n"
               "INSERT INTO u (a) SELECT 1\nWHERE NOT EXISTS (SELECT 1 FROM u);\n")
    assert [s for s in _INSERT.findall(bare) if not _IDEMPOTENT.search(s)]
    assert not [s for s in _INSERT.findall(guarded) if not _IDEMPOTENT.search(s)]


_SITE_CMMS = _SEED_DIR / "011_cmms_indianapolis.sql"


def _insert_statement(sql: str, table: str) -> str:
    (stmt,) = [s for s in _INSERT.findall(_code(sql)) if re.match(rf"INSERT INTO {table}\b", s)]
    return stmt


def test_site_cmms_rerun_gives_existing_rows_the_seeds_names():
    """The site CMMS rows name people (``work_orders.reported_by``,
    ``work_order_log.author``). A re-run gives a row the account already holds
    the seed's name: the work order upsert sets ``reported_by`` and nothing
    else (the live story owns the status, dates and descriptions of these
    rows), and a log entry is matched without its author, so a changed name
    is not written as a second entry."""
    sql = _sql(_SITE_CMMS)
    orders = _insert_statement(sql, "work_orders")
    update = re.search(r"\bON CONFLICT \(wonum\) DO UPDATE SET (.*?)\s+WHERE\b", orders, re.S)
    assert update and update.group(1) == "reported_by = EXCLUDED.reported_by", orders[-300:]
    log = _insert_statement(sql, "work_order_log")
    assert "NOT EXISTS" in log
    assert "author" not in log.split("NOT EXISTS", 1)[1], log[-400:]


# --------------------------------------------------------------------------
# Real PostgreSQL: ERP_SIM_TEST_PG_DSN (throwaway database); skipped otherwise.
# --------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")

#: The rows the workshop story depends on, as full rows (serial ids and the
#: historian's random values included, so a re-insert or re-randomize shows).
_PINNED_QUERIES = {
    "b2847_run": "SELECT * FROM mes_line3.production_runs WHERE batch_number = 'B-2847'",
    "b2847_checks": "SELECT * FROM mes_line3.quality_checks WHERE batch_number = 'B-2847' ORDER BY check_id",
    "plm_7742": "SELECT * FROM plm_engineering.products WHERE item_id LIKE 'PN-7742-%' ORDER BY item_id",
    "cmx441_registry": "SELECT * FROM iot_historian.equipment_registry WHERE equipment_id = '5-Axis-Milling-CMX-441'",
    "cmx441_vibration": ("SELECT * FROM iot_historian.sensor_readings WHERE equipment_id = '5-Axis-Milling-CMX-441' "
                         "AND measurement_type = 'vibration' ORDER BY id"),
    "cmms_orders": "SELECT * FROM cmms_indianapolis.work_orders WHERE wonum IN ('1182', '1195') ORDER BY wonum",
    "cmms_pm": "SELECT * FROM cmms_indianapolis.pm_schedules WHERE pm_num = 'PM-1001-0007'",
    "cmms_log": "SELECT * FROM cmms_indianapolis.work_order_log ORDER BY log_id",
}

#: The Hydraulic Manifold 5520 rows (added after accounts were first seeded).
_MANIFOLD_ROWS = {
    "mes_line3.production_runs": "product_code = '5520'",
    "plm_engineering.specifications": "item_id LIKE 'PN-5520-%'",
    "plm_engineering.bom_items": "parent_item LIKE 'PN-5520-%'",
    "plm_engineering.products": "item_id LIKE 'PN-5520-%'",
}


def _planted_mapping_query():
    """The cmms-ind table/column the module-4 planted wrong mapping targets,
    read from the fixture (so this pin follows the fixture)."""
    (row,) = json.loads(_OVERRIDES.read_text(encoding="utf-8"))["cmms-ind"]
    table, column = row["field"].split(".")
    assert table == row["table"]
    return stmt("SELECT {column}, * FROM cmms_indianapolis.{table} ORDER BY {column}", column=column, table=table)


@pytest.fixture
def make_seed_db():
    """A factory of throwaway databases (autocommit, as the seed Lambda);
    every database it made is dropped at teardown."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    driver = pgdriver()
    admin = driver.connect(_DSN)
    admin.autocommit = True
    made = []

    def make():
        name = "seedrerun_" + uuid.uuid4().hex[:8]
        with admin.cursor() as cur:
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
        conn = driver.connect(driver.make_dsn(**{**driver.parse_dsn(_DSN), "dbname": name}))
        conn.autocommit = True  # as lambda-adt-seed/handler.py
        made.append((name, conn))
        return conn

    try:
        yield make
    finally:
        for name, conn in made:
            conn.close()
            with admin.cursor() as cur:
                cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=name))
        admin.close()


@pytest.fixture
def seed_db(make_seed_db):
    return make_seed_db()


def _apply_all(conn) -> list:
    """Run every seed file whole, in order, like the seed Lambda; return the
    (file, error) pairs the Lambda would only have printed."""
    errors = []
    with conn.cursor() as cur:
        for path in _SEED_FILES:
            try:
                cur.execute(_sql(path))
            except Exception as exc:  # noqa: BLE001 - collected and asserted empty
                errors.append((path.name, str(exc).splitlines()[0]))
    return errors


def _counts(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' "
                    "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2")
        tables = cur.fetchall()
        counts = {}
        for schema, table in tables:
            cur.execute(stmt("SELECT count(*) FROM {table}", table=(schema, table)))
            counts[f"{schema}.{table}"] = cur.fetchone()[0]
    return counts


def _snapshot(conn) -> dict:
    queries = {**_PINNED_QUERIES, "planted_mapping_assets": _planted_mapping_query()}
    with conn.cursor() as cur:
        rows = {}
        for key, sql in queries.items():
            cur.execute(sql)
            rows[key] = cur.fetchall()
            assert rows[key], f"pinned fixture {key} is empty"
    return rows


def test_pg_every_seed_file_applies_twice_with_nothing_changed(seed_db):
    assert _apply_all(seed_db) == []
    counts, pinned = _counts(seed_db), _snapshot(seed_db)
    assert counts["mes_line3.production_runs"] == 159
    assert _apply_all(seed_db) == [], "a re-run must not abort any file"
    assert _counts(seed_db) == counts
    assert _snapshot(seed_db) == pinned


def test_pg_existing_account_gets_the_manifold_rows_and_keeps_the_rest(seed_db):
    """An account seeded before the 5520 rows existed: the re-run inserts
    exactly them, keeps every other row as it was (ids included), and keeps a
    row a source system added after the seed (the old 005 DROP lost it)."""
    sql = pgdriver().sql

    assert _apply_all(seed_db) == []
    expected = _counts(seed_db)
    with seed_db.cursor() as cur:
        for table, where in _MANIFOLD_ROWS.items():  # children before products
            cur.execute(stmt("DELETE FROM {table} WHERE {where}", table=tuple(table.split(".")), where=sql.SQL(where)))
        cur.execute("INSERT INTO plm_engineering.products VALUES ('PN-STEWARD-1', 'Added after seed', "
                    "NULL, 'Part', 'A', 'In Work', 'Eng', NULL)")
    pinned = _snapshot(seed_db)
    assert _apply_all(seed_db) == []
    counts = _counts(seed_db)
    assert counts == {**expected, "plm_engineering.products": expected["plm_engineering.products"] + 1}
    assert _snapshot(seed_db) == pinned
    with seed_db.cursor() as cur:
        cur.execute("SELECT batch_number FROM mes_line3.production_runs WHERE product_code = '5520' "
                    "ORDER BY batch_number")
        assert [r[0] for r in cur.fetchall()] == [f"B-{n}" for n in range(2951, 2959)]
        cur.execute("SELECT count(*) FROM plm_engineering.products WHERE item_id = 'PN-STEWARD-1'")
        assert cur.fetchone()[0] == 1


#: Every site CMMS table, as full rows (serial ids included).
_SITE_CMMS_STATE = {
    "assets": "SELECT * FROM cmms_indianapolis.assets ORDER BY asset_num",
    "pm_schedules": "SELECT * FROM cmms_indianapolis.pm_schedules ORDER BY pm_num",
    "work_orders": "SELECT * FROM cmms_indianapolis.work_orders ORDER BY wonum",
    "work_order_log": "SELECT * FROM cmms_indianapolis.work_order_log ORDER BY log_id",
}
#: An account seeded by an earlier seed whose rows named other people: the
#: same rows, every name column different.
_EARLIER_NAMES = (
    "UPDATE cmms_indianapolis.work_orders SET reported_by = 'Earlier ' || reported_by",
    "UPDATE cmms_indianapolis.work_order_log SET author = 'Earlier ' || author",
)


def _site_cmms_state(conn) -> dict:
    with conn.cursor() as cur:
        state = {}
        for key, sql in _SITE_CMMS_STATE.items():
            cur.execute(sql)
            state[key] = cur.fetchall()
    return state


def _seeded_under_earlier_names(conn) -> None:
    with conn.cursor() as cur:
        for sql in _EARLIER_NAMES:
            cur.execute(sql)
            assert cur.rowcount > 0, sql


def _earlier_names_left(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM cmms_indianapolis.work_orders WHERE reported_by LIKE 'Earlier %') "
                    "+ (SELECT count(*) FROM cmms_indianapolis.work_order_log WHERE author LIKE 'Earlier %')")
        return cur.fetchone()[0]


def _duplicate_log_entries(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT wonum, log_date, log_type, entry, count(*) FROM cmms_indianapolis.work_order_log "
                    "GROUP BY 1, 2, 3, 4 HAVING count(*) > 1")
        return cur.fetchall()


def test_pg_rerun_gives_an_earlier_seeds_rows_the_current_names(seed_db):
    """An account that holds the site CMMS rows under other names converges on
    the re-run: every row the seed wrote carries the seed's names again, no
    log entry is written twice, and the account ends identical to a fresh
    seed (ids included)."""
    assert _apply_all(seed_db) == []
    fresh_counts, fresh = _counts(seed_db), _site_cmms_state(seed_db)
    _seeded_under_earlier_names(seed_db)
    assert _apply_all(seed_db) == []
    assert _earlier_names_left(seed_db) == 0
    assert _duplicate_log_entries(seed_db) == []
    assert _counts(seed_db) == fresh_counts
    assert _site_cmms_state(seed_db) == fresh


def test_pg_rerun_changes_only_the_names_of_rows_the_live_story_moved(make_seed_db):
    """The live story rewrites seed rows in place (status, dates, descriptions)
    and adds its own. On an account seeded under other names and then moved
    onto the live story, the re-run changes only the names: it ends identical
    to the live story written over a fresh seed, which a re-run leaves alone."""
    from erp_simulation import condition_monitor as cm
    from erp_simulation import story_history as sh
    plan = sh.plan(float(cm.snap_anchor(1791000000, "down")))
    expected, converged = make_seed_db(), make_seed_db()
    for conn in (expected, converged):
        assert _apply_all(conn) == []
    _seeded_under_earlier_names(converged)
    for conn in (expected, converged):
        with conn.cursor() as cur:
            sh._write_seed(cur, plan)
    live = _site_cmms_state(expected)
    assert dict((r[0], r[4]) for r in live["work_orders"])["1182"] == "CAN", "the live story moved 1182"
    assert _apply_all(expected) == []
    assert _site_cmms_state(expected) == live
    assert _apply_all(converged) == []
    assert _earlier_names_left(converged) == 0
    assert _duplicate_log_entries(converged) == []
    assert _site_cmms_state(converged) == live


#: An account seeded before the work-order log insert had a duplicate guard
#: (before 2026-09-30, a plain INSERT) holds each seeded entry twice: the seed
#: ran again and wrote every entry a second time. Both copies carry the names
#: the account was seeded with.
_SECOND_COPY = (
    "INSERT INTO cmms_indianapolis.work_order_log (wonum, log_date, author, log_type, entry) "
    "SELECT wonum, log_date, author, log_type, entry FROM cmms_indianapolis.work_order_log "
    "WHERE wonum IN ('1182', '1187', '1195') ORDER BY log_id"
)
#: Rows the seed never wrote: an entry a source system added to a seeded work
#: order, and two identical entries on another work order. A rerun keeps all.
_NOT_SEEDED = (
    "INSERT INTO cmms_indianapolis.work_order_log (wonum, log_date, author, log_type, entry) VALUES "
    "('1182', '2026-03-01 08:00', 'Maintenance planner', 'UPDATE', 'Added after the seed.'), "
    "('1201', '2026-03-02 09:00', 'supervisor', 'UPDATE', 'Logged twice by its source.'), "
    "('1201', '2026-03-02 09:00', 'supervisor', 'UPDATE', 'Logged twice by its source.')"
)


def _log_rows(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(_SITE_CMMS_STATE["work_order_log"])
        return cur.fetchall()


#: The engine block's gasket line on the PLM bill of materials, and the vendors
#: the seed gives it.
_GASKET_LINE = ("SELECT * FROM plm_engineering.bom_items WHERE parent_item = 'PN-ENGBLK-V8-B' "
                "AND child_item = 'PN-ENGBLK-GASKET-REV-A' AND find_number = 40 ORDER BY bom_id")
_GASKET_VENDORS = "AnyCompany Gaskets,Example Corp Gaskets"


def test_pg_rerun_gives_the_gasket_line_the_seeds_vendors(seed_db):
    """An account seeded when the gasket line's second vendor had another name
    holds that line once, under the earlier name. The re-run gives the line
    the seed's vendors in place (its id kept) and writes no second line; a line
    that differs from the seed's in any other column keeps its vendors."""
    assert _apply_all(seed_db) == []
    fresh_counts = _counts(seed_db)
    with seed_db.cursor() as cur:
        cur.execute(_GASKET_LINE)
        fresh = cur.fetchall()
        assert [row[7] for row in fresh] == [_GASKET_VENDORS]
        cur.execute("UPDATE plm_engineering.bom_items SET approved_vendors = 'AnyCompany Gaskets,Earlier Vendor' "
                    "WHERE bom_id = %s", (fresh[0][0],))
        cur.execute("INSERT INTO plm_engineering.bom_items (parent_item, child_item, find_number, quantity_per, unit, "
                    "material_spec, approved_vendors, effectivity_date) VALUES ('PN-ENGBLK-V8-B', "
                    "'PN-ENGBLK-GASKET-REV-A', 40, 2.000, 'EA', 'MLS-STEEL', 'Added after the seed', '2025-06-01') "
                    "RETURNING bom_id")
        added = cur.fetchone()[0]
    assert _apply_all(seed_db) == []
    assert _counts(seed_db) == {**fresh_counts, "plm_engineering.bom_items":
                                fresh_counts["plm_engineering.bom_items"] + 1}
    with seed_db.cursor() as cur:
        cur.execute(_GASKET_LINE)
        rows = cur.fetchall()
    assert rows[0] == fresh[0]
    assert [(row[0], row[7]) for row in rows[1:]] == [(added, "Added after the seed")]


def test_pg_rerun_keeps_the_first_copy_of_each_seeded_log_entry(seed_db):
    """An account whose seed ran twice before the guard existed: two copies of
    every seeded entry, both under the names the account was seeded with. The
    rerun deletes the later copy of each, gives the first (its id kept) the
    seed's author, keeps every row the seed did not write (an exact pair
    included), and a second rerun changes nothing."""
    assert _apply_all(seed_db) == []
    fresh_counts, fresh_log = _counts(seed_db), _log_rows(seed_db)
    fresh_state = _site_cmms_state(seed_db)
    seeded = len([row for row in fresh_log if row[1] in ("1182", "1187", "1195")])
    _seeded_under_earlier_names(seed_db)
    with seed_db.cursor() as cur:
        cur.execute(_SECOND_COPY)
        assert cur.rowcount == seeded > 0
        cur.execute(_NOT_SEEDED)
        cur.execute("SELECT max(log_id) FROM cmms_indianapolis.work_order_log")
        top = cur.fetchone()[0]
    kept_extra = [row for row in _log_rows(seed_db) if row[0] > top - 3]
    assert len(_duplicate_log_entries(seed_db)) == seeded + 1

    assert _apply_all(seed_db) == []
    assert _log_rows(seed_db) == fresh_log + kept_extra
    assert _duplicate_log_entries(seed_db) == [
        ("1201", kept_extra[1][2], "UPDATE", "Logged twice by its source.", 2)]
    assert _earlier_names_left(seed_db) == 0
    assert _counts(seed_db) == {**fresh_counts, "cmms_indianapolis.work_order_log":
                                fresh_counts["cmms_indianapolis.work_order_log"] + 3}
    after = _site_cmms_state(seed_db)
    assert {k: v for k, v in after.items() if k != "work_order_log"} == {
        k: v for k, v in fresh_state.items() if k != "work_order_log"}

    assert _apply_all(seed_db) == []
    assert _site_cmms_state(seed_db) == after
