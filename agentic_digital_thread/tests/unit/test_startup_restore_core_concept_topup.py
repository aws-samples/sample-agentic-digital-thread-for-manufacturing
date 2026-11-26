# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The core concepts the startup seed leaves are sent again in the background.

On a fresh deploy the graph can drop every early connection. The startup
seed (``server._restore_seed_core_concepts``, bounded by
``CORE_CONCEPT_SEED_BUDGET_S``) then confirmed 9 of 65 core concepts and
nothing else sent the rest until the app restarted. Now what it leaves (not
confirmed, or not sent once its budget was spent) goes to a background
top-up (``server._CoreConceptTopUp``) on its own daemon thread. It sends
those concepts again in paced rounds until the graph confirms them all, its
bound (``CORE_CONCEPT_TOPUP_BOUND_S``) passes, or shutdown stops it. The
restore only starts it, so the restore, and the 503 refusal while it runs
(``startup_restore``), end as before. A write that timed out may still be
running, so no sender sends that concept again before the graph has stopped
it (its server limit plus ``db.GRAPH_READ_MARGIN_S``); and once shutdown has
begun no statement starts and no top-up starts.

The graph write (``db.neptune_write_outcome``, and ``db.neptune_write`` for
the code before it), the clock (``server._restore_clock``) and the top-up's
pause (``server._topup_wait``) are faked, so nothing sleeps. Old names are
patched with ``raising=False`` so these tests run, and fail, against the
code before the top-up.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

import agents.db as db
import agents.graph_resubmit as gr
import agents.server as server
from agents.catalog import CORE_CONCEPT_DEFINITIONS

IDS = list(CORE_CONCEPT_DEFINITIONS)
TOTAL = len(IDS)
#: The seed statement, as the restore sends it.
CORE_CONCEPT_SEED = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $desc, "
    "c.source = 'core'")
TOPUP_THREAD = "core-concept-topup"
BOUND_S = 3600
#: The real pause between rounds, before any test fakes it.
REAL_TOPUP_WAIT = getattr(server, "_topup_wait", None)


class _Clock:
    """A monotonic clock that only the fakes move."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Graph:
    """A fake ``db.neptune_write_outcome`` (:meth:`outcome`) and
    ``db.neptune_write`` (:meth:`__call__`) for the seed statement. Each send
    moves the clock by ``send_s``; ``confirms(cid, at)`` says how the send
    that started at ``at`` ends: True (confirmed, the default), False (an
    answered failure) or a ``graph_resubmit`` end such as ``TIMED_OUT``.
    Records each send with the thread it ran on, the resend opt-in it ran
    under and whether the process-wide seed lock was held, and how many
    sends were in flight at once."""

    def __init__(self, clock, send_s=1.0, confirms=None):
        self.clock = clock
        self.send_s = send_s
        self.confirms = confirms or (lambda _cid, _at: True)
        self.on_send = None
        self.sends: list = []
        self.confirmed: set = set()
        self.opt_ins: set = set()
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, cypher, parameters=None):
        return self.outcome(cypher, parameters) is None

    def outcome(self, cypher, parameters=None):
        assert cypher == CORE_CONCEPT_SEED
        cid = parameters["cid"]
        assert parameters["desc"] == CORE_CONCEPT_DEFINITIONS[cid]
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            send_lock = getattr(server, "_CORE_CONCEPT_SEND", None)
            self.opt_ins.add((db._RESUBMIT_SAFE.get(), db._RESUBMIT_AFTER_DROP.get(),
                              bool(send_lock is not None and send_lock.locked())))
            at = self.clock.now
            self.sends.append((cid, threading.current_thread().name, at))
            if self.on_send is not None:
                self.on_send(cid)
            self.clock.now += self.send_s
            ended = self.confirms(cid, at)
            if ended is True:
                self.confirmed.add(cid)
                return None
            return gr.FAILED if ended is False else ended
        finally:
            with self._lock:
                self.in_flight -= 1

    def topup_sends(self):
        return [s for s in self.sends if s[1] == TOPUP_THREAD]


class _Pauses:
    """A fake ``server._topup_wait``: records each pause and moves the clock
    by it; ``hook(stop, seconds)`` runs first when set. As a safety net it
    reports a stop after ``limit`` pauses, so a top-up that never ends its
    rounds still ends the test (and fails it)."""

    def __init__(self, clock, limit=500):
        self.clock = clock
        self.limit = limit
        self.hook = None
        self.seconds: list = []

    def __call__(self, stop, seconds):
        self.seconds.append(seconds)
        if self.hook is not None:
            self.hook(stop, seconds)
        self.clock.now += seconds
        return stop.is_set() or len(self.seconds) >= self.limit


def _topup():
    return getattr(server, "_core_concept_topup", None)


def _stop_topup():
    """What the app's shutdown does to the top-up."""
    topup = _topup()
    if topup is not None:
        (getattr(topup, "shut_down", None) or topup.stop.set)()


