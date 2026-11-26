# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Facilitator story lookup (``agents/demo/story_lookup.py``, ADR-0085).

Pins:

* the live phase clock computed from the stored ``story_*`` numbers agrees
  with the simulator's ONE model, ``erp_simulation.condition_monitor`` (phase,
  loop number, loop step and loop start for sampled instants);
* the lookup's copies of the story identities / config keys match the
  simulator and history constants (drift guard, since the app never imports
  ``erp_simulation``);
* frozen and live answers assemble from fixtures; a missing or failing source
  becomes a warning, never an exception;
* every statement is LIMITed and parameterized; sessions are read-only with a
  statement timeout, opened once per database.

Offline: a fake DB layer keyed on the module's SQL constants. No literals for
sites/schemas — everything is derived from the workshop/simulator constants.
"""
from __future__ import annotations

import random
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.demo import db_routing, story_lookup as sl, systems, workshop  # noqa: E402
from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import story_history, story_live  # noqa: E402
from erp_simulation.mes_simulator import MESSimulator  # noqa: E402

_ANCHOR = 1_790_000_000.0


class FakeDb:
    """``rows(schema, sql, params)`` answered from ``{sql_template: rows | Exception}``
    (the lookup runs RENDERED templates; keys are rendered the same way)."""

    def __init__(self, responses):
        self.responses = {sl.render(k): v for k, v in responses.items()}
        self.calls = []

    def rows(self, schema, sql, params):
        self.calls.append((schema, sql, params))
        got = self.responses.get(sql, [])
        if isinstance(got, Exception):
            raise got
        return [dict(r) for r in got]

    def params_for(self, sql):
        return [p for _s, q, p in self.calls if q == sl.render(sql)]


def _geometry_config(anchor=_ANCHOR, **extra):
    geo = cm.story_geometry()
    config = {"story_anchor_epoch": str(anchor)}
    config.update({key: str(geo[name]) for name, key in sl._GEOMETRY_KEYS.items()})
    config.update(extra)
    return config


def _config_rows(config):
    return [{"config_key": k, "config_value": v} for k, v in config.items()]


# ── Phase / clock parity with the simulator's model ───────────────────────────

def test_phase_parity_every_step():
    geo = cm.story_geometry()
    for step in range(cm.NUM_STEPS):
        assert sl.phase(step, geo["bpfo_alarm_step"], geo["fail_step"],
                        geo["repair_steps"]) == cm.phase(step), step


@pytest.mark.parametrize("anchor", [_ANCHOR, 1.0])
def test_clock_parity_with_condition_monitor_for_sampled_instants(anchor):
    config = _geometry_config(anchor)
    rng = random.Random(7)
    for _ in range(1000):
        now = anchor + rng.uniform(-2 * cm.TOTAL_SECONDS, 10 * cm.TOTAL_SECONDS)
        clock = sl.story_clock(config, now)
        t = cm.story_seconds(now, anchor)
        assert clock["phase"] == cm.phase(cm.loop_step(t))
        assert clock["loop_step"] == cm.loop_step(t)
        assert clock["loop_number"] == cm.loop_number(t)
        start = datetime.fromtimestamp(cm.loop_start_epoch(now, anchor), tz=timezone.utc)
        assert clock["loop_started_at"] == start.isoformat()


def test_clock_phase_boundaries_and_next_phase():
    config = _geometry_config()
    step = cm.INTERVAL_SEC
    cases = [
        (0, "degrading", "bpfo_alarm"),
        (cm.BPFO_ALARM_STEP, "bpfo_alarm", "repair"),
        (cm.BEARING_FAIL_STEP, "repair", "healthy"),
        (cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS, "healthy", "degrading"),
    ]
    for k, current, following in cases:
        clock = sl.story_clock(config, _ANCHOR + k * step)
        assert (clock["phase"], clock["next_phase"]) == (current, following)
    clock = sl.story_clock(config, _ANCHOR + 1)
    assert clock["next_phase_at"] == clock["bpfo_alarm_at"]
    assert clock["anchor_epoch"] == _ANCHOR


@pytest.mark.parametrize("key", sorted(sl._GEOMETRY_KEYS.values()) + ["story_anchor_epoch"])
def test_clock_missing_or_malformed_key_is_a_miss(key):
    config = _geometry_config()
    config.pop(key)
    with pytest.raises(sl._Missing, match=key):
        sl.story_clock(config, _ANCHOR)
    config[key] = "not-a-number"
    with pytest.raises(sl._Missing, match=key):
        sl.story_clock(config, _ANCHOR)


@pytest.mark.parametrize("anchor", ["0", "0.0", "-5", "nan", "inf", "-inf"])
def test_non_finite_or_non_positive_anchor_is_a_miss(anchor):
    with pytest.raises(sl._Missing, match="story_anchor_epoch"):
        sl.story_clock(_geometry_config(anchor), _ANCHOR)


def test_non_positive_geometry_is_a_miss():
    with pytest.raises(sl._Missing, match="positive"):
        sl.story_clock(_geometry_config(story_loop_seconds="0"), _ANCHOR)


# ── Phase timeline (the UI does no clock math) ────────────────────────────────

_PHASE_ORDER = ["degrading", "bpfo_alarm", "repair", "healthy"]
# Walk 2 (one-clock-live-2): the deployed anchor and the 08:10:23Z lookup.
_WALK2_ANCHOR = 1_790_737_810
_WALK2_NOW = datetime(2026, 9, 30, 8, 10, 23, tzinfo=timezone.utc).timestamp()


def _cm_geo():
    return sl._geometry(_geometry_config())


def _epoch(iso):
    return datetime.fromisoformat(iso).timestamp()


def test_phase_timeline_at_the_walk2_fixture():
    tl = sl.phase_timeline(_WALK2_ANCHOR, _cm_geo(), _WALK2_NOW)
    assert tl["current"] == "degrading" and tl["next_phase"] == "bpfo_alarm"
    assert tl["next_alarm_at"] == "2026-09-30T08:35:00+00:00"
    assert tl["seconds_to_alarm"] == 1477
    assert tl["seconds_to_next_phase"] == 1477
    assert tl["next_phase_at"] == tl["next_alarm_at"]
    assert tl["alarm_active"] is False
    assert tl["loop_started_at"] == "2026-09-30T03:10:10+00:00"
    assert tl["loop_ends_at"] == "2026-09-30T11:10:10+00:00"
    assert tl["seconds_into_phase"] == int(_WALK2_NOW - _WALK2_ANCHOR)
    assert 0 < tl["loop_fraction"] < 1


def test_phase_timeline_shape_is_exact():
    tl = sl.phase_timeline(_ANCHOR, _cm_geo(), _ANCHOR + 60)
    assert set(tl) == {"loop_started_at", "loop_ends_at", "phases", "current",
                       "seconds_into_phase", "seconds_to_next_phase", "next_phase",
                       "next_phase_at", "next_alarm_at", "seconds_to_alarm",
                       "alarm_active", "loop_fraction"}
    assert [p["phase"] for p in tl["phases"]] == _PHASE_ORDER
    for p in tl["phases"]:
        assert set(p) == {"phase", "start_at", "end_at", "start_offset_s", "end_offset_s"}
        assert isinstance(p["start_offset_s"], int) and isinstance(p["end_offset_s"], int)
    for key in ("seconds_into_phase", "seconds_to_next_phase", "seconds_to_alarm"):
        assert isinstance(tl[key], int), key
    assert isinstance(tl["loop_fraction"], float)


def test_phase_timeline_boundaries_match_condition_monitor():
    """The boundaries ARE the simulator's steps (stored geometry = cm's)."""
    tl = sl.phase_timeline(_ANCHOR, _cm_geo(), _ANCHOR + 1)
    ends = [p["end_offset_s"] for p in tl["phases"]]
    assert ends == [cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC, cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC,
                    (cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS) * cm.INTERVAL_SEC,
                    cm.TOTAL_SECONDS]
    assert ends[0] == cm.ALARM_OFFSET_SECONDS


@pytest.mark.parametrize("index", range(4))
def test_phase_timeline_at_each_phase_start(index):
    geo = _cm_geo()
    start = sl.phase_timeline(_ANCHOR, geo, _ANCHOR + 1)["phases"][index]
    tl = sl.phase_timeline(_ANCHOR, geo, _ANCHOR + start["start_offset_s"])
    assert tl["current"] == _PHASE_ORDER[index]
    assert tl["seconds_into_phase"] == 0
    assert tl["seconds_to_next_phase"] == start["end_offset_s"] - start["start_offset_s"]
    assert tl["next_phase"] == _PHASE_ORDER[(index + 1) % 4]
    assert tl["alarm_active"] is (_PHASE_ORDER[index] == "bpfo_alarm")


def test_seconds_to_alarm_after_the_alarm_points_to_the_next_loop():
    geo = _cm_geo()
    alarm = _ANCHOR + cm.ALARM_OFFSET_SECONDS
    at_alarm = sl.phase_timeline(_ANCHOR, geo, alarm)
    assert at_alarm["alarm_active"] is True
    assert at_alarm["seconds_to_alarm"] == cm.TOTAL_SECONDS
    assert _epoch(at_alarm["next_alarm_at"]) == alarm + cm.TOTAL_SECONDS
    before = sl.phase_timeline(_ANCHOR, geo, alarm - 1)
    assert before["seconds_to_alarm"] == 1 and _epoch(before["next_alarm_at"]) == alarm
    healthy = sl.phase_timeline(_ANCHOR, geo, _ANCHOR + cm.TOTAL_SECONDS - 1)
    assert healthy["current"] == "healthy" and healthy["next_phase"] == "degrading"
    assert healthy["seconds_to_alarm"] == cm.ALARM_OFFSET_SECONDS + 1
    assert healthy["seconds_to_next_phase"] == 1
    assert healthy["next_phase_at"] == healthy["loop_ends_at"]


def test_phase_timeline_property_tiles_the_loop_and_agrees_with_the_clock():
    rng = random.Random(11)
    geo = _cm_geo()
    for _ in range(500):
        anchor = float(rng.randrange(1, 2_000_000_000))
        now = anchor + rng.uniform(-3 * cm.TOTAL_SECONDS, 20 * cm.TOTAL_SECONDS)
        tl = sl.phase_timeline(anchor, geo, now)
        phases = tl["phases"]
        assert phases[0]["start_offset_s"] == 0
        assert phases[-1]["end_offset_s"] == geo["loop_seconds"]
        assert phases[0]["start_at"] == tl["loop_started_at"]
        assert phases[-1]["end_at"] == tl["loop_ends_at"]
        for a, b in zip(phases, phases[1:]):
            assert a["end_offset_s"] == b["start_offset_s"] and a["end_at"] == b["start_at"]
            assert a["start_offset_s"] < a["end_offset_s"]
        start = _epoch(tl["loop_started_at"])
        assert start <= now < _epoch(tl["loop_ends_at"])
        assert start == cm.loop_start_epoch(now, anchor)
        clock = sl.story_clock(_geometry_config(anchor), now)
        assert tl["current"] == clock["phase"] == cm.phase(cm.loop_step(cm.story_seconds(now, anchor)))
        assert tl["next_phase"] == clock["next_phase"]
        assert tl["next_phase_at"] == clock["next_phase_at"]
        assert tl["seconds_to_next_phase"] >= 0 and tl["seconds_into_phase"] >= 0
        assert 0 < tl["seconds_to_alarm"] <= geo["loop_seconds"]
        assert _epoch(tl["next_alarm_at"]) > now
        assert 0.0 <= tl["loop_fraction"] <= 1.0


def test_next_alarm_lands_on_the_telemetry_grid_for_a_congruent_anchor():
    anchor = (int(_ANCHOR) // cm.TELEMETRY_GRID_SECONDS) * cm.TELEMETRY_GRID_SECONDS + cm.ANCHOR_RESIDUE
    for k in range(0, 3 * cm.TOTAL_SECONDS, 997):
        tl = sl.phase_timeline(anchor, _cm_geo(), anchor + k)
        assert _epoch(tl["next_alarm_at"]) % sl.TELEMETRY_GRID_SECONDS == 0


def test_story_clock_carries_the_phase_timeline():
    config = _geometry_config(_WALK2_ANCHOR)
    clock = sl.story_clock(config, _WALK2_NOW)
    assert clock["phase_timeline"] == sl.phase_timeline(_WALK2_ANCHOR, _cm_geo(), _WALK2_NOW)
    assert clock["phase_timeline"]["next_alarm_at"] == clock["bpfo_alarm_at"]


def test_story_clock_without_a_live_key_reports_one_clock():
    clock = sl.story_clock(_geometry_config(_WALK2_ANCHOR), _WALK2_NOW)
    assert clock["live_anchor_epoch"] == clock["anchor_epoch"] == _WALK2_ANCHOR
    assert clock["live_anchor_at"] == clock["anchor_at"] and clock["live_anchor_source"] is None


def test_story_clock_phase_follows_the_live_anchor_and_the_history_anchor_stays(monkeypatch):
    """ADR-0095: the alarm was moved without rewriting the history. The phase,
    the alarm and the timeline are on the live anchor; ``anchor_epoch`` (the
    answer key's and the full-placement CAS's anchor) is still the history's."""
    live = _WALK2_ANCHOR + 1800
    config = {**_geometry_config(_WALK2_ANCHOR), sl.LIVE_ANCHOR_KEY: repr(float(live))}
    clock = sl.story_clock(config, _WALK2_NOW)
    one = sl.story_clock(_geometry_config(live), _WALK2_NOW)
    assert clock["anchor_epoch"] == _WALK2_ANCHOR and clock["live_anchor_epoch"] == live
    assert clock["live_anchor_source"] == "placed-live"
    for key in ("phase", "loop_step", "bpfo_alarm_at", "phase_timeline", "next_phase_at", "loop_started_at"):
        assert clock[key] == one[key], key
    assert sl._anchor(config) == _WALK2_ANCHOR, "the answer key and the maintenance window stay on the history"


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "nan"])
def test_a_malformed_live_key_falls_back_to_the_history_anchor(raw):
    config = {**_geometry_config(_WALK2_ANCHOR), sl.LIVE_ANCHOR_KEY: raw}
    clock = sl.story_clock(config, _WALK2_NOW)
    assert clock["live_anchor_epoch"] == _WALK2_ANCHOR and clock["live_anchor_source"] is None


