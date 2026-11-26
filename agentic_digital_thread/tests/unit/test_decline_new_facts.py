# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A decline does not lock a system out of a recovery the engine can prove.

Measured on a rehearsal deployment (2026-11-03): a system agent looked up a
value in a column that holds another form of identifier; the shape guard
refused the lookup and the engine carried its IDENTIFIER-MISS block, but the
agent also ended with ``DECLINED-BY-RULE kind=not_connected`` for the
concept. The orchestrator then got the other form from a second system,
which the turn's IdentityRules join to the first for that concept, and sent
the first system questions carrying those values; the decline guard refused
both, and the answer lacked the figures only the first system holds.

The decline guard exists because a system that declined a list for a stated
rule was re-asked with narrower wording and gave the rows. That stays
refused. Pinned here, neutral fixtures (sys-a, sys-b, sys-c):

* orchestrator side: after a ``not_connected`` decline, a delegation to the
  same system about the same concept is sent only when its QUESTION carries
  an identifier that another system's successful result returned under the
  field of an IdentityRule of the declined concept into the declined system
  (``carried_values``, over the route facts' recorded results, which leave
  out engine lines and IDENTIFIER-MISS blocks), and that the declined
  question did not carry. Plant codes, schema names, units, status values,
  versions, a peer's miss block, the route context, the declining system's
  own values, a rule of another concept, and a ``rule`` or
  ``withheld_metric`` decline reopen nothing. Identifiers are compared
  casefolded, so a case-only repeat is refused, and a declined question
  with more identifiers than the scan reads is never reopened;
* system side: every typed decline is kept, also a ``not_connected`` marker
  beside a wrong-form refusal of the same concept: the engine cannot tell
  which reason the marker was written for, and the orchestrator-side
  exception above is what lets the recovery through.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import agents.orchestrator as orch
import agents.route_facts as rf
import agents.system_agent as sa
import agents.unresolved_miss as um
from agents.call_delivery import CallDelivery
from agents.call_delivery import record as record_delivery


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


@pytest.fixture(autouse=True)
def _clean_turn():
    rf.reset_turn()
    yield
    rf.reset_turn()


def _result(text, status="success"):
    return {"status": status, "toolUseId": "t", "content": [{"text": text}]}


def _declined(concept="record_link", kind="not_connected", text="Those forms are not "
              "cross-referenced here."):
    return _result(f"{text}\n\n[engine] declined: reason_kind={kind}; concept={concept}")


_A = "sys-a___query_sys_a"
_B = "sys-b___query_sys_b"
_C = "sys-c___query_sys_c"


def _rule(concept, a_field, peer, peer_field, rid="ir-1"):
    return {"id": rid, "concept": concept, "source_system": peer, "source_field": peer_field,
            "target_system": "sys-a", "target_field": a_field}


#: sys-b's ``unit_runs.run_ref`` and ``unit_runs.order_ref`` both join to
#: sys-a's ``link_table.ref_key`` for record_link; sys-c joins to nothing.
_RULES = [_rule("record_link", "link_table.ref_key", "sys-b", "unit_runs.run_ref", "ir-1"),
          _rule("record_link", "link_table.ref_key", "sys-b", "unit_runs.order_ref", "ir-2")]


def _seed(rules=_RULES):
    rf.remember_turn({"systems": [
        {"id": "sys-a", "rules": {"identity_rules": list(rules)}},
        {"id": "sys-b"}, {"id": "sys-c"}]})


class _Client:
    """A gateway client whose answers are queued per tool name."""

    def __init__(self, answers):
        self.answers = {name: list(items) for name, items in answers.items()}
        self.calls = []
        self.lock = threading.Lock()
        self.list_tools_sync = lambda *a, **k: []

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            # The session keeper records each call it delivers.
            record_delivery(CallDelivery.SENT)
            with self.lock:
                self.calls.append((name, arguments["question"]))
                queue = self.answers.get(name) or []
                return queue.pop(0) if queue else _result("Here are the rows.")

        self.call_tool_async = _call


