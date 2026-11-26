# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""agents.auth.identity_claim — mint/verify unit tests (ADR-0049, Wave I2).

Offline, stdlib-only. Pins the compact-claim contract:
roundtrip, ≤5-minute expiry (mint clamp AND verify ceiling), tamper/wrong-secret
rejection via TYPED errors, constant-time signature compare, and the
sub+groups-only payload. The claim is never logged — this suite also pins that
the module performs no logging at all.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import inspect
import json

import pytest

from agents.auth import identity_claim as ic


SECRET = "unit-test-signing-key"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forge(payload: dict, secret: str = SECRET) -> str:
    """Hand-craft a claim (bypassing mint_claim's clamps) signed with ``secret``."""
    seg = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac_mod.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{_b64url(mac)}"


# --------------------------------------------------------------------------- #
# roundtrip + payload shape
# --------------------------------------------------------------------------- #
def test_mint_verify_roundtrip():
    token = ic.mint_claim("user-sub-123", ["ops", "quality"], SECRET, now=1_000_000)
    claims = ic.verify_claim(token, SECRET, now=1_000_010)
    assert claims["sub"] == "user-sub-123"
    assert claims["groups"] == ["ops", "quality"]
    assert claims["iat"] == 1_000_000
    assert claims["exp"] == 1_000_000 + ic.MAX_CLAIM_TTL_S


def test_payload_is_sub_and_groups_only():
    """ADR-0049: sub + groups + timestamps — no other PII fields."""
    token = ic.mint_claim("s", ["g"], SECRET, now=1_000_000)
    payload_seg = token.split(".")[0]
    padded = payload_seg + "=" * (-len(payload_seg) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    assert set(payload.keys()) == {"sub", "groups", "iat", "exp"}


def test_groups_default_empty():
    token = ic.mint_claim("s", None, SECRET, now=1_000_000)
    assert ic.verify_claim(token, SECRET, now=1_000_001)["groups"] == []


# --------------------------------------------------------------------------- #
# expiry — mint clamp AND verify ceiling (≤ 5 minutes, both sides)
# --------------------------------------------------------------------------- #
def test_mint_clamps_ttl_to_five_minutes():
    token = ic.mint_claim("s", [], SECRET, ttl_s=99_999, now=1_000_000)
    claims = ic.verify_claim(token, SECRET, now=1_000_001)
    assert claims["exp"] - claims["iat"] == ic.MAX_CLAIM_TTL_S == 300


def test_expired_claim_raises_typed_error():
    token = ic.mint_claim("s", [], SECRET, now=1_000_000)
    with pytest.raises(ic.ClaimExpiredError):
        ic.verify_claim(token, SECRET, now=1_000_000 + 301)
    # ...and the typed error IS a ClaimVerificationError (catch-all works)
    with pytest.raises(ic.ClaimVerificationError):
        ic.verify_claim(token, SECRET, now=1_000_000 + 301)


def test_verify_rejects_overlong_lifetime_even_with_valid_signature():
    """A leaked key cannot produce long-lived claims this code honors."""
    forged = _forge({"sub": "s", "groups": [], "iat": 1_000_000, "exp": 1_000_000 + 86_400})
    with pytest.raises(ic.ClaimExpiredError):
        ic.verify_claim(forged, SECRET, now=1_000_010)


def test_verify_rejects_future_dated_claim():
    forged = _forge({"sub": "s", "groups": [], "iat": 1_000_500, "exp": 1_000_700})
    with pytest.raises(ic.ClaimExpiredError):
        ic.verify_claim(forged, SECRET, now=1_000_000)


# --------------------------------------------------------------------------- #
# tamper / wrong secret / garbage — typed rejections
# --------------------------------------------------------------------------- #
def test_tampered_payload_rejected():
    token = ic.mint_claim("s", ["viewer"], SECRET, now=1_000_000)
    payload_seg, sig = token.split(".")
    evil_seg = _b64url(
        json.dumps(
            {"sub": "s", "groups": ["admin"], "iat": 1_000_000, "exp": 1_000_300},
            separators=(",", ":"), sort_keys=True,
        ).encode()
    )
    with pytest.raises(ic.ClaimSignatureError):
        ic.verify_claim(f"{evil_seg}.{sig}", SECRET, now=1_000_001)


def test_wrong_secret_rejected():
    token = ic.mint_claim("s", [], SECRET, now=1_000_000)
    with pytest.raises(ic.ClaimSignatureError):
        ic.verify_claim(token, "some-other-key", now=1_000_001)


@pytest.mark.parametrize(
    "garbage",
    [
        "", "no-dot-here", "a.b.c", ".", "onlypayload.", ".onlysig", None, 42,
        # Non-ASCII segments MUST be a typed ClaimFormatError, not an untyped
        # escape (gate finding, reproduced): a non-ASCII SIGNATURE segment used
        # to reach hmac.compare_digest and raise TypeError; a non-ASCII PAYLOAD
        # segment used to reach _sign's .encode("ascii") and raise
        # UnicodeEncodeError — HTTP 500s on the externally reachable authz path.
        "x.sïg",
        "pä.sig",
        "ünïcode.évérywhere",
    ],
)
def test_garbage_tokens_raise_format_error(garbage):
    with pytest.raises((ic.ClaimFormatError, ic.ClaimSignatureError)):
        ic.verify_claim(garbage, SECRET)  # type: ignore[arg-type]


def test_valid_signature_but_bad_schema_rejected():
    forged = _forge({"sub": "", "groups": "not-a-list", "iat": "x", "exp": None})
    with pytest.raises(ic.ClaimFormatError):
        ic.verify_claim(forged, SECRET, now=1_000_000)


def test_empty_secret_rejected_on_both_sides():
    with pytest.raises(ValueError):
        ic.mint_claim("s", [], "")
    token = ic.mint_claim("s", [], SECRET, now=1_000_000)
    with pytest.raises(ic.ClaimSignatureError):
        ic.verify_claim(token, "", now=1_000_001)


def test_empty_subject_rejected_at_mint():
    with pytest.raises(ValueError):
        ic.mint_claim("", ["g"], SECRET)


# --------------------------------------------------------------------------- #
# implementation pins: constant-time compare, no logging
# --------------------------------------------------------------------------- #
def test_verify_uses_constant_time_compare():
    src = inspect.getsource(ic.verify_claim)
    assert "compare_digest" in src


def test_module_never_logs():
    """R4 discipline: claim contents must never be loggable from this module."""
    src = inspect.getsource(ic)
    assert "getLogger" not in src
    assert "logger." not in src


# --------------------------------------------------------------------------- #
# identity_from_access_token — Cognito access-token extraction
# --------------------------------------------------------------------------- #
def _fake_jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}.fakesig"


def test_identity_from_access_token_extracts_sub_and_groups():
    token = _fake_jwt({"sub": "u-1", "cognito:groups": ["ops", "eng"], "scope": "x"})
    assert ic.identity_from_access_token(token) == ("u-1", ["ops", "eng"])


def test_identity_from_access_token_no_groups():
    token = _fake_jwt({"sub": "u-1"})
    assert ic.identity_from_access_token(token) == ("u-1", [])


@pytest.mark.parametrize(
    "bad",
    [None, "", "not-a-jwt", "a.b", "a.!!!.c", _b64url(b"x") + ".e30.s"],
)
def test_identity_from_access_token_garbage_returns_none(bad):
    assert ic.identity_from_access_token(bad) is None  # type: ignore[arg-type]


def test_identity_from_access_token_missing_sub_returns_none():
    assert ic.identity_from_access_token(_fake_jwt({"cognito:groups": ["g"]})) is None
