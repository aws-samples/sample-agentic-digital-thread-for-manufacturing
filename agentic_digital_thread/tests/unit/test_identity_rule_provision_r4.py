# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provision step and identity-rule patterns: what bc5e35cc still got wrong.

* A provision request can SAY a rule was measured (``value_exact_match``,
  ``value_core_match``). Those methods rank above model rules and keep their
  confidence uncapped, so provision keeps one only when its own
  re-measurement, the Resolve step's ``identity_affinity`` gates on the two
  columns now, builds the same rule; the confidence, shared count and
  reasoning then come from that measurement. Otherwise the rule is written as
  any rule of its request, capped, with why in its origin_context.
* Pattern literal text is literal to BOTH runtime readers (one compiler,
  ``identifier_translation.pattern_body``): ``LOT.{id}`` is a rule the
  affinity pass builds from real values and is written, and the resolver and
  the route seed translate ``LOT.123`` alike.

Neutral fixture: a batch register (``reg-sys``, an MES-class system) and a
receiving log (``recv-sys``, an ERP-class system). SQLite stands in for
PostgreSQL; the graph read of a field's concepts is a fake.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.db as agents_db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents import identifier_translation, identity_affinity, resolve, resolve_evidence  # noqa: E402
from agents.identity_affinity import AFFINITY_CONFIDENCE, SampledField  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "reg-sys", "reg_db"
TGT_SYS, TGT_SCHEMA = "recv-sys", "recv_db"
CONCEPT = "lot-ref"
SHARED = [f"LOT-2026-{n:05d}" for n in range(101, 109)]          # 8 values in both columns
SRC_ONLY = [f"LOT-2026-{n:05d}" for n in range(201, 205)]
TGT_ONLY = [f"LOT-2026-{n:05d}" for n in range(301, 305)]


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


class _Graph:
    """IdentityRule nodes by id (MERGE assignments applied; the onboarding
    stamp fills a NULL method) and the field-concept read."""

    def __init__(self):
        self.nodes: dict = {}
        self.writes: list = []
        self.field_concepts: dict = {}

    def write(self, cypher, params=None):
        params = params or {}
        self.writes.append((cypher, params))
        if cypher.startswith("MERGE (ir:IdentityRule"):
            node = self.nodes.setdefault(params["rid"], {})
            for prop, param in re.findall(r"ir\.(\w+) = \$(\w+)", cypher):
                node[prop] = params[param]
        elif "ir.method IS NULL" in cypher and "SET ir.method = 'onboarding-llm'" in cypher:
            for rid, node in self.nodes.items():
                if rid.startswith(params["prefix"]) and not node.get("method"):
                    node["method"] = "onboarding-llm"
        return True

    def query(self, cypher, params=None):
        params = params or {}
        if "schema_rewrite_started" in cypher and "MAPS_TO" in cypher:   # the target-field snapshot
            key = (params["sid"], params["tbl"], params["fld"])
            known = key in self.field_concepts
            return [{"started": None, "fields": int(known),
                     "edges": [{"concept": c, "confidence": 0.9}
                               for c in self.field_concepts.get(key, ())]}]
        if "MAPS_TO" in cypher and {"sid", "tbl", "fld"} <= set(params):
            key = (params["sid"], params["tbl"], params["fld"])
            return [{"concept": c, "confidence": 0.9} for c in self.field_concepts.get(key, ())]
        return []


@pytest.fixture
def world(monkeypatch):
    resolve_evidence._reset_sample_memo()
    graph = _Graph()
    graph.field_concepts[(TGT_SYS, "receipts", "lot_ref")] = (CONCEPT,)
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_query", graph.query)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    monkeypatch.setattr(onboard, "catalog", FakeCatalog({
        SRC_SYS: {"id": SRC_SYS, "schema": SRC_SCHEMA, "connection_type": "PostgreSQL",
                  "system_type": "MES"},
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL",
                  "system_type": "ERP"}}))
    state = SimpleNamespace(graph=graph, db=None)

    def build(source, target, fail=None, source_table="batches", source_column="lot_code"):
        resolve_evidence._reset_sample_memo()
        state.db = RuleCheckDb({
            SRC_SCHEMA: {source_table: (["row_id", source_column], _rows(source))},
            TGT_SCHEMA: {"receipts": (["row_id", "lot_ref"], _rows(target))},
        }, fail=fail)
        monkeypatch.setattr(onboard, "pg", state.db)
        monkeypatch.setattr(agents_db, "pg", state.db)  # the Resolve sampler and probe read here
        return state.db

    state.build = build
    yield state
    resolve_evidence._reset_sample_memo()


def _sampled(system_id, table, column, values):
    return SampledField(system_id, table, column, CONCEPT, tuple(values),
                        concepts=frozenset({CONCEPT}))


def _measured_exact_rule(source, target) -> dict:
    """The rule the Resolve step's exact pass builds for these columns."""
    src, tgt = _sampled(SRC_SYS, "batches", "lot_code", source), _sampled(TGT_SYS, "receipts", "lot_ref", target)

    def probe(field, values):
        held = set(target if field.system_id == TGT_SYS else source)
        return [v for v in values if v in held]
    rule = identity_affinity.exact_rule(src, tgt, probe)
    assert rule is not None and rule["method"] == "value_exact_match"
    return rule


