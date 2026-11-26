# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identifier provenance: the delegation that sends one system's value to a
system the graph reaches only through a third system's field.

The schema here is made up: a shop-floor system keeps a part code on its
jobs, a ledger keeps a design code on its items, and a drawing register keys
its records by drawing number. The graph records one join into the register,
from the ledger's design code. Nothing here names any real deployment.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agents import identifier_provenance as ip
from agents import identity_affinity as ia
from agents import live_concept_mappings as lcm
from agents import orchestrator as orch
from agents import runtime_asgi
from agents import system_agent as sa
from agents import tool_result_capture as trc

PART = "AB-CD-1234"
DESIGN = "DS-1234-V3"
QUESTION = "Which design version was job JOB-77001 built to, and is it approved?"
TOOL = {s: f"{s}___query_{s.replace('-', '_')}" for s in ("shop-floor", "ledger", "drawings")}

def _reasoning(concept: str) -> str:
    """The exact-agreement pass's own reasoning for a join of ``concept``."""
    side = ia.SampledField("a", "t", "c", concept, ("X-1",) * 20)
    return ia._exact_reasoning(side, side, "AA-9", set(range(5)), concept)


JOIN_INTO_DRAWINGS = {"id": "jp-1", "from_system": "ledger", "from_field": "ledger_db.items.design_code",
                      "to_system": "drawings", "to_field": "drawings_db.records.drawing_no",
                      "method": "value_exact_match", "provenance": _reasoning("part")}
JOIN_SHOP_LEDGER = {"id": "jp-2", "from_system": "shop-floor", "from_field": "shop_db.jobs.job_no",
                    "to_system": "ledger", "to_field": "ledger_db.items.job_ref",
                    "method": "value_exact_match", "provenance": _reasoning("job")}


def _rows(join_paths=(JOIN_INTO_DRAWINGS, JOIN_SHOP_LEDGER), identity_rules=(), value_mappings=()):
    """Route-seed rule rows: one per system, each carrying the rules naming it."""
    def mine(rules, a, b, system):
        return [r for r in rules if system in (r.get(a), r.get(b))]
    return [{"system_id": s,
             "join_paths": mine(join_paths, "from_system", "to_system", s),
             "identity_rules": mine(identity_rules, "source_system", "target_system", s),
             "value_mappings": mine(value_mappings, "from_system", "to_system", s)}
            for s in ("shop-floor", "ledger", "drawings")]


def _facts(question=QUESTION, **rows):
    return ip.facts_from(question, _rows(**rows), cap=50)


def _line(text, records):
    return "\n\n" + ip.line_for(text, records)


class _FakeClient:
    """A gateway client whose system agents answer like the real ones: the
    identifier-sources line only when the call asked for it."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def call_tool_async(self, tool_use_id, name, arguments=None,
                              read_timeout_seconds=None, meta=None):
        self.calls.append((name, dict(arguments or {})))
        text, records = self.answers.get(name, ("No rows.", []))
        if (arguments or {}).get(ip.RESERVED_ARG) == ip.REQUESTED and records:
            text += _line(text, records)
        return {"status": "success", "toolUseId": tool_use_id, "content": [{"text": text}]}


SHOP_ANSWER = (f"Job JOB-77001 built part {PART} on line 4.",
               [("jobs.part_code", [PART], ["part"])])
LEDGER_ANSWER = (f"Item for job JOB-77001: design code {DESIGN}.",
                 [("items.design_code", [DESIGN], ["part"])])


def _call(client, system, question):
    return asyncio.run(client.call_tool_async(f"t-{system}", TOOL[system], {"question": question}))


def _guarded(facts, answers=None):
    ip._TURN_FACTS.set(facts)
    fake = _FakeClient(answers or {TOOL["shop-floor"]: SHOP_ANSWER,
                                   TOOL["ledger"]: LEDGER_ANSWER})
    return fake, orch._wrap_gateway_client_with_decline_guard(fake)


@pytest.fixture(autouse=True)
def _clean():
    ip.reset_turn_facts()
    yield
    ip.reset_turn_facts()
    lcm.reset()


def _sent(fake, system):
    return [args for name, args in fake.calls if name == TOOL[system]]


# --------------------------------------------------------------------------- #
# Reproduction: a parallel cross-system turn through the real route-seed read
# --------------------------------------------------------------------------- #
def _seed_query(rule_rows):
    inventory = [{"system_id": r["system_id"], "system_name": r["system_id"]} for r in rule_rows]

    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rule_rows
        return []
    return query


def test_a_source_value_sent_beside_its_joined_system_is_refused_once():
    """Reproduction. The shop-floor answer hands back the part code; the
    model then asks the ledger (for the design code) and the drawing
    register (with the part code) in the same parallel batch. The register's
    call is refused, naming the recorded join from the ledger's design code;
    the ledger's call goes through."""
    orch.build_route_seed(query=_seed_query(_rows()), question=QUESTION)
    fake = _FakeClient({TOOL["shop-floor"]: SHOP_ANSWER, TOOL["ledger"]: LEDGER_ANSWER})
    client = orch._wrap_gateway_client_with_decline_guard(fake)
    _call(client, "shop-floor", "For job JOB-77001, which part was built?")
    refused = _call(client, "drawings", f"List the design versions of part \"{PART}\".")
    _call(client, "ledger", "For job JOB-77001, give the item's design code.")
    assert _sent(fake, "drawings") == []
    assert refused["status"] == "error"
    text = refused["content"][0]["text"]
    assert "ledger ledger_db.items.design_code -> drawings drawings_db.records.drawing_no" in text
    assert "shop-floor jobs.part_code" in text and DESIGN not in text
    assert len(_sent(fake, "ledger")) == 1


