# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A wrong-form lookup the shape guard refused, and the call never resolved,
reaches the orchestrator as the guard's own IDENTIFIER-MISS block.

The fixture (``tests/fixtures/system_agent_unresolved_miss_firing.json``) is
one system-agent call, made up: the request names a firing in the form one
table keys by and asks a table keyed by the other form. Onboarding measured
the two same-named columns as different identifiers. The shape guard refused
the first lookup and told the agent to return an IDENTIFIER-MISS block. The
agent guessed the other form with a date-prefix LIKE instead, read the
same-day firing orders on the lot as the rejected firing's own, and totalled
three of the five firings. No result row holds both forms, and the answer
has no block, so nothing told the orchestrator, whose IDENTIFIER-MISS
recovery would have asked the system that pairs the two forms.

The answer is returned unchanged, with no extra model turn. After it the
engine carries one fixed line and the guard's block, fenced as the protocol
writes it. The line is for the orchestrator only and is kept off every
surface a person reads; the block stays, as a block a system agent writes
itself does. The agent is a real system agent built by
``create_system_agent`` from the fixture's schema; only its model turns are
scripted, so what the code reads is what the agent's own hooks record.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents import identifier_shape_guard as g  # noqa: E402
from agents import schema_format  # noqa: E402
from agents.response_integrity_capture import initialize_integrity_state  # noqa: E402
from agents.response_integrity_query import strict_json_dumps  # noqa: E402

_FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                       / "system_agent_unresolved_miss_firing.json").read_text())
QUESTION = _FIXTURE["question"]
ANSWER = _FIXTURE["answer"]
CALLS = _FIXTURE["tool_calls"]
REFUSED = _FIXTURE["refused_value"]
LOOKED_IN = "kiln_consumption.firing_ref"
MARKER = "[engine] Identifier forms not paired:"
SENTENCE = "were not paired with one value of the other form by any result in this call"
TOOL = "query_kiln_data"


@pytest.fixture(autouse=True)
def _no_graph(monkeypatch):
    """No AWS service is reached: the agent's graph reads (record keys,
    concept mappings) are fail-soft and see an unreachable graph, and the
    runtime config finds no memory resource."""
    from agents import config, db

    def _unreachable(*_a, **_k):
        raise RuntimeError("no graph in this test")
    monkeypatch.setattr(db, "_neptune_execute", _unreachable)
    monkeypatch.setattr(config, "_discover_memory_id", lambda _region: None)


def _desc(table_columns=None, pairs=None) -> str:
    details = {t: [tuple(c) for c in cols]
               for t, cols in (table_columns or _FIXTURE["table_columns"]).items()}
    return schema_format.with_distinct_identifiers(
        schema_format.format_tables_desc(details),
        [_FIXTURE["measured_pair"]] if pairs is None else pairs)


def _agent(desc: str | None = None, schema: str = "kiln_data"):
    return sa.create_system_agent(
        system_id="kiln-ledger", system_name="Kiln ledger", schema=schema, system_prompt="",
        tables_desc=_desc() if desc is None else desc,
        conn_params={"host": "localhost", "port": 5432, "dbname": "kiln",
                     "user": "reader", "password": "test-only"},  # nosec B106
        connection_type="PostgreSQL",
    )


def _block(token: str, looked_in: str = LOOKED_IN) -> str:
    """The opening of the fenced block the protocol writes for ``token``."""
    return f"```\nIDENTIFIER-MISS:\ntoken: {token}\nlooked_in: {looked_in}\n"


class _Script:
    """One scripted model invocation: run ``calls``, then answer ``answer``."""

    def __init__(self, calls=None, answer=ANSWER, tool=TOOL):
        self.calls = CALLS if calls is None else calls
        self.answer = answer
        self.tool = tool
        self.prompts: list = []

    def __call__(self, agent, prompt=None, *, invocation_state=None, limits=None, **kwargs):
        from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent
        self.prompts.append(prompt)
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(
            agent=agent, invocation_state=invocation_state, messages=None))
        agent.messages.append({"role": "user", "content": [{"text": prompt}]})
        for k, call in enumerate(self.calls, start=1):
            use = {"toolUseId": f"tooluse_{k}", "name": self.tool, "input": {"sql": call["sql"]}}
            agent.messages.append({"role": "assistant", "content": [{"toolUse": use}]})
            agent.hooks.invoke_callbacks(BeforeToolCallEvent(
                agent=agent, selected_tool=None, tool_use=use,
                invocation_state=invocation_state, cancel_tool=False))
            result = {"toolUseId": use["toolUseId"], "status": call.get("status", "success"),
                      "content": [{"text": call["result"]}]}
            agent.hooks.invoke_callbacks(AfterToolCallEvent(
                agent=agent, selected_tool=None, tool_use=use,
                invocation_state=invocation_state, result=result, exception=None))
            agent.messages.append({"role": "user", "content": [{"toolResult": result}]})
        agent.messages.append({"role": "assistant", "content": [{"text": self.answer}]})
        return SimpleNamespace(message={"role": "assistant", "content": [{"text": self.answer}]},
                               stop_reason="end_turn")


