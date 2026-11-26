# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integrity-path post-turn learning (R1) + honest proposal context (R2).

The audited defect: under ``RESPONSE_INTEGRITY_MODE=enforce`` the usage
ledger stayed EMPTY and no post-turn learning ran — the legacy ``finally``
seam (usage hook + the three capture hooks) had no integrity-path
equivalent. ``runtime_app._integrity_post_turn_learning`` now mirrors that
seam, hanging off the invocation's completion (after ``final_events``
yields):

- the usage-ledger write reuses ``final_events``' single extraction
  (``precomputed_ids``/``prefetched_rows`` — never a re-scan);
- ``_post_turn_learning_checks`` / ``_capture_correlation_insights`` /
  ``_capture_value_mapping_proposals`` are the SAME functions with the same
  gating (briefing skip, best-effort swallow);
- the prompt threaded through is the ORIGINAL user prompt (R2) — stored
  ``query_context`` is never seed-augmented boilerplate, and ``concept`` is
  parsed from the proposal block or stored ``"unknown"``, never a hardcoded
  default.

Offline: no AWS, no network. Enforce-path tests monkeypatch
``RESPONSE_INTEGRITY_MODE`` (conventions.md).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agents.artifact_usage as artifact_usage
import agents.usage_tracking as usage_tracking
from agents import runtime_app, self_learning
from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, SystemAnswer,
)

SUBJ_ID = "subj-" + "a" * 16

MATCHED_ROW = {
    "neptune_node_id": SUBJ_ID,
    "proposed_type": "value_mapping",
    "from_system": "sys-a", "from_field": "supplier_id",
    "to_system": "sys-b", "to_field": "vendor_ref",
    "concept": "supplier",
}

_CATALOG = [{"id": "erp", "name": "ERP"}, {"id": "cmms", "name": "CMMS"}]

RAW_PROMPT = "How do maintenance faults line up with orders?"

_RESPONSE_WITH_PROPOSALS = (
    "Answer prose.\n"
    "---CORRELATION-PROPOSAL---\n"
    "Pattern: Machine faults in CMMS correlate with scrap spikes in ERP\n"
    "Confidence: HIGH\n"
    "---END-PROPOSAL---\n"
    "---VALUE-MAPPING-PROPOSAL---\n"
    "from_system: cmms\n"
    "from_field: assets.asset_ref\n"
    "from_value: A-77\n"
    "to_system: erp\n"
    "to_field: equipment.equipment_no\n"
    "to_value: 4400077\n"
    "concept: equipment-reference\n"
    "confidence: high\n"
    "method: value_format_match\n"
    "---END-PROPOSAL---\n"
)


