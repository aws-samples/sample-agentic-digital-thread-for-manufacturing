# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Quick-gateway REQUEST interceptor — delegated user identity (ADR-0079).

Attached to the dedicated Quick gateway (``adt-quick-gw``) ONLY when the
deployment accepts delegated (on-behalf-of) user tokens from an external OIDC
identity provider. The gateway's CUSTOM_JWT authorizer has ALREADY validated
the inbound bearer (signature, issuer, expiry, audience) before this function
runs; the interceptor re-packages, it never re-authenticates:

* every JSON-RPC message other than ``tools/call`` passes through unchanged;
* for ``tools/call`` it derives the token's subject and sets the ADR-0049
  reserved argument ``_adt_identity`` to a FRESH app-signed claim
  ``(sub, groups=[])`` — OVERWRITING any caller-supplied value. With no
  subject (or no readable signing key) the argument is STRIPPED, so the call
  continues anonymously. This function is the SOLE writer of the argument on
  this gateway.

The claim is minted by ``identity_claim`` — a staged COPY of the ONE claim
implementation (``agents/auth/identity_claim.py``), never re-implemented here.
The output carries ONLY the rewritten ``body`` (headers are never echoed, so
the inbound bearer is never forwarded toward the target). Tokens, claims,
subjects and headers are never logged; the one telemetry line is value-free.

Stdlib + boto3 (Lambda runtime) only.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import identity_claim  # staged next to this file (stacks/_quick_gateway_auth.py)

# A NAMED logger at INFO (propagates to the Lambda runtime's root handler) —
# never the root logger's level, which would leak into other code.
logger = logging.getLogger("adt.quick_interceptor")
logger.setLevel(logging.INFO)

OUTPUT_VERSION = "1.0"


@dataclass(frozen=True)
class DelegatedSubject:
    """The delegated user's subject + whether an actor client id was present."""

    sub: str
    actor_client_present: bool = False


def _b64url_json(segment: str) -> Optional[dict]:
    """Decode one base64url JWT segment into a dict, or None."""
    try:
        padded = segment + "=" * (-len(segment) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        return None
    return value if isinstance(value, dict) else None


def _bearer_payload(headers: Any) -> Optional[dict]:
    """The (already gateway-validated) bearer's payload claims, or None."""
    if not isinstance(headers, dict):
        return None
    value = next((v for k, v in headers.items()
                  if isinstance(k, str) and k.lower() == "authorization"), None)
    if not isinstance(value, str):
        return None
    token = value.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    return _b64url_json(parts[1]) if len(parts) == 3 else None


def _subject_from_claims(claims: Optional[dict]) -> Optional[DelegatedSubject]:
    if not isinstance(claims, dict):
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    actor = claims.get("act")
    present = isinstance(actor, dict) and bool(actor.get("client_id"))
    return DelegatedSubject(sub=sub, actor_client_present=present)


def extract_delegated_subject(event: Any) -> Optional[DelegatedSubject]:
    """ORDERED lookup: (1) a validated-claims block the gateway may attach
    (shape pinned from a sanitized live capture — ADR-0079 V2); else (2) the
    payload segment of the ``Authorization`` bearer the gateway validated."""
    mcp = event.get("mcp") if isinstance(event, dict) else None
    request = mcp.get("gatewayRequest") if isinstance(mcp, dict) else None
    if not isinstance(request, dict):
        return None
    for key in ("validatedClaims", "claims"):
        subject = _subject_from_claims(request.get(key))
        if subject is not None:
            return subject
    return _subject_from_claims(_bearer_payload(request.get("headers")))


_secret_cache: Optional[str] = None


def _fetch_secret() -> Optional[str]:
    """The signing key read BY NAME (cached on success), or None. Value-free logs."""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    try:
        import boto3

        resp = boto3.client("secretsmanager").get_secret_value(
            SecretId=identity_claim.IDENTITY_CLAIM_SECRET_NAME)
        secret = resp.get("SecretString") or ""
    except Exception as exc:  # noqa: BLE001 - fail to anonymous, never a guessed identity
        logger.warning(json.dumps({"event": "quick_interceptor_key_unreadable",
                                   "error_type": type(exc).__name__}))
        return None
    _secret_cache = secret or None
    return _secret_cache


def _rewrite_one(message: Any, subject: Optional[DelegatedSubject],
                 secret: Optional[str]) -> Optional[str]:
    """Rewrite ONE JSON-RPC message in place. Returns the outcome, or None
    when the message is not a ``tools/call`` (untouched)."""
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
        params["arguments"] = arguments
    had = arguments.pop(identity_claim.RESERVED_CLAIM_ARG, None) is not None
    if subject is not None and secret:
        arguments[identity_claim.RESERVED_CLAIM_ARG] = identity_claim.mint_claim(
            subject.sub, [], secret)
        return "minted"
    return "stripped" if had else "absent"


def rewrite_tool_call(body: Any, subject: Optional[DelegatedSubject],
                      secret: Optional[str]) -> tuple[Any, list[str]]:
    """Apply ``_rewrite_one`` to the body (a single message or a defensive
    top-level batch list). Returns ``(body, outcomes)``."""
    messages = body if isinstance(body, list) else [body]
    outcomes = [o for o in (_rewrite_one(m, subject, secret) for m in messages) if o]
    return body, outcomes


def handler(event, context):  # noqa: ARG001 - Lambda signature
    """Gateway REQUEST interceptor entry point (MCP target payload shape)."""
    mcp = event.get("mcp") if isinstance(event, dict) else None
    request = mcp.get("gatewayRequest") if isinstance(mcp, dict) else None
    body = request.get("body") if isinstance(request, dict) else None
    subject = extract_delegated_subject(event)
    needs_mint = subject is not None and _is_tool_call(body)
    secret = _fetch_secret() if needs_mint else None
    body, outcomes = rewrite_tool_call(body, subject, secret)
    for outcome in outcomes:
        logger.info(json.dumps({
            "event": "quick_interceptor", "identity": outcome, "method": "tools/call",
            "actor_client_present": bool(subject and subject.actor_client_present)}))
    return {"interceptorOutputVersion": OUTPUT_VERSION,
            "mcp": {"transformedGatewayRequest": {"body": body}}}


def _is_tool_call(body: Any) -> bool:
    messages = body if isinstance(body, list) else [body]
    return any(isinstance(m, dict) and m.get("method") == "tools/call" for m in messages)