def test_the_corrected_call_with_the_joined_value_goes_through():
    fake, client = _guarded(_facts())
    _call(client, "shop-floor", "For job JOB-77001, which part was built?")
    _call(client, "ledger", "For job JOB-77001, give the item's design code.")
    result = _call(client, "drawings", f"List the versions of {DESIGN} (part {PART}).")
    assert result["status"] == "success" and len(_sent(fake, "drawings")) == 1


def test_a_repeat_of_the_refused_call_goes_through_unchanged():
    fake, client = _guarded(_facts())
    _call(client, "shop-floor", "For job JOB-77001, which part was built?")
    ask = f"List the design versions of part {PART}."
    assert _call(client, "drawings", ask)["status"] == "error"
    assert _call(client, "drawings", ask)["status"] == "success"
    assert _sent(fake, "drawings")[0]["question"] == ask


def test_the_refusal_is_independent_of_a_link_between_the_source_and_the_joined_system():
    """The rule never reads whether the graph links the source column to the
    system the join starts from: the same refusal with and without one."""
    linked = {"source_system": "shop-floor", "source_field": "part_code",
              "target_system": "ledger", "target_field": "stock_code", "concept": "part",
              "method": "onboarding-llm"}
    for facts in (_facts(), _facts(identity_rules=(linked,))):
        fake, client = _guarded(facts)
        _call(client, "shop-floor", "For job JOB-77001, which part was built?")
        assert _call(client, "drawings", f"Versions of {PART}?")["status"] == "error"


@pytest.mark.parametrize("source", ["site-a", "site-b", "site-c", "site-d"])
def test_every_source_system_of_the_same_shape_is_treated_alike(source):
    joins = (JOIN_INTO_DRAWINGS, {**JOIN_SHOP_LEDGER, "from_system": source})
    rows = [{"system_id": s, "join_paths": [j for j in joins if s in (j["from_system"],
                                                                    j["to_system"])],
             "identity_rules": [], "value_mappings": []}
            for s in (source, "ledger", "drawings")]
    ip._TURN_FACTS.set(ip.facts_from(QUESTION, rows, cap=50))
    tool = f"{source}___query_{source.replace('-', '_')}"
    fake = _FakeClient({tool: SHOP_ANSWER})
    client = orch._wrap_gateway_client_with_decline_guard(fake)
    asyncio.run(client.call_tool_async("t1", tool, {"question": "Which part?"}))
    assert _call(client, "drawings", f"Versions of {PART}?")["status"] == "error"


# --------------------------------------------------------------------------- #
# Every other shape passes
# --------------------------------------------------------------------------- #
def _refused(facts, ask, answers=None, first=("shop-floor",)):
    fake, client = _guarded(facts, answers)
    for system in first:
        _call(client, system, "For job JOB-77001, what was recorded?")
    return _call(client, "drawings", ask)["status"] == "error"


def test_a_value_in_the_users_question_passes():
    assert not _refused(_facts(question=f"Which versions does part {PART} have?"),
                        f"Versions of {PART}?")


def test_a_prefix_or_family_search_passes():
    assert not _refused(_facts(), f"List every record whose number begins with {PART}.")
    assert not _refused(_facts(), f"List every record matching {PART}* or {PART}-%.")


