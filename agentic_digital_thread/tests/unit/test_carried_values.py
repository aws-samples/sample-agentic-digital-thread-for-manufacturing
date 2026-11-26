# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How ``carried_values`` reads a result's labels and a question's identifiers.

The whole-turn behaviour is pinned in ``test_route_facts_merge``; these pin
the readers and their bounds on their own. Made-up names. Pure.
"""
from __future__ import annotations

import json

from agents import carried_values as cv

RULE = cv.IdentityRule("ir-1", "crate-lot", ("press-floor", "ledger"), ("tag_no", "lot_key"),
                       ("", ""))


def test_a_json_answer_labels_its_values_by_key():
    text = json.dumps({"rows": [{"lot_key": "J-77-0001", "qty": 4},
                                {"lot_key": "J-77-0002", "qty": 2}]})
    read = cv.peer_result("ledger", text)
    assert read.labels["J-77-0001"] == {"lot key"}
    found = cv.carried((RULE,), (read,), "press-floor", "For tag_no J-77-0002, which bay?")
    assert found.rules == (RULE,) and found.specific
    assert found.concepts() == ("crate-lot",)


def test_table_kv_and_header_spellings_name_the_field():
    table = "| Ship | Lot Key |\n|:---|---:|\n| S-1 | **J-77-0001** |\n"
    lines = "- **Lot key:** J-77-0002\n"
    read = cv.peer_result("ledger", table + "\n" + lines)
    assert read.labels["J-77-0001"] == {"lot key"} and read.labels["J-77-0002"] == {"lot key"}
    assert {"lot key"} & cv._field_forms("fin.lot_trace.lot_key")


def test_a_question_names_a_field_in_any_spelling_or_plural():
    assert cv.names_field("which tag_no values", "tag_no")
    assert cv.names_field("for these tag numbers", "tag_number")
    assert cv.names_field("for tag-no T-1", "plant.tags.tag_no")
    assert not cv.names_field("for these tags", "tag_no")


def test_carried_tokens_are_bounded_and_need_a_digit():
    question = " ".join(f"T-{i:04d}" for i in range(40)) + " plain words only"
    tokens = cv._identifiers(question, cv.MAX_CARRIED)
    assert len(tokens) == cv.MAX_CARRIED
    assert cv._identifiers("what about that one?", cv.MAX_CARRIED) == []


def test_a_result_is_read_only_up_to_its_bound():
    padding = "x" * cv.RESULT_CHAR_CAP
    read = cv.peer_result("ledger", f"{padding}\nlot_key: J-77-0009\n")
    assert "J-77-0009" not in read.tokens


def test_a_rule_to_another_system_or_the_same_system_is_not_used():
    read = cv.peer_result("meter-bank", "lot_key: J-77-0001\n")
    assert cv.carried((RULE,), (read,), "press-floor", "tag_no J-77-0001").rules == ()
    own = cv.peer_result("press-floor", "lot_key: J-77-0001\n")
    assert cv.carried((RULE,), (own,), "press-floor", "tag_no J-77-0001").rules == ()


def _envelope(extra=None):
    """A SystemAnswer-shaped object with one claim input under ``lot_code``."""
    from agents.response_integrity_contracts import (
        AnswerClaim, ClaimInput, ClaimKind, SourceEvidence, SystemAnswer)
    evidence = SourceEvidence(evidence_id="ev-1", system_id="ledger", boundary="sql",
                              queried_at="2026-10-01T00:00:00Z", semantic_field="lot_code",
                              physical_field="lot_trace.lot_code", locator="row:0",
                              canonical_type="string")
    claim = AnswerClaim(claim_id="c1", kind=ClaimKind.VALUE, label="Lot", value="J-77-0001",
                        inputs=(ClaimInput(label="lot", value="J-77-0001", evidence_id="ev-1",
                                           semantic_field="lot_code"),))
    data = json.loads(SystemAnswer(status="complete", claims=(claim,),
                                   evidence=(evidence,)).model_dump_json())
    data.update(extra or {})
    return data


def test_an_envelope_with_an_engine_note_and_a_trailing_tag_is_read_by_field():
    text = json.dumps(_envelope({"_adt_engine_note": "[engine] Result cut."})) + "\n<chart/>"
    read = cv.peer_result("ledger", text)
    assert {"lot code", "lot trace lot code"} <= read.labels["J-77-0001"]


def test_a_shape_that_does_not_validate_falls_back_to_its_keys():
    read = cv.peer_result("ledger", json.dumps(_envelope({"unexpected": 1})))
    assert read.labels["J-77-0001"] == {"value"}


def test_without_a_physical_field_the_semantic_field_labels_the_value():
    data = _envelope()
    data["evidence"][0]["physical_field"] = ""
    read = cv.peer_result("ledger", json.dumps(data))
    assert read.labels["J-77-0001"] == {"lot code"}


def test_a_physical_field_is_the_only_label_when_present():
    data = _envelope()
    data["evidence"][0]["physical_field"] = "lot_trace.dock_ref"
    read = cv.peer_result("ledger", json.dumps(data))
    assert read.labels["J-77-0001"] == {"dock ref", "lot trace dock ref"}


def test_text_after_leading_json_is_still_read_for_labels():
    text = ('{"summary":"two rows"}\n\n| Shipment | Batch ID |\n|---|---|\n'
            "| S-1 | LR-42-0001 |\n")
    read = cv.peer_result("ledger", text)
    assert read.labels["LR-42-0001"] == {"batch id"}
    assert "two rows" not in read.labels


def test_a_forged_envelope_labels_no_more_than_the_model_could_write_plainly():
    # Outside the integrity gate the answer is model text either way: an
    # envelope-shaped object naming a field grants the label a plain key does.
    data = _envelope()
    data["evidence"][0]["physical_field"] = "lot_code"
    forged = cv.peer_result("ledger", json.dumps(data))
    plain = cv.peer_result("ledger", json.dumps({"lot_code": "J-77-0001"}))
    assert forged.labels["J-77-0001"] == plain.labels["J-77-0001"] == {"lot code"}
