# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""App-side story-clock mirror vs the simulator, across loop lengths (ADR-0091).

Both sides compute the loop geometry with a pure function of the loop length
(``agents.demo.story_loop.geometry_for`` and
``erp_simulation.condition_monitor.geometry_for``), so their agreement is
checked IN PROCESS for every valid loop length, and so are the shared parser's
bad values and edges.

What a pure function cannot show is that each side reads
``STORY_LOOP_SECONDS`` at import and binds its constants from it. That takes a
child interpreter (an in-process reload would leave the rest of the lane on
mixed geometry), and the file starts exactly three: one per shorter loop
length (the full mirror chain: constants, the anchor helpers vs
``story_replan``, the lookup timeline, ``alarm_in_minutes``), and one that
re-imports both env readers under every bad value. The unset (default) loop
is the lane's own environment, so its chain runs in process.

Offline: a child process runs this interpreter on test-authored code only.
"""
from __future__ import annotations

import json
import os
import random
import subprocess  # nosec B404 - runs this interpreter on test-authored code only
import sys
from pathlib import Path
from typing import Optional

import pytest

_APP_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _APP_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_LOOP_ENV = "STORY_LOOP_SECONDS"

from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import story_replan as sr  # noqa: E402

from agents.demo import story_clock as sc  # noqa: E402
from agents.demo import story_lookup as sl  # noqa: E402
from agents.demo import story_loop  # noqa: E402

_VALID_LOOPS = tuple(range(3600, 28800 + 1, 300))

# (loop, steps, fail, repair, alarm step, offset s, residue) — ADR-0091 table.
_TABLE = {
    28800: (28800, 5760, 5184, 240, 3898, 19490, 10),
    10800: (10800, 2160, 1944, 90, 1462, 7310, 190),
    21600: (21600, 4320, 3888, 180, 2924, 14620, 80),
}
_BAD_LOOPS = ("7000", "100", "abc", "3300", "29100", "43200", "86400", "-3600", "3600.0")


def _sim_phase_seconds(g: "cm.StoryGeometry") -> dict:
    """story_replan.PHASE_STEPS for geometry *g*, in seconds."""
    alarm, fail, repaired = g.bpfo_alarm_step, g.fail_step, g.fail_step + g.repair_steps
    steps = {"degrading": (0, alarm), "bpfo_alarm": (alarm, fail),
             "repair": (fail, repaired), "healthy": (repaired, g.num_steps)}
    return {k: (a * cm.INTERVAL_SEC, b * cm.INTERVAL_SEC) for k, (a, b) in steps.items()}


def _stored(g: "story_loop.LoopGeometry") -> dict:
    """The geometry the live-story history stores (cm.story_geometry() shape)."""
    return {"loop_seconds": g.loop_seconds, "interval_seconds": story_loop.INTERVAL_SECONDS,
            "bpfo_alarm_step": g.bpfo_alarm_step, "fail_step": g.fail_step,
            "repair_steps": g.repair_steps}


# --- the pure geometry, every valid loop length ---------------------------------

def test_app_and_simulator_geometry_agree_at_every_valid_loop_length():
    wrong = {}
    for loop in _VALID_LOOPS:
        app, sim = story_loop.geometry_for(loop), cm.geometry_for(loop)
        got = {"fields": tuple(app) == tuple(sim),
               "phases": app.phase_seconds == _sim_phase_seconds(sim),
               "alarm_in": app.alarm_in_minutes_range == (5, loop // 60),
               "tiles": app.phase_seconds["healthy"][1] == loop,
               "leads": sim.bpfo_alarm_step < sim.fail_step < sim.fail_step + sim.repair_steps
               < sim.num_steps}
        if not all(got.values()):
            wrong[loop] = got
    assert not wrong, wrong


@pytest.mark.parametrize("loop", sorted(_TABLE))
def test_the_adr_table_rows(loop):
    assert tuple(story_loop.geometry_for(loop)) == tuple(cm.geometry_for(loop)) == _TABLE[loop]


def test_the_lookup_timeline_from_the_stored_geometry_at_every_valid_loop_length():
    """The geometry the history stores drives story_lookup.phase_timeline to
    the simulator's own phase boundaries."""
    now = 1_790_000_000
    wrong = {}
    for loop in _VALID_LOOPS:
        g = story_loop.geometry_for(loop)
        anchor = (now // 300) * 300 + g.anchor_residue
        tl = sl.phase_timeline(anchor, _stored(g), anchor + 10)
        phases = _sim_phase_seconds(cm.geometry_for(loop))
        ends = [p["end_offset_s"] for p in tl["phases"]]
        if (ends != [phases[p][1] for p in ("degrading", "bpfo_alarm", "repair", "healthy")]
                or tl["seconds_to_alarm"] != g.alarm_offset_seconds - 10):
            wrong[loop] = ends
    assert not wrong, wrong


def test_the_geometry_refuses_what_the_parser_refuses():
    for value in (0, 3300, 3700, 28900, 43200, -3600):
        for geometry_for in (story_loop.geometry_for, cm.geometry_for):
            with pytest.raises(ValueError, match=_LOOP_ENV):
                geometry_for(value)


@pytest.mark.parametrize("bad", _BAD_LOOPS)
def test_the_same_bad_loop_is_refused_by_both_parsers(bad):
    for parse in (story_loop.loop_seconds_from, cm.loop_seconds_from):
        with pytest.raises(ValueError, match=_LOOP_ENV):
            parse(bad)


@pytest.mark.parametrize("edge", ["3600", "28800", " 10800 ", "", None])
def test_validation_edges_agree(edge):
    assert story_loop.loop_seconds_from(edge) == cm.loop_seconds_from(edge)


def test_story_loop_is_stdlib_only_and_reads_the_env_once():
    """The app's loop math loads without the place route's FastAPI/pydantic
    imports, and ``os`` is there for the ONE module-level env read."""
    import ast
    src = (_APP_ROOT / "agents" / "demo" / "story_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = sorted({a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                       for a in n.names}
                      | {(n.module or "").split(".")[0] for n in ast.walk(tree)
                         if isinstance(n, ast.ImportFrom)})
    assert imported == ["__future__", "math", "os", "typing"], imported
    reads = [n for n in tree.body if isinstance(n, ast.Assign)
             and "os.environ.get(LOOP_ENV)" in ast.unparse(n)]
    assert [ast.unparse(n.targets[0]) for n in reads] == ["LOOP_SECONDS"]
    assert len([n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "os"]) == 1


# --- the loop read at import: the lane's own (unset) environment -------------------

def test_the_default_loop_binds_every_module_in_process():
    assert os.environ.get(_LOOP_ENV) is None, "the unit lane runs with STORY_LOOP_SECONDS unset"
    out = _mirror_chain(cm, sr, sc, sl, story_loop)
    _check_chain(28800, out)


def test_story_clock_takes_its_geometry_from_story_loop():
    """story_clock binds, never recomputes: its constants ARE story_loop's."""
    assert sc._GEOMETRY is story_loop.GEOMETRY
    assert sc.LOOP_SECONDS == story_loop.LOOP_SECONDS
    assert sc.PHASE_SECONDS == story_loop.GEOMETRY.phase_seconds
    assert sc.ALARM_IN_MINUTES_RANGE == story_loop.GEOMETRY.alarm_in_minutes_range
    assert cm.GEOMETRY == cm.geometry_for(cm.TOTAL_SECONDS)


# Every module-level name story_clock had before the loop math moved to
# story_loop. Moving the math must not drop one.
_STORY_CLOCK_NAMES = frozenset({
    "ALARM_IN_MINUTES_RANGE", "ALARM_OFFSET_SECONDS", "ALARM_WINDOW_SECONDS", "ANCHOR_RESIDUE",
    "DEFAULT_LOOP_SECONDS", "INTERVAL_SECONDS", "IN_FLIGHT_DETAIL", "KEEP_HISTORY_ACTION",
    "KEEP_HISTORY_EFFECT", "KEEP_HISTORY_EFFECT_CODES", "LAMBDA_READ_TIMEOUT_S", "LIVE_STORY",
    "LOOPS_SHIFTED_WARNING", "LOOPS_SHIFTED_WARNING_CODE", "LOOP_ENV", "LOOP_SECONDS",
    "LOOP_SECONDS_RANGE", "MAX_ANCHOR_EPOCH", "NOT_LIVE_DETAIL", "NO_DEFAULT_DETAIL",
    "PHASE_SECONDS", "PLACEMENT_EFFECT", "PLACEMENT_EFFECT_CODES", "PLACE_ACTION",
    "PlaceClockRequest", "RESET_ACTION", "RESET_EFFECT", "RESET_EFFECT_CODE",
    "REWINDS_LIVE_WARNING", "REWINDS_LIVE_WARNING_CODE", "STORY_HISTORY_FUNCTION_ENV",
    "TELEMETRY_GRID_SECONDS", "_BPFO_ALARM_MM_S", "_BPFO_ALARM_STEP", "_BPFO_FAILED",
    "_BPFO_HEALTHY", "_FAIL_FRACTION", "_FAIL_STEP", "_NUM_STEPS", "_PLACE_IN_FLIGHT",
    "_REPAIR_LOOP_DIVISOR", "_REPAIR_STEPS", "_REPAIR_STEPS_CAP", "_STATUS_BY_ERROR",
    "_bad_gateway", "_default_client", "_default_reader", "_echo", "_effect", "_invoke", "_iso",
    "_mapped", "_masked", "_reference", "_resolved", "_stored_default", "_warnings",
    "alarm_in_to_epoch", "alarm_to_anchor", "live_past_alarm", "logger", "loop_seconds_from",
    "loops_shifted", "normalize_anchor", "payload", "phase_offset_to_anchor", "place", "preview",
    "reset_preview", "snap_anchor_nearest",
})


def test_story_clock_keeps_every_name_it_had():
    missing = sorted(name for name in _STORY_CLOCK_NAMES if not hasattr(sc, name))
    assert missing == []
    # The names that moved are story_loop's own objects, not copies.
    for name in ("DEFAULT_LOOP_SECONDS", "INTERVAL_SECONDS", "LOOP_ENV", "LOOP_SECONDS_RANGE",
                 "TELEMETRY_GRID_SECONDS", "loop_seconds_from"):
        assert getattr(sc, name) is getattr(story_loop, name), name


def _mirror_chain(cm_, sr_, sc_, sl_, loop_):
    """The derived constants and sampled helper results of one interpreter's
    modules (also run, as source, in each child)."""
    from pydantic import ValidationError

    def req(minutes):
        try:
            sc_.PlaceClockRequest(alarm_in_minutes=minutes, expected_anchor_epoch=None, force=True)
            return True
        except ValidationError:
            return False

    now = 1_790_000_000
    rng = random.Random(5)
    helpers_ok = True
    for _ in range(500):
        n = now + rng.randint(-10 ** 6, 10 ** 6)
        ref = n + rng.uniform(-3 * sc_.LOOP_SECONDS, 3 * sc_.LOOP_SECONDS)
        helpers_ok &= (sc_.snap_anchor_nearest(ref) == cm_.snap_anchor(ref, "nearest")
                       and sc_.normalize_anchor(ref, n) == sr_.normalize_anchor(ref, n)
                       and sc_.loops_shifted(ref, n) == sr_.loops_shifted(ref, n)
                       and sc_.alarm_to_anchor(ref) == sr_.alarm_to_anchor(ref))
    for p, (a, b) in sc_.PHASE_SECONDS.items():
        for m in (0, 1, (b - a) / 60 - 0.5):
            helpers_ok &= sc_.phase_offset_to_anchor(p, m, now) == sr_.phase_offset_to_anchor(p, m, now)
    app_geo = {"loop_seconds": sc_.LOOP_SECONDS, "interval_seconds": sc_.INTERVAL_SECONDS,
               "bpfo_alarm_step": sc_._BPFO_ALARM_STEP, "fail_step": sc_._FAIL_STEP,
               "repair_steps": sc_._REPAIR_STEPS}
    anchor = (now // 300) * 300 + sc_.ANCHOR_RESIDUE
    tl = sl_.phase_timeline(anchor, app_geo, anchor + 10)
    top = sc_.ALARM_IN_MINUTES_RANGE[1]
    return {
        "app": [sc_.LOOP_SECONDS, sc_._NUM_STEPS, sc_._FAIL_STEP, sc_._REPAIR_STEPS,
                sc_._BPFO_ALARM_STEP, sc_.ALARM_OFFSET_SECONDS, sc_.ANCHOR_RESIDUE],
        "sim": [sr_.LOOP_SECONDS, cm_.NUM_STEPS, cm_.BEARING_FAIL_STEP, cm_.BEARING_REPAIR_STEPS,
                cm_.BPFO_ALARM_STEP, cm_.ALARM_OFFSET_SECONDS, cm_.ANCHOR_RESIDUE],
        "app_phases": {k: list(v) for k, v in sc_.PHASE_SECONDS.items()},
        "sim_phases": {k: [a * cm_.INTERVAL_SEC, b * cm_.INTERVAL_SEC]
                       for k, (a, b) in sr_.PHASE_STEPS.items()},
        "sim_geometry": cm_.story_geometry(), "app_geometry": app_geo,
        "bound": [list(loop_.GEOMETRY), list(cm_.GEOMETRY), sc_._GEOMETRY is loop_.GEOMETRY],
        "helpers_ok": bool(helpers_ok),
        "timeline_ends": [p["end_offset_s"] for p in tl["phases"]],
        "timeline_seconds_to_alarm": tl["seconds_to_alarm"],
        "alarm_in_range": list(sc_.ALARM_IN_MINUTES_RANGE),
        "accepts": {str(m): req(m) for m in (4, 5, 180, 181, 400, 480, 481, top, top + 1)},
    }


def _check_chain(loop: int, out: dict) -> None:
    failed = []

    def check(name, ok):
        if not ok:
            failed.append(name)

    check("constants", out["app"] == out["sim"] == list(_TABLE[loop]))
    check("phases", out["app_phases"] == out["sim_phases"])
    check("stored geometry", out["app_geometry"] == out["sim_geometry"])
    check("bound to geometry_for", out["bound"][0] == out["bound"][1] == list(_TABLE[loop])
          and out["bound"][2] is True)
    check("helpers vs story_replan", out["helpers_ok"] is True)
    phases = out["sim_phases"]
    check("timeline", out["timeline_ends"] == [phases[p][1] for p in
                                               ("degrading", "bpfo_alarm", "repair", "healthy")]
          and out["timeline_seconds_to_alarm"] == out["sim"][5] - 10)
    top = loop // 60
    accepts = out["accepts"]
    check("alarm_in_minutes", out["alarm_in_range"] == [5, top]
          and accepts["4"] is False and accepts["5"] is True
          and accepts[str(top)] is True and accepts[str(top + 1)] is False)
    if loop == 10800:
        check("3 h caps", accepts["180"] and not accepts["181"] and not accepts["400"])
    if loop == 28800:
        check("8 h caps", accepts["480"] and not accepts["481"])
    assert not failed, f"loop {loop}: {failed}\n{json.dumps(out, indent=1, sort_keys=True)}"


# --- the loop read at import: child interpreters ----------------------------------

# A child's timeout is a hang guard, not a speed check: a child takes well
# under a second locally. Every child dumps its stack before the bound, so a
# timeout reports WHERE it was stuck instead of an empty TimeoutExpired.
_CHILD_TIMEOUT_S = 120
_CHILD_DUMP_AFTER_S = 90

_PRELUDE = (
    "import faulthandler\n"
    f"faulthandler.dump_traceback_later({_CHILD_DUMP_AFTER_S}, exit=False)\n"
    "import json\n"
    "def emit(obj):\n"
    "    print(json.dumps(obj, sort_keys=True))\n"
)


def _run(loop: Optional[str], code: str):
    env = {k: v for k, v in os.environ.items() if k != _LOOP_ENV}
    if loop is not None:
        env[_LOOP_ENV] = loop
    env["PYTHONPATH"] = os.pathsep.join((str(_APP_ROOT), str(_REPO_ROOT)))
    try:
        return subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", _PRELUDE + code], cwd=str(_APP_ROOT), env=env,
            capture_output=True, text=True, timeout=_CHILD_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        pytest.fail(f"child ({_LOOP_ENV}={loop!r}) still running after {_CHILD_TIMEOUT_S} s; "
                    f"its stack at {_CHILD_DUMP_AFTER_S} s:\n{stderr or '<no output>'}")


def _chain_child_source() -> str:
    import inspect
    return ("import random\n"
            "from erp_simulation import condition_monitor as cm, story_replan as sr\n"
            "from agents.demo import story_clock as sc, story_lookup as sl, story_loop\n"
            + inspect.getsource(_mirror_chain)
            + "emit(_mirror_chain(cm, sr, sc, sl, story_loop))\n")


@pytest.mark.parametrize("loop", [10800, 21600])
def test_a_shorter_loop_read_at_import_reaches_every_module(loop):
    proc = _run(str(loop), _chain_child_source())
    assert proc.returncode == 0, proc.stderr
    _check_chain(loop, json.loads(proc.stdout.strip().splitlines()[-1]))


# The two modules that read the env: one child re-imports each under every bad
# value. Before every import it sets the value and drops EVERY first-party
# module (``agents``, ``erp_simulation`` and their submodules) from
# sys.modules, so the target and anything first-party it imports re-execute
# under that value. The deepest frame must be the module's own file, so a
# failure raised by something it imported does not count, and a failed import
# must leave the module uncached. story_clock reads nothing itself; it binds
# story_loop's geometry (test_story_clock_takes_its_geometry_from_story_loop).
_ENV_READERS = ("erp_simulation.condition_monitor", "agents.demo.story_loop")

_BAD_MATRIX_CHILD = r"""
import importlib, os, sys, traceback
FIRST_PARTY = ("agents", "erp_simulation")
def purge():
    for name in list(sys.modules):
        if name.split(".", 1)[0] in FIRST_PARTY:
            del sys.modules[name]
out = {}
for module in MODULES:
    for bad in BAD:
        os.environ[LOOP_ENV] = bad
        purge()
        try:
            importlib.import_module(module)
        except Exception as exc:
            frames = traceback.extract_tb(exc.__traceback__)
            out[module + "|" + bad] = {
                "type": type(exc).__name__, "message": str(exc),
                "raised_in": os.path.realpath(frames[-1].filename) if frames else "",
                "cached": module in sys.modules}
        else:
            out[module + "|" + bad] = {"type": None, "message": "", "raised_in": "",
                                       "cached": True}
emit({"results": out})
"""


def _module_file(module: str) -> str:
    root = _REPO_ROOT if module.startswith("erp_simulation.") else _APP_ROOT
    return os.path.realpath(root.joinpath(*module.split(".")).with_suffix(".py"))


def test_the_same_bad_loop_fails_both_imports():
    code = (f"MODULES = {_ENV_READERS!r}\nBAD = {_BAD_LOOPS!r}\nLOOP_ENV = {_LOOP_ENV!r}\n"
            + _BAD_MATRIX_CHILD)
    proc = _run(None, code)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])["results"]
    wrong = {}
    for module in _ENV_READERS:
        for bad in _BAD_LOOPS:
            got = results[f"{module}|{bad}"]
            if not (got["type"] == "ValueError" and _LOOP_ENV in got["message"]
                    and got["raised_in"] == _module_file(module) and got["cached"] is False):
                wrong[f"{module} under {_LOOP_ENV}={bad!r}"] = got
    assert not wrong, json.dumps(wrong, indent=1)
