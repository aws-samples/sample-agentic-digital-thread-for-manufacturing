# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding withholds identity rules between systems that serve different plants.

A plant's maintenance log author shares its technician logins with the
downtime ``resolved_by`` of EVERY plant's MES, because each plant writes the
same login names. The resolve step proposed a rule to each plant's MES, each
backed by real overlap; only the same plant's MES names the same people.
ADR-0051 P8 refused such links only between two systems of the same class, and
not in the resolve step's model rules. ``resolve_site_gate`` withholds them
for any class (shown at the checkpoint with the reason), unless a
steward-confirmed All Sites authority for the concept vouches; an unsited or
All Sites system links to every plant. Fixture names are generic.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.resolve_gates as resolve_gates  # noqa: E402
import agents.resolve_site_gate as gate  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, world)

NEW = "site-cmms"            # serves Plant A
SAME = "mes-a"               # Plant A
OTHER_B = "mes-b"            # Plant B
OTHER_C = "mes-c"            # Plant C
CORP = "corp-erp"            # All Sites
NOPLANT = "corp-plm"         # no plant at all
LOGINS = ["tech_1", "tech_2", "tech_3", "lead"]


class _Catalog:
    """A catalog with ids, display names and plants; ``get_all`` for name matching."""

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
        {"id": NOPLANT, "name": "Corporate PLM", "plant": "", "system_type": "PLM"},
    ]


def _rule(target, concept="operator", **extra):
    return {"concept": concept, "source_field": f"{NEW}.work_log.author", "source_pattern": "*",
            "target_system": target, "target_field": "stops.resolved_by", "target_pattern": "*",
            "confidence": 0.8, "reasoning": f"4 shared values e.g. {LOGINS}", **extra}


def _no_vouch(_concept):
    return ("none", "")


# ---------------------------------------------------------------- the refusal

@pytest.mark.parametrize("target", [SAME, CORP, NOPLANT, "corporate erp"])
def test_a_rule_to_the_same_plant_or_to_no_plant_passes(target):
    assert gate.cross_site_refusal(NEW, _rule(target), _Catalog(_entries()), _no_vouch) == ""


@pytest.mark.parametrize("target", [OTHER_B, "Plant B MES", "plant b mes"])
def test_a_rule_to_another_plant_is_refused_by_id_or_display_name(target):
    reason = gate.cross_site_refusal(NEW, _rule(target), _Catalog(_entries()), _no_vouch)
    assert reason.startswith("different plants (Plant A, Plant B)"), reason


def test_a_short_target_name_resolves_like_the_writer_and_is_refused():
    """A short target name passed the gate unplaced, and
    the writer then resolved it to the other plant's MES by containment."""
    entries = [e for e in _entries() if e["id"] not in (SAME, OTHER_C)]
    reason = gate.cross_site_refusal(NEW, _rule("mes-b-db"), _Catalog(entries), _no_vouch)
    assert reason.startswith("different plants (Plant A, Plant B)"), reason


@pytest.mark.parametrize("target,word", [("mes", "more than one system"),
                                         ("unknown-system", "isn't a known system"),
                                         ("", "isn't a known system")])
def test_a_target_the_gate_cannot_place_is_withheld_for_a_sited_system(target, word):
    reason = gate.cross_site_refusal(NEW, _rule(target), _Catalog(_entries()), _no_vouch)
    assert word in reason and "plant is unknown" in reason, reason


def test_an_unplaced_target_is_left_to_the_writer_for_an_unsited_system():
    assert gate.cross_site_refusal(CORP, _rule("mes"), _Catalog(_entries()), _no_vouch) == ""


def test_an_ambiguous_display_name_is_withheld():
    entries = _entries() + [{"id": "mes-b2", "name": "Plant B MES", "plant": "Plant B"}]
    reason = gate.cross_site_refusal(NEW, _rule("Plant B MES"), _Catalog(entries), _no_vouch)
    assert "more than one system" in reason, reason


