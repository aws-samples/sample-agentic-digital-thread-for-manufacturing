# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The engine crosses a stored pairing of two identifier forms itself.

Measured on a workshop seat (2026-11-06): one system kept a concept under
one identifier in a consumption table and another in a dispatch table; a
second system paired the two per item. Asked who received goods from every
unit that drew one material lot, the holder found the other units in the
first form, could not look their dispatch lines up (wrong form, or a
``not_connected`` decline), and the orchestrator model was left to fetch the
other form from the pairing system and ask again. It did so in about half
the asks, so half the answers covered only the unit asked about.

Pinned here on a schema unlike the workshop's (systems ledger-x and cell-x,
tables consumption_log, dispatch_lines, jobs and builds, prefixes JB- and
BLD), through the turn's real gateway delegation stack:

* a holder answer with a typed ``not_connected`` decline about the line's
  concept, or a wrong-form IDENTIFIER-MISS block, makes the engine ask the
  pairing system for the other form of every listed-shape value (a reserved
  argument holding only the stored JoinPath id and the values, no model),
  then ask the holder again once with the pairs; the result carries one note;
* a round that starts and does not finish complete leaves the delegation's
  own result as it was, the same object: one value without a counterpart, a
  value or a counterpart that pairs twice (one peer, two peers or the other
  direction), a read that was not whole, a failed or late re-ask;
* everything else is byte-identical: no signal, another concept, a pairing
  the holder does itself, no pairing evidence, enforce mode, and a line about
  a person concept or a concept the catalog does not know;
* bounded: rounds, pairing calls per round and per turn, forms per line, and
  one deadline from the turn's ceiling; the reads of one round run at once;
* the model can never set the reserved argument: the outermost gateway
  wrapper strips it from every call the model makes.

Fully offline.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

import agents.identifier_provenance as ip
import agents.orchestrator as orch
import agents.route_facts as rf
from agents import form_crossing as fc
from agents import form_pairing as fp
from agents import identity_affinity as ia
from agents.call_delivery import CallDelivery
from agents.call_delivery import record as record_delivery

RESERVED = fp.RESERVED_ARG
HOLDER, PAIRER, OTHER_PAIRER = "ledger-x", "cell-x", "cell-y"
HOLDER_TOOL = "ledger-x___query_ledger_x"
PAIRER_TOOL, OTHER_TOOL = "cell-x___query_cell_x", "cell-y___query_cell_y"
JOB_SHAPE, BUILD_SHAPE = "AA-9999-999", "AAA99999"
FIELDS = ["jobs.job_code", "builds.build_code"]


#: The line's concept: the engine crosses only concepts on the catalog's
#: allowlist (``catalog.CROSSABLE_CONCEPTS``).
CONCEPT = "production-run"


def _reasoning(source, target, shape, concept=CONCEPT):
    return (f"40 exact values of {source} exist in {HOLDER} {target} (membership-probed); "
            f"both columns shape {shape}; concept {concept}; 50 and 90 distinct values sampled")


def _rule(rid, pairer, peer_field, peer_table, holder_table, shape, concept=CONCEPT):
    return {"id": rid, "concept": concept, "source_system": pairer, "source_field": peer_field,
            "target_system": HOLDER, "target_field": "unit_ref", "source_pattern": "",
            "target_pattern": "", "method": ia.EXACT_METHOD, "confidence": 0.85,
            "provenance": _reasoning(f"{peer_table}.{peer_field}",
                                     f"{holder_table}.unit_ref", shape, concept)}


def _exact_jp(rule, peer_table, holder_table):
    return {"id": "jp-" + rule["id"], "from_system": rule["source_system"],
            "from_field": f"s_cell.{peer_table}.{rule['source_field']}", "to_system": HOLDER,
            "to_field": f"s_led.{holder_table}.unit_ref", "transform": "identity",
            "method": ia.EXACT_METHOD, "confidence": 0.85, "provenance": rule["provenance"]}


def _pair_jp(system):
    return {"id": f"jp-int-{system}-jobs-builds", "from_system": system,
            "from_field": "s_cell.jobs.build_id", "to_system": system,
            "to_field": "s_cell.builds.build_id", "transform": "direct",
            "method": "foreign_key_match", "basis": "declared_foreign_key"}


