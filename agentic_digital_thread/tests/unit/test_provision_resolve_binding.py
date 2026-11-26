# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A provision is tied to the Resolve result whose rules the checkpoint showed.

``POST /provision`` stamped every rule of a request that said
``rules_from_checkpoint`` as approved at the Identity Rules checkpoint
(``onboarding-llm``, no 0.7 provision-request cap). Nothing on the server tied
the request to a Resolve job: ``_resolve_jobs`` deletes a job on its first
``done`` read and the wizard sent only the rules, so the flag was the caller's
own word. Now the Resolve job keeps its rules' digests server-side for a day
(``_bind_resolve_rules``, keyed by system and job), its answer carries
``resolve_job_id``, the wizard sends that id back, and only a rule that is,
content-exact, in that result gets the checkpoint treatment. Every other rule
is written as a provision request's.

Neutral fixture: a kiln's firing log (``kiln-log``) and a glaze supplier's
order book (``glaze-erp``), names unlike the seed data. SQLite stands in for
PostgreSQL; the graph is a fake.
"""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
from agents.models import OnboardResolveRequest, ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "kiln-log", "kiln_db"
TGT_SYS, TGT_SCHEMA = "glaze-erp", "glaze_db"
#: The app's own store, read before the fixture below swaps in a fresh one.
_APP_STORE = getattr(onboard, "_resolve_results", None)


class _SharedTable:
    """A DynamoDB table double two JobStores (two replicas) share."""

    def __init__(self):
        self.items: dict = {}

    def put_item(self, Item, **_kw):  # noqa: N803 (boto3 kwarg)
        self.items[Item["system_id"]] = dict(Item)

    def get_item(self, Key, **_kw):  # noqa: N803
        item = self.items.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key, **_kw):  # noqa: N803
        self.items.pop(Key["system_id"], None)


@pytest.fixture(autouse=True)
def bindings(monkeypatch):
    store = JobStore("resolve-result", table=None)
    # raising=False: on a server without the store, the tests fail on what
    # they check rather than on this setup.
    monkeypatch.setattr(onboard, "_resolve_results", store, raising=False)
    return store


@pytest.fixture
def world(monkeypatch):
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})) or True)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    monkeypatch.setattr(onboard, "catalog", FakeCatalog({
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL"}}))
    monkeypatch.setattr(onboard, "pg", RuleCheckDb({
        SRC_SCHEMA: {"firings": (["firing_id", "batch_tag"], [(1, "K-4410"), (2, "K-4411")])},
        TGT_SCHEMA: {"orders": (["order_id", "lot_no"], [(1, "GZ-4410"), (2, "GZ-4411")])},
    }))
    return SimpleNamespace(writes=writes)


def _rule(**over) -> dict:
    """A transform rule both columns bear out (its check passes; method-less,
    so it is written at most 0.7 either way)."""
    return {"concept": "batch-tag", "source_system": SRC_SYS, "source_field": "firings.batch_tag",
            "source_pattern": "K-{id}", "target_system": TGT_SYS, "target_field": "orders.lot_no",
            "target_table": "orders", "target_pattern": "GZ-{id}", "confidence": 0.8,
            "reasoning": "same batch", **over}


def _body(rules, **over) -> ProvisionRequest:
    return ProvisionRequest(**{
        "system_id": SRC_SYS, "system_name": "Kiln Log", "schema": SRC_SCHEMA,
        "rules": [dict(r) for r in rules],
        "mappings": [{"table": "firings", "field": "firings.batch_tag", "concept": "batch-tag"}],
        "connection_type": "PostgreSQL", "rules_from_checkpoint": True, **over})


def _provision(body) -> onboard._ProvisionState:
    ctx = onboard._make_provision_state(body)
    ctx.tables_seen = {"firings"}
    onboard._dp_identity_rules(ctx)
    return ctx


def _merges(world) -> list:
    return [(c, p) for c, p in world.writes if c.startswith("MERGE (ir:IdentityRule")]


def _assert_checkpoint(cypher, params):
    assert "ir.method" not in cypher  # left for the onboarding stamp
    assert params["octx"].startswith("Proposed by onboarding schema analysis (LLM); "
                                     "approved by the user at the Identity Rules checkpoint")
    # A method-less rule stays under the 0.7 ceiling even when its pattern
    # fits (ADR-0043 note 2026-10-28), so the checkpoint shows in the method
    # and origin above, not in the number.
    assert params["conf"] == 0.7


def _assert_unbound(cypher, params):
    assert "ir.method = $method" in cypher and params["method"] == "provision-request"
    assert params["octx"].startswith("Sent as kept at the Identity Rules checkpoint, but it is "
                                     "not a rule of the Resolve result")
    assert params["conf"] == 0.7


def test_a_rule_of_the_resolve_result_the_request_names_is_written_as_kept_there(world):
    """The wizard's case: the rules Resolve listed, sent back with its job id."""
    rules = [_rule()]
    job = onboard._bind_resolve_rules(SRC_SYS, rules, "job-1")
    ((cypher, params),) = _merges_of(world, _body(rules, resolve_job_id=job))
    _assert_checkpoint(cypher, params)


def test_a_request_that_names_no_resolve_result_is_written_as_a_provision_requests(world, caplog):
    """REPRODUCTION: the flag alone made the rule 'approved at the checkpoint'
    (method left for the onboarding stamp, the 0.8 lift kept)."""
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ((cypher, params),) = _merges_of(world, _body([_rule()]))
    _assert_unbound(cypher, params)
    assert "the request names no Resolve result" in caplog.text