@pytest.mark.parametrize("name,ids,want", [
    ("mes-b", {"mes-a", "mes-b"}, ("mes-b", "")),
    ("b", {"mes-a", "mes-b"}, ("mes-b", "")),
    ("mes-b-db", {"mes-a", "mes-b"}, ("mes-b", "")),
    ("mes", {"mes-a", "mes-b"}, ("", "ambiguous")),
    ("plm", {"mes-a", "mes-b"}, ("", "unknown")),
    ("", {"mes-a"}, ("", "unknown")),
])
def test_match_system_id(name, ids, want):
    assert gate.match_system_id(name, ids) == want


def test_the_writer_refuses_an_ambiguous_or_empty_target():
    """The writer took the first containment match of a set,
    so an ambiguous short name (or an empty one) became an arbitrary system."""
    ctx = SimpleNamespace(system_id=NEW)
    for target, word in (("mes", "names more than one"), ("", "is not an onboarded system")):
        rule = _rule(target)
        reason = onboard._dp_rule_target_check(ctx, rule, {SAME, OTHER_B, OTHER_C, CORP})
        assert reason and word in reason, (target, reason)
        assert rule["target_system"] == target


def test_plants_compare_without_case_or_padding():
    entries = _entries() + [{"id": "mes-a2", "name": "Second", "plant": " plant a "}]
    assert gate.cross_site_refusal(NEW, _rule("mes-a2"), _Catalog(entries), _no_vouch) == ""


def test_an_unsited_onboarding_system_links_to_every_plant():
    rule = dict(_rule(OTHER_B), source_field="orders.author")
    assert gate.cross_site_refusal(CORP, rule, _Catalog(_entries()), _no_vouch) == ""


def test_a_model_that_names_another_plant_as_the_source_is_refused():
    """The resolve prompt does not say which side is the onboarding system."""
    rule = dict(_rule(NEW), source_system=OTHER_C)
    reason = gate.cross_site_refusal(NEW, rule, _Catalog(_entries()), _no_vouch)
    assert reason.startswith("different plants (Plant A, Plant C)"), reason


def test_a_steward_confirmed_all_sites_authority_vouches():
    seen: list = []

    def vouch(concept):
        seen.append(concept)
        return ("steward", "ar-corp-erp-operator")

    assert gate.cross_site_refusal(NEW, _rule(OTHER_B), _Catalog(_entries()), vouch) == ""
    assert seen == ["operator"]


def test_an_unconfirmed_authority_does_not_vouch_and_is_named():
    reason = gate.cross_site_refusal(NEW, _rule(OTHER_B), _Catalog(_entries()),
                                     lambda _c: ("unverified", "ar-corp-erp-operator"))
    assert "different plants (Plant A, Plant B)" in reason
    assert "ar-corp-erp-operator" in reason and "isn't confirmed" in reason


def test_the_vouch_is_not_read_for_a_same_plant_rule():
    def vouch(_concept):
        raise AssertionError("vouch read for a rule that needs none")

    assert gate.cross_site_refusal(NEW, _rule(SAME), _Catalog(_entries()), vouch) == ""


# ---------------------------------------------------------------- the gate

def test_gate_moves_cross_plant_rules_to_withheld_after_those_already_there():
    earlier = {"concept": "criticality", "rejected_reason": "only 2 shared value(s) (need >= 3)"}
    result = {"rules": [_rule(SAME), _rule(OTHER_B), "not-a-rule", _rule(CORP), _rule(OTHER_C)],
              "rejected_rules": [earlier], "evidence": [{"x": 1}], "value_mappings": [{"y": 2}]}
    out = gate.gate_cross_site_rules(NEW, copy.deepcopy(result), _Catalog(_entries()), _no_vouch)
    assert [r if isinstance(r, str) else r["target_system"] for r in out["rules"]] == \
        [SAME, "not-a-rule", CORP]
    assert out["rejected_rules"][0] == earlier
    assert [r["target_system"] for r in out["rejected_rules"][1:]] == [OTHER_B, OTHER_C]
    assert all(r["rejected_reason"].startswith("different plants") for r in out["rejected_rules"][1:])
    assert out["evidence"] == result["evidence"] and out["value_mappings"] == result["value_mappings"]


