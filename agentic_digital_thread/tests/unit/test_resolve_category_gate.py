# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identity rules are never proposed between category or status fields.

Live finding (fresh workshop accounts, 2026-10-18): onboarding a maintenance
system proposed an identity rule from its work orders' ``failure_code`` to an
MES's ``downtime_reason`` (concept ``failure-mode``) on every fresh account.
Both columns hold the same reason codes, so the pair passed the overlap gate,
but a reason code names a KIND of stop, not one record: two rows sharing a code
are two different stops. The rule a person must keep seeing is the maintenance
log ``author`` to the MES ``resolved_by`` (concept operator or personnel): a
login names one person in both systems.

``resolve_gates`` now withholds a rule whose concept names a category or
status (mode, type, status, state, category, class, reason, result, kind,
disposition, criticality, priority, severity) on every path that proposes
rules: the model's rules, the identifier-core affinity rules (a category is no
identity however it was found) and the chat's ``resolve_identities``. The
withheld rule carries a reason the checkpoint shows. Fixture names are generic.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents import resolve_gates  # noqa: E402
from agents.catalog import CORE_CONCEPT_DEFINITIONS  # noqa: E402
from agents.identity_affinity import AFFINITY_METHOD, EXACT_METHOD  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, world)

NEW, PEER = "site-cmms", "site-mes"
CODES = ["ALIGN-01", "BEARING-02", "COOLANT-03", "DRIFT-04", "NOISE-05"]
LOGINS = ["tech_01", "tech_04", "tech_07", "tech_09"]

#: Every core concept the gate classifies as a category or status. Pinned so
#: a change to the token set that would also catch an identity concept (or
#: stop catching one of these) fails here and has to be argued.
CORE_CATEGORY_CONCEPTS = {
    "asset-class", "criticality", "defect-type", "failure-mode", "inspection-result",
    "lifecycle-state", "machine-state", "order-status", "product-category",
}
IDENTITY_CONCEPTS = ("equipment-id", "batch", "lot-number", "work-order", "production-run",
                     "production-order", "operator", "personnel", "customer", "supplier",
                     "shipment", "sales-order", "purchase-order", "material", "product",
                     "non-conformance", "inspection", "maintenance-event", "spare-part")


def _rule(concept, source_field, target_field, *, target=PEER, **extra):
    return {"concept": concept, "source_field": source_field, "source_pattern": "*",
            "target_system": target, "target_field": target_field, "target_pattern": "*",
            "confidence": 0.8, "reasoning": "measured overlap", **extra}


def _evidence(concept, source_field, target_field, examples, *, target=PEER):
    return {"concept": concept, "source_field": source_field, "target_system": target,
            "target_field": target_field, "overlap_count": len(examples),
            "unchanged_count": len(examples), "examples": list(examples), "ratio": 1.0}


FAILURE = _rule("failure-mode", "work_orders.failure_code", "downtime_events.downtime_reason")
FAILURE_EV = _evidence("failure-mode", "work_orders.failure_code",
                       "downtime_events.downtime_reason", CODES)
AUTHOR = _rule("operator", "work_order_log.author", "downtime_events.resolved_by")
AUTHOR_EV = _evidence("operator", "work_order_log.author", "downtime_events.resolved_by", LOGINS)
CATEGORY_REASON = "concept 'failure-mode' names a category or status, not one record"


# ── the gate itself ──────────────────────────────────────────────────────────

def _resolve_gates(result, entity_concepts=frozenset()):
    """The model's rules through the gates in the resolve job's order: the
    category gate, then the overlap gate."""
    return resolve_gates.gate_overlap_rules(
        resolve_gates.gate_category_rules(result, entity_concepts))


def test_a_shared_reason_code_rule_is_withheld_with_a_readable_reason():
    out = _resolve_gates({"rules": [FAILURE, AUTHOR], "evidence": [FAILURE_EV, AUTHOR_EV]})
    assert out["rules"] == [AUTHOR]
    assert [r["source_field"] for r in out["rejected_rules"]] == ["work_orders.failure_code"]
    assert out["rejected_rules"][0]["rejected_reason"] == CATEGORY_REASON


@pytest.mark.parametrize("concept", sorted(CORE_CATEGORY_CONCEPTS) + [
    "downtime-reason", "reason_code", "OrderType", "work order status", "stop-kind",
    "disposition", "priority", "severity", "quality-result"])
