# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The elicitation retry must not erase the evidence it is asked to summarise.

Measured live (pilot deployment, 2026-09-10): 31% of one system's turns were BLOCKED by
annotate with ``tool_calls=0`` — every one AFTER successful queries. The agent
burned its turn budget, emitted no text, and ``_legacy_mode_a_answer`` ran the
bounded "Produce your final answer NOW from the tool results already in this
conversation" retry. That retry is a SECOND invocation; the capture hook
re-initialises ``invocation_state`` on every ``BeforeInvocationEvent``, so the
model's truthful summary of real rows was verified against an EMPTY state and
withheld as zero-evidence fabrication. Honest answers were being blocked by the
gate meant to stop fabricated ones.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import agents.system_agent as sa
from agents.response_integrity_capture import (
    capture_trusted_tool_result, initialize_integrity_state,
)
from agents.response_integrity_contracts import PRIVATE_SOURCE_STATE_KEY
from agents.tool_result_capture import TOOL_RESULTS_STATE_KEY

QUESTION = "How many records completed at cell-a on 2026-01-05, by colour?"
SUMMARY = ("Completed records at cell-a on 2026-01-05: 1,028,984 monochrome and "
           "199,877 colour (segment grain).")


def _capture_real_rows(state: dict) -> None:
    payload = {"columns": ["colour", "segment_count"],
               "rows": [["MONO", 1028984], ["COLOUR", 199877]], "row_count": 2}
    capture_trusted_tool_result(
        {"toolUseId": "t1", "status": "success",
         "content": [{"text": json.dumps(payload)}]},
        state, system_id="sys-a", boundary="sql", tool_name="run_query",
        executed_query="SELECT colour, segment_count FROM records",
        physical_schema={"records": {"relation_kind": "base_table",
                                     "columns": {"colour": "varchar",
                                                 "segment_count": "integer"}}},
    )
    state[TOOL_RESULTS_STATE_KEY] = [{"tool": "run_query", "app_status": "ok"}]


class _BudgetBurningAgent:
    """Pass 1: queries succeed (captured into state) but no final text.
    Pass 2 (the retry): the framework hook RESETS invocation_state, then the
    model summarises the rows already in the conversation."""

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, invocation_state, limits):
        self.calls += 1
        if self.calls == 1:
            _capture_real_rows(invocation_state)
            return SimpleNamespace(message={"content": [{"text": ""}]})
        # What tool_result_capture._on_invocation_start does on every invocation:
        invocation_state[TOOL_RESULTS_STATE_KEY] = []
        initialize_integrity_state(invocation_state)
        assert "Do not call any tools" in prompt
        return SimpleNamespace(message={"content": [{"text": SUMMARY}]})


def test_retry_summary_of_real_rows_is_not_blocked_by_annotate():
    agent = _BudgetBurningAgent()
    state: dict = {}
    initialize_integrity_state(state, QUESTION)
    answer = sa._legacy_mode_a_answer(agent, QUESTION, "annotate", state)
    assert agent.calls == 2
    assert answer == SUMMARY, answer          # passed through, not withheld
    assert '"status": "withheld"' not in answer
    # The first pass's evidence is the verification basis and survives.
    assert state[PRIVATE_SOURCE_STATE_KEY].observations
    assert state[TOOL_RESULTS_STATE_KEY]


def test_retry_with_genuinely_no_evidence_is_still_blocked():
    """The fix must not weaken the gate: a retry summary asserting figures when
    NO tool ever returned a payload is still zero-evidence fabrication."""

    class _NoDataAgent:
        calls = 0

        def __call__(self, prompt, invocation_state, limits):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(message={"content": [{"text": ""}]})
            invocation_state[TOOL_RESULTS_STATE_KEY] = []
            initialize_integrity_state(invocation_state)
            return SimpleNamespace(message={"content": [{"text": SUMMARY}]})

    state: dict = {}
    initialize_integrity_state(state, QUESTION)
    answer = sa._legacy_mode_a_answer(_NoDataAgent(), QUESTION, "annotate", state)
    assert '"status": "withheld"' in answer
    assert "FABRICATION_BLOCKED" in answer


def test_carry_restore_round_trip_only_touches_capture_keys():
    state = {"other": 1}
    initialize_integrity_state(state, QUESTION)
    _capture_real_rows(state)
    carried = sa._carry_capture_state(state)
    assert set(carried) == {PRIVATE_SOURCE_STATE_KEY, TOOL_RESULTS_STATE_KEY}
    state[TOOL_RESULTS_STATE_KEY] = []
    initialize_integrity_state(state)
    sa._restore_capture_state(state, carried)
    assert state[PRIVATE_SOURCE_STATE_KEY] is carried[PRIVATE_SOURCE_STATE_KEY]
    assert state["other"] == 1
