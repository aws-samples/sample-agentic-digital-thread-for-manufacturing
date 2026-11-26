# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A system's Resolve step lists only candidates that touch that system.

Live finding (workshop walk, 2026-10-19): onboarding an MCP tag hub ended
its Identity Rules checkpoint with "No identity rule met the evidence bar:
11 candidates were withheld", and every withheld candidate paired a field of
the TELEMETRY system already connected (its pseudo-table columns) with an MES
column. None named the hub. The concept pass shows the model every peer field
that shares a concept with the new system's fields, from every connected
system, so the model can propose a rule between two OTHER systems; the gates
then withheld each one for its own reason and the checkpoint listed them under
the hub. Had one passed, provision would have written it as the hub's rule.

``resolve_gates.keep_own_candidates`` leaves out every rule and withheld row
whose source field is not a field of the system being resolved (its mappings,
its identity keys, or a field its measured evidence sampled), on both paths
that propose rules: the wizard's resolve job and the chat's
``resolve_identities``. Fixture names are generic; two differently shaped
systems (a relational one and a tag hub with dotted tag paths) prove it.
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
from agents.models import OnboardResolveRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, world)

HUB, CMMS = "cell-hub", "site-cmms"      # the systems being resolved (two shapes)
TELEMETRY, MES = "line-telemetry", "line-mes"
LOGINS = ["op_11", "op_14", "op_17", "op_19"]


def _rule(concept, source_field, target_field, *, target=MES, **extra):
    return {"concept": concept, "source_field": source_field, "source_pattern": "*",
            "target_system": target, "target_field": target_field, "target_pattern": "*",
            "confidence": 0.7, "reasoning": "fields share a concept", **extra}


def _evidence(source_field, target_field, examples, *, concept="operator", target=MES):
    return {"concept": concept, "source_field": source_field, "target_system": target,
            "target_field": target_field, "overlap_count": len(examples),
            "unchanged_count": len(examples), "examples": list(examples), "ratio": 1.0}


def _mapping(table, field, concept, role="identifier"):
    return {"table": table, "field": field, "concept": concept, "confidence": 0.9,
            "mappings": [{"concept": concept, "role": role, "confidence": 0.9}]}


# The tag hub: one tool table per station, columns are dotted tag paths.
HUB_OPERATOR = "Plant1.AreaB.Press3.Press3_Operator"
HUB_MAPPINGS = [
    _mapping("Press3", HUB_OPERATOR, "operator"),
    _mapping("Press3", "Plant1.AreaB.Press3.Press3_Force", "press-force", role="measure"),
]
HUB_OWN = _rule("operator", f"Press3.{HUB_OPERATOR}", "stops.resolved_by")
HUB_OWN_EV = _evidence(f"Press3.{HUB_OPERATOR}", "stops.resolved_by", LOGINS)
# What the model proposed between the two systems already connected: the
# telemetry system's pseudo-table fields paired with MES columns.
FOREIGN = [
    _rule("operator", "Spindle.Operator", "stops.resolved_by"),
    _rule("availability", "Cell.Availability", "shifts.planned_minutes"),
    _rule("cycle-time", "Spindle.CycleTime", "runs.cycle_seconds"),
]


# ── the pure filter ──────────────────────────────────────────────────────────

def _own_hub_fields():
    return onboard._resolve_own_fields(HUB_MAPPINGS)


def test_rules_and_withheld_rows_between_two_other_systems_are_left_out():
    withheld = [{**FOREIGN[0], "rejected_reason": "no measured value overlap"},
                {**_rule("operator", HUB_OPERATOR, "stops.closed_by"),
                 "rejected_reason": "only 2 shared value(s) (need >= 3)"}]
    result = {"rules": [FOREIGN[1], HUB_OWN, FOREIGN[2]], "rejected_rules": withheld,
              "evidence": [HUB_OWN_EV], "value_mappings": [], "recommendations": ["r"]}
    out = resolve_gates.keep_own_candidates(copy.deepcopy(result), _own_hub_fields())
    assert out["rules"] == [HUB_OWN]
    assert out["rejected_rules"] == [withheld[1]]
    assert (out["evidence"], out["value_mappings"], out["recommendations"]) == (
        [HUB_OWN_EV], [], ["r"])