def test_phase_timeline_uses_the_stored_geometry_not_constants():
    """A shorter stored loop (e.g. a packaged 3 h geometry) needs no code change."""
    geo = {"loop_seconds": 10800, "interval_seconds": 5, "bpfo_alarm_step": 1462,
           "fail_step": 1944, "repair_steps": 90}
    tl = sl.phase_timeline(_ANCHOR, geo, _ANCHOR + 10)
    assert [p["end_offset_s"] for p in tl["phases"]] == [7310, 9720, 10170, 10800]
    assert tl["seconds_to_alarm"] == 7300


@pytest.mark.parametrize("over", [{"bpfo_alarm_step": 5184}, {"repair_steps": 576},
                                  {"loop_seconds": 27000}])
def test_phase_timeline_refuses_a_geometry_whose_phases_do_not_tile(over):
    geo = {**_cm_geo(), **over}
    with pytest.raises(sl._Missing, match="tile"):
        sl.phase_timeline(_ANCHOR, geo, _ANCHOR)


_NON_TILING = [{"story_bpfo_alarm_step": "5184"}, {"story_repair_steps": "576"},
               {"story_loop_seconds": "27000"}]


@pytest.mark.parametrize("over", _NON_TILING)
def test_a_non_tiling_stored_geometry_keeps_the_clock_with_a_null_timeline(over):
    """The clock (tab + chip = the UI path for re-placing) stays; only the
    timeline is withheld, with ONE clock warning."""
    warnings = []
    clock = sl.story_clock(_geometry_config(**over), _ANCHOR + 60, warnings)
    assert clock["phase_timeline"] is None
    assert [w for w in warnings if w.startswith("clock:")] == warnings and len(warnings) == 1
    assert "tile" in warnings[0]
    for key in ("anchor_epoch", "anchor_at", "phase", "next_phase", "next_phase_at",
                "loop_number", "loop_step", "telemetry", "anchor_source", "bpfo_alarm_at"):
        assert key in clock, key


@pytest.mark.parametrize("over", _NON_TILING)
def test_live_lookup_with_a_non_tiling_geometry_keeps_the_clock(over):
    config = _geometry_config(story_failed_run_number=_HISTORY_RUN, story_history_days="3",
                              story_deferred_pm_wonum="3101", **over)
    out = sl.lookup("live", db=FakeDb(_live_responses(config)), now=_ANCHOR + 60)
    assert out["clock"] is not None and out["clock"]["phase_timeline"] is None
    assert out["clock"]["anchor_epoch"] == _ANCHOR
    clock_warnings = [w for w in out["warnings"] if w.startswith("clock:")]
    assert len(clock_warnings) == 1 and "tile" in clock_warnings[0]


