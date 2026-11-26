# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""After a wrong-form refusal, a decline reopens only for a form the pairing system's own results returned.

Measured on a test event (2026-11-11, a fresh account, one lot question asked
ten times): one system kept a batch under a work-order number in one table
and a run number in another, and a second system paired the two. The holder's
shape guard refused a wrong-form lookup and the holder ended its answer with a
typed ``not_connected`` decline about the concept. The engine's form crossing
could not pair the values on that seat, so the decline guard refused every
later delegation about the concept for the rest of the turn, including one
the model sent after it had fetched the other form from the pairing system
itself.

The decline's kind and concept are the system model's words, and the
runtime's own wrong-form line (``wrong_form_refused``) says a refusal
happened in the call, not that it caused the decline: the same call can
decline because the records need a source that is not connected. So every
decline is recorded, with the concepts that trusted line named (the model's
decline may name another concept than the one the guard refused, measured
on a rehearsal account on 2026-11-12), and a
later delegation is sent only when it brings the missing form itself
(``delegation_decline.brings_paired_form``): an identifier the declined
question did not carry, of one of the shapes of the forms line for a refused
concept other than the declined question's, that a pairing system named by
the turn's IDENTIFIER
FORMS line for the holder returned from its own tool results this turn. That
last fact comes only from the pairing system's runtime, in its
result-identifiers line (``result_identifiers``), never from its answer text:
a truthful negative answer to a question carrying a value the model made up
echoes that value.

Pinned here on the neutral schema of ``test_form_crossing.py`` (systems
ledger-x, cell-x, and cell-z that pairs nothing), through the turn's real
delegation stack, with every engine pairing read refused as on the
test-event seat. Each system's answer ends as its runtime writes it when
asked: the result-identifiers line when its results returned a value its
answer states, then the identifier-sources line.

* the measured recovery (the model fetched the other form from the pairing
  system and asked again with it) is sent, also when the model labelled its
  decline with another concept than the refused one;
* refused: a narrower rewording after a refusal or after a genuine
  unconnected-source decline; a value the pairing system's answer only
  echoes; a value its line lists that its own delegation carried; a line
  that is not the result's last; a value no pairing system returned, one
  only the declining system or a system that pairs nothing returned, one of
  the declined question's own form; and any value after a decline without
  the trusted line for a concept the forms line covers;
* ``rule`` and ``withheld_metric`` declines, no forms line, no turn facts,
  an error result, enforce mode, and the pairing system's own decline stay
  refused too.

Fully offline.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

import agents.identifier_provenance as ip
import agents.orchestrator as orch
import agents.route_facts as rf
from agents import form_pairing as fp
from agents.call_delivery import CallDelivery
from agents.call_delivery import record as record_delivery
from agents.delegation_decline import REFUSAL_PREFIX
from tests.unit import test_form_crossing as tfc

HOLDER, PAIRER, OTHER = tfc.HOLDER, tfc.PAIRER, "cell-z"
HOLDER_TOOL, PAIRER_TOOL, OTHER_TOOL = tfc.HOLDER_TOOL, tfc.PAIRER_TOOL, "cell-z___query_cell_z"
CONCEPT = tfc.CONCEPT
FIRST_QUESTION = "Which coil did production run BLD00412 draw, and which units drew it too?"
#: The holder could not look the build up where it keeps the job form.
NOT_PAIRED = ("I cannot look up production run BLD00412 in the consumption records here; "
              "they hold another form of identifier.")
#: The same, with the job forms of the coil's other units the holder returned.
WITH_JOBS = ("Coil CL-77 was drawn by JB-4471-001 and JB-4471-002. I cannot link "
             "BLD00412 to those consumption rows here.")
#: A decline for a source the holder does not have, about the same concept.
ARCHIVE = ("The production runs before the migration are kept in an archive that is not "
           "connected to this system, so I cannot list their coil draws.")
PAIR_QUESTION = "Which job made build BLD00412?"
#: The identifier-sources line a runtime writes last when asked and it names no column.
SOURCES = "\n\n" + ip.ACK_LINE
RIGHT_FORM = "Which coil did production run JB-4471-001 draw?"
NARROWER = "List only last week's production runs and the coils they drew."
PLAIN = "Coil CL-77, 40 kg."
#: A value of the job form that no system's rows hold.
MADE_UP = "JB-4471-009"


