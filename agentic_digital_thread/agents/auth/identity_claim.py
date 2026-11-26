# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""App-signed user-identity claim — orchestrator → system agents (ADR-0049).

The AgentCore gateway re-authenticates outbound (its targets are OAUTH
CLIENT_CREDENTIALS against the ``adt-m2m`` provider), so the
interactive user's identity does NOT survive the gateway hop natively
(``JWT_PASSTHROUGH`` is rejected for MCP-server targets — see the ADR's
write-probe evidence). Instead, the orchestrator mints a SHORT-LIVED,
HMAC-SHA256-signed compact claim (subject + groups ONLY, ≤5-minute expiry)
and attaches it under the reserved tool-call argument
``RESERVED_CLAIM_ARG`` on INTERACTIVE turns only. System-agent runtimes pop
the argument before query handling and verify it here.

Format (compact, deliberately NOT a JWT — no header/alg field to confuse or
downgrade): ``base64url(json_payload) + "." + base64url(hmac_sha256)``, where
the MAC is computed over the *encoded* payload segment with the
deploy-provisioned shared secret (``adt/identity-claim-key``).

Security rules (pinned by unit tests):
- ``verify_claim`` uses ``hmac.compare_digest`` (constant-time) and checks the
  signature BEFORE parsing the payload JSON.
- Expiry is enforced on BOTH sides: minting clamps the TTL to
  ``MAX_CLAIM_TTL_S`` and verification rejects any claim whose lifetime
  (``exp - iat``) exceeds it — a leaked key cannot produce long-lived claims
  that this code would honor.
- Verification failures raise TYPED errors (``ClaimFormatError`` /
  ``ClaimSignatureError`` / ``ClaimExpiredError``, all subclasses of
  ``ClaimVerificationError``) so callers can distinguish causes WITHOUT
  inspecting claim contents.
- NEVER log claim contents or tokens (R4 discipline — same as
  ``auth/forwarding.py``). This module performs no logging at all.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any, Optional

# Reserved tool-call argument name the orchestrator attaches the claim under and
# system-agent runtimes ALWAYS pop before query handling (even with enforcement
# disabled — external callers must not be able to smuggle it through).
RESERVED_CLAIM_ARG = "_adt_identity"

# Hard ceiling on claim lifetime (seconds). Minting clamps to this; verification
# rejects anything longer — a claim is a per-tool-call artifact, not a session.
MAX_CLAIM_TTL_S = 300  # 5 minutes

# Secrets Manager NAME of the deploy-provisioned signing key (deploy.sh creates
# it idempotently). Runtimes resolve it through the ARN env
# (SYSTEM_IDENTITY_CLAIM_SECRET_ARN); the Quick-gateway interceptor Lambda and
# the app's delegated-identity verifier read it BY NAME (ADR-0079). ONE spelling.
IDENTITY_CLAIM_SECRET_NAME = "adt/identity-claim-key"  # nosec B105 - secret NAME, not a credential


class ClaimVerificationError(Exception):
    """Base class for every claim-verification failure (typed, content-free)."""


class ClaimFormatError(ClaimVerificationError):
    """The claim is not a structurally valid compact token."""


class ClaimSignatureError(ClaimVerificationError):
    """The claim's HMAC does not verify against the provided secret."""