def test_legacy_phase_keys_agree_with_the_timeline_over_random_geometries_and_anchors():
    """The legacy ``phase``/``next_phase``/``next_phase_at`` and the timeline
    come from the same spans: they agree for ANY tiling stored geometry."""
    rng = random.Random(23)
    for _ in range(500):
        interval = rng.choice([1, 5, 30, 60, 300])
        alarm = rng.randint(1, 400)
        fail = alarm + rng.randint(1, 200)
        repair = rng.randint(1, 100)
        loop = (fail + repair) * interval + rng.randint(1, 5000)
        config = {"story_anchor_epoch": str(float(rng.randrange(1, 2_000_000_000))),
                  "story_loop_seconds": str(loop), "story_interval_seconds": str(interval),
                  "story_bpfo_alarm_step": str(alarm), "story_fail_step": str(fail),
                  "story_repair_steps": str(repair)}
        anchor = float(config["story_anchor_epoch"])
        now = anchor + rng.uniform(-3 * loop, 20 * loop)
        clock = sl.story_clock(config, now)
        tl = clock["phase_timeline"]
        assert clock["phase"] == tl["current"]
        assert clock["next_phase"] == tl["next_phase"]
        assert clock["next_phase_at"] == tl["next_phase_at"]
        assert clock["loop_started_at"] == tl["loop_started_at"]
        assert clock["next_loop_at"] == tl["loop_ends_at"]


# ── Clock provenance + telemetry sweep state (ADR-0088/0089) ─────────────────

def test_clock_reports_anchor_source_and_telemetry_state():
    grid = cm.TELEMETRY_GRID_SECONDS
    now = _ANCHOR + 7 * grid
    config = _geometry_config(story_anchor_source="placed",
                              story_telemetry_anchor=repr(_ANCHOR),
                              story_telemetry_cursor_epoch=str(int(now - grid)))
    clock = sl.story_clock(config, now)
    assert clock["anchor_source"] == "placed"
    assert clock["telemetry"] == {"anchor": _ANCHOR, "cursor_epoch": int(now - grid),
                                  "backfill_complete": True}


@pytest.mark.parametrize("into_step", [0, 1, 150, 299])
@pytest.mark.parametrize("behind,complete", [(0, True), (300, True), (301, False), (600, False), (86400, False)])
def test_backfill_complete_is_the_cursor_within_one_grid_step_of_the_last_grid_point(into_step, behind, complete):
    """Measured from ``floor(now/300)*300`` (the last grid point the sweep can
    have published), not from ``now``: late in a step the cursor one grid point
    behind is still complete."""
    grid = cm.TELEMETRY_GRID_SECONDS
    floor = (int(_ANCHOR) // grid + 400) * grid
    config = _geometry_config(story_telemetry_cursor_epoch=str(floor - behind))
    assert sl.story_clock(config, floor + into_step)["telemetry"]["backfill_complete"] is complete


def test_backfill_cursor_matches_the_history_writer_fresh_era():
    """A fresh era's cursor (story_history) is NOT complete until the sweep runs."""
    days = 3
    cursor = story_history.telemetry_cursor(_ANCHOR, days)
    config = _geometry_config(story_telemetry_cursor_epoch=str(cursor))
    assert sl.story_clock(config, _ANCHOR + 60)["telemetry"]["backfill_complete"] is False


@pytest.mark.parametrize("bad", [None, "", "soon", "nan", "inf"])
def test_clock_provenance_is_null_safe(bad):
    extra = {} if bad is None else {"story_anchor_source": bad or None,
                                    "story_telemetry_anchor": bad,
                                    "story_telemetry_cursor_epoch": bad}
    extra = {k: v for k, v in extra.items() if v is not None}
    clock = sl.story_clock(_geometry_config(**extra), _ANCHOR + 60)
    assert clock["telemetry"] == {"anchor": None, "cursor_epoch": None, "backfill_complete": None}
    assert clock["anchor_source"] == extra.get("story_anchor_source")


def test_live_lookup_clock_carries_the_new_fields():
    config = _geometry_config(story_failed_run_number=_HISTORY_RUN, story_history_days="3",
                              story_deferred_pm_wonum="3101", story_anchor_source="deploy",
                              story_telemetry_anchor=repr(_ANCHOR),
                              story_telemetry_cursor_epoch=str(int(_ANCHOR)))
    out = sl.lookup("live", db=FakeDb(_live_responses(config)), now=_ANCHOR + 60)
    assert out["warnings"] == []
    assert out["clock"]["anchor_source"] == "deploy"
    assert out["clock"]["telemetry"] == {"anchor": _ANCHOR, "cursor_epoch": int(_ANCHOR),
                                         "backfill_complete": True}


# ── Drift guards: the lookup's copies match the simulator / history / workshop ─

def test_config_keys_match_story_history():
    assert sl.CONFIG_KEYS == story_history.CONFIG_KEYS
    assert sl.TELEMETRY_GRID_SECONDS == cm.TELEMETRY_GRID_SECONDS


def test_story_identities_match_the_simulator():
    assert sl.STORY_EQUIPMENT_ID == cm.STORY_MES_EQUIPMENT_ID
    assert sl.STORY_MES_LINE == cm.STORY_MES_LINE
    assert sl.STORY_CMMS_ASSET_NUM == cm.STORY_CMMS_ASSET_NUM
    site = MESSimulator.CONDITION_MONITOR_SITE
    assert sl.schema("cmms") == MESSimulator.CMMS_SITE_TARGET[site][0]
    prefix = sl.CONDITION_WONUM_PATTERN.rstrip("%")
    assert MESSimulator._condition_wonum(cm.TOTAL_SECONDS * 3).startswith(prefix)
    assert sl.DEFECT_CHECK_TYPE == story_live.DEFECT_CHECK_TYPE
    assert sl.CONFIG_KEYS[0] == story_live.ANCHOR_KEY
    assert sl.STORY_CMMS_ASSET_NUM == story_history.cm.STORY_CMMS_ASSET_NUM
    assert sl.HISTORY_REPAIR_WONUMS == (story_history.CONDITION_WONUM,
                                        story_history.REPAIR_WONUM)


def test_schemas_derive_from_the_workshop_and_demo_manifests():
    by_id = {s["id"]: s for s in workshop.WORKSHOP_SEEDED_SYSTEMS}
    assert sl.schema("frozen_mes") == by_id["mes-ind"]["schema"]
    assert sl.schema("live_mes") == workshop.LIVE_MES_SYSTEM["schema"]
    assert sl.schema("cmms") == workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS[0]["schema"]
    assert sl.schema("plm") == next(s["schema"] for s in systems.DEMO_SYSTEMS if s["id"] == "plm")
    assert sl.schema("erp") == sl.ERP_SCHEMA
    # connection grouping: live MES + ERP share the ERP database, the rest the seed
    assert db_routing.is_erp_schema(sl.schema("erp")) and db_routing.is_erp_schema(sl.schema("live_mes"))
    for role in ("frozen_mes", "cmms", "plm"):
        assert not db_routing.is_erp_schema(sl.schema(role))


def test_every_template_placeholder_is_a_known_schema_role():
    import string
    for name, sql in _sql_constants().items():
        roles = {f for _t, f, _s, _c in string.Formatter().parse(sql) if f}
        assert roles and roles <= set(sl._SCHEMA_SOURCES), (name, roles)
        assert "{" not in sl.render(sql), name


def test_schema_resolution_is_lazy_and_a_broken_manifest_is_a_section_warning(monkeypatch):
    import importlib
    db = FakeDb(_frozen_responses())                # rendered while the manifest is intact
    monkeypatch.setattr(workshop, "WORKSHOP_SEEDED_SYSTEMS", [])
    try:
        importlib.reload(sl)            # import never resolves a schema: no raise
        with pytest.raises(sl._Missing, match="frozen_mes schema unresolvable"):
            sl.schema("frozen_mes")
        out = sl.lookup("frozen", db=db, now=_ANCHOR)
    finally:
        monkeypatch.undo()
        importlib.reload(sl)
    assert out["failed_batch"] is None
    assert out["warnings"][0] == "failed_batch: frozen_mes schema unresolvable (ValueError)"
    assert out["maintenance"]["work_orders"]          # the other sections still answer


def test_unknown_or_unsafe_schema_role_is_a_miss(monkeypatch):
    with pytest.raises(sl._Missing):
        sl.schema("nope")
    monkeypatch.setitem(sl._SCHEMA_SOURCES, "cmms", lambda: "x; DROP")
    with pytest.raises(sl._Missing, match="cmms schema unresolvable"):
        sl.render(sl.SQL_MAINTENANCE)


def _sql_constants():
    return {n: v for n, v in vars(sl).items() if n.startswith("SQL_")}


def test_every_statement_is_limited_and_parameterized():
    statements = _sql_constants()
    assert len(statements) >= 8
    for name, sql in statements.items():
        assert re.search(r"\bLIMIT\b", sql), name
        # no quoted VALUES: the only quoted text is the regexp_replace arguments
        quoted = re.findall(r"'([^']*)'", sql)
        assert set(quoted) <= {"[^0-9]", "", "g"}, (name, quoted)
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|CREATE)\b", sql), name


