# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reopened chat names the ids in its telemetry lines as the live trace does.

The live trace step names a call's stored coverage lines and sample line with
``id_name_resolver.annotate_ids_in_lines`` (``chat_client._trace_telemetry``).
The Recent sessions reload (``memory_turns._named_trace_lines``) first looks up
the session's ids together, then names each call's lines with the same helper,
call by call, so a line reads exactly as the live step showed it. The names
are display-only: every lookup is one short attempt, one at a time in the
process, a failed lookup pauses further lookups, and a reload starts no lookup
after its budget, so a slow or failing graph leaves ids as stored.

Each test says whether it reproduces a defect (and the commit it fails on) or
guards. The graph is faked at ``agents.db.neptune_query`` (the seam the
resolver imports at call time), so the real resolver, its 50-id batch and its
cache run.
"""

from __future__ import annotations

import uuid

import pytest

from agents import db, id_name_resolver, memory_turns


def _ids(n: int) -> list[str]:
    return [str(uuid.UUID(int=i + 1)) for i in range(n)]


@pytest.fixture
def graph(monkeypatch):
    """A fake graph that names every id ``N-<first 4 chars>`` (or as set in
    ``graph.names``) and records each query's ids and the limits in force."""
    class _Graph:
        names: dict = {}
        calls: list = []
        limits: list = []
        on_query = None

    g = _Graph()
    g.names, g.calls, g.limits = {}, [], []

    def query(cypher, parameters=None):
        ids = list((parameters or {}).get("ids") or [])
        g.calls.append(ids)
        g.limits.append((db._QUERY_TIMEOUT_MS.get(), db._SINGLE_ATTEMPT_CONNECT_S.get()))
        if g.on_query:
            g.on_query()
        return [{"id": i, "name": g.names.get(i, f"N-{i[:4]}")} for i in ids]

    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(id_name_resolver, "_PAUSED_UNTIL", 0.0, raising=False)
    monkeypatch.setattr(db, "neptune_query", query)
    return g


def _live(line: str) -> str:
    """One line as the live trace names it, with a cold cache."""
    saved = dict(id_name_resolver._CACHE)
    id_name_resolver._CACHE.clear()
    try:
        return id_name_resolver.annotate_ids_in_text(line)
    finally:
        id_name_resolver._CACHE.clear()
        id_name_resolver._CACHE.update(saved)


def test_more_than_one_batch_of_ids_is_named_on_every_line(graph):
    """Reproduction (fails on 98d9b208): 51 lines with one
    distinct id each. The live trace names each line on its own, so all 51
    are named; the reload resolved only the first 50 ids and left line 51
    raw."""
    ids = _ids(51)
    found = {f"t{n}": ([f"Telemetry read: {i} / a, 24 points"], "") for n, i in enumerate(ids)}
    named = memory_turns._named_trace_lines(found)
    for n, i in enumerate(ids):
        line = found[f"t{n}"][0][0]
        assert named[f"t{n}"][0] == [_live(line)]
        assert i not in named[f"t{n}"][0][0]


def test_a_name_holding_another_lines_id_is_not_renamed(graph):
    """Reproduction (fails on 98d9b208): the graph's
    name for id A holds id B, which another line carries. The live trace
    annotates line A with A's name only; the reload applied the whole
    session's map to every line, so B inside A's inserted name was renamed
    too."""
    a, b = _ids(2)
    graph.names = {a: f"Rig {b}", b: "Spindle"}
    found = {"t1": ([f"Telemetry read: {a} / x"], ""), "t2": ([f"Telemetry read: {b} / y"], "")}
    named = memory_turns._named_trace_lines(found)
    assert named["t1"][0] == [_live(found["t1"][0][0])]
    assert named["t2"][0] == [_live(found["t2"][0][0])]


def test_the_lookup_runs_as_one_short_attempt(graph):
    """Reproduction (fails on 98d9b208): the reload's optional name lookup ran
    under the default graph limits (a 60 s server limit and resubmits), so a
    slow graph held the reopened chat. Each lookup now runs under
    ``single_attempt_graph_reads`` with the resolver's short limits."""
    [i] = _ids(1)
    memory_turns._named_trace_lines({"t1": ([f"Telemetry read: {i} / a"], "")})
    assert graph.limits == [(id_name_resolver.LOOKUP_TIMEOUT_MS,
                             id_name_resolver.LOOKUP_CONNECT_TIMEOUT_S)]


