# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Routine MES quality checks use the check types of the site the work order runs at.

Every site used to draw from one bottling list, so machined shafts at the
machining site carried FILL_LEVEL and CAP_TORQUE checks in the workshop's
first answer (2026-10-03). Sites now map to their own list, DIMENSIONAL stays
out of every routine list (it is the machining site's defect signal), and
every routine type is one the mes_quality_checks constraint allows.

Each check's rejects are a binomial draw at the configured pass rate, whose
default (0.998-1.0) lets most checks pass clean (2026-10-04).
"""
import ast
import random
from datetime import datetime, timedelta
from pathlib import Path

from erp_simulation.mes_simulator import (
    DEFAULT_QUALITY_PASS_RATE_MAX, DEFAULT_QUALITY_PASS_RATE_MIN, MESSimulator, sampled_rejects,
)
from erp_simulation.scenarios import SCENARIOS

_HANDLER = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"
_STORY_SITE = MESSimulator.CONDITION_MONITOR_SITE


class _Cursor:
    """Answers the running-work-order query, the per-work-order count and
    latest check time, and config lookups; records quality-check inserts.
    ``stateful`` reports the checks inserted so far (so repeated cycles see
    their own rows); otherwise every order has none yet, which lets the bulk
    tests reuse one work-order tuple many times."""

    def __init__(self, work_orders, stateful=False):
        self._work_orders = work_orders
        self._stateful = stateful
        self._result = []
        self.inserts = []

    def execute(self, sql, params=None):
        if "INSERT INTO mes_quality_checks" in sql:
            self.inserts.append(params)
            self._result = []
        elif "FROM mes_work_orders mwo" in sql:
            self._result = self._work_orders
        elif "COUNT(*)" in sql:
            times = [p[1] for p in self.inserts if self._stateful and p[0] == params[0]]
            self._result = [(len(times), max(times) if times else None)]
        else:                                   # simulation_config lookups
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


def _sim():
    return MESSimulator()


def _run(work_orders):
    cursor = _Cursor(work_orders)
    conn = type("C", (), {"cursor": lambda self: cursor})()
    result = _sim().simulate_quality_checks(conn, "c-1")
    return result, cursor


def _work_order(site, hours=5):
    start = datetime.now() - timedelta(hours=hours)
    return ("wo-1", "WO-20261003-00001", "LINE1", start, 100, site)


def _db_init_check_types():
    tree = ast.parse(_HANDLER.read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "MES_QUALITY_CHECK_TYPES" for t in node.targets)):
            return set(ast.literal_eval(node.value))
    raise AssertionError("MES_QUALITY_CHECK_TYPES not found in lambda-db-init handler")


def test_story_site_checks_are_never_bottling_checks():
    result, cursor = _run([_work_order(_STORY_SITE)])
    types = {params[2] for params in cursor.inserts}
    assert result["quality_checks_created"] == len(cursor.inserts) == 10
    assert types <= set(MESSimulator.SITE_QUALITY_CHECK_TYPES[_STORY_SITE])
    assert not types & {"FILL_LEVEL", "CAP_TORQUE", "LABEL_PLACEMENT"}


def test_a_site_without_its_own_list_keeps_the_default():
    sim = _sim()
    assert sim.quality_check_types_for("SOME_OTHER_SITE") == tuple(sim.quality_check_types)
    assert sim.quality_check_types_for(None) == tuple(sim.quality_check_types)


def test_dimensional_stays_out_of_every_routine_list():
    sim = _sim()
    lists = [sim.quality_check_types, *MESSimulator.SITE_QUALITY_CHECK_TYPES.values()]
    assert all("DIMENSIONAL" not in types for types in lists)


def test_every_routine_type_is_allowed_by_the_check_constraint():
    allowed = _db_init_check_types()
    sim = _sim()
    for types in [sim.quality_check_types, *MESSimulator.SITE_QUALITY_CHECK_TYPES.values()]:
        assert set(types) <= allowed, set(types) - allowed


def test_checks_due_are_created_once_per_half_hour_of_running():
    result, cursor = _run([_work_order(_STORY_SITE, hours=2)])
    assert result["quality_checks_created"] == 4
    times = [params[1] for params in cursor.inserts]
    assert times == sorted(times)
    assert all(params[3] == params[4] + params[5] for params in cursor.inserts)


def test_no_check_is_dated_after_it_was_recorded():
    """A deployed work order runs 2-4 minutes and gets one check. It used to
    be stamped start + 30 min, about 27 minutes in the future (2026-10-04)."""
    before = datetime.now()
    _, short = _run([_work_order(_STORY_SITE, hours=0.05)])
    _, long_ = _run([_work_order(_STORY_SITE, hours=2.2)])
    after = datetime.now()
    (only,) = short.inserts
    assert before <= only[1] <= after
    times = [params[1] for params in long_.inserts]
    assert len(times) == 4 and max(times) <= after
    assert {b - a for a, b in zip(times, times[1:])} == {timedelta(minutes=30)}


def test_an_early_first_check_keeps_the_half_hour_cadence(monkeypatch):
    """A work order seen 3 minutes in gets its first check then; later cycles
    add one 30 minutes after the latest check, never skipping a slot and
    never dating one after the cycle that wrote it."""
    import erp_simulation.mes_simulator as mes

    start = datetime(2026, 10, 5, 8, 0, 0)
    clock = {"now": start}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(mes, "datetime", _Clock)
    cursor = _Cursor([("wo-1", "WO-1", "LINE1", start, 100, _STORY_SITE)], stateful=True)
    conn = type("C", (), {"cursor": lambda self: cursor})()
    for minutes in (3, 20, 34, 40, 64, 125):
        clock["now"] = start + timedelta(minutes=minutes)
        _sim().simulate_quality_checks(conn, "c-1")
        assert all(p[1] <= clock["now"] for p in cursor.inserts)
    stamps = [int((p[1] - start).total_seconds() // 60) for p in cursor.inserts]
    assert stamps == [3, 33, 63, 93, 123]


# ---------------------------------------------------------------------------
# Check outcomes: rejects are a binomial draw at the configured pass rate.
# The old ``sample - int(sample * rate)`` put a reject on almost every check,
# so the workshop's first answer called 7 of 8 shipped batches failed.
# ---------------------------------------------------------------------------

class _ConfigCursor(_Cursor):
    """_Cursor that also answers simulation_config lookups from ``config``."""

    def __init__(self, work_orders, config):
        super().__init__(work_orders)
        self._config = config

    def execute(self, sql, params=None):
        if "FROM simulation_config" in sql:
            value = self._config.get(params[0])
            self._result = [(str(value),)] if value is not None else []
        else:
            super().execute(sql, params)


def _outcomes(config, work_orders=60, seed=20261004):
    """(sample, pass, fail) of every check for ``work_orders`` 5-hour orders
    (10 checks each) at the machining site, with the global RNG seeded."""
    state = random.getstate()
    random.seed(seed)
    try:
        cursor = _ConfigCursor([_work_order(_STORY_SITE)] * work_orders, config)
        conn = type("C", (), {"cursor": lambda self: cursor})()
        _sim().simulate_quality_checks(conn, "c-1")
    finally:
        random.setstate(state)
    return [(p[3], p[4], p[5]) for p in cursor.inserts]


def test_sampled_rejects_is_bounded_by_the_sample():
    rng = random.Random(1)
    assert sampled_rejects(30, 0.0, rng) == 0
    assert sampled_rejects(30, 1.0, rng) == 30
    assert sampled_rejects(30, -0.5, rng) == 0
    assert sampled_rejects(30, 1.5, rng) == 30
    assert sampled_rejects(0, 0.5, rng) == 0


def test_a_check_at_a_high_pass_rate_can_pass_clean():
    # int(20 * 0.99) == 19: the truncation put one reject on EVERY such check.
    rng = random.Random(7)
    rejects = [sampled_rejects(20, 0.01, rng) for _ in range(4000)]
    assert 0.78 < rejects.count(0) / len(rejects) < 0.86      # 0.99 ** 20 == 0.818


def test_most_routine_checks_pass_clean_at_the_default_pass_rate():
    checks = _outcomes({})
    assert len(checks) == 600
    assert all(sample == passed + failed for sample, passed, failed in checks)
    clean = sum(1 for _, _, failed in checks if failed == 0) / len(checks)
    assert clean >= 0.90, clean
    assert sum(p for _, p, _ in checks) / sum(s for s, _, _ in checks) >= 0.997


def test_most_deployed_batches_carry_no_routine_reject():
    """Deployed work orders run 2-4 minutes, so each gets ONE check: the
    share of batches with a routine reject is the per-check share (~3-4 %)."""
    state = random.getstate()
    random.seed(4)
    try:
        cursor = _ConfigCursor([_work_order(_STORY_SITE, hours=0.05)] * 2000, {})
        conn = type("C", (), {"cursor": lambda self: cursor})()
        _sim().simulate_quality_checks(conn, "c-1")
    finally:
        random.setstate(state)
    assert len(cursor.inserts) == 2000                         # one check per batch
    with_reject = sum(1 for p in cursor.inserts if p[5] > 0) / 2000
    assert 0.01 <= with_reject <= 0.07, with_reject

def test_the_quality_issues_scenario_still_fails_most_checks():
    issues = SCENARIOS["quality_issues"]["config"]
    checks = _outcomes(issues)
    failing = sum(1 for _, _, failed in checks if failed > 0) / len(checks)
    rate = sum(p for _, p, _ in checks) / sum(s for s, _, _ in checks)
    assert failing >= 0.99, failing
    assert issues["mes_quality_pass_rate_min"] - 0.03 <= rate <= issues["mes_quality_pass_rate_max"] + 0.03


def _db_init_config_rows():
    tree = ast.parse(_HANDLER.read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "SIMULATION_CONFIG_ROWS" for t in node.targets)):
            return {row[0]: row for row in ast.literal_eval(node.value)}
    raise AssertionError("SIMULATION_CONFIG_ROWS not found in lambda-db-init handler")


def _db_init_moved_defaults():
    tree = ast.parse(_HANDLER.read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "MOVED_CONFIG_DEFAULTS" for t in node.targets)):
            return {key: new for key, _old, new in ast.literal_eval(node.value)}
    raise AssertionError("MOVED_CONFIG_DEFAULTS not found in lambda-db-init handler")


def test_one_pass_rate_default_everywhere():
    """The simulator's fallback, the db-init seed row (value and default), the
    existing-account migration and the Normal Operations scenario agree."""
    expected = {"mes_quality_pass_rate_min": DEFAULT_QUALITY_PASS_RATE_MIN,
                "mes_quality_pass_rate_max": DEFAULT_QUALITY_PASS_RATE_MAX}
    rows, moved = _db_init_config_rows(), _db_init_moved_defaults()
    normal = SCENARIOS["normal"]["config"]
    for key, value in expected.items():
        assert float(rows[key][1]) == float(rows[key][7]) == value
        assert float(moved[key]) == value
        assert normal[key] == value
