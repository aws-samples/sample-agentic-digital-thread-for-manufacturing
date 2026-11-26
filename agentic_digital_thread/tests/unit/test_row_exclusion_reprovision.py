# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A re-provision keeps a table's row exclusion across its schema rewrite.

Before this change a re-provision deleted every SchemaEntry of the system,
the stored facts with them, and kept the System node, so the manifest still
listed each table as settled under the same token. A warm system agent (the
cloud fast path keeps the running runtime) read only the manifest, saw no
change and kept its cached fact, even when the new schema no longer had a
condition column; a cold agent read the missing fact and refused every read
of the table until a steward set it again. Warm and cold disagreed, and the
owner's rule was lost.

Now the provision reads each listed table's fact and provenance before the
delete (and stops, before anything is deleted, if it cannot). Once the
replacement SchemaEntries and Fields exist it gives every listed table a new
manifest token, writes each fact back under it with its provenance, and
checks it against the new columns with the steward route's own check. A warm
agent reads the re-tokened tables again on its next query, so warm and cold
agree: an unchanged schema keeps the rule (reads filtered), and a removed
condition column leaves that table unavailable with the steward repair
reason while other tables run.

Fully offline (``_row_exclusion_graph.RxGraph``, a fake Athena lake); synthetic
names (the schema of ``test_row_exclusion_unknown_states``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.onboard as onboard
from agents import record_key
from agents import row_exclusion as rx
from tests.unit.test_row_exclusion_graph_faults import _steps
from tests.unit.test_row_exclusion_unknown_states import (
    _COUNT, _LEDGER, _LEDGER_COLUMNS, _LEDGER_SUM, _RULE, _SYS, _WIDE, _WIDE_COLUMNS, _graph,
    _pred, _resets)

_UNCHANGED = {_WIDE: _WIDE_COLUMNS, _LEDGER: _LEDGER_COLUMNS}
#: The new schema dropped one of the rule's condition columns.
_DROPPED = {_WIDE: {c: t for c, t in _WIDE_COLUMNS.items() if c != "route_tag"},
            _LEDGER: _LEDGER_COLUMNS}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    for reset in _resets():
        reset()
    yield
    for reset in _resets():
        reset()


def _ctx():
    return SimpleNamespace(system_id=_SYS, connection_type=onboard.ATHENA,
                           tables_seen={_WIDE, _LEDGER}, row_exclusions_owner=None,
                           row_exclusions_taken=None, row_exclusions_restored=None)


def _rewrite(monkeypatch, g, *, recreate=_UNCHANGED, between=None):
    """b0's start (the row exclusions read, the mark, the delete), then the
    replacement schema (b to b2a, ``recreate``, also the provision's own
    table list), then the restore step; ``between`` runs after the
    replacement exists, before the restore."""
    monkeypatch.setattr(rx, "_provision_graph", lambda: (g.write, g.query))
    monkeypatch.setattr(onboard, "_mark_schema_rewrite", lambda _sid: 1)
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph", g.delete_schema)
    # The record-key carry now also snapshots before the delete with a checked
    # read; these tests declare no record key, so answer it with no rows.
    monkeypatch.setattr(onboard, "neptune_query_checked", lambda *_a, **_k: [{}])
    ctx = _ctx()
    ctx.tables_seen = set(recreate)

    def run():
        onboard._begin_schema_rewrite(ctx)
        g.recreate(_SYS, recreate)
        if between is not None:
            between()
        onboard._dp_restore_row_exclusions(ctx)
        return ctx
    return run


def _checked(first, applied, ledger) -> None:
    """The rule is kept: the read without it is refused for the missing
    predicate, the read with it runs and says so, other tables run."""
    assert first["error_name"] == "ROW_EXCLUSION" and f"AND {_pred()}" in first["guidance"]
    assert applied["rows"] == [["42"]] and applied["row_exclusion"]["applied"] is True
    assert ledger["rows"] == [["42"]]


def _unfit(first, ledger) -> None:
    """The removed condition column: that table unavailable, with the steward
    repair reason; other tables run."""
    assert first["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert first["unavailable"] == "unknown_column" and first["columns"] == ["route_tag"]
    assert "A steward must repair or clear the row exclusion" in first["guidance"]
    assert ledger["rows"] == [["42"]]


# --------------------------------------------------------------------------- #
# Warm (the fast path keeps the running agent) and cold, both schemas.
# --------------------------------------------------------------------------- #
def test_warm_unchanged_schema_keeps_the_rule_and_rereads_it(monkeypatch):
    g = _graph(listed=(_WIDE,))
    before = g.manifest(_SYS)[_WIDE]["token"]
    reprovision = _rewrite(monkeypatch, g)
    out = _steps(monkeypatch, g, [reprovision, g.calls.clear, _COUNT,
                                  f"{_COUNT} AND {_pred()}", _LEDGER_SUM])
    ctx, _clear, first, applied, ledger = out
    _checked(first, applied, ledger)
    assert "facts" in g.kinds()                       # the warm agent read it again
    assert g.manifest(_SYS)[_WIDE]["token"] != before
    assert g.stored(_SYS, _WIDE) == _RULE
    props = g.entries[f"{_SYS}.{_WIDE}"]["props"]
    assert (props["row_exclusion_by"], props["row_exclusion_at"],
            props["row_exclusion_source"]) == ("steward", "2026-10-20T00:00:00Z", "owner")
    assert ctx.row_exclusions_restored == {"kept": [_WIDE]}


def test_cold_unchanged_schema_keeps_the_rule(monkeypatch):
    g = _graph(listed=(_WIDE,))
    _rewrite(monkeypatch, g)()
    first, applied, ledger = _steps(monkeypatch, g, [_COUNT, f"{_COUNT} AND {_pred()}",
                                                     _LEDGER_SUM])
    _checked(first, applied, ledger)


def test_warm_removed_condition_column_makes_that_table_unavailable(monkeypatch):
    g = _graph(listed=(_WIDE,))
    reprovision = _rewrite(monkeypatch, g, recreate=_DROPPED)
    ctx, first, ledger = _steps(monkeypatch, g, [reprovision, _COUNT, _LEDGER_SUM])
    _unfit(first, ledger)
    assert g.stored(_SYS, _WIDE) == _RULE             # kept for the steward to repair
    assert ctx.row_exclusions_restored == {"unfit": [_WIDE]}


def test_cold_removed_condition_column_makes_that_table_unavailable(monkeypatch):
    g = _graph(listed=(_WIDE,))
    _rewrite(monkeypatch, g, recreate=_DROPPED)()
    first, ledger = _steps(monkeypatch, g, [_COUNT, _LEDGER_SUM])
    _unfit(first, ledger)


# --------------------------------------------------------------------------- #
# The edges of the carry.
# --------------------------------------------------------------------------- #
def test_a_table_gone_from_the_new_schema_is_unavailable_and_others_run(monkeypatch):
    g = _graph(listed=(_WIDE,))
    ctx = _rewrite(monkeypatch, g, recreate={_LEDGER: _LEDGER_COLUMNS})()
    first, ledger = _steps(monkeypatch, g, [_COUNT, _LEDGER_SUM])
    assert first["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert first["unavailable"] == "entry_missing"
    assert ledger["rows"] == [["42"]]
    assert ctx.row_exclusions_restored == {"missing": [_WIDE]}


def test_the_provision_stops_before_the_delete_when_the_rules_cannot_be_read(monkeypatch):
    g = _graph(listed=(_WIDE,))
    g.fail.add("manifest")
    calls = []
    monkeypatch.setattr(rx, "_provision_graph", lambda: (g.write, g.query))
    monkeypatch.setattr(onboard, "_mark_schema_rewrite", lambda _sid: calls.append("mark") or 1)
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph",
                        lambda _sid: calls.append("delete"))
    with pytest.raises(RuntimeError, match="row exclusions"):
        onboard._begin_schema_rewrite(_ctx())
    assert calls == [] and g.stored(_SYS, _WIDE) == _RULE


def test_a_never_configured_system_marks_and_clears_the_rewrite_and_reads_no_fact(
        monkeypatch):
    """The rewrite marks the System node even with nothing listed (so a
    steward's first set cannot land on a SchemaEntry the delete removes,
    ``test_row_exclusion_rewrite_barrier``), reads no fact and leaves the
    manifest as it found it."""
    g = _graph()
    ctx = _rewrite(monkeypatch, g)()
    assert g.kinds().count("swap") == 2
    assert not {"facts", "records", "fields", "set_fact"} & set(g.kinds())
    assert g.manifests[_SYS] is None
    assert ctx.row_exclusions_restored == {}


def test_a_system_that_is_not_athena_is_not_read(monkeypatch):
    g = _graph(listed=(_WIDE,))
    monkeypatch.setattr(rx, "_provision_graph", lambda: (g.write, g.query))
    monkeypatch.setattr(onboard, "_mark_schema_rewrite", lambda _sid: 1)
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph", lambda _sid: None)
    ctx = _ctx()
    ctx.connection_type = "PostgreSQL"
    onboard._begin_schema_rewrite(ctx)
    onboard._dp_restore_row_exclusions(ctx)
    assert g.calls == []


# --------------------------------------------------------------------------- #
# Where provision runs the restore.
# --------------------------------------------------------------------------- #
_SCHEMA_STEPS = ("_dp_schema_entries", "_dp_field_mappings", "_dp_athena_physical_fields",
                 "_dp_discovery_completeness", "_dp_reapply_user_corrections",
                 "_dp_identity_key_edges", "_dp_mint_unmapped", "_dp_internal_joinpaths",
                 "_dp_mirrored_measure_tags", "_dp_settle_field_concepts")
_LATER_STEPS = ("_dp_valueoverlap_joinpaths", "_dp_samerow_joinpaths", "_dp_identity_rules",
                "_dp_rule_joinpaths", "_dp_cross_system_vms", "_dp_equipment_vms_legacy",
                "_dp_authority_rules", "_dp_onboarding_log", "_dp_start_pg_agent",
                "_dp_finalize")


def _record_steps(monkeypatch, *, failing=None) -> list:
    seen: list = []
    monkeypatch.setattr(onboard, "_make_provision_state",
                        lambda _body: SimpleNamespace(retired_links=[]))
    monkeypatch.setattr(onboard, "_dp_connection_phase", lambda _ctx: None)
    monkeypatch.setattr(onboard, "_dp_collect_tables", lambda _ctx: None)
    monkeypatch.setattr(onboard, "_dp_discover_athena_physical", lambda _ctx: None)

    def step(name):
        def run(_ctx):
            seen.append(name)
            if name == failing:
                raise RuntimeError("step failed")
        return run
    for name in (*_SCHEMA_STEPS, "_dp_restore_row_exclusions", "_dp_schema_rewritten",
                 *_LATER_STEPS):
        monkeypatch.setattr(onboard, name, step(name))
    monkeypatch.setattr(onboard, "_dp_result", lambda _ctx: seen.append("_dp_result") or {})
    return seen


def test_provision_restores_once_the_physical_fields_exist(monkeypatch):
    seen = _record_steps(monkeypatch)
    onboard._do_provision_base(None)
    at = seen.index("_dp_athena_physical_fields")
    assert seen[at + 1] == "_dp_restore_row_exclusions"
    # Again in the rewrite's finally (a no-op once restored), before it ends.
    assert seen.index("_dp_schema_rewritten") - 1 == len(seen) - 1 - seen[::-1].index(
        "_dp_restore_row_exclusions")


def test_provision_still_restores_when_a_schema_step_fails_first(monkeypatch):
    seen = _record_steps(monkeypatch, failing="_dp_field_mappings")
    with pytest.raises(RuntimeError, match="step failed"):
        onboard._do_provision_base(None)
    assert seen[-2:] == ["_dp_restore_row_exclusions", "_dp_schema_rewritten"]