def test_category_and_status_concepts_are_never_proposed(concept):
    rule = _rule(concept, "a.some_code", "b.some_code")
    strong = [_evidence(concept, "a.some_code", "b.some_code", CODES)]
    out = _resolve_gates({"rules": [rule], "evidence": strong})
    assert out["rules"] == []
    assert out["rejected_rules"][0]["rejected_reason"] == (
        f"concept '{concept}' names a category or status, not one record")


def test_the_gate_catches_exactly_the_core_category_concepts():
    caught = {c for c in CORE_CONCEPT_DEFINITIONS if resolve_gates.is_category_or_status_concept(c)}
    assert caught == CORE_CATEGORY_CONCEPTS


@pytest.mark.parametrize("concept", IDENTITY_CONCEPTS)
def test_identity_concepts_still_pass(concept):
    rule = _rule(concept, "a.ref_id", "b.ref_id")
    strong = [_evidence(concept, "a.ref_id", "b.ref_id", ["R-1001", "R-1002", "R-1003"])]
    assert not resolve_gates.is_category_or_status_concept(concept)
    assert _resolve_gates({"rules": [rule], "evidence": strong})["rules"] == [rule]


@pytest.mark.parametrize("rule, ev", [
    (AUTHOR, AUTHOR_EV),
    (_rule("personnel", "work_order_log.author", "downtime_events.resolved_by"),
     _evidence("personnel", "work_order_log.author", "downtime_events.resolved_by", LOGINS)),
    (_rule("batch", "batches.batch_number", "production_runs.batch_number"),
     _evidence("batch", "batches.batch_number", "production_runs.batch_number",
               ["B-24017", "B-24018", "B-24021"])),
    (_rule("lot-number", "material_lots.lot_number", "lot_usage.lot_id"),
     _evidence("lot-number", "material_lots.lot_number", "lot_usage.lot_id",
               ["L-88120", "L-88131", "L-88140"])),
    (_rule("equipment-id", "assets.asset_tag", "machines.equipment_id"),
     _evidence("equipment-id", "assets.asset_tag", "machines.equipment_id",
               ["EQ-201", "EQ-202", "EQ-207"])),
])
def test_person_batch_lot_and_equipment_rules_are_kept(rule, ev):
    out = _resolve_gates({"rules": [rule], "evidence": [ev]})
    assert out["rules"] == [rule]
    assert out["rejected_rules"] == []


def test_gate_category_rules_reads_only_the_concept():
    """The pass for rules the overlap gate never saw (affinity, chat): no
    evidence is needed and every other rule passes untouched."""
    no_evidence_identity = _rule("equipment-id", "assets.description", "machines.label")
    earlier = {**AUTHOR, "target_system": "elsewhere", "rejected_reason": "earlier gate"}
    result = {"rules": [FAILURE, no_evidence_identity], "rejected_rules": [earlier], "x": 1}
    out = resolve_gates.gate_category_rules(copy.deepcopy(result))
    assert out["rules"] == [no_evidence_identity]
    assert out["rejected_rules"] == [earlier, {**FAILURE, "rejected_reason": CATEGORY_REASON}]
    assert out["x"] == 1


def test_gate_category_rules_returns_the_result_itself_when_nothing_is_withheld():
    result = {"rules": [AUTHOR]}
    assert resolve_gates.gate_category_rules(result) is result
    empty = {"rules": []}
    assert resolve_gates.gate_category_rules(empty) is empty
    assert resolve_gates.gate_category_rules({}) == {}


def test_a_measure_concept_keeps_the_measure_reason():
    rule = _rule("cost-category", "a.some_code", "b.some_code")
    reason = resolve_gates.rule_rejection(rule, [_evidence("cost-category", "a.some_code",
                                                           "b.some_code", CODES)])
    assert reason.startswith("concept 'cost-category' names a measure or point in time")


# ── the wizard's resolve job ─────────────────────────────────────────────────

class _Catalog:
    def __init__(self, entries):
        self._entries = {e["id"]: e for e in entries}

    def get(self, sid):
        return self._entries.get(sid)

    def get_all(self):
        return list(self._entries.values())


def _entries():
    return [{"id": NEW, "name": "Site CMMS", "plant": "Plant A", "system_type": "CMMS"},
            {"id": PEER, "name": "Site MES", "plant": "Plant A", "system_type": "MES"}]


