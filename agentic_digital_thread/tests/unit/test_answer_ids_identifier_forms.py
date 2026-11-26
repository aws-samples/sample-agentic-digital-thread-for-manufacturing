# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An identifier the graph's own machinery produces is not flagged as made up,
and a caller cannot add to what counts.

``answer_ids`` flags an answer identifier in the form of the schema's
examples that no result of the call returned. One the graph produces from a
value the results did return is real: an IdentityRule's output (a prefix, a
case change, padding digits written into the pattern) or the other end of a
trusted stored value mapping. The system agent runtime reads those for its
own system from the graph (``identifier_forms.forms_for_system``); nothing
the caller sends can add to them, and a transform described only in free
text never counts.

Every schema, system and value here is invented: kiln lots keyed ``KLN-...``
in one system and a bare numeric ``lot_no`` in another.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents import answer_ids as ai  # noqa: E402
from agents import identifier_forms as idf  # noqa: E402

SYSTEM = "kiln-mes"
DESC = ("kiln_lots [view](lot_ref: uuid e.g. 0b6f0e8e-1c1d-4c55-9a41-6f0f3f0a9b11; "
        "lot_code: character varying e.g. KLN-0412, KLN-0413, KLN-0587; "
        "lot_no: integer e.g. 412, 413; glaze: character varying e.g. Matte White)")
ROWS = {"columns": ["lot_no", "glaze"], "rows": [[41, "Matte White"]], "row_count": 1,
        "_adt_executed_query": "SELECT lot_no, glaze FROM kiln_lots WHERE glaze = 'Matte White'"}
#: IdentityRule patterns as the graph stores them.
PREFIX = {"source_pattern": "{id}", "target_pattern": "KLN-{id}"}
PADDED = {"source_pattern": "{id}", "target_pattern": "KLN-00{id}"}
LOWER = {"source_pattern": "{id}", "target_pattern": "kln-{id}"}
WILDCARD = {"source_pattern": "{id}", "target_pattern": "KLN-{id}-{*}"}
#: A trusted ValueMapping row (deterministic method under the deterministic writer's id).
MAPPING = {"id": "vm-equip-kiln-41", "method": "value_format_match",
           "from_value": "41", "to_value": "KLN-0041"}
#: The invocation-state key the system agent puts the forms under.
STATE_KEY = "_adt_identifier_forms"


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset = getattr(idf, "reset", None)
    if reset:
        reset()
    yield
    if reset:
        reset()


def _graph(rules=(), mappings=(), fail=False):
    """A fake graph read: the IdentityRule read, then the ValueMapping read."""
    calls: list = []

    def query(cypher, params):
        calls.append((cypher, params))
        if fail:
            raise RuntimeError("graph unavailable")
        return list(rules) if "IdentityRule" in cypher else list(mappings)
    query.calls = calls
    return query


def _forms(rules=(), mappings=()):
    return idf.forms_for_system(SYSTEM, query=_graph(rules, mappings))


def _unreturned(answer, forms=None, payloads=(ROWS,), question=""):
    leads = ai.schema_id_leads(sa._desc_entries(DESC))
    state: dict = {}
    if forms is not None:
        state[STATE_KEY] = forms
    for payload in payloads:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        ai.record_result(state, {"status": "success", "content": [{"text": text}]}, leads)
    return ai.unreturned_ids(answer, leads, state, question)


# --------------------------------------------------------------------------- #
# Prefixing, case conversion and zero padding by an IdentityRule
# --------------------------------------------------------------------------- #

def test_prefixing_by_an_identity_rule():
    assert _unreturned("Lot KLN-41 was fired.") == ["KLN-41"]
    assert _unreturned("Lot KLN-41 was fired.", _forms([PREFIX])) == []


def test_zero_padding_written_into_the_rule():
    forms = _forms([PADDED])
    assert _unreturned("Lot KLN-0041 was fired.", forms) == []
    assert _unreturned("Lot KLN-00041 was fired.", forms) == ["KLN-00041"], (
        "a padding the rule does not produce from a returned value was accepted")


def test_case_conversion_by_the_rule():
    assert _unreturned("Lot KLN-41 was fired.", _forms([LOWER])) == []


def test_the_other_end_of_a_trusted_value_mapping():
    forms = _forms(mappings=[MAPPING])
    assert _unreturned("Lot KLN-0041 was fired.", forms) == []
    rows = {**ROWS, "rows": [[42, "Matte White"]]}
    assert _unreturned("Lot KLN-0041 was fired.", forms, (rows,)) == ["KLN-0041"], (
        "a mapping whose other end no result returned was accepted")


