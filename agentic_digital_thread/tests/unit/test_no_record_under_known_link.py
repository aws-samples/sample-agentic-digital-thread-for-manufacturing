# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No rows under a known link is "no record yet", never a translation gap.

Workshop walks, 2026-10-19 (two teams, three asks of one question and its
containment follow-up). A machine's live production run had not shipped yet,
so the ERP held no shipment rows for its run number. The orchestrator carried
the run number to the ERP along a confirmed join whose target field holds run
numbers (identical values measured in both systems at onboarding). The ERP
found no rows, and the answer said the run number was "a genuine data gap ...
no mapping exists to translate it" and that its "format differs from what's
mapped". It never answered the question's other ask, the open orders and
shipments of the part the machine builds.

Zero rows for a value sent in the form its field holds is an honest zero:
that system has no record of the value there yet. Three decision points
carry that, all generic:

1. The system agent's tool result (deterministic). An IDENTIFIER-MISS block
   whose looked_in fields are all the target of the request's own join_rule,
   with no observed values that contradict it, is replaced by one fixed
   no-record line. The join's measurement is the shape evidence the agent
   could not read from zero rows. Observed values of another form still win.
2. The system agent's ZERO-ROW LOOKUPS rule: the same distinction in words,
   and the request's other asks are still answered under their own scope.
3. The orchestrator's Mode-A delegate block: NO RECORD UNDER A KNOWN LINK.
   Not a miss, no translation lookup, never called a mapping, translation or
   format gap, and the question's other asks stay open at the scope the
   question gives them.

