# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provision step's identity-rule checks: what c875cbc5 still got wrong.

* Resolve withholds every rule between systems that serve different plants
  (``resolve_site_gate``, ADR-0051 P8) unless a steward-confirmed All Sites
  authority vouches for the concept; provision now holds every rule it is
  sent to the same decision, measured methods included.
* Earning a measured method reads fresh samples (never the Resolve step's
  sample memo) and counts an exact rule's shared values only from what the
  live membership probe confirms.
* A ``provision-request`` rule (nobody reviewed it at the checkpoint, nothing
  re-measured it) is never written above 0.7.
* The two pattern readers serve different inputs by design: the route seed
  finds identifiers inside a person's question (one word, any case), the
  resolver transforms one whole stored value (any characters, exact case).
  The affinity pass no longer writes a pattern whose ``{id}`` starts or ends
  with a separator, which the route seed could never find.

Neutral fixture: a shift log (``log-sys``, CMMS at Plant A), two line
systems (``line-a`` at Plant A, ``line-b`` at Plant B, MES), an order book
(``book-sys``, ERP, all sites) and a parts list (``parts-sys``, PLM, all
sites). SQLite stands in for PostgreSQL; the graph is a fake.
"""
from __future__ import annotations

import logging
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

CONCEPT = "lot-ref"
SYSTEMS = {
    "log-sys": {"system_type": "CMMS", "plant": "Plant A"},
    "line-a": {"system_type": "MES", "plant": "Plant A"},
    "line-b": {"system_type": "MES", "plant": "Plant B"},
    "book-sys": {"system_type": "ERP", "plant": "All Sites"},
    "parts-sys": {"system_type": "PLM", "plant": ""},
}
SHARED = [f"LOT-2026-{n:05d}" for n in range(101, 109)]          # 8 values
SRC_ONLY = [f"LOT-2026-{n:05d}" for n in range(201, 205)]
TGT_ONLY = [f"LOT-2026-{n:05d}" for n in range(301, 305)]


def _schema(system_id: str) -> str:
    return system_id.replace("-", "_") + "_db"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


class _Catalog(FakeCatalog):
    def get_all(self):
        return list(self.entries.values())


class _Graph:
    """IdentityRule nodes by id (MERGE assignments applied; the onboarding
    stamp fills a NULL method) and the field-concept read."""

    def __init__(self):
        self.nodes: dict = {}

    def write(self, cypher, params=None):
        params = params or {}
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
        if "schema_rewrite_started" in cypher and "MAPS_TO" in cypher:   # the target-field snapshot
            return [{"started": None, "fields": 1, "edges": [{"concept": CONCEPT, "confidence": 0.9}]}]
        if "MAPS_TO" in cypher and {"sid", "tbl", "fld"} <= set(params or {}):
            return [{"concept": CONCEPT, "confidence": 0.9}]
        return []


@pytest.fixture
def world(monkeypatch):
    resolve_evidence._reset_sample_memo()
    graph = _Graph()
    entries = {sid: {"id": sid, "name": sid, "schema": _schema(sid),
                     "connection_type": "PostgreSQL", **meta} for sid, meta in SYSTEMS.items()}
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_query", graph.query)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard, "catalog", _Catalog(entries))
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    # The All Sites vouch the site gate reads, as Resolve reads it.
    monkeypatch.setattr(onboard.self_learning, "known_systems", lambda: list(entries.values()))
    state = SimpleNamespace(graph=graph, db=None, vouch=("none", ""))
    monkeypatch.setattr(onboard.self_learning, "_authority_vouch_verdict",
                        lambda concept, _entries, known_owner_only=False: state.vouch)

    def build(source_sys, target_sys, source, target, fail=None):
        state.db = RuleCheckDb({
            _schema(source_sys): {"batches": (["row_id", "lot_code"], _rows(source))},
            _schema(target_sys): {"receipts": (["row_id", "lot_ref"], _rows(target))},
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


def _measured_exact_rule(source_sys, target_sys, source, target) -> dict:
    """The rule the Resolve step's exact pass builds for these columns."""
    src = _sampled(source_sys, "batches", "lot_code", source)
    tgt = _sampled(target_sys, "receipts", "lot_ref", target)

    def probe(field, values):
        held = set(target if field.system_id == target_sys else source)
        return [v for v in values if v in held]
    rule = identity_affinity.exact_rule(src, tgt, probe)
    assert rule is not None and rule["method"] == "value_exact_match"
    return rule