def _world(pairers=(PAIRER,)):
    rules, jps = [], []
    for system in pairers:
        job = _rule(f"ir-{system}-job", system, "job_code", "jobs", "consumption_log", JOB_SHAPE)
        build = _rule(f"ir-{system}-build", system, "build_code", "builds", "dispatch_lines",
                      BUILD_SHAPE)
        rules += [job, build]
        jps += [_exact_jp(job, "jobs", "consumption_log"),
                _exact_jp(build, "builds", "dispatch_lines"), _pair_jp(system)]
    return rules, jps


RULES, JPS = _world()
PAIR_JP = JPS[2]

USER_QUESTION = ("Which receivers got goods from production run BLD00412 or from any other "
                 "run built from the same coil? Give the pieces, the value and the total.")
DELEGATED = ("Find the coil consumed by unit BLD00412, every other unit that consumed that "
             "coil, and the receivers, pieces and value dispatched for each of those units.")
#: What the pairing system's rows hold (job code, build code).
ROWS = [("JB-4471-001", "BLD00412"), ("JB-4471-002", "BLD00413"), ("JB-4472-005", "BLD00419")]

FIRST = ("Coil CL-77 was consumed by JB-4471-001, JB-4471-002 and JB-4472-005. Dispatch "
         "lines for BLD00412: receiver R-1, 4 pieces, $400. The other units are tracked under "
         "another identifier here and I cannot link them to dispatch lines.")
DECLINE = f"\n\n[engine] declined: reason_kind=not_connected; concept={CONCEPT}"
MISS = ("\n\n[engine] Identifier forms not paired: this call looked up a value in a column "
        "that holds another form of identifier.\n\n```\nIDENTIFIER-MISS:\ntoken: BLD00412\n"
        "looked_in: consumption_log.unit_ref\nmy_format: consumption_log.unit_ref values are "
        "shaped like JB-4400-100\ncontext: BLD00412 has the form of dispatch_lines.unit_ref\n```")
SECOND = ("Receivers for the coil's units: R-1 4 pieces $400 (BLD00412), R-2 6 pieces $600 "
          "(BLD00413), R-3 2 pieces $200 (BLD00419). Total 12 pieces, $1,200.")


def _result(text, status="success"):
    return {"status": status, "toolUseId": "t", "content": [{"text": text}]}


def _reply(**over):
    body = {"engine": "pair_forms", "v": 2, "status": "ok", "fields": FIELDS, "pairs": [],
            "cut": False, "dropped": 0, **over}
    return _result(json.dumps(body))


def closure(rows, values):
    """The pairing read's rows: every row that shares a value with a row of a
    requested value (``form_pairing.statement``)."""
    asked = set(values)
    matched = [r for r in rows if r[0] in asked or r[1] in asked]
    left, right = {r[0] for r in matched}, {r[1] for r in matched}
    return [list(r) for r in rows if r[0] in left or r[1] in right]


class _Gateway:
    """A gateway client: the holder's answers queued in order, each pairing
    system answering reserved-argument requests from its rows."""

    def __init__(self, holder_answers, rows=None, pairing=None, delay=0.0, holder_delay=0.0):
        self.holder_answers = list(holder_answers)
        self.rows = {PAIRER: ROWS} if rows is None else rows
        #: ``delay`` slows every pairing read, ``holder_delay`` every holder re-ask.
        self.pairing, self.delay, self.holder_delay = pairing, delay, holder_delay
        self.calls = []
        self.lock = threading.Lock()
        self.list_tools_sync = lambda *a, **k: []

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            record_delivery(CallDelivery.SENT)
            with self.lock:
                self.calls.append((name, dict(arguments or {}), tool_use_id))
                reask = name == HOLDER_TOOL and len(self._holder()) > 1
            if RESERVED in (arguments or {}):
                await asyncio.sleep(self.delay)
                return self._pair(name, arguments)
            if reask:
                await asyncio.sleep(self.holder_delay)
            if name == HOLDER_TOOL and self.holder_answers:
                with self.lock:
                    return self.holder_answers.pop(0)
            return _result("Nothing else.")

        self.call_tool_async = _call

    def _holder(self):
        return [c for c in self.calls if c[0] == HOLDER_TOOL]

    def _pair(self, name, arguments):
        if self.pairing is not None:
            return self.pairing(name, arguments)
        system = name.split("___", 1)[0]
        request = json.loads(arguments[RESERVED])
        return _reply(pairs=closure(self.rows.get(system, []), request["values"]))


