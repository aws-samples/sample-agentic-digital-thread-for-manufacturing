# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The row-id note on real PostgreSQL: the db-init ERP database and a generic schema.

Test event, 2026-10-15: asked which batches drew one material lot, the ERP
system agent read ``lot_production_usage`` and answered with two UUID
``production_run_id`` values. These run onboarding's own baked schema and the
system agent's own SQL tool on real data and pin:

- the ERP agent's schema (the ``erp_data`` pass-through views) has no
  ``production_runs`` view, so no join key to it is declared there and a
  result of run ids carries no note: that case needs the run table of
  another system (the gap this note does not close);
- on the db-init base tables the declared key ``lot_production_usage.
  production_run_id = production_runs.run_id`` and the UNIQUE
  ``production_runs.run_number`` give the note, and the join it names
  returns each run's number;
- on ``erp_data`` a result of lot ids names ``material_lots.lot_number``,
  in Mode A and Mode B, and the unique-column read works as the schema's
  read-only role (pg_catalog, not information_schema);
- two uses of one lot keep their own run ids: joined on the lot, each pairs
  with every one of the lot's MES work orders, so no sibling table names the
  run, and no note ever points at one;
- on a generic schema: partial and expression unique indexes and a view
  column renamed from another base column prove nothing unique; an integer
  row id is named too.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_story_traceability_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_row_id_names_pg.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.integration import story_pg_support as support  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_ERP = support.ERP_VIEW_SCHEMA
_RUN_KEY = ("lot_production_usage", "production_run_id", "production_runs", "run_id")
_RUN_NOTE = ("[engine] production_run_id is a row id; production_runs.run_number names that "
             "record; join on lot_production_usage.production_run_id = production_runs.run_id "
             "and return it, with the row id labelled as the record's lookup key.")
_LOT_NOTE = ("[engine] lot_id is a row id; material_lots.lot_number names that record; join on "
             "lot_production_usage.lot_id = material_lots.lot_id and return it, with the row id "
             "labelled as the record's lookup key.")
_RUN_IDS_SQL = ("SELECT production_run_id, quantity_used FROM lot_production_usage "
                "ORDER BY usage_date LIMIT 5")

#: A base schema behind a view schema, with unique indexes that prove nothing
#: (partial, expression), and a second view schema whose view renames another
#: column to the unique column's name. Not ERP-family, so it routes to the
#: seed database.
_GENERIC_DDL = """
CREATE SCHEMA rk_base;
CREATE TABLE rk_base.items (item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            item_code VARCHAR(40) UNIQUE NOT NULL, label TEXT, alt_code TEXT);
CREATE UNIQUE INDEX items_alt_code_partial ON rk_base.items (alt_code) WHERE alt_code IS NOT NULL;
CREATE UNIQUE INDEX items_label_lower ON rk_base.items (lower(label));
CREATE TABLE rk_base.usages (usage_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                             item_id UUID NOT NULL REFERENCES rk_base.items (item_id),
                             quantity NUMERIC(10, 2));
CREATE TABLE rk_base.groups (group_id INTEGER PRIMARY KEY, group_name TEXT UNIQUE NOT NULL);
CREATE TABLE rk_base.members (member_id INTEGER PRIMARY KEY,
                              group_id INTEGER NOT NULL REFERENCES rk_base.groups (group_id));
CREATE SCHEMA rk;
CREATE VIEW rk.items AS SELECT * FROM rk_base.items;
CREATE VIEW rk.usages AS SELECT * FROM rk_base.usages;
CREATE VIEW rk.groups AS SELECT * FROM rk_base.groups;
CREATE VIEW rk.members AS SELECT * FROM rk_base.members;
CREATE SCHEMA rk_alt;
CREATE VIEW rk_alt.items AS SELECT item_id, label AS item_code FROM rk_base.items;
INSERT INTO rk_base.items (item_code, label) VALUES ('CODE-A', 'first'), ('CODE-B', 'second');
INSERT INTO rk_base.usages (item_id, quantity) SELECT item_id, 2.5 FROM rk_base.items;
INSERT INTO rk_base.groups VALUES (1, 'Group one'), (2, 'Group two');
INSERT INTO rk_base.members VALUES (10, 1), (11, 2), (12, 2);
"""


