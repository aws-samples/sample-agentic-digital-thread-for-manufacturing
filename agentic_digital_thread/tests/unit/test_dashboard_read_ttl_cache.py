# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``dashboard.dashboard_read_cached`` — the ONE bounded
TTL + single-flight cache shared by every expensive dashboard PANEL read
(sections, active-issues, line-state, and the grouped by-dimension/heatmap
resolves).

Why one generic helper: the line-state cache proved the algorithm (TTL +
per-key single-flight) but used UNBOUNDED dicts for both entries and locks.
Copying it once per endpoint would have made memory grow with the
plant/metric/dimension cardinality, so the algorithm moved into one helper with
a bounded LRU of completed entries and a FIXED array of striped locks.

Never caches errors: a raising compute propagates and leaves nothing behind,
and a friendly ``status='error'`` HTTP-200 envelope (the boundary style these
routes use) is returned but NOT stored — otherwise a transient backend fault
would be pinned in front of every viewer for a whole TTL window.

No AWS, no network. Threads are used for the single-flight pin only.
"""
from __future__ import annotations

import threading
import time

import pytest

import agents.dashboard as dashboard


@pytest.fixture(autouse=True)
def _fresh_cache():
    dashboard.invalidate_dashboard_read_cache()
    yield
    dashboard.invalidate_dashboard_read_cache()


def _counter():
    """A compute that returns its own invocation count."""
    calls = {"n": 0}

    def compute(*args):
        calls["n"] += 1
        return {"n": calls["n"], "args": args, "status": "ok"}

    return calls, compute


# --- single-flight -----------------------------------------------------------

def test_single_flight_under_concurrent_callers():
    """N concurrent callers on a COLD key produce exactly ONE compute and all
    receive the SAME value — the convoy of identical panel reads that a browser
    test wave (or several viewers) produces collapses to one backend sweep."""
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    calls = {"n": 0}
    results: list = [None] * n_threads

    def compute():
        calls["n"] += 1
        time.sleep(0.05)                  # a slow read while the others arrive
        return {"sweep": calls["n"], "status": "ok"}

    def worker(i):
        barrier.wait()
        results[i] = dashboard.dashboard_read_cached(("sections", "all"), compute)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    assert all(r == {"sweep": 1, "status": "ok"} for r in results)
    assert dashboard._READ_FLIGHTS == {}, "the finished flight was not retired"


# --- single-flight ALSO covers failures -------------------------------------
# The convoy that matters most is the FAILING one: when a source system is down,
# every viewer's panel read is slow AND doomed. Errors must never be cached, but
# the callers already joined to an in-flight attempt must SHARE it rather than
# each re-running the same doomed sweep (that is a stampede, not a cache). The
# flight is retired the moment it settles, so the NEXT request re-probes.

def _fan_in(n_threads: int, key: tuple, compute):
    """Release ``n_threads`` callers on one barrier; collect values/exceptions."""
    barrier = threading.Barrier(n_threads)
    outcomes: list = [None] * n_threads

    def worker(i):
        barrier.wait()
        try:
            outcomes[i] = ("ok", dashboard.dashboard_read_cached(key, compute))
        except BaseException as exc:                       # noqa: BLE001
            outcomes[i] = ("raised", exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "a caller never returned — flight deadlock"
    return outcomes


def test_error_envelope_is_computed_once_for_concurrent_callers(monkeypatch):
    """8 concurrent callers, a compute that returns the friendly ``status='error'``
    HTTP-200 envelope: ONE compute, all 8 get it, NOTHING is cached, and the next
    (non-concurrent) request re-probes and can recover."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        time.sleep(0.03)
        return {"status": "error", "error": "source down", "attempt": calls["n"]}

    outcomes = _fan_in(8, ("sections", "all"), compute)

    assert calls["n"] == 1, "the failing sweep ran once per caller (stampede)"
    assert all(o == ("ok", {"status": "error", "error": "source down", "attempt": 1})
               for o in outcomes)
    assert dashboard._READ_CACHE == {}, "an error envelope was cached"
    assert dashboard._READ_FLIGHTS == {}, "the failed flight was not retired"

    # Recovery: the very next request must recompute, not be served the failure.
    def recovered():
        calls["n"] += 1
        return {"status": "ok", "attempt": calls["n"]}

    assert dashboard.dashboard_read_cached(("sections", "all"), recovered) == {
        "status": "ok", "attempt": 2}
    assert calls["n"] == 2