def test_a_value_the_target_already_returned_passes():
    answers = {TOOL["shop-floor"]: SHOP_ANSWER,
               TOOL["drawings"]: (f"No record is keyed {PART}; it appears as a note.", [])}
    fake, client = _guarded(_facts(), answers)
    _call(client, "drawings", "List the notes recorded this week.")
    _call(client, "shop-floor", "For job JOB-77001, which part was built?")
    assert _call(client, "drawings", f"More about {PART}?")["status"] == "success"


def test_a_value_native_to_the_target_passes():
    answers = {TOOL["drawings"]: ("Records: DR-55-0001.", [("records.drawing_no", ["DR-55-0001"])])}
    assert not _refused(_facts(), "Show revision history of DR-55-0001.", answers,
                        first=("drawings",))


@pytest.mark.parametrize("facts", [
    None,
    _facts(join_paths=(JOIN_SHOP_LEDGER,)),
    _facts(join_paths=(JOIN_INTO_DRAWINGS, JOIN_SHOP_LEDGER,
                       {**JOIN_SHOP_LEDGER, "to_system": "drawings", "id": "jp-3"})),
    _facts(identity_rules=({"source_system": "shop-floor", "source_field": "part_code",
                            "target_system": "drawings", "target_field": "drawing_no"},)),
    _facts(value_mappings=({"from_system": "drawings", "from_field": "records.drawing_no",
                            "to_system": "shop-floor", "to_field": "jobs.part_code"},)),
], ids=["no-facts", "no-join-into-target", "source-system-joined-to-target",
        "identity-rule-link", "value-mapping-link"])
def test_missing_or_linking_graph_facts_pass(facts):
    assert not _refused(facts, f"Versions of {PART}?")


def test_capped_graph_facts_pass():
    rows = _rows()
    rows[-1]["identity_rules"] = [{"id": f"r{i}"} for i in range(50)]
    assert not _refused(ip.facts_from(QUESTION, rows, cap=50), f"Versions of {PART}?")


def test_a_result_without_provenance_passes():
    answers = {TOOL["shop-floor"]: (f"Job built part {PART}.", [])}
    assert not _refused(_facts(), f"Versions of {PART}?", answers)


def test_a_value_from_two_source_columns_passes():
    answers = {TOOL["shop-floor"]: (f"Part {PART}.", [("jobs.part_code", [PART]),
                                                      ("kits.part_ref", [PART])])}
    assert not _refused(_facts(), f"Versions of {PART}?", answers)


def test_a_call_carrying_a_joined_value_already_read_passes():
    fake, client = _guarded(_facts())
    _call(client, "ledger", "Design code for job JOB-77001?")
    _call(client, "shop-floor", "Which part?")
    assert _call(client, "drawings", f"{DESIGN} and {PART}: versions?")["status"] == "success"


def test_an_unknown_tool_or_a_non_delegation_tool_passes():
    fake, client = _guarded(_facts())
    _call(client, "shop-floor", "Which part?")
    other = asyncio.run(client.call_tool_async("t9", "explore_graph", {"question": PART}))
    stray = asyncio.run(client.call_tool_async("t8", "elsewhere___query_elsewhere",
                                               {"question": PART}))
    assert other["status"] == stray["status"] == "success"


# --------------------------------------------------------------------------- #
# The line never reaches a surface a person reads
# --------------------------------------------------------------------------- #
def test_the_model_and_the_activity_trace_never_see_the_line():
    from agents.tool_result_summary import summarize_tool_result
    fake, client = _guarded(_facts())
    result = _call(client, "shop-floor", "Which part?")
    assert ip.LINE_PREFIX not in json.dumps(result)
    assert result["content"][0]["text"] == SHOP_ANSWER[0]
    assert "identifier-sources" not in summarize_tool_result(TOOL["shop-floor"], result)


def test_a_json_answer_stays_the_same_json_after_the_strip():
    body = json.dumps({"rows": [[PART]], "columns": ["part_code"]})
    stripped, records, _text = ip.strip_result(
        {"content": [{"text": body + _line(body, [("jobs.part_code", [PART])])}],
         "structuredContent": {"result": body + _line(body, [("jobs.part_code", [PART])])}})
    assert stripped["content"][0]["text"] == body
    assert stripped["structuredContent"]["result"] == body
    assert records[0]["values"] == [PART]


def test_a_result_without_the_line_is_returned_as_it_was():
    result = {"status": "success", "content": [{"text": "plain"}]}
    assert ip.strip_result(result)[0] is result