@pytest.fixture(scope="module")
def dbs():
    """{erp|seed: connection}: db-init, the live story history, and the
    generic schemas on the seed database."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "rk") as (conns, _plan):
        with conns["seed"].cursor() as cur:
            cur.execute(_GENERIC_DDL)
        conns["seed"].commit()
        yield conns


def _pg_on(conn):
    """An ``agents.db.pg`` stand-in that runs every schema on *conn*."""
    def pg(schema, sql, params=None):
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('search_path', %s, false)", (schema,))
            cur.execute(sql, params)
            rows = [list(r) for r in cur.fetchall()]
        conn.rollback()
        return {"rows": rows}
    return pg


def _route_onboarding(monkeypatch, pg):
    import agents.db as db
    import agents.onboard as onboard
    monkeypatch.setattr(onboard, "pg", pg)
    monkeypatch.setattr(db, "pg", pg)
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    return onboard


@pytest.fixture
def onboard_pg(dbs, monkeypatch):
    """Onboarding's own ``pg`` over the test DBs, routed as production."""
    return _route_onboarding(monkeypatch, support.routed_pg(dbs))


@pytest.fixture
def onboard_erp_db(dbs, monkeypatch):
    """Onboarding's own ``pg`` with every schema (``public`` included) on the
    ERP database."""
    return _route_onboarding(monkeypatch, _pg_on(dbs["erp"]))


class _SharedConn:
    """The test connection handed to the SQL tool as its own (see
    ``test_aggregate_guard_pg``); the test rolls its transaction back."""

    def __init__(self, conn):
        self._conn = conn

    def set_session(self, **_kwargs):
        return None

    def cursor(self):
        return self._conn.cursor()

    def close(self):
        return None


@pytest.fixture
def sql_tool(monkeypatch):
    """``(system_agent, use)``: ``use(conn)`` points both SQL tools at a test DB."""
    import agents.row_id_names as rn
    import agents.system_agent as sa
    used = []

    def use(conn):
        used.append(conn)
        monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _SharedConn(conn))
        monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _SharedConn(conn))
        monkeypatch.setattr(sa, "_resolve_pg_conn_params", lambda *_a: {})
    rn.clear_cache()
    yield sa, use
    for conn in used:
        conn.rollback()
    rn.clear_cache()


def _relations(conn, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    (schema,))
        names = sorted(r[0] for r in cur.fetchall())
    conn.rollback()
    return names


def _baked_desc(onboard, schema: str, tables) -> str:
    ctx = onboard._ProvisionState(
        body=None, system_id="sys", system_name="System", plant="", schema=schema, rules=[],
        mappings=[], connection_type="PostgreSQL", cm_items=[], tables_seen=set(tables))
    return onboard._dp_pg_tables_desc(ctx)


# --------------------------------------------------------------------------- #
# The ERP database from db-init
# --------------------------------------------------------------------------- #
def test_the_erp_view_schema_has_no_run_table_so_run_ids_get_no_note(onboard_pg, dbs, sql_tool):
    """Characterizes the event's case: the ERP agent's schema reads the lot
    usage rows but not the run table, so nothing in it names a run."""
    from agents import row_id_names as rn
    from agents.schema_format import declared_join_keys
    views = _relations(dbs["erp"], _ERP)
    assert "lot_production_usage" in views and "production_runs" not in views
    desc = _baked_desc(onboard_pg, _ERP, views)
    assert not [k for k in declared_join_keys(desc) if k[1] == "production_run_id"]
    sa, use = sql_tool
    use(dbs["erp"])
    result, _ = sa._execute_direct_query(_ERP, {}, _RUN_IDS_SQL, rn.context_from(desc))
    assert result["row_count"] > 0 and rn.ROW_ID_NOTE_KEY not in result


