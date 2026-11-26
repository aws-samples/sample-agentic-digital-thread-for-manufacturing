# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regressions for the personal-data screen over record identifiers and dates.

The phone and card screens must keep catching real phone and card numbers,
including when a card is written glued to a label, while no longer treating
ordinary hyphenated record identifiers or ISO-8601 dates and date-times as
phone or card numbers. The field-name secret screen must keep every credential
key name sensitive (fail closed on an unknown ``...key`` name) while exempting
a narrow, generic set of record-identifier key forms.

Every identifier, date and card value below is a made-up generic shape; none
occurs in the demo data (erp_simulation, lambda-db-init or the mocks).
"""
from __future__ import annotations

import pytest

from agents import response_integrity_security as ris


# Real phone numbers the screen must still flag: E.164, punctuation, prose,
# JSON and a Markdown table cell (the review bar).
PHONE_VALUES = [
    "+14155550123",
    "+1 415 555 0123",
    "+1-415-555-0123",
    "+44 20 7946 0958",
    "(415) 555-0123",
    "415-555-0123",
    "415.555.0123",
    "415 555 0123",
    "+1 (415) 555-0123",
    "4155550123",
    "call 415-555-0123 today",
    "reach us at 415-555-0123.",
    "tel:415-555-0123",
    "phone=415-555-0123",
    '{"phone": "+14155550123"}',
    "| Contact | +1 415 555 0123 |",
]

# Record identifiers and ISO dates/date-times the screen must NOT flag. Made-up
# generic shapes; proven absent from the demo data.
NON_PHONE_VALUES = [
    "ZZX-70010003-44221",
    "QWO-88120099-0007",
    "KYT-2200-0042",
    "ABC-998877665",
    "SN-55667788",
    "2042-07-19",
    "2042-07-19 11:02:33",
    "2042-07-19T11:02:33",
    "shipped 2042-07-19 and arrived",
    "window 2042-07-19 to 2042-07-30",
    "run ZZX-70010003-44221 done",
]

# An extension or label glued to a phone by a letter. dedb4d03 already did NOT
# match this (its (?<!\w) guard rejects a letter right before the digits), so
# this is existing behavior, not a regression, and keeping it means a letter-
# glued identifier such as a serial code stays unmatched too.
LETTER_GLUED_PHONE_EXISTING_MISS = [
    "x415-555-0123",
    "ext415-555-0123",
]


@pytest.mark.parametrize("value", PHONE_VALUES)
def test_real_phone_numbers_are_still_sensitive(value):
    assert ris.sensitive_text(value), value


@pytest.mark.parametrize("value", PHONE_VALUES)
def test_real_phone_numbers_are_still_redacted(value):
    assert "[REDACTED_PHONE]" in ris.redact_sensitive(value), value


@pytest.mark.parametrize("value", NON_PHONE_VALUES)
def test_record_identifiers_and_dates_are_not_phone_numbers(value):
    # Pre-fix: _PHONE_RE matched the digit run after a hyphen in an identifier,
    # and matched ISO dates and the head of a timestamp, so these were reported
    # as phone numbers and dropped from captured evidence under enforce.
    assert not ris.sensitive_text(value), value


@pytest.mark.parametrize("value", NON_PHONE_VALUES)
def test_record_identifiers_and_dates_are_not_redacted(value):
    assert "[REDACTED_PHONE]" not in ris.redact_sensitive(value), value


@pytest.mark.parametrize("value", LETTER_GLUED_PHONE_EXISTING_MISS)
def test_letter_glued_phone_is_not_matched_existing_behavior(value):
    # Guard for behavior unchanged since dedb4d03: a letter glued directly to
    # the digits (no separator) is not treated as a phone number, so a letter-
    # prefixed glued identifier is not matched either.
    assert not ris.sensitive_text(value), value


# Real card numbers the screen must still flag: spaced, hyphenated, unseparated,
# in prose, colon-labelled, glued to a label, and in JSON.
CARD_VALUES = [
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111111111111111",
    "378282246310005",
    "pay with 4111 1111 1111 1111 now",
    "card: 4111111111111111",
    "card4111111111111111",
    '{"pan": "4111111111111111"}',
]

# Hyphen-joined identifier tails long enough to look like a card must NOT match.
NON_CARD_VALUES = [
    "ZZX-70010003-44221",
    "QWO-88120099-0001",
    "AAA-1234567890-55",
    "2042-07-19 11:02:33",
]


@pytest.mark.parametrize("value", CARD_VALUES)
def test_real_card_numbers_are_still_sensitive(value):
    assert ris.sensitive_text(value), value


@pytest.mark.parametrize("value", CARD_VALUES)
def test_real_card_numbers_are_still_redacted(value):
    # The digits are detected and masked. redact_sensitive runs the phone
    # substitution before the card one, so a space- or unseparated card run is
    # labelled [REDACTED_PHONE] while a label-glued run is [REDACTED_NUMBER];
    # either way the raw digit run is gone.
    out = ris.redact_sensitive(value)
    assert "[REDACTED_" in out, out
    digits = "".join(ch for ch in value if ch.isdigit())
    assert digits not in "".join(ch for ch in out if ch.isdigit()), out


@pytest.mark.parametrize("value", NON_CARD_VALUES)
def test_hyphen_joined_identifier_tails_are_not_card_numbers(value):
    assert "[REDACTED_NUMBER]" not in ris.redact_sensitive(value), value


# Every credential key name must stay sensitive. An unknown "...key" field is
# sensitive by default (fail closed).
SECRET_KEY_FIELDS = [
    "api_key", "x-api-key", "apiKey", "access_key", "secret_key",
    "signing_key", "encryption_key", "decryption_key", "private_key",
    "session_key", "master_key", "client_key", "auth_key", "license_key",
    "hmac_key", "ssh_key", "pgp_key", "kms_key", "tls_key", "ssl_key",
    "jwt_key", "aes_key", "ecdsa_key", "ed25519_key", "dsa_key",
    "service_key", "application_key", "app_key", "developer_key",
    "consumer_key", "merchant_key", "webhook_key", "recovery_key",
    "backup_key", "data_key", "dek_key", "kek_key", "wrapping_key",
    "transport_key", "identity_key", "device_key", "host_key",
    "cookie_key", "csrf_key", "mac_key", "vault_key", "keystore_key",
    "aws_secret_access_key", "client_secret", "refresh_token", "id_token",
    "sessionKey", "privateKey", "some_unknown_key",
]

# Narrow, generic record-identifier key forms that are not secrets.
RECORD_KEY_FIELDS = [
    "sort_key", "partition_key", "row_key", "primary_key", "foreign_key",
    "hash_key", "range_key", "shard_key", "surrogate_key", "lot_key",
    "batch_key",
]


@pytest.mark.parametrize("field", SECRET_KEY_FIELDS)
def test_credential_key_field_names_stay_sensitive(field):
    assert ris.sensitive_field(field), field


@pytest.mark.parametrize("field", RECORD_KEY_FIELDS)
def test_record_key_field_names_are_not_sensitive(field):
    # Pre-fix: any field ending in "_key" was treated as a secret, so these
    # record identifier columns were never captured as evidence.
    assert not ris.sensitive_field(field), field


def test_identifier_value_is_captured_under_enforce():
    """End to end: a tool result carrying a hyphenated record identifier under
    a classified field is captured as string evidence, where pre-fix the
    personal-data screen dropped it so no evidence was minted."""
    from types import SimpleNamespace

    from agents.response_integrity import (
        AnswerClaim, ClaimInput, ClaimKind, SystemAnswerDraft,
        capture_trusted_tool_result, system_answer_from_result,
    )

    identifier = "ZZX-70010003-44221"
    state: dict = {}
    capture_trusted_tool_result(
        {"content": [{"json": {"items": [{"run_code": identifier}]}}]},
        state, system_id="sys", boundary="rest", tool_name="read",
        current_intent=True,
    )
    answer = system_answer_from_result(SimpleNamespace(
        structured_output=SystemAnswerDraft(claims=(AnswerClaim(
            claim_id="run", kind=ClaimKind.VALUE, label="Run number", value=identifier,
            inputs=(ClaimInput(
                label="run_code", value=identifier, semantic_field="run_code",
                locator="items:0",
            ),),
        ),)),
    ), state)
    # Pre-fix: _canonical returned None, no evidence was minted, status was
    # "unavailable".
    assert answer is not None
    assert any(
        item.semantic_field == "run_code" and item.canonical_type == "string"
        for item in answer.evidence
    ), (answer.status, answer.evidence)