# --------------------------------------------------------------------------- #
# System side: the line only when asked, identifier columns only, bounded
# --------------------------------------------------------------------------- #
SCHEMA = {"jobs": {"columns": {"job_no": "text", "part_code": "text", "operator_email": "text",
                               "line": "text"}},
          "kits": {"columns": {"kit_no": "character varying", "job_no": "text"}}}
SQL = ("SELECT j.job_no, j.part_code, j.operator_email, k.kit_no FROM jobs j "
       "JOIN kits k ON k.job_no = j.job_no WHERE j.job_no = 'JOB-77001' LIMIT 500")


def _sql_result(rows):
    payload = {"columns": ["job_no", "part_code", "operator_email", "kit_no"], "rows": rows,
               "row_count": len(rows), "_adt_executed_query": SQL}
    return {"toolUseId": "u1", "status": "success", "content": [{"text": json.dumps(payload)}]}


def _captured(rows) -> dict:
    capture = trc.InvocationToolResultCapture(system_id="shop-floor", physical_schema=SCHEMA)
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    capture._on_after_tool_call(SimpleNamespace(
        selected_tool=None, tool_use={"toolUseId": "u1", "name": "query_public",
                                      "input": {"sql": SQL}},
        invocation_state=state, result=_sql_result(rows), exception=None, retry=False,
        cancel_message=None, agent=SimpleNamespace(messages=[])))
    return state


def _mode_a(answer, state, *, requested=True, write_token=None):
    provenance = ({"_adt_provenance": {"session_id": "sess-1", "query_ref": "q-1",
                                       "write_token": write_token}}
                  if write_token else {})
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "query_shop_floor", "arguments": {
               "question": "Which part?",
               **({ip.RESERVED_ARG: ip.REQUESTED} if requested else {}), **provenance}}}
    runtime_asgi._process_one_mcp_message(msg, "shop-floor")
    return _answered(answer, state), msg["params"]["arguments"]


def _answered(answer, state):
    """The Mode A answer for ``state``, under whatever the runtime popped last."""
    lcm.remember_snapshot("shop-floor", [
        {"concept": "part", "field": "jobs.part_code", "role": "identifier"},
        {"concept": "job", "field": "jobs.job_no", "role": "identifier"},
        {"concept": "operator", "field": "jobs.operator_email", "role": "identifier"},
        {"concept": "kit", "field": "kits.kit_no", "role": "correlate"}])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        mp.setattr(lcm, "_loaded", {})
        return asyncio.run(sa._run_mode_a_query(object(), "shop-floor", "Which part?"))


def test_a_requested_call_ends_with_the_identifier_columns_and_their_values():
    """Reproduction on the system side: a call the orchestrator asked
    carries the line with its cross-system key columns, a correlate-role key
    included, and never a PII-named one. The reserved argument is removed
    before any tool sees it."""
    stated = "Job JOB-77001 built AB-CD-1234 with kit KIT-9001."
    answer, args = _mode_a(stated, _captured([["JOB-77001", PART, "a@example.com", "KIT-9001"]]))
    assert ip.RESERVED_ARG not in args
    body, records = ip.split_line(answer)
    assert body.startswith(stated) and ip.LINE_PREFIX not in body
    assert {(r["table"], r["column"]): r["values"] for r in records} == {
        ("jobs", "job_no"): ["JOB-77001"], ("jobs", "part_code"): [PART],
        ("kits", "kit_no"): ["KIT-9001"]}


def test_an_external_caller_never_gets_the_line():
    answer, _args = _mode_a("Answer.", _captured([["JOB-77001", PART, "a@example.com", "K-1"]]),
                            requested=False)
    assert ip.LINE_PREFIX not in answer


def test_model_text_that_reads_as_the_line_is_made_inert():
    forged = "Answer ZZ-ZZ-9999.\n\n" + ip.LINE_PREFIX + '[{"f":"jobs.part_code","at":[[7,17]]}]'
    answer, _args = _mode_a(forged, _captured([]))
    body, records = ip.split_line(answer)
    assert records == [] and ip.LINE_PREFIX not in body
    assert answer.endswith("\n\n" + ip.ACK_LINE)


