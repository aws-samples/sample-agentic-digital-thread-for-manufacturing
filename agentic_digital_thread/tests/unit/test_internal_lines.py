# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``internal_lines``: which engine lines are kept off the surfaces a person
reads, and that everything else in the text is left as written."""
from __future__ import annotations

import json

from agents import (
    answer_ids,
    answer_names,
    result_identifiers,
    unresolved_miss,
    wrong_form_refused,
)
from agents.internal_lines import MARKERS, is_marker_line, public_text, strip_lines
from agents.public_answer import InternalTelemetryFilter, redact_internal_telemetry
from agents.tool_result_capture import RESULT_CUT_ITEMS_KEY, RESULT_CUT_NOTE_KEY

IDS = answer_ids.engine_line(["KLN-77-0042"])
NAMES = answer_names.engine_line(["Harbor Tile Works"])
OTHER = "[engine] 1 of 2 tool results this turn was cut off: the second."
#: The unpaired-forms line and the guard block that follows it.
UNPAIRED = f"{unresolved_miss.MARKER} figures were not paired by any result in this call."
BLOCK = "```\nIDENTIFIER-MISS:\ntoken: QZ-0042\nlooked_in: consumption.ref_code\n```"


def test_the_markers_are_the_five_orchestrator_only_lines():
    assert MARKERS == (answer_ids.MARKER, answer_names.MARKER, unresolved_miss.MARKER,
                       wrong_form_refused.MARKER, result_identifiers.MARKER)
    assert is_marker_line(IDS) and is_marker_line("  " + NAMES) and is_marker_line(UNPAIRED)
    assert is_marker_line(result_identifiers.line(["kln-77-0042"]))
    assert not is_marker_line(OTHER) and not is_marker_line(f"See {IDS}")


def test_the_unpaired_line_goes_and_its_block_stays():
    text = f"Lot fired.\n\n{UNPAIRED}\n\n{BLOCK}\n\n{OTHER}"
    assert strip_lines(text) == f"Lot fired.\n\n{BLOCK}\n\n{OTHER}"
    public, _ = redact_internal_telemetry(text)
    assert unresolved_miss.MARKER not in public and BLOCK in public


def test_text_without_a_marker_is_the_same_object():
    text = f"Lot KLN-77-0042 fired.\n\n{OTHER}"
    assert strip_lines(text) is text and public_text(text) is text


def test_the_line_and_the_blank_lines_before_it_go():
    assert strip_lines(f"Lot fired.\n\n{IDS}") == "Lot fired."
    assert strip_lines(f"Lot fired.\n\n{IDS}\n\n{OTHER}") == f"Lot fired.\n\n{OTHER}"
    assert strip_lines(f"{NAMES}\n\nLot fired.") == "Lot fired."
    assert strip_lines(f"Lot fired.\n\n{NAMES}\n\n{IDS}") == "Lot fired."


def test_a_mention_inside_a_sentence_stays():
    text = f"The note said {answer_ids.MARKER} nothing."
    assert strip_lines(text) == text


def test_the_json_note_member_loses_the_line_and_keeps_the_rest():
    payload = {"rows": [["KLN-77-0042", 3]], RESULT_CUT_NOTE_KEY: f"{OTHER}\n\n{IDS}"}
    out = json.loads(public_text(json.dumps(payload)))
    assert out == {"rows": [["KLN-77-0042", 3]], RESULT_CUT_NOTE_KEY: OTHER}


def test_an_empty_note_is_dropped_and_a_wrapped_array_unwrapped():
    only = {"rows": [], RESULT_CUT_NOTE_KEY: IDS}
    assert json.loads(public_text(json.dumps(only))) == {"rows": []}
    wrapped = {RESULT_CUT_ITEMS_KEY: [1, 2], RESULT_CUT_NOTE_KEY: NAMES}
    assert json.loads(public_text(json.dumps(wrapped))) == [1, 2]


def test_the_stream_filter_keeps_other_bracketed_lines():
    text = "[a link](https://example.com) here\n[engine] other\n[engine] Identi"
    filter_ = InternalTelemetryFilter()
    assert filter_.feed(text) + filter_.flush() == text


def test_the_stream_filter_drops_a_line_past_its_bound():
    long_line = answer_ids.MARKER + " x" * 2100
    public, _ = redact_internal_telemetry(f"Done.\n{long_line}\nAfter.")
    assert public == "Done.\nAfter."


def test_the_stream_filter_still_records_the_counter():
    public, counts = redact_internal_telemetry(
        f"Answer.\n\n{IDS}\n\nadditional_candidate_pairs_observed: 4\nEnd.")
    assert counts == [4]
    assert public == "Answer.\n\n\nEnd."
