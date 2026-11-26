# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""/health answers while a slow startup restore runs.

``server.lifespan`` used to wait for the whole startup restore before the app
answered anything, however long it took, so a restore that outlasted the ECS
service's health-check grace got the task replaced, and the replacement faced
the same restore. Startup now waits for the restore up to
``STARTUP_RESTORE_WAIT_S``; a restore still running then finishes in the
background. The startup steps that read or write what the restore rebuilds
are held until it ends, and the writes that would race it are refused with a
retryable 503 (``startup_restore``) until then.

Every startup step is replaced by a recorder here, so no real worker starts.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from agents import server

#: Seconds a test lets the restore block before a timer releases it, so an
#: app that waits for the whole restore still finishes the test (and fails it)
#: instead of hanging.
_SAFETY_RELEASE_S = 1.0
#: The startup budget the tests run with.
_BUDGET_S = 0.05


class _Recorder:
    """Records each startup step as it runs, with the thread it ran on."""

    def __init__(self):
        self.ran: list[str] = []
        self.threads: dict[str, str] = {}
        self.lock = threading.Lock()

    def step(self, name, result=None):
        def _run(*_a, **_k):
            with self.lock:
                self.ran.append(name)
                self.threads[name] = threading.current_thread().name
            return result
        return _run


#: (server attribute, recorded name) in today's lifespan order. Old names are
#: patched too (raising=False), so the old lifespan runs its own order here.
_STEPS = (
    ("_start_digital_thread_provisioner", "dt-provisioner"),
    ("_start_query_bridge_provisioner", "qb-provisioner"),
    ("_init_prompt_library", "prompt-library"),
    ("_init_prompt_scheduler", "prompt-scheduler-old"),
    ("_register_demo_systems", "demo-systems"),
    ("seed_registry", "seed-registry"),
    ("_start_prompt_scheduler", "start-scheduler"),
    ("_register_runtime_keepwarm", "keepwarm"),
    ("register_auto_accept_job", "auto-accept"),
    ("_start_cache_workers", "cache-workers"),
)

#: The order every step runs in when the restore ends within the budget:
#: exactly the lifespan's order before this change.
_IN_ORDER = ["dt-provisioner", "qb-provisioner", "mqtt", "prompt-library", "demo-systems",
             "seed-registry", "start-scheduler", "keepwarm", "auto-accept", "live-workers",
             "cache-workers"]
#: What runs before the app serves when the restore is still running.
_NOT_HELD = ["mqtt", "prompt-library", "seed-registry"]
#: What runs once the restore ends: the off-loop step first, then the rest in
#: lifespan order on the event loop.
_HELD = ["demo-systems", "dt-provisioner", "qb-provisioner", "start-scheduler", "keepwarm",
         "auto-accept", "live-workers", "cache-workers"]


@pytest.fixture
def steps(monkeypatch):
    rec = _Recorder()
    for attr, name in _STEPS:
        monkeypatch.setattr(server, attr, rec.step(name), raising=False)
    monkeypatch.setattr(server, "_start_live_workers", rec.step("live-workers", (False, False)))
    monkeypatch.setattr(server.mqtt_subscriber, "start_subscriber", rec.step("mqtt"))
    monkeypatch.setattr(server.mqtt_subscriber, "stop_subscriber", rec.step("stop-mqtt"))
    monkeypatch.setattr(server, "stop_scheduler", rec.step("stop-scheduler"))
    monkeypatch.setattr(server._agent_manager, "stop_all", rec.step("stop-agents"))
    monkeypatch.setattr(server, "_validate_metric_turn_config", lambda: None)
    monkeypatch.setattr(server, "_validate_model_allowlist", lambda: None)
    monkeypatch.setattr(server, "STARTUP_RESTORE_WAIT_S", _BUDGET_S, raising=False)
    monkeypatch.setattr(server, "RESTORE_WAIT_NOTICE_S", _BUDGET_S, raising=False)
    yield rec
    try:
        from agents import startup_restore
    except ImportError:  # the code before the fix has no refusal to clear
        return
    startup_restore.end()


class _SlowRestore:
    """A ``server._restore`` that blocks until released (or, as a safety net,
    until ``_SAFETY_RELEASE_S`` has passed), then raises ``error`` if set.
    Every instance is released and its thread joined when its test ends
    (:func:`_no_restore_outlives_its_test`), so a restore thread never logs
    into the next test's captured log."""

    def __init__(self, error: BaseException | None = None):
        self.release = threading.Event()
        self.ended = threading.Event()
        self.error = error
        self.thread: threading.Thread | None = None
        self._timer = threading.Timer(_SAFETY_RELEASE_S, self.release.set)
        _LIVE_RESTORES.append(self)

    def __call__(self):
        self.thread = threading.current_thread()
        try:
            self.release.wait()
            if self.error is not None:
                raise self.error
        finally:
            self.ended.set()

    def arm(self):
        self._timer.daemon = True
        self._timer.start()
        return self


