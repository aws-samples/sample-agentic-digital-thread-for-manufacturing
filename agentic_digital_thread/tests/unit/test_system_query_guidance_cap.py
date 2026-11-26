# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The system agent's per-question guidance fits the delivery cap.

Every rendered guidance section must fit ``DELIVERY_FIELD_CAP`` on its way
into a prompt (``guidance_scope``). The route seed fits its sections through
``fit_delivery_sections``; the system agent's per-question block
(``system_agent._augment_question_with_guidance``) rendered the same document
uncapped, so one long note, or several shorter ones, put a section well past
the cap into every question.

An onboarding interview answer may hold ``ANSWER_MAX_LEN`` characters and is
filed as a guidance note, so these tests take that note through the real
landing into an in-memory proposal table, deliver it as an accepted query
hint, and read what the system agent appends to a question. The text is
unlike the seed.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

import agents.system_agent as sa
import agents.system_guidance as sg
from agents import guidance_scope as gs
from agents.context_interview import landing
from agents.context_interview.questions import ANSWER_MAX_LEN, QUESTIONS
from tests.unit.test_vocab_lifecycle import FakeTable, _install

SYSTEM = "works-c-upkeep"
_SENTENCE = ("Readings from the second press line arrive in tenths of a bar "
             "and must be divided by ten before any comparison. ")
_FENCE_RE = re.compile(r"----- BEGIN (?P<title>[A-Z -]+?) -----\n(?P<body>.*?)\n"
                       r"----- END (?P=title) -----", re.DOTALL)


def _long_text(length: int, salt: str = "") -> str:
    text = (salt + _SENTENCE * (length // len(_SENTENCE) + 1))[:length]
    return text[:-1] + "." if text.endswith(" ") else text


def _filed_interview_note(monkeypatch) -> str:
    """The ``to_value`` the interview landing files for a maximum-length answer."""
    table = FakeTable()
    _install(monkeypatch, table)
    question = next(q for q in QUESTIONS if landing._PROPOSED_TYPE_BY_LANDING.get(
        q.landing) == "guidance_note")
    res = landing.land_answer(question, _long_text(ANSWER_MAX_LEN), system_id=SYSTEM)
    note = table.by_id(res.proposal_ids[0])["to_value"]
    assert len(note) == ANSWER_MAX_LEN
    return note


def _deliver(monkeypatch, document: str) -> str:
    """What the system agent appends to one question for ``document``."""
    delivered = gs.delivery_document(document)
    monkeypatch.setattr(sg, "read", lambda _system_id: delivered)
    question = "Which press line ran hottest?"
    augmented = sa._augment_question_with_guidance(SYSTEM, question)
    assert augmented.startswith(question)
    return augmented[len(question):]


def _fences(block: str) -> list[re.Match]:
    fences = list(_FENCE_RE.finditer(block))
    assert block.count("----- BEGIN ") == len(fences)
    assert block.count("----- END ") == len(fences)
    return fences


def _assert_fits(block: str) -> list[re.Match]:
    fences = _fences(block)
    assert fences, "no guidance section was delivered"
    for fence in fences:
        assert len(fence.group(0)) <= gs.DELIVERY_FIELD_CAP, fence.group("title")
        json.loads(fence.group("body").split("\n", 1)[1])
    return fences


def _truncation_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("GUIDANCE_DELIVERY_TRUNCATION")]


def test_a_maximum_length_interview_note_fits_the_cap_and_is_reported(
        monkeypatch, caplog):
    note = _filed_interview_note(monkeypatch)
    document = gs.append_note(gs.parse_document(""), note, gs.GuidanceScope.QUERY_HINT)

    with caplog.at_level(logging.WARNING, logger=sa.logger.name):
        block = _deliver(monkeypatch, document)

    (fence,) = _assert_fits(block)
    assert fence.group("title") == "QUERY HINTS"
    assert note[:200] in fence.group(0)
    assert gs.TRUNCATION_MARKER in fence.group(0)
    (line,) = _truncation_lines(caplog)
    assert f"system_id={SYSTEM}" in line
    assert "fields=query_guidance" in line
    assert "notes_total=1" in line and "notes_truncated=1" in line
    assert "action=report-only" in line
    assert note[:40] not in line


def test_several_notes_past_the_cap_keep_the_newest_and_report_the_drop(
        monkeypatch, caplog):
    document = gs.parse_document("")
    hints = [_long_text(400, salt=f"Hint {i}. ") for i in range(4)]
    for hint in hints:
        document = gs.append_note(document, hint, gs.GuidanceScope.QUERY_HINT)
    legacy = _long_text(1500, salt="Legacy. ")
    document = gs.append_note(document, legacy, gs.GuidanceScope.LEGACY_UNSCOPED)

    with caplog.at_level(logging.WARNING, logger=sa.logger.name):
        block = _deliver(monkeypatch, document)

    titles = [fence.group("title") for fence in _assert_fits(block)]
    assert titles == ["QUERY HINTS", "LEGACY UNSCOPED GUIDANCE"]
    assert hints[-1] in block and hints[0] not in block
    dropped = sum(hint not in block for hint in hints)
    (line,) = _truncation_lines(caplog)
    assert "fields=query_guidance,legacy_guidance" in line
    assert "notes_total=5" in line
    assert f"notes_dropped={dropped}" in line and "notes_truncated=1" in line


def test_withheld_answer_requirements_are_not_reported_as_cut(monkeypatch, caplog):
    document = gs.append_note(gs.parse_document(""), "Aggregate by shift first.",
                              gs.GuidanceScope.QUERY_HINT)
    document = gs.append_note(document, _long_text(ANSWER_MAX_LEN),
                              gs.GuidanceScope.ANSWER_REQUIREMENT)

    with caplog.at_level(logging.WARNING, logger=sa.logger.name):
        block = _deliver(monkeypatch, document)

    assert [f.group("title") for f in _assert_fits(block)] == ["QUERY HINTS"]
    assert "FINAL-ANSWER REQUIREMENTS" not in block
    assert _truncation_lines(caplog) == []


@pytest.mark.parametrize("scope", [gs.GuidanceScope.QUERY_HINT,
                                   gs.GuidanceScope.LEGACY_UNSCOPED])
def test_guidance_under_the_cap_is_delivered_unchanged(monkeypatch, caplog, scope):
    document = gs.append_note(gs.parse_document(""), "Aggregate by shift first.", scope)
    expected = gs.render_orchestrator_sections(gs.delivery_document(document))

    with caplog.at_level(logging.WARNING, logger=sa.logger.name):
        block = _deliver(monkeypatch, document)

    field = "query_guidance" if scope is gs.GuidanceScope.QUERY_HINT else "legacy_guidance"
    assert block == f"\n\n{expected[field]}"
    assert _truncation_lines(caplog) == []
