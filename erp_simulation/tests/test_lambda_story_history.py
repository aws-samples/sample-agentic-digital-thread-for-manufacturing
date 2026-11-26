# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Story-clock branches of ``lambda_/lambda_story_history.py`` (ADR-0089, Task K).

The base Create / Update / Delete handler tests live with the writer in
``test_story_history.py`` (they share its FakeDB); this module covers the
anchor sources (deploy | packaged), the packaged-``Anchor`` Update re-plan and
the direct ``{"action": "place"}`` invoke. ``story_replan.replan`` is stubbed:
its own behaviour is pinned in ``test_story_replan.py``.
"""
from __future__ import annotations

import ast
import importlib.util
import math

import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import story_history as sh
from erp_simulation import story_replan as sr
from erp_simulation.tests.test_story_history import _REPO, ANCHOR, FakeDB

NOW = int(ANCHOR)
_LAMBDA = _REPO / "lambda_" / "lambda_story_history.py"
REPORT = {"old_anchor": 1789990010.0, "new_anchor": 1789999810, "source": "packaged",
          "retired": {"runs": 19}, "written": {"runs": 19}, "seed": {}, "telemetry": {"backfill": "pending"},
          "failed_run_number": "RUN-20260921-0003", "alarm_at": 1790012410, "loops_shifted": 0}


def _load():
    spec = importlib.util.spec_from_file_location("lambda_story_history_k", _LAMBDA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lam(monkeypatch):
    """(module, dbs, calls): fake DBs, a fixed clock, spies on write / replan."""
    module = _load()
    dbs = {"DB_SECRET_NAME": FakeDB(), "SEED_DB_SECRET_NAME": FakeDB()}
    calls = {"write": [], "replan": [], "connect": []}
    for db in dbs.values():
        db.close = lambda: None

    def connect(env):
        calls["connect"].append(env)
        return dbs[env]

    def replan(erp, seed, new_anchor, *, now, source, expected_anchor=None):
        assert (erp, seed) == (dbs["DB_SECRET_NAME"], dbs["SEED_DB_SECRET_NAME"])
        calls["replan"].append({"new_anchor": new_anchor, "now": now, "source": source,
                                "expected_anchor": expected_anchor})
        return dict(REPORT, source=source)

    real_write = sh.write

    def write(erp, seed, story_plan):
        calls["write"].append(story_plan)
        return real_write(erp, seed, story_plan)

    monkeypatch.setattr(module, "_connect", connect)
    monkeypatch.setattr(module, "_now", lambda: NOW)
    monkeypatch.setattr(sr, "replan", replan)
    monkeypatch.setattr(sh, "write", write)
    return module, dbs, calls


def _cfn(request, new=None, old=None, **extra):
    event = {"RequestType": request, **extra}
    if new is not None:
        event["ResourceProperties"] = new
    if old is not None:
        event["OldResourceProperties"] = old
    return event


# ---------------------------------------------------------------------------
# Create: deploy anchor or packaged anchor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("props", [{"Days": "3"}, {"Days": "3", "Anchor": ""}, {"Anchor": "  "}])
def test_create_without_anchor_writes_the_snapped_deploy_anchor(lam, props):
    module, dbs, calls = lam
    out = module.lambda_handler(_cfn("Create", props), None)
    deploy = cm.snap_anchor(NOW, "down")
    assert deploy % cm.TELEMETRY_GRID_SECONDS == cm.ANCHOR_RESIDUE and deploy <= NOW
    assert out == {"PhysicalResourceId": module.PHYSICAL_ID, "Data": {
        "AnchorEpoch": str(deploy), "AnchorSource": "deploy",
        "FailedRunNumber": sh.plan(deploy).failed_run.run_number}}
    assert dbs["DB_SECRET_NAME"].config["story_anchor_source"] == "deploy"
    assert [p.anchor_epoch for p in calls["write"]] == [deploy] and calls["replan"] == []


@pytest.mark.parametrize("reference", [NOW - 5 * 3600 + 7, NOW - 7 * 86400 + 123, NOW + 3 * 86400])
def test_create_with_anchor_writes_the_normalized_packaged_anchor(lam, reference):
    module, dbs, calls = lam
    out = module.lambda_handler(_cfn("Create", {"Days": "3", "Anchor": str(reference)}), None)
    anchor = sr.normalize_anchor(reference, NOW)
    assert NOW - sr.LOOP_SECONDS < anchor <= NOW
    assert out["Data"] == {"AnchorEpoch": str(anchor), "AnchorSource": "packaged",
                           "FailedRunNumber": sh.plan(anchor).failed_run.run_number}
    assert dbs["DB_SECRET_NAME"].config["story_anchor_source"] == "packaged"
    assert [p.anchor_epoch for p in calls["write"]] == [anchor] and calls["replan"] == []


def test_create_plans_with_the_invocation_clock_for_the_cursor_clamp(lam, monkeypatch):
    """The history plan gets ``now`` so its telemetry cursor is clamped into
    SiteWise's 7-day window (story_history.telemetry_cursor)."""
    module, _dbs, _calls = lam
    seen, real_plan = [], sh.plan
    monkeypatch.setattr(sh, "plan", lambda *a, **k: seen.append(k.get("now")) or real_plan(*a, **k))
    module.lambda_handler(_cfn("Create", {"Days": "3"}), None)
    assert seen[0] == NOW