def _use(monkeypatch, script) -> None:
    from strands import Agent
    monkeypatch.setattr(Agent, "__call__", lambda self, *a, **k: script(self, *a, **k))
    # The executed-filters line (its own tests) appends to every answer with a WHERE.
    monkeypatch.setattr(sa, "_executed_filters_fact", lambda state: "")


def _run(monkeypatch, script, question=QUESTION, agent=None):
    """``(answer, tail)``: the agent's answer and what the engine appends to it."""
    _use(monkeypatch, script)
    state: dict = {}
    initialize_integrity_state(state, question)
    answer = sa._legacy_mode_a_answer(agent or _agent(), question, "report-only", state)
    full = sa._append_engine_facts(answer, state, question=question)
    return answer, full[len(answer):]


def _rows_call(sql: str, columns: list, rows: list, **extra) -> dict:
    return {"sql": sql, "result": strict_json_dumps(
        {"columns": columns, "rows": rows, "row_count": len(rows),
         "_adt_executed_query": sql, **extra})}


#: A result one row of which holds the refused value under the paired column
#: and its counterpart under the looked-in column (the two tables joined
#: through a link that says the rows belong together).
_PAIRING_SQL = ("SELECT dl.firing_ref, kc.firing_ref FROM dispatch_lines dl "
                "JOIN firing_links fl ON fl.dispatch_line_id = dl.dispatch_line_id "
                "JOIN kiln_consumption kc ON kc.consumption_id = fl.consumption_id "
                f"WHERE dl.firing_ref = '{REFUSED}'")
_PAIRING = _rows_call(_PAIRING_SQL, ["firing_ref", "firing_ref"],
                      [[REFUSED, "FO-20250314-40001"], [REFUSED, "FO-20250314-40001"]])


def _pairing(value: str, counterpart: str, columns=("firing_ref", "firing_ref")) -> dict:
    return _rows_call(_PAIRING_SQL.replace(REFUSED, value), list(columns),
                      [[value, counterpart]])


#: A row of the refused value with a link key, and a query for the other form
#: by that key alone: a two-query recovery.
LINK = "PR-7781-0042"
_LINK_ROW = _rows_call(
    "SELECT dl.firing_ref, dl.production_ref FROM dispatch_lines dl "
    f"WHERE dl.firing_ref = '{REFUSED}'", ["firing_ref", "production_ref"], [[REFUSED, LINK]])


def _by_link(rows: list, sql: str = "") -> dict:
    sql = sql or ("SELECT kc.firing_ref FROM kiln_consumption kc "
                  f"WHERE kc.production_ref = '{LINK}'")
    return _rows_call(sql, ["firing_ref"], rows)


def _refusal_call(values: list) -> dict:
    listed = ", ".join(f"'{v}'" for v in values)
    sql = ("SELECT kc.stock_lot_id FROM kiln_consumption kc "
           f"WHERE kc.firing_ref IN ({listed})")
    refused = g.check(sql, g.context_from(_desc()))
    assert refused is not None
    return {"sql": sql, "result": strict_json_dumps(refused)}


# --------------------------------------------------------------------------- #
# The fixture is the failure
# --------------------------------------------------------------------------- #

def test_the_fixture_refusal_is_the_guards_own_and_the_rest_ran():
    ctx = g.context_from(_desc())
    assert json.loads(CALLS[0]["result"]) == g.check(CALLS[0]["sql"], ctx)
    assert all(g.check(call["sql"], ctx) is None for call in CALLS[1:])