def test_an_untrusted_value_mapping_is_not_read():
    for row in ({**MAPPING, "method": "onboarding-llm"}, {**MAPPING, "id": "vm-other-1"},
                {**MAPPING, "id": "vm-learned-1", "method": "user_confirmed"}):
        assert _forms(mappings=[row]).pairs == (), row


# --------------------------------------------------------------------------- #
# What still counts as made up
# --------------------------------------------------------------------------- #

def test_a_rule_whose_input_no_result_returned_does_not_count():
    rows = {**ROWS, "rows": [[42, "Matte White"]]}
    assert _unreturned("Lot KLN-0041 was fired.", _forms([PADDED]), (rows,)) == ["KLN-0041"]


def test_the_input_counts_only_as_a_whole_returned_token():
    rows = {**ROWS, "columns": ["ref"], "rows": [["X-41"]]}
    assert _unreturned("Lot KLN-41 was fired.", _forms([PREFIX]), (rows,)) == ["KLN-41"]


def test_a_rule_input_the_question_gives_counts():
    rows = {**ROWS, "rows": []}
    assert _unreturned("Lot KLN-0041.", _forms([PADDED]), (rows,),
                       question="What glaze did lot no 41 get?") == []


def test_a_transform_described_in_free_text_never_counts():
    question = "Lot codes are the lot number with KLN- and zero padding to four digits."
    assert _unreturned("Lot KLN-0041 was fired.", None, question=question) == ["KLN-0041"]
    assert _unreturned("Lot KLN-0041 was fired.", idf.NO_FORMS, question=question) == ["KLN-0041"]


def test_a_wildcard_rule_is_not_read():
    assert _forms([WILDCARD]).rules == ()


def test_values_cut_means_a_rule_output_cannot_be_checked(monkeypatch):
    monkeypatch.setattr(ai, "MAX_RESULT_TOKENS", 2)
    many = {**ROWS, "rows": [[k, f"glaze {k}"] for k in range(10)]}
    assert _unreturned("Lot KLN-0099.", _forms([PADDED]), (many,)) == []


def test_the_state_key_is_the_one_the_check_reads():
    assert ai.FORMS_STATE_KEY == STATE_KEY


# --------------------------------------------------------------------------- #
# The server-side read
# --------------------------------------------------------------------------- #

def test_the_reads_are_scoped_to_the_system_and_bounded():
    query = _graph([PADDED], [MAPPING])
    idf.forms_for_system(SYSTEM, query=query)
    (rule_cypher, rule_params), (vm_cypher, vm_params) = query.calls
    assert rule_params["sid"] == SYSTEM and vm_params["sid"] == SYSTEM
    assert "ir.source_system = $sid OR ir.target_system = $sid" in rule_cypher
    assert "vm.from_system = $sid OR vm.to_system = $sid" in vm_cypher
    assert f"LIMIT {idf.MAX_RULES + 1}" in rule_cypher
    assert f"LIMIT {idf.MAX_MAPPINGS + 1}" in vm_cypher
    assert "status" in rule_cypher, "a switched-off rule must not be read"


