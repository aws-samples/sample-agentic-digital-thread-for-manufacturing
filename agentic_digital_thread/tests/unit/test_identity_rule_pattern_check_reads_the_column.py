# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provision's live pattern check reads the source column, not one value.

``_dp_rule_pattern_check`` read ONE arbitrary value (``SELECT DISTINCT ...
LIMIT 1``, no order) and ran ``resolve.transform_id`` on it. Two ways that
rejected a rule the Resolve checkpoint kept:

* the resolve model writes a direct match (values equal on both sides) as the
  shared values themselves, ``a|b|c`` on both sides. That pattern names no
  ``{placeholder}``, ``transform_id`` returns None for EVERY value, so the
  rule was rejected whatever the column held ("pattern ... doesn't match
  sample value ..."). It was written only when the check was skipped by
  accident: a source field the model qualified with the system id gave the
  check the system id as its table, ``validate_identifier`` refused it and the
  pattern was cleared;
* a transform pattern on a column that mixes two kinds of value (logins and
  names) passed or failed on whichever value the database returned first.

The check now reads a direct pattern's values against the column (and, since
round 2, the target column too: ``test_identity_rule_provision_recheck.py``),
reads a transform pattern against the column's values, takes the table from
the segment before the column however the field is qualified, and still
rejects a pattern no value fits and a rule whose target is missing. Every
rejected rule is logged once with its reason.

