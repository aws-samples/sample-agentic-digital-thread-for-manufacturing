# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision reads no source for an identity rule that carries no measured
method, and counts it unchecked.

The live pattern check reads only a PostgreSQL source with both patterns set,
so a model rule from any other source (or with no pattern) is written with no
check. It is written as before and counted once in ``rules_unchecked``, with
the reason that provision did not confirm it with live values, so
``PROVISION_SUMMARY`` shows it; it is not sampled or membership-probed here.
Provision never trusts a request's own ``value_check``: it cannot tell a
genuine advisory from one a model wrote into the request, so every
method-less rule is counted with the one generic plain reason, whatever the
request holds. A rule whose pair overlaps keeps its id, confidence and
JoinPath. A rule sent with a measured method is earned by its own
re-measurement and is not counted here.

Synthetic fixture: a REST hub ``cell-hub`` (``jobs.ext_lot_code``), an unsited
PostgreSQL ``ledger`` (``lines.lot_ref``) holding the same lot codes, and two
sited PostgreSQL systems ``plant-north-ops`` / ``plant-south-ops``
(``batches.lot_code``) whose lot codes are their own. SQLite stands in for
PostgreSQL.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from tests.unit._rule_check_db import RuleCheckDb, measured_as_sent  # noqa: E402

HUB, LEDGER, NORTH, SOUTH = "cell-hub", "ledger", "plant-north-ops", "plant-south-ops"
CONCEPT = "lot-number"


def sentence(asked: int, other: int) -> str:
    """A sentence a model might copy into a rule's ``value_check`` note;
    provision must never surface it."""
    return (f"No shared value in the sample: none of the {asked} and {other} "
            "values sampled from the two columns match")


def flagged(rule: dict, asked: int, other: int) -> dict:
    """``rule`` with a MODEL-WRITTEN ``value_check`` the sanitizer would strip
    on the resolve path; here it is passed straight to provision to prove
    provision ignores a request's ``value_check``."""
    return {**rule, "value_check": {"result": "no_shared_value", "asked": asked,
                                    "other": other, "note": sentence(asked, other)}}

HUB_LOTS = [f"L-9{n:03d}" for n in range(1, 21)]
NORTH_LOTS = [f"L-1{n:03d}" for n in range(1, 21)]
SOUTH_LOTS = [f"L-5{n:03d}" for n in range(1, 21)]

#: (system, table, column) -> every value of the column.
COLUMNS = {
    (HUB, "jobs", "ext_lot_code"): HUB_LOTS,
    (LEDGER, "lines", "lot_ref"): HUB_LOTS,
    (NORTH, "batches", "lot_code"): NORTH_LOTS,
    (SOUTH, "batches", "lot_code"): SOUTH_LOTS,
}

ENTRIES = {
    HUB: {"id": HUB, "name": "Cell Hub", "plant": "", "schema": "hub", "connection_type": "REST API"},
    LEDGER: {"id": LEDGER, "name": "Ledger", "plant": "", "schema": "ledger_db",
             "connection_type": "PostgreSQL"},
    NORTH: {"id": NORTH, "name": "North Ops", "plant": "Plant North", "schema": "north_db",
            "connection_type": "PostgreSQL"},
    SOUTH: {"id": SOUTH, "name": "South Ops", "plant": "Plant South", "schema": "south_db",
            "connection_type": "PostgreSQL"},
}

TARGET_FIELD = {LEDGER: "lines.lot_ref", NORTH: "batches.lot_code", SOUTH: "batches.lot_code"}


class _Catalog:
    def get(self, sid):
        return ENTRIES.get(sid)

    def get_all(self):
        return list(ENTRIES.values())

    def get_onboarded_ids(self):
        return set(ENTRIES)


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


def _database(extra: dict | None = None) -> RuleCheckDb:
    return RuleCheckDb({
        "ledger_db": {"lines": (["line_id", "lot_ref"], _rows(HUB_LOTS))},
        "north_db": {"batches": (["batch_id", "lot_code"], _rows(NORTH_LOTS))},
        "south_db": {"batches": (["batch_id", "lot_code"], _rows(SOUTH_LOTS))},
        **(extra or {}),
    })