#: Every _SlowRestore made by the running test.
_LIVE_RESTORES: list = []


@pytest.fixture(autouse=True)
def _no_restore_outlives_its_test(monkeypatch):
    """Release every fake restore the test made and wait for its thread,
    which logs how the restore ended and then runs what waited for it. A
    thread left running would log "Neptune restore finished" into the next
    test's captured log (seen in CI under load). Asks for ``monkeypatch`` so
    the join runs before the test's patches are undone."""
    yield
    while _LIVE_RESTORES:
        restore = _LIVE_RESTORES.pop()
        restore._timer.cancel()
        restore.release.set()
        if restore.thread is not None:
            restore.thread.join(timeout=5)
            assert not restore.thread.is_alive(), "a fake restore thread did not end"


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def _restore_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.server" and "Neptune restore" in r.getMessage()]


def _restoring():
    """Whether the refusal is up (``None`` on the code before the fix, which
    has none)."""
    try:
        from agents import startup_restore
    except ImportError:
        return None
    return startup_restore.restoring()


# ── The restore outlasts the budget ──────────────────────────────────────────

def test_the_app_starts_serving_while_a_slow_restore_runs(steps, monkeypatch, caplog):
    """A restore still running at the budget no longer holds startup: the
    lifespan yields with the restore running, only the steps that do not read
    what it rebuilds have run, and changes to onboarded systems are refused.
    When it ends, the held steps run once, in order, and the refusal lifts."""
    restore = _SlowRestore().arm()
    monkeypatch.setattr(server, "_restore", restore)
    seen = {}

    async def scenario():
        async with server.lifespan(server.app):
            seen["restore_ended_at_yield"] = restore.ended.is_set()
            seen["ran_at_yield"] = list(steps.ran)
            seen["restoring_at_yield"] = _restoring()
            restore.release.set()
            await _until(lambda: "cache-workers" in steps.ran)
            seen["restoring_after"] = _restoring()
        seen["after_shutdown"] = list(steps.ran)

    with caplog.at_level(logging.INFO, logger="agents.server"):
        asyncio.run(scenario())

    assert seen["restore_ended_at_yield"] is False, "startup waited for the whole restore"
    assert seen["ran_at_yield"] == _NOT_HELD
    assert seen["restoring_at_yield"] is True
    assert seen["restoring_after"] is False
    assert seen["after_shutdown"] == (_NOT_HELD + _HELD
                                      + ["stop-scheduler", "stop-agents", "stop-mqtt"])
    assert steps.threads["demo-systems"] == "startup-restore"
    assert steps.threads["start-scheduler"] == threading.main_thread().name
    lines = _restore_lines(caplog)
    assert len(lines) == 2, lines
    assert lines[0].startswith("Neptune restore still running after ")
    assert "finishes in the background" in lines[0]
    assert lines[1].startswith("Neptune restore finished after ")


def test_health_answers_while_the_restore_runs(steps, monkeypatch):
    """The real app answers /health (200, body unchanged) while the startup
    restore is still running."""
    restore = _SlowRestore().arm()
    monkeypatch.setattr(server, "_restore", restore)
    with TestClient(server.app) as client:
        running = not restore.ended.is_set()
        response = client.get("/health")
        restore.release.set()
    assert running, "the app did not answer until the restore ended"
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": server._APP_VERSION}


def test_a_restore_that_fails_in_the_background_is_logged_and_the_held_steps_run(
        steps, monkeypatch, caplog):
    """A background restore that raises is logged as failed with how long it
    ran, and the held steps still run, as they did after a failed restore."""
    restore = _SlowRestore(error=RuntimeError("no graph")).arm()
    monkeypatch.setattr(server, "_restore", restore)

    async def scenario():
        async with server.lifespan(server.app):
            restore.release.set()
            await _until(lambda: "cache-workers" in steps.ran)

    with caplog.at_level(logging.INFO, logger="agents.server"):
        asyncio.run(scenario())
    assert steps.ran[:len(_NOT_HELD) + len(_HELD)] == _NOT_HELD + _HELD
    lines = _restore_lines(caplog)
    assert len(lines) == 2, lines
    assert lines[1].startswith("Neptune restore failed after ")
    assert lines[1].endswith("s (RuntimeError: no graph)")