def test_no_result_row_holds_both_forms_and_the_answer_has_no_block():
    for call in CALLS[1:]:
        for row in json.loads(call["result"])["rows"]:
            cells = [str(cell) for cell in row]
            assert not (any(c.startswith("FO-") for c in cells)
                        and any(c.startswith("KB-") for c in cells)), row
    assert "IDENTIFIER-MISS" not in ANSWER
    assert "FO-20250314-41002" in ANSWER, "the answer states a pairing no row showed"


# --------------------------------------------------------------------------- #
# The orchestrator gets the guard's block
# --------------------------------------------------------------------------- #

def test_the_orchestrator_gets_the_guards_identifier_miss_block(monkeypatch):
    script = _Script()
    answer, tail = _run(monkeypatch, script)
    assert len(script.prompts) == 1, "a second model turn ran"
    assert answer == ANSWER, "the answer was replaced"
    assert _block(REFUSED) in tail, tail
    fence = sa._MISS_FENCE_RE.search(tail)
    assert fence is not None
    assert sa._MISS_TOKEN_RE.search(fence.group(0)).group(1) == REFUSED
    assert "my_format: kiln_consumption.firing_ref values are shaped like FO-99999999-99999" \
        in fence.group(0)


def test_the_line_says_the_translation_was_not_paired(monkeypatch):
    _answer, tail = _run(monkeypatch, _Script())
    assert MARKER in tail and SENTENCE in tail
    assert tail.index(MARKER) < tail.index("IDENTIFIER-MISS:")


def test_the_query_entry_point_returns_the_answer_and_the_block(monkeypatch):
    """The deployed default (report-only) through the runtime's own entry point."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    script = _Script()
    _use(monkeypatch, script)
    answer = asyncio.run(sa._run_mode_a_query(_agent(), "kiln-ledger", QUESTION))
    assert len(script.prompts) == 1
    assert answer.startswith(ANSWER), "the answer was replaced"
    assert _block(REFUSED) in answer[len(ANSWER):]


def _unit_schema_tail(monkeypatch, question: str = "Which lot did unit QZ-0042 use?") -> str:
    """A refused ``QZ-0042`` on another made-up schema: tables
    ``consumption`` and ``dispatch_lines``, column ``ref_code``, concept
    ``unit``, forms ``KX-9999`` and ``QZ-9999``."""
    columns = {"consumption": [["consumption_id", "uuid"], ["ref_code", "character varying"],
                               ["lot_id", "uuid"]],
               "dispatch_lines": [["line_id", "uuid"], ["ref_code", "character varying"],
                                  ["qty", "integer"]]}
    pair = {"concept": "unit", "column": "ref_code", "table_a": "consumption",
            "table_b": "dispatch_lines", "read_a": 40, "read_b": 37,
            "shape_a": "KX-9999", "shape_b": "QZ-9999"}
    desc = _desc(columns, [pair])
    wrong = "SELECT c.lot_id FROM consumption c WHERE c.ref_code = 'QZ-0042'"
    refused = g.check(wrong, g.context_from(desc))
    calls = [{"sql": wrong, "result": strict_json_dumps(refused)},
             _rows_call("SELECT c.ref_code, c.lot_id FROM consumption c "
                        "WHERE c.ref_code LIKE 'KX-00%'", ["ref_code", "lot_id"],
                        [["KX-0042", "0b9e4c1d-2f3a-4b5c-8d7e-6f5a4b3c2d1e"]])]
    agent = _agent(desc, schema="unit_data")
    _answer, tail = _run(monkeypatch, _Script(calls, "Unit QZ-0042 used lot 0b9e.",
                                              tool="query_unit_data"),
                         question=question, agent=agent)
    return tail


def test_a_differently_named_schema_gets_its_own_block(monkeypatch):
    """Generic: other tables, column, concept and forms."""
    tail = _unit_schema_tail(monkeypatch)
    assert _block("QZ-0042", "consumption.ref_code") in tail
    assert MARKER in tail


# --------------------------------------------------------------------------- #
# No line
# --------------------------------------------------------------------------- #

def _quiet(tail: str) -> None:
    assert MARKER not in tail and "IDENTIFIER-MISS" not in tail, tail


def test_a_later_row_that_pairs_the_value_gets_no_line(monkeypatch):
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], _PAIRING, *CALLS[1:]]))
    _quiet(tail)


def test_a_row_from_before_the_refusal_does_not_resolve_it(monkeypatch):
    """Evidence counts only after the value's refusal."""
    _answer, tail = _run(monkeypatch, _Script([_PAIRING, *CALLS]))
    assert _block(REFUSED) in tail