@pytest.mark.parametrize("spelling", [
    f"Press3.{HUB_OPERATOR}",                 # as the prompt showed it
    HUB_OPERATOR,                             # the table left out
    f"{HUB}.Press3.{HUB_OPERATOR}",           # the system id in front
    f"  press3.{HUB_OPERATOR.upper()} ",      # case and padding
    "Press3_Operator",                        # the tag's last segment only
])
def test_every_spelling_of_the_systems_own_field_is_kept(spelling):
    rule = _rule("operator", spelling, "stops.resolved_by")
    out = resolve_gates.keep_own_candidates({"rules": [rule]}, _own_hub_fields())
    assert out["rules"] == [rule]


def test_a_source_table_with_a_bare_column_is_read_as_one_field():
    fields = onboard._resolve_own_fields([_mapping("work_log", "author", "operator")])
    own = _rule("operator", "author", "stops.resolved_by", source_table="work_log")
    qualified = _rule("operator", "work_log.author", "x.y", source_table="cmms.work_log")
    other = _rule("operator", "author", "stops.resolved_by", source_table="Spindle")
    assert resolve_gates.names_own_field(own, fields)
    assert resolve_gates.names_own_field(qualified, fields)
    assert not resolve_gates.names_own_field(other, fields)
    # A bare column with no table may be ours, so it is kept: only a field
    # the system has none of is left out.
    assert resolve_gates.names_own_field(_rule("operator", "author", "x.y"), fields)
    assert not resolve_gates.names_own_field(
        _rule("operator", "Spindle.author", "stops.resolved_by"), fields)


def test_a_field_its_evidence_sampled_counts_as_its_own():
    """An entity-anchored identity key read from the graph may not be among
    the mappings; the evidence row the concept pass measured for it is."""
    rule = _rule("equipment-id", "machines.asset_ref", "equipment.equipment_id")
    ev = _evidence("machines.asset_ref", "equipment.equipment_id", ["A-1", "A-2", "A-3"],
                   concept="equipment-id")
    out = resolve_gates.keep_own_candidates({"rules": [rule], "evidence": [ev]},
                                            _own_hub_fields())
    assert out["rules"] == [rule]


def test_with_no_field_known_the_result_is_returned_as_it_is():
    result = {"rules": list(FOREIGN), "rejected_rules": [{"rule": "x", "rejected_reason": "m"}]}
    assert resolve_gates.keep_own_candidates(result, []) is result
    kept = {"rules": [HUB_OWN]}
    assert resolve_gates.keep_own_candidates(kept, _own_hub_fields()) is kept


def test_a_row_without_a_source_field_is_left_to_the_gates():
    malformed = {"rule": ["not", "a", "dict"], "rejected_reason": "malformed rule"}
    result = {"rules": ["junk", FOREIGN[0]], "rejected_rules": [malformed]}
    out = resolve_gates.keep_own_candidates(result, _own_hub_fields())
    assert out["rules"] == ["junk"]
    assert out["rejected_rules"] == [malformed]


def test_own_fields_are_the_mappings_and_the_identity_keys():
    fields = onboard._resolve_own_fields(
        [_mapping("work_log", "work_log.author", "operator"),
         {"table": "", "field": "bare_col"}, {"field": None}, "junk"],
        [{"table": "assets", "entity": "equipment", "identity_fields": ["asset_num"]}])
    assert fields == [("work_log", "work_log.author"), ("", "bare_col"),
                      ("assets", "assets.asset_num")]


def test_a_shortened_tag_under_the_systems_own_table_is_its_own():
    """A tool table whose columns are slash tag paths: the model may name a
    tag by its table and last segment. That is still the system's table;
    another system's table is not."""
    fields = onboard._resolve_own_fields(
        [_mapping("get_values", "get_values.Ent/Site/Line1/CellA/Count", "count")])
    assert resolve_gates.names_own_field(_rule("count", "get_values.Count", "x.y"), fields)
    assert resolve_gates.names_own_field(
        _rule("count", "Count", "x.y", source_table="get_values"), fields)
    assert not resolve_gates.names_own_field(_rule("count", "Gauge.Count", "x.y"), fields)
    assert not resolve_gates.names_own_field(
        _rule("count", "Count", "x.y", source_table="Gauge"), fields)


# ── the field matcher checks the field, and source_system vetoes ─────────────
# A slash-path leaf is the system's field; a field is never the system's
# because one table segment matches; a stated peer source system drops a row
# whose field names collide. The cases are adversarial on purpose.

OWN_ORDERS = [("sales.orders", "sales.orders.order_id")]
OWN_TAG = [("get_values", "get_values.Ent/Site/Line1/CellA/Count")]


