# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The card-number redactor must never eat a letter-prefixed record identifier,
and a prefix must never smuggle a real card number past it.

Observed (rehearsal R2): a final answer rendered "Batch: WO-[REDACTED_CARD]"
for a work-order id whose digits after the prefix happened to be 13 long.

Rule: a Luhn-valid standalone digit run is redacted UNLESS all of
  (a) it ends a letter-led hyphenated prefix (``WO-``, ``RUN-2026-``),
  (b) no segment of that prefix is a card keyword (``card-``, ``CC-``, ``Visa-``),
  (c) the run is not card-shaped (4-4-4-4, 4-6-5, or ungrouped 15-16 digits).
"""

import pytest

from agents import pii

# 8-5 identifier tail whose 13 digits are Luhn-VALID by construction, so the
# identifier skip (not the checksum) is what keeps it visible.
_LUHN_VALID_ID_TAIL = "20260928-20649"


@pytest.mark.parametrize("ident", [
    "WO-20260928-20644",
    "RUN-20260927-90001",
    "WO-20260927-90001",
    f"WO-{_LUHN_VALID_ID_TAIL}",
    f"RUN-2026-{_LUHN_VALID_ID_TAIL}",
    f"lot_A1-{_LUHN_VALID_ID_TAIL}",
])
def test_letter_prefixed_identifiers_are_never_cards(ident):
    text = f"Batch: {ident} shipped."
    assert pii.redact_pii(text) == text


def test_luhn_valid_tail_would_otherwise_be_redacted():
    # Guard the fixture: without the prefix the same run IS redacted.
    assert pii.redact_pii(f"x {_LUHN_VALID_ID_TAIL} y") == "x [REDACTED_CARD] y"


def test_observed_answer_lines_survive():
    line = "Batch: WO-20260928-20644 and batch RUN-20260927-90001 (1001-CMX-441)"
    assert pii.redact_pii(line) == line


# A prefix never exempts a card-SHAPED run: identifier ids are not written in
# card grouping, so ``SHIP-4111111111111111`` (formerly kept) is now treated
# as a card number that merely has a label glued on.
@pytest.mark.parametrize("text, expected", [
    ("card-4111111111111111", "card-[REDACTED_CARD]"),
    ("CC-4111111111111111", "CC-[REDACTED_CARD]"),
    ("Visa-4111 1111 1111 1111", "Visa-[REDACTED_CARD]"),
    ("a1b2-4111111111111111", "a1b2-[REDACTED_CARD]"),
    ("SHIP-4111111111111111", "SHIP-[REDACTED_CARD]"),
    ("SHIP-4111-1111-1111-1111", "SHIP-[REDACTED_CARD]"),
    ("lot_A1-4111 1111 1111 1111", "lot_A1-[REDACTED_CARD]"),
    ("ORD-20260927-4111111111111111", "ORD-20260927-[REDACTED_CARD]"),
    ("ref-3782-822463-10005", "ref-[REDACTED_CARD]"),
    ("WO-378282246310005", "WO-[REDACTED_CARD]"),
])
def test_card_shaped_run_behind_a_prefix_is_redacted(text, expected):
    assert pii.redact_pii(f"Batch: {text} shipped.") == f"Batch: {expected} shipped."


@pytest.mark.parametrize("keyword", [
    "card", "CC", "visa", "MasterCard", "mc", "AMEX", "discover",
    "pan", "acct", "Account", "number", "no", "num",
])
def test_card_keyword_prefix_never_exempts_a_luhn_valid_run(keyword):
    text = f"on file {keyword}-{_LUHN_VALID_ID_TAIL} ok"
    assert pii.redact_pii(text) == f"on file {keyword}-[REDACTED_CARD] ok"


def test_card_keyword_anywhere_in_the_prefix_chain_counts():
    text = f"x ACCT-X7-{_LUHN_VALID_ID_TAIL} y"
    assert pii.redact_pii(text) == "x ACCT-X7-[REDACTED_CARD] y"


@pytest.mark.parametrize("card", [
    "4111111111111111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "5555555555554444",
    "378282246310005",
    "3782 822463 10005",
])
def test_standalone_luhn_valid_card_is_redacted(card):
    out = pii.redact_pii(f"card {card} on file")
    assert out == "card [REDACTED_CARD] on file"


def test_luhn_invalid_digit_run_is_not_a_card():
    text = "serial 4111111111111112 logged"
    assert pii.redact_pii(text) == text
    assert pii.redact_pii("CC-4111111111111112") == "CC-4111111111111112"


def test_card_at_start_and_end_of_text():
    assert pii.redact_pii("4111111111111111") == "[REDACTED_CARD]"
    assert pii.redact_pii("x: 4111111111111111.") == "x: [REDACTED_CARD]."
