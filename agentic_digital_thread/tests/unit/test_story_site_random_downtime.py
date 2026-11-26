# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MES simulator's RANDOM downtime never plays the story mill's failure.

The story mill's bearing failure has one source per story: the condition
monitor raises its ``VIB-CMX-<loop>`` order at the BPFO alarm, and the story
history writes the failed batch's bearing downtime. A random downtime draw
follows no clock. When it picked the story's own reason at the
condition-monitor site, the MES-downtime bridge opened a priority-1
emergency bearing order on the story mill whenever the dice fell: on a
2026-10-14 workshop rehearsal an ``IND-<hex>`` "VIB-BEARING on LINE3"
order was open from 11:56Z, two hours before the story's alarm, and every
answer from module 4 on cited it.

The bridge picks the site's CMMS asset by reason, not by line, so a draw on
any line of that site reaches the story mill the same way. Pinned against the
simulator's own downtime step (``simulate_downtime_events``, with only the
probability gate, the time window, the row insert and the bridge stubbed),
and the bridge's own writer answered from the site CMMS seed's asset rows:

* at the condition-monitor site, on every line, the reason draw never offers
  the story's own downtime reason;
* no reason it can offer there is bridged onto the story mill's CMMS asset or
  given one of the story mill's failure codes;
* every category still has a reason there, and every other site draws from
  its full list, unchanged.

Offline: no sockets, no database, no AWS.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import mes_simulator as simulator_module  # noqa: E402
from erp_simulation import story_history as sh  # noqa: E402
from erp_simulation.mes_simulator import MESSimulator  # noqa: E402

from . import _site_cmms_rows as rows  # noqa: E402

STORY_SITE = MESSimulator.CONDITION_MONITOR_SITE
STORY_ASSET = cm.STORY_CMMS_ASSET_NUM
#: The story's own MES downtime reason (the failed batch's bearing stop).
STORY_REASON = sh.plan(rows.ANCHOR).downtime.reason
#: The failure codes the site CMMS seed records on the story mill, plus the
#: condition monitor's own.
STORY_FAILURE_CODES = frozenset(
    {wo["failure_code"] for wo in rows.seed_rows("work_orders")
     if wo["asset_num"] == STORY_ASSET and wo["failure_code"]}
    | {rows.condition_order()["failure_code"]})
SEED_ASSETS = rows.seed_rows("assets")


@pytest.fixture()
def sim(monkeypatch):
    """MESSimulator with config loading stubbed out (no Secrets Manager)."""
    monkeypatch.setattr(simulator_module, "get_config", lambda *a, **k: object())
    for var in ("SEED_DB_SECRET_NAME", "SEED_DB_HOST"):
        monkeypatch.delenv(var, raising=False)
    return MESSimulator()


class _ErpCursor:
    """The ERP cursor of the downtime step: the running-work-order SELECT is
    answered with exactly the columns that statement names."""

    rowcount = 0

    def __init__(self, running: dict):
        self._running, self._sql = running, ""

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchall(self):
        head = self._sql.split("SELECT", 1)[1].split("FROM", 1)[0]
        return [tuple(self._running[c.strip().split(".")[-1]] for c in head.split(","))]

    def close(self):
        pass


def _draw(sim, monkeypatch, site: str, line: str, category: str) -> SimpleNamespace:
    """One random downtime on a running work order at ``site`` / ``line``
    whose category draw lands on ``category``. Returns the reason lists the
    reason draw chose from (``offered``), the reasons written to the MES
    (``inserted``) and the ``(category, reason)`` pairs handed to the bridge
    (``bridged``)."""
    out = SimpleNamespace(offered=[], inserted=[], bridged=[])

    def choice(seq):
        seq = list(seq)
        if seq == list(sim.downtime_categories):
            return category
        out.offered.append(seq)
        return seq[0]

    def insert(cursor, work_order_id, line_id, category_, reason, *rest):
        out.inserted.append(reason)
        return uuid.uuid4()

    monkeypatch.setattr(simulator_module, "random", SimpleNamespace(choice=choice))
    monkeypatch.setattr(sim, "_condition_monitor_for_cycle", lambda cycle_id: 0)
    monkeypatch.setattr(sim, "_downtime_gates_pass", lambda *a: True)
    monkeypatch.setattr(sim, "_build_downtime_window",
                        lambda cursor, start, category=None:
                        (start, start + timedelta(seconds=600), 600))
    monkeypatch.setattr(sim, "_insert_downtime_event", insert)
    monkeypatch.setattr(sim, "_create_cmms_work_order_for_downtime",
                        lambda cursor, wo, line_id, category_, reason, downtime_id, **_started:
                        out.bridged.append((category_, reason)))
    cursor = _ErpCursor({"work_order_id": "wo", "work_order_number": "WO-1", "line": line,
                         "actual_start_time": datetime(2026, 10, 14, 11, 0), "site": site})
    result = sim.simulate_downtime_events(SimpleNamespace(cursor=lambda: cursor), "cycle")
    assert result["downtime_events_created"] == 1, (site, line, category, result)
    assert len(out.offered) == 1 and out.inserted == [out.offered[0][0]]
    return out


def test_the_story_line_is_a_line_the_draw_runs_on(sim):
    assert cm.STORY_MES_LINE in sim.production_lines
    assert any(STORY_REASON in reasons for reasons in sim.downtime_reasons.values()), (
        "the story's downtime reason is a simulator reason, so the draw could pick it")


def test_the_random_draw_never_offers_the_story_downtime_at_the_story_site(sim, monkeypatch):
    for line in sim.production_lines:
        for category in sim.downtime_categories:
            out = _draw(sim, monkeypatch, STORY_SITE, line, category)
            assert out.offered[0], f"{category} has no reason left on {line}"
            assert STORY_REASON not in out.offered[0], (
                f"a random {category} downtime on {line} can draw the story's own "
                f"{STORY_REASON}: {out.offered[0]}")


def test_no_reason_the_story_site_can_draw_is_bridged_onto_the_story_mill(sim, monkeypatch):
    bridged_categories = set()
    for line in sim.production_lines:
        for category in sim.downtime_categories:
            out = _draw(sim, monkeypatch, STORY_SITE, line, category)
            if not out.bridged:
                continue                  # a category the bridge raises no order for
            bridged_categories.add(category)
            for reason in out.offered[0]:
                order = rows.bridge_order(SEED_ASSETS, category, reason, str(uuid.uuid4()))
                assert order["asset_num"] != STORY_ASSET, (
                    f"a random {category}/{reason} downtime on {line} opens a priority-"
                    f"{order['priority']} {order['work_type']} order on the story mill")
                assert order["failure_code"] not in STORY_FAILURE_CODES, (line, category, reason, order)
    assert bridged_categories, "the bridge raised no order for any category: nothing was checked"


def test_every_other_site_draws_from_its_full_list(sim, monkeypatch):
    others = sorted(set(MESSimulator.CMMS_SITE_PREFIX) - {STORY_SITE})
    assert others
    for site in others:
        for category in sim.downtime_categories:
            out = _draw(sim, monkeypatch, site, cm.STORY_MES_LINE, category)
            assert out.offered == [sim.downtime_reasons[category]], (site, category)