def test_a_rule_edited_after_resolve_loses_the_label_and_its_sibling_keeps_it(world):
    """REPRODUCTION: a caller that raises one rule's confidence (or changes
    any key) sends a rule Resolve never listed; the untouched rule beside it
    is still the checkpoint's."""
    kept, edited = _rule(), _rule(concept="kiln-batch")
    job = onboard._bind_resolve_rules(SRC_SYS, [kept, edited], "job-2")
    raised = {**edited, "confidence": 0.99}
    merges = {p["concept"]: (c, p) for c, p in _merges_of(world, _body(
        [kept, raised], resolve_job_id=job))}
    _assert_checkpoint(*merges["batch-tag"])
    cypher, params = merges["kiln-batch"]
    assert params["method"] == "provision-request" and params["conf"] <= 0.7


def test_another_systems_resolve_result_binds_nothing(world, caplog):
    """REPRODUCTION: a Resolve job of another system names no rule of this one."""
    rules = [_rule()]
    onboard._bind_resolve_rules("other-sys", rules, "job-3")
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ((cypher, params),) = _merges_of(world, _body(rules, resolve_job_id="job-3"))
    _assert_unbound(cypher, params)
    assert "Resolve result 'job-3' was not found" in caplog.text


def test_a_result_of_another_system_under_this_systems_key_is_refused(world, bindings):
    """A record whose own system is another's (a key collision) binds nothing."""
    rules = [_rule()]
    onboard._bind_resolve_rules("other-sys", rules, "job-4")
    bindings[onboard._resolve_result_key(SRC_SYS, "job-4")] = bindings.get(
        onboard._resolve_result_key("other-sys", "job-4"))
    assert onboard._bound_resolve_digests(SRC_SYS, "job-4") == (
        frozenset(), "Resolve result 'job-4' is another system's")


def test_the_label_needs_the_requests_word_too(world):
    """A bound rule in a request that does not say it was kept at the
    checkpoint is a provision request's, with the plain origin."""
    rules = [_rule()]
    job = onboard._bind_resolve_rules(SRC_SYS, rules, "job-5")
    ((_cypher, params),) = _merges_of(world, _body(rules, resolve_job_id=job,
                                                   rules_from_checkpoint=False))
    assert params["method"] == "provision-request"
    assert params["octx"].startswith("Sent in a provision request that did not come from the "
                                     "Identity Rules checkpoint")


def test_a_float_the_browser_sends_back_as_an_integer_still_matches(world):
    """JSON in the browser writes 1.0 as 1: the digest reads them alike."""
    job = onboard._bind_resolve_rules(SRC_SYS, [_rule(confidence=1.0)], "job-6")
    sent = _rule(confidence=1)
    ctx = onboard._make_provision_state(_body([sent], resolve_job_id=job))
    assert ctx.checkpoint_rule_ids == frozenset({id(ctx.rules[0])})


def test_the_resolve_job_keeps_its_rules_past_the_status_read(monkeypatch, bindings):
    """REPRODUCTION: the job's record was the only copy, deleted on the first
    ``done`` read. Now its answer carries ``resolve_job_id`` and the rules it
    listed stay bound after the read."""
    listed = _rule()
    monkeypatch.setattr(onboard, "_resolve_jobs", JobStore("resolve", table=None))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: {"rules": []})
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules",
                        lambda body, result: {**result, "rules": [dict(listed)]})
    monkeypatch.setattr(onboard, "_own_candidates_only", lambda _sid, result, *_a: result)
    monkeypatch.setattr(onboard, "_entity_layer_concepts_of", lambda _result: set())
    monkeypatch.setattr("agents.resolve_gates.gate_overlap_rules", lambda result: result)
    monkeypatch.setattr("agents.resolve_site_gate.gate_cross_site_rules",
                        lambda _sid, result, _cat: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged", lambda _job, _body: {})
    monkeypatch.setattr(onboard, "_removal_sets", lambda _sid: (set(), set()))
    monkeypatch.setattr(onboard, "_join_candidate_summaries", lambda _disc: [])
    onboard._run_resolve_job("job-7", OnboardResolveRequest(system_id=SRC_SYS, mappings=[]))
    answer = asyncio.run(onboard.onboard_resolve_status("job-7"))
    assert answer["status"] == "done" and answer["resolve_job_id"] == "job-7"
    assert onboard._resolve_jobs.get("job-7") is None  # the job record is consumed
    (sent,) = answer["rules"]
    digests, why = onboard._bound_resolve_digests(SRC_SYS, answer["resolve_job_id"])
    assert why == "" and onboard._resolve_rule_digest(sent) in digests


def test_a_binding_written_on_one_replica_is_read_on_another(monkeypatch):
    """The store is the shared onboarding-sessions table, as every JobStore."""
    table = _SharedTable()
    rules = [_rule()]
    monkeypatch.setattr(onboard, "_resolve_results", JobStore("resolve-result", table=table))
    onboard._bind_resolve_rules(SRC_SYS, rules, "job-8")
    monkeypatch.setattr(onboard, "_resolve_results", JobStore("resolve-result", table=table))
    digests, why = onboard._bound_resolve_digests(SRC_SYS, "job-8")
    assert why == "" and digests == {onboard._resolve_rule_digest(rules[0])}


def test_the_resolve_job_id_is_bounded():
    """It is part of a shared-table key: an oversized one is refused at the boundary."""
    with pytest.raises(ValidationError):
        _body([], resolve_job_id="j" * 129)


def test_a_binding_lasts_as_long_as_the_schema_it_was_resolved_on():
    """The checkpoint can stay open as long as an onboarding: the binding
    lives as long as the Discover provenance the same provision rejoins."""
    assert isinstance(_APP_STORE, JobStore)
    assert _APP_STORE._ttl_seconds == onboard.SCHEMA_PROVENANCE_TTL_SECONDS


def _merges_of(world, body) -> list:
    _provision(body)
    return _merges(world)