def test_unsafe_identifier_is_refused():
    with pytest.raises(ValueError):
        sl._ident("x; DROP")


# ── Pins: the duplicated predicate constants select exactly the story rows ────

def _types_clause(row, bearing_types) -> bool:
    """Python mirror of the shared maintenance predicate (minus the wonum LIKE)."""
    return ((row["work_type"] == sl.PM_WORK_TYPE and row["status"] == sl.OPEN_PM_STATUS)
            or (row["work_type"] in bearing_types
                and row["failure_code"] == sl.BEARING_FAILURE_CODE))


def test_every_history_cmms_row_satisfies_the_live_predicate():
    """Each ``plan().cmms_work_orders`` row passes the LIVE SQL predicate —
    by its type/status clause (so PM_WORK_TYPE, OPEN_PM_STATUS,
    LIVE_BEARING_WORK_TYPES and BEARING_FAILURE_CODE are pinned, not bypassed
    by the wonum clause) AND by the era window."""
    story_plan = story_history.plan(_ANCHOR)
    config = {row.key: row.value for row in story_plan.config}
    since, history_wonums = sl.live_window(config)
    prefix = sl.CONDITION_WONUM_PATTERN.rstrip("%")
    story_orders = [wo for wo in story_plan.cmms_work_orders
                    if wo.wonum in story_history.STORY_ORDER_WONUMS]
    assert len(story_orders) == len(story_history.STORY_ORDER_WONUMS)
    for wo in story_orders:
        row = {"work_type": wo.work_type, "status": wo.status, "failure_code": wo.failure_code}
        assert _types_clause(row, sl.LIVE_BEARING_WORK_TYPES), wo.wonum
        assert _types_clause(row, sl.LIVE_BEARING_WORK_TYPES) or wo.wonum.startswith(prefix)
        assert wo.reported_date >= since or wo.wonum in history_wonums, wo.wonum
    assert config["story_deferred_pm_wonum"] in history_wonums
    # The PM's last completed cycle is routine history, not a story order: the
    # answer key must not list it (it fails the type clause AND the era window).
    done = next(wo for wo in story_plan.cmms_work_orders
                if wo.wonum == story_history.LAST_PM_WONUM)
    row = {"work_type": done.work_type, "status": done.status, "failure_code": done.failure_code}
    assert not _types_clause(row, sl.LIVE_BEARING_WORK_TYPES)
    assert done.reported_date < since and done.wonum not in history_wonums


def _frozen_seed():
    from . import test_seed_defect_chain as chain   # read-only reuse of the seed parsers
    return chain


def _first_fields(chain, sql: str, table: str) -> list[str]:
    block = chain._insert_blocks(sql, table)
    return [chain._unq(chain._extract_tuple(block, m.start())[0])
            for m in re.finditer(r"^\(", block, re.M)]


def test_frozen_batch_predicate_matches_exactly_the_stopped_story_run():
    chain = _frozen_seed()
    batches = _first_fields(chain, chain._MES_SQL, "production_runs")
    assert len(batches) > 1
    hits = [b for b in batches if (run := chain._mes_run(b))["status"] == sl.FROZEN_FAILED_STATUS
            and run["equipment"] == sl.STORY_EQUIPMENT_ID]
    assert hits == ["B-2847"]


def test_frozen_part_predicate_matches_exactly_one_released_item():
    chain = _frozen_seed()
    digits = re.sub(r"\D", "", chain._mes_run("B-2847")["product"])
    hits = [i["item_id"] for i in chain._plm_items()
            if i["lifecycle"] == sl.RELEASED_STATE and re.sub(r"\D", "", i["item_id"]) == digits]
    assert hits == ["PN-7742-REV-C"]


def test_frozen_maintenance_predicate_matches_exactly_the_deferred_pm_and_bearing_em():
    chain = _frozen_seed()
    wonums = _first_fields(chain, chain._CMMS_SQL, "work_orders")
    rows = {w: chain._cmms_wo(w) for w in wonums}
    hits = {w: (r["work_type"], r["status"], r["failure_code"]) for w, r in rows.items()
            if r["asset"] == sl.STORY_CMMS_ASSET_NUM
            and _types_clause(r, sl.FROZEN_BEARING_WORK_TYPES)}
    assert hits == {"1182": (sl.PM_WORK_TYPE, sl.OPEN_PM_STATUS, None),
                    "1195": (sl.FROZEN_BEARING_WORK_TYPES[0], "COMP", sl.BEARING_FAILURE_CODE)}
    # no frozen-era condition-monitor order sneaks in through the live wonum clause
    assert not [w for w in wonums if w.startswith(sl.CONDITION_WONUM_PATTERN.rstrip("%"))]


def test_cancelled_order_status_is_a_db_init_customer_order_status():
    ddl = (_REPO_ROOT / "lambda-db-init" / "handler.py").read_text()
    table = re.search(r"CREATE TABLE IF NOT EXISTS customer_orders \((.*?)\n\);", ddl, re.S)
    check = re.search(r"status VARCHAR\(20\)[^\n]*CHECK \(status IN \(([^)]*)\)", table.group(1))
    statuses = re.findall(r"'([A-Z_]+)'", check.group(1))
    assert sl.CANCELLED_ORDER_STATUS in statuses


# ── Frozen story ──────────────────────────────────────────────────────────────

_ITEM = "PN-1234-REV-C"


def _frozen_responses(**over):
    responses = {
        sl.SQL_FROZEN_BATCH: [{"batch_number": "B-0001", "work_order_ref": "WO-0001",
                               "equipment_id": sl.STORY_EQUIPMENT_ID, "product_code": "1234",
                               "status": sl.FROZEN_FAILED_STATUS, "reject_qty": 148}],
        sl.SQL_RELEASED_BY_DIGITS: [{"item_id": _ITEM, "lifecycle_state": sl.RELEASED_STATE}],
        sl.SQL_PART_CUSTOMERS: [
            {"customer_code": "C-1", "company_name": "Example Corp A", "orders": 2, "value": 1000.5},
            {"customer_code": "C-2", "company_name": "Example Corp B", "orders": 1, "value": 250.25}],
        sl.SQL_MAINTENANCE: [
            {"wonum": "2", "work_type": "EM", "status": "COMP", "failure_code": sl.BEARING_FAILURE_CODE},
            {"wonum": "1", "work_type": sl.PM_WORK_TYPE, "status": sl.OPEN_PM_STATUS}],
    }
    responses.update(over)
    return responses


def test_frozen_assembles_the_seeded_chain():
    db = FakeDb(_frozen_responses())
    out = sl.lookup("frozen", db=db, now=_ANCHOR)
    assert out["story"] == "frozen" and out["clock"] is None and out["warnings"] == []
    assert out["failed_batch"]["batch_number"] == "B-0001"
    assert out["failed_batch"]["source_schema"] == sl.schema("frozen_mes")
    assert out["part"]["item_id"] == _ITEM
    assert [c["customer_code"] for c in out["customers"]] == ["C-1", "C-2"]
    assert out["total_value"] == 1250.75
    assert out["maintenance"]["deferred_pm_wonum"] == "1"
    assert out["maintenance"]["asset_num"] == sl.STORY_CMMS_ASSET_NUM
    assert set(out) == {"story", "generated_at", "clock", "failed_batch", "part",
                        "maintenance", "customers", "total_value", "warnings",
                        "live_defect_run"}
    assert out["live_defect_run"] is None          # frozen: nothing runs
    assert not db.params_for(sl.SQL_LIVE_DEFECT_RUN)
    # parameters: story machine, the product-code DIGITS, the part id
    assert db.params_for(sl.SQL_FROZEN_BATCH) == [(sl.FROZEN_FAILED_STATUS, sl.STORY_EQUIPMENT_ID)]
    assert db.params_for(sl.SQL_RELEASED_BY_DIGITS) == [(sl.RELEASED_STATE, "1234")]
    assert db.params_for(sl.SQL_PART_CUSTOMERS) == [
        (_ITEM, sl.CANCELLED_ORDER_STATUS, sl.CUSTOMER_LIMIT)]
    assert db.params_for(sl.SQL_MAINTENANCE) == [(
        sl.STORY_CMMS_ASSET_NUM, sl.PM_WORK_TYPE, sl.OPEN_PM_STATUS, sl.FROZEN_BEARING_WORK_TYPES,
        sl.BEARING_FAILURE_CODE, None, *sl.SIMULATOR_COMPLETED_PARAMS, sl.ROW_LIMIT)]
    assert not db.params_for(sl.SQL_LIVE_MAINTENANCE)
    assert not db.params_for(sl.SQL_STORY_CONFIG)