def test_an_unrelated_row_of_both_shapes_does_not_resolve_it(monkeypatch):
    """A row pairing another value of the paired form proves nothing for this one."""
    other = _pairing("KB-20250314-49999", "FO-20250314-49999")
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], other, *CALLS[1:]]))
    assert _block(REFUSED) in tail


def test_a_mixed_list_gets_a_block_for_the_value_not_paired(monkeypatch):
    second = "KB-20250313-42001"
    calls = [_refusal_call([REFUSED, second]), _pairing(REFUSED, "FO-20250314-40001"),
             *CALLS[1:]]
    _answer, tail = _run(monkeypatch, _Script(calls))
    assert _block(second) in tail
    assert f"token: {REFUSED}" not in tail


def test_a_value_paired_with_two_counterparts_is_not_resolved(monkeypatch):
    """Rows that pair the value with several records of the other form (a
    join through a key they share) do not prove its counterpart."""
    calls = [CALLS[0], _pairing(REFUSED, "FO-20250314-40001"),
             _pairing(REFUSED, "FO-20250314-41002"), *CALLS[1:]]
    _answer, tail = _run(monkeypatch, _Script(calls))
    assert _block(REFUSED) in tail


def test_a_pairing_under_columns_that_are_not_the_measured_ones_does_not_count(monkeypatch):
    other_table = _rows_call(
        "SELECT fo.order_ref, fo.dispatch_label FROM firing_orders fo "
        f"WHERE fo.dispatch_label = '{REFUSED}'", ["order_ref", "dispatch_label"],
        [["FO-20250314-40001", REFUSED]])
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], other_table, *CALLS[1:]]))
    assert _block(REFUSED) in tail


def test_aliased_columns_the_select_list_proves_count(monkeypatch):
    """The SELECT list proves each aliased column's source column."""
    sql = ("SELECT dl.firing_ref AS dispatch_ref, kc.firing_ref AS order_ref "
           "FROM dispatch_lines dl JOIN kiln_consumption kc ON kc.qty_used = dl.qty "
           f"WHERE dl.firing_ref = '{REFUSED}'")
    aliased = _rows_call(sql, ["dispatch_ref", "order_ref"], [[REFUSED, "FO-20250314-40001"]])
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], aliased, *CALLS[1:]]))
    _quiet(tail)


# --------------------------------------------------------------------------- #
# A recovery through two queries
# --------------------------------------------------------------------------- #

def test_a_two_query_recovery_by_a_returned_link_key_gets_no_line(monkeypatch):
    by_link = _by_link([["FO-20250314-40001"], ["FO-20250314-40001"]])
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], _LINK_ROW, by_link, *CALLS[1:]]))
    _quiet(tail)


def test_a_link_key_query_before_the_key_was_returned_does_not_count(monkeypatch):
    by_link = _by_link([["FO-20250314-40001"]])
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], by_link, _LINK_ROW, *CALLS[1:]]))
    assert _block(REFUSED) in tail


def test_a_link_key_that_reaches_two_records_does_not_count(monkeypatch):
    by_link = _by_link([["FO-20250314-40001"], ["FO-20250314-41002"]])
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], _LINK_ROW, by_link, *CALLS[1:]]))
    assert _block(REFUSED) in tail


def test_a_link_key_query_with_another_filter_value_does_not_count(monkeypatch):
    sql = ("SELECT kc.firing_ref FROM kiln_consumption kc "
           f"WHERE kc.production_ref = '{LINK}' AND kc.firing_ref LIKE 'FO-20250314-%'")
    by_link = _by_link([["FO-20250314-40001"]], sql)
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], _LINK_ROW, by_link, *CALLS[1:]]))
    assert _block(REFUSED) in tail


def test_a_query_by_a_key_of_another_value_does_not_count(monkeypatch):
    other_row = _rows_call(
        "SELECT dl.firing_ref, dl.production_ref FROM dispatch_lines dl "
        "WHERE dl.firing_ref = 'KB-20250314-49999'", ["firing_ref", "production_ref"],
        [["KB-20250314-49999", LINK]])
    by_link = _by_link([["FO-20250314-40001"]])
    _answer, tail = _run(monkeypatch, _Script([CALLS[0], other_row, by_link, *CALLS[1:]]))
    assert _block(REFUSED) in tail


def test_a_call_with_no_refusal_gets_no_line(monkeypatch):
    _answer, tail = _run(monkeypatch, _Script(CALLS[1:]))
    _quiet(tail)


