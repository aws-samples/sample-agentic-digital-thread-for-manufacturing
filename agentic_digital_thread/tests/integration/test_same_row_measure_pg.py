# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The b5 same-row 1:1 measurement on the real workshop plant CMMS seed.

Provision step b5 (ADR-0041 D1) writes a same-row JoinPath for two columns of
one table that map to the same concept only when the table's rows read 1:1
(``onboard._dp_samerow_measure_table`` + ``_samerow_reads_one_to_one``).
This runs the production SQL on PostgreSQL against the plant CMMS that
``lambda-adt-seed/011_*.sql`` seeds, with and without the live-story history a
workshop account writes:

* ``assets.asset_num`` / ``serial_number`` (two names for one asset) read 1:1;
* ``work_orders.crew`` / ``reported_by`` (a crew code and a person, which the
  onboarding model can both tag ``operator``) do not;
* both are measured in ONE read of each table.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server. Run (from
``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=.:.. python -m pytest tests/integration/test_same_row_measure_pg.py -q
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ADT_ROOT, _ADT_ROOT.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import onboard  # noqa: E402
from tests.integration.story_pg_support import REPO, routed_pg, story_databases  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
#: The schema the seed file creates (read from it, not spelled here).
((_SEED_FILE,),) = [list((REPO / "lambda-adt-seed").glob("011_*.sql"))]
_SCHEMA = re.search(r"CREATE SCHEMA IF NOT EXISTS (\w+);",
                    _SEED_FILE.read_text(encoding="utf-8")).group(1)


@pytest.fixture(params=[False, True], ids=["frozen", "live-story"])
def cmms_pg(request, monkeypatch):
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    with story_databases(_DSN, "samerow_", with_history=request.param) as (conns, _plan):
        calls: list = []
        real = routed_pg(conns)

        def counting_pg(schema, sql, params=None):
            calls.append(sql)
            return real(schema, sql, params)
        monkeypatch.setattr(onboard, "pg", counting_pg)
        yield calls


def _ctx():
    return SimpleNamespace(schema=_SCHEMA)


def test_asset_number_and_serial_number_read_one_to_one(cmms_pg):
    status, stats = onboard._dp_samerow_measure_table(
        _ctx(), "assets", [("asset_num", "serial_number")])
    assert status == "ok"
    distinct_a, distinct_b, pairs = stats[("asset_num", "serial_number")]
    assert distinct_a == distinct_b == pairs >= onboard.SAMEROW_MIN_DISTINCT_PAIRS
    assert onboard._samerow_reads_one_to_one(stats[("asset_num", "serial_number")])


def test_crew_and_reported_by_do_not(cmms_pg):
    status, stats = onboard._dp_samerow_measure_table(
        _ctx(), "work_orders", [("crew", "reported_by")])
    assert status == "ok"
    distinct_crew, distinct_reporter, pairs = stats[("crew", "reported_by")]
    assert pairs >= onboard.SAMEROW_MIN_DISTINCT_PAIRS
    assert not (distinct_crew == distinct_reporter == pairs)
    assert not onboard._samerow_reads_one_to_one(stats[("crew", "reported_by")])


def test_several_pairs_are_one_read(cmms_pg):
    pairs = [("asset_num", "crew"), ("asset_num", "reported_by"), ("crew", "reported_by")]
    status, stats = onboard._dp_samerow_measure_table(_ctx(), "work_orders", pairs)
    assert status == "ok" and sorted(stats) == sorted(pairs)
    assert len(cmms_pg) == 1


def test_null_rows_are_left_out_of_each_pair(cmms_pg):
    # failure_code is NULL on the PM rows; only rows where both are set count.
    status, stats = onboard._dp_samerow_measure_table(
        _ctx(), "work_orders", [("failure_code", "wonum")])
    assert status == "ok"
    distinct_code, distinct_wo, pairs = stats[("failure_code", "wonum")]
    assert 0 < distinct_wo == pairs and distinct_code <= pairs


def test_missing_column_is_a_failed_read_not_a_link(cmms_pg):
    status, stats = onboard._dp_samerow_measure_table(
        _ctx(), "work_orders", [("crew", "no_such_column")])
    assert (status, stats) == ("failed", {})


def test_a_table_over_the_limit_is_too_large(cmms_pg, monkeypatch):
    monkeypatch.setattr(onboard, "SAMEROW_MEASURE_ROW_LIMIT", 3)
    status, stats = onboard._dp_samerow_measure_table(
        _ctx(), "assets", [("asset_num", "serial_number")])
    assert (status, stats) == ("too_large", {})