def test_raised_exception_is_computed_once_and_reaches_every_caller(monkeypatch):
    """Same for a RAISING compute: one attempt, and every joined caller sees the
    SAME failure — propagated, never swallowed into a fallback value."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        time.sleep(0.03)
        raise RuntimeError(f"boom-{calls['n']}")

    outcomes = _fan_in(8, ("active-issues", "all"), compute)

    assert calls["n"] == 1, "the raising sweep ran once per caller (stampede)"
    assert all(kind == "raised" for kind, _ in outcomes)
    assert {str(exc) for _, exc in outcomes} == {"boom-1"}
    assert all(isinstance(exc, RuntimeError) for _, exc in outcomes)
    assert dashboard._READ_CACHE == {}, "a raised failure left a cache entry"
    assert dashboard._READ_FLIGHTS == {}, "the raising flight was not retired"

    calls2, recovered = _counter()
    assert dashboard.dashboard_read_cached(("active-issues", "all"), recovered)["n"] == 1


def test_a_caller_never_joins_a_pre_invalidation_flight(monkeypatch):
    """Generation tagging: a caller arriving AFTER an invalidation must not be
    handed the in-flight pre-invalidation answer — it computes afresh."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    release = threading.Event()
    old_calls, old_compute = _blocking_compute(release, {"status": "ok", "v": "old"})
    leader = threading.Thread(
        target=lambda: dashboard.dashboard_read_cached(("sections", "p"), old_compute),
        daemon=True)
    leader.start()
    for _ in range(500):
        if old_calls["n"] == 1:
            break
        time.sleep(0.002)
    assert old_calls["n"] == 1, "the leader never started"

    dashboard.invalidate_dashboard_read_cache()
    # Pre-released: this caller must return on ITS OWN compute, so any wait here
    # means it was (wrongly) parked on the old generation's flight.
    ready = threading.Event()
    ready.set()
    new_calls, new_compute = _blocking_compute(ready, {"status": "ok", "v": "new"})
    joiner_got: list = []
    joiner = threading.Thread(
        target=lambda: joiner_got.append(
            dashboard.dashboard_read_cached(("sections", "p"), new_compute)),
        daemon=True)
    joiner.start()
    joiner.join(timeout=5)
    assert not joiner.is_alive(), "the post-invalidation caller joined the old flight"

    assert joiner_got == [{"status": "ok", "v": "new"}]
    assert new_calls["n"] == 1, "the post-invalidation caller did not recompute"
    release.set()
    leader.join(timeout=5)


def test_an_old_leader_cleanup_does_not_retire_the_replacement_flight(monkeypatch):
    """The pre-invalidation leader finishing LAST must remove only ITS OWN flight,
    or the replacement loses its joiners and the stampede returns."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    key = ("line-state", "all")
    old_release, new_release = threading.Event(), threading.Event()
    old_calls, old_compute = _blocking_compute(old_release, {"status": "ok", "v": "old"})
    new_calls, new_compute = _blocking_compute(new_release, {"status": "ok", "v": "new"})

    old_leader = threading.Thread(
        target=lambda: dashboard.dashboard_read_cached(key, old_compute), daemon=True)
    old_leader.start()
    for _ in range(500):
        if old_calls["n"] == 1:
            break
        time.sleep(0.002)
    assert old_calls["n"] == 1

    dashboard.invalidate_dashboard_read_cache()
    new_leader = threading.Thread(
        target=lambda: dashboard.dashboard_read_cached(key, new_compute), daemon=True)
    new_leader.start()
    for _ in range(500):
        if new_calls["n"] == 1:
            break
        time.sleep(0.002)
    assert new_calls["n"] == 1, "the replacement leader never started"

    old_release.set()                      # the OLD leader's cleanup runs now
    old_leader.join(timeout=5)
    assert not old_leader.is_alive()

    joined: list = []
    joiner = threading.Thread(
        target=lambda: joined.append(dashboard.dashboard_read_cached(key, new_compute)),
        daemon=True)
    joiner.start()
    time.sleep(0.05)
    new_release.set()
    joiner.join(timeout=5)
    new_leader.join(timeout=5)
    assert not joiner.is_alive()

    assert joined == [{"status": "ok", "v": "new"}]
    assert new_calls["n"] == 1, "the joiner recomputed — the replacement was retired"
    assert dashboard._READ_FLIGHTS == {}


def test_a_reentrant_same_thread_read_computes_instead_of_deadlocking(monkeypatch):
    """A compute that (today: does not, tomorrow: might) read the SAME key must
    not wait on its own flight. Sharing is per OTHER caller; the leader's own
    thread computes rather than hanging the request."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    key = ("sections", "reentrant")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        if calls["n"] == 1:
            inner = dashboard.dashboard_read_cached(key, compute)
            return {"status": "ok", "outer": True, "inner": inner}
        return {"status": "ok", "outer": False}

    assert dashboard.dashboard_read_cached(key, compute) == {
        "status": "ok", "outer": True, "inner": {"status": "ok", "outer": False}}
    assert calls["n"] == 2