@pytest.fixture(autouse=True)
def _clean_turn(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    rf.reset_turn()
    ip.reset_turn_facts()
    yield
    rf.reset_turn()
    ip.reset_turn_facts()


def _facts(rules=RULES, jps=JPS, question=USER_QUESTION, systems=(HOLDER, PAIRER)):
    rows = [{"system_id": s,
             "identity_rules": [r for r in rules if s in (r["source_system"], r["target_system"])],
             "join_paths": [j for j in jps if s in (j["from_system"], j["to_system"])],
             "value_mappings": []} for s in systems]
    ip.remember_turn_facts(question, rows, orch._ROUTE_SEED_RULE_CAP)


def _client(gateway, **facts):
    _facts(**facts)
    return orch._wrap_gateway_client_with_delegation_context(gateway)


def _delegate(client, question=DELEGATED, tid="t1"):
    return asyncio.run(client.call_tool_async(tid, HOLDER_TOOL, {"question": question}))


def _ask(holder_answers, question=DELEGATED, gateway=None, **facts):
    gateway = gateway or _Gateway(holder_answers)
    return gateway, _delegate(_client(gateway, **facts), question)


def _text(result):
    return "\n".join(b["text"] for b in result["content"] if isinstance(b.get("text"), str))


def _names(gateway):
    return [name for name, _args, _tid in gateway.calls]


def _holder_calls(gateway):
    return [args for name, args, _tid in gateway.calls if name == HOLDER_TOOL]


def _pairing_requests(gateway):
    return [json.loads(args[RESERVED]) for _n, args, _t in gateway.calls if RESERVED in args]


def _unchanged(original, result):
    assert result is original
    assert json.dumps(result, sort_keys=True) == json.dumps(original, sort_keys=True)


# --------------------------------------------------------------------------- #
# The reproduction
# --------------------------------------------------------------------------- #
def test_a_holder_decline_is_crossed_by_the_engine_and_the_holder_is_asked_again():
    """Before: one call, and the holder's partial answer went back to the
    model, which had to fetch the other form and ask again itself."""
    gateway, result = _ask([_result(FIRST + DECLINE), _result(SECOND)])
    assert _names(gateway) == [HOLDER_TOOL, PAIRER_TOOL, HOLDER_TOOL]
    reask = _holder_calls(gateway)[-1]["question"]
    assert reask.startswith(DELEGATED)
    for job, build in ROWS:
        assert f"- {job} = {build}" in reask
    assert "consumption_log.unit_ref = dispatch_lines.unit_ref:" in reask
    text = _text(result)
    assert text.startswith(SECOND)
    assert "[engine] Identifier forms crossed:" in text and "asked ledger-x again once" in text


def test_a_wrong_form_miss_block_is_crossed_the_same_way():
    gateway, result = _ask([_result(FIRST + MISS), _result(SECOND)])
    assert _names(gateway) == [HOLDER_TOOL, PAIRER_TOOL, HOLDER_TOOL]
    assert _text(result).startswith(SECOND)


def test_the_pairing_call_carries_only_the_stored_join_and_the_values():
    gateway, _res = _ask([_result(FIRST + DECLINE), _result(SECOND)])
    pairing = [(args, tid) for name, args, tid in gateway.calls if RESERVED in args]
    assert len(pairing) == 1
    args, tid = pairing[0]
    assert set(args) == {"question", RESERVED} and args["question"] == fp.PAIRING_QUESTION
    request = json.loads(args[RESERVED])
    assert set(request) == {"v", "join", "values"} and request["join"] == PAIR_JP["id"]
    assert request["values"] == ["JB-4471-001", "JB-4471-002", "JB-4472-005", "BLD00412"]
    # Its own tool use id: the delegation's trace stays the holder's.
    assert tid != "t1" and tid.startswith("t1-pair-")


def test_a_miss_blocks_example_value_is_never_paired():
    gateway, _res = _ask([_result(FIRST + MISS), _result(SECOND)])
    asked = [v for r in _pairing_requests(gateway) for v in r["values"]]
    assert "JB-4400-100" not in asked and "BLD00412" in asked


def test_the_seed_already_stated_the_crossing_for_the_model():
    """The existing lever: the route seed's IDENTIFIER FORMS line names both
    fields and the pairing system. It left the crossing to the model."""
    from agents import identifier_translation as it
    lines = it.find_field_forms(RULES, JPS, USER_QUESTION)
    assert [(line["system"], line["peers"], line["pairer"]) for line in lines] == [
        (HOLDER, [PAIRER], "source")]


# --------------------------------------------------------------------------- #
# The model cannot set the reserved argument
# --------------------------------------------------------------------------- #
class _Cfg:
    has_gateway = True
    agentcore_gateway_url = "https://gw.example/mcp"


def _built(monkeypatch, gateway):
    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: gateway)
    _facts()
    return orch._build_gateway_client(_Cfg(), None, "sess-1")