def test_create_with_a_malformed_anchor_raises(lam):
    module, _dbs, calls = lam
    with pytest.raises(ValueError):
        module.lambda_handler(_cfn("Create", {"Anchor": "2026-09-29T14:10Z"}), None)
    assert calls["connect"] == []


def test_create_keeps_an_already_stored_anchor_and_reports_its_source(lam):
    module, dbs, _calls = lam
    first = module.lambda_handler(_cfn("Create", {"Anchor": str(NOW - 3600)}), None)
    again = module.lambda_handler(_cfn("Create", {}), None)
    assert again["Data"] == first["Data"] and again["Data"]["AnchorSource"] == "packaged"


# ---------------------------------------------------------------------------
# Update: same Anchor -> write only (stored anchor wins); changed -> replan
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("old,new", [
    (None, {"Days": "3"}),                   # OldResourceProperties missing
    (None, {"Days": "3", "Anchor": ""}),     # ... and new ""
    ({"Days": "3"}, {"Days": "3", "Anchor": ""}),
    ({"Anchor": ""}, {}),
    ({"Anchor": str(NOW - 600)}, {"Anchor": str(NOW - 600)}),
    ({"Anchor": str(NOW - 600)}, {"Anchor": ""}),  # un-setting the Anchor never moves the clock
])
def test_update_with_the_same_anchor_writes_only_and_keeps_the_stored_anchor(lam, old, new):
    module, dbs, calls = lam
    created = module.lambda_handler(_cfn("Create", {"Days": "3"}), None)
    updated = module.lambda_handler(
        _cfn("Update", new, old, PhysicalResourceId=module.PHYSICAL_ID), None)
    assert updated == created and updated["Data"]["AnchorSource"] == "deploy"
    assert len(calls["write"]) == 2 and calls["replan"] == []
    assert float(dbs["DB_SECRET_NAME"].config["story_anchor_epoch"]) == cm.snap_anchor(NOW, "down")


@pytest.mark.parametrize("old", [None, {}, {"Anchor": ""}, {"Anchor": str(NOW - 600)}])
def test_update_with_a_changed_anchor_replans_packaged(lam, old):
    module, _dbs, calls = lam
    new_anchor = NOW - 4 * 3600
    out = module.lambda_handler(
        _cfn("Update", {"Days": "3", "Anchor": f" {new_anchor} "}, old,
             PhysicalResourceId=module.PHYSICAL_ID), None)
    assert calls["replan"] == [{"new_anchor": new_anchor, "now": NOW, "source": "packaged",
                                "expected_anchor": None}]
    assert calls["write"] == []
    assert out == {"PhysicalResourceId": module.PHYSICAL_ID, "Data": {
        "AnchorEpoch": str(REPORT["new_anchor"]), "AnchorSource": "packaged",
        "FailedRunNumber": REPORT["failed_run_number"]}}


def test_update_replan_errors_raise(lam, monkeypatch):
    module, _dbs, _calls = lam

    def stale(*_a, **_k):
        raise sr.StaleAnchor("nope")
    monkeypatch.setattr(sr, "replan", stale)
    with pytest.raises(sr.StaleAnchor):
        module.lambda_handler(_cfn("Update", {"Anchor": str(NOW)}, {"Anchor": ""}), None)


