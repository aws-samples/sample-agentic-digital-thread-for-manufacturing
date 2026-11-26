# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The wizard's Resolve proposes a measured pair the model left out.

The concept pass measures every field pair that shares a concept and shows the
pairs that share values as **Value Overlap Evidence**, then asks the model for
rules in ONE call. Only the model's rules reached the gates, so a pair the app
measured above the evidence bar was never a candidate when the model left it
out of its answer: on one onboarding the checkpoint proposed nothing for a
technician-login pair with four shared values, listed in its own evidence
table, and a second onboarding of the same system proposed it.

Now every measured pair no rule names, read unchanged, that meets the measured
evidence bar (``resolve_gates.rule_rejection``: shared count, overlap, shape,
whole values, never a measure or a point in time) becomes a candidate, marked
``resolve_gates.MEASURED_PAIR_KEY``. It then goes through the same gates as a
model rule: a category or status concept is withheld, a pair across plants is
withheld unless a confirmed All Sites authority vouches. Provision writes such
a rule kept at the checkpoint with an origin that says where it came from.
Fixture names are generic.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.resolve_gates as resolve_gates  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID as CHAT_SID, _chat_map_answer, _chat_provision_to_the_end, world)

NEW = "site-cmms"            # serves Plant A
SAME = "mes-a"               # Plant A
OTHER_B = "mes-b"            # Plant B
OTHER_C = "mes-c"            # Plant C
CORP = "corp-erp"            # All Sites
LOGINS = ["tech_1", "tech_2", "tech_3", "lead"]
CODES = ["JAM", "OVERLOAD", "SLIP", "COMMS", "POWER", "LEAK"]


class _Catalog:
    def __init__(self, entries):
        self._entries = {e["id"]: e for e in entries}

    def get(self, sid):
        return self._entries.get(sid)

    def get_all(self):
        return list(self._entries.values())


def _entries():
    return [
        {"id": NEW, "name": "Site CMMS", "plant": "Plant A", "system_type": "CMMS"},
        {"id": SAME, "name": "Plant A MES", "plant": "Plant A", "system_type": "MES"},
        {"id": OTHER_B, "name": "Plant B MES", "plant": "Plant B", "system_type": "MES"},
        {"id": OTHER_C, "name": "Plant C MES", "plant": "Plant C", "system_type": "MES"},
        {"id": CORP, "name": "Corporate ERP", "plant": "All Sites", "system_type": "ERP"},
    ]


def _ev(concept, source, target_system, target, examples, ratio, unchanged=None):
    n = len(examples)
    return {"concept": concept, "source_field": source, "target_system": target_system,
            "target_field": target, "overlap_count": n,
            "unchanged_count": n if unchanged is None else unchanged,
            "ratio": ratio, "examples": list(examples)}


def _model(concept, source, target_system, target):
    return {"concept": concept, "source_system": NEW, "source_field": source,
            "source_pattern": "", "target_system": target_system, "target_field": target,
            "target_pattern": "", "confidence": 0.8, "reasoning": "values match"}


#: What the concept pass answered on the onboarding that proposed nothing: the
#: model sent a failure-code rule and a start-time rule, and left the
#: technician-login pairs out, though they are measured evidence.
MODEL_RULES = [
    _model("failure-mode", "work_orders.failure_code", SAME, "stops.reason"),
    _model("event-time", "work_orders.actual_start", SAME, "stops.start_time"),
]
EVIDENCE = [
    _ev("event-time", "work_orders.actual_start", SAME, "stops.start_time",
        ["2026-01-02 03:04:05", "2026-01-03 03:04:05", "2026-01-04 03:04:05"], 1.0),
    _ev("failure-mode", "work_orders.failure_code", SAME, "stops.reason", CODES, 0.86),
    _ev("failure-mode", "problems.code_name", SAME, "stops.reason", CODES, 0.75),
    _ev("criticality", "work_orders.priority", CORP, "routing.priority", ["1", "2"], 1.0),
    _ev("personnel", "work_log.author", OTHER_B, "stops.resolved_by", LOGINS[2:3], 1.0),
    _ev("personnel", "work_log.author", SAME, "stops.resolved_by", LOGINS, 0.8),
    _ev("personnel", "work_log.author", OTHER_C, "stops.resolved_by", LOGINS[1:], 1.0),
    _ev("personnel", "work_orders.crew", SAME, "stops.resolved_by", ["CREW-A"], 0.33),
]


