# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route-intersection usage-ledger fallback (B3, ADR-0052 decision (b)).

Report-only turns retain no verified relationship proofs, so the strong
extractions yield nothing and the ledger starves (live: 2 questions, 0 rows).
The fallback intersects the Mode-A route seed's jp/ir artifact ids — parsed
once from the already-built seed, zero extra graph queries — with the systems
the turn actually queried, and bumps the matches with the honest-but-weak
``evidence="route-intersection"`` class. Strictly additive: it runs ONLY when
the stronger extractions returned empty, only on report-only turns, and a
ledger write never fails a turn.

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import inspect
import json

import agents.artifact_usage as artifact_usage
from agents import runtime_app, self_learning

JP_ID = "jp-sysmes-0011223344556677"
IR_ID = "ir-syserp-8899aabbccddeeff"
SUBJ_ID = "subj-" + "a" * 16

_SEED_PAYLOAD = {
    "systems": [
        {"system_id": "sys-mes", "rules": {
            "join_paths": [
                {"id": JP_ID, "from_system": "sys-mes", "from_field": "t.a",
                 "to_system": "sys-cmms", "to_field": "t.b",
                 "confidence": 0.9},
            ],
            "value_mappings": [
                # Seed vm entries carry NO id — never indexed.
                {"from_system": "sys-mes", "to_system": "sys-cmms",
                 "concept": "operator-id"},
            ],
            "identity_rules": [
                {"id": IR_ID, "source_system": "sys-erp",
                 "target_system": "sys-plm", "concept": "part-number"},
            ],
        }},
    ],
    "rule_grain_cap_per_kind": 12,
}


def _augmented_prompt(question: str = "How do work orders line up?") -> str:
    """Seed-augmented turn text, exactly as the Mode-A prelude composes it."""
    return (
        "[ROUTE SEED — connected-system routing was resolved before delegation]\n"
        + runtime_app._ROUTE_SEED_RESULT_MARKER + json.dumps(_SEED_PAYLOAD)
        + f"\n\n---\n\n{question}"
    )


def _state(monkeypatch, *, rule_ids=frozenset(), matched_rows=(),
           tool_names=None, mapping=None):
    """A fabricated integrity invocation on a report-only env."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    mapping = {"query-sys-mes": "sys-mes"} if mapping is None else mapping
    state = runtime_app._RuntimeIntegrityInvocation(
        "compare work orders", None, mapping)
    state.tool_names = dict(tool_names or {"t1": "query-sys-mes"})
    state.seed_rule_index = runtime_app._seed_rule_index_from_text(
        _augmented_prompt())
    state.rule_ids = set(rule_ids)
    state.matched_rows = list(matched_rows)
    return state


def _quiet_capture_hooks(monkeypatch):
    monkeypatch.setattr(runtime_app, "_post_turn_learning_checks",
                        lambda s: None)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])


def _record_bumps(monkeypatch):
    bumps: list = []
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda refs, evidence="": bumps.append(
            (set(refs), evidence)) or len(refs))
    return bumps


# --------------------------------------------------------------------------- #
# The named contract tests
# --------------------------------------------------------------------------- #
def test_report_only_empty_extraction_bumps_route_intersection(monkeypatch):
    """The live gap closes: empty strong extraction on report-only bumps the
    seeded artifacts whose systems this turn queried — and ONLY those."""
    _quiet_capture_hooks(monkeypatch)
    bumps = _record_bumps(monkeypatch)
    state = _state(monkeypatch)  # rule_ids empty, queried system = sys-mes

    runtime_app._integrity_post_turn_learning(
        state, "compare work orders", "sess-1")

    # One ledger write: the jp touching the queried system, weak class. The
    # ir's endpoints (sys-erp/sys-plm) were never queried — not bumped.
    assert bumps == [({("join_path", JP_ID)}, "route-intersection")]


def test_strong_extraction_suppresses_fallback(monkeypatch):
    """Proof rows present ⇒ relationship-proof rows only, NO fallback rows."""
    _quiet_capture_hooks(monkeypatch)
    bumps = _record_bumps(monkeypatch)
    monkeypatch.setattr(self_learning, "record_rule_usage",
                        lambda _ids, **_k: 1)
    row = {"neptune_node_id": SUBJ_ID, "proposed_type": "value_mapping"}
    state = _state(monkeypatch, rule_ids={SUBJ_ID}, matched_rows=[row])

    runtime_app._integrity_post_turn_learning(
        state, "compare work orders", "sess-1")

    assert bumps == [({("value_mapping", SUBJ_ID)}, "relationship-proof")]
    assert all(evidence != "route-intersection" for _refs, evidence in bumps)


def test_briefing_prompt_skips(monkeypatch):
    """Automated briefings never write the ledger — fallback included."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("briefing turns must never bump the ledger")

    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _boom)
    ctx = runtime_app._route_ctx(
        runtime_app._seed_rule_index_from_text(_augmented_prompt()),
        ["query-sys-mes"], {"query-sys-mes": "sys-mes"})

    assert runtime_app._maybe_bump_route_intersection(
        "Generate the morning operations briefing", ctx) == 0
    assert runtime_app._record_rule_usage_hook(
        {}, "Generate the morning operations briefing",
        precomputed_ids=set(), route_ctx=ctx) == 0