def _run(*rules, checkpoint=False, source_table="batches", source_column="lot_code"):
    body = ProvisionRequest(
        system_id=SRC_SYS, system_name="Batch Register", schema=SRC_SCHEMA,
        rules=[dict(r) for r in rules],
        mappings=[{"table": source_table, "field": f"{source_table}.{source_column}",
                   "concept": CONCEPT}],
        connection_type="PostgreSQL", rules_from_checkpoint=checkpoint,
        resolve_job_id=onboard._bind_resolve_rules(SRC_SYS, rules) if checkpoint else "")
    ctx = onboard._make_provision_state(body)
    ctx.tables_seen = {source_table}
    onboard._dp_identity_rules(ctx)
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    return ctx


def _node(world) -> dict:
    (node,) = world.graph.nodes.values()
    return node


def _rule(**over) -> dict:
    base = {"concept": CONCEPT, "source_system": SRC_SYS, "source_field": "batches.lot_code",
            "source_table": "batches", "source_pattern": "", "target_system": TGT_SYS,
            "target_field": "receipts.lot_ref", "target_table": "receipts", "target_pattern": "",
            "confidence": 0.99, "reasoning": "trust me"}
    base.update(over)
    return base


# ── 1. a measured method is earned at provision, never taken from the request ──

def test_a_spoofed_exact_rule_with_one_shared_value_is_downgraded_and_capped(world):
    """REPRODUCTION. One value in both columns, sent as value_exact_match at
    0.99 with an invented shared count: bc5e35cc stored value_exact_match at
    0.99 (uncapped, ranked tier 1). Now provision-request, 0.7, and the
    origin says the re-measurement fell short."""
    world.build(source=[SHARED[0], *SRC_ONLY], target=[SHARED[0], *TGT_ONLY])
    ctx = _run(_rule(method="value_exact_match", shared_count=500))
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    assert "sent as value_exact_match" in node["origin_context"]
    assert "fewer shared values than the floor" in node["origin_context"]
    assert "trust me" not in node["origin_context"]
    (reason,) = [r["reason"] for r in ctx.rules_unchecked]
    assert "sent as value_exact_match" in reason


def test_a_spoofed_exact_rule_from_the_checkpoint_is_onboarding_llm_capped(world):
    """REPRODUCTION: the same claim in a checkpoint request is the
    checkpoint's rule (onboarding-llm), capped, not value_exact_match."""
    world.build(source=[SHARED[0], *SRC_ONLY], target=[SHARED[0], *TGT_ONLY])
    _run(_rule(method="value_exact_match"), checkpoint=True)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("onboarding-llm", 0.7)


def test_a_genuine_exact_rule_keeps_its_method_with_the_measured_confidence(world):
    """REPRODUCTION (confidence): the columns really share eight values. The
    method is kept; the confidence is the measurement's, not the 0.99 the
    request sent (bc5e35cc stored 0.99); so are the shared count and the
    reasoning."""
    source, target = [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY]
    world.build(source=source, target=target)
    measured = _measured_exact_rule(source, target)
    _run({**measured, "confidence": 0.99, "shared_count": 500, "reasoning": "trust me"})
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("value_exact_match", AFFINITY_CONFIDENCE)
    assert node["origin_context"].startswith(measured["reasoning"])
    assert "provision re-measured it as value_exact_match" in node["origin_context"]


def test_the_seeds_measured_rule_keeps_value_exact_match(world):
    """Guard (workshop module 2 and 7): the seed runs Resolve, then sends its
    rules to provision without the checkpoint flag. A rule Resolve's exact
    pass measured is still stored as value_exact_match at its probe
    confidence, with the reasoning whose shape clause the route seed reads."""
    source, target = [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY]
    world.build(source=source, target=target)
    measured = _measured_exact_rule(source, target)
    ctx = _run(measured, checkpoint=False)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("value_exact_match", AFFINITY_CONFIDENCE)
    assert identity_affinity.exact_shape_from_reasoning(node["origin_context"]) == "AAA-9999-99999"
    assert ctx.rules_unchecked == [] and ctx.rules_rejected == []


def test_an_exact_rule_whose_target_cannot_be_read_is_downgraded_and_counted_once(world):
    """REPRODUCTION: the re-measurement cannot run (the target database is
    down). bc5e35cc kept value_exact_match. Now the rule is written as its
    request's (provision-request), capped, and counted unchecked ONCE with
    both reasons."""
    source, target = [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY]
    world.build(source=source, target=target, fail=lambda schema, _sql: schema == TGT_SCHEMA)
    ctx = _run(_measured_exact_rule(source, target))
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = ctx.rules_unchecked
    assert "sent as value_exact_match" in entry["reason"] and "catalog" in entry["reason"]


def _core_values():
    source = [f"B-{n}" for n in (2847, 3150, 4161, 5172, 6183, 7194)]
    target = [f"LOT-2026-0{n}" for n in (2847, 3150, 4161, 5172, 6183, 7194)]
    return source, target