Fixtures use invented system, table, column and identifier names, two
unrelated sets, so nothing here leans on the workshop's data. Fully offline.
"""
import asyncio
import re

import pytest

import agents.orchestrator as orch
import agents.system_agent as sa
from agents.demo import story_lookup
from agents.workflow_guidance import compose_quick_system_prompt


def _flat(text: str) -> str:
    return " ".join(text.split())


def _miss_block(token: str, looked_in: str, my_format: str, context: str = "") -> str:
    """A fenced IDENTIFIER-MISS block; ``context`` adds the protocol's
    context line inside the block (the current format), none is the older
    format whose context sentence follows the block."""
    return ("```\n"
            "IDENTIFIER-MISS:\n"
            f"token: {token}\n"
            f"looked_in: {looked_in}\n"
            f"my_format: {my_format}\n"
            + (f"context: {context}\n" if context else "")
            + "```")


def _route(target: str, ids: str, join_rule: str) -> str:
    return ("```ROUTE:\n"
            f"target_system: {target}\n"
            f"translated_identifiers: {ids}\n"
            "canonical_concept: job\n"
            "scope: north yard\n"
            f"join_rule: {join_rule}\n"
            "```")


# Fixture set 1: a dispatch ledger keyed on a tracker's job codes.
_LEDGER = "ledger"
_JOB = "JB-0412-77"
_JOIN_1 = "jp-9 tracker.jobs.job_code -> ledger.dispatch_lines.job_ref"
_REQUEST_1 = (f"Which dispatch lines carry job {_JOB}?\n\n"
              + _route(_LEDGER, _JOB, _JOIN_1))
_NO_VALUES = "no non-null values observed for that column this session"

# Fixture set 2: a warehouse system keyed on a sorter's load tags, schema-qualified.
_WMS = "wms-east"
_TAG = "LT/2209/A"
_JOIN_2 = "jp-41 sorter-2.loads.load_tag -> wms-east.public.pallet_moves.load_tag"
_REQUEST_2 = (f"Open pallet moves for load {_TAG}\n\n" + _route(_WMS, _TAG, _JOIN_2))


def _fixed_line(token: str, field: str) -> str:
    return sa._known_link_no_record_line(token, field)


#: Wording that claims a miss: a mapping, translation or format gap.
_MISS_WORDS = re.compile(r"(?i)mapp|translat|format|vocabular|different form")


# --------------------------------------------------------------------------- #
# 1. The deterministic tool-result decision
# --------------------------------------------------------------------------- #
def test_block_on_the_join_target_becomes_the_no_record_line():
    answer = (_miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES,
                          context="No dispatch line carries that job.")
              + "\nThe north yard has two open lines.")
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert "IDENTIFIER-MISS" not in out and "```" not in out
    assert out == (_fixed_line(_JOB, "dispatch_lines.job_ref")
                   + "\nThe north yard has two open lines.")


def test_a_differently_named_schema_qualified_join_is_read_the_same_way():
    answer = (_miss_block(_TAG, "public.pallet_moves.load_tag", "no non-null values observed")
              + "\nNothing matched.")
    out = sa._resolve_known_link_misses(answer, _WMS, _REQUEST_2)
    assert "IDENTIFIER-MISS" not in out
    assert _fixed_line(_TAG, "public.pallet_moves.load_tag") in out


def test_the_no_record_line_says_only_no_record():
    line = _fixed_line(_JOB, "dispatch_lines.job_ref")
    assert line.startswith(f"No record of {_JOB} in dispatch_lines.job_ref yet")
    assert not _MISS_WORDS.search(line), _MISS_WORDS.search(line)
    assert "IDENTIFIER-MISS" not in line


def test_the_request_may_carry_the_route_in_kg_context_or_the_question():
    # _run_mode_a_query hands the guard both strings; either one may hold the ROUTE.
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    for request in (_REQUEST_1, "plain question\n" + _REQUEST_1):
        out = sa._resolve_known_link_misses(answer, _LEDGER, request)
        assert "IDENTIFIER-MISS" not in out, request


def test_a_field_the_join_rule_does_not_name_keeps_its_block():
    answer = _miss_block(_JOB, "dispatch_lines.batch_ref", _NO_VALUES) + "\nNo rows."
    assert sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1) == answer


def test_observed_values_of_another_form_keep_the_block():
    # The agent read the field and its values have a different shape: that is
    # evidence the join rule cannot outrank, so the miss stands.
    answer = _miss_block(_JOB, "dispatch_lines.job_ref",
                         "numeric job refs, e.g. 7741203, 7741219")
    assert sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1) == answer


def test_observed_values_of_the_same_form_also_give_the_no_record_line():
    # Without the join rule the shape guard drops such a block silently; under
    # the join rule the orchestrator gets the fixed line instead.
    answer = (_miss_block(_JOB, "dispatch_lines.job_ref", "codes like JB-0399-12, JB-0401-03")
              + "\nNo rows.")
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert "IDENTIFIER-MISS" not in out
    assert _fixed_line(_JOB, "dispatch_lines.job_ref") in out


def test_every_looked_in_field_must_be_a_join_target():
    answer = _miss_block(_JOB, "dispatch_lines.job_ref, crew_logs.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1) == answer


def test_a_join_into_another_system_is_no_evidence_here():
    # Same table and column names, but the join rule targets the source system.
    request = (f"Which dispatch lines carry job {_JOB}?\n\n"
               + _route(_LEDGER, _JOB, "jp-9 ledger.jobs.job_code -> tracker.dispatch_lines.job_ref"))
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


def test_a_system_id_that_only_ends_with_this_id_is_not_this_system():
    request = (f"jobs {_JOB}\n"
               + _route(_LEDGER, _JOB, "jp-9 tracker.jobs.job_code -> subledger.dispatch_lines.job_ref"))
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


def test_a_token_the_request_never_sent_keeps_its_block():
    answer = _miss_block("JB-9999-01", "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1) == answer


def test_no_join_rule_is_identity():
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    request = f"Which dispatch lines carry job {_JOB}?\n" + _route(_LEDGER, _JOB, "none")
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) is answer
    assert sa._resolve_known_link_misses("No rows.", _LEDGER, _REQUEST_1) == "No rows."


def test_only_the_qualifying_block_of_several_is_replaced():
    other = _miss_block("CREW-7", "crew_logs.crew_code", "codes like C-12, C-19")
    answer = (_miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES, context="First.")
              + "\nBetween.\n\n" + other + "\nSecond.")
    request = _REQUEST_1 + "\nand crew CREW-7"
    out = sa._resolve_known_link_misses(answer, _LEDGER, request)
    assert out.count("IDENTIFIER-MISS") == 1 and "token: CREW-7" in out
    assert out == (_fixed_line(_JOB, "dispatch_lines.job_ref") + "\nBetween.\n\n" + other
                   + "\nSecond.")


def test_malformed_input_fails_open():
    unclosed = "```\nIDENTIFIER-MISS:\ntoken: " + _JOB + "\nlooked_in: dispatch_lines.job_ref\n"
    assert sa._resolve_known_link_misses(unclosed, _LEDGER, _REQUEST_1) == unclosed
    assert sa._resolve_known_link_misses(None, _LEDGER, _REQUEST_1) is None
    no_looked_in = "```\nIDENTIFIER-MISS:\ntoken: " + _JOB + "\nmy_format: none\n```"
    assert sa._resolve_known_link_misses(no_looked_in, _LEDGER, _REQUEST_1) == no_looked_in


def test_the_echoed_field_and_token_cannot_carry_markup():
    line = _fixed_line("JB`x", "dispatch_lines.job_ref")
    assert "`" not in line and "\n" not in line


def test_every_mode_a_answer_passes_through_the_decision(monkeypatch):
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES) + "\nNo rows."
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, {}))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    out = asyncio.run(sa._run_mode_a_query(object(), _LEDGER, "Which dispatch lines?",
                                           _route(_LEDGER, _JOB, _JOIN_1)))
    assert "IDENTIFIER-MISS" not in out
    assert _fixed_line(_JOB, "dispatch_lines.job_ref") in out
    # Without the join rule the same answer keeps its block.
    out = asyncio.run(sa._run_mode_a_query(object(), _LEDGER, "Which dispatch lines?", ""))
    assert "IDENTIFIER-MISS" in out


# --------------------------------------------------------------------------- #
# 1b. The block is the boundary: its context line goes with it, nothing
#     outside it changes
# --------------------------------------------------------------------------- #
_UNRELATED = ("Open dispatch lines for the north yard: DL-88 (LOADING), DL-91 (QUEUED). "
              "Both are for the yard's crane order book.")

#: True sentences that carry words a miss claim also uses.
_TRUE_SENTENCES = (
    "The loading plan has a two-hour gap before the next open dispatch.",
    f"{_JOB} is mapped to open order ORD-7, which remains active.",
    "The dispatch report uses the standard yard format.",
)


@pytest.mark.parametrize("claim", [
    "Its format differs and no mapping exists to translate it.",
    "That identifier does not match this system's vocabulary, so it needs a translation.",
    f"{_JOB} is a genuine data gap: no mapping exists to translate it.",
    "This identifier has no counterpart in this system.",
    "I cannot connect this identifier to records here.",
])
def test_the_context_line_inside_the_block_goes_with_the_block(claim):
    answer = (_miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES, context=claim)
              + "\n\n" + _UNRELATED)
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == _fixed_line(_JOB, "dispatch_lines.job_ref") + "\n\n" + _UNRELATED
    assert claim not in out


@pytest.mark.parametrize("where", ["before", "after", "both"])
def test_true_sentences_outside_the_block_survive_byte_for_byte(where):
    before = " ".join(_TRUE_SENTENCES) + "\n\n" if where in ("before", "both") else ""
    after = "\n" + "\n".join(_TRUE_SENTENCES) if where in ("after", "both") else ""
    block = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES,
                        context="No counterpart for this identifier here.")
    out = sa._resolve_known_link_misses(before + block + after, _LEDGER, _REQUEST_1)
    assert out == before + _fixed_line(_JOB, "dispatch_lines.job_ref") + after
    for sentence in _TRUE_SENTENCES:
        assert out.count(sentence) == (2 if where == "both" else 1)


#: Independent sentences after an older block's one sentence of context.
_LATER = (f"{_TRUE_SENTENCES[0]} {_TRUE_SENTENCES[1]}\n{_TRUE_SENTENCES[2]}")


@pytest.mark.parametrize("claim", [
    "Its format differs and no mapping exists to translate it.",
    "This identifier has no counterpart in this system.",
    "I cannot connect this identifier to records here.",
])
@pytest.mark.parametrize("gap", ["\n", "\n\n"], ids=["next-line", "after-a-blank-line"])
@pytest.mark.parametrize("after", [" ", "\n", "\n\n"], ids=["same-line", "next-line", "paragraph"])
def test_an_older_block_takes_its_one_context_sentence_with_it(claim, gap, after):
    # The older format (no context: line) put its one sentence of context
    # right after the block. That sentence goes with the block; every byte
    # after it stays exactly as written.
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES) + gap + claim + after + _LATER
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == _fixed_line(_JOB, "dispatch_lines.job_ref") + after + _LATER


@pytest.mark.parametrize("sentence,rest", [
    ("Its format differs, e.g. it uses dashes.", " The loading plan has a two-hour gap."),
    ("Its format differs from this field's values", "\nThe report uses the standard format."),
    ("Is it another system's code?", " The plan is mapped to ORD-7."),
], ids=["abbreviation", "no-terminator-ends-at-line", "question"])
def test_the_context_sentence_ends_at_its_own_end(sentence, rest):
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES) + "\n" + sentence + rest
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == _fixed_line(_JOB, "dispatch_lines.job_ref") + rest


@pytest.mark.parametrize("rest", [
    "",
    "\n- DL-88 is open; its format is standard.",
    "\n| line | status |\n|---|---|\n| DL-88 | open |",
    "\n## Open dispatch lines\nDL-88 has a gap.",
    "\n```\nother code\n```",
    "\n\n\nTwo blank lines later, the plan is mapped to ORD-7.",
], ids=["end", "list", "table", "heading", "fence", "two-blank-lines"])
def test_what_is_not_a_context_sentence_stays_exactly(rest):
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES) + rest
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == _fixed_line(_JOB, "dispatch_lines.job_ref") + rest


def test_a_block_with_a_context_line_takes_nothing_from_outside():
    answer = (_miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES, context="Its format differs.")
              + "\n" + _LATER)
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == _fixed_line(_JOB, "dispatch_lines.job_ref") + "\n" + _LATER


def test_the_shape_guards_miss_block_carries_a_context_line():
    from agents import identifier_shape_guard as guard
    col = guard.Column("dispatch_lines", "ref_code", "unit", sa._shape_signature("QZ-9999"),
                       "QZ-9999")
    partner = guard.Column("consumption", "ref_code", "unit", sa._shape_signature("KX-9999"),
                           "KX-9999")
    block = guard._miss_block("KX-0042", col, partner)
    assert re.findall(r"^([a-z_]+):", block, re.MULTILINE) == \
        ["token", "looked_in", "my_format", "context"]
    fenced = f"```\n{block}\n```"
    assert sa._parse_identifier_miss_block(fenced) == ("KX-0042", ["QZ-9999"])
    assert sa._strip_false_identifier_miss(fenced) == fenced        # a real miss: kept


# --------------------------------------------------------------------------- #
# 1b'. One parser for the block's labelled fields
# --------------------------------------------------------------------------- #
_PO = "PO-20260708-76827"
_WO_FORMAT = "WO-<date>-<5 digits>, e.g. WO-20260707-00317, WO-20260701-00042"
_WO_SAMPLES = ["WO-20260707-00317", "WO-20260701-00042"]
_PO_ROUTE = (f"Which work orders carry {_PO}?\n\n"
             + _route(_LEDGER, _PO, "jp-3 tracker.jobs.po_ref -> ledger.work_orders.wo_number"))


def _po_block(context_line: str = "") -> str:
    return ("```\nIDENTIFIER-MISS:\n"
            f"token: {_PO}\n"
            "looked_in: work_orders.wo_number\n"
            f"my_format: {_WO_FORMAT}\n"
            + context_line
            + "```")


@pytest.mark.parametrize("label", [" context:", "\tcontext:", "  Context:", "CONTEXT :"])
def test_an_indented_context_line_never_adds_format_samples(label):
    # A same-shape identifier in the context sentence must never count as a
    # value the agent observed: that would turn this wrong-form miss into an
    # honest zero, and the recovery would never run.
    block = _po_block(f"{label} The request may mean PO-20260701-00042, a purchase order.\n")
    assert sa._parse_identifier_miss_block(block) == (_PO, _WO_SAMPLES)
    answer = block + "\nThat number is not one of this system's work orders."
    assert sa._strip_false_identifier_miss(answer) == answer
    assert sa._resolve_known_link_misses(answer, _LEDGER, _PO_ROUTE) == answer


def test_an_older_blocks_following_sentence_never_adds_format_samples():
    block = _po_block()
    answer = block + "\nThe request may mean PO-20260701-00042, a purchase order."
    assert sa._parse_identifier_miss_block(block) == (_PO, _WO_SAMPLES)
    assert sa._strip_false_identifier_miss(answer) == answer
    assert sa._resolve_known_link_misses(answer, _LEDGER, _PO_ROUTE) == answer


def test_only_the_protocol_labels_start_a_field():
    # One rule for every protocol label (leading whitespace, any case); any
    # other labelled line continues the field before it.
    block = ("```\nIDENTIFIER-MISS:\n"
             f"  Token: {_JOB}\n"
             "\tLOOKED_IN: dispatch_lines.job_ref\n"
             " My_Format: job refs, e.g. JB-0399-12,\n"
             "   and JB-0401-03\n"
             "  note: also seen JB-0500-01\n"
             "  context: That job looks like another system's code.\n"
             "```")
    fields = sa._miss_fields(block)
    assert list(fields) == ["token", "looked_in", "my_format", "context"]
    assert fields["token"].strip() == _JOB
    assert "note: also seen JB-0500-01" in fields["my_format"]
    assert sa._parse_identifier_miss_block(block) == \
        (_JOB, ["JB-0399-12", "JB-0401-03", "JB-0500-01"])


# --------------------------------------------------------------------------- #
# 1b''. Formatting of the format field never decides the outcome
# --------------------------------------------------------------------------- #
_WO_EXAMPLES = "WO-20260707-00317, WO-20260701-00042"


@pytest.mark.parametrize("label", [
    "Examples:", "  Examples:", "Pattern:", "\tPattern:", "EXAMPLES:", "Values:", "Seen:",
    "e.g.:", "  Sample_values :"])
@pytest.mark.parametrize("context", ["", "That number looks like a purchase order."],
                         ids=["older-block", "context-line"])
def test_a_labelled_examples_line_still_holds_the_format_samples(label, context):
    # A genuine wrong-form miss whose observed values sit on a labelled line
    # after my_format: the values are the evidence, so the miss stays, byte
    # for byte, through the shape guard and through the no-record path under
    # a ROUTE whose join names the looked-in field.
    block = ("```\nIDENTIFIER-MISS:\n"
             f"token: {_PO}\n"
             "looked_in: work_orders.wo_number\n"
             "my_format: work order identifiers\n"
             f"{label} {_WO_EXAMPLES}\n"
             + (f"context: {context}\n" if context else "")
             + "```")
    answer = block + "\nThat number is not one of this system's work orders."
    assert sa._parse_identifier_miss_block(block) == (_PO, _WO_SAMPLES)
    assert sa._strip_false_identifier_miss(answer) == answer
    assert sa._resolve_known_link_misses(answer, _LEDGER, _PO_ROUTE) == answer


@pytest.mark.parametrize("my_format", [
    "work order identifiers",
    "short internal codes assigned by the planning module",
    "UUIDs",
    "none observed",
    "values look like the planner's own numbering",
])
@pytest.mark.parametrize("context", ["", "It may be another system's number."],
                         ids=["older-block", "context-line"])
def test_a_format_without_samples_or_the_no_values_wording_stays_a_miss(my_format, context):
    # Zero parsed samples is no evidence that the field holds the token's
    # form: only the protocol's own no-values wording is.
    answer = (_miss_block(_JOB, "dispatch_lines.job_ref", my_format, context=context)
              + "\nThat job is not one of this system's dispatch refs.")
    assert sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1) == answer
    assert sa._strip_false_identifier_miss(answer) == answer


@pytest.mark.parametrize("my_format", [
    "no non-null values observed",
    "No non-null values observed for that column this session.",
    "no non-null values were observed",
    "no  non-null\n  values observed",
])
@pytest.mark.parametrize("older", [True, False], ids=["older-block", "context-line"])
def test_the_no_values_wording_takes_the_no_record_path(my_format, older):
    # An older block's one context sentence follows it and goes with it.
    context = "No dispatch line carries it."
    block = _miss_block(_JOB, "dispatch_lines.job_ref", my_format,
                        context="" if older else context)
    answer = block + ("\n" + context if older else "") + "\nThe north yard has two open lines."
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == (_fixed_line(_JOB, "dispatch_lines.job_ref")
                   + "\nThe north yard has two open lines.")


def test_the_protocol_names_the_exact_no_values_wording():
    flat = _flat(sa._identifier_miss_prompt())
    assert ('If you observed no non-null values, write exactly "no non-null values observed" '
            "instead of describing a format") in flat


@pytest.mark.parametrize("label", ["context:", "  Context:", "\tCONTEXT :"])
def test_a_labelled_line_after_context_never_feeds_the_format(label):
    # Any line after the context field continues that field, labelled or not.
    block = ("```\nIDENTIFIER-MISS:\n"
             f"token: {_PO}\n"
             "looked_in: work_orders.wo_number\n"
             f"my_format: {_WO_FORMAT}\n"
             f"{label} The request may mean a purchase order.\n"
             "Examples: PO-20260701-00042, PO-20260702-00043\n"
             "```")
    assert sa._parse_identifier_miss_block(block) == (_PO, _WO_SAMPLES)
    answer = block + "\nThat number is not one of this system's work orders."
    assert sa._strip_false_identifier_miss(answer) == answer
    assert sa._resolve_known_link_misses(answer, _LEDGER, _PO_ROUTE) == answer


def test_a_kept_miss_block_and_its_context_stay_byte_identical():
    kept = _miss_block("CREW-7", "crew_logs.crew_code", "codes like C-12, C-19",
                       context="CREW-7 does not look like this system's crew codes.")
    replaced = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES,
                           context="Its format differs.")
    answer = replaced + "\n\n" + kept + "\nCREW-7 may be another system's code."
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1 + "\nand crew CREW-7")
    assert out == (_fixed_line(_JOB, "dispatch_lines.job_ref") + "\n\n" + kept
                   + "\nCREW-7 may be another system's code.")
    alone = kept + "\nCREW-7 may be another system's code."
    assert sa._resolve_known_link_misses(alone, _LEDGER, _REQUEST_1) is alone


def test_the_protocol_puts_its_context_sentence_inside_the_block():
    prompt = sa._identifier_miss_prompt()
    block = prompt[prompt.index("```\nIDENTIFIER-MISS:"):]
    block = block[:block.index("\n```", 4) + 4]
    labels = re.findall(r"^([a-z_]+):", block, re.MULTILINE)
    assert labels == ["token", "looked_in", "my_format", "context"], labels
    flat = _flat(prompt)
    assert ("Return this fenced block instead, with your one short sentence of context on "
            "its context: line and nothing about this identifier outside it") in flat
    assert "then one short sentence of context" not in flat


def test_the_context_line_never_becomes_a_format_sample():
    # The my_format samples are read up to the next labelled line, so a
    # context sentence holding identifier-shaped text adds no sample.
    block = _miss_block(_JOB, "dispatch_lines.job_ref", "codes like 7741203, 7741219",
                        context=f"{_JOB} resembles JB-0399-12 from the tracker.")
    token, samples = sa._parse_identifier_miss_block(block)
    assert token == _JOB and samples == ["7741203", "7741219"]


def test_a_false_positive_block_with_context_leaves_the_no_record_line():
    # The shape guard drops a block whose token has its own field's shape. A
    # block that carries its context sentence would leave nothing behind, so
    # it leaves the no-record line; an older block is removed as before.
    block = _miss_block(_JOB, "dispatch_lines.job_ref", "codes like JB-0399-12, JB-0401-03",
                        context="That job looks like another system's code.")
    assert sa._strip_false_identifier_miss(block) == _fixed_line(_JOB, "dispatch_lines.job_ref")
    older = (_miss_block(_JOB, "dispatch_lines.job_ref", "codes like JB-0399-12, JB-0401-03")
             + "\nNo dispatch line carries that job.")
    assert sa._strip_false_identifier_miss(older) == "No dispatch line carries that job."


# --------------------------------------------------------------------------- #
# 1c. The evidence matcher: the ROUTE block only, the exact token, the full field
# --------------------------------------------------------------------------- #
def _request(join_rule: str, sent: str = _JOB, target: str = _LEDGER) -> str:
    return f"Which dispatch lines carry job {sent}?\n\n" + _route(target, sent, join_rule)


def test_a_fully_qualified_field_in_another_schema_stays_a_miss():
    request = _request("jp-9 tracker.jobs.job_code -> ledger.public.dispatch_lines.job_ref")
    answer = _miss_block(_JOB, "archive.dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


def test_a_schema_the_route_does_not_name_must_be_the_systems_own():
    request = _request(_JOIN_1)
    answer = _miss_block(_JOB, "archive.dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request, schema="public") == answer
    out = sa._resolve_known_link_misses(answer, _LEDGER, request, schema="archive")
    assert "IDENTIFIER-MISS" not in out


def test_a_bare_table_column_matches_only_one_route_target():
    one = _request("jp-9 tracker.jobs.job_code -> ledger.public.dispatch_lines.job_ref")
    two = _request("jp-9 tracker.jobs.job_code -> ledger.public.dispatch_lines.job_ref;\n"
                   "  jp-10 tracker.jobs.job_code -> ledger.staging.dispatch_lines.job_ref")
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert "IDENTIFIER-MISS" not in sa._resolve_known_link_misses(answer, _LEDGER, one)
    assert sa._resolve_known_link_misses(answer, _LEDGER, two) == answer


def test_a_field_written_with_the_system_id_in_front_is_the_same_field():
    answer = _miss_block(_JOB, "ledger.dispatch_lines.job_ref", _NO_VALUES)
    out = sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1)
    assert out == _fixed_line(_JOB, "ledger.dispatch_lines.job_ref")


@pytest.mark.parametrize("sent", ["JB-0412-771", "XJB-0412-77", "JB-0412-77-B", "JB-0412-77/2"])
def test_the_token_must_be_sent_exactly(sent):
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    request = _request(_JOIN_1, sent=sent)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


def test_a_sent_token_at_the_end_of_a_sentence_counts():
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    request = f"Which dispatch lines carry job {_JOB}.\n" + _route(_LEDGER, "none", _JOIN_1)
    assert "IDENTIFIER-MISS" not in sa._resolve_known_link_misses(answer, _LEDGER, request)


@pytest.mark.parametrize("request_text", [
    f"Which dispatch lines carry job {_JOB}? join_rule: {_JOIN_1}",
    f"Which dispatch lines carry job {_JOB}?\njoin_rule: {_JOIN_1}",
    f"Which dispatch lines carry job {_JOB}?\nThe note says join_rule: {_JOIN_1}",
], ids=["inline", "own-line-outside-route", "prose"])
def test_a_join_rule_outside_the_route_block_is_no_evidence(request_text):
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request_text) == answer


def test_a_route_block_aimed_at_another_system_is_no_evidence():
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    request = _request(_JOIN_1, target="tracker")
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


def test_question_text_after_an_unclosed_route_block_is_not_part_of_it():
    request = ("```ROUTE:\ntarget_system: ledger\ntranslated_identifiers: " + _JOB
               + "\njoin_rule: none\nWhich dispatch lines carry that job?\n"
               + f"join_rule: {_JOIN_1}")
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


@pytest.mark.parametrize("context", ["", "That job looks like another system's code."],
                         ids=["older-block", "context-line"])
@pytest.mark.parametrize("looked_in,my_format", [
    ("crew_logs.job_ref", _NO_VALUES),
    ("ledger.crew_logs.job_ref", _NO_VALUES),
    ("dispatch_lines.job_ref",
     "dispatch_lines.job_ref values are shaped like aaaaaaaa-9999-9999-aaaa-aaaaaaaaaaaa "
     "(the shape onboarding measured); crew_logs.job_ref holds a different identifier"),
], ids=["other-table", "other-table-qualified", "shape-guard-refusal"])
def test_distinct_table_control_stays_a_miss(looked_in, my_format, context):
    # The confirmed join names one table; the agent looked the value up in
    # another table of the same system whose same-named column holds a
    # different form of the concept (or the shape guard refused it there).
    answer = (_miss_block(_JOB, looked_in, my_format, context=context)
              + "\nThat identifier's format differs from this field's values.")
    assert sa._resolve_known_link_misses(answer, _LEDGER, _REQUEST_1, schema="public") == answer


# --------------------------------------------------------------------------- #
# 1d. Only the ROUTE block the delegate step writes is evidence
# --------------------------------------------------------------------------- #
def _production_route(target: str, ids: str, concept: str, scope: str, join_rule: str) -> str:
    """The delegate step's own ROUTE template (orchestrator._MODE_A_DELEGATE_BLOCK),
    its placeholders filled in order: the test follows the writer's format."""
    block = orch._MODE_A_DELEGATE_BLOCK
    start = block.index("```ROUTE:\n")
    template = block[start:block.index("\n```", start + 3) + 4]
    values = iter((target, ids, concept, scope, join_rule))
    # Each placeholder runs from "<" to the ">" that ends its value, before
    # the next label or the closing fence (the join_rule one wraps and holds "->").
    return re.sub(r"<.*?>(?=\n(?:[a-z_]+:|```))", lambda _m: next(values), template,
                  flags=re.DOTALL)