def test_on_the_db_init_tables_the_run_ids_name_the_run_number(onboard_erp_db, dbs, sql_tool):
    """The base tables db-init creates declare the run key, and
    ``production_runs.run_number`` is UNIQUE: what the note says where the
    agent's schema holds both tables."""
    from agents import row_id_names as rn
    from agents.schema_format import declared_join_keys
    desc = _baked_desc(onboard_erp_db, "public", ["lot_production_usage", "production_runs"])
    assert _RUN_KEY in declared_join_keys(desc)
    sa, use = sql_tool
    use(dbs["erp"])
    context = rn.context_from(desc)
    result, _ = sa._execute_direct_query("public", {}, _RUN_IDS_SQL, context)
    assert result["row_count"] > 0
    assert result[rn.ROW_ID_NOTE_KEY] == _RUN_NOTE
    # The join the note names returns each run's number, and carries no note.
    joined, _ = sa._execute_direct_query("public", {}, (
        "SELECT pr.run_number, lpu.quantity_used FROM lot_production_usage lpu "
        "JOIN production_runs pr ON pr.run_id = lpu.production_run_id "
        "ORDER BY lpu.usage_date LIMIT 5"), context)
    assert rn.ROW_ID_NOTE_KEY not in joined
    assert joined["row_count"] == result["row_count"]
    assert all(isinstance(row[0], str) and row[0] for row in joined["rows"])


def test_lot_ids_on_the_erp_view_schema_name_the_lot_number(onboard_pg, dbs, sql_tool):
    from agents import row_id_names as rn
    desc = _baked_desc(onboard_pg, _ERP, _relations(dbs["erp"], _ERP))
    sa, use = sql_tool
    use(dbs["erp"])
    sql = "SELECT lot_id, quantity_used FROM lot_production_usage ORDER BY usage_date LIMIT 5"
    result, _ = sa._execute_direct_query(_ERP, {}, sql, rn.context_from(desc))
    assert result[rn.ROW_ID_NOTE_KEY] == _LOT_NOTE
    out = sa._mode_b_run_query("PostgreSQL", sql, _ERP, {}, None, desc)
    assert out[rn.ROW_ID_NOTE_KEY] == _LOT_NOTE


def _lot_with_two_runs(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT lot_id::text FROM erp_data.lot_production_usage GROUP BY lot_id "
                    "HAVING count(DISTINCT production_run_id) >= 2 ORDER BY 1 LIMIT 1")
        found = cur.fetchone()
    conn.rollback()
    assert found, "the story history draws at least one lot into two runs"
    return str(uuid.UUID(found[0]))          # a UUID, safe to place in the SQL text


def test_two_uses_of_one_lot_keep_their_own_run_ids(onboard_pg, dbs, sql_tool):
    """Each use of a lot names its own run only by the run's row id on the
    ERP views. A sibling table linked through the same lot is no proof of
    the run: joined on the lot, every use pairs with every one of the lot's
    MES work orders, so a key read from it would collapse or swap the two
    runs. The note never points at such a table; it names only the lot."""
    from agents import row_id_names as rn
    desc = _baked_desc(onboard_pg, _ERP, _relations(dbs["erp"], _ERP))
    context = rn.context_from(desc)
    lot = _lot_with_two_runs(dbs["erp"])
    sa, use = sql_tool
    use(dbs["erp"])
    uses, _ = sa._execute_direct_query(_ERP, {}, (
        "SELECT production_run_id, quantity_used FROM lot_production_usage "
        f"WHERE lot_id = '{lot}' ORDER BY usage_date"), context)
    runs = [row[0] for row in uses["rows"]]
    assert len(set(runs)) == len(runs) >= 2
    assert rn.ROW_ID_NOTE_KEY not in uses
    with_lot, _ = sa._execute_direct_query(_ERP, {}, (
        "SELECT lot_id, production_run_id, quantity_used FROM lot_production_usage "
        f"WHERE lot_id = '{lot}'"), context)
    assert with_lot[rn.ROW_ID_NOTE_KEY] == _LOT_NOTE
    paired, _ = sa._execute_direct_query(_ERP, {}, (
        "SELECT u.production_run_id, t.batch_id FROM lot_production_usage u "
        f"JOIN lot_mes_tracking t ON t.lot_id = u.lot_id WHERE u.lot_id = '{lot}'"), context)
    per_run: dict = {}
    for run, batch in paired["rows"]:
        per_run.setdefault(run, set()).add(batch)
    assert set(per_run) == set(runs)
    assert all(len(batches) == len(runs) for batches in per_run.values())   # no 1:1 pairing
    for result in (uses, with_lot, paired):
        note = result.get(rn.ROW_ID_NOTE_KEY, "")
        assert "lot_mes_tracking" not in note and "shipment_items" not in note


