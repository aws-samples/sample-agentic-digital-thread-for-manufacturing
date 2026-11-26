# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3.1 T-B — answer landing: the proposal lifecycle and the injection surface.

Interview answers are untrusted free text headed for an LLM prompt, so this suite
imports the SHARED ``UNTRUSTED_TEXT_BATTERY`` (T1.7) rather than inventing
fixtures — the whole point of that battery is that a new surface cannot quietly
become a new injection channel.
"""
import pytest

from agents.context_interview import landing as L
from agents.context_interview import questions as qb
from tests.unit.test_untrusted_text import UNTRUSTED_TEXT_BATTERY


def _q(qid: str):
    return qb.QUESTIONS_BY_ID[qid]


@pytest.fixture
def captured(monkeypatch):
    """Capture log_proposed_mapping calls; assert nothing else writes."""
    calls = []

    def fake_lpm(**kw):
        calls.append(kw)
        return f"map-{len(calls)}"

    monkeypatch.setattr("agents.self_learning.log_proposed_mapping", fake_lpm)
    return calls


# --------------------------------------------------------------------------- #
# Injection surface — the shared battery
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,reason",
    [pytest.param(raw, reason, id=name)
     for name, raw, reason in UNTRUSTED_TEXT_BATTERY if reason is not None])
def test_hostile_answers_are_rejected_with_pinned_reasons(raw, reason, captured):
    res = L.land_answer(_q("failure-taxonomy"), raw, system_id="sys-1")
    assert res.outcome == L.OUTCOME_REJECTED
    assert res.reason == reason
    assert captured == [], "a rejected answer must not reach the lifecycle"


@pytest.mark.parametrize(
    "raw",
    [pytest.param(raw, id=name)
     for name, raw, reason in UNTRUSTED_TEXT_BATTERY
     if reason is None and isinstance(raw, str) and raw.strip()])
def test_survivable_answers_land_sanitized(raw, captured):
    """Instruction-override / tool-shaped text SURVIVES sanitation by design —
    it is inert DATA once fenced. What must hold is that it lands sanitized
    (no control chars, no CR) and only via a lifecycle-or-draft outcome."""
    # failure-taxonomy drafts into business context; query-strategy proposes.
    for qid in ("failure-taxonomy", "query-strategy"):
        res = L.land_answer(_q(qid), raw, system_id="sys-1")
        assert res.outcome in (L.OUTCOME_PROPOSED, L.OUTCOME_DRAFT,
                               L.OUTCOME_SKIPPED)
        landed = res.draft_text or (captured[-1]["description"] if captured else "")
        assert "\x00" not in landed and "\r" not in landed


def test_rejection_detail_never_echoes_the_answer(captured):
    secret = "password: super-secret-value"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    res = L.land_answer(_q("failure-taxonomy"), secret, system_id="s")
    assert res.outcome == L.OUTCOME_REJECTED
    assert "super-secret-value" not in res.detail


# --------------------------------------------------------------------------- #
# One lifecycle — no side-door writes
# --------------------------------------------------------------------------- #
def test_proposals_route_through_log_proposed_mapping(captured):
    res = L.land_answer(_q("query-strategy"), "prefer aggregates", system_id="sys-1")
    assert res.outcome == L.OUTCOME_PROPOSED
    assert res.proposal_ids == ["map-1"]
    assert len(captured) == 1
    assert captured[0]["query_context"] == "context-interview:query-strategy"
    assert captured[0]["provenance"].startswith("interview:")


def test_answers_are_never_pre_accepted(captured):
    """Interview answers are human judgment about semantics — the class ADR-0050
    keeps OFF the auto-accept path."""
    L.land_answer(_q("query-strategy"), "prefer aggregates", system_id="s")
    assert "pre_accepted" not in captured[0]
    assert captured[0]["source"] == "steward"


def test_landing_maps_to_the_expected_proposed_types(captured):
    L.land_answer(_q("identifier-family"), "shaped <A>_<B>", system_id="s")
    L.land_answer(_q("coverage-boundary"), "no coverage at some sites", system_id="s")
    types = [c["proposed_type"] for c in captured]
    assert types == ["value_mapping", "guidance_note"]


def test_gated_or_staged_proposal_is_reported_honestly(monkeypatch):
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: None)
    res = L.land_answer(_q("query-strategy"), "prefer aggregates", system_id="s")
    assert res.outcome == L.OUTCOME_SKIPPED
    assert res.reason == "not_ingested"


# --------------------------------------------------------------------------- #
# Business context is a DRAFT, never a write
# --------------------------------------------------------------------------- #
def test_business_context_answers_are_held_as_draft(captured):
    res = L.land_answer(_q("retry-semantics"), "retries close the old record",
                        system_id="sys-1")
    assert res.outcome == L.OUTCOME_DRAFT
    assert res.draft_text == "retries close the old record"
    assert captured == [], "BC must not enter the proposal path"


def test_draft_merge_is_bounded_and_drops_whole_additions():
    long_piece = "y" * 1500
    out = L.merge_draft_context("start", [long_piece, "short tail"], max_len=100)
    assert long_piece not in out
    assert out.endswith("short tail")
    assert len(out) <= 100


def test_draft_merge_normalises_whitespace():
    assert L.merge_draft_context("", ["a\n\n  b\tc"]) == "a b c"


# --------------------------------------------------------------------------- #
# Authority — refuse to create the nondeterministic two-rule state
# --------------------------------------------------------------------------- #
def test_authority_conflict_blocks_instead_of_writing(monkeypatch, captured):
    monkeypatch.setattr(L, "_active_authority_system", lambda cid: "other-sys")
    res = L.land_answer(_q("authority-split"), "this system is authoritative",
                        system_id="sys-1", concept_id="machine-state")
    assert res.outcome == L.OUTCOME_CONFLICT
    assert res.conflict_with == "other-sys"
    assert captured == [], "a conflicting authority answer must not be landed"


def test_authority_same_system_is_not_a_conflict(monkeypatch, captured):
    monkeypatch.setattr(L, "_active_authority_system", lambda cid: "sys-1")
    res = L.land_answer(_q("authority-split"), "still authoritative",
                        system_id="sys-1", concept_id="machine-state")
    assert res.outcome == L.OUTCOME_PROPOSED


def test_authority_with_no_existing_rule_lands(monkeypatch, captured):
    monkeypatch.setattr(L, "_active_authority_system", lambda cid: "")
    res = L.land_answer(_q("authority-split"), "make this one authoritative",
                        system_id="sys-1", concept_id="machine-state")
    assert res.outcome == L.OUTCOME_PROPOSED


def test_graph_unavailable_asserts_no_conflict(monkeypatch):
    """Absence of evidence is not evidence of conflict — but it must also never
    raise into the interview."""
    def boom(*a, **k):
        raise RuntimeError("graph down")
    monkeypatch.setattr("agents.db.neptune_query", boom, raising=False)
    assert L._active_authority_system("machine-state") == ""


def test_ambiguous_existing_rules_do_not_claim_a_single_owner(monkeypatch):
    monkeypatch.setattr("agents.db.neptune_query",
                        lambda q, p=None: [{"system_id": "a"}, {"system_id": "b"}],
                        raising=False)
    assert L._active_authority_system("machine-state") == ""


# --------------------------------------------------------------------------- #
# Skips
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_answer_is_a_clean_skip(blank, captured):
    res = L.land_answer(_q("query-strategy"), blank, system_id="s")
    assert res.outcome == L.OUTCOME_SKIPPED
    assert captured == []


def test_non_string_answer_is_rejected_not_crashed(captured):
    res = L.land_answer(_q("query-strategy"), 42, system_id="s")
    assert res.outcome == L.OUTCOME_REJECTED
    assert res.reason == "invalid_input"
