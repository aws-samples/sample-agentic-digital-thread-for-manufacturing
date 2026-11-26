# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 of the orchestrator prompt says what a quality containment covers.

A quality containment covers the product the evidence implicates, not only the
record that revealed the problem. The planner establishes, from the records,
the suspect interval for the suspected cause (last known good, first known
suspect, and whether and when the cause was corrected and the correction
verified), then what was made in that interval and where it is now, and
reports any boundary the records do not establish instead of choosing one.

The rule states a principle, never an answer: it lists no place product can
sit, no record type to look in and no action to take on it, because those
depend on the records and on the plant's procedure. It is dataset neutral.

It lives in the standing prompt's DECOMPOSE step, both modes and both Quick
variants. It is not a core data concept: a core concept that no connected
system maps would make the engine say no system records the term.

Offline and pure: no graph, no model.
"""
from __future__ import annotations

import re

import pytest

import agents.concept_notes as cn
import agents.orchestrator as orch
from agents.catalog import CONCEPT_NAME, CORE_CONCEPT_DEFINITIONS
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")
KINDS = ("standing", "quick")

#: The principle, in the rule's own words (whitespace folded, case ignored).
PRINCIPLE = (
    "covers the product the evidence implicates, not only the record that revealed the problem",
    "from the records, establish the suspect interval for the suspected cause",
    "last known good",
    "first known suspect",
    "whether and when it was corrected and the correction verified",
    "everything made on or with the implicated source in that interval",
    "where it is now",
    "report any boundary the records do not establish rather than choosing one",
)

#: Places product can sit, record types to look in, and actions to take on
#: product. The rule names none: listing them hands the model an answer.
_ANSWER_CATEGORIES = re.compile(
    r"in process|in stock|\bstock\b|inventor|warehouse|open orders?\b|shipment|consignment|"
    r"transit|carrier|deliver|recipient|customer|notif|recall|scrap|rework|retriev|"
    r"\breturn|dispos|\bstop\b|intercept", re.I)

#: A plant whose schema shares no names with any demo dataset.
SYSTEM_ID = "press-floor"
FIELDS = ("press_cycle.cycle_ref", "press_cycle.press_tag", "consignment.leg_state",
          "consignment_line.cycle_ref")
QUESTION = ("Draft the containment for press cycle K-9 and the press that ran it: what to "
            "hold, including anything the press is running now.")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompt(kind: str, mode: str) -> str:
    return (compose_quick_system_prompt(mode) if kind == "quick"
            else orch._build_orchestrator_prompt(mode))


def _step_one(prompt: str) -> str:
    start = prompt.index("Step 1 \u2014 DECOMPOSE")
    return prompt[start:prompt.index("Step 2 \u2014 ROUTE", start)]


def _rule() -> str:
    return getattr(orch, "_CONTAINMENT_RULE", "")


def _body() -> str:
    """The rule without its heading."""
    return _flat(_rule()).split(":", 1)[-1]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", MODES)
def test_every_orchestrator_prompt_states_the_principle_in_step_one(kind, mode):
    step_one = _flat(_step_one(_prompt(kind, mode))).lower()
    for phrase in PRINCIPLE:
        assert phrase in step_one, phrase


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", MODES)
def test_rule_rides_once_after_the_quoted_value_rule(kind, mode):
    prompt = _prompt(kind, mode)
    assert _rule()
    assert (orch._QUOTED_VALUE_RULE + _rule()) in prompt
    assert prompt.count(_rule()) == 1


def test_rule_lists_no_place_record_type_or_action_for_product():
    body = _body()
    assert body
    found = sorted({m.group(0).lower() for m in _ANSWER_CATEGORIES.finditer(body)})
    assert not found, found


def test_rule_never_assumes_onset_or_removal_times():
    """The interval comes from recorded evidence; an unrecorded boundary is
    reported, not inferred."""
    low = _body().lower()
    for assumed in ("when the cause began", "until it was removed", "whether it was removed"):
        assert assumed not in low, assumed
    assert "from the records" in low
    assert low.index("from the records") < low.index("suspect interval")


def test_rule_names_no_dataset_literal_and_stays_short():
    body = _body()
    assert body
    assert not re.search(r"\b[A-Z]{2,}-?\d", body), body
    assert not re.search(r"\d", body), body
    assert "`" not in body and "_" not in body, body
    assert not re.search(r"\b[A-Z]{3,}\b", body), body
    for word in ("ERP", "MES", "CMMS", "bearing", "vibration", "spindle", "bore", "machine"):
        assert not re.search(rf"\b{word}\b", body, re.I), word
    assert "\u2014" not in _rule() and "\u2013" not in _rule()
    assert len(_rule()) <= 1400, len(_rule())


def test_rule_shares_no_field_name_with_any_schema():
    """Generic on any schema: the rule names no field of the core vocabulary's
    field lists or of the renamed plant below."""
    low = _body().lower()
    names = set()
    for text in CORE_CONCEPT_DEFINITIONS.values():
        listed = re.search(r"Fields: ([^.]*)\.", text)
        if listed:
            names.update(n.strip().lower() for n in listed.group(1).split(","))
    for field in FIELDS:
        names.update(part.lower() for part in field.split("."))
    multiword = sorted(n for n in names if "_" in n or "." in n)
    assert multiword
    for name in multiword:
        assert name not in low, name


def _renamed_vocabulary():
    """The core vocabulary, with the batch and shipment concepts mapped to the
    renamed plant."""
    concepts = tuple(
        cn.Concept(id=cid, name=CONCEPT_NAME[cid], description=text,
                   systems=(SYSTEM_ID,) if cid in ("batch", "shipment") else ())
        for cid, text in CORE_CONCEPT_DEFINITIONS.items())
    return cn.Vocabulary(concepts, fields={SYSTEM_ID: FIELDS})


def test_containment_on_a_renamed_plant_is_not_an_unrecorded_term():
    """Guard: containment stays planning vocabulary, not a data concept that a
    connected system must record. Were it a core concept, a plant that maps no
    field to it would get the engine line saying no system records it."""
    vocabulary = _renamed_vocabulary()
    unrecorded = cn.match_unrecorded(QUESTION, vocabulary)
    assert "containment" not in unrecorded.ids
    assert "containment" not in cn.unrecorded_terms_line(unrecorded)
    assert "containment" not in CORE_CONCEPT_DEFINITIONS


# --------------------------------------------------------------------------- #
# Guard: no definition prescribes what an answer lists
# --------------------------------------------------------------------------- #
#: Phrases that tell an answer what to list or do, as opposed to saying what a
#: term means.
_PRESCRIBES = (
    re.compile(r"\b(?:a|an|the|your)\s+(?:containment|answer|reply|response|report|hold list|"
               r"recall list|containment list)\s+(?:lists?|includes?|names?|shows?|must|should|"
               r"has to|needs to)\b", re.I),
    re.compile(r"\bmust\s+(?:list|include|name|show|hold|stop|recall|notify)\b", re.I),
    re.compile(r"\b(?:list|include|name|show)\s+(?:it|them|these|those|both|each)\s+as\b", re.I),
    re.compile(r"\bthat includes\b", re.I),
    re.compile(r"\bfor this question\b", re.I),
)


def _definitions():
    from agents.demo.concept_overlays import DEMO_CONCEPT_OVERLAYS
    out = [(f"core:{cid}", text) for cid, text in CORE_CONCEPT_DEFINITIONS.items()]
    for cid, fields in DEMO_CONCEPT_OVERLAYS.items():
        out.extend((f"overlay:{cid}.{key}", value) for key, value in fields.items())
    out.append(("rule:containment", _rule()))
    return out


def _prescribing(text: str) -> list:
    flat = _flat(text)
    return [m.group(0) for pattern in _PRESCRIBES for m in [pattern.search(flat)] if m]


_DEFINITIONS = _definitions()


@pytest.mark.parametrize("label,text", _DEFINITIONS, ids=[label for label, _ in _DEFINITIONS])
def test_no_definition_prescribes_what_an_answer_lists(label, text):
    assert text, label
    assert not _prescribing(text), (label, _prescribing(text))


@pytest.mark.parametrize("text", (
    "Until it is delivered it is still with the carrier, so a containment lists both as "
    "shipments to stop.",
    "The answer must list every open order of the part.",
    "A hold list includes the newest run.",
    "Name them as shipments to hold for this question.",
    "That includes product made after that record, wherever it is now.",
))
def test_the_guard_flags_answer_prescriptions(text):
    assert _prescribing(text), text


def test_the_category_guard_flags_a_listed_answer():
    listed = ("wherever it is now: in process, in stock, on open orders or in shipments not yet "
              "delivered. Product already delivered is a customer notification or recall.")
    assert _ANSWER_CATEGORIES.search(listed)


# --------------------------------------------------------------------------- #
# Scope: only a quality containment whose scope the user has not fixed
# --------------------------------------------------------------------------- #
#: The rule's trigger and its two limits, in its own words.
TRIGGER = ("when the user asks what to contain, hold or quarantine because product is "
           "suspected of a quality defect")
KEEPS_NAMED_SCOPE = "if the user limits what to hold and asks for nothing wider, keep that scope"
NOT_A_CONTAINMENT = ("a credit or order hold, or a lot awaiting incoming inspection, "
                     "is not a containment")

#: The verbs the trigger is worded with. A question carrying none of them does
#: not meet the trigger as worded.
_TRIGGER_VERBS = re.compile(r"\b(?:contain\w*|hold\w*|quarantin\w*)\b", re.I)

#: Trace and impact questions on a renamed plant, in the shape of a supplier
#: lot trace, its sibling check, its customer exposure, how much of the lot is
#: left, and an at-risk question about a degrading machine. None asks to
#: contain anything, so the rule must leave them alone.
NOT_TRIGGERED = (
    "Which supplier coil lot did that press cycle consume, who supplied it and when did it "
    "arrive, and which other press cycles drew from the same coil lot?",
    "Did any of the other press cycles made from that coil lot fail their gauge check?",
    "Which buyers received stampings from any press cycle made with that coil lot? Give the "
    "units each buyer received, the value of those stampings, and the total.",
    "Which buyers received stampings from any press cycle that drew the same coil lot as press "
    "cycle K-9? Give the units each buyer received, the value of those stampings, and the total.",
    "How much of that coil lot is left, and has the plant started any press cycle from it since "
    "my failed press cycle ran?",
    "Is ram load on press P-7 trending up over the last hour, and which current jobs and open "
    "consignments are at risk if it fails?",
)
#: A containment request on the same renamed plant (the positive control).
TRIGGERED = ("Draft the containment list for press cycle K-9 and the press that ran it: the "
             "cycles, orders and consignments to hold, including anything the press is running "
             "now.")
#: Holds the rule must not widen: a hold scoped by the user, and holds for a
#: reason other than suspect product.
SCOPED_HOLD = "Hold press cycle K-4 until its failed gauge check is rerun."
NON_QUALITY_HOLDS = (
    ("Place sales order SO-9 on hold while credit is checked.", "credit"),
    ("Put order OR-12 on hold until the buyer confirms the ship-to address.", "order hold"),
    ("Quarantine supplier coil lot C-17 pending incoming inspection.", "incoming inspection"),
)


def test_rule_applies_only_to_a_quality_containment_request():
    low = _flat(_rule()).lower()
    assert low.split(": ", 1)[1].startswith(TRIGGER), low


def test_rule_keeps_a_scope_the_user_names():
    low = _flat(_rule()).lower()
    assert KEEPS_NAMED_SCOPE in low
    # The named scope is said after the principle it limits.
    assert low.index("report any boundary") < low.index(KEEPS_NAMED_SCOPE)


def test_rule_excludes_holds_for_other_reasons():
    assert NOT_A_CONTAINMENT in _flat(_rule()).lower()


@pytest.mark.parametrize("question", NOT_TRIGGERED)
def test_trace_and_impact_questions_do_not_meet_the_trigger_as_worded(question):
    """Guard on the wording: these questions carry none of the trigger's verbs."""
    for verb in ("contain", "hold", "quarantine"):
        assert verb in TRIGGER
    assert not _TRIGGER_VERBS.search(question), question