@pytest.mark.parametrize("own, spelling", [
    (OWN_ORDERS, "orders.order_id"),            # a table-qualified suffix
    (OWN_ORDERS, "order_id"),
    (OWN_ORDERS, "SALES.orders.order_id "),
    (OWN_ORDERS, "erp_db.sales.orders.order_id"),
    (OWN_TAG, "Count"),                         # a bare slash-path leaf
    (OWN_TAG, "CellA/Count"),
    (OWN_TAG, "Ent/Site/Line1/CellA/Count"),
    (OWN_TAG, "get_values.Count"),
    (OWN_TAG, "get_values.CellA/Count"),
], ids=["table-suffix", "bare-column", "case-padding", "qualifier-in-front", "bare-tag-leaf",
        "tag-path-suffix", "tag-path-whole", "table-and-leaf", "table-and-path-suffix"])
def test_a_spelling_of_the_systems_own_field_is_kept(own, spelling):
    assert resolve_gates.names_own_field(_rule("ref", spelling, "x.y"), own)


@pytest.mark.parametrize("own, rule", [
    (OWN_ORDERS, _rule("ref", "archive.orders.other_column", "x.y")),   # a shared table, another column
    (OWN_ORDERS, _rule("ref", "archive.orders.order_id", "x.y")),
    (OWN_ORDERS, _rule("ref", "orders.other_column", "x.y")),
    (OWN_ORDERS, _rule("ref", "other_column", "x.y", source_table="orders")),
    (OWN_ORDERS, _rule("ref", "order_id", "x.y", source_table="archive.orders")),
    (OWN_TAG, _rule("ref", "get_values.Speed", "x.y")),
    (OWN_TAG, _rule("ref", "Site/Count", "x.y")),
    (OWN_TAG, _rule("ref", "Ount", "x.y")),
], ids=["foreign-schema-other-column", "foreign-schema-same-column", "own-table-other-column",
        "source-table-other-column", "source-table-foreign-schema", "own-tool-other-tag",
        "slash-path-not-a-suffix", "part-of-a-segment"])
def test_a_field_is_never_the_systems_because_one_table_segment_matches(own, rule):
    assert not resolve_gates.names_own_field(rule, own)


def test_a_row_whose_source_system_names_a_peer_is_left_out_though_its_field_collides():
    """Two systems each have ``orders.order_id``; the field
    cannot tell them apart, the stated source system can."""
    own = [("orders", "orders.order_id")]
    peer = _rule("order-number", "orders.order_id", "runs.order_ref", source_system="peer-erp")
    peer_by_name = {**peer, "source_system": " Peer ERP "}
    mine = {**peer, "source_system": "site-erp"}
    mine_by_name = {**peer, "source_system": "Site ERP"}
    unstated = {k: v for k, v in peer.items() if k != "source_system"}
    unknown = {**peer, "source_system": "the new system"}
    result = {"rules": [peer, mine, peer_by_name, mine_by_name, unstated, unknown],
              "rejected_rules": [{**peer, "rejected_reason": "only 1 shared value(s)"}]}
    out = resolve_gates.keep_own_candidates(result, own, own_names={"site-erp", "Site ERP"},
                                            peer_names={"peer-erp", "Peer ERP", "line-mes"})
    assert out["rules"] == [mine, mine_by_name, unstated, unknown]
    assert out["rejected_rules"] == []


def test_a_stated_own_source_system_never_makes_a_peer_field_the_systems():
    rule = _rule("operator", "Spindle.Operator", "stops.resolved_by", source_system=HUB)
    out = resolve_gates.keep_own_candidates({"rules": [rule]}, _own_hub_fields(),
                                            own_names={HUB}, peer_names={TELEMETRY})
    assert out["rules"] == []


def test_a_peer_source_system_is_vetoed_even_with_no_field_known():
    peer = _rule("ref", "orders.order_id", "x.y", source_system="peer-erp")
    out = resolve_gates.keep_own_candidates({"rules": [peer]}, [], own_names={"site-erp"},
                                            peer_names={"peer-erp"})
    assert out["rules"] == []


def test_the_system_names_are_the_catalogs_ids_and_display_names():
    own, peers = onboard._resolve_system_names(HUB, _Catalog(_entries()))
    assert own == {"cell-hub", "cell hub"}
    assert peers == {"site-cmms", "site cmms", "line-telemetry", "line telemetry",
                     "line-mes", "line mes"}
    assert onboard._resolve_system_names(HUB, object()) == ({"cell-hub"}, set())


