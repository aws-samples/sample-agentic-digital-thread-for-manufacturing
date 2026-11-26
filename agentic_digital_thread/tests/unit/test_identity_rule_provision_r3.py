# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provision re-check of an identity rule: what 0657e3dc still got wrong.

* A bounded sample is proof of a miss only when it read every candidate: a
  read that reached its cap with nothing fitting leaves the rule written
  unchecked (counted), never rejected.
* A pattern pair is valid only when both runtime readers read it alike
  (``resolve.transform_id`` reads literal text as a regular expression,
  ``identifier_translation.pattern_regex`` literally): every writer refuses
  literal regular-expression syntax, a placeholder one side does not carry,
  and a pattern that does not compile, with a fixed reason.
* A rule is stamped as approved at the Identity Rules checkpoint only when
  the provision request says it came from there (the wizard, and the chat's
  server-built request). Any other request's rule is written with the method
  ``provision-request`` and says so; a request cannot set a steward method.
* Every onboarding MERGE of a rule states this run's origin and re-check
  outcome, so a node that is written again never keeps stale evidence.
* The target-column check reads with the short deadline, and an unreadable
  target catalog is "not checked" (counted), not "field not found".

Neutral fixture: a shift log (``log-sys``) and a line's stop records
(``line-sys``). SQLite stands in for PostgreSQL.
"""
from __future__ import annotations

import logging
import os
import re
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents import identifier_translation, resolve  # noqa: E402
from agents.models import LearnRequest, ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "log-sys", "log_db"
TGT_SYS, TGT_SCHEMA = "line-sys", "line_db"
LOGINS = ["tech_01", "tech_02", "tech_07", "lead"]
LISTED = "tech_01|tech_02|tech_07|lead"
CHECKPOINT_WORDS = "approved by the user at the Identity Rules checkpoint"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


class _Graph:
    """The IdentityRule nodes the writes leave, by id: each MERGE applies its
    ``ir.<prop> = $<param>`` assignments to the node (properties it does not
    set keep their value), and the onboarding stamp fills a NULL method."""

    def __init__(self):
        self.nodes: dict = {}
        self.writes: list = []

    def write(self, cypher, params=None):
        params = params or {}
        self.writes.append((cypher, params))
        if cypher.startswith("MERGE (ir:IdentityRule"):
            node = self.nodes.setdefault(params["rid"], {})
            for prop, param in re.findall(r"ir\.(\w+) = \$(\w+)", cypher):
                node[prop] = params[param]
        elif "ir.method IS NULL" in cypher and "SET ir.method = 'onboarding-llm'" in cypher:
            keep = "coalesce(ir.origin_context, $origin_context)" in cypher
            for rid, node in self.nodes.items():
                if rid.startswith(params["prefix"]) and not node.get("method"):
                    node["method"] = "onboarding-llm"
                    node["origin_context"] = (node.get("origin_context") if keep and node.get("origin_context")
                                              else params["origin_context"])
        return True


@pytest.fixture
def world(monkeypatch):
    graph = _Graph()
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    monkeypatch.setattr(onboard, "catalog", FakeCatalog({
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL"}}))
    state = SimpleNamespace(graph=graph, db=None)

    def build(source=None, stops=None, fail=None):
        state.db = RuleCheckDb({
            SRC_SCHEMA: {"shift_notes": (["note_id", "signed_by"],
                                         _rows(["A. Lee", *LOGINS] if source is None else source))},
            TGT_SCHEMA: {"stops": (["stop_id", "closed_by"], _rows(LOGINS if stops is None else stops))},
        }, fail=fail)
        monkeypatch.setattr(onboard, "pg", state.db)
        return state.db

    state.build = build
    return state


def _rule(**over) -> dict:
    base = {"concept": "operator", "source_system": SRC_SYS,
            "source_field": "shift_notes.signed_by", "source_pattern": LISTED,
            "target_system": TGT_SYS, "target_field": "stops.closed_by",
            "target_pattern": LISTED, "confidence": 0.8, "reasoning": "4 shared values measured"}
    base.update(over)
    return base


def _run(*rules, checkpoint=True) -> onboard._ProvisionState:
    """One provision's rule step, the ctx built from the request as the
    provision builds it. ``checkpoint`` None: the request says nothing.
    ``checkpoint`` True also names a Resolve result that listed the rules."""
    extra = {} if checkpoint is None else {"rules_from_checkpoint": checkpoint}
    if checkpoint:
        extra["resolve_job_id"] = onboard._bind_resolve_rules(SRC_SYS, rules)
    body = ProvisionRequest(system_id=SRC_SYS, system_name="Shift Log", schema=SRC_SCHEMA,
                            rules=[dict(r) for r in rules],
                            mappings=[{"table": "shift_notes", "field": "shift_notes.signed_by",
                                       "concept": "operator"}],
                            connection_type="PostgreSQL", **extra)
    ctx = onboard._make_provision_state(body)
    ctx.tables_seen = {"shift_notes"}
    onboard._dp_identity_rules(ctx)
    return ctx


def _written(graph) -> list:
    return [p for c, p in graph.writes if c.startswith("MERGE (ir:IdentityRule")]


def _reasons(ctx) -> list:
    return [r["reason"] for r in ctx.rules_rejected]


def _unchecked(ctx) -> list:
    return [r["reason"] for r in getattr(ctx, "rules_unchecked", [])]


# ── 1. a capped sample is not proof of a miss ───────────────────────────────

_WILD = {"source_pattern": "LINE{*}-{id}", "target_pattern": "L-{id}"}


def _false_candidates(n) -> list:
    """Values the pattern's LIKE filter selects that ``transform_id`` refuses:
    ``{*}`` is one alphanumeric token, never ``_``."""
    return [f"LINE_{i}-1" for i in range(n)]


def test_a_fitting_value_after_the_first_sample_rows_is_found(world):
    """REPRODUCTION. 500 candidates that do not fit, then one
    that does. 0657e3dc read 500 rows and rejected the rule. The fit
    validates the pattern; with no value shown in both columns the rule is
    counted unchecked for that alone, and with no measured method the fit
    does not lift the 0.7 ceiling (2026-10-28; the model's 0.8 was written)."""
    world.build(source=[*_false_candidates(500), "LINE7-42"])
    ctx = _run(_rule(**_WILD))
    assert _reasons(ctx) == [] and _unchecked(ctx) == [
        "provision did not confirm this rule with live values (shift_notes.signed_by and line-sys "
        "stops.closed_by)"]
    (params,) = _written(world.graph)
    assert params["conf"] == 0.7  # the live transform succeeded; no measured method


def test_a_capped_sample_where_nothing_fits_leaves_the_rule_unchecked(world):
    """REPRODUCTION. More candidates than the read takes, none of
    the ones read fits (one further on would): not proof, so the rule is
    written unchecked and counted. 0657e3dc rejected it."""
    world.build(source=[*_false_candidates(600), "LINE7-42"])
    ctx = _run(_rule(**_WILD))
    assert _reasons(ctx) == []
    (params,) = _written(world.graph)
    assert params["conf"] == 0.7
    (reason,) = _unchecked(ctx)
    assert "more" in reason and "shift_notes.signed_by" in reason


def test_an_exhaustive_sample_where_nothing_fits_is_rejected(world):
    """Guard: every candidate was read and none fits, so the rule is rejected."""
    world.build(source=_false_candidates(10))
    ctx = _run(_rule(**_WILD))
    assert _written(world.graph) == []
    (reason,) = _reasons(ctx)
    assert "doesn't match sample value" in reason


def test_no_candidate_at_all_is_rejected_by_the_filter_alone(world):
    """Guard: the LIKE filter selects every value that could fit; none does."""
    world.build(source=["A. Lee", *LOGINS])
    ctx = _run(_rule(**_WILD))
    assert _written(world.graph) == []
    (reason,) = _reasons(ctx)
    assert "no value of shift_notes.signed_by fits it" in reason


# ── 2. one valid pattern pair, the same for both readers and every writer ───

@pytest.mark.parametrize("src,tgt,written", [
    ("LINE[0-9]+-{id}", "L-{id}", False),   # 0657e3dc wrote it, reading [0-9]+ as a class
    ("WO\\.{id}", "W-{id}", False),          # 0657e3dc wrote it, reading \. as a dot
    ("A+{id}", "B-{id}", False),
    ("B-{id}", "LOT.{id}", True),            # punctuation on the target side
])
def test_punctuation_in_literal_text_is_read_literally(world, src, tgt, written):
    """REPRODUCTION. 0657e3dc's resolver read the literal text as a regular
    expression while the route seed read it literally, so the readers
    disagreed on such a rule. Both now read it literally (one compiler,
    identifier_translation.pattern_body), so no syntax rule is needed: a
    pattern whose literal text no value holds is rejected by the sample
    check, and a pattern the values fit is written."""
    world.build(source=[*_false_candidates(500), "LINE7-42", "WO.5", "AA7", "B-7"])
    ctx = _run(_rule(source_pattern=src, target_pattern=tgt))
    assert bool(_written(world.graph)) is written
    if not written:
        (reason,) = _reasons(ctx)
        assert "fits it" in reason and "regular-expression" not in reason


@pytest.mark.parametrize("src,tgt,needle", [
    ("X-{id}", "Y-{other}", "target pattern names a placeholder the source pattern does not capture"),
    ("X-{id}", "Y", "source pattern captures a placeholder the target pattern does not name"),
    ("A{id}-{id}", "B-{id}", "names the same placeholder twice"),
])
def test_an_unusable_placeholder_pair_is_rejected(world, src, tgt, needle):
    """REPRODUCTION. ``transform_id("X-{id}", "Y-{other}", "X-7")``
    is ``Y-{other}``, and 0657e3dc counted that as a live transform (conf 0.8)."""
    world.build(source=["X-7", "A1-1"])
    ctx = _run(_rule(source_pattern=src, target_pattern=tgt))
    assert _written(world.graph) == []
    (reason,) = _reasons(ctx)
    assert needle in reason


@pytest.mark.parametrize("src,tgt,token", [
    ("LINE[0-9]+-{id}", "L-{id}", "LINE7-42"),
    ("X-{id}", "Y-{other}", "X-7"),
    ("B-{id}", "LOT-2026-0{id}", "B-1234"),
    ("PRT-{id}-R-{*}", "{id}", "PRT-5101-R-C"),
    ("{id}", "PRT-{id}-R-{*}", "5101"),
])
def test_both_readers_read_every_pair_provision_writes_alike(world, src, tgt, token):
    """REPRODUCTION. For each pair provision writes, the resolver
    (``transform_id``) and the route seed (``identifier_translation.apply``)
    agree whether the value is translated and to what. 0657e3dc wrote pairs
    the two read differently (LINE[0-9]+, an uncaptured target). The one
    difference left is by design: the route seed never reads FROM a pattern
    with no literal text (a bare ``{id}`` would match every word of a
    question), so that direction is not compared."""
    world.build(source=[token])
    _run(_rule(source_pattern=src, target_pattern=tgt))
    if not _written(world.graph) or identifier_translation.pattern_regex(src) is None:
        return
    by_resolver = resolve.transform_id(src, tgt, token)
    by_translation = identifier_translation.apply(src, tgt, token)
    assert (by_resolver or "").replace("%", "{*}") == (by_translation or "")


def test_the_learn_route_refuses_an_unusable_pair(monkeypatch):
    """REPRODUCTION: the taught-rule writer (/api/thread/learn)
    stored any pair; a target placeholder the source does not capture is a
    pair the readers fill differently (the resolver leaves ``{other}`` in,
    the route seed refuses). A valid one is still created."""
    writes = []
    monkeypatch.setattr(resolve, "neptune_query", lambda q, p=None: [{"s.id": TGT_SYS}])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: "")
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    bad = resolve.thread_learn(LearnRequest(
        concept="batch", source_system=SRC_SYS, source_field="run_code",
        source_pattern="X-{id}", target_system=TGT_SYS, target_field="lot_ref",
        target_pattern="Y-{other}"))
    assert "does not capture" in bad.get("error", "") and writes == []
    good = resolve.thread_learn(LearnRequest(
        concept="batch", source_system=SRC_SYS, source_field="run_code",
        source_pattern="R-{id}", target_system=TGT_SYS, target_field="lot_ref",
        target_pattern="L-{id}"))
    assert good["status"] == "created"