def test_gate_returns_the_result_itself_when_nothing_is_withheld():
    result = {"rules": [_rule(SAME)]}
    assert gate.gate_cross_site_rules(NEW, result, _Catalog(_entries()), _no_vouch) is result
    empty = {"rules": []}
    assert gate.gate_cross_site_rules(NEW, empty, _Catalog(_entries())) is empty


@pytest.mark.parametrize("target", ["Plant A MES", "plant a mes ", "mes-a-db"])
def test_a_kept_rule_names_its_target_by_id_and_the_writer_accepts_it(target):
    """A same-plant rule naming its target by display name passed the gate,
    then the writer, which matches ids only, refused it as not onboarded."""
    result = {"rules": [_rule(target)], "evidence": [{"x": 1}]}
    out = gate.gate_cross_site_rules(NEW, copy.deepcopy(result), _Catalog(_entries()), _no_vouch)
    (kept,) = out["rules"]
    assert kept["target_system"] == SAME
    assert "rejected_rules" not in out and out["evidence"] == result["evidence"]
    reason = onboard._dp_rule_target_check(SimpleNamespace(system_id=NEW), dict(kept),
                                           {NEW, SAME, OTHER_B, CORP})
    assert reason is None


def test_an_ambiguous_display_name_is_neither_kept_nor_written():
    entries = _entries() + [{"id": "mes-a2", "name": "Plant A MES", "plant": "Plant A"}]
    out = gate.gate_cross_site_rules(NEW, {"rules": [_rule("Plant A MES")]}, _Catalog(entries),
                                     _no_vouch)
    assert out["rules"] == []
    assert "more than one system" in out["rejected_rules"][0]["rejected_reason"]
    reason = onboard._dp_rule_target_check(SimpleNamespace(system_id=NEW), _rule("Plant A MES"),
                                           {NEW, SAME, "mes-a2"})
    assert reason and "is not an onboarded system" in reason


def test_make_vouch_reads_each_concept_once_and_a_failed_read_vouches_nothing(monkeypatch):
    calls: list = []

    def verdict(concept, _entries, *, known_owner_only=False):
        calls.append((concept, known_owner_only))
        if concept == "broken":
            raise RuntimeError("graph down")
        return ("steward", "ar-1")

    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    monkeypatch.setattr(self_learning, "_authority_vouch_verdict", verdict)
    vouch = gate.make_vouch()
    assert vouch("operator") == ("steward", "ar-1")
    assert vouch(" Operator ") == ("steward", "ar-1")
    assert vouch("broken") == ("none", "")
    assert vouch("broken") == ("none", "")
    assert vouch("") == ("none", "")
    assert calls == [("operator", True), ("broken", True)]


def _authority(owner):
    return [{"id": f"ar-{owner}", "concept": "operator", "sid": owner, "method": "user_confirmed",
             "approved_by": "", "evidence": "", "removal_incomplete": False}]


def test_make_vouch_uses_the_p8_authority_read(monkeypatch):
    """The real P8 vouch: a steward-confirmed authority on an All Sites system."""
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: _authority(CORP))
    assert gate.make_vouch()("operator") == ("steward", f"ar-{CORP}")
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: _authority(SAME))
    assert gate.make_vouch()("operator") == ("none", "")   # a plant's own authority never vouches


def test_an_authority_whose_owner_is_unknown_does_not_vouch(monkeypatch):
    """The P8 read counts an authority whose owner the catalog
    does not know as All Sites, so a confirmed authority of a plant system the
    inventory missed vouched for a cross-plant rule."""
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: _authority("gone-mes"))
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    assert gate.make_vouch()("operator") == ("none", "")
    # The proposal gate's own (fail-open) reading is unchanged.
    assert self_learning._authority_vouch_verdict("operator", {})[0] == "steward"


