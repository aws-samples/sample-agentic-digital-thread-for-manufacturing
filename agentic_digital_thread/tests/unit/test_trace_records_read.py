# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A traced call records how many records its tool parsed from the source.

The onboarding agent test proves an API source's read from its own call's
investigation trace (``entity_reads``). A step's status says only that the
tool returned: a tool that caught a source error returns it as text, with a
success status. What the tool itself parsed out of a successful response
(``tool_result_capture.report_read_count``, never a count field of the
source's JSON) is the proof, so the trace step now records it as
``records_read`` for a kept call of a tool that declares a read.

Only Tulip reads are proven this way (SiteWise Direct and i3X names
do not tell a connector-fixed table from a source-named one, so their reads
are never proven from the trace: ``entity_reads.EntityReads.names_tell_origin``).
The end-to-end cases below run their mapped tools from the source's answer
to the trace proof.

The answer-evidence count (``_successful_api_reads``, data reads only) is
unchanged. Names are made up. Offline.
"""
from __future__ import annotations

import pytest

import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.system_agent as sa
from agents.job_store import JobStore
from tests.unit._shared_table import SharedTable
from tests.unit.test_api_read_counts import _call, _hook, _reported


def _steps(state: dict) -> list:
    return it.collect_trace_steps(state)[0]


# ── the capture hook ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, kind_counted", [("list_things", False), ("read_values", True)])
def test_a_kept_call_of_a_declared_read_records_what_its_tool_parsed(name, kind_counted):
    """Discovery and data reads alike; only a data read is answer evidence,
    as before."""
    state: dict = {}
    _call(_hook(), state, name, report=4)
    (step,) = _steps(state)
    assert step["records_read"] == 4 and step["status"] == "success"
    assert sa._successful_api_reads(state) == (1 if kind_counted else 0)


def test_a_reported_zero_is_recorded_as_zero():
    state: dict = {}
    _call(_hook(), state, "list_things", report=0)
    assert _steps(state)[0]["records_read"] == 0


@pytest.mark.parametrize("kwargs", [
    {"name": "read_values"},                                         # no report
    {"name": "read_values", "report": 3, "status": "error"},
    {"name": "read_values", "report": 3, "exception": RuntimeError("down")},
    {"name": "other_tool", "report": 3},                             # declares no read
], ids=["no-report", "error-result", "exception", "undeclared"])
def test_no_count_is_recorded_without_a_kept_call_of_a_declared_read(kwargs):
    state: dict = {}
    _call(_hook(), state, **kwargs)
    (step,) = _steps(state)
    assert "records_read" not in step


def test_a_retried_attempt_records_no_step():
    state: dict = {}
    _call(_hook(), state, "list_things", report=3, retry=True)
    assert _steps(state) == []


@pytest.mark.parametrize("value", [True, -1, "3", 2.5, None])
def test_record_step_keeps_only_a_whole_non_negative_count(value):
    state: dict = {}
    it.record_step(state, tool_use={"toolUseId": "t1", "name": "list_things", "input": {}},
                   result={"status": "success", "content": [{"text": "[]"}]},
                   exception=None, retry=False, messages=[], records_read=value)
    assert "records_read" not in _steps(state)[0]


def test_record_step_drops_a_count_on_an_exception():
    state: dict = {}
    it.record_step(state, tool_use={"toolUseId": "t1", "name": "list_things", "input": {}},
                   result=None, exception=RuntimeError("down"), retry=False, messages=[],
                   records_read=5)
    assert "records_read" not in _steps(state)[0]


# ── the store ────────────────────────────────────────────────────────────────

SID_A, REF_A = "verify-agent-test-src-1", "q-0123456789ab"


def _entry(*steps) -> dict:
    return {"system_id": "src", "question": "q", "final_answer": "a",
            "steps": [{"seq": i + 1, "tool_name": "list_things", "tool_input": "{}",
                       "status": "success", **extra} for i, extra in enumerate(steps)]}


def test_the_store_keeps_a_valid_count_and_drops_any_other(monkeypatch):
    monkeypatch.setattr(its, "_job_store", JobStore(kind="investigation-trace", table=None))
    assert its.put_trace(SID_A, REF_A, _entry({"records_read": 7}, {"records_read": True},
                                              {"records_read": -2}, {"records_read": "7"},
                                              {}))
    steps = its.get_trace(SID_A, REF_A)["steps"]
    assert [s.get("records_read") for s in steps] == [7, None, None, None, None]


def test_a_consistent_read_sees_a_trace_another_process_just_wrote(monkeypatch):
    """The agent test reads back the trace the system's runtime wrote a
    moment before it answered. The plain read is eventually consistent and
    can miss that write; the consistent read cannot."""
    table = SharedTable()
    table.hold_back()
    monkeypatch.setattr(its, "_job_store", JobStore(kind="investigation-trace", table=table))
    assert its.put_trace(SID_A, REF_A, _entry({"records_read": 1}))
    monkeypatch.setattr(its, "_job_store", JobStore(kind="investigation-trace", table=table))
    assert its.get_trace(SID_A, REF_A) is None
    assert its.get_trace(SID_A, REF_A, consistent=True)["steps"][0]["records_read"] == 1
    table.fail_reads = True
    assert its.get_trace(SID_A, REF_A, consistent=True) is None


@pytest.mark.parametrize("body, count", [({"message": "not a record list"}, None),
                                         ("nope", None), ([], 0),
                                         ([{"id": "r1"}], 1)],
                         ids=["dict", "text", "empty", "one"])
def test_a_tulip_record_read_reports_a_count_only_for_a_record_list(body, count,
                                                                    monkeypatch):
    monkeypatch.setattr(sa._TulipClient, "get", lambda self, path, params=None: body)
    tools = {t.tool_name: t for t in sa._make_tulip_tools("https://tulip.invalid",
                                                          token="unused")}
    assert _reported(tools["tulip_query_records"], {"table_id": "kb7Q2"}) == count


# ── from the tool's output to the trace proof (end to end) ───────────────────
# A malformed or error answer must never become a count, zero included, since
# a zero proves the read (an empty entity). Each case runs the real tool
# through the real capture hook, then reads its trace step with the agent
# test's own check (``entity_reads.traced_reads`` / ``proves_read``).

import agents.entity_reads as er  # noqa: E402
import agents.tool_result_capture as trc  # noqa: E402
from tests.unit._api_reads import run_tool  # noqa: E402


def _proves(tools: dict, name: str, kwargs: dict, rules, entity: str,
            entity_id: str = "") -> bool:
    """Whether one real call of ``name`` proves a read of ``entity`` from its
    trace step, as the agent test reads it."""
    hook = trc.InvocationToolResultCapture(
        system_id="foundry", read_kinds=trc.declared_read_kinds(list(tools.values())))
    state: dict = {}
    run_tool(hook, state, tools[name], kwargs)
    rule = rules.rule(entity)
    return er.proves_read(er.traced_reads(_steps(state), rule), rule, entity_id)


@pytest.mark.parametrize("body, proven", [
    ([], True), ([{"id": "r1", "qty": 3}], True), ({"records": []}, True),
    ({"message": "not a record list"}, False), ("upstream failed", False),
    ({"records": "upstream failed"}, False),
], ids=["empty", "one", "wrapped-empty", "dict", "text", "wrapped-text"])
def test_a_tulip_record_read_proves_the_read_only_for_a_record_list(body, proven,
                                                                     monkeypatch):
    monkeypatch.setattr(sa._TulipClient, "get", lambda self, path, params=None: body)
    tools = {t.tool_name: t for t in sa._make_tulip_tools("https://tulip.invalid",
                                                          token="unused")}
    assert _proves(tools, "tulip_query_records", {"table_id": "kb7Q2"},
                   sa.TULIP_ENTITY_READS, "Kiln Batches", "kb7Q2") is proven


def test_a_tulip_record_read_that_raised_proves_nothing(monkeypatch):
    def _boom(self, path, params=None):
        raise RuntimeError("HTTP 503")
    monkeypatch.setattr(sa._TulipClient, "get", _boom)
    tools = {t.tool_name: t for t in sa._make_tulip_tools("https://tulip.invalid",
                                                          token="unused")}
    assert not _proves(tools, "tulip_query_records", {"table_id": "kb7Q2"},
                       sa.TULIP_ENTITY_READS, "Kiln Batches", "kb7Q2")