def _rule(target_sys, **over) -> dict:
    base = {"concept": CONCEPT, "source_field": "batches.lot_code", "source_table": "batches",
            "source_pattern": "", "target_system": target_sys, "target_field": "receipts.lot_ref",
            "target_table": "receipts", "target_pattern": "", "confidence": 0.8,
            "reasoning": "model rule"}
    base.update(over)
    return base


def _run(source_sys, *rules, checkpoint=False):
    body = ProvisionRequest(
        system_id=source_sys, system_name=source_sys, schema=_schema(source_sys),
        plant=SYSTEMS[source_sys]["plant"], rules=[dict(r) for r in rules],
        mappings=[{"table": "batches", "field": "batches.lot_code", "concept": CONCEPT}],
        connection_type="PostgreSQL", rules_from_checkpoint=checkpoint,
        resolve_job_id=onboard._bind_resolve_rules(source_sys, rules) if checkpoint else "")
    ctx = onboard._make_provision_state(body)
    ctx.tables_seen = {"batches"}
    onboard._dp_identity_rules(ctx)
    onboard._stamp_identity_rule_provenance(source_sys)
    return ctx


def _node(world) -> dict:
    (node,) = world.graph.nodes.values()
    return node


def _exact_world(world, source_sys, target_sys):
    source, target = [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY]
    world.build(source_sys, target_sys, source, target)
    return _measured_exact_rule(source_sys, target_sys, source, target)


# ── 1. the site gate, at provision, for every rule ──────────────────────────

def test_a_measured_rule_between_two_plants_is_rejected(world):
    """REPRODUCTION. A CMMS at Plant A and an MES at Plant B really share
    eight values; Resolve's site gate withholds the rule. c875cbc5's
    re-measurement ran only the same-class exact refusal and kept
    value_exact_match at 0.85."""
    rule = _exact_world(world, "log-sys", "line-b")
    ctx = _run("log-sys", rule)
    assert world.graph.nodes == {}
    (reason,) = [r["reason"] for r in ctx.rules_rejected]
    assert "different plants (Plant A, Plant B)" in reason


def test_a_model_rule_between_two_plants_is_rejected(world):
    """REPRODUCTION. The same pair as a plain model rule from a checkpoint
    request: c875cbc5 wrote it as onboarding-llm. The wizard cannot send it
    (its checkpoint lists Resolve's kept rules only)."""
    world.build("log-sys", "line-b", [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY])
    listed = "|".join(SHARED[:4])
    ctx = _run("log-sys", _rule("line-b", source_pattern=listed, target_pattern=listed),
               checkpoint=True)
    assert world.graph.nodes == {}
    (reason,) = [r["reason"] for r in ctx.rules_rejected]
    assert "different plants" in reason


def test_a_confirmed_all_sites_authority_lets_the_measured_rule_keep_its_method(world):
    """Guard (same vouch semantics as Resolve): a steward-confirmed All Sites
    authority for the concept says its values are one namespace."""
    world.vouch = ("steward", "ar-lot")
    rule = _exact_world(world, "log-sys", "line-b")
    ctx = _run("log-sys", rule)
    assert ctx.rules_rejected == []
    assert (_node(world)["method"], _node(world)["confidence"]) == ("value_exact_match", AFFINITY_CONFIDENCE)


def test_an_unconfirmed_all_sites_authority_does_not_vouch(world):
    """REPRODUCTION: an unconfirmed authority vouches for nothing (ADR-0052
    B4); c875cbc5 kept the method."""
    world.vouch = ("unverified", "ar-lot")
    rule = _exact_world(world, "log-sys", "line-b")
    ctx = _run("log-sys", rule)
    assert world.graph.nodes == {}
    (reason,) = [r["reason"] for r in ctx.rules_rejected]
    assert "the All Sites authority for 'lot-ref' (ar-lot) isn't confirmed" in reason


def test_a_measured_rule_within_one_plant_keeps_its_method(world):
    """Guard: the same plant on both sides."""
    rule = _exact_world(world, "log-sys", "line-a")
    _run("log-sys", rule)
    assert _node(world)["method"] == "value_exact_match"


@pytest.mark.parametrize("source_sys,target_sys", [("book-sys", "line-a"), ("parts-sys", "book-sys")],
                         ids=["order-book-to-plant-line", "parts-list-to-order-book"])