def test_an_answer_that_carries_the_block_gets_no_duplicate(monkeypatch):
    block = json.loads(CALLS[0]["result"])["identifier_miss"][0]
    answer = f"{ANSWER}\n\n```\n{block}\n```"
    out, tail = _run(monkeypatch, _Script(answer=answer))
    assert out == answer
    _quiet(tail)


def test_only_the_value_the_answer_did_not_report_gets_a_block(monkeypatch):
    other = "KB-20250313-42001"
    calls = [_refusal_call([REFUSED, other]), *CALLS[1:]]
    answer = f"{ANSWER}\n\n```\nIDENTIFIER-MISS:\ntoken: {REFUSED}\nlooked_in: {LOOKED_IN}\n```"
    _out, tail = _run(monkeypatch, _Script(calls, answer))
    assert _block(other) in tail
    assert f"token: {REFUSED}" not in tail


def test_a_value_the_answer_reports_as_no_record_gets_no_block(monkeypatch):
    answer = f"{ANSWER}\n\nNo record of {REFUSED} in {LOOKED_IN} yet: the lookup returned no rows."
    _out, tail = _run(monkeypatch, _Script(answer=answer))
    _quiet(tail)


def test_a_cut_result_gets_no_line(monkeypatch):
    cut = _rows_call("SELECT kc.firing_ref FROM kiln_consumption kc "
                     "WHERE kc.firing_ref LIKE 'FO-%'", ["firing_ref"],
                     [["FO-20250314-40001"]], truncated=True, partial_reason="row_limit")
    _answer, tail = _run(monkeypatch, _Script([*CALLS, cut]))
    _quiet(tail)


def test_a_result_whose_rows_cannot_be_read_gets_no_line(monkeypatch):
    unread = {"sql": "SELECT 1", "result": "rows: FO-20250314-40001 KB-20250314-40001"}
    _answer, tail = _run(monkeypatch, _Script([*CALLS, unread]))
    _quiet(tail)


def test_more_values_than_the_bound_get_no_line(monkeypatch):
    values = [f"KB-20250314-4000{k}" for k in range(1, 7)]
    _answer, tail = _run(monkeypatch, _Script([_refusal_call(values), *CALLS[1:]]))
    _quiet(tail)


def test_a_malformed_refusal_gets_no_line(monkeypatch):
    refusal = json.loads(CALLS[0]["result"])
    refusal["identifier_miss"] = []
    calls = [{"sql": CALLS[0]["sql"], "result": strict_json_dumps(refusal)}, *CALLS[1:]]
    _answer, tail = _run(monkeypatch, _Script(calls))
    _quiet(tail)


def test_a_value_that_could_close_the_fence_gets_no_line(monkeypatch):
    refusal = json.loads(CALLS[0]["result"])
    refusal["identifier_check"]["values"] = ["KB-```\nx-40001"]
    calls = [{"sql": CALLS[0]["sql"], "result": strict_json_dumps(refusal)}, *CALLS[1:]]
    _answer, tail = _run(monkeypatch, _Script(calls))
    _quiet(tail)


def test_a_fault_while_recording_gets_no_line(monkeypatch):
    from agents import unresolved_miss

    def _boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(unresolved_miss, "_read_payload", _boom)
    _answer, tail = _run(monkeypatch, _Script())
    _quiet(tail)


PAIRED_QUESTION = QUESTION.replace(
    f"firing_ref '{REFUSED}'", f"firing_ref '{REFUSED}' (firing order FO-20250314-40001)")


def test_a_question_that_gives_the_values_counterpart_gets_no_line(monkeypatch):
    """The question put a value of the looked-in form right next to the
    refused value, so the call had its counterpart from the request."""
    assert PAIRED_QUESTION != QUESTION
    _answer, tail = _run(monkeypatch, _Script(), question=PAIRED_QUESTION)
    _quiet(tail)


@pytest.mark.parametrize("extra", [
    "Firing order FO-20250313-99999 is unrelated.",
    "translated_identifiers: kiln_consumption.firing_ref=FO-20250314-40001",
    f"Compare {REFUSED}, FO-20250313-99999 and others.",
], ids=["other-sentence", "other-line", "after-a-comma"])
def test_an_unrelated_value_of_the_looked_in_form_does_not_count(monkeypatch, extra):
    """A value of the looked-in form that the question does not pair with
    the refused value is not its counterpart."""
    _answer, tail = _run(monkeypatch, _Script(), question=f"{QUESTION}\n{extra}")
    assert _block(REFUSED) in tail