# --- TTL --------------------------------------------------------------------

def test_second_call_within_ttl_serves_cached(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()
    key = ("sections", "all")

    first = dashboard.dashboard_read_cached(key, compute, "all")
    second = dashboard.dashboard_read_cached(key, compute, "all")

    assert calls["n"] == 1
    assert second is first


def test_ttl_expiry_recomputes(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "0.05")
    calls, compute = _counter()
    key = ("sections", "all")

    assert dashboard.dashboard_read_cached(key, compute)["n"] == 1
    time.sleep(0.06)
    assert dashboard.dashboard_read_cached(key, compute)["n"] == 2
    assert calls["n"] == 2


def test_ttl_zero_disables_caching(monkeypatch):
    """The rollback lever: TTL 0 bypasses the cache entirely (always fresh)."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "0")
    calls, compute = _counter()
    key = ("sections", "all")

    assert dashboard.dashboard_read_cached(key, compute)["n"] == 1
    assert dashboard.dashboard_read_cached(key, compute)["n"] == 2
    assert dashboard._READ_CACHE == {}


def test_invalid_ttl_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "not-a-number")
    assert dashboard._panel_read_ttl_seconds() == 60.0


def test_line_state_wrapper_keeps_its_own_ttl(monkeypatch):
    """``line_state_cached`` stays a thin compatibility wrapper on the generic
    helper and keeps honoring its OWN (shorter) env TTL."""
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "0")
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()

    assert dashboard.line_state_cached(("line-state", "all"), compute)["n"] == 1
    assert dashboard.line_state_cached(("line-state", "all"), compute)["n"] == 2


# --- errors are never cached ------------------------------------------------

def test_raised_exception_is_not_cached(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("backend down")
        return {"status": "ok", "value": "recovered"}

    key = ("active-issues", "all")
    with pytest.raises(RuntimeError, match="backend down"):
        dashboard.dashboard_read_cached(key, compute)
    assert dashboard._READ_CACHE == {}
    assert dashboard.dashboard_read_cached(key, compute)["value"] == "recovered"


def test_error_status_envelope_is_returned_but_not_cached(monkeypatch):
    """These routes convert boundary faults into friendly HTTP-200
    ``status='error'`` envelopes. Caching one would keep a transient fault in
    front of every viewer for a whole window, so it is served and dropped."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"kind": "active-issues", "issues": [], "status": "error",
                    "error": "temporarily unavailable"}
        return {"kind": "active-issues", "issues": [{"t": "x"}], "status": "ok"}

    key = ("active-issues", "all")
    assert dashboard.dashboard_read_cached(key, compute)["status"] == "error"
    assert dashboard._READ_CACHE == {}
    assert dashboard.dashboard_read_cached(key, compute)["status"] == "ok"
    assert calls["n"] == 2


