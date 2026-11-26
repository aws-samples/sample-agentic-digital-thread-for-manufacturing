# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the story mill's bearing-condition model (ADR-0073).

``erp_simulation/condition_monitor.py`` is the ONE curve two consumers read:
the OPC UA dataset generator (tags) and the simulator's condition monitor
(CMMS work order). These tests pin what makes the trigger honest:

* the loop geometry is the generator's (same steps, same fixed downtime);
* the BPFO band alarm fires strictly BEFORE the bearing stops (leading
  indicator) and never while the spindle is stopped or after the repair;
* every feature is monotone non-decreasing while the fault develops;
* both sides derive the loop step from the wall clock the same way, and
  the OPC UA server plays wall-clock aligned (no +1 drift, no restart at 0);
* the work-order description fits the CMMS column and names the band;
* the anchored story clock: anchor 0 is exactly the wall clock, a positive
  anchor shifts the loop to start at the anchor, and the phase / defect
  helpers agree with the band alarm.

Fully offline: pure functions plus one source-file read.
"""

import ast
import importlib.util
import math
import os
import random
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation.tests._loop_env import run_under_loop  # noqa: E402

_GENERATOR_PATH = _REPO_ROOT / "highbyte" / "generate_datasets.py"
_SERVER_PATH = _REPO_ROOT / "highbyte" / "components" / "opc-server" / "server.mjs"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_datasets_cm_under_test", str(_GENERATOR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# One curve, two consumers
# ---------------------------------------------------------------------------

def test_loop_geometry_is_the_generators():
    gen = _load_generator()
    assert (gen.INTERVAL_SEC, gen.TOTAL_SECONDS, gen.NUM_STEPS) == (cm.INTERVAL_SEC, cm.TOTAL_SECONDS, cm.NUM_STEPS)
    assert gen.CMX441_DOWNTIME == cm.DOWNTIME == (cm.BEARING_FAIL_STEP, cm.BEARING_REPAIR_STEPS)
    assert (gen.VIB_HEALTHY_MM_S, gen.VIB_ALARM_MM_S) == (cm.VIB_HEALTHY_MM_S, cm.VIB_ALARM_MM_S)
    assert gen.CMX441_CMMS_ASSET_NUM == cm.STORY_CMMS_ASSET_NUM
    assert gen.CMX441_ASSET_NAME == cm.STORY_SITEWISE_ASSET
    assert gen.CMX441_MES_EQUIPMENT_ID == cm.STORY_MES_EQUIPMENT_ID


def test_generator_trend_is_the_shared_trend():
    gen = _load_generator()
    for step in (0, 1, cm.BPFO_ALARM_STEP, cm.BEARING_FAIL_STEP - 1, cm.BEARING_FAIL_STEP, cm.NUM_STEPS - 1):
        assert gen.cmx441_trend(step, 1.0, 2.8) == cm.trend(step, 1.0, 2.8)
        assert gen.cmx441_spectral(step) == cm.spectral_features(step)


def test_model_is_dependency_free():
    """The generator imports this from outside the package; it must not drag
    in the package's DB/config modules. ``os`` is there for the ONE module-
    level read of STORY_LOOP_SECONDS (ADR-0091) and nothing else."""
    src = (_REPO_ROOT / "erp_simulation" / "condition_monitor.py").read_text(encoding="utf-8")
    body = src.split("from __future__ import annotations", 1)[1]
    imports = [ln.strip() for ln in body.splitlines() if ln.startswith(("import ", "from "))]
    assert imports == ["import math", "import os",
                       "from typing import Dict, NamedTuple, Optional, Tuple"], imports
    tree = ast.parse(src)
    os_uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "os"]
    assert len(os_uses) == 1, "os is used exactly once: the import-time loop read"
    reads = [n for n in tree.body if isinstance(n, ast.Assign) and "os.environ.get(LOOP_ENV)" in ast.unparse(n)]
    assert [ast.unparse(n.targets[0]) for n in reads] == ["TOTAL_SECONDS"], "module level, one target"


# ---------------------------------------------------------------------------
# Packaged loop length (ADR-0091): unset is byte-identical, bad values raise
# ---------------------------------------------------------------------------

# The shipped 8 h geometry, as literals: the byte-identical pin.
_SHIPPED = {"TOTAL_SECONDS": 28800, "NUM_STEPS": 5760, "BEARING_FAIL_STEP": 5184,
            "BPFO_ALARM_STEP": 3898, "BEARING_REPAIR_STEPS": 240, "ALARM_OFFSET_SECONDS": 19490,
            "ANCHOR_RESIDUE": 10, "TELEMETRY_GRID_SECONDS": 300, "INTERVAL_SEC": 5}


def _derived(loop):
    """The geometry recomputed from first principles (not via the module)."""
    steps = loop // 5
    fail = int(steps * 0.9)
    alarm = math.ceil(math.sqrt((0.70 - 0.05) / (1.20 - 0.05)) * fail)
    return {"TOTAL_SECONDS": loop, "NUM_STEPS": steps, "BEARING_FAIL_STEP": fail,
            "BPFO_ALARM_STEP": alarm, "BEARING_REPAIR_STEPS": min(240, steps // 24),
            "ALARM_OFFSET_SECONDS": alarm * 5,
            "ANCHOR_RESIDUE": (-alarm * 5) % 300, "TELEMETRY_GRID_SECONDS": 300, "INTERVAL_SEC": 5}


_DUMP = ("from erp_simulation import condition_monitor as cm\n"
         "emit({k: getattr(cm, k) for k in %r} | {'geometry': cm.story_geometry()})\n" % sorted(_SHIPPED))


def test_unset_loop_is_the_shipped_geometry():
    assert os.environ.get(cm.LOOP_ENV) is None, "the unit lane runs with STORY_LOOP_SECONDS unset"
    assert {k: getattr(cm, k) for k in _SHIPPED} == _SHIPPED == _derived(28800)
    assert cm.DOWNTIME == (5184, 240) and cm.LOOP_SECONDS_MULTIPLE == cm.TELEMETRY_GRID_SECONDS
    assert cm.story_geometry() == {"loop_seconds": 28800, "interval_seconds": 5, "bpfo_alarm_step": 3898,
                                   "fail_step": 5184, "repair_steps": 240}


@pytest.mark.parametrize("raw", [None, "", "  ", "28800", " 28800 "])
def test_default_spellings_are_byte_identical_in_a_fresh_import(raw):
    out = run_under_loop(raw, _DUMP)
    geometry = out.pop("geometry")
    assert out == _SHIPPED
    assert geometry == cm.story_geometry()


@pytest.mark.parametrize("raw", [None, "", "28800", "21600", "12300", "10800", "3600", "12000"])
def test_loop_seconds_from_accepts(raw):
    expected = 28800 if not raw else int(raw)
    assert cm.loop_seconds_from(raw) == expected


@pytest.mark.parametrize("raw", [
    "7000",      # not a multiple of 300
    "100", "0", "-300", "3300",  # below the range
    "28900", "29100", "43200", "86400", "86700",  # above the range (capped at 8 h)
    "abc", "10800.0", "1e4", "0x2A30", "+10800", "１０８００",
])
def test_loop_seconds_from_rejects(raw):
    with pytest.raises(ValueError):
        cm.loop_seconds_from(raw)


def test_the_loop_range_is_capped_at_the_shipped_eight_hours():
    """Above 8 h the history's failed batch can overlap a normal line-3 run
    (42600..51300 s and 75600..86400 s): the range stops at the default."""
    assert cm.LOOP_SECONDS_RANGE == (3600, 28800) and cm.LOOP_SECONDS_RANGE[1] == cm.DEFAULT_LOOP_SECONDS


def test_geometry_for_is_the_first_principles_geometry_at_every_valid_loop():
    keys = ("TOTAL_SECONDS", "NUM_STEPS", "BEARING_FAIL_STEP", "BEARING_REPAIR_STEPS",
            "BPFO_ALARM_STEP", "ALARM_OFFSET_SECONDS", "ANCHOR_RESIDUE")
    wrong = {loop: tuple(cm.geometry_for(loop)) for loop in range(3600, 28801, 300)
             if tuple(cm.geometry_for(loop)) != tuple(_derived(loop)[k] for k in keys)}
    assert not wrong, wrong
    assert cm.GEOMETRY == cm.geometry_for(28800)
    assert cm.bpfo_alarm_step() == cm.GEOMETRY.bpfo_alarm_step == cm.BPFO_ALARM_STEP


@pytest.mark.parametrize("raw", ["7000", "29100", "abc"])
def test_a_bad_loop_fails_the_import(raw):
    code, _out, err = run_under_loop(raw, "import erp_simulation.condition_monitor\n", expect_ok=False)
    assert code != 0 and "ValueError" in err and "STORY_LOOP_SECONDS" in err


@pytest.mark.parametrize("loop", [3600, 10800, 12000, 21600, 27000])
def test_a_packaged_loop_derives_every_constant(loop):
    out = run_under_loop(str(loop), _DUMP)
    geometry = out.pop("geometry")
    assert out == _derived(loop)
    assert geometry["loop_seconds"] == loop and geometry["fail_step"] == out["BEARING_FAIL_STEP"]
    assert out["BPFO_ALARM_STEP"] < out["BEARING_FAIL_STEP"]
    assert out["BEARING_FAIL_STEP"] + out["BEARING_REPAIR_STEPS"] < out["NUM_STEPS"]  # a healthy phase
    assert geometry["repair_steps"] == out["BEARING_REPAIR_STEPS"] <= 240


# The ADR-0091 3 h table, as literals.
_THREE_HOURS = {"TOTAL_SECONDS": 10800, "NUM_STEPS": 2160, "BEARING_FAIL_STEP": 1944,
                "BPFO_ALARM_STEP": 1462, "BEARING_REPAIR_STEPS": 90, "ALARM_OFFSET_SECONDS": 7310,
                "ANCHOR_RESIDUE": 190, "TELEMETRY_GRID_SECONDS": 300, "INTERVAL_SEC": 5}


def test_a_three_hour_loop_is_the_adr_table():
    out = run_under_loop("10800", _DUMP)
    out.pop("geometry")
    assert out == _THREE_HOURS == _derived(10800)
    # alarm window 2410 s, 90 steps healthy after a 450 s repair
    assert (1944 - 1462) * 5 == 2410 and 2160 - 1944 - 90 == 126


@pytest.mark.parametrize("loop", ["10800", "21600"])
def test_a_packaged_loop_keeps_the_alarm_on_the_grid(loop):
    code = ("import random\nfrom erp_simulation import condition_monitor as cm\n"
            "rng = random.Random(86)\nrows = []\n"
            "for _ in range(200):\n"
            "    a = cm.snap_anchor(rng.uniform(1.7e9, 1.9e9))\n"
            "    e = a + rng.uniform(-3, 9) * cm.TOTAL_SECONDS\n"
            "    rows.append([a % 300, cm.alarm_epoch(e, a) % 300,\n"
            "                 cm.loop_step(cm.story_seconds(cm.alarm_epoch(e, a), a)),\n"
            "                 (cm.fail_epoch(e, a) - cm.alarm_epoch(e, a))])\n"
            "emit({'rows': rows, 'residue': cm.ANCHOR_RESIDUE, 'alarm': cm.BPFO_ALARM_STEP,\n"
            "      'window': (cm.BEARING_FAIL_STEP - cm.BPFO_ALARM_STEP) * 5})\n")
    out = run_under_loop(loop, code)
    assert out["residue"] == _derived(int(loop))["ANCHOR_RESIDUE"]
    for residue, alarm_mod, step, gap in out["rows"]:
        assert residue == out["residue"] and alarm_mod == 0 and step == out["alarm"]
        assert gap == out["window"] and gap % 5 == 0


# ---------------------------------------------------------------------------
# Leading indicator
# ---------------------------------------------------------------------------

def test_bpfo_alarm_leads_the_bearing_failure():
    fail, repair = cm.DOWNTIME
    assert 0 < cm.BPFO_ALARM_STEP < fail, "the band alarm must fire while the mill is still running"
    # A meaningful lead: at least an hour of the 8 h loop, not the last minute.
    assert (fail - cm.BPFO_ALARM_STEP) * cm.INTERVAL_SEC >= 3600
    assert cm.spectral_features(cm.BPFO_ALARM_STEP - 1)["bpfo_alarm"] is False
    assert cm.spectral_features(cm.BPFO_ALARM_STEP)["bpfo_alarm"] is True
    assert cm.spectral_features(fail - 1)["bpfo_alarm"] is True
    # RMS has not yet reached the story alarm when the band crosses.
    assert cm.trend(cm.BPFO_ALARM_STEP, cm.VIB_HEALTHY_MM_S, cm.VIB_ALARM_MM_S) < cm.VIB_ALARM_MM_S


def test_features_climb_then_stop_then_reset():
    fail, repair = cm.DOWNTIME
    keys = list(cm.FEATURES)
    prev = cm.spectral_features(0)
    assert prev["bpfo_alarm"] is False
    for step in range(1, fail):
        cur = cm.spectral_features(step)
        for k in keys:
            assert cur[k] >= prev[k], f"{k} decreased at step {step}"
        prev = cur
    for step in range(fail, fail + repair):
        stopped = cm.spectral_features(step)
        assert all(stopped[k] == 0.0 for k in keys) and stopped["bpfo_alarm"] is False
    after = cm.spectral_features(fail + repair)
    assert after == cm.spectral_features(0)
    assert cm.spectral_features(cm.NUM_STEPS - 1)["bpfo_alarm"] is False


def test_bpfo_alarm_step_honours_a_shrunk_downtime():
    tiny = (60, 10)
    step = cm.bpfo_alarm_step(tiny)
    assert 0 < step < 60
    assert cm.spectral_features(step - 1, tiny)["bpfo_alarm"] is False
    assert cm.spectral_features(step, tiny)["bpfo_alarm"] is True
    assert cm.spectral_features(60, tiny)["bpfo"] == 0.0


def test_defect_frequencies_follow_the_running_speed():
    shaft_hz = cm.SPINDLE_RPM / 60.0
    assert shaft_hz < cm.BPFO_HZ < cm.BPFI_HZ
    assert cm.BPFO_HZ == pytest.approx(4.5 * shaft_hz * (1 - cm.BEARING_DIAMETER_RATIO), abs=0.1)
    assert cm.BPFI_HZ == pytest.approx(4.5 * shaft_hz * (1 + cm.BEARING_DIAMETER_RATIO), abs=0.1)


# ---------------------------------------------------------------------------
# Wall-clock alignment (server and simulator read the same row)
# ---------------------------------------------------------------------------

def test_loop_step_is_epoch_aligned():
    assert cm.loop_step(0) == 0
    assert cm.loop_step(cm.INTERVAL_SEC - 0.001) == 0
    assert cm.loop_step(cm.INTERVAL_SEC) == 1
    assert cm.loop_step(cm.TOTAL_SECONDS - 1) == cm.NUM_STEPS - 1
    assert cm.loop_step(cm.TOTAL_SECONDS) == 0
    assert cm.loop_step(7 * cm.TOTAL_SECONDS + 123) == 123 // cm.INTERVAL_SEC
    assert cm.loop_number(7 * cm.TOTAL_SECONDS + 123) == 7


def test_opc_server_plays_wall_clock_aligned():
    """The row rule lives in clock.mjs (behaviour pinned against this model by
    test_opcua_story_clock.py); the server re-derives the row from the clock
    on every tick, never by incrementing it."""
    src = _SERVER_PATH.read_text(encoding="utf-8")
    clock = (_SERVER_PATH.parent / "clock.mjs").read_text(encoding="utf-8")
    assert "export function wallClockRow" in clock and "% loopMs" in clock
    assert "Date.now()" in src and "UPDATE_INTERVAL_MS" in src and "from './clock.mjs'" in src
    assert "(dataIndex + 1)" not in src, "an incrementing index drifts and restarts at row 0"
    assert "dataIndex = currentRow();" in src


# ---------------------------------------------------------------------------
# Anchored story clock (live story; anchor 0 == wall clock)
# ---------------------------------------------------------------------------

def _sampled_epochs(n=1000):
    rng = random.Random(20260927)
    return [rng.uniform(0, 4_000_000_000) for _ in range(n)]


def test_anchor_zero_is_the_wall_clock():
    for t in _sampled_epochs():
        assert cm.story_seconds(t, 0) == t
        assert cm.loop_step(cm.story_seconds(t, 0)) == cm.loop_step(t)
        assert cm.loop_step(cm.story_seconds(t)) == cm.loop_step(t)


def test_positive_anchor_shifts_the_clock():
    anchor = 1_790_000_123.5
    for offset in (0, 1, cm.INTERVAL_SEC, cm.TOTAL_SECONDS - 1, 3 * cm.TOTAL_SECONDS + 42):
        assert cm.story_seconds(anchor + offset, anchor) == offset
    assert cm.loop_step(cm.story_seconds(anchor, anchor)) == 0
    assert cm.loop_step(cm.story_seconds(anchor + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC, anchor)) == cm.BPFO_ALARM_STEP


@pytest.mark.parametrize("anchor", [0.0, 1_790_000_123.5])
def test_loop_start_epoch_brackets_the_epoch(anchor):
    for t in _sampled_epochs(200) + [anchor + k * cm.TOTAL_SECONDS for k in range(3)]:
        start = cm.loop_start_epoch(t, anchor)
        assert start <= t < start + cm.TOTAL_SECONDS
        assert (start - anchor) % cm.TOTAL_SECONDS == pytest.approx(0.0, abs=1e-3)
        assert cm.loop_step(cm.story_seconds(start, anchor)) == 0


def test_loop_start_epoch_anchor_zero_matches_epoch_alignment():
    for t in _sampled_epochs(200):
        assert cm.loop_start_epoch(t) == cm.loop_number(t) * cm.TOTAL_SECONDS


def test_phase_boundaries():
    fail, repair = cm.BEARING_FAIL_STEP, cm.BEARING_REPAIR_STEPS
    assert cm.phase(0) == "degrading"
    assert cm.phase(cm.BPFO_ALARM_STEP - 1) == "degrading"
    assert cm.phase(cm.BPFO_ALARM_STEP) == "bpfo_alarm"
    assert cm.phase(fail - 1) == "bpfo_alarm"
    assert cm.phase(fail) == "repair"
    assert cm.phase(fail + repair - 1) == "repair"
    assert cm.phase(fail + repair) == "healthy"
    assert cm.phase(cm.NUM_STEPS - 1) == "healthy"


def test_phase_agrees_with_the_band_alarm():
    for step in range(cm.NUM_STEPS):
        alarm = cm.spectral_features(step)["bpfo_alarm"]
        assert alarm == (cm.phase(step) == "bpfo_alarm"), step


def test_defect_reject_fraction_only_inside_the_alarm_window():
    fail = cm.BEARING_FAIL_STEP
    inside = range(cm.BPFO_ALARM_STEP, fail)
    for step in range(cm.NUM_STEPS):
        if step not in inside:
            assert cm.defect_reject_fraction(step) == 0.0, step
    values = [cm.defect_reject_fraction(s) for s in inside]
    assert all(b >= a for a, b in zip(values, values[1:]))
    assert all(0.15 <= v <= 0.40 for v in values)
    assert values[0] == pytest.approx(0.15)
    assert values[-1] == pytest.approx(0.40)


def test_story_geometry_is_the_module_constants():
    assert cm.story_geometry() == {
        "loop_seconds": cm.TOTAL_SECONDS,
        "interval_seconds": cm.INTERVAL_SEC,
        "bpfo_alarm_step": cm.BPFO_ALARM_STEP,
        "fail_step": cm.BEARING_FAIL_STEP,
        "repair_steps": cm.BEARING_REPAIR_STEPS,
    }
    assert all(isinstance(v, int) for v in cm.story_geometry().values())


def test_story_mes_line_is_the_equipment_line():
    # L3-M1 lives on line 3; the line id is the MES production_lines key.
    assert cm.STORY_MES_LINE == "LINE" + cm.STORY_MES_EQUIPMENT_ID.split("-")[0][1:]


# ---------------------------------------------------------------------------
# Work-order text
# ---------------------------------------------------------------------------

def test_work_order_description_fits_the_column_and_names_the_band():
    from erp_simulation.mes_simulator import MESSimulator
    for step in (cm.BPFO_ALARM_STEP, cm.BEARING_FAIL_STEP - 1):
        text = MESSimulator._condition_work_order_description(cm.spectral_features(step))
        assert len(text) <= 250
        assert "BPFO" in text and f"{cm.BPFO_HZ:.0f} Hz" in text and cm.STORY_SITEWISE_ASSET in text
        assert f"{cm.BPFO_ALARM_MM_S:.2f} mm/s" in text


def test_work_order_description_says_vibration():
    """The CMMS sign asks for an 'open vibration work order': the order's own
    text must say so, not only 'BPFO band' (the agent missed it 1 in 4 asks)."""
    from erp_simulation.mes_simulator import MESSimulator
    features = cm.spectral_features(cm.BPFO_ALARM_STEP)
    text = MESSimulator._condition_work_order_description(features)
    assert text.startswith(
        f"Condition monitor: spindle vibration alarm. BPFO band {features['bpfo']:.2f} mm/s at "
        f"{cm.BPFO_HZ:.0f} Hz exceeded {cm.BPFO_ALARM_MM_S:.2f} mm/s")
    assert text.endswith("replace spindle bearing.")  # never cut by the 250-char cap


# ---------------------------------------------------------------------------
# Existing deploys are left alone
# ---------------------------------------------------------------------------

def test_existing_sitewise_models_only_gain_missing_properties():
    """DeploySitewiseModels reuses a model that already exists by name, and
    ADDS the declared properties it lacks (amended 2026-10-02): an account
    that kept a pre-spectrum CNC model otherwise never got the eight
    properties of decision 2, and SFC dropped those channels. Existing
    properties are sent back unchanged with their ids; nothing is removed
    (behaviour pinned in test_deploy_sitewise_models_reconcile.py)."""
    src = (_REPO_ROOT / "highbyte" / "DeploySitewiseModels.py").read_text(encoding="utf-8")
    assert "def create_or_get_model" in src
    assert "def reconcile_model" in src
    assert "Found existing" in src
    assert "delete_asset_model" not in src


# ---------------------------------------------------------------------------
# One story clock: the epoch 300 s telemetry grid (ADR-0089)
# ---------------------------------------------------------------------------

def _congruent_anchors(n=500):
    rng = random.Random(20260929)
    return [rng.randrange(1_700_000_000, 1_900_000_000) // 300 * 300 + cm.ANCHOR_RESIDUE for _ in range(n)]


def test_grid_constants():
    assert cm.TELEMETRY_GRID_SECONDS == 300
    assert cm.ALARM_OFFSET_SECONDS == cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC
    assert cm.ANCHOR_RESIDUE == (-cm.ALARM_OFFSET_SECONDS) % cm.TELEMETRY_GRID_SECONDS == 10
    # A whole loop is whole grid slots, so every loop of a congruent anchor stays congruent.
    assert cm.TOTAL_SECONDS % cm.TELEMETRY_GRID_SECONDS == 0


def test_alarm_epoch_is_a_grid_point_for_every_congruent_anchor():
    for a in _congruent_anchors():
        assert a % 300 == 10
        for epoch in (a, a + 1, a + cm.TOTAL_SECONDS - 1, a + 5 * cm.TOTAL_SECONDS + 77, a - 86400):
            alarm = cm.alarm_epoch(epoch, a)
            assert alarm % 300 == 0, (a, epoch)
            assert alarm == cm.loop_start_epoch(epoch, a) + cm.ALARM_OFFSET_SECONDS
            assert cm.loop_step(cm.story_seconds(alarm, a)) == cm.BPFO_ALARM_STEP


def test_fail_epoch_is_the_bearing_failure_of_the_same_loop():
    a = _congruent_anchors(1)[0]
    for epoch in (a, a + 3 * cm.TOTAL_SECONDS + 5):
        start = cm.loop_start_epoch(epoch, a)
        assert cm.fail_epoch(epoch, a) == start + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC
        assert cm.fail_epoch(epoch, a) > cm.alarm_epoch(epoch, a)


def test_grid_floor():
    assert cm.grid_floor(0) == 0
    assert cm.grid_floor(299.999) == 0
    assert cm.grid_floor(300) == 300
    assert cm.grid_floor(1_790_000_123.5) == 1_790_000_100
    assert cm.grid_floor(-1) == -300
    assert isinstance(cm.grid_floor(601.2), int)


def test_snap_anchor_down_is_at_most_one_slot_earlier():
    rng = random.Random(15)
    for _ in range(2000):
        t = rng.uniform(1_700_000_000, 1_900_000_000)
        a = cm.snap_anchor(t)
        assert a == cm.snap_anchor(t, "down")
        assert isinstance(a, int)
        assert a % 300 == cm.ANCHOR_RESIDUE
        assert t - 300 < a <= t


def test_snap_anchor_nearest():
    base = 1_790_000_110  # congruent
    assert base % 300 == 10
    assert cm.snap_anchor(base, "nearest") == base
    assert cm.snap_anchor(base + 150, "nearest") == base
    assert cm.snap_anchor(base + 151, "nearest") == base + 300
    assert cm.snap_anchor(base - 1, "nearest") == base
    assert cm.snap_anchor(base + 1, "down") == base
    assert cm.snap_anchor(base - 1, "down") == base - 300
    rng = random.Random(12)
    for _ in range(2000):
        t = rng.uniform(1_700_000_000, 1_900_000_000)
        a = cm.snap_anchor(t, "nearest")
        assert a % 300 == 10 and abs(a - t) <= 150


def test_snap_anchor_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        cm.snap_anchor(1_790_000_000, "up")
