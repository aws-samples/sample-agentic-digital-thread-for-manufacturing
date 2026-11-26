# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A row exclusion is never lost to a partial schema rewrite or an overlap.

Before this change three schedules ended with a listed table whose manifest
entry was pending and whose SchemaEntry held no fact, which a system agent
read as "no exclusion", so a plain ``COUNT(*)`` of that table ran unfiltered:

* a re-provision whose schema step raised before it wrote the table again:
  its restore found the table missing, still removed the rewrite marker (the
  only stored copy of the fact), and a retry then had nothing to carry;
* two re-provisions that overlapped: the second reused the first's marker
  without owning it, the first removed it, and the second deleted the schema
  with no copy left;
* a steward's first set in flight when the marker was copied (a pending
  entry whose copy holds no fact).

Now a pending entry with nothing stored is held (``write_pending``), not
absent; each marker names the run that owns it, so only that run removes it
(a later run takes it over, and the earlier one's restore is reported as
failed); and a restore keeps the marker while a carried fact could not be
written back to a table this provision writes. A table the provision does not
write is gone from the system: the marker is removed, its reads stay held
(``entry_missing``) and a steward can clear it.

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
from agents import row_exclusion_manifest as manifest
from tests.unit.test_row_exclusion_graph_faults import _steps
from tests.unit.test_row_exclusion_reprovision import _ctx
from tests.unit.test_row_exclusion_unknown_states import (
    _COUNT, _LEDGER, _LEDGER_COLUMNS, _LEDGER_SUM, _RULE, _SYS, _WIDE, _WIDE_COLUMNS, _graph,
    _pred, _resets)

_SCHEMA = {_WIDE: _WIDE_COLUMNS, _LEDGER: _LEDGER_COLUMNS}
_APPLIED = f"{_COUNT} AND {_pred()}"


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


def _seams(monkeypatch, g) -> None:
    monkeypatch.setattr(rx, "_provision_graph", lambda: (g.write, g.query))
    monkeypatch.setattr(onboard, "_mark_schema_rewrite", lambda _sid: 1)
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph", g.delete_schema)


def _provision(monkeypatch, g, *, written=_SCHEMA, fail=False):
    """One re-provision as the coordinator runs it: b0's start (carry, mark,
    delete), the schema step writing ``written``, and the restore, also from
    the ``finally`` when the schema step raised (``fail``) after writing only
    ``written``. Its own table list (``tables_seen``) is the whole schema."""
    def run():
        _seams(monkeypatch, g)
        ctx = _ctx()
        ctx.tables_seen = set(_SCHEMA)
        try:
            onboard._begin_schema_rewrite(ctx)
            g.recreate(_SYS, written)
            if fail:
                raise RuntimeError("schema step failed")
            onboard._dp_restore_row_exclusions(ctx)
        except RuntimeError:
            pass
        finally:
            onboard._dp_restore_row_exclusions(ctx)
        return ctx
    return run


def _ran(payload) -> bool:
    return isinstance(payload, dict) and "rows" in payload


def _held(payload, why: str) -> bool:
    return (not _ran(payload) and payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
            and payload["unavailable"] == why)


def _route(g, body: dict):
    return admin_routes.set_row_exclusion_sync(_SYS, body, "dana", connection_type="Athena",
                                               seams=(g.write, g.query))


# --------------------------------------------------------------------------- #
# A pending entry with nothing stored is held, never absent.
# --------------------------------------------------------------------------- #
def test_a_pending_entry_with_nothing_stored_is_held_until_a_steward_settles_it(monkeypatch):
    """An interrupted set (listed pending, no fact written yet): the table's
    reads are held with the steward repair sentence; the steward sets the
    rule again and it is enforced; a clear then lets reads run."""
    g = _graph(listed=(_WIDE,), pending=True, stored=False)

    def set_rule():
        return rx.set_row_exclusion(_SYS, _WIDE, _RULE["conditions"], label=_RULE["label"],
                                    write=g.write, query=g.query)

    def clear_rule():
        return rx.clear_row_exclusion(_SYS, _WIDE, write=g.write, query=g.query)
    held, ledger, _set, refused, _clear, after = _steps(monkeypatch, g, [
        _COUNT, _LEDGER_SUM, set_rule, f"{_COUNT} AND state <> 'X'", clear_rule,
        f"{_COUNT} AND state <> 'Y'"])
    assert _held(held, "write_pending")
    assert "a steward must set it again, or clear it" in held["guidance"]
    assert _ran(ledger)
    assert _set["status"] == "ok" and refused["error_name"] == "ROW_EXCLUSION"
    assert _clear["status"] == "ok" and _ran(after)


def test_a_pending_entry_carried_with_no_fact_is_held_after_the_rewrite(monkeypatch):
    """A steward's first set was in flight when the marker copied the table
    (a pending entry, nothing stored): after the rewrite the entry is still
    pending with no fact, and its reads are held, not run unfiltered."""
    g = _graph(listed=(_WIDE,), pending=True, stored=False)
    ctx, held = _steps(monkeypatch, g, [_provision(monkeypatch, g), _COUNT])
    assert g.rewrite_marker(_SYS) is None
    assert g.manifest(_SYS)[_WIDE]["pending"] is True
    assert _held(held, "write_pending")