_WRAPPED_RULE = ("jp-7 tracker.jobs.alt_code -> ledger.crew_logs.job_ref,\n"
                 "  jp-9 tracker.jobs.job_code -> ledger.dispatch_lines.job_ref")


def test_the_production_route_template_has_the_parsed_shape():
    route = _production_route(_LEDGER, _JOB, "job", "north yard", _WRAPPED_RULE)
    lines = route.splitlines()
    assert lines[0] == "```ROUTE:" and lines[-1] == "```"
    assert [ln.split(":")[0] for ln in lines[1:6]] == list(sa._ROUTE_LABELS)
    assert lines[6].startswith("  jp-9 ")


@pytest.mark.parametrize("carrier", ["question", "kg_context"])
def test_a_production_route_block_with_a_wrapped_join_rule_is_evidence(carrier):
    route = _production_route(_LEDGER, _JOB, "job", "north yard", _WRAPPED_RULE)
    question = f"Which dispatch lines carry job {_JOB}?"
    request = (question + "\n\n" + route) if carrier == "question" else (question + "\n" + route)
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == \
        _fixed_line(_JOB, "dispatch_lines.job_ref")


def _bad_route(kind: str) -> str:
    good = _production_route(_LEDGER, _JOB, "job", "north yard", _JOIN_1).splitlines()
    if kind == "unfenced":
        lines = ["ROUTE:"] + good[1:-1]
    elif kind == "unclosed":
        lines = good[:-1]
    elif kind == "tilde-fence":
        lines = ["~~~ROUTE:"] + good[1:-1] + ["~~~"]
    elif kind == "indented-opening":
        lines = ["  ```ROUTE:"] + good[1:]
    elif kind == "missing-target":
        lines = [ln for ln in good if not ln.startswith("target_system:")]
    elif kind == "empty-target":
        lines = ["target_system: " if ln.startswith("target_system:") else ln for ln in good]
    elif kind == "missing-label":
        lines = [ln for ln in good if not ln.startswith("scope:")]
    elif kind == "repeated-label":
        lines = good[:-1] + [f"join_rule: {_JOIN_1}", "```"]
    elif kind == "stray-line":
        lines = good[:-1] + ["Also check the yard's other jobs.", "```"]
    elif kind == "blank-line":
        lines = good[:-1] + ["", "```"]
    else:
        raise AssertionError(kind)
    return "\n".join(lines)


