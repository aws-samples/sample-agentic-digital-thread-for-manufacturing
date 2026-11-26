# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``GET /api/admin/workshop/story`` and ``POST /api/admin/workshop/story/clock``
(``agents/workshop_story_routes.py``, ADR-0085 / ADR-0089).

GET: steward-gated (explicit non-admin AND an absent verdict ⇒ 403, the
lookup never runs), 404 when workshop mode is off, 200 with the lookup's
payload for the DEPLOYED story, GET-only. POST clock: steward-gated, 404
outside workshop mode, 409 unless the story is live, 422 on a malformed body
(both forms, neither, an extra field), the placement's own statuses (409 CAS,
404 no function) passed through. Both are EVENT-LOOP OFFLOAD routes (the async
handler's only statement awaits ``offload``). The real ``server.app`` mounts
them. Offline: the lookup and the placement are stubbed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Match, Mount

from agents import workshop_story_routes as routes
from agents.demo import story_clock, story_lookup, workshop

_URL = "/api/admin/workshop/story"
_CLOCK_URL = "/api/admin/workshop/story/clock"
_SRC = Path(routes.__file__)


def _client(is_admin: "bool | None" = None) -> TestClient:
    """Bare app with the real router; ``is_admin=None`` mounts NO stamping
    middleware, so the verdict attribute is ABSENT."""
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.include_router(routes.router)
    return TestClient(app)


@pytest.fixture
def lookup_spy(monkeypatch):
    calls = []

    def _lookup(story_name):
        calls.append(story_name)
        return {"story": story_name, "generated_at": "t", "clock": None, "failed_batch": None,
                "part": None, "maintenance": None, "customers": [], "total_value": None,
                "warnings": [], "live_defect_run": None, "suspect_lot": None}

    monkeypatch.setattr(story_lookup, "lookup", _lookup)
    return calls


@pytest.mark.parametrize("is_admin", [False, None], ids=["non-admin", "no-verdict"])
def test_non_steward_is_403_and_lookup_never_runs(monkeypatch, lookup_spy, is_admin):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    resp = _client(is_admin=is_admin).get(_URL)
    assert resp.status_code == 403
    assert lookup_spy == []


def test_workshop_disabled_is_404(monkeypatch, lookup_spy):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    resp = _client(is_admin=True).get(_URL)
    assert resp.status_code == 404
    assert lookup_spy == []


@pytest.mark.parametrize("story", workshop.STORIES)
def test_steward_gets_the_deployed_story(monkeypatch, lookup_spy, story):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", story)
    resp = _client(is_admin=True).get(_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["story"] == story and lookup_spy == [story]
    assert set(body) == {"story", "generated_at", "clock", "failed_batch", "part",
                         "maintenance", "customers", "total_value", "warnings",
                         "live_defect_run", "suspect_lot"}


def test_default_story_is_frozen(monkeypatch, lookup_spy):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)
    assert _client(is_admin=True).get(_URL).json()["story"] == workshop.DEFAULT_STORY


def test_route_is_get_only(monkeypatch, lookup_spy):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    assert _client(is_admin=True).post(_URL).status_code == 405
    assert lookup_spy == []


def _routing_matches(app: FastAPI, method: str, url: str = _URL) -> list:
    """Top-level routes of ``app`` whose routing FULLY matches ``method`` + ``_URL``.

    Version-independent on purpose: FastAPI >= 0.14x (the deployment lock) keeps
    ``include_router`` LAZY — ``app.routes`` holds one opaque wrapper per include,
    not flattened ``APIRoute`` copies — so a flat ``isinstance(r, APIRoute)`` scan
    sees NO router-included route there, while older FastAPI flattens. Starlette's
    public ``BaseRoute.matches`` answers for both shapes. ``Mount``s are skipped:
    the SPA's ``/`` StaticFiles mount (present whenever ``frontend/dist`` is built)
    FULL-matches every path.
    """
    scope = {"type": "http", "path": url, "method": method, "root_path": "",
             "headers": [], "query_string": b""}
    return [r for r in app.routes
            if not isinstance(r, Mount) and r.matches(scope)[0] == Match.FULL]


def test_listed_in_openapi_and_mounted_on_the_real_app():
    assert "get" in _client(is_admin=True).app.openapi()["paths"][_URL]
    import agents.server as server
    # Mounted exactly ONCE on the real app, and GET-only there.
    assert len(_routing_matches(server.app, "GET")) == 1
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert _routing_matches(server.app, method) == [], method
    # The real app's schema lists exactly GET, served by THIS module's handler.
    operations = server.app.openapi()["paths"][_URL]
    assert set(operations) == {"get"}
    assert operations["get"]["operationId"].startswith(routes.workshop_story_route.__name__)


def _async_handlers() -> dict:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}