def test_the_seeds_measured_rules_keep_value_exact_match(world, source_sys, target_sys):
    """Guard (workshop modules 2 and 7): the seed provisions without the
    checkpoint flag; its ERP-to-plant-MES and PLM-to-ERP measured rules name
    at most one plant, so they keep value_exact_match at the probe
    confidence."""
    rule = _exact_world(world, source_sys, target_sys)
    ctx = _run(source_sys, rule, checkpoint=False)
    assert ctx.rules_rejected == [] and ctx.rules_unchecked == []
    assert (_node(world)["method"], _node(world)["confidence"]) == ("value_exact_match", AFFINITY_CONFIDENCE)


# ── 2. fresh samples and probe-confirmed shared values ──────────────────────

def _prime_memo(system_id, table, column, values):
    key = (system_id, "postgresql", _schema(system_id), table, column, resolve_evidence._SAMPLE_LIMIT)
    resolve_evidence._memo_put(key, list(values))


def test_a_stale_cached_overlap_the_live_columns_no_longer_hold_is_not_earned(world):
    """REPRODUCTION. The memo holds eight values both columns used to share;
    the live columns share none and the probes find nothing. c875cbc5 served
    the cached samples, counted their intersection and kept
    value_exact_match."""
    rule = _measured_exact_rule("log-sys", "line-a", [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY])
    world.build("log-sys", "line-a", SRC_ONLY * 2 + ["LOT-2026-00900"], TGT_ONLY)
    _prime_memo("log-sys", "batches", "lot_code", [*SHARED, *SRC_ONLY])
    _prime_memo("line-a", "receipts", "lot_ref", [*SHARED, *TGT_ONLY])
    _run("log-sys", rule)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)


def test_a_genuine_join_is_not_downgraded_by_an_old_cached_sample(world):
    """REPRODUCTION. The memo holds samples from before the columns shared
    anything; they do now. c875cbc5 read the memo and downgraded the rule."""
    rule = _exact_world(world, "log-sys", "line-a")
    _prime_memo("log-sys", "batches", "lot_code", SRC_ONLY)
    _prime_memo("line-a", "receipts", "lot_ref", TGT_ONLY)
    _run("log-sys", rule)
    assert _node(world)["method"] == "value_exact_match"


def test_shared_values_count_only_what_the_live_probe_confirms(world, monkeypatch):
    """REPRODUCTION. The two samples overlap but the membership probe
    confirms none of the values (the columns changed between the reads).
    c875cbc5 counted the bare sample intersection and kept the method."""
    rule = _exact_world(world, "log-sys", "line-a")
    monkeypatch.setattr(onboard, "_affinity_probe", lambda *_a, **_k: (lambda _field, _values: []))
    _run("log-sys", rule)
    assert _node(world)["method"] == "provision-request"


def test_the_resolve_step_still_reads_the_memo(world):
    """Guard: Resolve's own sampling still serves a memoized sample."""
    world.build("log-sys", "line-a", SRC_ONLY, TGT_ONLY)
    _prime_memo("log-sys", "batches", "lot_code", ["CACHED-1"])
    values = resolve_evidence.sample_field_values(
        system_id="log-sys", connection_type="PostgreSQL", schema=_schema("log-sys"),
        table="batches", field="lot_code")
    assert values == ["CACHED-1"]


# ── 3. a provision-request rule is never above 0.7 ──────────────────────────

_CORE_SRC = [f"B-{n}" for n in (2847, 3150, 4161, 5172, 6183, 7194)]
_CORE_TGT = [f"LOT-2026-0{n}" for n in (2847, 3150, 4161, 5172, 6183, 7194)]


def _core_rule(**over) -> dict:
    return _rule("line-a", source_pattern="B-{id}", target_pattern="LOT-2026-0{id}",
                 confidence=0.99, **over)


def test_a_downgraded_core_rule_whose_pattern_fits_is_capped(world):
    """REPRODUCTION (the reviewer's case). value_core_match sent at 0.99 for
    a transform only two value pairs bear out: the method is not earned, but
    the pattern fits a live value, so c875cbc5 lifted the cap and stored
    provision-request at 0.99."""
    world.build("log-sys", "line-a", _CORE_SRC[:2] + ["Q-1", "Q-2", "Q-3"],
                _CORE_TGT[:2] + ["Z-9", "Z-8", "Z-7"])
    _run("log-sys", _core_rule(method="value_core_match"))
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)


def test_a_model_rule_sent_without_the_checkpoint_is_capped(world):
    """REPRODUCTION: a plain transform rule from a request without the
    checkpoint flag; its pattern fits, and c875cbc5 stored 0.99."""
    world.build("log-sys", "line-a", _CORE_SRC, _CORE_TGT)
    _run("log-sys", _core_rule(), checkpoint=False)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)


