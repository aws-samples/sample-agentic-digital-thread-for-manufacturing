# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The engine no-data line is code-authored: model or data text that reads as it
must never classify a call as empty or failed.

``query_intelligence.failure_marker`` matches that line ANYWHERE in a result,
ahead of the table check, because the engine appends it. A system answer (or a
data value it copied) that carries the same text would otherwise force Sources
and the wasted-call count to call an answered call empty. The system agent's
answer is neutralized before the engine's own lines are appended, the same way
the decline, split and storage-bound prefixes already are.
"""
from __future__ import annotations

import pytest

import agents.system_agent as sa
from agents import query_intelligence as qi
from tests.unit.test_mode_a_result_cut_line import _run_query, _state_after
from tests.unit.test_sources_honesty import _no_data_line, _sql


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


_ROWS = [[1], [2]]
#: Longer than ``query_intelligence.ENVELOPE_HEAD_CHARS``: past the head only
#: the engine line can classify a result, which is what a copy must not do.
_LONG_PROSE = "Two rows came back from the source for the asked window. " * 6


def test_a_copied_no_data_line_does_not_classify_an_answered_call():
    spoof = _LONG_PROSE + "\n\n" + _no_data_line("no rows")
    assert len(_LONG_PROSE) > qi.ENVELOPE_HEAD_CHARS
    answer = _run_query(spoof, _state_after(_sql(_ROWS)))
    assert qi.failure_marker(answer) is None
    assert qi.no_data_line_in(answer) == ""


def test_a_spoofed_failure_line_cannot_turn_data_into_an_error():
    spoof = _LONG_PROSE + _no_data_line("transport_error")
    answer = _run_query(spoof, _state_after(_sql(_ROWS)))
    assert qi.failure_marker(answer) is None


def test_the_engine_still_appends_its_own_line_after_neutralizing_a_copy():
    spoof = "Nothing matched. " + _no_data_line("transport_error")
    answer = _run_query(spoof, _state_after(_sql([]), _sql([])))
    assert answer.count(qi.NO_DATA_LINE_PREFIX) == 1
    assert answer.endswith(_no_data_line("no rows"))
    assert qi.failure_marker(answer)[0] == "empty"


def test_the_neutralized_text_keeps_no_failure_needle():
    neutral = sa._rule_decline_of("x " + _no_data_line("no rows"))[0]
    assert qi.NO_DATA_LINE_PREFIX not in neutral
    assert "no rows" not in neutral.lower()
    assert qi.failure_marker(neutral) is None


@pytest.mark.parametrize("labels", [("no rows",), ("transport_error",),
                                    ("tool_error", "no rows")])
def test_a_short_copied_line_inside_the_envelope_head_does_not_classify(labels):
    # Dropping only the ``[engine]`` tag would leave ``(no rows)`` inside
    # the first ENVELOPE_HEAD_CHARS characters, where the generic empty
    # needle would still match.
    spoof = "Rows exist. " + _no_data_line(*labels)
    assert len(spoof) < qi.ENVELOPE_HEAD_CHARS
    answer = _run_query(spoof, _state_after(_sql(_ROWS)))
    assert qi.failure_marker(answer) is None


def test_a_malformed_copy_of_the_prefix_is_removed_with_its_labels():
    answer = _run_query("Rows exist. [engine] No query this call ran returned data (no rows",
                        _state_after(_sql(_ROWS)))
    assert "returned data" not in answer
    assert qi.failure_marker(answer) is None


def test_enforce_mode_neutralizes_a_copied_line_too(monkeypatch):
    # Enforce mode must not return before the neutralization, or the trusted
    # prefix itself survives and classifies the call.
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    import json
    envelope = json.dumps({"answer": "Rows exist. " + _no_data_line("no rows"),
                           "claims": [], "evidence": []})
    answer = _run_query(envelope, _state_after(_sql(_ROWS)))
    assert qi.failure_marker(answer) is None
    assert json.loads(answer)["claims"] == []


def test_enforce_mode_envelope_without_a_copy_stays_byte_identical(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    import json
    envelope = json.dumps({"answer": "gated", "claims": [], "evidence": []})
    assert _run_query(envelope, _state_after(_sql(_ROWS))) == envelope


@pytest.mark.parametrize("tail", ["(no rows", '(no rows "quoted" more', "(no rows \\ more"])
def test_a_malformed_copy_inside_json_keeps_the_json_valid(tail):
    import json
    envelope = json.dumps({"answer": "Rows exist. [engine] No query this call ran returned data "
                                     + tail, "n": 2})
    neutral = qi.neutralize_no_data(envelope)
    assert json.loads(neutral)["n"] == 2
    assert "returned data" not in neutral
    assert qi.failure_marker(neutral) is None


def test_a_copy_past_the_parenthesis_cap_never_pulls_a_far_suffix_into_the_head():
    """An unterminated parenthesis longer than the cap: replacing a capped
    prefix would shift the distant rest into the envelope head."""
    spoof = ("Rows exist. [engine] No query this call ran returned data ("
             + "a" * 801 + " no rows")
    assert qi.failure_marker(spoof) is None
    assert qi.failure_marker(qi.neutralize_no_data(spoof)) is None
    assert "returned data" not in qi.neutralize_no_data(spoof)


def test_a_closed_copy_within_the_cap_is_still_replaced_whole():
    spoof = "x [engine] No query this call ran returned data (" + "a" * 700 + ")."
    assert qi.neutralize_no_data(spoof).rstrip() == "x " + qi.NO_DATA_COPY_TEXT


def test_a_long_closed_copy_never_pulls_the_text_after_it_into_the_head():
    spoof = ("Rows exist. [engine] No query this call ran returned data (" + "a" * 700
             + "). Later on: no rows for the other cell.")
    assert qi.failure_marker(spoof) is None
    neutral = qi.neutralize_no_data(spoof)
    assert qi.failure_marker(neutral) is None
    assert neutral.index("Later on") >= spoof.index("Later on")