V, C = REFUSED, "FO-20250314-40001"


@pytest.mark.parametrize("pairing", [
    f"{V} (firing order {C})",
    f"{V} (also known as {C})",
    f"kiln batch {V} / {C}",
    f"firing order {C} (kiln batch {V})",
    f"| kiln batch | firing order |\n|---|---|\n| {V} | {C} |",
    f"{V}, firing order {C}",
    f"{V} ({C})",
    f"{V} (a.k.a. {C})",
    f"{V} aka {C}",
    f"{V}, also known as {C}",
    f"{V} = {C}",
    f"{V}/{C}",
    f"`{V}` (the firing order number `{C}`)",
], ids=["parens-label", "also-known-as", "slash", "label-each-side", "table-row",
        "comma-label", "parens", "a.k.a.", "aka", "comma-also-known-as", "equals",
        "slash-no-space", "quoted"])
def test_an_explicit_pairing_in_the_question_gets_no_line(monkeypatch, pairing):
    """The question pairs the refused value with its counterpart by a mark,
    a pairing phrase or a label naming the form after it."""
    _answer, tail = _run(monkeypatch, _Script(),
                         question=f"Which clients got parts from firing {pairing}?")
    _quiet(tail)


@pytest.mark.parametrize("wording", [
    f"Compare {V} against unrelated {C}",
    f"{V} versus {C}",
    f"{V} vs {C}",
    f"{V} (not {C})",
    f"{V} (different from {C})",
    f"{V}, unrelated firing order {C}",
    f"{V}, {C}",
    f"{V} and {C}",
    f"{V} {C}",
    f"{V} (lot {C})",
    f"{V}: {C}",
], ids=["against-unrelated", "versus", "vs", "parens-not", "different-from",
        "comma-unrelated-label", "comma-list", "and", "bare", "parens-other-label", "colon"])
def test_a_contrast_or_a_list_in_the_question_is_no_pairing(monkeypatch, wording):
    _answer, tail = _run(monkeypatch, _Script(), question=f"Look at firings {wording}.")
    assert _block(REFUSED) in tail


def test_a_multi_value_list_keeps_each_pair_local(monkeypatch):
    second, its_own = "KB-20250313-42001", "FO-20250313-42001"
    calls = [_refusal_call([REFUSED, second]), *CALLS[1:]]
    question = f"Firings {REFUSED} ({C}), {second} ({its_own})."
    _answer, tail = _run(monkeypatch, _Script(calls), question=question)
    _quiet(tail)


def test_a_value_after_a_pair_in_a_list_is_not_paired(monkeypatch):
    second = "KB-20250313-42001"
    calls = [_refusal_call([REFUSED, second]), *CALLS[1:]]
    question = f"Firings {REFUSED} ({C}), {second}."
    _answer, tail = _run(monkeypatch, _Script(calls), question=question)
    assert _block(second) in tail
    assert f"token: {REFUSED}" not in tail


def test_each_value_is_judged_on_its_own_counterpart(monkeypatch):
    second = "KB-20250313-42001"
    question = f"Firings {REFUSED} (firing order FO-20250314-40001) and {second}."
    calls = [_refusal_call([REFUSED, second]), *CALLS[1:]]
    _answer, tail = _run(monkeypatch, _Script(calls), question=question)
    assert _block(second) in tail
    assert f"token: {REFUSED}" not in tail


def test_a_counterpart_paired_with_two_values_is_neither_ones(monkeypatch):
    """A counterpart the question pairs with a value of the paired form on
    each side could be either one's, so neither value counts as supplied."""
    second = "KB-20250313-42001"
    question = f"Firings {REFUSED} / FO-20250314-40001 / {second}."
    calls = [_refusal_call([REFUSED, second]), *CALLS[1:]]
    _answer, tail = _run(monkeypatch, _Script(calls), question=question)
    assert _block(second) in tail and _block(REFUSED) in tail


