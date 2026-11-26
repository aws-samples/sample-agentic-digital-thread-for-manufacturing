# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Identity Rules checkpoint never lists a rule provision will refuse.

Provision stores a rule only when its pattern pair passes
``resolve.identity_pattern_problem`` (no pattern, the same text on both sides,
or literal text around a ``{placeholder}`` both sides name). Resolve never
asked that question, and both resolve prompts steered the model away from the
empty pair: the model wrote regular expressions, the checkpoint listed the
rule as normal, and provision refused it, so a confirmed rule was never
written.

Now both resolve paths (the wizard's Resolve job and the chat's
``resolve_identities``) run provision's own check after every other gate.
A rule it refuses is listed as written only when Resolve's own measurement of
that field pair shows the values the two fields share are the same text in
both: it is then the exact-agreement rule that measurement supports (no
pattern), marked as set from the measurement. Any other such rule is withheld
with provision's reason. Both prompts now say what can be stored. Fixture
names are generic.
"""
from __future__ import annotations

import copy
import json
import os
import re
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
from agents.identifier_translation import apply  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402
from agents.resolve import identity_pattern_problem  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, world)

NEW, PEER = "line-upkeep", "cell-ops"
LOGINS = ["tech_a1", "tech_a2", "tech_b3", "lead_c4"]
#: A regular expression pair, the shape the model wrote on the live walk.
REGEX = ("^[a-z0-9_]+$", "^[a-z_0-9]+$")


class _Catalog:
    def __init__(self, entries):
        self._entries = {e["id"]: e for e in entries}

    def get(self, sid):
        return self._entries.get(sid)

    def get_all(self):
        return list(self._entries.values())


def _entries():
    return [{"id": NEW, "name": "Line Upkeep", "plant": "Plant A", "system_type": "CMMS",
             "connection_type": "PostgreSQL", "schema": "upkeep"},
            {"id": PEER, "name": "Cell Ops", "plant": "Plant A", "system_type": "MES",
             "connection_type": "PostgreSQL", "schema": "cellops"}]


def _peer_row(table, field, concept):
    return {"sys_id": PEER, "sys_name": "Cell Ops", "sys_schema": "cellops", "field_name": field,
            "concept": concept, "table_name": table, "role": "correlate"}


def _mapping(table, field, concept):
    return {"table": table, "field": field, "concept": concept, "confidence": 0.8,
            "mappings": [{"concept": concept, "role": "correlate", "confidence": 0.8}]}


def _model_rule(source_field, target_field, concept, patterns):
    return {"concept": concept, "source_system": NEW, "source_field": source_field,
            "source_pattern": patterns[0], "target_system": PEER, "target_field": target_field,
            "target_pattern": patterns[1], "confidence": 0.8, "reasoning": "the values match"}


def wizard_resolve(monkeypatch, *, mappings, peer_rows, samples, model_rules,
                   value_mappings=(), probe=lambda **_kw: None, prompts=None, entity_rows=(),
                   real_sampler=False, affinity_rules=()):
    """The wizard's Resolve job with the real concept pass (``_do_resolve``)
    and gates; only the I/O seams are replaced. ``samples`` maps
    ``(system, table, field)`` to the values that field's sample returns;
    ``entity_rows`` are the graph's entity and identity-key rows.
    ``real_sampler``: ``samples`` is what each backend read returns, and the
    real ``sample_field_values`` (its bounds included) runs over it.
    ``affinity_rules``: what the deterministic affinity pass adds."""
    import agents.connection_resolver as connection_resolver
    import agents.resolve_evidence as resolve_evidence
    monkeypatch.setattr(onboard, "catalog", _Catalog(_entries()))

    def query(cypher, params=None):
        if "IDENTIFIED_BY" in cypher:
            return copy.deepcopy(list(entity_rows))
        if "r.role AS role" in cypher:
            return copy.deepcopy(peer_rows)
        return []

    def model(prompt, *_a, **_k):
        if prompts is not None:
            prompts.append(prompt)
        return json.dumps({"rules": copy.deepcopy(model_rules),
                           "value_mappings": copy.deepcopy(list(value_mappings))})

    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard, "bedrock_call", model)
    if real_sampler:
        resolve_evidence._reset_sample_memo()
        monkeypatch.setattr(resolve_evidence, "_sample_backend",
                            lambda ct, sid, schema, table, field, *a, **k: list(
                                samples.get((sid, table, field), [])))
    else:
        monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **kw: list(
            samples.get((kw["system_id"], kw["table"], kw["field"]), [])))
    monkeypatch.setattr(resolve_evidence, "probe_field_values", probe)
    monkeypatch.setattr(connection_resolver, "resolve_connection", lambda *_a, **_k: {})
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: {
        **result, "rules": list(result.get("rules") or []) + copy.deepcopy(list(affinity_rules))})
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _entries())
    onboard._run_resolve_job("job-store", OnboardResolveRequest(system_id=NEW, mappings=mappings))
    job = onboard._resolve_jobs.pop("job-store")
    assert job["status"] == "done", job
    return job["result"]


def _author_resolve(monkeypatch, patterns, target_values=LOGINS, **kw):
    return wizard_resolve(
        monkeypatch, mappings=[_mapping("crew_log", "entered_by", "personnel")],
        peer_rows=[_peer_row("stops", "cleared_by", "personnel")],
        samples={(NEW, "crew_log", "entered_by"): LOGINS + ["tech_e9"],
                 (PEER, "stops", "cleared_by"): list(target_values)},
        model_rules=[_model_rule("crew_log.entered_by", "stops.cleared_by", "personnel", patterns)],
        **kw)


def _assert_every_listed_rule_is_storable(result):
    for rule in result["rules"]:
        assert onboard._pattern_pair_rejection(rule) == "", rule


# ── the wizard's Resolve job ────────────────────────────────────────────────

def test_a_measured_rule_the_model_wrote_as_regular_expressions_is_listed_storable(monkeypatch):
    """The live walk: four logins shared unchanged, the model's patterns are
    regular expressions. The checkpoint lists the exact-agreement rule the
    measurement supports, says the pattern came from the measurement, and
    provision's own check accepts it."""
    result = _author_resolve(monkeypatch, REGEX)
    _assert_every_listed_rule_is_storable(result)
    assert [(r["source_field"], r["source_pattern"], r["target_pattern"])
            for r in result["rules"]] == [("crew_log.entered_by", "", "")]
    origin = result["rules"][0]["pattern_origin"]
    assert "measurement" in origin and REGEX[0] in origin and "4" in origin
    assert result.get("rejected_rules") in (None, [])


