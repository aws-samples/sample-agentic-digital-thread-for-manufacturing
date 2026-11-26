# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision measures each ``*_id`` name-pattern join on PostgreSQL (b3).

A plant MES's run view joins the runs to the products table and renames the
run key (``run_id AS production_run_id``), so no declared foreign key can be
read through it and provision's only record of the work-order to run join was
a guess from the column names. Since a guess no longer pairs identifier forms
per item, the route seed lost its IDENTIFIER FORMS line (rehearsal, 2026-10-17).

These tests run onboarding's own b3 writer (``_dp_internal_joinpaths``) and
its measurement SQL on a real server:

* a synthetic schema shaped like that view (join plus rename) and four
  hostile neighbours: a referencing value missing from the target, a target
  that repeats a value, a target with a NULL, and a target read through a
  slow function under a short statement deadline;
* two tables the column name could mean whose keys hold the same values
  (neither is promoted), beside two where only the written one does;
* UUID strings in a text column naming a uuid key: no ``=`` between the
  types, so the read fails closed;
* the site MES view schema db-init creates: with the live-story history the
  work-order to run join is measured; on the frozen seed (no work orders for
  that plant) it stays a guess that says there were too few values.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server. Run (from
``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_measured_reference_joins_pg.py -q
"""
from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.demo import workshop  # noqa: E402
from tests.integration import story_pg_support as support  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
MEASURED, GUESS = "measured_reference", "column_name_pattern"

#: Base tables, then views over them. Neither schema name is ERP-family, so
#: ``routed_pg`` sends them to the seed database.
_DDL = """
CREATE SCHEMA w8r_base;
CREATE TABLE w8r_base.products (product_id INTEGER PRIMARY KEY, sku TEXT NOT NULL);
CREATE TABLE w8r_base.runs (run_id INTEGER PRIMARY KEY, run_number TEXT NOT NULL,
                            product_id INTEGER NOT NULL REFERENCES w8r_base.products);
CREATE TABLE w8r_base.orders (order_id INTEGER PRIMARY KEY,
                              batch_id INTEGER REFERENCES w8r_base.runs (run_id));
CREATE TABLE w8r_base.tickets (ticket_id INTEGER PRIMARY KEY, batch_id INTEGER);
CREATE TABLE w8r_base.cells (cell_id INTEGER, label TEXT);
CREATE TABLE w8r_base.stops (stop_id INTEGER PRIMARY KEY, cell_id INTEGER);
CREATE TABLE w8r_base.bays (bay_id INTEGER, label TEXT);
CREATE TABLE w8r_base.moves (move_id INTEGER PRIMARY KEY, bay_id INTEGER);
INSERT INTO w8r_base.products VALUES (1, 'P-1'), (2, 'P-2');
INSERT INTO w8r_base.runs SELECT g, 'R-' || g, 1 + g % 2 FROM generate_series(1, 8) g;
INSERT INTO w8r_base.orders SELECT g, 1 + g % 8 FROM generate_series(1, 30) g;
INSERT INTO w8r_base.orders VALUES (31, NULL);
INSERT INTO w8r_base.tickets SELECT g, 1 + g % 9 FROM generate_series(1, 30) g;
INSERT INTO w8r_base.cells SELECT g, 'C' || g FROM generate_series(1, 5) g;
INSERT INTO w8r_base.cells VALUES (3, 'C3-again');
INSERT INTO w8r_base.stops SELECT g, 1 + g % 5 FROM generate_series(1, 20) g;
INSERT INTO w8r_base.bays SELECT g, 'B' || g FROM generate_series(1, 5) g;
INSERT INTO w8r_base.bays VALUES (NULL, 'unassigned');
INSERT INTO w8r_base.moves SELECT g, 1 + g % 5 FROM generate_series(1, 20) g;
CREATE FUNCTION w8r_base.slow_id(integer) RETURNS integer
    LANGUAGE sql VOLATILE AS 'SELECT $1 FROM pg_sleep(0.1)';
CREATE TABLE w8r_base.slow_items (item_id INTEGER PRIMARY KEY);
CREATE TABLE w8r_base.picks (pick_id INTEGER PRIMARY KEY, slow_item_id INTEGER);
INSERT INTO w8r_base.slow_items SELECT g FROM generate_series(1, 30) g;
INSERT INTO w8r_base.picks SELECT g, 1 + g % 30 FROM generate_series(1, 10) g;

CREATE SCHEMA w8r;
-- Shaped like the site run view: a join to another table and a renamed key.
CREATE VIEW w8r.batches AS SELECT r.run_id AS batch_id, r.run_number, p.sku
    FROM w8r_base.runs r JOIN w8r_base.products p ON p.product_id = r.product_id;
CREATE VIEW w8r.orders AS SELECT o.order_id, o.batch_id FROM w8r_base.orders o
    JOIN w8r_base.runs r ON r.run_id = o.batch_id;
-- A referencing value (9) the target does not hold.
CREATE VIEW w8r.tickets AS SELECT t.ticket_id, t.batch_id, p.sku FROM w8r_base.tickets t
    JOIN w8r_base.products p ON p.product_id = 1;
-- A target that holds one value twice.
CREATE VIEW w8r.cells AS SELECT c.cell_id, c.label, p.sku FROM w8r_base.cells c
    JOIN w8r_base.products p ON p.product_id = 1;
CREATE VIEW w8r.stops AS SELECT s.stop_id, s.cell_id, p.sku FROM w8r_base.stops s
    JOIN w8r_base.products p ON p.product_id = 1;
-- A target with a NULL key.
CREATE VIEW w8r.bays AS SELECT b.bay_id, b.label, p.sku FROM w8r_base.bays b
    JOIN w8r_base.products p ON p.product_id = 1;
CREATE VIEW w8r.moves AS SELECT m.move_id, m.bay_id, p.sku FROM w8r_base.moves m
    JOIN w8r_base.products p ON p.product_id = 1;

CREATE SCHEMA w8s;
-- A target read through a slow function: about 3 s for the whole column.
CREATE VIEW w8s.slow_items AS SELECT w8r_base.slow_id(item_id) AS slow_item_id
    FROM w8r_base.slow_items;
CREATE VIEW w8s.picks AS SELECT p.pick_id, p.slow_item_id, pr.sku FROM w8r_base.picks p
    JOIN w8r_base.products pr ON pr.product_id = 1;

CREATE SCHEMA w8a;
-- Two tables a line id could name, whose keys hold the same values.
CREATE TABLE w8a.assembly_lines (line_id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE w8a.production_lines (line_id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE w8a.events (event_id INTEGER PRIMARY KEY, line_id INTEGER);
INSERT INTO w8a.assembly_lines SELECT g, 'A' || g FROM generate_series(1, 5) g;
INSERT INTO w8a.production_lines SELECT g, 'P' || g FROM generate_series(1, 5) g;
INSERT INTO w8a.events SELECT g, 1 + g % 5 FROM generate_series(1, 20) g;
-- Two tables a cell id could name, only the first holding the values.
CREATE TABLE w8a.assembly_cells (cell_id INTEGER PRIMARY KEY);
CREATE TABLE w8a.production_cells (cell_id INTEGER PRIMARY KEY);
CREATE TABLE w8a.alarms (alarm_id INTEGER PRIMARY KEY, cell_id INTEGER);
INSERT INTO w8a.assembly_cells SELECT g FROM generate_series(1, 5) g;
INSERT INTO w8a.production_cells SELECT g FROM generate_series(101, 105) g;
INSERT INTO w8a.alarms SELECT g, 1 + g % 5 FROM generate_series(1, 20) g;

CREATE SCHEMA w8u;
-- UUID strings in a text column, the keys they name in a uuid column.
CREATE TABLE w8u.tags (tag_id UUID PRIMARY KEY);
CREATE TABLE w8u.scans (scan_id INTEGER PRIMARY KEY, tag_id TEXT);
INSERT INTO w8u.tags SELECT md5(g::text)::uuid FROM generate_series(1, 6) g;
INSERT INTO w8u.scans SELECT g, md5((1 + g % 6)::text)::uuid::text FROM generate_series(1, 12) g;
"""


@pytest.fixture(scope="module", params=[True, False], ids=["live-story", "frozen"])
def dbs(request):
    """``{"conns": {erp|seed: connection}, "history": bool}``: db-init (incl. the
    site MES views), the seed SQL, the live-story history when ``history``,
    and the synthetic schemas."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "mref", with_history=request.param) as (conns, _plan):
        with conns["seed"].cursor() as cur:
            cur.execute(_DDL)
        conns["seed"].commit()
        yield {"conns": conns, "history": request.param}


@pytest.fixture
def b3(dbs, monkeypatch):
    """Run provision step b3 on one schema against the test databases; return
    ``(joins, ctx, reads)``: each written JoinPath's endpoints and evidence."""
    import agents.onboard as onboard
    from agents.models import ProvisionRequest
    routed = support.routed_pg(dbs["conns"])
    reads: list = []

    def pg(schema, sql, params=None, **kwargs):
        reads.append(sql)
        return routed(schema, sql, params, **kwargs)
    monkeypatch.setattr(onboard, "pg", pg)

    def run(schema, system_id="sys-pg"):
        writes: list = []
        monkeypatch.setattr(onboard, "neptune_write",
                            lambda c, p=None: writes.append((c, dict(p or {}))) or True)
        body = ProvisionRequest(system_id=system_id, system_name=system_id, schema=schema,
                                connection_type="PostgreSQL")
        ctx = onboard._ProvisionState(
            body=body, system_id=system_id, system_name=system_id, plant="", schema=schema,
            rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[])
        reads.clear()
        onboard._dp_internal_joinpaths(ctx)
        joins = {}
        for cypher, params in writes:
            if cypher.startswith("MERGE (jp:JoinPath"):
                key = (params["ftbl"].split(".")[-1], params["ffld"],
                       params["ttbl"].split(".")[-1], params["tfld"])
                joins[key] = {"basis": params["basis"],
                              **{k: params[k] for k in ("rcount", "kcount", "rcheck") if k in params}}
                if "mat" in params:
                    measured_at[key] = params["mat"]
        return joins, ctx, list(reads)
    measured_at: dict = {}
    run.measured_at = measured_at
    return run


def test_a_join_onto_a_joined_and_renamed_key_view_is_measured(b3):
    joins, _ctx, _reads = b3("w8r")
    key = ("orders", "batch_id", "batches", "batch_id")
    assert joins[key] == {"basis": MEASURED, "rcount": 8, "kcount": 8}
    measured_at = datetime.datetime.fromisoformat(b3.measured_at[key])
    assert measured_at.utcoffset() == datetime.timedelta(0)


def test_two_target_tables_whose_keys_hold_the_same_values_promote_neither(b3):
    """``events.line_id`` could name either lines table, and both keys hold
    every value: the written guess stays a guess, and the other table gets
    no join at all."""
    joins, ctx, reads = b3("w8a")
    assert joins[("events", "line_id", "assembly_lines", "line_id")] == {
        "basis": GUESS, "rcheck": "ambiguous_target"}
    assert not [k for k in joins if k[2] == "production_lines"]
    assert any('"production_lines"' in sql for sql in reads if "NOT EXISTS" in sql)
    assert not [m for m in ctx.reference_not_measured if m["from_table"] == "events"]


def test_a_target_no_other_candidate_bears_out_is_measured(b3):
    joins, _ctx, _reads = b3("w8a")
    assert joins[("alarms", "cell_id", "assembly_cells", "cell_id")] == {
        "basis": MEASURED, "rcount": 5, "kcount": 5}


def test_text_uuid_strings_against_a_uuid_key_stay_a_guess(b3):
    """PostgreSQL has no ``=`` between text and uuid, so a join on these two
    columns cannot run as written: the read fails closed."""
    joins, ctx, _reads = b3("w8u")
    assert joins[("scans", "tag_id", "tags", "tag_id")] == {
        "basis": GUESS, "rcheck": "type_mismatch"}
    assert ctx.reference_not_measured == []


def test_a_referencing_value_the_target_lacks_keeps_the_guess(b3):
    joins, _ctx, _reads = b3("w8r")
    assert joins[("tickets", "batch_id", "batches", "batch_id")] == {
        "basis": GUESS, "rcheck": "values_missing"}


@pytest.mark.parametrize("join", [("stops", "cell_id", "cells", "cell_id"),
                                  ("moves", "bay_id", "bays", "bay_id")],
                         ids=["repeated target value", "null target value"])
def test_a_target_that_is_not_one_value_per_row_keeps_the_guess(b3, join):
    joins, _ctx, _reads = b3("w8r")
    assert joins[join] == {"basis": GUESS, "rcheck": "target_not_unique"}


def test_one_read_per_join(b3):
    joins, _ctx, reads = b3("w8r")
    measure_reads = [sql for sql in reads if "NOT EXISTS" in sql]
    assert len(measure_reads) == len(joins) == 4


def test_a_table_over_the_row_cap_is_not_measured(b3, monkeypatch):
    import agents.onboard as onboard
    monkeypatch.setattr(onboard, "REFERENCE_MEASURE_ROW_LIMIT", 5)
    joins, ctx, _reads = b3("w8r")
    assert joins[("orders", "batch_id", "batches", "batch_id")] == {
        "basis": GUESS, "rcheck": "too_large"}
    assert {"from_table": "orders", "from_field": "batch_id", "to_table": "batches",
            "to_field": "batch_id", "reason": "too_large"} in ctx.reference_not_measured


def test_a_read_past_its_deadline_keeps_the_guess_and_does_not_stall_provision(b3, monkeypatch):
    import agents.onboard as onboard
    monkeypatch.setattr(onboard, "REFERENCE_READ_TIMEOUT_MS", 300)
    started = time.monotonic()
    joins, ctx, _reads = b3("w8s")
    assert time.monotonic() - started < 2.5
    assert joins[("picks", "slow_item_id", "slow_items", "slow_item_id")] == {
        "basis": GUESS, "rcheck": "timeout"}
    assert [m["reason"] for m in ctx.reference_not_measured] == ["timeout"]


def test_the_site_mes_work_order_to_run_join_is_measured(b3, dbs):
    """The real db-init site MES views: the run view joins products and renames
    its key, and b3 now records the work order's join to it as measured once
    the live story has written this plant's work orders and runs. Without
    them (the frozen seed has none for this plant) nothing is proven and the
    join stays a guess that says so."""
    mes = workshop.LIVE_MES_SYSTEM
    joins, _ctx, _reads = b3(mes["schema"], mes["id"])
    run_join = joins[("work_orders", "production_run_id", "production_runs", "production_run_id")]
    if not dbs["history"]:
        assert run_join == {"basis": GUESS, "rcheck": "too_few_values"}, joins
        return
    assert run_join["basis"] == MEASURED, joins
    assert run_join["rcount"] >= 3 and run_join["kcount"] >= run_join["rcount"]
    # Every other name-pattern join of the schema was measured or says why not.
    for key, evidence in joins.items():
        assert evidence["basis"] == MEASURED or evidence.get("rcheck"), (key, evidence)