def _run(monkeypatch, found, job="job-measured"):
    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    onboard._run_resolve_job(job, OnboardResolveRequest(system_id=NEW, mappings=[]))
    out = onboard._resolve_jobs.pop(job)
    assert out["status"] == "done", out
    return out["result"]


def _pair(row):
    return (row.get("source_field"), row.get("target_system"), row.get("target_field"))


def test_a_measured_pair_the_model_left_out_is_proposed(monkeypatch):
    result = _run(monkeypatch, {"rules": copy.deepcopy(MODEL_RULES), "value_mappings": [],
                                "evidence": copy.deepcopy(EVIDENCE)})
    (rule,) = result["rules"]
    assert _pair(rule) == ("work_log.author", SAME, "stops.resolved_by")
    assert rule["concept"] == "personnel" and rule["source_system"] == NEW
    assert (rule["source_pattern"], rule["target_pattern"]) == ("", "")
    assert rule["confidence"] == 0.8   # the measured overlap, not a model number
    assert "4 values" in rule[resolve_gates.MEASURED_PAIR_KEY]


def test_the_proposed_pair_goes_through_the_same_gates(monkeypatch):
    result = _run(monkeypatch, {"rules": copy.deepcopy(MODEL_RULES), "value_mappings": [],
                                "evidence": copy.deepcopy(EVIDENCE)})
    reasons = {_pair(r): r["rejected_reason"] for r in result["rejected_rules"]}
    # Another plant's MES that passes the count: withheld by the site gate.
    assert reasons[("work_log.author", OTHER_C, "stops.resolved_by")].startswith(
        "different plants (Plant A, Plant C)")
    # A category concept the model did not send: withheld by the category gate.
    assert reasons[("problems.code_name", SAME, "stops.reason")] == (
        "concept 'failure-mode' names a category or status, not one record")
    # The model's own rules keep their reasons, listed once.
    assert reasons[("work_orders.failure_code", SAME, "stops.reason")].startswith(
        "concept 'failure-mode'")
    assert reasons[("work_orders.actual_start", SAME, "stops.start_time")].startswith(
        "concept 'event-time' names a measure or point in time")
    assert len(result["rejected_rules"]) == len(reasons) == 4
    # Below the bar, never a candidate: one shared login, one crew value, two
    # class values. They stay in the evidence table only.
    for pair in [("work_log.author", OTHER_B, "stops.resolved_by"),
                 ("work_orders.crew", SAME, "stops.resolved_by"),
                 ("work_orders.priority", CORP, "routing.priority")]:
        assert pair not in reasons
    assert len(result["evidence"]) == len(EVIDENCE)


def test_a_pair_the_model_proposed_is_not_proposed_again(monkeypatch):
    own = _model("operator", "work_log.author", SAME, "stops.resolved_by")
    own.update(source_pattern="*", target_pattern="*")
    result = _run(monkeypatch, {"rules": [own], "value_mappings": [], "evidence": [
        _ev("personnel", "work_log.author", SAME, "stops.resolved_by", LOGINS, 0.8)]})
    (rule,) = result["rules"]
    assert rule["concept"] == "operator"
    assert resolve_gates.MEASURED_PAIR_KEY not in rule


def test_a_pair_below_the_measured_bar_is_not_proposed(monkeypatch):
    result = _run(monkeypatch, {"rules": [], "value_mappings": [], "evidence": [
        _ev("part", "parts.code", SAME, "items.code", ["A1", "A2", "A3"], 0.3),
        _ev("part", "parts.ref", SAME, "items.ref", ["1.50", "2.25", "3.75"], 1.0),
        _ev("part", "parts.alt", SAME, "items.alt", ["A1", "A2", "A3"], 1.0, unchanged=1)]})
    assert result["rules"] == []
    assert not result.get("rejected_rules")


def test_a_class_value_pair_above_the_count_is_still_withheld(monkeypatch):
    result = _run(monkeypatch, {"rules": [], "value_mappings": [], "evidence": [
        _ev("criticality", "assets.priority", SAME, "lines.priority",
            ["HIGH", "LOW", "MEDIUM"], 1.0)]})
    assert result["rules"] == []
    (row,) = result["rejected_rules"]
    assert row["rejected_reason"] == (
        "concept 'criticality' names a category or status, not one record")


