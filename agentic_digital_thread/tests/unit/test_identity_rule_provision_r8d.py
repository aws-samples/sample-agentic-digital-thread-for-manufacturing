# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 8d: what dd16ca4d's in-window settle and query precheck still missed.

* A retired-link sweep that failed inside the marked window was logged, and
  the mark was then cleared as if the field concepts were final: a peer that
  re-measured a rule in between earned a measured method from the link the
  steward had retired. A rewrite that raised part-way was cleared the same
  way. The sweep is now retried inside the window; a window that still
  cannot settle (a failed sweep, a step that raised) leaves the System node
  with a failed marker readers treat as "could not run", the provision fails
  as before, and the marker is cleared by the system's next settled rewrite
  or, once stale, by the reader that finds it. The clear names the run's own
  mark, so it never removes a newer one.

Round 8e's follow-ups (the start mark, a landing that did not land, list
subscripts in model queries) are in ``test_identity_rule_provision_r8e.py``.

The world is ``_schema_rewrite_world`` (neutral systems, fake graph and clock).
"""
from __future__ import annotations

import time

import agents.onboard as onboard
from agents.identity_affinity import AFFINITY_CONFIDENCE, EXACT_METHOD
from agents.models import ProvisionRequest
from tests.unit._schema_rewrite_world import (  # noqa: F401 - world is a fixture
    SRC_MAPPINGS, SRC_SYS, SYSTEMS, TARGET_FIELD, TGT_CONCEPT, TGT_MAPPINGS, TGT_SYS,
    resolve_rules, schema_of, target_rewrite_starts, the_node, world)

RETIRED = [{"concept": TGT_CONCEPT, "field": "lot_lines.lot_ref"}]
UNSETTLED = f"the schema of '{TGT_SYS}' was left unsettled by its own provision"

#: Every coordinator phase but b0, b2, b3b, b3c and the identity-rule phase:
#: the target's re-provision writes its schema for real; ``_dp_valueoverlap_joinpaths``
#: (b4, the first step after the clear) is where a peer's provision reads.
_STUBBED = ("_dp_connection_phase", "_dp_discover_athena_physical", "_dp_athena_physical_fields",
            "_dp_discovery_completeness", "_dp_reapply_user_corrections", "_dp_identity_key_edges",
            "_dp_mint_unmapped", "_dp_internal_joinpaths", "_dp_mirrored_measure_tags",
            "_dp_samerow_joinpaths", "_dp_identity_rules", "_dp_rule_joinpaths",
            "_dp_cross_system_vms", "_dp_equipment_vms_legacy", "_dp_authority_rules",
            "_dp_onboarding_log", "_dp_finalize", "_dp_start_pg_agent",
            "_provision_i3x_post_pass", "_provision_knowledge_post_pass")


def _peer_provision(rules) -> object:
    """The source system's provision (the reader): its identity-rule phase."""
    ctx = onboard._make_provision_state(ProvisionRequest(
        system_id=SRC_SYS, system_name=SRC_SYS, schema=schema_of(SRC_SYS),
        plant=SYSTEMS[SRC_SYS]["plant"], rules=[dict(r) for r in rules],
        mappings=[dict(m) for m in SRC_MAPPINGS], connection_type="PostgreSQL"))
    ctx.tables_seen = {"runs"}
    _REAL_IDENTITY_RULES(ctx)
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    return ctx


_REAL_IDENTITY_RULES = onboard._dp_identity_rules


def _target_reprovision(monkeypatch, *, peer_rules=None, raise_in=None):
    """The target system's re-provision through ``_do_provision_writes``; with
    ``peer_rules`` the source's provision reads at b4, right after the
    target's mark was cleared. Returns ``(result, peer ctx)``."""
    for name in _STUBBED:
        monkeypatch.setattr(onboard, name, lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_dp_collect_tables",
                        lambda ctx: ctx.tables_seen.update(m["table"] for m in TGT_MAPPINGS))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    monkeypatch.setattr("agents.asset_role_pass.run_after_provision", lambda: None)
    seen: dict = {}

    def at_b4(_ctx):
        if peer_rules is not None:
            seen["peer"] = _peer_provision(peer_rules)
    monkeypatch.setattr(onboard, "_dp_valueoverlap_joinpaths", at_b4)
    if raise_in:
        def boom(_ctx):
            raise RuntimeError(f"{raise_in} failed")
        monkeypatch.setattr(onboard, raise_in, boom)
    body = ProvisionRequest(system_id=TGT_SYS, system_name=TGT_SYS, schema=schema_of(TGT_SYS),
                            plant=SYSTEMS[TGT_SYS]["plant"], mappings=[dict(m) for m in TGT_MAPPINGS],
                            connection_type="PostgreSQL")
    try:
        result = onboard._do_provision_writes(body)
    except RuntimeError as exc:
        result = {"raised": str(exc)}
    return result, seen.get("peer")


def _retired_after_resolve(monkeypatch, world):
    """Resolve measured the rule; the steward then retired the target field's
    concept link (its earlier sweep removed it, b2 will write it back)."""
    rules = resolve_rules()
    world.graph.fields[TARGET_FIELD] = []
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links",
                        lambda sid: list(RETIRED) if sid == TGT_SYS else [])
    return rules


