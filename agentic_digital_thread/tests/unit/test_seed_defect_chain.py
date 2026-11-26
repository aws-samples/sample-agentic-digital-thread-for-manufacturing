# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline pins for the B-2847 cross-system defect chain (workshop scenario).

The Indianapolis defect story spans four seed systems and must stay mutually
coherent — this is the regression guard so the chain cannot silently drift:

  * MES (003_mes_line3.sql): batch B-2847 ran on L3-M1 (work order WO-9921,
    product 7742) 2026-02-10 06:00-11:23 and was Stopped by SPC diameter drift;
  * historian (009_historian.sql): the SAME machine (5-Axis-Milling-CMX-441)
    shows escalating vibration Feb 10-14, recovering after the bearing
    replacement;
  * CMMS (011_cmms_indianapolis.sql): asset 1001-CMX-441 carries a DEFERRED
    90-day spindle bearing PM (work order 1182) whose ORIGINAL due date is
    ~6 weeks BEFORE the B-2847 run and which was never performed, plus the
    emergency bearing replacement (work order 1195) that fits the MES idle
    window;
  * PLM (005_plm_engineering.sql): the batch's product code has exactly one
    Released part revision sharing its identifier core — the MES -> PLM
    identity rule the graph discovers (agents/identity_affinity.py). Customer
    impact then runs through the one corporate ERP's part cross-reference
    (lambda-db-init), not through a plant ERP schema in this seed.