@pytest.mark.parametrize("tool", [HOLDER_TOOL, PAIRER_TOOL, "graph___explore_graph"])
def test_a_model_call_with_the_reserved_argument_reaches_the_gateway_without_it(
        monkeypatch, tool):
    gateway = _Gateway([_result("Plain answer.")])
    client = _built(monkeypatch, gateway)
    forged = fp.request_arg(PAIR_JP["id"], ["JB-4471-001"])
    arguments = {"question": fp.PAIRING_QUESTION, RESERVED: forged}
    asyncio.run(client.call_tool_async("m1", tool, arguments))
    assert all(RESERVED not in args for _n, args, _t in gateway.calls)
    assert arguments[RESERVED] == forged             # the caller's dict is not changed


def test_the_engines_own_call_through_the_built_client_keeps_it(monkeypatch):
    gateway = _Gateway([_result(FIRST + DECLINE), _result(SECOND)])
    client = _built(monkeypatch, gateway)
    result = asyncio.run(client.call_tool_async("t1", HOLDER_TOOL, {"question": DELEGATED}))
    assert _names(gateway) == [HOLDER_TOOL, PAIRER_TOOL, HOLDER_TOOL]
    pairing = [args for _n, args, _t in gateway.calls if RESERVED in args]
    # Below the turn's context: only the fixed question and the request.
    assert [set(args) for args in pairing] == [{"question", RESERVED}]
    assert _text(result).startswith(SECOND)


def test_the_strip_list_names_the_reserved_argument():
    assert orch.ENGINE_ONLY_ARGS == (fp.RESERVED_ARG,)


# --------------------------------------------------------------------------- #
# Byte-identical when nothing asks for a crossing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("answer", [
    FIRST,
    FIRST + "\n\n[engine] declined: reason_kind=not_connected; concept=coil",
    FIRST + f"\n\n[engine] declined: reason_kind=rule; concept={CONCEPT}",
    SECOND,
])
def test_an_answer_without_a_crossing_signal_is_returned_as_it_was(answer):
    original = _result(answer)
    gateway, result = _ask([original])
    assert _names(gateway) == [HOLDER_TOOL]
    _unchanged(original, result)


@pytest.mark.parametrize("concept", ["personnel", "operator", "customer", "shift",
                                     "engineering-change", "purchase-order", "spare-part",
                                     "badge-ref"])
def test_a_line_about_a_concept_off_the_allowlist_is_never_crossed(concept):
    """The catalog's allowlist (``catalog.CROSSABLE_CONCEPTS``), not names: a
    forms line about people, customers, shifts, engineering changes, purchase
    orders or parts, or about a concept the catalog does not know, is not
    crossed, and its decline goes back as it was."""
    rules = [{**r, "concept": concept,
              "provenance": r["provenance"].replace(f"concept {CONCEPT}", f"concept {concept}")}
             for r in RULES]
    # The question names the concept, so the seed carries the forms line.
    question = f"{USER_QUESTION} List the {concept.replace('-', ' ')} records too."
    from agents import identifier_translation as it
    assert it.find_field_forms(rules, JPS, question)
    original = _result(FIRST + DECLINE.replace(CONCEPT, concept))
    gateway = _Gateway([original, _result(SECOND)])
    result = _delegate(_client(gateway, rules=rules, question=question))
    assert _names(gateway) == [HOLDER_TOOL]
    _unchanged(original, result)


def test_only_a_line_about_an_allowlisted_concept_is_crossable():
    line = {"system": HOLDER, "pairer": "source", "pairing": ("a.k = b.k",), "concept": CONCEPT,
            "fields": [("t0.unit_ref", "tab0.code", "A0-999"),
                       ("t1.unit_ref", "tab1.code", "A1-999")]}
    assert fc._crossable(line, HOLDER) is not None
    for concept in ("work-order", "batch", "lot-number", "production-order"):
        assert fc._crossable({**line, "concept": concept}, HOLDER) is not None
    for concept in ("personnel", "operator", "customer", "shift", "engineering-change",
                    "supplier", "purchase-order", "sales-order", "shipment", "spare-part",
                    "material", "product", "asset", "equipment-id", "badge-ref", "", None):
        assert fc._crossable({**line, "concept": concept}, HOLDER) is None


