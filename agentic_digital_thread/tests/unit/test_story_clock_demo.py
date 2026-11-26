# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Placeable story clock, app side (``agents/demo/story_clock.py``, ADR-0089 D13).

Pins:

* the request model (``extra="forbid"``, exactly one of ``alarm_at`` (ISO,
  tz-aware) or ``phase`` + ``elapsed_minutes``; ``expected_anchor_epoch``
  required but nullable; ``dry_run`` default false);
* the dry-run preview mirrors ``erp_simulation.story_replan`` exactly (the
  app image ships only ``agents/``, so the arithmetic is a pinned copy) and
  NEVER invokes the Lambda;
* apply: the ``erp-story-history`` ``place`` payload, a RequestResponse
  invoke, the response returned as-is plus ``alarm_at_iso``, and the error
  mapping (stale_anchor 409, invalid 422, internal / FunctionError / transport
  502 with a digit-masked message; no env var 404);
* the ``aws_session.lambda_client`` transport (read timeout, ONE attempt);
* ``scripts/place_story_clock.py`` (login, body, exit codes 0/1/2).

Offline: the Lambda client is a fake; no network.
"""
from __future__ import annotations

import importlib.util
import io
import json
import random
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents import aws_session  # noqa: E402
from agents.demo import story_clock as sc  # noqa: E402
from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import story_replan  # noqa: E402

_NOW = 1_790_000_000
_ENV = sc.STORY_HISTORY_FUNCTION_ENV
_FN = "story-history-fn"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _req(**fields):
    """A request; a null CAS anchor carries ``force: true`` unless the test says otherwise."""
    fields.setdefault("expected_anchor_epoch", None)
    if fields["expected_anchor_epoch"] is None:
        fields.setdefault("force", True)
    return sc.PlaceClockRequest(**fields)


class FakeLambda:
    """``invoke(**kwargs)`` recorder answering one canned payload."""

    def __init__(self, payload=None, *, raw=None, function_error=None, exc=None):
        self.payload, self.raw, self.function_error, self.exc = payload, raw, function_error, exc
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        body = self.raw if self.raw is not None else json.dumps(self.payload).encode()
        resp = {"StatusCode": 200, "Payload": io.BytesIO(body)}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


# ── Request model ─────────────────────────────────────────────────────────────

def test_alarm_form_parses_an_aware_iso_string():
    req = _req(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=_NOW)
    assert req.alarm_at == datetime(2026, 10, 1, 14, 10, tzinfo=timezone.utc)
    assert req.expected_anchor_epoch == _NOW and req.dry_run is False


def test_phase_form_parses():
    req = _req(phase="bpfo_alarm", elapsed_minutes=12, dry_run=True)
    assert (req.phase, req.elapsed_minutes, req.dry_run) == ("bpfo_alarm", 12, True)


@pytest.mark.parametrize("fields", [
    {},                                                               # neither form
    {"alarm_at": "2026-10-01T14:10:00Z", "phase": "repair", "elapsed_minutes": 1},  # both
    {"phase": "repair"},                                              # half a phase form
    {"elapsed_minutes": 3},
    {"alarm_at": "2026-10-01T14:10:00Z", "elapsed_minutes": 3},
    {"alarm_at": "2026-10-01T14:10:00"},                              # naive
    {"alarm_at": _NOW},                                               # not a string
    {"alarm_at": "not-a-time"},
    {"phase": "exploded", "elapsed_minutes": 1},
    {"phase": "repair", "elapsed_minutes": -1},
    {"phase": "repair", "elapsed_minutes": float("nan")},
    {"phase": "repair", "elapsed_minutes": True},
    {"alarm_at": "2026-10-01T14:10:00Z", "extra": 1},                 # extra="forbid"
    {"alarm_at": "2026-10-01T14:10:00Z", "expected_anchor_epoch": "123"},
    {"alarm_at": "2026-10-01T14:10:00Z", "expected_anchor_epoch": True},
    {"alarm_at": "2026-10-01T14:10:00Z", "expected_anchor_epoch": 1.5},
    {"alarm_at": "1790000000"},                                       # numeric string, not ISO
    {"alarm_at": "1790000000.5Z"},
    {"alarm_at": b"2026-10-01T14:10:00Z"},                            # bytes, not str
    {"alarm_at": "2026-10-01T14:10:00Z", "expected_anchor_epoch": 4102444801},   # after 2100-01-01
    {"alarm_at": "2026-10-01T14:10:00Z", "force": "yes"},
    {"alarm_at": "2026-10-01T14:10:00Z", "dry_run": "true"},           # strict bool
    {"alarm_at": "2026-10-01T14:10:00Z", "dry_run": 1},
    {"alarm_at": "2026-10-01T14:10:00Z", "dry_run": "yes"},
], ids=lambda f: ",".join(sorted(f)) or "empty")
def test_invalid_requests_are_rejected(fields):
    with pytest.raises(ValidationError):
        _req(**fields)


def test_expected_anchor_is_required_but_nullable():
    with pytest.raises(ValidationError, match="expected_anchor_epoch"):
        sc.PlaceClockRequest(alarm_at="2026-10-01T14:10:00Z")
    assert _req(alarm_at="2026-10-01T14:10:00Z").expected_anchor_epoch is None
    assert _req(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=4102444800).expected_anchor_epoch \
        == 4102444800
    # the GET's float anchor is accepted when it is a whole number
    assert _req(alarm_at="2026-10-01T14:10:00Z",
                expected_anchor_epoch=1790000010.0).expected_anchor_epoch == 1790000010


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_null_expected_anchor_needs_force(dry_run):
    """Skipping the CAS is explicit: ``expected_anchor_epoch: null`` 422s
    unless the body also says ``force: true`` (and ``force`` never rides a
    real CAS anchor)."""
    with pytest.raises(ValidationError, match="force"):
        sc.PlaceClockRequest(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=None, dry_run=dry_run)
    with pytest.raises(ValidationError, match="force"):
        sc.PlaceClockRequest(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=None, force=False)
    with pytest.raises(ValidationError, match="force"):
        sc.PlaceClockRequest(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=_NOW, force=True)
    req = sc.PlaceClockRequest(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=None, force=True,
                               dry_run=dry_run)
    assert req.expected_anchor_epoch is None and req.force is True
    assert sc.PlaceClockRequest(alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=_NOW).force is False


# ── Mirror parity with erp_simulation (the app image has no erp_simulation) ───

def test_constants_mirror_condition_monitor_and_replan():
    assert sc.TELEMETRY_GRID_SECONDS == cm.TELEMETRY_GRID_SECONDS
    assert sc.LOOP_SECONDS == story_replan.LOOP_SECONDS == cm.TOTAL_SECONDS
    assert sc.ALARM_OFFSET_SECONDS == cm.ALARM_OFFSET_SECONDS
    assert sc.ANCHOR_RESIDUE == cm.ANCHOR_RESIDUE
    assert sc.PHASE_SECONDS == {p: (a * cm.INTERVAL_SEC, b * cm.INTERVAL_SEC)
                                for p, (a, b) in story_replan.PHASE_STEPS.items()}


def test_pure_helpers_match_story_replan_for_sampled_inputs():
    rng = random.Random(11)
    for _ in range(2000):
        now = _NOW + rng.randint(-10 ** 6, 10 ** 6)
        ref = now + rng.uniform(-3 * sc.LOOP_SECONDS, 3 * sc.LOOP_SECONDS)
        assert sc.snap_anchor_nearest(ref) == cm.snap_anchor(ref, "nearest")
        assert sc.normalize_anchor(ref, now) == story_replan.normalize_anchor(ref, now)
        assert sc.loops_shifted(ref, now) == story_replan.loops_shifted(ref, now)
        assert sc.alarm_to_anchor(ref) == story_replan.alarm_to_anchor(ref)


@pytest.mark.parametrize("phase", sorted(story_replan.PHASE_STEPS))
def test_phase_offset_matches_story_replan(phase):
    start, end = sc.PHASE_SECONDS[phase]
    for minutes in (0, 1, 7.5, (end - start) / 60 - 0.5):
        assert (sc.phase_offset_to_anchor(phase, minutes, _NOW)
                == story_replan.phase_offset_to_anchor(phase, minutes, _NOW))
    for bad in ((end - start) / 60, -0.1):
        with pytest.raises(ValueError):
            sc.phase_offset_to_anchor(phase, bad, _NOW)
        with pytest.raises(ValueError):
            story_replan.phase_offset_to_anchor(phase, bad, _NOW)


# ── Dry run: preview only, never the Lambda ───────────────────────────────────

def _no_lambda():
    raise AssertionError("a dry run must never build a Lambda client")


def test_dry_run_alarm_form_previews_without_invoking(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    alarm = _NOW + 3 * 3600 + 17        # rounds to the nearest grid point
    out = sc.place(_req(alarm_at=_iso(alarm), expected_anchor_epoch=_NOW, dry_run=True),
                   now=_NOW, client_factory=_no_lambda)
    ref = story_replan.alarm_to_anchor(alarm)
    assert out == {
        "dry_run": True, "expected_anchor": _NOW,
        "new_anchor": story_replan.normalize_anchor(ref, _NOW),
        "new_anchor_iso": _iso(story_replan.normalize_anchor(ref, _NOW)),
        "alarm_at": cm.snap_anchor(ref, "nearest") + cm.ALARM_OFFSET_SECONDS,
        "alarm_at_iso": _iso(cm.snap_anchor(ref, "nearest") + cm.ALARM_OFFSET_SECONDS),
        "loops_shifted": story_replan.loops_shifted(ref, _NOW),
        "effect": list(sc.PLACEMENT_EFFECT), "warnings": [],
        "effect_codes": ["rewrites_history", "resets_backfill"], "warning_codes": [],
    }
    assert out["alarm_at"] % sc.TELEMETRY_GRID_SECONDS == 0


def test_dry_run_far_alarm_shifts_loops(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(alarm_at=_iso(_NOW + 6 * 86400), dry_run=True), now=_NOW,
                   client_factory=_no_lambda)
    assert out["loops_shifted"] > 0 and out["new_anchor"] <= _NOW


def test_dry_run_phase_form(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(phase="repair", elapsed_minutes=5, dry_run=True), now=_NOW,
                   client_factory=_no_lambda)
    ref = story_replan.phase_offset_to_anchor("repair", 5, _NOW)
    assert out["new_anchor"] == story_replan.normalize_anchor(ref, _NOW) == ref
    assert out["alarm_at"] == cm.snap_anchor(ref, "nearest") + cm.ALARM_OFFSET_SECONDS
    assert out["loops_shifted"] == 0


@pytest.mark.parametrize("alarm", [_NOW - 86400 - 1, _NOW + 7 * 86400 + 1])
def test_dry_run_alarm_outside_window_is_422(monkeypatch, alarm):
    monkeypatch.setenv(_ENV, _FN)
    with pytest.raises(HTTPException) as err:
        sc.place(_req(alarm_at=_iso(alarm), dry_run=True), now=_NOW, client_factory=_no_lambda)
    assert err.value.status_code == 422


def test_dry_run_elapsed_outside_phase_is_422(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    with pytest.raises(HTTPException) as err:
        sc.place(_req(phase="repair", elapsed_minutes=999, dry_run=True), now=_NOW,
                 client_factory=_no_lambda)
    assert err.value.status_code == 422


@pytest.mark.parametrize("dry_run", [True, False])
def test_missing_function_env_is_404_not_a_live_workshop(monkeypatch, dry_run):
    monkeypatch.delenv(_ENV, raising=False)
    with pytest.raises(HTTPException) as err:
        sc.place(_req(alarm_at=_iso(_NOW), dry_run=dry_run), now=_NOW, client_factory=_no_lambda)
    assert err.value.status_code == 404 and "not a live workshop" in err.value.detail


# ── Apply: payload, invoke, response, error mapping ──────────────────────────

_REPORT = {"old_anchor": 1.0, "new_anchor": 2, "source": "placed", "alarm_at": _NOW + 600,
           "loops_shifted": 0, "retired": {"runs": 19}, "written": {"runs": 19}}


def test_apply_alarm_form_invokes_request_response_and_returns_report(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda(_REPORT)
    alarm = _NOW + 600
    out = sc.place(_req(alarm_at=_iso(alarm), expected_anchor_epoch=_NOW), now=_NOW,
                   client_factory=lambda: fake)
    (call,) = fake.calls
    assert call["FunctionName"] == _FN and call["InvocationType"] == "RequestResponse"
    assert json.loads(call["Payload"]) == {"action": "place", "alarm_at": alarm,
                                           "expected_anchor": _NOW}
    assert out == {**_REPORT, "alarm_at_iso": _iso(_NOW + 600)}


def test_apply_phase_form_payload_and_null_cas(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda(_REPORT)
    sc.place(_req(phase="healthy", elapsed_minutes=2.5), now=_NOW, client_factory=lambda: fake)
    assert json.loads(fake.calls[0]["Payload"]) == {
        "action": "place", "phase": "healthy", "elapsed_minutes": 2.5, "expected_anchor": None}


@pytest.mark.parametrize("error,status", [("stale_anchor", 409), ("invalid", 422)])
def test_lambda_errors_map_to_status_with_the_message(monkeypatch, error, status):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda({"error": error, "message": "stored anchor is 1790000010.0 on 10.0.0.5 " + "x" * 400})
    with pytest.raises(HTTPException) as err:
        sc.place(_req(alarm_at=_iso(_NOW)), now=_NOW, client_factory=lambda: fake)
    assert err.value.status_code == status
    message = err.value.detail["message"]
    assert err.value.detail["error"] == error and set(err.value.detail) == {"error", "message"}
    assert message.startswith("stored anchor is ##########.# on ##.#.#.# ") and len(message) == 300
    assert not any(ch.isdigit() for ch in message)


def test_a_placement_in_flight_is_409_without_invoking(monkeypatch):
    """Process-level, NON-blocking one-placement guard (the Evals one-run-lock
    shape): a second apply while one is in flight is refused at once."""
    monkeypatch.setenv(_ENV, _FN)
    assert sc._PLACE_IN_FLIGHT.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as err:
            sc.place(_req(alarm_at=_iso(_NOW), expected_anchor_epoch=_NOW), now=_NOW,
                     client_factory=_no_lambda)
        assert err.value.status_code == 409
        assert err.value.detail == {"error": "in_flight", "message": sc.IN_FLIGHT_DETAIL}
        # a dry run invokes nothing, so it is never refused
        assert sc.place(_req(alarm_at=_iso(_NOW), expected_anchor_epoch=_NOW, dry_run=True), now=_NOW,
                        client_factory=_no_lambda)["dry_run"] is True
    finally:
        sc._PLACE_IN_FLIGHT.release()


def test_the_in_flight_guard_is_held_during_the_invoke_and_released_after(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    held = []

    class Probe(FakeLambda):
        def invoke(self, **kwargs):
            held.append(sc._PLACE_IN_FLIGHT.locked())
            return super().invoke(**kwargs)

    sc.place(_req(alarm_at=_iso(_NOW), expected_anchor_epoch=_NOW), now=_NOW,
             client_factory=lambda: Probe(_REPORT))
    assert held == [True] and not sc._PLACE_IN_FLIGHT.locked()
    with pytest.raises(HTTPException):
        sc.place(_req(alarm_at=_iso(_NOW), expected_anchor_epoch=_NOW), now=_NOW,
                 client_factory=lambda: Probe({"error": "stale_anchor", "message": "m"}))
    assert not sc._PLACE_IN_FLIGHT.locked()
    with pytest.raises(RuntimeError, match="no client"):
        sc.place(_req(alarm_at=_iso(_NOW), expected_anchor_epoch=_NOW), now=_NOW,
                 client_factory=lambda: (_ for _ in ()).throw(RuntimeError("no client")))
    assert not sc._PLACE_IN_FLIGHT.locked()


@pytest.mark.parametrize("fake", [
    FakeLambda({"error": "internal", "message": "OperationalError: host 10.0.0.5"}),
    FakeLambda({"error": "surprise", "message": "port 5432"}),
    FakeLambda({"errorMessage": "boom 42"}, function_error="Unhandled"),
    FakeLambda(raw=b"not json 7"),
    FakeLambda(raw=b"[1, 2]"),
    FakeLambda(exc=RuntimeError("read timeout on 10.1.2.3")),
], ids=["internal", "unknown-error", "function-error", "not-json", "not-object", "transport"])
def test_lambda_failures_are_502_digit_masked(monkeypatch, fake):
    monkeypatch.setenv(_ENV, _FN)
    with pytest.raises(HTTPException) as err:
        sc.place(_req(alarm_at=_iso(_NOW)), now=_NOW, client_factory=lambda: fake)
    assert err.value.status_code == 502
    detail = json.dumps(err.value.detail)
    assert not any(ch.isdigit() for ch in detail), detail


def test_default_client_factory_uses_the_aws_session_lambda_helper(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda(_REPORT)
    built = []
    monkeypatch.setattr(aws_session, "lambda_client",
                        lambda **kw: built.append(kw) or fake)
    sc.place(_req(alarm_at=_iso(_NOW)), now=_NOW)
    assert built == [{"read_timeout": sc.LAMBDA_READ_TIMEOUT_S}] and len(fake.calls) == 1


def test_lambda_client_transport(monkeypatch):
    seen = {}
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    monkeypatch.setattr(aws_session.boto3, "client",
                        lambda service, **kw: seen.update(service=service, **kw) or "client")
    assert aws_session.lambda_client(read_timeout=90) == "client"
    cfg = seen["config"]
    assert seen["service"] == "lambda" and seen["region_name"] == "eu-west-3"
    assert cfg.read_timeout == 90 and cfg.retries == {"max_attempts": 1, "mode": "standard"}
    aws_session.lambda_client(region="us-west-2")
    assert seen["region_name"] == "us-west-2"
    assert seen["config"].read_timeout == aws_session.LAMBDA_READ_TIMEOUT_S


# ── Relative form: alarm_in_minutes (resolved app-side to alarm_at) ──────────

_GRID = 300
_ON_GRID = (_NOW // _GRID) * _GRID      # an exact grid point


def test_alarm_in_minutes_range_constant():
    assert sc.ALARM_IN_MINUTES_RANGE == (5, 480)


def test_default_loop_geometry_is_byte_identical():
    """``STORY_LOOP_SECONDS`` unset (this lane) -> the shipped 8 h mirror (ADR-0091 pin)."""
    assert (sc.LOOP_SECONDS, sc._NUM_STEPS, sc._FAIL_STEP, sc._REPAIR_STEPS, sc._BPFO_ALARM_STEP,
            sc.ALARM_OFFSET_SECONDS, sc.ANCHOR_RESIDUE) == (28800, 5760, 5184, 240, 3898, 19490, 10)
    assert sc.PHASE_SECONDS == {"degrading": (0, 19490), "bpfo_alarm": (19490, 25920),
                                "repair": (25920, 27120), "healthy": (27120, 28800)}


@pytest.mark.parametrize("now,minutes,expected", [
    (_ON_GRID, 30, _ON_GRID + 30 * 60),                 # an exact grid point stays put
    (_ON_GRID + 1, 30, _ON_GRID + 30 * 60 + _GRID),     # +1 s -> the next grid point
    (_ON_GRID + 299, 30, _ON_GRID + 30 * 60 + _GRID),
    (_ON_GRID, 5, _ON_GRID + 300),                      # lower bound
    (_ON_GRID + 0.5, 480, _ON_GRID + 480 * 60 + _GRID),  # upper bound, fractional now
])
def test_alarm_in_to_epoch_snaps_up_to_the_grid(now, minutes, expected):
    got = sc.alarm_in_to_epoch(minutes, now)
    assert got == expected and isinstance(got, int) and got % sc.TELEMETRY_GRID_SECONDS == 0
    assert got >= now + minutes * 60


def test_alarm_in_minutes_form_parses_at_the_bounds():
    for minutes in sc.ALARM_IN_MINUTES_RANGE:
        req = _req(alarm_in_minutes=minutes, expected_anchor_epoch=_NOW)
        assert req.alarm_in_minutes == minutes and req.alarm_at is None and req.phase is None


@pytest.mark.parametrize("fields", [
    {"alarm_in_minutes": 4},
    {"alarm_in_minutes": 481},
    {"alarm_in_minutes": "30"},
    {"alarm_in_minutes": True},
    {"alarm_in_minutes": 30.5},
    {"alarm_in_minutes": None},                                        # is neither form
    {"alarm_in_minutes": 30, "alarm_at": "2026-10-01T14:10:00Z"},      # two forms
    {"alarm_in_minutes": 30, "phase": "repair", "elapsed_minutes": 1},
    {"alarm_in_minutes": 30, "elapsed_minutes": 1},
], ids=["4", "481", "str", "bool", "float", "null", "with-alarm-at", "with-phase", "with-elapsed"])
def test_alarm_in_minutes_invalid_requests_are_rejected(fields):
    with pytest.raises(ValidationError):
        _req(**fields)


_WALK2_NOW = datetime(2026, 9, 30, 8, 10, 3, tzinfo=timezone.utc).timestamp()


def test_dry_run_alarm_in_minutes_walk2_value(monkeypatch):
    """ACCEPTANCE: 22 minutes at 08:10:03Z -> the 08:35:00Z alarm (walk 2)."""
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(alarm_in_minutes=22, expected_anchor_epoch=_NOW, dry_run=True),
                   now=_WALK2_NOW, client_factory=_no_lambda)
    assert out["alarm_at_iso"] == "2026-09-30T08:35:00+00:00"
    assert out["alarm_at"] == 1790757300 == sc.alarm_in_to_epoch(22, _WALK2_NOW)
    assert out["requested"] == {"alarm_in_minutes": 22, "alarm_at": 1790757300}
    assert out["effect"] == list(sc.PLACEMENT_EFFECT) and out["warnings"] == []
    assert out["loops_shifted"] == 0 and out["dry_run"] is True
    ref = story_replan.alarm_to_anchor(1790757300)
    assert out["new_anchor"] == story_replan.normalize_anchor(ref, _WALK2_NOW)
    assert "impact" not in out and "impact_error" not in out     # D2: Lambda-free


def test_dry_run_alarm_in_minutes_equals_the_alarm_at_preview(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    rel = sc.place(_req(alarm_in_minutes=90, dry_run=True), now=_NOW + 7, client_factory=_no_lambda)
    absolute = sc.place(_req(alarm_at=_iso(sc.alarm_in_to_epoch(90, _NOW + 7)), dry_run=True),
                        now=_NOW + 7, client_factory=_no_lambda)
    assert {k: v for k, v in rel.items() if k != "requested"} == absolute
    assert "requested" not in absolute


def test_effect_codes_pair_one_to_one_with_the_d2_text():
    """The UI maps the codes to i18n; the text list stays for the CLI."""
    assert sc.PLACEMENT_EFFECT_CODES == ("rewrites_history", "resets_backfill")
    assert len(sc.PLACEMENT_EFFECT_CODES) == len(sc.PLACEMENT_EFFECT)
    assert sc.LOOPS_SHIFTED_WARNING_CODE == "loops_shifted"


def test_dry_run_response_keys_are_pinned(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(alarm_at=_iso(_NOW + 600), expected_anchor_epoch=_NOW, dry_run=True), now=_NOW,
                   client_factory=_no_lambda)
    assert sorted(out) == ["alarm_at", "alarm_at_iso", "dry_run", "effect", "effect_codes",
                           "expected_anchor", "loops_shifted", "new_anchor", "new_anchor_iso",
                           "warning_codes", "warnings"]


def test_effect_is_the_fixed_d2_text():
    assert sc.PLACEMENT_EFFECT == (
        "rewrites the three-day history and every run number",
        "moves the sensor feed onto the new clock by the next alarm (recorded sensor history stays)",
    )


def test_dry_run_far_relative_alarm_warns_of_a_shifted_loop(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(alarm_in_minutes=400, dry_run=True), now=_NOW, client_factory=_no_lambda)
    assert out["loops_shifted"] >= 1
    assert out["warnings"] == [sc.LOOPS_SHIFTED_WARNING]
    assert out["warning_codes"] == ["loops_shifted"]
    assert out["effect_codes"] == list(sc.PLACEMENT_EFFECT_CODES)
    assert "condition order now" in sc.LOOPS_SHIFTED_WARNING


def test_apply_alarm_in_minutes_sends_the_alarm_at_payload_unchanged(monkeypatch):
    """The Lambda contract is unchanged: the SAME bytes as today's alarm_at path."""
    monkeypatch.setenv(_ENV, _FN)
    rel, absolute = FakeLambda(_REPORT), FakeLambda(_REPORT)
    out = sc.place(_req(alarm_in_minutes=22, expected_anchor_epoch=_NOW), now=_WALK2_NOW,
                   client_factory=lambda: rel)
    sc.place(_req(alarm_at="2026-09-30T08:35:00Z", expected_anchor_epoch=_NOW), now=_WALK2_NOW,
             client_factory=lambda: absolute)
    (call,) = rel.calls
    assert call["Payload"] == absolute.calls[0]["Payload"]
    assert json.loads(call["Payload"]) == {"action": "place", "alarm_at": 1790757300,
                                           "expected_anchor": _NOW}
    assert b"alarm_in_minutes" not in call["Payload"]
    assert out == {**_REPORT, "alarm_at_iso": _iso(_NOW + 600),
                   "requested": {"alarm_in_minutes": 22, "alarm_at": 1790757300}}