Fully offline: the seed SQL is parsed as text (no database needed).
"""

import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

_SEED_DIR = _ADT_ROOT.parent / "lambda-adt-seed"
_MES_SQL = (_SEED_DIR / "003_mes_line3.sql").read_text()
_PLM_SQL = (_SEED_DIR / "005_plm_engineering.sql").read_text()
_HIST_SQL = (_SEED_DIR / "009_historian.sql").read_text()
_CMMS_SQL = (_SEED_DIR / "011_cmms_indianapolis.sql").read_text()

# Canonical scenario anchors.
_MES_RUN_START = datetime(2026, 2, 10, 6, 0)


# --------------------------------------------------------------------------- #
# SQL VALUES-tuple parsing helpers (quote-aware — descriptions contain commas
# and parentheses, so naive regex splitting is not safe).
# --------------------------------------------------------------------------- #

def _extract_tuple(sql: str, start: int) -> list:
    """Parse the SQL VALUES tuple opening at ``sql[start] == '('`` into fields.

    Handles single-quoted strings (with '' escapes) and nested parentheses.
    Returns raw field strings; use :func:`_unq` to unquote them.
    """
    assert sql[start] == "(", sql[start : start + 20]
    depth, in_quote = 0, False
    fields, cur = [], []
    i = start
    while i < len(sql):
        c = sql[i]
        if in_quote:
            cur.append(c)
            if c == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    cur.append("'")
                    i += 1
                else:
                    in_quote = False
        elif c == "'":
            in_quote = True
            cur.append(c)
        elif c == "(":
            depth += 1
            if depth > 1:
                cur.append(c)
        elif c == ")":
            depth -= 1
            if depth == 0:
                fields.append("".join(cur).strip())
                return fields
            cur.append(c)
        elif c == "," and depth == 1:
            fields.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    raise AssertionError("unterminated SQL tuple")


def _insert_blocks(sql: str, table: str) -> str:
    """Concatenated text of every ``INSERT INTO <table> ...;`` statement.

    Statements end with a semicolon at end-of-line (quoted descriptions may
    contain mid-line semicolons, so a bare ``.*?;`` would truncate).
    """
    blocks = re.findall(
        rf"INSERT INTO {re.escape(table)}[\s(].*?;[ \t]*(?:\n|$)", sql, re.S
    )
    assert blocks, f"no INSERT INTO {table} blocks found"
    return "\n".join(blocks)


def _tuple_for(sql: str, head: str) -> list:
    """The single VALUES tuple whose first field is the quoted string *head*."""
    starts = [m.start() for m in re.finditer(re.escape(f"('{head}'"), sql)]
    assert len(starts) == 1, f"expected exactly one tuple for {head!r}, got {len(starts)}"
    return _extract_tuple(sql, starts[0])


def _unq(field: str):
    """Unquote a raw SQL field ('x''y' -> x'y); NULL -> None; else verbatim."""
    if field.upper() == "NULL":
        return None
    if field.startswith("'") and field.endswith("'"):
        return field[1:-1].replace("''", "'")
    return field


def _ts(field: str) -> datetime:
    value = _unq(field)
    assert value is not None, "expected a timestamp, got NULL"
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# MES — the stopped B-2847 run on L3-M1.
# --------------------------------------------------------------------------- #

def _mes_run(batch: str) -> dict:
    f = _tuple_for(_insert_blocks(_MES_SQL, "production_runs"), batch)
    return {
        "work_order_ref": _unq(f[1]), "line": _unq(f[2]), "equipment": _unq(f[3]),
        "product": _unq(f[4]), "start": _ts(f[9]),
        "end": _ts(f[10]) if _unq(f[10]) else None, "status": _unq(f[11]),
    }


def test_mes_b2847_stopped_run_on_l3m1():
    run = _mes_run("B-2847")
    assert run["equipment"] == "L3-M1"
    assert run["work_order_ref"] == "WO-9921"
    assert run["product"] == "7742"
    assert run["status"] == "Stopped"
    assert run["start"] == _MES_RUN_START
    assert run["end"] == datetime(2026, 2, 10, 11, 23)


# --------------------------------------------------------------------------- #
# Historian — vibration data on the SAME machine + the identity chain.
# --------------------------------------------------------------------------- #

def test_historian_has_cmx441_vibration_and_identity_chain():
    # Registry row + a vibration INSERT targeting the machine.
    assert "'5-Axis-Milling-CMX-441', '5-Axis Milling Center CMX-441', 'Indianapolis'" in _HIST_SQL
    vib_blocks = [
        b for b in _HIST_SQL.split("INSERT INTO")
        if "'5-Axis-Milling-CMX-441'" in b and "'vibration'" in b
    ]
    assert vib_blocks, "no vibration INSERT for 5-Axis-Milling-CMX-441"
    # The documented identity chain must name all three system-local ids.
    header = _HIST_SQL[:2000]
    assert "5-Axis-Milling-CMX-441 = MES L3-M1 = CMMS 1001-CMX-441" in header


# --------------------------------------------------------------------------- #
# CMMS — deferred PM predates the run; emergency repair fits the idle window.
# --------------------------------------------------------------------------- #

def _cmms_wo(wonum: str) -> dict:
    f = _tuple_for(_insert_blocks(_CMMS_SQL, "work_orders"), wonum)
    return {
        "asset": _unq(f[1]), "work_type": _unq(f[2]), "status": _unq(f[4]),
        "pm_num": _unq(f[6]),
        "reported": _ts(f[8]), "target_start": _ts(f[9]),
        "actual_start": _ts(f[11]) if _unq(f[11]) else None,
        "actual_finish": _ts(f[12]) if _unq(f[12]) else None,
        "failure_code": _unq(f[13]),
    }


def test_cmms_asset_and_schema_exist():
    assert "CREATE SCHEMA IF NOT EXISTS cmms_indianapolis" in _CMMS_SQL
    assert "('1001-CMX-441', '5-Axis Milling Center CMX-441'" in _CMMS_SQL


def test_cmms_deferred_pm_1182_predates_the_run_and_was_never_performed():
    wo = _cmms_wo("1182")
    assert wo["asset"] == "1001-CMX-441"
    assert wo["work_type"] == "PM"
    assert wo["status"] == "WAPPR", "the deferred PM must still be waiting approval"
    assert wo["actual_start"] is None and wo["actual_finish"] is None, (
        "the deferred PM must NOT have been performed before the B-2847 run"
    )
    gap_days = (_MES_RUN_START - wo["target_start"]).days
    assert 35 <= gap_days <= 56, (
        f"original PM due date must be ~6 weeks before the run, got {gap_days} days"
    )
    # It hangs off the 90-day spindle bearing PM schedule.
    assert wo["pm_num"] == "PM-1001-0007"
    assert "PM-1001-0007', '1001-CMX-441'" in _CMMS_SQL


def test_cmms_bearing_replacement_fits_mes_idle_window_and_historian():
    wo = _cmms_wo("1195")
    assert wo["asset"] == "1001-CMX-441"
    assert wo["work_type"] == "EM"
    assert wo["failure_code"] == "BEARING"
    assert wo["reported"] > _MES_RUN_START, "failure follows the stopped run"
    # MES shows L3-M1 producing B-2869 until Feb 14 22:00 and B-2873 from
    # Feb 15 14:00 — the repair must fit that idle window.
    b2869_end = _mes_run("B-2869")["end"]
    b2873_start = _mes_run("B-2873")["start"]
    assert wo["actual_start"] is not None and wo["actual_finish"] is not None
    assert b2869_end <= wo["actual_start"], (wo["actual_start"], b2869_end)
    assert wo["actual_finish"] <= b2873_start, (wo["actual_finish"], b2873_start)
    # The historian recovery branch pins the same completion instant.
    finish_literal = wo["actual_finish"].strftime("%Y-%m-%d %H:%M")
    assert f"'{finish_literal}:00+00'" in _HIST_SQL, (
        "historian CMX-441 recovery branch must switch at the WO 1195 finish time"
    )


# --------------------------------------------------------------------------- #
# PLM — the batch's product has exactly one Released revision.
# --------------------------------------------------------------------------- #

def _plm_items() -> list:
    block = _insert_blocks(_PLM_SQL, "products")
    items = []
    for m in re.finditer(r"\('PN-", block):
        f = _extract_tuple(block, m.start())
        items.append({"item_id": _unq(f[0]), "lifecycle": _unq(f[5])})
    assert items, "no PLM product rows parsed"
    return items


def test_mes_story_batch_product_has_a_released_plm_revision():
    from agents.identity_affinity import digit_cores

    product = _mes_run("B-2847")["product"]
    cores = digit_cores(product)
    assert cores, f"product code {product!r} carries no identifier core"
    released = [i["item_id"] for i in _plm_items()
                if i["lifecycle"] == "Released" and cores & digit_cores(i["item_id"])]
    assert len(released) == 1, (
        f"product {product} must match exactly one Released PLM revision, got {released}"
    )


#: The ONLY ERP-named schemas the seed database may create (verified against
#: lambda-adt-seed/*.sql). Anything else ``erp_*`` is a plant ERP creeping back.
_SEED_ERP_ALLOWLIST = frozenset({"erp_detroit"})

_CREATE_SCHEMA = re.compile(r"(?i)\bCREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)")


def _created_schemas(sql: str) -> set:
    return set(_CREATE_SCHEMA.findall(sql))


def _story_plant_schemas() -> tuple:
    """(plant token, schemas that may carry it) — derived from the manifest.

    The token is the plant part of the register-only maintenance schema; the
    only seed schemas allowed to contain it are that maintenance schema and the
    frozen storyline MES schema (same id as the live MES, different schema)."""
    from agents.demo import workshop

    maint = workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS[0]["schema"]
    mes_id = workshop.LIVE_MES_SYSTEM["id"]
    frozen_mes = next(s["schema"] for s in workshop.WORKSHOP_SEEDED_SYSTEMS
                      if s["id"] == mes_id)
    return maint.split("_", 1)[1], frozenset({maint, frozen_mes})


def _plant_schema_violations(sql: str) -> list:
    token, allowed = _story_plant_schemas()
    return sorted(s for s in _created_schemas(sql)
                  if (s.startswith("erp_") and s not in _SEED_ERP_ALLOWLIST)
                  or (token in s and s not in allowed))


def _all_seed_sql() -> str:
    return "\n".join(p.read_text() for p in sorted(_SEED_DIR.glob("*.sql")))


def test_no_seed_file_defines_a_plant_erp_schema():
    """One ERP: the seed creates no ``erp_*`` schema outside the explicit
    allowlist, and no schema carrying the story plant's token except its
    maintenance schema and the frozen MES schema — the plant's orders and lots
    live in the corporate ERP, and its live MES schema lives in the ERP DB."""
    sql = _all_seed_sql()
    assert _SEED_ERP_ALLOWLIST <= _created_schemas(sql)  # allowlist not stale
    assert _plant_schema_violations(sql) == []


@pytest.mark.parametrize("make_sql", [
    lambda token: f"CREATE SCHEMA IF NOT EXISTS erp_{token};",   # the old plant ERP
    lambda token: "create schema erp_other;",                    # any unlisted ERP
    lambda token: f"CREATE SCHEMA IF NOT EXISTS mes_{token};",   # live MES in seed
])
def test_plant_schema_guard_rejects_a_reintroduced_schema(make_sql):
    token, _ = _story_plant_schemas()
    sql = _all_seed_sql() + "\n" + make_sql(token)
    assert len(_plant_schema_violations(sql)) == 1


# --------------------------------------------------------------------------- #
# Routing — the new schema stays in the demo-seed database family (ADR-0044).
# --------------------------------------------------------------------------- #

def test_plant_maintenance_schema_routes_to_seed_db():
    from agents.demo import workshop
    from agents.demo.db_routing import is_erp_schema

    assert not is_erp_schema(workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS[0]["schema"])


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