def test_values_shared_only_after_token_normalization_are_not_an_exact_agreement(monkeypatch):
    """The target pads its logins: the tokens match once padding is set
    aside, the raw values never do, so an exact-agreement rule would join
    nothing. The rule is withheld with provision's own reason."""
    padded = [f" {v} " for v in LOGINS]
    result = _author_resolve(monkeypatch, REGEX, target_values=padded)
    _assert_every_listed_rule_is_storable(result)
    assert result["rules"] == []
    (withheld,) = result["rejected_rules"]
    assert withheld["rejected_reason"] == (
        f"pattern '{REGEX[0]}' -> '{REGEX[1]}' is not a valid pattern: the source pattern names "
        "no {placeholder} and the two sides differ, so it transforms nothing")
    assert "pattern_origin" not in withheld


@pytest.mark.parametrize("patterns", [
    ("LN*", "*-L*"),                      # a glob
    ("{a}", "{b}"),                       # placeholders the two sides do not share
    ("{n}-{n}", "{n}"),                   # one placeholder named twice
    ("^\\d{3}$", "^[A-Z]{2}\\d+$"),       # regular expressions
], ids=["glob", "unshared-placeholder", "repeated-placeholder", "regex"])
def test_an_unmeasured_rule_provision_refuses_is_withheld_with_provisions_reason(monkeypatch,
                                                                                 patterns):
    """No measurement for the pair (the target sample is empty) and identifier
    names, so every other gate keeps it: provision's check withholds it, with
    provision's words."""
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): ["J-101", "J-102", "J-103"]},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)])
    _assert_every_listed_rule_is_storable(result)
    assert result["rules"] == []
    (withheld,) = result["rejected_rules"]
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)
    assert withheld["rejected_reason"] == onboard._pattern_pair_rejection(rule) != ""


@pytest.mark.parametrize("patterns", [("", ""), ("*", "*"), ("JB-{n}", "ORD-{n}")],
                         ids=["empty", "bare-star", "placeholder-transform"])
def test_a_storable_rule_is_listed_as_the_model_wrote_it(monkeypatch, patterns):
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): ["J-101", "J-102", "J-103"]},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)])
    (rule,) = result["rules"]
    assert (rule["source_pattern"], rule["target_pattern"]) == patterns
    assert "pattern_origin" not in rule


def test_a_model_cannot_write_the_measurement_marker_itself(monkeypatch):
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("", ""))
    rule["pattern_origin"] = "set from the measurement"
    result = wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): ["J-101", "J-102", "J-103"]},
        model_rules=[rule])
    assert "pattern_origin" not in result["rules"][0]


def test_the_measurement_counts_how_many_shared_values_are_unchanged(monkeypatch):
    """Each evidence row says how many of its shared values are the same
    whole text on both sides: what the exact-agreement decision reads."""
    same = _author_resolve(monkeypatch, ("", ""))
    padded = _author_resolve(monkeypatch, ("", ""), target_values=[f" {v} " for v in LOGINS])
    (row,) = same["evidence"]
    (padded_row,) = padded["evidence"]
    assert (row["overlap_count"], row["unchanged_count"]) == (4, 4)
    assert (padded_row["overlap_count"], padded_row["unchanged_count"]) == (4, 0)


