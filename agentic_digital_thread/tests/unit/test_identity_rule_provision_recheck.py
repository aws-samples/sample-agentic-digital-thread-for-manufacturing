# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provision re-check of an identity rule, round 2 (review of 040f8b62).

* A direct match (the same text on both sides, no slot: the values the rule
  says both columns share) is proven by a value it names that occurs in the
  source column AND in the target column, read in the table the rule names. A
  value found on one side only, or values found on different sides, prove no
  join. A bare ``*`` names no value: it is written without a re-check, and
  counted.
* A transform pattern is read the way ``resolve.transform_id`` reads it (its
  literal text is a regular expression); the SQL prefilter is only used when
  it selects a superset of what that matcher accepts. A malformed pattern is
  rejected with a reason, never raised.
* Every read is bounded (no sort of the whole table), has a short deadline,
  and a check that could not run leaves the rule written unchecked, logged
  and counted in the provision summary and result.
* A validated direct match is stored with both patterns empty (the direct
  join convention) and its evidence in ``origin_context``, which the
  onboarding stamp keeps.

Neutral fixture: a shift log (``log-sys``) and a line's stop records
(``line-sys``); ``hub-sys`` is a REST system. SQLite stands in for PostgreSQL.
"""
from __future__ import annotations

import logging
import os
import re
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents import resolve  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "log-sys", "log_db"
TGT_SYS, TGT_SCHEMA = "line-sys", "line_db"
HUB_SYS = "hub-sys"

LOGINS = ["tech_01", "tech_02", "tech_07", "lead"]
LISTED = "tech_01|tech_02|tech_07|lead"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


@pytest.fixture
def world(monkeypatch):
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "catalog", FakeCatalog({
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL"},
        HUB_SYS: {"id": HUB_SYS, "schema": "hub", "connection_type": "REST API"}}))
    state = SimpleNamespace(writes=writes, db=None)

    def build(source=None, stops=None, audits=None, fail=None):
        target = {"stops": (["stop_id", "closed_by"], _rows(LOGINS if stops is None else stops))}
        if audits is not None:
            target["audits"] = (["audit_id", "closed_by"], _rows(audits))
        state.db = RuleCheckDb({
            SRC_SCHEMA: {"shift_notes": (["note_id", "signed_by"],
                                         _rows(["A. Lee", *LOGINS] if source is None else source))},
            TGT_SCHEMA: target,
        }, fail=fail)
        monkeypatch.setattr(onboard, "pg", state.db)
        return state.db

    state.build = build
    return state


def _rule(**over) -> dict:
    base = {"concept": "operator", "source_system": SRC_SYS,
            "source_field": "shift_notes.signed_by", "source_pattern": LISTED,
            "target_system": TGT_SYS, "target_field": "stops.closed_by",
            "target_pattern": LISTED, "confidence": 0.8,
            "reasoning": "4 shared values measured"}
    base.update(over)
    return base


def _run(*rules) -> onboard._ProvisionState:
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=SRC_SYS, system_name="Shift Log",
        plant="", schema=SRC_SCHEMA, rules=list(rules),
        mappings=[{"table": "shift_notes", "field": "shift_notes.signed_by",
                   "concept": "operator"}],
        connection_type="PostgreSQL", cm_items=[], committable=set(),
        tables_seen={"shift_notes"}, rules_from_checkpoint=True,  # the wizard's rules,
        checkpoint_rule_ids=frozenset(map(id, rules)))  # in its Resolve result
    onboard._dp_identity_rules(ctx)
    return ctx


def _written(writes) -> list:
    return [(c, p) for c, p in writes if c.startswith("MERGE (ir:IdentityRule")]


def _reasons(ctx) -> list:
    return [r["reason"] for r in ctx.rules_rejected]


def _unchecked(ctx) -> list:
    return [r["reason"] for r in getattr(ctx, "rules_unchecked", [])]


# ── 1. a direct match needs a named value in BOTH columns ───────────────────

def test_a_direct_match_with_no_named_value_in_the_target_is_rejected(world):
    """REPRODUCTION (finding 1). The source holds ``tech_01``; the target holds
    neither named value. 040f8b62 wrote the rule: it read only the source."""
    world.build(stops=["ops_1", "ops_2"])
    ctx = _run(_rule(source_pattern="tech_01|zz_9", target_pattern="tech_01|zz_9"))
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "none of which occur in" in reason and "stops.closed_by" in reason


def test_named_values_found_on_different_sides_are_not_a_shared_value(world):
    """REPRODUCTION (finding 1). ``tech_01`` is only in the source and
    ``ops_1`` only in the target: no value joins the two columns."""
    world.build(stops=["ops_1"])
    ctx = _run(_rule(source_pattern="tech_01|ops_1", target_pattern="tech_01|ops_1"))
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "none in both" in reason


def test_the_target_table_the_rule_names_is_the_one_read(world):
    """REPRODUCTION (finding 1). Two target tables hold a ``closed_by``
    column; the rule names ``stops``, where the value is not. The named table
    is read even though the target check rewrites the field to its column."""
    world.build(stops=["ops_1"], audits=["tech_01"])
    ctx = _run(_rule(source_pattern="tech_01", target_pattern="tech_01"))
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "stops.closed_by" in reason


def test_a_direct_match_proven_on_both_sides_is_stored_as_a_direct_join(world):
    """REPRODUCTION (findings 1 and 4). Written; both stored patterns are
    empty (040f8b62 stored the value list), the join reads as ``direct``, and
    the values found in both columns are in ``origin_context``."""
    world.build()
    rule = _rule()
    ctx = _run(rule)
    assert _reasons(ctx) == [] and _unchecked(ctx) == []
    ((cypher, params),) = _written(world.writes)
    assert (params["src_pat"], params["tgt_pat"]) == ("", "")
    assert onboard._rule_joinpath_provenance(rule)[0] == "direct"
    assert "ir.origin_context = $octx" in cypher
    assert "4 of the 4 values" in params["octx"]
    assert all(v in params["octx"] for v in LOGINS)
    assert "Identity Rules checkpoint" in params["octx"]


def test_the_direct_rule_keeps_the_onboarding_stamp_shape(world):
    """Guard (module 7 query 3b): the MERGE sets no method, so the onboarding
    stamp still gives ``onboarding-llm``; the confidence is the 0.7 cap."""
    world.build()
    _run(_rule())
    ((cypher, params),) = _written(world.writes)
    assert "ir.method" not in cypher
    assert (params["concept"], params["conf"]) == ("operator", 0.7)


def test_the_onboarding_stamp_keeps_an_origin_context_the_provision_wrote(monkeypatch):
    """REPRODUCTION (finding 4). The stamp replaced every unstamped rule's
    ``origin_context``, so the re-check evidence would be lost."""
    calls = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: calls.append((c, p or {})))
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    ((cypher, params),) = calls
    assert "ir.method IS NULL" in cypher
    assert "ir.origin_context = coalesce(ir.origin_context, $origin_context)" in cypher
    assert "Identity Rules checkpoint" in params["origin_context"]


def test_a_bare_star_is_written_unchecked_and_counted(world):
    """REPRODUCTION (findings 1, 3, 4). ``*`` names no value: written with
    the cap, stored as a direct join, and counted as not re-checked."""
    world.build()
    ctx = _run(_rule(source_pattern="*", target_pattern="*"))
    ((_cypher, params),) = _written(world.writes)
    assert (params["src_pat"], params["tgt_pat"], params["conf"]) == ("", "", 0.7)
    (reason,) = _unchecked(ctx)
    assert "names no value" in reason


def test_a_target_that_is_not_postgresql_leaves_the_rule_unchecked_and_counted(world):
    """REPRODUCTION (finding 3). The target column cannot be read: the rule is
    written as before, and the summary now says it was not re-checked."""
    world.build()
    ctx = _run(_rule(target_system=HUB_SYS, target_field="stops.closed_by"))
    assert len(_written(world.writes)) == 1
    (reason,) = _unchecked(ctx)
    assert "REST API" in reason


# ── 2. one grammar: transform_id's ──────────────────────────────────────────

@pytest.mark.parametrize("src,tgt,values", [
    ("LINE[0-9]+-{id}", "L-{id}", ["ALPHA-1", "LINE7-42"]),
    ("WO\\.{id}", "W-{id}", ["AAA", "WO.5"]),
])
def test_regex_looking_literal_text_is_literal_text(world, src, tgt, values):
    """0657e3dc wrote these, reading the literal text the way its resolver
    did (as a regular expression). Both runtime readers now read literal
    text literally, so these patterns fit none of the values (no value holds
    ``[0-9]+`` or a backslash) and the sample check rejects them; the
    resolver no longer translates LINE7-42 with it."""
    world.build(source=values)
    assert resolve.transform_id("LINE[0-9]+-{id}", "L-{id}", "LINE7-42") is None
    ctx = _run(_rule(source_pattern=src, target_pattern=tgt))
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "fits it" in reason


@pytest.mark.parametrize("values,written", [(["AXBY9", "A%B_7"], True), (["AXBY9"], False)])
def test_literal_percent_and_underscore_stay_literal(world, values, written):
    """Guard (040f8b62 already escaped them): ``%`` and ``_`` mean nothing to
    ``transform_id``, so ``AXBY9`` never fits ``A%B_{id}``."""
    world.build(source=values)
    ctx = _run(_rule(source_pattern="A%B_{id}", target_pattern="T-{id}"))
    assert (len(_written(world.writes)) == 1) is written
    assert (_reasons(ctx) == []) is written


@pytest.mark.parametrize("pattern,value", [
    ("A{id}-{id}", "A1-1"),     # the same placeholder twice: does not compile
])
def test_a_malformed_pattern_is_rejected_with_a_reason_never_raised(world, pattern, value):
    """REPRODUCTION (finding 2). 040f8b62 raised ``re.error`` from
    ``transform_id`` on the value its prefilter selected."""
    world.build(source=[value])
    ctx = _run(_rule(source_pattern=pattern, target_pattern="T-{id}"))
    assert _written(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "is not a valid pattern" in reason


def test_transform_id_returns_none_for_a_malformed_pattern():
    """REPRODUCTION (finding 2): the resolver's matcher no longer raises on a
    stored malformed pattern; a valid one translates as before. A bracket is
    literal text (it no longer opens a character set), so ``X[{id}`` is a
    valid pattern; a placeholder named twice is what does not compile."""
    assert resolve.transform_id("A{id}-{id}", "Y-{id}", "A1-1") is None
    assert resolve.transform_id("X[{id}", "Y-{id}", "X[5") == "Y-5"
    assert resolve.transform_id("B-{id}", "LOT-2026-0{id}", "B-1234") == "LOT-2026-01234"


# ── 3. bounded reads, short deadlines, no silent acceptance ────────────────

_SOURCE_READ = lambda schema, sql: schema == SRC_SCHEMA and "information_schema" not in sql  # noqa: E731
_TARGET_READ = lambda schema, sql: schema == TGT_SCHEMA and "information_schema" not in sql  # noqa: E731


@pytest.mark.parametrize("rule,fail", [
    (_rule(), _SOURCE_READ),
    (_rule(), _TARGET_READ),
    (_rule(source_pattern="tech_{n}", target_pattern="T-{n}"), _SOURCE_READ),
], ids=["direct-source", "direct-target", "transform"])
def test_a_check_that_cannot_read_writes_the_rule_unchecked_and_says_so(world, caplog, rule, fail):
    """REPRODUCTION (finding 3). A read that times out or fails leaves the
    rule written as before (0.7 cap), now logged and counted."""
    world.build(fail=fail)
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ctx = _run(rule)
    ((_cypher, params),) = _written(world.writes)
    assert params["conf"] == 0.7
    (reason,) = _unchecked(ctx)
    assert "could not be read" in reason and "QueryCanceled" in reason
    lines = [r.getMessage() for r in caplog.records if "IdentityRule written unchecked" in r.getMessage()]
    assert len(lines) == 1 and reason in lines[0]


def _levels(sql: str) -> list:
    """Each parenthesized level of ``sql`` with its own nested levels taken
    out, innermost first, the outermost statement last."""
    levels = []
    while (m := re.search(r"\(([^()]*)\)", sql)):
        levels.append(m.group(1))
        sql = sql[:m.start()] + " " + sql[m.end():]
    return levels + [sql]


def test_every_check_read_is_bounded_and_has_a_short_deadline(world):
    """REPRODUCTION (finding 3). No read sorts a whole table: a level that
    scans a table carries a LIMIT and no ORDER BY, or is a per-value EXISTS
    probe. The named values are looked up by equality on the column (an index
    applies) with the values bound, and every read passes a deadline below
    pg's default."""
    db = world.build()
    _run(_rule(), _rule(source_pattern="tech_{n}", target_pattern="T-{n}"))
    reads = db.reads_of("shift_notes") + db.reads_of("stops")
    assert reads
    for _schema, sql, params, kw in reads:
        for level in _levels(sql):
            if re.search(r"\bFROM\s+(shift_notes|stops)\b", level):
                assert "ORDER BY" not in level, sql
                assert re.search(r"\bLIMIT\s+\d+", level) or level.strip().startswith("SELECT 1"), sql
        assert 0 < (kw.get("statement_timeout_ms") or 0) <= 5000, sql
        assert "tech_01" not in sql
    lookups = [c for c in reads if "tech_01" in (c[2] or ())]
    assert lookups and all(re.search(r"WHERE\s+(signed_by|closed_by)\s*=", c[1]) for c in lookups)


def test_the_provision_summary_line_counts_unchecked_rules(world, caplog):
    """REPRODUCTION (finding 3): ``PROVISION_SUMMARY`` says how many rules were
    written without the re-check."""
    world.build()
    ctx = _run(_rule(source_pattern="*", target_pattern="*"))
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        onboard._log_provision_summary(ctx, [])
    (line,) = [r.getMessage() for r in caplog.records if "PROVISION_SUMMARY" in r.getMessage()]
    assert "identity_rules_written=1 identity_rules_rejected=0 identity_rules_unchecked=1" in line


def test_the_provision_result_carries_the_unchecked_rules(world, monkeypatch):
    """REPRODUCTION (finding 3): the result the wizard reads names them."""
    world.build()
    ctx = _run(_rule(source_pattern="*", target_pattern="*"))
    monkeypatch.setattr(onboard, "_dp_written_artifacts", lambda _sid: ([], []))
    monkeypatch.setattr(onboard, "_dp_reapply_self_learned", lambda _sid: 0)
    monkeypatch.setattr(onboard, "_dp_mapped_field_count", lambda _sid: None)
    result = onboard._dp_result(ctx)
    assert result.get("identity_rules_unchecked") == 1
    (detail,) = result.get("unchecked_details") or [None]
    assert "names no value" in detail["reason"]