#: Two tables of the new system both carry ``author``, each measured against
#: the same plant's ``resolved_by``.
TWO_AUTHORS = [
    _ev("personnel", "work_log.author", SAME, "stops.resolved_by", LOGINS, 0.8),
    _ev("personnel", "archive_log.author", SAME, "stops.resolved_by", LOGINS, 0.8),
]


def _proposed(result):
    return [_pair(r) for r in result["rules"] if resolve_gates.MEASURED_PAIR_KEY in r]


def test_an_ambiguous_shorthand_rule_suppresses_no_measured_pair(monkeypatch):
    """``author -> resolved_by`` could be either measured pair, so the gate
    withholds it as ambiguous; it names neither, so both are proposed, as
    when the model sends no rule at all."""
    shorthand = _model("personnel", "author", SAME, "resolved_by")
    with_rule = _run(monkeypatch, {"rules": [shorthand], "value_mappings": [],
                                   "evidence": copy.deepcopy(TWO_AUTHORS)}, job="job-short")
    without = _run(monkeypatch, {"rules": [], "value_mappings": [],
                                 "evidence": copy.deepcopy(TWO_AUTHORS)}, job="job-none")
    both = [("work_log.author", SAME, "stops.resolved_by"),
            ("archive_log.author", SAME, "stops.resolved_by")]
    assert _proposed(with_rule) == _proposed(without) == both
    assert [r for r in with_rule["rules"] if resolve_gates.MEASURED_PAIR_KEY not in r] == []
    (withheld,) = with_rule["rejected_rules"]
    assert _pair(withheld) == ("author", SAME, "resolved_by")
    assert withheld["rejected_reason"].startswith("the measured evidence is ambiguous: 2 measured")


def test_an_unambiguous_rule_still_suppresses_its_pair(monkeypatch):
    """A shorthand that only one measured pair ends with names it, and so
    does a qualified rule among two: that pair is the model's, the other is
    proposed from the measurement."""
    shorthand = _model("personnel", "author", SAME, "resolved_by")
    one = _run(monkeypatch, {"rules": [shorthand], "value_mappings": [],
                             "evidence": copy.deepcopy(TWO_AUTHORS[:1])}, job="job-one")
    assert [(_pair(r), resolve_gates.MEASURED_PAIR_KEY in r) for r in one["rules"]] == [
        (("author", SAME, "resolved_by"), False)]
    qualified = _model("personnel", "work_log.author", SAME, "resolved_by")
    two = _run(monkeypatch, {"rules": [qualified], "value_mappings": [],
                             "evidence": copy.deepcopy(TWO_AUTHORS)}, job="job-two")
    assert [(_pair(r), resolve_gates.MEASURED_PAIR_KEY in r) for r in two["rules"]] == [
        (("work_log.author", SAME, "resolved_by"), False),
        (("archive_log.author", SAME, "stops.resolved_by"), True)]


def test_a_pair_measured_for_a_rules_own_spelling_is_not_proposed_again():
    """A rule that spells a tag by its last segment is measured on a row
    spelled as it is (``onboard._measure_named_pairs``), which says which
    candidate pair it measured: that pair is the rule's."""
    tag = "tags.Site/Line1/CellA/Down"
    alias = dict(_ev("downtime", "tags.Down", SAME, "stops.stop_flag", LOGINS, 1.0),
                 **{resolve_gates.MEASURED_AS_KEY: {"target_system": SAME, "source_field": tag,
                                                     "target_field": "stops.stop_flag"}})
    rows = [_ev("downtime", tag, SAME, "stops.stop_flag", LOGINS, 1.0), alias]
    rule = _model("downtime", "tags.Down", SAME, "stops.stop_flag")
    assert resolve_gates.measured_pair_rules(NEW, {"rules": [rule], "evidence": rows}) == []
    # With no rule, the alias row is never a source; the candidate pair is.
    assert [_pair(r) for r in resolve_gates.measured_pair_rules(
        NEW, {"rules": [], "evidence": rows})] == [(tag, SAME, "stops.stop_flag")]