# ── the chat's resolve_identities ───────────────────────────────────────────

def test_the_chat_resolve_withholds_a_rule_provision_refuses(world):
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer
    plant = world.catalog.get(SID)["plant"]
    world.catalog.add({"id": "peer-mes", "name": "Peer MES", "status": "onboarded",
                       "plant": plant, "system_type": "MES", "connection_type": "PostgreSQL",
                       "schema": "s_peer"})
    rows = [{"sys_id": "peer-mes", "sys_name": "Peer MES", "sys_schema": "", "field_name": "stop_flag",
             "concept": "downtime", "table_name": "stops", "removal_incomplete": False}]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    # The chat measures its rules too: the two fields share three values once
    # padding is set aside, never as the same text, so neither rule is rebuilt.
    import agents.resolve_evidence as resolve_evidence
    world.mp.setattr(resolve_evidence, "sample_field_values", lambda **kw: (
        ["D-101", "D-102", "D-103"] if kw["system_id"] == SID else [" D-101 ", " D-102 ", " D-103 "]))
    down = "get_tags.Ent/Site/Line1/StationA/Down"

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        return json.dumps({"rules": [
            {"concept": "downtime", "source_field": down, "source_pattern": "^[01]$",
             "target_system": "peer-mes", "target_field": "stops.stop_flag",
             "target_pattern": "^(true|false)$"},
            {"concept": "downtime", "source_field": down, "source_pattern": "",
             "target_system": "peer-mes", "target_field": "stops.stop_flag", "target_pattern": "",
             "pattern_origin": "set from the measurement"}],
            "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    # Neither is listed, and the two are one candidate, withheld once: the
    # empty-pattern rule's shared values are the same text only once padding
    # is set aside, and a rule that reads values unchanged joins whole values
    # (test_resolve_gate_reads_what_runtime_compares); the regular expressions
    # are not rebuilt on them.
    assert resolved["rules"] == []
    assert [r["rejected_reason"] for r in resolved["rejected_rules"]] == [
        "0 of the 3 shared values are the same whole value in both fields (need >= 3); the "
        "others are shared only once padding is set aside or a JSON value's members are read "
        "out, and a rule that reads values unchanged joins whole values"]
    assert "pattern_origin" not in resolved["rejected_rules"][0]
    assert oa._store().get_session(SID)["rules"] == []


# ── the prompts ─────────────────────────────────────────────────────────────

# A pattern token: no space, quote or bracket, holding a slot ('{') or a glob ('*').
_TOKEN = r"['\"]?([^\s'\"(),;]*[*{][^\s'\"(),;]*)['\"]?"
_PAIR = re.compile(_TOKEN + r"\s+(?:→|->|to)\s+" + _TOKEN)
_SLOT = re.compile(r"\{(\w+)\}")


def _wizard_prompt(monkeypatch) -> str:
    prompts: list = []
    _author_resolve(monkeypatch, ("", ""), prompts=prompts)
    (prompt,) = prompts
    return prompt


def _chat_prompt() -> str:
    return oa._build_resolve_prompt("plant-x-hub", "m", "e", "s", "t")


def _prompts(monkeypatch):
    return {"wizard": _wizard_prompt(monkeypatch), "chat": _chat_prompt()}


@pytest.mark.parametrize("path", ["wizard", "chat"])
def test_each_resolve_prompt_states_the_storage_rule(monkeypatch, path):
    """Values that match unchanged leave both patterns empty; a translation
    is literal text around a shared {placeholder}; a regular expression can't
    be stored. Nothing tells the model an empty pattern is wrong."""
    prompt = _prompts(monkeypatch)[path]
    assert re.search(r"leave source_pattern and target_pattern (both )?empty", prompt), path
    assert "regular expression" in prompt, path
    assert "{placeholder}" in prompt, path
    for forbidden in ("or empty string", "concrete value format", "concrete pattern",
                      "avoid a bare"):
        assert forbidden not in prompt, (path, forbidden)


@pytest.mark.parametrize("path", ["wizard", "chat"])
def test_the_storage_rule_example_is_storable_and_translates(monkeypatch, path):
    """Every example pair on the line that states the storage rule passes
    provision's check and translates a value through the runtime reader."""
    rule_lines = [line for line in _prompts(monkeypatch)[path].splitlines()
                  if "regular expression" in line]
    pairs = [(a.rstrip(".,"), b.rstrip(".,")) for line in rule_lines for a, b in _PAIR.findall(line)]
    assert pairs, path
    for source, target in pairs:
        assert identity_pattern_problem(source, target) is None, (source, target)
        assert apply(source, target, _SLOT.sub("7", source)) == _SLOT.sub("7", target)