@pytest.mark.parametrize("handler,sync_body,args", [
    ("workshop_story_route", "workshop_story", ["request"]),
    ("workshop_story_clock_route", "workshop_story_clock", ["request", "body"]),
])
def test_handler_only_awaits_offload_of_the_sync_body(handler, sync_body, args):
    """EVENT-LOOP OFFLOAD PATTERN: one statement, ``return await offload(<sync body>, ...)``."""
    handlers = _async_handlers()
    assert set(handlers) == {"workshop_story_route", "workshop_story_clock_route"}
    (stmt,) = handlers[handler].body
    assert isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Await)
    call = stmt.value.value
    assert isinstance(call, ast.Call) and call.func.id == "offload"
    assert [a.id for a in call.args] == [sync_body, *args] and not call.keywords
    calls = [n for n in ast.walk(stmt) if isinstance(n, ast.Call)]
    assert len(calls) == 1


def test_route_is_steward_gated_at_registration():
    from agents.steward_gate import check_steward
    routes_ = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    assert {(r.path, tuple(sorted(r.methods))) for r in routes_} == {
        (_URL, ("GET",)), (_CLOCK_URL, ("POST",))}
    for route in routes_:
        assert any(d.call is check_steward for d in route.dependant.dependencies), route.path


# ── POST /story/clock ─────────────────────────────────────────────────────────

_BODY = {"alarm_at": "2026-10-01T14:10:00Z", "expected_anchor_epoch": 1790000010}


@pytest.fixture
def place_spy(monkeypatch):
    calls = []

    def _place(req):
        calls.append(req)
        return {"new_anchor": 1, "alarm_at_iso": "t", "dry_run": req.dry_run}

    monkeypatch.setattr(story_clock, "place", _place)
    return calls


def _live(monkeypatch):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "live")


@pytest.mark.parametrize("is_admin", [False, None], ids=["non-admin", "no-verdict"])
def test_clock_non_steward_is_403_and_nothing_runs(monkeypatch, place_spy, is_admin):
    _live(monkeypatch)
    assert _client(is_admin=is_admin).post(_CLOCK_URL, json=_BODY).status_code == 403
    assert place_spy == []


def test_clock_outside_workshop_is_404(monkeypatch, place_spy):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    assert _client(is_admin=True).post(_CLOCK_URL, json=_BODY).status_code == 404
    assert place_spy == []


def test_clock_frozen_story_is_409(monkeypatch, place_spy):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "frozen")
    resp = _client(is_admin=True).post(_CLOCK_URL, json=_BODY)
    assert resp.status_code == 409 and place_spy == []


