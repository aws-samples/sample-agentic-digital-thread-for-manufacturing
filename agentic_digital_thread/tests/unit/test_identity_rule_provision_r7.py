# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reader agreement is checked in both directions at provision.

Both runtime readers try an IdentityRule either way round: the route seed
and the resolver also translate a TARGET value back through the target
pattern. 0828dee0 compared the two readers only on the source column's
values (source pattern to target pattern), so ``LOT-{id} -> B-{id}`` with a
target column holding ``b-123`` was written: the route seed (any case) reads
``b-123`` back to ``LOT-123``, the resolver (exact case) does not. Provision
now also samples the rule's target column (the target pattern's LIKE in any
case, the same cap+1 rows and 3 s deadline as the source read) and runs
``resolve.reader_disagreement(target_pattern, source_pattern, target_values)``.
A target column it cannot read leaves the rule unchecked and counted.

Neutral fixture: a shift log (``log-sys``) and a line's stop records
(``line-sys``). SQLite stands in for PostgreSQL; the graph is a fake.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "log-sys", "log_db"
TGT_SYS, TGT_SCHEMA = "line-sys", "line_db"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


@pytest.fixture
def world(monkeypatch):
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})) or True)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    state = SimpleNamespace(writes=writes, db=None)

    def build(source, target, target_type="PostgreSQL", fail=None):
        monkeypatch.setattr(onboard, "catalog", FakeCatalog({
            TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": target_type}}))
        state.db = RuleCheckDb({
            SRC_SCHEMA: {"shift_notes": (["note_id", "code"], _rows(source))},
            TGT_SCHEMA: {"stops": (["stop_id", "ref"], _rows(target))},
        }, fail=fail)
        monkeypatch.setattr(onboard, "pg", state.db)
        return state.db

    state.build = build
    return state


def _rule(src, tgt) -> dict:
    return {"concept": "lot-ref", "source_system": SRC_SYS, "source_field": "shift_notes.code",
            "source_pattern": src, "target_system": TGT_SYS, "target_field": "stops.ref",
            "target_table": "stops", "target_pattern": tgt, "confidence": 0.8,
            "reasoning": "model rule"}


#: The unchecked reason of a method-less rule whose pattern check passed.
_UNMEASURED = ("provision did not confirm this rule with live values "
               "(shift_notes.code and line-sys stops.ref)")


def _run(*rules) -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=SRC_SYS, system_name="Shift Log", schema=SRC_SCHEMA,
                            rules=[dict(r) for r in rules],
                            mappings=[{"table": "shift_notes", "field": "shift_notes.code",
                                       "concept": "lot-ref"}],
                            connection_type="PostgreSQL", rules_from_checkpoint=True,
                            resolve_job_id=onboard._bind_resolve_rules(SRC_SYS, rules))
    ctx = onboard._make_provision_state(body)
    ctx.tables_seen = {"shift_notes"}
    onboard._dp_identity_rules(ctx)
    return ctx


def _merges(writes) -> list:
    return [p for c, p in writes if c.startswith("MERGE (ir:IdentityRule")]


def _reasons(ctx) -> list:
    return [r["reason"] for r in ctx.rules_rejected]


def _unchecked(ctx) -> list:
    return [r["reason"] for r in ctx.rules_unchecked]


def test_a_target_value_the_readers_read_differently_is_rejected(world):
    """REPRODUCTION. Source LOT-123 agrees in both
    readers; target b-123 is read back by the route seed only. 0828dee0
    wrote the rule."""
    world.build(source=["LOT-123"], target=["b-123"])
    ctx = _run(_rule("LOT-{id}", "B-{id}"))
    assert _merges(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "the two identifier readers would read value 'b-123' differently" in reason
    assert "stops.ref" in reason


def test_the_workshop_batch_rule_with_uppercase_targets_is_written(world):
    """Guard (workshop): B-{id} -> LOT-2026-0{id}, upper-case targets: both
    readers agree in both directions (no side unread). The rule is counted
    unchecked only because no value was shown in both columns, and with no
    measured method the fit does not lift the 0.7 ceiling (2026-10-28; the
    model's 0.8 was written)."""
    world.build(source=["B-2847", "B-3150"], target=["LOT-2026-02847", "LOT-2026-03150"])
    ctx = _run(_rule("B-{id}", "LOT-2026-0{id}"))
    assert ctx.rules_rejected == [] and [e["reason"] for e in ctx.rules_unchecked] == [_UNMEASURED]
    (merge,) = _merges(world.writes)
    assert merge["conf"] == 0.7


def test_the_target_sample_is_read_in_any_case_with_the_short_deadline(world):
    """REPRODUCTION: provision read the target column for a transform rule
    not at all; now once, through the target pattern's LIKE in any case,
    bounded, under the 3 s deadline."""
    db = world.build(source=["B-2847"], target=["LOT-2026-02847"])
    _run(_rule("B-{id}", "LOT-2026-0{id}"))
    reads = db.reads_of("stops")
    assert len(reads) == 1
    (_schema, sql, params, kw) = reads[0]
    assert "lower(CAST(ref AS TEXT)) LIKE lower(%s)" in sql and "LIMIT 501" in sql
    assert params == ("LOT-2026-0%",) and kw.get("statement_timeout_ms") == 3000


@pytest.mark.parametrize("case", ["not-postgresql", "read-fails"])
def test_a_target_that_cannot_be_read_leaves_the_rule_unchecked_and_counted(world, case):
    """REPRODUCTION: a target side provision cannot sample (not PostgreSQL,
    a read that fails or times out) leaves the reverse reader check undone;
    the rule is written unchecked (0.7 cap) and counted."""
    if case == "not-postgresql":
        world.build(source=["B-2847"], target=["LOT-2026-02847"], target_type="REST API")
    else:
        world.build(source=["B-2847"], target=["LOT-2026-02847"],
                    fail=lambda schema, sql: schema == TGT_SCHEMA and "FROM stops" in sql)
    ctx = _run(_rule("B-{id}", "LOT-2026-0{id}"))
    (merge,) = _merges(world.writes)
    assert merge["conf"] == 0.7
    (reason,) = _unchecked(ctx)
    assert "the two identifier readers were not compared on its values" in reason


def test_a_target_column_with_no_value_the_pattern_selects_is_not_unchecked(world):
    """Guard: the target column was read and holds nothing the target
    pattern selects: nothing to disagree on, the pattern is checked. The one
    unchecked entry is that no value was shown in both columns."""
    world.build(source=["B-2847"], target=["X-1"])
    ctx = _run(_rule("B-{id}", "LOT-2026-0{id}"))
    assert [e["reason"] for e in ctx.rules_unchecked] == [_UNMEASURED]
    assert len(_merges(world.writes)) == 1