def test_the_investigation_trace_never_carries_the_line(monkeypatch):
    """A call that asked for the line, with a session the trace and the
    provenance sidecar are written for: the answer the caller gets ends with
    the line, and nothing either store receives holds it or its marker."""
    from agents import investigation_trace_store, provenance_store
    traces, sidecars = [], []
    monkeypatch.setattr(investigation_trace_store, "put_trace",
                        lambda _s, _q, entry, **_kw: traces.append(entry) or True)
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda _s, entries, **_kw: sidecars.append(entries) or 1)
    monkeypatch.setattr(runtime_asgi, "parse_provenance_context", lambda _v: ("sess-1", "q-1"))
    answer, _args = _mode_a("Job JOB-77001 is complete.",
                            _captured([["JOB-77001", PART, "x@example.com", "K"]]),
                            write_token="0123456789abcdef0123456789abcdef")
    assert ip.LINE_PREFIX in answer and ip.split_line(answer)[1]
    assert traces and sidecars
    for stored in (traces, sidecars):
        text = json.dumps(stored, default=str)
        assert "identifier-sources" not in text and ip.token_digest("JOB-77001") not in text
        assert "Result identifiers" not in text
    # The result-identifiers line (``result_identifiers``) rides right before
    # the identifier-sources line, also after the trace write.
    from agents import result_identifiers
    assert traces[0]["final_answer"] == result_identifiers.split_text(ip.split_line(answer)[0])[0]


def test_the_record_is_bounded_and_skips_values_that_are_not_identifiers():
    rows = [[f"JOB-{i:05d}", "has space", "x@example.com", "K"] for i in range(40)]
    record = _captured(rows)[ip.STATE_KEY]
    assert len(record["jobs.job_no"]) == ip.MAX_VALUES
    assert "jobs.part_code" not in record or record["jobs.part_code"] == []
    assert "jobs.operator_email" not in record
    big = [(f"t{i}.c", [f"V{i}-{j:058d}" for j in range(ip.MAX_VALUES)])
           for i in range(ip.MAX_FIELDS)]
    text = " ".join(v for _f, values in big for v in values)
    assert 0 < len(ip.line_for(text, big)) <= ip.MAX_LINE_CHARS


def test_an_unreadable_projection_records_nothing():
    state: dict = {}
    ip.record_result(state, "SELECT * FROM jobs", {"columns": ["job_no"], "rows": [["JOB-1"]]},
                     SCHEMA)
    assert not state.get(ip.STATE_KEY)


def test_the_route_seed_keeps_the_turn_facts_and_a_failed_read_keeps_none():
    orch.build_route_seed(query=_seed_query(_rows()), question=QUESTION)
    facts = ip.turn_facts()
    assert facts.question == QUESTION and JOIN_INTO_DRAWINGS in facts.join_paths
    ip.reset_turn_facts()
    orch.build_route_seed(query=lambda *_a, **_k: [], question=QUESTION)
    assert ip.turn_facts() is None


def test_key_columns_are_the_ones_join_discovery_accepts_in_either_key_role():
    lcm.remember_snapshot("shop-floor", [
        {"concept": "part", "field": "jobs.part_code", "role": "identifier"},
        {"concept": "kit", "field": "kits.kit_no", "role": "correlate"},
        {"concept": "part", "field": "jobs.part_ref", "role": "correlate"},
        {"concept": "job", "field": "jobs.category", "role": "correlate"},
        {"concept": "job", "field": "jobs.cycle_count", "role": "identifier"},
        {"concept": "job", "field": "jobs.line_speed", "role": "measure"},
        {"concept": "job", "field": "jobs.remark_code", "role": "correlate"},
        {"concept": "job", "field": "jobs.batch_text", "role": "identifier"}])
    keys = lcm.join_key_fields("shop-floor", {"jobs.remark_code": "text",
                                              "jobs.batch_text": "text"})
    assert set(keys) == {("jobs", "part_code"), ("kits", "kit_no"), ("jobs", "part_ref"),
                         ("jobs", "batch_text")}
    assert keys[("jobs", "part_code")] == ("part",) and keys[("kits", "kit_no")] == ("kit",)


def test_only_values_with_a_join_key_shape_are_recorded():
    state: dict = {}
    ip.record_result(state, "SELECT j.job_no FROM jobs j",
                     {"columns": ["job_no"],
                      "rows": [["JOB-77001"], ["12345"], ["SINGLEWORD"], ["L4-M2"]]}, SCHEMA)
    assert state[ip.STATE_KEY]["jobs.job_no"] == ["JOB-77001", "L4-M2"]