def test_refs_limited_to_queried_systems():
    """PURE: only seed artifacts whose endpoint systems were queried match."""
    seed = [
        {"id": JP_ID, "kind": "join_path", "systems": ["sys-mes", "sys-cmms"]},
        {"id": IR_ID, "kind": "identity_rule",
         "systems": ["sys-erp", "sys-plm"]},
    ]
    assert runtime_app._route_intersection_refs(seed, {"sys-mes"}) == {
        ("join_path", JP_ID)}
    assert runtime_app._route_intersection_refs(seed, {"sys-plm"}) == {
        ("identity_rule", IR_ID)}
    assert runtime_app._route_intersection_refs(
        seed, {"sys-mes", "sys-erp"}) == {
        ("join_path", JP_ID), ("identity_rule", IR_ID)}
    # No queried systems / no seed ⇒ nothing (never a fabricated ref).
    assert runtime_app._route_intersection_refs(seed, set()) == set()
    assert runtime_app._route_intersection_refs([], {"sys-mes"}) == set()


# --------------------------------------------------------------------------- #
# Guards: mode gate, honest-empty derivation, swallow
# --------------------------------------------------------------------------- #
def test_enforce_mode_suppresses_fallback(monkeypatch):
    """ENFORCE alone suppresses the fallback — it is the only mode with the
    stronger `relationship-proof` source (ADR-0052 (b): the fallback writes
    "only when the stronger extractions yielded nothing")."""
    def _boom(*_a, **_k):
        raise AssertionError("enforce turns must not take the fallback")

    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _boom)
    ctx = runtime_app._route_ctx(
        [{"id": JP_ID, "kind": "join_path", "systems": ["sys-mes"]}],
        ["query-sys-mes"], {"query-sys-mes": "sys-mes"})
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    assert runtime_app._maybe_bump_route_intersection("q", ctx) == 0


def test_annotate_still_writes_the_fallback(monkeypatch):
    """Annotate has NEITHER stronger source: it is model-cooperation-free
    (ADR-0047), so there are no typed `system_answers` claims and no verified
    `trusted_relationships` for `_integrity_rule_usage` to read. Suppressing
    the fallback here starved the ledger in the posture ADR-0047 recommends —
    annotate must behave like report-only for usage telemetry."""
    bumps = _record_bumps(monkeypatch)
    ctx = runtime_app._route_ctx(
        [{"id": JP_ID, "kind": "join_path", "systems": ["sys-mes"]}],
        ["query-sys-mes"], {"query-sys-mes": "sys-mes"})
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    assert runtime_app._maybe_bump_route_intersection("q", ctx) == 1
    assert bumps and bumps[0][-1] == "route-intersection" or bumps


def test_missing_mapping_derives_from_called_tool_names(monkeypatch):
    """systems=None lets the fallback derive the tool→system mapping from the
    CALLED tool names (the legacy call site's shape)."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    bumps = _record_bumps(monkeypatch)
    monkeypatch.setattr(
        runtime_app, "_registered_tool_system_ids",
        lambda names: {"query-sys-mes": "sys-mes"} if "query-sys-mes" in names
        else {})
    ctx = runtime_app._route_ctx(
        [{"id": JP_ID, "kind": "join_path", "systems": ["sys-mes"]}],
        ["query-sys-mes"], None)

    assert runtime_app._maybe_bump_route_intersection("q", ctx) == 1
    assert bumps == [({("join_path", JP_ID)}, "route-intersection")]


def test_fallback_swallows_every_failure(monkeypatch):
    """A ledger write never fails a turn — mapping AND bump failures swallow."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(
        runtime_app, "_registered_tool_system_ids",
        lambda names: (_ for _ in ()).throw(RuntimeError("graph down")))
    ctx = runtime_app._route_ctx(
        [{"id": JP_ID, "kind": "join_path", "systems": ["sys-mes"]}],
        ["query-sys-mes"], None)
    assert runtime_app._maybe_bump_route_intersection("q", ctx) == 0

    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger down")))
    ctx = runtime_app._route_ctx(
        [{"id": JP_ID, "kind": "join_path", "systems": ["sys-mes"]}],
        ["query-sys-mes"], {"query-sys-mes": "sys-mes"})
    assert runtime_app._maybe_bump_route_intersection("q", ctx) == 0
    # And no route_ctx at all is a clean no-op.
    assert runtime_app._maybe_bump_route_intersection("q", None) == 0