def test_frozen_ambiguous_part_warns_and_skips_customers():
    two = [{"item_id": _ITEM}, {"item_id": _ITEM + "X"}]
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_RELEASED_BY_DIGITS: two})),
                    now=_ANCHOR)
    assert out["part"] is None and out["customers"] == [] and out["total_value"] is None
    assert any(w.startswith("part: expected exactly one") for w in out["warnings"])
    assert any(w.startswith("customers: skipped") for w in out["warnings"])
    assert out["maintenance"] is not None


def test_frozen_missing_batch_warns_and_keeps_maintenance():
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_FROZEN_BATCH: []})), now=_ANCHOR)
    assert out["failed_batch"] is None
    assert out["warnings"][0].startswith("failed_batch: no ")
    assert out["maintenance"]["work_orders"]


def test_failing_source_is_a_warning_never_a_raise(caplog):
    boom = RuntimeError("secret host detail 10.1.2.3:5432")
    db = FakeDb(_frozen_responses(**{sl.SQL_MAINTENANCE: boom}))
    with caplog.at_level("WARNING", logger=sl.logger.name):
        out = sl.lookup("frozen", db=db, now=_ANCHOR)
    assert out["maintenance"] is None
    assert "maintenance: source unavailable (RuntimeError)" in out["warnings"]
    assert not any("secret host detail" in w for w in out["warnings"])
    # server-side: the message IS logged, digit-masked
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "secret host detail ##.#.#.#:####" in logged and "10.1.2.3" not in logged


def test_no_matching_orders_is_an_empty_answer_with_a_warning():
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_PART_CUSTOMERS: []})),
                    now=_ANCHOR)
    assert out["customers"] == [] and out["total_value"] == 0
    assert out["warnings"] == ["customers: no matching orders in the ERP yet"]


def test_maintenance_truncation_warns():
    many = [{"wonum": str(i), "work_type": "EM"} for i in range(sl.ROW_LIMIT)]
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_MAINTENANCE: many})),
                    now=_ANCHOR)
    assert out["warnings"] == [f"maintenance: list truncated at {sl.ROW_LIMIT}"]
    short = many[:-1]
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_MAINTENANCE: short})),
                    now=_ANCHOR)
    assert out["warnings"] == []


def test_customer_truncation_warns():
    many = [{"customer_code": f"C-{i}", "value": 1} for i in range(sl.CUSTOMER_LIMIT)]
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_PART_CUSTOMERS: many})),
                    now=_ANCHOR)
    assert f"customers: list truncated at {sl.CUSTOMER_LIMIT}" in out["warnings"]


# ── Live story ────────────────────────────────────────────────────────────────

_HISTORY_RUN = "RUN-20260101-90001"
_LIVE_RUN = "RUN-20260103-12345"
_STORY_RUN = "RUN-20260104-80002"     # story_live's RUNNING story work order


def _live_responses(config=None, **over):
    config = _geometry_config(story_failed_run_number=_HISTORY_RUN, story_history_days="3",
                              story_deferred_pm_wonum="3101") if config is None else config
    responses = {
        sl.SQL_STORY_CONFIG: _config_rows(config),
        sl.SQL_LIVE_BATCH: [{"work_order_number": "WO-1", "run_number": _LIVE_RUN,
                             "line": sl.STORY_MES_LINE, "engineering_part_number": _ITEM,
                             "dimensional_fail_count": 7}],
        sl.SQL_LIVE_DEFECT_RUN: [{"work_order_number": "WO-20260104-80002",
                                  "run_number": _STORY_RUN, "line": sl.STORY_MES_LINE,
                                  "status": "RUNNING", "engineering_part_number": _ITEM,
                                  "dimensional_fail_count": 3}],
        sl.SQL_PLM_ITEM: [{"item_id": _ITEM, "lifecycle_state": sl.RELEASED_STATE}],
        sl.SQL_BATCH_CUSTOMERS: [{"customer_code": "C-1", "orders": 1, "value": Decimal("10.10")}],
        sl.SQL_BATCH_LOTS: [{"lot_number": "LOT-1", "supplier_code": "SUP-1"}],
        sl.SQL_LOT_SHIPPED_BATCHES: [{"run_number": _LIVE_RUN}, {"run_number": "RUN-20260102-91002"}],
        sl.SQL_LIVE_MAINTENANCE: [{"wonum": "3101", "work_type": sl.PM_WORK_TYPE,
                                   "status": sl.OPEN_PM_STATUS}],
    }
    responses.update(over)
    return responses


def test_live_assembles_clock_batch_customers_and_maintenance():
    db = FakeDb(_live_responses())
    now = _ANCHOR + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1
    out = sl.lookup("live", db=db, now=now)
    assert out["warnings"] == []
    assert out["clock"]["phase"] == cm.phase(cm.loop_step(cm.story_seconds(now, _ANCHOR)))
    assert out["failed_batch"]["run_number"] == _LIVE_RUN
    assert out["failed_batch"]["history_run_number"] == _HISTORY_RUN
    assert out["failed_batch"]["is_history_run"] is False
    assert out["part"]["item_id"] == _ITEM
    assert out["total_value"] == 10.1
    assert out["maintenance"]["deferred_pm_wonum"] == "3101"
    # the failed batch: shipped, or the history's own failed run
    assert db.params_for(sl.SQL_LIVE_BATCH) == [
        (sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, _HISTORY_RUN)]
    # the live defect run: a failing story-line run that is NEITHER the failed
    # batch NOR the history's failed run (a newer shipped batch displaced it)
    assert db.params_for(sl.SQL_LIVE_DEFECT_RUN) == [
        (sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, sorted([_LIVE_RUN, _HISTORY_RUN]))]
    assert out["live_defect_run"]["run_number"] == _STORY_RUN
    assert out["live_defect_run"]["source_schema"] == sl.schema("live_mes")
    assert out["live_defect_run"]["history_run_number"] == _HISTORY_RUN
    assert out["live_defect_run"]["is_history_run"] is False
    assert db.params_for(sl.SQL_PLM_ITEM) == [(_ITEM,)]
    # the batch's customers, then the suspect lot's (same query, the lot's shipped batches)
    assert db.params_for(sl.SQL_BATCH_CUSTOMERS) == [
        (sorted([_LIVE_RUN, _HISTORY_RUN]), sl.CUSTOMER_LIMIT),
        ([_LIVE_RUN, "RUN-20260102-91002"], sl.CUSTOMER_LIMIT)]
    assert db.params_for(sl.SQL_BATCH_LOTS) == [(_LIVE_RUN, sl.ROW_LIMIT)]
    assert db.params_for(sl.SQL_LOT_SHIPPED_BATCHES) == [(["LOT-1"], sl.ROW_LIMIT)]
    assert out["suspect_lot"] == {
        "source_schema": sl.schema("erp"), "lots": [{"lot_number": "LOT-1", "supplier_code": "SUP-1"}],
        "shipped_batches": [_LIVE_RUN, "RUN-20260102-91002"],
        "customers": [{"customer_code": "C-1", "orders": 1, "value": Decimal("10.10")}],
        "total_value": 10.1}
    since = datetime.fromtimestamp(_ANCHOR - 3 * 86400, tz=timezone.utc).replace(tzinfo=None)
    assert db.params_for(sl.SQL_LIVE_MAINTENANCE) == [(
        sl.STORY_CMMS_ASSET_NUM, sl.PM_WORK_TYPE, sl.OPEN_PM_STATUS, sl.LIVE_BEARING_WORK_TYPES,
        sl.BEARING_FAILURE_CODE, sl.CONDITION_WONUM_PATTERN, *sl.SIMULATOR_COMPLETED_PARAMS,
        since, ["3101", *sl.HISTORY_REPAIR_WONUMS], sl.ROW_LIMIT)]
    assert not db.params_for(sl.SQL_MAINTENANCE)
    assert db.params_for(sl.SQL_STORY_CONFIG) == [(list(sl.READ_KEYS), len(sl.READ_KEYS))]


