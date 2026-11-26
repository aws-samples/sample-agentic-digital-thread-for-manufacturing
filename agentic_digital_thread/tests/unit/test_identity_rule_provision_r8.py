# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Provision's re-measurement of a measured rule, on the path the seed takes.

The seed runs Resolve for a system, then sends Resolve's rules and the same
mapping rows to provision. Resolve's exact pass reads the source field's
concepts from those mapping rows and the target field's from the graph's
MAPS_TO edges; provision's re-measurement reads them from the same two places
(the same rows, the same edges of the same table and column). The graph is
the one input that can change in between: a re-provision of the TARGET
system deletes its schema subgraph (b0) and writes it again field by field
(b2 to b3a), and for that window the target field has no concept. A
genuinely measured rule then lost its method ("found no concept both fields
map to"; the workshop seed, 2026-10-21).

The re-provision now marks its System node while it rewrites its schema (set
before the b0 delete, cleared after b3a, also when a step raises), and the
re-measurement waits, bounded, while the target carries a fresh mark. A
target that is not marked is read at once, as before. The world (a cell line
and a ledger on a catalog-related concept pair, the fake graph and clock) is
``_schema_rewrite_world``; round 8b's follow-ups are in
``test_identity_rule_provision_r8b.py``.
"""
from __future__ import annotations

import logging
import types

import pytest

import agents.onboard as onboard
from agents.identity_affinity import AFFINITY_CONFIDENCE, EXACT_METHOD
from tests.unit._schema_rewrite_world import (  # noqa: F401 - world is a fixture
    TARGET_FIELD, TGT_SYS, provision_rules as _provision, resolve_rules as _resolve,
    target_rewrite_ends as _target_rewrite_ends, target_rewrite_starts as _target_rewrite_starts,
    the_node as _node, world)


def test_the_seeds_measured_rule_keeps_value_exact_match_on_the_real_path(world):
    """Guard: Resolve's exact pass, then provision with the same mapping rows,
    on a catalog-related concept pair and a graph nobody is rewriting. The
    guards in test_identity_rule_provision_r4.py and _r5.py feed provision a
    rule built by hand with one concept on both sides and a graph that
    answers every field with it; this one runs
    Resolve itself, as the seed does."""
    ctx = _provision(_resolve())
    assert ctx.rules_rejected == [] and ctx.rules_unchecked == []
    assert (_node(world)["method"], _node(world)["confidence"]) == (EXACT_METHOD, AFFINITY_CONFIDENCE)
    assert world.sleeps == []


def test_a_target_rewriting_its_schema_does_not_downgrade_the_measured_rule(world, caplog):
    """REPRODUCTION (the workshop seed, 2026-10-21 10:08:59): after the source
    system's Resolve, the target system's own re-provision deleted its schema
    subgraph and had not written the field back when the source's provision
    re-measured the rule. 43c4e971 read no concept for the target field and
    stored provision-request ("found no concept both fields map to"). The
    re-measurement now waits for the rewrite to finish, then measures."""
    rules = _resolve()
    target = _target_rewrite_starts()
    world.on_sleep = lambda n: _target_rewrite_ends(target) if n == 2 else None
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ctx = _provision(rules)
    assert not [r.getMessage() for r in caplog.records if "method not earned" in r.getMessage()]
    assert ctx.rules_unchecked == []
    assert (_node(world)["method"], _node(world)["confidence"]) == (EXACT_METHOD, AFFINITY_CONFIDENCE)
    assert len(world.sleeps) == 2


def test_a_rewrite_that_outlasts_the_wait_still_downgrades_and_says_why(world):
    """Fail closed: the target is still rewriting its schema when the bounded
    wait runs out, so the method is not earned, and the reason names the
    rewrite rather than a missing concept."""
    rules = _resolve()
    _target_rewrite_starts()
    ctx = _provision(rules)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = ctx.rules_unchecked
    assert f"the schema of '{TGT_SYS}' is being rewritten by its own provision" in entry["reason"]
    assert sum(world.sleeps) <= onboard._SCHEMA_REWRITE_WAIT_S


def test_one_wait_budget_per_provision_run(world):
    """Two measured rules to one target that is still rewriting: both wait
    on the same budget, so the second does not wait the whole bound again."""
    rule = _resolve()[0]
    twin = {**rule, "reasoning": rule["reasoning"] + " "}
    _target_rewrite_starts()
    ctx = _provision([rule, twin])
    assert [("being rewritten" in e["reason"]) for e in ctx.rules_unchecked] == [True, True]
    assert sum(world.sleeps) <= onboard._SCHEMA_REWRITE_WAIT_S


def test_a_field_with_no_related_concept_is_refused_without_waiting(world):
    """Guard: the gate is not loosened. A target field that maps no related
    concept, on a system that is not rewriting its schema, is refused at once
    as before, and nothing waits."""
    rules = _resolve()
    world.graph.add_field(*TARGET_FIELD)
    ctx = _provision(rules)
    node = _node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = ctx.rules_unchecked
    assert "found no concept both fields map to" in entry["reason"]
    assert world.sleeps == []


def test_a_stale_mark_from_a_stopped_provision_is_not_waited_on(world):
    """Guard: a mark older than the staleness bound (a provision that stopped
    between b0 and b3a without clearing it) does not make later provisions
    wait; the field is read as it stands."""
    rules = _resolve()
    world.graph.marks[TGT_SYS] = 1_000                         # epoch ms, long ago
    ctx = _provision(rules)
    assert ctx.rules_unchecked == []
    assert _node(world)["method"] == EXACT_METHOD
    assert world.sleeps == []


def test_b0_marks_the_system_before_it_deletes_its_schema(world, monkeypatch):
    """The mark is on the System node before the b0 delete, so a reader never
    sees the deleted schema unmarked."""
    order: list = []
    monkeypatch.setattr(onboard, "_mark_schema_rewrite",
                        lambda sid: order.append(("mark", sid)) or 1, raising=False)
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph",
                        lambda sid: order.append(("delete", sid)) or 0)
    _target_rewrite_starts()
    assert order[:2] == [("mark", TGT_SYS), ("delete", TGT_SYS)]


_PHASES = ("_dp_collect_tables", "_dp_discover_athena_physical", "_dp_schema_entries",
           "_dp_field_mappings", "_dp_athena_physical_fields", "_dp_discovery_completeness",
           "_dp_reapply_user_corrections", "_dp_identity_key_edges", "_dp_mint_unmapped",
           "_dp_internal_joinpaths", "_dp_mirrored_measure_tags", "_dp_settle_field_concepts",
           "_dp_valueoverlap_joinpaths",
           "_dp_samerow_joinpaths", "_dp_identity_rules", "_dp_rule_joinpaths",
           "_dp_cross_system_vms", "_dp_equipment_vms_legacy", "_dp_authority_rules",
           "_dp_onboarding_log", "_dp_finalize", "_dp_schema_rewritten")


def _coordinator(monkeypatch, raise_in=None) -> list:
    ran: list = []

    def phase(name):
        def run(_ctx):
            ran.append(name)
            if name == raise_in:
                raise RuntimeError(f"{name} failed")
        return run
    monkeypatch.setattr(onboard, "_make_provision_state",
                        lambda body: types.SimpleNamespace(system_id=TGT_SYS, body=body))
    for name in _PHASES:
        monkeypatch.setattr(onboard, name, phase(name), raising=False)
    monkeypatch.setattr(onboard, "_dp_connection_phase", lambda ctx: None)
    monkeypatch.setattr(onboard, "_dp_start_pg_agent", lambda ctx: ran.append("_dp_start_pg_agent"))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    return ran


def test_the_coordinator_clears_the_mark_after_the_last_field_write(monkeypatch):
    """The mark is cleared after b3a's measure tags and b3b (the emergent
    landing and the retired-link sweep, the last steps that change field
    concepts) and before b4. 7d0e6784 cleared it after b2b, before b3a's
    measure tags were written."""
    ran = _coordinator(monkeypatch)
    onboard._do_provision_base(types.SimpleNamespace(system_id=TGT_SYS))
    assert "_dp_schema_rewritten" in ran
    assert ran.index("_dp_mirrored_measure_tags") < ran.index("_dp_settle_field_concepts") \
        < ran.index("_dp_schema_rewritten") < ran.index("_dp_valueoverlap_joinpaths")


def test_the_coordinator_clears_the_mark_when_a_schema_step_raises(monkeypatch):
    """A provision that fails between b0 and b3a does not leave its system
    marked as rewriting (other provisions would wait on it)."""
    ran = _coordinator(monkeypatch, raise_in="_dp_field_mappings")
    with pytest.raises(RuntimeError, match="_dp_field_mappings failed"):
        onboard._do_provision_base(types.SimpleNamespace(system_id=TGT_SYS))
    assert ran[-1] == "_dp_schema_rewritten"