def test_nested_section_error_is_not_cached(monkeypatch):
    """The sections aggregate is HTTP 200 even when an INNER section failed —
    a per-section ``status='error'`` must not be pinned for the window."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"sections": [{"status": "ok"}, {"status": "error"}]}
        return {"sections": [{"status": "ok"}, {"status": "ok"}]}

    key = ("sections", "all")
    assert dashboard.dashboard_read_cached(key, compute)["sections"][1]["status"] == "error"
    assert dashboard._READ_CACHE == {}
    assert dashboard.dashboard_read_cached(key, compute)["sections"][1]["status"] == "ok"


@pytest.mark.parametrize("payload", [
    {"status": "ok", "rows": []},
    {"status": "uncovered", "missing_concepts": ["production-line"]},
    {"status": "no-data", "rows": []},
    {"sections": []},
])
def test_honest_stable_outcomes_are_cached(monkeypatch, payload):
    """ok / empty-ok / uncovered / no-data are honest, stable answers — sharing
    them is the whole point (the honest-data rule keeps no-data distinct from an
    error, and it is just as expensive to recompute)."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return payload

    key = ("resolve-by-dimension", "m", "d", None)
    assert dashboard.dashboard_read_cached(key, compute) is payload
    assert dashboard.dashboard_read_cached(key, compute) is payload
    assert calls["n"] == 1


# --- key isolation ----------------------------------------------------------

_DISTINCT_KEYS = [
    ("sections", "all"),
    ("sections", "plant-b"),
    ("sections", "PLANT-B"),          # _matches_plant is NOT case-folded for 'all'
    ("active-issues", "all"),
    ("active-issues", "plant-b"),
    ("line-state", "all"),
    ("line-state-machines", "all"),
    ("resolve-by-dimension", "quality-yield-rate", "production-line", None),
    ("resolve-by-dimension", "quality-yield-rate", "production-line", ""),
    ("resolve-by-dimension", "quality-yield-rate", "production-line", "sys-a"),
    ("resolve-by-dimension", "quality-yield-rate", "production-line", "sys-b"),
    ("resolve-by-dimension", "quality-yield-rate", "machine-state", None),
    ("resolve-by-dimension", "work-order-count", "production-line", None),
    ("resolve-heatmap", "m", "x", "y", None),
    ("resolve-heatmap", "m", "x", "y", ""),
    ("resolve-heatmap", "m", "x", "y", "thr"),
    ("resolve-heatmap", "m", "y", "x", None),
    ("resolve-heatmap", "m2", "x", "y", None),
]


def test_distinct_keys_never_collide(monkeypatch):
    """Every discriminator the current APIs accept partitions the cache:
    endpoint namespace, plant (case-sensitively — ``_matches_plant`` treats a
    differently-cased wildcard as a NON-wildcard), metric, each dimension axis
    and its ORDER, the threshold concept, and the ``system_id`` authority pin
    (``None`` — no pin — stays distinct from the empty string)."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()

    seen = [dashboard.dashboard_read_cached(k, compute)["n"] for k in _DISTINCT_KEYS]
    assert seen == list(range(1, len(_DISTINCT_KEYS) + 1))
    assert calls["n"] == len(_DISTINCT_KEYS)
    # Every key now warm — a repeat sweep recomputes nothing.
    again = [dashboard.dashboard_read_cached(k, compute)["n"] for k in _DISTINCT_KEYS]
    assert again == seen
    assert calls["n"] == len(_DISTINCT_KEYS)


# --- bounded memory --------------------------------------------------------

def test_completed_entries_are_lru_bounded(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()
    limit = dashboard._READ_CACHE_MAX

    for i in range(limit + 40):
        dashboard.dashboard_read_cached(("sections", f"plant-{i}"), compute)

    assert len(dashboard._READ_CACHE) == limit
    # The OLDEST keys were evicted; the newest survive.
    assert ("sections", "plant-0") not in dashboard._READ_CACHE
    assert ("sections", f"plant-{limit + 39}") in dashboard._READ_CACHE


def test_recent_use_keeps_a_key_resident(monkeypatch):
    """LRU, not FIFO: a key that keeps being read is not evicted by a flood of
    one-shot keys (the 'all' plant every viewer asks for must stay warm)."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()
    hot = ("sections", "all")
    dashboard.dashboard_read_cached(hot, compute)

    for i in range(dashboard._READ_CACHE_MAX + 50):
        dashboard.dashboard_read_cached(("sections", f"cold-{i}"), compute)
        dashboard.dashboard_read_cached(hot, compute)      # keep it hot

    assert len(dashboard._READ_CACHE) == dashboard._READ_CACHE_MAX
    assert hot in dashboard._READ_CACHE