def test_the_proposal_accept_writer_uses_a_pair_the_validator_accepts():
    """Guard (new code): the proposal-accept writer stores one constant pair."""
    from agents import self_learning
    pair = (self_learning._ACCEPTED_RULE_PATTERN, self_learning._ACCEPTED_RULE_PATTERN)
    assert resolve.identity_pattern_problem(*pair) is None


# ── 3. only a checkpoint request is stamped as approved there ───────────────

def test_a_rule_sent_without_the_checkpoint_says_so(world):
    """REPRODUCTION. A request that does not say its rules came
    from the Identity Rules checkpoint (a script, a direct API call) had its
    rules stamped onboarding-llm "approved by the user at the Identity Rules
    checkpoint". Now: method provision-request, and the origin says it."""
    world.build()
    _run(_rule(), checkpoint=None)
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    (node,) = world.graph.nodes.values()
    assert node["method"] == "provision-request"
    assert node["origin_context"].startswith(
        "Sent in a provision request that did not come from the Identity Rules checkpoint")
    assert CHECKPOINT_WORDS not in node["origin_context"]
    assert node["confidence"] == 0.7


def test_the_wizards_kept_rule_is_still_onboarding_llm_at_0_7(world):
    """Guard (module 7 query 3b): the wizard's request says its rules came
    from the checkpoint; the kept direct rule is written, stamped
    onboarding-llm, confidence 0.7, origin naming the checkpoint."""
    world.build()
    _run(_rule(), checkpoint=True)
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    (node,) = world.graph.nodes.values()
    assert (node["method"], node["confidence"]) == ("onboarding-llm", 0.7)
    assert node["origin_context"].startswith(
        "Proposed by onboarding schema analysis (LLM); " + CHECKPOINT_WORDS)