class _Reads:
    """Every ``resolve_evidence`` source read a provision could make for an
    identity rule, recorded and passed through to the real function: the
    sampler, the exact pass's probe and its completeness twin, and the
    backend sample under the sampler."""

    NAMES = ("sample_field_values", "probe_field_values", "_sample_backend")

    def __init__(self, monkeypatch):
        import agents.resolve_evidence as re_mod
        self.calls: list = []
        for name in self.NAMES:
            real = getattr(re_mod, name)

            def spy(*args, _name=name, _real=real, **kw):
                self.calls.append((_name, kw.get("system_id", args[0] if args else None)))
                return _real(*args, **kw)
            monkeypatch.setattr(re_mod, name, spy)


@pytest.fixture
def world(monkeypatch):
    from agents import self_learning
    writes: list = []
    db = _database()
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))

    def graph(cypher, params=None):
        params = params or {}
        if "RETURN se.name AS tbl" in cypher:
            target = (params.get("tsid"), params.get("fname"))
            return [{"tbl": t} for (s, t, c) in COLUMNS if (s, c) == target]
        return []

    monkeypatch.setattr(onboard, "neptune_query", graph)
    monkeypatch.setattr(onboard, "get_name", lambda sid: (ENTRIES.get(sid) or {}).get("name", sid))
    monkeypatch.setattr(onboard, "catalog", _Catalog())
    monkeypatch.setattr(onboard, "pg", db)
    monkeypatch.setattr("agents.db.pg", db)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: list(ENTRIES.values()))
    return SimpleNamespace(writes=writes, db=db, reads=_Reads(monkeypatch))


def _rule(target: str, source_field: str = "jobs.ext_lot_code", **over) -> dict:
    base = {"concept": CONCEPT, "source_field": source_field, "source_pattern": "",
            "target_system": target, "target_field": TARGET_FIELD[target], "target_pattern": "",
            "confidence": 0.96, "reasoning": "both fields carry the lot code"}
    base.update(over)
    return base


def _ctx(*rules, system_id=HUB, schema="hub", connection_type="REST API",
         mapping=("jobs", "jobs.ext_lot_code")) -> onboard._ProvisionState:
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=system_id, system_name=system_id,
        plant="", schema=schema, rules=list(rules),
        mappings=[{"table": mapping[0], "field": mapping[1], "concept": CONCEPT}],
        connection_type=connection_type, cm_items=[], committable=set(),
        tables_seen={mapping[0]}, rules_from_checkpoint=True,
        checkpoint_rule_ids=frozenset(map(id, rules)))  # in the Resolve result


def _provision(ctx) -> onboard._ProvisionState:
    onboard._dp_identity_rules(ctx)
    onboard._dp_rule_joinpaths(ctx)
    return ctx


def _identity_rules(writes) -> dict:
    """target system -> the IdentityRule MERGE's parameters."""
    return {p["tgt_sys"]: p for c, p in writes if c.startswith("MERGE (ir:IdentityRule")}


def _join_paths(writes) -> dict:
    """target system -> the JoinPath MERGE's parameters."""
    return {p["tsys"]: p for c, p in writes if c.startswith("MERGE (jp:JoinPath")}


def _unmeasured(target: str, source: str = "jobs.ext_lot_code") -> str:
    return (f"provision did not confirm this rule with live values ({source} and {target} "
            f"{TARGET_FIELD[target]})")


#: The SQL the provision writer runs for a written rule whatever its method,
#: and that this check leaves as it was: the target column's existence
#: (``information_schema``) and the JoinPath writer's five-value format check
#: (``_validate_joinpath_formats``).
_WRITER_SQL = ("FROM information_schema.columns", "IS NOT NULL LIMIT 5")


def _source_reads(world) -> list:
    """Every read provision made of a rule's columns beyond the writer's own
    SQL: a ``resolve_evidence`` sample or probe, or any other statement (a
    membership ``= ANY`` probe, a sample)."""
    sql = [c[1] for c in world.db.calls if not any(w in c[1] for w in _WRITER_SQL)]
    return world.reads.calls + sql


