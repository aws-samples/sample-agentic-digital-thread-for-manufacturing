# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A re-provision's carry of the row exclusions survives a crash and a steward.

Before this change a re-provision read the listed tables' facts into process
memory, deleted the SchemaEntries (the facts with them) and wrote the facts
back once the replacement schema existed. Two things went wrong:

* a crash after the replacement lost the carried facts. A table whose
  manifest entry was pending (an interrupted set that had already stored its
  fact) then read as absent, a matched row with no stored value, and its
  reads ran unfiltered;
* a steward's set that landed after that read and before the delete
  answered success, and the delete removed it.

Now the rewrite is a durable barrier on the System node. Before the delete,
ONE compare-and-set of the manifest writes a rewrite marker that holds the
carried facts (every listed table, settled and pending, with provenance).
While the marker exists:

* a system agent holds every data read of a listed table (structural
  discovery runs, and a table the manifest does not list answers);
* the steward route answers 409 ``rewrite_in_progress`` and changes nothing.

The restore reads the stored copy from the graph (not the provision's
memory), writes each fact back and checks it against the new columns, then
re-tokens every listed table and removes the marker in its last write. A
re-provision that finds a marker left by a crashed run carries that stored
copy instead of reading the replaced schema.

Fully offline (``_row_exclusion_graph.RxGraph``, a fake Athena lake); synthetic
names (the schema of ``test_row_exclusion_unknown_states``).
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents import admin_routes
from agents import connection_resolver as cr
from agents import record_key
from agents import row_exclusion as rx
from tests.unit.test_row_exclusion_graph_faults import _steps
from tests.unit.test_row_exclusion_reprovision import _ctx
from tests.unit.test_row_exclusion_unknown_states import (
    _COUNT, _DISCOVERY, _LEDGER, _LEDGER_COLUMNS, _LEDGER_SUM, _RULE, _SYS, _WIDE,
    _WIDE_COLUMNS, _graph, _pred, _resets)

_SCHEMA = {_WIDE: _WIDE_COLUMNS, _LEDGER: _LEDGER_COLUMNS}
_LEDGER_RULE = {"label": "test rows", "conditions": [{"column": "batch_tag", "op": "empty"}]}
#: A second rule for the wide table.
_OTHER_RULE = [{"column": "state", "op": "empty"}]
_APPLIED = f"{_COUNT} AND {_pred()}"


class _Crash(BaseException):
    """The provision process dies here: nothing after it runs, and no
    ``except Exception`` in the code under test catches it."""


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    monkeypatch.setattr(cr, "audit_connection_event", lambda *_a, **_k: None)
    for reset in _resets():
        reset()
    yield
    for reset in _resets():
        reset()


def _two_tables():
    """The wide table's entry pending with its rule already stored (a set
    interrupted after its fact write), the ledger table settled with its own
    rule."""
    g = _graph(listed=(_WIDE,), pending=True)
    g.list_table(_SYS, _LEDGER)
    g.store(_SYS, _LEDGER, _LEDGER_RULE)
    return g


def _ran(payload: dict) -> bool:
    return "rows" in payload


def _held_for_rewrite(payload: dict) -> bool:
    return (not _ran(payload) and payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
            and payload["unavailable"] == "rewrite_in_progress")


def _crash_on(g, kind: str, nth: int = 1) -> None:
    """Die just before the ``nth`` graph write of ``kind`` lands."""
    seen = {"n": 0}

    def hook(written, _params):
        if written == kind:
            seen["n"] += 1
            if seen["n"] == nth:
                g.before_write = None
                raise _Crash(kind)
    g.before_write = hook