def _listed(*values) -> str:
    """The result-identifiers line for ``values``, as the runtime writes it."""
    return "\n\n[engine] Result identifiers v1: " + json.dumps(list(values),
                                                                separators=(",", ":"))


def _tail(*values) -> str:
    """What a runtime appends to an answer when asked: the result-identifiers
    line when its results returned a value the answer states, then the
    identifier-sources line, last."""
    return (_listed(*values) if values else "") + SOURCES


#: What the pairing system returned on the seat: the other form, unlabelled,
#: from a row its query read.
PAIRED = "Build BLD00412 was made as job JB-4471-001." + _tail("jb-4471-001")
#: A truthful negative answer to a question that carried a value the model made
#: up: the answer echoes it, and no result of the call returned it.
ECHOED = f"No record exists for job {MADE_UP}; it is not paired with that build." + _tail()


def _refused(concept=CONCEPT):
    """The engine line the holder's runtime writes after a wrong-form refusal."""
    return f"\n\n[engine] Wrong-form lookup refused: concept={concept}"


def _decline(kind="not_connected", concept=CONCEPT):
    return f"\n\n[engine] declined: reason_kind={kind}; concept={concept}"


#: What the holder answered on the seat: its guard refused, then it declined.
MEASURED = NOT_PAIRED + _refused() + _decline()


def _result(text, status="success"):
    return {"status": status, "toolUseId": "t", "content": [{"text": text}]}


class _Gateway:
    """Each system answers from its own queue; every engine pairing read is
    refused, as every one was on the 2026-11-11 seat, so no crossing finishes."""

    def __init__(self, answers):
        self.answers = {name: list(items) for name, items in answers.items()}
        self.calls = []
        self.list_tools_sync = lambda *a, **k: []

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            record_delivery(CallDelivery.SENT)
            self.calls.append((name, dict(arguments or {})))
            if fp.RESERVED_ARG in (arguments or {}):
                return _result(json.dumps({"engine": fp.ENGINE_TAG, "v": 2,
                                           "status": "refused", "reason": "concept_unknown"}))
            queue = self.answers.get(name) or []
            answer = queue.pop(0) if queue else PLAIN
            return answer if isinstance(answer, dict) else _result(answer)

        self.call_tool_async = _call

    def sent(self, name):
        return [a for n, a in self.calls if n == name and fp.RESERVED_ARG not in a]