@pytest.fixture()
def enforce_mode(monkeypatch):
    """Enforce-path tests must monkeypatch the env (conventions.md)."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")


def _invocation_with_retained_evidence() -> "runtime_app._RuntimeIntegrityInvocation":
    """A fabricated invocation whose context retains a rule node id."""
    invocation = runtime_app._RuntimeIntegrityInvocation("compare supplier ids")
    invocation.context.trusted_relationships.add(
        (SUBJ_ID, "sys-a", "S-100", "supplier_id", "sys-b", "V-100", "vendor_ref"),
    )
    invocation.context.system_answers.append(SystemAnswer(
        status="complete",
        claims=(AnswerClaim(
            claim_id="c1", kind=ClaimKind.VALUE, label="supplier",
            inputs=(ClaimInput(
                label="supplier", value="S-100",
                semantic_field="supplier_ref", locator="row:0",
            ),),
        ),),
        evidence=(),
    ))
    return invocation


def _run_final_events(monkeypatch, invocation) -> None:
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    monkeypatch.setattr(
        self_learning, "get_accepted_rules_by_node_ids", lambda _ids: [MATCHED_ROW])
    list(invocation.final_events())


# --------------------------------------------------------------------------- #
# R1 — the audited defect: integrity turns must write the usage ledger.
# --------------------------------------------------------------------------- #
def test_integrity_turn_writes_usage_ledger_row(enforce_mode, monkeypatch):
    invocation = _invocation_with_retained_evidence()
    _run_final_events(monkeypatch, invocation)
    # final_events stored the SINGLE extraction for post-turn reuse.
    assert SUBJ_ID in invocation.rule_ids
    assert invocation.matched_rows == [MATCHED_ROW]

    bumps: list = []
    ledger: list = []
    monkeypatch.setattr(
        self_learning, "record_rule_usage",
        lambda ids, prefetched_rows=None: bumps.append(
            (set(ids), prefetched_rows)) or len(ids))
    monkeypatch.setattr(
        artifact_usage, "extract_artifact_refs",
        lambda *a, **k: [("rule", SUBJ_ID)])
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda refs, evidence="": ledger.append(
            (list(refs), evidence)) or len(refs))
    # Poison the scanner: the post-turn write must reuse precomputed ids.
    monkeypatch.setattr(
        usage_tracking, "extract_rule_node_ids",
        lambda _texts: (_ for _ in ()).throw(AssertionError("turn re-scanned")))
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(runtime_app, "_post_turn_learning_checks", lambda s: None)

    runtime_app._integrity_post_turn_learning(
        invocation, "compare supplier ids", "sess-1")

    assert bumps and SUBJ_ID in bumps[0][0]
    assert bumps[0][1] == [MATCHED_ROW]  # prefetched rows reused, no re-query
    # The ledger row was written, stamped with the integrity extraction's
    # honest evidence class (ADR-0052 decision (b)).
    assert ledger == [([("rule", SUBJ_ID)], "relationship-proof")]


def test_briefing_prompt_skips_learning(enforce_mode, monkeypatch):
    invocation = _invocation_with_retained_evidence()
    _run_final_events(monkeypatch, invocation)
    invocation.response_text = [_RESPONSE_WITH_PROPOSALS]

    calls: list = []
    monkeypatch.setattr(
        self_learning, "record_rule_usage",
        lambda *a, **k: calls.append("rule-bump") or 0)
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda **k: calls.append("mapping") or "m-1")
    monkeypatch.setattr(self_learning, "known_systems", lambda: _CATALOG)
    for check in ("_check_for_discoveries", "_check_for_reason_code_mappings",
                  "_persist_column_not_found_guidance"):
        monkeypatch.setattr(
            runtime_app, check,
            lambda *a, _c=check, **k: calls.append(_c))

    runtime_app._integrity_post_turn_learning(
        invocation, "Generate the operations briefing", "sess-1")

    assert calls == []  # every hook honors the briefing gate


def test_hook_exception_is_swallowed_by_each_capture(enforce_mode, monkeypatch):
    """Each capture hook swallows its own failure — none propagates."""
    invocation = _invocation_with_retained_evidence()
    _run_final_events(monkeypatch, invocation)
    invocation.response_text = [_RESPONSE_WITH_PROPOSALS]

    def _boom(**_kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(self_learning, "log_proposed_mapping", _boom)
    monkeypatch.setattr(
        self_learning, "record_rule_usage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ddb down")))
    monkeypatch.setattr(self_learning, "known_systems", lambda: _CATALOG)
    monkeypatch.setattr(runtime_app, "_post_turn_learning_checks", lambda s: None)

    # Must not raise despite every backend write failing.
    runtime_app._integrity_post_turn_learning(invocation, "compare ids", "sess-1")


# --------------------------------------------------------------------------- #
# End-to-end: the integrity invocation wires the seam after final_events.
# --------------------------------------------------------------------------- #
class _FakeAgent:
    def __init__(self, events):
        self._events = events

    async def stream_async(self, _prompt, **_kwargs):
        for event in self._events:
            yield event


def _drive_invocation(monkeypatch, *, prompt=RAW_PROMPT, response=None,
                      catalog=None) -> list[dict]:
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    # The prelude seam tolerates the historical 2-tuple shape — and augments
    # the prompt so R2 can prove the ORIGINAL text is what learning sees.
    monkeypatch.setattr(
        runtime_app, "_graph_first_prelude",
        lambda p: (f"[ROUTE SEED — resolved]\npayload\n\n---\n\n{p}", []))
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    monkeypatch.setattr(self_learning, "known_systems", lambda: catalog or [])
    for check in ("_check_for_discoveries", "_check_for_reason_code_mappings",
                  "_persist_column_not_found_guidance"):
        monkeypatch.setattr(runtime_app, check, lambda *a, **k: None)
    events = [{"data": response or "plain answer"}, {"result": None}]
    payload = {"prompt": prompt, "sessionId": "sess-e2e"}

    async def _collect():
        generator = runtime_app._invoke_integrity_runtime(
            payload, None, lambda **_kw: _FakeAgent(events))
        return [event async for event in generator]

    return asyncio.run(_collect())


def test_insight_and_vm_capture_fire_on_integrity_path(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda **kwargs: writes.append(kwargs) or f"m-{len(writes)}")

    output = _drive_invocation(
        monkeypatch, response=_RESPONSE_WITH_PROPOSALS, catalog=_CATALOG)

    assert any("intelligence_summary" in event for event in output)
    assert not any("error" in event for event in output)
    kinds = {write["proposed_type"] for write in writes}
    assert kinds == {"insight", "value_mapping"}
    insight = next(w for w in writes if w["proposed_type"] == "insight")
    assert insight["from_system"] == "cmms"
    assert insight["to_system"] == "erp"
    vm = next(w for w in writes if w["proposed_type"] == "value_mapping")
    # R2: the stored query_context is the ORIGINAL user prompt, never the
    # seed-augmented turn text; concept comes from the block, not a default.
    assert vm["query_context"] == RAW_PROMPT
    assert "ROUTE SEED" not in vm["query_context"]
    assert vm["concept"] == "equipment-reference"


def test_learning_failure_never_breaks_stream(monkeypatch):
    monkeypatch.setattr(
        runtime_app, "_integrity_post_turn_learning",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("learning down")))

    output = _drive_invocation(monkeypatch)

    assert not any("error" in event for event in output)
    assert any("data" in event for event in output)
    assert any("intelligence_summary" in event for event in output)
    assert any("response_integrity" in event for event in output)


def test_post_turn_learning_runs_after_final_events(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        runtime_app, "_integrity_post_turn_learning",
        lambda *a, **k: order.append("learning"))

    output = _drive_invocation(monkeypatch)

    assert any("intelligence_summary" in event for event in output)
    assert order == ["learning"]


# --------------------------------------------------------------------------- #
# R2 — concept parsed-or-unknown; original prompt as query_context.
# --------------------------------------------------------------------------- #
def _vm_block(*extra_lines: str) -> str:
    return "\n".join((
        "from_system: sys-a",
        "from_field: records.external_id",
        "from_value: WO-1001",
        "to_system: sys-b",
        "to_field: entities.canonical_id",
        "to_value: 4400001001",
        *extra_lines,
    ))


def test_concept_parsed_from_block():
    writes: list[dict] = []
    accepted = runtime_app._ingest_value_mapping_proposals(
        [_vm_block("concept: work-order")],
        prompt="original question",
        tool_calls_count=1,
        log_mapping=lambda **kwargs: writes.append(kwargs) or "m-1",
    )
    assert accepted == 1
    assert writes[0]["concept"] == "work-order"


def test_concept_unknown_when_absent():
    writes: list[dict] = []
    runtime_app._ingest_value_mapping_proposals(
        [_vm_block()],
        prompt="original question",
        tool_calls_count=1,
        log_mapping=lambda **kwargs: writes.append(kwargs) or "m-1",
    )
    assert writes[0]["concept"] == "unknown"
    # The audited defect: a hardcoded demo-flavored default.
    assert writes[0]["concept"] != "equipment-id"


def test_query_context_is_original_prompt_not_seed(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda **kwargs: writes.append(kwargs) or "m-1")
    original = "which supplier made lot 42?"
    s = SimpleNamespace(
        prompt=f"[ROUTE SEED — resolved]\npayload\n\n---\n\n{original}",
        raw_prompt=original,
        tool_calls_log=[],
        response_text=[
            "---VALUE-MAPPING-PROPOSAL---\n" + _vm_block() + "\n---END-PROPOSAL---",
        ],
    )
    runtime_app._capture_value_mapping_proposals(s)
    assert writes[0]["query_context"] == original
    assert "ROUTE SEED" not in writes[0]["query_context"]


def test_new_turn_state_carries_raw_prompt(monkeypatch):
    monkeypatch.setattr(
        runtime_app, "_graph_first_prelude",
        lambda p: (f"SEED\n{p}", []))
    state = runtime_app._new_turn_state(
        {"prompt": "plain question", "sessionId": "sess-1"}, None)
    assert state.raw_prompt == "plain question"
    assert state.prompt == "SEED\nplain question"


# --------------------------------------------------------------------------- #
# persist_typed_proposals reports only rows actually persisted.
# --------------------------------------------------------------------------- #
def test_persist_typed_proposals_counts_only_persisted(monkeypatch):
    """A gate-dropped proposal (``log_proposed_mapping`` → ``None``) must not
    inflate the persisted count (no staged/None over-counting)."""
    import agents.runtime_integrity as runtime_integrity

    results = iter(["vm-1", None, "vm-2"])
    monkeypatch.setattr(runtime_integrity, "_persist_one",
                        lambda proposal, tool_calls_count: next(results))
    monkeypatch.setattr(runtime_integrity, "_actionable_proposal",
                        lambda proposal: True)
    answer = SimpleNamespace(learning_allowed=True,
                             proposals=[object(), object(), object()])
    assert runtime_integrity.persist_typed_proposals(
        answer, tool_calls_count=1) == 2
