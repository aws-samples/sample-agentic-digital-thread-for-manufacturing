# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Random MES downtime has a realistic length, recorded to the second.

The draw used to be a whole number of minutes uniform between the floor and
the cap (5 to 30). A rehearsal account held, after a few weeks of cycles, 37
events of exactly 30 minutes, 29 of 29, 32 of 28 and 32 of 27, so the Daily
Briefing's "top 5 unplanned downtime events" was always a tie at the cap and
its answer hung on a tie-break (2026-10-17). Each category now draws from its
own right-skewed (lognormal) shape, truncated to the configured floor and cap
(5 and 60 minutes as seeded), in whole seconds:

* every stop starts and ends on a whole second, and its resolution note
  states the same length the timestamps give;
* at the seeded config, the longest stops are rare and rarely tie, and their
  two-decimal stored lengths separate them exactly as their seconds do;
* most stops are short and a few run close to an hour, never below the
  floor or above the cap;
* mechanical and electrical stops run longer and vary more than changeovers
  and operator stops;
* a narrower configured range (the Equipment Down scenario's) holds every
  draw without piling lengths up on a bound;
* the draw is reproducible along one seeded RNG and has exact, pinned values
  at fixed points of its range;
* the simulator's fallback, the db-init seed, the existing-account migration
  and the Normal Operations scenario agree on the floor and the cap.

Driven through the simulator's own downtime step
(``_maybe_create_downtime_event``) with the module RNG seeded and restored;
the database is a stub cursor answering simulation_config from the db-init
seed rows. Offline: no sockets, no database, no AWS.
"""
from __future__ import annotations

import ast
import math
import random
import statistics
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from erp_simulation import mes_simulator
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.scenarios import SCENARIOS

_HANDLER = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"


def _handler_constant(name):
    tree = ast.parse(_HANDLER.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in lambda-db-init handler")


def _seeded_config() -> dict:
    """The simulation_config values a fresh account is seeded with."""
    return {row[0]: row[1] for row in _handler_constant("SIMULATION_CONFIG_ROWS")}


def _stored(seconds: int) -> Decimal:
    """mes_downtime_events.duration_minutes for a stop of ``seconds``: the
    generated column's minutes, rounded half away from zero to two places
    (PostgreSQL's numeric(8,2) assignment)."""
    return (Decimal(seconds) / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class _DowntimeCursor:
    """The ERP cursor of the downtime step: simulation_config answered from
    ``config`` (a missing key has no row, so the simulator's own fallback
    applies), no active downtime on the line and none yet on the work order,
    and each inserted event recorded as ``(category, start, end, notes)``."""

    def __init__(self, config: dict):
        self._config, self._row = config, None
        self.rows = []

    def execute(self, sql, params=None):
        if "FROM simulation_config" in sql:
            value = self._config.get(params[0])
            self._row = None if value is None else (str(value),)
        elif "INSERT INTO mes_downtime_events" in sql:
            self.rows.append((params[2], params[4], params[5], params[7]))
            self._row = ("00000000-0000-0000-0000-000000000000",)
        elif "COUNT(*)" in sql:
            self._row = (0,)
        else:
            self._row = None

    def fetchone(self):
        return self._row


@pytest.fixture()
def sim(monkeypatch):
    """MESSimulator with config loading stubbed out (no Secrets Manager) and
    the MES-to-CMMS bridge a no-op."""
    monkeypatch.setattr(mes_simulator, "get_config", lambda *a, **k: object())
    simulator = MESSimulator()
    monkeypatch.setattr(simulator, "_create_cmms_work_order_for_downtime", lambda *a, **k: None)
    return simulator


def _rows(sim, n: int, seed: int, config: dict) -> list:
    """``n`` random downtimes through the simulator's own step, the module RNG
    seeded with ``seed`` and restored afterwards; ``(category, start, end,
    notes)`` as inserted. The work order started 3 min 20.5 s ago, so the
    stop's start inherits a sub-second part unless the step drops it."""
    cursor = _DowntimeCursor({**config, "mes_downtime_probability": "1.0"})
    started = datetime.now().replace(microsecond=500_000) - timedelta(minutes=3, seconds=20)
    state = random.getstate()
    random.seed(seed)
    try:
        for _ in range(n):
            assert sim._maybe_create_downtime_event(cursor, "wo-1", "WO-1", "LINE1", started, "SITE") == 1
    finally:
        random.setstate(state)
    return cursor.rows


def _draw(sim, n: int, seed: int, config: dict) -> list:
    """``(category, seconds)`` of ``n`` random downtimes (see :func:`_rows`)."""
    return [(category, (end - start).total_seconds()) for category, start, end, _notes in
            _rows(sim, n, seed, config)]


def _seconds(events) -> list:
    return [seconds for _category, seconds in events]


def test_each_stop_is_recorded_to_the_second(sim):
    """Whole-second timestamps (SiteWise and other readers reject sub-second
    dates), a whole-second length, and a resolution note that states that
    length in minutes and seconds. Most lengths are not whole minutes."""
    rows = _rows(sim, 500, 7, _seeded_config())
    for _category, start, end, notes in rows:
        assert start.microsecond == 0 and end.microsecond == 0, (start, end)
        seconds = (end - start).total_seconds()
        assert seconds == int(seconds)
        minutes, rest = divmod(int(seconds), 60)
        assert notes == (f"Resolved after {minutes} min {rest} s" if rest else
                         f"Resolved after {minutes} min"), (seconds, notes)
    whole_minutes = sum((end - start).total_seconds() % 60 == 0 for _c, start, end, _n in rows)
    assert whole_minutes / len(rows) < 0.05, whole_minutes


def test_the_longest_stops_rarely_tie_at_the_seeded_config(sim):
    """Ten accounts' worth of 2,000 stops each: the five longest stored
    lengths are five different values, and the fifth is not tied with the
    sixth, on nearly every one. The old draw tied all five at the 30-minute
    cap every time. The longest stops run close to the hour, never past it."""
    config = _seeded_config()
    inside, boundary, tops = 0, 0, []
    for seed in range(10):
        top = sorted((_stored(s) for s in _seconds(_draw(sim, 2000, seed, config))), reverse=True)[:6]
        tops.append(top[:5])
        inside += len(set(top[:5])) < 5
        boundary += top[4] == top[5]
    assert inside <= 1 and boundary == 0, (inside, boundary, tops)
    assert all(Decimal(45) <= top[0] <= Decimal(60) for top in tops), tops


def test_two_decimal_lengths_separate_and_order_stops_as_their_seconds_do(sim):
    """The Daily Briefing may rank by ``duration_minutes`` or by ``end_time -
    start_time``: either order is the same, and two stops share a stored
    length only when they last the same number of seconds."""
    seconds = _seconds(_draw(sim, 2000, 21, _seeded_config()))
    stored = [_stored(s) for s in seconds]
    assert len(set(stored)) == len(set(seconds)) >= 800, (len(set(stored)), len(set(seconds)))
    by_stored = sorted(range(len(seconds)), key=lambda i: (stored[i], i), reverse=True)
    by_seconds = sorted(range(len(seconds)), key=lambda i: (seconds[i], i), reverse=True)
    assert by_stored == by_seconds


def test_most_stops_are_short_and_a_few_run_close_to_an_hour(sim):
    minutes = [s / 60 for s in _seconds(_draw(sim, 4000, 11, _seeded_config()))]
    p90 = statistics.quantiles(minutes, n=10)[-1]
    over_45 = sum(m > 45 for m in minutes) / len(minutes)
    assert statistics.median(minutes) <= 15, statistics.median(minutes)
    assert p90 <= 30, p90
    assert 0.003 <= over_45 <= 0.04, over_45
    assert 50 <= max(minutes) <= 60, max(minutes)
    # Never shorter than the floor: a stop still outlasts a deployed work
    # order (2 to 4 minutes), so its availability stays what it was.
    assert min(minutes) >= 5


def test_breakdowns_run_longer_than_changeovers_and_operator_stops(sim):
    by_category = {}
    for category, seconds in _draw(sim, 6000, 12, _seeded_config()):
        by_category.setdefault(category, []).append(seconds / 60)
    assert set(by_category) == set(sim.downtime_categories)
    deciles = {c: statistics.quantiles(m, n=10) for c, m in by_category.items()}
    p10 = {c: q[0] for c, q in deciles.items()}
    p90 = {c: q[-1] for c, q in deciles.items()}
    for long in ("MECHANICAL", "ELECTRICAL"):
        for short in ("CHANGEOVER", "OPERATOR"):
            assert p90[long] > 1.5 * p90[short], p90
        assert p90[long] / p10[long] >= 2.8, (long, p10, p90)
        assert max(by_category[long]) >= 45, (long, max(by_category[long]))
    # Changeovers and operator stops are short and regular.
    for short in ("CHANGEOVER", "OPERATOR"):
        assert p90[short] / p10[short] <= 2.0, (short, p10, p90)
    assert p90["OPERATOR"] <= 15 and p90["CHANGEOVER"] <= 20, p90
    assert p90["MECHANICAL"] > p90["QUALITY_ISSUE"], p90


def test_a_narrow_configured_range_holds_every_draw_without_piling_on_a_bound(sim):
    """The Equipment Down scenario's 30 to 120 minutes: every stop inside it,
    skewed to the short end, and no length crowding the cap."""
    scenario = SCENARIOS["equipment_down"]["config"]
    lo, hi = scenario["mes_downtime_duration_min"], scenario["mes_downtime_duration_max"]
    seconds = _seconds(_draw(sim, 4000, 13, {**_seeded_config(), **scenario}))
    assert lo * 60 <= min(seconds) and max(seconds) <= hi * 60
    assert statistics.median(seconds) / 60 < (lo + hi) / 2 - 15, statistics.median(seconds)
    by_minute = Counter(int(s // 60) for s in seconds)
    assert sum(s >= (hi - 1) * 60 for s in seconds) / len(seconds) < 0.005
    assert sum(by_minute[m] for m in range(hi - 10, hi + 1)) < sum(by_minute[m] for m in range(lo, lo + 10))


def test_the_draw_is_reproducible_along_one_rng():
    """Two RNGs seeded alike give the same advancing sequence of lengths; a
    different seed gives a different one."""
    draw = mes_simulator.draw_downtime_seconds
    first, second, other = random.Random(3), random.Random(3), random.Random(4)
    sequence = [draw("MECHANICAL", 5, 60, first) for _ in range(20)]
    assert sequence == [draw("MECHANICAL", 5, 60, second) for _ in range(20)]
    assert len(set(sequence)) > 15, sequence
    assert sequence != [draw("MECHANICAL", 5, 60, other) for _ in range(20)]
    assert all(300 <= s <= 3600 and s == int(s) for s in sequence)


class _FixedRng:
    """An RNG whose ``random()`` answers ``values`` in turn."""

    def __init__(self, *values):
        self._values, self.calls = list(values), 0

    def random(self):
        self.calls += 1
        return self._values.pop(0)


_NEAR_ONE = math.nextafter(1.0, 0.0)   # the largest value random.random() returns


@pytest.mark.parametrize("category, floor, cap, expected", [
    # A normal range: u = 0 is the floor, the interior follows the truncated
    # lognormal (median 18 min: 1078 s at u = 0.5), near 1 is the cap.
    ("MECHANICAL", 5, 60, {0.0: 300, 0.25: 773, 0.5: 1078, _NEAR_ONE: 3600}),
    # A reversed range is read the other way round (20 to 60 minutes).
    ("ELECTRICAL", 60, 20, {0.0: 1200, 0.25: 1340, 0.5: 1535, _NEAR_ONE: 3600}),
    # A one-length range gives that length whatever the draw.
    ("CHANGEOVER", 7, 7, {0.0: 420, 0.25: 420, 0.5: 420, _NEAR_ONE: 420}),
    # Ranges so far into a tail that the float CDF has no mass left there
    # give the bound nearer the shape: the floor above it, the cap below it.
    ("OPERATOR", 100, 120, {0.0: 6000, 0.25: 6000, 0.5: 6000, _NEAR_ONE: 6000}),
    ("CHANGEOVER", 1, 2, {0.0: 120, 0.25: 120, 0.5: 120, _NEAR_ONE: 120}),
])
def test_the_draw_at_fixed_points_of_its_range(category, floor, cap, expected):
    draw = mes_simulator.draw_downtime_seconds
    for u, seconds in expected.items():
        rng = _FixedRng(u)
        assert draw(category, floor, cap, rng) == seconds, (category, floor, cap, u)
        assert rng.calls == 1


def test_one_duration_floor_and_cap_everywhere():
    """The simulator's fallback, the db-init seed row (value and default, inside
    its unchanged slider bounds), the existing-account migration and the
    Normal Operations scenario agree: 5 to 60 minutes."""
    expected = {"mes_downtime_duration_min": mes_simulator.DEFAULT_DOWNTIME_DURATION_MIN,
                "mes_downtime_duration_max": mes_simulator.DEFAULT_DOWNTIME_DURATION_MAX}
    assert expected == {"mes_downtime_duration_min": 5, "mes_downtime_duration_max": 60}
    rows = {row[0]: row for row in _handler_constant("SIMULATION_CONFIG_ROWS")}
    moved = {key: new for key, _old, new in _handler_constant("MOVED_CONFIG_DEFAULTS")}
    normal = SCENARIOS["normal"]["config"]
    for key, value in expected.items():
        assert int(rows[key][1]) == int(rows[key][7]) == value
        assert (rows[key][5], rows[key][6]) == (1, 120)       # the dashboard slider's bounds
        assert normal[key] == value
    assert int(moved["mes_downtime_duration_max"]) == expected["mes_downtime_duration_max"]
    equipment_down = SCENARIOS["equipment_down"]["config"]
    assert (equipment_down["mes_downtime_duration_min"], equipment_down["mes_downtime_duration_max"]) == (30, 120)