@pytest.fixture(autouse=True)
def _clean_turn(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    rf.reset_turn()
    ip.reset_turn_facts()
    yield
    rf.reset_turn()
    ip.reset_turn_facts()


def _client(gateway, facts=True, **world):
    """The turn's delegation stack: route facts for the three systems, and
    the route seed's forms facts (``test_form_crossing``'s) unless ``facts``
    is false."""
    rf.reset_turn()
    ip.reset_turn_facts()
    rf.remember_turn({"systems": [{"id": s} for s in (HOLDER, PAIRER, OTHER)]})
    if facts:
        tfc._facts(question=f"{FIRST_QUESTION} {tfc.USER_QUESTION}", **world)
    return orch._wrap_gateway_client_with_delegation_context(gateway)


def _delegate(client, tool, question, tid, **extra):
    return asyncio.run(client.call_tool_async(tid, tool, {"question": question, **extra}))


def _is_refusal(result):
    return (result["status"] == "error"
            and result["content"][0]["text"].startswith(REFUSAL_PREFIX))


def _refuses(first, later=RIGHT_FORM, pairer=None, other=None, pair_question=PAIR_QUESTION,
             pair_context=None, **world) -> bool:
    """Whether ``later`` to the holder is refused after the holder answered
    ``first`` (to :data:`FIRST_QUESTION`) and, in between, the pairing system
    answered ``pairer`` (to ``pair_question``, with ``pair_context`` as its
    context) and the system that pairs nothing answered ``other``."""
    gateway = _Gateway({HOLDER_TOOL: [first, PLAIN], PAIRER_TOOL: [pairer],
                        OTHER_TOOL: [other]})
    client = _client(gateway, **world)
    _delegate(client, HOLDER_TOOL, FIRST_QUESTION, "t1")
    extra = {"kg_context": pair_context} if pair_context else {}
    for tool, answer in ((PAIRER_TOOL, pairer), (OTHER_TOOL, other)):
        if answer is not None:
            _delegate(client, tool, pair_question, f"t-{tool}", **extra)
    refused = _is_refusal(_delegate(client, HOLDER_TOOL, later, "t9"))
    assert len(gateway.sent(HOLDER_TOOL)) == (1 if refused else 2)
    return refused


#: The measured recovery: the holder's measured decline, the pairing system's
#: measured answer, then the right-form re-ask.
RECOVERY = {"first": MEASURED, "pairer": PAIRED}


def _only_the_recovery_is_sent(**variant) -> bool:
    """Whether the measured recovery (:data:`RECOVERY`) is sent while
    ``variant``, the same turn with those arguments of :func:`_refuses`
    changed, is refused: what the variant changes is what decides. On a
    guard that reopens nothing, the recovery is refused too."""
    return not _refuses(**RECOVERY) and _refuses(**{**RECOVERY, **variant})


# --------------------------------------------------------------------------- #
# Every not_connected decline is recorded, with the trusted evidence
# --------------------------------------------------------------------------- #
def test_a_wrong_form_refusal_beside_a_genuine_unconnected_source_decline_still_latches():
    """The same call refused a wrong-form lookup and declined because the
    records need an archive that is not connected; the pairing system then
    returned the other form. A narrower rewording carries no value of it, so
    it stays refused while the re-ask with the returned value is sent."""
    assert _only_the_recovery_is_sent(first=ARCHIVE + _refused() + _decline(), later=NARROWER)


def test_a_decline_citing_a_source_that_is_not_connected_still_latches():
    """No wrong-form refusal in the call; a narrower rewording."""
    assert _only_the_recovery_is_sent(first=ARCHIVE + _decline(), later=NARROWER, pairer=None)


@pytest.mark.parametrize("first, flag", [(MEASURED, 1), (NOT_PAIRED + _decline(), 0)],
                         ids=["with-the-line", "without-it"])
def test_the_decline_is_recorded_with_whether_the_trusted_line_named_it(caplog, first, flag):
    """Before: a record had no such field."""
    with caplog.at_level(logging.INFO, logger=orch.logger.name):
        _delegate(_client(_Gateway({HOLDER_TOOL: [first]})), HOLDER_TOOL, FIRST_QUESTION, "t1")
    assert (f"DELEGATION_DECLINE recorded tool={HOLDER_TOOL} kind=not_connected concepts=1 "
            f"wrong_form={flag}") in caplog.text


# --------------------------------------------------------------------------- #
# Reopened only by a form the pairing system's own results returned
# --------------------------------------------------------------------------- #
def test_the_form_the_pairing_systems_results_returned_reopens_the_decline():
    """The measured trial: the model asked the pairing system for the other
    form itself (its answer gave the value under no rule's field, and its
    runtime's line lists it) and asked the holder again with it. Before:
    refused as a repeat."""
    assert not _refuses(MEASURED, pairer=PAIRED)


def test_an_answer_that_only_echoes_a_value_the_model_made_up_does_not_reopen():
    """The model put a made-up value of the job form in its question to the
    pairing system, whose truthful negative answer echoes it; no result of
    that call returned it, so its runtime lists nothing. Reading the answer
    text, the holder call would be sent."""
    assert MADE_UP in ECHOED
    assert _only_the_recovery_is_sent(
        later=f"Which coil did production run {MADE_UP} draw?", pairer=ECHOED,
        pair_question=f"Which build did job {MADE_UP} make?")


@pytest.mark.parametrize("where", ["question", "context"])
def test_a_listed_value_the_pairing_delegation_itself_carried_does_not_reopen(where):
    """A pairing system whose tool returns its request back as data lists
    the value the model sent it: what the delegation carried, in its
    question or its context, is never evidence it was returned."""
    sent = f"Which build did job {MADE_UP} make?"
    echoing = f"Job {MADE_UP}: no build recorded." + _tail(MADE_UP.lower())
    world = ({"pair_question": sent} if where == "question"
             else {"pair_context": f"Earlier the user mentioned job {MADE_UP}."})
    assert _only_the_recovery_is_sent(later=f"Which coil did production run {MADE_UP} draw?",
                                      pairer=echoing, **world)


@pytest.mark.parametrize("pairer", [
    "Build BLD00412 was made as job JB-4471-001." + _listed("jb-4471-001") + "\n\nDone."
    + SOURCES,
    "Build BLD00412 was made as job JB-4471-001.\n\n[engine] Result identifiers v1: "
    "jb-4471-001" + SOURCES,
], ids=["not-the-last-line", "malformed"])
def test_only_the_runtimes_last_line_is_read(pairer):
    """A copy of the line anywhere but right before the identifier-sources
    line (which the runtime always writes last when asked), or one that is
    not the engine's shape, lists nothing."""
    assert _only_the_recovery_is_sent(pairer=pairer)


def test_a_new_value_no_pairing_system_returned_is_refused():
    assert _only_the_recovery_is_sent(later=f"Which coil did production run {MADE_UP} draw?")


def test_a_value_only_the_declining_system_returned_is_refused():
    """The holder itself listed JB-4471-001, and its own line says so."""
    assert _only_the_recovery_is_sent(
        first=WITH_JOBS + _refused() + _decline() + _tail("jb-4471-001"), pairer=None)


def test_a_value_only_a_system_that_does_not_pair_the_forms_returned_is_refused():
    """cell-z is not a pairing system on the forms line."""
    assert _only_the_recovery_is_sent(pairer=None, other=PAIRED)


def test_a_value_of_the_declined_questions_own_form_is_refused():
    """The pairing system returned another build number, the form the
    declined question already used (the form the guard refused)."""
    assert _only_the_recovery_is_sent(
        later="Which coil did production run BLD00413 draw?",
        pairer="Builds BLD00413 and JB-4471-001 share a job." + _tail("bld00413", "jb-4471-001"))


@pytest.mark.parametrize("first", [
    NOT_PAIRED + _decline(),
    NOT_PAIRED + _refused("coil") + _decline(),
    NOT_PAIRED + _refused() + "\n\nUnit BLD00412 is in the other form." + _decline(),
    NOT_PAIRED + _decline() + _refused(),
    _result(MEASURED, status="error"),
], ids=["no-line", "a-concept-no-forms-line-covers", "not-right-before-the-decline",
        "after-the-decline", "error-result"])
def test_a_pairing_systems_value_does_not_reopen_a_decline_without_the_trusted_line(first):
    """Only the runtime's line right before the decline line, naming a
    concept the turn's forms line covers, in a successful result, is the
    evidence."""
    assert _only_the_recovery_is_sent(first=first)


# --------------------------------------------------------------------------- #
# The decline's concept is the model's word; the refused concept is the runtime's
# --------------------------------------------------------------------------- #
#: A concept word the holder's model chose for its decline, other than the
#: concept its shape guard refused (measured: guard ``batch``, decline
#: ``lot-number``).
LABEL = "coil-lot"
#: The right-form re-ask, worded about the model's label, so the latch reads it.
LABELLED_RIGHT_FORM = "Which coil lot did production run JB-4471-001 draw?"
#: The holder's measured answer with the model's own label on its decline.
RELABELLED = {"first": NOT_PAIRED + _refused() + _decline(concept=LABEL), "pairer": PAIRED,
              "later": LABELLED_RIGHT_FORM}


def _only_the_relabelled_recovery_is_sent(**variant) -> bool:
    """:func:`_only_the_recovery_is_sent` for :data:`RELABELLED`."""
    return not _refuses(**RELABELLED) and _refuses(**{**RELABELLED, **variant})


def test_a_decline_labelled_with_another_concept_reopens_for_the_refused_concepts_form():
    """Measured on a rehearsal account (2026-11-12): the holder's guard
    refused the run form under one concept, its model labelled the decline
    with another, and the right-form re-ask with the value the pairing
    system returned was refused as a repeat about the label. Before: the
    trusted line counted only when it named the decline's own concept."""
    assert not _refuses(**RELABELLED)


def test_a_relabelled_decline_still_refuses_a_narrower_rewording():
    assert _only_the_relabelled_recovery_is_sent(
        later="List only last week's coil lots and the runs that drew them.")


def test_a_relabelled_decline_is_not_reopened_by_an_echoed_value():
    assert _only_the_relabelled_recovery_is_sent(
        later=f"Which coil lot did production run {MADE_UP} draw?", pairer=ECHOED,
        pair_question=f"Which build did job {MADE_UP} make?")


@pytest.mark.parametrize("first", [
    NOT_PAIRED + _decline(concept=LABEL),
    NOT_PAIRED + _refused("coil") + _decline(concept=LABEL),
    NOT_PAIRED + _refused() + _decline("rule", concept=LABEL),
    NOT_PAIRED + _refused() + _decline("withheld_metric", concept=LABEL),
], ids=["no-line", "a-concept-no-forms-line-covers", "rule", "withheld-metric"])
def test_a_relabelled_decline_without_the_evidence_still_latches(first):
    assert _only_the_relabelled_recovery_is_sent(first=first)


def test_a_relabelled_decline_still_latches_without_a_forms_line():
    assert _only_the_relabelled_recovery_is_sent(jps=tfc.JPS[:2])


def test_a_decline_about_another_concept_beside_a_refusal_reopens_the_same_way():
    """The accepted trade-off (ADR-0087): the engine cannot tell a decline
    relabelled from the refused concept from a genuine one about another
    concept in the same call, so a question carrying the refused concept's
    other form, listed by a pairing system, is sent; a narrower rewording
    of it is not."""
    first = ARCHIVE + _refused() + _decline(concept="archive-record")
    assert not _refuses(first, later="Which archive record holds production run JB-4471-001?",
                        pairer=PAIRED)
    assert _refuses(first, later="List the archive records for last week's runs.",
                    pairer=PAIRED)


def test_enforce_mode_reopens_nothing(monkeypatch):
    """The typed envelope is not rewritten under enforce, so neither line in
    it is known to be the engine's."""
    assert not _refuses(**RECOVERY)
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    assert _refuses(**RECOVERY)


@pytest.mark.parametrize("kind", ["rule", "withheld_metric"])
def test_a_stated_rule_decline_is_not_reopened(kind):
    assert _only_the_recovery_is_sent(first=NOT_PAIRED + _refused() + _decline(kind))


@pytest.mark.parametrize("world", [{"jps": tfc.JPS[:2]}, {"facts": False}],
                         ids=["no-forms-line", "no-turn-facts"])
def test_without_a_crossable_forms_line_nothing_reopens(world):
    assert _only_the_recovery_is_sent(**world)


def test_a_decline_from_the_pairing_system_still_latches():
    """The forms line is the holder's; the pairing system's own decline about
    the concept is an ordinary decline, while the holder's reopens."""
    assert not _refuses(**RECOVERY)
    gateway = _Gateway({PAIRER_TOOL: [MEASURED]})
    client = _client(gateway)
    _delegate(client, PAIRER_TOOL, FIRST_QUESTION, "t1")
    assert _is_refusal(_delegate(client, PAIRER_TOOL, RIGHT_FORM, "t2"))


@pytest.mark.parametrize("line", ["", _refused()], ids=["without-the-line", "with-the-line"])
def test_an_engine_reask_that_still_declines_is_recorded(line):
    """The crossing finished and the holder still declined ``not_connected``:
    recorded like any decline, so a narrower rewording is refused, while the
    measured recovery is sent."""
    assert not _refuses(**RECOVERY)
    rf.reset_turn()
    ip.reset_turn_facts()
    first = tfc.FIRST + _refused() + tfc.DECLINE
    gateway = tfc._Gateway([_result(first), _result(tfc.FIRST + line + tfc.DECLINE),
                            _result(PLAIN)])
    client = tfc._client(gateway)
    tfc._delegate(client, tid="t1")
    assert _is_refusal(tfc._delegate(client, NARROWER, "t2"))


def test_the_line_never_reaches_the_model():
    """The pairing system's result as the orchestrator model gets it carries
    neither engine line, and its answer is unchanged. Before: the
    result-identifiers line reached the model."""
    gateway = _Gateway({PAIRER_TOOL: [PAIRED]})
    result = _delegate(_client(gateway), PAIRER_TOOL, PAIR_QUESTION, "t1")
    assert result["content"][0]["text"] == "Build BLD00412 was made as job JB-4471-001."