def test_no_held_step_starts_after_shutdown(steps, monkeypatch):
    """Shut down before the restore ends: the held steps never start, the
    scheduler (never started) is not stopped, and the rest of the shutdown
    runs."""
    restore = _SlowRestore().arm()
    monkeypatch.setattr(server, "_restore", restore)

    async def scenario():
        async with server.lifespan(server.app):
            pass

    asyncio.run(scenario())
    assert not _restoring()
    restore.release.set()
    restore.thread.join(timeout=5)   # the held steps would run on this thread first
    assert restore.ended.is_set()
    assert steps.ran == _NOT_HELD + ["stop-agents", "stop-mqtt"]


def test_a_held_step_that_raises_is_logged_and_the_next_still_runs(
        steps, monkeypatch, caplog):
    restore = _SlowRestore().arm()
    monkeypatch.setattr(server, "_restore", restore)

    def _register_runtime_keepwarm():
        raise ValueError("bad step")
    monkeypatch.setattr(server, "_register_runtime_keepwarm", _register_runtime_keepwarm)

    async def scenario():
        async with server.lifespan(server.app):
            restore.release.set()
            await _until(lambda: "cache-workers" in steps.ran)

    with caplog.at_level(logging.INFO, logger="agents.server"):
        asyncio.run(scenario())
    assert "auto-accept" in steps.ran and "keepwarm" not in steps.ran
    failed = [r.getMessage() for r in caplog.records if "_register_runtime_keepwarm" in
              r.getMessage()]
    assert failed and "ValueError" in failed[0], failed


def test_a_startup_step_that_raises_before_serving_stops_startup_as_before(
        steps, monkeypatch):
    """A step that is not held and raises stops startup, as before, with the
    restore still running: nothing held ever runs and no refusal is left up."""
    restore = _SlowRestore().arm()
    monkeypatch.setattr(server, "_restore", restore)

    def _init_prompt_library():
        raise ValueError("no prompt store")
    monkeypatch.setattr(server, "_init_prompt_library", _init_prompt_library)

    async def scenario():
        async with server.lifespan(server.app):
            pass  # pragma: no cover - startup raises first

    with pytest.raises(ValueError, match="no prompt store"):
        asyncio.run(scenario())
    assert not _restoring()
    restore.release.set()
    restore.thread.join(timeout=5)
    assert restore.ended.is_set()
    assert steps.ran == ["mqtt"]
    assert not _restoring()


# ── The restore ends within the budget ───────────────────────────────────────

def test_a_restore_that_ends_in_time_runs_every_step_in_order_before_serving(
        steps, monkeypatch, caplog):
    """The startup steps run in today's order before the app serves, and the
    blocking off-loop step (demo registration) runs on a worker thread while
    the event loop waits for it, never on the loop's own thread."""
    monkeypatch.setattr(server, "_restore", lambda: None)
    seen = {}

    async def scenario():
        async with server.lifespan(server.app):
            seen["ran_at_yield"] = list(steps.ran)
            seen["restoring"] = _restoring()

    with caplog.at_level(logging.INFO, logger="agents.server"):
        asyncio.run(scenario())
    assert seen["ran_at_yield"] == _IN_ORDER
    assert seen["restoring"] is False
    assert steps.threads["demo-systems"] != threading.main_thread().name
    assert steps.threads["start-scheduler"] == threading.main_thread().name
    lines = _restore_lines(caplog)
    assert len(lines) == 1 and lines[0].startswith("Neptune restore finished after "), lines


def test_a_restore_that_ends_after_the_wait_but_before_release_runs_nothing_on_the_loop(
        steps, monkeypatch):
    """The restore outlasts the wait and ends while the startup steps that do
    not wait for it are running, so it has ended by ``after.release()``. The
    held steps still run after the app starts serving: none runs on the
    event loop before ``yield``, and the off-loop step never runs on the
    loop's thread."""
    restore = _SlowRestore().arm()
    monkeypatch.setattr(server, "_restore", restore)
    started = {}
    real_restore_graph = server._restore_graph

    def _restore_graph():
        started["restore"] = real_restore_graph()
        return started["restore"]
    monkeypatch.setattr(server, "_restore_graph", _restore_graph)
    record_library = steps.step("prompt-library")

    def _init_prompt_library():  # not held: the restore ends while it runs
        restore.release.set()
        assert started["restore"].wait(5)
        record_library()
    monkeypatch.setattr(server, "_init_prompt_library", _init_prompt_library)
    seen = {}

    async def scenario():
        async with server.lifespan(server.app):
            seen["ran_at_yield"] = list(steps.ran)
            await _until(lambda: "cache-workers" in steps.ran)

    asyncio.run(scenario())
    loop_steps = set(_HELD) - {"demo-systems"}
    assert started["restore"].ended_in_time is False
    assert loop_steps.isdisjoint(seen["ran_at_yield"]), seen["ran_at_yield"]
    assert steps.threads["demo-systems"] != threading.main_thread().name
    assert steps.threads["start-scheduler"] == threading.main_thread().name
    assert steps.ran[:len(_NOT_HELD) + len(_HELD)] == _NOT_HELD + _HELD


