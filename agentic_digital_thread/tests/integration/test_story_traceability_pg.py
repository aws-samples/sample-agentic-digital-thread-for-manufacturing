# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live-story traceability through the ONBOARDED view schemas only (PG).

The live-story M2 Q1 regression (2026-09-29): the plant MES view and the
corporate ERP view had no shared value between a failed work order and the
shipment line it shipped on — ERP ``shipment_items.batch_id`` holds ERP
``production_runs.run_number``, which no onboarded schema exposed. This test
walks the hop a system agent can now take, reading ONLY the two onboarded
view schemas (never ``public``): the MES ``work_orders`` →
``production_runs`` (b3 heuristic join on ``production_run_id``) →
``run_number`` = ERP ``shipment_items.batch_id``, and lands on the story
history's three customers. That last hop is DERIVED, never curated
(ADR-0087): the history ships every completed run, and the exact-agreement
affinity pass finds it on the seeded values (while the work-order-number
``lot_mes_tracking.batch_id`` never qualifies).

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_story_lookup_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_story_traceability_pg.py -q
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
_ERP_VIEW_SCHEMA = support.ERP_VIEW_SCHEMA
#: The hop under test (test-local: the engine derives it, nothing seeds it).
_RUN = ("production_runs", "run_number")
_SHIP = ("shipment_items", "batch_id")
_LOT = ("lot_mes_tracking", "batch_id")


@pytest.fixture(scope="module")
def story_db():
    """(erp connection, story plan) after db-init + the live story history."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "tr") as (conns, plan):
        yield conns["erp"], plan


def test_failed_work_order_reaches_its_shipment_lines_through_the_views(story_db):
    from erp_simulation import story_history
    erp, plan = story_db
    mes = workshop.LIVE_MES_SYSTEM["schema"]
    (run_tbl, run_col), (ship_tbl, ship_col) = _RUN, _SHIP
    with erp.cursor() as cur:
        # MES hop: the failed work order -> its run number (MES view schema only)
        cur.execute(stmt("""
            SELECT pr.{run_col} FROM {mes}.work_orders w
            JOIN {mes}.{run_tbl} pr ON pr.production_run_id = w.production_run_id
            WHERE w.work_order_number = %s""", run_col=run_col, mes=mes, run_tbl=run_tbl),
            (plan.failed_run.work_order_number,))
        assert cur.fetchall() == [(plan.failed_run.run_number,)]
        # ERP hop: that run number -> the shipment lines and customers (ERP view only)
        cur.execute(stmt("""
            SELECT DISTINCT c.customer_code FROM {erp}.{ship_tbl} si
            JOIN {erp}.order_line_items li ON li.line_item_id = si.line_item_id
            JOIN {erp}.customer_orders o ON o.order_id = li.order_id
            JOIN {erp}.customers c ON c.customer_id = o.customer_id
            WHERE si.{ship_col} = %s""", erp=_ERP_VIEW_SCHEMA, ship_tbl=ship_tbl, ship_col=ship_col),
            (plan.failed_run.run_number,))
        customers = {row[0] for row in cur.fetchall()}
    assert customers == set(story_history.STORY_CUSTOMER_CODES)


def test_the_old_lot_tracking_join_finds_nothing(story_db):
    """Characterizes the regression's wrong path: ``lot_mes_tracking.batch_id``
    holds the MES WORK-ORDER number, so joining it to the shipment line's
    batch id never matches the story's failed run."""
    erp, plan = story_db
    with erp.cursor() as cur:
        cur.execute(stmt("""
            SELECT count(*) FROM {erp}.lot_mes_tracking t
            JOIN {erp}.shipment_items si ON si.batch_id = t.batch_id
            WHERE t.batch_id = %s""", erp=_ERP_VIEW_SCHEMA), (plan.failed_run.work_order_number,))
        assert cur.fetchone() == (0,)


def test_provision_b3_joins_the_mes_work_order_to_its_run(story_db, monkeypatch):
    """Provision's internal-JoinPath detection (b3, ``*_id`` heuristic) links
    ``work_orders.production_run_id`` to the new ``production_runs`` view on
    the real view DDL — so the MES agent's own graph schema names the hop."""
    import agents.onboard as onboard
    erp, _plan = story_db

    def pg(_schema, sql, params=None):
        with erp.cursor() as cur:
            cur.execute(sql, params)
            return {"rows": [list(r) for r in cur.fetchall()]}
    monkeypatch.setattr(onboard, "pg", pg)
    joins = onboard._detect_internal_joinpaths(workshop.LIVE_MES_SYSTEM["schema"],
                                               workshop.LIVE_MES_SYSTEM["id"])
    ends = [{k: j[k] for k in ("from_table", "from_field", "to_table", "to_field")}
            for j in joins]
    assert {"from_table": "work_orders", "from_field": "production_run_id",
            "to_table": "production_runs", "to_field": "production_run_id"} in ends


def _hop_fields(erp, monkeypatch):
    """The run-number column and the two ``batch`` ERP columns, sampled and
    probed by the engine's own code over the view schemas."""
    import agents.db as db
    mes = workshop.LIVE_MES_SYSTEM
    monkeypatch.setattr(db, "pg", support.routed_pg({"erp": erp, "seed": erp}))
    run = support.sampled(mes["id"], mes["schema"], *_RUN, "batch")
    targets = [support.sampled("erp", _ERP_VIEW_SCHEMA, *col, "batch") for col in (_SHIP, _LOT)]
    probe = support.engine_probe(monkeypatch, {mes["id"]: mes["schema"], "erp": _ERP_VIEW_SCHEMA})
    return run, targets, probe


def test_the_exact_agreement_pass_derives_the_hop(story_db, monkeypatch):
    """ADR-0087: the MES run number -> ERP shipment line is DERIVED from the
    seeded values (same concept, same shape, every history run shipped), and
    the work-order-number ``lot_mes_tracking.batch_id`` does NOT qualify."""
    from agents import identity_affinity as aff
    erp, plan = story_db
    run, targets, probe = _hop_fields(erp, monkeypatch)
    rules = aff.find_affinity_rules([run], targets, probe=probe)
    assert [(r["target_field"], r["method"]) for r in rules] == [
        (".".join(_SHIP), aff.EXACT_METHOD)]
    assert rules[0]["shared_count"] == len(plan.runs) + 1
    lot = targets[1]
    assert aff.exact_rule(run, lot, probe) is None
    assert aff.dominant_shape(lot.values) != aff.dominant_shape(run.values)