def _run_job(monkeypatch, found, affinity_rules=(), query=lambda *_a, **_k: []):
    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))

    def _affinity(body, result):
        from agents.identity_affinity import merge_affinity_rules
        result["rules"] = merge_affinity_rules(list(result.get("rules") or []),
                                               [copy.deepcopy(r) for r in affinity_rules])
        return result

    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", _affinity)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    onboard._run_resolve_job("job-cat", OnboardResolveRequest(system_id=NEW, mappings=[]))
    job = onboard._resolve_jobs.pop("job-cat")
    assert job["status"] == "done", job
    return job["result"]


def test_resolve_job_withholds_the_model_rule_and_keeps_the_author_rule(monkeypatch):
    result = _run_job(monkeypatch, {"rules": [FAILURE, AUTHOR], "value_mappings": [],
                                    "evidence": [FAILURE_EV, AUTHOR_EV]})
    assert [r["concept"] for r in result["rules"]] == ["operator"]
    assert [(r["source_field"], r["rejected_reason"]) for r in result["rejected_rules"]] == [
        ("work_orders.failure_code", CATEGORY_REASON)]
    assert len(result["evidence"]) == 2          # the measured overlap is still shown


def test_resolve_job_withholds_an_affinity_rule_on_a_category_concept_and_lists_it_once(
        monkeypatch):
    """With the model's rule withheld, the affinity merge adds its own rule
    for the same pair; a category is withheld however it was found. The
    checkpoint lists the candidate ONCE, with the first reason (it shows no
    method, so two rows read as two candidates). The affinity rule on an
    identity concept is kept."""
    exact = {**FAILURE, "method": EXACT_METHOD, "transform": "identity", "shared_count": 5,
             "source_pattern": "", "target_pattern": ""}
    core = _rule("equipment-id", "assets.asset_tag", "machines.equipment_id",
                 method=AFFINITY_METHOD, source_pattern="EQ-{n}", target_pattern="M-{n}")
    result = _run_job(monkeypatch, {"rules": [FAILURE, AUTHOR], "value_mappings": [],
                                    "evidence": [FAILURE_EV, AUTHOR_EV]},
                      affinity_rules=[exact, core])
    assert [(r["concept"], r.get("method")) for r in result["rules"]] == [
        ("operator", None), ("equipment-id", AFFINITY_METHOD)]
    withheld = [(r["source_field"], r.get("method"), r["rejected_reason"])
                for r in result["rejected_rules"]]
    assert withheld == [("work_orders.failure_code", None, CATEGORY_REASON)]


@pytest.mark.parametrize("model_spelling", [
    ("work_orders.failure_code", "downtime_events.downtime_reason"),
    ("failure_code", "downtime_reason"),
    (" Work_Orders.Failure_Code ", "DOWNTIME_EVENTS.downtime_reason"),
], ids=["qualified", "bare", "case-and-padding"])
def test_one_withheld_row_however_the_model_spelled_the_pair(monkeypatch, model_spelling):
    """The model's rule and the affinity rule name the same candidate in
    different spellings (table dropped, case, padding): still one row."""
    src, tgt = model_spelling
    model_rule = _rule("failure-mode", src, tgt)
    exact = {**FAILURE, "method": EXACT_METHOD, "transform": "identity", "shared_count": 5,
             "source_pattern": "", "target_pattern": ""}
    result = _run_job(monkeypatch, {"rules": [model_rule, AUTHOR], "value_mappings": [],
                                    "evidence": [FAILURE_EV, AUTHOR_EV]},
                      affinity_rules=[exact])
    assert [r["concept"] for r in result["rules"]] == ["operator"]
    assert [(r["source_field"], r["rejected_reason"]) for r in result["rejected_rules"]] == [
        (src, CATEGORY_REASON)]


def test_two_different_category_candidates_are_both_listed():
    """Deduplication is per candidate: another field pair, or the same
    columns in another table, is its own row."""
    other_pair = _rule("failure-mode", "work_orders.cause_code", "downtime_events.downtime_reason")
    other_table = _rule("failure-mode", "archive_orders.failure_code",
                        "downtime_events.downtime_reason")
    first = {**FAILURE, "rejected_reason": CATEGORY_REASON}
    out = resolve_gates.gate_category_rules({"rules": [other_pair, other_table],
                                             "rejected_rules": [first]})
    assert [r["source_field"] for r in out["rejected_rules"]] == [
        "work_orders.failure_code", "work_orders.cause_code", "archive_orders.failure_code"]


# ── a bare withheld row never hides two distinct table-qualified candidates ──