@pytest.mark.parametrize("kind", [
    "unfenced", "unclosed", "tilde-fence", "indented-opening", "missing-target",
    "empty-target", "missing-label", "repeated-label", "stray-line", "blank-line"])
def test_a_route_block_the_writer_never_emits_is_no_evidence(kind):
    request = f"Which dispatch lines carry job {_JOB}?\n\n" + _bad_route(kind)
    answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
    assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer


def test_the_target_system_must_equal_this_system_exactly():
    for target in ("ledger-2", "Ledger", "ledger, tracker", "`ledger`"):
        request = (f"Which dispatch lines carry job {_JOB}?\n\n"
                   + _production_route(target, _JOB, "job", "north yard", _JOIN_1))
        answer = _miss_block(_JOB, "dispatch_lines.job_ref", _NO_VALUES)
        assert sa._resolve_known_link_misses(answer, _LEDGER, request) == answer, target


def test_every_mode_a_answer_passes_its_schema_to_the_decision(monkeypatch):
    seen = {}

    def spy(text, system_id, request, schema=""):
        seen.update(system_id=system_id, schema=schema)
        return text

    monkeypatch.setattr(sa, "_resolve_known_link_misses", spy)
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: ("ok", {}))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    asyncio.run(sa._run_mode_a_query(object(), _LEDGER, "q", "", schema="public"))
    assert seen == {"system_id": _LEDGER, "schema": "public"}