def _client(answers, rules=_RULES):
    _seed(rules)
    fake = _Client(answers)
    return fake, orch._wrap_gateway_client_with_delegation_context(fake)


def _ask(client, name, question, concept="record_link", tid="t", context=""):
    args = {"question": question,
            "kg_context": f"ROUTE:\ntarget_system: x\ncanonical_concept: {concept}\n{context}"}
    return asyncio.run(client.call_tool_async(tid, name, args))


def _sent(fake):
    return [name for name, _ in fake.calls]


# --------------------------------------------------------------------------- #
# Orchestrator side: what reopens a not_connected decline
# --------------------------------------------------------------------------- #
#: The recovery seen live, neutral ids of the same shapes: sys-a was asked
#: about RUN-0091-90001, which its link table holds in the order form.
_FIRST = "Which supplier lot did run RUN-0091-90001 draw, and who received that lot's runs?"
_PAIR = _result("| Run Ref | Order Ref |\n|---|---|\n| RUN-0091-90001 | WO-0091-90001 |")
_SIBLINGS = _result("| Order Ref | Run Ref |\n|---|---|\n"
                    "| WO-0090-92001 | RUN-0090-92001 |\n| WO-0091-91002 | RUN-0091-91002 |")


def test_the_run_to_order_recovery_by_the_peers_order_value_is_sent():
    fake, client = _client({_A: [_declined()], _B: [_PAIR]})
    _ask(client, _A, _FIRST, tid="t1")
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?", tid="t2")
    sent = _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume, and "
                "which other orders used it?", tid="t3")
    # Before: refused as a repeat.
    assert sent["status"] == "success"
    assert _sent(fake) == [_A, _B, _A]


def test_the_run_to_order_recovery_by_the_peers_run_values_is_sent():
    fake, client = _client({_A: [_declined()], _B: [_SIBLINGS]})
    _ask(client, _A, _FIRST, tid="t1")
    _ask(client, _B, "Which runs belong to orders WO-0090-92001 and WO-0091-91002?", tid="t2")
    _ask(client, _A, "For ref_key values RUN-0090-92001 and RUN-0091-91002 list every "
         "line with its customer, units and value.", tid="t3")
    assert _sent(fake) == [_A, _B, _A]


#: The original incident's shape: a stuck-unit list declined because it needs
#: a source that is not connected; a peer then returns ordinary values.
_STUCK = "Which units are stuck, with their age?"
_PEER_NOISE = _result(
    "| plant_id | schema | unit | status | version |\n|---|---|---|---|---|\n"
    "| SITE-07 | ops_v2 | kg/m3 | in_progress | v1.2 |\n"
    "plant_id: SITE-07\nstatus_code: S-200\nsource_system: sys-b\nfirmware: 4.10.2")


@pytest.mark.parametrize("narrower", [
    "List the open rows with their age at SITE-07.",
    "List the open dwell_unit rows for ops_v2 at SITE-07 with status in_progress.",
    "Open dwell_unit rows at SITE-07, firmware 4.10.2, status S-200, kg/m3.",
])
def test_a_narrower_reword_with_a_peers_ordinary_values_stays_refused(narrower):
    fake, client = _client({_A: [_declined("dwell_unit")], _B: [_PEER_NOISE]},
                           rules=[_rule("dwell_unit", "units.unit_ref", "sys-b",
                                        "units.unit_ref")])
    _ask(client, _A, _STUCK, "dwell_unit", "t1")
    _ask(client, _B, "Which plant and status codes are there?", "none", "t2")
    refused = _ask(client, _A, narrower, "dwell_unit", "t3")
    # A check that counted any value a peer stated sent this (SITE-07 etc.).
    assert refused["status"] == "error"
    assert refused["content"][0]["text"].startswith("[engine] Not sent:")
    assert _sent(fake) == [_A, _B]