def _measured_core_rule(source, target) -> dict:
    rule = identity_affinity.affinity_rule(_sampled(SRC_SYS, "batches", "lot_code", source),
                                           _sampled(TGT_SYS, "receipts", "lot_ref", target))
    assert rule is not None and rule["method"] == "value_core_match"
    return rule


def test_a_genuine_core_rule_keeps_its_method_with_the_measured_confidence(world):
    """REPRODUCTION (confidence): a transform the columns bear out keeps
    value_core_match at the measurement's confidence, not the request's 0.99
    (bc5e35cc stored 0.99)."""
    source, target = _core_values()
    world.build(source=source, target=target)
    _run({**_measured_core_rule(source, target), "confidence": 0.99})
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("value_core_match", AFFINITY_CONFIDENCE)


def test_a_spoofed_core_rule_is_downgraded(world):
    """REPRODUCTION. value_core_match sent for a transform that fits only two
    value pairs (the affinity floor is three): bc5e35cc kept the method. Now
    provision-request; the transform itself is still checked as for any
    rule, and the origin says the method was not borne out."""
    source, target = _core_values()
    world.build(source=source[:2] + ["Q-1", "Q-2", "Q-3"], target=target[:2] + ["Z-9", "Z-8", "Z-7"])
    _run({**_measured_core_rule(source, target), "confidence": 0.99})
    node = _node(world)
    assert node["method"] == "provision-request"
    assert "sent as value_core_match" in node["origin_context"]


# ── 2. literal text is literal to both runtime readers ──────────────────────

def _dot_values():
    return [f"LOT.{n}" for n in (123, 456, 789, 512)], [f"BATCH.{n}" for n in (123, 456, 789, 512)]


def test_an_affinity_rule_with_a_dot_is_written(world):
    """REPRODUCTION. The affinity pass builds LOT.{id} -> BATCH.{id} from real
    values; bc5e35cc rejected it for "regular-expression syntax"."""
    source, target = _dot_values()
    world.build(source=source, target=target)
    measured = _measured_core_rule(source, target)
    assert (measured["source_pattern"], measured["target_pattern"]) == ("LOT.{id}", "BATCH.{id}")
    ctx = _run(measured)
    assert ctx.rules_rejected == []
    assert _node(world)["method"] == "value_core_match"


@pytest.mark.parametrize("src,tgt,value,translated,not_a_match", [
    ("LOT.{id}", "BATCH.{id}", "LOT.123", "BATCH.123", "LOTX123"),
    ("PN+({id})", "P-{id}", "PN+(1234)", "P-1234", "PNN1234"),
])
def test_both_readers_read_punctuation_literally_and_alike(src, tgt, value, translated, not_a_match):
    """REPRODUCTION. The resolver read the literal text as a regular
    expression: ``.`` matched any character (LOTX123 translated) and
    ``PN+(`` was a repetition and a group (``P-+(1234)``), while the route
    seed read it literally. Both now translate the value alike and refuse the
    near miss."""
    assert resolve.transform_id(src, tgt, value) == translated
    assert identifier_translation.apply(src, tgt, value) == translated
    assert resolve.transform_id(src, tgt, not_a_match) is None
    assert identifier_translation.apply(src, tgt, not_a_match) is None
    assert resolve.identity_pattern_problem(src, tgt) is None


def test_an_affinity_rule_with_a_plus_and_parentheses_is_written(world):
    """REPRODUCTION: PN+({id}) -> P-{id}, built from real values, is written."""
    source = [f"PN+({n})" for n in (1234, 2345, 3456, 4567)]
    target = [f"P-{n}" for n in (1234, 2345, 3456, 4567)]
    world.build(source=source, target=target)
    measured = _measured_core_rule(source, target)
    assert (measured["source_pattern"], measured["target_pattern"]) == ("PN+({id})", "P-{id}")
    ctx = _run(measured)
    assert ctx.rules_rejected == []
    assert _node(world)["method"] == "value_core_match"


def test_regex_looking_text_is_judged_by_the_sample_check_as_literal_text(world):
    """REPRODUCTION. ``LINE[0-9]+-{id}`` is literal text now. Against values
    like LINE7-42 it fits nothing and the sample check rejects it (bc5e35cc
    rejected it as regular-expression syntax); a column that holds that
    literal text writes it."""
    rule = _rule(source_pattern="LINE[0-9]+-{id}", target_pattern="L-{id}", confidence=0.8)
    world.build(source=["LINE7-42", "LINE8-7"], target=TGT_ONLY)
    ctx = _run(rule)
    (reason,) = [r["reason"] for r in ctx.rules_rejected]
    assert "no value of batches.lot_code fits it" in reason
    assert "regular-expression" not in reason
    world.graph.nodes.clear()
    world.build(source=["LINE[0-9]+-42"], target=TGT_ONLY)
    ctx = _run(rule)
    assert ctx.rules_rejected == [] and _node(world)["source_pattern"] == "LINE[0-9]+-{id}"