# --------------------------------------------------------------------------- #
# 2. The system agent's rules
# --------------------------------------------------------------------------- #
def test_zero_row_rule_tells_a_right_form_zero_from_a_miss():
    rule = _flat(sa._zero_row_lookup_prompt())
    for fragment in (
            "If that field holds that form (its values have it, or the request's join rule "
            "names it), say this system has no record of it there yet, never a missing mapping "
            "or another format",
            "Still answer the asks that do not depend on it, each under its own scope",
            "never fill the gap from other records"):
        assert fragment in rule, fragment
    assert "the request's other asks" not in rule


def test_identifier_miss_honest_zero_case_counts_the_join_rule_field():
    text = _flat(sa._identifier_miss_prompt())
    assert ("A field the request's join rule names for the identifier holds its form: "
            "the join measured the same values in both systems") in text


# --------------------------------------------------------------------------- #
# 3. The orchestrator's Mode-A rule
# --------------------------------------------------------------------------- #
_RULE = "NO RECORD UNDER A KNOWN LINK"
_MODE_A = {"full-agent": lambda: orch._build_orchestrator_prompt("agent"),
           "quick-agent": lambda: compose_quick_system_prompt("agent")}
_MODE_B = {"full-orchestrator": lambda: orch._build_orchestrator_prompt("orchestrator"),
           "quick-orchestrator": lambda: compose_quick_system_prompt("orchestrator")}