@pytest.mark.parametrize("body", [
    {"expected_anchor_epoch": 1},                                          # neither form
    {**_BODY, "phase": "repair", "elapsed_minutes": 1},                     # both forms
    {**_BODY, "surprise": True},                                            # extra field
    {"alarm_at": "2026-10-01T14:10:00Z"},                                   # no CAS field
    {**_BODY, "alarm_at": "2026-10-01T14:10:00"},                           # naive
    {**_BODY, "expected_anchor_epoch": None},                               # null CAS, no force
    {**_BODY, "expected_anchor_epoch": None, "force": False},
    {**_BODY, "alarm_at": "1790000000"},                                    # numeric string
    {**_BODY, "alarm_in_minutes": 30},                                      # alarm_at AND relative
    {"alarm_in_minutes": 30, "phase": "repair", "elapsed_minutes": 1,
     "expected_anchor_epoch": 1},                                           # relative AND phase
    {"alarm_in_minutes": "30", "expected_anchor_epoch": 1},                 # string minutes
    {"alarm_in_minutes": 4, "expected_anchor_epoch": 1},                    # below the range
    {"alarm_in_minutes": 481, "expected_anchor_epoch": 1},                  # above the range
    {"alarm_in_minutes": 30.5, "expected_anchor_epoch": 1},                 # not an integer
    {**_BODY, "reset": True},                                               # reset AND a form
    {"reset": "true", "expected_anchor_epoch": 1},                          # strict bool
    {"reset": False, "expected_anchor_epoch": 1},                           # no form
], ids=["neither", "both", "extra", "no-cas", "naive", "null-cas-unforced", "null-cas-force-false",
        "numeric-alarm", "alarm-at-and-relative", "relative-and-phase", "relative-str",
        "relative-4", "relative-481", "relative-float", "reset-and-form", "reset-str", "reset-false"])
def test_clock_malformed_body_is_422(monkeypatch, place_spy, body):
    _live(monkeypatch)
    assert _client(is_admin=True).post(_CLOCK_URL, json=body).status_code == 422
    assert place_spy == []


@pytest.mark.parametrize("body", [_BODY, {"phase": "repair", "elapsed_minutes": 3,
                                          "expected_anchor_epoch": None, "force": True, "dry_run": True},
                                  {"alarm_in_minutes": 30, "expected_anchor_epoch": 1790000010,
                                   "dry_run": True},
                                  {"reset": True, "expected_anchor_epoch": 1790000010, "dry_run": True}],
                         ids=["alarm-at", "phase", "alarm-in-minutes", "reset"])
def test_clock_steward_places_through_story_clock(monkeypatch, place_spy, body):
    _live(monkeypatch)
    resp = _client(is_admin=True).post(_CLOCK_URL, json=body)
    assert resp.status_code == 200
    (req,) = place_spy
    assert isinstance(req, story_clock.PlaceClockRequest)
    assert req.model_dump(exclude_defaults=True) == story_clock.PlaceClockRequest(
        **body).model_dump(exclude_defaults=True)
    assert resp.json()["dry_run"] is bool(body.get("dry_run"))


@pytest.mark.parametrize("status", [404, 409, 422, 502])
def test_clock_placement_status_passes_through(monkeypatch, status):
    _live(monkeypatch)

    def _place(req):
        raise HTTPException(status_code=status, detail={"error": "x", "message": "m"})

    monkeypatch.setattr(story_clock, "place", _place)
    resp = _client(is_admin=True).post(_CLOCK_URL, json=_BODY)
    assert resp.status_code == status and resp.json()["detail"]["error"] == "x"


def test_clock_cas_mismatch_from_the_lambda_is_409(monkeypatch):
    """End to end through the real placement, only the Lambda stubbed."""
    _live(monkeypatch)
    monkeypatch.setenv(story_clock.STORY_HISTORY_FUNCTION_ENV, "fn")

    class _Lambda:
        def invoke(self, **_kw):
            import io
            import json
            return {"Payload": io.BytesIO(json.dumps(
                {"error": "stale_anchor", "message": "stored anchor is 1.0"}).encode())}

    monkeypatch.setattr(story_clock, "_default_client", _Lambda)
    resp = _client(is_admin=True).post(_CLOCK_URL, json=_BODY)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "stale_anchor", "message": "stored anchor is #.#"}  # digit-masked