@pytest.mark.parametrize("fields", [
    [("t0.assigned_to", "tab0.code", "A0-999"), ("t1.unit_ref", "tab1.code", "A1-999")],
    [("t0.unit_ref", "tab0.code", "A0-999"), ("t1.unit_ref", "tab1.approved_by", "A1-999")],
    [("t0.assignedTo", "tab0.code", "A0-999"), ("t1.unit_ref", "tab1.code", "A1-999")],
    [("t0.unit_ref", "tab0.supplierId", "A0-999"), ("t1.unit_ref", "tab1.code", "A1-999")],
    [("t0.unit_ref", "tab0.code", "A0-999"), ("t1.vendor_id", "tab1.code", "A1-999")],
    [("t0.unit_ref", "tab0.code", "A0-999"), ("t1.unit_ref", "tab1.customerId", "A1-999")],
])
def test_a_line_with_a_person_valued_field_is_not_crossed(fields):
    """An assignee, approver, customer, supplier or vendor field under an
    allowlisted concept, in any spelling: the catalog's person-valued field
    names (``catalog.PERSON_VALUED_FIELDS``), matched as whole words."""
    line = {"system": HOLDER, "pairer": "source", "pairing": ("a.k = b.k",),
            "concept": "work-order", "fields": fields}
    assert fc._crossable(line, HOLDER) is None


def test_a_miss_block_on_a_field_the_line_does_not_list_is_no_signal():
    block = MISS.replace("looked_in: consumption_log.unit_ref", "looked_in: coils.coil_ref")
    gateway, _res = _ask([_result(FIRST + block)])
    assert _names(gateway) == [HOLDER_TOOL]


def test_no_pairing_evidence_means_no_crossing():
    gateway, _res = _ask([_result(FIRST + DECLINE)], jps=JPS[:2])
    assert _names(gateway) == [HOLDER_TOOL]


def test_a_column_name_guess_is_no_pairing_evidence():
    guessed = {**PAIR_JP, "basis": "column_name_pattern"}
    gateway, _res = _ask([_result(FIRST + DECLINE)], jps=[*JPS[:2], guessed])
    assert _names(gateway) == [HOLDER_TOOL]


def test_a_holder_that_pairs_its_own_forms_is_not_crossed():
    own = {"id": "jp-int-ledger-x", "from_system": HOLDER,
           "from_field": "s_led.consumption_log.dispatch_id",
           "to_system": HOLDER, "to_field": "s_led.dispatch_lines.dispatch_id",
           "transform": "direct", "method": "foreign_key_match", "basis": "declared"}
    gateway, _res = _ask([_result(FIRST + DECLINE)], jps=[*JPS[:2], own])
    assert _names(gateway) == [HOLDER_TOOL]


def test_a_line_whose_forms_share_one_source_table_is_not_crossed():
    """One row holds both forms: there is no stored JoinPath the pairing
    system could be asked through, so the engine does not cross it."""
    rules = [_rule("ir-j", PAIRER, "job_code", "units", "consumption_log", JOB_SHAPE),
             _rule("ir-b", PAIRER, "build_code", "units", "dispatch_lines", BUILD_SHAPE)]
    jps = [_exact_jp(rules[0], "units", "consumption_log"),
           _exact_jp(rules[1], "units", "dispatch_lines")]
    gateway, _res = _ask([_result(FIRST + DECLINE)], rules=rules, jps=jps)
    assert _names(gateway) == [HOLDER_TOOL]


def test_a_turn_whose_question_does_not_name_the_concept_gets_no_crossing():
    gateway = _Gateway([_result(FIRST + DECLINE)])
    _delegate(_client(gateway, question="Which receivers got goods from coil CL-77?"))
    assert _names(gateway) == [HOLDER_TOOL]


def test_a_turn_with_no_route_seed_facts_is_left_alone():
    gateway = _Gateway([_result(FIRST + DECLINE)])
    _delegate(orch._wrap_gateway_client_with_delegation_context(gateway))
    assert _names(gateway) == [HOLDER_TOOL]