def _finish():
    """Wait for every top-up thread to end."""
    for thread in [t for t in threading.enumerate() if t.name == TOPUP_THREAD]:
        thread.join(timeout=5)
        assert not thread.is_alive(), "the core-concept top-up did not end"


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(server, "_restore_clock", clock)
    graph = _Graph(clock)
    monkeypatch.setattr(db, "neptune_write", graph)
    monkeypatch.setattr(db, "neptune_write_outcome", graph.outcome, raising=False)
    monkeypatch.setattr(server, "_CORE_CONCEPT_HELD", {}, raising=False)
    pauses = _Pauses(clock)
    monkeypatch.setattr(server, "_topup_wait", pauses, raising=False)
    if hasattr(server, "_CoreConceptTopUp"):
        monkeypatch.setattr(server, "_core_concept_topup", server._CoreConceptTopUp())
    yield SimpleNamespace(clock=clock, graph=graph, pauses=pauses)
    _stop_topup()
    _finish()


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "agents.server"
            and "core concept" in r.getMessage().lower().replace("-", " ")]


def _seed_line(confirmed, not_confirmed=(), not_sent=(), topped_up=True):
    parts = [f"{confirmed} of {TOTAL} core concepts confirmed"]
    if not_confirmed:
        parts.append(f"{len(not_confirmed)} not confirmed: {', '.join(not_confirmed)}")
    if not_sent:
        parts.append(f"{len(not_sent)} not sent (the 60s seed budget was spent): "
                     f"{', '.join(not_sent)}")
    left = len(not_confirmed) + len(not_sent)
    parts.append(f"a background top-up sends the {left} left again, for up to 60 min"
                 if topped_up else "the next start sends them again")
    return "; ".join(parts)


def _seed(caplog):
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()


# ── The top-up sends what the seed left ─────────────────────────────────────

def test_the_concepts_the_seed_did_not_send_are_sent_again_and_confirmed(env, caplog):
    """As seen live: each send takes 7 s, so the 60 s seed sends 9 core
    concepts and leaves 56 not sent. The top-up sends those 56 after its
    first pause, the graph confirms them, and it logs how long it took."""
    env.graph.send_s = 7
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    assert [s[0] for s in env.graph.sends] == IDS
    assert [s[0] for s in env.graph.topup_sends()] == IDS[9:]
    assert env.graph.confirmed == set(IDS)
    assert env.pauses.seconds == [15]
    assert _lines(caplog) == [
        _seed_line(9, not_sent=IDS[9:]),
        f"Core-concept top-up done {15 + 56 * 7}s after it started: the graph confirmed "
        f"all 56 core concepts the startup seed left"]