def test_a_containment_request_meets_the_trigger_as_worded():
    assert _TRIGGER_VERBS.search(TRIGGERED)
    assert not re.search(r"\b(?:only|just)\b", TRIGGERED)


def test_a_hold_the_user_scopes_is_kept_as_worded():
    """Guard on the wording: the request names the record to hold."""
    assert re.match(r"hold press cycle k-4\b", SCOPED_HOLD.lower())
    assert KEEPS_NAMED_SCOPE in _flat(_rule()).lower()


@pytest.mark.parametrize("question,reason", NON_QUALITY_HOLDS)
def test_a_hold_for_another_reason_is_excluded_as_worded(question, reason):
    """Guard on the wording: each hold carries a trigger verb, and its reason is
    one the rule says is not a containment."""
    assert _TRIGGER_VERBS.search(question), question
    assert reason.split()[0] in question.lower()
    assert reason in NOT_A_CONTAINMENT


def test_scope_questions_name_no_demo_literal():
    text = " ".join((*NOT_TRIGGERED, TRIGGERED, SCOPED_HOLD, *(q for q, _ in NON_QUALITY_HOLDS)))
    assert not re.search(r"\b(?:RUN|LOT|WO|SHIP|ORD)-\d", text)
    assert not re.search(r"\d{4}", text)