# ---------------------------------------------------------------------------
# Delete: unchanged, never raises, never re-plans
# ---------------------------------------------------------------------------

def test_delete_with_an_anchor_never_replans_and_never_raises(lam, monkeypatch):
    module, _dbs, calls = lam

    def unreachable(env):
        raise RuntimeError("could not connect to server")
    monkeypatch.setattr(module, "_connect", unreachable)
    out = module.lambda_handler(_cfn("Delete", {"Anchor": str(NOW)}, {"Anchor": ""},
                                     PhysicalResourceId="x"), None)
    assert out == {"PhysicalResourceId": "x"} and calls["replan"] == [] and calls["write"] == []


# ---------------------------------------------------------------------------
# Direct invoke: {"action": "place"}
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expected", [None, float(NOW - 600), NOW - 600])
def test_place_alarm_at_replans_placed(lam, expected):
    module, _dbs, calls = lam
    alarm = NOW + 20 * 60 + 17
    out = module.lambda_handler({"action": "place", "alarm_at": alarm, "expected_anchor": expected}, None)
    assert calls["replan"] == [{"new_anchor": sr.alarm_to_anchor(alarm), "now": NOW, "source": "placed",
                                "expected_anchor": expected}]
    assert out == dict(REPORT, source="placed")


def test_place_phase_replans_placed(lam):
    module, _dbs, calls = lam
    out = module.lambda_handler({"action": "place", "phase": "repair", "elapsed_minutes": 12,
                                 "expected_anchor": NOW - 600}, None)
    assert calls["replan"] == [{"new_anchor": sr.phase_offset_to_anchor("repair", 12, NOW), "now": NOW,
                                "source": "placed", "expected_anchor": NOW - 600}]
    assert out["source"] == "placed"


@pytest.mark.parametrize("event", [
    {"action": "place", "expected_anchor": None},                                   # neither form
    {"action": "place", "alarm_at": NOW, "phase": "repair", "elapsed_minutes": 1,
     "expected_anchor": None},                                                     # both forms
    {"action": "place", "alarm_at": NOW, "elapsed_minutes": 1, "expected_anchor": None},
    {"action": "place", "alarm_at": NOW},                                           # no expected_anchor
    {"action": "place", "alarm_at": NOW, "expected_anchor": "123"},
    {"action": "place", "alarm_at": NOW, "expected_anchor": True},
    {"action": "place", "alarm_at": NOW, "expected_anchor": None, "dry_run": True},  # unknown key
    {"action": "place", "alarm_at": "2026-09-29T14:10:00Z", "expected_anchor": None},
    {"action": "place", "alarm_at": True, "expected_anchor": None},
    {"action": "place", "alarm_at": math.nan, "expected_anchor": None},
    {"action": "place", "alarm_at": math.inf, "expected_anchor": None},
    {"action": "place", "alarm_at": NOW - 86400 - 1, "expected_anchor": None},     # > 1 d ago
    {"action": "place", "alarm_at": NOW + 7 * 86400 + 1, "expected_anchor": None},  # > 7 d ahead
    {"action": "place", "phase": "exploding", "elapsed_minutes": 1, "expected_anchor": None},
    {"action": "place", "phase": 3, "elapsed_minutes": 1, "expected_anchor": None},
    {"action": "place", "phase": "repair", "expected_anchor": None},
    {"action": "place", "phase": "repair", "elapsed_minutes": "5", "expected_anchor": None},
    {"action": "place", "phase": "repair", "elapsed_minutes": 10_000, "expected_anchor": None},
    {"action": "place", "phase": "repair", "elapsed_minutes": -1, "expected_anchor": None},
    {"action": "republish"},                                                        # unknown action
])
def test_place_rejects_invalid_input_without_connecting(lam, event):
    module, _dbs, calls = lam
    out = module.lambda_handler(event, None)
    assert out["error"] == "invalid" and isinstance(out["message"], str) and out["message"]
    assert calls["connect"] == [] and calls["replan"] == []


def test_place_window_bounds_are_inclusive(lam):
    module, _dbs, calls = lam
    for alarm in (NOW - 86400, NOW + 7 * 86400):
        assert "error" not in module.lambda_handler(
            {"action": "place", "alarm_at": alarm, "expected_anchor": None}, None)
    assert len(calls["replan"]) == 2