# ── the wizard's resolve job ─────────────────────────────────────────────────

class _Catalog:
    def __init__(self, entries):
        self._entries = {e["id"]: e for e in entries}

    def get(self, sid):
        return self._entries.get(sid)

    def get_all(self):
        return list(self._entries.values())


def _entries():
    return [{"id": HUB, "name": "Cell Hub", "plant": "Plant A", "system_type": "Gateway"},
            {"id": CMMS, "name": "Site CMMS", "plant": "Plant A", "system_type": "CMMS"},
            {"id": TELEMETRY, "name": "Line Telemetry", "plant": "Plant A",
             "system_type": "Historian"},
            {"id": MES, "name": "Line MES", "plant": "Plant A", "system_type": "MES"}]


def _run_job(monkeypatch, system_id, mappings, found):
    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    onboard._run_resolve_job("job-own", OnboardResolveRequest(system_id=system_id,
                                                              mappings=mappings))
    job = onboard._resolve_jobs.pop("job-own")
    assert job["status"] == "done", job
    return job["result"]


def test_the_hub_checkpoint_lists_no_candidate_between_two_other_systems(monkeypatch):
    result = _run_job(monkeypatch, HUB, HUB_MAPPINGS,
                      {"rules": FOREIGN + [HUB_OWN], "value_mappings": [],
                       "evidence": [HUB_OWN_EV], "recommendations": []})
    assert [r["source_field"] for r in result["rules"]] == [HUB_OWN["source_field"]]
    assert result.get("rejected_rules") == []


def test_a_relational_systems_checkpoint_keeps_its_own_withheld_candidates(monkeypatch):
    mappings = [_mapping("work_log", "author", "operator"),
                _mapping("work_log", "crew", "operator")]
    own = _rule("operator", "work_log.author", "stops.resolved_by")
    weak = _rule("operator", "work_log.crew", "stops.resolved_by")
    foreign = _rule("operator", "Spindle.Operator", "stops.resolved_by")
    result = _run_job(monkeypatch, CMMS, mappings, {
        "rules": [own, foreign, weak], "value_mappings": [],
        "evidence": [_evidence("work_log.author", "stops.resolved_by", LOGINS),
                     _evidence("work_log.crew", "stops.resolved_by", ["crew_a"])]})
    assert [r["source_field"] for r in result["rules"]] == ["work_log.author"]
    assert [(r["source_field"], r["rejected_reason"]) for r in result["rejected_rules"]] == [
        ("work_log.crew", "only 1 shared value(s) (need >= 3)")]


def test_the_job_leaves_out_a_peer_stated_rule_whose_field_collides_with_its_own(monkeypatch):
    """End to end: the new system and the telemetry system both
    have ``work_log.author``. The model says the rule is the telemetry
    system's; the job neither keeps nor lists it, and keeps the one it says
    is the new system's (by id or display name)."""
    mappings = [_mapping("work_log", "author", "operator")]
    peer = _rule("operator", "work_log.author", "stops.resolved_by", source_system=TELEMETRY)
    peer_named = _rule("operator", "work_log.author", "stops.closed_by",
                       source_system="Line Telemetry")
    mine = _rule("operator", "work_log.author", "stops.resolved_by", source_system="Site CMMS")
    ev = [_evidence("work_log.author", "stops.resolved_by", LOGINS),
          _evidence("work_log.author", "stops.closed_by", LOGINS)]
    result = _run_job(monkeypatch, CMMS, mappings, {
        "rules": [peer, peer_named, mine], "value_mappings": [], "evidence": ev})
    model = [r for r in result["rules"] if resolve_gates.MEASURED_PAIR_KEY not in r]
    assert [r.get("source_system") for r in model] == ["Site CMMS"]
    # The measured closed_by pair is the new system's own (the concept pass
    # samples only its fields), so Resolve proposes it from the measurement
    # once the model's rule, stated as the telemetry system's, is left out.
    measured = [r for r in result["rules"] if resolve_gates.MEASURED_PAIR_KEY in r]
    assert [(r["source_system"], r["target_field"]) for r in measured] == [
        (CMMS, "stops.closed_by")]
    assert result["rejected_rules"] == []


# ── the chat's resolve_identities ────────────────────────────────────────────