@pytest.mark.parametrize("name", list(_MODE_A))
def test_mode_a_carries_the_rule_once_before_the_recovery(name):
    flat = _flat(_MODE_A[name]())
    assert flat.count(_RULE + ":") == 1, name
    assert flat.index(_RULE + ":") < flat.index("IDENTIFIER-MISS RECOVERY — BOUNDED")


@pytest.mark.parametrize("name", list(_MODE_A))
@pytest.mark.parametrize("fragment", [
    "no rows for a value you sent in the form its looked-up field holds is not a miss: that "
    "system has no record of the value there yet",
    "The form is known when you carried the value along a confirmed join_rule to that rule's "
    "target field, or when the IDENTIFIER FORMS block gives that field the value's form",
    "make no translation lookup or follow-up for that value, and never call it a missing "
    "mapping or translation or a different format",
    "A value a confirmed join carries needs no ValueMapping",
    "labelled with that scope, never as the value's own records",
])
def test_mode_a_rule_says_what_to_do(name, fragment):
    assert fragment in _flat(_MODE_A[name]()), (name, fragment)


# Only independently requested asks stay open. Customers of the value's own
# record depend on the empty value; an ask the question makes at a scope it
# names beside the value (the asset or product) does not.
_DEPENDENT = ("An ask about that value's own records depends on it and closes with this "
              "finding: answer it as no record, never from a wider scope (for example, which "
              "customers received that record)")
