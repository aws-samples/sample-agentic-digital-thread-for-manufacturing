# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A proposal's confidence word is stored the same way on every channel.

The agent states confidence as a word (low | medium | high). The proposal
store wants a float. Two channels turn the word into a number:

- the ``save_value_mapping`` / ``save_correlation`` tools
  (``digital_thread_server._confidence_to_float``): low 0.5, medium 0.7,
  high 0.9;
- the response-text blocks (``runtime_app._parse_vm_block`` for
  VALUE-MAPPING-PROPOSAL, ``runtime_app._extract_correlation_insights`` for
  CORRELATION-PROPOSAL), which stored ANY non-high word as 0.7.

So a proposal the agent marked ``low`` was indistinguishable from one it
marked ``medium`` once it arrived through a text block, and the same word
was stored differently depending on the channel. The orchestrator's
identifier-miss step 4 picks "the highest-confidence" accepted mapping, and
the review queue shows the stored number, so both read the collapsed value.

One table (``proposal_scoring.CONFIDENCE_WORD_FLOAT``) now serves every
channel. An absent or unrecognised word is stored as medium, as before, and
the text parsers keep their older rule that a word containing ``high`` is
high, so no proposal they stored as high is now stored lower.
"""

import pytest

from agents import digital_thread_server as dt_server
from agents import proposal_scoring
from agents.runtime_app import _extract_correlation_insights, _parse_vm_block

_CATALOG = [
    {"id": "cmms-a", "name": "Site CMMS"},
    {"id": "mes-a", "name": "Site MES"},
]

_WORDS = [
    ("low", 0.5),
    ("medium", 0.7),
    ("high", 0.9),
    ("LOW", 0.5),
    ("Medium", 0.7),
    ("HIGH", 0.9),
]


def _vm_block(confidence_line: str) -> str:
    lines = [
        "from_system: mes-a",
        "from_field: equipment.name",
        "from_value: M-07",
        "to_system: cmms-a",
        "to_field: assets.asset_num",
        "to_value: A-1001",
        "method: semantic_similarity",
    ]
    if confidence_line:
        lines.append(confidence_line)
    lines.append("reason: the only asset of that kind on the line in the CMMS")
    return "\n".join(lines)


def _correlation_block(confidence_line: str) -> str:
    body = "\n".join(filter(None, [
        "Pattern: unplanned stops in Site CMMS correlate with quality holds in Site MES",
        "Observed: holds follow unplanned stops within a shift",
        confidence_line,
    ]))
    return f"---CORRELATION-PROPOSAL---\n{body}\n---END-PROPOSAL---"


@pytest.mark.parametrize("word,expected", _WORDS)
def test_value_mapping_block_stores_the_word_it_was_given(word, expected):
    parsed = _parse_vm_block(_vm_block(f"confidence: {word}"))
    assert parsed is not None
    assert parsed["confidence"] == expected


@pytest.mark.parametrize("word,expected", _WORDS)
def test_correlation_block_stores_the_word_it_was_given(word, expected):
    out = _extract_correlation_insights(
        _correlation_block(f"Confidence: {word}"), _CATALOG)
    assert len(out) == 1
    assert out[0]["confidence"] == expected


@pytest.mark.parametrize("word", ["low", "medium", "high", "HIGH", "", "unsure"])
def test_text_block_and_tool_store_the_same_number(word):
    """The same word gives the same stored float on every channel."""
    tool = dt_server._confidence_to_float(word)
    vm = _parse_vm_block(_vm_block(f"confidence: {word}" if word else ""))
    assert vm is not None
    assert vm["confidence"] == tool
    corr = _extract_correlation_insights(
        _correlation_block(f"Confidence: {word}" if word else ""), _CATALOG)
    assert corr and corr[0]["confidence"] == tool


def test_low_stays_below_medium_after_parsing():
    low = _parse_vm_block(_vm_block("confidence: low"))
    medium = _parse_vm_block(_vm_block("confidence: medium"))
    assert low["confidence"] < medium["confidence"]


@pytest.mark.parametrize("line", ["", "confidence: unsure", "confidence: [n/a]"])
def test_absent_or_unknown_word_is_stored_as_medium(line):
    parsed = _parse_vm_block(_vm_block(line))
    assert parsed is not None
    assert parsed["confidence"] == proposal_scoring.CONFIDENCE_WORD_FLOAT["medium"]


@pytest.mark.parametrize("word", ["highest", "very_high", "High."])
def test_text_blocks_still_store_a_word_containing_high_as_high(word):
    """The text parsers' older rule (a word containing 'high' is high) is
    kept, so fixing low never lowers a proposal they stored as high."""
    high = proposal_scoring.CONFIDENCE_WORD_FLOAT["high"]
    parsed = _parse_vm_block(_vm_block(f"confidence: {word}"))
    assert parsed is not None and parsed["confidence"] == high
    out = _extract_correlation_insights(
        _correlation_block(f"Confidence: {word}"), _CATALOG)
    assert out and out[0]["confidence"] == high


def test_one_table_serves_every_channel():
    assert proposal_scoring.CONFIDENCE_WORD_FLOAT == {
        "low": 0.5, "medium": 0.7, "high": 0.9}
    for word, value in proposal_scoring.CONFIDENCE_WORD_FLOAT.items():
        assert proposal_scoring.confidence_word_to_float(word) == value
        assert dt_server._confidence_to_float(word) == value
    assert not hasattr(dt_server, "_CONFIDENCE_FLOAT")


def test_a_correlation_opener_under_a_rule_line_is_still_offered():
    """The display strips a marker alone on its line under a ``---`` rule, so
    the extractor must read the same block or the proposal silently vanishes."""
    body = ("Pattern: unplanned stops in Site CMMS correlate with quality holds in Site MES\n"
            "Observed: holds follow unplanned stops within a shift\nConfidence: high")
    out = _extract_correlation_insights(
        f"Answer.\n\n---\nCORRELATION-PROPOSAL\n{body}\n---END-PROPOSAL---\n", _CATALOG)
    assert len(out) == 1
    assert out[0]["confidence"] == 0.9