def test_only_the_concepts_the_seed_left_are_sent_again(env, caplog):
    """The seed's third and fifth sends are not confirmed and the budget
    leaves the rest not sent: the top-up sends exactly those, in order, and
    never a concept the graph already confirmed."""
    failed_once = {IDS[2], IDS[4]}
    seen: set = set()

    def confirms(cid, _at):
        if cid in failed_once and cid not in seen:
            seen.add(cid)
            return False
        return True
    env.graph.send_s, env.graph.confirms = 7, confirms
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    assert [s[0] for s in env.graph.topup_sends()] == [IDS[2], IDS[4]] + IDS[9:]
    assert env.graph.confirmed == set(IDS)
    assert _lines(caplog)[0] == _seed_line(7, [IDS[2], IDS[4]], IDS[9:])


def test_a_round_that_confirms_none_is_warned_once_and_the_rounds_back_off(env, caplog):
    """The graph confirms nothing before t=1200. The seed (1000-1060) and the
    first round confirm none, so that round logs one warning; the second
    round confirms some and logs nothing; the third confirms the rest. The
    pauses grow 15, 30, 60 s."""
    env.graph.confirms = lambda _cid, at: at >= 1200
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    assert env.pauses.seconds == [15, 30, 60]
    assert env.graph.confirmed == set(IDS)
    sent = [s[0] for s in env.graph.topup_sends()]
    assert sent == IDS + IDS + IDS[:30]
    assert _lines(caplog) == [
        _seed_line(0, IDS[:60], IDS[60:]),
        f"Core-concept top-up round 1 confirmed none of the {TOTAL} left; trying again in 30s",
        f"Core-concept top-up done 265s after it started: the graph confirmed all {TOTAL} "
        f"core concepts the startup seed left"]


def test_the_bound_ends_the_top_up_and_names_what_is_still_missing(env, caplog):
    """One concept is never confirmed. The rounds wait 15, 30, 60 and then
    120 s, start no statement after the bound, and end once no round fits
    before it, with one line naming what is missing and who sends it next."""
    never = IDS[0]
    env.graph.send_s = 0.5
    env.graph.confirms = lambda cid, _at: cid != never
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    started = 1000 + TOTAL * 0.5  # the seed's 65 sends end here; the top-up starts
    deadline = started + BOUND_S
    sends = env.graph.topup_sends()
    assert sends and {s[0] for s in sends} == {never}
    assert env.pauses.seconds[:5] == [15, 30, 60, 120, 120]
    assert set(env.pauses.seconds[4:]) == {120}
    assert all(at < deadline for _cid, _thread, at in sends)
    assert sum(env.pauses.seconds) < BOUND_S and env.clock.now < deadline, \
        "it went on pausing past its bound"
    assert env.clock.now + 120 >= deadline, "it stopped while another round still fit"
    lines = _lines(caplog)
    assert lines[0] == _seed_line(TOTAL - 1, [never])
    warnings = lines[1:-1]
    assert len(warnings) == len(sends) - 1
    assert all(w.endswith("confirmed none of the 1 left; trying again in "
                          f"{env.pauses.seconds[i + 1]}s") for i, w in enumerate(warnings))
    assert lines[-1] == (f"Core-concept top-up gave up {env.clock.now - started:.0f}s after it "
                         f"started (its bound is {BOUND_S}s) with 1 not confirmed: {never}; "
                         f"the next start sends them")


def test_no_top_up_starts_when_the_seed_confirms_every_concept(env, caplog):
    env.graph.send_s = 0.1
    _seed(caplog)
    assert TOPUP_THREAD not in {t.name for t in threading.enumerate()}
    assert getattr(_topup(), "thread", None) is None
    assert env.pauses.seconds == [] and len(env.graph.sends) == TOTAL
    assert _lines(caplog) == [f"{TOTAL} of {TOTAL} core concepts confirmed"]