#: The model's rule with its tables dropped, and two candidates in different
#: tables whose columns share those names.
BARE = _rule("failure-mode", "failure_code", "reason_code")
LIVE = _rule("failure-mode", "live.failure_code", "stops.reason_code")
ARCHIVE = _rule("failure-mode", "archive.failure_code", "history.reason_code")


def _withheld(rule):
    return {**rule, "rejected_reason": CATEGORY_REASON}


def test_a_bare_prior_row_and_two_same_name_candidates_in_other_tables_stay_two_rows():
    """The bare row could be either pair, so it stands for neither: both
    table-qualified candidates are listed and the bare row is not."""
    out = resolve_gates.gate_category_rules({"rules": [LIVE, ARCHIVE],
                                             "rejected_rules": [_withheld(BARE)]})
    assert out["rules"] == []
    assert [(r["source_field"], r["target_field"]) for r in out["rejected_rules"]] == [
        ("live.failure_code", "stops.reason_code"),
        ("archive.failure_code", "history.reason_code")]


def test_a_bare_rule_after_two_listed_candidates_adds_no_row():
    out = resolve_gates.gate_category_rules({"rules": [BARE],
                                             "rejected_rules": [_withheld(LIVE), _withheld(ARCHIVE)]})
    assert [r["source_field"] for r in out["rejected_rules"]] == [
        "live.failure_code", "archive.failure_code"]


def test_a_bare_row_with_one_listed_and_one_new_candidate_hides_neither():
    out = resolve_gates.gate_category_rules({"rules": [ARCHIVE],
                                             "rejected_rules": [_withheld(LIVE), _withheld(BARE)]})
    assert [r["source_field"] for r in out["rejected_rules"]] == [
        "live.failure_code", "archive.failure_code"]


def test_a_bare_row_that_names_one_candidate_is_still_that_candidate():
    """The loose reading collapses only when it resolves to exactly one
    candidate: one row, the first-listed spelling and reason kept."""
    first = {**BARE, "rejected_reason": "earlier gate"}
    out = resolve_gates.gate_category_rules({"rules": [LIVE], "rejected_rules": [first]})
    assert out["rejected_rules"] == [first]


def test_the_overlap_gate_lists_a_rule_the_model_sent_twice_once():
    weak = _rule("work-order", "jobs.job_ref", "orders.order_ref")
    ev = _evidence("work-order", "jobs.job_ref", "orders.order_ref", ["J-1"])
    out = resolve_gates.gate_overlap_rules({"rules": [weak, dict(weak)], "evidence": [ev]})
    assert out["rules"] == []
    assert len(out["rejected_rules"]) == 1
    assert out["rejected_rules"][0]["rejected_reason"].startswith("only 1 shared value(s)")


def test_rows_without_a_field_pair_are_never_merged():
    out = resolve_gates.gate_overlap_rules({"rules": ["not a rule", 7], "evidence": []})
    assert [r["rejected_reason"] for r in out["rejected_rules"]] == ["malformed rule"] * 2


# ── entity-layer concepts are records, whatever their names say ─────────────

#: A concept minted from a table name (``onboard._kebab_concept``), stored
#: with ``layer = 'entity'``: a table of state changes, or of status changes.
#: Each name holds a blocked token on its own (``state``, ``status``); a
#: plural such as ``results`` is not a blocked token, so it needs no exemption.
MINTED_ENTITIES = ("state-transitions", "status-changes")


def test_the_minted_names_are_what_onboarding_mints_from_those_tables():
    assert [onboard._kebab_concept(t)[0] for t in ("state_transitions", "status_changes")] == list(
        MINTED_ENTITIES)
    assert not resolve_gates.is_category_or_status_concept("inspection-results")


def _entity_layer_query(entity_ids, calls=None):
    """A graph that records ``entity_ids`` as entity-layer concepts."""
    def query(cypher, params=None):
        if calls is not None:
            calls.append((cypher, params))
        if "layer" in cypher and params and "ids" in params:
            return [{"id": i} for i in params["ids"] if i in entity_ids]
        return []
    return query


@pytest.mark.parametrize("concept", MINTED_ENTITIES)
def test_a_minted_entity_concept_is_not_a_category(concept):
    assert resolve_gates.is_category_or_status_concept(concept)          # by its name alone
    assert not resolve_gates.is_category_or_status_concept(
        concept, entity_concepts=frozenset({concept}))
    rule = _rule(concept, "results.result_id", "checks.result_id")
    out = resolve_gates.gate_category_rules({"rules": [rule]},
                                            entity_concepts=frozenset({concept}))
    assert out["rules"] == [rule]