@pytest.mark.parametrize("method", ["user_confirmed", "crosswalk_confirmed", "onboarding-llm"])
def test_a_provision_request_cannot_set_a_steward_method(world, method):
    """REPRODUCTION. 0657e3dc stored the method a request sent:
    a direct POST could write a rule the route seed ranks first and a
    re-provision never deletes (``user_confirmed``)."""
    world.build()
    _run(_rule(method=method), checkpoint=False)
    (node,) = world.graph.nodes.values()
    assert node["method"] == "provision-request"


def test_a_measured_method_provision_cannot_re_measure_is_not_kept(world):
    """A method the Resolve step emits is kept only when provision's own
    re-measurement earns it (test_identity_rule_provision_r4.py). Here the
    two columns share no concept and too few values, so a request that sends
    value_exact_match gets provision-request, capped."""
    world.build()
    _run(_rule(method="value_exact_match", source_pattern="", target_pattern="",
               confidence=0.85), checkpoint=None)
    (node,) = world.graph.nodes.values()
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)


def test_the_route_seed_ranks_a_provision_request_rule_with_onboarding_rules():
    """REPRODUCTION: a provision-request rule carries an
    origin_context, which every tiering reader read as an accepted proposal
    (tier 1, above onboarding rules)."""
    from agents import orchestrator as orch
    rule = {"method": "provision-request", "origin_context": "Sent in a provision request"}
    assert orch._provenance_tier(rule) == 2
    assert identifier_translation._tier(rule) == 2
    cypher = orch._ROUTE_SEED_CYPHER
    tier = cypher.index("WHEN ir.method = 'provision-request' THEN 2")
    assert tier < cypher.index("WHEN ir.provenance IS NOT NULL OR ir.origin_context IS NOT NULL", tier - 200)
    from agents.route_seed_trace import _METHOD_WORDS
    assert "provision-request" in _METHOD_WORDS