@pytest.mark.parametrize("exc,expected", [
    (sr.StaleAnchor("stored anchor is 1.0, expected 2.0"),
     {"error": "stale_anchor", "message": "stored anchor is 1.0, expected 2.0"}),
    # a ValueError raised SERVER-SIDE inside the re-plan is not the caller's input
    (ValueError("invalid input syntax for type timestamp: host db-1.internal"),
     {"error": "internal", "message": "ValueError"}),
    (RuntimeError("deadlock detected on 10.0.0.5:5432"), {"error": "internal", "message": "RuntimeError"}),
])
def test_place_maps_replan_errors_and_never_raises(lam, monkeypatch, caplog, exc, expected):
    module, _dbs, _calls = lam

    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(sr, "replan", boom)
    with caplog.at_level("WARNING"):
        out = module.lambda_handler({"action": "place", "alarm_at": NOW, "expected_anchor": 1.0}, None)
    assert out == expected
    if expected["error"] == "internal":      # the detail is logged, never returned
        assert any(str(exc) in r.getMessage() for r in caplog.records)


def test_place_connection_failure_is_internal_class_only_and_never_raises(lam, monkeypatch):
    module, _dbs, _calls = lam

    def unreachable(env):
        raise RuntimeError("could not connect to server at db.example.internal (10.1.2.3)")
    monkeypatch.setattr(module, "_connect", unreachable)
    out = module.lambda_handler({"action": "place", "alarm_at": NOW, "expected_anchor": None}, None)
    assert out == {"error": "internal", "message": "RuntimeError"}


def test_place_never_raises_when_story_replan_cannot_be_imported(lam, monkeypatch):
    """The import is inside the guard: a broken package is ``internal``."""
    import sys

    import erp_simulation
    module, _dbs, calls = lam
    monkeypatch.delattr(erp_simulation, "story_replan")
    monkeypatch.setitem(sys.modules, "erp_simulation.story_replan", None)
    out = module.lambda_handler({"action": "place", "alarm_at": NOW, "expected_anchor": None}, None)
    assert out == {"error": "internal", "message": "ModuleNotFoundError"}
    assert calls["connect"] == []


def test_place_closes_both_connections(lam):
    module, dbs, _calls = lam
    closed = []
    for env, db in dbs.items():
        db.close = lambda env=env: closed.append(env)
    module.lambda_handler({"action": "place", "alarm_at": NOW, "expected_anchor": None}, None)
    assert sorted(closed) == ["DB_SECRET_NAME", "SEED_DB_SECRET_NAME"]


# ---------------------------------------------------------------------------
# Reset failsafe (ADR-0091 Design C): the CR records the default it planned
# onto; {"action": "reset"} re-plans onto it with its stored source.
# ---------------------------------------------------------------------------

def _default(dbs):
    config = dbs["DB_SECRET_NAME"].config
    return config.get("story_default_anchor"), config.get("story_default_source")


def test_create_records_the_deploy_default(lam):
    module, dbs, _calls = lam
    module.lambda_handler(_cfn("Create", {"Days": "3"}), None)
    assert _default(dbs) == (str(cm.snap_anchor(NOW, "down")), "deploy")


def test_create_records_the_packaged_default(lam):
    module, dbs, _calls = lam
    module.lambda_handler(_cfn("Create", {"Anchor": str(NOW - 5 * 3600 + 7)}), None)
    assert _default(dbs) == (str(sr.normalize_anchor(NOW - 5 * 3600 + 7, NOW)), "packaged")


def test_create_over_a_placed_clock_records_the_first_anchor_not_the_placement(lam):
    module, dbs, _calls = lam
    erp = dbs["DB_SECRET_NAME"]
    erp.config.update({"story_anchor_epoch": repr(float(NOW - 7200)), "story_anchor_source": "placed"})
    erp.keys.update(("simulation_config", key) for key in erp.config)   # stored rows (insert-only writer)
    out = module.lambda_handler(_cfn("Create", {}), None)
    assert out["Data"]["AnchorSource"] == "placed"
    assert _default(dbs) == (str(cm.snap_anchor(NOW, "down")), "deploy")