@pytest.mark.parametrize("question,quiet", [
    ("Which lot did unit QZ-0042 (unit KX-0042) use?", True),
    ("Which lot did unit QZ-0042, consumption KX-0042 use?", True),
    ("Which lot did unit QZ-0042, firing order KX-0042 use?", False),
    ("Which lot did unit QZ-0042 (kiln batch KX-0042) use?", False),
], ids=["concept-label", "table-label", "other-schemas-words", "other-schemas-initials"])
def test_labels_come_from_the_measured_pair(monkeypatch, question, quiet):
    """On another schema, a label counts when it names that pair's own
    concept, table or shape letters; the words that label the first schema's
    forms mean nothing here."""
    tail = _unit_schema_tail(monkeypatch, question)
    if quiet:
        _quiet(tail)
    else:
        assert _block("QZ-0042", "consumption.ref_code") in tail


@pytest.mark.parametrize("kg_context", [
    "```ROUTE:\ntranslated_identifiers: kiln_consumption.firing_ref=FO-20250314-40001\n```",
    f"```ROUTE:\ntranslated_identifiers: {REFUSED} FO-20250314-40001\n```",
], ids=["route-gives-the-form", "route-gives-the-pair"])
def test_only_the_delegated_question_is_read(monkeypatch, kg_context):
    """Through the runtime's entry point the agent also gets the time line,
    concept mappings and route context around the question; none of them is
    read as what the request supplied."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    _use(monkeypatch, _Script())
    answer = asyncio.run(sa._run_mode_a_query(_agent(), "kiln-ledger", QUESTION, kg_context))
    assert _block(REFUSED) in answer[len(ANSWER):]


def test_the_entry_point_reads_the_delegated_question(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    _use(monkeypatch, _Script())
    answer = asyncio.run(sa._run_mode_a_query(_agent(), "kiln-ledger", PAIRED_QUESTION))
    _quiet(answer[len(ANSWER):])


def test_an_agent_without_measured_pairs_records_nothing(monkeypatch):
    """No guard context, so no result can be a guard refusal."""
    agent = _agent(_desc(pairs=[]))
    _answer, tail = _run(monkeypatch, _Script(), agent=agent)
    _quiet(tail)


def test_a_join_refusal_names_no_value_and_gets_no_line(monkeypatch):
    sql = ("SELECT dl.qty FROM dispatch_lines dl JOIN kiln_consumption kc "
           "ON kc.firing_ref = dl.firing_ref")
    refused = g.check(sql, g.context_from(_desc()))
    assert refused["identifier_check"]["kind"] == "paired_join"
    _answer, tail = _run(monkeypatch, _Script([{"sql": sql, "result": strict_json_dumps(
        refused)}, *CALLS[1:]]))
    _quiet(tail)


# --------------------------------------------------------------------------- #
# The line is the orchestrator's alone; the block stays where a person reads
# --------------------------------------------------------------------------- #

#: A short answer, so the summary's and the preview's caps can hide nothing.
SHORT = (f"Firing {REFUSED} drew lot SL-TALC-0315-B; its other firings are "
         "FO-20250313-42001 and FO-20250313-42004.")


@pytest.fixture
def delegated(monkeypatch):
    """What the system agent returns for the short answer, through the
    runtime's own entry point, with its investigation trace captured."""
    from agents import investigation_trace_store
    from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    _use(monkeypatch, _Script(answer=SHORT))
    stored: list = []
    monkeypatch.setattr(investigation_trace_store, "put_trace",
                        lambda session, ref, entry, **_kw: stored.append(entry) or True)
    token = provenance_context_var.set(("session-1", "query-1"))
    wtoken = provenance_write_token_var.set("0123456789abcdef0123456789abcdef")
    try:
        answer = asyncio.run(sa._run_mode_a_query(_agent(), "kiln-ledger", QUESTION))
    finally:
        provenance_context_var.reset(token)
        provenance_write_token_var.reset(wtoken)
    return answer, stored


def _line(answer: str) -> str:
    start = answer.index(MARKER)
    end = answer.find("\n", start)
    return answer[start:] if end < 0 else answer[start:end]


def _public(text: str) -> None:
    """A surface a person reads: no line, and the block as the protocol wrote it."""
    assert MARKER not in text and SENTENCE not in text, text
    assert f"token: {REFUSED}" in text


def test_the_marker_is_an_orchestrator_only_line(delegated):
    from agents.internal_lines import MARKERS, is_marker_line
    answer, _stored = delegated
    assert answer.startswith(SHORT)
    assert len(answer) < 2_000, "the answer must be short enough for the summary to show whole"
    assert MARKER in MARKERS
    assert is_marker_line(_line(answer))
    assert _block(REFUSED) in answer


