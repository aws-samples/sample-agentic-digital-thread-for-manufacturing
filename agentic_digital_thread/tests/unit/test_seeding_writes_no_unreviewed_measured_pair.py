# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A measured pair Resolve proposes is for a person to review, and never
displaces a measured-method rule.

Resolve proposes a measured pair the model left out
(``resolve_gates.measured_pair_rules``) so the Identity Rules checkpoint can
show it. The deployment's seeding drives the same Resolve job, then sends
every rule it lists straight to provision with no checkpoint
(``scripts/onboarding_smoketest._provision_request_body``: no
``rules_from_checkpoint``, no ``resolve_job_id``). On one deploy that wrote
159 rules nobody reviewed. Now:

* provision writes a measured-pair rule only when it was kept at the
  checkpoint, bound to the Resolve result the request names
  (``onboard._checkpoint_rule_ids``); any other provision leaves it out and
  logs it, so seeding writes the rules it wrote before measured pairs existed;
* a measured pair joins the candidates after the identifier-core affinity
  pass, so a measured-method rule (``value_exact_match`` or
  ``value_core_match``) on the same field pair keeps its pair, with its own
  pattern and evidence, and the measured pair is not proposed beside it.

Fixture names and values are generic.
"""
from __future__ import annotations

import copy
import importlib.util
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.resolve_gates as resolve_gates  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.identity_affinity import AFFINITY_METHOD, EXACT_METHOD  # noqa: E402
from agents.models import OnboardResolveRequest, ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import measured_as_sent  # noqa: E402

_HARNESS_PATH = _ADT_ROOT.parent / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest_seed", _HARNESS_PATH)
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

NEW, PEER = "plant-mes", "corp-ledger"
KEYS = ["K-1001", "K-1002", "K-1003", "K-1004"]
CODES = ["P-201", "P-202", "P-203"]


class _Catalog:
    def __init__(self):
        self._entries = {
            NEW: {"id": NEW, "name": "Plant MES", "plant": "Plant A", "system_type": "MES",
                  "connection_type": "PostgreSQL", "schema": "plant_mes"},
            PEER: {"id": PEER, "name": "Corporate Ledger", "plant": "All Sites",
                   "system_type": "ERP", "connection_type": "PostgreSQL", "schema": "ledger"},
        }

    def get(self, sid):
        return self._entries.get(sid)

    def get_all(self):
        return list(self._entries.values())

    def get_onboarded_ids(self):
        return {PEER}


def _ev(concept, source, target, examples, ratio=1.0):
    n = len(examples)
    return {"concept": concept, "source_field": source, "target_system": PEER,
            "target_field": target, "overlap_count": n, "unchanged_count": n,
            "ratio": ratio, "examples": list(examples)}


def _affinity(method, source, target, patterns=("", "")):
    s_table, t_table = source.split(".")[0], target.split(".")[0]
    rule = {"concept": "batch", "source_field": source, "source_table": s_table,
            "source_pattern": patterns[0], "target_system": PEER, "target_field": target,
            "target_table": t_table, "target_pattern": patterns[1], "confidence": 0.85,
            "method": method, "reasoning": "measured by the affinity pass"}
    if method == EXACT_METHOD:
        rule.update(transform="identity", shared_count=len(KEYS))
    return rule


#: The concept pass measured two pairs that share whole values; the model
#: proposed no rule. The affinity pass found a measured-method rule for the
#: first pair only.
EVIDENCE = [_ev("batch", "runs.run_ref", "shipments.batch_ref", KEYS),
            _ev("part", "orders.part_ref", "items.part_ref", CODES)]


def _resolve(monkeypatch, affinity_rules, job="job-seed"):
    """The wizard's Resolve job (the one seeding drives), with the model and
    the affinity pass's matcher answering as given; the affinity pass itself
    (its cross-site guard, the merge and its log lines) is the real one."""
    import agents.identity_affinity as identity_affinity
    cat = _Catalog()
    monkeypatch.setattr(onboard, "catalog", cat)
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: {
        "rules": [], "value_mappings": [], "evidence": copy.deepcopy(EVIDENCE)})
    monkeypatch.setattr(identity_affinity, "find_affinity_rules",
                        lambda *_a, **_k: copy.deepcopy(affinity_rules))
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: cat.get_all())
    onboard._run_resolve_job(job, OnboardResolveRequest(system_id=NEW, mappings=[]))
    out = onboard._resolve_jobs.pop(job)
    assert out["status"] == "done", out
    return out["result"]


def _pairs(rules):
    return [(r["source_field"], r["target_field"], r.get("method") or "",
             resolve_gates.MEASURED_PAIR_KEY in r) for r in rules]


# ---------------------------------------------------------------- 1. measured-method rules keep their pair

def test_an_exact_rule_keeps_its_pair_and_the_measured_pair_beside_it_is_not_proposed(
        monkeypatch, caplog):
    exact = _affinity(EXACT_METHOD, "runs.run_ref", "shipments.batch_ref")
    with caplog.at_level(logging.INFO):
        result = _resolve(monkeypatch, [exact])
    assert _pairs(result["rules"]) == [
        ("runs.run_ref", "shipments.batch_ref", EXACT_METHOD, False),
        ("orders.part_ref", "items.part_ref", "", True)]
    # The measured pair never reaches the merge as a rule the exact rule replaces.
    assert not any("replaced" in r.getMessage() for r in caplog.records)


def test_a_core_match_rule_keeps_its_pattern_over_a_measured_pair_on_its_pair(monkeypatch):
    """The affinity pass found a transform; the concept pass also measured
    whole values the two columns share. The rule with the pattern and its own
    evidence is the one listed."""
    core = _affinity(AFFINITY_METHOD, "runs.run_ref", "shipments.batch_ref",
                     ("K-{id}", "K-{id}-A"))
    result = _resolve(monkeypatch, [core], job="job-core")
    assert _pairs(result["rules"]) == [
        ("runs.run_ref", "shipments.batch_ref", AFFINITY_METHOD, False),
        ("orders.part_ref", "items.part_ref", "", True)]
    assert (result["rules"][0]["source_pattern"], result["rules"][0]["target_pattern"]) == (
        "K-{id}", "K-{id}-A")


# ---------------------------------------------------------------- 2. provision writes only reviewed measured pairs

class _ProvisionCatalog:
    def get(self, _sid):
        return None

    def get_onboarded_ids(self):
        return {PEER}


def _provision(monkeypatch, body: ProvisionRequest):
    """Provision's identity-rule writer (c, then c2) on ``body``, as the
    provision job builds its state; the MERGEd IdentityRule parameters."""
    measured_as_sent(monkeypatch)
    recorded: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe",
                        lambda c, p=None, **_k: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "catalog", _ProvisionCatalog())
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=NEW, system_name="Plant MES",
        plant="", schema="", rules=body.rules, mappings=[], connection_type="MCP Server",
        cm_items=[], committable=set(), rules_from_checkpoint=body.rules_from_checkpoint,
        checkpoint_rule_ids=onboard._checkpoint_rule_ids(body))
    onboard._dp_identity_rules(ctx)
    return [p for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]


def _resolved_rules(monkeypatch):
    exact = _affinity(EXACT_METHOD, "runs.run_ref", "shipments.batch_ref")
    result = _resolve(monkeypatch, [exact], job="job-prov")
    return result


def test_a_seeding_provision_writes_no_measured_pair_and_says_so(monkeypatch, caplog):
    """Seeding sends the Resolve job's rules as its harness builds the body:
    no checkpoint. The measured pair is left out; the measured-method rule
    is written as before."""
    result = _resolved_rules(monkeypatch)
    body = ProvisionRequest(**harness._provision_request_body(
        NEW, {"name": "Plant MES", "plant": "Plant A", "schema": "plant_mes",
              "connection_type": "MCP Server"}, [], result["rules"], ""))
    assert not body.rules_from_checkpoint and not body.resolve_job_id
    with caplog.at_level(logging.INFO):
        written = _provision(monkeypatch, body)
    assert [(p["src_field"], p["tgt_field"]) for p in written] == [("run_ref", "batch_ref")]
    notes = [r.getMessage() for r in caplog.records if "not approved at the Identity Rules" in
             r.getMessage()]
    assert len(notes) == 1 and "orders.part_ref" in notes[0], notes


def test_a_checkpoint_provision_writes_the_measured_pair_kept_there(monkeypatch):
    """The wizard sends the rules its checkpoint kept, naming the Resolve
    result: the measured pair is written, with its measured-pair origin."""
    result = _resolved_rules(monkeypatch)
    body = ProvisionRequest(system_id=NEW, system_name="Plant MES", mappings=[],
                            rules=copy.deepcopy(result["rules"]), connection_type="MCP Server",
                            rules_from_checkpoint=True, resolve_job_id=result["resolve_job_id"])
    written = _provision(monkeypatch, body)
    by_field = {p["src_field"]: p for p in written}
    assert set(by_field) == {"run_ref", "part_ref"}
    assert by_field["part_ref"]["octx"].startswith(onboard._MEASURED_PAIR_RULE_ORIGIN)


def test_a_measured_pair_sent_as_kept_but_not_in_the_named_result_is_left_out(monkeypatch):
    """A request that says its rules come from the checkpoint but names a
    Resolve result that does not list the rule binds nothing: a person never
    kept it there."""
    result = _resolved_rules(monkeypatch)
    body = ProvisionRequest(system_id=NEW, system_name="Plant MES", mappings=[],
                            rules=copy.deepcopy(result["rules"]), connection_type="MCP Server",
                            rules_from_checkpoint=True, resolve_job_id="no-such-result")
    written = _provision(monkeypatch, body)
    assert [p["src_field"] for p in written] == ["run_ref"]
