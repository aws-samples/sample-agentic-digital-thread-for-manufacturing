# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The core shipment concept says who holds the goods at each stage.

The core ``shipment`` concept is the vocabulary text both models read when a
question names shipments: the orchestrator in the route seed's concept notes,
and the system agent that holds the shipments in its delegation's
``kg_context``. It has to say what a shipment's stage means for custody:
before dispatch the goods are still at the shipping site, after handover and
until confirmed delivery they are with the carrier (which may be able to stop
or intercept them), and after delivery they are with the recipient, with
cancellation, refusal, return, loss and partial delivery read from the
shipment's own records. Without that, a model reads a shipment that has left
as closed.

The statement describes the concept, never an answer: it names no action a
particular question must take (no containment list, no "stop these"). It is
also dataset neutral: it names no table, column or status value, so each
system's own recorded status values are read against it. The renamed schema
below records different status values from any demo dataset.

Offline and pure: the concept vocabulary is built from the core definitions
and the system guidance store is patched out.
"""
from __future__ import annotations

import re

import pytest

import agents.concept_notes as cn
import agents.system_agent as sa
from agents.catalog import CONCEPT_NAME, CORE_CONCEPT_DEFINITIONS

#: A fulfilment system whose schema shares no names with any demo dataset.
SYSTEM_ID = "fulfil-hub"
TOOL = "fulfil-hub___query_fulfil_hub"
FIELDS = ("consignment.consignment_ref", "consignment.leg_state",
          "consignment.handed_to_haulier_at", "consignment_line.lot_code")
#: The status values that renamed schema records for its consignments.
RECORDED_STATES = ("STAGED", "LOADED", "ON_TRUCK", "HANDED_OVER")

CONTAINMENT_QUESTION = ("Draft the containment list for production run R-1 and the machine "
                        "that made it: batches, orders, and shipments to hold.")
AT_RISK_QUESTION = ("Is vibration on machine M-7 trending up over the last hour, and which "
                    "current work orders and open shipments are at risk if it fails?")
ROUTE = f"ROUTE:\ntarget_system: {SYSTEM_ID}\ntranslated_identifiers: none"

#: The custody statement the definition has to carry, in any wording close to it.
_WITH_CARRIER = re.compile(
    r"handed to a carrier.{0,60}with the carrier.{0,40}stop or intercept", re.I | re.S)
_DELIVERED = re.compile(r"after delivery.{0,30}with the recipient", re.I | re.S)
_READ_RECORDS = re.compile(
    r"cancell?ation, refusal, return, loss and partial delivery.{0,60}own records", re.I | re.S)


def _vocabulary():
    """The core vocabulary, with the shipment concept mapped to the renamed system."""
    concepts = tuple(
        cn.Concept(id=cid, name=CONCEPT_NAME[cid], description=text,
                   systems=(SYSTEM_ID,) if cid == "shipment" else ())
        for cid, text in CORE_CONCEPT_DEFINITIONS.items())
    return cn.Vocabulary(concepts, fields={SYSTEM_ID: FIELDS})


def _lifecycle_sentences(text: str) -> list:
    """The sentences of ``text`` that state what a shipment's stage means."""
    return [s for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
            if re.search(r"with the carrier|with the recipient|shipping site|own records",
                         s, re.I)]


def test_core_shipment_definition_says_who_holds_the_goods_at_each_stage():
    text = CORE_CONCEPT_DEFINITIONS["shipment"]
    assert re.search(r"before dispatch.{0,40}shipping site", text, re.I | re.S), text
    assert _WITH_CARRIER.search(text), text
    assert _DELIVERED.search(text), text
    assert _READ_RECORDS.search(text), text


def test_shipment_definition_prescribes_no_answer():
    """The definition describes custody; it never tells a question what to
    list or do (containment, recall, hold, notify)."""
    sentences = _lifecycle_sentences(CORE_CONCEPT_DEFINITIONS["shipment"])
    assert sentences
    for sentence in sentences:
        assert not re.search(r"containment|recall|\blists?\b|\bnotif|\bmust\b|\bshould\b",
                             sentence, re.I), sentence


def test_shipment_definition_still_fits_one_concept_note_whole():
    note = cn.match_notes("Which shipments are still in transit?", _vocabulary())
    assert [n.concept for n in note] == ["shipment"]
    assert not note[0].definition.endswith(cn.TRUNCATION_MARKER)
    assert _WITH_CARRIER.search(note[0].definition)


@pytest.mark.parametrize("question", [CONTAINMENT_QUESTION, AT_RISK_QUESTION])
def test_route_seed_carries_the_stop_statement_to_the_orchestrator(question):
    notes = cn.match_notes(question, _vocabulary())
    assert "shipment" in [n.concept for n in notes]
    block = cn.seed_block(notes)
    assert _WITH_CARRIER.search(block) and _DELIVERED.search(block)


@pytest.mark.parametrize("question", [CONTAINMENT_QUESTION, AT_RISK_QUESTION])
def test_renamed_shipments_system_reads_the_stop_statement_in_its_question(
        question, monkeypatch):
    """The text the renamed system's agent model reads for its delegation."""
    import agents.system_guidance as guidance
    monkeypatch.setattr(guidance, "read", lambda _system_id: "")
    notes = cn.match_notes(question, _vocabulary())
    arguments = {"question": "Which consignments of the part are not yet delivered?",
                 "kg_context": ROUTE}
    merged = cn.with_kg_context_notes(TOOL, arguments, notes)
    delegated = sa._augment_question_with_kg_context(
        SYSTEM_ID, merged["question"], merged["kg_context"])
    assert _WITH_CARRIER.search(delegated) and _DELIVERED.search(delegated)


def test_stop_statement_names_no_schema_or_status_value():
    """The statement is about a shipment's stage, never a stored literal: no
    table or column name, no code-like token, and none of the status values
    either the renamed schema or the definition's own field list records."""
    text = CORE_CONCEPT_DEFINITIONS["shipment"]
    sentences = _lifecycle_sentences(text)
    assert sentences, text
    listed = re.search(r"status \(([^)]*)\)", text)
    stored = set(RECORDED_STATES) | set((listed.group(1) if listed else "").split("/"))
    for sentence in sentences:
        assert "`" not in sentence and "_" not in sentence, sentence
        assert not re.search(r"\b[A-Z]{3,}\b", sentence), sentence
        assert not any(value and value in sentence for value in stored), sentence