def test_an_empty_system_inventory_vouches_nothing(monkeypatch):
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: _authority(CORP))
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    assert gate.make_vouch()("operator") == ("none", "")


# ---------------------------------------------------------------- the wizard's resolve job

def test_resolve_job_withholds_the_rules_to_other_plants(monkeypatch, caplog):
    """The concept pass's model proposes the author rule to every plant's MES
    and to the corporate ERP; only the same plant's and the corporate ones
    reach review, the others are listed as withheld with the plants."""
    found = {"rules": [_rule(SAME), _rule(OTHER_B), _rule(OTHER_C), _rule(CORP)],
             "value_mappings": [], "evidence": [
                 {"concept": "operator", "source_field": "work_log.author", "target_system": t,
                  "target_field": "stops.resolved_by", "overlap_count": 4, "unchanged_count": 4,
                  "ratio": 1.0, "examples": LOGINS} for t in (SAME, OTHER_B, OTHER_C, CORP)]}
    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    body = OnboardResolveRequest(system_id=NEW, mappings=[])
    with caplog.at_level(logging.INFO):
        onboard._run_resolve_job("job-site", body)
    job = onboard._resolve_jobs.pop("job-site")
    assert job["status"] == "done", job
    result = job["result"]
    assert [r["target_system"] for r in result["rules"]] == [SAME, CORP]
    withheld = [r for r in result.get("rejected_rules") or [] if r.get("target_system")]
    assert [r["target_system"] for r in withheld] == [OTHER_B, OTHER_C]
    assert withheld[0]["rejected_reason"].startswith("different plants (Plant A, Plant B)")
    assert len(result["evidence"]) == 4   # the measured overlap is still shown
    assert any("identity rule withheld" in r.getMessage() and OTHER_B in r.getMessage()
               for r in caplog.records)


def test_resolve_job_keeps_the_overlap_gate_reasons(monkeypatch):
    """A rule the evidence gate already withheld keeps its own reason; the site
    gate only sees the rules the evidence gate kept."""
    weak = dict(_rule(OTHER_B), concept="work-order")
    found = {"rules": [weak, _rule(OTHER_C)], "value_mappings": [], "evidence": [
        {"concept": "work-order", "source_field": "work_log.author", "target_system": OTHER_B,
         "target_field": "stops.resolved_by", "overlap_count": 2, "ratio": 1.0, "examples": ["1", "2"]},
        {"concept": "operator", "source_field": "work_log.author", "target_system": OTHER_C,
         "target_field": "stops.resolved_by", "overlap_count": 4, "unchanged_count": 4,
         "ratio": 1.0, "examples": LOGINS}]}
    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    onboard._run_resolve_job("job-site-2", OnboardResolveRequest(system_id=NEW, mappings=[]))
    result = onboard._resolve_jobs.pop("job-site-2")["result"]
    assert result["rules"] == []
    reasons = [r["rejected_reason"] for r in result["rejected_rules"]]
    assert reasons[0].startswith("only 2 shared value(s)")
    assert reasons[1].startswith("different plants (Plant A, Plant C)")
    assert resolve_gates.MIN_SHARED_VALUES == 3


# ---------------------------------------------------------------- one row per candidate

#: The model's rule with its tables dropped, as the overlap gate withheld it.
BARE_WEAK = {"concept": "operator", "source_field": "author", "source_pattern": "*",
             "target_system": OTHER_B, "target_field": "resolved_by", "target_pattern": "*",
             "confidence": 0.8, "rejected_reason": "only 2 shared value(s) (need >= 3)"}