The fixture schema is neutral (a shift log and a line's stop records); the
fake database is SQLite answering the same statements.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog as _Catalog  # noqa: E402
from tests.unit._rule_check_db import RuleCheckDb as _Db  # noqa: E402

SRC_SYS, SRC_SCHEMA = "log-sys", "log_db"
TGT_SYS, TGT_SCHEMA = "line-sys", "line_db"

LOGINS = ["tech_01", "tech_02", "tech_07", "lead"]
NAMES = ["A. Lee", "B. Cho", "Shift planner"]
LISTED = "tech_01|tech_02|tech_07|lead"


def _signers(first: list, then: list) -> list:
    """Rows of ``shift_notes``: each value in ``first`` then ``then`` (the
    insertion order SQLite returns an unordered read in), and a NULL."""
    rows = [(i, v) for i, v in enumerate(first + then, start=1)]
    return rows + [(len(rows) + 1, None)]


@pytest.fixture
def world(monkeypatch):
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "catalog", _Catalog({
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL"}}))

    def build(signers_first=NAMES, signers_then=LOGINS):
        db = _Db({
            SRC_SCHEMA: {"shift_notes": (["note_id", "signed_by"],
                                         _signers(list(signers_first), list(signers_then)))},
            TGT_SCHEMA: {"stops": (["stop_id", "closed_by"],
                                   [(i, v) for i, v in enumerate(LOGINS, start=1)])},
        })
        monkeypatch.setattr(onboard, "pg", db)
        return db

    return SimpleNamespace(writes=writes, build=build)


def _rule(**over) -> dict:
    """The rule shape the resolve model writes for a direct match: the
    measured shared values as the pattern on both sides."""
    base = {"concept": "operator", "source_system": SRC_SYS,
            "source_field": "shift_notes.signed_by", "source_pattern": LISTED,
            "target_system": TGT_SYS, "target_field": "stops.closed_by",
            "target_pattern": LISTED, "confidence": 0.8,
            "reasoning": "4 shared values measured"}
    base.update(over)
    return base


def _ctx(rules: list) -> onboard._ProvisionState:
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=SRC_SYS, system_name="Shift Log",
        plant="", schema=SRC_SCHEMA, rules=rules,
        mappings=[{"table": "shift_notes", "field": "shift_notes.signed_by",
                   "concept": "operator"}],
        connection_type="PostgreSQL", cm_items=[], committable=set(),
        tables_seen={"shift_notes"}, rules_from_checkpoint=True,  # the wizard's rules,
        checkpoint_rule_ids=frozenset(map(id, rules)))  # in its Resolve result


def _written(writes) -> list:
    return [p for c, p in writes if c.startswith("MERGE (ir:IdentityRule")]


def _reasons(ctx) -> list:
    return [r["reason"] for r in ctx.rules_rejected]


# ── the defect: a direct-match rule is rejected whatever the column holds ────

@pytest.mark.parametrize("first,then", [(NAMES, LOGINS), (LOGINS, NAMES)])
@pytest.mark.parametrize("pattern", [LISTED, "tech_02", "*"])
def test_a_direct_match_rule_the_checkpoint_kept_is_written(world, pattern, first, then):
    """REPRODUCTION. The kept rule names the shared values (or the bare ``*``
    direct-match convention) on both sides. It fits the column: some of its
    values are there. Unmodified code rejected it in either row order
    ("pattern ... doesn't match sample value ..."), even when the value it
    read was one of the listed values, because the pattern names no
    ``{placeholder}`` and ``transform_id`` then returns None."""
    world.build(first, then)
    ctx = _ctx([_rule(source_pattern=pattern, target_pattern=pattern)])
    onboard._dp_identity_rules(ctx)
    assert _reasons(ctx) == []
    (written,) = _written(world.writes)
    assert (written["src_field"], written["tgt_sys"], written["tgt_field"]) == (
        "signed_by", TGT_SYS, "closed_by")
    # A direct match transforms nothing, so no sample check lifts the cap.
    assert written["conf"] == 0.7


@pytest.mark.parametrize("first,then", [(NAMES, LOGINS), (LOGINS, NAMES)])
def test_a_transform_pattern_is_judged_on_the_column_not_on_one_value(world, first, then):
    """REPRODUCTION (order dependence). ``tech_{n}`` fits the column's
    logins and not its names. The old check read whichever value came back
    first, so the result depended on row order; now both orders write it.
    The fit validates the pattern on one column only, so a rule with no
    measured method keeps the 0.7 ceiling (2026-10-28; it was lifted to the
    model's 0.8)."""
    world.build(first, then)
    ctx = _ctx([_rule(source_pattern="tech_{n}", target_pattern="tech_{n}")])
    onboard._dp_identity_rules(ctx)
    assert _reasons(ctx) == []
    (written,) = _written(world.writes)
    assert written["conf"] == 0.7


def test_a_qualified_source_field_does_not_skip_the_check(world):
    """REPRODUCTION. The model can qualify the field with the system id
    (``<system>.<table>.<column>``). The check took the system id as the
    table, refused it as an identifier and wrote the rule unchecked; a
    pattern no value fits went through. The table is the segment before the
    column, so it is checked like any other spelling."""
    world.build()
    ctx = _ctx([_rule(source_field=f"{SRC_SYS}.shift_notes.signed_by",
                      source_pattern="WO-{n}", target_pattern="WO-{n}")])
    onboard._dp_identity_rules(ctx)
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "doesn't match sample value" in reason and "shift_notes.signed_by" in reason


def test_a_qualified_source_field_with_a_fitting_pattern_is_written(world):
    """Guard: the same qualified spelling with the direct-match pattern."""
    world.build()
    ctx = _ctx([_rule(source_field=f"{SRC_SYS}.shift_notes.signed_by")])
    onboard._dp_identity_rules(ctx)
    assert _reasons(ctx) == []
    assert len(_written(world.writes)) == 1


# ── what is still rejected ───────────────────────────────────────────────────

@pytest.mark.parametrize("src,tgt,needle", [
    ("WO-{n}", "WO-{n}", "doesn't match sample value"),
    ("x_9|y_9", "x_9|y_9", "none of which occur in shift_notes.signed_by"),
    ("tech_01", "T-01", "names no {placeholder}"),
    ("*", "T-{n}", "names no {placeholder}"),
])
def test_a_pattern_that_does_not_fit_the_column_is_rejected(world, src, tgt, needle):
    """Guard: a transform no value fits, a value list none of whose values
    occur, and literal patterns that differ (no transform) are rejected."""
    world.build()
    ctx = _ctx([_rule(source_pattern=src, target_pattern=tgt)])
    onboard._dp_identity_rules(ctx)
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert needle in reason


def test_rejection_does_not_depend_on_row_order(world):
    """A pattern no value fits is rejected in both row orders, with the same
    reason (the example value is the column's first value in order)."""
    reasons = []
    for first, then in ((NAMES, LOGINS), (LOGINS, NAMES)):
        world.writes.clear()
        world.build(first, then)
        ctx = _ctx([_rule(source_pattern="WO-{n}", target_pattern="WO-{n}")])
        onboard._dp_identity_rules(ctx)
        reasons.append(_reasons(ctx))
    assert reasons[0] == reasons[1] and len(reasons[0]) == 1


@pytest.mark.parametrize("over,needle", [
    ({"target_field": "stops.closed_on"}, "target_field 'stops.closed_on' not found in line_db"),
    ({"target_system": "no-such-sys"}, "target_system 'no-such-sys' is not an onboarded system"),
])
def test_a_rule_whose_target_is_missing_is_rejected(world, over, needle):
    """Guard: a target column or system that does not exist."""
    world.build()
    ctx = _ctx([_rule(**over)])
    onboard._dp_identity_rules(ctx)
    assert _written(world.writes) == []
    assert _reasons(ctx) == [needle]


def test_an_empty_source_column_is_not_a_mismatch(world, monkeypatch):
    """Guard (unchanged): with nothing to sample the rule is written
    unchecked, the cap still applies."""
    db = _Db({SRC_SCHEMA: {"shift_notes": (["note_id", "signed_by"], [(1, None)])},
              TGT_SCHEMA: {"stops": (["stop_id", "closed_by"], [(1, "tech_01")])}})
    monkeypatch.setattr(onboard, "pg", db)
    ctx = _ctx([_rule(source_pattern="WO-{n}", target_pattern="WO-{n}")])
    onboard._dp_identity_rules(ctx)
    assert _reasons(ctx) == []
    assert [p["conf"] for p in _written(world.writes)] == [0.7]


# ── every rejected rule is logged once, with its reason ─────────────────────

def test_every_rejected_rule_is_logged_once_with_its_reason(world, caplog):
    """REPRODUCTION. The target and pattern branches appended to
    ``rules_rejected`` without a log line, so a provision summary of
    ``identity_rules_rejected=1`` had no reason anywhere in the app log."""
    world.build()
    rules = [
        _rule(target_field="stops.closed_on"),                        # target column missing
        _rule(target_system="no-such-sys"),                           # target system missing
        _rule(source_pattern="WO-{n}", target_pattern="WO-{n}"),      # pattern fits no value
        _rule(source_pattern="x_9|y_9", target_pattern="x_9|y_9"),    # values do not occur
        _rule(source_pattern="tech_01", target_pattern="T-01"),       # no transform
        _rule(concept="failure-mode"),                                # category concept
    ]
    ctx = _ctx(rules)
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        onboard._dp_identity_rules(ctx)
    assert len(ctx.rules_rejected) == len(rules)
    lines = [r.getMessage() for r in caplog.records if "IdentityRule rejected" in r.getMessage()]
    assert len(lines) == len(rules)
    for line, reason in zip(lines, _reasons(ctx)):
        assert reason in line