_INDEPENDENT = ("An ask the question makes separately, at a scope it names itself, does not "
                "depend on it and stays open (for example, open orders for the asset or product "
                "it names beside the value)")


@pytest.mark.parametrize("name", list(_MODE_A))
def test_a_dependent_ask_closes_with_the_no_record_finding(name):
    flat = _flat(_MODE_A[name]())
    assert _DEPENDENT in flat, name


@pytest.mark.parametrize("name", list(_MODE_A))
def test_only_an_independent_ask_stays_open(name):
    flat = _flat(_MODE_A[name]())
    assert _INDEPENDENT in flat, name
    rule = flat[flat.index(_RULE + ":"):flat.index("IDENTIFIER VALUE MAPPINGS:")]
    # A rule that keeps every wider-scope ask open keeps a dependent one open too.
    assert "The question's other asks stay open" not in rule
    assert "or that the record is for" not in rule
    assert rule.index(_DEPENDENT) < rule.index(_INDEPENDENT)


@pytest.mark.parametrize("name", list(_MODE_A))
def test_the_recovery_triggers_point_at_the_rule(name):
    flat = _flat(_MODE_A[name]())
    start = flat.index("IDENTIFIER-MISS RECOVERY — BOUNDED, DISCLOSED, NEVER SILENT")
    head = flat[start:flat.index("1. Make ONE explore_graph", start)]
    assert ("Any other empty result or plain 'no data' triggers nothing, and neither does no "
            f"rows for a value in the form its field holds ({_RULE})") in head
    assert "No record of" in head and "a reply's no-record line is not a miss" in head


@pytest.mark.parametrize("name", list(_MODE_B))
def test_mode_b_has_no_join_rule_and_so_no_rule(name):
    assert _RULE not in _MODE_B[name](), name


def _rule_paragraph() -> str:
    block = orch._MODE_A_DELEGATE_BLOCK
    start = block.index(_RULE)
    end = block.find("\n\n", start)
    return block[start:end if end > 0 else len(block)]


@pytest.mark.parametrize("text", [_rule_paragraph, sa._zero_row_lookup_prompt],
                         ids=["orchestrator-rule", "zero-row-rule"])
def test_rules_are_generic(text):
    window = text()
    assert not re.search(r"\b(?:RUN|LOT|WO|SHIP|PO|MACH)-\w", window)
    assert not re.search(r"\b(?:ERP|MES|CMMS)\b", window)
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, "shipment", "batch"):
        assert token.lower() not in window.lower(), token