def test_each_statement_is_the_seed_statement_under_its_opt_in_and_one_at_a_time(env, caplog):
    """The top-up sends the seed's own statement under the same resend opt-in
    (``resubmit_safe_write(after_drop=True)``) with the process-wide seed
    lock held, one statement at a time, and only after the seed's last
    statement returned."""
    env.graph.send_s = 7
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    assert env.graph.topup_sends()
    assert env.graph.opt_ins == {(True, True, True)}
    assert env.graph.max_in_flight == 1
    threads = [s[1] for s in env.graph.sends]
    first_topup = threads.index(TOPUP_THREAD)
    assert TOPUP_THREAD not in threads[:first_topup] and set(threads[first_topup:]) == {
        TOPUP_THREAD}


def test_a_second_seed_waits_for_the_top_up_statement_in_flight(env):
    """A later restore's seed (``graph_snapshot.repair_systems`` runs the
    restore again) never sends while the top-up's statement is in flight: it
    waits for the seed lock, within its own budget."""
    lock = getattr(server, "_CORE_CONCEPT_SEND", None)
    assert lock is not None, "no process-wide seed lock"
    lock.acquire()
    try:
        assert server._send_core_concepts(0.01, [IDS[0]]) == ([], [IDS[0]])
    finally:
        lock.release()
    assert env.graph.sends == []


# ── The restore does not wait for it ────────────────────────────────────────

def test_the_restore_ends_without_waiting_for_the_top_up(env, monkeypatch, caplog):
    """The restore runs the seed, starts the top-up and ends: startup sees it
    end in time while the top-up is still in its first pause, on its own
    daemon thread, and no 503 refusal goes up."""
    from agents import startup_restore
    paused, release = threading.Event(), threading.Event()

    def hold(_stop, _seconds):
        paused.set()
        release.wait(5)
    env.pauses.hook = hold
    env.graph.confirms = lambda _cid, _at: False
    monkeypatch.setattr(server, "_restore_sql_systems", lambda: set())
    monkeypatch.setattr(server, "_restore_direct_source_systems", lambda _ids: None)
    monkeypatch.setattr(server, "_restore", server._restore_base)
    try:
        with caplog.at_level(logging.INFO, logger="agents.server"):
            restore = server._restore_graph()
            assert restore.ended_in_time is True
            assert paused.wait(5), "no top-up started"
            (thread,) = [t for t in threading.enumerate() if t.name == TOPUP_THREAD]
            assert thread.daemon and thread.is_alive()
            assert not startup_restore.restoring()
    finally:
        _stop_topup()
        release.set()
        _finish()
    finished = [line for line in (r.getMessage() for r in caplog.records)
                if line.startswith("Neptune restore finished after ")]
    assert len(finished) == 1


# ── Shutdown stops it ───────────────────────────────────────────────────────

def _no_startup_steps(monkeypatch):
    noop = lambda *_a, **_k: None  # noqa: E731 - a stub step
    for attr in ("_validate_metric_turn_config", "_validate_model_allowlist",
                 "_start_digital_thread_provisioner", "_start_query_bridge_provisioner",
                 "_init_prompt_library", "_register_demo_systems", "seed_registry",
                 "_start_prompt_scheduler", "_register_runtime_keepwarm",
                 "register_auto_accept_job", "_start_cache_workers", "stop_scheduler"):
        monkeypatch.setattr(server, attr, noop, raising=False)
    monkeypatch.setattr(server, "_start_live_workers", lambda: (False, False))
    monkeypatch.setattr(server.mqtt_subscriber, "start_subscriber", noop)
    monkeypatch.setattr(server.mqtt_subscriber, "stop_subscriber", noop)
    monkeypatch.setattr(server._agent_manager, "stop_all", noop)


