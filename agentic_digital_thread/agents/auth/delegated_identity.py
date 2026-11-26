# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delegated (on-behalf-of) user identity on the Quick path — app side (ADR-0079).

When the dedicated Quick gateway accepts delegated user tokens from an external
OIDC identity provider, its REQUEST interceptor mints the ADR-0049 app-signed
claim (``_adt_identity``) for the token's subject. The query-bridge runtime pops
that reserved argument (as every runtime does) and forwards the raw claim
VERBATIM to the app in ONE header, ``DELEGATED_IDENTITY_HEADER``. This module
is the ONE home of that header name and the ONE verifier on the app side:

* header ABSENT ⇒ ``None`` — the anonymous ``quick-gateway`` identity (today's
  service-auth connector, the kiosk path, or an interceptor that found no
  subject). No signing-key read happens on this path.
* header PRESENT and valid ⇒ ``DelegatedIdentity(sub)`` — verified with
  ``identity_claim.verify_claim``, the SAME function the runtimes use.
* header PRESENT but unverifiable (malformed, bad signature, expired, or the
  signing key cannot be read) ⇒ ``DelegatedIdentityRejected``. The caller
  answers 401 — it NEVER falls back to the shared identity (fail closed).

The key is read ONLY when the app task carries
``IDENTITY_CLAIM_KEY_READABLE=1``. The compute stack sets it exactly when it
grants the task role that read: the delegated-token mode. On every other
deploy (deploy.sh without the OIDC pair, the workshop CloudFormation path,
local runs) the role has no read, so an attempt could only be AccessDenied;
there no read is made, a present header is rejected as above, and the
onboarding agent test (``onboard_verify_agent._claim_secret``, the other
by-name reader) signs no claim.

The claim, the key and the subject are never logged (R4).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

from .identity_claim import (
    IDENTITY_CLAIM_SECRET_NAME,
    ClaimVerificationError,
    verify_claim,
)

logger = logging.getLogger(__name__)

#: Header the query-bridge uses to forward the interceptor-minted claim to the
#: app. Imported by BOTH sides (agents/query_bridge_server.py and the auth
#: middleware) — never spelled twice.
DELEGATED_IDENTITY_HEADER = "x-adt-identity"

#: 401 detail for a present-but-unverifiable claim. Distinct from the generic
#: "Unauthorized" so a broken chain (wrong key, stale claim) is diagnosable.
DELEGATED_IDENTITY_REJECTED_DETAIL = "delegated identity could not be verified"

#: Env flag on the app task: "1" ⇒ the task role may read the signing key.
#: Set by stacks/adt_compute_stack.py with the grant itself (pinned copy:
#: stacks/_quick_gateway_auth.py). Unset ⇒ the key is never read.
IDENTITY_CLAIM_KEY_READABLE_ENV = "IDENTITY_CLAIM_KEY_READABLE"


def signing_key_granted() -> bool:
    """True when this app task is granted read on the signing key."""
    return (os.environ.get(IDENTITY_CLAIM_KEY_READABLE_ENV) or "").strip() == "1"


@dataclass(frozen=True)
class DelegatedIdentity:
    """A verified delegated user: the external identity provider's subject."""

    sub: str


class DelegatedIdentityRejected(Exception):
    """A delegated-identity header was present but could not be verified."""


_identity_claim_secret: Optional[str] = None
_not_granted_noted = False


def reset_identity_claim_secret_cache() -> None:
    """Clear the cached signing key (tests + key rotation)."""
    global _identity_claim_secret, _not_granted_noted
    _identity_claim_secret = None
    _not_granted_noted = False


def _note_not_granted() -> None:
    """Say ONCE per process that no claim is signed or verified here."""
    global _not_granted_noted
    if _not_granted_noted:
        return
    _not_granted_noted = True
    logger.info("delegated-identity signing key not granted to this app (%s unset): "
                "no identity claim is read, signed or verified here",
                IDENTITY_CLAIM_KEY_READABLE_ENV)


def _fetch_identity_claim_secret() -> Optional[str]:
    """Return the signing key read BY NAME, or None. Cached on success only.

    None without a read when this app is not granted the key
    (``signing_key_granted``): the read could only be refused."""
    global _identity_claim_secret
    if not signing_key_granted():
        _note_not_granted()
        return None
    if _identity_claim_secret:
        return _identity_claim_secret
    try:
        import boto3  # lazy: only a request carrying the header reaches here

        resp = boto3.client("secretsmanager").get_secret_value(
            SecretId=IDENTITY_CLAIM_SECRET_NAME)
        secret = resp.get("SecretString") or ""
    except Exception as exc:  # noqa: BLE001 - value-free warning, caller fails closed
        logger.warning("delegated-identity signing key read failed (%s)",
                       type(exc).__name__)
        return None
    if not secret:
        return None
    _identity_claim_secret = secret
    return secret


def resolve_delegated_identity(headers: Mapping[str, str]) -> Optional[DelegatedIdentity]:
    """Verify the delegated-identity header, if any.

    Returns ``None`` when the header is absent or empty (anonymous — no key
    read). Raises ``DelegatedIdentityRejected`` when it is present but cannot
    be verified, including when the signing key is unreadable or not granted
    to this app.
    """
    token = (headers.get(DELEGATED_IDENTITY_HEADER) or "").strip()
    if not token:
        return None
    secret = _fetch_identity_claim_secret()
    if not secret:
        raise DelegatedIdentityRejected("signing key unavailable")
    try:
        claims = verify_claim(token, secret)
    except ClaimVerificationError as exc:
        raise DelegatedIdentityRejected(type(exc).__name__) from None
    return DelegatedIdentity(sub=claims["sub"])