def test_the_schemas_read_only_role_reads_the_unique_columns_too(dbs):
    """Production SQL runs as the schema's read-only login, which owns no
    table: pg_catalog is readable by every role (information_schema's
    constraint views list nothing to it)."""
    from agents import row_id_names as rn
    conn = dbs["erp"]
    with conn.cursor() as cur:
        cur.execute("CREATE ROLE rk_row_id_reader NOLOGIN")
        cur.execute("GRANT USAGE ON SCHEMA erp_data TO rk_row_id_reader")
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp_data TO rk_row_id_reader")
        cur.execute("SET ROLE rk_row_id_reader")
        try:
            unique = rn.unique_columns(cur, _ERP)
            cur.execute("SELECT count(*) FROM information_schema.table_constraints "
                        "WHERE constraint_type = 'UNIQUE' AND table_name = 'material_lots'")
            visible_constraints = cur.fetchone()[0]
        finally:
            cur.execute("RESET ROLE")
    conn.rollback()                                   # the role goes with the transaction
    assert visible_constraints == 0
    assert unique[("material_lots", "lot_number")] == "character varying(100)"
    assert ("material_lots", "lot_id") not in unique          # the primary key is the row id


# --------------------------------------------------------------------------- #
# A generic schema
# --------------------------------------------------------------------------- #
def test_only_a_full_unique_column_proven_through_the_view_counts(dbs):
    from agents import row_id_names as rn
    with dbs["seed"].cursor() as cur:
        views = rn.unique_columns(cur, "rk")
        base = rn.unique_columns(cur, "rk_base")
        renamed = rn.unique_columns(cur, "rk_alt")
    dbs["seed"].rollback()
    assert views == {("items", "item_code"): "character varying(40)",
                     ("groups", "group_name"): "text"}
    assert base == views                       # the base tables, read directly
    assert renamed == {}                       # its item_code is the base label


def test_uuid_and_integer_row_ids_on_the_generic_views(onboard_pg, dbs, sql_tool):
    from agents import row_id_names as rn
    sa, use = sql_tool
    use(dbs["seed"])
    context = rn.context_from(_baked_desc(onboard_pg, "rk", _relations(dbs["seed"], "rk")))
    uuids, _ = sa._execute_direct_query("rk", {}, "SELECT item_id, quantity FROM usages", context)
    assert uuids[rn.ROW_ID_NOTE_KEY] == (
        "[engine] item_id is a row id; items.item_code names that record; join on "
        "usages.item_id = items.item_id and return it, with the row id labelled as the "
        "record's lookup key.")
    ints, _ = sa._execute_direct_query(
        "rk", {}, "SELECT group_id, count(*) AS n FROM members GROUP BY group_id", context)
    assert ints[rn.ROW_ID_NOTE_KEY] == (
        "[engine] group_id is a row id; groups.group_name names that record; join on "
        "members.group_id = groups.group_id and return it, with the row id labelled as the "
        "record's lookup key.")