def test_an_unchanged_update_keeps_a_stored_default(lam):
    module, dbs, _calls = lam
    module.lambda_handler(_cfn("Create", {"Days": "3"}), None)
    dbs["DB_SECRET_NAME"].config["story_default_anchor"] = "123"
    module.lambda_handler(_cfn("Update", {"Days": "3"}, {"Days": "3"}, PhysicalResourceId="x"), None)
    assert _default(dbs) == ("123", "deploy")


def test_an_unchanged_update_backfills_a_history_written_without_a_default(lam):
    """An account upgraded in place: the history predates the default keys."""
    module, dbs, _calls = lam
    erp = dbs["DB_SECRET_NAME"]
    sh.write(erp, dbs["SEED_DB_SECRET_NAME"], sh.plan(float(NOW - 7200)))
    for key in sh.DEFAULT_KEYS:
        erp.config.pop(key, None)
    erp.keys.difference_update(("simulation_config", key) for key in sh.DEFAULT_KEYS)
    module.lambda_handler(_cfn("Update", {"Days": "3"}, {"Days": "3"}, PhysicalResourceId="x"), None)
    assert _default(dbs) == (str(NOW - 7200), "deploy")


def test_an_anchor_update_records_the_replanned_packaged_default(lam, monkeypatch):
    module, dbs, _calls = lam
    module.lambda_handler(_cfn("Update", {"Anchor": str(NOW - 4 * 3600)}, {"Anchor": ""}), None)
    assert _default(dbs) == (str(REPORT["new_anchor"]), "packaged")


def test_a_default_record_failure_is_logged_and_never_fails_the_stack(lam, monkeypatch, caplog):
    module, _dbs, _calls = lam

    def boom(*_a, **_k):
        raise RuntimeError("deadlock on 10.0.0.5")
    monkeypatch.setattr(sr, "write_default_anchor", boom)
    with caplog.at_level("WARNING"):
        out = module.lambda_handler(_cfn("Create", {"Days": "3"}), None)
    assert out["Data"]["AnchorSource"] == "deploy"
    assert any("default anchor record FAILED" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)


@pytest.fixture
def reset_spy(lam, monkeypatch):
    module, dbs, calls = lam
    calls["reset"] = []

    def reset(erp, seed, *, now, expected_anchor=None):
        assert (erp, seed) == (dbs["DB_SECRET_NAME"], dbs["SEED_DB_SECRET_NAME"])
        calls["reset"].append({"now": now, "expected_anchor": expected_anchor})
        return dict(REPORT, source="deploy", reset=True, default_anchor=1789990010, default_source="deploy")
    monkeypatch.setattr(sr, "reset", reset)
    return module, dbs, calls


@pytest.mark.parametrize("expected", [None, NOW - 600, float(NOW - 600)])
def test_reset_replans_onto_the_stored_default(reset_spy, expected):
    module, _dbs, calls = reset_spy
    out = module.lambda_handler({"action": "reset", "expected_anchor": expected}, None)
    assert calls["reset"] == [{"now": NOW, "expected_anchor": expected}] and calls["replan"] == []
    assert out["reset"] is True and out["source"] == "deploy"


@pytest.mark.parametrize("event", [
    {"action": "reset"},                                              # no expected_anchor
    {"action": "reset", "expected_anchor": "123"},
    {"action": "reset", "expected_anchor": True},
    {"action": "reset", "expected_anchor": math.nan},
    {"action": "reset", "expected_anchor": None, "alarm_at": NOW},    # a place key
    {"action": "reset", "expected_anchor": None, "dry_run": True},
])
def test_reset_rejects_invalid_input_without_connecting(reset_spy, event):
    module, _dbs, calls = reset_spy
    out = module.lambda_handler(event, None)
    assert out["error"] == "invalid" and out["message"]
    assert calls["connect"] == [] and calls["reset"] == []


@pytest.mark.parametrize("exc,expected", [
    (sr.NoDefaultAnchor("no default story anchor is stored"),
     {"error": "no_default_anchor", "message": "no default story anchor is stored"}),
    (sr.StaleAnchor("stored anchor is 1.0"), {"error": "stale_anchor", "message": "stored anchor is 1.0"}),
    (RuntimeError("10.0.0.5 gone"), {"error": "internal", "message": "RuntimeError"}),
])
def test_reset_maps_errors_and_never_raises(lam, monkeypatch, exc, expected):
    module, _dbs, _calls = lam

    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(sr, "reset", boom)
    assert module.lambda_handler({"action": "reset", "expected_anchor": None}, None) == expected