def test_no_new_lookup_starts_after_the_reload_budget(graph, monkeypatch):
    """Pin of the reload's bound: once its lookups have taken longer than
    ``memory_turns.RELOAD_NAME_BUDGET_S``, no further lookup is sent, and the
    lines whose ids were not looked up keep their stored text."""
    clock = _FakeClock(monkeypatch, per_query=4.0)
    monkeypatch.setattr(memory_turns, "RELOAD_NAME_BUDGET_S", 6.0)
    graph.on_query = clock.tick
    ids = _ids(150)
    found = {f"t{n}": ([f"Telemetry read: {i} / a"], "") for n, i in enumerate(ids)}
    named = memory_turns._named_trace_lines(found)
    assert len(graph.calls) == 2
    assert all(i not in named[f"t{n}"][0][0] for n, i in enumerate(ids[:100]))
    assert all(named[f"t{n}"] == found[f"t{n}"] for n in range(100, 150))


class _FakeClock:
    """The resolver's clock, advanced ``per_query`` seconds by each lookup."""

    def __init__(self, monkeypatch, per_query: float):
        self.t, self.per_query = 1000.0, per_query
        monkeypatch.setattr(id_name_resolver, "_now", lambda: self.t)

    def tick(self):
        self.t += self.per_query


def test_slow_lookups_name_a_session_as_its_live_steps_did(graph, monkeypatch):
    """Reproduction (fails on c7a87968): three calls of 50 ids each, each
    lookup taking 2 s. Each live step named its own 50 ids; the reload named
    the session's 150 ids under one 3 s budget, so it started two lookups and
    left the third call's ids bare. The live and reload naming no longer
    depends on elapsed time inside one set of lines, and the reload budget is
    the whole reload's, so all three calls read as they did live."""
    from agents import chat_client, investigation_trace_store
    clock = _FakeClock(monkeypatch, per_query=2.0)
    graph.on_query = clock.tick
    calls = {f"t{n}": [f"Telemetry read: {i} / a" for i in ids]
             for n, ids in enumerate((_ids(150)[0:50], _ids(150)[50:100], _ids(150)[100:150]))}
    live = {}
    for tool_id, coverage in calls.items():
        monkeypatch.setattr(investigation_trace_store, "get_trace_telemetry",
                            lambda session_id, ref, c=coverage: (c, ""))
        live[tool_id] = chat_client._trace_telemetry("s1", tool_id, "sys-k___query_sys_k", "ok")[1]
    id_name_resolver._CACHE.clear()
    graph.calls.clear()
    reload = memory_turns._named_trace_lines({t: (c, "") for t, c in calls.items()})
    assert {t: reload[t][0] for t in calls} == live
    assert len(graph.calls) == 3