def _delete_refused(world, times: int):
    """The sweep's delete of the retired link is refused ``times`` times, then runs."""
    left = {"n": times}

    def refuse(cypher):
        if cypher == onboard._RETIRED_LINK_DELETE_CYPHER and left["n"] > 0:
            left["n"] -= 1
            return True
        return False
    world.graph.fail_writes = refuse


# ── 1. a window that cannot settle is not cleared as settled ───────────────

def test_a_sweep_that_fails_once_in_the_window_is_retried_before_the_clear(world, monkeypatch):
    """REPRODUCTION. The in-window sweep's delete is refused once. dd16ca4d
    logged it and cleared the mark; a peer reading at b4 earned
    value_exact_match from the link the steward retired (the late sweep had
    not run yet). The sweep is now retried inside the window, so the peer
    finds no concept and the method is not earned."""
    rules = _retired_after_resolve(monkeypatch, world)
    _delete_refused(world, 1)
    result, peer = _target_reprovision(monkeypatch, peer_rules=rules)
    assert result.get("status") == "provisioned"
    node = the_node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = peer.rules_unchecked
    assert "found no concept both fields map to" in entry["reason"]
    assert TGT_SYS not in world.graph.marks and TGT_SYS not in world.graph.failed


def test_a_sweep_that_keeps_failing_leaves_a_failed_marker_readers_refuse(world, monkeypatch):
    """REPRODUCTION. The delete keeps being refused. dd16ca4d cleared the mark
    and the peer earned the method from the retired link. Now the window
    ends with a failed marker: the peer's re-measurement could not run, and
    the target's provision fails with the sweep's error, as before."""
    rules = _retired_after_resolve(monkeypatch, world)
    _delete_refused(world, 10_000)
    result, peer = _target_reprovision(monkeypatch, peer_rules=rules)
    assert result["reason"] == "retired_link_sweep_failed"
    assert TGT_SYS not in world.graph.marks and TGT_SYS in world.graph.failed
    node = the_node(world)
    assert (node["method"], node["confidence"]) == ("provision-request", 0.7)
    (entry,) = peer.rules_unchecked
    assert f"could not run: {UNSETTLED}" in entry["reason"]


def test_a_rewrite_that_raises_leaves_a_failed_marker(world, monkeypatch):
    """REPRODUCTION. The target's re-provision raises after b0 deleted its
    schema and before its fields were written back. dd16ca4d's finally
    cleared the mark, advertising the half-written schema as settled; a peer
    then refused the rule as if the field mapped nothing. The failed marker
    says the schema is not settled."""
    rules = resolve_rules()
    result, _peer = _target_reprovision(monkeypatch, raise_in="_dp_field_mappings")
    assert result == {"raised": "_dp_field_mappings failed"}
    assert TGT_SYS not in world.graph.marks and TGT_SYS in world.graph.failed
    peer = _peer_provision(rules)
    (entry,) = peer.rules_unchecked
    assert f"could not run: {UNSETTLED}" in entry["reason"]


def test_the_next_settled_rewrite_clears_the_failed_marker(world, monkeypatch):
    """REPRODUCTION (new): the system's next rewrite that settles removes the
    failed marker its earlier run left, so readers measure again."""
    rules = resolve_rules()
    world.graph.failed[TGT_SYS] = int(time.time() * 1000)
    result, peer = _target_reprovision(monkeypatch, peer_rules=rules)
    assert result.get("status") == "provisioned"
    assert TGT_SYS not in world.graph.failed and TGT_SYS not in world.graph.marks
    assert peer.rules_unchecked == []
    assert (the_node(world)["method"], the_node(world)["confidence"]) == (EXACT_METHOD,
                                                                           AFFINITY_CONFIDENCE)


def test_a_stale_failed_marker_is_removed_by_its_value_and_the_field_read(world):
    """REPRODUCTION (new): a failed marker older than the staleness bound (a
    system nobody re-provisioned) is removed with a compare-and-clear on the
    value read, and the field is measured as it stands."""
    rules = resolve_rules()
    world.graph.failed[TGT_SYS] = stale = 1_000
    peer = _peer_provision(rules)
    assert TGT_SYS not in world.graph.failed
    (clear,) = [(c, p) for c, p in world.graph.mark_writes if "REMOVE" in c]
    assert "WHERE s.schema_rewrite_failed = $failed" in clear[0]
    assert clear[1] == {"sid": TGT_SYS, "failed": stale}
    assert peer.rules_unchecked == [] and the_node(world)["method"] == EXACT_METHOD


def test_the_clear_never_removes_a_mark_another_run_wrote(world, monkeypatch):
    """REPRODUCTION: the clear names the run's own mark. dd16ca4d removed
    whatever mark the node carried."""
    real_entries = onboard._dp_schema_entries

    def b0_then_replaced(ctx):
        real_entries(ctx)
        world.graph.marks[TGT_SYS] = 42                  # another run's mark
    monkeypatch.setattr(onboard, "_dp_schema_entries", b0_then_replaced)
    _target_reprovision(monkeypatch)
    assert world.graph.marks[TGT_SYS] == 42