def test_a_checkpoint_rule_whose_pattern_fits_is_onboarding_llm_under_the_ceiling(world):
    """Guard: a checkpoint rule whose pattern fits a live value is written as
    ``onboarding-llm``. It carries no measured method, so the fit does not
    lift the 0.7 ceiling (2026-10-28; the model's 0.99 used to be written)."""
    world.build("log-sys", "line-a", _CORE_SRC, _CORE_TGT)
    _run("log-sys", _core_rule(), checkpoint=True)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("onboarding-llm", 0.7)


def test_the_wizards_kept_direct_rule_is_onboarding_llm_at_0_7(world):
    """Guard (workshop module 7 query 3b): a kept direct match from the
    checkpoint, both systems at one plant."""
    world.build("log-sys", "line-a", [*SHARED, *SRC_ONLY], [*SHARED, *TGT_ONLY])
    listed = "|".join(SHARED[:4])
    _run("log-sys", _rule("line-a", source_pattern=listed, target_pattern=listed), checkpoint=True)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("onboarding-llm", 0.7)


# ── 4. the two readers' contract ────────────────────────────────────────────

def test_the_route_seed_finds_a_word_identifier_in_free_text_in_any_case():
    """Guard (contract): the route seed reads a person's question. It finds
    the identifier as one word, whatever its case, and never inside a longer
    token."""
    question = "which customers got lot-2847 and LOT-3150, not XLOT-9999?"
    found = [t["token"] for t in identifier_translation.find_translations(question, [{
        "source_system": "a", "source_field": "f", "source_pattern": "LOT-{id}",
        "target_system": "b", "target_field": "g", "target_pattern": "B-{id}"}])]
    assert sorted(found) == ["LOT-3150", "lot-2847"]
    assert identifier_translation.apply("LOT-{id}", "B-{id}", "LOT-2026-02847") is None


def test_the_resolver_transforms_one_whole_stored_value_exactly():
    """Guard (contract): the resolver reads one stored value. Its core may
    hold any character, and case is the value's own."""
    assert resolve.transform_id("LOT-{id}", "B-{id}", "LOT-2026-02847") == "B-2026-02847"
    assert resolve.transform_id("LOT-{id}", "B-{id}", "lot-2847") is None


@pytest.mark.parametrize("source,target", [
    (["WO-9921", "WO-9935", "WO-9940", "WO-9952"], ["MFG-WO-9921", "MFG-WO-9935", "MFG-WO-9940", "MFG-WO-9952"]),
    (["X-123", "X-456", "X-789", "X-512"], ["Y-123", "Y-456", "Y-789", "Y-512"]),
])
def test_an_affinity_pattern_never_puts_a_separator_inside_its_core(source, target):
    """REPRODUCTION (the concrete break between the readers). For WO-9921 ↔
    MFG-WO-9921 the affinity pass wrote ``WO{id}`` -> ``MFG-WO{id}`` (its core
    ``-9921`` began with the hyphen). The resolver read it; the route seed,
    whose core is one word, could never find WO-9921 in a question. The core
    is now the digits; both readers translate the value alike."""
    rule = identity_affinity.affinity_rule(_sampled("log-sys", "t", "c", source),
                                           _sampled("line-a", "u", "d", target))
    assert rule is not None
    assert rule["source_pattern"].endswith("-{id}") and rule["target_pattern"].endswith("-{id}")
    value = source[0]
    by_resolver = resolve.transform_id(rule["source_pattern"], rule["target_pattern"], value)
    by_seed = identifier_translation.apply(rule["source_pattern"], rule["target_pattern"], value)
    assert by_resolver == by_seed == target[0]


def test_a_new_rules_literal_reading_is_not_logged(caplog):
    """A pattern read with the literal grammar (a rule this code wrote, or a
    provision check) logs nothing; only a stored row read with the
    pre-2026-10-21 grammar does (test_identity_rule_provision_r6.py)."""
    pattern = "TRACE(7)+-{id}"
    with caplog.at_level(logging.INFO):
        assert resolve.transform_id(pattern, "T-{id}", "TRACE(7)+-42") == "T-42"
        assert identifier_translation.apply(pattern, "T-{id}", "TRACE(7)+-42") == "T-42"
    assert not [r for r in caplog.records if "grammar" in r.getMessage()]