def test_a_core_category_concept_is_withheld_even_when_named_an_entity():
    """The core vocabulary is decided by its own list, not by the graph."""
    assert resolve_gates.is_category_or_status_concept(
        "failure-mode", entity_concepts=frozenset({"failure-mode"}))
    assert not resolve_gates.is_category_or_status_concept(
        "equipment-id", entity_concepts=frozenset())


def test_entity_layer_concepts_reads_only_the_names_the_token_rule_would_withhold():
    calls: list = []
    found = resolve_gates.entity_layer_concepts(
        ["state-transitions", "failure-mode", "equipment-id", "downtime-reason", "",
         "status-changes", None, "inspection-results"],
        _entity_layer_query({"state-transitions", "status-changes"}, calls))
    assert found == frozenset({"state-transitions", "status-changes"})
    assert len(calls) == 1
    assert sorted(calls[0][1]["ids"]) == ["downtime-reason", "state-transitions",
                                          "status-changes"]
    no_calls: list = []
    assert resolve_gates.entity_layer_concepts(
        ["failure-mode", "equipment-id"], _entity_layer_query(set(), no_calls)) == frozenset()
    assert no_calls == []


def test_a_failed_entity_layer_read_lets_the_name_rule_decide():
    def broken(*_a, **_k):
        raise RuntimeError("graph down")
    assert resolve_gates.entity_layer_concepts(["state-transitions"], broken) == frozenset()


@pytest.mark.parametrize("concept", MINTED_ENTITIES)
def test_resolve_job_keeps_a_rule_on_a_minted_entity_concept(monkeypatch, concept):
    rule = _rule(concept, "results.result_id", "checks.result_id")
    ev = _evidence(concept, "results.result_id", "checks.result_id",
                   ["R-1001", "R-1002", "R-1003"])
    result = _run_job(monkeypatch, {"rules": [rule, FAILURE], "value_mappings": [],
                                    "evidence": [ev, FAILURE_EV]},
                      query=_entity_layer_query(set(MINTED_ENTITIES)))
    assert [r["concept"] for r in result["rules"]] == [concept]
    assert [r["concept"] for r in result["rejected_rules"]] == ["failure-mode"]


def test_resolve_job_withholds_the_same_name_when_the_graph_holds_no_entity(monkeypatch):
    rule = _rule("state-transitions", "results.result_id", "checks.result_id")
    ev = _evidence("state-transitions", "results.result_id", "checks.result_id",
                   ["R-1001", "R-1002", "R-1003"])
    result = _run_job(monkeypatch, {"rules": [rule], "value_mappings": [], "evidence": [ev]})
    assert result["rules"] == []
    assert result["rejected_rules"][0]["rejected_reason"] == (
        "concept 'state-transitions' names a category or status, not one record")


# ── the chat's resolve_identities ────────────────────────────────────────────

def test_chat_resolve_withholds_a_category_rule_and_stores_the_rest(world):
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer
    plant = world.catalog.get(SID)["plant"]
    world.catalog.add({"id": "peer-mes", "name": "Peer MES", "status": "onboarded",
                       "plant": plant, "system_type": "MES", "connection_type": "PostgreSQL",
                       "schema": "s_peer"})
    rows = [{"sys_id": "peer-mes", "sys_name": "Peer MES", "sys_schema": "", "field_name": f,
             "concept": c, "table_name": "stops", "removal_incomplete": False}
            for f, c in (("stop_flag", "downtime"), ("stop_reason", "failure-mode"))]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    world.share_sampled_values()

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        return json.dumps({"rules": [
            {"concept": "failure-mode", "source_field": "get_tags.Reason",
             "target_system": "peer-mes", "target_field": "stops.stop_reason"},
            {"concept": "downtime", "source_field": "get_tags.Down",
             "target_system": "peer-mes", "target_field": "stops.stop_flag"}],
            "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    # The system has the tag the model's failure-mode rule names: a rule is
    # this system's only when its source is one of its fields.
    world.discover_tables[0]["columns"].append(
        {"name": "Ent/Site/Line1/StationA/Reason", "data_type": "str"})
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    assert [r["concept"] for r in resolved["rules"]] == ["downtime"], resolved
    assert [(r["concept"], r["rejected_reason"]) for r in resolved["rejected_rules"]] == [
        ("failure-mode", CATEGORY_REASON)]
    stored = oa._store().get_session(SID)
    assert [r["concept"] for r in stored["rules"]] == ["downtime"]
