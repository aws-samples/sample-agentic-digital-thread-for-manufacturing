# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0059 Task 1C — runtime steward-question templates + landing pass-through.

Two contracts pinned here:

1. **Composition is pure, deterministic, and total.** ``runtime_question`` is
   called by the failure-signal sweep with rows built from live query traffic —
   it must never raise, never involve an LLM, and always yield the same
   Question for the same (shape, subject).
2. **Landing is the EXISTING machinery, unchanged.** A runtime answer routes
   through ``landing.land_answer`` exactly like an onboarding answer — same
   sanitation, same proposal lifecycle, no new surface (ADR-0059 D4). The
   regression proof for "zero changes to existing landings" is that
   ``test_interview_landing.py`` runs untouched.
"""
import pytest

from agents.context_interview import landing as L
from agents.context_interview import questions as qb

_SUBJECTS = {
    "terminology_miss": {"term": "the finishing step", "system": "system-a"},
    "empty_join": {"system": "system-a", "other_system": "system-b",
                   "field": "resource_id"},
    "unit_confusion": {"concept": "throughput"},
}


# --------------------------------------------------------------------------- #
# Template inventory and shape
# --------------------------------------------------------------------------- #
def test_one_template_per_failure_shape():
    """The taxonomy is Task 1B's THRESHOLDS keys — one question wording each."""
    assert set(qb.RUNTIME_QUESTION_TEMPLATES) == {
        "terminology_miss", "empty_join", "unit_confusion"}


def test_templates_are_runtime_scoped_query_time():
    for shape, q in qb.RUNTIME_QUESTION_TEMPLATES.items():
        assert q.scope == qb.SCOPE_RUNTIME, shape
        assert q.treatment_phase == qb.PHASE_QUERY_TIME, shape


def test_templates_land_on_existing_proposal_surfaces():
    """No new landing surface: every template must map into land_answer's
    existing proposal routing (never business-context — a runtime answer must
    not accumulate into the BC draft flow, which is an onboarding batch)."""
    for shape, q in qb.RUNTIME_QUESTION_TEMPLATES.items():
        assert q.landing in L._PROPOSED_TYPE_BY_LANDING, shape


def test_templates_carry_goal_and_placeholder_examples():
    for shape, q in qb.RUNTIME_QUESTION_TEMPLATES.items():
        assert q.goal, shape
        assert q.examples, shape


def test_runtime_templates_never_enter_the_onboarding_bank():
    """QUESTIONS drives onboarding selection; runtime questions are composed on
    demand. A runtime template leaking into the bank would consume interview
    cap slots for failures that have not happened."""
    assert all(q.scope != qb.SCOPE_RUNTIME for q in qb.QUESTIONS)
    bank_ids = {q.id for q in qb.QUESTIONS}
    assert not bank_ids & {q.id for q in qb.RUNTIME_QUESTION_TEMPLATES.values()}


def test_every_template_placeholder_has_a_fallback():
    """format_map must never KeyError: each {placeholder} in a template must be
    a key the composer always supplies."""
    import string
    fmt = string.Formatter()
    for shape, q in qb.RUNTIME_QUESTION_TEMPLATES.items():
        fields = {name for _, name, _, _ in fmt.parse(q.prompt) if name}
        assert fields <= set(qb._RUNTIME_FACT_FALLBACKS), (shape, fields)


# --------------------------------------------------------------------------- #
# The composer — pure, deterministic, total
# --------------------------------------------------------------------------- #
def test_composer_is_deterministic():
    for shape, subject in _SUBJECTS.items():
        assert qb.runtime_question(shape, subject) == \
            qb.runtime_question(shape, dict(subject))


def test_composer_interpolates_subject_facts():
    q = qb.runtime_question("empty_join", _SUBJECTS["empty_join"])
    assert "system-a" in q.prompt and "system-b" in q.prompt
    assert "resource_id" in q.prompt
    assert "{" not in q.prompt, "no unfilled placeholders may remain"


def test_composer_id_is_stable_and_subject_distinct():
    a1 = qb.runtime_question("terminology_miss", _SUBJECTS["terminology_miss"])
    a2 = qb.runtime_question("terminology_miss", _SUBJECTS["terminology_miss"])
    b = qb.runtime_question("terminology_miss", {"term": "another term",
                                                 "system": "system-a"})
    assert a1.id == a2.id
    assert a1.id != b.id
    assert a1.id.startswith("runtime-terminology-miss-")


def test_irrelevant_subject_keys_do_not_change_the_question():
    """The id digests the composed TEXT: a fact the template never uses must
    not fork the identity (one shape-subject ⇒ one pending question)."""
    base = qb.runtime_question("unit_confusion", {"concept": "throughput"})
    noisy = qb.runtime_question("unit_confusion", {"concept": "throughput",
                                                   "field": "unused"})
    assert base == noisy