def test_the_app_shutdown_stops_the_top_up_in_its_pause(env, monkeypatch, caplog):
    """The lifespan's shutdown stops a top-up waiting between rounds at once
    (the real pause, which ends when the stop is set): nothing more is sent
    and it logs what it left for the next start."""
    _no_startup_steps(monkeypatch)
    paused = threading.Event()

    def wait(stop, seconds):
        paused.set()
        return REAL_TOPUP_WAIT(stop, seconds)
    monkeypatch.setattr(server, "_topup_wait", wait, raising=False)
    env.graph.confirms = lambda _cid, _at: False
    monkeypatch.setattr(server, "_restore", server._restore_seed_core_concepts)

    async def scenario():
        async with server.lifespan(server.app):
            assert await asyncio.to_thread(paused.wait, 5), "no top-up started"

    with caplog.at_level(logging.INFO, logger="agents.server"):
        asyncio.run(scenario())
        _finish()
    assert env.graph.topup_sends() == []
    assert _lines(caplog)[-1] == (
        f"Core-concept top-up stopped by shutdown with {TOTAL} not confirmed: "
        f"{', '.join(IDS)}; the next start sends them")


def test_shutdown_stops_the_top_up_between_statements(env, caplog):
    """A stop during a round ends it after the statement in flight: no other
    statement starts, and the line names what it left."""
    env.graph.send_s = 7

    def on_send(_cid):
        if len(env.graph.topup_sends()) == 3:
            _stop_topup()
    env.graph.on_send = on_send
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    assert [s[0] for s in env.graph.topup_sends()] == IDS[9:12]
    assert _lines(caplog)[-1] == (
        f"Core-concept top-up stopped by shutdown with {TOTAL - 12} not confirmed: "
        f"{', '.join(IDS[12:])}; the next start sends them")


def test_no_top_up_starts_once_shutdown_has_begun(env, caplog):
    _stop_topup()
    env.graph.confirms = lambda _cid, _at: False
    _seed(caplog)
    assert env.pauses.seconds == [] and env.graph.topup_sends() == []
    assert _lines(caplog) == [_seed_line(0, IDS[:60], IDS[60:], topped_up=False)]


# ── One per process ─────────────────────────────────────────────────────────

def test_at_most_one_top_up_runs_per_process(env, caplog):
    """A second seed in the same process (a repair re-runs the restore)
    starts no second top-up while the first one is running, and its line says
    the next start sends what it left."""
    paused, release = threading.Event(), threading.Event()

    def hold(_stop, _seconds):
        paused.set()
        release.wait(5)
    env.pauses.hook = hold
    env.graph.confirms = lambda _cid, _at: False
    try:
        _seed(caplog)
        assert paused.wait(5), "no top-up started"
        first = _topup().thread
        env.clock.now = 1000.0
        _seed(caplog)
        assert _topup().thread is first
        assert [t.name for t in threading.enumerate()].count(TOPUP_THREAD) == 1
    finally:
        _stop_topup()
        release.set()
        _finish()
    lines = _lines(caplog)
    assert lines[0] == _seed_line(0, IDS[:60], IDS[60:])
    assert lines[1] == _seed_line(0, IDS[:60], IDS[60:], topped_up=False)


def test_a_top_up_that_raises_is_logged_and_ends(env, caplog):
    env.graph.send_s = 7

    def on_send(_cid):
        if env.graph.topup_sends():
            raise RuntimeError("unexpected")
    env.graph.on_send = on_send
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    assert _lines(caplog)[-1] == ("Core-concept top-up ended early (RuntimeError); the next "
                                  "start sends the core concepts it had left")


# ── A write that timed out may still be running ─────────────────────────────

#: Seconds after a timed-out write returned before the graph has stopped it:
#: the server limit it was sent with plus the read margin.
HOLD_S = db.GRAPH_QUERY_TIMEOUT_MS / 1000 + db.GRAPH_READ_MARGIN_S