class ClaimExpiredError(ClaimVerificationError):
    """The claim's signature is valid but it is expired (or over-long-lived)."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _secret_bytes(secret: "str | bytes") -> bytes:
    if isinstance(secret, bytes):
        return secret
    return secret.encode("utf-8")


def _sign(payload_segment: str, secret: "str | bytes") -> str:
    mac = hmac.new(_secret_bytes(secret), payload_segment.encode("ascii"), hashlib.sha256)
    return _b64url_encode(mac.digest())


def mint_claim(
    sub: str,
    groups: "list[str] | tuple[str, ...] | None",
    secret: "str | bytes",
    *,
    ttl_s: int = MAX_CLAIM_TTL_S,
    now: "float | None" = None,
) -> str:
    """Mint a signed compact identity claim for ``sub`` with ``groups``.

    ``ttl_s`` is clamped into ``[1, MAX_CLAIM_TTL_S]`` — a claim can never be
    minted with more than a 5-minute lifetime. ``now`` is injectable for tests
    only. Raises ``ValueError`` on an empty subject or secret (a claim without
    a subject is meaningless; signing with an empty key is a misconfiguration
    that must fail loudly, not silently produce a weak token).
    """
    if not sub or not isinstance(sub, str):
        raise ValueError("mint_claim requires a non-empty string subject")
    if not secret:
        raise ValueError("mint_claim requires a non-empty signing secret")
    issued_at = int(now if now is not None else time.time())
    ttl = max(1, min(int(ttl_s), MAX_CLAIM_TTL_S))
    payload = {
        # sub + groups ONLY (no other PII beyond the subject — ADR-0049).
        "sub": sub,
        "groups": [str(g) for g in (groups or [])],
        "iat": issued_at,
        "exp": issued_at + ttl,
    }
    payload_segment = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_segment}.{_sign(payload_segment, secret)}"


def _token_segments(token: str) -> "tuple[str, str]":
    """Split ``token`` into (payload_segment, signature_segment) or raise.

    Structural gate for ``verify_claim``: non-string / wrong dot count / empty
    segments / non-ASCII all raise ``ClaimFormatError``. The ASCII check is part
    of the typed-error contract — without it, a non-ASCII payload segment would
    escape as UnicodeEncodeError from ``_sign``'s ``.encode("ascii")`` and a
    non-ASCII signature segment as TypeError from ``hmac.compare_digest`` —
    untyped exceptions on an externally reachable verification path.
    """
    if not isinstance(token, str) or token.count(".") != 1:
        raise ClaimFormatError("identity claim is not a two-segment compact token")
    if not token.isascii():
        raise ClaimFormatError("identity claim contains non-ASCII characters")
    payload_segment, signature_segment = token.split(".", 1)
    if not payload_segment or not signature_segment:
        raise ClaimFormatError("identity claim has an empty segment")
    return payload_segment, signature_segment


def verify_claim(
    token: str,
    secret: "str | bytes",
    *,
    now: "float | None" = None,
) -> dict:
    """Verify ``token`` and return its payload dict ``{sub, groups, iat, exp}``.

    Raises a TYPED ``ClaimVerificationError`` subclass: ``ClaimFormatError``
    (bad structure/JSON/schema, incl. non-ASCII segments), ``ClaimSignatureError``
    (constant-time HMAC mismatch — checked BEFORE the payload is parsed),
    ``ClaimExpiredError`` (past ``exp``, lifetime over ``MAX_CLAIM_TTL_S``, or
    future-dated ``iat``). Error messages never include claim contents.
    """
    if not secret:
        raise ClaimSignatureError("no signing secret available to verify the claim")
    payload_segment, signature_segment = _token_segments(token)
    expected = _sign(payload_segment, secret)
    # Constant-time compare on the fixed-length base64url MAC segments.
    if not hmac.compare_digest(expected, signature_segment):
        raise ClaimSignatureError("identity claim signature does not verify")
    try:
        payload = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise ClaimFormatError("identity claim payload is not valid JSON")
    if not isinstance(payload, dict):
        raise ClaimFormatError("identity claim payload is not an object")
    sub = payload.get("sub")
    groups = payload.get("groups")
    iat = payload.get("iat")
    exp = payload.get("exp")
    valid_schema = (
        isinstance(sub, str) and sub and isinstance(groups, list)
        and all(isinstance(g, str) for g in groups)
        and isinstance(iat, int) and isinstance(exp, int)
    )
    if not valid_schema:
        raise ClaimFormatError("identity claim payload has an invalid schema")
    current = now if now is not None else time.time()
    if exp <= current:
        raise ClaimExpiredError("identity claim is expired")
    if exp - iat > MAX_CLAIM_TTL_S:
        raise ClaimExpiredError("identity claim lifetime exceeds the 5-minute ceiling")
    if iat > current + 60:  # small skew allowance; beyond that it is bogus
        raise ClaimExpiredError("identity claim is future-dated")
    return {"sub": sub, "groups": groups, "iat": iat, "exp": exp}


def identity_from_access_token(access_token: str) -> "Optional[tuple[str, list[str]]]":
    """Best-effort ``(sub, groups)`` extraction from a Cognito access-token JWT.

    The orchestrator holds the interactive user's ``adt-app`` access token
    (already validated upstream by the app's auth middleware and accepted by the
    gateway's CUSTOM_JWT authorizer on this very turn), so the payload segment is
    decoded WITHOUT signature verification here — this function only re-packages
    ``sub`` + ``cognito:groups`` into the app-signed claim, whose own HMAC is the
    integrity boundary the system agents verify. Returns ``None`` when the token
    is not a decodable JWT or carries no subject (the caller then attaches no
    claim). Never logs the token (R4).
    """
    if not access_token or not isinstance(access_token, str):
        return None
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload: Any = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    raw_groups = payload.get("cognito:groups") or []
    groups = [str(g) for g in raw_groups] if isinstance(raw_groups, list) else []
    return sub, groups