def test_a_methodless_rule_from_a_rest_source_is_written_unchecked_without_a_read(world, monkeypatch,
                                                                                   caplog):
    """The hub is not PostgreSQL, so the pattern check cannot read it. Its
    method-less rule to the north plant is written with its JoinPath,
    rejected by nothing, and counted once in ``rules_unchecked`` with the
    plain reason; no column is sampled or probed for it, and the summary
    line counts it. A rule sent with a measured method (the south
    exact-agreement rule, earned by its own re-measurement) is written as
    before and is not counted."""
    measured_as_sent(monkeypatch)
    exact = _rule(SOUTH, method="value_exact_match", confidence=0.85, shared_count=4,
                  reasoning="4 values present exactly in both columns (membership-probed)")
    ctx = _provision(_ctx(_rule(NORTH), exact))
    assert ctx.rules_rejected == []
    assert _source_reads(world) == []
    assert set(_identity_rules(world.writes)) == {NORTH, SOUTH}
    assert set(_join_paths(world.writes)) == {NORTH, SOUTH}
    assert [(e["rule"]["target_system"], e["reason"]) for e in ctx.rules_unchecked] == [
        (NORTH, _unmeasured(NORTH))]
    north = _identity_rules(world.writes)[NORTH]
    assert north["octx"].endswith(f"not re-checked at provision: {_unmeasured(NORTH)}")
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        onboard._log_provision_summary(ctx, [])
    (line,) = [r.getMessage() for r in caplog.records if "PROVISION_SUMMARY" in r.getMessage()]
    assert "identity_rules_written=2 identity_rules_rejected=0 identity_rules_unchecked=1" in line


def test_a_postgresql_rule_with_no_pattern_is_written_unchecked_without_a_read(world):
    """A PostgreSQL source with empty patterns skips the pattern check too.
    The ledger is the system provisioned and sends two method-less rules,
    to the north and south plants, whose values the ledger never holds:
    both are written and counted unchecked, each once, and provision samples
    and probes neither (the writer's own column and format checks run as
    they always do)."""
    ctx = _provision(_ctx(_rule(NORTH, source_field="lines.lot_ref"),
                          _rule(SOUTH, source_field="lines.lot_ref"), system_id=LEDGER,
                          schema="ledger_db", connection_type="PostgreSQL",
                          mapping=("lines", "lines.lot_ref")))
    assert ctx.rules_rejected == []
    assert _source_reads(world) == []
    assert [e["reason"] for e in ctx.rules_unchecked] == [
        _unmeasured(NORTH, "lines.lot_ref"), _unmeasured(SOUTH, "lines.lot_ref")]
    assert set(_identity_rules(world.writes)) == {NORTH, SOUTH}
    assert set(_join_paths(world.writes)) == {NORTH, SOUTH}