def test_missing_facts_fall_back_neutrally():
    q = qb.runtime_question("terminology_miss", {})
    assert "that term" in q.prompt
    assert "this system" in q.prompt
    assert "{" not in q.prompt


@pytest.mark.parametrize("subject", [None, "a string", 42, ["list"],
                                     {"term": 42, "system": None},
                                     {"term": "", "system": "   "}])
def test_garbage_subjects_never_raise(subject):
    for shape in qb.RUNTIME_QUESTION_TEMPLATES:
        q = qb.runtime_question(shape, subject)
        assert q is not None
        assert "{" not in q.prompt


def test_unknown_shape_yields_none_not_an_exception():
    assert qb.runtime_question("not_a_shape", {}) is None


def test_subject_facts_are_length_bounded():
    long_term = "x" * 500
    q = qb.runtime_question("terminology_miss", {"term": long_term})
    assert long_term not in q.prompt
    assert "x" * qb.RUNTIME_FACT_MAX_LEN in q.prompt


def test_subject_whitespace_is_normalized():
    q = qb.runtime_question("terminology_miss",
                            {"term": "odd\n\t  spacing", "system": "sys"})
    assert "odd spacing" in q.prompt


def test_template_objects_are_never_mutated():
    """replace() must copy — a composer that mutated the shared template would
    bake one deployment's facts into every later question."""
    before = qb.RUNTIME_QUESTION_TEMPLATES["empty_join"].prompt
    qb.runtime_question("empty_join", _SUBJECTS["empty_join"])
    assert qb.RUNTIME_QUESTION_TEMPLATES["empty_join"].prompt == before
    assert "{system}" in before


# --------------------------------------------------------------------------- #
# Landing — the EXISTING land_answer machinery, zero changes
# --------------------------------------------------------------------------- #
@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_lpm(**kw):
        calls.append(kw)
        return f"map-{len(calls)}"

    monkeypatch.setattr("agents.self_learning.log_proposed_mapping", fake_lpm)
    return calls


def test_runtime_answers_land_as_pending_proposals(captured):
    q = qb.runtime_question("empty_join", _SUBJECTS["empty_join"])
    res = L.land_answer(q, "values match after removing a site prefix",
                        system_id="system-a")
    assert res.outcome == L.OUTCOME_PROPOSED
    assert res.proposal_ids == ["map-1"]
    assert captured[0]["proposed_type"] == "value_mapping"
    assert captured[0]["source"] == "steward"
    assert captured[0]["query_context"] == f"context-interview:{q.id}"
    assert captured[0]["query_context"].startswith(
        "context-interview:runtime-empty-join-")


def test_unit_confusion_answers_land_as_guidance_notes(captured):
    q = qb.runtime_question("unit_confusion", _SUBJECTS["unit_confusion"])
    res = L.land_answer(q, "recorded per hour, not per item", system_id="sys")
    assert res.outcome == L.OUTCOME_PROPOSED
    assert captured[0]["proposed_type"] == "guidance_note"


def test_runtime_answers_are_never_pre_accepted(captured):
    q = qb.runtime_question("terminology_miss", _SUBJECTS["terminology_miss"])
    L.land_answer(q, "it maps to a different identifier", system_id="sys")
    assert "pre_accepted" not in captured[0]


def test_hostile_runtime_answers_are_rejected_before_the_lifecycle(captured):
    """Secret-shaped text is the write-time rejection class (ADR-0054); the
    full hostile battery is exercised against land_answer in
    test_interview_landing.py — one probe here proves runtime questions ride
    the same gate."""
    q = qb.runtime_question("terminology_miss", _SUBJECTS["terminology_miss"])
    res = L.land_answer(q, "password: hunter2please", system_id="sys")  # nosec B106 - offline test fixture, not a credential
    assert res.outcome == L.OUTCOME_REJECTED
    assert res.reason == "secret_like_content"
    assert captured == []


def test_blank_runtime_answer_is_a_clean_skip(captured):
    q = qb.runtime_question("empty_join", _SUBJECTS["empty_join"])
    res = L.land_answer(q, "   ", system_id="sys")
    assert res.outcome == L.OUTCOME_SKIPPED
    assert captured == []


def test_runtime_answers_respect_the_answer_length_bound(captured):
    q = qb.runtime_question("unit_confusion", _SUBJECTS["unit_confusion"])
    res = L.land_answer(q, "y" * (qb.ANSWER_MAX_LEN + 1), system_id="sys")
    assert res.outcome == L.OUTCOME_REJECTED
    assert captured == []