def test_flight_registry_is_bounded_by_concurrency_not_key_cardinality(monkeypatch):
    """Compute-coordination memory must not grow with key cardinality (the
    unbounded-lock-dict trap the old line-state cache fell into).

    The registry holds ONE entry per key with a LIVE compute and retires it on
    every outcome, so after any number of completed reads it is EMPTY — a
    strictly tighter bound than the fixed lock array it replaced."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()

    for i in range(500):
        dashboard.dashboard_read_cached(("sections", f"p{i}"), compute)
        assert len(dashboard._READ_FLIGHTS) == 0

    assert dashboard._READ_FLIGHTS == {}
    assert calls["n"] == 500
    # No per-key lock dictionary may reappear anywhere in the module.
    assert not [n for n in vars(dashboard)
                if n.endswith("_LOCKS") and isinstance(getattr(dashboard, n), dict)]


# --- invalidation -----------------------------------------------------------

def test_invalidate_dashboard_read_cache_empties_every_key(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    calls, compute = _counter()
    for key in (("sections", "all"), ("active-issues", "all"), ("line-state", "all")):
        dashboard.dashboard_read_cached(key, compute)
    assert dashboard._READ_CACHE

    dashboard.invalidate_dashboard_read_cache()

    assert dashboard._READ_CACHE == {}
    assert dashboard.dashboard_read_cached(("sections", "all"), compute)["n"] == 4


def test_invalidate_dashboard_caches_clears_cards_and_reads(monkeypatch):
    """The ONE invalidator the production paths call — a provisioning change or
    a mapping correction changes what BOTH the cards and the panel reads would
    resolve to, so both drop together."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    monkeypatch.setattr(dashboard, "_build_cards", lambda: [{"card_id": "oee"}])
    dashboard.refresh_cards_cache()
    calls, compute = _counter()
    dashboard.dashboard_read_cached(("sections", "all"), compute)
    assert dashboard._CARDS_CACHE["cards"] is not None and dashboard._READ_CACHE

    dashboard.invalidate_dashboard_caches()

    assert dashboard._CARDS_CACHE["cards"] is None
    assert dashboard._READ_CACHE == {}


def test_invalidate_line_state_cache_stays_a_working_test_seam(monkeypatch):
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "9999")
    calls, compute = _counter()
    dashboard.line_state_cached(("line-state", "all"), compute)

    dashboard.invalidate_line_state_cache()

    assert dashboard.line_state_cached(("line-state", "all"), compute)["n"] == 2


# --- invalidation vs an IN-FLIGHT pre-invalidation compute -------------------
# Clearing the entry dict alone is NOT enough: a caller that started computing
# BEFORE the invalidation still holds a pre-invalidation answer, and storing it
# afterwards resurrects exactly the state the steward just corrected — a
# pre-correction authority/mapping answer pinned in front of every viewer for a
# whole TTL window. Admission is therefore gated on a cache GENERATION captured
# before the compute starts.

def _blocking_compute(release: threading.Event, value):
    """A compute that blocks on ``release`` before returning ``value``."""
    calls = {"n": 0}

    def compute(*_args):
        calls["n"] += 1
        release.wait(timeout=5)
        return value

    return calls, compute


def test_invalidation_during_a_cold_compute_is_not_resurrected(monkeypatch):
    """Start a compute, invalidate while it is blocked, then release it: the
    caller still receives its own value (errors/results propagate — never
    swallowed), but the cache stays EMPTY so the next read recomputes."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    release = threading.Event()
    calls, compute = _blocking_compute(release, {"status": "ok", "version": "old"})
    got: list = []

    def caller():
        got.append(dashboard.dashboard_read_cached(("sections", "plant-a"), compute))

    t = threading.Thread(target=caller, daemon=True)
    t.start()
    # Wait until the compute is actually in flight, then invalidate underneath it.
    for _ in range(500):
        if calls["n"] == 1:
            break
        time.sleep(0.002)
    assert calls["n"] == 1, "compute never started"
    dashboard.invalidate_dashboard_read_cache()
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()

    # The in-flight caller is served its own honest (if now-stale) answer …
    assert got == [{"status": "ok", "version": "old"}]
    # … but nothing was admitted, so the correction is not undone.
    assert dashboard._READ_CACHE == {}

    fresh_calls, fresh = _blocking_compute(release, {"status": "ok", "version": "new"})
    assert dashboard.dashboard_read_cached(("sections", "plant-a"), fresh) == {
        "status": "ok", "version": "new"}
    assert fresh_calls["n"] == 1, "post-invalidation read did not recompute"


def test_invalidation_only_blocks_computes_that_started_before_it(monkeypatch):
    """The generation gate must not disable caching outright — a compute started
    AFTER the invalidation is admitted normally."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    dashboard.invalidate_dashboard_read_cache()
    calls, compute = _counter()

    assert dashboard.dashboard_read_cached(("sections", "all"), compute)["n"] == 1
    assert dashboard.dashboard_read_cached(("sections", "all"), compute)["n"] == 1
    assert calls["n"] == 1