def test_the_read_is_cached_per_system(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(idf, "_monotonic", lambda: now[0])
    query = _graph([PADDED])
    first = idf.forms_for_system(SYSTEM, query=query)
    assert idf.forms_for_system(SYSTEM, query=query) is first
    assert len(query.calls) == 2
    idf.forms_for_system("cast-mes", query=query)
    assert len(query.calls) == 4, "another system shares no cache entry"
    now[0] += idf.CACHE_SECONDS + 1
    idf.forms_for_system(SYSTEM, query=query)
    assert len(query.calls) == 6, "the cache did not expire"


def test_a_failed_read_fails_closed_and_pauses(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(idf, "_monotonic", lambda: now[0])
    good = _graph([PADDED])
    idf.forms_for_system(SYSTEM, query=good)
    now[0] += idf.CACHE_SECONDS + 1
    bad = _graph(fail=True)
    assert idf.forms_for_system(SYSTEM, query=bad) is idf.NO_FORMS, (
        "a failed read kept the earlier forms")
    assert idf.forms_for_system(SYSTEM, query=bad) is idf.NO_FORMS
    assert len(bad.calls) == 1, "the graph was asked again during the pause"


def test_a_read_at_its_cap_keeps_only_what_it_read():
    rules = [{"source_pattern": "{id}", "target_pattern": f"P{k}-{{id}}"}
             for k in range(idf.MAX_RULES + 1)]
    forms = _forms(rules)
    assert len(forms.rules) == 2 * idf.MAX_RULES


def test_bad_rows_are_dropped():
    rules = [{"source_pattern": "A-{id}\n", "target_pattern": "B-{id}"},
             {"source_pattern": "x" * 500, "target_pattern": "B-{id}"},
             {"source_pattern": "B-{id}", "target_pattern": "B-{id}"}, {"oops": 1}, None]
    assert _forms(rules).rules == ()


def test_no_system_id_reads_nothing():
    query = _graph([PADDED])
    assert idf.forms_for_system("", query=query) is idf.NO_FORMS
    assert query.calls == []


# --------------------------------------------------------------------------- #
# End to end, through a raw JSON-RPC tools/call to the system agent's runtime
# --------------------------------------------------------------------------- #

FABRICATED = "KLN-FAB-9999"


def _script(answer):
    from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent

    def call(agent, prompt=None, *, invocation_state=None, limits=None, **kwargs):
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(
            agent=agent, invocation_state=invocation_state, messages=None))
        use = {"toolUseId": "t1", "name": "query_kiln",
               "input": {"sql": ROWS["_adt_executed_query"]}}
        agent.hooks.invoke_callbacks(BeforeToolCallEvent(
            agent=agent, selected_tool=None, tool_use=use,
            invocation_state=invocation_state, cancel_tool=False))
        agent.hooks.invoke_callbacks(AfterToolCallEvent(
            agent=agent, selected_tool=None, tool_use=use, invocation_state=invocation_state,
            result={"toolUseId": "t1", "status": "success",
                    "content": [{"text": json.dumps(ROWS)}]}, exception=None))
        return SimpleNamespace(message={"role": "assistant", "content": [{"text": answer}]},
                               stop_reason="end_turn")
    return call


def _tools_call(monkeypatch, answer, arguments, graph):
    """POST one raw JSON-RPC ``tools/call`` to the assembled system runtime."""
    from starlette.testclient import TestClient
    from strands import Agent
    from agents.runtime_asgi import _assemble_mcp_asgi
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(Agent, "__call__", lambda self, *a, **k: _script(answer)(self, *a, **k))
    monkeypatch.setattr(sa, "_executed_filters_fact", lambda state: "")
    monkeypatch.setattr(idf, "_default_query", graph, raising=False)
    mcp = sa._create_mode_a_mcp_server(
        SYSTEM, "Kiln MES", "kiln", "", DESC,
        {"host": "localhost", "port": 5432, "dbname": "kiln", "user": "reader",
         "password": "test-only"}, "PostgreSQL", None, "", "0.0.0.0", 0)  # nosec B104 B106 - never served
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "query_kiln_mes", "arguments": arguments}}
    with TestClient(_assemble_mcp_asgi(mcp, SYSTEM)) as tc:
        resp = tc.post("/mcp/", content=json.dumps(rpc), headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json"})
    assert resp.status_code == 200, resp.text
    return resp.text


def test_a_caller_sent_value_cannot_suppress_the_line(monkeypatch):
    """A raw caller adds a reserved-looking argument naming the fabricated
    identifier as allowed. The runtime's own graph read has no such form, so
    the line names it."""
    forged = json.dumps({"rules": [["{id}", "KLN-FAB-{id}"]], "values": [FABRICATED]})
    text = _tools_call(monkeypatch, f"Lot no 41 is {FABRICATED}, glazed Matte White.",
                       {"question": "Which lot got Matte White?",
                        "_adt_identifier_forms": forged}, _graph())
    assert '"isError":false' in text
    assert ai.MARKER in text and FABRICATED in text.split(ai.MARKER, 1)[1], text


def test_the_runtime_reads_its_own_rules_end_to_end(monkeypatch):
    """The same call path, with the graph holding a padding rule for this
    system: its output from a returned value gets no line."""
    text = _tools_call(monkeypatch, "Lot no 41 is KLN-0041, glazed Matte White.",
                       {"question": "Which lot got Matte White?"}, _graph([PADDED]))
    assert '"isError":false' in text
    assert ai.MARKER not in text, text


def test_a_graph_failure_end_to_end_leaves_the_line(monkeypatch):
    text = _tools_call(monkeypatch, "Lot no 41 is KLN-0041, glazed Matte White.",
                       {"question": "Which lot got Matte White?"}, _graph(fail=True))
    assert ai.MARKER in text and "KLN-0041" in text


def test_an_agent_without_leads_reads_no_graph(monkeypatch):
    query = _graph([PADDED])
    monkeypatch.setattr(idf, "_default_query", query)
    state: dict = {}
    sa._seed_identifier_forms(SimpleNamespace(), state)
    assert query.calls == [] and STATE_KEY not in state