# ── 4. every MERGE states this run's origin and outcome ─────────────────────

def test_a_rule_written_again_unchecked_drops_the_old_evidence(world):
    """REPRODUCTION. The same rule node, first a direct match
    proven on both sides, then a bare '*' (not re-checked): 0657e3dc set no
    origin_context on the second write, so the node kept "N of the M values
    ... occur in both"."""
    world.build()
    _run(_rule())
    (node,) = world.graph.nodes.values()
    assert "occur in both" in node["origin_context"]
    _run(_rule(source_pattern="*", target_pattern="*"))
    assert "occur in both" not in node["origin_context"]
    assert "not re-checked" in node["origin_context"] and "'*'" in node["origin_context"]


def test_a_rule_written_again_as_a_transform_drops_the_old_evidence(world):
    """REPRODUCTION: direct, then a transform pattern on the
    same endpoints."""
    world.build()
    _run(_rule())
    (node,) = world.graph.nodes.values()
    _run(_rule(source_pattern="tech_{n}", target_pattern="T-{n}"))
    assert "occur in both" not in node["origin_context"]
    assert "fits the pattern" in node["origin_context"]
    assert (node["source_pattern"], node["target_pattern"]) == ("tech_{n}", "T-{n}")


def test_every_onboarding_merge_sets_origin_context(world):
    """REPRODUCTION: an LLM rule written without a re-check
    outcome (a source that is not PostgreSQL) still states its origin."""
    world.build()
    body = ProvisionRequest(system_id=SRC_SYS, schema=SRC_SCHEMA, rules=[_rule()],
                            mappings=[{"table": "shift_notes", "field": "shift_notes.signed_by"}],
                            connection_type="REST API", rules_from_checkpoint=True,
                            resolve_job_id=onboard._bind_resolve_rules(SRC_SYS, [_rule()]))
    ctx = onboard._make_provision_state(body)
    onboard._dp_identity_rules(ctx)
    ((cypher, params),) = [(c, p) for c, p in world.graph.writes if c.startswith("MERGE (ir:IdentityRule")]
    assert "ir.origin_context = $octx" in cypher
    assert params["octx"].startswith("Proposed by onboarding schema analysis (LLM)")