def test_relative_dry_run_takes_no_lock_but_apply_is_409_in_flight(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    assert sc._PLACE_IN_FLIGHT.acquire(blocking=False)
    try:
        assert sc.place(_req(alarm_in_minutes=30, expected_anchor_epoch=_NOW, dry_run=True),
                        now=_NOW, client_factory=_no_lambda)["dry_run"] is True
        with pytest.raises(HTTPException) as err:
            sc.place(_req(alarm_in_minutes=30, expected_anchor_epoch=_NOW), now=_NOW,
                     client_factory=_no_lambda)
        assert err.value.status_code == 409 and err.value.detail["error"] == "in_flight"
    finally:
        sc._PLACE_IN_FLIGHT.release()


def test_live_story_is_a_workshop_story():
    from agents.demo import workshop
    assert sc.LIVE_STORY in workshop.STORIES and sc.LIVE_STORY != workshop.DEFAULT_STORY


def test_engine_side_files_carry_no_demo_literal():
    """ADR-0044: the story identities stay in agents/demo/; the route module,
    the Lambda helper and the CLI name none of them."""
    import re

    from tests.unit.test_engine_domain_neutrality import DENYLIST
    from agents import workshop_story_routes
    for path in (Path(workshop_story_routes.__file__), Path(aws_session.__file__),
                 _REPO_ROOT / "scripts" / "place_story_clock.py"):
        text = path.read_text(encoding="utf-8")
        hits = {name for name, rx in DENYLIST.items() if re.search(rx, text)}
        assert hits == set(), (path.name, hits)


# ── scripts/place_story_clock.py ──────────────────────────────────────────────

_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("place_story_clock_cli",
                                               _SCRIPTS / "place_story_clock.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_PW = "pw-fixture"  # nosec B105 - offline test fixture


class _FakeApp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, base_url):
        self.base_url = base_url
        return self

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture(autouse=True)
def _cli_password_env(monkeypatch):
    monkeypatch.setenv("ADT_PASSWORD", _PW)


def test_cli_alarm_dry_run_posts_the_body_and_prints(capsys):
    app = _FakeApp([{"status": "ok"}, {"dry_run": True, "alarm_at": 1}])
    code = cli.main(["--base-url", "https://x.example",
                     "--alarm-at", "2026-10-01T14:10:00Z", "--expect-anchor", "1790000010",
                     "--dry-run"], app_factory=app)
    assert code == 0
    assert app.calls == [
        ("POST", "/api/auth/login", {"email": "ADT", "password": _PW}),
        ("POST", cli.CLOCK_ROUTE, {"alarm_at": "2026-10-01T14:10:00Z",
                                   "expected_anchor_epoch": 1790000010, "dry_run": True}),
    ]
    assert json.loads(capsys.readouterr().out) == {"dry_run": True, "alarm_at": 1}


def test_cli_phase_form_with_no_cas():
    app = _FakeApp([{"status": "ok"}, {"new_anchor": 2}])
    code = cli.main(["--base-url", "https://x.example",
                     "--phase", "repair", "--elapsed-minutes", "4", "--expect-anchor", "none", "--force"],
                    app_factory=app)
    assert code == 0
    assert app.calls[1][2] == {"phase": "repair", "elapsed_minutes": 4.0,
                               "expected_anchor_epoch": None, "force": True, "dry_run": False}


def test_cli_http_error_is_exit_1_and_prints_the_body(capsys):
    app = _FakeApp([{"status": "ok"}, _http_error(409, {"detail": {"error": "stale_anchor"}})])
    code = cli.main(["--base-url", "https://x.example",
                     "--alarm-at", "2026-10-01T14:10:00Z", "--expect-anchor", "5"],
                    app_factory=app)
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "HTTP 409" and "stale_anchor" in out["body"]


@pytest.mark.parametrize("argv", [
    ["--alarm-at", "2026-10-01T14:10:00Z", "--expect-anchor", "5"],              # no base url
    ["--base-url", "u", "--alarm-at", "a", "--phase", "repair", "--elapsed-minutes", "1",
     "--expect-anchor", "5"],                                                    # both forms
    ["--base-url", "u", "--phase", "repair", "--expect-anchor", "5"],            # half phase
    ["--base-url", "u", "--alarm-at", "a"],                                      # no --expect-anchor
    ["--base-url", "u", "--alarm-at", "a", "--expect-anchor", "soon"],           # bad epoch
    ["--base-url", "u", "--alarm-at", "a", "--expect-anchor", "none"],           # no CAS without --force
    ["--base-url", "u", "--alarm-at", "a", "--expect-anchor", "5", "--force"],   # --force with a CAS
    ["--base-url", "u", "--alarm-in-minutes", "90", "--alarm-at", "a", "--expect-anchor", "5"],
    ["--base-url", "u", "--alarm-in-minutes", "90", "--phase", "repair", "--elapsed-minutes", "1",
     "--expect-anchor", "5"],
    ["--base-url", "u", "--alarm-in-minutes", "90", "--elapsed-minutes", "1", "--expect-anchor", "5"],
])
def test_cli_usage_errors_are_exit_2(monkeypatch, argv):
    monkeypatch.delenv("ADT_BASE_URL", raising=False)
    app = _FakeApp([])
    assert cli.main(argv, app_factory=app) == 2
    assert app.calls == []


def test_cli_login_failure_is_exit_2():
    app = _FakeApp([{"status": "error"}])
    code = cli.main(["--base-url", "u", "--alarm-at", "a",
                     "--expect-anchor", "none", "--force"], app_factory=app)
    assert code == 2 and len(app.calls) == 1


def test_cli_alarm_in_minutes_builds_the_relative_body():
    app = _FakeApp([{"status": "ok"}, {"dry_run": True}])
    code = cli.main(["--base-url", "https://x.example",
                     "--alarm-in-minutes", "90", "--expect-anchor", "1790000010", "--dry-run"],
                    app_factory=app)
    assert code == 0
    assert app.calls[1] == ("POST", cli.CLOCK_ROUTE, {"alarm_in_minutes": 90,
                                                      "expected_anchor_epoch": 1790000010,
                                                      "dry_run": True})
    assert isinstance(app.calls[1][2]["alarm_in_minutes"], int)


@pytest.mark.parametrize("value", ["90.5", "ninety"])
def test_cli_alarm_in_minutes_must_be_an_integer(value, capsys):
    app = _FakeApp([])
    with pytest.raises(SystemExit) as exc:
        cli.main(["--base-url", "u", "--alarm-in-minutes", value,
                  "--expect-anchor", "5"], app_factory=app)
    assert exc.value.code == 2 and app.calls == []


# ── Design C (ADR-0091): the reset failsafe form ──────────────────────────────

_DEFAULT = (1_789_990_010, "packaged")


def test_reset_form_parses_alone_with_the_cas():
    req = _req(reset=True, expected_anchor_epoch=_NOW, dry_run=True)
    assert (req.reset, req.expected_anchor_epoch, req.dry_run) == (True, _NOW, True)
    assert _req(reset=True).force is True and sc.PlaceClockRequest(
        alarm_at="2026-10-01T14:10:00Z", expected_anchor_epoch=_NOW).reset is False


@pytest.mark.parametrize("fields", [
    {"reset": True, "alarm_at": "2026-10-01T14:10:00Z"},
    {"reset": True, "alarm_in_minutes": 30},
    {"reset": True, "phase": "repair", "elapsed_minutes": 1},
    {"reset": True, "elapsed_minutes": 1},
    {"reset": "true"}, {"reset": 1}, {"reset": "yes"},                   # strict bool
    {"reset": False},                                                   # no form at all
    {"reset": True, "expected_anchor_epoch": None, "force": False},     # null CAS without force
], ids=lambda f: ",".join(f"{k}={v}" for k, v in sorted(f.items())))
def test_reset_invalid_requests_are_rejected(fields):
    with pytest.raises(ValidationError):
        if "force" in fields:
            sc.PlaceClockRequest(**fields)
        else:
            _req(**fields)


def _reader(value):
    return lambda: value


def test_reset_dry_run_reports_the_default_without_invoking(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(reset=True, expected_anchor_epoch=_NOW, dry_run=True), now=_NOW,
                   client_factory=_no_lambda, default_reader=_reader(_DEFAULT))
    new_anchor = story_replan.normalize_anchor(_DEFAULT[0], _NOW)
    assert out == {
        "dry_run": True, "reset": True, "expected_anchor": _NOW,
        "default_anchor_epoch": _DEFAULT[0], "default_anchor_iso": _iso(_DEFAULT[0]),
        "default_source": "packaged",
        "new_anchor": new_anchor, "new_anchor_iso": _iso(new_anchor),
        "alarm_at": new_anchor + cm.ALARM_OFFSET_SECONDS,
        "alarm_at_iso": _iso(new_anchor + cm.ALARM_OFFSET_SECONDS), "loops_shifted": 0,
        "effect": [sc.RESET_EFFECT, *sc.PLACEMENT_EFFECT],
        "effect_codes": ["restores_default", "rewrites_history", "resets_backfill"],
        "warnings": [], "warning_codes": [],
    }


def test_reset_dry_run_matches_what_the_lambda_reset_replans_onto():
    """The app mirror normalises the default exactly like story_replan.reset."""
    for now in (_NOW, _NOW + 12_345, _NOW + 5 * 86_400 + 7):
        assert sc.normalize_anchor(_DEFAULT[0], now) == story_replan.normalize_anchor(_DEFAULT[0], now)


def test_reset_dry_run_without_a_default_is_409(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    with pytest.raises(HTTPException) as err:
        sc.place(_req(reset=True, dry_run=True), now=_NOW, client_factory=_no_lambda,
                 default_reader=_reader(None))
    assert err.value.status_code == 409 and err.value.detail["error"] == "no_default_anchor"


def test_reset_dry_run_read_failure_is_502_masked(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)

    def boom():
        raise RuntimeError("could not connect to 10.0.0.5")
    with pytest.raises(HTTPException) as err:
        sc.place(_req(reset=True, dry_run=True), now=_NOW, client_factory=_no_lambda, default_reader=boom)
    assert err.value.status_code == 502 and "10.0.0.5" not in str(err.value.detail)
    assert "RuntimeError" in err.value.detail["message"]


def test_reset_dry_run_takes_no_lock(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    assert sc._PLACE_IN_FLIGHT.acquire(blocking=False)
    try:
        assert sc.place(_req(reset=True, dry_run=True), now=_NOW, client_factory=_no_lambda,
                        default_reader=_reader(_DEFAULT))["reset"] is True
    finally:
        sc._PLACE_IN_FLIGHT.release()


_RESET_REPORT = {**_REPORT, "source": "packaged", "reset": True, "default_anchor": _DEFAULT[0],
                 "default_source": "packaged"}


@pytest.mark.parametrize("expected", [_NOW, None])
def test_apply_reset_invokes_the_reset_action(monkeypatch, expected):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda(_RESET_REPORT)

    def unread():
        raise AssertionError("the apply leaves the default to the Lambda")
    out = sc.place(_req(reset=True, expected_anchor_epoch=expected), now=_NOW,
                   client_factory=lambda: fake, default_reader=unread)
    (call,) = fake.calls
    assert json.loads(call["Payload"]) == {"action": "reset", "expected_anchor": expected}
    assert out == {**_RESET_REPORT, "alarm_at_iso": _iso(_REPORT["alarm_at"])}


def test_apply_reset_without_a_default_is_409(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda({"error": "no_default_anchor", "message": "no default story anchor is stored"})
    with pytest.raises(HTTPException) as err:
        sc.place(_req(reset=True, expected_anchor_epoch=_NOW), now=_NOW, client_factory=lambda: fake)
    assert err.value.status_code == 409 and err.value.detail == {
        "error": "no_default_anchor", "message": "no default story anchor is stored"}


def test_existing_forms_send_byte_identical_payloads():
    """Design C adds a field; the place payload of every existing form is unchanged."""
    at = _req(alarm_at="2026-09-30T08:35:00Z", expected_anchor_epoch=_NOW)
    ph = _req(phase="repair", elapsed_minutes=5)
    assert json.dumps(sc.payload(at)).encode() == (
        b'{"action": "place", "alarm_at": 1790757300.0, "expected_anchor": 1790000000}')
    assert json.dumps(sc.payload(ph)).encode() == (
        b'{"action": "place", "phase": "repair", "elapsed_minutes": 5.0, "expected_anchor": null}')


def test_the_default_reader_is_the_lookups_stored_default(monkeypatch):
    from agents.demo import story_lookup
    monkeypatch.setenv(_ENV, _FN)
    monkeypatch.setattr(story_lookup, "stored_default", lambda: _DEFAULT)
    out = sc.place(_req(reset=True, dry_run=True), now=_NOW, client_factory=_no_lambda)
    assert out["default_anchor_epoch"] == _DEFAULT[0]


def test_cli_reset_builds_the_reset_body():
    app = _FakeApp([{"status": "ok"}, {"dry_run": True, "reset": True}])
    code = cli.main(["--base-url", "https://x.example", "--reset", "--expect-anchor", "1790000010",
                     "--dry-run"], app_factory=app)
    assert code == 0
    assert app.calls[1] == ("POST", cli.CLOCK_ROUTE, {"reset": True, "expected_anchor_epoch": 1790000010,
                                                      "dry_run": True})


@pytest.mark.parametrize("extra", [["--alarm-at", "a"], ["--alarm-in-minutes", "30"],
                                   ["--phase", "repair", "--elapsed-minutes", "1"]])
def test_cli_reset_with_a_placement_form_is_exit_2(extra):
    app = _FakeApp([])
    assert cli.main(["--base-url", "u", "--reset", "--expect-anchor", "5", *extra], app_factory=app) == 2
    assert app.calls == []


# ---------------------------------------------------------------------------
# keep_history (ADR-0095): move only the live clock
# ---------------------------------------------------------------------------

def test_keep_history_dry_run_states_the_live_only_effect(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(alarm_in_minutes=10, expected_anchor_epoch=_NOW, dry_run=True, keep_history=True),
                   now=_NOW, client_factory=_no_lambda)
    assert out["keep_history"] is True
    assert out["effect"] == list(sc.KEEP_HISTORY_EFFECT) and out["effect_codes"] == ["keeps_history", "moves_live"]
    assert "rewrites" not in " ".join(out["effect"])
    assert out["alarm_at"] == sc.alarm_in_to_epoch(10, _NOW) and out["requested"]["alarm_in_minutes"] == 10


def test_full_place_dry_run_payload_is_unchanged_by_the_keep_history_flag(monkeypatch):
    """The existing form's payload stays byte-identical: no keep_history key at all."""
    monkeypatch.setenv(_ENV, _FN)
    out = sc.place(_req(alarm_at=_iso(_NOW + 600), expected_anchor_epoch=_NOW, dry_run=True), now=_NOW,
                   client_factory=_no_lambda)
    assert "keep_history" not in out and out["effect_codes"] == list(sc.PLACEMENT_EFFECT_CODES)


def _live_anchor_in(phase: str, now: float, minutes_in: float = 5) -> int:
    """A live anchor putting *now* ``minutes_in`` into *phase* (the anchor the lookup would report)."""
    return sc.phase_offset_to_anchor(phase, minutes_in, now)


@pytest.mark.parametrize("phase", ["bpfo_alarm", "repair", "healthy"])
def test_keep_history_move_past_the_alarm_warns_that_the_live_side_rewinds(monkeypatch, phase):
    """Event 28 (2026-10-06): a keep-history move made while the plant was already in
    the alarm phase rewound the live sensor to degrading, so the hour's vibration trend
    read as a drop at the move and the 'trending up' answer honestly said no. The
    preview now says so before the attendee confirms."""
    monkeypatch.setenv(_ENV, _FN)
    live = _live_anchor_in(phase, _NOW)
    out = sc.place(_req(alarm_in_minutes=10, expected_anchor_epoch=live, dry_run=True, keep_history=True),
                   now=_NOW, client_factory=_no_lambda)
    assert out["warning_codes"] == [sc.REWINDS_LIVE_WARNING_CODE]
    assert out["warnings"] == [sc.REWINDS_LIVE_WARNING]
    assert "degrading" in sc.REWINDS_LIVE_WARNING and "trend" in sc.REWINDS_LIVE_WARNING


def test_keep_history_move_during_degrading_carries_no_rewind_warning(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    live = _live_anchor_in("degrading", _NOW, 30)
    out = sc.place(_req(alarm_in_minutes=10, expected_anchor_epoch=live, dry_run=True, keep_history=True),
                   now=_NOW, client_factory=_no_lambda)
    assert out["warnings"] == [] and out["warning_codes"] == []


def test_keep_history_move_to_a_past_alarm_does_not_rewind(monkeypatch):
    """An alarm placed BEHIND now puts the live side at or past the alarm: nothing rewinds."""
    monkeypatch.setenv(_ENV, _FN)
    live = _live_anchor_in("repair", _NOW)
    out = sc.place(_req(alarm_at=_iso(_NOW - 1800), expected_anchor_epoch=live, dry_run=True,
                        keep_history=True), now=_NOW, client_factory=_no_lambda)
    assert sc.REWINDS_LIVE_WARNING_CODE not in out["warning_codes"]


def test_full_rewrite_past_the_alarm_keeps_its_own_warning_set(monkeypatch):
    """The rewind note belongs to the keep-history form, whose CAS anchor IS the live
    anchor; the full rewrite's dry run is unchanged (its effect already says it moves
    everything)."""
    monkeypatch.setenv(_ENV, _FN)
    live = _live_anchor_in("bpfo_alarm", _NOW)
    out = sc.place(_req(alarm_in_minutes=10, expected_anchor_epoch=live, dry_run=True), now=_NOW,
                   client_factory=_no_lambda)
    assert sc.REWINDS_LIVE_WARNING_CODE not in out["warning_codes"]


def test_keep_history_apply_invokes_place_live_with_the_live_cas(monkeypatch):
    monkeypatch.setenv(_ENV, _FN)
    fake = FakeLambda({"keep_history": True, "new_live_anchor": 1, "alarm_at": _NOW + 600})
    alarm = _NOW + 600
    out = sc.place(_req(alarm_at=_iso(alarm), expected_anchor_epoch=_NOW - 7200, keep_history=True), now=_NOW,
                   client_factory=lambda: fake)
    assert json.loads(fake.calls[0]["Payload"]) == {"action": "place_live", "alarm_at": alarm,
                                                    "expected_live_anchor": _NOW - 7200}
    assert out["alarm_at_iso"] == _iso(alarm) and out["keep_history"] is True


@pytest.mark.parametrize("fields", [
    {"reset": True, "keep_history": True, "expected_anchor_epoch": 1},
    {"phase": "repair", "elapsed_minutes": 1, "keep_history": True, "expected_anchor_epoch": 1},
    {"alarm_in_minutes": 10, "keep_history": "yes", "expected_anchor_epoch": 1},
])
def test_keep_history_goes_with_an_alarm_form_only(fields):
    with pytest.raises(ValidationError):
        sc.PlaceClockRequest(**fields)


def test_keep_history_effect_codes_pair_with_the_text():
    assert len(sc.KEEP_HISTORY_EFFECT_CODES) == len(sc.KEEP_HISTORY_EFFECT) == 2
    assert sc.KEEP_HISTORY_ACTION == "place_live"


def test_cli_keep_history_posts_the_flag_with_an_alarm_form():
    app = _FakeApp([{"status": "ok"}, {"keep_history": True}])
    code = cli.main(["--base-url", "https://x.example", "--alarm-in-minutes", "10", "--keep-history",
                     "--expect-anchor", "1790000010"], app_factory=app)
    assert code == 0
    assert app.calls[1] == ("POST", cli.CLOCK_ROUTE, {"alarm_in_minutes": 10, "keep_history": True,
                                                       "expected_anchor_epoch": 1790000010, "dry_run": False})


def test_cli_keep_history_refuses_the_reset_and_phase_forms(capsys):
    for argv in (["--reset"], ["--phase", "repair", "--elapsed-minutes", "1"]):
        code = cli.main(["--base-url", "https://x.example", *argv, "--keep-history",
                         "--expect-anchor", "1"], app_factory=_FakeApp([]))
        assert code == 2 and "--keep-history goes with" in capsys.readouterr().err