def test_chat_resolve_leaves_out_a_rule_between_two_other_systems(world):
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer
    plant = world.catalog.get(SID)["plant"]
    for peer, name in (("peer-mes", "Peer MES"), ("peer-hist", "Peer Historian")):
        world.catalog.add({"id": peer, "name": name, "status": "onboarded", "plant": plant,
                           "system_type": "MES", "connection_type": "PostgreSQL",
                           "schema": f"s_{peer}"})
    rows = [{"sys_id": sid, "sys_name": sid, "sys_schema": "", "field_name": f, "concept": c,
             "table_name": t, "removal_incomplete": False}
            for sid, t, f, c in (("peer-mes", "stops", "stop_flag", "downtime"),
                                 ("peer-hist", "Gauge", "Down", "downtime"))]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    world.share_sampled_values()

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        return json.dumps({"rules": [
            {"concept": "downtime", "source_field": "Gauge.Down",
             "target_system": "peer-mes", "target_field": "stops.stop_flag"},
            {"concept": "downtime", "source_table": "Gauge", "source_field": "Down",
             "target_system": "peer-mes", "target_field": "stops.stop_flag"},
            {"concept": "downtime", "source_field": "get_tags.Down",
             "target_system": "peer-mes", "target_field": "stops.stop_flag"}],
            "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    # The model's own rule, then the measured pair it left out: the new
    # system's tag to the historian's Gauge.Down (the concept pass samples
    # only the new system's fields). Neither is between two other systems.
    own = (SID, "get_tags.Ent/Site/Line1/StationA/Down", "peer-hist", "Gauge.Down")
    assert [(r["source_field"], resolve_gates.MEASURED_PAIR_KEY in r) for r in resolved["rules"]] == [
        ("get_tags.Down", False), (own[1], True)], resolved
    assert tuple(resolved["rules"][1][k] for k in (
        "source_system", "source_field", "target_system", "target_field")) == own
    assert "rejected_rules" not in resolved
    stored = oa._store().get_session(SID)
    assert [r["source_field"] for r in stored["rules"]] == ["get_tags.Down", own[1]]


def test_chat_resolve_leaves_out_a_rule_the_model_says_is_a_peers(world):
    """On the chat path: the peer's field has the same name as
    one of the new system's tags; the stated source system decides."""
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer
    plant = world.catalog.get(SID)["plant"]
    world.catalog.add({"id": "peer-mes", "name": "Peer MES", "status": "onboarded",
                       "plant": plant, "system_type": "MES", "connection_type": "PostgreSQL",
                       "schema": "s_peer"})
    rows = [{"sys_id": "peer-mes", "sys_name": "Peer MES", "sys_schema": "", "field_name": "Down",
             "concept": "downtime", "table_name": "get_tags", "removal_incomplete": False}]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    world.share_sampled_values()
    prompts = []

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        prompts.append(prompt)
        rule = {"concept": "downtime", "source_field": "get_tags.Down",
                "target_system": "peer-mes", "target_field": "get_tags.Down"}
        return json.dumps({"rules": [{**rule, "source_system": "Peer MES"},
                                     {**rule, "source_system": SID}], "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    assert [r["source_system"] for r in resolved["rules"]] == [SID], resolved
    assert f'"source_system": "{SID}"' in prompts[0]


# ── the prompts ask for the source system ────────────────────────────────────

def test_the_chat_prompt_asks_for_the_new_systems_id_as_source_system():
    prompt = oa._build_resolve_prompt("plant-x-hub", "m", "e", "s", "t")
    assert '"source_system": "plant-x-hub"' in prompt
    assert "source_system must be 'plant-x-hub'" in prompt


def test_the_concept_pass_prompt_asks_for_the_new_systems_id_as_source_system(monkeypatch):
    prompts = []
    peers = [{"sys_id": MES, "sys_name": "Line MES", "sys_schema": "mes", "field_name": "resolved_by",
              "concept": "operator", "table_name": "stops", "role": "identifier"}]

    def _query(query, params=None):
        if "HAS_FIELD" in query and "IDENTIFIED_BY" not in query:
            return copy.deepcopy(peers)
        return []

    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "neptune_query", _query)
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda prompt: prompts.append(prompt) or '{"rules": []}')
    monkeypatch.setattr("agents.resolve_evidence.sample_field_values", lambda **_k: [])
    monkeypatch.setattr("agents.connection_resolver.resolve_connection", lambda sid: {})
    onboard._do_resolve(OnboardResolveRequest(
        system_id=CMMS, mappings=[_mapping("work_log", "author", "operator")]))
    (prompt,) = prompts
    assert f'"source_system":"{CMMS}"' in prompt
    assert f"source_system must be '{CMMS}'" in prompt