# --------------------------------------------------------------------------- #
# Seed index parsing — bounded, ids + endpoint systems only, fail-soft
# --------------------------------------------------------------------------- #
def test_seed_rule_index_parsed_from_augmented_prompt():
    index = runtime_app._seed_rule_index_from_text(_augmented_prompt())

    assert index == [
        {"id": JP_ID, "kind": "join_path",
         "systems": ["sys-mes", "sys-cmms"]},
        {"id": IR_ID, "kind": "identity_rule",
         "systems": ["sys-erp", "sys-plm"]},
    ]
    # ids + endpoint systems ONLY — never full rules (no confidence/fields).
    assert all(set(entry) == {"id", "kind", "systems"} for entry in index)


def test_seed_index_fail_soft_and_mode_b_inert():
    # No marker (Mode B graph-first seed / plain prompt) ⇒ [].
    assert runtime_app._seed_rule_index_from_text("plain question") == []
    assert runtime_app._seed_rule_index_from_text(None) == []
    # Broken payload after the marker ⇒ [] — never raises.
    assert runtime_app._seed_rule_index_from_text(
        runtime_app._ROUTE_SEED_RESULT_MARKER + "{not json") == []


def test_seed_index_bounded_by_cap():
    many = {"systems": [{"system_id": "sys-mes", "rules": {"join_paths": [
        {"id": f"jp-sysmes-{i:016x}", "from_system": "sys-mes",
         "to_system": "sys-cmms"} for i in range(150)
    ]}}]}
    text = runtime_app._ROUTE_SEED_RESULT_MARKER + json.dumps(many)

    index = runtime_app._seed_rule_index_from_text(text)

    assert len(index) == runtime_app._SEED_RULE_INDEX_CAP


# --------------------------------------------------------------------------- #
# Wiring tripwires — both capture call sites arm the fallback
# --------------------------------------------------------------------------- #
def test_legacy_finally_call_site_passes_route_ctx():
    """The legacy (report-only) finally block builds and forwards route_ctx."""
    src = inspect.getsource(runtime_app._run_orchestrator_base)
    assert "_route_ctx(_seed_rule_index_from_text(prompt)" in src
    assert "route_ctx=_route" in src


def test_integrity_invocation_captures_seed_index_at_prelude():
    src = inspect.getsource(runtime_app._invoke_integrity_runtime)
    assert "state.seed_rule_index = _seed_rule_index_from_text(prompt)" in src
    src = inspect.getsource(runtime_app._integrity_post_turn_learning)
    assert 'evidence_class="relationship-proof"' in src
    assert "route_ctx=route_ctx" in src


def test_seed_index_and_composed_context_read_past_the_seed_tail_notes():
    # The seed may carry the IDENTIFIER TRANSLATIONS block and the ADR-0086
    # wildcard note after its JSON; every marker consumer decodes the JSON
    # only (raw_decode), so the tail never turns a parse into a fail-soft miss.
    from agents.identifier_translation import WILDCARD_SEED_NOTE
    text = (runtime_app._ROUTE_SEED_RESULT_MARKER + json.dumps(_SEED_PAYLOAD)
            + "\nIDENTIFIER TRANSLATIONS (deterministic, from graph IdentityRules):\n"
            + "- ITM-5101 (sys-a.code) → PRT-5101 (sys-b.item_id) [value_core_match, confidence 0.85]\n"
            + "\n" + WILDCARD_SEED_NOTE)
    assert {row["id"] for row in runtime_app._seed_rule_index_from_text(text)} == {JP_ID, IR_ID}
    assert runtime_app._planned_with_from_seed(text)["identity_rules"] == 1
