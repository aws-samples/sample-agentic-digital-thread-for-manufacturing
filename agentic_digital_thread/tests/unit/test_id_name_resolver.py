# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic id → human-name resolution for rendered output (offline).

Proves ``agents.id_name_resolver``:
  * extracts UUID-shaped identifier tokens from arbitrary text (pure);
  * resolves ids → names via an INJECTED graph seam (no AWS), discarding
    empty/self names, with a process-local cache;
  * annotates a known id as ``Name (idprefix…)`` and leaves an UNKNOWN id
    EXACTLY unchanged (fail-soft);
  * the one-call ``annotate_ids_in_text`` end-to-end + fail-soft on a raising seam.

No AWS / Neptune / network — the graph read is passed in as a plain function.
GENERIC — synthetic ids/names only; no dataset literals.
"""
from __future__ import annotations

import agents.id_name_resolver as idr

_ID1 = "a380d7e4-1111-2222-3333-444455556666"
_ID2 = "b491e8f5-aaaa-bbbb-cccc-ddddeeeeffff"
_ID_UNKNOWN = "deadbeef-0000-0000-0000-000000000000"


def _fake_query(names_by_id):
    """A graph-read stub: returns rows for the ids it knows about."""
    def _q(cypher, params=None):
        ids = set((params or {}).get("ids", []))
        return [{"id": i, "name": n} for i, n in names_by_id.items() if i in ids]
    return _q


def setup_function(_):
    idr._CACHE.clear()
    idr._PAUSED_UNTIL = 0.0


# --------------------------------------------------------------------------- #
# extract_id_tokens (pure)
# --------------------------------------------------------------------------- #
def test_extract_finds_uuid_tokens():
    text = f"Asset {_ID1} had a stop; see event {_ID2}."
    assert idr.extract_id_tokens(text) == [_ID1, _ID2]


def test_extract_dedups_and_preserves_order():
    text = f"{_ID2} {_ID1} {_ID2}"
    assert idr.extract_id_tokens(text) == [_ID2, _ID1]


def test_extract_empty_or_no_ids():
    assert idr.extract_id_tokens("") == []
    assert idr.extract_id_tokens("no ids here, just 12345") == []
    assert idr.extract_id_tokens(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# resolve_id_names (injected graph seam)
# --------------------------------------------------------------------------- #
def test_resolve_returns_known_names_only():
    q = _fake_query({_ID1: "Diamant1HC1"})
    out = idr.resolve_id_names([_ID1, _ID_UNKNOWN], query=q)
    assert out == {_ID1: "Diamant1HC1"}


def test_resolve_discards_name_equal_to_id_or_empty():
    q = _fake_query({_ID1: _ID1, _ID2: "  "})
    assert idr.resolve_id_names([_ID1, _ID2], query=q) == {}


def test_resolve_caches_hits_and_misses():
    calls = {"n": 0}

    def _q(cypher, params=None):
        calls["n"] += 1
        return [{"id": _ID1, "name": "Press-01"}]

    assert idr.resolve_id_names([_ID1, _ID_UNKNOWN], query=_q) == {_ID1: "Press-01"}
    # Second call for the SAME ids must not hit the graph again (hit + miss cached).
    assert idr.resolve_id_names([_ID1, _ID_UNKNOWN], query=_q) == {_ID1: "Press-01"}
    assert calls["n"] == 1


def test_resolve_fail_soft_on_raising_query():
    def _boom(cypher, params=None):
        raise RuntimeError("graph down")
    assert idr.resolve_id_names([_ID1], query=_boom) == {}


def test_a_failed_lookup_is_not_cached_as_a_miss():
    """Reproduction (fails on a1a56b9b): a lookup that raised was cached as a
    confirmed miss, so once the graph recovered the id stayed unnamed until
    the process cache cleared. A failed lookup confirms nothing; the next
    render asks again."""
    def _boom(cypher, params=None):
        raise RuntimeError("graph down")

    assert idr.resolve_id_names([_ID1], query=_boom) == {}
    idr._PAUSED_UNTIL = 0.0  # the pause after a failure has passed
    assert idr.resolve_id_names([_ID1], query=_fake_query({_ID1: "Press-01"})) == {_ID1: "Press-01"}


def test_a_degraded_graph_read_is_not_cached_as_a_miss(monkeypatch):
    """Reproduction (fails on a1a56b9b): the default seam's graph read
    degrades a failure to no rows (``db.neptune_query``), which the resolver
    took as an authoritative empty answer and cached. A read the graph layer
    records as incomplete (``db.recording_incomplete_reads``) is now not
    cached, and the failure still reaches an outer recording scope."""
    from agents import db

    def _degraded(cypher, parameters=None):
        db.note_incomplete_read("QUERY_TIMEOUT")
        return []

    monkeypatch.setattr(db, "neptune_query", _degraded)
    with db.recording_incomplete_reads() as outer:
        assert idr.resolve_id_names([_ID1]) == {}
    assert outer == ["QUERY_TIMEOUT"]
    idr._PAUSED_UNTIL = 0.0  # the pause after a failure has passed
    monkeypatch.setattr(db, "neptune_query", _fake_query({_ID1: "Press-01"}))
    assert idr.resolve_id_names([_ID1]) == {_ID1: "Press-01"}


def test_guard_an_authoritative_empty_answer_is_still_cached():
    """Guard (passes on a1a56b9b): a lookup that answered without the id is a
    confirmed miss and is not asked again."""
    calls = {"n": 0}

    def _q(cypher, params=None):
        calls["n"] += 1
        return []

    assert idr.resolve_id_names([_ID_UNKNOWN], query=_q) == {}
    assert idr.resolve_id_names([_ID_UNKNOWN], query=_q) == {}
    assert calls["n"] == 1


def test_after_a_failed_lookup_none_is_sent_until_the_pause_ends(monkeypatch):
    """Reproduction (fails on 21bc2971): with failed lookups no longer cached,
    every render during an outage asked the graph again, once per id-bearing
    text. After a failed lookup the resolver now sends none for
    ``FAILURE_PAUSE_S``; once that has passed it asks again."""
    calls = {"n": 0}

    def _boom(cypher, params=None):
        calls["n"] += 1
        raise RuntimeError("graph down")

    now = {"t": 1000.0}
    monkeypatch.setattr(idr, "_now", lambda: now["t"])
    assert idr.resolve_id_names([_ID1], query=_boom) == {}
    assert idr.resolve_id_names([_ID2], query=_boom) == {}
    assert calls["n"] == 1
    now["t"] += idr.FAILURE_PAUSE_S + 1
    assert idr.resolve_id_names([_ID1], query=_fake_query({_ID1: "Press-01"})) == {_ID1: "Press-01"}


def test_every_lookup_is_one_short_attempt():
    """Reproduction (fails on 21bc2971): an optional display lookup ran under
    the default graph limits (60 s server limit, resubmits). Every lookup now
    runs under ``db.single_attempt_graph_reads`` with the resolver's short
    limits."""
    from agents import db
    seen = []

    def _q(cypher, params=None):
        seen.append((db._QUERY_TIMEOUT_MS.get(), db._SINGLE_ATTEMPT_CONNECT_S.get()))
        return []

    idr.resolve_id_names([_ID1], query=_q)
    assert seen == [(idr.LOOKUP_TIMEOUT_MS, idr.LOOKUP_CONNECT_TIMEOUT_S)]


def test_lines_are_named_each_with_its_own_ids_in_one_lookup():
    """Reproduction (fails on 21bc2971, which has no line helper): the live
    trace and the reload name a call's telemetry lines with one helper, one
    lookup for all their ids, and each line with the names of its own ids
    only, so a name holding another line's id is shown as the graph gave it."""
    calls = {"n": 0}
    names = {_ID1: f"Rig {_ID2}", _ID2: "Spindle"}

    def _q(cypher, params=None):
        calls["n"] += 1
        ids = set((params or {}).get("ids", []))
        return [{"id": i, "name": n} for i, n in names.items() if i in ids]

    lines = [f"read {_ID1} / a", f"read {_ID2} / b", "no id here"]
    assert idr.annotate_ids_in_lines(lines, query=_q) == [
        f"read Rig {_ID2} (a380d7e4\u2026) / a", "read Spindle (b491e8f5\u2026) / b", "no id here"]
    assert calls["n"] == 1