# ── The refusal ──────────────────────────────────────────────────────────────
# Which routes carry it: test_startup_restore_writers.py (a scan of every
# route that reaches a restore-competing writer). How they answer:
# test_startup_restore_routes.py.

def test_a_refused_route_answers_a_retryable_503_in_plain_words():
    from agents import startup_restore
    app = FastAPI()
    startup_restore.install(app)

    @app.post("/write", dependencies=[Depends(startup_restore.refuse_while_restoring)])
    def write():
        return {"status": "ok"}

    client = TestClient(app)
    assert client.post("/write").status_code == 200
    startup_restore.begin()
    try:
        response = client.post("/write")
    finally:
        startup_restore.end()
    assert response.status_code == 503
    assert response.json() == {"detail": "The app is still restoring onboarded systems after "
                                         "a restart. Try again in a minute.",
                               "reason": "startup_restore"}
    assert response.headers["Retry-After"] == str(startup_restore.RETRY_AFTER_S)
    assert client.post("/write").status_code == 200


def test_the_real_app_answers_the_refusal_with_its_reason():
    from agents import startup_restore
    assert server.app.exception_handlers.get(startup_restore.StartupRestoring) is \
        startup_restore.restoring_response


@pytest.fixture
def restoring():
    from agents import startup_restore
    startup_restore.begin()
    yield startup_restore
    startup_restore.end()


def _must_not_run(name):
    def _run(*_a, **_k):
        raise AssertionError(f"{name} ran while the restore was running")
    return _run


def test_the_chat_removal_is_refused_while_restoring(restoring, monkeypatch):
    from agents import onboard
    monkeypatch.setattr(onboard, "_claim_reset", _must_not_run("_claim_reset"))
    assert onboard.start_removal_for_chat("sys-a") == {
        "error": restoring.RESTORING_DETAIL, "reason": restoring.RESTORING_REASON,
        "system_id": "sys-a"}


def test_the_chat_provision_is_refused_while_restoring(restoring, monkeypatch):
    from agents import onboard, onboard_chat_lifecycle
    monkeypatch.setattr(onboard, "_claim_provision", _must_not_run("_claim_provision"))
    monkeypatch.setattr(onboard, "_reset_conflict_or_none", lambda _sid: None)
    assert onboard_chat_lifecycle._start("sys-a", {}) == {
        "error": restoring.RESTORING_DETAIL, "reason": restoring.RESTORING_REASON,
        "system_id": "sys-a"}


def test_the_chat_credentials_store_is_refused_while_restoring(restoring, monkeypatch):
    from agents import onboard_agent, onboard_chat_lifecycle
    monkeypatch.setattr(onboard_agent, "_removal_running_refusal", lambda _sid: None)
    monkeypatch.setattr(onboard_agent, "_removal_failed_refusal", lambda _sid: None)
    monkeypatch.setattr(onboard_chat_lifecycle, "store_system_credentials",
                        _must_not_run("store_system_credentials"))
    assert onboard_chat_lifecycle.store_credentials("sys-a", '{"token": "x"}') == {
        "error": restoring.RESTORING_DETAIL, "reason": restoring.RESTORING_REASON,
        "system_id": "sys-a"}


def test_a_chat_registration_is_refused_while_restoring(restoring, monkeypatch):
    from agents import onboard_agent
    monkeypatch.setattr(onboard_agent, "register_system", _must_not_run("register_system"))
    assert onboard_agent.connect_new_system(
        name="Line Data", system_type="MES", plant="Site One",
        connection_type="PostgreSQL", schema="line_data") == {
        "error": restoring.RESTORING_DETAIL, "reason": restoring.RESTORING_REASON}


def test_nothing_is_refused_when_no_restore_is_running():
    from agents import startup_restore
    assert not startup_restore.restoring()
    assert startup_restore.refusal("sys-a") is None
    startup_restore.refuse_while_restoring()  # no HTTPException


def test_the_refusal_raises_the_503():
    from agents import startup_restore
    startup_restore.begin()
    try:
        with pytest.raises(HTTPException) as caught:
            startup_restore.refuse_while_restoring()
    finally:
        startup_restore.end()
    assert caught.value.status_code == 503
