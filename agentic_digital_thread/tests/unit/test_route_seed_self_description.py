# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route-seed origin note gives the model the words to use with a person.

The header carries a plain-words origin note (built from the
knowledge graph for this question, part of this message, not standing
instructions). On the next live run the assistant still answered an attendee
with "the connected systems in the route seed": the note said where the block
came from but never said what to call it, so the model repeated the label it
saw. The note must forbid the code-name and name the plain phrase instead.
"""

import agents.orchestrator as orch


def _headers():
    return (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER)


def test_origin_note_forbids_the_code_name_in_answers():
    for header in _headers():
        text = header.lower()
        assert 'never say "route seed"' in text


def test_origin_note_names_the_plain_phrase_to_use():
    for header in _headers():
        text = header.lower()
        assert "knowledge graph" in text
        assert "connected systems" in text


def test_headers_still_end_with_the_result_marker():
    # runtime_app parses the JSON after this literal; the note sits before it.
    assert orch._ROUTE_SEED_HEADER.endswith("ROUTE SEED RESULT:\n")
    assert orch._ROUTE_SEED_PARTIAL_HEADER.endswith("PARTIAL ROUTE SEED RESULT:\n")