def test_clock_relative_dry_run_end_to_end_is_lambda_free(monkeypatch):
    """Through the REAL placement: the relative dry run answers the resolved
    alarm, the anchor, loops_shifted and the fixed effect text; no Lambda."""
    _live(monkeypatch)
    monkeypatch.setenv(story_clock.STORY_HISTORY_FUNCTION_ENV, "fn")

    def _no_client():
        raise AssertionError("a dry run must not build a Lambda client")

    monkeypatch.setattr(story_clock, "_default_client", _no_client)
    resp = _client(is_admin=True).post(_CLOCK_URL, json={
        "alarm_in_minutes": 30, "expected_anchor_epoch": 1790000010, "dry_run": True})
    assert resp.status_code == 200
    out = resp.json()
    assert set(out) == {"dry_run", "expected_anchor", "new_anchor", "new_anchor_iso", "alarm_at",
                        "alarm_at_iso", "loops_shifted", "effect", "effect_codes", "warnings",
                        "warning_codes", "requested"}
    assert out["requested"] == {"alarm_in_minutes": 30, "alarm_at": out["alarm_at"]}
    assert out["alarm_at"] % story_clock.TELEMETRY_GRID_SECONDS == 0
    assert out["effect"] == list(story_clock.PLACEMENT_EFFECT)


def test_clock_placement_in_flight_is_409(monkeypatch):
    """End to end: while a placement holds the process-level guard, a second
    POST is refused at once (non-blocking) and never reaches the Lambda."""
    _live(monkeypatch)
    monkeypatch.setenv(story_clock.STORY_HISTORY_FUNCTION_ENV, "fn")

    def _no_client():
        raise AssertionError("an in-flight refusal must not build a Lambda client")

    monkeypatch.setattr(story_clock, "_default_client", _no_client)
    assert story_clock._PLACE_IN_FLIGHT.acquire(blocking=False)
    try:
        resp = _client(is_admin=True).post(_CLOCK_URL, json=_BODY)
    finally:
        story_clock._PLACE_IN_FLIGHT.release()
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "in_flight", "message": story_clock.IN_FLIGHT_DETAIL}


def test_clock_route_is_post_only_and_mounted_on_the_real_app(monkeypatch, place_spy):
    _live(monkeypatch)
    assert _client(is_admin=True).get(_CLOCK_URL).status_code == 405
    import agents.server as server
    assert len(_routing_matches(server.app, "POST", _CLOCK_URL)) == 1
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        assert _routing_matches(server.app, method, _CLOCK_URL) == [], method
    operations = server.app.openapi()["paths"][_CLOCK_URL]
    assert set(operations) == {"post"}
    assert operations["post"]["operationId"].startswith(routes.workshop_story_clock_route.__name__)
    assert place_spy == []


def test_clock_reset_dry_run_end_to_end_is_lambda_free(monkeypatch):
    """ADR-0091 Design C through the REAL placement: the reset dry run reads the
    stored default (lookup stubbed) and invokes nothing."""
    from agents.demo import story_lookup

    _live(monkeypatch)
    monkeypatch.setenv(story_clock.STORY_HISTORY_FUNCTION_ENV, "fn")

    def _no_client():
        raise AssertionError("a dry run must not build a Lambda client")

    monkeypatch.setattr(story_clock, "_default_client", _no_client)
    monkeypatch.setattr(story_lookup, "stored_default", lambda: (1789990010, "deploy"))
    resp = _client(is_admin=True).post(_CLOCK_URL, json={
        "reset": True, "expected_anchor_epoch": 1790000010, "dry_run": True})
    assert resp.status_code == 200
    out = resp.json()
    assert (out["reset"], out["default_anchor_epoch"], out["default_source"]) == (True, 1789990010, "deploy")
    assert out["effect_codes"][0] == "restores_default"
    monkeypatch.setattr(story_lookup, "stored_default", lambda: None)
    resp = _client(is_admin=True).post(_CLOCK_URL, json={
        "reset": True, "expected_anchor_epoch": 1790000010, "dry_run": True})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "no_default_anchor"