# --------------------------------------------------------------------------- #
# A schema step that fails keeps the stored copy for the retry.
# --------------------------------------------------------------------------- #
def test_a_failed_schema_step_keeps_the_carried_rule_and_the_retry_restores_it(monkeypatch):
    """The reviewer's first schedule: a set had stored the wide table's rule
    and was interrupted (pending). A re-provision deletes the schema, its
    schema step raises before it writes that table again, and the restore in
    the ``finally`` cannot write the fact back. The marker (the only stored
    copy) stays, every listed table is held, and the admin's retry restores
    the rule, which is then enforced."""
    g = _graph(listed=(_WIDE,), pending=True)
    out = _steps(monkeypatch, g, [
        _provision(monkeypatch, g, written={_LEDGER: _LEDGER_COLUMNS}, fail=True),
        _COUNT, _LEDGER_SUM,
        _provision(monkeypatch, g), _COUNT, _APPLIED])
    first, held, ledger, retry, refused, applied = out
    assert first.row_exclusions_restored.get("missing") == [_WIDE]
    assert first.row_exclusions_restored.get("held") == [_WIDE]
    assert _held(held, "rewrite_in_progress") and _ran(ledger)
    assert retry.row_exclusions_restored == {"kept": [_WIDE]}
    assert g.rewrite_marker(_SYS) is None
    assert g.stored(_SYS, _WIDE) == _RULE and g.manifest(_SYS)[_WIDE]["pending"] is True
    assert refused["error_name"] == "ROW_EXCLUSION"
    assert applied["row_exclusion"]["applied"] is True


def test_a_table_the_provision_does_not_write_ends_the_marker_and_a_steward_can_clear_it(
        monkeypatch):
    """A listed table the provision's own table list leaves out is gone from
    the system, not lost to a failed step: the marker is removed (the route
    is not blocked for good), its reads stay held, and a steward clear takes
    it off the manifest."""
    g = _graph(listed=(_WIDE,))

    def run():
        _seams(monkeypatch, g)
        ctx = _ctx()
        ctx.tables_seen = {_LEDGER}
        onboard._begin_schema_rewrite(ctx)
        g.recreate(_SYS, {_LEDGER: _LEDGER_COLUMNS})
        onboard._dp_restore_row_exclusions(ctx)
        return ctx
    ctx, held, ledger = _steps(monkeypatch, g, [run, _COUNT, _LEDGER_SUM])
    assert ctx.row_exclusions_restored == {"missing": [_WIDE]}
    assert g.rewrite_marker(_SYS) is None
    assert _held(held, "entry_missing") and _ran(ledger)
    cleared = _route(g, {"table": _WIDE, "conditions": []})
    assert getattr(cleared, "status_code", 200) == 200
    assert _WIDE not in g.manifest(_SYS)


# --------------------------------------------------------------------------- #
# The marker's owner.
# --------------------------------------------------------------------------- #
def test_an_overlapping_rewrite_cannot_end_the_marker_the_later_run_owns(monkeypatch):
    """The reviewer's second schedule (a provision lease that failed to keep
    two runs apart): run A marks, deletes and writes the schema; run B takes
    the marker over before its own delete; A's restore must not remove it.
    B then deletes and writes the schema, restores the rule from the stored
    copy and removes the marker; the rule is enforced."""
    g = _graph(listed=(_WIDE,), pending=True)
    _seams(monkeypatch, g)
    a, b = _ctx(), _ctx()
    a.tables_seen = b.tables_seen = set(_SCHEMA)

    def a_writes():
        onboard._begin_schema_rewrite(a)
        g.recreate(_SYS, _SCHEMA)

    def b_takes_over():
        b.row_exclusions_taken = onboard._take_row_exclusions(b)

    def b_rewrites():
        g.delete_schema(_SYS)
        g.recreate(_SYS, _SCHEMA)
        onboard._dp_restore_row_exclusions(b)
    out = _steps(monkeypatch, g, [
        a_writes, b_takes_over, lambda: onboard._dp_restore_row_exclusions(a), _COUNT,
        b_rewrites, _COUNT, _APPLIED])
    _a, _b, _a_restore, held, _b_restore, refused, applied = out
    assert "failed" in a.row_exclusions_restored
    assert _held(held, "rewrite_in_progress")
    assert b.row_exclusions_restored == {"kept": [_WIDE]}
    assert g.rewrite_marker(_SYS) is None and g.stored(_SYS, _WIDE) == _RULE
    assert refused["error_name"] == "ROW_EXCLUSION"
    assert applied["row_exclusion"]["applied"] is True


def test_the_marker_names_its_owner_and_only_the_owner_ends_it():
    g = _graph(listed=(_WIDE,))
    carry = lambda entries: {t: {"pending": e.pending, "record": None}  # noqa: E731
                             for t, e in entries.items()}
    manifest.begin_rewrite(_SYS, carry, owner="run-a", query=g.query, write=g.write)
    assert g.rewrite_marker(_SYS)["owner"] == "run-a"
    with pytest.raises(manifest.ManifestWriteFailed):
        manifest.end_rewrite(_SYS, lambda e: e, owner="run-b", query=g.query, write=g.write)
    assert g.rewrite_marker(_SYS)["owner"] == "run-a"
    taken = manifest.begin_rewrite(_SYS, carry, owner="run-b", query=g.query, write=g.write)
    assert taken == {_WIDE: {"pending": False, "record": None}}
    assert g.rewrite_marker(_SYS)["owner"] == "run-b"
    with pytest.raises(manifest.ManifestWriteFailed):
        manifest.end_rewrite(_SYS, lambda e: e, owner="run-a", query=g.query, write=g.write)
    manifest.end_rewrite(_SYS, lambda e: e, owner="run-b", query=g.query, write=g.write)
    assert g.rewrite_marker(_SYS) is None
    with pytest.raises(manifest.ManifestWriteFailed):
        manifest.end_rewrite(_SYS, lambda e: e, owner="run-b", query=g.query, write=g.write)