def test_a_timed_out_write_is_held_until_the_graph_has_stopped_it(env, caplog):
    """The first concept's seed write times out (a read timeout or a 504),
    so its first copy may still be running until the graph's own limit
    stops it. The top-up's first round falls inside that window and sends
    nothing (and warns of nothing); the next round, after it, sends the
    concept once and the graph confirms it."""
    timed_out = []

    def confirms(cid, _at):
        if cid == IDS[0] and not timed_out:
            timed_out.append(cid)
            return gr.TIMED_OUT
        return True
    env.graph.send_s, env.graph.confirms = 0.5, confirms
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
        _finish()
    sends = [at for cid, _thread, at in env.graph.sends if cid == IDS[0]]
    returned = 1000.5   # the timed-out write returned here
    assert HOLD_S == 65
    assert env.pauses.seconds == [15, 30]
    seed_end = 1000 + TOTAL * 0.5
    assert seed_end + 15 < returned + HOLD_S < seed_end + 15 + 30
    assert sends == [1000.0, seed_end + 15 + 30]
    assert env.graph.confirmed == set(IDS)
    assert _lines(caplog) == [
        _seed_line(TOTAL - 1, [IDS[0]]),
        f"Core-concept top-up done {15 + 30 + 0.5:.0f}s after it started: the graph "
        f"confirmed all 1 core concepts the startup seed left"]


@pytest.mark.parametrize("limit_ms", [None, 120_000], ids=["default-limit", "long-limit"])
def test_no_sender_resends_a_timed_out_concept_before_its_hold_ends(env, limit_ms):
    """The hold is the process's, not one top-up's: a later seed in the
    process (a repair re-runs the restore) does not send the concept either
    until the server limit the write was sent with, plus the read margin,
    has passed since the write returned (1 s after it was sent here); it
    reports it not confirmed meanwhile."""
    hold_s = (limit_ms or db.GRAPH_QUERY_TIMEOUT_MS) / 1000 + db.GRAPH_READ_MARGIN_S
    env.graph.send_s = 1
    env.graph.confirms = lambda _cid, at: gr.TIMED_OUT if at == 1000 else True
    if limit_ms is None:
        assert server._send_core_concepts(60, [IDS[0]]) == ([IDS[0]], [])
    else:
        with db.graph_statement_limit(limit_ms):
            assert server._send_core_concepts(60, [IDS[0]]) == ([IDS[0]], [])
    returned = 1001
    env.clock.now = returned + hold_s - 0.5
    assert server._send_core_concepts(60, [IDS[0]]) == ([IDS[0]], [])
    env.clock.now = returned + hold_s
    assert server._send_core_concepts(60, [IDS[0]]) == ([], [])
    assert [at for _cid, _thread, at in env.graph.sends] == [1000, returned + hold_s]


def test_the_app_shutdown_shuts_the_top_up_down(env, monkeypatch):
    """The lifespan's shutdown stops the top-up through ``shut_down``, which
    refuses a start from then on, not by setting its stop alone."""
    _no_startup_steps(monkeypatch)
    monkeypatch.setattr(server, "_restore", lambda: None)
    calls = []
    topup = _topup()
    real = getattr(topup, "shut_down", topup.stop.set)
    monkeypatch.setattr(topup, "shut_down", lambda: calls.append("shut_down") or real(),
                        raising=False)

    async def scenario():
        async with server.lifespan(server.app):
            pass

    asyncio.run(scenario())
    assert calls == ["shut_down"]
    assert topup.stop.is_set()


# ── Shutdown while a sender waits, or while the top-up starts ───────────────

class _SignallingLock:
    """A lock that sets ``asked`` when ``watch`` (any thread when ``None``)
    asks for it, before it blocks. The test takes ``lock`` directly, so its
    own hold does not count."""

    def __init__(self, watch=None):
        self.lock = threading.Lock()
        self.asked = threading.Event()
        self.watch = watch

    def acquire(self, blocking=True, timeout=-1):
        if self.watch is None or threading.current_thread() is self.watch:
            self.asked.set()
        return self.lock.acquire(blocking, timeout)

    def release(self):
        self.lock.release()

    def locked(self):
        return self.lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


def _waiting_sender(monkeypatch):
    """A sender (the top-up's next statement) waiting for the seed lock,
    which the test holds as another sender's statement in flight would."""
    lock = _SignallingLock()
    monkeypatch.setattr(server, "_CORE_CONCEPT_SEND", lock)
    stop, result = threading.Event(), {}
    sender = threading.Thread(target=lambda: result.update(
        sent=server._send_core_concepts(BOUND_S, [IDS[0]], stop)), daemon=True)
    lock.lock.acquire()
    sender.start()
    assert lock.asked.wait(5), "the sender never asked for the lock"
    return lock, stop, sender, result