def test_the_tool_activity_summaries_drop_the_line(delegated):
    from agents.chat_client import _tool_result_from_payload, _ToolResultHook
    answer, _stored = delegated
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)
    hook._on_tool_done(SimpleNamespace(
        tool_use={"name": "kiln-ledger___query_kiln_ledger", "toolUseId": "tool-1",
                  "input": {"question": "q"}},
        result={"toolUseId": "tool-1", "status": "success", "content": [{"text": answer}]}))
    emitted = []
    while not q.empty():
        emitted.append(q.get_nowait())
    results = [e for e in emitted if isinstance(e, dict) and e.get("event") == "tool_result"]
    assert results, emitted
    summary = results[-1]["data"]["summary"]
    assert SHORT in summary
    assert MARKER not in summary and SENTENCE not in summary
    event = _tool_result_from_payload({"toolUseId": "tool-1", "content": [{"text": answer}]})
    _public(event.tool_result or "")
    assert MARKER not in event.summary


def test_the_stored_investigation_trace_drops_the_line(delegated):
    answer, stored = delegated
    assert len(stored) == 1
    final = stored[0]["final_answer"]
    _public(final)
    # The wrong-form line (``wrong_form_refused``) is orchestrator-only too.
    refused = "\n\n[engine] Wrong-form lookup refused: concept=firing"
    assert refused in answer and refused not in final
    assert final == answer.replace(f"\n\n{_line(answer)}", "", 1).replace(refused, "", 1)
    assert MARKER in answer, "the orchestrator's copy lost the line"


def _orchestrator_answer(answer: str) -> str:
    """An orchestrator answer that copies the engine line and its block."""
    return f"Three clients received parts.\n\n{answer[answer.index(MARKER):]}\n\nTotal: 70 units."


def test_the_external_answer_drops_a_copied_line(delegated):
    from agents.public_answer import external_answer_text
    answer, _stored = delegated
    _public(external_answer_text(_orchestrator_answer(answer)))


@pytest.mark.parametrize("size", [1, 5, 64, 8192])
def test_the_streamed_web_answer_drops_a_copied_line(delegated, size):
    from agents.public_answer import InternalTelemetryFilter
    answer, _stored = delegated
    text = _orchestrator_answer(answer)
    filter_ = InternalTelemetryFilter()
    out = "".join(filter_.feed(text[i:i + size]) for i in range(0, len(text), size))
    out += filter_.flush()
    _public(out)
    assert out.startswith("Three clients received parts.\n\n")
    assert out.rstrip().endswith("Total: 70 units.")


def test_the_done_event_drops_a_copied_line(delegated):
    from agents.events import DoneEvent
    from agents.public_answer import InternalTelemetryFilter
    from agents.server import _public_answer_events
    answer, _stored = delegated
    events = _public_answer_events(DoneEvent(_orchestrator_answer(answer)),
                                   InternalTelemetryFilter())
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done
    _public(done[0].text)


def test_the_stored_assistant_message_drops_a_copied_line(delegated):
    from tests.unit.test_engine_lines_off_stored_copies import _store, _texts
    answer, _stored = delegated
    text = _orchestrator_answer(answer)
    original, stored = _store({"role": "assistant", "content": [{"text": text}]})
    (kept,) = _texts(stored)
    _public(kept)
    assert _texts(original) == [text], "the live conversation was changed"


def test_recent_sessions_replay_drops_the_line(delegated):
    from tests.unit.test_engine_lines_off_stored_copies import _replayed
    answer, _stored = delegated
    body = _replayed(_orchestrator_answer(answer), tool_text=answer)
    assert MARKER not in json.dumps(body) and SENTENCE not in json.dumps(body)
    _public(body["messages"][-1]["content"])


def test_route_facts_read_the_result_without_the_line_or_the_block(delegated):
    """The line is left out, and the block's ``token`` label does not reach
    the value the answer states in prose."""
    from agents import carried_values
    from agents import route_facts as rf
    answer, _stored = delegated
    facts = rf.TurnRouteFacts(systems=("kiln-ledger",))
    rf.note_call(facts, "kiln-ledger___query_kiln_ledger", "t1", None,
                 {"status": "success", "content": [{"text": answer}]})
    without = answer.replace(f"\n\n{_line(answer)}", "", 1)
    assert facts.results == [carried_values.peer_result("kiln-ledger",
                                                        rf.without_miss_blocks(without))]
    (read,) = facts.results
    assert REFUSED in read.tokens
    assert "token" not in read.labels.get(REFUSED, frozenset())
