# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A set and a clear of the SAME table at once never leave a stored fact
outside the manifest.

Before this change a set paused after listing its table (pending, token S)
and before writing its fact could be overtaken by a whole clear: the clear
re-listed the table with its own token, cleared the fact and removed the
entry. The set then wrote its fact anyway, and settling an entry it no longer
held was a silent no-op. Both calls answered success, and the manifest was
empty while the fact was stored: an ordinary query ran unfiltered, and
neither a rebuild nor a restart healed it, because the empty manifest is
authoritative.

Now each fact write is conditional, in the same graph statement, on the
System's manifest being the one in which the caller holds that table's
token, so a writer whose token was taken writes no fact. Settling or removing
an entry that no longer holds the caller's token raises
(``row_exclusion_manifest.TokenLost``) instead of answering success, and the
steward route answers 409. A change to ANOTHER table's entry between the two
writes is not a lost token: the write reads the manifest again and lands.

Deterministic: the other steward's call runs to completion inside a
``RxGraph.before_write`` hook at the named point. Synthetic names.
"""
from __future__ import annotations

import pytest

from agents import admin_routes
from agents import connection_resolver as cr
from agents import row_exclusion as rx
from agents import row_exclusion_manifest as manifest
from tests.unit._row_exclusion_graph import RxGraph

_SYS = "sys-a"
_JOB = {"pipeline": "varchar", "run_ref": "varchar", "state": "varchar"}
_LEDGER = {"batch_tag": "varchar", "amount": "double"}
_JOB_RULE = [{"column": "pipeline", "op": "not_empty"}, {"column": "run_ref", "op": "not_empty"}]
_LEDGER_RULE = [{"column": "batch_tag", "op": "empty"}]


@pytest.fixture
def g(monkeypatch):
    rx.reset()
    graph = RxGraph({_SYS: {"job_events": _JOB, "ledger_lines": _LEDGER}})
    monkeypatch.setattr(rx, "_graph_query", graph.seam())
    monkeypatch.setattr(cr, "audit_connection_event", lambda *_a, **_k: None)
    yield graph
    rx.reset()


def _set(g, table="job_events", conditions=_JOB_RULE):
    return rx.set_row_exclusion(_SYS, table, conditions, label="shadow copies", actor="dana",
                                source="owner note", write=g.write, query=g.query)


def _clear(g, table="job_events"):
    return rx.clear_row_exclusion(_SYS, table, actor="dana", write=g.write, query=g.query)


def _pending(g, table="job_events") -> bool:
    entry = g.manifest(_SYS).get(table)
    return bool(entry and entry["pending"])


#: The points one call is paused at, each just before one of its writes.
_POINTS = {
    "before_fact": lambda g, kind: kind == "set_fact",
    "before_settle_or_remove": lambda g, kind: kind == "swap" and _pending(g),
}


def _overtake(g, point: str, other):
    """Run ``other`` to completion once, at ``point`` of the next call."""
    seen = {}

    def hook(kind, _params):
        if _POINTS[point](g, kind):
            g.before_write = None
            seen["other"] = other()
    g.before_write = hook
    return seen


def _consistent(g, table="job_events") -> None:
    """The manifest lists the table settled exactly when a fact is stored,
    and a system agent reading it agrees."""
    listed = g.manifest(_SYS).get(table)
    stored = g.stored(_SYS, table)
    assert (listed is not None) == (stored is not None)
    assert listed is None or listed["pending"] is False
    rx.remember_from_graph(_SYS)
    assert rx.unavailable_for(_SYS, f"SELECT COUNT(*) FROM {table}") is None
    assert (rx.exclusion_for(_SYS, table) is not None) == (stored is not None)


# --------------------------------------------------------------------------- #
# The same table, both orders, at both points.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("point", sorted(_POINTS))
def test_a_set_overtaken_by_a_clear_reports_the_lost_token_and_leaves_nothing(g, point):
    seen = _overtake(g, point, lambda: _clear(g))
    with pytest.raises(manifest.TokenLost):
        _set(g)
    assert seen["other"]["status"] == "ok"
    assert g.manifest(_SYS) == {} and g.stored(_SYS, "job_events") is None
    _consistent(g)


@pytest.mark.parametrize("point", sorted(_POINTS))
def test_a_clear_overtaken_by_a_set_reports_the_lost_token_and_keeps_the_set(g, point):
    _set(g, conditions=[{"column": "state", "op": "empty"}])        # an earlier rule
    seen = _overtake(g, point, lambda: _set(g))
    with pytest.raises(manifest.TokenLost):
        _clear(g)
    assert seen["other"]["status"] == "ok"
    assert g.manifest(_SYS)["job_events"]["pending"] is False
    assert g.stored(_SYS, "job_events")["conditions"] == _JOB_RULE
    _consistent(g)


def test_a_set_overtaken_by_another_set_keeps_the_later_one(g):
    later = [{"column": "state", "op": "empty"}]
    _overtake(g, "before_fact", lambda: _set(g, conditions=later))
    with pytest.raises(manifest.TokenLost):
        _set(g)
    assert g.stored(_SYS, "job_events")["conditions"] == later
    _consistent(g)


def test_a_lost_token_writes_no_fact(g):
    """The overtaken set's fact write is sent, conditional on its manifest,
    and matches nothing: no fact lands after the clear."""
    _overtake(g, "before_fact", lambda: _clear(g))
    with pytest.raises(manifest.TokenLost):
        _set(g)
    fact_writes = [p for kind, p in g.calls if kind == "set_fact" and p["row_exclusion"]]
    assert len(fact_writes) == 1 and fact_writes[0]["expected"]
    assert g.stored(_SYS, "job_events") is None


# --------------------------------------------------------------------------- #
# Another table between the two writes: not a lost token.
# --------------------------------------------------------------------------- #
def test_another_tables_write_between_the_two_writes_is_retried_and_lands(g):
    seen = _overtake(g, "before_fact", lambda: _set(g, "ledger_lines", _LEDGER_RULE))
    assert _set(g)["status"] == "ok"
    assert seen["other"]["status"] == "ok"
    listed = g.manifest(_SYS)
    assert set(listed) == {"job_events", "ledger_lines"}
    assert not any(e["pending"] for e in listed.values())
    assert g.stored(_SYS, "job_events")["conditions"] == _JOB_RULE
    assert g.stored(_SYS, "ledger_lines")["conditions"] == _LEDGER_RULE
    for table in ("job_events", "ledger_lines"):
        _consistent(g, table)


# --------------------------------------------------------------------------- #
# The manifest changes and the route.
# --------------------------------------------------------------------------- #
def test_settling_or_removing_an_entry_without_the_callers_token_raises():
    other = {"job_events": manifest.Entry("theirs", True)}
    for change in (manifest.settling("job_events", "mine"),
                   manifest.removing("job_events", "mine")):
        with pytest.raises(manifest.TokenLost):
            change(dict(other))
        with pytest.raises(manifest.TokenLost):
            change({})
    assert manifest.settling("job_events", "theirs")(dict(other)) == {
        "job_events": manifest.Entry("theirs", False)}
    assert manifest.removing("job_events", "theirs")(dict(other)) == {}


@pytest.mark.parametrize("first", ["set", "clear"])
def test_the_route_answers_409_for_a_lost_token(g, first):
    body = {"table": "job_events", "label": "shadow copies", "conditions": _JOB_RULE,
            "source": "owner note"}
    if first == "set":
        _overtake(g, "before_fact", lambda: _clear(g))
    else:
        _set(g)
        body = {"table": "job_events", "conditions": []}
        _overtake(g, "before_fact", lambda: _set(g, conditions=[{"column": "state",
                                                                   "op": "empty"}]))
    response = admin_routes.set_row_exclusion_sync(_SYS, body, "dana",
                                                   connection_type="Athena",
                                                   seams=(g.write, g.query))
    assert getattr(response, "status_code", 200) == 409
    assert b"concurrent_change" in response.body
    _consistent(g)