def test_a_sender_waiting_for_the_seed_lock_sends_nothing_once_shutdown_has_begun(
        env, monkeypatch):
    """Shutdown begins while the top-up waits for another sender's
    statement; that statement then ends. The top-up gets the lock but starts
    no write: it checks the stop again once it holds it."""
    lock, stop, sender, result = _waiting_sender(monkeypatch)
    try:
        stop.set()
    finally:
        lock.lock.release()
    sender.join(5)
    assert not sender.is_alive()
    assert env.graph.sends == []
    assert result["sent"] == ([], [IDS[0]])
    assert not lock.locked()


def test_a_sender_waiting_for_the_seed_lock_stops_waiting_at_shutdown(env, monkeypatch):
    """Shutdown begins while the top-up waits for another sender's
    statement, which can run 105 s: the top-up stops waiting within one wait
    slice instead of waiting for it."""
    monkeypatch.setattr(server, "CORE_CONCEPT_SEND_WAIT_SLICE_S", 0.05, raising=False)
    lock, stop, sender, result = _waiting_sender(monkeypatch)
    try:
        stop.set()
        sender.join(5)
        stopped_waiting = not sender.is_alive()
    finally:
        lock.lock.release()
        sender.join(5)
    assert stopped_waiting, "the sender waited for the statement in flight"
    assert result["sent"] == ([], [IDS[0]])
    assert env.graph.sends == []


class _RecordingStop(threading.Event):
    """The top-up's stop event. Records, each time it is set, whether the
    top-up had no thread or had already started it."""

    def __init__(self, topup):
        super().__init__()
        self.topup = topup
        self.started_when_set: list = []

    def set(self):
        thread = self.topup.thread
        self.started_when_set.append(thread is None or thread.ident is not None)
        super().set()


class _ShutDownOnTheSeedLine(logging.Handler):
    """Runs ``shutdown`` once, on the thread ``shutter``, when the seed logs
    that a top-up sends what it left, and waits until it asked for the
    top-up's lock (or ended)."""

    def __init__(self, shutter, asked):
        super().__init__()
        self.shutter, self.asked = shutter, asked

    def emit(self, record):
        if "a background top-up sends" in record.getMessage() and self.shutter.ident is None:
            self.shutter.start()
            assert self.asked.wait(5), "shutdown neither asked for the lock nor ended"


def test_shutdown_never_falls_between_claiming_the_top_up_and_starting_it(env, caplog):
    """Shutdown runs while the seed is starting the top-up, between claiming
    it and starting its thread. It waits for the start, so the top-up's stop
    is never set on a claimed thread that has not started; the top-up then
    stops at its first pause and sends nothing."""
    topup = _topup()
    stop = _RecordingStop(topup)
    topup.stop = stop
    shutter = threading.Thread(target=lambda: (_stop_topup(), lock.asked.set()),
                               name="test-shutdown", daemon=True)
    lock = _SignallingLock(watch=shutter)
    topup._lock = lock
    env.pauses.hook = lambda stop_, _s: stop_.wait(5)
    env.graph.confirms = lambda _cid, _at: False
    handler = _ShutDownOnTheSeedLine(shutter, lock.asked)
    logging.getLogger("agents.server").addHandler(handler)
    try:
        with caplog.at_level(logging.INFO, logger="agents.server"):
            server._restore_seed_core_concepts()
            shutter.join(5)
            _finish()
    finally:
        logging.getLogger("agents.server").removeHandler(handler)
    assert stop.started_when_set == [True]
    assert env.graph.topup_sends() == []
    assert _lines(caplog)[-1].startswith("Core-concept top-up stopped by shutdown with "
                                         f"{TOTAL} not confirmed")