def test_a_pair_already_withheld_is_not_listed_again_by_the_site_gate():
    """The overlap gate withheld the model's rule; the affinity pass re-added
    the same pair, which the site gate withholds: one row, the first reason."""
    out = gate.gate_cross_site_rules(NEW, {"rules": [_rule(OTHER_B)],
                                           "rejected_rules": [dict(BARE_WEAK)]},
                                     _Catalog(_entries()), _no_vouch)
    assert out["rules"] == []
    assert out["rejected_rules"] == [BARE_WEAK]


def test_a_bare_prior_row_and_two_other_table_pairs_stay_two_site_rows():
    other_table = dict(_rule(OTHER_B), source_field=f"{NEW}.archive_log.author")
    out = gate.gate_cross_site_rules(NEW, {"rules": [_rule(OTHER_B), other_table],
                                           "rejected_rules": [dict(BARE_WEAK)]},
                                     _Catalog(_entries()), _no_vouch)
    assert [(r["source_field"], r["rejected_reason"][:16]) for r in out["rejected_rules"]] == [
        (f"{NEW}.work_log.author", "different plants"),
        (f"{NEW}.archive_log.author", "different plants")]


def test_resolve_job_lists_a_pair_the_overlap_gate_withheld_and_affinity_readded_once(
        monkeypatch):
    from agents.identity_affinity import EXACT_METHOD, merge_affinity_rules
    model = {k: v for k, v in BARE_WEAK.items() if k != "rejected_reason"}
    found = {"rules": [model], "value_mappings": [], "evidence": [
        {"concept": "operator", "source_field": "work_log.author", "target_system": OTHER_B,
         "target_field": "stops.resolved_by", "overlap_count": 2, "ratio": 1.0,
         "examples": LOGINS[:2]}]}
    exact = dict(_rule(OTHER_B), method=EXACT_METHOD, transform="identity", shared_count=4,
                 source_pattern="", target_pattern="")

    def _affinity(body, result):
        result["rules"] = merge_affinity_rules(list(result.get("rules") or []), [dict(exact)])
        return result

    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", _affinity)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    onboard._run_resolve_job("job-site-once", OnboardResolveRequest(system_id=NEW, mappings=[]))
    result = onboard._resolve_jobs.pop("job-site-once")["result"]
    assert result["rules"] == []
    assert [(r["source_field"], r["rejected_reason"]) for r in result["rejected_rules"]] == [
        ("author", "only 2 shared value(s) (need >= 3)")]


# ---------------------------------------------------------------- the chat's resolve

def test_chat_resolve_stores_no_rule_to_another_plant(world):
    """The chat's resolve_identities stores the same-plant rule and answers the
    other plant's as withheld, with the reason."""
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer
    plant = world.catalog.get(SID)["plant"]
    world.catalog.add({"id": "peer-same", "name": "Same Plant MES", "status": "onboarded",
                       "plant": plant, "system_type": "MES", "connection_type": "PostgreSQL",
                       "schema": "s_same"})
    world.catalog.add({"id": "peer-other", "name": "Other Plant MES", "status": "onboarded",
                       "plant": "Plant Z", "system_type": "MES", "connection_type": "PostgreSQL",
                       "schema": "s_other"})
    rows = [{"sys_id": sid, "sys_name": name, "sys_schema": "", "field_name": "stop_flag",
             "concept": "downtime", "table_name": "stops", "removal_incomplete": False}
            for sid, name in (("peer-same", "Same Plant MES"), ("peer-other", "Other Plant MES"))]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    world.share_sampled_values()

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        return json.dumps({"rules": [
            {"concept": "downtime", "source_field": "get_tags.Down", "target_system": t,
             "target_field": "stops.stop_flag"} for t in ("peer-same", "peer-other")],
            "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    assert [r["target_system"] for r in resolved["rules"]] == ["peer-same"], resolved
    assert [r["target_system"] for r in resolved["rejected_rules"]] == ["peer-other"]
    assert resolved["rejected_rules"][0]["rejected_reason"].startswith(
        f"different plants ({plant}, Plant Z)")
    stored = oa._store().get_session(SID)
    assert [r["target_system"] for r in stored["rules"]] == ["peer-same"]