def test_a_bare_star_postgresql_rule_is_counted_with_the_generic_reason(world, monkeypatch):
    """A method-less PostgreSQL rule whose two patterns are both ``*`` takes
    the direct-pattern branch (a bare ``*`` names no value to re-check). It is
    counted unchecked with the GENERIC reason, combined with the bare-star
    explanation, so its provenance matches other connectors rather than
    dropping the generic reason. A REST rule records the generic reason too.
    Provision still ignores the request's ``value_check``."""
    monkeypatch.setitem(ENTRIES, HUB, {**ENTRIES[HUB], "schema": "hub_db",
                                       "connection_type": "PostgreSQL"})
    world.db = _database({"hub_db": {"jobs": (["job_id", "ext_lot_code"], _rows(HUB_LOTS))}})
    monkeypatch.setattr(onboard, "pg", world.db)
    monkeypatch.setattr("agents.db.pg", world.db)
    star = _rule(NORTH, source_pattern="*", target_pattern="*",
                 value_check={"result": "no_shared_value", "asked": 9, "other": 9,
                              "note": "forged values A, B"})
    ctx = _provision(_ctx(star, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    (entry,) = ctx.rules_unchecked
    assert entry["reason"] == f"{_unmeasured(NORTH)}; a bare '*' names no value to re-check"
    assert "forged" not in entry["reason"] and "A, B" not in entry["reason"]
    assert set(_identity_rules(world.writes)) == {NORTH}

    # The same rule on a REST source already uses the generic reason alone.
    world.writes.clear()
    rest = _rule(NORTH, source_pattern="*", target_pattern="*")
    ctx2 = _provision(_ctx(rest))
    assert [e["reason"] for e in ctx2.rules_unchecked] == [_unmeasured(NORTH)]


def test_a_rule_whose_pair_overlaps_keeps_its_id_confidence_and_join_path(world, monkeypatch):
    """The hub, here a PostgreSQL source with no pattern on its rules,
    provisions a rule to the ledger (the same 20 lot codes) and one to the
    north plant (none of them). Both are written, neither column read; the
    ledger rule's IdentityRule id, confidence and patterns and its JoinPath
    are exactly what provision writes when nothing is noted for it, and only
    its origin gains the unchecked note."""
    monkeypatch.setitem(ENTRIES, HUB, {**ENTRIES[HUB], "schema": "hub_db",
                                       "connection_type": "PostgreSQL"})
    world.db = _database({"hub_db": {"jobs": (["job_id", "ext_lot_code"], _rows(HUB_LOTS))}})
    monkeypatch.setattr(onboard, "pg", world.db)
    monkeypatch.setattr("agents.db.pg", world.db)

    def hub_ctx():
        return _ctx(_rule(LEDGER), _rule(NORTH), schema="hub_db", connection_type="PostgreSQL")
    noted = _provision(hub_ctx())
    assert noted.rules_rejected == []
    assert set(_identity_rules(world.writes)) == {LEDGER, NORTH}
    assert _source_reads(world) == []
    with_note = list(world.writes)
    world.writes.clear()
    monkeypatch.setattr(onboard, "_dp_unmeasured_reason", lambda *_a, **_k: "")
    plain = _provision(hub_ctx())
    without_note = list(world.writes)

    assert plain.rules_unchecked == []
    kept, before = _identity_rules(with_note)[LEDGER], _identity_rules(without_note)[LEDGER]
    assert {k: v for k, v in kept.items() if k != "octx"} == \
        {k: v for k, v in before.items() if k != "octx"}
    assert kept["conf"] == before["conf"] == 0.35
    assert _join_paths(with_note) == _join_paths(without_note)
    assert kept["octx"] == f"{before['octx']}; not re-checked at provision: {_unmeasured(LEDGER)}"


def test_a_postgresql_transform_rule_whose_pattern_fits_is_written_and_counted_once(world,
                                                                                  monkeypatch):
    """The hub, a PostgreSQL source, sends a method-less transform rule
    (``L-{n}`` -> ``LOT-{n}``) to the north plant. The pattern check reads a
    hub value that fits and finds nothing in the target column to disagree
    on, so it passes: the rule is written exactly as when nothing is noted
    for it, and its JoinPath with it. The check validated the pattern on one
    column and showed no value of it in the other, so the rule adds exactly
    one unchecked entry, with the plain reason, and its origin keeps what the
    check found. With no measured method the fit does not lift the 0.7
    ceiling (2026-10-28): the model's 0.96 is written as 0.7."""
    monkeypatch.setitem(ENTRIES, HUB, {**ENTRIES[HUB], "schema": "hub_db",
                                       "connection_type": "PostgreSQL"})
    world.db = _database({"hub_db": {"jobs": (["job_id", "ext_lot_code"], _rows(HUB_LOTS))}})
    monkeypatch.setattr(onboard, "pg", world.db)
    monkeypatch.setattr("agents.db.pg", world.db)

    def hub_ctx():
        return _ctx(_rule(NORTH, source_pattern="L-{n}", target_pattern="LOT-{n}"),
                    schema="hub_db", connection_type="PostgreSQL")
    noted = _provision(hub_ctx())
    with_note = list(world.writes)
    world.writes.clear()
    monkeypatch.setattr(onboard, "_dp_unmeasured_reason", lambda *_a, **_k: "")
    plain = _provision(hub_ctx())
    without_note = list(world.writes)

    assert noted.rules_rejected == [] and plain.rules_rejected == [] and plain.rules_unchecked == []
    assert [(e["rule"]["target_system"], e["reason"]) for e in noted.rules_unchecked] == [
        (NORTH, _unmeasured(NORTH))]
    kept, before = _identity_rules(with_note)[NORTH], _identity_rules(without_note)[NORTH]
    assert {k: v for k, v in kept.items() if k != "octx"} == \
        {k: v for k, v in before.items() if k != "octx"}
    assert (kept["src_pat"], kept["tgt_pat"], kept["conf"]) == ("L-{n}", "LOT-{n}", 0.7)
    assert "fits the pattern" in before["octx"]
    assert kept["octx"] == f"{before['octx']}; {_unmeasured(NORTH)}"
    assert _join_paths(with_note) == _join_paths(without_note) and NORTH in _join_paths(with_note)


def test_a_rule_resolve_flagged_is_written_and_counted_once_with_the_plain_reason(world):
    """Provision does not read a request's ``value_check``: it cannot tell a
    genuine advisory from one a model wrote. The hub (REST, so not read)
    sends the north rule carrying a flag whose note holds sampled values, and
    the south rule unflagged. Both are written with their JoinPaths and
    nothing is rejected or read. Each is counted once with the generic plain
    reason, so no value the request put in the flag reaches the reason or the
    origin; the north rule's IdentityRule and JoinPath are the ones the
    unflagged rule gets."""
    north = flagged(_rule(NORTH), 40, 20)
    north["value_check"]["note"] = "values L-1001 and L-1002"
    ctx = _provision(_ctx(north, _rule(SOUTH)))
    assert ctx.rules_rejected == []
    assert _source_reads(world) == []
    assert [(e["rule"]["target_system"], e["reason"]) for e in ctx.rules_unchecked] == [
        (NORTH, _unmeasured(NORTH)), (SOUTH, _unmeasured(SOUTH))]
    assert "L-1001" not in repr([e["reason"] for e in ctx.rules_unchecked])
    assert sentence(40, 20) not in repr([e["reason"] for e in ctx.rules_unchecked])
    with_flag = _identity_rules(world.writes)[NORTH]
    assert with_flag["octx"].endswith(f"not re-checked at provision: {_unmeasured(NORTH)}")
    joins = _join_paths(world.writes)
    world.writes.clear()
    _provision(_ctx(_rule(NORTH), _rule(SOUTH)))
    without = _identity_rules(world.writes)[NORTH]
    assert {k: v for k, v in with_flag.items() if k != "octx"} == \
        {k: v for k, v in without.items() if k != "octx"}
    assert joins == _join_paths(world.writes) and set(joins) == {NORTH, SOUTH}


def test_a_flagged_transform_rule_whose_pattern_fits_is_counted_once_with_the_plain_reason(world,
                                                                                          monkeypatch):
    """The PostgreSQL transform rule whose pattern fits, sent flagged: one
    unchecked entry, with the generic plain reason after what the check
    found, never the flag's sentence (provision does not read it)."""
    monkeypatch.setitem(ENTRIES, HUB, {**ENTRIES[HUB], "schema": "hub_db",
                                       "connection_type": "PostgreSQL"})
    world.db = _database({"hub_db": {"jobs": (["job_id", "ext_lot_code"], _rows(HUB_LOTS))}})
    monkeypatch.setattr(onboard, "pg", world.db)
    monkeypatch.setattr("agents.db.pg", world.db)
    rule = flagged(_rule(NORTH, source_pattern="L-{n}", target_pattern="LOT-{n}"), 40, 20)
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    assert [e["reason"] for e in ctx.rules_unchecked] == [_unmeasured(NORTH)]
    octx = _identity_rules(world.writes)[NORTH]["octx"]
    assert "fits the pattern" in octx and octx.endswith(f"; {_unmeasured(NORTH)}")


@pytest.mark.parametrize("check", [
    {"result": "shared_value", "asked": 40},
    {"result": "no_shared_value", "asked": "40"},
    {"result": "no_shared_value", "asked": True},
    {"result": "no_shared_value", "asked": 2},
    {"result": "no_shared_value"},
    "no_shared_value",
], ids=["other-result", "count-as-text", "count-as-bool", "too-few", "no-count", "not-a-dict"])
def test_a_flag_that_is_not_one_gives_the_plain_reason(world, check):
    ctx = _provision(_ctx({**_rule(NORTH), "value_check": check}))
    assert [e["reason"] for e in ctx.rules_unchecked] == [_unmeasured(NORTH)]


# ── a measured method that fails re-measurement is demoted and counted ──────

def _pg_hub(world, monkeypatch):
    """Make the hub a PostgreSQL source so the pattern check reads it."""
    monkeypatch.setitem(ENTRIES, HUB, {**ENTRIES[HUB], "schema": "hub_db",
                                       "connection_type": "PostgreSQL"})
    world.db = _database({"hub_db": {"jobs": (["job_id", "ext_lot_code"], _rows(HUB_LOTS))}})
    monkeypatch.setattr(onboard, "pg", world.db)
    monkeypatch.setattr("agents.db.pg", world.db)


def _demote(monkeypatch):
    """Provision's own re-measurement fails: the claim is not earned, the
    method is stripped (``onboard._remeasured_rule``)."""
    monkeypatch.setattr(onboard, "_remeasured_rule",
                        lambda _ctx, _rule, _table: (None, "found no shared values"))


def test_a_demoted_value_core_match_with_a_transform_fit_is_counted_unchecked(world, monkeypatch):
    """A ``value_core_match`` claim whose re-measurement fails is demoted to
    method-less. Its transform pattern (``L-{n}`` -> ``LOT-{n}``) fits one
    source value, which validates the grammar on one column only, not a value
    in both. The rule is still written (not dropped), its method stripped, and
    counted once unchecked with the generic reason. The generic reason is
    decided after the demotion, not from the method the request arrived with."""
    _pg_hub(world, monkeypatch)
    _demote(monkeypatch)
    rule = _rule(NORTH, method="value_core_match", shared_count=20,
                 source_pattern="L-{n}", target_pattern="LOT-{n}")
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    assert [(e["rule"]["target_system"], e["reason"]) for e in ctx.rules_unchecked] == [
        (NORTH, _unmeasured(NORTH))]
    written = _identity_rules(world.writes)[NORTH]
    assert written["method"] == ""  # demoted: the request method is not written
    octx = written["octx"]
    assert "re-measurement found no shared values" in octx
    assert octx.endswith(f"; {_unmeasured(NORTH)}")


def test_a_demoted_value_exact_match_is_counted_unchecked(world, monkeypatch):
    """A demoted ``value_exact_match`` claim with a transform fit is written
    method-less and counted unchecked with the generic reason."""
    _pg_hub(world, monkeypatch)
    _demote(monkeypatch)
    rule = _rule(NORTH, method="value_exact_match", shared_count=20,
                 source_pattern="L-{n}", target_pattern="LOT-{n}")
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    assert [e["reason"] for e in ctx.rules_unchecked] == [_unmeasured(NORTH)]
    assert _identity_rules(world.writes)[NORTH]["method"] == ""


def test_a_demoted_claim_with_an_empty_pattern_is_counted_unchecked(world, monkeypatch):
    """A demoted claim with empty patterns has no live check at all: it is
    written method-less and counted unchecked once with the generic reason."""
    _pg_hub(world, monkeypatch)
    _demote(monkeypatch)
    rule = _rule(NORTH, method="value_core_match", shared_count=20)  # empty patterns
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    # One entry, combining the demotion note and the generic reason.
    assert len(ctx.rules_unchecked) == 1
    (reason,) = [e["reason"] for e in ctx.rules_unchecked]
    assert _unmeasured(NORTH) in reason and "re-measurement found no shared values" in reason
    assert NORTH in _identity_rules(world.writes)  # still written
    assert _identity_rules(world.writes)[NORTH]["method"] == ""


def test_a_claim_that_re_measures_successfully_stays_checked_and_keeps_its_method(world, monkeypatch):
    """A ``value_core_match`` claim that provision's re-measurement rebuilds
    keeps its method and is NOT counted unchecked: it earned the measurement,
    so the generic reason is not added."""
    _pg_hub(world, monkeypatch)
    measured_as_sent(monkeypatch)  # re-measurement rebuilds the rule as sent
    rule = _rule(NORTH, method="value_core_match", shared_count=20,
                 source_pattern="L-{n}", target_pattern="LOT-{n}")
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    assert ctx.rules_unchecked == []
    assert _identity_rules(world.writes)[NORTH]["method"] == "value_core_match"


# ── a check that cannot run keeps the generic reason too ─────────────────────

def _rest_north(monkeypatch):
    """Make the north plant a REST system, so no check can read its column."""
    monkeypatch.setitem(ENTRIES, NORTH, {**ENTRIES[NORTH], "connection_type": "REST API"})


def _one_unchecked_reason(ctx) -> str:
    """The reason of the ONE unchecked entry ``ctx`` holds."""
    (entry,) = ctx.rules_unchecked
    return entry["reason"]


def test_a_methodless_transform_rule_to_a_rest_target_keeps_the_generic_reason(world, monkeypatch):
    """A method-less PostgreSQL rule to a REST target: the transform fits a
    source value, but the target column cannot be read. The rule is written
    and counted once, with the REST reason AND the generic reason, since no
    value was shown in both columns."""
    _pg_hub(world, monkeypatch)
    _rest_north(monkeypatch)
    rule = _rule(NORTH, source_pattern="L-{n}", target_pattern="LOT-{n}")
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    reason = _one_unchecked_reason(ctx)
    assert "is a REST API system" in reason and _unmeasured(NORTH) in reason
    assert NORTH in _identity_rules(world.writes)


def test_a_demoted_transform_claim_to_a_rest_target_keeps_the_generic_reason(world, monkeypatch):
    """The same rule sent as ``value_core_match``, whose re-measurement
    fails: demoted, written method-less, and counted once with the REST
    reason and the generic reason."""
    _pg_hub(world, monkeypatch)
    _rest_north(monkeypatch)
    _demote(monkeypatch)
    rule = _rule(NORTH, method="value_core_match", shared_count=20,
                 source_pattern="L-{n}", target_pattern="LOT-{n}")
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    reason = _one_unchecked_reason(ctx)
    assert "is a REST API system" in reason and _unmeasured(NORTH) in reason
    assert _identity_rules(world.writes)[NORTH]["method"] == ""


def test_a_methodless_direct_rule_to_a_rest_target_keeps_the_generic_reason(world, monkeypatch):
    """A method-less direct rule whose value is in the PostgreSQL source but
    whose REST target cannot be read is written as a direct join and counted
    once with the generic reason as well as the open side's reason."""
    _pg_hub(world, monkeypatch)
    _rest_north(monkeypatch)
    rule = _rule(NORTH, source_pattern=HUB_LOTS[0], target_pattern=HUB_LOTS[0])
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    reason = _one_unchecked_reason(ctx)
    assert _unmeasured(NORTH) in reason and reason != _unmeasured(NORTH)
    assert NORTH in _identity_rules(world.writes)


def test_a_methodless_rule_whose_target_catalog_cannot_be_read_keeps_the_generic_reason(world,
                                                                                         monkeypatch):
    """A method-less rule whose PostgreSQL target catalog read fails is
    accepted unverified, written, and counted once with the catalog reason and
    the generic reason."""
    _pg_hub(world, monkeypatch)
    real = onboard._check_read

    def failing_catalog(schema, sql, params=()):
        if "information_schema.columns" in sql:
            return None, "timed out"
        return real(schema, sql, params)

    monkeypatch.setattr(onboard, "_check_read", failing_catalog)
    rule = _rule(NORTH, source_pattern="L-{n}", target_pattern="LOT-{n}")
    ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    assert ctx.rules_rejected == []
    reason = _one_unchecked_reason(ctx)
    assert "could not be read (timed out)" in reason and _unmeasured(NORTH) in reason
    assert NORTH in _identity_rules(world.writes)


def _unchecked_lines(caplog) -> list:
    return [r.getMessage() for r in caplog.records if "IdentityRule written unchecked" in r.getMessage()]


def test_a_demoted_empty_pattern_claim_logs_one_complete_unchecked_line(world, monkeypatch, caplog):
    """A demoted claim with empty patterns is counted unchecked by the
    demotion and again by the missing pattern check. It logs ONE
    ``IdentityRule written unchecked`` line, holding the demotion reason and
    the generic reason together, as its single entry does."""
    _pg_hub(world, monkeypatch)
    _demote(monkeypatch)
    rule = _rule(NORTH, method="value_core_match", shared_count=20)
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    (line,) = _unchecked_lines(caplog)
    reason = _one_unchecked_reason(ctx)
    assert reason in line
    assert "re-measurement found no shared values" in reason and _unmeasured(NORTH) in reason
    assert reason.count(_unmeasured(NORTH)) == 1


@pytest.mark.parametrize("demoted", [False, True], ids=["methodless", "demoted"])
def test_a_rule_to_a_rest_target_logs_one_complete_unchecked_line(world, monkeypatch, caplog, demoted):
    """A transform rule to a REST target logs one unchecked line that holds
    both the REST reason and the generic reason."""
    _pg_hub(world, monkeypatch)
    _rest_north(monkeypatch)
    over = {"method": "value_core_match", "shared_count": 20} if demoted else {}
    if demoted:
        _demote(monkeypatch)
    rule = _rule(NORTH, source_pattern="L-{n}", target_pattern="LOT-{n}", **over)
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ctx = _provision(_ctx(rule, schema="hub_db", connection_type="PostgreSQL"))
    (line,) = _unchecked_lines(caplog)
    reason = _one_unchecked_reason(ctx)
    assert reason in line and "is a REST API system" in reason and _unmeasured(NORTH) in reason