def test_concurrent_renders_at_the_start_of_an_outage_send_one_lookup(monkeypatch):
    """Reproduction (fails on c7a87968): eight renders that start together
    when the graph fails each read the pause as expired and each sent a
    lookup. Lookups now take the process's one lookup slot, and the renders
    that waited for the failed one see its pause and send none."""
    import threading
    import time
    calls = []
    start = threading.Barrier(8)

    def broken(cypher, parameters=None):
        calls.append(1)
        time.sleep(0.2)
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(id_name_resolver, "_PAUSED_UNTIL", 0.0)
    monkeypatch.setattr(db, "neptune_query", broken)
    ids = _ids(8)

    def render(n):
        start.wait()
        id_name_resolver.annotate_ids_in_lines([f"Telemetry read: {ids[n]} / a"])

    threads = [threading.Thread(target=render, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(calls) == 1


def test_guard_a_failing_graph_leaves_the_lines_as_stored(monkeypatch):
    """Guard (passes on 98d9b208): a graph that raises leaves every line as
    stored and the reload goes on."""
    def broken(cypher, parameters=None):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(db, "neptune_query", broken)
    [i] = _ids(1)
    found = {"t1": ([f"Telemetry read: {i} / a"], f"[engine] Telemetry samples, not every value: {i} / a")}
    assert memory_turns._named_trace_lines(found) == found


def test_a_reload_during_an_outage_names_the_lines_once_the_graph_recovers(graph, monkeypatch):
    """Reproduction (fails on a1a56b9b): a reload while
    the graph failed cached every id it asked for as a confirmed miss, so a
    later reload (and the live trace) showed the bare ids after the graph
    recovered."""
    healthy = db.neptune_query

    def broken(cypher, parameters=None):
        raise RuntimeError("graph unreachable")

    [i] = _ids(1)
    found = {"t1": ([f"Telemetry read: {i} / a"], "")}
    monkeypatch.setattr(db, "neptune_query", broken)
    assert memory_turns._named_trace_lines(found) == found
    monkeypatch.setattr(id_name_resolver, "_PAUSED_UNTIL", 0.0)  # the pause has passed
    monkeypatch.setattr(db, "neptune_query", healthy)
    assert memory_turns._named_trace_lines(found)["t1"][0] == [_live(found["t1"][0][0])]
    assert i not in memory_turns._named_trace_lines(found)["t1"][0][0]


def test_a_live_trace_step_during_an_outage_sends_one_bounded_lookup(monkeypatch):
    """Reproduction (fails on 21bc2971, the live path): a
    telemetry call's live trace step named its 16 coverage lines and its
    sample line one at a time, so during an outage each asked the graph
    again under the default limits (17 lookups). The step now names all its
    lines in one lookup, sent as one short attempt, and after a failure the
    resolver sends none until its pause ends."""
    from agents import chat_client, investigation_trace_store
    calls = []

    def broken(cypher, parameters=None):
        calls.append((db._QUERY_TIMEOUT_MS.get(), db._SINGLE_ATTEMPT_CONNECT_S.get()))
        raise RuntimeError("graph unreachable")

    ids = _ids(17)
    coverage = [f"Telemetry read: {i} / axis, 24 points" for i in ids[:16]]
    sample = f"[engine] Telemetry samples, not every value: {ids[16]} / axis: 24 of 720"
    monkeypatch.setattr(investigation_trace_store, "get_trace_telemetry",
                        lambda session_id, ref: (coverage, sample))
    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(id_name_resolver, "_PAUSED_UNTIL", 0.0, raising=False)
    monkeypatch.setattr(db, "neptune_query", broken)
    for tool_id in ("t-1", "t-2"):
        _, shown, line = chat_client._trace_telemetry("s1", tool_id, "sys-k___query_sys_k", "ok")
        assert shown == coverage and line == sample
    assert calls == [(id_name_resolver.LOOKUP_TIMEOUT_MS, id_name_resolver.LOOKUP_CONNECT_TIMEOUT_S)]


def test_the_live_step_and_the_reload_name_the_same_lines_alike(graph, monkeypatch):
    """Guard of parity: the live trace step and the reload name a call's lines
    with the same helper (``id_name_resolver.annotate_ids_in_lines``), so a
    reopened chat shows exactly what the live chat showed."""
    from agents import chat_client, investigation_trace_store
    a, b = _ids(2)
    graph.names = {a: f"Rig {b}", b: "Spindle"}
    coverage = [f"Telemetry read: {a} / {b}, 24 points", f"Telemetry read: {b} / x"]
    sample = f"[engine] Telemetry samples, not every value: {a} / {b}: 24 of 720"
    monkeypatch.setattr(investigation_trace_store, "get_trace_telemetry",
                        lambda session_id, ref: (coverage, sample))
    _, live_coverage, live_sample = chat_client._trace_telemetry("s1", "t-1", "sys-k___query_sys_k", "ok")
    id_name_resolver._CACHE.clear()
    reload = memory_turns._named_trace_lines({"t-1": (coverage, sample)})["t-1"]
    assert (live_coverage, live_sample) == (reload[0], reload[1])
    assert a not in live_sample and b not in live_coverage[1]