# ── 5. the target-column check: short deadline, unavailable is not missing ──

def test_an_unreadable_target_catalog_leaves_the_rule_unchecked(world):
    """REPRODUCTION. The target catalog read failed and
    0657e3dc reported "target_field ... not found" and rejected the rule."""
    world.build(fail=lambda schema, sql: schema == TGT_SCHEMA and "information_schema" in sql)
    ctx = _run(_rule())
    assert _reasons(ctx) == []
    (params,) = _written(world.graph)
    assert params["conf"] == 0.7
    (reason,) = _unchecked(ctx)
    assert "catalog" in reason and "could not be read" in reason


def test_a_missing_target_field_is_still_rejected(world):
    """Guard: a readable catalog without the column rejects the rule."""
    world.build()
    ctx = _run(_rule(target_field="stops.closed_on"))
    assert _reasons(ctx) == ["target_field 'stops.closed_on' not found in line_db"]


def test_every_read_of_the_rule_checks_has_a_short_deadline(world):
    """REPRODUCTION: every read the rule step makes,
    catalog reads included, passes a deadline below pg's 30 s default."""
    db = world.build()
    _run(_rule(), _rule(source_pattern="tech_{n}", target_pattern="T-{n}"))
    assert db.calls
    for _schema, sql, _params, kw in db.calls:
        assert 0 < (kw.get("statement_timeout_ms") or 0) <= 5000, sql


def test_an_unreadable_target_catalog_is_logged_as_unchecked(world, caplog):
    """REPRODUCTION: the unreadable target catalog gets the
    "written unchecked" log line with its reason (0657e3dc rejected the rule)."""
    world.build(fail=lambda schema, sql: schema == TGT_SCHEMA and "information_schema" in sql)
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ctx = _run(_rule())
    (reason,) = _unchecked(ctx)
    assert any("IdentityRule written unchecked" in r.getMessage() and reason in r.getMessage()
               for r in caplog.records)