def _reprovision(monkeypatch, g, *, crash=None, between=None):
    """A re-provision of both tables: b0's start (the carry, the mark, the
    delete), the replacement schema, then the restore step, patched when it
    runs. ``crash`` names the step it dies after; ``between`` runs once the
    marker is written, before the delete."""
    def run():
        monkeypatch.setattr(rx, "_provision_graph", lambda: (g.write, g.query))
        monkeypatch.setattr(onboard, "_mark_schema_rewrite", lambda _sid: 1)

        def delete(sid):
            if between is not None:
                between()
            if crash == "after_marker":
                raise _Crash(crash)
            g.delete_schema(sid)
        monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph", delete)
        ctx = _ctx()
        onboard._begin_schema_rewrite(ctx)
        if crash == "after_delete":
            raise _Crash(crash)
        g.recreate(_SYS, _SCHEMA)
        if crash == "after_replacement":
            raise _Crash(crash)
        if crash == "after_one_fact":
            _crash_on(g, "set_fact", 2)
        if crash == "before_marker_cleared":
            _crash_on(g, "swap")
        onboard._dp_restore_row_exclusions(ctx)
        return ctx
    return run


def _crashing(run):
    def crashed():
        with pytest.raises(_Crash):
            run()
    return crashed


def _route(g, body: dict):
    return admin_routes.set_row_exclusion_sync(_SYS, body, "dana", connection_type="Athena",
                                               seams=(g.write, g.query))


# --------------------------------------------------------------------------- #
# A crash: every listed table held, then healed by the next re-provision.
# --------------------------------------------------------------------------- #
def test_a_pending_fact_survives_a_crash_after_the_schema_is_replaced(monkeypatch):
    """A set had stored the wide table's rule and was interrupted before it
    settled; a re-provision then dies once the replacement SchemaEntries
    exist. Its reads are held (never run unfiltered), and the next
    re-provision restores the rule from the copy stored on the System node:
    the entry is still pending and the rule is enforced again."""
    g = _graph(listed=(_WIDE,), pending=True)
    out = _steps(monkeypatch, g, [
        _crashing(_reprovision(monkeypatch, g, crash="after_replacement")), _COUNT,
        _reprovision(monkeypatch, g), _COUNT, _APPLIED])
    _crash, held, ctx, refused, applied = out
    assert _held_for_rewrite(held)
    assert refused["error_name"] == "ROW_EXCLUSION"
    assert applied["rows"] == [["42"]] and applied["row_exclusion"]["applied"] is True
    assert g.stored(_SYS, _WIDE) == _RULE and g.manifest(_SYS)[_WIDE]["pending"] is True
    props = g.entries[f"{_SYS}.{_WIDE}"]["props"]
    assert (props["row_exclusion_by"], props["row_exclusion_source"]) == ("steward", "owner")
    assert ctx.row_exclusions_restored == {"kept": [_WIDE]}
    assert g.rewrite_marker(_SYS) is None


_POINTS = ("after_marker", "after_delete", "after_replacement", "after_one_fact",
           "before_marker_cleared")


@pytest.mark.parametrize("point", _POINTS)
def test_a_crash_at_any_step_holds_every_listed_table_until_the_next_reprovision(
        monkeypatch, point):
    """Whatever step the re-provision dies after, a warm agent (which read
    both rules before) and a cold one hold every read of both listed tables,
    pending and settled; structural discovery runs. The next re-provision
    restores both rules and removes the marker."""
    g = _two_tables()
    out = _steps(monkeypatch, g, [
        _APPLIED, _crashing(_reprovision(monkeypatch, g, crash=point)),
        _COUNT, _LEDGER_SUM, _DISCOVERY,
        rx.reset, _COUNT, _LEDGER_SUM,
        _reprovision(monkeypatch, g), _COUNT, _LEDGER_SUM, _APPLIED])
    before, _crash, warm_count, warm_ledger, discovery = out[:5]
    _cold, cold_count, cold_ledger, ctx, count, ledger, applied = out[5:]
    assert before["row_exclusion"]["applied"] is True
    for held in (warm_count, warm_ledger, cold_count, cold_ledger):
        assert _held_for_rewrite(held), point
    assert _ran(discovery)
    assert count["error_name"] == "ROW_EXCLUSION" and ledger["error_name"] == "ROW_EXCLUSION"
    assert applied["row_exclusion"]["applied"] is True
    assert g.stored(_SYS, _WIDE) == _RULE and g.stored(_SYS, _LEDGER) == _LEDGER_RULE
    assert g.manifest(_SYS)[_WIDE]["pending"] is True
    assert g.manifest(_SYS)[_LEDGER]["pending"] is False
    assert ctx.row_exclusions_restored == {"kept": [_LEDGER, _WIDE]}
    assert g.rewrite_marker(_SYS) is None