def test_a_raw_tools_call_without_proof_gets_no_value_in_the_line():
    """Any caller that reaches the tool can ask for the line: the runtime has
    no verifiable sign that the caller is the orchestrator. So the line holds
    no value at all, only columns and positions in the answer the caller
    already has, and a value the rows held but the answer omits never
    crosses."""
    raw = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "query_shop_floor", "arguments": {"question": "Which part?",
                                                  ip.RESERVED_ARG: ip.REQUESTED}}}).encode()
    forwarded, deny, _rpc = runtime_asgi._pop_identity_from_mcp_body(raw, "shop-floor")
    assert deny is None
    assert ip.RESERVED_ARG not in json.loads(forwarded)["params"]["arguments"]
    rows = [["JOB-77001", PART, "a@example.com", "KIT-9001"]]
    answer = _answered("Job JOB-77001 is complete.", _captured(rows))
    body, records = ip.split_line(answer)
    line = answer[len(body):]
    assert ip.LINE_PREFIX in line
    assert not any(value in line for value in ("JOB-77001", PART, "KIT-9001"))
    assert PART not in answer and "KIT-9001" not in answer
    assert [(r["table"], r["column"], r["values"]) for r in records] == [
        ("jobs", "job_no", ["JOB-77001"])]


def test_a_position_that_does_not_read_back_to_a_key_is_dropped():
    body = "Job JOB-77001 built AB-CD-1234."
    d = ip.token_digest
    line = ip.LINE_PREFIX + json.dumps([{"f": "jobs.job_no",
                                         "at": [[4, 13, d("JOB-77001")], [0, 3, d("Job")],
                                                [4, 13]]},
                                        {"f": "jobs.part_code", "at": [[90, 99, d(PART)]]}],
                                       separators=(",", ":"))
    _text, records = ip.split_line(f"{body}\n\n{line}")
    assert records == [{"table": "jobs", "column": "job_no", "values": ["JOB-77001"],
                        "concepts": []}]


def test_a_value_that_reads_as_personal_data_is_never_recorded():
    """A column whose name gives no hint of personal data can still hold
    it: a value the value-level redaction would change is dropped."""
    from agents.pii import column_looks_pii, redact_pii
    personal = ("123-45-6789", "MBR-123-45-6789", "CARD-4111111111111111")
    assert not column_looks_pii("holder_ref")
    assert all(redact_pii(v) != v for v in personal)
    schema = {"kits": {"columns": {"holder_ref": "character varying"}}}
    state: dict = {}
    ip.record_result(state, "SELECT k.holder_ref FROM kits k",
                     {"columns": ["holder_ref"], "rows": [[v] for v in (*personal, "KIT-9001")]},
                     schema)
    assert state[ip.STATE_KEY]["kits.holder_ref"] == ["KIT-9001"]


# --------------------------------------------------------------------------- #
# A position counts only for the exact token it was written for
# --------------------------------------------------------------------------- #
_BOUND_BODY = "Part AA-BB-1111 uses kit CC-DD-2222."
_BOUND_FIELDS = [("jobs.part_code", ["AA-BB-1111"]), ("kits.kit_no", ["CC-DD-2222"])]


def _read_with(body: str) -> list:
    """The records read back when ``body`` replaces the text the line was written for."""
    line = ip.line_for(_BOUND_BODY, _BOUND_FIELDS)
    _text, records = ip.split_line(f"{body}\n\n{line}")
    return {(r["table"], r["column"]): r["values"] for r in records or ()}


def test_positions_read_back_on_the_text_they_were_written_for():
    assert _read_with(_BOUND_BODY) == {("jobs", "part_code"): ["AA-BB-1111"],
                                       ("kits", "kit_no"): ["CC-DD-2222"]}


def test_a_stale_position_on_another_valid_key_is_dropped():
    assert _read_with(_BOUND_BODY.replace("AA-BB-1111", "EE-FF-3333")) == {
        ("kits", "kit_no"): ["CC-DD-2222"]}


def test_positions_on_reordered_text_are_dropped():
    assert _read_with("Part CC-DD-2222 uses kit AA-BB-1111.") == {}


def test_positions_on_normalized_text_are_dropped():
    assert _read_with(_BOUND_BODY.lower()) == {}
    assert _read_with(_BOUND_BODY.replace("-", "_")) == {}


def test_positions_on_text_with_something_prepended_are_dropped():
    assert _read_with("Note: " + _BOUND_BODY) == {}
    assert _read_with("Part AA-BB-1111 uses kit " + _BOUND_BODY[5:]) == {
        ("jobs", "part_code"): ["AA-BB-1111"]}