def test_two_measurements_of_one_field_pair_give_one_candidate():
    rows = [_ev("personnel", "work_log.author", SAME, "stops.resolved_by", LOGINS, 0.8),
            _ev("employee", "work_log.author", SAME, "stops.resolved_by", LOGINS, 0.8)]
    rules = resolve_gates.measured_pair_rules(NEW, {"rules": [], "evidence": rows})
    assert [r["concept"] for r in rules] == ["personnel"]


def test_a_model_cannot_write_the_mark_itself():
    forged = dict(_model("personnel", "work_log.author", SAME, "stops.resolved_by"),
                  **{resolve_gates.MEASURED_PAIR_KEY: "trust me"})
    (out,) = resolve_gates.sanitize_model_rules([forged])
    assert resolve_gates.MEASURED_PAIR_KEY not in out


# ---------------------------------------------------------------- provision's origin

class _Cat:
    def get(self, _sid):
        return None

    def get_onboarded_ids(self):
        return {SAME}


def _write(monkeypatch, rule, *, from_checkpoint=True):
    recorded = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [{"tbl": "stops"}])
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "catalog", _Cat())
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=NEW, system_name="Site CMMS",
        plant="", schema="", rules=[rule], mappings=[], connection_type="MCP Server",
        cm_items=[], committable=set(), rules_from_checkpoint=from_checkpoint)
    ctx.checkpoint_rule_ids = frozenset({id(rule)}) if from_checkpoint else frozenset()
    onboard._dp_identity_rules(ctx)
    merges = [p for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]
    if not from_checkpoint:
        return merges
    (params,) = merges
    return params


def _measured_rule():
    (rule,) = resolve_gates.measured_pair_rules(NEW, {"rules": [], "evidence": [
        _ev("personnel", "work_log.author", SAME, "stops.resolved_by", LOGINS, 0.8)]})
    return rule


def test_a_measured_pair_kept_at_the_checkpoint_says_where_it_came_from(monkeypatch):
    params = _write(monkeypatch, _measured_rule())
    assert params["octx"].startswith(onboard._MEASURED_PAIR_RULE_ORIGIN)
    assert "(LLM)" not in params["octx"]


def test_a_measured_pair_not_from_the_checkpoint_is_not_written(monkeypatch):
    """Nobody kept it at the checkpoint, so provision writes no rule for it
    (``test_seeding_writes_no_unreviewed_measured_pair``)."""
    assert _write(monkeypatch, _measured_rule(), from_checkpoint=False) == []


def test_the_origin_sentence_is_generic():
    text = onboard._MEASURED_PAIR_RULE_ORIGIN
    assert "measured" in text and "Identity Rules checkpoint" in text
    assert "\u2014" not in text and "\u2013" not in text


# ---------------------------------------------------------------- the chat's resolve and provision

def test_chat_resolve_proposes_a_measured_pair_and_provision_binds_it(world):
    """The chat's resolve_identities runs the wizard's gate pass with the
    measured pairs: the pair its model left out is proposed and stored in
    the session. The chat's provision binds the session's rules as a
    Resolve result (``onboarding_chat_lifecycle._wizard_body``), so the rule
    is written as kept at the checkpoint, with the measured-pair origin."""
    plant = world.catalog.get(CHAT_SID)["plant"]
    world.catalog.add({"id": "peer-same", "name": "Same Plant MES", "status": "onboarded",
                       "plant": plant, "system_type": "MES", "connection_type": "PostgreSQL",
                       "schema": "s_same"})
    rows = [{"sys_id": "peer-same", "sys_name": "Same Plant MES", "sys_schema": "",
             "field_name": "stop_flag", "concept": "downtime", "table_name": "stops",
             "removal_incomplete": False}]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    world.share_sampled_values()
    # The peer's catalog holds the target column (provision's target check).
    world.mp.setattr(onboard, "pg", lambda _schema, sql, *_a, **_k: {
        "rows": [{"column_name": "stop_flag"}] if "information_schema.columns" in sql else [],
        "columns": []})

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        return json.dumps({"rules": [], "value_mappings": []})   # the model leaves it out

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(CHAT_SID)
    assert "error" not in oa.map_fields(CHAT_SID)
    resolved = oa.resolve_identities(CHAT_SID)
    pair = ("get_tags.Ent/Site/Line1/StationA/Down", "peer-same", "stops.stop_flag")
    (rule,) = resolved["rules"]
    assert _pair(rule) == pair and resolve_gates.MEASURED_PAIR_KEY in rule, resolved
    assert world.store.get_session(CHAT_SID)["rules"] == resolved["rules"]

    finished = _chat_provision_to_the_end()
    assert finished["status"] == "onboarded", finished
    (body,) = world.job_bodies
    assert body.rules_from_checkpoint and body.resolve_job_id
    # The origin is the measured pair's only when provision found the rule,
    # content-exact, in the Resolve result the request names.
    (params,) = [p for c, p in world.writes if c.startswith("MERGE (ir:IdentityRule")]
    assert params["octx"].startswith(onboard._MEASURED_PAIR_RULE_ORIGIN)


