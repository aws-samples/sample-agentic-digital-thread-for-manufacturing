# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the ``_Total`` counter fix in the OPC UA dataset
generator (``highbyte/generate_datasets.py``).

Background: the ``_Total`` branch used to emit a hardcoded ``0`` placeholder
that was never resolved. Because HighByte publishes on-change, a never-changing
value is never reported, so every equipment Total showed null/GoodNoData
downstream. The fix derives ``*_Total`` per step from the latest sibling
``*_Good`` + ``*_Bad`` counter values (order-guarded, deterministic).

The generator is NOT an installed package — it is imported by path with
``importlib``, mirroring the pattern in ``app.py`` (~330-340) that regenerates
missing datasets at synth time.

Coverage:
  * ``Total == Good + Bad`` at every timestep for every derived Total tag;
  * Totals are monotonically non-decreasing and actually increase during the
    active state (the original bug left them pinned at 0);
  * ``*_TotalScanned`` keeps its own ``CounterGen`` and is untouched by the
    derived-Total branch;
  * the Good/Bad-before-Total registration-order guard raises instead of
    silently assuming order;
  * output is deterministic for a fixed seed.

Run:
  pytest agentic_digital_thread/tests/unit/test_generate_datasets_totals.py -q
"""
import csv
import importlib.util
from pathlib import Path

import pytest

_GENERATOR_PATH = (
    Path(__file__).resolve().parents[3] / "highbyte" / "generate_datasets.py"
)

# A tiny generation window: 60 steps x 5s = 5 minutes. Downtime in the middle
# exercises the counter-pause path ("down" state) inside the window.
_TINY_NUM_STEPS = 60
_DOWNTIME = (20, 10)  # (start_step, duration_steps)


def _load_generator():
    """Import the generator module by path (app.py's importlib pattern)."""
    spec = importlib.util.spec_from_file_location(
        "generate_datasets_under_test", str(_GENERATOR_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gen_mod():
    """A freshly-loaded generator module (module exec re-seeds random)."""
    return _load_generator()


def _generate(gen_mod, tmp_path, tags_dict, filename="test_site.csv"):
    """Run generate_site_csv over a tiny window into tmp_path; return rows
    grouped as {alias: [value, ...]} in step order (values as raw strings)."""
    schedule = gen_mod.ShiftSchedule(
        active_windows=[(0, 24)], downtimes=[_DOWNTIME]
    )
    gen_mod.NUM_STEPS = _TINY_NUM_STEPS
    gen_mod.OUTPUT_DIR = tmp_path
    gen_mod.generate_site_csv(filename, [(tags_dict, schedule)])

    series = {}
    with open(tmp_path / filename, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            series.setdefault(row["ALIAS"], []).append(row["VALUE"])
    return series


def _derived_total_aliases(tags_dict):
    """Aliases whose Total is derived (registered with gen=None)."""
    return [
        alias
        for alias, (_dtype, gen) in tags_dict.items()
        if alias.endswith("_Total") and gen is None
    ]


def test_total_equals_good_plus_bad_at_every_timestep(gen_mod, tmp_path):
    tags = gen_mod.food_tags("Site/Plant/Area", "Line1")
    series = _generate(gen_mod, tmp_path, tags)

    totals = _derived_total_aliases(tags)
    assert totals, "expected at least one derived _Total tag in food_tags"

    for total_alias in totals:
        prefix = total_alias[: -len("_Total")]
        good = [int(v) for v in series[f"{prefix}_Good"]]
        bad = [int(v) for v in series[f"{prefix}_Bad"]]
        total = [int(v) for v in series[total_alias]]
        assert len(total) == _TINY_NUM_STEPS
        for step, (t, g, b) in enumerate(zip(total, good, bad)):
            assert t == g + b, (
                f"{total_alias} step {step}: Total {t} != Good {g} + Bad {b}"
            )


def test_total_monotonic_and_increasing_while_active(gen_mod, tmp_path):
    tags = gen_mod.bottling_tags("Site/Plant/Area", "Line1")
    series = _generate(gen_mod, tmp_path, tags)

    for total_alias in _derived_total_aliases(tags):
        total = [int(v) for v in series[total_alias]]
        # Monotonic non-decreasing over the whole window (counters only pause).
        for step in range(1, len(total)):
            assert total[step] >= total[step - 1], (
                f"{total_alias} decreased at step {step}"
            )
        # The original bug pinned Totals at a constant 0. With the schedule
        # fully active outside the downtime window, Totals must actually grow.
        assert total[-1] > 0, f"{total_alias} never left 0 (placeholder bug)"
        # And they hold flat during the downtime (Good/Bad pause when down).
        down_start, down_dur = _DOWNTIME
        down_vals = total[down_start : down_start + down_dur]
        assert down_vals == [down_vals[0]] * len(down_vals), (
            f"{total_alias} changed during downtime"
        )


def test_total_scanned_keeps_its_own_counter(gen_mod, tmp_path):
    tags = gen_mod.food_tags("Site/Plant/Area", "Line1")

    # _TotalScanned is registered with its own CounterGen — it must never be
    # eligible for the derived branch (which requires gen=None), and its alias
    # does not end with "_Total".
    scanned_aliases = [a for a in tags if a.endswith("_TotalScanned")]
    assert scanned_aliases, "expected a _TotalScanned tag in food_tags"
    for alias in scanned_aliases:
        assert not alias.endswith("_Total")
        assert isinstance(tags[alias][1], gen_mod.CounterGen)

    # It has no _Good/_Bad siblings, so if the derived branch caught it the
    # order guard would raise — generation completing is itself evidence.
    series = _generate(gen_mod, tmp_path, tags)
    for alias in scanned_aliases:
        scanned = [int(v) for v in series[alias]]
        assert len(scanned) == _TINY_NUM_STEPS
        # Still driven by its own counter: monotonic and growing while active.
        for step in range(1, len(scanned)):
            assert scanned[step] >= scanned[step - 1]
        assert scanned[-1] > 0


def test_registration_order_guard_raises_when_total_precedes_counters(
    gen_mod, tmp_path
):
    # A _Total registered BEFORE its Good/Bad siblings must fail loudly
    # instead of silently emitting a stale or zero value.
    tags = {
        "S/L/Eq_Total": ("INTEGER", None),
        "S/L/Eq_Good": ("INTEGER", gen_mod.CounterGen(5)),
        "S/L/Eq_Bad": ("INTEGER", gen_mod.CounterGen(1)),
    }
    with pytest.raises(RuntimeError, match="must be registered"):
        _generate(gen_mod, tmp_path, tags)


def test_output_is_deterministic_for_fixed_seed(gen_mod, tmp_path):
    def run(subdir):
        out = tmp_path / subdir
        out.mkdir()
        gen_mod.random.seed(42)
        tags = gen_mod.food_tags("Site/Plant/Area", "Line1")
        _generate(gen_mod, out, tags)
        return (out / "test_site.csv").read_bytes()

    assert run("a") == run("b")