@pytest.mark.parametrize("action", ["republish", ["place"], None, 3])
def test_an_unsupported_action_is_invalid(lam, action):
    module, _dbs, calls = lam
    out = module.lambda_handler({"action": action}, None)
    assert out["error"] == "invalid" and calls["connect"] == []


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_every_handler_function_is_under_50_lines():
    tree = ast.parse(_LAMBDA.read_text())
    long = {node.name: node.end_lineno - node.lineno + 1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.end_lineno - node.lineno + 1 >= 50}
    assert long == {}


# ---------------------------------------------------------------------------
# place_live (ADR-0095): the live clock moves, the history stays
# ---------------------------------------------------------------------------

LIVE_REPORT = {"keep_history": True, "history_anchor": NOW - 7200, "old_live_anchor": float(NOW - 7200),
               "new_live_anchor": NOW - 3600, "one_clock": False, "failed_run_number": "RUN-20260920-90001",
               "retired": {"story_runs": 1, "condition_orders": 1}, "written": {"config": 1},
               "alarm_at": NOW + 600, "loops_shifted": 0}


@pytest.fixture
def live_lam(lam, monkeypatch):
    module, dbs, calls = lam
    calls["replan_live"] = []

    def replan_live(erp, seed, new_anchor, *, now, expected_live_anchor=None):
        assert (erp, seed) == (dbs["DB_SECRET_NAME"], dbs["SEED_DB_SECRET_NAME"])
        calls["replan_live"].append({"new_anchor": new_anchor, "now": now, "expected_live_anchor": expected_live_anchor})
        return dict(LIVE_REPORT)

    monkeypatch.setattr(sr, "replan_live", replan_live)
    return module, dbs, calls


@pytest.mark.parametrize("expected", [NOW - 7200, None])
def test_place_live_moves_only_the_live_clock(live_lam, expected):
    module, _dbs, calls = live_lam
    alarm = NOW + 600
    out = module.lambda_handler({"action": "place_live", "alarm_at": alarm, "expected_live_anchor": expected}, None)
    assert calls["replan_live"] == [{"new_anchor": sr.alarm_to_anchor(alarm), "now": NOW,
                                     "expected_live_anchor": expected}]
    assert calls["replan"] == [], "a live move never runs the full re-plan"
    assert out == LIVE_REPORT


@pytest.mark.parametrize("event", [
    {"action": "place_live", "expected_live_anchor": None},                                  # no alarm
    {"action": "place_live", "alarm_at": NOW + 600},                                         # no CAS key
    {"action": "place_live", "alarm_at": NOW + 600, "expected_anchor": None},               # the full-place CAS key
    {"action": "place_live", "phase": "repair", "elapsed_minutes": 1, "expected_live_anchor": None},
    {"action": "place_live", "alarm_at": NOW + 8 * 86400, "expected_live_anchor": None},    # outside the window
])
def test_place_live_rejects_invalid_input_without_connecting(live_lam, event):
    module, _dbs, calls = live_lam
    out = module.lambda_handler(event, None)
    assert out["error"] == "invalid" and calls["connect"] == [] and calls["replan_live"] == []


def test_place_live_a_refused_move_is_invalid_not_internal(live_lam, monkeypatch):
    module, _dbs, _calls = live_lam

    def refuse(*_a, **_k):
        raise sr.LiveClockRefused("the new alarm at 1 would land inside the written history")

    monkeypatch.setattr(sr, "replan_live", refuse)
    out = module.lambda_handler({"action": "place_live", "alarm_at": NOW + 600, "expected_live_anchor": None}, None)
    assert out == {"error": "invalid", "message": "the new alarm at 1 would land inside the written history"}


def test_place_live_stale_live_anchor_is_stale_anchor(live_lam, monkeypatch):
    module, _dbs, _calls = live_lam

    def stale(*_a, **_k):
        raise sr.StaleAnchor("stored live anchor is 5, the move expected 4")

    monkeypatch.setattr(sr, "replan_live", stale)
    out = module.lambda_handler({"action": "place_live", "alarm_at": NOW + 600, "expected_live_anchor": 4}, None)
    assert out["error"] == "stale_anchor"