def test_enforce_mode_is_left_alone(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    original = _result(FIRST + DECLINE)
    gateway, result = _ask([original])
    assert _names(gateway) == [HOLDER_TOOL]
    _unchanged(original, result)


def test_a_delegation_to_the_pairing_system_itself_is_not_crossed():
    gateway = _Gateway([])
    client = _client(gateway)
    asyncio.run(client.call_tool_async("t1", PAIRER_TOOL, {"question": DELEGATED + DECLINE}))
    assert _names(gateway) == [PAIRER_TOOL]


# --------------------------------------------------------------------------- #
# A round that is not complete leaves the result as it was
# --------------------------------------------------------------------------- #
def _no_reask(gateway, original, result):
    assert len(_holder_calls(gateway)) == 1
    _unchanged(original, result)


def test_four_of_five_paired_is_not_a_crossing():
    """The measured incomplete-result class: one unit of the lot has no
    counterpart, so the holder is not asked again with four."""
    first = _result("Coil CL-77 was consumed by JB-4471-001, JB-4471-002, JB-4472-005, "
                    "JB-4472-006 and JB-4473-001." + DECLINE)
    rows = [*ROWS, ("JB-4472-006", "BLD00420")]          # JB-4473-001 has no row
    gateway = _Gateway([first, _result(SECOND)], rows={PAIRER: rows})
    _gw, result = _ask([], gateway=gateway)
    _no_reask(gateway, first, result)


def test_conflicting_pairing_systems_are_not_a_crossing():
    rules, jps = _world((PAIRER, OTHER_PAIRER))
    first = _result(FIRST + DECLINE)
    rows = {PAIRER: ROWS, OTHER_PAIRER: [("JB-4471-002", "BLD00999")]}
    gateway = _Gateway([first, _result(SECOND)], rows=rows)
    _gw, result = _ask([], gateway=gateway, rules=rules, jps=jps,
                       systems=(HOLDER, PAIRER, OTHER_PAIRER))
    assert sorted(_names(gateway)[1:]) == [PAIRER_TOOL, OTHER_TOOL]
    _no_reask(gateway, first, result)


def test_two_pairing_systems_that_each_hold_some_items_cross_together():
    rules, jps = _world((PAIRER, OTHER_PAIRER))
    rows = {PAIRER: ROWS[:2], OTHER_PAIRER: ROWS[2:]}
    gateway = _Gateway([_result(FIRST + DECLINE), _result(SECOND)], rows=rows)
    _gw, result = _ask([], gateway=gateway, rules=rules, jps=jps,
                       systems=(HOLDER, PAIRER, OTHER_PAIRER))
    assert _text(result).startswith(SECOND) and "cell-x, cell-y" in _text(result)


def test_a_counterpart_that_pairs_with_another_item_is_not_a_crossing():
    """The other direction: the asked unit's counterpart also pairs with a
    unit nobody asked about. The one-hop read returns that row too."""
    first = _result(FIRST + DECLINE)
    rows = [*ROWS, ("JB-4479-009", "BLD00413")]
    gateway = _Gateway([first, _result(SECOND)], rows={PAIRER: rows})
    _gw, result = _ask([], gateway=gateway)
    _no_reask(gateway, first, result)


def test_a_value_paired_twice_is_not_a_crossing():
    first = _result(FIRST + DECLINE)
    rows = [*ROWS, ("JB-4471-002", "BLD00414")]
    gateway = _Gateway([first, _result(SECOND)], rows={PAIRER: rows})
    _gw, result = _ask([], gateway=gateway)
    _no_reask(gateway, first, result)


@pytest.mark.parametrize("reply", [
    {"status": "refused", "reason": "no_stored_pairing"},
    {"status": "unsupported"},
    {"status": "failed", "error": "OperationalError"},
    {"cut": True},
    {"dropped": 1},
    {"fields": ["jobs.job_code", "builds.other_code"]},
    {"engine": "other"},
])
def test_a_pairing_answer_that_is_not_a_whole_read_of_the_two_fields_pairs_nothing(reply):
    first = _result(FIRST + DECLINE)
    pairs = [list(r) for r in ROWS]
    gateway = _Gateway([first, _result(SECOND)],
                       pairing=lambda _n, _a: _reply(pairs=pairs, **reply))
    _gw, result = _ask([], gateway=gateway)
    _no_reask(gateway, first, result)


def test_a_reply_naming_the_fields_the_other_way_round_is_read_the_other_way():
    flipped = [[b, a] for a, b in ROWS]
    gateway = _Gateway([_result(FIRST + DECLINE), _result(SECOND)],
                       pairing=lambda _n, _a: _reply(fields=FIELDS[::-1], pairs=flipped))
    _gw, result = _ask([], gateway=gateway)
    assert "- JB-4471-002 = BLD00413" in _holder_calls(gateway)[-1]["question"]
    assert _text(result).startswith(SECOND)


def test_a_failed_first_reask_returns_the_original_result_unchanged():
    first = _result(FIRST + DECLINE)
    gateway, result = _ask([first, _result("Busy.", status="error")])
    assert len(_holder_calls(gateway)) == 2
    _unchanged(first, result)


def _second_round_world():
    more = _result("Also unit JB-4473-009 drew that coil; its dispatch lines are not linked."
                   + DECLINE)
    rows = [*ROWS, ("JB-4473-009", "BLD00430")]
    return more, rows


def test_a_failed_second_reask_returns_the_original_result_unchanged():
    first = _result(FIRST + DECLINE)
    more, rows = _second_round_world()
    gateway = _Gateway([first, more, _result("Busy.", status="error")], rows={PAIRER: rows})
    _gw, result = _ask([], gateway=gateway)
    assert len(_holder_calls(gateway)) == 3
    _unchanged(first, result)


def test_an_incomplete_second_round_returns_the_original_result_unchanged():
    first = _result(FIRST + DECLINE)
    more, _rows = _second_round_world()                  # JB-4473-009 has no row
    gateway = _Gateway([first, more, _result(SECOND)])
    _gw, result = _ask([], gateway=gateway)
    assert len(_holder_calls(gateway)) == 2
    _unchanged(first, result)


# --------------------------------------------------------------------------- #
# Bounded
# --------------------------------------------------------------------------- #
def test_a_second_round_pairs_only_values_the_first_did_not():
    more, rows = _second_round_world()
    gateway = _Gateway([_result(FIRST + DECLINE), more, _result(SECOND)], rows={PAIRER: rows})
    _gw, result = _ask([], gateway=gateway)
    holder = _holder_calls(gateway)
    assert len(holder) == 3
    requests = _pairing_requests(gateway)
    assert requests[1]["values"] == ["JB-4473-009"]
    assert "- JB-4473-009 = BLD00430" in holder[-1]["question"]
    assert "- JB-4471-001 = BLD00412" in holder[-1]["question"]
    assert "asked ledger-x again 2 times" in _text(result)


def test_the_turn_budget_bounds_the_rounds():
    delegations = fc.MAX_ROUNDS_PER_TURN + 1
    answers = [_result(text) for _ in range(delegations) for text in (FIRST + DECLINE, SECOND)]
    gateway = _Gateway(answers)
    client = _client(gateway)
    results = [_delegate(client, tid=f"t{n}") for n in range(delegations)]
    assert len(_holder_calls(gateway)) == delegations + fc.MAX_ROUNDS_PER_TURN
    assert _text(results[-1]) == FIRST + DECLINE


def test_more_pairing_calls_than_one_round_allows_is_no_crossing():
    peers = [f"cell-{n}" for n in range(fc.MAX_PAIRING_CALLS_PER_ROUND + 1)]
    rules, jps = _world(peers)
    first = _result(FIRST + DECLINE)
    gateway = _Gateway([first], rows={p: ROWS for p in peers})
    _gw, result = _ask([], gateway=gateway, rules=rules, jps=jps, systems=(HOLDER, *peers))
    assert _names(gateway) == [HOLDER_TOOL]
    _unchanged(first, result)


def test_the_turns_pairing_call_cap_stops_further_crossings(monkeypatch):
    monkeypatch.setattr(fc, "MAX_PAIRING_CALLS_PER_TURN", 1)
    gateway = _Gateway([_result(FIRST + DECLINE), _result(SECOND), _result(FIRST + DECLINE)])
    client = _client(gateway)
    _delegate(client, tid="t1")
    later = _delegate(client, tid="t2")
    assert _names(gateway) == [HOLDER_TOOL, PAIRER_TOOL, HOLDER_TOOL, HOLDER_TOOL]
    assert _text(later) == FIRST + DECLINE


def test_a_line_with_more_forms_than_the_cap_is_not_crossable():
    line = {"system": HOLDER, "pairer": "source", "pairing": ("a.k = b.k",), "concept": CONCEPT,
            "fields": [(f"t{n}.unit_ref", f"tab{n}.code", f"A{n}-999") for n in range(4)]}
    assert fc._crossable(line, HOLDER) is None
    line["fields"] = line["fields"][:3]
    assert fc._crossable(line, HOLDER) is not None


def test_a_turn_too_close_to_its_ceiling_does_not_start_a_round(monkeypatch):
    gateway = _Gateway([_result(FIRST + DECLINE)])
    client = _client(gateway)
    late = fc.TURN_BUDGET_SECONDS - fc.ANSWER_RESERVE_SECONDS \
        - (fc.PAIRING_SECONDS + fc.REASK_MIN_SECONDS) + 1
    start = fc._monotonic()
    monkeypatch.setattr(fc, "_monotonic", lambda: start + late)
    _delegate(client)
    assert _names(gateway) == [HOLDER_TOOL]


def _short_turn(monkeypatch, pairing=0.1):
    monkeypatch.setattr(fc, "TURN_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(fc, "ANSWER_RESERVE_SECONDS", 0.0)
    monkeypatch.setattr(fc, "REASK_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fc, "PAIRING_SECONDS", pairing)


def test_slow_pairing_reads_leave_the_result_as_it_was(monkeypatch):
    _short_turn(monkeypatch, pairing=0.05)
    first = _result(FIRST + DECLINE)
    gateway = _Gateway([first, _result(SECOND)], delay=0.3)
    _gw, result = _ask([], gateway=gateway)
    _no_reask(gateway, first, result)


def test_a_reask_past_the_deadline_leaves_the_result_as_it_was(monkeypatch):
    _short_turn(monkeypatch)
    first = _result(FIRST + DECLINE)
    gateway = _Gateway([first, _result(SECOND)], holder_delay=1.0)
    _gw, result = _ask([], gateway=gateway)
    assert len(_holder_calls(gateway)) == 2
    _unchanged(first, result)


def test_the_reads_of_one_round_run_at_once(monkeypatch):
    """Two pairing systems, each 0.2 s: inside a 0.3 s window only if they
    run concurrently."""
    _short_turn(monkeypatch, pairing=0.3)
    rules, jps = _world((PAIRER, OTHER_PAIRER))
    gateway = _Gateway([_result(FIRST + DECLINE), _result(SECOND)],
                       rows={PAIRER: ROWS[:2], OTHER_PAIRER: ROWS[2:]}, delay=0.2)
    _gw, result = _ask([], gateway=gateway, rules=rules, jps=jps,
                       systems=(HOLDER, PAIRER, OTHER_PAIRER))
    assert _text(result).startswith(SECOND)


def test_the_note_and_the_question_block_are_generic_and_dash_free():
    for text in (fc.QUESTION_HEADER, fc.QUESTION_FOOTER, fc.NOTE_PREFIX, fc._CROSSED,
                 fp.PAIRING_QUESTION):
        assert "\u2014" not in text and "\u2013" not in text
        for literal in ("batch", "run", "work order", "shipment", "lot"):
            assert literal not in text.lower().replace("runtime", "")


# --------------------------------------------------------------------------- #
# The decline guard sees the delegation's final answer only
# --------------------------------------------------------------------------- #
def test_a_crossed_decline_does_not_lock_the_holder_out_for_the_turn():
    gateway = _Gateway([_result(FIRST + DECLINE), _result(SECOND),
                        _result("Unit BLD00412 shipped on day 3.")])
    client = _client(gateway)
    _delegate(client, tid="t1")
    later = _delegate(client, "When did unit BLD00412 ship?", tid="t2")
    assert later["status"] == "success" and _text(later).startswith("Unit BLD00412 shipped")


def test_a_reask_that_still_declines_is_recorded_like_any_decline():
    gateway = _Gateway([_result(FIRST + DECLINE), _result(FIRST + DECLINE)])
    client = _client(gateway)
    _delegate(client, tid="t1")
    sent_before = len(gateway.calls)
    later = _delegate(client, "List the production runs again, narrower.", tid="t2")
    assert later["status"] == "error"
    assert later["content"][0]["text"].startswith("[engine] Not sent:")
    assert len(gateway.calls) == sent_before