def test_live_history_run_is_flagged_when_it_is_the_latest():
    batch = [{"run_number": _HISTORY_RUN, "engineering_part_number": _ITEM}]
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_BATCH: batch}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["failed_batch"]["is_history_run"] is True
    assert db.params_for(sl.SQL_BATCH_CUSTOMERS)[0] == ([_HISTORY_RUN], sl.CUSTOMER_LIMIT)


def test_live_suspect_lot_missing_is_a_warning_and_frozen_has_none():
    db = FakeDb(_live_responses(**{sl.SQL_BATCH_LOTS: []}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["suspect_lot"] is None
    assert [w for w in out["warnings"] if w.startswith("suspect_lot:")] == [
        f"suspect_lot: no supplier lot recorded for {_LIVE_RUN}"]
    assert not db.params_for(sl.SQL_LOT_SHIPPED_BATCHES)


def test_live_suspect_lot_with_no_shipped_batch_has_no_customers():
    db = FakeDb(_live_responses(**{sl.SQL_LOT_SHIPPED_BATCHES: []}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["suspect_lot"]["shipped_batches"] == [] and out["suspect_lot"]["customers"] == []
    assert len(db.params_for(sl.SQL_BATCH_CUSTOMERS)) == 1


def test_live_without_story_config_warns_and_still_answers():
    db = FakeDb(_live_responses(config={}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["clock"] is None
    clock = [w for w in out["warnings"] if w.startswith("clock:")]
    assert len(clock) == 1 and "story_anchor_epoch" in clock[0]      # deduped: one clock warning
    assert out["failed_batch"]["history_run_number"] is None
    assert db.params_for(sl.SQL_LIVE_BATCH) == [(sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, None)]
    assert db.params_for(sl.SQL_BATCH_CUSTOMERS)[0] == ([_LIVE_RUN], sl.CUSTOMER_LIMIT)
    assert out["maintenance"]["deferred_pm_wonum"] == "3101"   # falls back to the open PM
    # no story window: unrestricted (epoch 0, no history wonums) and SAID so
    (maint,) = db.params_for(sl.SQL_LIVE_MAINTENANCE)
    assert maint[10:12] == (datetime(1970, 1, 1), [])
    assert any(w.startswith("maintenance: ") and "not restricted to the live era" in w
               for w in out["warnings"])


def test_live_window_from_config():
    config = _geometry_config(story_history_days="2", story_deferred_pm_wonum="77")
    since, wonums = sl.live_window(config)
    assert since == datetime.fromtimestamp(_ANCHOR - 2 * 86400, tz=timezone.utc).replace(tzinfo=None)
    assert wonums == ["77", *sl.HISTORY_REPAIR_WONUMS]
    assert sl.live_window(_geometry_config(story_history_days="1"))[1] == list(sl.HISTORY_REPAIR_WONUMS)
    for bad in ({}, {"story_history_days": "0"}, {"story_history_days": "x"}):
        config = {k: v for k, v in _geometry_config().items()}
        config.update(bad)
        with pytest.raises(sl._Missing, match="story_history_days"):
            sl.live_window(config)


def test_live_malformed_geometry_warns():
    config = _geometry_config(story_interval_seconds="soon")
    out = sl.lookup("live", db=FakeDb(_live_responses(config=config)), now=_ANCHOR)
    assert out["clock"] is None
    assert any("story_interval_seconds" in w for w in out["warnings"])


def test_live_no_failing_work_order_and_no_history_skips_customers():
    db = FakeDb(_live_responses(config={}, **{sl.SQL_LIVE_BATCH: []}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["failed_batch"] is None and out["customers"] == []
    assert any(w.startswith("customers: skipped") for w in out["warnings"])
    assert not db.params_for(sl.SQL_BATCH_CUSTOMERS)
    # no failed batch to exclude: the live defect run query still runs
    assert db.params_for(sl.SQL_LIVE_DEFECT_RUN) == [(sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, [])]
    assert out["live_defect_run"]["run_number"] == _STORY_RUN


def test_live_before_the_first_alarm_has_no_live_defect_run_and_no_warning():
    """History only (no story run yet): the key is None and that is not a miss."""
    history = [{"run_number": _HISTORY_RUN, "engineering_part_number": _ITEM}]
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_BATCH: history, sl.SQL_LIVE_DEFECT_RUN: []}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["failed_batch"]["run_number"] == _HISTORY_RUN
    assert out["live_defect_run"] is None
    assert out["warnings"] == []
    assert db.params_for(sl.SQL_LIVE_DEFECT_RUN) == [
        (sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, [_HISTORY_RUN])]


def test_live_story_run_never_widens_the_answer_key():
    """The live story run is reported apart: failed_batch, part and customers
    come from the shipped/history batch only."""
    history = [{"run_number": _HISTORY_RUN, "engineering_part_number": _ITEM}]
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_BATCH: history}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["failed_batch"]["run_number"] == _HISTORY_RUN
    assert out["live_defect_run"]["run_number"] == _STORY_RUN
    assert db.params_for(sl.SQL_BATCH_CUSTOMERS)[0] == ([_HISTORY_RUN], sl.CUSTOMER_LIMIT)
    assert db.params_for(sl.SQL_BATCH_LOTS) == [(_HISTORY_RUN, sl.ROW_LIMIT)]   # the lot of that batch
    assert db.params_for(sl.SQL_PLM_ITEM) == [(_ITEM,)]


def test_live_defect_run_is_never_the_displaced_history_run():
    """A newer shipped failing run is the failed batch; the history's failed
    run is then the next-newest failing LINE3 run but is NOT the live defect
    run - both are excluded, so the key only ever names the story run."""
    db = FakeDb(_live_responses())
    sl.lookup("live", db=db, now=_ANCHOR)
    ((_line, _type, excluded),) = db.params_for(sl.SQL_LIVE_DEFECT_RUN)
    assert _HISTORY_RUN in excluded and _LIVE_RUN in excluded


def test_live_defect_run_excludes_the_history_run_even_without_a_failed_batch():
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_BATCH: []}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["failed_batch"] is None
    assert db.params_for(sl.SQL_LIVE_DEFECT_RUN) == [
        (sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, [_HISTORY_RUN])]


def test_live_defect_run_source_failure_is_a_warning():
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_DEFECT_RUN: RuntimeError("down")}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["live_defect_run"] is None
    assert "live_defect_run: source unavailable (RuntimeError)" in out["warnings"]
    assert out["failed_batch"]["run_number"] == _LIVE_RUN


def test_failed_batch_is_shipped_through_the_shipping_simulators_linkage():
    """``shipment_items.batch_id`` holds ``production_runs.run_number`` — the
    value ``ShippingSimulator._traceable_batch_id`` selects and the history
    writes for its failed run — so "shipped" is that join, or the history run."""
    import inspect
    from erp_simulation.shipping_simulator import ShippingSimulator
    source = inspect.getsource(ShippingSimulator._traceable_batch_id)
    assert re.search(r"SELECT\s+pr\.run_number\s+FROM\s+production_runs pr", source)
    shipped = re.sub(r"\s+", " ", sl.SQL_LIVE_BATCH)
    assert ("(pr.run_number = %s OR EXISTS (SELECT 1 FROM {erp}.shipment_items si "
            "WHERE si.batch_id = pr.run_number))") in shipped
    defect = re.sub(r"\s+", " ", sl.SQL_LIVE_DEFECT_RUN)
    assert "NOT (pr.run_number = ANY(%s))" in defect
    # the same columns, the same newest-failure order, one row each
    head = lambda sql: re.sub(r"\s+", " ", sql).split(" WHERE ")[0]
    tail = lambda sql: re.sub(r"\s+", " ", sql).split(" GROUP BY ")[1]
    assert head(sl.SQL_LIVE_BATCH) == head(sl.SQL_LIVE_DEFECT_RUN)
    assert tail(sl.SQL_LIVE_BATCH) == tail(sl.SQL_LIVE_DEFECT_RUN)
    assert tail(sl.SQL_LIVE_BATCH).endswith("ORDER BY MAX(q.check_time) DESC LIMIT 1")


_SUPERSEDED_ROW = {"wonum": "IND-0822D3E2", "work_type": "EM", "status": "CAN",
                   "failure_code": "BEARING", "priority": 1,
                   "description": "MES-triggered: bearing on LINE3 [superseded by the live plant history]",
                   "reported_date": "2026-09-29T01:02:03"}


def test_cancelled_wo_status_is_the_history_supersede_status_and_a_cmms_status():
    assert f"SET status = '{sl.CANCELLED_WO_STATUS}'" in story_history._SUPERSEDE_FROZEN_ORDERS_SQL
    ddl = next((_REPO_ROOT / "lambda-adt-seed").glob("011_*.sql")).read_text()
    check = re.search(r"status\s+VARCHAR\(10\)[^\n]*CHECK \(status IN \(([^)]*)\)", ddl)
    assert sl.CANCELLED_WO_STATUS in re.findall(r"'([A-Z]+)'", check.group(1))


def test_completed_wo_status_is_the_simulators_condition_completion():
    """Both keys leave out exactly the orders the simulator completes once
    their loop has ended, and only those: condition orders (the reserved
    VIB-CMX number space) and orders the MES bridge reported (IND-* AND the
    bridge's reporter). A person's completed IND-* order is maintenance
    history and stays listed, like the history's own completed 3107 / 3108 and
    the seed's 1195."""
    from . import _site_cmms_rows as rows
    assert sl.COMPLETED_WO_STATUS == MESSimulator.CONDITION_COMPLETED_STATUS
    assert sl.CONDITION_WONUM_PATTERN == MESSimulator._loop_wonum(0).rstrip("0") + "%"
    bridge = rows.bridge_order(rows.seed_rows("assets"), "MECHANICAL", "VIB-BEARING",
                               "0822d3e2-1f2e-4a5b-8c6d-000000000001")
    assert sl.BRIDGE_WONUM_PATTERN == MESSimulator.CMMS_BRIDGE_WONUM_PREFIX + "%"
    assert bridge["wonum"].startswith(sl.BRIDGE_WONUM_PATTERN.rstrip("%"))
    assert sl.BRIDGE_REPORTED_BY == MESSimulator.CMMS_BRIDGE_REPORTED_BY == bridge["reported_by"]
    clause = ("AND NOT (status = %s AND (wonum LIKE %s OR (wonum LIKE %s AND "
              "reported_by IS NOT DISTINCT FROM %s)))")
    assert clause in sl.SQL_LIVE_MAINTENANCE and clause in sl.SQL_MAINTENANCE
    assert sl.SIMULATOR_COMPLETED_PARAMS == (sl.COMPLETED_WO_STATUS, sl.CONDITION_WONUM_PATTERN,
                                             sl.BRIDGE_WONUM_PATTERN, sl.BRIDGE_REPORTED_BY)
    prefixes = (sl.CONDITION_WONUM_PATTERN.rstrip("%"), sl.BRIDGE_WONUM_PATTERN.rstrip("%"))
    assert not [w for w in story_history.STORY_ORDER_WONUMS if w.startswith(prefixes)]
    assert not [r["wonum"] for r in rows.seed_rows("work_orders") if r["wonum"].startswith(prefixes)]


def test_live_cancelled_orders_move_to_superseded_work_orders():
    rows = [dict(_SUPERSEDED_ROW),
            {"wonum": "3108", "work_type": "EM", "status": "COMP",
             "failure_code": sl.BEARING_FAILURE_CODE},
            {"wonum": "3101", "work_type": sl.PM_WORK_TYPE, "status": sl.OPEN_PM_STATUS}]
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_MAINTENANCE: rows}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    maint = out["maintenance"]
    assert [w["wonum"] for w in maint["work_orders"]] == ["3108", "3101"]
    assert maint["superseded_work_orders"] == [{
        "wonum": "IND-0822D3E2", "status": sl.CANCELLED_WO_STATUS, "work_type": "EM",
        "description": _SUPERSEDED_ROW["description"],
        "reported_date": _SUPERSEDED_ROW["reported_date"]}]
    assert maint["deferred_pm_wonum"] == "3101"
    assert out["warnings"] == []
    # the SQL is unchanged: the split is in Python, after the same query
    assert len(db.params_for(sl.SQL_LIVE_MAINTENANCE)) == 1


def test_live_without_cancelled_orders_has_an_empty_superseded_list():
    out = sl.lookup("live", db=FakeDb(_live_responses()), now=_ANCHOR)
    assert out["maintenance"]["superseded_work_orders"] == []
    assert [w["wonum"] for w in out["maintenance"]["work_orders"]] == ["3101"]


def test_live_maintenance_failure_is_still_one_warning():
    db = FakeDb(_live_responses(**{sl.SQL_LIVE_MAINTENANCE: RuntimeError("down")}))
    out = sl.lookup("live", db=db, now=_ANCHOR)
    assert out["maintenance"] is None
    assert "maintenance: source unavailable (RuntimeError)" in out["warnings"]


# Frozen byte-identity pin: sha256 of json.dumps(lookup("frozen", ...),
# sort_keys=True) for the fixture below, captured from the lookup BEFORE the
# live-only superseded split existed. A CAN row stays in frozen's work_orders
# and frozen maintenance gains no key. Change it only with an intentional
# frozen shape change.
_FROZEN_SNAPSHOT_SHA256 = "bfa055a2f9142f8603155ab397b4c7421b91534ea4af2884c434dc08d296d4fc"


def test_frozen_output_is_byte_identical_with_a_cancelled_row():
    import hashlib
    import json
    can = {k: _SUPERSEDED_ROW[k] for k in ("wonum", "work_type", "status", "failure_code",
                                           "reported_date")}
    can["description"] = "x [superseded by the live plant history]"
    rows = [can, *_frozen_responses()[sl.SQL_MAINTENANCE]]
    out = sl.lookup("frozen", db=FakeDb(_frozen_responses(**{sl.SQL_MAINTENANCE: rows})),
                    now=_ANCHOR)
    assert set(out["maintenance"]) == {"source_schema", "asset_num", "deferred_pm_wonum",
                                       "work_orders"}
    assert [w["wonum"] for w in out["maintenance"]["work_orders"]] == ["IND-0822D3E2", "2", "1"]
    text = json.dumps(out, sort_keys=True)
    assert hashlib.sha256(text.encode()).hexdigest() == _FROZEN_SNAPSHOT_SHA256, text


def test_unknown_story_raises():
    with pytest.raises(ValueError):
        sl.lookup("bogus", db=FakeDb({}))


# ── Read-only DB layer ────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, log, rows=(), cols=()):
        self.log, self._rows = log, list(rows)
        self.description = [(c,) for c in cols]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows=(), cols=()):
        self.log, self.session, self.closed = [], None, False
        self._rows, self._cols = rows, cols

    def set_session(self, **kw):
        self.session = kw

    def cursor(self):
        return _Cursor(self.log, self._rows, self._cols)

    def close(self):
        self.closed = True


def test_connect_opens_a_read_only_bounded_session(monkeypatch):
    conn = _Conn()
    seen = {}

    def _fake_connect(dsn, **kw):
        seen.update(dsn=dsn, **kw)
        return conn

    from agents import pgdriver
    monkeypatch.setattr(pgdriver, "connect", _fake_connect)
    monkeypatch.setattr(db_routing, "demo_pg_dsn", lambda schema: f"dsn:{schema}")
    cmms = sl.schema("cmms")
    assert sl._connect(cmms) is conn
    assert seen == {"dsn": f"dsn:{cmms}", "connect_timeout": sl.CONNECT_TIMEOUT_S}
    assert conn.session == {"readonly": True, "autocommit": True}
    # A utility SET cannot bind a server-side parameter, so the timeout is set
    # with set_config and a str value.
    assert conn.log == [("SELECT set_config('statement_timeout', %s, false)",
                         (str(sl.STATEMENT_TIMEOUT_MS),))]


def test_read_only_db_connects_once_per_database_and_caches_failures():
    attempts = []

    def _connect(schema):
        attempts.append(schema)
        if db_routing.is_erp_schema(schema):
            raise OSError("down")
        return _Conn(rows=[(Decimal("1.50"), datetime(2026, 1, 2, 3, 4, 5))], cols=("v", "t"))

    db = sl.ReadOnlyDb(connect=_connect)
    assert db.rows(sl.schema("cmms"), "q", ()) == [{"v": 1.5, "t": "2026-01-02T03:04:05"}]
    db.rows(sl.schema("plm"), "q", ())
    for _ in range(3):
        with pytest.raises(OSError):
            db.rows(sl.ERP_SCHEMA, "q", ())
    assert len(attempts) == 2                      # one seed + one ERP attempt, no retries
    seed_conn = db._conns[False]
    db.close()
    assert seed_conn.closed and db._conns == {}


def test_lookup_closes_the_connections_it_opens(monkeypatch):
    closed = []

    class _Db:
        def rows(self, schema, sql, params):
            return []

        def close(self):
            closed.append(True)

    monkeypatch.setattr(sl, "ReadOnlyDb", _Db)
    out = sl.lookup("frozen", now=_ANCHOR)
    assert closed == [True] and out["failed_batch"] is None


# ── Design C (ADR-0091): reset default + auto-place policy on the clock ──────

_DEFAULT_KEYS_OUT = ("default_anchor_epoch", "default_anchor_iso", "default_source", "auto_place")


def test_default_keys_and_sources_mirror_the_simulator():
    from erp_simulation import story_replan
    assert sl.DEFAULT_KEYS == story_history.DEFAULT_KEYS
    assert sl.DEFAULT_SOURCES == story_replan.DEFAULT_SOURCES
    assert sl.READ_KEYS == sl.CONFIG_KEYS + sl.DEFAULT_KEYS + (sl.LIVE_ANCHOR_KEY,)
    assert sl.LIVE_ANCHOR_KEY == story_history.LIVE_ANCHOR_KEY == story_replan.LIVE_ANCHOR_KEY
    assert sl.AUTO_PLACE_ENV == "STORY_AUTO_PLACE_MINUTES"


@pytest.mark.parametrize("config", [
    {"story_default_anchor": "1789990010", "story_default_source": "deploy"},
    {"story_default_anchor": "1789990010.0", "story_default_source": "packaged"},
    {}, {"story_default_anchor": "1789990010"}, {"story_default_source": "deploy"},
    {"story_default_anchor": "1789990010", "story_default_source": "placed"},
    {"story_default_anchor": "nan", "story_default_source": "deploy"},
    {"story_default_anchor": "inf", "story_default_source": "deploy"},
    {"story_default_anchor": "0", "story_default_source": "deploy"},
    {"story_default_anchor": "x", "story_default_source": "deploy"},
])
def test_default_parse_parity_with_story_replan(config):
    from erp_simulation import story_replan
    assert sl.parse_default(config) == story_replan.parse_default(config)


def test_clock_reports_the_stored_default(monkeypatch):
    monkeypatch.delenv(sl.AUTO_PLACE_ENV, raising=False)
    clock = sl.story_clock(_geometry_config(story_default_anchor="1789990010",
                                            story_default_source="packaged"), _ANCHOR + 60)
    assert clock["default_anchor_epoch"] == 1789990010
    assert clock["default_anchor_iso"] == datetime.fromtimestamp(1789990010, tz=timezone.utc).isoformat()
    assert clock["default_source"] == "packaged"


@pytest.mark.parametrize("extra", [{}, {"story_default_anchor": "1789990010"},
                                   {"story_default_anchor": "x", "story_default_source": "deploy"},
                                   {"story_default_anchor": "1789990010", "story_default_source": "placed"}])
def test_clock_default_is_null_when_absent_or_malformed(monkeypatch, extra):
    monkeypatch.delenv(sl.AUTO_PLACE_ENV, raising=False)
    clock = sl.story_clock(_geometry_config(**extra), _ANCHOR + 60)
    assert (clock["default_anchor_epoch"], clock["default_anchor_iso"], clock["default_source"]) == (
        None, None, None)


@pytest.mark.parametrize("raw,expected", [
    (None, {"enabled": False, "minutes": 0}), ("", {"enabled": False, "minutes": 0}),
    ("0", {"enabled": False, "minutes": 0}), ("80", {"enabled": True, "minutes": 80}),
    (" 5 ", {"enabled": True, "minutes": 5}), ("480", {"enabled": True, "minutes": 480}),
])
def test_clock_publishes_the_auto_place_policy(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(sl.AUTO_PLACE_ENV, raising=False)
    else:
        monkeypatch.setenv(sl.AUTO_PLACE_ENV, raw)
    warnings = []
    assert sl.story_clock(_geometry_config(), _ANCHOR + 60, warnings)["auto_place"] == expected
    assert warnings == []


@pytest.mark.parametrize("raw", ["4", "481", "-5", "80.0", "abc", "1e2"])
def test_a_bad_auto_place_env_is_off_with_one_clock_warning(monkeypatch, raw):
    monkeypatch.setenv(sl.AUTO_PLACE_ENV, raw)
    warnings = []
    clock = sl.story_clock(_geometry_config(), _ANCHOR + 60, warnings)
    assert clock["auto_place"] == {"enabled": False, "minutes": 0}
    assert len(warnings) == 1 and warnings[0].startswith("clock:") and sl.AUTO_PLACE_ENV in warnings[0]


def test_auto_place_is_bounded_by_the_stored_loop():
    warnings = []
    assert sl.auto_place_policy("181", 10800, warnings) == {"enabled": False, "minutes": 0}
    assert sl.auto_place_policy("180", 10800, warnings) == {"enabled": True, "minutes": 180}
    assert len(warnings) == 1


def test_auto_place_is_bounded_by_the_env_loop_too(monkeypatch):
    """The place route caps alarm_in_minutes at story_clock's ENV loop, so the
    published policy is bounded by min(stored loop, env loop)."""
    monkeypatch.setenv(sl.LOOP_ENV, "10800")
    warnings = []
    assert sl.auto_place_policy("181", 28800, warnings) == {"enabled": False, "minutes": 0}
    assert sl.auto_place_policy("180", 28800, warnings) == {"enabled": True, "minutes": 180}
    assert len(warnings) == 1 and "5..180" in warnings[0]
    assert sl.auto_place_policy("120", 7200, []) == {"enabled": True, "minutes": 120}   # stored is lower


@pytest.mark.parametrize("raw", ["abc", "3000", "3601", "", "  "])
def test_an_unusable_env_loop_leaves_the_stored_bound(monkeypatch, raw):
    monkeypatch.setenv(sl.LOOP_ENV, raw)
    assert sl.auto_place_policy("480", 28800, []) == {"enabled": True, "minutes": 480}


@pytest.mark.parametrize("raw", [None, "", "3600", " 10800 ", "28800"])
def test_env_loop_parse_matches_story_clock(raw):
    from agents.demo import story_clock
    assert sl.LOOP_ENV == story_clock.LOOP_ENV
    assert sl.env_loop_seconds(raw) == story_clock.loop_seconds_from(raw)


@pytest.mark.parametrize("raw", ["abc", "3000", "3601", "28801", "-3600", "1e4"])
def test_env_loop_parse_rejects_what_story_clock_rejects(raw):
    from agents.demo import story_clock
    with pytest.raises(ValueError):
        story_clock.loop_seconds_from(raw)
    assert sl.env_loop_seconds(raw) is None

def test_live_lookup_clock_carries_the_design_c_fields(monkeypatch):
    monkeypatch.setenv(sl.AUTO_PLACE_ENV, "80")
    config = _geometry_config(story_failed_run_number=_HISTORY_RUN, story_history_days="3",
                              story_deferred_pm_wonum="3101", story_default_anchor="1789990010",
                              story_default_source="deploy")
    out = sl.lookup("live", db=FakeDb(_live_responses(config)), now=_ANCHOR + 60)
    assert out["warnings"] == []
    assert {k: out["clock"][k] for k in _DEFAULT_KEYS_OUT} == {
        "default_anchor_epoch": 1789990010,
        "default_anchor_iso": datetime.fromtimestamp(1789990010, tz=timezone.utc).isoformat(),
        "default_source": "deploy", "auto_place": {"enabled": True, "minutes": 80}}


@pytest.mark.parametrize("rows,expected", [
    ([{"config_key": "story_default_anchor", "config_value": "1789990010"},
      {"config_key": "story_default_source", "config_value": "deploy"}], (1789990010, "deploy")),
    ([], None),
])
def test_stored_default_reads_only_the_default_keys(rows, expected):
    db = FakeDb({sl.SQL_STORY_CONFIG: rows})
    assert sl.stored_default(db=db) == expected
    assert db.params_for(sl.SQL_STORY_CONFIG) == [(list(sl.DEFAULT_KEYS), len(sl.DEFAULT_KEYS))]