def test_resolve_empty_input():
    assert idr.resolve_id_names([], query=_fake_query({})) == {}
    assert idr.resolve_id_names(None, query=_fake_query({})) == {}


# --------------------------------------------------------------------------- #
# annotate_text (pure) — known id annotated; unknown id unchanged
# --------------------------------------------------------------------------- #
def test_annotate_known_id_gets_name_and_prefix():
    text = f"Top stop on asset {_ID1}."
    out = idr.annotate_text(text, {_ID1: "Diamant1HC1"})
    assert out == "Top stop on asset Diamant1HC1 (a380d7e4…)."


def test_annotate_unknown_id_left_unchanged():
    text = f"Asset {_ID_UNKNOWN} unresolved."
    assert idr.annotate_text(text, {_ID1: "Diamant1HC1"}) == text


def test_annotate_is_idempotent():
    text = f"Asset {_ID1}."
    once = idr.annotate_text(text, {_ID1: "Diamant1HC1"})
    twice = idr.annotate_text(once, {_ID1: "Diamant1HC1"})
    assert once == twice  # id token no longer present after first pass


def test_annotate_skips_empty_or_self_name():
    text = f"Asset {_ID1}."
    assert idr.annotate_text(text, {_ID1: ""}) == text
    assert idr.annotate_text(text, {_ID1: _ID1}) == text


# --------------------------------------------------------------------------- #
# annotate_ids_in_text (end-to-end, injected seam)
# --------------------------------------------------------------------------- #
def test_end_to_end_known_id_annotated():
    q = _fake_query({_ID1: "Diamant1HC1"})
    out = idr.annotate_ids_in_text(f"3 records: {_ID1}", query=q)
    assert out == "3 records: Diamant1HC1 (a380d7e4…)"


def test_end_to_end_unknown_id_unchanged():
    q = _fake_query({})  # graph knows nothing
    text = f"3 records: {_ID_UNKNOWN}"
    assert idr.annotate_ids_in_text(text, query=q) == text


def test_end_to_end_no_ids_makes_no_graph_call():
    calls = {"n": 0}

    def _q(cypher, params=None):
        calls["n"] += 1
        return []

    text = "12 records found: OPEN, CLOSED"
    assert idr.annotate_ids_in_text(text, query=_q) == text
    assert calls["n"] == 0  # no id tokens → no lookup


def test_end_to_end_fail_soft_on_raising_seam():
    def _boom(cypher, params=None):
        raise RuntimeError("graph down")
    text = f"asset {_ID1}"
    assert idr.annotate_ids_in_text(text, query=_boom) == text
