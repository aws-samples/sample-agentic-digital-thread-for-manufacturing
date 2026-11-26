# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Answer text never carries Unicode hyphen look-alikes (live rehearsal 2026-10-01).

Sonnet 5 wrote identifiers with U+2011 NON-BREAKING HYPHEN. They render the
same, but a copied run number no longer matches the record and the golden
regexes miss it. ``MessageEvent`` normalises U+2010/U+2011 to ``-`` and leaves
real dashes (punctuation) alone.
"""
import json

from agents.events import MessageEvent


def test_hyphen_look_alikes_become_ascii_hyphens():
    event = MessageEvent("RUN\u201120260929\u201190001 on L3\u2010M1")
    assert event.content == "RUN-20260929-90001 on L3-M1"
    assert json.loads(event.to_sse_format().split("data: ", 1)[1])["text"] == event.content


def test_dashes_and_plain_text_are_unchanged():
    text = "Line 3 \u2013 mill \u2014 2.8 mm/s, \u22121 % drift"
    assert MessageEvent(text).content == text


def test_a_non_string_payload_is_left_alone():
    assert MessageEvent(None).content is None
