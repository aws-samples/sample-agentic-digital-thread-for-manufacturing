# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Composed-context usage telemetry — did accepted context reach the turn?

``rules_applied`` records graph RULES traversed, so a learned guidance note —
prompt text with no rule node — was invisible to it. A live run (2026-08-04)
had all five questions report ``rules_applied: []`` while one demonstrably
used an accepted note, conflating three different states: context absent,
context present but ignored, context used but unreported. ``composed_context``
separates the first two from the third by reporting COMPOSITION (the artifact
was placed in the prompt) — never that the model obeyed it.

Pure/offline: the primitives take plain dicts; the runtime producer parses a
synthetic route seed. No dataset literals.
"""
from __future__ import annotations

import agents.query_intelligence as qi
import agents.runtime_app as ra


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def test_learned_notes_drops_base_and_strips_the_prefix():
    text = "Base onboarded guidance. LEARNED: note one LEARNED: note two"
    assert qi.learned_notes(text) == ["note one", "note two"]


def test_learned_notes_empty_without_the_marker():
    assert qi.learned_notes("Base guidance only.") == []
    assert qi.learned_notes("") == []
    assert qi.learned_notes(None) == []


def test_artifact_digest_is_stable_and_carries_no_text():
    note = "some accumulated learning about coded values"
    digest = qi.artifact_digest(note)
    assert digest == qi.artifact_digest(f"  {note}  ")  # whitespace-insensitive
    assert len(digest) == qi.ARTIFACT_DIGEST_CHARS
    assert note not in digest  # untrusted text never enters the payload
    assert digest != qi.artifact_digest(note + " different")


def test_composed_context_reports_artifacts_per_system():
    block = qi.build_composed_context({
        "sys-b": "Base. LEARNED: b one",
        "sys-a": "Base. LEARNED: a one LEARNED: a two",
    })
    assert block["artifact_count"] == 3
    assert block["systems_with_context"] == 2
    # Deterministic ordering by system id.
    assert [a["system"] for a in block["artifacts"]] == ["sys-a", "sys-a", "sys-b"]
    assert {a["kind"] for a in block["artifacts"]} == {"guidance_note"}


def test_composed_context_absent_when_nothing_was_composed():
    """Honest-data rule: absence is ABSENT, never a fabricated zero."""
    assert qi.build_composed_context({}) is None
    assert qi.build_composed_context({"sys": "Base guidance, no notes"}) is None
    assert qi.build_composed_context(None) is None


def test_composed_context_artifacts_are_capped():
    many = " ".join(f"LEARNED: note {i}" for i in range(qi.MAX_COMPOSED_ARTIFACTS + 15))
    block = qi.build_composed_context({"sys": f"Base. {many}"})
    assert len(block["artifacts"]) == qi.MAX_COMPOSED_ARTIFACTS
    # The COUNT stays truthful even though the list is capped.
    assert block["artifact_count"] == qi.MAX_COMPOSED_ARTIFACTS + 15


# --------------------------------------------------------------------------- #
# Summary integration — additive, never changes the pinned base key-set
# --------------------------------------------------------------------------- #
def _summary(**overrides):
    kwargs = {"tool_names": ["explore_graph", "q"], "wasted_calls": 0,
              "rules_applied": []}
    kwargs.update(overrides)
    return qi.build_intelligence_summary(**kwargs)


def test_summary_omits_composed_context_by_default():
    assert "composed_context" not in _summary()
    assert "composed_context" not in _summary(composed_context=None)
    assert "composed_context" not in _summary(composed_context={})


def test_summary_carries_composed_context_when_present():
    block = qi.build_composed_context({"sys": "Base. LEARNED: a note"})
    summary = _summary(composed_context=block)
    assert summary["composed_context"]["artifact_count"] == 1
    # Base counters are untouched.
    assert summary["total_tool_calls"] == 2 and summary["neptune_queries"] == 1


def test_composed_context_is_distinct_from_rules_applied():
    """The whole point: a guidance note has no rule node, so it can never show
    up in rules_applied — the two blocks measure different things."""
    block = qi.build_composed_context({"sys": "Base. LEARNED: a note"})
    summary = _summary(rules_applied=[], composed_context=block)
    assert summary["rules_applied"] == []
    assert summary["composed_context"]["artifact_count"] == 1


# --------------------------------------------------------------------------- #
# Runtime producer — parses the SAME seed planned_with/seed_rule_index use
# --------------------------------------------------------------------------- #
def _seed(systems) -> str:
    import json
    return (f"prefix {ra._ROUTE_SEED_RESULT_MARKER}"
            + json.dumps({"systems": systems}) + "\n\n---\n\nthe question")


def test_producer_reads_guidance_out_of_the_route_seed():
    text = _seed([
        {"id": "sys-a", "query_guidance": "Base. LEARNED: seed note"},
        {"id": "sys-b", "query_guidance": "Base only"},
    ])
    block = ra._composed_context_from_text(text)
    assert block["artifact_count"] == 1
    assert block["systems_with_context"] == 1
    assert block["artifacts"][0]["system"] == "sys-a"


def test_producer_is_fail_soft():
    """Mode B (no marker), malformed payloads, and junk all yield None — the
    key stays absent rather than breaking the turn."""
    for text in (None, "", "no marker here", 42,
                 f"x {ra._ROUTE_SEED_RESULT_MARKER} not-json",
                 _seed([]), _seed(["not-a-dict"])):
        assert ra._composed_context_from_text(text) is None


def test_producer_tolerates_a_missing_id_or_non_string_guidance():
    text = _seed([{"query_guidance": "Base. LEARNED: orphan"},
                  {"id": "sys-c", "query_guidance": {"not": "a string"}}])
    assert ra._composed_context_from_text(text) is None


def test_learned_prefix_has_one_home():
    """self_learning must not spell the marker again — drift between the write
    format and the readers would silently blind this telemetry."""
    import agents.self_learning as sl
    assert sl._LEARNED_PREFIX is qi.LEARNED_NOTE_PREFIX


def test_both_summary_emitters_forward_composed_context():
    """The wiring bug this pins: the key was passed at the LEGACY emitter only,
    so a live run reported no composed artifacts while the notes demonstrably
    reached the model. Production emits via ``final_events``; both call sites
    must forward the state field or the telemetry is silently blind.
    """
    import inspect
    src = inspect.getsource(ra)
    assert src.count("composed_context=getattr(") >= 2, (
        "every build_intelligence_summary call site must forward composed_context")


def test_the_legacy_production_state_carries_composed_context():
    """The SECOND wiring bug, found live: response integrity is OPT-IN
    (RESPONSE_INTEGRITY_MODE defaults to report-only), so the integrity
    invocation is NOT what a default deployment runs. Setting the field only
    there left every default env reporting no composed artifacts. Both the
    legacy state builder AND the integrity path must set it.
    """
    import inspect
    src = inspect.getsource(ra)
    # The legacy state builder gets the field via the shared seed-derived
    # helper, which is spread into its namespace.
    assert '"composed_context": _composed_context_from_text(prompt)' in src, (
        "the legacy (default/production) invocation state must set it")
    assert "**_seed_derived_turn_fields(prompt, planned_with)" in src, (
        "the legacy state must spread the seed-derived fields")
    assert "state.composed_context = _composed_context_from_text(" in src, (
        "the integrity invocation state must set it too")


def test_structured_composition_telemetry_records_scopes_and_digests_only():
    from agents.guidance_scope import GuidanceScope, append_note, parse_document

    document = append_note(
        parse_document(""), "bounded query hint", GuidanceScope.QUERY_HINT)
    document = append_note(
        document, "final answer requirement", GuidanceScope.ANSWER_REQUIREMENT)
    block = qi.build_composed_context({"sys": document})

    assert block["scope_counts"] == {
        "query_hint": 1, "answer_requirement": 1, "legacy_unscoped": 0}
    assert {artifact["scope"] for artifact in block["artifacts"]} == {
        "query_hint", "answer_requirement"}
    assert all(set(artifact) == {"system", "kind", "scope", "digest"}
               for artifact in block["artifacts"])
    assert "bounded query hint" not in repr(block)
    assert "final answer requirement" not in repr(block)
    assert "obey" not in repr(block).lower()


def test_runtime_producer_reads_scope_digests_from_route_metadata():
    from agents.guidance_scope import composition_metadata

    text = _seed([{
        "id": "sys-a",
        "query_guidance": "----- BEGIN QUERY HINTS -----",
        "guidance_composition": composition_metadata(
            "Base. LEARNED: legacy note"),
    }])
    block = ra._composed_context_from_text(text)

    assert block["scope_counts"] == {
        "query_hint": 0, "answer_requirement": 0, "legacy_unscoped": 1}
    assert block["artifacts"][0]["scope"] == "legacy_unscoped"
    assert "legacy note" not in repr(block)