# --------------------------------------------------------------------------- #
# A steward during the rewrite.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("change", ["set", "clear"])
def test_a_steward_change_between_the_snapshot_and_the_delete_is_409_and_nothing_is_lost(
        monkeypatch, change):
    g = _graph(listed=(_WIDE,))
    body = ({"table": _WIDE, "label": "test rows", "conditions": _OTHER_RULE}
            if change == "set" else {"table": _WIDE, "conditions": []})
    seen = {}

    def steward():
        before = (g.manifests[_SYS], g.stored(_SYS, _WIDE))
        seen["response"] = _route(g, body)
        seen["unchanged"] = (g.manifests[_SYS], g.stored(_SYS, _WIDE)) == before
    ctx = _reprovision(monkeypatch, g, between=steward)()
    response = seen["response"]
    assert getattr(response, "status_code", 200) == 409
    assert b"rewrite_in_progress" in response.body
    assert seen["unchanged"]
    assert g.stored(_SYS, _WIDE) == _RULE and g.rewrite_marker(_SYS) is None
    assert ctx.row_exclusions_restored == {"kept": [_WIDE]}
    first, applied = _steps(monkeypatch, g, [_COUNT, _APPLIED])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert applied["row_exclusion"]["applied"] is True


def test_a_steward_set_that_lands_before_the_marker_is_carried(monkeypatch):
    """The steward's set completes after the provision read the facts and
    before its marker write: the marker's compare-and-set misses, the
    provision reads again, and the steward's rule is the one carried."""
    g = _graph(listed=(_WIDE,))
    read = g.query
    seen = {}

    def query(cypher, params=None):
        rows = read(cypher, params)
        if cypher == rx.READ_ROW_EXCLUSION_RECORDS and "set" not in seen:
            seen["set"] = rx.set_row_exclusion(_SYS, _WIDE, _OTHER_RULE, label="test rows",
                                               write=g.write, query=read)
        return rows
    monkeypatch.setattr(g, "query", query)
    ctx = _reprovision(monkeypatch, g)()
    assert seen["set"]["status"] == "ok"
    assert g.stored(_SYS, _WIDE)["conditions"] == _OTHER_RULE
    assert g.manifest(_SYS)[_WIDE]["pending"] is False and g.rewrite_marker(_SYS) is None
    assert ctx.row_exclusions_restored == {"kept": [_WIDE]}
    (first,) = _steps(monkeypatch, g, [_COUNT])
    assert first["error_name"] == "ROW_EXCLUSION" and "COALESCE(state, '') = ''" in (
        first["guidance"])


def test_a_never_configured_system_answers_during_the_rewrite_and_its_route_waits(
        monkeypatch):
    """A system with no row exclusion: the rewrite still marks the System
    node, so a steward's first set cannot land on a SchemaEntry the delete
    removes. Its reads run while the marker stands; the route answers 409
    until the next re-provision removes the marker, then sets the rule."""
    g = _graph()
    body = {"table": _WIDE, "label": "test rows", "conditions": _OTHER_RULE}
    out = _steps(monkeypatch, g, [
        _crashing(_reprovision(monkeypatch, g, crash="after_replacement")),
        _COUNT, _LEDGER_SUM, lambda: _route(g, body),
        _reprovision(monkeypatch, g), lambda: _route(g, body), _COUNT])
    _crash, count, ledger, refused, ctx, stored, after = out
    assert count["rows"] == [["42"]] and ledger["rows"] == [["42"]]
    assert getattr(refused, "status_code", 200) == 409
    assert b"rewrite_in_progress" in refused.body
    assert ctx.row_exclusions_restored == {}
    assert stored["status"] == "ok"
    assert after["error_name"] == "ROW_EXCLUSION"
    assert g.rewrite_marker(_SYS) is None