def test_a_value_only_in_a_peers_identifier_miss_block_stays_refused():
    miss = _result("No run matches.\n\n```\nIDENTIFIER-MISS:\ntoken: WO-0091-90001\n"
                   "looked_in: unit_runs.order_ref\nmy_format: shaped like WO-9999-99999\n"
                   "context: not found\n```")
    fake, client = _client({_A: [_declined()], _B: [miss]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    # A check that counted a miss block's token as returned sent this.
    assert _sent(fake) == [_A, _B]


def test_a_value_only_in_the_route_context_stays_refused():
    fake, client = _client({_A: [_declined()], _B: [_PAIR]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did that order consume?",
         context="translated_identifiers: WO-0091-90001\n")
    # A check that read values from kg_context sent this.
    assert _sent(fake) == [_A, _B]


def test_a_peer_value_under_a_field_no_rule_names_stays_refused():
    other = _result("| Tag | Note |\n|---|---|\n| WO-0091-90001 | seen |")
    fake, client = _client({_A: [_declined()], _B: [other]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Any tags?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _B]


def test_a_rule_of_another_concept_does_not_reopen_the_decline():
    fake, client = _client({_A: [_declined()], _B: [_PAIR]},
                           rules=[_rule("other_link", "link_table.ref_key", "sys-b",
                                        "unit_runs.order_ref")])
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _B]


def test_a_system_with_no_rule_into_the_declined_one_reopens_nothing():
    fake, client = _client({_A: [_declined()], _C: [_PAIR]})
    _ask(client, _A, _FIRST)
    _ask(client, _C, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _C]


def test_an_identical_repeat_is_still_refused():
    fake, client = _client({_A: [_declined()], _B: [_PAIR]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, _FIRST)
    assert _sent(fake) == [_A, _B]


def test_a_case_only_repeat_is_still_refused():
    # The peer returns the declined question's own value in lower case.
    lower = _result("| Run Ref | Order Ref |\n|---|---|\n| run-0091-90001 | WO-0091-90001 |")
    fake, client = _client({_A: [_declined()], _B: [lower]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did run run-0091-90001 draw?")
    # A case-sensitive comparison sent this as a new identifier.
    assert _sent(fake) == [_A, _B]


_SEVENTEEN = [f"RUN-0091-{90001 + i}" for i in range(17)]


def test_a_repeat_of_the_seventeenth_identifier_is_refused():
    pairs = "\n".join(f"| {r} | WO-0091-{90001 + i} |" for i, r in enumerate(_SEVENTEEN))
    peer = _result("| Run Ref | Order Ref |\n|---|---|\n" + pairs)
    fake, client = _client({_A: [_declined()], _B: [peer]})
    _ask(client, _A, "Which lots did runs " + ", ".join(_SEVENTEEN) + " draw?")
    _ask(client, _B, "Which order refs belong to those runs?")
    _ask(client, _A, f"Which lot did run {_SEVENTEEN[-1]} draw?")
    # The declined scan stopped at 16, so the 17th looked new and was sent.
    assert _sent(fake) == [_A, _B]


def test_a_declined_question_past_the_scan_cap_is_never_reopened():
    pairs = "\n".join(f"| {r} | WO-0091-{90001 + i} |" for i, r in enumerate(_SEVENTEEN))
    peer = _result("| Run Ref | Order Ref |\n|---|---|\n" + pairs)
    fake, client = _client({_A: [_declined()], _B: [peer]})
    _ask(client, _A, "Which lots did runs " + ", ".join(_SEVENTEEN) + " draw?")
    _ask(client, _B, "Which order refs belong to those runs?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _B]


def test_values_the_declining_system_gave_itself_reopen_nothing():
    own = _result("| Order Ref |\n|---|\n| WO-0091-90001 |\n\n"
                  "[engine] declined: reason_kind=not_connected; concept=record_link")
    fake, client = _client({_A: [own]})
    _ask(client, _A, _FIRST)
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A]


@pytest.mark.parametrize("kind", ["rule", "withheld_metric"])
def test_a_stated_rule_decline_is_not_reopened(kind):
    fake, client = _client({_A: [_declined(kind=kind)], _B: [_PAIR]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _B]


def test_an_error_result_from_the_peer_reopens_nothing():
    fake, client = _client({_A: [_declined()], _B: [_result(_PAIR["content"][0]["text"],
                                                             status="error")]})
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _B]


def test_with_no_route_facts_a_decline_is_never_reopened():
    fake = _Client({_A: [_declined()], _B: [_PAIR]})
    client = orch._wrap_gateway_client_with_decline_guard(fake)
    _ask(client, _A, _FIRST)
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?")
    _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?")
    assert _sent(fake) == [_A, _B]


def test_concurrent_recording_and_refusal_checks():
    fake, client = _client({_A: [_declined()], _B: [_PAIR] * 8})
    _ask(client, _A, _FIRST, tid="t0")
    errors: list = []
    statuses: list = []

    def run(i):
        try:
            if i % 2:
                _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?",
                     tid=f"b{i}")
            else:
                statuses.append(_ask(client, _A, "List the rows again, narrower.",
                                     tid=f"a{i}")["status"])
        except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert statuses == ["error"] * 8
    sent = _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?", tid="z")
    assert sent["status"] == "success"
    assert _sent(fake).count(_A) == 2 and _sent(fake).count(_B) == 8


# --------------------------------------------------------------------------- #
# System side: a not_connected marker beside an identifier miss
# --------------------------------------------------------------------------- #
_MARKER = "DECLINED-BY-RULE kind=not_connected concept=record_link"


def _block(token):
    return (f"IDENTIFIER-MISS:\ntoken: {token}\nlooked_in: link_table.ref_key\n"
            "my_format: link_table.ref_key values are shaped like WO-9999-99999\n"
            f"context: {token} has the form of another column.")


def _refusal_state(*entries):
    """An invocation state whose wrong-form record holds ``entries``
    ``(value, concept)``: refused, none resolved."""
    run_sig = sa._shape_signature("RUN-9999-99999")
    order_sig = sa._shape_signature("WO-9999-99999")
    pending = {}
    for value, concept in entries:
        pending[(value.casefold(), "link_table.ref_key")] = {
            "value": value, "folded": value.casefold(), "column": "link_table.ref_key",
            "paired_column": "line_items.ref_key", "block": _block(value),
            "signature": order_sig, "partner": run_sig, "counterparts": set(),
            "keys": set(), "concept": concept,
            "value_form": ("run", frozenset()), "counterpart_form": ("wo", frozenset())}
    return {um.STATE_KEY: {"pending": pending, "unread": False}}


def _run_query(answer, monkeypatch, state=None, kg_context=""):
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state",
                        lambda _a, _q: (answer, state if state is not None else {}))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    return asyncio.run(sa._run_mode_a_query(object(), "sys-a", "Which lot did it use?",
                                            kg_context=kg_context))


_ROUTE = "```ROUTE:\ntarget_system: sys-a\ncanonical_concept: record_link\n```"


_DECLINE_LINE = "[engine] declined: reason_kind=not_connected; concept=record_link"


def test_a_same_concept_decline_beside_a_wrong_form_refusal_is_kept(monkeypatch):
    answer = _run_query(f"That list needs a source that is not connected.\n{_MARKER}",
                        monkeypatch, _refusal_state(("RUN-0091-90001", "record_link")),
                        _ROUTE)
    # A check that took a same-concept miss as the sole reason dropped this.
    assert answer.endswith(_DECLINE_LINE)
    assert "IDENTIFIER-MISS:" in answer


def test_a_decline_beside_a_refusal_on_a_call_with_no_route_concept_is_kept(monkeypatch):
    answer = _run_query(f"The two forms do not cross-reference.\n{_MARKER}", monkeypatch,
                        _refusal_state(("RUN-0091-90001", "record_link")))
    assert answer.endswith(_DECLINE_LINE)


def test_the_live_recovery_needs_only_the_guard_with_the_decline_kept():
    # The system side keeps the decline; the guard alone lets both asks through.
    fake, client = _client({_A: [_declined()], _B: [_PAIR, _SIBLINGS]})
    _ask(client, _A, _FIRST, tid="t1")
    _ask(client, _B, "Which order ref does run RUN-0091-90001 belong to?", tid="t2")
    first = _ask(client, _A, "Which supplier lot did order WO-0091-90001 consume?", tid="t3")
    _ask(client, _B, "Which runs belong to orders WO-0090-92001 and WO-0091-91002?", tid="t4")
    second = _ask(client, _A, "For ref_key values RUN-0090-92001 and RUN-0091-91002 list "
                  "every line with its customer, units and value.", tid="t5")
    assert first["status"] == second["status"] == "success"
    assert _sent(fake) == [_A, _B, _A, _B, _A]


def test_a_decline_for_one_concept_beside_a_miss_for_another_is_kept(monkeypatch):
    marker = "DECLINED-BY-RULE kind=not_connected concept=dwell_unit"
    answer = _run_query(f"No stuck-unit list here; and ORDER-404 is not in that form.\n"
                        f"{marker}", monkeypatch,
                        _refusal_state(("RUN-0091-90001", "record_link")))
    # A check that took any miss as the reason dropped the dwell_unit decline.
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=dwell_unit")


def test_a_decline_beside_misses_of_two_concepts_is_kept(monkeypatch):
    answer = _run_query(f"Neither form pairs.\n{_MARKER}", monkeypatch,
                        _refusal_state(("RUN-0091-90001", "record_link"),
                                       ("RUN-0091-90002", "other_link")))
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=record_link")


def test_a_decline_beside_only_the_models_own_miss_block_is_kept(monkeypatch):
    answer = _run_query(f"No match.\n\n```\n{_block('RUN-0091-90001')}\n```\n{_MARKER}",
                        monkeypatch)
    # A check that trusted the model's own block dropped the decline.
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=record_link")


def test_a_decline_whose_answer_reports_another_miss_is_kept(monkeypatch):
    answer = _run_query(f"No match.\n\n```\n{_block('RUN-0091-77777')}\n```\n{_MARKER}",
                        monkeypatch, _refusal_state(("RUN-0091-90001", "record_link")),
                        _ROUTE)
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=record_link")


def test_a_decline_on_a_call_routed_for_another_concept_too_is_kept(monkeypatch):
    route = "```ROUTE:\ntarget_system: sys-a\ncanonical_concept: record_link, dwell_unit\n```"
    answer = _run_query(f"The forms do not pair.\n{_MARKER}", monkeypatch,
                        _refusal_state(("RUN-0091-90001", "record_link")), route)
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=record_link")


def test_a_refusal_with_no_concept_keeps_the_decline(monkeypatch):
    answer = _run_query(f"The forms do not pair.\n{_MARKER}", monkeypatch,
                        _refusal_state(("RUN-0091-90001", "")), _ROUTE)
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=record_link")


def test_a_not_connected_marker_without_a_miss_is_still_a_decline(monkeypatch):
    answer = _run_query(f"That list needs a source that is not connected.\n{_MARKER}",
                        monkeypatch)
    assert answer.endswith("[engine] declined: reason_kind=not_connected; concept=record_link")


def test_a_stated_rule_marker_beside_a_miss_is_still_a_decline(monkeypatch):
    answer = _run_query("Not given here.\nDECLINED-BY-RULE kind=rule concept=record_link",
                        monkeypatch, _refusal_state(("RUN-0091-90001", "record_link")),
                        _ROUTE)
    assert answer.endswith("[engine] declined: reason_kind=rule; concept=record_link")