def test_generation_gate_does_not_drop_a_concurrent_untouched_key(monkeypatch):
    """Two cold keys in flight, invalidate once: BOTH are pre-invalidation reads,
    so neither is admitted. Invalidation is global by design (a provisioning or
    mapping change can change any panel), and dropping too much is safe —
    resurrecting a corrected answer is not."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    release = threading.Event()
    calls_a, compute_a = _blocking_compute(release, {"status": "ok", "k": "a"})
    calls_b, compute_b = _blocking_compute(release, {"status": "ok", "k": "b"})
    threads = [
        threading.Thread(
            target=lambda: dashboard.dashboard_read_cached(("sections", "a"), compute_a),
            daemon=True),
        threading.Thread(
            target=lambda: dashboard.dashboard_read_cached(("sections", "b"), compute_b),
            daemon=True),
    ]
    for t in threads:
        t.start()
    for _ in range(500):
        if calls_a["n"] == 1 and calls_b["n"] == 1:
            break
        time.sleep(0.002)
    assert (calls_a["n"], calls_b["n"]) == (1, 1)

    dashboard.invalidate_dashboard_read_cache()
    release.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert dashboard._READ_CACHE == {}


def test_invalidate_dashboard_caches_also_gates_in_flight_reads(monkeypatch):
    """The production invalidator (``invalidate_dashboard_caches``) — the one
    ``correct_mapping_route`` and ``onboard._dp_result`` call — carries the same
    generation bump, not just the cards drop."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    release = threading.Event()
    calls, compute = _blocking_compute(release, {"status": "ok", "version": "pre"})
    t = threading.Thread(
        target=lambda: dashboard.dashboard_read_cached(
            ("active-issues", "all"), compute),
        daemon=True)
    t.start()
    for _ in range(500):
        if calls["n"] == 1:
            break
        time.sleep(0.002)
    assert calls["n"] == 1

    dashboard.invalidate_dashboard_caches()
    release.set()
    t.join(timeout=5)

    assert dashboard._READ_CACHE == {}


# --- the honest fan-out status rule -----------------------------------------
# Shared by the Line State card and its machines drill: a fail-soft per-system
# fan-out must not report a source FAULT as empty success, because that is what
# the cache would then share (see test_dashboard_line_state /
# test_dashboard_drill_machines for the route-level proofs).

@pytest.mark.parametrize(
    "resolved,failed,expected_status",
    [
        (3, 0, "ok"),            # clean sweep
        (0, 0, "ok"),            # honest empty: every system is a coverage gap
        (2, 1, "partial-error"),  # some data, one source broke
        (0, 1, "error"),          # nothing usable AND something broke
        (0, 4, "error"),
    ],
)
def test_fanout_status_maps_resolved_and_failed(resolved, failed, expected_status):
    status, error = dashboard.fanout_status(resolved, failed)
    assert status == expected_status
    assert (error is None) == (expected_status == "ok")


def test_fanout_status_ok_results_are_cacheable_and_faults_are_not():
    """The rule and the cache-admission rule agree — a fault status is exactly
    what ``_cacheable_payload`` refuses."""
    for resolved, failed in ((3, 0), (0, 0)):
        status, error = dashboard.fanout_status(resolved, failed)
        assert dashboard._cacheable_payload({"status": status, "error": error})
    for resolved, failed in ((2, 1), (0, 1)):
        status, error = dashboard.fanout_status(resolved, failed)
        assert not dashboard._cacheable_payload({"status": status, "error": error})


def test_fanout_status_error_message_names_the_failure_count():
    _, one = dashboard.fanout_status(0, 1)
    _, many = dashboard.fanout_status(0, 3)
    assert "1 source system " in one and "systems" not in one
    assert "3 source systems" in many