# ---------------------------------------------------------------- affinity: wizard and chat alike

#: Two fields with different concepts whose values share an identifier core
#: (``B-2847`` and ``LOT-2026-02847``): no concept pair is measured, so only
#: the identifier-core affinity pass finds the rule.
AFF_NEW, AFF_PEER = "run-mes", "lot-ledger"
AFF_RUNS = [f"B-{n}" for n in range(2800, 2951)]
AFF_LOTS = [f"LOT-2026-0{n}" for n in range(2847, 2897)]
AFF_MAPPINGS = [
    {"table": "production_runs", "field": "batch_number", "concept": "batch",
     "confidence": 0.95, "mappings": [{"concept": "batch", "role": "identifier"}]},
    {"table": "production_runs", "field": "good_units", "concept": "actual-quantity",
     "confidence": 0.9, "mappings": [{"concept": "actual-quantity", "role": "measure"}]},
]
AFF_PEER_ROWS = [{"sys_id": AFF_PEER, "sys_schema": "lot_ledger", "table_name": "orders",
                  "field_name": "lot_ref", "concept": "lot-number", "role": "identifier"}]


class _AffinityCatalog:
    def __init__(self):
        self._entries = {
            AFF_NEW: {"id": AFF_NEW, "name": "Run MES", "connection_type": "PostgreSQL",
                      "schema": "run_mes", "system_type": "MES"},
            AFF_PEER: {"id": AFF_PEER, "name": "Lot Ledger", "connection_type": "PostgreSQL",
                       "schema": "lot_ledger", "system_type": "ERP"},
        }

    def get(self, sid):
        return self._entries.get(sid)

    def get_all(self):
        return list(self._entries.values())

    def get_onboarded_ids(self):
        return {AFF_PEER}


def _affinity_world(monkeypatch):
    """The graph, catalog and samples both resolves read."""
    import agents.resolve_evidence as resolve_evidence
    cat = _AffinityCatalog()
    for mod in (onboard, oa):
        monkeypatch.setattr(mod, "catalog", cat)
    monkeypatch.setattr(onboard, "neptune_query", lambda cypher, params=None: copy.deepcopy(
        AFF_PEER_ROWS) if "r.role = 'identifier'" in cypher else [])
    monkeypatch.setattr(oa, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: cat.get_all())
    samples = {(AFF_NEW, "production_runs", "batch_number"): AFF_RUNS,
               (AFF_PEER, "orders", "lot_ref"): AFF_LOTS}

    def _sample(*, system_id, table, field, **_kw):
        return list(samples.get((system_id, table, field), []))
    monkeypatch.setattr(resolve_evidence, "sample_field_values", _sample)


def test_chat_resolve_runs_the_wizards_affinity_pass(monkeypatch):
    """The model sends no rule and no concept pair is measured; the
    identifier-core affinity pass finds the rule. The wizard's resolve job
    and the chat's gate pass propose the same one."""
    _affinity_world(monkeypatch)
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: {
        "rules": [], "value_mappings": [], "evidence": []})
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    onboard._run_resolve_job("job-aff", OnboardResolveRequest(
        system_id=AFF_NEW, mappings=copy.deepcopy(AFF_MAPPINGS)))
    wizard = onboard._resolve_jobs.pop("job-aff")["result"]["rules"]
    chat, withheld, _partial = oa._gate_chat_rules(AFF_NEW, [], copy.deepcopy(AFF_MAPPINGS), [])
    assert [(r["source_field"], r["target_system"], r["target_field"], r["source_pattern"],
             r["target_pattern"]) for r in wizard] == [
        ("production_runs.batch_number", AFF_PEER, "orders.lot_ref", "B-{id}", "LOT-2026-0{id}")]
    assert chat == wizard
    assert withheld == []
